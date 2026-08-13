from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

MODALITIES = frozenset({"image", "text"})
IMAGE_CONTENT_TYPES = frozenset({"announcement", "result", "reminder", "goal", "live"})
TEXT_CONTENT_TYPES = frozenset({"announcement", "result", "reminder", "live"})
ACTIONS = frozenset(
    {
        "selected",
        "published",
        "approved",
        "rejected",
        "regenerated",
        "reverted",
        "manually_edited",
        "replaced",
        "skipped",
    }
)
SOURCES = frozenset(
    {
        "onboarding_explicit",
        "onboarding_calibration",
        "normal_usage",
        "explicit_feedback",
        "platform_admin_override",
    }
)
SENTIMENTS = frozenset({"positive", "negative", "neutral"})

REASON_CODES = frozenset(
    {
        "layout",
        "colors",
        "typography",
        "player_focus",
        "background",
        "logo_usage",
        "sponsor_usage",
        "text_length",
        "tone",
        "emoji_usage",
        "hashtags",
        "call_to_action",
        "factual_accuracy",
        "other",
        "player_too_small",
        "player_position",
        "too_much_text",
        "too_little_text",
        "too_dark",
        "too_bright",
        "too_busy",
        "too_plain",
        "composition",
        "overall_impression",
    }
)

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_VALUE_RE = re.compile(r"^[\w äöüÄÖÜß+./:#-]{1,100}$", re.UNICODE)


class CreativeValidationError(ValueError):
    pass


def normalize_modality(value: str) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized not in MODALITIES:
        raise CreativeValidationError("Unbekannte Kreativmodalität")
    return normalized


def normalize_content_type(modality: str, value: str) -> str:
    normalized_modality = normalize_modality(modality)
    normalized = str(value or "").strip().casefold()
    allowed = IMAGE_CONTENT_TYPES if normalized_modality == "image" else TEXT_CONTENT_TYPES
    if normalized not in allowed:
        raise CreativeValidationError("Unbekannter Beitragstyp für diese Modalität")
    return normalized


def normalize_action(value: str) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized not in ACTIONS:
        raise CreativeValidationError("Unbekannte Feedbackaktion")
    return normalized


def normalize_source(value: str) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized not in SOURCES:
        raise CreativeValidationError("Unbekannte Feedbackquelle")
    return normalized


def default_sentiment(action: str) -> str:
    return {
        "selected": "positive",
        "published": "positive",
        "approved": "positive",
        "rejected": "negative",
        "regenerated": "negative",
        # A conscious return to an older version is a strong preference signal
        # for that restored version.  The version that was replaced receives a
        # separate negative `replaced` event at the workflow boundary.
        "reverted": "positive",
        "manually_edited": "negative",
        "replaced": "negative",
        "skipped": "neutral",
    }[normalize_action(action)]


def normalize_reason_codes(values: Sequence[str] | None) -> list[str]:
    normalized: list[str] = []
    for raw in values or ():
        value = str(raw or "").strip().casefold()
        if value not in REASON_CODES:
            raise CreativeValidationError(f"Unbekannter Feedbackgrund: {value}")
        if value not in normalized:
            normalized.append(value)
    return normalized


def normalize_free_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(str(value).split()).strip()
    if not normalized:
        return None
    if len(normalized) > 1000:
        raise CreativeValidationError("Feedbacktext darf höchstens 1000 Zeichen enthalten")
    return normalized


def normalize_traits(values: Mapping[str, Any] | None) -> dict[str, str | list[str]]:
    """Normalize user-derived traits into a small, non-executable vocabulary.

    Traits are data, never instructions.  Unknown free-form keys or nested
    structures are rejected so they cannot become a prompt-injection channel.
    """

    result: dict[str, str | list[str]] = {}
    for raw_key, raw_value in (values or {}).items():
        key = str(raw_key or "").strip().casefold()
        if not _KEY_RE.fullmatch(key):
            raise CreativeValidationError("Ungültiger Merkmalsname")
        if isinstance(raw_value, bool):
            result[key] = "ja" if raw_value else "nein"
            continue
        if isinstance(raw_value, (str, int, float)):
            value = str(raw_value).strip()
            if not _VALUE_RE.fullmatch(value):
                raise CreativeValidationError("Ungültiger Merkmalswert")
            result[key] = value
            continue
        if isinstance(raw_value, Sequence) and not isinstance(raw_value, (bytes, bytearray)):
            items: list[str] = []
            for raw_item in raw_value:
                item = str(raw_item).strip()
                if not _VALUE_RE.fullmatch(item):
                    raise CreativeValidationError("Ungültiger Merkmalswert")
                if item not in items:
                    items.append(item)
            result[key] = items[:20]
            continue
        raise CreativeValidationError("Verschachtelte Kreativmerkmale sind nicht erlaubt")
    return result
