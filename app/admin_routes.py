import hashlib
import mimetypes
import secrets
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from PIL import Image, UnidentifiedImageError
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.approvals.service import ApprovalError, approve_matchday_bundle, edit_text
from app.auth.service import allowed, hash_password, normalize_email, validate_new_password
from app.branding.service import (
    STANDARD_FONTS,
    BrandingValidationError,
    branding_completion,
    branding_form_state,
    default_branding_settings,
    dynamic_text_examples,
    normalize_colors,
    normalize_hashtags,
    normalize_mentions,
    normalize_string_list,
    parse_structured_json,
    recommended_branding_settings,
    validate_branding_settings,
)
from app.channels.service import sync_instagram_channel
from app.config import get_settings
from app.db import get_db
from app.file_delivery import detached_file_response
from app.games.bundles import connect_games, dashboard_game_groups, separate_games
from app.games.identity import (
    TeamIdentityError,
    team_name_variants,
    validate_identity_aliases,
)
from app.games.overview import build_game_automation_summary
from app.limits.service import LimitExceeded, assert_resource_capacity, effective_limits
from app.logos.service import (
    LogoValidationError,
    import_shared_opponent_logo,
    normalize_club_name,
    opponent_name,
    publish_shared_opponent_logo,
    refresh_pending_generation_logo_snapshots,
    shared_logo_path,
    store_logo,
)
from app.media.library import (
    CONTRIBUTION_TYPE_LABELS,
    MEDIA_CATEGORIES,
    MEDIA_CATEGORY_LABELS,
    SAFE_DEFAULT_POLICIES,
    MediaLibraryError,
    effective_policy,
    release_asset,
    save_policy,
    set_automatic_usage,
    set_game_preference,
    soft_delete_asset,
    usage_status,
)
from app.media.storage import LocalStorageProvider, StorageError, media_asset_path
from app.media.uploads import (
    MAX_PLAYER_IMAGE_BYTES,
    MAX_PLAYER_IMAGE_FILES,
    PlayerImageUploadError,
    ValidatedPlayerImage,
    iter_player_images_from_zip,
    move_uploaded_media_to_team,
    store_player_image,
    validate_player_image,
)
from app.models import (
    AuditLog,
    Club,
    ClubBrandingConfiguration,
    ContentRuleSet,
    DesignTemplate,
    FontAsset,
    FussballSyncState,
    Game,
    GameMediaPreference,
    GeneratedMediaSlot,
    GeneratedMediaVersion,
    GenerationJob,
    GenerationJobStatus,
    InstagramPage,
    JobStatus,
    LogoAsset,
    MediaAsset,
    MediaUsageHistory,
    MetaPublishingAttempt,
    Post,
    PostChannelContent,
    PostStatus,
    PostTextVersion,
    PromptStatus,
    PromptTemplate,
    PromptTestRun,
    PublicationJob,
    PublicationMediaItem,
    PublicationRuleSlot,
    Role,
    SharedOpponentLogo,
    SocialChannelConnection,
    StoryRule,
    Team,
    TeamChannelAssignment,
    User,
    UserTeam,
    WhatsAppMessageTemplate,
    WhatsAppRecipient,
)
from app.platform.service import platform_audit
from app.posts.automation import (
    RECOMMENDED_AUTOMATION_PRESET,
    RESULT_POLL_MINUTES_MIN,
    RESULT_POLL_MINUTES_RECOMMENDED,
    apply_recommended_preset,
    automatic_rule_label,
    build_schedule_preview,
    generation_summary,
    selection_summary,
)
from app.posts.club_carousel import (
    ClubCarouselConflict,
    matchday_bundle_jobs,
    reorder_matchday_carousel,
)
from app.posts.manual import (
    MAX_MANUAL_IMAGE_BYTES,
    ManualPostError,
    create_manual_post,
    parse_manual_crop_specs,
    parse_manual_publication_time,
    parse_manual_user_tag_specs,
    validate_manual_image,
)
from app.posts.media_versions import (
    MediaVersionError,
    post_media_catalog,
    select_latest_media_automatically,
    select_latest_text_automatically,
    select_media_version,
    select_publication_media_variant,
    select_text_version,
)
from app.posts.rules import sync_team_rule_sets
from app.posts.service import PARTIAL_GENERATION_WARNING, logo_recompose_availability
from app.publishing.presentation import (
    APPROVAL_LABELS,
    JOB_STATUS_LABELS,
    group_views_by_channel,
    operational_channels,
    publication_views,
)
from app.publishing.schedule import (
    EDITABLE_JOB_STATUSES,
    PublicationScheduleError,
    reschedule_publication_job,
)
from app.storage.service import (
    StorageQuotaError,
    commit_local_media_upload,
    format_storage_gb,
    mark_local_media_deleted,
    move_local_media_storage_object,
    reserve_storage,
    storage_usage,
)
from app.teams.service import (
    derived_team_short_name,
    ensure_team_media_namespace,
    unique_team_slug,
)
from app.textgen.service import sanitize_generated_caption
from app.web import (
    berlin_datetime,
    check_csrf,
    csrf_token,
    current_user,
    require,
    require_admin,
    require_platform_admin,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["berlin"] = berlin_datetime
settings = get_settings()
templates.env.globals["environment"] = settings.environment
templates.env.globals["meta_test_enabled"] = settings.meta_test_enabled

WEEKDAY_LABELS = (
    "Montag",
    "Dienstag",
    "Mittwoch",
    "Donnerstag",
    "Freitag",
    "Samstag",
    "Sonntag",
)


def render(request, name, current, **context):
    return templates.TemplateResponse(
        request, name, {"user": current, "csrf": csrf_token(request), **context}
    )


def audit(db, current, action, entity, entity_id=None, team_id=None, details=None):
    db.add(
        AuditLog(
            user_id=current.id,
            team_id=team_id,
            action=action,
            entity_type=entity,
            entity_id=entity_id,
            details=details or {},
        )
    )


def redirect(path, message="Gespeichert"):
    separator = "&" if "?" in path else "?"
    return RedirectResponse(f"{path}{separator}notice={message}", 303)


def _structured_list(value: str) -> list[str]:
    return [item.strip() for item in value.replace(",", "\n").splitlines() if item.strip()]


def _structured_values(values: list[str]) -> list[str]:
    """Accept both repeated form fields and legacy comma/newline separated values."""
    return [item for value in values for item in _structured_list(value)]


def _branding_venues(db: Session, teams: list[Team], selected: str = "") -> list[str]:
    own_names: set[str] = set()
    for team in teams:
        for value in (team.display_name, team.internal_name, team.short_name, team.club):
            own_names.update(team_name_variants(value or ""))
    venues = {
        game.venue.strip()
        for game in db.scalars(select(Game).where(Game.venue.is_not(None))).all()
        if game.venue
        and game.venue.strip()
        and bool(team_name_variants(game.home_team) & own_names)
    }
    if selected:
        venues.add(selected)
    return sorted(venues, key=str.casefold)


def _branding_team_rows(teams: list[Team], configured: list[dict]) -> list[dict]:
    by_id = {str(item.get("team_id")): item for item in configured}
    return [
        {
            "team_id": team.id,
            "stored_name": team.display_name,
            "display_name": by_id.get(team.id, {}).get("display_name") or team.display_name,
            "short_name": by_id.get(team.id, {}).get("short_name") or team.short_name,
            "active": by_id.get(team.id, {}).get("active", True),
        }
        for team in teams
    ]


@router.get("/branding", response_class=HTMLResponse)
def club_branding(
    request: Request,
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    require_admin(current)
    if not current.club_id:
        raise HTTPException(403, "Ein eindeutiger Verein ist erforderlich")
    club = db.get(Club, current.club_id)
    if club is None:
        raise HTTPException(404)
    config = db.get(ClubBrandingConfiguration, club.id)
    teams = db.scalars(
        select(Team)
        .where(Team.club_id == club.id, Team.archived_at.is_(None))
        .order_by(Team.display_name)
    ).all()
    fonts = db.scalars(
        select(FontAsset)
        .where(
            FontAsset.club_id == club.id,
            FontAsset.active.is_(True),
            FontAsset.archived_at.is_(None),
        )
        .order_by(FontAsset.name)
    ).all()
    logos = db.scalars(
        select(LogoAsset)
        .where(
            LogoAsset.club_id == club.id,
            LogoAsset.logo_type == "team",
            LogoAsset.active.is_(True),
            LogoAsset.archived_at.is_(None),
        )
        .order_by(LogoAsset.display_name, LogoAsset.version.desc())
    ).all()
    media_assets = db.scalars(
        select(MediaAsset)
        .where(
            MediaAsset.club_id == club.id,
            MediaAsset.active.is_(True),
            MediaAsset.available.is_(True),
        )
        .order_by(MediaAsset.filename)
    ).all()
    image, text = branding_form_state(
        (config.image_settings if config else {}) or {},
        (config.text_settings if config else {}) or {},
    )
    team_rows = _branding_team_rows(teams, text.get("team_names") or [])
    selected_team = next((row for row in team_rows if row["active"]), None)
    venues = _branding_venues(db, teams, text.get("home_venue") or "")
    examples = dynamic_text_examples(
        club_name=club.name,
        club_short_name=club.short_name,
        venue=text.get("home_venue_short") or text.get("home_venue"),
        team_name=selected_team["display_name"] if selected_team else None,
        text=text,
    )
    current_logo = db.get(LogoAsset, club.logo_asset_id) if club.logo_asset_id else None
    if current_logo and (
        current_logo.archived_at or not current_logo.active or current_logo.logo_type != "team"
    ):
        current_logo = None
    progress = branding_completion(
        club_name=club.name,
        has_logo=current_logo is not None,
        primary_font_id=config.primary_font_id if config else None,
        secondary_font_id=config.secondary_font_id if config else None,
        image=image,
        text=text,
    )
    media_policies = {}
    for contribution_type in CONTRIBUTION_TYPE_LABELS:
        ordered_categories = effective_policy(db, club.id, contribution_type)
        media_policies[contribution_type] = {
            "allowed": ordered_categories,
            "primary": ordered_categories[0],
        }
    return render(
        request,
        "branding.html",
        current,
        club=club,
        config=config,
        image=image,
        text=text,
        fonts=fonts,
        standard_fonts=STANDARD_FONTS,
        logos=logos,
        current_logo=current_logo,
        media_assets=media_assets,
        teams=teams,
        team_rows=team_rows,
        venues=venues,
        examples=examples,
        progress=progress,
        media_policies=media_policies,
        media_categories=MEDIA_CATEGORIES,
        media_category_labels=MEDIA_CATEGORY_LABELS,
        contribution_type_labels=CONTRIBUTION_TYPE_LABELS,
        title="Vereinsbranding",
    )


@router.post("/branding")
def update_club_branding(
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    version: int = Form(),
    club_version: int = Form(),
    action: str = Form(default="save"),
    club_logo_id: str = Form(default=""),
    primary_color: str = Form(default="#172554"),
    secondary_color: str = Form(default="#ffffff"),
    accent_colors: list[str] = Form(default=[]),
    graphic_style: str = Form(default=""),
    image_effect: str = Form(default=""),
    image_effects: list[str] = Form(default=[]),
    background_style: str = Form(default=""),
    text_alignment: str = Form(default=""),
    logo_placement: str = Form(default=""),
    safe_margins: str = Form(default=""),
    player_position: str = Form(default=""),
    allowed_elements: list[str] = Form(default=[]),
    unwanted_elements: list[str] = Form(default=[]),
    sponsor_rules: str = Form(default=""),
    forbidden_colors: list[str] = Form(default=[]),
    feed_rules: str = Form(default=""),
    story_rules: str = Form(default=""),
    image_text_amount: str = Form(default=""),
    player_background_ratio: str = Form(default=""),
    dynamics: str = Form(default=""),
    individualization: str = Form(default=""),
    address_style: str = Form(default=""),
    tone: str = Form(default=""),
    text_length: str = Form(default=""),
    emoji_usage: str = Form(default=""),
    hashtags: list[str] = Form(default=[]),
    mentions: list[str] = Form(default=[]),
    typical_phrases: list[str] = Form(default=[]),
    unwanted_phrases: list[str] = Form(default=[]),
    team_name_spelling: str = Form(default=""),
    home_label: str = Form(default=""),
    away_label: str = Form(default=""),
    call_to_action: str = Form(default=""),
    cta_type: str = Form(default="none"),
    cta_custom: str = Form(default=""),
    home_venue: str = Form(default=""),
    home_venue_short: str = Form(default=""),
    team_names_json: str = Form(default="[]"),
    sponsors_json: str = Form(default="[]"),
    legacy_image_json: str = Form(default="{}"),
    legacy_text_json: str = Form(default="{}"),
    sponsor_mentions: str = Form(default=""),
    max_hashtags: int = Form(default=10),
    primary_font_id: str = Form(default=""),
    secondary_font_id: str = Form(default=""),
    primary_font_choice: str = Form(default=""),
    secondary_font_choice: str = Form(default=""),
    feed_max_text_amount: str = Form(default="normal"),
    feed_use_player_image: str = Form(default=""),
    feed_show_sponsors: str = Form(default=""),
    feed_show_club_logo: str = Form(default=""),
    feed_highlight_result: str = Form(default=""),
    feed_extra_rules: str = Form(default=""),
    result_image_fields: list[str] = Form(default=[]),
    result_image_extra_rules: str = Form(default=""),
    story_safe_top: int = Form(default=12),
    story_safe_bottom: int = Form(default=15),
    story_use_player_image: str = Form(default=""),
    story_show_sponsors: str = Form(default=""),
    story_show_club_logo: str = Form(default=""),
    story_show_call_to_action: str = Form(default=""),
    story_countdown_area: str = Form(default=""),
    story_extra_rules: str = Form(default=""),
    media_allowed_announcement: list[str] = Form(default=[]),
    media_primary_announcement: str = Form(default="match_photo"),
    media_allowed_reminder: list[str] = Form(default=[]),
    media_primary_reminder: str = Form(default="match_photo"),
    media_allowed_result: list[str] = Form(default=[]),
    media_primary_result: str = Form(default="match_photo"),
    media_allowed_live: list[str] = Form(default=[]),
    media_primary_live: str = Form(default="player_portrait"),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    if not current.club_id:
        raise HTTPException(403, "Ein eindeutiger Verein ist erforderlich")
    statement = select(ClubBrandingConfiguration).where(
        ClubBrandingConfiguration.club_id == current.club_id
    )
    if db.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    config = db.scalar(statement)
    current_version = config.version if config else 0
    if version != current_version:
        raise HTTPException(409, "Das Vereinsbranding wurde zwischenzeitlich geändert")
    club_statement = select(Club).where(Club.id == current.club_id)
    if db.bind.dialect.name == "postgresql":
        club_statement = club_statement.with_for_update()
    club = db.scalar(club_statement)
    if club is None:
        raise HTTPException(404)
    if club.version != club_version:
        raise HTTPException(409, "Der Verein wurde zwischenzeitlich geändert")

    def resolve_font_choice(choice: str, legacy_id: str) -> tuple[str | None, str]:
        value = (choice or legacy_id or "standard:system").strip()
        if value.startswith("standard:"):
            key = value.removeprefix("standard:")
            if key not in STANDARD_FONTS:
                raise HTTPException(422, "Unbekannte Standardschrift")
            return None, key
        asset_id = value.removeprefix("asset:")
        if not asset_id:
            return None, "system"
        return asset_id, "system"

    resolved_primary_font_id, primary_standard_font = resolve_font_choice(
        primary_font_choice, primary_font_id
    )
    resolved_secondary_font_id, secondary_standard_font = resolve_font_choice(
        secondary_font_choice, secondary_font_id
    )
    if action == "reset":
        resolved_primary_font_id = resolved_secondary_font_id = None
        primary_standard_font = secondary_standard_font = "system"
    font_ids = [value for value in (resolved_primary_font_id, resolved_secondary_font_id) if value]
    if font_ids:
        valid_fonts = set(
            db.scalars(
                select(FontAsset.id).where(
                    FontAsset.club_id == current.club_id,
                    FontAsset.id.in_(font_ids),
                    FontAsset.active.is_(True),
                    FontAsset.archived_at.is_(None),
                )
            )
        )
        if valid_fonts != set(font_ids):
            raise HTTPException(422, "Mindestens eine Schriftart gehört nicht zu diesem Verein")
    current_image = (config.image_settings if config else {}) or {}
    current_text = (config.text_settings if config else {}) or {}
    try:
        if action == "recommended":
            image_settings, text_settings = recommended_branding_settings(
                current_image, current_text
            )
        elif action == "reset":
            image_settings, text_settings = default_branding_settings(current_image, current_text)
        elif action == "save":
            normalized_image_effects = normalize_string_list(_structured_values(image_effects))
            image_settings = {
                "primary_color": primary_color,
                "secondary_color": secondary_color,
                "accent_colors": normalize_colors(_structured_values(accent_colors)),
                "graphic_style": graphic_style,
                "image_effect": ", ".join(normalized_image_effects) or image_effect,
                "image_effects": normalized_image_effects,
                "background_style": background_style,
                "text_alignment": text_alignment,
                "logo_placement": logo_placement,
                "safe_margins": safe_margins,
                "player_position": player_position,
                "allowed_elements": normalize_string_list(_structured_values(allowed_elements)),
                "unwanted_elements": normalize_string_list(_structured_values(unwanted_elements)),
                "sponsor_rules": normalize_string_list(_structured_list(sponsor_rules)),
                "forbidden_colors": normalize_colors(_structured_values(forbidden_colors)),
                "feed_rules": feed_extra_rules or feed_rules,
                "story_rules": story_extra_rules or story_rules,
                "feed_settings": {
                    "max_text_amount": feed_max_text_amount,
                    "use_player_image": feed_use_player_image == "on",
                    "show_sponsors": feed_show_sponsors == "on",
                    "show_club_logo": feed_show_club_logo == "on",
                    "highlight_result": feed_highlight_result == "on",
                    "extra_rules": feed_extra_rules,
                },
                "result_image_fields": result_image_fields,
                "result_image_extra_rules": result_image_extra_rules,
                "story_settings": {
                    "safe_top": story_safe_top,
                    "safe_bottom": story_safe_bottom,
                    "use_player_image": story_use_player_image == "on",
                    "show_sponsors": story_show_sponsors == "on",
                    "show_club_logo": story_show_club_logo == "on",
                    "show_call_to_action": story_show_call_to_action == "on",
                    "countdown_area": story_countdown_area == "on",
                    "extra_rules": story_extra_rules,
                },
                "image_text_amount": image_text_amount,
                "player_background_ratio": player_background_ratio,
                "dynamics": dynamics,
                "individualization": individualization,
                "primary_standard_font": primary_standard_font,
                "secondary_standard_font": secondary_standard_font,
                "legacy_values": parse_structured_json(legacy_image_json, "Übernommene Bildwerte"),
            }
            text_settings = {
                "address_style": address_style,
                "tone": tone,
                "text_length": text_length,
                "emoji_usage": emoji_usage,
                "hashtags": normalize_hashtags(_structured_values(hashtags)),
                "mentions": normalize_mentions(_structured_values(mentions)),
                "typical_phrases": normalize_string_list(_structured_values(typical_phrases)),
                "unwanted_phrases": normalize_string_list(_structured_values(unwanted_phrases)),
                "team_name_spelling": team_name_spelling,
                "team_names": parse_structured_json(team_names_json, "Mannschaftsschreibweisen"),
                "home_label": home_label,
                "away_label": away_label,
                "home_venue": home_venue,
                "home_venue_short": home_venue_short,
                "call_to_action": cta_custom if cta_type == "custom" else "",
                "cta_type": cta_type,
                "cta_custom": cta_custom,
                "sponsors": parse_structured_json(sponsors_json, "Sponsoren"),
                "sponsor_mentions": normalize_mentions(_structured_list(sponsor_mentions)),
                "max_hashtags": max_hashtags,
                "legacy_values": parse_structured_json(legacy_text_json, "Übernommene Textwerte"),
            }
            image_settings = validate_branding_settings(image_settings, strict_choices=True)
            text_settings = validate_branding_settings(text_settings, strict_choices=True)
        else:
            raise BrandingValidationError("Unbekannte Branding-Aktion")
    except BrandingValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    teams = db.scalars(
        select(Team).where(
            Team.club_id == current.club_id,
            Team.archived_at.is_(None),
        )
    ).all()
    team_ids = {team.id for team in teams}
    configured_team_ids = {item["team_id"] for item in text_settings.get("team_names", [])}
    if not configured_team_ids.issubset(team_ids):
        raise HTTPException(422, "Mindestens eine Mannschaft gehört nicht zu diesem Verein")
    sponsor_team_ids = {
        team_id
        for sponsor in text_settings.get("sponsors", [])
        for team_id in sponsor.get("team_ids", [])
    }
    if not sponsor_team_ids.issubset(team_ids):
        raise HTTPException(422, "Sponsor enthält eine fremde Mannschaft")
    sponsor_media_ids = {
        sponsor["media_asset_id"]
        for sponsor in text_settings.get("sponsors", [])
        if sponsor.get("media_asset_id")
    }
    if sponsor_media_ids:
        valid_media_ids = set(
            db.scalars(
                select(MediaAsset.id).where(
                    MediaAsset.club_id == club.id,
                    MediaAsset.id.in_(sponsor_media_ids),
                    MediaAsset.active.is_(True),
                    MediaAsset.available.is_(True),
                )
            )
        )
        if valid_media_ids != sponsor_media_ids:
            raise HTTPException(422, "Mindestens ein Sponsorenmedium gehört nicht zum Verein")
    venues = _branding_venues(db, teams, current_text.get("home_venue") or "")
    if text_settings.get("home_venue") and text_settings["home_venue"] not in venues:
        raise HTTPException(422, "Die Heimspielstätte gehört nicht zum Verein")
    selected_logo = db.get(LogoAsset, club_logo_id) if club_logo_id else None
    if club_logo_id and selected_logo is None:
        raise HTTPException(422, "Das Vereinslogo gehört nicht zum Verein")
    if selected_logo and (
        selected_logo.club_id != current.club_id
        or selected_logo.logo_type != "team"
        or not selected_logo.active
        or selected_logo.archived_at is not None
    ):
        raise HTTPException(422, "Das Vereinslogo gehört nicht zum Verein")
    if config is None:
        config = ClubBrandingConfiguration(club_id=current.club_id)
        db.add(config)
    else:
        config.version += 1
    if action != "save":
        image_settings["primary_standard_font"] = primary_standard_font
        image_settings["secondary_standard_font"] = secondary_standard_font
    config.image_settings = image_settings
    config.text_settings = text_settings
    config.primary_font_id = resolved_primary_font_id
    config.secondary_font_id = resolved_secondary_font_id
    config.updated_by = current.id
    selected_logo_id = selected_logo.id if selected_logo else None
    if club.logo_asset_id != selected_logo_id:
        club.logo_asset_id = selected_logo_id
        club.version += 1
    posted_media_policies = {
        "announcement": (media_allowed_announcement, media_primary_announcement),
        "reminder": (media_allowed_reminder, media_primary_reminder),
        "result": (media_allowed_result, media_primary_result),
        "live": (media_allowed_live, media_primary_live),
    }
    # Older clients and forms created before the media-library extension do
    # not submit these fields. Preserve the tenant's effective policy instead
    # of rejecting an otherwise valid branding update or resetting it.
    posted_media_policies = {
        contribution_type: (
            values
            if values[0]
            else (
                effective_policy(db, current.club_id, contribution_type),
                effective_policy(db, current.club_id, contribution_type)[0],
            )
        )
        for contribution_type, values in posted_media_policies.items()
    }
    if action in {"reset", "recommended"}:
        posted_media_policies = {
            contribution_type: (list(categories), categories[0])
            for contribution_type, categories in SAFE_DEFAULT_POLICIES.items()
        }
    try:
        saved_media_policies = {
            contribution_type: save_policy(
                db,
                club_id=current.club_id,
                contribution_type=contribution_type,
                allowed_categories=allowed_categories,
                primary_category=primary_category,
                actor_user_id=current.id,
            )
            for contribution_type, (
                allowed_categories,
                primary_category,
            ) in posted_media_policies.items()
        }
    except MediaLibraryError as exc:
        raise HTTPException(422, str(exc)) from exc
    audit(
        db,
        current,
        "branding.updated",
        "club_branding_configuration",
        current.club_id,
        details={
            "version": config.version,
            "image_keys": sorted(image_settings),
            "text_keys": sorted(text_settings),
            "media_usage_policies": {
                contribution_type: {
                    "allowed": policy.allowed_media_categories,
                    "priority": policy.category_priority,
                }
                for contribution_type, policy in saved_media_policies.items()
            },
        },
    )
    db.commit()
    messages = {
        "save": "Vereinsbranding wurde gespeichert",
        "recommended": "Empfohlene Branding-Einstellungen wurden übernommen",
        "reset": "Branding wurde auf Standardwerte zurückgesetzt",
    }
    return redirect("/branding", messages[action])


def _invalidate_posts_for_logo_change(db: Session, game: Game, reason: str) -> list[str]:
    affected = []
    for post in db.scalars(select(Post).where(Post.game_id == game.id)).all():
        if post.status in {PostStatus.PUBLISHED, PostStatus.CANCELLED}:
            continue
        post.version += 1
        post.approved_version = None
        post.status = PostStatus.REAPPROVAL
        warning = (
            "Logo-Zuordnung wurde geändert; Grafiken mit aktualisierten "
            "Logo-Referenzen neu erzeugen"
        )
        post.critical_warnings = list(dict.fromkeys([*(post.critical_warnings or []), warning]))
        for publication in db.scalars(
            select(PublicationJob).where(
                PublicationJob.post_id == post.id,
                PublicationJob.status != JobStatus.PUBLISHED,
            )
        ):
            publication.status = JobStatus.UNAPPROVED
            publication.approval_status = "reapproval_required"
            publication.approved_post_version = None
            publication.error = reason
        affected.append(post.id)
    return affected


def _invalidate_posts_for_result_change(db: Session, game: Game, reason: str) -> list[str]:
    """Revoke open result approvals, including shared matchday carousels."""
    affected: list[str] = []
    candidates = db.scalars(select(Post).where(Post.post_type == "result")).all()
    for post in candidates:
        bundle = (post.design_snapshot or {}).get("club_matchday_carousel") or {}
        if post.game_id != game.id and game.id not in (bundle.get("game_ids") or []):
            continue
        if post.status in {PostStatus.PUBLISHED, PostStatus.CANCELLED}:
            continue
        post.version += 1
        post.approved_version = None
        post.approved_by = None
        post.approved_at = None
        post.status = PostStatus.REAPPROVAL
        warning = "Bestätigtes Spielergebnis wurde geändert; erneute Prüfung erforderlich"
        post.critical_warnings = list(dict.fromkeys([*(post.critical_warnings or []), warning]))
        for publication in db.scalars(
            select(PublicationJob).where(PublicationJob.post_id == post.id)
        ):
            if publication.status in {
                JobStatus.PUBLISHED,
                JobStatus.CANCELLED,
                JobStatus.SKIPPED,
            }:
                continue
            publication.status = JobStatus.UNAPPROVED
            publication.approval_status = "reapproval_required"
            publication.approved_post_version = None
            publication.error = reason
        affected.append(post.id)
    return affected


def _audit_logo_approval_revocations(
    db: Session,
    current: User,
    team_id: str,
    game_id: str,
    post_ids: list[str],
    reason: str,
) -> None:
    for post_id in dict.fromkeys(post_ids):
        audit(
            db,
            current,
            "post.approval_revoked_logo_change",
            "post",
            post_id,
            team_id,
            {"game_id": game_id, "reason": reason},
        )


@router.get("/teams", response_class=HTMLResponse)
def teams(request: Request, current=Depends(current_user), db: Session = Depends(get_db)):
    require(current, db, "view")
    items = db.scalars(
        select(Team).where(Team.archived_at.is_(None)).order_by(Team.display_name)
    ).all()
    available_channels = list(
        db.scalars(
            select(SocialChannelConnection)
            .where(
                SocialChannelConnection.club_id == current.club_id,
                SocialChannelConnection.active.is_(True),
                SocialChannelConnection.status == "connected",
                SocialChannelConnection.disconnected_at.is_(None),
            )
            .order_by(
                SocialChannelConnection.channel_type,
                SocialChannelConnection.display_name,
            )
        )
    )
    team_channel_labels: dict[str, list[str]] = {item.id: [] for item in items}
    for assignment, connection in db.execute(
        select(TeamChannelAssignment, SocialChannelConnection)
        .join(
            SocialChannelConnection,
            SocialChannelConnection.id == TeamChannelAssignment.channel_connection_id,
        )
        .where(TeamChannelAssignment.enabled.is_(True))
        .where(TeamChannelAssignment.club_id == current.club_id)
        .order_by(
            TeamChannelAssignment.team_id,
            SocialChannelConnection.channel_type,
            SocialChannelConnection.display_name,
        )
    ):
        label = connection.channel_type.capitalize()
        account = (
            f"@{connection.username.lstrip('@')}"
            if connection.username
            else connection.display_phone_number or connection.display_name
        )
        team_channel_labels.setdefault(assignment.team_id, []).append(f"{label} · {account}")
    logos = {
        item.id: db.get(LogoAsset, item.logo_asset_id) if item.logo_asset_id else None
        for item in items
    }
    logo_versions = {
        item.id: db.scalars(
            select(LogoAsset)
            .where(
                LogoAsset.logo_type == "team",
                LogoAsset.team_id == item.id,
                LogoAsset.active.is_(True),
                LogoAsset.archived_at.is_(None),
            )
            .order_by(LogoAsset.version.desc())
        ).all()
        for item in items
    }
    uploader_ids = {logo.uploaded_by for versions in logo_versions.values() for logo in versions}
    uploaders = (
        {
            user.id: user.email
            for user in db.scalars(select(User).where(User.id.in_(uploader_ids))).all()
        }
        if uploader_ids
        else {}
    )
    return render(
        request,
        "teams.html",
        current,
        items=items,
        available_channels=available_channels,
        team_channel_labels=team_channel_labels,
        logos=logos,
        logo_versions=logo_versions,
        uploaders=uploaders,
        title="Mannschaften",
    )


@router.post("/teams")
def create_team(
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    internal_name: str = Form(),
    display_name: str = Form(),
    club: str = Form(),
    fussball_url: str = Form(),
    channel_connection_ids: list[str] = Form(default=[]),
    # Accepted for backwards-compatible clients, but no longer required by
    # the dashboard. Technical values are generated on the server.
    short_name: str = Form(default=""),
    slug: str = Form(default=""),
    instagram_page_id: str = Form(default=""),
    media_subdir: str = Form(default=""),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    try:
        assert_resource_capacity(db, current.club_id, "teams")
    except LimitExceeded as exc:
        audit(
            db, current, "team.limit_blocked", "club", current.club_id, details={"reason": str(exc)}
        )
        db.commit()
        raise HTTPException(409, str(exc)) from exc
    if not fussball_url.startswith(("https://www.fussball.de/", "https://fussball.de/")):
        raise HTTPException(422, "Ungültige FUSSBALL.DE-URL")
    requested_channel_ids = list(dict.fromkeys(channel_connection_ids))
    if instagram_page_id:
        page = db.get(InstagramPage, instagram_page_id)
        if not page or page.club_id != current.club_id or not page.active:
            raise HTTPException(422, "Instagram-Seite muss aktiv sein")
        legacy_channel = sync_instagram_channel(db, page)
        db.flush()
        if legacy_channel.id not in requested_channel_ids:
            requested_channel_ids.append(legacy_channel.id)

    connections = (
        list(
            db.scalars(
                select(SocialChannelConnection).where(
                    SocialChannelConnection.id.in_(requested_channel_ids),
                    SocialChannelConnection.club_id == current.club_id,
                    SocialChannelConnection.active.is_(True),
                    SocialChannelConnection.disconnected_at.is_(None),
                )
            )
        )
        if requested_channel_ids
        else []
    )
    if len(connections) != len(requested_channel_ids):
        raise HTTPException(422, "Mindestens ein ausgewählter Kanal ist nicht verfügbar")
    explicit_channel_ids = set(channel_connection_ids)
    if any(
        connection.id in explicit_channel_ids and connection.status != "connected"
        for connection in connections
    ):
        raise HTTPException(422, "Ausgewählte Kanäle müssen vollständig verbunden sein")

    technical_slug = unique_team_slug(db, current.club_id, slug or internal_name or display_name)
    legacy_instagram_page_id = next(
        (
            connection.legacy_instagram_page_id
            for connection in connections
            if connection.channel_type == "instagram" and connection.legacy_instagram_page_id
        ),
        None,
    )
    item = Team(
        club_id=current.club_id,
        internal_name=internal_name,
        display_name=display_name,
        short_name=short_name.strip()[:30] or derived_team_short_name(internal_name, display_name),
        slug=technical_slug,
        club=club,
        fussball_url=fussball_url,
        instagram_page_id=legacy_instagram_page_id,
        media_subdir="pending",
    )
    db.add(item)
    db.flush()
    try:
        item.media_subdir = ensure_team_media_namespace(
            settings.upload_root,
            club_id=current.club_id,
            team_id=item.id,
            slug=item.slug,
        )
    except (OSError, ValueError) as exc:
        db.rollback()
        raise HTTPException(
            503, "Der automatische Medienbereich konnte nicht angelegt werden"
        ) from exc

    for connection in connections:
        db.add(
            TeamChannelAssignment(
                club_id=current.club_id,
                team_id=item.id,
                channel_connection_id=connection.id,
                enabled=True,
                announcement_enabled=True,
                result_enabled=True,
                story_enabled=connection.channel_type == "instagram",
            )
        )
    audit(
        db,
        current,
        "team.created",
        "team",
        item.id,
        item.id,
        {
            "channel_types": sorted({connection.channel_type for connection in connections}),
            "managed_media_namespace": True,
        },
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "Die Mannschaft konnte nicht eindeutig angelegt werden") from exc
    return redirect("/teams")


@router.post("/teams/{team_id}/state")
def team_state(
    team_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    action: str = Form(),
    version: int = Form(),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    item = db.get(Team, team_id)
    if not item:
        raise HTTPException(404)
    if item.version != version:
        raise HTTPException(409, "Datensatz wurde zwischenzeitlich geändert")
    if action == "archive":
        item.archived_at = datetime.now(timezone.utc)
        item.active = False
    elif action == "toggle":
        if not item.active:
            try:
                assert_resource_capacity(db, current.club_id, "teams")
            except LimitExceeded as exc:
                audit(
                    db,
                    current,
                    "team.reactivation_limit_blocked",
                    "team",
                    item.id,
                    item.id,
                    {"reason": str(exc)},
                )
                db.commit()
                raise HTTPException(409, str(exc)) from exc
        item.active = not item.active
    else:
        raise HTTPException(422, "Unbekannte Aktion")
    item.version += 1
    audit(db, current, f"team.{action}", "team", item.id, item.id)
    db.commit()
    return redirect("/teams")


@router.post("/teams/{team_id}/logo")
async def upload_team_logo(
    team_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    file: UploadFile = File(),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    team = db.get(Team, team_id)
    if not team:
        raise HTTPException(404)
    old = db.get(LogoAsset, team.logo_asset_id) if team.logo_asset_id else None
    try:
        logo, created = store_logo(
            db,
            upload_root=settings.upload_root,
            logo_type="team",
            team_id=team.id,
            display_name=team.club,
            original_filename=file.filename or "logo",
            content_type=file.content_type,
            data=await file.read(),
            uploaded_by=current.id,
            club_id=team.club_id,
            team_slug=team.slug,
        )
    except LogoValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    team.logo_asset_id = logo.id
    team.logo_path = None
    team.version += 1
    affected = []
    if not old or old.id != logo.id:
        for game in db.scalars(select(Game).where(Game.team_id == team.id)).all():
            reason = "Mannschaftslogo wurde geändert; erneute Freigabe erforderlich"
            game_posts = _invalidate_posts_for_logo_change(db, game, reason)
            affected.extend(game_posts)
            _audit_logo_approval_revocations(db, current, team.id, game.id, game_posts, reason)
    audit(
        db,
        current,
        "team_logo.uploaded" if created else "team_logo.selected",
        "logo_asset",
        logo.id,
        team.id,
        {
            "old_logo": {"id": old.id, "version": old.version} if old else None,
            "new_logo": {"id": logo.id, "version": logo.version},
            "affected_posts": affected,
        },
    )
    db.commit()
    return redirect("/teams", "Verifiziertes Mannschaftslogo gespeichert")


@router.post("/teams/{team_id}/logo/state")
def team_logo_state(
    team_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    action: str = Form(),
    logo_id: str = Form(default=""),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    team = db.get(Team, team_id)
    if not team:
        raise HTTPException(404)
    old = db.get(LogoAsset, team.logo_asset_id) if team.logo_asset_id else None
    logo = old
    if action == "select":
        selected = db.get(LogoAsset, logo_id)
        if (
            not selected
            or selected.logo_type != "team"
            or selected.team_id != team.id
            or not selected.active
            or selected.archived_at
        ):
            raise HTTPException(422, "Die gewählte Mannschaftslogoversion ist ungültig")
        team.logo_asset_id = selected.id
        logo = selected
    elif not logo:
        raise HTTPException(404)
    elif action == "deactivate":
        logo.active = False
        team.logo_asset_id = None
    elif action == "archive":
        logo.active = False
        logo.archived_at = datetime.now(timezone.utc)
        team.logo_asset_id = None
    else:
        raise HTTPException(422, "Unbekannte Logoaktion")
    team.version += 1
    affected = []
    if (old.id if old else None) != (team.logo_asset_id or None):
        for game in db.scalars(select(Game).where(Game.team_id == team.id)).all():
            reason = "Mannschaftslogo wurde geändert; erneute Freigabe erforderlich"
            game_posts = _invalidate_posts_for_logo_change(db, game, reason)
            affected.extend(game_posts)
            _audit_logo_approval_revocations(db, current, team.id, game.id, game_posts, reason)
    audit(
        db,
        current,
        f"team_logo.{action}",
        "logo_asset",
        logo.id,
        team.id,
        {
            "old_logo": {"id": old.id, "version": old.version} if old else None,
            "new_logo": ({"id": logo.id, "version": logo.version} if team.logo_asset_id else None),
            "affected_posts": affected,
        },
    )
    db.commit()
    return redirect("/teams", "Mannschaftslogo aktualisiert")


@router.get("/instagram", response_class=HTMLResponse)
def instagram(request: Request, current=Depends(current_user), db: Session = Depends(get_db)):
    require(current, db, "view")
    return RedirectResponse("/channels", 308)


@router.post("/instagram")
def create_instagram(
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    raise HTTPException(
        410,
        "Die manuelle Eingabe technischer Instagram-Daten wurde deaktiviert. "
        "Bitte den Assistenten unter Social-Media-Kanäle verwenden.",
    )


@router.post("/instagram/{page_id}/state")
def instagram_state(
    page_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    action: str = Form(),
    version: int = Form(),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    item = db.get(InstagramPage, page_id)
    if not item:
        raise HTTPException(404)
    if item.version != version:
        raise HTTPException(409, "Datensatz wurde zwischenzeitlich geändert")
    if action == "archive":
        item.archived_at = datetime.now(timezone.utc)
        item.active = False
        item.publishing_enabled = False
    elif (
        action == "mock-connect"
        and settings.publisher_mode != "live"
        and settings.environment != "meta-test"
    ):
        if not item.active:
            try:
                assert_resource_capacity(db, current.club_id, "instagram_pages")
            except LimitExceeded as exc:
                audit(
                    db,
                    current,
                    "instagram.reactivation_limit_blocked",
                    "instagram_page",
                    item.id,
                    details={"reason": str(exc)},
                )
                db.commit()
                raise HTTPException(409, str(exc)) from exc
        item.connection_status = "connected"
        item.active = True
        item.last_check_at = datetime.now(timezone.utc)
    elif action == "toggle-publishing":
        item.publishing_enabled = not item.publishing_enabled
    else:
        raise HTTPException(422, "Aktion im aktuellen Modus nicht zulässig")
    item.version += 1
    audit(db, current, f"instagram.{action}", "instagram_page", item.id)
    db.commit()
    return redirect("/instagram")


@router.get("/users", response_class=HTMLResponse)
def users(request: Request, current=Depends(current_user), db: Session = Depends(get_db)):
    require_admin(current)
    items = db.scalars(select(User).where(User.archived_at.is_(None)).order_by(User.email)).all()
    teams = db.scalars(select(Team).where(Team.archived_at.is_(None))).all()
    assignments = {(x.user_id, x.team_id) for x in db.scalars(select(UserTeam))}
    return render(
        request,
        "users.html",
        current,
        items=items,
        teams=teams,
        assignments=assignments,
        roles=[Role.ADMIN, Role.APPROVER, Role.EDITOR, Role.REVIEWER, Role.VIEWER],
        title="Benutzer und Rechte",
    )


@router.post("/users")
def create_user(
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    email: str = Form(),
    password: str = Form(),
    role: Role = Form(),
    all_teams: bool = Form(default=False),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    password_error = validate_new_password(password)
    if password_error:
        raise HTTPException(422, password_error)
    try:
        normalized_email = normalize_email(email)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    item = User(
        email=normalized_email,
        password_hash=hash_password(password),
        role=role,
        all_teams=all_teams,
        active=True,
        registration_status="approved",
        registration_reviewed_at=datetime.now(timezone.utc),
        registration_reviewed_by=current.id,
    )
    db.add(item)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "E-Mail-Adresse wird bereits verwendet") from exc
    audit(db, current, "user.created", "user", item.id, details={"role": role.value})
    db.commit()
    return redirect("/users")


@router.post("/users/{user_id}/registration")
def review_registration(
    user_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    action: str = Form(),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    item = db.get(User, user_id)
    if not item or item.archived_at is not None:
        raise HTTPException(404)
    if item.registration_status != "pending":
        raise HTTPException(409, "Registrierung wurde bereits bearbeitet")
    now = datetime.now(timezone.utc)
    if action == "approve":
        item.registration_status = "approved"
        item.active = True
        message = f"Registrierung von {item.email} wurde freigegeben"
        audit_action = "registration.approved"
    elif action == "reject":
        item.registration_status = "rejected"
        item.active = False
        item.auth_version += 1
        message = f"Registrierung von {item.email} wurde abgelehnt"
        audit_action = "registration.rejected"
    else:
        raise HTTPException(422, "Unbekannte Aktion")
    item.registration_reviewed_at = now
    item.registration_reviewed_by = current.id
    audit(db, current, audit_action, "user", item.id)
    db.commit()
    return redirect("/users", message)


@router.post("/users/{user_id}/teams")
def assign_user_teams(
    user_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    team_ids: list[str] = Form(default=[]),
    all_teams: bool = Form(default=False),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    item = db.get(User, user_id)
    if not item:
        raise HTTPException(404)
    if item.registration_status == "pending":
        raise HTTPException(409, "Registrierung muss zuerst freigegeben werden")
    db.query(UserTeam).filter(UserTeam.user_id == user_id).delete()
    item.all_teams = all_teams
    if not all_teams:
        for team_id in set(team_ids):
            if not db.get(Team, team_id):
                raise HTTPException(422, "Unbekannte Mannschaft")
            db.add(UserTeam(user_id=user_id, team_id=team_id))
    audit(
        db,
        current,
        "user.teams_changed",
        "user",
        user_id,
        details={"all_teams": all_teams, "teams": team_ids},
    )
    db.commit()
    return redirect("/users")


@router.post("/users/{user_id}/role")
def assign_user_role(
    user_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    role: Role = Form(),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    item = db.get(User, user_id)
    if not item or item.archived_at is not None:
        raise HTTPException(404)
    if item.registration_status == "pending":
        raise HTTPException(409, "Registrierung muss zuerst freigegeben werden")
    if item.role == Role.ADMIN and role != Role.ADMIN:
        active_admin_ids = list(
            db.scalars(
                select(User.id).where(
                    User.role == Role.ADMIN,
                    User.active.is_(True),
                    User.archived_at.is_(None),
                )
            )
        )
        if len(active_admin_ids) <= 1:
            raise HTTPException(
                409, "Der letzte aktive Administrator kann nicht herabgestuft werden"
            )
    previous_role = item.role
    item.role = role
    audit(
        db,
        current,
        "user.role_changed",
        "user",
        item.id,
        details={"old_role": previous_role.value, "new_role": role.value},
    )
    db.commit()
    return redirect("/users", f"Rolle von {item.email} wurde auf {role.label} geändert")


@router.get("/media", response_class=HTMLResponse)
def media(
    request: Request,
    team_id: str | None = None,
    category: str = "all",
    status: str = "all",
    search: str = "",
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    if not current.club_id:
        raise HTTPException(403, "Eindeutiger Vereinskontext fehlt")
    teams = db.scalars(
        select(Team).where(
            Team.club_id == current.club_id,
            Team.archived_at.is_(None),
        )
    ).all()
    visible = [t for t in teams if require_visible(db, current, t.id)]
    selected = next((t for t in visible if t.id == team_id), None)
    filtered_team_ids = [selected.id] if selected else [team.id for team in visible]
    all_items = (
        db.scalars(
            select(MediaAsset)
            .where(
                MediaAsset.club_id == current.club_id,
                MediaAsset.team_id.in_(filtered_team_ids),
                MediaAsset.deleted_at.is_(None),
            )
            .order_by(MediaAsset.created_at.desc(), MediaAsset.filename)
        ).all()
        if filtered_team_ids
        else []
    )
    if category not in {*MEDIA_CATEGORIES, "all"}:
        category = "all"
    allowed_statuses = {"available", "reserved", "used", "excluded", "missing", "all"}
    if status not in allowed_statuses:
        status = "all"
    search_term = search.strip().casefold()
    item_statuses = {item.id: usage_status(item) for item in all_items}
    items = [
        item
        for item in all_items
        if (category == "all" or item.media_category == category)
        and (status == "all" or item_statuses[item.id] == status)
        and (
            not search_term
            or any(
                search_term in (value or "").casefold()
                for value in (
                    item.filename,
                    item.player_name,
                    item.description,
                    item.photographer,
                )
            )
        )
    ]
    category_counts = {
        key: sum(item.media_category == key for item in all_items) for key in MEDIA_CATEGORIES
    }
    reservation_game_ids = {
        item.reserved_game_id for item in all_items if item.reserved_game_id
    }
    reservation_games = (
        {
            game.id: game
            for game in db.scalars(
                select(Game).where(
                    Game.id.in_(reservation_game_ids),
                    Game.club_id == current.club_id,
                )
            ).all()
        }
        if reservation_game_ids
        else {}
    )
    games = (
        db.scalars(
            select(Game)
            .where(Game.team_id == selected.id, Game.club_id == current.club_id)
            .order_by(Game.kickoff.desc())
            .limit(100)
        ).all()
        if selected
        else []
    )
    storage_committed, storage_reserved = storage_usage(db, current.club_id)
    storage_bytes = storage_committed + storage_reserved
    try:
        storage_limit_bytes = effective_limits(db, current.club_id)["storage_bytes"].value
    except LimitExceeded:
        storage_limit_bytes = 0
    storage_percent = (
        min(100, round(storage_bytes * 100 / storage_limit_bytes))
        if storage_limit_bytes
        else 0
    )
    folders = []
    external_import_available = False
    try:
        folders = [
            x.name for x in settings.media_root.iterdir() if x.is_dir() and not x.is_symlink()
        ]
    except OSError:
        pass
    if selected:
        try:
            external_import_available = (
                LocalStorageProvider(settings.media_root).resolve(selected.media_subdir).is_dir()
            )
        except StorageError:
            external_import_available = False
    return render(
        request,
        "media.html",
        current,
        teams=visible,
        selected=selected,
        filter_team_id=selected.id if selected else "all",
        team_by_id={team.id: team for team in visible},
        items=items,
        folders=folders,
        external_import_available=external_import_available,
        storage_ok=settings.upload_root.is_dir(),
        games=games,
        category=category,
        status=status,
        search=search,
        item_statuses=item_statuses,
        category_counts=category_counts,
        total_media_count=len(all_items),
        reservation_games=reservation_games,
        category_labels=MEDIA_CATEGORY_LABELS,
        storage_used_display=format_storage_gb(storage_bytes, fixed_decimals=True),
        storage_limit_display=format_storage_gb(
            storage_limit_bytes, fixed_decimals=False
        ),
        storage_has_limit=bool(storage_limit_bytes),
        storage_percent=storage_percent,
        title="Medienbibliothek",
    )


def require_visible(db, current, team_id):
    try:
        require(current, db, "view", team_id)
        return True
    except HTTPException:
        return False


def _tenant_media_asset(
    db: Session,
    current: User,
    asset_id: str,
    *,
    include_deleted: bool = False,
) -> MediaAsset | None:
    if not current.club_id:
        raise HTTPException(403, "Eindeutiger Vereinskontext fehlt")
    conditions = [MediaAsset.id == asset_id, MediaAsset.club_id == current.club_id]
    if not include_deleted:
        conditions.append(MediaAsset.deleted_at.is_(None))
    return db.scalar(select(MediaAsset).where(*conditions))


@router.post("/media/{team_id}/scan")
def scan_media(
    team_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require(current, db, "generate", team_id)
    if not current.club_id:
        raise HTTPException(403, "Eindeutiger Vereinskontext fehlt")
    team = db.scalar(
        select(Team).where(Team.id == team_id, Team.club_id == current.club_id)
    )
    if not team:
        raise HTTPException(404)
    store = LocalStorageProvider(settings.media_root)
    try:
        folder = store.resolve(team.media_subdir)
    except StorageError as e:
        raise HTTPException(422, str(e)) from e
    if not folder.is_dir():
        raise HTTPException(503, "SMB-/Medienordner ist nicht erreichbar")
    seen = set()
    for path in folder.rglob("*"):
        if (
            not path.is_file()
            or path.is_symlink()
            or path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}
        ):
            continue
        relative = str(path.relative_to(settings.media_root))
        before = path.stat()
        content = path.read_bytes()
        after = path.stat()
        if before.st_mtime_ns != after.st_mtime_ns or before.st_size != after.st_size:
            raise HTTPException(409, f"Datei wurde während des Scans verändert: {relative}")
        seen.add(relative)
        stat = after
        try:
            with Image.open(BytesIO(content)) as probe:
                width, height = probe.size
        except (UnidentifiedImageError, OSError):
            continue
        asset = db.scalar(
            select(MediaAsset).where(
                MediaAsset.club_id == current.club_id,
                MediaAsset.team_id == team.id,
                MediaAsset.storage_kind == "external",
                MediaAsset.relative_path == relative,
            )
        )
        values = {
            "filename": path.name,
            "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            "size": stat.st_size,
            "width": width,
            "height": height,
            "checksum": hashlib.sha256(content).hexdigest(),
            "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc),
            "available": True,
        }
        if asset:
            for key, value in values.items():
                setattr(asset, key, value)
        else:
            db.add(
                MediaAsset(
                    club_id=team.club_id,
                    team_id=team.id,
                    storage_kind="external",
                    relative_path=relative,
                    media_category="match_photo",
                    automatic_usage_enabled=True,
                    active=True,
                    **values,
                )
            )
    for asset in db.scalars(
        select(MediaAsset).where(
            MediaAsset.club_id == current.club_id,
            MediaAsset.team_id == team.id,
            MediaAsset.storage_kind == "external",
        )
    ):
        if asset.relative_path not in seen:
            asset.available = False
    team.last_sync_at = datetime.now(timezone.utc)
    audit(db, current, "media.scanned", "team", team.id, team.id, {"files": len(seen)})
    db.commit()
    return redirect(f"/media?team_id={team.id}", f"{len(seen)} Dateien eingelesen")


@router.post("/media/{team_id}/upload")
async def upload_player_images(
    team_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    files: list[UploadFile] | None = File(default=None),
    archive: UploadFile | None = File(default=None),
    media_category: str = Form(default="match_photo"),
    game_id: str = Form(default=""),
    description: str = Form(default=""),
    captured_date: str = Form(default=""),
    player_name: str = Form(default=""),
    photographer: str = Form(default=""),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require(current, db, "generate", team_id)
    if not current.club_id:
        raise HTTPException(403, "Eindeutiger Vereinskontext fehlt")
    team = db.scalar(
        select(Team).where(Team.id == team_id, Team.club_id == current.club_id)
    )
    if not team:
        raise HTTPException(404)
    try:
        media_category = MEDIA_CATEGORIES[MEDIA_CATEGORIES.index(media_category)]
    except ValueError as exc:
        raise HTTPException(422, "Unbekannte Medienkategorie") from exc
    related_game = None
    if game_id:
        related_game = db.scalar(
            select(Game).where(
                Game.id == game_id,
                Game.club_id == current.club_id,
                Game.team_id == team.id,
            )
        )
        if not related_game:
            raise HTTPException(422, "Das gewählte Spiel gehört nicht zu dieser Mannschaft")
    if len(description) > 500 or len(player_name) > 160 or len(photographer) > 160:
        raise HTTPException(422, "Mindestens eine Beschreibung ist zu lang")
    captured_at = None
    if captured_date:
        try:
            captured_at = datetime.fromisoformat(captured_date).replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise HTTPException(422, "Ungültiges Aufnahmedatum") from exc
    direct_files = files or []
    has_archive = archive is not None and bool(archive.filename)
    if not direct_files and not has_archive:
        raise HTTPException(422, "Mindestens ein Spielerbild oder ZIP-Archiv auswählen")
    if len(direct_files) > MAX_PLAYER_IMAGE_FILES:
        raise HTTPException(
            422,
            f"Pro Upload sind höchstens {MAX_PLAYER_IMAGE_FILES} Spielerbilder erlaubt",
        )

    created_paths: list[Path] = []
    created_assets: list[MediaAsset] = []
    skipped: list[str] = []
    batch_checksums: set[str] = set()
    image_count = 0

    def persist(image: ValidatedPlayerImage) -> None:
        nonlocal image_count
        image_count += 1
        if image_count > MAX_PLAYER_IMAGE_FILES:
            raise PlayerImageUploadError(
                f"Pro Upload sind höchstens {MAX_PLAYER_IMAGE_FILES} Spielerbilder erlaubt"
            )
        if image.checksum in batch_checksums:
            skipped.append(image.original_filename)
            return
        batch_checksums.add(image.checksum)
        existing = db.scalar(
            select(MediaAsset).where(
                MediaAsset.club_id == current.club_id,
                MediaAsset.team_id == team.id,
                MediaAsset.checksum == image.checksum,
                MediaAsset.available.is_(True),
            )
        )
        if existing:
            try:
                media_asset_path(existing, settings.media_root, settings.upload_root)
            except StorageError:
                existing.available = False
            else:
                skipped.append(image.original_filename)
                return

        ledger = reserve_storage(
            db,
            club_id=team.club_id,
            bytes_requested=len(image.content),
            idempotency_key=f"dashboard-media:{secrets.token_hex(16)}",
            actor_user_id=current.id,
        )
        relative, target = store_player_image(
            settings.upload_root,
            team.id,
            image,
            club_id=team.club_id,
            team_slug=team.slug,
        )
        created_paths.append(target)
        stat = target.stat()
        asset = MediaAsset(
            club_id=team.club_id,
            team_id=team.id,
            storage_kind="upload",
            relative_path=relative,
            filename=image.original_filename,
            mime_type=image.mime_type,
            size=stat.st_size,
            width=image.width,
            height=image.height,
            checksum=image.checksum,
            mtime=datetime.fromtimestamp(stat.st_mtime, timezone.utc),
            player_name=player_name.strip() or image.player_name or None,
            media_category=media_category,
            game_id=related_game.id if related_game else None,
            description=description.strip() or None,
            captured_at=captured_at,
            photographer=photographer.strip() or None,
            uploaded_by=current.id,
            automatic_usage_enabled=True,
            active=True,
            available=True,
        )
        db.add(asset)
        db.flush()
        commit_local_media_upload(
            db,
            ledger,
            club_id=team.club_id,
            media_asset_id=asset.id,
            team_id=team.id,
            object_key=relative,
            size_bytes=stat.st_size,
            checksum=image.checksum,
            mime_type=image.mime_type,
        )
        created_assets.append(asset)

    try:
        for file in direct_files:
            content = await file.read(MAX_PLAYER_IMAGE_BYTES + 1)
            persist(validate_player_image(file.filename or "", file.content_type, content))
        if has_archive:
            await archive.seek(0)
            for image in iter_player_images_from_zip(
                archive.filename or "",
                archive.content_type,
                archive.file,
            ):
                persist(image)
        db.flush()
        team.last_sync_at = datetime.now(timezone.utc)
        audit(
            db,
            current,
            "media.player_images_uploaded",
            "team",
            team.id,
            team.id,
            {
                "created": [
                    {
                        "asset_id": asset.id,
                        "filename": asset.filename,
                        "category": asset.media_category,
                    }
                    for asset in created_assets
                ],
                "duplicates_skipped": skipped,
                "archive": archive.filename if has_archive else None,
            },
        )
        db.commit()
    except (PlayerImageUploadError, StorageQuotaError) as exc:
        db.rollback()
        for path in created_paths:
            path.unlink(missing_ok=True)
        raise HTTPException(422, str(exc)) from exc
    except Exception:
        db.rollback()
        for path in created_paths:
            path.unlink(missing_ok=True)
        raise

    message = f"{len(created_assets)} Medien hochgeladen"
    if skipped:
        message += f", {len(skipped)} Duplikate übersprungen"
    return redirect(f"/media?team_id={team.id}", message)


@router.get("/media/{asset_id}/preview")
def preview_media(
    asset_id: str,
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    asset = _tenant_media_asset(db, current, asset_id, include_deleted=True)
    if not asset:
        raise HTTPException(404)
    require(current, db, "view", asset.team_id)
    try:
        path = media_asset_path(asset, settings.media_root, settings.upload_root)
    except StorageError as exc:
        raise HTTPException(404, str(exc)) from exc
    return detached_file_response(db, path, media_type=asset.mime_type)


@router.post("/media/{asset_id}/toggle")
def toggle_media(
    asset_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    asset = _tenant_media_asset(db, current, asset_id)
    if not asset:
        raise HTTPException(404)
    require(current, db, "generate", asset.team_id)
    try:
        set_automatic_usage(
            db,
            asset,
            enabled=not asset.automatic_usage_enabled,
            actor_user_id=current.id,
        )
    except MediaLibraryError as exc:
        raise HTTPException(422, str(exc)) from exc
    audit(
        db,
        current,
        "media.automatic_usage_changed",
        "media",
        asset.id,
        asset.team_id,
        {"enabled": asset.automatic_usage_enabled},
    )
    db.commit()
    return redirect(f"/media?team_id={asset.team_id}")


@router.post("/media/{asset_id}/automatic")
def change_media_automatic_usage(
    asset_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    enabled: str = Form(default=""),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    asset = _tenant_media_asset(db, current, asset_id)
    if not asset or asset.deleted_at is not None:
        raise HTTPException(404)
    require(current, db, "generate", asset.team_id)
    try:
        set_automatic_usage(
            db,
            asset,
            enabled=enabled == "true",
            actor_user_id=current.id,
        )
    except MediaLibraryError as exc:
        raise HTTPException(422, str(exc)) from exc
    audit(
        db,
        current,
        "media.automatic_usage_changed",
        "media",
        asset.id,
        asset.team_id,
        {"enabled": asset.automatic_usage_enabled},
    )
    db.commit()
    return redirect(f"/media/{asset.id}", "Automatische Bildauswahl wurde angepasst")


@router.post("/media/{asset_id}/release")
def release_media_asset(
    asset_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    asset = _tenant_media_asset(db, current, asset_id)
    if not asset or asset.deleted_at is not None:
        raise HTTPException(404)
    require(current, db, "generate", asset.team_id)
    release_asset(db, asset, actor_user_id=current.id)
    audit(
        db,
        current,
        "media.released_globally",
        "media",
        asset.id,
        asset.team_id,
        {"historical_usage_preserved": True},
    )
    db.commit()
    return redirect(f"/media/{asset.id}", "Bild ist wieder für die Automatik verfügbar")


@router.post("/media/{asset_id}/delete")
def delete_media_asset(
    asset_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    asset = _tenant_media_asset(db, current, asset_id)
    if not asset or asset.deleted_at is not None:
        raise HTTPException(404)
    require(current, db, "generate", asset.team_id)
    source_path = None
    try:
        remove_source_file = soft_delete_asset(db, asset, actor_user_id=current.id)
        if remove_source_file:
            source_path = media_asset_path(asset, settings.media_root, settings.upload_root)
            mark_local_media_deleted(
                db,
                club_id=asset.club_id,
                media_asset_id=asset.id,
                actor_user_id=current.id,
            )
    except MediaLibraryError as exc:
        raise HTTPException(409, str(exc)) from exc
    except StorageError:
        source_path = None
    audit(
        db,
        current,
        "media.soft_deleted",
        "media",
        asset.id,
        asset.team_id,
        {
            "file_retained_for_history": not remove_source_file,
            "source_file_removed": remove_source_file,
        },
    )
    db.commit()
    if source_path is not None:
        source_path.unlink(missing_ok=True)
    return redirect(f"/media?team_id={asset.team_id}", "Medium wurde gelöscht")


def _update_media_metadata(
    db: Session,
    asset: MediaAsset,
    *,
    media_category: str,
    game_id: str,
    description: str,
    captured_date: str,
    player_name: str,
    photographer: str,
) -> None:
    if media_category not in MEDIA_CATEGORIES:
        raise MediaLibraryError("Unbekannte Medienkategorie")
    related_game = (
        db.scalar(
            select(Game).where(
                Game.id == game_id,
                Game.club_id == asset.club_id,
                Game.team_id == asset.team_id,
            )
        )
        if game_id
        else None
    )
    if game_id and not related_game:
        raise MediaLibraryError("Das gewählte Spiel gehört nicht zur Mannschaft")
    if len(description) > 500 or len(player_name) > 160 or len(photographer) > 160:
        raise MediaLibraryError("Mindestens eine Beschreibung ist zu lang")
    captured_at = None
    if captured_date:
        try:
            captured_at = datetime.fromisoformat(captured_date).replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise MediaLibraryError("Ungültiges Aufnahmedatum") from exc
    asset.media_category = media_category
    asset.game_id = related_game.id if related_game else None
    asset.description = description.strip() or None
    asset.captured_at = captured_at
    asset.player_name = player_name.strip() or None
    asset.photographer = photographer.strip() or None


@router.post("/media/{asset_id}/edit")
def edit_media_asset(
    asset_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    media_category: str = Form(),
    game_id: str = Form(default=""),
    description: str = Form(default=""),
    captured_date: str = Form(default=""),
    player_name: str = Form(default=""),
    photographer: str = Form(default=""),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    asset = _tenant_media_asset(db, current, asset_id)
    if not asset or asset.deleted_at is not None:
        raise HTTPException(404)
    require(current, db, "generate", asset.team_id)
    try:
        _update_media_metadata(
            db,
            asset,
            media_category=media_category,
            game_id=game_id,
            description=description,
            captured_date=captured_date,
            player_name=player_name,
            photographer=photographer,
        )
    except MediaLibraryError as exc:
        raise HTTPException(422, str(exc)) from exc
    audit(
        db,
        current,
        "media.metadata_changed",
        "media",
        asset.id,
        asset.team_id,
        {"category": asset.media_category, "game_id": asset.game_id},
    )
    db.commit()
    return redirect(f"/media/{asset.id}", "Medienangaben wurden gespeichert")


@router.post("/media/bulk")
def bulk_media_action(
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    asset_ids: list[str] = Form(default=[]),
    action: str = Form(),
    category: str = Form(default=""),
    target_team_id: str = Form(default=""),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    if not current.club_id:
        raise HTTPException(403, "Eindeutiger Vereinskontext fehlt")
    if not asset_ids or len(asset_ids) > 100:
        raise HTTPException(422, "Bitte mindestens ein und höchstens 100 Medien auswählen")
    assets = db.scalars(
        select(MediaAsset).where(
            MediaAsset.id.in_(set(asset_ids)),
            MediaAsset.club_id == current.club_id,
            MediaAsset.deleted_at.is_(None),
        )
    ).all()
    if len(assets) != len(set(asset_ids)):
        raise HTTPException(404, "Mindestens ein Medium ist nicht verfügbar")
    for asset in assets:
        require(current, db, "generate", asset.team_id)

    target_team = None
    if action == "category":
        if category not in MEDIA_CATEGORIES:
            raise HTTPException(422, "Unbekannte Medienkategorie")
    elif action == "team":
        target_team = db.scalar(
            select(Team).where(
                Team.id == target_team_id,
                Team.club_id == current.club_id,
            )
        )
        if not target_team or target_team.club_id != current.club_id:
            raise HTTPException(422, "Zielmannschaft gehört nicht zum Verein")
        require(current, db, "generate", target_team.id)
        if any(asset.uses or asset.reserved_game_id for asset in assets):
            raise HTTPException(
                409,
                "Bereits verwendete oder reservierte Medien können nicht verschoben werden",
            )
        if any(asset.storage_kind != "upload" for asset in assets):
            raise HTTPException(
                409,
                "Medien aus einer externen Importquelle können nicht verschoben werden",
            )
        selected_asset_ids = {asset.id for asset in assets}
        if db.scalar(
            select(GameMediaPreference.id)
            .where(
                GameMediaPreference.club_id == current.club_id,
                GameMediaPreference.selected_media_asset_id.in_(selected_asset_ids),
            )
            .limit(1)
        ):
            raise HTTPException(
                409,
                "Mindestens ein Medium ist noch bewusst für ein Spiel ausgewählt",
            )
    elif action not in {"release", "delete"}:
        raise HTTPException(422, "Unbekannte Mehrfachaktion")
    if action == "delete" and any(asset.reserved_game_id for asset in assets):
        raise HTTPException(409, "Mindestens ein Bild ist noch reserviert")

    moved: list[tuple[Path, Path]] = []
    deleted_source_paths: list[Path] = []
    try:
        for asset in assets:
            if action == "release":
                release_asset(db, asset, actor_user_id=current.id)
            elif action == "delete":
                remove_source_file = soft_delete_asset(
                    db, asset, actor_user_id=current.id
                )
                if remove_source_file:
                    mark_local_media_deleted(
                        db,
                        club_id=asset.club_id,
                        media_asset_id=asset.id,
                        actor_user_id=current.id,
                    )
                    try:
                        deleted_source_paths.append(
                            media_asset_path(
                                asset, settings.media_root, settings.upload_root
                            )
                        )
                    except StorageError:
                        pass
            elif action == "category":
                asset.media_category = category
            elif action == "team" and target_team:
                old_path = (settings.upload_root / asset.relative_path).resolve()
                relative, new_path = move_uploaded_media_to_team(
                    settings.upload_root,
                    asset.relative_path,
                    club_id=asset.club_id,
                    team_id=target_team.id,
                    team_slug=target_team.slug,
                )
                moved.append((new_path, old_path))
                asset.relative_path = relative
                asset.team_id = target_team.id
                asset.game_id = None
                move_local_media_storage_object(
                    db,
                    club_id=asset.club_id,
                    media_asset_id=asset.id,
                    team_id=target_team.id,
                    object_key=relative,
                )
        audit(
            db,
            current,
            f"media.bulk_{action}",
            "media",
            details={"count": len(assets), "category": category or None},
        )
        db.commit()
        for source_path in deleted_source_paths:
            source_path.unlink(missing_ok=True)
    except Exception:
        db.rollback()
        for new_path, old_path in reversed(moved):
            if new_path.exists():
                old_path.parent.mkdir(parents=True, exist_ok=True)
                new_path.replace(old_path)
        raise
    return redirect("/media", f"{len(assets)} Medien wurden aktualisiert")


@router.get("/media/{asset_id}", response_class=HTMLResponse)
def media_detail(
    asset_id: str,
    request: Request,
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    asset = _tenant_media_asset(db, current, asset_id, include_deleted=True)
    if not asset:
        raise HTTPException(404)
    require(current, db, "view", asset.team_id)
    team = db.scalar(
        select(Team).where(Team.id == asset.team_id, Team.club_id == current.club_id)
    )
    games = db.scalars(
        select(Game)
        .where(Game.team_id == asset.team_id, Game.club_id == current.club_id)
        .order_by(Game.kickoff.desc())
        .limit(100)
    ).all()
    history = db.scalars(
        select(MediaUsageHistory)
        .where(
            MediaUsageHistory.media_asset_id == asset.id,
            MediaUsageHistory.club_id == current.club_id,
        )
        .order_by(MediaUsageHistory.created_at.desc())
    ).all()
    game_ids = {item.game_id for item in history if item.game_id}
    history_games = {
        game.id: game
        for game in db.scalars(
            select(Game).where(Game.id.in_(game_ids), Game.club_id == current.club_id)
        ).all()
    } if game_ids else {}
    reservation_game = (
        db.scalar(
            select(Game).where(
                Game.id == asset.reserved_game_id,
                Game.club_id == current.club_id,
                Game.team_id == asset.team_id,
            )
        )
        if asset.reserved_game_id
        else None
    )
    uploader = (
        db.scalar(
            select(User).where(User.id == asset.uploaded_by, User.club_id == current.club_id)
        )
        if asset.uploaded_by
        else None
    )
    return render(
        request,
        "media_detail.html",
        current,
        asset=asset,
        team=team,
        games=games,
        history=history,
        history_games=history_games,
        reservation_game=reservation_game,
        uploader=uploader,
        media_status=usage_status(asset),
        category_labels=MEDIA_CATEGORY_LABELS,
        title="Medium verwalten",
    )


@router.get("/assets", response_class=HTMLResponse)
def assets(request: Request, current=Depends(current_user), db: Session = Depends(get_db)):
    require_admin(current)
    fonts = db.scalars(select(FontAsset).where(FontAsset.archived_at.is_(None))).all()
    designs = db.scalars(
        select(DesignTemplate)
        .where(DesignTemplate.archived_at.is_(None))
        .order_by(DesignTemplate.name, DesignTemplate.version.desc())
    ).all()
    return render(
        request,
        "assets.html",
        current,
        fonts=fonts,
        designs=designs,
        title="Schriftarten und Designvorlagen",
    )


@router.get("/fonts/{font_id}/preview")
def preview_font(
    font_id: str,
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    require_admin(current)
    font = db.get(FontAsset, font_id)
    if not font or not font.active or font.archived_at:
        raise HTTPException(404)
    relative = Path(font.relative_path)
    root = settings.upload_root.resolve()
    path = relative.resolve() if relative.is_absolute() else (Path.cwd() / relative).resolve()
    if (
        not path.is_relative_to(root)
        or path.is_symlink()
        or not path.is_file()
        or path.suffix.lower() not in {".woff2", ".ttf"}
    ):
        raise HTTPException(404)
    return detached_file_response(db, path, media_type=font.mime_type)


@router.post("/fonts")
async def upload_font(
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    name: str = Form(),
    family: str = Form(),
    file: UploadFile = File(),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    try:
        assert_resource_capacity(db, current.club_id, "fonts")
    except LimitExceeded as exc:
        audit(
            db, current, "font.limit_blocked", "club", current.club_id, details={"reason": str(exc)}
        )
        db.commit()
        raise HTTPException(409, str(exc)) from exc
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".woff2", ".ttf"}:
        raise HTTPException(422, "Nur WOFF2 und TTF sind erlaubt")
    data = await file.read()
    if not data or len(data) > 5 * 1024 * 1024:
        raise HTTPException(422, "Schriftdatei leer oder größer als 5 MB")
    target = Path("data/uploads/fonts") / f"{hashlib.sha256(data).hexdigest()}{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    item = FontAsset(
        name=name,
        family=family,
        relative_path=str(target),
        mime_type=file.content_type or "font/ttf",
        size=len(data),
    )
    db.add(item)
    db.flush()
    audit(db, current, "font.uploaded", "font", item.id)
    db.commit()
    return redirect("/assets")


@router.post("/designs")
def create_design(
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    name: str = Form(),
    post_type: str = Form(),
    media_kind: str = Form(),
    html_template: str = Form(),
    css: str = Form(),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    if media_kind not in {"feed", "story"}:
        raise HTTPException(422, "Ungültiges Medienformat")
    previous = db.scalar(
        select(DesignTemplate)
        .where(DesignTemplate.name == name)
        .order_by(DesignTemplate.version.desc())
    )
    version = (previous.version + 1) if previous else 1
    item = DesignTemplate(
        name=name,
        post_type=post_type,
        media_kind=media_kind,
        width=1080,
        height=1350 if media_kind == "feed" else 1920,
        html_template=html_template,
        css=css,
        version=version,
    )
    db.add(item)
    db.flush()
    audit(db, current, "design.created", "design", item.id, details={"version": version})
    db.commit()
    return redirect("/assets")


@router.get("/prompts", response_class=HTMLResponse)
def prompts(request: Request, current=Depends(current_user), db: Session = Depends(get_db)):
    from app.prompts.service import (
        ALLOWED_PLACEHOLDERS,
        DEFAULT_IMAGE_PROMPT,
        DEFAULT_TEXT_PROMPT,
        builtin_prompt_catalog,
    )

    require_platform_admin(current)
    items = db.scalars(
        select(PromptTemplate)
        .where(PromptTemplate.archived_at.is_(None))
        .order_by(PromptTemplate.name, PromptTemplate.version.desc())
    ).all()
    requested_run = request.query_params.get("test_run")
    edit_id = request.query_params.get("edit")
    builtin_key = request.query_params.get("builtin")
    builtin_prompts = builtin_prompt_catalog()
    if edit_id and builtin_key:
        raise HTTPException(422, "Bitte nur eine Prompt-Ausgangsversion auswählen")
    editing_prompt = db.get(PromptTemplate, edit_id) if edit_id else None
    if edit_id and editing_prompt is None:
        raise HTTPException(404, "Promptvorlage nicht gefunden")
    if builtin_key:
        editing_prompt = builtin_prompts.get(builtin_key)
        if editing_prompt is None:
            raise HTTPException(404, "Eingebaute Promptvorlage nicht gefunden")
    selected_test = db.get(PromptTestRun, requested_run) if requested_run else None
    return render(
        request,
        "prompts.html",
        current,
        items=items,
        placeholders=sorted(ALLOWED_PLACEHOLDERS),
        default_image=DEFAULT_IMAGE_PROMPT,
        default_text=DEFAULT_TEXT_PROMPT,
        preview=None,
        clubs=db.scalars(select(Club).order_by(Club.name)).all(),
        teams=db.scalars(select(Team).order_by(Team.display_name)).all(),
        games=db.scalars(select(Game).order_by(Game.kickoff.desc()).limit(200)).all(),
        prompt_tests=db.scalars(
            select(PromptTestRun).order_by(PromptTestRun.created_at.desc()).limit(20)
        ).all(),
        selected_test=selected_test,
        editing_prompt=editing_prompt,
        builtin_prompts=builtin_prompts.values(),
        title="KI-Promptvorlagen",
    )


@router.post("/prompts/preview", response_class=HTMLResponse)
def preview_prompt(
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    prompt_body: str = Form(),
    prompt_kind: str = Form(),
    post_type: str = Form(),
    media_kind: str = Form(default="none"),
    style_direction: str = Form(default=""),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    from app.prompts.service import (
        ALLOWED_PLACEHOLDERS,
        DEFAULT_IMAGE_PROMPT,
        DEFAULT_TEXT_PROMPT,
        TEXT_SAFETY_PREFIX,
        PromptValidationError,
        builtin_prompt_catalog,
        image_safety_prefix,
        prompt_context,
        render_body,
        sample_facts,
    )

    check_csrf(request, csrf_token_value)
    require_platform_admin(current)
    if (
        prompt_kind not in {"image", "text"}
        or post_type not in {"announcement", "reminder", "result"}
        or media_kind not in {"none", "feed", "story"}
    ):
        raise HTTPException(422, "Ungültiger Prompt-Typ")
    if prompt_kind == "image" and media_kind not in {"feed", "story"}:
        raise HTTPException(422, "Bildprompt benötigt Feed oder Story")
    if prompt_kind == "text":
        media_kind = "none"
    try:
        preview = render_body(
            prompt_body, prompt_context(sample_facts(), media_kind, style_direction or None)
        )
    except PromptValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    if prompt_kind == "image":
        preview = image_safety_prefix(sample_facts()) + "\n" + preview
    else:
        preview = TEXT_SAFETY_PREFIX + "\n" + preview
    items = db.scalars(
        select(PromptTemplate)
        .where(PromptTemplate.archived_at.is_(None))
        .order_by(PromptTemplate.name, PromptTemplate.version.desc())
    ).all()
    return render(
        request,
        "prompts.html",
        current,
        items=items,
        placeholders=sorted(ALLOWED_PLACEHOLDERS),
        default_image=DEFAULT_IMAGE_PROMPT,
        default_text=DEFAULT_TEXT_PROMPT,
        preview=preview,
        form={
            "prompt_body": prompt_body,
            "prompt_kind": prompt_kind,
            "post_type": post_type,
            "media_kind": media_kind,
            "style_direction": style_direction,
        },
        clubs=db.scalars(select(Club).order_by(Club.name)).all(),
        teams=db.scalars(select(Team).order_by(Team.display_name)).all(),
        games=db.scalars(select(Game).order_by(Game.kickoff.desc()).limit(200)).all(),
        prompt_tests=db.scalars(
            select(PromptTestRun).order_by(PromptTestRun.created_at.desc()).limit(20)
        ).all(),
        selected_test=None,
        editing_prompt=None,
        builtin_prompts=builtin_prompt_catalog().values(),
        title="KI-Promptvorlagen",
    )


@router.post("/prompts")
def create_prompt(
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    name: str = Form(),
    prompt_kind: str = Form(),
    post_type: str = Form(),
    media_kind: str = Form(default="none"),
    prompt_body: str = Form(),
    style_direction: str = Form(default=""),
    model: str = Form(),
    quality: str = Form(default="medium"),
    base_prompt_id: str = Form(default=""),
    builtin_prompt_key: str = Form(default=""),
    change_description: str = Form(default=""),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    from app.prompts.service import (
        PromptValidationError,
        builtin_prompt_catalog,
        validate_template,
    )

    check_csrf(request, csrf_token_value)
    require_platform_admin(current)
    base_prompt_id = base_prompt_id if isinstance(base_prompt_id, str) else ""
    builtin_prompt_key = builtin_prompt_key if isinstance(builtin_prompt_key, str) else ""
    if base_prompt_id and builtin_prompt_key:
        raise HTTPException(422, "Bitte nur eine Prompt-Ausgangsversion auswählen")
    base_prompt = db.get(PromptTemplate, base_prompt_id) if base_prompt_id else None
    if base_prompt_id and base_prompt is None:
        raise HTTPException(404, "Ausgangsversion nicht gefunden")
    if base_prompt:
        name = base_prompt.name
        prompt_kind = base_prompt.prompt_kind
        post_type = base_prompt.post_type
        media_kind = base_prompt.media_kind
    if builtin_prompt_key:
        builtin = builtin_prompt_catalog().get(builtin_prompt_key)
        if builtin is None:
            raise HTTPException(404, "Eingebaute Promptvorlage nicht gefunden")
        name = builtin["name"]
        prompt_kind = builtin["prompt_kind"]
        post_type = builtin["post_type"]
        media_kind = builtin["media_kind"]
    name = name.strip()
    if (
        not name
        or prompt_kind not in {"image", "text"}
        or post_type not in {"announcement", "reminder", "result"}
    ):
        raise HTTPException(422, "Prompt-Metadaten sind ungültig")
    if prompt_kind == "image" and media_kind not in {"feed", "story"}:
        raise HTTPException(422, "Bildprompt benötigt Feed oder Story")
    if prompt_kind == "text":
        media_kind = "none"
    if quality not in {"low", "medium", "high", "auto", "default"}:
        raise HTTPException(422, "Unbekannte Qualitätsstufe")
    if prompt_kind == "image" and (not model.startswith("gpt-image-") or quality == "default"):
        raise HTTPException(
            422, "Bildprompts benötigen ein GPT-Image-Modell und eine Bildqualitätsstufe"
        )
    try:
        allowed_variables = sorted(validate_template(prompt_body))
    except PromptValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    previous = db.scalar(
        select(PromptTemplate)
        .where(
            PromptTemplate.name == name,
            PromptTemplate.prompt_kind == prompt_kind,
            PromptTemplate.post_type == post_type,
            PromptTemplate.media_kind == media_kind,
        )
        .order_by(PromptTemplate.version.desc())
    )
    version = (previous.version + 1) if previous else 1
    item = PromptTemplate(
        name=name,
        prompt_kind=prompt_kind,
        post_type=post_type,
        media_kind=media_kind,
        prompt_body=prompt_body,
        status=PromptStatus.DRAFT,
        checksum=hashlib.sha256(prompt_body.encode("utf-8")).hexdigest(),
        allowed_variables=allowed_variables,
        validation_rules={},
        created_by=current.id,
        change_description=(
            change_description.strip()
            or (
                f"Neue Version auf Basis von v{base_prompt.version}"
                if base_prompt
                else "Neue geschützte Promptversion"
            )
        ),
        style_direction=style_direction.strip() or None,
        model=model.strip(),
        quality=quality,
        version=version,
        active=False,
    )
    db.add(item)
    db.flush()
    platform_audit(
        db,
        current,
        "prompt.created",
        "prompt_template",
        item.id,
        details={"name": name, "version": version, "kind": prompt_kind, "media_kind": media_kind},
    )
    db.commit()
    return redirect(
        f"/prompts?edit={item.id}#prompt-editor",
        f"Prompt {name} Version {version} gespeichert",
    )


def _active_team_rule_sets(db: Session, team: Team) -> list[ContentRuleSet]:
    return list(
        db.scalars(
            select(ContentRuleSet)
            .where(
                ContentRuleSet.club_id == team.club_id,
                ContentRuleSet.team_id == team.id,
                ContentRuleSet.scope_type == "team",
                ContentRuleSet.active.is_(True),
                ContentRuleSet.archived_at.is_(None),
            )
            .order_by(ContentRuleSet.post_type, ContentRuleSet.rule_version.desc())
        )
    )


def _serialize_team_publication_slots(db: Session, team: Team) -> list[dict]:
    configured = (team.rules or {}).get("publication_rule_slots")
    explicitly_configured = bool((team.rules or {}).get("publication_rule_slots_configured"))
    if isinstance(configured, list) and (configured or explicitly_configured):
        return deepcopy([row for row in configured if isinstance(row, dict)])
    rule_sets = _active_team_rule_sets(db, team)
    by_type: dict[str, ContentRuleSet] = {}
    for rule_set in rule_sets:
        by_type.setdefault(rule_set.post_type, rule_set)
    if not by_type:
        return []
    result: list[dict] = []
    for slot in db.scalars(
        select(PublicationRuleSlot)
        .where(
            PublicationRuleSlot.club_id == team.club_id,
            PublicationRuleSlot.rule_set_id.in_([row.id for row in by_type.values()]),
            PublicationRuleSlot.active.is_(True),
        )
        .order_by(PublicationRuleSlot.sort_order, PublicationRuleSlot.id)
    ):
        rule_set = next(row for row in by_type.values() if row.id == slot.rule_set_id)
        result.append(
            {
                "slot_key": slot.slot_key,
                "post_type": rule_set.post_type,
                "label": slot.label,
                "media_kind": slot.media_kind,
                "variant_number": slot.variant_number,
                "timing_model": slot.timing_model,
                "reference": slot.reference,
                "direction": slot.direction,
                "offset_minutes": slot.offset_minutes,
                "match_weekday": slot.match_weekday,
                "target_weekday": slot.target_weekday,
                "local_time": slot.local_time,
                "timezone": slot.timezone,
                "sort_order": slot.sort_order,
                "instagram_page_id": slot.instagram_page_id,
                "template": slot.template,
                "reuse_media": slot.reuse_media,
            }
        )
    return result


def _legacy_story_publication_rows(story: StoryRule, team: Team) -> list[dict]:
    """Translate the retained StoryRule endpoint into canonical publication slots."""
    entries = (
        sorted((story.weekday_times or {}).items())
        if story.timing_mode == "weekday_fixed"
        else [(None, None)]
    )
    rows: list[dict] = []
    for match_day, local_time in entries:
        suffix = match_day if match_day is not None else "default"
        rows.append(
            {
                "slot_key": f"legacy-story:{story.id}:{suffix}",
                "post_type": story.post_type,
                "label": story.name,
                "media_kind": "story",
                "variant_number": max(1, int(story.media_slot or 1)),
                "timing_model": story.timing_mode,
                "reference": story.reference,
                "direction": story.direction,
                "offset_minutes": story.offset_minutes,
                "match_weekday": int(match_day) if match_day is not None else None,
                "target_weekday": (
                    int((story.weekday_targets or {}).get(match_day, match_day))
                    if match_day is not None
                    else None
                ),
                "local_time": local_time,
                "timezone": team.timezone,
                "sort_order": story.sort_order,
                "instagram_page_id": story.instagram_page_id,
                "template": story.template,
                "reuse_media": story.reuse_media,
            }
        )
    return rows


def _replace_legacy_story_publication_rows(db: Session, team: Team, story: StoryRule) -> None:
    rules = dict(team.rules or {})
    configured = rules.get("publication_rule_slots")
    configured_is_authoritative = bool(configured) or bool(
        rules.get("publication_rule_slots_configured")
    )
    rows = (
        list(configured)
        if isinstance(configured, list) and configured_is_authoritative
        else _serialize_team_publication_slots(db, team)
    )
    prefix = f"legacy-story:{story.id}:"
    rows = [
        row
        for row in rows
        if not isinstance(row, dict) or not str(row.get("slot_key") or "").startswith(prefix)
    ]
    if story.active:
        rows.extend(_legacy_story_publication_rows(story, team))
    rules["publication_rule_slots"] = rows
    rules["publication_rule_slots_configured"] = True
    team.rules = rules


def _rule_cards(slots: list[dict]) -> list[dict]:
    cards = []
    for weekday in range(7):
        rows = sorted(
            (row for row in slots if row.get("match_weekday") == weekday),
            key=lambda row: (int(row.get("sort_order") or 0), str(row.get("slot_key") or "")),
        )
        if rows:
            cards.append({"weekday": weekday, "label": WEEKDAY_LABELS[weekday], "slots": rows})
    general = sorted(
        (row for row in slots if row.get("match_weekday") is None),
        key=lambda row: (int(row.get("sort_order") or 0), str(row.get("slot_key") or "")),
    )
    return cards + (
        [{"weekday": None, "label": "Alle Spieltage", "slots": general}] if general else []
    )


def _rule_slot_summary(row: dict) -> str:
    model = row.get("timing_model")
    if model == "weekday_fixed":
        target = row.get("target_weekday")
        day = WEEKDAY_LABELS[target] if isinstance(target, int) and 0 <= target <= 6 else "Zieltag"
        return f"{day}, {row.get('local_time') or '--:--'} Uhr"
    if model == "result_detected":
        offset = int(row.get("offset_minutes") or 0)
        return (
            "Direkt nach bestätigtem Ergebnis" if not offset else f"{offset} Minuten nach Ergebnis"
        )
    if model == "manual":
        return "Manuelle Planung"
    offset = int(row.get("offset_minutes") or 0)
    direction = "nach" if row.get("direction") == "after" else "vor"
    reference = "Ergebnis" if row.get("reference") == "result_detected" else "Anpfiff"
    return f"{offset} Minuten {direction} {reference}"


@router.get("/rules", response_class=HTMLResponse)
def rules(
    request: Request,
    team_id: str | None = None,
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    teams = [
        t
        for t in db.scalars(select(Team).where(Team.archived_at.is_(None)))
        if require_visible(db, current, t.id)
    ]
    selected = next((t for t in teams if t.id == team_id), teams[0] if teams else None)
    stories = (
        db.scalars(
            select(StoryRule)
            .where(StoryRule.team_id == selected.id, StoryRule.active.is_(True))
            .order_by(StoryRule.sort_order)
        ).all()
        if selected
        else []
    )
    pages = (
        db.scalars(
            select(InstagramPage).where(
                InstagramPage.club_id == selected.club_id,
                InstagramPage.archived_at.is_(None),
            )
        ).all()
        if selected
        else []
    )
    story_output_defaults = {"announcement": 1, "reminder": 1, "result": 1}
    for story in stories:
        story_output_defaults[story.post_type] = max(
            story_output_defaults.get(story.post_type, 1),
            int(getattr(story, "media_slot", 1) or 1),
        )
    carousel_teams = []
    if selected:
        club_key = normalize_club_name(selected.club)
        carousel_teams = sorted(
            (
                candidate
                for candidate in teams
                if candidate.instagram_page_id == selected.instagram_page_id
                and candidate.active
                and normalize_club_name(candidate.club) == club_key
            ),
            key=lambda candidate: (candidate.display_name.casefold(), candidate.id),
        )
    structured_slots = _serialize_team_publication_slots(db, selected) if selected else []
    for row in structured_slots:
        row["summary"] = _rule_slot_summary(row)
    preset_slots = [deepcopy(row) for row in RECOMMENDED_AUTOMATION_PRESET.slots]
    for row in preset_slots:
        row["summary"] = _rule_slot_summary(row)
    current_summary = None
    invalid_result_poll = False
    if selected:
        selected_rules = selected.rules or {}
        try:
            invalid_result_poll = (
                int(selected_rules.get("result_poll_interval_minutes", 15))
                < RESULT_POLL_MINUTES_MIN
            )
        except (TypeError, ValueError):
            invalid_result_poll = True
        current_summary = {
            "announcement": generation_summary(selected_rules, "announcement"),
            "announcement_selected": selection_summary(selected_rules, "announcement"),
            "reminder": generation_summary(selected_rules, "reminder"),
            "result": generation_summary(selected_rules, "result"),
            "result_selected": selection_summary(selected_rules, "result"),
            "configured_weekdays": sorted(
                {
                    int(row["match_weekday"])
                    for row in structured_slots
                    if row.get("match_weekday") is not None
                }
            ),
        }
    return render(
        request,
        "rules.html",
        current,
        teams=teams,
        selected=selected,
        stories=stories,
        pages=pages,
        story_output_defaults=story_output_defaults,
        carousel_teams=carousel_teams,
        publication_rule_cards=_rule_cards(structured_slots),
        publication_rule_slots=structured_slots,
        weekday_labels=WEEKDAY_LABELS,
        recommended_preset=RECOMMENDED_AUTOMATION_PRESET,
        recommended_preset_cards=_rule_cards(preset_slots),
        current_automation_summary=current_summary,
        invalid_result_poll=invalid_result_poll,
        can_manage_automation=current.role == Role.ADMIN,
        title="Automatische Beiträge",
    )


@router.post("/rules/{team_id}/recommended-preset")
def apply_automation_preset(
    team_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    expected_team_version: int = Form(),
    mode: str = Form(),
    confirm_replace: bool = Form(default=False),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    """Apply the visible preset without silently overwriting custom rules."""
    check_csrf(request, csrf_token_value)
    require_admin(current)
    team = _team_for_rule_write(db, current, team_id, expected_team_version)
    if mode == "replace" and not confirm_replace:
        raise HTTPException(
            422,
            "Das Ersetzen vorhandener Regeln muss ausdrücklich bestätigt werden",
        )
    try:
        updated, report = apply_recommended_preset(
            team.rules,
            mode=mode,
            timezone_name=team.timezone or "Europe/Berlin",
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    team.rules = updated
    team.version += 1
    sync_team_rule_sets(db, team)
    sync_state = db.get(FussballSyncState, team.id)
    if sync_state is None:
        db.add(
            FussballSyncState(
                club_id=team.club_id,
                team_id=team.id,
                status="idle",
                next_poll_at=datetime.now(timezone.utc),
            )
        )
    elif sync_state.status != "running":
        sync_state.status = "idle"
        sync_state.next_poll_at = datetime.now(timezone.utc)
        sync_state.lease_owner = None
        sync_state.lease_expires_at = None
    audit(
        db,
        current,
        "automation_preset.applied",
        "team",
        team.id,
        team.id,
        {
            "preset_key": RECOMMENDED_AUTOMATION_PRESET.key,
            "preset_version": RECOMMENDED_AUTOMATION_PRESET.version,
            "mode": mode,
            **report,
        },
    )
    db.commit()
    return redirect(
        f"/rules?team_id={team.id}#current-automation",
        "Die empfohlene Grundeinstellung wurde sicher übernommen",
    )


@router.post("/rules/{team_id}/schedule-preview", response_class=JSONResponse)
def preview_automation_schedule(
    team_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    kickoff_local: str = Form(),
    result_local: str = Form(default=""),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    """Read-only preview; deliberately performs no flush or commit."""
    check_csrf(request, csrf_token_value)
    require_admin(current)
    team = db.get(Team, team_id)
    if (
        not team
        or team.archived_at is not None
        or not require_visible(db, current, team.id)
        or (current.club_id and team.club_id != current.club_id)
    ):
        raise HTTPException(404, "Mannschaft nicht gefunden")
    zone = ZoneInfo(team.timezone or "Europe/Berlin")
    try:
        kickoff = datetime.fromisoformat(kickoff_local).replace(tzinfo=zone)
        result_detected = (
            datetime.fromisoformat(result_local).replace(tzinfo=zone) if result_local else None
        )
    except ValueError as exc:
        raise HTTPException(422, "Datum oder Uhrzeit ist ungültig") from exc
    if result_detected and result_detected < kickoff:
        raise HTTPException(422, "Das Ergebnis kann nicht vor dem Anpfiff feststehen")
    preview = build_schedule_preview(
        team,
        _serialize_team_publication_slots(db, team),
        kickoff=kickoff,
        result_detected_at=result_detected,
    )
    return JSONResponse(preview)


@router.post("/rules/{team_id}/copy-from")
def copy_team_rules(
    team_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    source_team_id: str = Form(),
    target_version: int = Form(),
    copy_mode: str = Form(default="append_missing"),
    copy_settings: bool = Form(default=True),
    copy_schedule: bool = Form(default=True),
    confirm_replace: bool = Form(default=False),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    target = db.scalar(select(Team).where(Team.id == team_id).with_for_update())
    source = db.get(Team, source_team_id)
    if not target or not source or target.archived_at is not None or source.archived_at is not None:
        raise HTTPException(404, "Mannschaft nicht gefunden")
    if target.club_id != source.club_id:
        raise HTTPException(422, "Regeln duerfen nur innerhalb desselben Vereins kopiert werden")
    if current.club_id and target.club_id != current.club_id:
        raise HTTPException(404, "Mannschaft nicht gefunden")
    if target.id == source.id:
        raise HTTPException(422, "Quell- und Zielmannschaft muessen verschieden sein")
    if target.version != target_version:
        raise HTTPException(409, "Die Zielmannschaft wurde zwischenzeitlich geaendert")
    if copy_mode not in {"append_missing", "replace"}:
        raise HTTPException(422, "Unbekannter Übernahmemodus")
    if not copy_settings and not copy_schedule:
        raise HTTPException(422, "Wähle mindestens einen zu kopierenden Bereich")
    if copy_mode == "replace" and not confirm_replace:
        raise HTTPException(422, "Das Ersetzen vorhandener Regeln muss bestätigt werden")

    source_config = deepcopy(source.rules or {})
    target_config = deepcopy(target.rules or {})
    source_slots = [
        deepcopy(row)
        for row in source_config.pop("publication_rule_slots", [])
        if isinstance(row, dict)
    ]
    source_config.pop("publication_rule_slots_configured", None)
    target_slots = [
        deepcopy(row)
        for row in target_config.get("publication_rule_slots", [])
        if isinstance(row, dict)
    ]
    if copy_settings:
        copied_poll_interval = int(
            source_config.get("result_poll_interval_minutes", RESULT_POLL_MINUTES_RECOMMENDED)
        )
        if copied_poll_interval < RESULT_POLL_MINUTES_MIN:
            raise HTTPException(
                422,
                "Die Quellmannschaft besitzt ein ungültiges Ergebnis-Prüfintervall. "
                "Korrigiere es zunächst auf mindestens 10 Minuten.",
            )
        if copy_mode == "replace":
            protected = {
                key: value
                for key, value in target_config.items()
                if key.startswith(("image_prompt", "text_prompt")) or key == "style_direction"
            }
            target_config = {**source_config, **protected}
        else:
            for key, value in source_config.items():
                target_config.setdefault(key, deepcopy(value))
    if copy_schedule:
        if copy_mode == "replace":
            target_slots = source_slots
        else:
            existing = {
                (
                    row.get("post_type"),
                    row.get("media_kind"),
                    row.get("variant_number"),
                    row.get("timing_model"),
                    row.get("match_weekday"),
                    row.get("target_weekday"),
                    row.get("local_time"),
                    row.get("reference"),
                    row.get("direction"),
                    row.get("offset_minutes"),
                )
                for row in target_slots
            }
            for row in source_slots:
                signature = (
                    row.get("post_type"),
                    row.get("media_kind"),
                    row.get("variant_number"),
                    row.get("timing_model"),
                    row.get("match_weekday"),
                    row.get("target_weekday"),
                    row.get("local_time"),
                    row.get("reference"),
                    row.get("direction"),
                    row.get("offset_minutes"),
                )
                if signature not in existing:
                    copied = deepcopy(row)
                    copied["slot_key"] = f"slot-{secrets.token_hex(12)}"
                    target_slots.append(copied)
                    existing.add(signature)
        target_config["publication_rule_slots"] = target_slots
        target_config["publication_rule_slots_configured"] = True
    target.rules = target_config
    target.version += 1
    source_rows = list(
        db.scalars(
            select(StoryRule).where(
                StoryRule.club_id == source.club_id,
                StoryRule.team_id == source.id,
                StoryRule.active.is_(True),
            )
        )
    )
    target_rows = {
        row.name: row
        for row in db.scalars(
            select(StoryRule).where(
                StoryRule.club_id == target.club_id,
                StoryRule.team_id == target.id,
            )
        )
    }
    if copy_schedule and copy_mode == "replace":
        for row in target_rows.values():
            row.active = False
    copied_fields = (
        "post_type",
        "reference",
        "direction",
        "offset_minutes",
        "fixed_time",
        "timing_mode",
        "weekday_times",
        "weekday_targets",
        "media_slot",
        "next_day",
        "template",
        "prompt_template",
        "text_variant",
        "priority",
        "sort_order",
        "reuse_media",
    )
    for source_row in source_rows if copy_schedule else []:
        target_row = target_rows.get(source_row.name)
        if target_row is not None and copy_mode == "append_missing":
            continue
        if target_row is None:
            target_row = StoryRule(
                club_id=target.club_id,
                team_id=target.id,
                name=source_row.name,
            )
            db.add(target_row)
        for field in copied_fields:
            value = getattr(source_row, field)
            setattr(target_row, field, deepcopy(value))
        # Keep the target team's Instagram page instead of leaking a page
        # assignment from another team configuration.
        target_row.instagram_page_id = None
        target_row.active = True
    sync_team_rule_sets(db, target)
    audit(
        db,
        current,
        "rules.copied",
        "team",
        target.id,
        target.id,
        {
            "source_team_id": source.id,
            "target_team_id": target.id,
            "mode": copy_mode,
            "copy_settings": copy_settings,
            "copy_schedule": copy_schedule,
            "story_rules": len(source_rows) if copy_schedule else 0,
        },
    )
    db.commit()
    return redirect(f"/rules?team_id={target.id}", "Regeln wurden kopiert")


@router.post("/rules/{team_id}/defaults")
def save_rules(
    team_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    expected_team_version: int | None = Form(default=None),
    preserve_legacy_weekday_settings: bool = Form(default=False),
    announcement_enabled: bool = Form(default=False),
    feed_before_minutes: int = Form(default=1440),
    announcement_timing_mode: str = Form(default="relative"),
    announcement_offset_direction: str = Form(default="before"),
    announcement_offset_minutes: int | None = Form(default=None),
    announcement_monday: str = Form(default=""),
    announcement_tuesday: str = Form(default=""),
    announcement_wednesday: str = Form(default=""),
    announcement_thursday: str = Form(default=""),
    announcement_friday: str = Form(default=""),
    announcement_saturday: str = Form(default=""),
    announcement_sunday: str = Form(default=""),
    announcement_target_monday: str = Form(default="0"),
    announcement_target_tuesday: str = Form(default="1"),
    announcement_target_wednesday: str = Form(default="2"),
    announcement_target_thursday: str = Form(default="3"),
    announcement_target_friday: str = Form(default="4"),
    announcement_target_saturday: str = Form(default="5"),
    announcement_target_sunday: str = Form(default="6"),
    late_approval: str = Form(),
    result_enabled: bool = Form(default=False),
    result_wait_minutes: int = Form(),
    result_timing_mode: str = Form(default="result_detected"),
    result_offset_direction: str = Form(default="after"),
    result_offset_minutes: int = Form(default=120),
    result_monday: str = Form(default=""),
    result_tuesday: str = Form(default=""),
    result_wednesday: str = Form(default=""),
    result_thursday: str = Form(default=""),
    result_friday: str = Form(default=""),
    result_saturday: str = Form(default=""),
    result_sunday: str = Form(default=""),
    result_target_monday: str = Form(default="0"),
    result_target_tuesday: str = Form(default="1"),
    result_target_wednesday: str = Form(default="2"),
    result_target_thursday: str = Form(default="3"),
    result_target_friday: str = Form(default="4"),
    result_target_saturday: str = Form(default="5"),
    result_target_sunday: str = Form(default="6"),
    allow_provisional_games: bool = Form(default=False),
    automatic_sync_enabled: bool = Form(default=False),
    automatic_generation_enabled: bool = Form(default=False),
    reminder_enabled: bool = Form(default=False),
    generation_lead_minutes: int = Form(default=120),
    generation_lead_days: int = Form(default=4),
    sync_interval_hours: int = Form(default=24),
    result_poll_interval_minutes: int = Form(default=15),
    identity_aliases: str | None = Form(default=None),
    auto_approve_announcements: bool = Form(default=False),
    auto_approve_results: bool = Form(default=False),
    auto_approve_announcements_acknowledged: bool = Form(default=False),
    auto_approve_results_acknowledged: bool = Form(default=False),
    club_matchday_feed_mode: str = Form(default="separate"),
    club_matchday_primary_team_id: str = Form(default=""),
    reminder_feed_before_minutes: int = Form(default=360),
    reminder_timing_mode: str = Form(default="relative"),
    reminder_monday: str = Form(default=""),
    reminder_tuesday: str = Form(default=""),
    reminder_wednesday: str = Form(default=""),
    reminder_thursday: str = Form(default=""),
    reminder_friday: str = Form(default=""),
    reminder_saturday: str = Form(default=""),
    reminder_sunday: str = Form(default=""),
    reminder_target_monday: str = Form(default="0"),
    reminder_target_tuesday: str = Form(default="1"),
    reminder_target_wednesday: str = Form(default="2"),
    reminder_target_thursday: str = Form(default="3"),
    reminder_target_friday: str = Form(default="4"),
    reminder_target_saturday: str = Form(default="5"),
    reminder_target_sunday: str = Form(default="6"),
    announcement_feed_output_count: int = Form(default=1),
    announcement_story_output_count: int = Form(default=1),
    reminder_feed_output_count: int = Form(default=1),
    reminder_story_output_count: int = Form(default=1),
    result_feed_output_count: int = Form(default=1),
    result_story_output_count: int = Form(default=1),
    announcement_feed_generation_count: int | None = Form(default=None),
    announcement_feed_publish_count: int | None = Form(default=None),
    announcement_story_generation_count: int | None = Form(default=None),
    announcement_story_publish_count: int | None = Form(default=None),
    reminder_feed_generation_count: int | None = Form(default=None),
    reminder_feed_publish_count: int | None = Form(default=None),
    reminder_story_generation_count: int | None = Form(default=None),
    reminder_story_publish_count: int | None = Form(default=None),
    result_feed_generation_count: int | None = Form(default=None),
    result_feed_publish_count: int | None = Form(default=None),
    result_story_generation_count: int | None = Form(default=None),
    result_story_publish_count: int | None = Form(default=None),
    image_prompt_feed: str = Form(default="default-image-feed"),
    image_prompt_story: str = Form(default="default-image-story"),
    text_prompt: str = Form(default="default-text-announcement"),
    result_image_prompt_feed: str = Form(default="default-image-feed"),
    result_image_prompt_story: str = Form(default="default-image-story"),
    result_text_prompt: str = Form(default="default-text-result"),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    team = db.scalar(select(Team).where(Team.id == team_id).with_for_update())
    if (
        not team
        or team.archived_at is not None
        or not require_visible(db, current, team.id)
        or (current.club_id and team.club_id != current.club_id)
    ):
        raise HTTPException(404, "Mannschaft nicht gefunden")
    if expected_team_version is not None and team.version != expected_team_version:
        raise HTTPException(409, "Die Regeln wurden zwischenzeitlich geändert")
    previous_rules = deepcopy(team.rules or {})
    previous_auto_approve_announcements = bool(
        (team.rules or {}).get("auto_approve_announcements", False)
    )
    previous_auto_approve_results = bool((team.rules or {}).get("auto_approve_results", False))
    if (
        auto_approve_announcements
        and not previous_auto_approve_announcements
        and not auto_approve_announcements_acknowledged
    ):
        raise HTTPException(
            422,
            "Die automatische Freigabe für Spielankündigungen muss ausdrücklich bestätigt werden",
        )
    if (
        auto_approve_results
        and not previous_auto_approve_results
        and not auto_approve_results_acknowledged
    ):
        raise HTTPException(
            422,
            "Die automatische Freigabe für Ergebnismeldungen muss ausdrücklich bestätigt werden",
        )
    existing_publication_slots = _serialize_team_publication_slots(db, team)
    if late_approval not in {"publish_now", "manual", "skip", "next_story"}:
        raise HTTPException(422)
    if announcement_timing_mode not in {"relative", "weekday_fixed"}:
        raise HTTPException(422, "Ungueltiger Zeitpunkt fuer Ankuendigungen")
    if result_timing_mode not in {"result_detected", "relative", "weekday_fixed"}:
        raise HTTPException(422, "Ungueltiger Zeitpunkt fuer Ergebnisse")
    if reminder_timing_mode not in {"relative", "weekday_fixed"}:
        raise HTTPException(422, "Ungültiger Zeitpunkt für Erinnerungsbeiträge")
    if club_matchday_feed_mode not in {
        "separate",
        "announcements",
        "announcements_and_results",
    }:
        raise HTTPException(422, "Ungueltige Vereins-Feed-Buendelung")
    club_key = normalize_club_name(team.club)
    club_teams = [team]
    club_teams.extend(
        sibling
        for sibling in db.scalars(
            select(Team).where(
                Team.instagram_page_id == team.instagram_page_id,
                Team.active.is_(True),
                Team.archived_at.is_(None),
                Team.id != team.id,
            )
        )
        if normalize_club_name(sibling.club) == club_key
    )
    club_matchday_primary_team_id = club_matchday_primary_team_id.strip()
    if club_matchday_primary_team_id and club_matchday_primary_team_id not in {
        candidate.id for candidate in club_teams
    }:
        raise HTTPException(
            422,
            "Die bevorzugte Mannschaft gehört nicht zu diesem Verein und dieser Instagram-Seite",
        )
    if announcement_offset_direction not in {"before", "after"} or result_offset_direction not in {
        "before",
        "after",
    }:
        raise HTTPException(422, "Ungueltige Zeitrichtung")
    announcement_offset_minutes = (
        feed_before_minutes if announcement_offset_minutes is None else announcement_offset_minutes
    )
    if not 0 <= announcement_offset_minutes <= 43200 or not 0 <= result_offset_minutes <= 43200:
        raise HTTPException(422, "Relative Zeitpunkte muessen zwischen 0 und 43200 Minuten liegen")
    if not 0 <= generation_lead_minutes <= 10080:
        raise HTTPException(422, "Der Generierungsvorlauf muss zwischen 0 und 10080 Minuten liegen")
    if not 0 <= generation_lead_days <= 30:
        raise HTTPException(422, "Der Generierungsvorlauf muss zwischen 0 und 30 Tagen liegen")
    if not 1 <= sync_interval_hours <= 168:
        raise HTTPException(422, "Das Abrufintervall muss zwischen 1 und 168 Stunden liegen")
    if not RESULT_POLL_MINUTES_MIN <= result_poll_interval_minutes <= 120:
        raise HTTPException(
            422,
            "Die Ergebnisprüfung am Spieltag kann frühestens alle 10 Minuten durchgeführt werden.",
        )
    try:
        parsed_identity_aliases = (
            list(validate_identity_aliases(identity_aliases))
            if identity_aliases is not None
            else list((team.rules or {}).get("identity_aliases", []))
        )
    except TeamIdentityError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not 0 <= reminder_feed_before_minutes <= 10080:
        raise HTTPException(422, "Der Erinnerungszeitpunkt ist ungültig")
    output_counts = {
        "announcement_feed_output_count": announcement_feed_output_count,
        "announcement_story_output_count": announcement_story_output_count,
        "reminder_feed_output_count": reminder_feed_output_count,
        "reminder_story_output_count": reminder_story_output_count,
        "result_feed_output_count": result_feed_output_count,
        "result_story_output_count": result_story_output_count,
    }
    if any(not 0 <= value <= 10 for value in output_counts.values()):
        raise HTTPException(422, "Ausgabeanzahlen müssen zwischen 0 und 10 liegen")
    structured_counts: dict[str, int] = {}
    count_rows = (
        (
            "announcement",
            announcement_feed_output_count,
            announcement_story_output_count,
            announcement_feed_generation_count,
            announcement_feed_publish_count,
            announcement_story_generation_count,
            announcement_story_publish_count,
        ),
        (
            "reminder",
            reminder_feed_output_count,
            reminder_story_output_count,
            reminder_feed_generation_count,
            reminder_feed_publish_count,
            reminder_story_generation_count,
            reminder_story_publish_count,
        ),
        (
            "result",
            result_feed_output_count,
            result_story_output_count,
            result_feed_generation_count,
            result_feed_publish_count,
            result_story_generation_count,
            result_story_publish_count,
        ),
    )
    for (
        post_type_key,
        legacy_feed,
        legacy_story,
        feed_generated,
        feed_published,
        story_generated,
        story_published,
    ) in count_rows:
        feed_generated = legacy_feed if feed_generated is None else feed_generated
        feed_published = legacy_feed if feed_published is None else feed_published
        story_generated = legacy_story if story_generated is None else story_generated
        story_published = legacy_story if story_published is None else story_published
        values = (feed_generated, feed_published, story_generated, story_published)
        if any(not 0 <= value <= 10 for value in values):
            raise HTTPException(
                422, "Erstellungs- und Auswahlzahlen muessen zwischen 0 und 10 liegen"
            )
        if feed_published > feed_generated or story_published > story_generated:
            raise HTTPException(
                422,
                "Die Standardauswahl darf die erzeugte Anzahl nicht ueberschreiten",
            )
        structured_counts.update(
            {
                f"{post_type_key}_feed_generation_count": feed_generated,
                f"{post_type_key}_feed_publish_count": feed_published,
                f"{post_type_key}_story_generation_count": story_generated,
                f"{post_type_key}_story_publish_count": story_published,
            }
        )

    for enabled, post_type, feed_count, story_count in (
        (
            announcement_enabled,
            "Ankündigung",
            announcement_feed_output_count,
            announcement_story_output_count,
        ),
        (
            reminder_enabled,
            "Erinnerung",
            reminder_feed_output_count,
            reminder_story_output_count,
        ),
        (
            result_enabled,
            "Ergebnis",
            result_feed_output_count,
            result_story_output_count,
        ),
    ):
        if enabled and feed_count == 0 and story_count == 0:
            raise HTTPException(
                422,
                f"Für {post_type} muss mindestens eine Feed- oder Story-Ausgabe aktiv sein",
            )
    grouped_announcements = club_matchday_feed_mode in {
        "announcements",
        "announcements_and_results",
    }
    grouped_results = club_matchday_feed_mode == "announcements_and_results"
    if (grouped_announcements and structured_counts["announcement_feed_publish_count"] != 1) or (
        grouped_results and structured_counts["result_feed_publish_count"] != 1
    ):
        raise HTTPException(
            422,
            "Für gebündelte Vereins-Karussells muss je beteiligtem Beitragstyp "
            "genau ein Feed-Bild pro Spiel eingestellt sein",
        )
    active_story_slots = {
        post_type: max(
            [
                int(getattr(item, "media_slot", 1) or 1)
                for item in db.scalars(
                    select(StoryRule).where(
                        StoryRule.team_id == team.id,
                        StoryRule.post_type == post_type,
                        StoryRule.active.is_(True),
                    )
                )
            ]
            or [0]
        )
        for post_type in ("announcement", "reminder", "result")
    }
    for post_type, configured in {
        "announcement": structured_counts["announcement_story_generation_count"],
        "reminder": structured_counts["reminder_story_generation_count"],
        "result": structured_counts["result_story_generation_count"],
    }.items():
        if active_story_slots[post_type] > configured:
            raise HTTPException(
                422,
                f"Story-Ausgabe {active_story_slots[post_type]} wird für {post_type} "
                "noch von einem aktiven Story-Zeitpunkt verwendet",
            )
    if automatic_generation_enabled and not automatic_sync_enabled:
        raise HTTPException(
            422,
            "Automatische Entwürfe erfordern den automatischen FUSSBALL.DE-Abruf",
        )
    announcement_weekday_times = {
        str(index): value
        for index, value in enumerate(
            [
                announcement_monday,
                announcement_tuesday,
                announcement_wednesday,
                announcement_thursday,
                announcement_friday,
                announcement_saturday,
                announcement_sunday,
            ]
        )
        if value
    }
    result_weekday_times = {
        str(index): value
        for index, value in enumerate(
            [
                result_monday,
                result_tuesday,
                result_wednesday,
                result_thursday,
                result_friday,
                result_saturday,
                result_sunday,
            ]
        )
        if value
    }
    reminder_weekday_times = {
        str(index): value
        for index, value in enumerate(
            [
                reminder_monday,
                reminder_tuesday,
                reminder_wednesday,
                reminder_thursday,
                reminder_friday,
                reminder_saturday,
                reminder_sunday,
            ]
        )
        if value
    }
    announcement_weekday_targets = {
        str(index): value
        for index, value in enumerate(
            [
                announcement_target_monday,
                announcement_target_tuesday,
                announcement_target_wednesday,
                announcement_target_thursday,
                announcement_target_friday,
                announcement_target_saturday,
                announcement_target_sunday,
            ]
        )
    }
    result_weekday_targets = {
        str(index): value
        for index, value in enumerate(
            [
                result_target_monday,
                result_target_tuesday,
                result_target_wednesday,
                result_target_thursday,
                result_target_friday,
                result_target_saturday,
                result_target_sunday,
            ]
        )
    }
    reminder_weekday_targets = {
        str(index): value
        for index, value in enumerate(
            [
                reminder_target_monday,
                reminder_target_tuesday,
                reminder_target_wednesday,
                reminder_target_thursday,
                reminder_target_friday,
                reminder_target_saturday,
                reminder_target_sunday,
            ]
        )
    }
    if any(
        value not in {"0", "1", "2", "3", "4", "5", "6"}
        for value in [
            *announcement_weekday_targets.values(),
            *reminder_weekday_targets.values(),
            *result_weekday_targets.values(),
        ]
    ):
        raise HTTPException(422, "Ungueltiger Veroeffentlichungs-Wochentag")
    for value in [
        *announcement_weekday_times.values(),
        *reminder_weekday_times.values(),
        *result_weekday_times.values(),
    ]:
        try:
            parsed = datetime.strptime(value, "%H:%M")
        except ValueError as exc:
            raise HTTPException(422, "Ungueltige feste Uhrzeit") from exc
        if parsed.strftime("%H:%M") != value:
            raise HTTPException(422, "Ungueltige feste Uhrzeit")
    team.rules = {
        **team.rules,
        "announcement_enabled": announcement_enabled,
        "feed_before_minutes": feed_before_minutes,
        "announcement_timing_mode": announcement_timing_mode,
        "announcement_offset_direction": announcement_offset_direction,
        "announcement_offset_minutes": announcement_offset_minutes,
        "announcement_weekday_times": announcement_weekday_times,
        "announcement_weekday_targets": announcement_weekday_targets,
        "late_approval": late_approval,
        "result_enabled": result_enabled,
        "result_wait_minutes": result_wait_minutes,
        "result_timing_mode": result_timing_mode,
        "result_offset_direction": result_offset_direction,
        "result_offset_minutes": result_offset_minutes,
        "result_weekday_times": result_weekday_times,
        "result_weekday_targets": result_weekday_targets,
        "allow_provisional_games": allow_provisional_games,
        "automatic_sync_enabled": automatic_sync_enabled,
        "automatic_generation_enabled": automatic_generation_enabled,
        "reminder_enabled": reminder_enabled,
        "reminder_timing_mode": reminder_timing_mode,
        "reminder_weekday_times": reminder_weekday_times,
        "reminder_weekday_targets": reminder_weekday_targets,
        "generation_lead_minutes": generation_lead_minutes,
        "generation_lead_days": generation_lead_days,
        "sync_interval_hours": sync_interval_hours,
        "result_poll_interval_minutes": result_poll_interval_minutes,
        "identity_aliases": parsed_identity_aliases,
        "auto_approve_announcements": auto_approve_announcements,
        "auto_approve_results": auto_approve_results,
        "club_matchday_feed_mode": club_matchday_feed_mode,
        "club_matchday_primary_team_id": club_matchday_primary_team_id or None,
        "reminder_feed_before_minutes": reminder_feed_before_minutes,
        **output_counts,
        **structured_counts,
        # Prompt selection is platform-owned. Keep any existing protected
        # assignment, otherwise use the server-side platform defaults. Values
        # submitted by a club form are deliberately ignored.
        "image_prompt_feed": (team.rules or {}).get("image_prompt_feed", "default-image-feed"),
        "image_prompt_story": (team.rules or {}).get("image_prompt_story", "default-image-story"),
        "text_prompt": (team.rules or {}).get("text_prompt", "default-text-announcement"),
        "image_prompt_feed_result": (team.rules or {}).get(
            "image_prompt_feed_result", "default-image-feed"
        ),
        "image_prompt_story_result": (team.rules or {}).get(
            "image_prompt_story_result", "default-image-story"
        ),
        "text_prompt_result": (team.rules or {}).get("text_prompt_result", "default-text-result"),
        "publication_rule_slots": existing_publication_slots,
    }
    if preserve_legacy_weekday_settings:
        # Die neue Oberfläche bearbeitet die kanonischen PublicationRuleSlots.
        # Alte Felder bleiben für bestehende Worker-/Fallbackpfade unverändert,
        # statt beim Speichern der reinen UX-Einstellungen geleert zu werden.
        existing_rules = previous_rules
        announcement_weekday_times = deepcopy(existing_rules.get("announcement_weekday_times", {}))
        announcement_weekday_targets = deepcopy(
            existing_rules.get("announcement_weekday_targets", {})
        )
        reminder_weekday_times = deepcopy(existing_rules.get("reminder_weekday_times", {}))
        reminder_weekday_targets = deepcopy(existing_rules.get("reminder_weekday_targets", {}))
        result_weekday_times = deepcopy(existing_rules.get("result_weekday_times", {}))
        result_weekday_targets = deepcopy(existing_rules.get("result_weekday_targets", {}))
        team.rules = {
            **team.rules,
            "announcement_weekday_times": announcement_weekday_times,
            "announcement_weekday_targets": announcement_weekday_targets,
            "reminder_weekday_times": reminder_weekday_times,
            "reminder_weekday_targets": reminder_weekday_targets,
            "result_weekday_times": result_weekday_times,
            "result_weekday_targets": result_weekday_targets,
        }
    team.version += 1
    # This is deliberately a club/page setting even though rules are stored on
    # teams. Keeping all sibling teams in sync prevents one half of a matchday
    # from silently opting out of a configured carousel.
    grouped_team_ids = [team.id]
    for sibling in club_teams:
        if sibling.id == team.id:
            continue
        sibling.rules = {
            **(sibling.rules or {}),
            "club_matchday_feed_mode": club_matchday_feed_mode,
            "club_matchday_primary_team_id": club_matchday_primary_team_id or None,
        }
        sibling.version += 1
        grouped_team_ids.append(sibling.id)
    sync_team_rule_sets(db, team)
    sync_state = db.get(FussballSyncState, team.id)
    if automatic_sync_enabled:
        if sync_state is None:
            db.add(
                FussballSyncState(
                    club_id=team.club_id,
                    team_id=team.id,
                    status="idle",
                    next_poll_at=datetime.now(timezone.utc),
                )
            )
        elif sync_state.status != "running":
            sync_state.status = "idle"
            sync_state.next_poll_at = datetime.now(timezone.utc)
            sync_state.lease_owner = None
            sync_state.lease_expires_at = None
    elif sync_state is not None and sync_state.status != "running":
        sync_state.status = "disabled"
    audit(
        db,
        current,
        "rules.updated",
        "team",
        team.id,
        team.id,
        {**team.rules, "club_feed_setting_applied_to": grouped_team_ids},
    )
    for post_type, old_value, new_value in (
        (
            "announcement",
            previous_auto_approve_announcements,
            auto_approve_announcements,
        ),
        ("result", previous_auto_approve_results, auto_approve_results),
    ):
        if old_value != new_value:
            audit(
                db,
                current,
                "automatic_approval.changed",
                "team",
                team.id,
                team.id,
                {
                    "club_id": team.club_id,
                    "team_id": team.id,
                    "post_type": post_type,
                    "old_value": old_value,
                    "new_value": new_value,
                },
            )
    db.commit()
    return redirect(
        f"/rules?team_id={team.id}",
        f"Die Einstellungen für {team.display_name} wurden gespeichert.",
    )


def _team_for_rule_write(db: Session, current: User, team_id: str, expected_version: int) -> Team:
    team = db.scalar(select(Team).where(Team.id == team_id).with_for_update())
    if not team or team.archived_at is not None or not require_visible(db, current, team.id):
        raise HTTPException(404, "Mannschaft nicht gefunden")
    if current.club_id and team.club_id != current.club_id:
        raise HTTPException(404, "Mannschaft nicht gefunden")
    if team.version != expected_version:
        raise HTTPException(409, "Die Regeln wurden zwischenzeitlich geändert")
    return team


def _parse_optional_weekday(value: str, *, field: str) -> int | None:
    if value == "":
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise HTTPException(422, f"Ungültiger {field}") from exc
    if not 0 <= parsed <= 6:
        raise HTTPException(422, f"Ungültiger {field}")
    return parsed


@router.post("/rules/{team_id}/publication-slots")
def save_publication_rule_slot(
    team_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    expected_team_version: int = Form(),
    slot_key: str = Form(default=""),
    label: str = Form(default=""),
    post_type: str = Form(),
    media_kind: str = Form(),
    variant_number: int = Form(default=1),
    timing_model: str = Form(),
    match_weekday: str = Form(default=""),
    target_weekday: str = Form(default=""),
    local_time: str = Form(default=""),
    reference: str = Form(default="kickoff"),
    direction: str = Form(default="before"),
    offset_minutes: int = Form(default=0),
    instagram_page_id: str = Form(default=""),
    template: str = Form(default=""),
    reuse_media: bool = Form(default=False),
    sort_order: int = Form(default=0),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    team = _team_for_rule_write(db, current, team_id, expected_team_version)
    if post_type not in {"announcement", "reminder", "result"}:
        raise HTTPException(422, "Ungültiger Beitragstyp")
    if media_kind not in {"feed", "story"}:
        raise HTTPException(422, "Ungültiger Medientyp")
    if timing_model not in {"relative", "weekday_fixed", "result_detected", "manual"}:
        raise HTTPException(422, "Ungültiges Zeitmodell")
    if direction not in {"before", "after"}:
        raise HTTPException(422, "Ungültige Zeitrichtung")
    if reference not in {"kickoff", "planned_end", "result_detected", "approval"}:
        raise HTTPException(422, "Ungültiger Bezugspunkt")
    if not 0 <= offset_minutes <= 43200:
        raise HTTPException(422, "Der Zeitabstand muss zwischen 0 und 43200 Minuten liegen")
    match_day = _parse_optional_weekday(match_weekday, field="Spielwochentag")
    target_day = _parse_optional_weekday(target_weekday, field="Zielwochentag")
    if timing_model == "weekday_fixed":
        if match_day is None or target_day is None or not local_time:
            raise HTTPException(
                422, "Kalenderbasierte Regeln benötigen Spieltag, Zieltag und Uhrzeit"
            )
        try:
            parsed_time = datetime.strptime(local_time, "%H:%M")
        except ValueError as exc:
            raise HTTPException(422, "Ungültige Uhrzeit") from exc
        if parsed_time.strftime("%H:%M") != local_time:
            raise HTTPException(422, "Ungültige Uhrzeit")
    else:
        target_day = None
        local_time = ""
    generated = int(
        (team.rules or {}).get(
            f"{post_type}_{media_kind}_generation_count",
            (team.rules or {}).get(f"{post_type}_{media_kind}_output_count", 1),
        )
    )
    if not 1 <= variant_number <= max(0, generated):
        raise HTTPException(
            422,
            f"{media_kind.title()}-Variante muss zwischen 1 und {max(0, generated)} liegen",
        )
    if instagram_page_id:
        page = db.scalar(
            select(InstagramPage).where(
                InstagramPage.id == instagram_page_id,
                InstagramPage.club_id == team.club_id,
                InstagramPage.archived_at.is_(None),
            )
        )
        if not page:
            raise HTTPException(422, "Instagram-Seite gehört nicht zu diesem Verein")
    rows = _serialize_team_publication_slots(db, team)
    existing_index = next(
        (index for index, row in enumerate(rows) if row.get("slot_key") == slot_key), None
    )
    if slot_key and existing_index is None:
        raise HTTPException(404, "Veröffentlichungsregel nicht gefunden")
    duplicate_media_slots = [
        row
        for row in rows
        if row.get("slot_key") != slot_key
        and row.get("post_type") == post_type
        and row.get("media_kind") == media_kind
        and int(row.get("variant_number") or 1) == variant_number
        and row.get("match_weekday") == match_day
    ]
    if duplicate_media_slots and (
        not reuse_media or any(not row.get("reuse_media") for row in duplicate_media_slots)
    ):
        raise HTTPException(
            422,
            "Diese Variante ist für diesen Spielwochentag bereits eingeplant. "
            "Aktivieren Sie bei allen betroffenen Regeln ausdrücklich die Wiederverwendung "
            "derselben Datei.",
        )
    stable_key = slot_key or f"slot-{secrets.token_hex(12)}"
    generated_label = automatic_rule_label(
        post_type=post_type,
        media_kind=media_kind,
        timing_model=timing_model,
        match_weekday=match_day,
        target_weekday=target_day,
        local_time=local_time or None,
        offset_minutes=offset_minutes,
    )
    payload = {
        "slot_key": stable_key,
        "post_type": post_type,
        "label": (label.strip() or generated_label)[:160],
        "media_kind": media_kind,
        "variant_number": variant_number,
        "timing_model": timing_model,
        "reference": "result_detected" if timing_model == "result_detected" else reference,
        "direction": direction,
        "offset_minutes": offset_minutes,
        "match_weekday": match_day,
        "target_weekday": target_day,
        "local_time": local_time or None,
        "timezone": team.timezone,
        "sort_order": sort_order,
        "instagram_page_id": instagram_page_id or None,
        "template": template.strip()[:100] or None,
        "reuse_media": reuse_media,
    }
    action = (
        "publication_rule_slot.updated"
        if existing_index is not None
        else "publication_rule_slot.created"
    )
    if existing_index is None:
        rows.append(payload)
    else:
        rows[existing_index] = payload
    team.rules = {
        **(team.rules or {}),
        "publication_rule_slots": rows,
        "publication_rule_slots_configured": True,
    }
    team.version += 1
    sync_team_rule_sets(db, team)
    audit(
        db,
        current,
        action,
        "publication_rule_slot",
        stable_key,
        team.id,
        {
            "post_type": post_type,
            "media_kind": media_kind,
            "match_weekday": match_day,
            "timing_model": timing_model,
        },
    )
    db.commit()
    return redirect(
        f"/rules?team_id={team.id}#publication-rules", "Veröffentlichungsregel gespeichert"
    )


@router.post("/rules/{team_id}/publication-slots/{slot_key}/delete")
def delete_publication_rule_slot(
    team_id: str,
    slot_key: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    expected_team_version: int = Form(),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    team = _team_for_rule_write(db, current, team_id, expected_team_version)
    rows = _serialize_team_publication_slots(db, team)
    kept = [row for row in rows if row.get("slot_key") != slot_key]
    if len(kept) == len(rows):
        raise HTTPException(404, "Veröffentlichungsregel nicht gefunden")
    team.rules = {
        **(team.rules or {}),
        "publication_rule_slots": kept,
        "publication_rule_slots_configured": True,
    }
    team.version += 1
    sync_team_rule_sets(db, team)
    audit(
        db,
        current,
        "publication_rule_slot.deleted",
        "publication_rule_slot",
        slot_key,
        team.id,
        {"deletion_mode": "new_rule_version"},
    )
    db.commit()
    return redirect(
        f"/rules?team_id={team.id}#publication-rules", "Veröffentlichungsregel gelöscht"
    )


@router.post("/rules/{team_id}/publication-weekdays/copy")
def copy_publication_weekday(
    team_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    expected_team_version: int = Form(),
    source_weekday: int = Form(),
    target_weekday: int = Form(),
    replace_existing: bool = Form(default=False),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    team = _team_for_rule_write(db, current, team_id, expected_team_version)
    if source_weekday == target_weekday or not all(
        0 <= value <= 6 for value in (source_weekday, target_weekday)
    ):
        raise HTTPException(422, "Quell- und Zielwochentag müssen verschieden und gültig sein")
    rows = _serialize_team_publication_slots(db, team)
    source = [row for row in rows if row.get("match_weekday") == source_weekday]
    if not source:
        raise HTTPException(422, "Für den Quellwochentag existiert keine Regel")
    target = [row for row in rows if row.get("match_weekday") == target_weekday]
    if target and not replace_existing:
        raise HTTPException(409, "Für den Zielwochentag existieren bereits Regeln")
    if replace_existing:
        rows = [row for row in rows if row.get("match_weekday") != target_weekday]
    shift = target_weekday - source_weekday
    copied = []
    for original in source:
        row = deepcopy(original)
        row["slot_key"] = f"slot-{secrets.token_hex(12)}"
        row["match_weekday"] = target_weekday
        if isinstance(row.get("target_weekday"), int):
            row["target_weekday"] = (row["target_weekday"] + shift) % 7
        row["label"] = (
            f"{row.get('label') or 'Veröffentlichung'} – {WEEKDAY_LABELS[target_weekday]}"
        )
        copied.append(row)
    rows.extend(copied)
    team.rules = {
        **(team.rules or {}),
        "publication_rule_slots": rows,
        "publication_rule_slots_configured": True,
    }
    team.version += 1
    sync_team_rule_sets(db, team)
    audit(
        db,
        current,
        "publication_weekday_rules.copied",
        "team",
        team.id,
        team.id,
        {
            "source_weekday": source_weekday,
            "target_weekday": target_weekday,
            "replaced": replace_existing,
            "slot_count": len(copied),
        },
    )
    db.commit()
    return redirect(f"/rules?team_id={team.id}#publication-rules", "Wochentagsregel kopiert")


@router.post("/rules/{team_id}/stories")
def create_story_rule(
    team_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    name: str = Form(),
    post_type: str = Form(),
    reference: str = Form(),
    direction: str = Form(),
    offset_minutes: int = Form(),
    fixed_time: str = Form(default=""),
    timing_mode: str = Form(default="relative"),
    weekday_monday: str = Form(default=""),
    weekday_tuesday: str = Form(default=""),
    weekday_wednesday: str = Form(default=""),
    weekday_thursday: str = Form(default=""),
    weekday_friday: str = Form(default=""),
    weekday_saturday: str = Form(default=""),
    weekday_sunday: str = Form(default=""),
    target_monday: str = Form(default="0"),
    target_tuesday: str = Form(default="1"),
    target_wednesday: str = Form(default="2"),
    target_thursday: str = Form(default="3"),
    target_friday: str = Form(default="4"),
    target_saturday: str = Form(default="5"),
    target_sunday: str = Form(default="6"),
    media_slot: int = Form(default=1),
    next_day: bool = Form(default=False),
    template: str = Form(),
    prompt_template: str = Form(default=""),
    instagram_page_id: str = Form(default=""),
    reuse_media: bool = Form(default=False),
    sort_order: int = Form(default=0),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    team = db.get(Team, team_id)
    if not team or team.archived_at is not None:
        raise HTTPException(404, "Mannschaft nicht gefunden")
    name = name.strip()
    if not name:
        raise HTTPException(422, "Name darf nicht leer sein")
    if reference not in {
        "kickoff",
        "planned_end",
        "result_detected",
        "approval",
        "next_day",
    } or direction not in {"before", "after"}:
        raise HTTPException(422, "Ungültiger Bezugspunkt")
    if timing_mode not in {"relative", "weekday_fixed"}:
        raise HTTPException(422, "Ungueltiger Story-Zeitmodus")
    if post_type not in {"announcement", "reminder", "result"}:
        raise HTTPException(422, "Ungültiger Beitragstyp")
    configured_story_count = int(
        (team.rules or {}).get(
            f"{post_type}_story_generation_count",
            (team.rules or {}).get(
                f"{post_type}_story_output_count",
                max(1, media_slot),
            ),
        )
    )
    if not 1 <= media_slot <= configured_story_count:
        raise HTTPException(
            422,
            f"Story-Ausgabe muss zwischen 1 und {configured_story_count} liegen",
        )
    weekday_times = {
        str(index): value
        for index, value in enumerate(
            [
                weekday_monday,
                weekday_tuesday,
                weekday_wednesday,
                weekday_thursday,
                weekday_friday,
                weekday_saturday,
                weekday_sunday,
            ]
        )
        if value
    }
    for value in weekday_times.values():
        try:
            datetime.strptime(value, "%H:%M")
        except ValueError as exc:
            raise HTTPException(422, "Ungueltige Story-Uhrzeit") from exc
    weekday_targets = {
        str(index): value
        for index, value in enumerate(
            [
                target_monday,
                target_tuesday,
                target_wednesday,
                target_thursday,
                target_friday,
                target_saturday,
                target_sunday,
            ]
        )
    }
    if any(value not in {"0", "1", "2", "3", "4", "5", "6"} for value in weekday_targets.values()):
        raise HTTPException(422, "Ungültiger Story-Veröffentlichungs-Wochentag")
    item = db.scalar(select(StoryRule).where(StoryRule.team_id == team_id, StoryRule.name == name))
    if item and item.active:
        raise HTTPException(409, "Ein Story-Zeitpunkt mit diesem Namen existiert bereits")
    restored = item is not None
    if item is None:
        item = StoryRule(team_id=team_id, name=name)
        db.add(item)
    item.active = True
    item.post_type = post_type
    item.reference = reference
    item.direction = direction
    item.offset_minutes = offset_minutes
    item.fixed_time = fixed_time or None
    item.timing_mode = timing_mode
    item.weekday_times = weekday_times
    item.weekday_targets = weekday_targets
    item.media_slot = media_slot
    item.next_day = next_day
    item.template = template
    protected_prompt_keys = {
        "announcement": "image_prompt_story",
        "reminder": "image_prompt_story",
        "result": "image_prompt_story_result",
    }
    item.prompt_template = (team.rules or {}).get(
        protected_prompt_keys[post_type], "default-image-story"
    )
    item.instagram_page_id = instagram_page_id or None
    item.reuse_media = reuse_media
    item.sort_order = sort_order
    db.flush()
    _replace_legacy_story_publication_rows(db, team, item)
    sync_team_rule_sets(db, team)
    audit(
        db,
        current,
        "story_rule.restored" if restored else "story_rule.created",
        "story_rule",
        item.id,
        team_id,
        {"name": item.name},
    )
    db.commit()
    return redirect(f"/rules?team_id={team_id}")


@router.post("/rules/{team_id}/stories/{story_rule_id}/delete")
def delete_story_rule(
    team_id: str,
    story_rule_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    item = db.scalar(
        select(StoryRule).where(
            StoryRule.id == story_rule_id,
            StoryRule.team_id == team_id,
            StoryRule.active.is_(True),
        )
    )
    if item is None:
        raise HTTPException(404, "Story-Zeitpunkt nicht gefunden")

    item.active = False
    team = db.get(Team, team_id)
    if team is not None:
        _replace_legacy_story_publication_rows(db, team, item)
        sync_team_rule_sets(db, team)
    audit(
        db,
        current,
        "story_rule.deleted",
        "story_rule",
        item.id,
        team_id,
        {"name": item.name, "deletion_mode": "deactivated"},
    )
    db.commit()
    return redirect(
        f"/rules?team_id={team_id}",
        "Story-Zeitpunkt gelöscht",
    )


@router.get("/posts", response_class=HTMLResponse)
def posts(
    request: Request,
    days: int = Query(default=7, ge=1, le=90),
    media_format: str = Query(default="all", alias="format"),
    channel: str = Query(default="all"),
    status: str = Query(default="all"),
    team: str = Query(default="all"),
    content: str = Query(default="all"),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    if media_format not in {"all", "feed", "story"}:
        raise HTTPException(422, "Ungültiger Formatfilter")
    if status not in {"all", "attention", "planned", "published"}:
        raise HTTPException(422, "Ungültiger Statusfilter")
    if not current.club_id:
        raise HTTPException(403, "Eindeutiger Vereinskontext fehlt")
    visible_teams = [
        row
        for row in db.scalars(
            select(Team)
            .where(Team.club_id == current.club_id, Team.archived_at.is_(None))
            .order_by(Team.display_name)
        )
        if require_visible(db, current, row.id)
    ]
    visible_team_ids = {row.id for row in visible_teams}
    if team != "all" and team not in visible_team_ids:
        raise HTTPException(404, "Mannschaft nicht gefunden")
    channels = operational_channels(db, current.club_id)
    channel_ids = {row.connection_id for row in channels}
    if channel != "all" and channel not in channel_ids:
        raise HTTPException(422, "Ungültiger Kanalfilter")

    now = datetime.now(timezone.utc)
    published_since = now - timedelta(days=2)
    planned_until = now + timedelta(days=days)

    recent_published = []
    relevant_open = []
    if visible_team_ids:
        recent_published = list(
            db.scalars(
                select(PublicationJob).where(
                    PublicationJob.club_id == current.club_id,
                    PublicationJob.team_id.in_(visible_team_ids),
                    PublicationJob.status == JobStatus.PUBLISHED,
                    PublicationJob.published_at.between(published_since, now),
                )
            )
        )
        relevant_open = list(
            db.scalars(
                select(PublicationJob).where(
                    PublicationJob.club_id == current.club_id,
                    PublicationJob.team_id.in_(visible_team_ids),
                    PublicationJob.scheduled_at >= now - timedelta(days=90),
                    PublicationJob.scheduled_at <= planned_until,
                    PublicationJob.status.notin_(
                        [JobStatus.PUBLISHED, JobStatus.CANCELLED, JobStatus.SKIPPED]
                    ),
                )
            )
        )
    jobs = list({row.id: row for row in [*recent_published, *relevant_open]}.values())
    views = publication_views(
        db,
        jobs,
        club_id=current.club_id,
        channels=channels,
        now=now,
    )

    # Keep old format links valid while the new content filter offers the
    # more precise cross-channel vocabulary.
    if content == "all" and media_format != "all":
        content = media_format
    if content not in {"all", "feed", "carousel", "story", "message"}:
        raise HTTPException(422, "Ungültiger Inhaltsfilter")

    def matches_content(row):
        if content == "all":
            return True
        if content == "feed":
            return row.job.kind in {"feed", "carousel"} and row.channel.channel_type != "whatsapp"
        if content == "message":
            return row.job.delivery_action == "send"
        return row.job.kind == content

    candidate_views = [
        row
        for row in views
        if (team == "all" or row.job.team_id == team)
        and (channel == "all" or row.channel.connection_id == channel)
    ]
    content_options = {
        "feed": any(
            row.job.kind in {"feed", "carousel"} and row.channel.channel_type != "whatsapp"
            for row in candidate_views
        ),
        "carousel": any(row.job.kind == "carousel" for row in candidate_views),
        "story": any(row.job.kind == "story" for row in candidate_views),
        "message": any(row.job.delivery_action == "send" for row in candidate_views),
    }
    base_views = [row for row in candidate_views if matches_content(row)]
    attention_rows = sorted(
        (row for row in base_views if row.attention and row.job.status != JobStatus.PUBLISHED),
        key=lambda row: row.scheduled_at,
    )
    planned_rows = sorted(
        (
            row
            for row in base_views
            if row.job.status not in {JobStatus.PUBLISHED, JobStatus.CANCELLED, JobStatus.SKIPPED}
            and row.scheduled_at >= now
            and not row.attention
        ),
        key=lambda row: row.scheduled_at,
    )
    published_rows = sorted(
        (row for row in base_views if row.job.status == JobStatus.PUBLISHED),
        key=lambda row: row.event_at,
        reverse=True,
    )
    if status == "attention":
        planned_rows = []
        published_rows = []
    elif status == "planned":
        attention_rows = []
        published_rows = []
    elif status == "published":
        attention_rows = []
        planned_rows = []

    return render(
        request,
        "posts.html",
        current,
        attention_rows=attention_rows,
        planned_rows=planned_rows,
        published_rows=published_rows,
        visible_teams=visible_teams,
        channels=channels,
        future_days=days,
        publication_format=media_format,
        selected_channel=channel,
        selected_status=status,
        selected_team=team,
        selected_content=content,
        content_options=content_options,
        published_since=published_since,
        planned_until=planned_until,
        summary={
            "attention": len([row for row in base_views if row.attention]),
            "planned": len(
                [
                    row
                    for row in base_views
                    if row.job.status
                    not in {JobStatus.PUBLISHED, JobStatus.CANCELLED, JobStatus.SKIPPED}
                    and row.scheduled_at >= now
                ]
            ),
            "published": len([row for row in base_views if row.job.status == JobStatus.PUBLISHED]),
        },
        title="Beiträge und Freigaben",
    )


def _manual_post_teams(db: Session, current: User) -> list[Team]:
    teams = db.scalars(
        select(Team)
        .where(Team.active.is_(True), Team.archived_at.is_(None))
        .order_by(Team.display_name)
    ).all()
    visible = []
    for team in teams:
        try:
            require(current, db, "edit_post", team.id)
        except HTTPException:
            continue
        visible.append(team)
    return visible


@router.get("/posts/manual/new", response_class=HTMLResponse)
def manual_post_form(
    request: Request,
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    teams = _manual_post_teams(db, current)
    default_zone = ZoneInfo(teams[0].timezone if teams else settings.timezone)
    default_publish_at = (
        datetime.now(timezone.utc).astimezone(default_zone) + timedelta(minutes=15)
    ).strftime("%Y-%m-%dT%H:%M")
    return render(
        request,
        "manual_post.html",
        current,
        teams=teams,
        submission_id=secrets.token_urlsafe(32),
        default_publish_at=default_publish_at,
        title="Beitrag manuell erstellen",
    )


@router.post("/posts/manual/new")
async def create_manual_post_route(
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    submission_id: str = Form(),
    team_id: str = Form(),
    kind: str = Form(),
    text_value: str = Form(alias="text"),
    scheduled_at_value: str = Form(alias="scheduled_at"),
    crop_specs_value: str = Form(default="", alias="crop_specs"),
    user_tags_value: str = Form(default="", alias="user_tags"),
    images: list[UploadFile] = File(),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    team = db.get(Team, team_id)
    if not team:
        raise HTTPException(404, "Mannschaft nicht gefunden")
    require(current, db, "edit_post", team.id)
    try:
        if kind == "carousel" and not 2 <= len(images) <= 10:
            raise ManualPostError("Ein Karussell benötigt 2 bis 10 Bilder")
        if kind != "carousel" and len(images) != 1:
            raise ManualPostError("Feed und Story benötigen genau ein Bild")
        crop_specs = parse_manual_crop_specs(crop_specs_value, len(images))
        user_tags_by_image = parse_manual_user_tag_specs(user_tags_value, len(images), kind)
        validated = []
        for image, crop in zip(images, crop_specs, strict=True):
            content = await image.read(MAX_MANUAL_IMAGE_BYTES + 1)
            validated.append(
                validate_manual_image(image.filename or "", image.content_type, content, kind, crop)
            )
        scheduled_at = parse_manual_publication_time(
            scheduled_at_value, team.timezone or settings.timezone
        )
        post, created = create_manual_post(
            db,
            settings,
            team=team,
            user=current,
            submission_id=submission_id,
            kind=kind,
            text=text_value,
            scheduled_at=scheduled_at,
            images=validated,
            user_tags_by_image=user_tags_by_image,
        )
    except ManualPostError as exc:
        raise HTTPException(422, str(exc)) from exc
    message = (
        "Manueller Beitrag zur Prüfung angelegt"
        if created
        else "Dieser manuelle Beitrag war bereits angelegt"
    )
    return redirect(f"/posts/{post.id}", message)


@router.get("/posts/{post_id}", response_class=HTMLResponse)
def post_detail(
    post_id: str, request: Request, current=Depends(current_user), db: Session = Depends(get_db)
):
    item = db.get(Post, post_id)
    if not item:
        raise HTTPException(404)
    require(current, db, "view", item.team_id)
    own_jobs = list(
        db.scalars(
            select(PublicationJob)
            .where(PublicationJob.post_id == item.id)
            .order_by(PublicationJob.scheduled_at)
        )
    )
    bundle = (item.design_snapshot or {}).get("club_matchday_carousel") or {}
    aggregate_bundle = bool(bundle.get("primary_post_id") and bundle.get("member_post_ids"))
    bundle_error = None
    if aggregate_bundle:
        try:
            primary, bundle_posts, jobs, job_posts = matchday_bundle_jobs(db, item)
        except ClubCarouselConflict as exc:
            bundle_error = str(exc)
            primary, bundle_posts, jobs, job_posts = matchday_bundle_jobs(
                db,
                item,
                allow_incomplete=True,
            )
        if primary.id != item.id:
            return redirect(
                f"/posts/{primary.id}",
                "Der gemeinsame Spieltagsbeitrag wird vollständig angezeigt",
            )
        for member in bundle_posts:
            require(current, db, "view", member.team_id)
    else:
        bundle_posts = [item]
        jobs = own_jobs
        job_posts = {job.id: item for job in jobs}
    detail_channels = operational_channels(db, item.club_id)
    detail_publications = publication_views(
        db,
        jobs,
        club_id=item.club_id,
        channels=detail_channels,
    )
    publication_groups = group_views_by_channel(detail_publications)
    publication_by_job = {row.job.id: row for row in detail_publications}
    pages = db.scalars(
        select(InstagramPage).where(
            InstagramPage.club_id == item.club_id,
            InstagramPage.archived_at.is_(None),
        )
    ).all()
    detail_team = db.scalar(
        select(Team).where(Team.id == item.team_id, Team.club_id == item.club_id)
    )
    detail_game = (
        db.scalar(select(Game).where(Game.id == item.game_id, Game.club_id == item.club_id))
        if item.game_id
        else None
    )
    detail_page = next((page for page in pages if page.id == item.instagram_page_id), None)
    current_media_asset = db.get(MediaAsset, item.media_asset_id) if item.media_asset_id else None
    alternative_media_assets = db.scalars(
        select(MediaAsset)
        .where(
            MediaAsset.team_id == item.team_id,
            MediaAsset.active.is_(True),
            MediaAsset.available.is_(True),
            MediaAsset.reserved_game_id.is_(None),
            MediaAsset.uses == 0,
        )
        .order_by(MediaAsset.filename)
    ).all()
    current_media_assets_by_post: dict[str, MediaAsset | None] = {}
    alternative_media_assets_by_post: dict[str, list[MediaAsset]] = {}
    for member in bundle_posts:
        current_media_assets_by_post[member.id] = (
            db.get(MediaAsset, member.media_asset_id) if member.media_asset_id else None
        )
        alternative_media_assets_by_post[member.id] = list(
            db.scalars(
                select(MediaAsset)
                .where(
                    MediaAsset.club_id == member.club_id,
                    MediaAsset.team_id == member.team_id,
                    MediaAsset.active.is_(True),
                    MediaAsset.available.is_(True),
                    MediaAsset.reserved_game_id.is_(None),
                    MediaAsset.uses == 0,
                )
                .order_by(MediaAsset.filename)
            )
        )
    from app.rendering.service import Renderer

    checks = {}
    carousel_items = {}
    now = datetime.now(timezone.utc)
    late_jobs = {}
    renderer = Renderer(settings.generated_root, settings.media_root, Path("data/uploads"))
    for job in jobs:
        scheduled_at = job.scheduled_at
        if scheduled_at.tzinfo is None:
            scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
        late_jobs[job.id] = scheduled_at < now
        media_items = list(
            db.scalars(
                select(PublicationMediaItem)
                .where(PublicationMediaItem.publication_job_id == job.id)
                .order_by(PublicationMediaItem.position)
            )
        )
        carousel_items[job.id] = media_items
        try:
            if job.kind == "carousel":
                if not 2 <= len(media_items) <= 10:
                    raise ValueError("Karussell benötigt 2 bis 10 geordnete Bilder")
                reports = [
                    renderer.validate(Path(media.media_path), "feed") for media in media_items
                ]
                checks[job.id] = (
                    f"{len(reports)} PNGs geprüft – jeweils "
                    f"{reports[0]['width']} × {reports[0]['height']}"
                )
            else:
                report = renderer.validate(Path(job.media_path), job.kind)
                checks[job.id] = f"PNG geprüft – {report['width']} × {report['height']}"
        except ValueError as exc:
            checks[job.id] = f"Prüfung fehlgeschlagen – {exc}"
    job_teams = {job.id: db.get(Team, job.team_id) for job in jobs}
    active_attempt_job_ids = set(
        db.scalars(
            select(MetaPublishingAttempt.publication_job_id).where(
                MetaPublishingAttempt.publication_job_id.in_([job.id for job in jobs]),
                MetaPublishingAttempt.active_key.is_not(None),
            )
        )
    )
    platform_attempt_job_ids = set(
        db.scalars(
            select(MetaPublishingAttempt.publication_job_id).where(
                MetaPublishingAttempt.publication_job_id.in_([job.id for job in jobs])
            )
        )
    )
    schedule_values = {}
    schedule_timezones = {}
    can_reschedule_jobs = {}
    for job in jobs:
        job_team = job_teams[job.id]
        timezone_name = (job_team.timezone if job_team else None) or settings.timezone
        schedule_timezones[job.id] = timezone_name
        scheduled_at = job.scheduled_at
        if scheduled_at.tzinfo is None:
            scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
        schedule_values[job.id] = scheduled_at.astimezone(ZoneInfo(timezone_name)).strftime(
            "%Y-%m-%dT%H:%M"
        )
        permitted = bool(job_team and allowed(db, current, "edit_post", job.team_id))
        if aggregate_bundle and job.kind == "carousel":
            permitted = permitted and all(
                allowed(db, current, "edit_post", member.team_id) for member in bundle_posts
            )
        can_reschedule_jobs[job.id] = bool(
            permitted
            and job.status in EDITABLE_JOB_STATUSES
            and not job.published_at
            and not job.platform_id
            and not job.attempts
            and not job.locked_at
            and job.id not in active_attempt_job_ids
        )
    can_edit_all = all(allowed(db, current, "edit_post", member.team_id) for member in bundle_posts)
    can_delete_all = all(allowed(db, current, "approve", member.team_id) for member in bundle_posts)
    incomplete_members = [
        member
        for member in bundle_posts
        if member.status == PostStatus.CREATING
        or PARTIAL_GENERATION_WARNING in (member.critical_warnings or [])
    ]
    resumable_generation_job = None
    if incomplete_members:
        member_game_ids = [member.game_id for member in incomplete_members if member.game_id]
        if member_game_ids:
            resumable_generation_job = db.scalar(
                select(GenerationJob)
                .where(
                    GenerationJob.club_id == item.club_id,
                    GenerationJob.game_id.in_(member_game_ids),
                    GenerationJob.post_type == item.post_type,
                    GenerationJob.status.in_(
                        {
                            GenerationJobStatus.FAILED,
                            GenerationJobStatus.MANUAL_REVIEW_REQUIRED,
                        }
                    ),
                )
                .order_by(GenerationJob.updated_at.desc(), GenerationJob.created_at.desc())
            )
    carousel_job = next((job for job in jobs if job.kind == "carousel"), None)
    can_reorder_carousel = bool(
        aggregate_bundle
        and not bundle_error
        and can_edit_all
        and carousel_job
        and carousel_job.status
        not in {
            JobStatus.PUBLISHING,
            JobStatus.PUBLISHED,
            JobStatus.RETRY,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.SKIPPED,
            JobStatus.UNCERTAIN,
        }
        and not carousel_job.published_at
        and not carousel_job.platform_id
        and not carousel_job.attempts
        and not carousel_job.locked_at
        and carousel_job.id not in platform_attempt_job_ids
    )
    media_catalog = []
    text_versions = {}
    for member in bundle_posts:
        member_team = db.get(Team, member.team_id)
        for entry in post_media_catalog(db, member):
            media_catalog.append(
                {
                    **entry,
                    "post": member,
                    "team": member_team,
                }
            )
        text_versions[member.id] = list(
            db.scalars(
                select(PostTextVersion)
                .where(
                    PostTextVersion.club_id == member.club_id,
                    PostTextVersion.post_id == member.id,
                )
                .order_by(PostTextVersion.version_number.desc())
            )
        )
    if aggregate_bundle:
        # Legacy carousel synchronization can expose the same feed path once
        # through the aggregate carousel and once through its original member
        # post.  Keep the source post's slot so a targeted regeneration uses
        # the correct game, team and player image.
        deduplicated: dict[tuple[str, str], dict] = {}
        for entry in media_catalog:
            slot = entry["slot"]
            if slot.media_kind != "feed":
                deduplicated[(slot.id, slot.id)] = entry
                continue
            selected = (
                db.get(GeneratedMediaVersion, slot.selected_version_id)
                if slot.selected_version_id
                else None
            )
            path = selected.media_path if selected else f"slot:{slot.id}"
            key = ("feed", path)
            previous = deduplicated.get(key)
            source_matches = entry["post"].feed_path == path
            previous_matches = bool(previous and previous["post"].feed_path == path)
            if previous is None or (source_matches and not previous_matches):
                deduplicated[key] = entry
        media_catalog = list(deduplicated.values())
    catalog_by_slot_id = {entry["slot"].id: entry for entry in media_catalog}
    catalog_entries_by_path: dict[str, list[dict]] = {}
    for entry in media_catalog:
        for version in entry["versions"]:
            catalog_entries_by_path.setdefault(version.media_path, []).append(entry)

    def publication_catalog_entry(
        version: GeneratedMediaVersion | None,
        *,
        media_path: str | None = None,
        preferred_post_id: str | None = None,
    ) -> tuple[dict | None, GeneratedMediaVersion | None]:
        """Resolve a frozen publication version to its tenant-visible source slot.

        Older bundled contributions can contain an aggregate carousel slot on
        the primary post as well as the original slot on the member post.  The
        bytes are identical, but editing must happen on the member post.  The
        path lookup deliberately prefers that member while retaining the exact
        publication image as the displayed version.
        """

        path = version.media_path if version else media_path
        candidates = catalog_entries_by_path.get(path or "", [])
        if preferred_post_id:
            preferred = next(
                (entry for entry in candidates if entry["post"].id == preferred_post_id),
                None,
            )
            if preferred:
                display_version = next(
                    (item for item in preferred["versions"] if item.media_path == path),
                    None,
                )
                return preferred, display_version or version
        direct = catalog_by_slot_id.get(version.slot_id) if version else None
        if direct:
            return direct, version
        if candidates:
            display_version = next(
                (item for item in candidates[0]["versions"] if item.media_path == path),
                None,
            )
            return candidates[0], display_version or version
        return None, version
    catalog_groups: dict[tuple[str, str, int], list[dict]] = {}
    for entry in media_catalog:
        slot = entry["slot"]
        catalog_groups.setdefault((slot.post_id, slot.media_kind, slot.output_position), []).append(
            entry
        )
    for entries in catalog_groups.values():
        entries.sort(key=lambda entry: entry["slot"].variant_number)
    feed_choices_by_post: dict[str, list[dict]] = {}
    for entry in media_catalog:
        slot = entry["slot"]
        if slot.media_kind == "feed":
            feed_choices_by_post.setdefault(slot.post_id, []).append(entry)
    for entries in feed_choices_by_post.values():
        entries.sort(
            key=lambda entry: (
                entry["slot"].output_position,
                entry["slot"].variant_number,
                entry["slot"].label,
            )
        )
    publication_variant_choices: dict[str, list[dict]] = {}
    for job in jobs:
        rows = []
        if job.kind == "carousel":
            for media in carousel_items.get(job.id, []):
                current_version = db.get(GeneratedMediaVersion, media.media_version_id)
                member = (
                    bundle_posts[media.position - 1]
                    if aggregate_bundle and media.position <= len(bundle_posts)
                    else None
                )
                current_entry, display_version = publication_catalog_entry(
                    current_version,
                    media_path=media.media_path,
                    preferred_post_id=member.id if member else None,
                )
                if current_entry:
                    current_slot = current_entry["slot"]
                    choices = (
                        feed_choices_by_post.get(member.id, [])
                        if member
                        else catalog_groups.get(
                            (current_slot.post_id, "feed", current_slot.output_position), []
                        )
                    )
                else:
                    choices = feed_choices_by_post.get(member.id, []) if member else []
                    current_slot = None
                rows.append(
                    {
                        "media_item": media,
                        "label": f"Karussellposition {media.position}",
                        "current_slot_id": current_slot.id if current_slot else None,
                        "choices": choices,
                        "current_entry": current_entry,
                        "current_version": display_version,
                    }
                )
        else:
            current_version = db.get(GeneratedMediaVersion, job.media_version_id)
            current_entry, display_version = publication_catalog_entry(
                current_version,
                media_path=job.media_path,
                preferred_post_id=job.post_id,
            )
            if current_entry:
                current_slot = current_entry["slot"]
                choices = (
                    feed_choices_by_post.get(current_slot.post_id, [])
                    if current_slot.media_kind == "feed"
                    else catalog_groups.get(
                        (
                            current_slot.post_id,
                            current_slot.media_kind,
                            current_slot.output_position,
                        ),
                        [],
                    )
                )
                rows.append(
                    {
                        "media_item": None,
                        "label": "Story-Ausgabe" if job.kind == "story" else "Feed-Ausgabe",
                        "current_slot_id": current_slot.id,
                        "choices": choices,
                        "current_entry": current_entry,
                        "current_version": display_version,
                    }
                )
        publication_variant_choices[job.id] = rows
    # The main media area is a publication preview, not a dump of every
    # generated candidate.  Build it from the exact media versions referenced
    # by open or historical publication jobs.  Alternatives stay available on
    # their assigned position, but no longer look like additional carousel
    # slides.
    publication_media_catalog: list[dict] = []
    displayed_publication_outputs: set[tuple[str, str]] = set()
    for job in jobs:
        rows = publication_variant_choices.get(job.id, [])
        carousel_total = len(carousel_items.get(job.id, [])) if job.kind == "carousel" else None
        for row in rows:
            current_slot_id = row.get("current_slot_id")
            current_entry = row.get("current_entry")
            media_item = row.get("media_item")
            output_key = (job.id, media_item.id if media_item is not None else job.kind)
            if not current_entry or output_key in displayed_publication_outputs:
                continue
            displayed_publication_outputs.add(output_key)
            if job.kind == "carousel" and media_item is not None:
                position = media_item.position
                publication_label = f"Karussellbild {position} von {carousel_total}"
                publication_state = "Wird in diesem Karussell veröffentlicht"
            elif job.kind == "story":
                position = current_entry["slot"].output_position
                publication_label = current_entry["slot"].label
                publication_state = "Wird als Story veröffentlicht"
            else:
                position = 1
                publication_label = "Feed-Bild"
                publication_state = "Wird als Feed-Beitrag veröffentlicht"
            publication_media_catalog.append(
                {
                    **current_entry,
                    "publication": {
                        "job": job,
                        "media_item": media_item,
                        "kind": job.kind,
                        "position": position,
                        "total": carousel_total,
                        "label": publication_label,
                        "state": publication_state,
                        "choices": row.get("choices", []),
                        "current_slot_id": current_slot_id,
                        "selected_version": row.get("current_version"),
                        "draft_selected_version": next(
                            (
                                candidate
                                for candidate in current_entry["versions"]
                                if candidate.id == current_entry["slot"].selected_version_id
                            ),
                            None,
                        ),
                    },
                }
            )
    status_labels = {
        "draft": "Entwurf",
        "detected": "Erkannt",
        "pending_approval": "Nicht freigegeben",
        "creating": "Wird erzeugt",
        "incomplete": "Unvollständig",
        "manual_review_required": "Manuelle Prüfung erforderlich",
        "approved": "Freigegeben",
        "scheduled": "Geplant",
        "partially_published": "Teilweise veröffentlicht",
        "published": "Veröffentlicht",
        "reapproval_required": "Erneute Freigabe erforderlich",
        "rejected": "Abgelehnt",
        "unapproved": "Nicht freigegeben",
        "waiting": "Wartet",
        "publishing": "Wird veröffentlicht",
        "failed": "Fehlgeschlagen",
        "cancelled": "Abgebrochen",
        "manual_schedule_required": "Manuelle Planung erforderlich",
    }
    job_version_labels = {}
    for job in jobs:
        media_version = db.get(GeneratedMediaVersion, job.media_version_id)
        text_version = db.get(PostTextVersion, job.text_version_id)
        job_version_labels[job.id] = {
            "media": media_version.version_number if media_version else None,
            "text": text_version.version_number if text_version else None,
        }
    unpublished_jobs = [job for job in jobs if job.status != JobStatus.PUBLISHED]
    next_publication = min(
        (job.scheduled_at for job in unpublished_jobs if job.scheduled_at),
        default=None,
    )
    published_count = sum(job.status == JobStatus.PUBLISHED for job in jobs)
    open_count = len(jobs) - published_count
    if incomplete_members:
        publication_summary = "Generierung unvollständig"
    elif jobs and published_count == len(jobs):
        publication_summary = "Vollständig veröffentlicht"
    elif published_count:
        publication_summary = "Teilweise veröffentlicht"
    elif any(job.approval_status == "manual_schedule_required" for job in jobs):
        publication_summary = "Manuelle Planung erforderlich"
    elif any(job.status == JobStatus.SCHEDULED for job in jobs):
        publication_summary = "Veröffentlichung geplant"
    elif any(job.status == JobStatus.UNAPPROVED for job in jobs):
        publication_summary = "Freigabe ausstehend"
    else:
        publication_summary = status_labels.get(item.status.value, item.status.value)
    relevant_team_ids = {member.team_id for member in bundle_posts}
    channel_plan = list(
        db.execute(
            select(TeamChannelAssignment, SocialChannelConnection)
            .join(
                SocialChannelConnection,
                SocialChannelConnection.id == TeamChannelAssignment.channel_connection_id,
            )
            .where(
                TeamChannelAssignment.team_id.in_(relevant_team_ids),
                TeamChannelAssignment.enabled.is_(True),
                SocialChannelConnection.active.is_(True),
                SocialChannelConnection.status == "connected",
                SocialChannelConnection.channel_type.in_({"facebook", "whatsapp"}),
            )
            .order_by(
                SocialChannelConnection.channel_type,
                SocialChannelConnection.display_name,
            )
        )
    )
    planned_connections = []
    seen_connections = set()
    for assignment, connection in channel_plan:
        enabled_for_post = (
            assignment.result_enabled
            if item.post_type == "result"
            else assignment.announcement_enabled
        )
        if not enabled_for_post or connection.id in seen_connections:
            continue
        seen_connections.add(connection.id)
        planned_connections.append(connection)
    try:
        display_text = sanitize_generated_caption(item.text or "")
    except ValueError:
        display_text = ""
    text_version_display = {}
    for versions in text_versions.values():
        for version in versions:
            try:
                text_version_display[version.id] = sanitize_generated_caption(version.text or "")
            except ValueError:
                text_version_display[version.id] = ""
    channel_previews = {}
    for connection in planned_connections:
        channel_content = db.scalar(
            select(PostChannelContent).where(
                PostChannelContent.post_id == item.id,
                PostChannelContent.channel_connection_id == connection.id,
            )
        )
        preview_text = channel_content.text if channel_content else display_text
        try:
            preview_text = sanitize_generated_caption(preview_text or "")
        except ValueError:
            preview_text = ""
        if connection.channel_type == "facebook":
            channel_previews[connection.id] = {
                "text": preview_text or "Noch kein Begleittext vorhanden.",
                "source": channel_content.source if channel_content else "derived",
                "recipient_count": None,
                "template": None,
            }
            continue
        recipient_count = sum(
            item.post_type in (recipient.preferred_message_types or [])
            for recipient in db.scalars(
                select(WhatsAppRecipient).where(
                    WhatsAppRecipient.channel_connection_id == connection.id,
                    WhatsAppRecipient.active.is_(True),
                    WhatsAppRecipient.opt_in_status == "confirmed",
                )
            )
        )
        template = db.scalar(
            select(WhatsAppMessageTemplate)
            .where(
                WhatsAppMessageTemplate.channel_connection_id == connection.id,
                WhatsAppMessageTemplate.message_type.in_({item.post_type, "general"}),
                WhatsAppMessageTemplate.status == "approved",
            )
            .order_by(WhatsAppMessageTemplate.message_type.desc())
        )
        channel_previews[connection.id] = {
            "text": preview_text or "Noch kein Nachrichtentext vorhanden.",
            "source": channel_content.source if channel_content else "derived",
            "recipient_count": recipient_count,
            "template": template,
        }
    return render(
        request,
        "post_detail.html",
        current,
        item=item,
        display_text=display_text,
        jobs=jobs,
        pages=pages,
        checks=checks,
        carousel_items=carousel_items,
        late_jobs=late_jobs,
        logo_recompose=logo_recompose_availability(item, own_jobs),
        bundle_posts=bundle_posts,
        bundle_member_teams={member.id: db.get(Team, member.team_id) for member in bundle_posts},
        aggregate_bundle=aggregate_bundle,
        bundle_error=bundle_error,
        job_posts=job_posts,
        job_teams=job_teams,
        schedule_values=schedule_values,
        schedule_timezones=schedule_timezones,
        can_reschedule_jobs=can_reschedule_jobs,
        carousel_job=carousel_job,
        can_reorder_carousel=can_reorder_carousel,
        can_save_carousel_default=current.role == Role.ADMIN,
        bundle_team_choices=[db.get(Team, member.team_id) for member in bundle_posts],
        carousel_teams={
            job.id: [db.get(Team, member.team_id) for member in bundle_posts]
            for job in jobs
            if job.kind == "carousel"
        },
        current_media_asset=current_media_asset,
        alternative_media_assets=alternative_media_assets,
        current_media_assets_by_post=current_media_assets_by_post,
        alternative_media_assets_by_post=alternative_media_assets_by_post,
        can_edit=can_edit_all,
        can_generate=not bundle_error
        and not incomplete_members
        and all(allowed(db, current, "generate", member.team_id) for member in bundle_posts),
        can_approve=not bundle_error and not incomplete_members and can_delete_all,
        incomplete_members=incomplete_members,
        resumable_generation_job=resumable_generation_job,
        can_delete=can_delete_all,
        media_catalog=publication_media_catalog,
        text_versions=text_versions,
        text_version_display=text_version_display,
        status_labels=status_labels,
        job_version_labels=job_version_labels,
        publication_variant_choices=publication_variant_choices,
        detail_team=detail_team,
        detail_game=detail_game,
        detail_page=detail_page,
        next_publication=next_publication,
        publication_summary=publication_summary,
        open_publication_count=open_count,
        published_publication_count=published_count,
        planned_channel_connections=planned_connections,
        publication_groups=publication_groups,
        publication_by_job=publication_by_job,
        channel_previews=channel_previews,
        now=now,
        title="Beitrag prüfen",
    )


@router.post("/posts/{post_id}/text")
def post_text(
    post_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    text_value: str = Form(alias="text"),
    version: int = Form(),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    item = db.get(Post, post_id)
    if not item:
        raise HTTPException(404)
    require(current, db, "edit_post", item.team_id)
    try:
        edit_text(db, item, current, text_value, version)
    except ApprovalError as e:
        raise HTTPException(409, str(e)) from e
    audit(db, current, "post.text_edited", "post", item.id, item.team_id)
    db.commit()
    return redirect(f"/posts/{item.id}")


@router.post("/posts/{post_id}/channels/{connection_id}/text")
def post_channel_text(
    post_id: str,
    connection_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    text_value: str = Form(alias="text"),
    version: int = Form(),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    item = db.scalar(select(Post).where(Post.id == post_id).with_for_update())
    connection = db.get(SocialChannelConnection, connection_id)
    if not item or not connection:
        raise HTTPException(404)
    require(current, db, "edit_post", item.team_id)
    if connection.channel_type not in {"facebook", "whatsapp"} or not connection.active:
        raise HTTPException(422, "Dieser Zielkanal kann nicht bearbeitet werden")
    if item.version != version:
        raise HTTPException(
            409,
            "Der Beitrag wurde zwischenzeitlich geändert. Bitte Seite neu laden.",
        )
    normalized = text_value.strip()
    if not normalized or len(normalized) > 5000:
        raise HTTPException(422, "Der Kanaltext muss 1 bis 5000 Zeichen lang sein")
    variant = db.scalar(
        select(PostChannelContent).where(
            PostChannelContent.post_id == item.id,
            PostChannelContent.channel_connection_id == connection.id,
        )
    )
    if variant is None:
        variant = PostChannelContent(
            post_id=item.id,
            channel_connection_id=connection.id,
            channel_type=connection.channel_type,
            text=normalized,
            source="manual",
            updated_by=current.id,
        )
        db.add(variant)
    else:
        variant.text = normalized
        variant.source = "manual"
        variant.updated_by = current.id
        variant.version += 1
    item.version += 1
    item.approved_version = None
    item.approved_by = None
    item.approved_at = None
    if item.status in {
        PostStatus.APPROVED,
        PostStatus.SCHEDULED,
        PostStatus.PARTIAL,
    }:
        item.status = PostStatus.REAPPROVAL
    for job in db.scalars(
        select(PublicationJob).where(
            PublicationJob.post_id == item.id,
            PublicationJob.status != JobStatus.PUBLISHED,
        )
    ):
        job.status = JobStatus.UNAPPROVED
        job.approval_status = "reapproval_required"
        job.approved_post_version = None
        if job.channel_connection_id == connection.id:
            job.text_snapshot = normalized
        job.error = "Kanaltext wurde geändert; erneute Freigabe erforderlich"
    audit(
        db,
        current,
        "post.channel_text_edited",
        "post",
        item.id,
        item.team_id,
        {"channel_type": connection.channel_type, "channel_connection_id": connection.id},
    )
    db.commit()
    return redirect(f"/posts/{item.id}#channel-previews", "Kanaltext gespeichert")


@router.get("/posts/{post_id}/versions/{version_id}")
def post_media_version(
    post_id: str,
    version_id: str,
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    post = db.get(Post, post_id)
    if not post:
        raise HTTPException(404)
    require(current, db, "view", post.team_id)
    version = db.scalar(
        select(GeneratedMediaVersion)
        .join(GeneratedMediaSlot, GeneratedMediaSlot.id == GeneratedMediaVersion.slot_id)
        .where(
            GeneratedMediaVersion.id == version_id,
            GeneratedMediaVersion.club_id == post.club_id,
            GeneratedMediaSlot.club_id == post.club_id,
            GeneratedMediaSlot.post_id == post.id,
        )
    )
    if not version:
        raise HTTPException(404)
    path = Path(version.media_path).resolve()
    root = settings.generated_root.resolve()
    if root not in path.parents or path.is_symlink() or not path.is_file():
        raise HTTPException(404, "Medienversion fehlt")
    return detached_file_response(db, path, media_type=version.mime_type)


@router.post("/posts/{post_id}/media-slots/{slot_id}/select")
def choose_post_media_version(
    post_id: str,
    slot_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    version_id: str = Form(),
    post_version: int = Form(),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    post = db.scalar(select(Post).where(Post.id == post_id).with_for_update())
    if not post:
        raise HTTPException(404)
    require(current, db, "edit_post", post.team_id)
    if post.version != post_version:
        raise HTTPException(409, "Beitrag wurde zwischenzeitlich geändert")
    slot = db.scalar(
        select(GeneratedMediaSlot).where(
            GeneratedMediaSlot.id == slot_id,
            GeneratedMediaSlot.club_id == post.club_id,
            GeneratedMediaSlot.post_id == post.id,
        )
    )
    previous = (
        db.get(GeneratedMediaVersion, slot.selected_version_id)
        if slot and slot.selected_version_id
        else None
    )
    try:
        selected = select_media_version(db, post, slot_id, version_id)
    except MediaVersionError as exc:
        raise HTTPException(422, str(exc)) from exc
    audit(
        db,
        current,
        "post.media_version_selected",
        "generated_media_version",
        selected.id,
        post.team_id,
        {"post_id": post.id, "slot_id": slot_id, "version": selected.version_number},
    )
    if slot is not None:
        from app.creative.hooks import record_media_selection

        record_media_selection(
            db,
            post=post,
            actor_user_id=current.id,
            slot=slot,
            selected=selected,
            previous=previous,
        )
    db.commit()
    return redirect(f"/posts/{post.id}", "Medienversion ausgewählt; erneute Freigabe erforderlich")


@router.post("/posts/{post_id}/media-slots/{slot_id}/auto-latest")
def choose_post_media_auto_latest(
    post_id: str,
    slot_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    post_version: int = Form(),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    post = db.scalar(select(Post).where(Post.id == post_id).with_for_update())
    if not post:
        raise HTTPException(404)
    require(current, db, "edit_post", post.team_id)
    if post.version != post_version:
        raise HTTPException(409, "Beitrag wurde zwischenzeitlich geändert")
    slot = db.scalar(
        select(GeneratedMediaSlot).where(
            GeneratedMediaSlot.id == slot_id,
            GeneratedMediaSlot.club_id == post.club_id,
            GeneratedMediaSlot.post_id == post.id,
        )
    )
    previous = (
        db.get(GeneratedMediaVersion, slot.selected_version_id)
        if slot and slot.selected_version_id
        else None
    )
    try:
        selected = select_latest_media_automatically(db, post, slot_id)
    except MediaVersionError as exc:
        raise HTTPException(422, str(exc)) from exc
    audit(
        db,
        current,
        "post.media_selection_auto_latest",
        "generated_media_slot",
        slot_id,
        post.team_id,
        {"post_id": post.id, "selected_version": selected.version_number},
    )
    if slot is not None and (previous is None or previous.id != selected.id):
        from app.creative.hooks import record_media_selection

        record_media_selection(
            db,
            post=post,
            actor_user_id=current.id,
            slot=slot,
            selected=selected,
            previous=previous,
        )
    db.commit()
    return redirect(
        f"/posts/{post.id}",
        "Automatische Auswahl der neuesten Medienversion aktiviert",
    )


@router.post("/posts/{post_id}/publications/{job_id}/variant")
def choose_publication_media_variant(
    post_id: str,
    job_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    slot_id: str = Form(),
    publication_media_item_id: str = Form(default=""),
    post_version: int = Form(),
    job_version: int = Form(),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    post = db.scalar(select(Post).where(Post.id == post_id).with_for_update())
    if not post:
        raise HTTPException(404)
    job = db.scalar(
        select(PublicationJob)
        .where(
            PublicationJob.id == job_id,
            PublicationJob.club_id == post.club_id,
        )
        .with_for_update()
    )
    if not job or job.post_id != post.id:
        raise HTTPException(404)
    require(current, db, "edit_post", post.team_id)
    if post.version != post_version or job.version != job_version:
        raise HTTPException(409, "Beitrag oder Veröffentlichung wurde zwischenzeitlich geändert")
    bundle = (post.design_snapshot or {}).get("club_matchday_carousel") or {}
    allowed_post_ids = set(bundle.get("member_post_ids") or [post.id])
    slot = db.scalar(
        select(GeneratedMediaSlot).where(
            GeneratedMediaSlot.id == slot_id,
            GeneratedMediaSlot.club_id == post.club_id,
            GeneratedMediaSlot.post_id.in_(allowed_post_ids),
        )
    )
    if not slot:
        raise HTTPException(422, "Ungültige Medienvariante")
    require(current, db, "edit_post", slot.team_id)
    previous_version_id = None
    if publication_media_item_id:
        previous_version_id = db.scalar(
            select(PublicationMediaItem.media_version_id).where(
                PublicationMediaItem.id == publication_media_item_id,
                PublicationMediaItem.club_id == post.club_id,
                PublicationMediaItem.publication_job_id == job.id,
            )
        )
    elif job.media_version_id:
        previous_version_id = job.media_version_id
    previous = (
        db.get(GeneratedMediaVersion, previous_version_id)
        if previous_version_id
        else None
    )
    try:
        selected = select_publication_media_variant(
            db,
            post,
            publication_job_id=job.id,
            slot_id=slot.id,
            publication_media_item_id=publication_media_item_id or None,
            allowed_post_ids=allowed_post_ids,
            allow_feed_candidates_for_same_post=bool(bundle.get("member_post_ids")),
        )
    except MediaVersionError as exc:
        raise HTTPException(422, str(exc)) from exc
    job.version += 1
    audit(
        db,
        current,
        "publication.media_variant_selected",
        "publication_job",
        job.id,
        post.team_id,
        {
            "post_id": post.id,
            "slot_id": slot.id,
            "variant_number": slot.variant_number,
            "media_version_id": selected.id,
            "publication_media_item_id": publication_media_item_id or None,
        },
    )
    feedback_post = db.get(Post, slot.post_id)
    if feedback_post is not None and (previous is None or previous.id != selected.id):
        from app.creative.hooks import record_media_selection

        record_media_selection(
            db,
            post=feedback_post,
            actor_user_id=current.id,
            slot=slot,
            selected=selected,
            previous=previous,
        )
    db.commit()
    return redirect(
        f"/posts/{post.id}",
        "Vorhandene Medienvariante ausgewählt; erneute Freigabe erforderlich",
    )


@router.post("/posts/{post_id}/text-versions/{version_id}/select")
def choose_post_text_version(
    post_id: str,
    version_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    post_version: int = Form(),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    post = db.scalar(select(Post).where(Post.id == post_id).with_for_update())
    if not post:
        raise HTTPException(404)
    require(current, db, "edit_post", post.team_id)
    if post.version != post_version:
        raise HTTPException(409, "Beitrag wurde zwischenzeitlich geändert")
    previous = (
        db.get(PostTextVersion, post.selected_text_version_id)
        if post.selected_text_version_id
        else None
    )
    try:
        selected = select_text_version(db, post, version_id)
    except MediaVersionError as exc:
        raise HTTPException(422, str(exc)) from exc
    audit(
        db,
        current,
        "post.text_version_selected",
        "post_text_version",
        selected.id,
        post.team_id,
        {"post_id": post.id, "version": selected.version_number},
    )
    if previous is None or previous.id != selected.id:
        from app.creative.hooks import record_text_selection

        record_text_selection(
            db,
            post=post,
            actor_user_id=current.id,
            selected=selected,
            previous=previous,
        )
    db.commit()
    return redirect(f"/posts/{post.id}", "Textversion ausgewählt; erneute Freigabe erforderlich")


@router.post("/posts/{post_id}/text-versions/auto-latest")
def choose_post_text_auto_latest(
    post_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    post_version: int = Form(),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    post = db.scalar(select(Post).where(Post.id == post_id).with_for_update())
    if not post:
        raise HTTPException(404)
    require(current, db, "edit_post", post.team_id)
    if post.version != post_version:
        raise HTTPException(409, "Beitrag wurde zwischenzeitlich geändert")
    previous = (
        db.get(PostTextVersion, post.selected_text_version_id)
        if post.selected_text_version_id
        else None
    )
    try:
        selected = select_latest_text_automatically(db, post)
    except MediaVersionError as exc:
        raise HTTPException(422, str(exc)) from exc
    audit(
        db,
        current,
        "post.text_selection_auto_latest",
        "post",
        post.id,
        post.team_id,
        {"selected_version": selected.version_number},
    )
    if previous is None or previous.id != selected.id:
        from app.creative.hooks import record_text_selection

        record_text_selection(
            db,
            post=post,
            actor_user_id=current.id,
            selected=selected,
            previous=previous,
        )
    db.commit()
    return redirect(
        f"/posts/{post.id}",
        "Automatische Auswahl der neuesten Textversion aktiviert",
    )


@router.post("/posts/{post_id}/rerender")
def rerender_post_media(
    post_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    version: int = Form(),
    rerender_feed: bool = Form(default=False),
    media_slot_ids: list[str] = Form(default=[]),
    story_job_ids: list[str] = Form(default=[]),
    media_asset_id: str = Form(default=""),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    from app.jobs.generation import enqueue_rerender

    check_csrf(request, csrf_token_value)
    item = db.get(Post, post_id)
    if not item:
        raise HTTPException(404)
    require(current, db, "generate", item.team_id)
    if (item.design_snapshot or {}).get("source") == "manual_upload":
        raise HTTPException(
            422,
            "Manuell hochgeladene Grafiken werden nicht durch KI neu erzeugt",
        )
    if item.version != version:
        raise HTTPException(409, "Beitrag wurde zwischenzeitlich geändert")
    bundle = (item.design_snapshot or {}).get("club_matchday_carousel") or {}
    member_ids = list(bundle.get("member_post_ids") or [item.id])
    target_posts = {
        post.id: post
        for post in db.scalars(
            select(Post).where(Post.club_id == item.club_id, Post.id.in_(member_ids))
        )
    }
    if len(target_posts) != len(set(member_ids)):
        raise HTTPException(409, "Der gemeinsame Beitrag ist unvollständig")
    for target_post in target_posts.values():
        require(current, db, "generate", target_post.team_id)
    selected_slots: list[GeneratedMediaSlot] = []
    if media_slot_ids:
        selected_slots = list(
            db.scalars(
                select(GeneratedMediaSlot).where(
                    GeneratedMediaSlot.club_id == item.club_id,
                    GeneratedMediaSlot.post_id.in_(target_posts),
                    GeneratedMediaSlot.id.in_(media_slot_ids),
                )
            )
        )
        if len(selected_slots) != len(set(media_slot_ids)):
            raise HTTPException(422, "Ungültige Medienauswahl")
        rerender_feed = any(slot.media_kind == "feed" for slot in selected_slots)
    if not rerender_feed and not story_job_ids and not selected_slots:
        raise HTTPException(422, "Bitte mindestens Feed oder eine Story auswählen")
    allowed_story_ids = set(
        db.scalars(
            select(PublicationJob.id).where(
                PublicationJob.post_id == item.id, PublicationJob.kind == "story"
            )
        )
    )
    if not set(story_job_ids).issubset(allowed_story_ids):
        raise HTTPException(422, "Ungültige Story-Auswahl")
    selected_media_asset_id = media_asset_id or item.media_asset_id
    if selected_media_asset_id and selected_media_asset_id != item.media_asset_id:
        selected_asset = db.get(MediaAsset, selected_media_asset_id)
        if not selected_asset or selected_asset.team_id != item.team_id:
            raise HTTPException(422, "Ungültiges Spielerbild")
        if (
            not selected_asset.active
            or not selected_asset.available
            or selected_asset.reserved_game_id is not None
            or selected_asset.uses != 0
        ):
            raise HTTPException(409, "Das ausgewählte Spielerbild ist nicht mehr frei")
    queued = []
    for target_post in target_posts.values():
        target_slots = [slot for slot in selected_slots if slot.post_id == target_post.id]
        has_feed_variants = bool(
            ((target_post.design_snapshot or {}).get("media") or {}).get("feed_variants")
        )
        target_feed_positions = sorted(
            {
                slot.variant_number if has_feed_variants else slot.output_position
                for slot in target_slots
                if slot.media_kind == "feed"
            }
        )
        story_slot_ids = [slot.id for slot in target_slots if slot.media_kind == "story"]
        target_story_variants = sorted(
            {slot.variant_number for slot in target_slots if slot.media_kind == "story"}
        )
        story_version_ids = list(
            db.scalars(
                select(GeneratedMediaVersion.id).where(
                    GeneratedMediaVersion.club_id == item.club_id,
                    GeneratedMediaVersion.slot_id.in_(story_slot_ids),
                )
            )
        )
        target_story_jobs = list(
            db.scalars(
                select(PublicationJob.id).where(
                    PublicationJob.club_id == item.club_id,
                    PublicationJob.post_id == target_post.id,
                    PublicationJob.kind == "story",
                    PublicationJob.media_version_id.in_(story_version_ids),
                )
            )
        )
        if not media_slot_ids and target_post.id == item.id:
            target_story_jobs = story_job_ids
            target_story_variants = []
            target_feed_positions = []
        target_rerender_feed = bool(target_feed_positions) or (
            not media_slot_ids and target_post.id == item.id and rerender_feed
        )
        if not target_rerender_feed and not target_story_jobs and not target_story_variants:
            continue
        queued.append(
            enqueue_rerender(
                db,
                target_post,
                current,
                target_post.version,
                target_story_jobs,
                selected_media_asset_id
                if target_post.id == item.id
                else target_post.media_asset_id,
                rerender_feed=target_rerender_feed,
                feed_positions=target_feed_positions or None,
                story_variant_numbers=target_story_variants or None,
            )
        )
    if not queued:
        raise HTTPException(422, "Bitte mindestens eine Medienausgabe auswählen")
    return redirect(
        f"/generation-jobs/{queued[0].id}",
        f"{len(queued)} Neurender-Auftrag/Aufträge wurden eingereiht",
    )


@router.post("/posts/{post_id}/ai-revision")
def revise_post_with_ai(
    post_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    version: int = Form(),
    instruction: str = Form(),
    revise_text: bool = Form(default=False),
    revise_feed: bool = Form(default=False),
    revise_graphics: bool = Form(default=False),
    media_slot_ids: list[str] = Form(default=[]),
    story_job_ids: list[str] = Form(default=[]),
    media_asset_id: str = Form(default=""),
    media_asset_choices: list[str] = Form(default=[]),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    from app.jobs.generation import enqueue_ai_revision

    check_csrf(request, csrf_token_value)
    item = db.get(Post, post_id)
    if not item:
        raise HTTPException(404)
    require(current, db, "generate", item.team_id)
    if (item.design_snapshot or {}).get("source") == "manual_upload":
        raise HTTPException(
            422,
            "Manuell hochgeladene Beiträge können nicht durch KI geändert werden",
        )
    if item.version != version:
        raise HTTPException(409, "Beitrag wurde zwischenzeitlich geändert")
    if not 10 <= len(instruction.strip()) <= 2000:
        raise HTTPException(422, "Die KI-Änderungsanweisung muss 10 bis 2000 Zeichen lang sein")
    bundle = (item.design_snapshot or {}).get("club_matchday_carousel") or {}
    member_ids = list(bundle.get("member_post_ids") or [item.id])
    target_posts = {
        post.id: post
        for post in db.scalars(
            select(Post).where(Post.club_id == item.club_id, Post.id.in_(member_ids))
        )
    }
    if len(target_posts) != len(set(member_ids)):
        raise HTTPException(409, "Der gemeinsame Beitrag ist unvollständig")
    for target_post in target_posts.values():
        require(current, db, "generate", target_post.team_id)
    selected_assets_by_post: dict[str, str] = {}
    for raw_choice in media_asset_choices:
        choice_post_id, separator, choice_asset_id = raw_choice.partition(":")
        if not separator or choice_post_id not in target_posts or not choice_asset_id:
            raise HTTPException(422, "Ungültige Spielerbild-Zuordnung")
        if choice_post_id in selected_assets_by_post:
            raise HTTPException(422, "Spielerbild wurde für eine Mannschaft mehrfach angegeben")
        selected_assets_by_post[choice_post_id] = choice_asset_id
    selected_slots: list[GeneratedMediaSlot] = []
    if media_slot_ids:
        selected_slots = list(
            db.scalars(
                select(GeneratedMediaSlot).where(
                    GeneratedMediaSlot.club_id == item.club_id,
                    GeneratedMediaSlot.post_id.in_(target_posts),
                    GeneratedMediaSlot.id.in_(media_slot_ids),
                )
            )
        )
        if len(selected_slots) != len(set(media_slot_ids)):
            raise HTTPException(422, "Ungültige Medienauswahl")
        revise_feed = any(slot.media_kind == "feed" for slot in selected_slots)
    # Keep accepting ``revise_graphics`` for already open legacy forms. New
    # forms select the feed and each story explicitly.
    revise_feed = revise_feed or revise_graphics
    has_graphics = revise_feed or bool(story_job_ids) or bool(selected_slots)
    if not revise_text and not has_graphics:
        raise HTTPException(422, "Bitte Begleittext, Feed oder mindestens eine Story auswählen")
    allowed_story_ids = set(
        db.scalars(
            select(PublicationJob.id).where(
                PublicationJob.post_id == item.id,
                PublicationJob.kind == "story",
                PublicationJob.status != JobStatus.PUBLISHED,
            )
        )
    )
    if story_job_ids and not set(story_job_ids).issubset(allowed_story_ids):
        raise HTTPException(422, "Ungültige oder bereits veröffentlichte Story-Auswahl")
    selected_media_asset_id = media_asset_id or item.media_asset_id
    queued = []
    try:
        for target_post in target_posts.values():
            target_slots = [slot for slot in selected_slots if slot.post_id == target_post.id]
            has_feed_variants = bool(
                ((target_post.design_snapshot or {}).get("media") or {}).get("feed_variants")
            )
            target_feed_positions = sorted(
                {
                    slot.variant_number if has_feed_variants else slot.output_position
                    for slot in target_slots
                    if slot.media_kind == "feed"
                }
            )
            story_slot_ids = [slot.id for slot in target_slots if slot.media_kind == "story"]
            target_story_variants = sorted(
                {slot.variant_number for slot in target_slots if slot.media_kind == "story"}
            )
            story_version_ids = list(
                db.scalars(
                    select(GeneratedMediaVersion.id).where(
                        GeneratedMediaVersion.club_id == item.club_id,
                        GeneratedMediaVersion.slot_id.in_(story_slot_ids),
                    )
                )
            )
            target_story_jobs = list(
                db.scalars(
                    select(PublicationJob.id).where(
                        PublicationJob.club_id == item.club_id,
                        PublicationJob.post_id == target_post.id,
                        PublicationJob.kind == "story",
                        PublicationJob.media_version_id.in_(story_version_ids),
                    )
                )
            )
            if not media_slot_ids and target_post.id == item.id:
                target_story_jobs = story_job_ids
                target_story_variants = []
                target_feed_positions = []
            target_revise_feed = bool(target_feed_positions) or (
                not media_slot_ids and target_post.id == item.id and revise_feed
            )
            target_revise_text = revise_text and target_post.id == item.id
            if (
                not target_revise_text
                and not target_revise_feed
                and not target_story_jobs
                and not target_story_variants
            ):
                continue
            target_media_asset_id = selected_assets_by_post.get(
                target_post.id,
                selected_media_asset_id
                if target_post.id == item.id
                else target_post.media_asset_id,
            )
            if (
                target_revise_feed or target_story_jobs or target_story_variants
            ) and target_media_asset_id != target_post.media_asset_id:
                selected_asset = db.get(MediaAsset, target_media_asset_id)
                if (
                    not selected_asset
                    or selected_asset.club_id != target_post.club_id
                    or selected_asset.team_id != target_post.team_id
                ):
                    raise HTTPException(422, "Ungültiges Spielerbild")
                if (
                    not selected_asset.active
                    or not selected_asset.available
                    or selected_asset.reserved_game_id is not None
                    or selected_asset.uses != 0
                ):
                    raise HTTPException(409, "Das ausgewählte Spielerbild ist nicht mehr frei")
            queued.append(
                enqueue_ai_revision(
                    db,
                    target_post,
                    current,
                    target_post.version,
                    instruction,
                    revise_text=target_revise_text,
                    revise_graphics=bool(
                        target_revise_feed or target_story_jobs or target_story_variants
                    ),
                    revise_feed=target_revise_feed,
                    story_job_ids=target_story_jobs,
                    media_asset_id=target_media_asset_id,
                    feed_positions=target_feed_positions or None,
                    story_variant_numbers=target_story_variants or None,
                )
            )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not queued:
        raise HTTPException(422, "Bitte Begleittext oder mindestens eine Medienausgabe auswählen")
    return redirect(
        f"/generation-jobs/{queued[0].id}",
        f"{len(queued)} KI-Änderungsauftrag/Aufträge wurden eingereiht",
    )


@router.post("/posts/{post_id}/media-slots/{slot_id}/ai-edit")
def edit_single_post_media_with_ai(
    post_id: str,
    slot_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    post_version: int = Form(),
    mode: str = Form(),
    instruction: str = Form(default=""),
    source_version_id: str = Form(default=""),
    media_asset_id: str = Form(default=""),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    """Queue one explicit media edit without widening it to bundle members."""

    from app.jobs.generation import enqueue_ai_revision

    check_csrf(request, csrf_token_value)
    post = db.scalar(
        select(Post).where(Post.id == post_id).with_for_update()
    )
    if not post:
        raise HTTPException(404)
    require(current, db, "generate", post.team_id)
    if (post.design_snapshot or {}).get("source") == "manual_upload":
        raise HTTPException(
            422,
            "Manuell hochgeladene Beiträge können nicht durch KI geändert werden",
        )
    if post.version != post_version:
        raise HTTPException(409, "Beitrag wurde zwischenzeitlich geändert")
    if mode not in {"targeted_edit", "full_regenerate"}:
        raise HTTPException(422, "Unbekannte Art der Bildbearbeitung")

    slot = db.scalar(
        select(GeneratedMediaSlot).where(
            GeneratedMediaSlot.id == slot_id,
            GeneratedMediaSlot.club_id == post.club_id,
            GeneratedMediaSlot.post_id == post.id,
        )
    )
    if not slot:
        raise HTTPException(404, "Medienausgabe wurde nicht gefunden")
    selected_version = db.scalar(
        select(GeneratedMediaVersion).where(
            GeneratedMediaVersion.id == slot.selected_version_id,
            GeneratedMediaVersion.club_id == post.club_id,
            GeneratedMediaVersion.slot_id == slot.id,
        )
    )
    if not selected_version:
        raise HTTPException(409, "Für diese Ausgabe ist keine verwendbare Version ausgewählt")
    if mode == "targeted_edit":
        if source_version_id != selected_version.id:
            raise HTTPException(
                409,
                "Die Ausgangsversion wurde zwischenzeitlich geändert. Bitte laden Sie die Seite neu.",
            )
        if not 10 <= len(instruction.strip()) <= 2000:
            raise HTTPException(
                422,
                "Die Änderungsanweisung muss 10 bis 2000 Zeichen lang sein",
            )
        selected_media_asset_id = post.media_asset_id
    else:
        instruction = ""
        selected_media_asset_id = media_asset_id or post.media_asset_id
        if selected_media_asset_id != post.media_asset_id:
            selected_asset = db.scalar(
                select(MediaAsset).where(
                    MediaAsset.id == selected_media_asset_id,
                    MediaAsset.club_id == post.club_id,
                    MediaAsset.team_id == post.team_id,
                    MediaAsset.deleted_at.is_(None),
                )
            )
            if not selected_asset:
                raise HTTPException(422, "Das Spielerbild gehört nicht zu dieser Mannschaft")
            if (
                not selected_asset.active
                or not selected_asset.available
                or selected_asset.reserved_game_id is not None
                or selected_asset.uses != 0
            ):
                raise HTTPException(409, "Das ausgewählte Spielerbild ist nicht mehr frei")

    has_feed_variants = bool(
        ((post.design_snapshot or {}).get("media") or {}).get("feed_variants")
    )
    feed_positions = None
    story_variants = None
    revise_feed = slot.media_kind == "feed"
    if revise_feed:
        feed_positions = [
            slot.variant_number if has_feed_variants else slot.output_position
        ]
    else:
        story_variants = [slot.variant_number]

    try:
        job = enqueue_ai_revision(
            db,
            post,
            current,
            post.version,
            instruction,
            revise_text=False,
            revise_graphics=True,
            revise_feed=revise_feed,
            story_job_ids=[],
            media_asset_id=selected_media_asset_id,
            feed_positions=feed_positions,
            story_variant_numbers=story_variants,
            revision_mode=mode,
            source_media_version_id=(
                selected_version.id if mode == "targeted_edit" else None
            ),
            target_media_slot_id=slot.id,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    action = "Gezielte Bildänderung" if mode == "targeted_edit" else "Neugenerierung"
    return redirect(
        f"/generation-jobs/{job.id}",
        f"{action} für {slot.label} wurde eingereiht",
    )


@router.post("/posts/{post_id}/recompose-logos")
def recompose_post_media_logos(
    post_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    version: int = Form(),
    story_job_ids: list[str] = Form(default=[]),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    from app.jobs.generation import enqueue_logo_recompose

    check_csrf(request, csrf_token_value)
    item = db.get(Post, post_id)
    if not item:
        raise HTTPException(404)
    require(current, db, "generate", item.team_id)
    if (item.design_snapshot or {}).get("source") == "manual_upload":
        raise HTTPException(
            422,
            "Manuell hochgeladene Grafiken besitzen keine Logo-Komposition",
        )
    if item.version != version:
        raise HTTPException(409, "Beitrag wurde zwischenzeitlich geändert")
    allowed_story_ids = set(
        db.scalars(
            select(PublicationJob.id).where(
                PublicationJob.post_id == item.id,
                PublicationJob.kind == "story",
                PublicationJob.status != JobStatus.PUBLISHED,
            )
        )
    )
    if not set(story_job_ids).issubset(allowed_story_ids):
        raise HTTPException(422, "Ungültige oder bereits veröffentlichte Story-Auswahl")
    try:
        job = enqueue_logo_recompose(db, item, current, version, story_job_ids)
    except LogoValidationError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return redirect(
        f"/generation-jobs/{job.id}",
        "Logo-Neuzusammensetzung wurde ohne neuen KI-Aufruf eingereiht",
    )


@router.post("/posts/{post_id}/carousel/order")
def change_carousel_order(
    post_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    first_team_id: str = Form(),
    job_version: int = Form(),
    save_as_default: bool = Form(default=False),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    item = db.get(Post, post_id)
    if not item:
        raise HTTPException(404)
    require(current, db, "view", item.team_id)
    try:
        primary, members, _jobs, _job_posts = matchday_bundle_jobs(db, item)
    except ClubCarouselConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if primary.id != item.id or len(members) < 2:
        raise HTTPException(409, "Dieser Beitrag ist kein gemeinsames Vereinskarussell")
    for member in members:
        require(current, db, "edit_post", member.team_id)
    if save_as_default:
        require_admin(current)
    order_changed = members[0].team_id != first_team_id
    try:
        reorder_matchday_carousel(
            db,
            item,
            first_team_id=first_team_id,
            expected_job_version=job_version,
            requested_by=current.id,
            save_as_default=save_as_default,
        )
        db.commit()
    except ClubCarouselConflict as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    message = "Karussell-Reihenfolge gespeichert"
    if save_as_default:
        message += "; Mannschaft auch für künftige Karussells priorisiert"
    if order_changed:
        message += "; erneute Freigabe erforderlich"
    return redirect(f"/posts/{item.id}", message)


@router.post("/posts/{post_id}/approve")
def approve_post(
    post_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    job_ids: list[str] = Form(default=[]),
    channel_connection_ids: list[str] = Form(default=[]),
    channel_selection_submitted: bool = Form(default=False),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    item = db.get(Post, post_id)
    if not item:
        raise HTTPException(404)
    try:
        approve_matchday_bundle(
            db,
            item,
            current,
            job_ids or None,
            channel_connection_ids if channel_selection_submitted else None,
        )
    except ApprovalError as e:
        raise HTTPException(422, str(e)) from e
    return redirect(f"/posts/{item.id}", "Beitrag ausdrücklich freigegeben")


@router.post("/posts/{post_id}/publications/{job_id}/schedule")
def change_publication_schedule(
    post_id: str,
    job_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    scheduled_at_value: str = Form(alias="scheduled_at"),
    job_version: int = Form(),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    display_post = db.get(Post, post_id)
    job = db.get(PublicationJob, job_id)
    if not display_post or not job:
        raise HTTPException(404)
    require(current, db, "view", display_post.team_id)

    source_post = db.get(Post, job.post_id)
    if not source_post:
        raise HTTPException(404)
    if source_post.id != display_post.id:
        try:
            primary, members, visible_jobs, job_posts = matchday_bundle_jobs(db, display_post)
        except ClubCarouselConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        visible_ids = {visible.id for visible in visible_jobs}
        if (
            primary.id != display_post.id
            or job.id not in visible_ids
            or job_posts[job.id].id != source_post.id
        ):
            raise HTTPException(404)
        for member in members:
            require(current, db, "view", member.team_id)

    require(current, db, "edit_post", job.team_id)
    if job.kind == "carousel" and display_post.id != source_post.id:
        raise HTTPException(409, "Karussellauftrag ist keinem gültigen Hauptbeitrag zugeordnet")
    if job.kind == "carousel" and (display_post.design_snapshot or {}).get(
        "club_matchday_carousel"
    ):
        try:
            _primary, members, _visible_jobs, _job_posts = matchday_bundle_jobs(db, display_post)
        except ClubCarouselConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        for member in members:
            require(current, db, "edit_post", member.team_id)

    locked_post = db.scalar(select(Post).where(Post.id == source_post.id).with_for_update())
    locked_job = db.scalar(
        select(PublicationJob)
        .where(PublicationJob.id == job.id, PublicationJob.post_id == source_post.id)
        .with_for_update()
    )
    if not locked_post or not locked_job:
        raise HTTPException(404)
    team = db.get(Team, locked_job.team_id)
    if not team:
        raise HTTPException(404)
    try:
        scheduled_at = parse_manual_publication_time(
            scheduled_at_value, team.timezone or settings.timezone
        )
        change = reschedule_publication_job(
            db,
            post=locked_post,
            job=locked_job,
            user=current,
            scheduled_at=scheduled_at,
            expected_job_version=job_version,
        )
    except ManualPostError as exc:
        raise HTTPException(422, str(exc)) from exc
    except PublicationScheduleError as exc:
        raise HTTPException(409, str(exc)) from exc
    message = "Veröffentlichungszeitpunkt geändert"
    if change.approval_invalidated:
        message += "; erneute Freigabe erforderlich"
    return redirect(f"/posts/{display_post.id}", message)


@router.post("/posts/{post_id}/reject")
def reject_post(
    post_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    reason: str = Form(default=""),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    from app.models import JobStatus, PostStatus
    from app.usage.service import mark_post_rejected

    check_csrf(request, csrf_token_value)
    item = db.get(Post, post_id)
    if not item:
        raise HTTPException(404)
    require(current, db, "approve", item.team_id)
    reason = reason.strip()
    item.status = PostStatus.REJECTED
    for job in db.scalars(
        select(PublicationJob).where(
            PublicationJob.post_id == item.id, PublicationJob.status != JobStatus.PUBLISHED
        )
    ):
        job.status = JobStatus.UNAPPROVED
        job.approval_status = "rejected"
    audit(
        db,
        current,
        "post.rejected",
        "post",
        item.id,
        item.team_id,
        {"reason": reason or None},
    )
    mark_post_rejected(db, item.id)
    from app.creative.hooks import record_post_decision

    record_post_decision(
        db,
        post=item,
        actor_user_id=current.id,
        action="rejected",
        free_text=reason or None,
    )
    db.commit()
    return redirect(f"/posts/{item.id}", "Beitrag abgelehnt")


@router.post("/posts/{post_id}/delete")
def delete_post(
    post_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    version: int = Form(),
    confirmation: str = Form(),
    reason: str = Form(default=""),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    from app.posts.deletion import PostDeletionConflict, delete_unpublished_post

    check_csrf(request, csrf_token_value)
    item = db.get(Post, post_id)
    if not item:
        raise HTTPException(404)
    require(current, db, "approve", item.team_id)
    if confirmation.strip() != "BEITRAG LÖSCHEN":
        raise HTTPException(422, "Bitte zur Bestätigung exakt BEITRAG LÖSCHEN eingeben")
    try:
        result = delete_unpublished_post(
            db,
            settings,
            item,
            current,
            expected_version=version,
            reason=reason,
        )
    except PostDeletionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return redirect(
        "/posts",
        (
            f"{result.posts} verbundene Beiträge gelöscht; "
            f"{result.publication_jobs} unveröffentlichte Aufträge entfernt"
            if result.posts > 1
            else f"Beitrag gelöscht; {result.publication_jobs} unveröffentlichte Aufträge entfernt"
        ),
    )


@router.get("/publications", response_class=HTMLResponse)
def publications(
    request: Request,
    page: int = Query(default=1, ge=1),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    if not current.club_id:
        raise HTTPException(403, "Eindeutiger Vereinskontext fehlt")
    visible_team_ids = [
        team_id
        for team_id in db.scalars(
            select(Team.id).where(
                Team.club_id == current.club_id,
                Team.archived_at.is_(None),
            )
        )
        if require_visible(db, current, team_id)
    ]
    page_size = 100
    items = []
    has_next = False
    if visible_team_ids:
        rows = list(
            db.scalars(
                select(PublicationJob)
                .where(
                    PublicationJob.club_id == current.club_id,
                    PublicationJob.team_id.in_(visible_team_ids),
                )
                .order_by(PublicationJob.scheduled_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size + 1)
            )
        )
        has_next = len(rows) > page_size
        items = rows[:page_size]
    channels = operational_channels(db, current.club_id)
    presented = publication_views(
        db,
        items,
        club_id=current.club_id,
        channels=channels,
    )
    return render(
        request,
        "publications.html",
        current,
        items=items,
        presentation_by_job={row.job.id: row for row in presented},
        job_status_labels=JOB_STATUS_LABELS,
        approval_labels=APPROVAL_LABELS,
        page=page,
        has_next=has_next,
        title="Technische Veröffentlichungshistorie",
    )


@router.post("/publications/{job_id}/cancel")
def cancel_job(
    job_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    from app.models import JobStatus

    check_csrf(request, csrf_token_value)
    item = db.get(PublicationJob, job_id)
    if not item:
        raise HTTPException(404)
    require(current, db, "approve", item.team_id)
    if item.status == JobStatus.PUBLISHED:
        raise HTTPException(409, "Veröffentlichte Aufträge können nicht abgebrochen werden")
    item.status = JobStatus.CANCELLED
    audit(db, current, "publication.cancelled", "publication_job", item.id, item.team_id)
    db.commit()
    return redirect("/publications")


@router.get("/logos/{logo_id}/preview")
def logo_preview(
    logo_id: str,
    game_id: str | None = None,
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    logo = db.get(LogoAsset, logo_id)
    if not logo or logo.archived_at:
        raise HTTPException(404)
    if logo.logo_type == "team" and logo.team_id:
        require(current, db, "view", logo.team_id)
    elif current.role != Role.ADMIN:
        context_game = db.get(Game, game_id) if game_id else None
        if context_game:
            require(current, db, "view", context_game.team_id)
        else:
            assigned_team_ids = db.scalars(
                select(Game.team_id).where(Game.opponent_logo_id == logo.id)
            ).all()
            if not any(require_visible(db, current, team_id) for team_id in assigned_team_ids):
                raise HTTPException(403, "Keine Berechtigung für dieses Gegnerlogo")
    relative = Path(logo.original_path)
    root = settings.upload_root.resolve()
    path = (root / relative).resolve()
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or not path.is_relative_to(root)
        or path.is_symlink()
        or not path.is_file()
    ):
        raise HTTPException(404)
    return detached_file_response(
        db,
        path,
        media_type=logo.mime_type,
        filename=f"gegnerlogo-{logo.id[:8]}{Path(logo.original_path).suffix.lower()}",
        content_disposition_type="inline",
    )


@router.get("/shared-opponent-logos/{logo_id}/preview")
def shared_opponent_logo_preview(
    logo_id: str,
    game_id: str,
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    game = db.get(Game, game_id)
    if not game:
        raise HTTPException(404)
    require(current, db, "view", game.team_id)
    logo = db.get(SharedOpponentLogo, logo_id)
    if not logo or not logo.active or logo.archived_at:
        raise HTTPException(404)
    try:
        path = shared_logo_path(logo, settings.upload_root)
    except LogoValidationError as exc:
        raise HTTPException(404, str(exc)) from exc
    return detached_file_response(
        db,
        path,
        media_type=logo.mime_type,
        filename=f"gegnerlogo-{logo.id[:8]}{Path(logo.original_path).suffix.lower()}",
        content_disposition_type="inline",
    )


@router.get("/games/{game_id}/opponent-logo", response_class=HTMLResponse)
def manage_opponent_logo(
    game_id: str,
    request: Request,
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    game = db.get(Game, game_id)
    if not game:
        raise HTTPException(404)
    require(current, db, "view", game.team_id)
    team = db.get(Team, game.team_id)
    name = opponent_name(game, team)
    normalized = normalize_club_name(name)
    current_logo = db.get(LogoAsset, game.opponent_logo_id) if game.opponent_logo_id else None
    suggestions = db.scalars(
        select(LogoAsset)
        .where(
            LogoAsset.logo_type == "opponent",
            LogoAsset.normalized_name == normalized,
            LogoAsset.active.is_(True),
            LogoAsset.archived_at.is_(None),
        )
        .order_by(LogoAsset.version.desc())
    ).all()
    library = db.scalars(
        select(LogoAsset)
        .where(
            LogoAsset.logo_type == "opponent",
            LogoAsset.active.is_(True),
            LogoAsset.archived_at.is_(None),
        )
        .order_by(LogoAsset.display_name, LogoAsset.version.desc())
    ).all()
    shared_suggestions = db.scalars(
        select(SharedOpponentLogo)
        .where(
            SharedOpponentLogo.normalized_name == normalized,
            SharedOpponentLogo.active.is_(True),
            SharedOpponentLogo.archived_at.is_(None),
        )
        .order_by(SharedOpponentLogo.catalog_version.desc())
    ).all()
    shared_library = db.scalars(
        select(SharedOpponentLogo)
        .where(
            SharedOpponentLogo.active.is_(True),
            SharedOpponentLogo.archived_at.is_(None),
        )
        .order_by(
            SharedOpponentLogo.display_name,
            SharedOpponentLogo.catalog_version.desc(),
        )
    ).all()
    uploader_ids = {logo.uploaded_by for logo in library}
    uploaders = (
        {
            user.id: user.email
            for user in db.scalars(select(User).where(User.id.in_(uploader_ids))).all()
        }
        if uploader_ids
        else {}
    )
    return render(
        request,
        "opponent_logo.html",
        current,
        game=game,
        team=team,
        opponent=name,
        current_logo=current_logo,
        opponent_logo_enabled=bool(
            current_logo and (game.overrides or {}).get("use_opponent_logo", True)
        ),
        suggestions=suggestions,
        library=library,
        shared_suggestions=shared_suggestions,
        shared_library=shared_library,
        uploaders=uploaders,
        title="Gegnerlogo verwalten",
    )


@router.post("/games/{game_id}/opponent-logo")
async def update_opponent_logo(
    game_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    action: str = Form(),
    logo_id: str = Form(default=""),
    shared_logo_id: str = Form(default=""),
    file: UploadFile | None = File(default=None),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    game = db.scalar(select(Game).where(Game.id == game_id).with_for_update())
    if not game:
        raise HTTPException(404)
    require(current, db, "edit_game", game.team_id)
    team = db.get(Team, game.team_id)
    old = db.get(LogoAsset, game.opponent_logo_id) if game.opponent_logo_id else None
    old_enabled = bool(old and (game.overrides or {}).get("use_opponent_logo", True))
    selected = None
    created = False
    if action == "upload":
        if not file or not file.filename:
            raise HTTPException(422, "Bitte eine PNG- oder WebP-Datei auswählen")
        try:
            upload_data = await file.read()
            selected, created = store_logo(
                db,
                upload_root=settings.upload_root,
                logo_type="opponent",
                team_id=None,
                display_name=opponent_name(game, team),
                original_filename=file.filename,
                content_type=file.content_type,
                data=upload_data,
                uploaded_by=current.id,
                club_id=team.club_id,
            )
            publish_shared_opponent_logo(
                db,
                upload_root=settings.upload_root,
                source=selected,
                data=upload_data,
            )
        except LogoValidationError as exc:
            raise HTTPException(422, str(exc)) from exc
    elif action == "select":
        selected = db.get(LogoAsset, logo_id)
        if (
            not selected
            or selected.logo_type != "opponent"
            or not selected.active
            or selected.archived_at
        ):
            raise HTTPException(422, "Das gewählte Gegnerlogo ist nicht aktiv verfügbar")
    elif action == "select_shared":
        shared = db.get(SharedOpponentLogo, shared_logo_id)
        if not shared:
            raise HTTPException(422, "Das gewählte systemweite Gegnerlogo fehlt")
        try:
            selected, created = import_shared_opponent_logo(
                db,
                upload_root=settings.upload_root,
                shared=shared,
                display_name=opponent_name(game, team),
                uploaded_by=current.id,
                club_id=team.club_id,
            )
        except LogoValidationError as exc:
            raise HTTPException(422, str(exc)) from exc
    elif action in {"enable_usage", "disable_usage"}:
        if not old:
            raise HTTPException(422, "Es ist kein Gegnerlogo zugeordnet")
        selected = old
    elif action != "remove":
        raise HTTPException(422, "Unbekannte Logoaktion")
    if action in {"enable_usage", "disable_usage"}:
        source = str((game.overrides or {}).get("opponent_logo_source") or "manuell")
    elif action == "select_shared" and selected:
        source = "shared_catalog_confirmed"
    elif selected and selected.normalized_name != normalize_club_name(opponent_name(game, team)):
        # Abweichende Schreibweisen sind erlaubt, aber nur nach dieser bewussten Auswahl.
        source = "manual_confirmed_non_exact"
    elif selected:
        source = "exact_name_confirmed"
    else:
        source = "removed"
    opponent_logo_enabled = bool(selected and action not in {"disable_usage", "remove"})
    game.opponent_logo_id = selected.id if selected else None
    game.overrides = {
        **(game.overrides or {}),
        "opponent_logo_source": source,
        "use_opponent_logo": opponent_logo_enabled,
        "opponent_logo_confirmed_by": current.id if selected else None,
        "opponent_logo_confirmed_at": datetime.now(timezone.utc).isoformat(),
    }
    game.version += 1
    affected = []
    selection_changed = (old.id if old else None) != (selected.id if selected else None)
    usage_changed = old_enabled != opponent_logo_enabled
    refreshed_jobs = refresh_pending_generation_logo_snapshots(db, game, team)
    if selection_changed or usage_changed:
        reason = "Gegnerlogo wurde geändert; erneute Freigabe erforderlich"
        affected = _invalidate_posts_for_logo_change(db, game, reason)
        _audit_logo_approval_revocations(db, current, game.team_id, game.id, affected, reason)
    action_name = (
        "opponent_logo.removed"
        if action == "remove"
        else "opponent_logo.usage_enabled"
        if action == "enable_usage"
        else "opponent_logo.usage_disabled"
        if action == "disable_usage"
        else (
            "opponent_logo.shared_catalog_assigned"
            if action == "select_shared"
            else "opponent_logo.uploaded"
            if created
            else (
                "opponent_logo.suggestion_confirmed"
                if source == "exact_name_confirmed"
                else "opponent_logo.assigned"
            )
        )
    )
    audit(
        db,
        current,
        action_name,
        "game",
        game.id,
        game.team_id,
        {
            "old_logo": {"id": old.id, "version": old.version} if old else None,
            "new_logo": ({"id": selected.id, "version": selected.version} if selected else None),
            "source": source,
            "use_opponent_logo": opponent_logo_enabled,
            "refreshed_generation_jobs": refreshed_jobs,
            "affected_posts": affected,
        },
    )
    db.commit()
    return redirect(
        f"/games/{game.id}/opponent-logo",
        "Gegnerlogo-Zuordnung gespeichert",
    )


@router.get("/games", response_class=HTMLResponse)
def games(
    request: Request,
    team_id: str = Query(default=""),
    period: str = Query(default="upcoming"),
    contribution_status: str = Query(default="all"),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    if not current.club_id:
        raise HTTPException(403, "Eindeutiger Vereinskontext fehlt")
    teams = [
        t
        for t in db.scalars(
            select(Team).where(
                Team.club_id == current.club_id,
                Team.archived_at.is_(None),
            ).order_by(Team.display_name.asc(), Team.id.asc())
        )
        if require_visible(db, current, t.id)
    ]
    team_map = {team.id: team for team in teams}
    if team_id and team_id not in team_map:
        raise HTTPException(403, "Diese Mannschaft ist nicht verfügbar")
    allowed_periods = {"upcoming", "today", "next_7", "next_30", "past", "all"}
    allowed_contribution_statuses = {
        "all",
        "missing",
        "attention",
        "planned",
        "published",
        "problem",
    }
    if period not in allowed_periods:
        raise HTTPException(422, "Unbekannter Zeitraumfilter")
    if contribution_status not in allowed_contribution_statuses:
        raise HTTPException(422, "Unbekannter Beitragsstatus")
    visible_games = [
        g
        for g in db.scalars(
            select(Game)
            .where(Game.club_id == current.club_id)
            .order_by(Game.kickoff.asc(), Game.team_id.asc(), Game.id.asc())
        )
        if require_visible(db, current, g.team_id)
    ]
    items = [
        game
        for game in visible_games
        if not bool((game.overrides or {}).get("dashboard_deleted"))
        and not bool((game.overrides or {}).get("import_suppressed"))
    ]
    suppressed_items = [
        game
        for game in visible_games
        if bool((game.overrides or {}).get("dashboard_deleted"))
        or bool((game.overrides or {}).get("import_suppressed"))
    ]
    local_zone = ZoneInfo(settings.timezone)
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(local_zone)
    today = now_local.date()

    def local_kickoff(game: Game) -> datetime:
        kickoff = game.kickoff
        if kickoff.tzinfo is None:
            kickoff = kickoff.replace(tzinfo=timezone.utc)
        return kickoff.astimezone(local_zone)

    if team_id:
        items = [game for game in items if game.team_id == team_id]
        suppressed_items = [game for game in suppressed_items if game.team_id == team_id]

    def in_selected_period(game: Game) -> bool:
        game_date = local_kickoff(game).date()
        if period == "upcoming":
            return game_date >= today
        if period == "today":
            return game_date == today
        if period == "next_7":
            return today <= game_date <= today + timedelta(days=6)
        if period == "next_30":
            return today <= game_date <= today + timedelta(days=29)
        if period == "past":
            return game_date < today
        return True

    items = [game for game in items if in_selected_period(game)]
    team_order = {team.id: index for index, team in enumerate(teams)}
    items.sort(
        key=lambda game: (
            local_kickoff(game),
            team_order.get(game.team_id, len(team_order)),
            game.id,
        )
    )
    logo_ids = {game.opponent_logo_id for game in items if game.opponent_logo_id}
    logos_by_id = (
        {
            logo.id: logo
            for logo in db.scalars(
                select(LogoAsset).where(
                    LogoAsset.club_id == current.club_id,
                    LogoAsset.id.in_(logo_ids),
                )
            )
        }
        if logo_ids
        else {}
    )
    logo_map = {
        game.id: logos_by_id.get(game.opponent_logo_id) if game.opponent_logo_id else None
        for game in items
    }
    opponents: dict[str, str] = {}
    unresolved_opponent_game_ids: set[str] = set()
    for game in items:
        team = team_map.get(game.team_id)
        if not team:
            continue
        try:
            opponents[game.id] = opponent_name(game, team)
        except TeamIdentityError:
            unresolved_opponent_game_ids.add(game.id)
    game_groups = dashboard_game_groups(db, items, team_map)
    game_ids = {game.id for game in items}
    game_posts = (
        list(
            db.scalars(
                select(Post).where(
                    Post.club_id == current.club_id,
                    Post.game_id.in_(game_ids),
                    Post.active_key == "active",
                )
            )
        )
        if game_ids
        else []
    )
    post_ids = {post.id for post in game_posts}
    generation_jobs = (
        list(
            db.scalars(
                select(GenerationJob).where(
                    GenerationJob.club_id == current.club_id,
                    GenerationJob.game_id.in_(game_ids),
                )
            )
        )
        if game_ids
        else []
    )
    story_rules = (
        list(
            db.scalars(
                select(StoryRule).where(
                    StoryRule.club_id == current.club_id,
                    StoryRule.team_id.in_(team_map),
                    StoryRule.active.is_(True),
                )
            )
        )
        if team_map
        else []
    )
    media_preferences = (
        list(
            db.scalars(
                select(GameMediaPreference).where(
                    GameMediaPreference.club_id == current.club_id,
                    GameMediaPreference.game_id.in_(game_ids),
                    GameMediaPreference.contribution_type == "announcement",
                )
            )
        )
        if game_ids
        else []
    )
    media_preferences_by_game = {
        preference.game_id: preference for preference in media_preferences
    }
    publication_jobs = (
        list(
            db.scalars(
                select(PublicationJob).where(
                    PublicationJob.club_id == current.club_id,
                    PublicationJob.post_id.in_(post_ids),
                )
            )
        )
        if post_ids
        else []
    )
    channel_rows = operational_channels(db, current.club_id)
    publication_rows = publication_views(
        db,
        publication_jobs,
        club_id=current.club_id,
        channels=channel_rows,
    )
    views_by_game: dict[str, list] = {game_id: [] for game_id in game_ids}
    for row in publication_rows:
        if row.job.game_id in views_by_game:
            views_by_game[row.job.game_id].append(row)
    posts_by_game: dict[str, list[Post]] = {game_id: [] for game_id in game_ids}
    for post in game_posts:
        if post.game_id in posts_by_game:
            posts_by_game[post.game_id].append(post)
    generation_jobs_by_game: dict[str, list[GenerationJob]] = {
        game_id: [] for game_id in game_ids
    }
    for job in generation_jobs:
        if job.game_id in generation_jobs_by_game:
            generation_jobs_by_game[job.game_id].append(job)
    for group in game_groups:
        group["games"].sort(
            key=lambda game: (
                local_kickoff(game),
                team_order.get(game.team_id, len(team_order)),
                game.id,
            )
        )
        rows = [row for game in group["games"] for row in views_by_game.get(game.id, [])]
        posts_for_group = [
            post for game in group["games"] for post in posts_by_game.get(game.id, [])
        ]
        generation_jobs_for_group = [
            job
            for game in group["games"]
            for job in generation_jobs_by_game.get(game.id, [])
        ]
        group["publication_rows"] = sorted(rows, key=lambda row: row.scheduled_at)
        group["publication_targets"] = list(
            dict.fromkeys((row.channel.label, row.target) for row in group["publication_rows"])
        )
        group["contribution_count"] = len({post.id for post in posts_for_group})
        group["attention"] = any(row.attention for row in rows)
        group["next_publication"] = min(
            (
                row
                for row in rows
                if row.job.status
                not in {JobStatus.PUBLISHED, JobStatus.CANCELLED, JobStatus.SKIPPED}
            ),
            key=lambda row: row.scheduled_at,
            default=None,
        )
        group["detail_post"] = next(
            (row.post for row in rows if row.post),
            posts_for_group[0] if posts_for_group else None,
        )
        group["automation"] = build_game_automation_summary(
            db,
            club_id=current.club_id,
            games=group["games"],
            teams={game.team_id: team_map[game.team_id] for game in group["games"]},
            posts=posts_for_group,
            generation_jobs=generation_jobs_for_group,
            story_rules=story_rules,
            publication_rows=group["publication_rows"],
            settings=settings,
            bundle_id=group["key"],
            now=now_utc,
        )
        identity_review_game = next(
            (
                game
                for game in group["games"]
                if (game.overrides or {}).get("generation_identity_review_required")
            ),
            None,
        )
        group["identity_review_team_id"] = (
            identity_review_game.team_id if identity_review_game else None
        )
        job_statuses = {row.job.status for row in rows}
        post_statuses = {post.status for post in posts_for_group}
        failed_generation = any(
            job.status
            in {
                GenerationJobStatus.FAILED,
                GenerationJobStatus.MANUAL_REVIEW_REQUIRED,
            }
            for job in generation_jobs_for_group
        ) and not posts_for_group
        group["problem_generation_job"] = max(
            (
                job
                for job in generation_jobs_for_group
                if job.status
                in {
                    GenerationJobStatus.FAILED,
                    GenerationJobStatus.MANUAL_REVIEW_REQUIRED,
                }
            ),
            key=lambda job: job.updated_at or job.created_at,
            default=None,
        )
        if (
            failed_generation
            or JobStatus.FAILED in job_statuses
            or JobStatus.UNCERTAIN in job_statuses
            or PostStatus.ERROR in post_statuses
        ):
            status_key, status_label = "problem", "Problem"
        elif (
            any(row.attention for row in rows)
            or post_statuses
            & {
                PostStatus.CREATING,
                PostStatus.INCOMPLETE,
                PostStatus.PENDING,
                PostStatus.REAPPROVAL,
            }
        ):
            status_key, status_label = "attention", "Freigabe ausstehend"
        elif JobStatus.PUBLISHED in job_statuses or PostStatus.PUBLISHED in post_statuses:
            if job_statuses and job_statuses <= {
                JobStatus.PUBLISHED,
                JobStatus.CANCELLED,
                JobStatus.SKIPPED,
            }:
                status_key, status_label = "published", "Veröffentlicht"
            else:
                status_key, status_label = "attention", "Teilweise veröffentlicht"
        elif job_statuses & {
            JobStatus.APPROVED,
            JobStatus.SCHEDULED,
            JobStatus.WAITING,
            JobStatus.PUBLISHING,
            JobStatus.RETRY,
        } or PostStatus.SCHEDULED in post_statuses:
            status_key, status_label = "planned", "Geplant"
        elif posts_for_group:
            status_key, status_label = "attention", "Manuelle Planung erforderlich"
        elif group["automation"].contribution_status == "planned":
            status_key, status_label = "planned", "Automatisch geplant"
        elif group["automation"].contribution_status == "problem":
            status_key, status_label = "problem", "Erstellung prüfen"
        elif group["automation"].contribution_status == "manual":
            status_key, status_label = "attention", "Manuelle Erstellung erforderlich"
        else:
            status_key, status_label = "missing", "Beitrag kann erstellt werden"
        group["status_key"] = status_key
        group["status_label"] = status_label
        group["contribution_label"] = group["automation"].contribution_label
        group["channel_labels"] = list(
            dict.fromkeys(row.channel.label for row in group["publication_rows"])
        )
        if group["detail_post"]:
            group["action_label"] = {
                "attention": "Beitrag prüfen",
                "published": "Veröffentlichung ansehen",
                "problem": "Problem prüfen",
            }.get(status_key, "Beitrag ansehen")
        elif group["automation"].action_type == "create_early":
            group["action_label"] = "Jetzt vorzeitig erstellen"
        elif group["automation"].action_type == "problem":
            group["action_label"] = "Problem prüfen"
        elif group["automation"].action_type == "overdue":
            group["action_label"] = "Jetzt erstellen"
        else:
            group["action_label"] = (
                "Gemeinsamen Beitrag erstellen"
                if group["grouped"]
                else "Beitrag jetzt erstellen"
            )

    if contribution_status != "all":
        game_groups = [
            group for group in game_groups if group["status_key"] == contribution_status
        ]
    upcoming_groups: dict[object, list[dict]] = {}
    past_groups: dict[object, list[dict]] = {}
    for group in game_groups:
        local_date = min(local_kickoff(game) for game in group["games"]).date()
        target = upcoming_groups if local_date >= today else past_groups
        target.setdefault(local_date, []).append(group)
    upcoming_day_groups = sorted(upcoming_groups.items(), key=lambda row: row[0])
    past_day_groups = sorted(past_groups.items(), key=lambda row: row[0], reverse=True)
    return render(
        request,
        "games.html",
        current,
        teams=teams,
        items=items,
        logo_map=logo_map,
        opponents=opponents,
        unresolved_opponent_game_ids=unresolved_opponent_game_ids,
        game_groups=game_groups,
        upcoming_day_groups=upcoming_day_groups,
        past_day_groups=past_day_groups,
        today=today,
        weekday_labels=WEEKDAY_LABELS,
        selected_team_id=team_id,
        selected_period=period,
        selected_contribution_status=contribution_status,
        views_by_game=views_by_game,
        media_preferences_by_game=media_preferences_by_game,
        suppressed_items=suppressed_items,
        title="Spiele",
    )


@router.get("/games/{game_id}/media-selection", response_class=HTMLResponse)
def game_media_selection(
    game_id: str,
    request: Request,
    contribution_type: str = Query(default="announcement"),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    if not current.club_id:
        raise HTTPException(403, "Eindeutiger Vereinskontext fehlt")
    game = db.scalar(
        select(Game).where(Game.id == game_id, Game.club_id == current.club_id)
    )
    if not game:
        raise HTTPException(404, "Spiel nicht gefunden")
    require(current, db, "generate", game.team_id)
    team = db.scalar(
        select(Team).where(Team.id == game.team_id, Team.club_id == current.club_id)
    )
    if not team:
        raise HTTPException(404, "Mannschaft nicht gefunden")
    if contribution_type not in CONTRIBUTION_TYPE_LABELS:
        raise HTTPException(422, "Unbekannter Beitragstyp")

    preference = db.scalar(
        select(GameMediaPreference).where(
            GameMediaPreference.club_id == current.club_id,
            GameMediaPreference.game_id == game.id,
            GameMediaPreference.team_id == team.id,
            GameMediaPreference.contribution_type == contribution_type,
        )
    )
    media_assets = list(
        db.scalars(
            select(MediaAsset)
            .where(
                MediaAsset.club_id == current.club_id,
                MediaAsset.team_id == team.id,
                MediaAsset.deleted_at.is_(None),
            )
            .order_by(MediaAsset.captured_at.desc(), MediaAsset.created_at.desc())
        )
    )
    policy_categories = effective_policy(db, current.club_id, contribution_type)
    existing_post = db.scalar(
        select(Post).where(
            Post.club_id == current.club_id,
            Post.game_id == game.id,
            Post.post_type == contribution_type,
            Post.active_key == "active",
        )
    )
    return render(
        request,
        "game_media_selection.html",
        current,
        game=game,
        team=team,
        contribution_type=contribution_type,
        contribution_labels=CONTRIBUTION_TYPE_LABELS,
        media_assets=media_assets,
        media_category_labels=MEDIA_CATEGORY_LABELS,
        usage_status=usage_status,
        preference=preference,
        policy_categories=policy_categories,
        existing_post=existing_post,
        title="Bildauswahl für das Spiel",
    )


@router.post("/games/{game_id}/media-selection")
def update_game_media_selection(
    game_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    contribution_type: str = Form(),
    selection_mode: str = Form(),
    selected_media_asset_id: str = Form(default=""),
    allow_used_once: str = Form(default=""),
    apply_existing: str = Form(default="future"),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    if not current.club_id:
        raise HTTPException(403, "Eindeutiger Vereinskontext fehlt")
    game = db.scalar(
        select(Game).where(Game.id == game_id, Game.club_id == current.club_id)
    )
    if not game:
        raise HTTPException(404, "Spiel nicht gefunden")
    require(current, db, "generate", game.team_id)
    try:
        preference = set_game_preference(
            db,
            club_id=current.club_id,
            team_id=game.team_id,
            game_id=game.id,
            contribution_type=contribution_type,
            selection_mode=selection_mode,
            selected_media_asset_id=selected_media_asset_id or None,
            allow_used_once=allow_used_once == "on",
            actor_user_id=current.id,
        )
    except MediaLibraryError as exc:
        raise HTTPException(422, str(exc)) from exc
    audit(
        db,
        current,
        "game.media_selection_updated",
        "game_media_preference",
        preference.id,
        game.team_id,
        {
            "game_id": game.id,
            "contribution_type": contribution_type,
            "selection_mode": selection_mode,
            "selected_media_asset_id": preference.selected_media_asset_id,
            "allow_used_once": preference.allow_used_once,
        },
    )
    existing_post = db.scalar(
        select(Post).where(
            Post.club_id == current.club_id,
            Post.game_id == game.id,
            Post.post_type == contribution_type,
            Post.active_key == "active",
        )
    )
    if apply_existing == "regenerate":
        if not existing_post:
            raise HTTPException(409, "Für dieses Spiel ist kein bestehender Beitrag vorhanden")
        if selection_mode != "manual" or not preference.selected_media_asset_id:
            raise HTTPException(
                422,
                "Für eine sofortige Neuerzeugung muss ein Bild bewusst ausgewählt sein",
            )
        from app.jobs.generation import enqueue_rerender

        story_job_ids = list(
            db.scalars(
                select(PublicationJob.id).where(
                    PublicationJob.club_id == current.club_id,
                    PublicationJob.post_id == existing_post.id,
                    PublicationJob.kind == "story",
                    PublicationJob.status != JobStatus.PUBLISHED,
                )
            )
        )
        job = enqueue_rerender(
            db,
            existing_post,
            current,
            existing_post.version,
            story_job_ids,
            preference.selected_media_asset_id,
            rerender_feed=True,
        )
        return redirect(
            f"/generation-jobs/{job.id}",
            "Bildauswahl gespeichert und Neuerzeugung eingereiht",
        )
    db.commit()
    message = (
        "Die automatische Bildauswahl wurde gespeichert"
        if selection_mode == "automatic"
        else "Das Bild wurde für diesen Beitragstyp vorgemerkt"
    )
    return redirect(
        f"/games/{game.id}/media-selection?contribution_type={contribution_type}",
        message,
    )


@router.post("/games/bundles/connect")
def connect_game_bundle(
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    game_ids: list[str] = Form(default=[]),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    selected_ids = list(dict.fromkeys(game_ids))
    games = [db.get(Game, item_id) for item_id in selected_ids]
    if len(selected_ids) < 2 or any(item is None for item in games):
        raise HTTPException(422, "Bitte mindestens zwei vorhandene Spiele auswählen")
    teams = {item.team_id: db.get(Team, item.team_id) for item in games}
    for item in games:
        require(current, db, "edit_game", item.team_id)
        if not teams.get(item.team_id):
            raise HTTPException(404, "Mannschaft nicht gefunden")
    try:
        bundle_id = connect_games(db, games, teams)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    audit(
        db,
        current,
        "games.generation_bundle_connected",
        "game_bundle",
        bundle_id,
        games[0].team_id,
        {"game_ids": selected_ids},
    )
    db.commit()
    return redirect("/games", "Spiele wurden zu einem gemeinsamen Spieltag zusammengefasst")


@router.post("/games/bundles/separate")
def separate_game_bundle(
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    game_ids: list[str] = Form(default=[]),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    selected_ids = list(dict.fromkeys(game_ids))
    games = [db.get(Game, item_id) for item_id in selected_ids]
    if len(selected_ids) < 2 or any(item is None for item in games):
        raise HTTPException(422, "Die verbundene Spielgruppe ist nicht mehr vollständig")
    for item in games:
        require(current, db, "edit_game", item.team_id)
    separate_games(games)
    audit(
        db,
        current,
        "games.generation_bundle_separated",
        "game_bundle",
        None,
        games[0].team_id,
        {"game_ids": selected_ids},
    )
    db.commit()
    return redirect("/games", "Spiele werden künftig getrennt behandelt")


@router.post("/games/mock")
async def create_mock_game(
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    team_id: str = Form(),
    opponent: str = Form(),
    side: str = Form(),
    kickoff: str = Form(),
    competition: str = Form(default=""),
    venue: str = Form(default=""),
    pitch: str = Form(default=""),
    opponent_logo: UploadFile | None = File(default=None),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require(current, db, "edit_game", team_id)
    team = db.get(Team, team_id)
    if not team or team.archived_at:
        raise HTTPException(404, "Mannschaft nicht gefunden")
    opponent = opponent.strip()
    if not opponent:
        raise HTTPException(422, "Gegner muss angegeben werden")
    if side not in {"home", "away"}:
        raise HTTPException(422, "Bitte Heim- oder Auswärtsspiel auswählen")
    own_variants = set().union(
        team_name_variants(team.display_name),
        team_name_variants(team.club),
    )
    if own_variants & team_name_variants(opponent):
        raise HTTPException(422, "Der Gegner darf nicht die eigene Mannschaft sein")
    own_name = team.display_name
    home_team, away_team = (own_name, opponent) if side == "home" else (opponent, own_name)
    from zoneinfo import ZoneInfo

    try:
        kickoff_at = (
            datetime.fromisoformat(kickoff)
            .replace(tzinfo=ZoneInfo("Europe/Berlin"))
            .astimezone(timezone.utc)
        )
    except ValueError as e:
        raise HTTPException(422, "Ungültiger Spieltermin") from e
    item = Game(
        team_id=team_id,
        provider="mock",
        external_id=f"mock-{hashlib.sha256(f'{team_id}:{kickoff}:{home_team}:{away_team}'.encode()).hexdigest()[:20]}",
        home_team=home_team,
        away_team=away_team,
        kickoff=kickoff_at,
        competition=competition or None,
        venue=venue or None,
        pitch=pitch or None,
        source_url="fixture://dashboard",
        checked_at=datetime.now(timezone.utc),
    )
    db.add(item)
    try:
        db.flush()
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(409, "Dieses Spiel existiert bereits") from e
    if opponent_logo and opponent_logo.filename:
        try:
            logo_data = await opponent_logo.read()
            logo, _ = store_logo(
                db,
                upload_root=settings.upload_root,
                logo_type="opponent",
                team_id=None,
                display_name=opponent,
                original_filename=opponent_logo.filename,
                content_type=opponent_logo.content_type,
                data=logo_data,
                uploaded_by=current.id,
            )
            publish_shared_opponent_logo(
                db,
                upload_root=settings.upload_root,
                source=logo,
                data=logo_data,
            )
            item.opponent_logo_id = logo.id
            item.overrides = {
                **(item.overrides or {}),
                "opponent_logo_source": "manual_upload_confirmed",
            }
        except LogoValidationError as exc:
            db.rollback()
            raise HTTPException(422, str(exc)) from exc
    audit(
        db,
        current,
        "game.mock_created",
        "game",
        item.id,
        team_id,
        {"opponent_logo_uploaded": bool(item.opponent_logo_id)},
    )
    db.commit()
    return redirect("/games", "Spiel wurde manuell angelegt")


@router.post("/games/{game_id}/delete")
@router.post("/games/{game_id}/delete-mock")
def delete_game(
    game_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    game = db.get(Game, game_id)
    if not game:
        raise HTTPException(404, "Spiel nicht gefunden")
    require(current, db, "edit_game", game.team_id)
    active_statuses = {
        GenerationJobStatus.QUEUED,
        GenerationJobStatus.RUNNING,
        GenerationJobStatus.RETRY_WAIT,
    }
    if db.scalar(
        select(GenerationJob.id).where(
            GenerationJob.game_id == game.id,
            GenerationJob.status.in_(active_statuses),
        )
    ):
        raise HTTPException(
            409,
            "Für dieses Spiel läuft noch ein Generierungsauftrag",
        )
    posts = db.scalars(select(Post).where(Post.game_id == game.id)).all()
    publication_jobs = db.scalars(
        select(PublicationJob).where(PublicationJob.game_id == game.id)
    ).all()
    publication_job_ids = [job.id for job in publication_jobs]
    if publication_job_ids and db.scalar(
        select(MetaPublishingAttempt.id).where(
            MetaPublishingAttempt.publication_job_id.in_(publication_job_ids),
            MetaPublishingAttempt.phase.not_in(["completed", "failed"]),
        )
    ):
        raise HTTPException(
            409,
            "Für dieses Spiel läuft ein Meta-Veröffentlichungsversuch; zuerst abschließen oder abgleichen",
        )

    is_dashboard_mock = game.provider == "mock" and game.source_url == "fixture://dashboard"
    if not is_dashboard_mock or posts:
        now = datetime.now(timezone.utc)
        overrides = dict(game.overrides or {})
        overrides.update(
            {
                "dashboard_deleted": True,
                "dashboard_deleted_at": now.isoformat(),
                "dashboard_deleted_by": current.id,
                "automation_blocked": True,
            }
        )
        if not is_dashboard_mock:
            overrides.update(
                {
                    "import_suppressed": True,
                    "import_suppressed_at": now.isoformat(),
                    "import_suppressed_by": current.id,
                }
            )
        game.overrides = overrides
        game.version += 1
        for post in posts:
            post.publishing_enabled = False
            if post.status not in {PostStatus.PUBLISHED, PostStatus.PARTIAL}:
                post.status = PostStatus.CANCELLED
                post.approved_version = None
        for publication_job in publication_jobs:
            if publication_job.status != JobStatus.PUBLISHED:
                publication_job.status = JobStatus.CANCELLED
                publication_job.approval_status = "unapproved"
                publication_job.approved_post_version = None
                publication_job.error = (
                    "Spiel wurde im Dashboard gelöscht und für Provider-Importe unterdrückt"
                )
        audit(
            db,
            current,
            "game.mock_suppressed" if is_dashboard_mock else "game.provider_suppressed",
            "game",
            game.id,
            game.team_id,
            {
                "provider": game.provider,
                "external_id": game.external_id,
                "home_team": game.home_team,
                "away_team": game.away_team,
                "preserved_published_jobs": sum(
                    job.status == JobStatus.PUBLISHED for job in publication_jobs
                ),
            },
        )
        db.commit()
        return redirect(
            "/games",
            "Spiel ausgeblendet"
            if is_dashboard_mock
            else "Spiel gelöscht und für erneute Provider-Importe unterdrückt",
        )

    for asset in db.scalars(select(MediaAsset).where(MediaAsset.reserved_game_id == game.id)):
        asset.reserved_game_id = None
        asset.uses = max(0, asset.uses - 1)
    terminal_job_ids = list(
        db.scalars(select(GenerationJob.id).where(GenerationJob.game_id == game.id))
    )
    if terminal_job_ids:
        db.execute(delete(GenerationJob).where(GenerationJob.game_id == game.id))

    # SQLAlchemy kennt hier keine ORM-Beziehung, aus der es die erforderliche
    # Reihenfolge ableiten könnte. Deshalb müssen Reservierungen und abhängige
    # Generierungsaufträge vor dem DELETE des Spiels explizit geschrieben werden.
    db.flush()
    audit(
        db,
        current,
        "game.mock_deleted",
        "game",
        game.id,
        game.team_id,
        {
            "home_team": game.home_team,
            "away_team": game.away_team,
            "kickoff": game.kickoff.isoformat(),
            "removed_generation_jobs": len(terminal_job_ids),
        },
    )
    db.delete(game)
    db.flush()
    db.commit()
    return redirect("/games", "Spiel wurde gelöscht")


@router.post("/games/{game_id}/restore")
def restore_provider_game(
    game_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    game = db.get(Game, game_id)
    if not game:
        raise HTTPException(404, "Spiel nicht gefunden")
    require(current, db, "edit_game", game.team_id)
    overrides = dict(game.overrides or {})
    is_dashboard_mock = game.provider == "mock" and game.source_url == "fixture://dashboard"
    if not overrides.get("dashboard_deleted") and not overrides.get("import_suppressed"):
        raise HTTPException(409, "Dieses Spiel ist nicht gelöscht")
    for key in (
        "dashboard_deleted",
        "dashboard_deleted_at",
        "dashboard_deleted_by",
        "import_suppressed",
        "import_suppressed_at",
        "import_suppressed_by",
    ):
        overrides.pop(key, None)
    team = db.get(Team, game.team_id)
    provider_status = overrides.get("provider_status", game.status)
    provisional_allowed = bool(overrides.get("provisional_confirmed_by")) or bool(
        (team.rules or {}).get("allow_provisional_games") if team else False
    )
    overrides["automation_blocked"] = provider_status in {"cancelled", "postponed"} or (
        provider_status == "provisional" and not provisional_allowed
    )
    game.overrides = overrides
    game.version += 1
    audit(
        db,
        current,
        "game.mock_restored" if is_dashboard_mock else "game.provider_restored",
        "game",
        game.id,
        game.team_id,
        {"provider": game.provider, "external_id": game.external_id},
    )
    db.commit()
    return redirect("/games", "Unterdrücktes Provider-Spiel wieder eingeblendet")


@router.post("/games/{game_id}/details")
def update_game_details(
    game_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    competition: str = Form(),
    venue: str = Form(),
    pitch: str = Form(),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    from app.models import JobStatus, PostStatus

    check_csrf(request, csrf_token_value)
    game = db.get(Game, game_id)
    if not game:
        raise HTTPException(404)
    require(current, db, "edit_game", game.team_id)
    if pitch not in {"", "Rasenplatz", "Kunstrasenplatz"}:
        raise HTTPException(422, "Platzart muss Rasenplatz oder Kunstrasenplatz sein")
    before = {"competition": game.competition, "venue": game.venue, "pitch": game.pitch}
    after = {
        "competition": competition.strip() or None,
        "venue": venue.strip() or None,
        "pitch": pitch or None,
    }
    if before == after:
        return redirect("/games", "Keine Spieldaten geändert")
    game.competition = after["competition"]
    game.venue = after["venue"]
    game.pitch = after["pitch"]
    game.overrides = {**(game.overrides or {}), "manual_venue_details": True}
    game.version += 1
    posts = db.scalars(
        select(Post).where(
            Post.game_id == game.id,
            Post.status.not_in([PostStatus.PUBLISHED, PostStatus.CANCELLED]),
        )
    ).all()
    for post in posts:
        post.status = PostStatus.REAPPROVAL
        post.version += 1
        post.approved_version = None
        for job in db.scalars(
            select(PublicationJob).where(
                PublicationJob.post_id == post.id, PublicationJob.status != JobStatus.PUBLISHED
            )
        ):
            job.status = JobStatus.UNAPPROVED
            job.approval_status = "reapproval_required"
            job.approved_post_version = None
            job.error = "Manuelle Spieldetails wurden geändert; Grafiken prüfen und neu erzeugen"
    audit(
        db,
        current,
        "game.details_updated",
        "game",
        game.id,
        game.team_id,
        {"before": before, "after": after, "affected_posts": [post.id for post in posts]},
    )
    db.commit()
    return redirect("/games", "Spielort, Platzart und Wettbewerb gespeichert")


@router.post("/games/{game_id}/result")
def confirm_game_result(
    game_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    version: int = Form(),
    home_score: int = Form(),
    away_score: int = Form(),
    confirmation: bool = Form(default=False),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    game = db.scalar(select(Game).where(Game.id == game_id).with_for_update())
    if not game:
        raise HTTPException(404, "Spiel nicht gefunden")
    require(current, db, "edit_game", game.team_id)
    if game.version != version:
        raise HTTPException(
            409,
            "Das Spiel wurde zwischenzeitlich geändert. Bitte Seite neu laden und Ergebnis prüfen.",
        )
    if not confirmation:
        raise HTTPException(422, "Das Ergebnis muss ausdrücklich bestätigt werden")
    if not 0 <= home_score <= 99 or not 0 <= away_score <= 99:
        raise HTTPException(422, "Tore müssen als Zahl zwischen 0 und 99 angegeben werden")
    if game.status in {"cancelled", "postponed"}:
        raise HTTPException(409, "Für abgesagte oder verschobene Spiele ist kein Ergebnis möglich")

    before = {
        "home_score": game.home_score,
        "away_score": game.away_score,
        "result_confirmed": game.result_confirmed,
        "status": game.status,
    }
    after = {
        "home_score": home_score,
        "away_score": away_score,
        "result_confirmed": True,
        "status": "finished",
    }
    if before == after:
        return redirect("/games", "Dieses Ergebnis ist bereits bestätigt")

    now = datetime.now(timezone.utc)
    candidate = f"{home_score}:{away_score}"
    game.home_score = home_score
    game.away_score = away_score
    game.result_confirmed = True
    game.status = "finished"
    game.checked_at = now
    game.version += 1
    overrides = dict(game.overrides or {})
    overrides.update(
        {
            "provider_score_candidate": candidate,
            "provider_score_first_seen_at": overrides.get(
                "provider_score_first_seen_at", now.isoformat()
            ),
            "provider_score_observations": max(
                2, int(overrides.get("provider_score_observations", 0))
            ),
            "result_detected_at": overrides.get("result_detected_at", now.isoformat()),
            "result_confirmed_at": now.isoformat(),
            "result_confirmation_source": "dashboard_manual",
            "result_confirmed_by": current.id,
        }
    )
    game.overrides = overrides

    result_changed = before["result_confirmed"] and (
        before["home_score"],
        before["away_score"],
    ) != (home_score, away_score)
    reason = "Bestätigtes Spielergebnis wurde im Dashboard geändert"
    affected_posts = _invalidate_posts_for_result_change(db, game, reason) if result_changed else []
    audit(
        db,
        current,
        "game.result_confirmed_manually",
        "game",
        game.id,
        game.team_id,
        {
            "before": before,
            "after": after,
            "affected_posts": affected_posts,
        },
    )
    db.commit()
    message = f"Ergebnis {home_score}:{away_score} wurde bestätigt"
    if affected_posts:
        message += "; vorhandene Ergebnisbeiträge benötigen eine erneute Freigabe"
    return redirect("/games", message)


@router.post("/games/{game_id}/generate")
def generate_game_post(
    game_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    post_type: str = Form(),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    from app.jobs.generation import enqueue_bundle_create

    check_csrf(request, csrf_token_value)
    game = db.get(Game, game_id)
    if not game:
        raise HTTPException(404)
    require(current, db, "generate", game.team_id)
    team = db.get(Team, game.team_id)
    if not team:
        raise HTTPException(404, "Mannschaft nicht gefunden")
    try:
        job, post = enqueue_bundle_create(db, game, team, current, post_type)
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    except ValueError as e:
        if post_type == "result" and "bestätigt" in str(e):
            return redirect("/games", str(e))
        raise HTTPException(422, str(e)) from e
    if post:
        return redirect(f"/posts/{post.id}", "Vorhandener Beitrag geöffnet")
    return redirect(f"/generation-jobs/{job.id}", "Generierung wurde eingereiht")


@router.get("/generation-jobs", response_class=HTMLResponse)
def generation_jobs(request: Request, current=Depends(current_user), db: Session = Depends(get_db)):
    all_items = [
        item
        for item in db.scalars(
            select(GenerationJob).order_by(GenerationJob.created_at.desc()).limit(200)
        )
        if require_visible(db, current, item.team_id)
    ]
    superseded_by = {
        str((item.parameters or {}).get("manual_retry_of_job_id")): item.id
        for item in all_items
        if (item.parameters or {}).get("manual_retry_of_job_id")
    }
    show_history = request.query_params.get("history") == "1"
    items = (
        all_items if show_history else [item for item in all_items if item.id not in superseded_by]
    )
    teams = {item.id: item for item in db.scalars(select(Team))}
    games_map = {item.id: item for item in db.scalars(select(Game))}
    return render(
        request,
        "generation_jobs.html",
        current,
        items=items,
        teams=teams,
        games=games_map,
        show_history=show_history,
        superseded_by=superseded_by,
        title="Generierungsaufträge",
    )


@router.get("/generation-jobs/{job_id}", response_class=HTMLResponse)
def generation_job_detail(
    job_id: str, request: Request, current=Depends(current_user), db: Session = Depends(get_db)
):
    item = db.get(GenerationJob, job_id)
    if not item:
        raise HTTPException(404)
    require(current, db, "view", item.team_id)
    team = db.get(Team, item.team_id)
    game = db.get(Game, item.game_id)
    requester = db.get(User, item.requested_by)
    return render(
        request,
        "generation_job.html",
        current,
        item=item,
        team=team,
        game=game,
        requester=requester,
        title="Generierungsauftrag",
    )


@router.post("/generation-jobs/{job_id}/cancel")
def cancel_generation_job(
    job_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    from app.jobs.generation import request_cancel

    check_csrf(request, csrf_token_value)
    item = db.get(GenerationJob, job_id)
    if not item:
        raise HTTPException(404)
    require(current, db, "generate", item.team_id)
    try:
        request_cancel(db, item)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return redirect(f"/generation-jobs/{item.id}", "Abbruch wurde gespeichert")


@router.post("/generation-jobs/{job_id}/retry")
def retry_generation_job(
    job_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    confirm_new_budget: str = Form(""),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    from app.jobs.generation import retry_job

    check_csrf(request, csrf_token_value)
    item = db.get(GenerationJob, job_id)
    if not item:
        raise HTTPException(404)
    require(current, db, "generate", item.team_id)
    try:
        retry = retry_job(
            db,
            item,
            current,
            confirm_new_budget_with_existing_output=(confirm_new_budget == "1"),
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return redirect(
        f"/generation-jobs/{retry.id}",
        "Ein neuer Auftrag mit frischem Wiederholungsbudget wurde eingereiht",
    )


@router.post("/generation-jobs/{job_id}/review")
def review_generation_job(
    job_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    note: str = Form(),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    item = db.get(GenerationJob, job_id)
    if not item:
        raise HTTPException(404)
    require(current, db, "generate", item.team_id)
    if item.status != GenerationJobStatus.MANUAL_REVIEW_REQUIRED:
        raise HTTPException(409, "Dieser Auftrag benötigt keine manuelle Prüfung")
    item.parameters = {
        **(item.parameters or {}),
        "manual_review": {
            "by": current.id,
            "at": datetime.now(timezone.utc).isoformat(),
            "note": note.strip(),
        },
    }
    audit(
        db,
        current,
        "generation.manual_review_acknowledged",
        "generation_job",
        item.id,
        item.team_id,
        {"note": note.strip()},
    )
    db.commit()
    return redirect(f"/generation-jobs/{item.id}", "Manuelle Prüfung wurde dokumentiert")


@router.get("/diagnostics", response_class=HTMLResponse)
def diagnostics(request: Request, current=Depends(current_user), db: Session = Depends(get_db)):
    from app.models import ProviderSnapshot

    require_admin(current)
    teams = db.scalars(select(Team).where(Team.archived_at.is_(None))).all()
    snapshots = db.scalars(
        select(ProviderSnapshot).order_by(ProviderSnapshot.fetched_at.desc()).limit(100)
    ).all()
    return render(
        request,
        "diagnostics.html",
        current,
        teams=teams,
        snapshots=snapshots,
        title="Provider-Diagnose",
    )


@router.post("/diagnostics/fussball/{team_id}")
def run_diagnostic(
    team_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    confirmation: str = Form(),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    from app.games.live_test import LiveTestDisabled, capture

    check_csrf(request, csrf_token_value)
    require_admin(current)
    if confirmation != "NUR LESEN":
        raise HTTPException(422, "Bestätigung 'NUR LESEN' erforderlich")
    team = db.get(Team, team_id)
    if not team:
        raise HTTPException(404)
    try:
        snapshot = capture(db, team, settings)
    except LiveTestDisabled as exc:
        raise HTTPException(409, str(exc)) from exc
    audit(
        db,
        current,
        "provider.snapshot_captured",
        "provider_snapshot",
        snapshot.id,
        team.id,
        {"checksum": snapshot.checksum},
    )
    db.commit()
    return redirect("/diagnostics", "Diagnose-Snapshot gespeichert")


@router.get("/diagnostics/{snapshot_id}/html")
def download_snapshot(
    snapshot_id: str, current=Depends(current_user), db: Session = Depends(get_db)
):
    from app.models import ProviderSnapshot

    require_admin(current)
    snapshot = db.get(ProviderSnapshot, snapshot_id)
    if not snapshot:
        raise HTTPException(404)
    root = settings.provider_snapshot_root.resolve()
    path = (root / snapshot.relative_path).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(404, "Snapshot-Datei fehlt oder ist unvollständig")
    return detached_file_response(
        db,
        path,
        media_type="text/html",
        filename=f"fussball-{snapshot.checksum[:12]}.html",
    )


@router.post("/diagnostics/{snapshot_id}/fixture")
def snapshot_to_fixture(
    snapshot_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    name: str = Form(),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    from app.models import ProviderSnapshot

    check_csrf(request, csrf_token_value)
    require_admin(current)
    if not name.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(422, "Ungültiger Fixture-Name")
    snapshot = db.get(ProviderSnapshot, snapshot_id)
    root = settings.provider_snapshot_root.resolve()
    if not snapshot:
        raise HTTPException(404)
    source = (root / snapshot.relative_path).resolve()
    if root not in source.parents or not source.is_file():
        raise HTTPException(404, "Snapshot-Datei fehlt")
    target = Path("data/uploads/provider-fixtures") / f"{name}.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise HTTPException(409, "Fixture existiert bereits")
    target.write_bytes(source.read_bytes())
    audit(
        db,
        current,
        "provider.fixture_created",
        "provider_snapshot",
        snapshot.id,
        snapshot.team_id,
        {"fixture": str(target)},
    )
    db.commit()
    return redirect("/diagnostics", "Fixture durch Administrator übernommen")


@router.get("/posts/{post_id}/media/{job_id}")
def post_media(
    post_id: str, job_id: str, current=Depends(current_user), db: Session = Depends(get_db)
):
    item = db.get(Post, post_id)
    job = db.get(PublicationJob, job_id)
    if not item or not job or job.post_id != item.id:
        raise HTTPException(404)
    require(current, db, "view", item.team_id)
    path = Path(job.media_path).resolve()
    root = settings.generated_root.resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(404, "Grafik fehlt")
    return detached_file_response(db, path, media_type="image/png")


@router.get("/posts/{post_id}/media/{job_id}/items/{item_id}")
def post_carousel_media(
    post_id: str,
    job_id: str,
    item_id: str,
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    post = db.get(Post, post_id)
    job = db.get(PublicationJob, job_id)
    media = db.get(PublicationMediaItem, item_id)
    if (
        not post
        or not job
        or not media
        or job.post_id != post.id
        or media.publication_job_id != job.id
    ):
        raise HTTPException(404)
    require(current, db, "view", post.team_id)
    path = Path(media.media_path).resolve()
    root = settings.generated_root.resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(404, "Karussellbild fehlt")
    return detached_file_response(db, path, media_type="image/png")


@router.get("/diagnostics/{snapshot_id}/import", response_class=HTMLResponse)
def snapshot_import_preview(
    snapshot_id: str, request: Request, current=Depends(current_user), db: Session = Depends(get_db)
):
    from app.games.importer import preview_snapshot
    from app.models import ProviderSnapshot

    require_admin(current)
    snapshot = db.get(ProviderSnapshot, snapshot_id)
    if not snapshot:
        raise HTTPException(404)
    team = db.get(Team, snapshot.team_id)
    return render(
        request,
        "diagnostic_import.html",
        current,
        snapshot=snapshot,
        team=team,
        games=preview_snapshot(snapshot),
        title="Spielübernahme prüfen",
    )


@router.post("/diagnostics/{snapshot_id}/import")
def snapshot_import_confirm(
    snapshot_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    confirmation: str = Form(),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    from app.games.importer import SnapshotImportError, import_snapshot
    from app.models import ProviderSnapshot

    check_csrf(request, csrf_token_value)
    require_admin(current)
    if confirmation != "SPIELE ÜBERNEHMEN":
        raise HTTPException(422, "Explizite Bestätigung 'SPIELE ÜBERNEHMEN' erforderlich")
    snapshot = db.get(ProviderSnapshot, snapshot_id)
    if not snapshot:
        raise HTTPException(404)
    try:
        result = import_snapshot(db, snapshot, current)
    except SnapshotImportError as exc:
        raise HTTPException(422, str(exc)) from exc
    return redirect(
        "/diagnostics",
        f"Übernahme abgeschlossen: {result['created']} neu, {result['updated']} aktualisiert, {result['unchanged']} unverändert",
    )


@router.post("/games/{game_id}/confirm-provisional")
def confirm_provisional_game(
    game_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    confirmation: str = Form(),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    game = db.get(Game, game_id)
    if not game:
        raise HTTPException(404)
    if game.status != "provisional":
        raise HTTPException(409, "Spiel ist nicht als vorläufig markiert")
    if confirmation != "VORLÄUFIGES SPIEL BESTÄTIGEN":
        raise HTTPException(422, "Explizite Bestätigung erforderlich")
    game.status = "scheduled"
    game.overrides = {
        **game.overrides,
        "automation_blocked": False,
        "provisional_confirmed_by": current.id,
        "provisional_confirmed_at": datetime.now(timezone.utc).isoformat(),
    }
    game.version += 1
    audit(
        db,
        current,
        "game.provisional_confirmed",
        "game",
        game.id,
        game.team_id,
        {"external_id": game.external_id},
    )
    db.commit()
    return redirect("/games", "Vorläufiges Spiel manuell bestätigt")
