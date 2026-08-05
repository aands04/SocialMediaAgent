from app.auth.service import hash_password
from app.db import Base, SessionLocal, engine
from app.models import Role, User

Base.metadata.create_all(engine)
with SessionLocal() as db:
    if not db.query(User).filter_by(email="admin@example.invalid").first():
        db.add(
            User(
                email="admin@example.invalid",
                password_hash=hash_password("ChangeMe-Immediately!"),
                role=Role.ADMIN,
                all_teams=True,
            )
        )
        db.commit()
print("Beispiel-Administrator angelegt; Passwort sofort ändern.")
