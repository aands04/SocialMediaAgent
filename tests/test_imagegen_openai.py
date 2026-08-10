import base64
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from openai import OpenAI
from PIL import Image

from app.imagegen.service import ImageGenerationError, OpenAIImageProvider

PNG_BYTES = b"\x89PNG\r\n\x1a\nmock-image-data"


def image_response():
    return SimpleNamespace(
        data=[SimpleNamespace(b64_json=base64.b64encode(PNG_BYTES).decode("ascii"))]
    )


def provider_with_mock_client():
    provider = OpenAIImageProvider.__new__(OpenAIImageProvider)
    provider.client = Mock()
    return provider


def write_reference(path, *, alpha=False, size=(64, 48)):
    mode = "RGBA" if alpha else "RGB"
    color = (10, 70, 140, 180) if alpha else (10, 70, 140)
    image = Image.new(mode, size, color)
    image_format = {
        ".jpg": "JPEG",
        ".jpeg": "JPEG",
        ".png": "PNG",
        ".webp": "WEBP",
    }[path.suffix.lower()]
    image.save(path, image_format)


def test_gpt_image_2_generate_requests_supported_compressed_webp():
    provider = provider_with_mock_client()
    provider.client.images.generate.return_value = image_response()

    result = provider.generate(
        prompt="Testmotiv",
        references=[],
        size="1088x1360",
        model="gpt-image-2",
        quality="medium",
    )

    assert result == PNG_BYTES
    options = provider.client.images.generate.call_args.kwargs
    assert options["output_format"] == "webp"
    assert options["output_compression"] == 60
    assert options["size"] == "1024x1536"
    assert "1080 × 1350 Pixel" in options["prompt"]
    assert "Kein wichtiges Motiv" in options["prompt"]
    assert "response_format" not in options
    assert "input_fidelity" not in options


def test_gpt_image_2_references_use_dedicated_image_edit_endpoint(tmp_path):
    references = [
        tmp_path / "player.png",
        tmp_path / "team-logo.png",
        tmp_path / "opponent-logo.png",
    ]
    for reference in references:
        write_reference(reference)
    provider = provider_with_mock_client()
    provider.client.images.edit.return_value = image_response()

    result = provider.generate(
        prompt="Testmotiv mit Spieler",
        references=references,
        size="1088x1920",
        model="gpt-image-2-2026-07-01",
        quality="medium",
    )

    assert result == PNG_BYTES
    options = provider.client.images.edit.call_args.kwargs
    assert options["model"] == "gpt-image-2-2026-07-01"
    assert options["output_format"] == "webp"
    assert options["output_compression"] == 60
    assert options["size"] == "1024x1536"
    assert "input_fidelity" not in options
    assert "3 getrennte Eingabebilder" in options["prompt"]
    assert "1080 × 1920 Pixel" in options["prompt"]
    assert "12 bis 88 Prozent der Breite" in options["prompt"]
    assert [upload.name for upload in options["image"]] == [
        "reference-1.jpg",
        "reference-2.png",
        "reference-3.png",
    ]
    provider.client.responses.create.assert_not_called()


@pytest.mark.parametrize(
    "filename",
    ["player.jpg", "player.jpeg", "player.png", "player.webp"],
)
def test_edit_normalizes_player_reference_to_explicit_jpeg(tmp_path, filename):
    reference = tmp_path / filename
    write_reference(reference)
    provider = provider_with_mock_client()
    provider.client.images.edit.return_value = image_response()

    result = provider.generate(
        prompt="Referenzbild mit explizitem MIME-Typ",
        references=[reference],
        size="1088x1360",
        model="gpt-image-2",
        quality="medium",
    )

    assert result == PNG_BYTES
    uploads = provider.client.images.edit.call_args.kwargs["image"]
    assert uploads[0].name == "reference-1.jpg"


def test_edit_sends_multiple_normalized_references_as_separate_files(tmp_path):
    player = tmp_path / "player.jpg"
    logo = tmp_path / "logo.webp"
    write_reference(player, size=(3000, 1200))
    write_reference(logo, alpha=True, size=(900, 1600))
    provider = provider_with_mock_client()
    provider.client.images.edit.return_value = image_response()

    result = provider.generate(
        prompt="Normalisierte Referenzen",
        references=[player, logo],
        size="1088x1360",
        model="gpt-image-2",
        quality="medium",
    )

    assert result == PNG_BYTES
    uploads = provider.client.images.edit.call_args.kwargs["image"]
    assert [upload.name for upload in uploads] == ["reference-1.jpg", "reference-2.png"]


def test_reference_request_uses_official_multipart_image_edit_endpoint(tmp_path):
    player = tmp_path / "player.jpg"
    logo = tmp_path / "logo.png"
    write_reference(player)
    write_reference(logo, alpha=True)
    captured: dict[str, object] = {}

    def handler(request: httpx.Request):
        captured["path"] = request.url.path
        captured["content_type"] = request.headers["content-type"]
        captured["body"] = request.read()
        return httpx.Response(
            520,
            request=request,
            headers={"x-request-id": "req_transport_test"},
            json={"error": {"message": "test transport failure", "type": "server_error"}},
        )

    provider = OpenAIImageProvider.__new__(OpenAIImageProvider)
    provider.client = OpenAI(
        api_key="test",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(ImageGenerationError) as raised:
        provider.generate(
            prompt="Multipart-Transport-Test",
            references=[player, logo],
            size="1088x1360",
            model="gpt-image-2",
            quality="medium",
        )

    assert raised.value.provider_status_code == 520
    assert raised.value.provider_request_id == "req_transport_test"
    assert captured["path"] == "/v1/images/edits"
    assert str(captured["content_type"]).startswith("multipart/form-data; boundary=")
    body = captured["body"]
    assert isinstance(body, bytes)
    assert body.count(b'name="image[]"') == 2
    assert b'filename="reference-1.jpg"' in body
    assert b'filename="reference-2.png"' in body
    assert b'name="size"' in body
    assert b"1024x1536" in body
    assert b"input_fidelity" not in body


def test_single_reference_adds_safe_reference_instruction(tmp_path):
    reference = tmp_path / "player.jpg"
    write_reference(reference, size=(3000, 1200))
    provider = provider_with_mock_client()
    provider.client.images.edit.return_value = image_response()

    provider.generate(
        prompt="Unveränderter Einzelreferenz-Prompt",
        references=[reference],
        size="1088x1360",
        model="gpt-image-2",
        quality="medium",
    )

    prompt = provider.client.images.edit.call_args.kwargs["prompt"]
    assert prompt.startswith("Unveränderter Einzelreferenz-Prompt")
    assert "1 getrennte Eingabebilder" in prompt


def test_edit_rejects_unsupported_reference_format_before_api_call(tmp_path):
    reference = tmp_path / "player.gif"
    reference.write_bytes(PNG_BYTES)
    provider = provider_with_mock_client()

    with pytest.raises(
        ImageGenerationError, match=r"Nicht unterstütztes Referenzbildformat: \.gif"
    ):
        provider.generate(
            prompt="Ungültiges Referenzformat",
            references=[reference],
            size="1088x1360",
            model="gpt-image-2",
            quality="medium",
        )

    provider.client.images.edit.assert_not_called()


def test_edit_rejects_unreadable_supported_reference_before_api_call(tmp_path):
    reference = tmp_path / "player.webp"
    reference.write_bytes(b"not-an-image")
    provider = provider_with_mock_client()

    with pytest.raises(ImageGenerationError, match="technisch nicht lesbar"):
        provider.generate(
            prompt="Unlesbare Referenz",
            references=[reference],
            size="1088x1360",
            model="gpt-image-2",
            quality="medium",
        )

    provider.client.images.edit.assert_not_called()


def test_older_gpt_image_edit_uses_high_input_fidelity(tmp_path):
    reference = tmp_path / "player.png"
    write_reference(reference)
    provider = provider_with_mock_client()
    provider.client.images.edit.return_value = image_response()

    result = provider.generate(
        prompt="Kompatibilitätstest",
        references=[reference],
        size="1088x1360",
        model="gpt-image-1.5",
        quality="high",
    )

    assert result == PNG_BYTES
    options = provider.client.images.edit.call_args.kwargs
    assert options["output_format"] == "webp"
    assert options["output_compression"] == 60
    assert options["input_fidelity"] == "high"


def test_provider_disables_sdk_retries_for_persistent_cost_guard(monkeypatch):
    openai = Mock()
    monkeypatch.setattr("app.imagegen.service.OpenAI", openai)

    OpenAIImageProvider("secret")

    openai.assert_called_once_with(api_key="secret", max_retries=0)


def test_provider_exposes_only_safe_transport_diagnostics(tmp_path):
    reference = tmp_path / "player.webp"
    write_reference(reference)
    provider = provider_with_mock_client()
    error = RuntimeError("Cloudflare response with internal body")
    error.status_code = 520
    error.request_id = "req_123\r\ninjected"
    provider.client.images.edit.side_effect = error

    with pytest.raises(ImageGenerationError) as raised:
        provider.generate(
            prompt="Transportfehler",
            references=[reference],
            size="1088x1360",
            model="gpt-image-2",
            quality="medium",
        )

    assert raised.value.provider_status_code == 520
    assert raised.value.provider_request_id == "req_123injected"
    assert raised.value.provider_reference_count == 1
    assert raised.value.provider_reference_total_bytes > 0
    assert raised.value.provider_reference_mime_types == ("image/jpeg",)
    assert raised.value.provider_reference_dimensions == ("64x48",)
    assert str(raised.value) == (
        "KI-Bildgenerierung fehlgeschlagen (HTTP 520, Request-ID req_123injected)"
    )
    assert "internal body" not in str(raised.value)
