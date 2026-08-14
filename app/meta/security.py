import hashlib
import secrets
from collections.abc import Mapping

from cryptography.fernet import Fernet, InvalidToken


class MetaSecretError(RuntimeError):
    pass


def secret_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class TokenCipher:
    """Authentifizierte Tokenverschlüsselung; Schlüssel bleibt außerhalb der DB."""

    def __init__(self, key: str | None):
        if not key:
            raise MetaSecretError("META_TOKEN_ENCRYPTION_KEY-Secret fehlt")
        try:
            self._fernet = Fernet(key.strip().encode("ascii"))
        except (ValueError, TypeError) as exc:
            raise MetaSecretError(
                "META_TOKEN_ENCRYPTION_KEY muss ein gültiger Fernet-Schlüssel sein"
            ) from exc

    def encrypt(self, value: str) -> str:
        if not value:
            raise MetaSecretError("Leerer Meta-Token wird nicht gespeichert")
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str | None) -> str:
        if not value:
            raise MetaSecretError("Für diese Verbindung ist kein Token gespeichert")
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise MetaSecretError(
                "Meta-Token kann mit der aktiven Schlüsselversion nicht entschlüsselt werden"
            ) from exc


def random_oauth_state() -> str:
    return secrets.token_urlsafe(48)


def random_media_token() -> str:
    return secrets.token_urlsafe(48)


def random_confirmation_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _secret_key(key: object) -> bool:
    normalized = str(key).lower().replace("-", "_")
    return (
        "token" in normalized
        or "secret" in normalized
        or "authorization" in normalized
        or normalized
        in {
            "code",
            "oauth_code",
            "pin",
            "registration_pin",
            "two_step_verification_pin",
        }
    )


def sanitize_platform_data(value, *, max_text: int = 500):
    """Entfernt geheime Werte rekursiv vor Speicherung, Audit oder Anzeige."""
    if isinstance(value, Mapping):
        return {
            str(key): (
                "[entfernt]"
                if _secret_key(key)
                else sanitize_platform_data(item, max_text=max_text)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_platform_data(item, max_text=max_text) for item in value[:50]]
    if isinstance(value, str):
        return value[:max_text]
    return value
