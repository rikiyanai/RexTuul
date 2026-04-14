#!/usr/bin/env python3
"""
batch_png_to_xp.py — Zero-dependency PNG/XP batch converter and viewer.

CAPABILITIES:
    - Zero Dependencies: Uses ONLY Python standard library (struct, zlib, gzip).
    - Batch Conversion: PNG -> XP ([P2X] prefix) and XP -> PNG ([X2P] prefix).
    - Pure-Python Codecs: Custom PNG decoder/encoder and REXPaint .xp parser.
    - High-Fidelity Dithering: Ordered (Bayer 4x4) and Floyd-Steinberg support.
    - Interactive TUI Mode (--watch): Live, scroll-free preview of images and XP files.
    - C++ Backend Support (--cpp): Optional high-performance path if C++ tool is available.

USAGE:
    python3 scripts/batch_png_to_xp.py INPUT_DIR
    python3 scripts/batch_png_to_xp.py INPUT_DIR --export-png
    python3 scripts/batch_png_to_xp.py INPUT_DIR --watch
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import shutil
import signal
import sys
import uuid
import subprocess
import struct
import gzip
import zlib
import io
import tty
import termios
import select
from pathlib import Path

# ── PNG Codec (Pure Python) ──────────────────────────────────────────────────

def _load_png_rgba(path: Path) -> tuple[int, int, list[tuple[int, int, int, int]]]:
    """Pure-Python PNG decoder (RGBA). Supports 8-bit RGB/RGBA."""
    with open(path, "rb") as f:
        sig = f.read(8)
        if sig != b"\x89PNG\r\n\x1a\n":
            raise ValueError("Not a PNG file")

        width = height = 0
        idat = bytearray()
        color_type = 6
        while True:
            chunk_head = f.read(8)
            if not chunk_head: break
            length, chunk_type = struct.unpack(">I4s", chunk_head)
            data = f.read(length)
            f.read(4)  # CRC

            if chunk_type == b"IHDR":
                width, height, bit_depth, color_type, compression, filter_method, interlace = struct.unpack(">IIBBBBB", data)
                if bit_depth != 8 or interlace != 0 or color_type not in (2, 6):
                    raise ValueError(f"Unsupported PNG: depth={bit_depth} type={color_type}")
            elif chunk_type == b"IDAT":
                idat.extend(data)
            elif chunk_type == b"IEND":
                break

    decompressed = zlib.decompress(idat)
    bpp = 4 if color_type == 6 else 3
    stride = width * bpp
    pixels = []

    def paeth(a, b, c):
        p = a + b - c
        pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
        return a if pa <= pb and pa <= pc else (b if pb <= pc else c)

    prev_row = bytearray(stride)
    for y in range(height):
        row_start = y * (stride + 1)
        filter_type = decompressed[row_start]
        row_data = decompressed[row_start + 1 : row_start + 1 + stride]
        recon_row = bytearray(stride)

        for x in range(stride):
            a = recon_row[x - bpp] if x >= bpp else 0
            b = prev_row[x]
            c = prev_row[x - bpp] if x >= bpp else 0

            if filter_type == 0: val = row_data[x]
            elif filter_type == 1: val = (row_data[x] + a) & 0xFF
            elif filter_type == 2: val = (row_data[x] + b) & 0xFF
            elif filter_type == 3: val = (row_data[x] + (a + b) // 2) & 0xFF
            elif filter_type == 4: val = (row_data[x] + paeth(a, b, c)) & 0xFF
            else: raise ValueError(f"Unknown PNG filter {filter_type}")
            recon_row[x] = val

        for i in range(0, stride, bpp):
            if color_type == 6:
                pixels.append((recon_row[i], recon_row[i+1], recon_row[i+2], recon_row[i+3]))
            else:
                pixels.append((recon_row[i], recon_row[i+1], recon_row[i+2], 255))
        prev_row = recon_row
    return width, height, pixels

def _save_png_rgba(path: Path, width: int, height: int, pixels: list[tuple[int, int, int, int]]):
    """Pure-Python PNG encoder (8-bit RGBA)."""
    raw_data = bytearray()
    for y in range(height):
        raw_data.append(0) # Filter byte (None)
        for x in range(width):
            r, g, b, a = pixels[y * width + x]
            raw_data.extend((r, g, b, a))

    def write_chunk(f, chunk_type, data):
        f.write(struct.pack(">I", len(data)))
        f.write(chunk_type)
        f.write(data)
        crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
        f.write(struct.pack(">I", crc))

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
        write_chunk(f, b"IHDR", ihdr_data)
        write_chunk(f, b"IDAT", zlib.compress(raw_data))
        write_chunk(f, b"IEND", b"")

# ── Palette & Dithering ──────────────────────────────────────────────────────

def _load_palette(path: Path) -> list[tuple[int, int, int]]:
    """Load standard 216-color cube or raw RGB palette."""
    palette = []
    for i in range(216):
        j = i
        b = (j % 6) * 51; j //= 6
        g = (j % 6) * 51; j //= 6
        r = (j % 6) * 51
        palette.append((r, g, b))
    if path.exists():
        with open(path, "rb") as f:
            data = f.read()
        if len(data) >= 3 and len(data) % 3 == 0 and len(data) <= 768:
            return [tuple(data[i:i+3]) for i in range(0, len(data), 3)]
    return palette

def _closest_color(r, g, b, palette):
    best_idx = 0
    min_dist = 1000000
    for i, (pr, pg, pb) in enumerate(palette):
        dist = (r - pr)**2 + (g - pg)**2 + (b - pb)**2
        if dist < min_dist:
            min_dist = dist
            best_idx = i
    return palette[best_idx]

BAYER_4x4 = [
    [ 0,  8,  2, 10],
    [12,  4, 14,  6],
    [ 3, 11,  1,  9],
    [15,  7, 13,  5]
]

def _apply_dither_bayer(r, g, b, x, y, limit, palette):
    if limit <= 0: return _closest_color(r, g, b, palette)
    threshold = (BAYER_4x4[y % 4][x % 4] / 16.0 - 0.5) * (limit * 50)
    return _closest_color(max(0, min(255, int(r + threshold))),
                         max(0, min(255, int(g + threshold))),
                         max(0, min(255, int(b + threshold))), palette)

def _apply_dither_fs(pixels, sw, sh, palette):
    data = [list(p[:3]) for p in pixels]
    out = []
    for y in range(sh):
        for x in range(sw):
            old_r, old_g, old_b = data[y * sw + x]
            new_r, new_g, new_b = _closest_color(old_r, old_g, old_b, palette)
            out.append((new_r, new_g, new_b, pixels[y * sw + x][3]))
            err_r, err_g, err_b = old_r - new_r, old_g - new_g, old_b - new_b
            def distribute(dx, dy, factor):
                nx, ny = x + dx, y + dy
                if 0 <= nx < sw and 0 <= ny < sh:
                    idx = ny * sw + nx
                    data[idx][0] += err_r * factor
                    data[idx][1] += err_g * factor
                    data[idx][2] += err_b * factor
            distribute(1, 0, 7/16); distribute(-1, 1, 3/16)
            distribute(0, 1, 5/16); distribute(1, 1, 1/16)
    return out

# ── REXPaint .xp Core ───────────────────────────────────────────────────────

MAGENTA_BG = (255, 0, 255)

class XPLayer:
    def __init__(self, width, height, data=None):
        self.width = width
        self.height = height
        self.data = data if data else [[(0, (0, 0, 0), (0, 0, 0)) for _ in range(width)] for _ in range(height)]

class XPFile:
    def __init__(self, filename=None):
        self.version = -1
        self.layers = []
        if filename: self.load(filename)

    def load(self, filename):
        with gzip.open(filename, 'rb') as f:
            content = f.read()
        offset = 0
        self.version = struct.unpack('<i', content[offset:offset+4])[0]
        offset += 4
        layer_count = struct.unpack('<I', content[offset:offset+4])[0]
        offset += 4
        for _ in range(layer_count):
            width = struct.unpack('<i', content[offset:offset+4])[0]
            offset += 4
            height = struct.unpack('<i', content[offset:offset+4])[0]
            offset += 4
            layer_data = [[None for _ in range(width)] for _ in range(height)]
            for x in range(width):
                for y in range(height):
                    glyph = struct.unpack('<I', content[offset:offset+4])[0]
                    offset += 4
                    fg = tuple(content[offset:offset+3])
                    offset += 3
                    bg = tuple(content[offset:offset+3])
                    offset += 3
                    layer_data[y][x] = (glyph, fg, bg)
            self.layers.append(XPLayer(width, height, layer_data))

def write_xp(path, width, height, layers):
    with gzip.open(path, "wb") as f:
        f.write(struct.pack("<i", -1))
        f.write(struct.pack("<I", len(layers)))
        for layer in layers:
            f.write(struct.pack("<i", width))
            f.write(struct.pack("<i", height))
            for x in range(width):
                for y in range(height):
                    glyph, fg, bg = layer[y * width + x]
                    f.write(struct.pack("<I", int(glyph)))
                    f.write(bytes([fg[0], fg[1], fg[2], bg[0], bg[1], bg[2]]))

# ── ANSI Rendering ──────────────────────────────────────────────────────────

_RESET = "\033[0m"
def _fg(r, g, b): return f"\033[38;2;{int(r)};{int(g)};{int(b)}m"
def _bg(r, g, b): return f"\033[48;2;{int(r)};{int(g)};{int(b)}m"

def _cell_pixel_color(glyph, fg, bg):
    if bg == MAGENTA_BG:
        if glyph in (0, 32): return MAGENTA_BG
        return fg
    if glyph == 219: return fg
    return bg

def _render_png_halfblock_raw(pixels, sw, sh, cols: int) -> str:
    tgt_w = cols
    tgt_h = max(2, int(sh * tgt_w / sw))
    if tgt_h % 2: tgt_h += 1
    UPPER, LOWER = "\u2580", "\u2584"
    lines = []
    for y in range(0, tgt_h, 2):
        row = []
        for x in range(tgt_w):
            sx = int(x * sw / tgt_w)
            sy0, sy1 = int(y * sh / tgt_h), int((y + 1) * sh / tgt_h)
            r0, g0, b0, a0 = pixels[sy0 * sw + sx]
            r1, g1, b1, a1 = pixels[sy1 * sw + sx] if sy1 < sh else (0, 0, 0, 0)
            tv, bv = a0 >= 16, a1 >= 16
            if not tv and not bv: row.append(" ")
            elif tv and not bv: row.append(f"\033[49m{_fg(r0,g0,b0)}{UPPER}{_RESET}")
            elif not tv and bv: row.append(f"\033[49m{_fg(r1,g1,b1)}{LOWER}{_RESET}")
            else: row.append(f"{_fg(r0,g0,b0)}{_bg(r1,g1,b1)}{UPPER}{_RESET}")
        lines.append("".join(row))
    return "\n".join(lines)

def _render_xp_halfblock(xp: XPFile, target_cols: int | None = None) -> str:
    if not xp.layers: return "[empty]"
    idx = 2 if len(xp.layers) >= 3 else 0
    layer = xp.layers[idx]
    sw, sh = layer.width, layer.height
    tw = target_cols if target_cols else sw
    UPPER, LOWER = "\u2580", "\u2584"
    lines = []
    for y in range(0, sh, 2):
        row = []
        for x in range(tw):
            sx = int(x * sw / tw)
            g0, fg0, bg0 = layer.data[y][sx]
            c0 = _cell_pixel_color(g0, fg0, bg0)
            if y + 1 < sh:
                g1, fg1, bg1 = layer.data[y+1][sx]
                c1 = _cell_pixel_color(g1, fg1, bg1)
            else:
                c1 = MAGENTA_BG
            t_t, b_t = c0 == MAGENTA_BG, c1 == MAGENTA_BG
            if t_t and b_t: row.append(" ")
            elif t_t: row.append(f"\033[49m{_fg(*c1)}{LOWER}{_RESET}")
            elif b_t: row.append(f"\033[49m{_fg(*c0)}{UPPER}{_RESET}")
            else: row.append(f"{_fg(*c0)}{_bg(*c1)}{UPPER}{_RESET}")
        lines.append("".join(row))
    return "\n".join(lines)

class _WatchRenderer:
    """Minimal, high-speed watcher. Only redraws on SIGWINCH."""
    def __init__(self, file_paths: list[Path], args) -> None:
        self._file_paths = file_paths
        self._args = args
        self._data = []
        # Pre-load raw data once
        for p in file_paths:
            try:
                if p.suffix.lower() == ".xp":
                    xp = XPFile(p)
                    w, h, pix = _xp_to_png_rgba(xp)
                    self._data.append((p.name, w, h, pix, True))
                else:
                    w, h, pix = _load_png_rgba(p)
                    self._data.append((p.name, w, h, pix, False))
            except Exception as e:
                print(f"Error pre-loading {p.name}: {e}", file=sys.stderr)

    def _draw(self) -> None:
        cols, rows = shutil.get_terminal_size(fallback=(80, 24))
        tw = self._args.target_cols or (cols - 2)
        tw = max(4, tw)

        # Build whole screen buffer
        buf = ["\033[H\033[2J"] # Home + Clear
        for name, sw, sh, pixels, is_xp in self._data:
            bar = f"── {name} "
            buf.append(bar + "─" * max(0, tw - len(bar)) + "\r\n")
            if is_xp:
                content = _render_xp_halfblock(XPFile(), tw) # Placeholder, we use cached pix instead
                # Actually, let's just use the pixels for both, it's simpler
                content = _render_png_halfblock_raw(pixels, sw, sh, tw)
            else:
                content = _render_png_halfblock_raw(pixels, sw, sh, tw)

            # Ensure proper line endings for raw-like behavior in some terminals
            lines = content.splitlines()
            for line in lines:
                buf.append(line + "\r\n")
            buf.append("\r\n")

        sys.stdout.write("".join(buf))
        sys.stdout.flush()

    def run(self) -> None:
        try:
            # Alternate Screen, Hide Cursor
            sys.stdout.write("\033[?1049h\033[?25l")
            sys.stdout.flush()
            
            # Use a flag to signal redraw from the signal handler
            self._redraw_pending = True
            def on_resize(*_):
                self._redraw_pending = True
            
            signal.signal(signal.SIGWINCH, on_resize)
            
            while True:
                if self._redraw_pending:
                    self._redraw_pending = False
                    self._draw()
                # Use a short sleep to prevent 100% CPU while staying responsive
                import time
                time.sleep(0.05)
        except (KeyboardInterrupt, EOFError):
            pass
        finally:
            # Exit Alternate Screen, Show Cursor
            sys.stdout.write("\033[?1049l\033[?25h")
            sys.stdout.flush()
def _tile_to_cells_raw(pixels, sw, sh, out_w, out_h, palette, dither_limit, use_floyd=False):
    sampled = []
    for y in range(out_h):
        for x in range(out_w):
            sx, sy = int(x * sw / out_w), int(y * sh / out_h)
            sampled.append(pixels[sy * sw + sx])
    if use_floyd: dithered = _apply_dither_fs(sampled, out_w, out_h, palette)
    else:
        dithered = []
        for y in range(out_h):
            for x in range(out_w):
                p = sampled[y * out_w + x]
                if p[3] < 128: dithered.append((0, 0, 0, 0))
                else:
                    color = _apply_dither_bayer(p[0], p[1], p[2], x, y, dither_limit, palette)
                    dithered.append((*color, 255))
    cells = []
    for p in dithered:
        if p[3] < 128: cells.append((32, (0, 0, 0), MAGENTA_BG))
        else: cells.append((219, (int(p[0]), int(p[1]), int(p[2])), MAGENTA_BG))
    return cells

def _xp_to_png_rgba(xp: XPFile) -> tuple[int, int, list[tuple[int, int, int, int]]]:
    if not xp.layers: raise ValueError("XP has no layers")
    idx = 2 if len(xp.layers) >= 3 else 0
    layer = xp.layers[idx]
    pixels = []
    for y in range(layer.height):
        for x in range(layer.width):
            glyph, fg, bg = layer.data[y][x]
            c = _cell_pixel_color(glyph, fg, bg)
            pixels.append((0,0,0,0) if c == MAGENTA_BG else (*c, 255))
    return layer.width, layer.height, pixels

# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="PNG <-> XP converter and viewer.",
                                     formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    parser.add_argument("input", help="Folder or file to process")
    parser.add_argument("-W", "--target-cols", type=int, help="Override output width")
    parser.add_argument("-H", "--target-rows", type=int, help="Override output height")
    parser.add_argument("--watch", action="store_true", help="TUI Mode: Display ONLY")
    parser.add_argument("-x", "--export-png", action="store_true", help="Export Mode: XP -> PNG")
    parser.add_argument("-d", "--dither", type=int, default=2, help="Bayer limit (0-5, default 2)")
    parser.add_argument("-f", "--floyd", action="store_true", help="Use Floyd-Steinberg")
    parser.add_argument("--cpp", action="store_true", help="Use C++ backend")
    args = parser.parse_args()

    path = Path(args.input).resolve()
    if not path.exists(): sys.exit(f"error: not found: {path}")

    plt_path = Path(__file__).resolve().parent.parent / "cpp" / "test.plt"
    palette = _load_palette(plt_path)
    cpp_bin = Path(__file__).resolve().parent.parent / "cpp" / "png2xp"

    # ── MODE 1: WATCH (TUI) ──
    if args.watch:
        files = [path] if path.is_file() else sorted(list(path.glob("*.xp")) + list(path.glob("*.png")))
        if not files: sys.exit("no files to view")
        _WatchRenderer(files, args).run()
        return

    # ── MODE 2: EXPORT (XP -> PNG) ──
    if args.export_png:
        files = [path] if path.is_file() and path.suffix.lower() == ".xp" else sorted(path.glob("*.xp"))
        if not files: sys.exit("no .xp files to export")
        print(f"Exporting {len(files)} files...")
        for f in files:
            out = f.parent / f"[X2P]{f.stem}.png"
            try:
                xp = XPFile(f); w, h, pix = _xp_to_png_rgba(xp); _save_png_rgba(out, w, h, pix)
                print(f"  {f.name} -> {out.name} OK")
            except Exception as e: print(f"  {f.name} FAIL: {e}")
        return

    # ── MODE 3: CONVERT (PNG -> XP) ──
    files = [path] if path.is_file() and path.suffix.lower() == ".png" else sorted(path.glob("*.png"))
    if not files: sys.exit("no .png files to convert")
    print(f"Converting {len(files)} files...")
    term_w = shutil.get_terminal_size(fallback=(120, 24)).columns
    for f in files:
        out = f.parent / f"[P2X]{f.stem}.xp"
        try:
            if args.cpp and cpp_bin.exists():
                tw = args.target_cols or term_w
                cmd = [str(cpp_bin), "-w", str(tw*2), "-d", str(args.dither)]
                if args.floyd: cmd.append("-f")
                cmd.extend([str(plt_path), str(f), str(out)])
                subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                print(f"  {f.name} OK (C++)")
            else:
                sw, sh, pixels = _load_png_rgba(f)
                tw = args.target_cols or term_w
                th = args.target_rows or int(sh * tw / sw)
                cells = _tile_to_cells_raw(pixels, sw, sh, tw, th, palette, args.dither, args.floyd)
                write_xp(out, tw, th, [cells])
                print(f"  {f.name} OK ({tw}x{th})")
        except Exception as e: print(f"  {f.name} FAIL: {e}")

if __name__ == "__main__":
    main()
