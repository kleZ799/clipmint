"""The edit a person would make after the cut: captions, jump cuts, punch-ins.

A rendered clip is a correct span of the source. What the short-form tools add
on top is editing -- the dead air taken out, a zoom on the line that matters,
captions, the odd emoji or cutaway -- and that is what this does, to every
clip, in one extra encode:

  1. Listen to the clip for word timings (words.py).
  2. Plan the cuts: pauses between words that are actually quiet, and filler
     words ("um", "uh"). A pause with something audible in it -- a laugh, an
     explosion in the game -- is not dead air and is left alone, and so is the
     quiet run-up to the clip's loudest moment, which is a build, not a gap.
  3. Move every word onto the new, shorter timeline.
  4. Plan punch-ins on emphasis (a trigger phrase, an exclamation, a word
     said louder than the rest), emoji on words that name a feeling or a
     thing, and B-roll where the model found something concrete.
  5. Build one ffmpeg filtergraph that does all of it, captions last, so the
     words sit on top of everything else.

Each step is optional and each one degrades on its own: no Whisper means no
captions and no cuts, but punch-ins from the audio alone would still be too
blind to trust, so they go too. Nothing here may fail a clip -- polish() hands
back the clip it was given if anything goes wrong.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import accel, captions, proc
from .bundled import asset_dir

Span = Tuple[float, float]

# Filler words cut out of the audio when pauses are cut. Only sounds that are
# never a word in their own right: "like" and "you know" are fillers half the
# time and meaning the other half, and cutting meaning is worse than leaving
# a filler in.
FILLERS = {"um", "umm", "uh", "uhh", "uhm", "erm", "er", "hmm", "hm", "mm", "mmm",
           "ah", "ahh", "eh"}

# A pause longer than this is dead air. Talking videos run tighter than
# streams, where a beat of silence is usually the streamer watching the game.
_GAP = {"stream": 0.9}
_GAP_DEFAULT = 0.55
# How much of a cut pause is kept either side, so speech still breathes.
_PAD = {"stream": 0.16}
_PAD_DEFAULT = 0.1
_LEAD_KEEP = 0.12       # before the first word
_TAIL_KEEP = 0.45       # after the last word -- room for the reaction
_MIN_CUT = 0.15         # a shorter cut is a stutter, not an edit
_MIN_KEEP = 0.2
_MAX_SEGMENTS = 40
# A pause is quiet when nothing in it is louder than this share of speech.
_QUIET_SHARE = 0.3
# The loudness envelope comes in quarter-second windows, so a pause is only
# listened to from one window in from each edge -- otherwise the window it
# starts in still holds the end of the word before it.
_EDGE = 0.26
# The quiet before the clip's peak is a build-up, not a gap.
_PROTECT_BEFORE_PEAK, _PROTECT_AFTER_PEAK = 3.0, 0.5

# Punch-ins.
_PUNCH_ZOOM = 1.14
_JUMP_ZOOM = 1.06       # alternating zoom that hides a jump cut on a face
_PUNCH_MIN, _PUNCH_MAX = 0.9, 1.8
_PUNCH_SPACING = 3.5

# Emoji.
_EMOJI_SECONDS = 1.1
_EMOJI_SPACING = 3.0

EMOJI_WORDS: Dict[str, Tuple[str, ...]] = {
    "laugh": ("laugh", "laughing", "laughed", "hilarious", "funny", "lol", "lmao",
              "haha", "hahaha", "joke", "joking"),
    "fire": ("fire", "lit", "insane", "flames", "heat"),
    "mindblown": ("crazy", "unbelievable", "mindblowing", "wild", "shocking", "shocked"),
    "scream": ("scary", "scared", "terrifying", "terrified", "horror", "afraid",
               "creepy", "jumpscare"),
    "money": ("money", "cash", "rich", "dollars", "dollar", "paid", "million",
              "millions", "billion", "expensive", "price", "salary", "profit"),
    "heart": ("love", "loved", "lovely", "heart", "adore"),
    "skull": ("dead", "died", "dying", "killed", "rip"),
    "trophy": ("win", "won", "winner", "winning", "champion", "victory", "clutch"),
    "bulb": ("idea", "tip", "trick", "secret", "hack", "lesson", "realised", "realized"),
    "warning": ("mistake", "mistakes", "warning", "careful", "danger", "dangerous", "avoid"),
    "rocket": ("launch", "launched", "skyrocket", "viral", "rocket"),
    "angry": ("angry", "mad", "rage", "furious", "hate", "annoying"),
    "cry": ("sad", "cry", "crying", "cried", "tears", "heartbroken"),
    "hundred": ("exactly", "facts", "perfect", "hundred", "percent"),
    "game": ("gaming", "gamer", "controller"),
    "food": ("food", "pizza", "eat", "eating", "hungry", "delicious", "burger"),
    "party": ("party", "celebrate", "birthday", "congrats", "congratulations"),
    "flushed": ("awkward", "embarrassing", "embarrassed", "cringe"),
    "eyes": ("sus", "suspicious", "secretly", "staring"),
    "thinking": ("wonder", "curious", "confused", "question"),
    "check": ("correct", "success", "solved"),
    "cross": ("wrong", "fail", "failed", "failure", "nope"),
    "cool": ("cool", "chill", "smooth"),
    "pray": ("please", "pray", "grateful", "blessed", "thankful"),
    "muscle": ("strong", "strength", "gym", "workout", "powerful"),
    "goat": ("goat", "legend", "legendary", "greatest"),
    "brain": ("smart", "genius", "brain", "clever", "intelligent"),
    "alarm": ("late", "deadline", "hurry"),
    "chart": ("grow", "growing", "growth", "increase", "subscribers", "followers"),
}
EMOJI_PHRASES = {
    ("no", "way"): "mindblown", ("what", "the"): "mindblown",
    ("oh", "my", "god"): "scream", ("oh", "no"): "scream",
    ("let's", "go"): "trophy", ("i'm", "dead"): "skull",
}
_EMOJI_OF = {w: name for name, ws in EMOJI_WORDS.items() for w in ws}


@dataclass
class Options:
    captions: str = captions.OFF
    cut_pauses: bool = False
    punch_ins: bool = False
    emoji: bool = False
    broll: bool = False
    layout: str = "stacked"
    aspect_ratio: str = "9:16"
    cam_panel_fraction: float = 0.42
    kind: str = "other"
    # The channel's logo (brand.py), and the corner it goes in. None for none.
    logo: Optional[str] = None
    logo_corner: str = "top-right"

    @property
    def needs_words(self) -> bool:
        """Whether anything asked for depends on what is said."""
        return (self.captions != captions.OFF or self.cut_pauses or self.punch_ins
                or self.emoji or self.broll)

    @property
    def anything(self) -> bool:
        return self.needs_words or bool(self.logo)


def options_from_spec(spec, kind: Optional[str] = None) -> Options:
    """The edit a LayoutSpec asks for, for a video of `kind`."""
    from . import brand
    from . import broll as broll_mod

    k = (kind or getattr(spec, "content_kind", "") or "other").lower()
    logo = brand.logo_path() if getattr(spec, "logo", False) else None
    wants_broll = bool(getattr(spec, "broll", False))
    if wants_broll and not broll_mod.available():
        # Said once, here, rather than after every clip has been listened to
        # for nothing.
        print("[broll] skipped - add a free Pexels key in Settings to use B-roll",
              flush=True)
        wants_broll = False
    return Options(
        captions=captions.normalise(getattr(spec, "captions", captions.OFF)),
        cut_pauses=bool(getattr(spec, "cut_pauses", False)),
        punch_ins=bool(getattr(spec, "punch_ins", False)),
        emoji=bool(getattr(spec, "emoji", False)),
        # Never over gameplay: on a stream the game is the picture.
        broll=wants_broll and k != "stream",
        layout=getattr(spec, "layout", "stacked"),
        aspect_ratio=getattr(spec, "aspect_ratio", "9:16"),
        cam_panel_fraction=float(getattr(spec, "cam_panel_fraction", 0.42)),
        kind=k,
        logo=str(logo) if logo else None,
        logo_corner=brand.corner(),
    )


@dataclass
class Plan:
    """Everything decided about one clip before anything is encoded."""
    path: str
    duration: float
    width: int
    height: int
    has_audio: bool
    words: List[Dict] = field(default_factory=list)          # on the new timeline
    # What the captions say, on the new timeline. The same as `words` unless
    # someone corrected the text (captions.retime), in which case the cuts
    # and punch-ins still follow what was heard and only the captions change.
    caption_words: List[Dict] = field(default_factory=list)
    # What Whisper heard, on the clip's own uncut timeline -- kept on the clip
    # so a caption fix re-renders from the same words without listening again.
    heard: List[Dict] = field(default_factory=list)
    keeps: List[Span] = field(default_factory=list)
    zooms: List[Tuple[float, float, float]] = field(default_factory=list)
    emoji: List[Tuple[float, float, str]] = field(default_factory=list)
    broll: List[Dict] = field(default_factory=list)

    @property
    def new_duration(self) -> float:
        return sum(b - a for a, b in self.keeps) if self.keeps else self.duration

    @property
    def cut(self) -> bool:
        return bool(self.keeps) and not (
            len(self.keeps) == 1 and self.keeps[0][0] <= 0.001
            and self.keeps[0][1] >= self.duration - 0.001)


# --- probing ----------------------------------------------------------------

def _probe(path: str) -> Tuple[float, int, int, bool]:
    out = proc.run_checked(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,width,height", "-of", "json", path],
        what="ffprobe (clip for editing)", capture_stdout=True,
    ).stdout
    import json
    data = json.loads(out or "{}")
    duration = float((data.get("format") or {}).get("duration") or 0)
    width = height = 0
    has_audio = False
    for s in data.get("streams") or []:
        if s.get("codec_type") == "video" and not width:
            width, height = int(s.get("width") or 0), int(s.get("height") or 0)
        elif s.get("codec_type") == "audio":
            has_audio = True
    return duration, width, height, has_audio


def _norm(word: str) -> str:
    return re.sub(r"[^\w']+", "", str(word or "").lower())


# --- cutting ----------------------------------------------------------------

def _speech_level(words: List[Dict], track) -> float:
    if not track:
        return 0.0
    levels = sorted(track.peak(float(w["start"]), float(w["end"])) for w in words)
    return levels[len(levels) // 2] if levels else 0.0


def plan_cuts(words: List[Dict], duration: float, track=None, kind: str = "other",
              protect: Optional[Span] = None) -> List[Span]:
    """The spans of the clip to keep, in order. [(0, duration)] means no cut.

    `track` is the clip's own loudness envelope (signals.AudioTrack). Without
    one, a pause cannot be heard to be empty; on a stream that means nothing
    is cut, because the pauses there are usually full of game.
    """
    whole = [(0.0, duration)]
    if not words or duration <= 0:
        return whole

    gap_limit = _GAP.get(kind, _GAP_DEFAULT)
    pad = _PAD.get(kind, _PAD_DEFAULT)
    level = _speech_level(words, track)
    threshold = level * _QUIET_SHARE

    def quiet(a: float, b: float) -> bool:
        """Is nothing audible between two moments that bound a pause?"""
        if not track or level <= 0:
            return kind != "stream"
        a, b = a + _EDGE, b - _EDGE
        if b <= a:
            # Too short to hear inside without touching the words. Listen to
            # the one window in the middle; if that still holds speech, the
            # pause stays -- a missed cut costs less than a clipped word.
            a = b = (a + b) / 2
            return track.peak(a, a + 0.001) <= threshold
        return track.peak(a, b) <= threshold

    cuts: List[Span] = []
    first = float(words[0]["start"])
    if first > _LEAD_KEEP + _MIN_CUT and quiet(-_EDGE, first):
        cuts.append((0.0, first - _LEAD_KEEP))

    for prev, nxt in zip(words, words[1:]):
        a, b = float(prev["end"]), float(nxt["start"])
        if b - a > gap_limit and quiet(a, b):
            cuts.append((a + pad, b - pad))

    for w in words:
        if _norm(w.get("word")) in FILLERS:
            cuts.append((max(0.0, float(w["start"]) - 0.02),
                         min(duration, float(w["end"]) + 0.02)))

    last = float(words[-1]["end"])
    if duration - last > _TAIL_KEEP + _MIN_CUT and quiet(last, duration + _EDGE):
        cuts.append((last + _TAIL_KEEP, duration))

    if protect:
        pa, pb = protect
        cuts = [(a, b) for a, b in cuts if b <= pa or a >= pb]

    cuts = _merge([(max(0.0, a), min(duration, b)) for a, b in cuts if b - a >= _MIN_CUT])
    if not cuts:
        return whole

    keeps = _complement(cuts, duration)
    # A sliver of clip between two cuts is a flash frame unless a word is in it.
    spoken = [(float(w["start"]), float(w["end"])) for w in words
              if _norm(w.get("word")) not in FILLERS]
    keeps = [(a, b) for a, b in keeps
             if b - a >= _MIN_KEEP or any(s < b and e > a for s, e in spoken)]
    # Too many pieces makes a filtergraph nobody should have to run. Put the
    # smallest cuts back until it fits.
    while len(keeps) > _MAX_SEGMENTS:
        gaps = [(keeps[i + 1][0] - keeps[i][1], i) for i in range(len(keeps) - 1)]
        _, i = min(gaps)
        keeps[i:i + 2] = [(keeps[i][0], keeps[i + 1][1])]
    return keeps or whole


def _merge(spans: List[Span]) -> List[Span]:
    out: List[Span] = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1] + 0.01:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _complement(cuts: List[Span], duration: float) -> List[Span]:
    keeps, at = [], 0.0
    for a, b in cuts:
        if a > at:
            keeps.append((at, a))
        at = max(at, b)
    if at < duration:
        keeps.append((at, duration))
    return [(round(a, 3), round(b, 3)) for a, b in keeps if b - a > 0.001]


def remap(t: float, keeps: List[Span]) -> float:
    """A time on the uncut clip, on the cut one. Inside a cut, the next kept frame."""
    done = 0.0
    for a, b in keeps:
        if t < a:
            return done
        if t <= b:
            return done + (t - a)
        done += b - a
    return done


def remap_words(words: List[Dict], keeps: List[Span]) -> List[Dict]:
    out = []
    for w in words:
        s, e = remap(float(w["start"]), keeps), remap(float(w["end"]), keeps)
        if e - s < 0.03:
            continue                  # it was cut
        out.append({**w, "start": round(s, 3), "end": round(e, 3)})
    return out


# --- punch-ins ----------------------------------------------------------------

def _anchor(layout: str, cam_panel_fraction: float) -> Tuple[float, float]:
    """The point a punch-in zooms toward, as fractions of the frame."""
    if layout == "stacked":
        return 0.5, max(0.1, min(0.9, cam_panel_fraction / 2))   # the face panel
    if layout == "facetrack":
        return 0.5, 0.42                                          # where a face sits
    return 0.5, 0.5


def plan_punch_ins(words: List[Dict], duration: float, keeps: List[Span],
                   layout: str) -> List[Tuple[float, float, float]]:
    """(start, end, zoom) windows on the new timeline."""
    from .signals import TRIGGER_PHRASES

    if not words:
        return []
    norm = [_norm(w.get("word")) for w in words]
    loud = sorted(float(w.get("loud") or 0) for w in words)
    loud_bar = loud[int(len(loud) * 0.9)] if loud else 0.0

    scored: List[Tuple[float, int]] = []
    for i, w in enumerate(words):
        score = 0.0
        for phrase, weight in TRIGGER_PHRASES.items():
            parts = phrase.split()
            if norm[i:i + len(parts)] == [_norm(p) for p in parts]:
                score = max(score, float(weight))
        if "!" in str(w.get("word", "")):
            score = max(score, 0.6)
        if loud_bar > 0 and float(w.get("loud") or 0) >= loud_bar:
            score = max(score, 0.5)
        if score:
            scored.append((score, i))

    budget = max(1, int(duration // 8))
    chosen: List[Tuple[float, float, float]] = []
    for score, i in sorted(scored, reverse=True):
        start = max(0.0, float(words[i]["start"]) - 0.04)
        end = start + _PUNCH_MAX
        # End on the sentence, not in the middle of the next one.
        for j in range(i + 1, len(words)):
            if float(words[j]["start"]) - float(words[j - 1]["end"]) > 0.35 \
                    or re.search(r"[.!?]$", str(words[j - 1].get("word", ""))):
                end = min(end, float(words[j - 1]["end"]) + 0.15)
                break
        end = min(max(end, start + _PUNCH_MIN), duration)
        if end - start < 0.5:
            continue
        if any(abs(start - a) < _PUNCH_SPACING or (start < b and end > a)
               for a, b, _ in chosen):
            continue
        chosen.append((round(start, 3), round(end, 3), _PUNCH_ZOOM))
        if len(chosen) >= budget:
            break

    # On a face, a jump cut reads as a stumble unless the framing changes with
    # it -- which is why editors alternate between two zooms across cuts.
    if layout == "facetrack" and len(keeps) > 1:
        at = 0.0
        for n, (a, b) in enumerate(keeps):
            seg = b - a
            if n % 2 == 1 and seg >= 0.6:
                chosen.append((round(at, 3), round(at + seg, 3), _JUMP_ZOOM))
            at += seg
    return sorted(chosen)


# --- emoji ------------------------------------------------------------------

def plan_emoji(words: List[Dict], duration: float) -> List[Tuple[float, float, str]]:
    """(start, seconds, name) for each emoji pop."""
    folder = asset_dir("emoji")
    if folder is None or not words:
        return []
    norm = [_norm(w.get("word")) for w in words]
    hits: List[Tuple[float, float, str]] = []
    for i, w in enumerate(words):
        name, strength = None, 0.0
        for phrase, emo in EMOJI_PHRASES.items():
            if tuple(norm[i:i + len(phrase)]) == phrase:
                name, strength = emo, 1.0
                break
        if name is None and norm[i] in _EMOJI_OF:
            name, strength = _EMOJI_OF[norm[i]], 0.7
        if name is None or not (folder / f"{name}.png").exists():
            continue
        if "!" in str(w.get("word", "")):
            strength += 0.2
        hits.append((strength, float(w["start"]), name))

    budget = min(5, 1 + int(duration // 8))
    chosen: List[Tuple[float, float, str]] = []
    for strength, at, name in sorted(hits, key=lambda h: (-h[0], h[1])):
        if at > duration - 0.5:
            continue
        if any(abs(at - t) < _EMOJI_SPACING for t, _, _ in chosen):
            continue
        # The same face twice in one clip reads as a glitch, not a reaction.
        if any(n == name for _, _, n in chosen):
            continue
        chosen.append((round(at, 3), min(_EMOJI_SECONDS, duration - at), name))
        if len(chosen) >= budget:
            break
    return sorted(chosen)


# --- planning ---------------------------------------------------------------

def analyse(path: str, highlight: Dict, opts: Options,
            language: Optional[str] = None) -> Optional[Plan]:
    """Listen to a rendered clip and decide its edit. None if there is none."""
    from . import words as words_mod
    from .signals import analyse_audio

    try:
        duration, width, height, has_audio = _probe(path)
    except Exception as e:  # noqa: BLE001
        print(f"[edit] could not read {os.path.basename(path)} ({e})", flush=True)
        return None
    if duration <= 0 or not width or not height:
        return None

    plan = Plan(path=path, duration=duration, width=width, height=height,
                has_audio=has_audio, keeps=[(0.0, duration)])

    given = highlight.get("heard_words")
    if not opts.needs_words:
        spoken = []
    elif isinstance(given, list) and given:
        spoken = [dict(w) for w in given]
    else:
        spoken = words_mod.transcribe_words(path, language) if has_audio else []
    plan.heard = [{"start": w["start"], "end": w["end"], "word": w["word"]} for w in spoken]
    fixed = highlight.get("caption_words")
    shown = [dict(w) for w in fixed] if isinstance(fixed, list) and fixed else spoken
    track = analyse_audio(path, duration) if has_audio and (
        opts.cut_pauses or opts.punch_ins) else None
    if track:
        for w in spoken:
            w["loud"] = track.peak(float(w["start"]), float(w["end"]))

    if opts.cut_pauses and spoken:
        protect = None
        peak = highlight.get("hook_peak")
        start = highlight.get("start_time")
        if peak is not None and start is not None:
            at = float(peak) - float(start)
            if 0 <= at <= duration:
                protect = (at - _PROTECT_BEFORE_PEAK, at + _PROTECT_AFTER_PEAK)
        plan.keeps = plan_cuts(spoken, duration, track, opts.kind, protect)

    plan.words = remap_words(spoken, plan.keeps) if plan.cut else spoken
    plan.caption_words = remap_words(shown, plan.keeps) if plan.cut else shown
    new_duration = plan.new_duration
    if opts.punch_ins and plan.words:
        plan.zooms = plan_punch_ins(plan.words, new_duration, plan.keeps, opts.layout)
    if opts.emoji and plan.words:
        plan.emoji = plan_emoji(plan.words, new_duration)
    return plan


# --- rendering --------------------------------------------------------------

def _between(spans) -> str:
    return "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in spans)


def build_filter(plan: Plan, opts: Options, ass_name: Optional[str],
                 fonts_dir: Optional[str], first_extra_input: int = 1
                 ) -> Tuple[str, List[List[str]], bool, str]:
    """The filtergraph for a plan: (graph, extra inputs, audio cut, video pad).

    Extra inputs are argument lists ([-t, 3, -i, path]) in input order,
    starting at input index `first_extra_input`. The graph is "" when the
    plan changes nothing.
    """
    W, H = plan.width, plan.height
    chains: List[str] = []
    inputs: List[List[str]] = []
    n = 0

    def label() -> str:
        nonlocal n
        n += 1
        return f"v{n}"

    cur = "0:v"
    audio_cut = False
    if plan.cut:
        k = len(plan.keeps)
        if k > 1:
            chains.append(f"[0:v]split={k}" + "".join(f"[vs{i}]" for i in range(k)))
            if plan.has_audio:
                chains.append(f"[0:a]asplit={k}" + "".join(f"[as{i}]" for i in range(k)))
        seg = []
        for i, (a, b) in enumerate(plan.keeps):
            src_v = f"vs{i}" if k > 1 else "0:v"
            chains.append(f"[{src_v}]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[vt{i}]")
            seg.append(f"[vt{i}]")
            if plan.has_audio:
                src_a = f"as{i}" if k > 1 else "0:a"
                fade = min(0.012, (b - a) / 4)
                chains.append(
                    f"[{src_a}]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS,"
                    f"afade=t=in:st=0:d={fade:.3f},"
                    f"afade=t=out:st={max(0.0, b - a - fade):.3f}:d={fade:.3f}[at{i}]")
                seg.append(f"[at{i}]")
        out = label()
        if plan.has_audio:
            chains.append("".join(seg) + f"concat=n={k}:v=1:a=1[{out}][acut]")
            audio_cut = True
        else:
            chains.append("".join(seg) + f"concat=n={k}:v=1:a=0[{out}]")
        cur = out

    # Punch-ins: one zoomed copy per zoom level, laid over the frame only
    # while it is wanted. Crops are fixed-size, so nothing reconfigures mid-clip.
    ax, ay = _anchor(opts.layout, opts.cam_panel_fraction)
    levels: Dict[float, List[Span]] = {}
    for a, b, z in plan.zooms:
        levels.setdefault(z, []).append((a, b))
    # The smaller zoom first, so an emphasis punch lands on top of it.
    for z in sorted(levels):
        zw = int(-(-W * z // 2) * 2)
        zh = int(-(-H * z // 2) * 2)
        x = max(0, min(zw - W, int(round(W * ax * (z - 1)))))
        y = max(0, min(zh - H, int(round(H * ay * (z - 1)))))
        base, zoom, out = label(), label(), label()
        chains.append(f"[{cur}]split=2[{base}][{zoom}]")
        zoomed = label()
        chains.append(f"[{zoom}]scale={zw}:{zh},crop={W}:{H}:{x}:{y}[{zoomed}]")
        chains.append(f"[{base}][{zoomed}]overlay=0:0:enable='{_between(levels[z])}'[{out}]")
        cur = out

    idx = first_extra_input
    for b in plan.broll:
        at, secs = float(b["at"]), float(b["seconds"])
        inputs.append(["-t", f"{secs + 0.5:.2f}", "-i", os.path.abspath(b["path"])])
        clip, out = label(), label()
        fade = 0.2
        chains.append(
            f"[{idx}:v]trim=duration={secs:.3f},setpts=PTS-STARTPTS+{at:.3f}/TB,"
            f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,"
            f"format=yuva420p,fade=t=in:st={at:.3f}:d={fade}:alpha=1,"
            f"fade=t=out:st={at + secs - fade:.3f}:d={fade}:alpha=1[{clip}]")
        chains.append(f"[{cur}][{clip}]overlay=0:0:eof_action=pass:"
                      f"enable='between(t,{at:.3f},{at + secs:.3f})'[{out}]")
        cur = out
        idx += 1

    folder = asset_dir("emoji")
    if plan.emoji and folder is not None:
        size = int(min(W, H) * 0.15) // 2 * 2
        y_cap = captions.caption_y(opts.layout, opts.aspect_ratio, opts.cam_panel_fraction)
        if opts.layout == "stacked":
            # Beside the caption on the gameplay side, never over the face.
            ex, ey = int(W * 0.78 - size / 2), int(H * y_cap + H * 0.05)
        else:
            ex, ey = int(W / 2 - size / 2), int(H * y_cap - H * 0.08 - size)
        ey = max(0, min(H - size, ey))
        bounce = int(H * 0.035)
        total = plan.new_duration
        for at, secs, name in plan.emoji:
            # Only as long as it is on screen: a looped still is decoded
            # frame by frame for as long as its input lasts.
            inputs.append(["-loop", "1", "-framerate", "30",
                           "-t", f"{min(total, at + secs + 0.1):.3f}",
                           "-i", str((folder / f"{name}.png").resolve())])
            pic, out = label(), label()
            chains.append(
                f"[{idx}:v]scale={size}:{size},format=rgba,"
                f"fade=t=in:st={at:.3f}:d=0.12:alpha=1,"
                f"fade=t=out:st={max(at, at + secs - 0.18):.3f}:d=0.18:alpha=1[{pic}]")
            chains.append(
                f"[{cur}][{pic}]overlay=x={ex}:y='{ey}+{bounce}*max(0,1-(t-{at:.3f})/0.2)':"
                f"eof_action=pass:enable='between(t,{at:.3f},{at + secs:.3f})'[{out}]")
            cur = out
            idx += 1

    if opts.logo and os.path.exists(opts.logo):
        # Under the captions, over everything else, in the corner asked for.
        # Scaled to a box so a wide wordmark and a square icon both fit.
        box = int(min(W, H) * brand_size()) // 2 * 2
        margin = int(min(W, H) * 0.04)
        right = opts.logo_corner.endswith("right")
        bottom = opts.logo_corner.startswith("bottom")
        x = f"W-w-{margin}" if right else str(margin)
        # Bottom corners sit above the band every app covers with its own UI.
        y = f"H-h-{int(H * 0.2)}" if bottom else str(margin)
        inputs.append(["-loop", "1", "-framerate", "30",
                       "-t", f"{plan.new_duration:.3f}",
                       "-i", os.path.abspath(opts.logo)])
        pic, out = label(), label()
        chains.append(
            f"[{idx}:v]scale={box}:{box}:force_original_aspect_ratio=decrease,"
            f"format=rgba,colorchannelmixer=aa={brand_opacity()}[{pic}]")
        chains.append(f"[{cur}][{pic}]overlay=x={x}:y={y}:eof_action=pass[{out}]")
        cur = out
        idx += 1

    if ass_name:
        out = label()
        opt = f"subtitles={ass_name}" + (f":fontsdir={fonts_dir}" if fonts_dir else "")
        chains.append(f"[{cur}]{opt}[{out}]")
        cur = out

    if cur == "0:v":
        return "", inputs, audio_cut, cur
    return ";".join(chains), inputs, audio_cut, cur


def brand_size() -> float:
    from . import brand
    return brand.SIZE


def brand_opacity() -> float:
    from . import brand
    return brand.OPACITY


def render(plan: Plan, opts: Options) -> Dict:
    """Encode the plan over the clip, in place. Returns what was done.

    Raises on an encode failure; polish() is the caller that keeps that from
    reaching the clip.
    """
    work = tempfile.mkdtemp(prefix="clipmint-edit-")
    try:
        ass_name = fonts = None
        style = opts.captions
        if style != captions.OFF and plan.caption_words:
            y = captions.caption_y(opts.layout, opts.aspect_ratio, opts.cam_panel_fraction)
            script = captions.build_ass(plan.caption_words, style, plan.width, plan.height, y)
            if script:
                with open(os.path.join(work, "captions.ass"), "w", encoding="utf-8") as f:
                    f.write(script)
                ass_name = "captions.ass"
                if captions.copy_font(style, Path(work) / "fonts"):
                    fonts = "fonts"

        graph, extra, audio_cut, video = build_filter(plan, opts, ass_name, fonts)
        if not graph:
            return {}

        # ffmpeg runs inside the work folder, so every path it is handed
        # has to stand on its own.
        src = os.path.abspath(plan.path)
        root, ext = os.path.splitext(src)
        tmp = f"{root}.edit{ext or '.mp4'}"
        if plan.has_audio and audio_cut:
            audio = ["-map", "[acut]", "-c:a", "aac", "-b:a", "160k"]
        elif plan.has_audio:
            audio = ["-map", "0:a:0", "-c:a", "copy"]
        else:
            audio = []

        def build(enc):
            cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", src]
            for args in extra:
                cmd += args
            return cmd + ["-filter_complex", graph, "-map", f"[{video}]", *audio, *enc,
                          "-movflags", "+faststart", tmp]

        try:
            accel.run_encode(build, what="ffmpeg (captions and edit)", crf=20, cwd=work)
            os.replace(tmp, src)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    finally:
        shutil.rmtree(work, ignore_errors=True)

    removed = plan.duration - plan.new_duration if plan.cut else 0.0
    return {
        "captions": style if ass_name else captions.OFF,
        "cuts": max(0, len(plan.keeps) - 1) if plan.cut else 0,
        "removed_seconds": round(removed, 2),
        "punch_ins": sum(1 for _, _, z in plan.zooms if z == _PUNCH_ZOOM),
        "emoji": [name for _, _, name in plan.emoji],
        "broll": [b["query"] for b in plan.broll],
        "keeps": [[a, b] for a, b in plan.keeps] if plan.cut else None,
        "logo": bool(opts.logo),
    }


def polish(results: List[Dict], opts: Options, language: Optional[str] = None,
           llm_fn: Optional[Callable[[str], str]] = None) -> None:
    """Edit every rendered clip in `results`, in place.

    Sets `edit` on each result it touched (what was done) and moves
    `hook_peak_offset` onto the new timeline, so the cold open that is added
    afterwards still opens on the right moment. A clip that cannot be edited
    keeps its plain render and says why in the log.
    """
    if not opts.anything:
        return
    from . import words as words_mod

    todo = [r for r in results if r.get("clip_url") and os.path.exists(r["clip_url"])]
    # Two passes over the same clips -- listen to every one, then encode every
    # one -- counted as one run of 2n steps, so the progress bar moves
    # through both instead of filling once and then sitting still.
    steps = 2 * len(todo)
    plans: List[Tuple[Dict, Plan]] = []
    try:
        for i, r in enumerate(todo, 1):
            print(f"[edit] {i}/{steps}: listening to "
                  f"{os.path.basename(r['clip_url'])}", flush=True)
            plan = analyse(r["clip_url"], r, opts, language)
            if plan is not None:
                plans.append((r, plan))
    finally:
        # Every clip has been listened to; the model is no longer needed and
        # is a few hundred megabytes of memory.
        words_mod.release()

    if opts.broll and llm_fn is not None and plans:
        _plan_broll(plans, llm_fn)

    for i, (r, plan) in enumerate(plans, 1):
        name = os.path.basename(plan.path)
        try:
            print(f"[edit] {len(todo) + i}/{steps}: editing {name}", flush=True)
            info = render(plan, opts)
        except Exception as e:  # noqa: BLE001 - a plain clip beats no clip
            first = (str(e).strip().splitlines() or [e.__class__.__name__])[-1][:200]
            print(f"[edit] could not edit {name} ({first}) - keeping the plain cut",
                  flush=True)
            continue
        if not info:
            continue
        r["edit"] = info
        # Kept with the clip, so its captions can be corrected later and
        # burned again without listening to it a second time.
        if plan.heard:
            r["heard_words"] = plan.heard
        bits = []
        if info["captions"] != captions.OFF:
            bits.append(f"{info['captions']} captions")
        if info["cuts"]:
            bits.append(f"{info['cuts']} pause(s) cut, {info['removed_seconds']:.1f}s shorter")
        if info["punch_ins"]:
            bits.append(f"{info['punch_ins']} punch-in(s)")
        if info["emoji"]:
            bits.append("emoji " + " ".join(info["emoji"]))
        if info["broll"]:
            bits.append("B-roll: " + ", ".join(info["broll"]))
        if info["logo"]:
            bits.append("logo")
        print(f"[edit] {name}: " + ("; ".join(bits) or "nothing to change"), flush=True)
        if plan.cut:
            peak, start = r.get("hook_peak"), r.get("start_time")
            if peak is not None and start is not None:
                r["hook_peak_offset"] = round(remap(float(peak) - float(start),
                                                    plan.keeps), 3)


def _plan_broll(plans: List[Tuple[Dict, Plan]], llm_fn) -> None:
    from . import broll

    picks = broll.pick_moments(
        [{"words": p.words, "duration": p.new_duration} for _, p in plans], llm_fn)
    used: set = set()
    for i, moments in picks.items():
        plan = plans[i][1]
        for m in moments:
            path = broll.fetch(m["query"], m["seconds"], plan.width, plan.height, used)
            if path:
                plan.broll.append({**m, "path": path})
