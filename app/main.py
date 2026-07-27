import secrets

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app.auth.service import authenticate
from app.config import get_settings
from app.db import get_db
from app.models import Game, InstagramPage, Post, PublicationJob, Team, User

settings=get_settings(); app=FastAPI(title="Vereins Social Media Agent",docs_url="/api/docs" if settings.environment!="production" else None)
app.add_middleware(SessionMiddleware,secret_key=settings.session_secret,max_age=settings.session_max_age,https_only=settings.environment=="production",same_site="lax")
app.mount("/static",StaticFiles(directory="app/static"),name="static")
templates=Jinja2Templates(directory="app/templates")
@app.on_event("startup")
def startup():
    settings.media_root.mkdir(parents=True,exist_ok=True); settings.generated_root.mkdir(parents=True,exist_ok=True)
def csrf(request:Request):
    token=request.session.setdefault("csrf",secrets.token_urlsafe(32)); return token
def user(request:Request,db:Session=Depends(get_db)):
    uid=request.session.get("uid"); current=db.get(User,uid) if uid else None
    if not current or not current.active: raise HTTPException(status.HTTP_401_UNAUTHORIZED)
    return current
@app.get("/health")
def health(db:Session=Depends(get_db)):
    checks={"web":"ok","database":"error","smb":"ok" if settings.media_root.is_dir() else "error","openai":"configured" if settings.openai_api_key else "mock","meta":"dry-run" if settings.publisher_mode!="live" else "configured" if settings.meta_access_token else "error","fussball_provider":"configured"}
    try: db.execute(text("select 1")); checks["database"]="ok"
    except Exception: pass
    return {"status":"ok" if checks["database"]=="ok" else "degraded","checks":checks}
@app.get("/login",response_class=HTMLResponse)
def login_form(request:Request): return templates.TemplateResponse(request,"login.html",{"csrf":csrf(request)})
@app.post("/login")
def login(request:Request,email:str=Form(),password:str=Form(),csrf_token:str=Form(),db:Session=Depends(get_db)):
    if not secrets.compare_digest(csrf_token,request.session.get("csrf","")): raise HTTPException(403,"CSRF-Prüfung fehlgeschlagen")
    current=authenticate(db,email,password)
    if not current: return templates.TemplateResponse(request,"login.html",{"csrf":csrf(request),"error":"Anmeldung fehlgeschlagen"},status_code=401)
    request.session.clear(); request.session["uid"]=current.id; csrf(request); return RedirectResponse("/",303)
@app.post("/logout")
def logout(request:Request): request.session.clear(); return RedirectResponse("/login",303)
@app.get("/",response_class=HTMLResponse)
def dashboard(request:Request,current:User=Depends(user),db:Session=Depends(get_db)):
    counts={"teams":db.scalar(select(func.count()).select_from(Team)),"games":db.scalar(select(func.count()).select_from(Game)),"posts":db.scalar(select(func.count()).select_from(Post)),"jobs":db.scalar(select(func.count()).select_from(PublicationJob))}
    posts=db.scalars(select(Post).order_by(Post.updated_at.desc()).limit(10)).all()
    return templates.TemplateResponse(request,"dashboard.html",{"user":current,"counts":counts,"posts":posts,"csrf":csrf(request)})
@app.get("/teams",response_class=HTMLResponse)
def teams(request:Request,current:User=Depends(user),db:Session=Depends(get_db)): return templates.TemplateResponse(request,"list.html",{"user":current,"title":"Mannschaften","headers":["Name","Slug","Aktiv"],"rows":[(x.display_name,x.slug,"Ja" if x.active else "Nein") for x in db.scalars(select(Team).where(Team.archived_at.is_(None)))],"csrf":csrf(request)})
@app.get("/instagram",response_class=HTMLResponse)
def pages(request:Request,current:User=Depends(user),db:Session=Depends(get_db)): return templates.TemplateResponse(request,"list.html",{"user":current,"title":"Instagram-Seiten","headers":["Name","Benutzername","Verbindung"],"rows":[(x.display_name,"@"+x.username,x.connection_status) for x in db.scalars(select(InstagramPage).where(InstagramPage.archived_at.is_(None)))],"csrf":csrf(request)})
