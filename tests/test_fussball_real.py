from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import func, select

from app.config import Settings
from app.games.importer import import_snapshot
from app.games.live_test import capture, serialize
from app.games.provider import (
    FussballDeProvider,
    GameRecord,
    GameVenueDetail,
    ProviderError,
)
from app.models import (
    AuditLog,
    Game,
    InstagramPage,
    JobStatus,
    Post,
    PostStatus,
    ProviderSnapshot,
    PublicationJob,
    Role,
    Team,
    User,
)
from app.posts.service import create_post
from app.publishing.service import MockPublisher, PublishError
from app.publishing.worker import process_job
from app.rendering.service import Renderer
from app.textgen.service import FixtureTextGenerator

FIXTURE=Path("tests/fixtures/fussball_sv_ehlen_2627.html")
DETAIL_FIXTURE = Path("tests/fixtures/fussball_game_detail.html")

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


def test_real_game_detail_extracts_pitch_venue_and_address():
    detail = FussballDeProvider.parse_game_detail(
        DETAIL_FIXTURE.read_text(encoding="utf-8"),
        expected_external_id="0318JUMQIS000000VS5489BUVV628VP4",
    )
    assert detail == GameVenueDetail(
        pitch="Rasenplatz",
        venue="RP Immenhausen (Stadion)",
        address="Bernhardt-Vocke-Str. 5, 34376 Immenhausen",
    )
    with pytest.raises(ProviderError, match="Spiel-ID"):
        FussballDeProvider.parse_game_detail(
            DETAIL_FIXTURE.read_text(encoding="utf-8"),
            expected_external_id="WRONG",
        )


def test_detail_url_is_restricted_to_matching_fussball_game():
    provider = FussballDeProvider()
    valid = (
        "https://www.fussball.de/spiel/tsv-immenhausen-ii-sv-ehlen/-/spiel/"
        "0318JUMQIS000000VS5489BUVV628VP4"
    )
    assert provider.validate_game_detail_url(
        valid, "0318JUMQIS000000VS5489BUVV628VP4"
    )
    for url in (
        "https://evil.example/spiel/x/-/spiel/0318JUMQIS000000VS5489BUVV628VP4",
        "https://www.fussball.de/mannschaft/sv-ehlen",
    ):
        with pytest.raises(ProviderError):
            provider.validate_game_detail_url(url, "0318JUMQIS000000VS5489BUVV628VP4")


def test_provisional_hint_does_not_hide_cancellation():
    soup = BeautifulSoup(FIXTURE.read_text(encoding="utf-8"), "html.parser")
    first_game_row = next(
        row
        for row in soup.select("#id-team-matchplan-table tr")
        if len(row.select(".column-club .club-name")) >= 2
    )
    first_game_row.append(" abgesagt ")
    assert FussballDeProvider().parse(str(soup))[0].status == "cancelled"

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
    monkeypatch.setattr(
        FussballDeProvider,
        "fetch_game_detail",
        lambda self, url, expected_external_id: GameVenueDetail(
            venue="RP Immenhausen (Stadion)",
            pitch="Rasenplatz",
            address="Bernhardt-Vocke-Str. 5, 34376 Immenhausen",
        ),
    )
    before_games=db.scalar(select(func.count()).select_from(Game)); before_posts=db.scalar(select(func.count()).select_from(Post))
    item=capture(db,team,Settings(fussball_live_test_enabled=True,provider_snapshot_root=tmp_path))
    assert len(item.parser_result["games"])==3 and item.parser_result["team_name"]=="SV Ehlen"
    assert item.parser_result["games"][0]["venue"] == "RP Immenhausen (Stadion)"
    assert item.parser_result["games"][0]["pitch"] == "Rasenplatz"
    assert db.scalar(select(func.count()).select_from(Game))==before_games and db.scalar(select(func.count()).select_from(Post))==before_posts


def test_detail_enrichment_failure_keeps_matchplan_record(monkeypatch):
    provider = FussballDeProvider()
    record = real_records()[0]

    def fail(*args, **kwargs):
        raise ProviderError("temporär nicht erreichbar")

    monkeypatch.setattr(provider, "fetch_game_detail", fail)
    enriched = provider.enrich_game_details([record], delay_seconds=0)[0]
    assert enriched.external_id == record.external_id
    assert enriched.venue is None and enriched.pitch is None
    assert any("temporär nicht erreichbar" in warning for warning in enriched.warnings)

def test_detail_enrichment_limit_keeps_remaining_matchplan_records(monkeypatch):
    provider = FussballDeProvider()
    records = [
        GameRecord(
            f"GAME{index}",
            "SV Ehlen",
            f"Gegner {index}",
            datetime(2026, 8, 1, 13, tzinfo=timezone.utc),
            source_url=f"https://www.fussball.de/spiel/test/-/spiel/GAME{index}",
        )
        for index in range(26)
    ]
    calls = []

    def fake_fetch(url, expected_external_id):
        calls.append((url, expected_external_id))
        return GameVenueDetail(venue="Sportplatz", pitch="Rasenplatz")

    monkeypatch.setattr(provider, "fetch_game_detail", fake_fetch)
    enriched = provider.enrich_game_details(records, delay_seconds=0)

    assert len(enriched) == len(records)
    assert len(calls) == 25
    assert enriched[24].venue == "Sportplatz"
    assert enriched[25].venue is None
    assert "Abruflimit" in enriched[25].warnings[-1]


def test_manual_import_is_idempotent_audited_and_blocks_provisional_posts(db,tmp_path):
    team,user=entities(db); item=snapshot(db,team)
    first=import_snapshot(db,item,user); second=import_snapshot(db,item,user)
    assert first["created"]==3 and second=={"created":0,"updated":0,"unchanged":3,"game_ids":second["game_ids"]}
    games=list(db.scalars(select(Game).where(Game.team_id==team.id))); assert len(games)==3 and all(x.status=="provisional" and x.overrides["automation_blocked"] for x in games)
    assert db.scalar(select(func.count()).select_from(Post))==0
    assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action=="provider_snapshot.games_imported"))==2
    with pytest.raises(ValueError,match="Vorläufige Spiele"): create_post(db,games[0],team,FixtureTextGenerator(),Renderer(tmp_path/"render"))

def test_team_rule_allows_provisional_games_but_not_cancellations(db):
    team, user = entities(db)
    team.rules = {"allow_provisional_games": True}
    db.commit()
    item = snapshot(db, team)
    import_snapshot(db, item, user)
    game = db.scalar(
        select(Game).where(Game.external_id == item.parser_result["games"][0]["external_id"])
    )
    assert game.status == "scheduled"
    assert game.overrides["provider_status"] == "provisional"
    assert game.overrides["provisional_allowed_by_team_rule"]
    assert not game.overrides["automation_blocked"]

    update_snapshot_game(db, item, status="cancelled")
    import_snapshot(db, item, user)
    db.refresh(game)
    assert game.status == "cancelled" and game.overrides["automation_blocked"]


def test_imports_enriched_venue_and_preserves_manual_details(db):
    team, user = entities(db)
    item = snapshot(db, team)
    update_snapshot_game(
        db,
        item,
        venue="RP Immenhausen (Stadion)",
        pitch="Rasenplatz",
        venue_address="Bernhardt-Vocke-Str. 5, 34376 Immenhausen",
    )
    import_snapshot(db, item, user)
    game = db.scalar(
        select(Game).where(Game.external_id == item.parser_result["games"][0]["external_id"])
    )
    assert game.venue == "RP Immenhausen (Stadion)"
    assert game.pitch == "Rasenplatz"
    assert game.overrides["venue_address"] == (
        "Bernhardt-Vocke-Str. 5, 34376 Immenhausen"
    )

    update_snapshot_game(
        db,
        item,
        venue=None,
        pitch=None,
        venue_address=None,
    )
    import_snapshot(db, item, user)
    db.refresh(game)
    assert game.venue == "RP Immenhausen (Stadion)"
    assert game.pitch == "Rasenplatz"
    assert game.overrides["venue_address"] == (
        "Bernhardt-Vocke-Str. 5, 34376 Immenhausen"
    )

    game.venue = "Manuell gepflegter Ort"
    game.pitch = "Kunstrasenplatz"
    game.overrides = {**game.overrides, "manual_venue_details": True}
    db.commit()
    update_snapshot_game(
        db,
        item,
        venue="Anderer Provider-Ort",
        pitch="Rasenplatz",
        venue_address="Neue Straße 1, 12345 Ort",
    )
    import_snapshot(db, item, user)
    db.refresh(game)
    assert game.venue == "Manuell gepflegter Ort"
    assert game.pitch == "Kunstrasenplatz"
    assert game.overrides["venue_address"] == (
        "Bernhardt-Vocke-Str. 5, 34376 Immenhausen"
    )


def test_controlled_update_same_external_id(db):
    team,user=entities(db); item=snapshot(db,team); import_snapshot(db,item,user)
    changed=item.parser_result.copy(); changed["games"]=[dict(game) for game in item.parser_result["games"]]; changed["games"][0]["kickoff"]="2026-08-02T12:15:00+00:00"; item.parser_result=changed; db.commit()
    result=import_snapshot(db,item,user); game=db.scalar(select(Game).where(Game.external_id==changed["games"][0]["external_id"]))
    assert result["updated"]==1 and game.original_kickoff is not None and game.kickoff.hour==12
    changed["games"][0]["status"]="scheduled"; item.parser_result=changed; db.commit(); import_snapshot(db,item,user); db.refresh(game)
    assert game.status=="scheduled" and not game.overrides["automation_blocked"]


def approved_publications(db, game, team, user, tmp_path):
    page = db.get(InstagramPage, team.instagram_page_id)
    page.publishing_enabled = True
    page.account_id = "dry-run"
    media = tmp_path / "feed.png"
    media.write_bytes(b"png")
    post = Post(
        game_id=game.id,
        team_id=team.id,
        instagram_page_id=page.id,
        post_type="announcement",
        status=PostStatus.APPROVED,
        text="Test",
        feed_path=str(media),
        approved_by=user.id,
        approved_version=1,
    )
    db.add(post)
    db.flush()
    relative = PublicationJob(
        post_id=post.id,
        game_id=game.id,
        team_id=team.id,
        instagram_page_id=page.id,
        kind="feed",
        media_path=str(media),
        scheduled_at=game.kickoff - timedelta(hours=24),
        status=JobStatus.SCHEDULED,
        approval_status="approved",
        approved_post_version=1,
        idempotency_key=f"{post.id}:relative",
    )
    absolute = PublicationJob(
        post_id=post.id,
        game_id=game.id,
        team_id=team.id,
        instagram_page_id=page.id,
        kind="story",
        media_path=str(media),
        scheduled_at=game.kickoff - timedelta(hours=12),
        absolute_time=True,
        status=JobStatus.SCHEDULED,
        approval_status="approved",
        approved_post_version=1,
        idempotency_key=f"{post.id}:absolute",
    )
    db.add_all([relative, absolute])
    db.commit()
    return post, relative, absolute


def update_snapshot_game(db, item, **changes):
    parser_result = dict(item.parser_result)
    parser_result["games"] = [dict(game) for game in item.parser_result["games"]]
    parser_result["games"][0].update(changes)
    item.parser_result = parser_result
    db.commit()


def test_kickoff_change_requires_reapproval_and_reschedules_relative_only(db, tmp_path):
    team, user = entities(db)
    item = snapshot(db, team)
    import_snapshot(db, item, user)
    game = db.scalar(select(Game).where(Game.external_id == item.parser_result["games"][0]["external_id"]))
    post, relative, absolute = approved_publications(db, game, team, user, tmp_path)
    old_relative, old_absolute = relative.scheduled_at, absolute.scheduled_at

    update_snapshot_game(db, item, kickoff="2026-08-02T13:15:00+00:00")
    import_snapshot(db, item, user)
    db.refresh(post); db.refresh(relative); db.refresh(absolute)

    assert post.status == PostStatus.REAPPROVAL and post.version == 2
    assert relative.scheduled_at == old_relative + timedelta(hours=2)
    assert not relative.stale_time
    assert absolute.scheduled_at == old_absolute and absolute.stale_time
    assert all(job.status == JobStatus.UNAPPROVED for job in (relative, absolute))
    assert all(job.approval_status == "reapproval_required" for job in (relative, absolute))


def test_pending_post_jobs_are_rescheduled_without_invalidating_post(db, tmp_path):
    team, user = entities(db)
    item = snapshot(db, team)
    import_snapshot(db, item, user)
    game = db.scalar(select(Game).where(Game.external_id == item.parser_result["games"][0]["external_id"]))
    post, relative, absolute = approved_publications(db, game, team, user, tmp_path)
    post.status = PostStatus.PENDING
    post.approved_version = None
    original_version = post.version
    for job in (relative, absolute):
        job.status = JobStatus.UNAPPROVED
        job.approval_status = "unapproved"
        job.approved_post_version = None
    old_relative, old_absolute = relative.scheduled_at, absolute.scheduled_at
    db.commit()

    update_snapshot_game(db, item, kickoff="2026-08-02T13:15:00+00:00")
    import_snapshot(db, item, user)
    db.refresh(post); db.refresh(relative); db.refresh(absolute)

    assert post.status == PostStatus.PENDING and post.version == original_version
    assert relative.scheduled_at == old_relative + timedelta(hours=2)
    assert relative.status == JobStatus.UNAPPROVED and not relative.stale_time
    assert absolute.scheduled_at == old_absolute and absolute.stale_time
    assert absolute.status == JobStatus.UNAPPROVED


def test_cancellation_blocks_approved_publications(db, tmp_path):
    team, user = entities(db); item = snapshot(db, team); import_snapshot(db, item, user)
    game = db.scalar(select(Game).where(Game.external_id == item.parser_result["games"][0]["external_id"]))
    game.status = "scheduled"; game.overrides = {**game.overrides, "automation_blocked": False}; db.commit()
    post, relative, _ = approved_publications(db, game, team, user, tmp_path)
    update_snapshot_game(db, item, status="cancelled")
    import_snapshot(db, item, user); db.refresh(game); db.refresh(post); db.refresh(relative)
    assert game.status == "cancelled" and game.overrides["automation_blocked"]
    assert post.status == PostStatus.REAPPROVAL
    assert relative.status == JobStatus.UNAPPROVED


def test_result_confirmation_and_manual_overrides_survive_safe_reimports(db):
    team, user = entities(db); item = snapshot(db, team); import_snapshot(db, item, user)
    game = db.scalar(select(Game).where(Game.external_id == item.parser_result["games"][0]["external_id"]))
    game.home_score = 2; game.away_score = 1; game.result_confirmed = True
    game.status = "scheduled"
    game.overrides = {
        **game.overrides,
        "manual_feed_at": "2026-08-01T12:00:00Z",
        "provisional_confirmed_by": user.id,
        "provisional_confirmed_at": "2026-07-20T12:00:00Z",
    }
    update_snapshot_game(db, item, home_score=2, away_score=1)
    import_snapshot(db, item, user); db.refresh(game)
    assert game.result_confirmed and game.status == "scheduled"
    assert game.overrides["provider_status"] == "provisional"
    assert not game.overrides["automation_blocked"]
    assert game.overrides["manual_feed_at"] == "2026-08-01T12:00:00Z"
    update_snapshot_game(db, item, home_score=3, away_score=1)
    import_snapshot(db, item, user); db.refresh(game)
    assert not game.result_confirmed


def test_worker_rejects_blocked_or_stale_game(db, tmp_path):
    team, user = entities(db); item = snapshot(db, team); import_snapshot(db, item, user)
    game = db.scalar(select(Game).where(Game.external_id == item.parser_result["games"][0]["external_id"]))
    game.status = "scheduled"; game.overrides = {**game.overrides, "automation_blocked": False}; db.commit()
    post, job, _ = approved_publications(db, game, team, user, tmp_path)
    post.version = 1; job.scheduled_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    game.overrides = {**game.overrides, "automation_blocked": True}; db.commit()
    with pytest.raises(PublishError, match="Automatisierung"):
        process_job(db, job.id, MockPublisher(), Settings(global_publish_enabled=True))
    db.refresh(job)
    assert job.attempts == 0 and job.status == JobStatus.SCHEDULED
