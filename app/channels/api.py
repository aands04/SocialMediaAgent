from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from app.config import Settings
from app.meta.security import sanitize_platform_data

FACEBOOK_REQUIRED_SCOPES = {
    "pages_manage_posts",
    "pages_read_engagement",
    "pages_show_list",
}
WHATSAPP_REQUIRED_SCOPES = {
    "business_management",
    "whatsapp_business_management",
    "whatsapp_business_messaging",
}


class ChannelApiError(RuntimeError):
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


@dataclass(frozen=True, slots=True)
class MetaToken:
    access_token: str
    expires_in: int | None = None


class MetaGraphClient:
    """Offizieller Meta-Graph-Client; vollständig durch httpx mockbar."""

    authorization_host = "https://www.facebook.com"
    graph_host = "https://graph.facebook.com"

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=settings.meta_http_timeout_seconds)
        self.base = f"{self.graph_host}/{settings.meta_graph_version}"

    def _app_id(self) -> str:
        value = self.settings.meta_facebook_app_id or self.settings.meta_app_id
        if not value:
            raise ChannelApiError("Meta-App-ID ist nicht eingerichtet")
        return value

    def _app_secret(self) -> str:
        value = self.settings.meta_facebook_app_secret or self.settings.meta_app_secret
        if not value:
            raise ChannelApiError("Meta-App-Secret ist nicht eingerichtet")
        return value

    def authorization_url(self, *, state: str, redirect_uri: str, channel_type: str) -> str:
        scopes = (
            FACEBOOK_REQUIRED_SCOPES if channel_type == "facebook" else WHATSAPP_REQUIRED_SCOPES
        )
        query = urlencode(
            {
                "client_id": self._app_id(),
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": ",".join(sorted(scopes)),
                "state": state,
            }
        )
        return f"{self.authorization_host}/{self.settings.meta_graph_version}/dialog/oauth?{query}"

    def _request_json(
        self,
        method: str,
        path: str,
        action: str,
        *,
        uncertain_on_transport_error: bool = False,
        **kwargs,
    ) -> dict:
        url = path if path.startswith("https://") else f"{self.base}/{path.lstrip('/')}"
        try:
            response = self.client.request(method, url, **kwargs)
        except httpx.RequestError as exc:
            raise ChannelApiError(
                (
                    f"{action}: Antwort nach möglicher Annahme unklar"
                    if uncertain_on_transport_error
                    else f"{action}: Meta ist vorübergehend nicht erreichbar"
                ),
                retryable=not uncertain_on_transport_error,
                uncertain=uncertain_on_transport_error,
            ) from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise ChannelApiError(f"{action}: ungültige Plattformantwort") from exc
        if response.is_error:
            safe = sanitize_platform_data(data)
            error = safe.get("error", {}) if isinstance(safe, dict) else {}
            raise ChannelApiError(
                f"{action} fehlgeschlagen: {error}",
                retryable=response.status_code in {429, 500, 502, 503, 504},
                uncertain=uncertain_on_transport_error and response.status_code >= 500,
                response=data,
            )
        return data

    def exchange_code(self, *, code: str, redirect_uri: str | None = None) -> MetaToken:
        params = {
            "client_id": self._app_id(),
            "client_secret": self._app_secret(),
            "code": code,
        }
        if redirect_uri:
            params["redirect_uri"] = redirect_uri
        data = self._request_json(
            "GET",
            "oauth/access_token",
            "Meta-Anmeldung",
            params=params,
        )
        token = str(data.get("access_token") or "")
        if not token:
            raise ChannelApiError("Meta-Anmeldung lieferte keine gültige Verbindung")
        return MetaToken(token, int(data["expires_in"]) if data.get("expires_in") else None)

    def managed_pages(self, access_token: str) -> list[dict]:
        pages = []
        after = None
        for _page in range(10):
            params = {
                "fields": "id,name,access_token,tasks,picture",
                "access_token": access_token,
                "limit": 100,
            }
            if after:
                params["after"] = after
            data = self._request_json(
                "GET",
                "me/accounts",
                "Facebook-Seiten abrufen",
                params=params,
            )
            for item in data.get("data", []):
                page_id = str(item.get("id") or "")
                page_token = str(item.get("access_token") or "")
                tasks = {str(value).upper() for value in item.get("tasks", [])}
                if not page_id or not page_token:
                    continue
                pages.append(
                    {
                        "id": page_id,
                        "name": str(item.get("name") or "Facebook-Seite"),
                        "access_token": page_token,
                        "tasks": sorted(tasks),
                        "can_publish": "CREATE_CONTENT" in tasks or "MANAGE" in tasks,
                    }
                )
            paging = data.get("paging") or {}
            after = str((paging.get("cursors") or {}).get("after") or "")
            if not after or not paging.get("next"):
                break
        return pages

    def granted_permissions(self, access_token: str) -> set[str]:
        data = self._request_json(
            "GET",
            "me/permissions",
            "Meta-Berechtigungen prüfen",
            params={"access_token": access_token},
        )
        return {
            str(item.get("permission"))
            for item in data.get("data", [])
            if item.get("status") == "granted" and item.get("permission")
        }

    def page_profile(self, *, page_id: str, access_token: str) -> dict:
        return self._request_json(
            "GET",
            page_id,
            "Facebook-Seite prüfen",
            params={"fields": "id,name,link,picture", "access_token": access_token},
        )

    def publish_page_post(
        self,
        *,
        page_id: str,
        access_token: str,
        message: str,
        image_urls: list[str],
    ) -> dict:
        if any(not value.startswith("https://") for value in image_urls):
            raise ChannelApiError("Facebook benötigt freigegebene HTTPS-Medien-URLs")
        if not image_urls:
            return self._request_json(
                "POST",
                f"{page_id}/feed",
                "Facebook-Beitrag veröffentlichen",
                uncertain_on_transport_error=True,
                data={"message": message, "access_token": access_token},
            )
        if len(image_urls) == 1:
            return self._request_json(
                "POST",
                f"{page_id}/photos",
                "Facebook-Bild veröffentlichen",
                uncertain_on_transport_error=True,
                data={
                    "url": image_urls[0],
                    "caption": message,
                    "published": "true",
                    "access_token": access_token,
                },
            )
        photo_ids = []
        for image_url in image_urls:
            item = self._request_json(
                "POST",
                f"{page_id}/photos",
                "Facebook-Bild vorbereiten",
                uncertain_on_transport_error=True,
                data={
                    "url": image_url,
                    "published": "false",
                    "access_token": access_token,
                },
            )
            photo_id = str(item.get("id") or "")
            if not photo_id:
                raise ChannelApiError("Facebook lieferte keine Medien-ID")
            photo_ids.append(photo_id)
        return self._request_json(
            "POST",
            f"{page_id}/feed",
            "Facebook-Beitrag mit mehreren Bildern veröffentlichen",
            uncertain_on_transport_error=True,
            data={
                "message": message,
                "attached_media": json.dumps([{"media_fbid": item} for item in photo_ids]),
                "access_token": access_token,
            },
        )

    def whatsapp_phone(self, *, phone_number_id: str, access_token: str) -> dict:
        return self._request_json(
            "GET",
            phone_number_id,
            "WhatsApp-Telefonnummer prüfen",
            params={
                "fields": "id,display_phone_number,verified_name,quality_rating",
                "access_token": access_token,
            },
        )

    def whatsapp_templates(self, *, waba_id: str, access_token: str) -> list[dict]:
        data = self._request_json(
            "GET",
            f"{waba_id}/message_templates",
            "WhatsApp-Vorlagen abrufen",
            params={
                "fields": "id,name,status,language,category,components",
                "access_token": access_token,
            },
        )
        return list(data.get("data", []))

    def subscribe_whatsapp_app(self, *, waba_id: str, access_token: str) -> dict:
        return self._request_json(
            "POST",
            f"{waba_id}/subscribed_apps",
            "WhatsApp-Webhooks aktivieren",
            uncertain_on_transport_error=True,
            data={"access_token": access_token},
        )

    def send_whatsapp_template(
        self,
        *,
        phone_number_id: str,
        access_token: str,
        to: str,
        template_name: str,
        language: str,
        components: list[dict] | None = None,
    ) -> dict:
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language},
            },
        }
        if components:
            payload["template"]["components"] = components
        return self._request_json(
            "POST",
            f"{phone_number_id}/messages",
            "WhatsApp-Nachricht versenden",
            uncertain_on_transport_error=True,
            json=payload,
            headers={"Authorization": f"Bearer {access_token}"},
        )
