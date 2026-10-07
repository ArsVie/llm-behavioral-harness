"""Photos the owner sends: stored once, referenced everywhere, inlined on send.

A photo is saved under ``<image dir>/<sha256>.jpg`` (next to the run's
database, like the wire dumps) and the user row keeps only its reference in
``meta.images``. Every request carries the reference, ``harness-image:<name>``,
as an ``image_url`` part, so the stored stream, the audit envelopes and the
wire dumps stay small and byte-stable. :func:`inline` swaps the reference for
the base64 data URL in the request body the client posts, and nowhere else.

A photo is normalized before it is saved (EXIF rotation applied, longest side
at most ``MAX_SIDE``, re-encoded as JPEG) so the bytes, and with them the
reference and the cached prefix, never change after the first send.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path

#: Scheme of a stored photo's reference inside an ``image_url`` part.
SCHEME = "harness-image:"
#: The longest side a stored photo keeps: Telegram's own photos are 1280,
#: and more pixels only cost tokens.
MAX_SIDE = 1568
JPEG_QUALITY = 85
#: Bigger uploads (an image sent as a file) are refused before decoding.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

_DIR: Path | None = None


def configure(path: str | os.PathLike | None) -> None:
    """Point the store at ``path`` (env ``HARNESS_IMAGE_DIR`` overrides)."""
    global _DIR
    _DIR = Path(path) if path is not None else None


def _dir() -> Path | None:
    env = os.environ.get("HARNESS_IMAGE_DIR")
    if env:
        return Path(env)
    return _DIR


def normalize(data: bytes) -> bytes:
    """The bytes as stored: upright, at most ``MAX_SIDE``, JPEG.

    Raises ``ValueError`` when ``data`` is not an image Pillow can read.
    """
    from PIL import Image, ImageOps, UnidentifiedImageError

    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(f"image too large ({len(data)} bytes)")
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError(f"not a readable image: {exc}") from exc
    if img.mode not in ("RGB", "L"):
        # Transparent areas go white, not black.
        rgba = img.convert("RGBA")
        img = Image.new("RGB", rgba.size, (255, 255, 255))
        img.paste(rgba, mask=rgba.split()[-1])
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=JPEG_QUALITY)
    return out.getvalue()


def save(data: bytes) -> str:
    """Normalize and store one photo; returns its file name (the reference).

    Content-addressed, so the same photo sent twice is one file. Raises
    ``ValueError`` for an unreadable image and ``RuntimeError`` when no image
    directory is configured.
    """
    d = _dir()
    if d is None:
        raise RuntimeError("no image directory configured")
    body = normalize(data)
    name = hashlib.sha256(body).hexdigest() + ".jpg"
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    if not path.exists():
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(body)
        tmp.replace(path)
    return name


def path_of(name: str) -> Path | None:
    """The file behind a reference, or None when it is not on disk."""
    d = _dir()
    if d is None or "/" in name or "\\" in name or name.startswith("."):
        return None
    p = d / name
    return p if p.is_file() else None


def of(row) -> list[str]:
    """The photo references a stored message row carries (``meta.images``).

    ``row`` is a dict or a ``sqlite3.Row``.
    """
    try:
        meta = row["meta"]
    except (KeyError, IndexError, TypeError):
        return []
    if isinstance(meta, str):
        if "images" not in meta:
            return []
        try:
            meta = json.loads(meta)
        except ValueError:
            return []
    if not isinstance(meta, dict):
        return []
    found = meta.get("images")
    return [str(n) for n in found] if isinstance(found, list) else []


def parts(text: str, names: list[str]) -> list[dict]:
    """A user turn's multimodal content: the photos, then the text."""
    out: list[dict] = [
        {"type": "image_url", "image_url": {"url": SCHEME + name}}
        for name in names
    ]
    if text:
        out.append({"type": "text", "text": text})
    return out


def inline(messages: list[dict]) -> list[dict]:
    """``messages`` with every photo reference swapped for its data URL.

    New dicts only where a reference is replaced; the caller's messages (and
    so every recorded envelope) keep the reference. A photo missing from disk
    drops out of the request instead of failing it.
    """
    out: list[dict] = []
    for msg in messages:
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list) or not any(_ref(p) for p in content):
            out.append(msg)
            continue
        new_parts: list = []
        for part in content:
            name = _ref(part)
            if name is None:
                new_parts.append(part)
                continue
            p = path_of(name)
            if p is None:
                continue
            data = base64.b64encode(p.read_bytes()).decode("ascii")
            new_parts.append({"type": "image_url", "image_url": {
                "url": f"data:image/jpeg;base64,{data}"}})
        out.append({**msg, "content": new_parts or ""})
    return out


def _ref(part) -> str | None:
    if not isinstance(part, dict) or part.get("type") != "image_url":
        return None
    url = (part.get("image_url") or {}).get("url")
    if isinstance(url, str) and url.startswith(SCHEME):
        return url[len(SCHEME):]
    return None
