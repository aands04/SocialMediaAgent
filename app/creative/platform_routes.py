from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.creative.flags import (
    CREATIVE_FLAG,
    DEFAULT_CREATIVE_SETTINGS,
    DEFAULT_ONBOARDING_SETTINGS,
    ONBOARDING_FLAG,
    creative_feature,
)
from app.creative.onboarding import get_or_create_session, restart_calibration
from app.creative.service import (
    rebuild_all_profiles,
    reset_profiles,
    restore_profile_version,
)
from app.creative.types import normalize_content_type, normalize_modality, normalize_traits
from app.db import get_db
from app.models import (
    Club,
    ClubOnboardingSession,
    CreativeExampleReference,
    CreativeFeedbackEvent,
    CreativePreferenceProfile,
    CreativeProfileOverride,
    CreativeRecipe,
    FeatureFlag,
    UsageLedgerEntry,
    User,
    uid,
)
from app.platform.service import platform_audit, set_feature_flag
from app.tenancy.context import TenantContext
from app.web import check_csrf, csrf_token, current_user, require_platform_admin

router = APIRouter(prefix="/platform/creative-intelligence")
templates = Jinja2Templates(directory="app/templates")


def _render(request: Request, name: str, current: User, **context):
    return templates.TemplateResponse(
        request,
        name,
        {
            "user": current,
            "csrf": csrf_token(request),
            "platform_area": True,
            **context,
        },
    )


def _redirect(path: str, notice: str) -> RedirectResponse:
    return RedirectResponse(f"{path}?notice={quote_plus(notice)}", status_code=303)


def _trait_map(payload: dict | None) -> dict[str, str]:
    result: dict[str, str] = {}
    traits = (payload or {}).get("traits", [])
    if isinstance(traits, dict):
        return {str(key): str(value) for key, value in traits.items()}
    for item in traits if isinstance(traits, list) else []:
        if isinstance(item, dict) and item.get("key") and item.get("value") is not None:
            result[str(item["key"])] = str(item["value"])
    return result


def _trait_payload(values: dict[str, str]) -> dict:
    return {
        "traits": [
            {"key": key, "value": value}
            for key, value in sorted(values.items())
        ]
    }


def _profile_comparison(
    left: CreativePreferenceProfile | None,
    right: CreativePreferenceProfile | None,
) -> dict | None:
    if left is None or right is None:
        return None
    left_values = {
        ("prefer", str(item.get("key")), str(item.get("value"))): float(
            item.get("score") or 0
        )
        for item in (left.preferences or {}).get("traits", [])
        if isinstance(item, dict)
    }
    left_values.update(
        {
            ("avoid", str(item.get("key")), str(item.get("value"))): float(item.get("score") or 0)
            for item in (left.avoidances or {}).get("traits", [])
            if isinstance(item, dict)
        }
    )
    right_values = {
        ("prefer", str(item.get("key")), str(item.get("value"))): float(item.get("score") or 0)
        for item in (right.preferences or {}).get("traits", [])
        if isinstance(item, dict)
    }
    right_values.update(
        {
            ("avoid", str(item.get("key")), str(item.get("value"))): float(item.get("score") or 0)
            for item in (right.avoidances or {}).get("traits", [])
            if isinstance(item, dict)
        }
    )
    return {
        "left": left,
        "right": right,
        "added": sorted(set(right_values) - set(left_values)),
        "removed": sorted(set(left_values) - set(right_values)),
        "changed": sorted(
            (
                key,
                round(left_values[key], 4),
                round(right_values[key], 4),
            )
            for key in set(left_values) & set(right_values)
            if left_values[key] != right_values[key]
        ),
    }


@router.get("", response_class=HTMLResponse)
def overview(
    request: Request,
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    require_platform_admin(current)
    club_rows = list(db.scalars(select(Club).order_by(Club.name)))
    clubs: list[dict] = []
    for club in club_rows:
        feature = creative_feature(db, club.id)
        clubs.append(
            {
                "club": club,
                "feature": feature,
                "feedback_count": int(
                    db.scalar(
                        select(func.count()).select_from(CreativeFeedbackEvent).where(
                            CreativeFeedbackEvent.club_id == club.id
                        )
                    )
                    or 0
                ),
                "profile_count": int(
                    db.scalar(
                        select(func.count()).select_from(CreativePreferenceProfile).where(
                            CreativePreferenceProfile.club_id == club.id,
                            CreativePreferenceProfile.status == "active",
                        )
                    )
                    or 0
                ),
            }
        )
    global_flags = {
        key: db.scalar(
            select(FeatureFlag).where(FeatureFlag.club_id.is_(None), FeatureFlag.key == key)
        )
        for key in (CREATIVE_FLAG, ONBOARDING_FLAG)
    }
    total_feedback = int(
        db.scalar(select(func.count()).select_from(CreativeFeedbackEvent)) or 0
    )
    active_clubs = sum(1 for row in clubs if row["feature"].enabled)
    total_usage = list(
        db.execute(
            select(
                UsageLedgerEntry.generation_type,
                func.count(UsageLedgerEntry.id),
                func.coalesce(func.sum(UsageLedgerEntry.provider_cost), 0),
            )
            .where(
                UsageLedgerEntry.generation_type.in_(
                    (
                        "creative_director",
                        "preference_learning",
                        "visual_trait_analysis",
                        "onboarding_calibration",
                    )
                )
            )
            .group_by(UsageLedgerEntry.generation_type)
        )
    )
    recipes = list(
        db.scalars(
            select(CreativeRecipe).order_by(
                CreativeRecipe.modality,
                CreativeRecipe.content_type,
                CreativeRecipe.key,
                CreativeRecipe.recipe_version.desc(),
            )
        )
    )
    return _render(
        request,
        "creative/platform_overview.html",
        current,
        clubs=clubs,
        global_flags=global_flags,
        active_clubs=active_clubs,
        total_feedback=total_feedback,
        total_usage=total_usage,
        recipes=recipes,
    )


@router.post("/recipes")
def create_recipe(
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    key: str = Form(),
    name: str = Form(),
    description: str = Form(default=""),
    modality: str = Form(),
    content_type: str = Form(),
    trait: str = Form(),
    value: str = Form(),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Create an immutable draft recipe version.

    Recipe input is deliberately limited to the same controlled trait
    vocabulary as club preferences.  Platform administrators never enter raw
    prompt fragments here.
    """

    check_csrf(request, csrf_token_value)
    require_platform_admin(current)
    normalized_key = key.strip().casefold().replace(" ", "-")[:100]
    if not normalized_key or not all(
        character.isalnum() or character in {"-", "_"}
        for character in normalized_key
    ):
        raise HTTPException(422, "Ungültiger Rezeptschlüssel")
    normalized_name = " ".join(name.split()).strip()[:160]
    if not normalized_name:
        raise HTTPException(422, "Rezeptname fehlt")
    normalized_modality = normalize_modality(modality)
    normalized_content = normalize_content_type(normalized_modality, content_type)
    traits = normalize_traits({trait: value})
    next_version = int(
        db.scalar(
            select(func.coalesce(func.max(CreativeRecipe.recipe_version), 0)).where(
                CreativeRecipe.key == normalized_key
            )
        )
        or 0
    ) + 1
    recipe = CreativeRecipe(
        id=uid(),
        key=normalized_key,
        name=normalized_name,
        description=" ".join(description.split()).strip()[:2000] or None,
        modality=normalized_modality,
        content_type=normalized_content,
        recipe_version=next_version,
        status="draft",
        traits=traits,
        constraints={},
        created_by=current.id,
    )
    db.add(recipe)
    db.flush()
    platform_audit(
        db,
        current,
        "creative.recipe_created",
        "creative_recipe",
        recipe.id,
        {
            "key": recipe.key,
            "version": recipe.recipe_version,
            "modality": recipe.modality,
            "content_type": recipe.content_type,
        },
    )
    db.commit()
    return _redirect("/platform/creative-intelligence", "Rezeptentwurf versioniert gespeichert")


@router.post("/recipes/{recipe_id}/activate")
def activate_recipe(
    recipe_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_platform_admin(current)
    recipe = db.get(CreativeRecipe, recipe_id)
    if recipe is None:
        raise HTTPException(404)
    active_recipes = list(
        db.scalars(
            select(CreativeRecipe).where(
                CreativeRecipe.modality == recipe.modality,
                CreativeRecipe.content_type == recipe.content_type,
                CreativeRecipe.status == "active",
            )
        )
    )
    for active in active_recipes:
        active.status = "archived"
    recipe.status = "active"
    recipe.activated_at = datetime.now(timezone.utc)
    platform_audit(
        db,
        current,
        "creative.recipe_activated",
        "creative_recipe",
        recipe.id,
        {"key": recipe.key, "version": recipe.recipe_version},
    )
    db.commit()
    return _redirect("/platform/creative-intelligence", "Rezept aktiviert")


@router.post("/recipes/{recipe_id}/archive")
def archive_recipe(
    recipe_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_platform_admin(current)
    recipe = db.get(CreativeRecipe, recipe_id)
    if recipe is None:
        raise HTTPException(404)
    recipe.status = "archived"
    platform_audit(
        db,
        current,
        "creative.recipe_archived",
        "creative_recipe",
        recipe.id,
        {"key": recipe.key, "version": recipe.recipe_version},
    )
    db.commit()
    return _redirect("/platform/creative-intelligence", "Rezept archiviert")


@router.post("/flags")
def flags(
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    creative_enabled: bool = Form(default=False),
    onboarding_enabled: bool = Form(default=False),
    default_clubs_enabled: bool = Form(default=False),
    announcement_image_count: int = Form(default=4),
    result_image_count: int = Form(default=4),
    announcement_text_count: int = Form(default=3),
    result_text_count: int = Form(default=3),
    minimum_feedback_events: int = Form(default=5),
    positive_example_limit: int = Form(default=5),
    negative_example_limit: int = Form(default=3),
    preference_recency_weight: float = Form(default=0.985),
    vision_analysis_enabled: bool = Form(default=False),
    onboarding_cost_limit_cents: int = Form(default=0),
    confidence_first_tendency: float = Form(default=0.35),
    confidence_well_trained: float = Form(default=0.55),
    confidence_high: float = Form(default=0.8),
    preference_learning_model: str = Form(default="deterministic-v1"),
    creative_director_model: str = Form(default="structured-v1"),
    visual_trait_analysis_model: str = Form(default=""),
    feedback_sampling_rate: float = Form(default=1.0),
    profile_rebuild_schedule: str = Form(default="nightly"),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_platform_admin(current)
    creative_values = dict(DEFAULT_CREATIVE_SETTINGS)
    creative_values.update(
        {
            "default_clubs_enabled": default_clubs_enabled,
            "minimum_samples": max(1, min(100, minimum_feedback_events)),
            "positive_example_limit": max(1, min(10, positive_example_limit)),
            "negative_example_limit": max(0, min(10, negative_example_limit)),
            "preference_recency_weight": max(0.5, min(1.0, preference_recency_weight)),
            "vision_analysis_enabled": vision_analysis_enabled,
            "preference_learning_model": preference_learning_model.strip()[:120]
            or "deterministic-v1",
            "creative_director_model": creative_director_model.strip()[:120]
            or "structured-v1",
            "visual_trait_analysis_model": visual_trait_analysis_model.strip()[:120],
            "feedback_sampling_rate": max(0.0, min(1.0, feedback_sampling_rate)),
            "profile_rebuild_schedule": (
                profile_rebuild_schedule.strip().casefold()
                if profile_rebuild_schedule.strip().casefold()
                in {"disabled", "hourly", "nightly"}
                else "nightly"
            ),
            "confidence_thresholds": {
                "first_tendencies": max(0.0, min(1.0, confidence_first_tendency)),
                "well_trained": max(0.0, min(1.0, confidence_well_trained)),
                "high_confidence": max(0.0, min(1.0, confidence_high)),
            },
        }
    )
    onboarding_values = dict(DEFAULT_ONBOARDING_SETTINGS)
    onboarding_values.update(
        {
            "default_clubs_enabled": default_clubs_enabled,
            "announcement_image_count": max(2, min(6, announcement_image_count)),
            "result_image_count": max(2, min(6, result_image_count)),
            "announcement_text_count": max(2, min(6, announcement_text_count)),
            "result_text_count": max(2, min(6, result_text_count)),
            "onboarding_cost_limit_cents": max(0, onboarding_cost_limit_cents),
        }
    )
    set_feature_flag(
        db,
        current,
        key=CREATIVE_FLAG,
        enabled=creative_enabled,
        value=creative_values,
    )
    set_feature_flag(
        db,
        current,
        key=ONBOARDING_FLAG,
        enabled=onboarding_enabled,
        value=onboarding_values,
    )
    db.commit()
    return _redirect("/platform/creative-intelligence", "Plattformschalter gespeichert")


@router.get("/clubs/{club_id}", response_class=HTMLResponse)
def club_detail(
    club_id: str,
    request: Request,
    compare_left: str | None = Query(default=None),
    compare_right: str | None = Query(default=None),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    require_platform_admin(current)
    club = db.get(Club, club_id)
    if club is None:
        raise HTTPException(404)
    profiles = list(
        db.scalars(
            select(CreativePreferenceProfile)
            .where(CreativePreferenceProfile.club_id == club.id)
            .order_by(
                CreativePreferenceProfile.modality,
                CreativePreferenceProfile.content_type,
                CreativePreferenceProfile.profile_version.desc(),
            )
        )
    )
    overrides = list(
        db.scalars(
            select(CreativeProfileOverride)
            .where(CreativeProfileOverride.club_id == club.id)
            .order_by(CreativeProfileOverride.created_at.desc())
        )
    )
    feedback = list(
        db.scalars(
            select(CreativeFeedbackEvent)
            .where(CreativeFeedbackEvent.club_id == club.id)
            .order_by(CreativeFeedbackEvent.occurred_at.desc())
            .limit(100)
        )
    )
    internal_usage = list(
        db.scalars(
            select(UsageLedgerEntry)
            .where(
                UsageLedgerEntry.club_id == club.id,
                UsageLedgerEntry.generation_type.in_(
                    (
                        "creative_director",
                        "preference_learning",
                        "visual_trait_analysis",
                        "onboarding_calibration",
                    )
                ),
            )
            .order_by(UsageLedgerEntry.created_at.desc())
            .limit(100)
        )
    )
    session = db.scalar(
        select(ClubOnboardingSession).where(ClubOnboardingSession.club_id == club.id)
    )
    references = list(
        db.scalars(
            select(CreativeExampleReference)
            .where(
                CreativeExampleReference.club_id == club.id,
                CreativeExampleReference.active.is_(True),
            )
            .order_by(
                CreativeExampleReference.sentiment,
                CreativeExampleReference.score.desc(),
                CreativeExampleReference.rank.asc(),
            )
            .limit(100)
        )
    )
    feedback_stats: dict[tuple[str, str, str], int] = {}
    for modality, content_type, action, count in db.execute(
        select(
            CreativeFeedbackEvent.modality,
            CreativeFeedbackEvent.content_type,
            CreativeFeedbackEvent.action,
            func.count(CreativeFeedbackEvent.id),
        )
        .where(CreativeFeedbackEvent.club_id == club.id)
        .group_by(
            CreativeFeedbackEvent.modality,
            CreativeFeedbackEvent.content_type,
            CreativeFeedbackEvent.action,
        )
    ):
        feedback_stats[(modality, content_type, action)] = int(count)
    usage_stats = [
        {
            "generation_type": generation_type,
            "calls": int(calls),
            "quantity": int(quantity or 0),
            "cost": cost or 0,
        }
        for generation_type, calls, quantity, cost in db.execute(
            select(
                UsageLedgerEntry.generation_type,
                func.count(UsageLedgerEntry.id),
                func.coalesce(func.sum(UsageLedgerEntry.actual_quantity), 0),
                func.coalesce(func.sum(UsageLedgerEntry.provider_cost), 0),
            )
            .where(
                UsageLedgerEntry.club_id == club.id,
                UsageLedgerEntry.generation_type.in_(
                    (
                        "creative_director",
                        "preference_learning",
                        "visual_trait_analysis",
                        "onboarding_calibration",
                    )
                ),
            )
            .group_by(UsageLedgerEntry.generation_type)
        )
    ]
    profile_index = {item.id: item for item in profiles}
    comparison = _profile_comparison(
        profile_index.get(compare_left or ""),
        profile_index.get(compare_right or ""),
    )
    return _render(
        request,
        "creative/platform_detail.html",
        current,
        club=club,
        feature=creative_feature(db, club.id),
        profiles=profiles,
        overrides=overrides,
        feedback=feedback,
        internal_usage=internal_usage,
        onboarding_session=session,
        references=references,
        feedback_stats=feedback_stats,
        usage_stats=usage_stats,
        comparison=comparison,
    )


@router.post("/clubs/{club_id}/settings")
def club_settings(
    club_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    enabled: bool = Form(default=False),
    learning_enabled: bool = Form(default=False),
    application_enabled: bool = Form(default=False),
    minimum_samples: int = Form(default=5),
    minimum_confidence: float = Form(default=0.55),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_platform_admin(current)
    if db.get(Club, club_id) is None:
        raise HTTPException(404)
    feature = creative_feature(db, club_id)
    value = dict(DEFAULT_CREATIVE_SETTINGS)
    value.update(feature.value or {})
    value.update(
        {
            "learning_enabled": learning_enabled,
            "application_enabled": application_enabled,
            "minimum_samples": max(1, min(100, minimum_samples)),
            "minimum_confidence": max(0.0, min(1.0, minimum_confidence)),
        }
    )
    set_feature_flag(
        db,
        current,
        key=CREATIVE_FLAG,
        enabled=enabled,
        value=value,
        club_id=club_id,
    )
    db.commit()
    return _redirect(
        f"/platform/creative-intelligence/clubs/{club_id}",
        "Vereinseinstellungen gespeichert",
    )


@router.post("/clubs/{club_id}/overrides")
def create_override(
    club_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    modality: str = Form(),
    content_type: str = Form(),
    trait: str = Form(),
    value: str = Form(),
    direction: str = Form(),
    reason: str = Form(),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_platform_admin(current)
    if db.get(Club, club_id) is None:
        raise HTTPException(404)
    modality = normalize_modality(modality)
    content_type = normalize_content_type(modality, content_type)
    traits = normalize_traits({trait: value})
    if direction not in {"prefer", "avoid"}:
        raise HTTPException(422, "Ungültige Override-Richtung")
    previous = list(
        db.scalars(
            select(CreativeProfileOverride)
            .where(
                CreativeProfileOverride.club_id == club_id,
                CreativeProfileOverride.modality == modality,
                CreativeProfileOverride.content_type == content_type,
                CreativeProfileOverride.active.is_(True),
            )
            .order_by(CreativeProfileOverride.override_version.desc())
        )
    )
    preference_values = _trait_map(previous[0].preferences if previous else None)
    avoidance_values = _trait_map(previous[0].avoidances if previous else None)
    for row in previous:
        row.active = False
    next_version = int(
        db.scalar(
            select(func.coalesce(func.max(CreativeProfileOverride.override_version), 0)).where(
                CreativeProfileOverride.club_id == club_id,
                CreativeProfileOverride.modality == modality,
                CreativeProfileOverride.content_type == content_type,
            )
        )
        or 0
    ) + 1
    for key, item in traits.items():
        preference_values.pop(key, None)
        avoidance_values.pop(key, None)
        if direction == "prefer":
            preference_values[key] = item
        else:
            avoidance_values[key] = item
    override = CreativeProfileOverride(
        id=uid(),
        club_id=club_id,
        modality=modality,
        content_type=content_type,
        override_version=next_version,
        preferences=_trait_payload(preference_values),
        avoidances=_trait_payload(avoidance_values),
        trait=trait,
        override_type="structured_trait",
        override_value=traits,
        reason=reason.strip()[:500] or None,
        active=True,
        created_by=current.id,
    )
    db.add(override)
    db.flush()
    platform_audit(
        db,
        current,
        "creative.override_created",
        "creative_profile_override",
        override.id,
        {"club_id": club_id, "modality": modality, "content_type": content_type, "trait": trait},
    )
    db.commit()
    return _redirect(
        f"/platform/creative-intelligence/clubs/{club_id}",
        "Creative Override versioniert gespeichert",
    )


@router.post("/clubs/{club_id}/profiles/{profile_id}/restore")
def restore_profile(
    club_id: str,
    profile_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_platform_admin(current)
    context = TenantContext(club_id=club_id, actor_user_id=current.id)
    try:
        restored = restore_profile_version(
            db,
            context=context,
            source_profile_id=profile_id,
            actor_user_id=current.id,
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    platform_audit(
        db,
        current,
        "creative.profile_restored",
        "creative_preference_profile",
        restored.id,
        {"club_id": club_id, "source_profile_id": profile_id, "new_version": restored.profile_version},
    )
    db.commit()
    return _redirect(
        f"/platform/creative-intelligence/clubs/{club_id}",
        "Profilversion als neue aktive Version wiederhergestellt",
    )


@router.post("/clubs/{club_id}/overrides/{override_id}/deactivate")
def deactivate_override(
    club_id: str,
    override_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_platform_admin(current)
    item = db.scalar(
        select(CreativeProfileOverride).where(
            CreativeProfileOverride.id == override_id,
            CreativeProfileOverride.club_id == club_id,
        )
    )
    if item is None:
        raise HTTPException(404)
    item.active = False
    platform_audit(
        db,
        current,
        "creative.override_deactivated",
        "creative_profile_override",
        item.id,
        {"club_id": club_id, "version": item.override_version},
    )
    db.commit()
    return _redirect(
        f"/platform/creative-intelligence/clubs/{club_id}",
        "Creative Override deaktiviert",
    )


@router.post("/clubs/{club_id}/rebuild")
def rebuild_profiles(
    club_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_platform_admin(current)
    if db.get(Club, club_id) is None:
        raise HTTPException(404)
    context = TenantContext(club_id=club_id, actor_user_id=current.id)
    profiles = rebuild_all_profiles(db, context, force=True)
    platform_audit(
        db,
        current,
        "creative.profiles_rebuilt",
        "club",
        club_id,
        {"club_id": club_id, "created_profiles": len(profiles)},
    )
    db.commit()
    return _redirect(
        f"/platform/creative-intelligence/clubs/{club_id}",
        f"{len(profiles)} Lernprofile neu berechnet",
    )


@router.post("/clubs/{club_id}/reset")
def reset_club_profiles(
    club_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    confirmation: str = Form(),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_platform_admin(current)
    if confirmation.strip() != "PROFILE ZURÜCKSETZEN":
        raise HTTPException(422, "Bestätigung stimmt nicht überein")
    if db.get(Club, club_id) is None:
        raise HTTPException(404)
    context = TenantContext(club_id=club_id, actor_user_id=current.id)
    count = reset_profiles(db, context, current)
    platform_audit(
        db,
        current,
        "creative.profiles_reset_by_platform",
        "club",
        club_id,
        {"club_id": club_id, "archived_profiles": count},
    )
    db.commit()
    return _redirect(
        f"/platform/creative-intelligence/clubs/{club_id}",
        f"{count} aktive Profile zurückgesetzt",
    )


@router.post("/clubs/{club_id}/restart-calibration")
def restart_club_calibration(
    club_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_platform_admin(current)
    if db.get(Club, club_id) is None:
        raise HTTPException(404)
    context = TenantContext(club_id=club_id, actor_user_id=current.id)
    session = get_or_create_session(db, context)
    removed = restart_calibration(db, context, session)
    platform_audit(
        db,
        current,
        "creative.calibration_restarted",
        "club_onboarding_session",
        session.id,
        {"club_id": club_id, "discarded_samples": removed},
    )
    db.commit()
    return _redirect(
        f"/platform/creative-intelligence/clubs/{club_id}",
        "Stil-Kalibrierung wurde neu gestartet",
    )
