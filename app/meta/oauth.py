from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.meta.api import REQUIRED_SCOPES, MetaApiClient, MetaApiError
from app.meta.security import (
    MetaSecretError,
    TokenCipher,
    random_oauth_state,
    secret_hash,
)
from app.models import (
    AuditLog,
    InstagramConnection,
    InstagramOAuthState,
    InstagramPage,
    MetaPublishingAttempt,
    PublicMediaGrant,
    User,
)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, (MetaApiError, MetaSecretError)):
        return str(exc)[:500]
    return "Interner Fehler während der Meta-Verarbeitung"


def _oauth_grant_scopes() -> set[str]:
    """Return the exact scopes represented by this OAuth authorization code.

    Instagram Login does not expose the Facebook Graph ``/me/permissions``
    edge.  The authorization URL requests this fixed, minimal scope set and a
    successful authorization code represents the user's grant for that
    request.  Token and account validity are verified separately through the
    supported Instagram ``/me`` endpoint.
    """

    return set(REQUIRED_SCOPES)


def assert_meta_environment(settings: Settings, *, external_call: bool = False) -> None:
    if settings.environment == "meta-test":
        if not settings.meta_test_enabled:
            raise MetaApiError("Instagram-Meta-Test ist nicht aktiviert")
        if external_call and not settings.meta_test_publish_enabled:
            raise MetaApiError("Externe Meta-Aufrufe sind durch META_TEST_PUBLISH_ENABLED gesperrt")
        return
    if settings.environment == "production":
        if settings.publisher_mode != "instagram" or not settings.meta_production_enabled:
            raise MetaApiError("Instagram-Produktion ist nicht ausdrücklich aktiviert")
        return
    raise MetaApiError("Instagram-Verbindungen sind nur in Meta-Test oder Produktion erlaubt")


def start_oauth(
    db: Session,
    settings: Settings,
    page: InstagramPage,
    user: User,
    api: MetaApiClient,
) -> str:
    assert_meta_environment(settings)
    if not page.club_id or not user.club_id or page.club_id != user.club_id:
        raise MetaApiError("Instagram-Seite und Benutzer sind nicht demselben Verein zugeordnet")
    if not settings.meta_oauth_redirect_uri:
        raise MetaApiError("META_OAUTH_REDIRECT_URI fehlt")
    state = random_oauth_state()
    db.add(
        InstagramOAuthState(
            club_id=page.club_id,
            state_hash=secret_hash(state),
            instagram_page_id=page.id,
            user_id=user.id,
            redirect_uri=settings.meta_oauth_redirect_uri,
            expires_at=datetime.now(timezone.utc)
            + timedelta(seconds=settings.meta_oauth_state_ttl_seconds),
        )
    )
    db.add(
        AuditLog(
            club_id=page.club_id,
            user_id=user.id,
            action="meta.oauth_started",
            entity_type="instagram_page",
            entity_id=page.id,
            details={"redirect_uri": settings.meta_oauth_redirect_uri},
        )
    )
    db.commit()
    return api.authorization_url(state, settings.meta_oauth_redirect_uri)


def consume_oauth_state(db: Session, state: str) -> InstagramOAuthState:
    record = db.scalar(
        select(InstagramOAuthState)
        .where(InstagramOAuthState.state_hash == secret_hash(state))
        .with_for_update()
    )
    now = datetime.now(timezone.utc)
    if not record or record.used_at or _utc(record.expires_at) <= now:
        raise MetaApiError("OAuth-State fehlt, ist abgelaufen oder wurde bereits verwendet")
    record.used_at = now
    db.flush()
    return record


def complete_oauth(
    db: Session,
    settings: Settings,
    *,
    state: str,
    code: str,
    api: MetaApiClient,
) -> InstagramConnection:
    assert_meta_environment(settings, external_call=True)
    record = consume_oauth_state(db, state)
    record_id = record.id
    try:
        initiating_user = db.get(User, record.user_id)
        if not initiating_user or not initiating_user.active:
            raise MetaApiError("Der Benutzer, der OAuth gestartet hat, ist nicht mehr aktiv")
        if not record.club_id or initiating_user.club_id != record.club_id:
            raise MetaApiError("Der Instagram-Verbindungsvorgang ist keinem Verein zugeordnet")
        short = api.exchange_code(code, record.redirect_uri)
        token = api.exchange_long_lived(short)
        profile = api.profile(token.access_token)
        scopes = _oauth_grant_scopes()
        account_type = str(profile.get("account_type") or "").upper()
        if account_type != "BUSINESS":
            raise MetaApiError(
                "Für Story-Tests ist ein professionelles Business-Konto erforderlich"
            )
        profile_id = str(profile.get("user_id") or profile.get("id") or short.user_id)
        page = db.get(InstagramPage, record.instagram_page_id)
        if not page:
            raise MetaApiError("Zielseite wurde während OAuth entfernt")
        if page.club_id != record.club_id:
            raise MetaApiError("Die Instagram-Zielseite gehört nicht zum Verbindungsvorgang")
        confirmed_username = str(profile.get("username") or "")
        if (
            not (page.defaults or {}).get("guided_setup")
            and page.username
            and confirmed_username
            and page.username.casefold() != confirmed_username.casefold()
        ):
            raise MetaApiError(
                f"Falsches Instagram-Konto verbunden: erwartet @{page.username}, "
                f"erhalten @{confirmed_username}"
            )
        existing = db.scalar(
            select(InstagramConnection)
            .where(InstagramConnection.instagram_page_id == page.id)
            .with_for_update()
        )
        if existing and existing.club_id != page.club_id:
            raise MetaApiError("Die bestehende Instagram-Verbindung gehört zu einem anderen Verein")
        connection = existing or InstagramConnection(
            club_id=page.club_id,
            instagram_page_id=page.id,
        )
        if not existing:
            db.add(connection)
        connection.instagram_user_id = profile_id
        connection.confirmed_username = confirmed_username
        connection.account_type = account_type
        connection.login_variant = "instagram_login"
        connection.api_version = settings.meta_graph_version
        connection.scopes = sorted(scopes)
        connection.status = "connected"
        connection.encrypted_token = TokenCipher(settings.meta_token_encryption_key).encrypt(
            token.access_token
        )
        connection.token_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=token.expires_in
        )
        connection.token_key_version = settings.meta_token_key_version
        connection.last_check_at = datetime.now(timezone.utc)
        connection.last_success_at = datetime.now(timezone.utc)
        connection.last_error = None
        connection.disconnected_at = None
        page.account_id = profile_id
        page.username = connection.confirmed_username or page.username
        page.display_name = page.display_name or page.username
        page.connection_status = "connected"
        page.last_check_at = datetime.now(timezone.utc)
        if (page.defaults or {}).get("guided_setup"):
            page.defaults = {**(page.defaults or {}), "guided_setup": False}
        db.flush()
        db.add(
            AuditLog(
                club_id=page.club_id,
                user_id=record.user_id,
                action="meta.oauth_completed",
                entity_type="instagram_connection",
                entity_id=connection.id,
                details={
                    "page_id": page.id,
                    "account_type": account_type,
                    "scopes": sorted(scopes),
                },
            )
        )
        db.commit()
        return connection
    except Exception as exc:
        safe_error = _safe_error(exc)
        # A failed flush leaves the SQLAlchemy session unusable until rollback.
        # Persist only the sanitized rejection in a fresh transaction and never
        # replace the original exception with PendingRollbackError.
        db.rollback()
        try:
            rejected = db.get(InstagramOAuthState, record_id)
            if rejected is not None:
                rejected.used_at = datetime.now(timezone.utc)
                rejected.error = safe_error
                db.add(
                    AuditLog(
                        club_id=rejected.club_id,
                        user_id=rejected.user_id,
                        action="meta.oauth_rejected",
                        entity_type="instagram_page",
                        entity_id=rejected.instagram_page_id,
                        details={"error": safe_error},
                    )
                )
                db.commit()
        except Exception:
            db.rollback()
        raise


def reject_oauth(db: Session, settings: Settings, *, state: str, error: str) -> None:
    assert_meta_environment(settings)
    record = consume_oauth_state(db, state)
    record.error = error[:500]
    db.add(
        AuditLog(
            club_id=record.club_id,
            user_id=record.user_id,
            action="meta.oauth_rejected",
            entity_type="instagram_page",
            entity_id=record.instagram_page_id,
            details={"error": error[:500]},
        )
    )
    db.commit()


def check_connection(
    db: Session,
    settings: Settings,
    connection: InstagramConnection,
    user: User | None,
    api: MetaApiClient,
) -> InstagramConnection:
    assert_meta_environment(settings, external_call=True)
    page = db.get(InstagramPage, connection.instagram_page_id)
    if page is None or page.club_id != connection.club_id:
        raise MetaApiError("Instagram-Seite der Verbindung ist nicht eindeutig zugeordnet")
    try:
        token = TokenCipher(settings.meta_token_encryption_key).decrypt(
            connection.encrypted_token
        )
        profile = api.profile(token)
        # Instagram Login has no supported /me/permissions edge.  Revalidate
        # the token and account through /me and retain the exact scope grant
        # recorded when this connection completed OAuth.
        scopes = set(connection.scopes or [])
        connection.confirmed_username = str(profile.get("username") or "")
        connection.account_type = str(profile.get("account_type") or "").upper()
        connection.scopes = sorted(scopes)
        checked_at = datetime.now(timezone.utc)
        connection.last_check_at = checked_at
        page.last_check_at = checked_at
        if connection.account_type != "BUSINESS" or not REQUIRED_SCOPES.issubset(scopes):
            connection.status = "invalid"
            connection.last_error = "Kontoart oder Berechtigungen genügen nicht"
        else:
            connection.status = "connected"
            connection.last_success_at = checked_at
            connection.last_error = None
        page.connection_status = connection.status
        page.last_error = connection.last_error
        db.add(
            AuditLog(
                user_id=user.id if user else None,
                action=(
                    "meta.connection_checked"
                    if user
                    else "meta.connection_checked_automatic"
                ),
                entity_type="instagram_connection",
                entity_id=connection.id,
                details={
                    "status": connection.status,
                    "account_type": connection.account_type,
                    "scopes": connection.scopes,
                },
            )
        )
        db.commit()
        return connection
    except Exception as exc:
        checked_at = datetime.now(timezone.utc)
        safe_error = _safe_error(exc)
        connection.status = "error"
        connection.last_check_at = checked_at
        connection.last_error = safe_error
        page.connection_status = "error"
        page.last_check_at = checked_at
        page.last_error = safe_error
        db.add(
            AuditLog(
                user_id=user.id if user else None,
                action=(
                    "meta.connection_check_failed"
                    if user
                    else "meta.connection_check_failed_automatic"
                ),
                entity_type="instagram_connection",
                entity_id=connection.id,
                details={"error": safe_error},
            )
        )
        db.commit()
        raise


def refresh_connection(
    db: Session,
    settings: Settings,
    connection: InstagramConnection,
    user: User,
    api: MetaApiClient,
) -> InstagramConnection:
    assert_meta_environment(settings, external_call=True)
    token = TokenCipher(settings.meta_token_encryption_key).decrypt(connection.encrypted_token)
    refreshed = api.refresh_token(token, connection.instagram_user_id or "")
    connection.encrypted_token = TokenCipher(settings.meta_token_encryption_key).encrypt(
        refreshed.access_token
    )
    connection.token_expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=refreshed.expires_in
    )
    connection.token_key_version = settings.meta_token_key_version
    connection.last_check_at = datetime.now(timezone.utc)
    connection.last_success_at = datetime.now(timezone.utc)
    connection.status = "connected"
    connection.last_error = None
    db.add(
        AuditLog(
            user_id=user.id,
            action="meta.token_refreshed",
            entity_type="instagram_connection",
            entity_id=connection.id,
            details={"expires_at": connection.token_expires_at.isoformat()},
        )
    )
    db.commit()
    return connection


def disconnect(
    db: Session, connection: InstagramConnection, page: InstagramPage, user: User
) -> None:
    attempts = list(
        db.scalars(
            select(MetaPublishingAttempt).where(
                MetaPublishingAttempt.connection_id == connection.id,
                MetaPublishingAttempt.phase.notin_(["completed", "failed"]),
            )
        )
    )
    from app.models import MetaCarouselItem

    revoked_grants = 0
    for attempt in attempts:
        grant_ids = list(
            db.scalars(
                select(MetaCarouselItem.public_media_grant_id).where(
                    MetaCarouselItem.attempt_id == attempt.id,
                    MetaCarouselItem.public_media_grant_id.is_not(None),
                )
            )
        )
        if attempt.public_media_grant_id:
            grant_ids.append(attempt.public_media_grant_id)
        for grant_id in set(grant_ids):
            grant = db.get(PublicMediaGrant, grant_id)
            if grant and not grant.revoked_at:
                grant.revoked_at = datetime.now(timezone.utc)
                grant.active_key = None
                revoked_grants += 1
        if attempt.phase != "uncertain":
            attempt.phase = "failed"
            attempt.active_key = None
            attempt.error_category = "connection_disconnected"
            attempt.error_message = "Instagram-Verbindung wurde getrennt"
    connection.encrypted_token = None
    connection.status = "disconnected"
    connection.disconnected_at = datetime.now(timezone.utc)
    connection.last_error = None
    page.connection_status = "disconnected"
    page.publishing_enabled = False
    db.add(
        AuditLog(
            user_id=user.id,
            action="meta.connection_disconnected",
            entity_type="instagram_connection",
            entity_id=connection.id,
            details={
                "page_id": page.id,
                "open_attempts": len(attempts),
                "revoked_media_grants": revoked_grants,
            },
        )
    )
    db.commit()
