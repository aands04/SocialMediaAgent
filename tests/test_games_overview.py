from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.games.overview import build_game_automation_summary
from app.models import (
    Game,
    GenerationJob,
    GenerationJobStatus,
    GenerationJobType,
    InstagramPage,
    Post,
    PostStatus,
    Team,
)


def _game_setup(db, *, suffix="one", kickoff=None, rules=None):
    kickoff = kickoff or datetime(2026, 8, 16, 13, 0, tzinfo=timezone.utc)
    page = InstagramPage(
        internal_name=f"overview-{suffix}",
        display_name=f"Übersicht {suffix}",
        username=f"overview_{suffix}",
        club="Testverein",
        active=True,
    )
    db.add(page)
    db.flush()
    team = Team(
        internal_name=f"overview-team-{suffix}",
        display_name=f"Testverein {suffix}",
        short_name=f"TV {suffix}",
        slug=f"overview-{suffix}",
        club="Testverein",
        fussball_url=f"https://www.fussball.de/mannschaft/{suffix}",
        instagram_page_id=page.id,
        media_subdir=f"overview-{suffix}",
        rules=rules
        or {
            "automatic_generation_enabled": True,
            "announcement_enabled": True,
            "reminder_enabled": True,
            "result_enabled": True,
            "generation_lead_days": 4,
        },
    )
    db.add(team)
    db.flush()
    game = Game(
        team_id=team.id,
        provider="fussball.de",
        external_id=f"overview-game-{suffix}",
        home_team=team.display_name,
        away_team=f"Gegner {suffix}",
        kickoff=kickoff,
        competition="Kreisliga",
        venue="Sportplatz",
        pitch="Rasenplatz",
        status="scheduled",
        source_url=f"https://www.fussball.de/spiel/{suffix}",
        overrides={},
    )
    db.add(game)
    db.commit()
    return page, team, game


def _summary(db, games, teams, *, posts=None, jobs=None, now=None, enabled=True):
    return build_game_automation_summary(
        db,
        club_id=db.info["test_club_id"],
        games=games,
        teams={team.id: team for team in teams},
        posts=posts or [],
        generation_jobs=jobs or [],
        story_rules=[],
        publication_rows=[],
        settings=Settings(automatic_post_generation_enabled=enabled),
        bundle_id="bundle" if len(games) > 1 else None,
        now=now or datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc),
    )


def test_overview_lists_multiple_generation_types_and_event_result(db):
    _, team, game = _game_setup(db)
    summary = _summary(db, [game], [team])

    assert summary.next_generation_at == datetime(2026, 8, 12, 13, 0, tzinfo=timezone.utc)
    assert summary.generation_schedule_state == "planned"
    assert summary.contribution_status == "planned"
    assert summary.contribution_label == "Automatisch geplant"
    assert summary.automation_enabled is True
    assert summary.generation_count == 3
    assert summary.additional_generation_count == 2
    assert {item.post_type for item in summary.generation_items} == {
        "announcement",
        "reminder",
        "result",
    }
    result = next(item for item in summary.generation_items if item.post_type == "result")
    assert result.state == "event"
    assert result.label == "Nach bestätigtem Endergebnis"


def test_overview_marks_created_overdue_and_disabled_states(db):
    page, team, game = _game_setup(
        db,
        rules={
            "automatic_generation_enabled": True,
            "announcement_enabled": True,
            "generation_lead_days": 4,
        },
    )
    created = Post(
        game_id=game.id,
        team_id=team.id,
        instagram_page_id=page.id,
        post_type="announcement",
        status=PostStatus.APPROVED,
        text="Fertig",
    )
    db.add(created)
    db.commit()

    complete = _summary(db, [game], [team], posts=[created])
    assert complete.contribution_label == "Erstellt"
    assert complete.generation_schedule_state == "created"

    overdue = _summary(
        db,
        [game],
        [team],
        now=datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc),
    )
    assert overdue.generation_schedule_state == "overdue"
    assert overdue.action_required is True

    disabled = _summary(db, [game], [team], enabled=False)
    assert disabled.generation_schedule_state == "disabled"
    assert disabled.next_generation_label == "Nicht automatisch geplant"
    assert disabled.automation_enabled is False


def test_overview_marks_missing_contribution_rule_as_manual(db):
    _, team, game = _game_setup(
        db,
        rules={
            "automatic_generation_enabled": True,
            "announcement_enabled": False,
            "reminder_enabled": False,
            "result_enabled": False,
        },
    )

    summary = _summary(db, [game], [team])

    assert summary.automation_enabled is True
    assert summary.generation_schedule_state == "no_rule"
    assert summary.next_generation_label == "Manuelle Erstellung erforderlich"
    assert summary.contribution_status == "manual"


def test_overview_bundle_keeps_different_team_schedules_visible(db):
    kickoff = datetime(2026, 8, 16, 13, 0, tzinfo=timezone.utc)
    _, first, first_game = _game_setup(
        db,
        suffix="first",
        kickoff=kickoff,
        rules={
            "automatic_generation_enabled": True,
            "announcement_enabled": True,
            "generation_lead_days": 4,
        },
    )
    _, second, second_game = _game_setup(
        db,
        suffix="second",
        kickoff=kickoff + timedelta(hours=2),
        rules={
            "automatic_generation_enabled": True,
            "announcement_enabled": True,
            "generation_lead_days": 2,
        },
    )

    summary = _summary(db, [first_game, second_game], [first, second])

    assert summary.generation_count == 2
    assert summary.distinct_generation_times == 2
    assert {item.team_name for item in summary.generation_items} == {
        first.display_name,
        second.display_name,
    }


def test_overview_recalculates_after_rescheduling_and_rejects_foreign_tenant(db):
    _, team, game = _game_setup(db)
    before = _summary(db, [game], [team]).next_generation_at
    game.kickoff += timedelta(days=7)
    after = _summary(db, [game], [team]).next_generation_at
    assert after == before + timedelta(days=7)

    game.club_id = "foreign-club"
    with pytest.raises(ValueError, match="Vereinskontext"):
        _summary(db, [game], [team])


def test_overview_shows_running_generation_without_inventing_a_second_schedule(db):
    _, team, game = _game_setup(
        db,
        rules={
            "automatic_generation_enabled": True,
            "announcement_enabled": True,
            "generation_lead_days": 4,
        },
    )
    job = GenerationJob(
        id="overview-running-job",
        club_id=db.info["test_club_id"],
        job_type=GenerationJobType.CREATE_POST,
        game_id=game.id,
        team_id=team.id,
        post_type="announcement",
        requested_by="overview-actor",
        status=GenerationJobStatus.RUNNING,
        idempotency_key="overview-running",
        active_key="overview-running",
    )

    summary = _summary(db, [game], [team], jobs=[job])

    assert summary.generation_schedule_state == "running"
    assert summary.contribution_status == "creating"
    assert summary.contribution_label == "Wird erstellt"


def test_overview_marks_unavailable_scheduler_calculation(monkeypatch, db):
    _, team, game = _game_setup(db)

    def fail_schedule(*args, **kwargs):
        raise ValueError("keine passende Regel")

    monkeypatch.setattr(
        "app.games.overview.automatic_generation_candidates",
        fail_schedule,
    )
    summary = _summary(db, [game], [team])

    assert summary.generation_schedule_state == "unavailable"
    assert summary.next_generation_label == ("Automatisierungszeit derzeit nicht bestimmbar")
    assert summary.action_required is False
