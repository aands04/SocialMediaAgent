from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.posts.automation import (
    RECOMMENDED_AUTOMATION_PRESET,
    RESULT_POLL_MINUTES_MIN,
    apply_recommended_preset,
    build_schedule_preview,
)


def team_with_rules(**rules):
    return SimpleNamespace(
        id="team-1",
        club_id="club-1",
        display_name="Testmannschaft",
        timezone="Europe/Berlin",
        rules=rules,
    )


def test_recommended_preset_is_safe_and_complete():
    preset = RECOMMENDED_AUTOMATION_PRESET

    assert preset.active is True
    assert preset.version == 1
    assert preset.values["announcement_enabled"] is True
    assert preset.values["announcement_feed_generation_count"] == 2
    assert preset.values["announcement_story_generation_count"] == 4
    assert preset.values["announcement_feed_publish_count"] == 1
    assert preset.values["announcement_story_publish_count"] == 2
    assert preset.values["result_feed_generation_count"] == 1
    assert preset.values["result_story_generation_count"] == 1
    assert preset.values["result_poll_interval_minutes"] == 15
    assert preset.values["result_poll_interval_minutes"] >= RESULT_POLL_MINUTES_MIN
    assert preset.values["auto_approve_announcements"] is False
    assert preset.values["auto_approve_results"] is False
    assert len(preset.slots) == 8


def test_append_preset_preserves_custom_values_and_adds_only_missing_rules():
    existing_slot = dict(RECOMMENDED_AUTOMATION_PRESET.slots[0])
    current = {
        "announcement_feed_generation_count": 7,
        "text_prompt": "protected-platform-selection",
        "style_direction": "legacy-style-stays-stored",
        "publication_rule_slots": [existing_slot],
    }

    updated, report = apply_recommended_preset(current, mode="append_missing")

    assert updated["announcement_feed_generation_count"] == 7
    assert updated["text_prompt"] == "protected-platform-selection"
    assert updated["style_direction"] == "legacy-style-stays-stored"
    assert len(updated["publication_rule_slots"]) == 8
    assert report["slots_added"] == 7
    assert current["publication_rule_slots"] == [existing_slot]


def test_replace_preset_requires_known_mode_and_preserves_platform_assignments():
    updated, report = apply_recommended_preset(
        {
            "announcement_enabled": False,
            "image_prompt_feed": "protected-image-prompt",
            "publication_rule_slots": [{"slot_key": "custom"}],
        },
        mode="replace",
        timezone_name="Europe/London",
    )

    assert updated["announcement_enabled"] is True
    assert updated["image_prompt_feed"] == "protected-image-prompt"
    assert len(updated["publication_rule_slots"]) == 8
    assert {
        slot["timezone"] for slot in updated["publication_rule_slots"]
    } == {"Europe/London"}
    assert report["slots_replaced"] == 1
    with pytest.raises(ValueError, match="Übernahmemodus"):
        apply_recommended_preset({}, mode="unknown")


def test_preview_uses_production_schedule_for_sunday_and_result_detection():
    team = team_with_rules(**RECOMMENDED_AUTOMATION_PRESET.values)
    berlin = ZoneInfo("Europe/Berlin")

    preview = build_schedule_preview(
        team,
        list(RECOMMENDED_AUTOMATION_PRESET.slots),
        kickoff=datetime(2026, 8, 9, 15, 0, tzinfo=berlin),
        result_detected_at=datetime(2026, 8, 9, 17, 7, tzinfo=berlin),
    )

    event_times = {event["when"] for event in preview["events"]}
    assert "Freitag, 07.08.2026 · 18:00 Uhr" in event_times
    assert "Samstag, 08.08.2026 · 18:00 Uhr" in event_times
    assert "Sonntag, 09.08.2026 · 10:00 Uhr" in event_times
    assert "Sonntag, 09.08.2026 · 17:07 Uhr" in event_times
    assert preview["manual_schedule_required"] is False
    assert preview["warnings"] == []


def test_preview_marks_weekday_without_rule_for_manual_planning():
    team = team_with_rules(**RECOMMENDED_AUTOMATION_PRESET.values)

    preview = build_schedule_preview(
        team,
        list(RECOMMENDED_AUTOMATION_PRESET.slots),
        kickoff=datetime(2026, 8, 12, 19, 0, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert preview["weekday"] == "Mittwoch"
    assert preview["manual_schedule_required"] is True
    assert any("manuelle Planung" in warning for warning in preview["warnings"])


def test_preview_ignores_rules_for_disabled_content_and_warns_about_invalid_setup():
    rules = dict(RECOMMENDED_AUTOMATION_PRESET.values)
    rules.update(
        {
            "announcement_enabled": False,
            "result_enabled": False,
            "result_poll_interval_minutes": 9,
        }
    )
    team = team_with_rules(**rules)

    preview = build_schedule_preview(
        team,
        list(RECOMMENDED_AUTOMATION_PRESET.slots),
        kickoff=datetime(2026, 8, 9, 15, 0, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert not any(event["kind"] in {"feed", "story", "result"} for event in preview["events"])
    assert preview["manual_schedule_required"] is False
    assert any("Mindestintervall" in warning for warning in preview["warnings"])
