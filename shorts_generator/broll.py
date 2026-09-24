"""Stock B-roll over the lines that name something you can film.

A talking head saying "I moved to Tokyo" is a better Short with two seconds
of Tokyo over it. The picking is done by the same model that ranks the
moments, because telling "a city at night" (filmable) from "a bad week"
(not) is a reading job; the footage comes from Pexels, whose videos are free
to use without credit, through its API and the user's own free key.

Everything here is optional and fails quiet. No key, no network, a model
that says nothing is concrete enough -- each of those is a clip without
B-roll, never a clip that failed. It is never offered on a stream either:
there the gameplay is the picture, and covering it with stock footage would
cover the thing people came to watch.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import user_config

KEY_NAME = "PEXELS_API_KEY"
SEARCH_URL = "https://api.pexels.com/videos/search"

MAX_PER_CLIP = 2
MIN_SECONDS, MAX_SECONDS = 1.5, 3.5
# Never over the opening -- that is the hook, and it has to be the speaker --
# or the last beat, which is the payoff.
KEEP_CLEAR_START, KEEP_CLEAR_END = 3.0, 2.0
# Big enough to look sharp at 1080 wide, small enough to fetch in seconds.
MAX_DOWNLOAD_BYTES = 80 * 1024 * 1024

_PROMPT = """You choose stock-footage cutaways for short vertical videos.

For each clip below you get what is said in it, with the second each line
starts at. Pick at most {max_per} moments per clip where 2-3 seconds of stock
video would show the concrete thing being talked about: a place, an object,
an animal, an activity. "money", "airplane taking off", "coffee", "city at
night", "running on a beach".

Rules:
- Only concrete, filmable things. No feelings, no abstract ideas, no names of
  people, brands, films or video games.
- Never in the first {clear_start:.0f} seconds or the last {clear_end:.0f} seconds of a clip.
- A clip where nothing concrete is named gets an empty list. That is a good
  answer, and much better than a stretch.
- "query" is 1 to 3 plain English words that would find that footage on a
  stock video site, even if the clip is in another language.

Respond with ONLY a JSON object, no markdown:
{{"clips":[{{"clip":1,"moments":[{{"at":12.4,"seconds":2.5,"query":"city at night"}}]}}]}}

{clips}"""


def api_key() -> str:
    return user_config.get(KEY_NAME)


def available() -> bool:
    return bool(api_key())


def _lines(words: List[Dict], per_line: int = 10) -> List[str]:
    """What is said, as timed lines of about ten words."""
    out, cur, at = [], [], None
    for w in words:
        if at is None:
            at = float(w["start"])
        cur.append(str(w.get("word", "")).strip())
        if len(cur) >= per_line or re.search(r"[.!?]$", cur[-1]):
            out.append(f"[{at:.1f}] {' '.join(cur)}")
            cur, at = [], None
    if cur:
        out.append(f"[{at:.1f}] {' '.join(cur)}")
    return out


def _parse(raw: str) -> Dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        return json.loads(text[start:end + 1])
    except ValueError:
        return {}


def pick_moments(clips: List[Dict], llm_fn: Callable[[str], str]) -> Dict[int, List[Dict]]:
    """Ask the model where B-roll fits. {clip index: [{at, seconds, query}]}.

    `clips` is [{"words": [...], "duration": float}] in the timeline the
    clip will be rendered in. One request for the whole batch.
    """
    blocks, index = [], []
    for i, c in enumerate(clips):
        dur = float(c.get("duration") or 0)
        if dur < KEEP_CLEAR_START + KEEP_CLEAR_END + MIN_SECONDS or not c.get("words"):
            continue
        index.append(i)
        blocks.append(f"Clip {len(index)} ({dur:.1f}s):\n" + "\n".join(_lines(c["words"])))
    if not blocks:
        return {}

    prompt = _PROMPT.format(max_per=MAX_PER_CLIP, clear_start=KEEP_CLEAR_START,
                            clear_end=KEEP_CLEAR_END, clips="\n\n".join(blocks))
    try:
        data = _parse(llm_fn(prompt))
    except Exception as e:  # noqa: BLE001 - B-roll is never worth a failed render
        print(f"[broll] could not ask where it fits ({str(e).splitlines()[0][:140]})",
              flush=True)
        return {}

    picked: Dict[int, List[Dict]] = {}
    for item in data.get("clips") or []:
        if not isinstance(item, dict):
            continue
        try:
            n = int(item.get("clip")) - 1
        except (TypeError, ValueError):
            continue
        if not 0 <= n < len(index):
            continue
        i = index[n]
        dur = float(clips[i].get("duration") or 0)
        chosen: List[Dict] = []
        for m in item.get("moments") or []:
            if not isinstance(m, dict):
                continue
            try:
                at = float(m.get("at"))
                secs = float(m.get("seconds") or 2.5)
            except (TypeError, ValueError):
                continue
            query = re.sub(r"[^\w\s'-]", " ", str(m.get("query") or "")).strip()
            query = " ".join(query.split()[:3])
            if not query:
                continue
            secs = max(MIN_SECONDS, min(MAX_SECONDS, secs))
            if at < KEEP_CLEAR_START or at + secs > dur - KEEP_CLEAR_END:
                continue
            if any(abs(at - c["at"]) < c["seconds"] + 1.0 for c in chosen):
                continue
            chosen.append({"at": round(at, 2), "seconds": round(secs, 2), "query": query})
            if len(chosen) >= MAX_PER_CLIP:
                break
        if chosen:
            picked[i] = chosen
    return picked


def _cache_dir() -> Path:
    d = user_config.source_dir() / "b-roll"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _orientation(width: int, height: int) -> str:
    if height > width * 1.1:
        return "portrait"
    if width > height * 1.1:
        return "landscape"
    return "square"


def _best_file(video: Dict, want_h: int) -> Optional[Dict]:
    files = [f for f in video.get("video_files") or []
             if str(f.get("file_type", "")).endswith("mp4") and f.get("link")
             and int(f.get("width") or 0) >= 540]
    if not files:
        return None
    # The smallest file that still fills the frame; failing that, the largest.
    big_enough = [f for f in files if int(f.get("height") or 0) >= want_h * 0.9]
    if big_enough:
        return min(big_enough, key=lambda f: int(f.get("height") or 0))
    return max(files, key=lambda f: int(f.get("height") or 0))


def fetch(query: str, seconds: float, width: int, height: int,
          avoid: Optional[set] = None) -> Optional[str]:
    """A local file of stock footage for `query`, downloaded once and cached."""
    key = api_key()
    if not key:
        return None
    try:
        import requests
    except ImportError:
        return None

    avoid = avoid or set()
    try:
        r = requests.get(SEARCH_URL, headers={"Authorization": key}, timeout=20, params={
            "query": query, "orientation": _orientation(width, height),
            "size": "medium", "per_page": 10,
        })
        if r.status_code in (401, 403):
            print("[broll] Pexels did not accept the key - check it in Settings", flush=True)
            return None
        r.raise_for_status()
        videos = r.json().get("videos") or []
    except Exception as e:  # noqa: BLE001
        print(f"[broll] could not search Pexels for {query!r} ({str(e)[:120]})", flush=True)
        return None

    for video in videos:
        vid = str(video.get("id") or "")
        if not vid or vid in avoid or float(video.get("duration") or 0) < seconds:
            continue
        f = _best_file(video, height)
        if f is None:
            continue
        slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")[:40] or "broll"
        dest = _cache_dir() / f"{slug}-{vid}.mp4"
        if dest.exists() and dest.stat().st_size > 0:
            avoid.add(vid)
            return str(dest)
        tmp = dest.with_suffix(".part")
        try:
            with requests.get(f["link"], stream=True, timeout=60) as dl:
                dl.raise_for_status()
                got = 0
                with tmp.open("wb") as out:
                    for block in dl.iter_content(1 << 16):
                        got += len(block)
                        if got > MAX_DOWNLOAD_BYTES:
                            raise ValueError("the file is larger than B-roll needs")
                        out.write(block)
            tmp.replace(dest)
        except Exception as e:  # noqa: BLE001
            print(f"[broll] could not download footage for {query!r} ({str(e)[:120]})",
                  flush=True)
            try:
                tmp.unlink()
            except OSError:
                pass
            continue
        avoid.add(vid)
        return str(dest)

    print(f"[broll] no footage long enough for {query!r}", flush=True)
    return None
