"""Drawing the dictionary card.

The card is built, not templated: every word goes through the same layout
pass, which measures real glyphs and stacks blocks on an 8px rhythm, so a
three-letter word and a forty-letter one produce the same visual language.

Layout happens in :func:`plan_card` and is pure data — a list of blocks
with exact boxes. Painting happens afterwards. That split is what lets the
tests assert "nothing overflows, nothing overlaps" without looking at
pixels, and it keeps the layout deterministic for the render cache.

Rules the layout enforces:

- everything lives inside the padding box; text is wrapped, then truncated
  with an ellipsis, never clipped;
- blocks that have no data are skipped entirely (no empty headings);
- the headword shrinks through a size ramp before it is ever truncated;
- any glyph the chosen face lacks is drawn from a fallback face.

If Pillow is missing the module still imports and :func:`render_card`
raises :class:`RenderUnavailable`, which the cog turns into a text card.
"""

from __future__ import annotations

import hashlib
import io
import logging
import threading
from dataclasses import dataclass, field

from .fonts import PILLOW_AVAILABLE, FontLibrary, FontSet
from .models import WordEntry

log = logging.getLogger(__name__)

try:  # pragma: no cover - mirrored by fonts.PILLOW_AVAILABLE
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]


class RenderUnavailable(RuntimeError):
    """Raised when there is no way to produce an image on this machine."""


# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Theme:
    """A compact Material Expressive token set for one rendered card.

    Discord gives image attachments no information about the viewer's app
    theme, so dark is the safe default next to Discord's surfaces.  The same
    layout also accepts :data:`LIGHT_THEME` for installations that prefer a
    light card; neither mode is a screenshot or a web-page imitation.
    """

    width: int = 1080
    padding: int = 72
    radius: int = 48
    min_height: int = 420
    max_height: int = 1240

    # Material-style surface and tonal-container tokens.
    background: tuple[int, int, int, int] = (18, 25, 22, 255)
    surface_edge: tuple[int, int, int, int] = (55, 71, 63, 255)
    surface_highlight: tuple[int, int, int, int] = (91, 112, 101, 100)
    surface_container: tuple[int, int, int, int] = (29, 39, 35, 255)
    surface_container_edge: tuple[int, int, int, int] = (56, 73, 64, 255)
    shadow: tuple[int, int, int, int] = (0, 0, 0, 72)

    accent: tuple[int, int, int, int] = (122, 226, 193, 255)
    accent_soft: tuple[int, int, int, int] = (122, 226, 193, 52)
    accent_container: tuple[int, int, int, int] = (18, 82, 65, 255)
    shape_primary: tuple[int, int, int, int] = (93, 212, 178, 28)
    shape_secondary: tuple[int, int, int, int] = (165, 196, 255, 18)
    hairline: tuple[int, int, int, int] = (52, 67, 60, 255)

    text: tuple[int, int, int, int] = (245, 249, 246, 255)
    text_secondary: tuple[int, int, int, int] = (193, 207, 199, 255)
    text_muted: tuple[int, int, int, int] = (144, 161, 151, 255)

    chip_fill: tuple[int, int, int, int] = (36, 48, 43, 255)
    chip_edge: tuple[int, int, int, int] = (67, 84, 75, 255)
    chip_text: tuple[int, int, int, int] = (218, 230, 222, 255)

    # Type ramp
    label_size: int = 15
    word_sizes: tuple[int, ...] = (92, 84, 76, 68, 60, 54, 48, 42, 36, 32)
    ipa_size: int = 29
    pos_size: int = 18
    definition_size: int = 29
    example_size: int = 25
    chip_size: int = 20
    footer_size: int = 17

    # Limits that keep the card compact inside a Discord message.
    max_definition_lines: int = 4
    max_example_lines: int = 2
    max_synonyms: int = 6
    max_antonyms: int = 4
    max_related: int = 4
    name: str = "dark"


DEFAULT_THEME = Theme()
LIGHT_THEME = Theme(
    name="light",
    background=(250, 253, 250, 255),
    surface_edge=(204, 217, 208, 255),
    surface_highlight=(255, 255, 255, 180),
    surface_container=(237, 246, 240, 255),
    surface_container_edge=(197, 218, 205, 255),
    shadow=(22, 39, 29, 36),
    accent=(0, 105, 79, 255),
    accent_soft=(0, 105, 79, 26),
    accent_container=(190, 244, 221, 255),
    shape_primary=(0, 140, 104, 20),
    shape_secondary=(61, 93, 168, 17),
    hairline=(207, 220, 211, 255),
    text=(25, 34, 29, 255),
    text_secondary=(65, 78, 69, 255),
    text_muted=(101, 116, 106, 255),
    chip_fill=(228, 239, 232, 255),
    chip_edge=(193, 210, 199, 255),
    chip_text=(39, 54, 45, 255),
)


def resolve_theme(name: str | None) -> Theme:
    """Resolve a user-facing card theme without exposing renderer internals."""
    return LIGHT_THEME if str(name or "").strip().casefold() == "light" else DEFAULT_THEME

# Pillow's FreeType objects are shared across renders; serialise painting.
_RENDER_LOCK = threading.Lock()
_LIBRARY = FontLibrary()


# ---------------------------------------------------------------------------
# Text measurement helpers
# ---------------------------------------------------------------------------


def text_width(font_set: FontSet, text: str, tracking: float = 0.0) -> float:
    if not text or not font_set.usable:
        return 0.0
    total = 0.0
    for run, font in font_set.runs(text):
        try:
            total += font.getlength(run)  # type: ignore[union-attr]
        except (AttributeError, OSError):  # pragma: no cover - exotic fonts
            total += len(run) * font_set.size * 0.55
    return total + tracking * max(0, len(text) - 1)


def line_height(font_set: FontSet, factor: float = 1.0) -> int:
    return int(round(font_set.size * factor))


def fit_text(font_set: FontSet, text: str, max_width: float, tracking: float = 0.0) -> str:
    """Trim a single line to fit, adding an ellipsis when it had to cut."""
    if text_width(font_set, text, tracking) <= max_width:
        return text
    ellipsis = "\u2026"
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if text_width(font_set, text[:middle].rstrip() + ellipsis, tracking) <= max_width:
            low = middle
        else:
            high = middle - 1
    return (text[:low].rstrip() + ellipsis) if low else ellipsis


def wrap_text(
    font_set: FontSet,
    text: str,
    max_width: float,
    max_lines: int,
) -> list[str]:
    """Greedy word wrap with character-level breaking for very long words."""
    words = str(text or "").split()
    if not words or max_lines <= 0:
        return []
    lines: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current:
            lines.append(current)
            current = ""

    for word in words:
        candidate = f"{current} {word}".strip()
        if text_width(font_set, candidate) <= max_width:
            current = candidate
            continue
        flush()
        if text_width(font_set, word) <= max_width:
            current = word
            if len(lines) >= max_lines:
                break
            continue
        # A single unbreakable word wider than the column: split it.
        piece = ""
        for char in word:
            if text_width(font_set, piece + char) <= max_width:
                piece += char
                continue
            lines.append(piece)
            piece = char
            if len(lines) >= max_lines:
                break
        current = piece
        if len(lines) >= max_lines:
            break
    flush()

    if len(lines) > max_lines:
        kept = lines[:max_lines]
        kept[-1] = fit_text(font_set, kept[-1] + "\u2026", max_width)
        return kept
    return lines


# ---------------------------------------------------------------------------
# Layout model
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Block:
    """One painted element with its exact box."""

    kind: str
    x: float
    y: float
    width: float
    height: float
    text: str = ""
    role: str = "regular"
    size: int = 0
    colour: tuple[int, int, int, int] = (255, 255, 255, 255)
    tracking: float = 0.0
    items: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    @property
    def bottom(self) -> float:
        return self.y + self.height

    @property
    def right(self) -> float:
        return self.x + self.width


@dataclass(slots=True)
class CardLayout:
    width: int
    height: int
    blocks: list[Block]
    theme: Theme

    def kinds(self) -> list[str]:
        return [block.kind for block in self.blocks]

    def find(self, kind: str) -> Block | None:
        for block in self.blocks:
            if block.kind == kind:
                return block
        return None


def _library() -> FontLibrary:
    return _LIBRARY


def plan_card(entry: WordEntry, theme: Theme = DEFAULT_THEME) -> CardLayout:
    """Measure everything and decide where it goes. No pixels are touched."""
    if not PILLOW_AVAILABLE:
        raise RenderUnavailable("Pillow is not installed")

    library = _library()
    left = float(theme.padding)
    content_width = float(theme.width - theme.padding * 2)
    blocks: list[Block] = []
    y = float(theme.padding)

    # -- micro label ------------------------------------------------------
    label_font = library.get("semibold", theme.label_size)
    blocks.append(
        Block(
            kind="eyebrow",
            x=left + 18,
            y=y,
            width=text_width(label_font, "DICTIONARY", 2.2),
            height=line_height(label_font, 1.2),
            text="DICTIONARY",
            role="semibold",
            size=theme.label_size,
            colour=theme.text_muted,
            tracking=2.2,
            meta={"dot": (left + 4, y + line_height(label_font, 0.5) - 4)},
        )
    )
    y = blocks[-1].bottom + 26

    # -- headword ---------------------------------------------------------
    word = entry.word or "—"
    word_font = library.get("display", theme.word_sizes[-1])
    chosen_size = theme.word_sizes[-1]
    for size in theme.word_sizes:
        candidate = library.get("display", size)
        if text_width(candidate, word) <= content_width:
            word_font, chosen_size = candidate, size
            break
    # Large display type wants a touch of negative tracking to look set,
    # not typed.
    word_tracking = -chosen_size * 0.018
    drawn_word = fit_text(word_font, word, content_width, word_tracking)
    blocks.append(
        Block(
            kind="word",
            x=left,
            y=y,
            width=text_width(word_font, drawn_word, word_tracking),
            height=line_height(word_font, 1.08),
            text=drawn_word,
            role="display",
            size=chosen_size,
            colour=theme.text,
            tracking=word_tracking,
        )
    )
    y = blocks[-1].bottom + 14

    # -- pronunciation ----------------------------------------------------
    ipa_font = library.get("regular", theme.ipa_size)
    variants = entry.phonetic_variants[:2]
    if variants:
        pronunciation_label = "PRONUNCIATION"
        blocks.append(
            Block(
                kind="pronunciation-label",
                x=left,
                y=y,
                width=text_width(label_font, pronunciation_label, 1.8),
                height=line_height(label_font, 1.15),
                text=pronunciation_label,
                role="semibold",
                size=theme.label_size - 1,
                colour=theme.text_muted,
                tracking=1.8,
            )
        )
        y = blocks[-1].bottom + 8
        row_height = line_height(ipa_font, 1.25)
        x = left
        for index, variant in enumerate(variants):
            spelling = fit_text(ipa_font, variant.text, content_width * 0.7)
            width = text_width(ipa_font, spelling)
            if index and x + width + 16 > left + content_width:
                break
            blocks.append(
                Block(
                    kind="ipa",
                    x=x,
                    y=y,
                    width=width,
                    height=row_height,
                    text=spelling,
                    role="regular",
                    size=theme.ipa_size,
                    colour=theme.text_secondary,
                )
            )
            x += width + 14
            if variant.label:
                chip = _chip_block(
                    library, theme, variant.label, x, y + 4, size=theme.label_size,
                    fill=theme.chip_fill, edge=theme.chip_edge, colour=theme.text_muted,
                    tracking=1.2, kind="region",
                )
                if chip.right <= left + content_width:
                    blocks.append(chip)
                    x = chip.right + 18
        y += row_height + 10

    y += 18
    blocks.append(
        Block(kind="rule", x=left, y=y, width=content_width, height=1, colour=theme.hairline)
    )
    y += 33

    # -- meaning surface --------------------------------------------------
    # A tonal container makes the primary meaning feel like a Material
    # component within the card, rather than a paragraph on a flat page.
    sense = entry.primary
    example = sense.example if sense is not None else ""
    if not example:
        example = entry.examples[0] if entry.examples else ""
    has_meaning = bool(sense is not None and (sense.definition or example))
    if has_meaning:
        surface_top = y - 16
        surface = Block(
            kind="meaning-surface",
            x=left - 20,
            y=surface_top,
            width=content_width + 20,
            height=1,  # resolved after the text has been measured
            meta={"radius": 32},
        )
        blocks.append(surface)
        y += 12

        definition_label = "DEFINITION"
        definition_label_block = Block(
            kind="definition-label",
            x=left,
            y=y,
            width=text_width(label_font, definition_label, 1.8),
            height=line_height(label_font, 1.15),
            text=definition_label,
            role="semibold",
            size=theme.label_size - 1,
            colour=theme.text_muted,
            tracking=1.8,
        )
        blocks.append(definition_label_block)
        row_bottom = definition_label_block.bottom
        if sense is not None and sense.part_of_speech:
            pill = _chip_block(
                library,
                theme,
                sense.part_of_speech.upper(),
                definition_label_block.right + 18,
                y - 8,
                size=theme.pos_size,
                fill=theme.accent_container,
                edge=(0, 0, 0, 0),
                colour=theme.accent,
                tracking=1.4,
                kind="pos",
                pad_x=16,
                pad_y=8,
            )
            if pill.right <= left + content_width:
                blocks.append(pill)
                row_bottom = max(row_bottom, pill.bottom)
        y = row_bottom + 19

        if sense is not None and sense.definition:
            definition_font = library.get("regular", theme.definition_size)
            lines = wrap_text(
                definition_font, sense.definition, content_width, theme.max_definition_lines
            )
            step = line_height(definition_font, 1.42)
            for index, text in enumerate(lines):
                blocks.append(
                    Block(
                        kind="definition",
                        x=left,
                        y=y + index * step,
                        width=text_width(definition_font, text),
                        height=step,
                        text=text,
                        role="regular",
                        size=theme.definition_size,
                        colour=theme.text,
                    )
                )
            if lines:
                y += len(lines) * step + 12

        if example:
            context_label = "IN CONTEXT"
            blocks.append(
                Block(
                    kind="example-label",
                    x=left,
                    y=y,
                    width=text_width(label_font, context_label, 1.8),
                    height=line_height(label_font, 1.15),
                    text=context_label,
                    role="semibold",
                    size=theme.label_size - 1,
                    colour=theme.text_muted,
                    tracking=1.8,
                )
            )
            y = blocks[-1].bottom + 8
            example_font = library.get("italic", theme.example_size)
            indent = 26.0
            lines = wrap_text(
                example_font, example, content_width - indent, theme.max_example_lines
            )
            if lines:
                step = line_height(example_font, 1.4)
                block_height = len(lines) * step
                blocks.append(
                    Block(
                        kind="example-rule",
                        x=left,
                        y=y + 6,
                        width=3,
                        height=max(4, block_height - 8),
                        colour=theme.accent,
                    )
                )
                for index, text in enumerate(lines):
                    blocks.append(
                        Block(
                            kind="example",
                            x=left + indent,
                            y=y + index * step,
                            width=text_width(example_font, text),
                            height=step,
                            text=text,
                            role="italic",
                            size=theme.example_size,
                            colour=theme.text_secondary,
                        )
                    )
                y += block_height + 4

        surface.height = y - surface_top + 18
        y = surface.bottom + 28

    # -- related word families -------------------------------------------
    y = _plan_chip_row(
        blocks, library, theme, "SYNONYMS",
        entry.all_synonyms(theme.max_synonyms), left, y, content_width, "synonym",
    )
    y = _plan_chip_row(
        blocks, library, theme, "ANTONYMS",
        entry.all_antonyms(theme.max_antonyms), left, y, content_width, "antonym",
    )
    y = _plan_chip_row(
        blocks, library, theme, "RELATED",
        entry.related[:theme.max_related], left, y, content_width, "related",
    )

    # -- footer -----------------------------------------------------------
    footer_font = library.get("medium", theme.footer_size)
    footer_left = _footer_text(entry)
    extra = _extra_note(entry)
    if not footer_left and not extra:
        # No footer content: no divider, no reserved space, no dead air.
        height = int(min(theme.max_height, max(theme.min_height, y + theme.padding - 24)))
        return CardLayout(width=theme.width, height=height, blocks=blocks, theme=theme)

    y += 6
    blocks.append(
        Block(kind="rule", x=left, y=y, width=content_width, height=1, colour=theme.hairline)
    )
    y += 24
    footer_height = line_height(footer_font, 1.2)
    if footer_left:
        blocks.append(
            Block(
                kind="footer",
                x=left,
                y=y,
                width=text_width(footer_font, footer_left),
                height=footer_height,
                text=fit_text(footer_font, footer_left, content_width * 0.62),
                role="medium",
                size=theme.footer_size,
                colour=theme.text_muted,
            )
        )
    if extra:
        extra = fit_text(footer_font, extra, content_width * 0.36)
        width = text_width(footer_font, extra)
        blocks.append(
            Block(
                kind="footer-right",
                x=left + content_width - width,
                y=y,
                width=width,
                height=footer_height,
                text=extra,
                role="medium",
                size=theme.footer_size,
                colour=theme.accent,
            )
        )
    y += footer_height

    height = int(min(theme.max_height, max(theme.min_height, y + theme.padding - 10)))
    return CardLayout(width=theme.width, height=height, blocks=blocks, theme=theme)


def _plan_chip_row(
    blocks: list[Block],
    library: FontLibrary,
    theme: Theme,
    label: str,
    values: tuple[str, ...],
    left: float,
    y: float,
    content_width: float,
    kind: str,
) -> float:
    if not values:
        return y
    label_font = library.get("semibold", theme.label_size - 1)
    blocks.append(
        Block(
            kind=f"{kind}-label",
            x=left,
            y=y,
            width=text_width(label_font, label, 2.0),
            height=line_height(label_font, 1.2),
            text=label,
            role="semibold",
            size=theme.label_size - 1,
            colour=theme.text_muted,
            tracking=2.0,
        )
    )
    y = blocks[-1].bottom + 14

    x = left
    row_height = 0.0
    rows = 1
    for value in values:
        chip = _chip_block(
            library, theme, value, x, y, size=theme.chip_size,
            fill=theme.chip_fill, edge=theme.chip_edge, colour=theme.chip_text, kind=kind,
        )
        if chip.right > left + content_width:
            if rows >= 2:
                break
            rows += 1
            x = left
            y += chip.height + 10
            chip = _chip_block(
                library, theme, value, x, y, size=theme.chip_size,
                fill=theme.chip_fill, edge=theme.chip_edge, colour=theme.chip_text, kind=kind,
            )
            if chip.right > left + content_width:
                break
        blocks.append(chip)
        row_height = chip.height
        x = chip.right + 10
    return y + row_height + 30


def _chip_block(
    library: FontLibrary,
    theme: Theme,
    text: str,
    x: float,
    y: float,
    *,
    size: int,
    fill: tuple[int, int, int, int],
    edge: tuple[int, int, int, int],
    colour: tuple[int, int, int, int],
    tracking: float = 0.0,
    kind: str = "chip",
    pad_x: int = 16,
    pad_y: int = 8,
) -> Block:
    font = library.get("medium" if kind not in ("pos", "region") else "semibold", size)
    label = fit_text(font, text, theme.width * 0.55, tracking)
    width = text_width(font, label, tracking) + pad_x * 2
    height = line_height(font, 1.0) + pad_y * 2
    return Block(
        kind=kind,
        x=x,
        y=y,
        width=width,
        height=height,
        text=label,
        role="medium" if kind not in ("pos", "region") else "semibold",
        size=size,
        colour=colour,
        tracking=tracking,
        meta={"fill": fill, "edge": edge, "pad_x": pad_x},
    )


def _footer_text(entry: WordEntry) -> str:
    bits: list[str] = []
    if entry.sources:
        host = entry.sources[0].split("//")[-1].split("/")[0].replace("www.", "")
        bits.append(host)
    if entry.licence:
        bits.append(entry.licence)
    return "  ·  ".join(bits)[:80]


def _extra_note(entry: WordEntry) -> str:
    extra = max(0, len(entry.senses) - 1)
    parts = entry.parts_of_speech
    if extra and len(parts) > 1:
        return f"+{extra} more meanings · {len(parts)} parts of speech"
    if extra:
        return f"+{extra} more meaning{'s' if extra > 1 else ''}"
    return ""


# ---------------------------------------------------------------------------
# Painting
# ---------------------------------------------------------------------------


def _draw_runs(draw, font_set: FontSet, xy: tuple[float, float], text: str, fill, tracking: float = 0.0) -> None:
    x, y = xy
    if not tracking:
        for run, font in font_set.runs(text):
            draw.text((x, y), run, font=font, fill=fill)
            try:
                x += font.getlength(run)
            except (AttributeError, OSError):  # pragma: no cover
                x += len(run) * font_set.size * 0.55
        return
    for char in text:
        face = font_set.face_for(char)
        font = face.font if face else font_set.primary
        draw.text((x, y), char, font=font, fill=fill)
        try:
            x += font.getlength(char) + tracking
        except (AttributeError, OSError):  # pragma: no cover
            x += font_set.size * 0.55 + tracking


def paint(layout: CardLayout) -> "Image.Image":
    theme = layout.theme
    library = _library()
    image = Image.new("RGBA", (layout.width, layout.height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # Card surface, edge and highlight use a Material tonal hierarchy. The
    # organic shapes beneath it stay deliberately quiet: this is a dictionary
    # component, not an infographic.
    draw.rounded_rectangle(
        (0, 0, layout.width - 1, layout.height - 1),
        radius=theme.radius,
        fill=theme.background,
        outline=theme.surface_edge,
        width=1,
    )
    draw.rounded_rectangle(
        (1, 1, layout.width - 2, layout.height - 2),
        radius=max(1, theme.radius - 1),
        outline=theme.surface_highlight,
        width=1,
    )

    # The restrained shape placement is deterministic per headword, which
    # keeps card caching valid while avoiding a repetitive static template.
    word_block = layout.find("word")
    word = word_block.text if word_block is not None else ""
    drift = int(hashlib.sha256(word.encode("utf-8")).hexdigest()[:4], 16) % 96
    ornament = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ornament_draw = ImageDraw.Draw(ornament)
    ornament_draw.ellipse((-170 + drift, -210, 300 + drift, 230), fill=theme.shape_primary)
    ornament_draw.ellipse(
        (
            layout.width - 260 - drift // 2,
            layout.height - 190,
            layout.width + 180 - drift // 2,
            layout.height + 170,
        ),
        fill=theme.shape_secondary,
    )
    image.alpha_composite(ornament)
    draw = ImageDraw.Draw(image)
    # A compact accent capsule is the one top-level flourish.
    draw.rounded_rectangle(
        (theme.padding, 0, theme.padding + 118, 5), radius=3, fill=theme.accent
    )

    for block in layout.blocks:
        if block.kind == "meaning-surface":
            radius = block.meta.get("radius", 28)
            # Two offset layers suggest elevation while remaining crisp in a
            # PNG attachment (no huge soft blur that turns muddy on mobile).
            draw.rounded_rectangle(
                (block.x + 3, block.y + 7, block.right + 3, block.bottom + 7),
                radius=radius,
                fill=theme.shadow,
            )
            draw.rounded_rectangle(
                (block.x, block.y, block.right, block.bottom),
                radius=radius,
                fill=theme.surface_container,
                outline=theme.surface_container_edge,
                width=1,
            )
            continue
        if block.kind == "rule":
            draw.rectangle(
                (block.x, block.y, block.right, block.y + block.height - 1),
                fill=block.colour,
            )
            continue
        if block.kind == "example-rule":
            draw.rounded_rectangle(
                (block.x, block.y, block.x + block.width, block.bottom),
                radius=2,
                fill=block.colour,
            )
            continue
        if block.kind in ("chip", "synonym", "antonym", "related", "pos", "region"):
            fill = block.meta.get("fill")
            edge = block.meta.get("edge")
            draw.rounded_rectangle(
                (block.x, block.y, block.right, block.bottom),
                radius=block.height / 2,
                fill=fill,
                outline=edge if edge and edge[3] else None,
                width=1,
            )
            font_set = library.get(block.role, block.size)
            pad_x = block.meta.get("pad_x", 16)
            text_y = block.y + (block.height - line_height(font_set, 1.0)) / 2 - block.size * 0.09
            _draw_runs(
                draw, font_set, (block.x + pad_x, text_y), block.text, block.colour, block.tracking
            )
            continue

        if block.kind == "eyebrow":
            dot = block.meta.get("dot")
            if dot:
                draw.ellipse((dot[0], dot[1], dot[0] + 7, dot[1] + 7), fill=theme.accent)

        if not block.text:
            continue
        font_set = library.get(block.role, block.size)
        _draw_runs(draw, font_set, (block.x, block.y), block.text, block.colour, block.tracking)

    return image


def render_card(entry: WordEntry, theme: Theme = DEFAULT_THEME) -> bytes:
    """Render a word into PNG bytes. Deterministic for the same input."""
    if not PILLOW_AVAILABLE or Image is None:
        raise RenderUnavailable("Pillow is not installed")
    with _RENDER_LOCK:
        layout = plan_card(entry, theme)
        image = paint(layout)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True, compress_level=7)
        return buffer.getvalue()


def card_signature(entry: WordEntry, theme: Theme = DEFAULT_THEME) -> str:
    """Stable cache key for a rendered card: same content, same picture."""
    sense = entry.primary
    parts = [
        entry.word,
        sense.part_of_speech if sense else "",
        sense.definition if sense else "",
        sense.example if sense else (entry.examples[0] if entry.examples else ""),
        "|".join(p.text for p in entry.phonetic_variants[:2]),
        "|".join(p.label for p in entry.phonetic_variants[:2]),
        "|".join(entry.all_synonyms(theme.max_synonyms)),
        "|".join(entry.all_antonyms(theme.max_antonyms)),
        "|".join(entry.related[:theme.max_related]),
        _footer_text(entry),
        _extra_note(entry),
        f"{theme.name}:{theme.width}x{theme.radius}",
    ]
    digest = hashlib.sha256("\u241f".join(parts).encode("utf-8")).hexdigest()
    return digest[:32]


def renderer_available() -> bool:
    return PILLOW_AVAILABLE and Image is not None
