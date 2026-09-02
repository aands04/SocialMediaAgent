import re
import unicodedata
from collections.abc import Iterable


class TeamIdentityError(ValueError):
    pass


MAX_IDENTITY_ALIASES = 20
MAX_IDENTITY_ALIAS_LENGTH = 160


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


def validate_identity_aliases(value: object) -> tuple[str, ...]:
    """Return a bounded, deterministic list of explicitly configured aliases."""

    if value is None:
        return ()
    if isinstance(value, str):
        candidates = value.splitlines()
    elif isinstance(value, (list, tuple)):
        candidates = value
    else:
        raise TeamIdentityError("Mannschafts-Aliase müssen als Liste gespeichert werden")

    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, str):
            raise TeamIdentityError("Mannschafts-Aliase müssen Textwerte sein")
        alias = candidate.strip()
        if not alias:
            continue
        if len(alias) > MAX_IDENTITY_ALIAS_LENGTH:
            raise TeamIdentityError(
                f"Mannschafts-Aliase dürfen höchstens {MAX_IDENTITY_ALIAS_LENGTH} Zeichen lang sein"
            )
        normalized = normalize_team_name(alias)
        if not normalized:
            continue
        if normalized in seen:
            continue
        if len(result) >= MAX_IDENTITY_ALIASES:
            raise TeamIdentityError(
                f"Es dürfen höchstens {MAX_IDENTITY_ALIASES} Mannschafts-Aliase gespeichert werden"
            )
        seen.add(normalized)
        result.append(alias)
    return tuple(result)


def team_aliases(team) -> tuple[str, ...]:
    values = [
        getattr(team, "display_name", None),
        getattr(team, "internal_name", None),
        getattr(team, "short_name", None),
        getattr(team, "club", None),
    ]
    rules = getattr(team, "rules", None) or {}
    values.extend(validate_identity_aliases(rules.get("identity_aliases")))

    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        alias = str(value or "").strip()
        normalized = normalize_team_name(alias)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(alias)
    return tuple(result)


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
