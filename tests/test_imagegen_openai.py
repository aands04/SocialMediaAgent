import base64
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from openai import OpenAI
from PIL import Image

from app.imagegen.service import (
    REFERENCE_LOGO_MAX_EDGE,
    REFERENCE_PLAYER_MAX_EDGE,
    ImageGenerationError,
    OpenAIImageProvider,
)

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


def test_gpt_image_2_generate_requests_compressed_webp_without_response_format():
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
    assert "response_format" not in options
    assert "input_fidelity" not in options


def test_gpt_image_2_edit_omits_unsupported_parameters(tmp_path):
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
    assert options["output_format"] == "webp"
    assert options["output_compression"] == 60
    assert "response_format" not in options
    assert "input_fidelity" not in options
    assert len(options["image"]) == 3
    assert [item.name for item in options["image"]] == [
        "reference-1.jpg",
        "reference-2.png",
        "reference-3.png",
    ]
    assert all(item.closed for item in options["image"])


@pytest.mark.parametrize(
    "filename",
    [
        "player.jpg",
        "player.jpeg",
        "player.png",
        "player.webp",
    ],
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
    upload = provider.client.images.edit.call_args.kwargs["image"][0]
    assert upload.name == "reference-1.jpg"
    assert upload.closed


def test_edit_normalizes_and_bounds_reference_images(tmp_path):
    player = tmp_path / "player.jpg"
    logo = tmp_path / "logo.webp"
    write_reference(player, size=(3000, 1200))
    write_reference(logo, alpha=True, size=(900, 1600))
    captured = []
    provider = provider_with_mock_client()

    def capture_uploads(**options):
        for handle in options["image"]:
            captured.append((handle.name, handle.read()))
        return image_response()

    provider.client.images.edit.side_effect = capture_uploads

    result = provider.generate(
        prompt="Normalisierte Referenzen",
        references=[player, logo],
        size="1088x1360",
        model="gpt-image-2",
        quality="medium",
    )

    assert result == PNG_BYTES
    assert [item[0] for item in captured] == ["reference-1.jpg", "reference-2.png"]
    for index, (_name, content) in enumerate(captured, start=1):
        with Image.open(BytesIO(content)) as image:
            image.load()
            expected_max_edge = REFERENCE_PLAYER_MAX_EDGE if index == 1 else REFERENCE_LOGO_MAX_EDGE
            assert max(image.size) <= expected_max_edge
            assert not image.getexif()
    with Image.open(BytesIO(captured[1][1])) as logo_upload:
        assert "A" in logo_upload.getbands()


def test_edit_sends_named_image_parts_with_explicit_sdk_mime_types(tmp_path):
    player = tmp_path / "player.jpg"
    logo = tmp_path / "logo.png"
    write_reference(player)
    write_reference(logo, alpha=True)
    captured: dict[str, bytes | str] = {}

    def handler(request: httpx.Request):
        captured["content_type"] = request.headers["content-type"]
        captured["body"] = request.read()
        return httpx.Response(
            200,
            json={"created": 0, "data": [{"b64_json": base64.b64encode(PNG_BYTES).decode()}]},
        )

    provider = OpenAIImageProvider.__new__(OpenAIImageProvider)
    provider.client = OpenAI(
        api_key="test",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert (
        provider.generate(
            prompt="Multipart-Test",
            references=[player, logo],
            size="1088x1360",
            model="gpt-image-2",
            quality="medium",
        )
        == PNG_BYTES
    )

    body = captured["body"]
    assert isinstance(body, bytes)
    assert b'name="image[]"; filename="reference-1.jpg"' in body
    assert b"Content-Type: image/jpeg" in body
    assert b'name="image[]"; filename="reference-2.png"' in body
    assert b"Content-Type: image/png" in body


def test_edit_rejects_unsupported_reference_format_before_api_call(tmp_path):
    reference = tmp_path / "player.gif"
    reference.write_bytes(PNG_BYTES)
    provider = provider_with_mock_client()

    with pytest.raises(
        ImageGenerationError,
        match=r"Nicht unterstütztes Referenzbildformat: \.gif",
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


def test_older_gpt_image_edit_retains_high_input_fidelity(tmp_path):
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
    assert "response_format" not in options


def test_provider_disables_sdk_retries_for_multipart_uploads(monkeypatch):
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
