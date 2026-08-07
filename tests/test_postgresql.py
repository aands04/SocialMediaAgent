"""PostgreSQL-only transaction and concurrency tests for staging/CI."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.jobs.generation import claim_next, enqueue_create
from app.meta.publishing import MetaPublishingError, _reload_attempt_context
from app.models import (
    AccountType,
    Club,
    ClubStatus,
    Game,
    GeneratedMediaSlot,
    GeneratedMediaVersion,
    GenerationJob,
    InstagramConnection,
    InstagramPage,
    MediaAsset,
    MetaPublishingAttempt,
    PlanProfile,
    Post,
    PublicationJob,
    Role,
    Team,
    User,
)
from app.posts.media_versions import register_media_version
from app.posts.service import reserve_image

URL = os.getenv("TEST_POSTGRES_URL")
pytestmark = pytest.mark.postgresql


@pytest.fixture
def pg():
    if not URL:
        pytest.skip("TEST_POSTGRES_URL nicht gesetzt")
    engine = create_engine(URL, pool_pre_ping=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    yield factory
    Base.metadata.drop_all(engine)
    engine.dispose()


def graph(factory):
    with factory() as db:
        profile = PlanProfile(name="PostgreSQL-Test", description="Testprofil", version=1)
        db.add(profile)
        db.flush()
        club = Club(
            name="PostgreSQL-Testverein",
            short_name="PG",
            slug="postgresql-testverein",
            status=ClubStatus.ACTIVE,
            timezone="Europe/Berlin",
            plan_profile_id=profile.id,
        )
        db.add(club)
        db.flush()
        page = InstagramPage(
            club_id=club.id,
            internal_name="pg",
            display_name="PG",
            username="pg",
            club="C",
            active=True,
        )
        db.add(page)
        db.flush()
        team = Team(
            club_id=club.id,
            internal_name="pg",
            display_name="PG",
            short_name="PG",
            slug="pg",
            club="C",
            fussball_url="https://www.fussball.de/pg",
            instagram_page_id=page.id,
            media_subdir="pg",
        )
        db.add(team)
        db.flush()
        games = [
            Game(
                club_id=club.id,
                team_id=team.id,
                external_id=f"g{x}",
                home_team="A",
                away_team="B",
                kickoff=datetime.now(timezone.utc) + timedelta(days=x),
                source_url="fixture://pg",
            )
            for x in (1, 2)
        ]
        db.add_all(games)
        db.flush()
        asset = MediaAsset(
            club_id=club.id,
            team_id=team.id,
            relative_path="p.png",
            filename="p.png",
            mime_type="image/png",
            size=1,
            checksum="x",
            mtime=datetime.now(timezone.utc),
        )
        db.add(asset)
        db.commit()
        return club.id, page.id, team.id, [x.id for x in games], asset.id


def test_parallel_image_reservation_uses_postgresql_lock(pg):
    _, _, team_id, game_ids, asset_id = graph(pg)

    def reserve(game_id):
        with pg() as db:
            result = reserve_image(db, team_id, game_id)
            db.commit()
            return result.id if result else None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, game_ids))
    assert results.count(asset_id) == 1 and results.count(None) == 1
    with pg() as db:
        assert db.get(MediaAsset, asset_id).uses == 1


def test_unique_idempotency_key_and_main_post_constraint(pg):
    club_id, page_id, team_id, game_ids, _ = graph(pg)
    with pg() as db:
        post = Post(
            club_id=club_id,
            game_id=game_ids[0],
            team_id=team_id,
            instagram_page_id=page_id,
            post_type="announcement",
        )
        db.add(post)
        db.flush()
        base = dict(
            club_id=club_id,
            post_id=post.id,
            game_id=game_ids[0],
            team_id=team_id,
            instagram_page_id=page_id,
            kind="feed",
            media_path="x",
            scheduled_at=datetime.now(timezone.utc),
            idempotency_key="same-key",
        )
        db.add(PublicationJob(**base))
        db.commit()
        db.add(PublicationJob(**base))
        with pytest.raises(IntegrityError):
            db.commit()
    with pg() as db:
        original = db.scalar(select(Post).where(Post.game_id == game_ids[0]))
        db.add(
            Post(
                club_id=club_id,
                game_id=game_ids[0],
                team_id=team_id,
                instagram_page_id=page_id,
                post_type="announcement",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        assert original is not None


def test_generation_job_claim_uses_skip_locked(pg):
    club_id, _, team_id, game_ids, _ = graph(pg)
    with pg() as db:
        user = User(
            club_id=club_id,
            account_type=AccountType.CLUB_USER,
            email="generation@pg.invalid",
            password_hash="x",
            role=Role.ADMIN,
            all_teams=True,
        )
        db.add(user)
        db.flush()
        game = db.get(Game, game_ids[0])
        team = db.get(Team, team_id)
        job, _ = enqueue_create(db, game, team, user, "announcement")
        job_id = job.id

    def claim(worker):
        with pg() as db:
            return claim_next(db, worker)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(claim, ["worker-a", "worker-b"]))
    assert claimed.count(job_id) == 1
    assert claimed.count(None) == 1
    with pg() as db:
        assert db.get(GenerationJob, job_id).locked_by in {"worker-a", "worker-b"}


def test_meta_attempt_lock_uses_skip_locked(pg):
    club_id, page_id, team_id, game_ids, _ = graph(pg)
    with pg() as db:
        user = User(
            club_id=club_id,
            account_type=AccountType.CLUB_USER,
            email="meta-lock@pg.invalid",
            password_hash="x",
            role=Role.ADMIN,
            all_teams=True,
        )
        connection = InstagramConnection(
            club_id=club_id,
            instagram_page_id=page_id,
            instagram_user_id="ig-test",
            status="connected",
        )
        post = Post(
            club_id=club_id,
            game_id=game_ids[0],
            team_id=team_id,
            instagram_page_id=page_id,
            post_type="announcement",
        )
        db.add_all([user, connection, post])
        db.flush()
        publication = PublicationJob(
            club_id=club_id,
            post_id=post.id,
            game_id=game_ids[0],
            team_id=team_id,
            instagram_page_id=page_id,
            kind="feed",
            media_path="fixture.png",
            scheduled_at=datetime.now(timezone.utc),
            idempotency_key="meta-lock-publication",
        )
        db.add(publication)
        db.flush()
        attempt = MetaPublishingAttempt(
            club_id=club_id,
            publication_job_id=publication.id,
            connection_id=connection.id,
            active_key=publication.id,
            target_account_id="ig-test",
            media_kind="feed",
            local_media_version=1,
            media_path="fixture.png",
            file_checksum="a" * 64,
            started_by=user.id,
        )
        db.add(attempt)
        db.commit()
        attempt_id = attempt.id

    with pg() as locker:
        locker.scalar(
            select(MetaPublishingAttempt)
            .where(MetaPublishingAttempt.id == attempt_id)
            .with_for_update()
        )

        def competing_lock():
            with pg() as contender:
                with pytest.raises(MetaPublishingError, match="bereits"):
                    _reload_attempt_context(contender, attempt_id)

        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(competing_lock).result(timeout=5)


def test_parallel_media_versions_are_monotonic_and_never_overwritten(pg, tmp_path):
    club_id, page_id, team_id, game_ids, _ = graph(pg)
    with pg() as db:
        post = Post(
            club_id=club_id,
            game_id=game_ids[0],
            team_id=team_id,
            instagram_page_id=page_id,
            post_type="announcement",
        )
        db.add(post)
        db.flush()
        slot = GeneratedMediaSlot(
            club_id=post.club_id,
            post_id=post.id,
            game_id=post.game_id,
            team_id=post.team_id,
            slot_key="feed:1:variant:1",
            media_kind="feed",
            output_position=1,
            variant_number=1,
            label="Feed-Variante 1",
        )
        db.add(slot)
        db.commit()
        post_id, slot_id = post.id, slot.id

    paths: list[Path] = []
    for number, color in ((1, "blue"), (2, "red")):
        path = tmp_path / f"parallel-version-{number}.png"
        Image.new("RGB", (1080, 1350), color).save(path)
        paths.append(path)

    def create_version(path: Path):
        with pg() as db:
            post = db.get(Post, post_id)
            slot = db.get(GeneratedMediaSlot, slot_id)
            version = register_media_version(db, post, slot, str(path))
            db.commit()
            return version.id, version.version_number, version.media_path

    with ThreadPoolExecutor(max_workers=2) as pool:
        created = list(pool.map(create_version, paths))

    assert sorted(number for _, number, _ in created) == [1, 2]
    assert {path for _, _, path in created} == {str(path) for path in paths}
    with pg() as db:
        versions = list(
            db.scalars(
                select(GeneratedMediaVersion)
                .where(GeneratedMediaVersion.slot_id == slot_id)
                .order_by(GeneratedMediaVersion.version_number)
            )
        )
        slot = db.get(GeneratedMediaSlot, slot_id)
        assert [item.version_number for item in versions] == [1, 2]
        assert slot.latest_version_id == versions[-1].id
        assert slot.selected_version_id == versions[-1].id
