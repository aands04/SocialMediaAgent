from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BillingResult:
    customer_id: str | None
    subscription_id: str | None
    status: str
    metadata: dict


class BillingProvider(ABC):
    """Small boundary that a future Stripe provider can implement."""

    @abstractmethod
    def create_subscription(self, *, club_id: str, plan_id: str) -> BillingResult: ...

    @abstractmethod
    def change_plan(self, *, subscription_id: str, plan_id: str) -> BillingResult: ...

    @abstractmethod
    def cancel_subscription(self, *, subscription_id: str) -> BillingResult: ...

    @abstractmethod
    def reconcile(self, *, subscription_id: str) -> BillingResult: ...


class MockBillingProvider(BillingProvider):
    """Deterministic test provider. It performs no network or payment operation."""

    def create_subscription(self, *, club_id: str, plan_id: str) -> BillingResult:
        return BillingResult(
            customer_id=f"mock-club-{club_id}",
            subscription_id=f"mock-subscription-{club_id}-{plan_id}",
            status="trialing",
            metadata={"provider": "mock"},
        )

    def change_plan(self, *, subscription_id: str, plan_id: str) -> BillingResult:
        return BillingResult(None, subscription_id, "active", {"plan_id": plan_id})

    def cancel_subscription(self, *, subscription_id: str) -> BillingResult:
        return BillingResult(None, subscription_id, "cancelled", {})

    def reconcile(self, *, subscription_id: str) -> BillingResult:
        return BillingResult(None, subscription_id, "active", {})
