from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.channels.api import (
    FACEBOOK_REQUIRED_SCOPES,
    WHATSAPP_REQUIRED_SCOPES,
    MetaToken,
)
from app.channels.capabilities import capability_keys, status_label
from app.channels.delivery import _deliver_one, _whatsapp_components
from app.channels.jobs import ensure_approved_channel_jobs
from app.channels.oauth import (
    complete_facebook_selection,
    complete_whatsapp_onboarding,
    prepare_facebook_selection,
    start_channel_oauth,
)
from app.channels.webhooks import _process_whatsapp_payload, _verify_signature
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
        meta_app_id="app-1",
        meta_app_secret="app-secret",
        meta_facebook_oauth_redirect_uri=(
            "https://meta.example.invalid/public/meta/channels/oauth/callback"
        ),
        meta_token_encryption_key=Fernet.generate_key().decode("ascii"),
    )


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
    connection = complete_whatsapp_onboarding(
        db,
        settings,
        user=user,
        code="embedded-code",
        waba_id="111111",
        phone_number_id="222222",
        api=WhatsAppApiStub(),
    )
    assert connection.status == "connected"
    assert connection.publishing_enabled is False
    assert connection.automatic_delivery_enabled is False
    assert connection.display_phone_number == "+49 561 123456"
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
        + hmac.new(settings.meta_app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
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
