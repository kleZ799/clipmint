"""Auto-generate clip thumbnails: sharpest frame + JPEG output.

Finding the sharpest frame uses the Laplacian variance of each sampled frame —
a standard focus-quality metric.  If OpenCV is not installed the function
falls back to a fixed 30%-into-the-clip timestamp, which is still far better
than frame 0 (often black) or the last frame (often a cut or freeze).

All heavy lifting is done with ffmpeg + optional OpenCV; no new dependencies.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .. import proc


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _video_duration(path: str) -> float:
    """Return the duration of *path* in seconds via ffprobe."""
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


def _sharpest_frame_time(clip_path: str, samples: int = 8) -> float:
    """Return the timestamp (seconds) of the sharpest sampled frame.

    Samples the clip at *samples* evenly spaced points (excluding the very
    first and last frames) and returns the one with the highest Laplacian
    variance.  Falls back to 30% into the clip if OpenCV is unavailable.
    """
    duration = _video_duration(clip_path)
    if duration <= 0:
        return 0.5

    best_t = duration * 0.3  # safe fallback
    try:
        import cv2          # type: ignore
        import numpy as np  # type: ignore

        cap = cv2.VideoCapture(clip_path)
        best_lap = -1.0
        for i in range(1, samples + 1):
            t = duration * i / (samples + 1)
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, frame = cap.read()
            if not ok:
                continue
            grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            lap = float(cv2.Laplacian(grey, cv2.CV_64F).var())
            if lap > best_lap:
                best_lap = lap
                best_t = t
        cap.release()
    except Exception:
        pass  # OpenCV not available — use the fallback timestamp

    return best_t


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_thumbnail(
    clip_path: str,
    out_path: str,
    quality: int = 90,
) -> str:
    """Extract the sharpest frame from *clip_path* and save it as a JPEG.

    Args:
        clip_path: Source video file.
        out_path:  Destination JPEG path (created or overwritten).
        quality:   JPEG quality 1-100 (default 90).

    Returns:
        *out_path* on success, or an empty string on failure.
    """
    try:
        t = _sharpest_frame_time(clip_path)
        # Scale to fit inside 1280×720 while preserving aspect ratio.
        scale = "scale='if(gt(iw,ih),1280,-2)':'if(gt(iw,ih),-2,720)'"
        # Map quality 1-100 → ffmpeg -q:v 31-1 (inverted: higher q = worse).
        q_v = max(1, min(31, int(31 - quality * 0.3)))
        proc.run_checked(
            [
                "ffmpeg", "-v", "error",
                "-ss", f"{t:.3f}",
                "-i", clip_path,
                "-frames:v", "1",
                "-vf", scale,
                "-q:v", str(q_v),
                "-y", out_path,
            ],
            what="thumbnail extraction",
        )
        print(
            f"[thumbnail] saved {Path(out_path).name} from t={t:.1f}s",
            flush=True,
        )
        return out_path
    except Exception as exc:
        print(
            f"[thumbnail] could not generate ({str(exc)[:80]})",
            flush=True,
        )
        return ""
