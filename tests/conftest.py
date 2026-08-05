import pytest
from sqlalchemy import create_engine

from app.db import Base, TenantSession
from app.models import Club, ClubStatus, PlanProfile
from app.tenancy.state import system_scope, tenant_scope


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    with TenantSession(bind=engine, expire_on_commit=False) as session:
        with system_scope("pytest tenant bootstrap"):
            profile = PlanProfile(name="Test", description="Testprofil", version=1)
            session.add(profile)
            session.flush()
            club = Club(
                name="Testverein",
                short_name="Test",
                slug="testverein",
                status=ClubStatus.ACTIVE,
                timezone="Europe/Berlin",
                plan_profile_id=profile.id,
            )
            session.add(club)
            session.commit()
        session.info["test_club_id"] = club.id
        with tenant_scope(club.id, "pytest-actor"):
            yield session


@pytest.fixture
def media_root(tmp_path):
    path = tmp_path / "media"
    path.mkdir()
    return path
