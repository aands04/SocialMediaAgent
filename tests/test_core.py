from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.approvals.service import ApprovalError, approve, edit_text
from app.auth.service import allowed, hash_password
from app.config import Settings
from app.games.provider import FussballDeProvider, ProviderError
from app.media.storage import LocalStorageProvider, StorageError
from app.models import (
 Game,
 InstagramPage,
 JobStatus,
 MediaAsset,
 PostStatus,
 PublicationJob,
 Role,
 StoryRule,
 Team,
 User,
 UserTeam,
)
from app.posts.service import create_post, reschedule_game, reserve_image, story_time
from app.publishing.service import DryRunPublisher, PublishError
from app.publishing.worker import process_job
from app.rendering.service import Renderer
from app.textgen.service import FixtureTextGenerator


def graph(db,tmp_path):
 page=InstagramPage(internal_name="main",display_name="Hauptseite",username="club",club="SV",active=True,connection_status="connected",publishing_enabled=True,account_id="42"); db.add(page); db.flush()
 team=Team(internal_name="one",display_name="Erste",short_name="I",slug="erste",club="SV",fussball_url="https://www.fussball.de/x",instagram_page_id=page.id,media_subdir="erste",rules={"feed_before_minutes":60}); db.add(team); db.flush()
 game=Game(team_id=team.id,external_id="g1",home_team="SV",away_team="FC",kickoff=datetime.now(timezone.utc)+timedelta(hours=3),source_url=team.fussball_url); db.add(game); db.commit(); return page,team,game

def test_password_and_team_scope(db,tmp_path):
 _,team,_=graph(db,tmp_path); user=User(email="e@x.de",password_hash=hash_password("long-enough"),role=Role.EDITOR,all_teams=False); db.add(user); db.commit()
 assert not allowed(db,user,"edit_post",team.id); db.add(UserTeam(user_id=user.id,team_id=team.id)); db.commit(); assert allowed(db,user,"edit_post",team.id); assert not allowed(db,user,"approve",team.id)
def test_storage_blocks_escape_and_bad_type(media_root):
 store=LocalStorageProvider(media_root); (media_root/"x.jpg").write_bytes(b"x")
 assert store.validate_file("x.jpg").name=="x.jpg"
 with pytest.raises(StorageError): store.resolve("../secret")
 (media_root/"x.exe").write_bytes(b"x")
 with pytest.raises(StorageError): store.validate_file("x.exe")
def test_fussball_fixture_and_structure_change():
 html=Path("tests/fixtures/games.html").read_text(); result=FussballDeProvider().parse(html)
 assert result[0].external_id=="abc-123" and result[0].home_score==2
 with pytest.raises(ProviderError): FussballDeProvider().parse("<html></html>")
def test_story_times_and_collision_safe_generation(db,tmp_path):
 _,team,game=graph(db,tmp_path); r=StoryRule(team_id=team.id,name="24h",post_type="announcement",reference="kickoff",direction="before",offset_minutes=60,template="s"); db.add(r); db.commit()
 assert story_time(r,game)==game.kickoff-timedelta(hours=1)
 post=create_post(db,game,team,FixtureTextGenerator(),Renderer(tmp_path/"out")); jobs=db.query(PublicationJob).filter_by(post_id=post.id).all()
 assert post.status==PostStatus.INCOMPLETE and len(jobs)==2
 assert __import__('PIL').Image.open(post.feed_path).size==(1080,1350)
 assert __import__('PIL').Image.open([x.media_path for x in jobs if x.kind=='story'][0]).size==(1080,1920)
def test_image_reserved_once_per_matchday(db,tmp_path):
 _,team,game=graph(db,tmp_path); p=tmp_path/"a.jpg";p.write_bytes(b"x"); asset=MediaAsset(team_id=team.id,relative_path="a.jpg",filename="a.jpg",mime_type="image/jpeg",size=1,checksum="x",mtime=datetime.now(timezone.utc));db.add(asset);db.commit()
 assert reserve_image(db,team.id,game.id).id==asset.id; assert reserve_image(db,team.id,game.id).id==asset.id and asset.uses==1
def test_approval_and_publish_gate(db,tmp_path):
 page,team,game=graph(db,tmp_path); post=create_post(db,game,team,FixtureTextGenerator(),Renderer(tmp_path/"out")); post.critical_warnings=[]; user=User(email="a@x.de",password_hash="x",role=Role.APPROVER,all_teams=True);db.add(user);db.commit()
 job=db.query(PublicationJob).filter_by(post_id=post.id).first(); job.scheduled_at=datetime.now(timezone.utc)-timedelta(seconds=1);db.commit()
 with pytest.raises(PublishError): process_job(db,job.id,DryRunPublisher(),Settings(global_publish_enabled=True))
 approve(db,post,user); done=process_job(db,job.id,DryRunPublisher(),Settings(global_publish_enabled=True)); assert done.status==JobStatus.PUBLISHED
def test_optimistic_lock_and_reapproval(db,tmp_path):
 _,team,game=graph(db,tmp_path); post=create_post(db,game,team,FixtureTextGenerator(),Renderer(tmp_path/"out")); user=User(email="a@x.de",password_hash="x",role=Role.EDITOR,all_teams=True);db.add(user);db.commit(); version=post.version
 edit_text(db,post,user,"Neu",version)
 with pytest.raises(ApprovalError): edit_text(db,post,user,"Alt",version)
def test_reschedule_relative_but_not_absolute(db,tmp_path):
 _,team,game=graph(db,tmp_path); post=create_post(db,game,team,FixtureTextGenerator(),Renderer(tmp_path/"out")); jobs=db.query(PublicationJob).filter_by(post_id=post.id).all(); jobs[0].absolute_time=True; old=[x.scheduled_at for x in jobs]; new=game.kickoff+timedelta(days=1); reschedule_game(db,game,new)
 assert jobs[0].scheduled_at==old[0] and jobs[0].stale_time
