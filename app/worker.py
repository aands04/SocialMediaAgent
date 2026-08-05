import json
import time
from datetime import datetime, timezone
from pathlib import Path

import structlog
from sqlalchemy import select

from app.config import Settings, get_settings
from app.db import SessionLocal
from app.games.automatic import run_automatic_fussball_cycle
from app.jobs.generation import claim_next, process_generation_job
from app.meta.publishing import MetaPublishingError
from app.meta.scheduler import run_automatic_publishing_cycle
from app.models import Club, ClubStatus, GenerationJob, JobStatus, PublicationJob
from app.publishing.service import DryRunPublisher, PublishError
from app.publishing.worker import process_job
from app.storage.providers import build_object_storage_provider
from app.storage.service import cleanup_expired_uploads
from app.tenancy.state import system_scope, tenant_scope

log = structlog.get_logger()
settings = get_settings()


def _automatic_scheduler_enabled(settings: Settings) -> bool:
    return all(
        [
            settings.environment == "production",
            settings.publisher_mode == "instagram",
            settings.meta_production_enabled,
            settings.global_publish_enabled,
            settings.meta_scheduler_enabled,
            settings.meta_automatic_publish_enabled,
            not settings.meta_test_enabled,
            not settings.meta_test_publish_enabled,
            not settings.meta_access_token,
        ]
    )


def _automatic_fussball_enabled(settings: Settings) -> bool:
    return settings.environment == "production" and settings.fussball_automatic_sync_enabled


def _validate_worker_environment(settings: Settings) -> str:
    if settings.environment == "staging":
        if (
            settings.publisher_mode != "dry-run"
            or settings.global_publish_enabled
            or settings.meta_access_token
            or settings.meta_scheduler_enabled
            or settings.meta_automatic_publish_enabled
            or settings.fussball_automatic_sync_enabled
            or settings.automatic_post_generation_enabled
        ):
            raise RuntimeError("Staging-Worker verweigert Live-Publishing")
        return "dry-run"

    if settings.environment == "meta-test":
        if (
            settings.publisher_mode != "instagram"
            or settings.global_publish_enabled
            or settings.meta_scheduler_enabled
            or settings.meta_automatic_publish_enabled
            or settings.fussball_automatic_sync_enabled
            or settings.automatic_post_generation_enabled
        ):
            raise RuntimeError("Automatische Instagram-Veröffentlichung ist im Meta-Test verboten")
        return "manual-meta-test"

    if settings.environment == "production":
        if settings.publisher_mode != "instagram" or not settings.meta_production_enabled:
            raise RuntimeError("Produktions-Worker ist nicht für Instagram freigegeben")
        automatic_flags = [
            settings.global_publish_enabled,
            settings.meta_scheduler_enabled,
            settings.meta_automatic_publish_enabled,
        ]
        if any(automatic_flags) and not all(automatic_flags):
            raise RuntimeError(
                "Automatische Veröffentlichung ist nur mit allen drei Gates zulässig"
            )
        if settings.meta_test_enabled or settings.meta_test_publish_enabled:
            raise RuntimeError("Meta-Test-Gates müssen in Produktion aus sein")
        if settings.meta_access_token:
            raise RuntimeError("Globaler META_ACCESS_TOKEN ist in Produktion verboten")
        if (
            settings.automatic_post_generation_enabled
            and not settings.fussball_automatic_sync_enabled
        ):
            raise RuntimeError(
                "Automatische Beitragserstellung erfordert den automatischen FUSSBALL.DE-Abruf"
            )
        return "automatic-instagram" if all(automatic_flags) else "production-paused"

    if settings.publisher_mode != "dry-run" or settings.global_publish_enabled:
        raise RuntimeError("Unbekannte Umgebung verweigert Live-Publishing")
    return "dry-run"


def run():
    worker_mode = _validate_worker_environment(settings)
    scheduler_enabled = _automatic_scheduler_enabled(settings)
    fussball_enabled = _automatic_fussball_enabled(settings)
    scheduler_active = worker_mode == "dry-run" or scheduler_enabled
    log.info(
        "worker_started",
        mode=worker_mode,
        scheduler=scheduler_enabled,
        fussball_sync=fussball_enabled,
    )
    loops = 0
    processed = 0
    generated = 0
    settings.log_root.mkdir(parents=True, exist_ok=True)
    dry_run_settings = Settings(
        **{
            **settings.model_dump(),
            "global_publish_enabled": True,
            "publisher_mode": "dry-run",
            "meta_access_token": None,
        }
    )

    while True:
        loops += 1
        due_count = 0
        automatic_result = None
        fussball_result = None
        with SessionLocal() as db:
            if loops == 1 or loops % 240 == 0:
                try:
                    with system_scope("Abgelaufene direkte Uploads bereinigen"):
                        expired_uploads = cleanup_expired_uploads(
                            db, build_object_storage_provider(settings)
                        )
                    if expired_uploads:
                        log.info("expired_direct_uploads_cleaned", count=expired_uploads)
                except Exception as exc:
                    db.rollback()
                    log.error("direct_upload_cleanup_failed", error=str(exc))
            if fussball_enabled:
                fussball_result = run_automatic_fussball_cycle(db, settings)
            generation_ids = []
            for _ in range(5):
                with system_scope("Generierungsauftrag global beanspruchen"):
                    generation_id = claim_next(db)
                if not generation_id:
                    break
                with system_scope("Mandant des Generierungsauftrags bestimmen"):
                    generation_job = db.get(GenerationJob, generation_id)
                    club = db.get(Club, generation_job.club_id) if generation_job else None
                if not generation_job or not club or club.status not in {
                    ClubStatus.ACTIVE,
                    ClubStatus.TRIAL,
                }:
                    log.warning(
                        "generation_job_blocked_by_club",
                        generation_job_id=generation_id,
                        club_id=generation_job.club_id if generation_job else None,
                    )
                    continue
                generation_ids.append(generation_id)
                with tenant_scope(generation_job.club_id, "system:generation-worker"):
                    result = process_generation_job(db, generation_id, settings)
                generated += int(result.status.value == "succeeded")

            if worker_mode == "dry-run":
                with system_scope("Dry-Run-Veröffentlichungen global ermitteln"):
                    due = list(
                        db.scalars(
                            select(PublicationJob)
                            .where(
                                PublicationJob.status.in_(
                                    [JobStatus.SCHEDULED, JobStatus.RETRY]
                                )
                            )
                            .limit(20)
                        )
                    )
                due_count = len(due)
                for job in due:
                    try:
                        with tenant_scope(job.club_id, "system:dry-run-worker"):
                            process_job(db, job.id, DryRunPublisher(), dry_run_settings)
                        processed += 1
                    except PublishError as exc:
                        log.warning("dry_run_job_blocked", job_id=job.id, error=str(exc))
            elif scheduler_enabled:
                try:
                    automatic_result = run_automatic_publishing_cycle(db, settings)
                    due_count = automatic_result.queued
                    processed += automatic_result.published
                except MetaPublishingError as exc:
                    db.rollback()
                    log.error("automatic_scheduler_blocked", error=str(exc))

        payload = {
            "at": datetime.now(timezone.utc).isoformat(),
            "loops": loops,
            "scheduler": scheduler_active,
            "automatic_scheduler": scheduler_enabled,
            "automatic_fussball_sync": fussball_enabled,
            "automatic_post_generation": (
                fussball_enabled and settings.automatic_post_generation_enabled
            ),
            "scheduler_mode": worker_mode,
            "due_jobs": due_count,
            "generation_jobs": len(generation_ids),
            "generated": generated,
            "processed": processed,
            "publisher": worker_mode,
            "automatic_cycle": (
                automatic_result.__dict__ if automatic_result is not None else None
            ),
            "fussball_cycle": (fussball_result.__dict__ if fussball_result is not None else None),
        }
        temporary = settings.log_root / "worker-heartbeat.tmp"
        temporary.write_text(json.dumps(payload))
        temporary.replace(settings.log_root / "worker-heartbeat.json")
        Path("/tmp/worker-heartbeat").touch()
        time.sleep(15)


if __name__ == "__main__":
    run()
