from datetime import datetime, timezone
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import func, select

from app.config import Settings
from app.games.importer import import_snapshot
from app.games.live_test import capture, serialize
from app.games.provider import FussballDeProvider, ProviderError
from app.models import AuditLog, Game, InstagramPage, Post, ProviderSnapshot, Role, Team, User
from app.posts.service import create_post
from app.rendering.service import Renderer
from app.textgen.service import FixtureTextGenerator

FIXTURE=Path("tests/fixtures/fussball_sv_ehlen_2627.html")

def real_records(): return FussballDeProvider().parse(FIXTURE.read_text(encoding="utf-8"))

def entities(db):
    page=InstagramPage(internal_name="p",display_name="P",username="p",club="SV",active=True,connection_status="connected"); db.add(page); db.flush()
    team=Team(internal_name="ehlen",display_name="SV Ehlen",short_name="SVE",slug="sv-ehlen",club="SV Ehlen",fussball_url="https://www.fussball.de/mannschaft/sv-ehlen/-/team-id/011MI9UQI4000000VTVG0001VTR8C1K7",instagram_page_id=page.id,media_subdir="ehlen"); user=User(email="admin@x",password_hash="x",role=Role.ADMIN,all_teams=True); db.add_all([team,user]); db.commit(); return team,user

def snapshot(db,team):
    games=[serialize(record) for record in real_records()]; item=ProviderSnapshot(team_id=team.id,source_url=team.fussball_url,status_code=200,checksum="a"*64,relative_path="test/snapshot.html",parser_result={"team_name":"SV Ehlen","games":games,"parser_warnings":["vorläufig"]}); db.add(item); db.commit(); return item

def test_real_matchplan_ids_sides_times_names_and_provisional():
    records=real_records(); assert len(records)==3
    first,second,third=records
    assert first.external_id=="0318JUMQIS000000VS5489BUVV628VP4" and first.tracked_team_side=="away"
    assert first.kickoff==datetime(2026,8,2,11,15,tzinfo=timezone.utc)
    assert second.kickoff==datetime(2026,8,9,13,0,tzinfo=timezone.utc) and second.tracked_team_side=="home"
    assert second.away_team=="SG Weser/Diemel" and "\u200b" not in second.away_team
    assert third.game_number=="340583017" and all(x.status=="provisional" for x in records)
    assert all(x.competition=="Kreisliga A" and x.home_score is None and x.away_score is None for x in records)
    assert all(any("Symbolschrift" in warning for warning in x.warnings) for x in records)

def test_plain_numeric_score_is_accepted_but_obfuscated_is_not():
    soup=BeautifulSoup(FIXTURE.read_text(encoding="utf-8"),"html.parser"); soup.select_one(".hint-pre-publish").decompose(); score=soup.select_one(".column-score"); href=score.select_one("a")["href"]; score.clear(); anchor=soup.new_tag("a",href=href); anchor.string="2 : 1"; score.append(anchor)
    record=FussballDeProvider().parse(str(soup))[0]
    assert (record.home_score,record.away_score,record.status)==(2,1,"scheduled")

def test_incomplete_meta_rows_missing_links_and_duplicate_ids_are_safe():
    html='''<div id="id-team-matchplan-table"><table><tr class="row-competition"><td class="column-date">unbekannt</td></tr><tr><td class="column-club"><div class="club-name">A</div></td><td class="column-club"><div class="club-name">B</div></td></tr></table></div>'''
    with pytest.raises(ProviderError,match="Keine Spiele"): FussballDeProvider().parse(html)
    soup=BeautifulSoup(FIXTURE.read_text(encoding="utf-8"),"html.parser"); rows=soup.select("#id-team-matchplan-table tr"); rows[-1].decompose(); rows[-1].decompose()
    assert len(FussballDeProvider().parse(str(soup)))==2

def test_ajax_url_allowlist_blocks_foreign_hosts_protocols_and_paths():
    provider=FussballDeProvider()
    assert provider.validate_public_url("https://www.fussball.de/ajax.team.prev.games/-/mode/PAGE",ajax_only=True)
    for url in ("http://www.fussball.de/ajax.team.prev.games/x","https://evil.example/ajax.team.prev.games/x","https://www.fussball.de/private/resource"):
        with pytest.raises(ProviderError): provider.validate_public_url(url,ajax_only=True)

def test_read_only_diagnostic_only_creates_snapshot(db,tmp_path,monkeypatch):
    team,_=entities(db); monkeypatch.setattr(FussballDeProvider,"fetch_html",lambda self,url: FIXTURE.read_text(encoding="utf-8"))
    before_games=db.scalar(select(func.count()).select_from(Game)); before_posts=db.scalar(select(func.count()).select_from(Post))
    item=capture(db,team,Settings(fussball_live_test_enabled=True,provider_snapshot_root=tmp_path))
    assert len(item.parser_result["games"])==3 and item.parser_result["team_name"]=="SV Ehlen"
    assert db.scalar(select(func.count()).select_from(Game))==before_games and db.scalar(select(func.count()).select_from(Post))==before_posts

def test_manual_import_is_idempotent_audited_and_blocks_provisional_posts(db,tmp_path):
    team,user=entities(db); item=snapshot(db,team)
    first=import_snapshot(db,item,user); second=import_snapshot(db,item,user)
    assert first["created"]==3 and second=={"created":0,"updated":0,"unchanged":3,"game_ids":second["game_ids"]}
    games=list(db.scalars(select(Game).where(Game.team_id==team.id))); assert len(games)==3 and all(x.status=="provisional" and x.overrides["automation_blocked"] for x in games)
    assert db.scalar(select(func.count()).select_from(Post))==0
    assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action=="provider_snapshot.games_imported"))==2
    with pytest.raises(ValueError,match="Vorläufige Spiele"): create_post(db,games[0],team,FixtureTextGenerator(),Renderer(tmp_path/"render"))

def test_controlled_update_same_external_id(db):
    team,user=entities(db); item=snapshot(db,team); import_snapshot(db,item,user)
    changed=item.parser_result.copy(); changed["games"]=[dict(game) for game in item.parser_result["games"]]; changed["games"][0]["kickoff"]="2026-08-02T12:15:00+00:00"; item.parser_result=changed; db.commit()
    result=import_snapshot(db,item,user); game=db.scalar(select(Game).where(Game.external_id==changed["games"][0]["external_id"]))
    assert result["updated"]==1 and game.original_kickoff is not None and game.kickoff.hour==12
    changed["games"][0]["status"]="scheduled"; item.parser_result=changed; db.commit(); import_snapshot(db,item,user); db.refresh(game)
    assert game.status=="scheduled" and not game.overrides["automation_blocked"]
