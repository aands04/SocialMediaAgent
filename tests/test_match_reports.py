from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.match_reports import service as match_report_service
from app.match_reports.context import build_match_content_context
from app.match_reports.fupa import (
    FupaReader,
    parse_fupa_html,
    parse_fupa_stream,
    validate_fupa_url,
)
from app.match_reports.fupa_browser import (
    BrowserFupaPublisher,
    FupaBrowserPublishError,
    _admin_match_report_url,
    _context_match_id,
    _is_exact_match_destination,
    _match_association_confirmed,
    _normalize_editor_content,
    _url_match_id,
)
from app.match_reports.fupa_session import (
    FupaSessionError,
    decrypt_fupa_browser_session,
    revoke_fupa_browser_session,
    sanitize_storage_state,
    save_fupa_browser_session,
)
from app.match_reports.generator import (
    MATCH_REPORT_PROMPT_VERSION,
    FixtureMatchReportGenerator,
    MatchReportGenerationError,
    render_match_report_prompt,
)
from app.match_reports.publisher import FupaPublishResult
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
    FupaBrowserSession,
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


def test_fupa_browser_rejects_unassociated_club_news_editor():
    assert not _match_association_confirmed(
        source_url="https://www.fupa.net/match/tsv-carlsdorf-m1-sv-ehlen-m1-260816",
        editor_url="https://www.fupa.net/vereinsverwaltung/vereinsnachricht/neu",
        visible_text="Vereinsverwaltung Neuer Bericht Schlagzeile Haupttext",
        scoped_values=(),
        home_team="TSV Carlsdorf",
        away_team="SV Ehlen",
    )


def test_fupa_browser_accepts_editor_with_both_match_teams():
    assert _match_association_confirmed(
        source_url="https://www.fupa.net/match/tsv-carlsdorf-m1-sv-ehlen-m1-260816",
        editor_url="https://www.fupa.net/vereinsverwaltung/spielbericht/neu",
        visible_text="Spielbericht für TSV Carlsdorf gegen SV Ehlen",
        scoped_values=(),
        home_team="TSV Carlsdorf",
        away_team="SV Ehlen",
    )


def test_fupa_browser_accepts_match_key_in_scoped_editor_value():
    assert _match_association_confirmed(
        source_url="https://www.fupa.net/match/tsv-carlsdorf-m1-sv-ehlen-m1-260816",
        editor_url="https://www.fupa.net/vereinsverwaltung/spielbericht/neu",
        visible_text="Spielbericht bearbeiten",
        scoped_values=("tsv-carlsdorf-m1-sv-ehlen-m1-260816",),
        home_team="TSV Carlsdorf",
        away_team="SV Ehlen",
    )


def test_fupa_browser_accepts_exact_numeric_admin_match_id():
    assert _match_association_confirmed(
        source_url="https://www.fupa.net/match/tsv-carlsdorf-m2-sv-ehlen-m2-260816",
        editor_url=(
            "https://admin.fupa.net/fupa/admin/index.php?page=news_edit2&aktion=edit"
            "&news_id=3208426&kategorie=58&match_id=14917720"
        ),
        visible_text="Spielbericht für TSV Carlsdorf II gegen SV Ehlen II",
        scoped_values=(),
        home_team="TSV Carlsdorf II",
        away_team="SV Ehlen II",
        expected_match_id=14917720,
    )


def test_fupa_browser_rejects_wrong_numeric_admin_match_id():
    assert not _match_association_confirmed(
        source_url="https://www.fupa.net/match/tsv-carlsdorf-m2-sv-ehlen-m2-260816",
        editor_url=(
            "https://admin.fupa.net/fupa/admin/index.php?page=news_edit2&match_id=14917721"
        ),
        # Even matching team names must never compensate for a mismatched
        # numeric FuPa game association.
        visible_text="Spielbericht für TSV Carlsdorf II gegen SV Ehlen II",
        scoped_values=(),
        home_team="TSV Carlsdorf II",
        away_team="SV Ehlen II",
        expected_match_id=14917720,
    )


def test_fupa_admin_match_report_url_and_parser_are_exact():
    url = _admin_match_report_url(14917720)

    assert url == "https://admin.fupa.net/fupa/admin/spielbericht.php?spiel=14917720"
    assert _url_match_id(url) == 14917720
    assert _url_match_id("https://admin.fupa.net/fupa/admin/index.php?spiel=invalid") is None


def test_fupa_browser_recognizes_direct_exact_editor_navigation():
    exact_editor = (
        "https://admin.fupa.net/fupa/admin/index.php?page=news_edit2&aktion=edit"
        "&news_id=3208495&kategorie=58&match_id=14916867"
    )

    assert _is_exact_match_destination(
        exact_editor,
        match_id=14916867,
        path_markers=("news_edit2",),
    )
    assert not _is_exact_match_destination(
        exact_editor,
        match_id=14916868,
        path_markers=("news_edit2",),
    )
    assert not _is_exact_match_destination(
        "https://admin.fupa.net/fupa/admin/index.php?page=news_edit2",
        match_id=14916867,
        path_markers=("news_edit2",),
    )


class _FakeLink:
    def __init__(self, href: str, *, visible: bool = True):
        self.href = href
        self.visible = visible

    def get_attribute(self, name: str):
        return self.href if name == "href" else None

    def is_visible(self) -> bool:
        return self.visible


class _FakeLinks:
    def __init__(self, links: tuple[_FakeLink, ...]):
        self.links = links

    def count(self) -> int:
        return len(self.links)

    def nth(self, index: int) -> _FakeLink:
        return self.links[index]


class _FakePage:
    def __init__(self, links: tuple[_FakeLink, ...]):
        self.links = links

    def locator(self, selector: str) -> _FakeLinks:
        assert selector == "a[href]"
        return _FakeLinks(self.links)


def test_fupa_browser_follows_only_exact_match_link_after_redirect():
    wrong = _FakeLink("spielbericht.php?spiel=14917719")
    exact = _FakeLink("spielbericht.php?spiel=14917720")
    page = _FakePage((wrong, exact))

    selected = BrowserFupaPublisher._exact_match_link(
        page,
        match_id=14917720,
        path_markers=("spielbericht.php",),
    )

    assert selected is exact


def test_fupa_browser_does_not_treat_hidden_exact_link_as_visible():
    exact = _FakeLink(
        "index.php?page=news_edit2&aktion=edit&match_id=14917720",
        visible=False,
    )
    page = _FakePage((exact,))

    assert (
        BrowserFupaPublisher._exact_match_link(
            page,
            match_id=14917720,
            path_markers=("news_edit2",),
        )
        is None
    )
    assert (
        BrowserFupaPublisher._exact_match_link(
            page,
            match_id=14917720,
            path_markers=("news_edit2",),
            visible_only=False,
        )
        is exact
    )


def test_fupa_context_match_id_reads_provenance_only():
    context = SimpleNamespace(
        provenance={"fupa_match_id": "14917720"},
        facts={"fupa_match_id": "99999999"},
    )

    assert _context_match_id(context) == 14917720


def test_fupa_editor_content_comparison_ignores_layout_whitespace_only():
    expected = "Erste Zeile\n\nZweite Zeile mit Umlaut: Ehlen."
    actual = "  Erste Zeile  \n  Zweite Zeile mit Umlaut: Ehlen.  "

    assert _normalize_editor_content(actual) == _normalize_editor_content(expected)


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
    match_id: int | None = 14917720,
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
        source_metadata={"parser": "test", "match_id": match_id},
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


def _fupa_browser_settings(**overrides):
    values = {
        "fupa_browser_publish_enabled": True,
        "fupa_browser_session_max_bytes": 524_288,
        "fupa_browser_timeout_seconds": 30.0,
        "fupa_browser_headless": True,
        "meta_token_encryption_key": Fernet.generate_key().decode("ascii"),
        "meta_token_key_version": "v1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _fupa_storage_state(cookie_value: str = "fupa-session-secret") -> str:
    return (
        '{"cookies":['
        '{"name":"session","value":"' + cookie_value + '","domain":".fupa.net","path":"/"},'
        '{"name":"foreign","value":"discard-me","domain":"accounts.google.com","path":"/"}'
        '],"origins":['
        '{"origin":"https://www.fupa.net","localStorage":[]},'
        '{"origin":"https://accounts.google.com","localStorage":[]}'
        "]}"
    )


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
    assert MATCH_REPORT_PROMPT_VERSION == 2
    assert "veröffentlichungsfertigen deutschen Spielbericht" in prompt
    assert "ausschließlich aus zusammenhängendem Fließtext" in prompt
    assert "Verwende keine Aufzählungen" in prompt
    assert "jede Tickermeldung einzeln abzuschreiben" in prompt
    assert "Ergebnisse anderer Mannschaften" in prompt
    assert "nur zulässig" in prompt
    assert "wenn diese Information" in prompt
    assert "Quellenhinweise" in prompt
    assert "im sichtbaren Artikel niemals" in prompt


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


def test_fupa_session_state_is_reduced_encrypted_and_revocable(db):
    settings = _fupa_browser_settings()
    user = _admin(db)

    sanitized = sanitize_storage_state(_fupa_storage_state())
    assert "fupa-session-secret" in sanitized
    assert "accounts.google.com" not in sanitized
    assert "discard-me" not in sanitized

    session = save_fupa_browser_session(
        db,
        club_id=db.info["test_club_id"],
        raw_state=_fupa_storage_state(),
        user_id=user.id,
        settings=settings,
    )
    db.flush()

    assert session.id
    assert session.status == "active"
    assert "fupa-session-secret" not in session.encrypted_storage_state
    assert "fupa-session-secret" in decrypt_fupa_browser_session(session, settings)
    audit = db.scalar(select(AuditLog).where(AuditLog.action == "match_report.fupa_session_saved"))
    assert audit.details == {
        "key_version": "v1",
        "contains_password": False,
        "contains_encrypted_session": True,
    }

    revoke_fupa_browser_session(db, session, user_id=user.id)
    db.flush()
    assert session.status == "revoked"
    assert session.encrypted_storage_state is None


def test_fupa_session_rejects_state_without_fupa_login():
    with pytest.raises(FupaSessionError, match="keine FuPa-Sitzung"):
        sanitize_storage_state(
            '{"cookies":[{"name":"sid","value":"x",'
            '"domain":"example.org","path":"/"}],"origins":[]}'
        )


def test_browser_publication_uses_active_tenant_session_and_marks_report_published(db):
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
    approve_report(db, report, user_id=user.id)
    settings = _fupa_browser_settings()
    session = save_fupa_browser_session(
        db,
        club_id=game.club_id,
        raw_state=_fupa_storage_state(),
        user_id=user.id,
        settings=settings,
    )

    class SuccessfulPublisher:
        def publish(self, *, context, version, idempotency_key):
            assert context.club_id == game.club_id
            assert context.game_id == game.id
            assert version.report_id == report.id
            return FupaPublishResult(
                status="published",
                external_id=idempotency_key,
                external_url=game.fupa_url,
                updated_storage_state=_fupa_storage_state("rotated-fupa-session"),
            )

    publication = prepare_fupa_publication(
        db,
        report,
        user_id=user.id,
        settings=settings,
        publisher=SuccessfulPublisher(),
    )
    db.flush()

    assert publication.status == "published"
    assert publication.attempt_count == 1
    assert report.status == "published"
    assert report.published_at is not None
    assert session.status == "active"
    assert session.last_verified_at is not None
    assert "rotated-fupa-session" in decrypt_fupa_browser_session(session, settings)


def test_browser_publication_expires_session_when_fupa_requires_login(db):
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
    approve_report(db, report, user_id=user.id)
    settings = _fupa_browser_settings()
    session = save_fupa_browser_session(
        db,
        club_id=game.club_id,
        raw_state=_fupa_storage_state(),
        user_id=user.id,
        settings=settings,
    )

    class ExpiredPublisher:
        def publish(self, **_kwargs):
            raise FupaBrowserPublishError(
                "authentication_required",
                "Die FuPa-Anmeldung ist abgelaufen.",
            )

    publication = prepare_fupa_publication(
        db,
        report,
        user_id=user.id,
        settings=settings,
        publisher=ExpiredPublisher(),
    )
    db.flush()

    assert publication.status == "failed"
    assert publication.last_error_category == "authentication_required"
    assert report.status == "approved"
    assert session.status == "expired"
    assert session.last_error == "Die FuPa-Anmeldung ist abgelaufen."
    assert db.scalar(select(FupaBrowserSession).where(FupaBrowserSession.id == session.id))


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
    assert persisted.source_snapshot["provenance"]["fupa_match_id"] == 14917720
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
