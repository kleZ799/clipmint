"""What is actually on screen in each clip.

Everything else the pipeline knows about a clip is words: the transcript, the
source video's title, its tags. That is enough when the video is about one
thing. It falls apart on a variety stream. A stream titled "The Finals chill
stream" that wanders into Firewatch and then a horror game hands every clip
the same subject, and the titles end up confidently naming a game that is not
on screen. Nobody says "I am now playing Firewatch" out loud, so the transcript
cannot fix it either.

So before any title is written, a few frames from each clip are shown to a
vision-capable model, which says what kind of video it is (gameplay, podcast,
a face-cam story, a tutorial...) and what is in it. That answer is attached to
the clip as `scene`, and the metadata writer treats it as the ground truth for
what the clip is about.

One thing the model is bad at, measured on real clips: naming a game from its
look alone. It called a Fear to Fathom clip "Phasmophobia" at full confidence,
because dark houses look alike. So a name only counts as confirmed when there
is evidence for it -- text on screen, a word said in the clip, or the source's
own listing. A name recognised from visuals alone is kept as a guess, and the
title then says "this horror game" instead of printing a wrong name in front
of the one audience that would notice.

Best-effort, like the rest of the packaging: a clip the model cannot see just
has no `scene`, and its metadata is written from its words as before.
"""
from __future__ import annotations

import json
import re
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union

from . import proc

# A frame is either prompt text or an image as (bytes, mime type).
Part = Union[str, Tuple[bytes, str]]
VisionFn = Callable[[List[Part]], str]

# Four frames spread across the clip: enough to see the game change mid-clip
# or a HUD that only shows up now and then, few enough that a whole run fits
# in one or two requests.
FRAMES_PER_CLIP = 4
# The long side of each frame sent. HUD text is still legible at this size,
# and keeping it 640px keeps multi-clip vision requests fast and reliable.
FRAME_LONG_SIDE = 640

# What kinds of video there are, as the metadata writer needs to tell them
# apart. A podcast clip filed under "gameplay" reaches nobody.
CONTENT_TYPES = (
    "gameplay", "podcast", "talking_head", "just_chatting", "irl", "storytelling",
    "reaction", "tutorial", "commentary", "music", "sports", "comedy_skit",
    "vlog", "interview", "news", "other",
)

# Evidence a name can rest on. Anything else is a guess.
CONFIRMING = ("on_screen_text", "speech", "source_listing", "unmistakable")

VISION_PROMPT = """You are looking at frames from {n} short clips cut from one long video. For EACH clip, describe what is actually on screen, so the right title and tags can be written for it.

SOURCE VIDEO (its own listing — may describe only part of the video)
{video_context}

NAMES MENTIONED ANYWHERE IN THE FULL VIDEO (candidates, not facts — a variety stream covers several):
{candidates}

Do NOT assume this is a gaming video. It could be a podcast, a face-cam story, a reaction, a tutorial, IRL footage, a sports clip, anything. Judge each clip on its own frames.

For each clip return:
- "content_type": one of {types}
- "layout": what the frame is made of, e.g. "gameplay with small webcam overlay", "full-frame face cam", "two people at podcast mics", "screen recording"
- "subject": the proper name of the game / show / person / product / place the clip is about, ONLY when you have evidence for it (see below). Otherwise ""
- "named_by": what that evidence is — "on_screen_text" (a title, HUD, menu, caption or watermark names it, OR subtitles/UI show names of characters, places or items that belong to that one title and no other — "Henry:" and "Delilah:" on radio subtitles are Firewatch), "speech" (the clip's own words name it, or name characters or places unique to it), "source_listing" (the listing above names it AND nothing in the frames contradicts it), "unmistakable" (a famous title whose look nobody could confuse: Minecraft, Fortnite, GTA V, Roblox, Among Us — and nothing that merely shares a genre's look), or "none"
- "subject_guess": your best guess at the name when you have no evidence, else ""
- "genre": the plain category a stranger would search, e.g. "horror game", "hero shooter", "narrative adventure", "comedy podcast", "cooking tutorial"
- "on_screen_text": the readable text in the frames that matters (subtitles, HUD, captions), under 200 characters
- "scene": one or two plain sentences: what is literally happening on screen, and what the person on camera is doing or feeling
- "people": who is visible, described not named unless named on screen (e.g. "streamer on webcam", "two hosts")
- "mood": one or two words (tense, funny, wholesome, chaotic, sad...)

THE EVIDENCE RULE IS THE WHOLE POINT. Many games look alike — dark houses, forests, shooters with similar HUDs. A wrong game name in a title is worse than no name: it is the one thing the exact audience that would watch will call out. If you recognise a game only from how it looks, put it in "subject_guess" and leave "subject" empty. Prefer a candidate from the list above only when something in the frames or the clip's words actually matches it.

{clips_block}

Respond with ONLY valid JSON:
{{"clips":[{{"clip":int,"content_type":"string","layout":"string","subject":"string","named_by":"string","subject_guess":"string","genre":"string","on_screen_text":"string","scene":"string","people":"string","mood":"string"}}]}}"""


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


def grab_frame(path: str, t: float, long_side: int = FRAME_LONG_SIDE) -> Optional[bytes]:
    """One JPEG frame at `t` seconds, scaled so its long side is `long_side`."""
    scale = (f"scale='if(gt(iw,ih),{long_side},-2)':'if(gt(iw,ih),-2,{long_side})'")
    try:
        out = proc.run(
            ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", path,
             "-frames:v", "1", "-vf", scale, "-q:v", "4",
             "-f", "image2pipe", "-c:v", "mjpeg", "-"],
            capture_output=True,
            timeout=25,
        ).stdout
    except proc.Cancelled:
        raise
    except Exception:
        return None
    return out or None


def sample_frames(path: str, start: float, end: float,
                  count: int = FRAMES_PER_CLIP) -> List[bytes]:
    """`count` frames spread evenly across [start, end], skipping the edges.

    The very first and last frames are the ones most likely to be a cut or a
    transition, so the samples sit inside the span rather than on its ends.
    """
    if end <= start:
        end = start + (_duration(path) or 30.0)
    step = (end - start) / (count + 1)
    frames = []
    for i in range(1, count + 1):
        jpg = grab_frame(path, start + step * i)
        if jpg:
            frames.append(jpg)
    return frames


def _parse_json_loose(raw: str) -> Dict:
    text = re.sub(r"^```(?:json)?\s*", "", (raw or "").strip())
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        a, b = text.find("{"), text.rfind("}")
        if a != -1 and b != -1:
            return json.loads(text[a:b + 1])
        raise


def _clean(s: object, limit: int) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()[:limit]


def coerce_scene(item: Dict) -> Dict:
    """Force one model answer into shape, and apply the evidence rule.

    The prompt asks for the rule; this enforces it. A name whose stated
    evidence is not one of the confirming kinds is demoted to a guess however
    confident the model sounded, because the failure it prevents -- a wrong
    game in the title -- is the one this whole module exists to stop.
    """
    ctype = _clean(item.get("content_type"), 30).lower().replace(" ", "_")
    if ctype not in CONTENT_TYPES:
        ctype = "other"
    named_by = _clean(item.get("named_by"), 30).lower()
    subject = _clean(item.get("subject"), 80)
    guess = _clean(item.get("subject_guess"), 80)
    if subject and named_by not in CONFIRMING:
        guess, subject = guess or subject, ""
    if not subject:
        named_by = "none"
    return {
        "content_type": ctype,
        "layout": _clean(item.get("layout"), 80),
        "subject": subject,
        "named_by": named_by,
        "subject_guess": "" if guess.lower() == subject.lower() else guess,
        "genre": _clean(item.get("genre"), 60).lower(),
        "on_screen_text": _clean(item.get("on_screen_text"), 240),
        "scene": _clean(item.get("scene"), 400),
        "people": _clean(item.get("people"), 120),
        "mood": _clean(item.get("mood"), 40).lower(),
    }


def _clip_block(i: int, said: str) -> str:
    said = _clean(said, 700) or "(no speech in this clip)"
    return f"--- CLIP {i}: its frames follow, in order. What is said in it: {said}"


def describe_clips(
    clips: Sequence[Dict],
    vision_fn: Optional[VisionFn],
    video_context: str = "",
    candidates: Sequence[str] = (),
    images_per_request: int = 20,
) -> List[Optional[Dict]]:
    """A `scene` for each clip, in order, or None where it could not be seen.

    Each item in `clips` needs a `path` to read frames from and a `start` and
    `end` inside it; `said` (the clip's words) is optional but helps the
    evidence rule, since a game named out loud counts as confirmed.
    """
    out: List[Optional[Dict]] = [None] * len(clips)
    if vision_fn is None or not clips:
        return out

    per_clip = max(1, min(FRAMES_PER_CLIP, images_per_request))
    batch = max(1, images_per_request // per_clip)
    cand = ", ".join(dict.fromkeys(c for c in candidates if c)) or "(none)"

    for lo in range(0, len(clips), batch):
        chunk = list(range(lo, min(len(clips), lo + batch)))
        frames = {i: sample_frames(clips[i]["path"], float(clips[i].get("start") or 0),
                                   float(clips[i].get("end") or 0), per_clip)
                  for i in chunk}
        chunk = [i for i in chunk if frames[i]]
        if not chunk:
            continue

        parts: List[Part] = [VISION_PROMPT.format(
            n=len(chunk), video_context=video_context or "(no listing)",
            candidates=cand, types=", ".join(CONTENT_TYPES),
            clips_block="The clips follow, each introduced by its own header.",
        )]
        for n, i in enumerate(chunk, 1):
            parts.append(_clip_block(n, str(clips[i].get("said") or "")))
            parts.extend((jpg, "image/jpeg") for jpg in frames[i])

        print(f"[vision] looking at clips {chunk[0] + 1}-{chunk[-1] + 1} of "
              f"{len(clips)}...", flush=True)
        try:
            parsed = _parse_json_loose(vision_fn(parts))
        except Exception as e:
            if isinstance(e, proc.Cancelled):
                raise
            print(f"[vision] could not look at clips {chunk[0] + 1}-{chunk[-1] + 1} "
                  f"({str(e).splitlines()[0][:120]}) - their titles come from "
                  f"their words alone", flush=True)
            continue

        for item in parsed.get("clips") or []:
            if not isinstance(item, dict):
                continue
            try:
                n = int(item.get("clip"))
            except (TypeError, ValueError):
                continue
            if 1 <= n <= len(chunk):
                out[chunk[n - 1]] = coerce_scene(item)

    for i, scene in enumerate(out):
        if scene:
            name = scene["subject"] or (f"maybe {scene['subject_guess']}"
                                        if scene["subject_guess"] else "unnamed")
            print(f"[vision] clip {i + 1}: {scene['content_type']} - {name}"
                  f"{' (' + scene['genre'] + ')' if scene['genre'] else ''}", flush=True)
    return out
