from datetime import datetime, timedelta, timezone

import pytest

from app.approvals.service import ApprovalError, approve, edit_text
from app.config import Settings
from app.games.live_test import LiveTestDisabled, capture
from app.models import (
    Game,
    InstagramPage,
    JobStatus,
    MediaAsset,
    PostStatus,
    PublicationJob,
    Role,
    StoryRule,
    SystemSetting,
    Team,
    User,
)
from app.posts.service import create_post, reserve_image
from app.publishing.service import DryRunPublisher, PublishError
from app.publishing.worker import process_job
from app.rendering.service import Renderer
from app.textgen.service import FixtureTextGenerator


def setup(db,tmp_path,late="publish_now"):
    page=InstagramPage(internal_name="p",display_name="P",username="p",club="C",active=True,connection_status="connected",publishing_enabled=True,account_id="mock")
    db.add(page); db.flush(); team=Team(internal_name="t",display_name="T",short_name="T",slug=f"t-{late}",club="C",fussball_url="https://www.fussball.de/test",instagram_page_id=page.id,media_subdir="t",rules={"late_approval":late},publishing_enabled=True)
    db.add(team); db.flush(); game=Game(team_id=team.id,external_id="g",home_team="T",away_team="G",kickoff=datetime.now(timezone.utc)+timedelta(hours=2),source_url=team.fussball_url); db.add(game); db.commit(); return page,team,game

def approver(db):
    user=User(email=f"a{len(db.query(User).all())}@x",password_hash="x",role=Role.APPROVER,all_teams=True); db.add(user); db.commit(); return user

def mark_verified_logo(post):
    snapshot=dict(post.design_snapshot or {})
    snapshot["logos"]={"team":{"id":"verified-test-logo","version":1,"checksum":"0"*64,"verified":True}}
    post.design_snapshot=snapshot
    return post

def test_late_approval_modes(db,tmp_path):
    _,team,game=setup(db,tmp_path,"manual"); post=mark_verified_logo(create_post(db,game,team,FixtureTextGenerator(),Renderer(tmp_path/"o"))); post.critical_warnings=[]; job=db.query(PublicationJob).filter_by(post_id=post.id).one(); job.scheduled_at=datetime.now(timezone.utc)-timedelta(minutes=1); db.commit()
    with pytest.raises(ApprovalError,match="verstrichen"): approve(db,post,approver(db))
    team.rules={"late_approval":"skip"}; db.commit(); approve(db,post,approver(db)); assert job.status==JobStatus.SKIPPED

def test_change_after_approval_requires_reapproval(db,tmp_path):
    _,team,game=setup(db,tmp_path); post=mark_verified_logo(create_post(db,game,team,FixtureTextGenerator(),Renderer(tmp_path/"o"))); post.critical_warnings=[]; user=approver(db); approve(db,post,user); old=post.version
    editor=User(email="e@x",password_hash="x",role=Role.EDITOR,all_teams=True); db.add(editor); db.commit(); edit_text(db,post,editor,"Geändert",old)
    assert post.status==PostStatus.REAPPROVAL and all(j.status==JobStatus.UNAPPROVED for j in db.query(PublicationJob).filter_by(post_id=post.id))

def test_partial_and_duplicate_job_execution(db,tmp_path):
    _,team,game=setup(db,tmp_path); db.add(StoryRule(team_id=team.id,name="s",post_type="announcement",reference="kickoff",direction="before",offset_minutes=30,template="s")); db.commit(); post=mark_verified_logo(create_post(db,game,team,FixtureTextGenerator(),Renderer(tmp_path/"o"))); post.critical_warnings=[]; approve(db,post,approver(db)); jobs=db.query(PublicationJob).filter_by(post_id=post.id).all()
    for job in jobs: job.scheduled_at=datetime.now(timezone.utc)-timedelta(seconds=1)
    db.commit(); first=process_job(db,jobs[0].id,DryRunPublisher(),Settings(global_publish_enabled=True)); attempts=first.attempts; process_job(db,jobs[0].id,DryRunPublisher(),Settings(global_publish_enabled=True))
    assert first.attempts==attempts and post.status==PostStatus.PARTIAL
    process_job(db,jobs[1].id,DryRunPublisher(),Settings(global_publish_enabled=True)); assert post.status==PostStatus.PUBLISHED

def test_global_emergency_stop(db,tmp_path):
    _,team,game=setup(db,tmp_path); post=mark_verified_logo(create_post(db,game,team,FixtureTextGenerator(),Renderer(tmp_path/"o"))); post.critical_warnings=[]; approve(db,post,approver(db)); job=db.query(PublicationJob).filter_by(post_id=post.id).one(); job.scheduled_at=datetime.now(timezone.utc)-timedelta(seconds=1); db.add(SystemSetting(key="emergency_stop",value={"enabled":True})); db.commit()
    with pytest.raises(PublishError,match="Not-Aus"): process_job(db,job.id,DryRunPublisher(),Settings(global_publish_enabled=True))
    assert job.attempts==0

def test_image_reservation_is_idempotent_and_exclusive(db,tmp_path):
    _,team,game=setup(db,tmp_path); other=Game(team_id=team.id,external_id="g2",home_team="T",away_team="X",kickoff=game.kickoff+timedelta(days=1),source_url=team.fussball_url); db.add(other); db.flush(); asset=MediaAsset(team_id=team.id,relative_path="p.jpg",filename="p.jpg",mime_type="image/jpeg",size=1,checksum="c",mtime=datetime.now(timezone.utc)); db.add(asset); db.commit()
    assert reserve_image(db,team.id,game.id).id==asset.id
    assert reserve_image(db,team.id,game.id).id==asset.id
    assert reserve_image(db,team.id,other.id) is None and asset.uses==1

def test_live_mode_disabled_without_http(db,tmp_path):
    _,team,_=setup(db,tmp_path)
    with pytest.raises(LiveTestDisabled): capture(db,team,Settings(fussball_live_test_enabled=False,provider_snapshot_root=tmp_path))
