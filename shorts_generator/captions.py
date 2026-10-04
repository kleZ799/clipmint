"""Burned-in captions, one word at a time.

Most short-form video is watched with the sound off, and the captions every
Shorts tool now burns in are not subtitles in the old sense: a few words at a
time, big, in the middle of the frame, with the word being spoken lit up as it
is said. That is what this writes, as an ASS script for libass -- the renderer
ffmpeg's `subtitles` filter already uses -- so it costs no new dependency.

Four looks, each a complete preset rather than a pile of knobs:

  bold   white, heavy, uppercase, the spoken word in yellow and a little larger
  punch  tall condensed type, two words at a time, the spoken word in green
  clean  sentence case on a soft dark box, five words at a time, no bounce
  comic  a comic-book face with a purple outline, the spoken word in gold

Three things can be put over a style without unpicking it: the typeface (any
of FONTS, sized to fill the style's line), the colour of the spoken word and
of the rest of the line, and where the words sit (POSITIONS). The style keeps
everything else -- that is what makes it a look.

The fonts ship with the app (assets/fonts), because a caption style that
depends on what is installed looks different on every PC that renders it.

Left to itself, where the words sit depends on the layout. On the stacked layout they go on
the seam between the webcam and the gameplay -- the one place that covers
neither the face nor the action. Everywhere else they sit in the lower middle,
above the band the apps cover with their own buttons and captions.

The clip's hook line goes across the top for its first couple of seconds, in
the same face on a dark box. The packaging step has always written one per
clip (`hook_text`) and until this nothing put it on screen: a line that tells
a stranger what they are about to see, before the moment has arrived, is the
on-screen text every growth playbook asks for in the first two seconds.
"""
from __future__ import annotations

import difflib
import re
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# How long the hook line stays up, and how long it may run before it is cut.
HOOK_SECONDS = 2.6
HOOK_CHARS = 60

from .bundled import asset_dir

OFF = "off"
DEFAULT = "bold"

# Sizes are fractions of the shorter side of the frame, so a 1080x1920 Short
# and a 2160x3840 one get the same look rather than the same pixel count.
PRESETS: Dict[str, Dict] = {
    "bold": {
        "label": "Bold",
        "hint": "White, heavy, the spoken word in yellow",
        "font": "Montserrat Black", "file": "Montserrat-Black.ttf",
        "size": 0.084, "outline": 0.0095, "shadow": 0.004, "spacing": 0,
        "primary": "FFFFFF", "active": "FFE14D", "edge": "000000",
        "upper": True, "words": 3, "chars": 18, "pop": True, "box": False,
    },
    "punch": {
        "label": "Punch",
        "hint": "Tall type, two words at a time, the spoken word in green",
        "font": "Anton", "file": "Anton-Regular.ttf",
        "size": 0.112, "outline": 0.0085, "shadow": 0.006, "spacing": 1,
        "primary": "FFFFFF", "active": "4DFF7C", "edge": "000000",
        "upper": True, "words": 2, "chars": 14, "pop": True, "box": False,
    },
    "clean": {
        "label": "Clean",
        "hint": "Sentence case on a soft box, no bounce",
        "font": "Montserrat ExtraBold", "file": "Montserrat-ExtraBold.ttf",
        "size": 0.056, "outline": 0.013, "shadow": 0.0, "spacing": 0,
        "primary": "FFFFFF", "active": "FFD54A", "edge": "000000",
        "upper": False, "words": 5, "chars": 30, "pop": False, "box": True,
    },
    "comic": {
        "label": "Comic",
        "hint": "Comic-book type, purple outline, the spoken word in gold",
        "font": "Bangers", "file": "Bangers-Regular.ttf",
        "size": 0.104, "outline": 0.0095, "shadow": 0.005, "spacing": 2,
        "primary": "FFFFFF", "active": "FFD43B", "edge": "3B1F6E",
        "upper": True, "words": 3, "chars": 18, "pop": True, "box": False,
    },
}

CHOICES = (OFF, *PRESETS)

# Typefaces any style can be drawn in instead of its own. The style still
# decides everything else -- case, words per line, the pop, the box -- so a
# font is a change of face, not of look. `width` is how wide a run of
# capitals and of lower case sets, in ems (the same sample for every face),
# so a swapped face can be sized to fill the line its style was tuned for.
FONTS: Dict[str, Dict] = {
    "montserrat": {"label": "Montserrat", "font": "Montserrat Black",
                   "file": "Montserrat-Black.ttf", "width": (17.61, 14.88)},
    "anton": {"label": "Anton", "font": "Anton",
              "file": "Anton-Regular.ttf", "width": (11.24, 11.04)},
    "bebas": {"label": "Bebas Neue", "font": "Bebas Neue",
              "file": "BebasNeue-Regular.ttf", "width": (9.26, 9.26)},
    "poppins": {"label": "Poppins", "font": "Poppins Black",
                "file": "Poppins-Black.ttf", "width": (15.87, 14.00)},
    "archivo": {"label": "Archivo Black", "font": "Archivo Black",
                "file": "ArchivoBlack-Regular.ttf", "width": (18.33, 14.89)},
    "lilita": {"label": "Lilita One", "font": "Lilita One",
               "file": "LilitaOne-Regular.ttf", "width": (14.37, 11.38)},
    "luckiest": {"label": "Luckiest Guy", "font": "Luckiest Guy",
                 "file": "LuckiestGuy-Regular.ttf", "width": (13.93, 13.87)},
    "bangers": {"label": "Bangers", "font": "Bangers",
                "file": "Bangers-Regular.ttf", "width": (10.21, 10.21)},
    "marker": {"label": "Permanent Marker", "font": "Permanent Marker",
               "file": "PermanentMarker-Regular.ttf", "width": (16.88, 13.83)},
    "bungee": {"label": "Bungee", "font": "Bungee",
               "file": "Bungee-Regular.ttf", "width": (17.19, 17.19)},
}
# The styles' own faces, measured the same way, for sizing a swap against.
_OWN_WIDTH = {"Montserrat-ExtraBold.ttf": (17.41, 14.57),
              **{f["file"]: f["width"] for f in FONTS.values()}}

# Where the captions sit, when not left to the layout. Top stays below the
# hook line; bottom is the layout's own low place, clear of the apps' UI.
POSITIONS = ("auto", "top", "middle", "bottom")
_TOP, _MIDDLE = 0.27, 0.5

# Colours offered for the spoken word and for the rest of the line. Any
# other RRGGBB works too; these are the ones the picker shows.
HIGHLIGHTS = ("FFE14D", "4DFF7C", "FF4D4D", "4DD2FF", "FF5CC8", "FF9A3C", "FFFFFF")
TEXT_COLOURS = ("FFFFFF", "FFE14D", "4DD2FF", "000000")

# A pause this long between two words starts a new caption: the line on
# screen should end when the sentence does, not wait for the next one.
_BREAK_GAP = 0.45
# Hold a caption this long after its last word unless the next one is due.
_HOLD = 0.25
# Closer than this, two captions are joined edge to edge instead of blinking
# off and on again.
_JOIN_GAP = 0.6

# Scripts written without spaces between words. Whisper hands these back one
# character or morpheme at a time, and joining them with spaces is wrong.
_NO_SPACES = re.compile(r"[぀-ヿ㐀-鿿豈-﫿฀-๿]")


def normalise(value: Optional[str]) -> str:
    """A caption style this module knows, or `off`."""
    v = str(value or "").strip().lower()
    if v in ("", "none", "no", "false", "0"):
        return OFF
    return v if v in CHOICES else DEFAULT


def options() -> List[Dict]:
    """The styles, for the interface to offer."""
    return [{"value": OFF, "label": "Off", "hint": "No captions on the video"}] + [
        {"value": k, "label": p["label"], "hint": p["hint"]} for k, p in PRESETS.items()
    ]


def font_options() -> List[Dict]:
    """The faces a style can be swapped to, for the interface to offer."""
    return [{"value": k, "label": f["label"], "file": f["file"], "width": f["width"]}
            for k, f in FONTS.items()]


def normalise_font(value: Optional[str]) -> str:
    """A font this module knows, or "" for the style's own."""
    v = str(value or "").strip().lower()
    return v if v in FONTS else ""


def normalise_colour(value: Optional[str]) -> str:
    """An RRGGBB colour in capitals, or "" for the style's own."""
    v = str(value or "").strip().lstrip("#").upper()
    return v if re.fullmatch(r"[0-9A-F]{6}", v) else ""


def normalise_position(value: Optional[str]) -> str:
    v = str(value or "").strip().lower()
    return v if v in POSITIONS else "auto"


def caption_y(layout: str, aspect_ratio: str, cam_panel_fraction: float,
              position: str = "auto") -> float:
    """Where the middle of the caption goes, as a fraction of frame height."""
    if position == "top":
        return _TOP
    if position == "middle":
        return _MIDDLE
    if layout == "stacked" and position != "bottom":
        # The seam between the two panels.
        return max(0.2, min(0.8, float(cam_panel_fraction)))
    if aspect_ratio == "16:9":
        return 0.82
    if aspect_ratio in ("1:1", "4:5"):
        return 0.78
    # 9:16: above the bottom fifth, where every app puts its own UI.
    return 0.70


def look(style: str, font: str = "", colour: str = "", text_colour: str = "") -> Dict:
    """A style's preset with the face and colours someone chose put over it.

    A swapped face is sized to set the same width of line the style was
    tuned for, within limits: a condensed face scaled to a wide one's width
    would be taller than the frame wants. Dark text gets a light edge, and
    on the boxed style a light box, so it never sits black on black.
    """
    preset = dict(PRESETS.get(style) or {})
    if not preset:
        return preset
    face = FONTS.get(normalise_font(font))
    if face and face["file"] != preset["file"]:
        side = 0 if preset["upper"] else 1
        own = _OWN_WIDTH.get(preset["file"], face["width"])[side]
        scale = max(0.8, min(1.35, own / face["width"][side]))
        preset.update(font=face["font"], file=face["file"],
                      size=round(preset["size"] * scale, 4))
    colour, text_colour = normalise_colour(colour), normalise_colour(text_colour)
    if colour:
        preset["active"] = colour
    if text_colour:
        preset["primary"] = text_colour
    if _dark(preset["primary"]):
        preset["edge"] = "FFFFFF"
        preset["box_colour"] = "F2F2F2"
    return preset


def _dark(hex_rgb: str) -> bool:
    r, g, b = (int(hex_rgb[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b < 90


def font_file(style: str, font: str = "") -> Optional[Path]:
    """The bundled font file a style is drawn in, if this copy has it."""
    preset = look(style, font)
    folder = asset_dir("fonts")
    if not preset or folder is None:
        return None
    path = folder / preset["file"]
    return path if path.exists() else None


def copy_font(style: str, dest_dir: Path, font: str = "") -> bool:
    """Put a style's font where libass will be told to look. False if absent."""
    src = font_file(style, font)
    if src is None:
        return False
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest_dir / src.name)
    return True


# --- words into captions ---------------------------------------------------

def _clean(word: str, preset: Dict) -> str:
    text = str(word or "").strip()
    # Nothing in a caption can be allowed to read as an override block or a
    # line break to libass.
    text = text.replace("\\", "/").replace("{", "(").replace("}", ")")
    if preset["upper"]:
        # The big styles read as shouted words, and a trailing comma or full
        # stop on a shouted word only looks like a smudge. ! and ? stay: they
        # are how the line was said.
        text = text.rstrip(",.;:")
        text = text.upper()
    return text


def _joiner(words: List[Dict]) -> str:
    sample = "".join(str(w.get("word", "")) for w in words[:40])
    if not sample:
        return " "
    return "" if len(_NO_SPACES.findall(sample)) > len(sample) * 0.3 else " "


def chunk_words(words: List[Dict], max_words: int, max_chars: int,
                joiner: str = " ") -> List[List[Dict]]:
    """Group words into the lines shown one at a time.

    A line ends at its word limit, at its width limit, after a word that ends
    a clause, or before a pause -- whichever comes first.
    """
    chunks: List[List[Dict]] = []
    cur: List[Dict] = []
    width = 0
    for w in words:
        text = str(w.get("text", w.get("word", "")))
        if cur:
            gap = float(w["start"]) - float(cur[-1]["end"])
            too_long = width + len(joiner) + len(text) > max_chars
            if len(cur) >= max_words or too_long or gap > _BREAK_GAP:
                chunks.append(cur)
                cur, width = [], 0
        cur.append(w)
        width += (len(joiner) if len(cur) > 1 else 0) + len(text)
        if re.search(r"[.!?,;:]$", str(w.get("word", ""))):
            chunks.append(cur)
            cur, width = [], 0
    if cur:
        chunks.append(cur)
    return chunks


def _ass_time(t: float) -> str:
    cs = max(0, int(round(t * 100)))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_colour(hex_rgb: str, alpha: int = 0) -> str:
    r, g, b = hex_rgb[0:2], hex_rgb[2:4], hex_rgb[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def build_ass(words: List[Dict], style: str, width: int, height: int,
              y_frac: float, hook: str = "",
              hook_windows: Sequence[Tuple[float, float]] = (),
              font: str = "", colour: str = "", text_colour: str = "") -> str:
    """An ASS script that captions `words` in `style`. "" when there is nothing
    to say.

    `hook` is the clip's on-screen hook line, shown across the top during each
    of `hook_windows` (clip seconds). More than one window when a cold open
    will be put on the front afterwards: the replayed slice has to carry the
    line too, or the viewer's first second would be the one without it.

    `font`, `colour` and `text_colour` swap the style's face, the spoken
    word's colour and the rest of the line's -- see look().
    """
    preset = look(style, font, colour, text_colour)
    if not preset:
        return ""

    words = [dict(w, text=_clean(w.get("word", ""), preset)) for w in words or []]
    words = [w for w in words if w["text"]]
    hook = re.sub(r"\s+", " ", (hook or "").replace("{", "(").replace("}", ")")
                  .replace("\\", "/")).strip()[:HOOK_CHARS]
    windows = [(a, b) for a, b in hook_windows if b - a > 0.2] if hook else []
    if not words and not windows:
        return ""

    base = min(width, height)
    size = max(12, int(round(base * preset["size"])))
    outline = round(base * preset["outline"], 1)
    shadow = round(base * preset["shadow"], 1)
    margin = int(round(width * 0.07))
    primary = _ass_colour(preset["primary"])
    active = _ass_colour(preset["active"])
    edge = _ass_colour(preset["edge"])
    # The box is opaque on purpose. libass draws one per run of text, and the
    # lit word is its own run, so a see-through box shows a darker patch
    # wherever two of them overlap.
    back = (_ass_colour(preset.get("box_colour", "141414"), 0x00) if preset["box"]
            else _ass_colour("000000", 0x80))
    border_style = 3 if preset["box"] else 1

    joiner = _joiner(words)
    chunks = chunk_words(words, preset["words"],
                         max(6, preset["chars"] // (2 if not joiner else 1)), joiner)

    x = width // 2
    y = int(round(height * y_frac))
    pos = f"\\an5\\pos({x},{y})"

    events: List[str] = []
    for n, chunk in enumerate(chunks):
        chunk_end = float(chunk[-1]["end"]) + _HOLD
        if n + 1 < len(chunks):
            nxt = float(chunks[n + 1][0]["start"])
            chunk_end = nxt if nxt - float(chunk[-1]["end"]) < _JOIN_GAP else min(chunk_end, nxt)

        for k, w in enumerate(chunk):
            start = float(w["start"]) if k else float(chunk[0]["start"])
            end = float(chunk[k + 1]["start"]) if k + 1 < len(chunk) else chunk_end
            if end - start < 0.02:
                continue
            parts = []
            for j, other in enumerate(chunk):
                if j == k:
                    lit = f"\\1c{active}"
                    if preset["pop"]:
                        lit += "\\t(0,70,\\fscx116\\fscy116)\\t(70,150,\\fscx108\\fscy108)"
                    parts.append(f"{{{lit}}}{other['text']}{{\\1c{primary}\\fscx100\\fscy100}}")
                else:
                    parts.append(other["text"])
            lead = f"{{{pos}" + ("\\fad(60,0)" if k == 0 else "") + "}"
            events.append(
                f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Cap,,0,0,0,,"
                f"{lead}{joiner.join(parts)}"
            )

    # The hook line: the preset's face, sentence case, on a dark box across
    # the top. Its own layer, so it never trades places with a caption.
    hook_size = max(12, int(round(base * 0.06)))
    for a, b in windows:
        events.append(
            f"Dialogue: 1,{_ass_time(a)},{_ass_time(b)},Hook,,0,0,0,,"
            f"{{\\fad(80,200)}}{hook}"
        )

    if not events:
        return ""

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: TV.709\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding\n"
        f"Style: Cap,{preset['font']},{size},{primary},{primary},"
        f"{edge if not preset['box'] else back},{back},0,0,0,0,100,100,"
        f"{preset['spacing']},0,{border_style},{outline},{shadow},5,{margin},{margin},0,1\n"
        f"Style: Hook,{preset['font']},{hook_size},{_ass_colour('FFFFFF')},"
        f"{_ass_colour('FFFFFF')},{_ass_colour('141414', 0x30)},{_ass_colour('141414', 0x30)},"
        f"0,0,0,0,100,100,0,0,3,{round(hook_size * 0.32, 1)},0,8,{margin},{margin},"
        f"{int(round(height * 0.06))},1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    return header + "\n".join(events) + "\n"


# --- fixing the words ---------------------------------------------------------

def _key(word: str) -> str:
    return re.sub(r"[^\w']+", "", str(word or "").lower())


def plain_text(words: List[Dict]) -> str:
    """The words as the person fixing them sees them: one line of text."""
    joiner = _joiner(words)
    return joiner.join(str(w.get("word", "")).strip() for w in words).strip()


def retime(words: List[Dict], text: str) -> List[Dict]:
    """Put corrected text back on the timings Whisper heard it at.

    Whisper gets a name or a word wrong; the person fixing it types the line
    as it should read. The two are aligned word by word: words that did not
    change keep their exact timing (with the new spelling or punctuation),
    and a stretch that was rewritten -- one word for two, a name for a
    mishearing -- has its new words spread evenly over the time the old ones
    took. A deleted word simply drops out.
    """
    if not words:
        return []
    joiner = _joiner(words)
    tokens = list(text.strip()) if not joiner else text.split()
    tokens = [t for t in tokens if t.strip()]
    if not tokens:
        return []

    old = [_key(w.get("word")) for w in words]
    new = [_key(t) for t in tokens]
    out: List[Dict] = []
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            for k in range(i2 - i1):
                out.append({**words[i1 + k], "word": tokens[j1 + k]})
            continue
        if op == "delete" or j2 == j1:
            continue
        if i2 > i1:
            start, end = float(words[i1]["start"]), float(words[i2 - 1]["end"])
        else:
            # Words added where nothing was heard: the gap between neighbours,
            # or a short slot at the edge of the clip.
            start = float(words[i1 - 1]["end"]) if i1 > 0 else max(
                0.0, float(words[0]["start"]) - 0.3)
            end = float(words[i1]["start"]) if i1 < len(words) else start + 0.3 * (j2 - j1)
            if end - start < 0.1 * (j2 - j1):
                end = start + 0.1 * (j2 - j1)
        step = (end - start) / (j2 - j1)
        for k in range(j2 - j1):
            out.append({"start": round(start + k * step, 3),
                        "end": round(start + (k + 1) * step, 3),
                        "word": tokens[j1 + k]})
    out.sort(key=lambda w: w["start"])
    return out
