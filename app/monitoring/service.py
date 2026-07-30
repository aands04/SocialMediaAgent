import json
import shutil
from datetime import datetime, timezone

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    GenerationJob,
    GenerationJobStatus,
    JobStatus,
    Post,
    PostStatus,
    ProviderSnapshot,
    PublicationJob,
    SystemSetting,
)


def system_status(db: Session, settings: Settings) -> dict:
    checks = {}
    critical = []
    try:
        db.execute(text("select 1"))
        checks["postgresql"] = {"ok": True, "detail": db.bind.dialect.name}
    except Exception as exc:
        checks["postgresql"] = {"ok": False, "detail": str(exc)}
        critical.append("PostgreSQL")
    heartbeat = settings.log_root / "worker-heartbeat.json"
    try:
        data = json.loads(heartbeat.read_text())
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(data["at"])).total_seconds()
        worker_ok = age < 90
        checks["worker"] = {
            "ok": worker_ok,
            "detail": f"Heartbeat vor {int(age)}s, Läufe {data.get('loops', 0)}",
        }
        checks["scheduler"] = {
            "ok": worker_ok and data.get("scheduler", False),
            "detail": "aktiv" if data.get("scheduler") else "inaktiv",
        }
    except Exception as exc:
        checks["worker"] = {"ok": False, "detail": f"Heartbeat fehlt: {exc}"}
        checks["scheduler"] = {"ok": False, "detail": "kein Worker-Heartbeat"}
    if not checks["worker"]["ok"]:
        critical.append("Worker")
    media_ok = settings.media_root.is_dir() and settings.media_root.exists()
    checks["smb"] = {"ok": media_ok, "detail": str(settings.media_root)}
    if not media_ok:
        critical.append("SMB")
    usage = shutil.disk_usage(settings.generated_root)
    checks["disk"] = {
        "ok": usage.free > 512 * 1024 * 1024,
        "detail": f"{usage.free // (1024 * 1024)} MiB frei",
    }
    try:
        latest = db.scalar(select(ProviderSnapshot).order_by(ProviderSnapshot.fetched_at.desc()))
        checks["provider"] = {
            "ok": not latest or not latest.error,
            "detail": latest.error if latest and latest.error else "kein kritischer Parserfehler",
        }
    except Exception as exc:
        db.rollback()
        checks["provider"] = {"ok": False, "detail": f"Providerstatus nicht lesbar: {exc}"}
    openai_needed = (
        settings.text_generator_mode == "openai" or settings.image_generator_mode == "openai"
    )
    checks["openai"] = {
        "ok": not openai_needed or bool(settings.openai_api_key),
        "detail": f"Text: {settings.text_generator_mode}; Bild: {settings.image_generator_mode}; Bildmodell: {settings.openai_image_model}",
    }
    dry = (
        settings.publisher_mode == "dry-run"
        and not settings.global_publish_enabled
        and not settings.meta_access_token
    )
    checks["publishing"] = {
        "ok": dry,
        "detail": "DryRun aktiv; Live deaktiviert" if dry else "UNSICHERE KONFIGURATION",
    }
    if not dry:
        critical.append("Publishing")
    marker = settings.backup_root / "last-success.json"
    checks["backup"] = {
        "ok": marker.is_file(),
        "detail": marker.read_text()[:200]
        if marker.is_file()
        else "noch kein erfolgreicher Backup-Lauf",
    }
    try:
        counts = {
            "wartende_beitraege": db.scalar(
                select(func.count()).select_from(Post).where(Post.status == PostStatus.PENDING)
            ),
            "wartende_freigaben": db.scalar(
                select(func.count())
                .select_from(PublicationJob)
                .where(PublicationJob.status == JobStatus.UNAPPROVED)
            ),
            "fehlgeschlagene_jobs": db.scalar(
                select(func.count())
                .select_from(PublicationJob)
                .where(PublicationJob.status == JobStatus.FAILED)
            ),
        }
        generation_counts = {
            "queued": db.scalar(
                select(func.count())
                .select_from(GenerationJob)
                .where(
                    GenerationJob.status.in_(
                        [GenerationJobStatus.QUEUED, GenerationJobStatus.RETRY_WAIT]
                    )
                )
            ),
            "running": db.scalar(
                select(func.count())
                .select_from(GenerationJob)
                .where(GenerationJob.status == GenerationJobStatus.RUNNING)
            ),
            "failed": db.scalar(
                select(func.count())
                .select_from(GenerationJob)
                .where(GenerationJob.status == GenerationJobStatus.FAILED)
            ),
            "manual_review_required": db.scalar(
                select(func.count())
                .select_from(GenerationJob)
                .where(GenerationJob.status == GenerationJobStatus.MANUAL_REVIEW_REQUIRED)
            ),
        }
        oldest = db.scalar(
            select(GenerationJob)
            .where(
                GenerationJob.status.in_(
                    [GenerationJobStatus.QUEUED, GenerationJobStatus.RETRY_WAIT]
                )
            )
            .order_by(GenerationJob.created_at)
        )
        last_success = db.scalar(
            select(GenerationJob)
            .where(GenerationJob.status == GenerationJobStatus.SUCCEEDED)
            .order_by(GenerationJob.completed_at.desc())
        )
        long_running = db.scalar(
            select(GenerationJob)
            .where(GenerationJob.status == GenerationJobStatus.RUNNING)
            .order_by(GenerationJob.started_at)
        )
        if long_running and long_running.started_at:
            started = (
                long_running.started_at
                if long_running.started_at.tzinfo
                else long_running.started_at.replace(tzinfo=timezone.utc)
            )
            generation_counts["long_running_seconds"] = int(
                (datetime.now(timezone.utc) - started).total_seconds()
            )
        generation_counts["oldest_queued_at"] = oldest.created_at.isoformat() if oldest else None
        generation_counts["last_success_at"] = (
            last_success.completed_at.isoformat()
            if last_success and last_success.completed_at
            else None
        )
        checks["counts"] = {"ok": True, "detail": counts}
        checks["generation_jobs"] = {
            "ok": not long_running or generation_counts.get("long_running_seconds", 0) < 900,
            "detail": generation_counts,
        }
        stop = db.get(SystemSetting, "emergency_stop")
        checks["emergency_stop"] = {
            "ok": True,
            "detail": "aktiv" if stop and stop.value.get("enabled") else "aus",
        }
    except Exception as exc:
        db.rollback()
        checks["counts"] = {"ok": False, "detail": f"Schema nicht bereit: {exc}"}
        checks["emergency_stop"] = {"ok": False, "detail": "Schema nicht bereit"}
    return {"ok": not critical, "critical": critical, "checks": checks}
