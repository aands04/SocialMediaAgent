import argparse

from app.config import get_settings
from app.db import SessionLocal
from app.games.live_test import capture
from app.models import Team

parser = argparse.ArgumentParser(
    description="Nur lesender, explizit aktivierter FUSSBALL.DE-Strukturtest"
)
parser.add_argument("team_id")
args = parser.parse_args()
with SessionLocal() as db:
    team = db.get(Team, args.team_id)
    if not team:
        raise SystemExit("Mannschaft nicht gefunden")
    result = capture(db, team, get_settings())
    print(
        f"Snapshot {result.relative_path}; Spiele={len(result.parser_result['games'])}; Fehler={result.error or 'keiner'}"
    )
