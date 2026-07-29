from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx


@dataclass
class PublishResult: confirmed:bool; platform_id:str|None=None; permalink:str|None=None; uncertain:bool=False
class PublishError(RuntimeError):
    def __init__(self,message:str,retryable:bool=False): super().__init__(message); self.retryable=retryable
class SocialMediaPublisher(ABC):
    @abstractmethod
    def publish(self,*,account_id:str,kind:str,media_url:str,caption:str|None,idempotency_key:str)->PublishResult: ...
class DryRunPublisher(SocialMediaPublisher):
    def publish(self,**kwargs)->PublishResult: return PublishResult(True,"dry-run:"+kwargs["idempotency_key"])
class MockPublisher(DryRunPublisher): pass
class InstagramPublisher(SocialMediaPublisher):
    """Offizielle Instagram Graph API: Container anlegen, Status prüfen, media_publish."""
    def __init__(self,token:str,graph_version:str="v23.0",timeout:float=20): self.token=token; self.base=f"https://graph.facebook.com/{graph_version}"; self.timeout=timeout
    def publish(self,*,account_id:str,kind:str,media_url:str,caption:str|None,idempotency_key:str)->PublishResult:
        field="image_url"; payload={field:media_url,"access_token":self.token}
        if kind=="story": payload["media_type"]="STORIES"
        elif caption: payload["caption"]=caption
        try:
            created=httpx.post(f"{self.base}/{account_id}/media",data=payload,timeout=self.timeout); created.raise_for_status(); creation_id=created.json()["id"]
            status=httpx.get(f"{self.base}/{creation_id}",params={"fields":"status_code","access_token":self.token},timeout=self.timeout); status.raise_for_status()
            if status.json().get("status_code")!="FINISHED": return PublishResult(False,creation_id,uncertain=True)
            published=httpx.post(f"{self.base}/{account_id}/media_publish",data={"creation_id":creation_id,"access_token":self.token},timeout=self.timeout); published.raise_for_status()
            return PublishResult(True,published.json()["id"])
        except httpx.TimeoutException as e: raise PublishError("Plattformantwort unklar; vor Wiederholung Status prüfen") from e
        except httpx.HTTPStatusError as e: raise PublishError(f"Meta API: {e.response.text}",e.response.status_code in {429,500,502,503,504}) from e
