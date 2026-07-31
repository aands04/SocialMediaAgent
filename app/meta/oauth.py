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
    if settings.environment != "meta-test" or not settings.meta_test_enabled:
        raise MetaApiError("Instagram-Verbindungen sind nur in der Meta-Testumgebung erlaubt")
    if external_call and not settings.meta_test_publish_enabled:
        raise MetaApiError("Externe Meta-Aufrufe sind durch META_TEST_PUBLISH_ENABLED gesperrt")


def start_oauth(
    db: Session,
    settings: Settings,
    page: InstagramPage,
    user: User,
    api: MetaApiClient,
) -> str:
    assert_meta_environment(settings)
    if not settings.meta_oauth_redirect_uri:
        raise MetaApiError("META_OAUTH_REDIRECT_URI fehlt")
    state = random_oauth_state()
    db.add(
        InstagramOAuthState(
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
    try:
        initiating_user = db.get(User, record.user_id)
        if not initiating_user or not initiating_user.active:
            raise MetaApiError("Der Benutzer, der OAuth gestartet hat, ist nicht mehr aktiv")
        short = api.exchange_code(code, record.redirect_uri)
        token = api.exchange_long_lived(short)
        profile = api.profile(token.access_token)
        scopes = _oauth_grant_scopes()
        account_type = str(profile.get("account_type") or "").upper()
        if account_type != "BUSINESS":
            raise MetaApiError("Für Story-Tests ist ein professionelles Business-Konto erforderlich")
        profile_id = str(profile.get("user_id") or profile.get("id") or short.user_id)
        page = db.get(InstagramPage, record.instagram_page_id)
        if not page:
            raise MetaApiError("Zielseite wurde während OAuth entfernt")
        confirmed_username = str(profile.get("username") or "")
        if (
            page.username
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
        connection = existing or InstagramConnection(instagram_page_id=page.id)
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
        page.connection_status = "connected"
        page.last_check_at = datetime.now(timezone.utc)
        db.add(
            AuditLog(
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
        record.error = safe_error
        db.add(
            AuditLog(
                user_id=record.user_id,
                action="meta.oauth_rejected",
                entity_type="instagram_page",
                entity_id=record.instagram_page_id,
                details={"error": safe_error},
            )
        )
        db.commit()
        raise


def reject_oauth(db: Session, settings: Settings, *, state: str, error: str) -> None:
    assert_meta_environment(settings)
    record = consume_oauth_state(db, state)
    record.error = error[:500]
    db.add(
        AuditLog(
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
    user: User,
    api: MetaApiClient,
) -> InstagramConnection:
    assert_meta_environment(settings, external_call=True)
    token = TokenCipher(settings.meta_token_encryption_key).decrypt(connection.encrypted_token)
    try:
        profile = api.profile(token)
        # Instagram Login has no supported /me/permissions edge.  Revalidate
        # the token and account through /me and retain the exact scope grant
        # recorded when this connection completed OAuth.
        scopes = set(connection.scopes or [])
        connection.confirmed_username = str(profile.get("username") or "")
        connection.account_type = str(profile.get("account_type") or "").upper()
        connection.scopes = sorted(scopes)
        connection.last_check_at = datetime.now(timezone.utc)
        if connection.account_type != "BUSINESS" or not REQUIRED_SCOPES.issubset(scopes):
            connection.status = "invalid"
            connection.last_error = "Kontoart oder Berechtigungen genügen nicht"
        else:
            connection.status = "connected"
            connection.last_success_at = datetime.now(timezone.utc)
            connection.last_error = None
        db.add(
            AuditLog(
                user_id=user.id,
                action="meta.connection_checked",
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
        connection.status = "error"
        connection.last_check_at = datetime.now(timezone.utc)
        connection.last_error = _safe_error(exc)
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
    revoked_grants = 0
    for attempt in attempts:
        if attempt.public_media_grant_id:
            grant = db.get(PublicMediaGrant, attempt.public_media_grant_id)
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
