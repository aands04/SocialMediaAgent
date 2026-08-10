from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from openai import OpenAI

OPENAI_TEXT_MAX_OUTPUT_TOKENS = 1600

INTERNAL_OUTPUT_MARKERS = (
    "VERBINDLICHE, SERVERSEITIG VALIDIERTE VEREINSTEXTREGELN:",
    "GESCHÜTZTE VEREINSANPASSUNG DES PLATFORMADMINS:",
    "VERBINDLICHE FAKTENREGELN:",
)


def caption_contains_internal_rules(value: str | None) -> bool:
    folded = str(value or "").casefold()
    return any(marker.casefold() in folded for marker in INTERNAL_OUTPUT_MARKERS)


def sanitize_generated_caption(value: str) -> str:
    """Return only the user-facing caption and remove echoed internal rules.

    Provider output is untrusted.  Even though the prompt explicitly requests
    only the final caption, a model can echo protected server-side policy
    sections.  Those sections must never become tenant-visible post content.
    """

    text = str(value or "").strip()
    folded = text.casefold()
    cut_at = len(text)
    for marker in INTERNAL_OUTPUT_MARKERS:
        index = folded.find(marker.casefold())
        if index >= 0:
            cut_at = min(cut_at, index)
    text = text[:cut_at].rstrip(" \t\r\n-:")
    if not text:
        raise ValueError(
            "Der KI-Dienst hat keinen verwendbaren öffentlichen Begleittext geliefert."
        )
    return text


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
        # Do not hide repeated provider requests inside the SDK.  This class
        # can use the independently supported Chat Completions endpoint when
        # Responses fails before returning usable text; the persistent job
        # layer remains responsible for the one delayed retry after that.
        self.client = OpenAI(api_key=key, max_retries=0)
        self.model = model

    @staticmethod
    def _provider_status_code(exc: Exception) -> int | None:
        status_code = getattr(exc, "status_code", None)
        if not isinstance(status_code, int):
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
        return status_code if isinstance(status_code, int) else None

    @classmethod
    def _can_use_transport_fallback(cls, exc: Exception) -> bool:
        status_code = cls._provider_status_code(exc)
        if status_code is not None:
            return 500 <= status_code <= 599
        name = type(exc).__name__.casefold()
        if name in {"internalservererror", "apiconnectionerror", "apitimeouterror"}:
            return True
        text = str(exc).casefold()
        return any(
            marker in text
            for marker in (
                "connection",
                "timed out",
                "timeout",
                "incomplete chunked read",
                "peer closed",
                "error code: 500",
                "error code: 502",
                "error code: 503",
                "error code: 504",
                "error code: 520",
            )
        )

    @staticmethod
    def _incomplete_reason(response) -> str | None:
        details = getattr(response, "incomplete_details", None)
        reason = getattr(details, "reason", None)
        return str(reason) if reason else None

    @staticmethod
    def _usage_tokens(response) -> int | None:
        return getattr(getattr(response, "usage", None), "total_tokens", None)

    def _chat_completion(self, rendered: str, model: str) -> tuple[str, int | None]:
        options = {
            "model": model,
            "messages": [{"role": "user", "content": rendered}],
            "max_completion_tokens": OPENAI_TEXT_MAX_OUTPUT_TOKENS,
        }
        if model.startswith("gpt-5"):
            options["reasoning_effort"] = "low"
            options["verbosity"] = "low"
        completion = self.client.chat.completions.create(**options)
        choices = list(getattr(completion, "choices", None) or [])
        content = (
            getattr(getattr(choices[0], "message", None), "content", None) if choices else None
        )
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Der KI-Dienst hat keinen verwendbaren Begleittext geliefert.")
        return content, self._usage_tokens(completion)

    def _request_text(self, rendered: str, model: str) -> tuple[str, int | None]:
        options = {
            "model": model,
            "input": rendered,
            "max_output_tokens": OPENAI_TEXT_MAX_OUTPUT_TOKENS,
        }
        if model.startswith("gpt-5"):
            options["reasoning"] = {"effort": "low"}
            options["text"] = {"verbosity": "low"}
        try:
            response = self.client.responses.create(**options)
        except Exception as exc:
            if not self._can_use_transport_fallback(exc):
                raise
            return self._chat_completion(rendered, model)

        status = str(getattr(response, "status", "") or "").casefold()
        output_text = getattr(response, "output_text", None)
        if (not status or status == "completed") and isinstance(output_text, str):
            if output_text.strip():
                return output_text, self._usage_tokens(response)
        if status == "incomplete" and self._incomplete_reason(response) == "max_output_tokens":
            return self._chat_completion(rendered, model)
        if not output_text:
            return self._chat_completion(rendered, model)
        raise ValueError("Der KI-Dienst hat die Texterstellung nicht vollständig abgeschlossen.")

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
        output_text, tokens = self._request_text(rendered, model)
        return GeneratedText(
            sanitize_generated_caption(output_text),
            model,
            prompt_version=version,
            tokens=tokens,
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
        output_text, tokens = self._request_text(rendered, model)
        return GeneratedText(
            sanitize_generated_caption(output_text),
            model,
            prompt_version=version,
            tokens=tokens,
            rendered_prompt=rendered,
        )
