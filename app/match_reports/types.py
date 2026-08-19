from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class FupaTickerItem:
    source_id: str
    event_type: str
    minute: int | None = None
    text: str = ""
    team: str | None = None
    player: str | None = None
    home_score: int | None = None
    away_score: int | None = None


@dataclass(frozen=True)
class FupaReadResult:
    source_url: str
    fetch_status: str
    structured_data: dict[str, Any] = field(default_factory=dict)
    ticker: tuple[FupaTickerItem, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    content_digest: str = ""
    error_category: str | None = None
    error: str | None = None

    def ticker_json(self) -> list[dict[str, Any]]:
        return [asdict(item) for item in self.ticker]


@dataclass(frozen=True)
class ContentConflict:
    field: str
    values: dict[str, Any]
    message: str
    blocking: bool = True


@dataclass(frozen=True)
class MatchContentContext:
    club_id: str
    game_id: str
    team_id: str
    facts: dict[str, Any]
    events: tuple[dict[str, Any], ...]
    feedback: tuple[dict[str, Any], ...]
    manual_notes: tuple[dict[str, Any], ...]
    writing_examples: tuple[dict[str, Any], ...]
    branding: dict[str, Any]
    provenance: dict[str, Any]
    conflicts: tuple[ContentConflict, ...]
    built_at: datetime

    @property
    def has_blocking_conflicts(self) -> bool:
        return any(item.blocking for item in self.conflicts)

    def as_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "built_at": self.built_at.isoformat(),
        }


@dataclass(frozen=True)
class GeneratedMatchReport:
    headline: str
    teaser: str | None
    body: str
    used_sources: tuple[str, ...]
    omitted_sources: tuple[str, ...] = ()
    model: str | None = None
    prompt_template_id: str = "match-report-system"
    prompt_version: int = 1
