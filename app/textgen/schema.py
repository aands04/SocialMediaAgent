from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.prompts.service import prompt_context
from app.textgen.service import GeneratedText

TEXT_GENERATION_MODE_AI = "ai"
TEXT_GENERATION_MODE_SCHEMA = "schema"
TEXT_GENERATION_MODES = frozenset({TEXT_GENERATION_MODE_AI, TEXT_GENERATION_MODE_SCHEMA})
SCHEMA_POST_TYPES = frozenset({"announcement", "reminder", "result"})


def configured_text_generation_mode(
    rules: Mapping[str, object] | None,
    post_type: str,
) -> str:
    """Return the validated per-post-type text strategy.

    Missing settings retain the historic AI/provider path. Unknown persisted
    values fail closed instead of silently selecting a different strategy.
    """

    if post_type not in SCHEMA_POST_TYPES:
        raise ValueError("Unbekannte Textart")
    value = str((rules or {}).get(f"text_generation_mode_{post_type}", "ai"))
    if value not in TEXT_GENERATION_MODES:
        raise ValueError(f"Ungültige Texterstellung für {post_type}")
    return value


def _competition_phrase(competition: str) -> str:
    folded = competition.casefold()
    if "liga" in folded or "klasse" in folded:
        return f"in der {competition}"
    if "pokal" in folded:
        return f"im {competition}"
    return f"im Wettbewerb {competition}"


def generate_schema_text(data: Mapping[str, object], post_type: str) -> GeneratedText:
    """Build a deterministic caption exclusively from validated match facts."""

    if post_type not in SCHEMA_POST_TYPES:
        raise ValueError("Für diese Textart ist kein Standardschema vorhanden")
    context = prompt_context(dict(data), "none")
    team_short = str(data.get("team_short") or context["own_team_display"]).strip()
    competition = str(context["competition"])
    match_details = (
        f"🆚 {context['opponent']}\n"
        f"📅 {context['weekday']}, {context['date_de']}\n"
        f"⏰ {context['time_de']} Uhr\n"
        f"📍 {context['venue_display']}"
    )
    competition_phrase = _competition_phrase(competition)

    if post_type == "announcement":
        text = (
            "⚽ Nächstes Spiel\n\n"
            f"Am {context['weekday']} steht für unsere {team_short} das nächste Spiel "
            f"{competition_phrase} an.\n\n"
            f"{match_details}\n\n"
            "Wir freuen uns auf das Spiel und auf eure Unterstützung am Spielfeldrand! ⚽"
        )
    elif post_type == "reminder":
        text = (
            "⚽ Spielerinnerung\n\n"
            f"Am {context['weekday']} steht für unsere {team_short} das nächste Spiel "
            f"{competition_phrase} an.\n\n"
            f"{match_details}\n\n"
            "Kommt vorbei und unterstützt unsere Mannschaft am Spielfeldrand! ⚽"
        )
    else:
        score = str(context.get("score") or "").strip()
        if not score:
            raise ValueError("Für das Ergebnisschema fehlt das bestätigte Ergebnis")
        text = (
            "⚽ Endergebnis\n\n"
            f"Das Spiel unserer {team_short} {competition_phrase} ist beendet.\n\n"
            f"🆚 {context['home_team']} – {context['away_team']}\n"
            f"🔢 {score}\n"
            f"📅 {context['weekday']}, {context['date_de']}\n"
            f"📍 {context['venue_display']}\n\n"
            "Danke für eure Unterstützung! ⚽"
        )

    return GeneratedText(
        text=text,
        model="schema",
        prompt_version=f"standard-schema-{post_type}-v1",
    )


def generate_matchday_schema_text(
    games: Sequence[Mapping[str, object]], post_type: str
) -> GeneratedText:
    """Build the deterministic shared caption used by grouped matchdays."""

    if post_type not in {"announcement", "result"}:
        raise ValueError("Für diese Textart ist kein gemeinsames Spieltagsschema vorhanden")
    if not games:
        raise ValueError("Das gemeinsame Textschema benötigt mindestens ein Spiel")

    blocks: list[str] = []
    for index, game in enumerate(games, start=1):
        block = (
            f"{index}. {game['home_team']} – {game['away_team']}\n"
            f"📅 {game['date']}\n"
            f"⏰ {game['time']} Uhr"
        )
        if game.get("venue"):
            block += f"\n📍 {game['venue']}"
        if post_type == "result":
            score = str(game.get("score") or "").strip()
            if not score:
                raise ValueError("Für das Ergebnisschema fehlt ein bestätigtes Ergebnis")
            block += f"\n🔢 {score}"
        blocks.append(block)

    if post_type == "announcement":
        heading = "⚽ Nächste Spiele"
        introduction = "Für unsere Mannschaften stehen folgende Spiele an:"
        closing = "Wir freuen uns auf die Spiele und auf eure Unterstützung! ⚽"
    else:
        heading = "⚽ Ergebnisse"
        introduction = "Die Ergebnisse unserer Mannschaften im Überblick:"
        closing = "Danke für eure Unterstützung! ⚽"

    return GeneratedText(
        text=f"{heading}\n\n{introduction}\n\n" + "\n\n".join(blocks) + f"\n\n{closing}",
        model="schema",
        prompt_version=f"standard-schema-matchday-{post_type}-v1",
    )
