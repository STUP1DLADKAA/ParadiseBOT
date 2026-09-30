# Bundled fonts

The dictionary card renderer (`functions/dictionary/render.py`) ships with its own
fonts so a card looks identical on every machine — no system font hunting, no tofu
boxes where an IPA symbol should be.

| File | Used for |
| --- | --- |
| `InterDisplay-SemiBold.ttf` | the headword |
| `Inter-SemiBold.ttf` | section labels, chips, part of speech |
| `Inter-Medium.ttf` | meta lines, synonym chips |
| `Inter-Regular.ttf` | definitions, IPA |
| `Inter-Italic.ttf` | example sentences |

## Provenance

[Inter](https://github.com/rsms/inter) 4.2 by Rasmus Andersson, SIL Open Font
License 1.1 (full text in `Inter-OFL.txt`; the licence carries **no** Reserved
Font Name, so subsets may keep the Inter name).

The bundled files are subsets of the official `.woff2` releases, converted to
TrueType and trimmed to the code points the renderer can actually draw. That
keeps all five faces at ~110 KB each instead of ~350 KB:

```bash
pyftsubset Inter-Regular.woff2 \
  --unicodes="U+0020-007E,U+00A0-00FF,U+0100-017F,U+0180-024F,U+0250-02AF,\
U+02B0-02FF,U+0300-036F,U+2010-201F,U+2020-2022,U+2026,U+2032-2033,\
U+2039-203A,U+20AC,U+2192,U+FB01-FB02" \
  --layout-features="kern,liga,calt,ccmp,mark,mkmk" --name-IDs="*" \
  --output-file=Inter-Regular.ttf --flavor=""
```

The ranges cover Latin, Latin Extended A/B, **IPA Extensions**, **spacing
modifier letters** (`ˈ ˌ ː`), combining diacritics, Greek-free punctuation and
the typographic marks the card uses.

## Swapping fonts

`functions/dictionary/fonts.py` resolves faces in this order:

1. these bundled files,
2. matching system fonts (Inter, Noto Sans, Source Sans, Roboto, Open Sans,
   Liberation Sans, DejaVu Sans, Helvetica, Arial, Segoe UI…),
3. Pillow's built-in bitmap font (degraded, but the card still renders).

Drop a differently-named TTF/OTF in this folder and it is picked up
automatically as long as the file name contains the role keyword
(`display`, `semibold`, `medium`, `italic`, or nothing for regular).
Anything a chosen face cannot draw falls back, glyph by glyph, to the next font
in the chain — that is how stray Greek or Cyrillic in an etymology still renders.
