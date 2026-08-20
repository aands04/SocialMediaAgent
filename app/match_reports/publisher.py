from __future__ import annotations

from dataclasses import dataclass

from app.match_reports.types import MatchContentContext
from app.models import MatchReportVersion


@dataclass(frozen=True)
class FupaPublishResult:
    status: str
    external_id: str | None = None
    external_url: str | None = None
    message: str | None = None
    updated_storage_state: str | None = None


class FupaPublisher:
    """Explicit provider boundary for a future supported FuPa write integration."""

    automatic_supported = False

    def publish(
        self,
        *,
        context: MatchContentContext,
        version: MatchReportVersion,
        idempotency_key: str,
    ) -> FupaPublishResult:
        raise NotImplementedError


class ManualFupaPublisher(FupaPublisher):
    def publish(
        self,
        *,
        context: MatchContentContext,
        version: MatchReportVersion,
        idempotency_key: str,
    ) -> FupaPublishResult:
        return FupaPublishResult(
            status="manual_required",
            external_url=context.facts.get("source_url"),
            message=(
                "FuPa stellt in dieser Installation keinen stabilen, offiziell unterstützten "
                "Schreibzugang bereit. Der freigegebene Text kann kontrolliert zu FuPa übertragen werden."
            ),
        )
