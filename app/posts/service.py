from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.branding.compiler import (
    SPONSOR_POSITION_LABELS,
    applicable_sponsors,
    team_display_name,
)
from app.branding.service import STANDARD_FONTS, branding_form_state, branding_snapshot
from app.config import get_settings
from app.games.identity import resolve_team_side, team_aliases
from app.logos.service import LogoCompositor, LogoValidationError, frozen_logo_set
from app.models import (
    ClubBrandingConfiguration,
    DesignTemplate,
    FontAsset,
    Game,
    GeneratedMediaSlot,
    GeneratedMediaVersion,
    InstagramPage,
    JobStatus,
    MediaAsset,
    Post,
    PostStatus,
    PublicationJob,
    PublicationMediaItem,
    PublicationRuleSlot,
    StoryRule,
    Team,
)
from app.posts.rules import calculate_publication_time, resolve_publication_slots
from app.prompts.service import (
    matchday_bundle_prompt,
    prompt_for_variant,
    resolve_prompt,
)
from app.rendering.service import Renderer, builtin_template
from app.textgen.service import GeneratedText, TextGenerator


class RerenderConflict(ValueError):
    pass


@dataclass(frozen=True)
class _SyntheticResultStoryRule:
    """Runtime fallback so every result post always contains one story."""

    id: str = "result-immediate"
    name: str = "Ergebnis – automatisch"
    template: str = "default-story"
    prompt_template: str = "default-image-story"
    instagram_page_id: str | None = None
    text_variant: str | None = None
    post_type: str = "result"
    timing_mode: str = "result_detected"
    reference: str = "result_detected"
    direction: str = "after"
    offset_minutes: int = 0
    fixed_time: str | None = None
    weekday_times: dict | None = None
    weekday_targets: dict | None = None
    next_day: bool = False
    media_slot: int = 1


def _revision_prompt(prompt, instruction: str | None):
    """Append a user-requested change without weakening the frozen safety prompt."""
    if not prompt or not instruction:
        return prompt
    addition = (
        "\n\nZUSÄTZLICHER ÄNDERUNGSAUFTRAG FÜR DIESE NEUE VERSION:\n"
        + instruction.strip()
        + "\nSetze diesen Wunsch nur um, soweit er den oben stehenden Fakten-, "
        "Identitäts-, Logo- und Sicherheitsregeln nicht widerspricht."
    )
    return replace(prompt, rendered=prompt.rendered + addition)


def reserve_image(db: Session, team_id: str, game_id: str) -> MediaAsset | None:
    existing = db.scalar(select(MediaAsset).where(MediaAsset.reserved_game_id == game_id))
    if existing:
        return existing
    asset = db.scalar(
        select(MediaAsset)
        .where(
            MediaAsset.team_id == team_id,
            MediaAsset.active.is_(True),
            MediaAsset.available.is_(True),
            MediaAsset.reserved_game_id.is_(None),
            MediaAsset.uses == 0,
        )
        .order_by(MediaAsset.size.desc(), MediaAsset.filename)
        .with_for_update(skip_locked=True)
    )
    if asset:
        asset.reserved_game_id = game_id
        asset.uses += 1
        db.flush()
    return asset


BERLIN = ZoneInfo("Europe/Berlin")


class ManualScheduleRequired(ValueError):
    """Raised when a rule intentionally has no schedule for the match weekday."""


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _result_detected_at(game: Game) -> datetime | None:
    raw = (game.overrides or {}).get("result_detected_at")
    if raw:
        try:
            return _aware_utc(datetime.fromisoformat(str(raw)))
        except ValueError:
            return None
    if game.result_confirmed and game.checked_at:
        return _aware_utc(game.checked_at)
    return None


def _weekday_time(
    game: Game,
    values: dict | None,
    target_weekdays: dict | None = None,
    occurrence: str = "same",
) -> datetime | None:
    """Resolve a weekday/time mapping relative to the local match date."""
    local_kickoff = _aware_utc(game.kickoff).astimezone(BERLIN)
    match_weekday = local_kickoff.weekday()
    match_key = str(match_weekday)
    configured = (values or {}).get(match_key)
    if not configured:
        return None
    try:
        hour, minute = map(int, configured.split(":"))
    except (AttributeError, TypeError, ValueError):
        return None
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    try:
        target_weekday = int((target_weekdays or {}).get(match_key, match_weekday))
    except (TypeError, ValueError):
        return None
    if not 0 <= target_weekday <= 6:
        return None
    day_offset = 0
    if occurrence == "before":
        day_offset = -((match_weekday - target_weekday) % 7)
    elif occurrence == "after":
        day_offset = (target_weekday - match_weekday) % 7
    return (
        (local_kickoff + timedelta(days=day_offset))
        .replace(hour=hour, minute=minute, second=0, microsecond=0)
        .astimezone(timezone.utc)
    )


def feed_time(team: Team, game: Game, post_type: str) -> tuple[datetime, bool]:
    """Resolve the feed publication time and whether it is an absolute schedule."""
    rules = team.rules or {}
    kickoff = _aware_utc(game.kickoff)
    if post_type == "result":
        detected = (game.overrides or {}).get("result_detected_at")
        detected_at = (
            _aware_utc(datetime.fromisoformat(detected))
            if detected
            else _aware_utc(game.checked_at)
        )
        mode = rules.get("result_timing_mode", "result_detected")
        # "Sofort" is deliberately literal.  The result was already protected
        # by the provider stability checks before result_confirmed was set, so
        # another scheduling delay would make ad-hoc result publishing
        # surprisingly late.
        if mode == "result_detected":
            return detected_at, False
        earliest = detected_at + timedelta(minutes=int(rules.get("result_wait_minutes", 0)))
        if mode == "weekday_fixed":
            target = _weekday_time(
                game,
                rules.get("result_weekday_times"),
                rules.get("result_weekday_targets"),
                "after",
            )
            if target is None:
                raise ManualScheduleRequired(
                    "Für diesen Spiel-Wochentag ist keine Ergebnisveröffentlichung eingerichtet"
                )
            return max(target, earliest), True
        if mode == "relative":
            minutes = int(rules.get("result_offset_minutes", 120))
            direction = rules.get("result_offset_direction", "after")
            target = kickoff + timedelta(minutes=minutes if direction == "after" else -minutes)
            return max(target, earliest), False
        return earliest, False
    if post_type == "reminder":
        if rules.get("reminder_timing_mode", "relative") == "weekday_fixed":
            target = _weekday_time(
                game,
                rules.get("reminder_weekday_times"),
                rules.get("reminder_weekday_targets"),
                "before",
            )
            if target is not None:
                return target, True
            raise ManualScheduleRequired(
                "Für diesen Spiel-Wochentag ist keine Erinnerungsveröffentlichung eingerichtet"
            )
        return kickoff - timedelta(
            minutes=int(rules.get("reminder_feed_before_minutes", 360))
        ), False

    mode = rules.get("announcement_timing_mode", "relative")
    if mode == "weekday_fixed":
        target = _weekday_time(
            game,
            rules.get("announcement_weekday_times"),
            rules.get("announcement_weekday_targets"),
            "before",
        )
        if target is not None:
            return target, True
        raise ManualScheduleRequired(
            "Für diesen Spiel-Wochentag ist keine Ankündigungsveröffentlichung eingerichtet"
        )
    minutes = int(rules.get("announcement_offset_minutes", rules.get("feed_before_minutes", 1440)))
    direction = rules.get("announcement_offset_direction", "before")
    return kickoff + timedelta(minutes=minutes if direction == "after" else -minutes), False


def story_time(rule: StoryRule, game: Game, approved_at: datetime | None = None) -> datetime:
    detected = (game.overrides or {}).get("result_detected_at")
    result_detected = datetime.fromisoformat(detected) if detected else game.checked_at
    refs = {
        "kickoff": game.kickoff,
        "planned_end": game.kickoff + timedelta(minutes=120),
        "result_detected": result_detected,
        "approval": approved_at,
    }
    if getattr(rule, "timing_mode", "relative") == "weekday_fixed":
        configured = _weekday_time(
            game,
            getattr(rule, "weekday_times", None),
            getattr(rule, "weekday_targets", None),
            "after" if rule.post_type == "result" else "before",
        )
        if configured is not None:
            if rule.post_type == "result":
                detected_at = _aware_utc(result_detected)
                return max(configured, detected_at)
            return configured
        raise ManualScheduleRequired(
            "Für diesen Spiel-Wochentag ist kein Story-Zeitpunkt eingerichtet"
        )
    base = refs.get(rule.reference) or game.checked_at
    delta = timedelta(minutes=rule.offset_minutes) * (1 if rule.direction == "after" else -1)
    result = base + delta
    if rule.next_day:
        result += timedelta(days=1)
    if rule.fixed_time:
        h, m = map(int, rule.fixed_time.split(":"))
        result = result.replace(hour=h, minute=m, second=0, microsecond=0)
    return result


def _effective_story_rules(
    team: Team,
    rules: list[StoryRule],
    post_type: str,
) -> tuple[list[tuple[StoryRule, int]], int]:
    """Resolve Story output slots while retaining legacy rule semantics.

    Before media slots existed, every active Story rule rendered its own image.
    Teams that have not saved an explicit output count therefore keep that
    behaviour, even when the database default on older rows is ``1``.
    """
    settings = team.rules or {}
    generation_key = f"{post_type}_story_generation_count"
    publish_key = f"{post_type}_story_publish_count"
    count_key = f"{post_type}_story_output_count"
    if generation_key in settings:
        generated = max(0, min(10, int(settings.get(generation_key, 0))))
        published = max(0, min(generated, int(settings.get(publish_key, generated))))
        planned = [
            (rule, max(1, int(getattr(rule, "media_slot", 1) or 1)))
            for rule in rules
            if max(1, int(getattr(rule, "media_slot", 1) or 1)) <= published
        ]
        return planned, generated
    if count_key not in settings:
        planned = [(rule, index) for index, rule in enumerate(rules, start=1)]
        default_count = len(planned) or (1 if post_type == "result" else 0)
        return planned, default_count

    configured = max(0, min(10, int(settings.get(count_key, 0))))
    planned = [
        (rule, max(1, int(getattr(rule, "media_slot", 1) or 1)))
        for rule in rules
        if max(1, int(getattr(rule, "media_slot", 1) or 1)) <= configured
    ]
    return planned, configured


def _design(db: Session, name: str, post_type: str, kind: str) -> dict:
    item = db.scalar(
        select(DesignTemplate)
        .where(
            DesignTemplate.name == name,
            DesignTemplate.post_type == post_type,
            DesignTemplate.media_kind == kind,
            DesignTemplate.active.is_(True),
            DesignTemplate.archived_at.is_(None),
        )
        .order_by(DesignTemplate.version.desc())
    )
    if not item:
        return builtin_template(f"default-{kind}", post_type, kind)
    return {
        "id": item.id,
        "name": item.name,
        "version": item.version,
        "post_type": item.post_type,
        "media_kind": item.media_kind,
        "html_template": item.html_template,
        "css": item.css,
        "builtin": False,
    }


def _font(db: Session, configured: str) -> dict | None:
    item = db.scalar(
        select(FontAsset).where(
            (FontAsset.name == configured) | (FontAsset.family == configured),
            FontAsset.active.is_(True),
            FontAsset.archived_at.is_(None),
        )
    )
    return {"id": item.id, "family": item.family, "path": item.relative_path} if item else None


def _media_path(asset: MediaAsset | None) -> str | None:
    if not asset:
        return None
    settings = get_settings()
    root = settings.upload_root if asset.storage_kind == "upload" else settings.media_root
    return str(root / asset.relative_path)


def _upload_path(relative: str | None) -> str | None:
    if not relative:
        return None
    path = Path(relative)
    if path.is_absolute():
        return str(path)
    return str(get_settings().upload_root / path)


def _normalize_design_snapshot(value: object) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def _facts_for_media(facts: dict, media_kind: str) -> dict:
    by_media = facts.get("sponsor_references_by_media") or {}
    return {
        **facts,
        "sponsor_references": list(by_media.get(media_kind) or []),
    }


def _sponsor_snapshot(facts: dict) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for media_kind, references in (facts.get("sponsor_references_by_media") or {}).items():
        result[media_kind] = [
            {key: value for key, value in item.items() if key != "path"}
            for item in references
        ]
    return result


def _resolve_sponsor_references(
    db: Session,
    *,
    snapshot: dict,
    team: Team,
    game: Game,
    post_type: str,
    media_kind: str,
    kickoff: datetime,
) -> list[dict]:
    references: list[dict] = []
    seen: set[str] = set()
    for sponsor in applicable_sponsors(
        snapshot,
        team_id=team.id,
        post_type=post_type,
        media_kind=media_kind,
        at=kickoff,
    ):
        media_id = str(sponsor.get("media_asset_id") or "").strip()
        if not media_id:
            if sponsor.get("required"):
                raise ValueError(
                    f"Für den Pflichtsponsor {sponsor.get('name')} ist kein Logo hinterlegt"
                )
            continue
        if media_id in seen:
            continue
        asset = db.scalar(
            select(MediaAsset).where(
                MediaAsset.id == media_id,
                MediaAsset.club_id == team.club_id,
            )
        )
        if (
            asset is None
            or not asset.active
            or not asset.available
            or asset.mime_type not in {"image/jpeg", "image/png", "image/webp"}
        ):
            raise ValueError(
                f"Das hinterlegte Sponsorenlogo {sponsor.get('name')} ist nicht verfügbar"
            )
        placement = str(sponsor.get("placement") or "auto")
        references.append(
            {
                "name": str(sponsor.get("name") or asset.filename),
                "media_asset_id": asset.id,
                "path": _media_path(asset),
                "mime_type": asset.mime_type,
                "size": asset.size,
                "checksum": asset.checksum,
                "placement": placement,
                "placement_instruction": (
                    SPONSOR_POSITION_LABELS.get(
                        placement, "An einer zur Gesamtkomposition passenden Stelle integrieren."
                    ).capitalize()
                    + "; die genaue Position frei aus der Gesamtkomposition bestimmen."
                ),
                "instagram_mention": sponsor.get("instagram_mention") or "",
                "required": bool(sponsor.get("required")),
            }
        )
        seen.add(media_id)
    return references


def _render_metadata(renderer: Renderer, path: str) -> dict:
    metadata = renderer.metadata_for(path) if hasattr(renderer, "metadata_for") else {}
    return {**(metadata or {}), "path": path}


def _story_snapshot_map(value: object) -> dict[str, dict]:
    entries: list[dict] = []
    if isinstance(value, dict):
        for rule_id, entry in value.items():
            if not isinstance(entry, dict):
                continue
            normalized = dict(entry)
            normalized.setdefault("rule_id", str(rule_id))
            entries.append(normalized)
    elif isinstance(value, list):
        entries = [dict(entry) for entry in value if isinstance(entry, dict)]
    return {str(entry["rule_id"]): entry for entry in entries if entry.get("rule_id")}


def _facts(
    db: Session,
    game: Game,
    team: Team,
    asset: MediaAsset | None,
    post_type: str,
    logos: dict | None = None,
) -> dict:
    primary_font = _font(db, team.primary_font)
    secondary_font = _font(db, team.secondary_font)
    kickoff = (
        game.kickoff.replace(tzinfo=timezone.utc) if game.kickoff.tzinfo is None else game.kickoff
    )
    logos = logos or frozen_logo_set(db, game, team)
    team_logo = logos.get("team") or {}
    opponent_logo = logos.get("opponent") or {}
    aliases = team_aliases(team)
    side = resolve_team_side(game.home_team, game.away_team, aliases)
    branding = db.get(ClubBrandingConfiguration, team.club_id)
    image_settings, text_settings = branding_form_state(
        (branding.image_settings if branding else {}) or {},
        (branding.text_settings if branding else {}) or {},
    )
    snapshot = branding_snapshot(db, team.club_id)
    snapshot = {**snapshot, "image": image_settings, "text": text_settings}
    home_venue_display = (
        str(text_settings.get("home_venue_short") or "").strip()
        or str(text_settings.get("home_venue") or "").strip()
    )
    primary_standard_key = str(image_settings.get("primary_standard_font") or "system")
    secondary_standard_key = str(image_settings.get("secondary_standard_font") or "system")
    primary_family = STANDARD_FONTS.get(primary_standard_key, STANDARD_FONTS["system"])[
        "family"
    ]
    secondary_family = STANDARD_FONTS.get(
        secondary_standard_key, STANDARD_FONTS["system"]
    )["family"]
    display_name, display_short = team_display_name(snapshot, team.id, team.display_name)
    sponsor_references_by_media = {
        media_kind: _resolve_sponsor_references(
            db,
            snapshot=snapshot,
            team=team,
            game=game,
            post_type=post_type,
            media_kind=media_kind,
            kickoff=kickoff,
        )
        for media_kind in ("feed", "story")
    }
    facts = {
        "club_id": team.club_id,
        "home_team": game.home_team,
        "away_team": game.away_team,
        "own_team": team.display_name,
        "own_team_display": display_name,
        "own_team_aliases": list(aliases),
        "kickoff": kickoff.isoformat(),
        "venue": game.venue,
        "home_venue_display": home_venue_display if side == "home" else "",
        "pitch": game.pitch,
        "competition": game.competition,
        "post_type": post_type,
        "hashtags": text_settings.get("hashtags") or team.hashtags,
        "primary_color": image_settings.get("primary_color") or team.colors.get("primary"),
        "secondary_color": image_settings.get("secondary_color")
        or team.colors.get("secondary"),
        "style_direction": team.rules.get("style_direction"),
        "team_short": display_short or team.short_name,
        "home_label": text_settings.get("home_label") or "Heimspiel",
        "away_label": text_settings.get("away_label") or "Auswärtsspiel",
        "side_label": (
            text_settings.get("home_label") or "Heimspiel"
            if side == "home"
            else text_settings.get("away_label") or "Auswärtsspiel"
        ),
        "player_image": _media_path(asset),
        "team_logo": _upload_path(team_logo.get("path")),
        "opponent_logo": _upload_path(opponent_logo.get("path"))
        if not opponent_logo.get("fallback")
        else None,
        "logos": logos,
        "primary_font_asset": primary_font,
        "secondary_font_asset": secondary_font,
        "primary_font_family": primary_family,
        "secondary_font_family": secondary_family,
        "sponsor_references_by_media": sponsor_references_by_media,
    }
    if post_type == "result" and game.result_confirmed:
        facts["score"] = f"{game.home_score}:{game.away_score}"
    if post_type == "result" and not game.result_confirmed:
        raise ValueError("Ergebnis ist nicht bestätigt")
    return facts


def create_post(
    db: Session,
    game: Game,
    team: Team,
    generator: TextGenerator,
    renderer: Renderer,
    post_type="announcement",
    logo_snapshot: dict | None = None,
    *,
    generated_text: GeneratedText | None = None,
    text_prompt_override=None,
    matchday_bundle: dict | None = None,
) -> Post:
    if game.status == "provisional" or game.overrides.get("automation_blocked"):
        raise ValueError("Vorläufige Spiele sind für die Beitragserstellung gesperrt")
    existing = db.scalar(
        select(Post).where(
            Post.game_id == game.id, Post.post_type == post_type, Post.active_key == "active"
        )
    )
    if existing:
        return existing
    page = db.get(InstagramPage, team.instagram_page_id)
    warnings = []
    logos = logo_snapshot or frozen_logo_set(db, game, team)
    if not logos.get("team"):
        warnings.append("Eigenes Mannschaftslogo fehlt; der Beitrag darf nicht freigegeben werden")
    asset = reserve_image(db, team.id, game.id)
    if not asset:
        warnings.append("Kein unverbrauchtes Spielerbild; neutrale Vorlage verwendet")
    if getattr(renderer, "is_ai", False) and not asset:
        raise ValueError("Für eine KI-Grafik ist ein unverbrauchtes Spielerbild erforderlich")
    feed_design = _design(db, team.feed_template, post_type, "feed")
    facts = _facts(db, game, team, asset, post_type, logos)
    feed_facts = _facts_for_media(facts, "feed")
    feed_prompt = None
    text_prompt = None
    if getattr(renderer, "is_ai", False):
        feed_prompt_name = team.rules.get(
            f"image_prompt_feed_{post_type}",
            team.rules.get("image_prompt_feed", "default-image-feed"),
        )
        feed_prompt = resolve_prompt(
            db, feed_prompt_name, "image", post_type, "feed", feed_facts
        )
    if text_prompt_override is not None:
        text_prompt = text_prompt_override
        facts = {**feed_facts, "text_prompt": text_prompt}
    elif getattr(generator, "is_ai", False):
        text_prompt_name = team.rules.get(
            f"text_prompt_{post_type}", team.rules.get("text_prompt", f"default-text-{post_type}")
        )
        text_prompt = resolve_prompt(
            db, text_prompt_name, "text", post_type, "none", feed_facts
        )
        facts = {**feed_facts, "text_prompt": text_prompt}
    primary_font = facts["primary_font_asset"]
    secondary_font = facts["secondary_font_asset"]
    post = Post(
        game_id=game.id,
        team_id=team.id,
        instagram_page_id=page.id,
        post_type=post_type,
        status=PostStatus.CREATING,
        media_asset_id=asset.id if asset else None,
        critical_warnings=warnings,
        design_snapshot={
            "mode": {
                "image": "openai" if feed_prompt else "playwright",
                "text": "openai" if text_prompt else "fixture",
                "manual_approval_required": True,
            },
            "feed": feed_design,
            "prompts": {
                "feed": feed_prompt.snapshot() if feed_prompt else None,
                "text": text_prompt.snapshot() if text_prompt else None,
            },
            "stories": [],
            "logos": logos,
            "sponsors": _sponsor_snapshot(facts),
            "media": {},
            "fonts": {
                "primary": primary_font
                or {"family": facts["primary_font_family"], "fallback": True},
                "secondary": secondary_font
                or {"family": facts["secondary_font_family"], "fallback": True},
            },
            "colors": team.colors,
            "matchday_bundle": matchday_bundle,
        },
    )
    db.add(post)
    db.flush()
    text_result = generated_text or generator.generate(facts)
    post.text = text_result.text
    post.design_snapshot = {
        **post.design_snapshot,
        "text_generation": {
            "model": text_result.model,
            "prompt_version": text_result.prompt_version,
            "tokens": text_result.tokens,
            "shared_matchday_prompt": bool(matchday_bundle),
        },
    }
    feed_requires_manual_schedule = False
    try:
        feed_at, feed_is_absolute = feed_time(team, game, post_type)
    except ManualScheduleRequired:
        feed_at, feed_is_absolute = _aware_utc(game.kickoff), False
        feed_requires_manual_schedule = True
    rule_resolution = resolve_publication_slots(
        db,
        club_id=team.club_id,
        post_type=post_type,
        match_weekday=_aware_utc(game.kickoff).astimezone(BERLIN).weekday(),
        team_id=team.id,
        game_id=game.id,
    )
    feed_rule_slots = [slot for slot in rule_resolution.slots if slot.media_kind == "feed"]
    feed_rule_slot = feed_rule_slots[0] if feed_rule_slots else None
    if rule_resolution.rule_set:
        if feed_rule_slot:
            resolved_at = calculate_publication_time(feed_rule_slot, game)
            if resolved_at is None:
                feed_requires_manual_schedule = True
            else:
                feed_at = resolved_at
                feed_is_absolute = feed_rule_slot.timing_model == "weekday_fixed"
        elif rule_resolution.manual_schedule_required:
            feed_requires_manual_schedule = True
    team_rules = team.rules or {}
    structured_feed_variants = f"{post_type}_feed_generation_count" in team_rules
    feed_output_count = max(
        0,
        min(
            10,
            int(
                rule_resolution.rule_set.feed_generation_count
                if rule_resolution.rule_set
                else team_rules.get(
                    f"{post_type}_feed_generation_count",
                    team_rules.get(f"{post_type}_feed_output_count", 1),
                )
            ),
        ),
    )
    feed_publish_count = max(
        0,
        min(
            feed_output_count,
            int(
                team_rules.get(
                    f"{post_type}_feed_publish_count",
                    feed_output_count,
                )
            ),
        ),
    )
    feed_paths = []
    for output_index in range(1, feed_output_count + 1):
        output_prompt = prompt_for_variant(feed_prompt, output_index, feed_output_count)
        relative = (
            f"{post.id}/feed-variant-{output_index}-v1.png"
            if structured_feed_variants
            else (
                f"{post.id}/feed-v1.png"
                if output_index == 1
                else f"{post.id}/feed-{output_index}-v1.png"
            )
        )
        feed_paths.append(
            str(
                renderer.render(
                    "feed",
                    relative,
                    {
                        **_facts_for_media(facts, "feed"),
                        "template": feed_design,
                        "image_prompt": output_prompt,
                        "feed_output_index": output_index,
                        "feed_output_count": feed_output_count,
                    },
                )
            )
        )
    published_feed_paths = feed_paths[:feed_publish_count]
    if rule_resolution.rule_set:
        preferred_feed_variant = (
            feed_rule_slot.variant_number
            if feed_rule_slot
            else next(iter(rule_resolution.rule_set.feed_publish_variants or [1]), 1)
        )
        post.feed_path = (
            feed_paths[preferred_feed_variant - 1]
            if 1 <= preferred_feed_variant <= len(feed_paths)
            else (feed_paths[0] if feed_paths else None)
        )
    else:
        post.feed_path = published_feed_paths[0] if published_feed_paths else None
    post.design_snapshot = {
        **post.design_snapshot,
        "media": {
            "feed": _render_metadata(renderer, post.feed_path) if post.feed_path else None,
            "feed_outputs": [_render_metadata(renderer, path) for path in feed_paths],
            "feed_variants": [
                {
                    "path": path,
                    "output_position": 1 if structured_feed_variants else index,
                    "variant_number": index if structured_feed_variants else 1,
                    "label": (
                        f"Feed-Variante {index}"
                        if structured_feed_variants
                        else f"Feed-Bild {index}"
                    ),
                    "rendering": _render_metadata(renderer, path),
                }
                for index, path in enumerate(feed_paths, start=1)
            ],
        },
    }
    if rule_resolution.rule_set:
        effective_feed_slots: list[PublicationRuleSlot | None] = list(feed_rule_slots)
        if not effective_feed_slots and rule_resolution.manual_schedule_required and feed_paths:
            effective_feed_slots = [None]
        for sequence, publication_slot in enumerate(effective_feed_slots, start=1):
            variant_number = publication_slot.variant_number if publication_slot else 1
            if not 1 <= variant_number <= len(feed_paths):
                warnings.append(
                    f"Feed-Veröffentlichung verweist auf nicht erzeugte Variante {variant_number}"
                )
                continue
            resolved_at = (
                calculate_publication_time(
                    publication_slot,
                    game,
                    result_detected_at=_result_detected_at(game),
                )
                if publication_slot
                else None
            )
            manual_schedule = resolved_at is None
            selected_path = feed_paths[variant_number - 1]
            selected_page_id = (
                publication_slot.instagram_page_id
                if publication_slot and publication_slot.instagram_page_id
                else page.id
            )
            feed_job = PublicationJob(
                post_id=post.id,
                game_id=game.id,
                team_id=team.id,
                instagram_page_id=selected_page_id,
                kind="feed",
                media_path=selected_path,
                text_snapshot=post.text,
                scheduled_at=resolved_at or _aware_utc(game.kickoff),
                absolute_time=bool(
                    publication_slot and publication_slot.timing_model == "weekday_fixed"
                ),
                approval_status=(
                    "manual_schedule_required" if manual_schedule else "unapproved"
                ),
                status=JobStatus.DRAFT if manual_schedule else JobStatus.UNAPPROVED,
                schedule_source=(
                    "manual_required" if manual_schedule else rule_resolution.source
                ),
                publication_rule_slot_id=publication_slot.id if publication_slot else None,
                idempotency_key=(
                    f"{post.id}:feed:rule:{publication_slot.id}:v1"
                    if publication_slot
                    else f"{post.id}:feed:manual:{sequence}:v1"
                ),
            )
            db.add(feed_job)
    elif published_feed_paths:
        feed_job = PublicationJob(
            post_id=post.id,
            game_id=game.id,
            team_id=team.id,
            instagram_page_id=page.id,
            kind="carousel" if len(published_feed_paths) > 1 else "feed",
            media_path=published_feed_paths[0],
            text_snapshot=post.text,
            scheduled_at=feed_at,
            absolute_time=feed_is_absolute,
            approval_status=(
                "manual_schedule_required" if feed_requires_manual_schedule else "unapproved"
            ),
            status=JobStatus.DRAFT if feed_requires_manual_schedule else JobStatus.UNAPPROVED,
            schedule_source=(
                "manual_required"
                if feed_requires_manual_schedule
                else rule_resolution.source
            ),
            publication_rule_slot_id=feed_rule_slot.id if feed_rule_slot else None,
            idempotency_key=f"{post.id}:{'carousel' if len(published_feed_paths) > 1 else 'feed'}:v1",
        )
        db.add(feed_job)
        db.flush()
        if len(published_feed_paths) > 1:
            for position, path_value in enumerate(published_feed_paths, start=1):
                path = Path(path_value)
                payload = path.read_bytes()
                with Image.open(path) as image:
                    width, height = image.size
                db.add(
                    PublicationMediaItem(
                        publication_job_id=feed_job.id,
                        position=position,
                        media_path=path_value,
                        checksum=sha256(payload).hexdigest(),
                        mime_type="image/png",
                        file_size=len(payload),
                        width=width,
                        height=height,
                    )
                )
    rules = list(
        db.scalars(
            select(StoryRule)
            .where(
                StoryRule.team_id == team.id,
                StoryRule.post_type == post_type,
                StoryRule.active.is_(True),
            )
            .order_by(StoryRule.sort_order, StoryRule.created_at, StoryRule.id)
        ).all()
    )
    structured_story_slots = [
        slot for slot in rule_resolution.slots if slot.media_kind == "story"
    ]
    if rule_resolution.rule_set:
        story_output_count = max(
            0, min(10, int(rule_resolution.rule_set.story_generation_count))
        )
        planned_rules = []
    else:
        planned_rules, story_output_count = _effective_story_rules(team, rules, post_type)
    if (
        not rule_resolution.rule_set
        and post_type == "result"
        and not planned_rules
        and story_output_count > 0
    ):
        planned_rules = [(_SyntheticResultStoryRule(instagram_page_id=page.id), 1)]
    seen = set()
    rendered_slots = {}
    story_snapshots = []
    for rule, media_slot in planned_rules:
        story_requires_manual_schedule = False
        try:
            at = story_time(rule, game)
        except ManualScheduleRequired:
            at = _aware_utc(game.kickoff)
            story_requires_manual_schedule = True
        collision = (at, media_slot)
        if collision in seen:
            warnings.append(f"Story-Regel {rule.name} kollidiert und wurde nicht doppelt geplant")
            continue
        seen.add(collision)
        rendered = rendered_slots.get(media_slot)
        if rendered is None:
            story_design = _design(db, rule.template, post_type, "story")
            story_prompt_name = rule.prompt_template
            if not story_prompt_name or story_prompt_name == "default-image-story":
                story_prompt_name = team.rules.get(
                    f"image_prompt_story_{post_type}",
                    team.rules.get("image_prompt_story", "default-image-story"),
                )
            story_prompt = (
                resolve_prompt(
                    db,
                    story_prompt_name,
                    "image",
                    post_type,
                    "story",
                    _facts_for_media(facts, "story"),
                )
                if getattr(renderer, "is_ai", False)
                else None
            )
            story_prompt = prompt_for_variant(story_prompt, media_slot, story_output_count)
            render_context = {
                **_facts_for_media(facts, "story"),
                "template": story_design,
                "story_output_index": media_slot,
            }
            if story_prompt:
                render_context["image_prompt"] = story_prompt
            path = str(
                renderer.render(
                    "story", f"{post.id}/story-slot-{media_slot}-v1.png", render_context
                )
            )
            rendered = (path, story_design, story_prompt)
            rendered_slots[media_slot] = rendered
        path, story_design, story_prompt = rendered
        story_snapshots.append(
            {
                "rule_id": rule.id,
                "media_slot": media_slot,
                "path": path,
                "template": story_design,
                "prompt": story_prompt.snapshot() if story_prompt else None,
                "media_version": 1,
                "rendering": renderer.metadata_for(path)
                if hasattr(renderer, "metadata_for")
                else {},
            }
        )
        db.add(
            PublicationJob(
                post_id=post.id,
                game_id=game.id,
                team_id=team.id,
                instagram_page_id=rule.instagram_page_id or page.id,
                story_rule_id=None if isinstance(rule, _SyntheticResultStoryRule) else rule.id,
                kind="story",
                media_path=path,
                text_snapshot=post.text if rule.text_variant else None,
                scheduled_at=at,
                absolute_time=getattr(rule, "timing_mode", "relative") == "weekday_fixed",
                approval_status=(
                    "manual_schedule_required" if story_requires_manual_schedule else "unapproved"
                ),
                status=(JobStatus.DRAFT if story_requires_manual_schedule else JobStatus.UNAPPROVED),
                schedule_source=(
                    "manual_required" if story_requires_manual_schedule else "rule"
                ),
                publication_rule_slot_id=next(
                    (
                        slot.id
                        for slot in rule_resolution.slots
                        if slot.media_kind == "story"
                        and slot.legacy_story_rule_id == getattr(rule, "id", None)
                    ),
                    None,
                ),
                idempotency_key=f"{post.id}:story:{rule.id}:v1",
            )
        )
    # Generate every configured Story candidate even if it is not selected by
    # a publication rule. This keeps generation and publication selection
    # separate and lets reviewers choose a candidate later without another
    # provider call.
    for media_slot in range(1, story_output_count + 1):
        if media_slot in rendered_slots:
            continue
        candidate_slot = next(
            (
                slot
                for slot in structured_story_slots
                if slot.variant_number == media_slot
            ),
            None,
        )
        source_rule = (
            db.get(StoryRule, candidate_slot.legacy_story_rule_id)
            if candidate_slot and candidate_slot.legacy_story_rule_id
            else next(
                (
                    rule
                    for rule in rules
                    if max(1, int(getattr(rule, "media_slot", 1) or 1)) == media_slot
                ),
                rules[0] if rules else None,
            )
        )
        template_name = (
            candidate_slot.template
            if candidate_slot and candidate_slot.template
            else (source_rule.template if source_rule else "default-story")
        )
        story_design = _design(db, template_name, post_type, "story")
        story_prompt_name = (
            source_rule.prompt_template if source_rule and source_rule.prompt_template else None
        )
        if not story_prompt_name or story_prompt_name == "default-image-story":
            story_prompt_name = team_rules.get(
                f"image_prompt_story_{post_type}",
                team_rules.get("image_prompt_story", "default-image-story"),
            )
        story_prompt = (
            resolve_prompt(
                db,
                story_prompt_name,
                "image",
                post_type,
                "story",
                _facts_for_media(facts, "story"),
            )
            if getattr(renderer, "is_ai", False)
            else None
        )
        story_prompt = prompt_for_variant(story_prompt, media_slot, story_output_count)
        render_context = {
            **_facts_for_media(facts, "story"),
            "template": story_design,
            "story_output_index": media_slot,
        }
        if story_prompt:
            render_context["image_prompt"] = story_prompt
        path = str(
            renderer.render(
                "story", f"{post.id}/story-slot-{media_slot}-v1.png", render_context
            )
        )
        rendered_slots[media_slot] = (path, story_design, story_prompt)

    if rule_resolution.rule_set:
        effective_story_slots: list[PublicationRuleSlot | None] = list(
            structured_story_slots
        )
        if (
            not effective_story_slots
            and rule_resolution.manual_schedule_required
            and story_output_count
        ):
            manual_variants = [
                int(value)
                for value in (rule_resolution.rule_set.story_publish_variants or [1])
                if 1 <= int(value) <= story_output_count
            ]
            effective_story_slots = [None for _value in (manual_variants or [1])]
        else:
            manual_variants = []
        for sequence, publication_slot in enumerate(effective_story_slots, start=1):
            variant_number = (
                publication_slot.variant_number
                if publication_slot
                else (manual_variants[sequence - 1] if manual_variants else 1)
            )
            rendered = rendered_slots.get(variant_number)
            if rendered is None:
                warnings.append(
                    f"Story-Veröffentlichung verweist auf nicht erzeugte Variante {variant_number}"
                )
                continue
            path, story_design, story_prompt = rendered
            result_detected_at = _result_detected_at(game)
            at = (
                calculate_publication_time(
                    publication_slot,
                    game,
                    result_detected_at=result_detected_at,
                )
                if publication_slot
                else None
            )
            manual_schedule = at is None
            legacy_rule_id = (
                publication_slot.legacy_story_rule_id if publication_slot else None
            )
            source_rule = db.get(StoryRule, legacy_rule_id) if legacy_rule_id else None
            story_snapshots.append(
                {
                    "rule_id": legacy_rule_id,
                    "publication_rule_slot_id": (
                        publication_slot.id if publication_slot else None
                    ),
                    "media_slot": variant_number,
                    "path": path,
                    "template": story_design,
                    "prompt": story_prompt.snapshot() if story_prompt else None,
                    "media_version": 1,
                    "rendering": (
                        renderer.metadata_for(path)
                        if hasattr(renderer, "metadata_for")
                        else {}
                    ),
                }
            )
            db.add(
                PublicationJob(
                    post_id=post.id,
                    game_id=game.id,
                    team_id=team.id,
                    instagram_page_id=(
                        publication_slot.instagram_page_id
                        if publication_slot and publication_slot.instagram_page_id
                        else page.id
                    ),
                    story_rule_id=legacy_rule_id,
                    kind="story",
                    media_path=path,
                    text_snapshot=(post.text if source_rule and source_rule.text_variant else None),
                    scheduled_at=at or _aware_utc(game.kickoff),
                    absolute_time=bool(
                        publication_slot
                        and publication_slot.timing_model == "weekday_fixed"
                    ),
                    approval_status=(
                        "manual_schedule_required" if manual_schedule else "unapproved"
                    ),
                    status=(
                        JobStatus.DRAFT if manual_schedule else JobStatus.UNAPPROVED
                    ),
                    schedule_source=(
                        "manual_required" if manual_schedule else rule_resolution.source
                    ),
                    publication_rule_slot_id=(
                        publication_slot.id if publication_slot else None
                    ),
                    idempotency_key=(
                        f"{post.id}:story:rule:{publication_slot.id}:v1"
                        if publication_slot
                        else f"{post.id}:story:manual:{sequence}:v1"
                    ),
                )
            )

    story_variants = [
        {
            "path": value[0],
            "output_position": 1,
            "variant_number": media_slot,
            "label": f"Story-Variante {media_slot}",
            "template": value[1],
            "prompt": value[2].snapshot() if value[2] else None,
            "rendering": renderer.metadata_for(value[0])
            if hasattr(renderer, "metadata_for")
            else {},
        }
        for media_slot, value in sorted(rendered_slots.items())
    ]
    post.design_snapshot = {
        **post.design_snapshot,
        "stories": story_snapshots,
        "story_variants": story_variants,
    }
    post.critical_warnings = warnings
    post.status = PostStatus.INCOMPLETE if warnings else PostStatus.PENDING
    from app.posts.media_versions import synchronize_post_versions

    synchronize_post_versions(db, post)
    db.commit()
    return post


def create_matchday_bundle_posts(
    db: Session,
    games: list[Game],
    teams: dict[str, Team],
    generator: TextGenerator,
    renderer: Renderer,
    post_type: str,
    logo_snapshots: dict[str, dict],
    bundle_key: str,
) -> list[Post]:
    """Create all game-specific media with one shared publication text.

    Images and stories remain tied to their individual game.  The text model is
    called exactly once with an explicit, complete match list and that result
    is frozen into every member post before the carousel coordinator runs.
    """
    if len(games) < 2:
        raise ValueError("Ein gemeinsamer Spieltagsauftrag benötigt mindestens zwei Spiele")
    primary = games[0]
    primary_team = teams[primary.team_id]
    bundle_facts = {
        game.id: _facts(
            db,
            game,
            teams[game.team_id],
            None,
            post_type,
            logo_snapshots[game.id],
        )
        for game in games
    }
    base_facts = bundle_facts[primary.id]
    combined_sponsors = []
    seen_sponsors: set[str] = set()
    for game in games:
        for sponsor in _facts_for_media(bundle_facts[game.id], "feed").get(
            "sponsor_references", []
        ):
            identity = str(sponsor.get("media_asset_id") or sponsor.get("name") or "")
            if identity and identity not in seen_sponsors:
                combined_sponsors.append(sponsor)
                seen_sponsors.add(identity)
    base_facts = {**base_facts, "sponsor_references": combined_sponsors}
    local_games = []
    for index, game in enumerate(games, start=1):
        kickoff = _aware_utc(game.kickoff).astimezone(BERLIN)
        score = (
            f"{game.home_score}:{game.away_score}"
            if post_type == "result" and game.result_confirmed
            else None
        )
        local_games.append(
            {
                "position": index,
                "home_team": game.home_team,
                "away_team": game.away_team,
                "date": kickoff.strftime("%d.%m.%Y"),
                "time": kickoff.strftime("%H:%M"),
                "competition": game.competition or "",
                "venue": game.venue or "",
                "score": score,
            }
        )
    match_lines = []
    for item in local_games:
        line = (
            f"{item['position']}. {item['home_team']} gegen {item['away_team']}; "
            f"{item['date']}, {item['time']} Uhr"
        )
        if item["competition"]:
            line += f"; Wettbewerb: {item['competition']}"
        if item["venue"]:
            line += f"; Spielort: {item['venue']}"
        if item["score"]:
            line += f"; bestätigtes Ergebnis: {item['score']}"
        match_lines.append(line)
    match_list = "\n".join(match_lines)
    shared_prompt = None
    if getattr(generator, "is_ai", False):
        prompt_name = primary_team.rules.get(
            f"text_prompt_{post_type}",
            primary_team.rules.get("text_prompt", f"default-text-{post_type}"),
        )
        shared_prompt = resolve_prompt(
            db,
            prompt_name,
            "text",
            post_type,
            "none",
            base_facts,
        )
        shared_prompt = matchday_bundle_prompt(shared_prompt, local_games)
        shared_text = generator.generate(
            {
                **base_facts,
                "text_prompt": shared_prompt,
                "matchday_games": local_games,
            }
        )
    else:
        heading = "Ergebnisse des gemeinsamen Spieltags" if post_type == "result" else "Gemeinsamer Spieltag"
        hashtags = []
        for game in games:
            for tag in teams[game.team_id].hashtags or []:
                if tag not in hashtags:
                    hashtags.append(tag)
        shared_text = GeneratedText(
            heading + "\n\n" + match_list + ("\n\n" + " ".join(hashtags) if hashtags else ""),
            "fixture",
            prompt_version="shared-matchday-fixture-v1",
        )
    bundle_snapshot = {
        "key": bundle_key,
        "game_ids": [item.id for item in games],
        "single_text_prompt": True,
        "stories_remain_separate": True,
    }
    posts = []
    for game in games:
        posts.append(
            create_post(
                db,
                game,
                teams[game.team_id],
                generator,
                renderer,
                post_type,
                logo_snapshots[game.id],
                generated_text=shared_text,
                text_prompt_override=shared_prompt,
                matchday_bundle=bundle_snapshot,
            )
        )
    return posts


def rerender_post(
    db: Session,
    post: Post,
    renderer: Renderer,
    story_job_ids: list[str] | None = None,
    logo_snapshot: dict | None = None,
    media_asset_id: str | None = None,
    revision_instruction: str | None = None,
    *,
    rerender_feed: bool = True,
    feed_positions: list[int] | None = None,
    story_variant_numbers: list[int] | None = None,
) -> Post:
    game = db.get(Game, post.game_id)
    team = db.get(Team, post.team_id)
    asset = db.get(MediaAsset, post.media_asset_id) if post.media_asset_id else None
    if not game or not team:
        raise ValueError("Beitrag hat keine gültigen Spiel- oder Mannschaftsdaten")
    jobs = list(
        db.scalars(
            select(PublicationJob).where(PublicationJob.post_id == post.id).with_for_update()
        )
    )
    selected = set(story_job_ids or [])
    selected_story_variants = {
        int(number) for number in (story_variant_numbers or []) if int(number) > 0
    }
    story_jobs = {job.id: job for job in jobs if job.kind == "story"}
    if not selected.issubset(story_jobs):
        raise RerenderConflict("Mindestens eine ausgewählte Story gehört nicht zu diesem Beitrag")
    if not rerender_feed and not selected and not selected_story_variants:
        raise RerenderConflict("Bitte mindestens Feed oder eine Story auswählen")
    if rerender_feed and any(
        job.status == JobStatus.PUBLISHED for job in jobs if job.kind in {"feed", "carousel"}
    ):
        raise RerenderConflict(
            "Der Feed wurde bereits veröffentlicht und darf nicht neu erzeugt werden"
        )
    if any(story_jobs[job_id].status == JobStatus.PUBLISHED for job_id in selected):
        raise RerenderConflict(
            "Eine ausgewählte Story wurde bereits veröffentlicht und darf nicht neu erzeugt werden"
        )
    if media_asset_id and media_asset_id != post.media_asset_id:
        target_asset = db.scalar(
            select(MediaAsset).where(MediaAsset.id == media_asset_id).with_for_update()
        )
        if not target_asset or target_asset.team_id != team.id:
            raise RerenderConflict("Das ausgewählte Spielerbild gehört nicht zu dieser Mannschaft")
        if not target_asset.active or not target_asset.available:
            raise RerenderConflict("Das ausgewählte Spielerbild ist nicht mehr verfügbar")
        if target_asset.reserved_game_id not in {None, game.id} or target_asset.uses > 0:
            raise RerenderConflict("Das ausgewählte Spielerbild wurde inzwischen bereits verwendet")
        if asset:
            asset = db.scalar(select(MediaAsset).where(MediaAsset.id == asset.id).with_for_update())
            asset.reserved_game_id = None
            # Das bisherige Bild wurde bereits für diesen Spieltag verwendet und
            # bleibt deshalb über uses > 0 verbraucht. Nur seine Reservierung wird
            # für das neu ausgewählte Bild freigegeben.
            db.flush()
        target_asset.reserved_game_id = game.id
        target_asset.uses += 1
        post.media_asset_id = target_asset.id
        asset = target_asset
        db.flush()
    logos = logo_snapshot or frozen_logo_set(db, game, team)
    facts = _facts(db, game, team, asset, post.post_type, logos)
    old_snapshot = _normalize_design_snapshot(post.design_snapshot)
    snapshots = _story_snapshot_map(old_snapshot.get("stories"))
    story_candidates = {
        max(1, int(candidate.get("variant_number", 1) or 1)): dict(candidate)
        for candidate in (old_snapshot.get("story_variants") or [])
        if isinstance(candidate, dict) and candidate.get("path")
    }
    for job_id in selected:
        publication = story_jobs[job_id]
        snapshot = snapshots.get(publication.story_rule_id, {})
        slot = None
        if publication.media_version_id:
            version = db.get(GeneratedMediaVersion, publication.media_version_id)
            slot = db.get(GeneratedMediaSlot, version.slot_id) if version else None
        if slot and slot.post_id == post.id and slot.media_kind == "story":
            variant_number = slot.variant_number
        else:
            variant_number = max(1, int(snapshot.get("media_slot", 1) or 1))
        selected_story_variants.add(variant_number)
        if variant_number not in story_candidates and publication.media_path:
            # Recover a selectable source from legacy snapshots that were a
            # string, mapping or mixed list.  The existing file and historical
            # media version are retained instead of silently resetting them.
            story_candidates[variant_number] = {
                **snapshot,
                "path": publication.media_path,
                "output_position": 1,
                "variant_number": variant_number,
                "media_slot": variant_number,
                "media_version": snapshot.get("media_version", publication.version or 1),
                "rule_id": publication.story_rule_id,
            }
    if any(number not in story_candidates for number in selected_story_variants):
        raise RerenderConflict("Mindestens eine ausgewählte Story-Variante ist nicht vorhanden")
    feed_design = old_snapshot.get("feed")
    feed_prompt = None
    feed_paths = []
    if rerender_feed:
        feed_design = _design(db, team.feed_template, post.post_type, "feed")
        post.feed_version += 1
        feed_prompt_name = team.rules.get(
            f"image_prompt_feed_{post.post_type}",
            team.rules.get("image_prompt_feed", "default-image-feed"),
        )
        feed_prompt = (
            resolve_prompt(
                db,
                feed_prompt_name,
                "image",
                post.post_type,
                "feed",
                _facts_for_media(facts, "feed"),
            )
            if getattr(renderer, "is_ai", False)
            else None
        )
        feed_prompt = _revision_prompt(feed_prompt, revision_instruction)
        previous_feed_outputs = (old_snapshot.get("media") or {}).get("feed_outputs") or []
        previous_feed_variants = (
            (old_snapshot.get("media") or {}).get("feed_variants") or []
        )
        structured_feed_variants = bool(
            f"{post.post_type}_feed_generation_count" in (team.rules or {})
            or (
                previous_feed_variants
                and all(
                    int(candidate.get("output_position", 1) or 1) == 1
                    for candidate in previous_feed_variants
                    if isinstance(candidate, dict)
                )
                and any(
                    int(candidate.get("variant_number", 1) or 1) > 1
                    for candidate in previous_feed_variants
                    if isinstance(candidate, dict)
                )
            )
        )
        feed_output_count = max(1, len(previous_feed_outputs))
        selected_feed_positions = set(feed_positions or range(1, feed_output_count + 1))
        if not selected_feed_positions or any(
            position < 1 or position > feed_output_count
            for position in selected_feed_positions
        ):
            raise RerenderConflict("Ungültige Auswahl der Feed-Ausgaben")
        if any(job.kind == "carousel" for job in jobs) and feed_output_count == 1:
            raise RerenderConflict(
                "Ein gebündelter Vereins-Karussellbeitrag kann nicht über die normale Feed-Neugenerierung geändert werden"
            )
        for output_index in range(1, feed_output_count + 1):
            if output_index not in selected_feed_positions:
                previous = previous_feed_outputs[output_index - 1]
                previous_path = previous.get("path") if isinstance(previous, dict) else None
                if not previous_path or not Path(previous_path).is_file():
                    raise RerenderConflict(
                        "Eine nicht ausgewählte Feed-Ausgabe ist nicht mehr verfügbar"
                    )
                feed_paths.append(previous_path)
                continue
            relative = (
                f"{post.id}/feed-variant-{output_index}-v{post.feed_version}.png"
                if structured_feed_variants
                else (
                    f"{post.id}/feed-v{post.feed_version}.png"
                    if output_index == 1
                    else f"{post.id}/feed-{output_index}-v{post.feed_version}.png"
                )
            )
            feed_paths.append(
                str(
                    renderer.render(
                        "feed",
                        relative,
                        {
                            **_facts_for_media(facts, "feed"),
                            "template": feed_design,
                            "image_prompt": prompt_for_variant(
                                feed_prompt, output_index, feed_output_count
                            ),
                            "feed_output_index": output_index,
                            "feed_output_count": feed_output_count,
                        },
                    )
                )
            )
        post.feed_path = feed_paths[0]
    for job in jobs:
        if job.kind in {"feed", "carousel"} and rerender_feed:
            job.media_path = post.feed_path
            job.version += 1
            job.idempotency_key = f"{post.id}:{job.kind}:v{post.feed_version}"
            if job.kind == "carousel":
                published_count = int(
                    db.scalar(
                        select(func.count(PublicationMediaItem.id)).where(
                            PublicationMediaItem.publication_job_id == job.id
                        )
                    )
                    or 0
                )
                db.execute(
                    delete(PublicationMediaItem).where(
                        PublicationMediaItem.publication_job_id == job.id
                    )
                )
                for position, path_value in enumerate(
                    feed_paths[:published_count], start=1
                ):
                    media_path = Path(path_value)
                    payload = media_path.read_bytes()
                    with Image.open(media_path) as image:
                        width, height = image.size
                    db.add(
                        PublicationMediaItem(
                            publication_job_id=job.id,
                            position=position,
                            media_path=path_value,
                            checksum=sha256(payload).hexdigest(),
                            mime_type="image/png",
                            file_size=len(payload),
                            width=width,
                            height=height,
                        )
                    )
    active_story_rules = list(
        db.scalars(
            select(StoryRule)
            .where(
                StoryRule.club_id == post.club_id,
                StoryRule.team_id == post.team_id,
                StoryRule.post_type == post.post_type,
                StoryRule.active.is_(True),
            )
            .order_by(StoryRule.sort_order, StoryRule.created_at, StoryRule.id)
        )
    )
    for variant_number in sorted(selected_story_variants):
        candidate = story_candidates[variant_number]
        matching_jobs = []
        for publication in story_jobs.values():
            current_slot = None
            if publication.media_version_id:
                current_version = db.get(
                    GeneratedMediaVersion, publication.media_version_id
                )
                current_slot = (
                    db.get(GeneratedMediaSlot, current_version.slot_id)
                    if current_version
                    else None
                )
            snapshot = snapshots.get(publication.story_rule_id, {})
            current_variant = (
                current_slot.variant_number
                if current_slot and current_slot.post_id == post.id
                else max(1, int(snapshot.get("media_slot", 1) or 1))
            )
            if current_variant == variant_number:
                matching_jobs.append(publication)
        source_job = next(
            (publication for publication in matching_jobs if publication.story_rule_id),
            None,
        )
        rule = (
            db.get(StoryRule, source_job.story_rule_id)
            if source_job and source_job.story_rule_id
            else next(
                (
                    item
                    for item in active_story_rules
                    if int(item.media_slot or 1) == variant_number
                ),
                active_story_rules[0] if active_story_rules else None,
            )
        )
        template_name = (
            rule.template
            if rule
            else str(candidate.get("template") or "default-story")
        )
        design = _design(db, template_name, post.post_type, "story")
        story_prompt_name = rule.prompt_template if rule else None
        if not story_prompt_name or story_prompt_name == "default-image-story":
            story_prompt_name = team.rules.get(
                f"image_prompt_story_{post.post_type}",
                team.rules.get("image_prompt_story", "default-image-story"),
            )
        story_prompt = (
            resolve_prompt(
                db,
                story_prompt_name,
                "image",
                post.post_type,
                "story",
                _facts_for_media(facts, "story"),
            )
            if getattr(renderer, "is_ai", False)
            else None
        )
        story_prompt = _revision_prompt(story_prompt, revision_instruction)
        story_prompt = prompt_for_variant(
            story_prompt, variant_number, max(story_candidates or {1: {}})
        )
        slot = db.scalar(
            select(GeneratedMediaSlot)
            .where(
                GeneratedMediaSlot.club_id == post.club_id,
                GeneratedMediaSlot.post_id == post.id,
                GeneratedMediaSlot.media_kind == "story",
                GeneratedMediaSlot.variant_number == variant_number,
            )
            .with_for_update()
        )
        latest_number = (
            int(
                db.scalar(
                    select(func.max(GeneratedMediaVersion.version_number)).where(
                        GeneratedMediaVersion.slot_id == slot.id
                    )
                )
                or 0
            )
            if slot
            else 0
        )
        try:
            legacy_number = max(0, int(candidate.get("media_version", 0) or 0))
        except (TypeError, ValueError):
            legacy_number = 0
        next_version = max(latest_number, legacy_number) + 1
        path_value = str(
            renderer.render(
                "story",
                f"{post.id}/story-slot-{variant_number}-v{next_version}.png",
                {
                    **_facts_for_media(facts, "story"),
                    "template": design,
                    "image_prompt": story_prompt,
                    "story_output_index": variant_number,
                },
            )
        )
        rendering = (
            renderer.metadata_for(path_value)
            if hasattr(renderer, "metadata_for")
            else {}
        )
        story_candidates[variant_number] = {
            **candidate,
            "path": path_value,
            "output_position": 1,
            "variant_number": variant_number,
            "label": candidate.get("label") or f"Story-Variante {variant_number}",
            "template": design,
            "prompt": story_prompt.snapshot() if story_prompt else None,
            "media_version": next_version,
            "rendering": rendering,
        }
        for publication in matching_jobs:
            if publication.status == JobStatus.PUBLISHED:
                continue
            publication.media_path = path_value
            publication.version += 1
            publication.idempotency_key = (
                f"{post.id}:story:{publication.story_rule_id or variant_number}:"
                f"v{next_version}"
            )
            entry = dict(snapshots.get(publication.story_rule_id) or {})
            snapshots[publication.story_rule_id] = {
                **entry,
                "rule_id": publication.story_rule_id,
                "media_slot": variant_number,
                "path": path_value,
                "template": design,
                "prompt": story_prompt.snapshot() if story_prompt else None,
                "media_version": next_version,
                "rendering": rendering,
            }
    raw_prompts = old_snapshot.get("prompts")
    prompt_snapshot = dict(raw_prompts) if isinstance(raw_prompts, dict) else {}
    if rerender_feed:
        prompt_snapshot["feed"] = feed_prompt.snapshot() if feed_prompt else None
    media_snapshot = dict(old_snapshot.get("media") or {})
    if rerender_feed:
        media_snapshot["feed"] = _render_metadata(renderer, post.feed_path)
        media_snapshot["feed_outputs"] = [_render_metadata(renderer, path) for path in feed_paths]
        if structured_feed_variants:
            media_snapshot["feed_variants"] = [
                {
                    "path": path,
                    "output_position": 1,
                    "variant_number": index,
                    "label": f"Feed-Variante {index}",
                    "rendering": _render_metadata(renderer, path),
                }
                for index, path in enumerate(feed_paths, start=1)
            ]
        else:
            media_snapshot["feed_variants"] = [
                {
                    "path": path,
                    "output_position": index,
                    "variant_number": 1,
                    "label": f"Feed-Bild {index}",
                    "rendering": _render_metadata(renderer, path),
                }
                for index, path in enumerate(feed_paths, start=1)
            ]
    post.design_snapshot = {
        **old_snapshot,
        "feed": feed_design,
        "prompts": prompt_snapshot,
        "stories": list(snapshots.values()),
        "story_variants": [
            story_candidates[number] for number in sorted(story_candidates)
        ],
        "logos": logos,
        "sponsors": _sponsor_snapshot(facts),
        "media": media_snapshot,
        "player_asset": {"id": asset.id, "filename": asset.filename, "checksum": asset.checksum}
        if asset
        else None,
        "fonts": {
            "primary": facts["primary_font_asset"]
            or {"family": facts["primary_font_family"], "fallback": True},
            "secondary": facts["secondary_font_asset"]
            or {"family": facts["secondary_font_family"], "fallback": True},
        },
        "colors": team.colors,
    }
    if logos.get("team"):
        post.critical_warnings = [
            warning
            for warning in (post.critical_warnings or [])
            if warning
            not in {
                "Logo-Zuordnung wurde geändert; Grafiken neu zusammensetzen",
                (
                    "Logo-Zuordnung wurde geändert; Grafiken mit aktualisierten "
                    "Logo-Referenzen neu erzeugen"
                ),
                "Eigenes Mannschaftslogo fehlt; der Beitrag darf nicht freigegeben werden",
            }
        ]
    was_approved = post.status in {PostStatus.APPROVED, PostStatus.SCHEDULED, PostStatus.PARTIAL}
    post.version += 1
    if was_approved:
        post.status = PostStatus.REAPPROVAL
        post.approved_version = None
        for job in jobs:
            if job.status != JobStatus.PUBLISHED:
                job.status = JobStatus.UNAPPROVED
                job.approval_status = "reapproval_required"
                job.approved_post_version = None
                job.error = "Grafiken wurden neu erzeugt; erneute Freigabe erforderlich"
    from app.posts.media_versions import synchronize_post_versions

    synchronize_post_versions(db, post)
    db.flush()
    return post


def revise_post(
    db: Session,
    post: Post,
    *,
    instruction: str,
    revise_text: bool,
    revise_graphics: bool,
    rerender_feed: bool | None = None,
    text_generator: TextGenerator | None = None,
    renderer: Renderer | None = None,
    story_job_ids: list[str] | None = None,
    logo_snapshot: dict | None = None,
    media_asset_id: str | None = None,
    feed_positions: list[int] | None = None,
    story_variant_numbers: list[int] | None = None,
) -> Post:
    """Apply a persistent AI revision while preserving published outputs."""
    instruction = instruction.strip()
    if not 10 <= len(instruction) <= 2000:
        raise ValueError("Die KI-Änderungsanweisung muss 10 bis 2000 Zeichen lang sein")
    if not revise_text and not revise_graphics:
        raise ValueError("Bitte mindestens Begleittext oder Grafiken auswählen")
    if rerender_feed is None:
        rerender_feed = revise_graphics
    if (
        revise_graphics
        and not rerender_feed
        and not story_job_ids
        and not story_variant_numbers
    ):
        raise ValueError("Bitte Feed oder mindestens eine Story auswählen")
    if revise_text and text_generator is None:
        raise ValueError("Textgenerator fehlt")
    if revise_graphics and renderer is None:
        raise ValueError("Bildgenerator fehlt")

    game = db.get(Game, post.game_id)
    team = db.get(Team, post.team_id)
    if not game or not team:
        raise ValueError("Beitrag hat keine gültigen Spiel- oder Mannschaftsdaten")
    jobs = list(
        db.scalars(
            select(PublicationJob).where(PublicationJob.post_id == post.id).with_for_update()
        )
    )
    if revise_text and any(
        job.kind in {"feed", "carousel"}
        and (job.status == JobStatus.PUBLISHED or job.platform_id or job.published_at)
        for job in jobs
    ):
        raise RerenderConflict(
            "Der Feed wurde bereits veröffentlicht; sein Begleittext darf nicht per KI geändert werden"
        )

    previous_status = post.status
    generated_text = None
    if revise_graphics:
        post = rerender_post(
            db,
            post,
            renderer,
            story_job_ids,
            logo_snapshot,
            media_asset_id,
            instruction,
            rerender_feed=rerender_feed,
            feed_positions=feed_positions,
            story_variant_numbers=story_variant_numbers,
        )

    if revise_text:
        asset = db.get(MediaAsset, post.media_asset_id) if post.media_asset_id else None
        facts = _facts(db, game, team, asset, post.post_type, logo_snapshot)
        generated_text = text_generator.revise(facts, post.text or "", instruction)
        post.text = generated_text.text
        post.text_version += 1
        for publication in jobs:
            if publication.status == JobStatus.PUBLISHED:
                continue
            if publication.kind in {"feed", "carousel"} or publication.text_snapshot is not None:
                publication.text_snapshot = post.text

    if not revise_graphics:
        post.version += 1

    was_approved = previous_status in {
        PostStatus.APPROVED,
        PostStatus.SCHEDULED,
        PostStatus.PARTIAL,
    }
    post.approved_version = None
    post.approved_by = None
    post.approved_at = None
    post.status = (
        PostStatus.REAPPROVAL
        if was_approved
        else (PostStatus.INCOMPLETE if post.critical_warnings else PostStatus.PENDING)
    )
    approval_status = "reapproval_required" if was_approved else "unapproved"
    for publication in jobs:
        if publication.status == JobStatus.PUBLISHED:
            continue
        publication.status = JobStatus.UNAPPROVED
        publication.approval_status = approval_status
        publication.approved_post_version = None
        publication.error = "KI-Änderungen wurden erzeugt; erneute Freigabe erforderlich"

    snapshot = _normalize_design_snapshot(post.design_snapshot)
    revisions = [
        dict(entry) for entry in snapshot.get("ai_revisions", []) if isinstance(entry, dict)
    ]
    revisions.append(
        {
            "instruction": instruction,
            "text": revise_text,
            "graphics": revise_graphics,
            "feed": bool(rerender_feed) if revise_graphics else False,
            "story_job_ids": sorted(set(story_job_ids or [])),
            "story_variant_numbers": sorted(set(story_variant_numbers or [])),
            "text_model": generated_text.model if generated_text else None,
            "text_prompt_version": generated_text.prompt_version if generated_text else None,
            "text_tokens": generated_text.tokens if generated_text else None,
            "post_version": post.version,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    post.design_snapshot = {**snapshot, "ai_revisions": revisions}
    from app.posts.media_versions import ensure_text_version, synchronize_post_versions

    ensure_text_version(db, post, source="ai_revision")
    if revise_graphics:
        synchronize_post_versions(db, post)
    db.flush()
    return post


def _safe_generated_base(value: str | None) -> Path:
    if not value:
        raise LogoValidationError(
            "Für diese Legacy-Grafik ist keine eingefrorene KI-Grundgrafik vorhanden."
        )
    root = get_settings().generated_root.resolve()
    path = Path(value).resolve()
    if not path.is_relative_to(root) or path.is_symlink() or not path.is_file():
        raise LogoValidationError("Die eingefrorene KI-Grundgrafik ist nicht sicher verfügbar.")
    return path


def logo_recompose_availability(
    post: Post,
    jobs: list[PublicationJob],
) -> dict:
    """Report whether every publication has a safe, frozen AI base image."""
    snapshot = _normalize_design_snapshot(post.design_snapshot)
    feed_metadata = dict((snapshot.get("media") or {}).get("feed") or {})
    stories = _story_snapshot_map(snapshot.get("stories"))

    ai_reference_reason = (
        "Die Logos sind Bestandteil der KI-Komposition. Eine Logoänderung "
        "erfordert eine vollständige Bild-Neugenerierung."
    )

    def status(rendering: dict) -> dict:
        integration = rendering.get("logo_integration")
        if isinstance(integration, dict) and integration.get("mode") == "ai-reference":
            return {
                "available": False,
                "reason": ai_reference_reason,
                "requires_full_rerender": True,
            }
        try:
            return {
                "available": True,
                "path": str(_safe_generated_base(rendering.get("ai_base_path"))),
                "legacy_compositor": True,
            }
        except LogoValidationError as exc:
            return {"available": False, "reason": str(exc)}

    story_status = {}
    for publication in jobs:
        if publication.kind != "story":
            continue
        entry = dict(stories.get(publication.story_rule_id) or {})
        rendering = dict(entry.get("rendering") or {})
        story_status[publication.id] = status(rendering)
    feed_status = status(feed_metadata)
    return {
        "feed": feed_status,
        "stories": story_status,
        "all_available": feed_status["available"]
        and all(item["available"] for item in story_status.values()),
    }


def logo_recompose_preflight(
    post: Post,
    jobs: list[PublicationJob],
    story_job_ids: list[str],
) -> dict:
    """Resolve all required base images before writing a recomposed file."""
    availability = logo_recompose_availability(post, jobs)
    missing = []
    if not availability["feed"]["available"]:
        missing.append("Feed")
    selected = set(story_job_ids)
    for publication in jobs:
        if publication.id not in selected:
            continue
        item = availability["stories"].get(publication.id) or {"available": False}
        if not item["available"]:
            missing.append(
                f"Story {publication.scheduled_at.astimezone().strftime('%d.%m.%Y %H:%M')}"
            )
    if missing:
        raise LogoValidationError(
            "Lokale Logo-Neuzusammensetzung nicht möglich: Für "
            + ", ".join(missing)
            + " fehlt eine separat eingefrorene KI-Grundgrafik. "
            "Bitte stattdessen „Grafiken neu erzeugen“ verwenden. "
            "Dabei werden die verifizierten Logos als Referenzbilder in die "
            "KI-Komposition integriert."
        )
    return {
        "feed": Path(availability["feed"]["path"]),
        "stories": {
            job_id: Path(item["path"])
            for job_id, item in availability["stories"].items()
            if job_id in selected
        },
    }


def recompose_post_logos(
    db: Session,
    post: Post,
    story_job_ids: list[str],
    logo_snapshot: dict,
) -> Post:
    game = db.get(Game, post.game_id)
    team = db.get(Team, post.team_id)
    if not game or not team:
        raise LogoValidationError("Spiel oder Mannschaft ist nicht mehr verfügbar.")
    jobs = list(
        db.scalars(
            select(PublicationJob).where(PublicationJob.post_id == post.id).with_for_update()
        )
    )
    selected = set(story_job_ids)
    story_jobs = {job.id: job for job in jobs if job.kind == "story"}
    if not selected.issubset(story_jobs):
        raise RerenderConflict("Mindestens eine ausgewählte Story gehört nicht zu diesem Beitrag")
    feed_job = next((job for job in jobs if job.kind == "feed"), None)
    if not feed_job or feed_job.status == JobStatus.PUBLISHED:
        raise RerenderConflict(
            "Der Feed wurde bereits veröffentlicht oder fehlt und darf nicht neu zusammengesetzt werden"
        )
    if any(story_jobs[job_id].status == JobStatus.PUBLISHED for job_id in selected):
        raise RerenderConflict("Eine ausgewählte Story wurde bereits veröffentlicht")
    sources = logo_recompose_preflight(post, jobs, list(selected))
    old_snapshot = _normalize_design_snapshot(post.design_snapshot)
    media_snapshot = dict(old_snapshot.get("media") or {})
    feed_metadata = dict(media_snapshot.get("feed") or {})
    compositor = LogoCompositor(get_settings().upload_root)
    validator = Renderer(
        get_settings().generated_root,
        get_settings().media_root,
        get_settings().upload_root,
    )
    post.feed_version += 1
    feed_target = (
        get_settings().generated_root / post.id / f"feed-v{post.feed_version}.png"
    ).resolve()
    feed_composition = compositor.compose(
        base_path=sources["feed"],
        output_path=feed_target,
        kind="feed",
        logos=logo_snapshot,
    )
    validator.validate(feed_target, "feed")
    post.feed_path = str(feed_target)
    feed_job.media_path = post.feed_path
    feed_job.version += 1
    feed_job.idempotency_key = f"{post.id}:feed:v{post.feed_version}"
    media_snapshot["feed"] = {
        **feed_metadata,
        "final_path": str(feed_target),
        "composition": feed_composition,
        "logo_only_recomposition": True,
    }
    snapshots = _story_snapshot_map(old_snapshot.get("stories"))
    for job_id in selected:
        publication = story_jobs[job_id]
        entry = dict(snapshots.get(publication.story_rule_id) or {})
        rendering = dict(entry.get("rendering") or {})
        version = int(entry.get("media_version", 1)) + 1
        target = (
            get_settings().generated_root
            / post.id
            / f"story-{publication.story_rule_id}-v{version}.png"
        ).resolve()
        composition = compositor.compose(
            base_path=sources["stories"][job_id],
            output_path=target,
            kind="story",
            logos=logo_snapshot,
        )
        validator.validate(target, "story")
        publication.media_path = str(target)
        publication.version += 1
        publication.idempotency_key = f"{post.id}:story:{publication.story_rule_id}:v{version}"
        snapshots[publication.story_rule_id] = {
            **entry,
            "rule_id": publication.story_rule_id,
            "media_version": version,
            "rendering": {
                **rendering,
                "final_path": str(target),
                "composition": composition,
                "logo_only_recomposition": True,
            },
        }
    post.design_snapshot = {
        **old_snapshot,
        "logos": logo_snapshot,
        "media": media_snapshot,
        "stories": list(snapshots.values()),
    }
    removable = {
        "Logo-Zuordnung wurde geändert; Grafiken neu zusammensetzen",
        ("Logo-Zuordnung wurde geändert; Grafiken mit aktualisierten Logo-Referenzen neu erzeugen"),
        "Eigenes Mannschaftslogo fehlt; der Beitrag darf nicht freigegeben werden",
    }
    post.critical_warnings = [
        warning for warning in (post.critical_warnings or []) if warning not in removable
    ]
    post.version += 1
    post.status = PostStatus.REAPPROVAL
    post.approved_version = None
    for publication in jobs:
        if publication.status != JobStatus.PUBLISHED:
            publication.status = JobStatus.UNAPPROVED
            publication.approval_status = "reapproval_required"
            publication.approved_post_version = None
            publication.error = "Logos wurden neu zusammengesetzt; erneute Freigabe erforderlich"
    db.flush()
    return post


def reschedule_game(db: Session, game: Game, new_kickoff: datetime):
    old = game.kickoff
    game.original_kickoff = game.original_kickoff or old
    game.kickoff = new_kickoff
    affected_posts: set[str] = set()
    new_weekday = _aware_utc(new_kickoff).astimezone(BERLIN).weekday()
    for job in db.scalars(
        select(PublicationJob).where(
            PublicationJob.game_id == game.id,
            PublicationJob.status.not_in([JobStatus.PUBLISHED, JobStatus.CANCELLED]),
        ).with_for_update()
    ):
        affected_posts.add(job.post_id)
        if job.schedule_source == "manual":
            # A deliberate editor decision is not silently replaced by a rule.
            continue
        current_slot = (
            db.get(PublicationRuleSlot, job.publication_rule_slot_id)
            if job.publication_rule_slot_id
            else None
        )
        if current_slot:
            resolution = resolve_publication_slots(
                db,
                club_id=game.club_id,
                post_type=db.get(Post, job.post_id).post_type,
                match_weekday=new_weekday,
                team_id=game.team_id,
                game_id=game.id,
            )
            candidates = [
                slot
                for slot in resolution.slots
                if slot.media_kind == ("story" if job.kind == "story" else "feed")
                and slot.variant_number == current_slot.variant_number
                and (
                    not current_slot.legacy_story_rule_id
                    or slot.legacy_story_rule_id == current_slot.legacy_story_rule_id
                )
            ]
            replacement = candidates[0] if candidates else None
            scheduled_at = (
                calculate_publication_time(replacement, game) if replacement else None
            )
            if replacement and scheduled_at:
                job.publication_rule_slot_id = replacement.id
                job.scheduled_at = scheduled_at
                job.absolute_time = replacement.timing_model == "weekday_fixed"
                job.schedule_source = resolution.source
                job.stale_time = False
                job.error = "Spielverlegung erkannt; Termin anhand der neuen Regel berechnet"
            else:
                job.publication_rule_slot_id = None
                job.status = JobStatus.DRAFT
                job.approval_status = "manual_schedule_required"
                job.approved_post_version = None
                job.schedule_source = "manual_required"
                job.stale_time = True
                job.error = (
                    "Für den neuen Spiel-Wochentag fehlt eine passende Regel; "
                    "Veröffentlichungszeit bitte manuell festlegen"
                )
            job.version += 1
        elif job.absolute_time:
            job.stale_time = True
        else:
            job.scheduled_at += new_kickoff - old
            job.version += 1
        if job.approval_status == "approved":
            job.status = JobStatus.UNAPPROVED
            job.approval_status = "reapproval_required"
            job.approved_post_version = None
    for post in db.scalars(
        select(Post).where(
            Post.id.in_(affected_posts),
            Post.status.in_(
                [PostStatus.APPROVED, PostStatus.SCHEDULED, PostStatus.PARTIAL]
            ),
        )
    ):
        post.status = PostStatus.REAPPROVAL
        post.approved_version = None
        post.approved_by = None
        post.approved_at = None
        post.version += 1
    db.commit()
