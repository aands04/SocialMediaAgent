from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.creative.types import normalize_content_type, normalize_modality, normalize_traits
from app.models import (
    CreativeExampleReference,
    GeneratedMediaVersion,
    PostTextVersion,
    uid,
)
from app.tenancy.context import TenantContext, TenantContextError, tenant_get


def retrieve_examples(
    db: Session,
    context: TenantContext,
    *,
    modality: str,
    content_type: str,
    positive_limit: int = 5,
    negative_limit: int = 3,
) -> tuple[list[CreativeExampleReference], list[CreativeExampleReference]]:
    normalized_modality = normalize_modality(modality)
    normalized_content = normalize_content_type(normalized_modality, content_type)
    base = select(CreativeExampleReference).where(
        CreativeExampleReference.club_id == context.club_id,
        CreativeExampleReference.modality == normalized_modality,
        CreativeExampleReference.content_type == normalized_content,
        CreativeExampleReference.active.is_(True),
    )
    positives = list(
        db.scalars(
            base.where(CreativeExampleReference.sentiment == "positive")
            .order_by(
                CreativeExampleReference.rank,
                CreativeExampleReference.score.desc(),
                CreativeExampleReference.created_at.desc(),
            )
            .limit(max(0, min(positive_limit, 5)))
        )
    )
    negatives = list(
        db.scalars(
            base.where(CreativeExampleReference.sentiment == "negative")
            .order_by(
                CreativeExampleReference.rank,
                CreativeExampleReference.score.desc(),
                CreativeExampleReference.created_at.desc(),
            )
            .limit(max(0, min(negative_limit, 3)))
        )
    )
    return positives, negatives


def add_example(
    db: Session,
    context: TenantContext,
    *,
    modality: str,
    content_type: str,
    sentiment: str,
    media_version_id: str | None = None,
    text_version_id: str | None = None,
    profile_id: str | None = None,
    rank: int = 0,
    traits: dict | None = None,
    score: float = 0,
) -> CreativeExampleReference:
    normalized_modality = normalize_modality(modality)
    normalized_content = normalize_content_type(normalized_modality, content_type)
    normalized_sentiment = str(sentiment).casefold()
    if normalized_sentiment not in {"positive", "negative"}:
        raise ValueError("Beispielwertung muss positiv oder negativ sein")
    if normalized_modality == "image":
        if not media_version_id or text_version_id:
            raise ValueError("Bildbeispiel benötigt genau eine Bildversion")
        if tenant_get(db, GeneratedMediaVersion, media_version_id, context) is None:
            raise TenantContextError("Bildbeispiel gehört nicht zum aktuellen Verein")
    else:
        if not text_version_id or media_version_id:
            raise ValueError("Textbeispiel benötigt genau eine Textversion")
        if tenant_get(db, PostTextVersion, text_version_id, context) is None:
            raise TenantContextError("Textbeispiel gehört nicht zum aktuellen Verein")
    item = CreativeExampleReference(
        id=uid(),
        club_id=context.club_id,
        profile_id=profile_id,
        modality=normalized_modality,
        content_type=normalized_content,
        sentiment=normalized_sentiment,
        media_version_id=media_version_id,
        text_version_id=text_version_id,
        rank=max(0, int(rank)),
        traits=normalize_traits(traits),
        score=float(score),
        active=True,
        created_by=context.actor_user_id,
    )
    db.add(item)
    db.flush()
    return item
