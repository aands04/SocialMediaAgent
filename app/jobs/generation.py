import hashlib
from datetime import datetime, timedelta, timezone
from socket import gethostname

import structlog
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.approvals.service import ApprovalError, approve
from app.auth.service import allowed
from app.config import Settings
from app.games.bundles import generation_bundle_games
from app.generation import build_renderer, build_text_generator
from app.logos.service import (
    LogoValidationError,
    frozen_logo_set,
    validate_frozen_file,
    validate_frozen_logo,
)
from app.models import (
    AccountType,
    AiPromptDispatch,
    AuditLog,
    Club,
    ClubStatus,
    Game,
    GenerationJob,
    GenerationJobStatus,
    GenerationJobType,
    Post,
    PostStatus,
    PublicationJob,
    StoryRule,
    Team,
    UsageLedgerEntry,
    UsageStatus,
    User,
)
from app.posts.club_carousel import ClubCarouselState, coordinate_club_matchday_feed
from app.posts.service import (
    RerenderConflict,
    create_matchday_bundle_posts,
    create_post,
    logo_recompose_preflight,
    recompose_post_logos,
    rerender_post,
    revise_post,
)
from app.usage.service import complete_usage, release_usage, reserve_usage

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


def _automatically_approve_created_outputs(
    db: Session,
    *,
    post: Post,
    user: User,
    team: Team,
    parameters: dict,
    carousel: ClubCarouselState,
) -> int:
    """Approve only outputs explicitly opted in by their owning team.

    A pending bundle may release its per-game stories immediately.  Its feed
    remains blocked until every member exists.  Once complete, the shared feed
    is auto-approved only when every participating team opted in.
    """
    if not carousel.active:
        if parameters.get("automatic_approval_requested"):
            approve(db, post, user)
            return 1
        return 0

    member_ids = list(carousel.member_post_ids) or [post.id]
    members = [db.get(Post, post_id) for post_id in member_ids]
    members = [item for item in members if item is not None]
    auto_key = (
        "auto_approve_results" if post.post_type == "result" else "auto_approve_announcements"
    )
    all_auto = bool(carousel.complete and members) and all(
        bool((owner.rules or {}).get(auto_key) if (owner := db.get(Team, item.team_id)) else False)
        for item in members
    )
    approved = 0
    for member in members:
        owner = db.get(Team, member.team_id)
        owner_auto = bool(owner and (owner.rules or {}).get(auto_key))
        jobs = list(db.scalars(select(PublicationJob).where(PublicationJob.post_id == member.id)))
        selected = [job.id for job in jobs if job.kind == "story" and owner_auto]
        if carousel.complete and member.id == carousel.primary_post_id and all_auto:
            selected.extend(job.id for job in jobs if job.kind == "carousel")
        if selected:
            approve(db, member, user, selected)
            approved += 1
    return approved


def _record_prompt_dispatch(
    db: Session,
    job: GenerationJob,
    *,
    prompt_kind: str,
    media_kind: str,
    rendered_prompt: str,
    model: str,
    call_index: int,
    prompt=None,
) -> AiPromptDispatch:
    """Persist the exact provider input outside tenant-visible post data."""
    if not rendered_prompt.strip():
        raise RuntimeError("Der an den KI-Anbieter zu sendende Prompt ist leer")
    attempt_number = max(1, int(job.attempts or 1))
    idempotency_key = (
        f"generation:{job.id}:attempt:{attempt_number}:"
        f"{prompt_kind}:{media_kind}:{call_index}"
    )
    checksum = hashlib.sha256(rendered_prompt.encode("utf-8")).hexdigest()
    existing = db.scalar(
        select(AiPromptDispatch).where(
            AiPromptDispatch.club_id == job.club_id,
            AiPromptDispatch.idempotency_key == idempotency_key
        )
    )
    if existing:
        if existing.prompt_checksum != checksum:
            raise RuntimeError(
                "Prompt eines bereits protokollierten KI-Aufrufs hat sich widersprüchlich geändert"
            )
        return existing
    item = AiPromptDispatch(
        club_id=job.club_id,
        generation_job_id=job.id,
        post_id=job.post_id,
        team_id=job.team_id,
        game_id=job.game_id,
        prompt_kind=prompt_kind,
        post_type=job.post_type,
        media_kind=media_kind,
        provider="openai",
        model=model,
        prompt_template_id=getattr(prompt, "template_id", None),
        prompt_name=getattr(prompt, "name", None),
        prompt_version=getattr(prompt, "version", None),
        prompt_checksum=checksum,
        rendered_prompt=rendered_prompt,
        attempt_number=attempt_number,
        call_index=call_index,
        status="dispatched",
        idempotency_key=idempotency_key,
    )
    db.add(item)
    db.flush()
    return item


class _ProgressTextGenerator:
    def __init__(self, inner, db: Session, job: GenerationJob):
        self.inner = inner
        self.db = db
        self.job = job
        self.is_ai = getattr(inner, "is_ai", False)
        self._calls = 0

    def _before_provider(
        self,
        rendered_prompt: str,
        model: str,
        prompt=None,
    ) -> tuple[UsageLedgerEntry | None, AiPromptDispatch | None]:
        if not self.is_ai:
            return None, None
        self._calls += 1
        entry = reserve_usage(
            self.db,
            club_id=self.job.club_id,
            generation_type="text",
            quantity=1,
            idempotency_key=f"generation:{self.job.id}:text:{self._calls}",
            provider="openai",
            model=model,
            user_id=self.job.requested_by,
            generation_job_id=self.job.id,
            post_id=self.job.post_id,
        )
        dispatch = _record_prompt_dispatch(
            self.db,
            self.job,
            prompt_kind="text",
            media_kind="none",
            rendered_prompt=rendered_prompt,
            model=model,
            call_index=self._calls,
            prompt=prompt,
        )
        if entry.status == UsageStatus.RESERVED:
            entry.status = UsageStatus.PROVIDER_PROCESSING
        self.db.commit()
        return entry, dispatch

    def _after_provider(
        self,
        entry: UsageLedgerEntry | None,
        dispatch: AiPromptDispatch | None,
    ) -> None:
        if entry and entry.status in {
            UsageStatus.RESERVED,
            UsageStatus.PROVIDER_PROCESSING,
        }:
            complete_usage(self.db, entry, actual_quantity=1, post_id=self.job.post_id)
        if dispatch:
            dispatch.status = "completed"
            dispatch.completed_at = _now()
        self.db.commit()

    def _failed_provider(self, dispatch: AiPromptDispatch | None, exc: Exception) -> None:
        if dispatch:
            dispatch.status = "failed"
            dispatch.error_summary = type(exc).__name__
            dispatch.completed_at = _now()
            self.db.commit()

    def generate(self, facts):
        _phase(self.db, self.job, "generating_text", 10)
        if not self.is_ai:
            result = self.inner.generate(facts)
            _check_cancel(self.db, self.job)
            return result
        prepared = self.inner.prepare_generate(facts)
        rendered, _version, model = prepared
        usage, dispatch = self._before_provider(rendered, model, facts.get("text_prompt"))
        try:
            result = self.inner.generate(facts)
        except Exception as exc:
            self._failed_provider(dispatch, exc)
            raise
        self._after_provider(usage, dispatch)
        _check_cancel(self.db, self.job)
        return result

    def revise(self, facts, current_text, instruction):
        _phase(self.db, self.job, "generating_text", 10)
        if not self.is_ai:
            result = self.inner.revise(facts, current_text, instruction)
            _check_cancel(self.db, self.job)
            return result
        rendered, _version, model = self.inner.prepare_revision(
            facts, current_text, instruction
        )
        usage, dispatch = self._before_provider(rendered, model)
        try:
            result = self.inner.revise(facts, current_text, instruction)
        except Exception as exc:
            self._failed_provider(dispatch, exc)
            raise
        self._after_provider(usage, dispatch)
        _check_cancel(self.db, self.job)
        return result


class _ProgressRenderer:
    def __init__(self, inner, db: Session, job: GenerationJob):
        self.inner = inner
        self.db = db
        self.job = job
        self.is_ai = getattr(inner, "is_ai", False)
        self._calls = 0

    def _before_provider(
        self, kind: str, context: dict
    ) -> tuple[UsageLedgerEntry | None, AiPromptDispatch | None]:
        if not self.is_ai:
            return None, None
        self._calls += 1
        prompt = context.get("image_prompt")
        model = str(getattr(prompt, "model", None) or "unknown")
        prompt_version = getattr(prompt, "version", None)
        entry = reserve_usage(
            self.db,
            club_id=self.job.club_id,
            generation_type="image",
            quantity=1,
            idempotency_key=f"generation:{self.job.id}:image:{self._calls}",
            provider="openai",
            model=model,
            user_id=self.job.requested_by,
            generation_job_id=self.job.id,
            post_id=self.job.post_id,
            prompt_version=prompt_version,
        )
        rendered_prompt = str(getattr(prompt, "rendered", ""))
        dispatch = (
            _record_prompt_dispatch(
                self.db,
                self.job,
                prompt_kind="image",
                media_kind=kind,
                rendered_prompt=rendered_prompt,
                model=model,
                call_index=self._calls,
                prompt=prompt,
            )
            if rendered_prompt
            else None
        )
        if entry.status == UsageStatus.RESERVED:
            entry.status = UsageStatus.PROVIDER_PROCESSING
        self.db.commit()
        return entry, dispatch

    def render(self, kind, relative_path, context):
        phase = "generating_feed" if kind == "feed" else "generating_story"
        completed = self.job.completed_outputs
        progress = 20 + int(65 * completed / max(1, self.job.planned_outputs))
        _phase(self.db, self.job, phase, progress, completed)
        phase_progress = {
            "generating_ai_base": progress,
            "generating_ai_composition": progress,
            "compositing_logos": min(84, progress + 4),
            "validating_final_media": min(88, progress + 7),
        }
        render_context = {
            **context,
            # Bind persisted AI outputs to the generation job.  A retry of the
            # same job may safely reuse its completed, costly result, while a
            # later rerender job must never adopt an older file that happens
            # to use the same media version path.
            "_generation_job_id": self.job.id,
            "_generation_phase": lambda name: _phase(
                self.db,
                self.job,
                name,
                phase_progress.get(name, progress),
                completed,
            ),
        }
        usage, dispatch = self._before_provider(kind, context)
        try:
            path = self.inner.render(kind, relative_path, render_context)
        except Exception as exc:
            if dispatch:
                dispatch.status = "failed"
                dispatch.error_summary = type(exc).__name__
                dispatch.completed_at = _now()
                self.db.commit()
            raise
        if usage and usage.status in {
            UsageStatus.RESERVED,
            UsageStatus.PROVIDER_PROCESSING,
        }:
            complete_usage(self.db, usage, actual_quantity=1, post_id=self.job.post_id)
        if dispatch:
            dispatch.status = "completed"
            dispatch.completed_at = _now()
        self.db.commit()
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


def _story_count(db: Session, team: Team, post_type: str) -> int:
    rows = list(
        db.scalars(
            select(StoryRule)
            .where(
                StoryRule.team_id == team.id,
                StoryRule.post_type == post_type,
                StoryRule.active.is_(True),
            )
            .order_by(StoryRule.sort_order, StoryRule.created_at, StoryRule.id)
        ).all()
    )
    count_key = f"{post_type}_story_output_count"
    if count_key not in (team.rules or {}):
        return len(rows) or (1 if post_type == "result" else 0)

    slots = {int(row.media_slot or 1) for row in rows}
    default_count = max(slots or ({1} if post_type == "result" else {0}))
    configured = max(
        0,
        min(
            10,
            int((team.rules or {}).get(f"{post_type}_story_output_count", default_count)),
        ),
    )
    rendered = len({slot for slot in slots if slot <= configured})
    return max(1, rendered) if post_type == "result" and configured else rendered


def _feed_count(team: Team, post_type: str) -> int:
    return max(
        0,
        min(10, int((team.rules or {}).get(f"{post_type}_feed_output_count", 1))),
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
        planned_outputs=_feed_count(team, post_type) + _story_count(db, team, post_type),
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


def enqueue_bundle_create(
    db: Session,
    game: Game,
    team: Team,
    user: User,
    post_type: str,
) -> tuple[GenerationJob | None, Post | None]:
    games, teams, bundle_key = generation_bundle_games(db, game, team, post_type)
    if not bundle_key or len(games) < 2:
        return enqueue_create(db, game, team, user, post_type)
    if post_type == "result":
        missing = [teams[item.team_id].display_name for item in games if not item.result_confirmed]
        if missing:
            raise ValueError(
                "Für den gemeinsamen Ergebnisbeitrag fehlen bestätigte Ergebnisse: "
                + ", ".join(missing)
            )
    for item in games:
        if item.status == "provisional" or (item.overrides or {}).get("automation_blocked"):
            raise ValueError("Vorläufige oder gesperrte Spiele dürfen nicht verarbeitet werden")
        if not allowed(db, user, "generate", item.team_id):
            raise PermissionError("Keine Generierungsberechtigung für alle verbundenen Spiele")
    game_ids = [item.id for item in games]
    existing_posts = list(
        db.scalars(
            select(Post).where(
                Post.game_id.in_(game_ids),
                Post.post_type == post_type,
                Post.active_key == "active",
            )
        )
    )
    if len(existing_posts) == len(games):
        by_game = {item.game_id: item for item in existing_posts}
        return None, by_game[games[0].id]
    if existing_posts:
        # Older versions allowed one member of a generated bundle to be
        # deleted independently. Open the surviving contribution instead of
        # trapping the user between a failed regeneration and an inaccessible
        # partial bundle. The detail page offers the guarded whole-bundle
        # cleanup; no existing AI result is deleted automatically.
        by_game = {item.game_id: item for item in existing_posts}
        existing = next(by_game[item.id] for item in games if item.id in by_game)
        return None, existing
    digest = hashlib.sha256(":".join(game_ids).encode("utf-8")).hexdigest()[:24]
    key = f"create-bundle:{post_type}:{digest}"
    existing_job = db.scalar(
        select(GenerationJob).where(
            or_(GenerationJob.active_key == key, GenerationJob.idempotency_key == key)
        )
    )
    if existing_job:
        return existing_job, None
    logos = {item.id: frozen_logo_set(db, item, teams[item.team_id]) for item in games}
    planned_outputs = sum(
        _feed_count(teams[item.team_id], post_type)
        + _story_count(db, teams[item.team_id], post_type)
        for item in games
    )
    job = GenerationJob(
        job_type=GenerationJobType.CREATE_POST,
        game_id=games[0].id,
        team_id=games[0].team_id,
        post_type=post_type,
        requested_by=user.id,
        status=GenerationJobStatus.QUEUED,
        phase="preparing",
        planned_outputs=planned_outputs,
        idempotency_key=key,
        active_key=key,
        parameters={
            "post_type": post_type,
            "matchday_bundle_key": bundle_key,
            "bundle_game_ids": game_ids,
            "logos_by_game": logos,
            "single_shared_text_prompt": True,
        },
    )
    db.add(job)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing_job = db.scalar(
            select(GenerationJob).where(
                or_(GenerationJob.active_key == key, GenerationJob.idempotency_key == key)
            )
        )
        if existing_job:
            return existing_job, None
        raise
    _audit(
        db,
        job,
        "generation.matchday_bundle_queued",
        {"game_ids": game_ids, "post_type": post_type, "bundle_key": bundle_key},
    )
    db.commit()
    return job, None


def enqueue_rerender(
    db: Session,
    post: Post,
    user: User,
    expected_version: int,
    story_job_ids: list[str],
    media_asset_id: str | None = None,
    *,
    rerender_feed: bool = True,
) -> GenerationJob:
    selected = sorted(set(story_job_ids))
    if not rerender_feed and not selected:
        raise ValueError("Bitte mindestens Feed oder eine Story auswählen")
    selection_hash = hashlib.sha256(repr((rerender_feed, selected)).encode()).hexdigest()[:16]
    asset_key = media_asset_id or post.media_asset_id or "neutral"
    key = f"rerender:{post.id}:v{expected_version}:{selection_hash}:{asset_key}"
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
        planned_outputs=int(rerender_feed) + len(selected),
        idempotency_key=key,
        active_key=active_key,
        parameters={
            "expected_post_version": expected_version,
            "rerender_feed": rerender_feed,
            "story_job_ids": selected,
            "media_asset_id": media_asset_id or post.media_asset_id,
            "logos": frozen_logo_set(db, db.get(Game, post.game_id), db.get(Team, post.team_id)),
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


def enqueue_ai_revision(
    db: Session,
    post: Post,
    user: User,
    expected_version: int,
    instruction: str,
    *,
    revise_text: bool,
    revise_graphics: bool,
    story_job_ids: list[str],
    media_asset_id: str | None = None,
    revise_feed: bool | None = None,
) -> GenerationJob:
    instruction = instruction.strip()
    if not 10 <= len(instruction) <= 2000:
        raise ValueError("Die KI-Änderungsanweisung muss 10 bis 2000 Zeichen lang sein")
    if revise_feed is None:
        revise_feed = revise_graphics
    if not post.game_id:
        raise ValueError("Nur spielbezogene KI-Beiträge können durch KI geändert werden")
    selected = sorted(set(story_job_ids))
    revise_graphics = bool(revise_feed or selected)
    if not revise_text and not revise_graphics:
        raise ValueError("Bitte Begleittext, Feed oder mindestens eine Story auswählen")
    digest = hashlib.sha256(
        repr(
            (
                expected_version,
                instruction,
                revise_text,
                revise_graphics,
                revise_feed,
                selected,
                media_asset_id or post.media_asset_id,
            )
        ).encode("utf-8")
    ).hexdigest()[:24]
    key = f"ai-revision:{post.id}:{digest}"
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
    game = db.get(Game, post.game_id)
    team = db.get(Team, post.team_id)
    if not game or not team:
        raise ValueError("Spiel oder Mannschaft ist nicht mehr vorhanden")
    job = GenerationJob(
        job_type=GenerationJobType.RERENDER_POST,
        game_id=game.id,
        team_id=team.id,
        post_id=post.id,
        post_type=post.post_type,
        requested_by=user.id,
        status=GenerationJobStatus.QUEUED,
        phase="preparing",
        planned_outputs=(int(revise_text) + int(revise_feed) + len(selected)),
        idempotency_key=key,
        active_key=active_key,
        parameters={
            "operation": "ai_revision",
            "expected_post_version": expected_version,
            "instruction": instruction,
            "revise_text": revise_text,
            "revise_graphics": revise_graphics,
            "revise_feed": revise_feed,
            "story_job_ids": selected,
            "media_asset_id": media_asset_id or post.media_asset_id,
            "logos": frozen_logo_set(db, game, team),
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
    _audit(
        db,
        job,
        "generation.ai_revision_queued",
        {
            "post_id": post.id,
            "revise_text": revise_text,
            "revise_graphics": revise_graphics,
            "revise_feed": revise_feed,
            "story_count": len(selected),
        },
    )
    db.commit()
    return job


def enqueue_logo_recompose(
    db: Session,
    post: Post,
    user: User,
    expected_version: int,
    story_job_ids: list[str],
) -> GenerationJob:
    game = db.get(Game, post.game_id)
    team = db.get(Team, post.team_id)
    if not game or not team:
        raise ValueError("Spiel oder Mannschaft ist nicht mehr vorhanden.")
    logos = frozen_logo_set(db, game, team)
    if not logos.get("team"):
        raise ValueError("Bitte zuerst ein verifiziertes Mannschaftslogo zuordnen.")
    selected = sorted(set(story_job_ids))
    logo_key = ":".join(
        [
            str((logos.get("team") or {}).get("id")),
            str((logos.get("team") or {}).get("version")),
            str((logos.get("opponent") or {}).get("id") or "fallback"),
            str((logos.get("opponent") or {}).get("version") or "0"),
        ]
    )
    selection_hash = hashlib.sha256(":".join(selected).encode()).hexdigest()[:12]
    key = f"recompose:{post.id}:v{expected_version}:{selection_hash}:{hashlib.sha256(logo_key.encode()).hexdigest()[:12]}"
    active_key = f"rerender:{post.id}"
    existing = db.scalar(
        select(GenerationJob).where(
            or_(GenerationJob.active_key == active_key, GenerationJob.idempotency_key == key)
        )
    )
    if existing:
        return existing
    publication_jobs = list(
        db.scalars(select(PublicationJob).where(PublicationJob.post_id == post.id))
    )
    logo_recompose_preflight(post, publication_jobs, selected)
    job = GenerationJob(
        job_type=GenerationJobType.RERENDER_POST,
        game_id=post.game_id,
        team_id=post.team_id,
        post_id=post.id,
        post_type=post.post_type,
        requested_by=user.id,
        status=GenerationJobStatus.QUEUED,
        phase="loading_verified_logos",
        planned_outputs=1 + len(selected),
        idempotency_key=key,
        active_key=active_key,
        parameters={
            "expected_post_version": expected_version,
            "story_job_ids": selected,
            "logos": logos,
            "recompose_only": True,
        },
    )
    db.add(job)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(GenerationJob).where(
                or_(GenerationJob.active_key == active_key, GenerationJob.idempotency_key == key)
            )
        )
        if existing:
            return existing
        raise
    _audit(db, job, "generation.logo_recompose_queued", {"logos": logos})
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


def _finalize_job_usage(db: Session, job: GenerationJob, post_id: str | None) -> None:
    """Attach completed ledger rows and release every unfinished reservation.

    A provider timeout is deliberately not charged to the club when no
    technically usable result was returned to the application. Provider costs
    can still be recorded on the non-billable row by a later reconciliation.
    """
    entries = list(
        db.scalars(
            select(UsageLedgerEntry).where(UsageLedgerEntry.generation_job_id == job.id)
        )
    )
    for dispatch in db.scalars(
        select(AiPromptDispatch).where(AiPromptDispatch.generation_job_id == job.id)
    ):
        dispatch.post_id = post_id or dispatch.post_id
    for entry in entries:
        if entry.status in {
            UsageStatus.COMPLETED_BILLABLE,
            UsageStatus.COMPLETED_NOT_BILLABLE,
            UsageStatus.REJECTED_BY_USER,
        }:
            entry.post_id = post_id or entry.post_id
        elif entry.status in {UsageStatus.RESERVED, UsageStatus.PROVIDER_PROCESSING}:
            release_usage(
                entry,
                technical=True,
                details={"reason": "generation_job_ended_without_usable_result"},
            )


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
        club = db.get(Club, job.club_id)
        user = db.get(User, job.requested_by)
        game = db.get(Game, job.game_id)
        team = db.get(Team, job.team_id)
        if not club or club.status not in {ClubStatus.ACTIVE, ClubStatus.TRIAL}:
            raise PermissionError(
                "Der Verein ist gesperrt; die Generierung wurde vor dem nächsten "
                "kostenpflichtigen Schritt beendet."
            )
        if not user or not user.active or user.archived_at:
            raise PermissionError("Der auslösende Benutzer ist nicht mehr aktiv.")
        if user.account_type != AccountType.CLUB_USER or user.club_id != job.club_id:
            raise PermissionError("Der auslösende Benutzer gehört nicht mehr zu diesem Verein.")
        if not allowed(db, user, "generate", job.team_id):
            raise PermissionError(
                "Der auslösende Benutzer besitzt keine Mannschaftsberechtigung mehr."
            )
        if not game or not team:
            raise ValueError("Spiel oder Mannschaft ist nicht mehr vorhanden.")
        if game.club_id != job.club_id or team.club_id != job.club_id:
            raise PermissionError("Spiel oder Mannschaft gehört nicht zum Generierungsverein.")
        if game.status in {"provisional", "cancelled", "postponed"} or (game.overrides or {}).get(
            "automation_blocked"
        ):
            raise ValueError(
                "Das Spiel ist vorläufig, abgesagt, verschoben oder für Automatisierung gesperrt."
            )
        parameters = dict(job.parameters or {})
        bundle_game_ids = list(parameters.get("bundle_game_ids") or [])
        if job.job_type == GenerationJobType.CREATE_POST and bundle_game_ids:
            bundle_games = [db.get(Game, item_id) for item_id in bundle_game_ids]
            if any(item is None for item in bundle_games):
                raise ValueError("Mindestens ein verbundenes Spiel ist nicht mehr vorhanden")
            if [item.id for item in bundle_games] != bundle_game_ids:
                raise ValueError("Die eingefrorene Reihenfolge der verbundenen Spiele ist ungültig")
            bundle_teams = {item.team_id: db.get(Team, item.team_id) for item in bundle_games}
            for item in bundle_games:
                member_team = bundle_teams.get(item.team_id)
                if not member_team or item.club_id != job.club_id or member_team.club_id != job.club_id:
                    raise PermissionError("Ein verbundenes Spiel gehört nicht zum Generierungsverein")
                if not allowed(db, user, "generate", item.team_id):
                    raise PermissionError(
                        "Der auslösende Benutzer besitzt nicht mehr alle Mannschaftsrechte"
                    )
                if item.status in {"provisional", "cancelled", "postponed"} or (
                    item.overrides or {}
                ).get("automation_blocked"):
                    raise ValueError("Ein verbundenes Spiel ist gesperrt oder nicht mehr regulär")
                if job.post_type == "result" and not item.result_confirmed:
                    raise ValueError("Nicht alle verbundenen Ergebnisse sind bestätigt")
            logos_by_game = dict(parameters.get("logos_by_game") or {})
            for item in bundle_games:
                frozen = dict(logos_by_game.get(item.id) or {})
                team_logo = validate_frozen_logo(db, frozen.get("team"), "team")
                opponent_logo = validate_frozen_logo(db, frozen.get("opponent"), "opponent")
                member_team = bundle_teams[item.team_id]
                if settings.image_generator_mode == "openai" and not team_logo:
                    raise LogoValidationError(
                        f"Eigenes Mannschaftslogo fehlt: {member_team.display_name}"
                    )
                if team_logo and member_team.logo_asset_id != team_logo.id:
                    raise LogoValidationError(
                        f"Mannschaftslogo wurde geändert: {member_team.display_name}"
                    )
                if opponent_logo and item.opponent_logo_id != opponent_logo.id:
                    raise LogoValidationError(
                        f"Gegnerlogo wurde nach dem Einreihen geändert: {member_team.display_name}"
                    )
                validate_frozen_file(team_logo, settings.upload_root)
                validate_frozen_file(opponent_logo, settings.upload_root)
            _phase(db, job, "preparing", 5)
            posts = create_matchday_bundle_posts(
                db,
                bundle_games,
                bundle_teams,
                _ProgressTextGenerator(build_text_generator(settings), db, job),
                _ProgressRenderer(build_renderer(settings), db, job),
                job.post_type,
                logos_by_game,
                str(parameters.get("matchday_bundle_key") or job.id),
            )
            carousel = coordinate_club_matchday_feed(
                db, posts[-1], requested_by=job.requested_by
            )
            post = db.get(Post, carousel.primary_post_id) if carousel.primary_post_id else posts[0]
            if not post:
                raise ValueError("Der gemeinsame Hauptbeitrag konnte nicht bestimmt werden")
            job.result_post_id = post.id
            job.post_id = post.id
            job.parameters = {
                **parameters,
                "result_post_ids": [item.id for item in posts],
                "carousel_primary_post_id": carousel.primary_post_id,
            }
            db.commit()
            _phase(db, job, "validating", 90, job.planned_outputs)
            _phase(db, job, "saving", 95, job.planned_outputs)
            _finalize_job_usage(db, job, post.id)
            _finish(db, job, GenerationJobStatus.SUCCEEDED)
            return db.get(GenerationJob, job_id)
        logos = dict(parameters.get("logos") or {})
        needs_images = (
            job.job_type == GenerationJobType.CREATE_POST
            or parameters.get("recompose_only")
            or parameters.get("operation") != "ai_revision"
            or bool(parameters.get("revise_graphics"))
        )
        if needs_images and settings.image_generator_mode == "openai" and not logos.get("team"):
            raise LogoValidationError(
                "Eigenes Mannschaftslogo fehlt. Bitte zuerst ein verifiziertes Teamlogo zuordnen."
            )
        team_logo = validate_frozen_logo(db, logos.get("team"), "team") if needs_images else None
        opponent_logo = (
            validate_frozen_logo(db, logos.get("opponent"), "opponent") if needs_images else None
        )
        if team_logo and team.logo_asset_id != team_logo.id:
            raise LogoValidationError(
                "Das eingefrorene Mannschaftslogo ist dieser Mannschaft nicht mehr zugeordnet."
            )
        if opponent_logo and game.opponent_logo_id != opponent_logo.id:
            raise LogoValidationError(
                "Das eingefrorene Gegnerlogo ist diesem Spiel nicht mehr zugeordnet."
            )
        if needs_images and (
            not opponent_logo
            and (logos.get("opponent") or {}).get("fallback")
            and game.opponent_logo_id
        ):
            raise LogoValidationError("Die Gegnerlogo-Zuordnung wurde nach dem Einreihen geändert.")
        if needs_images:
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
            carousel = coordinate_club_matchday_feed(
                db,
                post,
                requested_by=job.requested_by,
            )
            db.commit()
        else:
            carousel = ClubCarouselState()
            post = db.get(Post, job.post_id)
            if not post:
                raise ValueError("Der zu rendernde Beitrag ist nicht mehr vorhanden.")
            expected = int(job.parameters.get("expected_post_version", 0))
            if post.version != expected:
                raise RerenderConflict(
                    "Der Beitrag wurde seit dem Einreihen verändert; es wurden keine "
                    "neuen Dateien erzeugt."
                )
            if parameters.get("operation") == "ai_revision":
                post = revise_post(
                    db,
                    post,
                    instruction=str(parameters.get("instruction") or ""),
                    revise_text=bool(parameters.get("revise_text")),
                    revise_graphics=bool(parameters.get("revise_graphics")),
                    rerender_feed=bool(
                        parameters.get("revise_feed", parameters.get("revise_graphics"))
                    ),
                    text_generator=(
                        _ProgressTextGenerator(build_text_generator(settings), db, job)
                        if parameters.get("revise_text")
                        else None
                    ),
                    renderer=(
                        _ProgressRenderer(build_renderer(settings), db, job)
                        if parameters.get("revise_graphics")
                        else None
                    ),
                    story_job_ids=list(parameters.get("story_job_ids", [])),
                    logo_snapshot=logos,
                    media_asset_id=parameters.get("media_asset_id"),
                )
                _audit(
                    db,
                    job,
                    "post.ai_revision_completed",
                    {
                        "post_id": post.id,
                        "revise_text": bool(parameters.get("revise_text")),
                        "revise_graphics": bool(parameters.get("revise_graphics")),
                    },
                )
            elif job.parameters.get("recompose_only"):
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
                    job.parameters.get("media_asset_id"),
                    rerender_feed=bool(job.parameters.get("rerender_feed", True)),
                )
            job.result_post_id = post.id
            db.commit()
        _phase(db, job, "validating", 90, job.planned_outputs)
        _phase(db, job, "saving", 95, job.planned_outputs)
        _finalize_job_usage(db, job, post.id)
        if (
            job.job_type == GenerationJobType.CREATE_POST
            and parameters.get("trigger_mode") == "automatic_fussball"
        ):
            try:
                approved_count = _automatically_approve_created_outputs(
                    db,
                    post=post,
                    user=user,
                    team=team,
                    parameters=parameters,
                    carousel=carousel,
                )
                if approved_count:
                    db.add(
                        AuditLog(
                            user_id=user.id,
                            team_id=team.id,
                            action="post.approved_automatically",
                            entity_type="post",
                            entity_id=post.id,
                            details={
                                "generation_job_id": job.id,
                                "post_type": post.post_type,
                                "rule_opt_in": bool(parameters.get("automatic_approval_requested")),
                                "club_carousel_active": carousel.active,
                                "club_carousel_complete": carousel.complete,
                            },
                        )
                    )
                db.commit()
            except ApprovalError as exc:
                db.rollback()
                db.add(
                    AuditLog(
                        user_id=None,
                        team_id=team.id,
                        action="post.automatic_approval_blocked",
                        entity_type="post",
                        entity_id=post.id,
                        details={
                            "generation_job_id": job.id,
                            "reason": str(exc)[:500],
                        },
                    )
                )
                db.commit()
                log.warning(
                    "automatic_post_approval_blocked",
                    job_id=job.id,
                    post_id=post.id,
                    reason=str(exc),
                )
        _finish(db, job, GenerationJobStatus.SUCCEEDED)
    except GenerationCancelled as exc:
        db.rollback()
        job = db.get(GenerationJob, job_id)
        _capture_partial_post(db, job)
        _finalize_job_usage(db, job, job.result_post_id)
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
        _finalize_job_usage(db, job, job.result_post_id)
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
            _finalize_job_usage(db, job, job.result_post_id)
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
        _finalize_job_usage(db, job, job.result_post_id)
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
