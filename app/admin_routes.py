import hashlib
import mimetypes
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
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
from app.config import get_settings
from app.db import get_db
from app.games.bundles import connect_games, dashboard_game_groups, separate_games
from app.games.identity import team_name_variants
from app.limits.service import LimitExceeded, assert_resource_capacity
from app.logos.service import (
    LogoValidationError,
    import_shared_opponent_logo,
    normalize_club_name,
    opponent_name,
    publish_shared_opponent_logo,
    shared_logo_path,
    store_logo,
)
from app.media.storage import LocalStorageProvider, StorageError, media_asset_path
from app.media.uploads import (
    MAX_PLAYER_IMAGE_BYTES,
    MAX_PLAYER_IMAGE_FILES,
    PlayerImageUploadError,
    ValidatedPlayerImage,
    iter_player_images_from_zip,
    store_player_image,
    validate_player_image,
)
from app.models import (
    AuditLog,
    Club,
    ClubBrandingConfiguration,
    DesignTemplate,
    FontAsset,
    FussballSyncState,
    Game,
    GenerationJob,
    GenerationJobStatus,
    InstagramConnection,
    InstagramPage,
    JobStatus,
    LogoAsset,
    MediaAsset,
    MetaPublishingAttempt,
    Post,
    PostStatus,
    PromptStatus,
    PromptTemplate,
    PromptTestRun,
    PublicationJob,
    PublicationMediaItem,
    Role,
    SharedOpponentLogo,
    StoryRule,
    Team,
    User,
    UserTeam,
)
from app.platform.service import platform_audit
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
from app.posts.service import logo_recompose_availability
from app.publishing.schedule import (
    EDITABLE_JOB_STATUSES,
    PublicationScheduleError,
    reschedule_publication_job,
)
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
        select(Team).where(Team.archived_at.is_(None)).order_by(Team.display_name)
    ).all()
    fonts = db.scalars(
        select(FontAsset).where(
            FontAsset.active.is_(True),
            FontAsset.archived_at.is_(None),
        ).order_by(FontAsset.name)
    ).all()
    logos = db.scalars(
        select(LogoAsset).where(
            LogoAsset.logo_type == "team",
            LogoAsset.active.is_(True),
            LogoAsset.archived_at.is_(None),
        ).order_by(LogoAsset.display_name, LogoAsset.version.desc())
    ).all()
    media_assets = db.scalars(
        select(MediaAsset).where(
            MediaAsset.active.is_(True),
            MediaAsset.available.is_(True),
        ).order_by(MediaAsset.filename)
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
        current_logo.archived_at
        or not current_logo.active
        or current_logo.logo_type != "team"
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
    story_safe_top: int = Form(default=12),
    story_safe_bottom: int = Form(default=15),
    story_use_player_image: str = Form(default=""),
    story_show_sponsors: str = Form(default=""),
    story_show_club_logo: str = Form(default=""),
    story_show_call_to_action: str = Form(default=""),
    story_countdown_area: str = Form(default=""),
    story_extra_rules: str = Form(default=""),
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
    font_ids = [
        value
        for value in (resolved_primary_font_id, resolved_secondary_font_id)
        if value
    ]
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
            normalized_image_effects = normalize_string_list(
                _structured_values(image_effects)
            )
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
                "unwanted_elements": normalize_string_list(
                    _structured_values(unwanted_elements)
                ),
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
                "legacy_values": parse_structured_json(
                    legacy_image_json, "Übernommene Bildwerte"
                ),
            }
            text_settings = {
                "address_style": address_style,
                "tone": tone,
                "text_length": text_length,
                "emoji_usage": emoji_usage,
                "hashtags": normalize_hashtags(_structured_values(hashtags)),
                "mentions": normalize_mentions(_structured_values(mentions)),
                "typical_phrases": normalize_string_list(
                    _structured_values(typical_phrases)
                ),
                "unwanted_phrases": normalize_string_list(
                    _structured_values(unwanted_phrases)
                ),
                "team_name_spelling": team_name_spelling,
                "team_names": parse_structured_json(
                    team_names_json, "Mannschaftsschreibweisen"
                ),
                "home_label": home_label,
                "away_label": away_label,
                "home_venue": home_venue,
                "home_venue_short": home_venue_short,
                "call_to_action": cta_custom if cta_type == "custom" else "",
                "cta_type": cta_type,
                "cta_custom": cta_custom,
                "sponsors": parse_structured_json(sponsors_json, "Sponsoren"),
                "sponsor_mentions": normalize_mentions(
                    _structured_list(sponsor_mentions)
                ),
                "max_hashtags": max_hashtags,
                "legacy_values": parse_structured_json(
                    legacy_text_json, "Übernommene Textwerte"
                ),
            }
            image_settings = validate_branding_settings(image_settings, strict_choices=True)
            text_settings = validate_branding_settings(text_settings, strict_choices=True)
        else:
            raise BrandingValidationError("Unbekannte Branding-Aktion")
    except BrandingValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    teams = db.scalars(select(Team).where(Team.archived_at.is_(None))).all()
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
        selected_logo.logo_type != "team"
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
    pages = db.scalars(select(InstagramPage).where(InstagramPage.archived_at.is_(None))).all()
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
        pages=pages,
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
    short_name: str = Form(),
    slug: str = Form(),
    club: str = Form(),
    fussball_url: str = Form(),
    instagram_page_id: str = Form(),
    media_subdir: str = Form(),
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
    try:
        LocalStorageProvider(settings.media_root).resolve(media_subdir)
    except StorageError as e:
        raise HTTPException(422, str(e)) from e
    page = db.get(InstagramPage, instagram_page_id)
    if not page or page.club_id != current.club_id or not page.active:
        raise HTTPException(422, "Instagram-Seite muss aktiv sein")
    item = Team(
        internal_name=internal_name,
        display_name=display_name,
        short_name=short_name,
        slug=slug,
        club=club,
        fussball_url=fussball_url,
        instagram_page_id=page.id,
        media_subdir=media_subdir,
    )
    db.add(item)
    db.flush()
    audit(db, current, "team.created", "team", item.id, item.id)
    db.commit()
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
    items = db.scalars(
        select(InstagramPage)
        .where(InstagramPage.archived_at.is_(None))
        .order_by(InstagramPage.display_name)
    ).all()
    connections = {
        connection.instagram_page_id: connection
        for connection in db.scalars(select(InstagramConnection)).all()
    }
    attempt_summary = {}
    for item in items:
        connection = connections.get(item.id)
        attempts = (
            db.scalars(
                select(MetaPublishingAttempt)
                .where(MetaPublishingAttempt.connection_id == connection.id)
                .order_by(MetaPublishingAttempt.created_at.desc())
            ).all()
            if connection
            else []
        )
        attempt_summary[item.id] = {
            "last_success": next(
                (x for x in attempts if x.phase == "completed" and x.meta_media_id),
                None,
            ),
            "last_failure": next((x for x in attempts if x.phase == "failed"), None),
            "uncertain": sum(x.phase == "uncertain" for x in attempts),
        }
    return render(
        request,
        "instagram.html",
        current,
        items=items,
        connections=connections,
        attempt_summary=attempt_summary,
        settings=settings,
        title="Instagram-Seiten",
    )


@router.post("/instagram")
def create_instagram(
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    internal_name: str = Form(),
    display_name: str = Form(),
    username: str = Form(),
    club: str = Form(),
    account_id: str = Form(default=""),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    try:
        assert_resource_capacity(db, current.club_id, "instagram_pages")
    except LimitExceeded as exc:
        audit(
            db,
            current,
            "instagram.limit_blocked",
            "club",
            current.club_id,
            details={"reason": str(exc)},
        )
        db.commit()
        raise HTTPException(409, str(exc)) from exc
    item = InstagramPage(
        internal_name=internal_name,
        display_name=display_name,
        username=username.lstrip("@"),
        club=club,
        account_id=account_id or None,
        active=False,
        publishing_enabled=False,
        connection_status="unconfigured",
    )
    db.add(item)
    db.flush()
    audit(db, current, "instagram.created", "instagram_page", item.id)
    db.commit()
    return redirect("/instagram", "Seite angelegt – sicher deaktiviert")


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
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    teams = db.scalars(select(Team).where(Team.archived_at.is_(None))).all()
    visible = [t for t in teams if require_visible(db, current, t.id)]
    selected = next((t for t in visible if t.id == team_id), visible[0] if visible else None)
    items = (
        db.scalars(
            select(MediaAsset)
            .where(MediaAsset.team_id == selected.id)
            .order_by(MediaAsset.filename)
        ).all()
        if selected
        else []
    )
    folders = []
    try:
        folders = [
            x.name for x in settings.media_root.iterdir() if x.is_dir() and not x.is_symlink()
        ]
    except OSError:
        pass
    return render(
        request,
        "media.html",
        current,
        teams=visible,
        selected=selected,
        items=items,
        folders=folders,
        storage_ok=settings.media_root.is_dir(),
        title="Medienbibliothek",
    )


def require_visible(db, current, team_id):
    try:
        require(current, db, "view", team_id)
        return True
    except HTTPException:
        return False


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
    team = db.get(Team, team_id)
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
        asset = db.scalar(
            select(MediaAsset).where(
                MediaAsset.team_id == team.id,
                MediaAsset.storage_kind == "external",
                MediaAsset.relative_path == relative,
            )
        )
        values = {
            "filename": path.name,
            "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            "size": stat.st_size,
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
                    team_id=team.id,
                    storage_kind="external",
                    relative_path=relative,
                    active=True,
                    **values,
                )
            )
    for asset in db.scalars(
        select(MediaAsset).where(
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
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require(current, db, "generate", team_id)
    team = db.get(Team, team_id)
    if not team:
        raise HTTPException(404)
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

        relative, target = store_player_image(settings.upload_root, team.id, image)
        created_paths.append(target)
        stat = target.stat()
        asset = MediaAsset(
            team_id=team.id,
            storage_kind="upload",
            relative_path=relative,
            filename=image.original_filename,
            mime_type=image.mime_type,
            size=stat.st_size,
            checksum=image.checksum,
            mtime=datetime.fromtimestamp(stat.st_mtime, timezone.utc),
            player_name=image.player_name or None,
            active=True,
            available=True,
        )
        db.add(asset)
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
                        "checksum": asset.checksum,
                    }
                    for asset in created_assets
                ],
                "duplicates_skipped": skipped,
                "archive": archive.filename if has_archive else None,
            },
        )
        db.commit()
    except PlayerImageUploadError as exc:
        db.rollback()
        for path in created_paths:
            path.unlink(missing_ok=True)
        raise HTTPException(422, str(exc)) from exc
    except Exception:
        db.rollback()
        for path in created_paths:
            path.unlink(missing_ok=True)
        raise

    message = f"{len(created_assets)} Spielerbilder hochgeladen"
    if skipped:
        message += f", {len(skipped)} Duplikate übersprungen"
    return redirect(f"/media?team_id={team.id}", message)


@router.get("/media/{asset_id}/preview")
def preview_media(
    asset_id: str,
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    asset = db.get(MediaAsset, asset_id)
    if not asset:
        raise HTTPException(404)
    require(current, db, "view", asset.team_id)
    try:
        path = media_asset_path(asset, settings.media_root, settings.upload_root)
    except StorageError as exc:
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(path, media_type=asset.mime_type)


@router.post("/media/{asset_id}/toggle")
def toggle_media(
    asset_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    asset = db.get(MediaAsset, asset_id)
    if not asset:
        raise HTTPException(404)
    require(current, db, "generate", asset.team_id)
    asset.active = not asset.active
    if asset.active and not asset.available:
        raise HTTPException(422, "Eine fehlende Datei kann nicht aktiviert werden")
    audit(db, current, "media.toggled", "media", asset.id, asset.team_id, {"active": asset.active})
    db.commit()
    return redirect(f"/media?team_id={asset.team_id}")


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
    return FileResponse(path, media_type=font.mime_type)


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
    pages = db.scalars(select(InstagramPage).where(InstagramPage.archived_at.is_(None))).all()
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
        title="Veröffentlichungsregeln",
    )


@router.post("/rules/{team_id}/defaults")
def save_rules(
    team_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
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
    auto_approve_announcements: bool = Form(default=False),
    auto_approve_results: bool = Form(default=False),
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
    image_prompt_feed: str = Form(default="default-image-feed"),
    image_prompt_story: str = Form(default="default-image-story"),
    text_prompt: str = Form(default="default-text-announcement"),
    result_image_prompt_feed: str = Form(default="default-image-feed"),
    result_image_prompt_story: str = Form(default="default-image-story"),
    result_text_prompt: str = Form(default="default-text-result"),
    style_direction: str = Form(default=""),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    team = db.get(Team, team_id)
    if not team:
        raise HTTPException(404)
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
    if not 5 <= result_poll_interval_minutes <= 120:
        raise HTTPException(422, "Das Ergebnisintervall muss zwischen 5 und 120 Minuten liegen")
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
    if (grouped_announcements and announcement_feed_output_count != 1) or (
        grouped_results and result_feed_output_count != 1
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
        "announcement": announcement_story_output_count,
        "reminder": reminder_story_output_count,
        "result": result_story_output_count,
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
    if announcement_timing_mode == "weekday_fixed" and len(announcement_weekday_times) != 7:
        raise HTTPException(
            422, "Fuer feste Ankuendigungszeiten sind alle sieben Wochentage erforderlich"
        )
    if reminder_timing_mode == "weekday_fixed" and len(reminder_weekday_times) != 7:
        raise HTTPException(
            422,
            "Für feste Erinnerungszeiten sind alle sieben Wochentage erforderlich",
        )
    if result_timing_mode == "weekday_fixed" and len(result_weekday_times) != 7:
        raise HTTPException(
            422, "Fuer feste Ergebniszeiten sind alle sieben Wochentage erforderlich"
        )
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
        "auto_approve_announcements": auto_approve_announcements,
        "auto_approve_results": auto_approve_results,
        "club_matchday_feed_mode": club_matchday_feed_mode,
        "club_matchday_primary_team_id": club_matchday_primary_team_id or None,
        "reminder_feed_before_minutes": reminder_feed_before_minutes,
        **output_counts,
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
        "style_direction": style_direction.strip(),
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
    sync_state = db.get(FussballSyncState, team.id)
    if automatic_sync_enabled:
        if sync_state is None:
            db.add(
                FussballSyncState(
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
    db.commit()
    return redirect(f"/rules?team_id={team.id}")


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
            f"{post_type}_story_output_count",
            max(1, media_slot),
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
    if timing_mode == "weekday_fixed" and len(weekday_times) != 7:
        raise HTTPException(422, "Fuer feste Story-Zeiten sind alle sieben Wochentage erforderlich")
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
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    if media_format not in {"all", "feed", "story"}:
        raise HTTPException(422, "Ungültiger Formatfilter")
    selected_kinds = {
        "all": None,
        "feed": ["feed", "carousel"],
        "story": ["story"],
    }[media_format]
    now = datetime.now(timezone.utc)
    published_since = now - timedelta(days=2)
    planned_until = now + timedelta(days=days)

    published_query = select(PublicationJob).where(
        PublicationJob.status == JobStatus.PUBLISHED,
        PublicationJob.published_at.is_not(None),
        PublicationJob.published_at >= published_since,
        PublicationJob.published_at <= now,
    )
    overdue_query = select(PublicationJob).where(
        PublicationJob.scheduled_at < now,
        PublicationJob.status.notin_([JobStatus.PUBLISHED, JobStatus.CANCELLED, JobStatus.SKIPPED]),
    )
    planned_query = select(PublicationJob).where(
        PublicationJob.scheduled_at >= now,
        PublicationJob.scheduled_at <= planned_until,
        PublicationJob.status.notin_([JobStatus.PUBLISHED, JobStatus.CANCELLED, JobStatus.SKIPPED]),
    )
    if selected_kinds:
        published_query = published_query.where(PublicationJob.kind.in_(selected_kinds))
        overdue_query = overdue_query.where(PublicationJob.kind.in_(selected_kinds))
        planned_query = planned_query.where(PublicationJob.kind.in_(selected_kinds))

    published_jobs = [
        job
        for job in db.scalars(published_query.order_by(PublicationJob.published_at.desc()))
        if require_visible(db, current, job.team_id)
    ]
    overdue_jobs = [
        job
        for job in db.scalars(overdue_query.order_by(PublicationJob.scheduled_at.desc()))
        if require_visible(db, current, job.team_id)
    ]
    planned_jobs = [
        job
        for job in db.scalars(planned_query.order_by(PublicationJob.scheduled_at))
        if require_visible(db, current, job.team_id)
    ]

    calendar_jobs = [*published_jobs, *overdue_jobs, *planned_jobs]
    post_ids = {job.post_id for job in calendar_jobs}
    game_ids = {job.game_id for job in calendar_jobs if job.game_id}
    page_ids = {job.instagram_page_id for job in calendar_jobs}
    calendar_posts = (
        {post.id: post for post in db.scalars(select(Post).where(Post.id.in_(post_ids)))}
        if post_ids
        else {}
    )
    games = (
        {game.id: game for game in db.scalars(select(Game).where(Game.id.in_(game_ids)))}
        if game_ids
        else {}
    )
    pages = (
        {
            page.id: page
            for page in db.scalars(select(InstagramPage).where(InstagramPage.id.in_(page_ids)))
        }
        if page_ids
        else {}
    )
    calendar_job_ids = {job.id for job in calendar_jobs}
    carousel_items = {job_id: [] for job_id in calendar_job_ids}
    if calendar_job_ids:
        for media in db.scalars(
            select(PublicationMediaItem)
            .where(PublicationMediaItem.publication_job_id.in_(calendar_job_ids))
            .order_by(
                PublicationMediaItem.publication_job_id,
                PublicationMediaItem.position,
            )
        ):
            carousel_items[media.publication_job_id].append(media)

    items = [
        p
        for p in db.scalars(select(Post).order_by(Post.updated_at.desc()))
        if require_visible(db, current, p.team_id)
    ]
    teams = {x.id: x for x in db.scalars(select(Team))}
    attention_statuses = {
        JobStatus.DRAFT,
        JobStatus.UNAPPROVED,
        JobStatus.FAILED,
        JobStatus.UNCERTAIN,
    }
    attention_count = len(overdue_jobs) + sum(
        job.approval_status != "approved"
        or job.status in attention_statuses
        or job.stale_time
        or bool(job.error)
        for job in planned_jobs
    )
    return render(
        request,
        "posts.html",
        current,
        items=items,
        teams=teams,
        published_jobs=published_jobs,
        overdue_jobs=overdue_jobs,
        planned_jobs=planned_jobs,
        calendar_posts=calendar_posts,
        games=games,
        pages=pages,
        carousel_items=carousel_items,
        future_days=days,
        publication_format=media_format,
        published_since=published_since,
        planned_until=planned_until,
        attention_count=attention_count,
        format_labels={"feed": "Feed", "story": "Story", "carousel": "Karussell"},
        status_labels={
            JobStatus.DRAFT: "Entwurf",
            JobStatus.UNAPPROVED: "Nicht freigegeben",
            JobStatus.APPROVED: "Freigegeben",
            JobStatus.SCHEDULED: "Geplant",
            JobStatus.WAITING: "Wartet",
            JobStatus.PUBLISHING: "Wird veröffentlicht",
            JobStatus.PUBLISHED: "Veröffentlicht",
            JobStatus.RETRY: "Wiederholung geplant",
            JobStatus.FAILED: "Fehlgeschlagen",
            JobStatus.CANCELLED: "Abgebrochen",
            JobStatus.SKIPPED: "Übersprungen",
            JobStatus.UNCERTAIN: "Manuell prüfen",
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
    pages = db.scalars(select(InstagramPage).where(InstagramPage.archived_at.is_(None))).all()
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
    can_edit_all = all(
        allowed(db, current, "edit_post", member.team_id) for member in bundle_posts
    )
    can_delete_all = all(
        allowed(db, current, "approve", member.team_id) for member in bundle_posts
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
    return render(
        request,
        "post_detail.html",
        current,
        item=item,
        jobs=jobs,
        pages=pages,
        checks=checks,
        carousel_items=carousel_items,
        late_jobs=late_jobs,
        logo_recompose=logo_recompose_availability(item, own_jobs),
        bundle_posts=bundle_posts,
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
        can_edit=can_edit_all,
        can_generate=not bundle_error
        and all(
            allowed(db, current, "generate", member.team_id) for member in bundle_posts
        ),
        can_approve=not bundle_error and can_delete_all,
        can_delete=can_delete_all,
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


@router.post("/posts/{post_id}/rerender")
def rerender_post_media(
    post_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    version: int = Form(),
    rerender_feed: bool = Form(default=False),
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
    if not rerender_feed and not story_job_ids:
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
    job = enqueue_rerender(
        db,
        item,
        current,
        version,
        story_job_ids,
        selected_media_asset_id,
        rerender_feed=rerender_feed,
    )
    return redirect(f"/generation-jobs/{job.id}", "Neurendern wurde eingereiht")


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
    story_job_ids: list[str] = Form(default=[]),
    media_asset_id: str = Form(default=""),
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
    # Keep accepting ``revise_graphics`` for already open legacy forms. New
    # forms select the feed and each story explicitly.
    revise_feed = revise_feed or revise_graphics
    has_graphics = revise_feed or bool(story_job_ids)
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
    if has_graphics and selected_media_asset_id != item.media_asset_id:
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
    try:
        job = enqueue_ai_revision(
            db,
            item,
            current,
            version,
            instruction,
            revise_text=revise_text,
            revise_graphics=has_graphics,
            revise_feed=revise_feed,
            story_job_ids=story_job_ids,
            media_asset_id=selected_media_asset_id,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return redirect(
        f"/generation-jobs/{job.id}",
        "KI-Änderungsauftrag wurde eingereiht",
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
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    item = db.get(Post, post_id)
    if not item:
        raise HTTPException(404)
    try:
        approve_matchday_bundle(db, item, current, job_ids or None)
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
def publications(request: Request, current=Depends(current_user), db: Session = Depends(get_db)):
    items = [
        j
        for j in db.scalars(select(PublicationJob).order_by(PublicationJob.scheduled_at.desc()))
        if require_visible(db, current, j.team_id)
    ]
    return render(
        request, "publications.html", current, items=items, title="Veröffentlichungsaufträge"
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
    return FileResponse(
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
    return FileResponse(
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
            )
        except LogoValidationError as exc:
            raise HTTPException(422, str(exc)) from exc
    elif action != "remove":
        raise HTTPException(422, "Unbekannte Logoaktion")
    if action == "select_shared" and selected:
        source = "shared_catalog_confirmed"
    elif selected and selected.normalized_name != normalize_club_name(opponent_name(game, team)):
        # Abweichende Schreibweisen sind erlaubt, aber nur nach dieser bewussten Auswahl.
        source = "manual_confirmed_non_exact"
    elif selected:
        source = "exact_name_confirmed"
    else:
        source = "removed"
    game.opponent_logo_id = selected.id if selected else None
    game.overrides = {
        **(game.overrides or {}),
        "opponent_logo_source": source,
        "opponent_logo_confirmed_by": current.id if selected else None,
        "opponent_logo_confirmed_at": datetime.now(timezone.utc).isoformat(),
    }
    game.version += 1
    affected = []
    if (old.id if old else None) != (selected.id if selected else None):
        reason = "Gegnerlogo wurde geändert; erneute Freigabe erforderlich"
        affected = _invalidate_posts_for_logo_change(db, game, reason)
        _audit_logo_approval_revocations(db, current, game.team_id, game.id, affected, reason)
    action_name = (
        "opponent_logo.removed"
        if not selected
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
            "affected_posts": affected,
        },
    )
    db.commit()
    return redirect(
        f"/games/{game.id}/opponent-logo",
        "Gegnerlogo-Zuordnung gespeichert",
    )


@router.get("/games", response_class=HTMLResponse)
def games(request: Request, current=Depends(current_user), db: Session = Depends(get_db)):
    teams = [
        t
        for t in db.scalars(select(Team).where(Team.archived_at.is_(None)))
        if require_visible(db, current, t.id)
    ]
    visible_games = [
        g
        for g in db.scalars(select(Game).order_by(Game.kickoff.desc()))
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
    team_map = {team.id: team for team in teams}
    logo_map = {
        game.id: db.get(LogoAsset, game.opponent_logo_id) if game.opponent_logo_id else None
        for game in items
    }
    opponents = {
        game.id: opponent_name(game, team_map[game.team_id])
        for game in items
        if game.team_id in team_map
    }
    game_groups = dashboard_game_groups(db, items, team_map)
    return render(
        request,
        "games.html",
        current,
        teams=teams,
        items=items,
        logo_map=logo_map,
        opponents=opponents,
        game_groups=game_groups,
        suppressed_items=suppressed_items,
        title="Spiele und Testdaten",
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
    return redirect("/games", "Spiele wurden bewusst zu einem Auftrag verbunden")


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
    return redirect("/games", "Spiele wurden bewusst getrennt")


@router.post("/games/mock")
def create_mock_game(
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    team_id: str = Form(),
    opponent: str = Form(),
    side: str = Form(),
    kickoff: str = Form(),
    competition: str = Form(default=""),
    venue: str = Form(default=""),
    pitch: str = Form(default=""),
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
        raise HTTPException(409, "Dieses Testspiel existiert bereits") from e
    audit(db, current, "game.mock_created", "game", item.id, team_id)
    db.commit()
    return redirect("/games", "Lokales Testspiel angelegt")


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
    return redirect("/games", "Lokales Testspiel gelöscht")


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
        raise HTTPException(422, str(e)) from e
    if post:
        return redirect(f"/posts/{post.id}", "Vorhandener Beitrag geöffnet")
    return redirect(f"/generation-jobs/{job.id}", "Generierung wurde eingereiht")


@router.get("/generation-jobs", response_class=HTMLResponse)
def generation_jobs(request: Request, current=Depends(current_user), db: Session = Depends(get_db)):
    items = [
        item
        for item in db.scalars(
            select(GenerationJob).order_by(GenerationJob.created_at.desc()).limit(200)
        )
        if require_visible(db, current, item.team_id)
    ]
    teams = {item.id: item for item in db.scalars(select(Team))}
    games_map = {item.id: item for item in db.scalars(select(Game))}
    return render(
        request,
        "generation_jobs.html",
        current,
        items=items,
        teams=teams,
        games=games_map,
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
        retry_job(db, item)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return redirect(f"/generation-jobs/{item.id}", "Auftrag wurde bewusst erneut eingereiht")


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
    from fastapi.responses import FileResponse

    from app.models import ProviderSnapshot

    require_admin(current)
    snapshot = db.get(ProviderSnapshot, snapshot_id)
    if not snapshot:
        raise HTTPException(404)
    root = settings.provider_snapshot_root.resolve()
    path = (root / snapshot.relative_path).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(404, "Snapshot-Datei fehlt oder ist unvollständig")
    return FileResponse(
        path, media_type="text/html", filename=f"fussball-{snapshot.checksum[:12]}.html"
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
    from fastapi.responses import FileResponse

    item = db.get(Post, post_id)
    job = db.get(PublicationJob, job_id)
    if not item or not job or job.post_id != item.id:
        raise HTTPException(404)
    require(current, db, "view", item.team_id)
    path = Path(job.media_path).resolve()
    root = settings.generated_root.resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(404, "Grafik fehlt")
    return FileResponse(path, media_type="image/png")


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
    return FileResponse(path, media_type="image/png")


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
