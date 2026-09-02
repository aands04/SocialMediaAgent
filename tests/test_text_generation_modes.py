from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.models import Game, Team
from app.posts.service import create_post
from app.rendering.service import Renderer
from app.textgen.schema import (
    configured_text_generation_mode,
    generate_matchday_schema_text,
    generate_schema_text,
)
from app.textgen.service import GeneratedText


def _facts(*, post_type: str = "announcement", score: str | None = None) -> dict:
    return {
        "home_team": "JSG Warmetal/Wolfhagen 1",
        "away_team": "JSG Willingshausen 1",
        "own_team": "JSG Warmetal/Wolfhagen 1",
        "own_team_display": "JSG Warmetal/Wolfhagen 1",
        "own_team_aliases": ["JSG Warmetal/Wolfhagen 1"],
        "team_short": "C1",
        "kickoff": "2026-09-05T13:00:00+00:00",
        "competition": "Gruppenliga",
        "venue": "Zierenberg-Oelshausen",
        "home_venue_display": "RP Zierenberg-Oelshausen",
        "pitch": "Rasenplatz",
        "post_type": post_type,
        "score": score,
    }


def _logo_snapshot() -> dict:
    return {
        "team": {
            "id": "verified-logo",
            "version": 1,
            "checksum": "0" * 64,
            "verified": True,
        },
        "opponent": {"fallback": True, "verified": False, "disabled": False},
    }


def _graph(db, *, mode: str) -> tuple[Team, Game]:
    team = Team(
        internal_name="c1-text-mode",
        display_name="JSG Warmetal/Wolfhagen 1",
        short_name="C1",
        slug="c1-text-mode",
        club="SV Ehlen",
        fussball_url="https://example.invalid/c1-text-mode",
        media_subdir="c1-text-mode",
        rules={"text_generation_mode_announcement": mode},
    )
    db.add(team)
    db.flush()
    game = Game(
        team_id=team.id,
        provider="fussball.de",
        external_id=f"text-mode-{mode}",
        home_team=team.display_name,
        away_team="JSG Willingshausen 1",
        kickoff=datetime(2026, 9, 5, 13, 0, tzinfo=timezone.utc),
        competition="Gruppenliga",
        venue="RP Zierenberg-Oelshausen",
        pitch="Rasenplatz",
        source_url=team.fussball_url,
    )
    db.add(game)
    db.commit()
    return team, game


class RecordingAITextGenerator:
    is_ai = True

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def generate(self, data: dict):
        self.calls.append(data)
        raise AssertionError("Das Textschema darf keinen KI-Aufruf auslösen")


class SuccessfulAITextGenerator:
    is_ai = True

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def generate(self, data: dict) -> GeneratedText:
        self.calls.append(data)
        return GeneratedText("Von der KI erzeugter Testtext", "test-ai", "test-ai-v1")


def test_announcement_standard_schema_matches_requested_structure() -> None:
    result = generate_schema_text(_facts(), "announcement")

    assert result.text == (
        "⚽ Nächstes Spiel\n\n"
        "Am Samstag steht für unsere C1 das nächste Spiel in der Gruppenliga an.\n\n"
        "🆚 JSG Willingshausen 1\n"
        "📅 Samstag, 05.09.2026\n"
        "⏰ 15:00 Uhr\n"
        "📍 RP Zierenberg-Oelshausen\n\n"
        "Wir freuen uns auf das Spiel und auf eure Unterstützung am Spielfeldrand! ⚽"
    )
    assert result.model == "schema"
    assert result.prompt_version == "standard-schema-announcement-v1"


@pytest.mark.parametrize(
    ("post_type", "heading", "prompt_version"),
    [
        ("reminder", "⚽ Spielerinnerung", "standard-schema-reminder-v1"),
        ("result", "⚽ Endergebnis", "standard-schema-result-v1"),
    ],
)
def test_each_supported_text_type_has_a_versioned_schema(
    post_type: str, heading: str, prompt_version: str
) -> None:
    result = generate_schema_text(
        _facts(post_type=post_type, score="2:1" if post_type == "result" else None),
        post_type,
    )

    assert result.text.startswith(heading)
    assert result.prompt_version == prompt_version


def test_result_schema_requires_a_confirmed_score() -> None:
    with pytest.raises(ValueError, match="bestätigte Ergebnis"):
        generate_schema_text(_facts(post_type="result"), "result")


def test_schema_uses_the_actual_opponent_for_an_away_game() -> None:
    facts = _facts()
    facts.update(
        {
            "home_team": "JSG Willingshausen 1",
            "away_team": "JSG Warmetal/Wolfhagen 1",
            "home_venue_display": "",
            "venue": "RP Willingshausen",
        }
    )

    result = generate_schema_text(facts, "announcement")

    assert "🆚 JSG Willingshausen 1" in result.text
    assert "📍 RP Willingshausen" in result.text


def test_text_generation_modes_default_to_ai_and_reject_unknown_values() -> None:
    assert configured_text_generation_mode({}, "announcement") == "ai"
    assert (
        configured_text_generation_mode(
            {"text_generation_mode_announcement": "schema"}, "announcement"
        )
        == "schema"
    )
    with pytest.raises(ValueError, match="Ungültige Texterstellung"):
        configured_text_generation_mode(
            {"text_generation_mode_announcement": "provider-controlled-value"},
            "announcement",
        )


def test_schema_mode_bypasses_ai_generator_and_is_frozen_in_post_snapshot(db, tmp_path) -> None:
    team, game = _graph(db, mode="schema")
    generator = RecordingAITextGenerator()

    post = create_post(
        db,
        game,
        team,
        generator,
        Renderer(tmp_path / "generated"),
        logo_snapshot=_logo_snapshot(),
    )

    assert generator.calls == []
    assert post.text.startswith("⚽ Nächstes Spiel")
    assert post.design_snapshot["mode"]["text"] == "schema"
    assert post.design_snapshot["prompts"]["text"] is None
    assert post.design_snapshot["text_generation"] == {
        "model": "schema",
        "prompt_version": "standard-schema-announcement-v1",
        "tokens": None,
        "shared_matchday_prompt": False,
        "strategy": "schema",
    }


def test_existing_default_ai_mode_still_uses_the_configured_generator(db, tmp_path) -> None:
    team, game = _graph(db, mode="ai")
    generator = SuccessfulAITextGenerator()

    post = create_post(
        db,
        game,
        team,
        generator,
        Renderer(tmp_path / "generated"),
        logo_snapshot=_logo_snapshot(),
    )

    assert len(generator.calls) == 1
    assert generator.calls[0]["text_prompt"].post_type == "announcement"
    assert post.text == "Von der KI erzeugter Testtext"
    assert post.design_snapshot["mode"]["text"] == "openai"
    assert post.design_snapshot["text_generation"]["strategy"] == "ai"


def test_matchday_schema_is_deterministic_and_does_not_need_a_provider() -> None:
    result = generate_matchday_schema_text(
        [
            {
                "home_team": "SV Beispiel 1",
                "away_team": "FC Muster 1",
                "date": "05.09.2026",
                "time": "15:00",
                "venue": "RP Beispielstadt",
                "score": None,
            },
            {
                "home_team": "SV Beispiel 2",
                "away_team": "FC Muster 2",
                "date": "06.09.2026",
                "time": "11:00",
                "venue": "KR Musterstadt",
                "score": None,
            },
        ],
        "announcement",
    )

    assert result.model == "schema"
    assert result.prompt_version == "standard-schema-matchday-announcement-v1"
    assert "1. SV Beispiel 1 – FC Muster 1" in result.text
    assert "2. SV Beispiel 2 – FC Muster 2" in result.text
