"""Profile photo processing: an upload becomes new, safe JPEG renditions.

The upload is NEVER stored or served as received. It is decoded, checked,
cropped to a square, resized and RE-ENCODED, so what leaves this module is
bytes this module itself produced:

* **The content decides, not the name or the Content-Type.** The first bytes
  must be those of a JPEG, a PNG or a WebP, and the decoder - restricted to
  exactly those three formats - must agree. SVG (which is a document that can
  carry script), GIF, TIFF, BMP, HEIC, PDF and anything else are refused, and so
  is an animated image.
* **Size is bounded twice, before any pixel is decoded.** The request body is
  capped in bytes by the caller; the dimensions read from the header are capped
  here in pixels, so a few kilobytes that claim to expand to a huge canvas (a
  "decompression bomb") are refused without allocating it. JPEGs are
  additionally decoded at a reduced scale. At most two images are processed at
  the same time, so a rush of uploads cannot exhaust memory.
* **Metadata is removed.** EXIF (GPS position, camera, time), XMP, IPTC,
  comments and the embedded ICC profile do not survive: the colours are
  converted to sRGB and the output JPEGs are written without any of them. The
  EXIF orientation is applied to the pixels first, so the photo is not turned.
* **Optimised.** Two renditions - a thumbnail for lists and a larger one for
  cards - progressive, optimised JPEG at quality 85; a small source is not
  enlarged.
"""

from __future__ import annotations

import io
import threading
from dataclasses import dataclass

from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError

# What a photo is served as: (thumbnail, card/profile). Kept in one place: the
# URL helper, the store and the tests all read it.
SMALL = 128
LARGE = 384
SIZES: tuple[int, ...] = (SMALL, LARGE)
MIN_SIDE = 64
JPEG_QUALITY = 85
ACCEPTED_FORMATS = ("JPEG", "PNG", "WEBP")
_MAX_CONCURRENT = 2
_WAIT_SECONDS = 30.0

_gate = threading.BoundedSemaphore(_MAX_CONCURRENT)


class ImageRejected(Exception):  # noqa: N818 - a refusal, not an error condition
    """The upload cannot be used. `reason` is a short machine code; the text of
    the exception never carries anything from the file."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ProcessedImage:
    renditions: dict[int, bytes]  # size -> JPEG bytes
    source_format: str
    source_size: tuple[int, int]


def sniff(data: bytes) -> str | None:
    """The format the first bytes claim, or None. A name or a Content-Type is
    not consulted anywhere."""
    if data[:3] == b"\xff\xd8\xff":
        return "JPEG"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "PNG"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WEBP"
    return None


def process_avatar(data: bytes, *, max_pixels: int) -> ProcessedImage:
    """Raises `ImageRejected` (reason: unsupported | corrupt | animated |
    too_small | too_many_pixels | busy)."""
    claimed = sniff(data)
    if claimed is None:
        raise ImageRejected("unsupported")
    if not _gate.acquire(timeout=_WAIT_SECONDS):
        raise ImageRejected("busy")
    try:
        return _process(data, claimed, max_pixels)
    finally:
        _gate.release()


def _process(data: bytes, claimed: str, max_pixels: int) -> ProcessedImage:
    # The global limit is Pillow's own second line of defence; our explicit check
    # below comes first and is stricter.
    Image.MAX_IMAGE_PIXELS = max_pixels
    try:
        opened = Image.open(io.BytesIO(data), formats=list(ACCEPTED_FORMATS))
    except Image.DecompressionBombError:
        # Pillow's own refusal (more than twice the limit): the header alone said so.
        raise ImageRejected("too_many_pixels") from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        raise ImageRejected("corrupt") from None
    image: Image.Image = opened
    if opened.format != claimed:
        raise ImageRejected("unsupported")  # the bytes and the decoder disagree
    width, height = image.size
    if width * height > max_pixels:
        raise ImageRejected("too_many_pixels")
    if min(width, height) < MIN_SIDE:
        raise ImageRejected("too_small")
    if getattr(image, "is_animated", False):
        raise ImageRejected("animated")
    source_format, source_size = claimed, (width, height)
    try:
        if claimed == "JPEG":
            # Decode at a reduced scale: the output is far smaller than a phone's
            # full-resolution photo, so there is no reason to hold all its pixels.
            opened.draft("RGB", (SIZES[-1] * 2, SIZES[-1] * 2))
        opened.load()
        icc = opened.info.get("icc_profile")
        image = ImageOps.exif_transpose(opened) or opened
        image = _to_srgb_rgb(image, icc)
        renditions = {size: _encode(image, size) for size in SIZES}
    except ImageRejected:
        raise
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError, MemoryError):
        raise ImageRejected("corrupt") from None
    return ProcessedImage(
        renditions=renditions, source_format=source_format, source_size=source_size
    )


def _to_srgb_rgb(image: Image.Image, icc: bytes | None) -> Image.Image:
    if image.mode == "P":
        image = image.convert("RGBA" if "transparency" in image.info else "RGB")
    if icc and image.mode in ("RGB", "RGBA", "CMYK", "L"):
        try:
            source = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            target = ImageCms.createProfile("sRGB")
            keep_alpha = image.mode == "RGBA"
            base = image.convert("RGB") if keep_alpha else image
            converted = ImageCms.profileToProfile(base, source, target, outputMode="RGB")
            if converted is not None:
                if keep_alpha:
                    converted.putalpha(image.getchannel("A"))
                image = converted
        except (ImageCms.PyCMSError, OSError, ValueError):
            pass  # an unreadable profile is dropped, not trusted
    if image.mode in ("RGBA", "LA"):
        flat = Image.new("RGB", image.size, (255, 255, 255))
        flat.paste(image, mask=image.getchannel("A"))
        return flat
    return image.convert("RGB")


def _encode(image: Image.Image, size: int) -> bytes:
    side = min(size, *image.size)  # never enlarge a small photo
    square = ImageOps.fit(image, (side, side), Image.Resampling.LANCZOS, centering=(0.5, 0.5))
    out = io.BytesIO()
    # No exif=, icc_profile= or comment: nothing from the source is carried over.
    square.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
    return out.getvalue()
