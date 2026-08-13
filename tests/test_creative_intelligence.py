from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.creative.director import build_creative_directive
from app.creative.examples import retrieve_examples
from app.creative.feedback import record_feedback
from app.creative.flags import CREATIVE_FLAG, ONBOARDING_FLAG
from app.creative.learner import rebuild_profile
from app.creative.onboarding import (
    complete_onboarding,
    get_or_create_session,
    rate_sample,
    save_step,
    seed_calibration,
)
from app.creative.scheduler import run_creative_profile_cycle
from app.models import (
    Club,
    ClubBrandingConfiguration,
    ClubStatus,
    CreativeExampleReference,
    CreativeFeedbackEvent,
    CreativePreferenceProfile,
    CreativeProfileOverride,
    CreativeRecipe,
    FeatureFlag,
    OnboardingCalibrationSample,
    UsageLedgerEntry,
    uid,
)
from app.tenancy.context import TenantContext, TenantContextError
from app.tenancy.state import system_scope, tenant_scope


def _context(db) -> TenantContext:
    return TenantContext(club_id=db.info["test_club_id"], actor_user_id="pytest-actor")


def _enable(db, key: str, **values) -> None:
    with system_scope(f"enable {key} for test"):
        db.add(
            FeatureFlag(
                id=uid(),
                club_id=None,
                key=key,
                enabled=True,
                value={"default_clubs_enabled": True, **values},
            )
        )
        db.commit()


def _feedback(
    db,
    context: TenantContext,
    *,
    key: str,
    trait: str,
    value: str,
    action: str = "selected",
) -> CreativeFeedbackEvent:
    event = record_feedback(
        db,
        context,
        modality="image",
        content_type="announcement",
        action=action,
        source="normal_usage",
        idempotency_key=key,
        traits={trait: value},
        force=True,
    )
    assert event is not None
    return event


def test_feedback_is_idempotent_append_only_and_tenant_scoped(db):
    context = _context(db)
    event = _feedback(
        db,
        context,
        key="feedback:one",
        trait="graphic_style",
        value="modern",
    )
    duplicate = _feedback(
        db,
        context,
        key="feedback:one",
        trait="graphic_style",
        value="klassisch",
    )
    assert duplicate.id == event.id
    assert duplicate.traits_snapshot == {"graphic_style": "modern"}
    db.commit()

    event.action = "rejected"
    with pytest.raises(PermissionError, match="Historische"):
        db.flush()
    db.rollback()

    persisted = db.get(CreativeFeedbackEvent, event.id)
    assert persisted is not None
    db.delete(persisted)
    with pytest.raises(PermissionError, match="Feedback"):
        db.flush()
    db.rollback()

    other_id = uid()
    with system_scope("create foreign club for tenant isolation test"):
        original = db.get(Club, context.club_id)
        assert original is not None
        db.add(
            Club(
                id=other_id,
                name="Anderer Verein",
                short_name="AV",
                slug="anderer-verein",
                status=ClubStatus.ACTIVE,
                timezone="Europe/Berlin",
                plan_profile_id=original.plan_profile_id,
            )
        )
        db.commit()
    foreign_context = TenantContext(club_id=other_id, actor_user_id="foreign-actor")
    with tenant_scope(other_id, "foreign-actor"):
        with pytest.raises(TenantContextError):
            record_feedback(
                db,
                foreign_context,
                modality="image",
                content_type="announcement",
                action="selected",
                source="normal_usage",
                idempotency_key="feedback:foreign-reference",
                correction_of_id=event.id,
                traits={"graphic_style": "modern"},
                force=True,
            )


def test_feedback_sampling_and_learning_can_be_disabled_without_blocking(db):
    context = _context(db)
    _enable(
        db,
        CREATIVE_FLAG,
        learning_enabled=True,
        application_enabled=True,
        feedback_sampling_rate=0,
    )
    sampled_out = record_feedback(
        db,
        context,
        modality="text",
        content_type="result",
        action="approved",
        source="normal_usage",
        idempotency_key="feedback:sampled-out",
        traits={"tone": "sachlich"},
    )
    assert sampled_out is None
    assert db.scalar(select(func.count()).select_from(CreativeFeedbackEvent)) == 0


def test_profile_versions_are_traceable_and_usage_is_non_billable(db):
    context = _context(db)
    _enable(
        db,
        CREATIVE_FLAG,
        learning_enabled=True,
        application_enabled=True,
        minimum_samples=5,
    )
    for index in range(5):
        _feedback(
            db,
            context,
            key=f"feedback:profile:{index}",
            trait="graphic_style",
            value="modern",
        )
    first = rebuild_profile(
        db,
        context,
        modality="image",
        content_type="announcement",
    )
    assert first is not None
    assert first.profile_version == 1
    assert first.status == "active"
    assert first.preferences["traits"][0]["value"] == "modern"

    for index in range(5, 10):
        _feedback(
            db,
            context,
            key=f"feedback:profile:{index}",
            trait="dynamics",
            value="dynamisch",
        )
    second = rebuild_profile(
        db,
        context,
        modality="image",
        content_type="announcement",
        reason="nightly",
    )
    assert second is not None
    assert second.profile_version == 2
    assert first.status == "superseded"

    usage = list(
        db.scalars(
            select(UsageLedgerEntry)
            .where(
                UsageLedgerEntry.club_id == context.club_id,
                UsageLedgerEntry.generation_type == "preference_learning",
            )
            .order_by(UsageLedgerEntry.created_at)
        )
    )
    assert len(usage) == 2
    assert all(not item.billable and item.platform_test for item in usage)
    assert all(item.actual_quantity == 1 for item in usage)


def test_periodic_profile_cycle_is_tenant_scoped_and_skips_duplicate_versions(db):
    context = _context(db)
    _enable(
        db,
        CREATIVE_FLAG,
        learning_enabled=True,
        application_enabled=True,
        minimum_samples=5,
        profile_rebuild_schedule="hourly",
    )
    for index in range(5):
        _feedback(
            db,
            context,
            key=f"feedback:scheduled:first:{index}",
            trait="graphic_style",
            value="modern",
        )
    db.commit()

    first_cycle = run_creative_profile_cycle(db)
    assert first_cycle.failures == 0
    assert first_cycle.profiles_built == 1
    first_count = int(
        db.scalar(
            select(func.count()).select_from(CreativePreferenceProfile).where(
                CreativePreferenceProfile.club_id == context.club_id,
                CreativePreferenceProfile.modality == "image",
                CreativePreferenceProfile.content_type == "announcement",
            )
        )
        or 0
    )

    unchanged_cycle = run_creative_profile_cycle(db)
    assert unchanged_cycle.failures == 0
    assert unchanged_cycle.profiles_built == 0
    assert (
        int(
            db.scalar(
                select(func.count()).select_from(CreativePreferenceProfile).where(
                    CreativePreferenceProfile.club_id == context.club_id,
                    CreativePreferenceProfile.modality == "image",
                    CreativePreferenceProfile.content_type == "announcement",
                )
            )
            or 0
        )
        == first_count
    )

    for index in range(5):
        _feedback(
            db,
            context,
            key=f"feedback:scheduled:second:{index}",
            trait="dynamics",
            value="dynamisch",
        )
    db.commit()
    second_cycle = run_creative_profile_cycle(db)
    assert second_cycle.failures == 0
    assert second_cycle.profiles_built == 1
    active = db.scalar(
        select(CreativePreferenceProfile).where(
            CreativePreferenceProfile.club_id == context.club_id,
            CreativePreferenceProfile.modality == "image",
            CreativePreferenceProfile.content_type == "announcement",
            CreativePreferenceProfile.status == "active",
        )
    )
    assert active is not None
    assert active.profile_version == 2


def test_director_respects_branding_then_override_then_learned_preferences(db):
    context = _context(db)
    _enable(
        db,
        CREATIVE_FLAG,
        learning_enabled=True,
        application_enabled=True,
        minimum_confidence=0.5,
    )
    db.add(
        ClubBrandingConfiguration(
            club_id=context.club_id,
            image_settings={"graphic_style": "klassisch"},
            text_settings={},
        )
    )
    profile = CreativePreferenceProfile(
        id=uid(),
        club_id=context.club_id,
        modality="image",
        content_type="announcement",
        profile_version=1,
        status="active",
        preferences={
            "traits": [
                {"key": "graphic_style", "value": "minimalistisch", "score": 5},
                {"key": "dynamics", "value": "ruhig", "score": 4},
                {"key": "contrast", "value": "hoch", "score": 3},
            ]
        },
        avoidances={"traits": []},
        confidence=0.9,
        sample_count=10,
    )
    db.add(profile)
    db.add(
        CreativeProfileOverride(
            id=uid(),
            club_id=context.club_id,
            modality="image",
            content_type="announcement",
            override_version=1,
            preferences={
                "traits": [
                    {"key": "graphic_style", "value": "modern"},
                    {"key": "dynamics", "value": "dynamisch"},
                ]
            },
            avoidances={"traits": []},
            reason="Test",
            active=True,
            created_by=context.actor_user_id,
        )
    )
    db.add(
        CreativeRecipe(
            id=uid(),
            key="announcement-default",
            name="Ankuendigungsstandard",
            modality="image",
            content_type="announcement",
            recipe_version=1,
            status="active",
            traits={"contrast": "niedrig"},
            description="Testet die Prioritaet vor gelernten Praeferenzen.",
            created_by=context.actor_user_id,
        )
    )
    db.flush()

    directive = build_creative_directive(
        db,
        club_id=context.club_id,
        actor_user_id=context.actor_user_id,
        modality="image",
        content_type="announcement",
    )
    assert "Dynamik: dynamisch" in directive.supplement
    assert "Dynamik: ruhig" not in directive.supplement
    assert "Kontrast: hoch" in directive.supplement
    assert "Kontrast: niedrig" not in directive.supplement
    assert "Grafikstil: modern" not in directive.supplement
    assert "Grafikstil: minimalistisch" not in directive.supplement
    assert directive.snapshot["protected_branding_trait_count"] == 1
    assert directive.snapshot["profile_id"] == profile.id


def test_onboarding_is_resumable_version_checked_and_never_publishable(db):
    context = _context(db)
    _enable(db, ONBOARDING_FLAG)
    _enable(db, CREATIVE_FLAG, learning_enabled=True, application_enabled=True)
    session = get_or_create_session(db, context)
    original_id = session.id
    original_version = session.version
    save_step(
        db,
        context,
        step=1,
        values={"welcome": "bestaetigt"},
        expected_version=original_version,
    )
    with pytest.raises(ValueError, match="zwischenzeitlich"):
        save_step(
            db,
            context,
            step=2,
            values={"club_data": "geprueft"},
            expected_version=original_version,
        )
    assert get_or_create_session(db, context).id == original_id

    samples = seed_calibration(db, context, session)
    assert len(samples) == 14
    assert all(item.publishing_blocked for item in samples)
    assert all((item.preview_payload or {}).get("fixture") is True for item in samples)
    assert len(seed_calibration(db, context, session)) == 14

    calibration_usage = list(
        db.scalars(
            select(UsageLedgerEntry).where(
                UsageLedgerEntry.club_id == context.club_id,
                UsageLedgerEntry.generation_type == "onboarding_calibration",
            )
        )
    )
    assert len(calibration_usage) == 14
    assert sum(item.actual_quantity for item in calibration_usage) == 14
    assert all(not item.billable and item.platform_test for item in calibration_usage)

    groups: dict[tuple[str, str], OnboardingCalibrationSample] = {}
    for sample in samples:
        groups.setdefault((sample.modality, sample.content_type), sample)
    for sample in groups.values():
        rate_sample(db, context, sample_id=sample.id, rating="favorite")
    complete_onboarding(db, context, session)
    assert session.status == "completed"
    assert session.completed_steps == list(range(1, 12))
    profiles = list(
        db.scalars(
            select(CreativePreferenceProfile).where(
                CreativePreferenceProfile.club_id == context.club_id,
                CreativePreferenceProfile.status == "active",
            )
        )
    )
    assert {(item.modality, item.content_type) for item in profiles} == {
        ("image", "announcement"),
        ("image", "result"),
        ("text", "announcement"),
        ("text", "result"),
    }


def test_recipe_versions_apply_only_the_active_platform_version(db):
    context = _context(db)
    _enable(db, CREATIVE_FLAG, learning_enabled=True, application_enabled=True)
    first = CreativeRecipe(
        id=uid(),
        key="announcement-style",
        name="Ankuendigung",
        modality="image",
        content_type="announcement",
        recipe_version=1,
        status="active",
        traits={"dynamics": "ruhig"},
        constraints={},
        created_by=context.actor_user_id,
    )
    second = CreativeRecipe(
        id=uid(),
        key="announcement-style",
        name="Ankuendigung",
        modality="image",
        content_type="announcement",
        recipe_version=2,
        status="draft",
        traits={"dynamics": "dynamisch"},
        constraints={},
        created_by=context.actor_user_id,
    )
    with system_scope("create versioned recipes for test"):
        db.add_all([first, second])
        db.commit()

    directive = build_creative_directive(
        db,
        club_id=context.club_id,
        actor_user_id=context.actor_user_id,
        modality="image",
        content_type="announcement",
    )
    assert "Dynamik: ruhig" in directive.supplement
    assert "Dynamik: dynamisch" not in directive.supplement
    assert directive.snapshot["recipe_id"] == first.id
    assert directive.snapshot["recipe_version"] == 1

    with system_scope("activate next recipe version for test"):
        first.status = "archived"
        second.status = "active"
        db.commit()

    directive = build_creative_directive(
        db,
        club_id=context.club_id,
        actor_user_id=context.actor_user_id,
        modality="image",
        content_type="announcement",
    )
    assert "Dynamik: dynamisch" in directive.supplement
    assert "Dynamik: ruhig" not in directive.supplement
    assert directive.snapshot["recipe_id"] == second.id
    assert directive.snapshot["recipe_version"] == 2


def test_example_retrieval_never_crosses_the_tenant_boundary(db):
    context = _context(db)
    foreign_club_id = uid()
    local_reference = CreativeExampleReference(
        id=uid(),
        club_id=context.club_id,
        modality="text",
        content_type="result",
        sentiment="positive",
        text_version_id=uid(),
        rank=0,
        traits={"tone": "emotional"},
        score=1,
        active=True,
    )
    foreign_reference = CreativeExampleReference(
        id=uid(),
        club_id=foreign_club_id,
        modality="text",
        content_type="result",
        sentiment="positive",
        text_version_id=uid(),
        rank=0,
        traits={"tone": "sachlich"},
        score=1,
        active=True,
    )
    with system_scope("create cross-tenant reference fixture"):
        original = db.get(Club, context.club_id)
        assert original is not None
        db.add(
            Club(
                id=foreign_club_id,
                name="Fremder Testverein",
                short_name="FT",
                slug="fremder-testverein",
                status=ClubStatus.ACTIVE,
                timezone="Europe/Berlin",
                plan_profile_id=original.plan_profile_id,
            )
        )
        db.add_all([local_reference, foreign_reference])
        db.commit()

    positives, negatives = retrieve_examples(
        db,
        context,
        modality="text",
        content_type="result",
    )
    assert [item.id for item in positives] == [local_reference.id]
    assert negatives == []

    foreign_context = TenantContext(
        club_id=foreign_club_id,
        actor_user_id="foreign-actor",
    )
    with tenant_scope(foreign_club_id, "foreign-actor"):
        positives, negatives = retrieve_examples(
            db,
            foreign_context,
            modality="text",
            content_type="result",
        )
    assert [item.id for item in positives] == [foreign_reference.id]
    assert negatives == []
