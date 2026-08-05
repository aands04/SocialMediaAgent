import hashlib
import re
import struct
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
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
MAX_DETAIL_FETCHES = 25
MAX_FONT_BYTES = 128 * 1024
FONT_ID_RE = re.compile(r"^[A-Za-z0-9]{4,32}$")
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
    venue_address: str | None = None
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


@dataclass(frozen=True)
class GameVenueDetail:
    venue: str
    pitch: str
    address: str | None = None


class GameDataProvider(ABC):
    @abstractmethod
    def fetch(self, url: str) -> list[GameRecord]: ...


class FussballDeProvider(GameDataProvider):
    def __init__(
        self,
        timeout: float = 10,
        max_attempts: int = 2,
        *,
        decode_obfuscated_results: bool = False,
    ):
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.decode_obfuscated_results = decode_obfuscated_results
        self._font_cache: dict[str, dict[int, int]] = {}

    @staticmethod
    def validate_public_url(url: str, *, ajax_only: bool = False) -> str:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
            raise ProviderError("Nur öffentliche HTTPS-URLs von FUSSBALL.DE sind erlaubt")
        if parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ProviderError("URL enthält unzulässige Zugangsdaten oder Ports")
        if ajax_only and not any(
            parsed.path.startswith(prefix) for prefix in ALLOWED_AJAX_PREFIXES
        ):
            raise ProviderError("Nicht erlaubter FUSSBALL.DE-AJAX-Pfad")
        return url

    @classmethod
    def validate_game_detail_url(cls, url: str, expected_external_id: str | None = None) -> str:
        cls.validate_public_url(url)
        match = GAME_ID_RE.search(urlparse(url).path)
        if not match:
            raise ProviderError("Nicht erlaubter FUSSBALL.DE-Spielpfad")
        if expected_external_id and match.group(1).upper() != expected_external_id.upper():
            raise ProviderError("Spiel-ID der Detailseite stimmt nicht mit dem Spielplan überein")
        return url

    def _get(self, url: str, *, ajax_only: bool = False) -> str:
        self.validate_public_url(url, ajax_only=ajax_only)
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            try:
                with httpx.Client(
                    timeout=self.timeout,
                    follow_redirects=False,
                    headers={
                        "User-Agent": "Vereins-SocialMediaBot/1.0 (read-only synchronization)"
                    },
                ) as client:
                    response = client.get(url)
                if response.is_redirect:
                    target = urljoin(url, response.headers.get("location", ""))
                    self.validate_public_url(target, ajax_only=ajax_only)
                    with httpx.Client(
                        timeout=self.timeout,
                        follow_redirects=False,
                        headers={
                            "User-Agent": "Vereins-SocialMediaBot/1.0 (read-only synchronization)"
                        },
                    ) as client:
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

    def _get_bytes(self, url: str, *, maximum: int) -> bytes:
        self.validate_public_url(url)
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            try:
                with httpx.Client(
                    timeout=self.timeout,
                    follow_redirects=False,
                    headers={
                        "User-Agent": "Vereins-SocialMediaBot/1.0 (read-only synchronization)"
                    },
                ) as client:
                    response = client.get(url)
                if response.is_redirect:
                    target = urljoin(url, response.headers.get("location", ""))
                    self.validate_public_url(target)
                    with httpx.Client(
                        timeout=self.timeout,
                        follow_redirects=False,
                        headers={
                            "User-Agent": "Vereins-SocialMediaBot/1.0 (read-only synchronization)"
                        },
                    ) as client:
                        response = client.get(target)
                response.raise_for_status()
                if len(response.content) > maximum:
                    raise ProviderError("FUSSBALL.DE-Schriftdatei ist unerwartet groß")
                return response.content
            except (httpx.HTTPError, ProviderError) as exc:
                last_error = exc
                if attempt + 1 < self.max_attempts:
                    time.sleep(0.25 * (2**attempt))
        raise ProviderError(f"FUSSBALL.DE-Schrift nicht erreichbar: {last_error}") from last_error

    def fetch_html(self, url: str, *, ajax_only: bool = False) -> str:
        return self._get(url, ajax_only=ajax_only)

    def fetch(self, url: str) -> list[GameRecord]:
        return self.parse(self.fetch_html(url))

    def fetch_ajax(self, url: str) -> list[GameRecord]:
        """Optionaler read-only Abruf ausschließlich erlaubter öffentlicher AJAX-Ressourcen."""
        return self.parse(self._get(url, ajax_only=True))

    @classmethod
    def ajax_resource(cls, html: str, resource: str) -> str | None:
        allowed = {"next", "prev", "matchplan"}
        if resource not in allowed:
            raise ProviderError("Unbekannte FUSSBALL.DE-AJAX-Ressource")
        soup = BeautifulSoup(html, "html.parser")
        prefix = (
            f"/ajax.team.{resource}.games/" if resource != "matchplan" else "/ajax.team.matchplan/"
        )
        for node in soup.select("[data-ajax-resource]"):
            raw = str(node.get("data-ajax-resource") or "")
            candidate = urljoin("https://www.fussball.de/", raw)
            if urlparse(candidate).path.startswith(prefix):
                return cls.validate_public_url(candidate, ajax_only=True)
        return None

    def fetch_game_detail(self, url: str, expected_external_id: str) -> GameVenueDetail:
        self.validate_game_detail_url(url, expected_external_id)
        return self.parse_game_detail(self._get(url), expected_external_id=expected_external_id)

    def enrich_game_details(
        self, records: list[GameRecord], *, delay_seconds: float = 0.25
    ) -> list[GameRecord]:
        """Read-only enrichment from the linked public game pages.

        A failed detail page must not discard the matchplan record. Requests are
        deliberately sequential and bounded to keep the diagnostic considerate.
        """
        enriched: list[GameRecord] = []
        fetched_count = 0
        for record in records:
            if not record.source_url:
                enriched.append(record)
                continue
            if fetched_count >= MAX_DETAIL_FETCHES:
                enriched.append(
                    replace(
                        record,
                        warnings=record.warnings
                        + (
                            "Spielort/Platzart nicht angereichert: "
                            f"Abruflimit von {MAX_DETAIL_FETCHES} Detailseiten erreicht",
                        ),
                    )
                )
                continue
            if delay_seconds > 0 and fetched_count:
                time.sleep(delay_seconds)
            fetched_count += 1
            try:
                detail = self.fetch_game_detail(record.source_url, record.external_id)
            except ProviderError as exc:
                enriched.append(
                    replace(
                        record,
                        warnings=record.warnings
                        + (f"Spielort/Platzart konnten nicht gelesen werden: {exc}",),
                    )
                )
            else:
                enriched.append(
                    replace(
                        record,
                        venue=detail.venue,
                        pitch=detail.pitch,
                        venue_address=detail.address,
                    )
                )
        return enriched

    @classmethod
    def parse_game_detail(
        cls, html: str, *, expected_external_id: str | None = None
    ) -> GameVenueDetail:
        soup = BeautifulSoup(html, "html.parser")
        canonical = soup.select_one('link[rel="canonical"][href]')
        if expected_external_id:
            if not canonical:
                raise ProviderError("Kanonische Spiel-URL fehlt auf der Detailseite")
            cls.validate_game_detail_url(canonical.get("href", ""), expected_external_id)
        location = soup.select_one("a.location")
        raw = clean_name(location.get_text(" ", strip=True)) if location else ""
        parts = [clean_name(part) for part in raw.split(",") if clean_name(part)]
        if len(parts) < 2:
            raise ProviderError("Spielort oder Platzart fehlen auf der Detailseite")
        pitch, venue = parts[:2]
        address = ", ".join(parts[2:]) or None
        if len(pitch) > 80 or len(venue) > 250 or (address and len(address) > 500):
            raise ProviderError("Spielortdaten überschreiten die zulässige Länge")
        return GameVenueDetail(venue=venue, pitch=pitch, address=address)

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
                external_id = (
                    node.get("data-game-id")
                    or hashlib.sha256(node.get_text(" ", strip=True).encode()).hexdigest()[:24]
                )
                home = clean_name(node.select_one(".home, .club-home").get_text(" ", strip=True))
                away = clean_name(node.select_one(".away, .club-away").get_text(" ", strip=True))
                raw = node.get("data-kickoff") or node.select_one("time").get("datetime")
                kickoff = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if kickoff.tzinfo is None:
                    kickoff = kickoff.replace(tzinfo=BERLIN).astimezone(timezone.utc)
                score = node.select_one(".score")
                home_score = away_score = None
                match = (
                    SCORE_RE.fullmatch(score.get_text(" ", strip=True))
                    if score and not score.select_one("[data-obfuscation]")
                    else None
                )
                if match:
                    home_score, away_score = map(int, match.groups())
                records.append(
                    GameRecord(
                        external_id,
                        home,
                        away,
                        kickoff.astimezone(timezone.utc),
                        venue=node.select_one(".venue").get_text(strip=True)
                        if node.select_one(".venue")
                        else None,
                        status=node.get("data-status", "scheduled"),
                        home_score=home_score,
                        away_score=away_score,
                    )
                )
            except (AttributeError, ValueError, TypeError) as exc:
                raise ProviderError(
                    "Kompaktes Fixture ist unvollständig oder widersprüchlich"
                ) from exc
        return records

    def _parse_matchplan(self, soup: BeautifulSoup, table: Tag) -> list[GameRecord]:
        provisional = table.select_one(".hint-pre-publish") is not None
        title = (
            clean_name(soup.title.get_text(" ", strip=True).split("(", 1)[0])
            if soup.title
            else None
        )
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
        local = datetime.strptime(f"{date_text} {time_text}", f"{date_format} %H:%M").replace(
            tzinfo=BERLIN
        )
        competition_node = row.select_one(".column-team a")
        detail_text = " ".join(node.get_text(" ", strip=True) for node in row.select("td")[-1:])
        number_match = re.search(r"(?:ME\s*\|\s*)?(\d{6,})", detail_text)
        return {
            "kickoff": local.astimezone(timezone.utc),
            "competition": clean_name(competition_node.get_text(" ", strip=True))
            if competition_node
            else None,
            "game_number": number_match.group(1) if number_match else None,
        }

    def _decode_obfuscated_number(self, node: Tag) -> int:
        font_id = str(node.get("data-obfuscation") or "")
        if not FONT_ID_RE.fullmatch(font_id):
            raise ProviderError("Ungültige Kennung der FUSSBALL.DE-Symbolschrift")
        cmap = self._font_cache.get(font_id)
        if cmap is None:
            url = f"https://www.fussball.de/export.fontface/-/format/ttf/id/{font_id}/type/font"
            cmap = _validated_digit_cmap(self._get_bytes(url, maximum=MAX_FONT_BYTES))
            self._font_cache[font_id] = cmap
        decoded: list[str] = []
        for character in node.get_text("", strip=True):
            if character.isspace():
                continue
            glyph_id = cmap.get(ord(character))
            if glyph_id is None or not 1 <= glyph_id <= 10:
                raise ProviderError("Symbolschrift enthält kein eindeutig lesbares Torergebnis")
            decoded.append(str(glyph_id - 1))
        if not decoded:
            raise ProviderError("Leeres Torergebnis in der Symbolschrift")
        return int("".join(decoded))

    def _parse_game_row(
        self, row: Tag, meta: dict[str, object], provisional: bool, tracked_team: str | None
    ) -> GameRecord | None:
        clubs = row.select(".column-club .club-name")
        home, away = (clean_name(club.get_text(" ", strip=True)) for club in clubs[:2])
        link = next(
            (
                node.get("href")
                for node in row.select('a[href*="/spiel/"]')
                if GAME_ID_RE.search(node.get("href", ""))
            ),
            None,
        )
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
                left = score.select_one(".score-left[data-obfuscation]")
                right = score.select_one(".score-right[data-obfuscation]")
                if not left or not right:
                    symbols = score.select("[data-obfuscation]")
                    if len(symbols) == 2:
                        left, right = symbols
                if self.decode_obfuscated_results and left and right:
                    try:
                        home_score = self._decode_obfuscated_number(left)
                        away_score = self._decode_obfuscated_number(right)
                    except ProviderError as exc:
                        warnings.append(
                            f"Ergebnis-Symbolschrift konnte nicht sicher gelesen werden: {exc}"
                        )
                    else:
                        warnings.append(
                            "Ergebnis deterministisch aus der offiziellen Symbolschrift "
                            "gelesen; Bestätigung steht aus"
                        )
                else:
                    warnings.append(
                        "Ergebnis ist durch Symbolschrift verschleiert und wurde nicht übernommen"
                    )
            else:
                numeric = SCORE_RE.fullmatch(score.get_text(" ", strip=True))
                if numeric:
                    home_score, away_score = map(int, numeric.groups())
        row_text = clean_name(row.get_text(" ", strip=True)).lower()
        status = (
            "cancelled"
            if "abgesagt" in row_text
            else "postponed"
            if "verlegt" in row_text
            else "provisional"
            if provisional
            else "scheduled"
        )
        side = (
            "home"
            if tracked_team and home == tracked_team
            else "away"
            if tracked_team and away == tracked_team
            else None
        )
        if provisional:
            warnings.append("Vorläufiger Spielplan")
        return GameRecord(
            match.group(1).upper(),
            home,
            away,
            meta["kickoff"],
            competition=meta.get("competition"),
            status=status,
            home_score=home_score,
            away_score=away_score,
            game_number=meta.get("game_number"),
            source_url=source_url,
            tracked_team=tracked_team,
            tracked_team_side=side,
            warnings=tuple(warnings),
        )


def clean_name(value: str | None) -> str:
    return re.sub(r"\s+", " ", ZERO_WIDTH_RE.sub("", value or "")).strip()


def _validated_digit_cmap(font: bytes) -> dict[int, int]:
    """Read the Unicode cmap of FUSSBALL.DE's small digit font.

    The downloaded font contains exactly the ten digits in glyph IDs 1..10 and
    a dash in glyph 11. The server randomizes only the private Unicode mapping.
    We validate that compact structure before using it and never use OCR or
    visual guessing.
    """
    try:
        if len(font) < 12 or font[:4] not in {b"\x00\x01\x00\x00", b"true"}:
            raise ValueError("kein TrueType-Header")
        table_count = struct.unpack_from(">H", font, 4)[0]
        if not 1 <= table_count <= 64 or len(font) < 12 + table_count * 16:
            raise ValueError("ungültiges Tabellenverzeichnis")
        tables: dict[bytes, tuple[int, int]] = {}
        for index in range(table_count):
            tag, _, offset, length = struct.unpack_from(">4sIII", font, 12 + index * 16)
            if offset + length > len(font):
                raise ValueError("Tabelle liegt außerhalb der Datei")
            tables[tag] = (offset, length)
        maxp_offset, _ = tables[b"maxp"]
        glyph_count = struct.unpack_from(">H", font, maxp_offset + 4)[0]
        if glyph_count != 12:
            raise ValueError("unerwartete Anzahl von Schriftzeichen")
        cmap_offset, cmap_length = tables[b"cmap"]
        _, subtable_count = struct.unpack_from(">HH", font, cmap_offset)
        result: dict[int, int] = {}
        for index in range(subtable_count):
            _, _, relative = struct.unpack_from(">HHI", font, cmap_offset + 4 + index * 8)
            subtable = cmap_offset + relative
            if not cmap_offset <= subtable < cmap_offset + cmap_length:
                continue
            if struct.unpack_from(">H", font, subtable)[0] != 4:
                continue
            _, _, segment_count_x2 = struct.unpack_from(">HHH", font, subtable + 2)
            segment_count = segment_count_x2 // 2
            if not 1 <= segment_count <= 256:
                raise ValueError("ungültige Zeichensegmente")
            end_offset = subtable + 14
            ends = struct.unpack_from(">" + "H" * segment_count, font, end_offset)
            start_offset = end_offset + 2 * segment_count + 2
            starts = struct.unpack_from(">" + "H" * segment_count, font, start_offset)
            delta_offset = start_offset + 2 * segment_count
            deltas = struct.unpack_from(">" + "h" * segment_count, font, delta_offset)
            range_offset = delta_offset + 2 * segment_count
            ranges = struct.unpack_from(">" + "H" * segment_count, font, range_offset)
            for segment, (start, end, delta, offset) in enumerate(
                zip(starts, ends, deltas, ranges, strict=True)
            ):
                if end < start or end - start > 8192:
                    raise ValueError("ungültiger Zeichenbereich")
                for codepoint in range(start, end + 1):
                    if codepoint == 0xFFFF:
                        continue
                    if offset == 0:
                        glyph = (codepoint + delta) & 0xFFFF
                    else:
                        position = range_offset + 2 * segment + offset + 2 * (codepoint - start)
                        if position + 2 > len(font):
                            raise ValueError("Zeichenzuordnung außerhalb der Datei")
                        glyph = struct.unpack_from(">H", font, position)[0]
                        if glyph:
                            glyph = (glyph + delta) & 0xFFFF
                    if glyph:
                        result[codepoint] = glyph
        if not result or not set(range(1, 12)).issubset(set(result.values())):
            raise ValueError("Ziffernbelegung ist unvollständig")
        return result
    except (KeyError, struct.error, ValueError) as exc:
        raise ProviderError(f"Ungültige FUSSBALL.DE-Symbolschrift: {exc}") from exc
