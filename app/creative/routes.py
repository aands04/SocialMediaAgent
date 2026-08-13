from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.creative.flags import onboarding_feature
from app.creative.onboarding import (
    STEPS,
    complete_onboarding,
    get_or_create_session,
    rate_sample,
    restart_calibration,
    save_step,
    seed_calibration,
    skip_calibration,
)
from app.creative.service import (
    profile_status,
    rebuild_all_profiles,
    reset_profiles,
    update_club_settings,
)
from app.db import get_db
from app.models import OnboardingCalibrationSample, User
from app.tenancy.context import TenantContext
from app.web import check_csrf, csrf_token, current_user, require_admin

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _render(request: Request, name: str, current: User, **context):
    return templates.TemplateResponse(
        request,
        name,
        {"user": current, "csrf": csrf_token(request), **context},
    )


def _redirect(path: str, notice: str) -> RedirectResponse:
    return RedirectResponse(f"{path}?notice={quote_plus(notice)}", status_code=303)


@router.get("/creative-intelligence", response_class=HTMLResponse)
def creative_preferences(
    request: Request,
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    require_admin(current)
    context = TenantContext.from_user(current)
    return _render(
        request,
        "creative/preferences.html",
        current,
        status=profile_status(db, context),
    )


@router.post("/creative-intelligence/settings")
def creative_settings(
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    learning_enabled: bool = Form(default=False),
    application_enabled: bool = Form(default=False),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    context = TenantContext.from_user(current)
    update_club_settings(
        db,
        context,
        current,
        learning=learning_enabled,
        application=application_enabled,
    )
    db.commit()
    return _redirect("/creative-intelligence", "Einstellungen gespeichert")


@router.post("/creative-intelligence/rebuild")
def creative_rebuild(
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    context = TenantContext.from_user(current)
    built = rebuild_all_profiles(db, context)
    db.commit()
    return _redirect(
        "/creative-intelligence",
        f"{len(built)} Präferenzprofile neu berechnet",
    )


@router.post("/creative-intelligence/reset")
def creative_reset(
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    confirmation: str = Form(),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    if confirmation.strip() != "PROFILE ZURÜCKSETZEN":
        raise HTTPException(422, "Bitte die Sicherheitsabfrage exakt bestätigen")
    context = TenantContext.from_user(current)
    archived = reset_profiles(db, context, current)
    db.commit()
    return _redirect(
        "/creative-intelligence",
        f"{archived} aktive Profile archiviert; das Feedback bleibt erhalten",
    )


@router.get("/onboarding", response_class=HTMLResponse)
def onboarding(
    request: Request,
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    require_admin(current)
    context = TenantContext.from_user(current)
    session = get_or_create_session(db, context)
    samples: list[OnboardingCalibrationSample] = []
    if session.current_step >= 10:
        samples = seed_calibration(db, context, session)
    db.commit()
    return _render(
        request,
        "creative/onboarding.html",
        current,
        session=session,
        steps=STEPS,
        samples=samples,
        feature=onboarding_feature(db, context.club_id),
    )


@router.post("/onboarding/step")
def onboarding_step(
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    step: int = Form(),
    answer_keys: list[str] = Form(default=[]),
    answer_values: list[str] = Form(default=[]),
    session_version: int = Form(),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    if len(answer_keys) != len(answer_values):
        raise HTTPException(422, "Einrichtungsangaben sind unvollständig")
    context = TenantContext.from_user(current)
    try:
        save_step(
            db,
            context,
            step=step,
            values={
                key: value
                for key, value in zip(answer_keys, answer_values, strict=True)
            },
            expected_version=session_version,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    db.commit()
    return RedirectResponse("/onboarding", status_code=303)


@router.post("/onboarding/calibration/{sample_id}")
def onboarding_rate_sample(
    sample_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    rating: str = Form(),
    reason_codes: list[str] = Form(default=[]),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    context = TenantContext.from_user(current)
    try:
        rate_sample(
            db,
            context,
            sample_id=sample_id,
            rating=rating,
            reason_codes=reason_codes,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    return RedirectResponse("/onboarding", status_code=303)


@router.post("/onboarding/complete")
def onboarding_complete(
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    context = TenantContext.from_user(current)
    session = get_or_create_session(db, context)
    try:
        complete_onboarding(db, context, session)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    return _redirect("/creative-intelligence", "Einrichtung abgeschlossen")


@router.post("/onboarding/skip-calibration")
def onboarding_skip_calibration(
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    context = TenantContext.from_user(current)
    session = get_or_create_session(db, context)
    skip_calibration(session, current.id)
    db.commit()
    return _redirect("/creative-intelligence", "Kalibrierung übersprungen")


@router.post("/onboarding/restart-calibration")
def onboarding_restart_calibration(
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    confirmation: str = Form(),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
    if confirmation.strip() != "KALIBRIERUNG NEU STARTEN":
        raise HTTPException(422, "Bitte die Sicherheitsabfrage exakt bestätigen")
    context = TenantContext.from_user(current)
    session = get_or_create_session(db, context)
    count = restart_calibration(db, context, session)
    db.commit()
    return _redirect(
        "/onboarding",
        f"Kalibrierung neu gestartet; {count} alte Testbeispiele entfernt",
    )
