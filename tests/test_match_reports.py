from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.match_reports import service as match_report_service
from app.match_reports.context import build_match_content_context
from app.match_reports.fupa import (
    FupaReader,
    parse_fupa_html,
    parse_fupa_stream,
    validate_fupa_url,
)
from app.match_reports.generator import (
    FixtureMatchReportGenerator,
    MatchReportGenerationError,
    render_match_report_prompt,
)
from app.match_reports.routes import templates as match_report_templates
from app.match_reports.scheduler import _final_result_available
from app.match_reports.service import (
    approve_report,
    create_edited_version,
    current_version,
    delete_unpublished_report,
    generate_report_version,
    get_or_create_report,
    prepare_fupa_publication,
)
from app.match_reports.types import GeneratedMatchReport
from app.models import (
    AiPromptDispatch,
    AuditLog,
    FupaMatchSnapshot,
    Game,
    MatchEvent,
    MatchReportPublication,
    MatchReportVersion,
    Role,
    Team,
    User,
)
from app.web import berlin_datetime


def test_match_report_template_registers_berlin_datetime_filter():
    assert match_report_templates.env.filters["berlin"] is berlin_datetime
    assert match_report_templates.env.get_template("match_reports/detail.html") is not None


def _team_and_game(
    db,
    *,
    confirmed: bool = True,
    home_score: int | None = 2,
    away_score: int | None = 1,
) -> tuple[Team, Game]:
    suffix = uuid4().hex[:8]
    team = Team(
        internal_name=f"Erste-{suffix}",
        display_name=f"Testverein {suffix}",
        short_name=f"TV-{suffix[:4]}",
        slug=f"team-{suffix}",
        club="Testverein",
        active=True,
        fussball_url=f"https://www.fussball.de/team/{suffix}",
        fupa_url=f"https://www.fupa.net/match/{suffix}",
        media_subdir=f"team-{suffix}",
    )
    db.add(team)
    db.flush()
    game = Game(
        team_id=team.id,
        provider="fupa",
        external_id=f"match-{suffix}",
        home_team=team.display_name,
        away_team="Gastverein",
        kickoff=datetime.now(timezone.utc) - timedelta(hours=3),
        competition="Kreisliga",
        venue="Teststadion",
        status="finished" if confirmed else "scheduled",
        home_score=home_score,
        away_score=away_score,
        result_confirmed=confirmed,
        source_url=f"https://www.fussball.de/spiel/{suffix}",
        fupa_url=f"https://www.fupa.net/match/{suffix}",
    )
    db.add(game)
    db.flush()
    return team, game


def _snapshot(
    db,
    game: Game,
    *,
    home_score: int | None = 2,
    away_score: int | None = 1,
    status: str | None = "EventCompleted",
    ticker: list[dict] | None = None,
) -> FupaMatchSnapshot:
    snapshot = FupaMatchSnapshot(
        club_id=game.club_id,
        game_id=game.id,
        source_url=game.fupa_url,
        fetch_status="success",
        structured_data={
            "home_team": game.home_team,
            "away_team": game.away_team,
            "home_score": home_score,
            "away_score": away_score,
            "status": status,
        },
        ticker_data=ticker or [],
        source_metadata={"parser": "test"},
        content_digest=uuid4().hex + uuid4().hex,
    )
    db.add(snapshot)
    db.flush()
    return snapshot


def _admin(db) -> User:
    user = User(
        email=f"admin-{uuid4().hex[:8]}@example.test",
        password_hash="test",
        role=Role.ADMIN,
        all_teams=True,
    )
    db.add(user)
    db.flush()
    return user


def test_fupa_url_validation_rejects_non_official_and_unsafe_urls():
    valid = "https://www.fupa.net/match/test"
    assert validate_fupa_url(valid) == valid
    for value in (
        "http://www.fupa.net/match/test",
        "https://user:secret@www.fupa.net/match/test",
        "https://www.fupa.net.evil.example/match/test",
        "https://www.fupa.net:444/match/test",
    ):
        with pytest.raises(ValueError):
            validate_fupa_url(value)


def test_fupa_parser_reads_structured_match_and_fulltime_ticker():
    html = """
    <html><head><title>FuPa Spiel</title>
      <script type="application/ld+json">
      {"@type":"SportsEvent","homeTeam":{"name":"Heim"},
       "awayTeam":{"name":"Gast"},"startDate":"2026-08-19T18:00:00+02:00",
       "homeScore":3,"awayScore":1,"eventStatus":"Beendet"}
      </script>
      <script id="__NEXT_DATA__" type="application/json">
      {"props":{"events":[{"id":"end","minute":90,"text":"Abpfiff 3:1"}]}}
      </script>
    </head></html>
    """
    result = parse_fupa_html("https://www.fupa.net/match/test", html)
    assert result.fetch_status == "success"
    assert result.structured_data["home_team"] == "Heim"
    assert result.structured_data["away_team"] == "Gast"
    assert result.structured_data["home_score"] == 3
    assert result.structured_data["away_score"] == 1
    assert result.ticker[-1].event_type == "fulltime"


def test_fupa_parser_reads_current_redux_bootstrap_and_ticker():
    html = """
    <html><head><title>FuPa Spielbericht</title></head><body>
      <script>
      window.REDUX_DATA = {"dataHistory":[{"MatchPage":{
        "matchInfo":{
          "homeTeamName":"TSV Carlsdorf",
          "awayTeamName":"SV Ehlen",
          "homeGoal":1,
          "awayGoal":2,
          "kickoff":"2026-08-16T15:00:00+02:00",
          "competitionName":"Kreisliga A",
          "venueName":"RP Hofgeismar-Carlsdorf",
          "section":"POST"
        },
        "ticker":{"events":[
          {"id":"goal-1","type":"goal","minute":18,
           "homeGoal":0,"awayGoal":1,
           "primaryRole":{"firstName":"Max","lastName":"Muster"}},
          {"id":"end","type":"fulltime","minute":90,
           "homeGoal":1,"awayGoal":2}
        ]}
      }}]};
      </script>
    </body></html>
    """

    result = parse_fupa_html("https://www.fupa.net/match/test", html)

    assert result.fetch_status == "success"
    assert result.metadata["parser"] == "jsonld-nextdata-redux-v3"
    assert result.structured_data == {
        "home_team": "TSV Carlsdorf",
        "away_team": "SV Ehlen",
        "home_score": 1,
        "away_score": 2,
        "kickoff": "2026-08-16T15:00:00+02:00",
        "competition": "Kreisliga A",
        "venue": "RP Hofgeismar-Carlsdorf",
        "status": "finished",
    }
    assert [item.event_type for item in result.ticker] == ["goal", "fulltime"]
    assert result.ticker[0].player == "Max Muster"
    assert (result.ticker[-1].home_score, result.ticker[-1].away_score) == (1, 2)


def test_fupa_stream_reads_scorers_descriptions_scores_and_whistles():
    result = parse_fupa_stream(
        [
            {
                "type": "matchevent",
                "entity": {
                    "id": 3,
                    "type": "goal",
                    "subtype": "goal_shoot",
                    "minute": 17,
                    "text": "Nach einer Drehung schiebt er links unten ein.",
                    "homeGoal": 3,
                    "awayGoal": 0,
                    "team": {"name": {"full": "TSV Carlsdorf"}},
                    "primaryRole": {"player": {"firstName": "Owen Louis", "lastName": "Wenzel"}},
                },
            },
            {
                "type": "matchevent",
                "entity": {
                    "id": 1,
                    "type": "goal",
                    "subtype": "goal_shoot",
                    "minute": 3,
                    "text": "",
                    "homeGoal": 1,
                    "awayGoal": 0,
                    "primaryRole": {"player": {"firstName": "Gian-Luca", "lastName": "Masannek"}},
                },
            },
            {
                "type": "matchevent",
                "entity": {
                    "id": 4,
                    "type": "whistle",
                    "subtype": "whistle_regular_stop_second_halftime",
                    "minute": 90,
                    "text": "Abpfiff",
                    "homeGoal": 6,
                    "awayGoal": 0,
                },
            },
            {"type": "liveticker-eingetragen", "entity": {"id": "ignored"}},
        ]
    )

    assert [item.minute for item in result] == [3, 17, 90]
    assert result[0].player == "Gian-Luca Masannek"
    assert result[0].text == "Tor – Gian-Luca Masannek – 1:0"
    assert result[1].player == "Owen Louis Wenzel"
    assert result[1].team == "TSV Carlsdorf"
    assert result[1].text == "Nach einer Drehung schiebt er links unten ein."
    assert (result[1].home_score, result[1].away_score) == (3, 0)
    assert result[2].event_type == "fulltime"


def test_fupa_reader_enriches_highlights_from_public_stream(monkeypatch):
    html = """
    <html><head><title>FuPa Spielbericht</title></head><body><script>
    window.REDUX_DATA = {"dataHistory":[{"MatchPage":{"matchInfo":{
      "id":14916867,"homeTeamName":"TSV Carlsdorf","awayTeamName":"SV Ehlen",
      "homeGoal":6,"awayGoal":0,"section":"POST","flags":["ticker"],
      "highlights":[
        {"id":84763897,"minute":3,"homeGoal":1,"awayGoal":0,"type":"goal",
         "primaryRole":{"firstName":"Gian-Luca","lastName":"Masannek"}}
      ]
    }}}]};
    </script></body></html>
    """
    stream = [
        {
            "type": "matchevent",
            "entity": {
                "id": 84763897,
                "minute": 3,
                "type": "goal",
                "homeGoal": 1,
                "awayGoal": 0,
                "text": "Kurze Torbeschreibung",
                "primaryRole": {"player": {"firstName": "Gian-Luca", "lastName": "Masannek"}},
            },
        },
        {
            "type": "matchevent",
            "entity": {
                "id": 84788650,
                "minute": 90,
                "type": "whistle",
                "subtype": "whistle_regular_stop_second_halftime",
                "text": "Abpfiff",
                "homeGoal": 6,
                "awayGoal": 0,
            },
        },
    ]
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.host == "api.fupa.net":
            assert request.headers["origin"] == "https://www.fupa.net"
            return httpx.Response(200, json=stream)
        return httpx.Response(200, text=html)

    monkeypatch.setattr("app.match_reports.fupa._reject_private_resolution", lambda _host: None)
    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        result = FupaReader(client=client).fetch(
            "https://www.fupa.net/match/tsv-carlsdorf-m1-sv-ehlen-m1-260816"
        )

    assert requested == [
        "https://www.fupa.net/match/tsv-carlsdorf-m1-sv-ehlen-m1-260816",
        "https://api.fupa.net/v2/matches/14916867/stream",
    ]
    assert result.fetch_status == "success"
    assert result.metadata["stream_status"] == "success"
    assert result.metadata["stream_event_count"] == 2
    assert len(result.ticker) == 2
    assert result.ticker[0].player == "Gian-Luca Masannek"
    assert result.ticker[0].text == "Kurze Torbeschreibung"
    assert result.ticker[1].event_type == "fulltime"


def test_fupa_reader_uses_structured_highlights_when_stream_is_unavailable(monkeypatch):
    html = """
    <html><body><script>
    window.REDUX_DATA = {"dataHistory":[{"MatchPage":{"matchInfo":{
      "id":14916867,"homeTeamName":"Heim","awayTeamName":"Gast","flags":["ticker"],
      "highlights":[
        {"id":11,"minute":15,"homeGoal":1,"awayGoal":0,"type":"goal",
         "primaryRole":{"firstName":"Heinrich","lastName":"Deichmann"}}
      ]
    }}}]};
    </script></body></html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.fupa.net":
            return httpx.Response(503, text="temporarily unavailable")
        return httpx.Response(200, text=html)

    monkeypatch.setattr("app.match_reports.fupa._reject_private_resolution", lambda _host: None)
    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        result = FupaReader(client=client).fetch("https://www.fupa.net/match/test")

    assert result.fetch_status == "success"
    assert result.metadata["stream_status"] == "unavailable"
    assert result.metadata["ticker_fallback_count"] == 1
    assert result.ticker[0].player == "Heinrich Deichmann"
    assert result.error is None


def test_fupa_reader_marks_expected_but_missing_ticker_incomplete(monkeypatch):
    html = """
    <html><body><script>
    window.REDUX_DATA = {"dataHistory":[{"MatchPage":{"matchInfo":{
      "id":14916867,"homeTeamName":"Heim","awayTeamName":"Gast","flags":["ticker"]
    }}}]};
    </script></body></html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.fupa.net":
            return httpx.Response(503)
        return httpx.Response(200, text=html)

    monkeypatch.setattr("app.match_reports.fupa._reject_private_resolution", lambda _host: None)
    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        result = FupaReader(client=client).fetch("https://www.fupa.net/match/test")

    assert result.fetch_status == "incomplete"
    assert result.error_category == "ticker_unavailable"
    assert "Liveticker" in (result.error or "")


def test_fupa_parser_reads_current_nested_team_names():
    html = """
    <html><head><title>FuPa Spielbericht</title></head><body><script>
    window.REDUX_DATA = {"dataHistory":[{"key":"undefined","MatchPage":{
      "matchInfo":{
        "homeTeam":{"name":{"full":"TSV Carlsdorf","middle":"Carlsdorf","short":"Carls"}},
        "awayTeam":{"name":{"full":"SV Ehlen","middle":"Ehlen","short":"SVE"}},
        "homeTeamName":"TSV Carlsdorf","awayTeamName":"SV Ehlen",
        "kickoff":"2026-08-16T15:00:00+02:00","section":"POST"
      }
    }}]};
    </script></body></html>
    """

    result = parse_fupa_html("https://www.fupa.net/match/test", html)

    assert result.fetch_status == "success"
    assert result.structured_data["home_team"] == "TSV Carlsdorf"
    assert result.structured_data["away_team"] == "SV Ehlen"


def test_fupa_parser_does_not_execute_invalid_redux_javascript():
    html = """
    <html><head><title>Anstoß 14:30 Uhr</title></head><body>
      <script>window.REDUX_DATA = alert('darf nicht ausgeführt werden');</script>
    </body></html>
    """

    result = parse_fupa_html("https://www.fupa.net/match/no-data", html)

    assert result.fetch_status == "incomplete"
    assert result.structured_data["home_score"] is None
    assert result.structured_data["away_score"] is None


def test_fupa_parser_never_treats_page_wide_time_as_a_score():
    html = "<html><head><title>Anstoß 14:30 Uhr</title></head><body>Beginn 14:30 Uhr</body></html>"
    result = parse_fupa_html("https://www.fupa.net/match/no-data", html)
    assert result.fetch_status == "incomplete"
    assert result.structured_data["home_score"] is None
    assert result.structured_data["away_score"] is None


def test_final_result_requires_confirmation_or_explicit_finished_marker(db):
    _, game = _team_and_game(db, confirmed=False, home_score=2, away_score=1)
    snapshot = _snapshot(db, game, status="Live", ticker=[])
    assert _final_result_available(snapshot, game) is False

    snapshot.ticker_data = [
        {
            "event_type": "fulltime",
            "home_score": 2,
            "away_score": 1,
            "text": "Abpfiff 2:1",
        }
    ]
    assert _final_result_available(snapshot, game) is True

    snapshot.ticker_data = []
    game.result_confirmed = True
    assert _final_result_available(snapshot, game) is True


def test_context_prefers_fupa_and_blocks_conflicting_confirmed_score(db):
    _, game = _team_and_game(db, confirmed=True, home_score=1, away_score=0)
    _snapshot(db, game, home_score=2, away_score=1)

    context = build_match_content_context(db, game.id)

    assert context.facts["home_score"] == 2
    assert context.facts["away_score"] == 1
    assert context.has_blocking_conflicts is True
    assert context.provenance["score_candidates"] == {
        "fupa_strukturiert": [2, 1],
        "spielstamm": [1, 0],
    }


def test_context_uses_confirmed_live_event_when_fupa_has_no_score(db):
    team, game = _team_and_game(
        db,
        confirmed=False,
        home_score=None,
        away_score=None,
    )
    _snapshot(db, game, home_score=None, away_score=None, status="Live")
    db.add(
        MatchEvent(
            game_id=game.id,
            team_id=team.id,
            provider="dashboard",
            idempotency_key=f"manual-fulltime:{game.id}",
            event_sequence=1,
            event_type="fulltime",
            status="confirmed",
            home_score_after=3,
            away_score_after=2,
        )
    )
    db.flush()

    context = build_match_content_context(db, game.id)

    assert context.facts["home_score"] == 3
    assert context.facts["away_score"] == 2
    assert context.has_blocking_conflicts is False
    assert context.events[0]["source_id"].startswith("live:")


def test_context_includes_fupa_ticker_events_in_summary_and_ai_prompt(db):
    _, game = _team_and_game(db)
    _snapshot(
        db,
        game,
        ticker=[
            {
                "source_id": "goal-17",
                "event_type": "goal",
                "minute": 17,
                "text": "Nach einer Drehung schiebt er links unten ein.",
                "team": game.home_team,
                "player": "Owen Louis Wenzel",
                "home_score": 3,
                "away_score": 0,
            },
            {
                "source_id": "end-90",
                "event_type": "fulltime",
                "minute": 90,
                "text": "Abpfiff",
                "home_score": 6,
                "away_score": 0,
            },
        ],
    )

    context = build_match_content_context(db, game.id)
    prompt = render_match_report_prompt(context, desired_length="medium")

    assert len(context.events) == 2
    assert context.events[0]["source_id"] == "fupa-ticker:goal-17"
    assert context.events[0]["player"] == "Owen Louis Wenzel"
    assert context.events[0]["comment"] == "Nach einer Drehung schiebt er links unten ein."
    assert "Owen Louis Wenzel" in prompt
    assert "Nach einer Drehung schiebt er links unten ein." in prompt


def test_generated_match_report_records_exact_prompt_for_platform_admin(db, monkeypatch):
    _, game = _team_and_game(db)
    _snapshot(db, game)
    user = _admin(db)
    report = get_or_create_report(db, game)
    exact_prompt = "GESCHÜTZTER EXAKTER SPIELBERICHT-PROMPT"

    class FakeGenerator:
        def generate(self, context, *, desired_length):
            return GeneratedMatchReport(
                headline="Testbericht",
                teaser="Test",
                body="Ein technisch gültiger Testbericht.",
                used_sources=(),
                model="test-model",
                rendered_prompt=exact_prompt,
            )

    monkeypatch.setattr(
        match_report_service,
        "build_match_report_generator",
        lambda _settings: FakeGenerator(),
    )
    settings = SimpleNamespace(text_generator_mode="openai", openai_model="test-model")

    version = generate_report_version(db, report, settings, user_id=user.id)
    dispatch = db.scalar(
        select(AiPromptDispatch).where(
            AiPromptDispatch.club_id == game.club_id,
            AiPromptDispatch.post_type == "match_report",
        )
    )

    assert version.version_number == 1
    assert dispatch is not None
    assert dispatch.generation_job_id is None
    assert dispatch.game_id == game.id
    assert dispatch.prompt_name == "Spielbericht"
    assert dispatch.rendered_prompt == exact_prompt
    assert dispatch.status == "completed"


def test_generator_refuses_incomplete_context(db):
    _, game = _team_and_game(
        db,
        confirmed=False,
        home_score=None,
        away_score=None,
    )
    context = build_match_content_context(db, game.id)
    assert context.has_blocking_conflicts
    with pytest.raises(MatchReportGenerationError, match="Quellenkonflikte"):
        FixtureMatchReportGenerator().generate(context, desired_length="medium")


def test_report_versions_are_immutable_and_manual_transfer_is_idempotent(db):
    _, game = _team_and_game(db)
    _snapshot(db, game)
    user = _admin(db)
    report = get_or_create_report(db, game)
    settings = SimpleNamespace(text_generator_mode="fixture")

    first = generate_report_version(db, report, settings, user_id=user.id)
    original_body = first.body
    second = create_edited_version(
        db,
        report,
        headline="Geprüfter Spielbericht",
        teaser="Bestätigter Endstand",
        body="Der bestätigte Bericht wurde redaktionell ergänzt.",
        user_id=user.id,
        change_reason="Redaktionelle Präzisierung",
    )
    db.flush()

    assert first.version_number == 1
    assert first.body == original_body
    assert second.version_number == 2
    assert current_version(db, report).id == second.id

    approve_report(db, report, user_id=user.id)
    first_publication = prepare_fupa_publication(db, report, user_id=user.id)
    second_publication = prepare_fupa_publication(db, report, user_id=user.id)
    db.flush()

    assert first_publication.id == second_publication.id
    assert first_publication.status == "manual_required"
    count = (
        db.query(MatchReportPublication)
        .filter(MatchReportPublication.report_id == report.id)
        .count()
    )
    assert count == 1


def test_generated_version_keeps_source_snapshot_for_audit(db):
    _, game = _team_and_game(db)
    snapshot = _snapshot(db, game)
    report = get_or_create_report(db, game)

    version = generate_report_version(
        db,
        report,
        SimpleNamespace(text_generator_mode="fixture"),
        user_id=None,
    )
    db.flush()

    persisted = db.get(MatchReportVersion, version.id)
    assert persisted.source_snapshot["provenance"]["snapshot_id"] == snapshot.id
    assert persisted.used_sources == [f"fupa:{snapshot.id}"]


def test_delete_unpublished_report_removes_versions_but_keeps_sources_and_audit(db):
    _, game = _team_and_game(db)
    snapshot = _snapshot(db, game)
    user = _admin(db)
    report = get_or_create_report(db, game)
    version = generate_report_version(
        db,
        report,
        SimpleNamespace(text_generator_mode="fixture"),
        user_id=user.id,
    )
    approve_report(db, report, user_id=user.id)
    publication = prepare_fupa_publication(db, report, user_id=user.id)
    db.flush()

    delete_unpublished_report(db, report, user_id=user.id)
    db.flush()

    assert db.get(type(report), report.id) is None
    assert db.get(MatchReportVersion, version.id) is None
    assert db.get(MatchReportPublication, publication.id) is None
    assert db.get(FupaMatchSnapshot, snapshot.id) is not None
    audit = db.scalar(
        select(AuditLog).where(
            AuditLog.action == "match_report.deleted",
            AuditLog.entity_id == report.id,
        )
    )
    assert audit is not None
    assert audit.details["sources_retained"] is True
    assert audit.details["deleted_versions"] == 1
    assert audit.details["deleted_publication_preparations"] == 1


def test_delete_published_report_is_blocked(db):
    _, game = _team_and_game(db)
    _snapshot(db, game)
    user = _admin(db)
    report = get_or_create_report(db, game)
    generate_report_version(
        db,
        report,
        SimpleNamespace(text_generator_mode="fixture"),
        user_id=user.id,
    )
    report.status = "published"
    report.published_at = datetime.now(timezone.utc)
    db.flush()

    with pytest.raises(RuntimeError, match="kann nicht gelöscht werden"):
        delete_unpublished_report(db, report, user_id=user.id)
