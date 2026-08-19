from __future__ import annotations

import io
import subprocess
from dataclasses import dataclass
from typing import Any

from openai import OpenAI

from app.match_reports.telegram import TelegramApiError, TelegramBotClient


class TelegramVoiceTranscriptionError(RuntimeError):
    """Neutraler Fehler ohne Provider-Payload, Token oder Audiodaten."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class TelegramVoiceTranscription:
    text: str
    model: str
    duration_seconds: int | None


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _to_wav(content: bytes, *, timeout_seconds: float, max_bytes: int) -> bytes:
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                "pipe:0",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-f",
                "wav",
                "pipe:1",
            ],
            input=content,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError):
        raise TelegramVoiceTranscriptionError("conversion_failed") from None
    if result.returncode != 0 or not result.stdout or len(result.stdout) > max_bytes:
        raise TelegramVoiceTranscriptionError("conversion_failed")
    return result.stdout


def transcribe_telegram_voice(
    *,
    client: TelegramBotClient,
    message: dict[str, Any],
    settings,
) -> TelegramVoiceTranscription:
    if not settings.telegram_voice_transcription_enabled:
        raise TelegramVoiceTranscriptionError("disabled")
    if not settings.openai_api_key:
        raise TelegramVoiceTranscriptionError("service_not_configured")
    voice = message.get("voice")
    if not isinstance(voice, dict):
        raise TelegramVoiceTranscriptionError("invalid_voice")
    duration = _positive_int(voice.get("duration"))
    if duration is not None and duration > settings.telegram_voice_max_duration_seconds:
        raise TelegramVoiceTranscriptionError("duration_exceeded")
    provider_size = _positive_int(voice.get("file_size"))
    if provider_size is not None and provider_size > settings.telegram_voice_max_bytes:
        raise TelegramVoiceTranscriptionError("size_exceeded")
    file_id = str(voice.get("file_id") or "").strip()
    try:
        downloaded = client.download_file(file_id, max_bytes=settings.telegram_voice_max_bytes)
    except TelegramApiError:
        raise TelegramVoiceTranscriptionError("download_failed") from None
    wav = _to_wav(
        downloaded.content,
        timeout_seconds=settings.telegram_voice_transcription_timeout_seconds,
        max_bytes=settings.telegram_voice_max_bytes,
    )
    upload = io.BytesIO(wav)
    upload.name = "telegram-voice.wav"
    try:
        response = OpenAI(
            api_key=settings.openai_api_key,
            max_retries=0,
            timeout=settings.telegram_voice_transcription_timeout_seconds,
        ).audio.transcriptions.create(
            model=settings.telegram_voice_transcription_model,
            file=upload,
            language="de",
        )
    except Exception:
        raise TelegramVoiceTranscriptionError("provider_failed") from None
    text = str(getattr(response, "text", "") or "").strip()
    if not text:
        raise TelegramVoiceTranscriptionError("empty_transcript")
    return TelegramVoiceTranscription(
        text=text[:5000],
        model=settings.telegram_voice_transcription_model,
        duration_seconds=duration,
    )
