import hashlib
from io import BytesIO
from zipfile import ZIP_STORED, ZipFile

import pytest
from PIL import Image

from app.media.uploads import (
    PlayerImageUploadError,
    iter_player_images_from_zip,
    validate_player_image,
)


def photo_bytes(image_format="JPEG", *, size=(640, 480), orientation=None):
    output = BytesIO()
    options = {}
    if orientation is not None:
        exif = Image.Exif()
        exif[274] = orientation
        options["exif"] = exif
    if image_format == "MPO":
        options.update(save_all=True, append_images=[Image.new("RGB", (320, 320), "blue")])
    Image.new("RGB", size, "red").save(output, format=image_format, **options)
    return output.getvalue()


@pytest.mark.parametrize("extension", ["jpg", "jpeg", "JPG", "JPEG"])
@pytest.mark.parametrize("mime_type", ["image/jpeg", "image/jpg"])
def test_jpeg_extensions_are_equivalent(extension, mime_type):
    content = photo_bytes()
    image = validate_player_image(f"photo.{extension}", mime_type, content)
    assert image.content == content
    assert image.extension == ".jpg"
    assert image.mime_type == "image/jpeg"


@pytest.mark.parametrize("extension", ["jpg", "jpeg", "JPG", "JPEG"])
def test_mpo_jpeg_is_normalized_to_primary_photo(extension):
    content = photo_bytes("MPO", orientation=6)
    with Image.open(BytesIO(content)) as source:
        assert source.format == "MPO"
        assert source.n_frames == 2
    image = validate_player_image(f"photo.{extension}", "image/jpeg", content)
    assert image.original_filename == f"photo.{extension}"
    assert (image.width, image.height) == (480, 640)
    assert image.extension == ".jpg"
    assert image.mime_type == "image/jpeg"
    assert image.checksum == hashlib.sha256(image.content).hexdigest()
    with Image.open(BytesIO(image.content)) as result:
        assert result.format == "JPEG"
        assert getattr(result, "n_frames", 1) == 1
        assert result.size == (480, 640)
        assert result.getexif().get(274, 1) == 1
        red, green, blue = result.getpixel((200, 200))
        assert red > 240 and green < 10 and blue < 10


def test_mpo_jpeg_inside_zip_is_normalized():
    archive = BytesIO()
    with ZipFile(archive, "w", ZIP_STORED) as output:
        output.writestr("photos/IMG_7748.jpeg", photo_bytes("MPO"))
    images = list(iter_player_images_from_zip("photos.zip", "application/zip", archive))
    assert len(images) == 1
    assert images[0].original_filename == "IMG_7748.jpeg"
    with Image.open(BytesIO(images[0].content)) as image:
        assert image.format == "JPEG"


@pytest.mark.parametrize(
    "filename,mime_type,error",
    [
        ("photo.png", "image/jpeg", "Dateiendung"),
        ("photo.jpeg", "image/png", "MIME-Type"),
        ("photo.mpo", "image/jpeg", "Dateiendung"),
    ],
)
def test_mpo_requires_jpeg_extension_and_mime_type(filename, mime_type, error):
    with pytest.raises(PlayerImageUploadError, match=error):
        validate_player_image(filename, mime_type, photo_bytes("MPO"))


def test_mpo_dimensions_are_checked_before_decoding(monkeypatch):
    import app.media.uploads as uploads

    content = photo_bytes("MPO")
    monkeypatch.setattr(uploads, "MAX_PLAYER_IMAGE_EDGE", 600)
    with pytest.raises(PlayerImageUploadError, match="maximale Bildkante"):
        validate_player_image("photo.jpeg", "image/jpeg", content)


def test_normalized_mpo_must_still_respect_size_limit(monkeypatch):
    import app.media.uploads as uploads

    content = photo_bytes("MPO")
    original_save = Image.Image.save

    def oversized_save(image, output, *args, **kwargs):
        original_save(image, output, *args, **kwargs)
        output.write(b"x" * len(content))

    monkeypatch.setattr(uploads, "MAX_PLAYER_IMAGE_BYTES", len(content))
    monkeypatch.setattr(Image.Image, "save", oversized_save)
    with pytest.raises(PlayerImageUploadError, match="maximal 20 MiB"):
        validate_player_image("photo.jpeg", "image/jpeg", content)


@pytest.mark.parametrize("image_format,mime_type", [("PNG", "image/png"), ("WEBP", "image/webp")])
def test_animated_images_are_still_rejected(image_format, mime_type):
    content = BytesIO()
    Image.new("RGB", (640, 480), "red").save(
        content,
        format=image_format,
        save_all=True,
        append_images=[Image.new("RGB", (640, 480), "blue")],
        duration=100,
        loop=0,
    )
    with pytest.raises(PlayerImageUploadError, match="animierte Bilder"):
        validate_player_image(f"photo.{image_format.lower()}", mime_type, content.getvalue())


def test_unrelated_format_disguised_as_jpeg_is_rejected():
    with pytest.raises(PlayerImageUploadError, match="nur JPG"):
        validate_player_image("photo.jpeg", "image/jpeg", photo_bytes("GIF"))
