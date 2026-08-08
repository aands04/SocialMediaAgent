"""Safe recommended automation presets and read-only schedule previews.

The functions in this module deliberately contain no HTTP or persistence
side-effects.  Production publishing and the dashboard preview both use
``calculate_publication_time`` so that the preview cannot drift into a second
scheduling implementation.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from app.models import Game, Team
from app.posts.rules import calculate_publication_time

WEEKDAYS_DE = (
    "Montag",
    "Dienstag",
    "Mittwoch",
    "Donnerstag",
    "Freitag",
    "Samstag",
    "Sonntag",
)

RESULT_POLL_MINUTES_MIN = 10
RESULT_POLL_MINUTES_RECOMMENDED = 15


def automatic_rule_label(
    *,
    post_type: str,
    media_kind: str,
    timing_model: str,
    match_weekday: int | None,
    target_weekday: int | None,
    local_time: str | None,
    offset_minutes: int,
) -> str:
    kind = {
        "announcement": "Spielankündigung",
        "reminder": "Spielerinnerung",
        "result": "Ergebnis",
    }.get(post_type, "Beitrag")
    medium = "Feed" if media_kind == "feed" else "Story"
    if timing_model == "weekday_fixed" and match_weekday is not None:
        target = (
            WEEKDAYS_DE[target_weekday]
            if target_weekday is not None and target_weekday in range(7)
            else "Zieltag"
        )
        return (
            f"{WEEKDAYS_DE[match_weekday]}sspiel · {medium} · "
            f"{target} {local_time or '--:--'}"
        )
    if timing_model == "result_detected":
        suffix = (
            "Direkt nach Ergebnis"
            if not offset_minutes
            else f"{offset_minutes} Minuten nach Ergebnis"
        )
        return f"{kind} · {medium} · {suffix}"
    if timing_model == "manual":
        return f"{kind} · {medium} · Manuelle Planung"
    return f"{kind} · {medium} · {offset_minutes} Minuten zum Anpfiff"


@dataclass(frozen=True)
class RecommendedAutomationPreset:
    """One centrally versioned, reviewable recommendation."""

    key: str
    version: int
    title: str
    description: str
    active: bool
    values: dict[str, Any]
    slots: tuple[dict[str, Any], ...]


def _slot(
    key: str,
    *,
    post_type: str,
    media_kind: str,
    variant: int,
    match_weekday: int | None = None,
    target_weekday: int | None = None,
    local_time: str | None = None,
    timing_model: str = "weekday_fixed",
    reference: str = "kickoff",
    direction: str = "before",
    offset_minutes: int = 0,
    sort_order: int = 0,
) -> dict[str, Any]:
    label = automatic_rule_label(
        post_type=post_type,
        media_kind=media_kind,
        timing_model=timing_model,
        match_weekday=match_weekday,
        target_weekday=target_weekday,
        local_time=local_time,
        offset_minutes=offset_minutes,
    )
    return {
        "slot_key": key,
        "post_type": post_type,
        "label": label,
        "media_kind": media_kind,
        "variant_number": variant,
        "timing_model": timing_model,
        "reference": reference,
        "direction": direction,
        "offset_minutes": offset_minutes,
        "match_weekday": match_weekday,
        "target_weekday": target_weekday,
        "local_time": local_time,
        "timezone": "Europe/Berlin",
        "sort_order": sort_order,
        "instagram_page_id": None,
        "template": None,
        "reuse_media": False,
    }


RECOMMENDED_AUTOMATION_PRESET = RecommendedAutomationPreset(
    key="safe-club-automation",
    version=1,
    title="Empfohlene Grundeinstellung",
    description=(
        "Sichere Automatik für Wochenendspiele mit manueller Prüfung vor jeder "
        "Veröffentlichung. Für andere Spieltage werden Inhalte erstellt und anschließend "
        "bewusst manuell geplant."
    ),
    active=True,
    values={
        "announcement_enabled": True,
        "announcement_feed_generation_count": 2,
        "announcement_feed_publish_count": 1,
        "announcement_story_generation_count": 4,
        "announcement_story_publish_count": 2,
        "announcement_feed_output_count": 2,
        "announcement_story_output_count": 4,
        "reminder_enabled": False,
        "reminder_feed_generation_count": 0,
        "reminder_feed_publish_count": 0,
        "reminder_story_generation_count": 0,
        "reminder_story_publish_count": 0,
        "reminder_feed_output_count": 0,
        "reminder_story_output_count": 0,
        "result_enabled": True,
        "result_timing_mode": "result_detected",
        "result_wait_minutes": 0,
        "result_feed_generation_count": 1,
        "result_feed_publish_count": 1,
        "result_story_generation_count": 1,
        "result_story_publish_count": 1,
        "result_feed_output_count": 1,
        "result_story_output_count": 1,
        "allow_provisional_games": True,
        "automatic_sync_enabled": True,
        "automatic_generation_enabled": True,
        "generation_lead_days": 4,
        "sync_interval_hours": 24,
        "result_poll_interval_minutes": RESULT_POLL_MINUTES_RECOMMENDED,
        "auto_approve_announcements": False,
        "auto_approve_results": False,
        "late_approval": "manual",
        "club_matchday_feed_mode": "announcements_and_results",
    },
    slots=(
        _slot(
            "preset-v1:announcement:feed:saturday",
            post_type="announcement",
            media_kind="feed",
            variant=1,
            match_weekday=5,
            target_weekday=3,
            local_time="18:00",
            sort_order=10,
        ),
        _slot(
            "preset-v1:announcement:story:saturday:1",
            post_type="announcement",
            media_kind="story",
            variant=1,
            match_weekday=5,
            target_weekday=4,
            local_time="18:00",
            sort_order=20,
        ),
        _slot(
            "preset-v1:announcement:story:saturday:2",
            post_type="announcement",
            media_kind="story",
            variant=2,
            match_weekday=5,
            target_weekday=5,
            local_time="10:00",
            sort_order=30,
        ),
        _slot(
            "preset-v1:announcement:feed:sunday",
            post_type="announcement",
            media_kind="feed",
            variant=1,
            match_weekday=6,
            target_weekday=4,
            local_time="18:00",
            sort_order=40,
        ),
        _slot(
            "preset-v1:announcement:story:sunday:1",
            post_type="announcement",
            media_kind="story",
            variant=1,
            match_weekday=6,
            target_weekday=5,
            local_time="18:00",
            sort_order=50,
        ),
        _slot(
            "preset-v1:announcement:story:sunday:2",
            post_type="announcement",
            media_kind="story",
            variant=2,
            match_weekday=6,
            target_weekday=6,
            local_time="10:00",
            sort_order=60,
        ),
        _slot(
            "preset-v1:result:feed:any",
            post_type="result",
            media_kind="feed",
            variant=1,
            timing_model="result_detected",
            reference="result_detected",
            direction="after",
            sort_order=70,
        ),
        _slot(
            "preset-v1:result:story:any",
            post_type="result",
            media_kind="story",
            variant=1,
            timing_model="result_detected",
            reference="result_detected",
            direction="after",
            sort_order=80,
        ),
    ),
)


def _slot_signature(slot: dict[str, Any]) -> tuple[Any, ...]:
    return (
        slot.get("post_type"),
        slot.get("media_kind"),
        int(slot.get("variant_number") or 1),
        slot.get("timing_model"),
        slot.get("reference"),
        slot.get("direction"),
        int(slot.get("offset_minutes") or 0),
        slot.get("match_weekday"),
        slot.get("target_weekday"),
        slot.get("local_time"),
    )


def apply_recommended_preset(
    current: dict[str, Any] | None,
    *,
    mode: str,
    timezone_name: str = "Europe/Berlin",
) -> tuple[dict[str, Any], dict[str, int]]:
    """Return new settings without mutating the existing dictionary."""
    if mode not in {"append_missing", "replace"}:
        raise ValueError("Unbekannter Übernahmemodus")
    old = deepcopy(current or {})
    protected = {
        key: value
        for key, value in old.items()
        if key.startswith(("image_prompt", "text_prompt")) or key == "style_direction"
    }
    existing_slots = [
        deepcopy(row)
        for row in old.get("publication_rule_slots", [])
        if isinstance(row, dict)
    ]
    preset_slots = [deepcopy(row) for row in RECOMMENDED_AUTOMATION_PRESET.slots]
    for row in preset_slots:
        row["timezone"] = timezone_name
    if mode == "replace":
        result = {**old, **deepcopy(RECOMMENDED_AUTOMATION_PRESET.values)}
        result["publication_rule_slots"] = preset_slots
        report = {"settings_added": len(RECOMMENDED_AUTOMATION_PRESET.values), "slots_added": len(preset_slots), "slots_replaced": len(existing_slots)}
    else:
        result = deepcopy(old)
        added_settings = 0
        for key, value in RECOMMENDED_AUTOMATION_PRESET.values.items():
            if key not in result:
                result[key] = deepcopy(value)
                added_settings += 1
        signatures = {_slot_signature(row) for row in existing_slots}
        added_slots = [row for row in preset_slots if _slot_signature(row) not in signatures]
        result["publication_rule_slots"] = existing_slots + added_slots
        report = {"settings_added": added_settings, "slots_added": len(added_slots), "slots_replaced": 0}
    result.update(protected)
    result["publication_rule_slots_configured"] = True
    result["recommended_preset"] = {
        "key": RECOMMENDED_AUTOMATION_PRESET.key,
        "version": RECOMMENDED_AUTOMATION_PRESET.version,
        "mode": mode,
    }
    return result, report


def _preview_slot(raw: dict[str, Any], timezone_name: str) -> SimpleNamespace:
    values = deepcopy(raw)
    values["timezone"] = values.get("timezone") or timezone_name
    return SimpleNamespace(**values)


def build_schedule_preview(
    team: Team,
    slots: list[dict[str, Any]],
    *,
    kickoff: datetime,
    result_detected_at: datetime | None = None,
) -> dict[str, Any]:
    """Calculate a read-only timeline using the production scheduler function."""
    zone = ZoneInfo(team.timezone or "Europe/Berlin")
    kickoff = kickoff if kickoff.tzinfo else kickoff.replace(tzinfo=zone)
    kickoff_utc = kickoff.astimezone(timezone.utc)
    result_utc = (
        (result_detected_at if result_detected_at.tzinfo else result_detected_at.replace(tzinfo=zone)).astimezone(timezone.utc)
        if result_detected_at
        else None
    )
    preview_game = Game(
        club_id=team.club_id,
        team_id=team.id,
        external_id="schedule-preview",
        provider="preview",
        home_team=team.display_name,
        away_team="Beispielgegner",
        kickoff=kickoff_utc,
        status="scheduled",
        source_url="",
        overrides={},
    )
    match_weekday = kickoff.astimezone(zone).weekday()
    lead_days = max(0, int((team.rules or {}).get("generation_lead_days", 4)))
    generated_at = kickoff_utc - timedelta(days=lead_days)
    events: list[dict[str, Any]] = [
        {"kind": "generation", "at": generated_at, "title": "Beitrag wird vorbereitet", "description": generation_summary(team.rules or {}, "announcement")},
        {"kind": "kickoff", "at": kickoff_utc, "title": "Anpfiff", "description": "Beispielspiel"},
    ]
    warnings: list[str] = []
    rules = team.rules or {}

    def post_type_enabled(post_type: str) -> bool:
        setting = {
            "announcement": "announcement_enabled",
            "reminder": "reminder_enabled",
            "result": "result_enabled",
        }.get(post_type)
        return bool(setting and rules.get(setting, False))

    matched = [
        row
        for row in slots
        if row.get("match_weekday") in {None, match_weekday}
        and post_type_enabled(str(row.get("post_type") or "announcement"))
    ]
    announcement_scheduled = any(
        row.get("post_type") == "announcement" for row in matched
    )
    if rules.get("announcement_enabled") and not announcement_scheduled:
        warnings.append(
            f"Für {WEEKDAYS_DE[match_weekday]}sspiele gibt es aktuell keinen automatischen Veröffentlichungsplan. Die Inhalte werden trotzdem erstellt und warten auf die manuelle Planung."
        )
    seen_times: dict[tuple[datetime, str], int] = {}
    for raw in matched:
        post_type = str(raw.get("post_type") or "announcement")
        generated = int(
            rules.get(f"{post_type}_{raw.get('media_kind')}_generation_count", 1)
        )
        variant = int(raw.get("variant_number") or 1)
        if variant > generated:
            warnings.append(
                f"Für {raw.get('label') or 'eine Regel'} ist Variante {variant} vorgesehen, es werden aber nur {generated} Varianten erstellt."
            )
        if post_type == "result" and result_utc is None:
            events.append(
                {"kind": "result", "at": None, "title": raw.get("label") or "Ergebnismeldung", "description": "Nach bestätigtem Endergebnis · wartet auf Ergebnis und Freigabe"}
            )
            continue
        slot = _preview_slot(raw, team.timezone)
        calculated = calculate_publication_time(slot, preview_game, result_detected_at=result_utc)
        if calculated is None:
            continue
        if post_type != "result" and calculated < generated_at:
            warnings.append(
                f"{raw.get('label') or 'Eine Veröffentlichung'} liegt vor dem Zeitpunkt, zu dem die Inhalte automatisch erstellt werden."
            )
        collision_key = (calculated, str(raw.get("media_kind") or "feed"))
        seen_times[collision_key] = seen_times.get(collision_key, 0) + 1
        events.append(
            {
                "kind": raw.get("media_kind") or "feed",
                "at": calculated,
                "title": raw.get("label") or "Veröffentlichung",
                "description": f"{medium_label(str(raw.get('media_kind')))} · Variante {variant} · {approval_label(team.rules or {}, post_type)}",
            }
        )
    if any(count > 1 for count in seen_times.values()):
        warnings.append("Zwei Veröffentlichungen sind für denselben Zeitpunkt vorgesehen. Prüfe, ob dies beabsichtigt ist.")
    poll = int(
        rules.get(
            "result_poll_interval_minutes", RESULT_POLL_MINUTES_RECOMMENDED
        )
    )
    if poll < RESULT_POLL_MINUTES_MIN:
        warnings.append("Die Ergebnisprüfung ist ungültig. Das Mindestintervall beträgt 10 Minuten.")
    events.sort(key=lambda event: event["at"] or datetime.max.replace(tzinfo=timezone.utc))
    formatted = []
    for event in events:
        item = dict(event)
        item["at_iso"] = event["at"].isoformat() if event["at"] else None
        if event["at"]:
            local_event = event["at"].astimezone(zone)
            item["when"] = (
                f"{WEEKDAYS_DE[local_event.weekday()]}, "
                f"{local_event.strftime('%d.%m.%Y · %H:%M Uhr')}"
            )
        else:
            item["when"] = "Nach bestätigtem Endergebnis"
        item.pop("at", None)
        formatted.append(item)
    return {
        "weekday": WEEKDAYS_DE[match_weekday],
        "events": formatted,
        "warnings": warnings,
        "manual_schedule_required": bool(
            rules.get("announcement_enabled") and not announcement_scheduled
        ),
        "generation": generation_summary(rules, "announcement"),
    }


def medium_label(value: str) -> str:
    return "Feed-Beitrag" if value == "feed" else "Story"


def approval_label(rules: dict[str, Any], post_type: str) -> str:
    automatic = rules.get("auto_approve_results" if post_type == "result" else "auto_approve_announcements")
    return "automatisch freigegeben" if automatic else "wartet auf manuelle Freigabe"


def generation_summary(rules: dict[str, Any], post_type: str) -> str:
    feed = int(rules.get(f"{post_type}_feed_generation_count", rules.get(f"{post_type}_feed_output_count", 1)))
    stories = int(rules.get(f"{post_type}_story_generation_count", rules.get(f"{post_type}_story_output_count", 1)))
    return f"{feed} Feed-Variante{'n' if feed != 1 else ''} · {stories} Story-Variante{'n' if stories != 1 else ''}"


def selection_summary(rules: dict[str, Any], post_type: str) -> str:
    feed = int(rules.get(f"{post_type}_feed_publish_count", 1))
    stories = int(rules.get(f"{post_type}_story_publish_count", 1))
    return f"{feed} Feed · {stories} Story{'s' if stories != 1 else ''}"
