import hashlib
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup, Tag

BERLIN = ZoneInfo("Europe/Berlin")
ALLOWED_HOSTS = {"fussball.de", "www.fussball.de"}
ALLOWED_AJAX_PREFIXES = (
    "/ajax.team.next.games/",
    "/ajax.team.prev.games/",
    "/ajax.team.matchplan/",
)
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
GAME_ID_RE = re.compile(r"/spiel/(?:[^/?#]+/)*spiel/([A-Z0-9]+)(?:[/?#]|$)", re.I)
DATE_RE = re.compile(r"(\d{2}\.\d{2}\.(?:\d{2}|\d{4})).*?(\d{1,2}:\d{2})")
SCORE_RE = re.compile(r"^\s*(\d+)\s*:\s*(\d+)\s*$", re.ASCII)
ZERO_WIDTH_RE = re.compile("[\u200b\u200c\u200d\u2060\ufeff]")


@dataclass(frozen=True)
class GameRecord:
    external_id: str
    home_team: str
    away_team: str
    kickoff: datetime
    competition: str | None = None
    venue: str | None = None
    pitch: str | None = None
    status: str = "scheduled"
    home_score: int | None = None
    away_score: int | None = None
    halftime: str | None = None
    game_number: str | None = None
    source_url: str | None = None
    tracked_team: str | None = None
    tracked_team_side: str | None = None
    warnings: tuple[str, ...] = ()


class ProviderError(RuntimeError):
    pass


class GameDataProvider(ABC):
    @abstractmethod
    def fetch(self, url: str) -> list[GameRecord]: ...


class FussballDeProvider(GameDataProvider):
    def __init__(self, timeout: float = 10, max_attempts: int = 2):
        self.timeout = timeout
        self.max_attempts = max_attempts

    @staticmethod
    def validate_public_url(url: str, *, ajax_only: bool = False) -> str:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
            raise ProviderError("Nur öffentliche HTTPS-URLs von FUSSBALL.DE sind erlaubt")
        if parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ProviderError("URL enthält unzulässige Zugangsdaten oder Ports")
        if ajax_only and not any(parsed.path.startswith(prefix) for prefix in ALLOWED_AJAX_PREFIXES):
            raise ProviderError("Nicht erlaubter FUSSBALL.DE-AJAX-Pfad")
        return url

    def _get(self, url: str, *, ajax_only: bool = False) -> str:
        self.validate_public_url(url, ajax_only=ajax_only)
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            try:
                with httpx.Client(timeout=self.timeout, follow_redirects=False, headers={"User-Agent": "Vereins-SocialMediaBot/1.0 (read-only diagnostic)"}) as client:
                    response = client.get(url)
                if response.is_redirect:
                    target = urljoin(url, response.headers.get("location", ""))
                    self.validate_public_url(target, ajax_only=ajax_only)
                    with httpx.Client(timeout=self.timeout, follow_redirects=False, headers={"User-Agent": "Vereins-SocialMediaBot/1.0 (read-only diagnostic)"}) as client:
                        response = client.get(target)
                response.raise_for_status()
                if len(response.content) > MAX_RESPONSE_BYTES:
                    raise ProviderError("FUSSBALL.DE-Antwort überschreitet das Größenlimit")
                return response.text
            except (httpx.HTTPError, ProviderError) as exc:
                last_error = exc
                if attempt + 1 < self.max_attempts:
                    time.sleep(0.25 * (2**attempt))
        raise ProviderError(f"FUSSBALL.DE nicht erreichbar: {last_error}") from last_error

    def fetch_html(self, url: str, *, ajax_only: bool = False) -> str:
        return self._get(url, ajax_only=ajax_only)

    def fetch(self, url: str) -> list[GameRecord]:
        return self.parse(self.fetch_html(url))

    def fetch_ajax(self, url: str) -> list[GameRecord]:
        """Optionaler read-only Abruf ausschließlich erlaubter öffentlicher AJAX-Ressourcen."""
        return self.parse(self._get(url, ajax_only=True))

    def parse(self, html: str) -> list[GameRecord]:
        soup = BeautifulSoup(html, "html.parser")
        table = soup.select_one("#id-team-matchplan-table")
        if table:
            records = self._parse_matchplan(soup, table)
        else:
            records = self._parse_compact(soup)
        if not records:
            raise ProviderError("Keine Spiele erkannt; HTML-Struktur oder Pflichtfelder prüfen")
        return records

    def _parse_compact(self, soup: BeautifulSoup) -> list[GameRecord]:
        records: list[GameRecord] = []
        for node in soup.select("[data-game-id], .fixture"):
            try:
                external_id = node.get("data-game-id") or hashlib.sha256(node.get_text(" ", strip=True).encode()).hexdigest()[:24]
                home = clean_name(node.select_one(".home, .club-home").get_text(" ", strip=True))
                away = clean_name(node.select_one(".away, .club-away").get_text(" ", strip=True))
                raw = node.get("data-kickoff") or node.select_one("time").get("datetime")
                kickoff = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if kickoff.tzinfo is None:
                    kickoff = kickoff.replace(tzinfo=BERLIN).astimezone(timezone.utc)
                score = node.select_one(".score")
                home_score = away_score = None
                match = SCORE_RE.fullmatch(score.get_text(" ", strip=True)) if score and not score.select_one("[data-obfuscation]") else None
                if match:
                    home_score, away_score = map(int, match.groups())
                records.append(GameRecord(external_id, home, away, kickoff.astimezone(timezone.utc), venue=node.select_one(".venue").get_text(strip=True) if node.select_one(".venue") else None, status=node.get("data-status", "scheduled"), home_score=home_score, away_score=away_score))
            except (AttributeError, ValueError, TypeError) as exc:
                raise ProviderError("Kompaktes Fixture ist unvollständig oder widersprüchlich") from exc
        return records

    def _parse_matchplan(self, soup: BeautifulSoup, table: Tag) -> list[GameRecord]:
        provisional = table.select_one(".hint-pre-publish") is not None
        title = clean_name(soup.title.get_text(" ", strip=True).split("(", 1)[0]) if soup.title else None
        records: list[GameRecord] = []
        seen: set[str] = set()
        pending_meta: dict[str, object] | None = None
        for row in table.select("tr"):
            classes = set(row.get("class", []))
            if "row-competition" in classes:
                pending_meta = self._parse_meta(row)
                continue
            clubs = row.select(".column-club .club-name")
            if len(clubs) < 2:
                continue
            if pending_meta is None:
                continue
            parsed = self._parse_game_row(row, pending_meta, provisional, title)
            pending_meta = None
            if parsed is None or parsed.external_id in seen:
                continue
            seen.add(parsed.external_id)
            records.append(parsed)
        return records

    @staticmethod
    def _parse_meta(row: Tag) -> dict[str, object] | None:
        date_cell = row.select_one(".column-date")
        match = DATE_RE.search(date_cell.get_text(" ", strip=True)) if date_cell else None
        if not match:
            return None
        date_text, time_text = match.groups()
        date_format = "%d.%m.%Y" if len(date_text.split(".")[-1]) == 4 else "%d.%m.%y"
        local = datetime.strptime(f"{date_text} {time_text}", f"{date_format} %H:%M").replace(tzinfo=BERLIN)
        competition_node = row.select_one(".column-team a")
        detail_text = " ".join(node.get_text(" ", strip=True) for node in row.select("td")[-1:])
        number_match = re.search(r"(?:ME\s*\|\s*)?(\d{6,})", detail_text)
        return {"kickoff": local.astimezone(timezone.utc), "competition": clean_name(competition_node.get_text(" ", strip=True)) if competition_node else None, "game_number": number_match.group(1) if number_match else None}

    @staticmethod
    def _parse_game_row(row: Tag, meta: dict[str, object], provisional: bool, tracked_team: str | None) -> GameRecord | None:
        clubs = row.select(".column-club .club-name")
        home, away = (clean_name(club.get_text(" ", strip=True)) for club in clubs[:2])
        link = next((node.get("href") for node in row.select('a[href*="/spiel/"]') if GAME_ID_RE.search(node.get("href", ""))), None)
        if not link:
            return None
        match = GAME_ID_RE.search(link)
        if not match:
            return None
        source_url = link if link.startswith("https://") else f"https://www.fussball.de{link}"
        score = row.select_one(".column-score")
        home_score = away_score = None
        warnings: list[str] = []
        if score:
            if score.select_one("[data-obfuscation]"):
                warnings.append("Ergebnis ist durch Symbolschrift verschleiert und wurde nicht übernommen")
            else:
                numeric = SCORE_RE.fullmatch(score.get_text(" ", strip=True))
                if numeric:
                    home_score, away_score = map(int, numeric.groups())
        row_text = clean_name(row.get_text(" ", strip=True)).lower()
        status = "provisional" if provisional else "cancelled" if "abgesagt" in row_text else "postponed" if "verlegt" in row_text else "scheduled"
        side = "home" if tracked_team and home == tracked_team else "away" if tracked_team and away == tracked_team else None
        if provisional:
            warnings.append("Vorläufiger Spielplan; automatische Verarbeitung gesperrt")
        return GameRecord(match.group(1).upper(), home, away, meta["kickoff"], competition=meta.get("competition"), status=status, home_score=home_score, away_score=away_score, game_number=meta.get("game_number"), source_url=source_url, tracked_team=tracked_team, tracked_team_side=side, warnings=tuple(warnings))


def clean_name(value: str | None) -> str:
    return re.sub(r"\s+", " ", ZERO_WIDTH_RE.sub("", value or "")).strip()
