import struct
from datetime import datetime, timedelta, timezone

import pytest
from bs4 import BeautifulSoup

from app.config import Settings
from app.games.automatic import (
    _next_interval,
    _observe_results,
    claim_due_team,
    plan_generation_jobs,
)
from app.games.provider import FussballDeProvider, ProviderError, _validated_digit_cmap
from app.models import (
    FussballSyncState,
    Game,
    GenerationJob,
    InstagramPage,
    ProviderSnapshot,
    Role,
    StoryRule,
    Team,
    User,
)
from app.posts.service import feed_time, story_time


def _digit_font() -> bytes:
    segment_count = 2
    delta = ((1 - 0xE650 + 0x8000) % 0x10000) - 0x8000
    subtable = struct.pack(
        ">HHHHHHH",
        4,
        32,
        0,
        segment_count * 2,
        4,
        1,
        0,
    )
    subtable += struct.pack(">HH", 0xE65A, 0xFFFF)
    subtable += struct.pack(">H", 0)
    subtable += struct.pack(">HH", 0xE650, 0xFFFF)
    subtable += struct.pack(">hh", delta, 1)
    subtable += struct.pack(">HH", 0, 0)
    cmap = struct.pack(">HHHHI", 0, 1, 3, 1, 12) + subtable
    maxp = struct.pack(">IH", 0x00010000, 12)
    directory_size = 12 + 2 * 16
    maxp_offset = directory_size
    cmap_offset = maxp_offset + len(maxp)
    header = struct.pack(">IHHHH", 0x00010000, 2, 0, 0, 0)
    records = struct.pack(">4sIII", b"maxp", 0, maxp_offset, len(maxp))
    records += struct.pack(">4sIII", b"cmap", 0, cmap_offset, len(cmap))
    return header + records + maxp + cmap


def _base(db, now):
    page = InstagramPage(
        internal_name="ig",
        display_name="IG",
        username="club",
        club="SV",
        active=True,
    )
    db.add(page)
    db.flush()
    team = Team(
        internal_name="one",
        display_name="SV Ehlen I",
        short_name="SVE",
        slug="sve-one",
        club="SV Ehlen",
        fussball_url="https://www.fussball.de/mannschaft/test",
        instagram_page_id=page.id,
        media_subdir="one",
        rules={
            "automatic_sync_enabled": True,
            "automatic_generation_enabled": True,
            "announcement_enabled": True,
            "feed_before_minutes": 1440,
            "generation_lead_minutes": 120,
            "result_enabled": True,
            "result_wait_minutes": 120,
        },
    )
    user = User(
        email="admin@example.invalid",
        password_hash="x",
        role=Role.ADMIN,
        all_teams=True,
        active=True,
    )
    db.add_all([team, user])
    db.flush()
    game = Game(
        team_id=team.id,
        provider="fussball.de",
        external_id="MATCH1",
        home_team="SV Ehlen",
        away_team="Gast",
        kickoff=now + timedelta(hours=26),
        competition="Liga",
        status="scheduled",
        source_url="https://www.fussball.de/spiel/x/-/spiel/MATCH1",
        checked_at=now,
        overrides={},
    )
    db.add(game)
    db.commit()
    return team, game


def test_obfuscated_score_uses_validated_digit_cmap(monkeypatch):
    font = _digit_font()
    cmap = _validated_digit_cmap(font)
    assert cmap[0xE650] == 1
    assert cmap[0xE65A] == 11
    provider = FussballDeProvider(decode_obfuscated_results=True)
    monkeypatch.setattr(provider, "_get_bytes", lambda *_args, **_kwargs: font)
    node = BeautifulSoup(
        '<span data-obfuscation="abcd1234">&#xE652;&#xE651;</span>',
        "html.parser",
    ).span
    assert provider._decode_obfuscated_number(node) == 21


def test_real_score_spans_without_direction_classes_are_decoded(monkeypatch):
    provider = FussballDeProvider(decode_obfuscated_results=True)
    monkeypatch.setattr(provider, "_get_bytes", lambda *_args, **_kwargs: _digit_font())
    records = provider.parse(
        """
        <html><head><title>SV Ehlen (Herren)</title></head><body>
        <div id="id-team-matchplan-table"><table><tbody>
        <tr class="row-competition"><td class="column-date">02.08.2026 | 13:15</td>
          <td class="column-team"><a>Kreisliga A</a></td><td>ME | 340583005</td></tr>
        <tr><td class="column-club"><div class="club-name">SV Ehlen</div></td>
          <td class="column-club"><div class="club-name">Gast</div></td>
          <td class="column-score"><a href="/spiel/a/-/spiel/MATCH123">
            <span data-obfuscation="abcd1234">&#xE652;</span>:
            <span data-obfuscation="abcd1234">&#xE651;</span>
          </a></td></tr></tbody></table></div></body></html>
        """
    )
    assert (records[0].home_score, records[0].away_score) == (2, 1)


def test_previous_games_ajax_resource_is_allowlisted():
    html = (
        '<a data-ajax-resource="https://www.fussball.de/ajax.team.prev.games/'
        '-/mode/PAGE/team-id/TEAM1">Letzte Spiele</a>'
    )
    assert FussballDeProvider.ajax_resource(html, "prev") == (
        "https://www.fussball.de/ajax.team.prev.games/-/mode/PAGE/team-id/TEAM1"
    )
    with pytest.raises(ProviderError):
        FussballDeProvider.ajax_resource(
            '<a data-ajax-resource="https://evil.example/ajax.team.prev.games/x">x</a>',
            "prev",
        )


def test_obfuscated_score_rejects_unexpected_font():
    with pytest.raises(ProviderError):
        _validated_digit_cmap(b"not-a-font")


def test_sync_claim_has_persistent_lease(db):
    now = datetime.now(timezone.utc)
    team, _ = _base(db, now)
    settings = Settings(fussball_sync_batch_size=1, fussball_sync_lease_seconds=60)
    assert claim_due_team(db, settings, worker_id="one", now=now) == team.id
    assert claim_due_team(db, settings, worker_id="two", now=now) is None
    state = db.get(FussballSyncState, team.id)
    assert state.status == "running"
    assert state.lease_owner == "one"


def test_announcement_is_queued_once_and_stays_unapproved(db):
    now = datetime.now(timezone.utc)
    team, _ = _base(db, now)
    settings = Settings(automatic_post_generation_enabled=True)
    assert plan_generation_jobs(db, team, settings, now=now) == 1
    assert plan_generation_jobs(db, team, settings, now=now) == 0
    job = db.query(GenerationJob).one()
    assert job.parameters["trigger_mode"] == "automatic_fussball"
    assert job.status.value == "queued"


def test_announcement_generation_uses_configured_day_lead(db):
    now = datetime.now(timezone.utc)
    team, game = _base(db, now)
    team.rules = {**team.rules, "generation_lead_days": 4}
    game.kickoff = now + timedelta(days=5)
    db.commit()
    settings = Settings(automatic_post_generation_enabled=True)
    assert plan_generation_jobs(db, team, settings, now=now) == 0
    assert plan_generation_jobs(db, team, settings, now=now + timedelta(days=1)) == 1


def test_result_is_queued_immediately_after_confirmation(db):
    now = datetime.now(timezone.utc)
    team, game = _base(db, now)
    team.rules = {**team.rules, "generation_lead_days": 4}
    game.kickoff = now - timedelta(hours=3)
    game.result_confirmed = True
    game.status = "finished"
    game.overrides = {"result_detected_at": now.isoformat()}
    db.commit()
    settings = Settings(automatic_post_generation_enabled=True)
    assert plan_generation_jobs(db, team, settings, now=now) == 1
    job = db.query(GenerationJob).one()
    assert job.post_type == "result"


def test_matchday_uses_team_result_poll_interval_and_normal_days_are_daily(db):
    now = datetime(2026, 8, 9, 8, 0, tzinfo=timezone.utc)
    team, game = _base(db, now)
    team.rules = {
        **team.rules,
        "sync_interval_hours": 24,
        "result_poll_interval_minutes": 15,
    }
    game.kickoff = datetime(2026, 8, 9, 13, 0, tzinfo=timezone.utc)
    db.commit()
    assert _next_interval(db, team.id, Settings(), now) == 15 * 60
    assert _next_interval(db, team.id, Settings(), now - timedelta(days=2)) == 24 * 3600


def test_weekday_fixed_feed_and_story_times_use_berlin_match_date(db):
    now = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
    team, game = _base(db, now)
    game.kickoff = datetime(2026, 8, 9, 13, 0, tzinfo=timezone.utc)
    team.rules = {
        **team.rules,
        "announcement_timing_mode": "weekday_fixed",
        "announcement_weekday_times": {"6": "09:00"},
        "announcement_weekday_targets": {"6": "4"},
    }
    story = StoryRule(
        team_id=team.id,
        name="Sonntag fest",
        post_type="announcement",
        reference="kickoff",
        direction="before",
        offset_minutes=0,
        timing_mode="weekday_fixed",
        weekday_times={"6": "10:30"},
        template="default-story",
    )
    db.add(story)
    db.commit()
    feed_at, absolute = feed_time(team, game, "announcement")
    assert absolute is True
    assert feed_at == datetime(2026, 8, 7, 7, 0, tzinfo=timezone.utc)
    assert story_time(story, game) == datetime(2026, 8, 9, 8, 30, tzinfo=timezone.utc)


def test_weekday_mapping_uses_previous_day_for_announcement_and_next_for_result(db):
    now = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
    team, game = _base(db, now)
    game.kickoff = datetime(2026, 8, 7, 17, 0, tzinfo=timezone.utc)
    game.checked_at = datetime(2026, 8, 7, 20, 0, tzinfo=timezone.utc)
    game.overrides = {"result_detected_at": "2026-08-07T20:00:00+00:00"}
    team.rules = {
        **team.rules,
        "announcement_timing_mode": "weekday_fixed",
        "announcement_weekday_times": {"4": "15:00"},
        "announcement_weekday_targets": {"4": "3"},
        "result_timing_mode": "weekday_fixed",
        "result_wait_minutes": 0,
        "result_weekday_times": {"4": "10:00"},
        "result_weekday_targets": {"4": "5"},
    }
    db.commit()
    announcement_at, announcement_absolute = feed_time(
        team, game, "announcement"
    )
    result_at, result_absolute = feed_time(team, game, "result")
    assert announcement_absolute is True
    assert announcement_at == datetime(2026, 8, 6, 13, 0, tzinfo=timezone.utc)
    assert result_absolute is True
    assert result_at == datetime(2026, 8, 8, 8, 0, tzinfo=timezone.utc)


def test_automatic_approval_choice_is_frozen_into_generation_job(db):
    now = datetime.now(timezone.utc)
    team, _ = _base(db, now)
    team.rules = {
        **team.rules,
        "generation_lead_days": 4,
        "auto_approve_announcements": True,
    }
    db.commit()
    settings = Settings(automatic_post_generation_enabled=True)
    assert plan_generation_jobs(db, team, settings, now=now) == 1
    job = db.query(GenerationJob).one()
    assert job.parameters["automatic_approval_requested"] is True


def test_result_requires_two_stable_observations(db):
    now = datetime.now(timezone.utc)
    team, game = _base(db, now)
    game.kickoff = now - timedelta(hours=3)
    db.commit()
    settings = Settings(
        fussball_result_min_age_minutes=120,
        fussball_result_stability_seconds=600,
    )

    def snapshot(at, snapshot_id):
        item = ProviderSnapshot(
            id=snapshot_id,
            team_id=team.id,
            source_url=team.fussball_url,
            fetched_at=at,
            status_code=200,
            checksum=snapshot_id.ljust(64, "0")[:64],
            relative_path=f"{snapshot_id}.html",
            parser_result={
                "games": [
                    {
                        "external_id": game.external_id,
                        "home_score": 2,
                        "away_score": 1,
                        "warnings": [],
                    }
                ]
            },
        )
        db.add(item)
        db.commit()
        return item

    first = snapshot(now, "snap1")
    assert _observe_results(db, team, first, settings) == 0
    assert game.result_confirmed is False
    second = snapshot(now + timedelta(seconds=601), "snap2")
    assert _observe_results(db, team, second, settings) == 1
    db.refresh(game)
    assert game.result_confirmed is True
    assert game.status == "finished"
    assert game.overrides["result_confirmation_source"] == "fussball.de_stable"


def test_historical_result_is_not_automatically_confirmed(db):
    now = datetime.now(timezone.utc)
    team, game = _base(db, now)
    game.kickoff = now - timedelta(days=7)
    db.commit()
    snapshot = ProviderSnapshot(
        team_id=team.id,
        source_url=team.fussball_url,
        fetched_at=now,
        status_code=200,
        checksum="a" * 64,
        relative_path="historical.html",
        parser_result={
            "games": [
                {
                    "external_id": game.external_id,
                    "home_score": 4,
                    "away_score": 0,
                    "warnings": [],
                }
            ]
        },
    )
    db.add(snapshot)
    db.commit()
    settings = Settings(fussball_result_max_age_hours=48)
    assert _observe_results(db, team, snapshot, settings) == 0
    db.refresh(game)
    assert game.result_confirmed is False
    assert "provider_score_candidate" not in game.overrides
