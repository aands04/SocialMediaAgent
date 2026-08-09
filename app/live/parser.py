from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Protocol

from openai import OpenAI


class MatchEventParseError(ValueError):
    pass


EVENT_TYPES = {
    "kickoff",
    "goal",
    "opponent_goal",
    "own_goal",
    "penalty_scored",
    "penalty_missed",
    "yellow_card",
    "second_yellow_card",
    "red_card",
    "substitution",
    "halftime",
    "second_half",
    "fulltime",
    "interruption",
    "resume",
    "abandoned",
    "comment",
    "score_correction",
    "event_correction",
}
PARSED_EVENT_TYPES = EVENT_TYPES | {"score_update"}


@dataclass(frozen=True, slots=True)
class ParsedMatchEvent:
    event_type: str
    minute: int | None = None
    stoppage_minute: int | None = None
    home_score_after: int | None = None
    away_score_after: int | None = None
    player_name: str | None = None
    assist_name: str | None = None
    related_player_name: str | None = None
    reason: str | None = None
    comment: str | None = None
    confidence: float = 1.0
    parser: str = "deterministic"

    def validated(self) -> ParsedMatchEvent:
        if self.event_type not in PARSED_EVENT_TYPES:
            raise MatchEventParseError("Unbekannter Ereignistyp")
        if self.minute is not None and not 0 <= self.minute <= 150:
            raise MatchEventParseError("Spielminute liegt außerhalb des erlaubten Bereichs")
        if self.stoppage_minute is not None and not 0 <= self.stoppage_minute <= 30:
            raise MatchEventParseError("Nachspielzeit liegt außerhalb des erlaubten Bereichs")
        for score in (self.home_score_after, self.away_score_after):
            if score is not None and not 0 <= score <= 99:
                raise MatchEventParseError("Spielstand liegt außerhalb des erlaubten Bereichs")
        if not 0 <= self.confidence <= 1:
            raise MatchEventParseError("Konfidenz liegt außerhalb des erlaubten Bereichs")
        return self

    def as_dict(self) -> dict:
        return asdict(self)


class MatchEventAiParser(Protocol):
    def parse(self, text: str) -> ParsedMatchEvent | None: ...


_SPACE = re.compile(r"\s+")
_MINUTE = re.compile(
    r"(?<![\d:])(?P<minute>\d{1,3})(?:\s*\+\s*(?P<extra>\d{1,2}))?"
    r"(?!\s*[:\-]\s*\d)\s*\.?\s*(?:min(?:ute)?\.?|')?",
    re.I,
)
_SCORE = re.compile(r"(?<!\d)(?P<home>\d{1,2})\s*[:\-]\s*(?P<away>\d{1,2})(?!\d)")


def sanitize_message_text(value: str) -> str:
    clean = "".join(character for character in value if character in "\n\t" or ord(character) >= 32)
    return _SPACE.sub(" ", clean).strip()[:500]


def _minute(text: str) -> tuple[int | None, int | None]:
    match = _MINUTE.search(text)
    if not match:
        return None, None
    minute = int(match.group("minute"))
    extra = int(match.group("extra")) if match.group("extra") else None
    if minute > 150:
        return None, None
    return minute, extra


def _score(text: str) -> tuple[int | None, int | None]:
    match = _SCORE.search(text)
    if not match:
        return None, None
    return int(match.group("home")), int(match.group("away"))


def _remaining_name(text: str, *tokens: str) -> str | None:
    clean = text
    for token in tokens:
        clean = re.sub(re.escape(token), " ", clean, flags=re.I)
    clean = _SCORE.sub(" ", clean)
    clean = _MINUTE.sub(" ", clean)
    clean = re.sub(r"[^\wÄÖÜäöüß .'-]", " ", clean)
    clean = _SPACE.sub(" ", clean).strip(" .-")
    return clean[:160] or None


def parse_match_event(text: str) -> ParsedMatchEvent | None:
    """Parse common German touchline messages without invoking an AI provider."""

    clean = sanitize_message_text(text)
    folded = clean.casefold()
    if not clean:
        return None
    home, away = _score(clean)
    minute_source = _SCORE.sub(" ", clean)
    minute_source = re.sub(
        r"\b((?:2\.?|zweite)\s+halbzeit|wiederanpfiff)\b",
        " ",
        minute_source,
        flags=re.I,
    )
    minute, extra = _minute(minute_source)

    if re.search(r"\b(korrektur|korrigiere)\b", folded) and home is not None:
        return ParsedMatchEvent(
            "score_correction",
            minute,
            extra,
            home,
            away,
            reason="Manuell gemeldete Spielstandskorrektur",
        ).validated()
    if re.search(r"\b(abpfiff|ende|endstand|schluss)\b", folded):
        return ParsedMatchEvent("fulltime", minute, extra, home, away).validated()
    if re.search(r"\b((?:2\.?|zweite) halbzeit|wiederanpfiff)\b", folded):
        return ParsedMatchEvent("second_half", minute, extra).validated()
    if re.search(r"\b(halbzeit|pause|hz)\b", folded):
        return ParsedMatchEvent("halftime", minute, extra, home, away).validated()
    if re.search(r"\b(anpfiff|los geht'?s|spiel beginnt)\b", folded):
        return ParsedMatchEvent("kickoff", minute, extra).validated()
    if re.search(r"\b(abbruch|abgebrochen)\b", folded):
        return ParsedMatchEvent("abandoned", minute, extra, reason=clean).validated()
    if re.search(r"\b(unterbrechung|unterbrochen)\b", folded):
        return ParsedMatchEvent("interruption", minute, extra, reason=clean).validated()
    if re.search(r"\b(fortsetzung|weiter geht'?s|wiederaufnahme)\b", folded):
        return ParsedMatchEvent("resume", minute, extra).validated()

    substitution = re.search(
        r"\bwechsel\b(?:\s+\d{1,3})?\s+(?P<incoming>.+?)\s+(?:für|fuer)\s+(?P<outgoing>.+)$",
        clean,
        re.I,
    )
    if substitution:
        return ParsedMatchEvent(
            "substitution",
            minute,
            extra,
            player_name=substitution.group("incoming").strip()[:160],
            related_player_name=substitution.group("outgoing").strip()[:160],
        ).validated()

    event_type = None
    token = ""
    if re.search(r"\b(gelb[ -]?rot|zweite gelbe)\b", folded):
        event_type, token = "second_yellow_card", "gelb-rot"
    elif re.search(r"\brote? karte|\brot\b", folded):
        event_type, token = "red_card", "rot"
    elif re.search(r"\bgelbe? karte|\bgelb\b", folded):
        event_type, token = "yellow_card", "gelb"
    elif re.search(r"\belfmeter\b.*\b(verschossen|vorbei|gehalten)\b", folded):
        event_type, token = "penalty_missed", "elfmeter"
    elif re.search(r"\belfmeter\b", folded):
        event_type, token = "penalty_scored", "elfmeter"
    elif re.search(r"\b(eigentor)\b", folded):
        event_type, token = "own_goal", "eigentor"
    elif re.search(r"\b(gegentor|tor gegner|gegner trifft)\b", folded):
        event_type, token = "opponent_goal", "gegentor"
    elif re.search(r"\b(tor|treffer)\b", folded):
        event_type, token = "goal", "tor"
    if event_type:
        return ParsedMatchEvent(
            event_type,
            minute,
            extra,
            home,
            away,
            player_name=_remaining_name(
                clean, token, "karte", "treffer", "minute", "in der", "durch"
            ),
        ).validated()
    if home is not None and away is not None:
        return ParsedMatchEvent(
            "score_update",
            minute,
            extra,
            home,
            away,
            player_name=_remaining_name(clean, "durch", "macht", "das", "minute", "min"),
        ).validated()
    return None


class OpenAIMatchEventParser:
    """Optional strict fallback. The system prompt is never returned to clients or logs."""

    def __init__(self, api_key: str, model: str):
        self.client = OpenAI(api_key=api_key)
        self.model = model

    def parse(self, text: str) -> ParsedMatchEvent | None:
        clean = sanitize_message_text(text)
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "event_type",
                "minute",
                "stoppage_minute",
                "home_score_after",
                "away_score_after",
                "player_name",
                "assist_name",
                "related_player_name",
                "reason",
                "comment",
                "confidence",
            ],
            "properties": {
                "event_type": {"type": ["string", "null"], "enum": [*sorted(EVENT_TYPES), None]},
                "minute": {"type": ["integer", "null"], "minimum": 0, "maximum": 150},
                "stoppage_minute": {"type": ["integer", "null"], "minimum": 0, "maximum": 30},
                "home_score_after": {"type": ["integer", "null"], "minimum": 0, "maximum": 99},
                "away_score_after": {"type": ["integer", "null"], "minimum": 0, "maximum": 99},
                "player_name": {"type": ["string", "null"], "maxLength": 160},
                "assist_name": {"type": ["string", "null"], "maxLength": 160},
                "related_player_name": {"type": ["string", "null"], "maxLength": 160},
                "reason": {"type": ["string", "null"], "maxLength": 250},
                "comment": {"type": ["string", "null"], "maxLength": 500},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
        }
        response = self.client.responses.create(
            model=self.model,
            input=(
                "Ordne genau eine kurze deutsche Fußball-Livemeldung einem erlaubten "
                "Ereignistyp zu. Erfinde keine Namen, Spielstände oder Minuten. "
                "Unklare Meldung: event_type=null. Meldung: "
                + json.dumps(clean, ensure_ascii=False)
            ),
            text={
                "format": {
                    "type": "json_schema",
                    "name": "match_event",
                    "strict": True,
                    "schema": schema,
                }
            },
        )
        value = json.loads(response.output_text)
        if value.get("event_type") is None:
            return None
        value["parser"] = "openai"
        return ParsedMatchEvent(**value).validated()
