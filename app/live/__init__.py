"""Tenant-safe live match events and controlled event distribution."""

from app.live.parser import ParsedMatchEvent, parse_match_event

__all__ = ["ParsedMatchEvent", "parse_match_event"]
