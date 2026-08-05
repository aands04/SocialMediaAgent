from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.config import Settings
from app.meta.api import MetaApiClient
from app.meta.security import TokenCipher
from app.models import InstagramConnection


@dataclass
class PublishResult:
    confirmed: bool
    platform_id: str | None = None
    permalink: str | None = None
    uncertain: bool = False


class PublishError(RuntimeError):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class SocialMediaPublisher(ABC):
    @abstractmethod
    def publish(
        self,
        *,
        account_id: str,
        kind: str,
        media_url: str,
        caption: str | None,
        idempotency_key: str,
    ) -> PublishResult: ...


class DryRunPublisher(SocialMediaPublisher):
    def publish(self, **kwargs) -> PublishResult:
        return PublishResult(True, "dry-run:" + kwargs["idempotency_key"])


class MockPublisher(DryRunPublisher):
    pass


class InstagramPublisher(SocialMediaPublisher):
    """Fassade für den persistenten Instagram-Login-Workflow.

    Der frühere Ein-Schritt-Aufruf ist gesperrt, weil Container- und Media-ID
    zwischen den offiziellen API-Schritten persistiert werden müssen.
    """

    def __init__(self, settings: Settings, api: MetaApiClient | None = None):
        self.settings = settings
        self.api = api or MetaApiClient(settings)

    def token_for(self, connection: InstagramConnection) -> str:
        return TokenCipher(self.settings.meta_token_encryption_key).decrypt(
            connection.encrypted_token
        )

    def create_container(
        self,
        *,
        connection: InstagramConnection,
        kind: str,
        media_url: str,
        caption: str | None,
    ) -> dict:
        return self.api.create_container(
            access_token=self.token_for(connection),
            account_id=connection.instagram_user_id or "",
            kind=kind,
            image_url=media_url,
            caption=caption,
        )

    def container_status(self, *, connection: InstagramConnection, container_id: str) -> dict:
        return self.api.container_status(
            access_token=self.token_for(connection), container_id=container_id
        )

    def publish_container(self, *, connection: InstagramConnection, container_id: str) -> dict:
        return self.api.publish_container(
            access_token=self.token_for(connection),
            account_id=connection.instagram_user_id or "",
            container_id=container_id,
        )

    def publish(self, **kwargs) -> PublishResult:
        raise PublishError(
            "Instagram-Live-Publishing erfordert den persistenten Meta-Testassistenten"
        )
