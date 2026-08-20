from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.match_reports.types import ContentConflict, MatchContentContext
from app.models import (
    Club,
    ClubBrandingConfiguration,
    ClubWritingExample,
    FupaMatchSnapshot,
    Game,
    MatchEvent,
    MatchFeedbackRequest,
    MatchFeedbackResponse,
    MatchManualNote,
    Team,
)


class MatchContextError(RuntimeError):
    pass


def _score(value: Any) -> tuple[int, int] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        left, right = int(value[0]), int(value[1])
    except (TypeError, ValueError):
        return None
    return (left, right) if left >= 0 and right >= 0 else None


def _writing_category(game: Game, team: Team) -> str:
    competition = (game.competition or "").casefold()
    if "pokal" in competition or "cup" in competition:
        return "cup"
    if "freundschaft" in competition or "testspiel" in competition:
        return "friendly"
    if game.home_score is None or game.away_score is None:
        return "general"
    own_is_home = game.home_team.casefold() in {
        team.display_name.casefold(),
        (team.short_name or "").casefold(),
    }
    own, opponent = (
        (game.home_score, game.away_score) if own_is_home else (game.away_score, game.home_score)
    )
    return "win" if own > opponent else "loss" if own < opponent else "draw"


def build_match_content_context(db: Session, game_id: str) -> MatchContentContext:
    game = db.get(Game, game_id)
    if game is None:
        raise MatchContextError("Spiel nicht gefunden")
    team = db.get(Team, game.team_id)
    club = db.get(Club, game.club_id)
    if team is None or team.club_id != game.club_id or club is None:
        raise MatchContextError("Spiel besitzt keine eindeutige Vereins- und Mannschaftszuordnung")

    snapshot = db.scalar(
        select(FupaMatchSnapshot)
        .where(
            FupaMatchSnapshot.club_id == game.club_id,
            FupaMatchSnapshot.game_id == game.id,
            FupaMatchSnapshot.fetch_status == "success",
        )
        .order_by(desc(FupaMatchSnapshot.fetched_at))
    )
    structured = snapshot.structured_data if snapshot else {}
    ticker = snapshot.ticker_data if snapshot else []
    events = list(
        db.scalars(
            select(MatchEvent)
            .where(
                MatchEvent.club_id == game.club_id,
                MatchEvent.game_id == game.id,
                MatchEvent.status == "confirmed",
            )
            .order_by(MatchEvent.event_sequence)
        )
    )
    notes = list(
        db.scalars(
            select(MatchManualNote)
            .where(
                MatchManualNote.club_id == game.club_id,
                MatchManualNote.game_id == game.id,
            )
            .order_by(MatchManualNote.created_at)
        )
    )
    requests = list(
        db.scalars(
            select(MatchFeedbackRequest)
            .where(
                MatchFeedbackRequest.club_id == game.club_id,
                MatchFeedbackRequest.game_id == game.id,
            )
            .order_by(MatchFeedbackRequest.created_at)
        )
    )
    request_ids = [item.id for item in requests]
    responses = (
        list(
            db.scalars(
                select(MatchFeedbackResponse)
                .where(
                    MatchFeedbackResponse.club_id == game.club_id,
                    MatchFeedbackResponse.request_id.in_(request_ids),
                )
                .order_by(MatchFeedbackResponse.received_at)
            )
        )
        if request_ids
        else []
    )

    structured_score = _score([structured.get("home_score"), structured.get("away_score")])
    ticker_score = None
    for item in reversed(ticker):
        if str(item.get("event_type") or "").casefold() != "fulltime":
            continue
        candidate = _score([item.get("home_score"), item.get("away_score")])
        if candidate:
            ticker_score = candidate
            break
    confirmed_event_score = None
    for item in reversed(events):
        candidate = _score([item.home_score_after, item.away_score_after])
        if candidate:
            confirmed_event_score = candidate
            break
    game_score = _score([game.home_score, game.away_score]) if game.result_confirmed else None

    conflicts: list[ContentConflict] = []
    score_candidates = {
        key: value
        for key, value in {
            "fupa_strukturiert": structured_score,
            "fupa_ticker": ticker_score,
            "bestaetigte_live_daten": confirmed_event_score,
            "spielstamm": game_score,
        }.items()
        if value is not None
    }
    if len(set(score_candidates.values())) > 1:
        conflicts.append(
            ContentConflict(
                field="score",
                values={key: list(value) for key, value in score_candidates.items()},
                message="Die Ergebnisquellen widersprechen sich. Vor der Freigabe ist eine Prüfung erforderlich.",
            )
        )
    final_score = structured_score or ticker_score or confirmed_event_score or game_score
    if final_score is None:
        conflicts.append(
            ContentConflict(
                field="score",
                values={},
                message="Es liegt noch kein belastbarer Endstand vor.",
            )
        )

    facts = {
        "club_name": club.name,
        "club_short_name": club.short_name,
        "team_name": team.display_name,
        "home_team": structured.get("home_team") or game.home_team,
        "away_team": structured.get("away_team") or game.away_team,
        "home_score": final_score[0] if final_score else None,
        "away_score": final_score[1] if final_score else None,
        "kickoff": structured.get("kickoff")
        or (game.kickoff.isoformat() if game.kickoff else None),
        "competition": structured.get("competition") or game.competition,
        "venue": structured.get("venue") or game.venue,
        "result_confirmed": final_score is not None,
        "source_url": snapshot.source_url if snapshot else game.fupa_url,
    }
    ticker_event_payload = tuple(
        {
            "source_id": f"fupa-ticker:{item.get('source_id') or index}",
            "provider": "fupa",
            "type": item.get("event_type"),
            "minute": item.get("minute"),
            "team": item.get("team"),
            "player": item.get("player"),
            "score": [item.get("home_score"), item.get("away_score")],
            "comment": item.get("text"),
        }
        for index, item in enumerate(ticker, start=1)
        if isinstance(item, dict)
    )
    live_event_payload = tuple(
        {
            "source_id": f"live:{item.id}",
            "provider": item.provider,
            "type": item.event_type,
            "minute": item.minute,
            "team_side": item.team_side,
            "player": item.player_name,
            "assist": item.assist_name,
            "score": [item.home_score_after, item.away_score_after],
            "comment": item.comment,
        }
        for item in events
    )
    event_payload = ticker_event_payload + live_event_payload
    feedback_payload = tuple(
        {
            "source_id": f"{item.provider}:{item.id}",
            "provider": item.provider,
            "source_role": item.source_role or f"{item.provider}_trainer",
            "payload_type": item.payload_type,
            "payload_metadata": item.payload_metadata,
            "body": item.body,
            "received_at": item.received_at.isoformat(),
        }
        for item in responses
    )
    note_payload = tuple(
        {
            "source_id": f"manual:{item.id}",
            "body": item.body,
            "confirmed_facts": item.confirmed_facts,
        }
        for item in notes
    )

    category = _writing_category(game, team)
    examples = list(
        db.scalars(
            select(ClubWritingExample)
            .where(
                ClubWritingExample.club_id == game.club_id,
                ClubWritingExample.active.is_(True),
                ClubWritingExample.category.in_([category, "general"]),
                (ClubWritingExample.team_id == team.id) | (ClubWritingExample.team_id.is_(None)),
            )
            .order_by(
                (ClubWritingExample.team_id == team.id).desc(),
                (ClubWritingExample.category == category).desc(),
                desc(ClubWritingExample.updated_at),
            )
            .limit(3)
        )
    )
    branding = db.get(ClubBrandingConfiguration, club.id)
    return MatchContentContext(
        club_id=club.id,
        game_id=game.id,
        team_id=team.id,
        facts=facts,
        events=event_payload,
        feedback=feedback_payload,
        manual_notes=note_payload,
        writing_examples=tuple(
            {"category": item.category, "title": item.title, "body": item.body} for item in examples
        ),
        branding={
            "text_settings": branding.text_settings if branding else {},
        },
        provenance={
            "fact_priority": [
                "fupa_structured",
                "fupa_ticker",
                "confirmed_manual",
                "messenger_feedback",
            ],
            "snapshot_id": snapshot.id if snapshot else None,
            # FuPa's authenticated editor is addressed by this numeric ID.
            # Keep it in provenance instead of user-facing facts so it cannot
            # accidentally become part of the editorial AI prompt.
            "fupa_match_id": (
                snapshot.source_metadata.get("match_id")
                if snapshot and isinstance(snapshot.source_metadata, dict)
                else None
            ),
            "score_candidates": {key: list(value) for key, value in score_candidates.items()},
        },
        conflicts=tuple(conflicts),
        built_at=datetime.now(timezone.utc),
    )
