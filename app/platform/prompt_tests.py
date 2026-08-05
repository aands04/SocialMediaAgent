from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.branding.service import branding_snapshot, prompt_data_block
from app.models import (
    Club,
    Game,
    PromptTemplate,
    PromptTestRun,
    Team,
    User,
    uid,
)
from app.platform.service import platform_audit
from app.prompts.service import (
    TEXT_SAFETY_PREFIX,
    image_safety_prefix,
    prompt_context,
    render_body,
    sample_facts,
)
from app.usage.service import complete_usage, reserve_usage


class PromptTestError(ValueError):
    pass


def _facts(club: Club, team: Team | None, game: Game | None) -> dict:
    facts = sample_facts()
    facts["club_id"] = club.id
    facts["own_team"] = team.display_name if team else club.short_name
    facts["own_team_aliases"] = [
        value
        for value in (
            team.internal_name if team else None,
            team.short_name if team else None,
            club.name,
            club.short_name,
        )
        if value
    ]
    if team:
        facts["primary_color"] = (team.colors or {}).get("primary") or "#172554"
        facts["secondary_color"] = (team.colors or {}).get("secondary") or "#ffffff"
        facts["hashtags"] = team.hashtags or []
        facts["style_direction"] = (team.rules or {}).get("style_direction")
    if game:
        facts.update(
            {
                "home_team": game.home_team,
                "away_team": game.away_team,
                "kickoff": game.kickoff.isoformat(),
                "competition": game.competition or "Testwettbewerb",
                "venue": game.venue or "RP Testort",
                "pitch": game.pitch or "Rasenplatz",
                "score": (
                    f"{game.home_score}:{game.away_score}"
                    if game.result_confirmed
                    and game.home_score is not None
                    and game.away_score is not None
                    else ""
                ),
            }
        )
    else:
        facts["home_team"] = facts["own_team"]
    return facts


def _render(db: Session, template: PromptTemplate, facts: dict) -> str:
    rendered = render_body(
        template.prompt_body,
        prompt_context(facts, template.media_kind, template.style_direction),
    )
    rendered += "\n\n" + prompt_data_block(
        branding_snapshot(db, facts["club_id"]), template.prompt_kind
    )
    if template.prompt_kind == "image":
        return image_safety_prefix(facts) + "\n" + rendered
    return TEXT_SAFETY_PREFIX + "\n" + rendered


def run_fixture_prompt_test(
    db: Session,
    actor: User,
    *,
    club: Club,
    candidate: PromptTemplate,
    comparison: PromptTemplate | None = None,
    team: Team | None = None,
    game: Game | None = None,
) -> PromptTestRun:
    if game is not None and team is None:
        team = db.get(Team, game.team_id)
    if team and team.club_id != club.id:
        raise PromptTestError("Die Testmannschaft gehört nicht zum ausgewählten Verein")
    if game and (game.club_id != club.id or (team and game.team_id != team.id)):
        raise PromptTestError("Das Testspiel gehört nicht zum ausgewählten Verein")
    if comparison and (
        comparison.prompt_kind != candidate.prompt_kind
        or comparison.post_type != candidate.post_type
        or comparison.media_kind != candidate.media_kind
    ):
        raise PromptTestError("Vergleichsvorlagen müssen Art, Beitragstyp und Format teilen")

    facts = _facts(club, team, game)
    candidate_result = _render(db, candidate, facts)
    comparison_result = _render(db, comparison, facts) if comparison else None
    test_id = uid()
    usage = reserve_usage(
        db,
        club_id=club.id,
        generation_type=candidate.prompt_kind,
        quantity=1,
        idempotency_key=f"platform-prompt-test:{test_id}",
        provider="fixture",
        model=candidate.model,
        user_id=actor.id,
        platform_test=True,
        prompt_template_id=candidate.id,
        prompt_version=candidate.version,
    )
    complete_usage(db, usage, actual_quantity=1, provider_cost=0)
    result = PromptTestRun(
        id=test_id,
        club_id=club.id,
        team_id=team.id if team else None,
        game_id=game.id if game else None,
        old_prompt_template_id=comparison.id if comparison else None,
        new_prompt_template_id=candidate.id,
        fixture_snapshot={
            "club_id": club.id,
            "team_id": team.id if team else None,
            "game_id": game.id if game else None,
            "mode": "fixture",
        },
        result_snapshot={
            "candidate": candidate_result,
            "candidate_checksum": hashlib.sha256(candidate_result.encode()).hexdigest(),
            "comparison": comparison_result,
            "comparison_checksum": (
                hashlib.sha256(comparison_result.encode()).hexdigest()
                if comparison_result
                else None
            ),
            "tested_at": datetime.now(timezone.utc).isoformat(),
        },
        provider_cost=0,
        status="completed",
        created_by=actor.id,
    )
    db.add(result)
    platform_audit(
        db,
        actor,
        "prompt.fixture_tested",
        "prompt_test_run",
        result.id,
        {
            "club_id": club.id,
            "candidate_prompt_id": candidate.id,
            "candidate_version": candidate.version,
            "comparison_prompt_id": comparison.id if comparison else None,
            "candidate_checksum": result.result_snapshot["candidate_checksum"],
        },
    )
    db.flush()
    return result
