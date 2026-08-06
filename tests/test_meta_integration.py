from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.fernet import Fernet
from PIL import Image
from sqlalchemy import select

from app.config import Settings
from app.meta.api import REQUIRED_SCOPES, MetaApiClient, MetaApiError, OAuthToken
from app.meta.connection_health import run_automatic_connection_check_cycle
from app.meta.media import (
    MediaGrantError,
    create_grant,
    resolve_grant,
    verify_public_media_url,
)
from app.meta.oauth import (
    check_connection,
    complete_oauth,
    consume_oauth_state,
    start_oauth,
)
from app.meta.publishing import (
    MetaPublishingError,
    create_attempt,
    create_container,
    issue_confirmation,
    publish,
    reconcile_attempt,
    refresh_container_status,
)
from app.meta.scheduler import run_automatic_publishing_cycle
from app.meta.security import TokenCipher, sanitize_platform_data, secret_hash
from app.models import (
    AuditLog,
    Game,
    InstagramConnection,
    InstagramOAuthState,
    InstagramPage,
    JobStatus,
    MetaCarouselItem,
    MetaPublishingAttempt,
    Post,
    PostStatus,
    PublicationJob,
    PublicationMediaItem,
    Role,
    SystemSetting,
    Team,
    User,
)


def meta_settings(tmp_path):
    key = Fernet.generate_key().decode()
    generated = tmp_path / "generated"
    generated.mkdir()
    return Settings(
        environment="meta-test",
        publisher_mode="instagram",
        meta_test_enabled=True,
        meta_test_publish_enabled=True,
        meta_scheduler_enabled=False,
        global_publish_enabled=False,
        meta_app_id="app-id",
        meta_app_secret="app-secret",
        meta_token_encryption_key=key,
        meta_oauth_redirect_uri=(
            "https://meta-test.example.org/public/instagram/oauth/callback"
        ),
        meta_public_base_url="https://meta-test.example.org",
        generated_root=generated,
    )


def make_png(path, size=(1080, 1350)):
    Image.new("RGB", size, (10, 30, 80)).save(path, "PNG")


def public_media_client(settings):
    def handler(_request):
        payload = (settings.generated_root / "feed.png").read_bytes()
        return httpx.Response(
            200,
            content=payload,
            headers={"content-type": "image/png"},
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def make_context(db, settings):
    user = User(
        email="meta-admin@example.invalid",
        password_hash="unused",
        role=Role.ADMIN,
        all_teams=True,
    )
    page = InstagramPage(
        internal_name="svehlen1901",
        display_name="SV Ehlen",
        username="svehlen1901",
        club="SV Ehlen",
        active=True,
        publishing_enabled=True,
        connection_status="connected",
    )
    db.add_all([user, page])
    db.flush()
    team = Team(
        internal_name="erste",
        display_name="SV Ehlen",
        short_name="SVE",
        slug="meta-test-team",
        club="SV Ehlen",
        fussball_url="https://www.fussball.de/team",
        instagram_page_id=page.id,
        media_subdir="erste_mannschaft/spieler",
        publishing_enabled=True,
    )
    connection = InstagramConnection(
        instagram_page_id=page.id,
        instagram_user_id="17841400000000000",
        confirmed_username="svehlen1901",
        account_type="BUSINESS",
        login_variant="instagram_login",
        api_version=settings.meta_graph_version,
        scopes=sorted(REQUIRED_SCOPES),
        status="connected",
        encrypted_token=TokenCipher(settings.meta_token_encryption_key).encrypt(
            "very-secret-instagram-token"
        ),
        token_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        token_key_version="v1",
        test_account=True,
        last_check_at=datetime.now(timezone.utc),
    )
    db.add_all([team, connection])
    db.flush()
    game = Game(
        team_id=team.id,
        external_id="meta-fixture",
        home_team="SV Ehlen",
        away_team="Testgegner",
        kickoff=datetime.now(timezone.utc) + timedelta(days=3),
        competition="Kreisliga A",
        venue="Ehlen",
        status="scheduled",
        source_url="fixture://meta",
        overrides={},
    )
    db.add(game)
    db.flush()
    post = Post(
        game_id=game.id,
        team_id=team.id,
        instagram_page_id=page.id,
        post_type="announcement",
        status=PostStatus.APPROVED,
        text="SV Ehlen gegen Testgegner.",
        approved_version=1,
        publishing_enabled=True,
    )
    db.add(post)
    db.flush()
    path = settings.generated_root / "feed.png"
    make_png(path)
    job = PublicationJob(
        post_id=post.id,
        game_id=game.id,
        team_id=team.id,
        instagram_page_id=page.id,
        kind="feed",
        media_path=str(path),
        text_snapshot=post.text,
        scheduled_at=datetime.now(timezone.utc),
        approval_status="approved",
        status=JobStatus.APPROVED,
        idempotency_key="meta-test-feed-v1",
        approved_post_version=1,
    )
    db.add(job)
    db.commit()
    return user, page, connection, post, job


def test_token_cipher_and_sanitizer_never_expose_secrets(tmp_path):
    settings = meta_settings(tmp_path)
    cipher = TokenCipher(settings.meta_token_encryption_key)
    encrypted = cipher.encrypt("top-secret-token")
    assert encrypted != "top-secret-token"
    assert "top-secret-token" not in encrypted
    assert cipher.decrypt(encrypted) == "top-secret-token"
    cleaned = sanitize_platform_data(
        {
            "access_token": "secret",
            "nested": {"client_secret": "also-secret", "id": "safe"},
        }
    )
    assert cleaned == {
        "access_token": "[entfernt]",
        "nested": {"client_secret": "[entfernt]", "id": "safe"},
    }


class OAuthApi:
    def __init__(self, username="svehlen1901"):
        self.username = username
        self.profile_calls = 0

    def authorization_url(self, state, redirect_uri):
        return f"https://www.instagram.com/oauth/authorize?state={state}"

    def exchange_code(self, code, redirect_uri):
        return OAuthToken("short-secret", "ig-user", 3600)

    def exchange_long_lived(self, token):
        return OAuthToken("long-secret", token.user_id, 60 * 24 * 3600)

    def profile(self, access_token):
        self.profile_calls += 1
        return {
            "user_id": "ig-user",
            "username": self.username,
            "account_type": "BUSINESS",
        }


def test_oauth_state_is_one_time_and_token_is_encrypted(db, tmp_path):
    settings = meta_settings(tmp_path)
    user = User(
        email="oauth@example.invalid",
        password_hash="unused",
        role=Role.ADMIN,
        all_teams=True,
    )
    page = InstagramPage(
        internal_name="oauth-page",
        display_name="SV Ehlen",
        username="svehlen1901",
        club="SV Ehlen",
    )
    db.add_all([user, page])
    db.commit()
    url = start_oauth(db, settings, page, user, OAuthApi())
    state = parse_qs(url.split("?", 1)[1])["state"][0]
    api = OAuthApi()
    connection = complete_oauth(
        db, settings, state=state, code="single-use-code", api=api
    )
    assert connection.status == "connected"
    assert connection.account_type == "BUSINESS"
    assert set(connection.scopes) == REQUIRED_SCOPES
    assert api.profile_calls == 1
    assert connection.encrypted_token != "long-secret"
    assert "long-secret" not in connection.encrypted_token
    assert TokenCipher(settings.meta_token_encryption_key).decrypt(
        connection.encrypted_token
    ) == "long-secret"
    with pytest.raises(MetaApiError, match="bereits verwendet"):
        consume_oauth_state(db, state)


def test_connection_check_uses_stored_oauth_grant_without_permissions_edge(
    db, tmp_path
):
    settings = meta_settings(tmp_path)
    user, _, connection, _, _ = make_context(db, settings)
    api = OAuthApi()

    checked = check_connection(db, settings, connection, user, api)

    assert checked.status == "connected"
    assert set(checked.scopes) == REQUIRED_SCOPES
    assert api.profile_calls == 1


def test_connection_check_rejects_incomplete_stored_oauth_grant(db, tmp_path):
    settings = meta_settings(tmp_path)
    user, _, connection, _, _ = make_context(db, settings)
    connection.scopes = ["instagram_business_basic"]
    db.commit()

    checked = check_connection(db, settings, connection, user, OAuthApi())

    assert checked.status == "invalid"
    assert "Berechtigungen" in checked.last_error


def test_automatic_connection_check_runs_twice_daily_and_is_audited(db, tmp_path):
    settings = production_settings(tmp_path)
    settings.meta_connection_check_interval_seconds = 12 * 60 * 60
    _, page, connection, _, _ = make_context(db, settings)
    now = datetime.now(timezone.utc)
    connection.last_check_at = now - timedelta(hours=13)
    page.last_check_at = now - timedelta(hours=13)
    db.commit()
    api = OAuthApi()

    first = run_automatic_connection_check_cycle(db, settings, api=api, now=now)
    second = run_automatic_connection_check_cycle(
        db,
        settings,
        api=api,
        now=now + timedelta(hours=11, minutes=59),
    )
    third = run_automatic_connection_check_cycle(
        db,
        settings,
        api=api,
        now=now + timedelta(hours=12, minutes=1),
    )

    db.refresh(connection)
    db.refresh(page)
    assert first.claimed == first.checked == first.succeeded == 1
    assert first.failed == 0
    assert second.claimed == second.checked == 0
    assert third.claimed == third.checked == third.succeeded == 1
    assert api.profile_calls == 2
    assert connection.status == "connected"
    assert page.connection_status == "connected"
    audit_items = list(
        db.scalars(
            select(AuditLog).where(
                AuditLog.action == "meta.connection_checked_automatic"
            )
        )
    )
    assert len(audit_items) == 2
    assert all(item.user_id is None for item in audit_items)


def test_automatic_connection_check_failure_stays_blocking_and_is_recorded(db, tmp_path):
    class FailingProfileApi:
        def profile(self, _access_token):
            raise MetaApiError("Profilprüfung vorübergehend fehlgeschlagen")

    settings = production_settings(tmp_path)
    _, page, connection, _, _ = make_context(db, settings)
    now = datetime.now(timezone.utc)
    connection.last_check_at = now - timedelta(hours=13)
    page.last_check_at = now - timedelta(hours=13)
    db.commit()

    result = run_automatic_connection_check_cycle(
        db,
        settings,
        api=FailingProfileApi(),
        now=now,
    )

    db.refresh(connection)
    db.refresh(page)
    assert result.checked == result.failed == 1
    assert result.succeeded == 0
    assert connection.status == "error"
    assert page.connection_status == "error"
    assert "Profilprüfung" in connection.last_error
    audit_item = db.scalar(
        select(AuditLog).where(
            AuditLog.action == "meta.connection_check_failed_automatic"
        )
    )
    assert audit_item is not None
    assert audit_item.user_id is None


def test_oauth_rejects_wrong_instagram_page(db, tmp_path):
    settings = meta_settings(tmp_path)
    user = User(
        email="wrong@example.invalid",
        password_hash="unused",
        role=Role.ADMIN,
        all_teams=True,
    )
    page = InstagramPage(
        internal_name="wrong-page",
        display_name="SV Ehlen",
        username="svehlen1901",
        club="SV Ehlen",
    )
    db.add_all([user, page])
    db.commit()
    url = start_oauth(db, settings, page, user, OAuthApi())
    state = parse_qs(url.split("?", 1)[1])["state"][0]
    with pytest.raises(MetaApiError, match="Falsches Instagram-Konto"):
        complete_oauth(
            db,
            settings,
            state=state,
            code="code",
            api=OAuthApi(username="anderes_konto"),
        )


def test_meta_api_uses_instagram_host_and_story_has_no_caption(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"id": "container-1"})

    api = MetaApiClient(
        meta_settings(tmp_path),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    api.create_container(
        access_token="token",
        account_id="ig-user",
        kind="story",
        image_url="https://meta.example/story.png",
        caption="must-not-be-sent",
    )
    request = calls[0]
    assert request.url.host == "graph.instagram.com"
    payload = parse_qs(request.content.decode())
    assert payload["media_type"] == ["STORIES"]
    assert "caption" not in payload
    assert request.headers["Authorization"] == "Bearer token"


def test_meta_api_sends_positioned_user_tags_only_for_feed_media(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"id": "container-1"})

    api = MetaApiClient(
        meta_settings(tmp_path),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    api.create_container(
        access_token="token",
        account_id="ig-user",
        kind="feed",
        image_url="https://meta.example/feed.png",
        caption="Mit markiertem Spieler",
        user_tags=[{"username": "spieler.eins", "x": 0.125, "y": 0.875}],
    )
    payload = parse_qs(calls[0].content.decode())
    assert payload["user_tags"] == [
        '[{"username":"spieler.eins","x":0.125,"y":0.875}]'
    ]
    with pytest.raises(MetaApiError, match="Storys"):
        api.create_container(
            access_token="token",
            account_id="ig-user",
            kind="story",
            image_url="https://meta.example/story.png",
            caption=None,
            user_tags=[{"username": "spieler.eins", "x": 0.5, "y": 0.5}],
        )


def test_meta_api_builds_carousel_child_and_ordered_parent_payloads(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"id": f"container-{len(calls)}"})

    api = MetaApiClient(
        meta_settings(tmp_path),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    api.create_carousel_item(
        access_token="token",
        account_id="ig-user",
        image_url="https://meta.example/item-1.png",
        user_tags=[{"username": "spieler.eins", "x": 0.25, "y": 0.75}],
    )
    api.create_carousel_container(
        access_token="token",
        account_id="ig-user",
        child_ids=["child-1", "child-2"],
        caption="Gemeinsamer Text",
    )
    child_payload = parse_qs(calls[0].content.decode())
    parent_payload = parse_qs(calls[1].content.decode())
    assert child_payload == {
        "image_url": ["https://meta.example/item-1.png"],
        "is_carousel_item": ["true"],
        "user_tags": ['[{"username":"spieler.eins","x":0.25,"y":0.75}]'],
    }
    assert parent_payload == {
        "media_type": ["CAROUSEL"],
        "children": ["child-1,child-2"],
        "caption": ["Gemeinsamer Text"],
    }


def test_hashed_media_grant_allows_multiple_fetches(db, tmp_path):
    settings = meta_settings(tmp_path)
    user, _, _, _, job = make_context(db, settings)
    grant, raw_token, url = create_grant(db, settings, job, user)
    db.commit()
    assert raw_token not in grant.token_hash
    assert raw_token not in str(grant.__dict__)
    assert url.startswith("https://")
    resolve_grant(db, settings, raw_token)
    resolve_grant(db, settings, raw_token)
    db.refresh(grant)
    assert grant.fetch_count == 2


def test_public_media_verification_rejects_changed_payload(db, tmp_path):
    settings = meta_settings(tmp_path)
    user, _, _, _, job = make_context(db, settings)
    grant, _, url = create_grant(db, settings, job, user)

    def handler(_request):
        return httpx.Response(
            200,
            content=b"not-the-frozen-png",
            headers={"content-type": "image/png"},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(MediaGrantError, match="Dateigröße"):
        verify_public_media_url(settings, grant, url, client)


class PublishingApi:
    def __init__(self):
        self.container_calls = 0
        self.publish_calls = 0

    def create_container(self, **kwargs):
        self.container_calls += 1
        assert kwargs["image_url"].startswith("https://")
        assert kwargs["caption"] == "SV Ehlen gegen Testgegner."
        return {"id": "container-123"}

    def container_status(self, **kwargs):
        return {"status_code": "FINISHED", "status": "ready"}

    def publish_container(self, **kwargs):
        self.publish_calls += 1
        return {"id": "media-456"}

    def media_details(self, **kwargs):
        return {
            "id": "media-456",
            "permalink": "https://www.instagram.com/p/test/",
        }


class CarouselPublishingApi(PublishingApi):
    def __init__(self):
        super().__init__()
        self.child_urls = []
        self.child_tags = []
        self.parent_children = []

    def create_carousel_item(self, **kwargs):
        self.child_urls.append(kwargs["image_url"])
        self.child_tags.append(kwargs.get("user_tags"))
        return {"id": f"child-{len(self.child_urls)}"}

    def create_carousel_container(self, **kwargs):
        self.container_calls += 1
        self.parent_children = list(kwargs["child_ids"])
        assert kwargs["caption"] == "SV Ehlen gegen Testgegner."
        return {"id": "carousel-parent-123"}


def test_manual_container_and_publish_flow_is_idempotent(db, tmp_path):
    settings = meta_settings(tmp_path)
    user, _, _, post, job = make_context(db, settings)
    attempt, raw = create_attempt(
        db,
        settings,
        publication_job_id=job.id,
        stage="publish",
        user=user,
        media_http_client=public_media_client(settings),
    )
    assert raw
    assert "media_url" not in attempt.sanitized_response
    api = PublishingApi()
    code = issue_confirmation(db, settings, attempt, user, "create_container")
    create_container(
        db,
        settings,
        attempt_id=attempt.id,
        user=user,
        confirmation_code=code,
        api=api,
        media_http_client=public_media_client(settings),
    )
    assert api.container_calls == 1
    create_container(
        db,
        settings,
        attempt_id=attempt.id,
        user=user,
        confirmation_code="unused-after-id-exists",
        api=api,
        media_http_client=public_media_client(settings),
    )
    assert api.container_calls == 1
    refresh_container_status(
        db, settings, attempt_id=attempt.id, user=user, api=api
    )
    publish_code = issue_confirmation(db, settings, attempt, user, "publish")
    publish(
        db,
        settings,
        attempt_id=attempt.id,
        user=user,
        confirmation_code=publish_code,
        api=api,
    )
    assert api.publish_calls == 1
    publish(
        db,
        settings,
        attempt_id=attempt.id,
        user=user,
        confirmation_code="unused-after-media-id-exists",
        api=api,
    )
    assert api.publish_calls == 1
    db.refresh(job)
    db.refresh(post)
    assert job.status == JobStatus.PUBLISHED
    assert job.platform_id == "media-456"
    assert post.status == PostStatus.PUBLISHED


class UncertainContainerApi(PublishingApi):
    def create_container(self, **kwargs):
        self.container_calls += 1
        raise MetaApiError("Timeout nach möglicher Annahme", uncertain=True)


class UnavailableStatusApi(PublishingApi):
    def container_status(self, **kwargs):
        raise MetaApiError("Containerstatus vorübergehend nicht erreichbar")


def test_uncertain_container_is_never_repeated_automatically(db, tmp_path):
    settings = meta_settings(tmp_path)
    user, _, _, _, job = make_context(db, settings)
    attempt, _ = create_attempt(
        db,
        settings,
        publication_job_id=job.id,
        stage="publish",
        user=user,
        media_http_client=public_media_client(settings),
    )
    api = UncertainContainerApi()
    code = issue_confirmation(db, settings, attempt, user, "create_container")
    with pytest.raises(MetaPublishingError, match="Timeout"):
        create_container(
            db,
            settings,
            attempt_id=attempt.id,
            user=user,
            confirmation_code=code,
            api=api,
            media_http_client=public_media_client(settings),
        )
    db.refresh(attempt)
    assert attempt.phase == "uncertain"
    with pytest.raises(MetaPublishingError, match="manueller Abgleich"):
        create_container(
            db,
            settings,
            attempt_id=attempt.id,
            user=user,
            confirmation_code="ignored",
            api=api,
            media_http_client=public_media_client(settings),
        )
    assert api.container_calls == 1


def test_in_progress_container_call_is_not_repeated(db, tmp_path):
    settings = meta_settings(tmp_path)
    user, _, _, _, job = make_context(db, settings)
    attempt, _ = create_attempt(
        db,
        settings,
        publication_job_id=job.id,
        stage="publish",
        user=user,
        media_http_client=public_media_client(settings),
    )
    attempt.phase = "creating_container"
    db.commit()
    api = PublishingApi()
    with pytest.raises(MetaPublishingError, match="läuft bereits"):
        create_container(
            db,
            settings,
            attempt_id=attempt.id,
            user=user,
            confirmation_code="unused",
            api=api,
            media_http_client=public_media_client(settings),
        )
    assert api.container_calls == 0


def test_interrupted_container_can_only_be_reconciled_after_safety_period(
    db, tmp_path
):
    settings = meta_settings(tmp_path)
    user, _, _, _, job = make_context(db, settings)
    attempt, _ = create_attempt(
        db,
        settings,
        publication_job_id=job.id,
        stage="publish",
        user=user,
        media_http_client=public_media_client(settings),
    )
    attempt.phase = "creating_container"
    db.commit()

    with pytest.raises(MetaPublishingError, match="Sicherheitsfrist"):
        reconcile_attempt(
            db,
            settings,
            attempt_id=attempt.id,
            user=user,
            resolution="not_published",
            note="Noch nicht sicher beendet.",
        )

    attempt.updated_at = datetime.now(timezone.utc) - timedelta(minutes=3)
    db.commit()
    reconcile_attempt(
        db,
        settings,
        attempt_id=attempt.id,
        user=user,
        resolution="not_published",
        note="Nach Neustart im Testkonto geprüft.",
    )
    db.refresh(attempt)
    db.refresh(job)
    assert attempt.phase == "failed"
    assert attempt.active_key is None
    assert job.status == JobStatus.FAILED


class PermalinkFailureApi(PublishingApi):
    def media_details(self, **kwargs):
        raise MetaApiError("Permalink-Abfrage vorübergehend nicht erreichbar")


def test_permalink_failure_does_not_reclassify_confirmed_publish(db, tmp_path):
    settings = meta_settings(tmp_path)
    user, _, _, post, job = make_context(db, settings)
    attempt, _ = create_attempt(
        db,
        settings,
        publication_job_id=job.id,
        stage="publish",
        user=user,
        media_http_client=public_media_client(settings),
    )
    api = PermalinkFailureApi()
    container_code = issue_confirmation(
        db, settings, attempt, user, "create_container"
    )
    create_container(
        db,
        settings,
        attempt_id=attempt.id,
        user=user,
        confirmation_code=container_code,
        api=api,
        media_http_client=public_media_client(settings),
    )
    refresh_container_status(
        db, settings, attempt_id=attempt.id, user=user, api=api
    )
    publish_code = issue_confirmation(db, settings, attempt, user, "publish")
    publish(
        db,
        settings,
        attempt_id=attempt.id,
        user=user,
        confirmation_code=publish_code,
        api=api,
    )
    db.refresh(attempt)
    db.refresh(job)
    db.refresh(post)
    assert attempt.phase == "completed"
    assert attempt.meta_media_id == "media-456"
    assert attempt.error_category == "permalink_lookup"
    assert job.status == JobStatus.PUBLISHED
    assert post.status == PostStatus.PUBLISHED


def test_transport_error_does_not_expose_token_or_app_secret(tmp_path):
    settings = meta_settings(tmp_path)

    def handler(request):
        raise httpx.ConnectError(
            "failed request with hidden transport details",
            request=request,
        )

    api = MetaApiClient(
        settings,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(MetaApiError) as captured:
        api.refresh_token("sensitive-access-token", "ig-user")
    message = str(captured.value)
    assert "sensitive-access-token" not in message
    assert settings.meta_app_secret not in message


def test_expired_oauth_state_is_rejected(db, tmp_path):
    record = InstagramOAuthState(
        state_hash=secret_hash("expired-state"),
        instagram_page_id="page",
        user_id="user",
        redirect_uri="https://example.invalid/callback",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    db.add(record)
    db.commit()
    assert db.scalar(select(InstagramOAuthState))
    with pytest.raises(MetaApiError):
        consume_oauth_state(db, "expired-state")


def production_settings(tmp_path):
    generated = tmp_path / "production-generated"
    generated.mkdir()
    return Settings(
        environment="production",
        publisher_mode="instagram",
        meta_production_enabled=True,
        global_publish_enabled=True,
        meta_scheduler_enabled=True,
        meta_automatic_publish_enabled=True,
        meta_test_enabled=False,
        meta_token_encryption_key=Fernet.generate_key().decode(),
        meta_public_base_url="https://meta.example.org",
        generated_root=generated,
        meta_container_poll_interval_seconds=0,
        meta_container_max_wait_seconds=60,
    )


def automatic_context(db, settings):
    user, page, connection, post, job = make_context(db, settings)
    page.automatic_publishing_enabled = True
    page.automatic_publishing_confirmed_by = user.id
    page.automatic_publishing_confirmed_at = datetime.now(timezone.utc)
    page.allowed_types = {"feed": True, "story": True}
    connection.test_account = False
    connection.last_check_at = datetime.now(timezone.utc)
    post.approved_by = user.id
    job.status = JobStatus.SCHEDULED
    job.scheduled_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.add(SystemSetting(key="emergency_stop", value={"enabled": False}))
    db.commit()
    return user, page, connection, post, job


def test_automatic_scheduler_publishes_once_without_confirmation(db, tmp_path):
    settings = production_settings(tmp_path)
    _, _, _, post, job = automatic_context(db, settings)
    api = PublishingApi()
    media_client = public_media_client(settings)

    first = run_automatic_publishing_cycle(
        db, settings, api=api, media_http_client=media_client
    )
    second = run_automatic_publishing_cycle(
        db, settings, api=api, media_http_client=media_client
    )
    third = run_automatic_publishing_cycle(
        db, settings, api=api, media_http_client=media_client
    )
    fourth = run_automatic_publishing_cycle(
        db, settings, api=api, media_http_client=media_client
    )

    db.refresh(job)
    db.refresh(post)
    assert first.queued == 1
    assert first.containers_created == 1
    assert second.statuses_checked == 1
    assert third.published == 1
    assert fourth.published == 0
    assert api.container_calls == 1
    assert api.publish_calls == 1
    assert job.status == JobStatus.PUBLISHED
    assert post.status == PostStatus.PUBLISHED


def test_carousel_creates_ordered_children_then_one_parent_and_publishes(db, tmp_path):
    settings = meta_settings(tmp_path)
    user, _, _, post, job = make_context(db, settings)
    job.kind = "carousel"
    job.idempotency_key = "meta-test-carousel-v1"
    post.design_snapshot = {
        "source": "manual_upload",
        "manual_upload": {
            "images": [
                {
                    "position": 1,
                    "user_tags": [
                        {"username": "spieler.eins", "x": 0.2, "y": 0.3}
                    ],
                },
                {"position": 2, "user_tags": []},
                {
                    "position": 3,
                    "user_tags": [
                        {"username": "spieler.drei", "x": 0.7, "y": 0.8}
                    ],
                },
            ]
        },
    }
    source = settings.generated_root / "feed.png"
    for position in range(1, 4):
        target = settings.generated_root / f"carousel-{position}.png"
        target.write_bytes(source.read_bytes())
        db.add(
            PublicationMediaItem(
                publication_job_id=job.id,
                position=position,
                media_path=str(target),
                checksum="will-be-validated-from-file",
                mime_type="image/png",
                file_size=target.stat().st_size,
                width=1080,
                height=1350,
            )
        )
    db.commit()

    attempt, _ = create_attempt(
        db,
        settings,
        publication_job_id=job.id,
        stage="publish",
        user=user,
        media_http_client=public_media_client(settings),
    )
    api = CarouselPublishingApi()
    code = issue_confirmation(db, settings, attempt, user, "create_container")
    create_container(
        db,
        settings,
        attempt_id=attempt.id,
        user=user,
        confirmation_code=code,
        api=api,
        media_http_client=public_media_client(settings),
    )
    assert len(api.child_urls) == 3
    assert api.child_tags == [
        [{"username": "spieler.eins", "x": 0.2, "y": 0.3}],
        [],
        [{"username": "spieler.drei", "x": 0.7, "y": 0.8}],
    ]
    assert api.parent_children == ["child-1", "child-2", "child-3"]
    assert api.container_calls == 1
    children = list(
        db.scalars(
            select(MetaCarouselItem)
            .where(MetaCarouselItem.attempt_id == attempt.id)
            .order_by(MetaCarouselItem.position)
        )
    )
    assert [child.meta_container_id for child in children] == api.parent_children

    refresh_container_status(db, settings, attempt_id=attempt.id, user=user, api=api)
    publish_code = issue_confirmation(db, settings, attempt, user, "publish")
    publish(
        db,
        settings,
        attempt_id=attempt.id,
        user=user,
        confirmation_code=publish_code,
        api=api,
    )
    assert api.publish_calls == 1
    db.refresh(job)
    db.refresh(post)
    assert job.status == JobStatus.PUBLISHED
    assert post.status == PostStatus.PUBLISHED


def test_automatic_scheduler_ignores_not_due_or_disabled_page(db, tmp_path):
    settings = production_settings(tmp_path)
    _, page, _, _, job = automatic_context(db, settings)
    api = PublishingApi()
    job.scheduled_at = datetime.now(timezone.utc) + timedelta(hours=1)
    db.commit()

    result = run_automatic_publishing_cycle(
        db, settings, api=api, media_http_client=public_media_client(settings)
    )
    assert result.queued == 0
    assert api.container_calls == 0

    job.scheduled_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    page.automatic_publishing_enabled = False
    db.commit()
    result = run_automatic_publishing_cycle(
        db, settings, api=api, media_http_client=public_media_client(settings)
    )
    assert result.queued == 0
    assert api.container_calls == 0


def test_automatic_scheduler_never_repeats_uncertain_container(db, tmp_path):
    settings = production_settings(tmp_path)
    _, _, _, _, job = automatic_context(db, settings)
    api = UncertainContainerApi()
    media_client = public_media_client(settings)

    first = run_automatic_publishing_cycle(
        db, settings, api=api, media_http_client=media_client
    )
    second = run_automatic_publishing_cycle(
        db, settings, api=api, media_http_client=media_client
    )

    db.refresh(job)
    assert first.queued == 1
    assert api.container_calls == 1
    assert second.containers_created == 0
    assert api.container_calls == 1
    assert job.status == JobStatus.UNCERTAIN


def test_automatic_scheduler_releases_timed_out_container_attempt(db, tmp_path):
    settings = production_settings(tmp_path)
    settings.meta_container_max_wait_seconds = 0
    _, _, _, _, job = automatic_context(db, settings)
    api = UnavailableStatusApi()
    media_client = public_media_client(settings)

    run_automatic_publishing_cycle(
        db, settings, api=api, media_http_client=media_client
    )
    result = run_automatic_publishing_cycle(
        db, settings, api=api, media_http_client=media_client
    )

    attempt = db.scalar(
        select(MetaPublishingAttempt).where(
            MetaPublishingAttempt.publication_job_id == job.id
        )
    )
    db.refresh(job)
    assert result.paused == 1
    assert attempt.phase == "failed"
    assert attempt.active_key is None
    assert job.status == JobStatus.FAILED


def test_automatic_scheduler_requires_explicit_disabled_emergency_stop(db, tmp_path):
    settings = production_settings(tmp_path)
    _, _, _, _, job = automatic_context(db, settings)
    stop = db.get(SystemSetting, "emergency_stop")
    db.delete(stop)
    db.commit()

    result = run_automatic_publishing_cycle(
        db,
        settings,
        api=PublishingApi(),
        media_http_client=public_media_client(settings),
    )

    db.refresh(job)
    assert result.queued == 0
    assert job.status == JobStatus.SCHEDULED
