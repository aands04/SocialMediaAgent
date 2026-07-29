from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    DesignTemplate,
    FontAsset,
    Game,
    InstagramPage,
    JobStatus,
    MediaAsset,
    Post,
    PostStatus,
    PublicationJob,
    StoryRule,
    Team,
)
from app.rendering.service import Renderer, builtin_template
from app.textgen.service import TextGenerator


def reserve_image(db:Session,team_id:str,game_id:str)->MediaAsset|None:
    existing=db.scalar(select(MediaAsset).where(MediaAsset.reserved_game_id==game_id))
    if existing:return existing
    asset=db.scalar(select(MediaAsset).where(MediaAsset.team_id==team_id,MediaAsset.active.is_(True),MediaAsset.available.is_(True),MediaAsset.reserved_game_id.is_(None)).with_for_update(skip_locked=True))
    if asset: asset.reserved_game_id=game_id; asset.uses+=1; db.flush()
    return asset
def story_time(rule:StoryRule,game:Game,approved_at:datetime|None=None)->datetime:
    refs={"kickoff":game.kickoff,"planned_end":game.kickoff+timedelta(minutes=120),"approval":approved_at}
    base=refs.get(rule.reference) or game.checked_at
    delta=timedelta(minutes=rule.offset_minutes)*(1 if rule.direction=="after" else -1)
    result=base+delta
    if rule.next_day: result+=timedelta(days=1)
    if rule.fixed_time:
        h,m=map(int,rule.fixed_time.split(":")); result=result.replace(hour=h,minute=m,second=0,microsecond=0)
    return result


def _design(db: Session, name: str, post_type: str, kind: str) -> dict:
    item = db.scalar(
        select(DesignTemplate).where(
            DesignTemplate.name == name,
            DesignTemplate.post_type == post_type,
            DesignTemplate.media_kind == kind,
            DesignTemplate.active.is_(True),
            DesignTemplate.archived_at.is_(None),
        ).order_by(DesignTemplate.version.desc())
    )
    if not item:
        return builtin_template(f"default-{kind}", post_type, kind)
    return {
        "id": item.id,
        "name": item.name,
        "version": item.version,
        "post_type": item.post_type,
        "media_kind": item.media_kind,
        "html_template": item.html_template,
        "css": item.css,
        "builtin": False,
    }


def _font(db: Session, configured: str) -> dict | None:
    item = db.scalar(
        select(FontAsset).where(
            (FontAsset.name == configured) | (FontAsset.family == configured),
            FontAsset.active.is_(True),
            FontAsset.archived_at.is_(None),
        )
    )
    return {"id": item.id, "family": item.family, "path": item.relative_path} if item else None


def _media_path(relative: str | None) -> str | None:
    if not relative:
        return None
    path = Path(relative)
    if path.is_absolute():
        return str(path)
    return str(get_settings().media_root / path)


def create_post(db:Session,game:Game,team:Team,generator:TextGenerator,renderer:Renderer,post_type="announcement")->Post:
    if game.status=="provisional" or game.overrides.get("automation_blocked"):
        raise ValueError("Vorläufige Spiele sind für die Beitragserstellung gesperrt")
    existing=db.scalar(select(Post).where(Post.game_id==game.id,Post.post_type==post_type,Post.active_key=="active"))
    if existing:return existing
    page=db.get(InstagramPage,team.instagram_page_id); warnings=[]
    asset=reserve_image(db,team.id,game.id)
    if not asset:warnings.append("Kein unverbrauchtes Spielerbild; neutrale Vorlage verwendet")
    feed_design=_design(db,team.feed_template,post_type,"feed")
    primary_font=_font(db,team.primary_font); secondary_font=_font(db,team.secondary_font)
    kickoff=game.kickoff.replace(tzinfo=timezone.utc) if game.kickoff.tzinfo is None else game.kickoff
    facts={"home_team":game.home_team,"away_team":game.away_team,"kickoff":kickoff.isoformat(),"venue":game.venue,"competition":game.competition,"post_type":post_type,"hashtags":team.hashtags,"primary_color":team.colors.get("primary"),"secondary_color":team.colors.get("secondary"),"team_short":team.short_name,"side_label":"Heimspiel" if game.home_team in {team.display_name,team.club} else "Auswärtsspiel","player_image":_media_path(asset.relative_path) if asset else None,"team_logo":_media_path(team.logo_path),"opponent_logo":_media_path(game.overrides.get("opponent_logo_path")),"primary_font_asset":primary_font,"secondary_font_asset":secondary_font}
    if post_type=="result" and game.result_confirmed:facts["score"]=f"{game.home_score}:{game.away_score}"
    if post_type=="result" and not game.result_confirmed: raise ValueError("Ergebnis ist nicht bestätigt")
    post=Post(game_id=game.id,team_id=team.id,instagram_page_id=page.id,post_type=post_type,status=PostStatus.CREATING,media_asset_id=asset.id if asset else None,critical_warnings=warnings,design_snapshot={"feed":feed_design,"stories":[],"fonts":{"primary":primary_font or {"family":team.primary_font,"fallback":True},"secondary":secondary_font or {"family":team.secondary_font,"fallback":True}},"colors":team.colors})
    db.add(post); db.flush(); post.text=generator.generate(facts).text
    post.feed_path=str(renderer.render("feed",f"{post.id}/feed-v1.png",{**facts,"template":feed_design}))
    feed_at=game.kickoff-timedelta(minutes=int(team.rules.get("feed_before_minutes",1440)))
    db.add(PublicationJob(post_id=post.id,game_id=game.id,team_id=team.id,instagram_page_id=page.id,kind="feed",media_path=post.feed_path,text_snapshot=post.text,scheduled_at=feed_at,idempotency_key=f"{post.id}:feed:v1"))
    rules=db.scalars(select(StoryRule).where(StoryRule.team_id==team.id,StoryRule.post_type==post_type,StoryRule.active.is_(True)).order_by(StoryRule.sort_order)).all(); seen=set()
    for rule in rules:
        at=story_time(rule,game)
        collision=(at,rule.template)
        if collision in seen: warnings.append(f"Story-Regel {rule.name} kollidiert und wurde nicht doppelt geplant"); continue
        story_design=_design(db,rule.template,post_type,"story")
        post.design_snapshot["stories"].append({"rule_id":rule.id,"template":story_design})
        seen.add(collision); path=str(renderer.render("story",f"{post.id}/story-{rule.id}-v1.png",{**facts,"template":story_design}))
        db.add(PublicationJob(post_id=post.id,game_id=game.id,team_id=team.id,instagram_page_id=rule.instagram_page_id or page.id,story_rule_id=rule.id,kind="story",media_path=path,text_snapshot=post.text if rule.text_variant else None,scheduled_at=at,idempotency_key=f"{post.id}:story:{rule.id}:v1"))
    post.critical_warnings=warnings; post.status=PostStatus.INCOMPLETE if warnings else PostStatus.PENDING; db.commit(); return post

def reschedule_game(db:Session,game:Game,new_kickoff:datetime):
    old=game.kickoff; game.original_kickoff=game.original_kickoff or old; game.kickoff=new_kickoff
    for job in db.scalars(select(PublicationJob).where(PublicationJob.game_id==game.id,PublicationJob.status.not_in([JobStatus.PUBLISHED,JobStatus.CANCELLED]))):
        if job.absolute_time: job.stale_time=True
        else: job.scheduled_at += new_kickoff-old
    for post in db.scalars(select(Post).where(Post.game_id==game.id,Post.status.in_([PostStatus.APPROVED,PostStatus.SCHEDULED]))): post.status=PostStatus.REAPPROVAL
    db.commit()
