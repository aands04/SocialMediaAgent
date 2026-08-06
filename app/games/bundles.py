"""Resolve explicit and rule-driven same-club matchday generation bundles."""

from __future__ import annotations

from datetime import datetime, time, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logos.service import normalize_club_name
from app.models import Game, Team

BERLIN = ZoneInfo("Europe/Berlin")
GROUPABLE_POST_TYPES = {"announcement", "result"}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _groups_by_rule(team: Team, post_type: str) -> bool:
    mode = str((team.rules or {}).get("club_matchday_feed_mode", "separate"))
    return mode == "announcements_and_results" or (
        mode == "announcements" and post_type == "announcement"
    )


def _candidate_rows(db: Session, game: Game, team: Team) -> tuple[list[Game], dict[str, Team]]:
    local_date = _utc(game.kickoff).astimezone(BERLIN).date()
    start = datetime.combine(local_date, time.min, BERLIN).astimezone(timezone.utc)
    end = datetime.combine(local_date, time.max, BERLIN).astimezone(timezone.utc)
    teams = {
        item.id: item
        for item in db.scalars(
            select(Team).where(
                Team.instagram_page_id == team.instagram_page_id,
                Team.active.is_(True),
                Team.archived_at.is_(None),
            )
        )
        if normalize_club_name(item.club) == normalize_club_name(team.club)
    }
    games = list(
        db.scalars(
            select(Game).where(
                Game.team_id.in_(teams),
                Game.kickoff >= start,
                Game.kickoff <= end,
                Game.status.not_in(["cancelled", "postponed"]),
            )
        )
    )
    games = [
        item
        for item in games
        if not bool((item.overrides or {}).get("dashboard_deleted"))
        and not bool((item.overrides or {}).get("import_suppressed"))
        and not bool((item.overrides or {}).get("automation_blocked"))
    ]
    return games, teams


def _sort_games(games: list[Game], teams: dict[str, Team], reference_team: Team) -> list[Game]:
    preferred = str((reference_team.rules or {}).get("club_matchday_primary_team_id") or "")
    if not preferred:
        for candidate in teams.values():
            preferred = str(
                (candidate.rules or {}).get("club_matchday_primary_team_id") or ""
            )
            if preferred:
                break
    return sorted(
        games,
        key=lambda item: (
            0 if item.team_id == preferred else 1,
            _utc(item.kickoff),
            teams[item.team_id].display_name,
            item.id,
        ),
    )


def generation_bundle_games(
    db: Session,
    game: Game,
    team: Team,
    post_type: str,
) -> tuple[list[Game], dict[str, Team], str | None]:
    """Return the validated games for one shared generation request.

    A manual bundle wins over the team rule.  ``generation_bundle_separated``
    explicitly opts a game out of future automatic grouping until an editor
    connects it again.
    """
    if post_type not in GROUPABLE_POST_TYPES:
        return [game], {team.id: team}, None
    own_overrides = game.overrides or {}
    if own_overrides.get("generation_bundle_separated"):
        return [game], {team.id: team}, None
    rows, teams = _candidate_rows(db, game, team)
    manual_id = str(own_overrides.get("generation_bundle_id") or "")
    if manual_id:
        selected = [
            item
            for item in rows
            if str((item.overrides or {}).get("generation_bundle_id") or "") == manual_id
        ]
        if len({item.team_id for item in selected}) >= 2:
            return _sort_games(selected, teams, team), teams, f"manual:{manual_id}"
        return [game], teams, None
    if not _groups_by_rule(team, post_type):
        return [game], teams, None
    selected = [
        item
        for item in rows
        if not (item.overrides or {}).get("generation_bundle_separated")
        and not (item.overrides or {}).get("generation_bundle_id")
        and _groups_by_rule(teams[item.team_id], post_type)
        and bool(
            (teams[item.team_id].rules or {}).get(
                "result_enabled" if post_type == "result" else "announcement_enabled"
            )
        )
    ]
    if len({item.team_id for item in selected}) < 2:
        return [game], teams, None
    key = f"automatic:{team.instagram_page_id}:{_utc(game.kickoff).astimezone(BERLIN):%Y-%m-%d}"
    return _sort_games(selected, teams, team), teams, key


def connect_games(db: Session, games: list[Game], teams: dict[str, Team]) -> str:
    if len(games) < 2 or len({item.team_id for item in games}) < 2:
        raise ValueError("Bitte Spiele von mindestens zwei Mannschaften auswählen")
    first = games[0]
    first_team = teams[first.team_id]
    first_date = _utc(first.kickoff).astimezone(BERLIN).date()
    for item in games:
        team = teams.get(item.team_id)
        if not team or team.club_id != first.club_id:
            raise ValueError("Alle Spiele müssen zum selben Verein gehören")
        if team.instagram_page_id != first_team.instagram_page_id:
            raise ValueError("Alle Spiele müssen dieselbe Instagram-Seite verwenden")
        if _utc(item.kickoff).astimezone(BERLIN).date() != first_date:
            raise ValueError("Es können nur Spiele desselben Spieltags verbunden werden")
        if item.status in {"cancelled", "postponed"}:
            raise ValueError("Abgesagte oder verschobene Spiele können nicht verbunden werden")
    bundle_id = str(uuid4())
    for item in games:
        overrides = dict(item.overrides or {})
        overrides["generation_bundle_id"] = bundle_id
        overrides["generation_bundle_source"] = "manual"
        overrides.pop("generation_bundle_separated", None)
        item.overrides = overrides
        item.version += 1
    db.flush()
    return bundle_id


def separate_games(games: list[Game]) -> None:
    for item in games:
        overrides = dict(item.overrides or {})
        overrides.pop("generation_bundle_id", None)
        overrides.pop("generation_bundle_source", None)
        overrides["generation_bundle_separated"] = True
        item.overrides = overrides
        item.version += 1


def dashboard_game_groups(
    db: Session, games: list[Game], teams: dict[str, Team]
) -> list[dict]:
    remaining = {item.id: item for item in games}
    groups: list[dict] = []
    for game in games:
        if game.id not in remaining:
            continue
        team = teams.get(game.team_id)
        if not team:
            continue
        bundled, bundle_teams, key = generation_bundle_games(db, game, team, "announcement")
        bundled = [item for item in bundled if item.id in remaining]
        if key and len(bundled) >= 2:
            for item in bundled:
                remaining.pop(item.id, None)
            result_grouped = len(generation_bundle_games(db, game, team, "result")[0]) >= 2
            groups.append(
                {
                    "key": key,
                    "games": bundled,
                    "teams": bundle_teams,
                    "grouped": True,
                    "manual": key.startswith("manual:"),
                    "result_grouped": result_grouped,
                }
            )
        else:
            remaining.pop(game.id, None)
            groups.append(
                {
                    "key": None,
                    "games": [game],
                    "teams": teams,
                    "grouped": False,
                    "manual": False,
                    "result_grouped": False,
                }
            )
    return groups
