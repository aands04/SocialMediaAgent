import hashlib
from datetime import datetime, timedelta, timezone
from io import BytesIO

import pytest
from PIL import Image, ImageDraw

from app.config import Settings
from app.imagegen.service import (
    AIImageRenderer,
    ImageGenerationError,
    ImageProvider,
    _fit_full_bleed,
)
from app.logos.service import store_logo
from app.models import (
    ClubBrandingConfiguration,
    Game,
    InstagramPage,
    MediaAsset,
    PromptTemplate,
    Role,
    StoryRule,
    Team,
    User,
)
from app.posts.service import _facts, create_post
from app.prompts.service import (
    PromptValidationError,
    builtin_prompt,
    prompt_context,
    resolve_prompt,
    validate_template,
    venue_display,
)
from app.textgen.service import (
    FixtureTextGenerator,
    OpenAITextGenerator,
    caption_contains_internal_rules,
    sanitize_generated_caption,
)


def facts(**updates):
    data = {
        "home_team": "SV Ehlen",
        "away_team": "SG Beispiel",
        "own_team": "SV Ehlen",
        "kickoff": "2026-08-09T13:00:00+00:00",
        "competition": "Kreisliga A",
        "venue": "Sportplatz Ehlen",
        "home_venue_display": "Habichtswaldstadion",
        "pitch": "Rasenplatz",
        "primary_color": "#172554",
        "secondary_color": "#ffffff",
        "hashtags": ["#SVEhlen"],
    }
    return data | updates


def test_prompt_context_uses_exact_home_venue_german_date_and_placeholders():
    context = prompt_context(facts(), "feed")
    assert context["venue_display"] == "Habichtswaldstadion"
    assert context["weekday"] == "Sonntag"
    assert context["date_de"] == "09.08.2026"
    assert context["output_width"] == 1080
    prompt = builtin_prompt("image", "announcement", "feed", facts())
    assert "SV Ehlen gegen SG Beispiel" in prompt.rendered
    assert "Habichtswaldstadion" in prompt.rendered
    assert "Referenzbild 2" in prompt.rendered
    assert "kein drittes Referenzbild" in prompt.rendered
    assert "oben links und oben rechts" not in prompt.rendered
    assert prompt.policy_version == "verified-media-ai-references-v4-full-bleed-safe-layout"
    assert "{{" not in prompt.rendered


def test_away_venue_requires_pitch_and_formats_only_place():
    away = facts(
        home_team="TSV Immenhausen",
        away_team="SV Ehlen",
        venue="Sportplatz Immenhausen",
    )
    assert venue_display(away) == "RP Immenhausen"
    assert venue_display(away | {"pitch": "Kunstrasenplatz"}) == "KR Immenhausen"
    assert venue_display(away | {"venue": "Jahnstraße 10, 34376 Immenhausen"}) == "RP Immenhausen"
    with pytest.raises(PromptValidationError, match="Platzart"):
        venue_display(away | {"pitch": None})


def test_post_facts_prefer_configured_home_venue_over_provider_name(db):
    page = InstagramPage(
        internal_name="venue-test",
        display_name="Venue Test",
        username="venue_test",
        club="Beispielverein",
        active=True,
    )
    db.add(page)
    db.flush()
    team = Team(
        internal_name="venue-team",
        display_name="Beispielverein Erste",
        short_name="Erste",
        slug="venue-team",
        club="Beispielverein",
        fussball_url="https://example.invalid/venue-team",
        instagram_page_id=page.id,
        media_subdir="venue-team",
    )
    db.add(team)
    db.flush()
    game = Game(
        team_id=team.id,
        provider="fixture",
        external_id="configured-home-venue",
        home_team=team.display_name,
        away_team="Gastverein",
        kickoff=datetime.now(timezone.utc) + timedelta(days=2),
        competition="Bezirksliga",
        venue="RP Anbieter-Ortsname",
        pitch="Rasenplatz",
        source_url="https://example.invalid/game",
    )
    db.add_all(
        [
            game,
            ClubBrandingConfiguration(
                club_id=team.club_id,
                image_settings={
                    "primary_standard_font": "dejavu-sans",
                    "secondary_standard_font": "liberation-serif",
                },
                text_settings={
                    "home_venue": "Sportanlage Beispielstadt",
                    "home_venue_short": "Beispielstadion",
                },
            ),
        ]
    )
    db.commit()

    prepared = _facts(
        db,
        game,
        team,
        None,
        "announcement",
        {"team": {}, "opponent": {"fallback": True}},
    )

    assert prepared["venue"] == "RP Anbieter-Ortsname"
    assert prepared["home_venue_display"] == "Beispielstadion"
    assert venue_display(prepared) == "Beispielstadion"
    assert "DejaVu Sans" in prepared["primary_font_family"]
    assert "Liberation Serif" in prepared["secondary_font_family"]


def test_prompt_rejects_unknown_placeholders_and_resolves_latest_version(db):
    with pytest.raises(PromptValidationError, match="Unbekannte Platzhalter"):
        validate_template("Spiel {{ invented_fact }}")
    db.add_all(
        [
            PromptTemplate(
                name="sve-feed",
                prompt_kind="image",
                post_type="announcement",
                media_kind="feed",
                prompt_body="Version eins: {{ home_team }} gegen {{ away_team }}",
                model="gpt-image-2",
                quality="medium",
                version=1,
            ),
            PromptTemplate(
                name="sve-feed",
                prompt_kind="image",
                post_type="announcement",
                media_kind="feed",
                prompt_body="Version zwei: {{ venue_display }}",
                model="gpt-image-2",
                quality="high",
                version=2,
            ),
        ]
    )
    db.commit()
    resolved = resolve_prompt(db, "sve-feed", "image", "announcement", "feed", facts())
    assert resolved.version == 2
    assert resolved.quality == "high"
    assert "Version zwei: Habichtswaldstadion" in resolved.rendered


class FakeImageProvider(ImageProvider):
    def __init__(self, output_format="PNG"):
        self.calls = []
        self.output_format = output_format

    def generate(self, prompt, references, size, model, quality):
        self.calls.append(
            {
                "prompt": prompt,
                "references": references,
                "size": size,
                "model": model,
                "quality": quality,
            }
        )
        width, height = map(int, size.split("x"))
        image = Image.effect_noise((width, height), 90).convert("RGB")
        data = BytesIO()
        image.save(data, self.output_format)
        return data.getvalue()


@pytest.mark.parametrize(
    ("target_size", "edge_sample", "edge_color"),
    [
        ((1080, 1350), (5, 675), (255, 255, 0)),
        ((1080, 1920), (540, 5), (255, 0, 0)),
    ],
)
def test_ai_format_conversion_is_full_bleed_and_preserves_safe_area(
    target_size, edge_sample, edge_color
):
    source = Image.new("RGB", (1024, 1536), "white")
    draw = ImageDraw.Draw(source)
    draw.rectangle((0, 0, 160, 1536), fill=(255, 255, 0))
    draw.rectangle((864, 0, 1024, 1536), fill=(0, 255, 0))
    draw.rectangle((0, 0, 1024, 160), fill=(255, 0, 0))
    draw.rectangle((0, 1376, 1024, 1536), fill=(0, 0, 255))

    marker_size = 64
    markers = {
        (255, 0, 255): (160, 160, 160 + marker_size, 160 + marker_size),
        (0, 255, 255): (800, 160, 800 + marker_size, 160 + marker_size),
        (128, 0, 128): (160, 1312, 160 + marker_size, 1312 + marker_size),
        (255, 128, 0): (800, 1312, 800 + marker_size, 1312 + marker_size),
    }
    for color, box in markers.items():
        draw.rectangle(box, fill=color)

    converted = _fit_full_bleed(source, target_size)

    assert converted.size == target_size
    assert converted.getpixel(edge_sample) == edge_color
    colors = set(converted.get_flattened_data())
    assert set(markers).issubset(colors)


def test_ai_renderer_uses_reference_images_and_enforces_exact_output(tmp_path):
    media = tmp_path / "media"
    uploads = tmp_path / "uploads"
    media.mkdir()
    uploads.mkdir()
    player = media / "player.jpg"
    team_logo = uploads / "team-logo.png"
    opponent_logo = uploads / "opponent-logo.png"
    Image.new("RGB", (600, 900), "blue").save(player)
    Image.new("RGBA", (200, 200), (255, 255, 255, 255)).save(team_logo)
    Image.new("RGBA", (180, 210), (20, 180, 60, 255)).save(opponent_logo)
    provider = FakeImageProvider("WEBP")
    renderer = AIImageRenderer(tmp_path / "out", media, uploads, provider)
    prompt = builtin_prompt(
        "image",
        "announcement",
        "feed",
        facts(team_logo=str(team_logo), opponent_logo=str(opponent_logo)),
    )
    output = renderer.render(
        "feed",
        "post/feed.png",
        {
            "player_image": str(player),
            "team_logo": str(team_logo),
            "opponent_logo": str(opponent_logo),
            "logos": {
                "team": {"id": "team-1", "version": 2, "checksum": "a" * 64},
                "opponent": {
                    "id": "opponent-1",
                    "version": 3,
                    "checksum": "b" * 64,
                },
            },
            "image_prompt": prompt,
        },
    )
    with Image.open(output) as normalized:
        assert normalized.size == (1080, 1350)
        assert normalized.format == "PNG"
    assert provider.calls[0]["size"] == "1088x1360"
    assert provider.calls[0]["model"] == "gpt-image-2"
    assert "Referenzbild 3" in provider.calls[0]["prompt"]
    assert provider.calls[0]["references"] == [
        player.resolve(),
        team_logo.resolve(),
        opponent_logo.resolve(),
    ]
    metadata = renderer.metadata_for(output)
    assert metadata["logo_integration"]["mode"] == "ai-reference"
    assert [item["role"] for item in metadata["logo_integration"]["reference_order"]] == [
        "player",
        "team_logo",
        "opponent_logo",
    ]
    assert "ai_base_path" not in metadata


def test_ai_renderer_passes_verified_sponsor_as_compositional_reference(tmp_path):
    media = tmp_path / "media"
    uploads = tmp_path / "uploads"
    media.mkdir()
    uploads.mkdir()
    player = media / "player.jpg"
    team_logo = uploads / "team-logo.png"
    sponsor_logo = uploads / "sponsor-logo.png"
    Image.new("RGB", (600, 900), "blue").save(player)
    Image.new("RGBA", (200, 200), (255, 255, 255, 255)).save(team_logo)
    Image.new("RGBA", (300, 120), (20, 180, 60, 255)).save(sponsor_logo)
    sponsor_checksum = hashlib.sha256(sponsor_logo.read_bytes()).hexdigest()
    provider = FakeImageProvider()
    renderer = AIImageRenderer(tmp_path / "out", media, uploads, provider)
    sponsor = {
        "name": "Beispielsponsor",
        "media_asset_id": "sponsor-1",
        "path": str(sponsor_logo),
        "checksum": sponsor_checksum,
        "placement": "right",
        "placement_instruction": (
            "Bevorzugt im rechten Bildbereich; die genaue Position frei bestimmen."
        ),
    }
    prompt_facts = facts(
        team_logo=str(team_logo),
        opponent_logo=None,
        sponsor_references=[sponsor],
    )
    prompt = builtin_prompt("image", "announcement", "feed", prompt_facts)

    output = renderer.render(
        "feed",
        "post/sponsor-feed.png",
        {
            **prompt_facts,
            "player_image": str(player),
            "logos": {"team": {"id": "team-1", "checksum": "a" * 64}},
            "image_prompt": prompt,
        },
    )

    assert provider.calls[0]["references"] == [
        player.resolve(),
        team_logo.resolve(),
        sponsor_logo.resolve(),
    ]
    assert "Referenzbild 3 ist das verifizierte Originallogo von Beispielsponsor" in prompt.rendered
    assert "keine starren Koordinaten" in prompt.rendered
    integration = renderer.metadata_for(output)["logo_integration"]
    assert integration["sponsor_count"] == 1
    assert integration["fixed_logo_positions"] is False
    assert [item["role"] for item in integration["reference_order"]] == [
        "player",
        "team_logo",
        "sponsor_logo",
    ]


def test_ai_renderer_rejects_modified_sponsor_reference(tmp_path):
    media = tmp_path / "media"
    uploads = tmp_path / "uploads"
    media.mkdir()
    uploads.mkdir()
    player = media / "player.jpg"
    team_logo = uploads / "team-logo.png"
    sponsor_logo = uploads / "sponsor-logo.png"
    Image.new("RGB", (600, 900), "blue").save(player)
    Image.new("RGBA", (200, 200), "white").save(team_logo)
    Image.new("RGBA", (200, 100), "green").save(sponsor_logo)
    renderer = AIImageRenderer(tmp_path / "out", media, uploads, FakeImageProvider())
    prompt = builtin_prompt("image", "announcement", "feed", facts())

    with pytest.raises(ImageGenerationError, match="Prüfsumme des Sponsorenlogos"):
        renderer.render(
            "feed",
            "post/invalid-sponsor.png",
            {
                "player_image": str(player),
                "team_logo": str(team_logo),
                "sponsor_references": [
                    {
                        "name": "Verändert",
                        "path": str(sponsor_logo),
                        "checksum": "0" * 64,
                    }
                ],
                "image_prompt": prompt,
            },
        )


def test_ai_renderer_reuses_only_output_from_same_generation_job(tmp_path):
    media = tmp_path / "media"
    uploads = tmp_path / "uploads"
    output_root = tmp_path / "out"
    media.mkdir()
    uploads.mkdir()
    player = media / "player.jpg"
    team_logo = uploads / "team-logo.png"
    Image.new("RGB", (600, 900), "blue").save(player)
    Image.new("RGBA", (200, 200), (255, 255, 255, 255)).save(team_logo)
    provider = FakeImageProvider()
    renderer = AIImageRenderer(output_root, media, uploads, provider)
    prompt = builtin_prompt(
        "image",
        "announcement",
        "feed",
        facts(team_logo=str(team_logo), opponent_logo=None),
    )
    legacy_path = output_root / "post" / "feed-v8.png"
    legacy_path.parent.mkdir(parents=True)
    Image.new("RGB", (1080, 1350), "red").save(legacy_path)
    context = {
        "player_image": str(player),
        "team_logo": str(team_logo),
        "opponent_logo": None,
        "logos": {
            "team": {"id": "team-1", "version": 1, "checksum": "a" * 64},
            "opponent": {"fallback": True, "name": "SG Beispiel"},
        },
        "image_prompt": prompt,
        "_generation_job_id": "rerender-job-1",
    }

    first = renderer.render("feed", "post/feed-v8.png", context)
    repeated = renderer.render("feed", "post/feed-v8.png", context)
    second_job = renderer.render(
        "feed",
        "post/feed-v8.png",
        {**context, "_generation_job_id": "rerender-job-2"},
    )

    assert first != legacy_path
    assert first == repeated
    assert second_job not in {legacy_path, first}
    assert "-job-" in first.name
    assert len(provider.calls) == 2
    assert renderer.metadata_for(repeated)["reused_final"] is True
    assert renderer.metadata_for(repeated)["generation_job_id"] == "rerender-job-1"
    assert Image.open(legacy_path).getpixel((0, 0)) == (255, 0, 0)


def test_ai_renderer_uses_text_fallback_without_opponent_logo(tmp_path):
    media = tmp_path / "media"
    uploads = tmp_path / "uploads"
    media.mkdir()
    uploads.mkdir()
    player = media / "player.jpg"
    team_logo = uploads / "team-logo.png"
    Image.new("RGB", (600, 900), "blue").save(player)
    Image.new("RGBA", (200, 200), (255, 255, 255, 255)).save(team_logo)
    provider = FakeImageProvider()
    renderer = AIImageRenderer(tmp_path / "out", media, uploads, provider)
    prompt = builtin_prompt(
        "image",
        "announcement",
        "story",
        facts(team_logo=str(team_logo), opponent_logo=None),
    )
    output = renderer.render(
        "story",
        "post/story.png",
        {
            "player_image": str(player),
            "team_logo": str(team_logo),
            "opponent_logo": None,
            "logos": {
                "team": {"id": "team-1", "version": 1, "checksum": "a" * 64},
                "opponent": {"fallback": True, "name": "SG Beispiel"},
            },
            "image_prompt": prompt,
        },
    )
    assert provider.calls[0]["references"] == [
        player.resolve(),
        team_logo.resolve(),
    ]
    assert renderer.metadata_for(output)["logo_integration"]["opponent_text_fallback"] is True
    assert "Erfinde dafür kein Wappen" in prompt.rendered


def test_ai_renderer_refuses_missing_player(tmp_path):
    media = tmp_path / "media"
    uploads = tmp_path / "uploads"
    media.mkdir()
    uploads.mkdir()
    renderer = AIImageRenderer(tmp_path / "out", media, uploads, FakeImageProvider())
    with pytest.raises(ValueError, match="Spielerbild"):
        renderer.render(
            "story",
            "post/story.png",
            {
                "player_image": None,
                "image_prompt": builtin_prompt("image", "announcement", "story", facts()),
            },
        )


def test_ai_renderer_refuses_missing_verified_team_logo(tmp_path):
    media = tmp_path / "media"
    uploads = tmp_path / "uploads"
    media.mkdir()
    uploads.mkdir()
    player = media / "player.jpg"
    Image.new("RGB", (600, 900), "blue").save(player)
    renderer = AIImageRenderer(tmp_path / "out", media, uploads, FakeImageProvider())
    with pytest.raises(ValueError, match="Mannschaftslogo"):
        renderer.render(
            "feed",
            "post/feed.png",
            {
                "player_image": str(player),
                "team_logo": None,
                "image_prompt": builtin_prompt("image", "announcement", "feed", facts()),
            },
        )


def test_post_creation_freezes_image_prompt_versions(db, tmp_path, monkeypatch):
    media = tmp_path / "media"
    uploads = tmp_path / "uploads"
    media.mkdir()
    uploads.mkdir()
    player = media / "player.jpg"
    team_logo_file = uploads / "source-team-logo.png"
    Image.new("RGB", (600, 900), "blue").save(player)
    Image.new("RGBA", (200, 200), (20, 70, 180, 255)).save(team_logo_file)
    monkeypatch.setattr(
        "app.posts.service.get_settings",
        lambda: Settings(media_root=media, upload_root=uploads),
    )
    user = User(
        email="prompt-test@example.invalid",
        password_hash="x",
        role=Role.ADMIN,
        all_teams=True,
    )
    page = InstagramPage(
        internal_name="main",
        display_name="Hauptseite",
        username="sve",
        club="SV Ehlen",
        active=True,
        connection_status="connected",
    )
    db.add_all([user, page])
    db.flush()
    team = Team(
        internal_name="erste",
        display_name="SV Ehlen",
        short_name="SVE",
        slug="sve",
        club="SV Ehlen",
        fussball_url="https://www.fussball.de/team",
        instagram_page_id=page.id,
        media_subdir="erste",
        rules={"feed_before_minutes": 1440},
    )
    db.add(team)
    db.flush()
    team_logo, _ = store_logo(
        db,
        upload_root=uploads,
        logo_type="team",
        team_id=team.id,
        display_name=team.club,
        original_filename="team-logo.png",
        content_type="image/png",
        data=team_logo_file.read_bytes(),
        uploaded_by=user.id,
    )
    team.logo_asset_id = team_logo.id
    game = Game(
        team_id=team.id,
        external_id="ai-1",
        home_team="SV Ehlen",
        away_team="SG Beispiel",
        kickoff=datetime.now(timezone.utc) + timedelta(days=3),
        competition="Kreisliga A",
        venue="Ehlen",
        pitch="Rasenplatz",
        source_url=team.fussball_url,
    )
    db.add(game)
    db.flush()
    db.add_all(
        [
            MediaAsset(
                team_id=team.id,
                relative_path="player.jpg",
                filename="player.jpg",
                mime_type="image/jpeg",
                size=player.stat().st_size,
                checksum="x" * 64,
                mtime=datetime.now(timezone.utc),
            ),
            StoryRule(
                team_id=team.id,
                name="24h",
                post_type="announcement",
                reference="kickoff",
                direction="before",
                offset_minutes=360,
                template="default-story",
            ),
        ]
    )
    db.commit()
    post = create_post(
        db,
        game,
        team,
        FixtureTextGenerator(),
        AIImageRenderer(tmp_path / "out", media, uploads, FakeImageProvider()),
    )
    assert post.design_snapshot["mode"]["image"] == "openai"
    assert post.design_snapshot["prompts"]["feed"]["name"] == "default-image-feed"
    assert post.design_snapshot["prompts"]["feed"]["version"] == 3
    assert (
        post.design_snapshot["prompts"]["feed"]["policy_version"]
        == "verified-media-ai-references-v4-full-bleed-safe-layout"
    )
    prompt_snapshot = post.design_snapshot["prompts"]["feed"]
    assert "rendered" not in prompt_snapshot
    assert "body" not in prompt_snapshot
    assert len(prompt_snapshot["template_checksum"]) == 64
    assert len(prompt_snapshot["rendered_checksum"]) == 64
    assert post.design_snapshot["stories"][0]["prompt"]["name"] == "default-image-story"
    assert post.design_snapshot["media"]["feed"]["logo_integration"]["mode"] == "ai-reference"
    assert Image.open(post.feed_path).size == (1080, 1350)


def test_openai_text_generator_uses_resolved_prompt_without_live_request():
    prompt = builtin_prompt("text", "announcement", "none", facts())
    generator = OpenAITextGenerator("test-key", "unused")

    class Responses:
        def create(self, **options):
            model = options["model"]
            input = options["input"]
            assert model == prompt.model
            assert input == prompt.rendered
            assert options["max_output_tokens"] == 1600
            assert options["reasoning"] == {"effort": "low"}
            assert options["text"] == {"verbosity": "low"}
            return type(
                "Response",
                (),
                {
                    "output_text": "Kopierbarer Testtext",
                    "usage": type("Usage", (), {"total_tokens": 42})(),
                },
            )()

    generator.client = type("Client", (), {"responses": Responses()})()
    result = generator.generate({"text_prompt": prompt})
    assert result.text == "Kopierbarer Testtext"
    assert result.prompt_version == "default-text-announcement:v3"
    assert result.tokens == 42


def test_openai_text_generator_falls_back_to_chat_after_responses_520():
    prompt = builtin_prompt("text", "announcement", "none", facts())
    generator = OpenAITextGenerator("test-key", "unused")
    error = RuntimeError("provider body must not be exposed")
    error.status_code = 520

    class Responses:
        def create(self, **_options):
            raise error

    captured = {}

    class Completions:
        def create(self, **options):
            captured.update(options)
            return type(
                "Completion",
                (),
                {
                    "choices": [
                        type(
                            "Choice",
                            (),
                            {"message": type("Message", (), {"content": "Fallback-Text"})()},
                        )()
                    ],
                    "usage": type("Usage", (), {"total_tokens": 55})(),
                },
            )()

    generator.client = type(
        "Client",
        (),
        {
            "responses": Responses(),
            "chat": type("Chat", (), {"completions": Completions()})(),
        },
    )()

    result = generator.generate({"text_prompt": prompt})

    assert result.text == "Fallback-Text"
    assert result.tokens == 55
    assert captured["model"] == prompt.model
    assert captured["messages"] == [{"role": "user", "content": prompt.rendered}]
    assert captured["max_completion_tokens"] == 1600
    assert captured["reasoning_effort"] == "low"
    assert captured["verbosity"] == "low"


def test_openai_text_generator_does_not_fallback_after_authentication_error():
    prompt = builtin_prompt("text", "announcement", "none", facts())
    generator = OpenAITextGenerator("test-key", "unused")
    error = RuntimeError("sensitive provider response")
    error.status_code = 401

    class Responses:
        def create(self, **_options):
            raise error

    class Completions:
        def create(self, **_options):
            raise AssertionError("Authentifizierungsfehler darf keinen zweiten API-Aufruf auslösen")

    generator.client = type(
        "Client",
        (),
        {
            "responses": Responses(),
            "chat": type("Chat", (), {"completions": Completions()})(),
        },
    )()

    with pytest.raises(RuntimeError, match="sensitive provider response"):
        generator.generate({"text_prompt": prompt})


def test_openai_text_generator_falls_back_when_response_hits_output_limit():
    prompt = builtin_prompt("text", "result", "none", facts(score="2:1"))
    generator = OpenAITextGenerator("test-key", "unused")

    class Responses:
        def create(self, **_options):
            return type(
                "Response",
                (),
                {
                    "status": "incomplete",
                    "output_text": "",
                    "incomplete_details": type("Incomplete", (), {"reason": "max_output_tokens"})(),
                },
            )()

    class Completions:
        def create(self, **_options):
            return type(
                "Completion",
                (),
                {
                    "choices": [
                        type(
                            "Choice",
                            (),
                            {"message": type("Message", (), {"content": "Vollständiger Text"})()},
                        )()
                    ],
                    "usage": type("Usage", (), {"total_tokens": 61})(),
                },
            )()

    generator.client = type(
        "Client",
        (),
        {
            "responses": Responses(),
            "chat": type("Chat", (), {"completions": Completions()})(),
        },
    )()

    result = generator.generate({"text_prompt": prompt})

    assert result.text == "Vollständiger Text"
    assert result.tokens == 61


def test_generated_caption_strips_echoed_internal_rules():
    leaked = (
        "Was für ein Spieltag! ⚽\n\n"
        "VERBINDLICHE, SERVERSEITIG VALIDIERTE VEREINSTEXTREGELN:\n"
        "- Diese internen Regeln dürfen nicht veröffentlicht werden."
    )

    assert caption_contains_internal_rules(leaked) is True
    assert sanitize_generated_caption(leaked) == "Was für ein Spieltag! ⚽"


def test_generated_caption_rejects_rule_only_provider_output():
    with pytest.raises(ValueError, match="keinen verwendbaren öffentlichen Begleittext"):
        sanitize_generated_caption("VERBINDLICHE FAKTENREGELN:\n- Interne Anweisung")
