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

from app.approvals.service import ApprovalError, approve, edit_text
from app.auth.service import allowed, hash_password, validate_new_password
from app.config import get_settings
from app.db import get_db
from app.games.identity import team_name_variants
from app.logos.service import (
    LogoValidationError,
    normalize_club_name,
    opponent_name,
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
    PromptTemplate,
    PublicationJob,
    PublicationMediaItem,
    Role,
    StoryRule,
    Team,
    User,
    UserTeam,
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
from app.web import berlin_datetime, check_csrf, csrf_token, current_user, require, require_admin

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
    return RedirectResponse(f"{path}?notice={message}", 303)


def _invalidate_posts_for_logo_change(
    db: Session, game: Game, reason: str
) -> list[str]:
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
        post.critical_warnings = list(
            dict.fromkeys([*(post.critical_warnings or []), warning])
        )
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
    uploader_ids = {
        logo.uploaded_by
        for versions in logo_versions.values()
        for logo in versions
    }
    uploaders = {user.id: user.email for user in db.scalars(select(User).where(User.id.in_(uploader_ids))).all()} if uploader_ids else {}
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
    if not fussball_url.startswith(("https://www.fussball.de/", "https://fussball.de/")):
        raise HTTPException(422, "Ungültige FUSSBALL.DE-URL")
    try:
        LocalStorageProvider(settings.media_root).resolve(media_subdir)
    except StorageError as e:
        raise HTTPException(422, str(e)) from e
    page = db.get(InstagramPage, instagram_page_id)
    if not page or not page.active:
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
            _audit_logo_approval_revocations(
                db, current, team.id, game.id, game_posts, reason
            )
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
            _audit_logo_approval_revocations(
                db, current, team.id, game.id, game_posts, reason
            )
    audit(
        db,
        current,
        f"team_logo.{action}",
        "logo_asset",
        logo.id,
        team.id,
        {
            "old_logo": {"id": old.id, "version": old.version} if old else None,
            "new_logo": (
                {"id": logo.id, "version": logo.version}
                if team.logo_asset_id
                else None
            ),
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
        roles=[Role.ADMIN, Role.APPROVER, Role.EDITOR, Role.VIEWER],
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
    item = User(
        email=email.lower(), password_hash=hash_password(password), role=role, all_teams=all_teams
    )
    db.add(item)
    db.flush()
    audit(db, current, "user.created", "user", item.id, details={"role": role.value})
    db.commit()
    return redirect("/users")


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
            raise HTTPException(409, "Der letzte aktive Administrator kann nicht herabgestuft werden")
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
            persist(
                validate_player_image(file.filename or "", file.content_type, content)
            )
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
    )

    require_admin(current)
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
        preview=None,
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
        image_safety_prefix,
        prompt_context,
        render_body,
        sample_facts,
    )

    check_csrf(request, csrf_token_value)
    require_admin(current)
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
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    from app.prompts.service import PromptValidationError, validate_template

    check_csrf(request, csrf_token_value)
    require_admin(current)
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
        validate_template(prompt_body)
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
        style_direction=style_direction.strip() or None,
        model=model.strip(),
        quality=quality,
        version=version,
    )
    db.add(item)
    db.flush()
    audit(
        db,
        current,
        "prompt.created",
        "prompt_template",
        item.id,
        details={"name": name, "version": version, "kind": prompt_kind, "media_kind": media_kind},
    )
    db.commit()
    return redirect("/prompts", f"Prompt {name} Version {version} gespeichert")


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
            select(StoryRule).where(StoryRule.team_id == selected.id).order_by(StoryRule.sort_order)
        ).all()
        if selected
        else []
    )
    pages = db.scalars(select(InstagramPage).where(InstagramPage.archived_at.is_(None))).all()
    prompt_items = db.scalars(
        select(PromptTemplate)
        .where(PromptTemplate.active.is_(True), PromptTemplate.archived_at.is_(None))
        .order_by(PromptTemplate.version.desc())
    ).all()
    latest = {}
    for prompt in prompt_items:
        latest.setdefault(
            (prompt.name, prompt.prompt_kind, prompt.post_type, prompt.media_kind), prompt
        )
    return render(
        request,
        "rules.html",
        current,
        teams=teams,
        selected=selected,
        stories=stories,
        pages=pages,
        prompts=list(latest.values()),
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
    reminder_feed_before_minutes: int = Form(default=360),
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
    if announcement_offset_direction not in {"before", "after"} or result_offset_direction not in {"before", "after"}:
        raise HTTPException(422, "Ungueltige Zeitrichtung")
    announcement_offset_minutes = (
        feed_before_minutes
        if announcement_offset_minutes is None
        else announcement_offset_minutes
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
    for value in [*announcement_weekday_times.values(), *result_weekday_times.values()]:
        try:
            parsed = datetime.strptime(value, "%H:%M")
        except ValueError as exc:
            raise HTTPException(422, "Ungueltige feste Uhrzeit") from exc
        if parsed.strftime("%H:%M") != value:
            raise HTTPException(422, "Ungueltige feste Uhrzeit")
    if announcement_timing_mode == "weekday_fixed" and len(announcement_weekday_times) != 7:
        raise HTTPException(422, "Fuer feste Ankuendigungszeiten sind alle sieben Wochentage erforderlich")
    if result_timing_mode == "weekday_fixed" and len(result_weekday_times) != 7:
        raise HTTPException(422, "Fuer feste Ergebniszeiten sind alle sieben Wochentage erforderlich")
    team.rules = {
        **team.rules,
        "announcement_enabled": announcement_enabled,
        "feed_before_minutes": feed_before_minutes,
        "announcement_timing_mode": announcement_timing_mode,
        "announcement_offset_direction": announcement_offset_direction,
        "announcement_offset_minutes": announcement_offset_minutes,
        "announcement_weekday_times": announcement_weekday_times,
        "late_approval": late_approval,
        "result_enabled": result_enabled,
        "result_wait_minutes": result_wait_minutes,
        "result_timing_mode": result_timing_mode,
        "result_offset_direction": result_offset_direction,
        "result_offset_minutes": result_offset_minutes,
        "result_weekday_times": result_weekday_times,
        "allow_provisional_games": allow_provisional_games,
        "automatic_sync_enabled": automatic_sync_enabled,
        "automatic_generation_enabled": automatic_generation_enabled,
        "reminder_enabled": reminder_enabled,
        "generation_lead_minutes": generation_lead_minutes,
        "generation_lead_days": generation_lead_days,
        "sync_interval_hours": sync_interval_hours,
        "result_poll_interval_minutes": result_poll_interval_minutes,
        "auto_approve_announcements": auto_approve_announcements,
        "auto_approve_results": auto_approve_results,
        "reminder_feed_before_minutes": reminder_feed_before_minutes,
        "image_prompt_feed": image_prompt_feed,
        "image_prompt_story": image_prompt_story,
        "text_prompt": text_prompt,
        "image_prompt_feed_result": result_image_prompt_feed,
        "image_prompt_story_result": result_image_prompt_story,
        "text_prompt_result": result_text_prompt,
        "style_direction": style_direction.strip(),
    }
    team.version += 1
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
    audit(db, current, "rules.updated", "team", team.id, team.id, team.rules)
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
    next_day: bool = Form(default=False),
    template: str = Form(),
    prompt_template: str = Form(default="default-image-story"),
    instagram_page_id: str = Form(default=""),
    reuse_media: bool = Form(default=False),
    sort_order: int = Form(default=0),
    current=Depends(current_user),
    db: Session = Depends(get_db),
):
    check_csrf(request, csrf_token_value)
    require_admin(current)
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
    item = StoryRule(
        team_id=team_id,
        name=name,
        post_type=post_type,
        reference=reference,
        direction=direction,
        offset_minutes=offset_minutes,
        fixed_time=fixed_time or None,
        timing_mode=timing_mode,
        weekday_times=weekday_times,
        next_day=next_day,
        template=template,
        prompt_template=prompt_template,
        instagram_page_id=instagram_page_id or None,
        reuse_media=reuse_media,
        sort_order=sort_order,
    )
    db.add(item)
    db.flush()
    audit(db, current, "story_rule.created", "story_rule", item.id, team_id)
    db.commit()
    return redirect(f"/rules?team_id={team_id}")


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
    planned_query = select(PublicationJob).where(
        PublicationJob.scheduled_at >= now,
        PublicationJob.scheduled_at <= planned_until,
        PublicationJob.status.notin_(
            [JobStatus.PUBLISHED, JobStatus.CANCELLED, JobStatus.SKIPPED]
        ),
    )
    if selected_kinds:
        published_query = published_query.where(PublicationJob.kind.in_(selected_kinds))
        planned_query = planned_query.where(PublicationJob.kind.in_(selected_kinds))

    published_jobs = [
        job
        for job in db.scalars(
            published_query.order_by(PublicationJob.published_at.desc())
        )
        if require_visible(db, current, job.team_id)
    ]
    planned_jobs = [
        job
        for job in db.scalars(
            planned_query.order_by(PublicationJob.scheduled_at)
        )
        if require_visible(db, current, job.team_id)
    ]

    calendar_jobs = [*published_jobs, *planned_jobs]
    post_ids = {job.post_id for job in calendar_jobs}
    game_ids = {job.game_id for job in calendar_jobs if job.game_id}
    page_ids = {job.instagram_page_id for job in calendar_jobs}
    calendar_posts = (
        {
            post.id: post
            for post in db.scalars(select(Post).where(Post.id.in_(post_ids)))
        }
        if post_ids
        else {}
    )
    games = (
        {
            game.id: game
            for game in db.scalars(select(Game).where(Game.id.in_(game_ids)))
        }
        if game_ids
        else {}
    )
    pages = (
        {
            page.id: page
            for page in db.scalars(
                select(InstagramPage).where(InstagramPage.id.in_(page_ids))
            )
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
    attention_count = sum(
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
        user_tags_by_image = parse_manual_user_tag_specs(
            user_tags_value, len(images), kind
        )
        validated = []
        for image, crop in zip(images, crop_specs, strict=True):
            content = await image.read(MAX_MANUAL_IMAGE_BYTES + 1)
            validated.append(
                validate_manual_image(
                    image.filename or "", image.content_type, content, kind, crop
                )
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
    jobs = db.scalars(
        select(PublicationJob)
        .where(PublicationJob.post_id == item.id)
        .order_by(PublicationJob.scheduled_at)
    ).all()
    pages = db.scalars(select(InstagramPage).where(InstagramPage.archived_at.is_(None))).all()
    current_media_asset = db.get(MediaAsset, item.media_asset_id) if item.media_asset_id else None
    alternative_media_assets = db.scalars(
        select(MediaAsset).where(
            MediaAsset.team_id == item.team_id,
            MediaAsset.active.is_(True),
            MediaAsset.available.is_(True),
            MediaAsset.reserved_game_id.is_(None),
            MediaAsset.uses == 0,
        ).order_by(MediaAsset.filename)
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
                reports = [renderer.validate(Path(media.media_path), "feed") for media in media_items]
                checks[job.id] = (
                    f"{len(reports)} PNGs geprüft – jeweils "
                    f"{reports[0]['width']} × {reports[0]['height']}"
                )
            else:
                report = renderer.validate(Path(job.media_path), job.kind)
                checks[job.id] = f"PNG geprüft – {report['width']} × {report['height']}"
        except ValueError as exc:
            checks[job.id] = f"Prüfung fehlgeschlagen – {exc}"
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
        logo_recompose=logo_recompose_availability(item, jobs),
        current_media_asset=current_media_asset,
        alternative_media_assets=alternative_media_assets,
        can_edit=allowed(db, current, "edit_post", item.team_id),
        can_generate=allowed(db, current, "generate", item.team_id),
        can_approve=allowed(db, current, "approve", item.team_id),
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
        raise HTTPException(
            422, "Die KI-Änderungsanweisung muss 10 bis 2000 Zeichen lang sein"
        )
    # Keep accepting ``revise_graphics`` for already open legacy forms. New
    # forms select the feed and each story explicitly.
    revise_feed = revise_feed or revise_graphics
    has_graphics = revise_feed or bool(story_job_ids)
    if not revise_text and not has_graphics:
        raise HTTPException(
            422, "Bitte Begleittext, Feed oder mindestens eine Story auswählen"
        )
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
        approve(db, item, current, job_ids or None)
    except ApprovalError as e:
        raise HTTPException(422, str(e)) from e
    return redirect(f"/posts/{item.id}", "Beitrag ausdrücklich freigegeben")


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
        f"Beitrag gelöscht; {result.publication_jobs} unveröffentlichte Aufträge entfernt",
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
            if not any(
                require_visible(db, current, team_id) for team_id in assigned_team_ids
            ):
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
        filename=logo.original_filename,
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
    uploader_ids = {logo.uploaded_by for logo in library}
    uploaders = {user.id: user.email for user in db.scalars(select(User).where(User.id.in_(uploader_ids))).all()} if uploader_ids else {}
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
            selected, created = store_logo(
                db,
                upload_root=settings.upload_root,
                logo_type="opponent",
                team_id=None,
                display_name=opponent_name(game, team),
                original_filename=file.filename,
                content_type=file.content_type,
                data=await file.read(),
                uploaded_by=current.id,
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
    elif action != "remove":
        raise HTTPException(422, "Unbekannte Logoaktion")
    if selected and selected.normalized_name != normalize_club_name(opponent_name(game, team)):
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
        _audit_logo_approval_revocations(
            db, current, game.team_id, game.id, affected, reason
        )
    action_name = (
        "opponent_logo.removed"
        if not selected
        else (
            "opponent_logo.uploaded"
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
            "new_logo": (
                {"id": selected.id, "version": selected.version} if selected else None
            ),
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
    return render(
        request,
        "games.html",
        current,
        teams=teams,
        items=items,
        logo_map=logo_map,
        opponents=opponents,
        suppressed_items=suppressed_items,
        title="Spiele und Testdaten",
    )


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
    home_team, away_team = (
        (own_name, opponent) if side == "home" else (opponent, own_name)
    )
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
                publication_job.error = "Spiel wurde im Dashboard gelöscht und für Provider-Importe unterdrückt"
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

    for asset in db.scalars(
        select(MediaAsset).where(MediaAsset.reserved_game_id == game.id)
    ):
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
    from app.jobs.generation import enqueue_create

    check_csrf(request, csrf_token_value)
    game = db.get(Game, game_id)
    if not game:
        raise HTTPException(404)
    require(current, db, "generate", game.team_id)
    team = db.get(Team, game.team_id)
    try:
        job, post = enqueue_create(db, game, team, current, post_type)
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
