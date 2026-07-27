import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

import httpx
from bs4 import BeautifulSoup


@dataclass(frozen=True)
class GameRecord:
    external_id:str; home_team:str; away_team:str; kickoff:datetime; competition:str|None=None; venue:str|None=None; pitch:str|None=None; status:str="scheduled"; home_score:int|None=None; away_score:int|None=None; halftime:str|None=None
class ProviderError(RuntimeError): pass
class GameDataProvider(ABC):
    @abstractmethod
    def fetch(self,url:str)->list[GameRecord]: ...
class FussballDeProvider(GameDataProvider):
    def __init__(self,timeout:float=10): self.timeout=timeout
    def fetch(self,url:str)->list[GameRecord]:
        if not url.startswith(("https://www.fussball.de/","https://fussball.de/")): raise ProviderError("Ungültige FUSSBALL.DE-URL")
        try:
            r=httpx.get(url,timeout=self.timeout,follow_redirects=True,headers={"User-Agent":"Vereins-SocialMediaBot/1.0 (kontakt@example.invalid)"}); r.raise_for_status()
        except httpx.HTTPError as e: raise ProviderError(f"FUSSBALL.DE nicht erreichbar: {e}") from e
        return self.parse(r.text)
    def parse(self,html:str)->list[GameRecord]:
        soup=BeautifulSoup(html,"html.parser"); records=[]
        for node in soup.select("[data-game-id], .fixture"):
            try:
                eid=node.get("data-game-id") or hashlib.sha256(node.get_text(" ",strip=True).encode()).hexdigest()[:24]
                home=node.select_one(".home, .club-home").get_text(" ",strip=True); away=node.select_one(".away, .club-away").get_text(" ",strip=True)
                raw=node.get("data-kickoff") or node.select_one("time").get("datetime")
                kickoff=datetime.fromisoformat(raw.replace("Z","+00:00"))
                score=node.select_one(".score"); hs=aws=None
                if score and ":" in score.get_text(): hs,aws=(int(x.strip()) for x in score.get_text().split(":",1))
                records.append(GameRecord(eid,home,away,kickoff,venue=(node.select_one(".venue").get_text(strip=True) if node.select_one(".venue") else None),status=node.get("data-status","scheduled"),home_score=hs,away_score=aws))
            except (AttributeError,ValueError,TypeError) as e: raise ProviderError("HTML-Struktur unbekannt oder widersprüchlich") from e
        if not records: raise ProviderError("Keine Spiele erkannt; Provider-Selektoren prüfen")
        return records
