from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext

from app.approvals.service import ApprovalError, approve
from app.channels.service import InstagramAssignmentConflict, instagram_page_for_team
from app.config import Settings
from app.jobs import generation
from app.models import (
    AuditLog,
    ContentRuleSet,
    Game,
    GeneratedMediaSlot,
    GenerationJobStatus,
    InstagramPage,
    Post,
    PostStatus,
    PublicationJob,
    PublicationRuleSlot,
    Role,
    SocialChannelConnection,
    Team,
    TeamChannelAssignment,
    User,
)
from app.posts.service import create_post
from app.rendering.service import Renderer
from app.textgen.service import FixtureTextGenerator


def _channel_less_graph(db):
    team = Team(
        internal_name="channel-less",
        display_name="SV Kanalfrei",
        short_name="SVK",
        slug="channel-less",
        club="SV Kanalfrei",
        fussball_url="https://www.fussball.de/channel-less",
        instagram_page_id=None,
        media_subdir="channel-less",
        rules={"feed_before_minutes": 60},
    )
    db.add(team)
    db.flush()
    game = Game(
        team_id=team.id,
        provider="fussball.de",
        external_id="channel-less-game",
        home_team=team.display_name,
        away_team="FC Beispiel",
        kickoff=datetime.now(timezone.utc) + timedelta(days=2),
        source_url=team.fussball_url,
    )
    user = User(
        email="channel-less@test.invalid",
        password_hash="x",
        role=Role.ADMIN,
        all_teams=True,
    )
    db.add_all([game, user])
    db.commit()
    return team, game, user


def _verified_logo_snapshot() -> dict:
    return {
        "team": {
            "id": "verified-logo",
            "version": 1,
            "checksum": "0" * 64,
            "verified": True,
        },
        "opponent": {
            "fallback": True,
            "verified": False,
            "disabled": False,
        },
    }


def _assigned_instagram_page(db, team, *, suffix="assigned", enabled=True):
    page = InstagramPage(
        internal_name=f"instagram-{suffix}",
        display_name=f"Instagram {suffix}",
        username=f"instagram_{suffix}",
        club=team.club,
        active=True,
        connection_status="connected",
        publishing_enabled=True,
    )
    db.add(page)
    db.flush()
    connection = SocialChannelConnection(
        channel_type="instagram",
        internal_name=f"instagram-{suffix}",
        display_name=f"Instagram {suffix}",
        username=f"instagram_{suffix}",
        legacy_instagram_page_id=page.id,
        status="connected",
        active=True,
        publishing_enabled=True,
    )
    db.add(connection)
    db.flush()
    assignment = TeamChannelAssignment(
        team_id=team.id,
        channel_connection_id=connection.id,
        enabled=enabled,
        announcement_enabled=enabled,
        result_enabled=enabled,
        story_enabled=enabled,
    )
    db.add(assignment)
    db.flush()
    return page, connection, assignment


def test_generated_post_does_not_require_a_social_media_channel(db, tmp_path) -> None:
    team, game, _user = _channel_less_graph(db)

    post = create_post(
        db,
        game,
        team,
        FixtureTextGenerator(),
        Renderer(tmp_path / "generated"),
        logo_snapshot=_verified_logo_snapshot(),
    )

    jobs = db.query(PublicationJob).filter_by(post_id=post.id).all()
    assert post.instagram_page_id is None
    assert post.status == PostStatus.INCOMPLETE
    assert all(
        "Instagram" not in warning and "Kanal" not in warning for warning in post.critical_warnings
    )
    assert post.text
    assert post.feed_path and Path(post.feed_path).is_file()
    assert jobs
    assert all(job.instagram_page_id is None for job in jobs)
    assert db.query(GeneratedMediaSlot).filter_by(post_id=post.id).count() == 1
    assert db.query(SocialChannelConnection).count() == 0


def test_generated_post_uses_authoritative_instagram_team_assignment(db, tmp_path) -> None:
    team, game, user = _channel_less_graph(db)
    page, _connection, _assignment = _assigned_instagram_page(db, team)
    db.commit()

    post = create_post(
        db,
        game,
        team,
        FixtureTextGenerator(),
        Renderer(tmp_path / "generated"),
        logo_snapshot=_verified_logo_snapshot(),
    )
    post.status = PostStatus.PENDING
    post.critical_warnings = []
    db.commit()

    assert team.instagram_page_id is None
    assert post.instagram_page_id == page.id
    assert {
        job.instagram_page_id for job in db.query(PublicationJob).filter_by(post_id=post.id).all()
    } == {page.id}
    approve(db, post, user)
    assert post.status == PostStatus.APPROVED


def test_disabled_assignment_prevents_legacy_instagram_fallback(db) -> None:
    team, _game, _user = _channel_less_graph(db)
    page, _connection, _assignment = _assigned_instagram_page(db, team, enabled=False)
    team.instagram_page_id = page.id
    db.commit()

    resolved = instagram_page_for_team(db, team)

    assert resolved is None
    assert not db.new
    assert not db.dirty
    assert not db.deleted


def test_legacy_instagram_page_remains_fallback_without_assignment_rows(db) -> None:
    team, _game, _user = _channel_less_graph(db)
    page = InstagramPage(
        internal_name="legacy-instagram",
        display_name="Legacy Instagram",
        username="legacy_instagram",
        club=team.club,
        active=True,
        connection_status="connected",
    )
    db.add(page)
    db.flush()
    team.instagram_page_id = page.id
    db.commit()

    assert instagram_page_for_team(db, team) is page
    assert not db.new
    assert not db.dirty
    assert not db.deleted


def test_multiple_enabled_instagram_assignments_fail_closed(db) -> None:
    team, _game, _user = _channel_less_graph(db)
    _assigned_instagram_page(db, team, suffix="first")
    _assigned_instagram_page(db, team, suffix="second")
    db.commit()

    with pytest.raises(InstagramAssignmentConflict, match="mehrere aktive Instagram-Kanäle"):
        instagram_page_for_team(db, team)


def test_structured_generation_rules_work_without_a_channel(db, tmp_path) -> None:
    team, game, _user = _channel_less_graph(db)
    rule_set = ContentRuleSet(
        scope_type="team",
        scope_key=f"team:{team.id}",
        team_id=team.id,
        post_type="announcement",
        feed_generation_count=1,
        story_generation_count=2,
        feed_publish_variants=[1],
        story_publish_variants=[1],
    )
    db.add(rule_set)
    db.flush()
    db.add_all(
        [
            PublicationRuleSlot(
                rule_set_id=rule_set.id,
                slot_key="feed-before-kickoff",
                label="Feed",
                media_kind="feed",
                variant_number=1,
                timing_model="relative",
                reference="kickoff",
                direction="before",
                offset_minutes=60,
            ),
            PublicationRuleSlot(
                rule_set_id=rule_set.id,
                slot_key="story-before-kickoff",
                label="Story",
                media_kind="story",
                variant_number=1,
                timing_model="relative",
                reference="kickoff",
                direction="before",
                offset_minutes=30,
            ),
        ]
    )
    db.commit()

    post = create_post(
        db,
        game,
        team,
        FixtureTextGenerator(),
        Renderer(tmp_path / "generated"),
        logo_snapshot=_verified_logo_snapshot(),
    )

    jobs = db.query(PublicationJob).filter_by(post_id=post.id).all()
    slots = db.query(GeneratedMediaSlot).filter_by(post_id=post.id).all()
    assert len(jobs) == 2
    assert len(slots) == 3
    assert all(job.instagram_page_id is None for job in jobs)
    assert {slot.media_kind for slot in slots} == {"feed", "story"}


def test_channel_less_post_remains_blocked_from_approval(db, tmp_path) -> None:
    team, game, user = _channel_less_graph(db)
    post = create_post(
        db,
        game,
        team,
        FixtureTextGenerator(),
        Renderer(tmp_path / "generated"),
        logo_snapshot=_verified_logo_snapshot(),
    )
    post.critical_warnings = []
    db.commit()

    with pytest.raises(ApprovalError, match="Instagram-Seite nicht aktiv verbunden"):
        approve(db, post, user)

    assert post.status != PostStatus.APPROVED


def test_automatic_generation_succeeds_while_channel_less_auto_approval_is_blocked(
    db, monkeypatch, tmp_path
) -> None:
    team, game, user = _channel_less_graph(db)
    job, _post = generation.enqueue_create(db, game, team, user, "announcement")
    job.parameters = {
        **(job.parameters or {}),
        "trigger_mode": "automatic_fussball",
        "automatic_approval_requested": True,
    }
    db.commit()
    claimed = generation.claim_next(db, "channel-less-worker")
    monkeypatch.setattr(
        generation,
        "build_renderer",
        lambda settings: Renderer(settings.generated_root),
    )
    monkeypatch.setattr(
        generation,
        "build_text_generator",
        lambda settings: FixtureTextGenerator(),
    )

    result = generation.process_generation_job(
        db,
        claimed,
        Settings(
            generated_root=tmp_path / "generated",
            media_root=tmp_path / "media",
            upload_root=tmp_path / "uploads",
        ),
    )

    post = db.get(Post, result.result_post_id)
    assert result.status == GenerationJobStatus.SUCCEEDED
    assert post is not None and post.instagram_page_id is None
    assert db.scalar(
        sa.select(AuditLog).where(AuditLog.action == "post.automatic_approval_blocked")
    )


def _load_migration():
    path = Path("alembic/versions/0035_optional_post_instagram.py")
    spec = importlib.util.spec_from_file_location("optional_post_instagram_0035", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_optional_post_instagram_migration_is_reversible() -> None:
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")

    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE posts ("
            "id VARCHAR(36) PRIMARY KEY, "
            "instagram_page_id VARCHAR(36) NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO posts (id, instagram_page_id) VALUES ('post-1', 'page-1')"
        )
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()

        columns = {item["name"]: item for item in sa.inspect(connection).get_columns("posts")}
        assert columns["instagram_page_id"]["nullable"] is True

        with Operations.context(context):
            migration.downgrade()

        columns = {item["name"]: item for item in sa.inspect(connection).get_columns("posts")}
        assert columns["instagram_page_id"]["nullable"] is False


def test_optional_post_instagram_downgrade_blocks_null_rows() -> None:
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")

    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE posts ("
            "id VARCHAR(36) PRIMARY KEY, "
            "instagram_page_id VARCHAR(36) NOT NULL)"
        )
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
        connection.exec_driver_sql(
            "INSERT INTO posts (id, instagram_page_id) VALUES ('post-1', NULL)"
        )

        with pytest.raises(RuntimeError, match="Downgrade blockiert"):
            with Operations.context(context):
                migration.downgrade()
