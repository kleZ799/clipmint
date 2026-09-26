"""Measured hook signals: what the footage itself says about a moment.

Ranking used to be one opinion — an LLM reading a transcript. That opinion is
good at meaning and blind to sound. It cannot hear the scream, the laugh, or
the half-second of silence before the punchline, because none of that is
written down. Two moments can read identically on the page and be worlds apart
in the audio, and the audio is what a viewer actually meets in the first
second.

So this module measures the things a transcript cannot carry, per the clip
rule book:

  * audio energy spike   — a loudness jump against the video's own baseline
  * keyword trigger      — the phrases that mark surprise and payoff
  * silence-to-peak      — a quiet beat right before the spike (build-up)
  * dialogue density     — words per second, which is how dead air is caught

Two signals from the rule book are deliberately absent. Chat velocity would be
the strongest ground truth there is, and it needs a chat log this app never
receives; face reaction needs a face model per frame, which costs more than the
whole render. Rather than scoring them zero and quietly shrinking every clip's
ceiling, their weight is redistributed across the signals that ARE available —
so a score of 80 means the same thing whatever was measurable.

Everything here is best-effort. No audio track, no numpy, no ffmpeg: the text
signals still work, the weights redistribute again, and the run continues. A
ranking aid must never be able to sink a render.
"""
from __future__ import annotations


import re
import subprocess
from typing import Dict, List, Optional, Sequence

from . import proc

# Sample rate we decode the audio at. This is an envelope, not a signal we
# ever listen to: 4 kHz resolves loudness fine and decodes an hours-long VOD
# in a fraction of the time 48 kHz would.
ENVELOPE_RATE = 4000
# One RMS value per window. Short enough to catch a laugh, long enough that a
# consonant does not read as a spike.
WINDOW_SECONDS = 0.25

# The rule book's starting weights. Chat and face are listed so the
# redistribution below has something to redistribute -- and so the day a chat
# log becomes available, adding it is one line rather than a rewrite.
BASE_WEIGHTS = {
    "audio_spike": 0.30,
    "keyword": 0.25,
    "chat_velocity": 0.20,
    "face_reaction": 0.15,
    # How much the picture moves, ranked against the other candidates from the
    # same video -- see visual.py. A reaction a stranger can see something
    # behind beats the same reaction over a still frame.
    "motion": 0.15,
    "silence_to_peak": 0.10,
}

# How much of the final rank is measured signal versus the model's judgement.
# The model reads meaning and the signals cannot; the signals hear the room and
# the model cannot. Weighted toward the model because a loud moment that means
# nothing is still worthless, while a quiet moment that means something can
# carry a Short on its own.
MODEL_WEIGHT = 0.62
SIGNAL_WEIGHT = 1.0 - MODEL_WEIGHT

# Words per second under which the opening of a clip reads as dead air. Normal
# conversational speech sits near 2.5-3; below ~1.2 there is more silence than
# sentence, which is the single most reliable way to lose a viewer in the first
# two seconds.
MIN_OPENING_DENSITY = 1.2
OPENING_WINDOW = 2.0


# Phrases that mark a surprise or a payoff, weighted by how hard they land.
# Deliberately a short controlled list rather than anything generated: a
# trigger list that drifts stops being comparable between runs, and comparing
# runs is the entire point of scoring them the same way twice.
TRIGGER_PHRASES = {
    # the strongest — something has just happened and the speaker is reacting
    "what the": 1.0, "no way": 1.0, "oh my god": 1.0, "oh my gosh": 0.9,
    "are you kidding": 1.0, "you're kidding": 0.9, "holy": 0.95,
    "i can't believe": 1.0, "i cannot believe": 1.0, "what just happened": 1.0,
    "did that just": 1.0, "wait what": 1.0, "excuse me": 0.7,
    # direct address to the viewer — an explicit promise of a payoff
    "watch this": 0.9, "wait for it": 1.0, "look at this": 0.8,
    "check this out": 0.8, "you have to see": 0.9,
    # revelation and reversal
    "nobody talks about": 0.9, "the secret is": 0.9, "here's the thing": 0.7,
    "i was wrong": 0.9, "turns out": 0.7, "plot twist": 0.9,
    "nobody tells you": 0.9, "the truth is": 0.7,
    # the sound of something going wrong, which is most of a stream's best
    "oh no": 0.85, "what happened": 0.8, "that's not": 0.6, "why is": 0.6,
    "how did": 0.7, "i'm dead": 0.8, "are you serious": 0.95,
    "you gotta be": 0.9, "bro": 0.5, "dude": 0.5, "shut up": 0.7,
    "stop it": 0.6, "let's go": 0.8, "oh my days": 0.8,
}

# Punctuation the transcriber puts where a speaker reacted. A line ending in
# "?!" is doing something a flat statement is not.
_EXCLAIM = re.compile(r"[!?]")


class AudioTrack:
    """A loudness envelope for one source video, in 0.25s windows.

    Everything the audio signals need is derived from this one decode: peak
    loudness inside a span, the quiet run before it, and where in a span the
    loudest moment actually sits (which is what the hook replay opens on).
    """

    def __init__(self, rms: Sequence[float], window: float = WINDOW_SECONDS) -> None:
        self.rms = list(rms)
        self.window = window
        ordered = sorted(v for v in self.rms if v > 0)
        # Percentile anchors instead of min/max: one clipped frame or one
        # burst of silence would otherwise define the whole scale, and every
        # real moment would then score within a few points of every other.
        self._p50 = _percentile(ordered, 0.50)
        self._p95 = _percentile(ordered, 0.95)
        # Anything under this counts as silence for the build-up signal.
        self._quiet = max(1e-6, self._p50 * 0.35)

    def __bool__(self) -> bool:
        return bool(self.rms)

    @property
    def duration(self) -> float:
        return len(self.rms) * self.window

    def _index(self, t: float) -> int:
        return max(0, min(len(self.rms) - 1, int(t / self.window)))

    def peak(self, start: float, end: float) -> float:
        """Loudest window in [start, end], raw RMS."""
        if not self.rms or end <= start:
            return 0.0
        a, b = self._index(start), self._index(end)
        return max(self.rms[a:b + 1] or [0.0])

    def peak_time(self, start: float, end: float) -> Optional[float]:
        """When the loudest moment in a span happens, in source seconds."""
        if not self.rms or end <= start:
            return None
        a, b = self._index(start), self._index(end)
        window = self.rms[a:b + 1]
        if not window:
            return None
        return (a + window.index(max(window))) * self.window

    def spike(self, start: float, end: float) -> float:
        """0-1: how loud this span's peak is against the whole video.

        Normalised across the source, exactly as the rule book asks, so one
        very loud scene cannot make every span in the video look like a hook.
        """
        if self._p95 <= 0:
            return 0.0
        return _clamp01(self.peak(start, end) / self._p95)

    def silence_to_peak(self, at: float, lookback: float = 3.0) -> float:
        """0-1: how much quiet sits in the seconds before `at`.

        A short still beat before a spike is what makes the spike read as a
        payoff rather than as more of the same noise. A flat, loud run-up to a
        loud moment scores nothing here.
        """
        if not self.rms:
            return 0.0
        a, b = self._index(max(0.0, at - lookback)), self._index(at)
        window = self.rms[a:b + 1]
        if not window:
            return 0.0
        quiet = sum(1 for v in window if v <= self._quiet)
        return _clamp01(quiet / len(window))

    def quietest_time(self, start: float, end: float) -> Optional[float]:
        """The most silent moment in a span — where a cut is least audible."""
        if not self.rms or end <= start:
            return None
        a, b = self._index(start), self._index(end)
        window = self.rms[a:b + 1]
        if not window:
            return None
        return (a + window.index(min(window))) * self.window


def _clamp01(v: float) -> float:
    return 0.0 if v < 0 else (1.0 if v > 1 else float(v))


def _percentile(ordered: Sequence[float], q: float) -> float:
    if not ordered:
        return 0.0
    i = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return float(ordered[i])


def analyse_audio(source_path: str, duration: float = 0.0) -> Optional[AudioTrack]:
    """Decode the source's audio down to a loudness envelope.

    Streamed rather than loaded: a four-hour VOD is gigabytes of PCM even at
    4 kHz, and all that survives the read is one float per quarter second —
    about 60,000 numbers for a stream that long. Returns None on anything that
    goes wrong, because every caller is expected to cope without it.
    """
    try:
        import numpy as np
    except ImportError:
        print("[signals] numpy is not installed — scoring on the transcript alone",
              flush=True)
        return None

    cmd = [
        "ffmpeg", "-v", "error", "-i", source_path,
        "-vn", "-ac", "1", "-ar", str(ENVELOPE_RATE),
        "-f", "s16le", "-",
    ]
    frame = int(ENVELOPE_RATE * WINDOW_SECONDS)
    # Read a couple of minutes of audio at a time: big enough that the pipe is
    # never the bottleneck, small enough that memory stays flat.
    block = frame * 480

    rms: List[float] = []
    child = None
    try:
        child = proc.popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        leftover = b""
        while True:
            chunk = child.stdout.read(block * 2)
            if not chunk:
                break
            buf = leftover + chunk
            usable = (len(buf) // (frame * 2)) * frame * 2
            leftover = buf[usable:]
            if not usable:
                continue
            samples = np.frombuffer(buf[:usable], dtype="<i2").astype("float32")
            windows = samples.reshape(-1, frame)
            rms.extend(np.sqrt((windows * windows).mean(axis=1)).tolist())
        child.stdout.close()
        child.wait(timeout=60)
    except Exception as e:
        print(f"[signals] could not read the audio ({e}) — "
              f"scoring on the transcript alone", flush=True)
        if child is not None:
            try:
                child.kill()
            except Exception:
                pass
        return None
    finally:
        if child is not None:
            proc.forget(child)

    if not rms:
        return None
    covered = len(rms) * WINDOW_SECONDS
    print(f"[signals] audio envelope: {len(rms)} windows over {covered:.0f}s", flush=True)
    return AudioTrack(rms)


# --- text signals ---------------------------------------------------------

def words_in(transcript: Optional[Dict], start: float, end: float) -> List[str]:
    """Every word spoken inside a span, from the segment transcript."""
    if not transcript:
        return []
    out: List[str] = []
    for seg in transcript.get("segments", []):
        try:
            s, e = float(seg["start"]), float(seg["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if e < start or s > end:
            continue
        out.extend(str(seg.get("text", "")).split())
    return out


def text_in(transcript: Optional[Dict], start: float, end: float) -> str:
    return " ".join(words_in(transcript, start, end))


def dialogue_density(transcript: Optional[Dict], start: float, end: float) -> float:
    """Words per second across a span. Low means silence, which means swipe."""
    span = max(0.001, end - start)
    return len(words_in(transcript, start, end)) / span


def keyword_hit(text: str) -> float:
    """0-1: how strongly a stretch of speech reads as a reaction.

    Weighted by phrase, not counted: three weak phrases do not add up to one
    "I can't believe that just happened", and letting them would reward
    rambling. The strongest phrase present sets the floor, and anything else
    nudges it up a little.
    """
    low = f" {text.lower()} "
    hits = [w for phrase, w in TRIGGER_PHRASES.items() if phrase in low]
    if not hits:
        # Punctuation is the transcriber's own record of how a line was said.
        return 0.25 if _EXCLAIM.search(text) else 0.0
    best = max(hits)
    extra = min(0.2, 0.05 * (len(hits) - 1))
    return _clamp01(best + extra)


# --- composite scoring ----------------------------------------------------

def measure(highlight: Dict, transcript: Optional[Dict],
            audio: Optional[AudioTrack]) -> Dict[str, float]:
    """Every sub-signal for one candidate, each normalised to 0-1.

    Measured on the OPENING of the clip as much as on the clip as a whole.
    That is the whole argument of the rule book: distribution is decided in
    the first second, so a signal that only describes second twenty is
    describing something most viewers will never reach.
    """
    start = float(highlight.get("start_time", 0) or 0)
    end = float(highlight.get("end_time", 0) or 0)
    opening_end = min(end, start + OPENING_WINDOW * 2)

    opening_text = (str(highlight.get("first_line") or "")
                    or text_in(transcript, start, opening_end))
    whole_text = text_in(transcript, start, end) or opening_text

    out: Dict[str, float] = {
        # The opening line's own trigger strength counts double against the
        # rest of the clip's, because it is the part that has to earn the
        # second second.
        "keyword": _clamp01(0.65 * keyword_hit(opening_text)
                            + 0.35 * keyword_hit(whole_text)),
    }

    # Only when there is a transcript to count words in. Without one, density
    # measures zero -- which is not "this clip opens on silence", it is "we
    # did not look", and penalising a clip for that would be a lie about it.
    if transcript:
        out["density"] = dialogue_density(transcript, start,
                                          min(end, start + OPENING_WINDOW))

    seen = highlight.get("visual") or {}
    if "motion_rank" in seen:
        out["motion"] = float(seen["motion_rank"])

    if audio:
        out["audio_spike"] = audio.spike(start, end)
        peak = audio.peak_time(start, end)
        out["silence_to_peak"] = audio.silence_to_peak(peak if peak is not None else start)
        if peak is not None:
            out["peak_time"] = peak
    return out


def _available_weights(measured: Dict[str, float]) -> Dict[str, float]:
    """Weights for the signals we actually have, summing to 1.

    Redistribution rather than zeroing, per the rule book: a video with no
    usable audio should still produce scores that use the full 0-100 range,
    otherwise its best clip looks worse than a mediocre clip from a video that
    happened to decode.
    """
    have = {k: w for k, w in BASE_WEIGHTS.items() if k in measured}
    total = sum(have.values())
    if not have or total <= 0:
        return {}
    return {k: w / total for k, w in have.items()}


def signal_score(measured: Dict[str, float]) -> Optional[int]:
    """The rule book's HookScore for one candidate, 0-100, or None."""
    weights = _available_weights(measured)
    if not weights:
        return None
    score = sum(measured[k] * w for k, w in weights.items())
    return int(round(100 * _clamp01(score)))


# The most that can ever be measured here: chat velocity and face reaction are
# not obtainable in this app, so their weight is not a shortfall to apologise
# for. This is the denominator coverage is measured against.
_OBTAINABLE = sum(w for k, w in BASE_WEIGHTS.items()
                  if k in ("audio_spike", "keyword", "motion", "silence_to_peak"))


def coverage(measured: Dict[str, float]) -> float:
    """How much of the measurable evidence this candidate actually has.

    Redistribution keeps the 0-100 scale honest, but it cannot manufacture
    confidence: with no audio the whole score rests on one keyword list, and a
    clip containing the words "no way" would otherwise score a flat 100 and
    outrank everything the model actually understood. So coverage scales how
    far the measured half of the rank is allowed to move things.
    """
    have = sum(w for k, w in BASE_WEIGHTS.items() if k in measured)
    return _clamp01(have / _OBTAINABLE) if _OBTAINABLE else 0.0


def rescore(highlights: List[Dict], transcript: Optional[Dict],
            audio: Optional[AudioTrack]) -> List[Dict]:
    """Fold measured signals into each highlight's rank, in place.

    `score` stays the field everything downstream sorts on, so nothing else in
    the app has to learn about any of this. What changes is what goes into it:
    the model's blended judgement, pulled toward what the audio and the words
    actually did.

    Every sub-signal is kept on the highlight as well. That is not for display
    — it is the record the rule book's learning loop needs, so that once real
    retention numbers exist there is something to correlate them against.
    """
    for h in highlights:
        measured = measure(h, transcript, audio)
        h["signals"] = {k: round(v, 4) for k, v in measured.items()}

        peak = measured.pop("peak_time", None)
        if peak is not None:
            h["hook_peak"] = round(peak, 2)
        density = measured.pop("density", None)
        if density is not None:
            h["opening_density"] = round(density, 2)

        measured_score = signal_score(measured)
        h["signal_score"] = measured_score

        model_score = int(h.get("score", 0) or 0)
        if measured_score is None:
            continue
        share = SIGNAL_WEIGHT * coverage(measured)
        blended = (1.0 - share) * model_score + share * measured_score

        # Dead air in the opening is not a matter of degree. Two seconds of
        # near-silence at the top of a Short is the one failure the rule book
        # calls out as disqualifying, so it is a penalty rather than a
        # slightly lower weighted average.
        if density is not None and density < MIN_OPENING_DENSITY:
            shortfall = 1.0 - (density / MIN_OPENING_DENSITY)
            blended *= 1.0 - 0.35 * _clamp01(shortfall)
            h["opening_penalty"] = round(0.35 * _clamp01(shortfall), 3)

        # The same for the picture: a black or frozen opening loses the test
        # audience before the first line has landed, whatever follows it.
        from .visual import opening_penalty
        dark = opening_penalty(h.get("visual") or {})
        if dark:
            blended *= 1.0 - dark
            h["visual_penalty"] = dark

        h["model_score"] = model_score
        h["score"] = int(round(max(0.0, min(100.0, blended))))
    return highlights


def summarise(highlights: List[Dict]) -> str:
    """One line for the log, so a run's ranking can be read at a glance."""
    parts = []
    for h in highlights[:8]:
        parts.append(f"{h.get('score', '?')}"
                     f"(m{h.get('model_score', '?')}/s{h.get('signal_score', '?')})")
    return " ".join(parts)
