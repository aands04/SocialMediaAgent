from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from datetime import timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.games.identity import team_name_variants
from app.live.parser import OpenAIMatchEventParser, ParsedMatchEvent, sanitize_message_text
from app.live.providers import WhatsAppMatchEventProvider
from app.models import (
    AuditLog,
    Club,
    ClubStatus,
    Game,
    LiveEventDelivery,
    LiveEventRule,
    LiveGameState,
    LiveReporter,
    LiveReporterTeam,
    MatchEvent,
    SocialChannelConnection,
    SystemSetting,
    Team,
    TeamChannelAssignment,
    UsageLedgerEntry,
    UsageStatus,
    WhatsAppAudience,
    WhatsAppAudienceRecipient,
    WhatsAppRecipient,
    now,
    uid,
)
from app.usage.service import billing_period


class LiveEventError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class IngestResult:
    status: str
    event: MatchEvent | None = None
    message: str | None = None


PHASE_FOR_EVENT = {
    "kickoff": "first_half",
    "halftime": "halftime",
    "second_half": "second_half",
    "fulltime": "finished",
    "interruption": "interrupted",
    "resume": "second_half",
    "abandoned": "abandoned",
}


def normalize_phone(value: str) -> str:
    raw = value.strip()
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("00"):
        digits = digits[2:]
    elif raw.startswith("0"):
        raise LiveEventError(
            "Telefonnummer muss mit internationaler Ländervorwahl angegeben werden"
        )
    if not 7 <= len(digits) <= 15:
        raise LiveEventError("Telefonnummer ist ungültig")
    if digits.startswith("0"):
        raise LiveEventError("Telefonnummer ist keine gültige internationale Nummer")
    return f"+{digits}"


def reporter_can_access_team(db: Session, reporter: LiveReporter, team_id: str) -> bool:
    if reporter.all_teams:
        team = db.get(Team, team_id)
        return team is not None and team.club_id == reporter.club_id
    assignment = db.get(
        LiveReporterTeam,
        {"reporter_id": reporter.id, "team_id": team_id},
    )
    return assignment is not None and assignment.club_id == reporter.club_id


def get_or_create_state(db: Session, game: Game) -> LiveGameState:
    # Serialize event ordering and state transitions on one stable game row.
    # PostgreSQL locks it; SQLite safely ignores FOR UPDATE in tests.
    db.execute(select(Game.id).where(Game.id == game.id).with_for_update()).scalar_one()
    state = db.scalar(
        select(LiveGameState).where(LiveGameState.game_id == game.id).with_for_update()
    )
    if state is None:
        state = LiveGameState(
            game_id=game.id,
            team_id=game.team_id,
            phase="finished" if game.result_confirmed else "scheduled",
            home_score=game.home_score or 0,
            away_score=game.away_score or 0,
            source=game.provider,
            finished_at=game.checked_at if game.result_confirmed else None,
        )
        db.add(state)
        db.flush()
    return state


def own_team_is_home(team: Team, game: Game) -> bool:
    own = set()
    for value in (team.display_name, team.internal_name, team.short_name, team.club):
        own.update(team_name_variants(value or ""))
    home_match = bool(own & team_name_variants(game.home_team))
    away_match = bool(own & team_name_variants(game.away_team))
    if home_match == away_match:
        raise LiveEventError("Eigene Mannschaft ist in der Spielpaarung nicht eindeutig")
    return home_match


def _derived_score(
    state: LiveGameState,
    game: Game,
    team: Team,
    parsed: ParsedMatchEvent,
) -> tuple[int | None, int | None]:
    if parsed.home_score_after is not None and parsed.away_score_after is not None:
        return parsed.home_score_after, parsed.away_score_after
    home, away = state.home_score, state.away_score
    own_home = own_team_is_home(team, game)
    if parsed.event_type in {"goal", "penalty_scored"}:
        return (home + 1, away) if own_home else (home, away + 1)
    if parsed.event_type in {"opponent_goal", "own_goal"}:
        return (home, away + 1) if own_home else (home + 1, away)
    return None, None


def _resolve_score_update(
    state: LiveGameState,
    game: Game,
    team: Team,
    parsed: ParsedMatchEvent,
) -> ParsedMatchEvent:
    if parsed.event_type != "score_update":
        return parsed
    if parsed.home_score_after is None or parsed.away_score_after is None:
        raise LiveEventError("Spielstandsmeldung ist unvollständig")
    home_delta = parsed.home_score_after - state.home_score
    away_delta = parsed.away_score_after - state.away_score
    if (home_delta, away_delta) not in {(1, 0), (0, 1)}:
        raise LiveEventError("Spielstandsmeldung ist nicht eindeutig plausibel")
    own_scored = (home_delta == 1) == own_team_is_home(team, game)
    return replace(parsed, event_type="goal" if own_scored else "opponent_goal")


def _normalized_person(value: str | None) -> str:
    return re.sub(r"[^a-z0-9äöüß]", "", (value or "").casefold())


def _semantic_duplicate(
    db: Session,
    *,
    game: Game,
    parsed: ParsedMatchEvent,
    home_after: int | None,
    away_after: int | None,
) -> MatchEvent | None:
    cutoff = now() - timedelta(minutes=3)
    event_types = (
        {"goal", "opponent_goal"} if parsed.event_type == "score_update" else {parsed.event_type}
    )
    candidates = list(
        db.scalars(
            select(MatchEvent)
            .where(
                MatchEvent.game_id == game.id,
                MatchEvent.event_type.in_(event_types),
                MatchEvent.status.in_({"pending", "confirmed"}),
                MatchEvent.occurred_at >= cutoff,
            )
            .order_by(MatchEvent.occurred_at.desc())
        )
    )
    person = _normalized_person(parsed.player_name)
    for candidate in candidates:
        candidate_person = _normalized_person(candidate.player_name)
        same_person = not person or not candidate_person or person == candidate_person
        same_minute = (
            parsed.minute is None
            or candidate.minute is None
            or abs(parsed.minute - candidate.minute) <= 1
        )
        same_score = (
            home_after is None
            or away_after is None
            or candidate.home_score_after is None
            or candidate.away_score_after is None
            or (
                candidate.home_score_after == home_after
                and candidate.away_score_after == away_after
            )
        )
        if same_person and same_minute and same_score:
            return candidate
    return None


def _validate_transition(
    state: LiveGameState,
    parsed: ParsedMatchEvent,
    home_after: int | None,
    away_after: int | None,
    *,
    correction_allowed: bool,
) -> list[str]:
    warnings: list[str] = []
    if parsed.minute is not None and state.minute is not None and parsed.minute + 5 < state.minute:
        warnings.append("Spielminute liegt deutlich vor dem aktuellen Stand")
    if home_after is not None and away_after is not None:
        decreasing = home_after < state.home_score or away_after < state.away_score
        if decreasing and parsed.event_type != "score_correction":
            warnings.append("Spielstand würde ohne Korrektur sinken")
        if parsed.event_type == "score_correction" and not correction_allowed:
            warnings.append("Reporter darf keine Spielstandskorrektur bestätigen")
        if max(
            home_after - state.home_score, away_after - state.away_score
        ) > 1 and parsed.event_type in {
            "goal",
            "opponent_goal",
            "own_goal",
            "penalty_scored",
        }:
            warnings.append("Ein einzelnes Tor überspringt mehr als einen Treffer")
    if state.phase in {"finished", "abandoned"} and parsed.event_type not in {
        "score_correction",
        "event_correction",
        "comment",
    }:
        warnings.append("Spiel ist bereits beendet")
    return warnings


def _audit(
    db: Session,
    *,
    action: str,
    entity_type: str,
    entity_id: str,
    user_id: str | None,
    team_id: str,
    details: dict | None = None,
) -> None:
    db.add(
        AuditLog(
            user_id=user_id,
            team_id=team_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details or {},
        )
    )


def create_match_event(
    db: Session,
    *,
    game: Game,
    parsed: ParsedMatchEvent,
    provider: str,
    idempotency_key: str,
    reporter: LiveReporter | None = None,
    channel_connection_id: str | None = None,
    provider_event_id: str | None = None,
    created_by: str | None = None,
    raw_text: str | None = None,
    source_sender_id: str | None = None,
    force_confirmed: bool = False,
    supersedes_event_id: str | None = None,
) -> MatchEvent:
    parsed.validated()
    existing = db.scalar(select(MatchEvent).where(MatchEvent.idempotency_key == idempotency_key))
    if existing:
        return existing
    team = db.get(Team, game.team_id)
    if team is None or team.club_id != game.club_id:
        raise LiveEventError("Spiel und Mannschaft gehören nicht eindeutig zusammen")
    if reporter and (
        reporter.club_id != game.club_id
        or not reporter.active
        or not reporter_can_access_team(db, reporter, game.team_id)
    ):
        raise LiveEventError("Reporter ist für diese Mannschaft nicht berechtigt")
    state = get_or_create_state(db, game)
    if parsed.event_type == "score_update":
        duplicate = _semantic_duplicate(
            db,
            game=game,
            parsed=parsed,
            home_after=parsed.home_score_after,
            away_after=parsed.away_score_after,
        )
        if duplicate:
            _audit(
                db,
                action="live.event_duplicate_suppressed",
                entity_type="match_event",
                entity_id=duplicate.id,
                user_id=created_by,
                team_id=game.team_id,
                details={"provider": provider, "event_type": duplicate.event_type},
            )
            return duplicate
    parsed = _resolve_score_update(state, game, team, parsed)
    if (
        reporter
        and reporter.allowed_event_types
        and parsed.event_type not in set(reporter.allowed_event_types)
    ):
        raise LiveEventError("Reporter darf diesen Ereignistyp nicht melden")
    home_after, away_after = _derived_score(state, game, team, parsed)
    duplicate = _semantic_duplicate(
        db,
        game=game,
        parsed=parsed,
        home_after=home_after,
        away_after=away_after,
    )
    if duplicate:
        _audit(
            db,
            action="live.event_duplicate_suppressed",
            entity_type="match_event",
            entity_id=duplicate.id,
            user_id=created_by,
            team_id=game.team_id,
            details={"provider": provider, "event_type": parsed.event_type},
        )
        return duplicate
    correction_allowed = force_confirmed or bool(reporter and reporter.may_correct)
    warnings = _validate_transition(
        state,
        parsed,
        home_after,
        away_after,
        correction_allowed=correction_allowed,
    )
    auto_confirm = force_confirmed or bool(
        reporter and reporter.trusted_auto_confirm and parsed.confidence >= 0.95 and not warnings
    )
    clean = sanitize_message_text(raw_text or "") or None
    sequence = (
        int(
            db.scalar(
                select(func.max(MatchEvent.event_sequence)).where(MatchEvent.game_id == game.id)
            )
            or 0
        )
        + 1
    )
    own_home = own_team_is_home(team, game)
    own_after = (
        (home_after if own_home else away_after)
        if home_after is not None and away_after is not None
        else None
    )
    opponent_after = (
        (away_after if own_home else home_after)
        if home_after is not None and away_after is not None
        else None
    )
    team_side = (
        "own"
        if parsed.event_type
        in {
            "goal",
            "penalty_scored",
            "penalty_missed",
            "yellow_card",
            "second_yellow_card",
            "red_card",
            "substitution",
        }
        else "opponent"
        if parsed.event_type in {"opponent_goal", "own_goal"}
        else "neutral"
    )
    event = MatchEvent(
        game_id=game.id,
        team_id=game.team_id,
        reporter_id=reporter.id if reporter else None,
        channel_connection_id=channel_connection_id,
        provider=provider,
        provider_event_id=provider_event_id,
        idempotency_key=idempotency_key,
        event_sequence=sequence,
        event_type=parsed.event_type,
        team_side=team_side,
        status="confirmed" if auto_confirm else "pending",
        minute=parsed.minute,
        stoppage_minute=parsed.stoppage_minute,
        home_score_after=home_after,
        away_score_after=away_after,
        own_score_after=own_after,
        opponent_score_after=opponent_after,
        player_name=parsed.player_name,
        assist_name=parsed.assist_name,
        related_player_name=parsed.related_player_name,
        reason=parsed.reason,
        comment=parsed.comment,
        confidence=Decimal(str(parsed.confidence)),
        needs_confirmation=not auto_confirm,
        supersedes_event_id=supersedes_event_id,
        raw_text_digest=hashlib.sha256(clean.encode("utf-8")).hexdigest() if clean else None,
        source_sender_digest=(
            hashlib.sha256(source_sender_id.encode("utf-8")).hexdigest()
            if source_sender_id
            else None
        ),
        sanitized_input=clean,
        metadata_json={"parser": parsed.parser, "warnings": warnings},
        occurred_at=now(),
        confirmed_at=now() if auto_confirm else None,
        confirmed_by=created_by if auto_confirm else None,
        created_by=created_by,
    )
    try:
        with db.begin_nested():
            db.add(event)
            db.flush()
    except IntegrityError:
        existing = db.scalar(
            select(MatchEvent).where(MatchEvent.idempotency_key == idempotency_key)
        )
        if existing:
            return existing
        raise
    if supersedes_event_id and auto_confirm:
        previous = db.get(MatchEvent, supersedes_event_id)
        if previous is None or previous.game_id != game.id:
            raise LiveEventError("Zu korrigierendes Ereignis gehört nicht zu diesem Spiel")
        previous.status = "superseded"
        previous.needs_confirmation = False
        previous.corrected_at = now()
        previous.corrected_by = created_by
    state.last_event_sequence = max(state.last_event_sequence, sequence)
    if auto_confirm:
        apply_confirmed_event(db, event, state=state)
        plan_live_deliveries(db, event)
    _audit(
        db,
        action="live.event_created",
        entity_type="match_event",
        entity_id=event.id,
        user_id=created_by,
        team_id=game.team_id,
        details={
            "provider": provider,
            "event_type": parsed.event_type,
            "status": event.status,
            "warnings": warnings,
        },
    )
    return event


def apply_confirmed_event(
    db: Session,
    event: MatchEvent,
    *,
    state: LiveGameState | None = None,
) -> LiveGameState:
    if event.status != "confirmed":
        raise LiveEventError("Nur bestätigte Ereignisse dürfen den Live-Stand verändern")
    game = db.get(Game, event.game_id)
    if game is None:
        raise LiveEventError("Spiel fehlt")
    state = state or get_or_create_state(db, game)
    if event.home_score_after is not None and event.away_score_after is not None:
        state.home_score = event.home_score_after
        state.away_score = event.away_score_after
    if event.minute is not None:
        state.minute = event.minute
        state.stoppage_minute = event.stoppage_minute
    if event.event_type in PHASE_FOR_EVENT:
        state.phase = PHASE_FOR_EVENT[event.event_type]
    if event.event_type == "kickoff" and state.started_at is None:
        state.started_at = event.occurred_at
    if event.event_type in {"fulltime", "abandoned"}:
        state.finished_at = event.occurred_at
    state.last_event_id = event.id
    state.last_event_sequence = max(state.last_event_sequence, event.event_sequence)
    state.source = event.provider
    if event.event_type in {"fulltime", "score_correction"}:
        game.home_score = state.home_score
        game.away_score = state.away_score
    if event.event_type == "fulltime":
        game.status = "finished"
        game.result_confirmed = True
    db.flush()
    return state


def confirm_event(db: Session, event: MatchEvent, *, user_id: str) -> MatchEvent:
    if event.status != "pending":
        raise LiveEventError("Ereignis ist nicht mehr zur Bestätigung offen")
    warnings = list((event.metadata_json or {}).get("warnings") or [])
    if warnings:
        raise LiveEventError("Ereignis enthält ungeklärte Plausibilitätswarnungen")
    event.status = "confirmed"
    event.needs_confirmation = False
    event.confirmed_at = now()
    event.confirmed_by = user_id
    apply_confirmed_event(db, event)
    plan_live_deliveries(db, event)
    _audit(
        db,
        action="live.event_confirmed",
        entity_type="match_event",
        entity_id=event.id,
        user_id=user_id,
        team_id=event.team_id,
    )
    return event


def _live_message(event: MatchEvent, game: Game) -> str:
    minute = f" ({event.minute}. Minute)" if event.minute is not None else ""
    score = (
        f" – {event.home_score_after}:{event.away_score_after}"
        if event.home_score_after is not None and event.away_score_after is not None
        else ""
    )
    labels = {
        "kickoff": "Anpfiff",
        "goal": "Tor für uns",
        "opponent_goal": "Tor für den Gegner",
        "own_goal": "Eigentor",
        "halftime": "Halbzeit",
        "second_half": "Die zweite Halbzeit läuft",
        "fulltime": "Abpfiff",
        "yellow_card": "Gelbe Karte",
        "second_yellow_card": "Gelb-Rote Karte",
        "red_card": "Rote Karte",
        "substitution": "Wechsel",
        "interruption": "Spielunterbrechung",
        "resume": "Das Spiel wird fortgesetzt",
        "abandoned": "Spielabbruch",
        "score_correction": "Korrigierter Spielstand",
        "comment": "Live-Info",
    }
    actor = f": {event.player_name}" if event.player_name else ""
    return f"{labels.get(event.event_type, 'Live-Info')}{minute}{actor}{score}\n{game.home_team} – {game.away_team}"


def plan_live_deliveries(db: Session, event: MatchEvent) -> list[LiveEventDelivery]:
    if event.status != "confirmed":
        return []
    game = db.get(Game, event.game_id)
    club = db.get(Club, event.club_id)
    if game is None or club is None:
        raise LiveEventError("Verein oder Spiel fehlt")
    rules = list(
        db.scalars(
            select(LiveEventRule).where(
                LiveEventRule.team_id == event.team_id,
                LiveEventRule.event_type == event.event_type,
                LiveEventRule.enabled.is_(True),
                LiveEventRule.delivery_mode != "off",
            )
        )
    )
    created: list[LiveEventDelivery] = []
    state = get_or_create_state(db, game)
    paused = bool((club.technical_settings or {}).get("live_center_paused")) or bool(
        state.live_publishing_paused
    )
    emergency = db.get(SystemSetting, "emergency_stop")
    globally_paused = emergency is None or emergency.value.get("enabled") is not False
    for rule in rules:
        audience = (
            db.get(WhatsAppAudience, rule.whatsapp_audience_id)
            if rule.whatsapp_audience_id
            else None
        )
        for channel_type in dict.fromkeys(rule.channel_types or ["dashboard"]):
            if channel_type not in {"dashboard", "instagram", "facebook", "whatsapp"}:
                continue
            connections: list[SocialChannelConnection | None] = [None]
            if channel_type != "dashboard":
                connections = list(
                    db.scalars(
                        select(SocialChannelConnection)
                        .join(
                            TeamChannelAssignment,
                            TeamChannelAssignment.channel_connection_id
                            == SocialChannelConnection.id,
                        )
                        .where(
                            TeamChannelAssignment.team_id == event.team_id,
                            TeamChannelAssignment.enabled.is_(True),
                            SocialChannelConnection.channel_type == channel_type,
                            SocialChannelConnection.active.is_(True),
                            SocialChannelConnection.status == "connected",
                        )
                    )
                )
                if channel_type == "whatsapp" and audience is not None:
                    connections = [
                        item for item in connections if item.id == audience.channel_connection_id
                    ]
            if not connections:
                continue
            for connection in connections:
                key = f"live:{event.id}:{rule.id}:{channel_type}:{connection.id if connection else 'dashboard'}"
                existing = db.scalar(
                    select(LiveEventDelivery).where(LiveEventDelivery.idempotency_key == key)
                )
                if existing:
                    created.append(existing)
                    continue
                blocked_reason = None
                if paused or globally_paused:
                    blocked_reason = "Live-Verteilung ist pausiert"
                elif channel_type in {"instagram", "facebook"}:
                    blocked_reason = (
                        "Der kanalbezogene, versionierte Medienauftrag muss zuerst "
                        "im bestehenden Beitragsworkflow erzeugt werden"
                    )
                elif connection and (
                    not connection.publishing_enabled or not connection.automatic_delivery_enabled
                ):
                    blocked_reason = "Kanal ist für automatische Verteilung deaktiviert"
                elif channel_type == "whatsapp" and (audience is None or not audience.active):
                    blocked_reason = "WhatsApp-Zielgruppe fehlt oder ist deaktiviert"
                elif (
                    channel_type == "whatsapp"
                    and connection
                    and (audience.channel_connection_id != connection.id)
                ):
                    blocked_reason = "WhatsApp-Zielgruppe gehört zu einer anderen Verbindung"
                elif (
                    channel_type == "whatsapp"
                    and audience.audience_type == "group"
                    and (
                        "groups" not in (connection.capabilities or [])
                        or audience.eligibility_status != "available"
                        or not audience.external_group_id
                    )
                ):
                    blocked_reason = "Offizielle WhatsApp-Gruppe ist nicht verfügbar"
                elif channel_type == "whatsapp" and audience.audience_type == "recipient_list":
                    eligible_recipients = db.scalar(
                        select(func.count(WhatsAppRecipient.id))
                        .join(
                            WhatsAppAudienceRecipient,
                            WhatsAppAudienceRecipient.recipient_id == WhatsAppRecipient.id,
                        )
                        .where(
                            WhatsAppAudienceRecipient.audience_id == audience.id,
                            WhatsAppRecipient.active.is_(True),
                            WhatsAppRecipient.opt_in_status == "confirmed",
                        )
                    )
                    if not eligible_recipients:
                        blocked_reason = "Empfängerliste enthält keine aktiven Opt-ins"
                automatic = rule.delivery_mode == "automatic" and not rule.require_confirmation
                status = (
                    "delivered"
                    if channel_type == "dashboard"
                    else "blocked"
                    if blocked_reason
                    else "queued"
                    if automatic
                    else "awaiting_approval"
                )
                delivery = LiveEventDelivery(
                    event_id=event.id,
                    rule_id=rule.id,
                    channel_type=channel_type,
                    channel_connection_id=connection.id if connection else None,
                    whatsapp_audience_id=(
                        audience.id if channel_type == "whatsapp" and audience else None
                    ),
                    status=status,
                    target=(
                        audience.external_group_id
                        if audience and audience.audience_type == "group"
                        else audience.id
                        if audience
                        else rule.audience_type
                    ),
                    message_snapshot=_live_message(event, game),
                    last_error=blocked_reason,
                    idempotency_key=key,
                    delivered_at=now() if channel_type == "dashboard" else None,
                )
                db.add(delivery)
                created.append(delivery)
    return created


def _record_ai_parse_usage(
    db: Session,
    *,
    club_id: str,
    provider_event_id: str,
    settings: Settings,
    success: bool,
) -> None:
    key = f"live-parse:{provider_event_id}"
    if db.scalar(select(UsageLedgerEntry.id).where(UsageLedgerEntry.idempotency_key == key)):
        return
    start, end = billing_period()
    db.add(
        UsageLedgerEntry(
            id=uid(),
            club_id=club_id,
            generation_type="live_event_parsing",
            provider="openai",
            model=settings.live_event_ai_model,
            period_start=start,
            period_end=end,
            status=(
                UsageStatus.COMPLETED_NOT_BILLABLE if success else UsageStatus.FAILED_TECHNICAL
            ),
            reserved_quantity=0,
            actual_quantity=1 if success else 0,
            billable=False,
            platform_test=False,
            idempotency_key=key,
            details={"purpose": "live_event_parsing"},
        )
    )


def _candidate_game(
    db: Session,
    reporter: LiveReporter,
    settings: Settings,
) -> Game:
    expires_at = reporter.active_game_expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if reporter.active_game_id and (expires_at is None or expires_at >= now()):
        game = db.get(Game, reporter.active_game_id)
        if game and reporter_can_access_team(db, reporter, game.team_id):
            return game
        raise LiveEventError("Aktives Spiel des Reporters ist nicht mehr verfügbar")
    if reporter.active_game_id:
        reporter.active_game_id = None
        reporter.active_game_expires_at = None
    current = now()
    earliest = current - timedelta(minutes=settings.live_event_game_window_after_minutes)
    latest = current + timedelta(minutes=settings.live_event_game_window_before_minutes)
    candidates = list(
        db.scalars(
            select(Game)
            .where(
                Game.kickoff.between(earliest, latest),
                Game.status.not_in({"cancelled", "postponed"}),
            )
            .order_by(Game.kickoff)
        )
    )
    candidates = [
        game for game in candidates if reporter_can_access_team(db, reporter, game.team_id)
    ]
    if len(candidates) != 1:
        raise LiveEventError("Aktives Spiel ist nicht eindeutig; bitte im Live Center auswählen")
    return candidates[0]


def ingest_whatsapp_message(
    db: Session,
    *,
    connection: SocialChannelConnection,
    provider_message_id: str,
    sender: str,
    text: str,
    settings: Settings,
) -> IngestResult:
    if not settings.live_center_enabled:
        return IngestResult("disabled", message="Live Center ist nicht aktiviert")
    club = db.get(Club, connection.club_id)
    if club is None or club.status not in {ClubStatus.ACTIVE, ClubStatus.TRIAL}:
        return IngestResult("disabled", message="Live Center ist für diesen Verein pausiert")
    if not connection.active or connection.status != "connected":
        return IngestResult("disabled", message="WhatsApp-Verbindung ist nicht aktiv")
    try:
        phone = normalize_phone(sender)
    except LiveEventError as exc:
        return IngestResult("rejected", message=str(exc))
    reporter = db.scalar(
        select(LiveReporter).where(
            LiveReporter.channel_connection_id == connection.id,
            LiveReporter.normalized_phone == phone,
            LiveReporter.active.is_(True),
        )
    )
    if reporter is None:
        return IngestResult(
            "unknown_reporter", message="Absender ist nicht als Reporter freigegeben"
        )
    since = now() - timedelta(minutes=1)
    recent = db.scalar(
        select(func.count(MatchEvent.id)).where(
            MatchEvent.reporter_id == reporter.id,
            MatchEvent.created_at >= since,
        )
    )
    if int(recent or 0) >= settings.live_event_reporter_rate_limit_per_minute:
        return IngestResult("rate_limited", message="Zu viele Meldungen in kurzer Zeit")
    try:
        game = _candidate_game(db, reporter, settings)
    except LiveEventError as exc:
        return IngestResult("manual_review", message=str(exc))
    ai_parser = None
    if settings.live_event_ai_parsing_enabled and settings.openai_api_key:
        ai_parser = OpenAIMatchEventParser(settings.openai_api_key, settings.live_event_ai_model)
    parser = WhatsAppMatchEventProvider(ai_parser)
    try:
        parsed = parser.interpret(text)
    except Exception:
        if parser.used_ai:
            _record_ai_parse_usage(
                db,
                club_id=connection.club_id,
                provider_event_id=provider_message_id,
                settings=settings,
                success=False,
            )
        return IngestResult(
            "manual_review", message="Meldung konnte nicht sicher interpretiert werden"
        )
    if parser.used_ai:
        _record_ai_parse_usage(
            db,
            club_id=connection.club_id,
            provider_event_id=provider_message_id,
            settings=settings,
            success=True,
        )
    if parsed is None:
        return IngestResult(
            "manual_review", message="Meldung konnte nicht eindeutig zugeordnet werden"
        )
    try:
        event = create_match_event(
            db,
            game=game,
            parsed=parsed,
            provider="whatsapp",
            idempotency_key=f"whatsapp:{provider_message_id}",
            reporter=reporter,
            channel_connection_id=connection.id,
            provider_event_id=provider_message_id,
            raw_text=text,
            source_sender_id=phone,
        )
    except LiveEventError as exc:
        reporter.last_seen_at = now()
        return IngestResult("manual_review", message=str(exc))
    reporter.last_seen_at = now()
    return IngestResult(event.status, event=event)


def reconcile_official_result(
    db: Session,
    *,
    game: Game,
    home_score: int,
    away_score: int,
    provider_event_id: str,
) -> MatchEvent | None:
    state = get_or_create_state(db, game)
    if (
        state.home_score == home_score
        and state.away_score == away_score
        and state.phase == "finished"
    ):
        return None
    parsed = ParsedMatchEvent(
        event_type="score_correction",
        home_score_after=home_score,
        away_score_after=away_score,
        reason="Abgleich mit bestätigtem offiziellem Endergebnis",
        confidence=1,
    )
    return create_match_event(
        db,
        game=game,
        parsed=parsed,
        provider=game.provider,
        provider_event_id=provider_event_id,
        idempotency_key=f"official-result:{game.id}:{provider_event_id}:{home_score}:{away_score}",
        force_confirmed=True,
    )
