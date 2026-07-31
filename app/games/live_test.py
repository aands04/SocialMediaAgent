"""Read-only, explicitly enabled FUSSBALL.DE diagnostic fetcher."""
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.config import Settings
from app.games.provider import FussballDeProvider, ProviderError
from app.models import ProviderSnapshot, Team

BERLIN = ZoneInfo("Europe/Berlin")

class LiveTestDisabled(RuntimeError): pass

def serialize(record):
    return {
        "external_id": record.external_id,
        "home_team": record.home_team,
        "away_team": record.away_team,
        "kickoff": record.kickoff.isoformat(),
        "kickoff_berlin": record.kickoff.astimezone(BERLIN).isoformat(),
        "competition": record.competition,
        "venue": record.venue,
        "pitch": record.pitch,
        "venue_address": record.venue_address,
        "game_number": record.game_number,
        "source_url": record.source_url,
        "tracked_team_side": record.tracked_team_side,
        "status": record.status,
        "home_score": record.home_score,
        "away_score": record.away_score,
        "warnings": list(record.warnings),
    }

def capture(db: Session, team: Team, settings: Settings) -> ProviderSnapshot:
    if not settings.fussball_live_test_enabled: raise LiveTestDisabled("FUSSBALL_LIVE_TEST_ENABLED ist nicht aktiviert")
    provider=FussballDeProvider(timeout=15,max_attempts=2)
    html=provider.fetch_html(team.fussball_url); payload=html.encode("utf-8"); checksum=hashlib.sha256(payload).hexdigest(); now=datetime.now(timezone.utc)
    root=settings.provider_snapshot_root; root.mkdir(parents=True,exist_ok=True)
    relative=Path(team.id)/f"{now.strftime('%Y%m%dT%H%M%SZ')}-{checksum[:12]}.html"; target=root/relative; target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(payload)
    parsed=[]; error=None; parser_warnings=[]
    try:
        records = provider.enrich_game_details(provider.parse(html))
        parsed = [serialize(record) for record in records]
        parser_warnings = sorted(
            {warning for record in records for warning in record.warnings}
        )
    except ProviderError as exc: error=f"Strukturänderung vermutet: {exc}"
    fixture_ids=[]; fixture=Path("tests/fixtures/fussball_sv_ehlen_2627.html")
    if fixture.is_file():
        try: fixture_ids=[x.external_id for x in provider.parse(fixture.read_text(encoding="utf-8"))]
        except ProviderError: pass
    live_ids=[x["external_id"] for x in parsed]; comparison={"only_live":sorted(set(live_ids)-set(fixture_ids)),"only_fixture":sorted(set(fixture_ids)-set(live_ids)),"same_count":len(live_ids)==len(fixture_ids)}
    snapshot=ProviderSnapshot(team_id=team.id,source_url=team.fussball_url,status_code=200,checksum=checksum,relative_path=str(relative),parser_result={"team_name":team.display_name,"games":parsed,"parser_warnings":parser_warnings,"fixture_comparison":comparison,"read_only":True},error=error)
    db.add(snapshot); db.commit(); return snapshot
