import hashlib
import mimetypes
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.approvals.service import ApprovalError, approve, edit_text
from app.auth.service import hash_password
from app.config import get_settings
from app.db import get_db
from app.media.storage import LocalStorageProvider, StorageError
from app.models import (
    AuditLog,
    DesignTemplate,
    FontAsset,
    Game,
    InstagramPage,
    MediaAsset,
    Post,
    PromptTemplate,
    PublicationJob,
    Role,
    StoryRule,
    Team,
    User,
    UserTeam,
)
from app.web import berlin_datetime, check_csrf, csrf_token, current_user, require, require_admin

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["berlin"] = berlin_datetime
settings = get_settings()

def render(request, name, current, **context):
    return templates.TemplateResponse(request, name, {"user": current, "csrf": csrf_token(request), **context})

def audit(db, current, action, entity, entity_id=None, team_id=None, details=None):
    db.add(AuditLog(user_id=current.id, team_id=team_id, action=action, entity_type=entity, entity_id=entity_id, details=details or {}))

def redirect(path, message="Gespeichert"):
    return RedirectResponse(f"{path}?notice={message}", 303)

@router.get("/teams", response_class=HTMLResponse)
def teams(request: Request, current=Depends(current_user), db: Session=Depends(get_db)):
    require(current, db, "view")
    items=db.scalars(select(Team).where(Team.archived_at.is_(None)).order_by(Team.display_name)).all()
    pages=db.scalars(select(InstagramPage).where(InstagramPage.archived_at.is_(None))).all()
    return render(request,"teams.html",current,items=items,pages=pages,title="Mannschaften")

@router.post("/teams")
def create_team(request:Request,csrf_token_value:str=Form(alias="csrf_token"),internal_name:str=Form(),display_name:str=Form(),short_name:str=Form(),slug:str=Form(),club:str=Form(),fussball_url:str=Form(),instagram_page_id:str=Form(),media_subdir:str=Form(),current=Depends(current_user),db:Session=Depends(get_db)):
    check_csrf(request,csrf_token_value); require_admin(current)
    if not fussball_url.startswith(("https://www.fussball.de/","https://fussball.de/")): raise HTTPException(422,"Ungültige FUSSBALL.DE-URL")
    try: LocalStorageProvider(settings.media_root).resolve(media_subdir)
    except StorageError as e: raise HTTPException(422,str(e)) from e
    page=db.get(InstagramPage,instagram_page_id)
    if not page or not page.active: raise HTTPException(422,"Instagram-Seite muss aktiv sein")
    item=Team(internal_name=internal_name,display_name=display_name,short_name=short_name,slug=slug,club=club,fussball_url=fussball_url,instagram_page_id=page.id,media_subdir=media_subdir)
    db.add(item); db.flush(); audit(db,current,"team.created","team",item.id,item.id); db.commit(); return redirect("/teams")

@router.post("/teams/{team_id}/state")
def team_state(team_id:str,request:Request,csrf_token_value:str=Form(alias="csrf_token"),action:str=Form(),version:int=Form(),current=Depends(current_user),db:Session=Depends(get_db)):
    check_csrf(request,csrf_token_value); require_admin(current); item=db.get(Team,team_id)
    if not item: raise HTTPException(404)
    if item.version!=version: raise HTTPException(409,"Datensatz wurde zwischenzeitlich geändert")
    if action=="archive": item.archived_at=datetime.now(timezone.utc); item.active=False
    elif action=="toggle": item.active=not item.active
    else: raise HTTPException(422,"Unbekannte Aktion")
    item.version+=1; audit(db,current,f"team.{action}","team",item.id,item.id); db.commit(); return redirect("/teams")

@router.get("/instagram",response_class=HTMLResponse)
def instagram(request:Request,current=Depends(current_user),db:Session=Depends(get_db)):
    require(current,db,"view"); items=db.scalars(select(InstagramPage).where(InstagramPage.archived_at.is_(None)).order_by(InstagramPage.display_name)).all(); return render(request,"instagram.html",current,items=items,title="Instagram-Seiten")

@router.post("/instagram")
def create_instagram(request:Request,csrf_token_value:str=Form(alias="csrf_token"),internal_name:str=Form(),display_name:str=Form(),username:str=Form(),club:str=Form(),account_id:str=Form(default=""),current=Depends(current_user),db:Session=Depends(get_db)):
    check_csrf(request,csrf_token_value); require_admin(current)
    item=InstagramPage(internal_name=internal_name,display_name=display_name,username=username.lstrip("@"),club=club,account_id=account_id or None,active=False,publishing_enabled=False,connection_status="unconfigured")
    db.add(item); db.flush(); audit(db,current,"instagram.created","instagram_page",item.id); db.commit(); return redirect("/instagram","Seite angelegt – sicher deaktiviert")

@router.post("/instagram/{page_id}/state")
def instagram_state(page_id:str,request:Request,csrf_token_value:str=Form(alias="csrf_token"),action:str=Form(),version:int=Form(),current=Depends(current_user),db:Session=Depends(get_db)):
    check_csrf(request,csrf_token_value); require_admin(current); item=db.get(InstagramPage,page_id)
    if not item: raise HTTPException(404)
    if item.version!=version: raise HTTPException(409,"Datensatz wurde zwischenzeitlich geändert")
    if action=="archive": item.archived_at=datetime.now(timezone.utc); item.active=False; item.publishing_enabled=False
    elif action=="mock-connect" and settings.publisher_mode!="live": item.connection_status="connected"; item.active=True; item.last_check_at=datetime.now(timezone.utc)
    elif action=="toggle-publishing": item.publishing_enabled=not item.publishing_enabled
    else: raise HTTPException(422,"Aktion im aktuellen Modus nicht zulässig")
    item.version+=1; audit(db,current,f"instagram.{action}","instagram_page",item.id); db.commit(); return redirect("/instagram")

@router.get("/users",response_class=HTMLResponse)
def users(request:Request,current=Depends(current_user),db:Session=Depends(get_db)):
    require_admin(current); items=db.scalars(select(User).where(User.archived_at.is_(None)).order_by(User.email)).all(); teams=db.scalars(select(Team).where(Team.archived_at.is_(None))).all(); assignments={(x.user_id,x.team_id) for x in db.scalars(select(UserTeam))}; return render(request,"users.html",current,items=items,teams=teams,assignments=assignments,title="Benutzer und Rechte")

@router.post("/users")
def create_user(request:Request,csrf_token_value:str=Form(alias="csrf_token"),email:str=Form(),password:str=Form(),role:Role=Form(),all_teams:bool=Form(default=False),current=Depends(current_user),db:Session=Depends(get_db)):
    check_csrf(request,csrf_token_value); require_admin(current)
    if len(password)<12: raise HTTPException(422,"Passwort muss mindestens 12 Zeichen haben")
    item=User(email=email.lower(),password_hash=hash_password(password),role=role,all_teams=all_teams); db.add(item); db.flush(); audit(db,current,"user.created","user",item.id,details={"role":role.value}); db.commit(); return redirect("/users")

@router.post("/users/{user_id}/teams")
def assign_user_teams(user_id:str,request:Request,csrf_token_value:str=Form(alias="csrf_token"),team_ids:list[str]=Form(default=[]),all_teams:bool=Form(default=False),current=Depends(current_user),db:Session=Depends(get_db)):
    check_csrf(request,csrf_token_value); require_admin(current); item=db.get(User,user_id)
    if not item: raise HTTPException(404)
    db.query(UserTeam).filter(UserTeam.user_id==user_id).delete(); item.all_teams=all_teams
    if not all_teams:
        for team_id in set(team_ids):
            if not db.get(Team,team_id): raise HTTPException(422,"Unbekannte Mannschaft")
            db.add(UserTeam(user_id=user_id,team_id=team_id))
    audit(db,current,"user.teams_changed","user",user_id,details={"all_teams":all_teams,"teams":team_ids}); db.commit(); return redirect("/users")

@router.get("/media",response_class=HTMLResponse)
def media(request:Request,team_id:str|None=None,current=Depends(current_user),db:Session=Depends(get_db)):
    teams=db.scalars(select(Team).where(Team.archived_at.is_(None))).all()
    visible=[t for t in teams if require_visible(db,current,t.id)]
    selected=next((t for t in visible if t.id==team_id),visible[0] if visible else None)
    items=db.scalars(select(MediaAsset).where(MediaAsset.team_id==selected.id).order_by(MediaAsset.filename)).all() if selected else []
    folders=[]
    try: folders=[x.name for x in settings.media_root.iterdir() if x.is_dir() and not x.is_symlink()]
    except OSError: pass
    return render(request,"media.html",current,teams=visible,selected=selected,items=items,folders=folders,storage_ok=settings.media_root.is_dir(),title="Medienbibliothek")

def require_visible(db,current,team_id):
    try: require(current,db,"view",team_id); return True
    except HTTPException: return False

@router.post("/media/{team_id}/scan")
def scan_media(team_id:str,request:Request,csrf_token_value:str=Form(alias="csrf_token"),current=Depends(current_user),db:Session=Depends(get_db)):
    check_csrf(request,csrf_token_value); require(current,db,"generate",team_id); team=db.get(Team,team_id)
    if not team: raise HTTPException(404)
    store=LocalStorageProvider(settings.media_root)
    try: folder=store.resolve(team.media_subdir)
    except StorageError as e: raise HTTPException(422,str(e)) from e
    if not folder.is_dir(): raise HTTPException(503,"SMB-/Medienordner ist nicht erreichbar")
    seen=set()
    for path in folder.rglob("*"):
        if not path.is_file() or path.is_symlink() or path.suffix.lower() not in {".jpg",".jpeg",".png",".webp"}: continue
        relative=str(path.relative_to(settings.media_root)); before=path.stat(); content=path.read_bytes(); after=path.stat()
        if before.st_mtime_ns!=after.st_mtime_ns or before.st_size!=after.st_size: raise HTTPException(409,f"Datei wurde während des Scans verändert: {relative}")
        seen.add(relative); stat=after
        asset=db.scalar(select(MediaAsset).where(MediaAsset.team_id==team.id,MediaAsset.relative_path==relative))
        values={"filename":path.name,"mime_type":mimetypes.guess_type(path.name)[0] or "application/octet-stream","size":stat.st_size,"checksum":hashlib.sha256(content).hexdigest(),"mtime":datetime.fromtimestamp(stat.st_mtime,timezone.utc),"available":True}
        if asset:
            for key,value in values.items(): setattr(asset,key,value)
        else: db.add(MediaAsset(team_id=team.id,relative_path=relative,active=True,**values))
    for asset in db.scalars(select(MediaAsset).where(MediaAsset.team_id==team.id)):
        if asset.relative_path not in seen: asset.available=False
    team.last_sync_at=datetime.now(timezone.utc); audit(db,current,"media.scanned","team",team.id,team.id,{"files":len(seen)}); db.commit(); return redirect(f"/media?team_id={team.id}",f"{len(seen)} Dateien eingelesen")

@router.post("/media/{asset_id}/toggle")
def toggle_media(asset_id:str,request:Request,csrf_token_value:str=Form(alias="csrf_token"),current=Depends(current_user),db:Session=Depends(get_db)):
    check_csrf(request,csrf_token_value); asset=db.get(MediaAsset,asset_id)
    if not asset: raise HTTPException(404)
    require(current,db,"generate",asset.team_id); asset.active=not asset.active
    if asset.active and not asset.available: raise HTTPException(422,"Eine fehlende Datei kann nicht aktiviert werden")
    audit(db,current,"media.toggled","media",asset.id,asset.team_id,{"active":asset.active}); db.commit(); return redirect(f"/media?team_id={asset.team_id}")

@router.get("/assets",response_class=HTMLResponse)
def assets(request:Request,current=Depends(current_user),db:Session=Depends(get_db)):
    require_admin(current); fonts=db.scalars(select(FontAsset).where(FontAsset.archived_at.is_(None))).all(); designs=db.scalars(select(DesignTemplate).where(DesignTemplate.archived_at.is_(None)).order_by(DesignTemplate.name,DesignTemplate.version.desc())).all(); return render(request,"assets.html",current,fonts=fonts,designs=designs,title="Schriftarten und Designvorlagen")

@router.post("/fonts")
async def upload_font(request:Request,csrf_token_value:str=Form(alias="csrf_token"),name:str=Form(),family:str=Form(),file:UploadFile=File(),current=Depends(current_user),db:Session=Depends(get_db)):
    check_csrf(request,csrf_token_value); require_admin(current); suffix=Path(file.filename or "").suffix.lower()
    if suffix not in {".woff2",".ttf"}: raise HTTPException(422,"Nur WOFF2 und TTF sind erlaubt")
    data=await file.read()
    if not data or len(data)>5*1024*1024: raise HTTPException(422,"Schriftdatei leer oder größer als 5 MB")
    target=Path("data/uploads/fonts")/f"{hashlib.sha256(data).hexdigest()}{suffix}"; target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(data)
    item=FontAsset(name=name,family=family,relative_path=str(target),mime_type=file.content_type or "font/ttf",size=len(data)); db.add(item); db.flush(); audit(db,current,"font.uploaded","font",item.id); db.commit(); return redirect("/assets")

@router.post("/designs")
def create_design(request:Request,csrf_token_value:str=Form(alias="csrf_token"),name:str=Form(),post_type:str=Form(),media_kind:str=Form(),html_template:str=Form(),css:str=Form(),current=Depends(current_user),db:Session=Depends(get_db)):
    check_csrf(request,csrf_token_value); require_admin(current)
    if media_kind not in {"feed","story"}: raise HTTPException(422,"Ungültiges Medienformat")
    previous=db.scalar(select(DesignTemplate).where(DesignTemplate.name==name).order_by(DesignTemplate.version.desc())); version=(previous.version+1) if previous else 1
    item=DesignTemplate(name=name,post_type=post_type,media_kind=media_kind,width=1080,height=1350 if media_kind=="feed" else 1920,html_template=html_template,css=css,version=version)
    db.add(item); db.flush(); audit(db,current,"design.created","design",item.id,details={"version":version}); db.commit(); return redirect("/assets")


@router.get("/prompts",response_class=HTMLResponse)
def prompts(request:Request,current=Depends(current_user),db:Session=Depends(get_db)):
    from app.prompts.service import (
        ALLOWED_PLACEHOLDERS,
        DEFAULT_IMAGE_PROMPT,
        DEFAULT_TEXT_PROMPT,
    )
    require_admin(current)
    items=db.scalars(select(PromptTemplate).where(PromptTemplate.archived_at.is_(None)).order_by(PromptTemplate.name,PromptTemplate.version.desc())).all()
    return render(request,"prompts.html",current,items=items,placeholders=sorted(ALLOWED_PLACEHOLDERS),default_image=DEFAULT_IMAGE_PROMPT,default_text=DEFAULT_TEXT_PROMPT,preview=None,title="KI-Promptvorlagen")


@router.post("/prompts/preview",response_class=HTMLResponse)
def preview_prompt(request:Request,csrf_token_value:str=Form(alias="csrf_token"),prompt_body:str=Form(),prompt_kind:str=Form(),post_type:str=Form(),media_kind:str=Form(default="none"),style_direction:str=Form(default=""),current=Depends(current_user),db:Session=Depends(get_db)):
    from app.prompts.service import (
        ALLOWED_PLACEHOLDERS,
        DEFAULT_IMAGE_PROMPT,
        DEFAULT_TEXT_PROMPT,
        IMAGE_SAFETY_PREFIX,
        TEXT_SAFETY_PREFIX,
        PromptValidationError,
        prompt_context,
        render_body,
        sample_facts,
    )
    check_csrf(request,csrf_token_value); require_admin(current)
    if prompt_kind not in {"image","text"} or post_type not in {"announcement","reminder","result"} or media_kind not in {"none","feed","story"}: raise HTTPException(422,"Ungültiger Prompt-Typ")
    if prompt_kind=="image" and media_kind not in {"feed","story"}: raise HTTPException(422,"Bildprompt benötigt Feed oder Story")
    if prompt_kind=="text": media_kind="none"
    try:
        preview=render_body(prompt_body,prompt_context(sample_facts(),media_kind,style_direction or None))
    except PromptValidationError as exc: raise HTTPException(422,str(exc)) from exc
    if prompt_kind=="image": preview=IMAGE_SAFETY_PREFIX+"\n"+preview
    else: preview=TEXT_SAFETY_PREFIX+"\n"+preview
    items=db.scalars(select(PromptTemplate).where(PromptTemplate.archived_at.is_(None)).order_by(PromptTemplate.name,PromptTemplate.version.desc())).all()
    return render(request,"prompts.html",current,items=items,placeholders=sorted(ALLOWED_PLACEHOLDERS),default_image=DEFAULT_IMAGE_PROMPT,default_text=DEFAULT_TEXT_PROMPT,preview=preview,form={"prompt_body":prompt_body,"prompt_kind":prompt_kind,"post_type":post_type,"media_kind":media_kind,"style_direction":style_direction},title="KI-Promptvorlagen")


@router.post("/prompts")
def create_prompt(request:Request,csrf_token_value:str=Form(alias="csrf_token"),name:str=Form(),prompt_kind:str=Form(),post_type:str=Form(),media_kind:str=Form(default="none"),prompt_body:str=Form(),style_direction:str=Form(default=""),model:str=Form(),quality:str=Form(default="medium"),current=Depends(current_user),db:Session=Depends(get_db)):
    from app.prompts.service import PromptValidationError, validate_template
    check_csrf(request,csrf_token_value); require_admin(current)
    name=name.strip()
    if not name or prompt_kind not in {"image","text"} or post_type not in {"announcement","reminder","result"}: raise HTTPException(422,"Prompt-Metadaten sind ungültig")
    if prompt_kind=="image" and media_kind not in {"feed","story"}: raise HTTPException(422,"Bildprompt benötigt Feed oder Story")
    if prompt_kind=="text": media_kind="none"
    if quality not in {"low","medium","high","auto","default"}: raise HTTPException(422,"Unbekannte Qualitätsstufe")
    if prompt_kind=="image" and (not model.startswith("gpt-image-") or quality=="default"): raise HTTPException(422,"Bildprompts benötigen ein GPT-Image-Modell und eine Bildqualitätsstufe")
    try: validate_template(prompt_body)
    except PromptValidationError as exc: raise HTTPException(422,str(exc)) from exc
    previous=db.scalar(select(PromptTemplate).where(PromptTemplate.name==name,PromptTemplate.prompt_kind==prompt_kind,PromptTemplate.post_type==post_type,PromptTemplate.media_kind==media_kind).order_by(PromptTemplate.version.desc()))
    version=(previous.version+1) if previous else 1
    item=PromptTemplate(name=name,prompt_kind=prompt_kind,post_type=post_type,media_kind=media_kind,prompt_body=prompt_body,style_direction=style_direction.strip() or None,model=model.strip(),quality=quality,version=version)
    db.add(item); db.flush(); audit(db,current,"prompt.created","prompt_template",item.id,details={"name":name,"version":version,"kind":prompt_kind,"media_kind":media_kind}); db.commit(); return redirect("/prompts",f"Prompt {name} Version {version} gespeichert")


@router.get("/rules",response_class=HTMLResponse)
def rules(request:Request,team_id:str|None=None,current=Depends(current_user),db:Session=Depends(get_db)):
    teams=[t for t in db.scalars(select(Team).where(Team.archived_at.is_(None))) if require_visible(db,current,t.id)]; selected=next((t for t in teams if t.id==team_id),teams[0] if teams else None); stories=db.scalars(select(StoryRule).where(StoryRule.team_id==selected.id).order_by(StoryRule.sort_order)).all() if selected else []; pages=db.scalars(select(InstagramPage).where(InstagramPage.archived_at.is_(None))).all()
    prompt_items=db.scalars(select(PromptTemplate).where(PromptTemplate.active.is_(True),PromptTemplate.archived_at.is_(None)).order_by(PromptTemplate.version.desc())).all(); latest={}
    for prompt in prompt_items: latest.setdefault((prompt.name,prompt.prompt_kind,prompt.post_type,prompt.media_kind),prompt)
    return render(request,"rules.html",current,teams=teams,selected=selected,stories=stories,pages=pages,prompts=list(latest.values()),title="Veröffentlichungsregeln")

@router.post("/rules/{team_id}/defaults")
def save_rules(team_id:str,request:Request,csrf_token_value:str=Form(alias="csrf_token"),announcement_enabled:bool=Form(default=False),feed_before_minutes:int=Form(),late_approval:str=Form(),result_enabled:bool=Form(default=False),result_wait_minutes:int=Form(),image_prompt_feed:str=Form(default="default-image-feed"),image_prompt_story:str=Form(default="default-image-story"),text_prompt:str=Form(default="default-text-announcement"),result_image_prompt_feed:str=Form(default="default-image-feed"),result_image_prompt_story:str=Form(default="default-image-story"),result_text_prompt:str=Form(default="default-text-result"),style_direction:str=Form(default=""),current=Depends(current_user),db:Session=Depends(get_db)):
    check_csrf(request,csrf_token_value); require_admin(current); team=db.get(Team,team_id)
    if not team: raise HTTPException(404)
    if late_approval not in {"publish_now","manual","skip","next_story"}: raise HTTPException(422)
    team.rules={**team.rules,"announcement_enabled":announcement_enabled,"feed_before_minutes":feed_before_minutes,"late_approval":late_approval,"result_enabled":result_enabled,"result_wait_minutes":result_wait_minutes,"image_prompt_feed":image_prompt_feed,"image_prompt_story":image_prompt_story,"text_prompt":text_prompt,"image_prompt_feed_result":result_image_prompt_feed,"image_prompt_story_result":result_image_prompt_story,"text_prompt_result":result_text_prompt,"style_direction":style_direction.strip()}; team.version+=1; audit(db,current,"rules.updated","team",team.id,team.id,team.rules); db.commit(); return redirect(f"/rules?team_id={team.id}")

@router.post("/rules/{team_id}/stories")
def create_story_rule(team_id:str,request:Request,csrf_token_value:str=Form(alias="csrf_token"),name:str=Form(),post_type:str=Form(),reference:str=Form(),direction:str=Form(),offset_minutes:int=Form(),fixed_time:str=Form(default=""),next_day:bool=Form(default=False),template:str=Form(),prompt_template:str=Form(default="default-image-story"),instagram_page_id:str=Form(default=""),reuse_media:bool=Form(default=False),sort_order:int=Form(default=0),current=Depends(current_user),db:Session=Depends(get_db)):
    check_csrf(request,csrf_token_value); require_admin(current)
    if reference not in {"kickoff","planned_end","result_detected","approval","next_day"} or direction not in {"before","after"}: raise HTTPException(422,"Ungültiger Bezugspunkt")
    item=StoryRule(team_id=team_id,name=name,post_type=post_type,reference=reference,direction=direction,offset_minutes=offset_minutes,fixed_time=fixed_time or None,next_day=next_day,template=template,prompt_template=prompt_template,instagram_page_id=instagram_page_id or None,reuse_media=reuse_media,sort_order=sort_order); db.add(item); db.flush(); audit(db,current,"story_rule.created","story_rule",item.id,team_id); db.commit(); return redirect(f"/rules?team_id={team_id}")

@router.get("/posts",response_class=HTMLResponse)
def posts(request:Request,current=Depends(current_user),db:Session=Depends(get_db)):
    items=[p for p in db.scalars(select(Post).order_by(Post.updated_at.desc())) if require_visible(db,current,p.team_id)]; teams={x.id:x for x in db.scalars(select(Team))}; return render(request,"posts.html",current,items=items,teams=teams,title="Beiträge und Freigaben")

@router.get("/posts/{post_id}",response_class=HTMLResponse)
def post_detail(post_id:str,request:Request,current=Depends(current_user),db:Session=Depends(get_db)):
    item=db.get(Post,post_id)
    if not item: raise HTTPException(404)
    require(current,db,"view",item.team_id); jobs=db.scalars(select(PublicationJob).where(PublicationJob.post_id==item.id).order_by(PublicationJob.scheduled_at)).all(); pages=db.scalars(select(InstagramPage).where(InstagramPage.archived_at.is_(None))).all()
    from app.rendering.service import Renderer
    checks={}; renderer=Renderer(settings.generated_root,settings.media_root,Path("data/uploads"))
    for job in jobs:
        try:
            report=renderer.validate(Path(job.media_path),job.kind); checks[job.id]=f"PNG geprüft – {report['width']} × {report['height']}"
        except ValueError as exc: checks[job.id]=f"Prüfung fehlgeschlagen – {exc}"
    return render(request,"post_detail.html",current,item=item,jobs=jobs,pages=pages,checks=checks,now=datetime.now(timezone.utc),title="Beitrag prüfen")

@router.post("/posts/{post_id}/text")
def post_text(post_id:str,request:Request,csrf_token_value:str=Form(alias="csrf_token"),text_value:str=Form(alias="text"),version:int=Form(),current=Depends(current_user),db:Session=Depends(get_db)):
    check_csrf(request,csrf_token_value); item=db.get(Post,post_id)
    if not item: raise HTTPException(404)
    require(current,db,"edit_post",item.team_id)
    try: edit_text(db,item,current,text_value,version)
    except ApprovalError as e: raise HTTPException(409,str(e)) from e
    audit(db,current,"post.text_edited","post",item.id,item.team_id); db.commit(); return redirect(f"/posts/{item.id}")

@router.post("/posts/{post_id}/rerender")
def rerender_post_media(post_id:str,request:Request,csrf_token_value:str=Form(alias="csrf_token"),version:int=Form(),story_job_ids:list[str]=Form(default=[]),current=Depends(current_user),db:Session=Depends(get_db)):
    from app.generation import build_renderer
    from app.posts.service import RerenderConflict, rerender_post
    check_csrf(request,csrf_token_value); item=db.get(Post,post_id)
    if not item: raise HTTPException(404)
    require(current,db,"generate",item.team_id)
    if item.version!=version: raise HTTPException(409,"Beitrag wurde zwischenzeitlich geändert")
    allowed_story_ids=set(db.scalars(select(PublicationJob.id).where(PublicationJob.post_id==item.id,PublicationJob.kind=="story")))
    if not set(story_job_ids).issubset(allowed_story_ids): raise HTTPException(422,"Ungültige Story-Auswahl")
    try: rerender_post(db,item,build_renderer(settings),story_job_ids)
    except RerenderConflict as exc:
        db.rollback(); raise HTTPException(409,str(exc)) from exc
    except ValueError as exc:
        db.rollback(); raise HTTPException(422,str(exc)) from exc
    audit(db,current,"post.graphics_rerendered","post",item.id,item.team_id,{"post_version":item.version,"story_jobs":story_job_ids}); db.commit(); return redirect(f"/posts/{item.id}","Grafiken versioniert neu erzeugt")

@router.post("/posts/{post_id}/approve")
def approve_post(post_id:str,request:Request,csrf_token_value:str=Form(alias="csrf_token"),job_ids:list[str]=Form(default=[]),current=Depends(current_user),db:Session=Depends(get_db)):
    check_csrf(request,csrf_token_value); item=db.get(Post,post_id)
    if not item: raise HTTPException(404)
    try: approve(db,item,current,job_ids or None)
    except ApprovalError as e: raise HTTPException(422,str(e)) from e
    return redirect(f"/posts/{item.id}","Beitrag ausdrücklich freigegeben")

@router.post("/posts/{post_id}/reject")
def reject_post(post_id:str,request:Request,csrf_token_value:str=Form(alias="csrf_token"),reason:str=Form(),current=Depends(current_user),db:Session=Depends(get_db)):
    from app.models import JobStatus, PostStatus
    check_csrf(request,csrf_token_value); item=db.get(Post,post_id)
    if not item: raise HTTPException(404)
    require(current,db,"approve",item.team_id)
    if not reason.strip(): raise HTTPException(422,"Eine Begründung ist erforderlich")
    item.status=PostStatus.REJECTED
    for job in db.scalars(select(PublicationJob).where(PublicationJob.post_id==item.id,PublicationJob.status!=JobStatus.PUBLISHED)): job.status=JobStatus.UNAPPROVED; job.approval_status="rejected"
    audit(db,current,"post.rejected","post",item.id,item.team_id,{"reason":reason}); db.commit(); return redirect(f"/posts/{item.id}","Beitrag abgelehnt")

@router.get("/publications",response_class=HTMLResponse)
def publications(request:Request,current=Depends(current_user),db:Session=Depends(get_db)):
    items=[j for j in db.scalars(select(PublicationJob).order_by(PublicationJob.scheduled_at.desc())) if require_visible(db,current,j.team_id)]; return render(request,"publications.html",current,items=items,title="Veröffentlichungsaufträge")

@router.post("/publications/{job_id}/cancel")
def cancel_job(job_id:str,request:Request,csrf_token_value:str=Form(alias="csrf_token"),current=Depends(current_user),db:Session=Depends(get_db)):
    from app.models import JobStatus
    check_csrf(request,csrf_token_value); item=db.get(PublicationJob,job_id)
    if not item: raise HTTPException(404)
    require(current,db,"approve",item.team_id)
    if item.status==JobStatus.PUBLISHED: raise HTTPException(409,"Veröffentlichte Aufträge können nicht abgebrochen werden")
    item.status=JobStatus.CANCELLED; audit(db,current,"publication.cancelled","publication_job",item.id,item.team_id); db.commit(); return redirect("/publications")

@router.get("/games",response_class=HTMLResponse)
def games(request:Request,current=Depends(current_user),db:Session=Depends(get_db)):
    teams=[t for t in db.scalars(select(Team).where(Team.archived_at.is_(None))) if require_visible(db,current,t.id)]; items=[g for g in db.scalars(select(Game).order_by(Game.kickoff.desc())) if require_visible(db,current,g.team_id)]; return render(request,"games.html",current,teams=teams,items=items,title="Spiele und Testdaten")

@router.post("/games/mock")
def create_mock_game(request:Request,csrf_token_value:str=Form(alias="csrf_token"),team_id:str=Form(),home_team:str=Form(),away_team:str=Form(),kickoff:str=Form(),competition:str=Form(default=""),venue:str=Form(default=""),pitch:str=Form(default=""),current=Depends(current_user),db:Session=Depends(get_db)):
    check_csrf(request,csrf_token_value); require(current,db,"edit_game",team_id)
    from zoneinfo import ZoneInfo
    try: kickoff_at=datetime.fromisoformat(kickoff).replace(tzinfo=ZoneInfo("Europe/Berlin")).astimezone(timezone.utc)
    except ValueError as e: raise HTTPException(422,"Ungültiger Spieltermin") from e
    item=Game(team_id=team_id,provider="mock",external_id=f"mock-{hashlib.sha256(f'{team_id}:{kickoff}:{home_team}:{away_team}'.encode()).hexdigest()[:20]}",home_team=home_team,away_team=away_team,kickoff=kickoff_at,competition=competition or None,venue=venue or None,pitch=pitch or None,source_url="fixture://dashboard",checked_at=datetime.now(timezone.utc)); db.add(item)
    try: db.flush()
    except IntegrityError as e: db.rollback(); raise HTTPException(409,"Dieses Testspiel existiert bereits") from e
    audit(db,current,"game.mock_created","game",item.id,team_id); db.commit(); return redirect("/games","Lokales Testspiel angelegt")


@router.post("/games/{game_id}/details")
def update_game_details(game_id:str,request:Request,csrf_token_value:str=Form(alias="csrf_token"),competition:str=Form(),venue:str=Form(),pitch:str=Form(),current=Depends(current_user),db:Session=Depends(get_db)):
    from app.models import JobStatus, PostStatus
    check_csrf(request,csrf_token_value); game=db.get(Game,game_id)
    if not game: raise HTTPException(404)
    require(current,db,"edit_game",game.team_id)
    if pitch not in {"","Rasenplatz","Kunstrasenplatz"}: raise HTTPException(422,"Platzart muss Rasenplatz oder Kunstrasenplatz sein")
    before={"competition":game.competition,"venue":game.venue,"pitch":game.pitch}
    after={"competition":competition.strip() or None,"venue":venue.strip() or None,"pitch":pitch or None}
    if before==after: return redirect("/games","Keine Spieldaten geändert")
    game.competition=after["competition"]; game.venue=after["venue"]; game.pitch=after["pitch"]; game.overrides={**(game.overrides or {}),"manual_venue_details":True}; game.version+=1
    posts=db.scalars(select(Post).where(Post.game_id==game.id,Post.status.not_in([PostStatus.PUBLISHED,PostStatus.CANCELLED]))).all()
    for post in posts:
        post.status=PostStatus.REAPPROVAL; post.version+=1; post.approved_version=None
        for job in db.scalars(select(PublicationJob).where(PublicationJob.post_id==post.id,PublicationJob.status!=JobStatus.PUBLISHED)):
            job.status=JobStatus.UNAPPROVED; job.approval_status="reapproval_required"; job.approved_post_version=None; job.error="Manuelle Spieldetails wurden geändert; Grafiken prüfen und neu erzeugen"
    audit(db,current,"game.details_updated","game",game.id,game.team_id,{"before":before,"after":after,"affected_posts":[post.id for post in posts]}); db.commit(); return redirect("/games","Spielort, Platzart und Wettbewerb gespeichert")


@router.post("/games/{game_id}/generate")
def generate_game_post(game_id:str,request:Request,csrf_token_value:str=Form(alias="csrf_token"),post_type:str=Form(),current=Depends(current_user),db:Session=Depends(get_db)):
    from app.generation import build_renderer, build_text_generator
    from app.posts.service import create_post
    check_csrf(request,csrf_token_value); game=db.get(Game,game_id)
    if not game: raise HTTPException(404)
    require(current,db,"generate",game.team_id); team=db.get(Team,game.team_id)
    try: post=create_post(db,game,team,build_text_generator(settings),build_renderer(settings),post_type)
    except ValueError as e: raise HTTPException(422,str(e)) from e
    audit(db,current,"post.generated_manually","post",post.id,game.team_id,{"text_generator":settings.text_generator_mode,"image_generator":settings.image_generator_mode}); db.commit(); return redirect(f"/posts/{post.id}","Beitrag erzeugt")

@router.get("/diagnostics",response_class=HTMLResponse)
def diagnostics(request:Request,current=Depends(current_user),db:Session=Depends(get_db)):
    from app.models import ProviderSnapshot
    require_admin(current); teams=db.scalars(select(Team).where(Team.archived_at.is_(None))).all(); snapshots=db.scalars(select(ProviderSnapshot).order_by(ProviderSnapshot.fetched_at.desc()).limit(100)).all(); return render(request,"diagnostics.html",current,teams=teams,snapshots=snapshots,title="Provider-Diagnose")

@router.post("/diagnostics/fussball/{team_id}")
def run_diagnostic(team_id:str,request:Request,csrf_token_value:str=Form(alias="csrf_token"),confirmation:str=Form(),current=Depends(current_user),db:Session=Depends(get_db)):
    from app.games.live_test import LiveTestDisabled, capture
    check_csrf(request,csrf_token_value); require_admin(current)
    if confirmation!="NUR LESEN": raise HTTPException(422,"Bestätigung 'NUR LESEN' erforderlich")
    team=db.get(Team,team_id)
    if not team: raise HTTPException(404)
    try: snapshot=capture(db,team,settings)
    except LiveTestDisabled as exc: raise HTTPException(409,str(exc)) from exc
    audit(db,current,"provider.snapshot_captured","provider_snapshot",snapshot.id,team.id,{"checksum":snapshot.checksum}); db.commit(); return redirect("/diagnostics","Diagnose-Snapshot gespeichert")

@router.get("/diagnostics/{snapshot_id}/html")
def download_snapshot(snapshot_id:str,current=Depends(current_user),db:Session=Depends(get_db)):
    from fastapi.responses import FileResponse

    from app.models import ProviderSnapshot
    require_admin(current); snapshot=db.get(ProviderSnapshot,snapshot_id)
    if not snapshot: raise HTTPException(404)
    root=settings.provider_snapshot_root.resolve(); path=(root/snapshot.relative_path).resolve()
    if root not in path.parents or not path.is_file(): raise HTTPException(404,"Snapshot-Datei fehlt oder ist unvollständig")
    return FileResponse(path,media_type="text/html",filename=f"fussball-{snapshot.checksum[:12]}.html")

@router.post("/diagnostics/{snapshot_id}/fixture")
def snapshot_to_fixture(snapshot_id:str,request:Request,csrf_token_value:str=Form(alias="csrf_token"),name:str=Form(),current=Depends(current_user),db:Session=Depends(get_db)):
    from app.models import ProviderSnapshot
    check_csrf(request,csrf_token_value); require_admin(current)
    if not name.replace("-","").replace("_","").isalnum(): raise HTTPException(422,"Ungültiger Fixture-Name")
    snapshot=db.get(ProviderSnapshot,snapshot_id); root=settings.provider_snapshot_root.resolve()
    if not snapshot: raise HTTPException(404)
    source=(root/snapshot.relative_path).resolve()
    if root not in source.parents or not source.is_file(): raise HTTPException(404,"Snapshot-Datei fehlt")
    target=Path("data/uploads/provider-fixtures")/f"{name}.html"; target.parent.mkdir(parents=True,exist_ok=True)
    if target.exists(): raise HTTPException(409,"Fixture existiert bereits")
    target.write_bytes(source.read_bytes()); audit(db,current,"provider.fixture_created","provider_snapshot",snapshot.id,snapshot.team_id,{"fixture":str(target)}); db.commit(); return redirect("/diagnostics","Fixture durch Administrator übernommen")

@router.get("/posts/{post_id}/media/{job_id}")
def post_media(post_id:str,job_id:str,current=Depends(current_user),db:Session=Depends(get_db)):
    from fastapi.responses import FileResponse
    item=db.get(Post,post_id); job=db.get(PublicationJob,job_id)
    if not item or not job or job.post_id!=item.id: raise HTTPException(404)
    require(current,db,"view",item.team_id); path=Path(job.media_path).resolve(); root=settings.generated_root.resolve()
    if root not in path.parents or not path.is_file(): raise HTTPException(404,"Grafik fehlt")
    return FileResponse(path,media_type="image/png")

@router.get("/diagnostics/{snapshot_id}/import",response_class=HTMLResponse)
def snapshot_import_preview(snapshot_id:str,request:Request,current=Depends(current_user),db:Session=Depends(get_db)):
    from app.games.importer import preview_snapshot
    from app.models import ProviderSnapshot
    require_admin(current); snapshot=db.get(ProviderSnapshot,snapshot_id)
    if not snapshot: raise HTTPException(404)
    team=db.get(Team,snapshot.team_id)
    return render(request,"diagnostic_import.html",current,snapshot=snapshot,team=team,games=preview_snapshot(snapshot),title="Spielübernahme prüfen")

@router.post("/diagnostics/{snapshot_id}/import")
def snapshot_import_confirm(snapshot_id:str,request:Request,csrf_token_value:str=Form(alias="csrf_token"),confirmation:str=Form(),current=Depends(current_user),db:Session=Depends(get_db)):
    from app.games.importer import SnapshotImportError, import_snapshot
    from app.models import ProviderSnapshot
    check_csrf(request,csrf_token_value); require_admin(current)
    if confirmation!="SPIELE ÜBERNEHMEN": raise HTTPException(422,"Explizite Bestätigung 'SPIELE ÜBERNEHMEN' erforderlich")
    snapshot=db.get(ProviderSnapshot,snapshot_id)
    if not snapshot: raise HTTPException(404)
    try: result=import_snapshot(db,snapshot,current)
    except SnapshotImportError as exc: raise HTTPException(422,str(exc)) from exc
    return redirect("/diagnostics",f"Übernahme abgeschlossen: {result['created']} neu, {result['updated']} aktualisiert, {result['unchanged']} unverändert")

@router.post("/games/{game_id}/confirm-provisional")
def confirm_provisional_game(game_id:str,request:Request,csrf_token_value:str=Form(alias="csrf_token"),confirmation:str=Form(),current=Depends(current_user),db:Session=Depends(get_db)):
    check_csrf(request,csrf_token_value); require_admin(current); game=db.get(Game,game_id)
    if not game: raise HTTPException(404)
    if game.status!="provisional": raise HTTPException(409,"Spiel ist nicht als vorläufig markiert")
    if confirmation!="VORLÄUFIGES SPIEL BESTÄTIGEN": raise HTTPException(422,"Explizite Bestätigung erforderlich")
    game.status="scheduled"; game.overrides={**game.overrides,"automation_blocked":False,"provisional_confirmed_by":current.id,"provisional_confirmed_at":datetime.now(timezone.utc).isoformat()}; game.version+=1
    audit(db,current,"game.provisional_confirmed","game",game.id,game.team_id,{"external_id":game.external_id}); db.commit(); return redirect("/games","Vorläufiges Spiel manuell bestätigt")
