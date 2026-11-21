"""Whether a file has a picture, and where it stops.

A file's sound and picture can end at different times: a recording whose video
dropped out, a download that stopped, an edit exported with a long outro of
audio only. ffmpeg cutting a clip from past the end of the picture does not
fail. It writes the sound, the mp4 muxer leaves out a video track with no
frames in it, and the result is a "clip" that plays as a black screen. Every
step after it -- the auto-edit, the cold open -- then skips it quietly, so the
person gets their files and only finds out when they open them.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from . import proc


def _seconds(value) -> Optional[float]:
    """A duration as ffprobe gives it: "180.03", or a tag like "00:03:00.03"."""
    if value in (None, "", "N/A"):
        return None
    text = str(value).strip()
    m = re.fullmatch(r"(\d+):(\d{1,2}):(\d{1,2}(?:\.\d+)?)", text)
    try:
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        return float(text)
    except ValueError:
        return None


def picture_seconds(path: str) -> Optional[float]:
    """How long the picture runs. 0.0 with no video at all, None when unknown.

    The stream's own duration, or its DURATION tag in an mkv or webm, which
    put it there instead. Cover art (an attached picture) is not a picture.
    """
    try:
        out = proc.run_checked(
            ["ffprobe", "-v", "error", "-select_streams", "v",
             "-show_entries", "stream=duration:stream_tags=DURATION:"
                              "stream_disposition=attached_pic",
             "-of", "json", path],
            what="ffprobe (picture length)", capture_stdout=True,
        ).stdout
        streams = json.loads(out or "{}").get("streams") or []
    except Exception:  # noqa: BLE001 - not knowing is not a reason to fail
        return None
    streams = [s for s in streams
               if not (s.get("disposition") or {}).get("attached_pic")]
    if not streams:
        return 0.0
    s = streams[0]
    return _seconds(s.get("duration")) or _seconds((s.get("tags") or {}).get("DURATION"))


def has_picture(path: str) -> Optional[bool]:
    """Whether a written clip has a video track. None when it cannot be read."""
    seconds = picture_seconds(path)
    return None if seconds is None and not _readable(path) else seconds != 0.0


def _readable(path: str) -> bool:
    try:
        proc.run_checked(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", path],
                         what="ffprobe (clip)", capture_stdout=True)
        return True
    except Exception:  # noqa: BLE001
        return False
