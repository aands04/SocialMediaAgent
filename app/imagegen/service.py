import base64
import hashlib
from contextlib import ExitStack
from io import BytesIO
from pathlib import Path

from openai import OpenAI
from PIL import Image, ImageOps

from app.rendering.service import Renderer, RenderValidationError

LOGO_REFERENCE_VERSION = "verified-media-ai-references-v2"
OPENAI_IMAGE_OUTPUT_FORMAT = "webp"
OPENAI_IMAGE_OUTPUT_COMPRESSION = 60

REFERENCE_IMAGE_MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


class ImageGenerationError(RenderValidationError):
    pass


class ImageProvider:
    def generate(
        self,
        prompt: str,
        references: list[Path],
        size: str,
        model: str,
        quality: str,
    ) -> bytes:
        raise NotImplementedError


class OpenAIImageProvider(ImageProvider):
    def __init__(self, api_key: str):
        self.client = OpenAI(api_key=api_key)

    def generate(
        self,
        prompt: str,
        references: list[Path],
        size: str,
        model: str,
        quality: str,
    ) -> bytes:
        try:
            if references:
                with ExitStack() as stack:
                    files = []
                    for path in references:
                        mime_type = REFERENCE_IMAGE_MIME_TYPES.get(path.suffix.lower())
                        if not mime_type:
                            raise ImageGenerationError(
                                "Nicht unterstütztes Referenzbildformat: "
                                f"{path.suffix or 'keine Dateiendung'}"
                            )
                        file_handle = stack.enter_context(path.open("rb"))
                        files.append((path.name, file_handle, mime_type))
                    edit_options = {
                        "model": model,
                        "image": files,
                        "prompt": prompt,
                        "size": size,
                        "quality": quality,
                        "output_format": OPENAI_IMAGE_OUTPUT_FORMAT,
                        "output_compression": OPENAI_IMAGE_OUTPUT_COMPRESSION,
                    }
                    if not model.startswith("gpt-image-2"):
                        edit_options["input_fidelity"] = "high"
                    response = self.client.images.edit(**edit_options)
            else:
                response = self.client.images.generate(
                    model=model,
                    prompt=prompt,
                    size=size,
                    quality=quality,
                    output_format=OPENAI_IMAGE_OUTPUT_FORMAT,
                    output_compression=OPENAI_IMAGE_OUTPUT_COMPRESSION,
                )
            encoded = response.data[0].b64_json
            if not encoded:
                raise ImageGenerationError("Bild-API hat keine eingebetteten Bilddaten geliefert")
            return base64.b64decode(encoded, validate=True)
        except ImageGenerationError:
            raise
        except Exception as exc:
            raise ImageGenerationError(f"KI-Bildgenerierung fehlgeschlagen: {exc}") from exc


class AIImageRenderer:
    """Renderer-kompatible KI-Ausgabe mit lokal erzwungenem Zielformat."""

    sizes = Renderer.sizes
    api_sizes = {"feed": "1088x1360", "story": "1088x1920"}
    is_ai = True

    def __init__(
        self,
        root: Path,
        media_root: Path,
        upload_root: Path,
        provider: ImageProvider,
    ):
        self.root = Path(root).resolve()
        self.media_root = Path(media_root).resolve()
        self.upload_root = Path(upload_root).resolve()
        self.provider = provider
        self.validator = Renderer(root, media_root, upload_root)
        self._metadata: dict[str, dict] = {}

    def _player_reference(self, value: str | None) -> Path | None:
        return Renderer._safe_file(
            value,
            (self.media_root, self.upload_root),
            Renderer.image_types,
            20 * 1024 * 1024,
        )

    def _logo_reference(self, value: str | None) -> Path | None:
        return Renderer._safe_file(
            value,
            (self.upload_root,),
            Renderer.image_types,
            20 * 1024 * 1024,
        )

    def _sponsor_reference(self, item: dict) -> Path:
        path = Renderer._safe_file(
            item.get("path"),
            (self.media_root, self.upload_root),
            Renderer.image_types,
            20 * 1024 * 1024,
        )
        if path is None:
            raise ImageGenerationError(
                f"Das verifizierte Sponsorenlogo {item.get('name') or ''} ist nicht verfügbar"
            )
        expected = str(item.get("checksum") or "").lower()
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if not expected or actual != expected:
            raise ImageGenerationError(
                f"Die Prüfsumme des Sponsorenlogos {item.get('name') or ''} stimmt nicht mehr"
            )
        return path

    @staticmethod
    def _reference_metadata(
        data: dict, opponent_present: bool, sponsors: list[dict]
    ) -> dict:
        logos = data.get("logos") if isinstance(data.get("logos"), dict) else {}
        team = logos.get("team") if isinstance(logos.get("team"), dict) else {}
        opponent = logos.get("opponent") if isinstance(logos.get("opponent"), dict) else {}
        references = [
            {"position": 1, "role": "player"},
            {
                "position": 2,
                "role": "team_logo",
                "logo_id": team.get("id"),
                "version": team.get("version"),
                "checksum": team.get("checksum"),
            },
        ]
        if opponent_present:
            references.append(
                {
                    "position": 3,
                    "role": "opponent_logo",
                    "logo_id": opponent.get("id"),
                    "version": opponent.get("version"),
                    "checksum": opponent.get("checksum"),
                }
            )
        for sponsor in sponsors:
            references.append(
                {
                    "position": len(references) + 1,
                    "role": "sponsor_logo",
                    "media_asset_id": sponsor.get("media_asset_id"),
                    "name": sponsor.get("name"),
                    "checksum": sponsor.get("checksum"),
                    "placement": sponsor.get("placement"),
                }
            )
        return {
            "mode": "ai-reference",
            "version": LOGO_REFERENCE_VERSION,
            "reference_order": references,
            "opponent_text_fallback": not opponent_present,
            "sponsor_count": len(sponsors),
            "fixed_logo_positions": False,
            "manual_logo_review_required": True,
        }

    def render(self, kind: str, target: str, data: dict) -> Path:
        if kind not in self.sizes:
            raise ImageGenerationError("Unbekanntes Bildformat")
        prompt = data.get("image_prompt")
        if not prompt:
            raise ImageGenerationError("Gerenderter KI-Bildprompt fehlt")
        player = self._player_reference(data.get("player_image"))
        if not player:
            raise ImageGenerationError(
                "Für eine KI-Grafik ist ein verfügbares Spielerbild erforderlich"
            )
        team_logo = self._logo_reference(data.get("team_logo"))
        if not team_logo:
            raise ImageGenerationError(
                "Für eine KI-Grafik ist ein verifiziertes Mannschaftslogo erforderlich"
            )
        opponent_logo = self._logo_reference(data.get("opponent_logo"))
        sponsor_items = [
            dict(item)
            for item in (data.get("sponsor_references") or [])
            if isinstance(item, dict)
        ]
        sponsor_paths = [self._sponsor_reference(item) for item in sponsor_items]
        references = [player, team_logo]
        if opponent_logo:
            references.append(opponent_logo)
        references.extend(sponsor_paths)
        integration = self._reference_metadata(
            data, opponent_logo is not None, sponsor_items
        )
        requested_out = (self.root / target).resolve()
        out = requested_out
        generation_job_id = data.get("_generation_job_id")
        if generation_job_id:
            # Media versions are database counters and can point at a file
            # left behind by a rolled-back or legacy render.  Scope the
            # physical filename to the persistent job instead of trusting the
            # version filename alone.  The stable digest also keeps retries of
            # this exact job idempotent without exposing user-controlled text
            # in a path.
            job_digest = hashlib.sha256(str(generation_job_id).encode("utf-8")).hexdigest()[:12]
            out = requested_out.with_name(
                f"{requested_out.stem}-job-{job_digest}{requested_out.suffix}"
            )
        if out != self.root and not out.is_relative_to(self.root):
            raise ImageGenerationError("Ausgabepfad liegt außerhalb des Render-Verzeichnisses")
        out.parent.mkdir(parents=True, exist_ok=True)
        phase = data.get("_generation_phase")
        if out.is_file():
            self.validate(out, kind)
            self._metadata[str(out)] = {
                "final_path": str(out),
                "requested_path": str(requested_out),
                "generation_job_id": str(generation_job_id) if generation_job_id else None,
                "reused_final": True,
                "logo_integration": integration,
            }
            return out
        if callable(phase):
            phase("generating_ai_composition")
        raw = self.provider.generate(
            prompt=prompt.rendered,
            references=references,
            size=self.api_sizes[kind],
            model=prompt.model,
            quality=prompt.quality,
        )
        temporary = out.with_name(f".{out.name}.tmp")
        try:
            with Image.open(BytesIO(raw)) as image:
                image.load()
                normalized = ImageOps.fit(
                    image.convert("RGB"),
                    self.sizes[kind],
                    method=Image.Resampling.LANCZOS,
                    centering=(0.5, 0.5),
                )
                normalized.save(temporary, "PNG", optimize=True)
            self.validate(temporary, kind)
            temporary.replace(out)
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            if isinstance(exc, ImageGenerationError):
                raise
            raise ImageGenerationError(f"KI-Ausgabe ist kein verarbeitbares Bild: {exc}") from exc
        if callable(phase):
            phase("validating_final_media")
        self.validate(out, kind)
        self._metadata[str(out)] = {
            "final_path": str(out),
            "requested_path": str(requested_out),
            "generation_job_id": str(generation_job_id) if generation_job_id else None,
            "logo_integration": integration,
        }
        return out

    def metadata_for(self, path: str | Path) -> dict:
        return dict(self._metadata.get(str(Path(path).resolve()), {}))

    def validate(self, path: Path, kind: str) -> dict:
        return self.validator.validate(path, kind)
