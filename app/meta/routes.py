from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.meta.api import MetaApiClient, MetaApiError
from app.meta.media import MediaGrantError, resolve_grant, revoke_grant
from app.meta.oauth import (
    check_connection,
    complete_oauth,
    disconnect,
    refresh_connection,
    reject_oauth,
    start_oauth,
)
from app.meta.publishing import (
    MetaPublishingError,
    create_attempt,
    create_container,
    issue_confirmation,
    publish,
    reconcile_attempt,
    refresh_container_status,
)
from app.models import (
    AuditLog,
    InstagramConnection,
    InstagramPage,
    MetaPublishingAttempt,
    Post,
    PublicationJob,
    PublicMediaGrant,
    SystemSetting,
    User,
)
from app.web import berlin_datetime, check_csrf, csrf_token, current_user, require_admin

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["berlin"] = berlin_datetime
settings = get_settings()
templates.env.globals["environment"] = settings.environment
templates.env.globals["meta_test_enabled"] = settings.meta_test_enabled


def _redirect(path: str, notice: str) -> RedirectResponse:
    return RedirectResponse(f"{path}?notice={notice}", 303)


def _admin(current: User) -> None:
    require_admin(current)


def _page_connection(db: Session, page_id: str):
    page = db.get(InstagramPage, page_id)
    if not page or page.archived_at:
        raise HTTPException(404, "Instagram-Seite nicht gefunden")
    connection = db.scalar(
        select(InstagramConnection).where(
            InstagramConnection.instagram_page_id == page.id
        )
    )
    return page, connection


@router.post("/instagram/{page_id}/meta/connect")
def meta_connect(
    page_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    _admin(current)
    page, _ = _page_connection(db, page_id)
    try:
        url = start_oauth(db, settings, page, current, MetaApiClient(settings))
    except MetaApiError as exc:
        raise HTTPException(409, str(exc)) from exc
    return RedirectResponse(url, 303)


@router.get("/public/instagram/oauth/callback", response_class=HTMLResponse)
def meta_oauth_callback(
    request: Request,
    state: str = "",
    code: str = "",
    error: str = "",
    error_description: str = "",
    db: Session = Depends(get_db),
):
    if error:
        message = f"Instagram-Verbindung abgelehnt: {error_description or error}"
        if state:
            try:
                # Persist only Meta's bounded error identifier. The externally
                # supplied description may contain arbitrary or sensitive text.
                reject_oauth(db, settings, state=state, error=error[:100])
            except Exception:
                db.rollback()
        return templates.TemplateResponse(
            request,
            "meta_oauth_result.html",
            {"ok": False, "message": message},
            status_code=400,
        )
    if not state or not code:
        return templates.TemplateResponse(
            request,
            "meta_oauth_result.html",
            {"ok": False, "message": "OAuth-State oder Code fehlt"},
            status_code=400,
        )
    try:
        connection = complete_oauth(
            db, settings, state=state, code=code, api=MetaApiClient(settings)
        )
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "meta_oauth_result.html",
            {"ok": False, "message": str(exc)},
            status_code=400,
        )
    return templates.TemplateResponse(
        request,
        "meta_oauth_result.html",
        {
            "ok": True,
            "message": (
                f"Instagram @{connection.confirmed_username} wurde als "
                f"{connection.account_type} verbunden."
            ),
        },
    )


@router.post("/instagram/{page_id}/meta/check")
def meta_check(
    page_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    _admin(current)
    _, connection = _page_connection(db, page_id)
    if not connection:
        raise HTTPException(409, "Noch keine Meta-Verbindung vorhanden")
    try:
        check_connection(db, settings, connection, current, MetaApiClient(settings))
    except MetaApiError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _redirect("/instagram", "Meta-Verbindung wurde geprüft")


@router.post("/instagram/{page_id}/meta/refresh")
def meta_refresh(
    page_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    _admin(current)
    _, connection = _page_connection(db, page_id)
    if not connection:
        raise HTTPException(409, "Noch keine Meta-Verbindung vorhanden")
    try:
        refresh_connection(db, settings, connection, current, MetaApiClient(settings))
    except MetaApiError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _redirect("/instagram", "Meta-Token wurde kontrolliert erneuert")


@router.post("/instagram/{page_id}/meta/disconnect")
def meta_disconnect(
    page_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    _admin(current)
    page, connection = _page_connection(db, page_id)
    if connection:
        disconnect(db, connection, page, current)
    return _redirect("/instagram", "Instagram-Verbindung wurde getrennt")


@router.post("/instagram/{page_id}/meta/settings")
def meta_settings(
    page_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    test_account: bool = Form(default=False),
    publishing_enabled: bool = Form(default=False),
    automatic_publishing_enabled: bool = Form(default=False),
    automatic_confirmation: str = Form(default=""),
    allow_feed: bool = Form(default=False),
    allow_story: bool = Form(default=False),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    _admin(current)
    page, connection = _page_connection(db, page_id)
    if not connection:
        raise HTTPException(409, "Zuerst Instagram verbinden")
    if publishing_enabled and (
        connection.status != "connected" or connection.account_type != "BUSINESS"
    ):
        raise HTTPException(409, "Nur eine geprüfte Business-Verbindung darf aktiviert werden")
    required_scopes = {
        "instagram_business_basic",
        "instagram_business_content_publish",
    }
    if publishing_enabled and not required_scopes.issubset(set(connection.scopes or [])):
        raise HTTPException(409, "Erforderliche Instagram-Berechtigungen fehlen")
    if publishing_enabled and not (allow_feed or allow_story):
        raise HTTPException(409, "Mindestens eine Medienart muss aktiviert sein")
    if settings.environment == "meta-test" and automatic_publishing_enabled:
        raise HTTPException(409, "Automatische Veröffentlichung ist im Meta-Test verboten")
    if settings.environment == "production" and automatic_publishing_enabled:
        if not publishing_enabled:
            raise HTTPException(409, "Zuerst Publishing für die Seite aktivieren")
        if automatic_confirmation != "AUTOMATISCH VERÖFFENTLICHEN":
            raise HTTPException(409, "Bestätigung für automatische Veröffentlichung fehlt")
        if not connection.last_check_at:
            raise HTTPException(409, "Meta-Verbindung wurde noch nicht aktuell geprüft")
        page.automatic_publishing_confirmed_by = current.id
        page.automatic_publishing_confirmed_at = datetime.now(timezone.utc)
    elif not automatic_publishing_enabled:
        page.automatic_publishing_confirmed_by = None
        page.automatic_publishing_confirmed_at = None
    connection.test_account = test_account if settings.environment == "meta-test" else False
    page.publishing_enabled = publishing_enabled
    page.allowed_types = {"feed": allow_feed, "story": allow_story}
    page.automatic_publishing_enabled = (
        automatic_publishing_enabled if settings.environment == "production" else False
    )
    page.active = True
    db.add(
        AuditLog(
            user_id=current.id,
            action="meta.settings_changed",
            entity_type="instagram_connection",
            entity_id=connection.id,
            details={
                "test_account": test_account,
                "publishing_enabled": publishing_enabled,
                "automatic_publishing_enabled": page.automatic_publishing_enabled,
                "allowed_types": page.allowed_types,
            },
        )
    )
    db.commit()
    return _redirect("/instagram", "Instagram-Einstellungen gespeichert")


@router.post("/meta-test/emergency-stop")
@router.post("/meta/emergency-stop")
def meta_emergency_stop(
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    enabled: bool = Form(default=False),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    _admin(current)
    setting = db.get(SystemSetting, "emergency_stop")
    if setting:
        setting.value = {"enabled": enabled}
    else:
        db.add(SystemSetting(key="emergency_stop", value={"enabled": enabled}))
    db.add(
        AuditLog(
            user_id=current.id,
            action="meta.emergency_stop_changed",
            entity_type="system_setting",
            entity_id=None,
            details={"enabled": enabled},
        )
    )
    db.commit()
    return _redirect(
        "/instagram",
        "Globaler Not-Aus aktiviert" if enabled else "Globaler Not-Aus deaktiviert",
    )


@router.get("/meta-test/{page_id}", response_class=HTMLResponse)
def meta_test_assistant(
    page_id: str,
    request: Request,
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    _admin(current)
    page, connection = _page_connection(db, page_id)
    jobs = db.scalars(
        select(PublicationJob)
        .where(PublicationJob.instagram_page_id == page.id)
        .order_by(PublicationJob.scheduled_at.desc())
    ).all()
    posts = {post.id: post for post in db.scalars(select(Post))}
    attempts = db.scalars(
        select(MetaPublishingAttempt)
        .join(PublicationJob)
        .where(PublicationJob.instagram_page_id == page.id)
        .order_by(MetaPublishingAttempt.created_at.desc())
        .limit(50)
    ).all()
    return templates.TemplateResponse(
        request,
        "meta_test.html",
        {
            "user": current,
            "csrf": csrf_token(request),
            "title": "Meta-Testassistent",
            "page": page,
            "connection": connection,
            "jobs": jobs,
            "posts": posts,
            "attempts": attempts,
            "settings": settings,
        },
    )


@router.post("/meta-test/{page_id}/attempt")
def meta_test_create_attempt(
    page_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    publication_job_id: str = Form(),
    stage: str = Form(),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    _admin(current)
    page, _ = _page_connection(db, page_id)
    job = db.get(PublicationJob, publication_job_id)
    if not job or job.instagram_page_id != page.id:
        raise HTTPException(404, "Veröffentlichungsauftrag gehört nicht zu dieser Seite")
    try:
        with httpx.Client(
            timeout=settings.meta_http_timeout_seconds,
            follow_redirects=False,
        ) as media_client:
            attempt, _ = create_attempt(
                db,
                settings,
                publication_job_id=job.id,
                stage=stage,
                user=current,
                media_http_client=media_client,
            )
    except (MetaPublishingError, MediaGrantError, MetaApiError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return _redirect(f"/meta-attempts/{attempt.id}", "Meta-Testprüfung abgeschlossen")


@router.get("/meta-attempts/{attempt_id}", response_class=HTMLResponse)
def meta_attempt_detail(
    attempt_id: str,
    request: Request,
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    _admin(current)
    return _render_attempt(request, db, current, attempt_id)


def _render_attempt(
    request: Request,
    db: Session,
    current: User,
    attempt_id: str,
    *,
    confirmation_code: str | None = None,
):
    attempt = db.get(MetaPublishingAttempt, attempt_id)
    if not attempt:
        raise HTTPException(404)
    job = db.get(PublicationJob, attempt.publication_job_id)
    post = db.get(Post, job.post_id) if job else None
    connection = db.get(InstagramConnection, attempt.connection_id)
    grant = (
        db.get(PublicMediaGrant, attempt.public_media_grant_id)
        if attempt.public_media_grant_id
        else None
    )
    media_grant_active = bool(
        grant
        and not grant.revoked_at
        and attempt.phase not in {"completed", "failed"}
    )
    response = templates.TemplateResponse(
        request,
        "meta_attempt.html",
        {
            "user": current,
            "csrf": csrf_token(request),
            "title": "Meta-Veröffentlichungsversuch",
            "attempt": attempt,
            "job": job,
            "post": post,
            "connection": connection,
            "confirmation_code": confirmation_code,
            "media_grant_active": media_grant_active,
            "settings": settings,
        },
    )
    if confirmation_code:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    return response


@router.post("/meta-attempts/{attempt_id}/confirmation")
def meta_attempt_confirmation(
    attempt_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    purpose: str = Form(),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    _admin(current)
    attempt = db.get(MetaPublishingAttempt, attempt_id)
    if not attempt:
        raise HTTPException(404)
    try:
        code = issue_confirmation(db, settings, attempt, current, purpose)
    except MetaPublishingError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _render_attempt(
        request,
        db,
        current,
        attempt.id,
        confirmation_code=code,
    )


@router.post("/meta-attempts/{attempt_id}/container")
def meta_attempt_container(
    attempt_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    confirmation_code: str = Form(),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    _admin(current)
    try:
        with httpx.Client(
            timeout=settings.meta_http_timeout_seconds,
            follow_redirects=False,
        ) as media_client:
            create_container(
                db,
                settings,
                attempt_id=attempt_id,
                user=current,
                confirmation_code=confirmation_code,
                api=MetaApiClient(settings),
                media_http_client=media_client,
            )
    except (MetaPublishingError, MediaGrantError, MetaApiError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return _redirect(f"/meta-attempts/{attempt_id}", "Meta-Container wurde bewusst erstellt")


@router.post("/meta-attempts/{attempt_id}/status")
def meta_attempt_status(
    attempt_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    _admin(current)
    try:
        refresh_container_status(
            db,
            settings,
            attempt_id=attempt_id,
            user=current,
            api=MetaApiClient(settings),
        )
    except (MetaPublishingError, MetaApiError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return _redirect(f"/meta-attempts/{attempt_id}", "Containerstatus aktualisiert")


@router.post("/meta-attempts/{attempt_id}/publish")
def meta_attempt_publish(
    attempt_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    confirmation_code: str = Form(),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    _admin(current)
    try:
        publish(
            db,
            settings,
            attempt_id=attempt_id,
            user=current,
            confirmation_code=confirmation_code,
            api=MetaApiClient(settings),
        )
    except (MetaPublishingError, MetaApiError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return _redirect(f"/meta-attempts/{attempt_id}", "Instagram-Veröffentlichung bestätigt")


@router.post("/meta-attempts/{attempt_id}/reconcile")
def meta_attempt_reconcile(
    attempt_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    resolution: str = Form(),
    note: str = Form(),
    meta_media_id: str = Form(default=""),
    permalink: str = Form(default=""),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    _admin(current)
    try:
        reconcile_attempt(
            db,
            settings,
            attempt_id=attempt_id,
            user=current,
            resolution=resolution,
            note=note,
            meta_media_id=meta_media_id,
            permalink=permalink,
        )
    except MetaPublishingError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _redirect(f"/meta-attempts/{attempt_id}", "Unklarer Vorgang manuell abgeglichen")


@router.post("/meta-attempts/{attempt_id}/grant/revoke")
def meta_attempt_revoke_grant(
    attempt_id: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    _admin(current)
    attempt = db.get(MetaPublishingAttempt, attempt_id)
    if not attempt or not attempt.public_media_grant_id:
        raise HTTPException(404, "Keine Medienfreigabe vorhanden")
    grant = db.get(PublicMediaGrant, attempt.public_media_grant_id)
    if not grant:
        raise HTTPException(404, "Medienfreigabe nicht gefunden")
    if not grant.revoked_at:
        revoke_grant(db, grant, current, reason="durch Administrator widerrufen")
        db.commit()
    return _redirect(f"/meta-attempts/{attempt_id}", "Öffentliche Medienfreigabe widerrufen")


@router.get("/public/meta-media/{token}")
def public_meta_media(token: str, db: Session = Depends(get_db)):
    try:
        _, path = resolve_grant(db, settings, token)
    except MediaGrantError as exc:
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(
        path,
        media_type="image/png",
        headers={
            "Cache-Control": "private, no-store, max-age=0",
            "X-Content-Type-Options": "nosniff",
        },
    )
