"""Experimental local photo composition; the provider only paints the background."""

import hashlib
import re
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps

from app.imagegen.service import (
    REFERENCE_IMAGE_MAX_PIXELS,
    AIImageRenderer,
    ImageGenerationError,
    _fit_full_bleed,
    _provider_prompt,
)
from app.prompts.service import venue_display

POLICY = "original-photo-montage-v1"

HTML = """<main class="canvas">
<header>
  <div class="competition montage-fit">{{ competition }}</div>
  <div class="title montage-fit">{{ montage_title }}</div>
  <div class="side montage-fit">{{ side_label }}</div>
  <div class="logos"><img src="{{ team_logo }}" alt="Mannschaftslogo">
  {% if opponent_logo %}<img src="{{ opponent_logo }}" alt="Gegnerlogo">{% endif %}</div>
</header>
<section class="details">
  <div class="fixture montage-fit"><strong>{{ home_team }}</strong><span>gegen</span><strong>{{ away_team }}</strong></div>
  <div class="when montage-fit">{{ date_de }} · {{ time_de }} Uhr</div>
  <div class="venue montage-fit">{{ venue }}</div>
</section>
<footer class="{% if not montage_sponsors %}empty{% endif %}">{% for sponsor in montage_sponsors %}<img src="{{ sponsor }}" alt="Sponsor">{% endfor %}</footer>
</main>"""

CSS = """
*{box-sizing:border-box}html,body{margin:0;width:100%;height:100%;overflow:hidden}
body{font-family:var(--primary-font),Arial,sans-serif;color:#fff;background:transparent}
.canvas{position:relative;width:100%;height:100%;overflow:hidden}
header{position:absolute;top:0;left:0;right:0;height:280px;padding:40px 64px;background:rgba(5,13,31,.65)}
.competition{width:670px;height:42px;font-size:28px;text-transform:uppercase;letter-spacing:2px}
.title{width:670px;height:102px;font-size:88px;line-height:1.1;font-weight:900}
.side{width:670px;height:42px;font-size:28px;font-weight:700}
.logos{position:absolute;right:64px;top:40px;width:130px;height:210px;display:flex;flex-direction:column;align-items:center;gap:12px}
.logos img{object-fit:contain;width:110px;height:98px}
.details{position:absolute;left:0;right:0;bottom:100px;height:320px;padding:22px 64px;background:rgba(5,13,31,.8)}
.fixture{height:175px;font-size:46px;line-height:1.12;overflow-wrap:anywhere}
.fixture strong,.fixture span{display:block}.fixture span{font-size:.5em;line-height:1.5;opacity:.7}
.when{height:48px;margin-top:10px;font-size:32px;font-weight:700}
.venue{height:40px;font-size:28px}
footer{position:absolute;bottom:0;left:0;right:0;height:100px;padding:16px 64px;display:flex;justify-content:center;gap:24px;background:#fff}
footer img{min-width:0;max-width:180px;flex:0 1 auto;height:68px;object-fit:contain}
footer.empty{background:transparent}
"""


def _color(value: object, fallback: str) -> str:
    return (
        str(value) if re.fullmatch(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?", str(value)) else fallback
    )


def background_prompt(data: dict) -> str:
    primary = _color(data.get("primary_color"), "#172554")
    secondary = _color(data.get("secondary_color"), "#ffffff")
    return (
        "Erzeuge ausschließlich eine abstrakte Hintergrundtextur für eine Fußballgrafik. "
        f"Hauptfarbe {primary}, dezente Akzente {secondary}. "
        "Edle Papierstruktur, weiche Lichtverläufe, dynamische diagonale Farbflächen. "
        "Keine Menschen, Gesichter, Körper, Silhouetten, Tiere, Buchstaben, Zahlen, "
        "Texte, Logos, Wappen, Sponsoren oder Wasserzeichen. "
        "Keine konkreten Spielstätten darstellen. Die Mitte ruhig und kontrastarm halten. "
        "Foto, Originalwappen und Spielinformationen werden erst danach lokal eingefügt."
    )


class PhotoMontageRenderer(AIImageRenderer):
    @staticmethod
    def provider_prompt(data: dict) -> str:
        kind = getattr(data.get("image_prompt"), "media_kind", "feed")
        return _provider_prompt(background_prompt(data), 0, AIImageRenderer.api_sizes[kind])

    def render(self, kind: str, target: str, data: dict) -> Path:
        if kind not in self.sizes:
            raise ImageGenerationError("Unbekanntes Bildformat")
        if data.get("targeted_edit_source"):
            raise ImageGenerationError("Fotomontagen werden ausschließlich aus Originalen erstellt")
        prompt = data.get("image_prompt")
        if not prompt:
            raise ImageGenerationError("Gerenderter KI-Bildprompt fehlt")
        player = self._player_reference(data.get("player_image"))
        team_logo = self._logo_reference(data.get("team_logo"))
        if not player or not team_logo:
            raise ImageGenerationError("Fotomontage benötigt Originalfoto und Mannschaftslogo")
        opponent = self._logo_reference(data.get("opponent_logo"))
        if data.get("opponent_logo") and not opponent:
            raise ImageGenerationError("Das Gegnerlogo ist nicht verfügbar")
        sponsors = [self._sponsor_reference(item) for item in data.get("sponsor_references") or []]
        # Decode before any paid request, without the reference-upload downscale.
        with Image.open(player) as source:
            if source.width * source.height > REFERENCE_IMAGE_MAX_PIXELS:
                raise ImageGenerationError("Das Originalfoto überschreitet die sichere Pixelanzahl")
            photo = ImageOps.exif_transpose(source).convert("RGBA")
        width, height = self.sizes[kind]
        fitted = ImageOps.contain(photo, (width - 128, height - 740), Image.Resampling.LANCZOS)
        xy = ((width - fitted.width) // 2, 300 + (height - 740 - fitted.height) // 2)
        job_id = data.get("_generation_job_id")
        requested, out = self._output_path(target, job_id)
        metadata = {
            "final_path": str(out),
            "requested_path": str(requested),
            "generation_job_id": job_id,
            "image_creation_mode": "photo_montage",
            "photo_preservation": {
                "policy": POLICY,
                "source_checksum": hashlib.sha256(player.read_bytes()).hexdigest(),
                "source_media_asset_id": data.get("player_media_asset_id"),
                "method": "local-original-contain",
                "photo_box": [*xy, fitted.width, fitted.height],
                "provider_received_photo": False,
                "generative_processing_after_composition": False,
            },
            "logo_integration": {
                "mode": "local-original-montage",
                "version": POLICY,
                "manual_logo_review_required": True,
            },
        }
        reuse_id = data.get("_reuse_generation_job_id") or job_id
        if reuse_id:
            reused = self.reusable_output(target, reuse_id, kind)
            if reused:
                self._metadata[str(reused)] = {
                    **metadata,
                    "final_path": str(reused),
                    "reused_final": True,
                }
                return reused
        # Never overwrite an existing version outside the job-bound reuse path.
        if out.exists():
            raise ImageGenerationError("Die Fotomontage-Ausgabe existiert bereits")
        out.parent.mkdir(parents=True, exist_ok=True)
        temporary = out.with_name(f".{out.stem}.montage.png")
        try:
            # Validate layout, facts, fonts and logos before paying for a background.
            self.validator.render(
                kind,
                str(temporary.relative_to(self.root)),
                {
                    **data,
                    "player_image": None,
                    "primary_color": _color(data.get("primary_color"), "#172554"),
                    "secondary_color": "#ffffff",
                    "venue": venue_display(data),
                    "montage_title": (
                        f"ERGEBNIS {data.get('score') or ''}"
                        if data.get("post_type") == "result"
                        else "SPIELTAG"
                    ),
                    "_transparent_canvas": True,
                    "montage_sponsors": [self.validator._asset(path) for path in sponsors],
                    "template": {"html_template": HTML, "css": CSS},
                },
            )
            raw = self.provider.generate(
                prompt=background_prompt(data),
                references=[],
                size=self.api_sizes[kind],
                model=prompt.model,
                quality=prompt.quality,
            )
            with Image.open(BytesIO(raw)) as image:
                image.load()
                background = _fit_full_bleed(image, self.sizes[kind])
            # Apply the local text/logo layer first; the original photo is pasted
            # last in its reserved area and never receives a tint or overlay.
            with Image.open(temporary) as layout:
                final = Image.alpha_composite(background.convert("RGBA"), layout.convert("RGBA"))
            final.alpha_composite(fitted, xy)
            final.convert("RGB").save(temporary, "PNG", optimize=True)
            self.validate(temporary, kind)
            temporary.replace(out)
        finally:
            temporary.unlink(missing_ok=True)
        self._metadata[str(out)] = metadata
        return out
