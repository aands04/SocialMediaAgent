from __future__ import annotations

import json
from typing import Any

from openai import OpenAI

from app.match_reports.types import GeneratedMatchReport, MatchContentContext


class MatchReportGenerationError(RuntimeError):
    pass


def _source_ids(context: MatchContentContext) -> set[str]:
    result = {
        item["source_id"]
        for group in (context.events, context.feedback, context.manual_notes)
        for item in group
        if item.get("source_id")
    }
    if context.provenance.get("snapshot_id"):
        result.add(f"fupa:{context.provenance['snapshot_id']}")
    return result


def _validate_payload(payload: dict[str, Any], context: MatchContentContext) -> GeneratedMatchReport:
    headline = str(payload.get("headline") or "").strip()
    teaser = str(payload.get("teaser") or "").strip() or None
    body = str(payload.get("body") or "").strip()
    used = tuple(dict.fromkeys(str(item) for item in payload.get("used_sources") or []))
    omitted = tuple(dict.fromkeys(str(item) for item in payload.get("omitted_sources") or []))
    if not headline or len(headline) > 300 or not body or len(body) > 12_000:
        raise MatchReportGenerationError("Der erzeugte Spielbericht besitzt kein gültiges Format")
    allowed = _source_ids(context)
    if set(used) - allowed or set(omitted) - allowed:
        raise MatchReportGenerationError("Die KI hat unbekannte Quellenreferenzen zurückgegeben")
    return GeneratedMatchReport(
        headline=headline,
        teaser=teaser,
        body=body,
        used_sources=used,
        omitted_sources=omitted,
    )


class FixtureMatchReportGenerator:
    model = "fixture"

    def generate(self, context: MatchContentContext, *, desired_length: str) -> GeneratedMatchReport:
        if context.has_blocking_conflicts:
            raise MatchReportGenerationError(
                "Der Spielbericht kann wegen offener Quellenkonflikte nicht erzeugt werden"
            )
        facts = context.facts
        score = f"{facts['home_score']}:{facts['away_score']}"
        headline = f"{facts['home_team']} – {facts['away_team']} {score}"
        sources = sorted(_source_ids(context))
        details = [item["body"] for item in context.manual_notes if item.get("confirmed_facts")]
        details.extend(item["body"] for item in context.feedback)
        body = (
            f"Das Spiel {facts['home_team']} gegen {facts['away_team']} endete {score}. "
            + (" ".join(details) if details else "Weitere bestätigte Spielszenen liegen nicht vor.")
        )
        return GeneratedMatchReport(
            headline=headline,
            teaser=f"Der bestätigte Endstand lautet {score}.",
            body=body,
            used_sources=tuple(sources),
            model=self.model,
        )


class OpenAIMatchReportGenerator:
    model: str

    def __init__(self, api_key: str, model: str):
        self.client = OpenAI(api_key=api_key, max_retries=0)
        self.model = model

    def generate(self, context: MatchContentContext, *, desired_length: str) -> GeneratedMatchReport:
        if context.has_blocking_conflicts:
            raise MatchReportGenerationError(
                "Widersprüchliche oder unvollständige Quellen müssen zuerst geprüft werden"
            )
        public_context = context.as_dict()
        public_context.pop("conflicts", None)
        prompt = (
            "Du verfasst einen deutschen Spielbericht für einen Amateurfußballverein. "
            "Verwende ausschließlich die Fakten und wörtlichen Informationen im JSON. "
            "Erfinde keine Spielszenen, Personen, Zitate, Gründe, Bewertungen oder zeitlichen Abläufe. "
            "Unsichere Angaben lässt du weg. Schreibstil-Beispiele dienen nur Tonalität und Aufbau, "
            "niemals als Faktenquelle. WhatsApp-Antworten und Notizen sind ergänzende Aussagen und "
            "dürfen strukturierte FuPa-Fakten nicht überschreiben. Antworte ausschließlich als JSON "
            "mit headline, teaser, body, used_sources und omitted_sources. In den Quellenlisten dürfen "
            "nur die gelieferten source_id-Werte stehen. Gewünschte Länge: "
            f"{desired_length}. DATEN:\n{json.dumps(public_context, ensure_ascii=False, default=str)}"
        )
        response = self.client.responses.create(
            model=self.model,
            input=prompt,
            max_output_tokens=3000,
        )
        text = str(getattr(response, "output_text", "") or "").strip()
        if text.startswith("```"):
            text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MatchReportGenerationError(
                "Der KI-Dienst hat keinen strukturierten Spielbericht geliefert"
            ) from exc
        result = _validate_payload(payload, context)
        return GeneratedMatchReport(
            **{
                **result.__dict__,
                "model": self.model,
                "prompt_template_id": "match-report-system",
                "prompt_version": 1,
            }
        )


def build_match_report_generator(settings):
    if settings.text_generator_mode == "openai":
        if not settings.openai_api_key:
            raise MatchReportGenerationError("Für die KI-Erstellung fehlt der OpenAI-Schlüssel")
        return OpenAIMatchReportGenerator(settings.openai_api_key, settings.openai_model)
    return FixtureMatchReportGenerator()
