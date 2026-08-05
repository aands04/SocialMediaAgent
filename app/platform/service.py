from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.auth.service import hash_password, normalize_email, validate_new_password
from app.models import (
    AccountType,
    AuditLog,
    Club,
    ClubAdditionalAllowance,
    ClubPromptOverride,
    ClubStatus,
    FeatureFlag,
    GenerationJob,
    GenerationJobStatus,
    JobStatus,
    PlanProfile,
    PromptStatus,
    PromptTemplate,
    PublicationJob,
    Role,
    User,
    UserTeam,
)


class PlatformOperationError(ValueError):
    pass


SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
LIMIT_KEYS = {
    "teams",
    "storage_bytes",
    "ai_texts",
    "ai_images",
    "fonts",
    "instagram_pages",
}


def platform_audit(
    db: Session,
    actor: User,
    action: str,
    entity_type: str,
    entity_id: str | None,
    details: dict | None = None,
) -> None:
    db.add(
        AuditLog(
            scope="platform",
            club_id=None,
            user_id=actor.id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details or {},
        )
    )


def create_club_with_admin(
    db: Session,
    actor: User,
    *,
    name: str,
    short_name: str,
    slug: str,
    timezone_name: str,
    contact_name: str,
    contact_email: str,
    admin_email: str,
    admin_password: str,
    plan_profile_id: str,
    status: ClubStatus,
    trial_ends_at: datetime | None,
    limit_overrides: dict,
    branding_settings: dict,
) -> Club:
    name = name.strip()
    short_name = short_name.strip()
    slug = slug.strip().casefold()
    if not name or not short_name or not SLUG_PATTERN.fullmatch(slug):
        raise PlatformOperationError("Vereinsname, Kurzname oder Slug ist ungültig")
    if db.scalar(select(Club.id).where(Club.slug == slug)):
        raise PlatformOperationError("Dieser Vereins-Slug ist bereits vergeben")
    plan = db.get(PlanProfile, plan_profile_id)
    if plan is None or not plan.active or plan.archived_at is not None:
        raise PlatformOperationError("Das Limitprofil ist nicht aktiv")
    admin_email = normalize_email(admin_email)
    if db.scalar(select(User.id).where(User.email == admin_email)):
        raise PlatformOperationError("Die E-Mail-Adresse des Vereinsadministrators ist belegt")
    password_error = validate_new_password(admin_password)
    if password_error:
        raise PlatformOperationError(password_error)
    now = datetime.now(timezone.utc)
    club = Club(
        name=name,
        short_name=short_name,
        slug=slug,
        status=status,
        activated_at=now if status in {ClubStatus.ACTIVE, ClubStatus.TRIAL} else None,
        timezone=timezone_name.strip() or "Europe/Berlin",
        contact_name=contact_name.strip() or None,
        contact_email=normalize_email(contact_email) if contact_email.strip() else None,
        plan_profile_id=plan.id,
        limit_overrides=limit_overrides,
        branding_settings=branding_settings,
        trial_ends_at=trial_ends_at,
        technical_settings={},
        billing_details={},
        contract_details={},
        usage_snapshot={},
    )
    db.add(club)
    db.flush()
    admin = User(
        club_id=club.id,
        account_type=AccountType.CLUB_USER,
        email=admin_email,
        password_hash=hash_password(admin_password),
        role=Role.ADMIN,
        all_teams=True,
        active=True,
        registration_status="approved",
        registration_reviewed_at=now,
        registration_reviewed_by=actor.id,
    )
    db.add(admin)
    db.flush()
    platform_audit(
        db,
        actor,
        "club.created",
        "club",
        club.id,
        {"plan_profile_id": plan.id, "administrator_id": admin.id, "status": status.value},
    )
    return club


def change_club_status(
    db: Session, actor: User, club: Club, new_status: ClubStatus, expected_version: int
) -> None:
    if club.version != expected_version:
        raise PlatformOperationError("Der Verein wurde zwischenzeitlich geändert")
    old_status = club.status
    if old_status == new_status:
        return
    now = datetime.now(timezone.utc)
    club.status = new_status
    club.version += 1
    if new_status in {ClubStatus.ACTIVE, ClubStatus.TRIAL} and not club.activated_at:
        club.activated_at = now
    if new_status == ClubStatus.ARCHIVED:
        club.archived_at = now
    elif old_status == ClubStatus.ARCHIVED:
        club.archived_at = None
    if new_status in {ClubStatus.SUSPENDED, ClubStatus.CANCELLED, ClubStatus.ARCHIVED}:
        for user in db.scalars(select(User).where(User.club_id == club.id)):
            user.auth_version += 1
        for job in db.scalars(
            select(GenerationJob).where(
                GenerationJob.club_id == club.id,
                GenerationJob.status.in_(
                    [
                        GenerationJobStatus.QUEUED,
                        GenerationJobStatus.RETRY_WAIT,
                        GenerationJobStatus.RUNNING,
                    ]
                ),
            )
        ):
            job.cancel_requested = True
            if job.status != GenerationJobStatus.RUNNING:
                job.status = GenerationJobStatus.CANCELLED
                job.active_key = None
                job.completed_at = now
            job.error_category = "club_blocked"
            job.error_message = f"Verein wurde auf {new_status.value} gesetzt"
        for publication in db.scalars(
            select(PublicationJob).where(
                PublicationJob.club_id == club.id,
                PublicationJob.status.not_in(
                    [JobStatus.PUBLISHED, JobStatus.CANCELLED, JobStatus.SKIPPED]
                ),
            )
        ):
            publication.status = JobStatus.CANCELLED
            publication.approval_status = "blocked"
            publication.error = f"Verein wurde auf {new_status.value} gesetzt"
    platform_audit(
        db,
        actor,
        "club.status_changed",
        "club",
        club.id,
        {"old_status": old_status.value, "new_status": new_status.value},
    )


def move_user_to_club(
    db: Session,
    actor: User,
    user: User,
    target_club: Club,
    expected_version: int,
) -> None:
    if user.account_type != AccountType.CLUB_USER or not user.club_id:
        raise PlatformOperationError("Nur Vereinsbenutzer können verschoben werden")
    if user.version != expected_version:
        raise PlatformOperationError("Das Benutzerkonto wurde zwischenzeitlich geändert")
    old_club_id = user.club_id
    if old_club_id == target_club.id:
        return
    db.execute(delete(UserTeam).where(UserTeam.user_id == user.id))
    user.club_id = target_club.id
    user.all_teams = False
    user.auth_version += 1
    user.version += 1
    platform_audit(
        db,
        actor,
        "user.club_changed",
        "user",
        user.id,
        {"old_club_id": old_club_id, "new_club_id": target_club.id},
    )


def update_club_limits(
    db: Session,
    actor: User,
    club: Club,
    *,
    overrides: dict[str, int | None],
    expected_version: int,
) -> None:
    if club.version != expected_version:
        raise PlatformOperationError("Der Verein wurde zwischenzeitlich geändert")
    unknown = set(overrides) - LIMIT_KEYS
    if unknown:
        raise PlatformOperationError("Unbekannte Limitwerte: " + ", ".join(sorted(unknown)))
    cleaned = {
        key: int(value)
        for key, value in overrides.items()
        if value is not None
    }
    if any(value < 0 for value in cleaned.values()):
        raise PlatformOperationError("Limits dürfen nicht negativ sein")
    previous = dict(club.limit_overrides or {})
    club.limit_overrides = cleaned
    club.version += 1
    platform_audit(
        db,
        actor,
        "club.limits_changed",
        "club",
        club.id,
        {"old": previous, "new": cleaned},
    )


def add_temporary_allowance(
    db: Session,
    actor: User,
    club: Club,
    *,
    limit_key: str,
    amount: int,
    starts_at: datetime,
    ends_at: datetime,
    reason: str,
) -> ClubAdditionalAllowance:
    if limit_key not in LIMIT_KEYS or amount <= 0:
        raise PlatformOperationError("Zusatzkontingent ist ungültig")
    if ends_at <= starts_at:
        raise PlatformOperationError("Das Enddatum muss nach dem Startdatum liegen")
    item = ClubAdditionalAllowance(
        club_id=club.id,
        limit_key=limit_key,
        amount=amount,
        starts_at=starts_at,
        ends_at=ends_at,
        reason=reason.strip() or None,
        created_by=actor.id,
    )
    db.add(item)
    db.flush()
    platform_audit(
        db,
        actor,
        "club.allowance_created",
        "club_additional_allowance",
        item.id,
        {
            "club_id": club.id,
            "limit_key": limit_key,
            "amount": amount,
            "starts_at": starts_at.isoformat(),
            "ends_at": ends_at.isoformat(),
        },
    )
    return item


def set_feature_flag(
    db: Session,
    actor: User,
    *,
    key: str,
    enabled: bool,
    value: dict,
    club_id: str | None = None,
) -> FeatureFlag:
    key = key.strip().casefold()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{1,99}", key):
        raise PlatformOperationError("Feature-Flag-Schlüssel ist ungültig")
    statement = select(FeatureFlag).where(FeatureFlag.key == key)
    statement = (
        statement.where(FeatureFlag.club_id == club_id)
        if club_id
        else statement.where(FeatureFlag.club_id.is_(None))
    )
    item = db.scalar(statement.with_for_update() if db.bind.dialect.name == "postgresql" else statement)
    if item is None:
        item = FeatureFlag(
            club_id=club_id,
            key=key,
            enabled=enabled,
            value=value,
            updated_by=actor.id,
        )
        db.add(item)
    else:
        item.enabled = enabled
        item.value = value
        item.updated_by = actor.id
        item.version += 1
    db.flush()
    platform_audit(
        db,
        actor,
        "feature_flag.changed",
        "feature_flag",
        item.id,
        {"club_id": club_id, "key": key, "enabled": enabled},
    )
    return item


def activate_prompt_version(db: Session, actor: User, item: PromptTemplate) -> None:
    if item.status == PromptStatus.ARCHIVED:
        raise PlatformOperationError("Eine archivierte Promptversion kann nicht aktiviert werden")
    now = datetime.now(timezone.utc)
    siblings = list(
        db.scalars(
            select(PromptTemplate).where(
                PromptTemplate.name == item.name,
                PromptTemplate.prompt_kind == item.prompt_kind,
                PromptTemplate.post_type == item.post_type,
                PromptTemplate.media_kind == item.media_kind,
                PromptTemplate.id != item.id,
                PromptTemplate.status == PromptStatus.ACTIVE,
            )
        )
    )
    for sibling in siblings:
        sibling.status = PromptStatus.ARCHIVED
        sibling.active = False
        sibling.archived_at = now
    item.status = PromptStatus.ACTIVE
    item.active = True
    item.activated_at = now
    item.archived_at = None
    platform_audit(
        db,
        actor,
        "prompt.activated",
        "prompt_template",
        item.id,
        {
            "version": item.version,
            "checksum": item.checksum,
            "change_description": item.change_description,
        },
    )


def archive_prompt_version(db: Session, actor: User, item: PromptTemplate) -> None:
    item.status = PromptStatus.ARCHIVED
    item.active = False
    item.archived_at = datetime.now(timezone.utc)
    platform_audit(
        db,
        actor,
        "prompt.archived",
        "prompt_template",
        item.id,
        {"version": item.version, "checksum": item.checksum},
    )


def create_prompt_override(
    db: Session,
    actor: User,
    club: Club,
    *,
    prompt_kind: str,
    post_type: str,
    media_kind: str,
    additional_instruction: str,
    forbidden_phrases: list[str],
    sponsor_rules: list[str],
    club_rules: list[str],
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
    activate: bool = False,
) -> ClubPromptOverride:
    if prompt_kind not in {"image", "text"} or post_type not in {
        "announcement",
        "reminder",
        "result",
    }:
        raise PlatformOperationError("Promptanpassung ist ungültig")
    if prompt_kind == "text":
        media_kind = "none"
    elif media_kind not in {"feed", "story"}:
        raise PlatformOperationError("Bildanpassung benötigt Feed oder Story")
    if valid_from and valid_until and valid_until <= valid_from:
        raise PlatformOperationError("Der Gültigkeitszeitraum ist ungültig")
    instruction = additional_instruction.strip()
    if len(instruction) > 2000:
        raise PlatformOperationError("Zusatzanweisung ist zu lang")
    previous = db.scalar(
        select(ClubPromptOverride)
        .where(
            ClubPromptOverride.club_id == club.id,
            ClubPromptOverride.prompt_kind == prompt_kind,
            ClubPromptOverride.post_type == post_type,
            ClubPromptOverride.media_kind == media_kind,
        )
        .order_by(ClubPromptOverride.version.desc())
    )
    version = previous.version + 1 if previous else 1
    payload = {
        "additional_instruction": instruction,
        "forbidden_phrases": forbidden_phrases,
        "sponsor_rules": sponsor_rules,
        "club_rules": club_rules,
    }
    if activate:
        for active in db.scalars(
            select(ClubPromptOverride).where(
                ClubPromptOverride.club_id == club.id,
                ClubPromptOverride.prompt_kind == prompt_kind,
                ClubPromptOverride.post_type == post_type,
                ClubPromptOverride.media_kind == media_kind,
                ClubPromptOverride.status == PromptStatus.ACTIVE,
            )
        ):
            active.status = PromptStatus.ARCHIVED
    item = ClubPromptOverride(
        club_id=club.id,
        prompt_kind=prompt_kind,
        post_type=post_type,
        media_kind=media_kind,
        additional_instruction=instruction or None,
        forbidden_phrases=forbidden_phrases,
        preferred_design={},
        sponsor_rules=sponsor_rules,
        club_rules=club_rules,
        valid_from=valid_from,
        valid_until=valid_until,
        status=PromptStatus.ACTIVE if activate else PromptStatus.DRAFT,
        checksum=hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest(),
        created_by=actor.id,
        version=version,
    )
    db.add(item)
    db.flush()
    platform_audit(
        db,
        actor,
        "prompt_override.created",
        "club_prompt_override",
        item.id,
        {"club_id": club.id, "version": version, "checksum": item.checksum},
    )
    return item
