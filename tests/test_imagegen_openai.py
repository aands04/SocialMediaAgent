import base64
from types import SimpleNamespace
from unittest.mock import Mock

from app.imagegen.service import OpenAIImageProvider

PNG_BYTES = b"\x89PNG\r\n\x1a\nmock-image-data"


def image_response():
    return SimpleNamespace(
        data=[
            SimpleNamespace(
                b64_json=base64.b64encode(PNG_BYTES).decode("ascii")
            )
        ]
    )


def provider_with_mock_client():
    provider = OpenAIImageProvider.__new__(OpenAIImageProvider)
    provider.client = Mock()
    return provider


def test_gpt_image_2_generate_uses_embedded_png_without_response_format():
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
    assert "response_format" not in options
    assert "input_fidelity" not in options


def test_gpt_image_2_edit_omits_unsupported_parameters(tmp_path):
    reference = tmp_path / "player.png"
    reference.write_bytes(PNG_BYTES)
    provider = provider_with_mock_client()
    provider.client.images.edit.return_value = image_response()

    result = provider.generate(
        prompt="Testmotiv mit Spieler",
        references=[reference],
        size="1088x1920",
        model="gpt-image-2-2026-07-01",
        quality="medium",
    )

    assert result == PNG_BYTES
    options = provider.client.images.edit.call_args.kwargs
    assert options["output_format"] == "png"
    assert "response_format" not in options
    assert "input_fidelity" not in options
    assert len(options["image"]) == 1
    assert options["image"][0].closed


def test_older_gpt_image_edit_retains_high_input_fidelity(tmp_path):
    reference = tmp_path / "player.png"
    reference.write_bytes(PNG_BYTES)
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
    assert options["input_fidelity"] == "high"
    assert "response_format" not in options
