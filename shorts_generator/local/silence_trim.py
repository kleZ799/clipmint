"""Remove silent gaps from clips — inspired by auto-editor.

All work is done with ffmpeg's ``silencedetect`` filter and the ``select``
video/audio filter, so there are no new Python dependencies.

Typical use: pass the clip path to :func:`trim_silences` after rendering a
clip.  If nothing meaningful is removed (less than 0.5 s total), the original
path is returned and the caller is not asked to swap the file.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List, Tuple

from .. import proc


def detect_silences(
    path: str,
    noise_db: float = -35.0,
    min_duration: float = 0.4,
) -> List[Tuple[float, float]]:
    """Return ``(start, end)`` pairs of silent intervals detected by ffmpeg.

    Args:
        path:          Path to the audio or video file.
        noise_db:      Silence threshold in dBFS.  Louder = more aggressive.
        min_duration:  Minimum duration in seconds to count as silence.

    Returns:
        A list of ``(start_seconds, end_seconds)`` tuples, possibly empty.
    """
    try:
        result = proc.run(
            [
                "ffmpeg", "-v", "error", "-i", path,
                "-af", f"silencedetect=noise={noise_db}dB:d={min_duration}",
                "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
        )
        stderr = result.stderr or ""
    except Exception:
        return []

    starts = re.findall(r"silence_start: ([0-9.]+)", stderr)
    ends   = re.findall(r"silence_end: ([0-9.]+)",   stderr)
    return [(float(s), float(e)) for s, e in zip(starts, ends)]


def _video_duration(path: str) -> float:
    """Return duration in seconds via ffprobe, or 0.0 on failure."""
    try:
        r = proc.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "csv=p=0", path,
            ],
            capture_output=True,
            text=True,
        )
        return float((r.stdout or "0").strip() or "0")
    except Exception:
        return 0.0


def trim_silences(
    input_path: str,
    output_path: str,
    noise_db: float = -35.0,
    min_silence: float = 0.5,
    pad_seconds: float = 0.08,
) -> str:
    """Cut silent gaps longer than *min_silence* seconds from a clip.

    Uses only ffmpeg — no new Python dependencies.

    Args:
        input_path:   Source video clip.
        output_path:  Where to write the trimmed clip.
        noise_db:     Silence threshold in dBFS (more negative = quieter).
        min_silence:  Minimum silent gap to remove (seconds).
        pad_seconds:  Keep this much audio on each side of a cut so consonants
                      at word edges are not clipped.

    Returns:
        *output_path* if trimming was performed, *input_path* if no meaningful
        silence was found (so the caller does not have to swap the file).
    """
    silences = detect_silences(input_path, noise_db, min_silence)
    if not silences:
        return input_path

    total_dur = _video_duration(input_path)

    # Build keep-intervals: everything that is NOT silent.
    keep: List[Tuple[float, float]] = []
    cursor = 0.0
    for sil_start, sil_end in silences:
        seg_end = max(cursor, sil_start - pad_seconds)
        if seg_end > cursor + 0.05:          # skip sub-50 ms slivers
            keep.append((cursor, seg_end))
        cursor = sil_end + pad_seconds

    if total_dur and cursor < total_dur - 0.05:
        keep.append((cursor, total_dur))

    # Nothing meaningful left to cut?
    if not keep:
        return input_path
    if len(keep) == 1 and keep[0][0] < 0.05:
        return input_path  # would keep almost everything → not worth it

    # Build ffmpeg select/aselect filter expressions.
    select_parts = "+".join(
        f"between(t,{s:.3f},{e:.3f})" for s, e in keep
    )
    try:
        proc.run_checked(
            [
                "ffmpeg", "-v", "error", "-i", input_path,
                "-vf", f"select='{select_parts}',setpts=N/FRAME_RATE/TB",
                "-af", f"aselect='{select_parts}',asetpts=N/SR/TB",
                "-y", output_path,
            ],
            what="silence trim",
        )
        saved = sum(e - s for s, e in silences)
        print(
            f"[silence_trim] removed {saved:.1f}s of silence from "
            f"{Path(input_path).name}",
            flush=True,
        )
        return output_path
    except Exception as exc:
        print(
            f"[silence_trim] could not trim ({str(exc)[:80]}) — using original",
            flush=True,
        )
        return input_path
