from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.branding.service import STANDARD_FONTS

BRANDING_COMPILER_VERSION = "effective-branding-v1"

GRAPHIC_STYLE_LABELS = {
    "classic": "klassisch und traditionsbewusst",
    "modern": "modern und klar",
    "dynamic": "dynamisch und bewegungsbetont",
    "minimal": "minimalistisch und reduziert",
    "emotional": "emotional und nahbar",
    "stadium": "atmosphärischer Stadion-Look",
}
IMAGE_EFFECT_LABELS = {
    "emotional": "emotional",
    "modern": "modern",
    "dynamic": "dynamisch",
    "calm": "ruhig",
    "premium": "hochwertig",
    "classic": "klassisch",
}
BACKGROUND_LABELS = {
    "club-color": "Vereinsfarben als Hintergrund",
    "gradient": "Farbverlauf aus den Vereinsfarben",
    "photo": "fotografischer Hintergrund",
    "stadium": "Stadion- oder Flutlichtatmosphäre",
    "abstract": "abstrakte grafische Formen",
    "cutout": "freigestelltes Hauptmotiv mit ruhigem Hintergrund",
}
ALIGNMENT_LABELS = {"left": "links", "center": "zentriert", "right": "rechts"}
POSITION_LABELS = {
    "top-left": "im oberen linken Bildbereich",
    "top-center": "im oberen mittleren Bildbereich",
    "top-right": "im oberen rechten Bildbereich",
    "center-left": "im mittleren linken Bildbereich",
    "center": "im Bildzentrum",
    "center-right": "im mittleren rechten Bildbereich",
    "bottom-left": "im unteren linken Bildbereich",
    "bottom-center": "im unteren mittleren Bildbereich",
    "bottom-right": "im unteren rechten Bildbereich",
}
SPONSOR_POSITION_LABELS = {
    "auto": "an einer zur Gesamtkomposition passenden Stelle",
    "top": "bevorzugt im oberen Bildbereich",
    "bottom": "bevorzugt im unteren Bildbereich",
    "left": "bevorzugt im linken Bildbereich",
    "right": "bevorzugt im rechten Bildbereich",
    "footer": "bevorzugt dezent im unteren Bildbereich",
}
TONE_LABELS = {
    "factual": "sachlich und klar",
    "emotional": "emotional und vereinsnah",
    "motivating": "motivierend und aktivierend",
    "casual": "locker und natürlich",
    "professional": "professionell und hochwertig",
    "traditional": "traditionsbewusst und gemeinschaftlich",
}
ADDRESS_LABELS = {
    "du": "Sprich einzelne Leser mit „du“ an.",
    "ihr": "Sprich die Gemeinschaft mit „ihr“ an.",
    "neutral": "Formuliere ohne direkte persönliche Anrede.",
}
TEXT_LENGTH_LABELS = {
    "short": "kurz und kompakt",
    "medium": "mittellang mit gut lesbaren Absätzen",
    "detailed": "ausführlich, aber ohne Wiederholungen",
}
EMOJI_LABELS = {
    "none": "Verwende keine Emojis.",
    "sparse": "Verwende höchstens ein bis zwei passende Emojis.",
    "normal": "Verwende einige passende Emojis, ohne den Text zu überladen.",
    "frequent": "Emojis dürfen deutlich eingesetzt werden, müssen aber lesbar bleiben.",
}
CTA_LABELS = {
    "support": "Fordere zur Unterstützung der beteiligten Mannschaft auf.",
    "share": "Fordere knapp zum Teilen des Beitrags auf.",
    "comment": "Fordere zu einem passenden Kommentar auf, ohne Fakten zu erfinden.",
    "attend": "Lade zum Besuch des Spiels ein.",
    "none": "Verwende keine Handlungsaufforderung.",
}


def _value(mapping: dict[str, str], raw: Any, fallback: str) -> str:
    return mapping.get(str(raw or ""), fallback)


def _as_date(value: datetime | date | str | None) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value:
        try:
            return datetime.fromisoformat(str(value)).date()
        except ValueError:
            pass
    return datetime.now().date()


def applicable_sponsors(
    snapshot: dict,
    *,
    team_id: str,
    post_type: str,
    media_kind: str,
    at: datetime | date | str | None,
) -> list[dict]:
    """Return only sponsor declarations applicable to this exact output.

    The function consumes validated branding data. It deliberately does not
    resolve media files; tenant-aware database resolution remains in the post
    service where the current club can be enforced.
    """

    current = _as_date(at).isoformat()
    selected: list[dict] = []
    for raw in (snapshot.get("text") or {}).get("sponsors") or []:
        sponsor = dict(raw) if isinstance(raw, dict) else {}
        if media_kind == "feed" and not sponsor.get("use_feed", True):
            continue
        if media_kind == "story" and not sponsor.get("use_story", True):
            continue
        if post_type in {"announcement", "reminder"} and not sponsor.get(
            "use_announcement", True
        ):
            continue
        if post_type == "result" and not sponsor.get("use_result", True):
            continue
        team_ids = {str(value) for value in sponsor.get("team_ids") or []}
        if team_ids and team_id not in team_ids:
            continue
        if sponsor.get("valid_from") and current < str(sponsor["valid_from"]):
            continue
        if sponsor.get("valid_until") and current > str(sponsor["valid_until"]):
            continue
        selected.append(sponsor)
    return selected


def team_display_name(snapshot: dict, team_id: str, fallback: str) -> tuple[str, str]:
    for item in (snapshot.get("text") or {}).get("team_names") or []:
        if str(item.get("team_id") or "") != team_id or not item.get("active", True):
            continue
        return (
            str(item.get("display_name") or fallback).strip() or fallback,
            str(item.get("short_name") or "").strip(),
        )
    return fallback, ""


def compile_branding_instructions(
    snapshot: dict,
    prompt_kind: str,
    *,
    post_type: str,
    media_kind: str,
    facts: dict | None = None,
) -> str:
    """Translate validated branding values into explicit provider rules.

    Raw JSON is intentionally not sent to the provider. The compiler emits a
    controlled vocabulary so tenant values cannot become system directives.
    """

    facts = facts or {}
    if prompt_kind == "image":
        return _compile_image(snapshot, post_type, media_kind, facts)
    return _compile_text(snapshot, post_type, facts)


def _compile_image(snapshot: dict, post_type: str, media_kind: str, facts: dict) -> str:
    image = snapshot.get("image") or {}
    settings = image.get("story_settings" if media_kind == "story" else "feed_settings") or {}
    accents = ", ".join(image.get("accent_colors") or []) or "keine zusätzliche Akzentfarbe"
    effects = ", ".join(
        _value(IMAGE_EFFECT_LABELS, item, str(item)) for item in image.get("image_effects") or []
    ) or "ausgewogen"
    primary_font = STANDARD_FONTS.get(str(image.get("primary_standard_font") or "system"), {})
    secondary_font = STANDARD_FONTS.get(
        str(image.get("secondary_standard_font") or "system"), {}
    )
    margins = {
        "tight": "enge, aber vollständig lesbare Sicherheitsabstände",
        "normal": "normale Sicherheitsabstände",
        "generous": "großzügige Sicherheitsabstände",
    }.get(str(image.get("safe_margins") or "normal"), "normale Sicherheitsabstände")
    ratio = max(0, min(100, int(image.get("player_background_ratio") or 60)))
    lines = [
        "VERBINDLICHE, SERVERSEITIG VALIDIERTE VEREINSGESTALTUNG:",
        "- Diese aktuellen Branding-Werte haben bei stilistischen Widersprüchen Vorrang vor allgemeineren Vorgaben der Promptvorlage.",
        f"- Farbwelt: Primärfarbe {image.get('primary_color') or '#172554'}, "
        f"Sekundärfarbe {image.get('secondary_color') or '#FFFFFF'}, Akzentfarben: {accents}.",
        f"- Grafikstil: {_value(GRAPHIC_STYLE_LABELS, image.get('graphic_style'), 'modern und klar')}; Bildwirkung: {effects}.",
        f"- Hintergrund: {_value(BACKGROUND_LABELS, image.get('background_style'), 'Farbverlauf aus den Vereinsfarben')}.",
        f"- Textausrichtung bevorzugt {_value(ALIGNMENT_LABELS, image.get('text_alignment'), 'links')}.",
        f"- Eigenes Vereinslogo ungefähr {_value(POSITION_LABELS, image.get('logo_placement'), 'an einer harmonischen Stelle')} integrieren. "
        "Das ist eine Gestaltungspräferenz, keine feste Koordinate oder reservierte Logofläche.",
        f"- Spieler bevorzugt {_value(POSITION_LABELS, image.get('player_position'), 'im mittleren rechten Bildbereich')} anordnen; "
        f"Spielerfokus ungefähr {ratio} %, Hintergrundfokus ungefähr {100 - ratio} %.",
        f"- Verwende {margins} und eine {_value({'little':'geringe','normal':'mittlere','detailed':'ausführliche'}, image.get('image_text_amount'), 'mittlere')} Textmenge.",
        f"- Dynamik: {_value({'calm':'ruhig','balanced':'ausgewogen','dynamic':'deutlich dynamisch'}, image.get('dynamics'), 'ausgewogen')}.",
        f"- Individualisierung: {_value({'standard':'konsistentes Standarddesign','club':'klar vereinsspezifisch','strong':'stark individuell'}, image.get('individualization'), 'klar vereinsspezifisch')}.",
        f"- Bevorzugte Schriftwirkung: {primary_font.get('label', 'Systemschrift')} für Haupttexte und "
        f"{secondary_font.get('label', 'Systemschrift')} für ergänzende Informationen. Keine Schriftdatei erfinden oder imitieren.",
    ]
    if post_type == "result":
        selected_fields = list(
            dict.fromkeys(
                str(value)
                for value in (
                    image.get("result_image_fields")
                    or ["score", "teams", "competition", "date", "venue"]
                )
            )
        )
        labels = {
            "score": "bestätigtes Ergebnis",
            "teams": "beide Mannschaftsnamen",
            "competition": "Wettbewerb",
            "date": "Spieldatum",
            "kickoff_time": "Anstoßzeit",
            "venue": "Spielort",
            "home_away": "Heim-/Auswärtskennzeichnung",
        }
        selected_fields = [
            "score",
            "teams",
            *(value for value in selected_fields if value not in {"score", "teams"}),
        ]
        selected_labels = [labels[value] for value in selected_fields if value in labels]
        excluded_labels = [
            label for key, label in labels.items() if key not in selected_fields
        ]
        lines.append(
            "- Auf Ergebnisbildern ausschließlich diese Spieldaten zeigen: "
            + ", ".join(selected_labels)
            + "."
        )
        if excluded_labels:
            lines.append(
                "- Diese Spieldaten auf Ergebnisbildern weglassen: "
                + ", ".join(excluded_labels)
                + "."
            )
        result_extra = str(image.get("result_image_extra_rules") or "").strip()
        if result_extra:
            lines.append(
                "- Ergänzende validierte Vereinsvorgabe für Ergebnisbilder: "
                + result_extra
            )
    if media_kind == "story":
        lines.append(
            f"- Story-Sicherheitsbereiche: oben etwa {int(settings.get('safe_top', 12))} %, "
            f"unten etwa {int(settings.get('safe_bottom', 15))} % frei von wichtigen Texten halten."
        )
        lines.append(
            "- Call-to-Action sichtbar einplanen."
            if settings.get("show_call_to_action", True)
            else "- Keine Call-to-Action-Fläche einplanen."
        )
        if settings.get("countdown_area"):
            lines.append("- Einen ruhigen, frei gestalteten Bereich für einen Countdown vorsehen.")
    else:
        if post_type == "result" and settings.get("highlight_result", True):
            lines.append("- Das bestätigte Ergebnis als wichtigstes Textelement hervorheben.")
    if not settings.get("use_player_image", True):
        lines.append("- Das Spielerreferenzbild nicht als Hauptmotiv verwenden; es dient nur der Identitätskontrolle.")
    if not settings.get("show_club_logo", True):
        lines.append("- Das Vereinslogo nicht dominant darstellen, aber die verbindliche Referenzregel weiterhin beachten.")
    allowed = image.get("allowed_elements") or []
    unwanted = image.get("unwanted_elements") or []
    forbidden_colors = image.get("forbidden_colors") or []
    if allowed:
        lines.append("- Erlaubte Gestaltungselemente: " + ", ".join(allowed) + ".")
    if unwanted:
        lines.append("- Nicht verwenden: " + ", ".join(unwanted) + ".")
    if forbidden_colors:
        lines.append("- Folgende Farben nicht als zusätzliche Gestaltungsfarben verwenden: " + ", ".join(forbidden_colors) + ".")
    extra = str(settings.get("extra_rules") or "").strip()
    if extra:
        lines.append("- Ergänzende validierte Vereinsvorgabe: " + extra)
    sponsor_refs = facts.get("sponsor_references") or []
    if sponsor_refs:
        lines.append("- Die nachfolgend beschriebenen Sponsorenlogos sind verbindliche Referenzbilder und müssen in die natürliche Gesamtkomposition einfließen:")
        for sponsor in sponsor_refs:
            placement = _value(
                SPONSOR_POSITION_LABELS,
                sponsor.get("placement"),
                "an einer zur Gesamtkomposition passenden Stelle",
            )
            lines.append(
                f"  - {sponsor.get('name')}: {placement}. Die genaue Position bestimmt die Bildkomposition; keine feste Box, Ecke oder Koordinate erzwingen."
            )
    else:
        lines.append("- Keine Sponsorenmarke oder kein Sponsorenlogo erfinden oder ergänzen.")
    return "\n".join(lines)


def _compile_text(snapshot: dict, post_type: str, facts: dict) -> str:
    text = snapshot.get("text") or {}
    lines = [
        "VERBINDLICHE, SERVERSEITIG VALIDIERTE VEREINSTEXTREGELN:",
        "- Diese aktuellen Branding-Werte haben bei stilistischen Widersprüchen Vorrang vor allgemeineren Vorgaben der Promptvorlage.",
        "- Tonalität: " + _value(TONE_LABELS, text.get("tone"), "sachlich und klar") + ".",
        "- " + _value(ADDRESS_LABELS, text.get("address_style"), ADDRESS_LABELS["neutral"]),
        "- Textlänge: " + _value(TEXT_LENGTH_LABELS, text.get("text_length"), "mittellang") + ".",
        "- " + _value(EMOJI_LABELS, text.get("emoji_usage"), EMOJI_LABELS["sparse"]),
    ]
    cta_type = str(text.get("cta_type") or "none")
    if cta_type == "custom" and text.get("cta_custom"):
        lines.append("- Handlungsaufforderung sinngemäß verwenden: " + str(text["cta_custom"]))
    else:
        lines.append("- " + CTA_LABELS.get(cta_type, CTA_LABELS["none"]))
    hashtags = list(text.get("hashtags") or facts.get("hashtags") or [])
    max_hashtags = max(0, min(30, int(text.get("max_hashtags", 10))))
    if hashtags and max_hashtags:
        lines.append(
            f"- Verwende höchstens {max_hashtags} Hashtags aus dieser freigegebenen Liste: "
            + " ".join(hashtags[:max_hashtags])
        )
    else:
        lines.append("- Verwende keine Hashtags.")
    mentions = list(text.get("mentions") or [])
    if mentions:
        lines.append("- Zulässige allgemeine Erwähnungen: " + " ".join(mentions) + ".")
    typical = text.get("typical_phrases") or []
    unwanted = text.get("unwanted_phrases") or []
    if typical:
        lines.append("- Typische Formulierungen dürfen sinngemäß einfließen: " + " | ".join(typical))
    if unwanted:
        lines.append("- Folgende Formulierungen nicht verwenden: " + " | ".join(unwanted))
    sponsor_refs = facts.get("sponsor_references") or []
    sponsor_mentions = [
        str(item.get("instagram_mention") or "").strip()
        for item in sponsor_refs
        if item.get("instagram_mention")
    ]
    sponsor_mentions.extend(text.get("sponsor_mentions") or [])
    sponsor_mentions = list(dict.fromkeys(sponsor_mentions))
    if sponsor_mentions:
        lines.append("- Sponsorenerwähnungen, sofern natürlich passend: " + " ".join(sponsor_mentions) + ".")
    if post_type == "result":
        lines.append("- Das bestätigte Ergebnis klar nennen; keinen Spielverlauf daraus ableiten.")
    elif post_type == "reminder":
        lines.append("- Den nahen Spieltermin deutlich und ohne Wiederholung ankündigen.")
    else:
        lines.append("- Spieltermin und Begegnung einladend ankündigen.")
    return "\n".join(lines)
