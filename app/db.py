from collections.abc import Generator

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker, with_loader_criteria

from app.config import get_settings


class Base(DeclarativeBase):
    pass


class TenantSession(Session):
    """SQLAlchemy session with deny-by-mismatch tenant guards."""

    def get(self, entity, ident, **kwargs):
        item = super().get(entity, ident, **kwargs)
        if item is None or not hasattr(item, "club_id"):
            return item
        from app.tenancy.state import active_scope

        scope = active_scope()
        if scope and scope.kind == "tenant" and getattr(item, "club_id", None) != scope.club_id:
            # Session.get may satisfy requests from the identity map without
            # issuing SQL, so loader criteria alone are not sufficient.
            return None
        return item


settings = get_settings()
engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(engine, class_=TenantSession, expire_on_commit=False)


def _tenant_mapped_classes():
    # Resolve lazily: app.models imports Base from this module.
    special = {"FeatureFlag", "TenantMigrationReport"}
    return tuple(
        mapper.class_
        for mapper in Base.registry.mappers
        if hasattr(mapper.class_, "club_id") and mapper.class_.__name__ not in special
    )


def _statement_targets_tenant(statement) -> bool:
    tenant_classes = set(_tenant_mapped_classes())
    for description in getattr(statement, "column_descriptions", ()):
        entity = description.get("entity")
        if entity in tenant_classes:
            return True
    return False


@event.listens_for(TenantSession, "do_orm_execute")
def _tenant_filter(execute_state):
    from app.tenancy.state import active_scope

    scope = active_scope()
    if scope is None:
        if settings.multi_tenant_enabled and _statement_targets_tenant(execute_state.statement):
            raise PermissionError("Mandantenbezogene Datenabfrage ohne Tenant-Kontext blockiert")
        return
    if scope.kind != "tenant":
        return
    club_id = scope.club_id
    statement = execute_state.statement
    if execute_state.is_update or execute_state.is_delete:
        description = getattr(statement, "entity_description", {}) or {}
        entity = description.get("entity")
        if entity in set(_tenant_mapped_classes()):
            execute_state.statement = statement.where(entity.club_id == club_id)
        return
    if not execute_state.is_select:
        return
    for model in _tenant_mapped_classes():
        statement = statement.options(
            with_loader_criteria(
                model,
                lambda row: row.club_id == club_id,
                include_aliases=True,
            )
        )
    execute_state.statement = statement


@event.listens_for(TenantSession, "before_flush")
def _tenant_write_guard(session, _flush_context, _instances):
    from app.models import (
        AccountType,
        AuditLog,
        CreativeFeedbackEvent,
        GeneratedMediaVersion,
        PostTextVersion,
        User,
    )
    from app.tenancy.state import active_scope

    immutable_versions = (GeneratedMediaVersion, PostTextVersion, CreativeFeedbackEvent)
    for item in session.dirty:
        if isinstance(item, immutable_versions) and session.is_modified(
            item, include_collections=False
        ):
            changed = sorted(
                attribute.key
                for attribute in inspect(item).attrs
                if attribute.history.has_changes()
            )
            raise PermissionError(
                "Historische Medien- und Textversionen sind unveränderlich"
                + (f" ({', '.join(changed)})" if changed else "")
            )

    if any(isinstance(item, CreativeFeedbackEvent) for item in session.deleted):
        raise PermissionError("Creative-Feedback wird nur durch Korrekturen ergaenzt")

    scope = active_scope()
    changed = session.new.union(session.dirty).union(session.deleted)
    if scope is None:
        if settings.multi_tenant_enabled and any(
            hasattr(item, "club_id") for item in changed
        ):
            raise PermissionError("Mandantenbezogener Schreibzugriff ohne Tenant-Kontext blockiert")
        return
    if scope.kind != "tenant":
        return
    for item in changed:
        if not hasattr(item, "club_id"):
            continue
        if isinstance(item, User) and item.account_type == AccountType.PLATFORM_ADMIN:
            raise PermissionError("PlatformAdmin darf nicht im Vereinskontext gespeichert werden")
        if isinstance(item, AuditLog) and item.scope == "platform":
            raise PermissionError("Plattform-Audit darf nicht im Vereinskontext entstehen")
        actual = getattr(item, "club_id", None)
        if item in session.new and actual is None:
            item.club_id = scope.club_id
            actual = scope.club_id
        if actual != scope.club_id:
            raise PermissionError("Vereinsübergreifender Schreibzugriff wurde blockiert")


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as db:
        yield db
