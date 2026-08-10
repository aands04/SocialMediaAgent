import base64
import hashlib
import time
import uuid
from contextlib import ExitStack
from io import BytesIO
from pathlib import Path
from typing import BinaryIO, NamedTuple

import structlog
from openai import OpenAI
from PIL import Image, ImageOps

from app.rendering.service import Renderer, RenderValidationError

log = structlog.get_logger()

LOGO_REFERENCE_VERSION = "verified-media-ai-references-v2"
OPENAI_IMAGE_OUTPUT_FORMAT = "png"
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
        provider_operation_id: str | None = None,
        provider_transport: str | None = None,
        provider_duration_ms: int | None = None,
        provider_fallback_used: bool = False,
    ):
        super().__init__(message)
        self.provider_status_code = provider_status_code
        self.provider_request_id = provider_request_id
        self.provider_reference_count = provider_reference_count
        self.provider_reference_total_bytes = provider_reference_total_bytes
        self.provider_reference_mime_types = provider_reference_mime_types
        self.provider_reference_dimensions = provider_reference_dimensions
        self.provider_operation_id = provider_operation_id
        self.provider_transport = provider_transport
        self.provider_duration_ms = provider_duration_ms
        self.provider_fallback_used = provider_fallback_used


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


def _provider_response_request_id(response: object) -> str | None:
    return _safe_provider_request_id(
        getattr(response, "_request_id", None) or getattr(response, "request_id", None)
    )


def _attach_provider_diagnostics(
    exc: Exception,
    *,
    operation_id: str,
    transport: str,
    duration_ms: int,
    fallback_used: bool,
) -> None:
    """Attach only non-sensitive transport metadata for the persistent job log."""

    for name, value in (
        ("provider_operation_id", operation_id),
        ("provider_transport", transport),
        ("provider_duration_ms", duration_ms),
        ("provider_fallback_used", fallback_used),
    ):
        try:
            setattr(exc, name, value)
        except (AttributeError, TypeError):
            pass


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


def _layout_safety_prompt(size: str) -> str:
    try:
        width, height = (int(part) for part in size.lower().split("x", maxsplit=1))
    except (AttributeError, TypeError, ValueError):
        width, height = (1080, 1350)
    width, height = {
        (1088, 1360): (1080, 1350),
        (1152, 2048): (1080, 1920),
    }.get((width, height), (width, height))
    story = height / max(width, 1) >= 1.5
    safe_zone = (
        "12 bis 88 Prozent der Breite und 12 bis 88 Prozent der Höhe"
        if story
        else "8 bis 92 Prozent der Breite und 10 bis 90 Prozent der Höhe"
    )
    return (
        "TECHNISCHER FORMAT- UND RANDSCHUTZ:\n"
        f"- Die fertige Veröffentlichung hat das Hochformat {width} × {height} Pixel.\n"
        "- Halte Spieler, Gesichter, Bälle, sämtliche Logos, Ergebnisse, "
        "Mannschaftsnamen sowie alle anderen lesbaren Texte vollständig innerhalb "
        f"des zentralen sicheren Bereichs von {safe_zone}.\n"
        "- Kein wichtiges Motiv und kein Buchstabe darf den Bildrand berühren oder "
        "angeschnitten sein. Dekorativer Hintergrund, Licht und Texturen dürfen bis "
        "an den Rand reichen.\n"
        "- Erzeuge die Komposition vollflächig im exakt passenden Seitenverhältnis. "
        "Die Anwendung verkleinert das Bild anschließend nur proportional und "
        "schneidet keine Ränder ab."
    )


def _provider_prompt(prompt: str, reference_count: int, size: str) -> str:
    sections = [prompt]
    if reference_count > 0:
        sections.append(
            f"TECHNISCHER REFERENZHINWEIS: Zusätzlich wurden {reference_count} "
            "getrennte Eingabebilder in der im fachlichen Prompt beschriebenen Reihenfolge "
            "übergeben. Verwende jedes Bild ausschließlich für die dort genannte "
            "Rolle. Logos, Wappen und Sponsorzeichen müssen inhaltlich unverändert "
            "bleiben. Die Eingabebilder dürfen nicht als Collage oder technische "
            "Tafel im Ergebnis erscheinen."
        )
    sections.append(_layout_safety_prompt(size))
    return "\n\n".join(sections)


def _fit_full_bleed(image: Image.Image, target_size: tuple[int, int]) -> Image.Image:
    """Scale an already aspect-correct provider image without clipping.

    GPT Image 2 accepts custom dimensions whose edges are multiples of 16. The
    renderer therefore requests native 4:5 and 9:16 canvases. A defensive cover
    fit remains only for unexpected provider output from older models.
    """

    source = image.convert("RGB")
    source_ratio = source.width / max(source.height, 1)
    target_ratio = target_size[0] / max(target_size[1], 1)
    if abs(source_ratio - target_ratio) <= 0.001:
        return source.resize(target_size, Image.Resampling.LANCZOS)
    return ImageOps.fit(
        source, target_size, method=Image.Resampling.LANCZOS, centering=(0.5, 0.5)
    )


def _provider_image_size(size: str, model: str) -> str:
    """Use native Instagram ratios on GPT Image 2, legacy sizes otherwise."""

    try:
        width, height = (int(part) for part in size.lower().split("x", maxsplit=1))
    except (TypeError, ValueError):
        return "auto"
    if (
        model.startswith("gpt-image-2")
        and width > 0
        and height > 0
        and width <= 3840
        and height <= 3840
        and width % 16 == 0
        and height % 16 == 0
        and max(width, height) / min(width, height) <= 3
        and 655_360 <= width * height <= 8_294_400
    ):
        return f"{width}x{height}"
    if width == height:
        return "1024x1024"
    return "1024x1536" if height > width else "1536x1024"


def _provider_output_options() -> dict[str, str | int]:
    options: dict[str, str | int] = {"output_format": OPENAI_IMAGE_OUTPUT_FORMAT}
    if OPENAI_IMAGE_OUTPUT_FORMAT in {"jpeg", "webp"}:
        options["output_compression"] = 60
    return options


def _reference_uploads(
    stack: ExitStack,
    payloads: list[tuple[str, bytes, str, tuple[int, int]]],
) -> list[BinaryIO]:
    """Create fresh named streams for one non-retriable multipart request."""

    return [
        stack.enter_context(_NamedUpload(content, name))
        for name, content, _mime_type, _size in payloads
    ]


def _response_image_bytes(response: object) -> bytes:
    for item in getattr(response, "output", ()) or ():
        if getattr(item, "type", None) != "image_generation_call":
            continue
        encoded = getattr(item, "result", None)
        if encoded:
            return base64.b64decode(encoded, validate=True)
    raise ImageGenerationError("Bild-Tool hat keine eingebetteten Bilddaten geliefert")


def _may_fallback_to_responses(exc: Exception, reference_count: int) -> bool:
    """Allow one official alternate transport for an unhandled upstream 520.

    The fallback is deliberately restricted to a single reference and to a
    response that never received an OpenAI request ID.  Authentication,
    validation and quota errors must never be hidden by a second request.
    """

    status_code, request_id = _provider_error_metadata(exc)
    return reference_count == 1 and status_code == 520 and request_id is None


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
    def __init__(self, api_key: str, *, responses_model: str = "gpt-5.4-mini"):
        # Keep provider retries under the generation job's persistent cost and
        # idempotency guard.  This also prevents an SDK-level retry from racing
        # with the worker's single delayed retry for the same output slot.
        self.client = OpenAI(api_key=api_key, max_retries=0)
        self.responses_model = responses_model

    @staticmethod
    def _request_context(
        *,
        operation_id: str,
        transport: str,
        model: str,
        requested_size: str,
        provider_size: str,
        quality: str,
        prompt_chars: int,
        diagnostics: ReferenceUploadDiagnostics | None,
        fallback_used: bool,
    ) -> dict[str, object]:
        return {
            "operation_id": operation_id,
            "transport": transport,
            "model": model,
            "requested_size": requested_size,
            "provider_size": provider_size,
            "quality": quality,
            "output_format": OPENAI_IMAGE_OUTPUT_FORMAT,
            "prompt_chars": prompt_chars,
            "reference_count": diagnostics.count if diagnostics else 0,
            "reference_total_bytes": diagnostics.total_bytes if diagnostics else 0,
            "reference_mime_types": diagnostics.mime_types if diagnostics else (),
            "reference_dimensions": diagnostics.dimensions if diagnostics else (),
            "fallback_used": fallback_used,
        }

    def _execute_provider_request(
        self,
        *,
        context: dict[str, object],
        request,
        extract,
    ) -> bytes:
        """Run one API request with content-free structured diagnostics."""

        log.info("openai_image_request_started", **context)
        started = time.perf_counter()
        try:
            response = request()
            result = extract(response)
        except Exception as exc:
            duration_ms = round((time.perf_counter() - started) * 1000)
            status_code, request_id = _provider_error_metadata(exc)
            _attach_provider_diagnostics(
                exc,
                operation_id=str(context["operation_id"]),
                transport=str(context["transport"]),
                duration_ms=duration_ms,
                fallback_used=bool(context["fallback_used"]),
            )
            log.warning(
                "openai_image_request_failed",
                **context,
                duration_ms=duration_ms,
                exception_type=type(exc).__name__,
                provider_status_code=status_code,
                provider_request_id=request_id,
            )
            raise
        duration_ms = round((time.perf_counter() - started) * 1000)
        log.info(
            "openai_image_request_succeeded",
            **context,
            duration_ms=duration_ms,
            provider_request_id=_provider_response_request_id(response),
            response_bytes=len(result),
        )
        return result

    def _responses_edit(
        self,
        *,
        prompt: str,
        payload: tuple[str, bytes, str, tuple[int, int]],
        size: str,
        model: str,
        quality: str,
        operation_id: str,
        diagnostics: ReferenceUploadDiagnostics,
    ) -> bytes:
        _name, image_bytes, mime_type, _dimensions = payload
        content = [
            {"type": "input_text", "text": prompt},
            {
                "type": "input_image",
                "image_url": (
                    f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
                ),
                "detail": "high",
            },
        ]
        image_tool = {
            "type": "image_generation",
            "action": "edit",
            "model": model,
            "size": _provider_image_size(size, model),
            "quality": quality,
            **_provider_output_options(),
        }
        if not model.startswith("gpt-image-2"):
            image_tool["input_fidelity"] = "high"
        context = self._request_context(
            operation_id=operation_id,
            transport="responses.image_generation",
            model=model,
            requested_size=size,
            provider_size=str(image_tool["size"]),
            quality=quality,
            prompt_chars=len(prompt),
            diagnostics=diagnostics,
            fallback_used=True,
        )
        return self._execute_provider_request(
            context=context,
            request=lambda: self.client.responses.create(
                model=self.responses_model,
                input=[{"role": "user", "content": content}],
                tools=[image_tool],
                tool_choice={"type": "image_generation"},
                store=False,
            ),
            extract=_response_image_bytes,
        )

    def generate(
        self,
        prompt: str,
        references: list[Path],
        size: str,
        model: str,
        quality: str,
    ) -> bytes:
        reference_diagnostics: ReferenceUploadDiagnostics | None = None
        operation_id = uuid.uuid4().hex
        try:
            if references:
                payloads = [
                    _normalized_reference_bytes(path, position=position)
                    for position, path in enumerate(references, start=1)
                ]
                reference_diagnostics = ReferenceUploadDiagnostics(
                    count=len(payloads),
                    total_bytes=sum(len(content) for _name, content, _mime, _size in payloads),
                    mime_types=tuple(mime for _name, _content, mime, _size in payloads),
                    dimensions=tuple(
                        f"{dimensions[0]}x{dimensions[1]}"
                        for _name, _content, _mime, dimensions in payloads
                    ),
                )
                # Reference-based generation is an image edit.  Use the
                # dedicated official multipart endpoint instead of embedding
                # all source bytes as JSON data URLs in a Responses request.
                # Production repeatedly received an upstream HTTP 520 before
                # an OpenAI request ID was assigned on that JSON transport.
                with ExitStack() as stack:
                    uploads = _reference_uploads(stack, payloads)
                    edit_options = {
                        "model": model,
                        # The SDK accepts one file or a sequence.  Sending a
                        # singleton as a file avoids serialising it as the
                        # multi-image field `image[]`, which repeatedly failed
                        # upstream before OpenAI assigned a request ID.
                        "image": uploads[0] if len(uploads) == 1 else uploads,
                        "prompt": _provider_prompt(prompt, len(payloads), size),
                        "size": _provider_image_size(size, model),
                        "quality": quality,
                        **_provider_output_options(),
                    }
                    # GPT Image 2 applies high input fidelity automatically
                    # and rejects the explicit option.  Older image models
                    # still accept it.
                    if not model.startswith("gpt-image-2"):
                        edit_options["input_fidelity"] = "high"
                    context = self._request_context(
                        operation_id=operation_id,
                        transport="images.edit",
                        model=model,
                        requested_size=size,
                        provider_size=str(edit_options["size"]),
                        quality=quality,
                        prompt_chars=len(str(edit_options["prompt"])),
                        diagnostics=reference_diagnostics,
                        fallback_used=False,
                    )
                    try:
                        return self._execute_provider_request(
                            context=context,
                            request=lambda: self.client.images.edit(**edit_options),
                            extract=lambda response: base64.b64decode(
                                response.data[0].b64_json, validate=True
                            ),
                        )
                    except Exception as edit_exc:
                        if not _may_fallback_to_responses(edit_exc, len(payloads)):
                            raise
                        status_code, request_id = _provider_error_metadata(edit_exc)
                        log.info(
                            "openai_image_fallback_selected",
                            operation_id=operation_id,
                            from_transport="images.edit",
                            to_transport="responses.image_generation",
                            reason="unassigned_http_520",
                            provider_status_code=status_code,
                            provider_request_id=request_id,
                            reference_count=reference_diagnostics.count,
                        )
                        return self._responses_edit(
                            prompt=edit_options["prompt"],
                            payload=payloads[0],
                            size=size,
                            model=model,
                            quality=quality,
                            operation_id=operation_id,
                            diagnostics=reference_diagnostics,
                        )
            else:
                provider_prompt = _provider_prompt(prompt, 0, size)
                provider_size = _provider_image_size(size, model)
                context = self._request_context(
                    operation_id=operation_id,
                    transport="images.generate",
                    model=model,
                    requested_size=size,
                    provider_size=provider_size,
                    quality=quality,
                    prompt_chars=len(provider_prompt),
                    diagnostics=None,
                    fallback_used=False,
                )
                return self._execute_provider_request(
                    context=context,
                    request=lambda: self.client.images.generate(
                        model=model,
                        prompt=provider_prompt,
                        size=provider_size,
                        quality=quality,
                        **_provider_output_options(),
                    ),
                    extract=lambda response: base64.b64decode(
                        response.data[0].b64_json, validate=True
                    ),
                )
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
                provider_operation_id=getattr(exc, "provider_operation_id", operation_id),
                provider_transport=getattr(exc, "provider_transport", None),
                provider_duration_ms=getattr(exc, "provider_duration_ms", None),
                provider_fallback_used=bool(getattr(exc, "provider_fallback_used", False)),
            ) from exc


class AIImageRenderer:
    """Renderer-kompatible KI-Ausgabe mit lokal erzwungenem Zielformat."""

    sizes = Renderer.sizes
    # Both dimensions comply with GPT Image 2's multiple-of-16 constraints and
    # exactly match Instagram's 4:5 feed and 9:16 story aspect ratios.
    api_sizes = {"feed": "1088x1360", "story": "1152x2048"}
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

    def _result_layout_reference(self, value: str | None) -> Path | None:
        """Resolve a tenant-selected generated image inside this renderer root."""

        return Renderer._safe_file(
            value,
            (self.root,),
            Renderer.image_types,
            20 * 1024 * 1024,
        )

    @staticmethod
    def provider_prompt(data: dict) -> str:
        """Return the exact prompt the lower-level provider will transmit."""

        prompt = data.get("image_prompt")
        rendered = str(getattr(prompt, "rendered", "") or "")
        if data.get("post_type") == "result" and data.get("result_layout_reference"):
            reference_count = 1
        else:
            reference_count = int(bool(data.get("player_image"))) + int(bool(data.get("team_logo")))
            reference_count += int(bool(data.get("opponent_logo")))
            reference_count += len(
                [item for item in (data.get("sponsor_references") or []) if isinstance(item, dict)]
            )
        size = "1152x2048" if getattr(prompt, "media_kind", "") == "story" else "1088x1360"
        return _provider_prompt(rendered, reference_count, size)

    @staticmethod
    def _reference_metadata(
        data: dict,
        opponent_present: bool,
        sponsors: list[dict],
        result_layout_present: bool,
    ) -> dict:
        if data.get("post_type") == "result" and result_layout_present:
            return {
                "mode": "ai-result-image-edit",
                "version": LOGO_REFERENCE_VERSION,
                "reference_order": [
                    {
                        "position": 1,
                        "role": "same_fixture_announcement_layout",
                        "source_post_id": data.get("result_layout_reference_post_id"),
                        "source_media_kind": data.get("result_layout_reference_media_kind"),
                        "source_variant": data.get("result_layout_reference_variant"),
                    }
                ],
                "result_layout_reference": True,
                "result_transform_mode": True,
                "fixed_logo_positions": False,
                "manual_logo_review_required": True,
            }
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
        if result_layout_present:
            references.append(
                {
                    "position": len(references) + 1,
                    "role": "announcement_feed_layout",
                    "source_post_id": data.get("result_layout_reference_post_id"),
                }
            )
        return {
            "mode": "ai-reference",
            "version": LOGO_REFERENCE_VERSION,
            "reference_order": references,
            "opponent_text_fallback": not opponent_present,
            "sponsor_count": len(sponsors),
            "result_layout_reference": result_layout_present,
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
        result_layout = self._result_layout_reference(data.get("result_layout_reference"))
        result_transform = bool(data.get("post_type") == "result" and result_layout)
        if result_transform:
            # Continue from the already reviewed pre-match artwork. Supplying
            # player and logo files again encouraged a redesign and made the
            # multipart request larger and more fragile.
            opponent_logo = None
            sponsor_items = []
            references = [result_layout]
        else:
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
            data,
            opponent_logo is not None,
            sponsor_items,
            result_layout is not None,
        )
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
                normalized = _fit_full_bleed(image, self.sizes[kind])
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
