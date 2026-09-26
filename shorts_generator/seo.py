"""Per-clip YouTube metadata: the title, description and tags to upload with.

A rendered clip is only half the job. What decides whether a Short gets seen is
the packaging around it - a title that earns the tap, a description that tells
the algorithm what the clip is about, and tags that put it next to the videos
its audience already watches.

Those three are not equal, and the prompt below is built around the order.
Titles and descriptions are the strong ranking signals; the first three
hashtags in the description are what viewers actually see, rendered as links
above the title; tags are a weak supporting signal that a long list only
dilutes. And the title has one job before any of that: it has to NAME the
thing. A title lifted straight out of the transcript reads fine to someone who
watched the stream and is invisible to everyone else, because it contains no
word anyone would ever search or browse for.

Naming the thing is also where this used to go wrong. The subject was worked
out once per video, from its listing and transcript, and every clip was filed
under it. A variety stream titled "The Finals" that moved on to Firewatch and a
horror game came back with Firewatch clips tagged #thefinals. So each clip now
carries a `scene` -- what its own frames show, from vision.py -- and the
subject a clip is filed under comes from that first. The run-level subject is
only a fallback for clips the frames do not contradict.

Nothing about a clip is assumed to be gaming either. The scene says what kind
of video it is -- a podcast, a face-cam story, a tutorial -- and the format
tags and the framing of the title follow from that.

And the writer no longer hands back one title. It writes several, each on a
different angle, scores them against a rubric, and the app blends that with a
check of its own (length, whether the subject is named, filler) to rank them.
The best one is used; the rest are kept so the person uploading can pick.

It is deliberately best-effort: SEO text sits on top of a clip that already
exists, so every failure path here falls back to something usable rather than
sinking a run that has already paid for a download, a transcription and a
render.

One clip is posted to three places, and they do not reward the same packaging.
YouTube Shorts has a title, a description and a tag box; an Instagram Reel is
found by its caption's first 125 characters and grows on sends; TikTok is
searched like a search engine and ignores #fyp. So after the YouTube metadata
is written, a second pass writes a Reels caption, cover text and alt text, and
a TikTok caption, each with its own hashtags. Both passes work from the growth
playbook in playbook.py -- how each platform distributes short video right now
-- which is kept in a file so it can change when the platforms do.

Everything the writer offers more than one of is ranked: titles, descriptions,
Reels captions and tags each carry a score, blended from the model's editorial
rubric and a check of the app's own that can count.
"""
import json
import re
from typing import Callable, Dict, List, Optional

from . import content_kinds, playbook

LLMFn = Callable[[str], str]

# YouTube's own limits, minus a little headroom so a stray character can't
# push a field over and get it rejected at upload time.
TITLE_LIMIT = 100
DESCRIPTION_LIMIT = 4800
TAGS_TOTAL_LIMIT = 460          # the real cap is 500 across all tags
TAG_LIMIT = 30                  # characters per tag
# The tags actually put in the box. More are written and kept as options, but
# tags are a weaker signal than the title and description, and padding the
# box only dilutes it.
MAX_TAGS = 15
MAX_TAG_OPTIONS = 24
HOOK_LIMIT = 60
# Hashtags are the most visible metadata on the upload - the first three render
# as links above the title - and past a handful they read as spam.
MAX_HASHTAGS = 5
# Titles written per clip. Enough to cover the distinct angles; more just
# produces near-duplicates of the good ones.
TITLE_OPTIONS = 5
# Descriptions and Reels captions per clip: one built for search, one for the
# feed. Two is enough to choose between without doubling what a run costs.
DESCRIPTION_OPTIONS = 2
CAPTION_OPTIONS = 2

# Instagram and TikTok limits, with the same headroom as YouTube's.
REELS_CAPTION_LIMIT = 2150
TIKTOK_CAPTION_LIMIT = 3900
# What shows of a Reels caption before "more".
CAPTION_FOLD = 125
COVER_LIMIT = 40
ALT_TEXT_LIMIT = 100
# Hashtags that mean nothing on the platform they would be posted to, or
# nothing anywhere. "#shorts" on a Reel files it with YouTube.
OFF_PLATFORM_HASHTAGS = {"#shorts", "#ytshorts", "#youtubeshorts", "#fyp", "#foryou",
                         "#foryoupage", "#fypシ", "#reels", "#reelsinstagram", "#viral"}

# How much each kind of tag is worth, before its position in the model's own
# ranking. The exact subject is what someone searches; a format term only
# says what shelf the clip sits on.
TAG_KIND_WEIGHT = {"subject": 100, "query": 90, "variant": 80, "moment": 72,
                   "genre": 68, "format": 55}

# Format terms by kind of video. Only the clip's own kind goes into its tags:
# "gaming clips" on a podcast clip files it with an audience that will not
# watch it.
FORMAT_TAGS = {
    "gameplay": ["gaming", "gameplay", "gaming clips"],
    "podcast": ["podcast", "podcast clips"],
    "talking_head": ["storytime", "face cam"],
    "just_chatting": ["just chatting", "stream highlights"],
    "irl": ["irl", "irl stream"],
    "storytelling": ["storytime", "story time"],
    "reaction": ["reaction", "reaction video"],
    "tutorial": ["tutorial", "how to"],
    "commentary": ["commentary"],
    "music": ["music"],
    "sports": ["sports", "sports highlights"],
    "comedy_skit": ["comedy", "funny"],
    "vlog": ["vlog"],
    "interview": ["interview"],
    "news": ["news"],
}

# Phrases that promise without saying anything. A title spending characters
# on them has fewer left for the words that make it findable.
FILLER = ("you won't believe", "you wont believe", "wait for it", "watch till the end",
          "watch until the end", "must watch", "gone wrong", "not clickbait", "omg")

# Ask for the whole batch at once. Ten separate calls would take ten times as
# long and give the model no way to keep the titles from repeating each other.
SEO_PROMPT = """You are an expert social media strategist and growth hacker. You package short-form video for channels that consistently break 1M views, and you write the titles, description and tags that decide whether a Short gets watched or scrolled past. This pass is for YOUTUBE SHORTS.

Everything you write follows the growth playbook below: how YouTube Shorts, Instagram Reels and TikTok distribute short video right now. Use it to grow every clip.

GROWTH PLAYBOOK
{playbook}

SOURCE VIDEO
{video_context}

{subject_block}

You are given {n} clips cut from that video, already ranked best to worst by viral potential. For EACH clip, write upload-ready metadata.

THE TWO RULES THAT OUTRANK EVERYTHING BELOW:
1. ACCURATE. Every claim must be provable from that clip's own WHAT IS ON SCREEN and WHAT IS ACTUALLY SAID blocks. Never promise a reveal the clip does not contain, never name a person, game or number the clip does not support, never imply a stake the clip does not reach. A title the clip fails to deliver gets swiped in two seconds, and short-form ranking punishes that harder than a boring title ever could.
2. VIRAL. Within what is true, pick the most arresting framing. The strongest hook is almost always a real specific detail from the clip - the exact number, the exact word, the actual thing that happened on screen - not a vague tease. Specific and true beats sensational and empty every time.

WHAT EACH CLIP IS ABOUT - read this before writing anything:
- Each clip has a WHAT IS ON SCREEN block, taken from its own frames. It says what kind of video the clip is and what is in it, and it OUTRANKS the source video's title. A stream titled after one game can wander into three others; the frames say which one this clip is.
- If the block says the subject is CONFIRMED, name it. If it gives only an UNCONFIRMED guess, do NOT print that name anywhere - use the genre instead ("this horror game", "a co-op shooter"). A wrong name is the one thing the exact audience that would watch will call out in the comments.
- Do not assume gaming. A podcast clip is packaged like a podcast clip, a story told to camera like a story. Write for the kind of video the clip actually is.

HOW SHORTS ARE ACTUALLY DISTRIBUTED - write for this, not for a search engine:
- The title is read in well under a second, on a phone, next to a video that is already playing. Only about the first 40 characters are sure to be seen. Front-loaded and concrete beats complete and tidy.
- Shorts are also found through search. A title that contains the phrase someone would type ("firewatch ending", "podcast guest storms out") is found for months; one that does not is found only while the feed is pushing it.
- Titles and descriptions outrank tags as ranking signals. The words that name the subject must appear in the title and in the first line of the description, not only in the tag box.
- The FIRST THREE hashtags in the description are shown as clickable links above the title. They must be the terms this clip should be filed under.
- A hashtag in the TITLE buys nothing and spends characters you need for keywords. No hashtags in titles.
- 3-5 hashtags total. More than that dilutes them, and past 60 YouTube ignores every hashtag on the video.

REACHING PAST THE AUDIENCE THE CHANNEL ALREADY HAS:
- WRITE FOR A STRANGER. Assume the viewer has never heard of this creator and has never played the game or seen the show. Insider framing - a nickname, a running joke, "he did it again" - caps the clip at the audience it started with.
- PAIR THE NARROW TERM WITH A BROAD ONE. The exact name makes a clip findable; the category it belongs to makes it recommendable to people who would never search that name.
- NO TWO CLIPS IN THIS SET MAY LEAD THE SAME WAY. They will be posted to one channel; if every best title opens the same way they compete for one slice of the feed.
- NEVER WIDEN BY LYING. Broadening is a choice of plainer words, never a bigger claim.

TITLE_OPTIONS - write exactly {options} titles per clip, each on a DIFFERENT angle:
- "search": leads with the phrase a person would actually type to find this moment
- "curiosity": says enough to raise a question, withholds the answer
- "reaction": names who reacted and how hard, and to what
- "detail": the exact number, object, line or event lifted from the clip
- "contradiction" or "stakes": the outcome fights the setup, or names what is about to go wrong or right
Every option: 35-70 characters (hard limit {title_limit}), the first 3-4 words carry the payoff, names the subject when it is confirmed, sentence case or title case, at most one emoji, no hashtags, no ALL CAPS, no filler like "you won't believe" or "wait for it". A transcript line pasted in is NOT a title.
Score each option honestly, as an editor would before posting:
- "hook" 0-40: would a stranger stop scrolling for it?
- "clarity" 0-20: does a stranger understand what the clip is from the title alone?
- "search" 0-20: does it contain a phrase people really search?
- "truth" 0-20: does the clip fully deliver it? Anything under 20 overclaims.
Then list the options best first.

DESCRIPTION_OPTIONS - write exactly {description_options} descriptions per clip, each on a different angle:
- "search": built around the phrase a person would type to find this moment
- "story": built around what actually happens, for the viewer who is already watching
Every description:
- Line 1: the hook as a full sentence, containing the main search phrase - it is the only line most viewers ever see
- Lines 2-3: what actually happens, in plain words, naming the subject (only if confirmed) and the kind of moment it is. This is the text the ranker reads to decide who to show the clip to, so spend it on real nouns, not adjectives
- Then one short call to action: a specific question about THIS clip beats "comment below"
- Then one line of 3-5 hashtags, #shorts first, then the most specific ones for this clip. Lowercase, no spaces inside a hashtag
- Under 500 characters. Plain text, no markdown
Score each description honestly:
- "hook" 0-40: does line 1 alone make a stranger want to watch?
- "search" 0-30: are the search phrase and the confirmed subject in line 1?
- "clarity" 0-15: would the ranker know exactly who to show this to?
- "truth" 0-15: does the clip fully deliver it?
Then list the descriptions best first.

TAGS - 14-20 lowercase tags per clip, ranked most valuable first, each labelled with its kind:
- "subject": the exact name of what the clip is about (only if confirmed) - always first
- "variant": other ways people write it - abbreviations, the name without spaces, a common misspelling
- "query": a real multi-word search a person would type to find this moment ("firewatch delilah radio", "the finals destruction")
- "genre": the category ("horror games", "comedy podcast")
- "moment": what kind of moment it is ("jumpscare", "funny moments", "plot twist")
- "format": what kind of video it is, matched to the clip ("gameplay", "podcast clips", "storytime") - never "gaming" for a clip that is not gameplay
Nothing invented, no hashes, no duplicates, nothing over 30 characters.

HOOK_TEXT rules:
- The on-screen caption burned over the first 2 seconds, read while the clip is already playing
- Under 8 words, sentence case, no ending punctuation
- It must raise something the next three seconds answer
- Never the same sentence as any title

SEARCH_PHRASE: the single phrase this clip should rank for, 2-5 words, lowercase.

WHY_IT_WORKS: one sentence for the channel owner about the best title: which angle it takes and what it is betting on.
{avoid_block}
CLIPS
{clips_block}

Respond with ONLY valid JSON, no markdown fences:
{{"clips":[{{"index":int,"title_options":[{{"title":"string","angle":"string","hook":int,"clarity":int,"search":int,"truth":int}}],"description_options":[{{"description":"string","angle":"string","hook":int,"search":int,"clarity":int,"truth":int}}],"tags":[{{"tag":"string","kind":"string"}}],"hashtags":["string"],"hook_text":"string","search_phrase":"string","why_it_works":"string"}}]}}"""


# The second pass: the same clips, packaged for the two feeds that do not have
# a title box. Asked after YouTube's so it can build on the search phrase and
# hook already chosen instead of inventing a second, competing angle.
SOCIAL_PROMPT = """You are an expert social media strategist and growth hacker. The clips below are already packaged for YouTube Shorts. Now package each one for INSTAGRAM REELS and TIKTOK.

Follow the growth playbook for each platform. They reward different things - Reels grow on sends and a caption's first 125 characters, TikTok is searched like a search engine - so write natively for each. Never paste the YouTube text.

GROWTH PLAYBOOK
{playbook}

{subject_block}

THE RULES THAT OUTRANK EVERYTHING BELOW:
1. ACCURATE. Nothing the clip does not deliver. Name a person, game or show only when that clip's block says the subject is CONFIRMED; otherwise use the genre.
2. NATIVE. Write the way the platform's best creators write: a person talking, not a press release. Specific and true beats sensational and empty.
3. FOR A STRANGER. Assume the viewer has never heard of this creator.
4. NO TWO CLIPS MAY OPEN THE SAME WAY. They are posted to one account.

For EACH clip write:

REELS
- "caption_options": exactly {caption_options} captions, each on a different angle:
    "search": leads with the phrase a person would type into Instagram search
    "send": written so a viewer forwards it to a friend
  Every caption: the hook and main keyword inside the first 125 characters; then one or two short lines of context; then a call to action that fits this clip (ask for a send, a save, or one specific comment). 150-400 characters. No hashtags inside the caption. At most two emoji.
  Score each honestly: "hook" 0-40 (does the first 125 characters stop a stranger?), "search" 0-25 (is a phrase people really search in the first line?), "share" 0-20 (would someone send or save it?), "truth" 0-15 (does the clip fully deliver it?). List them best first.
- "hashtags": 3-5, the most specific first, lowercase, no spaces. Never #shorts, #ytshorts, #fyp, #reels or #viral.
- "cover_text": 2-5 words for the cover image, readable in the centre of a 3:4 crop. No emoji, no hashtags, not the caption's opening words.
- "alt_text": one plain sentence under 100 characters saying what happens in the clip.

TIKTOK
- "caption": the search phrase in natural words in the first line, one line of context, then a question that invites comments. 100-300 characters. No hashtags inside the caption.
- "hashtags": 3-5: one broad community tag, then specific ones about the subject and the moment. Lowercase. Never #fyp, #foryou or #shorts.

CLIPS
{clips_block}

Respond with ONLY valid JSON, no markdown fences:
{{"clips":[{{"index":int,"reels":{{"caption_options":[{{"caption":"string","angle":"string","hook":int,"search":int,"share":int,"truth":int}}],"hashtags":["string"],"cover_text":"string","alt_text":"string"}},"tiktok":{{"caption":"string","hashtags":["string"]}}}}]}}"""


# Asked once per run, before any title is written. It names what the video is
# mostly about -- the fallback for any clip whose frames do not say -- and
# collects every other name that comes up, because a variety stream is about
# several things and the vision pass needs candidates to match against.
SUBJECT_PROMPT = """Identify what this video is ABOUT, as a viewer browsing YouTube would name it.

SOURCE VIDEO
{video_context}

WHAT IS SAID IN IT (samples from across the whole video)
{sample}

Rules:
- "subject" is the one thing a stranger would search for: the game being played, the show being watched, the person being interviewed, or the topic being discussed. Prefer the proper name over a category: "Elden Ring", not "a souls game". If the video genuinely has no nameable subject, or clearly covers several things equally, use an empty string rather than picking one.
- "kind" is one of: game, show, movie, person, sport, topic, none
- "format" is what kind of video it is: gameplay stream, variety stream, podcast, just chatting, storytelling, tutorial, irl, reaction, other
- "also_known_as" lists up to 3 other names real people use for the subject. Empty list if none.
- "all_subjects" lists EVERY game, show, product or named topic that the listing or the samples mention as being played, watched or discussed - up to 10. A variety stream will have several. Only names that are actually there.
- "hashtags" is 2-4 hashtags for the main subject and its niche, lowercase, no spaces, no #shorts.
- "tags" is 3-6 lowercase search terms for the main subject and its category.
- Never guess. Everything here must be supported by the listing or the samples above.

Respond with ONLY valid JSON:
{{"subject":"string","kind":"string","format":"string","also_known_as":["string"],"all_subjects":["string"],"hashtags":["string"],"tags":["string"]}}"""


def _spread_sample(segments: List[Dict], limit: int = 4000) -> str:
    """Lines from across the whole transcript, not just its first minutes.

    A variety stream spends its first ten minutes on the first game. Sampling
    only the start is how a three-game stream got described as one game.
    """
    if not segments:
        return ""
    want = 120
    step = max(1, len(segments) // want)
    picked = [str(s.get("text", "")).strip() for s in segments[::step]]
    return " ".join(p for p in picked if p)[:limit]


def detect_subject(video_meta: Optional[Dict], transcript: Optional[Dict] = None,
                   source: str = "", llm_fn: Optional[LLMFn] = None) -> Dict:
    """What the video is about, named once for the whole run.

    A title that does not name its subject is invisible outside the feed — it
    contains no word anyone would ever search or browse for. The subject is
    rarely in the transcript (nobody says "welcome to my Elden Ring stream"
    every ten minutes), so it is worked out once from the video's own listing
    plus a sample of what is said, and then handed to every clip whose own
    frames do not say otherwise.

    Best-effort like everything else here: an empty subject just means the
    titles are written from the clips alone, which is what happened before.
    """
    empty = {"subject": "", "kind": "none", "format": "", "also_known_as": [],
             "all_subjects": [], "hashtags": [], "tags": []}
    if llm_fn is None:
        return empty

    sample = _spread_sample((transcript or {}).get("segments") or [])
    prompt = SUBJECT_PROMPT.format(
        video_context=describe_video(video_meta, source),
        sample=sample or "(no transcript available)",
    )
    try:
        parsed = _parse_json_loose(llm_fn(prompt))
    except Exception as e:
        print(f"[seo] could not identify the subject ({e}) — "
              f"titles will be written from the clips alone", flush=True)
        return empty

    subject = re.sub(r"\s+", " ", str(parsed.get("subject") or "")).strip()[:80]
    out = {
        "subject": subject,
        "kind": str(parsed.get("kind") or "none").strip().lower()[:20],
        "format": str(parsed.get("format") or "").strip().lower()[:30],
        "also_known_as": [str(a).strip()[:40] for a in (parsed.get("also_known_as") or [])
                          if str(a).strip()][:3],
        "all_subjects": list(dict.fromkeys(
            str(a).strip()[:60] for a in (parsed.get("all_subjects") or [])
            if str(a).strip()))[:10],
        "hashtags": _clean_hashtags(parsed.get("hashtags"))[1:],   # drop the forced #shorts
        "tags": _clean_tags(parsed.get("tags"), []),
    }
    if subject:
        print(f"[seo] subject: {subject} ({out['kind']})", flush=True)
    others = [s for s in out["all_subjects"] if s.lower() != subject.lower()]
    if others:
        print(f"[seo] also in this video: {', '.join(others)}", flush=True)
    return out


def _same(a: str, b: str) -> bool:
    norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())
    return bool(norm(a)) and norm(a) == norm(b)


def clip_subject(h: Dict, run: Optional[Dict]) -> Dict:
    """What this one clip is filed under, and how sure that is.

    In order: what the person typed for this clip; a name the clip's own
    frames confirm; a visual guess that matches something the video itself
    mentions; the run's subject, as long as the frames do not point at
    something else. Anything less certain leaves the clip unnamed, filed by
    its genre instead -- which is honest, where a wrong name is not.
    """
    run = run or {}
    scene = h.get("scene") or {}
    genre = scene.get("genre") or ""
    base = {"genre": genre, "content_type": scene.get("content_type") or "",
            "guess": scene.get("subject_guess") or ""}

    override = str(h.get("subject_override") or "").strip()
    if override:
        return {**base, "subject": override, "confirmed": True, "by": "you", "guess": ""}
    if scene.get("subject"):
        return {**base, "subject": scene["subject"], "confirmed": True,
                "by": scene.get("named_by") or "frames"}

    guess = base["guess"]
    mentioned = list(run.get("all_subjects") or [])
    if guess and any(_same(guess, m) for m in mentioned):
        match = next(m for m in mentioned if _same(guess, m))
        return {**base, "subject": match, "confirmed": True, "by": "mentioned_in_video",
                "guess": ""}

    name = run.get("subject") or ""
    aliases = [name] + list(run.get("also_known_as") or [])
    if name and (not scene or not guess or any(_same(guess, a) for a in aliases)):
        return {**base, "subject": name, "confirmed": True, "by": "source_listing"}
    return {**base, "subject": "", "confirmed": False, "by": "none"}


def subject_block(subject: Dict) -> str:
    """The run-level subject, stated to the metadata writer as a default."""
    subject = subject or {}
    name = subject.get("subject") or ""
    mentioned = [s for s in (subject.get("all_subjects") or []) if not _same(s, name)]
    lines = []
    if name:
        aka = ", ".join(subject.get("also_known_as") or [])
        lines.append(f"SUBJECT OF THE VIDEO AS A WHOLE: {name} ({subject.get('kind')})"
                     + (f" - also called {aka}" if aka else ""))
    else:
        lines.append("SUBJECT OF THE VIDEO AS A WHOLE: (none identified)")
    if subject.get("format"):
        lines.append(f"Format: {subject['format']}")
    kind = content_kinds.normalise(subject.get("content_kind"))
    if kind != content_kinds.AUTO:
        lines.append(f"Kind of video, as the moments were chosen: {content_kinds.LABELS[kind]}. "
                     "Package it the way the best accounts in that format package theirs.")
    if mentioned:
        lines.append("It also covers: " + ", ".join(mentioned))
    lines.append(
        "This is only the default. Each clip's WHAT IS ON SCREEN block says what THAT "
        "clip is, and a clip is named after the subject its block gives - which may "
        "differ from this one. Every title must place its clip: by naming the confirmed "
        "subject outright, or, when there is none, by naming the genre and the specific "
        "thing that happens. A title that would fit any video on the platform is a "
        "title nobody can find.")
    return "\n".join(lines)


def _scene_block(h: Dict, run: Optional[Dict]) -> str:
    who = clip_subject(h, run)
    scene = h.get("scene") or {}
    if who["confirmed"]:
        about = f"{who['subject']} (CONFIRMED - by {who['by'].replace('_', ' ')})"
    elif who["guess"]:
        about = (f"not confirmed. It looks like it could be {who['guess']}, but NOTHING "
                 f"confirms it - do NOT name {who['guess']}; use the genre")
    else:
        about = "not named - use the genre and what happens"
    lines = [f"Subject: {about}"]
    if scene:
        lines.append(f"Kind of video: {scene.get('content_type', 'other')}"
                     + (f" - {scene['layout']}" if scene.get("layout") else ""))
        if scene.get("genre"):
            lines.append(f"Genre: {scene['genre']}")
        if scene.get("scene"):
            lines.append(f"What happens on screen: {scene['scene']}")
        if scene.get("on_screen_text"):
            lines.append(f"Text on screen: {scene['on_screen_text']}")
        if scene.get("mood"):
            lines.append(f"Mood: {scene['mood']}")
    else:
        lines.append("(The frames could not be looked at - judge the kind of video from "
                     "the words, and do not assume it is gameplay.)")
    return "\n".join(lines)


def _clip_transcript(transcript: Optional[Dict], start: float, end: float,
                     limit: int = 1600) -> str:
    """The spoken words inside a clip's span.

    This is what keeps the metadata honest - the model writes about the words
    in front of it rather than about the video in general.
    """
    if not transcript:
        return ""
    parts = []
    for seg in transcript.get("segments", []):
        try:
            s, e = float(seg["start"]), float(seg["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if e < start or s > end:
            continue
        text = str(seg.get("text", "")).strip()
        if text:
            parts.append(text)
    return " ".join(parts)[:limit]


def clip_words(h: Dict, transcript: Optional[Dict]) -> str:
    """What is said in a highlight, from its own copy or from the source's."""
    said = str(h.get("transcript_text") or "").strip()[:1600]
    start = float(h.get("start_time", 0) or 0)
    end = float(h.get("end_time", 0) or 0)
    return said or _clip_transcript(transcript, start, end)


def describe_video(meta: Optional[Dict], source: str = "") -> str:
    """Context block about the source video, straight from its own listing."""
    meta = meta or {}
    lines = []
    if meta.get("title"):
        lines.append(f"Title: {meta['title']}")
    if meta.get("uploader"):
        lines.append(f"Channel: {meta['uploader']}")
    if meta.get("categories"):
        lines.append(f"Category: {', '.join(str(c) for c in meta['categories'][:3])}")
    if meta.get("tags"):
        lines.append("The full video's own tags: "
                     + ", ".join(str(t) for t in meta["tags"][:20]))
    if meta.get("description"):
        lines.append(f"Description: {str(meta['description'])[:600]}")
    link = meta.get("webpage_url") or source
    if link and str(link).startswith("http"):
        lines.append(f"Link: {link}")
    return "\n".join(lines) or (
        "(No metadata for the source - judge the topic from the clips alone, "
        "and do not guess at anything outside them.)"
    )


def _build_clips_block(highlights: List[Dict], transcript: Optional[Dict],
                       run: Optional[Dict] = None) -> str:
    blocks = []
    total = len(highlights)
    for i, h in enumerate(highlights, 1):
        start = float(h.get("start_time", 0) or 0)
        end = float(h.get("end_time", 0) or 0)
        # A clip restored from disk carries its own transcript instead of a
        # span into the source's — either way the model sees real words.
        said = clip_words(h, transcript) or (
            "(no speech in this span - write from what is on screen)"
        )
        blocks.append(
            f"--- CLIP {i} (rank {i} of {total}, "
            f"viral score {h.get('score', 'n/a')}, {max(0.0, end - start):.0f}s)\n"
            f"Working title: {h.get('title', '')}\n"
            f"Opens on this line: {h.get('first_line') or h.get('hook_sentence', '')}\n"
            f"Why it was picked: {h.get('virality_reason', '')}\n"
            f"WHAT IS ON SCREEN:\n{_scene_block(h, run)}\n"
            f"WHAT IS ACTUALLY SAID: {said}"
        )
    return "\n\n".join(blocks)


def _parse_json_loose(raw: str) -> Dict:
    text = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1:
            return json.loads(text[start:end + 1])
        raise


def _is_quota(e: BaseException) -> bool:
    """Whether a model call failed because the day's allowance is spent."""
    said = str(e).lower()
    return "quota" in said or "resource_exhausted" in said or "rate limit" in said


def _index_items(items: object, n: int) -> Dict[int, Dict]:
    """The model's per-clip answers, keyed 1..n.

    Trusted by the "index" each one carries only when those indices are
    distinct and in range. A model that numbers every answer 1, or counts
    from 0, would otherwise pile ten answers into one slot and leave nine
    clips with nothing -- so then the order they came back in decides.
    """
    items = [i for i in (items if isinstance(items, list) else []) if isinstance(i, dict)]
    said = []
    for item in items:
        try:
            said.append(int(item.get("index")))
        except (TypeError, ValueError):
            said.append(None)
    if all(k is not None and 1 <= k <= n for k in said) and len(set(said)) == len(said):
        return dict(zip(said, items))
    return {k: item for k, item in enumerate(items[:n], 1)}


def _clean_tag(tag: object) -> str:
    tag = re.sub(r"[#,\n]", " ", str(tag or "")).strip().lower()
    return re.sub(r"\s+", " ", tag)[:TAG_LIMIT].strip()


def _clean_tags(raw: object, fallback: List[str], limit: int = MAX_TAGS) -> List[str]:
    """Normalize a tag list and keep it inside YouTube's 500-character budget."""
    items: List[str] = []
    if isinstance(raw, str):
        items = raw.split(",")
    elif isinstance(raw, list):
        items = [str(t.get("tag") if isinstance(t, dict) else t) for t in raw]

    seen, out, total = set(), [], 0
    for tag in (items or fallback):
        tag = _clean_tag(tag)
        if not tag or tag in seen:
            continue
        # +1 for the separating comma, which counts against the limit too.
        if total + len(tag) + 1 > TAGS_TOTAL_LIMIT or len(out) >= limit:
            break
        seen.add(tag)
        out.append(tag)
        total += len(tag) + 1
    return out


def _clean_hashtags(raw: object) -> List[str]:
    """Normalize hashtags, and make sure #shorts leads.

    YouTube renders the first three hashtags in a description as clickable
    links above the title, so their order is not cosmetic - it is the most
    valuable metadata slot on the whole upload. #shorts goes first because it
    is what tells the platform the video belongs in the Shorts feed at all.
    """
    items = raw if isinstance(raw, list) else []
    out, seen = [], set()
    for tag in items:
        tag = "#" + re.sub(r"[^A-Za-z0-9_]", "", str(tag))
        if len(tag) < 3 or tag.lower() in seen:
            continue
        seen.add(tag.lower())
        out.append(tag)
        if len(out) >= MAX_HASHTAGS:
            break

    if "#shorts" not in seen:
        out = ["#shorts"] + out[:MAX_HASHTAGS - 1]
    else:
        out.sort(key=lambda t: t.lower() != "#shorts")
    return out


def _strip_title_hashtags(title: str) -> str:
    """Take the hashtags back out of a title.

    They earn nothing there - the ones that get shown come from the description
    - and they spend characters the title needs for the words someone might
    actually search for.
    """
    title = re.sub(r"#\w+", " ", title)
    return re.sub(r"\s+", " ", title).strip(" -|,")


def _as_hashtag(name: str) -> str:
    tag = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    return f"#{tag}" if len(tag) >= 2 else ""


def _controlled_hashtags(who: Dict, run: Optional[Dict]) -> List[str]:
    """The hashtags a clip is filed under before the model's own suggestions.

    The run's controlled set only applies to clips that are about the run's
    subject. A Firewatch clip from a stream about The Finals files under
    #firewatch, not #thefinals.
    """
    run = run or {}
    if who.get("confirmed") and _same(who.get("subject", ""), run.get("subject", "")):
        return list(run.get("hashtags") or [])
    own = _as_hashtag(who.get("subject", "")) if who.get("confirmed") else ""
    return [own] if own else []


def _merge_hashtags(model_tags: List[str], controlled: List[str]) -> List[str]:
    """#shorts, then the clip's controlled tags, then whatever the model added.

    Order is the whole point: YouTube renders the first three as links above
    the title, so the slots are spent on the terms this clip should be filed
    under rather than on whatever a model reached for that clip. The model's
    own suggestions are kept, just behind them -- it sometimes catches
    something clip-specific worth having.
    """
    out, seen = [], set()
    for tag in ["#shorts"] + list(controlled) + list(model_tags):
        key = tag.lower()
        if key in seen or len(tag) < 3:
            continue
        seen.add(key)
        out.append(tag)
        if len(out) >= MAX_HASHTAGS:
            break
    return out


def _subject_terms(who: Dict, run: Optional[Dict]) -> List[str]:
    """The clip's own subject and its aliases, as tags."""
    if not who.get("confirmed"):
        return [who["genre"]] if who.get("genre") else []
    name = who["subject"]
    terms = [name, name.replace(" ", "")] if " " in name else [name]
    run = run or {}
    if _same(name, run.get("subject", "")):
        terms += list(run.get("also_known_as") or []) + list(run.get("tags") or [])
    if who.get("genre"):
        terms.append(who["genre"])
    return [t for t in terms if t]


def _format_terms(who: Dict) -> List[str]:
    return FORMAT_TAGS.get(who.get("content_type") or "", []) + ["shorts"]


def score_title(title: str, who: Dict) -> int:
    """The app's own check on a title, 0-100, independent of the model.

    The model is good at judging a hook and bad at counting. This catches what
    it misses: titles too short to carry a subject or too long to be read,
    a confirmed subject left out, shouting, filler, hashtags.
    """
    t = title.strip()
    low = t.lower()
    score = 100
    n = len(t)
    if n < 25:
        score -= 30
    elif n < 35:
        score -= 10
    elif n > 70:
        score -= min(35, (n - 70) + 10)

    name = who.get("subject") if who.get("confirmed") else ""
    if name:
        at = low.find(name.lower())
        if at < 0 and not _same(name, re.sub(r"[^a-z0-9]", "", low)):
            score -= 25
        elif at > 40:
            score -= 8           # named, but past where a phone shows it
    elif who.get("guess") and who["guess"].lower() in low:
        score -= 60              # printed a name nothing confirmed

    shouty = [w for w in re.findall(r"[A-Za-z]{4,}", t) if w.isupper()]
    if shouty:
        score -= 10 * min(3, len(shouty))
    if "#" in t:
        score -= 20
    if any(f in low for f in FILLER):
        score -= 15
    if t.endswith("..."):
        score -= 5
    emoji = sum(1 for ch in t if ord(ch) > 0x2600)
    if emoji > 1:
        score -= 10 * (emoji - 1)
    if re.search(r"\d", t) or t.endswith("?"):
        score += 4               # a number or a question is a concrete hook
    return max(0, min(100, score))


def _mentions(text: str, who: Dict, search_phrase: str = "") -> Optional[bool]:
    """Does `text` carry the clip's subject or search phrase? None if it has neither."""
    low = (text or "").lower()
    terms = []
    if who.get("confirmed") and who.get("subject"):
        terms.append(who["subject"].lower())
    if search_phrase:
        terms.append(search_phrase.lower())
    if not terms:
        return None
    squashed = re.sub(r"[^a-z0-9]", "", low)
    for term in terms:
        if term in low or re.sub(r"[^a-z0-9]", "", term) in squashed:
            return True
        # A phrase counts when most of its words are there, in any order.
        words = [w for w in re.findall(r"[a-z0-9']+", term) if len(w) > 2]
        if words and sum(w in low for w in words) >= max(1, round(len(words) * 0.66)):
            return True
    return False


def _shared_checks(text: str, who: Dict) -> int:
    """Penalties every piece of packaging shares: filler, shouting, a wrong name."""
    low = text.lower()
    penalty = 0
    if any(f in low for f in FILLER):
        penalty += 15
    shouty = [w for w in re.findall(r"[A-Za-z]{4,}", text) if w.isupper()]
    penalty += 10 * min(3, len(shouty))
    if not who.get("confirmed") and who.get("guess") and who["guess"].lower() in low:
        penalty += 60
    return penalty


def score_description(text: str, who: Dict, search_phrase: str = "") -> int:
    """The app's own check on a YouTube description, 0-100.

    The first line is the one most viewers see and the one YouTube leans on to
    place the clip, so that is where the subject and search phrase have to be.
    """
    t = (text or "").strip()
    body = re.sub(r"#\w+", "", t).strip()
    first = body.splitlines()[0] if body else ""
    score = 100
    n = len(body)
    if n < 80:
        score -= 25
    elif n > 600:
        score -= 15
    if _mentions(first, who, search_phrase) is False:
        score -= 25
    tags = re.findall(r"#\w+", t)
    if not tags:
        score -= 10
    elif len(tags) > MAX_HASHTAGS:
        score -= 10
    if "?" in body:
        score += 4
    score -= _shared_checks(t, who)
    return max(0, min(100, score))


def score_caption(text: str, who: Dict, search_phrase: str = "",
                  platform: str = "reels") -> int:
    """The app's own check on a Reels or TikTok caption, 0-100.

    Both platforms search captions, and Instagram folds a caption after about
    125 characters, so the keyword has to land before the fold. Hashtags belong
    in their own line, not in the sentence.
    """
    t = (text or "").strip()
    score = 100
    n = len(t)
    if n < 60:
        score -= 25
    elif n > (600 if platform == "reels" else 450):
        score -= 15
    if _mentions(t[:CAPTION_FOLD], who, search_phrase) is False:
        score -= 20
    if "#" in t:
        score -= 15
    emoji = sum(1 for ch in t if ord(ch) > 0x2600)
    if emoji > 2:
        score -= 10
    low = t.lower()
    if t.rstrip().endswith("?") or re.search(r"\b(send|save|tag|share)\b", low):
        score += 5
    score -= _shared_checks(t, who)
    return max(0, min(100, score))


def _rubric(item: Dict, caps: Dict[str, int]) -> Optional[int]:
    """The model's own scores for an option, summed, or None if any is missing."""
    total = 0
    for key, cap in caps.items():
        try:
            total += max(0, min(cap, int(item.get(key))))
        except (TypeError, ValueError):
            return None
    return total


def _truth_ok(item: Dict, key: str = "truth", floor: int = 11) -> bool:
    """False only when the model itself scored an option as overclaiming."""
    try:
        return int(item.get(key)) >= floor
    except (TypeError, ValueError):
        return True


def _rank_texts(raw: object, field: str, caps: Dict[str, int], check, limit: int,
                keep: int) -> List[Dict]:
    """Rank model-written text options -- descriptions, captions -- best first.

    The same blend as titles: mostly the model's rubric, partly the app's check.
    """
    options, seen = [], set()
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            item = {field: item}
        if not isinstance(item, dict):
            continue
        text = str(item.get(field) or "").strip()[:limit]
        key = re.sub(r"\s+", " ", text.lower())
        if not text or key in seen or not _truth_ok(item):
            continue
        seen.add(key)
        rubric = _rubric(item, caps)
        own = check(text)
        options.append({
            field: text,
            "angle": str(item.get("angle") or "").strip().lower()[:20],
            "score": round(0.7 * rubric + 0.3 * own) if rubric is not None else own,
            "model_score": rubric,
            "check_score": own,
        })
    options.sort(key=lambda o: o["score"], reverse=True)
    for rank, o in enumerate(options, 1):
        o["rank"] = rank
    return options[:keep]


def _rank_options(raw: object, who: Dict) -> List[Dict]:
    """Model-written title options, scored and ordered best first.

    The final score is mostly the model's rubric (it can judge a hook) and
    partly the app's own check (it can count). An option the model itself
    marked as not fully true is dropped outright: accuracy is rule one.
    """
    options, seen = [], set()
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            item = {"title": item}
        if not isinstance(item, dict):
            continue
        title = _strip_title_hashtags(re.sub(r"\s+", " ", str(item.get("title") or "")))
        title = title[:TITLE_LIMIT].strip()
        if not title or title.lower() in seen:
            continue
        seen.add(title.lower())

        def part(key, cap):
            try:
                return max(0, min(cap, int(item.get(key))))
            except (TypeError, ValueError):
                return None
        hook, clarity, search, truth = (part("hook", 40), part("clarity", 20),
                                        part("search", 20), part("truth", 20))
        if truth is not None and truth < 14:
            continue
        rubric = sum(v for v in (hook, clarity, search, truth) if v is not None)
        rated = all(v is not None for v in (hook, clarity, search, truth))
        check = score_title(title, who)
        final = round(0.7 * rubric + 0.3 * check) if rated else check
        options.append({
            "title": title,
            "angle": str(item.get("angle") or "").strip().lower()[:20],
            "score": final,
            "model_score": rubric if rated else None,
            "check_score": check,
        })
    options.sort(key=lambda o: o["score"], reverse=True)
    for rank, o in enumerate(options, 1):
        o["rank"] = rank
    return options[:TITLE_OPTIONS + 2]


def _tag_options(raw: object, who: Dict, run: Optional[Dict],
                 extra: List[str]) -> List[Dict]:
    """Every tag worth offering, ranked, each with the kind of term it is.

    The clip's own subject leads whatever the model returned: it is the one
    term someone might actually search, and a clip filed without it is
    findable only by accident.
    """
    out, seen = [], set()

    def add(tag, kind):
        tag = _clean_tag(tag)
        if tag and tag not in seen and len(out) < MAX_TAG_OPTIONS:
            seen.add(tag)
            out.append({"tag": tag, "kind": kind})

    subject_kind = "subject" if who.get("confirmed") else "genre"
    for t in _subject_terms(who, run):
        add(t, "genre" if _same(t, who.get("genre") or "") else subject_kind)
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            add(item.get("tag"), str(item.get("kind") or "query").strip().lower()[:12])
        else:
            add(item, "query")
    for t in extra:
        add(t, "format")
    # A name the frames only guessed must not reach the tag box by the back
    # door either.
    guess = (who.get("guess") or "").lower()
    if guess:
        out = [o for o in out if guess not in o["tag"]]
    return _score_tags(out)


def _score_tags(options: List[Dict]) -> List[Dict]:
    """Give every tag a score and put them in that order.

    What kind of term a tag is counts for most of it, and where the model put
    it for the rest -- so a real search query the model ranked high beats a
    format term it added to fill the list, and the subject stays on top.
    """
    for pos, o in enumerate(options):
        weight = TAG_KIND_WEIGHT.get(o.get("kind") or "", 70)
        o["score"] = round(0.6 * weight + 0.4 * max(0, 100 - 5 * pos))
    options.sort(key=lambda o: o["score"], reverse=True)
    for rank, o in enumerate(options, 1):
        o["rank"] = rank
    return options


def _pick_tags(options: List[Dict]) -> List[str]:
    return _clean_tags([o["tag"] for o in options], [])


def _fallback_for(h: Dict, video_meta: Optional[Dict],
                  subject: Optional[Dict] = None) -> Dict:
    """Metadata derived from the highlight alone, when the LLM can't be reached.

    Weaker than the model's version, but every word of it comes from the clip's
    own hook line and its frames, so it stays accurate - and a user can copy
    and upload it, which beats an empty box next to a finished clip.
    """
    who = clip_subject(h, subject)
    title = (h.get("hook_sentence") or h.get("title") or "Watch this").strip()
    title = re.sub(r"\s+", " ", title).rstrip(" .,-")
    if who.get("confirmed") and who["subject"].lower() not in title.lower():
        title = f"{title} | {who['subject']}"
    if len(title) > TITLE_LIMIT - 8:
        title = title[:TITLE_LIMIT - 11].rstrip() + "..."
    hashtags = _merge_hashtags(["#clips", "#viral"], _controlled_hashtags(who, subject))

    words = re.findall(r"[a-z]{4,}", f"{title} {h.get('virality_reason', '')}".lower())
    topic = list(dict.fromkeys(words))[:6]
    source_title = (video_meta or {}).get("title", "")
    options = _tag_options([], who, subject, topic + _format_terms(who))

    description = "\n".join([
        title,
        "",
        f"Clipped from: {source_title}" if source_title else "Full video on the channel.",
        "Follow for more clips like this.",
        "",
        " ".join(hashtags),
    ])
    entry = {
        "title": title[:TITLE_LIMIT],
        "title_options": [{"title": title[:TITLE_LIMIT], "angle": "hook line",
                           "score": score_title(title, who), "model_score": None,
                           "check_score": score_title(title, who), "rank": 1}],
        "description": description,
        "description_options": [{"description": description, "angle": "hook line",
                                 "score": score_description(description, who),
                                 "model_score": None,
                                 "check_score": score_description(description, who),
                                 "rank": 1}],
        "tags": _pick_tags(options),
        "tag_options": options,
        "hashtags": hashtags,
        "hook_text": (h.get("hook_sentence") or h.get("title") or "")[:HOOK_LIMIT],
        "search_phrase": "",
        "why_it_works": h.get("virality_reason", ""),
        "about": _about(who),
        "generated": False,
    }
    entry.update(_social_fallback(entry, h, who, subject))
    return entry


def _social_hashtags(raw: object, controlled: List[str]) -> List[str]:
    """3-5 hashtags for Reels or TikTok: the clip's own terms first, nothing off-platform."""
    out, seen = [], set()
    items = list(controlled) + (list(raw) if isinstance(raw, list) else [])
    for tag in items:
        tag = "#" + re.sub(r"[^A-Za-z0-9_]", "", str(tag)).lower()
        if len(tag) < 3 or tag in seen or tag in OFF_PLATFORM_HASHTAGS:
            continue
        seen.add(tag)
        out.append(tag)
        if len(out) >= MAX_HASHTAGS:
            break
    return out


def _plain(text: str) -> str:
    """Packaging text with its hashtags taken out and its spacing tidied."""
    text = re.sub(r"#\w+", "", text or "")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _social_fallback(entry: Dict, h: Dict, who: Dict, run: Optional[Dict]) -> Dict:
    """Reels and TikTok packaging built from the YouTube packaging.

    Used when the second pass could not run. Not native to either platform,
    but accurate -- every word already passed the YouTube checks -- and
    stripped of the parts that would be wrong there: #shorts, and hashtags
    inside the sentence.
    """
    caption = _plain(entry.get("description") or entry.get("title") or "")[:600]
    tags = _social_hashtags(entry.get("hashtags"), _controlled_hashtags(who, run))
    phrase = entry.get("search_phrase") or ""
    scene = (h.get("scene") or {}).get("scene") or ""
    cover = " ".join(re.findall(r"\S+", entry.get("hook_text") or entry.get("title") or "")[:5])
    return {
        "reels": {
            "caption": caption,
            "caption_options": [{"caption": caption, "angle": "from youtube",
                                 "score": score_caption(caption, who, phrase, "reels"),
                                 "model_score": None,
                                 "check_score": score_caption(caption, who, phrase, "reels"),
                                 "rank": 1}],
            "hashtags": tags,
            "cover_text": cover[:COVER_LIMIT],
            "alt_text": (scene or entry.get("title") or "")[:ALT_TEXT_LIMIT],
            "generated": False,
        },
        "tiktok": {
            "caption": caption[:TIKTOK_CAPTION_LIMIT],
            "score": score_caption(caption, who, phrase, "tiktok"),
            "hashtags": tags,
            "generated": False,
        },
    }


def _about(who: Dict) -> Dict:
    """What the clip was filed under, for the UI to show and let someone fix."""
    return {k: who.get(k) for k in ("subject", "confirmed", "by", "guess", "genre",
                                     "content_type")}


def _coerce_entry(item: Dict, h: Dict, video_meta: Optional[Dict],
                  subject: Optional[Dict] = None) -> Dict:
    """Force one model entry into shape, filling any gap from the fallback."""
    base = _fallback_for(h, video_meta, subject)
    who = clip_subject(h, subject)

    options = _rank_options(item.get("title_options"), who)
    if not options and item.get("title"):
        options = _rank_options([{"title": item.get("title")}], who)
    options = options or base["title_options"]
    title = options[0]["title"]

    phrase = re.sub(r"\s+", " ", str(item.get("search_phrase") or "")).strip().lower()[:60]
    hashtags = (_merge_hashtags(_clean_hashtags(item.get("hashtags")),
                                _controlled_hashtags(who, subject)) or base["hashtags"])

    def with_hashtags(text: str) -> str:
        if any(t.lower() in text.lower() for t in hashtags):
            return text
        return f"{text}\n\n{' '.join(hashtags)}"[:DESCRIPTION_LIMIT]

    raw_descriptions = item.get("description_options")
    if not raw_descriptions and item.get("description"):
        raw_descriptions = [{"description": item.get("description")}]
    descriptions = _rank_texts(
        [{**d, "description": with_hashtags(str(d.get("description") or "").strip())}
         if isinstance(d, dict) else {"description": with_hashtags(str(d))}
         for d in (raw_descriptions or []) if d],
        "description", {"hook": 40, "search": 30, "clarity": 15, "truth": 15},
        lambda t: score_description(t, who, phrase), DESCRIPTION_LIMIT, DESCRIPTION_OPTIONS + 1,
    ) or base["description_options"]
    description = descriptions[0]["description"]

    model_tags = item.get("tags")
    if isinstance(model_tags, str):
        model_tags = model_tags.split(",")
    tag_opts = _tag_options(model_tags, who, subject, _format_terms(who))
    hook = re.sub(r"\s+", " ", str(item.get("hook_text") or "")).strip()[:HOOK_LIMIT]

    entry = {
        "title": title,
        "title_options": options,
        "description": description,
        "description_options": descriptions,
        "tags": _pick_tags(tag_opts) or base["tags"],
        "tag_options": tag_opts or base["tag_options"],
        "hashtags": hashtags,
        "hook_text": hook or base["hook_text"],
        "search_phrase": phrase,
        "why_it_works": str(item.get("why_it_works") or h.get("virality_reason") or "").strip(),
        "about": _about(who),
        "generated": True,
    }
    # Until the second pass writes them, Reels and TikTok get the YouTube
    # packaging made safe for them -- never the other video's leftovers.
    entry.update(_social_fallback(entry, h, who, subject))
    return entry


def _coerce_social(item: Dict, entry: Dict, h: Dict, who: Dict,
                   run: Optional[Dict]) -> Dict:
    """Force one clip's Reels and TikTok packaging into shape."""
    base = _social_fallback(entry, h, who, run)
    phrase = entry.get("search_phrase") or ""
    controlled = _controlled_hashtags(who, run)
    out = {}

    reels = item.get("reels") if isinstance(item.get("reels"), dict) else None
    if reels:
        captions = _rank_texts(
            [{**c, "caption": _plain(str(c.get("caption") or ""))} if isinstance(c, dict)
             else {"caption": _plain(str(c))} for c in (reels.get("caption_options") or [])],
            "caption", {"hook": 40, "search": 25, "share": 20, "truth": 15},
            lambda t: score_caption(t, who, phrase, "reels"), REELS_CAPTION_LIMIT,
            CAPTION_OPTIONS + 1,
        )
        cover = _strip_title_hashtags(re.sub(r"\s+", " ", str(reels.get("cover_text") or "")))
        alt = re.sub(r"\s+", " ", str(reels.get("alt_text") or "")).strip()
        if captions:
            out["reels"] = {
                "caption": captions[0]["caption"],
                "caption_options": captions,
                "hashtags": _social_hashtags(reels.get("hashtags"), controlled)
                            or base["reels"]["hashtags"],
                "cover_text": cover[:COVER_LIMIT] or base["reels"]["cover_text"],
                "alt_text": alt[:ALT_TEXT_LIMIT] or base["reels"]["alt_text"],
                "generated": True,
            }

    tiktok = item.get("tiktok") if isinstance(item.get("tiktok"), dict) else None
    if tiktok and str(tiktok.get("caption") or "").strip():
        caption = _plain(str(tiktok["caption"]))[:TIKTOK_CAPTION_LIMIT]
        out["tiktok"] = {
            "caption": caption,
            "score": score_caption(caption, who, phrase, "tiktok"),
            "hashtags": _social_hashtags(tiktok.get("hashtags"), controlled)
                        or base["tiktok"]["hashtags"],
            "generated": True,
        }
    return {**base, **out}


def _spread_leads(entries: List[Dict]) -> None:
    """Keep two clips in one batch from leading with the same words.

    The prompt asks for it; this makes sure of it. Where a clip's best title
    opens like an earlier clip's, its next-best option that does not is used
    instead -- they will be posted to the same channel, and ten titles opening
    the same way compete for one slice of the feed.
    """
    def lead(t):
        return " ".join(re.findall(r"[a-z0-9']+", t.lower())[:3])
    used = set()
    for e in entries:
        options = e.get("title_options") or []
        if lead(e["title"]) in used:
            alt = next((o for o in options if lead(o["title"]) not in used), None)
            if alt:
                e["title"] = alt["title"]
        used.add(lead(e["title"]))


def apply_edit(existing: Optional[Dict], edit: Dict) -> Dict:
    """Merge a person's own wording into a clip's metadata.

    Held to the same limits the model's output is held to — a title over 100
    characters is rejected by YouTube whoever typed it — but not to the same
    rules. The house style says no hashtags in a title and a controlled tag
    vocabulary; those exist to stop a model padding, and a person who types a
    hashtag into their own title meant it. So the shape is enforced and the
    judgement is not.

    Only the fields actually present in `edit` change. Everything else on the
    clip, including the title options and why_it_works, survives untouched.
    """
    out = dict(existing or {})
    out.setdefault("generated", False)

    if "title" in edit:
        title = re.sub(r"\s+", " ", str(edit["title"] or "")).strip()
        if not title:
            raise ValueError("A title can't be empty.")
        if len(title) > TITLE_LIMIT:
            raise ValueError(f"Titles have to be {TITLE_LIMIT} characters or fewer "
                             f"— that one is {len(title)}.")
        out["title"] = title

    if "description" in edit:
        out["description"] = str(edit["description"] or "").strip()[:DESCRIPTION_LIMIT]

    if "tags" in edit:
        raw = edit["tags"]
        out["tags"] = _clean_tags(raw if isinstance(raw, list) else str(raw or ""), [])

    if "hashtags" in edit:
        out["hashtags"] = _clean_hashtags(edit["hashtags"])

    if "hook_text" in edit:
        out["hook_text"] = re.sub(r"\s+", " ",
                                  str(edit["hook_text"] or "")).strip()[:HOOK_LIMIT]

    # Reels and TikTok, as typed. Hashtags come in as the one line the box
    # shows ("#a #b") or as a list; either way they are kept as the person
    # wrote them, only shaped.
    def hashtag_list(raw: object) -> List[str]:
        items = raw if isinstance(raw, list) else re.split(r"[\s,]+", str(raw or ""))
        out_tags, seen = [], set()
        for tag in items:
            tag = "#" + re.sub(r"[^\w]", "", str(tag).lstrip("#"))
            if len(tag) >= 2 and tag.lower() not in seen:
                seen.add(tag.lower())
                out_tags.append(tag)
        return out_tags[:30]

    reels = dict(out.get("reels") or {})
    if "reels_caption" in edit:
        reels["caption"] = str(edit["reels_caption"] or "").strip()[:REELS_CAPTION_LIMIT]
    if "reels_hashtags" in edit:
        reels["hashtags"] = hashtag_list(edit["reels_hashtags"])
    if "reels_cover_text" in edit:
        reels["cover_text"] = re.sub(r"\s+", " ", str(edit["reels_cover_text"] or "")).strip()[:COVER_LIMIT]
    if "reels_alt_text" in edit:
        reels["alt_text"] = re.sub(r"\s+", " ", str(edit["reels_alt_text"] or "")).strip()[:ALT_TEXT_LIMIT]
    if reels != (out.get("reels") or {}):
        out["reels"] = reels

    tiktok = dict(out.get("tiktok") or {})
    if "tiktok_caption" in edit:
        tiktok["caption"] = str(edit["tiktok_caption"] or "").strip()[:TIKTOK_CAPTION_LIMIT]
    if "tiktok_hashtags" in edit:
        tiktok["hashtags"] = hashtag_list(edit["tiktok_hashtags"])
    if tiktok != (out.get("tiktok") or {}):
        out["tiktok"] = tiktok

    # So a later "Rewrite" is a deliberate choice rather than a surprise: the
    # UI can warn that it is about to throw away words a person wrote.
    out["edited"] = True
    return out


def generate_seo(
    highlights: List[Dict],
    transcript: Optional[Dict] = None,
    video_meta: Optional[Dict] = None,
    source: str = "",
    llm_fn: Optional[LLMFn] = None,
    errors: Optional[List[str]] = None,
    subject: Optional[Dict] = None,
    avoid_titles: Optional[List[str]] = None,
) -> List[Dict]:
    """Upload metadata for each highlight, in the order given (best first).

    Each highlight may carry a `scene` (what its frames show) and a
    `subject_override` (what a person said it is); both decide what the clip
    is filed under. `avoid_titles` are titles already used elsewhere on the
    channel -- the rest of the run, when only one clip is being rewritten.

    Falling back to filename-derived titles is the right behaviour mid-render —
    a run must not die because the metadata step failed. But the caller has to
    be able to tell the difference, so anything that went wrong is appended to
    `errors` rather than only printed. Each returned entry also carries
    `generated`, saying whether a model actually wrote it.
    """
    if not highlights:
        return []

    if llm_fn is None:
        return [_fallback_for(h, video_meta, subject) for h in highlights]

    # Named once for the batch unless the caller already did it -- ten clips
    # from one video share one listing, and asking ten times would invite
    # ten answers.
    if subject is None:
        subject = detect_subject(video_meta, transcript, source, llm_fn)

    avoid = [t for t in (avoid_titles or []) if t]
    avoid_block = ("\nALREADY POSTED FROM THIS VIDEO - do not open any title the way these open:\n"
                   + "\n".join(f"- {t}" for t in avoid[:12]) + "\n") if avoid else ""
    guide = playbook.prompt_text()
    _, where = playbook.load()
    print(f"[seo] writing for YouTube Shorts, Instagram Reels and TikTok from the {where}",
          flush=True)

    def ask(which: List[int]) -> Dict[int, Dict]:
        """Metadata for the clips at these 1-based positions, keyed the same way."""
        subset = [highlights[i - 1] for i in which]
        prompt = SEO_PROMPT.format(
            playbook=guide or "(no playbook available - rely on what you know)",
            n=len(subset),
            options=TITLE_OPTIONS,
            description_options=DESCRIPTION_OPTIONS,
            title_limit=TITLE_LIMIT,
            video_context=describe_video(video_meta, source),
            subject_block=subject_block(subject),
            clips_block=_build_clips_block(subset, transcript, subject),
            avoid_block=avoid_block,
        )
        got = _index_items(_parse_json_loose(llm_fn(prompt)).get("clips"), len(subset))
        return {which[k - 1]: item for k, item in got.items()}

    # One request for the whole batch, then the clips it left out asked for
    # again in small groups. Two Dying Light runs came back with 1 of 10 clips
    # written and nine hook-line fallbacks -- "Holy shit | Dying Light" among
    # them -- and those went to YouTube. A clip the model skipped is worth
    # another question before it is worth a fallback.
    #
    # Except when the day's quota is gone. Titles are the last thing a run
    # asks for, after ranking and looking have spent their share, so on a
    # long stream this is the step that meets the limit -- and asking again
    # one clip at a time only hears the same answer ten times.
    by_index: Dict[int, Dict] = {}
    todo = list(range(1, len(highlights) + 1))
    out_of_quota = ""
    for attempt, size in enumerate((len(todo), 3, 1)):
        if not todo or out_of_quota:
            break
        if attempt:
            print(f"[seo] {len(todo)} clip(s) came back without metadata - asking again "
                  f"{size} at a time", flush=True)
        for lo in range(0, len(todo), size):
            group = todo[lo:lo + size]
            try:
                by_index.update(ask(group))
            except Exception as e:
                print(f"[seo] could not generate metadata for clip(s) {group} ({e})",
                      flush=True)
                if errors is not None and (attempt == 2 or _is_quota(e)):
                    errors.append(str(e))
                if _is_quota(e):
                    out_of_quota = str(e)
                    break
        todo = [i for i in todo if i not in by_index]
    print(f"[seo] wrote metadata for {len(by_index)}/{len(highlights)} clip(s)"
          + (" - the rest fall back to their hook lines" if todo else ""), flush=True)
    if todo and out_of_quota:
        print(f"[seo] {len(todo)} title(s) could not be written because the model's "
              f"quota ran out. They will not be uploaded as they are: use Rewrite "
              f"titles once it resets, or add a second provider's key in Settings.",
              flush=True)

    out = []
    for i, h in enumerate(highlights, 1):
        item = by_index.get(i)
        out.append(_coerce_entry(item, h, video_meta, subject) if isinstance(item, dict)
                   else _fallback_for(h, video_meta, subject))
    _spread_leads(out)

    # The second pass only builds on metadata a model actually wrote. Over a
    # fallback it would be writing Instagram copy from a filename.
    if by_index:
        _write_social(out, highlights, transcript, subject, guide, llm_fn, errors)
    for i, e in enumerate(out, 1):
        about = e.get("about") or {}
        named = about.get("subject") or (f"unnamed {about.get('genre') or ''}".strip())
        print(f"[seo] clip {i}: \"{e['title']}\" - filed under {named}", flush=True)
    return out


def _write_social(entries: List[Dict], highlights: List[Dict], transcript: Optional[Dict],
                  subject: Optional[Dict], guide: str, llm_fn: LLMFn,
                  errors: Optional[List[str]] = None) -> None:
    """Write each clip's Reels and TikTok packaging into its entry, in place.

    A failure keeps the fallback every entry already carries, and is reported
    the way a failed YouTube pass is -- but it never touches the YouTube
    metadata, which is the part a run cannot do without.
    """
    blocks = []
    for i, (h, e) in enumerate(zip(highlights, entries), 1):
        blocks.append(
            _build_clips_block([h], transcript, subject).replace("--- CLIP 1 (rank 1 of 1",
                                                                  f"--- CLIP {i} (rank {i} of {len(entries)}", 1)
            + f"\nYouTube title chosen: {e.get('title', '')}"
            + f"\nSearch phrase: {e.get('search_phrase') or '(none)'}"
            + f"\nOn-screen hook: {e.get('hook_text', '')}"
        )
    prompt = SOCIAL_PROMPT.format(
        playbook=guide or "(no playbook available - rely on what you know)",
        subject_block=subject_block(subject),
        caption_options=CAPTION_OPTIONS,
        clips_block="\n\n".join(blocks),
    )
    try:
        parsed = _parse_json_loose(llm_fn(prompt))
    except Exception as e:
        print(f"[seo] could not write the Reels and TikTok captions ({e}); "
              f"using the YouTube text made safe for them", flush=True)
        if errors is not None:
            errors.append(f"reels/tiktok: {e}")
        return

    written = 0
    for n, item in enumerate(parsed.get("clips") or [], 1):
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("index"))
        except (TypeError, ValueError):
            idx = n
        if not 1 <= idx <= len(entries):
            continue
        h, e = highlights[idx - 1], entries[idx - 1]
        social = _coerce_social(item, e, h, clip_subject(h, subject), subject)
        e.update(social)
        written += social["reels"].get("generated", False) or social["tiktok"].get("generated", False)
    print(f"[seo] wrote Reels and TikTok captions for {written}/{len(entries)} clip(s)", flush=True)


def attach_seo(highlights: List[Dict], **kwargs) -> List[Dict]:
    """Set a `seo` key on each highlight in place, and return them.

    Renderers copy every key they are handed onto their results, so metadata
    attached here rides along to the UI without anything in between needing to
    know about it.
    """
    for h, seo in zip(highlights, generate_seo(highlights, **kwargs)):
        h["seo"] = seo
    return highlights
