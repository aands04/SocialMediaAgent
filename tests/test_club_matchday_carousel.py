from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PIL import Image

from app.approvals.service import approve
from app.config import Settings
from app.games.bundles import connect_games, generation_bundle_games, separate_games
from app.models import (
    Game,
    InstagramPage,
    JobStatus,
    Post,
    PostStatus,
    PublicationJob,
    PublicationMediaItem,
    Role,
    Team,
    User,
)
from app.posts.club_carousel import coordinate_club_matchday_feed
from app.posts.service import create_matchday_bundle_posts, create_post, feed_time
from app.prompts.service import ResolvedPrompt
from app.publishing.service import DryRunPublisher, PublishError
from app.publishing.worker import process_job
from app.textgen.service import FixtureTextGenerator, GeneratedText


class LocalRenderer:
    is_ai = False

    def __init__(self, root: Path):
        self.root = root

    def render(self, kind: str, target: str, _data: dict) -> Path:
        output = self.root / target
        output.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1080, 1350 if kind == "feed" else 1920), "blue").save(
            output
        )
        return output


def _page(db) -> InstagramPage:
    page = InstagramPage(
        internal_name="sv-ehlen",
        display_name="SV Ehlen",
        username="svehlen1901",
        club="SV Ehlen",
        account_id="instagram-1",
        active=True,
        connection_status="connected",
        publishing_enabled=True,
    )
    db.add(page)
    db.flush()
    return page


def _team(db, page: InstagramPage, *, number: int, mode: str) -> Team:
    team = Team(
        internal_name=f"Mannschaft {number}",
        display_name=f"SV Ehlen {number}",
        short_name=f"SVE {number}",
        slug=f"sv-ehlen-{number}",
        club="SV Ehlen",
        fussball_url=f"https://www.fussball.de/team/{number}",
        instagram_page_id=page.id,
        media_subdir=f"team-{number}",
        publishing_enabled=True,
        hashtags=["#SVEhlen"],
        rules={
            "announcement_enabled": True,
            "result_enabled": True,
            "club_matchday_feed_mode": mode,
            "result_timing_mode": "result_detected",
            "result_wait_minutes": 180,
        },
    )
    db.add(team)
    db.flush()
    return team


def _game(db, team: Team, *, hour: int, number: int) -> Game:
    game = Game(
        team_id=team.id,
        external_id=f"game-{number}",
        home_team=team.display_name,
        away_team=f"Gegner {number}",
        kickoff=datetime(2026, 8, 9, hour, 0, tzinfo=timezone.utc),
        competition="Kreisliga",
        venue="Habichtswaldstadion",
        status="scheduled",
        source_url=f"https://www.fussball.de/spiel/{number}",
        overrides={},
    )
    db.add(game)
    db.flush()
    return game


def _feed_post(
    db,
    tmp_path: Path,
    team: Team,
    game: Game,
    *,
    number: int,
    post_type: str = "announcement",
) -> Post:
    path = tmp_path / f"feed-{number}.png"
    Image.new("RGB", (1080, 1350), "blue").save(path)
    story_path = tmp_path / f"story-{number}.png"
    Image.new("RGB", (1080, 1920), "blue").save(story_path)
    post = Post(
        game_id=game.id,
        team_id=team.id,
        instagram_page_id=team.instagram_page_id,
        post_type=post_type,
        status=PostStatus.PENDING,
        text=f"Ankündigung {number}",
        feed_path=str(path),
        design_snapshot={},
        critical_warnings=[],
    )
    db.add(post)
    db.flush()
    db.add_all(
        [
            PublicationJob(
                post_id=post.id,
                game_id=game.id,
                team_id=team.id,
                instagram_page_id=team.instagram_page_id,
                kind="feed",
                media_path=str(path),
                text_snapshot=post.text,
                scheduled_at=game.kickoff - timedelta(days=1),
                idempotency_key=f"{post.id}:feed:v1",
            ),
            PublicationJob(
                post_id=post.id,
                game_id=game.id,
                team_id=team.id,
                instagram_page_id=team.instagram_page_id,
                kind="story",
                media_path=str(story_path),
                scheduled_at=game.kickoff - timedelta(hours=6),
                idempotency_key=f"{post.id}:story:v1",
            ),
        ]
    )
    db.flush()
    return post


def test_result_detected_is_literal_and_creates_feed_and_story(db, tmp_path):
    detected = datetime(2026, 8, 9, 15, 8, tzinfo=timezone.utc)
    page = _page(db)
    team = _team(db, page, number=1, mode="separate")
    game = _game(db, team, hour=13, number=1)
    game.status = "finished"
    game.result_confirmed = True
    game.home_score = 3
    game.away_score = 1
    game.overrides = {"result_detected_at": detected.isoformat()}
    db.commit()

    scheduled_at, absolute = feed_time(team, game, "result")
    assert scheduled_at == detected
    assert absolute is False

    post = create_post(
        db,
        game,
        team,
        FixtureTextGenerator(),
        LocalRenderer(tmp_path / "generated"),
        post_type="result",
    )
    jobs = db.query(PublicationJob).filter_by(post_id=post.id).all()
    assert sorted(job.kind for job in jobs) == ["feed", "story"]
    assert {job.scheduled_at.replace(tzinfo=timezone.utc) for job in jobs} == {detected}
    assert next(job for job in jobs if job.kind == "story").story_rule_id is None


def test_result_detected_publishes_immediately_after_manual_approval(db, tmp_path):
    detected = datetime.now(timezone.utc) - timedelta(minutes=20)
    page = _page(db)
    team = _team(db, page, number=1, mode="separate")
    team.rules = {**team.rules, "late_approval": "manual"}
    game = _game(db, team, hour=13, number=1)
    game.status = "finished"
    game.result_confirmed = True
    game.home_score = 2
    game.away_score = 0
    game.overrides = {"result_detected_at": detected.isoformat()}
    post = create_post(
        db,
        game,
        team,
        FixtureTextGenerator(),
        LocalRenderer(tmp_path / "manual-approval"),
        post_type="result",
    )
    post.critical_warnings = []
    post.design_snapshot = {
        **(post.design_snapshot or {}),
        "logos": {
            "team": {
                "id": "verified-logo",
                "checksum": "a" * 64,
                "verified": True,
            }
        },
    }
    approver = User(
        email="redaktion@example.invalid",
        password_hash="not-used",
        role=Role.APPROVER,
        all_teams=True,
        active=True,
    )
    db.add(approver)
    db.commit()

    approved_after = datetime.now(timezone.utc)
    approve(db, post, approver)

    jobs = db.query(PublicationJob).filter_by(post_id=post.id).all()
    assert jobs
    assert all(job.status == JobStatus.SCHEDULED for job in jobs)
    assert all(job.approval_status == "approved" for job in jobs)
    assert all(
        job.scheduled_at.replace(tzinfo=timezone.utc) >= approved_after for job in jobs
    )


def test_same_club_same_day_builds_one_feed_carousel_and_separate_stories(db, tmp_path):
    page = _page(db)
    first = _team(db, page, number=1, mode="announcements")
    second = _team(db, page, number=2, mode="announcements")
    first_game = _game(db, first, hour=13, number=1)
    second_game = _game(db, second, hour=15, number=2)
    first_post = _feed_post(db, tmp_path, first, first_game, number=1)
    db.commit()

    waiting = coordinate_club_matchday_feed(db, first_post, requested_by=None)
    assert waiting.active is True and waiting.complete is False
    first_feed = db.query(PublicationJob).filter_by(
        post_id=first_post.id, kind="feed"
    ).one()
    assert first_feed.status == JobStatus.WAITING
    assert first_feed.approval_status == "bundle_wait"

    second_post = _feed_post(db, tmp_path, second, second_game, number=2)
    db.commit()
    completed = coordinate_club_matchday_feed(db, second_post, requested_by=None)
    db.commit()

    assert completed.active is True and completed.complete is True
    assert completed.primary_post_id == first_post.id
    db.refresh(first_post)
    assert "SV Ehlen 1" in first_post.text
    assert "SV Ehlen 2" in first_post.text
    primary = db.query(PublicationJob).filter_by(
        post_id=first_post.id, kind="carousel"
    ).one()
    media = db.query(PublicationMediaItem).filter_by(
        publication_job_id=primary.id
    ).order_by(PublicationMediaItem.position).all()
    assert [item.position for item in media] == [1, 2]
    secondary_feed = db.query(PublicationJob).filter_by(
        post_id=second_post.id, kind="feed"
    ).one()
    assert secondary_feed.status == JobStatus.CANCELLED
    stories = db.query(PublicationJob).filter(
        PublicationJob.post_id.in_([first_post.id, second_post.id]),
        PublicationJob.kind == "story",
    ).all()
    assert len(stories) == 2
    assert all(job.status != JobStatus.CANCELLED for job in stories)


def test_games_can_be_consciously_connected_and_separated(db):
    page = _page(db)
    first = _team(db, page, number=1, mode="separate")
    second = _team(db, page, number=2, mode="separate")
    first_game = _game(db, first, hour=13, number=1)
    second_game = _game(db, second, hour=15, number=2)
    teams = {first.id: first, second.id: second}

    bundle_id = connect_games(db, [first_game, second_game], teams)
    bundled, _, key = generation_bundle_games(
        db, first_game, first, "announcement"
    )

    assert key == f"manual:{bundle_id}"
    assert [item.id for item in bundled] == [first_game.id, second_game.id]

    separate_games(bundled)
    db.flush()
    separated, _, key = generation_bundle_games(
        db, first_game, first, "announcement"
    )
    assert [item.id for item in separated] == [first_game.id]
    assert key is None
    assert first_game.overrides["generation_bundle_separated"] is True
    assert second_game.overrides["generation_bundle_separated"] is True


def test_shared_matchday_generation_uses_one_text_prompt_and_per_game_media(
    db, tmp_path, monkeypatch
):
    page = _page(db)
    first = _team(db, page, number=1, mode="announcements")
    second = _team(db, page, number=2, mode="announcements")
    first_game = _game(db, first, hour=13, number=1)
    second_game = _game(db, second, hour=15, number=2)
    games = [first_game, second_game]
    teams = {first.id: first, second.id: second}
    calls: list[dict] = []

    class SharedTextGenerator:
        is_ai = True

        def generate(self, data):
            calls.append(data)
            return GeneratedText(
                "Gemeinsam: SV Ehlen 1 und SV Ehlen 2 spielen am Sonntag.",
                "test-model",
                prompt_version="shared-v1",
            )

    prompt = ResolvedPrompt(
        name="announcement",
        version=1,
        prompt_kind="text",
        post_type="announcement",
        media_kind="none",
        model="test-model",
        quality="standard",
        body="Grundprompt",
        rendered="Grundprompt",
        builtin=True,
    )
    monkeypatch.setattr("app.posts.service.resolve_prompt", lambda *_args, **_kwargs: prompt)

    posts = create_matchday_bundle_posts(
        db,
        games,
        teams,
        SharedTextGenerator(),
        LocalRenderer(tmp_path / "shared-generation"),
        "announcement",
        {item.id: {"team": None, "opponent": None} for item in games},
        "automatic:test",
    )
    state = coordinate_club_matchday_feed(db, posts[-1], requested_by=None)
    db.commit()

    assert len(calls) == 1
    assert calls[0]["matchday_games"] == [
        {
            "position": 1,
            "home_team": first_game.home_team,
            "away_team": first_game.away_team,
            "date": "09.08.2026",
            "time": "15:00",
            "competition": "Kreisliga",
            "venue": "Habichtswaldstadion",
            "score": None,
        },
        {
            "position": 2,
            "home_team": second_game.home_team,
            "away_team": second_game.away_team,
            "date": "09.08.2026",
            "time": "17:00",
            "competition": "Kreisliga",
            "venue": "Habichtswaldstadion",
            "score": None,
        },
    ]
    assert len(posts) == 2
    assert {item.text for item in posts} == {
        "Gemeinsam: SV Ehlen 1 und SV Ehlen 2 spielen am Sonntag."
    }
    assert all((item.design_snapshot or {})["matchday_bundle"] for item in posts)
    assert state.complete is True
    carousel = db.query(PublicationJob).filter_by(
        post_id=state.primary_post_id, kind="carousel"
    ).one()
    media = db.query(PublicationMediaItem).filter_by(
        publication_job_id=carousel.id
    ).all()
    assert len(media) == 2


def test_preferred_team_image_is_first_even_when_it_plays_later(db, tmp_path):
    page = _page(db)
    first = _team(db, page, number=1, mode="announcements")
    second = _team(db, page, number=2, mode="announcements")
    first.rules = {
        **first.rules,
        "club_matchday_primary_team_id": first.id,
    }
    second.rules = {
        **second.rules,
        "club_matchday_primary_team_id": first.id,
    }
    second_game = _game(db, second, hour=13, number=2)
    first_game = _game(db, first, hour=15, number=1)
    second_post = _feed_post(db, tmp_path, second, second_game, number=2)
    first_post = _feed_post(db, tmp_path, first, first_game, number=1)
    db.commit()

    completed = coordinate_club_matchday_feed(db, first_post, requested_by=None)
    db.commit()

    assert completed.complete is True
    assert completed.primary_post_id == first_post.id
    carousel = db.query(PublicationJob).filter_by(
        post_id=first_post.id,
        kind="carousel",
    ).one()
    media = (
        db.query(PublicationMediaItem)
        .filter_by(publication_job_id=carousel.id)
        .order_by(PublicationMediaItem.position)
        .all()
    )
    assert [item.media_path for item in media] == [
        first_post.feed_path,
        second_post.feed_path,
    ]


def test_shared_result_feed_waits_for_every_club_result(db, tmp_path):
    page = _page(db)
    first = _team(db, page, number=1, mode="announcements_and_results")
    second = _team(db, page, number=2, mode="announcements_and_results")
    first_game = _game(db, first, hour=13, number=1)
    second_game = _game(db, second, hour=15, number=2)
    first_game.status = "finished"
    first_game.result_confirmed = True
    first_game.home_score = 2
    first_game.away_score = 1
    first_post = _feed_post(
        db, tmp_path, first, first_game, number=1, post_type="result"
    )
    db.commit()

    waiting = coordinate_club_matchday_feed(db, first_post, requested_by=None)
    assert waiting.active is True and waiting.complete is False
    assert waiting.waiting_for == (second.display_name,)

    second_game.status = "finished"
    second_game.result_confirmed = True
    second_game.home_score = 0
    second_game.away_score = 3
    second_post = _feed_post(
        db, tmp_path, second, second_game, number=2, post_type="result"
    )
    db.commit()
    completed = coordinate_club_matchday_feed(db, second_post, requested_by=None)
    db.commit()

    assert completed.complete is True
    primary = db.query(Post).filter_by(id=completed.primary_post_id).one()
    assert "2:1" in primary.text
    assert "0:3" in primary.text
    assert (
        db.query(PublicationJob)
        .filter_by(post_id=primary.id, kind="carousel")
        .one()
        .scheduled_at
        == max(
            job.scheduled_at
            for job in db.query(PublicationJob).filter(
                PublicationJob.post_id.in_([first_post.id, second_post.id]),
                PublicationJob.kind.in_(["carousel", "feed"]),
            )
        )
    )


def test_grouped_feed_rechecks_every_game_immediately_before_publish(db, tmp_path):
    page = _page(db)
    first = _team(db, page, number=1, mode="announcements")
    second = _team(db, page, number=2, mode="announcements")
    first_game = _game(db, first, hour=13, number=1)
    second_game = _game(db, second, hour=15, number=2)
    first_post = _feed_post(db, tmp_path, first, first_game, number=1)
    second_post = _feed_post(db, tmp_path, second, second_game, number=2)
    db.commit()
    coordinate_club_matchday_feed(db, second_post, requested_by=None)
    db.commit()
    db.refresh(first_post)
    carousel = db.query(PublicationJob).filter_by(
        post_id=first_post.id, kind="carousel"
    ).one()
    first_post.status = PostStatus.APPROVED
    first_post.approved_version = first_post.version
    carousel.status = JobStatus.SCHEDULED
    carousel.approval_status = "approved"
    carousel.approved_post_version = first_post.version
    carousel.scheduled_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    second_game.status = "cancelled"
    db.commit()

    with pytest.raises(PublishError, match="Vereins-Karussell"):
        process_job(
            db,
            carousel.id,
            DryRunPublisher(),
            Settings(global_publish_enabled=True),
        )
    db.refresh(carousel)
    assert carousel.status == JobStatus.UNAPPROVED
