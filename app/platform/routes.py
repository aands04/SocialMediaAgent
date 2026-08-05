from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.limits.service import effective_limits
from app.models import (
    AuditLog,
    Club,
    ClubAdditionalAllowance,
    ClubPromptOverride,
    ClubStatus,
    FeatureFlag,
    Game,
    GenerationJob,
    GenerationJobStatus,
    PlanProfile,
    PromptTemplate,
    StorageObject,
    StorageReconciliationRun,
    Team,
    UsageLedgerEntry,
    UsageStatus,
    User,
)
from app.platform.prompt_tests import PromptTestError, run_fixture_prompt_test
from app.platform.service import (
    PlatformOperationError,
    activate_prompt_version,
    add_temporary_allowance,
    archive_prompt_version,
    change_club_status,
    create_club_with_admin,
    create_prompt_override,
    move_user_to_club,
    platform_audit,
    set_feature_flag,
    update_club_limits,
)
from app.storage.providers import ObjectStorageError, build_object_storage_provider
from app.storage.service import reconcile_storage
from app.usage.service import usage_summary
from app.web import (
    berlin_datetime,
    check_csrf,
    csrf_token,
    current_user,
    require_platform_admin,
)

router = APIRouter(prefix="/platform")
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["berlin"] = berlin_datetime


def render(request: Request, name: str, current: User, **context):
    return templates.TemplateResponse(
        request,
        name,
        {"user": current, "csrf": csrf_token(request), "platform_area": True, **context},
    )


@router.get("", response_class=HTMLResponse)
def dashboard(
    request: Request,
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    require_platform_admin(current)
    statuses = {
        status.value: int(
            db.scalar(select(func.count()).select_from(Club).where(Club.status == status)) or 0
        )
        for status in ClubStatus
    }
    storage_bytes = int(
        db.scalar(
            select(func.coalesce(func.sum(StorageObject.size_bytes), 0)).where(
                StorageObject.deleted_at.is_(None), StorageObject.billable.is_(True)
            )
        )
        or 0
    )
    month_start = datetime.now(timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    usage = {
        kind: int(
            db.scalar(
                select(func.coalesce(func.sum(UsageLedgerEntry.actual_quantity), 0)).where(
                    UsageLedgerEntry.generation_type == kind,
                    UsageLedgerEntry.period_start == month_start,
                    UsageLedgerEntry.status.in_(
                        [UsageStatus.COMPLETED_BILLABLE, UsageStatus.REJECTED_BY_USER]
                    ),
                )
            )
            or 0
        )
        for kind in ("text", "image")
    }
    failed_jobs = int(
        db.scalar(
            select(func.count())
            .select_from(GenerationJob)
            .where(
                GenerationJob.status.in_(
                    [GenerationJobStatus.FAILED, GenerationJobStatus.MANUAL_REVIEW_REQUIRED]
                )
            )
        )
        or 0
    )
    near_limit_clubs: set[str] = set()
    over_limit_clubs: set[str] = set()
    clubs_list = list(db.scalars(select(Club).order_by(Club.name)))
    for club in clubs_list:
        limits = effective_limits(db, club.id)
        values = {
            "teams": int(
                db.scalar(
                    select(func.count()).select_from(Team).where(
                        Team.club_id == club.id,
                        Team.active.is_(True),
                        Team.archived_at.is_(None),
                    )
                )
                or 0
            ),
            "storage_bytes": int(
                db.scalar(
                    select(func.coalesce(func.sum(StorageObject.size_bytes), 0)).where(
                        StorageObject.club_id == club.id,
                        StorageObject.deleted_at.is_(None),
                        StorageObject.billable.is_(True),
                    )
                )
                or 0
            ),
            "ai_texts": usage_summary(db, club.id, "text").completed,
            "ai_images": usage_summary(db, club.id, "image").completed,
        }
        for key, used in values.items():
            maximum = limits[key].value
            if used > maximum:
                over_limit_clubs.add(club.id)
            elif maximum and used / maximum >= 0.75:
                near_limit_clubs.add(club.id)
    near_limit_clubs -= over_limit_clubs
    reconciliation_alerts = int(
        db.scalar(
            select(func.count()).select_from(StorageReconciliationRun).where(
                StorageReconciliationRun.status == "attention_required"
            )
        )
        or 0
    )
    reconciliation_runs = list(
        db.scalars(
            select(StorageReconciliationRun)
            .order_by(StorageReconciliationRun.created_at.desc())
            .limit(10)
        )
    )
    return render(
        request,
        "platform_dashboard.html",
        current,
        statuses=statuses,
        club_count=sum(statuses.values()),
        team_count=int(db.scalar(select(func.count()).select_from(Team)) or 0),
        storage_bytes=storage_bytes,
        usage=usage,
        failed_jobs=failed_jobs,
        clubs=clubs_list,
        club_names={club.id: club.name for club in clubs_list},
        near_limit_count=len(near_limit_clubs),
        over_limit_count=len(over_limit_clubs),
        reconciliation_alerts=reconciliation_alerts,
        reconciliation_runs=reconciliation_runs,
        feature_flags=db.scalars(
            select(FeatureFlag).order_by(FeatureFlag.key, FeatureFlag.club_id)
        ).all(),
        title="Plattformübersicht",
    )


@router.post("/storage/reconcile")
def run_storage_reconciliation(
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    club_id: str = Form(default=""),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_platform_admin(current)
    selected_club_id = club_id.strip() or None
    if selected_club_id and db.get(Club, selected_club_id) is None:
        raise HTTPException(404, "Verein nicht gefunden")
    try:
        provider = build_object_storage_provider(get_settings())
        run = reconcile_storage(
            db,
            provider,
            club_id=selected_club_id,
            started_by=current.id,
        )
        platform_audit(
            db,
            current,
            "storage.reconciliation_completed",
            "storage_reconciliation_run",
            run.id,
            {
                "club_id": selected_club_id,
                "provider": run.provider,
                "status": run.status,
                "checked_objects": run.checked_objects,
                "missing_objects": run.missing_objects,
                "unexpected_objects": run.unexpected_objects,
                "size_mismatches": run.size_mismatches,
            },
        )
        db.commit()
    except ObjectStorageError as exc:
        db.rollback()
        raise HTTPException(
            503,
            "Der konfigurierte Objektspeicher konnte nicht sicher geprüft werden.",
        ) from exc
    message = (
        "Speicherabgleich abgeschlossen"
        if run.status == "completed"
        else "Speicherabgleich abgeschlossen: Abweichungen bitte prüfen"
    )
    return RedirectResponse(f"/platform?notice={quote_plus(message)}", 303)


@router.get("/clubs", response_class=HTMLResponse)
def clubs(
    request: Request,
    q: str = "",
    status_filter: str = "",
    sort: str = "name",
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    require_platform_admin(current)
    statement = select(Club)
    clean_query = q.strip()
    if clean_query:
        needle = f"%{clean_query.casefold()}%"
        statement = statement.where(
            func.lower(Club.name).like(needle)
            | func.lower(Club.short_name).like(needle)
            | func.lower(Club.slug).like(needle)
        )
    if status_filter:
        try:
            statement = statement.where(Club.status == ClubStatus(status_filter))
        except ValueError as exc:
            raise HTTPException(422, "Unbekannter Vereinsstatus") from exc
    ordering = {
        "name": Club.name.asc(),
        "name_desc": Club.name.desc(),
        "created_desc": Club.created_at.desc(),
        "status": Club.status.asc(),
    }.get(sort)
    if ordering is None:
        raise HTTPException(422, "Unbekannte Sortierung")
    return render(
        request,
        "platform_clubs.html",
        current,
        clubs=db.scalars(statement.order_by(ordering)).all(),
        plans=db.scalars(
            select(PlanProfile).where(PlanProfile.archived_at.is_(None)).order_by(PlanProfile.name)
        ).all(),
        statuses=list(ClubStatus),
        filters={"q": clean_query, "status": status_filter, "sort": sort},
        title="Vereine verwalten",
    )


@router.post("/clubs")
def create_club(
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    name: str = Form(),
    short_name: str = Form(),
    slug: str = Form(),
    timezone_name: str = Form(default="Europe/Berlin"),
    contact_name: str = Form(default=""),
    contact_email: str = Form(default=""),
    admin_email: str = Form(),
    admin_password: str = Form(),
    plan_profile_id: str = Form(),
    status_value: ClubStatus = Form(alias="status"),
    max_teams: int | None = Form(default=None),
    max_storage_bytes: int | None = Form(default=None),
    monthly_ai_texts: int | None = Form(default=None),
    monthly_ai_images: int | None = Form(default=None),
    max_fonts: int | None = Form(default=None),
    max_instagram_pages: int | None = Form(default=None),
    primary_color: str = Form(default="#172554"),
    secondary_color: str = Form(default="#ffffff"),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_platform_admin(current)
    overrides = {
        key: value
        for key, value in {
            "teams": max_teams,
            "storage_bytes": max_storage_bytes,
            "ai_texts": monthly_ai_texts,
            "ai_images": monthly_ai_images,
            "fonts": max_fonts,
            "instagram_pages": max_instagram_pages,
        }.items()
        if value is not None
    }
    if any(value < 0 for value in overrides.values()):
        raise HTTPException(422, "Limits dürfen nicht negativ sein")
    try:
        club = create_club_with_admin(
            db,
            current,
            name=name,
            short_name=short_name,
            slug=slug,
            timezone_name=timezone_name,
            contact_name=contact_name,
            contact_email=contact_email,
            admin_email=admin_email,
            admin_password=admin_password,
            plan_profile_id=plan_profile_id,
            status=status_value,
            trial_ends_at=None,
            limit_overrides=overrides,
            branding_settings={"primary_color": primary_color, "secondary_color": secondary_color},
        )
        db.commit()
    except (PlatformOperationError, ValueError) as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            409, "Verein oder Administratorkonto kollidiert mit vorhandenen Daten"
        ) from exc
    return RedirectResponse(f"/platform/clubs/{club.id}", 303)


@router.get("/clubs/{club_id}", response_class=HTMLResponse)
def club_detail(
    club_id: str,
    request: Request,
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    require_platform_admin(current)
    club = db.get(Club, club_id)
    if club is None:
        raise HTTPException(404)
    limits = effective_limits(db, club.id)
    counts = {
        "teams": int(
            db.scalar(select(func.count()).select_from(Team).where(Team.club_id == club.id)) or 0
        ),
        "users": int(
            db.scalar(select(func.count()).select_from(User).where(User.club_id == club.id)) or 0
        ),
        "storage": int(
            db.scalar(
                select(func.coalesce(func.sum(StorageObject.size_bytes), 0)).where(
                    StorageObject.club_id == club.id, StorageObject.deleted_at.is_(None)
                )
            )
            or 0
        ),
    }
    return render(
        request,
        "platform_club_detail.html",
        current,
        club=club,
        limits=limits,
        counts=counts,
        statuses=list(ClubStatus),
        users=db.scalars(select(User).where(User.club_id == club.id).order_by(User.email)).all(),
        allowances=db.scalars(
            select(ClubAdditionalAllowance)
            .where(ClubAdditionalAllowance.club_id == club.id)
            .order_by(ClubAdditionalAllowance.ends_at.desc())
        ).all(),
        prompt_overrides=db.scalars(
            select(ClubPromptOverride)
            .where(ClubPromptOverride.club_id == club.id)
            .order_by(ClubPromptOverride.created_at.desc())
        ).all(),
        title=club.name,
    )


@router.post("/clubs/{club_id}/status")
def club_status(
    club_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    status_value: ClubStatus = Form(alias="status"),
    version: int = Form(),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_platform_admin(current)
    statement = select(Club).where(Club.id == club_id)
    if db.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    club = db.scalar(statement)
    if club is None:
        raise HTTPException(404)
    try:
        change_club_status(db, current, club, status_value, version)
        db.commit()
    except PlatformOperationError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    return RedirectResponse(f"/platform/clubs/{club.id}", 303)


@router.post("/clubs/{club_id}/limits")
def club_limits(
    club_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    version: int = Form(),
    max_teams: int | None = Form(default=None),
    max_storage_bytes: int | None = Form(default=None),
    monthly_ai_texts: int | None = Form(default=None),
    monthly_ai_images: int | None = Form(default=None),
    max_fonts: int | None = Form(default=None),
    max_instagram_pages: int | None = Form(default=None),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_platform_admin(current)
    statement = select(Club).where(Club.id == club_id)
    if db.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    club = db.scalar(statement)
    if club is None:
        raise HTTPException(404)
    try:
        update_club_limits(
            db,
            current,
            club,
            overrides={
                "teams": max_teams,
                "storage_bytes": max_storage_bytes,
                "ai_texts": monthly_ai_texts,
                "ai_images": monthly_ai_images,
                "fonts": max_fonts,
                "instagram_pages": max_instagram_pages,
            },
            expected_version=version,
        )
        db.commit()
    except PlatformOperationError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    return RedirectResponse(f"/platform/clubs/{club_id}", 303)


@router.post("/clubs/{club_id}/allowances")
def club_allowance(
    club_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    limit_key: str = Form(),
    amount: int = Form(),
    starts_at: datetime = Form(),
    ends_at: datetime = Form(),
    reason: str = Form(default=""),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_platform_admin(current)
    club = db.get(Club, club_id)
    if club is None:
        raise HTTPException(404)
    starts_at = starts_at.replace(tzinfo=starts_at.tzinfo or timezone.utc)
    ends_at = ends_at.replace(tzinfo=ends_at.tzinfo or timezone.utc)
    try:
        add_temporary_allowance(
            db,
            current,
            club,
            limit_key=limit_key,
            amount=amount,
            starts_at=starts_at,
            ends_at=ends_at,
            reason=reason,
        )
        db.commit()
    except PlatformOperationError as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc
    return RedirectResponse(f"/platform/clubs/{club_id}", 303)


@router.post("/clubs/{club_id}/prompt-overrides")
def club_prompt_override(
    club_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    prompt_kind: str = Form(),
    post_type: str = Form(),
    media_kind: str = Form(default="none"),
    additional_instruction: str = Form(default=""),
    forbidden_phrases: str = Form(default=""),
    sponsor_rules: str = Form(default=""),
    club_rules: str = Form(default=""),
    activate: bool = Form(default=False),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_platform_admin(current)
    club = db.get(Club, club_id)
    if club is None:
        raise HTTPException(404)
    def split(value: str) -> list[str]:
        return [line.strip() for line in value.splitlines() if line.strip()]
    try:
        create_prompt_override(
            db,
            current,
            club,
            prompt_kind=prompt_kind,
            post_type=post_type,
            media_kind=media_kind,
            additional_instruction=additional_instruction,
            forbidden_phrases=split(forbidden_phrases),
            sponsor_rules=split(sponsor_rules),
            club_rules=split(club_rules),
            activate=activate,
        )
        db.commit()
    except PlatformOperationError as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc
    return RedirectResponse(f"/platform/clubs/{club_id}", 303)


@router.get("/plans", response_class=HTMLResponse)
def plans(
    request: Request,
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    require_platform_admin(current)
    return render(
        request,
        "platform_plans.html",
        current,
        plans=db.scalars(
            select(PlanProfile).order_by(PlanProfile.name, PlanProfile.version.desc())
        ).all(),
        title="Tarif- und Limitprofile",
    )


@router.post("/plans")
def create_plan(
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    name: str = Form(),
    description: str = Form(default=""),
    max_teams: int = Form(),
    max_storage_bytes: int = Form(),
    monthly_ai_texts: int = Form(),
    monthly_ai_images: int = Form(),
    max_fonts: int = Form(),
    max_instagram_pages: int = Form(),
    trial_days: int | None = Form(default=None),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_platform_admin(current)
    values = [
        max_teams,
        max_storage_bytes,
        monthly_ai_texts,
        monthly_ai_images,
        max_fonts,
        max_instagram_pages,
    ]
    if not name.strip() or any(value < 0 for value in values):
        raise HTTPException(422, "Profilname und Limits sind ungültig")
    previous = db.scalar(
        select(PlanProfile)
        .where(PlanProfile.name == name.strip())
        .order_by(PlanProfile.version.desc())
    )
    item = PlanProfile(
        name=name.strip(),
        description=description.strip() or None,
        max_teams=max_teams,
        max_storage_bytes=max_storage_bytes,
        monthly_ai_texts=monthly_ai_texts,
        monthly_ai_images=monthly_ai_images,
        max_fonts=max_fonts,
        max_instagram_pages=max_instagram_pages,
        trial_days=trial_days,
        feature_flags={},
        version=(previous.version + 1) if previous else 1,
    )
    db.add(item)
    db.flush()
    platform_audit(
        db,
        current,
        "plan.created",
        "plan_profile",
        item.id,
        {"name": item.name, "version": item.version},
    )
    db.commit()
    return RedirectResponse("/platform/plans", 303)


@router.post("/plans/{plan_id}/archive")
def archive_plan(
    plan_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_platform_admin(current)
    item = db.get(PlanProfile, plan_id)
    if item is None:
        raise HTTPException(404)
    item.active = False
    item.archived_at = datetime.now(timezone.utc)
    platform_audit(
        db,
        current,
        "plan.archived",
        "plan_profile",
        item.id,
        {"name": item.name, "version": item.version},
    )
    db.commit()
    return RedirectResponse("/platform/plans", 303)


@router.post("/feature-flags")
def feature_flag(
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    key: str = Form(),
    enabled: bool = Form(default=False),
    value_json: str = Form(default="{}"),
    club_id: str | None = Form(default=None),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_platform_admin(current)
    try:
        value = json.loads(value_json)
        if not isinstance(value, dict):
            raise ValueError
        set_feature_flag(
            db,
            current,
            key=key,
            enabled=enabled,
            value=value,
            club_id=club_id or None,
        )
        db.commit()
    except (json.JSONDecodeError, ValueError, PlatformOperationError) as exc:
        db.rollback()
        raise HTTPException(422, "Feature-Flag ist ungültig") from exc
    return RedirectResponse("/platform", 303)


@router.post("/prompts/{prompt_id}/activate")
def activate_prompt(
    prompt_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_platform_admin(current)
    statement = select(PromptTemplate).where(PromptTemplate.id == prompt_id)
    if db.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    item = db.scalar(statement)
    if item is None:
        raise HTTPException(404)
    try:
        activate_prompt_version(db, current, item)
        db.commit()
    except PlatformOperationError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    return RedirectResponse("/prompts", 303)


@router.post("/prompts/{prompt_id}/archive")
def archive_prompt(
    prompt_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_platform_admin(current)
    item = db.get(PromptTemplate, prompt_id)
    if item is None:
        raise HTTPException(404)
    archive_prompt_version(db, current, item)
    db.commit()
    return RedirectResponse("/prompts", 303)


@router.post("/prompt-tests")
def prompt_fixture_test(
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    club_id: str = Form(),
    prompt_id: str = Form(),
    comparison_prompt_id: str = Form(default=""),
    team_id: str = Form(default=""),
    game_id: str = Form(default=""),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_platform_admin(current)
    club = db.get(Club, club_id)
    candidate = db.get(PromptTemplate, prompt_id)
    comparison = db.get(PromptTemplate, comparison_prompt_id) if comparison_prompt_id else None
    team = db.get(Team, team_id) if team_id else None
    game = db.get(Game, game_id) if game_id else None
    if club is None or candidate is None:
        raise HTTPException(404)
    try:
        result = run_fixture_prompt_test(
            db,
            current,
            club=club,
            candidate=candidate,
            comparison=comparison,
            team=team,
            game=game,
        )
        db.commit()
    except PromptTestError as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc
    return RedirectResponse(f"/prompts?test_run={result.id}", 303)


@router.post("/users/{user_id}/move")
def move_user(
    user_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    club_id: str = Form(),
    version: int = Form(),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_platform_admin(current)
    user_statement = select(User).where(User.id == user_id)
    if db.bind.dialect.name == "postgresql":
        user_statement = user_statement.with_for_update()
    user = db.scalar(user_statement)
    target = db.get(Club, club_id)
    if user is None or target is None:
        raise HTTPException(404)
    try:
        move_user_to_club(db, current, user, target, version)
        db.commit()
    except PlatformOperationError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    return RedirectResponse(f"/platform/clubs/{target.id}", 303)


@router.get("/usage.csv")
def usage_csv(current: User = Depends(current_user), db: Session = Depends(get_db)):
    require_platform_admin(current)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["club_id", "verein", "status", "speicher_bytes", "ki_texte", "ki_bilder"])
    for club in db.scalars(select(Club).order_by(Club.name)):
        storage = int(
            db.scalar(
                select(func.coalesce(func.sum(StorageObject.size_bytes), 0)).where(
                    StorageObject.club_id == club.id, StorageObject.deleted_at.is_(None)
                )
            )
            or 0
        )
        amounts = {}
        for kind in ("text", "image"):
            amounts[kind] = int(
                db.scalar(
                    select(func.coalesce(func.sum(UsageLedgerEntry.actual_quantity), 0)).where(
                        UsageLedgerEntry.club_id == club.id,
                        UsageLedgerEntry.generation_type == kind,
                        UsageLedgerEntry.billable.is_(True),
                    )
                )
                or 0
            )
        writer.writerow(
            [club.id, club.name, club.status.value, storage, amounts["text"], amounts["image"]]
        )
    return Response(
        output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="plattform-verbrauch.csv"'},
    )


@router.get("/audit", response_class=HTMLResponse)
def audit_log(
    request: Request,
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    require_platform_admin(current)
    items = db.scalars(
        select(AuditLog).where(AuditLog.scope == "platform").order_by(AuditLog.at.desc()).limit(500)
    ).all()
    return render(request, "platform_audit.html", current, items=items, title="Plattform-Audit")
