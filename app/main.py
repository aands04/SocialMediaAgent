import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app.admin_routes import router as admin_router
from app.auth.service import authenticate
from app.config import get_settings
from app.db import get_db
from app.models import Game, Post, PublicationJob, Team, User
from app.monitoring.service import system_status
from app.web import berlin_datetime, csrf_token, current_user

settings=get_settings()
@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.media_root.mkdir(parents=True,exist_ok=True)
    settings.generated_root.mkdir(parents=True,exist_ok=True)
    yield
app=FastAPI(title="Vereins Social Media Agent",docs_url="/api/docs" if settings.environment!="production" else None,lifespan=lifespan)
app.add_middleware(SessionMiddleware,secret_key=settings.session_secret,max_age=settings.session_max_age,https_only=settings.environment=="production",same_site="lax")
app.mount("/static",StaticFiles(directory="app/static"),name="static")
templates=Jinja2Templates(directory="app/templates")
templates.env.filters["berlin"] = berlin_datetime
def csrf(request: Request):
    return csrf_token(request)
@app.get("/health")
def health(db:Session=Depends(get_db)):
    report=system_status(db,settings)
    database="ok" if report["checks"]["postgresql"]["ok"] else "error"
    return {"status":"ok" if report["ok"] else "degraded","checks":{"web":"ok","database":database,"worker":"ok" if report["checks"]["worker"]["ok"] else "error"}}
@app.get("/system",response_class=HTMLResponse)
def system_dashboard(request:Request,current:User=Depends(current_user),db:Session=Depends(get_db)):
    return templates.TemplateResponse(request,"system.html",{"user":current,"report":system_status(db,settings),"csrf":csrf(request),"title":"Systemstatus"})
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
def dashboard(request:Request,current:User=Depends(current_user),db:Session=Depends(get_db)):
    counts={"teams":db.scalar(select(func.count()).select_from(Team)),"games":db.scalar(select(func.count()).select_from(Game)),"posts":db.scalar(select(func.count()).select_from(Post)),"jobs":db.scalar(select(func.count()).select_from(PublicationJob))}
    posts=db.scalars(select(Post).order_by(Post.updated_at.desc()).limit(10)).all()
    return templates.TemplateResponse(request,"dashboard.html",{"user":current,"counts":counts,"posts":posts,"csrf":csrf(request)})

app.include_router(admin_router)
