"""Natural-language layout control.

Turns a free-text instruction ("webcam bigger, top right, make it square")
into a validated LayoutSpec the renderers understand.

Two-stage parsing on purpose:

  1. A deterministic keyword pass that handles the phrasings people actually
     type. Costs nothing, works offline, and never burns LLM quota.
  2. An optional LLM pass for anything the keyword pass didn't resolve.

The keyword pass runs first and its results win, so a prompt like
"square, webcam top right" never needs a network call at all. The LLM is
only consulted when the prompt said something the keywords missed.
"""
from dataclasses import asdict, dataclass, field, fields as dataclass_fields
from typing import Dict, List, Optional
import json
import re

from . import captions as caption_styles
from . import content_kinds

# aspect ratio -> (width, height). Values are the platform-native upload sizes,
# used as the floor. When the source can support more, pick_output_size() moves
# up the ladder for that ratio instead.
ASPECT_PRESETS: Dict[str, tuple] = {
    "9:16": (1080, 1920),   # Shorts / Reels / TikTok
    "4:5": (1080, 1350),    # Instagram feed portrait
    "1:1": (1080, 1080),    # square
    "16:9": (1920, 1080),   # landscape
}

# Standard sizes per ratio, smallest first. We climb these while the source
# still has the pixels to justify it — never past what the crop actually holds.
QUALITY_LADDER: Dict[str, List[tuple]] = {
    "9:16": [(1080, 1920), (1440, 2560), (2160, 3840)],
    "4:5": [(1080, 1350), (1440, 1800), (2160, 2700)],
    "1:1": [(1080, 1080), (1440, 1440), (2160, 2160)],
    "16:9": [(1920, 1080), (2560, 1440), (3840, 2160)],
}


def pick_output_size(src_w: int, src_h: int, aspect_ratio: str,
                     layout: str = "stacked") -> tuple:
    """Largest standard size the source genuinely supports for this ratio.

    Upscaling past what the source holds only inflates the file — it adds no
    detail. So we measure the crop we're actually going to take, then climb the
    ladder as far as that crop's real pixels justify, and no further.
    """
    ladder = QUALITY_LADDER.get(aspect_ratio, QUALITY_LADDER["9:16"])
    if src_w <= 0 or src_h <= 0:
        return ladder[0]

    base_w, base_h = ladder[0]
    target = base_w / base_h

    # The gameplay/centre crop is the panel that carries the detail. In the
    # stacked layout it only occupies the lower portion of the output.
    from_h = src_h
    from_w = min(int(from_h * target), src_w)
    if layout == "stacked":
        # The bottom panel is ~58% of output height but still sourced from the
        # full frame height, so it has proportionally more pixels to give.
        from_w = min(int(src_h * (base_w / (base_h * 0.58))), src_w)

    best = ladder[0]
    for w, h in ladder:
        # Allow a standard size if the crop supplies at least ~85% of its
        # width; below that we would be inventing pixels.
        if from_w >= w * 0.85:
            best = (w, h)
        else:
            break
    return best

LAYOUTS = ("stacked", "facetrack", "center")
# The LayoutSpec fields that describe the edit rather than the framing.
EDIT_FIELDS = ("captions", "cut_pauses", "punch_ins", "emoji", "broll")
CORNERS = ("bottom-left", "bottom-right", "top-left", "top-right")


@dataclass
class LayoutSpec:
    """Everything the renderer needs to know about framing."""

    layout: str = "stacked"
    aspect_ratio: str = "9:16"
    webcam_corner: str = "bottom-left"
    cam_panel_fraction: float = 0.42
    face_zoom: float = 5.0
    # Ten is the ceiling and the default: one run should hand back a full
    # week's posting slate, ranked, rather than three clips and another wait.
    num_clips: int = 10
    # Render at the best size the source actually supports rather than the
    # platform-minimum preset. Off means always use ASPECT_PRESETS.
    match_source_quality: bool = True
    # Explicit [start, end] spans in seconds. When present, the pipeline cuts
    # exactly these and skips transcription and AI ranking entirely.
    time_ranges: List[List[float]] = field(default_factory=list)
    # How long each clip should run, as [min, max] seconds. None leaves the
    # ranker on its own 30-60s default. This is a request to the model, not a
    # trim: cutting a clip to length afterwards would slice mid-sentence, so
    # the length has to be part of what it is asked to find.
    clip_seconds: Optional[List[float]] = None
    # Open every clip with a second of its own loudest moment before playing
    # it in full. Only does anything when the payoff is genuinely late in the
    # clip -- see hook_open -- so leaving it on costs nothing on the clips
    # that already open on their hook.
    hook_replay: bool = True
    # What KIND of short to look for, in the user's own words — the angle, the
    # hook, the mood. Handed to the ranker verbatim; the renderer never reads
    # it. Kept whole rather than parsed, because the value is in the nuance a
    # keyword pass would throw away.
    brief: str = ""
    # What kind of video this is -- stream, vlog, podcast, tutorial or other,
    # see content_kinds. "auto" leaves it to the ranker to work out; anything
    # else is the user saying so, and decides what counts as a good moment.
    content_kind: str = content_kinds.AUTO
    # Whether the framing came from the user's words, rather than the default.
    # Only a default framing is the kind of video's to change: someone who
    # typed "webcam at the top" over a vlog meant it.
    layout_set: bool = False
    # The edit made after the cut -- see autoedit. Captions burned in, one
    # word lit at a time, in one of captions.PRESETS or "off".
    captions: str = caption_styles.DEFAULT
    # Take out quiet pauses and "um"s. A pause with sound in it is kept.
    cut_pauses: bool = True
    # Zoom in on the lines said with emphasis.
    punch_ins: bool = True
    # Pop an emoji over words that name a feeling or a thing.
    emoji: bool = False
    # Stock footage over lines that name something filmable. Needs a Pexels
    # key, and never runs on a stream.
    broll: bool = False
    # Which of the edit settings above the prompt's own words decided, so the
    # interface can show the words won rather than silently ignoring them.
    edit_from_words: List[str] = field(default_factory=list)
    # Human-readable notes about what the parser understood, shown in the UI
    # so the user can see their prompt was actually applied.
    notes: List[str] = field(default_factory=list)

    @property
    def width(self) -> int:
        return ASPECT_PRESETS.get(self.aspect_ratio, ASPECT_PRESETS["9:16"])[0]

    @property
    def height(self) -> int:
        return ASPECT_PRESETS.get(self.aspect_ratio, ASPECT_PRESETS["9:16"])[1]

    def validate(self) -> "LayoutSpec":
        """Clamp every field into a renderable range. Never raises."""
        if self.layout not in LAYOUTS:
            self.layout = "stacked"
        if self.aspect_ratio not in ASPECT_PRESETS:
            self.aspect_ratio = "9:16"
        if self.webcam_corner not in CORNERS:
            self.webcam_corner = "bottom-left"
        # Below ~0.15 the webcam is unreadable; above ~0.75 there's no gameplay left.
        self.cam_panel_fraction = max(0.15, min(0.75, float(self.cam_panel_fraction)))
        # Below 2x the crop is inside the face; above 12x the webcam is a speck.
        self.face_zoom = max(2.0, min(12.0, float(self.face_zoom)))
        self.num_clips = max(1, min(10, int(self.num_clips)))
        self.content_kind = content_kinds.normalise(self.content_kind)
        self.layout_set = bool(self.layout_set)
        self.captions = caption_styles.normalise(self.captions)
        for name in ("cut_pauses", "punch_ins", "emoji", "broll"):
            setattr(self, name, bool(getattr(self, name)))
        self.edit_from_words = [f for f in (self.edit_from_words or [])
                                if f in EDIT_FIELDS]

        # Keep only sane, ordered, non-duplicate spans.
        clean: List[List[float]] = []
        for r in self.time_ranges or []:
            try:
                start, end = float(r[0]), float(r[1])
            except (TypeError, ValueError, IndexError):
                continue
            if end <= start or start < 0:
                continue
            if [start, end] not in clean:
                clean.append([start, end])
        self.time_ranges = sorted(clean, key=lambda r: r[0])
        return self

    def to_dict(self) -> Dict:
        d = asdict(self)
        d["width"] = self.width
        d["height"] = self.height
        return d

    @classmethod
    def from_dict(cls, data: Optional[Dict]) -> "LayoutSpec":
        """Rebuild a spec from its own to_dict output.

        Used when a job is restored from disk, so re-cutting one of its clips
        months later still renders in the layout it was first made with.
        Unknown and missing keys fall back to the defaults; validate() then
        clamps whatever survived.
        """
        spec = cls()
        if not isinstance(data, dict):
            return spec.validate()
        # A run made before the app edited its clips. Re-cutting one of those
        # must give back the plain look it was made with, not a new one.
        if "captions" not in data:
            spec.captions = caption_styles.OFF
            spec.cut_pauses = spec.punch_ins = spec.emoji = spec.broll = False
        fields = {f.name for f in dataclass_fields(cls)}
        for key, value in data.items():
            if key in fields and value is not None:
                setattr(spec, key, value)
        return spec.validate()

    def warning(self) -> Optional[str]:
        """A caveat worth showing before the user commits to a long render."""
        # There used to be one for "follow my face", which rendered many times
        # slower than the others. Most of that turned out to be the clip cut
        # decoding the source from the start instead of seeking to it; with
        # that fixed it renders 30s of 720p60 in ~11s on a GPU and ~17s on a
        # CPU -- the same ballpark as the other layouts -- so warning about it
        # would only put people off the right layout for a face cam.
        return None

    def apply_kind(self, kind: Optional[str] = None) -> Optional[str]:
        """Frame the clips for a kind of video, unless the user chose a framing.

        `kind` defaults to the spec's own. Returns the layout it switched to,
        or None when nothing changed. See content_kinds.default_layout for
        which kinds move and why.
        """
        kind = content_kinds.normalise(kind if kind is not None else self.content_kind)
        wanted = content_kinds.default_layout(kind)
        if not wanted or self.layout_set or self.layout == wanted:
            return None
        self.layout = wanted
        return wanted

    def describe(self) -> str:
        """One-line human summary, for the UI and the logs."""
        if self.time_ranges:
            n = len(self.time_ranges)
            prefix = f"{n} exact cut{'s' if n > 1 else ''} · "
        else:
            # Say the count up front: it is the number of Shorts the run hands
            # back, and the one setting people most often want to change.
            prefix = f"{self.num_clips} clip{'s' if self.num_clips > 1 else ''} · "
        if self.clip_seconds:
            lo, hi = int(self.clip_seconds[0]), int(self.clip_seconds[1])
            prefix += f"{lo}-{hi}s each · "
        if self.hook_replay:
            prefix += "hook up front · "
        if self.content_kind != content_kinds.AUTO:
            prefix = f"{content_kinds.LABELS[self.content_kind]} · " + prefix
        edit = self.describe_edit()
        return prefix + self._describe_layout() + (f" · {edit}" if edit else "")

    def describe_edit(self) -> str:
        """The edit made after the cut, in a few words, or "" for none."""
        bits = []
        if self.captions != caption_styles.OFF:
            bits.append(f"{self.captions} captions")
        if self.cut_pauses:
            bits.append("pauses cut")
        if self.punch_ins:
            bits.append("punch-ins")
        if self.emoji:
            bits.append("emoji")
        if self.broll:
            bits.append("B-roll")
        return ", ".join(bits)

    def _describe_layout(self) -> str:
        if self.layout == "stacked":
            pct = round(self.cam_panel_fraction * 100)
            where = self.webcam_corner.replace("-", " ")
            return (
                f"{self.aspect_ratio} · webcam on top at {pct}% height "
                f"(overlay found in the {where}) · gameplay below"
            )
        if self.layout == "facetrack":
            return f"{self.aspect_ratio} · single frame, crop follows the face"
        return f"{self.aspect_ratio} · single frame, centre crop"


# --- stage 1: deterministic keyword parsing -------------------------------

# Order matters: explicit ratios and unambiguous words are matched before the
# loose platform names. "shorts" means 9:16 in "make it for shorts" but means
# "clips" in "5 shorts" — the clip-count phrase is stripped before we look.
_ASPECT_WORDS = [
    (r"\b16[:\s/]?9\b|\blandscape\b|\bhorizontal\b|\bwidescreen\b", "16:9"),
    (r"\b4[:\s/]?5\b|\binstagram feed\b|\bfeed post\b", "4:5"),
    (r"\b1[:\s/]?1\b|\bsquare\b", "1:1"),
    (r"\b9[:\s/]?16\b|\bvertical\b|\bportrait\b|\breels?\b|\btiktok\b|\bshorts?\b", "9:16"),
]

# Words that say what kind of video the source is.
_KIND_WORDS = [
    (r"\b(?:live ?)?streams?\b|\bvods?\b|\bgameplay\b|\blet'?s ?plays?\b"
     r"|\bplaythrough\b|\bgaming\b|\btwitch\b", content_kinds.STREAM),
    (r"\bvlogs?\b|\birl\b|\bday in (?:the|my) life\b|\btravel video\b", content_kinds.VLOG),
    (r"\bpodcasts?\b|\binterviews?\b|\bdebate\b", content_kinds.PODCAST),
    (r"\btutorials?\b|\bhow[\s-]?to\b|\bexplainer\b|\blecture\b", content_kinds.TUTORIAL),
]

_CORNER_WORDS = [
    (r"bottom[\s-]*left|lower[\s-]*left", "bottom-left"),
    (r"bottom[\s-]*right|lower[\s-]*right", "bottom-right"),
    (r"top[\s-]*left|upper[\s-]*left", "top-left"),
    (r"top[\s-]*right|upper[\s-]*right", "top-right"),
]


def _to_seconds(token: str) -> Optional[float]:
    """'1:30' -> 90, '01:02:03' -> 3723, '90s' -> 90, '90' -> 90."""
    token = re.sub(r"(?:seconds|secs|sec|s)\s*$", "", token.strip()).strip()
    if not token:
        return None
    if ":" in token:
        parts = token.split(":")
        if len(parts) > 3 or any(not p.strip().isdigit() for p in parts):
            return None
        nums = [int(p) for p in parts]
        while len(nums) < 3:
            nums.insert(0, 0)
        h, m, s = nums
        if m > 59 or s > 59:
            return None
        return h * 3600 + m * 60 + s
    try:
        return float(token)
    except ValueError:
        return None


# A timestamp is either clock form (1:30, 00:01:30) or an explicit seconds
# value (90s). Bare numbers are only accepted inside "from X to Y", because
# on their own they collide with everything from percentages to resolutions.
# The trailing \b matters: without it "928 square" parses as "928 s" and eats
# the leading letter of the next word.
_TS = r"(?:\d{1,2}:\d{2}(?::\d{2})?|\d{1,5}\s*(?:s|sec|secs|seconds)\b)"

_RANGE_PATTERNS = [
    # "from 1:30 to 2:45" / "between 90s and 150s" — explicit, so bare numbers are safe here
    rf"\b(?:from|between)\s+({_TS}|\d{{1,5}})\s*(?:to|-|–|—|and|until)\s*({_TS}|\d{{1,5}})",
    # "1:30-2:45", "1:30 to 2:45", "90s - 150s"
    rf"({_TS})\s*(?:to|-|–|—|until)\s*({_TS})",
]


def _parse_time_ranges(p: str, spec: LayoutSpec) -> tuple:
    """Pull explicit clip spans out of the prompt.

    Returns (remaining_text, found) — the matched spans are stripped so the
    rest of the parser can't misread "9:16" style leftovers.
    """
    found = []
    for pattern in _RANGE_PATTERNS:
        spans = []
        for m in re.finditer(pattern, p):
            a, b = _to_seconds(m.group(1)), _to_seconds(m.group(2))
            if a is None or b is None or b <= a:
                continue
            found.append([a, b])
            spans.append((m.start(), m.end()))
        # Cut from the back. Every match's offsets were measured on the text
        # before anything was removed, so cutting front-to-back shifted each
        # later cut onto the wrong characters -- with five spans it ate
        # "gameplay only" and left digits that read as a 4:5 request.
        for start, end in reversed(spans):
            p = p[:start] + " " + p[end:]
        if found:
            break

    if found:
        spec.time_ranges = found
        for a, b in found:
            spec.notes.append(
                f"exact cut → {_fmt_ts(a)} to {_fmt_ts(b)} ({b - a:.0f}s)"
            )
    return p, bool(found)


def _fmt_ts(seconds: float) -> str:
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


# Clip length, longest-reaching patterns first so "between 20 and 40 seconds"
# is not eaten by the bare "40 seconds" rule. Each returns (min, max).
_LENGTH_PATTERNS = [
    # a range: "20-40s", "between 20 and 40 seconds"
    (r"\b(?:between\s+)?(\d{1,3})\s*(?:-|–|to|and)\s*(\d{1,3})\s*(?:s\b|secs?\b|seconds?\b)",
     lambda m: (float(m.group(1)), float(m.group(2)))),
    # an upper bound: "under 45s", "max 40 seconds", "no longer than 60s"
    (r"\b(?:under|below|less than|at most|max(?:imum)?|no (?:longer|more) than)\s+"
     r"(\d{1,3})\s*(?:s\b|secs?\b|seconds?\b)",
     lambda m: (max(10.0, float(m.group(1)) * 0.6), float(m.group(1)))),
    # a lower bound: "at least 30 seconds", "over 30s"
    (r"\b(?:over|above|at least|min(?:imum)?|longer than|more than)\s+"
     r"(\d{1,3})\s*(?:s\b|secs?\b|seconds?\b)",
     lambda m: (float(m.group(1)), float(m.group(1)) * 1.5)),
    # about a minute
    (r"\b(?:about|around|roughly|~)?\s*(?:a|one)\s+minute\b",
     lambda m: (50.0, 70.0)),
    # a flat target: "30 seconds only", "30s clips", "30 second shorts"
    (r"\b(\d{1,3})\s*(?:s\b|secs?\b|seconds?\b)",
     lambda m: (float(m.group(1)) * 0.85, float(m.group(1)) * 1.15)),
]


def _parse_clip_length(p: str, spec: LayoutSpec) -> tuple:
    """Read a requested clip length and strip the phrase that said it.

    Runs before the time-range pass, because "between 20 and 40 seconds" is a
    length and reads identically to a span. Getting that order wrong turned a
    request for 20-40s clips into a single exact cut from 0:20 to 0:40, with
    ranking skipped altogether.

    "cut" is this app's explicit span verb, and a prompt using it wants exact
    spans — which bypass ranking, making a clip length meaningless. So that
    word hands the whole phrase back to the range parser.
    """
    if re.search(r"\bcut\b", p):
        return p, False

    for pattern, to_range in _LENGTH_PATTERNS:
        m = re.search(pattern, p)
        if not m:
            continue
        lo, hi = to_range(m)
        lo, hi = round(min(lo, hi)), round(max(lo, hi))
        # Under ~5s there is no clip; over MAX_CLIP_SECONDS the ranker's own
        # sanitiser would throw the result away, so promising it is a lie.
        lo = max(5, min(lo, 240))
        hi = max(lo + 1, min(hi, 240))
        spec.clip_seconds = [float(lo), float(hi)]
        spec.notes.append(f"clip length → {lo}-{hi}s")
        return p[:m.start()] + " " + p[m.end():], True
    return p, False


_CAPTION_WORD = r"(?:captions?|subtitles?|subs)"
_STYLE_WORD = "(" + "|".join(caption_styles.PRESETS) + ")"

# Each is (pattern, field, value). Matched first and cut out of the prompt,
# because the phrases are full of words the framing rules also read: "cut the
# pauses" would otherwise switch on the exact-span parser, and "zoom in on
# emphasis" would tighten the webcam crop.
_EDIT_PHRASES = [
    (rf"\b(?:no|without|turn off|remove the|hide the)\s+{_CAPTION_WORD}\b"
     rf"|\b{_CAPTION_WORD}\s+off\b", "captions", caption_styles.OFF),
    (rf"\b{_STYLE_WORD}\s+{_CAPTION_WORD}\b", "captions", None),
    (rf"\b{_CAPTION_WORD}\s+(?:in\s+)?{_STYLE_WORD}\b", "captions", None),
    (rf"\b(?:add|with|burn in|burned in|show)\s+{_CAPTION_WORD}\b", "captions", "on"),
    (r"\bkeep (?:the |my )?(?:pauses|silences?|ums?)\b|\bno jump ?cuts\b"
     r"|\bdon'?t cut (?:the |my )?(?:pauses|silences?)\b", "cut_pauses", False),
    (r"\b(?:cut|remove|trim|take out) (?:the |all the )?(?:pauses|silences?|dead air"
     r"|filler(?: words)?|ums?)\b|\bjump ?cuts\b", "cut_pauses", True),
    (r"\bno (?:zooms?|punch[\s-]?ins?)\b", "punch_ins", False),
    (r"\bpunch[\s-]?ins?\b|\bzoom(?:s|ing)? on (?:the )?emphasis\b", "punch_ins", True),
    (r"\b(?:no|without) emojis?\b", "emoji", False),
    (r"\bemojis?\b", "emoji", True),
    (r"\b(?:no|without) (?:b[\s-]?roll|stock footage)\b", "broll", False),
    (r"\bb[\s-]?roll\b|\bstock footage\b", "broll", True),
]


def _parse_edit(p: str, spec: LayoutSpec) -> tuple:
    """Read the edit settings out of the prompt. Returns (rest, fields set)."""
    found = set()
    for pattern, name, value in _EDIT_PHRASES:
        if name in found:
            continue
        m = re.search(pattern, p)
        if not m:
            continue
        if name == "captions":
            if value is None:
                value = m.group(1)
            elif value == "on":
                value = (spec.captions if spec.captions != caption_styles.OFF
                         else caption_styles.DEFAULT)
            spec.captions = value
            spec.notes.append("captions → off" if value == caption_styles.OFF
                              else f"captions → {value}")
        else:
            setattr(spec, name, value)
            label = {"cut_pauses": "cut pauses and fillers", "punch_ins": "punch-ins",
                     "emoji": "emoji", "broll": "B-roll"}[name]
            spec.notes.append(f"{label} → {'on' if value else 'off'}")
        found.add(name)
        p = p[:m.start()] + " " + p[m.end():]
    spec.edit_from_words = sorted(found)
    return p, found


def _parse_keywords(prompt: str, spec: LayoutSpec) -> set:
    """Apply what we can read directly. Returns the set of fields we resolved."""
    p = prompt.lower()
    resolved = set()

    p, edits = _parse_edit(p, spec)
    resolved |= edits

    # Clip length first — see _parse_clip_length for why it has to beat the
    # time-range pass. It also strips the phrase, so "45 second clips" cannot
    # have its number misread as a clip count.
    p, had_length = _parse_clip_length(p, spec)
    if had_length:
        resolved.add("clip_seconds")

    # Time ranges next: they're stripped from the text so a span like
    # "1:30-2:45" can never be mistaken for an aspect ratio later.
    p, had_ranges = _parse_time_ranges(p, spec)
    if had_ranges:
        resolved.add("time_ranges")

    # Clip count first, and strip the phrase so "5 shorts" can't also be read
    # as a request for 9:16.
    m = re.search(r"\b(\d{1,2})\s*(clips?|shorts?|videos?)\b", p)
    if m:
        spec.num_clips = int(m.group(1))
        spec.notes.append(f"clip count → {spec.num_clips}")
        resolved.add("num_clips")
        p = p[:m.start()] + " " + p[m.end():]

    # The cold open is on by default, so the only thing worth reading here is
    # someone asking for it to stop. Both directions are matched anyway: a
    # setting you can turn off in words and not back on again is a trap.
    if re.search(r"\bno hook (repeat|replay|intro)\b|\bdon'?t repeat the hook\b"
                 r"|\bwithout the hook (repeat|replay)\b|\bno cold open\b", p):
        spec.hook_replay = False
        spec.notes.append("hook replay → off, clips start straight in")
        resolved.add("hook_replay")
    elif re.search(r"\brepeat the hook\b|\bhook (first|up front|replay|loop)\b"
                   r"|\bcold open\b|\btease the hook\b", p):
        spec.hook_replay = True
        spec.notes.append("hook replay → on, each clip opens on its own peak")
        resolved.add("hook_replay")

    for pattern, value in _ASPECT_WORDS:
        if re.search(pattern, p):
            spec.aspect_ratio = value
            spec.notes.append(f"aspect ratio → {value}")
            resolved.add("aspect_ratio")
            break

    # Layout intent. Checked most-specific first.
    if re.search(r"\bno (web)?cam\b|\bwithout (a )?(web)?cam\b|\bhide (the )?(web)?cam\b"
                 r"|\bgameplay only\b|\bjust (the )?gameplay\b|\bno face\b", p):
        spec.layout = "center"
        spec.notes.append("layout → gameplay only, no webcam panel")
        resolved.add("layout")
    elif re.search(r"\bfollow (my |the )?face\b|\bface[\s-]*track\w*\b|\btrack (my |the )?face\b"
                   r"|\bkeep (my |the )?face cent\w+\b|\btalking head\b", p):
        spec.layout = "facetrack"
        spec.notes.append("layout → single frame, crop follows the face")
        resolved.add("layout")
    elif re.search(r"\bstack\w*\b|\b(web)?cam (on |at |to )?(the )?top\b|\btop\b.*\b(web)?cam\b"
                   r"|\b(web)?cam above\b|\bsplit\b|\bpicture[\s-]*in[\s-]*picture\b|\bpip\b", p):
        spec.layout = "stacked"
        spec.notes.append("layout → webcam on top, gameplay below")
        resolved.add("layout")

    if "layout" in resolved:
        spec.layout_set = True

    # What kind of video it is, when the words say so. Only an unambiguous
    # mention counts: "the interview bit of my stream" names two kinds, and
    # guessing between them would be worse than leaving it to the detector.
    kinds = {kind for pattern, kind in _KIND_WORDS if re.search(pattern, p)}
    if len(kinds) == 1:
        spec.content_kind = kinds.pop()
        spec.notes.append(f"kind of video → {content_kinds.LABELS[spec.content_kind]}")
        resolved.add("content_kind")

    # Where the webcam overlay physically sits in the SOURCE footage.
    for pattern, value in _CORNER_WORDS:
        if re.search(pattern, p):
            # "webcam on top" is a layout instruction, not a corner. Only read a
            # corner when the phrasing is actually about locating the overlay.
            if re.search(r"(web)?cam|overlay|face", p):
                spec.webcam_corner = value
                spec.notes.append(f"looking for the webcam overlay in the {value.replace('-', ' ')}")
                resolved.add("webcam_corner")
            break

    # Panel size.
    if re.search(r"\b(bigger|larger|big|huge|more)\b.*\b(web)?cam\b|\b(web)?cam\b.*\b(bigger|larger|big|huge|more)\b", p):
        spec.cam_panel_fraction = 0.55
        spec.notes.append("webcam panel → larger (55% of height)")
        resolved.add("cam_panel_fraction")
    elif re.search(r"\b(smaller|small|tiny|less|shrink)\b.*\b(web)?cam\b|\b(web)?cam\b.*\b(smaller|small|tiny|less|shrink)\b", p):
        spec.cam_panel_fraction = 0.30
        spec.notes.append("webcam panel → smaller (30% of height)")
        resolved.add("cam_panel_fraction")

    # Explicit percentage, e.g. "webcam 60%".
    m = re.search(r"(\d{2})\s*%", p)
    if m:
        spec.cam_panel_fraction = int(m.group(1)) / 100.0
        spec.notes.append(f"webcam panel → {m.group(1)}% of height")
        resolved.add("cam_panel_fraction")

    # Face framing tightness.
    if re.search(r"\b(zoom|closer|tighter|tight|close up|closeup)\b", p):
        spec.face_zoom = 3.0
        spec.notes.append("webcam framing → tighter on the face")
        resolved.add("face_zoom")
    elif re.search(r"\b(wider|zoom out|pull back|more room|wide)\b", p):
        spec.face_zoom = 7.5
        spec.notes.append("webcam framing → wider")
        resolved.add("face_zoom")

    return resolved


# --- stage 2: LLM fallback ------------------------------------------------

_LLM_PROMPT = """You convert a video-editing instruction into JSON config.

Fields (omit any the instruction does not mention):
- "layout": "stacked" (webcam panel on top, gameplay below) | "facetrack" (one frame, crop follows the speaker's face) | "center" (one frame, plain centre crop, no webcam)
- "aspect_ratio": "9:16" | "4:5" | "1:1" | "16:9"
- "webcam_corner": where the webcam overlay sits in the ORIGINAL footage: "bottom-left" | "bottom-right" | "top-left" | "top-right"
- "cam_panel_fraction": 0.15-0.75, how much output height the webcam panel gets
- "face_zoom": 2.0-12.0, crop width as a multiple of face width (lower = tighter)
- "num_clips": 1-10

Respond with ONLY a JSON object. No markdown, no explanation.

Instruction: {prompt}"""


def _parse_with_llm(prompt: str, spec: LayoutSpec, already: set) -> None:
    """Ask the configured LLM about whatever the keyword pass didn't catch."""
    from .local.llm import call_local_llm

    raw = call_local_llm(_LLM_PROMPT.format(prompt=prompt))
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in LLM reply: {raw[:200]}")
    data = json.loads(text[start:end + 1])

    # Keyword results win — they came from the user's literal words.
    for key in ("layout", "aspect_ratio", "webcam_corner", "cam_panel_fraction",
                "face_zoom", "num_clips"):
        if key in already or key not in data or data[key] is None:
            continue
        setattr(spec, key, data[key])
        spec.notes.append(f"{key.replace('_', ' ')} → {data[key]} (interpreted)")
        if key == "layout":
            spec.layout_set = True


def parse_layout_prompt(
    prompt: Optional[str],
    base: Optional[LayoutSpec] = None,
    use_llm: bool = True,
) -> LayoutSpec:
    """Build a LayoutSpec from free text. Always returns something renderable.

    Args:
        prompt: free-text instruction. Empty/None returns the defaults.
        base: spec to start from, so UI controls can seed the prompt parse.
        use_llm: consult the LLM for anything keywords missed.
    """
    spec = base or LayoutSpec()
    spec.notes = []

    if not prompt or not prompt.strip():
        spec.notes.append("no layout prompt — using defaults")
        return spec.validate()

    # The whole prompt doubles as editorial direction for the ranker. Framing
    # words in it are harmless noise there, and trying to subtract them would
    # cost the very nuance ("only the rage moments", "hooks that ask a
    # question") that makes the brief worth having.
    spec.brief = prompt.strip()

    resolved = _parse_keywords(prompt, spec)

    # Only pay for an LLM call when the keyword pass understood nothing at all.
    # Anything it did resolve came from the user's literal words, so a second
    # opinion adds latency (and quota) without adding accuracy — and this runs
    # on every keystroke behind the live preview.
    if use_llm and not resolved:
        try:
            _parse_with_llm(prompt, spec, resolved)
        except Exception as e:
            # A layout prompt is a convenience, never a hard dependency —
            # falling back to defaults beats failing the whole render.
            spec.notes.append(f"could not interpret the rest of the prompt ({e}); kept defaults")

    if spec.apply_kind():
        spec.notes.append(f"framing → follows the face, for a {content_kinds.LABELS[spec.content_kind]}")

    if not spec.notes:
        spec.notes.append("nothing recognised in the prompt — using defaults")

    return spec.validate()
