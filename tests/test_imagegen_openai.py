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


class CapturingLog:
    def __init__(self):
        self.events = []

    def info(self, event, **fields):
        self.events.append({"level": "info", "event": event, **fields})

    def warning(self, event, **fields):
        self.events.append({"level": "warning", "event": event, **fields})


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


def test_gpt_image_2_generate_requests_native_feed_ratio_as_png():
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
    assert options["output_format"] == "png"
    assert "output_compression" not in options
    assert options["size"] == "1088x1360"
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
        size="1152x2048",
        model="gpt-image-2-2026-07-01",
        quality="medium",
    )

    assert result == PNG_BYTES
    options = provider.client.images.edit.call_args.kwargs
    assert options["model"] == "gpt-image-2-2026-07-01"
    assert options["output_format"] == "png"
    assert "output_compression" not in options
    assert options["size"] == "1152x2048"
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
    upload = provider.client.images.edit.call_args.kwargs["image"]
    assert upload.name == "reference-1.jpg"


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
    assert b"1088x1360" in body
    assert b"input_fidelity" not in body


def test_single_reference_uses_non_array_multipart_field(tmp_path):
    player = tmp_path / "player.jpg"
    write_reference(player)
    captured: dict[str, object] = {}

    def handler(request: httpx.Request):
        captured["body"] = request.read()
        return httpx.Response(
            200,
            request=request,
            json={"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}]},
        )

    provider = OpenAIImageProvider.__new__(OpenAIImageProvider)
    provider.client = OpenAI(
        api_key="test",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    provider.responses_model = "gpt-5.4-mini"

    assert (
        provider.generate(
            prompt="Einzeldatei-Transport",
            references=[player],
            size="1088x1360",
            model="gpt-image-2",
            quality="medium",
        )
        == PNG_BYTES
    )

    body = captured["body"]
    assert isinstance(body, bytes)
    assert b'name="image"' in body
    assert b'name="image[]"' not in body


def test_single_reference_falls_back_to_responses_after_unassigned_520(tmp_path):
    player = tmp_path / "player.jpg"
    write_reference(player)
    provider = provider_with_mock_client()
    error = RuntimeError("upstream 520")
    error.status_code = 520
    provider.client.images.edit.side_effect = error
    provider.client.responses.create.return_value = responses_image_response()

    result = provider.generate(
        prompt="Transport-Fallback",
        references=[player],
        size="1088x1360",
        model="gpt-image-2",
        quality="medium",
    )

    assert result == PNG_BYTES
    provider.client.responses.create.assert_called_once()
    options = provider.client.responses.create.call_args.kwargs
    assert options["model"] == "gpt-5.4-mini"
    assert options["tools"][0]["action"] == "edit"
    assert options["tools"][0]["model"] == "gpt-image-2"
    assert len(options["input"][0]["content"]) == 2
    assert options["input"][0]["content"][1]["image_url"].startswith("data:image/jpeg;base64,")


@pytest.mark.parametrize("status_code", [400, 401, 429, 500, 503])
def test_single_reference_does_not_fallback_for_other_provider_errors(tmp_path, status_code):
    player = tmp_path / "player.jpg"
    write_reference(player)
    provider = provider_with_mock_client()
    error = RuntimeError("provider error")
    error.status_code = status_code
    provider.client.images.edit.side_effect = error

    with pytest.raises(ImageGenerationError) as raised:
        provider.generate(
            prompt="Kein Transport-Fallback",
            references=[player],
            size="1088x1360",
            model="gpt-image-2",
            quality="medium",
        )

    assert raised.value.provider_status_code == status_code
    provider.client.responses.create.assert_not_called()


def test_single_reference_does_not_fallback_when_520_has_request_id(tmp_path):
    player = tmp_path / "player.jpg"
    write_reference(player)
    provider = provider_with_mock_client()
    error = RuntimeError("assigned provider request")
    error.status_code = 520
    error.request_id = "req_assigned"
    provider.client.images.edit.side_effect = error

    with pytest.raises(ImageGenerationError) as raised:
        provider.generate(
            prompt="Kein zweiter Provider-Aufruf",
            references=[player],
            size="1088x1360",
            model="gpt-image-2",
            quality="medium",
        )

    assert raised.value.provider_request_id == "req_assigned"
    provider.client.responses.create.assert_not_called()


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
    assert options["output_format"] == "png"
    assert "output_compression" not in options
    assert options["input_fidelity"] == "high"


def test_provider_disables_sdk_retries_for_persistent_cost_guard(monkeypatch):
    openai = Mock()
    monkeypatch.setattr("app.imagegen.service.OpenAI", openai)

    OpenAIImageProvider("secret", responses_model="gpt-5.4-mini")

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


def test_image_transport_logs_only_safe_metadata(monkeypatch, tmp_path):
    reference = tmp_path / "secret-player-name.webp"
    write_reference(reference)
    provider = provider_with_mock_client()
    error = RuntimeError("secret raw upstream response")
    error.status_code = 503
    error.request_id = "req_safe"
    provider.client.images.edit.side_effect = error
    captured = CapturingLog()
    monkeypatch.setattr("app.imagegen.service.log", captured)

    with pytest.raises(ImageGenerationError) as raised:
        provider.generate(
            prompt="SECRET PROMPT MUST NEVER BE LOGGED",
            references=[reference],
            size="1088x1360",
            model="gpt-image-2",
            quality="medium",
        )

    failed = next(
        item for item in captured.events if item["event"] == "openai_image_request_failed"
    )
    assert failed["transport"] == "images.edit"
    assert failed["provider_status_code"] == 503
    assert failed["provider_request_id"] == "req_safe"
    assert failed["reference_count"] == 1
    assert failed["reference_mime_types"] == ("image/jpeg",)
    assert failed["duration_ms"] >= 0
    assert raised.value.provider_operation_id == failed["operation_id"]
    assert raised.value.provider_transport == "images.edit"
    assert raised.value.provider_duration_ms is not None
    serialized = repr(captured.events)
    assert "SECRET PROMPT" not in serialized
    assert "secret-player-name" not in serialized
    assert "secret raw upstream response" not in serialized


def test_image_fallback_uses_same_diagnostic_operation_id(monkeypatch, tmp_path):
    reference = tmp_path / "player.jpg"
    write_reference(reference)
    provider = provider_with_mock_client()
    error = RuntimeError("upstream 520")
    error.status_code = 520
    provider.client.images.edit.side_effect = error
    provider.client.responses.create.return_value = responses_image_response()
    captured = CapturingLog()
    monkeypatch.setattr("app.imagegen.service.log", captured)

    assert (
        provider.generate(
            prompt="Fallback-Diagnose",
            references=[reference],
            size="1088x1360",
            model="gpt-image-2",
            quality="medium",
        )
        == PNG_BYTES
    )

    relevant = [
        item
        for item in captured.events
        if item["event"]
        in {
            "openai_image_request_started",
            "openai_image_request_failed",
            "openai_image_fallback_selected",
            "openai_image_request_succeeded",
        }
    ]
    assert {item["operation_id"] for item in relevant} == {relevant[0]["operation_id"]}
    assert any(
        item["event"] == "openai_image_fallback_selected"
        and item["to_transport"] == "responses.image_generation"
        for item in relevant
    )
    assert any(
        item["event"] == "openai_image_request_succeeded"
        and item["transport"] == "responses.image_generation"
        and item["fallback_used"] is True
        for item in relevant
    )
