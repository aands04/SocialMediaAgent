"""Read-only, explicitly enabled FUSSBALL.DE diagnostic fetcher."""
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from app.config import Settings
from app.games.provider import FussballDeProvider, ProviderError
from app.models import ProviderSnapshot, Team


class LiveTestDisabled(RuntimeError): pass

def capture(db: Session, team: Team, settings: Settings) -> ProviderSnapshot:
    if not settings.fussball_live_test_enabled:
        raise LiveTestDisabled("FUSSBALL_LIVE_TEST_ENABLED ist nicht aktiviert")
    if not team.fussball_url.startswith(("https://www.fussball.de/", "https://fussball.de/")):
        raise ValueError("Nur FUSSBALL.DE-URLs sind erlaubt")
    response = httpx.get(team.fussball_url, timeout=15, follow_redirects=True, headers={"User-Agent":"SocialMediaAgent-Diagnostic/1.0"})
    response.raise_for_status()
    payload=response.content; checksum=hashlib.sha256(payload).hexdigest(); now=datetime.now(timezone.utc)
    root=settings.provider_snapshot_root; root.mkdir(parents=True,exist_ok=True)
    relative=Path(team.id)/f"{now.strftime('%Y%m%dT%H%M%SZ')}-{checksum[:12]}.html"; target=root/relative; target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(payload)
    parsed=[]; error=None
    try:
        parsed=[{"external_id":x.external_id,"home_team":x.home_team,"away_team":x.away_team,"kickoff":x.kickoff.isoformat(),"status":x.status} for x in FussballDeProvider().parse(response.text)]
    except ProviderError as exc: error=f"Strukturänderung vermutet: {exc}"
    fixture_ids=[]
    fixture=Path("tests/fixtures/games.html")
    if fixture.is_file():
        try: fixture_ids=[x.external_id for x in FussballDeProvider().parse(fixture.read_text())]
        except ProviderError: pass
    live_ids=[x["external_id"] for x in parsed]
    comparison={"only_live":sorted(set(live_ids)-set(fixture_ids)),"only_fixture":sorted(set(fixture_ids)-set(live_ids)),"same_count":len(live_ids)==len(fixture_ids)}
    snapshot=ProviderSnapshot(team_id=team.id,source_url=team.fussball_url,status_code=response.status_code,checksum=checksum,relative_path=str(relative),parser_result={"games":parsed,"fixture_comparison":comparison},error=error)
    db.add(snapshot); db.commit(); return snapshot
