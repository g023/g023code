"""Shape digests: what a big directory, data file or binary *is*, in a few hundred chars.

One idea, applied to every multiset a read can return: replace N similar items
by a template, its parameter ranges and an exact count, and show the rare items
verbatim with an address. Common items are compressible and predictable; rare
ones (an ``ERROR`` line among 400k ``INFO`` lines, a ``README.md`` among 4,000
frames) carry the information.

- names   ``frame_00000.png`` .. ``frame_03999.png`` -> ``frame_{00000..03999}.png x4000``
- lines   log lines -> masked templates with counts, line addresses and slot ranges
- rows    CSV/TSV -> columns with inferred type, nulls, range, distinct, top values
- records JSONL / JSON -> a merged schema (``items: [50000 x {id:int 0..49999, ...}]``)
- bytes   magic-byte format sniffing and bounded, stdlib-only decoders
  (images, archives, SQLite, ELF/PE/Mach-O, pyc, PDF, audio, ...)

Everything is local, deterministic and bounded in time and memory: no API call.
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
import sqlite3
import struct
import tarfile
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Optional

# ---------------------------------------------------------------------------
# Name templates (directory listings, glob results, archive members)
# ---------------------------------------------------------------------------
_DIGITS = re.compile(r"(\d+)")
# Groups smaller than this are listed name by name.
NAME_GROUP_MIN = 3
# Opaque ids (hashes, uuids): collapsed by shape when there are this many.
OPAQUE_GROUP_MIN = 5
_OPAQUE = re.compile(r"[0-9a-f]{8,}(?:-[0-9a-f]{4,})*|[A-Za-z0-9_-]{24,}", re.I)


def human_size(n: Optional[int]) -> str:
    if n is None:
        return "?"
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{int(f)} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{n} B"


def _name_key(name: str) -> tuple[tuple, list[str]]:
    parts = _DIGITS.split(name)
    texts = tuple(parts[0::2])
    nums = parts[1::2]
    return texts, nums


def _slot(values: list[str]) -> str:
    ints = [int(v) for v in values]
    lo, hi = min(ints), max(ints)
    if lo == hi:
        return values[0]
    padded = all(len(v) == len(values[0]) for v in values) and values[0].startswith("0") or (
        len({len(v) for v in values}) == 1 and any(v.startswith("0") for v in values)
    )
    width = len(values[0]) if padded else 0
    return "{" + f"{lo:0{width}d}..{hi:0{width}d}" + "}"


def _render_group(texts: tuple, nums_list: list[list[str]]) -> tuple[str, Optional[str]]:
    """Template string plus a gap note for a group of names sharing ``texts``."""
    slots = list(zip(*nums_list)) if nums_list and nums_list[0] else []
    rendered = [texts[0]]
    for i, col in enumerate(slots):
        rendered.append(_slot(list(col)))
        rendered.append(texts[i + 1])
    gap = None
    varying = [col for col in slots if len(set(col)) > 1]
    if len(varying) == 1:
        ints = sorted({int(v) for v in varying[0]})
        span = ints[-1] - ints[0] + 1
        missing = span - len(ints)
        if missing > 0 and len(nums_list) == len(ints):
            have = set(ints)
            holes = [n for n in range(ints[0], ints[-1] + 1) if n not in have]
            gap = f"{missing} missing: " + ", ".join(str(h) for h in holes[:6]) + (" ..." if missing > 6 else "")
    return "".join(rendered), gap


def _opaque_shape(name: str) -> Optional[str]:
    stem, dot, ext = name.partition(".")
    m = _OPAQUE.fullmatch(stem)
    if not m:
        return None
    hexy = all(c in "0123456789abcdefABCDEF-" for c in stem)
    return f"<{'hex' if hexy else 'id'}{len(stem)}>" + (dot + ext if dot else "")


def group_names(names: Iterable[str], sizes: Optional[dict[str, Optional[int]]] = None) -> tuple[list[dict], list[str]]:
    """Collapse names into templates. Returns ``(groups, singles)``.

    A group is ``{"template", "count", "size", "example", "members", "gap"?}``; ``singles``
    are the names that fit no group of :data:`NAME_GROUP_MIN` or more.
    """
    sizes = sizes or {}
    names = list(names)
    by_key: dict[tuple, list[tuple[str, list[str]]]] = {}
    for n in names:
        texts, nums = _name_key(n)
        if nums:
            by_key.setdefault(texts, []).append((n, nums))
    groups: list[dict] = []
    grouped: set[str] = set()
    for texts, members in by_key.items():
        if len(members) < NAME_GROUP_MIN:
            continue
        tpl, gap = _render_group(texts, [m[1] for m in members])
        total = sum((sizes.get(m[0]) or 0) for m in members)
        g = {"template": tpl, "count": len(members), "size": total, "example": members[0][0],
             "members": [m[0] for m in members]}
        if gap:
            g["gap"] = gap
        groups.append(g)
        grouped.update(m[0] for m in members)
    rest = [n for n in names if n not in grouped]
    by_shape: dict[str, list[str]] = {}
    for n in rest:
        shape = _opaque_shape(n)
        if shape:
            by_shape.setdefault(shape, []).append(n)
    for shape, members in by_shape.items():
        if len(members) >= OPAQUE_GROUP_MIN:
            groups.append({
                "template": shape, "count": len(members),
                "size": sum((sizes.get(m) or 0) for m in members), "example": members[0],
                "members": members,
            })
            grouped.update(members)
    singles = [n for n in names if n not in grouped]
    groups.sort(key=lambda g: (-g["count"], g["template"]))
    return groups, singles


def render_group(g: dict, *, prefix: str = "") -> str:
    row = f"{prefix}{g['template']}  x{g['count']}"
    if g.get("size"):
        row += f"  {human_size(g['size'])}"
    if g.get("gap"):
        row += f"  ({g['gap']})"
    return row


def ext_histogram(names: Iterable[str], top: int = 6) -> str:
    c = Counter((Path(n).suffix.lower() or "(none)") for n in names)
    items = c.most_common()
    shown = ", ".join(f"{n} {e}" for e, n in items[:top])
    if len(items) > top:
        shown += f", +{sum(n for _, n in items[top:])} other"
    return shown


# ---------------------------------------------------------------------------
# Binary sniffing
# ---------------------------------------------------------------------------
SNIFF_BYTES = 8192
_TEXT_CTRL = set(range(32)) - {9, 10, 12, 13, 27}

_MAGIC: list[tuple[int, bytes, str]] = [
    (0, b"\x89PNG\r\n\x1a\n", "png"),
    (0, b"\xff\xd8\xff", "jpeg"),
    (0, b"GIF87a", "gif"),
    (0, b"GIF89a", "gif"),
    (0, b"BM", "bmp?"),
    (0, b"\x00\x00\x01\x00", "ico?"),
    (0, b"II*\x00", "tiff"),
    (0, b"MM\x00*", "tiff"),
    (0, b"%PDF-", "pdf"),
    (0, b"PK\x03\x04", "zip"),
    (0, b"PK\x05\x06", "zip"),
    (0, b"\x1f\x8b", "gzip"),
    (0, b"BZh", "bzip2"),
    (0, b"\xfd7zXZ\x00", "xz"),
    (0, b"\x28\xb5\x2f\xfd", "zstd"),
    (0, b"7z\xbc\xaf\x27\x1c", "7z"),
    (0, b"Rar!\x1a\x07", "rar"),
    (0, b"SQLite format 3\x00", "sqlite"),
    (0, b"\x7fELF", "elf"),
    (0, b"MZ", "pe?"),
    (0, b"\xfe\xed\xfa\xce", "macho"),
    (0, b"\xfe\xed\xfa\xcf", "macho"),
    (0, b"\xce\xfa\xed\xfe", "macho"),
    (0, b"\xcf\xfa\xed\xfe", "macho"),
    (0, b"\xca\xfe\xba\xbe", "cafebabe"),
    (0, b"\x00asm", "wasm"),
    (0, b"PAR1", "parquet"),
    (0, b"\x93NUMPY", "npy"),
    (0, b"\x89HDF\r\n\x1a\n", "hdf5"),
    (0, b"ID3", "mp3"),
    (0, b"OggS", "ogg"),
    (0, b"fLaC", "flac"),
    (0, b"wOFF", "woff"),
    (0, b"wOF2", "woff2"),
    (0, b"OTTO", "otf"),
    (0, b"\x00\x01\x00\x00\x00", "ttf"),
    (0, b"\x80\x04\x95", "pickle"),
    (0, b"\x80\x05\x95", "pickle"),
    (0, b"\x80\x02", "pickle?"),
    (0, b"\x1a\x45\xdf\xa3", "matroska"),
]

FORMAT_LABEL = {
    "png": "PNG image", "jpeg": "JPEG image", "gif": "GIF image", "bmp": "BMP image",
    "ico": "Windows icon", "tiff": "TIFF image", "webp": "WebP image", "pdf": "PDF document",
    "zip": "ZIP archive", "gzip": "gzip stream", "bzip2": "bzip2 stream", "xz": "xz stream",
    "zstd": "Zstandard stream", "7z": "7-Zip archive", "rar": "RAR archive", "tar": "tar archive",
    "sqlite": "SQLite 3 database", "elf": "ELF executable/object", "pe": "Windows PE executable",
    "macho": "Mach-O binary", "java-class": "Java class file", "macho-fat": "Mach-O universal binary",
    "wasm": "WebAssembly module", "parquet": "Parquet table", "npy": "NumPy array",
    "hdf5": "HDF5 data", "mp3": "MP3 audio", "ogg": "Ogg media", "flac": "FLAC audio",
    "wav": "WAV audio", "avi": "AVI video", "mp4": "MP4/QuickTime media", "matroska": "Matroska/WebM media",
    "woff": "WOFF font", "woff2": "WOFF2 font", "otf": "OpenType font", "ttf": "TrueType font",
    "pickle": "Python pickle", "pyc": "Python bytecode", "binary": "binary data",
}

# CPython bytecode magic (first 2 bytes, little-endian) -> version.
_PYC_MAGIC = {
    3413: "3.8", 3425: "3.9", 3439: "3.10", 3495: "3.11", 3531: "3.12",
    3571: "3.13", 3600: "3.14",
}


def _pyc_version(head: bytes) -> Optional[str]:
    if len(head) < 4 or head[2:4] != b"\r\n":
        return None
    magic = struct.unpack("<H", head[:2])[0]
    best = None
    for m, v in sorted(_PYC_MAGIC.items()):
        if magic >= m:
            best = v
    return best if magic >= 3000 and magic < 4000 else None


def text_encoding(head: bytes) -> Optional[str]:
    """A non-UTF-8 text encoding announced by a BOM (the file is text, not binary)."""
    if head.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if head.startswith(b"\xff\xfe\x00\x00") or head.startswith(b"\x00\x00\xfe\xff"):
        return "utf-32"
    if head.startswith(b"\xff\xfe") or head.startswith(b"\xfe\xff"):
        return "utf-16"
    return None


def looks_binary(head: bytes) -> bool:
    """NUL bytes, a high share of control bytes, or undecodable UTF-8."""
    if not head:
        return False
    if b"\x00" in head:
        return True
    ctrl = sum(1 for b in head if b in _TEXT_CTRL)
    if ctrl / len(head) > 0.10:
        return True
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as e:
        # A multi-byte character cut by the sniff window is not evidence.
        if e.start < len(head) - 4:
            # Latin-1 text is still text: mostly printable, few high bytes.
            high = sum(1 for b in head if b >= 0x80)
            return high / len(head) > 0.30
    return False


def sniff(head: bytes, path: Optional[Path] = None) -> Optional[str]:
    """Format id of a binary file, or ``None`` for text (including BOM-marked UTF-16/32)."""
    if text_encoding(head):
        return None
    for off, magic, fmt in _MAGIC:
        if head[off: off + len(magic)] == magic:
            if fmt == "bmp?":
                if len(head) >= 26 and struct.unpack("<I", head[14:18])[0] in (12, 40, 56, 108, 124):
                    return "bmp"
                continue
            if fmt == "ico?":
                if len(head) >= 6 and 0 < struct.unpack("<H", head[4:6])[0] < 256 and looks_binary(head):
                    return "ico"
                continue
            if fmt == "pe?":
                if len(head) >= 64:
                    off_pe = struct.unpack("<I", head[60:64])[0]
                    if head[off_pe: off_pe + 4] == b"PE\x00\x00" or off_pe >= len(head):
                        return "pe"
                continue
            if fmt == "cafebabe":
                # Java class (major >= 45) vs Mach-O fat (small arch count).
                n = struct.unpack(">I", head[4:8])[0] if len(head) >= 8 else 0
                return "macho-fat" if 0 < n < 30 else "java-class"
            if fmt == "pickle?":
                if not looks_binary(head):
                    continue
                return "pickle"
            if fmt == "ttf" and not looks_binary(head):
                continue
            return fmt
    if len(head) >= 12 and head[:4] == b"RIFF":
        kind = head[8:12]
        return {b"WEBP": "webp", b"WAVE": "wav", b"AVI ": "avi"}.get(kind, "binary")
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "mp4"
    if len(head) >= 262 and head[257:262] == b"ustar":
        return "tar"
    if _pyc_version(head) and looks_binary(head[16:] or head):
        return "pyc"
    if looks_binary(head):
        return "binary"
    return None


# ---------------------------------------------------------------------------
# Binary decoders (bounded, stdlib only)
# ---------------------------------------------------------------------------
LIST_MEMBERS_MAX = 20_000
STRINGS_SCAN_BYTES = 1 << 20


def _entropy(data: bytes) -> float:
    if not data:
        return 0.0
    c = Counter(data)
    n = len(data)
    return -sum(v / n * math.log2(v / n) for v in c.values())


_STRINGS = re.compile(rb"[\x20-\x7e]{6,}")


def printable_strings(data: bytes, top: int = 16) -> tuple[int, list[str]]:
    found = _STRINGS.findall(data)
    seen: dict[bytes, None] = {}
    for s in found:
        if s.strip() and s not in seen:
            seen[s] = None
    # Longer, wordier strings say more than symbol soup.
    ranked = sorted(seen, key=lambda s: (-(len(re.findall(rb"[A-Za-z]{3,}", s))), -len(s)))
    return len(found), [s[:80].decode("ascii") for s in ranked[:top]]


def _image_info(fmt: str, head: bytes) -> dict:
    w = h = None
    if fmt == "png" and len(head) >= 26 and head[12:16] == b"IHDR":
        w, h, depth, ctype = struct.unpack(">IIBB", head[16:26])
        modes = {0: "gray", 2: "RGB", 3: "palette", 4: "gray+alpha", 6: "RGBA"}
        return {"width": w, "height": h, "bit_depth": depth, "color": modes.get(ctype, str(ctype))}
    if fmt == "gif" and len(head) >= 10:
        w, h = struct.unpack("<HH", head[6:10])
    elif fmt == "bmp" and len(head) >= 26:
        w, h = struct.unpack("<ii", head[18:26])
        h = abs(h)
    elif fmt == "webp" and len(head) >= 30:
        chunk = head[12:16]
        if chunk == b"VP8X":
            w = 1 + int.from_bytes(head[24:27], "little")
            h = 1 + int.from_bytes(head[27:30], "little")
        elif chunk == b"VP8 ":
            w, h = (v & 0x3FFF for v in struct.unpack("<HH", head[26:30]))
        elif chunk == b"VP8L":
            b = int.from_bytes(head[21:25], "little")
            w, h = (b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1
    elif fmt == "jpeg":
        from .images import _jpeg_size

        size = _jpeg_size(head)
        if size:
            w, h = size
    return {"width": w, "height": h} if w and h else {}


def _zip_info(path: Path) -> tuple[dict, list[str]]:
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()[:LIST_MEMBERS_MAX]
        names = [i.filename for i in infos if not i.is_dir()]
        total = sum(i.file_size for i in infos)
        details: dict[str, Any] = {"members": len(names), "uncompressed": human_size(total)}
        kind = None
        nset = set(names)
        if "word/document.xml" in nset:
            kind = "docx"
        elif "xl/workbook.xml" in nset:
            kind = "xlsx"
        elif "ppt/presentation.xml" in nset:
            kind = "pptx"
        elif "META-INF/MANIFEST.MF" in nset:
            kind = "jar"
        elif "mimetype" in nset and any(n.endswith(".opf") for n in names):
            kind = "epub"
        elif any(n.endswith(".dist-info/METADATA") for n in names):
            kind = "wheel"
        if kind:
            details["kind"] = kind
        extra: list[str] = []
        try:
            if kind == "docx":
                xml = zf.read("word/document.xml")[:4_000_000].decode("utf-8", "replace")
                paras = [re.sub(r"<[^>]+>", "", p) for p in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S)]
                paras = [p.strip() for p in paras if p.strip()]
                details["paragraphs"] = len(paras)
                extra += ["text: " + p[:160] for p in paras[:8]]
            elif kind == "xlsx":
                wb = zf.read("xl/workbook.xml")[:1_000_000].decode("utf-8", "replace")
                details["sheets"] = re.findall(r'<sheet[^>]*name="([^"]+)"', wb)[:30]
            elif kind == "wheel":
                meta = next(n for n in names if n.endswith(".dist-info/METADATA"))
                text = zf.read(meta)[:20_000].decode("utf-8", "replace")
                for key in ("Name", "Version", "Summary", "Requires-Python"):
                    m = re.search(rf"^{key}: (.+)$", text, re.M)
                    if m:
                        details[key.lower()] = m.group(1).strip()
        except (KeyError, StopIteration, zipfile.BadZipFile, OSError, RuntimeError):
            pass
        sizes = {i.filename: i.file_size for i in infos}
    return details, extra + listing_lines(names, sizes)


def _tar_info(path: Path) -> tuple[dict, list[str]]:
    names, sizes = [], {}
    with tarfile.open(path, "r:*") as tf:
        for n, m in enumerate(tf):
            if n >= LIST_MEMBERS_MAX:
                break
            if m.isfile():
                names.append(m.name)
                sizes[m.name] = m.size
    return {"members": len(names), "uncompressed": human_size(sum(sizes.values()))}, listing_lines(names, sizes)


def listing_lines(paths: list[str], sizes: Optional[dict] = None, budget: int = 40) -> list[str]:
    """Archive/glob member list: whole-path templates (``pkg{0..29}/lib/mod{0..19}.js
    x600``) first, then the remaining names per directory, rare names verbatim."""
    sizes = sizes or {}
    rows: list[str] = []
    groups, rest = group_names(paths, sizes)
    # A whole-path template spanning several directories says more than one per
    # directory; per-directory grouping then handles what is left.
    multi = [g for g in groups if len({m.rpartition("/")[0] for m in g["members"]}) > 1]
    taken = {m for g in multi for m in g["members"]}
    rows += [render_group(g) for g in multi]
    rest = [p for p in paths if p not in taken]
    by_dir: dict[str, list[str]] = {}
    for p in rest:
        d, _, n = p.rstrip("/").rpartition("/")
        by_dir.setdefault(d, []).append(n)
    for d in sorted(by_dir):
        pre = f"{d}/" if d else ""
        groups, singles = group_names(by_dir[d], {n: sizes.get(f"{pre}{n}") for n in by_dir[d]})
        rows += [render_group(g, prefix=pre) for g in groups]
        rows += [f"{pre}{n}  {human_size(sizes.get(pre + n))}" if sizes.get(pre + n) is not None else f"{pre}{n}" for n in singles]
    if len(rows) > budget:
        more = len(rows) - budget
        rows = rows[:budget] + [f"... {more} more rows ({len(by_dir)} directories)"]
    return rows


def _gzip_info(path: Path) -> tuple[dict, list[str]]:
    import gzip

    details: dict[str, Any] = {}
    with open(path, "rb") as f:
        head = f.read(512)
        if head[3:4] and head[3] & 0x08:  # FNAME
            end = head.find(b"\x00", 10)
            if end > 10:
                details["original_name"] = head[10:end].decode("latin-1")
        f.seek(-4, os.SEEK_END)
        details["uncompressed_mod_4GiB"] = human_size(struct.unpack("<I", f.read(4))[0])
    with gzip.open(path, "rb") as g:
        inner = g.read(SNIFF_BYTES)
    return details, _inner_stream(inner, details)


def _inner_stream(inner: bytes, details: dict) -> list[str]:
    if len(inner) >= 262 and inner[257:262] == b"ustar":
        details["inner"] = "tar archive"
        return []
    fmt = sniff(inner)
    if fmt:
        details["inner"] = FORMAT_LABEL.get(fmt, fmt)
        return []
    details["inner"] = "text"
    text = inner.decode("utf-8", "replace")
    rows: list[str] = []
    for ln in text.splitlines()[:40]:
        row = "head: " + ln[:160]
        if rows and rows[-1].split("  (x")[0] == row:
            prev = rows[-1]
            k = int(prev.rsplit("(x", 1)[1][:-1]) + 1 if "  (x" in prev else 2
            rows[-1] = f"{row}  (x{k})"
        else:
            rows.append(row)
        if len(rows) >= 8:
            break
    return rows


def _sqlite_info(path: Path) -> tuple[dict, list[str]]:
    uri = f"file:{path.as_posix()}?mode=ro&immutable=1"
    con = sqlite3.connect(uri, uri=True, timeout=1)
    budget = {"ops": 0}

    def abort() -> int:
        budget["ops"] += 1
        return 1 if budget["ops"] > 400 else 0  # ~400 x 10k VM steps

    con.set_progress_handler(abort, 10_000)
    rows: list[str] = []
    try:
        objs = con.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type DESC, name"
        ).fetchall()
        tables = [o for o in objs if o[0] == "table"]
        details: dict[str, Any] = {"tables": len(tables), "indexes": sum(o[0] == "index" for o in objs),
                                   "views": sum(o[0] == "view" for o in objs)}
        for _, name, _, _ in tables[:60]:
            try:
                cols = con.execute(f'PRAGMA table_info("{name}")').fetchall()
                coldesc = ", ".join(f"{c[1]} {c[2]}".strip() + (" PK" if c[5] else "") for c in cols[:24])
                if len(cols) > 24:
                    coldesc += f", +{len(cols) - 24} more"
            except sqlite3.Error:
                coldesc = "?"
            try:
                budget["ops"] = 0
                count = f"{con.execute(f'SELECT count(*) FROM \"{name}\"').fetchone()[0]:,} rows"
            except sqlite3.Error:
                count = "rows: count aborted (large)"
            rows.append(f"table {name}  {count}  ({coldesc})")
        for typ, name, tbl, _ in objs:
            if typ in ("index", "view", "trigger"):
                rows.append(f"{typ} {name}" + (f" on {tbl}" if typ != "view" else ""))
    finally:
        con.close()
    return details, rows[:80]


_ELF_MACHINE = {3: "x86", 62: "x86-64", 40: "ARM", 183: "AArch64", 243: "RISC-V", 8: "MIPS", 20: "PowerPC",
                21: "PowerPC64", 22: "s390"}
_ELF_TYPE = {1: "relocatable", 2: "executable", 3: "shared object/PIE", 4: "core dump"}
_PE_MACHINE = {0x14C: "x86", 0x8664: "x86-64", 0xAA64: "ARM64", 0x1C0: "ARM"}


def _exe_info(fmt: str, head: bytes) -> dict:
    d: dict[str, Any] = {}
    if fmt == "elf" and len(head) >= 20:
        d["class"] = "64-bit" if head[4] == 2 else "32-bit"
        endian = "<" if head[5] == 1 else ">"
        d["endian"] = "little" if endian == "<" else "big"
        etype, mach = struct.unpack(endian + "HH", head[16:20])
        d["type"] = _ELF_TYPE.get(etype, str(etype))
        d["machine"] = _ELF_MACHINE.get(mach, str(mach))
        m = re.search(rb"/lib[\w/.-]*ld-[\w.-]+\.so[\w.]*", head)
        if m:
            d["interpreter"] = m.group(0).decode()
    elif fmt == "pe" and len(head) >= 64:
        off = struct.unpack("<I", head[60:64])[0]
        if head[off: off + 4] == b"PE\x00\x00" and len(head) >= off + 24:
            mach, _, _, _, _, _, chars = struct.unpack("<HHIIIHH", head[off + 4: off + 24])
            d["machine"] = _PE_MACHINE.get(mach, hex(mach))
            d["type"] = "DLL" if chars & 0x2000 else "executable"
    elif fmt == "java-class" and len(head) >= 8:
        major = struct.unpack(">H", head[6:8])[0]
        d["java"] = f"Java {major - 44}" if major > 44 else str(major)
    elif fmt == "macho" and len(head) >= 8:
        endian = "<" if head[:4] in (b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe") else ">"
        cpu = struct.unpack(endian + "i", head[4:8])[0]
        d["cpu"] = {7: "x86", 0x01000007: "x86-64", 12: "ARM", 0x0100000C: "ARM64"}.get(cpu, str(cpu))
    return d


def _pdf_info(path: Path, size: int) -> tuple[dict, list[str]]:
    cap = 64 << 20
    with open(path, "rb") as f:
        data = f.read(cap)
    d: dict[str, Any] = {"version": data[5:8].decode("latin-1", "replace")}
    pages = len(re.findall(rb"/Type\s*/Page(?!s)\b", data))
    m = re.search(rb"/Count\s+(\d+)", data)
    if pages:
        d["pages"] = pages
    elif m:
        d["pages"] = int(m.group(1))
    if size > cap:
        d["pages_note"] = f"counted in first {human_size(cap)}"
    for key in (b"Title", b"Author", b"Producer"):
        m = re.search(rb"/" + key + rb"\s*\(([^)]{1,200})\)", data)
        if m:
            d[key.decode().lower()] = m.group(1).decode("latin-1", "replace")
    d["encrypted"] = bool(re.search(rb"/Encrypt\b", data))
    return d, []


def _wav_info(head: bytes) -> dict:
    i = 12
    d: dict[str, Any] = {}
    while i + 8 <= len(head):
        cid, size = head[i: i + 4], struct.unpack("<I", head[i + 4: i + 8])[0]
        if cid == b"fmt " and i + 24 <= len(head):
            _, ch, rate, byte_rate = struct.unpack("<HHII", head[i + 8: i + 20])
            d.update(channels=ch, sample_rate=rate)
            d["_byte_rate"] = byte_rate
        if cid == b"data" and d.get("_byte_rate"):
            d["duration_s"] = round(size / d["_byte_rate"], 2)
            break
        i += 8 + size + (size & 1)
    d.pop("_byte_rate", None)
    return d


def _npy_info(head: bytes) -> dict:
    try:
        hl = struct.unpack("<H", head[8:10])[0]
        return {"header": head[10: 10 + hl].decode("latin-1").strip()}
    except struct.error:
        return {}


def describe_binary(path: Path, fmt: str, size: int) -> dict:
    """``{"format", "label", "details", "outline", "hint"}`` for a binary file. Never raises."""
    out: dict[str, Any] = {"format": fmt, "label": FORMAT_LABEL.get(fmt, fmt), "details": {}, "outline": []}
    try:
        with open(path, "rb") as f:
            head = f.read(65_536)
    except OSError as e:
        out["details"]["error"] = str(e)
        return out
    details: dict[str, Any] = {}
    rows: list[str] = []
    hint = ""
    try:
        if fmt in ("png", "jpeg", "gif", "bmp", "webp", "tiff", "ico"):
            details = _image_info(fmt, head)
            hint = "AnalyzeImage describes what the image shows."
        elif fmt == "zip":
            details, rows = _zip_info(path)
        elif fmt == "tar" or (fmt in ("gzip", "bzip2", "xz") and _is_tarball(path)):
            details, rows = _tar_info(path)
            if fmt != "tar":
                details["compression"] = fmt
                out["label"] = f"tar archive ({fmt})"
        elif fmt == "gzip":
            details, rows = _gzip_info(path)
        elif fmt in ("bzip2", "xz"):
            import bz2
            import lzma

            opener = bz2.open if fmt == "bzip2" else lzma.open
            with opener(path, "rb") as g:
                inner = g.read(SNIFF_BYTES)
            rows = _inner_stream(inner, details)
        elif fmt == "sqlite":
            details, rows = _sqlite_info(path)
            hint = "Query it with Bash: sqlite3 -readonly FILE 'SELECT ...' (or python -c with sqlite3)."
        elif fmt in ("elf", "pe", "macho", "java-class"):
            details = _exe_info(fmt, head)
        elif fmt == "pyc":
            v = _pyc_version(head)
            details = {"python": v} if v else {}
            hint = "Compiled bytecode: read the .py source instead."
        elif fmt == "pdf":
            details, rows = _pdf_info(path, size)
            hint = "Text extraction: Bash `pdftotext FILE -` when poppler is installed."
        elif fmt == "wav":
            details = _wav_info(head)
        elif fmt == "mp4" and len(head) >= 12:
            details = {"brand": head[8:12].decode("latin-1", "replace").strip()}
        elif fmt == "npy":
            details = _npy_info(head)
        elif fmt == "pickle":
            hint = "Never unpickle untrusted data."
    except Exception as e:  # a corrupt file must still get a description
        details["decode_error"] = f"{type(e).__name__}: {str(e)[:120]}"
    if fmt in ("binary", "elf", "pe", "macho", "wasm") or (fmt == "zip" and not rows):
        with open(path, "rb") as f:
            blob = f.read(STRINGS_SCAN_BYTES)
        n, strs = printable_strings(blob)
        details["entropy_bits_per_byte"] = round(_entropy(head), 2)
        if details["entropy_bits_per_byte"] > 7.5:
            details["looks"] = "compressed or encrypted"
        if strs and details["entropy_bits_per_byte"] <= 7.5:
            rows.append(f"strings ({n} runs of 6+ printable chars in first {human_size(len(blob))}, distinct shown):")
            rows += ["  " + s for s in strs]
        if fmt == "binary":
            rows += hexdump(head[:96])
    out["details"] = details
    out["outline"] = rows
    if hint:
        out["hint"] = hint
    return out


def _is_tarball(path: Path) -> bool:
    try:
        return tarfile.is_tarfile(path)
    except (OSError, tarfile.TarError, EOFError):
        return False


def hexdump(data: bytes, base: int = 0) -> list[str]:
    rows = []
    for i in range(0, len(data), 16):
        chunk = data[i: i + 16]
        hexs = " ".join(f"{b:02x}" for b in chunk)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        rows.append(f"{base + i:08x}  {hexs:<47}  {text}")
    return rows


# ---------------------------------------------------------------------------
# Line templates (logs and other repetitive line files)
# ---------------------------------------------------------------------------
_NUM = re.compile(r"\d+")
# Hex ids with at least one digit and one letter (pure digit runs are numbers).
_HEX = re.compile(r"\b(?:0x)?[0-9a-fA-F]{8,}\b")


def _hex_or_number(m: "re.Match[str]") -> str:
    tok = m.group(0)
    return tok if tok.isdigit() else "<hex>"
_SLOT = "\x01"
KEY_CHARS = 400
TEMPLATES_MAX = 4_000
SLOT_VALUES_MAX = 8
TEMPLATE_SECONDS = 2.5


class _Tpl:
    __slots__ = ("count", "first", "lines", "example", "lo", "hi", "vals", "wide")

    def __init__(self, ln: int, line: str, nslots: int):
        self.count = 0
        self.first = ln
        self.lines: list[int] = []
        self.example = line.rstrip("\r\n")
        self.lo: list = [None] * nslots
        self.hi: list = [None] * nslots
        self.vals: list[dict] = [{} for _ in range(nslots)]
        # Width of each slot's digit run while constant (zero-padded counters), else -1.
        self.wide: list[int] = [0] * nslots


def line_templates(lines: Iterable[str], *, start: int = 1, seconds: float = TEMPLATE_SECONDS) -> dict:
    """Masked templates over ``lines``.

    Every digit run becomes a slot (``worker-3`` -> ``worker-#``, ``12:00:01``
    -> ``#:#:#``) and hex ids become ``<hex>``, so one template stands for all
    lines of the same shape. Per template: count, first line, a few line
    numbers, one example and, per slot, the numeric range and up to 8 distinct
    values. Stops after ``seconds`` (``complete`` False) so huge logs stay fast.
    """
    import time

    deadline = time.monotonic() + seconds
    tpls: dict[str, _Tpl] = {}
    n = start - 1
    overflow = 0
    complete = True
    sub_hex, sub_num, find_num = _HEX.sub, _NUM.sub, _NUM.findall
    for n, line in enumerate(lines, start=start):
        if not (n & 4095) and time.monotonic() > deadline:
            complete = False
            n -= 1
            break
        body = sub_hex(_hex_or_number, line[:KEY_CHARS].strip())
        key = sub_num(_SLOT, body)
        nums = find_num(body)
        t = tpls.get(key)
        if t is None:
            if len(tpls) >= TEMPLATES_MAX:
                overflow += 1
                continue
            t = tpls[key] = _Tpl(n, line, len(nums))
        t.count += 1
        if len(t.lines) < 6:
            t.lines.append(n)
        lo, hi, vals, wide = t.lo, t.hi, t.vals, t.wide
        for i, raw in enumerate(nums):
            v = int(raw) if len(raw) < 19 else 0
            if lo[i] is None or v < lo[i]:
                lo[i] = v
            if hi[i] is None or v > hi[i]:
                hi[i] = v
            d = vals[i]
            if len(d) <= SLOT_VALUES_MAX:
                d[raw] = None
            w = wide[i]
            if w == 0:
                wide[i] = len(raw)
            elif w != len(raw):
                wide[i] = -1
    return {"templates": tpls, "overflow": overflow, "lines": n - start + 1, "complete": complete}


def _render_template(key: str, t: _Tpl) -> str:
    parts = key.split(_SLOT)
    out = [parts[0]]
    for i, tail in enumerate(parts[1:]):
        vals = list(t.vals[i])
        if len(vals) == 1:
            out.append(vals[0])
        elif len(vals) <= 4 and len("|".join(vals)) < 40:
            out.append("{" + "|".join(sorted(vals, key=lambda v: (len(v), v))) + "}")
        else:
            w = t.wide[i] if t.wide[i] > 0 and any(v.startswith("0") for v in vals) else 0
            out.append("{" + f"{t.lo[i]:0{w}d}..{t.hi[i]:0{w}d}" + "}")
        out.append(tail)
    text = "".join(out)
    return text if len(text) <= 220 else text[:217] + "..."


def template_digest(lines: Iterable[str], *, budget_rows: int = 14, seconds: float = TEMPLATE_SECONDS) -> Optional[dict]:
    """Template digest of a line file, or ``None`` when its lines do not repeat.

    Shows the most frequent templates and, separately, the rare ones (each under
    1% of lines) with their line numbers — rare lines are where errors live.
    """
    res = line_templates(lines, seconds=seconds)
    tpls: dict[str, _Tpl] = res["templates"]
    total = res["lines"]
    rare_exact = False
    nonblank = sum(t.count for k, t in tpls.items() if k)
    if total < 50 or not tpls:
        return None
    ranked = sorted(tpls.items(), key=lambda kv: -kv[1].count)
    # Repetitive enough to be worth templating: few templates cover most lines.
    if len(tpls) > max(20, nonblank // 4) and sum(t.count for _, t in ranked[:10]) < 0.5 * nonblank:
        return None
    coverage = round(sum(t.count for _, t in ranked[:3]) / max(1, nonblank), 3)
    cut = max(2, 0.01 * nonblank)
    common = [(k, t) for k, t in ranked if k and t.count >= cut]
    rare = [(k, t) for k, t in ranked if k and t.count < cut]
    if not res["complete"] and isinstance(lines, list) and rare:
        # The time budget stopped the scan, but rare lines are the point: count
        # each shown rare template over the unscanned rest with one regex pass
        # (C speed), so "ERROR x402" is exact even when the scan was partial.
        rest = "\n".join(x.rstrip("\r\n") for x in lines[total:])
        rare.sort(key=lambda kv: (kv[1].count, kv[1].first))
        for k, t in rare[:RARE_RECOUNT]:
            rx = _template_regex(k)
            if rx is None:
                continue
            for m in rx.finditer(rest):
                t.count += 1
                if len(t.lines) < 6:
                    t.lines.append(total + rest.count("\n", 0, m.start()) + 1)
        rare_exact = True
    rows: list[str] = []
    for k, t in common[: budget_rows // 2]:
        rows.append(f"x{t.count:<7} first L{t.first}  {_render_template(k, t)}")
    if rare:
        rows.append(f"rare ({len(rare)} templates, {sum(t.count for _, t in rare)} lines, rarest first):")
        rare.sort(key=lambda kv: (kv[1].count, kv[1].first))
        for k, t in rare[: max(3, budget_rows - len(rows))]:
            where = ",".join(f"L{x}" for x in t.lines[:4]) + ("..." if t.count > 4 else "")
            rows.append(f"  x{t.count} {where}  {t.example[:200]}")
        if len(rare) > max(3, budget_rows - len(rows)):
            rows.append(f"  (+{len(rare) - max(3, budget_rows - len(rows))} more rare templates)")
    more = len(common) - budget_rows // 2
    if more > 0:
        rows.append(f"(+{more} more frequent templates not shown)")
    out = {
        "lines_scanned": total,
        "templates": len(tpls) + (1 if res["overflow"] else 0),
        "coverage_top3": coverage,
        "rows": rows,
    }
    if not res["complete"]:
        out["partial"] = (f"time budget hit after {total:,} lines; frequent-template counts cover those lines"
                          + (f", rare-template counts (first {RARE_RECOUNT}) cover the whole file" if rare_exact else " only"))
    return out


RARE_RECOUNT = 8


def _template_regex(key: str) -> "Optional[re.Pattern[str]]":
    """A multiline regex matching the lines a template key stands for."""
    if len(key) >= KEY_CHARS - 5:
        return None  # key was cut from a long line; cannot match reliably
    parts = []
    for i, chunk in enumerate(key.split(_SLOT)):
        if i:
            parts.append(r"\d+")
        parts.append("(?:0x)?[0-9a-fA-F]{8,}".join(re.escape(x) for x in chunk.split("<hex>")))
    try:
        return re.compile(r"^[ \t]*" + "".join(parts) + r"[ \t\r]*$", re.M)
    except re.error:
        return None


# ---------------------------------------------------------------------------
# Tables (CSV / TSV)
# ---------------------------------------------------------------------------
TABLE_STAT_ROWS = 200_000
DISTINCT_MAX = 1_000
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$")
_NULLS = frozenset({"", "na", "n/a", "null", "none", "nan", "-"})


def _cell_type(v: str) -> str:
    s = v.strip()
    if s.lower() in _NULLS:
        return "null"
    try:
        int(s)
        return "int"
    except ValueError:
        pass
    try:
        float(s)
        return "float"
    except ValueError:
        pass
    if s.lower() in ("true", "false"):
        return "bool"
    if _DATE.match(s):
        return "date"
    return "str"


class _Col:
    __slots__ = ("name", "types", "nulls", "lo", "hi", "distinct", "overflow", "maxlen")

    def __init__(self, name: str):
        self.name = name
        self.types: Counter = Counter()
        self.nulls = 0
        self.lo: Any = None
        self.hi: Any = None
        self.distinct: Counter = Counter()
        self.overflow = False
        self.maxlen = 0

    def add(self, v: str) -> None:
        t = _cell_type(v)
        if t == "null":
            self.nulls += 1
            return
        self.types[t] += 1
        self.maxlen = max(self.maxlen, len(v))
        key: Any = v.strip()
        if t in ("int", "float"):
            key = float(key) if t == "float" else int(key)
        if t != "str":
            self.lo = key if self.lo is None or _lt(key, self.lo) else self.lo
            self.hi = key if self.hi is None or _lt(self.hi, key) else self.hi
        if not self.overflow:
            self.distinct[v.strip()] += 1
            if len(self.distinct) > DISTINCT_MAX:
                self.overflow = True

    def render(self) -> str:
        if not self.types:
            return f"{self.name}: empty"
        kinds = [k for k, _ in self.types.most_common()]
        typ = kinds[0] if len(kinds) == 1 else (
            "float" if set(kinds) <= {"int", "float"} else "mixed(" + "/".join(kinds[:3]) + ")"
        )
        bits = [f"{self.name}: {typ}"]
        if self.lo is not None and typ in ("int", "float", "date", "bool"):
            bits.append(f"{self.lo}..{self.hi}")
        nd = len(self.distinct)
        bits.append(f"distinct {DISTINCT_MAX}+" if self.overflow else f"distinct {nd}")
        if not self.overflow and nd <= 12 and typ not in ("float",):
            bits.append("{" + ", ".join(f"{k} {c}" for k, c in self.distinct.most_common(5)) + (", ..." if nd > 5 else "") + "}")
        elif typ in ("str", "mixed") or typ.startswith("mixed"):
            bits.append("e.g. " + ", ".join(repr(k[:30]) for k, _ in self.distinct.most_common(2)))
        if self.nulls:
            bits.append(f"nulls {self.nulls}")
        return "  ".join(bits)


def _lt(a: Any, b: Any) -> bool:
    try:
        return a < b
    except TypeError:
        return str(a) < str(b)


def table_digest(text_iter: Iterable[str], delimiter: Optional[str] = None, *, sample_text: str = "") -> Optional[dict]:
    """Column profile of a delimited text table, or ``None`` when it is not one."""
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(sample_text[:20_000], delimiters=",\t;|").delimiter
        except csv.Error:
            return None
    reader = csv.reader(text_iter, delimiter=delimiter)
    try:
        header = next(reader)
    except (StopIteration, csv.Error):
        return None
    if len(header) < 2:
        return None
    has_header = not all(_cell_type(h) in ("int", "float") for h in header if h.strip())
    names = [h.strip() or f"col{i + 1}" for i, h in enumerate(header)] if has_header else [f"col{i + 1}" for i in range(len(header))]
    cols = [_Col(n) for n in names]
    rows = 0
    ragged = 0
    samples: list[list[str]] = [] if has_header else [header]
    scanned_all = True
    try:
        for row in reader:
            if rows >= TABLE_STAT_ROWS:
                scanned_all = False
                break
            rows += 1
            if len(samples) < 2:
                samples.append(row)
            if len(row) != len(cols):
                ragged += 1
            for c, v in zip(cols, row):
                c.add(v)
    except csv.Error as e:
        return {"error": f"csv parse stopped at data row {rows + 1}: {e}", "columns": len(cols)}
    if not has_header:
        rows += 1
    out = {
        "delimiter": {",": "comma", "\t": "tab", ";": "semicolon", "|": "pipe"}.get(delimiter, delimiter),
        "columns": len(cols),
        "header": has_header,
        "rows_scanned": rows,
        "scanned_all": scanned_all,
        "outline": [c.render() for c in cols[:40]],
    }
    if len(cols) > 40:
        out["outline"].append(f"... {len(cols) - 40} more columns")
    if ragged:
        out["ragged_rows"] = ragged
    out["sample_rows"] = [delimiter.join(r)[:200] for r in samples]
    return out


# ---------------------------------------------------------------------------
# JSON shapes (JSON documents and JSON Lines)
# ---------------------------------------------------------------------------
ARRAY_SAMPLE = 5_000


class _Shape:
    """Merged type of many JSON values at one position."""

    __slots__ = ("n", "types", "lo", "hi", "keys", "items", "lens", "values", "overflow", "sampled")

    def __init__(self):
        self.n = 0
        self.types: Counter = Counter()
        self.lo: Any = None
        self.hi: Any = None
        self.keys: dict[str, "_Shape"] = {}
        self.items: Optional["_Shape"] = None
        self.lens: list[int] = []
        self.values: Counter = Counter()
        self.overflow = False
        self.sampled = False

    def add(self, v: Any, depth: int = 0) -> None:
        self.n += 1
        if isinstance(v, bool):
            t = "bool"
        elif isinstance(v, int):
            t = "int"
        elif isinstance(v, float):
            t = "float"
        elif isinstance(v, str):
            t = "str"
        elif v is None:
            t = "null"
        elif isinstance(v, list):
            t = "array"
        else:
            t = "object"
        self.types[t] += 1
        if t in ("int", "float"):
            self.lo = v if self.lo is None or v < self.lo else self.lo
            self.hi = v if self.hi is None or v > self.hi else self.hi
        if t in ("str", "int", "bool") and not self.overflow:
            key = v if t != "str" else v[:40]
            self.values[key] += 1
            if len(self.values) > 50:
                self.overflow = True
                self.values.clear()
        if depth > 12:
            return
        if t == "object":
            for k, sub in v.items():
                if k not in self.keys:
                    if len(self.keys) >= 200:
                        continue
                    self.keys[k] = _Shape()
                self.keys[k].add(sub, depth + 1)
        elif t == "array":
            if len(self.lens) < 100_000:
                self.lens.append(len(v))
            if self.items is None:
                self.items = _Shape()
            step = max(1, len(v) // ARRAY_SAMPLE)
            for x in v[::step]:
                self.items.add(x, depth + 1)
            if step > 1:
                self.items.add(v[-1], depth + 1)
                self.sampled = True

    def render(self, budget: int = 900, depth: int = 0) -> str:
        parts = []
        for t, _ in self.types.most_common():
            parts.append(self._render_type(t, budget, depth))
        return " | ".join(parts) if parts else "?"

    def _render_type(self, t: str, budget: int, depth: int) -> str:
        if t in ("int", "float"):
            if self.lo == self.hi:
                return f"{t} ={self.lo}"
            return f"{t} {self.lo}..{self.hi}"
        if t in ("str", "bool"):
            if not self.overflow and 0 < len(self.values) <= 4:
                return t + " {" + "|".join(json.dumps(k, ensure_ascii=False)[:30] for k in self.values) + "}"
            return t
        if t == "null":
            return "null"
        if t == "array":
            if not self.lens:
                return "[]"
            lo, hi = min(self.lens), max(self.lens)
            n = str(lo) if lo == hi else f"{lo}..{hi}"
            inner = self.items.render(budget - 10, depth + 1) if self.items and self.items.n else "?"
            return f"[{n} x {inner}]" + (" (items sampled evenly)" if self.sampled else "")
        if t == "object":
            if depth > 6:
                return "{...}"
            fields = []
            used = 0
            total = self.types["object"]
            for k, sub in self.keys.items():
                opt = "?" if sub.n < total else ""
                piece = f"{k}{opt}: {sub.render(max(60, budget // 3), depth + 1)}"
                used += len(piece) + 2
                if used > budget:
                    fields.append(f"+{len(self.keys) - len(fields)} keys")
                    break
                fields.append(piece)
            return "{" + ", ".join(fields) + "}"
        return t


def json_shape(value: Any, budget: int = 1_200) -> str:
    s = _Shape()
    s.add(value)
    return s.render(budget)


def jsonl_digest(lines: Iterable[str], *, max_records: int = 200_000) -> Optional[dict]:
    shape = _Shape()
    good = bad = blank = 0
    first_bad = None
    scanned_all = True
    n = 0
    for n, line in enumerate(lines, start=1):
        if good + bad >= max_records:
            scanned_all = False
            break
        if not line.strip():
            blank += 1
            continue
        try:
            shape.add(json.loads(line))
            good += 1
        except (json.JSONDecodeError, ValueError, RecursionError):
            bad += 1
            first_bad = first_bad or n
    if good < 2 or bad > good:
        return None
    out = {"records": good, "scanned_all": scanned_all, "record_shape": shape.render(1_400)}
    if bad:
        out["invalid_lines"] = bad
        out["first_invalid_line"] = first_bad
    return out


# ---------------------------------------------------------------------------
# Long lines (minified / single-line files)
# ---------------------------------------------------------------------------
LINE_CLIP_CHARS = 2_000


def clip_line(line: str, n: int, limit: int = LINE_CLIP_CHARS) -> tuple[str, bool]:
    if len(line) <= limit:
        return line, False
    return f"{line[:limit]} ...[line {n} continues: {len(line) - limit:,} more chars; focus='<phrase>' shows a window]", True
