"""Put the hook at the very front of the clip, even when it happens later.

The cut rules already open every clip on its hook line. That is the right
answer when the hook IS the opening line. It is not always available: some
moments are a build and a payoff, and the payoff is the part that stops a
scroll — a scream, a laugh, the thing going wrong. Open on the build and the
first second is a person talking quietly; open on the payoff and the clip makes
no sense.

So the clip gets a cold open: a second or two of its own loudest moment, played
first, then the clip in full from the top. The viewer meets the payoff before
they have decided whether to stay, and then watches it arrive properly. It is
the oldest trick in short-form editing and it works for the same reason the
rule book's first-frame rule does — the first second is the audition.

Two things keep it honest:

  * It only runs when the peak is actually late. If the loudest moment is
    already in the first few seconds, the clip opens on it anyway and a replay
    would just be a stutter.
  * The length it adds is reserved before the cut is chosen, not bolted on
    afterwards, so a request for 30-second clips still yields 30-second files.
"""
from __future__ import annotations

import os
from typing import Dict, Optional

from . import accel, proc

# How long the cold open runs. Long enough to register as a moment, short
# enough that the viewer is inside the real clip before they could get bored
# of the repeat. It was 1.9s, and a tester read that as the video glitching
# and restarting: too short to be a moment, so it looked like a skip.
REPLAY_SECONDS = 2.5
# How much of the run-in to include before the peak, so the sound lands
# rather than starting mid-scream.
REPLAY_LEAD = 0.8
# The hand-over from the cold open to the clip: a quick dip to white and back,
# with the sound faded across it. A hard cut back to the start is what made
# the cold open look like a glitch. A flash is the editing shorthand for
# "jump back in time", so the replay reads as a preview put there on purpose.
FLASH_OUT = 0.08
FLASH_IN = 0.12
FLASH_COLOUR = "white"
ALREADY_UP_FRONT = 6.0


def budget(enabled: bool) -> float:
    """Seconds to hold back from the clip length for the cold open."""
    return REPLAY_SECONDS if enabled else 0.0


def _has_audio(path: str) -> bool:
    try:
        out = proc.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=index", "-of", "csv=p=0", path],
            capture_output=True, text=True,
        ).stdout
    except Exception:
        return False
    return bool((out or "").strip())


def _anchor_offset(highlight: Dict, clip_length: float, kind: str = "other") -> Optional[float]:
    """Best cold-open anchor inside the clip in clip-relative seconds."""
    start = highlight.get("start_time")
    if start is None:
        return None
    s = float(start)
    prefer_payoff = (kind or "other").lower() not in ("stream", "sports")
    order = ("payoff_time", "hook_peak") if prefer_payoff else ("hook_peak", "payoff_time")
    # Also honour an already-remapped clip offset when passed directly
    if highlight.get("_clip_anchor_offset") is not None:
        return float(highlight["_clip_anchor_offset"])
    for key in order:
        val = highlight.get(key)
        if val is None:
            continue
        try:
            off = float(val) - s
        except (TypeError, ValueError):
            continue
        if 0.0 < off <= clip_length:
            return off
    return None


def _snap_to_words(a: float, b: float, words: Optional[list], clip_length: float) -> tuple:
    """Snap [a, b] onto real word boundaries so the cold open never cuts mid-word."""
    if not words:
        return a, b
    starts = [float(w["start"]) for w in words if float(w["end"]) > float(w["start"])]
    ends = [float(w["end"]) for w in words if float(w["end"]) > float(w["start"])]
    if starts:
        near_starts = [ws for ws in starts if abs(ws - a) <= 0.65]
        if near_starts:
            a = max(0.0, min(near_starts, key=lambda ws: abs(ws - a)) - 0.04)
    if ends:
        target_b = a + REPLAY_SECONDS
        valid_ends = [we for we in ends if a + 1.3 <= we <= min(clip_length, a + 2.9)]
        if valid_ends:
            b = min(valid_ends, key=lambda we: abs(we - target_b)) + 0.04
            b = min(clip_length, b)
    return round(a, 3), round(b, 3)


def choose_window(highlight: Dict, clip_length: float,
                  words: Optional[list] = None, kind: str = "other") -> Optional[tuple]:
    """Meaning-aware, word-snapped cold-open slice in clip-relative seconds."""
    offset = _anchor_offset(highlight, clip_length, kind=kind)
    if offset is None or offset < ALREADY_UP_FRONT:
        return None
    a = max(0.0, offset - REPLAY_LEAD)
    b = min(clip_length, a + REPLAY_SECONDS)
    a, b = _snap_to_words(a, b, words, clip_length)
    if b - a < 0.8:
        return None
    return a, b


def _window(highlight: Dict, clip_length: float) -> Optional[tuple]:
    """Which slice of the rendered clip to replay, in clip-relative seconds."""
    return choose_window(highlight, clip_length)


def _duration(path: str) -> float:
    try:
        out = proc.run_checked(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            what="ffprobe (clip duration)", capture_stdout=True,
        ).stdout
        return float((out or "0").strip())
    except Exception:
        return 0.0


def apply(clip_path: str, highlight: Dict) -> Optional[float]:
    """Prepend the hook to a rendered clip, in place.

    Returns the seconds added, or None if nothing was done. Never raises: a
    clip that fails to get its cold open is still a finished clip, and losing
    the whole render over a garnish would be a bad trade.
    """
    length = _duration(clip_path)
    if length <= 0:
        return None

    window = _window(highlight, length)
    if window is None:
        return None
    a, b = window

    root, ext = os.path.splitext(clip_path)
    tmp = f"{root}.hook{ext or '.mp4'}"

    out_at = max(0.0, (b - a) - FLASH_OUT)
    flash_out = f"fade=t=out:st={out_at:.3f}:d={FLASH_OUT:.3f}:color={FLASH_COLOUR}"
    flash_in = f"fade=t=in:st=0:d={FLASH_IN:.3f}:color={FLASH_COLOUR}"
    if _has_audio(clip_path):
        filt = (
            f"[0:v]split=2[va][vb];[0:a]asplit=2[aa][ab];"
            f"[va]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS,{flash_out}[v0];"
            f"[aa]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS,"
            f"afade=t=out:st={out_at:.3f}:d={FLASH_OUT:.3f}[a0];"
            f"[vb]setpts=PTS-STARTPTS,{flash_in}[v1];"
            f"[ab]asetpts=PTS-STARTPTS,afade=t=in:st=0:d={FLASH_IN:.3f}[a1];"
            f"[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]"
        )
        maps = ["-map", "[v]", "-map", "[a]", "-c:a", "aac", "-b:a", "192k"]
    else:
        filt = (
            f"[0:v]split=2[va][vb];"
            f"[va]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS,{flash_out}[v0];"
            f"[vb]setpts=PTS-STARTPTS,{flash_in}[v1];"
            f"[v0][v1]concat=n=2:v=1:a=0[v]"
        )
        maps = ["-map", "[v]"]

    def build(enc):
        return ["ffmpeg", "-y", "-loglevel", "error", "-i", clip_path,
                "-filter_complex", filt, *maps, *enc,
                "-movflags", "+faststart", tmp]

    try:
        accel.run_encode(build, what="ffmpeg (cold open)", crf=19)
        os.replace(tmp, clip_path)
    except Exception as e:
        print(f"[hook] could not add the cold open ({e}) — keeping the plain cut",
              flush=True)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return None

    added = b - a
    print(f"[hook] opened on the {a:.1f}s peak for {added:.1f}s, then the full clip",
          flush=True)
    return added
