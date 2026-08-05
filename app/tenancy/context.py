from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeVar

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.models import AccountType, Club, ClubStatus, User


class TenantContextError(PermissionError):
    """Raised when a tenant-scoped operation cannot be proven safe."""


T = TypeVar("T")


def _account_type(user: User) -> AccountType:
    value = user.account_type
    if isinstance(value, AccountType):
        return value
    try:
        return AccountType(str(value).casefold())
    except ValueError as exc:
        raise TenantContextError("Unbekannter Kontotyp") from exc


@dataclass(frozen=True, slots=True)
class TenantContext:
    club_id: str
    actor_user_id: str

    @classmethod
    def from_user(cls, user: User) -> TenantContext:
        if _account_type(user) is not AccountType.CLUB_USER or not user.club_id:
            raise TenantContextError("Für diese Aktion ist ein eindeutiger Verein erforderlich")
        return cls(club_id=user.club_id, actor_user_id=user.id)

    def assert_club(self, actual_club_id: str | None) -> None:
        if not actual_club_id or actual_club_id != self.club_id:
            raise TenantContextError("Der Datensatz gehört nicht zum aktuellen Verein")

    def cache_key(self, namespace: str, *parts: object) -> str:
        clean_namespace = namespace.strip().replace(":", "-")
        if not clean_namespace:
            raise TenantContextError("Cache-Namespace fehlt")
        return ":".join(("club", self.club_id, clean_namespace, *(str(part) for part in parts)))

    def require_actionable_club(self, db: Session, action: str) -> Club:
        club = db.get(Club, self.club_id)
        if club is None:
            raise TenantContextError("Verein ist nicht vorhanden")
        if club.status not in {ClubStatus.ACTIVE, ClubStatus.TRIAL}:
            raise TenantContextError(
                f"Verein ist für {action} gesperrt (Status: {club.status.value})"
            )
        return club


@dataclass(frozen=True, slots=True)
class PlatformContext:
    actor_user_id: str

    @classmethod
    def from_user(cls, user: User) -> PlatformContext:
        if _account_type(user) is not AccountType.PLATFORM_ADMIN or user.club_id is not None:
            raise TenantContextError("PlatformAdmin-Kontext erforderlich")
        return cls(actor_user_id=user.id)


def tenant_select(model: type[T], context: TenantContext) -> Select[tuple[T]]:
    club_column = getattr(model, "club_id", None)
    if club_column is None:
        raise TenantContextError(f"{model.__name__} ist nicht mandantenfähig")
    return select(model).where(club_column == context.club_id)


def tenant_get(
    db: Session,
    model: type[T],
    object_id: Any,
    context: TenantContext,
    *,
    for_update: bool = False,
) -> T | None:
    id_column = getattr(model, "id", None)
    club_column = getattr(model, "club_id", None)
    if id_column is None or club_column is None:
        raise TenantContextError(f"{model.__name__} ist nicht sicher mandantenfähig")
    statement = select(model).where(id_column == object_id, club_column == context.club_id)
    if for_update:
        statement = statement.with_for_update()
    return db.scalar(statement)
