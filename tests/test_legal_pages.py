from fastapi.testclient import TestClient

from app.main import app


def test_privacy_policy_is_public_and_contains_meta_information():
    with TestClient(app) as client:
        response = client.get("/datenschutz")

    assert response.status_code == 200
    assert "Datenschutzerklärung" in response.text
    assert "Instagram, Facebook und WhatsApp" in response.text
    assert "socialmedia@svehlen.de" in response.text
    assert 'href="/datenloeschung"' in response.text
    assert "access_token" not in response.text
    assert response.headers["x-content-type-options"] == "nosniff"


def test_data_deletion_instructions_are_public_and_actionable():
    with TestClient(app) as client:
        response = client.get("/datenloeschung")

    assert response.status_code == 200
    assert "Datenlöschung beantragen" in response.text
    assert "Datenlöschung Vereinszentrale" in response.text
    assert "STOPP" in response.text
    assert 'href="/datenschutz"' in response.text
    assert "Passwort oder einen Zugriffstoken" in response.text


def test_legal_pages_do_not_create_an_authenticated_session():
    with TestClient(app) as client:
        privacy = client.get("/datenschutz")
        deletion = client.get("/datenloeschung")

    assert "Eingeloggt als" not in privacy.text
    assert "Eingeloggt als" not in deletion.text
    assert "session=" not in privacy.headers.get("set-cookie", "")
    assert "session=" not in deletion.headers.get("set-cookie", "")
