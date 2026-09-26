"""Find the most viral-worthy highlights in a transcript.

Logic ported from ViralVadoo's transcript_analysis/highlight_generator.py:
  - content-type / density detection
  - chunking for long videos with overlap
  - virality-criteria prompt
  - score-based dedupe with overlap suppression

The LLM call is pluggable via the `llm_fn` argument so the same prompts can
drive either MuAPI (default, --mode api) or a direct local LLM client
(--mode local).

One thing here is not ported and is worth stating plainly, because it is the
difference between a clip that gets shown and one that does not: a highlight is
ranked on where it OPENS, not only on how good its best moment is. Short-form
distribution is decided in the first second - the early drop-off is read as a
verdict on the whole clip - so a moment that needs eight seconds of setup before
it pays off is worth less than a weaker moment that opens cold on its own hook.
The model scores both, and `score` is the blend the rest of the app sorts on.
"""
import json
import re
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import boundaries, content_kinds, muapi, signals
from .signals import AudioTrack


LLMFn = Callable[[str], str]


CONTENT_TYPE_PROMPT = """Analyze this video and classify the content type.
Choose one:
- gaming_stream: a live stream or recording of someone playing a video game, usually talking over it
- just_chatting: a streamer talking to chat with no game being played
- vlog: someone filming their own day, a trip, an event or a challenge, talking to camera
- podcast: two or more people in a long conversation
- interview: one person questioning another
- debate: people arguing opposing sides
- tutorial: showing how to do something, step by step
- lecture: teaching or explaining a topic
- commentary: one person reacting to or discussing something they are watching or reading
- other: none of these
The transcript samples come from the start, middle and end of the video, and the
listing is the video's own title and description. Game audio (narration, character
dialogue) mixed in with someone's commentary is a strong sign of gaming_stream.
Also estimate content density: low (mostly filler/chit-chat), medium, or high (dense info/stories).
Respond with JSON only: {"content_type": "...", "density": "..."}"""


VIRALITY_CRITERIA = """
Virality signals to prioritize (ranked by impact):
1. HOOK MOMENTS — statements that create immediate curiosity ("The secret is...", "Nobody talks about...", "I was completely wrong about...")
2. EMOTIONAL PEAKS — genuine surprise, laughter, anger, vulnerability, excitement; raw unscripted reactions
3. OPINION BOMBS — strong, polarizing or counter-intuitive statements that trigger agree/disagree
4. REVELATION MOMENTS — surprising facts, stats, or confessions that reframe how the viewer thinks
5. CONFLICT/TENSION — disagreement, pushback, or a problem being confronted head-on
6. QUOTABLE ONE-LINERS — a sentence that works as a standalone quote card
7. STORY PEAKS — the climax or twist of an anecdote; the payoff moment
8. PRACTICAL VALUE — a concrete tip, hack, or insight the viewer can immediately apply
"""


STREAM_VIRALITY_CRITERIA = """
This transcript is a raw LIVE STREAM VOD or gameplay recording of someone
playing a video game - often a story-heavy one. The audio is a single mixed
track: the streamer's microphone AND the game's own scripted narration/dialogue
are transcribed together, with no labels.

Telling them apart:
- GAME NARRATION reads like written prose — literary, past tense, polished, no
  filler words, no self-correction, never addresses anyone directly.
- THE STREAMER sounds spoken — reactions, filler words, false starts, laughter,
  swearing, questions, addressing chat, commenting on what just happened.

HARD REQUIREMENT: every highlight MUST contain the streamer's own speech.
A clip of pure game narration is worthless — that footage belongs to the game,
not the channel, and a clip with nothing of the creator in it is exactly the
reused content platforms hold back. A story beat is only clippable when the
streamer reacts to it, talks over it, or responds after it.

THE PAYOFF MUST BE ON SCREEN. This is the difference between a gaming Short
that reaches strangers and one that dies at ten views. The people a new Short
is tested on have never heard of this streamer, so the streamer's reaction is
worth nothing to them on its own — "what the hell was that?" is only
interesting when the viewer SEES what "that" was. Measured on a real channel:
clutches, physics chaos, a famous story twist and a game's scripted joke with
the streamer's response each reached ~1,000 viewers; reactions to things the
clip never showed, rage with no visible cause and chat talk averaged 8 views.

Virality signals to prioritize (ranked by impact):
1. VISIBLE PAYOFF + REACTION — something happens in the game that a stranger
   can see and understand with the sound off (a clutch, a multi-kill, a
   last-second death, a physics disaster, a jump scare that lands, a boss going
   down, a plan collapsing) and the streamer reacts to it in the same clip
2. FAMOUS STORY BEATS — the twist, death or ending that everyone who played
   the game remembers, with the streamer's live response to it. The game's name
   and the moment are searchable; the reaction is what makes it the channel's
3. THE GAME'S OWN JOKE, ANSWERED — a scripted line or event that is funny by
   itself, and the streamer's comeback to it
4. FAILS WITH A VISIBLE CAUSE — something goes wrong on screen, not just in
   the streamer's words, and you can see why
5. HOT TAKES & RANTS — a blunt opinion about a named game, mechanic or scene,
   when the subject is clear without the rest of the stream
6. SINCERITY — an unguarded, genuinely felt moment a stranger can follow

DEMOTE (score under 50 unless the frames would carry it anyway):
- A reaction to something the clip does not show: it happened before
  start_time, off-screen, or only in chat
- Rage, shouting or swearing with no visible cause. Swearing is not a hook;
  the thing that caused it is
- Chat reading, donations, subs, questions to chat, stream housekeeping
- Menus, inventories, map screens, puzzles being worked out, walking, driving,
  cutscenes the streamer is silent through, loading and death screens
- Anything that is only funny if you know the streamer or were watching live

Hard rules for stream VODs:
- SKIP dead air, loading screens, technical difficulties, and stream housekeeping
- SKIP anything requiring 10 minutes of prior context — it must land for a stranger
- If a span is entirely game narration with no streamer speech, DO NOT return it
- For every clip, say in "on_screen" what the viewer should SEE at the payoff.
  If you cannot say, because nothing visible happens, the clip is a reaction
  without a payoff: skip it or score it under 50
"""

VLOG_VIRALITY_CRITERIA = """
This transcript is a VLOG: someone filming their own day, a trip, an event or a
challenge, talking to camera as it happens. Friends, strangers, traffic and
music beds are often on the same track. The creator's own talk is what carries
the video.

HARD REQUIREMENT: every highlight MUST contain the creator speaking. A span
that is only music, crowd noise or other people cannot be judged from a
transcript, and a Short built on it has nobody for the viewer to follow.

Virality signals to prioritize (ranked by impact):
1. STORY PAYOFF — the twist, punchline or outcome of something that happened
2. RAW REACTIONS — surprise, fear, disbelief or laughter, as it happens
3. SOMETHING GOES WRONG — a plan falling apart, getting lost, a mishap on camera
4. FIRST TIME — trying, seeing or tasting something new, with the reaction to it
5. HONEST MOMENTS — a confession or something personal said straight to camera
6. STRONG OPINIONS — a blunt verdict on a place, a food, a person, a price
7. QUOTABLE ONE-LINERS — a line that works as a caption on its own
8. PRACTICAL VALUE — a cost, a tip, a warning a viewer could actually use

Hard rules for vlogs:
- SKIP greetings ("hey guys, welcome back"), subscribe asks, outros and sponsor reads
- SKIP travel filler: walking, driving, packing, narrating what is about to happen
- SKIP anything that needs the rest of the day explained first — it must land for a stranger
"""

PODCAST_VIRALITY_CRITERIA = """
This transcript is a PODCAST, INTERVIEW or DEBATE: two or more people in a long
conversation, with no speaker labels. Speakers change on questions, answers,
interruptions and agreement ("right", "exactly", "no, but").

Virality signals to prioritize (ranked by impact):
1. OPINION BOMBS — a strong, polarizing or counter-intuitive claim said with conviction
2. REVELATIONS & CONFESSIONS — a surprising fact, a number, or something personal admitted
3. DISAGREEMENT — pushback and the back-and-forth that follows, while it is still heated
4. STORY PEAKS — the climax or twist of an anecdote one of them is telling
5. BANTER — an exchange that gets a real laugh from the room
6. QUOTABLE ONE-LINERS — a sentence that works as a standalone quote card
7. PRACTICAL VALUE — a concrete insight or piece of advice a viewer can apply

Hard rules for conversations:
- An answer usually lands harder than the question. Open on the most arresting
  sentence of the answer, not the question, unless the question is the hook
- The clip must land for someone who has never heard of the guest or the show
- SKIP intros, guest bios, ad reads, "where can people find you", and housekeeping
- SKIP a point that is still being built up when the span ends — end on the payoff
"""

TUTORIAL_VIRALITY_CRITERIA = """
This transcript is a TUTORIAL or EXPLAINER: someone showing or teaching how
something works, often over a screen recording or a demonstration.

A tutorial Short is worth watching when it hands over ONE complete, useful
thing. A step that only makes sense after the three before it is not a Short.

Virality signals to prioritize (ranked by impact):
1. THE MISTAKE — a common error named, and the fix for it
2. ONE COMPLETE TIP — a trick, shortcut or setting that works on its own
3. SURPRISING RESULT — a before/after, or an outcome nobody expected
4. COUNTER-INTUITIVE CLAIM — "you have been doing this wrong", backed up in the clip
5. THE WHY — a short explanation that makes something finally make sense
6. QUOTABLE RULE — a rule of thumb that works as a standalone line

Hard rules for tutorials:
- The clip must be usable without the rest of the video: no "as we set up earlier"
- SKIP setup, installs, "link in the description", sponsor reads and recaps
- SKIP spans that only describe what is on screen ("click here, then here")
  without saying what it achieves
"""

COLD_OPEN_RULES = """
HOW A CLIP MUST START - this decides whether it gets shown to anyone at all:

A Short is judged in its first second. Around half of everyone who leaves is
gone inside three seconds, and the platform reads that early drop as "low value"
and stops distributing the clip, no matter how good second 20 is. So the opening
line is not the run-up to the clip. It IS the clip's audition.

- START ON THE HOOK, NOT THE RUN-UP. start_time goes on the first word of the
  most arresting line in the moment. Cut the throat-clear, the "so", the "okay
  so basically", the menu, the walking, the silence before the reaction.
- NO SETUP FIRST. If the interesting thing happens 8 seconds into a span, the
  clip starts at second 8 - not at second 0 with the context first. Either the
  context is implied by the moment, or the moment is not clippable.
- AT MOST ~1 SECOND OF RUNWAY, and only when the payoff is a sound rather than
  a sentence (a laugh, a scream, a gasp), where the instant before it lands is
  what makes the sound read.
- THE FIRST LINE MUST WORK ALONE. Read only the opening sentence, as a stranger
  who has never seen this video and knows nothing about it. If it does not
  create a question, a shock, or a laugh by itself, the clip starts in the wrong
  place - move start_time until it does.
- END ON THE PUNCH. end_time lands just after the payoff, never trailing into
  dead air, a topic change, or "anyway". A clip that ends flat loses the replay.
- A MOMENT THAT NEEDS A PREAMBLE IS NOT A HIGHLIGHT. If it cannot open cold,
  skip it and spend the slot on one that can.
"""

STRANGER_TEST = """
THE STRANGER TEST - every clip must pass it, whatever else was asked for:

A new Short is shown first to a small group of people who have never heard of
this creator. Only if they watch instead of swiping does it reach anyone else.
Picture one of them: no idea who is talking, no idea what happened earlier, a
thumb already moving. Ask of each clip:
- In the first three seconds, can they tell what is going on?
- Is there something to SEE, not only something to hear?
- Would they send it to a friend, or watch it twice?
A clip that only works for fans or for people who were there is not a Short,
however good it felt live. Returning fewer clips beats returning ones that fail
this: a separate check looks at each clip's frames before it is cut, and
anything whose "on_screen" claim the frames do not back up gets dropped.
"""


# Which criteria block gets injected into the highlight prompt, by the kind of
# video being ranked -- see content_kinds. Anything the detector cannot place
# gets the general criteria, which assume nothing about who is talking.
CRITERIA_BY_KIND = {
    content_kinds.STREAM: STREAM_VIRALITY_CRITERIA,
    content_kinds.VLOG: VLOG_VIRALITY_CRITERIA,
    content_kinds.PODCAST: PODCAST_VIRALITY_CRITERIA,
    content_kinds.TUTORIAL: TUTORIAL_VIRALITY_CRITERIA,
    content_kinds.OTHER: VIRALITY_CRITERIA,
}


HIGHLIGHT_SYSTEM_PROMPT = """You are an elite short-form video editor who has studied thousands of viral clips on TikTok, Instagram Reels, and YouTube Shorts. You know exactly what makes viewers stop scrolling, watch to the end, and share.

{virality_criteria}
{cold_open_rules}
{stranger_test}
Content type: {content_type} | Density: {density}
{user_brief}
Your task: identify the most viral-worthy highlights from the transcript.

Rules:
- {duration_rule}
- Never cut mid-sentence or mid-thought — each clip must feel complete and self-contained
- Clips must not overlap significantly with each other
- {num_clips_instruction}
- "first_line" is the exact transcript sentence the clip opens on, copied
  verbatim, word for word, from the transcript above. Write it out before you
  settle on start_time — if the line you are about to copy is filler, setup, or
  a neutral observation, then the clip starts in the wrong place and you must
  move start_time to a line that hooks. This line matters more than the number:
  the cut is placed by finding this exact line in the transcript, so a
  paraphrase, a summary, or a line you invented puts the clip in the wrong
  place entirely.
- "hook_sentence" is that same opening line
- Score each clip TWICE, 0-100, independently. USE THE WHOLE SCALE. Anchors:
    90-100  once or twice in an entire video. You would open the channel with it.
    70-89   strong. A clear reaction, line or tip a stranger would watch to the end.
    50-69   good in the moment, ordinary as a Short. Works for existing fans only.
    30-49   mildly interesting if you were there. Flat to a stranger.
    0-29    nothing really happens, or it cannot land without prior context.
  Most moments in any video sit between 30 and 60. A twenty-minute stretch
  containing eight 90s does not exist. If your scores land inside a ten-point
  band you have not ranked anything — you have only agreed with yourself, and
  the clips that get cut will be chosen at random from the tie. Spread them
  out, and let at most one clip in this chunk score above 90.
    "score" — viral potential of the moment as a whole
    "hook_score" — how hard "first_line" ALONE stops a scroll, judged as if you
      cannot see the rest of the clip. Setup, filler or a flat observation
      scores under 40 here however good the payoff is. Be harsh: this is the
      number that decides whether anybody ever reaches the payoff.
- Explain in one sentence why this clip is viral ("virality_reason")
- "on_screen": what a viewer should SEE at the payoff, in plain words ("the
  car flips over the barrier", "the host holds up the receipt"). "" when the
  moment is purely spoken, which is fine for a podcast and a warning sign for
  gameplay

Respond ONLY with valid JSON (no markdown, no explanation):
{{"highlights":[{{"title":"string","start_time":float,"end_time":float,"score":int,"hook_score":int,"first_line":"string","hook_sentence":"string","virality_reason":"string","on_screen":"string"}}]}}"""


# Bump whenever the ranking prompt -- or the shape of the transcript we hand
# it -- changes meaning. v3 widened each chunk's declared duration to cover its
# overlap tail, so chunks ranked under v2 were asked a narrower question. v4
# anchored the 0-100 scale: without anchors a real 96-candidate run came back
# spread over 73-95, so the top-five cut was being made on gaps smaller than
# the model's own noise. v5 made first_line load-bearing -- the cut is now
# placed by finding that line in the transcript rather than by trusting
# start_time -- so a chunk ranked under v4 was answering a question where the
# line was decoration. v6 chose the criteria by the kind of video instead of
# ranking everything as a game stream, and widened the score anchors from
# "stream" to "video". v7 put the payoff on screen ahead of the reaction to
# it, added the stranger test and the "on_screen" claim the visual check reads.
# Cached older chunks are not comparable and must be redone.
PROMPT_VERSION = 7
HOOK_SCORE_WEIGHT = 0.4       # how much the opening line counts toward the rank
MAX_CLIP_SECONDS = 90         # reject anything the model returns above this
CHUNK_SIZE_SECONDS = 1200       # 20-min chunks for long videos
LONG_VIDEO_THRESHOLD = 1800     # chunk videos longer than 30 min
CHUNK_OVERLAP_SECONDS = 60
GPT_CALL_TIMEOUT_SECONDS = 300  # cap LLM polls at 5 min — a wedged call should fail fast
MAX_HIGHLIGHT_API_ATTEMPTS = 3


def call_muapi_llm(prompt: str) -> str:
    """Default LLM backend: MuAPI gpt-5-mini."""
    result = muapi.run(
        "gpt-5-mini",
        {"prompt": prompt},
        label="gpt-5-mini",
        timeout=GPT_CALL_TIMEOUT_SECONDS,
    )

    outputs = result.get("outputs")
    if isinstance(outputs, list) and outputs and isinstance(outputs[0], str) and outputs[0].strip():
        return outputs[0]

    for key in ("output", "text", "response", "result", "content"):
        v = result.get(key)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, dict):
            inner = v.get("text") or v.get("content")
            if isinstance(inner, str) and inner.strip():
                return inner
        if isinstance(v, list) and v and isinstance(v[0], str):
            return v[0]

    raise RuntimeError(f"Could not extract gpt-5-mini text from response: {result}")


def _parse_json_loose(raw: str) -> Dict:
    """gpt-5-4 sometimes wraps JSON in markdown fences — strip and parse."""
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            return json.loads(text[start:end + 1])
        raise


def _coerce_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: object, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


DEFAULT_DURATION_RULE = (
    "Duration: TARGET 18-35 seconds. Completion rate is the signal that buys "
    "distribution, and the bar is stricter the longer the clip runs — a 20s "
    "clip watched to the end beats a 45s clip watched halfway, every time. "
    "Drop to 10-17s for a single perfect line. Go past 40s only when the payoff "
    "genuinely needs the room, and NEVER exceed 60 seconds"
)


def brief_block(brief: str) -> str:
    """The user's own description of the short they want, if they gave one.

    Placed above the task and marked as outranking the generic criteria: the
    house virality list is a good default, but someone who says "only the
    funny fails" has told us something the list cannot know.
    """
    brief = (brief or "").strip()
    if not brief:
        return ""
    return (
        "\nWHAT THE USER ASKED FOR (outranks the generic criteria above where "
        f"they disagree):\n\"{brief}\"\n"
        "Honour any editorial direction in it — the angle, the mood, the kind "
        "of moment, what the hook should do. Ignore any framing or layout "
        "instructions (webcam position, aspect ratio, clip count); those are "
        "handled elsewhere and are not your concern. The brief narrows WHICH "
        "moments to look for; it never lowers the bar. \"Only the rage "
        "moments\" means the rage moments that pass the stranger test, not "
        "every time the streamer swears.\n"
    )


def duration_rule(clip_seconds: Optional[List[float]]) -> str:
    """The length instruction, either the house default or what was asked for."""
    if not clip_seconds or len(clip_seconds) != 2:
        return DEFAULT_DURATION_RULE
    lo, hi = int(clip_seconds[0]), int(clip_seconds[1])
    return (
        f"Duration: every clip MUST run between {lo} and {hi} seconds. This is "
        f"a hard requirement the user asked for by name, not a preference. "
        f"Prefer a moment that is naturally this long over trimming a longer "
        f"one, and never end mid-sentence to hit the number"
    )


def _clip_ceiling(clip_seconds: Optional[List[float]]) -> float:
    """Longest clip to accept back from the model.

    A user who asks for 90-120s clips must not have every one of them thrown
    away by a limit they never saw, so an explicit request raises the ceiling.
    """
    if clip_seconds and len(clip_seconds) == 2:
        return max(float(MAX_CLIP_SECONDS), float(clip_seconds[1]) * 1.2)
    return float(MAX_CLIP_SECONDS)


def _sanitize_highlights(raw_highlights: object, duration: float,
                         clip_seconds: Optional[List[float]] = None) -> List[Dict]:
    """Normalize model output into the expected shape; skip invalid entries."""
    if not isinstance(raw_highlights, list):
        return []
    ceiling = _clip_ceiling(clip_seconds)

    max_end = duration if duration > 0 else float("inf")
    cleaned: List[Dict] = []
    for item in raw_highlights:
        if not isinstance(item, dict):
            continue

        start = _coerce_float(item.get("start_time"), default=-1.0)
        end = _coerce_float(item.get("end_time"), default=-1.0)
        if start < 0 or end <= start:
            continue

        if (end - start) > ceiling:
            continue

        if max_end != float("inf"):
            start = min(start, max_end)
            end = min(end, max_end)
            if end <= start:
                continue

        viral = max(0, min(100, _coerce_int(item.get("score"), default=0)))
        # A model that ignores the field should not be punished for it, so an
        # absent hook_score means "no opinion" rather than zero.
        hook = max(0, min(100, _coerce_int(item.get("hook_score"), default=viral)))

        cleaned.append(
            {
                "title": str(item.get("title") or "Untitled Highlight").strip(),
                "start_time": start,
                "end_time": end,
                # What every caller sorts and cuts on. A brilliant moment behind
                # a flat opening line is not a good Short, because nobody stays
                # long enough to reach it — so the opening line gets a real vote.
                "score": int(round(HOOK_SCORE_WEIGHT * hook
                                   + (1 - HOOK_SCORE_WEIGHT) * viral)),
                "viral_score": viral,
                "hook_score": hook,
                "first_line": str(item.get("first_line") or "").strip(),
                "hook_sentence": str(item.get("hook_sentence")
                                     or item.get("first_line") or "").strip(),
                "virality_reason": str(item.get("virality_reason") or "").strip(),
                "on_screen": str(item.get("on_screen") or "").strip()[:200],
            }
        )

    return cleaned


def _transcript_samples(segments: List[Dict], per_sample: int = 20) -> List[str]:
    """Three stretches of speech: the start, the middle and the end.

    The opening alone is the worst place to judge a video from. A stream opens
    on "starting soon" and chat hellos, a podcast on an ad read, a vlog on
    "hey guys" -- none of which says what the next two hours are.
    """
    if len(segments) <= per_sample * 3:
        return [" ".join(s["text"] for s in segments)]
    mid = len(segments) // 2 - per_sample // 2
    picks = (segments[:per_sample], segments[mid:mid + per_sample], segments[-per_sample:])
    return [" ".join(s["text"] for s in part) for part in picks]


def _listing_lines(video_meta: Optional[Dict]) -> str:
    meta = video_meta or {}
    lines = []
    if meta.get("title"):
        lines.append(f"Title: {meta['title']}")
    if meta.get("channel") or meta.get("uploader"):
        lines.append(f"Channel: {meta.get('channel') or meta.get('uploader')}")
    if meta.get("categories"):
        lines.append(f"Category: {', '.join(str(c) for c in meta['categories'][:3])}")
    if meta.get("description"):
        lines.append(f"Description: {str(meta['description'])[:400]}")
    return "\n".join(lines)


def detect_content_type(transcript: Dict, llm_fn: LLMFn = call_muapi_llm,
                        video_meta: Optional[Dict] = None) -> Dict[str, str]:
    """The detector's label for this video, its density, and the kind it folds to."""
    samples = _transcript_samples(transcript.get("segments", []))
    parts = [CONTENT_TYPE_PROMPT]
    listing = _listing_lines(video_meta)
    if listing:
        parts.append(f"Listing:\n{listing}")
    for name, text in zip(("start", "middle", "end"), samples):
        label = "Transcript sample" if len(samples) == 1 else f"Transcript sample ({name})"
        parts.append(f"{label}:\n{text[:1200]}")
    try:
        info = _parse_json_loose(llm_fn("\n\n".join(parts)))
        if not isinstance(info, dict):
            raise ValueError("not an object")
    except Exception:
        info = {"content_type": "other", "density": "medium"}
    info["kind"] = content_kinds.from_content_type(info.get("content_type"))
    return info


def resolve_content(transcript: Dict, llm_fn: LLMFn, kind: str = content_kinds.AUTO,
                    video_meta: Optional[Dict] = None,
                    saved: Optional[Dict] = None) -> Dict[str, str]:
    """What this video is, for ranking: detected, reused, or told.

    `saved` is the answer an earlier attempt at this same run already got. It
    is reused rather than asked again, because the detector is a model and can
    answer differently twice -- and a resumed run that suddenly ranks as a
    different kind of video has quietly thrown away the chunks it already paid
    for.

    A kind the user picked always wins over the detector. Detection still runs
    then, for the density, and so the log can say when the two disagree.
    """
    kind = content_kinds.normalise(kind)
    if saved and saved.get("kind") and (kind == content_kinds.AUTO or saved.get("kind") == kind):
        info = dict(saved)
    else:
        info = detect_content_type(transcript, llm_fn=llm_fn, video_meta=video_meta)
        if kind != content_kinds.AUTO:
            info["detected_kind"] = info.get("kind")
            info["kind"] = kind
    info["kind_source"] = "chosen" if kind != content_kinds.AUTO else "detected"
    return info


def build_transcript_text(transcript: Dict) -> str:
    segments = transcript.get("segments", [])
    return "\n".join(f"[{s['start']:.1f}s] {s['text'].strip()}" for s in segments)


def chunk_transcript(transcript: Dict) -> List[Dict]:
    segments = transcript.get("segments", [])
    duration = transcript.get("duration", segments[-1]["end"] if segments else 0)
    chunks = []
    start = 0
    while start < duration:
        end = min(start + CHUNK_SIZE_SECONDS, duration)
        # The window carries a tail of extra context past its own end, so a
        # moment straddling the boundary is still readable in full. That tail
        # has to count toward the chunk's declared duration as well: it is the
        # clamp bound _sanitize_highlights measures against, and leaving it at
        # `end - start` threw away every highlight the model found in the last
        # 60 seconds of each chunk -- silently, because clamping a span to
        # start == end just drops it.
        seg_end = min(end + CHUNK_OVERLAP_SECONDS, duration)
        chunk_segs = [
            s for s in segments
            if s["start"] >= start and s["end"] <= seg_end
        ]
        if chunk_segs:
            # Rebase segment times to the chunk so they match the relative
            # duration we pass as the clamp bound; get_highlights adds _offset
            # back afterwards. Without this, chunks after the first hand the
            # model absolute timestamps that then get clamped away entirely.
            chunk = dict(transcript)
            chunk["segments"] = [
                {**seg, "start": seg["start"] - start, "end": seg["end"] - start}
                for seg in chunk_segs
            ]
            chunk["duration"] = seg_end - start
            chunk["_offset"] = start
            chunks.append(chunk)
        start += CHUNK_SIZE_SECONDS - CHUNK_OVERLAP_SECONDS
    return chunks


def call_highlight_api(
    transcript_text: str,
    content_info: Dict,
    duration: float,
    num_clips: int,
    is_chunk: bool = False,
    llm_fn: LLMFn = call_muapi_llm,
    clip_seconds: Optional[List[float]] = None,
    brief: str = "",
) -> Dict:
    # Ask for ~2× the user's target so dedupe has headroom, but cap so the model
    # doesn't have to generate a huge JSON payload (which times out gpt-5-mini).
    target = max(num_clips * 2, 5)
    natural_max = max(2 if is_chunk else 3, int(duration / 90))
    min_clips = min(target, natural_max, 8)
    system = HIGHLIGHT_SYSTEM_PROMPT.format(
        virality_criteria=CRITERIA_BY_KIND.get(content_info.get("kind"), VIRALITY_CRITERIA),
        cold_open_rules=COLD_OPEN_RULES,
        stranger_test=STRANGER_TEST,
        content_type=content_info.get("content_type", "other"),
        density=content_info.get("density", "medium"),
        num_clips_instruction=f"Generate at least {min_clips} highlights",
        duration_rule=duration_rule(clip_seconds),
        user_brief=brief_block(brief),
    )
    base_prompt = f"{system}\n\nTranscript:\n{transcript_text}"
    prompt = base_prompt
    last_error = "unknown"

    for attempt in range(1, MAX_HIGHLIGHT_API_ATTEMPTS + 1):
        raw = llm_fn(prompt)
        try:
            parsed = _parse_json_loose(raw)
            highlights = _sanitize_highlights(parsed.get("highlights"), duration=duration,
                                              clip_seconds=clip_seconds)
            if highlights:
                return {"highlights": highlights}
            last_error = "no valid highlights in response"
        except Exception as e:
            last_error = str(e)

        if attempt < MAX_HIGHLIGHT_API_ATTEMPTS:
            print(
                f"[highlights] invalid model output on attempt {attempt}/{MAX_HIGHLIGHT_API_ATTEMPTS}; retrying",
                flush=True,
            )
            prompt = (
                base_prompt
                + "\n\nIMPORTANT: Return ONLY valid JSON with a top-level 'highlights' array."
                + " Each item must include: title, start_time, end_time, score, hook_sentence, virality_reason."
                + " No markdown fences, no commentary."
            )

    raise RuntimeError(
        f"Highlight generator produced invalid output after {MAX_HIGHLIGHT_API_ATTEMPTS} attempts: {last_error}"
    )


def dedupe_highlights(highlights: List[Dict]) -> List[Dict]:
    """Drop a highlight if it overlaps >50% with a higher-scoring one already kept."""
    highlights = sorted(highlights, key=lambda x: int(x.get("score", 0)), reverse=True)
    kept: List[Dict] = []
    for h in highlights:
        h_start = float(h["start_time"])
        h_end = float(h["end_time"])
        h_dur = h_end - h_start
        overlapping = False
        for k in kept:
            latest_start = max(h_start, float(k["start_time"]))
            earliest_end = min(h_end, float(k["end_time"]))
            overlap = earliest_end - latest_start
            if overlap > 0 and overlap > 0.5 * h_dur:
                overlapping = True
                break
        if not overlapping:
            kept.append(h)
    return kept


def _checkpoint_fingerprint(duration: float, chunk_count: int, num_clips: int,
                            clip_seconds: Optional[List[float]] = None,
                            kind: str = content_kinds.OTHER) -> str:
    """Identifies the run a saved checkpoint belongs to.

    Includes the requested clip length: asking for 30s clips after a run that
    found 60s ones is a different question, and reusing those answers would
    silently ignore what was asked for. The kind of video is the same: chunks
    ranked as a stream are no answer for the same video ranked as a podcast.
    PROMPT_VERSION does that job across releases - chunks ranked by an older
    prompt are answers to a question we no longer ask, and resuming onto them
    would hide the change from every video that has already been through the
    app once.
    """
    length = "-".join(str(int(x)) for x in clip_seconds) if clip_seconds else "default"
    # Duration to the minute, not the second. It is here to notice that the
    # media changed, and it was measuring something else as well: a transcript
    # reports the media's own length when it has just been made, and its last
    # cue's end when it is read back from the cached .srt. Those differ by
    # however much silence trails the last word -- three seconds on a 4h27m
    # stream measured here -- so every second run of a long video computed a
    # different fingerprint and threw away all fifteen chunks it had already
    # paid an LLM to rank. That is the exact case the checkpoint exists for.
    #
    # A minute is coarse enough to absorb that and far finer than any real
    # change of file, and it is not the only guard: the checkpoint is written
    # beside one specific video, and chunk_count moves with the length too.
    return (f"v{PROMPT_VERSION}|{duration / 60:.0f}|{chunk_count}|{num_clips}"
            f"|{length}|{kind}")


def _load_saved_content(path: Optional[Path], duration: float) -> Optional[Dict]:
    """What an earlier attempt at this video decided it was, if it saved one.

    Read whatever the fingerprint says: the question may have changed (a new
    clip length) while the video, and so what kind of video it is, has not.
    """
    if not path or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    # Same reasoning as the fingerprint above: compared to the minute, so a
    # cached transcript's slightly shorter reading still matches the run that
    # wrote it.
    if not isinstance(data, dict) or round(data.get("duration", -1) / 60) != round(duration / 60):
        return None
    content = data.get("content")
    return content if isinstance(content, dict) else None


def _load_checkpoint(path: Optional[Path], fingerprint: str) -> Dict[str, List[Dict]]:
    """Chunks already ranked on an earlier attempt, keyed by chunk index."""
    if not path or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    if not isinstance(data, dict) or data.get("fingerprint") != fingerprint:
        # A different video, or the same one asked a different question.
        return {}
    chunks = data.get("chunks")
    return chunks if isinstance(chunks, dict) else {}


def _save_checkpoint(path: Optional[Path], fingerprint: str,
                     chunks: Dict[str, List[Dict]],
                     content: Optional[Dict] = None, duration: float = 0.0) -> None:
    if not path:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"fingerprint": fingerprint, "chunks": chunks,
                        "content": content, "duration": round(duration)}, indent=2),
            encoding="utf-8",
        )
    except OSError:
        # Losing the ability to resume is not a reason to fail the run.
        pass


def finalize(
    highlights: List[Dict],
    transcript: Dict,
    clip_seconds: Optional[List[float]] = None,
    audio: Optional[AudioTrack] = None,
    content_type: str = "",
    reserve_seconds: float = 0.0,
) -> List[Dict]:
    """Turn the model's proposals into the spans that actually get cut.

    Three passes, in this order for a reason:

    1. Boundaries first. Snapping moves every span -- often by seconds, since
       a clip is re-opened on its own hook line -- so measuring signals before
       this would be measuring audio that is no longer in the clip.
    2. Signals second, on the final spans, blending what the footage did into
       what the model thought.
    3. Dedupe last. Two candidates the model kept apart can land on top of
       each other once both are snapped to the same sentence boundaries, and
       shipping the same moment twice is worse than shipping one fewer clip.
    """
    highlights = boundaries.refine(
        highlights, transcript,
        clip_seconds=clip_seconds, audio=audio,
        content_type=content_type, reserve_seconds=reserve_seconds,
    )
    signals.rescore(highlights, transcript, audio)
    highlights = dedupe_highlights(highlights)
    highlights.sort(key=lambda h: int(h.get("score", 0) or 0), reverse=True)
    return highlights


def get_highlights(
    transcript: Dict,
    num_clips: int = 3,
    llm_fn: Optional[LLMFn] = None,
    checkpoint_path: Optional[Path] = None,
    clip_seconds: Optional[List[float]] = None,
    brief: str = "",
    audio: Optional[AudioTrack] = None,
    reserve_seconds: float = 0.0,
    kind: str = content_kinds.AUTO,
    video_meta: Optional[Dict] = None,
) -> Dict:
    """Main entry point — returns {highlights: [...], content: {...}}, best first.

    `llm_fn` swaps the underlying LLM. Defaults to MuAPI gpt-5-mini; local
    mode passes in a local LLM-backed callable.

    `kind` is what sort of video this is -- stream, vlog, podcast, tutorial or
    other -- and decides what the ranker counts as a good moment. "auto" works
    it out from the transcript and `video_meta`, the video's own listing. What
    it settled on comes back as `content`, so the caller can frame the clips
    for it.

    `checkpoint_path` makes a long video resumable. Each chunk costs an API
    request, and a nine-chunk video that dies on chunk three used to throw
    away the two it had already paid for -- so every finished chunk is written
    out, and a later attempt picks up where the quota ran out.

    `audio` is the source's loudness envelope, when one could be measured. It
    is what lets the ranking hear the clip rather than only read it, and what
    tells the renderer where a hook replay should open.
    """
    llm_fn = llm_fn or call_muapi_llm
    duration = transcript.get("duration", 0)
    content_info = resolve_content(
        transcript, llm_fn, kind=kind, video_meta=video_meta,
        saved=_load_saved_content(checkpoint_path, duration),
    )
    print(f"[highlights] content={content_info.get('content_type')} density={content_info.get('density')} duration={duration:.0f}s", flush=True)
    said = f"ranking as {content_info['kind']} ({content_info['kind_source']})"
    if content_info.get("detected_kind") and content_info["detected_kind"] != content_info["kind"]:
        said += f" - it looked like {content_info['detected_kind']}"
    print(f"[highlights] {said}", flush=True)

    if duration >= LONG_VIDEO_THRESHOLD:
        chunks = chunk_transcript(transcript)
        print(f"[highlights] long video — splitting into {len(chunks)} chunks", flush=True)

        fingerprint = _checkpoint_fingerprint(duration, len(chunks), num_clips, clip_seconds,
                                              kind=content_info["kind"])
        done = _load_checkpoint(checkpoint_path, fingerprint)
        if done:
            print(f"[highlights] resuming — {len(done)}/{len(chunks)} chunk(s) "
                  f"already ranked earlier", flush=True)

        all_highlights: List[Dict] = []
        for i, chunk in enumerate(chunks):
            offset = chunk.get("_offset", 0)
            key = str(i)
            if key in done:
                all_highlights.extend(done[key])
                continue

            text = build_transcript_text(chunk)
            print(f"[highlights] chunk {i + 1}/{len(chunks)} (offset {offset:.0f}s)", flush=True)
            result = call_highlight_api(text, content_info, chunk["duration"], num_clips=num_clips, is_chunk=True, llm_fn=llm_fn, clip_seconds=clip_seconds, brief=brief)
            ranked = []
            for h in result.get("highlights", []):
                h["start_time"] = float(h["start_time"]) + offset
                h["end_time"] = float(h["end_time"]) + offset
                ranked.append(h)
            all_highlights.extend(ranked)

            # Written per chunk, not at the end: the whole point is to survive
            # the failure that happens on the *next* one.
            done[key] = ranked
            _save_checkpoint(checkpoint_path, fingerprint, done,
                             content=content_info, duration=duration)

        candidates = dedupe_highlights(all_highlights)
    else:
        text = build_transcript_text(transcript)
        result = call_highlight_api(text, content_info, duration, num_clips=num_clips, llm_fn=llm_fn, clip_seconds=clip_seconds, brief=brief)
        candidates = dedupe_highlights(result.get("highlights", []))

    highlights = finalize(
        candidates, transcript,
        clip_seconds=clip_seconds, audio=audio,
        content_type=str(content_info.get("content_type") or ""),
        reserve_seconds=reserve_seconds,
    )
    print(f"[rank] {len(highlights)} candidate(s) after snapping · "
          f"{signals.summarise(highlights)}", flush=True)
    return {"highlights": highlights, "content": content_info}
