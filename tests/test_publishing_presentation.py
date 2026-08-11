from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import (
    Club,
    InstagramPage,
    JobStatus,
    Post,
    PostStatus,
    PublicationJob,
    SocialChannelConnection,
    Team,
)
from app.publishing.presentation import operational_channels, publication_views


def _publishing_records(db):
    club = db.scalar(select(Club))
    page = InstagramPage(
        internal_name="presentation-instagram",
        display_name="Vereinsprofil",
        username="vereinsprofil",
        account_id="ig-presentation",
        club=club.name,
        active=True,
        connection_status="connected",
        publishing_enabled=True,
        automatic_publishing_enabled=True,
    )
    db.add(page)
    db.flush()
    team = Team(
        internal_name="presentation-team",
        display_name="Erste Mannschaft",
        short_name="Erste",
        slug="presentation-team",
        club=club.name,
        fussball_url="https://example.invalid/team",
        instagram_page_id=page.id,
        media_subdir="presentation/players",
    )
    db.add(team)
    db.flush()
    post = Post(
        team_id=team.id,
        instagram_page_id=page.id,
        post_type="announcement",
        status=PostStatus.APPROVED,
        text="Am Sonntag steht das nächste Heimspiel an.",
    )
    facebook = SocialChannelConnection(
        channel_type="facebook",
        internal_name="presentation-facebook",
        display_name="Offizielle Vereinsseite",
        external_account_id="facebook-page-123",
        status="connected",
        capabilities=["page_post", "image_post"],
        active=True,
        publishing_enabled=True,
        automatic_delivery_enabled=True,
    )
    unfinished_whatsapp = SocialChannelConnection(
        channel_type="whatsapp",
        internal_name="unfinished-whatsapp",
        display_name="Noch nicht eingerichtet",
        status="setup_required",
        capabilities=[],
        active=False,
        publishing_enabled=False,
    )
    db.add_all([post, facebook, unfinished_whatsapp])
    db.flush()
    job = PublicationJob(
        post_id=post.id,
        team_id=team.id,
        instagram_page_id=page.id,
        channel_type="facebook",
        channel_connection_id=facebook.id,
        content_type="page_post",
        delivery_action="publish",
        kind="feed",
        media_path="/tmp/facebook-feed.png",
        scheduled_at=datetime.now(timezone.utc) + timedelta(days=1),
        approval_status="approved",
        status=JobStatus.SCHEDULED,
        idempotency_key="presentation-facebook-job",
    )
    db.add(job)
    db.commit()
    return club, team, post, facebook, unfinished_whatsapp, job


def test_presentation_uses_concrete_configured_target_and_german_status(db):
    club, team, post, facebook, unfinished_whatsapp, job = _publishing_records(db)

    channels = operational_channels(db, club.id)
    by_type = {channel.channel_type: channel for channel in channels}

    assert by_type["facebook"].concrete_target == "Offizielle Vereinsseite"
    assert "whatsapp" not in by_type

    views = publication_views(db, [job], club_id=club.id, channels=channels)
    assert len(views) == 1
    view = views[0]
    assert view.channel.channel_type == "facebook"
    assert view.target == "Offizielle Vereinsseite"
    assert view.content_label == "Facebook-Beitrag"
    assert view.status_label == "Geplant"
    assert view.approval_label == "Freigegeben"
    assert view.contribution_label == "Spielankündigung"
    assert view.team.id == team.id
    assert "nächste Heimspiel" in view.subtitle


def test_presentation_denies_missing_or_mismatched_tenant_context(db):
    club, _team, _post, _facebook, _unfinished_whatsapp, job = _publishing_records(db)

    with pytest.raises(ValueError, match="Vereinskontext"):
        publication_views(db, [job], club_id="")

    assert publication_views(db, [job], club_id="fremder-verein", channels=[]) == []
    with pytest.raises(ValueError, match="Vereinskontext"):
        operational_channels(db, "")
