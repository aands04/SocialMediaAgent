from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import (
    AuditLog,
    InstagramPage,
    JobStatus,
    Post,
    PostStatus,
    PublicationJob,
    Role,
    Team,
    User,
)
from app.publishing.schedule import PublicationScheduleError, reschedule_publication_job


def _graph(db, *, role=Role.APPROVER):
    page = InstagramPage(
        internal_name="schedule-page",
        display_name="Schedule Page",
        username="schedule_page",
        club="Testverein",
        active=True,
    )
    db.add(page)
    db.flush()
    team = Team(
        internal_name="schedule-team",
        display_name="Testverein I",
        short_name="TV",
        slug="schedule-team",
        club="Testverein",
        fussball_url="https://example.invalid/schedule-team",
        instagram_page_id=page.id,
        media_subdir="schedule-team/players",
        timezone="Europe/Berlin",
    )
    user = User(
        email=f"schedule-{role.value}@example.invalid",
        password_hash="not-used",
        role=role,
        all_teams=True,
        active=True,
    )
    db.add_all([team, user])
    db.flush()
    post = Post(
        team_id=team.id,
        instagram_page_id=page.id,
        post_type="announcement",
        status=PostStatus.APPROVED,
        text="Freigegebener Text",
        approved_version=1,
        approved_by=user.id,
        approved_at=datetime.now(timezone.utc),
    )
    db.add(post)
    db.flush()
    jobs = []
    for kind, hour in (("feed", 2), ("story", 3)):
        job = PublicationJob(
            post_id=post.id,
            team_id=team.id,
            instagram_page_id=page.id,
            kind=kind,
            media_path=f"/tmp/{kind}.png",
            scheduled_at=datetime.now(timezone.utc) + timedelta(hours=hour),
            status=JobStatus.SCHEDULED,
            approval_status="approved",
            approved_post_version=post.version,
            idempotency_key=f"schedule-{kind}",
        )
        db.add(job)
        jobs.append(job)
    db.commit()
    return team, user, post, jobs


def test_manual_schedule_change_is_absolute_audited_and_revokes_approval(db):
    _team, user, post, jobs = _graph(db)
    target = jobs[1]
    old_time = target.scheduled_at
    new_time = datetime.now(timezone.utc) + timedelta(days=2, hours=1)

    result = reschedule_publication_job(
        db,
        post=post,
        job=target,
        user=user,
        scheduled_at=new_time,
        expected_job_version=target.version,
    )

    assert result.old_scheduled_at == old_time
    assert result.new_scheduled_at == new_time
    assert result.approval_invalidated is True
    assert target.scheduled_at == new_time
    assert target.absolute_time is True
    assert target.stale_time is False
    assert post.status == PostStatus.REAPPROVAL
    assert post.approved_version is None
    assert all(job.status == JobStatus.UNAPPROVED for job in jobs)
    assert all(job.approval_status == "reapproval_required" for job in jobs)
    audit = db.scalar(
        select(AuditLog).where(AuditLog.action == "publication.schedule_changed")
    )
    assert audit and audit.entity_id == target.id
    assert audit.details["old_scheduled_at"] == old_time.isoformat()
    assert audit.details["new_scheduled_at"] == new_time.isoformat()
    assert audit.details["approval_invalidated"] is True


def test_schedule_change_rejects_stale_version_and_published_job(db):
    _team, user, post, jobs = _graph(db)
    target = jobs[0]
    new_time = datetime.now(timezone.utc) + timedelta(days=1)

    with pytest.raises(PublicationScheduleError, match="Bearbeitungskonflikt"):
        reschedule_publication_job(
            db,
            post=post,
            job=target,
            user=user,
            scheduled_at=new_time,
            expected_job_version=target.version + 1,
        )

    target.status = JobStatus.PUBLISHED
    target.platform_id = "instagram-media-id"
    db.commit()
    with pytest.raises(PublicationScheduleError, match="aktuellen Status"):
        reschedule_publication_job(
            db,
            post=post,
            job=target,
            user=user,
            scheduled_at=new_time,
            expected_job_version=target.version,
        )


def test_viewer_cannot_change_publication_schedule(db):
    _team, user, post, jobs = _graph(db, role=Role.VIEWER)

    with pytest.raises(PublicationScheduleError, match="Keine Berechtigung"):
        reschedule_publication_job(
            db,
            post=post,
            job=jobs[0],
            user=user,
            scheduled_at=datetime.now(timezone.utc) + timedelta(days=1),
            expected_job_version=jobs[0].version,
        )


def test_schedule_change_rejects_active_sibling_job(db):
    _team, user, post, jobs = _graph(db)
    jobs[1].status = JobStatus.PUBLISHING
    jobs[1].locked_at = datetime.now(timezone.utc)
    db.commit()

    with pytest.raises(PublicationScheduleError, match="anderer Auftrag"):
        reschedule_publication_job(
            db,
            post=post,
            job=jobs[0],
            user=user,
            scheduled_at=datetime.now(timezone.utc) + timedelta(days=1),
            expected_job_version=jobs[0].version,
        )
