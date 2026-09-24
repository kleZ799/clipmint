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

The fonts ship with the app (assets/fonts), because a caption style that
depends on what is installed looks different on every PC that renders it.

Where the words sit depends on the layout. On the stacked layout they go on
the seam between the webcam and the gameplay -- the one place that covers
neither the face nor the action. Everywhere else they sit in the lower middle,
above the band the apps cover with their own buttons and captions.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Dict, List, Optional

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


def caption_y(layout: str, aspect_ratio: str, cam_panel_fraction: float) -> float:
    """Where the middle of the caption goes, as a fraction of frame height."""
    if layout == "stacked":
        # The seam between the two panels.
        return max(0.2, min(0.8, float(cam_panel_fraction)))
    if aspect_ratio == "16:9":
        return 0.82
    if aspect_ratio in ("1:1", "4:5"):
        return 0.78
    # 9:16: above the bottom fifth, where every app puts its own UI.
    return 0.70


def font_file(style: str) -> Optional[Path]:
    """The bundled font file a style is drawn in, if this copy has it."""
    preset = PRESETS.get(style)
    folder = asset_dir("fonts")
    if not preset or folder is None:
        return None
    path = folder / preset["file"]
    return path if path.exists() else None


def copy_font(style: str, dest_dir: Path) -> bool:
    """Put a style's font where libass will be told to look. False if absent."""
    src = font_file(style)
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
              y_frac: float) -> str:
    """An ASS script that captions `words` in `style`. "" when there is nothing
    to say."""
    preset = PRESETS.get(style)
    if not preset or not words:
        return ""

    words = [dict(w, text=_clean(w.get("word", ""), preset)) for w in words]
    words = [w for w in words if w["text"]]
    if not words:
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
    back = _ass_colour("141414", 0x00) if preset["box"] else _ass_colour("000000", 0x80)
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
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    return header + "\n".join(events) + "\n"
