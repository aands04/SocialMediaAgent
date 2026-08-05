from fastapi import Depends, HTTPException

from app.models import User
from app.tenancy.context import PlatformContext, TenantContext, TenantContextError
from app.web import current_user


def current_tenant(current: User = Depends(current_user)) -> TenantContext:
    try:
        return TenantContext.from_user(current)
    except TenantContextError as exc:
        raise HTTPException(403, str(exc)) from exc


def current_platform(current: User = Depends(current_user)) -> PlatformContext:
    try:
        return PlatformContext.from_user(current)
    except TenantContextError as exc:
        raise HTTPException(403, str(exc)) from exc
