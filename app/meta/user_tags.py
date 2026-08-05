import json
import math
import re

MAX_USER_TAGS_PER_IMAGE = 20
MAX_USER_TAG_SPEC_CHARS = 50_000
_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_](?:[A-Za-z0-9._]{0,28}[A-Za-z0-9_])?$")


class UserTagValidationError(ValueError):
    pass


def normalize_instagram_username(value: object) -> str:
    if not isinstance(value, str):
        raise UserTagValidationError("Instagram-Benutzername ist ungültig")
    username = value.strip().removeprefix("@").lower()
    if not _USERNAME_PATTERN.fullmatch(username) or ".." in username or len(username) > 30:
        raise UserTagValidationError(
            "Instagram-Benutzername darf nur Buchstaben, Zahlen, Punkte und Unterstriche enthalten"
        )
    return username


def normalize_user_tag_list(value: object) -> list[dict[str, float | str]]:
    if value in (None, []):
        return []
    if not isinstance(value, list):
        raise UserTagValidationError("Instagram-Markierungen müssen eine Liste sein")
    if len(value) > MAX_USER_TAGS_PER_IMAGE:
        raise UserTagValidationError(
            f"Pro Bild sind höchstens {MAX_USER_TAGS_PER_IMAGE} Instagram-Markierungen zulässig"
        )
    result: list[dict[str, float | str]] = []
    usernames: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise UserTagValidationError("Instagram-Markierung ist ungültig")
        username = normalize_instagram_username(item.get("username"))
        if username in usernames:
            raise UserTagValidationError(f"@{username} ist auf diesem Bild mehrfach markiert")
        try:
            x = float(item["x"])
            y = float(item["y"])
        except (KeyError, TypeError, ValueError) as exc:
            raise UserTagValidationError(
                f"Position der Instagram-Markierung @{username} ist ungültig"
            ) from exc
        if not math.isfinite(x) or not math.isfinite(y) or not 0 <= x <= 1 or not 0 <= y <= 1:
            raise UserTagValidationError(
                f"Position der Instagram-Markierung @{username} liegt außerhalb des Bildes"
            )
        usernames.add(username)
        result.append({"username": username, "x": round(x, 6), "y": round(y, 6)})
    return result


def parse_user_tag_specs(
    value: str | None,
    image_count: int,
    *,
    allow_tags: bool,
) -> list[list[dict[str, float | str]]]:
    if not value or not value.strip():
        return [[] for _ in range(image_count)]
    if len(value) > MAX_USER_TAG_SPEC_CHARS:
        raise UserTagValidationError("Instagram-Markierungsdaten sind zu groß")
    try:
        raw = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise UserTagValidationError("Instagram-Markierungsdaten sind ungültig") from exc
    if not isinstance(raw, list) or len(raw) != image_count:
        raise UserTagValidationError(
            "Instagram-Markierungen passen nicht zu den ausgewählten Bildern"
        )
    result = [normalize_user_tag_list(item) for item in raw]
    if not allow_tags and any(result):
        raise UserTagValidationError(
            "Positionsbezogene Instagram-Markierungen werden für Storys nicht unterstützt"
        )
    return result


def user_tags_from_snapshot(
    design_snapshot: dict | None,
    position: int,
) -> list[dict[str, float | str]]:
    snapshot = design_snapshot or {}
    if snapshot.get("source") != "manual_upload":
        return []
    images = (snapshot.get("manual_upload") or {}).get("images") or []
    if not isinstance(images, list):
        raise UserTagValidationError("Eingefrorene Bilddaten sind ungültig")
    image = next(
        (item for item in images if isinstance(item, dict) and item.get("position") == position),
        None,
    )
    if image is None:
        raise UserTagValidationError(f"Eingefrorene Bilddaten für Position {position} fehlen")
    return normalize_user_tag_list(image.get("user_tags") or [])


def serialize_user_tags(value: object) -> str | None:
    tags = normalize_user_tag_list(value)
    if not tags:
        return None
    return json.dumps(tags, ensure_ascii=True, separators=(",", ":"))
