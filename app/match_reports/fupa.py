from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.match_reports.types import FupaReadResult, FupaTickerItem

_MAX_BYTES = 2_000_000
_ALLOWED_HOSTS = {"fupa.net", "www.fupa.net"}
_SCORE_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[:\-]\s*(\d{1,2})(?!\d)")
_MINUTE_RE = re.compile(r"(?<!\d)(\d{1,3})(?:\+\d{1,2})?\s*[.'’]")
_REDUX_DATA_RE = re.compile(r"(?:window\.)?REDUX_DATA\s*=\s*")


class FupaReadError(RuntimeError):
    pass


def validate_fupa_url(value: str) -> str:
    parsed = urlparse(value.strip())
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Die FuPa-Spiel-URL enthält keinen gültigen Port") from exc
    if (
        parsed.scheme != "https"
        or host not in _ALLOWED_HOSTS
        or parsed.username
        or parsed.password
        or port not in {None, 443}
    ):
        raise ValueError("Es ist ausschließlich eine HTTPS-Spiel-URL von fupa.net erlaubt")
    return parsed.geturl()


def _reject_private_resolution(host: str) -> None:
    for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM):
        address = ipaddress.ip_address(item[4][0])
        if not address.is_global:
            raise FupaReadError("FuPa-Adresse löst auf ein nicht öffentliches Netz auf")


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _name(value: Any) -> str | None:
    """Return a human-readable name from FuPa's changing nested structures."""

    if value is None:
        return None
    if isinstance(value, dict):
        # Current FuPa match data uses structures such as
        # ``{"name": {"full": "TSV Carlsdorf", "short": "Carls"}}``.
        # Resolve these values recursively instead of comparing the nested
        # dictionary with a set (which raises ``unhashable type: 'dict'``).
        for key in ("full", "displayName", "name", "title", "label", "middle", "short"):
            resolved = _name(value.get(key))
            if resolved:
                return resolved
        person = " ".join(
            part for key in ("firstName", "lastName") if (part := _name(value.get(key)))
        ).strip()
        return person or None
    if isinstance(value, (list, tuple, set)):
        return None
    text = str(value).strip()
    return text or None


def _parse_datetime(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.isoformat()
    except ValueError:
        return text[:100]


def _event_type(text: str) -> str:
    folded = text.casefold()
    if any(word in folded for word in ("abpfiff", "spielende", "endstand")):
        return "fulltime"
    if any(word in folded for word in ("halbzeit", "pause")):
        return "halftime"
    if any(word in folded for word in ("rote karte", "platzverweis")):
        return "red_card"
    if any(word in folded for word in ("gelbe karte", "verwarnung")):
        return "yellow_card"
    if any(word in folded for word in ("wechsel", "auswechslung", "eingewechselt")):
        return "substitution"
    if any(word in folded for word in ("tor", "treffer", "elfmeter verwandelt")):
        return "goal"
    if any(word in folded for word in ("anpfiff", "spielbeginn")):
        return "kickoff"
    return "comment"


def _ticker_item(raw: dict[str, Any], index: int) -> FupaTickerItem | None:
    text = " ".join(
        str(raw.get(key) or "").strip()
        for key in ("title", "text", "description", "comment", "message")
    ).strip()
    provider_type = str(raw.get("type") or raw.get("eventType") or "").casefold()
    provider_subtype = str(raw.get("subtype") or "").casefold()
    inferred_types = {
        "goal": ("goal", "Tor"),
        "yellow_card": ("yellow_card", "Gelbe Karte"),
        "yellowcard": ("yellow_card", "Gelbe Karte"),
        "red_card": ("red_card", "Rote Karte"),
        "redcard": ("red_card", "Rote Karte"),
        "substitution": ("substitution", "Auswechslung"),
        "kickoff": ("kickoff", "Anpfiff"),
        "halftime": ("halftime", "Halbzeit"),
        "fulltime": ("fulltime", "Abpfiff"),
    }
    inferred = inferred_types.get(provider_type) or inferred_types.get(provider_subtype)
    player = _name(
        raw.get("player") or raw.get("person") or raw.get("primaryRole") or raw.get("primaryPerson")
    )
    explicit_home_score = raw.get("homeScore")
    if explicit_home_score is None:
        explicit_home_score = raw.get("homeGoal")
    explicit_away_score = raw.get("awayScore")
    if explicit_away_score is None:
        explicit_away_score = raw.get("awayGoal")
    if not text and inferred:
        text_parts = [inferred[1]]
        if player:
            text_parts.append(player)
        if explicit_home_score is not None and explicit_away_score is not None:
            text_parts.append(f"{explicit_home_score}:{explicit_away_score}")
        text = " – ".join(text_parts)
    if not text:
        return None
    minute_value = raw.get("minute") or raw.get("matchMinute")
    minute_match = _MINUTE_RE.search(text)
    try:
        minute = int(minute_value) if minute_value is not None else None
    except (TypeError, ValueError):
        minute = None
    if minute is None and minute_match:
        minute = int(minute_match.group(1))
    source_id = str(raw.get("id") or raw.get("eventId") or f"ticker-{index}")[:200]
    score = _SCORE_RE.search(text)

    def _score(value: Any, fallback: int | None) -> int | None:
        try:
            return int(value) if value is not None else fallback
        except (TypeError, ValueError):
            return fallback

    return FupaTickerItem(
        source_id=source_id,
        event_type=inferred[0] if inferred else _event_type(text),
        minute=minute,
        text=text[:1000],
        team=_name(raw.get("team") or raw.get("club") or raw.get("teamName")),
        player=player,
        home_score=_score(explicit_home_score, int(score.group(1)) if score else None),
        away_score=_score(explicit_away_score, int(score.group(2)) if score else None),
    )


def _redux_document(script_text: str) -> Any | None:
    """Decode FuPa's current Redux bootstrap without evaluating JavaScript."""

    marker = _REDUX_DATA_RE.search(script_text)
    if marker is None:
        return None
    try:
        document, _ = json.JSONDecoder().raw_decode(script_text[marker.end() :].lstrip())
    except (json.JSONDecodeError, TypeError):
        return None
    return document


def _redux_match_page(document: Any) -> dict[str, Any] | None:
    for item in _walk(document):
        match_info = item.get("matchInfo")
        if isinstance(match_info, dict) and any(
            key in match_info for key in ("homeTeamName", "awayTeamName", "kickoff")
        ):
            return item
    return None


def _redux_ticker_candidates(match_page: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for key in ("ticker", "tickerEvents", "events", "matchTicker", "matchEvents"):
        value = match_page.get(key)
        if value is None:
            continue
        for item in _walk(value):
            provider_type = str(item.get("type") or item.get("eventType") or "").casefold()
            if provider_type in {
                "goal",
                "yellow_card",
                "yellowcard",
                "red_card",
                "redcard",
                "substitution",
                "kickoff",
                "halftime",
                "fulltime",
            } or any(key in item for key in ("text", "comment", "message", "title")):
                candidates.append(item)
    return candidates


def _extract_json(soup: BeautifulSoup) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    match: dict[str, Any] = {}
    ticker: list[dict[str, Any]] = []
    documents: list[Any] = []
    for script in soup.find_all("script"):
        script_type = (script.get("type") or "").lower()
        script_id = script.get("id") or ""
        script_text = script.string or script.get_text() or ""
        redux = _redux_document(script_text)
        if redux is not None:
            documents.append(redux)
        if script_type != "application/ld+json" and script_id != "__NEXT_DATA__":
            continue
        try:
            documents.append(json.loads(script_text or "null"))
        except json.JSONDecodeError:
            continue
    for document in documents:
        match_page = _redux_match_page(document)
        if match_page is not None:
            match = match_page["matchInfo"]
            ticker.extend(_redux_ticker_candidates(match_page))
            continue
        for item in _walk(document):
            item_type = str(item.get("@type") or item.get("type") or "").casefold()
            if not match and item_type in {"sportsevent", "game", "match"}:
                match = item
            if any(key in item for key in ("minute", "matchMinute")) and any(
                key in item for key in ("text", "comment", "message", "title", "description")
            ):
                ticker.append(item)
    return match, ticker


def parse_fupa_html(source_url: str, html: str, *, status_code: int = 200) -> FupaReadResult:
    source_url = validate_fupa_url(source_url)
    soup = BeautifulSoup(html, "html.parser")
    match, raw_ticker = _extract_json(soup)
    home = _name(match.get("homeTeam") or match.get("home") or match.get("homeTeamName"))
    away = _name(match.get("awayTeam") or match.get("away") or match.get("awayTeamName"))
    home_score = match.get("homeScore")
    if home_score is None:
        home_score = match.get("homeGoal")
    away_score = match.get("awayScore")
    if away_score is None:
        away_score = match.get("awayGoal")
    items = tuple(
        item
        for index, raw in enumerate(raw_ticker)
        if (item := _ticker_item(raw, index)) is not None
    )
    # A score found somewhere in the rendered page may be a kickoff time,
    # another fixture or an advertisement. Only an explicitly structured
    # score or the final-score ticker event is trustworthy enough here.
    if home_score is None or away_score is None:
        final_ticker = next(
            (
                item
                for item in reversed(items)
                if item.event_type == "fulltime"
                and item.home_score is not None
                and item.away_score is not None
            ),
            None,
        )
        if final_ticker is not None:
            home_score = final_ticker.home_score
            away_score = final_ticker.away_score
    raw_status = _name(match.get("eventStatus") or match.get("status") or match.get("section"))
    status = {
        "pre": "scheduled",
        "live": "live",
        "post": "finished",
    }.get((raw_status or "").casefold(), raw_status)
    structured = {
        "home_team": home,
        "away_team": away,
        "home_score": home_score,
        "away_score": away_score,
        "kickoff": _parse_datetime(
            match.get("startDate") or match.get("date") or match.get("kickoff")
        ),
        "competition": _name(
            match.get("superEvent")
            or match.get("competition")
            or match.get("competitionName")
            or match.get("leagueName")
        ),
        "venue": _name(match.get("location") or match.get("venue") or match.get("venueName")),
        "status": status,
    }
    normalized = json.dumps(
        {"structured": structured, "ticker": [item.__dict__ for item in items]},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    useful = bool(home or away or items or home_score is not None)
    return FupaReadResult(
        source_url=source_url,
        fetch_status="success" if useful else "incomplete",
        structured_data=structured,
        ticker=items,
        metadata={
            "http_status": status_code,
            "title": (soup.title.string or "")[:300] if soup.title else None,
            "parser": "jsonld-nextdata-redux-v2",
        },
        content_digest=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
    )


class FupaReader:
    def __init__(self, *, timeout_seconds: float = 20.0):
        self.timeout_seconds = timeout_seconds

    def fetch(self, source_url: str) -> FupaReadResult:
        current = validate_fupa_url(source_url)
        for _ in range(4):
            host = urlparse(current).hostname or ""
            _reject_private_resolution(host)
            with httpx.Client(timeout=self.timeout_seconds, follow_redirects=False) as client:
                response = client.get(
                    current,
                    headers={"User-Agent": "Vereinszentrale/1.0 (+FuPa match report import)"},
                )
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise FupaReadError("FuPa-Weiterleitung ohne Ziel")
                current = validate_fupa_url(urljoin(current, location))
                continue
            response.raise_for_status()
            if len(response.content) > _MAX_BYTES:
                raise FupaReadError("FuPa-Antwort überschreitet die zulässige Größe")
            return parse_fupa_html(current, response.text, status_code=response.status_code)
        raise FupaReadError("Zu viele FuPa-Weiterleitungen")
