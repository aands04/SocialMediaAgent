import base64
import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from openai import OpenAI
from PIL import Image

from app.imagegen.service import (
    ImageGenerationError,
    OpenAIImageProvider,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\nmock-image-data"


def image_response():
    return SimpleNamespace(
        data=[SimpleNamespace(b64_json=base64.b64encode(PNG_BYTES).decode("ascii"))]
    )


def responses_image_response():
    return SimpleNamespace(
        output=[
            SimpleNamespace(
                type="image_generation_call",
                result=base64.b64encode(PNG_BYTES).decode("ascii"),
            )
        ]
    )


def provider_with_mock_client():
    provider = OpenAIImageProvider.__new__(OpenAIImageProvider)
    provider.client = Mock()
    provider.responses_model = "gpt-5.4-mini"
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


def test_gpt_image_2_references_use_responses_image_tool(tmp_path):
    references = [
        tmp_path / "player.png",
        tmp_path / "team-logo.png",
        tmp_path / "opponent-logo.png",
    ]
    for reference in references:
        write_reference(reference)
    provider = provider_with_mock_client()
    provider.client.responses.create.return_value = responses_image_response()

    result = provider.generate(
        prompt="Testmotiv mit Spieler",
        references=references,
        size="1088x1920",
        model="gpt-image-2-2026-07-01",
        quality="medium",
    )

    assert result == PNG_BYTES
    options = provider.client.responses.create.call_args.kwargs
    assert options["model"] == "gpt-5.4-mini"
    assert options["store"] is False
    assert options["tool_choice"] == {"type": "image_generation"}
    tool = options["tools"][0]
    assert tool["model"] == "gpt-image-2-2026-07-01"
    assert tool["action"] == "edit"
    assert tool["output_format"] == "webp"
    assert tool["output_compression"] == 60
    assert tool["size"] == "1024x1536"
    assert "input_fidelity" not in tool
    content = options["input"][0]["content"]
    assert "3 getrennte Eingabebilder" in content[0]["text"]
    assert len(content) == 4
    assert all(item["type"] == "input_image" for item in content[1:])
    assert content[1]["image_url"].startswith("data:image/jpeg;base64,")
    assert content[2]["image_url"].startswith("data:image/png;base64,")


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
    provider.client.responses.create.return_value = responses_image_response()

    result = provider.generate(
        prompt="Referenzbild mit explizitem MIME-Typ",
        references=[reference],
        size="1088x1360",
        model="gpt-image-2",
        quality="medium",
    )

    assert result == PNG_BYTES
    content = provider.client.responses.create.call_args.kwargs["input"][0]["content"]
    assert content[1]["image_url"].startswith("data:image/jpeg;base64,")


def test_edit_sends_multiple_normalized_references_as_separate_inputs(tmp_path):
    player = tmp_path / "player.jpg"
    logo = tmp_path / "logo.webp"
    write_reference(player, size=(3000, 1200))
    write_reference(logo, alpha=True, size=(900, 1600))
    provider = provider_with_mock_client()
    provider.client.responses.create.return_value = responses_image_response()

    result = provider.generate(
        prompt="Normalisierte Referenzen",
        references=[player, logo],
        size="1088x1360",
        model="gpt-image-2",
        quality="medium",
    )

    assert result == PNG_BYTES
    content = provider.client.responses.create.call_args.kwargs["input"][0]["content"]
    assert len(content) == 3
    assert content[1]["image_url"].startswith("data:image/jpeg;base64,")
    assert content[2]["image_url"].startswith("data:image/png;base64,")


def test_edit_does_not_use_multipart_image_endpoint(tmp_path):
    player = tmp_path / "player.jpg"
    logo = tmp_path / "logo.png"
    write_reference(player)
    write_reference(logo, alpha=True)
    provider = provider_with_mock_client()
    provider.client.responses.create.return_value = responses_image_response()

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

    provider.client.responses.create.assert_called_once()
    provider.client.images.edit.assert_not_called()


def test_responses_reference_request_is_valid_json_not_multipart(tmp_path):
    player = tmp_path / "player.jpg"
    logo = tmp_path / "logo.png"
    write_reference(player)
    write_reference(logo, alpha=True)
    captured: dict[str, object] = {}

    def handler(request: httpx.Request):
        captured["path"] = request.url.path
        captured["content_type"] = request.headers["content-type"]
        captured["payload"] = json.loads(request.read())
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
    provider.responses_model = "gpt-5.4-mini"

    with pytest.raises(ImageGenerationError) as raised:
        provider.generate(
            prompt="JSON-Transport-Test",
            references=[player, logo],
            size="1088x1360",
            model="gpt-image-2",
            quality="medium",
        )

    assert raised.value.provider_status_code == 520
    assert raised.value.provider_request_id == "req_transport_test"
    assert captured["path"] == "/v1/responses"
    assert captured["content_type"] == "application/json"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["tool_choice"] == {"type": "image_generation"}
    assert "input_fidelity" not in payload["tools"][0]
    assert len(payload["input"][0]["content"]) == 3


def test_single_reference_adds_safe_reference_instruction(tmp_path):
    reference = tmp_path / "player.jpg"
    write_reference(reference, size=(3000, 1200))
    provider = provider_with_mock_client()
    provider.client.responses.create.return_value = responses_image_response()

    provider.generate(
        prompt="Unveränderter Einzelreferenz-Prompt",
        references=[reference],
        size="1088x1360",
        model="gpt-image-2",
        quality="medium",
    )

    content = provider.client.responses.create.call_args.kwargs["input"][0]["content"]
    assert content[0]["text"].startswith("Unveränderter Einzelreferenz-Prompt")
    assert "1 getrennte Eingabebilder" in content[0]["text"]
    assert len(content) == 2


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

    provider.client.responses.create.assert_not_called()


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

    provider.client.responses.create.assert_not_called()


def test_older_gpt_image_edit_uses_high_input_fidelity(tmp_path):
    reference = tmp_path / "player.png"
    write_reference(reference)
    provider = provider_with_mock_client()
    provider.client.responses.create.return_value = responses_image_response()

    result = provider.generate(
        prompt="Kompatibilitätstest",
        references=[reference],
        size="1088x1360",
        model="gpt-image-1.5",
        quality="high",
    )

    assert result == PNG_BYTES
    tool = provider.client.responses.create.call_args.kwargs["tools"][0]
    assert tool["output_format"] == "webp"
    assert tool["output_compression"] == 60
    assert tool["input_fidelity"] == "high"


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
    provider.client.responses.create.side_effect = error

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
