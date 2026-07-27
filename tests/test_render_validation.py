
import pytest
from PIL import Image

from app.rendering.service import Renderer, RenderValidationError


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
