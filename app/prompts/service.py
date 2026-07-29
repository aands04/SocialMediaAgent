import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from jinja2 import StrictUndefined, meta
from jinja2.sandbox import SandboxedEnvironment
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import PromptTemplate


class PromptValidationError(ValueError):
    pass


ALLOWED_PLACEHOLDERS = {
    "competition",
    "home_team",
    "away_team",
    "own_team",
    "opponent",
    "weekday",
    "date_de",
    "time_de",
    "home_away",
    "venue_display",
    "pitch_type",
    "primary_color",
    "secondary_color",
    "style_direction",
    "output_kind",
    "output_width",
    "output_height",
    "score",
    "hashtags",
}

DEFAULT_STYLE = (
    "hochwertig, emotional und vereinsnah; moderner Amateurfußball-Look, "
    "kräftige Kontraste, Tiefe, Licht und dezente Stadion-Atmosphäre; jede "
    "Ausgabe soll eine eigenständige Komposition erhalten"
)

IMAGE_SAFETY_PREFIX = """VERBINDLICHE DATEN- UND MEDIENREGELN:
- Verwende ausschließlich die nachfolgend angegebenen Spieldaten.
- Erfinde keine Spieler, Namen, Logos, Sponsoren, Ergebnisse oder Vereinsinformationen.
- Das bereitgestellte Spielerfoto ist die Identitätsreferenz. Gesicht, Körper,
  Trikot, Vereinsabzeichen und Proportionen müssen erkennbar unverändert bleiben.
- Verwende ausschließlich die bereitgestellten Logos. Erzeuge keine Fantasielogos.
- Stelle jeden angegebenen Text buchstaben- und zahlengenau dar.
- Keine vollständige Anschrift, keine Spiel-ID, Staffel-ID oder Schiedsrichterdaten.
"""

TEXT_SAFETY_PREFIX = """VERBINDLICHE FAKTENREGELN:
- Verwende ausschließlich die nachfolgend angegebenen, verifizierten Spieldaten.
- Erfinde keinen Spielverlauf, keine Torschützen, Zitate, Zuschauerzahlen,
  Tabellenstände, Verletzungen, Wetterdaten oder sonstigen Fakten.
- Ändere keine Mannschaftsnamen, Ergebnisse, Daten, Uhrzeiten oder Spielorte.
- Gib ausschließlich den direkt kopierbaren deutschen Begleittext aus.
"""

DEFAULT_IMAGE_PROMPT = """Erstelle eine eigenständige, hochwertige Sportgrafik für
Instagram im Format {{ output_kind }} ({{ output_width }} × {{ output_height }} Pixel).
Sie gehört zu einem zusammenhängenden Feed-/Story-Paar, darf aber keine bloß
gestreckte oder beschnittene Kopie des anderen Formats sein.

Nutze den bereitgestellten einzelnen Spieler als dominantes Hauptmotiv. Erzeuge
eine dynamische, glaubwürdige Komposition für einen Amateurfußballverein mit
Licht, Tiefe, Schatten, Kontrast und dezenten Fußball- oder Stadionelementen.
Orientiere die Farbwelt an {{ primary_color }} und {{ secondary_color }}.
Stilrichtung: {{ style_direction }}.

Stelle diese Angaben klar, mobil lesbar und hierarchisch dar:
1. {{ competition }}
2. {{ home_team }} gegen {{ away_team }}
3. {{ weekday }}, {{ date_de }}
4. {{ time_de }} Uhr
5. {{ venue_display }}
{% if score %}6. das bestätigte Ergebnis {{ score }} als dominantes Ergebniselement{% endif %}

Kennzeichnung: {{ home_away }}. Verwende höchstens zwei gut lesbare
Schriftstile. Halte im Story-Format deutliche Sicherheitsabstände oben und unten.
Falls kein Gegnerlogo bereitgestellt ist, verwende dort ausschließlich eine
neutrale typografische Lösung. Bewahre die Originalfarben des Trikots."""

DEFAULT_TEXT_PROMPT = """Verfasse einen kurzen deutschen Instagram-Begleittext
für einen Amateurfußballverein. Der Ton ist sachlich, vereinsnah, einladend und
natürlich. Verwende ausschließlich diese Fakten:

Wettbewerb: {{ competition }}
Spielpaarung: {{ home_team }} gegen {{ away_team }}
Datum: {{ weekday }}, {{ date_de }}
Uhrzeit: {{ time_de }} Uhr
Spielart: {{ home_away }}
Spielort: {{ venue_display }}
{% if score %}Bestätigtes Ergebnis: {{ score }}{% endif %}
Hashtags: {{ hashtags }}

Rufe die Zuschauer knapp zur Unterstützung von {{ own_team }} auf. Erfinde
keinen Spielverlauf, keine Torschützen, Zitate, Zuschauerzahlen oder sonstigen
Fakten. Gib ausschließlich den direkt kopierbaren Begleittext aus."""


@dataclass(frozen=True)
class ResolvedPrompt:
    name: str
    version: int
    prompt_kind: str
    post_type: str
    media_kind: str
    model: str
    quality: str
    body: str
    rendered: str
    builtin: bool

    def snapshot(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "prompt_kind": self.prompt_kind,
            "post_type": self.post_type,
            "media_kind": self.media_kind,
            "model": self.model,
            "quality": self.quality,
            "body": self.body,
            "rendered": self.rendered,
            "builtin": self.builtin,
        }


environment = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
WEEKDAYS_DE = (
    "Montag",
    "Dienstag",
    "Mittwoch",
    "Donnerstag",
    "Freitag",
    "Samstag",
    "Sonntag",
)


def validate_template(body: str) -> set[str]:
    if not body.strip():
        raise PromptValidationError("Prompt darf nicht leer sein")
    try:
        parsed = environment.parse(body)
    except Exception as exc:
        raise PromptValidationError(f"Prompt-Syntax ist ungültig: {exc}") from exc
    variables = meta.find_undeclared_variables(parsed)
    unknown = variables - ALLOWED_PLACEHOLDERS
    if unknown:
        raise PromptValidationError(
            "Unbekannte Platzhalter: " + ", ".join(sorted(unknown))
        )
    return variables


def _own_and_opponent(facts: dict) -> tuple[str, str, bool]:
    own = str(facts.get("own_team") or "").strip()
    home = str(facts.get("home_team") or "").strip()
    away = str(facts.get("away_team") or "").strip()
    if not own:
        raise PromptValidationError("Eigene Mannschaft fehlt")
    if own == home:
        return own, away, True
    if own == away:
        return own, home, False
    raise PromptValidationError(
        "Eigene Mannschaft konnte der Spielpaarung nicht eindeutig zugeordnet werden"
    )


def _place_name(venue: str) -> str:
    postal = re.search(r"\b\d{5}\s+([^,;]+)", venue)
    if postal:
        venue = postal.group(1)
    elif "," in venue or ";" in venue:
        parts = [
            part.strip()
            for part in re.split(r"[,;]", venue)
            if part.strip() and not re.search(r"\d", part)
        ]
        if parts:
            venue = parts[-1]
    value = re.sub(
        r"\b(Kunstrasenplatz|Rasenplatz|Sportplatz|Stadion|Kunstrasen|Rasen)\b",
        " ",
        venue,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"\b\d{5}\b|[,;]", " ", value)
    value = " ".join(value.split()).strip()
    if re.search(r"\d", value):
        return ""
    return value


def venue_display(facts: dict) -> str:
    _, _, is_home = _own_and_opponent(facts)
    if is_home:
        return "Habichtswaldstadion Ehlen"
    venue = str(facts.get("venue") or "").strip()
    pitch = str(facts.get("pitch") or "").strip().lower()
    if venue.upper().startswith(("RP ", "KR ")):
        return venue
    if not venue:
        raise PromptValidationError("Ort des Auswärtsspiels fehlt")
    if any(word in pitch for word in ("kunst", "synthetik")):
        prefix = "KR"
    elif any(word in pitch for word in ("rasen", "natur")):
        prefix = "RP"
    else:
        raise PromptValidationError(
            "Platzart des Auswärtsspiels fehlt; bitte Rasen oder Kunstrasen hinterlegen"
        )
    place = _place_name(venue)
    if not place:
        raise PromptValidationError("Ort des Auswärtsspiels ist nicht auswertbar")
    return f"{prefix} {place}"


def prompt_context(facts: dict, media_kind: str = "none", style_direction: str | None = None) -> dict:
    own, opponent, is_home = _own_and_opponent(facts)
    competition = str(facts.get("competition") or "").strip()
    if not competition:
        raise PromptValidationError("Wettbewerb fehlt")
    kickoff = (
        datetime.fromisoformat(facts["kickoff"])
        if isinstance(facts.get("kickoff"), str)
        else facts.get("kickoff")
    )
    if not kickoff or not kickoff.tzinfo:
        raise PromptValidationError("Anpfiff mit Zeitzone fehlt")
    local = kickoff.astimezone(ZoneInfo("Europe/Berlin"))
    sizes = {"feed": (1080, 1350), "story": (1080, 1920), "none": (0, 0)}
    width, height = sizes.get(media_kind, (0, 0))
    return {
        "competition": competition,
        "home_team": facts.get("home_team"),
        "away_team": facts.get("away_team"),
        "own_team": own,
        "opponent": opponent,
        "weekday": WEEKDAYS_DE[local.weekday()],
        "date_de": local.strftime("%d.%m.%Y"),
        "time_de": local.strftime("%H:%M"),
        "home_away": "Heimspiel" if is_home else "Auswärtsspiel",
        "venue_display": venue_display(facts),
        "pitch_type": facts.get("pitch") or "",
        "primary_color": facts.get("primary_color") or "#172554",
        "secondary_color": facts.get("secondary_color") or "#ffffff",
        "style_direction": style_direction or facts.get("style_direction") or DEFAULT_STYLE,
        "output_kind": {"feed": "Instagram-Feed", "story": "Instagram-Story"}.get(media_kind, ""),
        "output_width": width,
        "output_height": height,
        "score": facts.get("score") or "",
        "hashtags": " ".join(facts.get("hashtags") or []),
    }


def render_body(body: str, context: dict) -> str:
    validate_template(body)
    try:
        return environment.from_string(body).render(**context).strip()
    except Exception as exc:
        raise PromptValidationError(f"Prompt kann nicht gerendert werden: {exc}") from exc


def builtin_prompt(
    prompt_kind: str, post_type: str, media_kind: str, facts: dict
) -> ResolvedPrompt:
    settings = get_settings()
    image = prompt_kind == "image"
    name = (
        f"default-image-{media_kind}" if image else f"default-text-{post_type}"
    )
    body = DEFAULT_IMAGE_PROMPT if image else DEFAULT_TEXT_PROMPT
    context = prompt_context(facts, media_kind)
    rendered = render_body(body, context)
    if image:
        rendered = IMAGE_SAFETY_PREFIX + "\n" + rendered
    else:
        rendered = TEXT_SAFETY_PREFIX + "\n" + rendered
    return ResolvedPrompt(
        name=name,
        version=1,
        prompt_kind=prompt_kind,
        post_type=post_type,
        media_kind=media_kind,
        model=settings.openai_image_model if image else settings.openai_model,
        quality=settings.openai_image_quality if image else "default",
        body=body,
        rendered=rendered,
        builtin=True,
    )


def resolve_prompt(
    db: Session,
    name: str,
    prompt_kind: str,
    post_type: str,
    media_kind: str,
    facts: dict,
) -> ResolvedPrompt:
    item = db.scalar(
        select(PromptTemplate)
        .where(
            PromptTemplate.name == name,
            PromptTemplate.prompt_kind == prompt_kind,
            PromptTemplate.post_type == post_type,
            PromptTemplate.media_kind == media_kind,
            PromptTemplate.active.is_(True),
            PromptTemplate.archived_at.is_(None),
        )
        .order_by(PromptTemplate.version.desc())
    )
    if not item:
        if not name.startswith("default-"):
            raise PromptValidationError(
                f"Die zugewiesene Promptvorlage '{name}' ist nicht aktiv oder passt nicht zu Typ und Format"
            )
        return builtin_prompt(prompt_kind, post_type, media_kind, facts)
    context = prompt_context(facts, media_kind, item.style_direction)
    rendered = render_body(item.prompt_body, context)
    if prompt_kind == "image":
        rendered = IMAGE_SAFETY_PREFIX + "\n" + rendered
    else:
        rendered = TEXT_SAFETY_PREFIX + "\n" + rendered
    return ResolvedPrompt(
        name=item.name,
        version=item.version,
        prompt_kind=item.prompt_kind,
        post_type=item.post_type,
        media_kind=item.media_kind,
        model=item.model,
        quality=item.quality,
        body=item.prompt_body,
        rendered=rendered,
        builtin=False,
    )


def sample_facts() -> dict:
    return {
        "home_team": "SV Ehlen",
        "away_team": "SG Beispiel",
        "own_team": "SV Ehlen",
        "kickoff": "2026-08-09T13:00:00+00:00",
        "competition": "Kreisliga A",
        "venue": "Ehlen",
        "pitch": "Rasenplatz",
        "primary_color": "#172554",
        "secondary_color": "#ffffff",
        "hashtags": ["#SVEhlen", "#Spieltag"],
    }
