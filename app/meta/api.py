from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from app.config import Settings
from app.meta.security import sanitize_platform_data
from app.meta.user_tags import UserTagValidationError, serialize_user_tags

REQUIRED_SCOPES = {
    "instagram_business_basic",
    "instagram_business_content_publish",
}


class MetaApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        uncertain: bool = False,
        response: dict | None = None,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.uncertain = uncertain
        self.response = sanitize_platform_data(response or {})


@dataclass
class OAuthToken:
    access_token: str
    user_id: str
    expires_in: int


class MetaApiClient:
    """Nur offizielle Instagram-Login-Hosts; HTTP ist vollständig mockbar."""

    authorization_endpoint = "https://www.instagram.com/oauth/authorize"
    token_endpoint = "https://api.instagram.com/oauth/access_token"
    graph_host = "https://graph.instagram.com"

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=settings.meta_http_timeout_seconds)
        self.base = f"{self.graph_host}/{settings.meta_graph_version}"

    def authorization_url(self, state: str, redirect_uri: str) -> str:
        if not self.settings.meta_app_id:
            raise MetaApiError("META_APP_ID-Secret fehlt")
        query = urlencode(
            {
                "client_id": self.settings.meta_app_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": ",".join(sorted(REQUIRED_SCOPES)),
                "state": state,
            }
        )
        return f"{self.authorization_endpoint}?{query}"

    def _json(self, response: httpx.Response, action: str) -> dict:
        try:
            data = response.json()
        except ValueError as exc:
            raise MetaApiError(f"{action}: ungültige Plattformantwort") from exc
        if response.is_error:
            retryable = response.status_code in {429, 500, 502, 503, 504}
            message = sanitize_platform_data(data).get("error", {})
            raise MetaApiError(
                f"{action} fehlgeschlagen: {message}",
                retryable=retryable,
                response=data,
            )
        return data

    def _request_json(
        self,
        method: str,
        url: str,
        action: str,
        *,
        uncertain_on_transport_error: bool = False,
        **kwargs,
    ) -> dict:
        try:
            response = self.client.request(method, url, **kwargs)
        except httpx.RequestError as exc:
            if uncertain_on_transport_error:
                raise MetaApiError(
                    f"{action}: Antwort nach möglicher Plattformannahme unklar; "
                    "der schreibende Aufruf wird nicht automatisch wiederholt",
                    uncertain=True,
                ) from exc
            raise MetaApiError(
                f"{action}: Meta ist vorübergehend nicht erreichbar",
                retryable=True,
            ) from exc
        if uncertain_on_transport_error and response.status_code >= 500:
            try:
                response_data = response.json()
            except ValueError:
                response_data = {}
            raise MetaApiError(
                f"{action}: Plattformfehler nach möglicher Annahme; "
                "der schreibende Aufruf wird nicht automatisch wiederholt",
                uncertain=True,
                response=response_data,
            )
        return self._json(response, action)

    def exchange_code(self, code: str, redirect_uri: str) -> OAuthToken:
        if not self.settings.meta_app_id or not self.settings.meta_app_secret:
            raise MetaApiError("Meta-App-ID oder App-Secret fehlt")
        data = self._request_json(
            "POST",
            self.token_endpoint,
            "OAuth-Codeaustausch",
            data={
                "client_id": self.settings.meta_app_id,
                "client_secret": self.settings.meta_app_secret,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
                "code": code,
            },
        )
        token = str(data.get("access_token") or "")
        user_id = str(data.get("user_id") or "")
        if not token or not user_id:
            raise MetaApiError("OAuth-Codeaustausch lieferte keinen Token oder Benutzer")
        return OAuthToken(token, user_id, int(data.get("expires_in") or 3600))

    def exchange_long_lived(self, token: OAuthToken) -> OAuthToken:
        if not self.settings.meta_app_secret:
            raise MetaApiError("Meta-App-Secret fehlt")
        data = self._request_json(
            "GET",
            f"{self.graph_host}/access_token",
            "Long-Lived-Tokenaustausch",
            params={
                "grant_type": "ig_exchange_token",
                "client_secret": self.settings.meta_app_secret,
                "access_token": token.access_token,
            },
        )
        return OAuthToken(
            str(data.get("access_token") or token.access_token),
            token.user_id,
            int(data.get("expires_in") or token.expires_in),
        )

    def refresh_token(self, access_token: str, user_id: str) -> OAuthToken:
        data = self._request_json(
            "GET",
            f"{self.graph_host}/refresh_access_token",
            "Tokenerneuerung",
            params={"grant_type": "ig_refresh_token", "access_token": access_token},
        )
        return OAuthToken(
            str(data.get("access_token") or access_token),
            user_id,
            int(data.get("expires_in") or 0),
        )

    def profile(self, access_token: str) -> dict:
        return self._request_json(
            "GET",
            f"{self.base}/me",
            "Kontoprüfung",
            params={"fields": "user_id,username,account_type"},
            headers={"Authorization": f"Bearer {access_token}"},
        )

    def create_container(
        self,
        *,
        access_token: str,
        account_id: str,
        kind: str,
        image_url: str,
        caption: str | None,
        user_tags: list[dict[str, float | str]] | None = None,
    ) -> dict:
        if not image_url.startswith("https://"):
            raise MetaApiError("Meta akzeptiert ausschließlich freigegebene HTTPS-Medien-URLs")
        payload = {"image_url": image_url}
        try:
            serialized_user_tags = serialize_user_tags(user_tags)
        except UserTagValidationError as exc:
            raise MetaApiError(str(exc)) from exc
        if kind == "story":
            if serialized_user_tags:
                raise MetaApiError(
                    "Positionsbezogene Instagram-Markierungen werden für Storys nicht unterstützt"
                )
            payload["media_type"] = "STORIES"
        elif kind == "feed" and caption:
            payload["caption"] = caption
        elif kind != "feed":
            raise MetaApiError("Nicht unterstützte Medienart")
        if kind == "feed" and serialized_user_tags:
            payload["user_tags"] = serialized_user_tags
        return self._request_json(
            "POST",
            f"{self.base}/{account_id}/media",
            "Containererstellung",
            uncertain_on_transport_error=True,
            data=payload,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    def container_status(self, *, access_token: str, container_id: str) -> dict:
        return self._request_json(
            "GET",
            f"{self.base}/{container_id}",
            "Containerstatus",
            params={"fields": "status_code,status"},
            headers={"Authorization": f"Bearer {access_token}"},
        )

    def create_carousel_item(
        self,
        *,
        access_token: str,
        account_id: str,
        image_url: str,
        user_tags: list[dict[str, float | str]] | None = None,
    ) -> dict:
        if not image_url.startswith("https://"):
            raise MetaApiError("Meta akzeptiert ausschließlich freigegebene HTTPS-Medien-URLs")
        payload = {"image_url": image_url, "is_carousel_item": "true"}
        try:
            serialized_user_tags = serialize_user_tags(user_tags)
        except UserTagValidationError as exc:
            raise MetaApiError(str(exc)) from exc
        if serialized_user_tags:
            payload["user_tags"] = serialized_user_tags
        return self._request_json(
            "POST",
            f"{self.base}/{account_id}/media",
            "Karussell-Element erstellen",
            uncertain_on_transport_error=True,
            data=payload,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    def create_carousel_container(
        self,
        *,
        access_token: str,
        account_id: str,
        child_ids: list[str],
        caption: str | None,
    ) -> dict:
        if not 2 <= len(child_ids) <= 10 or any(not child_id for child_id in child_ids):
            raise MetaApiError("Karussell benötigt 2 bis 10 gültige Child-Container")
        if len(set(child_ids)) != len(child_ids):
            raise MetaApiError("Karussell enthält doppelte Child-Container")
        payload = {"media_type": "CAROUSEL", "children": ",".join(child_ids)}
        if caption:
            payload["caption"] = caption
        return self._request_json(
            "POST",
            f"{self.base}/{account_id}/media",
            "Karussell-Container erstellen",
            uncertain_on_transport_error=True,
            data=payload,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    def publish_container(
        self, *, access_token: str, account_id: str, container_id: str
    ) -> dict:
        return self._request_json(
            "POST",
            f"{self.base}/{account_id}/media_publish",
            "Veröffentlichung",
            uncertain_on_transport_error=True,
            data={"creation_id": container_id},
            headers={"Authorization": f"Bearer {access_token}"},
        )

    def media_details(self, *, access_token: str, media_id: str) -> dict:
        return self._request_json(
            "GET",
            f"{self.base}/{media_id}",
            "Medienabgleich",
            params={"fields": "id,permalink"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
