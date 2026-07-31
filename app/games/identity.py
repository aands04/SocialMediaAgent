import re
import unicodedata
from collections.abc import Iterable


class TeamIdentityError(ValueError):
    pass


def normalize_team_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", value)
    value = value.casefold().replace("&", " und ")
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return " ".join(value.replace("_", " ").split())


def team_name_variants(value: str) -> set[str]:
    """Return conservative aliases for squad suffixes, never arbitrary substrings."""
    normalized = normalize_team_name(value)
    if not normalized:
        return set()
    variants = {normalized}
    suffixes = (
        r"\s+(?:i|ii|iii|iv|v|vi)$",
        r"\s+[1-6]$",
        r"\s+(?:erste|zweite|dritte|vierte|fünfte|sechste)\s+mannschaft$",
        r"\s+[1-6]\s+mannschaft$",
    )
    for pattern in suffixes:
        base = re.sub(pattern, "", normalized).strip()
        if base and base != normalized:
            variants.add(base)
    return variants


def team_aliases(team) -> tuple[str, ...]:
    values = (
        getattr(team, "display_name", None),
        getattr(team, "internal_name", None),
        getattr(team, "short_name", None),
        getattr(team, "club", None),
    )
    return tuple(str(value).strip() for value in values if str(value or "").strip())


def resolve_team_side(home_team: str, away_team: str, aliases: Iterable[str]) -> str:
    own_variants: set[str] = set()
    for alias in aliases:
        own_variants.update(team_name_variants(alias))
    home_matches = bool(team_name_variants(home_team) & own_variants)
    away_matches = bool(team_name_variants(away_team) & own_variants)
    if home_matches == away_matches:
        raise TeamIdentityError(
            "Eigene Mannschaft konnte der Spielpaarung nicht eindeutig zugeordnet werden"
        )
    return "home" if home_matches else "away"


def opponent_for_game(game, team) -> str:
    side = resolve_team_side(game.home_team, game.away_team, team_aliases(team))
    return game.away_team if side == "home" else game.home_team
