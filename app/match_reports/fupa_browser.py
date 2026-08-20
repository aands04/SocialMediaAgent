from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from urllib.parse import urlparse

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
    def _first_visible(page, selectors: tuple[str, ...]):
        for selector in selectors:
            locator = page.locator(selector)
            for index in range(min(locator.count(), 5)):
                candidate = locator.nth(index)
                if candidate.is_visible():
                    return candidate
        return None

    def publish(self, *, context, version, idempotency_key: str) -> FupaPublishResult:
        source_url = str(context.facts.get("source_url") or "")
        if not _is_fupa_url(source_url):
            raise FupaBrowserPublishError(
                "invalid_source",
                "Für dieses Spiel ist keine gültige öffentliche FuPa-Spielseite hinterlegt.",
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
                page.goto(source_url, wait_until="domcontentloaded", timeout=timeout)
                self._detect_blocked_state(page)

                open_editor = self._first_visible(
                    page,
                    (
                        "a:has-text('Spielbericht anlegen')",
                        "button:has-text('Spielbericht anlegen')",
                        "a:has-text('Spielbericht bearbeiten')",
                        "button:has-text('Spielbericht bearbeiten')",
                        "a[href*='spielbericht']",
                        "a[href*='match-report']",
                    ),
                )
                if open_editor is None:
                    raise FupaBrowserPublishError(
                        "ui_changed",
                        "FuPa zeigt für dieses Konto keine bearbeitbare Spielberichtsfunktion an. Bitte Vereinsrecht und Spielzuordnung bei FuPa prüfen.",
                    )
                open_editor.click(timeout=timeout)
                page.wait_for_load_state("domcontentloaded", timeout=timeout)
                self._detect_blocked_state(page)

                headline = self._first_visible(
                    page,
                    (
                        "input[name='headline']",
                        "input[name='title']",
                        "input[aria-label*='Überschrift']",
                        "input[placeholder*='Überschrift']",
                    ),
                )
                body = self._first_visible(
                    page,
                    (
                        "textarea[name='report']",
                        "textarea[name='body']",
                        "textarea[aria-label*='Spielbericht']",
                        "textarea[placeholder*='Spielbericht']",
                        "[contenteditable='true']",
                    ),
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

                submit = self._first_visible(
                    page,
                    (
                        "button:has-text('Spielbericht speichern')",
                        "button:has-text('Speichern')",
                        "button:has-text('Veröffentlichen')",
                        "button:has-text('Online stellen')",
                        "input[type='submit']",
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
                success_markers = (
                    "spielbericht wurde gespeichert",
                    "spielbericht erfolgreich gespeichert",
                    "erfolgreich veröffentlicht",
                    "änderungen gespeichert",
                )
                editor_still_visible = headline.is_visible() and body.is_visible()
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
