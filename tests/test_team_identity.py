from types import SimpleNamespace

import pytest

from app.games.identity import (
    MAX_IDENTITY_ALIAS_LENGTH,
    MAX_IDENTITY_ALIASES,
    TeamIdentityError,
    opponent_for_game,
    resolve_team_side,
    team_aliases,
    team_name_variants,
    validate_identity_aliases,
)
from app.prompts.service import _own_and_opponent


def test_first_team_matches_club_name_without_squad_suffix():
    team = SimpleNamespace(
        display_name="SV Ehlen I",
        internal_name="Erste Mannschaft",
        short_name="I",
        club="SV Ehlen",
        rules={},
    )
    game = SimpleNamespace(home_team="SV Ehlen", away_team="Testverein Kassel")

    assert resolve_team_side(game.home_team, game.away_team, team_aliases(team)) == "home"
    assert opponent_for_game(game, team) == "Testverein Kassel"
    assert "sv ehlen" in team_name_variants("SV Ehlen I")


def test_prompt_uses_all_team_aliases_and_preserves_pairing_name():
    own, opponent, is_home = _own_and_opponent(
        {
            "own_team": "SV Ehlen I",
            "own_team_aliases": ["SV Ehlen", "Erste Mannschaft"],
            "home_team": "SV Ehlen",
            "away_team": "Testverein Kassel",
        }
    )

    assert (own, opponent, is_home) == ("SV Ehlen", "Testverein Kassel", True)


def test_team_side_rejects_ambiguous_or_unrelated_pairing():
    with pytest.raises(TeamIdentityError):
        resolve_team_side("SV Ehlen", "SV Ehlen II", ["SV Ehlen I"])
    with pytest.raises(TeamIdentityError):
        resolve_team_side("FC A", "FC B", ["SV Ehlen I", "SV Ehlen"])


def test_explicit_alias_resolves_an_otherwise_unknown_provider_name():
    team = SimpleNamespace(
        display_name="C1",
        internal_name="c1",
        short_name="C1",
        club="SV Ehlen",
        rules={"identity_aliases": ["JSG Ehlen/Hoof C-Junioren"]},
    )

    assert (
        resolve_team_side(
            "JSG Ehlen/Hoof C-Junioren",
            "JSG Gegner C-Junioren",
            team_aliases(team),
        )
        == "home"
    )


def test_explicit_alias_still_rejects_ambiguous_and_unknown_pairings():
    aliases = ["JSG Ehlen/Hoof C-Junioren"]

    with pytest.raises(TeamIdentityError):
        resolve_team_side(
            "JSG Ehlen/Hoof C-Junioren",
            "JSG Ehlen/Hoof C-Junioren",
            aliases,
        )
    with pytest.raises(TeamIdentityError):
        resolve_team_side("JSG Unbekannt A", "JSG Unbekannt B", aliases)


def test_explicit_aliases_are_trimmed_deduplicated_and_bounded():
    assert validate_identity_aliases("  Alias A  \nAlias A\nalias a\n\nAlias B") == (
        "Alias A",
        "Alias B",
    )
    with pytest.raises(TeamIdentityError):
        validate_identity_aliases(["x" * (MAX_IDENTITY_ALIAS_LENGTH + 1)])
    with pytest.raises(TeamIdentityError):
        validate_identity_aliases([f"Alias {index}" for index in range(MAX_IDENTITY_ALIASES + 1)])
