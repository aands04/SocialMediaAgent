"""Explicit, audited and idempotent snapshot-to-game import; never creates posts."""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, Game, ProviderSnapshot, Team, User


class SnapshotImportError(ValueError): pass

def preview_snapshot(snapshot:ProviderSnapshot)->list[dict]:
    games=snapshot.parser_result.get("games",[]) if snapshot.parser_result else []
    return [game for game in games if all(game.get(key) for key in ("external_id","home_team","away_team","kickoff"))]

def import_snapshot(db:Session,snapshot:ProviderSnapshot,user:User)->dict:
    team=db.get(Team,snapshot.team_id)
    if not team: raise SnapshotImportError("Snapshot hat keine gültige Mannschaft")
    if snapshot.error: raise SnapshotImportError("Snapshot enthält einen Parserfehler")
    created=updated=unchanged=0; ids=[]
    for item in preview_snapshot(snapshot):
        kickoff=datetime.fromisoformat(item["kickoff"])
        if kickoff.tzinfo is None: raise SnapshotImportError("Anpfiff ohne Zeitzone wird nicht übernommen")
        kickoff=kickoff.astimezone(timezone.utc); external_id=item["external_id"]
        game=db.scalar(select(Game).where(Game.team_id==team.id,Game.provider=="fussball.de",Game.external_id==external_id).with_for_update())
        values={"home_team":item["home_team"],"away_team":item["away_team"],"kickoff":kickoff,"competition":item.get("competition"),"status":item.get("status") or "scheduled","home_score":item.get("home_score"),"away_score":item.get("away_score"),"source_url":item.get("source_url") or snapshot.source_url,"checked_at":snapshot.fetched_at,"result_confirmed":False,"overrides":{"game_number":item.get("game_number"),"snapshot_id":snapshot.id,"automation_blocked":item.get("status")=="provisional"}}
        if game is None:
            game=Game(team_id=team.id,provider="fussball.de",external_id=external_id,**values); db.add(game); db.flush(); created+=1
        else:
            def comparable(value):
                return value.replace(tzinfo=timezone.utc) if isinstance(value,datetime) and value.tzinfo is None else value
            changed=any(comparable(getattr(game,key))!=comparable(value) for key,value in values.items() if key!="overrides") or game.overrides!=values["overrides"]
            if changed:
                if game.kickoff!=kickoff: game.original_kickoff=game.original_kickoff or game.kickoff
                for key,value in values.items(): setattr(game,key,value)
                game.version+=1; updated+=1
            else: unchanged+=1
        ids.append(game.id)
    if not ids: raise SnapshotImportError("Snapshot enthält keine vollständig parsebaren Spiele")
    db.add(AuditLog(user_id=user.id,team_id=team.id,action="provider_snapshot.games_imported",entity_type="provider_snapshot",entity_id=snapshot.id,details={"created":created,"updated":updated,"unchanged":unchanged,"game_ids":ids,"posts_created":False})); db.commit()
    return {"created":created,"updated":updated,"unchanged":unchanged,"game_ids":ids}
