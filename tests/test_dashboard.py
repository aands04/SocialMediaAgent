import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.service import hash_password
from app.db import Base, get_db
from app.main import app
from app.models import InstagramPage, Role, Team, User


@pytest.fixture
def browser(tmp_path):
    engine=create_engine(f"sqlite:///{tmp_path/'dashboard.db'}",connect_args={"check_same_thread":False})
    Base.metadata.create_all(engine); factory=sessionmaker(engine,expire_on_commit=False)
    with factory() as db:
        db.add(User(email="admin@test.invalid",password_hash=hash_password("Very-Secure-Test-Password"),role=Role.ADMIN,all_teams=True)); db.commit()
    def override():
        with factory() as db: yield db
    app.dependency_overrides[get_db]=override
    with TestClient(app) as client:
        page=client.get("/login"); token=re.search(r'name="csrf_token" value="([^"]+)',page.text).group(1)
        response=client.post("/login",data={"email":"admin@test.invalid","password":"Very-Secure-Test-Password","csrf_token":token},follow_redirects=False)
        assert response.status_code==303
        yield client,factory
    app.dependency_overrides.clear()

def csrf(client):
    response=client.get("/"); return re.search(r'name="csrf_token" value="([^"]+)',response.text).group(1) if 'name="csrf_token"' in response.text else client.cookies

def session_csrf(client):
    response=client.get("/teams")
    return re.search(r'name="csrf_token" value="([^"]+)',response.text).group(1)

def test_dashboard_admin_flow(browser):
    client,factory=browser; token=session_csrf(client)
    result=client.post("/instagram",data={"csrf_token":token,"internal_name":"main","display_name":"Hauptseite","username":"club","club":"SV","account_id":"mock-42"},follow_redirects=False)
    assert result.status_code==303
    with factory() as db: page=db.query(InstagramPage).one(); version=page.version
    assert client.post(f"/instagram/{page.id}/state",data={"csrf_token":token,"action":"mock-connect","version":version},follow_redirects=False).status_code==303
    result=client.post("/teams",data={"csrf_token":token,"internal_name":"erste","display_name":"Erste Mannschaft","short_name":"I","slug":"erste","club":"SV","fussball_url":"https://www.fussball.de/team","instagram_page_id":page.id,"media_subdir":"erste"},follow_redirects=False)
    assert result.status_code==303
    with factory() as db: team=db.query(Team).one()
    result=client.post(f"/rules/{team.id}/stories",data={"csrf_token":token,"name":"24 Stunden","post_type":"announcement","reference":"kickoff","direction":"before","offset_minutes":"1440","fixed_time":"","template":"default-story","sort_order":"1"},follow_redirects=False)
    assert result.status_code==303
    assert "24 Stunden" in client.get(f"/rules?team_id={team.id}").text
    result=client.post("/games/mock",data={"csrf_token":token,"team_id":team.id,"home_team":"SV Test","away_team":"FC Fixture","kickoff":"2026-08-10T18:00","venue":"Testplatz"},follow_redirects=False)
    assert result.status_code==303
    with factory() as db: game=db.query(__import__("app.models",fromlist=["Game"]).Game).one()
    result=client.post(f"/games/{game.id}/generate",data={"csrf_token":token,"post_type":"announcement"},follow_redirects=False)
    assert result.status_code==303 and result.headers["location"].startswith("/posts/")
    result=client.post("/users",data={"csrf_token":token,"email":"editor@test.invalid","password":"Another-Secure-Test","role":"editor"},follow_redirects=False)
    assert result.status_code==303
    assert "editor@test.invalid" in client.get("/users").text

def test_csrf_and_non_admin_are_rejected(browser):
    client,factory=browser
    assert client.post("/instagram",data={"csrf_token":"wrong","internal_name":"x","display_name":"x","username":"x","club":"x","account_id":""}).status_code==403
    with factory() as db:
        editor=User(email="limited@test.invalid",password_hash=hash_password("Very-Secure-Test-Password"),role=Role.EDITOR,all_teams=False); db.add(editor); db.commit()
    client.post("/logout")
    page=client.get("/login"); token=re.search(r'name="csrf_token" value="([^"]+)',page.text).group(1)
    client.post("/login",data={"email":"limited@test.invalid","password":"Very-Secure-Test-Password","csrf_token":token})
    assert client.get("/users").status_code==403
