import base64
from contextlib import ExitStack
from io import BytesIO
from pathlib import Path

from openai import OpenAI
from PIL import Image, ImageOps

from app.logos.service import LogoCompositor
from app.rendering.service import Renderer, RenderValidationError


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
                    files = [stack.enter_context(path.open("rb")) for path in references]
                    edit_options = {
                        "model": model,
                        "image": files,
                        "prompt": prompt,
                        "size": size,
                        "quality": quality,
                        "output_format": "png",
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
                    output_format="png",
                )
            encoded = response.data[0].b64_json
            if not encoded:
                raise ImageGenerationError(
                    "Bild-API hat keine eingebetteten PNG-Daten geliefert"
                )
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
        self.compositor = LogoCompositor(upload_root)
        self._metadata: dict[str, dict] = {}

    def _reference(self, value: str | None) -> Path | None:
        return Renderer._safe_file(
            value,
            (self.media_root, self.upload_root),
            Renderer.image_types,
            20 * 1024 * 1024,
        )

    def render(self, kind: str, target: str, data: dict) -> Path:
        if kind not in self.sizes:
            raise ImageGenerationError("Unbekanntes Bildformat")
        prompt = data.get("image_prompt")
        if not prompt:
            raise ImageGenerationError("Gerenderter KI-Bildprompt fehlt")
        player = self._reference(data.get("player_image"))
        if not player:
            raise ImageGenerationError(
                "Für eine KI-Grafik ist ein verfügbares Spielerbild erforderlich"
            )
        out = (self.root / target).resolve()
        if out != self.root and not out.is_relative_to(self.root):
            raise ImageGenerationError(
                "Ausgabepfad liegt außerhalb des Render-Verzeichnisses"
            )
        out.parent.mkdir(parents=True, exist_ok=True)
        base = out.with_name(f"{out.stem}-ai-base.png")
        phase = data.get("_generation_phase")
        if out.is_file():
            self.validate(out, kind)
            self._metadata[str(out)] = {
                "ai_base_path": str(base),
                "final_path": str(out),
                "reused_final": True,
            }
            return out
        if not base.is_file():
            if callable(phase):
                phase("generating_ai_base")
            raw = self.provider.generate(
                prompt=prompt.rendered,
                references=[player],
                size=self.api_sizes[kind],
                model=prompt.model,
                quality=prompt.quality,
            )
            try:
                with Image.open(BytesIO(raw)) as image:
                    image.load()
                    normalized = ImageOps.fit(
                        image.convert("RGB"),
                        self.sizes[kind],
                        method=Image.Resampling.LANCZOS,
                        centering=(0.5, 0.5),
                    )
                    normalized.save(base, "PNG", optimize=True)
            except Exception as exc:
                raise ImageGenerationError(
                    f"KI-Ausgabe ist kein verarbeitbares Bild: {exc}"
                ) from exc
            self.validate(base, kind)
        logos = data.get("logos")
        composition = None
        if logos:
            if callable(phase):
                phase("compositing_logos")
            composition = self.compositor.compose(
                base_path=base,
                output_path=out,
                kind=kind,
                logos=logos,
            )
        else:
            with Image.open(base) as image:
                image.convert("RGB").save(out, "PNG", optimize=True)
        if callable(phase):
            phase("validating_final_media")
        self.validate(out, kind)
        self._metadata[str(out)] = {
            "ai_base_path": str(base),
            "final_path": str(out),
            "composition": composition,
        }
        return out

    def metadata_for(self, path: str | Path) -> dict:
        return dict(self._metadata.get(str(Path(path).resolve()), {}))

    def validate(self, path: Path, kind: str) -> dict:
        return self.validator.validate(path, kind)
