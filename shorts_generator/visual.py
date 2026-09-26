"""What the picture does in each candidate, measured without a model.

The ranker reads a transcript and the signals module hears the audio. Neither
can see. On a real channel that let through clips that opened on two seconds
of a black death screen, or on a dim corridor with nothing moving, and those
are the clips a feed swipes past before a word lands. Brightness and motion
are cheap to measure straight from the source, so they are measured here for
every candidate before anything is cut:

  * opening brightness -- how much of the first 1.5 seconds is black or near
    black. A black frame is the one opening that is never a hook.
  * opening stillness  -- whether the first seconds move at all. A menu, a
    paused frame or a loading screen reads as "nothing is happening".
  * motion             -- how much the picture changes across the whole clip,
    ranked against the other candidates from the same video. Games, sport and
    IRL footage that moves tend to hold a stranger; a static frame has to be
    carried entirely by what is said.

Frames are tiny (48x27 grey) because only averages are needed. The opening is
decoded properly; the rest of the clip is read from keyframes only, which
costs almost nothing even on a 1440p60 source.

Best-effort like every other signal: a candidate that cannot be read gets no
`visual` entry and is ranked on everything else.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from . import proc

FRAME_W, FRAME_H = 48, 27
OPENING_SECONDS = 1.5
OPENING_FPS = 6
# A pixel is "lit" above this grey level. Mean brightness alone was fooled on
# real footage: a dark game scene with a bright webcam box and a black death
# screen with white text both average near 10. What separates them is how much
# of the frame is lit at all.
LIT_LEVEL = 40
# Under this share of lit pixels a frame is black for a viewer's purposes --
# a webcam box and a line of text on nothing. Measured: a dark room with only
# the webcam visible read 0.02-0.03, a dim but readable scene 0.08-0.13, an
# ordinary lit scene 0.35 and up.
BLACK_LIT = 0.06
# Under this average share the whole opening is dim.
DIM_LIT = 0.15
# Mean absolute difference between consecutive opening frames under which the
# opening is effectively a still image.
STILL_DIFF = 1.2


def _frames(cmd: List[str]) -> List[bytes]:
    size = FRAME_W * FRAME_H
    try:
        out = proc.run(cmd, capture_output=True, timeout=120).stdout or b""
    except Exception:
        return []
    return [out[i:i + size] for i in range(0, len(out) - size + 1, size)]


def _grey(source: str, start: float, seconds: float, fps: Optional[int],
          keyframes_only: bool = False) -> List[bytes]:
    vf = f"scale={FRAME_W}:{FRAME_H},format=gray"
    if fps:
        vf = f"fps={fps}," + vf
    cmd = ["ffmpeg", "-v", "error"]
    if keyframes_only:
        cmd += ["-skip_frame", "nokey"]
    cmd += ["-ss", f"{max(0.0, start):.3f}", "-t", f"{max(0.1, seconds):.3f}",
            "-i", source, "-an", "-vf", vf]
    if keyframes_only:
        cmd += ["-fps_mode", "passthrough"]
    cmd += ["-f", "rawvideo", "-"]
    return _frames(cmd)


def _mean(frame: bytes) -> float:
    return sum(frame) / len(frame) if frame else 0.0


def _lit(frame: bytes) -> float:
    return sum(1 for px in frame if px > LIT_LEVEL) / len(frame) if frame else 0.0


def _diff(a: bytes, b: bytes) -> float:
    if not a or not b:
        return 0.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def measure(source: str, start: float, end: float) -> Optional[Dict[str, float]]:
    """Brightness and motion for one span of the source, or None."""
    opening = _grey(source, start, OPENING_SECONDS, OPENING_FPS)
    if not opening:
        return None
    lumas = [_mean(f) for f in opening]
    lit = [_lit(f) for f in opening]
    black = sum(1 for v in lit if v < BLACK_LIT) / len(lit)
    still = [_diff(a, b) for a, b in zip(opening, opening[1:])]

    body = _grey(source, start, max(0.1, end - start), None, keyframes_only=True)
    moves = [_diff(a, b) for a, b in zip(body, body[1:])]
    return {
        "opening_luma": round(sum(lumas) / len(lumas), 1),
        "opening_lit": round(sum(lit) / len(lit), 3),
        "opening_black": round(black, 3),
        "opening_motion": round(sum(still) / len(still), 2) if still else 0.0,
        "motion": round(sum(moves) / len(moves), 2) if moves else 0.0,
    }


def measure_all(highlights: List[Dict], source: Optional[str]) -> int:
    """Attach `visual` to each highlight in place. Returns how many were read.

    Motion is also turned into `motion_rank`, 0-1 across this batch: a slow
    narrative game and a shooter move by very different amounts, and only the
    comparison between candidates from the same video means anything.
    """
    if not source or not highlights:
        return 0
    read = 0
    for h in highlights:
        try:
            v = measure(source, float(h["start_time"]), float(h["end_time"]))
        except Exception:
            v = None
        if v:
            h["visual"] = v
            read += 1
    moving = sorted(h["visual"]["motion"] for h in highlights if h.get("visual"))
    if len(moving) > 1:
        for h in highlights:
            v = h.get("visual")
            if v:
                below = sum(1 for m in moving if m < v["motion"])
                v["motion_rank"] = round(below / (len(moving) - 1), 3)
    elif moving:
        for h in highlights:
            if h.get("visual"):
                h["visual"]["motion_rank"] = 0.5
    if read:
        dark = sum(1 for h in highlights
                   if h.get("visual") and opening_penalty(h["visual"]) > 0)
        print(f"[visual] measured {read}/{len(highlights)} candidate(s) - "
              f"{dark} open dark or still", flush=True)
    return read


def opening_penalty(v: Dict[str, float]) -> float:
    """0-1: how much a clip's opening frames cost it.

    A black opening is disqualifying in all but name: most of a feed's test
    audience is gone before it ends. Dim or frozen openings cost less, since a
    line can still carry them.
    """
    if not v:
        return 0.0
    penalty = 0.0
    black = float(v.get("opening_black", 0.0))
    if black >= 0.3:
        penalty = max(penalty, 0.25 + 0.35 * min(1.0, black))
    if float(v.get("opening_lit", 1.0)) < DIM_LIT:
        penalty = max(penalty, 0.15)
    if float(v.get("opening_motion", 99.0)) < STILL_DIFF:
        penalty = max(penalty, 0.12)
    return round(penalty, 3)
