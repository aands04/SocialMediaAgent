from pathlib import Path

from app.config import Settings, get_settings
from app.imagegen.service import AIImageRenderer, OpenAIImageProvider
from app.rendering.service import Renderer
from app.textgen.service import FixtureTextGenerator, OpenAITextGenerator


def build_renderer(settings: Settings | None = None):
    settings = settings or get_settings()
    if settings.image_generator_mode == "openai":
        if not settings.openai_api_key:
            raise ValueError(
                "IMAGE_GENERATOR_MODE=openai benötigt ein OPENAI_API_KEY-Secret"
            )
        return AIImageRenderer(
            settings.generated_root,
            settings.media_root,
            Path("data/uploads"),
            OpenAIImageProvider(settings.openai_api_key),
        )
    return Renderer(
        settings.generated_root, settings.media_root, Path("data/uploads")
    )


def build_text_generator(settings: Settings | None = None):
    settings = settings or get_settings()
    if settings.text_generator_mode == "openai":
        if not settings.openai_api_key:
            raise ValueError(
                "TEXT_GENERATOR_MODE=openai benötigt ein OPENAI_API_KEY-Secret"
            )
        return OpenAITextGenerator(settings.openai_api_key, settings.openai_model)
    return FixtureTextGenerator()
