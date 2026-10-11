"""`app.core.images`: what an upload becomes before anything is stored.

Pure unit tests (no database, no network). The properties that matter:

* the CONTENT decides - a name, an extension or a declared type never does;
* only JPEG, PNG and WebP; SVG, GIF, TIFF, BMP, HEIC, PDF, text, an animated
  image and a damaged one are all refused, each with a short machine reason;
* the pixel count is judged from the header BEFORE any pixel is decoded;
* what comes out is a fresh JPEG: square, resized, no EXIF/GPS/XMP/comment/ICC,
  with the camera's orientation applied to the pixels.
"""

from __future__ import annotations

import io
import struct
import threading
import zlib
from typing import Any

import pytest
from PIL import Image, ImageCms

from app.core import images
from app.core.images import LARGE, SIZES, SMALL, ImageRejected, process_avatar, sniff
from tests.profile_support import jpeg_bytes, png_bytes

MAX_PIXELS = 25_000_000


def _reason(data: bytes, max_pixels: int = MAX_PIXELS) -> str:
    with pytest.raises(ImageRejected) as caught:
        process_avatar(data, max_pixels=max_pixels)
    return caught.value.reason


def _px(image: Image.Image, xy: tuple[int, int]) -> tuple[int, int, int]:
    value = image.getpixel(xy)
    assert isinstance(value, tuple)
    return (int(value[0]), int(value[1]), int(value[2]))


def _open(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def _png_claiming(width: int, height: int) -> bytes:
    """A few dozen bytes of PNG whose header CLAIMS a huge canvas - what a
    decompression bomb looks like before it is decoded."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b""))
        + chunk(b"IEND", b"")
    )


# --- accepted formats and what comes out ----------------------------------------------


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP"])
def test_each_accepted_format_becomes_two_square_jpegs(fmt: str) -> None:
    source = io.BytesIO()
    Image.new("RGB", (640, 480), (10, 120, 200)).save(source, fmt)
    result = process_avatar(source.getvalue(), max_pixels=MAX_PIXELS)

    assert result.source_format == fmt
    assert tuple(sorted(result.renditions)) == tuple(sorted(SIZES))
    for size, data in result.renditions.items():
        assert data[:3] == b"\xff\xd8\xff"  # a JPEG, whatever came in
        out = _open(data)
        assert out.format == "JPEG"
        assert out.size == (size, size)  # square
        assert out.mode == "RGB"
    assert SMALL < LARGE


def test_a_small_photo_is_not_enlarged() -> None:
    result = process_avatar(png_bytes((100, 90)), max_pixels=MAX_PIXELS)
    for data in result.renditions.values():
        assert _open(data).size == (90, 90)


def test_a_wide_photo_is_cropped_to_the_centre() -> None:
    wide = Image.new("RGB", (600, 200), (255, 255, 255))
    for x in range(200):  # a red band on the far left, outside the centre square
        for y in range(200):
            wide.putpixel((x, y), (255, 0, 0))
    source = io.BytesIO()
    wide.save(source, "PNG")
    out = _open(process_avatar(source.getvalue(), max_pixels=MAX_PIXELS).renditions[LARGE])
    red, green, blue = _px(out, (out.width // 2, out.height // 2))
    assert (red, green, blue) != (255, 0, 0) and min(red, green, blue) > 200


def test_a_transparent_png_is_flattened_on_white() -> None:
    out = _open(
        process_avatar(
            png_bytes((200, 200), (0, 0, 0, 0), "RGBA"), max_pixels=MAX_PIXELS
        ).renditions[SMALL]
    )
    r, g, b = _px(out, (out.width // 2, out.height // 2))
    assert min(r, g, b) > 245


# --- the content decides ------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"hello, this is not an image",
        b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>',
        b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
        b"GIF89a\x01\x00\x01\x00\x00\x00\x00;",
        b"BM" + b"\x00" * 60,
        b"II*\x00" + b"\x00" * 60,
        b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n",
        b"\x00\x00\x00\x18ftypheic" + b"\x00" * 60,
        b"MZ\x90\x00" + b"\x00" * 60,
    ],
)
def test_anything_that_is_not_jpeg_png_or_webp_is_unsupported(data: bytes) -> None:
    assert _reason(data) == "unsupported"


def test_gif_is_refused_even_when_it_is_a_real_image() -> None:
    source = io.BytesIO()
    Image.new("RGB", (200, 200), (1, 2, 3)).save(source, "GIF")
    assert _reason(source.getvalue()) == "unsupported"


def test_the_magic_bytes_of_a_format_with_a_damaged_body_are_corrupt_not_trusted() -> None:
    assert _reason(b"\x89PNG\r\n\x1a\n" + b"not a real png body" * 5) == "corrupt"
    assert _reason(b"\xff\xd8\xff\xe0" + b"garbage" * 20) == "corrupt"
    assert _reason(b"RIFF\x10\x00\x00\x00WEBPjunk" + b"\x00" * 20) == "corrupt"


def test_a_truncated_image_is_corrupt() -> None:
    data = jpeg_bytes((800, 600))
    assert _reason(data[: len(data) // 3]) == "corrupt"


def test_an_animated_image_is_refused() -> None:
    frames = [Image.new("RGB", (120, 120), (i * 40, 0, 0)) for i in range(4)]
    webp = io.BytesIO()
    frames[0].save(webp, "WEBP", save_all=True, append_images=frames[1:], duration=80)
    assert _reason(webp.getvalue()) == "animated"
    apng = io.BytesIO()
    frames[0].save(apng, "PNG", save_all=True, append_images=frames[1:], duration=80)
    assert _reason(apng.getvalue()) == "animated"


def test_a_photo_that_is_too_small_to_be_useful_is_refused() -> None:
    assert _reason(png_bytes((40, 40))) == "too_small"
    assert _reason(png_bytes((500, 30))) == "too_small"


# --- pixel limits are checked from the header, before decoding ---------------------------------


def test_a_file_that_claims_a_huge_canvas_is_refused_without_decoding_it() -> None:
    bomb = _png_claiming(60_000, 60_000)  # 3.6 gigapixels in ~70 bytes
    assert len(bomb) < 200
    assert _reason(bomb) == "too_many_pixels"


@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
def test_the_limit_is_the_configured_one() -> None:
    assert _reason(png_bytes((1100, 1000)), max_pixels=1_000_000) == "too_many_pixels"
    assert process_avatar(png_bytes((900, 1000)), max_pixels=1_000_000).renditions


def test_a_bomb_does_not_allocate_its_canvas(monkeypatch: pytest.MonkeyPatch) -> None:
    """The decoder is never asked to load the pixels of an over-limit image."""
    loaded: list[bool] = []
    original = Image.Image.load

    def spy(self: Image.Image, *args: Any, **kwargs: Any) -> Any:
        loaded.append(True)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "load", spy)
    assert _reason(_png_claiming(40_000, 40_000)) == "too_many_pixels"
    assert loaded == []


# --- metadata -----------------------------------------------------------------------------------


def _jpeg_with_metadata() -> bytes:
    image = Image.new("RGB", (400, 300), (90, 140, 60))
    exif = Image.Exif()
    exif[0x010F] = "SecretCameraMaker"  # Make
    exif[0x0132] = "2026:01:02 03:04:05"  # DateTime
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (40.0, 23.0, 42.0)
    gps[3] = "E"
    gps[4] = (49.0, 51.0, 0.0)
    out = io.BytesIO()
    image.save(
        out,
        "JPEG",
        exif=exif,
        comment=b"SecretComment",
        icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes(),
    )
    return out.getvalue()


def test_exif_gps_comment_and_icc_do_not_survive() -> None:
    source = _jpeg_with_metadata()
    # the fixture really carries what we claim to remove
    carried = Image.open(io.BytesIO(source))
    assert carried.getexif().get(0x010F) == "SecretCameraMaker"
    assert carried.getexif().get_ifd(0x8825)
    assert b"SecretComment" in source

    for data in process_avatar(source, max_pixels=MAX_PIXELS).renditions.values():
        out = Image.open(io.BytesIO(data))
        assert len(out.getexif()) == 0
        assert "icc_profile" not in out.info
        assert "comment" not in out.info
        for marker in (
            b"SecretCameraMaker",
            b"SecretComment",
            b"Exif",
            b"GPS",
            b"xmpmeta",
            b"ICC_PROFILE",
        ):
            assert marker not in data


def test_png_text_chunks_do_not_survive() -> None:
    from PIL.PngImagePlugin import PngInfo

    info = PngInfo()
    info.add_text("Author", "SecretAuthor")
    info.add_text("Description", "SecretDescription")
    source = io.BytesIO()
    Image.new("RGB", (300, 300), (1, 2, 3)).save(source, "PNG", pnginfo=info)
    assert b"SecretAuthor" in source.getvalue()
    for data in process_avatar(source.getvalue(), max_pixels=MAX_PIXELS).renditions.values():
        assert b"SecretAuthor" not in data and b"SecretDescription" not in data


def test_the_exif_orientation_is_applied_to_the_pixels_not_kept() -> None:
    """A phone photo is stored sideways with an 'orientation' tag. The tag is
    removed with the rest of the metadata, so the rotation must be baked in."""
    image = Image.new("RGB", (300, 200), (0, 0, 255))
    for x in range(150):  # the LEFT half red
        for y in range(200):
            image.putpixel((x, y), (255, 0, 0))
    exif = Image.Exif()
    exif[0x0112] = 6  # to display, rotate 90 degrees clockwise
    out = io.BytesIO()
    image.save(out, "JPEG", exif=exif, quality=95)

    result = _open(process_avatar(out.getvalue(), max_pixels=MAX_PIXELS).renditions[LARGE])
    assert len(result.getexif()) == 0
    top = _px(result, (result.width // 2, 8))
    bottom = _px(result, (result.width // 2, result.height - 9))
    assert top[0] > 200 and top[2] < 80  # the red half is now ABOVE ...
    assert bottom[2] > 200 and bottom[0] < 80  # ... the blue half


def test_a_cmyk_or_palette_or_grayscale_source_becomes_rgb() -> None:
    for mode in ("L", "P", "CMYK", "LA"):
        source = io.BytesIO()
        image = Image.new("RGB", (200, 200), (120, 30, 30)).convert(mode)
        fmt = "JPEG" if mode in ("CMYK",) else "PNG"
        image.save(source, fmt)
        result = process_avatar(source.getvalue(), max_pixels=MAX_PIXELS)
        assert _open(result.renditions[SMALL]).mode == "RGB"


def test_the_output_is_optimised_and_progressive() -> None:
    data = process_avatar(jpeg_bytes((1200, 900)), max_pixels=MAX_PIXELS).renditions[LARGE]
    assert _open(data).info.get("progressive") or _open(data).info.get("progression")
    assert len(data) < 40_000  # a 384 px avatar, not a camera original


# --- concurrency gate -------------------------------------------------------------------


def test_when_the_decode_slots_are_taken_the_upload_is_turned_away_not_queued_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = threading.BoundedSemaphore(1)
    gate.acquire()
    monkeypatch.setattr(images, "_gate", gate)
    monkeypatch.setattr(images, "_WAIT_SECONDS", 0.05)
    assert _reason(png_bytes()) == "busy"
    gate.release()
    assert process_avatar(png_bytes(), max_pixels=MAX_PIXELS).renditions


def test_the_slot_is_released_after_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = threading.BoundedSemaphore(1)
    monkeypatch.setattr(images, "_gate", gate)
    monkeypatch.setattr(images, "_WAIT_SECONDS", 0.05)
    for _ in range(3):
        assert _reason(b"\x89PNG\r\n\x1a\n" + b"junk" * 20) == "corrupt"
    assert process_avatar(png_bytes(), max_pixels=MAX_PIXELS).renditions  # still available


def test_sniff_reads_only_the_bytes() -> None:
    assert sniff(png_bytes()) == "PNG"
    assert sniff(jpeg_bytes()) == "JPEG"
    webp = io.BytesIO()
    Image.new("RGB", (80, 80)).save(webp, "WEBP")
    assert sniff(webp.getvalue()) == "WEBP"
    assert sniff(b"<svg/>") is None
    assert sniff(b"") is None
