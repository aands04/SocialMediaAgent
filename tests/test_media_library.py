import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.auth.service import hash_password
from app.db import Base, TenantSession, get_db
from app.main import app
from app.media.library import (
    SAFE_DEFAULT_POLICIES,
    MediaLibraryError,
    effective_policy,
    mark_asset_used,
    release_asset,
    reserve_media,
    set_game_preference,
    soft_delete_asset,
    usage_status,
)
from app.models import (
    Club,
    ClubStatus,
    Game,
    GameMediaPreference,
    InstagramPage,
    LedgerStatus,
    MediaAsset,
    MediaUsageHistory,
    PlanProfile,
    Role,
    StorageLedgerEntry,
    StorageObject,
    Team,
    User,
)
from app.tenancy.state import system_scope, tenant_scope


@pytest.fixture
def browser(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'media-library.db'}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    raw_factory = sessionmaker(engine, class_=TenantSession, expire_on_commit=False)
    with raw_factory() as db, system_scope("Medienbibliothek-Testmandant anlegen"):
        profile = PlanProfile(name="Medien-Test", description="Testprofil", version=1)
        db.add(profile)
        db.flush()
        club = Club(
            name="Testverein",
            short_name="Test",
            slug="medien-testverein",
            status=ClubStatus.ACTIVE,
            timezone="Europe/Berlin",
            plan_profile_id=profile.id,
        )
        db.add(club)
        db.flush()
        admin = User(
            email="media-admin@test.invalid",
            password_hash=hash_password("Very-Secure-Test-Password"),
            role=Role.ADMIN,
            all_teams=True,
            club_id=club.id,
        )
        db.add(admin)
        db.commit()

    @contextmanager
    def factory():
        with tenant_scope(club.id, admin.id), raw_factory() as db:
            yield db

    async def override():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override
    with TestClient(app) as client:
        page = client.get("/login")
        token = re.search(r'name="csrf_token" value="([^"]+)', page.text).group(1)
        response = client.post(
            "/login",
            data={
                "email": "media-admin@test.invalid",
                "password": "Very-Secure-Test-Password",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        yield client, factory
    app.dependency_overrides.clear()


def _graph(db):
    page = InstagramPage(
        internal_name="media-library",
        display_name="Medienbibliothek",
        username="media-library",
        club="Testverein",
        active=True,
    )
    db.add(page)
    db.flush()
    team = Team(
        internal_name="media-team",
        display_name="Testverein I",
        short_name="TV I",
        slug="media-team",
        club="Testverein",
        fussball_url="https://www.fussball.de/test",
        instagram_page_id=page.id,
        media_subdir="test/spieler",
    )
    db.add(team)
    db.flush()
    games = []
    for index in range(1, 5):
        game = Game(
            team_id=team.id,
            external_id=f"media-game-{index}",
            home_team="Testverein",
            away_team=f"Gegner {index}",
            kickoff=datetime.now(timezone.utc) + timedelta(days=index),
            source_url=team.fussball_url,
        )
        db.add(game)
        games.append(game)
    db.flush()
    return team, games


def _asset(db, team, name, category="match_photo", *, uses=0, automatic=True):
    item = MediaAsset(
        team_id=team.id,
        storage_kind="upload",
        relative_path=f"clubs/{team.club_id}/teams/{team.id}/players/{name}.jpg",
        filename=f"{name}.jpg",
        mime_type="image/jpeg",
        size=1024,
        width=1080,
        height=1350,
        checksum=(name * 64)[:64],
        mtime=datetime.now(timezone.utc),
        media_category=category,
        uses=uses,
        automatic_usage_enabled=automatic,
        active=automatic,
        available=True,
    )
    db.add(item)
    db.flush()
    return item


def test_safe_default_media_policies_are_contribution_specific(db):
    assert effective_policy(db, db.info["test_club_id"], "announcement") == ["match_photo"]
    assert effective_policy(db, db.info["test_club_id"], "reminder") == ["match_photo"]
    assert effective_policy(db, db.info["test_club_id"], "result") == ["match_photo"]
    assert effective_policy(db, db.info["test_club_id"], "live") == [
        "player_portrait",
        "match_photo",
    ]
    assert SAFE_DEFAULT_POLICIES["result"] == ["match_photo"]


def test_media_library_uses_decimal_gb_and_counts_legacy_assets(browser):
    client, factory = browser
    with factory() as db:
        team, _games = _graph(db)
        profile = db.query(PlanProfile).one()
        profile.max_storage_bytes = 1_000_000_000_000
        asset = _asset(db, team, "legacy-storage", "match_photo")
        asset.size = 90_000_000
        db.commit()
        team_id = team.id

    response = client.get(f"/media?team_id={team_id}")
    assert response.status_code == 200
    assert "0,09 GB von 1.000 GB" in response.text


def test_automatic_selection_uses_only_policy_categories_and_never_reuses(db):
    team, games = _graph(db)
    match = _asset(db, team, "match", "match_photo")
    portrait = _asset(db, team, "portrait", "player_portrait")

    selected = reserve_media(
        db,
        club_id=team.club_id,
        team_id=team.id,
        game_id=games[0].id,
        contribution_type="announcement",
    )
    assert selected.id == match.id
    mark_asset_used(
        db,
        match,
        game_id=games[0].id,
        post_id=None,
        contribution_type="announcement",
    )
    assert match.automatic_usage_enabled is False
    assert (
        reserve_media(
            db,
            club_id=team.club_id,
            team_id=team.id,
            game_id=games[1].id,
            contribution_type="announcement",
        )
        is None
    )

    live = reserve_media(
        db,
        club_id=team.club_id,
        team_id=team.id,
        game_id=games[2].id,
        contribution_type="live",
    )
    assert live.id == portrait.id


def test_automatic_selection_replaces_consumed_saved_preference(db):
    team, games = _graph(db)
    consumed = _asset(db, team, "consumed")
    replacement = _asset(db, team, "replacement")

    first = reserve_media(
        db,
        club_id=team.club_id,
        team_id=team.id,
        game_id=games[0].id,
        contribution_type="announcement",
    )
    assert first.id in {consumed.id, replacement.id}
    replacement = replacement if first.id == consumed.id else consumed
    mark_asset_used(
        db,
        first,
        game_id=games[0].id,
        post_id="completed-post",
        contribution_type="announcement",
    )

    selected = reserve_media(
        db,
        club_id=team.club_id,
        team_id=team.id,
        game_id=games[0].id,
        contribution_type="announcement",
    )

    assert selected.id == replacement.id
    preference = (
        db.query(GameMediaPreference)
        .filter_by(
            game_id=games[0].id,
            contribution_type="announcement",
        )
        .one()
    )
    assert preference.selection_mode == "automatic"
    assert preference.selected_media_asset_id == replacement.id


def test_manual_selection_still_rejects_consumed_saved_preference(db):
    team, games = _graph(db)
    selected = _asset(db, team, "manual-consumed")
    set_game_preference(
        db,
        club_id=team.club_id,
        team_id=team.id,
        game_id=games[0].id,
        contribution_type="announcement",
        selection_mode="manual",
        selected_media_asset_id=selected.id,
        allow_used_once=False,
        actor_user_id=None,
    )
    selected.uses = 1
    selected.automatic_usage_enabled = False
    selected.active = False

    with pytest.raises(MediaLibraryError, match="bereits verwendet"):
        reserve_media(
            db,
            club_id=team.club_id,
            team_id=team.id,
            game_id=games[0].id,
            contribution_type="announcement",
        )


def test_manual_one_time_reuse_overrides_policy_without_global_release(db):
    team, games = _graph(db)
    portrait = _asset(
        db,
        team,
        "historical-portrait",
        "player_portrait",
        uses=1,
        automatic=False,
    )

    with pytest.raises(MediaLibraryError, match="einmalige Wiederverwendung"):
        set_game_preference(
            db,
            club_id=team.club_id,
            team_id=team.id,
            game_id=games[0].id,
            contribution_type="result",
            selection_mode="manual",
            selected_media_asset_id=portrait.id,
            allow_used_once=False,
            actor_user_id=None,
        )

    set_game_preference(
        db,
        club_id=team.club_id,
        team_id=team.id,
        game_id=games[0].id,
        contribution_type="result",
        selection_mode="manual",
        selected_media_asset_id=portrait.id,
        allow_used_once=True,
        actor_user_id=None,
    )
    selected = reserve_media(
        db,
        club_id=team.club_id,
        team_id=team.id,
        game_id=games[0].id,
        contribution_type="result",
    )
    assert selected.id == portrait.id
    assert portrait.automatic_usage_enabled is False
    assert portrait.uses == 2
    assert (
        db.query(MediaUsageHistory)
        .filter_by(
            media_asset_id=portrait.id,
            action="manual_reuse",
            game_id=games[0].id,
        )
        .one()
    )


def test_manual_selection_rejects_asset_from_another_team(db):
    team, games = _graph(db)
    other_team = Team(
        internal_name="other-media-team",
        display_name="Testverein II",
        short_name="TV II",
        slug="other-media-team",
        club="Testverein",
        fussball_url="https://www.fussball.de/other-media-team",
        instagram_page_id=team.instagram_page_id,
        media_subdir="test-zwei/spieler",
    )
    db.add(other_team)
    db.flush()
    foreign_team_asset = _asset(db, other_team, "wrong-team")

    with pytest.raises(MediaLibraryError, match="nicht verfügbar"):
        set_game_preference(
            db,
            club_id=team.club_id,
            team_id=team.id,
            game_id=games[0].id,
            contribution_type="announcement",
            selection_mode="manual",
            selected_media_asset_id=foreign_team_asset.id,
            allow_used_once=False,
            actor_user_id=None,
        )


def test_delete_blocks_future_selection_and_preserves_used_source(db):
    team, games = _graph(db)
    planned = _asset(db, team, "planned")
    set_game_preference(
        db,
        club_id=team.club_id,
        team_id=team.id,
        game_id=games[0].id,
        contribution_type="announcement",
        selection_mode="manual",
        selected_media_asset_id=planned.id,
        allow_used_once=False,
        actor_user_id=None,
    )
    with pytest.raises(MediaLibraryError, match="bewusst.*ausgewählt"):
        soft_delete_asset(db, planned, actor_user_id=None)

    used = _asset(db, team, "used", uses=1, automatic=False)
    assert soft_delete_asset(db, used, actor_user_id=None) is False
    assert used.deleted_at is not None
    assert used.available is True
    assert usage_status(used) == "deleted"


def test_global_release_preserves_immutable_history(db):
    team, games = _graph(db)
    item = _asset(db, team, "release")
    reserve_media(
        db,
        club_id=team.club_id,
        team_id=team.id,
        game_id=games[0].id,
        contribution_type="announcement",
    )
    mark_asset_used(
        db,
        item,
        game_id=games[0].id,
        post_id=None,
        contribution_type="announcement",
    )
    release_asset(db, item, actor_user_id=None)
    actions = [
        row.action
        for row in db.query(MediaUsageHistory)
        .filter_by(media_asset_id=item.id)
        .order_by(MediaUsageHistory.created_at)
    ]
    assert actions == ["reserved", "used", "released"]
    assert item.uses == 0
    assert item.automatic_usage_enabled is True


def test_gallery_hides_technical_values_and_supports_all_visible_teams(browser):
    client, factory = browser
    with factory() as db:
        first, first_games = _graph(db)
        second = Team(
            internal_name="media-team-two",
            display_name="Testverein II",
            short_name="TV II",
            slug="media-team-two",
            club="Testverein",
            fussball_url="https://www.fussball.de/test-two",
            instagram_page_id=first.instagram_page_id,
            media_subdir="test-zwei/spieler",
        )
        db.add(second)
        db.flush()
        first_asset = _asset(db, first, "first-gallery")
        _asset(db, second, "second-gallery", "team_photo")
        first_asset.reserved_game_id = first_games[0].id
        first_asset.uses = 1
        db.commit()

    page = client.get("/media")
    assert page.status_code == 200
    assert "Alle Bilder" in page.text
    assert "Testverein I" in page.text and "Testverein II" in page.text
    assert "Vorgemerkt für Testverein" in page.text
    assert "Prüfsumme" not in page.text
    assert first_asset.checksum not in page.text
    assert "Direkter Upload" not in page.text

    detail = client.get(f"/media/{first_asset.id}")
    assert detail.status_code == 200
    assert "Technische Dateiinformationen" in detail.text
    assert "Hochgeladen am" in detail.text


def test_unused_uploaded_medium_is_removed_from_database_view_and_storage(
    browser, tmp_path, monkeypatch
):
    client, factory = browser
    upload_root = tmp_path / "uploads"
    import app.admin_routes as admin_routes

    monkeypatch.setattr(admin_routes.settings, "upload_root", upload_root)
    with factory() as db:
        team, _games = _graph(db)
        source = upload_root / "clubs" / team.club_id / "players" / "unused.jpg"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"unused")
        asset = MediaAsset(
            team_id=team.id,
            storage_kind="upload",
            relative_path=source.relative_to(upload_root).as_posix(),
            filename="unused.jpg",
            mime_type="image/jpeg",
            size=source.stat().st_size,
            width=1080,
            height=1350,
            checksum="f" * 64,
            mtime=datetime.now(timezone.utc),
            media_category="match_photo",
        )
        db.add(asset)
        db.commit()
        asset_id = asset.id
        team_id = team.id

    csrf = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/teams").text).group(1)
    response = client.post(
        f"/media/{asset_id}/delete",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert not source.exists()
    with factory() as db:
        deleted = db.get(MediaAsset, asset_id)
        assert deleted.deleted_at is not None
        assert deleted.available is False
    assert f'/media/{asset_id}"' not in client.get(f"/media?team_id={team_id}").text


def test_dashboard_upload_respects_club_storage_limit(browser, tmp_path, monkeypatch):
    client, factory = browser
    upload_root = tmp_path / "quota-uploads"
    import app.admin_routes as admin_routes

    monkeypatch.setattr(admin_routes.settings, "upload_root", upload_root)
    with factory() as db:
        team, _games = _graph(db)
        club = db.get(Club, team.club_id)
        club.limit_overrides = {**(club.limit_overrides or {}), "storage_bytes": 128}
        db.commit()
        team_id = team.id

    payload = BytesIO()
    Image.new("RGB", (640, 640), color=(18, 42, 99)).save(payload, format="PNG")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/teams").text).group(1)
    response = client.post(
        f"/media/{team_id}/upload",
        data={"csrf_token": csrf, "media_category": "match_photo"},
        files={"files": ("limit.png", payload.getvalue(), "image/png")},
    )
    assert response.status_code == 422
    assert "Speicherlimit" in response.text
    assert not list(upload_root.rglob("*.png"))


def test_dashboard_upload_and_delete_are_recorded_in_storage_ledger(browser, tmp_path, monkeypatch):
    client, factory = browser
    upload_root = tmp_path / "ledger-uploads"
    import app.admin_routes as admin_routes

    monkeypatch.setattr(admin_routes.settings, "upload_root", upload_root)
    with factory() as db:
        team, _games = _graph(db)
        db.commit()
        team_id = team.id

    payload = BytesIO()
    Image.new("RGB", (640, 640), color=(18, 99, 42)).save(payload, format="PNG")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/teams").text).group(1)
    response = client.post(
        f"/media/{team_id}/upload",
        data={"csrf_token": csrf, "media_category": "team_photo"},
        files={"files": ("mannschaft.png", payload.getvalue(), "image/png")},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with factory() as db:
        asset = db.query(MediaAsset).filter_by(team_id=team_id).one()
        stored = db.query(StorageObject).filter_by(club_id=asset.club_id).one()
        ledger = db.query(StorageLedgerEntry).filter_by(storage_object_id=stored.id).one()
        assert stored.references == {"media_asset_id": asset.id, "team_id": team_id}
        assert stored.object_key == asset.relative_path
        assert stored.size_bytes == asset.size
        assert stored.deleted_at is None
        assert ledger.status == LedgerStatus.COMMITTED
        assert ledger.actual_bytes == asset.size
        asset_id = asset.id
        club_id = asset.club_id
        source = upload_root / asset.relative_path
        assert source.is_file()

    response = client.post(
        f"/media/{asset_id}/delete",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert not source.exists()
    with factory() as db:
        stored = db.query(StorageObject).filter_by(club_id=club_id).one()
        deletion = (
            db.query(StorageLedgerEntry)
            .filter_by(storage_object_id=stored.id, status=LedgerStatus.DELETED)
            .one()
        )
        assert stored.deleted_at is not None
        assert deletion.details["media_asset_id"] == asset_id


def test_foreign_tenant_media_is_not_visible_by_direct_url(browser):
    client, factory = browser
    with factory() as db, system_scope("Fremdmandant für Isolationstest"):
        profile = PlanProfile(name="Fremdprofil", description="Test", version=1)
        db.add(profile)
        db.flush()
        foreign_club = Club(
            name="Fremdverein",
            short_name="Fremd",
            slug="fremdverein-medien",
            status=ClubStatus.ACTIVE,
            timezone="Europe/Berlin",
            plan_profile_id=profile.id,
        )
        db.add(foreign_club)
        db.flush()
        page = InstagramPage(
            club_id=foreign_club.id,
            internal_name="foreign-media",
            display_name="Fremd",
            username="foreign-media",
            club="Fremdverein",
            active=True,
        )
        db.add(page)
        db.flush()
        team = Team(
            club_id=foreign_club.id,
            internal_name="foreign-team",
            display_name="Fremdverein I",
            short_name="FV I",
            slug="foreign-team",
            club="Fremdverein",
            fussball_url="https://www.fussball.de/fremd",
            instagram_page_id=page.id,
            media_subdir="fremd/spieler",
        )
        db.add(team)
        db.flush()
        item = MediaAsset(
            club_id=foreign_club.id,
            team_id=team.id,
            storage_kind="upload",
            relative_path=f"clubs/{foreign_club.id}/teams/{team.id}/players/foreign.jpg",
            filename="foreign.jpg",
            mime_type="image/jpeg",
            size=1024,
            width=1080,
            height=1350,
            checksum="e" * 64,
            mtime=datetime.now(timezone.utc),
            media_category="match_photo",
        )
        db.add(item)
        db.commit()
        asset_id = item.id

    assert client.get(f"/media/{asset_id}").status_code == 404
    assert client.get(f"/media/{asset_id}/preview").status_code == 404
