from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import Settings
from app.jobs import generation
from app.models import (
    AuditLog,
    Game,
    GenerationJobStatus,
    InstagramPage,
    JobStatus,
    Post,
    PostStatus,
    PublicationJob,
    Role,
    Team,
    User,
)
from app.posts.deletion import PostDeletionConflict, delete_unpublished_post
from app.posts.service import revise_post
from app.textgen.service import FixtureTextGenerator


def graph(db):
    page = InstagramPage(
        internal_name="post-management",
        display_name="Post Management",
        username="post-management",
        club="SV",
        active=True,
    )
    db.add(page)
    db.flush()
    team = Team(
        internal_name="post-management",
        display_name="SV Test",
        short_name="SVT",
        slug="post-management",
        club="SV Test",
        fussball_url="https://www.fussball.de/test",
        instagram_page_id=page.id,
        media_subdir="test",
    )
    db.add(team)
    db.flush()
    game = Game(
        team_id=team.id,
        provider="mock",
        external_id="post-management",
        home_team="SV Test",
        away_team="FC Beispiel",
        kickoff=datetime.now(timezone.utc) + timedelta(days=2),
        competition="Testliga",
        venue="Teststadion",
        pitch="Rasenplatz",
        source_url="fixture://post-management",
    )
    user = User(
        email="post-management@test.invalid",
        password_hash="x",
        role=Role.ADMIN,
        all_teams=True,
    )
    db.add_all([game, user])
    db.commit()
    return page, team, game, user


def post_with_feed(db, page, team, game, media_path, status=PostStatus.PENDING):
    post = Post(
        game_id=game.id,
        team_id=team.id,
        instagram_page_id=page.id,
        post_type="announcement",
        status=status,
        text="Alter Begleittext",
        feed_path=str(media_path),
    )
    db.add(post)
    db.flush()
    publication = PublicationJob(
        post_id=post.id,
        game_id=game.id,
        team_id=team.id,
        instagram_page_id=page.id,
        kind="feed",
        media_path=str(media_path),
        text_snapshot=post.text,
        scheduled_at=game.kickoff - timedelta(days=1),
        idempotency_key=f"{post.id}:feed:v1",
    )
    db.add(publication)
    db.commit()
    return post, publication


def test_unpublished_post_is_deleted_with_files_jobs_and_audit(db, tmp_path):
    page, team, game, user = graph(db)
    generated = tmp_path / "generated"
    upload = tmp_path / "uploads"
    media = generated / "post" / "feed.png"
    media.parent.mkdir(parents=True)
    upload.mkdir()
    media.write_bytes(b"png-placeholder")
    post, publication = post_with_feed(db, page, team, game, media)

    result = delete_unpublished_post(
        db,
        Settings(generated_root=generated, upload_root=upload),
        post,
        user,
        expected_version=post.version,
        reason="Fehlentwurf",
    )

    assert db.get(Post, post.id) is None
    assert db.get(PublicationJob, publication.id) is None
    assert not media.exists()
    assert result.removed_files == 1
    audit = db.scalar(select(AuditLog).where(AuditLog.action == "post.deleted"))
    assert audit and audit.entity_id == post.id
    assert audit.details["reason"] == "Fehlentwurf"


def test_deleting_bundle_removes_every_unpublished_member_and_file(db, tmp_path):
    page, first_team, first_game, user = graph(db)
    generated = tmp_path / "generated"
    upload = tmp_path / "uploads"
    generated.mkdir()
    upload.mkdir()
    second_team = Team(
        internal_name="post-management-two",
        display_name="SV Test II",
        short_name="SVT II",
        slug="post-management-two",
        club=first_team.club,
        fussball_url="https://www.fussball.de/test-two",
        instagram_page_id=page.id,
        media_subdir="test-two",
    )
    db.add(second_team)
    db.flush()
    second_game = Game(
        team_id=second_team.id,
        provider="mock",
        external_id="post-management-two",
        home_team="SV Test II",
        away_team="FC Beispiel II",
        kickoff=first_game.kickoff + timedelta(hours=2),
        competition="Testliga",
        venue="Teststadion",
        pitch="Rasenplatz",
        source_url="fixture://post-management-two",
    )
    db.add(second_game)
    db.flush()
    first_media = generated / "first.png"
    second_media = generated / "second.png"
    first_media.write_bytes(b"first")
    second_media.write_bytes(b"second")
    primary, first_publication = post_with_feed(db, page, first_team, first_game, first_media)
    member, second_publication = post_with_feed(db, page, second_team, second_game, second_media)
    member_ids = [primary.id, member.id]
    for item, role in ((primary, "primary"), (member, "member")):
        item.design_snapshot = {
            "club_matchday_carousel": {
                "primary_post_id": primary.id,
                "member_post_ids": member_ids,
                "role": role,
            }
        }
    db.commit()

    result = delete_unpublished_post(
        db,
        Settings(generated_root=generated, upload_root=upload),
        primary,
        user,
        expected_version=primary.version,
    )

    assert result.posts == 2
    assert result.publication_jobs == 2
    assert db.get(Post, primary.id) is None
    assert db.get(Post, member.id) is None
    assert db.get(PublicationJob, first_publication.id) is None
    assert db.get(PublicationJob, second_publication.id) is None
    assert not first_media.exists()
    assert not second_media.exists()
    assert db.query(AuditLog).filter_by(action="post.deleted").count() == 2


def test_deleting_incomplete_legacy_bundle_removes_surviving_post(db, tmp_path):
    page, team, game, user = graph(db)
    generated = tmp_path / "generated"
    upload = tmp_path / "uploads"
    generated.mkdir()
    upload.mkdir()
    media = generated / "legacy.png"
    media.write_bytes(b"legacy")
    post, publication = post_with_feed(db, page, team, game, media)
    missing_post_id = "00000000-0000-0000-0000-000000000099"
    post.design_snapshot = {
        "club_matchday_carousel": {
            "primary_post_id": post.id,
            "member_post_ids": [post.id, missing_post_id],
            "role": "primary",
        }
    }
    db.commit()

    result = delete_unpublished_post(
        db,
        Settings(generated_root=generated, upload_root=upload),
        post,
        user,
        expected_version=post.version,
    )

    assert result.posts == 1
    assert db.get(Post, post.id) is None
    assert db.get(PublicationJob, publication.id) is None
    assert not media.exists()


def test_published_post_cannot_be_deleted(db, tmp_path):
    page, team, game, user = graph(db)
    generated = tmp_path / "generated"
    upload = tmp_path / "uploads"
    generated.mkdir()
    upload.mkdir()
    media = generated / "published.png"
    media.write_bytes(b"published")
    post, publication = post_with_feed(db, page, team, game, media, status=PostStatus.PUBLISHED)
    publication.status = JobStatus.PUBLISHED
    publication.platform_id = "instagram-1"
    db.commit()

    with pytest.raises(PostDeletionConflict, match="veröffentlichte Beiträge"):
        delete_unpublished_post(
            db,
            Settings(generated_root=generated, upload_root=upload),
            post,
            user,
            expected_version=post.version,
            reason="darf nicht",
        )
    assert media.exists()
    assert db.get(Post, post.id) is not None


def test_text_only_ai_revision_versions_text_and_revokes_rejection(db, tmp_path):
    page, team, game, _ = graph(db)
    post, publication = post_with_feed(
        db, page, team, game, tmp_path / "unused.png", status=PostStatus.REJECTED
    )
    publication.approval_status = "rejected"
    db.commit()

    revised = revise_post(
        db,
        post,
        instruction="Formuliere den Text emotionaler und einladender.",
        revise_text=True,
        revise_graphics=False,
        text_generator=FixtureTextGenerator(),
    )
    db.commit()

    assert revised.version == 2
    assert revised.text_version == 2
    assert revised.status == PostStatus.PENDING
    assert "Fixture-Änderungswunsch" in revised.text
    assert publication.status == JobStatus.UNAPPROVED
    assert publication.approval_status == "unapproved"
    assert revised.design_snapshot["ai_revisions"][-1]["text"] is True


def test_ai_revision_enqueue_is_idempotent(db):
    page, team, game, user = graph(db)
    post, _ = post_with_feed(db, page, team, game, "unused.png")
    first = generation.enqueue_ai_revision(
        db,
        post,
        user,
        post.version,
        "Erzeuge eine emotionalere Abendstimmung im Bild.",
        revise_text=False,
        revise_graphics=True,
        revise_feed=False,
        story_job_ids=["story-target"],
    )
    second = generation.enqueue_ai_revision(
        db,
        post,
        user,
        post.version,
        "Erzeuge eine emotionalere Abendstimmung im Bild.",
        revise_text=False,
        revise_graphics=True,
        revise_feed=False,
        story_job_ids=["story-target"],
    )
    assert first.id == second.id
    assert first.status == GenerationJobStatus.QUEUED
    assert first.parameters["operation"] == "ai_revision"
    assert first.parameters["revise_feed"] is False
    assert first.parameters["story_job_ids"] == ["story-target"]
    assert first.planned_outputs == 1


@pytest.mark.parametrize("mode", ["targeted_edit", "photo_montage"])
def test_targeted_media_revision_enqueue_persists_exact_source_and_one_output(db, mode):
    page, team, game, user = graph(db)
    post, _ = post_with_feed(db, page, team, game, "unused.png")

    job = generation.enqueue_ai_revision(
        db,
        post,
        user,
        post.version,
        "Verschiebe nur den Spieler etwas nach rechts und ändere sonst nichts.",
        revise_text=False,
        revise_graphics=True,
        revise_feed=True,
        story_job_ids=[],
        feed_positions=[2],
        revision_mode=mode,
        source_media_version_id="selected-version-id" if mode == "targeted_edit" else None,
        target_media_slot_id="selected-slot-id",
    )

    assert job.status == GenerationJobStatus.QUEUED
    assert job.planned_outputs == 1
    assert job.parameters["revision_mode"] == mode
    assert job.parameters["source_media_version_id"] == (
        "selected-version-id" if mode == "targeted_edit" else None
    )
    assert job.parameters["target_media_slot_id"] == "selected-slot-id"
    assert job.parameters["feed_positions"] == [2]
    assert job.parameters["revise_text"] is False


def test_post_detail_uses_one_media_catalog_with_per_image_actions():
    source = Path("app/templates/post_detail.html").read_text(encoding="utf-8")

    assert source.count("Medien für die Veröffentlichung") == 1
    assert "Zur Veröffentlichung: Version" in source
    assert "Im Entwurf ausgewählt: Version" in source
    assert "Für Veröffentlichung übernehmen" in source
    assert "media_version.id==slot.selected_version_id %}selected" not in source
    assert "Medienausgaben und Versionen" not in source
    assert "Dieses Karussell enthält genau" in source
    assert "publication.selected_version" in source
    assert "aktuell eingeplant" in source
    assert "/ai-edit" in source
    assert "Dieses Bild gezielt ändern" in source
    assert "Dieses Bild komplett neu erstellen" in source


@pytest.fixture
def shared_contribution(db, tmp_path):
    page, team, game, user = graph(db)
    game.kickoff = game.kickoff.replace(hour=12, minute=0)
    team.rules = {"announcement_enabled": True, "club_matchday_feed_mode": "announcements"}
    other = Team(
        internal_name="separation-two",
        display_name="SV Test II",
        short_name="SVT II",
        slug="separation-two",
        club=team.club,
        instagram_page_id=page.id,
        fussball_url="https://example.invalid/two",
        media_subdir="two",
        rules=team.rules,
    )
    db.add(other)
    db.flush()
    second_game = Game(
        team_id=other.id,
        provider="mock",
        external_id="separation-two",
        home_team=other.display_name,
        away_team="FC II",
        kickoff=game.kickoff + timedelta(hours=2),
        source_url="fixture://two",
    )
    db.add(second_game)
    db.commit()
    primary, feed = post_with_feed(db, page, team, game, tmp_path / "first.png")
    member, story = post_with_feed(db, page, other, second_game, tmp_path / "second.png")
    feed.kind = "carousel"
    story.kind = "story"
    for post in (primary, member):
        post.design_snapshot = {
            "club_matchday_carousel": {
                "primary_post_id": primary.id,
                "member_post_ids": [primary.id, member.id],
                "game_ids": [game.id, second_game.id],
                "role": "primary" if post == primary else "member",
            }
        }
    db.commit()
    return user, [game, second_game], [team, other], [primary, member], [feed, story]


def test_separation_preserves_published_history_and_allows_independent_generation(
    db,
    shared_contribution,
):
    from copy import deepcopy

    from app.approvals.service import ApprovalError, approve_matchday_bundle
    from app.games.bundles import generation_bundle_games
    from app.models import GenerationJob
    from app.posts.separation import separate_matchday_posts

    user, games, teams, posts, jobs = shared_contribution
    jobs[0].status = JobStatus.PUBLISHED
    jobs[0].platform_id = "published-carousel"
    jobs[0].published_at = datetime.now(timezone.utc)
    posts[0].status = PostStatus.PARTIAL
    old_snapshots = [deepcopy(post.design_snapshot) for post in posts]
    old_job = (
        jobs[0].version,
        jobs[0].text_snapshot,
        jobs[0].media_path,
        jobs[0].published_at.replace(tzinfo=None),
    )
    db.commit()

    separate_matchday_posts(db, [games[1]], user)
    db.commit()
    assert jobs[0].status == JobStatus.PUBLISHED
    assert (
        jobs[0].version,
        jobs[0].text_snapshot,
        jobs[0].media_path,
        jobs[0].published_at.replace(tzinfo=None),
    ) == old_job
    assert jobs[0].platform_id == "published-carousel"
    assert jobs[1].status == JobStatus.CANCELLED
    assert all(post.active_key != "active" and not post.publishing_enabled for post in posts)
    assert [post.design_snapshot for post in posts] == old_snapshots
    assert all((game.overrides or {}).get("generation_bundle_separated") for game in games)
    with pytest.raises(ApprovalError, match="Archivierte"):
        approve_matchday_bundle(db, posts[0], user)
    for game, team in zip(games, teams, strict=True):
        assert generation_bundle_games(db, game, team, "announcement")[0] == [game]
        job, existing = generation.enqueue_bundle_create(db, game, team, user, "announcement")
        assert existing is None and job.game_id == game.id
        assert not job.parameters.get("bundle_game_ids")
    assert db.query(GenerationJob).count() == 2
    # Repeating separation cannot cancel the new independent work or rotate keys.
    revisions = [game.overrides["generation_revision"] for game in games]
    separate_matchday_posts(db, games, user)
    assert [game.overrides["generation_revision"] for game in games] == revisions
    assert all(
        job.status == GenerationJobStatus.QUEUED for job in db.scalars(select(GenerationJob))
    )


@pytest.mark.parametrize("status", [JobStatus.PUBLISHING, JobStatus.UNCERTAIN])
def test_separation_refuses_inflight_publications_atomically(db, shared_contribution, status):
    from app.posts.separation import separate_matchday_posts

    user, games, _, posts, jobs = shared_contribution
    jobs[1].status = status
    db.commit()
    with pytest.raises(ValueError, match="Plattformvorgang"):
        separate_matchday_posts(db, games, user)
    assert all(post.active_key == "active" for post in posts)
    assert not games[0].overrides.get("generation_bundle_separated")
    assert jobs[0].status != JobStatus.CANCELLED


def test_separation_blocks_existing_meta_container(db, shared_contribution):
    from app.models import MetaPublishingAttempt
    from app.posts.separation import separate_matchday_posts

    user, games, _, posts, jobs = shared_contribution
    from app.models import InstagramConnection

    connection = InstagramConnection(instagram_page_id=posts[1].instagram_page_id)
    db.add(connection)
    db.flush()
    attempt = MetaPublishingAttempt(
        publication_job_id=jobs[1].id,
        connection_id=connection.id,
        target_account_id="fixture",
        media_kind="story",
        local_media_version=1,
        media_path="fixture.png",
        file_checksum="a" * 64,
        started_by=user.id,
        meta_container_id="container",
        phase="failed",
    )
    db.add(attempt)
    db.commit()
    with pytest.raises(ValueError, match="Meta-Vorgang"):
        separate_matchday_posts(db, games, user)
    assert all(post.active_key == "active" for post in posts)


def test_separation_requires_every_teams_permission(db, shared_contribution):
    from app.posts.separation import separate_matchday_posts

    user, games, _, posts, _ = shared_contribution
    user.role = Role.VIEWER
    user.all_teams = False
    db.commit()
    with pytest.raises(PermissionError):
        separate_matchday_posts(db, [games[0]], user)
    assert all(post.active_key == "active" for post in posts)


def test_reschedule_dissolves_existing_bundle_but_time_change_does_not(db, shared_contribution):
    from app.posts.separation import separate_rescheduled_matchday

    _, games, _, posts, jobs = shared_contribution
    assert not separate_rescheduled_matchday(db, games[1], games[1].kickoff + timedelta(hours=1))
    assert posts[0].active_key == "active"
    assert separate_rescheduled_matchday(db, games[1], games[1].kickoff + timedelta(days=10))
    assert all(post.active_key != "active" for post in posts)
    assert all(job.status == JobStatus.CANCELLED for job in jobs)


def test_imported_reschedule_preserves_split_overrides_and_published_jobs(db, shared_contribution):
    from app.games.importer import import_snapshot
    from app.models import ProviderSnapshot

    _, games, teams, posts, jobs = shared_contribution
    game = games[1]
    game.provider = "fussball.de"
    jobs[0].status = JobStatus.PUBLISHED
    jobs[0].platform_id = "existing-publication"
    new_kickoff = game.kickoff + timedelta(days=10)
    snapshot = ProviderSnapshot(
        team_id=teams[1].id,
        source_url="fixture://reschedule",
        status_code=200,
        fetched_at=datetime.now(timezone.utc),
        checksum="b" * 64,
        relative_path="fixture.html",
        parser_result={
            "games": [
                {
                    "external_id": game.external_id,
                    "home_team": game.home_team,
                    "away_team": game.away_team,
                    "kickoff": new_kickoff.isoformat(),
                    "status": "scheduled",
                }
            ]
        },
    )
    db.add(snapshot)
    db.commit()
    import_snapshot(db, snapshot)
    db.commit()
    db.refresh(game)
    assert game.kickoff.replace(tzinfo=timezone.utc) == new_kickoff
    assert game.overrides["generation_bundle_separated"]
    assert game.overrides["generation_revision"]
    assert game.overrides["snapshot_id"] == snapshot.id
    assert all(post.active_key != "active" for post in posts)
    assert jobs[0].status == JobStatus.PUBLISHED
    assert jobs[0].platform_id == "existing-publication"
    assert jobs[1].status == JobStatus.CANCELLED


@pytest.mark.parametrize("running", [False, True])
def test_separation_cancels_queued_coordinator_but_blocks_running_generation(
    db, shared_contribution, running
):
    from app.models import GenerationJob, GenerationJobType
    from app.posts.separation import separate_matchday_posts

    user, games, _, posts, jobs = shared_contribution
    job = GenerationJob(
        job_type=GenerationJobType.CREATE_POST,
        game_id=games[0].id,
        team_id=games[0].team_id,
        post_type="announcement",
        requested_by=user.id,
        status=GenerationJobStatus.RUNNING if running else GenerationJobStatus.QUEUED,
        idempotency_key="old-bundle",
        active_key="old-bundle",
        parameters={"bundle_game_ids": [game.id for game in games]},
    )
    db.add(job)
    db.commit()
    if running:
        with pytest.raises(ValueError, match="Generierung"):
            separate_matchday_posts(db, [games[1]], user)
        assert all(post.active_key == "active" for post in posts)
        assert all(publication.status != JobStatus.CANCELLED for publication in jobs)
    else:
        separate_matchday_posts(db, [games[1]], user)
        assert job.status == GenerationJobStatus.CANCELLED
        assert job.cancel_requested and job.active_key is None
        assert job.completed_at is not None


def test_separation_refuses_nonreciprocal_bundle(db, shared_contribution):
    from app.posts.separation import separate_matchday_posts

    user, games, _, posts, jobs = shared_contribution
    posts[1].design_snapshot = {}
    db.commit()
    with pytest.raises(ValueError, match="widersprüchlich"):
        separate_matchday_posts(db, [games[0]], user)
    assert all(post.active_key == "active" for post in posts)
    assert all(job.status != JobStatus.CANCELLED for job in jobs)


def test_old_bundle_retry_cannot_adopt_new_drafts_or_call_generators(
    db, shared_contribution, monkeypatch
):
    from app.models import GenerationJob, GenerationJobType
    from app.posts.separation import separate_matchday_posts

    user, games, _, posts, _ = shared_contribution
    job = GenerationJob(
        job_type=GenerationJobType.CREATE_POST,
        game_id=games[0].id,
        team_id=games[0].team_id,
        post_type="announcement",
        requested_by=user.id,
        status=GenerationJobStatus.FAILED,
        idempotency_key="old-retry",
        parameters={"bundle_game_ids": [game.id for game in games]},
    )
    db.add(job)
    db.commit()
    separate_matchday_posts(db, [games[1]], user)
    db.commit()
    new_post = Post(
        game_id=games[0].id,
        team_id=games[0].team_id,
        instagram_page_id=posts[0].instagram_page_id,
        post_type="announcement",
        status=PostStatus.PENDING,
        text="Neuer unabhängiger Beitrag",
    )
    db.add(new_post)
    job.status = GenerationJobStatus.RUNNING
    db.commit()

    def forbidden(*args, **kwargs):
        pytest.fail("An archived bundle must never reach a generator")

    monkeypatch.setattr(generation, "build_text_generator", forbidden)
    monkeypatch.setattr(generation, "build_renderer", forbidden)
    result = generation.process_generation_job(db, job.id, Settings())
    assert result.status == GenerationJobStatus.FAILED
    assert "getrennt" in result.error_message
    assert new_post.status == PostStatus.PENDING
    assert not new_post.critical_warnings
    assert result.result_post_id is None
