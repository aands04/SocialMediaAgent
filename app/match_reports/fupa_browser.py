from __future__ import annotations

import json
import os
import re
import shutil
import unicodedata
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from app.match_reports.publisher import FupaPublisher, FupaPublishResult


class FupaBrowserPublishError(RuntimeError):
    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category
        self.user_message = message


def _browser_executable() -> str | None:
    candidates = (
        os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        shutil.which("google-chrome"),
        shutil.which("msedge"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    )
    return next((item for item in candidates if item and Path(item).exists()), None)


def _is_fupa_url(value: str | None) -> bool:
    parsed = urlparse(value or "")
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (host == "fupa.net" or host.endswith(".fupa.net"))


def _normalize_identity(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    return " ".join(re.findall(r"[a-z0-9äöüß]+", normalized))


def _source_match_key(source_url: str) -> str:
    path_parts = [part for part in urlparse(source_url).path.split("/") if part]
    if "match" not in path_parts:
        return ""
    match_index = path_parts.index("match")
    if match_index + 1 >= len(path_parts):
        return ""
    return _normalize_identity(unquote(path_parts[match_index + 1]))


def _positive_match_id(value) -> int | None:
    try:
        match_id = int(value)
    except (TypeError, ValueError):
        return None
    return match_id if match_id > 0 else None


def _context_match_id(context) -> int | None:
    provenance = context.provenance if isinstance(context.provenance, dict) else {}
    return _positive_match_id(provenance.get("fupa_match_id"))


def _url_match_id(value: str) -> int | None:
    parsed = urlparse(value)
    query = parse_qs(parsed.query)
    for key in ("match_id", "spiel"):
        values = query.get(key, ())
        if values:
            match_id = _positive_match_id(values[0])
            if match_id:
                return match_id
    return None


def _admin_match_report_url(match_id: int) -> str:
    return f"https://admin.fupa.net/fupa/admin/spielbericht.php?spiel={match_id}"


def _match_association_confirmed(
    *,
    source_url: str,
    editor_url: str,
    visible_text: str,
    scoped_values: tuple[str, ...],
    home_team: str,
    away_team: str,
    expected_match_id: int | None = None,
) -> bool:
    """Require match-specific evidence that FuPa selected the intended game.

    The report text itself must never be passed as evidence: otherwise content
    filled by this provider could make an unrelated club-news editor appear to
    be associated with the match.
    """

    if expected_match_id is not None:
        # Once the trusted snapshot supplied a numeric FuPa match ID, never
        # weaken the association back to team-name matching. Team names are
        # not unique and may also occur in unrelated club-news editors.
        return _url_match_id(editor_url) == expected_match_id

    scoped_evidence = _normalize_identity(" ".join((editor_url, *scoped_values)))
    match_key = _source_match_key(source_url)
    if match_key and match_key in scoped_evidence:
        return True

    page_evidence = _normalize_identity(" ".join((visible_text, *scoped_values)))
    home = _normalize_identity(home_team)
    away = _normalize_identity(away_team)
    return bool(home and away and home in page_evidence and away in page_evidence)


def _normalize_editor_content(value: str | None) -> str:
    return " ".join(unicodedata.normalize("NFKC", value or "").split())


class BrowserFupaPublisher(FupaPublisher):
    """Submit one approved report through an already authenticated FuPa session.

    This provider never performs a login and never bypasses CAPTCHA or 2FA. It
    fails closed when FuPa's visible authoring UI cannot be identified safely.
    """

    automatic_supported = False

    def __init__(self, *, settings, storage_state: str):
        self.settings = settings
        self.storage_state = json.loads(storage_state)

    @staticmethod
    def _detect_blocked_state(page) -> None:
        url = page.url.casefold()
        text = page.locator("body").inner_text(timeout=5000).casefold()[:20000]
        if any(marker in text for marker in ("captcha", "bist du ein mensch", "robot")):
            raise FupaBrowserPublishError(
                "human_verification_required",
                "FuPa verlangt eine menschliche Sicherheitsprüfung. Bitte die FuPa-Sitzung neu einrichten.",
            )
        if "login" in url or "anmeld" in url or page.locator("input[type=password]").count():
            raise FupaBrowserPublishError(
                "authentication_required",
                "Die FuPa-Anmeldung ist abgelaufen. Bitte die FuPa-Sitzung neu einrichten.",
            )

    @staticmethod
    def _first_visible(page, selectors: tuple[str, ...], labels: tuple[str, ...] = ()):
        for selector in selectors:
            locator = page.locator(selector)
            for index in range(min(locator.count(), 5)):
                candidate = locator.nth(index)
                if candidate.is_visible():
                    return candidate
        for label in labels:
            locator = page.get_by_label(label, exact=False)
            for index in range(min(locator.count(), 5)):
                candidate = locator.nth(index)
                if candidate.is_visible():
                    return candidate
        return None

    @staticmethod
    def _association_values(page) -> tuple[str, ...]:
        selectors = (
            "input[name*='match' i]",
            "input[name*='game' i]",
            "input[name*='spiel' i]",
            "select[name*='match' i]",
            "select[name*='game' i]",
            "select[name*='spiel' i]",
            "[data-match-id]",
            "[data-game-id]",
            "[data-spiel-id]",
        )
        values: list[str] = []
        for selector in selectors:
            locator = page.locator(selector)
            for index in range(min(locator.count(), 25)):
                candidate = locator.nth(index)
                for attribute in (
                    "value",
                    "data-match-id",
                    "data-game-id",
                    "data-spiel-id",
                ):
                    value = candidate.get_attribute(attribute)
                    if value:
                        values.append(value)
                try:
                    text = candidate.inner_text(timeout=1000)
                except Exception:
                    text = ""
                if text:
                    values.append(text)
        return tuple(values)

    @staticmethod
    def _field_value(locator) -> str:
        if (locator.get_attribute("contenteditable") or "").casefold() == "true":
            return locator.inner_text(timeout=5000)
        return locator.input_value(timeout=5000)

    @staticmethod
    def _exact_match_link(
        page,
        *,
        match_id: int,
        path_markers: tuple[str, ...],
        visible_only: bool = True,
    ):
        """Return a link whose own URL proves the numeric FuPa match association.

        FuPa sometimes redirects the direct ``spielbericht.php`` request back to
        a team schedule or rewrites the surrounding page URL.  The match-specific
        links rendered in that page remain the authoritative navigation target.
        Inspecting their href keeps the fail-closed association guarantee without
        requiring FuPa to preserve the originally requested URL verbatim.
        """

        links = page.locator("a[href]")
        for index in range(min(links.count(), 300)):
            candidate = links.nth(index)
            href = candidate.get_attribute("href") or ""
            parsed = urlparse(href)
            destination = f"{parsed.path}?{parsed.query}".casefold()
            if not any(marker.casefold() in destination for marker in path_markers):
                continue
            if _url_match_id(href) != match_id:
                continue
            if visible_only and not candidate.is_visible():
                continue
            return candidate
        return None

    def publish(self, *, context, version, idempotency_key: str) -> FupaPublishResult:
        source_url = str(context.facts.get("source_url") or "")
        if not _is_fupa_url(source_url):
            raise FupaBrowserPublishError(
                "invalid_source",
                "Für dieses Spiel ist keine gültige öffentliche FuPa-Spielseite hinterlegt.",
            )
        match_id = _context_match_id(context)
        if match_id is None:
            raise FupaBrowserPublishError(
                "match_association_missing",
                "Die öffentliche FuPa-Spielseite enthält noch keine eindeutige interne Spiel-ID. Bitte die FuPa-Quelle erneut abrufen und die Übertragung danach wiederholen.",
            )
        timeout = max(5_000, int(self.settings.fupa_browser_timeout_seconds * 1000))
        try:
            with sync_playwright() as playwright:
                executable = _browser_executable()
                browser = playwright.chromium.launch(
                    headless=self.settings.fupa_browser_headless,
                    executable_path=executable,
                )
                browser_context = browser.new_context(storage_state=self.storage_state)
                page = browser_context.new_page()
                page.goto(
                    _admin_match_report_url(match_id),
                    wait_until="domcontentloaded",
                    timeout=timeout,
                )
                self._detect_blocked_state(page)

                report_context_confirmed = _url_match_id(page.url) == match_id
                if not report_context_confirmed:
                    # The legacy FuPa administration may redirect to the team
                    # schedule. Follow only a link that carries the exact trusted
                    # numeric match ID; never select a row by team name or order.
                    exact_report_link = self._exact_match_link(
                        page,
                        match_id=match_id,
                        path_markers=("spielbericht.php",),
                    )
                    if exact_report_link is not None:
                        exact_report_link.click(timeout=timeout)
                        page.wait_for_load_state("domcontentloaded", timeout=timeout)
                        self._detect_blocked_state(page)
                        report_context_confirmed = _url_match_id(page.url) == match_id

                open_editor = self._exact_match_link(
                    page,
                    match_id=match_id,
                    path_markers=("news_edit2",),
                )
                if open_editor is None:
                    hidden_exact_editor = self._exact_match_link(
                        page,
                        match_id=match_id,
                        path_markers=("news_edit2",),
                        visible_only=False,
                    )
                    # Text-only controls are safe only while the current page URL
                    # itself still attests the exact game, or while the collapsed
                    # section already contains an exact hidden editor link.
                    if report_context_confirmed or hidden_exact_editor is not None:
                        section_toggle = self._first_visible(
                            page,
                            (
                                "button:has-text('Spielbericht schreiben')",
                                "a:has-text('Spielbericht schreiben')",
                                "summary:has-text('Spielbericht schreiben')",
                                "[role='button']:has-text('Spielbericht schreiben')",
                                "[onclick]:has-text('Spielbericht schreiben')",
                                "text=Spielbericht schreiben",
                            ),
                        )
                    else:
                        section_toggle = None
                    if section_toggle is not None:
                        section_toggle.click(timeout=timeout)
                        open_editor = self._exact_match_link(
                            page,
                            match_id=match_id,
                            path_markers=("news_edit2",),
                        )
                if open_editor is None and report_context_confirmed:
                    # Some FuPa variants expose a JavaScript button without an
                    # href. It may be used only from the already attested game
                    # page; the destination is verified again before any input.
                    open_editor = self._first_visible(
                        page,
                        (
                            "a:has-text('Neuen Spielbericht schreiben')",
                            "a:has-text('Spielbericht bearbeiten')",
                            "button:has-text('Neuen Spielbericht schreiben')",
                            "button:has-text('Spielbericht bearbeiten')",
                        ),
                    )
                if open_editor is None:
                    raise FupaBrowserPublishError(
                        "match_association_missing",
                        "FuPa hat keinen eindeutig diesem Spiel zugeordneten Berichtseditor geöffnet. Es wurde nichts ausgefüllt oder gespeichert.",
                    )
                open_editor.click(timeout=timeout)
                page.wait_for_load_state("domcontentloaded", timeout=timeout)
                self._detect_blocked_state(page)

                prefill_text = page.locator("body").inner_text(timeout=5000)[:50000]
                association_values = self._association_values(page)
                if not _match_association_confirmed(
                    source_url=source_url,
                    editor_url=page.url,
                    visible_text=prefill_text,
                    scoped_values=association_values,
                    home_team=str(context.facts.get("home_team") or ""),
                    away_team=str(context.facts.get("away_team") or ""),
                    expected_match_id=match_id,
                ):
                    raise FupaBrowserPublishError(
                        "match_association_missing",
                        "FuPa hat nur den allgemeinen Vereinsnachrichten-Editor geöffnet und keine eindeutige Zuordnung zum vorgesehenen Spiel geliefert. Es wurde nichts ausgefüllt oder gespeichert.",
                    )

                headline = self._first_visible(
                    page,
                    (
                        "input[name='headline']",
                        "input[name='title']",
                        "input[name*='headline' i]",
                        "input[name*='title' i]",
                        "input[name*='schlagzeile' i]",
                        "input[aria-label*='Überschrift']",
                        "input[placeholder*='Überschrift']",
                        "input[aria-label*='Schlagzeile']",
                        "input[placeholder*='Schlagzeile']",
                    ),
                    ("Überschrift", "Schlagzeile"),
                )
                body = self._first_visible(
                    page,
                    (
                        "textarea[name='report']",
                        "textarea[name='body']",
                        "textarea[aria-label*='Spielbericht']",
                        "textarea[placeholder*='Spielbericht']",
                        ".ck-editor__editable[contenteditable='true']",
                        "[contenteditable='true'][role='textbox']",
                        "[contenteditable='true']",
                    ),
                    ("Spielbericht", "Haupttext"),
                )
                if headline is None or body is None:
                    raise FupaBrowserPublishError(
                        "ui_changed",
                        "Die FuPa-Eingabefelder konnten nicht eindeutig erkannt werden. Es wurde nichts übertragen.",
                    )
                headline.fill(version.headline[:300])
                text = "\n\n".join(
                    part.strip() for part in (version.teaser or "", version.body) if part.strip()
                )
                if body.get_attribute("contenteditable") == "true":
                    body.click()
                    page.keyboard.press("Control+A")
                    page.keyboard.insert_text(text)
                else:
                    body.fill(text)

                saved_headline = self._field_value(headline)
                saved_body = self._field_value(body)
                if _normalize_editor_content(saved_headline) != _normalize_editor_content(
                    version.headline[:300]
                ) or _normalize_editor_content(saved_body) != _normalize_editor_content(text):
                    raise FupaBrowserPublishError(
                        "editor_fill_failed",
                        "FuPa hat Überschrift oder Berichtstext nicht vollständig in den spielbezogenen Editor übernommen. Es wurde nichts gespeichert.",
                    )

                submit = self._first_visible(
                    page,
                    (
                        "button:has-text('Spielbericht speichern')",
                        "button:has-text('Speichern')",
                        "button:has-text('Veröffentlichen')",
                        "button:has-text('Online stellen')",
                        "input[type='submit'][value='Speichern']",
                        "input[type='submit'][value*='Spielbericht speichern']",
                    ),
                )
                if submit is None:
                    raise FupaBrowserPublishError(
                        "ui_changed",
                        "Die FuPa-Speicheraktion konnte nicht eindeutig erkannt werden. Es wurde nichts übertragen.",
                    )
                editor_url = page.url
                submit.click(timeout=timeout)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=timeout)
                except PlaywrightTimeoutError:
                    # Some FuPa forms save asynchronously and intentionally do
                    # not navigate. Confirmation is checked below.
                    pass
                self._detect_blocked_state(page)
                page.wait_for_timeout(750)
                visible_text = page.locator("body").inner_text(timeout=5000).casefold()[:20000]
                failure_markers = (
                    "entwurf wurde noch nicht gespeichert",
                    "konnte nicht gespeichert werden",
                    "speichern fehlgeschlagen",
                )
                if any(marker in visible_text for marker in failure_markers):
                    raise FupaBrowserPublishError(
                        "unconfirmed_submission",
                        "FuPa hat den Bericht nicht als gespeichert bestätigt. Die Vereinszentrale markiert ihn deshalb nicht als übertragen.",
                    )
                success_markers = (
                    "spielbericht wurde gespeichert",
                    "spielbericht erfolgreich gespeichert",
                    "der entwurf wurde gespeichert",
                    "entwurf gespeichert",
                    "erfolgreich veröffentlicht",
                    "änderungen gespeichert",
                )
                try:
                    editor_still_visible = headline.is_visible() and body.is_visible()
                except Exception:
                    editor_still_visible = False
                if not any(marker in visible_text for marker in success_markers) and (
                    page.url == editor_url or editor_still_visible
                ):
                    raise FupaBrowserPublishError(
                        "unconfirmed_submission",
                        "FuPa hat keine eindeutige Speicherbestätigung geliefert. Bitte bei FuPa prüfen; die Vereinszentrale markiert den Bericht vorsichtshalber nicht als übertragen.",
                    )
                updated_state = json.dumps(browser_context.storage_state(), ensure_ascii=False)
                result_url = page.url if _is_fupa_url(page.url) else source_url
                browser.close()
                return FupaPublishResult(
                    status="published",
                    external_url=result_url,
                    external_id=idempotency_key,
                    updated_storage_state=updated_state,
                )
        except FupaBrowserPublishError:
            raise
        except PlaywrightTimeoutError as exc:
            raise FupaBrowserPublishError(
                "provider_timeout",
                "FuPa hat nicht rechtzeitig geantwortet. Es wurde keine erfolgreiche Übertragung bestätigt.",
            ) from exc
        except Exception as exc:
            # No HTML, cookies, storage state or credentials are included in the
            # user-facing error or audit trail.
            raise FupaBrowserPublishError(
                "provider_error",
                "Die FuPa-Browserübergabe ist technisch fehlgeschlagen. Es wurde keine erfolgreiche Übertragung bestätigt.",
            ) from exc
