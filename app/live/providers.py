from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.live.parser import MatchEventAiParser, ParsedMatchEvent, parse_match_event


@dataclass(frozen=True, slots=True)
class ProviderEvent:
    provider_event_id: str
    parsed: ParsedMatchEvent
    raw_digest: str


class MatchEventProvider(ABC):
    name: str

    @abstractmethod
    def interpret(self, text: str) -> ParsedMatchEvent | None: ...


class ManualMatchEventProvider(MatchEventProvider):
    name = "dashboard"

    def interpret(self, text: str) -> ParsedMatchEvent | None:
        return parse_match_event(text)


class WhatsAppMatchEventProvider(MatchEventProvider):
    name = "whatsapp"

    def __init__(self, ai_parser: MatchEventAiParser | None = None):
        self.ai_parser = ai_parser
        self.used_ai = False

    def interpret(self, text: str) -> ParsedMatchEvent | None:
        self.used_ai = False
        parsed = parse_match_event(text)
        if parsed is not None or self.ai_parser is None:
            return parsed
        self.used_ai = True
        return self.ai_parser.parse(text)


class FussballDeMatchEventProvider(MatchEventProvider):
    name = "fussball.de"

    def interpret(self, text: str) -> ParsedMatchEvent | None:
        raise NotImplementedError("Keine offizielle Live-Ereignisschnittstelle konfiguriert")


class FuPaMatchEventProvider(MatchEventProvider):
    name = "fupa"

    def interpret(self, text: str) -> ParsedMatchEvent | None:
        raise NotImplementedError("Keine offizielle Live-Ereignisschnittstelle konfiguriert")
