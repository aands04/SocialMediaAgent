"""Create or rotate a PlatformAdmin account outside normal HTTP routes."""

from __future__ import annotations

import argparse
import getpass
from datetime import datetime, timezone

from sqlalchemy import select

from app.auth.service import hash_password, normalize_email, validate_new_password
from app.db import SessionLocal
from app.models import AccountType, AuditLog, Role, User
from app.tenancy.state import system_scope


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Besonders geschützte Anlage oder Kennwortrotation eines PlatformAdmin"
    )
    parser.add_argument("--email", required=True)
    args = parser.parse_args()
    email = normalize_email(args.email)
    password = getpass.getpass("Neues PlatformAdmin-Passwort: ")
    confirmation = getpass.getpass("Passwort wiederholen: ")
    if password != confirmation:
        raise SystemExit("Passwörter stimmen nicht überein")
    if error := validate_new_password(password):
        raise SystemExit(error)

    with system_scope("PlatformAdmin über lokalen Wartungsbefehl verwalten"), SessionLocal() as db:
        item = db.scalar(select(User).where(User.email == email).with_for_update())
        now = datetime.now(timezone.utc)
        if item is not None and (
            item.account_type != AccountType.PLATFORM_ADMIN or item.club_id is not None
        ):
            raise SystemExit(
                "Diese E-Mail-Adresse gehört einem Vereinsbenutzer und kann nicht eskaliert werden"
            )
        action = "platform_admin.password_rotated"
        if item is None:
            item = User(
                email=email,
                password_hash=hash_password(password),
                account_type=AccountType.PLATFORM_ADMIN,
                club_id=None,
                role=Role.ADMIN,
                all_teams=False,
                active=True,
                registration_status="approved",
                registration_reviewed_at=now,
            )
            db.add(item)
            db.flush()
            action = "platform_admin.created"
        else:
            item.password_hash = hash_password(password)
            item.active = True
            item.archived_at = None
            item.failed_logins = 0
            item.locked_until = None
            item.auth_version += 1
            item.version += 1
        db.add(
            AuditLog(
                scope="platform",
                club_id=None,
                user_id=item.id,
                action=action,
                entity_type="user",
                entity_id=item.id,
                details={"email": email},
            )
        )
        db.commit()
    print(f"PlatformAdmin {email} wurde sicher verwaltet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
