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
    if isinstance(value, dict):
        value = value.get("name")
    return str(value).strip() if value not in {None, ""} else None


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
    score = _SCORE_RE.search(text)
    source_id = str(raw.get("id") or raw.get("eventId") or f"ticker-{index}")[:200]
    return FupaTickerItem(
        source_id=source_id,
        event_type=_event_type(text),
        minute=minute,
        text=text[:1000],
        team=_name(raw.get("team") or raw.get("club")),
        player=_name(raw.get("player") or raw.get("person")),
        home_score=int(score.group(1)) if score else None,
        away_score=int(score.group(2)) if score else None,
    )


def _extract_json(soup: BeautifulSoup) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    match: dict[str, Any] = {}
    ticker: list[dict[str, Any]] = []
    documents: list[Any] = []
    for script in soup.find_all("script"):
        script_type = (script.get("type") or "").lower()
        script_id = script.get("id") or ""
        if script_type != "application/ld+json" and script_id != "__NEXT_DATA__":
            continue
        try:
            documents.append(json.loads(script.string or script.get_text() or "null"))
        except json.JSONDecodeError:
            continue
    for document in documents:
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
    home = _name(match.get("homeTeam") or match.get("home"))
    away = _name(match.get("awayTeam") or match.get("away"))
    home_score = match.get("homeScore")
    away_score = match.get("awayScore")
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
    structured = {
        "home_team": home,
        "away_team": away,
        "home_score": home_score,
        "away_score": away_score,
        "kickoff": _parse_datetime(match.get("startDate") or match.get("date")),
        "competition": _name(match.get("superEvent") or match.get("competition")),
        "venue": _name(match.get("location") or match.get("venue")),
        "status": _name(match.get("eventStatus") or match.get("status")),
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
            "parser": "jsonld-nextdata-v1",
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
