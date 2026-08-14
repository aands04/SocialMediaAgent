from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet

from app.channels.webhooks import _process_whatsapp_payload
from app.config import Settings
from app.live.parser import MatchEventParseError, ParsedMatchEvent, parse_match_event
from app.live.publishing import recover_stale_live_deliveries, run_live_delivery_cycle
from app.live.service import (
    LiveEventError,
    confirm_event,
    create_match_event,
    ingest_whatsapp_message,
    normalize_phone,
)
from app.meta.security import TokenCipher
from app.models import (
    Club,
    ClubStatus,
    Game,
    InstagramPage,
    LiveDeliveryAttempt,
    LiveEventDelivery,
    LiveEventRule,
    LiveGameState,
    LiveReporter,
    LiveReporterTeam,
    MatchEvent,
    PlanProfile,
    SocialChannelConnection,
    SystemSetting,
    Team,
    TeamChannelAssignment,
    UsageLedgerEntry,
    UsageStatus,
    WhatsAppAudience,
    WhatsAppAudienceRecipient,
    WhatsAppMessageTemplate,
    WhatsAppRecipient,
)
from app.tenancy.state import system_scope


def live_fixture(db, *, suffix: str = "one"):
    page = InstagramPage(
        internal_name=f"instagram-{suffix}",
        display_name=f"Instagram {suffix}",
        username=f"instagram_{suffix}",
        club="Testverein",
        active=True,
        connection_status="connected",
        publishing_enabled=True,
    )
    db.add(page)
    db.flush()
    team = Team(
        internal_name=f"team-{suffix}",
        display_name=f"Testmannschaft {suffix}",
        short_name=f"Test {suffix}",
        slug=f"testmannschaft-{suffix}",
        club="Testverein",
        active=True,
        fussball_url=f"https://example.invalid/{suffix}",
        instagram_page_id=page.id,
        media_subdir=f"{suffix}/spieler",
        publishing_enabled=True,
    )
    db.add(team)
    db.flush()
    game = Game(
        team_id=team.id,
        provider="mock",
        external_id=f"game-{suffix}",
        home_team=team.display_name,
        away_team="Testgegner",
        kickoff=datetime.now(timezone.utc),
        competition="Testliga",
        venue="Testplatz",
        status="scheduled",
        source_url=f"fixture://{suffix}",
        checked_at=datetime.now(timezone.utc),
    )
    db.add(game)
    db.flush()
    return team, game


@pytest.mark.parametrize(
    ("message", "event_type", "minute", "home", "away"),
    [
        ("Tor 23 Müller", "goal", 23, None, None),
        ("Gegentor 31", "opponent_goal", 31, None, None),
        ("Gelb 42 Schmidt", "yellow_card", 42, None, None),
        ("Wechsel 60 Weber für Müller", "substitution", 60, None, None),
        ("Halbzeit 1:0", "halftime", None, 1, 0),
        ("Abpfiff 2:1", "fulltime", None, 2, 1),
        ("Korrektur 1:2", "score_correction", None, 1, 2),
        ("1:0 Martin Mohr", "score_update", None, 1, 0),
        ("1-0 Mohr", "score_update", None, 1, 0),
        ("34. Minute 1:0 Martin Mohr", "score_update", 34, 1, 0),
        ("1:1 52", "score_update", 52, 1, 1),
        ("2. Halbzeit", "second_half", None, None, None),
    ],
)
def test_deterministic_live_parser(message, event_type, minute, home, away):
    parsed = parse_match_event(message)

    assert parsed is not None
    assert parsed.event_type == event_type
    assert parsed.minute == minute
    assert parsed.home_score_after == home
    assert parsed.away_score_after == away


def test_parser_rejects_unknown_and_impossible_minute():
    assert parse_match_event("Wir sind gleich da") is None
    with pytest.raises(MatchEventParseError):
        ParsedMatchEvent("goal", minute=151).validated()


def test_confirmed_goal_updates_materialized_live_state(db):
    team, game = live_fixture(db)

    event = create_match_event(
        db,
        game=game,
        parsed=ParsedMatchEvent("goal", minute=12, player_name="Müller"),
        provider="dashboard",
        idempotency_key="manual-goal-1",
        created_by="pytest-actor",
        force_confirmed=True,
    )
    db.flush()
    state = db.query(LiveGameState).filter_by(game_id=game.id).one()

    assert event.status == "confirmed"
    assert state.home_score == 1
    assert state.away_score == 0
    assert state.minute == 12


def test_untrusted_reporter_event_waits_for_confirmation(db):
    team, game = live_fixture(db)
    connection = SocialChannelConnection(
        channel_type="whatsapp",
        internal_name="whatsapp-live",
        display_name="WhatsApp Live",
        external_account_id="waba-1",
        phone_number_id="phone-1",
        status="connected",
        active=True,
    )
    db.add(connection)
    db.flush()
    reporter = LiveReporter(
        channel_connection_id=connection.id,
        normalized_phone="+491701234567",
        display_name="Ticker",
        all_teams=True,
        trusted_auto_confirm=False,
        active=True,
        active_game_id=game.id,
    )
    db.add(reporter)
    db.flush()

    result = ingest_whatsapp_message(
        db,
        connection=connection,
        provider_message_id="wamid-1",
        sender="491701234567",
        text="Tor 23 Müller",
        settings=Settings(_env_file=None),
    )

    assert result.status == "pending"
    assert result.event is not None
    assert result.event.needs_confirmation is True
    state = db.query(LiveGameState).filter_by(game_id=game.id).one()
    assert (state.home_score, state.away_score) == (0, 0)

    confirm_event(db, result.event, user_id="pytest-actor")
    assert (state.home_score, state.away_score) == (1, 0)


def test_whatsapp_ingest_is_idempotent(db):
    team, game = live_fixture(db)
    connection = SocialChannelConnection(
        channel_type="whatsapp",
        internal_name="whatsapp-live",
        display_name="WhatsApp Live",
        external_account_id="waba-2",
        phone_number_id="phone-2",
        status="connected",
        active=True,
    )
    db.add(connection)
    db.flush()
    reporter = LiveReporter(
        channel_connection_id=connection.id,
        normalized_phone="+491701234567",
        display_name="Ticker",
        all_teams=True,
        trusted_auto_confirm=True,
        active=True,
        active_game_id=game.id,
    )
    db.add(reporter)
    db.flush()
    kwargs = dict(
        db=db,
        connection=connection,
        provider_message_id="wamid-idempotent",
        sender="491701234567",
        text="Tor 5 Müller",
        settings=Settings(_env_file=None),
    )

    first = ingest_whatsapp_message(**kwargs)
    second = ingest_whatsapp_message(**kwargs)

    assert first.event is not None
    assert second.event is not None
    assert first.event.id == second.event.id
    assert db.query(MatchEvent).filter_by(provider_event_id="wamid-idempotent").count() == 1
    state = db.query(LiveGameState).filter_by(game_id=game.id).one()
    assert state.home_score == 1


def test_score_only_message_is_resolved_against_live_state(db):
    _, game = live_fixture(db, suffix="score-update")
    connection = SocialChannelConnection(
        channel_type="whatsapp",
        internal_name="whatsapp-score-update",
        display_name="WhatsApp Live",
        external_account_id="waba-score-update",
        phone_number_id="phone-score-update",
        status="connected",
        active=True,
    )
    db.add(connection)
    db.flush()
    reporter = LiveReporter(
        channel_connection_id=connection.id,
        normalized_phone="+491701234570",
        display_name="Ticker",
        all_teams=True,
        trusted_auto_confirm=True,
        active=True,
        active_game_id=game.id,
    )
    db.add(reporter)
    db.flush()

    result = ingest_whatsapp_message(
        db,
        connection=connection,
        provider_message_id="wamid-score-update",
        sender="491701234570",
        text="1:0 durch Martin Mohr",
        settings=Settings(_env_file=None),
    )

    assert result.status == "confirmed"
    assert result.event is not None
    assert result.event.event_type == "goal"
    assert result.event.player_name == "Martin Mohr"
    state = db.query(LiveGameState).filter_by(game_id=game.id).one()
    assert (state.home_score, state.away_score) == (1, 0)


def test_equivalent_goal_from_second_reporter_is_suppressed(db):
    _, game = live_fixture(db, suffix="semantic-duplicate")
    connection = SocialChannelConnection(
        channel_type="whatsapp",
        internal_name="whatsapp-semantic-duplicate",
        display_name="WhatsApp Live",
        external_account_id="waba-semantic-duplicate",
        phone_number_id="phone-semantic-duplicate",
        status="connected",
        active=True,
    )
    db.add(connection)
    db.flush()
    reporters = []
    for index, phone in enumerate(("+491701234571", "+491701234572"), start=1):
        reporter = LiveReporter(
            channel_connection_id=connection.id,
            normalized_phone=phone,
            display_name=f"Ticker {index}",
            all_teams=True,
            trusted_auto_confirm=True,
            active=True,
            active_game_id=game.id,
        )
        db.add(reporter)
        reporters.append(reporter)
    db.flush()

    first = ingest_whatsapp_message(
        db,
        connection=connection,
        provider_message_id="wamid-semantic-first",
        sender=reporters[0].normalized_phone,
        text="1:0 Martin Mohr",
        settings=Settings(_env_file=None),
    )
    second = ingest_whatsapp_message(
        db,
        connection=connection,
        provider_message_id="wamid-semantic-second",
        sender=reporters[1].normalized_phone,
        text="Tor durch Martin Mohr 1:0",
        settings=Settings(_env_file=None),
    )

    assert first.event is not None
    assert second.event is not None
    assert second.event.id == first.event.id
    assert db.query(MatchEvent).filter_by(game_id=game.id).count() == 1
    state = db.query(LiveGameState).filter_by(game_id=game.id).one()
    assert (state.home_score, state.away_score) == (1, 0)


def test_unknown_whatsapp_reporter_is_not_applied(db):
    _, _game = live_fixture(db)
    connection = SocialChannelConnection(
        channel_type="whatsapp",
        internal_name="whatsapp-live",
        display_name="WhatsApp Live",
        external_account_id="waba-unknown",
        phone_number_id="phone-unknown",
        status="connected",
        active=True,
    )
    db.add(connection)
    db.flush()

    result = ingest_whatsapp_message(
        db,
        connection=connection,
        provider_message_id="wamid-unknown",
        sender="491799999999",
        text="Tor 8 Unbekannt",
        settings=Settings(_env_file=None),
    )

    assert result.status == "unknown_reporter"
    assert db.query(MatchEvent).count() == 0


def test_inconclusive_ai_parse_is_recorded_as_non_billable_usage(db, monkeypatch):
    _, game = live_fixture(db, suffix="ai-inconclusive")
    connection = SocialChannelConnection(
        channel_type="whatsapp",
        internal_name="whatsapp-ai-inconclusive",
        display_name="WhatsApp AI",
        external_account_id="waba-ai-inconclusive",
        phone_number_id="phone-ai-inconclusive",
        status="connected",
        active=True,
    )
    db.add(connection)
    db.flush()
    reporter = LiveReporter(
        channel_connection_id=connection.id,
        normalized_phone="+491701234571",
        display_name="KI-Reporter",
        all_teams=True,
        active=True,
        active_game_id=game.id,
    )
    db.add(reporter)
    db.flush()

    class InconclusiveAiParser:
        def __init__(self, _api_key, _model):
            pass

        def parse(self, _text):
            return None

    monkeypatch.setattr("app.live.service.OpenAIMatchEventParser", InconclusiveAiParser)

    result = ingest_whatsapp_message(
        db,
        connection=connection,
        provider_message_id="wamid-ai-inconclusive",
        sender=reporter.normalized_phone,
        text="Unklare Meldung vom Spielfeld",
        settings=Settings(
            _env_file=None,
            live_event_ai_parsing_enabled=True,
            openai_api_key="test-key",
        ),
    )

    assert result.status == "manual_review"
    usage = (
        db.query(UsageLedgerEntry)
        .filter_by(idempotency_key="live-parse:wamid-ai-inconclusive")
        .one()
    )
    assert usage.generation_type == "live_event_parsing"
    assert usage.status == UsageStatus.COMPLETED_NOT_BILLABLE
    assert usage.actual_quantity == 1
    assert usage.billable is False


def test_reporter_cannot_report_foreign_team_inside_club(db):
    first_team, _ = live_fixture(db, suffix="first")
    _, second_game = live_fixture(db, suffix="second")
    reporter = LiveReporter(
        user_id=None,
        normalized_phone="+491701234568",
        display_name="Eingeschränkt",
        all_teams=False,
        active=True,
    )
    db.add(reporter)
    db.flush()
    db.add(LiveReporterTeam(reporter_id=reporter.id, team_id=first_team.id))
    db.flush()

    with pytest.raises(LiveEventError, match="nicht berechtigt"):
        create_match_event(
            db,
            game=second_game,
            parsed=ParsedMatchEvent("kickoff"),
            provider="whatsapp",
            idempotency_key="wrong-team",
            reporter=reporter,
        )


def test_live_rule_creates_channel_delivery_only_with_existing_assignment(db):
    team, game = live_fixture(db)
    db.add(SystemSetting(key="emergency_stop", value={"enabled": False}))
    connection = SocialChannelConnection(
        channel_type="whatsapp",
        internal_name="whatsapp-live",
        display_name="WhatsApp Live",
        external_account_id="waba-rule",
        phone_number_id="phone-rule",
        status="connected",
        capabilities=["direct_message"],
        active=True,
        publishing_enabled=True,
        automatic_delivery_enabled=True,
    )
    db.add(connection)
    db.flush()
    recipient = WhatsAppRecipient(
        channel_connection_id=connection.id,
        normalized_phone="+491701234599",
        display_name="Live-Empfänger",
        opt_in_status="confirmed",
        opt_in_at=datetime.now(timezone.utc),
        opt_in_source="pytest",
        active=True,
        preferred_message_types=["live_event"],
    )
    db.add(recipient)
    db.flush()
    audience = WhatsAppAudience(
        channel_connection_id=connection.id,
        name="Vereinsinfos",
        audience_type="recipient_list",
        eligibility_status="available",
        active=True,
    )
    db.add(audience)
    db.flush()
    db.add(
        WhatsAppAudienceRecipient(
            audience_id=audience.id,
            recipient_id=recipient.id,
        )
    )
    db.add(
        TeamChannelAssignment(
            team_id=team.id,
            channel_connection_id=connection.id,
            enabled=True,
            announcement_enabled=True,
            result_enabled=True,
        )
    )
    db.add(
        LiveEventRule(
            team_id=team.id,
            event_type="goal",
            delivery_mode="automatic",
            audience_type="opt_in_recipients",
            whatsapp_audience_id=audience.id,
            channel_types=["dashboard", "whatsapp"],
            enabled=True,
            require_confirmation=False,
        )
    )
    db.flush()

    event = create_match_event(
        db,
        game=game,
        parsed=ParsedMatchEvent("goal", minute=7),
        provider="dashboard",
        idempotency_key="delivery-goal",
        force_confirmed=True,
    )
    db.flush()
    deliveries = db.query(LiveEventDelivery).filter_by(event_id=event.id).all()

    assert {(item.channel_type, item.status) for item in deliveries} == {
        ("dashboard", "delivered"),
        ("whatsapp", "queued"),
    }


def test_group_delivery_is_blocked_without_official_capability(db):
    team, game = live_fixture(db)
    db.add(SystemSetting(key="emergency_stop", value={"enabled": False}))
    connection = SocialChannelConnection(
        channel_type="whatsapp",
        internal_name="whatsapp-live",
        display_name="WhatsApp Live",
        external_account_id="waba-group",
        phone_number_id="phone-group",
        status="connected",
        capabilities=["direct_message"],
        settings={"group_id": "group-1"},
        active=True,
        publishing_enabled=True,
        automatic_delivery_enabled=True,
    )
    db.add(connection)
    db.flush()
    audience = WhatsAppAudience(
        channel_connection_id=connection.id,
        name="Nicht verfügbare Gruppe",
        audience_type="group",
        external_group_id="group-1",
        eligibility_status="not_available",
        active=True,
    )
    db.add(audience)
    db.flush()
    db.add(
        TeamChannelAssignment(team_id=team.id, channel_connection_id=connection.id, enabled=True)
    )
    db.add(
        LiveEventRule(
            team_id=team.id,
            event_type="kickoff",
            delivery_mode="automatic",
            audience_type="eligible_group",
            whatsapp_audience_id=audience.id,
            channel_types=["whatsapp"],
            enabled=True,
            require_confirmation=False,
        )
    )
    db.flush()

    event = create_match_event(
        db,
        game=game,
        parsed=ParsedMatchEvent("kickoff"),
        provider="dashboard",
        idempotency_key="group-kickoff",
        force_confirmed=True,
    )
    db.flush()
    delivery = db.query(LiveEventDelivery).filter_by(event_id=event.id).one()

    assert delivery.status == "blocked"
    assert "nicht verfügbar" in delivery.last_error


def test_normalize_phone_rejects_invalid_values():
    assert normalize_phone("+49 170 1234567") == "+491701234567"
    assert normalize_phone("0049 170 1234567") == "+491701234567"
    with pytest.raises(LiveEventError):
        normalize_phone("123")
    with pytest.raises(LiveEventError, match="Ländervorwahl"):
        normalize_phone("0170 1234567")


def test_live_reporters_are_hidden_across_tenants(db):
    with system_scope("zweiten Live-Center-Testverein anlegen"):
        profile = PlanProfile(name="Fremdes Live-Profil", description="Test", version=1)
        db.add(profile)
        db.flush()
        foreign_club = Club(
            name="Fremder Live-Verein",
            short_name="Fremd",
            slug="fremder-live-verein",
            status=ClubStatus.ACTIVE,
            timezone="Europe/Berlin",
            plan_profile_id=profile.id,
        )
        db.add(foreign_club)
        db.flush()
        foreign_reporter = LiveReporter(
            club_id=foreign_club.id,
            normalized_phone="+491709999999",
            display_name="Fremder Reporter",
            all_teams=True,
            active=True,
        )
        db.add(foreign_reporter)
        db.flush()
        foreign_reporter_id = foreign_reporter.id
        db.expunge(foreign_reporter)

    assert db.get(LiveReporter, foreign_reporter_id) is None
    assert "Fremder Reporter" not in {
        reporter.display_name for reporter in db.query(LiveReporter).all()
    }


def test_implausible_whatsapp_correction_stays_pending_for_manual_review(db):
    _, game = live_fixture(db, suffix="unsafe-correction")
    connection = SocialChannelConnection(
        channel_type="whatsapp",
        internal_name="whatsapp-unsafe-correction",
        display_name="WhatsApp Live",
        external_account_id="waba-unsafe-correction",
        phone_number_id="phone-unsafe-correction",
        status="connected",
        active=True,
    )
    db.add(connection)
    db.flush()
    reporter = LiveReporter(
        channel_connection_id=connection.id,
        normalized_phone="+491701234569",
        display_name="Ticker ohne Korrekturrecht",
        all_teams=True,
        trusted_auto_confirm=True,
        may_correct=False,
        active=True,
        active_game_id=game.id,
    )
    db.add(reporter)
    db.flush()

    result = ingest_whatsapp_message(
        db,
        connection=connection,
        provider_message_id="wamid-unsafe-correction",
        sender="491701234569",
        text="Korrektur 9:0",
        settings=Settings(_env_file=None),
    )

    assert result.status == "pending"
    assert result.event is not None
    assert result.event.needs_confirmation is True
    assert any(
        "keine Spielstandskorrektur" in warning
        for warning in result.event.metadata_json["warnings"]
    )
    state = db.query(LiveGameState).filter_by(game_id=game.id).one()
    assert (state.home_score, state.away_score) == (0, 0)


class RecordingWhatsAppApi:
    def __init__(self):
        self.calls: list[dict] = []

    def send_whatsapp_template(self, **kwargs):
        self.calls.append(kwargs)
        return {"messages": [{"id": f"wamid-live-{len(self.calls)}"}]}


def _queued_live_whatsapp_delivery(db):
    team, game = live_fixture(db, suffix="publish")
    db.add(SystemSetting(key="emergency_stop", value={"enabled": False}))
    key = Fernet.generate_key().decode("ascii")
    connection = SocialChannelConnection(
        channel_type="whatsapp",
        internal_name="whatsapp-publish",
        display_name="WhatsApp Live",
        external_account_id="waba-publish",
        phone_number_id="phone-publish",
        status="connected",
        settings={"phone_registered": True},
        capabilities=["direct_message", "template_message"],
        active=True,
        publishing_enabled=True,
        automatic_delivery_enabled=True,
        encrypted_token=TokenCipher(key).encrypt("provider-token"),
    )
    db.add(connection)
    db.flush()
    recipient = WhatsAppRecipient(
        channel_connection_id=connection.id,
        normalized_phone="+491701234577",
        display_name="Live-Empfänger",
        opt_in_status="confirmed",
        opt_in_at=datetime.now(timezone.utc),
        opt_in_source="pytest",
        active=True,
        preferred_message_types=["live_event"],
    )
    db.add(recipient)
    db.flush()
    audience = WhatsAppAudience(
        channel_connection_id=connection.id,
        name="Live-Liste",
        audience_type="recipient_list",
        eligibility_status="available",
        active=True,
    )
    db.add(audience)
    db.flush()
    db.add(
        WhatsAppAudienceRecipient(
            audience_id=audience.id,
            recipient_id=recipient.id,
        )
    )
    db.add(
        WhatsAppMessageTemplate(
            channel_connection_id=connection.id,
            name="live_event_de",
            provider_template_id="provider-live-event",
            language="de",
            category="utility",
            message_type="live_event",
            status="approved",
            components=[{"type": "BODY", "text": "{{1}}"}],
        )
    )
    db.add(
        TeamChannelAssignment(
            team_id=team.id,
            channel_connection_id=connection.id,
            enabled=True,
            announcement_enabled=True,
            result_enabled=True,
        )
    )
    db.add(
        LiveEventRule(
            team_id=team.id,
            event_type="goal",
            delivery_mode="automatic",
            audience_type="opt_in_recipients",
            whatsapp_audience_id=audience.id,
            channel_types=["whatsapp"],
            enabled=True,
            require_confirmation=False,
        )
    )
    db.flush()
    event = create_match_event(
        db,
        game=game,
        parsed=ParsedMatchEvent("goal", minute=19, player_name="Testspieler"),
        provider="dashboard",
        idempotency_key="publish-goal",
        force_confirmed=True,
    )
    db.flush()
    delivery = db.query(LiveEventDelivery).filter_by(event_id=event.id).one()
    settings = Settings(
        _env_file=None,
        environment="production",
        meta_production_enabled=True,
        global_publish_enabled=True,
        meta_scheduler_enabled=True,
        meta_automatic_publish_enabled=True,
        whatsapp_channel_enabled=True,
        meta_token_encryption_key=key,
    )
    return delivery, settings


def test_live_whatsapp_delivery_is_sent_once_and_idempotent(db):
    delivery, settings = _queued_live_whatsapp_delivery(db)
    api = RecordingWhatsAppApi()

    first = run_live_delivery_cycle(db, settings, api=api)
    second = run_live_delivery_cycle(db, settings, api=api)

    db.refresh(delivery)
    attempt = db.query(LiveDeliveryAttempt).filter_by(delivery_id=delivery.id).one()
    assert first.delivered == 1
    assert second.queued == 0
    assert len(api.calls) == 1
    assert api.calls[0]["to"] == "+491701234577"
    assert api.calls[0]["components"] == [
        {"type": "body", "parameters": [{"type": "text", "text": delivery.message_snapshot}]}
    ]
    assert attempt.status == "sent"
    assert delivery.status == "sent"
    assert delivery.delivered_at is None


def test_stale_live_delivery_never_blindly_repeats_in_flight_send(db):
    delivery, _settings = _queued_live_whatsapp_delivery(db)
    delivery.status = "processing"
    delivery.updated_at = datetime.now(timezone.utc) - timedelta(minutes=30)
    db.flush()
    attempt = LiveDeliveryAttempt(
        delivery_id=delivery.id,
        status="processing",
        idempotency_key=f"{delivery.id}:manual-recovery-test",
    )
    db.add(attempt)
    db.flush()
    delivery.updated_at = datetime.now(timezone.utc) - timedelta(minutes=30)
    db.commit()

    recovered = recover_stale_live_deliveries(db)

    db.refresh(delivery)
    db.refresh(attempt)
    assert recovered == 1
    assert delivery.status == "failed"
    assert attempt.status == "uncertain"
    assert attempt.error_category == "worker_interrupted"


def test_live_whatsapp_delivery_is_only_delivered_after_meta_webhook(db):
    delivery, settings = _queued_live_whatsapp_delivery(db)
    run_live_delivery_cycle(db, settings, api=RecordingWhatsAppApi())
    attempt = db.query(LiveDeliveryAttempt).filter_by(delivery_id=delivery.id).one()
    connection = db.get(SocialChannelConnection, delivery.channel_connection_id)

    _process_whatsapp_payload(
        db,
        {
            "entry": [
                {
                    "id": connection.parent_business_id or connection.external_account_id,
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": connection.phone_number_id},
                                "statuses": [
                                    {
                                        "id": attempt.platform_id,
                                        "status": "delivered",
                                    }
                                ],
                            }
                        }
                    ],
                }
            ]
        },
        connection,
    )
    db.flush()

    assert attempt.status == "delivered"
    assert attempt.delivered_at is not None
    assert delivery.status == "delivered"
    assert delivery.delivered_at is not None


def test_live_whatsapp_failure_webhook_marks_parent_failed(db):
    delivery, settings = _queued_live_whatsapp_delivery(db)
    run_live_delivery_cycle(db, settings, api=RecordingWhatsAppApi())
    attempt = db.query(LiveDeliveryAttempt).filter_by(delivery_id=delivery.id).one()
    connection = db.get(SocialChannelConnection, delivery.channel_connection_id)

    _process_whatsapp_payload(
        db,
        {
            "entry": [
                {
                    "id": connection.parent_business_id or connection.external_account_id,
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": connection.phone_number_id},
                                "statuses": [
                                    {
                                        "id": attempt.platform_id,
                                        "status": "failed",
                                        "errors": [{"title": "Empfänger nicht erreichbar"}],
                                    }
                                ],
                            }
                        }
                    ],
                }
            ]
        },
        connection,
    )
    db.flush()

    assert attempt.status == "failed"
    assert attempt.error_category == "provider_delivery_failed"
    assert delivery.status == "failed"
    assert delivery.last_error == "Empfänger nicht erreichbar"
