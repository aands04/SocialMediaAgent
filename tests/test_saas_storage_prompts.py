import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from starlette.requests import Request

import app.platform.routes as platform_routes
from app.branding.service import branding_snapshot
from app.config import Settings
from app.models import (
    AccountType,
    AuditLog,
    Club,
    ClubBrandingConfiguration,
    LedgerStatus,
    PromptStatus,
    PromptTemplate,
    Role,
    StorageLedgerEntry,
    StorageObject,
    StorageReconciliationRun,
    UsageLedgerEntry,
    User,
)
from app.platform.prompt_tests import run_fixture_prompt_test
from app.prompts.service import resolve_prompt
from app.storage.providers import (
    LocalObjectStorageProvider,
    ObjectStorageError,
    SmbImportProvider,
)
from app.storage.service import (
    cleanup_expired_uploads,
    complete_direct_upload,
    create_direct_upload,
    reconcile_storage,
)
from app.tenancy.state import platform_scope, system_scope


def _facts(club_id: str) -> dict:
    return {
        "club_id": club_id,
        "home_team": "Testverein",
        "away_team": "Gegner",
        "own_team": "Testverein",
        "kickoff": "2026-08-09T13:00:00+00:00",
        "competition": "Kreisliga A",
        "venue": "Teststadion",
        "pitch": "Rasenplatz",
        "hashtags": ["#Test"],
    }


def test_prompt_snapshot_hides_body_and_contains_branding_reference(db):
    club_id = db.info["test_club_id"]
    db.add(
        ClubBrandingConfiguration(
            club_id=club_id,
            image_settings={"primary_color": "#123456", "graphic_style": "dynamisch"},
            text_settings={"tone": "emotional"},
        )
    )
    template = PromptTemplate(
        name="protected-text",
        prompt_kind="text",
        post_type="announcement",
        media_kind="none",
        prompt_body="Spiel: {{ home_team }} gegen {{ away_team }}",
        model="fixture-model",
        quality="default",
        version=1,
        status=PromptStatus.ACTIVE,
        active=True,
    )
    db.add(template)
    db.commit()
    prompt = resolve_prompt(
        db, "protected-text", "text", "announcement", "none", _facts(club_id)
    )
    assert '"tone":"emotional"' in prompt.rendered
    assert prompt.branding["image"]["primary_color"] == "#123456"
    snapshot = prompt.snapshot()
    assert snapshot["template_id"] == template.id
    assert snapshot["branding"]["club_id"] == club_id
    assert "body" not in snapshot and "rendered" not in snapshot
    assert len(snapshot["template_checksum"]) == 64
    assert branding_snapshot(db, club_id)["text"]["tone"] == "emotional"


def test_private_direct_upload_uses_immutable_club_namespace(db, tmp_path):
    club_id = db.info["test_club_id"]
    user = User(
        email="upload@example.invalid",
        password_hash="x",
        role=Role.ADMIN,
        all_teams=True,
    )
    db.add(user)
    db.commit()
    provider = LocalObjectStorageProvider(tmp_path / "objects")
    settings = Settings(upload_root=tmp_path, s3_presign_ttl_seconds=300)
    payload = b"\x89PNG\r\n\x1a\n"  # invalid body is rejected at completion
    upload, url = create_direct_upload(
        db,
        settings,
        provider,
        user=user,
        category="players",
        expected_size=len(payload),
        mime_type="image/png",
        checksum=hashlib.sha256(payload).hexdigest(),
        idempotency_key="upload-1",
    )
    assert upload.object_key.startswith(f"clubs/{club_id}/players/")
    assert club_id in upload.object_key and club_id not in url
    provider.put(upload.object_key, payload, "image/png")
    with pytest.raises(Exception, match="Bilddatei"):
        complete_direct_upload(db, provider, upload)
    db.rollback()


def test_local_object_storage_blocks_traversal(tmp_path):
    provider = LocalObjectStorageProvider(tmp_path / "objects")
    with pytest.raises(ObjectStorageError):
        provider.put("clubs/abc/../secret", b"x", "text/plain")


def test_smb_import_provider_is_strictly_read_only(tmp_path):
    root = tmp_path / "smb"
    source = root / "team" / "players"
    source.mkdir(parents=True)
    image = source / "player.png"
    image.write_bytes(b"readonly-import")
    provider = SmbImportProvider(root)

    assert provider.get("team/players/player.png") == b"readonly-import"
    assert provider.head("team/players/player.png")["size"] == len(b"readonly-import")
    assert provider.list("team") == [
        {"key": "team/players/player.png", "size": len(b"readonly-import")}
    ]
    with pytest.raises(ObjectStorageError, match="lesende Importquelle"):
        provider.put("team/new.png", b"x", "image/png")
    with pytest.raises(ObjectStorageError, match="nicht gelöscht"):
        provider.delete("team/players/player.png")
    assert image.is_file()


def test_expired_direct_upload_releases_storage_reservation(db, tmp_path):
    user = User(
        email="expired-upload@example.invalid",
        password_hash="x",
        role=Role.ADMIN,
        all_teams=True,
    )
    db.add(user)
    db.commit()
    provider = LocalObjectStorageProvider(tmp_path / "expired-objects")
    upload, _ = create_direct_upload(
        db,
        Settings(upload_root=tmp_path),
        provider,
        user=user,
        category="players",
        expected_size=100,
        mime_type="image/png",
        checksum=None,
        idempotency_key="expired-upload",
    )
    upload.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()

    assert cleanup_expired_uploads(db, provider) == 1
    assert upload.status == "expired"
    ledger = db.get(StorageLedgerEntry, upload.ledger_entry_id)
    assert ledger.status == LedgerStatus.RELEASED
    assert ledger.reserved_bytes == 0


def test_storage_reconciliation_detects_size_changes_without_mutating_objects(db, tmp_path):
    club_id = db.info["test_club_id"]
    provider = LocalObjectStorageProvider(tmp_path / "reconciliation-objects")
    object_key = f"clubs/{club_id}/players/player-image"
    original = b"original-content"
    provider.put(object_key, original, "image/png")
    item = StorageObject(
        club_id=club_id,
        provider=provider.name,
        bucket=provider.bucket,
        object_key=object_key,
        category="players",
        size_bytes=len(original),
        checksum=hashlib.sha256(original).hexdigest(),
        mime_type="image/png",
    )
    db.add(item)
    db.commit()

    completed = reconcile_storage(db, provider, club_id=club_id, started_by=None)
    db.commit()
    assert completed.status == "completed"
    assert completed.checked_objects == 1
    assert completed.size_mismatches == 0

    changed = b"changed-and-longer-content"
    provider.put(object_key, changed, "image/png")
    attention = reconcile_storage(db, provider, club_id=club_id, started_by=None)
    db.commit()
    assert attention.status == "attention_required"
    assert attention.checked_objects == 1
    assert attention.size_mismatches == 1
    assert attention.report["size_mismatches"] == [object_key]
    assert provider.get(object_key) == changed


def test_platform_admin_can_start_audited_storage_reconciliation(db, tmp_path, monkeypatch):
    club_id = db.info["test_club_id"]
    with system_scope("PlatformAdmin für Speicherabgleich anlegen"):
        actor = User(
            email="storage-platform@example.invalid",
            password_hash="x",
            role=Role.ADMIN,
            account_type=AccountType.PLATFORM_ADMIN,
            club_id=None,
        )
        db.add(actor)
        db.commit()
    monkeypatch.setattr(
        platform_routes,
        "get_settings",
        lambda: Settings(upload_root=tmp_path, object_storage_provider="local"),
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/platform/storage/reconcile",
            "query_string": b"",
            "headers": [],
            "scheme": "https",
            "server": ("testserver", 443),
            "client": ("testclient", 123),
            "session": {"csrf": "storage-csrf"},
        }
    )

    with platform_scope(actor.id):
        response = platform_routes.run_storage_reconciliation(
            request,
            csrf_token_value="storage-csrf",
            club_id=club_id,
            current=actor,
            db=db,
        )
        run = db.query(StorageReconciliationRun).one()
        audit = db.query(AuditLog).filter_by(entity_id=run.id).one()

    assert response.status_code == 303
    assert run.status == "completed"
    assert run.club_id == club_id
    assert audit.scope == "platform"
    assert audit.details["status"] == "completed"
    assert "report" not in audit.details


def test_platform_prompt_fixture_test_is_recorded_without_club_charge_or_audit_body(db):
    club_id = db.info["test_club_id"]
    with system_scope("PlatformAdmin für Prompt-Test anlegen"):
        actor = User(
            email="prompt-platform@example.invalid",
            password_hash="x",
            role=Role.ADMIN,
            account_type=AccountType.PLATFORM_ADMIN,
            club_id=None,
        )
        candidate = PromptTemplate(
            name="fixture-candidate",
            prompt_kind="text",
            post_type="announcement",
            media_kind="none",
            prompt_body="Fixture {{ home_team }} gegen {{ away_team }}",
            model="fixture-model",
            quality="default",
            version=1,
            status=PromptStatus.DRAFT,
            active=False,
        )
        db.add_all([actor, candidate])
        db.commit()
        club = db.get(Club, club_id)

    with platform_scope(actor.id):
        result = run_fixture_prompt_test(db, actor, club=club, candidate=candidate)
        db.commit()
        usage = db.query(UsageLedgerEntry).filter_by(
            idempotency_key=f"platform-prompt-test:{result.id}"
        ).one()
        audit = db.query(AuditLog).filter_by(entity_id=result.id).one()

    assert result.id and result.status == "completed"
    assert usage.platform_test is True
    assert usage.billable is False
    assert usage.actual_quantity == 1
    assert "Fixture Test gegen" in result.result_snapshot["candidate"]
    assert "Fixture Test gegen" not in str(audit.details)
