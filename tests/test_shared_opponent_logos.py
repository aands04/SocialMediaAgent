from io import BytesIO

from PIL import Image
from sqlalchemy import select

from app.logos.service import (
    import_shared_opponent_logo,
    publish_shared_opponent_logo,
    store_logo,
)
from app.models import Club, ClubStatus, LogoAsset, PlanProfile, Role, User
from app.tenancy.state import system_scope, tenant_scope


def _logo_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGBA", (256, 256), (0, 140, 70, 180)).save(output, format="PNG")
    return output.getvalue()


def test_verified_opponent_logo_is_reusable_across_clubs_as_tenant_copy(db, tmp_path):
    upload_root = tmp_path / "uploads"
    source_club = db.scalar(select(Club).where(Club.slug == "testverein"))
    source_user = User(
        email="quelle@example.invalid",
        password_hash="unused",
        role=Role.ADMIN,
        all_teams=True,
        active=True,
    )
    db.add(source_user)
    db.flush()
    payload = _logo_bytes()
    source, created = store_logo(
        db,
        upload_root=upload_root,
        logo_type="opponent",
        team_id=None,
        display_name="TSV Immenhausen II",
        original_filename="immenhausen.png",
        content_type="image/png",
        data=payload,
        uploaded_by=source_user.id,
    )
    shared, published = publish_shared_opponent_logo(
        db,
        upload_root=upload_root,
        source=source,
        data=payload,
    )
    db.commit()
    assert created is True and published is True
    assert shared.source_club_id == source_club.id
    assert shared.original_path != source.original_path

    with system_scope("zweiten Testverein für systemweiten Logokatalog anlegen"):
        profile = PlanProfile(name="Zweiter Testtarif", description="Test", version=1)
        db.add(profile)
        db.flush()
        target_club = Club(
            name="Zielverein",
            short_name="Ziel",
            slug="zielverein",
            status=ClubStatus.ACTIVE,
            timezone="Europe/Berlin",
            plan_profile_id=profile.id,
        )
        db.add(target_club)
        db.flush()
        target_user = User(
            email="ziel@example.invalid",
            password_hash="unused",
            role=Role.ADMIN,
            all_teams=True,
            active=True,
            club_id=target_club.id,
        )
        db.add(target_user)
        db.commit()

    with tenant_scope(target_club.id, target_user.id):
        imported, imported_created = import_shared_opponent_logo(
            db,
            upload_root=upload_root,
            shared=shared,
            display_name="TSV Immenhausen 2.",
            uploaded_by=target_user.id,
        )
        db.commit()

        assert imported_created is True
        assert imported.club_id == target_club.id
        assert imported.checksum == shared.checksum
        assert imported.original_path != shared.original_path
        assert db.get(LogoAsset, source.id) is None
        assert db.get(LogoAsset, imported.id).id == imported.id
