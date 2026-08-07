"""Structured, inheritable generation and publication rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ContentRuleSet, Game, PublicationRuleSlot, StoryRule, Team


@dataclass(frozen=True)
class RuleResolution:
    rule_set: ContentRuleSet | None
    slots: tuple[PublicationRuleSlot, ...]
    source: str
    manual_schedule_required: bool


def _scope_key(scope_type: str, scope_id: str | None) -> str:
    return "club" if scope_type == "club" else f"{scope_type}:{scope_id}"


def resolve_rule_set(
    db: Session,
    *,
    club_id: str,
    post_type: str,
    team_id: str | None = None,
    game_id: str | None = None,
) -> ContentRuleSet | None:
    """Resolve game > team > club without ever crossing the tenant boundary."""
    candidates = (("game", game_id), ("team", team_id), ("club", None))
    for scope_type, scope_id in candidates:
        if scope_type != "club" and not scope_id:
            continue
        item = db.scalar(
            select(ContentRuleSet)
            .where(
                ContentRuleSet.club_id == club_id,
                ContentRuleSet.scope_type == scope_type,
                ContentRuleSet.scope_key == _scope_key(scope_type, scope_id),
                ContentRuleSet.post_type == post_type,
                ContentRuleSet.active.is_(True),
                ContentRuleSet.archived_at.is_(None),
            )
            .order_by(ContentRuleSet.rule_version.desc())
        )
        if item:
            return item
    return None


def resolve_publication_slots(
    db: Session,
    *,
    club_id: str,
    post_type: str,
    match_weekday: int,
    team_id: str | None = None,
    game_id: str | None = None,
) -> RuleResolution:
    rule_set = resolve_rule_set(
        db,
        club_id=club_id,
        post_type=post_type,
        team_id=team_id,
        game_id=game_id,
    )
    if not rule_set:
        return RuleResolution(None, (), "none", True)
    all_slots = list(
        db.scalars(
            select(PublicationRuleSlot)
            .where(
                PublicationRuleSlot.club_id == club_id,
                PublicationRuleSlot.rule_set_id == rule_set.id,
                PublicationRuleSlot.active.is_(True),
            )
            .order_by(PublicationRuleSlot.sort_order, PublicationRuleSlot.id)
        )
    )
    matched = tuple(
        slot
        for slot in all_slots
        if slot.match_weekday is None or slot.match_weekday == match_weekday
    )
    has_weekday_rules = any(slot.match_weekday is not None for slot in all_slots)
    has_matching_weekday = any(
        slot.match_weekday == match_weekday
        for slot in all_slots
    )
    source = f"{rule_set.scope_type}:v{rule_set.rule_version}"
    return RuleResolution(
        rule_set,
        matched,
        source,
        bool(not matched and (not all_slots or (has_weekday_rules and not has_matching_weekday))),
    )


def calculate_publication_time(
    slot: PublicationRuleSlot,
    game: Game,
    *,
    result_detected_at: datetime | None = None,
    approval_at: datetime | None = None,
) -> datetime | None:
    """Return no time instead of inventing a fallback for an unmatched rule."""
    kickoff = game.kickoff
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=timezone.utc)
    zone = ZoneInfo(slot.timezone or "Europe/Berlin")
    local_kickoff = kickoff.astimezone(zone)
    if slot.timing_model == "manual":
        return None
    if slot.timing_model == "result_detected":
        if result_detected_at is None:
            return None
        amount = timedelta(minutes=int(slot.offset_minutes or 0))
        return result_detected_at + (amount if slot.direction != "before" else -amount)
    if slot.timing_model == "relative":
        bases = {
            "kickoff": kickoff,
            "planned_end": kickoff + timedelta(minutes=120),
            "result_detected": result_detected_at,
            "approval": approval_at,
        }
        base = bases.get(slot.reference or "kickoff")
        if base is None:
            return None
        amount = timedelta(minutes=int(slot.offset_minutes or 0))
        return base + (amount if slot.direction == "after" else -amount)
    if slot.timing_model == "weekday_fixed":
        if slot.match_weekday != local_kickoff.weekday() or slot.target_weekday is None or not slot.local_time:
            return None
        hour, minute = (int(value) for value in slot.local_time.split(":"))
        if slot.reference == "result_detected":
            offset = (slot.target_weekday - local_kickoff.weekday()) % 7
        else:
            offset = -((local_kickoff.weekday() - slot.target_weekday) % 7)
        return (
            (local_kickoff + timedelta(days=offset))
            .replace(hour=hour, minute=minute, second=0, microsecond=0)
            .astimezone(timezone.utc)
        )
    return None


def sync_team_rule_sets(db: Session, team: Team) -> list[ContentRuleSet]:
    """Version the current legacy UI values into the structured rule model."""
    result = []
    rules = team.rules or {}
    story_rules = list(
        db.scalars(
            select(StoryRule).where(
                StoryRule.club_id == team.club_id,
                StoryRule.team_id == team.id,
                StoryRule.active.is_(True),
            )
        )
    )
    for post_type in ("announcement", "reminder", "result"):
        previous = db.scalar(
            select(ContentRuleSet)
            .where(
                ContentRuleSet.club_id == team.club_id,
                ContentRuleSet.scope_key == f"team:{team.id}",
                ContentRuleSet.post_type == post_type,
                ContentRuleSet.active.is_(True),
            )
            .order_by(ContentRuleSet.rule_version.desc())
            .with_for_update()
        )
        next_version = int(
            db.scalar(
                select(func.max(ContentRuleSet.rule_version)).where(
                    ContentRuleSet.club_id == team.club_id,
                    ContentRuleSet.scope_key == f"team:{team.id}",
                    ContentRuleSet.post_type == post_type,
                )
            )
            or 0
        ) + 1
        if previous:
            previous.active = False
            previous.archived_at = datetime.now(timezone.utc)
        feed_count = max(
            0,
            min(
                10,
                int(
                    rules.get(
                        f"{post_type}_feed_generation_count",
                        rules.get(f"{post_type}_feed_output_count", 1),
                    )
                ),
            ),
        )
        story_count = max(
            0,
            min(
                10,
                int(
                    rules.get(
                        f"{post_type}_story_generation_count",
                        rules.get(f"{post_type}_story_output_count", 1),
                    )
                ),
            ),
        )
        feed_publish_count = max(
            0,
            min(
                feed_count,
                int(rules.get(f"{post_type}_feed_publish_count", feed_count)),
            ),
        )
        story_publish_count = max(
            0,
            min(
                story_count,
                int(rules.get(f"{post_type}_story_publish_count", story_count)),
            ),
        )
        item = ContentRuleSet(
            club_id=team.club_id,
            scope_type="team",
            scope_key=f"team:{team.id}",
            team_id=team.id,
            post_type=post_type,
            rule_version=next_version,
            active=True,
            feed_generation_count=feed_count,
            story_generation_count=story_count,
            feed_publish_variants=list(range(1, feed_publish_count + 1)),
            story_publish_variants=list(range(1, story_publish_count + 1)),
            approval_policy=(
                "automatic"
                if rules.get(
                    "auto_approve_results"
                    if post_type == "result"
                    else "auto_approve_announcements"
                )
                else "manual"
            ),
            inherited_from_id=previous.id if previous else None,
        )
        db.add(item)
        db.flush()
        canonical_slots = rules.get("publication_rule_slots")
        canonical_configured = bool(rules.get("publication_rule_slots_configured"))
        if isinstance(canonical_slots, list) and (canonical_slots or canonical_configured):
            for slot_index, raw in enumerate(canonical_slots, start=1):
                if not isinstance(raw, dict) or raw.get("post_type") != post_type:
                    continue
                media_kind = raw.get("media_kind")
                timing_model = raw.get("timing_model")
                if media_kind not in {"feed", "story"} or timing_model not in {
                    "relative",
                    "weekday_fixed",
                    "result_detected",
                    "manual",
                }:
                    continue
                slot_key = str(raw.get("slot_key") or "").strip()[:100]
                if not slot_key:
                    slot_key = f"{post_type}:{media_kind}:slot-{slot_index}"
                match_weekday = raw.get("match_weekday")
                target_weekday = raw.get("target_weekday")
                match_weekday = int(match_weekday) if match_weekday not in {None, ""} else None
                target_weekday = (
                    int(target_weekday) if target_weekday not in {None, ""} else None
                )
                if match_weekday is not None and match_weekday not in range(7):
                    continue
                if target_weekday is not None and target_weekday not in range(7):
                    continue
                db.add(
                    PublicationRuleSlot(
                        club_id=team.club_id,
                        rule_set_id=item.id,
                        slot_key=slot_key,
                        label=str(raw.get("label") or "Veröffentlichung")[:160],
                        media_kind=media_kind,
                        variant_number=max(1, int(raw.get("variant_number") or 1)),
                        timing_model=timing_model,
                        reference=raw.get("reference"),
                        direction=raw.get("direction"),
                        offset_minutes=raw.get("offset_minutes"),
                        match_weekday=match_weekday,
                        target_weekday=target_weekday,
                        local_time=raw.get("local_time"),
                        timezone=str(raw.get("timezone") or team.timezone),
                        sort_order=int(raw.get("sort_order") or 0),
                        instagram_page_id=raw.get("instagram_page_id"),
                        template=(str(raw.get("template"))[:100] if raw.get("template") else None),
                        reuse_media=bool(raw.get("reuse_media", False)),
                    )
                )
            result.append(item)
            continue
        mode = rules.get(f"{post_type}_timing_mode", "relative")
        weekday_times = rules.get(f"{post_type}_weekday_times") or {}
        weekday_targets = rules.get(f"{post_type}_weekday_targets") or {}
        if feed_count:
            if mode == "weekday_fixed":
                for match_day, local_time in sorted(weekday_times.items()):
                    db.add(
                        PublicationRuleSlot(
                            club_id=team.club_id,
                            rule_set_id=item.id,
                            slot_key=f"feed:weekday:{match_day}",
                            label=f"Feed bei Spiel am Wochentag {match_day}",
                            media_kind="feed",
                            variant_number=1,
                            timing_model="weekday_fixed",
                            reference="result_detected" if post_type == "result" else "kickoff",
                            match_weekday=int(match_day),
                            target_weekday=int(weekday_targets.get(match_day, match_day)),
                            local_time=local_time,
                            timezone=team.timezone,
                            sort_order=int(match_day),
                            instagram_page_id=team.instagram_page_id,
                        )
                    )
            else:
                reference = "result_detected" if mode == "result_detected" else "kickoff"
                db.add(
                    PublicationRuleSlot(
                        club_id=team.club_id,
                        rule_set_id=item.id,
                        slot_key="feed:default",
                        label="Feed-Veröffentlichung",
                        media_kind="feed",
                        variant_number=1,
                        timing_model=mode,
                        reference=reference,
                        direction=rules.get(
                            f"{post_type}_offset_direction",
                            "after" if post_type == "result" else "before",
                        ),
                        offset_minutes=int(
                            rules.get(
                                f"{post_type}_offset_minutes",
                                rules.get("feed_before_minutes", 1440),
                            )
                        ),
                        timezone=team.timezone,
                        instagram_page_id=team.instagram_page_id,
                    )
                )
        for legacy in (row for row in story_rules if row.post_type == post_type):
            entries = (
                sorted((legacy.weekday_times or {}).items())
                if legacy.timing_mode == "weekday_fixed"
                else [(None, None)]
            )
            for match_day, local_time in entries:
                suffix = match_day if match_day is not None else "default"
                db.add(
                    PublicationRuleSlot(
                        club_id=team.club_id,
                        rule_set_id=item.id,
                        slot_key=f"story:{legacy.id}:{suffix}",
                        label=legacy.name,
                        media_kind="story",
                        variant_number=max(1, int(legacy.media_slot or 1)),
                        timing_model=legacy.timing_mode,
                        reference=legacy.reference,
                        direction=legacy.direction,
                        offset_minutes=legacy.offset_minutes,
                        match_weekday=int(match_day) if match_day is not None else None,
                        target_weekday=(
                            int((legacy.weekday_targets or {}).get(match_day, match_day))
                            if match_day is not None
                            else None
                        ),
                        local_time=local_time,
                        timezone=team.timezone,
                        sort_order=legacy.sort_order,
                        instagram_page_id=legacy.instagram_page_id or team.instagram_page_id,
                        template=legacy.template,
                        reuse_media=legacy.reuse_media,
                        legacy_story_rule_id=legacy.id,
                    )
                )
        result.append(item)
    db.flush()
    return result
