import json
import shutil
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    FussballSyncState,
    GenerationJob,
    GenerationJobStatus,
    InstagramConnection,
    InstagramPage,
    JobStatus,
    MetaPublishingAttempt,
    Post,
    PostStatus,
    ProviderSnapshot,
    PublicationJob,
    PublicMediaGrant,
    SocialChannelConnection,
    SystemSetting,
    Team,
)
from app.monitoring.health_rules import (
    SOCIAL_CHANNEL_TYPES,
    SOCIAL_CONNECTION_STATUSES,
    SOCIAL_HEALTH_REASONS,
    SocialHealthSnapshot,
    SocialHealthState,
    classify_fussball_provider_error,
    fussball_retry_scheduled,
    fussball_sync_interval_hours,
    social_connection_health_reasons,
)
from app.monitoring.health_rules import (
    fussball_sync_stale_reason as _fussball_sync_stale_reason,
)

_MISSING_SOCIAL_HEALTH_STATE = SocialHealthSnapshot(
    status="unknown",
    last_check_at=None,
    last_success_at=None,
)


def _social_channel_health_detail(
    active_items: list[SocialHealthState],
    enabled_items: list[SocialHealthState],
    *,
    stale_before: datetime,
) -> tuple[dict, bool]:
    assessments = [
        (
            item,
            social_connection_health_reasons(
                item,
                stale_before=stale_before,
            ),
        )
        for item in enabled_items
    ]
    unhealthy = [item for item, reasons in assessments if reasons]
    health_reason_counts = {
        reason: sum(reason in reasons for _, reasons in assessments)
        for reason in SOCIAL_HEALTH_REASONS
    }
    status_counts = {
        status: sum(item.status == status for item in enabled_items)
        for status in SOCIAL_CONNECTION_STATUSES
    }
    unknown_statuses = sum(item.status not in SOCIAL_CONNECTION_STATUSES for item in enabled_items)
    status_counts = {status: count for status, count in status_counts.items() if count}
    if unknown_statuses:
        status_counts["unknown"] = unknown_statuses
    return (
        {
            "active_connections": len(active_items),
            "enabled_connections": len(enabled_items),
            "unhealthy_connections": len(unhealthy),
            "non_connected_connections": health_reason_counts["non_connected"],
            "missing_last_success": health_reason_counts["missing_last_success"],
            "stale_last_success": health_reason_counts["stale_last_success"],
            "last_check_at": max(
                (item.last_check_at for item in enabled_items if item.last_check_at),
                default=None,
            ),
            "last_successful_check": max(
                (item.last_success_at for item in enabled_items if item.last_success_at),
                default=None,
            ),
            "status_counts": status_counts,
        },
        not unhealthy,
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
        automatic_scheduler_expected = all(
            [
                settings.environment == "production",
                settings.publisher_mode == "instagram",
                settings.meta_production_enabled,
                settings.global_publish_enabled,
                settings.meta_scheduler_enabled,
                settings.meta_automatic_publish_enabled,
            ]
        )
        scheduler_expected = settings.publisher_mode == "dry-run" or automatic_scheduler_expected
        checks["scheduler"] = {
            "ok": worker_ok
            and (
                bool(data.get("scheduler"))
                if scheduler_expected
                else not bool(data.get("scheduler"))
            ),
            "detail": (
                "aktiv"
                if data.get("scheduler")
                else ("bewusst deaktiviert" if not scheduler_expected else "inaktiv")
            ),
        }
        checks["automatic_scheduler"] = {
            "ok": worker_ok
            and bool(data.get("automatic_scheduler")) == automatic_scheduler_expected,
            "detail": ("kontrolliert aktiv" if data.get("automatic_scheduler") else "deaktiviert"),
        }
        automatic_fussball_expected = (
            settings.environment == "production" and settings.fussball_automatic_sync_enabled
        )
        checks["automatic_fussball_sync"] = {
            "ok": worker_ok
            and bool(data.get("automatic_fussball_sync")) == automatic_fussball_expected,
            "detail": {
                "sync": "aktiv" if data.get("automatic_fussball_sync") else "deaktiviert",
                "draft_generation": (
                    "aktiv" if data.get("automatic_post_generation") else "deaktiviert"
                ),
                "last_cycle": data.get("fussball_cycle"),
            },
        }
    except Exception as exc:
        checks["worker"] = {"ok": False, "detail": f"Heartbeat fehlt: {exc}"}
        checks["scheduler"] = {"ok": False, "detail": "kein Worker-Heartbeat"}
        checks["automatic_scheduler"] = {
            "ok": False,
            "detail": "kein Worker-Heartbeat",
        }
        checks["automatic_fussball_sync"] = {
            "ok": False,
            "detail": "kein Worker-Heartbeat",
        }
    if not checks["worker"]["ok"]:
        critical.append("Worker")
    if not checks["scheduler"]["ok"]:
        critical.append("Scheduler")
    if not checks["automatic_scheduler"]["ok"]:
        critical.append("Automatischer Instagram-Scheduler")
    if not checks["automatic_fussball_sync"]["ok"]:
        critical.append("Automatischer FUSSBALL.DE-Abruf")
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
        settings.environment not in {"meta-test", "production"}
        and settings.publisher_mode == "dry-run"
        and not settings.global_publish_enabled
        and not settings.meta_access_token
    )
    guarded_meta_test = (
        settings.environment == "meta-test"
        and settings.publisher_mode == "instagram"
        and settings.meta_test_enabled
        and not settings.meta_scheduler_enabled
        and not settings.global_publish_enabled
        and not settings.meta_automatic_publish_enabled
    )
    production_paused = (
        settings.environment == "production"
        and settings.publisher_mode == "instagram"
        and settings.meta_production_enabled
        and not settings.meta_test_enabled
        and not settings.meta_test_publish_enabled
        and not settings.global_publish_enabled
        and not settings.meta_scheduler_enabled
        and not settings.meta_automatic_publish_enabled
    )
    production_automatic = (
        settings.environment == "production"
        and settings.publisher_mode == "instagram"
        and settings.meta_production_enabled
        and not settings.meta_test_enabled
        and not settings.meta_test_publish_enabled
        and settings.global_publish_enabled
        and settings.meta_scheduler_enabled
        and settings.meta_automatic_publish_enabled
    )
    checks["publishing"] = {
        "ok": dry or guarded_meta_test or production_paused or production_automatic,
        "detail": (
            "DryRun aktiv; Live deaktiviert"
            if dry
            else (
                "Meta-Test: ausschließlich manueller Assistent"
                if guarded_meta_test
                else (
                    "Produktion vorbereitet; Automatik pausiert"
                    if production_paused
                    else (
                        "Produktion: kontrollierte Automatik aktiv"
                        if production_automatic
                        else "UNSICHERE KONFIGURATION"
                    )
                )
            )
        ),
    }
    if not (dry or guarded_meta_test or production_paused or production_automatic):
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
        now = datetime.now(timezone.utc)
        connections = db.scalars(select(InstagramConnection)).all()
        expiring = [
            item
            for item in connections
            if not item.token_expires_at
            or (
                item.token_expires_at
                if item.token_expires_at.tzinfo
                else item.token_expires_at.replace(tzinfo=timezone.utc)
            )
            <= now + timedelta(days=7)
        ]
        meta_counts = {
            "mode": settings.environment,
            "live_gate": (
                settings.meta_test_publish_enabled
                if settings.environment == "meta-test"
                else settings.global_publish_enabled
            ),
            "scheduler": settings.meta_scheduler_enabled,
            "automatic_gate": settings.meta_automatic_publish_enabled,
            "connected_pages": sum(item.status == "connected" for item in connections),
            "automatic_pages": db.scalar(
                select(func.count())
                .select_from(InstagramPage)
                .where(InstagramPage.automatic_publishing_enabled.is_(True))
            ),
            "expiring_tokens": len(expiring),
            "missing_permissions": sum(
                not {
                    "instagram_business_basic",
                    "instagram_business_content_publish",
                }.issubset(set(item.scopes or []))
                for item in connections
            ),
            "open_containers": db.scalar(
                select(func.count())
                .select_from(MetaPublishingAttempt)
                .where(
                    MetaPublishingAttempt.meta_container_id.is_not(None),
                    MetaPublishingAttempt.meta_media_id.is_(None),
                    MetaPublishingAttempt.phase.notin_(["failed", "completed"]),
                )
            ),
            "uncertain": db.scalar(
                select(func.count())
                .select_from(MetaPublishingAttempt)
                .where(MetaPublishingAttempt.phase == "uncertain")
            ),
            "failed": db.scalar(
                select(func.count())
                .select_from(MetaPublishingAttempt)
                .where(MetaPublishingAttempt.phase == "failed")
            ),
            "due_automatic_jobs": db.scalar(
                select(func.count())
                .select_from(PublicationJob)
                .join(InstagramPage)
                .where(
                    InstagramPage.automatic_publishing_enabled.is_(True),
                    PublicationJob.status.in_([JobStatus.SCHEDULED, JobStatus.RETRY]),
                    PublicationJob.approval_status == "approved",
                    PublicationJob.scheduled_at <= now,
                )
            ),
            "active_automatic_attempts": db.scalar(
                select(func.count())
                .select_from(MetaPublishingAttempt)
                .where(
                    MetaPublishingAttempt.trigger_mode == "automatic",
                    MetaPublishingAttempt.active_key.is_not(None),
                )
            ),
            "active_media_grants": db.scalar(
                select(func.count())
                .select_from(PublicMediaGrant)
                .where(
                    PublicMediaGrant.revoked_at.is_(None),
                    PublicMediaGrant.expires_at > now,
                )
            ),
            "last_connection_check": max(
                (item.last_check_at for item in connections if item.last_check_at),
                default=None,
            ),
            "last_successful_meta_test": db.scalar(
                select(MetaPublishingAttempt.completed_at)
                .where(
                    MetaPublishingAttempt.phase == "completed",
                    MetaPublishingAttempt.meta_media_id.is_not(None),
                )
                .order_by(MetaPublishingAttempt.completed_at.desc())
            ),
        }
        checks["meta_test"] = {
            "ok": (
                (
                    settings.environment != "meta-test"
                    or (
                        settings.meta_test_enabled
                        and not settings.meta_scheduler_enabled
                        and not settings.global_publish_enabled
                        and not settings.meta_automatic_publish_enabled
                    )
                )
                and (
                    settings.environment != "production"
                    or production_paused
                    or production_automatic
                )
            ),
            "detail": meta_counts,
        }
        instagram_rows = list(
            db.execute(
                select(InstagramPage, InstagramConnection).outerjoin(
                    InstagramConnection,
                    InstagramConnection.instagram_page_id == InstagramPage.id,
                )
            )
        )
        channel_connections = list(
            db.scalars(
                select(SocialChannelConnection).where(
                    SocialChannelConnection.channel_type.in_(("facebook", "whatsapp"))
                )
            )
        )
        channel_detail = {}
        stale_connection_before = now - timedelta(
            seconds=max(3600, settings.meta_connection_check_interval_seconds * 2)
        )
        channel_health_ok = True
        instagram_active = [
            connection if connection is not None else _MISSING_SOCIAL_HEALTH_STATE
            for page, connection in instagram_rows
            if page.active
        ]
        instagram_enabled = [
            connection if connection is not None else _MISSING_SOCIAL_HEALTH_STATE
            for page, connection in instagram_rows
            if page.active and page.publishing_enabled
        ]
        channel_detail["instagram"], instagram_ok = _social_channel_health_detail(
            instagram_active,
            instagram_enabled,
            stale_before=stale_connection_before,
        )
        channel_health_ok = channel_health_ok and instagram_ok
        for channel_type in SOCIAL_CHANNEL_TYPES:
            if channel_type == "instagram":
                continue
            channel_items = [
                item
                for item in channel_connections
                if item.channel_type == channel_type and item.active
            ]
            enabled_items = [item for item in channel_items if item.publishing_enabled]
            channel_detail[channel_type], channel_ok = _social_channel_health_detail(
                channel_items,
                enabled_items,
                stale_before=stale_connection_before,
            )
            channel_health_ok = channel_health_ok and channel_ok
        checks["social_media_channels"] = {
            "ok": channel_health_ok,
            "detail": channel_detail,
        }
        if not channel_health_ok:
            critical.append("Social-Media-Kanalverbindung")
        sync_states = list(db.scalars(select(FussballSyncState)))
        enabled_teams = {
            team.id: team
            for team in db.scalars(
                select(Team).where(
                    Team.active.is_(True),
                    Team.archived_at.is_(None),
                )
            )
            if team.fussball_url and (team.rules or {}).get("automatic_sync_enabled")
        }
        enabled_states = [state for state in sync_states if state.team_id in enabled_teams]
        stale = []
        if settings.fussball_automatic_sync_enabled:
            for state in enabled_states:
                team = enabled_teams[state.team_id]
                reason = _fussball_sync_stale_reason(
                    state,
                    team,
                    settings,
                    now=now,
                )
                if reason:
                    stale.append((state, team, reason))
        stale_reasons = {
            reason: sum(stale_reason == reason for _, _, stale_reason in stale)
            for reason in sorted({stale_reason for _, _, stale_reason in stale})
        }
        unhealthy_teams = [
            {
                "display_name": team.display_name,
                "short_name": team.short_name,
                "status": state.status,
                "stale_reason": reason,
                "sync_interval_hours": fussball_sync_interval_hours(team),
                "consecutive_failures": max(0, state.consecutive_failures),
                "last_success_at": state.last_success_at,
                "last_completed_at": state.last_completed_at,
                "next_poll_at": state.next_poll_at,
                "retry_scheduled": fussball_retry_scheduled(state, now=now),
                "error_category": classify_fussball_provider_error(state.last_error),
            }
            for state, team, reason in sorted(
                stale,
                key=lambda item: (item[1].display_name, item[1].short_name),
            )
        ]
        checks["fussball_automatic"] = {
            "ok": not settings.fussball_automatic_sync_enabled or not stale,
            "detail": {
                "global_sync_gate": settings.fussball_automatic_sync_enabled,
                "global_generation_gate": settings.automatic_post_generation_enabled,
                "enabled_teams": len(enabled_teams),
                "running": sum(state.status == "running" for state in enabled_states),
                "errors": sum(state.status == "error" for state in enabled_states),
                "stale": len(stale),
                "stale_reasons": stale_reasons,
                "unhealthy_teams": unhealthy_teams,
                "last_success": max(
                    (state.last_success_at for state in enabled_states if state.last_success_at),
                    default=None,
                ),
                "last_errors": {
                    state.team_id: state.last_error for state in enabled_states if state.last_error
                },
            },
        }
        if not checks["fussball_automatic"]["ok"]:
            critical.append("Automatische FUSSBALL.DE-Synchronisation")
    except Exception as exc:
        db.rollback()
        checks["counts"] = {"ok": False, "detail": f"Schema nicht bereit: {exc}"}
        checks["emergency_stop"] = {"ok": False, "detail": "Schema nicht bereit"}
    return {"ok": not critical, "critical": critical, "checks": checks}
