import base64
import hashlib
from contextlib import ExitStack
from io import BytesIO
from pathlib import Path
from typing import BinaryIO, NamedTuple

from openai import OpenAI
from PIL import Image, ImageOps

from app.rendering.service import Renderer, RenderValidationError

LOGO_REFERENCE_VERSION = "verified-media-ai-references-v2"
OPENAI_IMAGE_OUTPUT_FORMAT = "webp"
OPENAI_IMAGE_OUTPUT_COMPRESSION = 60
REFERENCE_PLAYER_MAX_EDGE = 1536
REFERENCE_LOGO_MAX_EDGE = 1024
REFERENCE_IMAGE_MAX_BYTES = 8 * 1024 * 1024
REFERENCE_IMAGE_MAX_PIXELS = 40_000_000
REFERENCE_PLAYER_JPEG_QUALITY = 90

REFERENCE_IMAGE_MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


class ReferenceUploadDiagnostics(NamedTuple):
    count: int
    total_bytes: int
    mime_types: tuple[str, ...]
    dimensions: tuple[str, ...]


class _NamedUpload(BytesIO):
    """In-memory file with a stable name for the SDK multipart encoder."""

    def __init__(self, content: bytes, name: str):
        super().__init__(content)
        self.name = name


class ImageGenerationError(RenderValidationError):
    def __init__(
        self,
        message: str,
        *,
        provider_status_code: int | None = None,
        provider_request_id: str | None = None,
        provider_reference_count: int | None = None,
        provider_reference_total_bytes: int | None = None,
        provider_reference_mime_types: tuple[str, ...] = (),
        provider_reference_dimensions: tuple[str, ...] = (),
    ):
        super().__init__(message)
        self.provider_status_code = provider_status_code
        self.provider_request_id = provider_request_id
        self.provider_reference_count = provider_reference_count
        self.provider_reference_total_bytes = provider_reference_total_bytes
        self.provider_reference_mime_types = provider_reference_mime_types
        self.provider_reference_dimensions = provider_reference_dimensions


def _safe_provider_request_id(value: object) -> str | None:
    if value is None:
        return None
    candidate = "".join(
        character
        for character in str(value).strip()[:200]
        if character.isalnum() or character in {"-", "_", ".", ":"}
    )
    return candidate or None


def _provider_error_metadata(exc: Exception) -> tuple[int | None, str | None]:
    status_code = getattr(exc, "status_code", None)
    if not isinstance(status_code, int):
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
    if not isinstance(status_code, int):
        status_code = None
    request_id = getattr(exc, "request_id", None)
    if not request_id:
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        if headers:
            request_id = headers.get("x-request-id")
    return status_code, _safe_provider_request_id(request_id)


def _has_alpha(image: Image.Image) -> bool:
    return "A" in image.getbands() or (image.mode == "P" and "transparency" in image.info)


def _normalized_reference_bytes(
    path: Path,
    *,
    position: int,
) -> tuple[str, bytes, str, tuple[int, int]]:
    """Create a fresh, metadata-free upload for one provider request.

    The first reference is the player photo and may use high-quality JPEG.
    Logo references remain lossless PNG (or lossless WebP only when the PNG
    would exceed our conservative transport limit).  Re-encoding also avoids
    replaying a partially consumed multipart stream on an SDK-level retry.
    """

    suffix = path.suffix.lower()
    if suffix not in REFERENCE_IMAGE_MIME_TYPES:
        raise ImageGenerationError(
            f"Nicht unterstütztes Referenzbildformat: {path.suffix or 'keine Dateiendung'}"
        )
    try:
        with Image.open(path) as source:
            if source.width * source.height > REFERENCE_IMAGE_MAX_PIXELS:
                raise ImageGenerationError("Das Referenzbild überschreitet die sichere Pixelanzahl")
            source.load()
            normalized = ImageOps.exif_transpose(source)
            max_edge = REFERENCE_PLAYER_MAX_EDGE if position == 1 else REFERENCE_LOGO_MAX_EDGE
            if max(normalized.size) > max_edge:
                normalized.thumbnail(
                    (max_edge, max_edge),
                    Image.Resampling.LANCZOS,
                )
            alpha = _has_alpha(normalized)
            buffer = BytesIO()
            if position == 1 and not alpha:
                normalized.convert("RGB").save(
                    buffer,
                    "JPEG",
                    quality=REFERENCE_PLAYER_JPEG_QUALITY,
                    optimize=True,
                )
                extension = ".jpg"
                mime_type = "image/jpeg"
            else:
                mode = "RGBA" if alpha else "RGB"
                normalized.convert(mode).save(buffer, "PNG", optimize=True)
                extension = ".png"
                mime_type = "image/png"
            content = buffer.getvalue()
            if len(content) > REFERENCE_IMAGE_MAX_BYTES:
                buffer = BytesIO()
                mode = "RGBA" if alpha else "RGB"
                normalized.convert(mode).save(
                    buffer,
                    "WEBP",
                    lossless=True,
                    quality=100,
                    method=6,
                )
                content = buffer.getvalue()
                extension = ".webp"
                mime_type = "image/webp"
            if len(content) > REFERENCE_IMAGE_MAX_BYTES:
                raise ImageGenerationError(
                    "Das normalisierte Referenzbild überschreitet die sichere Upload-Größe"
                )
    except ImageGenerationError:
        raise
    except Exception as exc:
        raise ImageGenerationError(f"Referenzbild ist technisch nicht lesbar: {path.name}") from exc
    return f"reference-{position}{extension}", content, mime_type, normalized.size


def _reference_uploads(
    stack: ExitStack,
    references: list[Path],
) -> tuple[list[BinaryIO], ReferenceUploadDiagnostics]:
    uploads: list[BinaryIO] = []
    total_bytes = 0
    mime_types: list[str] = []
    dimensions: list[str] = []
    for position, path in enumerate(references, start=1):
        name, content, mime_type, size = _normalized_reference_bytes(
            path,
            position=position,
        )
        # Match the official Python client example: pass named binary handles
        # and let the SDK build image[] multipart parts.  Supplying nested
        # tuples worked for many requests but repeatedly produced provider
        # HTTP 520 responses for otherwise valid multi-reference edits.
        handle = stack.enter_context(_NamedUpload(content, name))
        uploads.append(handle)
        total_bytes += len(content)
        mime_types.append(mime_type)
        dimensions.append(f"{size[0]}x{size[1]}")
    return uploads, ReferenceUploadDiagnostics(
        count=len(uploads),
        total_bytes=total_bytes,
        mime_types=tuple(mime_types),
        dimensions=tuple(dimensions),
    )


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
        # Multipart streams cannot safely be replayed after they have already
        # been consumed.  Disable the SDK's transparent HTTP retries and let
        # the generation job perform the single delayed retry with freshly
        # normalized streams and the existing per-output cost guard.
        self.client = OpenAI(api_key=api_key, max_retries=0)

    def generate(
        self,
        prompt: str,
        references: list[Path],
        size: str,
        model: str,
        quality: str,
    ) -> bytes:
        reference_diagnostics: ReferenceUploadDiagnostics | None = None
        try:
            if references:
                with ExitStack() as stack:
                    files, reference_diagnostics = _reference_uploads(stack, references)
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
            status_code, request_id = _provider_error_metadata(exc)
            details = []
            if status_code is not None:
                details.append(f"HTTP {status_code}")
            if request_id:
                details.append(f"Request-ID {request_id}")
            suffix = f" ({', '.join(details)})" if details else ""
            raise ImageGenerationError(
                f"KI-Bildgenerierung fehlgeschlagen{suffix}",
                provider_status_code=status_code,
                provider_request_id=request_id,
                provider_reference_count=(
                    reference_diagnostics.count if reference_diagnostics else None
                ),
                provider_reference_total_bytes=(
                    reference_diagnostics.total_bytes if reference_diagnostics else None
                ),
                provider_reference_mime_types=(
                    reference_diagnostics.mime_types if reference_diagnostics else ()
                ),
                provider_reference_dimensions=(
                    reference_diagnostics.dimensions if reference_diagnostics else ()
                ),
            ) from exc


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
    def _reference_metadata(data: dict, opponent_present: bool, sponsors: list[dict]) -> dict:
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

    def _output_path(self, target: str, generation_job_id: str | None) -> tuple[Path, Path]:
        requested_out = (self.root / target).resolve()
        if requested_out != self.root and not requested_out.is_relative_to(self.root):
            raise ImageGenerationError("Ausgabepfad liegt außerhalb des Render-Verzeichnisses")
        out = requested_out
        if generation_job_id:
            job_digest = hashlib.sha256(str(generation_job_id).encode("utf-8")).hexdigest()[:12]
            out = requested_out.with_name(
                f"{requested_out.stem}-job-{job_digest}{requested_out.suffix}"
            )
        return requested_out, out

    def reusable_output(self, target: str, generation_job_id: str | None, kind: str) -> Path | None:
        """Return a validated provider result previously saved for this output.

        The caller uses this before reserving another paid image generation.
        """

        _requested, candidate = self._output_path(target, generation_job_id)
        if not candidate.is_file():
            return None
        try:
            self.validate(candidate, kind)
        except ImageGenerationError:
            # An interrupted provider response can leave a truncated file. It
            # is not a reusable output and must not block the one permitted
            # replacement generation for this slot.
            candidate.unlink(missing_ok=True)
            return None
        return candidate

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
            dict(item) for item in (data.get("sponsor_references") or []) if isinstance(item, dict)
        ]
        sponsor_paths = [self._sponsor_reference(item) for item in sponsor_items]
        references = [player, team_logo]
        if opponent_logo:
            references.append(opponent_logo)
        references.extend(sponsor_paths)
        integration = self._reference_metadata(data, opponent_logo is not None, sponsor_items)
        generation_job_id = data.get("_generation_job_id")
        requested_out, out = self._output_path(target, generation_job_id)
        reuse_generation_job_id = data.get("_reuse_generation_job_id")
        if reuse_generation_job_id:
            reused = self.reusable_output(target, reuse_generation_job_id, kind)
            if reused is not None:
                self._metadata[str(reused)] = {
                    "final_path": str(reused),
                    "requested_path": str(requested_out),
                    "generation_job_id": str(generation_job_id) if generation_job_id else None,
                    "reused_from_generation_job_id": str(reuse_generation_job_id),
                    "reused_final": True,
                    "logo_integration": integration,
                }
                return reused
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
