from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.models import Club, ClubBrandingConfiguration


class BrandingValidationError(ValueError):
    pass


HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
HASHTAG = re.compile(r"^[\wäöüÄÖÜß]{1,50}$", re.UNICODE)
MENTION = re.compile(r"^[A-Za-z0-9._]{1,30}$")
DATE_VALUE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

GRAPHIC_STYLES = {
    "classic",
    "modern",
    "dynamic",
    "minimal",
    "emotional",
    "stadium",
}
IMAGE_EFFECTS = {"emotional", "modern", "dynamic", "calm", "premium", "classic"}
BACKGROUND_STYLES = {"club-color", "gradient", "photo", "stadium", "abstract", "cutout"}
ALIGNMENTS = {"left", "center", "right"}
POSITIONS = {
    "top-left",
    "top-center",
    "top-right",
    "center-left",
    "center",
    "center-right",
    "bottom-left",
    "bottom-center",
    "bottom-right",
}
SAFE_MARGINS = {"tight", "normal", "generous"}
TEXT_AMOUNTS = {"little", "normal", "detailed"}
DYNAMICS = {"calm", "balanced", "dynamic"}
INDIVIDUALIZATION = {"standard", "club", "strong"}
ADDRESS_STYLES = {"du", "ihr", "neutral"}
TONES = {"factual", "emotional", "motivating", "casual", "professional", "traditional"}
TEXT_LENGTHS = {"short", "medium", "detailed"}
EMOJI_USAGE = {"none", "sparse", "normal", "frequent"}
CTA_TYPES = {"support", "share", "comment", "attend", "none", "custom"}
SPONSOR_PLACEMENTS = {"auto", "top", "bottom", "left", "right", "footer"}

# Browser- und Chromium-taugliche Standardschriften. Vereinsbenutzer wählen
# ausschließlich einen stabilen Schlüssel; die serverseitig kontrollierte
# Font-Familie wird nie als beliebiger CSS-Wert aus einem Formular übernommen.
STANDARD_FONTS = {
    "system": {
        "label": "Systemschrift",
        "family": "system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
    },
    "dejavu-sans": {"label": "DejaVu Sans", "family": "'DejaVu Sans', Arial, sans-serif"},
    "dejavu-serif": {"label": "DejaVu Serif", "family": "'DejaVu Serif', Georgia, serif"},
    "dejavu-mono": {
        "label": "DejaVu Sans Mono",
        "family": "'DejaVu Sans Mono', 'Courier New', monospace",
    },
    "liberation-sans": {
        "label": "Liberation Sans",
        "family": "'Liberation Sans', Arial, sans-serif",
    },
    "liberation-serif": {
        "label": "Liberation Serif",
        "family": "'Liberation Serif', Georgia, serif",
    },
    "liberation-mono": {
        "label": "Liberation Mono",
        "family": "'Liberation Mono', 'Courier New', monospace",
    },
}

FEED_SETTING_KEYS = {
    "max_text_amount",
    "use_player_image",
    "show_sponsors",
    "show_club_logo",
    "highlight_result",
    "extra_rules",
}
STORY_SETTING_KEYS = {
    "safe_top",
    "safe_bottom",
    "use_player_image",
    "show_sponsors",
    "show_club_logo",
    "show_call_to_action",
    "countdown_area",
    "extra_rules",
}
TEAM_NAME_KEYS = {"team_id", "display_name", "short_name", "active"}
SPONSOR_KEYS = {
    "name",
    "media_asset_id",
    "instagram_mention",
    "use_feed",
    "use_story",
    "use_announcement",
    "use_result",
    "team_ids",
    "placement",
    "valid_from",
    "valid_until",
    "required",
}

SAFE_KEYS = {
    "primary_color",
    "secondary_color",
    "accent_colors",
    "graphic_style",
    "image_effect",
    "image_effects",
    "background_style",
    "text_alignment",
    "logo_placement",
    "safe_margins",
    "player_position",
    "allowed_elements",
    "unwanted_elements",
    "sponsor_rules",
    "forbidden_colors",
    "feed_rules",
    "story_rules",
    "feed_settings",
    "story_settings",
    "image_text_amount",
    "player_background_ratio",
    "dynamics",
    "individualization",
    "primary_standard_font",
    "secondary_standard_font",
    "address_style",
    "tone",
    "text_length",
    "emoji_usage",
    "hashtags",
    "mentions",
    "typical_phrases",
    "unwanted_phrases",
    "team_name_spelling",
    "team_names",
    "home_label",
    "away_label",
    "home_venue",
    "home_venue_short",
    "call_to_action",
    "cta_type",
    "cta_custom",
    "sponsors",
    "sponsor_mentions",
    "max_hashtags",
    "legacy_values",
}
INJECTION_MARKERS = (
    "ignore previous",
    "ignore all",
    "system prompt",
    "developer message",
    "überschreibe die anweisung",
    "ignoriere vorherige",
    "jailbreak",
    "<script",
    "javascript:",
)

DEFAULT_IMAGE_SETTINGS = {
    "primary_color": "#172554",
    "secondary_color": "#FFFFFF",
    "accent_colors": [],
    "graphic_style": "modern",
    "image_effects": ["modern"],
    "background_style": "gradient",
    "text_alignment": "left",
    "logo_placement": "top-left",
    "safe_margins": "normal",
    "player_position": "center-right",
    "allowed_elements": [],
    "unwanted_elements": [],
    "sponsor_rules": [],
    "forbidden_colors": [],
    "feed_rules": "",
    "story_rules": "",
    "feed_settings": {
        "max_text_amount": "normal",
        "use_player_image": True,
        "show_sponsors": True,
        "show_club_logo": True,
        "highlight_result": True,
        "extra_rules": "",
    },
    "story_settings": {
        "safe_top": 12,
        "safe_bottom": 15,
        "use_player_image": True,
        "show_sponsors": True,
        "show_club_logo": True,
        "show_call_to_action": True,
        "countdown_area": False,
        "extra_rules": "",
    },
    "image_text_amount": "normal",
    "player_background_ratio": 60,
    "dynamics": "balanced",
    "individualization": "club",
    "primary_standard_font": "system",
    "secondary_standard_font": "system",
    "legacy_values": {},
}

DEFAULT_TEXT_SETTINGS = {
    "address_style": "ihr",
    "tone": "emotional",
    "text_length": "medium",
    "emoji_usage": "sparse",
    "hashtags": [],
    "mentions": [],
    "typical_phrases": [],
    "unwanted_phrases": [],
    "team_name_spelling": "",
    "team_names": [],
    "home_label": "Heimspiel",
    "away_label": "Auswärtsspiel",
    "home_venue": "",
    "home_venue_short": "",
    "call_to_action": "",
    "cta_type": "support",
    "cta_custom": "",
    "sponsors": [],
    "sponsor_mentions": [],
    "max_hashtags": 10,
    "legacy_values": {},
}


def _text_limit(key: str) -> int:
    return 1200 if key in {"feed_rules", "story_rules", "extra_rules"} else 500


def _validate_text(key: str, value: str) -> str:
    value = value.strip()
    if len(value) > _text_limit(key):
        raise BrandingValidationError(f"Branding-Wert {key} ist zu lang")
    lowered = value.casefold()
    if any(marker in lowered for marker in INJECTION_MARKERS):
        raise BrandingValidationError(
            f"Branding-Wert {key} enthält eine unzulässige Steueranweisung"
        )
    if any(char in value for char in ("<", ">", "\x00")):
        raise BrandingValidationError(f"Branding-Wert {key} enthält unzulässige Zeichen")
    return value


def _validate_color(key: str, value: str) -> str:
    value = _validate_text(key, value)
    if value and not HEX_COLOR.fullmatch(value):
        raise BrandingValidationError(f"Farbwert {key} ist ungültig")
    return value.upper()


def _validate_choice(key: str, value: Any, choices: set[str], strict: bool) -> Any:
    value = _validate_text(key, str(value)) if value is not None else ""
    if strict and value not in choices:
        raise BrandingValidationError(f"Auswahl für {key} ist ungültig")
    return value


def _validate_feed_settings(value: Any, strict: bool) -> dict:
    if not isinstance(value, dict):
        raise BrandingValidationError("Feed-Einstellungen müssen strukturiert sein")
    unknown = set(value) - FEED_SETTING_KEYS
    if unknown:
        raise BrandingValidationError("Unbekannte Feed-Einstellungen: " + ", ".join(sorted(unknown)))
    result = {}
    for key, item in value.items():
        if key == "max_text_amount":
            result[key] = _validate_choice(key, item, TEXT_AMOUNTS, strict)
        elif key == "extra_rules":
            result[key] = _validate_text(key, str(item or ""))
        elif isinstance(item, bool):
            result[key] = item
        else:
            raise BrandingValidationError(f"Feed-Einstellung {key} ist ungültig")
    return result


def _validate_story_settings(value: Any, strict: bool) -> dict:
    if not isinstance(value, dict):
        raise BrandingValidationError("Story-Einstellungen müssen strukturiert sein")
    unknown = set(value) - STORY_SETTING_KEYS
    if unknown:
        raise BrandingValidationError(
            "Unbekannte Story-Einstellungen: " + ", ".join(sorted(unknown))
        )
    result = {}
    for key, item in value.items():
        if key in {"safe_top", "safe_bottom"}:
            try:
                numeric = int(item)
            except (TypeError, ValueError) as exc:
                raise BrandingValidationError(f"Story-Sicherheitsbereich {key} ist ungültig") from exc
            if not 0 <= numeric <= 35:
                raise BrandingValidationError(
                    "Story-Sicherheitsbereiche müssen zwischen 0 und 35 Prozent liegen"
                )
            result[key] = numeric
        elif key == "extra_rules":
            result[key] = _validate_text(key, str(item or ""))
        elif isinstance(item, bool):
            result[key] = item
        else:
            raise BrandingValidationError(f"Story-Einstellung {key} ist ungültig")
    return result


def normalize_hashtags(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw or "").strip().lstrip("#").replace(" ", "")
        if not value:
            continue
        if not HASHTAG.fullmatch(value):
            raise BrandingValidationError(f"Hashtag #{value} ist ungültig")
        normalized = f"#{value}"
        marker = normalized.casefold()
        if marker not in seen:
            result.append(normalized)
            seen.add(marker)
    return result


def normalize_mentions(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw or "").strip().lstrip("@").casefold()
        if not value:
            continue
        if not MENTION.fullmatch(value):
            raise BrandingValidationError(f"Instagram-Erwähnung @{value} ist ungültig")
        normalized = f"@{value}"
        if normalized not in seen:
            result.append(normalized)
            seen.add(normalized)
    return result


def normalize_colors(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for raw in values:
        color = _validate_color("Farbe", str(raw or ""))
        if color and color not in result:
            result.append(color)
    return result


def normalize_string_list(values: Iterable[str], *, maximum: int = 20) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = _validate_text("Listeneintrag", str(raw or ""))
        marker = value.casefold()
        if value and marker not in seen:
            result.append(value)
            seen.add(marker)
        if len(result) > maximum:
            raise BrandingValidationError("Zu viele Listeneinträge")
    return result


def _validate_team_names(value: Any) -> list[dict]:
    if not isinstance(value, list) or len(value) > 100:
        raise BrandingValidationError("Mannschaftsschreibweisen sind ungültig")
    result = []
    seen = set()
    for entry in value:
        if not isinstance(entry, dict) or set(entry) - TEAM_NAME_KEYS:
            raise BrandingValidationError("Mannschaftsschreibweise ist nicht strukturiert")
        team_id = _validate_text("team_id", str(entry.get("team_id") or ""))
        if not team_id or team_id in seen:
            raise BrandingValidationError("Mannschaft wurde mehrfach oder ohne ID übermittelt")
        result.append(
            {
                "team_id": team_id,
                "display_name": _validate_text(
                    "display_name", str(entry.get("display_name") or "")
                ),
                "short_name": _validate_text("short_name", str(entry.get("short_name") or "")),
                "active": bool(entry.get("active", True)),
            }
        )
        if result[-1]["active"] and not result[-1]["display_name"]:
            raise BrandingValidationError(
                "Eine aktive Mannschaft benötigt einen Anzeigenamen"
            )
        seen.add(team_id)
    return result


def _validate_sponsors(value: Any) -> list[dict]:
    if not isinstance(value, list) or len(value) > 30:
        raise BrandingValidationError("Sponsorenliste ist ungültig")
    result = []
    for entry in value:
        if not isinstance(entry, dict) or set(entry) - SPONSOR_KEYS:
            raise BrandingValidationError("Sponsor ist nicht strukturiert")
        name = _validate_text("Sponsor", str(entry.get("name") or ""))
        if not name:
            raise BrandingValidationError("Sponsor benötigt einen Namen")
        mention = normalize_mentions([entry.get("instagram_mention") or ""])
        placement = _validate_choice(
            "Sponsorplatzierung", entry.get("placement") or "auto", SPONSOR_PLACEMENTS, True
        )
        valid_from = _validate_text("valid_from", str(entry.get("valid_from") or ""))
        valid_until = _validate_text("valid_until", str(entry.get("valid_until") or ""))
        if valid_from and not DATE_VALUE.fullmatch(valid_from):
            raise BrandingValidationError("Beginn der Sponsorengültigkeit ist ungültig")
        if valid_until and not DATE_VALUE.fullmatch(valid_until):
            raise BrandingValidationError("Ende der Sponsorengültigkeit ist ungültig")
        if valid_from and valid_until and valid_from > valid_until:
            raise BrandingValidationError("Sponsorengültigkeit endet vor ihrem Beginn")
        team_ids = normalize_string_list(entry.get("team_ids") or [], maximum=100)
        result.append(
            {
                "name": name,
                "media_asset_id": _validate_text(
                    "media_asset_id", str(entry.get("media_asset_id") or "")
                ),
                "instagram_mention": mention[0] if mention else "",
                "use_feed": bool(entry.get("use_feed", True)),
                "use_story": bool(entry.get("use_story", True)),
                "use_announcement": bool(entry.get("use_announcement", True)),
                "use_result": bool(entry.get("use_result", True)),
                "team_ids": team_ids,
                "placement": placement,
                "valid_from": valid_from,
                "valid_until": valid_until,
                "required": bool(entry.get("required", False)),
            }
        )
    return result


def _validate_legacy(value: Any) -> dict:
    if not isinstance(value, dict) or len(value) > 30:
        raise BrandingValidationError("Übernommene Altwerte sind ungültig")
    result = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key)
        if not re.fullmatch(r"[a-z0-9_]{1,80}", key):
            raise BrandingValidationError("Schlüssel eines Altwerts ist ungültig")
        if isinstance(raw_value, list):
            result[key] = normalize_string_list(raw_value)
        else:
            result[key] = _validate_text(key, str(raw_value or ""))
    return result


def validate_branding_settings(settings: dict | None, *, strict_choices: bool = False) -> dict:
    if not settings:
        return {}
    if not isinstance(settings, dict):
        raise BrandingValidationError("Branding-Konfiguration muss strukturiert sein")
    unknown = set(settings) - SAFE_KEYS
    if unknown:
        raise BrandingValidationError(
            "Unbekannte Branding-Felder: " + ", ".join(sorted(unknown))
        )
    result: dict[str, Any] = {}
    choice_fields = {
        "graphic_style": GRAPHIC_STYLES,
        "background_style": BACKGROUND_STYLES,
        "text_alignment": ALIGNMENTS,
        "logo_placement": POSITIONS,
        "safe_margins": SAFE_MARGINS,
        "player_position": POSITIONS,
        "image_text_amount": TEXT_AMOUNTS,
        "dynamics": DYNAMICS,
        "individualization": INDIVIDUALIZATION,
        "address_style": ADDRESS_STYLES,
        "tone": TONES,
        "text_length": TEXT_LENGTHS,
        "emoji_usage": EMOJI_USAGE,
        "cta_type": CTA_TYPES,
        "primary_standard_font": set(STANDARD_FONTS),
        "secondary_standard_font": set(STANDARD_FONTS),
    }
    for key, value in settings.items():
        if key in {"primary_color", "secondary_color"}:
            result[key] = _validate_color(key, str(value or ""))
            if strict_choices and not result[key]:
                raise BrandingValidationError(f"Farbwert {key} ist erforderlich")
        elif key in {"accent_colors", "forbidden_colors"}:
            if not isinstance(value, list):
                raise BrandingValidationError(f"{key} muss eine Farbliste sein")
            result[key] = normalize_colors(value)
        elif key == "image_effects":
            if not isinstance(value, list):
                raise BrandingValidationError("Bildwirkung muss eine Auswahl sein")
            selected = normalize_string_list(value)
            if strict_choices and set(selected) - IMAGE_EFFECTS:
                raise BrandingValidationError("Mindestens eine Bildwirkung ist ungültig")
            result[key] = selected
        elif key in {"hashtags"}:
            result[key] = normalize_hashtags(value if isinstance(value, list) else [value])
        elif key in {"mentions", "sponsor_mentions"}:
            result[key] = normalize_mentions(value if isinstance(value, list) else [value])
        elif key in {
            "allowed_elements",
            "unwanted_elements",
            "sponsor_rules",
            "typical_phrases",
            "unwanted_phrases",
        }:
            if not isinstance(value, list):
                raise BrandingValidationError(f"{key} muss eine Liste sein")
            result[key] = normalize_string_list(value)
        elif key == "feed_settings":
            result[key] = _validate_feed_settings(value, strict_choices)
        elif key == "story_settings":
            result[key] = _validate_story_settings(value, strict_choices)
        elif key == "team_names":
            result[key] = _validate_team_names(value)
        elif key == "sponsors":
            result[key] = _validate_sponsors(value)
        elif key == "legacy_values":
            result[key] = _validate_legacy(value)
        elif key == "max_hashtags":
            try:
                numeric = int(value)
            except (TypeError, ValueError) as exc:
                raise BrandingValidationError("Maximale Hashtag-Anzahl ist ungültig") from exc
            if not 0 <= numeric <= 30:
                raise BrandingValidationError(
                    "Maximale Hashtag-Anzahl muss zwischen 0 und 30 liegen"
                )
            result[key] = numeric
        elif key == "player_background_ratio":
            try:
                numeric = int(value)
            except (TypeError, ValueError) as exc:
                raise BrandingValidationError("Spieler-/Hintergrundverhältnis ist ungültig") from exc
            if not 0 <= numeric <= 100:
                raise BrandingValidationError(
                    "Spieler-/Hintergrundverhältnis muss zwischen 0 und 100 liegen"
                )
            result[key] = numeric
        elif key in choice_fields:
            result[key] = _validate_choice(key, value, choice_fields[key], strict_choices)
        elif isinstance(value, list):
            result[key] = normalize_string_list(value)
        elif value is None or isinstance(value, (bool, int, float)):
            result[key] = value
        else:
            result[key] = _validate_text(key, str(value))
    if strict_choices and result.get("cta_type") == "custom" and not result.get(
        "cta_custom"
    ):
        raise BrandingValidationError(
            "Für die eigene Handlungsaufforderung ist eine Formulierung erforderlich"
        )
    return result


def parse_structured_json(value: str, label: str) -> Any:
    if len(value or "") > 50_000:
        raise BrandingValidationError(f"{label} ist zu umfangreich")
    try:
        return json.loads(value or "[]")
    except json.JSONDecodeError as exc:
        raise BrandingValidationError(f"{label} konnte nicht gelesen werden") from exc


def _legacy_choice(view: dict, key: str, choices: set[str], fallback: str) -> None:
    value = str(view.get(key) or "").strip()
    if value and value not in choices:
        view.setdefault("legacy_values", {})[key] = value
        view[key] = fallback


def branding_form_state(image: dict | None, text: dict | None) -> tuple[dict, dict]:
    raw_image = validate_branding_settings(image or {})
    raw_text = validate_branding_settings(text or {})
    image_view = deepcopy(DEFAULT_IMAGE_SETTINGS)
    image_view.update(raw_image)
    image_view["feed_settings"] = {
        **DEFAULT_IMAGE_SETTINGS["feed_settings"],
        **(raw_image.get("feed_settings") or {}),
    }
    image_view["story_settings"] = {
        **DEFAULT_IMAGE_SETTINGS["story_settings"],
        **(raw_image.get("story_settings") or {}),
    }
    if raw_image.get("feed_rules") and not image_view["feed_settings"].get("extra_rules"):
        image_view["feed_settings"]["extra_rules"] = raw_image["feed_rules"]
    if raw_image.get("story_rules") and not image_view["story_settings"].get("extra_rules"):
        image_view["story_settings"]["extra_rules"] = raw_image["story_rules"]
    if not raw_image.get("image_effects") and raw_image.get("image_effect"):
        tokens = re.split(r"[,;/]|\bund\b", str(raw_image["image_effect"]), flags=re.I)
        recognized = [token.strip().casefold() for token in tokens]
        selected = [value for value in recognized if value in IMAGE_EFFECTS]
        if selected:
            image_view["image_effects"] = list(dict.fromkeys(selected))
        if set(recognized) - IMAGE_EFFECTS:
            image_view.setdefault("legacy_values", {})["image_effect"] = raw_image[
                "image_effect"
            ]
    for key, choices, fallback in (
        ("graphic_style", GRAPHIC_STYLES, "modern"),
        ("background_style", BACKGROUND_STYLES, "gradient"),
        ("text_alignment", ALIGNMENTS, "left"),
        ("logo_placement", POSITIONS, "top-left"),
        ("safe_margins", SAFE_MARGINS, "normal"),
        ("player_position", POSITIONS, "center-right"),
        ("image_text_amount", TEXT_AMOUNTS, "normal"),
        ("dynamics", DYNAMICS, "balanced"),
        ("individualization", INDIVIDUALIZATION, "club"),
    ):
        _legacy_choice(image_view, key, choices, fallback)

    text_view = deepcopy(DEFAULT_TEXT_SETTINGS)
    text_view.update(raw_text)
    for key, choices, fallback in (
        ("address_style", ADDRESS_STYLES, "ihr"),
        ("tone", TONES, "emotional"),
        ("text_length", TEXT_LENGTHS, "medium"),
        ("emoji_usage", EMOJI_USAGE, "sparse"),
        ("cta_type", CTA_TYPES, "support"),
    ):
        _legacy_choice(text_view, key, choices, fallback)
    if raw_text.get("call_to_action") and not raw_text.get("cta_type"):
        text_view["cta_type"] = "custom"
        text_view["cta_custom"] = raw_text["call_to_action"]
    if raw_text.get("team_name_spelling"):
        text_view.setdefault("legacy_values", {})["team_name_spelling"] = raw_text[
            "team_name_spelling"
        ]
    return image_view, text_view


def recommended_branding_settings(
    image: dict | None = None, text: dict | None = None
) -> tuple[dict, dict]:
    current_image, current_text = branding_form_state(image, text)
    recommended_image = deepcopy(DEFAULT_IMAGE_SETTINGS)
    recommended_text = deepcopy(DEFAULT_TEXT_SETTINGS)
    recommended_image["accent_colors"] = current_image.get("accent_colors") or []
    recommended_image["allowed_elements"] = current_image.get("allowed_elements") or []
    recommended_image["unwanted_elements"] = current_image.get("unwanted_elements") or []
    recommended_image["sponsor_rules"] = current_image.get("sponsor_rules") or []
    recommended_image["forbidden_colors"] = current_image.get("forbidden_colors") or []
    recommended_image["legacy_values"] = current_image.get("legacy_values") or {}
    for key in (
        "hashtags",
        "mentions",
        "typical_phrases",
        "unwanted_phrases",
        "team_names",
        "home_venue",
        "home_venue_short",
        "sponsors",
        "sponsor_mentions",
        "legacy_values",
    ):
        recommended_text[key] = deepcopy(current_text.get(key) or recommended_text.get(key))
    return recommended_image, recommended_text


def default_branding_settings(
    image: dict | None = None, text: dict | None = None
) -> tuple[dict, dict]:
    current_image, current_text = branding_form_state(image, text)
    default_image = deepcopy(DEFAULT_IMAGE_SETTINGS)
    default_text = deepcopy(DEFAULT_TEXT_SETTINGS)
    default_image["legacy_values"] = current_image.get("legacy_values") or {}
    default_text["legacy_values"] = current_text.get("legacy_values") or {}
    default_text["team_names"] = current_text.get("team_names") or []
    default_text["sponsors"] = current_text.get("sponsors") or []
    return default_image, default_text


def branding_completion(
    *,
    club_name: str | None,
    has_logo: bool,
    primary_font_id: str | None,
    secondary_font_id: str | None,
    image: dict,
    text: dict,
) -> dict:
    checks = [
        (bool(str(club_name or "").strip()), "Vereinsname"),
        (has_logo, "Vereinslogo"),
        (bool(image.get("primary_color")), "Primärfarbe"),
        (bool(image.get("secondary_color")), "Sekundärfarbe"),
        (bool(image.get("graphic_style")), "Grafikstil"),
        (bool(image.get("logo_placement")), "Logo-Platzierung"),
        (bool(text.get("address_style")), "Ansprache"),
        (bool(text.get("tone")), "Tonalität"),
        (bool(image.get("accent_colors")), "Akzentfarbe"),
        (bool(primary_font_id or image.get("primary_standard_font")), "primäre Schriftart"),
        (bool(secondary_font_id or image.get("secondary_standard_font")), "sekundäre Schriftart"),
        (bool(image.get("background_style")), "Hintergrundstil"),
        (bool(image.get("safe_margins")), "Sicherheitsabstände"),
        (bool(text.get("home_venue")), "Standard-Heimspielstätte"),
        (bool(text.get("hashtags")), "Standard-Hashtags"),
    ]
    complete = sum(1 for passed, _label in checks if passed)
    missing = [label for passed, label in checks if not passed]
    return {
        "percent": round(complete * 100 / len(checks)),
        "missing": missing,
        "complete": complete,
        "total": len(checks),
    }


def dynamic_text_examples(
    *,
    club_name: str | None,
    club_short_name: str | None,
    venue: str | None,
    team_name: str | None,
    text: dict,
) -> dict:
    club = str(club_name or "").strip() or "Dein Verein"
    short = str(club_short_name or "").strip()
    team = str(team_name or "").strip() or short or club
    ground = str(venue or "").strip() or "eurer Heimspielstätte"
    tone = text.get("tone") or "factual"
    tone_examples = {
        "factual": f"Am Sonntag empfängt {team} den kommenden Gegner in {ground}.",
        "emotional": f"Heimspielzeit für {team} – gemeinsam alles geben!",
        "motivating": f"Unterstützt {team} am Sonntag und macht {ground} zur Festung!",
        "casual": f"Sonntag, Heimspiel, {ground}. Wir sehen uns!",
        "professional": f"{team} freut sich auf die nächste Begegnung in {ground}.",
        "traditional": f"Gemeinsam für {club}: Am Sonntag zählt jede Stimme in {ground}.",
    }
    address_examples = {
        "du": f"Komm vorbei und unterstütze {team}.",
        "ihr": f"Kommt vorbei und unterstützt {team}.",
        "neutral": f"Unterstützung für {team} ist herzlich willkommen.",
    }
    length_examples = {
        "short": f"Heimspiel für {team} in {ground}.",
        "medium": f"Am Sonntag steht für {team} das nächste Heimspiel in {ground} an.",
        "detailed": (
            f"Am Sonntag bestreitet {team} das nächste Heimspiel in {ground}. "
            "Alle Vereinsmitglieder und Fans sind herzlich eingeladen, die Mannschaft zu unterstützen."
        ),
    }
    cta_examples = {
        "support": f"Unterstützt {team} vor Ort!",
        "share": "Teilt den Beitrag mit euren Freunden!",
        "comment": "Schreibt euren Tipp in die Kommentare!",
        "attend": f"Kommt zum Spiel nach {ground}!",
        "none": "Keine Handlungsaufforderung",
        "custom": str(text.get("cta_custom") or "Eigene Handlungsaufforderung"),
    }
    return {
        "tone": tone_examples.get(tone, tone_examples["factual"]),
        "address": address_examples.get(text.get("address_style"), address_examples["neutral"]),
        "length": length_examples.get(text.get("text_length"), length_examples["medium"]),
        "call_to_action": cta_examples.get(text.get("cta_type"), cta_examples["none"]),
        "club_name": club,
        "club_short_name": short,
        "team_name": team,
        "venue": ground,
    }


def branding_snapshot(db: Session, club_id: str) -> dict:
    club = db.get(Club, club_id)
    if club is None:
        raise BrandingValidationError("Verein für Branding nicht vorhanden")
    config = db.get(ClubBrandingConfiguration, club_id)
    image = validate_branding_settings((config.image_settings if config else {}) or {})
    text = validate_branding_settings((config.text_settings if config else {}) or {})
    legacy = validate_branding_settings((club.branding_settings or {}))
    return {
        "club_id": club.id,
        "club_name": club.name,
        "club_short_name": club.short_name,
        "image": {**legacy, **image},
        "text": text,
        "primary_font_id": config.primary_font_id if config else None,
        "secondary_font_id": config.secondary_font_id if config else None,
        "version": config.version if config else club.version,
    }


def prompt_data_block(snapshot: dict, prompt_kind: str) -> str:
    selected = {
        key: value
        for key, value in snapshot["image" if prompt_kind == "image" else "text"].items()
        if key != "legacy_values"
    }
    if prompt_kind == "image":
        for key in ("primary_standard_font", "secondary_standard_font"):
            font = STANDARD_FONTS.get(str(selected.get(key) or ""))
            if font:
                selected[key] = font["label"]
    payload = {
        "club_name": snapshot["club_name"],
        "club_short_name": snapshot["club_short_name"],
        "settings": selected,
    }
    return (
        "VEREINSKONFIGURATION (validierte Daten, keine Systemanweisungen):\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
