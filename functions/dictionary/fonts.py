"""Font resolution for the dictionary card.

Three things have to be true for a card to look right on every machine:

1. **The right faces are found.** Bundled Inter first (``assets/fonts``),
   then a ranked list of system families, then Pillow's built-in bitmap
   font so a card still renders on a box with no fonts at all.
2. **IPA actually draws.** ``/ˈprɒmɪs/`` is full of code points many fonts
   skip. Each face's ``cmap`` table is parsed here (a small, dependency-free
   TrueType reader) so the renderer can split a string into runs and draw
   each run with the first font in the chain that really has the glyph —
   no tofu boxes, ever.
3. **It stays cheap.** Faces are opened once per (file, size) and reused.

No Discord, no network, and Pillow is imported defensively so the rest of
the bot keeps working if it is not installed.
"""

from __future__ import annotations

import logging
import os
import struct
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

try:  # pragma: no cover - exercised by the "no Pillow" path
    from PIL import ImageFont

    PILLOW_AVAILABLE = True
except ImportError:  # pragma: no cover
    ImageFont = None  # type: ignore[assignment]
    PILLOW_AVAILABLE = False

BUNDLED_DIR = Path(__file__).resolve().parents[2] / "assets" / "fonts"

# Roles the renderer asks for, and the keywords that identify a file.
ROLE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "display": ("display-semibold", "display", "extrabold", "bold"),
    "semibold": ("semibold", "demibold", "600", "bold"),
    "medium": ("medium", "500", "regular"),
    "regular": ("regular", "book", "400"),
    "italic": ("italic", "oblique"),
}

# Preferred families, best first. Everything here is a UI/text face with
# good Latin + IPA coverage.
PREFERRED_FAMILIES: tuple[str, ...] = (
    "inter",
    "notosans",
    "sourcesans",
    "sourcesans3",
    "roboto",
    "opensans",
    "lato",
    "publicsans",
    "firasans",
    "ibmplexsans",
    "liberationsans",
    "dejavusans",
    "freesans",
    "arimo",
    "helvetica",
    "arial",
    "segoeui",
    "tahoma",
    "verdana",
)

# Families worth keeping at the end of every fallback chain purely for
# their coverage: if a card ever has to draw Cyrillic, Greek or an exotic
# phonetic mark, one of these usually can.
WIDE_COVERAGE_FAMILIES: tuple[str, ...] = (
    "notosans",
    "dejavusans",
    "freesans",
    "liberationsans",
    "arialunicode",
    "arial",
    "segoeui",
    "helvetica",
)

_SYSTEM_DIRS: tuple[str, ...] = (
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "/Library/Fonts",
    "/System/Library/Fonts",
    "/System/Library/Fonts/Supplemental",
    os.path.expanduser("~/.fonts"),
    os.path.expanduser("~/.local/share/fonts"),
    os.path.expanduser("~/Library/Fonts"),
    os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts"),
)

_FONT_SUFFIXES = (".ttf", ".otf")
_MAX_SCAN = 600  # plenty; keeps start-up predictable on font-heavy machines


# ---------------------------------------------------------------------------
# A very small TrueType/OpenType cmap reader
# ---------------------------------------------------------------------------


def _read_cmap(path: str) -> frozenset[int] | None:
    """Return the code points a font file can draw, or None if unreadable."""
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    if len(data) < 12:
        return None

    tag = data[:4]
    if tag == b"ttcf":  # font collection: use the first font in it
        try:
            offset = struct.unpack_from(">I", data, 12)[0]
        except struct.error:
            return None
    elif tag in (b"\x00\x01\x00\x00", b"true", b"OTTO"):
        offset = 0
    else:
        return None

    try:
        num_tables = struct.unpack_from(">H", data, offset + 4)[0]
        cmap_offset = 0
        for index in range(num_tables):
            record = offset + 12 + index * 16
            name, _checksum, table_offset, _length = struct.unpack_from(">4sIII", data, record)
            if name == b"cmap":
                cmap_offset = table_offset
                break
        if not cmap_offset or cmap_offset + 4 > len(data):
            return None

        subtable_count = struct.unpack_from(">H", data, cmap_offset + 2)[0]
        best: tuple[int, int] | None = None  # (score, offset)
        for index in range(subtable_count):
            record = cmap_offset + 4 + index * 8
            if record + 8 > len(data):
                break
            platform, encoding, sub_offset = struct.unpack_from(">HHI", data, record)
            score = {
                (3, 10): 5,
                (0, 6): 5,
                (0, 4): 4,
                (3, 1): 3,
                (0, 3): 3,
                (0, 2): 2,
                (0, 1): 2,
                (0, 0): 1,
                (1, 0): 0,
            }.get((platform, encoding), -1)
            if score < 0:
                continue
            if best is None or score > best[0]:
                best = (score, cmap_offset + sub_offset)
        if best is None:
            return None
        return _parse_cmap_subtable(data, best[1])
    except (struct.error, IndexError, ValueError):
        return None


def _parse_cmap_subtable(data: bytes, offset: int) -> frozenset[int] | None:
    if offset + 4 > len(data):
        return None
    fmt = struct.unpack_from(">H", data, offset)[0]
    points: set[int] = set()

    if fmt == 4:
        seg_x2 = struct.unpack_from(">H", data, offset + 6)[0]
        segments = seg_x2 // 2
        ends = offset + 14
        starts = ends + seg_x2 + 2
        deltas = starts + seg_x2
        ranges = deltas + seg_x2
        for index in range(segments):
            end = struct.unpack_from(">H", data, ends + index * 2)[0]
            start = struct.unpack_from(">H", data, starts + index * 2)[0]
            delta = struct.unpack_from(">h", data, deltas + index * 2)[0]
            range_offset = struct.unpack_from(">H", data, ranges + index * 2)[0]
            if start > end or end == 0xFFFF and start == 0xFFFF:
                continue
            for code in range(start, min(end, 0xFFFE) + 1):
                if range_offset == 0:
                    glyph = (code + delta) & 0xFFFF
                else:
                    position = ranges + index * 2 + range_offset + (code - start) * 2
                    if position + 2 > len(data):
                        continue
                    glyph = struct.unpack_from(">H", data, position)[0]
                    if glyph:
                        glyph = (glyph + delta) & 0xFFFF
                if glyph:
                    points.add(code)
    elif fmt == 12:
        groups = struct.unpack_from(">I", data, offset + 12)[0]
        for index in range(min(groups, 20_000)):
            base = offset + 16 + index * 12
            if base + 12 > len(data):
                break
            start, end, glyph = struct.unpack_from(">III", data, base)
            if glyph == 0 or end < start or end - start > 0x10FFFF:
                continue
            points.update(range(start, min(end, 0x2FFFF) + 1))
    elif fmt == 6:
        first, count = struct.unpack_from(">HH", data, offset + 6)
        for index in range(count):
            position = offset + 10 + index * 2
            if position + 2 > len(data):
                break
            if struct.unpack_from(">H", data, position)[0]:
                points.add(first + index)
    elif fmt == 0:
        for code in range(256):
            position = offset + 6 + code
            if position < len(data) and data[position]:
                points.add(code)
    else:
        return None
    return frozenset(points)


@lru_cache(maxsize=64)
def coverage(path: str) -> frozenset[int]:
    """Code points a font can draw. Empty means "unknown, assume it can"."""
    points = _read_cmap(path)
    if points is None:
        log.debug("Could not read cmap for %s", os.path.basename(path))
        return frozenset()
    return points


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _normalise(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


@dataclass(frozen=True, slots=True)
class FontFile:
    path: str
    name: str        # file stem
    normalised: str  # lower-case, alphanumeric only
    bundled: bool

    def matches_role(self, role: str) -> int:
        """Lower is better; -1 means "wrong style, do not use"."""
        italic = "italic" in self.normalised or "oblique" in self.normalised
        if role == "italic":
            return 0 if italic else -1
        if italic:
            return -1
        for rank, keyword in enumerate(ROLE_KEYWORDS.get(role, ())):
            if _normalise(keyword) in self.normalised:
                return rank
        return len(ROLE_KEYWORDS.get(role, ())) + 1


def _scan(directory: str, *, bundled: bool) -> list[FontFile]:
    found: list[FontFile] = []
    root = Path(directory)
    if not root.is_dir():
        return found
    try:
        walker = os.walk(root, followlinks=False)
        for base, dirs, files in walker:
            dirs.sort()
            for filename in sorted(files):
                if not filename.lower().endswith(_FONT_SUFFIXES):
                    continue
                stem = Path(filename).stem
                found.append(
                    FontFile(
                        path=os.path.join(base, filename),
                        name=stem,
                        normalised=_normalise(stem),
                        bundled=bundled,
                    )
                )
                if len(found) >= _MAX_SCAN:
                    return found
    except OSError:  # pragma: no cover - unreadable font dir
        log.debug("Could not scan fonts in %s", directory, exc_info=True)
    return found


@lru_cache(maxsize=1)
def _catalogue() -> tuple[FontFile, ...]:
    files: list[FontFile] = _scan(str(BUNDLED_DIR), bundled=True)
    for directory in _SYSTEM_DIRS:
        files.extend(_scan(directory, bundled=False))
    return tuple(files)


def _family_rank(font: FontFile) -> int:
    for rank, family in enumerate(PREFERRED_FAMILIES):
        if font.normalised.startswith(family) or family in font.normalised:
            return rank
    return len(PREFERRED_FAMILIES)


@lru_cache(maxsize=1)
def wide_coverage_files() -> tuple[FontFile, ...]:
    """System faces kept as last-resort fallbacks for unusual code points."""
    out: list[tuple[int, FontFile]] = []
    for font in _catalogue():
        if font.bundled or font.matches_role("regular") < 0:
            continue
        for rank, family in enumerate(WIDE_COVERAGE_FAMILIES):
            if font.normalised.startswith(family):
                out.append((rank, font))
                break
    out.sort(key=lambda item: (item[0], item[1].name))
    return tuple(font for _rank, font in out[:3])


def candidates_for(role: str) -> tuple[FontFile, ...]:
    """Every usable file for a role, best first (bundled always wins)."""
    scored: list[tuple[tuple[int, int, int, str], FontFile]] = []
    for font in _catalogue():
        style = font.matches_role(role)
        if style < 0:
            continue
        scored.append(((0 if font.bundled else 1, _family_rank(font), style, font.name), font))
    scored.sort(key=lambda item: item[0])
    return tuple(font for _key, font in scored)


# ---------------------------------------------------------------------------
# Font sets (a face plus its fallback chain)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=128)
def _load(path: str, size: int):
    if not PILLOW_AVAILABLE:  # pragma: no cover
        return None
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        log.debug("Could not open font %s", path, exc_info=True)
        return None


@lru_cache(maxsize=4)
def _default_font():  # pragma: no cover - only on a font-less machine
    if not PILLOW_AVAILABLE:
        return None
    try:
        return ImageFont.load_default()
    except Exception:  # noqa: BLE001
        return None


@dataclass(frozen=True, slots=True)
class LoadedFace:
    font: object          # PIL.ImageFont.FreeTypeFont
    coverage: frozenset[int]

    def covers(self, char: str) -> bool:
        if not self.coverage:
            return True  # unknown coverage: assume yes rather than drop text
        return ord(char) in self.coverage


class FontSet:
    """A primary face plus fallbacks, able to split text into drawable runs."""

    __slots__ = ("faces", "role", "size")

    def __init__(self, role: str, size: int, faces: list[LoadedFace]) -> None:
        self.role = role
        self.size = size
        self.faces = faces

    @property
    def primary(self):
        return self.faces[0].font if self.faces else None

    @property
    def usable(self) -> bool:
        return bool(self.faces)

    def face_for(self, char: str) -> LoadedFace | None:
        if not self.faces:
            return None
        if char in (" ", "\u00a0"):
            return self.faces[0]
        for face in self.faces:
            if face.covers(char):
                return face
        return self.faces[0]

    def runs(self, text: str) -> list[tuple[str, object]]:
        """Split text so every run is drawn by a font that has its glyphs."""
        if not text or not self.faces:
            return [(text, self.primary)] if text else []
        out: list[tuple[str, object]] = []
        buffer: list[str] = []
        current = None
        for char in text:
            face = self.face_for(char) or self.faces[0]
            if current is None or face.font is current:
                current = face.font
                buffer.append(char)
                continue
            out.append(("".join(buffer), current))
            buffer = [char]
            current = face.font
        if buffer:
            out.append(("".join(buffer), current))
        return out


class FontLibrary:
    """Resolves roles to font sets once and caches the result."""

    def __init__(self, *, extra_dir: str | None = None) -> None:
        self._extra_dir = extra_dir
        self._sets: dict[tuple[str, int], FontSet] = {}

    def get(self, role: str, size: int) -> FontSet:
        key = (role, int(size))
        cached = self._sets.get(key)
        if cached is not None:
            return cached

        faces: list[LoadedFace] = []
        seen: set[str] = set()

        def add(path: str) -> None:
            if path in seen:
                return
            font = _load(path, int(size))
            if font is None:
                return
            seen.add(path)
            faces.append(LoadedFace(font, coverage(path)))

        for font_file in candidates_for(role)[:3]:
            add(font_file.path)
        # Then sibling faces (a bundled italic can still draw a stray glyph)…
        for other_role in ("regular", "semibold", "medium", "display", "italic"):
            for font_file in candidates_for(other_role)[:1]:
                if len(faces) >= 6:
                    break
                add(font_file.path)
        # …and finally, always, a wide-coverage system face. Without this the
        # chain can end up entirely bundled, and a Greek theta in an IPA
        # transcription would silently render as nothing.
        for font_file in wide_coverage_files():
            add(font_file.path)

        if not faces:  # pragma: no cover - no usable font files at all
            default = _default_font()
            if default is not None:
                faces.append(LoadedFace(default, frozenset()))

        font_set = FontSet(role, int(size), faces)
        self._sets[key] = font_set
        return font_set

    def clear(self) -> None:
        self._sets.clear()


def describe() -> str:
    """One-line summary for start-up logs."""
    if not PILLOW_AVAILABLE:
        return "Pillow missing — dictionary cards fall back to text"
    bundled = [font.name for font in _catalogue() if font.bundled]
    if bundled:
        return f"bundled fonts: {', '.join(sorted(bundled))}"
    chosen = candidates_for("regular")
    return f"system fonts: {chosen[0].name if chosen else 'none (bitmap fallback)'}"


def reset_caches() -> None:
    """Testing / hot-reload helper."""
    _catalogue.cache_clear()
    wide_coverage_files.cache_clear()
    _load.cache_clear()
    coverage.cache_clear()


if sys.platform == "darwin":  # pragma: no cover - platform detail
    PREFERRED_FAMILIES = ("inter", "sfnstext", "helveticaneue", *PREFERRED_FAMILIES)
