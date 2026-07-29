from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from openai import OpenAI


@dataclass
class GeneratedText: text:str; model:str; prompt_version:str="de-facts-v1"; tokens:int|None=None
class TextGenerator:
    def generate(self,data:dict)->GeneratedText: raise NotImplementedError
class FixtureTextGenerator(TextGenerator):
    def generate(self,data:dict)->GeneratedText:
        kickoff=datetime.fromisoformat(data["kickoff"]) if isinstance(data["kickoff"],str) else data["kickoff"]
        local=kickoff.astimezone(ZoneInfo("Europe/Berlin"))
        when=f"am {local:%d.%m.%Y} um {local:%H:%M} Uhr"
        if data.get("score") is not None:
            result=f"Endstand: {data['home_team']} {data['score']} {data['away_team']}."
        else:
            result=f"{data['home_team']} trifft {when} auf {data['away_team']}."
        if data.get("venue"): result+=f" Spielort: {data['venue']}."
        return GeneratedText(result+" "+" ".join(data.get("hashtags",[])),"fixture")
class OpenAITextGenerator(TextGenerator):
    def __init__(self,key:str,model:str): self.client=OpenAI(api_key=key); self.model=model
    def generate(self,data:dict)->GeneratedText:
        prompt="Erstelle einen deutschen Instagram-Text ausschließlich aus diesen Fakten. Keine weiteren Fakten: "+repr(data)
        response=self.client.responses.create(model=self.model,input=prompt)
        return GeneratedText(response.output_text,self.model,tokens=getattr(getattr(response,"usage",None),"total_tokens",None))
