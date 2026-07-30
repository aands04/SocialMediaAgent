import hashlib
from datetime import datetime, timedelta, timezone
from socket import gethostname

import structlog
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.auth.service import allowed
from app.config import Settings
from app.generation import build_renderer, build_text_generator
from app.logos.service import (
    LogoValidationError,
    frozen_logo_set,
    validate_frozen_file,
    validate_frozen_logo,
)
from app.models import (
    AuditLog,
    Game,
    GenerationJob,
    GenerationJobStatus,
    GenerationJobType,
    Post,
    PostStatus,
    StoryRule,
    Team,
    User,
)
from app.posts.service import (
    RerenderConflict,
    create_post,
    recompose_post_logos,
    rerender_post,
)

log = structlog.get_logger()
ACTIVE_STATUSES = {
    GenerationJobStatus.QUEUED,
    GenerationJobStatus.RUNNING,
    GenerationJobStatus.RETRY_WAIT,
}
TERMINAL_STATUSES = {
    GenerationJobStatus.SUCCEEDED,
    GenerationJobStatus.FAILED,
    GenerationJobStatus.CANCELLED,
    GenerationJobStatus.MANUAL_REVIEW_REQUIRED,
}
SAFE_RETRY_PHASES = {"preparing", "validating", "saving"}
LEASE_SECONDS = 300


class GenerationCancelled(RuntimeError):
    pass


class _ProgressTextGenerator:
    def __init__(self, inner, db: Session, job: GenerationJob):
        self.inner = inner
        self.db = db
        self.job = job
        self.is_ai = getattr(inner, "is_ai", False)

    def generate(self, facts):
        _phase(self.db, self.job, "generating_text", 10)
        result = self.inner.generate(facts)
        _check_cancel(self.db, self.job)
        return result


class _ProgressRenderer:
    def __init__(self, inner, db: Session, job: GenerationJob):
        self.inner = inner
        self.db = db
        self.job = job
        self.is_ai = getattr(inner, "is_ai", False)

    def render(self, kind, relative_path, context):
        phase = "generating_feed" if kind == "feed" else "generating_story"
        completed = self.job.completed_outputs
        progress = 20 + int(65 * completed / max(1, self.job.planned_outputs))
        _phase(self.db, self.job, phase, progress, completed)
        phase_progress = {
            "generating_ai_base": progress,
            "compositing_logos": min(84, progress + 4),
            "validating_final_media": min(88, progress + 7),
        }
        render_context = {
            **context,
            "_generation_phase": lambda name: _phase(
                self.db,
                self.job,
                name,
                phase_progress.get(name, progress),
                completed,
            ),
        }
        path = self.inner.render(kind, relative_path, render_context)
        _check_cancel(self.db, self.job)
        completed += 1
        progress = 20 + int(65 * completed / max(1, self.job.planned_outputs))
        _phase(self.db, self.job, phase, progress, completed)
        return path

    def __getattr__(self, name):
        return getattr(self.inner, name)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _audit(
    db: Session,
    job: GenerationJob,
    action: str,
    details: dict | None = None,
) -> None:
    db.add(
        AuditLog(
            user_id=job.requested_by,
            team_id=job.team_id,
            action=action,
            entity_type="generation_job",
            entity_id=job.id,
            details=details or {},
        )
    )


def _story_count(db: Session, team_id: str, post_type: str) -> int:
    return len(
        db.scalars(
            select(StoryRule.id).where(
                StoryRule.team_id == team_id,
                StoryRule.post_type == post_type,
                StoryRule.active.is_(True),
            )
        ).all()
    )


def enqueue_create(
    db: Session,
    game: Game,
    team: Team,
    user: User,
    post_type: str,
) -> tuple[GenerationJob | None, Post | None]:
    if game.status == "provisional" or (game.overrides or {}).get("automation_blocked"):
        raise ValueError("Vorläufige oder gesperrte Spiele dürfen nicht verarbeitet werden.")
    if post_type == "result" and not game.result_confirmed:
        raise ValueError("Das Ergebnis muss vor der Beitragserstellung bestätigt werden.")
    existing_post = db.scalar(
        select(Post).where(
            Post.game_id == game.id,
            Post.post_type == post_type,
            Post.active_key == "active",
        )
    )
    if existing_post:
        return None, existing_post
    key = f"create:{game.id}:{post_type}"
    existing_job = db.scalar(select(GenerationJob).where(GenerationJob.idempotency_key == key))
    if existing_job:
        return existing_job, None
    job = GenerationJob(
        job_type=GenerationJobType.CREATE_POST,
        game_id=game.id,
        team_id=team.id,
        post_type=post_type,
        requested_by=user.id,
        status=GenerationJobStatus.QUEUED,
        phase="preparing",
        planned_outputs=1 + _story_count(db, team.id, post_type),
        idempotency_key=key,
        active_key=key,
        parameters={"post_type": post_type, "logos": frozen_logo_set(db, game, team)},
    )
    db.add(job)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing_job = db.scalar(
            select(GenerationJob).where(
                or_(
                    GenerationJob.active_key == key,
                    GenerationJob.idempotency_key == key,
                )
            )
        )
        if existing_job:
            return existing_job, None
        raise
    _audit(db, job, "generation.queued", {"job_type": job.job_type.value})
    db.commit()
    return job, None


def enqueue_rerender(
    db: Session,
    post: Post,
    user: User,
    expected_version: int,
    story_job_ids: list[str],
) -> GenerationJob:
    selected = sorted(set(story_job_ids))
    selection_hash = hashlib.sha256(":".join(selected).encode()).hexdigest()[:16]
    key = f"rerender:{post.id}:v{expected_version}:{selection_hash}"
    active_key = f"rerender:{post.id}"
    existing = db.scalar(
        select(GenerationJob).where(
            or_(
                GenerationJob.active_key == active_key,
                GenerationJob.idempotency_key == key,
            )
        )
    )
    if existing:
        return existing
    job = GenerationJob(
        job_type=GenerationJobType.RERENDER_POST,
        game_id=post.game_id,
        team_id=post.team_id,
        post_id=post.id,
        post_type=post.post_type,
        requested_by=user.id,
        status=GenerationJobStatus.QUEUED,
        phase="preparing",
        planned_outputs=1 + len(selected),
        idempotency_key=key,
        active_key=active_key,
        parameters={
            "expected_post_version": expected_version,
            "story_job_ids": selected,
            "logos": frozen_logo_set(
                db, db.get(Game, post.game_id), db.get(Team, post.team_id)
            ),
        },
    )
    db.add(job)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(GenerationJob).where(
                or_(
                    GenerationJob.active_key == active_key,
                    GenerationJob.idempotency_key == key,
                )
            )
        )
        if existing:
            return existing
        raise
    _audit(db, job, "generation.queued", {"job_type": job.job_type.value})
    db.commit()
    return job


def enqueue_logo_recompose(
    db: Session,
    post: Post,
    user: User,
    expected_version: int,
    story_job_ids: list[str],
) -> GenerationJob:
    game=db.get(Game,post.game_id); team=db.get(Team,post.team_id)
    if not game or not team:
        raise ValueError("Spiel oder Mannschaft ist nicht mehr vorhanden.")
    logos=frozen_logo_set(db,game,team)
    if not logos.get("team"):
        raise ValueError("Bitte zuerst ein verifiziertes Mannschaftslogo zuordnen.")
    selected=sorted(set(story_job_ids))
    logo_key=":".join(
        [
            str((logos.get("team") or {}).get("id")),
            str((logos.get("team") or {}).get("version")),
            str((logos.get("opponent") or {}).get("id") or "fallback"),
            str((logos.get("opponent") or {}).get("version") or "0"),
        ]
    )
    selection_hash=hashlib.sha256(":".join(selected).encode()).hexdigest()[:12]
    key=f"recompose:{post.id}:v{expected_version}:{selection_hash}:{hashlib.sha256(logo_key.encode()).hexdigest()[:12]}"
    active_key=f"rerender:{post.id}"
    existing=db.scalar(
        select(GenerationJob).where(
            or_(GenerationJob.active_key==active_key,GenerationJob.idempotency_key==key)
        )
    )
    if existing:
        return existing
    job=GenerationJob(
        job_type=GenerationJobType.RERENDER_POST,
        game_id=post.game_id,
        team_id=post.team_id,
        post_id=post.id,
        post_type=post.post_type,
        requested_by=user.id,
        status=GenerationJobStatus.QUEUED,
        phase="loading_verified_logos",
        planned_outputs=1+len(selected),
        idempotency_key=key,
        active_key=active_key,
        parameters={
            "expected_post_version":expected_version,
            "story_job_ids":selected,
            "logos":logos,
            "recompose_only":True,
        },
    )
    db.add(job)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing=db.scalar(
            select(GenerationJob).where(
                or_(GenerationJob.active_key==active_key,GenerationJob.idempotency_key==key)
            )
        )
        if existing:
            return existing
        raise
    _audit(db,job,"generation.logo_recompose_queued",{"logos":logos})
    db.commit()
    return job


def recover_stale_jobs(db: Session, now: datetime | None = None) -> int:
    now = now or _now()
    stale = db.scalars(
        select(GenerationJob)
        .where(
            GenerationJob.status == GenerationJobStatus.RUNNING,
            GenerationJob.lease_expires_at.is_not(None),
            GenerationJob.lease_expires_at < now,
        )
        .with_for_update(skip_locked=db.bind.dialect.name == "postgresql")
    ).all()
    for job in stale:
        if job.phase.startswith("generating_"):
            job.status = GenerationJobStatus.MANUAL_REVIEW_REQUIRED
            job.error_category = "ambiguous_worker_exit"
            job.error_message = (
                "Der Worker endete während eines möglicherweise kostenpflichtigen "
                "API-Aufrufs. Zur Kostensicherheit erfolgt keine automatische Wiederholung."
            )
            job.completed_at = now
            job.active_key = None
            _audit(db, job, "generation.manual_review_required")
        else:
            job.status = GenerationJobStatus.QUEUED
            job.available_at = now
            job.error_category = "orphan_recovered"
            job.error_message = "Verwaister Auftrag wurde sicher erneut eingereiht."
            _audit(db, job, "generation.orphan_requeued")
        job.locked_by = None
        job.locked_at = None
        job.lease_expires_at = None
    if stale:
        db.commit()
    return len(stale)


def claim_next(
    db: Session,
    worker_id: str | None = None,
    now: datetime | None = None,
) -> str | None:
    now = now or _now()
    worker_id = worker_id or f"{gethostname()}-generation"
    recover_stale_jobs(db, now)
    query = (
        select(GenerationJob)
        .where(
            GenerationJob.status.in_([GenerationJobStatus.QUEUED, GenerationJobStatus.RETRY_WAIT]),
            GenerationJob.available_at <= now,
            GenerationJob.cancel_requested.is_(False),
        )
        .order_by(GenerationJob.created_at, GenerationJob.id)
        .limit(1)
    )
    if db.bind.dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    job = db.scalar(query)
    if not job:
        return None
    job.status = GenerationJobStatus.RUNNING
    job.phase = "preparing"
    job.attempts += 1
    job.started_at = job.started_at or now
    job.locked_by = worker_id
    job.locked_at = now
    job.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
    job.error_category = None
    job.error_message = None
    _audit(db, job, "generation.claimed", {"attempt": job.attempts})
    db.commit()
    return job.id


def _finish(
    db: Session,
    job: GenerationJob,
    status: GenerationJobStatus,
    *,
    category: str | None = None,
    message: str | None = None,
) -> None:
    job.status = status
    job.error_category = category
    job.error_message = message
    job.completed_at = _now() if status in TERMINAL_STATUSES else None
    job.active_key = None if status in TERMINAL_STATUSES else job.active_key
    job.locked_by = None
    job.locked_at = None
    job.lease_expires_at = None
    if status == GenerationJobStatus.SUCCEEDED:
        job.phase = "completed"
        job.progress = 100
        job.completed_outputs = job.planned_outputs
    _audit(db, job, f"generation.{status.value}", {"category": category})
    db.commit()


def _capture_partial_post(db: Session, job: GenerationJob) -> None:
    if job.job_type != GenerationJobType.CREATE_POST or job.result_post_id:
        return
    post = db.scalar(
        select(Post).where(
            Post.game_id == job.game_id,
            Post.post_type == job.post_type,
            Post.active_key == "active",
        )
    )
    if not post:
        return
    job.post_id = post.id
    job.result_post_id = post.id
    if post.status == PostStatus.CREATING:
        post.status = PostStatus.INCOMPLETE
        warning = "Die Hintergrundgenerierung wurde unterbrochen; manuelle Prüfung erforderlich."
        post.critical_warnings = list(dict.fromkeys([*(post.critical_warnings or []), warning]))


def _check_cancel(db: Session, job: GenerationJob) -> None:
    db.refresh(job, attribute_names=["cancel_requested"])
    if job.cancel_requested:
        raise GenerationCancelled("Der Auftrag wurde auf Benutzerwunsch abgebrochen.")


def _phase(
    db: Session,
    job: GenerationJob,
    phase: str,
    progress: int,
    completed_outputs: int | None = None,
) -> None:
    _check_cancel(db, job)
    job.phase = phase
    job.progress = max(0, min(100, progress))
    if completed_outputs is not None:
        job.completed_outputs = completed_outputs
    job.lease_expires_at = _now() + timedelta(seconds=LEASE_SECONDS)
    db.commit()


def process_generation_job(
    db: Session,
    job_id: str,
    settings: Settings,
) -> GenerationJob:
    job = db.get(GenerationJob, job_id)
    if not job or job.status != GenerationJobStatus.RUNNING:
        raise ValueError("Generierungsauftrag ist nicht beansprucht")
    try:
        user = db.get(User, job.requested_by)
        game = db.get(Game, job.game_id)
        team = db.get(Team, job.team_id)
        if not user or not user.active or user.archived_at:
            raise PermissionError("Der auslösende Benutzer ist nicht mehr aktiv.")
        if not allowed(db, user, "generate", job.team_id):
            raise PermissionError(
                "Der auslösende Benutzer besitzt keine Mannschaftsberechtigung mehr."
            )
        if not game or not team:
            raise ValueError("Spiel oder Mannschaft ist nicht mehr vorhanden.")
        if game.status in {"provisional", "cancelled", "postponed"} or (
            game.overrides or {}
        ).get("automation_blocked"):
            raise ValueError(
                "Das Spiel ist vorläufig, abgesagt, verschoben oder für Automatisierung gesperrt."
            )
        logos = dict((job.parameters or {}).get("logos") or {})
        if settings.image_generator_mode == "openai" and not logos.get("team"):
            raise LogoValidationError(
                "Eigenes Mannschaftslogo fehlt. Bitte zuerst ein verifiziertes Teamlogo zuordnen."
            )
        team_logo = validate_frozen_logo(db, logos.get("team"), "team")
        opponent_logo = validate_frozen_logo(db, logos.get("opponent"), "opponent")
        if team_logo and team.logo_asset_id != team_logo.id:
            raise LogoValidationError(
                "Das eingefrorene Mannschaftslogo ist dieser Mannschaft nicht mehr zugeordnet."
            )
        if opponent_logo and game.opponent_logo_id != opponent_logo.id:
            raise LogoValidationError(
                "Das eingefrorene Gegnerlogo ist diesem Spiel nicht mehr zugeordnet."
            )
        if (
            not opponent_logo
            and (logos.get("opponent") or {}).get("fallback")
            and game.opponent_logo_id
        ):
            raise LogoValidationError(
                "Die Gegnerlogo-Zuordnung wurde nach dem Einreihen geändert."
            )
        validate_frozen_file(team_logo, settings.upload_root)
        validate_frozen_file(opponent_logo, settings.upload_root)
        _phase(db, job, "preparing", 5)
        if job.job_type == GenerationJobType.CREATE_POST:
            renderer = _ProgressRenderer(build_renderer(settings), db, job)
            _phase(db, job, "generating_text", 10)
            post = create_post(
                db,
                game,
                team,
                _ProgressTextGenerator(build_text_generator(settings), db, job),
                renderer,
                job.post_type,
                logos,
            )
            job.result_post_id = post.id
            job.post_id = post.id
        else:
            post = db.get(Post, job.post_id)
            if not post:
                raise ValueError("Der zu rendernde Beitrag ist nicht mehr vorhanden.")
            expected = int(job.parameters.get("expected_post_version", 0))
            if post.version != expected:
                raise RerenderConflict(
                    "Der Beitrag wurde seit dem Einreihen verändert; es wurden keine "
                    "neuen Dateien erzeugt."
                )
            if job.parameters.get("recompose_only"):
                _phase(db, job, "compositing_logos", 35)
                post = recompose_post_logos(
                    db,
                    post,
                    list(job.parameters.get("story_job_ids", [])),
                    logos,
                )
                _audit(
                    db,
                    job,
                    "graphics.logos_recomposed",
                    {
                        "post_id": post.id,
                        "logo_snapshot": logos,
                        "openai_image_call": False,
                    },
                )
            else:
                renderer = _ProgressRenderer(build_renderer(settings), db, job)
                _phase(db, job, "generating_feed", 20)
                post = rerender_post(
                    db,
                    post,
                    renderer,
                    list(job.parameters.get("story_job_ids", [])),
                    logos,
                )
            job.result_post_id = post.id
            db.commit()
        _phase(db, job, "validating", 90, job.planned_outputs)
        _phase(db, job, "saving", 95, job.planned_outputs)
        _finish(db, job, GenerationJobStatus.SUCCEEDED)
    except GenerationCancelled as exc:
        db.rollback()
        job = db.get(GenerationJob, job_id)
        _capture_partial_post(db, job)
        _finish(
            db,
            job,
            GenerationJobStatus.CANCELLED,
            category="cancelled_by_user",
            message=str(exc),
        )
    except LogoValidationError as exc:
        db.rollback()
        job = db.get(GenerationJob, job_id)
        _capture_partial_post(db, job)
        _finish(
            db,
            job,
            GenerationJobStatus.MANUAL_REVIEW_REQUIRED,
            category="verified_logo_unavailable",
            message=str(exc),
        )
    except OperationalError as exc:
        db.rollback()
        job = db.get(GenerationJob, job_id)
        _capture_partial_post(db, job)
        if job.attempts < job.max_attempts and job.phase in SAFE_RETRY_PHASES:
            job.status = GenerationJobStatus.RETRY_WAIT
            job.available_at = _now() + timedelta(seconds=min(300, 15 * (2 ** (job.attempts - 1))))
            job.error_category = "database_transient"
            job.error_message = str(exc)[:2000]
            job.locked_by = None
            job.locked_at = None
            job.lease_expires_at = None
            _audit(db, job, "generation.retry_scheduled")
            db.commit()
        else:
            _finish(
                db,
                job,
                GenerationJobStatus.FAILED,
                category="database_error",
                message=str(exc)[:2000],
            )
    except Exception as exc:
        db.rollback()
        job = db.get(GenerationJob, job_id)
        _capture_partial_post(db, job)
        text = str(exc)
        ambiguous = (
            job.phase.startswith("generating_")
            and (
                settings.text_generator_mode == "openai"
                or settings.image_generator_mode == "openai"
            )
            and any(
                marker in text.lower()
                for marker in ("timeout", "timed out", "connection", "unknown", "closed")
            )
        )
        status = (
            GenerationJobStatus.MANUAL_REVIEW_REQUIRED if ambiguous else GenerationJobStatus.FAILED
        )
        category = (
            "ambiguous_external_response"
            if ambiguous
            else ("permission_changed" if isinstance(exc, PermissionError) else "generation_error")
        )
        _finish(db, job, status, category=category, message=text[:4000])
        log.warning(
            "generation_job_failed",
            job_id=job.id,
            status=status.value,
            category=category,
        )
    return db.get(GenerationJob, job_id)


def request_cancel(db: Session, job: GenerationJob) -> None:
    if job.status in TERMINAL_STATUSES:
        raise ValueError("Abgeschlossene Aufträge können nicht abgebrochen werden.")
    if job.status in {GenerationJobStatus.QUEUED, GenerationJobStatus.RETRY_WAIT}:
        job.cancel_requested = True
        _finish(
            db,
            job,
            GenerationJobStatus.CANCELLED,
            category="cancelled_by_user",
            message="Der wartende Auftrag wurde abgebrochen.",
        )
        return
    job.cancel_requested = True
    _audit(db, job, "generation.cancel_requested")
    db.commit()


def retry_job(db: Session, job: GenerationJob) -> None:
    if job.status not in {
        GenerationJobStatus.FAILED,
        GenerationJobStatus.MANUAL_REVIEW_REQUIRED,
    }:
        raise ValueError(
            "Nur fehlgeschlagene oder unklare Aufträge können erneut gestartet werden."
        )
    if job.job_type == GenerationJobType.CREATE_POST and job.result_post_id:
        raise ValueError(
            "Es existiert bereits ein unvollständiger Teilbeitrag. Prüfen Sie ihn "
            "manuell; eine kostenpflichtige Wiederholung wird nicht automatisch gestartet."
        )
    game = db.get(Game, job.game_id)
    team = db.get(Team, job.team_id)
    if game and team:
        job.parameters = {
            **(job.parameters or {}),
            "logos": frozen_logo_set(db, game, team),
        }
    active_key = (
        f"create:{job.game_id}:{job.post_type}"
        if job.job_type == GenerationJobType.CREATE_POST
        else f"rerender:{job.post_id}"
    )
    competing = db.scalar(
        select(GenerationJob).where(
            GenerationJob.active_key == active_key,
            GenerationJob.id != job.id,
        )
    )
    if competing:
        raise ValueError("Für diesen Inhalt läuft bereits ein anderer Auftrag.")
    job.status = GenerationJobStatus.QUEUED
    job.phase = "preparing"
    job.progress = 0
    job.cancel_requested = False
    job.error_category = None
    job.error_message = None
    job.completed_at = None
    job.available_at = _now()
    job.active_key = active_key
    _audit(db, job, "generation.manual_retry")
    db.commit()
