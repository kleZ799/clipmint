"""How the Shorts that went up on YouTube actually did.

The ranking guesses what will travel; this reads back what did. For every clip
that is on the connected channel it fetches the views, likes and comments, and
keeps them on the clip, where shorts_generator/performance.py can learn from
them and the library can show them.

Finding a clip's video. A clip uploaded from ClipMint carries its video's id.
A clip someone uploaded by hand does not, so the channel's list of uploads is
read and matched by title: the clip's file is named after its title, and the
title box is what went into YouTube's title box. Only videos published after
the run that made the clip are considered, so an older video that happens to
share a title is never mistaken for it. The list of uploads is read to match
and then dropped; nothing about a video that matched no clip is kept.

YouTube's rules. Everything here comes through the Data API with the
`youtube.readonly` permission the connection already has -- no new permission.
YouTube's developer policies (III.E.4) cap how long anything it hands back may
be kept without being refreshed: 30 days. So each clip's numbers carry the
moment they were fetched, a check refreshes them, and anything older than 30
days is dropped at startup, the same sweep that clears old upload records.
Disconnecting drops them all.

Cost. A check is one request for the channel, one per 50 uploads for the list,
and one per 50 matched videos for their numbers: a few units of the 10,000 a
day YouTube allows, however many clips there are.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

from shorts_generator import user_config

from . import youtube_upload as yt

# At most this many of the channel's uploads are read to match titles against.
# Twenty pages of fifty: more than enough to reach back past any clip still on
# disk, and a hard stop on a channel with thousands of videos.
MAX_UPLOAD_PAGES = 20
# A check starts on its own at launch when the last one is older than this.
AUTO_EVERY = timedelta(hours=6)
# A video counts as a clip's upload only if it went up after the run started,
# less this much slack for clocks and time zones.
MATCH_SLACK = timedelta(days=1)

_lock = threading.Lock()
_state: Dict = {"state": "idle", "error": "", "matched": 0, "checked_at": "",
                "videos": 0}


def _state_path():
    return user_config.config_dir() / "youtube_stats.json"


def _load_state() -> Dict:
    """When the last check ran and what it found -- counts, no YouTube data."""
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(values: Dict) -> None:
    try:
        _state_path().write_text(json.dumps(values), encoding="utf-8")
    except OSError:
        pass


def status() -> Dict:
    with _lock:
        out = dict(_state)
    if not out.get("checked_at"):
        out.update({k: v for k, v in _load_state().items() if k in ("checked_at", "matched")})
    return out


def norm_title(t: str) -> str:
    """A title reduced to its words, for matching a file name to a video."""
    t = re.sub(r"#\w+", " ", str(t or "").lower())
    return re.sub(r"[^\w]+", " ", t).strip()


def _get(path: str, params: Dict, token: str) -> Dict:
    r = requests.get(f"{yt.API_URL}/{path}", params=params, headers=yt._headers(token),
                     timeout=yt.HTTP_TIMEOUT)
    if not r.ok:
        raise RuntimeError(yt._explain(r))
    return r.json()


def _channel_uploads(token: str) -> List[Dict]:
    """The connected channel's videos, newest first: id, title and when."""
    ch = _get("channels", {"part": "contentDetails", "mine": "true"}, token)
    items = ch.get("items") or []
    if not items:
        return []
    playlist = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    videos, page = [], None
    for _ in range(MAX_UPLOAD_PAGES):
        params = {"part": "snippet,contentDetails", "playlistId": playlist, "maxResults": 50}
        if page:
            params["pageToken"] = page
        data = _get("playlistItems", params, token)
        for it in data.get("items") or []:
            cd, sn = it.get("contentDetails") or {}, it.get("snippet") or {}
            videos.append({"id": cd.get("videoId"), "title": sn.get("title", ""),
                           "published": cd.get("videoPublishedAt") or sn.get("publishedAt", "")})
        page = data.get("nextPageToken")
        if not page:
            break
    return [v for v in videos if v["id"]]


def _when(raw) -> Optional[datetime]:
    try:
        d = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _clip_titles(clip: Dict) -> List[str]:
    import os
    seo = clip.get("seo") or {}
    names = [seo.get("title"), clip.get("title")]
    if clip.get("file"):
        names.append(os.path.splitext(clip["file"])[0])
    return [n for n in (norm_title(x) for x in names if x) if n]


def match(jobs, videos: List[Dict]) -> Dict[tuple, Dict]:
    """(job id, clip file) -> {video_id, matched} for every clip on the channel."""
    by_title: Dict[str, List[Dict]] = {}
    for v in videos:
        by_title.setdefault(norm_title(v["title"]), []).append(v)
    taken = set()
    out: Dict[tuple, Dict] = {}
    # Uploads made from ClipMint first: they are certain, and a title match must
    # never hand their video to some other clip.
    for job in jobs:
        for c in job.clips:
            vid = (c.get("youtube") or {}).get("video_id")
            if vid and c.get("file"):
                out[(job.id, c["file"])] = {"video_id": vid, "matched": "upload"}
                taken.add(vid)
    for job in jobs:
        made = datetime.fromtimestamp(job.created_at, timezone.utc) - MATCH_SLACK
        for c in job.clips:
            key = (job.id, c.get("file"))
            if not c.get("file") or key in out:
                continue
            for t in _clip_titles(c):
                hits = [v for v in by_title.get(t, [])
                        if v["id"] not in taken and (_when(v["published"]) or made) >= made]
                if hits:
                    # The earliest upload after the run: a re-upload of the same
                    # clip later is the copy people saw least.
                    v = min(hits, key=lambda v: v["published"])
                    out[key] = {"video_id": v["id"], "matched": "title"}
                    taken.add(v["id"])
                    break
    return out


def _stats(token: str, ids: List[str]) -> Dict[str, Dict]:
    out: Dict[str, Dict] = {}
    for i in range(0, len(ids), 50):
        data = _get("videos", {"part": "statistics,snippet", "id": ",".join(ids[i:i + 50])}, token)
        for it in data.get("items") or []:
            s = it.get("statistics") or {}
            out[it["id"]] = {
                "views": int(s.get("viewCount", 0) or 0),
                "likes": int(s.get("likeCount", 0) or 0),
                "comments": int(s.get("commentCount", 0) or 0),
                "published_at": (it.get("snippet") or {}).get("publishedAt", ""),
            }
    return out


def check(store) -> Dict:
    """Read every clip's numbers from the channel, now. Returns the state."""
    with _lock:
        if _state["state"] == "running":
            return dict(_state)
        _state.update(state="running", error="")
    try:
        token = yt._access_token()
        videos = _channel_uploads(token)
        jobs = store.jobs()
        found = match(jobs, videos)
        numbers = _stats(token, sorted({m["video_id"] for m in found.values()}))
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        matched = 0
        for job in jobs:
            for c in list(job.clips):
                m = found.get((job.id, c.get("file")))
                got = numbers.get(m["video_id"]) if m else None
                if got:
                    matched += 1
                    perf = {**got, "video_id": m["video_id"], "matched": m["matched"],
                            "url": f"https://youtube.com/shorts/{m['video_id']}",
                            "fetched_at": now}
                    store.replace_clip(job, c["file"], {"performance": perf})
                elif c.get("performance"):
                    # Deleted or made private since: what was known is no
                    # longer true, so it goes rather than lingering.
                    store.replace_clip(job, c["file"], {"performance": None})
        with _lock:
            _state.update(state="done", matched=matched, checked_at=now, videos=len(videos))
        _save_state({"checked_at": now, "matched": matched})
        print(f"[youtube] views read for {matched} clip(s) on the channel", flush=True)
    except yt.NotConnected as e:
        with _lock:
            _state.update(state="error", error=str(e))
    except Exception as e:  # noqa: BLE001 - a failed check must not take anything down
        with _lock:
            _state.update(state="error", error=f"Couldn't read the channel's numbers: {e}")
    return status()


def check_in_background(store) -> Dict:
    threading.Thread(target=check, args=(store,), name="youtube-stats", daemon=True).start()
    time.sleep(0.05)
    return status()


def maybe_check_at_launch(store) -> None:
    """A check at startup, when connected and the last one is stale."""
    if not yt.status().get("connected"):
        return
    last = _when(_load_state().get("checked_at") or "")
    if last and datetime.now(timezone.utc) - last < AUTO_EVERY:
        return
    check_in_background(store)


def expired(perf: Optional[Dict]) -> bool:
    """Numbers older than YouTube lets them be kept without a refresh."""
    if not perf:
        return False
    when = _when(perf.get("fetched_at") or "")
    return when is None or datetime.now(timezone.utc) - when > timedelta(days=yt.KEEP_DAYS)


def forget() -> None:
    """On disconnect: the last-check note goes with the numbers."""
    try:
        _state_path().unlink()
    except OSError:
        pass
    with _lock:
        _state.update(state="idle", error="", matched=0, checked_at="", videos=0)
