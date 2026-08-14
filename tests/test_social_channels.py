from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.channels.api import (
    FACEBOOK_REQUIRED_SCOPES,
    WHATSAPP_REQUIRED_SCOPES,
    ChannelApiError,
    MetaGraphClient,
    MetaToken,
)
from app.channels.capabilities import capability_keys, status_label
from app.channels.delivery import _deliver_one, _whatsapp_components
from app.channels.jobs import ensure_approved_channel_jobs
from app.channels.oauth import (
    activate_existing_whatsapp_phone,
    assert_channel_enabled,
    complete_facebook_selection,
    complete_whatsapp_onboarding,
    ensure_whatsapp_webhook_subscription,
    prepare_facebook_selection,
    start_channel_oauth,
)
from app.channels.service import channel_cards
from app.channels.webhooks import (
    _process_whatsapp_payload,
    _resolve_whatsapp_connection,
    _verify_signature,
    _whatsapp_identifiers,
)
from app.config import Settings
from app.meta.security import TokenCipher
from app.models import (
    AuditLog,
    ChannelDeliveryAttempt,
    InstagramPage,
    JobStatus,
    Post,
    PostStatus,
    PublicationJob,
    Role,
    SocialChannelConnection,
    SocialChannelOAuthState,
    Team,
    TeamChannelAssignment,
    User,
    WhatsAppMessageTemplate,
    WhatsAppRecipient,
)


def channel_settings() -> Settings:
    return Settings(
        environment="production",
        meta_production_enabled=True,
        facebook_channel_enabled=True,
        whatsapp_channel_enabled=True,
        meta_app_id="instagram-app",
        meta_app_secret="instagram-secret",
        meta_facebook_app_id="channels-app",
        meta_facebook_app_secret="channels-secret",
        meta_facebook_oauth_redirect_uri=(
            "https://meta.example.invalid/public/meta/channels/oauth/callback"
        ),
        meta_token_encryption_key=Fernet.generate_key().decode("ascii"),
    )


def test_channel_client_never_falls_back_to_instagram_credentials():
    settings = Settings(
        _env_file=None,
        meta_app_id="instagram-app",
        meta_app_secret="instagram-secret",
    )
    client = MetaGraphClient(settings)

    with pytest.raises(ChannelApiError, match="Facebook und WhatsApp"):
        client.authorization_url(
            state="state",
            redirect_uri="https://example.invalid/callback",
            channel_type="whatsapp",
        )


def test_channel_client_uses_dedicated_meta_app_id():
    settings = channel_settings()
    url = MetaGraphClient(settings).authorization_url(
        state="state",
        redirect_uri="https://example.invalid/callback",
        channel_type="whatsapp",
    )

    assert "client_id=channels-app" in url
    assert "instagram-app" not in url


def test_whatsapp_phone_registration_uses_official_endpoint_without_exposing_pin():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"success": True})

    client = MetaGraphClient(
        channel_settings(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = client.register_whatsapp_phone(
        phone_number_id="222222",
        access_token="whatsapp-access-token",
        pin="123456",
    )

    assert result == {"success": True}
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path.endswith("/222222/register")
    assert json.loads(request.content) == {
        "messaging_product": "whatsapp",
        "pin": "123456",
    }
    assert request.headers["Authorization"] == "Bearer whatsapp-access-token"


def test_facebook_and_whatsapp_are_available_by_default():
    settings = Settings(
        _env_file=None,
        environment="production",
        meta_production_enabled=True,
    )

    assert settings.facebook_channel_enabled is True
    assert settings.whatsapp_channel_enabled is True
    assert_channel_enabled(settings, "facebook")
    assert_channel_enabled(settings, "whatsapp")


@pytest.mark.parametrize("channel_type", ["facebook", "whatsapp"])
def test_platform_wide_channel_pause_remains_an_explicit_emergency_gate(channel_type):
    settings = Settings(
        _env_file=None,
        environment="production",
        meta_production_enabled=True,
        facebook_channel_enabled=channel_type != "facebook",
        whatsapp_channel_enabled=channel_type != "whatsapp",
    )

    with pytest.raises(ChannelApiError, match="plattformweit vorübergehend pausiert"):
        assert_channel_enabled(settings, channel_type)


def admin_user(db) -> User:
    item = User(
        email="channels@example.invalid",
        password_hash="not-used",
        role=Role.ADMIN,
        all_teams=True,
    )
    db.add(item)
    db.flush()
    return item


class FacebookApiStub:
    def authorization_url(self, *, state, redirect_uri, channel_type):
        return f"https://www.facebook.com/dialog/oauth?state={state}&redirect_uri={redirect_uri}&type={channel_type}"

    def exchange_code(self, *, code, redirect_uri=None):
        assert code == "oauth-code"
        assert redirect_uri
        return MetaToken("user-access-token", 3600)

    def granted_permissions(self, access_token):
        assert access_token == "user-access-token"
        return set(FACEBOOK_REQUIRED_SCOPES)

    def managed_pages(self, access_token):
        assert access_token == "user-access-token"
        return [
            {
                "id": "123456",
                "name": "Testvereinsseite",
                "access_token": "page-access-token",
                "tasks": ["CREATE_CONTENT"],
                "can_publish": True,
            }
        ]

    def page_profile(self, *, page_id, access_token):
        assert page_id == "123456"
        assert access_token == "page-access-token"
        return {"id": page_id, "name": "Testvereinsseite"}


class WhatsAppApiStub:
    def __init__(self):
        self.registered_pin = None

    def exchange_code(self, *, code, redirect_uri=None):
        assert code == "embedded-code"
        return MetaToken("whatsapp-access-token", 7200)

    def granted_permissions(self, access_token):
        assert access_token == "whatsapp-access-token"
        return set(WHATSAPP_REQUIRED_SCOPES)

    def whatsapp_phone(self, *, phone_number_id, access_token):
        assert phone_number_id == "222222"
        return {
            "id": phone_number_id,
            "display_phone_number": "+49 561 123456",
            "verified_name": "Testverein News",
        }

    def register_whatsapp_phone(self, *, phone_number_id, access_token, pin):
        assert phone_number_id == "222222"
        assert access_token == "whatsapp-access-token"
        self.registered_pin = pin
        return {"success": True}

    def subscribe_whatsapp_app(self, *, waba_id, access_token):
        assert waba_id == "111111"
        return {"success": True}

    def whatsapp_templates(self, *, waba_id, access_token):
        return [
            {
                "id": "template-1",
                "name": "spielankuendigung",
                "status": "APPROVED",
                "language": "de",
                "category": "UTILITY",
                "components": [
                    {"type": "BODY", "text": "{{1}}"},
                ],
            }
        ]


class WhatsAppSendApiStub:
    def __init__(self):
        self.calls = 0

    def send_whatsapp_template(self, **kwargs):
        self.calls += 1
        assert kwargs["to"] == "+49561123456"
        assert kwargs["template_name"] == "spielankuendigung"
        return {"messages": [{"id": "wamid.test-1"}]}


class WhatsAppSubscriptionApiStub:
    def __init__(self, *, subscribed: bool):
        self.subscribed = subscribed
        self.subscribe_calls = 0

    def whatsapp_subscribed_apps(self, *, waba_id, access_token):
        assert waba_id == "111111"
        assert access_token == "whatsapp-access-token"
        if not self.subscribed:
            return []
        return [{"whatsapp_business_api_data": {"id": "channels-app"}}]

    def subscribe_whatsapp_app(self, *, waba_id, access_token):
        assert waba_id == "111111"
        assert access_token == "whatsapp-access-token"
        self.subscribe_calls += 1
        self.subscribed = True
        return {"success": True}


def channel_post_fixture(db, settings: Settings):
    page = InstagramPage(
        internal_name="instagram-test",
        display_name="Instagram Test",
        username="instagram_test",
        club="Testverein",
        active=True,
        connection_status="connected",
        publishing_enabled=True,
    )
    db.add(page)
    db.flush()
    team = Team(
        internal_name="team-test",
        display_name="Testmannschaft",
        short_name="Test",
        slug="testmannschaft",
        club="Testverein",
        active=True,
        fussball_url="https://example.invalid/team",
        instagram_page_id=page.id,
        media_subdir="test/spieler",
        publishing_enabled=True,
    )
    db.add(team)
    db.flush()
    user = admin_user(db)
    post = Post(
        team_id=team.id,
        instagram_page_id=page.id,
        post_type="announcement",
        status=PostStatus.APPROVED,
        text="Am Sonntag ist Heimspiel.",
        feed_path="/tmp/feed.png",
        approved_by=user.id,
        approved_at=datetime.now(timezone.utc),
        approved_version=1,
    )
    db.add(post)
    db.flush()
    source = PublicationJob(
        post_id=post.id,
        team_id=team.id,
        instagram_page_id=page.id,
        channel_type="instagram",
        content_type="announcement",
        delivery_action="publish",
        kind="feed",
        media_path="/tmp/feed.png",
        text_snapshot=post.text,
        scheduled_at=datetime.now(timezone.utc) + timedelta(hours=1),
        approval_status="approved",
        status=JobStatus.SCHEDULED,
        idempotency_key=f"instagram:{post.id}",
        approved_post_version=1,
    )
    db.add(source)
    db.flush()
    return page, team, user, post, source


def test_facebook_oauth_hides_tokens_and_stores_page_token_encrypted(db):
    settings = channel_settings()
    user = admin_user(db)
    url = start_channel_oauth(
        db,
        settings,
        channel_type="facebook",
        user=user,
        api=FacebookApiStub(),
    )
    raw_state = parse_qs(urlparse(url).query)["state"][0]

    state, pages = prepare_facebook_selection(
        db,
        settings,
        raw_state=raw_state,
        code="oauth-code",
        api=FacebookApiStub(),
    )
    assert pages == [
        {
            "id": "123456",
            "name": "Testvereinsseite",
            "tasks": ["CREATE_CONTENT"],
            "can_publish": True,
        }
    ]
    assert "page-access-token" not in state.encrypted_selection_payload

    connection = complete_facebook_selection(
        db,
        settings,
        raw_state=raw_state,
        page_id="123456",
        api=FacebookApiStub(),
    )
    assert connection.status == "connected"
    assert connection.automatic_delivery_enabled is False
    assert (
        TokenCipher(settings.meta_token_encryption_key).decrypt(connection.encrypted_token)
        == "page-access-token"
    )
    assert "page-access-token" not in str(
        list(db.scalars(select(AuditLog).where(AuditLog.entity_id == connection.id)))[0].details
    )
    assert (
        db.scalar(
            select(SocialChannelOAuthState).where(SocialChannelOAuthState.id == state.id)
        ).encrypted_selection_payload
        is None
    )


def test_whatsapp_onboarding_starts_disabled_and_synchronizes_approved_template(db):
    settings = channel_settings()
    user = admin_user(db)
    api = WhatsAppApiStub()
    connection = complete_whatsapp_onboarding(
        db,
        settings,
        user=user,
        code="embedded-code",
        waba_id="111111",
        phone_number_id="222222",
        registration_pin="123456",
        api=api,
    )
    assert connection.status == "connected"
    assert connection.publishing_enabled is False
    assert connection.automatic_delivery_enabled is False
    assert connection.display_phone_number == "+49 561 123456"
    assert connection.settings["phone_registered"] is True
    assert api.registered_pin == "123456"
    assert "123456" not in str(connection.settings)
    assert (
        TokenCipher(settings.meta_token_encryption_key).decrypt(connection.encrypted_token)
        == "whatsapp-access-token"
    )
    template = db.scalar(
        select(WhatsAppMessageTemplate).where(
            WhatsAppMessageTemplate.channel_connection_id == connection.id
        )
    )
    assert template.status == "approved"
    assert template.message_type == "general"


def test_whatsapp_onboarding_reuses_disconnected_connection_and_preserves_related_data(db):
    settings = channel_settings()
    user = admin_user(db)
    existing = SocialChannelConnection(
        channel_type="whatsapp",
        internal_name="Testverein News",
        display_name="Testverein News",
        external_account_id="111111",
        parent_business_id="111111",
        phone_number_id="222222",
        display_phone_number="+49 561 123456",
        status="disconnected",
        active=False,
        settings={
            "phone_registered": True,
            "webhook_subscription_confirmed": True,
        },
        encrypted_token=None,
    )
    db.add(existing)
    db.flush()
    template = WhatsAppMessageTemplate(
        channel_connection_id=existing.id,
        provider_template_id="historical-template",
        name="historische_vorlage",
        message_type="announcement",
        status="approved",
    )
    recipient = WhatsAppRecipient(
        channel_connection_id=existing.id,
        normalized_phone="+49561123456",
        display_name="Historischer Empfänger",
        opt_in_status="confirmed",
        active=True,
    )
    db.add_all([template, recipient])
    db.flush()

    reconnected = complete_whatsapp_onboarding(
        db,
        settings,
        user=user,
        code="embedded-code",
        waba_id="111111",
        phone_number_id="222222",
        registration_pin="123456",
        api=WhatsAppApiStub(),
    )

    assert reconnected.id == existing.id
    assert reconnected.status == "connected"
    assert reconnected.active is True
    assert reconnected.encrypted_token
    assert db.scalar(select(WhatsAppRecipient).where(WhatsAppRecipient.id == recipient.id))
    assert db.scalar(
        select(WhatsAppMessageTemplate).where(WhatsAppMessageTemplate.id == template.id)
    )
    assert len(
        list(
            db.scalars(
                select(SocialChannelConnection).where(
                    SocialChannelConnection.channel_type == "whatsapp"
                )
            )
        )
    ) == 1


def test_whatsapp_card_exposes_reconnect_and_reregistration_states(db):
    disconnected = SocialChannelConnection(
        channel_type="whatsapp",
        internal_name="disconnected-whatsapp",
        display_name="Getrenntes WhatsApp",
        external_account_id="waba-disconnected",
        parent_business_id="waba-disconnected",
        phone_number_id="phone-disconnected",
        status="disrupted",
        settings={"phone_registered": True},
        encrypted_token=None,
    )
    connected = SocialChannelConnection(
        channel_type="whatsapp",
        internal_name="connected-whatsapp",
        display_name="Verbundenes WhatsApp",
        external_account_id="waba-connected",
        parent_business_id="waba-connected",
        phone_number_id="phone-connected",
        status="connected",
        settings={"phone_registered": True},
        encrypted_token="encrypted-token-placeholder",
    )
    db.add_all([disconnected, connected])
    db.flush()

    cards = {item["connection"].id: item for item in channel_cards(db)["whatsapp"]}

    assert cards[disconnected.id]["reconnect_required"] is True
    assert cards[disconnected.id]["has_token"] is False
    assert cards[disconnected.id]["reregistration_available"] is False
    assert cards[connected.id]["reconnect_required"] is False
    assert cards[connected.id]["has_token"] is True
    assert cards[connected.id]["reregistration_available"] is True


def test_whatsapp_onboarding_does_not_require_business_management(db):
    settings = channel_settings()
    user = admin_user(db)
    api = WhatsAppApiStub()

    assert "business_management" not in api.granted_permissions("whatsapp-access-token")
    connection = complete_whatsapp_onboarding(
        db,
        settings,
        user=user,
        code="embedded-code",
        waba_id="111111",
        phone_number_id="222222",
        registration_pin="123456",
        api=api,
    )

    assert connection.status == "connected"
    assert set(connection.scopes) == {
        "whatsapp_business_management",
        "whatsapp_business_messaging",
    }


@pytest.mark.parametrize("initially_subscribed", [True, False])
def test_whatsapp_webhook_subscription_is_checked_and_repaired(
    db,
    initially_subscribed,
):
    settings = channel_settings()
    connection = SocialChannelConnection(
        channel_type="whatsapp",
        internal_name="Testverein News",
        display_name="Testverein News",
        external_account_id="111111",
        parent_business_id="111111",
        phone_number_id="222222",
        settings={"phone_registered": True},
    )
    db.add(connection)
    db.flush()
    api = WhatsAppSubscriptionApiStub(subscribed=initially_subscribed)

    repaired = ensure_whatsapp_webhook_subscription(
        settings,
        connection=connection,
        access_token="whatsapp-access-token",
        api=api,
    )

    assert repaired is (not initially_subscribed)
    assert api.subscribe_calls == (0 if initially_subscribed else 1)
    assert connection.settings["webhook_subscription_confirmed"] is True
    assert connection.settings["webhook_subscription_checked_at"]


def test_whatsapp_onboarding_still_rejects_missing_whatsapp_permission(db):
    class MissingMessagingPermissionApi(WhatsAppApiStub):
        def granted_permissions(self, access_token):
            assert access_token == "whatsapp-access-token"
            return {"whatsapp_business_management"}

        def whatsapp_phone(self, **kwargs):  # pragma: no cover - must not be reached
            raise AssertionError("Die Asset-Prüfung darf ohne Messaging-Recht nicht starten")

    settings = channel_settings()
    user = admin_user(db)

    with pytest.raises(ChannelApiError, match="erforderliche Berechtigung"):
        complete_whatsapp_onboarding(
            db,
            settings,
            user=user,
            code="embedded-code",
            waba_id="111111",
            phone_number_id="222222",
            registration_pin="123456",
            api=MissingMessagingPermissionApi(),
        )


def test_whatsapp_onboarding_rejects_invalid_registration_pin_without_registering(db):
    settings = channel_settings()
    user = admin_user(db)
    api = WhatsAppApiStub()

    with pytest.raises(ChannelApiError, match="genau 6 Ziffern"):
        complete_whatsapp_onboarding(
            db,
            settings,
            user=user,
            code="embedded-code",
            waba_id="111111",
            phone_number_id="222222",
            registration_pin="12ab",
            api=api,
        )

    assert api.registered_pin is None


def test_existing_whatsapp_connection_can_be_registered_without_storing_pin(db):
    settings = channel_settings()
    user = admin_user(db)
    connection = SocialChannelConnection(
        channel_type="whatsapp",
        internal_name="legacy-whatsapp",
        display_name="Legacy WhatsApp",
        external_account_id="111111",
        parent_business_id="111111",
        phone_number_id="222222",
        status="connected",
        active=True,
        encrypted_token=TokenCipher(settings.meta_token_encryption_key).encrypt(
            "whatsapp-access-token"
        ),
    )
    db.add(connection)
    db.flush()
    api = WhatsAppApiStub()

    activate_existing_whatsapp_phone(
        db,
        settings,
        user=user,
        connection=connection,
        registration_pin="654321",
        api=api,
    )

    assert api.registered_pin == "654321"
    assert connection.settings["phone_registered"] is True
    assert "654321" not in str(connection.settings)
    audit = db.scalar(
        select(AuditLog).where(
            AuditLog.entity_id == connection.id,
            AuditLog.action == "channel.whatsapp.phone_registered",
        )
    )
    assert audit is not None
    assert "654321" not in str(audit.details)


def test_whatsapp_capabilities_do_not_offer_story_or_status_and_template_is_strict(db):
    assert capability_keys("whatsapp") == {"template_message"}
    assert "story" not in capability_keys("whatsapp")
    assert status_label("permission_missing") == "Berechtigung fehlt"

    template = WhatsAppMessageTemplate(
        channel_connection_id="connection",
        name="resultat",
        provider_template_id="provider-template",
        language="de",
        message_type="result",
        status="approved",
        components=[{"type": "BODY", "text": "{{1}}"}],
    )
    job = PublicationJob(text_snapshot="Endstand: 2:1")
    assert _whatsapp_components(template, job) == [
        {
            "type": "body",
            "parameters": [{"type": "text", "text": "Endstand: 2:1"}],
        }
    ]


def test_channel_models_are_tenant_scoped(db):
    item = SocialChannelConnection(
        channel_type="facebook",
        internal_name="Test",
        display_name="Test",
        external_account_id="fb-test",
    )
    db.add(item)
    db.flush()
    assert item.club_id == db.info["test_club_id"]


def test_cross_channel_jobs_require_explicit_selection_and_are_not_retroactive(db):
    settings = channel_settings()
    _page, team, _user, post, source = channel_post_fixture(db, settings)
    cipher = TokenCipher(settings.meta_token_encryption_key)
    facebook = SocialChannelConnection(
        channel_type="facebook",
        internal_name="facebook-test",
        display_name="Facebook Test",
        external_account_id="page-1",
        status="connected",
        active=True,
        publishing_enabled=True,
        automatic_delivery_enabled=True,
        encrypted_token=cipher.encrypt("facebook-token"),
    )
    whatsapp = SocialChannelConnection(
        channel_type="whatsapp",
        internal_name="whatsapp-test",
        display_name="WhatsApp Test",
        external_account_id="waba-1",
        parent_business_id="waba-1",
        phone_number_id="phone-1",
        status="connected",
        settings={"phone_registered": True},
        active=True,
        publishing_enabled=True,
        automatic_delivery_enabled=True,
        encrypted_token=cipher.encrypt("whatsapp-token"),
    )
    db.add_all([facebook, whatsapp])
    db.flush()
    for connection in (facebook, whatsapp):
        db.add(
            TeamChannelAssignment(
                team_id=team.id,
                channel_connection_id=connection.id,
                enabled=True,
                announcement_enabled=True,
                result_enabled=True,
            )
        )
    db.flush()

    created = ensure_approved_channel_jobs(
        db,
        post,
        [source],
        {facebook.id},
    )
    assert [item.channel_type for item in created] == ["facebook"]
    assert (
        db.scalar(
            select(PublicationJob).where(
                PublicationJob.post_id == post.id,
                PublicationJob.channel_connection_id == whatsapp.id,
            )
        )
        is None
    )

    # A connection created or selected later must not silently activate the old approval.
    created_again = ensure_approved_channel_jobs(db, post, [source], set())
    assert created_again == []
    facebook_job = db.scalar(
        select(PublicationJob).where(
            PublicationJob.post_id == post.id,
            PublicationJob.channel_connection_id == facebook.id,
        )
    )
    assert facebook_job.status == JobStatus.CANCELLED
    assert facebook_job.approval_status == "rejected"


def test_whatsapp_delivery_is_idempotent_and_never_sends_twice(db):
    settings = channel_settings()
    _page, team, user, post, source = channel_post_fixture(db, settings)
    connection = SocialChannelConnection(
        channel_type="whatsapp",
        internal_name="whatsapp-send",
        display_name="WhatsApp Versand",
        external_account_id="waba-send",
        parent_business_id="waba-send",
        phone_number_id="phone-send",
        status="connected",
        settings={"phone_registered": True},
        active=True,
        publishing_enabled=True,
        automatic_delivery_enabled=True,
        encrypted_token=TokenCipher(settings.meta_token_encryption_key).encrypt("whatsapp-token"),
    )
    db.add(connection)
    db.flush()
    recipient = WhatsAppRecipient(
        channel_connection_id=connection.id,
        normalized_phone="+49561123456",
        display_name="Testempfaenger",
        opt_in_status="confirmed",
        opt_in_at=datetime.now(timezone.utc),
        opt_in_source="Testeinwilligung",
        active=True,
        preferred_message_types=["announcement"],
    )
    template = WhatsAppMessageTemplate(
        channel_connection_id=connection.id,
        name="spielankuendigung",
        provider_template_id="template-send",
        language="de",
        message_type="announcement",
        status="approved",
        components=[{"type": "BODY", "text": "{{1}}"}],
    )
    job = PublicationJob(
        post_id=post.id,
        team_id=team.id,
        instagram_page_id=None,
        channel_type="whatsapp",
        channel_connection_id=connection.id,
        content_type="announcement",
        target="audience",
        delivery_action="send",
        kind="message",
        media_path=source.media_path,
        text_snapshot="Am Sonntag ist Heimspiel.",
        scheduled_at=datetime.now(timezone.utc),
        approval_status="approved",
        status=JobStatus.SCHEDULED,
        idempotency_key="whatsapp:delivery-test",
        approved_post_version=1,
    )
    db.add_all([recipient, template, job])
    db.flush()
    api = WhatsAppSendApiStub()

    assert (
        _deliver_one(
            db,
            settings,
            job=job,
            connection=connection,
            user=user,
            recipient=recipient,
            template=template,
            api=api,
        )
        is True
    )
    assert (
        _deliver_one(
            db,
            settings,
            job=job,
            connection=connection,
            user=user,
            recipient=recipient,
            template=template,
            api=api,
        )
        is False
    )
    assert api.calls == 1
    attempts = list(
        db.scalars(
            select(ChannelDeliveryAttempt).where(
                ChannelDeliveryAttempt.publication_job_id == job.id
            )
        )
    )
    assert len(attempts) == 1
    assert attempts[0].status == "sent"
    assert attempts[0].platform_id == "wamid.test-1"


def test_whatsapp_webhook_signature_and_opt_out_are_enforced(db, monkeypatch):
    import app.channels.webhooks as webhook_module

    settings = channel_settings()
    monkeypatch.setattr(webhook_module, "settings", settings)
    body = b'{"object":"whatsapp_business_account"}'
    signature = (
        "sha256="
        + hmac.new(
            settings.meta_facebook_app_secret.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
    )
    _verify_signature(body, signature)
    with pytest.raises(Exception) as error:
        _verify_signature(body, "sha256=invalid")
    assert getattr(error.value, "status_code", None) == 403

    connection = SocialChannelConnection(
        channel_type="whatsapp",
        internal_name="whatsapp-webhook",
        display_name="WhatsApp Webhook",
        external_account_id="waba-webhook",
        parent_business_id="waba-webhook",
        phone_number_id="phone-webhook",
        status="connected",
        active=True,
    )
    db.add(connection)
    db.flush()
    club_id, connection_id = _resolve_whatsapp_connection(
        db,
        waba_id="waba-webhook",
        phone_id="phone-webhook",
    )
    assert (club_id, connection_id) == (connection.club_id, connection.id)
    with pytest.raises(Exception) as unknown_channel:
        _resolve_whatsapp_connection(
            db,
            waba_id="waba-unbekannt",
            phone_id="phone-unbekannt",
        )
    assert getattr(unknown_channel.value, "status_code", None) == 404
    recipient = WhatsAppRecipient(
        channel_connection_id=connection.id,
        normalized_phone="+49561123456",
        opt_in_status="confirmed",
        opt_in_at=datetime.now(timezone.utc),
        opt_in_source="Onlineformular",
        active=True,
        preferred_message_types=["announcement", "result"],
    )
    db.add(recipient)
    db.flush()
    payload = {
        "entry": [
            {
                "id": "waba-webhook",
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "phone-webhook"},
                            "messages": [
                                {
                                    "id": "wamid.stop",
                                    "from": "49561123456",
                                    "text": {"body": "STOP"},
                                }
                            ],
                        }
                    }
                ],
            }
        ]
    }
    _process_whatsapp_payload(db, payload, connection.id)
    assert recipient.active is False
    assert recipient.opt_in_status == "revoked"
    assert recipient.opt_out_at is not None
    audit = db.scalar(
        select(AuditLog).where(
            AuditLog.entity_id == recipient.id,
            AuditLog.action == "channel.whatsapp.recipient_opted_out_by_message",
        )
    )
    assert audit is not None


def test_whatsapp_webhook_rejects_mixed_tenant_identifiers_before_content():
    payload = {
        "entry": [
            {
                "id": "waba-club-a",
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "phone-club-a"},
                            "messages": [{"id": "message-a", "text": {"body": "Tor"}}],
                        }
                    }
                ],
            },
            {
                "id": "waba-club-b",
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "phone-club-b"},
                            "messages": [{"id": "message-b", "text": {"body": "Tor"}}],
                        }
                    }
                ],
            },
        ]
    }

    with pytest.raises(Exception) as error:
        _whatsapp_identifiers(payload)

    assert getattr(error.value, "status_code", None) == 409
