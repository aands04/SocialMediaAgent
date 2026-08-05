import base64
import html
import mimetypes
import os
import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment
from PIL import Image
from playwright.sync_api import sync_playwright


class RenderValidationError(ValueError):
    pass


BASE_CSS = """
*{box-sizing:border-box}html,body{margin:0;width:100%;height:100%;overflow:hidden}
body{font-family:var(--primary-font),Arial,sans-serif;background:var(--primary);color:var(--secondary)}
.canvas{position:relative;width:100%;height:100%;overflow:hidden;background:linear-gradient(145deg,var(--primary),color-mix(in srgb,var(--primary),#000 28%))}
.player{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;object-position:center top}
.shade{position:absolute;inset:0;background:linear-gradient(180deg,rgba(0,0,0,.05) 20%,rgba(0,0,0,.88) 90%)}
.brand{position:absolute;top:6%;left:7%;right:7%;display:flex;justify-content:space-between;align-items:center}
.logo,.logo-fallback{width:150px;height:150px;object-fit:contain}.logo-fallback{display:grid;place-items:center;border:5px solid currentColor;border-radius:50%;font-size:42px;font-weight:900}
.content{position:absolute;left:7%;right:7%;bottom:7%;display:flex;flex-direction:column;gap:24px}
.eyebrow{font:700 30px/1 var(--secondary-font),Arial,sans-serif;letter-spacing:.16em;text-transform:uppercase}
.teams{font-size:clamp(54px,7vw,92px);line-height:.94;font-weight:900;overflow-wrap:anywhere;text-wrap:balance}
.versus{font-size:.5em;opacity:.78}.meta{display:flex;flex-wrap:wrap;gap:14px;font-size:34px;font-weight:700}
.meta span{padding:12px 20px;background:rgba(0,0,0,.42);border-radius:14px}.score{font-size:150px;font-weight:900;line-height:.9}
"""

BUILTIN_HTML = """<main class="canvas">
{% if player_image %}<img class="player" src="{{ player_image }}" alt="Spielerbild">{% endif %}<div class="shade"></div>
<header class="brand">{% if team_logo %}<img class="logo" src="{{ team_logo }}" alt="Mannschaftslogo">{% else %}<div class="logo-fallback">{{ team_short }}</div>{% endif %}{% if opponent_logo %}<img class="logo" src="{{ opponent_logo }}" alt="Gegnerlogo">{% endif %}</header>
<section class="content"><div class="eyebrow">{{ label }} · {{ side_label }}</div>{% if score %}<div class="score">{{ score }}</div>{% endif %}<div class="teams"><div>{{ home_team }}</div><div class="versus">gegen</div><div>{{ away_team }}</div></div><div class="meta"><span>{{ date_de }}</span><span>{{ time_de }} Uhr</span><span>{{ competition }}</span><span>{{ venue }}</span></div></section>
</main>"""


def builtin_template(name: str, post_type: str, kind: str) -> dict:
    expected = "default-feed" if kind == "feed" else "default-story"
    if name != expected:
        name = expected
    return {
        "name": name,
        "version": 1,
        "post_type": post_type,
        "media_kind": kind,
        "html_template": BUILTIN_HTML,
        "css": BASE_CSS,
        "builtin": True,
    }


class Renderer:
    sizes = {"feed": (1080, 1350), "story": (1080, 1920)}

    image_types = {".png", ".jpg", ".jpeg", ".webp"}
    font_types = {".woff2", ".ttf"}

    def __init__(self, root: Path, media_root: Path | None = None, upload_root: Path | None = None):
        self.root = Path(root).resolve()
        self.media_root = Path(media_root).resolve() if media_root else None
        self.upload_root = Path(upload_root).resolve() if upload_root else None
        self.environment = SandboxedEnvironment(autoescape=True, undefined=StrictUndefined)

    @staticmethod
    def _browser_executable() -> str | None:
        candidates = [
            shutil.which("chromium"),
            shutil.which("chromium-browser"),
            os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"),
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]
        return next((str(path) for path in candidates if path and Path(path).is_file()), None)

    @staticmethod
    def _safe_file(
        path_value, roots: tuple[Path | None, ...], types: set[str], limit: int
    ) -> Path | None:
        if not path_value:
            return None
        path = Path(path_value).resolve()
        allowed = tuple(root for root in roots if root is not None)
        if not allowed or not any(path == root or path.is_relative_to(root) for root in allowed):
            raise RenderValidationError("Asset-Pfad liegt außerhalb der erlaubten Verzeichnisse")
        if path.suffix.lower() not in types:
            raise RenderValidationError("Nicht freigegebener Asset-Dateityp")
        if not path.exists():
            return None
        if not path.is_file() or path.is_symlink() or path.stat().st_size > limit:
            raise RenderValidationError("Asset-Datei ist unzulässig oder zu groß")
        return path

    def _asset(self, path_value) -> str | None:
        path = self._safe_file(
            path_value, (self.media_root, self.upload_root), self.image_types, 20 * 1024 * 1024
        )
        if not path:
            return None
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"

    def _font_face(self, font: dict | None, variable: str, fallback: str) -> tuple[str, str]:
        if not font:
            return "", fallback
        path = self._safe_file(
            font.get("path"), (self.upload_root,), self.font_types, 10 * 1024 * 1024
        )
        if not path:
            return "", fallback
        mime = mimetypes.guess_type(path.name)[0] or "font/ttf"
        encoded = base64.b64encode(path.read_bytes()).decode()
        family = html.escape(font.get("family") or fallback, quote=True)
        rule = f"@font-face{{font-family:'{family}';src:url(data:{mime};base64,{encoded})}}"
        return rule, f"'{family}'"

    def render(self, kind: str, target: str, data: dict) -> Path:
        if kind not in self.sizes:
            raise ValueError("Unbekanntes Format")
        required = ("home_team", "away_team", "kickoff", "post_type")
        missing = [key for key in required if not str(data.get(key, "")).strip()]
        if missing:
            raise RenderValidationError(f"Pflichtangaben fehlen: {', '.join(missing)}")
        if any(len(str(data[key])) > 300 for key in ("home_team", "away_team")):
            raise RenderValidationError("Pflichtangabe passt nicht in Textbereich")
        kickoff = (
            datetime.fromisoformat(data["kickoff"])
            if isinstance(data["kickoff"], str)
            else data["kickoff"]
        )
        if kickoff.tzinfo is None:
            raise RenderValidationError("Anpfiff benötigt eine Zeitzone")
        local = kickoff.astimezone(ZoneInfo("Europe/Berlin"))
        template = data.get("template") or builtin_template(
            f"default-{kind}", data["post_type"], kind
        )
        context = {
            **data,
            "date_de": local.strftime("%d.%m.%Y"),
            "time_de": local.strftime("%H:%M"),
            "competition": data.get("competition") or "Wettbewerb folgt",
            "venue": data.get("venue") or "Spielort folgt",
            "score": data.get("score"),
            "label": "Ergebnis" if data["post_type"] == "result" else "Spielankündigung",
            "side_label": data.get("side_label") or "Spieltag",
            "team_short": data.get("team_short") or "VEREIN",
            "player_image": self._asset(data.get("player_image")),
            "team_logo": self._asset(data.get("team_logo")),
            "opponent_logo": self._asset(data.get("opponent_logo")),
        }
        primary_rule, primary = self._font_face(data.get("primary_font_asset"), "primary", "Arial")
        secondary_rule, secondary = self._font_face(
            data.get("secondary_font_asset"), "secondary", "Arial"
        )
        css = (
            f":root{{--primary:{data.get('primary_color') or '#172554'};--secondary:{data.get('secondary_color') or '#fff'};--primary-font:{primary};--secondary-font:{secondary}}}"
            + primary_rule
            + secondary_rule
            + template.get("css", "")
        )
        markup = self.environment.from_string(template["html_template"]).render(**context)
        soup = BeautifulSoup(markup, "html.parser")
        for forbidden in soup.select("script,iframe,object,embed,link,base,meta"):
            forbidden.decompose()
        for element in soup.find_all(True):
            for attribute in tuple(element.attrs):
                value = str(element.attrs[attribute])
                if attribute.lower().startswith("on"):
                    del element.attrs[attribute]
                elif attribute.lower() in {"src", "href", "poster"} and not value.startswith(
                    ("data:", "#")
                ):
                    del element.attrs[attribute]
        csp = "default-src 'none'; img-src data:; font-src data:; style-src 'unsafe-inline'; script-src 'none'; connect-src 'none'; media-src 'none'; object-src 'none'; frame-src 'none'; base-uri 'none'"
        document = f"<!doctype html><html><head><meta charset='utf-8'><meta http-equiv='Content-Security-Policy' content=\"{csp}\"><style>{css}</style></head><body>{soup}</body></html>"
        out = (self.root / target).resolve()
        if out != self.root and not out.is_relative_to(self.root):
            raise RenderValidationError("Ausgabepfad liegt außerhalb des Render-Verzeichnisses")
        out.parent.mkdir(parents=True, exist_ok=True)
        width, height = self.sizes[kind]
        try:
            with sync_playwright() as playwright:
                executable = self._browser_executable()
                browser = playwright.chromium.launch(headless=True, executable_path=executable)
                page = browser.new_page(
                    viewport={"width": width, "height": height}, device_scale_factor=1
                )
                page.route("**/*", lambda route: route.abort())
                page.set_content(document, wait_until="load")
                page.evaluate(
                    """() => { for (const el of document.querySelectorAll('.teams')) { let n=parseFloat(getComputedStyle(el).fontSize); const bad=()=>{const r=el.getBoundingClientRect();return r.top<0||r.bottom>innerHeight||el.scrollWidth>el.clientWidth}; while (bad() && n>34) { n-=2; el.style.fontSize=n+'px'; } } }"""
                )
                teams = page.locator(".teams")
                clipped = teams.count() > 0 and teams.evaluate(
                    "el => {const r=el.getBoundingClientRect();return r.top<0||r.bottom>innerHeight||el.scrollWidth>el.clientWidth}"
                )
                if clipped:
                    raise RenderValidationError("Pflichtangabe passt nicht in Textbereich")
                page.screenshot(path=str(out), full_page=False, type="png")
                browser.close()
        except RenderValidationError:
            raise
        except Exception as exc:
            raise RenderValidationError(f"Playwright-Rendering fehlgeschlagen: {exc}") from exc
        self.validate(out, kind)
        return out

    def validate(self, path: Path, kind: str) -> dict:
        if not path.is_file() or path.stat().st_size <= 0:
            raise RenderValidationError("Grafikdatei fehlt oder ist leer")
        with Image.open(path) as image:
            image.load()
            if image.size != self.sizes[kind] or image.format != "PNG":
                raise RenderValidationError("Auflösung oder PNG-Format ungültig")
            if image.mode == "RGBA" and image.getchannel("A").getextrema() == (0, 0):
                raise RenderValidationError("Grafik ist vollständig transparent")
            colors = image.convert("RGB").getcolors(maxcolors=image.width * image.height)
            if colors is not None and len(colors) < 2:
                raise RenderValidationError("Grafik ist einfarbig und vermutlich leer")
            return {
                "kind": kind,
                "width": image.width,
                "height": image.height,
                "format": image.format,
                "bytes": path.stat().st_size,
            }
