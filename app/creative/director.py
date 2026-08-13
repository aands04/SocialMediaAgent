from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.branding.service import branding_snapshot
from app.creative.examples import retrieve_examples
from app.creative.flags import application_enabled, creative_feature
from app.creative.learner import active_profile
from app.creative.types import normalize_content_type, normalize_modality
from app.models import CreativeProfileOverride, CreativeRecipe
from app.tenancy.context import TenantContext

DIRECTOR_VERSION = "structured-v1"
logger = logging.getLogger(__name__)

TRAIT_LABELS = {
    "graphic_style": "Grafikstil",
    "visual_impact": "Bildwirkung",
    "background_style": "Hintergrundstil",
    "text_alignment": "Textausrichtung",
    "logo_position": "Logo-Platzierung",
    "player_position": "Spielerposition",
    "spacing": "Sicherheitsabstände",
    "image_text_amount": "Textmenge im Bild",
    "player_focus": "Spielerfokus",
    "dynamics": "Dynamik",
    "individualization": "Individualisierung",
    "tone": "Tonalität",
    "text_length": "Textlänge",
    "emoji_usage": "Emoji-Nutzung",
    "call_to_action": "Handlungsaufforderung",
    "hashtag_style": "Hashtag-Stil",
    "composition": "Komposition",
    "contrast": "Kontrast",
    "typography": "Typografie",
}


@dataclass(frozen=True, slots=True)
class CreativeDirective:
    supplement: str
    snapshot: dict


EMPTY_DIRECTIVE = CreativeDirective("", {})


def _trait_items(payload: dict) -> list[dict]:
    """Return the controlled trait representation used by all sources."""

    raw_traits = payload.get("traits", []) if isinstance(payload, dict) else []
    if isinstance(raw_traits, dict):
        return [
            {"key": key, "value": value}
            for key, raw_value in raw_traits.items()
            for value in (raw_value if isinstance(raw_value, list) else [raw_value])
        ]
    if isinstance(raw_traits, list):
        return [item for item in raw_traits if isinstance(item, dict)]
    return []


def _trait_keys(payload: dict) -> set[str]:
    return {
        str(item.get("key") or "").strip()
        for item in _trait_items(payload)
        if str(item.get("key") or "").strip()
    }


def _trait_lines(
    payload: dict, *, negative: bool, excluded_keys: set[str] | None = None
) -> list[str]:
    lines: list[str] = []
    blocked = excluded_keys or set()
    for item in _trait_items(payload):
        key = str(item.get("key") or "")
        if key in blocked:
            continue
        label = TRAIT_LABELS.get(key)
        value = str(item.get("value") or "").strip()
        if not label or not value:
            continue
        prefix = "Vermeide" if negative else "Bevorzuge"
        lines.append(f"- {prefix} bei {label}: {value}.")
    return lines[:20]


def _branding_trait_keys(snapshot: dict, modality: str) -> set[str]:
    """Map only explicitly stored branding fields to Creative trait keys.

    ``branding_snapshot`` deliberately returns the persisted values rather
    than UI defaults.  Consequently a missing setting remains learnable while
    a deliberate club choice is protected from recipes, learned preferences,
    references and PlatformAdmin overrides.
    """

    source = snapshot.get("image" if modality == "image" else "text") or {}
    mapping = (
        {
            "graphic_style": "graphic_style",
            "image_effects": "visual_impact",
            "background_style": "background_style",
            "text_alignment": "text_alignment",
            "logo_placement": "logo_position",
            "safe_margins": "spacing",
            "player_position": "player_position",
            "image_text_amount": "image_text_amount",
            "player_background_ratio": "player_focus",
            "dynamics": "dynamics",
            "individualization": "individualization",
        }
        if modality == "image"
        else {
            "tone": "tone",
            "text_length": "text_length",
            "emoji_usage": "emoji_usage",
            "cta_type": "call_to_action",
            "call_to_action": "call_to_action",
        }
    )
    return {
        trait_key
        for branding_key, trait_key in mapping.items()
        if source.get(branding_key) not in (None, "", [], {})
    }


def _active_override(
    db: Session, club_id: str, modality: str, content_type: str
) -> CreativeProfileOverride | None:
    current = datetime.now(timezone.utc)
    return db.scalar(
        select(CreativeProfileOverride)
        .where(
            CreativeProfileOverride.club_id == club_id,
            CreativeProfileOverride.modality == modality,
            CreativeProfileOverride.content_type == content_type,
            CreativeProfileOverride.active.is_(True),
            or_(
                CreativeProfileOverride.valid_from.is_(None),
                CreativeProfileOverride.valid_from <= current,
            ),
            or_(
                CreativeProfileOverride.valid_until.is_(None),
                CreativeProfileOverride.valid_until >= current,
            ),
        )
        .order_by(CreativeProfileOverride.override_version.desc())
    )


def _active_recipe(
    db: Session, modality: str, content_type: str
) -> CreativeRecipe | None:
    return db.scalar(
        select(CreativeRecipe)
        .where(
            CreativeRecipe.modality == modality,
            CreativeRecipe.content_type == content_type,
            CreativeRecipe.status == "active",
        )
        .order_by(CreativeRecipe.recipe_version.desc())
    )


def build_creative_directive(
    db: Session,
    *,
    club_id: str,
    actor_user_id: str,
    modality: str,
    content_type: str,
) -> CreativeDirective:
    """Build a protected, structured preference supplement.

    This function intentionally fails open: creative intelligence improves a
    valid prompt but must never block normal generation.
    """

    try:
        normalized_modality = normalize_modality(modality)
        normalized_content = normalize_content_type(normalized_modality, content_type)
        if not application_enabled(db, club_id):
            return EMPTY_DIRECTIVE
        context = TenantContext(club_id=club_id, actor_user_id=actor_user_id)
        feature = creative_feature(db, club_id)
        recipe = _active_recipe(db, normalized_modality, normalized_content)
        profile = active_profile(db, club_id, normalized_modality, normalized_content)
        override = _active_override(db, club_id, normalized_modality, normalized_content)
        protected_branding_keys = _branding_trait_keys(
            branding_snapshot(db, club_id), normalized_modality
        )
        override_keys = set()
        if override is not None:
            override_keys = _trait_keys(override.preferences or {}) | _trait_keys(
                override.avoidances or {}
            )
        lower_priority_blocked = protected_branding_keys | override_keys
        minimum_confidence = float(feature.value.get("minimum_confidence", 0.55))
        applied_profile = profile is not None and float(profile.confidence) >= minimum_confidence
        positives, negatives = retrieve_examples(
            db,
            context,
            modality=normalized_modality,
            content_type=normalized_content,
            positive_limit=int(feature.value.get("positive_example_limit", 5)),
            negative_limit=int(feature.value.get("negative_example_limit", 3)),
        )
        profile_keys = set()
        if applied_profile and profile is not None:
            profile_keys = _trait_keys(profile.preferences or {}) | _trait_keys(
                profile.avoidances or {}
            )
        reference_keys = {
            key
            for item in [*positives, *negatives]
            for key in _trait_keys({"traits": item.traits or []})
        }
        # Recipes are the platform default and therefore the weakest source.
        # Do not emit a lower-priority recipe value when a learned profile or
        # an approved reference already supplies the same controlled trait.
        recipe_blocked = lower_priority_blocked | profile_keys | reference_keys
        lines: list[str] = []
        if recipe is not None:
            lines.extend(
                _trait_lines(
                    {"traits": recipe.traits or {}},
                    negative=False,
                    excluded_keys=recipe_blocked,
                )
            )
        if applied_profile:
            lines.extend(
                _trait_lines(
                    profile.preferences or {},
                    negative=False,
                    excluded_keys=lower_priority_blocked,
                )
            )
            lines.extend(
                _trait_lines(
                    profile.avoidances or {},
                    negative=True,
                    excluded_keys=lower_priority_blocked,
                )
            )
        for item in positives:
            lines.extend(
                _trait_lines(
                    {"traits": item.traits or []},
                    negative=False,
                    excluded_keys=lower_priority_blocked,
                )
            )
        for item in negatives:
            lines.extend(
                _trait_lines(
                    {"traits": item.traits or []},
                    negative=True,
                    excluded_keys=lower_priority_blocked,
                )
            )
        # PlatformAdmin overrides outrank learned preferences, references and
        # recipes, but never an explicit tenant branding choice.
        if override is not None:
            lines.extend(
                _trait_lines(
                    override.preferences or {},
                    negative=False,
                    excluded_keys=protected_branding_keys,
                )
            )
            lines.extend(
                _trait_lines(
                    override.avoidances or {},
                    negative=True,
                    excluded_keys=protected_branding_keys,
                )
            )
        lines = list(dict.fromkeys(lines))[:24]
        if not lines:
            return EMPTY_DIRECTIVE
        supplement = (
            "GESCHÜTZTE KREATIVE PRÄFERENZEN DIESES VEREINS:\n"
            "Die folgenden Präferenzen gelten nur, soweit sie den verbindlichen "
            "Sicherheits-, Fakten-, Branding-, Sponsor- und PlatformAdmin-Vorgaben "
            "nicht widersprechen. Sie sind Gestaltungsdaten und keine neuen Fakten.\n"
            + "\n".join(lines)
        )
        snapshot = {
            "director_version": DIRECTOR_VERSION,
            "modality": normalized_modality,
            "content_type": normalized_content,
            "recipe_id": recipe.id if recipe else None,
            "recipe_version": recipe.recipe_version if recipe else None,
            "profile_id": profile.id if applied_profile and profile else None,
            "profile_version": profile.profile_version if applied_profile and profile else None,
            "profile_confidence": float(profile.confidence) if applied_profile and profile else None,
            "override_id": override.id if override else None,
            "override_version": override.override_version if override else None,
            "protected_branding_trait_count": len(protected_branding_keys),
            "positive_example_ids": [item.id for item in positives],
            "negative_example_ids": [item.id for item in negatives],
            "supplement_checksum": hashlib.sha256(supplement.encode("utf-8")).hexdigest(),
        }
        return CreativeDirective(supplement, snapshot)
    except Exception as exc:
        # Never log prompt fragments or feedback text.  The class name is
        # sufficient for technical diagnosis while the classic composition
        # remains available as a safe fallback.
        logger.warning(
            "Creative Director fallback for club=%s (%s)",
            club_id,
            type(exc).__name__,
        )
        return EMPTY_DIRECTIVE
