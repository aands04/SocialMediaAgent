from fastapi.testclient import TestClient

from app.main import app


def test_health():
 with TestClient(app) as c:
  r=c.get('/health'); assert r.status_code==200 and r.json()['checks']['database']=='ok'
def test_dashboard_requires_login():
 with TestClient(app,raise_server_exceptions=False) as c:
  response=c.get('/',follow_redirects=False)
  assert response.status_code==303
  assert response.headers['location']=='/login'
