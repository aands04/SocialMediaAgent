import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app.admin_routes import router as admin_router
from app.auth.email_change import (
    complete_email_change,
    find_valid_email_change_token,
    request_email_change,
)
from app.auth.password_reset import (
    GENERIC_RESET_MESSAGE,
    complete_password_reset,
    find_valid_reset_token,
    request_password_reset,
)
from app.auth.service import (
    authenticate,
    hash_password,
    normalize_email,
    validate_new_password,
    verify_password,
)
from app.config import get_settings
from app.db import get_db
from app.meta.routes import router as meta_router
from app.models import AuditLog, Game, GenerationJob, Post, PublicationJob, Role, Team, User
from app.monitoring.service import system_status
from app.web import berlin_datetime, csrf_token, current_user, optional_current_user

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.media_root.mkdir(parents=True, exist_ok=True)
    settings.generated_root.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(
    title="Vereins Social Media Agent",
    docs_url="/api/docs" if settings.environment != "production" else None,
    lifespan=lifespan,
)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    max_age=settings.session_max_age,
    https_only=settings.environment == "production",
    same_site="lax",
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["berlin"] = berlin_datetime
templates.env.globals["environment"] = settings.environment
templates.env.globals["meta_test_enabled"] = settings.meta_test_enabled
templates.env.globals["password_reset_enabled"] = settings.password_reset_enabled
templates.env.globals["minimum_password_length"] = 8


def csrf(request: Request):
    return csrf_token(request)


def request_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def no_store(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


@app.get("/health")
def health(db: Session = Depends(get_db)):
    report = system_status(db, settings)
    database = "ok" if report["checks"]["postgresql"]["ok"] else "error"
    return {
        "status": "ok" if report["ok"] else "degraded",
        "checks": {
            "web": "ok",
            "database": database,
            "worker": "ok" if report["checks"]["worker"]["ok"] else "error",
        },
    }


@app.get("/system", response_class=HTMLResponse)
def system_dashboard(
    request: Request, current: User = Depends(current_user), db: Session = Depends(get_db)
):
    return templates.TemplateResponse(
        request,
        "system.html",
        {
            "user": current,
            "report": system_status(db, settings),
            "csrf": csrf(request),
            "title": "Systemstatus",
        },
    )


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return no_store(
        templates.TemplateResponse(
            request,
            "login.html",
            {"csrf": csrf(request), "notice": request.query_params.get("notice")},
        )
    )


@app.post("/login")
def login(
    request: Request,
    email: str = Form(),
    password: str = Form(),
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    if not secrets.compare_digest(csrf_token, request.session.get("csrf", "")):
        raise HTTPException(403, "CSRF-Prüfung fehlgeschlagen")
    current = authenticate(db, email, password)
    if not current:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"csrf": csrf(request), "error": "Anmeldung fehlgeschlagen"},
            status_code=401,
        )
    request.session.clear()
    request.session["uid"] = current.id
    request.session["auth_version"] = current.auth_version
    csrf(request)
    return RedirectResponse("/", 303)


@app.get("/register", response_class=HTMLResponse)
def register_form(request: Request):
    return no_store(
        templates.TemplateResponse(
            request,
            "register.html",
            {"csrf": csrf(request)},
        )
    )


@app.post("/register", response_class=HTMLResponse)
def register(
    request: Request,
    email: str = Form(),
    password: str = Form(),
    password_confirmation: str = Form(),
    csrf_token_value: str = Form(alias="csrf_token"),
    db: Session = Depends(get_db),
):
    if not secrets.compare_digest(csrf_token_value, request.session.get("csrf", "")):
        raise HTTPException(403, "CSRF-Prüfung fehlgeschlagen")
    try:
        normalized_email = normalize_email(email)
    except ValueError as exc:
        error = str(exc)
    else:
        error = validate_new_password(password)
    if password != password_confirmation:
        error = "Passwörter stimmen nicht überein"
    if error:
        return no_store(
            templates.TemplateResponse(
                request,
                "register.html",
                {"csrf": csrf(request), "error": error, "email": email},
                status_code=422,
            )
        )

    existing = db.scalar(select(User).where(User.email == normalized_email))
    now = datetime.now(timezone.utc)
    if existing is None:
        item = User(
            email=normalized_email,
            password_hash=hash_password(password),
            role=Role.VIEWER,
            all_teams=False,
            active=False,
            registration_status="pending",
            registration_requested_at=now,
        )
        db.add(item)
        db.flush()
        db.add(
            AuditLog(
                user_id=item.id,
                action="registration.requested",
                entity_type="user",
                entity_id=item.id,
                details={},
                ip=request_ip(request),
            )
        )
        db.commit()
    elif existing.registration_status == "rejected" and not existing.active:
        existing.password_hash = hash_password(password)
        existing.registration_status = "pending"
        existing.registration_requested_at = now
        existing.registration_reviewed_at = None
        existing.registration_reviewed_by = None
        existing.auth_version += 1
        db.add(
            AuditLog(
                user_id=existing.id,
                action="registration.requested_again",
                entity_type="user",
                entity_id=existing.id,
                details={},
                ip=request_ip(request),
            )
        )
        db.commit()

    return RedirectResponse(
        "/login?notice=Registrierung+eingereicht.+Die+Administration+muss+das+Konto+freigeben.",
        303,
    )


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", 303)


@app.get("/password/forgot", response_class=HTMLResponse)
def forgot_password_form(request: Request):
    return no_store(
        templates.TemplateResponse(
            request,
            "forgot_password.html",
            {"csrf": csrf(request), "enabled": settings.password_reset_enabled},
        )
    )


@app.post("/password/forgot", response_class=HTMLResponse)
def forgot_password(
    request: Request,
    email: str = Form(),
    csrf_token_value: str = Form(alias="csrf_token"),
    db: Session = Depends(get_db),
):
    if not secrets.compare_digest(csrf_token_value, request.session.get("csrf", "")):
        raise HTTPException(403, "CSRF-Prüfung fehlgeschlagen")
    request_password_reset(db, settings, email, request_ip(request))
    return no_store(
        templates.TemplateResponse(
            request,
            "forgot_password.html",
            {
                "csrf": csrf(request),
                "enabled": settings.password_reset_enabled,
                "success": GENERIC_RESET_MESSAGE,
            },
        )
    )


@app.get("/password/reset/{token}", response_class=HTMLResponse)
def reset_password_form(token: str, request: Request, db: Session = Depends(get_db)):
    if not settings.password_reset_enabled:
        raise HTTPException(404, "Passwort-Reset ist nicht aktiviert")
    item = find_valid_reset_token(db, token)
    response = templates.TemplateResponse(
        request,
        "reset_password.html",
        {
            "csrf": csrf(request),
            "token": token if item else None,
            "error": None if item else "Reset-Link ist ungültig oder abgelaufen",
        },
        status_code=200 if item else 400,
    )
    return no_store(response)


@app.post("/password/reset/{token}", response_class=HTMLResponse)
def reset_password(
    token: str,
    request: Request,
    password: str = Form(),
    password_confirmation: str = Form(),
    csrf_token_value: str = Form(alias="csrf_token"),
    db: Session = Depends(get_db),
):
    if not settings.password_reset_enabled:
        raise HTTPException(404, "Passwort-Reset ist nicht aktiviert")
    if not secrets.compare_digest(csrf_token_value, request.session.get("csrf", "")):
        raise HTTPException(403, "CSRF-Prüfung fehlgeschlagen")
    item = find_valid_reset_token(db, token)
    error = validate_new_password(password)
    if password != password_confirmation:
        error = "Passwörter stimmen nicht überein"
    if item is None:
        error = "Reset-Link ist ungültig oder abgelaufen"
    if error:
        return no_store(
            templates.TemplateResponse(
                request,
                "reset_password.html",
                {"csrf": csrf(request), "token": token if item else None, "error": error},
                status_code=422 if item else 400,
            )
        )
    try:
        complete_password_reset(db, item, hash_password(password), request_ip(request))
    except ValueError:
        return no_store(
            templates.TemplateResponse(
                request,
                "reset_password.html",
                {
                    "csrf": csrf(request),
                    "token": None,
                    "error": "Reset-Link ist ungültig oder wurde bereits verwendet",
                },
                status_code=400,
            )
        )
    request.session.clear()
    return RedirectResponse("/login?notice=Passwort+erfolgreich+ge%C3%A4ndert", 303)


@app.get("/account/password", response_class=HTMLResponse)
def change_password_form(request: Request, current: User = Depends(current_user)):
    return no_store(
        templates.TemplateResponse(
            request,
            "change_password.html",
            {"user": current, "csrf": csrf(request), "title": "Passwort ändern"},
        )
    )


@app.post("/account/password", response_class=HTMLResponse)
def change_password(
    request: Request,
    current_password: str = Form(),
    password: str = Form(),
    password_confirmation: str = Form(),
    csrf_token_value: str = Form(alias="csrf_token"),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    if not secrets.compare_digest(csrf_token_value, request.session.get("csrf", "")):
        raise HTTPException(403, "CSRF-Prüfung fehlgeschlagen")
    error = None
    if not verify_password(current_password, current.password_hash):
        error = "Aktuelles Passwort ist nicht korrekt"
    elif password != password_confirmation:
        error = "Passwörter stimmen nicht überein"
    else:
        error = validate_new_password(password)
    if error is None and verify_password(password, current.password_hash):
        error = "Das neue Passwort muss sich vom aktuellen Passwort unterscheiden"
    if error:
        return no_store(
            templates.TemplateResponse(
                request,
                "change_password.html",
                {"user": current, "csrf": csrf(request), "title": "Passwort ändern", "error": error},
                status_code=422,
            )
        )
    current.password_hash = hash_password(password)
    current.auth_version += 1
    current.failed_logins = 0
    current.locked_until = None
    db.add(
        AuditLog(
            user_id=current.id,
            action="password.changed",
            entity_type="user",
            entity_id=current.id,
            details={},
            ip=request_ip(request),
        )
    )
    db.commit()
    request.session.clear()
    return RedirectResponse("/login?notice=Passwort+erfolgreich+ge%C3%A4ndert", 303)


@app.get("/account/email", response_class=HTMLResponse)
def change_email_form(request: Request, current: User = Depends(current_user)):
    return no_store(
        templates.TemplateResponse(
            request,
            "change_email.html",
            {"user": current, "csrf": csrf(request), "title": "E-Mail-Adresse ändern"},
        )
    )


@app.post("/account/email", response_class=HTMLResponse)
def change_email(
    request: Request,
    current_password: str = Form(),
    new_email: str = Form(),
    csrf_token_value: str = Form(alias="csrf_token"),
    current: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    if not secrets.compare_digest(csrf_token_value, request.session.get("csrf", "")):
        raise HTTPException(403, "CSRF-Prüfung fehlgeschlagen")
    if not verify_password(current_password, current.password_hash):
        error = "Aktuelles Passwort ist nicht korrekt"
    else:
        try:
            result = request_email_change(
                db, settings, current, new_email, request_ip(request)
            )
        except ValueError as exc:
            error = str(exc)
        else:
            error = result.error
            if result.delivered:
                return no_store(
                    templates.TemplateResponse(
                        request,
                        "change_email.html",
                        {
                            "user": current,
                            "csrf": csrf(request),
                            "title": "E-Mail-Adresse ändern",
                            "success": (
                                "Bestätigungslink wurde an die bisherige E-Mail-Adresse "
                                f"{current.email} gesendet."
                            ),
                        },
                    )
                )
    return no_store(
        templates.TemplateResponse(
            request,
            "change_email.html",
            {
                "user": current,
                "csrf": csrf(request),
                "title": "E-Mail-Adresse ändern",
                "error": error,
                "new_email": new_email,
            },
            status_code=422,
        )
    )


@app.get("/account/email/confirm/{token}", response_class=HTMLResponse)
def confirm_email_form(token: str, request: Request, db: Session = Depends(get_db)):
    item = find_valid_email_change_token(db, token)
    return no_store(
        templates.TemplateResponse(
            request,
            "confirm_email.html",
            {
                "csrf": csrf(request),
                "token": token if item else None,
                "old_email": item.old_email if item else None,
                "new_email": item.new_email if item else None,
                "error": None if item else "Bestätigungslink ist ungültig oder abgelaufen",
            },
            status_code=200 if item else 400,
        )
    )


@app.post("/account/email/confirm/{token}", response_class=HTMLResponse)
def confirm_email(
    token: str,
    request: Request,
    csrf_token_value: str = Form(alias="csrf_token"),
    db: Session = Depends(get_db),
):
    if not secrets.compare_digest(csrf_token_value, request.session.get("csrf", "")):
        raise HTTPException(403, "CSRF-Prüfung fehlgeschlagen")
    item = find_valid_email_change_token(db, token)
    if item is None:
        error = "Bestätigungslink ist ungültig oder abgelaufen"
    else:
        try:
            complete_email_change(db, item, request_ip(request))
        except ValueError as exc:
            error = str(exc)
        else:
            request.session.clear()
            return RedirectResponse(
                "/login?notice=E-Mail-Adresse+erfolgreich+ge%C3%A4ndert.+Bitte+neu+anmelden.",
                303,
            )
    return no_store(
        templates.TemplateResponse(
            request,
            "confirm_email.html",
            {"csrf": csrf(request), "token": None, "error": error},
            status_code=400,
        )
    )


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    current: User | None = Depends(optional_current_user),
    db: Session = Depends(get_db),
):
    if current is None:
        return RedirectResponse("/login", 303)
    counts = {
        "teams": db.scalar(select(func.count()).select_from(Team)),
        "games": db.scalar(select(func.count()).select_from(Game)),
        "posts": db.scalar(select(func.count()).select_from(Post)),
        "jobs": db.scalar(select(func.count()).select_from(PublicationJob)),
        "generation_jobs": db.scalar(select(func.count()).select_from(GenerationJob)),
    }
    posts = db.scalars(select(Post).order_by(Post.updated_at.desc()).limit(10)).all()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"user": current, "counts": counts, "posts": posts, "csrf": csrf(request)},
    )


app.include_router(admin_router)
app.include_router(meta_router)
