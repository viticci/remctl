"""Inline terminal image rendering for RemCTL attachments.

Pure stdlib at import time. Pillow is used only when available, imported
lazily inside functions behind try/except ImportError; otherwise macOS `sips`
plus a small BMP parser provide pixels for half-block/ascii rendering.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

IMAGE_MODES = ("kitty", "iterm2", "halfblock", "ascii", "none")

_UTI_EXTENSIONS = {
    "public.png": ".png",
    "public.jpeg": ".jpg",
    "public.heic": ".heic",
    "public.heif": ".heif",
    "public.gif": ".gif",
    "com.compuserve.gif": ".gif",
    "public.tiff": ".tiff",
    "com.adobe.pdf": ".pdf",
}
_FALLBACK_EXTS = (".png", ".jpg", ".jpeg", ".heic")

_ASCII_RAMP = " .:-=+*#%@"


def _truthy(value) -> bool:
    return value is not None and str(value).strip().lower() in {"1", "true", "yes", "on"}


def detect_image_mode() -> str | None:
    """Pick the best rendering protocol for the current terminal.

    REMCTL_IMAGE_MODE overrides detection (kitty/iterm2/halfblock/ascii/none).
    """
    override = os.environ.get("REMCTL_IMAGE_MODE", "").strip().lower()
    if override in IMAGE_MODES:
        return None if override == "none" else override

    term_program = os.environ.get("TERM_PROGRAM", "").strip().lower()
    if term_program in {"ghostty", "kitty", "wezterm"}:
        return "kitty"
    if os.environ.get("KONSOLE_VERSION") or os.environ.get("KITTY_WINDOW_ID"):
        return "kitty"
    lc_terminal = os.environ.get("LC_TERMINAL", "").strip().lower()
    if term_program == "iterm.app" or lc_terminal in {"iterm2", "blink"}:
        return "iterm2"
    colorterm = os.environ.get("COLORTERM", "").strip().lower()
    term = os.environ.get("TERM", "").strip().lower()
    if colorterm in {"truecolor", "24bit"} or "truecolor" in term or "256color" in term:
        return "halfblock"
    if sys.stdout.isatty():
        return "ascii"
    return None


# ── Attachment file resolution ───────────────────────────────────────────────

_attachment_dir_cache: dict = {}


def _attachment_dirs(store_dir: Path) -> list[Path]:
    """Account-*/Attachments dirs under Files/, cached per store-dir state."""
    files_dir = Path(store_dir).parent / "Files"
    try:
        signature = files_dir.stat().st_mtime_ns
    except OSError:
        return []
    key = str(files_dir)
    cached = _attachment_dir_cache.get(key)
    if cached and cached[0] == signature:
        return cached[1]
    dirs = []
    try:
        for account_dir in sorted(files_dir.glob("Account-*")):
            attachments = account_dir / "Attachments"
            if attachments.is_dir():
                dirs.append(attachments)
    except OSError:
        dirs = []
    _attachment_dir_cache[key] = (signature, dirs)
    return dirs


def _candidate_extensions(filename: str | None, uti: str | None) -> list[str]:
    exts: list[str] = []
    if filename:
        suffix = Path(filename).suffix.lower()
        if suffix and suffix not in exts:
            exts.append(suffix)
    if uti:
        mapped = _UTI_EXTENSIONS.get(uti.strip().lower())
        if mapped and mapped not in exts:
            exts.append(mapped)
    for ext in _FALLBACK_EXTS:
        if ext not in exts:
            exts.append(ext)
    return exts


def _sha512_of(path: Path) -> str | None:
    digest = hashlib.sha512()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def resolve_attachment_file(store_dir, sha512sum, filename=None, uti=None) -> str | None:
    """Resolve an attachment row to a verified on-disk file, or None.

    Files live in Files/Account-*/Attachments/<ZSHA512SUM><ext>; the file is
    only trusted when its sha512 matches the row's ZSHA512SUM.
    """
    if not sha512sum:
        return None
    sha = str(sha512sum).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{128}", sha):
        return None
    for attachments_dir in _attachment_dirs(Path(store_dir)):
        for ext in _candidate_extensions(filename, uti):
            candidate = attachments_dir / f"{sha}{ext}"
            if not candidate.is_file():
                continue
            if _sha512_of(candidate) == sha:
                return str(candidate)
    return None


# ── Pixel decoding ───────────────────────────────────────────────────────────

class BMPError(Exception):
    pass


def load_bmp(path):
    """Minimal BMP reader: 24-bit BI_RGB and 32-bit BI_RGB/BI_BITFIELDS (BGRA).

    Handles top-down and bottom-up row order and 4-byte row stride. Returns
    (width, height, rows); rows are lists of (r, g, b) or (r, g, b, a) tuples.
    """
    with open(path, "rb") as f:
        d = f.read()
    if len(d) < 54 or d[:2] != b"BM":
        raise BMPError("not a BMP file")
    data_off = struct.unpack_from("<I", d, 10)[0]
    dib_size = struct.unpack_from("<I", d, 14)[0]
    if dib_size < 40:
        raise BMPError(f"unsupported DIB header size {dib_size}")
    w, h, planes, bpp, comp = struct.unpack_from("<iiHHI", d, 18)
    if planes != 1:
        raise BMPError("bad plane count")
    top_down = h < 0
    height = -h if top_down else h
    if w <= 0 or height <= 0:
        raise BMPError("bad dimensions")
    if bpp == 24 and comp == 0:
        px_size = 3
    elif bpp == 32 and comp in (0, 3):
        if comp == 3 and dib_size >= 52:
            rm, gm, bm = struct.unpack_from("<III", d, 14 + 40)
            if (rm, gm, bm) != (0x00FF0000, 0x0000FF00, 0x000000FF):
                raise BMPError(f"non-standard 32-bit masks {rm:#x} {gm:#x} {bm:#x}")
        px_size = 4
    else:
        raise BMPError(f"unsupported bpp={bpp} compression={comp}")
    stride = ((w * px_size + 3) // 4) * 4
    if len(d) < data_off + stride * height:
        raise BMPError("truncated pixel data")
    rows = []
    for y in range(height):
        src_y = y if top_down else (height - 1 - y)
        base = data_off + src_y * stride
        row = []
        if px_size == 3:
            for x in range(w):
                b, g, r = d[base + x * 3: base + x * 3 + 3]
                row.append((r, g, b))
        else:
            for x in range(w):
                b, g, r, a = d[base + x * 4: base + x * 4 + 4]
                row.append((r, g, b, a))
        rows.append(row)
    return w, height, rows


def _flatten_over_black(rgb_rows):
    """Composite (r, g, b[, a]) rows over black, dropping alpha."""
    flat = []
    for row in rgb_rows:
        out_row = []
        for px in row:
            if len(px) == 3:
                out_row.append(px)
            else:
                r, g, b, a = px
                out_row.append((r * a // 255, g * a // 255, b * a // 255))
        flat.append(out_row)
    return flat


def _sips(path: Path, *args: str) -> bool:
    if not shutil.which("sips"):
        return False
    try:
        result = subprocess.run(
            ["sips", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _pixel_rows_via_pillow(path: Path, target_w: int):
    from PIL import Image  # noqa: PLC0415 -- optional, lazy by design

    with Image.open(path) as im:
        w, h = im.size
        if w <= 0 or h <= 0:
            return None
        target_h = max(1, round(h * (target_w / w)))
        im = im.convert("RGBA")
        if (w, h) != (target_w, target_h):
            im = im.resize((target_w, target_h), Image.LANCZOS)
        background = Image.new("RGB", im.size, (0, 0, 0))
        background.paste(im, mask=im.getchannel("A"))
        data = list(background.getdata())
    rows = [data[y * target_w:(y + 1) * target_w] for y in range(target_h)]
    return target_w, target_h, rows


def _pixel_rows_via_sips(path: Path, target_w: int):
    tmpdir = tempfile.mkdtemp(prefix="remctl-img-")
    try:
        bmp_path = Path(tmpdir) / "out.bmp"
        # sips -Z preserves aspect; upscaling to target width is harmless here.
        if not _sips(path, "-Z", str(target_w), "-s", "format", "bmp",
                     str(path), "--out", str(bmp_path)):
            return None
        if not bmp_path.is_file():
            return None
        w, h, rows = load_bmp(bmp_path)
        return w, h, _flatten_over_black(rows)
    except (OSError, BMPError, ValueError):
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _pixel_rows(path, target_w: int):
    """Decode an image to (w, h, rows of (r, g, b)) at ~target_w wide.

    Alpha is flattened over black. Tries Pillow first, then sips -> BMP.
    Returns None when the image cannot be decoded.
    """
    source = Path(path)
    try:
        if not source.is_file():
            return None
    except OSError:
        return None
    try:
        return _pixel_rows_via_pillow(source, target_w)
    except ImportError:
        pass
    except Exception:
        # Pillow present but could not decode this file; try sips.
        pass
    return _pixel_rows_via_sips(source, target_w)


# ── Protocol renderers ───────────────────────────────────────────────────────

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"


def _kitty_escape(payload: bytes, width_cells: int, fmt: int) -> str | None:
    b64 = base64.b64encode(payload).decode("ascii")
    if not b64:
        return None
    chunks = [b64[i:i + 4096] for i in range(0, len(b64), 4096)]
    parts = []
    for index, chunk in enumerate(chunks):
        more = 1 if index < len(chunks) - 1 else 0
        parts.append(
            f"\x1b_Ga=T,t=d,f={fmt},c={width_cells},m={more};{chunk}\x1b\\"
        )
    return "".join(parts)


def _render_kitty(path: Path, width_cells: int) -> str | None:
    data = path.read_bytes()
    if not data:
        return None
    if data.startswith(_PNG_MAGIC):
        return _kitty_escape(data, width_cells, 100)
    if data.startswith(_JPEG_MAGIC):
        return _kitty_escape(data, width_cells, 106)
    # Other formats (HEIC etc.): convert to PNG via sips.
    tmpdir = tempfile.mkdtemp(prefix="remctl-img-")
    try:
        png_path = Path(tmpdir) / "out.png"
        if not _sips(path, "-s", "format", "png", str(path), "--out", str(png_path)):
            return None
        if not png_path.is_file():
            return None
        return _kitty_escape(png_path.read_bytes(), width_cells, 100)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _render_iterm2(path: Path, width_cells: int) -> str | None:
    data = path.read_bytes()
    if not data:
        return None
    b64 = base64.b64encode(data).decode("ascii")
    return (
        f"\x1b]1337;File=inline=1;width={width_cells};preserveAspectRatio=1"
        f":{b64}\x07"
    )


def _render_halfblock(path: Path, width_cells: int) -> str | None:
    pixels = _pixel_rows(path, width_cells)
    if not pixels:
        return None
    w, h, rows = pixels
    lines = []
    for y in range(0, h - 1, 2):
        top, bottom = rows[y], rows[y + 1]
        line = []
        for x in range(w):
            tr, tg, tb = top[x]
            br, bg, bb = bottom[x]
            line.append(
                f"\x1b[38;2;{tr};{tg};{tb}m\x1b[48;2;{br};{bg};{bb}m▀"
            )
        line.append("\x1b[0m")
        lines.append("".join(line))
    return "\n".join(lines) if lines else None


def _render_ascii(path: Path, width_cells: int) -> str | None:
    pixels = _pixel_rows(path, width_cells)
    if not pixels:
        return None
    w, h, rows = pixels
    ramp = _ASCII_RAMP
    last = len(ramp) - 1
    lines = []
    for row in rows:
        line = []
        for r, g, b in row:
            lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
            line.append(ramp[min(last, int(lum * (last + 1) / 256))])
        lines.append("".join(line))
    return "\n".join(lines) if lines else None


def render_attachment(path, mode, width_cells: int = 32) -> str | None:
    """Render an image file for a terminal protocol; None when not renderable.

    Never raises: any failure means the caller falls back to text output.
    """
    try:
        width_cells = int(width_cells)
    except (TypeError, ValueError):
        return None
    if mode not in {"kitty", "iterm2", "halfblock", "ascii"} or width_cells < 1:
        return None
    try:
        source = Path(path)
        if not source.is_file():
            return None
        if mode == "kitty":
            return _render_kitty(source, width_cells)
        if mode == "iterm2":
            return _render_iterm2(source, width_cells)
        if mode == "halfblock":
            return _render_halfblock(source, width_cells)
        return _render_ascii(source, width_cells)
    except Exception:
        return None
