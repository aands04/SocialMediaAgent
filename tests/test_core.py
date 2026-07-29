from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.approvals.service import ApprovalError, approve, edit_text
from app.auth.service import allowed, hash_password
from app.config import Settings
from app.games.provider import FussballDeProvider, ProviderError
from app.media.storage import LocalStorageProvider, StorageError
from app.models import (
 DesignTemplate,
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
from app.posts.service import (
 RerenderConflict,
 create_post,
 rerender_post,
 reschedule_game,
 reserve_image,
 story_time,
)
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

def test_latest_design_template_version_is_frozen_on_post(db,tmp_path):
 _,team,game=graph(db,tmp_path)
 from app.rendering.service import BASE_CSS, BUILTIN_HTML
 db.add_all([
  DesignTemplate(name="default-feed",post_type="announcement",media_kind="feed",width=1080,height=1350,html_template=BUILTIN_HTML,css=BASE_CSS,version=1),
  DesignTemplate(name="default-feed",post_type="announcement",media_kind="feed",width=1080,height=1350,html_template=BUILTIN_HTML,css=BASE_CSS+".canvas{outline:1px solid white}",version=2),
 ]); db.commit()
 post=create_post(db,game,team,FixtureTextGenerator(),Renderer(tmp_path/"out"))
 assert post.design_snapshot["feed"]["version"]==2
 assert post.design_snapshot["feed"]["builtin"] is False

def test_story_template_snapshots_persist_after_session_reload(db,tmp_path):
 _,team,game=graph(db,tmp_path)
 from sqlalchemy.orm import Session

 from app.rendering.service import BASE_CSS, BUILTIN_HTML
 db.add_all([
  DesignTemplate(name="story-a",post_type="announcement",media_kind="story",width=1080,height=1920,html_template=BUILTIN_HTML,css=BASE_CSS,version=3),
  DesignTemplate(name="story-b",post_type="announcement",media_kind="story",width=1080,height=1920,html_template=BUILTIN_HTML,css=BASE_CSS,version=7),
  StoryRule(team_id=team.id,name="A",post_type="announcement",reference="kickoff",direction="before",offset_minutes=120,template="story-a"),
  StoryRule(team_id=team.id,name="B",post_type="announcement",reference="kickoff",direction="before",offset_minutes=60,template="story-b"),
 ]); db.commit()
 post=create_post(db,game,team,FixtureTextGenerator(),Renderer(tmp_path/"out")); post_id=post.id; db.close()
 with Session(db.bind) as reloaded:
  snapshots=reloaded.get(__import__('app.models',fromlist=['Post']).Post,post_id).design_snapshot["stories"]
  assert sorted(entry["template"]["version"] for entry in snapshots)==[3,7]

def test_controlled_rerender_versions_files_and_revokes_approval(db,tmp_path):
 _,team,game=graph(db,tmp_path); rule=StoryRule(team_id=team.id,name="S",post_type="announcement",reference="kickoff",direction="before",offset_minutes=60,template="default-story"); db.add(rule); db.commit()
 post=create_post(db,game,team,FixtureTextGenerator(),Renderer(tmp_path/"out")); jobs=db.query(PublicationJob).filter_by(post_id=post.id).all(); story=next(job for job in jobs if job.kind=="story")
 post.status=PostStatus.APPROVED; post.approved_version=post.version
 for job in jobs: job.status=JobStatus.SCHEDULED; job.approval_status="approved"; job.approved_post_version=post.version
 old_feed=Path(post.feed_path); old_story=Path(story.media_path); db.commit()
 rerender_post(db,post,Renderer(tmp_path/"out"),[story.id]); db.commit()
 assert old_feed.is_file() and old_story.is_file() and Path(post.feed_path)!=old_feed and Path(story.media_path)!=old_story
 assert post.status==PostStatus.REAPPROVAL and all(job.status==JobStatus.UNAPPROVED for job in jobs)

def immutable_job_values(job):
 return (job.media_path,job.version,job.idempotency_key,job.status,job.approval_status,job.approved_post_version,job.error,job.platform_id,job.published_at)

def test_rerender_rejects_published_feed_without_files_or_changes(db,tmp_path):
 _,team,game=graph(db,tmp_path); rule=StoryRule(team_id=team.id,name="S",post_type="announcement",reference="kickoff",direction="before",offset_minutes=60,template="default-story"); db.add(rule); db.commit()
 post=create_post(db,game,team,FixtureTextGenerator(),Renderer(tmp_path/"out")); jobs=db.query(PublicationJob).filter_by(post_id=post.id).all(); feed=next(job for job in jobs if job.kind=="feed")
 feed.status=JobStatus.PUBLISHED; feed.approval_status="approved"; feed.platform_id="feed-platform"; feed.published_at=datetime.now(timezone.utc); db.commit()
 before=immutable_job_values(feed); files=set((tmp_path/"out").rglob("*.png")); post_version=post.version; feed_version=post.feed_version
 with pytest.raises(RerenderConflict,match="Feed wurde bereits veröffentlicht"): rerender_post(db,post,Renderer(tmp_path/"out"),[next(job.id for job in jobs if job.kind=="story")])
 assert set((tmp_path/"out").rglob("*.png"))==files and immutable_job_values(feed)==before
 assert post.version==post_version and post.feed_version==feed_version

def test_rerender_rejects_selected_published_story_before_feed_write(db,tmp_path):
 _,team,game=graph(db,tmp_path); rule=StoryRule(team_id=team.id,name="S",post_type="announcement",reference="kickoff",direction="before",offset_minutes=60,template="default-story"); db.add(rule); db.commit()
 post=create_post(db,game,team,FixtureTextGenerator(),Renderer(tmp_path/"out")); story=db.query(PublicationJob).filter_by(post_id=post.id,kind="story").one()
 story.status=JobStatus.PUBLISHED; story.approval_status="approved"; story.platform_id="story-platform"; story.published_at=datetime.now(timezone.utc); db.commit()
 before=immutable_job_values(story); files=set((tmp_path/"out").rglob("*.png")); post_values=(post.version,post.feed_version,post.feed_path)
 with pytest.raises(RerenderConflict,match="ausgewählte Story wurde bereits veröffentlicht"): rerender_post(db,post,Renderer(tmp_path/"out"),[story.id])
 assert set((tmp_path/"out").rglob("*.png"))==files and immutable_job_values(story)==before
 assert (post.version,post.feed_version,post.feed_path)==post_values

def test_partial_post_rerenders_only_open_outputs_and_preserves_published_story(db,tmp_path):
 _,team,game=graph(db,tmp_path); rules=[StoryRule(team_id=team.id,name=name,post_type="announcement",reference="kickoff",direction="before",offset_minutes=offset,template="default-story") for name,offset in (("A",120),("B",60))]; db.add_all(rules); db.commit()
 post=create_post(db,game,team,FixtureTextGenerator(),Renderer(tmp_path/"out")); jobs=db.query(PublicationJob).filter_by(post_id=post.id).all(); feed=next(job for job in jobs if job.kind=="feed"); stories=[job for job in jobs if job.kind=="story"]
 published,selected=stories; post.status=PostStatus.PARTIAL; post.approved_version=post.version
 for job in jobs: job.status=JobStatus.SCHEDULED; job.approval_status="approved"; job.approved_post_version=post.version
 published.status=JobStatus.PUBLISHED; published.platform_id="immutable-story"; published.published_at=datetime.now(timezone.utc); db.commit()
 published_before=immutable_job_values(published); old_feed=feed.media_path; old_selected=selected.media_path
 rerender_post(db,post,Renderer(tmp_path/"out"),[selected.id]); db.commit()
 assert immutable_job_values(published)==published_before
 assert feed.media_path!=old_feed and selected.media_path!=old_selected
 assert post.status==PostStatus.REAPPROVAL and feed.status==selected.status==JobStatus.UNAPPROVED
