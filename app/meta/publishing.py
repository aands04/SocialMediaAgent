from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.meta.api import REQUIRED_SCOPES, MetaApiClient, MetaApiError
from app.meta.media import (
    MediaGrantError,
    create_grant,
    publication_media_items,
    revoke_grant,
    validate_publication_png,
    verify_public_media_url,
)
from app.meta.oauth import assert_meta_environment
from app.meta.security import (
    TokenCipher,
    random_confirmation_code,
    sanitize_platform_data,
    secret_hash,
)
from app.models import (
    AuditLog,
    Game,
    InstagramConnection,
    InstagramPage,
    JobStatus,
    MetaCarouselItem,
    MetaPublishConfirmation,
    MetaPublishingAttempt,
    Post,
    PostStatus,
    PublicationJob,
    PublicationMediaItem,
    PublicMediaGrant,
    SystemSetting,
    Team,
    User,
)


class MetaPublishingError(RuntimeError):
    pass


def _job_media_reports(
    db: Session, settings: Settings, job: PublicationJob
) -> list[tuple[PublicationMediaItem | None, dict]]:
    if job.kind == "carousel":
        return [
            (item, validate_publication_png(job, settings, item))
            for item in publication_media_items(db, job)
        ]
    return [(None, validate_publication_png(job, settings))]


def _attempt_carousel_items(db: Session, attempt_id: str) -> list[MetaCarouselItem]:
    return list(
        db.scalars(
            select(MetaCarouselItem)
            .where(MetaCarouselItem.attempt_id == attempt_id)
            .order_by(MetaCarouselItem.position)
        )
    )


def _revoke_attempt_grants(
    db: Session,
    attempt: MetaPublishingAttempt,
    user: User | None,
    *,
    reason: str,
) -> None:
    grant_ids = {
        item.public_media_grant_id
        for item in _attempt_carousel_items(db, attempt.id)
        if item.public_media_grant_id
    }
    if attempt.public_media_grant_id:
        grant_ids.add(attempt.public_media_grant_id)
    for grant_id in grant_ids:
        grant = db.get(PublicMediaGrant, grant_id)
        if grant and not grant.revoked_at:
            revoke_grant(db, grant, user, reason=reason)


def _create_attempt_grants(
    db: Session,
    settings: Settings,
    attempt: MetaPublishingAttempt,
    job: PublicationJob,
    user: User,
) -> list[tuple[PublicMediaGrant, str, str, MetaCarouselItem | None]]:
    reports = _job_media_reports(db, settings, job)
    if job.kind != "carousel":
        grant, raw_token, url = create_grant(db, settings, job, user)
        attempt.public_media_grant_id = grant.id
        return [(grant, raw_token, url, None)]

    persisted = {
        item.publication_media_item_id: item
        for item in _attempt_carousel_items(db, attempt.id)
    }
    result = []
    for media_item, _ in reports:
        assert media_item is not None
        child = persisted.get(media_item.id)
        if child is None:
            child = MetaCarouselItem(
                attempt_id=attempt.id,
                publication_media_item_id=media_item.id,
                position=media_item.position,
                sanitized_response={},
            )
            db.add(child)
            db.flush()
        grant, raw_token, url = create_grant(db, settings, job, user, media_item)
        child.public_media_grant_id = grant.id
        if not result:
            attempt.public_media_grant_id = grant.id
        result.append((grant, raw_token, url, child))
    return result


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _audit(
    db: Session,
    user: User,
    action: str,
    entity_type: str,
    entity_id: str,
    details: dict | None = None,
    team_id: str | None = None,
) -> None:
    db.add(
        AuditLog(
            user_id=user.id,
            team_id=team_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=sanitize_platform_data(details or {}),
        )
    )


def _locked_context(db: Session, job_id: str):
    job = db.scalar(
        select(PublicationJob)
        .where(PublicationJob.id == job_id)
        .with_for_update(skip_locked=True)
    )
    if not job:
        if db.scalar(select(PublicationJob.id).where(PublicationJob.id == job_id)):
            raise MetaPublishingError(
                "Veröffentlichungsauftrag wird bereits von einem anderen Vorgang bearbeitet"
            )
        raise MetaPublishingError("Veröffentlichungsauftrag nicht gefunden")
    post = db.get(Post, job.post_id)
    game = db.get(Game, job.game_id) if job.game_id else None
    team = db.get(Team, job.team_id)
    page = db.get(InstagramPage, job.instagram_page_id)
    connection = db.scalar(
        select(InstagramConnection)
        .where(InstagramConnection.instagram_page_id == job.instagram_page_id)
        .with_for_update(skip_locked=True)
    )
    if not all((post, team, page, connection)):
        raise MetaPublishingError("Beitrag, Seite oder Meta-Verbindung fehlt")
    if job.game_id is not None and game is None:
        raise MetaPublishingError("Zugeordnetes Spiel fehlt")
    return job, post, game, team, page, connection


def _assert_publication_gates(
    db: Session,
    settings: Settings,
    *,
    job: PublicationJob,
    post: Post,
    game: Game | None,
    team: Team,
    page: InstagramPage,
    connection: InstagramConnection,
    external_call: bool,
) -> None:
    assert_meta_environment(settings, external_call=external_call)
    stop = db.get(SystemSetting, "emergency_stop")
    now = datetime.now(timezone.utc)
    checks = [
        (settings.publisher_mode == "instagram", "PUBLISHER_MODE ist nicht instagram"),
        (not (stop and stop.value.get("enabled")), "Globaler Not-Aus ist aktiv"),
        (
            settings.environment != "meta-test" or connection.test_account,
            "Instagram-Seite ist nicht ausdrücklich als Testseite markiert",
        ),
        (connection.status == "connected", "Instagram-Verbindung ist nicht erfolgreich geprüft"),
        (connection.account_type == "BUSINESS", "Zielkonto ist kein Business-Konto"),
        (
            REQUIRED_SCOPES.issubset(set(connection.scopes or [])),
            "Erforderliche Instagram-Berechtigungen fehlen",
        ),
        (
            connection.token_expires_at is not None
            and _utc(connection.token_expires_at) > now + timedelta(minutes=5),
            "Instagram-Token ist abgelaufen oder läuft unmittelbar ab",
        ),
        (page.active and page.publishing_enabled, "Publishing ist für die Testseite deaktiviert"),
        (post.instagram_page_id == page.id, "Beitrag gehört nicht zur Testseite"),
        (job.instagram_page_id == page.id, "Auftrag gehört nicht zur Testseite"),
        (
            post.status in {PostStatus.APPROVED, PostStatus.SCHEDULED, PostStatus.PARTIAL},
            "Beitrag ist nicht ausdrücklich freigegeben",
        ),
        (
            post.approved_version is not None
            and post.approved_version == post.version
            and job.approved_post_version == post.version,
            "Freigegebene Beitragsversion stimmt nicht",
        ),
        (
            job.approval_status == "approved"
            and job.status
            not in {JobStatus.PUBLISHED, JobStatus.CANCELLED, JobStatus.SKIPPED},
            "Auftrag ist nicht freigegeben oder bereits abgeschlossen",
        ),
        (
            game is None
            or game.status not in {"cancelled", "postponed", "provisional"},
            "Spielstatus sperrt die Veröffentlichung",
        ),
        (
            game is None or not game.overrides.get("automation_blocked"),
            "Spiel ist für Automatisierung gesperrt",
        ),
        (not job.stale_time, "Veröffentlichungszeit ist als veraltet markiert"),
        (post.publishing_enabled and team.publishing_enabled, "Publishing wurde deaktiviert"),
        (job.kind in {"feed", "carousel", "story"}, "Nicht unterstützte Medienart"),
    ]
    for ok, message in checks:
        if not ok:
            raise MetaPublishingError(message)
    _job_media_reports(db, settings, job)


def assert_automatic_scheduler_environment(settings: Settings) -> None:
    checks = [
        (settings.environment == "production", "ENVIRONMENT ist nicht production"),
        (settings.publisher_mode == "instagram", "PUBLISHER_MODE ist nicht instagram"),
        (settings.meta_production_enabled, "META_PRODUCTION_ENABLED ist nicht aktiv"),
        (settings.global_publish_enabled, "GLOBAL_PUBLISH_ENABLED ist nicht aktiv"),
        (settings.meta_scheduler_enabled, "META_SCHEDULER_ENABLED ist nicht aktiv"),
        (
            settings.meta_automatic_publish_enabled,
            "META_AUTOMATIC_PUBLISH_ENABLED ist nicht aktiv",
        ),
        (not settings.meta_test_enabled, "META_TEST_ENABLED muss in Produktion aus sein"),
        (
            not settings.meta_test_publish_enabled,
            "META_TEST_PUBLISH_ENABLED muss in Produktion aus sein",
        ),
        (not settings.meta_access_token, "Globaler META_ACCESS_TOKEN ist nicht zulässig"),
    ]
    for ok, message in checks:
        if not ok:
            raise MetaPublishingError(message)


def _assert_automatic_publication_gates(
    db: Session,
    settings: Settings,
    *,
    job: PublicationJob,
    post: Post,
    game: Game | None,
    team: Team,
    page: InstagramPage,
    connection: InstagramConnection,
) -> User:
    assert_automatic_scheduler_environment(settings)
    _assert_publication_gates(
        db,
        settings,
        job=job,
        post=post,
        game=game,
        team=team,
        page=page,
        connection=connection,
        external_call=False,
    )
    now = datetime.now(timezone.utc)
    stop = db.get(SystemSetting, "emergency_stop")
    approver = db.get(User, post.approved_by) if post.approved_by else None
    last_check = _utc(connection.last_check_at) if connection.last_check_at else None
    scheduled_at = _utc(job.scheduled_at)
    retry_at = _utc(job.next_attempt_at) if job.next_attempt_at else None
    checks = [
        (
            stop is not None and stop.value.get("enabled") is False,
            "Globaler Not-Aus wurde nicht ausdrücklich deaktiviert",
        ),
        (
            page.automatic_publishing_enabled
            and page.automatic_publishing_confirmed_by
            and page.automatic_publishing_confirmed_at,
            "Automatische Veröffentlichung ist für die Instagram-Seite nicht freigegeben",
        ),
        (
            last_check is not None
            and last_check
            >= now - timedelta(seconds=settings.meta_connection_max_age_seconds),
            "Meta-Verbindungsprüfung ist zu alt",
        ),
        (
            bool(
                (page.allowed_types or {}).get(
                    "feed" if job.kind == "carousel" else job.kind, False
                )
            ),
            "Medienart ist für die Instagram-Seite deaktiviert",
        ),
        (not post.critical_warnings, "Beitrag enthält ungeklärte kritische Warnungen"),
        (approver is not None and approver.active, "Freigebender Benutzer ist nicht aktiv"),
        (scheduled_at <= now, "Veröffentlichungszeitpunkt ist noch nicht erreicht"),
        (retry_at is None or retry_at <= now, "Sicherheitswartezeit ist noch nicht abgelaufen"),
        (job.platform_id is None, "Auftrag besitzt bereits eine Plattform-ID"),
        (
            job.attempts < settings.max_publish_attempts,
            "Maximale Anzahl automatischer Versuche erreicht",
        ),
    ]
    for ok, message in checks:
        if not ok:
            raise MetaPublishingError(message)
    return approver


def create_attempt(
    db: Session,
    settings: Settings,
    *,
    publication_job_id: str,
    stage: str,
    user: User,
    media_http_client: httpx.Client,
    trigger_mode: str = "manual",
) -> tuple[MetaPublishingAttempt, str | None]:
    if stage not in {"validate-only", "container-only", "publish"}:
        raise MetaPublishingError("Ungültige Meta-Teststufe")
    if trigger_mode not in {"manual", "automatic"}:
        raise MetaPublishingError("Ungültiger Auslöser für Meta-Versuch")
    job, post, game, team, page, connection = _locked_context(db, publication_job_id)
    _assert_publication_gates(
        db,
        settings,
        job=job,
        post=post,
        game=game,
        team=team,
        page=page,
        connection=connection,
        external_call=False,
    )
    if trigger_mode == "automatic":
        automatic_user = _assert_automatic_publication_gates(
            db,
            settings,
            job=job,
            post=post,
            game=game,
            team=team,
            page=page,
            connection=connection,
        )
        if automatic_user.id != user.id:
            raise MetaPublishingError(
                "Automatischer Auftrag muss an die ursprüngliche Freigabe gebunden sein"
            )
    existing = db.scalar(
        select(MetaPublishingAttempt)
        .where(MetaPublishingAttempt.active_key == job.id)
        .with_for_update(skip_locked=True)
    )
    if existing:
        return existing, None
    if db.scalar(
        select(MetaPublishingAttempt.id).where(
            MetaPublishingAttempt.active_key == job.id
        )
    ):
        raise MetaPublishingError(
            "Für diesen Auftrag wird bereits ein Meta-Versuch bearbeitet"
        )
    media_reports = _job_media_reports(db, settings, job)
    _, report = media_reports[0]
    attempt = MetaPublishingAttempt(
        publication_job_id=job.id,
        connection_id=connection.id,
        active_key=job.id,
        target_account_id=connection.instagram_user_id or "",
        media_kind=job.kind,
        local_media_version=job.version,
        media_path=str(report["path"]),
        file_checksum=report["checksum"],
        stage=stage,
        trigger_mode=trigger_mode,
        phase="validating_public_media",
        next_action_at=datetime.now(timezone.utc),
        sanitized_response={
            "request_preview": {
                "endpoint": (
                    f"https://graph.instagram.com/{settings.meta_graph_version}/"
                    f"{connection.instagram_user_id}/media"
                ),
                "media_url": (
                    f"{settings.meta_public_base_url.rstrip('/')}/"
                    "public/meta-media/[temporäres-token]"
                ),
                "kind": job.kind,
                "caption_included": job.kind in {"feed", "carousel"}
                and bool(job.text_snapshot),
                "image_count": len(media_reports),
                "ordered_checksums": [entry[1]["checksum"] for entry in media_reports],
            },
        },
        started_by=user.id,
    )
    db.add(attempt)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        duplicate = db.scalar(
            select(MetaPublishingAttempt).where(
                MetaPublishingAttempt.active_key == publication_job_id
            )
        )
        if duplicate:
            return duplicate, None
        raise
    grant_rows = _create_attempt_grants(db, settings, attempt, job, user)
    if trigger_mode == "automatic":
        job.status = JobStatus.PUBLISHING
        job.attempts += 1
        job.last_attempt_at = datetime.now(timezone.utc)
        job.next_attempt_at = None
        job.error = None
    # The public endpoint uses a separate database connection. Commit the
    # hash-only grant and active attempt before checking that exact URL.
    db.commit()
    try:
        public_checks = [
            verify_public_media_url(settings, grant, media_url, media_http_client)
            for grant, _, media_url, _ in grant_rows
        ]
    except MediaGrantError as exc:
        attempt = db.get(MetaPublishingAttempt, attempt.id)
        attempt.phase = "failed"
        attempt.active_key = None
        attempt.error_category = "public_media_unreachable"
        attempt.error_message = str(exc)
        if trigger_mode == "automatic":
            job = db.get(PublicationJob, publication_job_id)
            if job:
                if job.attempts < settings.max_publish_attempts:
                    job.status = JobStatus.RETRY
                    job.next_attempt_at = datetime.now(timezone.utc) + timedelta(
                        minutes=min(30, 2 ** max(1, job.attempts))
                    )
                else:
                    job.status = JobStatus.FAILED
                job.error = str(exc)
        _revoke_attempt_grants(
            db, attempt, user, reason="Öffentliche Medienprüfung fehlgeschlagen"
        )
        _audit(
            db,
            user,
            "meta.public_media_validation_failed",
            "meta_publishing_attempt",
            attempt.id,
            {"error": str(exc)},
            job.team_id,
        )
        db.commit()
        raise
    attempt.sanitized_response = {
        **(attempt.sanitized_response or {}),
        "public_media_checks": public_checks,
    }
    if stage == "validate-only":
        attempt.phase = "completed"
        attempt.completed_at = datetime.now(timezone.utc)
        attempt.active_key = None
        _revoke_attempt_grants(
            db, attempt, user, reason="Validate-only-Prüfung abgeschlossen"
        )
    else:
        attempt.phase = "creating_media_grant"
        attempt.next_action_at = datetime.now(timezone.utc)
    _audit(
        db,
        user,
        "meta.validate_only" if stage == "validate-only" else "meta.attempt_created",
        "meta_publishing_attempt",
        attempt.id,
        {
            "publication_job_id": job.id,
            "stage": stage,
            "checksum": attempt.file_checksum,
        },
        job.team_id,
    )
    db.commit()
    return attempt, grant_rows[0][1]


def issue_confirmation(
    db: Session,
    settings: Settings,
    attempt: MetaPublishingAttempt,
    user: User,
    purpose: str,
) -> str:
    if purpose not in {"create_container", "publish"}:
        raise MetaPublishingError("Ungültiger Bestätigungszweck")
    code = random_confirmation_code()
    db.add(
        MetaPublishConfirmation(
            attempt_id=attempt.id,
            user_id=user.id,
            purpose=purpose,
            code_hash=secret_hash(code),
            expires_at=datetime.now(timezone.utc)
            + timedelta(seconds=settings.meta_confirmation_ttl_seconds),
        )
    )
    db.commit()
    return code


def _consume_confirmation(
    db: Session,
    attempt: MetaPublishingAttempt,
    user: User,
    purpose: str,
    code: str,
) -> None:
    confirmation = db.scalar(
        select(MetaPublishConfirmation)
        .where(
            MetaPublishConfirmation.attempt_id == attempt.id,
            MetaPublishConfirmation.user_id == user.id,
            MetaPublishConfirmation.purpose == purpose,
            MetaPublishConfirmation.code_hash == secret_hash(code),
        )
        .order_by(MetaPublishConfirmation.created_at.desc())
        .with_for_update()
    )
    now = datetime.now(timezone.utc)
    if (
        not confirmation
        or confirmation.used_at
        or _utc(confirmation.expires_at) <= now
    ):
        raise MetaPublishingError("Bestätigungscode ist falsch, abgelaufen oder bereits verwendet")
    confirmation.used_at = now


def _reload_attempt_context(db: Session, attempt_id: str):
    job_id = db.scalar(
        select(MetaPublishingAttempt.publication_job_id).where(
            MetaPublishingAttempt.id == attempt_id
        )
    )
    if not job_id:
        raise MetaPublishingError("Meta-Versuch nicht gefunden")
    context = _locked_context(db, job_id)
    attempt = db.scalar(
        select(MetaPublishingAttempt)
        .where(MetaPublishingAttempt.id == attempt_id)
        .with_for_update(skip_locked=True)
    )
    if not attempt:
        raise MetaPublishingError(
            "Meta-Versuch wird bereits von einem anderen Vorgang bearbeitet"
        )
    return (attempt, *context)


def create_container(
    db: Session,
    settings: Settings,
    *,
    attempt_id: str,
    user: User,
    confirmation_code: str,
    api: MetaApiClient,
    media_http_client: httpx.Client,
    automatic: bool = False,
) -> MetaPublishingAttempt:
    attempt, job, post, game, team, page, connection = _reload_attempt_context(
        db, attempt_id
    )
    if attempt.meta_container_id:
        return attempt
    if attempt.phase == "validating_public_media":
        raise MetaPublishingError(
            "Öffentliche Medienprüfung läuft bereits oder wurde unterbrochen; "
            "es wird kein paralleler Meta-Aufruf gestartet"
        )
    if attempt.phase == "creating_container":
        raise MetaPublishingError(
            "Containererstellung läuft bereits oder wurde unterbrochen; "
            "es wird kein zweiter Container erzeugt"
        )
    if attempt.phase == "uncertain":
        raise MetaPublishingError(
            "Containerantwort ist unklar; vor einem weiteren Versuch ist manueller Abgleich erforderlich"
        )
    if attempt.stage not in {"container-only", "publish"}:
        raise MetaPublishingError("Validate-only darf keinen Meta-Container erzeugen")
    _assert_publication_gates(
        db,
        settings,
        job=job,
        post=post,
        game=game,
        team=team,
        page=page,
        connection=connection,
        external_call=True,
    )
    if automatic:
        if attempt.trigger_mode != "automatic":
            raise MetaPublishingError("Manueller Meta-Versuch darf nicht automatisch laufen")
        automatic_user = _assert_automatic_publication_gates(
            db,
            settings,
            job=job,
            post=post,
            game=game,
            team=team,
            page=page,
            connection=connection,
        )
        if automatic_user.id != user.id:
            raise MetaPublishingError("Freigabebindung des automatischen Auftrags ist ungültig")
    if attempt.local_media_version != job.version:
        raise MetaPublishingError("Lokale Medienversion wurde seit der Prüfung verändert")
    media_reports = _job_media_reports(db, settings, job)
    expected_checksums = (attempt.sanitized_response or {}).get(
        "request_preview", {}
    ).get("ordered_checksums") or [attempt.file_checksum]
    if [entry[1]["checksum"] for entry in media_reports] != expected_checksums:
        raise MetaPublishingError("Mediendateien oder Reihenfolge wurden seit der Prüfung verändert")
    if not automatic:
        _consume_confirmation(db, attempt, user, "create_container", confirmation_code)
    grant_rows = _create_attempt_grants(db, settings, attempt, job, user)
    attempt.phase = "validating_public_media"
    attempt.attempts += 1
    db.commit()
    try:
        public_checks = [
            verify_public_media_url(settings, grant, media_url, media_http_client)
            for grant, _, media_url, _ in grant_rows
        ]
    except MediaGrantError as exc:
        attempt = db.get(MetaPublishingAttempt, attempt.id)
        attempt.phase = "failed"
        attempt.active_key = None
        attempt.error_category = "public_media_unreachable"
        attempt.error_message = str(exc)
        if automatic:
            job = db.get(PublicationJob, job.id)
            job.status = JobStatus.FAILED
            job.error = str(exc)
        _revoke_attempt_grants(
            db,
            attempt,
            user,
            reason="Öffentliche Medienprüfung vor Containererstellung fehlgeschlagen",
        )
        _audit(
            db,
            user,
            "meta.public_media_validation_failed",
            "meta_publishing_attempt",
            attempt.id,
            {"error": str(exc)},
            job.team_id,
        )
        db.commit()
        raise
    attempt, job, post, game, team, page, connection = _reload_attempt_context(
        db, attempt_id
    )
    if attempt.phase != "validating_public_media":
        raise MetaPublishingError(
            "Meta-Versuch wurde während der Medienprüfung verändert"
        )
    _assert_publication_gates(
        db,
        settings,
        job=job,
        post=post,
        game=game,
        team=team,
        page=page,
        connection=connection,
        external_call=True,
    )
    if automatic:
        automatic_user = _assert_automatic_publication_gates(
            db,
            settings,
            job=job,
            post=post,
            game=game,
            team=team,
            page=page,
            connection=connection,
        )
        if automatic_user.id != user.id:
            raise MetaPublishingError("Freigabebindung des automatischen Auftrags ist ungültig")
    media_reports = _job_media_reports(db, settings, job)
    if (
        attempt.local_media_version != job.version
        or [entry[1]["checksum"] for entry in media_reports] != expected_checksums
    ):
        raise MetaPublishingError(
            "Medium oder Medienfreigabe wurde während der Prüfung verändert"
        )
    attempt.phase = "creating_container"
    attempt.sanitized_response = {
        **(attempt.sanitized_response or {}),
        "public_media_checks": public_checks,
    }
    db.commit()
    token = TokenCipher(settings.meta_token_encryption_key).decrypt(
        connection.encrypted_token
    )
    try:
        if attempt.media_kind == "carousel":
            child_ids = []
            for _, _, media_url, child in grant_rows:
                assert child is not None
                if not child.meta_container_id:
                    child_response = api.create_carousel_item(
                        access_token=token,
                        account_id=attempt.target_account_id,
                        image_url=media_url,
                    )
                    child_id = str(child_response.get("id") or "")
                    if not child_id:
                        raise MetaApiError(
                            "Meta lieferte keine Child-Container-ID",
                            response=child_response,
                        )
                    child.meta_container_id = child_id
                    child.container_status = "IN_PROGRESS"
                    child.sanitized_response = sanitize_platform_data(child_response)
                    db.commit()
                child_ids.append(child.meta_container_id)
            response = api.create_carousel_container(
                access_token=token,
                account_id=attempt.target_account_id,
                child_ids=child_ids,
                caption=job.text_snapshot,
            )
        else:
            response = api.create_container(
                access_token=token,
                account_id=attempt.target_account_id,
                kind=attempt.media_kind,
                image_url=grant_rows[0][2],
                caption=job.text_snapshot if attempt.media_kind == "feed" else None,
            )
        container_id = str(response.get("id") or "")
        if not container_id:
            raise MetaApiError("Meta lieferte keine Container-ID", response=response)
        db.expire_all()
        attempt, job, post, game, team, page, connection = _reload_attempt_context(
            db, attempt_id
        )
        if attempt.meta_container_id:
            return attempt
        attempt.meta_container_id = container_id
        attempt.phase = "waiting_for_container"
        attempt.container_status = "IN_PROGRESS"
        attempt.next_action_at = datetime.now(timezone.utc) + timedelta(
            seconds=settings.meta_container_poll_interval_seconds
        )
        attempt.sanitized_response = sanitize_platform_data(response)
        _audit(
            db,
            user,
            "meta.container_created",
            "meta_publishing_attempt",
            attempt.id,
            {"container_id": container_id, "stage": attempt.stage},
            job.team_id,
        )
        db.commit()
        return attempt
    except MetaApiError as exc:
        db.expire_all()
        attempt, job, post, game, team, page, connection = _reload_attempt_context(
            db, attempt_id
        )
        attempt.phase = "uncertain" if exc.uncertain else "failed"
        if not exc.uncertain:
            attempt.active_key = None
        attempt.error_category = "uncertain_response" if exc.uncertain else "meta_api"
        attempt.error_message = str(exc)
        attempt.sanitized_response = exc.response
        if automatic:
            job.status = JobStatus.UNCERTAIN if exc.uncertain else JobStatus.FAILED
            job.error = str(exc)
        if not exc.uncertain:
            _revoke_attempt_grants(
                db, attempt, user, reason="Meta-Container wurde nicht angenommen"
            )
        _audit(
            db,
            user,
            "meta.publication_uncertain" if exc.uncertain else "meta.publication_failed",
            "meta_publishing_attempt",
            attempt.id,
            {"phase": "creating_container", "error": str(exc)},
            job.team_id,
        )
        db.commit()
        raise MetaPublishingError(str(exc)) from exc


def refresh_container_status(
    db: Session,
    settings: Settings,
    *,
    attempt_id: str,
    user: User,
    api: MetaApiClient,
) -> MetaPublishingAttempt:
    attempt, job, post, game, team, page, connection = _reload_attempt_context(
        db, attempt_id
    )
    if not attempt.meta_container_id:
        raise MetaPublishingError("Es ist noch keine Container-ID gespeichert")
    _assert_publication_gates(
        db,
        settings,
        job=job,
        post=post,
        game=game,
        team=team,
        page=page,
        connection=connection,
        external_call=True,
    )
    if attempt.trigger_mode == "automatic":
        automatic_user = _assert_automatic_publication_gates(
            db,
            settings,
            job=job,
            post=post,
            game=game,
            team=team,
            page=page,
            connection=connection,
        )
        if automatic_user.id != user.id:
            raise MetaPublishingError("Freigabebindung des automatischen Auftrags ist ungültig")
    token = TokenCipher(settings.meta_token_encryption_key).decrypt(
        connection.encrypted_token
    )
    response = api.container_status(
        access_token=token, container_id=attempt.meta_container_id
    )
    old_status = attempt.container_status
    attempt.container_status = str(response.get("status_code") or "UNKNOWN")
    attempt.sanitized_response = sanitize_platform_data(response)
    if attempt.container_status == "ERROR":
        attempt.phase = "failed"
        attempt.active_key = None
        attempt.error_category = "container_error"
        attempt.error_message = str(response.get("status") or "Meta-Container meldet Fehler")
        if attempt.trigger_mode == "automatic":
            job.status = JobStatus.FAILED
            job.error = attempt.error_message
        _revoke_attempt_grants(
            db,
            attempt,
            user,
            reason="Meta-Container meldet einen endgültigen Fehler",
        )
    elif attempt.container_status == "FINISHED":
        attempt.phase = "ready_to_publish" if attempt.stage == "publish" else "completed"
        attempt.next_action_at = datetime.now(timezone.utc)
        if attempt.stage == "container-only":
            attempt.completed_at = datetime.now(timezone.utc)
            attempt.active_key = None
            _revoke_attempt_grants(
                db, attempt, user, reason="Container-only-Test abgeschlossen"
            )
    else:
        attempt.next_action_at = datetime.now(timezone.utc) + timedelta(
            seconds=settings.meta_container_poll_interval_seconds
        )
    _audit(
        db,
        user,
        "meta.container_status_changed",
        "meta_publishing_attempt",
        attempt.id,
        {"old": old_status, "new": attempt.container_status},
        job.team_id,
    )
    db.commit()
    return attempt


def publish(
    db: Session,
    settings: Settings,
    *,
    attempt_id: str,
    user: User,
    confirmation_code: str,
    api: MetaApiClient,
    automatic: bool = False,
) -> MetaPublishingAttempt:
    attempt, job, post, game, team, page, connection = _reload_attempt_context(
        db, attempt_id
    )
    if attempt.meta_media_id:
        return attempt
    if attempt.phase == "publishing":
        raise MetaPublishingError(
            "Veröffentlichung läuft bereits oder wurde unterbrochen; "
            "media_publish wird nicht erneut aufgerufen"
        )
    if attempt.phase == "uncertain":
        raise MetaPublishingError(
            "Veröffentlichungsantwort ist unklar; media_publish wird nicht wiederholt"
        )
    if (
        attempt.stage != "publish"
        or attempt.container_status != "FINISHED"
        or attempt.phase != "ready_to_publish"
    ):
        raise MetaPublishingError("Container ist nicht zur Veröffentlichung bereit")
    _assert_publication_gates(
        db,
        settings,
        job=job,
        post=post,
        game=game,
        team=team,
        page=page,
        connection=connection,
        external_call=True,
    )
    if automatic:
        if attempt.trigger_mode != "automatic":
            raise MetaPublishingError("Manueller Meta-Versuch darf nicht automatisch laufen")
        automatic_user = _assert_automatic_publication_gates(
            db,
            settings,
            job=job,
            post=post,
            game=game,
            team=team,
            page=page,
            connection=connection,
        )
        if automatic_user.id != user.id:
            raise MetaPublishingError("Freigabebindung des automatischen Auftrags ist ungültig")
    else:
        _consume_confirmation(db, attempt, user, "publish", confirmation_code)
    attempt.phase = "publishing"
    db.commit()
    token = TokenCipher(settings.meta_token_encryption_key).decrypt(
        connection.encrypted_token
    )
    try:
        response = api.publish_container(
            access_token=token,
            account_id=attempt.target_account_id,
            container_id=attempt.meta_container_id or "",
        )
        media_id = str(response.get("id") or "")
        if not media_id:
            raise MetaApiError("Meta lieferte keine Media-ID", response=response)
        db.expire_all()
        attempt, job, post, game, team, page, connection = _reload_attempt_context(
            db, attempt_id
        )
        if attempt.meta_media_id:
            return attempt
        attempt.meta_media_id = media_id
        attempt.phase = "reconciling"
        attempt.sanitized_response = sanitize_platform_data(response)
        job.status = JobStatus.PUBLISHED
        job.platform_id = media_id
        job.published_at = datetime.now(timezone.utc)
        sibling_statuses = list(
            db.scalars(
                select(PublicationJob.status).where(PublicationJob.post_id == post.id)
            )
        )
        post.status = (
            PostStatus.PUBLISHED
            if sibling_statuses
            and all(status == JobStatus.PUBLISHED for status in sibling_statuses)
            else PostStatus.PARTIAL
        )
        db.commit()
        try:
            details = api.media_details(access_token=token, media_id=media_id)
            attempt.permalink = str(details.get("permalink") or "") or None
            job.permalink = attempt.permalink
            attempt.sanitized_response = sanitize_platform_data(details)
        except MetaApiError as details_error:
            # The publish response already supplied a durable media ID. A
            # best-effort permalink lookup must not turn that confirmed
            # publication into a failed or uncertain job.
            attempt.error_category = "permalink_lookup"
            attempt.error_message = str(details_error)
        attempt.phase = "completed"
        attempt.completed_at = datetime.now(timezone.utc)
        attempt.active_key = None
        _revoke_attempt_grants(
            db, attempt, user, reason="Instagram-Veröffentlichung abgeschlossen"
        )
        _audit(
            db,
            user,
            "meta.publication_succeeded",
            "meta_publishing_attempt",
            attempt.id,
            {"media_id": media_id, "permalink": attempt.permalink},
            job.team_id,
        )
        db.commit()
        return attempt
    except MetaApiError as exc:
        db.expire_all()
        attempt, job, post, game, team, page, connection = _reload_attempt_context(
            db, attempt_id
        )
        attempt.phase = "uncertain" if exc.uncertain else "failed"
        if not exc.uncertain:
            attempt.active_key = None
        attempt.error_category = "uncertain_response" if exc.uncertain else "meta_api"
        attempt.error_message = str(exc)
        attempt.sanitized_response = exc.response
        job.status = JobStatus.UNCERTAIN if exc.uncertain else JobStatus.FAILED
        job.error = str(exc)
        _audit(
            db,
            user,
            "meta.publication_uncertain" if exc.uncertain else "meta.publication_failed",
            "meta_publishing_attempt",
            attempt.id,
            {"phase": "publishing", "error": str(exc)},
            job.team_id,
        )
        db.commit()
        raise MetaPublishingError(str(exc)) from exc


def reconcile_attempt(
    db: Session,
    settings: Settings,
    *,
    attempt_id: str,
    user: User,
    resolution: str,
    note: str,
    meta_media_id: str = "",
    permalink: str = "",
) -> MetaPublishingAttempt:
    if resolution not in {"published", "not_published"} or not note.strip():
        raise MetaPublishingError("Abgleichsergebnis und Prüfvermerk sind erforderlich")
    attempt, job, post, _, _, _, _ = _reload_attempt_context(db, attempt_id)
    interrupted_phases = {
        "validating_public_media",
        "creating_container",
        "publishing",
    }
    if attempt.phase not in {"uncertain", *interrupted_phases}:
        raise MetaPublishingError(
            "Nur ein unklarer oder nach einem Neustart festhängender Vorgang "
            "kann manuell abgeglichen werden"
        )
    if attempt.phase in interrupted_phases:
        minimum_age = timedelta(
            seconds=max(120, int(settings.meta_http_timeout_seconds * 3))
        )
        if datetime.now(timezone.utc) - _utc(attempt.updated_at) < minimum_age:
            raise MetaPublishingError(
                "Der externe Aufruf könnte noch laufen. Der manuelle Abgleich ist "
                "erst nach Ablauf der Sicherheitsfrist möglich."
            )
        if attempt.phase == "validating_public_media" and resolution == "published":
            raise MetaPublishingError(
                "Während der Medienprüfung wurde noch keine Meta-Veröffentlichung "
                "gestartet; 'veröffentlicht' ist hier kein zulässiges Ergebnis."
            )
    if resolution == "published":
        if not meta_media_id.strip():
            raise MetaPublishingError("Die bestätigte Instagram-Media-ID fehlt")
        attempt.meta_media_id = meta_media_id.strip()
        attempt.permalink = permalink.strip() or None
        attempt.phase = "completed"
        attempt.completed_at = datetime.now(timezone.utc)
        attempt.active_key = None
        job.status = JobStatus.PUBLISHED
        job.platform_id = attempt.meta_media_id
        job.permalink = attempt.permalink
        job.published_at = datetime.now(timezone.utc)
        sibling_statuses = list(
            db.scalars(
                select(PublicationJob.status).where(PublicationJob.post_id == post.id)
            )
        )
        post.status = (
            PostStatus.PUBLISHED
            if sibling_statuses
            and all(status == JobStatus.PUBLISHED for status in sibling_statuses)
            else PostStatus.PARTIAL
        )
    else:
        attempt.phase = "failed"
        attempt.error_category = "manually_reconciled_not_published"
        attempt.error_message = note.strip()
        attempt.active_key = None
        job.status = JobStatus.FAILED
        job.error = "Manuell geprüft: nicht veröffentlicht"
    _revoke_attempt_grants(
        db, attempt, user, reason="manueller Meta-Abgleich abgeschlossen"
    )
    _audit(
        db,
        user,
        "meta.manual_reconciliation",
        "meta_publishing_attempt",
        attempt.id,
        {
            "resolution": resolution,
            "media_id": attempt.meta_media_id,
            "note": note.strip(),
        },
        job.team_id,
    )
    db.commit()
    return attempt
