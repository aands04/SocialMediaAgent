from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Iterator, Literal

ScopeKind = Literal["tenant", "platform", "system"]


@dataclass(frozen=True, slots=True)
class ActiveScope:
    kind: ScopeKind
    actor_user_id: str | None = None
    club_id: str | None = None
    reason: str | None = None


_ACTIVE_SCOPE: ContextVar[ActiveScope | None] = ContextVar(
    "social_media_agent_active_scope", default=None
)


def active_scope() -> ActiveScope | None:
    return _ACTIVE_SCOPE.get()


def activate_tenant(club_id: str, actor_user_id: str) -> Token:
    if not club_id or not actor_user_id:
        raise PermissionError("Tenant-Kontext ist unvollständig")
    return _ACTIVE_SCOPE.set(
        ActiveScope(kind="tenant", club_id=club_id, actor_user_id=actor_user_id)
    )


def activate_platform(actor_user_id: str) -> Token:
    if not actor_user_id:
        raise PermissionError("PlatformAdmin-Kontext ist unvollständig")
    return _ACTIVE_SCOPE.set(ActiveScope(kind="platform", actor_user_id=actor_user_id))


def clear_scope() -> Token:
    return _ACTIVE_SCOPE.set(None)


def reset_scope(token: Token) -> None:
    _ACTIVE_SCOPE.reset(token)


@contextmanager
def tenant_scope(club_id: str, actor_user_id: str) -> Iterator[ActiveScope]:
    token = activate_tenant(club_id, actor_user_id)
    try:
        yield active_scope()  # type: ignore[misc]
    finally:
        reset_scope(token)


@contextmanager
def platform_scope(actor_user_id: str) -> Iterator[ActiveScope]:
    token = activate_platform(actor_user_id)
    try:
        yield active_scope()  # type: ignore[misc]
    finally:
        reset_scope(token)


@contextmanager
def system_scope(reason: str) -> Iterator[ActiveScope]:
    clean_reason = reason.strip()
    if not clean_reason:
        raise PermissionError("Systemkontext benötigt einen dokumentierten Grund")
    token = _ACTIVE_SCOPE.set(ActiveScope(kind="system", reason=clean_reason))
    try:
        yield active_scope()  # type: ignore[misc]
    finally:
        reset_scope(token)
