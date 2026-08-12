"""Tenant-safe media library policies, preferences and reservations."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    ClubMediaUsagePolicy,
    Game,
    GameMediaPreference,
    MediaAsset,
    MediaUsageHistory,
    Post,
    Team,
)

MEDIA_CATEGORIES = ("match_photo", "player_portrait", "team_photo")
CONTRIBUTION_TYPES = ("announcement", "reminder", "result", "live")

MEDIA_CATEGORY_LABELS = {
    "match_photo": "Spielbild",
    "player_portrait": "Einzelfoto",
    "team_photo": "Mannschaftsfoto",
}

CONTRIBUTION_TYPE_LABELS = {
    "announcement": "Spielankündigung",
    "reminder": "Spielerinnerung",
    "result": "Ergebnismeldung",
    "live": "Live Center",
}

SAFE_DEFAULT_POLICIES = {
    "announcement": ["match_photo"],
    "reminder": ["match_photo"],
    "result": ["match_photo"],
    "live": ["player_portrait", "match_photo"],
}


class MediaLibraryError(ValueError):
    """A user-facing media library validation failure."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def validate_category(value: str) -> str:
    category = (value or "").strip()
    if category not in MEDIA_CATEGORIES:
        raise MediaLibraryError("Unbekannte Medienkategorie")
    return category


def validate_contribution_type(value: str) -> str:
    contribution_type = (value or "").strip()
    if contribution_type not in CONTRIBUTION_TYPES:
        raise MediaLibraryError("Unbekannter Beitragstyp")
    return contribution_type


def normalize_policy_categories(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        category = validate_category(value)
        if category not in result:
            result.append(category)
    if not result:
        raise MediaLibraryError("Mindestens eine Medienkategorie muss erlaubt sein")
    return result


def effective_policy(db: Session, club_id: str, contribution_type: str) -> list[str]:
    """Return allowed categories in effective selection order."""

    contribution_type = validate_contribution_type(contribution_type)
    policy = db.scalar(
        select(ClubMediaUsagePolicy).where(
            ClubMediaUsagePolicy.club_id == club_id,
            ClubMediaUsagePolicy.contribution_type == contribution_type,
            ClubMediaUsagePolicy.active.is_(True),
        )
    )
    if not policy:
        return list(SAFE_DEFAULT_POLICIES[contribution_type])
    allowed = [item for item in (policy.allowed_media_categories or []) if item in MEDIA_CATEGORIES]
    if not allowed:
        return list(SAFE_DEFAULT_POLICIES[contribution_type])
    ordered = [item for item in (policy.category_priority or []) if item in allowed]
    return [*dict.fromkeys([*ordered, *allowed])]


def save_policy(
    db: Session,
    *,
    club_id: str,
    contribution_type: str,
    allowed_categories: list[str],
    primary_category: str | None,
    actor_user_id: str | None,
) -> ClubMediaUsagePolicy:
    contribution_type = validate_contribution_type(contribution_type)
    allowed = normalize_policy_categories(allowed_categories)
    if primary_category:
        primary_category = validate_category(primary_category)
        if primary_category not in allowed:
            raise MediaLibraryError("Die bevorzugte Kategorie muss zugleich erlaubt sein")
    priority = [*([primary_category] if primary_category else []), *allowed]
    priority = list(dict.fromkeys(priority))
    statement = select(ClubMediaUsagePolicy).where(
        ClubMediaUsagePolicy.club_id == club_id,
        ClubMediaUsagePolicy.contribution_type == contribution_type,
    )
    if db.bind and db.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    policy = db.scalar(statement)
    if policy is None:
        policy = ClubMediaUsagePolicy(
            club_id=club_id,
            contribution_type=contribution_type,
        )
        db.add(policy)
    else:
        policy.version += 1
    policy.allowed_media_categories = allowed
    policy.category_priority = priority
    policy.active = True
    policy.updated_by = actor_user_id
    db.flush()
    return policy


def usage_status(asset: MediaAsset) -> str:
    if asset.deleted_at is not None:
        return "deleted"
    if not asset.available:
        return "missing"
    if asset.reserved_game_id:
        return "reserved"
    if asset.uses > 0:
        return "used"
    if not asset.automatic_usage_enabled or not asset.active:
        return "excluded"
    return "available"


def add_history(
    db: Session,
    asset: MediaAsset,
    action: str,
    *,
    game_id: str | None = None,
    post_id: str | None = None,
    contribution_type: str | None = None,
    actor_user_id: str | None = None,
    details: dict | None = None,
) -> MediaUsageHistory:
    item = MediaUsageHistory(
        club_id=asset.club_id,
        media_asset_id=asset.id,
        team_id=asset.team_id,
        game_id=game_id,
        post_id=post_id,
        contribution_type=contribution_type,
        action=action,
        actor_user_id=actor_user_id,
        details=details or {},
    )
    db.add(item)
    return item


def set_game_preference(
    db: Session,
    *,
    club_id: str,
    team_id: str,
    game_id: str,
    contribution_type: str,
    selection_mode: str,
    selected_media_asset_id: str | None,
    allow_used_once: bool,
    actor_user_id: str | None,
) -> GameMediaPreference:
    contribution_type = validate_contribution_type(contribution_type)
    if selection_mode not in {"automatic", "manual"}:
        raise MediaLibraryError("Unbekannte Bildauswahl")
    game = db.scalar(
        select(Game).where(
            Game.id == game_id,
            Game.club_id == club_id,
            Game.team_id == team_id,
        )
    )
    team = db.scalar(select(Team).where(Team.id == team_id, Team.club_id == club_id))
    if not game or not team:
        raise MediaLibraryError("Spiel oder Mannschaft gehört nicht zu diesem Verein")

    asset: MediaAsset | None = None
    if selection_mode == "manual":
        if not selected_media_asset_id:
            raise MediaLibraryError("Bitte ein Bild auswählen")
        asset = db.scalar(
            select(MediaAsset).where(
                MediaAsset.id == selected_media_asset_id,
                MediaAsset.club_id == club_id,
                MediaAsset.team_id == team_id,
            )
        )
        if not asset or asset.deleted_at is not None or not asset.available:
            raise MediaLibraryError("Das gewählte Bild ist nicht verfügbar")
        if asset.reserved_game_id not in {None, game_id}:
            raise MediaLibraryError(
                "Das gewählte Bild ist bereits für ein anderes Spiel reserviert"
            )
        if asset.uses > 0 and not allow_used_once:
            raise MediaLibraryError(
                "Das Bild wurde bereits verwendet. Bitte die einmalige Wiederverwendung bestätigen."
            )

    statement = select(GameMediaPreference).where(
        GameMediaPreference.club_id == club_id,
        GameMediaPreference.game_id == game_id,
        GameMediaPreference.team_id == team_id,
        GameMediaPreference.contribution_type == contribution_type,
    )
    if db.bind and db.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    preference = db.scalar(statement)
    previous_asset_id = preference.selected_media_asset_id if preference else None
    if preference is None:
        preference = GameMediaPreference(
            club_id=club_id,
            game_id=game_id,
            team_id=team_id,
            contribution_type=contribution_type,
        )
        db.add(preference)
    else:
        preference.version += 1

    new_asset_id = asset.id if asset else None
    if previous_asset_id and previous_asset_id != new_asset_id:
        previous_asset = _locked_asset(db, previous_asset_id)
        if previous_asset and previous_asset.club_id == club_id:
            already_used = db.scalar(
                select(MediaUsageHistory.id).where(
                    MediaUsageHistory.club_id == club_id,
                    MediaUsageHistory.media_asset_id == previous_asset.id,
                    MediaUsageHistory.game_id == game_id,
                    MediaUsageHistory.contribution_type == contribution_type,
                    MediaUsageHistory.action == "used",
                )
            )
            if previous_asset.reserved_game_id == game_id and not already_used:
                previous_asset.reserved_game_id = None
                previous_asset.uses = max(0, previous_asset.uses - 1)
                add_history(
                    db,
                    previous_asset,
                    "reservation_released",
                    game_id=game_id,
                    contribution_type=contribution_type,
                    actor_user_id=actor_user_id,
                    details={"reason": "game_preference_changed"},
                )

    preference.selection_mode = selection_mode
    preference.selected_media_asset_id = new_asset_id
    preference.allow_used_once = bool(allow_used_once and asset)
    preference.selected_by = actor_user_id
    preference.selected_at = _now()
    db.flush()
    return preference


def _locked_asset(db: Session, asset_id: str) -> MediaAsset | None:
    statement = select(MediaAsset).where(MediaAsset.id == asset_id)
    if db.bind and db.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    return db.scalar(statement)


def reserve_media(
    db: Session,
    *,
    club_id: str,
    team_id: str,
    game_id: str,
    contribution_type: str,
    actor_user_id: str | None = None,
) -> MediaAsset | None:
    """Atomically reserve the explicit choice or an eligible policy candidate."""

    contribution_type = validate_contribution_type(contribution_type)
    preference_statement = select(GameMediaPreference).where(
        GameMediaPreference.club_id == club_id,
        GameMediaPreference.game_id == game_id,
        GameMediaPreference.team_id == team_id,
        GameMediaPreference.contribution_type == contribution_type,
    )
    if db.bind and db.bind.dialect.name == "postgresql":
        preference_statement = preference_statement.with_for_update()
    preference = db.scalar(preference_statement)

    if preference and preference.selected_media_asset_id:
        asset = _locked_asset(db, preference.selected_media_asset_id)
        if preference.selection_mode == "manual":
            if not asset or asset.club_id != club_id or asset.team_id != team_id:
                raise MediaLibraryError(
                    "Die gespeicherte Bildauswahl gehört nicht zu diesem Verein"
                )
            if asset.deleted_at is not None or not asset.available:
                raise MediaLibraryError("Das bewusst gewählte Bild ist nicht mehr verfügbar")
            if asset.reserved_game_id == game_id:
                return asset
            if asset.reserved_game_id is not None:
                raise MediaLibraryError(
                    "Das bewusst gewählte Bild ist für ein anderes Spiel reserviert"
                )
            if asset.uses > 0 and not preference.allow_used_once:
                raise MediaLibraryError("Das bewusst gewählte Bild wurde bereits verwendet")
            asset.reserved_game_id = game_id
            asset.uses += 1
            add_history(
                db,
                asset,
                "manual_reuse" if asset.uses > 1 else "reserved",
                game_id=game_id,
                contribution_type=contribution_type,
                actor_user_id=actor_user_id,
                details={"selection_mode": "manual"},
            )
            db.flush()
            return asset

        # Automatic preferences remember the asset chosen for a running job so
        # retries stay deterministic.  After a completed/deleted contribution,
        # however, that pointer can legitimately refer to an already consumed,
        # disabled or otherwise unavailable asset.  Such a pointer is not a
        # deliberate user choice and must not block the next automatic run.
        automatic_asset_is_eligible = bool(
            asset
            and asset.club_id == club_id
            and asset.team_id == team_id
            and asset.deleted_at is None
            and asset.available
            and asset.active
            and asset.automatic_usage_enabled
        )
        if automatic_asset_is_eligible and asset.reserved_game_id == game_id:
            return asset
        if automatic_asset_is_eligible and asset.reserved_game_id is None and asset.uses == 0:
            asset.reserved_game_id = game_id
            asset.uses += 1
            add_history(
                db,
                asset,
                "reserved",
                game_id=game_id,
                contribution_type=contribution_type,
                actor_user_id=actor_user_id,
                details={"selection_mode": "automatic", "source": "saved_preference"},
            )
            db.flush()
            return asset

        if asset and asset.club_id == club_id and asset.reserved_game_id == game_id:
            already_used = db.scalar(
                select(MediaUsageHistory.id).where(
                    MediaUsageHistory.club_id == club_id,
                    MediaUsageHistory.media_asset_id == asset.id,
                    MediaUsageHistory.game_id == game_id,
                    MediaUsageHistory.contribution_type == contribution_type,
                    MediaUsageHistory.action == "used",
                )
            )
            if not already_used:
                asset.reserved_game_id = None
                asset.uses = max(0, asset.uses - 1)
                add_history(
                    db,
                    asset,
                    "reservation_released",
                    game_id=game_id,
                    contribution_type=contribution_type,
                    actor_user_id=actor_user_id,
                    details={"reason": "stale_automatic_preference"},
                )
        preference.selected_media_asset_id = None
        preference.allow_used_once = False
        preference.selected_at = _now()
        db.flush()

    # An older reservation created before the per-contribution preference model
    # remains stable for retries and is adopted into the new preference table.
    legacy = db.scalar(
        select(MediaAsset).where(
            MediaAsset.club_id == club_id,
            MediaAsset.team_id == team_id,
            MediaAsset.reserved_game_id == game_id,
            MediaAsset.deleted_at.is_(None),
        )
    )
    if legacy:
        already_adopted = db.scalar(
            select(GameMediaPreference.id).where(
                GameMediaPreference.club_id == club_id,
                GameMediaPreference.game_id == game_id,
                GameMediaPreference.selected_media_asset_id == legacy.id,
                GameMediaPreference.contribution_type != contribution_type,
            )
        )
        matching_history = db.scalar(
            select(MediaUsageHistory.id).where(
                MediaUsageHistory.club_id == club_id,
                MediaUsageHistory.media_asset_id == legacy.id,
                MediaUsageHistory.game_id == game_id,
                MediaUsageHistory.contribution_type == contribution_type,
            )
        )
        if already_adopted and not matching_history:
            legacy = None
        elif preference is None:
            preference = GameMediaPreference(
                club_id=club_id,
                game_id=game_id,
                team_id=team_id,
                contribution_type=contribution_type,
                selection_mode="automatic",
            )
            db.add(preference)
        if legacy:
            preference.selected_media_asset_id = legacy.id
            preference.selected_at = _now()
            db.flush()
            return legacy

    categories = effective_policy(db, club_id, contribution_type)
    asset = None
    for category in categories:
        statement = (
            select(MediaAsset)
            .where(
                MediaAsset.club_id == club_id,
                MediaAsset.team_id == team_id,
                MediaAsset.media_category == category,
                MediaAsset.active.is_(True),
                MediaAsset.automatic_usage_enabled.is_(True),
                MediaAsset.available.is_(True),
                MediaAsset.deleted_at.is_(None),
                MediaAsset.reserved_game_id.is_(None),
                MediaAsset.uses == 0,
            )
            .order_by(func.random())
            .limit(1)
        )
        if db.bind and db.bind.dialect.name == "postgresql":
            statement = statement.with_for_update(skip_locked=True)
        asset = db.scalar(statement)
        if asset:
            break
    if not asset:
        return None

    if preference is None:
        preference = GameMediaPreference(
            club_id=club_id,
            game_id=game_id,
            team_id=team_id,
            contribution_type=contribution_type,
            selection_mode="automatic",
        )
        db.add(preference)
    preference.selected_media_asset_id = asset.id
    preference.selected_at = _now()
    asset.reserved_game_id = game_id
    asset.uses += 1
    add_history(
        db,
        asset,
        "reserved",
        game_id=game_id,
        contribution_type=contribution_type,
        actor_user_id=actor_user_id,
        details={"selection_mode": "automatic", "category": asset.media_category},
    )
    db.flush()
    return asset


def mark_asset_used(
    db: Session,
    asset: MediaAsset,
    *,
    game_id: str,
    post_id: str | None,
    contribution_type: str,
    actor_user_id: str | None = None,
) -> None:
    """Finalize one reservation exactly once after usable output exists."""

    existing = db.scalar(
        select(MediaUsageHistory.id).where(
            MediaUsageHistory.club_id == asset.club_id,
            MediaUsageHistory.media_asset_id == asset.id,
            MediaUsageHistory.post_id == post_id,
            MediaUsageHistory.action == "used",
        )
    )
    if existing:
        return
    if asset.reserved_game_id == game_id:
        asset.reserved_game_id = None
    asset.automatic_usage_enabled = False
    add_history(
        db,
        asset,
        "used",
        game_id=game_id,
        post_id=post_id,
        contribution_type=contribution_type,
        actor_user_id=actor_user_id,
        details={"technically_usable_output": True},
    )


def set_automatic_usage(
    db: Session,
    asset: MediaAsset,
    *,
    enabled: bool,
    actor_user_id: str | None,
) -> None:
    if enabled and (not asset.available or asset.deleted_at is not None):
        raise MediaLibraryError("Eine fehlende oder gelöschte Datei kann nicht freigegeben werden")
    asset.automatic_usage_enabled = enabled
    asset.active = enabled
    add_history(
        db,
        asset,
        "automatic_enabled" if enabled else "automatic_excluded",
        actor_user_id=actor_user_id,
    )


def release_asset(
    db: Session,
    asset: MediaAsset,
    *,
    actor_user_id: str | None,
) -> None:
    previous_game = asset.reserved_game_id
    asset.reserved_game_id = None
    asset.uses = 0
    asset.automatic_usage_enabled = True
    asset.active = True
    add_history(
        db,
        asset,
        "released",
        game_id=previous_game,
        actor_user_id=actor_user_id,
        details={"scope": "global", "historical_usage_preserved": True},
    )


def soft_delete_asset(
    db: Session,
    asset: MediaAsset,
    *,
    actor_user_id: str | None,
) -> bool:
    if asset.reserved_game_id:
        raise MediaLibraryError(
            "Das Bild ist für einen Beitrag reserviert und kann erst nach Freigabe gelöscht werden"
        )
    referenced_post_count = int(
        db.scalar(
            select(Post.id)
            .where(
                Post.club_id == asset.club_id,
                Post.media_asset_id == asset.id,
            )
            .limit(1)
        )
        is not None
    )
    preferences = list(
        db.scalars(
            select(GameMediaPreference).where(
                GameMediaPreference.club_id == asset.club_id,
                GameMediaPreference.selected_media_asset_id == asset.id,
            )
        )
    )
    active_preferences: list[GameMediaPreference] = []
    for preference in preferences:
        consumed = db.scalar(
            select(MediaUsageHistory.id).where(
                MediaUsageHistory.club_id == asset.club_id,
                MediaUsageHistory.media_asset_id == asset.id,
                MediaUsageHistory.game_id == preference.game_id,
                MediaUsageHistory.contribution_type == preference.contribution_type,
                MediaUsageHistory.action == "used",
            )
        )
        if not consumed:
            active_preferences.append(preference)
    if active_preferences:
        raise MediaLibraryError(
            "Das Bild ist noch bewusst f\u00fcr ein Spiel ausgew\u00e4hlt. "
            "Hebe diese Zuordnung zuerst in der Bildauswahl des Spiels auf."
        )

    # Keep the source object and all historical post/media-version references.
    # Only consumed preferences may be detached. Active future choices are
    # blocked above and are never removed silently.
    for preference in preferences:
        preference.selection_mode = "automatic"
        preference.selected_media_asset_id = None
        preference.allow_used_once = False
        preference.selected_by = actor_user_id
        preference.selected_at = _now()
        preference.version += 1
    remove_source_file = bool(
        asset.storage_kind == "upload"
        and not referenced_post_count
        and not preferences
        and asset.uses <= 0
    )
    asset.deleted_at = _now()
    asset.automatic_usage_enabled = False
    asset.active = False
    if remove_source_file:
        asset.available = False
    add_history(
        db,
        asset,
        "soft_deleted",
        actor_user_id=actor_user_id,
        details={
            "historical_post_reference_preserved": bool(referenced_post_count),
            "cleared_game_preferences": len(preferences),
            "source_file_removed": remove_source_file,
        },
    )
    return remove_source_file
