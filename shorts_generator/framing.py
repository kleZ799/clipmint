"""Where a clip's crop window sits, and putting it somewhere by hand.

Every layout cuts a vertical window out of a wider source. Where that window
goes is decided automatically -- following a face, centring, or nudging the
gameplay away from the webcam -- and automatic is sometimes wrong: the face
follow locks onto the wrong person, the centre crop cuts the action in half,
the gameplay panel shows the empty side of the screen. Every clipping tool's
reviews say the same thing about this, and the fix people ask for is the
obvious one: let me drag the window to where it should be.

A position set by hand is stored on the clip as a fraction of the window's
travel -- 0 is flush left (or top), 1 flush right (or bottom), 0.5 centred --
so it means the same thing whatever resolution the clip is rendered at, and
survives every later re-render of the clip: a trim, a caption fix, a retry.

What the window covers depends on the layout. On the face-follow and centre
layouts it is the whole picture. On the webcam-over-gameplay layout the
webcam panel is found automatically, and the window is the gameplay panel
under it -- which is the part that is usually wrong, because the game's action
is not always in the middle of the screen.
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

PICTURE = "picture"
GAMEPLAY = "gameplay"

# The caption's height as a fraction of the frame, when it is set by hand.
# Not above a quarter of the way down: the clip's hook line sits across the
# top for its first seconds, and a caption moved up there would sit on it.
CAPTION_Y_MIN = 0.25
CAPTION_Y_MAX = 0.88


def _clamp01(v: float) -> float:
    return 0.0 if v < 0 else (1.0 if v > 1 else float(v))


def normalise(frame) -> Optional[Dict[str, float]]:
    """A stored frame position, cleaned up, or None for "automatic"."""
    if not isinstance(frame, dict):
        return None
    out: Dict[str, float] = {}
    for key in ("x", "y"):
        v = frame.get(key)
        if v is None:
            continue
        try:
            out[key] = round(_clamp01(float(v)), 4)
        except (TypeError, ValueError):
            continue
    return out or None


def caption_y(value) -> Optional[float]:
    """A caption height set by hand, kept clear of the hook line; or None."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return round(max(CAPTION_Y_MIN, min(CAPTION_Y_MAX, v)), 4)


def picture_window(src_w: int, src_h: int, out_w: int, out_h: int) -> Tuple[int, int]:
    """The largest window at the output's shape that fits inside the source."""
    target = out_w / out_h
    if target < src_w / src_h:
        h = src_h
        w = int(h * target)
    else:
        w = src_w
        h = int(w / target)
    return max(2, min(src_w, w - w % 2)), max(2, min(src_h, h - h % 2))


def gameplay_window(src_w: int, src_h: int, out_w: int, out_h: int,
                    cam_panel_fraction: float) -> Tuple[int, int]:
    """The full-height window the gameplay panel is cut from, under a webcam."""
    cam_h = int(out_h * cam_panel_fraction)
    cam_h -= cam_h % 2
    game_h = max(2, out_h - cam_h)
    w = min(int(src_h * (out_w / game_h)), src_w)
    return max(2, w - w % 2), src_h


def place(frame: Optional[Dict[str, float]], src_w: int, src_h: int,
          win_w: int, win_h: int, default: Tuple[int, int]) -> Tuple[int, int]:
    """The window's top-left corner: from `frame` where it says, else `default`."""
    x, y = default
    frame = normalise(frame) or {}
    if "x" in frame:
        x = int(round(frame["x"] * max(0, src_w - win_w)))
    if "y" in frame:
        y = int(round(frame["y"] * max(0, src_h - win_h)))
    x = max(0, min(max(0, src_w - win_w), x))
    y = max(0, min(max(0, src_h - win_h), y))
    return x & ~1, y & ~1


def fraction(pos: int, src: int, win: int) -> float:
    """A window position in pixels as the stored 0-1 fraction of its travel."""
    travel = src - win
    return round(_clamp01(pos / travel), 4) if travel > 0 else 0.5


def summary(layout_info) -> Optional[Dict]:
    """What a render says about its framing, small enough to keep on the clip.

    `layout_info` is what a renderer returned for the clip. Only what the
    frame editor needs survives: which panel the window was, and where the
    automatic choice put it, as a fraction, when that is a single place.
    """
    if not isinstance(layout_info, dict):
        return None
    out: Dict = {"panel": layout_info.get("panel") or PICTURE}
    for key in ("x", "y"):
        if isinstance(layout_info.get(key), (int, float)):
            out[key] = round(float(layout_info[key]), 4)
    if layout_info.get("manual"):
        out["manual"] = True
    cam = layout_info.get("cam")
    if out["panel"] == GAMEPLAY and isinstance(cam, dict):
        # Where the webcam was found, so the editor can show it is taken care
        # of and only the game underneath is being placed.
        out["cam"] = {k: int(cam[k]) for k in ("x", "y", "w", "h")
                      if isinstance(cam.get(k), (int, float))}
    return out
