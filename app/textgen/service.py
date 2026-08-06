from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from openai import OpenAI


@dataclass
class GeneratedText:
    text: str
    model: str
    prompt_version: str = "de-facts-v1"
    tokens: int | None = None
    rendered_prompt: str | None = None


class TextGenerator:
    is_ai = False

    def generate(self, data: dict) -> GeneratedText:
        raise NotImplementedError

    def revise(self, data: dict, current_text: str, instruction: str) -> GeneratedText:
        raise NotImplementedError


class FixtureTextGenerator(TextGenerator):
    def generate(self, data: dict) -> GeneratedText:
        kickoff = (
            datetime.fromisoformat(data["kickoff"])
            if isinstance(data["kickoff"], str)
            else data["kickoff"]
        )
        local = kickoff.astimezone(ZoneInfo("Europe/Berlin"))
        when = f"am {local:%d.%m.%Y} um {local:%H:%M} Uhr"
        if data.get("score") is not None:
            result = f"Endstand: {data['home_team']} {data['score']} {data['away_team']}."
        else:
            result = f"{data['home_team']} trifft {when} auf {data['away_team']}."
        venue = data.get("venue_display") or data.get("home_venue_display") or data.get("venue")
        if venue:
            result += f" Spielort: {venue}."
        return GeneratedText(result + " " + " ".join(data.get("hashtags", [])), "fixture")

    def revise(self, data: dict, current_text: str, instruction: str) -> GeneratedText:
        return GeneratedText(
            current_text.rstrip() + f"\n\n[Fixture-Änderungswunsch: {instruction.strip()}]",
            "fixture",
            prompt_version="revision-fixture-v1",
        )


class OpenAITextGenerator(TextGenerator):
    is_ai = True

    def __init__(self, key: str, model: str):
        self.client = OpenAI(api_key=key)
        self.model = model

    def prepare_generate(self, data: dict) -> tuple[str, str, str]:
        prompt = data.get("text_prompt")
        if hasattr(prompt, "rendered"):
            rendered = prompt.rendered
            version = f"{prompt.name}:v{prompt.version}"
            model = prompt.model or self.model
        else:
            rendered = (
                "Erstelle einen deutschen Instagram-Text ausschließlich aus diesen Fakten. Keine weiteren Fakten: "
                + repr(data)
            )
            version = "de-facts-v1"
            model = self.model
        return rendered, version, model

    def generate(self, data: dict) -> GeneratedText:
        rendered, version, model = self.prepare_generate(data)
        response = self.client.responses.create(model=model, input=rendered)
        return GeneratedText(
            response.output_text,
            model,
            prompt_version=version,
            tokens=getattr(getattr(response, "usage", None), "total_tokens", None),
            rendered_prompt=rendered,
        )

    def prepare_revision(
        self, data: dict, current_text: str, instruction: str
    ) -> tuple[str, str, str]:
        allowed_facts = {
            key: data.get(key)
            for key in (
                "home_team",
                "away_team",
                "own_team",
                "kickoff",
                "venue",
                "venue_display",
                "home_venue_display",
                "pitch",
                "competition",
                "post_type",
                "hashtags",
                "side_label",
                "score",
            )
            if data.get(key) not in (None, "")
        }
        rendered = (
            "VERBINDLICHE REGELN:\n"
            "- Überarbeite den vorhandenen deutschen Instagram-Begleittext gemäß "
            "dem Änderungsauftrag.\n"
            "- Verwende ausschließlich die angegebenen Fakten und den vorhandenen Text.\n"
            "- Erfinde keinen Spielverlauf, keine Personen, Ergebnisse, Zitate oder sonstigen Fakten.\n"
            "- Mannschaftsnamen, Datum, Uhrzeit, Wettbewerb und Spielort dürfen nicht verfälscht werden.\n"
            "- Gib ausschließlich den vollständigen, direkt kopierbaren neuen Begleittext aus.\n\n"
            f"FAKTEN:\n{allowed_facts!r}\n\n"
            f"VORHANDENER TEXT:\n{current_text}\n\n"
            f"ÄNDERUNGSAUFTRAG:\n{instruction.strip()}"
        )
        return rendered, "ai-revision-v1", self.model

    def revise(self, data: dict, current_text: str, instruction: str) -> GeneratedText:
        rendered, version, model = self.prepare_revision(data, current_text, instruction)
        response = self.client.responses.create(model=model, input=rendered)
        return GeneratedText(
            response.output_text,
            model,
            prompt_version=version,
            tokens=getattr(getattr(response, "usage", None), "total_tokens", None),
            rendered_prompt=rendered,
        )
