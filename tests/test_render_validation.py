
import pytest
from PIL import Image

from app.rendering.service import Renderer, RenderValidationError
from app.textgen.service import FixtureTextGenerator


def facts(): return {"home_team":"SV Test","away_team":"FC Fixture","kickoff":"2026-08-01T18:00:00+00:00","post_type":"announcement"}

def test_render_validation_rejects_wrong_size_empty_and_solid(tmp_path):
    renderer=Renderer(tmp_path); valid=renderer.render("feed","valid.png",facts()); report=renderer.validate(valid,"feed")
    assert report["width"]==1080 and report["height"]==1350 and report["bytes"]>0
    wrong=tmp_path/"wrong.png"; Image.new("RGB",(100,100),"red").save(wrong)
    with pytest.raises(RenderValidationError,match="Auflösung"): renderer.validate(wrong,"feed")
    solid=tmp_path/"solid.png"; Image.new("RGB",(1080,1350),"red").save(solid)
    with pytest.raises(RenderValidationError,match="einfarbig"): renderer.validate(solid,"feed")

def test_render_validation_rejects_missing_or_clipped_required_text(tmp_path):
    renderer=Renderer(tmp_path)
    with pytest.raises(RenderValidationError,match="Pflichtangaben"): renderer.render("story","missing.png",{})
    data=facts(); data["home_team"]="X"*1000
    with pytest.raises(RenderValidationError,match="Textbereich"): renderer.render("story","clipped.png",data)


def test_feed_story_player_image_logo_fallback_and_long_names(tmp_path):
    player=tmp_path/"player.png"; Image.new("RGB",(600,900),(240,20,30)).save(player)
    data=facts() | {
        "player_image":str(player),
        "home_team":"Sportgemeinschaft mit einem außergewöhnlich langen Mannschaftsnamen",
        "away_team":"Fußballclub Beispielstadt Zweite Mannschaft",
        "primary_color":"#103b2d",
        "secondary_color":"#ffffff",
        "competition":"Kreisliga A",
    }
    renderer=Renderer(tmp_path/"out",media_root=tmp_path,upload_root=tmp_path)
    feed=renderer.render("feed","feed.png",data); story=renderer.render("story","story.png",data)
    with Image.open(feed) as image:
        assert image.size==(1080,1350)
        assert any(r>g*2 and r>b*2 for _,(r,g,b) in image.convert("RGB").getcolors(image.width*image.height))
    assert Image.open(story).size==(1080,1920)


def test_missing_font_falls_back_and_fixture_text_uses_berlin_time(tmp_path):
    data=facts() | {"primary_font_asset":{"family":"Fehlt","path":str(tmp_path/"missing.woff2")}}
    assert Renderer(tmp_path/"out",media_root=tmp_path,upload_root=tmp_path).render("feed","fallback.png",data).is_file()
    text=FixtureTextGenerator().generate(facts()).text
    assert "01.08.2026" in text and "20:00 Uhr" in text
    assert "T18:00:00" not in text and "+00:00" not in text


def test_assets_are_confined_to_allowed_roots_and_symlink_escapes_are_rejected(tmp_path):
    media=tmp_path/"media"; uploads=tmp_path/"uploads"; outside=tmp_path/"outside"
    media.mkdir(); uploads.mkdir(); outside.mkdir()
    secret=outside/"secret.png"; Image.new("RGB",(20,20),"red").save(secret)
    renderer=Renderer(tmp_path/"out",media,uploads)
    with pytest.raises(RenderValidationError,match="außerhalb"):
        renderer.render("feed","absolute.png",facts()|{"player_image":str(secret)})
    link=media/"escape.png"; link.symlink_to(secret)
    with pytest.raises(RenderValidationError,match="außerhalb"):
        renderer.render("feed","symlink.png",facts()|{"player_image":str(link)})
    invalid=media/"asset.svg"; invalid.write_text("<svg/>")
    with pytest.raises(RenderValidationError,match="Dateityp"):
        renderer.render("feed","type.png",facts()|{"team_logo":str(invalid)})
    with pytest.raises(RenderValidationError,match="Ausgabepfad"):
        renderer.render("feed","../escape.png",facts())


def test_template_scripts_and_file_resources_are_removed(tmp_path):
    malicious={"name":"test","version":1,"html_template":"<main class='canvas'><script>document.body.innerHTML='';throw new Error('executed')</script><img src='file:///etc/passwd'><div class='teams'>{{ home_team }} gegen {{ away_team }}</div></main>","css":".canvas{width:100%;height:100%;background:linear-gradient(red,blue);color:white}.teams{font-size:60px}","media_kind":"feed"}
    rendered=Renderer(tmp_path).render("feed","safe.png",facts()|{"template":malicious})
    assert rendered.is_file() and Image.open(rendered).size==(1080,1350)
