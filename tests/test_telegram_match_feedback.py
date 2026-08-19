from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import Settings
from app.db import get_db
from app.match_reports import feedback as feedback_service
from app.match_reports.feedback import (
    consume_feedback_response,
    request_match_feedback,
    resolve_feedback_providers,
)
from app.match_reports.feedback_providers import FeedbackSendResult, FeedbackTarget
from app.match_reports.telegram import (
    TelegramApiError,
    TelegramBotClient,
    TelegramDownloadedFile,
    consume_contact_link,
    create_contact_link,
    feedback_provider_enabled,
    safe_payload_metadata,
)
from app.match_reports.telegram_voice import (
    TelegramVoiceTranscriptionError,
    transcribe_telegram_voice,
)
from app.match_reports.telegram_webhooks import router as telegram_webhook_router
from app.models import (
    AccountType,
    Club,
    FeatureFlag,
    Game,
    MatchFeedbackContact,
    MatchFeedbackEndpoint,
    MatchFeedbackLinkToken,
    MatchFeedbackRequest,
    MatchFeedbackResponse,
    Role,
    SocialChannelConnection,
    Team,
    TelegramWebhookUpdate,
    User,
)
from app.platform.routes import feature_flag as platform_feature_flag
from app.tenancy.state import platform_scope, system_scope


class _FakeTelegramClient:
    sent: list[dict] = []
    answered: list[dict] = []

    def __init__(self, settings, token: str):
        self.settings = settings
        self.token = token

    def send_message(self, *, chat_id: str, text: str, reply_markup=None):
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"message_id": 999}

    def answer_callback_query(self, callback_query_id: str, text: str):
        self.answered.append({"callback_query_id": callback_query_id, "text": text})


def _webhook_client(db, monkeypatch, *, secret: str = "valid-secret"):
    connection = _connection(db)
    identifier = connection.settings["webhook_identifier"]
    monkeypatch.setattr(
        "app.match_reports.telegram_webhooks.secret_matches",
        lambda current, supplied, settings: current.id == connection.id and supplied == secret,
    )
    monkeypatch.setattr(
        "app.match_reports.telegram_webhooks.decrypt_bot_token",
        lambda current, settings: "123456:test-token",
    )
    _FakeTelegramClient.sent = []
    _FakeTelegramClient.answered = []
    monkeypatch.setattr(
        "app.match_reports.telegram_webhooks.TelegramBotClient",
        _FakeTelegramClient,
    )
    app = FastAPI()
    app.include_router(telegram_webhook_router)

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    return connection, identifier, TestClient(app)


def _team_and_game(db) -> tuple[Team, Game]:
    suffix = uuid4().hex[:8]
    team = Team(
        internal_name=f"Telegram-{suffix}",
        display_name=f"Testverein {suffix}",
        short_name=f"TG-{suffix[:4]}",
        slug=f"telegram-{suffix}",
        club="Testverein",
        active=True,
        fussball_url=f"https://www.fussball.de/mannschaft/{suffix}",
        media_subdir=f"telegram-{suffix}",
    )
    db.add(team)
    db.flush()
    game = Game(
        team_id=team.id,
        provider="fupa",
        external_id=f"telegram-match-{suffix}",
        home_team=team.display_name,
        away_team="Gastverein",
        kickoff=datetime.now(timezone.utc) - timedelta(hours=3),
        competition="Kreisliga",
        venue="Teststadion",
        status="finished",
        home_score=2,
        away_score=1,
        result_confirmed=True,
        source_url=f"https://www.fussball.de/spiel/{suffix}",
        fupa_url=f"https://www.fupa.net/match/{suffix}",
    )
    db.add(game)
    db.flush()
    return team, game


def _connection(db, provider: str = "telegram") -> SocialChannelConnection:
    suffix = uuid4().hex[:8]
    connection = SocialChannelConnection(
        channel_type=provider,
        internal_name=f"{provider}-{suffix}",
        display_name=f"{provider.title()} Test",
        username="test_feedback_bot" if provider == "telegram" else None,
        external_account_id=f"{provider}-{suffix}",
        status="connected",
        active=True,
        encrypted_token="encrypted-test-token",
        settings={"webhook_identifier": f"hook-{suffix}"},
    )
    db.add(connection)
    db.flush()
    return connection


def _contact(
    db,
    team: Team,
    *,
    preferred: str | None = "telegram",
    fallback: str | None = None,
) -> MatchFeedbackContact:
    contact = MatchFeedbackContact(
        team_id=team.id,
        display_name=f"Kontakt {uuid4().hex[:6]}",
        role_label="Trainer/in",
        preferred_provider=preferred,
        fallback_provider=fallback,
        request_match_reports=True,
        active=True,
    )
    db.add(contact)
    db.flush()
    return contact


def _endpoint(
    db,
    *,
    contact: MatchFeedbackContact,
    connection: SocialChannelConnection,
    provider: str,
    chat_id: str | None = None,
) -> MatchFeedbackEndpoint:
    endpoint = MatchFeedbackEndpoint(
        contact_id=contact.id,
        provider=provider,
        connection_id=connection.id,
        external_user_id=chat_id or f"user-{uuid4().hex[:6]}",
        external_chat_id=chat_id or f"chat-{uuid4().hex[:6]}",
        status="connected",
        is_primary=contact.preferred_provider == provider,
        linked_at=datetime.now(timezone.utc),
    )
    db.add(endpoint)
    db.flush()
    return endpoint


def _enable_platform_provider(db, provider: str, enabled: bool = True) -> FeatureFlag:
    with system_scope(f"pytest {provider} platform flag"):
        flag = FeatureFlag(
            club_id=None,
            key=f"match_feedback.{provider}",
            enabled=enabled,
            value={},
        )
        db.add(flag)
        db.flush()
    return flag


def test_provider_flags_default_to_whatsapp_and_fail_closed_for_telegram(db):
    club_id = db.info["test_club_id"]
    assert feedback_provider_enabled(db, club_id, "whatsapp") is True
    assert feedback_provider_enabled(db, club_id, "telegram") is False

    _enable_platform_provider(db, "telegram")
    assert feedback_provider_enabled(db, club_id, "telegram") is True

    db.add(
        FeatureFlag(
            club_id=club_id,
            key="match_feedback.telegram",
            enabled=False,
            value={},
        )
    )
    db.flush()
    assert feedback_provider_enabled(db, club_id, "telegram") is False


def test_only_platform_admin_can_change_club_provider_entitlements(db):
    club_id = db.info["test_club_id"]
    club_admin = User(
        email=f"club-admin-{uuid4().hex[:8]}@test.invalid",
        password_hash="test-only",
        role=Role.ADMIN,
        account_type=AccountType.CLUB_USER,
        club_id=club_id,
        all_teams=True,
    )
    db.add(club_admin)
    db.flush()
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/platform/feature-flags",
            "headers": [],
            "session": {"csrf": "test-csrf"},
        }
    )
    with pytest.raises(HTTPException) as exc_info:
        platform_feature_flag(
            request=request,
            csrf_token_value="test-csrf",
            key="match_feedback.telegram",
            enabled=True,
            value_json="{}",
            club_id=club_id,
            current=club_admin,
            db=db,
        )
    assert exc_info.value.status_code == 403

    with system_scope("PlatformAdmin für Messenger-Freischaltung anlegen"):
        platform_admin = User(
            email=f"platform-admin-{uuid4().hex[:8]}@test.invalid",
            password_hash="test-only",
            role=Role.ADMIN,
            account_type=AccountType.PLATFORM_ADMIN,
            club_id=None,
            all_teams=True,
        )
        db.add(platform_admin)
        db.commit()
    with platform_scope(platform_admin.id):
        response = platform_feature_flag(
            request=request,
            csrf_token_value="test-csrf",
            key="match_feedback.telegram",
            enabled=True,
            value_json="{}",
            club_id=club_id,
            current=platform_admin,
            db=db,
        )
    assert response.status_code == 303
    with system_scope("Messenger-Freischaltung prüfen"):
        flag = db.scalar(
            select(FeatureFlag).where(
                FeatureFlag.club_id == club_id,
                FeatureFlag.key == "match_feedback.telegram",
            )
        )
    assert flag is not None
    assert flag.enabled is True


def test_platform_pause_overrides_club_enablement(db):
    club_id = db.info["test_club_id"]
    _enable_platform_provider(db, "telegram", enabled=False)
    db.add(
        FeatureFlag(
            club_id=club_id,
            key="match_feedback.telegram",
            enabled=True,
            value={},
        )
    )
    db.flush()
    assert feedback_provider_enabled(db, club_id, "telegram") is False


def test_telegram_contact_link_is_opaque_short_lived_and_single_use(db):
    team, _ = _team_and_game(db)
    contact = _contact(db, team)
    connection = _connection(db)
    settings = Settings(telegram_link_ttl_minutes=15)

    link = create_contact_link(
        db,
        contact=contact,
        connection=connection,
        created_by="pytest",
        settings=settings,
    )
    db.flush()
    raw_token = link.split("?start=", 1)[1]
    stored = db.scalar(select(MatchFeedbackLinkToken))
    assert stored is not None
    assert raw_token not in stored.token_digest
    assert len(raw_token) <= 64
    stored_expiry = stored.expires_at
    if stored_expiry.tzinfo is None:
        stored_expiry = stored_expiry.replace(tzinfo=timezone.utc)
    assert stored_expiry > datetime.now(timezone.utc)

    endpoint = consume_contact_link(
        db,
        connection=connection,
        raw_token=raw_token,
        external_user_id="1234",
        external_chat_id="1234",
        external_username="trainer",
    )
    assert endpoint is not None
    assert endpoint.club_id == contact.club_id
    assert endpoint.contact_id == contact.id
    assert endpoint.status == "connected"
    assert endpoint.is_primary is True
    assert stored.used_at is not None
    assert (
        consume_contact_link(
            db,
            connection=connection,
            raw_token=raw_token,
            external_user_id="1234",
            external_chat_id="1234",
            external_username="trainer",
        )
        is None
    )


def test_telegram_link_respects_existing_whatsapp_preference(db):
    team, _ = _team_and_game(db)
    contact = _contact(db, team, preferred="whatsapp", fallback="telegram")
    connection = _connection(db)
    settings = Settings()
    link = create_contact_link(
        db,
        contact=contact,
        connection=connection,
        created_by="pytest",
        settings=settings,
    )
    db.flush()
    endpoint = consume_contact_link(
        db,
        connection=connection,
        raw_token=link.split("?start=", 1)[1],
        external_user_id="4321",
        external_chat_id="4321",
        external_username=None,
    )
    assert endpoint is not None
    assert endpoint.is_primary is False
    assert contact.preferred_provider == "whatsapp"
    assert contact.fallback_provider == "telegram"


def test_provider_resolution_uses_contact_then_team_then_club(db):
    team, game = _team_and_game(db)
    current_club = db.scalar(select(Club).where(Club.id == game.club_id))
    current_club.technical_settings = {
        **(current_club.technical_settings or {}),
        "match_feedback_messenger": {
            "preferred_provider": "whatsapp",
            "fallback_provider": "telegram",
        },
    }
    team.rules = {
        **(team.rules or {}),
        "match_feedback_messenger": {
            "preferred_provider": "telegram",
            "fallback_provider": "whatsapp",
        },
    }
    inherited = _contact(db, team, preferred=None, fallback=None)
    assert resolve_feedback_providers(db, contact=inherited, game=game) == (
        "telegram",
        "whatsapp",
    )

    explicit = _contact(db, team, preferred="whatsapp", fallback="telegram")
    assert resolve_feedback_providers(db, contact=explicit, game=game) == (
        "whatsapp",
        "telegram",
    )


def test_failed_primary_uses_only_explicit_fallback(db, monkeypatch):
    team, game = _team_and_game(db)
    contact = _contact(db, team, preferred="telegram", fallback="whatsapp")
    telegram_connection = _connection(db, "telegram")
    whatsapp_connection = _connection(db, "whatsapp")
    telegram_endpoint = _endpoint(
        db,
        contact=contact,
        connection=telegram_connection,
        provider="telegram",
    )
    whatsapp_endpoint = _endpoint(
        db,
        contact=contact,
        connection=whatsapp_connection,
        provider="whatsapp",
    )
    _enable_platform_provider(db, "telegram")

    class FailingProvider:
        def send(self, *args, **kwargs):
            raise TelegramApiError("private provider detail", status_code=503)

    class SuccessfulProvider:
        def send(self, *args, **kwargs):
            return FeedbackSendResult("wa-message", "wa-chat")

    targets = {
        "telegram": FeedbackTarget(contact, telegram_endpoint, telegram_connection),
        "whatsapp": FeedbackTarget(contact, whatsapp_endpoint, whatsapp_connection),
    }
    monkeypatch.setitem(feedback_service.PROVIDERS, "telegram", FailingProvider())
    monkeypatch.setitem(feedback_service.PROVIDERS, "whatsapp", SuccessfulProvider())
    monkeypatch.setattr(
        feedback_service,
        "_target_for_provider",
        lambda _db, *, contact, game, provider: targets[provider],
    )

    sent = request_match_feedback(
        db,
        game,
        SimpleNamespace(fupa_report_feedback_wait_minutes=30),
    )
    requests = list(
        db.scalars(select(MatchFeedbackRequest).order_by(MatchFeedbackRequest.created_at))
    )
    assert sent == 1
    assert [(item.provider, item.status) for item in requests] == [
        ("telegram", "failed"),
        ("whatsapp", "sent"),
    ]
    assert "private provider detail" not in requests[0].last_error


def test_response_correlation_fails_closed_when_chat_is_ambiguous(db):
    team, game = _team_and_game(db)
    contact = _contact(db, team)
    connection = _connection(db)
    _endpoint(
        db,
        contact=contact,
        connection=connection,
        provider="telegram",
        chat_id="chat-42",
    )
    future = datetime.now(timezone.utc) + timedelta(minutes=30)
    for index in (1, 2):
        db.add(
            MatchFeedbackRequest(
                game_id=game.id,
                team_id=team.id,
                contact_id=contact.id,
                channel_connection_id=connection.id,
                provider="telegram",
                external_chat_id="chat-42",
                external_message_id=f"question-{index}",
                provider_message_id=f"question-{index}",
                idempotency_key=f"request-{uuid4().hex}",
                requested_at=datetime.now(timezone.utc),
                deadline_at=future,
                status="sent",
                delivery_status="sent",
            )
        )
    db.flush()

    assert (
        consume_feedback_response(
            db,
            connection=connection,
            provider="telegram",
            sender="42",
            provider_message_id="answer-1",
            body="Bestätigte Beobachtung",
            external_chat_id="chat-42",
        )
        is False
    )
    assert db.scalar(select(func.count(MatchFeedbackResponse.id))) == 0

    assert (
        consume_feedback_response(
            db,
            connection=connection,
            provider="telegram",
            sender="42",
            provider_message_id="answer-1",
            body="Bestätigte Beobachtung",
            external_chat_id="chat-42",
            reply_to_message_id="question-1",
        )
        is True
    )
    assert db.scalar(select(func.count(MatchFeedbackResponse.id))) == 1
    assert (
        consume_feedback_response(
            db,
            connection=connection,
            provider="telegram",
            sender="42",
            provider_message_id="answer-1",
            body="Duplikat",
            external_chat_id="chat-42",
            reply_to_message_id="question-1",
        )
        is True
    )
    assert db.scalar(select(func.count(MatchFeedbackResponse.id))) == 1


def test_disabled_telegram_webhook_is_idempotently_logged_without_mutation(db, monkeypatch):
    connection = _connection(db)
    identifier = connection.settings["webhook_identifier"]
    monkeypatch.setattr(
        "app.match_reports.telegram_webhooks.secret_matches",
        lambda connection, supplied, settings: supplied == "valid-secret",
    )
    app = FastAPI()
    app.include_router(telegram_webhook_router)

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    payload = {
        "update_id": 987654,
        "message": {
            "message_id": 12,
            "chat": {"id": 55, "type": "private"},
            "from": {"id": 55, "username": "trainer"},
            "text": "/start cannot-be-consumed",
        },
    }
    with TestClient(app) as client:
        first = client.post(
            f"/webhooks/telegram/{identifier}",
            json=payload,
            headers={"X-Telegram-Bot-Api-Secret-Token": "valid-secret"},
        )
        second = client.post(
            f"/webhooks/telegram/{identifier}",
            json=payload,
            headers={"X-Telegram-Bot-Api-Secret-Token": "valid-secret"},
        )
    assert first.status_code == 200
    assert second.status_code == 200
    ledger = list(db.scalars(select(TelegramWebhookUpdate)))
    assert len(ledger) == 1
    assert ledger[0].status == "ignored_disabled"
    assert db.scalar(select(func.count(MatchFeedbackEndpoint.id))) == 0


def test_telegram_webhook_rejects_invalid_secret_before_ledger_write(db, monkeypatch):
    connection = _connection(db)
    identifier = connection.settings["webhook_identifier"]
    monkeypatch.setattr(
        "app.match_reports.telegram_webhooks.secret_matches",
        lambda connection, supplied, settings: False,
    )
    app = FastAPI()
    app.include_router(telegram_webhook_router)

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as client:
        response = client.post(
            f"/webhooks/telegram/{identifier}",
            json={"update_id": 1},
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
        )
    assert response.status_code == 403
    assert db.scalar(select(func.count(TelegramWebhookUpdate.id))) == 0


def test_telegram_errors_and_media_metadata_never_expose_token(monkeypatch):
    token = "123456:super-secret-token"
    request = httpx.Request("POST", f"https://api.telegram.org/bot{token}/getMe")

    def fail(*args, **kwargs):
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    monkeypatch.setattr(httpx, "post", fail)
    client = TelegramBotClient(Settings(), token)
    with pytest.raises(TelegramApiError) as exc_info:
        client.get_me()
    assert token not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert safe_payload_metadata(
        {"voice": {"file_id": "secret-provider-id"}, "caption": "Hinweis"}
    ) == {
        "has_photo": False,
        "has_document": False,
        "has_video": False,
        "has_voice": True,
        "has_audio": False,
        "has_video_note": False,
        "has_sticker": False,
        "has_location": False,
        "has_caption": True,
    }


def test_telegram_client_get_me_send_and_rate_limit(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def success(url, *, json, timeout):
        calls.append((url.rsplit("/", 1)[-1], json))
        method = url.rsplit("/", 1)[-1]
        payload = (
            {"id": 42, "username": "vereins_bot", "first_name": "Vereinsbot"}
            if method == "getMe"
            else {"message_id": 77}
        )
        return httpx.Response(
            200,
            json={"ok": True, "result": payload},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", success)
    client = TelegramBotClient(Settings(), "123456:secret-token")
    identity = client.get_me()
    assert (identity.bot_id, identity.username, identity.display_name) == (
        "42",
        "vereins_bot",
        "Vereinsbot",
    )
    assert client.send_message(chat_id="123", text="Hallo") == {"message_id": 77}
    assert calls == [("getMe", {}), ("sendMessage", {"chat_id": "123", "text": "Hallo"})]

    def rate_limited(url, *, json, timeout):
        return httpx.Response(
            429,
            json={
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests",
                "parameters": {"retry_after": 17},
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", rate_limited)
    with pytest.raises(TelegramApiError) as exc_info:
        client.send_message(chat_id="123", text="Hallo")
    assert exc_info.value.status_code == 429
    assert exc_info.value.retry_after == 17
    assert exc_info.value.permanent is False


def test_telegram_client_downloads_voice_without_exposing_token(monkeypatch):
    token = "123456:secret-token"

    def telegram_post(url, *, json, timeout):
        assert token in url
        assert json == {"file_id": "voice-id"}
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"file_path": "voice/file_0.oga", "file_size": 8},
            },
            request=httpx.Request("POST", url),
        )

    def telegram_get(url, *, timeout):
        assert token in url
        return httpx.Response(
            200,
            content=b"ogg-data",
            headers={"content-length": "8"},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx, "post", telegram_post)
    monkeypatch.setattr(httpx, "get", telegram_get)

    downloaded = TelegramBotClient(Settings(), token).download_file("voice-id", max_bytes=100)

    assert downloaded == TelegramDownloadedFile(
        content=b"ogg-data",
        filename="file_0.oga",
        mime_type="audio/ogg",
    )


@pytest.mark.parametrize("file_path", ["../secret", "/etc/passwd", "C:/secret", "a\\b"])
def test_telegram_client_rejects_unsafe_download_paths(monkeypatch, file_path):
    def telegram_post(url, *, json, timeout):
        return httpx.Response(
            200,
            json={"ok": True, "result": {"file_path": file_path, "file_size": 8}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", telegram_post)
    with pytest.raises(TelegramApiError, match="nicht zulässig"):
        TelegramBotClient(Settings(), "123456:secret-token").download_file(
            "voice-id", max_bytes=100
        )


def test_voice_transcription_converts_transient_audio_and_returns_text(monkeypatch):
    captured: dict = {}

    class FakeClient:
        def download_file(self, file_id, *, max_bytes):
            captured["download"] = (file_id, max_bytes)
            return TelegramDownloadedFile(b"telegram-ogg", "voice.oga", "audio/ogg")

    class FakeTranscriptions:
        def create(self, **kwargs):
            captured["request"] = kwargs
            return SimpleNamespace(text="  In der 55. Minute fiel der Ausgleich.  ")

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
            self.audio = SimpleNamespace(transcriptions=FakeTranscriptions())

    monkeypatch.setattr("app.match_reports.telegram_voice.OpenAI", FakeOpenAI)
    monkeypatch.setattr(
        "app.match_reports.telegram_voice._to_wav",
        lambda content, **kwargs: b"wav-data",
    )
    settings = Settings(
        openai_api_key="test-key",
        telegram_voice_transcription_model="gpt-transcribe",
    )

    result = transcribe_telegram_voice(
        client=FakeClient(),
        message={"voice": {"file_id": "voice-id", "duration": 12, "file_size": 42}},
        settings=settings,
    )

    assert result.text == "In der 55. Minute fiel der Ausgleich."
    assert result.duration_seconds == 12
    assert captured["download"] == ("voice-id", settings.telegram_voice_max_bytes)
    assert captured["request"]["model"] == "gpt-transcribe"
    assert captured["request"]["language"] == "de"
    assert captured["request"]["file"].getvalue() == b"wav-data"


def test_voice_transcription_rejects_missing_key_and_oversized_audio():
    message = {"voice": {"file_id": "voice-id", "duration": 12, "file_size": 42}}
    with pytest.raises(TelegramVoiceTranscriptionError) as exc_info:
        transcribe_telegram_voice(client=object(), message=message, settings=Settings())
    assert exc_info.value.reason == "service_not_configured"

    settings = Settings(openai_api_key="test-key", telegram_voice_max_bytes=10)
    with pytest.raises(TelegramVoiceTranscriptionError) as exc_info:
        transcribe_telegram_voice(client=object(), message=message, settings=settings)
    assert exc_info.value.reason == "size_exceeded"


@pytest.mark.parametrize("status_code", [401, 403, 404])
def test_telegram_client_classifies_permanent_api_errors(monkeypatch, status_code):
    def rejected(url, *, json, timeout):
        return httpx.Response(
            status_code,
            json={
                "ok": False,
                "error_code": status_code,
                "description": "Provider detail",
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", rejected)
    with pytest.raises(TelegramApiError) as exc_info:
        TelegramBotClient(Settings(), "123456:secret-token").send_message(
            chat_id="123", text="Hallo"
        )
    assert exc_info.value.status_code == status_code
    assert exc_info.value.permanent is True


def test_contact_link_rejects_expired_manipulated_and_other_club(db):
    team, _ = _team_and_game(db)
    contact = _contact(db, team)
    connection = _connection(db)
    link = create_contact_link(
        db,
        contact=contact,
        connection=connection,
        created_by="pytest",
        settings=Settings(telegram_link_ttl_minutes=15),
    )
    db.flush()
    raw = link.split("?start=", 1)[1]
    stored = db.scalar(select(MatchFeedbackLinkToken))
    stored.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.flush()
    assert (
        consume_contact_link(
            db,
            connection=connection,
            raw_token=raw,
            external_user_id="1234",
            external_chat_id="1234",
            external_username=None,
        )
        is None
    )
    assert (
        consume_contact_link(
            db,
            connection=connection,
            raw_token=f"{raw}manipuliert",
            external_user_id="1234",
            external_chat_id="1234",
            external_username=None,
        )
        is None
    )

    with system_scope("pytest second tenant"):
        original_club = db.scalar(select(Club).where(Club.id == team.club_id))
        assert original_club is not None
        other_club = Club(
            name=f"Anderer Verein {uuid4().hex[:6]}",
            short_name="AV",
            slug=f"anderer-verein-{uuid4().hex[:8]}",
            status="ACTIVE",
            timezone="Europe/Berlin",
            plan_profile_id=original_club.plan_profile_id,
        )
        db.add(other_club)
        db.flush()
        other_connection = SocialChannelConnection(
            club_id=other_club.id,
            channel_type="telegram",
            internal_name="telegram-other",
            display_name="Telegram anderer Verein",
            username="other_feedback_bot",
            external_account_id=f"telegram-{uuid4().hex}",
            status="connected",
            active=True,
            encrypted_token="encrypted-test-token",
            settings={"webhook_identifier": f"hook-{uuid4().hex}"},
        )
        db.add(other_connection)
        db.flush()
        assert (
            consume_contact_link(
                db,
                connection=other_connection,
                raw_token=raw,
                external_user_id="1234",
                external_chat_id="1234",
                external_username=None,
            )
            is None
        )


def test_enabled_webhook_links_contact_and_is_idempotent(db, monkeypatch):
    team, _ = _team_and_game(db)
    contact = _contact(db, team)
    _enable_platform_provider(db, "telegram")
    connection, identifier, client = _webhook_client(db, monkeypatch)
    link = create_contact_link(
        db,
        contact=contact,
        connection=connection,
        created_by="pytest",
        settings=Settings(),
    )
    db.flush()
    raw = link.split("?start=", 1)[1]
    payload = {
        "update_id": 7001,
        "message": {
            "message_id": 81,
            "chat": {"id": 4242, "type": "private"},
            "from": {"id": 4242, "username": "trainer"},
            "text": f"/start {raw}",
        },
    }
    with client:
        first = client.post(
            f"/webhooks/telegram/{identifier}",
            json=payload,
            headers={"X-Telegram-Bot-Api-Secret-Token": "valid-secret"},
        )
        second = client.post(
            f"/webhooks/telegram/{identifier}",
            json=payload,
            headers={"X-Telegram-Bot-Api-Secret-Token": "valid-secret"},
        )
    assert first.status_code == second.status_code == 200
    endpoint = db.scalar(
        select(MatchFeedbackEndpoint).where(
            MatchFeedbackEndpoint.contact_id == contact.id,
            MatchFeedbackEndpoint.provider == "telegram",
        )
    )
    assert endpoint is not None
    assert endpoint.external_chat_id == "4242"
    assert db.scalar(select(func.count(TelegramWebhookUpdate.id))) == 1
    assert len(_FakeTelegramClient.sent) == 1
    assert "Verbindung erfolgreich" in _FakeTelegramClient.sent[0]["text"]


def test_webhook_text_voice_callback_unknown_and_unknown_connection(db, monkeypatch):
    team, game = _team_and_game(db)
    contact = _contact(db, team)
    _enable_platform_provider(db, "telegram")
    connection, identifier, client = _webhook_client(db, monkeypatch)
    _endpoint(
        db,
        contact=contact,
        connection=connection,
        provider="telegram",
        chat_id="5151",
    )
    deadline = datetime.now(timezone.utc) + timedelta(minutes=30)
    first_request = MatchFeedbackRequest(
        game_id=game.id,
        team_id=team.id,
        contact_id=contact.id,
        channel_connection_id=connection.id,
        provider="telegram",
        external_chat_id="5151",
        external_message_id="91",
        provider_message_id="91",
        idempotency_key=f"request-{uuid4().hex}",
        requested_at=datetime.now(timezone.utc),
        deadline_at=deadline,
        status="sent",
        delivery_status="sent",
    )
    db.add(first_request)
    db.flush()
    text_update = {
        "update_id": 7101,
        "message": {
            "message_id": 92,
            "chat": {"id": 5151, "type": "private"},
            "from": {"id": 5151},
            "reply_to_message": {"message_id": 91},
            "text": "Nach dem Ausgleich wurde die Mannschaft stärker.",
        },
    }
    with client:
        assert (
            client.post(
                f"/webhooks/telegram/{identifier}",
                json=text_update,
                headers={"X-Telegram-Bot-Api-Secret-Token": "valid-secret"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/webhooks/telegram/unbekannt",
                json={"update_id": 7102},
                headers={"X-Telegram-Bot-Api-Secret-Token": "valid-secret"},
            ).status_code
            == 404
        )
        assert (
            client.post(
                f"/webhooks/telegram/{identifier}",
                json={"update_id": 7103, "edited_message": {"message_id": 1}},
                headers={"X-Telegram-Bot-Api-Secret-Token": "valid-secret"},
            ).status_code
            == 200
        )
    response = db.scalar(
        select(MatchFeedbackResponse).where(MatchFeedbackResponse.provider_message_id == "92")
    )
    assert response is not None
    assert response.body.startswith("Nach dem Ausgleich")
    unknown = db.scalar(
        select(TelegramWebhookUpdate).where(TelegramWebhookUpdate.update_id == "7103")
    )
    assert unknown.status == "ignored_non_private"

    voice_request = MatchFeedbackRequest(
        game_id=game.id,
        team_id=team.id,
        contact_id=contact.id,
        channel_connection_id=connection.id,
        provider="telegram",
        external_chat_id="5151",
        external_message_id="101",
        provider_message_id="101",
        idempotency_key=f"request-{uuid4().hex}",
        requested_at=datetime.now(timezone.utc),
        deadline_at=deadline,
        status="sent",
        delivery_status="sent",
    )
    db.add(voice_request)
    db.flush()
    monkeypatch.setattr(
        "app.match_reports.telegram_webhooks.transcribe_telegram_voice",
        lambda **kwargs: SimpleNamespace(
            text="Nach dem Ausgleich wurde die Mannschaft stärker.",
            model="gpt-transcribe",
            duration_seconds=9,
        ),
    )
    with TestClient(client.app) as active_client:
        assert (
            active_client.post(
                f"/webhooks/telegram/{identifier}",
                json={
                    "update_id": 7104,
                    "message": {
                        "message_id": 102,
                        "chat": {"id": 5151, "type": "private"},
                        "from": {"id": 5151},
                        "reply_to_message": {"message_id": 101},
                        "voice": {"file_id": "must-not-be-stored"},
                    },
                },
                headers={"X-Telegram-Bot-Api-Secret-Token": "valid-secret"},
            ).status_code
            == 200
        )
    voice = db.scalar(
        select(MatchFeedbackResponse).where(MatchFeedbackResponse.provider_message_id == "102")
    )
    assert voice.payload_type == "voice"
    assert voice.body == "Nach dem Ausgleich wurde die Mannschaft stärker."
    assert voice.payload_metadata["has_voice"] is True
    assert voice.payload_metadata["transcription"] == {
        "status": "completed",
        "model": "gpt-transcribe",
        "duration_seconds": 9,
    }
    assert "must-not-be-stored" not in str(voice.payload_metadata)

    failed_voice_request = MatchFeedbackRequest(
        game_id=game.id,
        team_id=team.id,
        contact_id=contact.id,
        channel_connection_id=connection.id,
        provider="telegram",
        external_chat_id="5151",
        external_message_id="106",
        provider_message_id="106",
        idempotency_key=f"request-{uuid4().hex}",
        requested_at=datetime.now(timezone.utc),
        deadline_at=deadline,
        status="sent",
        delivery_status="sent",
    )
    db.add(failed_voice_request)
    db.flush()

    def fail_transcription(**kwargs):
        raise TelegramVoiceTranscriptionError("provider_failed")

    monkeypatch.setattr(
        "app.match_reports.telegram_webhooks.transcribe_telegram_voice",
        fail_transcription,
    )
    with TestClient(client.app) as active_client:
        assert (
            active_client.post(
                f"/webhooks/telegram/{identifier}",
                json={
                    "update_id": 7195,
                    "message": {
                        "message_id": 107,
                        "chat": {"id": 5151, "type": "private"},
                        "from": {"id": 5151},
                        "reply_to_message": {"message_id": 106},
                        "voice": {"file_id": "must-not-be-stored-on-failure"},
                    },
                },
                headers={"X-Telegram-Bot-Api-Secret-Token": "valid-secret"},
            ).status_code
            == 200
        )
    assert (
        db.scalar(
            select(MatchFeedbackResponse).where(MatchFeedbackResponse.provider_message_id == "107")
        )
        is None
    )
    db.refresh(failed_voice_request)
    assert failed_voice_request.status == "sent"
    failed_update = db.scalar(
        select(TelegramWebhookUpdate).where(TelegramWebhookUpdate.update_id == "7195")
    )
    assert failed_update.status == "voice_transcription_failed"
    assert any("noch einmal als Text" in sent["text"] for sent in _FakeTelegramClient.sent)

    callback_request = MatchFeedbackRequest(
        game_id=game.id,
        team_id=team.id,
        contact_id=contact.id,
        channel_connection_id=connection.id,
        provider="telegram",
        external_chat_id="5151",
        external_message_id="111",
        provider_message_id="111",
        idempotency_key=f"request-{uuid4().hex}",
        requested_at=datetime.now(timezone.utc),
        deadline_at=deadline,
        status="sent",
        delivery_status="sent",
    )
    db.add(callback_request)
    db.flush()
    with TestClient(client.app) as active_client:
        assert (
            active_client.post(
                f"/webhooks/telegram/{identifier}",
                json={
                    "update_id": 7105,
                    "callback_query": {
                        "id": "callback-1",
                        "from": {"id": 5151},
                        "message": {"chat": {"id": 5151, "type": "private"}},
                        "data": f"match_feedback:none:{callback_request.id}",
                    },
                },
                headers={"X-Telegram-Bot-Api-Secret-Token": "valid-secret"},
            ).status_code
            == 200
        )
    callback_response = db.scalar(
        select(MatchFeedbackResponse).where(
            MatchFeedbackResponse.provider_message_id == "callback:callback-1"
        )
    )
    assert callback_response.no_additional_feedback is True
    assert _FakeTelegramClient.answered[-1]["text"] == "Keine Ergänzungen gespeichert"


def test_both_disabled_providers_do_not_block_report_flow(db, monkeypatch):
    team, game = _team_and_game(db)
    _contact(db, team, preferred="telegram", fallback="whatsapp")
    _enable_platform_provider(db, "telegram", enabled=False)
    _enable_platform_provider(db, "whatsapp", enabled=False)

    def must_not_send(*args, **kwargs):
        raise AssertionError("Deaktivierter Provider wurde aufgerufen")

    monkeypatch.setattr(feedback_service, "_target_for_provider", must_not_send)
    assert (
        request_match_feedback(
            db,
            game,
            SimpleNamespace(fupa_report_feedback_wait_minutes=30),
        )
        == 0
    )
    assert db.scalar(select(func.count(MatchFeedbackRequest.id))) == 0
