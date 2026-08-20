#!/usr/bin/env python3
"""Capture an authenticated FuPa Playwright state without collecting a password."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.match_reports.fupa_browser import _browser_executable  # noqa: E402
from app.match_reports.fupa_session import sanitize_storage_state  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Öffnet FuPa interaktiv. Die Anmeldung erfolgt ausschließlich im echten "
            "Browserfenster; dieses Skript fragt kein Passwort ab."
        )
    )
    parser.add_argument("--output", required=True, help="Ziel für die JSON-Sitzungsdatei")
    parser.add_argument("--force", action="store_true", help="Vorhandene Zieldatei ersetzen")
    args = parser.parse_args()
    target = Path(args.output).expanduser().resolve()
    if target.exists() and not args.force:
        parser.error("Die Zieldatei existiert bereits; verwende --force zum Ersetzen")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=False,
            executable_path=_browser_executable(),
        )
        context = browser.new_context()
        page = context.new_page()
        page.goto("https://www.fupa.net/", wait_until="domcontentloaded")
        print("Melde dich jetzt im geöffneten FuPa-Fenster mit deinem FuPa-Konto an.")
        print(
            "Öffne danach eine Vereinsverwaltungsseite, auf der du Spielberichte bearbeiten darfst."
        )
        input(
            "Drücke anschließend hier Enter, um ausschließlich den FuPa-Sitzungszustand zu speichern: "
        )
        raw = json.dumps(context.storage_state(), ensure_ascii=False)
        canonical = sanitize_storage_state(raw)
        browser.close()

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(canonical, encoding="utf-8")
    if os.name != "nt":
        target.chmod(0o600)
    print(f"FuPa-Sitzungsdatei gespeichert: {target}")
    print("Lade sie in der Vereinszentrale hoch und lösche die lokale Datei danach sicher.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
