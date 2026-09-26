"""A second opinion from a model that can see, before anything is cut.

The ranker chooses from a transcript. On a gaming channel that is the wrong
way round: what decides whether a stranger stays is mostly on screen, and a
transcript cannot tell "what the hell was that?" over a car flipping off a
bridge from the same words over a black loading screen. The commercial
clippers that work on gameplay (OpusClip's ClipAnything, Eklipse, Powder)
all judge the picture as well as the words for exactly this reason.

So the best candidates -- about twice as many as will be kept -- are shown
to a vision model as frames from four moments: the first instant, a second
and a half in, the payoff, and the end. It is asked the stranger test in
three parts, each 0-100:

  * first_second -- would someone who has never heard of this creator stop
    scrolling on these opening frames and this opening line?
  * payoff_visible -- can they SEE what the clip is about? Checked against
    the ranker's own "on_screen" claim, so a claim the frames do not back up
    costs the clip.
  * standalone -- does it make sense with no context from the rest of the
    video?

and for a verdict: "keep" or "cut". The answer is blended into the rank and
the pool is re-sorted, so the clips that get rendered are the ones that
passed a look as well as a read. How much seeing counts depends on the kind of
video: a podcast clip is carried by what is said, so there "payoff_visible"
barely moves it.

Best-effort like the rest: no vision provider, or a failed call, and the
ranking stands as the transcript and signals left it.
"""
from __future__ import annotations

import json
import re
from typing import Callable, Dict, List, Optional, Sequence

from . import content_kinds
from .vision import Part, grab_frame

VisionFn = Callable[[List[Part]], str]

FRAME_LONG_SIDE = 512
FRAMES_PER_CLIP = 4
# How much of the final rank the look accounts for. Well under half: the
# ranker read the whole transcript around the moment and the judge sees four
# frames. Tried at 0.45 with a free verdict on a real stream, the judge cut the
# story twist that had reached 1,500 viewers when posted by hand, because four
# stills cannot show a scene landing.
JUDGE_WEIGHT = 0.35
# A clip the judge would cut keeps this much of its blended score. "cut" is
# reserved for a clear disqualifier the frames show (see the prompt), so it is
# allowed to bite -- but not to zero: four frames can still be wrong.
CUT_FACTOR = 0.7

# How the three parts combine, by kind of video. Gameplay and vlogs live or
# die on what is visible; a podcast or tutorial is carried by what is said.
PART_WEIGHTS = {
    content_kinds.STREAM: (0.35, 0.40, 0.25),
    content_kinds.VLOG: (0.40, 0.30, 0.30),
    content_kinds.PODCAST: (0.50, 0.10, 0.40),
    content_kinds.TUTORIAL: (0.40, 0.25, 0.35),
    content_kinds.OTHER: (0.40, 0.30, 0.30),
}

JUDGE_PROMPT = """You are the first test audience for new YouTube Shorts. For each clip below you get four frames -- the first instant, 1.5 seconds in, the payoff, the end -- plus its opening line, everything said in it, and what the editor claims is on screen at the payoff.

You have never heard of this creator. You know nothing about the rest of the video. Your thumb is already moving. Judge each clip the way that viewer would, 0-100 each, using the whole scale:

- "first_second": do the opening frames and opening line make you stop? A black screen, a loading or death screen, a menu, a dim corridor with nothing happening, or a line that is only a swear word all score under 30.
- "payoff_visible": can you SEE what the clip is about? Compare the frames with the editor's claim. If the claim is not in the frames, or the clip is a person reacting to something the frames never show, score under 35. For a clip that is purely talk (a podcast, a story told to camera), score how watchable the speaker is instead.
- "standalone": does it make sense with zero context? Inside jokes, "chat said", callbacks to earlier and unnamed "he"/"that" score under 40.
- "verdict": "cut" ONLY for a clear disqualifier you can see: the opening frames are black, a loading, death or pause screen, a menu, a browser or stream dashboard; nothing happens in any of the four frames; or the clip cannot make sense to a stranger at all. Everything else is "keep", even when it is ordinary -- the scores say how good it is.
- "reason": one short sentence, the main thing that decided it.

Four stills cannot show a story beat landing or a joke's timing, so do not mark a clip down for being a quiet narrative game: judge whether a stranger could follow it and whether there is something to look at. A visible webcam with a strong reaction counts as something to look at. The clips were already picked as the best of a long video; most sit between 45 and 75 -- use the scale to separate them.

Kind of video: {kind}
{clips_block}

Respond with ONLY valid JSON:
{{"clips":[{{"clip":int,"first_second":int,"payoff_visible":int,"standalone":int,"verdict":"keep|cut","reason":"string"}}]}}"""


def _moments(h: Dict) -> List[float]:
    start = float(h["start_time"])
    end = float(h["end_time"])
    peak = h.get("hook_peak")
    try:
        peak = float(peak) if peak is not None else None
    except (TypeError, ValueError):
        peak = None
    if peak is None or not (start + 2.0 < peak < end - 1.0):
        peak = start + (end - start) * 0.6
    return [start + 0.2, min(end, start + 1.5), peak, max(start, end - 1.5)]


def _block(i: int, h: Dict, said: str) -> str:
    said = re.sub(r"\s+", " ", said or "").strip()[:500] or "(nothing said)"
    return (f"--- CLIP {i}: its four frames follow.\n"
            f"Opening line: {h.get('first_line') or h.get('hook_sentence') or '(none)'}\n"
            f"Editor's on-screen claim: {h.get('on_screen') or '(none given)'}\n"
            f"Everything said: {said}")


def _parse(raw: str) -> Dict:
    text = re.sub(r"^```(?:json)?\s*", "", (raw or "").strip())
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        a, b = text.find("{"), text.rfind("}")
        if a != -1 and b != -1:
            return json.loads(text[a:b + 1])
        raise


def _int(v: object) -> Optional[int]:
    try:
        return max(0, min(100, int(float(v))))
    except (TypeError, ValueError):
        return None


def blend(h: Dict, verdict: Dict, kind: str) -> int:
    """The clip's rank once the look is folded in."""
    w_first, w_payoff, w_alone = PART_WEIGHTS.get(kind, PART_WEIGHTS[content_kinds.OTHER])
    looked = (w_first * verdict["first_second"] + w_payoff * verdict["payoff_visible"]
              + w_alone * verdict["standalone"])
    score = (1.0 - JUDGE_WEIGHT) * float(h.get("score", 0) or 0) + JUDGE_WEIGHT * looked
    if verdict["verdict"] == "cut":
        score *= CUT_FACTOR
    return int(round(max(0.0, min(100.0, score))))


def judge(
    highlights: Sequence[Dict],
    source_path: Optional[str],
    vision_fn: Optional[VisionFn],
    said_fn: Callable[[Dict], str],
    kind: str = content_kinds.OTHER,
    images_per_request: int = 20,
) -> int:
    """Look at each candidate and re-score it in place. Returns how many were seen.

    Each judged highlight gets `judge` (the model's answer) and `pre_judge_score`
    (what it was ranked on before), and `score` becomes the blend -- so the
    caller re-sorts on the same field as always.
    """
    if vision_fn is None or not source_path or not highlights:
        return 0
    kind = content_kinds.normalise(kind)
    if kind == content_kinds.AUTO:
        kind = content_kinds.OTHER
    per_request = max(1, images_per_request // FRAMES_PER_CLIP)
    seen = 0

    for lo in range(0, len(highlights), per_request):
        batch = list(highlights[lo:lo + per_request])
        frames = []
        for h in batch:
            shots = [grab_frame(source_path, t, FRAME_LONG_SIDE) for t in _moments(h)]
            frames.append([s for s in shots if s])
        usable = [(h, f) for h, f in zip(batch, frames) if f]
        if not usable:
            continue

        parts: List[Part] = [JUDGE_PROMPT.format(
            kind=content_kinds.LABELS.get(kind, kind),
            clips_block="The clips follow, each introduced by its own header.")]
        for n, (h, shots) in enumerate(usable, 1):
            parts.append(_block(n, h, said_fn(h)))
            parts.extend((jpg, "image/jpeg") for jpg in shots)

        try:
            parsed = _parse(vision_fn(parts))
        except Exception as e:
            print(f"[judge] could not look at candidates {lo + 1}-{lo + len(batch)} "
                  f"({str(e).splitlines()[0][:120]}) - they keep their rank", flush=True)
            continue

        for item in parsed.get("clips") or []:
            if not isinstance(item, dict):
                continue
            try:
                n = int(item.get("clip"))
            except (TypeError, ValueError):
                continue
            if not 1 <= n <= len(usable):
                continue
            parts_ = {k: _int(item.get(k)) for k in ("first_second", "payoff_visible", "standalone")}
            if any(v is None for v in parts_.values()):
                continue
            verdict = {**parts_,
                       "verdict": "cut" if str(item.get("verdict", "")).lower().startswith("cut")
                       else "keep",
                       "reason": re.sub(r"\s+", " ", str(item.get("reason") or "")).strip()[:200]}
            h = usable[n - 1][0]
            h["pre_judge_score"] = int(h.get("score", 0) or 0)
            h["judge"] = verdict
            h["score"] = blend(h, verdict, kind)
            seen += 1

    cut = sum(1 for h in highlights if (h.get("judge") or {}).get("verdict") == "cut")
    if seen:
        print(f"[judge] looked at {seen}/{len(highlights)} candidate(s) - "
              f"{cut} would be swiped past", flush=True)
    return seen


def pool_size(num_clips: int) -> int:
    """How many of the top candidates are worth a look for `num_clips` slots."""
    return min(24, max(num_clips * 2, num_clips + 4))
