import json
import time
from datetime import datetime, timezone
from pathlib import Path

import structlog
from sqlalchemy import select

from app.config import Settings, get_settings
from app.db import SessionLocal
from app.jobs.generation import claim_next, process_generation_job
from app.models import JobStatus, PublicationJob
from app.publishing.service import DryRunPublisher, PublishError
from app.publishing.worker import process_job

log = structlog.get_logger()
settings = get_settings()


def run():
    if settings.environment == "staging" and (
        settings.publisher_mode != "dry-run" or settings.meta_access_token
    ):
        raise RuntimeError("Staging-Worker verweigert Live-Publishing")
    if settings.publisher_mode != "dry-run":
        raise RuntimeError(
            "Dieser Worker-Build ist für Staging ausschließlich im Dry-Run freigegeben"
        )
    log.info("worker_started", mode=settings.publisher_mode)
    loops = 0
    processed = 0
    generated = 0
    settings.log_root.mkdir(parents=True, exist_ok=True)
    effective = Settings(
        **{
            **settings.model_dump(),
            "global_publish_enabled": True,
            "publisher_mode": "dry-run",
            "meta_access_token": None,
        }
    )
    while True:
        loops += 1
        with SessionLocal() as db:
            generation_ids = []
            for _ in range(5):
                generation_id = claim_next(db)
                if not generation_id:
                    break
                generation_ids.append(generation_id)
                result = process_generation_job(db, generation_id, settings)
                generated += int(result.status.value == "succeeded")
            due = list(
                db.scalars(
                    select(PublicationJob)
                    .where(PublicationJob.status.in_([JobStatus.SCHEDULED, JobStatus.RETRY]))
                    .limit(20)
                )
            )
            for job in due:
                try:
                    process_job(db, job.id, DryRunPublisher(), effective)
                    processed += 1
                except PublishError as exc:
                    log.warning("dry_run_job_blocked", job_id=job.id, error=str(exc))
        payload = {
            "at": datetime.now(timezone.utc).isoformat(),
            "loops": loops,
            "scheduler": True,
            "due_jobs": len(due),
            "generation_jobs": len(generation_ids),
            "generated": generated,
            "processed": processed,
            "publisher": "dry-run",
        }
        temporary = settings.log_root / "worker-heartbeat.tmp"
        temporary.write_text(json.dumps(payload))
        temporary.replace(settings.log_root / "worker-heartbeat.json")
        Path("/tmp/worker-heartbeat").touch()
        time.sleep(15)


if __name__ == "__main__":
    run()
