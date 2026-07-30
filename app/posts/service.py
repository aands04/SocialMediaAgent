from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.logos.service import LogoCompositor, LogoValidationError, frozen_logo_set
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
from app.prompts.service import resolve_prompt
from app.rendering.service import Renderer, builtin_template
from app.textgen.service import TextGenerator


class RerenderConflict(ValueError):
    pass


def reserve_image(db:Session,team_id:str,game_id:str)->MediaAsset|None:
    existing=db.scalar(select(MediaAsset).where(MediaAsset.reserved_game_id==game_id))
    if existing:return existing
    asset=db.scalar(select(MediaAsset).where(MediaAsset.team_id==team_id,MediaAsset.active.is_(True),MediaAsset.available.is_(True),MediaAsset.reserved_game_id.is_(None)).order_by(MediaAsset.size.desc(),MediaAsset.filename).with_for_update(skip_locked=True))
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


def _upload_path(relative: str | None) -> str | None:
    if not relative:
        return None
    path = Path(relative)
    if path.is_absolute():
        return str(path)
    return str(get_settings().upload_root / path)


def _normalize_design_snapshot(value: object) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def _story_snapshot_map(value: object) -> dict[str, dict]:
    entries: list[dict] = []
    if isinstance(value, dict):
        for rule_id, entry in value.items():
            if not isinstance(entry, dict):
                continue
            normalized = dict(entry)
            normalized.setdefault("rule_id", str(rule_id))
            entries.append(normalized)
    elif isinstance(value, list):
        entries = [dict(entry) for entry in value if isinstance(entry, dict)]
    return {
        str(entry["rule_id"]): entry
        for entry in entries
        if entry.get("rule_id")
    }


def _facts(
    db: Session,
    game: Game,
    team: Team,
    asset: MediaAsset | None,
    post_type: str,
    logos: dict | None = None,
) -> dict:
    primary_font=_font(db,team.primary_font); secondary_font=_font(db,team.secondary_font)
    kickoff=game.kickoff.replace(tzinfo=timezone.utc) if game.kickoff.tzinfo is None else game.kickoff
    logos=logos or frozen_logo_set(db,game,team)
    team_logo=logos.get("team") or {}; opponent_logo=logos.get("opponent") or {}
    facts={"home_team":game.home_team,"away_team":game.away_team,"own_team":team.display_name,"kickoff":kickoff.isoformat(),"venue":game.venue,"pitch":game.pitch,"competition":game.competition,"post_type":post_type,"hashtags":team.hashtags,"primary_color":team.colors.get("primary"),"secondary_color":team.colors.get("secondary"),"style_direction":team.rules.get("style_direction"),"team_short":team.short_name,"side_label":"Heimspiel" if game.home_team in {team.display_name,team.club} else "Auswärtsspiel","player_image":_media_path(asset.relative_path) if asset else None,"team_logo":_upload_path(team_logo.get("path")),"opponent_logo":_upload_path(opponent_logo.get("path")) if not opponent_logo.get("fallback") else None,"logos":logos,"primary_font_asset":primary_font,"secondary_font_asset":secondary_font}
    if post_type=="result" and game.result_confirmed:facts["score"]=f"{game.home_score}:{game.away_score}"
    if post_type=="result" and not game.result_confirmed: raise ValueError("Ergebnis ist nicht bestätigt")
    return facts


def create_post(db:Session,game:Game,team:Team,generator:TextGenerator,renderer:Renderer,post_type="announcement",logo_snapshot:dict|None=None)->Post:
    if game.status=="provisional" or game.overrides.get("automation_blocked"):
        raise ValueError("Vorläufige Spiele sind für die Beitragserstellung gesperrt")
    existing=db.scalar(select(Post).where(Post.game_id==game.id,Post.post_type==post_type,Post.active_key=="active"))
    if existing:return existing
    page=db.get(InstagramPage,team.instagram_page_id); warnings=[]
    logos=logo_snapshot or frozen_logo_set(db,game,team)
    if not logos.get("team"):
        warnings.append("Eigenes Mannschaftslogo fehlt; der Beitrag darf nicht freigegeben werden")
    asset=reserve_image(db,team.id,game.id)
    if not asset:warnings.append("Kein unverbrauchtes Spielerbild; neutrale Vorlage verwendet")
    if getattr(renderer,"is_ai",False) and not asset:
        raise ValueError("Für eine KI-Grafik ist ein unverbrauchtes Spielerbild erforderlich")
    feed_design=_design(db,team.feed_template,post_type,"feed")
    facts=_facts(db,game,team,asset,post_type,logos)
    feed_prompt=None; text_prompt=None
    if getattr(renderer,"is_ai",False):
        feed_prompt_name=team.rules.get(f"image_prompt_feed_{post_type}",team.rules.get("image_prompt_feed","default-image-feed"))
        feed_prompt=resolve_prompt(db,feed_prompt_name,"image",post_type,"feed",facts)
    if getattr(generator,"is_ai",False):
        text_prompt_name=team.rules.get(f"text_prompt_{post_type}",team.rules.get("text_prompt",f"default-text-{post_type}"))
        text_prompt=resolve_prompt(db,text_prompt_name,"text",post_type,"none",facts)
        facts={**facts,"text_prompt":text_prompt}
    primary_font=facts["primary_font_asset"]; secondary_font=facts["secondary_font_asset"]
    post=Post(game_id=game.id,team_id=team.id,instagram_page_id=page.id,post_type=post_type,status=PostStatus.CREATING,media_asset_id=asset.id if asset else None,critical_warnings=warnings,design_snapshot={"mode":{"image":"openai" if feed_prompt else "playwright","text":"openai" if text_prompt else "fixture","manual_approval_required":True},"feed":feed_design,"prompts":{"feed":feed_prompt.snapshot() if feed_prompt else None,"text":text_prompt.snapshot() if text_prompt else None},"stories":[],"logos":logos,"media":{},"fonts":{"primary":primary_font or {"family":team.primary_font,"fallback":True},"secondary":secondary_font or {"family":team.secondary_font,"fallback":True}},"colors":team.colors})
    db.add(post); db.flush(); generated_text=generator.generate(facts); post.text=generated_text.text
    post.design_snapshot={**post.design_snapshot,"text_generation":{"model":generated_text.model,"prompt_version":generated_text.prompt_version,"tokens":generated_text.tokens}}
    post.feed_path=str(renderer.render("feed",f"{post.id}/feed-v1.png",{**facts,"template":feed_design,"image_prompt":feed_prompt}))
    if hasattr(renderer,"metadata_for"):
        post.design_snapshot={**post.design_snapshot,"media":{"feed":renderer.metadata_for(post.feed_path)}}
    feed_at=game.kickoff-timedelta(minutes=int(team.rules.get("feed_before_minutes",1440)))
    db.add(PublicationJob(post_id=post.id,game_id=game.id,team_id=team.id,instagram_page_id=page.id,kind="feed",media_path=post.feed_path,text_snapshot=post.text,scheduled_at=feed_at,idempotency_key=f"{post.id}:feed:v1"))
    rules=db.scalars(select(StoryRule).where(StoryRule.team_id==team.id,StoryRule.post_type==post_type,StoryRule.active.is_(True)).order_by(StoryRule.sort_order)).all(); seen=set()
    story_snapshots=[]
    for rule in rules:
        at=story_time(rule,game)
        collision=(at,rule.template)
        if collision in seen: warnings.append(f"Story-Regel {rule.name} kollidiert und wurde nicht doppelt geplant"); continue
        story_design=_design(db,rule.template,post_type,"story")
        story_prompt_name=rule.prompt_template
        if not story_prompt_name or story_prompt_name=="default-image-story":
            story_prompt_name=team.rules.get(f"image_prompt_story_{post_type}",team.rules.get("image_prompt_story","default-image-story"))
        story_prompt=resolve_prompt(db,story_prompt_name,"image",post_type,"story",facts) if getattr(renderer,"is_ai",False) else None
        seen.add(collision)
        if story_prompt:
            path=str(renderer.render("story",f"{post.id}/story-{rule.id}-v1.png",{**facts,"template":story_design,"image_prompt":story_prompt}))
        else:
            path=str(renderer.render("story",f"{post.id}/story-{rule.id}-v1.png",{**facts,"template":story_design}))
        story_snapshots.append({"rule_id":rule.id,"template":story_design,"prompt":story_prompt.snapshot() if story_prompt else None,"media_version":1,"rendering":renderer.metadata_for(path) if hasattr(renderer,"metadata_for") else {}})
        db.add(PublicationJob(post_id=post.id,game_id=game.id,team_id=team.id,instagram_page_id=rule.instagram_page_id or page.id,story_rule_id=rule.id,kind="story",media_path=path,text_snapshot=post.text if rule.text_variant else None,scheduled_at=at,idempotency_key=f"{post.id}:story:{rule.id}:v1"))
    post.design_snapshot={**post.design_snapshot,"stories":story_snapshots}
    post.critical_warnings=warnings; post.status=PostStatus.INCOMPLETE if warnings else PostStatus.PENDING; db.commit(); return post


def rerender_post(db:Session,post:Post,renderer:Renderer,story_job_ids:list[str]|None=None,logo_snapshot:dict|None=None)->Post:
    game=db.get(Game,post.game_id); team=db.get(Team,post.team_id); asset=db.get(MediaAsset,post.media_asset_id) if post.media_asset_id else None
    if not game or not team: raise ValueError("Beitrag hat keine gültigen Spiel- oder Mannschaftsdaten")
    jobs=list(db.scalars(select(PublicationJob).where(PublicationJob.post_id==post.id).with_for_update())); selected=set(story_job_ids or [])
    story_jobs={job.id:job for job in jobs if job.kind=="story"}
    if not selected.issubset(story_jobs): raise RerenderConflict("Mindestens eine ausgewählte Story gehört nicht zu diesem Beitrag")
    if any(job.status==JobStatus.PUBLISHED for job in jobs if job.kind=="feed"):
        raise RerenderConflict("Der Feed wurde bereits veröffentlicht und darf nicht neu erzeugt werden")
    if any(story_jobs[job_id].status==JobStatus.PUBLISHED for job_id in selected):
        raise RerenderConflict("Eine ausgewählte Story wurde bereits veröffentlicht und darf nicht neu erzeugt werden")
    logos=logo_snapshot or frozen_logo_set(db,game,team)
    facts=_facts(db,game,team,asset,post.post_type,logos)
    old_snapshot=_normalize_design_snapshot(post.design_snapshot)
    snapshots=_story_snapshot_map(old_snapshot.get("stories"))
    feed_design=_design(db,team.feed_template,post.post_type,"feed"); post.feed_version+=1
    feed_prompt_name=team.rules.get(f"image_prompt_feed_{post.post_type}",team.rules.get("image_prompt_feed","default-image-feed"))
    feed_prompt=resolve_prompt(db,feed_prompt_name,"image",post.post_type,"feed",facts) if getattr(renderer,"is_ai",False) else None
    post.feed_path=str(renderer.render("feed",f"{post.id}/feed-v{post.feed_version}.png",{**facts,"template":feed_design,"image_prompt":feed_prompt}))
    for job in jobs:
        if job.kind=="feed":
            job.media_path=post.feed_path; job.version+=1; job.idempotency_key=f"{post.id}:feed:v{post.feed_version}"
        elif job.id in selected:
            rule=db.get(StoryRule,job.story_rule_id) if job.story_rule_id else None
            design=_design(db,rule.template if rule else "default-story",post.post_type,"story"); media_version=int(snapshots.get(job.story_rule_id,{}).get("media_version",1))+1
            story_prompt_name=rule.prompt_template if rule else None
            if not story_prompt_name or story_prompt_name=="default-image-story":
                story_prompt_name=team.rules.get(f"image_prompt_story_{post.post_type}",team.rules.get("image_prompt_story","default-image-story"))
            story_prompt=resolve_prompt(db,story_prompt_name,"image",post.post_type,"story",facts) if getattr(renderer,"is_ai",False) else None
            job.media_path=str(renderer.render("story",f"{post.id}/story-{job.story_rule_id}-v{media_version}.png",{**facts,"template":design,"image_prompt":story_prompt})); job.version+=1; job.idempotency_key=f"{post.id}:story:{job.story_rule_id}:v{media_version}"
            snapshots[job.story_rule_id]={"rule_id":job.story_rule_id,"template":design,"prompt":story_prompt.snapshot() if story_prompt else None,"media_version":media_version,"rendering":renderer.metadata_for(job.media_path) if hasattr(renderer,"metadata_for") else {}}
    raw_prompts=old_snapshot.get("prompts")
    prompt_snapshot=dict(raw_prompts) if isinstance(raw_prompts,dict) else {}
    prompt_snapshot["feed"]=feed_prompt.snapshot() if feed_prompt else None
    media_snapshot=dict(old_snapshot.get("media") or {})
    if hasattr(renderer,"metadata_for"):
        media_snapshot["feed"]=renderer.metadata_for(post.feed_path)
    post.design_snapshot={**old_snapshot,"feed":feed_design,"prompts":prompt_snapshot,"stories":list(snapshots.values()),"logos":logos,"media":media_snapshot,"fonts":{"primary":facts["primary_font_asset"] or {"family":team.primary_font,"fallback":True},"secondary":facts["secondary_font_asset"] or {"family":team.secondary_font,"fallback":True}},"colors":team.colors}
    if logos.get("team"):
        post.critical_warnings=[
            warning
            for warning in (post.critical_warnings or [])
            if warning
            not in {
                "Logo-Zuordnung wurde geändert; Grafiken neu zusammensetzen",
                (
                    "Logo-Zuordnung wurde geändert; Grafiken mit aktualisierten "
                    "Logo-Referenzen neu erzeugen"
                ),
                "Eigenes Mannschaftslogo fehlt; der Beitrag darf nicht freigegeben werden",
            }
        ]
    was_approved=post.status in {PostStatus.APPROVED,PostStatus.SCHEDULED,PostStatus.PARTIAL}
    post.version+=1
    if was_approved:
        post.status=PostStatus.REAPPROVAL; post.approved_version=None
        for job in jobs:
            if job.status!=JobStatus.PUBLISHED:
                job.status=JobStatus.UNAPPROVED; job.approval_status="reapproval_required"; job.approved_post_version=None; job.error="Grafiken wurden neu erzeugt; erneute Freigabe erforderlich"
    db.flush(); return post


def _safe_generated_base(value: str | None) -> Path:
    if not value:
        raise LogoValidationError(
            "Für diese Legacy-Grafik ist keine eingefrorene KI-Grundgrafik vorhanden."
        )
    root = get_settings().generated_root.resolve()
    path = Path(value).resolve()
    if (
        not path.is_relative_to(root)
        or path.is_symlink()
        or not path.is_file()
    ):
        raise LogoValidationError("Die eingefrorene KI-Grundgrafik ist nicht sicher verfügbar.")
    return path


def logo_recompose_availability(
    post: Post,
    jobs: list[PublicationJob],
) -> dict:
    """Report whether every publication has a safe, frozen AI base image."""
    snapshot = _normalize_design_snapshot(post.design_snapshot)
    feed_metadata = dict((snapshot.get("media") or {}).get("feed") or {})
    stories = _story_snapshot_map(snapshot.get("stories"))

    ai_reference_reason = (
        "Die Logos sind Bestandteil der KI-Komposition. Eine Logoänderung "
        "erfordert eine vollständige Bild-Neugenerierung."
    )

    def status(rendering: dict) -> dict:
        integration = rendering.get("logo_integration")
        if isinstance(integration, dict) and integration.get("mode") == "ai-reference":
            return {
                "available": False,
                "reason": ai_reference_reason,
                "requires_full_rerender": True,
            }
        try:
            return {
                "available": True,
                "path": str(_safe_generated_base(rendering.get("ai_base_path"))),
                "legacy_compositor": True,
            }
        except LogoValidationError as exc:
            return {"available": False, "reason": str(exc)}

    story_status = {}
    for publication in jobs:
        if publication.kind != "story":
            continue
        entry = dict(stories.get(publication.story_rule_id) or {})
        rendering = dict(entry.get("rendering") or {})
        story_status[publication.id] = status(rendering)
    feed_status = status(feed_metadata)
    return {
        "feed": feed_status,
        "stories": story_status,
        "all_available": feed_status["available"]
        and all(item["available"] for item in story_status.values()),
    }


def logo_recompose_preflight(
    post: Post,
    jobs: list[PublicationJob],
    story_job_ids: list[str],
) -> dict:
    """Resolve all required base images before writing a recomposed file."""
    availability = logo_recompose_availability(post, jobs)
    missing = []
    if not availability["feed"]["available"]:
        missing.append("Feed")
    selected = set(story_job_ids)
    for publication in jobs:
        if publication.id not in selected:
            continue
        item = availability["stories"].get(publication.id) or {"available": False}
        if not item["available"]:
            missing.append(
                f"Story {publication.scheduled_at.astimezone().strftime('%d.%m.%Y %H:%M')}"
            )
    if missing:
        raise LogoValidationError(
            "Lokale Logo-Neuzusammensetzung nicht möglich: Für "
            + ", ".join(missing)
            + " fehlt eine separat eingefrorene KI-Grundgrafik. "
            "Bitte stattdessen „Grafiken neu erzeugen“ verwenden. "
            "Dabei werden die verifizierten Logos als Referenzbilder in die "
            "KI-Komposition integriert."
        )
    return {
        "feed": Path(availability["feed"]["path"]),
        "stories": {
            job_id: Path(item["path"])
            for job_id, item in availability["stories"].items()
            if job_id in selected
        },
    }


def recompose_post_logos(
    db: Session,
    post: Post,
    story_job_ids: list[str],
    logo_snapshot: dict,
) -> Post:
    game=db.get(Game,post.game_id); team=db.get(Team,post.team_id)
    if not game or not team:
        raise LogoValidationError("Spiel oder Mannschaft ist nicht mehr verfügbar.")
    jobs=list(db.scalars(select(PublicationJob).where(PublicationJob.post_id==post.id).with_for_update()))
    selected=set(story_job_ids)
    story_jobs={job.id:job for job in jobs if job.kind=="story"}
    if not selected.issubset(story_jobs):
        raise RerenderConflict("Mindestens eine ausgewählte Story gehört nicht zu diesem Beitrag")
    feed_job=next((job for job in jobs if job.kind=="feed"),None)
    if not feed_job or feed_job.status==JobStatus.PUBLISHED:
        raise RerenderConflict("Der Feed wurde bereits veröffentlicht oder fehlt und darf nicht neu zusammengesetzt werden")
    if any(story_jobs[job_id].status==JobStatus.PUBLISHED for job_id in selected):
        raise RerenderConflict("Eine ausgewählte Story wurde bereits veröffentlicht")
    sources=logo_recompose_preflight(post,jobs,list(selected))
    old_snapshot=_normalize_design_snapshot(post.design_snapshot)
    media_snapshot=dict(old_snapshot.get("media") or {})
    feed_metadata=dict(media_snapshot.get("feed") or {})
    compositor=LogoCompositor(get_settings().upload_root)
    validator=Renderer(
        get_settings().generated_root,
        get_settings().media_root,
        get_settings().upload_root,
    )
    post.feed_version+=1
    feed_target=(get_settings().generated_root / post.id / f"feed-v{post.feed_version}.png").resolve()
    feed_composition=compositor.compose(
        base_path=sources["feed"],
        output_path=feed_target,
        kind="feed",
        logos=logo_snapshot,
    )
    validator.validate(feed_target,"feed")
    post.feed_path=str(feed_target)
    feed_job.media_path=post.feed_path
    feed_job.version+=1
    feed_job.idempotency_key=f"{post.id}:feed:v{post.feed_version}"
    media_snapshot["feed"]={
        **feed_metadata,
        "final_path":str(feed_target),
        "composition":feed_composition,
        "logo_only_recomposition":True,
    }
    snapshots=_story_snapshot_map(old_snapshot.get("stories"))
    for job_id in selected:
        publication=story_jobs[job_id]
        entry=dict(snapshots.get(publication.story_rule_id) or {})
        rendering=dict(entry.get("rendering") or {})
        version=int(entry.get("media_version",1))+1
        target=(
            get_settings().generated_root
            / post.id
            / f"story-{publication.story_rule_id}-v{version}.png"
        ).resolve()
        composition=compositor.compose(
            base_path=sources["stories"][job_id],
            output_path=target,
            kind="story",
            logos=logo_snapshot,
        )
        validator.validate(target,"story")
        publication.media_path=str(target)
        publication.version+=1
        publication.idempotency_key=f"{post.id}:story:{publication.story_rule_id}:v{version}"
        snapshots[publication.story_rule_id]={
            **entry,
            "rule_id":publication.story_rule_id,
            "media_version":version,
            "rendering":{
                **rendering,
                "final_path":str(target),
                "composition":composition,
                "logo_only_recomposition":True,
            },
        }
    post.design_snapshot={
        **old_snapshot,
        "logos":logo_snapshot,
        "media":media_snapshot,
        "stories":list(snapshots.values()),
    }
    removable={
        "Logo-Zuordnung wurde geändert; Grafiken neu zusammensetzen",
        (
            "Logo-Zuordnung wurde geändert; Grafiken mit aktualisierten "
            "Logo-Referenzen neu erzeugen"
        ),
        "Eigenes Mannschaftslogo fehlt; der Beitrag darf nicht freigegeben werden",
    }
    post.critical_warnings=[
        warning for warning in (post.critical_warnings or []) if warning not in removable
    ]
    post.version+=1
    post.status=PostStatus.REAPPROVAL
    post.approved_version=None
    for publication in jobs:
        if publication.status!=JobStatus.PUBLISHED:
            publication.status=JobStatus.UNAPPROVED
            publication.approval_status="reapproval_required"
            publication.approved_post_version=None
            publication.error="Logos wurden neu zusammengesetzt; erneute Freigabe erforderlich"
    db.flush()
    return post

def reschedule_game(db:Session,game:Game,new_kickoff:datetime):
    old=game.kickoff; game.original_kickoff=game.original_kickoff or old; game.kickoff=new_kickoff
    for job in db.scalars(select(PublicationJob).where(PublicationJob.game_id==game.id,PublicationJob.status.not_in([JobStatus.PUBLISHED,JobStatus.CANCELLED]))):
        if job.absolute_time: job.stale_time=True
        else: job.scheduled_at += new_kickoff-old
    for post in db.scalars(select(Post).where(Post.game_id==game.id,Post.status.in_([PostStatus.APPROVED,PostStatus.SCHEDULED]))): post.status=PostStatus.REAPPROVAL
    db.commit()
