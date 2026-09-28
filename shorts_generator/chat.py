"""Chat velocity: when the audience reacted, read from the stream's own chat.

Everything else the ranker knows about a moment is an inference. The model
reads the words and guesses what lands; the audio envelope hears a scream and
guesses it matters. A live chat is not a guess. It is a few hundred people
watching the moment as it happened and typing "WHAT" at the same second, which
is the closest thing to a test audience a stream will ever have.

It also sees what a transcript cannot. A clutch play with the streamer silent,
a jump scare answered with a gasp, something on screen nobody names out loud:
none of that is written down, so a ranker that only reads the transcript never
proposes it. Chat erupts all the same. So the chat is used twice -- as a
marker in the transcript the model reads, pointing it at the moments people
reacted to, and as a measured signal in the rank, alongside the audio.

YouTube keeps a replay of a stream's chat, and yt-dlp can fetch it as the
`live_chat` subtitle track: one JSON object per chat action, each stamped with
its offset into the video. Nothing else needs it but this module, and nothing
else needs to know whether there was one. Everything here is best-effort: no
chat, a chat too quiet to mean anything, or YouTube refusing to hand it over,
and the run goes on exactly as it did before this existed.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# Seconds each chat rate is summed over. A reaction is a burst: ten seconds
# catches all of it, where one second would split a burst across buckets and
# a minute would blur it into the conversation around it.
RATE_WINDOW = 10

# How far either side the "usual" rate is measured. Chat drifts a lot across a
# stream -- a trickle for the first half hour, packed for the big reveal, dying
# away at the end -- so a spike is measured against the ten minutes around it,
# not against the whole stream, or every busy stretch would read as a spike and
# every quiet one as nothing.
BASELINE_RADIUS = 300

# People react after the moment, not during it. The stream reaches them a few
# seconds late and typing takes a few more, so the chat that belongs to a span
# lands up to this long after it ends -- and none of it lands before.
REACTION_LAG = 12.0

# A chat too quiet to count on. Under a message every twenty seconds or so, a
# "spike" is three people saying hello at once, and scoring a clip on that
# would be measuring noise with a straight face. A small channel's own stream
# often sits here, and the right thing then is to not pretend.
MIN_MESSAGES = 150
MIN_PER_MINUTE = 3.0

# How much more than usual a burst has to be before it is pointed out to the
# model. Twice the local rate is where it stops being the ordinary ebb.
SPIKE_RATIO = 2.0
SPIKE_GAP = 60.0

# The first minute and a half of a stream is people arriving: "LETS GOOO",
# "HYPE", "yes", which read as a reaction and are about the stream starting.
# On a real 22-minute stream the opening wave was the first two "spikes"
# found. Nothing before this counts.
START_GRACE = 90

# A streamer asking chat to type something gets a flood of exactly that --
# "everybody say hi", "type P in the chat", "can we get a W". On the same
# stream, three of its six biggest bursts were answers to a request, and none
# of them was a moment. So the chat for a while after a request is set aside:
# the length of the line, plus this long for the answers to arrive.
REQUEST_WINDOW = 15.0
_REQUEST = re.compile(
    r"\b(every(body|one)|y'?all|you guys|chat)\b[^.?!]{0,40}\b(say|type|spam|drop|put)\b"
    r"|\b(say|type|spam|drop|put)\b[^.?!]{0,40}\b(in|into|of) (the |my |your )?"
    r"(youtube |twitch )?chat\b"
    r"|\bcan (we|i) get (a|an|some)\b[^.?!]{0,20}\b(w|ws|l|f|gg|in the chat)\b"
    # "type one", "type P": the answer asked for, on its own. Needed because
    # Whisper mishears "chat" often enough ("Chaps ... type one if you don't
    # want to be banned") that the addressee cannot be relied on. Only a
    # single letter or digit, or a one-word vote: "type it in" and "you type
    # so fast" are ordinary speech, and so is "type a game", which is how
    # Whisper often hears "type of game".
    r"|\btype (the letter )?(one|two|yes|no|[b-z0-9])\b",
    re.IGNORECASE)

# Bumped when the parsing or the weighting changes, so a cached summary from
# an older build is re-read from the replay rather than trusted.
CACHE_VERSION = 1

# Messages that are about the stream, not the moment: hellos when it starts,
# goodbyes when it ends, "just subbed", birthdays. They arrive in floods that
# look exactly like a reaction on a rate chart and mean nothing about what is
# on screen, so they are not counted at all.
_NOT_A_REACTION = re.compile(
    r"^\s*(hi+|hey+|hel+o+|hii+|yo+|sup+|bye+|good ?bye|gn|good ?(night|morning)"
    r"|first|early)\b"
    r"|\b(sub(bed|scribed?|scribe)?|birthday|bday|like the (video|stream))\b",
    re.IGNORECASE)

# What a reaction looks like in chat: shouting, stretched letters, stacked
# punctuation, and a short vocabulary of disbelief and laughter. A plain
# sentence still counts, just for less -- it is conversation, and conversation
# is the baseline a spike is measured against.
_REACTION = re.compile(
    r"!{2,}|\?!|!\?|(\w)\1{3,}"
    r"|\b(omg|omfg|wtf|what+|no+|yes+|lo+l+|lmao+|lmfao|rofl|w+|gg+|holy|bro+|dead"
    r"|insane|clip( it| that)?|let'?s+ go+|wo+w|hu+h|na+h|crazy|peak|cooked"
    r"|no ?way|kekw|lul|pog\w*)\b"
    r"|\U0001F480|\U0001F602|\U0001F923|\U0001F62D|\U0001F631|\U0001F525"
    r"|:(skull|rolling_on_the_floor_laughing|face-with-tears-of-joy"
    r"|loudly_crying_face|fire):",
    re.IGNORECASE)
_PLAIN_WEIGHT = 0.4
# A paid message is somebody spending money on this exact second. That is a
# stronger vote than typing, but not so much stronger that one donation can
# outweigh a room shouting.
_PAID_WEIGHT = 2.0


def message_weight(text: str) -> float:
    """How much one chat message says about the moment it was typed in."""
    t = (text or "").strip()
    if not t:
        # An emote-only message has no text runs to read, and emote spam is
        # about the most reliable reaction there is.
        return 1.0
    if _NOT_A_REACTION.search(t):
        return 0.0
    letters = [c for c in t if c.isalpha()]
    if len(letters) >= 3 and sum(c.isupper() for c in letters) >= 0.7 * len(letters):
        return 1.0
    return 1.0 if _REACTION.search(t) else _PLAIN_WEIGHT


def _message_text(renderer: Dict) -> str:
    """A chat message's text, emoji spelled out as their shortcodes."""
    out = []
    for run in (renderer.get("message") or {}).get("runs") or []:
        if "text" in run:
            out.append(str(run["text"]))
        else:
            cuts = (run.get("emoji") or {}).get("shortcuts") or [""]
            out.append(str(cuts[0]))
    return "".join(out)


def parse_live_chat(path: str) -> Tuple[List[float], int]:
    """Weighted chat activity per second of video, from a yt-dlp live_chat file.

    Read a line at a time: a big stream's replay runs to hundreds of
    megabytes, and all that survives is one number per second.
    """
    per_second: List[float] = []
    messages = 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                action = json.loads(line).get("replayChatItemAction") or {}
                offset = int(action.get("videoOffsetTimeMsec", -1)) / 1000.0
            except (ValueError, TypeError, AttributeError):
                continue
            # Messages from the waiting room before the stream started carry a
            # negative offset. They are about nothing in the video.
            if offset < 0:
                continue
            for act in action.get("actions") or []:
                item = (act.get("addChatItemAction") or {}).get("item") or {}
                text = item.get("liveChatTextMessageRenderer")
                paid = item.get("liveChatPaidMessageRenderer")
                if text is not None:
                    w = message_weight(_message_text(text))
                elif paid is not None:
                    w = _PAID_WEIGHT
                else:
                    continue
                second = int(offset)
                if second >= len(per_second):
                    per_second.extend([0.0] * (second + 1 - len(per_second)))
                per_second[second] += w
                messages += 1
    return per_second, messages


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    mid = len(ordered) // 2
    return float(ordered[mid]) if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def _percentile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return float(ordered[min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))])


def _clamp01(v: float) -> float:
    return 0.0 if v < 0 else (1.0 if v > 1 else float(v))


class ChatTrack:
    """How hard chat reacted, second by second, against its own usual pace."""

    def __init__(self, per_second: Sequence[float], messages: int) -> None:
        self.per_second = [float(v) for v in per_second]
        self.messages = int(messages)
        n = len(self.per_second)

        # Rolling sum over the last RATE_WINDOW seconds, so second t holds
        # the burst that ends there.
        rate: List[float] = []
        running = 0.0
        for i, v in enumerate(self.per_second):
            running += v
            if i >= RATE_WINDOW:
                running -= self.per_second[i - RATE_WINDOW]
            rate.append(running)
        self.rate = rate

        # The usual pace around each second: the median of the rate across
        # the ten minutes around it, sampled every 5s -- plenty for a median,
        # and a four-hour stream stays quick to read. Floored at half the
        # stream's own median, so the dregs of chat after the stream ends --
        # three messages against a baseline of one -- cannot pass for the
        # biggest moment of the night.
        overall = _median(rate[::5]) if rate else 0.0
        floor = max(1.0, 0.5 * overall)
        centres = list(range(0, n, 5))
        coarse = [max(floor, _median(rate[max(0, c - BASELINE_RADIUS):c + BASELINE_RADIUS:5]))
                  for c in centres]
        self.baseline = [coarse[min(len(coarse) - 1, i // 5)] for i in range(n)]
        self.ratio = [r / b for r, b in zip(rate, self.baseline)]
        # Arrivals, not reactions -- see START_GRACE. Held at the usual pace
        # rather than zeroed, so nothing here reads as a lull either.
        self._mute(0, START_GRACE)
        self._requests_heard = False

        # What counts as a big reaction for this stream. A top-1% second, and
        # never less than the spike threshold, so a chat that never really
        # erupts cannot make its best murmur read as a full-scale reaction.
        self._top = max(SPIKE_RATIO, _percentile(self.ratio, 0.99))

    def _mute(self, start: float, end: float) -> None:
        """Hold chat at its usual pace over a stretch: it is not evidence there."""
        a = max(0, int(start))
        b = min(len(self.ratio), int(end) + 1)
        for i in range(a, b):
            self.ratio[i] = min(self.ratio[i], 1.0)

    def ignore_requests(self, transcript: Optional[Dict]) -> int:
        """Set aside the chat that answered the streamer asking it to type.

        Needs the transcript, which the chat does not have, so it is called
        once the two meet. Returns how many requests were heard. Safe to call
        twice: a resumed ranking does, and muting is idempotent anyway.
        """
        if self._requests_heard or not transcript:
            return 0
        self._requests_heard = True
        heard = 0
        for seg in transcript.get("segments") or []:
            if _REQUEST.search(str(seg.get("text") or "")):
                try:
                    self._mute(float(seg["start"]), float(seg["end"]) + REQUEST_WINDOW)
                except (KeyError, TypeError, ValueError):
                    continue
                heard += 1
        if heard:
            # The scale was set with those answers in it; one prompted flood
            # would otherwise stay the stream's top reaction, so every real one
            # would score as a fraction of something that never happened.
            self._top = max(SPIKE_RATIO, _percentile(self.ratio, 0.99))
        return heard

    def __bool__(self) -> bool:
        return bool(self.per_second)

    @property
    def duration(self) -> float:
        return float(len(self.per_second))

    def _window(self, start: float, end: float) -> Tuple[int, int]:
        """The seconds whose chat belongs to a span: during it, and just after."""
        a = max(0, int(start))
        b = min(len(self.ratio) - 1, int(end + REACTION_LAG))
        return a, b

    def peak(self, start: float, end: float) -> Tuple[float, Optional[float]]:
        """The biggest burst answering a span, as (times usual, when)."""
        if not self.ratio or end <= start:
            return 0.0, None
        a, b = self._window(start, end)
        if b < a:
            return 0.0, None
        window = self.ratio[a:b + 1]
        best = max(window)
        return best, float(a + window.index(best))

    def velocity(self, start: float, end: float) -> float:
        """0-1: how hard chat reacted to this span, against this stream.

        Usual pace scores nothing: a moment chat talked through at its normal
        rate earns no credit for chat having been open.
        """
        ratio, _ = self.peak(start, end)
        return _clamp01((ratio - 1.0) / (self._top - 1.0))

    def spikes(self, start: float = 0.0, end: Optional[float] = None,
               limit: int = 8) -> List[Tuple[float, float]]:
        """The biggest bursts in a stretch, as (second, times usual), in time order.

        At least SPIKE_GAP apart: one reaction spans several seconds over the
        threshold, and listing each of them would point the model at the same
        moment eight times and at nothing else.
        """
        if not self.ratio:
            return []
        a = max(0, int(start))
        b = len(self.ratio) if end is None else min(len(self.ratio), int(end) + 1)
        order = sorted(range(a, b), key=lambda i: -self.ratio[i])
        picked: List[int] = []
        for i in order:
            if self.ratio[i] < SPIKE_RATIO or len(picked) >= limit:
                break
            if all(abs(i - j) >= SPIKE_GAP for j in picked):
                picked.append(i)
        # Each rate is the burst that *ends* at its second, so the burst
        # itself sits half a window earlier. Reported there: a marker at the
        # tail of the burst points the model further past the moment than the
        # reaction lag already does.
        half = RATE_WINDOW / 2.0
        return [(max(0.0, i - half), round(self.ratio[i], 1)) for i in sorted(picked)]

    def to_json(self) -> Dict:
        return {"version": CACHE_VERSION, "messages": self.messages,
                "per_second": [round(v, 2) for v in self.per_second]}


def usable(per_second: Sequence[float], messages: int) -> bool:
    """Is there enough chat here to say anything about a moment?"""
    minutes = max(1.0, len(per_second) / 60.0)
    return messages >= MIN_MESSAGES and messages / minutes >= MIN_PER_MINUTE


def cache_path(source_path: str) -> Path:
    """Where a source's chat summary lives: beside it, like its .srt."""
    return Path(source_path).with_suffix(".chat.json")


def _read_cache(path: Path) -> Optional[Dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return None
    return data


def _write_cache(path: Path, per_second: Sequence[float], messages: int) -> None:
    try:
        path.write_text(json.dumps({"version": CACHE_VERSION, "messages": messages,
                                    "per_second": [round(v, 2) for v in per_second]}),
                        encoding="utf-8")
    except OSError:
        # Losing the cache costs one re-fetch next time, never this run.
        pass


def _sidecar(source_path: str) -> Optional[str]:
    """A live_chat file somebody saved beside a local video with yt-dlp.

    yt-dlp names it `<video>.live_chat.json`, so a VOD fetched by hand with
    `--write-subs --sub-langs live_chat` brings its chat along.
    """
    base = os.path.splitext(source_path)[0]
    candidate = base + ".live_chat.json"
    return candidate if os.path.isfile(candidate) else None


def _download(video_url: str, into: str) -> Optional[str]:
    """Fetch a YouTube stream's chat replay; the file's path, or None."""
    from .local import yt_access
    from .local.downloader import _import_ytdlp

    yt_dlp = _import_ytdlp()
    opts = {
        "skip_download": True,
        "writesubtitles": True,
        "subtitleslangs": ["live_chat"],
        "outtmpl": os.path.join(into, "chat.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        # yt-dlp prints a progress line per chat fragment even when quiet, and
        # a long stream has thousands of them.
        "noprogress": True,
    }
    yt_access.run(yt_dlp, opts,
                  lambda ydl: ydl.extract_info(video_url, download=True),
                  what="this stream's chat")
    for name in os.listdir(into):
        if name.endswith(".live_chat.json"):
            return os.path.join(into, name)
    return None


def load(source_path: str, video_url: str = "") -> Optional[ChatTrack]:
    """The chat for a source video, fetched once and cached beside it.

    Tries, in order: the summary cached by an earlier run; a live_chat file
    saved beside a local video; and, for a YouTube link, the replay itself.
    A video with no chat, or too little to mean anything, is cached as such,
    so a second run over it does not ask YouTube again.
    """
    cache = cache_path(source_path)
    data = _read_cache(cache)
    if data is not None:
        per_second, messages = data.get("per_second") or [], int(data.get("messages") or 0)
        if not usable(per_second, messages):
            return None
        print(f"[chat] using the saved chat replay ({messages} messages)", flush=True)
        return ChatTrack(per_second, messages)

    raw = _sidecar(source_path)
    scratch = None
    try:
        if raw is None:
            from .local.downloader import _extract_youtube_video_id
            if not video_url or not _extract_youtube_video_id(video_url):
                return None
            print("[chat] fetching the chat replay", flush=True)
            scratch = tempfile.mkdtemp(prefix="clipmint-chat-")
            raw = _download(video_url, scratch)
            if raw is None:
                print("[chat] no chat replay on this video - "
                      "ranking on the transcript and audio", flush=True)
                _write_cache(cache, [], 0)
                return None
        per_second, messages = parse_live_chat(raw)
    except Exception as e:  # noqa: BLE001 - chat is a bonus, never the run
        why = (str(e).strip().splitlines() or [e.__class__.__name__])[0][:160]
        print(f"[chat] could not read the chat replay ({why}) - "
              f"ranking without it", flush=True)
        return None
    finally:
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)

    _write_cache(cache, per_second, messages)
    if not usable(per_second, messages):
        print(f"[chat] only {messages} chat message(s) - too quiet to rank on, "
              f"so it is left out", flush=True)
        return None
    track = ChatTrack(per_second, messages)
    found = track.spikes(limit=5)
    print(f"[chat] {messages} messages over {track.duration / 60:.0f} min; "
          f"biggest reactions at "
          + (", ".join(f"{_clock(t)} ({r}x)" for t, r in found) or "none"), flush=True)
    return track


def _clock(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
