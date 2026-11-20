"""Where a clip actually starts and stops.

The model proposes a span. It is good at finding the moment and bad at the two
things that decide whether the moment survives contact with a feed:

**Length.** Ask for 30-second clips and a model will hand back 19s, 24s, 47s —
close enough to look obedient in a list, and nowhere near what was asked for.
It is not being careless; it is estimating durations from timestamps it half
remembers while also writing JSON. Length is arithmetic, so it is done here,
where it can simply be true.

**The opening.** A Short is auditioned in its first second, so the clip has to
open ON the hook rather than on the run-up to it. The model is told this in the
prompt and it names the line it means (`first_line`) — but the timestamp it
pairs with that line routinely lands seconds early, on the throat-clear before
it. The line is the reliable part of that answer, so the line is what the start
time is derived from.

Everything is snapped to transcript segment boundaries, which is where the
speaker actually paused. That is the difference between a clip that opens on a
word and one that opens halfway through a syllable — and it means enforcing a
length never cuts mid-sentence, which is the one thing worse than the wrong
length.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

from .signals import (
    MIN_OPENING_DENSITY,
    OPENING_WINDOW,
    AudioTrack,
    dialogue_density,
)

# How much of the source may be shown before the hook lands. The rule book
# allows about a second, and only to let a sound land; past that the viewer is
# watching a run-up and deciding to leave.
MAX_RUNWAY = 1.0
DEFAULT_RUNWAY = 0.35

# When no length was asked for. Not one number: the rule book is explicit that
# a reaction beat and a narrative payoff have different natural lengths, and a
# single global target is listed as an anti-pattern. This is the outer band —
# `TARGET_BY_KIND` narrows it per content type.
DEFAULT_MIN = 12.0
DEFAULT_MAX = 42.0

# Content type → the band that type's clips should land in.
TARGET_BY_KIND = {
    "reaction": (14.0, 26.0),
    "narrative": (24.0, 40.0),
    "explainer": (30.0, 55.0),
}

# How far past the requested maximum a clip may run to avoid ending mid-
# sentence. Landing a beat late is a smaller sin than a clip that stops on a
# half-finished word — but it is still a sin, so the allowance is small.
OVERRUN_TOLERANCE = 2.5

# How far a boundary may move to find a quiet moment to cut on.
SNAP_WINDOW = 0.4

# When no sentence ends where the clip has to, how far back from that point to
# look for a pause to end in. A cut on the exact second lands inside a word.
FORCED_CUT_LOOKBACK = 3.0


def _norm(text: str) -> str:
    """Lowercase, stripped of punctuation — for comparing spoken lines."""
    return re.sub(r"[^a-z0-9 ]+", " ", str(text or "").lower()).strip()


def _tokens(text: str) -> List[str]:
    return [t for t in _norm(text).split() if t]


def _segments(transcript: Optional[Dict]) -> List[Dict]:
    if not transcript:
        return []
    out = []
    for seg in transcript.get("segments", []):
        try:
            s, e = float(seg["start"]), float(seg["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if e <= s:
            continue
        out.append({"start": s, "end": e, "text": str(seg.get("text", "")).strip()})
    out.sort(key=lambda x: x["start"])
    return out


def find_hook_segment(segments: Sequence[Dict], first_line: str,
                      near: float, window: float = 45.0) -> Optional[int]:
    """Index of the segment the clip's opening line was actually spoken in.

    Matched on words rather than on the timestamp, because the line is the
    part of the model's answer that is reliable. Scored by how much of the
    line a segment carries, with distance from the proposed start as the
    tie-breaker, so a stock phrase said twice in a stream resolves to the one
    the model meant.
    """
    wanted = _tokens(first_line)
    if not wanted or not segments:
        return None

    # Only a stock phrase is this short; matching on it would be a coin flip.
    if len(wanted) < 3:
        return None

    best, best_score = None, 0.0
    for i, seg in enumerate(segments):
        if abs(seg["start"] - near) > window:
            continue
        have = set(_tokens(seg["text"]))
        if not have:
            continue
        overlap = sum(1 for w in wanted if w in have) / len(wanted)
        if overlap < 0.6:
            continue
        # Closeness only breaks ties — a strong word match twenty seconds away
        # still beats a weak one right next door.
        closeness = 1.0 - min(1.0, abs(seg["start"] - near) / window)
        score = overlap + 0.15 * closeness
        if score > best_score:
            best, best_score = i, score
    return best


def _segment_at(segments: Sequence[Dict], t: float) -> Optional[int]:
    """Index of the segment covering `t`, or the next one starting after it."""
    for i, seg in enumerate(segments):
        if seg["end"] >= t:
            return i
    return None


def _snap_start(audio: Optional[AudioTrack], t: float, word_at: float,
                not_before: float) -> float:
    """A start nudged to a quiet instant, but only ever earlier than `word_at`.

    Moving a start later than the first word's onset clips that word, and the
    two-way nudge did exactly that whenever the quietest frame fell a beat in.
    """
    if not audio:
        return t
    a = max(0.0, not_before, t - SNAP_WINDOW)
    if word_at <= a:
        return min(t, word_at)
    quiet = audio.quietest_time(a, word_at)
    return min(quiet, word_at) if quiet is not None else t


def _snap_end(audio: Optional[AudioTrack], segments: Sequence[Dict], t: float) -> float:
    """An end nudged to a quiet instant, but only ever later than `t`, and never
    into whoever speaks next.

    `t` is where the last word ends. The old two-way nudge could pull the cut
    back inside that word, or push it into the next speaker's first syllable
    when two people talk back to back -- "it cut one of the guys off mid word".
    """
    nxt = next((seg["start"] for seg in segments if seg["start"] > t + 0.01), None)
    hi = t + SNAP_WINDOW if nxt is None else min(t + SNAP_WINDOW, nxt - 0.05)
    if not audio or hi <= t:
        return t
    quiet = audio.quietest_time(t, hi)
    return min(max(quiet, t), hi) if quiet is not None else t


def _forced_cut(audio: Optional[AudioTrack], at: float, floor: float) -> float:
    """Where to end when no sentence ends near `at`: the quietest moment in the
    few seconds before it, so the clip at least stops between words."""
    if not audio:
        return at
    quiet = audio.quietest_time(max(floor, at - FORCED_CUT_LOOKBACK), at)
    return quiet if quiet is not None and quiet > floor else at


def _band(clip_seconds: Optional[Sequence[float]], content_type: str) -> Tuple[float, float]:
    """The length window this clip has to land in.

    An explicit request wins outright — someone who types "30 second clips"
    has told us the answer, and a house heuristic that overrides it is a bug
    with an opinion.
    """
    if clip_seconds and len(clip_seconds) == 2:
        lo, hi = float(clip_seconds[0]), float(clip_seconds[1])
        if hi > lo > 0:
            return lo, hi
    kind = _kind_of(content_type)
    return TARGET_BY_KIND.get(kind, (DEFAULT_MIN, DEFAULT_MAX))


def _kind_of(content_type: str) -> str:
    ct = (content_type or "").lower()
    if ct in ("tutorial", "lecture", "explainer"):
        return "explainer"
    if ct in ("commentary", "vlog", "debate", "interview", "podcast"):
        return "reaction"
    return "narrative"


def _opening_ok(transcript: Optional[Dict], start: float, end: float) -> bool:
    """Is there enough speech in the first two seconds to hold anyone?"""
    if not transcript:
        return True
    return dialogue_density(transcript, start, min(end, start + OPENING_WINDOW)) \
        >= MIN_OPENING_DENSITY


def _choose_start(segments: Sequence[Dict], highlight: Dict,
                  audio: Optional[AudioTrack]) -> Tuple[float, Optional[int], List[str]]:
    """Where the clip opens, and which segment that is.

    Returns the start time, the index of the segment it opens on, and any
    notes worth putting in the log about how it got there.
    """
    proposed = float(highlight.get("start_time", 0) or 0)
    notes: List[str] = []

    hook = find_hook_segment(segments, highlight.get("first_line")
                             or highlight.get("hook_sentence") or "", proposed)
    if hook is not None:
        idx = hook
        if abs(segments[idx]["start"] - proposed) > 1.0:
            notes.append(f"opened on the hook line at {segments[idx]['start']:.1f}s "
                         f"instead of {proposed:.1f}s")
    else:
        found = _segment_at(segments, proposed)
        if found is None:
            return proposed, None, notes
        idx = found

    # Skip forward over anything that opens on dead air. A clip whose first two
    # seconds are silence is thrown away by the feed before its hook arrives,
    # so it is better to open a beat later and be heard.
    limit = min(len(segments), idx + 4)
    while idx < limit and not _opening_ok({"segments": segments},
                                          segments[idx]["start"],
                                          segments[idx]["start"] + 30):
        idx += 1
        notes.append("skipped a silent opening")
    idx = min(idx, len(segments) - 1)

    seg = segments[idx]
    # A little runway, but never into the previous speaker's tail.
    runway = DEFAULT_RUNWAY
    if idx > 0:
        runway = min(runway, max(0.0, seg["start"] - segments[idx - 1]["end"]))
    start = max(0.0, seg["start"] - min(runway, MAX_RUNWAY))
    prev_end = segments[idx - 1]["end"] if idx > 0 else 0.0
    return _snap_start(audio, start, seg["start"], prev_end), idx, notes


def _choose_end(segments: Sequence[Dict], start_idx: Optional[int], start: float,
                proposed_end: float, lo: float, hi: float,
                audio: Optional[AudioTrack]) -> Tuple[float, List[str]]:
    """Where the clip stops, inside the requested length band.

    Walks whole segments forward from the opening, which is what keeps this
    from ending mid-sentence. The model's own end time wins whenever it
    already lands in the band — it chose that beat as the payoff, and nothing
    measured here knows better than that.
    """
    notes: List[str] = []
    if start_idx is None or not segments:
        # No transcript to snap to: honour the band arithmetically. Blunt, but
        # a clip of the right length beats a clip of the wrong one.
        end = min(max(proposed_end, start + lo), start + hi)
        return end, (["length forced without a transcript to snap to"]
                     if abs(end - proposed_end) > 0.5 else [])

    floor, ceiling = start + lo, start + hi
    if floor <= proposed_end <= ceiling:
        # Still snap it to the end of whatever sentence it lands in.
        for seg in segments[start_idx:]:
            if seg["end"] >= proposed_end - 0.5:
                if seg["end"] <= ceiling + OVERRUN_TOLERANCE:
                    return _snap_end(audio, segments, seg["end"]), notes
                break
        # That sentence runs too long. End on the sentence boundary in the band
        # nearest the model's choice, not on the model's own timestamp, which
        # lands wherever it lands -- often mid-word.
        near = [seg["end"] for seg in segments[start_idx:]
                if floor <= seg["end"] <= ceiling + OVERRUN_TOLERANCE]
        if near:
            best = min(near, key=lambda t: abs(t - proposed_end))
            return _snap_end(audio, segments, best), notes
        notes.append("no sentence boundary inside the requested length")
        return _forced_cut(audio, proposed_end, floor), notes

    # Which boundaries land inside the band at all.
    fits = [seg["end"] for seg in segments[start_idx:]
            if floor <= seg["end"] <= ceiling]

    if fits:
        if proposed_end > ceiling:
            # The payoff runs past the ceiling. Keep as much of it as the
            # length allows -- the last sentence that fits, not an arbitrary
            # one -- so the clip stops as close to the payoff as it can.
            best = fits[-1]
        else:
            # The model stopped short of the length that was asked for, so the
            # clip has to run on. Land near the middle of the band rather than
            # scraping its floor: someone who typed "30 seconds" wants 30, and
            # a run of 26s clips is the same complaint in a different shape.
            target = start + (lo + hi) / 2
            best = min(fits, key=lambda t: abs(t - target))
    else:
        # No sentence ends inside the band. Take the first boundary past the
        # ceiling if it barely overruns; otherwise cut on the quietest moment
        # at the ceiling rather than abandoning the requested length.
        over = next((seg["end"] for seg in segments[start_idx:]
                     if seg["end"] > ceiling), None)
        if over is not None and over <= ceiling + OVERRUN_TOLERANCE:
            best = over
        else:
            best = min(ceiling, max(segments[-1]["end"], start + lo))
            notes.append("no sentence boundary inside the requested length")
            if abs(best - proposed_end) > 1.0:
                notes.append(f"length {best - start:.1f}s (asked for {lo:.0f}-{hi:.0f}s)")
            return _forced_cut(audio, best, floor), notes

    if abs(best - proposed_end) > 1.0:
        notes.append(f"length {best - start:.1f}s (asked for {lo:.0f}-{hi:.0f}s)")
    return _snap_end(audio, segments, best), notes


def refine(highlights: List[Dict], transcript: Optional[Dict],
           clip_seconds: Optional[Sequence[float]] = None,
           audio: Optional[AudioTrack] = None,
           content_type: str = "",
           reserve_seconds: float = 0.0) -> List[Dict]:
    """Snap every highlight's span to real boundaries and the right length.

    `reserve_seconds` is room held back for something that will be added in
    front of the clip later — the hook replay. Held back here rather than
    trimmed afterwards, so a request for 30-second clips still produces
    30-second files once the replay is on the front of them.
    """
    segments = _segments(transcript)
    lo, hi = _band(clip_seconds, content_type)
    lo = max(4.0, lo - reserve_seconds)
    hi = max(lo + 1.0, hi - reserve_seconds)

    # Nothing may run past the end of the video. Extending a clip to reach the
    # length that was asked for is right up until it asks ffmpeg for footage
    # that does not exist, which yields a short clip with no explanation.
    try:
        limit = float(transcript.get("duration", 0) or 0) if transcript else 0.0
    except (TypeError, ValueError):
        limit = 0.0

    for h in highlights:
        original = (float(h.get("start_time", 0) or 0), float(h.get("end_time", 0) or 0))
        start, idx, notes = _choose_start(segments, h, audio)
        end, more = _choose_end(segments, idx, start, original[1], lo, hi, audio)
        notes += more
        if limit > 0 and end > limit:
            end = limit
            notes.append("ran to the end of the video")

        if end - start < 2.0:
            # Nothing usable came out of the snap; leave the model's own span
            # rather than shipping two seconds of nothing.
            h["boundary_notes"] = ["snapping produced nothing usable — kept the original span"]
            continue

        h["start_time"] = round(start, 2)
        h["end_time"] = round(end, 2)
        h["duration_target"] = [round(lo, 1), round(hi, 1)]
        h["boundary_notes"] = notes
        h["original_span"] = [round(original[0], 2), round(original[1], 2)]
        # The peak the audio found may now sit outside the clip. Recompute it
        # against the span that will actually be rendered, because the hook
        # replay opens on it.
        if audio:
            peak = audio.peak_time(start, end)
            if peak is not None:
                h["hook_peak"] = round(peak, 2)
    return highlights


def report(highlights: List[Dict]) -> None:
    """Print what moved, so a run's cuts can be checked without a video player."""
    for i, h in enumerate(highlights, 1):
        notes = h.get("boundary_notes") or []
        span = f"{h.get('start_time', 0):.1f}-{h.get('end_time', 0):.1f}s"
        length = float(h.get("end_time", 0) or 0) - float(h.get("start_time", 0) or 0)
        print(f"[cut] {i}: {span} ({length:.1f}s)"
              + (" · " + "; ".join(notes) if notes else ""), flush=True)
