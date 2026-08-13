import hashlib
import re
from dataclasses import dataclass, replace
from datetime import datetime
from zoneinfo import ZoneInfo

from jinja2 import StrictUndefined, meta
from jinja2.sandbox import SandboxedEnvironment
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.branding.compiler import (
    BRANDING_COMPILER_VERSION,
    compile_branding_instructions,
)
from app.branding.service import branding_snapshot
from app.config import get_settings
from app.games.identity import TeamIdentityError, resolve_team_side
from app.models import ClubPromptOverride, PromptStatus, PromptTemplate


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
    "own_team_display",
    "channel_type",
    "channel_name",
    "result_fields_instruction",
}

DEFAULT_STYLE = (
    "hochwertig, emotional und vereinsnah; moderner Amateurfußball-Look, "
    "kräftige Kontraste, Tiefe, Licht und dezente Stadion-Atmosphäre; jede "
    "Ausgabe soll eine eigenständige Komposition erhalten"
)

IMAGE_POLICY_VERSION = "verified-media-ai-references-v7-configured-result-fields"

RESULT_IMAGE_EDIT_SAFETY_PREFIX = """VERBINDLICHER ERGEBNISBILD-UMBAU:
- Referenzbild 1 ist die bereits geprüfte Ankündigungsgrafik genau dieses Spiels
  und in diesem Ausgabeformat. Verwende ausschließlich dieses Bild als visuelle
  Grundlage. Dies ist eine gezielte Bildbearbeitung, keine neue freie Komposition.
- Bewahre alle vorhandenen realen Personen, Gesichter, Körper, Trikots, Logos,
  Sponsorenzeichen, Hintergründe, Lichtstimmung, Farben, Bildausschnitt und
  Motivpositionen so unverändert wie technisch möglich.
- Füge keine Person, kein Logo, kein Wappen, kein Sponsorenzeichen und kein
  zusätzliches Motiv hinzu. Entferne oder ersetze keine vorhandene Person und
  tausche keine Identität aus.
- Ändere ausschließlich die Informationshierarchie zur Ergebnismeldung. Zeige
  nur die nachfolgend serverseitig freigegebenen Ergebnisangaben. Ergebnis und
  Mannschaftsnamen sind dabei immer erforderlich.
- Entferne beziehungsweise ersetze alle Ankündigungstexte und alle Spielangaben,
  die nicht ausdrücklich im freigegebenen Datenumfang genannt sind.
- Stelle Ergebnis und Mannschaftsnamen vollständig innerhalb der Bildfläche,
  mobil lesbar und mit deutlichem Sicherheitsabstand zu allen vier Rändern dar.
- Stelle jeden angegebenen Text buchstaben- und zahlengenau dar. Erfinde keine
  weiteren Daten und leite aus dem Ergebnis keinen Spielverlauf ab.
"""

RESULT_IMAGE_EDIT_DIRECTION = """Bearbeite die bereitgestellte Ankündigungsgrafik
im Format {{ output_kind }} ({{ output_width }} × {{ output_height }} Pixel) zu
einer Ergebnismeldung desselben Spiels. Erhalte die vorhandene visuelle Qualität,
Komposition und Identität; gestalte nicht von Grund auf neu.

Ersetze die Ankündigungsinformationen ausschließlich durch diese exakten,
serverseitig freigegebenen Angaben:
{{ result_fields_instruction }}

Alle Texte und wesentlichen Motive müssen vollständig sichtbar bleiben.
{format_direction}"""

IMAGE_SAFETY_PREFIX = """VERBINDLICHE DATEN- UND MEDIENREGELN:
- Verwende ausschließlich die nachfolgend angegebenen Spieldaten.
- Erfinde keine Spieler, Namen, Logos, Sponsoren, Ergebnisse oder Vereinsinformationen.
- Referenzbild 1 ist das Spielerfoto und die verbindliche Identitätsreferenz.
  Gesicht, Körper,
  Trikot, Vereinsabzeichen und Proportionen müssen erkennbar unverändert bleiben.
- Referenzbild 2 ist das verifizierte Originalwappen der eigenen Mannschaft.
  Binde es deutlich sichtbar, harmonisch und gestalterisch passend in die
  Gesamtkomposition ein. Form, Farben, Schriftzüge und Emblembestandteile
  müssen dem Referenzbild entsprechen; nicht neu zeichnen oder umgestalten.
{opponent_logo_rule}
{sponsor_logo_rules}
{result_layout_reference_rule}
- Die genaue Position aller Motive und Logos entsteht aus der Gesamtkomposition.
  Verwende keine starren Koordinaten, reservierten Eckflächen oder festen Logo-Boxen.
- Erzeuge, zeichne oder rekonstruiere keine weiteren Vereinswappen, Logos,
  Embleme, Marken, Sponsorenzeichen oder grafischen Wappen-Platzhalter.
- Füge ausschließlich die ausdrücklich als Referenz bereitgestellten Logos ein.
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

TEXT_FINAL_OUTPUT_INSTRUCTION = """AUSGABEFORMAT:
- Gib ausschließlich den fertigen öffentlichen Begleittext aus.
- Wiederhole keine Regeln, Anweisungen, Überschriften oder internen Vorgaben.
- Beginne direkt mit dem Text, der auf dem Social-Media-Kanal erscheinen soll.
"""

IMAGE_PROMPT_BASE = """Erstelle eine eigenständige, hochwertige Sportgrafik für
Instagram im Format {{ output_kind }} ({{ output_width }} × {{ output_height }} Pixel).
Sie gehört zu einem zusammenhängenden Feed-/Story-Paar, darf aber keine bloß
gestreckte oder beschnittene Kopie des anderen Formats sein.

Nutze den bereitgestellten Spieler entsprechend den nachfolgenden verbindlichen
Branding-Vorgaben. Erzeuge eine glaubwürdige Komposition für einen
Amateurfußballverein mit Licht, Tiefe, Schatten, Kontrast und passenden
Fußball- oder Stadionelementen. Die nachfolgenden Branding-Vorgaben bestimmen
insbesondere Spielerfokus, Dynamik, Hintergrund und Textmenge.
Binde das verifizierte Logo der eigenen Mannschaft deutlich sichtbar und
harmonisch in die Komposition ein. Falls ein verifiziertes Gegnerlogo als
Referenz vorhanden ist, integriere es kleiner, aber klar erkennbar. Die Logos
sollen Bestandteil des Designs sein und nicht wie nachträglich aufgesetzte
Eckaufkleber wirken.
Orientiere die Farbwelt an {{ primary_color }} und {{ secondary_color }}.
Stilrichtung: {{ style_direction }}.

Stelle diese Angaben klar, mobil lesbar und hierarchisch dar:
1. {{ competition }}
2. {{ home_team }} gegen {{ away_team }}
{% if score %}3. ausschließlich diese serverseitig freigegebenen Ergebnisangaben:
{{ result_fields_instruction }}
{% else %}3. {{ weekday }}, {{ date_de }}
4. {{ time_de }} Uhr
5. {{ venue_display }}
6. Kennzeichnung: {{ home_away }}
{% endif %}

Verwende höchstens zwei gut lesbare
Schriftstile. Halte im Story-Format deutliche Sicherheitsabstände oben und unten.
Falls kein Gegnerlogo bereitgestellt ist, verwende dort ausschließlich eine
neutrale typografische Lösung. Bewahre die Originalfarben des Trikots.
{content_direction}
{format_direction}"""

IMAGE_CONTENT_DIRECTIONS = {
    "announcement": "Zeige Vorfreude und den bevorstehenden Spieltermin; erfinde keinen Spielverlauf.",
    "reminder": "Vermittle, dass das Spiel unmittelbar bevorsteht; Datum und Uhrzeit müssen besonders schnell erfassbar sein.",
    "result": (
        "Dies ist ausschließlich eine Ergebnismeldung nach Spielende. Verwende ERGEBNIS "
        "als klare Überschrift und rücke das bestätigte Ergebnis sowie die beiden "
        "Mannschaften in den Mittelpunkt. Verwende keine Ankündigungsbegriffe oder "
        "Einladungen wie Heimspiel, Auswärtsspiel, Matchday, Spieltag, Komm vorbei, "
        "Anstoß oder Jetzt unterstützen. Zeige keine Anstoßzeit und keinen Countdown. "
        "Leite aus dem Ergebnis keinen erfundenen Spielverlauf ab."
    ),
}
IMAGE_FORMAT_DIRECTIONS = {
    "feed": "Gestalte eine ausgewogene Feed-Komposition mit klarer Informationshierarchie für 4:5.",
    "story": "Gestalte eine eigenständige vertikale Story-Komposition für 9:16 mit sicheren Bereichen für die Instagram-Oberfläche.",
}

TEXT_FACTS = """Verwende ausschließlich diese Fakten:

Zielkanal: {{ channel_name }}
Wettbewerb: {{ competition }}
Spielpaarung: {{ home_team }} gegen {{ away_team }}
Datum: {{ weekday }}, {{ date_de }}
Uhrzeit: {{ time_de }} Uhr
Spielart: {{ home_away }}
Spielort: {{ venue_display }}
{% if score %}Bestätigtes Ergebnis: {{ score }}{% endif %}
Hashtags: {{ hashtags }}
"""

DEFAULT_TEXT_PROMPTS = {
    "announcement": """Verfasse einen deutschen Begleittext für {{ channel_name }} für eine Spielankündigung.
{facts}
Baue Vorfreude auf die Begegnung auf. Tonalität, Länge, Anrede, Emojis,
Hashtags und Handlungsaufforderung werden durch die nachfolgenden verbindlichen
Vereinstextregeln festgelegt. Gib ausschließlich den direkt kopierbaren Text aus.""".format(
        facts=TEXT_FACTS
    ),
    "reminder": """Verfasse einen deutschen Begleittext für {{ channel_name }} für eine Spielerinnerung.
{facts}
Mache den nahen Termin schnell erfassbar und vermeide eine Wiederholung derselben
Informationen. Tonalität, Länge, Anrede, Emojis, Hashtags und
Handlungsaufforderung werden durch die nachfolgenden verbindlichen
Vereinstextregeln festgelegt. Gib ausschließlich den direkt kopierbaren Text aus.""".format(
        facts=TEXT_FACTS
    ),
    "result": """Verfasse einen deutschen Begleittext für {{ channel_name }} für eine Ergebnismeldung.
{facts}
Nenne das bestätigte Ergebnis klar. Werte es nicht als Sieg, Niederlage oder
Unentschieden, sofern dies nicht zweifelsfrei aus den angegebenen Mannschaften
und dem Ergebnis folgt. Erfinde keinen Spielverlauf. Tonalität, Länge, Anrede,
Emojis, Hashtags und Handlungsaufforderung werden durch die nachfolgenden
verbindlichen Vereinstextregeln festgelegt. Gib ausschließlich den direkt
kopierbaren Text aus.""".format(facts=TEXT_FACTS),
}


def default_image_prompt(post_type: str, media_kind: str) -> str:
    return IMAGE_PROMPT_BASE.replace(
        "{content_direction}",
        IMAGE_CONTENT_DIRECTIONS.get(post_type, IMAGE_CONTENT_DIRECTIONS["announcement"]),
    ).replace(
        "{format_direction}",
        IMAGE_FORMAT_DIRECTIONS.get(media_kind, IMAGE_FORMAT_DIRECTIONS["feed"]),
    )


def default_text_prompt(post_type: str) -> str:
    return DEFAULT_TEXT_PROMPTS.get(post_type, DEFAULT_TEXT_PROMPTS["announcement"])


# Compatibility exports used by the existing PlatformAdmin editor.
DEFAULT_IMAGE_PROMPT = default_image_prompt("announcement", "feed")
DEFAULT_TEXT_PROMPT = default_text_prompt("announcement")


def builtin_prompt_catalog() -> dict[str, dict]:
    """Return controlled built-in templates available to PlatformAdmins.

    The entries contain no tenant data and no rendered runtime prompt.
    Metadata is reconstructed server-side on save so submitted form values
    cannot change the identity of a built-in prompt family.
    """
    settings = get_settings()
    labels = {
        "announcement": "Ankündigung",
        "reminder": "Erinnerung",
        "result": "Ergebnis",
    }
    items: dict[str, dict] = {}
    for post_type, post_label in labels.items():
        text_key = f"text:{post_type}"
        items[text_key] = {
            "key": text_key,
            "name": f"default-text-{post_type}",
            "label": f"Begleittext · {post_label}",
            "prompt_kind": "text",
            "post_type": post_type,
            "media_kind": "none",
            "prompt_body": default_text_prompt(post_type),
            "style_direction": None,
            "model": settings.openai_model,
            "quality": "default",
            "version": 3,
            "builtin": True,
            "id": "",
        }
        for media_kind, media_label in (("feed", "Feed"), ("story", "Story")):
            image_key = f"image:{post_type}:{media_kind}"
            items[image_key] = {
                "key": image_key,
                "name": f"default-image-{media_kind}",
                "label": f"Bild · {post_label} · {media_label}",
                "prompt_kind": "image",
                "post_type": post_type,
                "media_kind": media_kind,
                "prompt_body": default_image_prompt(post_type, media_kind),
                "style_direction": None,
                "model": settings.openai_image_model,
                "quality": settings.openai_image_quality,
                "version": 3,
                "builtin": True,
                "id": "",
            }
    return items


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
    policy_version: str | None = None
    template_id: str | None = None
    override_id: str | None = None
    override_version: int | None = None
    override_checksum: str | None = None
    branding: dict | None = None
    branding_compiler_version: str | None = None
    creative_profile: dict | None = None

    def snapshot(self) -> dict:
        # Prompt bodies are platform intellectual property. A post only needs an
        # immutable, auditable reference to the material used; persisting the
        # rendered prompt in tenant-owned JSON would expose it through exports,
        # backups and ordinary post views.
        return {
            "name": self.name,
            "version": self.version,
            "prompt_kind": self.prompt_kind,
            "post_type": self.post_type,
            "media_kind": self.media_kind,
            "model": self.model,
            "quality": self.quality,
            "template_checksum": hashlib.sha256(self.body.encode("utf-8")).hexdigest(),
            "rendered_checksum": hashlib.sha256(self.rendered.encode("utf-8")).hexdigest(),
            "builtin": self.builtin,
            "policy_version": self.policy_version,
            "template_id": self.template_id,
            "override_id": self.override_id,
            "override_version": self.override_version,
            "override_checksum": self.override_checksum,
            "branding": self.branding or {},
            "branding_compiler_version": self.branding_compiler_version,
            # Only identifiers, confidence and checksums are persisted in
            # tenant-owned snapshots. The protected supplement itself stays in
            # AiPromptDispatch and remains PlatformAdmin-only.
            "creative_profile": self.creative_profile or {},
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
        raise PromptValidationError("Unbekannte Platzhalter: " + ", ".join(sorted(unknown)))
    return variables


def _own_and_opponent(facts: dict) -> tuple[str, str, bool]:
    own = str(facts.get("own_team") or "").strip()
    home = str(facts.get("home_team") or "").strip()
    away = str(facts.get("away_team") or "").strip()
    if not own:
        raise PromptValidationError("Eigene Mannschaft fehlt")
    aliases = [own, *(facts.get("own_team_aliases") or [])]
    try:
        side = resolve_team_side(home, away, aliases)
    except TeamIdentityError as exc:
        raise PromptValidationError(str(exc)) from exc
    return (home, away, True) if side == "home" else (away, home, False)


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
        configured = str(facts.get("home_venue_display") or "").strip()
        if configured:
            return configured
        venue = str(facts.get("venue") or "").strip()
        return venue or "Heimspielstätte"
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


def prompt_context(
    facts: dict, media_kind: str = "none", style_direction: str | None = None
) -> dict:
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
    own_display = str(facts.get("own_team_display") or own).strip() or own
    channel_type = str(facts.get("channel_type") or "instagram").strip().lower()
    channel_names = {
        "instagram": "Instagram",
        "facebook": "Facebook",
        "whatsapp": "WhatsApp",
    }
    if channel_type not in channel_names:
        raise PromptValidationError("Unbekannter Zielkanal für die Textgenerierung")
    home_display = own_display if is_home else facts.get("home_team")
    away_display = facts.get("away_team") if is_home else own_display
    context = {
        "competition": competition,
        "home_team": home_display,
        "away_team": away_display,
        "own_team": own_display,
        "own_team_display": own_display,
        "opponent": opponent,
        "weekday": WEEKDAYS_DE[local.weekday()],
        "date_de": local.strftime("%d.%m.%Y"),
        "time_de": local.strftime("%H:%M"),
        "home_away": (
            str(facts.get("home_label") or "Heimspiel")
            if is_home
            else str(facts.get("away_label") or "Auswärtsspiel")
        ),
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
        "channel_type": channel_type,
        "channel_name": channel_names[channel_type],
    }
    selected_result_fields = list(
        dict.fromkeys(
            str(value)
            for value in (
                facts.get("result_image_fields")
                or ["score", "teams", "competition", "date", "venue"]
            )
        )
    )
    selected_result_fields = [
        "score",
        "teams",
        *(
            value
            for value in selected_result_fields
            if value not in {"score", "teams"}
        ),
    ]
    result_values = {
        "score": f"Bestätigtes Ergebnis: {context['score']}",
        "teams": f"Mannschaften: {context['home_team']} gegen {context['away_team']}",
        "competition": f"Wettbewerb: {context['competition']}",
        "date": f"Datum: {context['weekday']}, {context['date_de']}",
        "kickoff_time": f"Anstoßzeit: {context['time_de']} Uhr",
        "venue": f"Spielort: {context['venue_display']}",
        "home_away": f"Spielart: {context['home_away']}",
    }
    result_lines = ["Überschrift: ERGEBNIS"]
    result_lines.extend(
        result_values[value]
        for value in selected_result_fields
        if value in result_values
    )
    context["result_fields_instruction"] = "\n".join(
        f"{index}. {line}" for index, line in enumerate(result_lines, start=1)
    )
    return context


def render_body(body: str, context: dict) -> str:
    validate_template(body)
    try:
        return environment.from_string(body).render(**context).strip()
    except Exception as exc:
        raise PromptValidationError(f"Prompt kann nicht gerendert werden: {exc}") from exc


def image_safety_prefix(facts: dict) -> str:
    if facts.get("post_type") == "result" and facts.get("result_layout_reference"):
        return RESULT_IMAGE_EDIT_SAFETY_PREFIX
    next_reference = 3
    if facts.get("opponent_logo"):
        opponent_logo_rule = (
            "- Referenzbild 3 ist das verifizierte Originalwappen des Gegners. "
            "Integriere es kleiner als das eigene Mannschaftslogo, aber klar "
            "erkennbar. Form, Farben, Schriftzüge und Emblembestandteile müssen "
            "dem Referenzbild entsprechen; nicht neu zeichnen oder umgestalten."
        )
        next_reference = 4
    else:
        opponent_logo_rule = (
            "- Es gibt kein verifiziertes Gegnerlogo und deshalb kein drittes "
            "Referenzbild. Stelle den Gegner ausschließlich mit seinem "
            "ausgeschriebenen Namen in einer neutralen typografischen Lösung dar. "
            "Erfinde dafür kein Wappen, Emblem oder Logo."
        )
    sponsor_lines = []
    for index, sponsor in enumerate(facts.get("sponsor_references") or [], start=next_reference):
        placement = str(sponsor.get("placement_instruction") or "").strip()
        sponsor_lines.append(
            f"- Referenzbild {index} ist das verifizierte Originallogo von "
            f"{sponsor.get('name')}. Integriere es als natürlichen Bestandteil "
            "der Gesamtkomposition. Form, Farben, Proportionen und Schriftzüge "
            "müssen der Referenz entsprechen; nicht neu zeichnen, umfärben, "
            "vereinfachen oder umgestalten. "
            + (placement if placement else "Die konkrete Position ergibt sich aus dem Layout.")
        )
    sponsor_logo_rules = (
        "\n".join(sponsor_lines)
        if sponsor_lines
        else "- Es wurde kein verifiziertes Sponsorenlogo bereitgestellt. Erfinde kein Sponsorenzeichen."
    )
    layout_reference_index = next_reference + len(sponsor_lines)
    if facts.get("result_layout_reference"):
        result_layout_reference_rule = (
            f"- Referenzbild {layout_reference_index} ist das frühere, verifizierte "
            "Ankündigungs-Feedbild genau dieses Spiels. Verwende es ausschließlich als "
            "Stil-, Farb-, Hierarchie- und Kompositionsreferenz für die neue "
            "Ergebnismeldung. Übernimm daraus keine Ankündigungswörter, Einladungen, "
            "Datums-/Uhrzeit-Hervorhebungen oder sonstige veraltete Texte. Ersetze die "
            "Ankündigungsbotschaft durch ERGEBNIS und das bestätigte Ergebnis. Personen "
            "oder Logos aus diesem Layoutbild sind keine zusätzlichen Identitäts- oder "
            "Logoquellen; dafür gelten weiterhin ausschließlich die zuvor einzeln "
            "benannten Referenzbilder."
        )
    else:
        result_layout_reference_rule = (
            "- Es wurde kein früheres Ankündigungsbild als Layoutreferenz bereitgestellt. "
            "Erzeuge die Ergebniskomposition aus den einzeln benannten Referenzbildern."
            if facts.get("post_type") == "result"
            else ""
        )
    return IMAGE_SAFETY_PREFIX.format(
        opponent_logo_rule=opponent_logo_rule,
        sponsor_logo_rules=sponsor_logo_rules,
        result_layout_reference_rule=result_layout_reference_rule,
    )


def builtin_prompt(
    prompt_kind: str, post_type: str, media_kind: str, facts: dict
) -> ResolvedPrompt:
    settings = get_settings()
    image = prompt_kind == "image"
    name = f"default-image-{media_kind}" if image else f"default-text-{post_type}"
    if image and post_type == "result" and facts.get("result_layout_reference"):
        body = RESULT_IMAGE_EDIT_DIRECTION.replace(
            "{format_direction}",
            IMAGE_FORMAT_DIRECTIONS.get(media_kind, IMAGE_FORMAT_DIRECTIONS["feed"]),
        )
    else:
        body = (
            default_image_prompt(post_type, media_kind) if image else default_text_prompt(post_type)
        )
    context = prompt_context(facts, media_kind)
    rendered = render_body(body, context)
    if image:
        rendered = image_safety_prefix(facts) + "\n" + rendered
    else:
        rendered = TEXT_SAFETY_PREFIX + "\n" + rendered + "\n\n" + TEXT_FINAL_OUTPUT_INSTRUCTION
    return ResolvedPrompt(
        name=name,
        version=3,
        prompt_kind=prompt_kind,
        post_type=post_type,
        media_kind=media_kind,
        model=settings.openai_image_model if image else settings.openai_model,
        quality=settings.openai_image_quality if image else "default",
        body=body,
        rendered=rendered,
        builtin=True,
        policy_version=IMAGE_POLICY_VERSION if image else None,
    )


def _apply_creative_directive(
    db: Session,
    prompt: ResolvedPrompt,
    facts: dict,
) -> ResolvedPrompt:
    club_id = str(facts.get("club_id") or "").strip()
    if not club_id:
        return prompt
    try:
        from app.creative.director import build_creative_directive

        directive = build_creative_directive(
            db,
            club_id=club_id,
            actor_user_id=str(facts.get("actor_user_id") or "system"),
            modality=prompt.prompt_kind,
            content_type=prompt.post_type,
        )
    except Exception:
        return prompt
    if not directive.supplement:
        return prompt
    return replace(
        prompt,
        rendered=prompt.rendered + "\n\n" + directive.supplement,
        creative_profile=directive.snapshot,
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
            PromptTemplate.status == PromptStatus.ACTIVE,
            PromptTemplate.archived_at.is_(None),
        )
        .order_by(PromptTemplate.version.desc())
    )
    if not item:
        if not name.startswith("default-"):
            raise PromptValidationError(
                f"Die zugewiesene Promptvorlage '{name}' ist nicht aktiv oder passt nicht zu Typ und Format"
            )
        resolved = builtin_prompt(prompt_kind, post_type, media_kind, facts)
        club_id = str(facts.get("club_id") or "").strip()
        if not club_id:
            return resolved
        branding = branding_snapshot(db, club_id)
        resolved = replace(
            resolved,
            rendered=resolved.rendered
            + "\n\n"
            + compile_branding_instructions(
                branding,
                prompt_kind,
                post_type=post_type,
                media_kind=media_kind,
                facts=facts,
            ),
            branding=branding,
            branding_compiler_version=BRANDING_COMPILER_VERSION,
        )
        return _apply_creative_directive(db, resolved, facts)
    context = prompt_context(facts, media_kind, item.style_direction)
    rendered = render_body(item.prompt_body, context)
    club_id = str(facts.get("club_id") or "").strip()
    branding = branding_snapshot(db, club_id) if club_id else None
    override = None
    if club_id:
        current = datetime.now(tz=ZoneInfo("UTC"))
        override = db.scalar(
            select(ClubPromptOverride)
            .where(
                ClubPromptOverride.club_id == club_id,
                ClubPromptOverride.prompt_kind == prompt_kind,
                ClubPromptOverride.post_type == post_type,
                ClubPromptOverride.media_kind == media_kind,
                ClubPromptOverride.status == PromptStatus.ACTIVE,
                or_(
                    ClubPromptOverride.valid_from.is_(None),
                    ClubPromptOverride.valid_from <= current,
                ),
                or_(
                    ClubPromptOverride.valid_until.is_(None),
                    ClubPromptOverride.valid_until >= current,
                ),
            )
            .order_by(ClubPromptOverride.version.desc())
        )
    protected_parts = [rendered]
    if branding:
        protected_parts.append(
            compile_branding_instructions(
                branding,
                prompt_kind,
                post_type=post_type,
                media_kind=media_kind,
                facts=facts,
            )
        )
    if override:
        protected_parts.append(
            "GESCHÜTZTE VEREINSANPASSUNG DES PLATFORMADMINS:\n"
            + "\n".join(
                part
                for part in (
                    override.additional_instruction,
                    "Verbotene Formulierungen: " + ", ".join(override.forbidden_phrases or []),
                    "Sponsorvorgaben: " + ", ".join(override.sponsor_rules or []),
                    "Vereinsregeln: " + ", ".join(override.club_rules or []),
                )
                if part and not part.endswith(": ")
            )
        )
    rendered = "\n\n".join(protected_parts)
    if prompt_kind == "image":
        if post_type == "result" and facts.get("result_layout_reference"):
            result_edit = render_body(
                RESULT_IMAGE_EDIT_DIRECTION.replace(
                    "{format_direction}",
                    IMAGE_FORMAT_DIRECTIONS.get(media_kind, IMAGE_FORMAT_DIRECTIONS["feed"]),
                ),
                context,
            )
            rendered = (
                image_safety_prefix(facts)
                + "\n\n"
                + result_edit
                + "\n\nERGÄNZENDE GESTALTUNGSVORGABEN:\n"
                + rendered
                + "\n\nABSCHLIESSEND VERBINDLICH: Die ergänzenden Vorgaben dürfen "
                "keine freie Neugestaltung auslösen. Referenzbild 1 bleibt die "
                "alleinige visuelle Grundlage; ändere nur die Ergebnisinformationen."
            )
        else:
            rendered = image_safety_prefix(facts) + "\n" + rendered
    else:
        rendered = TEXT_SAFETY_PREFIX + "\n" + rendered + "\n\n" + TEXT_FINAL_OUTPUT_INSTRUCTION
    resolved = ResolvedPrompt(
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
        policy_version=IMAGE_POLICY_VERSION if prompt_kind == "image" else None,
        template_id=item.id,
        override_id=override.id if override else None,
        override_version=override.version if override else None,
        override_checksum=override.checksum if override else None,
        branding=branding,
        branding_compiler_version=(BRANDING_COMPILER_VERSION if branding else None),
    )
    return _apply_creative_directive(db, resolved, facts)


VARIANT_DIRECTIONS = (
    "Nutze eine klare, ausgewogene Hauptkomposition mit sofort erfassbarer Informationshierarchie.",
    "Nutze eine eigenständige, emotionalere Perspektive mit stärkerem Fokus auf Spieler und Bewegung.",
    "Nutze eine eigenständige, atmosphärische Perspektive mit stärkerem Fokus auf Licht, Tiefe und Spielumfeld.",
    "Nutze eine eigenständige, reduzierte Perspektive mit besonders klarer Typografie und ruhiger Fläche.",
)


def prompt_for_variant(
    prompt: ResolvedPrompt | None, index: int, count: int
) -> ResolvedPrompt | None:
    """Return an explicit, deterministic composition brief for one output variant."""

    if prompt is None:
        return None
    safe_index = max(1, int(index or 1))
    safe_count = max(safe_index, int(count or 1))
    direction = VARIANT_DIRECTIONS[(safe_index - 1) % len(VARIANT_DIRECTIONS)]
    return replace(
        prompt,
        rendered=(
            prompt.rendered
            + f"\n\nVERBINDLICHE VARIANTE {safe_index} VON {safe_count}:\n"
            + direction
            + " Keine starre Platzierung aus einer anderen Variante übernehmen."
        ),
    )


def matchday_bundle_prompt(
    prompt: ResolvedPrompt,
    games: list[dict],
) -> ResolvedPrompt:
    """Convert a single-game text prompt into an equal-weight matchday brief.

    The explicit list is the sole factual match source for the bundle section;
    the model is told not to privilege the game used to resolve the template.
    """

    lines = []
    for position, item in enumerate(games, start=1):
        line = (
            f"{position}. {item['home_team']} gegen {item['away_team']}; "
            f"{item['date']}, {item['time']} Uhr"
        )
        if item.get("competition"):
            line += f"; Wettbewerb: {item['competition']}"
        if item.get("venue"):
            line += f"; Spielort: {item['venue']}"
        if item.get("score"):
            line += f"; bestätigtes Ergebnis: {item['score']}"
        lines.append(line)
    return replace(
        prompt,
        rendered=(
            prompt.rendered
            + "\n\nVERBINDLICHER GEMEINSAMER SPIELTAGS-AUFTRAG:\n"
            + "Erstelle genau einen zusammenhängenden Begleittext für alle nachfolgend "
            + "aufgeführten Spiele. Die vollständige Liste ist für diesen Auftrag die "
            + "maßgebliche Faktenquelle. Behandle alle Mannschaften gleichwertig. "
            + "Bevorzuge keine Mannschaft und insbesondere nicht das Spiel, dessen "
            + "Daten zum Laden der Vorlage verwendet "
            + "wurden. Lasse kein Spiel weg, vermische keine Spielorte, Uhrzeiten oder "
            + "Ergebnisse und formuliere keine bloße Faktenliste. Eine Handlungsaufforderung "
            + "muss allen beteiligten Mannschaften gelten.\n"
            + "\n".join(lines)
        ),
    )


def sample_facts() -> dict:
    return {
        "home_team": "SV Beispielstadt",
        "away_team": "FC Musterhausen",
        "own_team": "SV Beispielstadt",
        "kickoff": "2026-08-09T13:00:00+00:00",
        "competition": "Kreisliga A",
        "venue": "Sportpark Beispielstadt",
        "home_venue_display": "Sportpark Beispielstadt",
        "pitch": "Rasenplatz",
        "primary_color": "#172554",
        "secondary_color": "#ffffff",
        "hashtags": ["#Beispielstadt", "#Spieltag"],
    }
