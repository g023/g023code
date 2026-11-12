"""
Image loading for AnalyzeImage — read, measure, cap at 800 px, sniff the media type.

DeepSeek's Anthropic-format endpoint takes base64 ``image`` blocks whose
``media_type`` must be one of jpeg / png / gif / webp and must match the bytes.
Images that already fit the 800 px cap are sent as they are (no re-encode), so
the media type is sniffed from the magic bytes rather than assumed; a resized
image is always re-encoded as PNG.

Local paths go through :func:`g023_code.paths.resolve_in_project`, the same
confinement every other file tool uses. http(s) URLs are downloaded and capped
here so the server never receives an oversized raster.
"""

from __future__ import annotations

import base64
import io
import struct
from dataclasses import dataclass
from pathlib import Path

import httpx

from .paths import resolve_in_project

# Longest side sent to the model. Fewer pixels = fewer input tokens per image.
VISION_SEND_MAX_SIDE = 800


@dataclass
class LoadedImage:
    b64: str
    raw: bytes
    media_type: str = "image/png"
    original_bytes: int = 0
    original_size: tuple[int, int] = (0, 0)
    final_size: tuple[int, int] = (0, 0)
    downscaled: bool = False
    source: str = ""

    @property
    def bytes_sent(self) -> int:
        return len(self.raw)

    @property
    def data_uri(self) -> str:
        return f"data:{self.media_type};base64,{self.b64}"


def sniff_media_type(data: bytes) -> str | None:
    """Media type from magic bytes, or ``None`` when it is not a supported format."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def constrained_size(width: int, height: int, max_side: int = VISION_SEND_MAX_SIDE) -> tuple[int, int]:
    """Fit (width, height) into max_side x max_side. Never upscale; never above the cap."""
    max_side = VISION_SEND_MAX_SIDE if max_side <= 0 else min(int(max_side), VISION_SEND_MAX_SIDE)
    if width <= 0 or height <= 0:
        return width, height
    if width <= max_side and height <= max_side:
        return width, height
    scale = min(max_side / width, max_side / height)
    return max(1, round(width * scale)), max(1, round(height * scale))


def _png_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    w, h = struct.unpack(">II", data[16:24])
    return (int(w), int(h)) if w > 0 and h > 0 else None


_SOF_MARKERS = frozenset((0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF))


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    i, n = 2, len(data)
    while i + 8 <= n:
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker in _SOF_MARKERS:
            h, w = struct.unpack(">HH", data[i + 5 : i + 9])
            return (int(w), int(h)) if w > 0 and h > 0 else None
        if marker in (0xD8, 0xD9):
            i += 2
            continue
        i += 2 + struct.unpack(">H", data[i + 2 : i + 4])[0]
    return None


def image_dimensions(data: bytes) -> tuple[int, int]:
    """Width, height. Pillow first; PNG/JPEG headers when Pillow is missing; (0, 0) if unknown."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            return img.size
    except Exception:
        pass
    return _png_size(data) or _jpeg_size(data) or (0, 0)


def constrain_image_bytes(
    data: bytes, max_side: int = VISION_SEND_MAX_SIDE
) -> tuple[bytes, tuple[int, int], tuple[int, int]]:
    """Fit image bytes into max_side x max_side. Never upscale.

    Returns ``(bytes_to_send, original_size, final_size)``. Raises RuntimeError
    rather than ship a raster that is known (or cannot be shown not) to exceed
    the cap — install Pillow to downscale.
    """
    max_side = VISION_SEND_MAX_SIDE if max_side <= 0 else min(int(max_side), VISION_SEND_MAX_SIDE)
    orig = image_dimensions(data)
    w, h = orig
    if 0 < w <= max_side and 0 < h <= max_side:
        return data, orig, orig
    try:
        from PIL import Image
    except ImportError:
        Image = None  # type: ignore[assignment]
    if Image is not None:
        try:
            with Image.open(io.BytesIO(data)) as img:
                size = img.size
                orig = size if orig == (0, 0) else orig
                if max(size) <= max_side:
                    return data, orig, size
                img = img.convert("RGB")
                img.thumbnail((max_side, max_side), Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format="PNG", optimize=True)
                final = img.size
                if max(final) > max_side:
                    raise RuntimeError(f"resized image still has a side > {max_side}: {final[0]}x{final[1]}")
                return buf.getvalue(), orig, final
        except RuntimeError:
            raise
        except Exception:
            pass
    if w > max_side or h > max_side:
        raise RuntimeError(
            f"image {w}x{h} exceeds {max_side}px and cannot be resized "
            "(install pillow to downscale before a vision send)"
        )
    raise RuntimeError(
        f"cannot determine image size; refusing to send without a {max_side}px cap (install pillow)"
    )


async def _read_source(path_or_url: str, timeout: float) -> tuple[bytes, str]:
    if path_or_url.startswith(("http://", "https://")):
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(path_or_url)
            resp.raise_for_status()
            return resp.content, "url"
    # Same confinement as ReadFile: relative to the project root, no escapes.
    p = resolve_in_project(Path(path_or_url.strip()).expanduser())
    if not p.is_file():
        raise FileNotFoundError(f"Image not found: {p}")
    return p.read_bytes(), str(p)


async def load_image_for_vision(path_or_url: str, timeout: float = 30.0) -> LoadedImage:
    """Load a local (project) path or http(s) URL, capped at VISION_SEND_MAX_SIDE."""
    data, source = await _read_source(path_or_url, timeout)
    original = len(data)
    if sniff_media_type(data) is None:
        raise RuntimeError("not a supported image (jpeg, png, gif or webp)")
    data, before, after = constrain_image_bytes(data, VISION_SEND_MAX_SIDE)
    media_type = sniff_media_type(data) or "image/png"
    return LoadedImage(
        b64=base64.b64encode(data).decode("ascii"),
        raw=data,
        media_type=media_type,
        original_bytes=original,
        original_size=before,
        final_size=after,
        downscaled=before != after and after != (0, 0),
        source=source,
    )
