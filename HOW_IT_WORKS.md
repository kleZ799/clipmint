# How ClipMint works

By **Parth Bhadana** — [YouTube](https://www.youtube.com/@ParthBhadana799) · [GitHub](https://github.com/kleZ799) · [LinkedIn](https://www.linkedin.com/in/parth-bhadana-530014202/) · [Discord](https://discord.gg/jnMrGbBz3m) · [Buy me a coffee](https://buymeacoffee.com/parthbhadana)

> **📚 Study document.** This is a learning and reference companion to the
> codebase — written to be read end-to-end so the whole system can be explained
> from memory. It is *not* setup instructions (see the [README](README.md)) and
> it is *not* a specification: the code is the authority, and this describes the
> code as it stood when written. If the two ever disagree, the code is right and
> this file needs updating.

A complete walkthrough of this codebase: what it does, how it does it, what
technology it uses, and *why* each piece is shaped the way it is.

The [README](README.md) is the pitch. This is the map.
[CONCEPTS.md](CONCEPTS.md) is the theory — the same system described in terms of
the AI/ML and CS concepts behind it, for explaining the project rather than
navigating it. Everything here was read
off the current source, so where the README and the code disagree, this file
follows the code and says so ([§17](#17-rough-edges-and-stale-docs)).

Written to be read end-to-end. If you can explain [§1](#1-the-one-paragraph-answer),
[§4](#4-the-pipeline-stage-by-stage), [§6](#6-the-ranking-engine-the-brain) and
[§11](#11-the-backend-job-runner--web-layer), you can explain this project to
anyone.

---

## Contents

**Orientation**
1. [The one-paragraph answer](#1-the-one-paragraph-answer)
2. [The problem this actually solves](#2-the-problem-this-actually-solves)
3. [Tech stack, and why each choice](#3-tech-stack-and-why-each-choice)

**The engine**
4. [The pipeline, stage by stage](#4-the-pipeline-stage-by-stage)
5. [Getting the video](#5-getting-the-video)
6. [The ranking engine — the brain](#6-the-ranking-engine--the-brain)
7. [The three renderers](#7-the-three-renderers)
8. [Natural-language layout parsing](#8-natural-language-layout-parsing)
9. [SEO: the packaging step](#9-seo-the-packaging-step)
10. [Transcription](#10-transcription)

**The application**
11. [The backend: job runner + web layer](#11-the-backend-job-runner--web-layer)
12. [The frontend](#12-the-frontend)
13. [Configuration and precedence](#13-configuration-and-precedence)
14. [Quota accounting and provider fallback](#14-quota-accounting-and-provider-fallback)
15. [Caching: seven independent layers](#15-caching-seven-independent-layers)
    - 15b. [Two themes out of one set of rules](#15b-two-themes-out-of-one-set-of-rules)
16. [Packaging: a Windows .exe, a mac .app, a Linux binary](#16-packaging-a-windows-exe-a-mac-app-a-linux-binary)
    - 16a. [Subprocesses: windows, and stopping them](#16a-subprocesses-windows-and-stopping-them)
    - 16b. [Shipping updates to an installed .exe](#16b-shipping-updates-to-an-installed-exe)
    - 16c. [Telling somebody a run has finished](#16c-telling-somebody-a-run-has-finished)
    - 16d. [Posting a clip to YouTube](#16d-posting-a-clip-to-youtube)

**Reference**
17. [Rough edges and stale docs](#17-rough-edges-and-stale-docs)
18. [HTTP API reference](#18-http-api-reference)
19. [Interview cheat-sheet](#19-interview-cheat-sheet)

---

## 1. The one-paragraph answer

**ClipMint takes a long video — a stream VOD, a vlog, a podcast, any mp4 — and
produces a set of ranked vertical clips ready to upload as YouTube Shorts,
Reels or TikToks, each packaged for the app it is going to.**

It downloads the source with **yt-dlp**, transcribes it locally with
**faster-whisper**, sends the transcript to an **LLM (Gemini, Groq or OpenAI)**
with a prompt tuned to find moments that travel, dedupes and scores the results,
shows **frames from each winner to a vision model** so its title names what is
actually on screen, then renders the winners with **ffmpeg + OpenCV** into 9:16
video, and edits each one: word-by-word **captions** burned in with libass,
quiet pauses cut, punch-ins on emphasis, and a scorecard saying why it ranked
where it did. It ships as a **FastAPI** backend behind a **vanilla-JS** single-page UI,
wrapped in a native **pywebview** window and packaged by **PyInstaller** into a
Windows .exe, a mac .app and a Linux binary that need nothing installed.

The whole thing runs on the user's own machine. What leaves is the transcript
text and a few still frames per clip, sent to the model the user chose, and,
only when B-roll is switched on, a few search words sent to Pexels.

### The four stages

```
  get the file   →   transcribe   →   rank moments   →   render clips   →   edit
    yt-dlp          faster-whisper     Gemini/OpenAI      ffmpeg + OpenCV   Whisper per clip
   (network)          (slow, CPU)     (fast, costs $)      (medium, free)    + one encode
```

The fifth step is a pass over the finished clips rather than a stage of its
own: it listens to each one for word timings and re-encodes it with captions
and cuts (§7.7). It scales with clip count, like rendering, and is off
entirely when captions and the edit are switched off.

They are kept strictly separate because **they fail for different reasons and
cost different amounts.** That single observation explains most of the
architecture: the caching, the checkpointing, the provider fallback, the fact
that re-cutting a clip doesn't re-run the pipeline. A failure in one stage must
never throw away what the others already paid for.

#### What each stage costs, measured

The first three read the source end to end, so they are **linear in its
duration**. Rendering is not: it goes by clip count. That is the whole shape of
"how long will this take", and the README states it in those terms.

Measured on an RTX 5060 laptop, `small` model, against a real 4h16m stream VOD
and a 4m45s speech sample cut from its own clips:

| Stage | Throughput | Per hour of source |
|---|---|---|
| Download | ~1.6 GB per hour of 1080p source | 2.2 min at 100 Mbps, 8.7 min at 25 Mbps |
| Transcribe, CUDA | **25.1x realtime** | 2.4 min |
| Transcribe, CPU | **6.9x realtime** | 8.7 min |
| Render, NVENC | ~2.6x realtime per clip | *(flat — ~12s per 30s clip)* |

The GPU/CPU gap on transcription is 3.6x, and on a 4-hour VOD that is the
difference between a 26-minute run and a 50-minute one. Nothing else in the
pipeline moves the total nearly as much, which is why **Processor** is on the
Create page rather than buried in settings.

Ranking is not in the table because it is the one stage whose cost is not ours
to measure — it is a network round trip to whichever provider is up, chunked
over the transcript (§6.5), so it grows with length but the constant is
somebody else's load that day.

---

## 2. The problem this actually solves

Worth knowing, because it explains why this isn't just a wrapper around an API.

The author streams story games and posts Shorts. A 35-minute session has maybe
five clippable moments, and finding them means scrubbing the whole VOD twice.
Off-the-shelf clippers exist — and they fail on stream footage for two specific
reasons:

**1. They crop to the wrong thing.** Auto-croppers slide a vertical window across
the frame hunting for a face. On a stream, the biggest face on screen is usually
a *game character*, so the clip ends up centred on a cutscene with the streamer's
commentary playing from off-frame.

**2. They can't tell the streamer from the game.** A story game's audio is
narration, dialogue and score — all mixed onto the same track as the mic, with no
speaker labels. Generic highlight detection happily returns 45 seconds of
beautifully-written game narration with zero streamer in it. That footage belongs
to the studio, not the channel.

Two fixes, and they are the two most interesting parts of the codebase:

- **`local/gaming_layout.py`** — a webcam-over-gameplay renderer that locates the
  webcam overlay by searching *only the corner it lives in*, so a character's
  face can't win. ([§7](#7-the-three-renderers))
- **`STREAM_VIRALITY_CRITERIA`** — a ranking prompt that teaches the model to
  separate streamer speech from game narration by register, and refuses any clip
  without the streamer in it. ([§6](#6-the-ranking-engine--the-brain))

---

## 3. Tech stack, and why each choice

| Layer | Technology | Why this one |
|---|---|---|
| Language | Python 3.10+ | The whole ML/video ecosystem lives here |
| Web framework | **FastAPI** | Async, typed request bodies via Pydantic, native `StreamingResponse` for SSE, zero-config static serving |
| ASGI server | **uvicorn** | FastAPI's standard runtime; embeddable in a thread, which the desktop app depends on |
| Request validation | **Pydantic** `BaseModel` | Declarative request schemas; malformed JSON is rejected before any handler runs |
| Video download | **yt-dlp** | Actively maintained, handles YouTube's format churn, supports flat metadata-only extraction for channel listing |
| Transcription | **faster-whisper** (CTranslate2) | 4× faster than reference Whisper, runs int8 on CPU, no cloud round trip, no upload |
| LLM | **Google Gemini** (`google-genai`) / **Groq** / **OpenAI** | Gemini's free tier makes the app free to run; Groq is a free fallback with capacity of its own, OpenAI the paid one |
| Scene understanding | the same providers' **vision models** | Four frames per clip say which game is on screen — or that it is a podcast — with no model of our own to ship |
| Video processing | **ffmpeg** (subprocess) | Single-pass filter graphs do crop/scale/stack without touching frames in Python |
| Computer vision | **OpenCV** — **YuNet** (`FaceDetectorYN`), Haar as fallback — + **NumPy** | A 230 KB learned face detector shipped in the build, no GPU needed; overlay borders from statistics over a stack of frames |
| Hardware encoding | ffmpeg's **NVENC / VideoToolbox / Quick Sync / AMF** | Encoding on the graphics chip when one really works — each is test-encoded before use — with libx264 as the fallback |
| Frontend | **Vanilla JS + CSS**, no framework | Zero build step, ~5.2k lines total, ships as static files inside the build |
| Live updates | **Server-Sent Events** | One-directional server→client is exactly the shape of progress reporting; simpler than WebSockets |
| Desktop shell | **pywebview** (Edge WebView2) | A native window with no address bar, using a browser engine Windows already has |
| Packaging | **PyInstaller** | Single-file .exe with ffmpeg bundled — the user installs nothing |
| Config | **python-dotenv** + a JSON settings file | `.env` for developers, `settings.json` for people running the .exe |

### The three non-obvious choices

**Why SSE and not WebSockets.** Progress reporting is purely server→client. SSE
gives that over plain HTTP, auto-reconnects in the browser, and needs no protocol
upgrade or extra dependency. The client is one line: `new EventSource(url)`.

**Why no frontend framework.** The UI has to ship inside a PyInstaller bundle. A
build step means a toolchain in the release pipeline and a `node_modules` in the
repo. Three static files that PyInstaller copies verbatim eliminates that
entirely — and the UI is a single page with maybe eight interactive regions,
which is well inside what vanilla DOM handles cleanly.

**Why subprocess ffmpeg instead of a Python binding.** The renderers build filter
graphs (`crop → scale → vstack`) that ffmpeg executes in one pass with zero
frames crossing into Python. That is the difference between clips rendering in
seconds and rendering in minutes. The one renderer that *does* touch frames in
Python (`facetrack`) reads every frame of the clip to plan its camera path, and
still renders 30 seconds of 720p60 in about 11s on a GPU and 17s on a CPU —
because it seeks straight to the clip rather than decoding the stream up to it.

---

## 4. The pipeline, stage by stage

### 4.0 First: two front doors that behave differently

This is the thing most likely to confuse you when reading the code:
**the CLI and the app do not run the same pipeline.**

| | `python main.py …` | `desktop.py` / `python -m webapp` |
|---|---|---|
| Orchestrator | `shorts_generator/pipeline.py` | `webapp/jobs.py :: JobStore._execute` |
| Default mode | `api` (MuAPI, cloud) | local, always |
| Layout | face-tracking crop only, hardcoded | any of three, via `LayoutSpec` |
| Uses `render.py` | no | yes |
| Uses `layout_spec.py` | no | yes |
| Writes SEO metadata | no | yes |
| Resumable chunk checkpoints | no | yes |
| Source-quality ladder | no | yes |
| Exact-span fast path | no | yes |
| Output | `output/short_NN.mp4` | `shorts/<job-id>/short_NN.mp4` |

`pipeline.py` is the original upstream shape, kept working. It is the only caller
of `api` mode, where MuAPI does download, transcription, ranking and cropping
server-side. **If you are reading the code to understand the product, read
`webapp/jobs.py::_execute`.** Everything below describes that path.

### 4.1 Module map

```
main.py                     CLI entry — argparse → generate_shorts()
desktop.py                  Desktop entry — uvicorn thread + native window
build_exe.py                PyInstaller wrapper

shorts_generator/
├── pipeline.py             CLI orchestrator; picks api vs local mode
├── config.py               .env loading, env vars, key requirements
├── user_config.py          per-user settings.json, output roots
├── usage.py                daily API-request ledger + Pacific day boundary
├── highlights.py           THE BRAIN — prompts, chunking, scoring, dedupe
├── content_kinds.py        stream / vlog / podcast / tutorial / other
├── signals.py              loudness envelope, trigger phrases, hook score
├── boundaries.py           snap spans to sentences; enforce clip length
├── hook_open.py            prepend a late payoff to the front of a clip
├── layout_spec.py          natural language → LayoutSpec; quality ladder
├── render.py               one entry point that dispatches a LayoutSpec
├── autoedit.py             after the cut: jump cuts, punch-ins, emoji, B-roll, one encode
├── captions.py             burned-in word-by-word captions: four presets, ASS for libass
├── words.py                word timings for a rendered clip (faster-whisper, per clip)
├── broll.py                where stock footage fits (the LLM) and fetching it (Pexels)
├── scorecard.py            a clip's score split into Hook / Moment / Energy / Pace + why
├── brand.py                the channel's logo: stored, validated, placed in a corner
├── bundled.py              finds assets/ in a checkout or a PyInstaller build
├── vision.py               what each clip shows, from four of its frames
├── faces.py                face detection: YuNet, with Haar as the fallback
├── accel.py                which processor encodes video and runs Whisper
├── seo.py                  Shorts, Reels and TikTok packaging, all ranked
├── playbook.py             the growth playbook: bundled, fetched, overridable
│
├── downloader.py           api mode: MuAPI /youtube-download
├── transcriber.py          api mode: MuAPI /openai-whisper
├── clipper.py              api mode: MuAPI /autocrop
├── muapi.py                thin submit-and-poll client
│
└── local/
    ├── downloader.py       yt-dlp, download cache, channel listing, metadata
    ├── yt_access.py        YouTube's bot gate: client ladder, browser cookies, cookies.txt
    ├── transcriber.py      faster-whisper + .srt cache
    ├── llm.py              Gemini / Groq / OpenAI, text + vision, retries, fallback
    ├── clipper.py          renderer: face-following crop on a planned camera path
    └── gaming_layout.py    renderer: webcam-over-gameplay vstack, overlay located

webapp/
├── __main__.py             python -m webapp
├── server.py               FastAPI routes (~30 endpoints)
├── jobs.py                 job queue, worker thread, progress, persistence
└── static/                 index.html / app.js / style.css — the whole UI
```

### 4.2 Two design rules hold this together

**One transcript shape.** Every transcriber, local or remote, returns exactly:

```python
{"duration": 2130.0, "segments": [{"start": 12.4, "end": 15.1, "text": "..."}]}
```

`highlights.py` cannot tell which one ran. That is the point — the ranking logic
stays a single copy that can't drift between modes.

**The LLM is an argument, not an import.**

```python
get_highlights(transcript, num_clips=5, llm_fn=call_local_llm)
```

`llm_fn` is *the function that calls a model*. Swapping Gemini for OpenAI for
MuAPI touches one line at the call site and nothing inside the ranker. This is
plain dependency injection, and it's why the same prompts drive three backends.

### 4.3 The run, top to bottom

`webapp/jobs.py::JobStore._execute` in ~80 lines:

```
reset_fallback()                      ← undo any previous provider switch
      │
      ▼
download_youtube_local()              ← stage: download   (bar 0.00 → 0.15)
fetch_video_meta()                    ← title/channel/tags, best-effort
      │
      ├─── spec.time_ranges set? ────► render directly, skip everything below
      │
      ▼
transcribe_local()                    ← stage: transcribe (bar 0.15 → 0.55)
      │
      ▼
analyse_audio()                       ← one loudness envelope for the whole VOD
      │
      ▼
get_highlights(audio=…, kind=…)       ← stage: rank       (bar 0.55 → 0.70)
  ├ resolve_content(): what kind of video this is
  └ finalize(): snap boundaries → rescore → dedupe
sort by score, take top N
      │
      ▼
detect_subject()                      ← what the video is about, and all it mentions
_look_at_clips() → describe_clips()   ← what each clip's frames show (vision.py)
attach_seo()                          ← Shorts, then Reels + TikTok, from the playbook
      │
      ▼
render_highlights(spec, language, kind, llm_fn)
                                      ← stage: render     (bar 0.70 → 0.86)
  ├ autoedit.polish(): listen, plan, one encode per clip
  │                                   ← stage: edit       (bar 0.86 → 1.00)
  └ hook_open.apply() on each clip whose payoff lands late,
    at the peak moved onto the cut timeline
      │
      ▼
_finalize() → _persist()              ← scorecards attached, job.json written
```

**Step 0 — `reset_fallback()`.** A previous run may have fallen back from Gemini
to OpenAI when the daily quota ran out. That choice is process-sticky, so each
new job explicitly re-checks the provider the user actually chose — its quota may
have reset since.

**The exact-span fast path.** If the layout prompt contained something like
`cut 14:45 to 15:30`, the user has already told us what to cut. There is nothing
to transcribe and nothing to rank, so the job goes straight to SEO + render.
Seconds instead of minutes, zero API quota, and no transcript ever leaves the
machine.

---

## 5. Getting the video

`local/downloader.py` handles three input shapes:

- **A local path or `file://` URL** → returned as-is. Nothing downloads.
- **A YouTube video URL** → yt-dlp, saved as `source_<videoid>.mp4`.
- **A channel or playlist URL** → rejected here. The UI resolves it first via
  `POST /api/resolve`, which calls `list_channel_videos()` — yt-dlp's
  `extract_flat` mode, so it's a *metadata-only* call that pulls no media — and
  shows 12 recent videos as a pickable grid.

`is_channel_or_playlist()` distinguishes them by URL shape: a `list=` query
param, or a path starting `/@`, `/channel/`, `/c/`, `/user/`, `/playlist`, or
ending `/videos`, `/streams`, `/shorts`.

### The quality-aware download cache

This is subtler than a normal cache, because the wrong hit is expensive.

`_existing_download()` finds every `source_<id>*` file on disk and ffprobes each
for height, keeping the tallest. `_cache_is_good_enough()` then decides whether
that copy satisfies the *current* request:

| Requested | Rule |
|---|---|
| `best` | Anything ≥ 1440p counts. Below that, check whether the source offers more |
| A number (`1080`) | The cached copy must be within 95% of that height |
| ffprobe can't read it | Assume it's fine — don't re-download on a guess |

A better re-fetch is written with a **quality tag in the filename**
(`source_abc_1080.mp4`) rather than overwriting the old file. That is not
tidiness: **the transcript cache is keyed to the video's filename**, so
overwriting would silently invalidate a transcription that took twenty minutes.

**A failed upgrade falls back to the copy on disk.** Asking for `best` with a
1080p copy cached is a request to *improve* on something usable — and that
optimisation used to be allowed to sink the run. If YouTube refused the better
copy, the download stage failed, and with it a finished 10 GB download, its
transcript and its ranked highlights, over three-quarters of a megapixel. Now
the refusal is logged and the cached file is returned. Everything keyed to it
stays valid, because it is the same file it always was.

### YouTube's bot gate — `local/yt_access.py`

Sometimes YouTube answers yt-dlp with a consent wall instead of a video:

```
Sign in to confirm you're not a bot. Use --cookies-from-browser or --cookies
```

It is not a broken URL and not a bug here. It is YouTube deciding, for that
request from that IP, that it wants a signed-in human — common on VPNs, shared
connections, after a run of downloads, and on anything age- or region-gated.
Left alone it kills the run at 0% in about a second, which reads as the app
being broken. Every yt-dlp call that touches YouTube goes through
`yt_access.run()`, which climbs a ladder until a rung gets through:

| Rung | What it sends | Costs |
|---|---|---|
| as normal | yt-dlp's default clients | nothing |
| as the TV app | `player_client=tv_simply` | nothing |
| as a phone app | `android_vr,ios` | nothing |
| as an embedded player | `web_embedded,tv` | nothing |
| with cookies | `web,tv,mweb` plus a browser's or a file's cookies | identifies an account |

**Ask differently before asking as somebody.** The clients are gated separately
and inconsistently, so a request the web client is refused often goes straight
through as the TV app — free, with no setup. Cookies come last because they are
the one rung that tells YouTube who is asking. The app clients cannot carry
cookies, so the cookie rungs switch to clients that can.

**Only the gate climbs the ladder.** `is_gate()` matches the refusal's many
phrasings, including yt-dlp's "requested format is not available", which is how
an empty player response surfaces. A real failure — ffmpeg refusing a merge, a
full disk — is raised on the spot; trying it four more ways would only make the
person wait longer to read the same error. `needs_an_account()` separates age-
and members-only refusals, which no client juggling fixes, so the final message
names the real problem.

**The winner is remembered for the run.** A channel job asks YouTube a dozen
times; once a rung works it goes first for the rest of that run, so the ladder is
climbed once rather than per request. `reset()` forgets it when the next run
starts, and whenever the setting changes.

**Where cookies come from** is the Settings → *YouTube sign-in* choice, stored as
`YOUTUBE_COOKIES_MODE`:

- `auto` (default) — no cookies until YouTube asks, then any browser that can
  hand some over. `probe()` reads each installed browser once — cached, since it
  reads the whole cookie database — and ranks browsers holding the signed-in
  cookies (`SID`, `__Secure-1PSID`, `LOGIN_INFO`) above ones that have merely
  visited YouTube.
- `browser` — one named browser, `firefox` or `firefox:Profile`. A deliberate
  choice **leads the ladder**: someone who went into settings did it because the
  gate keeps appearing, and four anonymous rungs first would waste the setting.
- `file` — a Netscape-format `cookies.txt`, also led with.
- `off` — never, even when YouTube asks; the final message says so rather than
  quietly overriding the choice.

A `cookies.txt` **dropped into the settings folder or the clips folder** is picked
up with no configuration, under any of the names the popular export extensions
save as (`cookies.txt`, `www.youtube.com_cookies.txt`, …). Finding a settings
panel and pasting a path is where most people give up; dragging a file into a
folder the app already talks about is not. `YTDLP_COOKIES` in the environment
outranks both, as env vars do for every setting here, and the panel shows when
it is doing so.

`POST /api/youtube-access` proves a choice before storing it: a browser is
probed, and a file goes through `check_file()` — no file, not a cookie jar, no
YouTube cookies, all expired, not signed in. A bad choice fails in the settings
panel instead of twenty minutes into the next run.

**yt-dlp never gets the user's own file.** It writes the jar back to whatever
path it was handed, and measured here, a run YouTube refused came back with the
login cookies *stripped out* of the export. So `working_copy_of()` hands it a
private copy — `youtube-cookies-in-use.txt` in the settings folder, mode `0600`
because it is a session token — refreshed whenever the original is newer, so a
re-export still takes effect.

**Browsers that can't be read, and why.** On Windows, Chrome 127+ and the other
Chromium browsers use App-Bound Encryption: the cookie store is encrypted with a
key only that browser's own process can unwrap, so nothing outside it can read
it, this app included — the advice is Firefox or a `cookies.txt`. On a Mac,
Chrome is readable but Safari sits behind Full Disk Access, and the message says
where that setting lives. Linux has neither lock, so an empty result almost
always means nobody is signed in. `_explain_probe_failure()` and
`_why_browsers_refuse()` branch on the platform, so nobody is told about another
operating system's lock.

**What cookies mean for the account.** They are the signed-in session of that
browser profile. Nothing leaves the PC — yt-dlp sends them to YouTube, where they
came from — but YouTube does see which account the downloads belong to, and
accounts have been restricted for automated access. The settings panel says so,
and suggests a spare account.

### How fast the download says it is going

`_progress_line()` prints one line a second, and the rate on it is measured
here rather than taken from yt-dlp. That is not duplication for its own sake.
yt-dlp's `d["speed"]` is an *instantaneous* reading, and the first one it
hands out covers a few milliseconds of a connection that has not opened its
window yet — it comes out near a hundred KB/s no matter how fast the line
really is. The once-a-second gate made that worse rather than better: the
counter starts at zero, so the first callback always cleared it, and the
single least trustworthy sample was guaranteed to be the one on screen.

On a 25 MB file that read `0.0% at 361KB/s - 1m11s left` for a download that
finished in three seconds. On a 7 GB stream VOD the same first sample reads
`126KB/s - 21h46m left`, and that is the line people screenshot when they
report the app as slow — the download is usually fine.

So the module keeps `(when, bytes)` samples over a trailing **20-second
window** and divides. Nothing is claimed until the window holds at least two
seconds and 1 MB; before that the line gives the percentage alone, which is
true. A window rather than an average over the whole transfer, because
YouTube does throttle mid-download and that is worth *seeing* when it happens.
Bytes going backwards means yt-dlp has moved from the video file to the audio
one, so the samples reset rather than reading as a stall.

`webapp/jobs.py` treats the rate and the estimate as optional when it turns
the line into `Fetching the video 5% at 35.8MB/s, 3m11s left`, so the early
lines simply arrive as `Fetching the video 0%`.

**Two yt-dlp options matter to the speed itself.** `http_chunk_size` asks for
the file in 10 MB ranges: YouTube rate-limits a single long-lived connection,
so a continuous download decays as it runs, and a fresh request per chunk
sidesteps that. It is worth nothing on a healthy connection — 21.0 MB/s
against 23.7 MB/s on a 4-hour VOD is noise — and it is insurance for the
throttled case. `concurrent_fragment_downloads: 4` does nothing for these
downloads at all: measured, yt-dlp picks *continuous* H.264 streams for every
format the selector asks for. It stays for the formats that really are
fragmented — live VODs, and the VP9 and AV1 ladders `best` reaches for above
1080p.

### Video metadata for the SEO writer

`fetch_video_meta()` pulls title, uploader, description and tags without
downloading anything. The transcript says *what was said*; this says *what the
video is about* — the channel, the topic, the words the uploader already ranks
for. It's best-effort: a failure costs a little context, never the run.

---

## 6. The ranking engine — the brain

`shorts_generator/highlights.py` is the most opinionated file in the repo, and
the one that decides output quality.

### 6.1 What kind of video is this?

A stream, a vlog, a podcast and a tutorial are good for different reasons, and a
ranking prompt written for one reads the others wrong. Told that every video is
a game stream, the model goes looking for game narration in a travel vlog. So
before anything is ranked, `resolve_content()` settles which of five kinds the
video is — `stream`, `vlog`, `podcast`, `tutorial` or `other`
(`content_kinds.py`).

**Told, or detected.** The *Kind of video* picker, or a word in the prompt
("my vlog", "podcast"), pins it. Otherwise one cheap call to
`detect_content_type()` classifies the video from **three samples — start,
middle and end — plus the video's own listing** (title, channel, category,
description). The opening alone is the worst place to judge from: a stream opens
on "starting soon", a podcast on an ad read, a vlog on "hey guys". The detector
has finer labels than the ranker needs (`gaming_stream`, `just_chatting`,
`interview`, `lecture`…), and `from_content_type()` folds them onto the five
kinds, because an interview and a podcast are good for the same reasons and five
copies of one rule book would drift apart. It also estimates density
(`low` / `medium` / `high`).

A kind the user chose always wins. Detection still runs then, for the density,
and the log says when the two disagree: `ranking as stream (chosen) - it looked
like vlog`. On *any* failure the answer is `other`, which gets the general
criteria — a classification is context for the main prompt, never a dependency.

**It is saved with the checkpoint.** A detector is a model and can answer
differently twice. A resumed run that suddenly ranks as a different kind has
quietly thrown away every chunk it already paid for, so the answer is written
into `<source>.highlights.json` and reused ([§6.6](#66-resumable-checkpoints)).

### 6.2 The prompt is assembled from four blocks

```
HIGHLIGHT_SYSTEM_PROMPT
  ├── {virality_criteria}   ← CRITERIA_BY_KIND[kind]
  ├── {cold_open_rules}     ← COLD_OPEN_RULES
  ├── {user_brief}          ← the user's own words, if any
  └── {duration_rule}       ← house default, or what the user asked for
```

**`CRITERIA_BY_KIND` is the single biggest lever in the codebase.** It maps each
kind of video to the block that says what counts as a good moment in it.

**Streams** get `STREAM_VIRALITY_CRITERIA`, written for the mixed-audio-track
problem from [§2](#2-the-problem-this-actually-solves). It teaches the model to
separate the two voices by **register**:

> **Game narration** reads like written prose — literary, past tense, polished,
> no filler words, no self-correction, never addresses anyone.
>
> **The streamer** sounds spoken — reactions, false starts, laughter, swearing,
> questions, talking to chat.

Then the hard rule: **every highlight must contain the streamer's own speech.** A
story beat only counts when the streamer reacts to it, talks over it, or responds
after it.

Its ranked priorities: reaction to a story beat → raw unscripted reactions →
fails and disasters → hot takes → chat interaction → personal tangents →
quotable one-liners → sincerity. Dead air, loading screens and stream
housekeeping are explicitly skipped.

The other kinds each get their own block:

| Kind | Criteria | What it hunts for | What it refuses |
|---|---|---|---|
| `vlog` | `VLOG_VIRALITY_CRITERIA` | story payoffs, raw reactions, things going wrong, first times, honest moments to camera | greetings, subscribe asks, travel filler, spans with no creator speech |
| `podcast` | `PODCAST_VIRALITY_CRITERIA` | opinion bombs, confessions, disagreement while it is heated, banter, quotables | intros, guest bios, ad reads; opens on the answer rather than the question |
| `tutorial` | `TUTORIAL_VIRALITY_CRITERIA` | the mistake and its fix, one complete tip, a surprising result, the why | setup, installs, "link in the description", steps that need earlier steps |
| `other` | `VIRALITY_CRITERIA` | hooks, emotional peaks, opinion bombs, revelations, conflict, story peaks, practical value | — |

**`COLD_OPEN_RULES`** encodes the distribution reality the whole app sorts on. A
Short is judged in its first second; the platform reads early drop-off as a
verdict on the entire clip. So:

- `start_time` goes on the first word of the most arresting line, not the run-up
- at most ~1 second of runway, and only when the payoff is a *sound* (a laugh, a
  scream) where the instant before it lands is what makes it read
- the first line must work alone, read by a stranger
- end on the punch, never trailing into dead air
- **a moment that needs a preamble is not a highlight** — skip it and spend the
  slot on one that can open cold

**`brief_block()`** injects the user's own prompt verbatim, marked as outranking
the generic criteria where they disagree. Someone who types *"only the funny rage
moments"* has told the ranker something the house list cannot know. It explicitly
instructs the model to ignore framing instructions (webcam position, aspect
ratio, clip count) — those belong to the renderer.

### 6.3 Double scoring, and the blend

The model scores each clip **twice, independently**:

- `score` — viral potential of the moment as a whole
- `hook_score` — how hard the opening line *alone* stops a scroll, judged as if
  you cannot see the rest of the clip

`_sanitize_highlights()` blends them into the number everything else sorts on:

```python
HOOK_SCORE_WEIGHT = 0.4
score = 0.4 * hook_score + 0.6 * viral_score
```

The original is kept as `viral_score`, and `hook_score` survives alongside. A
model that ignores `hook_score` isn't punished — an absent value defaults to the
viral score, meaning *"no opinion"*, not zero.

The reasoning: a brilliant moment behind a flat opening line is not a good Short,
because nobody stays long enough to reach it. The opening line gets a real vote.

### 6.4 Sanitising what comes back

Never trust model output. `_sanitize_highlights()` drops non-dicts, negative
starts, `end <= start`, and spans over the ceiling; clamps the rest to the
video's real duration; and coerces every field to its expected type with a safe
default.

The ceiling is `MAX_CLIP_SECONDS` (90) — but `_clip_ceiling()` raises it to
`clip_seconds[1] * 1.2` when the user explicitly asked for long clips. Otherwise
someone requesting 90–120s clips would have every single one silently thrown away
by a limit they never saw.

### 6.5 Chunking long videos

Videos at or over `LONG_VIDEO_THRESHOLD` (1800s / 30 min) are split into
`CHUNK_SIZE_SECONDS` (1200s / 20 min) windows with `CHUNK_OVERLAP_SECONDS` (60s)
of overlap, advancing by `CHUNK_SIZE - OVERLAP` each step. Overlap exists so a
moment straddling a boundary isn't cut in half by the chunking itself.

The critical detail is **rebasing**:

```python
chunk["segments"] = [{**seg, "start": seg["start"] - start,
                             "end":   seg["end"]   - start} for seg in chunk_segs]
chunk["duration"] = end - start
chunk["_offset"]  = start          # added back after ranking
```

Without this, chunks past the first hand the model *absolute* timestamps that are
then clamped against a *relative* duration bound — and every highlight after the
first 20 minutes is silently discarded. **Long videos returned zero highlights
before this fix.** It's the kind of bug that produces no error, just an empty
result.

`seg_end` is the same idea one level down. The window deliberately carries 60s of
segments past its own end so a moment straddling the boundary stays readable —
and the declared duration has to cover that tail, because it is the bound the
clamp measures against. Declaring `end - start` while carrying segments to
`end + 60` meant every highlight found in that last minute was clamped to zero
length and dropped, silently, on three of every four chunks.

### 6.6 Resumable checkpoints

Each chunk costs an API request. A nine-chunk video that died on chunk three used
to throw away the two it had already paid for.

So every finished chunk is written to `<source>.highlights.json` **immediately —
per chunk, not at the end**, because the whole point is surviving the failure
that happens on the *next* one.

Reuse is gated by a fingerprint:

```
v{PROMPT_VERSION}|{duration, to the minute}|{chunk_count}|{num_clips}|{clip_length}|{kind}
```

- Duration is compared **to the minute**, and that is load-bearing. A transcript
  reports the media's own length when it has just been made, and its last cue's
  end when read back from the cached `.srt` — seconds apart, by however much
  silence trails the last word. Compared to the second, every run after the first
  computed a different fingerprint and re-asked the LLM for all fifteen chunks of
  a 4h27m stream: the exact case the checkpoint exists for had never worked. A
  minute absorbs the trailing silence and is still far finer than a real change
  of file — and duration was never the only guard, since the checkpoint sits
  beside one specific video and `chunk_count` moves with length.

- `PROMPT_VERSION` is in there because chunks ranked by an older prompt are
  answers to a question the app no longer asks. Resuming onto them would hide a
  prompt change from every video that had already been through once.
- Clip length is in there because asking for 30s clips after a run that found 60s
  ones is a *different question*, and reusing those answers would silently ignore
  what was asked for.
- The kind of video is in there for the same reason: chunks ranked as a stream
  are no answer for the same video ranked as a podcast. The detected kind itself
  is saved in the file too, and read back *whatever the fingerprint says* — the
  question may have changed while what kind of video it is has not.

### 6.7 Dedupe

```python
overlap > 0.5 * candidate_duration  →  drop
```

Sorted by score descending, so when two candidates overlap the higher-scoring one
is already kept and the weaker one is dropped. Overlap is measured against the
*candidate's own* duration, so a short clip buried inside a long one is correctly
recognised as redundant.

### 6.8 Retry on malformed output

`call_highlight_api()` makes up to `MAX_HIGHLIGHT_API_ATTEMPTS` (3) attempts.
After a failure it appends an explicit *"return ONLY valid JSON, no markdown
fences"* instruction and retries. `_parse_json_loose()` also strips markdown
fences and, failing that, slices from the first `{` to the last `}`.

It asks the model for roughly **2× the requested clip count** so dedupe has
headroom, capped so the model doesn't have to emit a huge JSON payload — which
times out smaller models mid-object.

### 6.9 `finalize()` — the model's answer is not the last word

Everything above produces *candidates*. `finalize()` turns them into the spans
that actually get cut, in three passes whose order is load-bearing:

```python
highlights = boundaries.refine(...)   # 1. move the span
signals.rescore(...)                  # 2. re-rank it on what the audio did
highlights = dedupe_highlights(...)   # 3. drop what now collides
```

1. **Boundaries first.** Snapping moves every span, often by seconds, because a
   clip is re-opened on its own hook line. Measuring signals before this would
   be measuring audio that is no longer inside the clip.
2. **Signals second**, on the final spans.
3. **Dedupe last.** Two candidates the model kept apart can land on top of each
   other once both are snapped to the same sentence boundaries.

### 6.10 Measured signals — `signals.py`

A transcript cannot carry a scream, a laugh, or the half-second of silence
before a punchline. Two moments can read identically on the page and be worlds
apart in the audio, and the audio is what a viewer meets first. So four things
are measured directly from the footage and folded into the rank.

**The envelope.** `analyse_audio()` runs one ffmpeg decode of the whole source
to mono 16-bit at **4 kHz**, streams the PCM back through a pipe, and reduces it
to **one RMS value per 0.25s**. Streaming matters: a four-hour VOD is gigabytes
of PCM even at that rate, and all that survives the read is ~60,000 floats.
Every audio signal is derived from that one array.

Normalisation is against **percentiles of the video's own distribution**, not
min/max — one clipped frame would otherwise define the whole scale and every
real moment would score within a few points of every other.

```python
BASE_WEIGHTS = {
    "audio_spike":     0.30,   # peak vs the video's own p95
    "keyword":         0.25,   # trigger phrases, weighted by strength
    "chat_velocity":   0.20,   # not obtainable — no chat log
    "face_reaction":   0.15,   # not obtainable — too expensive per frame
    "silence_to_peak": 0.10,   # quiet run-up before the spike
}
```

The two unobtainable signals are **redistributed, not zeroed**. Scoring them 0
would shrink every clip's ceiling and make an 80 from a video with no chat log
mean something different from an 80 with one.

**Coverage caps how far measurement can move a rank.** Redistribution keeps the
scale honest but cannot manufacture confidence: with no audio the whole score
rests on one keyword list, and a clip containing the words *"no way"* would
otherwise score a flat 100 and outrank everything the model actually understood.
So the measured half's share is scaled by how much of the obtainable evidence
was actually obtained — all of it with audio, 38% without.

```python
share   = SIGNAL_WEIGHT * coverage(measured)     # SIGNAL_WEIGHT = 0.38
blended = (1 - share) * model_score + share * measured_score
```

**Dialogue density is a penalty, not a weight.** Two seconds of near-silence at
the top of a Short is the one failure that is disqualifying rather than merely
bad, so it scales the blend down by up to 35% instead of nudging an average.
It is only measured when a transcript exists — zero words counted with no
transcript means *"we did not look"*, not *"this clip opens on silence"*.

Every sub-signal is kept on the highlight and written into `job.json`. The rule
book those weights come from is explicit that they are seed values to be
corrected against real retention data; that correction is impossible against
outcomes nobody recorded. Reading YouTube Analytics back in is not built — this
is the half of the loop that can exist without an OAuth flow.

### 6.11 Boundaries — `boundaries.py`

**Length is arithmetic.** Ask a model for 30-second clips and it returns 19s,
24s, 47s: it is estimating durations from timestamps it half remembers while
also writing JSON. So the length is enforced in code. `_choose_end()` walks
whole transcript segments forward from the opening and picks:

| The model's end time | What happens |
|---|---|
| Inside the band | Kept, snapped to the end of the sentence it lands in |
| Short of the band | Extended to the boundary nearest the band's **middle** — someone who typed "30 seconds" wants 30, and a run of 26s clips is the same complaint in a different shape |
| Past the band | Cut at the **last** boundary that fits, keeping as much of the payoff as the length allows |
| No boundary fits | The first one past the ceiling if it overruns by under 2.5s, else a cut at the ceiling on the quietest instant |

Because it moves in whole segments, enforcing a length can never cut mid-word.

**The opening is placed by the line, not the number.** The prompt asks for
`first_line` — the exact sentence the clip opens on — and the model quotes it
accurately while pairing it with a timestamp that routinely lands seconds early,
on the throat-clear before it. `find_hook_segment()` therefore searches the
transcript for that line by word overlap (≥60% of its tokens, distance only
breaking ties) and opens the clip there, with at most ~0.35s of runway and never
into the previous speaker's tail. A clip whose first two seconds fall below the
dialogue-density floor steps forward up to four segments to find one that does
not.

Both cut points are then nudged to the quietest instant within ±0.4s, because a
cut in a gap is inaudible and a cut on a loud frame is not.

`reserve_seconds` shortens the band before any of this, so the room the hook
cold open will take is held back rather than added on top — which is why
30-second clips are still 30 seconds once it is on the front.

### 6.12 The hook cold open — `hook_open.py`

Opening on the hook line is the right answer when the hook *is* the opening
line. Some moments are a build and a payoff, and the payoff — a scream, a
laugh, the thing going wrong — is what stops a scroll. Open on the build and the
first second is someone talking quietly.

So a clip whose loudest moment lands more than 3s in gets 1.9s of that moment
played first, then the clip in full. One ffmpeg pass over the **rendered** clip,
splitting and concatenating its own frames, so the teaser is guaranteed to match
the layout exactly:

```
[0:v]split=2[va][vb];[0:a]asplit=2[aa][ab];
[va]trim=start=A:end=B,setpts=PTS-STARTPTS[v0];  …
[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]
```

It returns `None` — leaving the plain cut in place — when there is no audio
envelope to find a peak in, when the peak is already at the front, or when
anything at all goes wrong. A garnish must never cost a render.

Exports are also normalised to **-14 LUFS** (`render.LOUDNESS_FILTER`), the
target all three platforms mix toward.

---

## 7. The three renderers

`render.py::render_highlights()` is the only entry point. Callers never branch on
layout themselves — that's the whole reason the module exists. The repo had grown
two renderers with different shapes plus a centre crop with no home.

### 7.1 Choosing the output size

If `spec.match_source_quality` is on (the default), `pick_output_size()` measures
the crop that will *actually be taken* and climbs a per-ratio ladder as far as
those real pixels justify:

```python
QUALITY_LADDER["9:16"] = [(1080,1920), (1440,2560), (2160,3840)]
```

A rung is allowed when the crop supplies **at least ~85% of its width**. Below
that the renderer would be inventing pixels — which adds no detail and only
inflates the file.

For the stacked layout the maths accounts for the gameplay panel being only ~58%
of output height while still sourced from the *full* frame height, so it has
proportionally more pixels to give.

Net effect: a 1440p stream renders at 1440×2560 instead of being flattened to the
1080p platform minimum.

### 7.2 `stacked` — the webcam-over-gameplay layout

`local/gaming_layout.py`. This is the reason the repo exists.

```
┌──────────────────────┐
│       WEBCAM         │  42%  (CAM_PANEL_FRACTION)
│   auto-located,      │
│  head-and-shoulders  │
├──────────────────────┤
│                      │
│      GAMEPLAY        │  58%  centre crop, nudged away
│                      │       from the webcam corner
└──────────────────────┘
       1080 × 1920
```

The webcam is **not a hardcoded rectangle.** Each clip gets its overlay located
from scratch. The first version of this took the median face in the corner and
cropped five face-widths around it, and real streams broke it twice: it locked
onto a baby's photo *inside the game*, and wherever the overlay was smaller than
the guess it filled the cam panel with gameplay and letterbox bars. Both are
fixed by the one property that tells an overlay from a game — **it does not
move** — which means looking at more than the clip:

1. **Sample ~20 frames**: `SAMPLE_COUNT` (6) inside the clip and
   `CONTEXT_SAMPLES` (14) from `CONTEXT_SECONDS` (240) either side of it, each
   scaled to `ANALYSIS_WIDTH` (960). Over eight minutes the game underneath
   changes completely; the overlay does not.
2. **The streamer is the face that recurs.** YuNet runs on every sample; each
   detection counts how many others sit within a face-width of it, and the
   best-supported one plus its neighbours is the streamer (`_recurring_face`).
   A face in the game appears in one or two samples and loses the vote. Only
   detections near the configured `corner` take part.
3. **The overlay's border is found, not guessed** (`_overlay_bounds`). Two maps
   over the samples: the 20th-percentile Sobel gradient (an edge present in
   ~80% of samples) and the per-pixel standard deviation (how much the picture
   changes; always-black letterbox counts as "outside"). Walking out from the
   face in each direction, the border is the persistent edge with the most
   change beyond it and the least before it. A door frame behind the streamer is
   persistent too — but it is still on both sides, so it scores nothing.
4. **The crop fits inside the border**, at the panel's own aspect
   (`out_w / cam_h` — the old 4:3 stretched any panel but the default), as much
   of `face_w × FACE_CONTEXT_MULTIPLE` as fits, face at `FACE_VERTICAL_ANCHOR`,
   pulled in `BORDER_INSET` pixels so the border never shows.
5. Coordinates are scaled back to the source and rounded to even numbers — h264
   chroma subsampling requires it.

On the Edith Finch VOD the repo is tested against, three spans that used to
give three different rects (one of them the baby) now give the same one, from
8–16 agreeing detections, in about 5s per clip.

**A face that fills the frame** (`FULL_FRAME_FACE`, 10% of the width, recurring)
means there is no overlay: the camera *is* the video. That clip is handed to
the face-following renderer below rather than split in two — so a podcast or a
just-chatting segment run with the default layout still comes out right.

Then the whole thing renders in **one ffmpeg pass**:

```
[0:v]crop=cam…,scale=1080:806,setsar=1[cam];
[0:v]crop=game…,scale=1080:1114,setsar=1[game];
[cam][game]vstack=inputs=2[v]
```

No per-frame Python. Clips render in seconds, not minutes.

If no face turns up in any sample, it falls back to a full-height centre crop and
**says so in the log** rather than silently shipping something wrong.

### 7.3 `facetrack` — the talking-head crop

`local/clipper.py`. For footage where the speaker fills the frame — and what the
stacked renderer hands a clip to when it finds that the camera is the whole
picture.

The upstream version detected the largest face on every frame and eased 15% of
the way toward it. It stuttered: Haar boxes wobble by several pixels between
identical frames, a false positive yanks the window for a frame or two, and a
crop that is always moving a little reads as a shaky camera. So the path is now
**planned before anything renders**, the way a camera operator would shoot it:

1. **Detect** (`track_faces`) ~8 times a second (`DETECT_PER_SECOND`) on a 640px
   copy, with YuNet (`faces.py`, [§7.6](#76-finding-faces--facespy)); a
   `proc.wait_if_paused()` between samples is what lets Pause hold this pass. The track stays on the person already followed; a face somewhere
   else must hold for `CONFIRM_SAMPLES` before the window goes to it.
2. **Clean**: gaps are held at the last position, spikes removed with a ~0.6s
   median.
3. **Operate** (`_operate`): the window does not move while the face stays inside
   a `DEAD_ZONE` of 12% of the window; when it leaves, the window re-centres on it.
   A jump larger than `CUT_JUMP` between samples is a cut in the source.
4. **Ease**: a zero-lag Gaussian (`EASE_SECONDS` 0.45) over each stretch between
   cuts, so every move eases in and out and a cut stays a cut.
5. **Render** in one ffmpeg pass: `sendcmd` feeds the planned `crop` x/y frame by
   frame, each command half a frame early so a timestamp that rounds up still
   lands on its own frame. No mp4v intermediate, no audio re-mux.

On a real face cam: direction reversals went from 50 to 4, the share of frames
with any movement from 39% to 21%, and the face stays within 4% of centre
(median).

Fixes that carried over, each of which once produced an opaque failure:

- **Downscaled detection.** At 1440p, full-resolution Haar cost more than the
  entire rest of the pipeline combined.
- **The `sendcmd` file path** is escaped as ffmpeg's filter syntax wants
  (`C\:/…`) — an unescaped drive colon ends the option on Windows.
- **Retry-with-backoff temp deletion.** Windows holds a file lock until every
  handle closes, and a real encoding error otherwise surfaces as a confusing
  `WinError 32` from the cleanup path.

It used to be about 9× slower than the other layouts — 91s for 30 seconds of
720p60 — and almost none of that was face tracking. The clip cut put `-ss`
*after* `-i`, which makes it an output seek: ffmpeg decoded the stream from its
first frame and threw frames away until it reached the clip, so a clip at 28:20
paid for decoding 28 minutes of video it never used. With the seek moved before
the input (and `-t` for a duration instead of `-to`), the same clip renders in
**10.8s on a GPU and 17.0s on a CPU** — the same ballpark as the stacked layout
— and `LayoutSpec.warning()` no longer warns about it.

### 7.4 `center` — plain centre crop

`render.py::_render_center_clip`. Widest centre crop at the target ratio, one
ffmpeg pass, no face detection at all. For *"gameplay only, no webcam"*.

### 7.5 Which processor — `accel.py`

Two stages can use a GPU, and they need different things from it.

**Video encoding** is ffmpeg's, and ffmpeg can hand it to the graphics chip —
NVENC on NVIDIA, VideoToolbox on a Mac, Quick Sync on Intel, AMF on AMD — which
needs only the graphics driver the machine already has. Every encode in the app
goes through `accel.run_encode(build_cmd, …)`: the caller builds the command
around a slot for the encoder arguments, and `accel` fills it. Each hardware
encoder maps libx264's CRF onto its own constant-quality control (`-cq`,
`-global_quality`, `-qp_*`, `-q:v`), so a clip looks the same whichever chip
made it.

Nothing is trusted from a name. `ffmpeg -encoders` lists what the *build*
supports, not what the *machine* can do — this laptop's ffmpeg lists AMD's AMF
with no AMD GPU in it — so `probe_video()` gives each listed encoder a
one-second test encode, in order, and uses the first that passes. The answer is
cached for the process. If a hardware encode then fails mid-run (an NVENC
session limit, a driver fault), `run_encode` redoes that clip on libx264 and
marks the encoder broken for the rest of the run; `reset_run()` at the start of
each job gives it another chance. Measured here: the stacked layout renders 30s
of 720p60 in **10.6s on NVENC against 20.3s on the CPU**.

**Transcription** runs on CTranslate2, which uses an NVIDIA GPU through CUDA —
but only with NVIDIA's cuBLAS and cuDNN, which the published single-file builds
leave out (~2 GB). The old check asked CTranslate2 whether it could *count* a
GPU, which it can on any NVIDIA machine, so packaged builds started on CUDA and
died on the first window looking for `cublas64_12.dll`. `whisper_device()` now
also loads the libraries with `ctypes` first; a missing one means the CPU and a
sentence saying so. A CUDA failure mid-run still falls back, and
`mark_cuda_failed()` keeps the rest of the run off the GPU. On a Mac the engine
has no GPU path at all, and the reason says that too.

The setting is `PROCESSOR` — `auto` (the default: whatever works fastest),
`gpu` (the same search, said out loud when there is no GPU), or `cpu` (never
touch one) — read live through `config.current_processor()`, so a change made
while a run is paused applies from the next clip. `LOCAL_WHISPER_DEVICE`, the
older developer knob, still outranks it for transcription when set. The
Processor box under the live preview reads `GET /api/processor` when the page
opens and again after every run, and "Check again" re-runs the encoder tests
for someone who has just installed a driver; while a run is paused and the encoder
has never been probed, it reports it as unknown rather than starting a test
encode that would wait on the pause.

### 7.6 Finding faces — `faces.py`

Both renderers find faces through one function, `faces.detect()`, which returns
`(cx, cy, width, confidence)` for every face in a BGR frame. It uses **YuNet**,
a ~230 KB learned detector from OpenCV's model zoo that OpenCV runs natively
through `FaceDetectorYN`, and falls back to the Haar cascades (frontal, then
profile both ways) if the model file is missing, this OpenCV lacks
`FaceDetectorYN`, or the model will not load — saying so once in the log.

The model is committed under `assets/models/` with its MIT licence and bundled
into every build, so nothing downloads at run time; `model_path()` finds it in
a source checkout or under PyInstaller's `_MEIPASS`. YuNet's own confidence
floor is 0.9, tuned for photographs; it is 0.7 here, because a webcam face in a
corner overlay is small and softly lit and both renderers already vote across
many frames.

Measured on the repo's test footage: the face cam's face found in 132 of 150
samples (Haar: 125), and the stacked locator's samples agreeing 18–20 times out
of ~20 (Haar: 3–16), at about 12 ms a frame on a laptop CPU. YuNet sees *more*
faces, not fewer — including the photos inside a game — which is why the
recurring-face vote stays.

### 7.7 The edit — `autoedit.py`, `captions.py`, `words.py`, `broll.py`

The user-facing guide is [docs/captions-and-editing.md](docs/captions-and-editing.md);
this section is how the code does it.

What the Shorts tools sell on top of the cut is editing: captions, the dead air
taken out, a zoom on the line that matters. `render_highlights()` does all of
it after the layout render and before the cold open, so every layout gets it
from one place and the replayed moment carries its captions with it. It is a
second pass over the finished clips, in three steps:

1. **Listen** (`words.py`). Each rendered clip goes through faster-whisper
   again with `word_timestamps=True`. The source transcript is in sentences,
   because ranking needs nothing finer and word timings over a four-hour VOD
   are not free. A 30–90 second clip is cheap, and its times are the clip's
   own, so trims, re-cuts and exact spans need no mapping back to the source.
   The model is loaded once per batch and released at the end of it.
2. **Plan** (`autoedit.analyse`). Nothing is encoded yet:
   - *Cuts.* A gap between words longer than 0.55s (0.9s on a stream) is dead
     air **only if it is quiet**, judged against the clip's own loudness
     envelope (`signals.analyse_audio`) at under 30% of the median speech
     level. A gap holding a laugh or an explosion in the game stays. So does
     the three seconds before the clip's `hook_peak`, because a quiet run-up
     to the loudest moment is a build, not a gap (`silence_to_peak` rewards
     exactly that). Fillers ("um", "uh") are cut; "like" is not. The envelope
     comes in 0.25s windows, so a pause is only listened to from one window in
     from each edge, or it hears the word next to it. Slivers under 0.2s
     without a word in them are dropped, and the piece count is capped at 40.
   - Every word is **remapped** onto the new, shorter timeline, and so is
     `hook_peak`, which the cold open reads afterwards (`hook_peak_offset`).
   - *Punch-ins.* A trigger phrase from `signals.TRIGGER_PHRASES`, a word
     with "!", or a word in the loudest 10% gets a 1.14× zoom to the end of
     its sentence, at most one per 8 seconds and 3.5s apart. On the face-crop
     layout, jump cuts alternate between 1× and 1.06×, the editor's trick
     that makes a cut read as a new angle rather than a stumble. The zoom
     aims at the webcam panel on the stacked layout.
   - *Emoji* are a controlled word list (`EMOJI_WORDS`, English), a few per
     clip, never the same one twice. The art is Twemoji (CC-BY 4.0), rendered
     to PNG in `assets/emoji/`.
   - *B-roll* is opt-in and needs the user's own Pexels key. One LLM request
     for the whole batch reads each clip's timed lines and names up to two
     concrete, filmable things, never in the first 3s or last 2s. Each is
     searched on Pexels, downloaded once to `output/b-roll/`, and reused. It
     never runs on a stream, where the gameplay is the picture.
3. **Render** (`autoedit.render`). One ffmpeg filtergraph per clip:
   `trim`/`atrim` + `concat` for the cuts (with 12ms audio fades so they
   don't click), a fixed-size `scale`+`crop` copy of the frame laid over it
   with `enable='between(t,…)'` for each zoom level (so nothing reconfigures
   mid-stream), `overlay` for B-roll and emoji, and `subtitles` last so the
   words sit on top. It runs with a working directory holding the ASS script
   and the style's font, so the filter names both relatively. An absolute
   Windows path inside a filtergraph needs escaping twice, and a drive colon
   or an apostrophe in a user name gets it wrong sooner or later. That is
   why `proc.run_checked` and `accel.run_encode` take `cwd`.

**Captions** (`captions.py`) are ASS for libass, one event per word: the whole
line on screen, the spoken word recoloured and popped with `\t` scale
transforms. The four presets are complete looks, not knobs: *bold* (Montserrat
Black, yellow), *punch* (Anton, two words, green), *clean* (sentence case on an
opaque box; a see-through box shows darker patches where libass's per-run
boxes overlap) and *comic* (Bangers, purple edge). The fonts ship in
`assets/fonts/` (OFL) because a caption look that depends on installed fonts
looks different on every PC. On the stacked layout the line sits on the seam
between webcam and gameplay; elsewhere at 70% height, above the band each app
covers with its own buttons. Lines break at the word limit, on punctuation, and
before a pause. Scripts written without spaces (Japanese, Chinese, Thai) are
joined without them.

**Settings** live on `LayoutSpec` (`captions`, `cut_pauses`, `punch_ins`,
`emoji`, `broll`), so a trim or a retry re-renders with the edit the run was
made with. A manifest from before these fields existed reads back with all of
them off, so re-cutting an old clip does not suddenly add captions. The prompt
can set them ("comic captions", "keep the pauses", "no zooms", "add b-roll").
Those phrases are parsed **first** and cut out of the prompt, because "cut the
pauses" would otherwise switch on the exact-span parser and "zoom in on
emphasis" would tighten the webcam crop. Where the words set a field they
win over the controls, and `edit_from_words` tells the UI which fields to tag.

**Fixing captions.** Each clip keeps `heard_words`, what Whisper heard on the
clip's own uncut timeline. `PUT …/captions` aligns the corrected text to them
with `difflib` (`captions.retime`): unchanged words keep their exact timing and
take the new spelling; a rewritten stretch spreads its new words over the time
the old ones took. The clip is re-rendered from the same span with
`heard_words` and `caption_words` passed in, so nothing is transcribed again.
The cuts and punch-ins are still planned from what was *heard*, so deleting
"um" from the text doesn't stop it being cut. Only `Plan.caption_words`
changes. Trimming drops both lists, because a new span means new words.
Trimming and the caption fix share `_rerender()` in `server.py`.

**The logo** (`brand.py`) lives in the settings folder, not beside any run,
because it belongs to the channel. It is overlaid after emoji and before the
captions, scaled into a box 16% of the frame's shorter side, at 85% opacity.
Bottom corners sit above the bottom fifth, where each app draws its own UI.
A logo-only edit skips Whisper entirely (`Options.needs_words`).

The release workflow runs `.github/scripts/check_captions.py` against each
bundled ffmpeg, burning a real caption with a bundled font. A build without
libass fails on Windows and Linux and warns on the Mac beta, because without
libass every clip would ship with no captions and nothing would say so.

Every step fails soft. No faster-whisper means no words, so no captions, cuts
or punch-ins. An encode that fails keeps the plain render. `polish()` never
raises into the run. What was done is recorded on the clip as `edit`.

### 7.8 Why it ranked — `scorecard.py`

Every clip already carried the numbers it was ranked on: `hook_score` and
`viral_score` from the model, and `signals` from the audio and the words.
`scorecard.build()` turns them into four 0–100 parts: **Hook**, **Moment**,
**Energy** (`audio_spike`) and **Pace** (opening words per second against
2.5), each drawn only when it was actually measured. It adds a grade (Top pick
≥ 85, Strong ≥ 70, Worth a look ≥ 55, Long shot), the model's own
`virality_reason`, and up to three plain notes ("A quiet beat before the
payoff"). It is attached in `_finalize` and again when a run is restored from
disk, so older runs get one too. Exact spans and adopted files have no score
and get no card rather than an invented one. The library can sort each run by
Hook, Energy or length as well as by rank.

---

## 8. Natural-language layout parsing

`layout_spec.py` turns free text into a validated `LayoutSpec`. Type
*"webcam at the top, square, 5 clips"* and the frame preview updates as you type.

### 8.1 Two stages, in this order

**Stage 1 — deterministic keywords.** Runs in ~70ms, costs nothing, works
offline. This is what the live preview calls on every keystroke (debounced 450ms).

**Stage 2 — the LLM.** Consulted **only when the keyword pass resolved absolutely
nothing.** Anything the keywords did resolve came from the user's literal words,
so a second opinion adds latency and quota without adding accuracy. And if the
LLM call fails, the parser notes it and keeps the defaults — a layout prompt is a
convenience, never a hard dependency.

### 8.2 Parse order is load-bearing

Each step **strips the text it matched**, so later steps can't misread the
leftovers:

| # | Step | Why here |
|---|---|---|
| 1 | **Clip length** (`"between 20 and 40 seconds"`) | Reads identically to a timespan. Getting this order wrong turned a request for 20–40s clips into a single exact cut from 0:20 to 0:40, with ranking skipped entirely. The word `cut` is the app's explicit-span verb, so its presence hands the phrase to the range parser instead |
| 2 | **Time ranges** (`"14:45 to 15:30"`) | Stripped so `1:30-2:45` can never be mistaken for an aspect ratio later |
| 3 | **Clip count** (`"5 clips"`) | Stripped so `5 shorts` can't *also* be read as a request for 9:16 |
| 4 | **Aspect ratio** | Explicit ratios and unambiguous words before loose platform names |
| 5 | **Layout** | Most specific first: "no webcam"→`center`, "follow my face"→`facetrack`, "webcam on top"/"pip"→`stacked` |
| 5b | **Kind of video** | "my vlog", "podcast", "tutorial", "stream" — only when exactly one kind is named ([§8.3](#83-what-comes-out)) |
| 6 | **Webcam corner** | Only read when the phrasing is about *locating an overlay* — "webcam on top" is a layout instruction, not a corner |
| 7 | **Panel size** | bigger→0.55, smaller→0.30, or explicit `"60%"` |
| 8 | **Face zoom** | "closer"→3.0, "wider"→7.5 |

Timestamp parsing accepts `1:30`, `00:01:30`, `90s`, and bare numbers *only*
inside `from X to Y` (bare numbers collide with percentages and resolutions
otherwise). The regex's trailing `\b` matters: without it, `"928 square"` parses
as `"928 s"` and eats the leading letter of the next word.

Stripping several matches has an order of its own: **from the back**. Every
match's offsets are measured on the text before anything is removed, so cutting
front-to-back shifts each later cut onto the wrong characters — see
[§17.1](#171-fixed-since-this-document-was-written).

### 8.3 What comes out

```python
LayoutSpec(
    layout="stacked",              # stacked | facetrack | center
    aspect_ratio="9:16",           # 9:16 | 4:5 | 1:1 | 16:9
    webcam_corner="bottom-left",
    cam_panel_fraction=0.42,       # clamped 0.15–0.75
    face_zoom=5.0,                 # clamped 2.0–12.0
    num_clips=10,                  # clamped 1–10
    match_source_quality=True,
    time_ranges=[],                # [[start, end], …] → skips transcribe + rank
    clip_seconds=None,             # [min, max] → becomes a prompt instruction
    brief="",                      # the whole prompt, handed to the ranker
    content_kind="auto",           # stream | vlog | podcast | tutorial | other
    layout_set=False,              # did the words choose a framing?
    notes=[],                      # human-readable "what I understood"
)
```

Two subtleties worth knowing:

**`clip_seconds` is a request to the model, not a trim.** Cutting a clip to
length afterwards would slice mid-sentence, so the length has to be part of what
the ranker is *asked to find*.

**`brief` keeps the whole prompt, unparsed.** Framing words in it are harmless
noise to the ranker, and trying to subtract them would cost exactly the nuance
(*"only the rage moments"*, *"hooks that ask a question"*) that makes the brief
worth having.

**The kind of video can move the framing, but only a default one.** "my vlog",
"podcast", "tutorial", "stream" or "gameplay" in the prompt sets `content_kind` —
only when exactly one kind is named, since "the interview bit of my stream"
names two. A vlog or podcast then follows the face (`apply_kind()`), because its
camera *is* the picture — unless the words already chose a framing, which
`layout_set` records. Someone who typed "webcam at the top" over a podcast meant
it. The *Kind of video* picker does the same through `_override_kind()` in the
server, and beats the words the way the Shape toggle does. A kind the ranker
*detected* never moves the framing: getting that wrong on a stream would put a
face-follow crop on a webcam overlay, and the stacked renderer already falls
back to following a face that fills the frame.

`validate()` clamps every field into a renderable range and **never raises** —
bad input degrades to a working render rather than an error. `from_dict()`
rebuilds a spec from a saved manifest, so re-cutting a clip months later still
renders in the layout it was first made with.

`notes` is why the UI can show *"clip count → 5"*, *"aspect ratio → 1:1"* under
the preview: the parser reports what it understood, so the user can see their
words actually landed.

---

## 9. SEO: the packaging step

A rendered clip is only half the job. What decides whether a Short gets seen is
the packaging: a title that earns the tap, a description that tells the algorithm
what the clip is about, and tags that file it next to the videos its audience
already watches.

`seo.py` asks the same model that ranked the highlights to write that packaging —
**one call for the whole batch** per pass, not one per clip. Ten separate calls
would take ten times as long and give the model no way to stop the titles
repeating each other.

There are two passes, because one clip is posted to three apps that reward
different things. The first writes the YouTube Shorts packaging. The second
(`_write_social()`) takes the chosen title, search phrase and hook and writes the
**Instagram Reels** and **TikTok** packaging from them. Both prompts cast the
model as a social media strategist and hand it the growth playbook below.

### The growth playbook — `playbook.py`

How YouTube Shorts, Instagram Reels and TikTok decide who sees a video changes
several times a year, and what the writer knows about that should not be frozen
into whichever build someone downloaded. So it lives in a Markdown file,
`assets/playbook/short-form-algorithms.md`, not in the prompt. It says what each
platform measures (swipe-away rate and replays on Shorts; watch time, likes per
reach and sends per reach on Reels; completion and search on TikTok) and what the
packaging should do about it. Its first line is a review date.

`load()` takes the most specific copy there is:

1. `playbook.md` in the settings folder — your own notes, always;
2. a copy fetched from the repository's `main` branch and cached as
   `playbook-cache.md`, taken only when its `reviewed:` date is **newer than the
   bundled one**, so a stale cache can never roll a fresh build backwards;
3. the copy bundled into the build (`build_exe.py` adds `assets/playbook`).

`refresh()` runs in the background once at startup and asks GitHub at most once
a day. It is the updater's trust model at a smaller scale: one fixed URL on
`raw.githubusercontent.com` in this project's own repo, text only, capped at
64KB, and any failure leaves the copy on disk in charge. So updating what every
installed copy knows about the algorithms is an edit to one file and a push — no
release. `prompt_text()` strips the review date and the maintenance note before
the model sees it; to a model they are noise, or an instruction to go and edit
something.

### Naming the subject once — `detect_subject()`

A title that does not name its subject is invisible outside the feed: it holds
no word anyone would search or browse for. The prompt has always demanded the
name, but the model had to find it per clip from whatever the transcript
happened to say — and for a stream that is usually nothing. Nobody announces
the game they are playing every ten minutes.

So one extra call, before any title is written, asks what the video is *about*
from its own listing plus a sample of what is said, and returns a small fixed
vocabulary for the whole run:

```json
{"subject": "Elden Ring", "kind": "game", "also_known_as": ["ER"],
 "hashtags": ["#eldenring", "#soulsgames"], "tags": ["elden ring", "boss fight"]}
```

`subject_block()` states it to the metadata writer as a requirement — every
title must place the clip in that subject — and the vocabulary is then enforced
in code rather than trusted to the prompt. `_merge_hashtags()` puts `#shorts`
first, the run's controlled tags next, and the model's own suggestions behind
them; the subject's name leads every tag list whatever came back. Ten clips
inventing ten ways to say the same thing compete in ten narrow slices of the
feed instead of stacking in one.

The subject is stored on the `Job` and written into `job.json`, so a rewrite
months later files the clips under the same name as the original run. An empty
subject is a valid answer — it means the titles are written from the clips
alone, exactly as before.

It also returns `all_subjects`: every game, show or topic the listing and the
transcript mention. The transcript sample is spread across the whole video
(`_spread_sample`), not its first minutes — a variety stream spends those on
its first game. That list is the candidate set for the next step.

### What is on screen — `vision.py`

One subject per run was the bug. A stream titled *"The Finals chill stream"*
that moved on to Firewatch and a horror game came back with Firewatch clips
tagged `#thefinals`, and the horror clip named after a game it was not.
Nothing ever looked at the screen, and nobody says "I am now playing Firewatch"
out loud.

So before any title is written, `_look_at_clips()` in the job runner sends four
frames per clip (768px on the long side) to a vision-capable model through
`call_vision_llm()` — Gemini, then OpenAI, then Groq's vision model, up to 20
frames per request (5 on Groq). Each clip gets a `scene`: its `content_type`
(gameplay, podcast, talking head, storytelling, tutorial…), `layout`,
`subject`, `named_by`, `subject_guess`, `genre`, `on_screen_text`, `scene`,
`people`, `mood`. It is saved on the clip in `job.json`, so a rewrite never pays
for it twice; clips made before this are looked at once, from the source if it
is still on disk and from the rendered clip if not.

**The evidence rule** is the part that matters. Tested on the real clips, the
model called a Fears to Fathom clip *Phasmophobia* at confidence 1.0 — dark
houses look alike, and its confidence number meant nothing. So a name counts
only when `named_by` is `on_screen_text` (including characters or places unique
to one title — "Henry:" and "Delilah:" subtitles are Firewatch), `speech`,
`source_listing` or `unmistakable` (Minecraft-level fame). Anything else is
demoted to `subject_guess` in `coerce_scene()`, however sure the model sounded.

`seo.clip_subject()` then decides what each clip is filed under, in order: a
name the user typed (`subject_override`); a name the frames confirm; a guess
that matches one of `all_subjects`; the run's subject, as long as the frames do
not point elsewhere. Anything less certain leaves the clip unnamed and filed by
its genre — the prompt is told the guess and told not to print it, and
`_tag_options()` strips it from the tags as well. The run's controlled hashtags
only apply to clips about the run's subject (`_controlled_hashtags`), and the
format tags follow the clip's `content_type` (`FORMAT_TAGS`) — a podcast clip is
no longer tagged "gaming clips".

On that stream, rewriting after the change: three clips confirmed Firewatch from
their subtitles, one The Finals from its HUD, and the horror clip filed as
"horror game" with its guess shown in the panel for a person to confirm.

### Several titles, ranked

The writer returns `TITLE_OPTIONS` (5) titles per clip, each on a named angle —
*search*, *curiosity*, *reaction*, *detail*, *stakes/contradiction* — and scores
each on an editor's rubric: **hook** 0–40, **clarity** 0–20, **search** 0–20,
**truth** 0–20. `_rank_options()` then:

- drops any option with truth under 14 — the model's own admission it overclaims;
- blends the rubric with `score_title()`, the app's own 0–100 check of what a
  model is bad at: length (35–70 characters), whether a confirmed subject is
  named and named within the first 40 characters, shouting, filler phrases,
  hashtags, emoji, and whether it prints an unconfirmed guess;
- orders them `0.7 × rubric + 0.3 × check`.

The best becomes `title`; all of them are kept as `title_options` for the Boost
panel. **Descriptions are ranked the same way:** `DESCRIPTION_OPTIONS` (2) per
clip, a *search* angle and a *story* angle, scored on hook 0–40, search 0–30,
clarity 0–15, truth 0–15, and blended with `score_description()` — which checks
that the subject or search phrase is in the first line, the length, and that
there are 3–5 hashtags. `_rank_texts()` does the blending for descriptions and
captions alike, and drops anything the model scored as overclaiming. `_spread_leads()` then makes sure no two clips in one batch open with the
same three words, taking a clip's next-best option where they would. Tags come
back ranked and labelled by kind (subject, variant, query, genre, moment,
format), and `_score_tags()` gives each a score: 60% what kind of term it is
(`TAG_KIND_WEIGHT` — the exact subject is worth most, a format term least) and
40% where the model placed it, then re-sorts. The top ones fitting YouTube's
budget go in the box and all of them are kept as `tag_options`. Each clip also gets a `search_phrase` — the one phrase it
should rank for — and an `about` block saying what it was filed under and why.

### Reels and TikTok — `_write_social()`

The second pass writes, per clip:

- **Reels:** `CAPTION_OPTIONS` (2) captions — a *search* angle for Instagram
  search, a *send* angle written to be forwarded — with the hook and keyword
  inside the first 125 characters, where Instagram folds a caption. Scored on
  hook 0–40, search 0–25, share 0–20, truth 0–15 and blended with
  `score_caption()`. Plus 3–5 hashtags, **cover text** of 2–5 words for the
  3:4 profile grid, and **alt text**.
- **TikTok:** one caption with the search phrase in its first line and a question
  that invites comments, scored by `score_caption()`, and 3–5 hashtags.

`_social_hashtags()` puts the clip's own controlled terms first and drops
anything that means nothing on these apps (`OFF_PLATFORM_HASHTAGS`: `#shorts`,
`#fyp`, `#foryou`, `#reels`, `#viral`…). Hashtags are stripped out of the
caption sentence itself and kept on their own line.

**If the second pass fails**, the YouTube packaging is not touched, and every
entry already carries `_social_fallback()`: the YouTube description with its
hashtags taken out and `#shorts` removed, marked `generated: false` so the panel
can say so. The second pass is skipped entirely when the first produced nothing
but fallbacks — it would be writing Instagram copy from a filename.

### The two rules that outrank everything

1. **Accurate.** Every claim must be provable from that clip's own transcript
   and its `scene`, both of which are given to the model. A title the clip fails to deliver gets swiped in
   two seconds, and short-form ranking punishes that harder than a boring title
   ever could.
2. **Viral.** Within what is true, pick the most arresting framing. A real
   specific detail — the exact number, the actual thing that happened — beats a
   vague tease every time.

### What the prompt encodes about Shorts distribution

- Titles and descriptions **outrank tags** as ranking signals, so the words that
  name the subject must appear in the title and the first line of the
  description, not only in the tag box.
- The **first three hashtags** render as clickable links above the title, making
  them the most visible metadata on the whole upload.
- A hashtag **in the title** buys nothing and spends characters you need for
  keywords.
- **The title must name the subject** — the game, person or topic. A title lifted
  straight from the transcript reads fine to someone who watched the stream and
  is invisible to everyone else, because it contains no word anyone would search
  or browse for.

Limits are enforced in code, each set slightly *under* the platform's real cap
so a stray character can't get the upload rejected: `TITLE_LIMIT` 100,
`DESCRIPTION_LIMIT` 4800, `MAX_TAGS` 15 in the box (up to `MAX_TAG_OPTIONS` 24
offered), `MAX_HASHTAGS` 5, `TAGS_TOTAL_LIMIT` 460 (real cap 500),
`REELS_CAPTION_LIMIT` 2150 (2200), `TIKTOK_CAPTION_LIMIT` 3900 (4000),
`COVER_LIMIT` 40, `ALT_TEXT_LIMIT` 100.

### Best-effort, but honestly reported

This step sits on top of a clip that **already exists**, so every failure path
falls back to a hook-line title rather than sinking a run that already paid for a
download, a transcription and a render.

But the caller must be able to tell the difference. So each entry carries a
`generated` flag, and failures are appended to an `errors` list rather than only
printed. `regenerate_seo()` uses that flag carefully:

```python
if seo.get("generated") or not clip.get("seo"):
    store.set_seo(job, clip["file"], seo)
```

Only overwrite existing metadata when a model actually wrote the replacement.
Without that check, the **"Rewrite" button — whose entire job is to *improve*
what's there — would quietly downgrade a good title into a filename.**

And if *nothing* was generated, `regenerate_seo()` raises. Returning a count of
fallbacks read as success all the way to the UI, which then reported titles were
"ready" when nothing had changed.

### The title is also the filename

`short_01.mp4` tells you nothing in a folder of thirty. Since ranking and SEO
both run *before* rendering, the title exists by the time the mp4 does — so
`_finalize()` renames each clip to it, and rewriting the titles renames the files
again. `safe_stem()` makes that survive a filesystem and a URL path segment:
emoji and the characters Windows forbids are dropped, combining marks are kept
(losing them turned Hindi's `क्या` into `क य`), reserved device names and
over-long stems fall back, and collisions take a `_2` suffix.

### Editing it yourself — `apply_edit()`

All four fields were already editable in the panel, and the edit was thrown away
the next time it rendered. Editable text you cannot save is worse than read-only
text, because it looks like it worked.

`PUT /api/jobs/{id}/clips/{file}/seo` merges whatever fields were sent into the
clip's metadata, renames the mp4 to match a new title, and persists. It applies
YouTube's **limits** — a 130-character title is rejected whoever typed it — but
not the house **style**: the no-hashtags-in-titles rule exists to stop a model
padding, and someone who types one into their own title meant it. The entry is
marked `edited`, which is what makes Rewrite ask before replacing hand-written
words.

It also takes the Reels and TikTok fields — `reels_caption`, `reels_hashtags`,
`reels_cover_text`, `reels_alt_text`, `tiktok_caption`, `tiktok_hashtags` —
with hashtags accepted as the one line the box shows or as a list.

The same route takes a `subject`: what the clip is actually about, when the app
got it wrong or could only guess. It is saved on the clip as `subject_override`
rather than in the text, and outranks everything in `clip_subject()` on every
rewrite after. The panel's Rewrite saves a typed subject first, then calls
`POST /api/jobs/{id}/seo?only={file}` — one clip, with the rest of the run's
titles passed as `avoid_titles` so the new one does not open the same way.

### Finding the words for old clips

`_clip_words()` works cheapest-first: slice the clip's span out of the cached
`.srt` if one exists (free), otherwise transcribe the clip file itself (quick — a
Short is under a minute). This is what gives clips made before any of this
existed a real title, including ones whose only remaining trace is the mp4.

---

## 10. Transcription

`local/transcriber.py`, faster-whisper.

**Device selection** is auto by default: `cuda` + `float16` when a usable GPU is
found, otherwise `cpu` + `int8`. Getting this right took three separate fixes,
and each failure was silent — worth studying as a case of *asking the wrong
component a reasonable-sounding question*:

1. **Ask CTranslate2, not torch.** The original probe called
   `torch.cuda.is_available()`. But faster-whisper does not run on torch — it
   runs on **CTranslate2**, and torch is not installed here at all (it is an
   explicit `--exclude-module` in the build, because it adds ~2GB and is not
   used). So the import raised `ImportError`, the handler swallowed it, and
   every machine took the CPU path. The correct probe is
   `ctranslate2.get_cuda_device_count() > 0`.
2. **Register the DLL directories.** `nvidia-cublas-cu12` and
   `nvidia-cudnn-cu12` install their DLLs under `site-packages/nvidia/*/bin`.
   Python 3.8+ removed the current directory and `PATH` from the DLL search
   order, and CTranslate2 — unlike torch — never calls `os.add_dll_directory`
   for them. So *installing the wheels changes nothing*: the device counts, the
   model constructs, and the first `encode()` dies on `cublas64_12.dll is not
   found`.
3. **Ship them.** PyInstaller cannot see a dependency that is resolved by DLL
   search path rather than by import, so the build needs
   `--collect-binaries nvidia`.

The lesson worth keeping: **a device that enumerates, and a model that
constructs on it, are both worthless as proof.** Only draining the segment
generator exercises the maths library. So the fallback wraps the *entire*
transcription, not the constructor:

```python
try:
    segments, info = _run(device, compute_type)      # drains the generator
except Exception:
    if device != "cuda":
        raise
    device, compute_type = "cpu", "int8"
    segments, info = _run(device, compute_type)      # redo the whole thing
```

Measured on an RTX 5060 Laptop (8GB), 900s of audio:

| model | device | time | realtime factor |
|---|---|---|---|
| base | cuda | 25.3s | 35.6x |
| base | cpu | 46.1s | 19.5x |
| small | **cuda** | **20.8s** | **43.3x** |
| small | cpu | 104.2s | 8.6x |

Note `small` on CUDA beats `base` on CUDA. On a GPU the *more accurate* model is
also the faster one, which matters because model size is the main defence
against the hallucination described next.

**Language pinning.** The spoken language defaults to `en` rather than
auto-detect. Whisper re-decides the language on unclear audio, and on a game
stream — music beds, effects, non-speech — it drifts and then generates fluent,
confident text in the language it landed on. A real 3h47m English VOD came back
with **703 of 1097 cues in Korean**, none of it spoken. One of those hallucinated
cues propagated into a clip's `hook_sentence`, then into its SEO title (via the
fallback in [§9](#9-seo-the-packaging-step)), then into the filename on disk.
`"auto"` is still available and is the only value that restores auto-detection.

**Settings that matter:**

- `beam_size=5`
- `condition_on_previous_text=False` — stops Whisper looping a hallucinated
  phrase forward through a long VOD. Note this limits *propagation* of a
  hallucination, not its *occurrence*; language pinning addresses the latter.
- **VAD off by default.** Voice-activity detection is too aggressive on mixed
  speech/music content — a stream with a game score under the mic loses real
  speech to it. Enable with `LOCAL_WHISPER_VAD_FILTER=true`.

**The cache** is an `.srt` written next to the video, validated by modification
time (`cache_mtime >= source_mtime`). SRT rather than JSON because it's a format
the user can open, edit, and hand to a subtitle tool.

An empty or zero-duration cache is treated as **invalid**, deleted, and
re-transcribed — that shape comes from a run that died partway, and honouring it
would mean an empty transcript forever.

**This is the slowest step in the pipeline and it is paid exactly once per
video.** Every re-rank and re-render afterwards is free. That fact drives the
workflow in [§15](#15-caching-seven-independent-layers).

---

## 11. The backend: job runner + web layer

### 11.1 Why a queue exists at all

Transcription alone outlives any sensible HTTP timeout. A three-hour VOD is not a
two-second wait.

So `POST /api/jobs` **only ever enqueues and returns an id**. The browser follows
progress separately over server-sent events. The request never blocks on the
pipeline.

### 11.2 One worker, on purpose

```python
self._worker = threading.Thread(target=self._run_forever, daemon=True)
```

Jobs run **one at a time on a single worker thread**. That is deliberate, not a
limitation: Whisper and ffmpeg are both CPU-bound, and running two at once makes
both slower than running them in sequence. A queued job reports its depth
(*"Queued — 2 job(s) ahead"*) rather than pretending to run.

The pattern is a classic producer/consumer: FastAPI handlers produce job ids into
a `queue.Queue`, the worker consumes them forever. All shared state is guarded by
a single `threading.Lock`.

### 11.3 Progress is derived, not guessed

This is the nicest trick in the backend.

The pipeline already narrates itself to stdout. `_JobStdout` — a tiny
`io.TextIOBase` subclass — captures that stream line by line inside a
`contextlib.redirect_stdout`, and `_log()` maps each line's prefix onto a stage:

```python
[download…]                        → download
[transcribe…]                      → transcribe
[highlights…] [llm…] [rank…]       → rank
[stack…] [clip/local…] [center…]   → render
[edit…] [broll…]                   → edit
```

Each stage owns a band of the progress bar, sized by how long it actually takes:

```python
download   0.00 → 0.15
transcribe 0.15 → 0.55      # dominates on CPU, so it gets the widest band
rank       0.55 → 0.70
render     0.70 → 0.86
edit       0.86 → 1.00      # captions and the edit; skipped when they are off
```

The edit pass goes over the clips twice (listen to every one, then encode every
one) and reports it as one count of `2n` steps (`[edit] 3/20: …`), so the bar
keeps moving through both halves instead of filling once and stalling.

And when a line carries an `N/M` counter — `[stack] 2/5: …` — the bar moves to
the right position *inside* the render band:

```python
frac = (done - 1) / max(1, total)
job.progress = lo + (hi - lo) * frac
```

**The pipeline has no idea a UI exists.** It just prints what it's doing, and a
render reporting `3/5` moves the bar correctly without any coupling.

Progress is monotonic (`max(job.progress, …)`), so a late-arriving line can never
make the bar jump backwards.

### 11.4 Streaming it to the browser

```python
@app.get("/api/jobs/{job_id}/stream")
async def stream_job(job_id: str) -> StreamingResponse:
    async def gen():
        last = -1
        while True:
            snap = job.snapshot()
            if snap["version"] != last:      # only send on real change
                last = snap["version"]
                yield f"data: {json.dumps(snap)}\n\n"
            elif idle % 30 == 0:
                yield ": ping\n\n"           # keep-alive every ~15s
            if snap["status"] in ("done", "error"):
                break
            await asyncio.sleep(0.5)
    return StreamingResponse(gen(), media_type="text/event-stream")
```

Every mutation bumps `job._version`. The generator polls twice a second but
**only emits when that version actually changed**, so an idle job costs one
comment line every 15 seconds instead of two full snapshots per second. The
stream closes itself when the job finishes.

`X-Accel-Buffering: no` and `Cache-Control: no-cache` stop proxies from buffering
the stream into uselessness.

### 11.5 Not blocking the event loop

FastAPI handlers are `async`, so any blocking call would stall every other
request. Anything slow goes through a thread:

```python
spec = await asyncio.to_thread(parse_layout_prompt, req.prompt, None, req.use_llm)
written = await asyncio.to_thread(regenerate_seo, STORE, job, force)
result = await asyncio.to_thread(_render)
```

### 11.5b Trying again — by itself, then on request

A run is long, and the stages in it fail for reasons that usually do not
repeat: a host that is briefly overloaded, a transcription that runs out of
memory once, a render that trips over a file Windows has not released yet.
Losing forty minutes of paid-for work to one of those is the worst outcome
the app has, so there are two layers.

**Automatic.** `JobStore._attempt()` wraps the download, transcription and
ranking: `STAGE_ATTEMPTS` (3) tries, 5s then 15s apart, each retry printed to
the run's log with the reason. `_is_permanent()` short-circuits errors trying
again cannot fix — an invalid API key, a spent daily quota, a private or
unavailable video, a local file that is not there — because retrying those
only delays the same message. Rendering is per clip: `_render()` renders the
batch, then gives each clip that failed `CLIP_ATTEMPTS` of its own, under its
own name (`short_03_try2_01.mp4`) so a half-written file from the failed
attempt is never mistaken for it.

**After a crash.** The same property makes a *closed app* recoverable, and for
free. A job's manifest used to be written only when the run finished, so a run
killed halfway left a folder nobody could explain. It is now written when the
job is created and at every stage boundary, carrying `status` and `stage`. On
the next launch, `_job_from_folder()` reads a manifest still saying "running"
as an **interrupted** run — restored even with no clips yet — and the page
offers it back. `POST /api/jobs/{id}/continue` (`carry_on()`) puts it on the
queue, and because every stage is idempotent and cached, it walks past the
download, the transcript and the checkpointed ranking chunks to the first
thing it had not done. Measured on a run killed during ranking: resumed and
finished in 98s, with the download and transcription skipped entirely.

**On request.** When a stage still fails, the progress panel offers **Try
again** — `POST /api/jobs/{id}/retry` puts the same `Job` back on the queue.
Nothing about that is special-cased, because the caches already make a second
run resume: the download is cached, the transcript is an `.srt` beside it, and
the ranking is checkpointed per chunk. A run that finished with some clips
missing offers **Retry failed clips** instead (`?clips_only=true`), which sets
`job.mode = "clips"` and has the worker re-render only those, from the source
still on disk, putting each back in its place and renaming it to its title.
The snapshot's `failed` and `source_on_disk` are what the page reads to decide
which of the two to show.

### 11.6 Persistence — surviving a restart

Every finished job writes a `job.json` manifest beside its clips, written to a
temp file then `os.replace`d so a crash mid-write can't corrupt it (atomic
rename). Manifest writes **never raise**: losing a manifest costs the library a
row, but letting the write throw would cost the user a render they already waited
for.

`restore()` runs at startup and on every `/api/library` hit, adopting two shapes
of folder:

- **With a manifest** → rebuilt completely: titles, scores, spans, SEO. Clips
  listed but no longer on disk are dropped (someone deleted them by hand).
- **Bare mp4s** → adopted from filenames alone, with `_title_from_filename()`
  turning `short_03.mp4` into `Clip 3`. These are clips from before manifests
  existed; without this they'd be invisible to the app that made them.

Before this existed the store was purely in-memory — close the app and every clip
you'd ever made became unreachable through the UI, even though the mp4s were
still sitting in the folder.

### 11.7 Clip editing — re-cut from source

`POST /api/jobs/{id}/clips/{file}/trim` re-runs the **renderer over the original
download** with new timestamps. It does not trim the rendered file.

That distinction *is* the feature: re-rendering means the span can **grow** as
well as shrink, and the layout stays exactly what the user asked for the first
time. Trimming the output could only ever remove.

The new render gets a fresh `edit_NN_<timestamp>` name, replaces the old entry
**in place** (keeping its position in the list), and the superseded file is
deleted. It goes through the edit like any other render, so the new span comes
back captioned and cut. Its old `heard_words` and `caption_words` are dropped,
because a new span is new words.

`PUT /api/jobs/{id}/clips/{file}/captions` shares the same `_rerender()`: same
span, same edit, with the corrected words passed in, so nothing is transcribed
again (§7.7).

### 11.8 Security posture

The app binds `127.0.0.1` and has **no authentication**, which is correct for a
local desktop app and stated plainly in the warning `python -m webapp` prints
when you bind elsewhere.

The defences that do exist are about path handling, since filenames come from
the client:

```python
def _clip_path(job, filename: str) -> Path:
    safe = os.path.basename(filename)          # strip any directory part
    root = Path(job.out_dir).resolve()
    path = (root / safe).resolve()
    try:
        path.relative_to(root)                 # prove it didn't escape
    except ValueError:
        raise HTTPException(403, "That file is outside the job folder.")
    return path
```

`os.path.basename` first, then resolve, then prove containment with
`relative_to`. Uploads are similarly constrained: extension checked against a
whitelist, filename sanitised to `[A-Za-z0-9._-]`, collisions given a numeric
suffix rather than overwriting.

And one deliberate non-parameter:

```python
# The URL is hardcoded on purpose — this opens an external page, so it must
# not be steerable by anything the page sends.
url = "https://www.youtube.com/upload"
```

### 11.9 Disk cleanup

`/api/cleanup` is deliberately narrow: **downloaded source videos,
half-finished `.part` files, and the B-roll cache in `output/b-roll/`** (stock
footage, fetched again whenever a clip asks for it). Rendered clips live elsewhere and are the
whole point of the app. Transcripts stay too — a `.srt` is a few hundred KB and
saves re-transcribing hours of audio.

It refuses to run while a job is in flight (409), since the source that job is
reading is exactly what would be deleted.

---

## 12. The frontend

`webapp/static/` — three files, ~3,500 lines, **no framework, no build step.**

| File | Lines | What |
|---|---|---|
| `index.html` | 444 | Full page markup + an inline SVG sprite sheet |
| `app.js` | 1,514 | All behaviour, plain DOM, no dependencies |
| `style.css` | 1,514 | Design system in CSS custom properties |

### 12.1 The design decision

The UI is built on **YouTube's own layout** — masthead, left guide rail, card
grid, a player with a right-hand control rail — because the audience is people
who post to YouTube. They already know how to use it. From the CSS header:

> *Surfaces, spacing and motion follow youtube.com's dark theme so the app feels
> like a place the user has already been; the neon accents and depth are ours.*

The design system is CSS custom properties on `:root`: YouTube's dark surfaces
(`--bg: #0f0f0f`, `--raised: #181818`, `--hover: #272727`), an accent set
(`--red: #ff0033`, `--blue: #3ea6ff`), a radius scale, and **one easing curve for
everything that moves** (`cubic-bezier(.2,.8,.25,1)`) with two durations. One
easing function across the whole app is what makes motion feel like a single
system rather than a pile of separate animations.

### 12.2 Page structure

```
masthead        search box (paste a link) · file picker · Create · folder · settings
guide rail      Create · Your clips · Settings · Clips folder · Upload to YouTube
main
 ├── setup panel        first-run only: provider + API key
 ├── chip bar           clickable example phrases
 ├── 1 Source           drag-drop zone / URL / channel grid
 ├── 2 What to make     the layout prompt + aspect toggle
 ├── 3 Render           format, spoken language, the Edit box
 │                      (captions, pauses, punch-ins, emoji, B-roll, logo),
 │                      cold open, Generate
 ├── side: Live preview the 9:16 frame, redrawn as you type, with a
 │                      caption sample and the logo where they will sit
 ├── progress panel     stage pills, % bar, live pipeline log
 └── results panel      search + date chips + sort, then the clip grid
                        (each card: rank, score, four score bars, the reason)
player overlay   video + control rail + trim panel + captions panel + SEO panel
mini player      persists while you scroll
drawer           settings: provider, model, budget meters, logo, Pexels key,
                 save location, cleanup
```

### 12.3 The live preview

The single most important interaction. As you type, the preview redraws **the
real frame shape and the real webcam panel height** — so you can see your words
land before spending a second of render time.

```js
$("prompt").addEventListener("input", () => {
  syncChips();
  clearTimeout(specTimer);
  specTimer = setTimeout(refreshPreview, 450);   // debounce
});

async function refreshPreview() {
  try {
    const d = await api("/api/layout/preview", json("POST", {…}));
    drawPreview(d.spec, d.summary, d.notes, d.warning);
  } catch (_) { /* the preview is cosmetic — never block on it */ }
}
```

Three things to notice:

- **Debounced 450ms**, so typing doesn't fire a request per keystroke.
- **The parse happens server-side** — `POST /api/layout/preview` runs the *same*
  `parse_layout_prompt()` the real job will run. There is no duplicated parsing
  logic in JS that could drift from the Python.
- **The catch is empty on purpose.** The preview is cosmetic; a failed preview
  must never block the user from rendering.

`drawPreview()` sizes a div to the spec's real aspect ratio, sets the webcam
panel to `height × cam_panel_fraction`, prints the resolution, lists the parser's
`notes` so the user sees what was understood, and shows the exact-cut box when
`time_ranges` came back. It also draws a caption sample, in the real bundled
typeface (served by `GET /api/fonts/{name}`), at the size and height the
burned-in captions will have on that layout, and the uploaded logo in its
corner.

**The Edit box** sends its switches with every preview and every job. The
server applies them except where the prompt's words named the same thing
(`edit_from_words`); the preview hands that list back and the box tags those
controls *set by your words*. A click on a control the words decided is
answered with a toast rather than silently undone by the next preview. The
switches are remembered per machine in local storage, because a caption style
is a channel's look rather than a per-run choice.

### 12.4 The chips

The example chips (`"webcam at the top"`, `"cut 14:45 to 15:30"`) aren't buttons
that set hidden state — they **insert their phrase into the prompt text**, and
`syncChips()` lights up any chip whose phrase is currently present. Clicking an
active chip removes it, with a regex that avoids leaving a stray comma behind.

The prompt box stays the single source of truth. The chips are just shortcuts for
phrases the parser already understands.

### 12.5 Following a job

```js
es = new EventSource(`/api/jobs/${job.id}/stream`);
es.onmessage = (ev) => onUpdate(JSON.parse(ev.data));
es.onerror = () => {
  // The stream drops when the job ends; fall back to one direct read.
  es.close();
  fetch(`/api/jobs/${job.id}`).then(r => r.json()).then(onUpdate).catch(() => {});
};
```

`onUpdate()` writes the stage label, the percentage, a `scaleX()` transform on
the bar (transform, not width — it's GPU-composited and doesn't trigger layout),
the stage pills, and the live log.

Two details that show care:

**The log auto-scrolls only if you were already at the bottom.**

```js
const stuck = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
el.textContent = s.log.join("\n");
if (stuck) el.scrollTop = el.scrollHeight;
```

Scroll up to read something and it stops yanking you back down.

**`finish()` is idempotent.** The stream's last message and the error-path
fallback read can both arrive, so a `finished` guard makes sure the completion
path runs once.

**Existing clips stay on screen while a new run works** — the results grid isn't
cleared on submit.

**A failed run offers to go again.** `finish()` calls `offerRetry()`, which
reads the snapshot's `failed` and `source_on_disk`: a run that ended in error
shows **Try again**, and a finished run with clips missing shows **Retry failed
clips**. Either one posts to `/api/jobs/{id}/retry` and hands the same id to
`follow()` — the function every new run goes through too — so a retried job
streams exactly like a fresh one. See
[§11.5b](#115b-trying-again--by-itself-then-on-request).

### 12.6 Searching the library

A library built up over weeks needs a way in that isn't scrolling. The search box
matches everything a clip carries — title, hook, tags, filename, and the title of
the video it was cut from — because you rarely remember which of those holds the
phrase you are searching by. Terms are ANDed, so a second word narrows.

The date chips filter by *run* rather than by clip, since every clip in a run was
rendered at once, and `Today` means the calendar day rather than the last 24
hours: a clip made last night is not one you made today.

The subtle part is indexing. Cards used to carry a counter that walked every run
in order, which assumed the grid always showed everything — filtering under that
assumption would have made cards open the wrong clip. Each clip now carries its
own position in `clips`, reassigned by `reindex()` whenever that array changes,
and arrow-key navigation walks the *visible* list so it matches the screen.

**Sort** reorders clips inside each run: *Ranked* (the order they were ranked
in), or by one part of the scorecard, *Strongest hook* and *Most energy*, or
*Shortest first*. It is remembered per machine. Each card carries the
scorecard's four bars and the model's one-line reason (§7.8), and the score
badge takes its grade's colour.

### 12.7 The player and trim editor

The player is a `<video>` with a custom control rail (Play, Sound, Trim,
Captions, Boost, Save, Download, Show file, Delete, Mini) and keyboard
shortcuts — <kbd>Space</kbd>, <kbd>M</kbd>, <kbd>T</kbd>, <kbd>C</kbd>,
<kbd>B</kbd>, <kbd>F</kbd>.

The **captions panel** appears only for a clip that was captioned and kept its
`heard_words`. It is a text box holding the captions as one line of text; **Burn
in again** sends it to `PUT …/captions` and swaps the clip in place, the same
way a trim does.

The **trim panel** is a two-handle range control built on pointer events, plus
`−1s`/`+1s` nudge buttons and text fields that accept clock format (`1:30`).
Handles drag, the fill between them updates live, and the length readout tracks.
Applying it POSTs to the trim endpoint and swaps the clip in place when the
re-cut returns.

The **mini player** persists as you scroll away, and an `IntersectionObserver`
drives the guide rail's active-section highlight.

The **SEO panel** ("Boost") opens with **why the clip ranked where it did**:
the grade, the score, the model's reason, the four parts with their numbers,
the scorecard's notes, and a line saying what the edit did (captions, pauses
cut, punch-ins, emoji, B-roll). Then comes **What's in this clip**: the kind of
video and genre from the clip's `scene`, what it was filed under and on what
evidence, and a box to type the real name — saved as `subject_override`, and
used by every rewrite after. Under the title box, `title_options` are listed
best first with their score and angle; tapping one fills the title box and
arms Save, so an option can be tweaked before it is kept. Under the tags,
every entry in `tag_options` is a chip that toggles itself in and out of the
tag box, with YouTube's 500-character count beside them — the chips and the box
are one list seen two ways, so typing in the box relights the chips. **Rewrite**
rewrites just this clip (`?only={file}`), saving a typed subject first so the
rewrite does not file the clip under the very guess it was meant to correct.
Copy buttons and copy-everything as before. The clip cards in the grid show
what each clip is filed under, so a wrong label is visible before upload.

Under **What's in this clip** sit three tabs — **YouTube Shorts**, **Instagram
Reels**, **TikTok**. Every tab's boxes stay in the page and only the open one is
shown, so one Save keeps all three apps' wording. The YouTube tab adds
`description_options` as a ranked list and a score on each tag chip. The Reels
tab shows the caption with its 2200 count and the 125-character fold called out,
ranked `caption_options`, hashtags, cover text and alt text; the TikTok tab its
caption and hashtags. **Copy everything** copies for whichever app's tab is
open, since that is the one being posted to. The chosen tab is remembered across
clips — someone posting a batch to Instagram posts all of it there.

On the Create page, a **Kind of video** row sits under Shape and behaves like it:
*Work it out* leaves it to the words and then to the ranker; any other pick is
sent as `content_kind` and redraws the preview, so choosing Vlog shows the
face-following crop before anything renders.

### 12.8 Settings drawer

Reads `/api/settings` and `/api/usage` to render:

- Provider switcher (Gemini / OpenAI) — offering a switch rather than asking for
  a key that's already on disk
- Model picker, with each Gemini model's **free-tier daily allowance shown next
  to it**. The allowance differs enormously (20/day on one, 1000 on another), and
  picking wrong is the difference between a working afternoon and a paid API. The
  choice is made with the number in view.
- **Your logo** — upload (PNG or JPEG), a thumbnail, remove, and which corner
- **B-roll** — the Pexels key, with a button to the page that hands one out
- **Budget meters** — requests used today vs the daily cap, and time until reset
- Save-location picker and the disk cleanup scanner

The Processor choice is deliberately *not* here. It decides how fast the render
about to be started will be, so it lives with the render: under the live
preview on the Create page ([§7.5](#75-which-processor--accelpy)).

If a setting is pinned by an environment variable, the API reports
`provider_pinned` / `model_pinned` and the UI says so — the difference between a
switch that looks broken and one that explains itself.

### 12.9 Small things that make it feel finished

- **`esc()` on every interpolation** — all user and model text is HTML-escaped
  before it reaches `innerHTML`.
- **A toast** for transient feedback, a **confirm dialog** for destructive
  actions ("The file is removed from your PC. This cannot be undone.").
- **A top loading bar** mirrors job progress even when the progress panel is
  scrolled out of view.
- **Drag-and-drop** anywhere on the drop zone, plus a file picker, plus URL
  paste, plus the channel grid — four ways in.
- **Usage refreshes after every run** — including failed ones, which is exactly
  when knowing what's left matters most.

---

## 13. Configuration and precedence

Three layers, and the order between them is deliberate:

```
1. Real environment variables         ← always wins
2. .env files                         ← loaded with override=False
3. %APPDATA%\ClipMint\settings.json
```

**Why env wins.** Someone with a working `.env` setup keeps behaving exactly as
before when the settings UI is added. Existing setups don't break.

**Why `.env` is searched in three places** (`config.py::_env_file_candidates`):
a bare `load_dotenv()` searches the *current directory*, and the packaged build
`chdir`s to the user's video folder before any of this is imported. Someone who
put their key in the repo's `.env` and then ran the `.exe` got told the key "is
not set" — true only of the directory the app happened to be standing in. So it
also looks beside the executable and in the config directory.

That last one asks `user_config.config_dir()` rather than reading `%APPDATA%`
itself. It used to read the variable, which exists only on Windows, so the
third place quietly became two on a Mac — a second copy of "where does config
live" that knew about one platform.

**Why `user_config.load()` records *why* it failed.** This file holds the user's
API key. Reading it as `{}` because of a bad byte or a locked handle turns a
fixable problem into *"GEMINI_API_KEY is not set"* twenty minutes into a run,
pointing the user at a `.env` that was never involved. So the reason is
remembered, and `_where_we_looked()` puts it in the error that finally reaches
them:

> *A GEMINI_API_KEY saved in C:\…\settings.json was NOT used, because that file
> is not valid JSON (…). Fix or delete that file, or set GEMINI_API_KEY in the
> environment.*

A **missing** file is ordinary — a first run has none — and stays silent. A file
we can *see* but cannot *use* is a bug and says so, once per distinct reason.

**Why `save()` can refuse.** If a config file exists but won't parse, merging
onto a silently-empty dict would drop the API key it holds. It raises instead of
destroying the thing it was asked to update. It also `chmod 600`s the file where
that's meaningful.

**Live re-reading.** `current_provider()`, `current_model()` and
`current_processor()` go through `user_config` on every call, so switching
provider takes effect on the next request and switching processor on the next
clip — even partway through a paused run — without a restart.

### Where things land

| | Path |
|---|---|
| Settings + usage ledger | `%APPDATA%\ClipMint\` (`~/.config` Linux, `~/Library/Application Support` macOS) |
| Growth playbook: your own / fetched | `playbook.md` / `playbook-cache.md`, in that same folder |
| Your logo | `brand/logo.png` (or `.jpg`), in that same folder |
| Stock footage fetched for B-roll | `<OUTPUT_ROOT>/output/b-roll/` |
| Source videos, `.srt`, `.highlights.json` | `<OUTPUT_ROOT>/output/` |
| Rendered clips + `job.json` | `<OUTPUT_ROOT>/shorts/<job-id>/` |
| `OUTPUT_ROOT` default | cwd — which is `~/Videos/ClipMint` in the packaged build, `~/Movies/ClipMint` on a Mac |

`set_output_root()` **proves it can write** to a new location — creates the
folder, writes a probe file, deletes it — before saving the setting. A save
location that turns out to be read-only fails at the moment you choose it, not
twenty minutes into a render.

---

## 14. Quota accounting and provider fallback

`usage.py` exists because of one specific failure: Gemini's free tier caps
requests **per day**, and the only way the app learned it had hit the cap was a
429 three chunks into a run that had already paid for a download and a
transcription.

### The ledger

`usage.json` sits next to `settings.json` and tracks requests **per provider per
model**.

Per-model matters: Google's daily cap is per project *per model*, so a model that
has never been called still has its full allowance — which is exactly why
switching models is a way out of a spent quota. Marking the *provider* dead would
lock out models with a far bigger allowance that were never touched.

Known free-tier limits are hardcoded (`gemini-2.5-flash-lite` 1000/day,
`gemini-2.5-flash` 250, `gemini-2.5-pro` 100, `gemini-3.6-flash` 20) and used
only to **annotate** the model list. The *list* of available models is asked of
the API, never hardcoded — a hand-written list here already went stale and
offered a withdrawn model that 404'd mid-run.

Yesterday's tally isn't an error, it's spent history: a ledger whose `day`
doesn't match today reads as blank.

### The count is advisory; the 429 is truth

```python
def is_exhausted(provider, model):
    """Deliberately ignores the local count."""
```

The count assumes the free tier and assumes this machine is the only thing
spending the key. Refusing to call an API because a *guess* said so would strand
anyone on a paid plan after twenty requests. **Only a 429 that names the daily
quota stops a run**, via `mark_exhausted()`.

### Telling per-minute from per-day

Both arrive as a 429. The per-minute one clears in a minute and is worth sleeping
through; the per-day one doesn't clear until midnight Pacific, so sleeping on it
just wastes five minutes before failing anyway.

Google names the quota in the payload — `GenerateRequestsPerDayPerProject` —
which `_is_daily_quota()` matches on. It's the only reliable way to tell them
apart.

### The Pacific day boundary, spelled out by hand

Google's free-tier day rolls over at midnight **US Pacific** — not local, not
UTC. `zoneinfo` would need the `tzdata` package on Windows, which is one more
thing to bundle into the exe and one more way for a build to break. The US
Pacific DST rule is stable and short, so it's written out directly: second Sunday
of March to first Sunday of November, `-7` inside, `-8` outside.

### Retry and fallback — `local/llm.py`

By the time a Gemini call happens, the run has already paid for a download and a
transcription, so a blip must not sink it. **The usual rule is inverted:**

> Give up immediately only on errors retrying can never fix — bad key,
> `PERMISSION_DENIED`, `INVALID_ARGUMENT`, `404`, a spent daily allowance — and
> **retry everything else.**

**Eight** attempts. If the error carries a `retry in Xs` hint, that exact delay
is honoured (the rate limiter told us precisely how long to wait); otherwise
exponential backoff capped at 60s — 5, 10, 20, 40, then 60s repeating, about
4.2 minutes of patience in total.

It used to be five, ≈75 seconds, and that was measurably too short: a real
capacity spike outlasted it at chunk 9 of 12 and came within one attempt of
discarding an 11.9 GB download and a 29-minute transcription. **A retry budget
should be sized against how long the failure actually lasts and what losing the
in-flight work costs**, not picked as a round number.

### The fallback ladder

Retrying has a ceiling, and it is a low one: if Google is still refusing after
four minutes, more requests to Google will not help either. Somewhere else will,
because its capacity has nothing to do with Google's. So there is a ladder:

```
gemini  →  groq  →  openai
```

**Free before paid**, so failing over cannot quietly start spending money.
`_LADDER` pairs each provider with a predicate that reports whether its key is
configured, and `_next_provider()` walks it and returns `None` when nothing is
left — which is what stops `call_local_llm()` recursing forever when there is no
fallback to reach.

It fires on **two** distinct conditions, and telling them apart is the point:

| | means | retrying helps? |
|---|---|---|
| **429** with a per-day quota | the allowance is gone until midnight Pacific | no — switch at once |
| **503**, still failing after the full retry budget | the provider is busy | no — it already tried for 4 minutes |

Only the first was handled originally, which is precisely why a capacity spike
could still end a run.

**Groq** is the interesting addition. It speaks the OpenAI Chat Completions API,
so `call_groq_llm()` is the `openai` client with `base_url` pointed at
`api.groq.com` — no new dependency, no second response parser. Its free tier is
30 rpm / 1000 rpd / **8k tokens per minute**, and that last figure is the one
that binds: a ranking chunk is roughly 6–7k tokens in and out together, so Groq
runs at about one chunk a minute. Slower than Gemini, and infinitely faster than
a failed run.

The switch is process-sticky — no point asking the spent provider again on every
remaining chunk — and `reset_fallback()` clears it at the start of each new job.

Two Gemini-specific details:

- `max_output_tokens = 32768` because Gemini 3.x spends part of its output budget
  on **internal reasoning** before emitting any JSON; 8192 truncated long-chunk
  responses mid-object.
- `check_gemini_model()` probes a model with a 1-token request before saving it,
  because **ListModels is not proof of anything** — it happily returns models
  that answer *"no longer available to new users"* when actually called, and that
  404 used to surface nine chunks into a run. One tiny request at the moment of
  choosing is far cheaper than discovering it later.
- An empty response reports its `finish_reason` rather than failing as
  "invalid JSON", so you learn *why* it came back empty.

---

## 15. Caching: seven independent layers

Each guards a different expense:

| Cache | Where | Keyed by | Guards |
|---|---|---|---|
| Download | `output/source_<id>*.mp4` | video id + probed height | Re-downloading gigabytes |
| Transcript | `output/<stem>.srt` | filename + mtime | The slowest step in the pipeline |
| Chunk rankings | `output/<stem>.highlights.json` | prompt version, duration, chunk count, clip count, clip length | API quota already spent |
| Job manifest | `shorts/<id>/job.json` | — | Clips becoming invisible after a restart |
| Gemini model list | in-process, 600s | — | A network round trip on every settings load |
| Words heard per clip | `heard_words` in `job.json` | the clip's span | Listening to a clip again to fix its captions |
| B-roll footage | `output/b-roll/<query>-<id>.mp4` | Pexels video id | Downloading the same stock clip twice |

### The workflow this enables

Ranking and rendering are separate on purpose, because they fail for different
reasons and cost different amounts.

1. **First pass** — transcribe, rank, render. Slow, paid once per VOD.
2. **Read the picks.** They're plain JSON with timestamps. Nudge a start time
   back three seconds, drop the one that didn't land, retitle the good ones.
3. **Re-render from the edited list** — with **no LLM calls at all.** On a free
   tier this matters: re-ranking a long VOD burns quota, and once you've
   hand-picked five timestamps, re-ranking is pure waste.

One trick worth stealing: **transcribe from the 720p download, render from a
1440p one.** Whisper doesn't care about resolution and CPU transcription is the
bottleneck, so one session yields both cheap transcription and a sharp render.
The root-level `render_1440.py` / `render_from_saved.py` scripts are exactly
this workflow, scripted.

---

## 16. Packaging: a Windows .exe, a mac .app, a Linux binary

`build_exe.py` wraps PyInstaller. `--onedir` (default) starts faster;
`--onefile` is a single self-contained exe that unpacks itself each launch.

**Bundled:** `webapp/static`, `assets/models` (the YuNet face detector),
`assets/playbook`, `assets/fonts` (the four caption typefaces, OFL) and
`assets/emoji` (Twemoji PNGs, CC-BY 4.0), found at run time through
`bundled.asset_dir()`, and `./bin` (ffmpeg + ffprobe) when present — which
is what makes the published build need nothing installed. Hidden imports cover
everything PyInstaller's static analysis can't see:
`webview.platforms.edgechromium`, `faster_whisper`, `ctranslate2`, `cv2`,
`google.genai`, `yt_dlp`, and the uvicorn loop/protocol/lifespan modules.
`torch`, `matplotlib`, `tkinter` and `pytest` are excluded to keep size down
(about 235 MB with ffmpeg as of v1.19.0; ffmpeg is roughly 67 MB of that). The webview backend is the one hidden
import that differs per platform, and each is unavailable on the others.

**Where `./bin` comes from on a release.** Each release job downloads a static
ffmpeg for its own platform — from gyan.dev, osxexperts.net and
johnvansickle.com — which makes three small hosts the only things standing
between a pushed tag and a release. So every fetch is **bounded** (30s to
connect, ten minutes in all, and a stall under 100 KB/s for a minute counts as
a failure), **tested as a real archive** before it counts (`xz -t`, `unzip -t`,
or unpacking and finding `ffmpeg.exe`), and **retried** five times with a
growing wait. Both halves came from real failures: v1.11.0's Linux build died
in `tar` on an error page served in place of the archive — which curl's own
`--retry` never saw, because the response looked like success — and v1.11.1's
sat for forty minutes on a transfer that had stalled without failing.

**Every fetched ffmpeg has to burn a caption.** Captions need ffmpeg built with
libass, and a build without it fails quietly: the edit keeps each plain clip
and says so in a log nobody reads. So each job runs
`.github/scripts/check_captions.py` against the ffmpeg it just fetched,
burning a real ASS caption with a bundled font, from a working folder, the way
`autoedit.py` does. It fails the release on Windows and Linux and warns on the
Mac beta. v1.19.0 was the first release to run it, and all three passed.

**Cutting a release from the Actions tab.** Pushing a `v*` tag is still the
whole release. A run started by hand from `main`, with a tag that doesn't exist
yet, now creates that tag on the commit it was started from, before building.
It only does that from the default branch, and leaves an existing tag alone.

### What changes on a Mac

PyInstaller cannot cross-compile, so the mac build is made on a `macos-latest`
runner by the release workflow. `build_exe.py` takes a different branch there,
for four reasons that are all macOS insisting on something Windows never asked
about:

| | Windows | macOS |
|---|---|---|
| Shape | `.exe`, or a folder | `.app` bundle; `--onefile` is refused |
| Webview backend | Edge WebView2, via `winforms` + `clr` | Cocoa WebKit, via `pyobjc` |
| Icon | `assets/icon.ico` | `.icns`, rendered from `icon.png` by `sips` + `iconutil` |
| Version metadata | `version_info.txt`, compiled in | `CFBundleShortVersionString`, written into `Info.plist` after the build |

Two more things happen after PyInstaller finishes. `Info.plist` gets
`NSAllowsLocalNetworking`, because the window loads `http://127.0.0.1` and App
Transport Security blocks that by default — with no error, just a blank window
over a server working perfectly. pywebview patches the same setting into the
bundle's info dictionary at runtime, so this is a second lock on one door
rather than the only one. And the bundle is signed ad-hoc, along with
the bundled ffmpeg individually: `codesign --deep` signs nested *code*, and
ffmpeg went in as a resource, so it is skipped. On Apple Silicon an unsigned
Mach-O is not distrusted, it is killed on exec — the app would have launched
and then failed on the first clip.

Ad-hoc means signed by nobody. Gatekeeper still refuses the first launch, and
on macOS 15 the old right-click → Open shortcut is gone, so the user has to go
to System Settings → Privacy & Security → Open Anyway. Notarising is what
removes that, and it costs $99 a year.

The published zip is made with `ditto`, not `zip`: a `.app` is full of symlinks
and carries a signature that plain `zip` mangles, and a mangled signature is an
app that will not open. The same applies one step earlier, to the copy into the
staging folder — `cp` there would be the identical mistake — so that is
`ditto` as well, and the signature is re-verified afterwards rather than assumed.

The archive holds a folder rather than the bare bundle, so `docs/install/macos.txt`
can ride along as `READ ME FIRST.txt`. Nobody reads a file on a releases page;
they read the one next to what they just unzipped — and this is a build whose
first launch is *supposed* to be refused, which is worth knowing beforehand
rather than discovering as a dialog saying the app is damaged.

Linux gets the same text as a second asset instead, `docs/install/linux.txt`
published as `ClipMint-linux-README.txt`. The binary there cannot carry
anything: the updater downloads that asset and swaps it into place, so it has
to stay exactly a binary.

### What changes on Linux

Built by the release workflow too — not because it must be, since WSL would do
it, but because what ships should be built by the thing that ships it. The
branch in `build_exe.py` is short, and most of it is things left out:

| | Windows | Linux |
|---|---|---|
| Shape | `.exe`, or a folder | `ClipMint`, no extension, either shape |
| Webview backend | Edge WebView2, via `winforms` + `clr` | none — it opens the user's browser |
| Icon | `assets/icon.ico` | none; a Linux app's icon lives in a `.desktop` file |
| Version metadata | `version_info.txt`, compiled in | none; there is nowhere to put one |
| CUDA | bundled by `--onedir` | never |

**No webview backend**, because the one that exists cannot travel. pywebview
reaches WebKit2GTK through PyGObject, whose typelibs and some two hundred
shared libraries would all have to come along — and even bundled they are half
of it, because WebKit renders pages in a separate `WebKitWebProcess` that it
locates by a path compiled into the library when the distribution built it. Get
that wrong and the app opens a window that stays blank, which is worse than
opening none: `desktop.py` already falls back to the browser, and a browser is
a working app. Running from source is untouched by any of this — there
pywebview finds the system's own `python3-gi` and opens a real window.

**No CUDA**, for a reason that has nothing to do with the hardware. The pip
CUDA wheels put their `.so` files under `site-packages/nvidia/*/lib`, and the
dynamic linker reads `LD_LIBRARY_PATH` once, at exec. Windows can call
`add_dll_directory` from inside the already-running process, which is exactly
what `transcriber.py` does; Linux has no equivalent, so bundling them would add
two gigabytes CTranslate2 could never load.

**libGL is the one thing not in the box.** OpenCV links against
`libGL.so.1`, and PyInstaller deliberately does not bundle graphics libraries —
correctly, since the right one belongs to the host's driver stack. Any desktop
has it. A headless machine does not, and the failure surfaces at the first face
detection rather than at startup, so the smoke test below cannot catch it. The
build container installs it too, because PyInstaller learns what to collect by
importing the package and `import cv2` without libGL raises rather than
degrading.

**Nothing here has been run on a desktop distribution.** It is built and
exercised under WSL, which is a real kernel and a real userland but not a
real desktop — no window manager, no notification daemon, no file manager to
reveal a clip in. What CI proves is below; what nobody has done is sit in
front of Fedora and make a Short with it.

**No version resource**, so the workflow cannot check the built artefact the
way it checks the exe's `FileVersion` or the bundle's `Info.plist`. It does
something better instead: it runs the thing. The Linux job starts the real
binary, waits for it to report a port, fetches `/`, and checks that what comes
back is the interface. That covers unpacking, every import, the port bind and
the routes — the failures that happen at a user's first launch and nowhere
earlier. Neither of the other two jobs can make that check, because on Windows
and macOS the app is a window rather than something a runner can talk to.

**The glibc floor is chosen rather than inherited.** PyInstaller does not
bundle libc; it links against the build machine's, and glibc only promises to
work forwards. So the job builds inside an `ubuntu:22.04` container instead of
on whatever the runner image has become — which also means GitHub retiring a
runner image cannot quietly move the floor out from under everyone below it.

Measuring that floor takes one more trick. `objdump` on the built binary
reports `GLIBC_2.14` and means nothing by it: a one-file build is a bootloader
with a compressed archive stapled on, and every library carrying a real
requirement is inside the archive. Built on Ubuntu 26.04 the bootloader still
said 2.14 while the payload wanted 2.43 — so the obvious check does not merely
under-report, it passes builds that cannot start anywhere they claim to run.
The payload does exist unpacked, under `/tmp/_MEI…`, for exactly as long as the
app is running. So the floor is read from there during the smoke test above,
and a build wanting more than 2.35 is rejected.


`desktop.py` solves four problems specific to being a windowed app.

**1. No stdout.** Launched from Explorer there's no console, so PyInstaller
leaves `sys.stdout`/`sys.stderr` as `None`. `print()` tolerates that; libraries
reaching for a stream do not — uvicorn's log config calls `sys.stdout.isatty()`
and reports the resulting `AttributeError` as *"Unable to configure formatter
'default'"*, killing the engine thread before it could bind a port. The symptom
was an icon that spun and then did nothing.

`_attach_streams()` points both at `app.log`. **Runs with redirected output
already have real streams, which is exactly why this never showed up in
testing** — so packaged-app bugs must be reproduced *unredirected*.

**2. Nowhere to write.** A packaged app must not write clips next to the `.exe`
(often Program Files, often read-only; on a Mac, *inside* the `.app`, where the
next download would take them with it), so a frozen build `chdir`s to
`~/Videos/ClipMint` — `~/Movies/ClipMint` on macOS, which has no
Videos folder — and creates its subfolders up front.

**3. Double-clicking.** A slow start looks exactly like a dead one, so the
natural reaction is to click the icon again — and four copies each unpacking
themselves and loading the same models is how a slow start becomes a *failed*
one.

```python
LOCK_PORT = 50507
s.bind(("127.0.0.1", LOCK_PORT))   # SO_REUSEADDR deliberately NOT set
```

Binding a fixed port and holding it for the process lifetime is the mutex.
Windows refuses the second bind, and that refusal *is* the lock.

**4. Cold-start time.** A first launch pays for importing faster-whisper,
ctranslate2, cv2 and the Google client off disk while the virus scanner reads
them too. `STARTUP_TIMEOUT_SECONDS` is 300 — but `_wait_until_up()` also watches
the engine thread, so a genuine failure reports in seconds rather than spending
the full timeout on a corpse.

Anything worth printing on the way out also gets a `MessageBoxW`, since stderr
goes nowhere a user will ever look. And the engine thread stashes its exception
in `_engine_error` so `main()` can say *why* rather than reporting a bare
timeout — a daemon thread's traceback goes to a stderr the windowed build
doesn't have.

If pywebview is missing or the native window fails, it falls back to the default
browser rather than failing: a working browser window beats no app at all.

**Distribution note:** the `.exe` ships via GitHub Release. Pushing source
changes nothing for people downloading the app, and the onedir and onefile builds
are separate artifacts — one build run refreshes only one of them.

---

## 15b. Two themes out of one set of rules

`style.css` defines colour once, as tokens on `:root`, and
`:root[data-theme="light"]` redefines the tokens rather than restating any
rule. That is the whole mechanism, and the reason for it is maintenance: a
colour added to a rule later works in both themes or in neither. It cannot
work in only one by accident, which is the failure mode of a second stylesheet
that has to be kept in step by hand.

Getting there meant pulling out ~180 hardcoded colours first. Three patterns
were worth naming:

**Faint overlays became one token.** Twenty rules used
`rgba(255, 255, 255, a)` for hairlines, hovers and glass. They are now
`rgba(var(--fg-rgb), a)`, and the light theme sets `--fg-rgb: 0, 0, 0`. All
twenty flip on one line, and the alphas — which are what the dark theme was
actually tuned with — never move.

**Some things must not flip.** The chrome drawn *on* a video — the duration
pill, the rank badge, the flags — sits on black in both themes, because a
video frame is black whatever the page around it is doing. Left as
`var(--text)` it would have gone black-on-black in the light theme. Those have
their own `--on-media*` tokens, spelled out in `:root` and deliberately not
redefined below, so they cannot be flipped by accident.

**Grey does not travel.** `#8b8b8b` for the quietest text looked right beside
the other light tokens and measured 3.4:1 against white — under the 4.5 floor
for body text. The same grey is easier to read on black than on white, so
`--text-3` is darker in the light theme than the dark theme's is light.

### No flash of the wrong theme

The theme is applied by a snippet in `<head>`, not by `app.js`, which loads at
the end of `<body>`. Six lines early beats every launch opening dark and
turning white a moment later.

The choice lives in `localStorage`, not `settings.json`, because it belongs to
the screen you are looking at rather than to the person: someone running this
on a laptop and a bright desktop wants two answers, and a synced setting would
give them one. A machine that has never chosen follows
`prefers-color-scheme` and keeps following it, until the button is pressed
once and the stored choice starts winning.

### Checking it, rather than looking at it

Both themes were checked with a contrast audit run in the page: walk every
text node, compute the effective background by climbing until something is
opaque, and flag anything under the WCAG floor for its size. The light theme
comes back clean on the create, clips, help and settings views. The dark theme
reports fifteen it has always reported — `--text-3` at 3.6:1 and white on the
brand red at 3.96:1 — which the theme work did not touch and which are worth
their own change.

One trap worth writing down: measure *after* the transition, not during it.
Colours are transitioned, and `getComputedStyle` mid-transition returns the
in-between value — which produced a first audit full of light-theme text on
dark-theme backgrounds, all of it fiction.

## 16a. Subprocesses: windows, and stopping them

Everything external this app runs — ffmpeg, ffprobe, and yt-dlp's own ffmpeg
calls — goes through `shorts_generator/proc.py`. Three unrelated problems share
that chokepoint, which is the reason it exists.

### Why a windowed build flashes black boxes

A GUI process has no console. When it starts a console program, Windows makes
one for it. ffmpeg is a console program and a render runs dozens, so a long
video means black windows blinking open and shut for minutes on end.

That is cosmetic in the sense that nothing malfunctions, and not cosmetic at
all in the sense that matters: an unsigned exe spawning unexplained console
windows is indistinguishable, to a normal user, from something malicious. The
fix is `CREATE_NO_WINDOW` plus a hidden `STARTUPINFO`.

Our own call sites are the easy half. yt-dlp spawns ffmpeg itself, from inside
a library, with no parameter to pass creationflags through — and a single
download runs several. Rather than fork it, `silence_console_windows()` changes
`subprocess.Popen`'s default at startup. Patching a stdlib constructor deserves
suspicion, so it is kept narrow: Windows only, and only when `stdout.isatty()`
is false, meaning there is no console to inherit. Run from a terminal, nothing
is patched and output behaves normally.

### Why a failed render used to say nothing

`check=True` raises `CalledProcessError`, whose message is the command and an
exit code. Both halves are less useful than they look.

The code is not an exit code in the ordinary sense. ffmpeg exits with its own
`AVERROR`: a negative value, which Windows hands back as unsigned 32-bit. A
real bug report read *returned non-zero exit status 3752568763*, and that
number is four bytes of ASCII — `-MKTAG('E','X','T',' ')`, `AVERROR_EXTERNAL`,
"a library ffmpeg calls has failed", which during an encode means libx264.
Reading it requires knowing to negate it first. `explain_exit_status()` does
that arithmetic instead, and covers the `AVERROR(errno)` codes too, so
`4294967268` prints as *No space left on device*.

The reason itself had already been printed. `-loglevel error` means ffmpeg
says one useful thing on the way out — to stderr, which the windowed build
throws away for the reason in **1. No stdout** above: there is no console for
the child to inherit, so it writes to a handle pointing nowhere. Every render
failure then looked identical from outside, which is no help to the person
reporting one and none at all to whoever reads the report.

`run_checked()` captures stderr and puts its tail in the exception. The volume
is bounded — `-loglevel error` prints a handful of lines, and `communicate()`
drains the pipe — so capturing cannot fill a buffer and stall a long encode.
Every ffmpeg and ffprobe call that has to succeed goes through it. The one that
does not is the best-effort height probe in the downloader, which already
answers 0 and carries on.

The lesson is older than this fix. The downloader learned it first, when
yt-dlp's `quiet` swallowed the ffmpeg stderr explaining why a merge failed and
left a bug report saying only that something had. Suppressing a child's output
is cheap to write and expensive exactly once: the first time something fails on
a machine you cannot reach.

### Pausing work that is already running

A cooperative pause — finish the current clip, then stop — is no use to the
person this feature is for, whose machine is unusable *now*. ffmpeg holds every
core it can get for the length of a clip.

So pausing suspends the process. Windows has no `SIGSTOP`; the equivalent is
`NtSuspendProcess` in ntdll, undocumented but stable since NT and what every
process explorer uses. POSIX gets `SIGSTOP` and `SIGCONT`.

`proc.py` keeps a registry of the children it started so it knows what to
suspend, and a `threading.Event` gate that `run()` waits on before starting
anything new. Both halves are needed: suspending only the current process would
let the remaining clips start, and gating only the next one would leave the
current ffmpeg running.

Two edges worth naming. A child started in the gap between the gate check and
registration is suspended immediately on registration, so a pause cannot be
raced. And a job that *ends* while paused clears the gate on its way out —
otherwise the next run would start and block on a pause with no UI to lift it,
because the job it belonged to is gone.

Two stages run inside the app rather than as a child, so there is no process to
suspend: transcription and the face-follow detection pass. Both now hold at a
checkpoint instead — `proc.wait_if_paused()` between Whisper segments (the
generator is lazy, so blocking the loop stops the model decoding the next
window, on the CPU or the GPU alike) and between face samples. Measured: a
paused ffmpeg cut's output stayed at exactly the same byte count for the whole
pause, the face pass made no detections during it, and an 8-second pause
added 7.6s to a CPU transcription. The limit that remains is granularity:
Whisper stops at the end of the window it is decoding, a second or two later.

---

## 16b. Shipping updates to an installed .exe

Packaging solves getting the app onto a machine once. It does nothing about the
second time. The exe is a single unsigned file someone downloads and keeps, so
without a way to reach it, every fix written after their download is a fix they
never receive — and asking someone to re-fetch 229 MB by hand is a step most
will not take twice.

`webapp/updater.py` is the whole mechanism. Three problems, each with a
constraint worth understanding.

### Knowing there is something new

The check reads GitHub's releases API for the repository compiled into the
build, compares the newest tag against `APP_VERSION`, and reports one of four
states: `current`, `update`, `ahead`, or `unavailable`.

`ahead` exists because the first version of this collapsed "a newer release
exists" and "this build is newer than the last release" into one branch, and
produced a message telling someone on the newest build to install an older one.
A development build is a normal thing to be running; it is not an update.

Checks run at launch, every thirty minutes while the window is open, and on
window focus, with a ten-minute floor so a window being focused repeatedly does
not become a stream of requests. Unauthenticated GitHub allows sixty calls an
hour; this uses two.

Failure is silent by design. Offline, rate-limited, or no releases yet all
return a quiet "nothing to report" rather than an error, because an app that
cannot reach GitHub is still a working app.

### Trusting what comes back

The releases API publishes a SHA-256 for each asset. The download is hashed as
it streams and compared before anything is replaced; a mismatch or a missing
digest fails closed and deletes the partial file.

This is integrity, not authorship — the exe is unsigned, so it proves the file
matches what the API described, not who built it. Without a code-signing
certificate that is the strongest available check.

Two smaller boundaries matter as much. Every URL is checked against an
allowlist of GitHub's own hosts, before the request and again after redirects,
so a lookalike domain in an API response goes nowhere. And the page never
handles a URL at all: it asks the server to install *the* update, and the
server resolves what that means itself. Anything rendered in the window is
therefore unable to aim the updater at a file of its choosing.

### Replacing a file that is currently running

Windows will not let a running `.exe` be overwritten. It will let it be
**renamed**. So:

```
download  -> .update-xxxx.part   (beside the exe, not in %TEMP%:
                                  a rename only works within one volume)
verify    -> SHA-256 must match, or stop here
rename    -> ClipMint.exe  -> ClipMint.exe.old-version
move      -> .update-xxxx.part   -> ClipMint.exe
relaunch  -> detached, from the same path
exit      -> 1.5s later, so the reply reaches the browser first
```

Ordering is the safety property. Nothing is touched until the hash matches, and
if the second move fails the original is put straight back — the failure mode
is "you are still on the version you had", never "you have no app".

Cleanup is the part that looks trivial and is not. The replaced build cannot
simply be deleted by the new process: the old one is still shutting down and
still holding its own file open, so the delete fails. The first version of this
swallowed that error and left 229 MB on disk until the app happened to be
started a second time. It now retries on a background thread until the handover
completes, and sweeps abandoned `.part` files at the same time.

The staged file is also deleted explicitly after the move rather than trusting
the move to consume it. `os.replace` is documented to rename, and was observed
on one volume to satisfy the request by copying and leaving the source — which
stranded a full-size duplicate next to the exe it had just become.

### What cannot self-update

Only the one-file build. The one-folder build is hundreds of files, and
swapping those under a running process is a different and far more fragile
problem, so it reports the new version and links to the releases page instead.
The two are told apart by where `sys._MEIPASS` points: a one-file build unpacks
to a temp directory far from the exe, a one-folder build unpacks nowhere and
`_MEIPASS` is the `_internal` folder beside it.

The mac build is in the same position for a sharper reason: a `.app` is signed
as a single unit, so replacing its contents piecemeal leaves a signature that
no longer matches them, and the app macOS then refuses to open is the one the
update was supposed to deliver. It also looks for a different release asset —
`ClipMint-macOS-arm64.zip` rather than `ClipMint.exe` — since
looking for the exe would report every release as having nothing in it, and the
app would go quiet about updates rather than obviously break.

And nothing can update a build that shipped before this code existed. Every
release up to v1.4.0 has no updater in it and never will — those installs need
one manual download to reach v1.5.0, after which they are self-maintaining.

### Where the version lives

`shorts_generator/version.py` holds `APP_VERSION`, and `build_exe.py` refuses
to build when `version_info.txt` disagrees with it. The release workflow checks
the git tag against it too, before building and again after.

Three guards for one number is not excessive here. The updater compares the
newest release tag against the version compiled into the running build, so a
build that reports the wrong number offers every user an update to the version
they already have, installs it, reports the wrong number again, and offers it
once more.

---

## 16c. Telling somebody a run has finished

`webapp/notify.py`, called from one place: the `finally` in
`JobManager._run_forever` that already clears the pause gate. Every run ends
there — the ones that succeeded, the ones that raised, the ones that rendered
nothing — so there is exactly one place that has to be right.

### Why this exists

A three-hour VOD is tens of minutes of transcribing, ranking and rendering.
Nobody watches that. The window goes behind a game, or gets minimised, or the
browser tab it opened ends up three deep — and the clips sit there finished
with nothing to say so. The run that ended at 2am was found at nine.

### Three platforms, no new dependency

| | Mechanism | Arrives as |
|---|---|---|
| Windows | a toast, through PowerShell's WinRT bindings | Action Center, filed under Windows PowerShell |
| macOS | `osascript -e 'display notification …'` | Notification Centre |
| Linux | `notify-send`, or `kdialog --passivepopup` | whatever the desktop uses — on some, nothing |

The Windows toast borrows PowerShell's own AppUserModelID. A toast needs an ID
the shell already knows about, and an unsigned exe run from wherever it was
downloaded to, with no Start Menu shortcut, has none to offer. The cost is the
wrong name on the notification. The alternative was no notification.

Every one of these is allowed to fail, and on Linux one regularly will: there
is no notifier that is always installed, and a headless machine has nowhere to
show one anyway. A notification that does not arrive is a disappointment; a
render that died because a notification did not arrive would be a bug. So
nothing in the module raises, the work happens on a daemon thread of its own so
a cold PowerShell start cannot hold up the queue, and a failure is one line in
the log naming the reason.

The subprocess goes out with `proc.hidden_kwargs()`. A windowed build that
flashes a black console every time a render finishes looks broken in exactly
the way an unsigned exe can least afford — see 16a.

### Knowing whether anybody is looking

The harder half. The server cannot see its own window, and the two states it
has to tell apart are indistinguishable from inside a process: somebody
watching a render finish, and somebody who left an hour ago.

So the page says. `POST /api/attention` carries one boolean —
`!document.hidden && document.hasFocus()` — sent whenever that changes, and
again every twenty seconds whether it changed or not.

The heartbeat is the whole design. A flag would be wrong for the case that
matters most: a window that was closed, or a tab shut mid-render, never gets to
send `false`. It leaves a `true` behind and the app goes silent forever, at
precisely the moment it should speak. Silence is the only signal such a page
leaves, and only a heartbeat has any. So `someone_is_watching()` insists on
both halves — a last report of `true`, **and** one that arrived inside
forty-five seconds.

It is the same mechanism as the SSE `: ping` in section 14, pointed the other
way. There a heartbeat proves a connection is still alive; here its absence
proves a window is not.

## 16d. Posting a clip to YouTube

`webapp/youtube_upload.py`, behind the routes under `/api/youtube` and
`POST …/clips/{file}/youtube`. The setup a user or maintainer does is in
[docs/youtube-upload.md](docs/youtube-upload.md); this section is how the code
works.

### The site Google links to

Google's consent screen needs a home page and a privacy policy on a domain it
can list as authorized, and github.com isn't one. So ClipMint has a small
GitHub Pages site at <https://klez799.github.io/clipmint/>:

- `site/index.html` is the home page, written by hand.
- `privacy.html` is rendered from `PRIVACY.md` by `site/build.py` on every
  deploy, so there is one copy of the policy's words and the site can't drift
  from the repo.
- `.github/workflows/pages.yml` builds and deploys it whenever `site/`,
  `PRIVACY.md` or the images it uses change.

The in-app privacy link (`EXTERNAL_LINKS["privacy"]`) points at the same page as
the consent screen, because Google expects the two to match.

### Why the API, and not a scripted browser

The obvious shortcut is to drive YouTube Studio's upload page with Playwright:
no Google Cloud project, no audit. It was turned down for three reasons:

- YouTube's terms forbid automated access.
- Studio's markup changes without notice, so the upload would break silently.
- A flagged channel is a far worse outcome than a setup step.

The Data API is the route YouTube built for this. Its one real cost is that an
unaudited project's uploads are locked private, and the code is built around
saying so rather than hiding it.

### Signing in: OAuth for an installed app

`POST /api/youtube/connect` builds Google's consent URL and opens it with
`webbrowser`, in the user's real browser, never the app window: Google refuses
sign-ins in embedded webviews. The redirect URI is this same local server,
`http://127.0.0.1:{port}/api/youtube/callback`. Google allows any loopback port
for a Desktop app client, so it survives the app picking a free port each
launch.

Two values guard the round trip:

- **`state`**, random and remembered in `_pending`, so a callback the app never
  asked for finds nothing and is refused.
- **PKCE**: a random `code_verifier` stays in memory, and only its SHA-256
  (`code_challenge`) goes to Google. The code that comes back to the loopback
  address is useless without the verifier, so another program on the machine
  that catches the redirect gets nothing.

`access_type=offline` with `prompt=consent` makes Google return a refresh token
every time, not only on the first consent. The token goes to
`youtube_token.json` in the config folder, written through a temp file and
`os.replace`, owner-only where the OS supports it. The page follows the sign-in
by polling `GET /api/youtube` every two seconds until `connect_state` leaves
`waiting`, because it finishes in a different window.

Google's consent screen lets people untick individual permissions, so the
callback checks the granted `scope` string. A sign-in without `youtube.upload`
is refused with an explanation instead of failing at the first upload.

### Which client

`client()` resolves in order:

1. the user's own client, from `YOUTUBE_CLIENT_ID`/`_SECRET` (environment, then
   `settings.json`)
2. the bundled `webapp/youtube_client.json`

The bundled file is **never in git**. YouTube's developer policies (III.D.1)
forbid credentials in open-source projects, so `build_exe.py` writes it from the
`CLIPMINT_YOUTUBE_CLIENT` secret the release workflow passes in. It validates
the JSON first and stops the build over a Web application client, which would
otherwise fail much later at the redirect. A sign-in remembers the client that
issued it; switching clients disconnects, since the old refresh token is useless
to the new one.

### Uploading: the resumable protocol

`start_upload` runs `_run` on a daemon thread and returns an id at once, the
same shape as the job queue.

1. **Initiate.** `POST` the metadata JSON to the upload endpoint with
   `uploadType=resumable`. The response's `Location` header is a session URL.
2. **Send in chunks** of 8 MiB, which must be a multiple of 256 KiB. Each `PUT`
   carries `Content-Range: bytes a-b/total`. A `308` answer carries
   `Range: bytes=0-n`, the server's word on what it has, so the next offset
   comes from that header and not from what was sent.
3. **On a 5xx or a dropped connection:** back off exponentially, then `PUT` an
   empty body with `Content-Range: bytes */total` to ask where to resume. The
   cost of a failure is one chunk.
4. **On a 401**, refresh the access token and resend, at most twice.
5. **A 200/201** carries the video resource.

The same `key` (job and file) can't be uploading twice at once: a second press
returns the running upload's id. The page polls
`GET /api/youtube/uploads/{id}` once a second, and the watcher keeps going when
the panel closes, painting only while that clip is on screen.

### One queue, and stopping it when YouTube says no

`start_upload` doesn't start a thread per upload. It appends to `_queue`, and a
single worker thread takes them in order. Several uploads at once would share
one home connection's upload bandwidth and finish no sooner, and a queue gives
every waiting clip something true to report: `upload_status` adds `ahead`, how
many are in front of it.

Some refusals will refuse everything after them too: the channel's upload
limit for the day, the project's quota, the API switched off, a channel that
doesn't exist. `UploadError` carries YouTube's `reason`, and when it's one of
`_HALTING`, `_stop_waiting` fails everything still queued with that same
message. Twenty clips then fail once, with the reason, instead of twenty times.

### Upload all

Each run's header has **Upload all**. It opens a dialog built from that run's
clips, not a background job:

- every clip with the title, description and tags it will go up with, all
  editable, and a tick to include or skip it (clips already on YouTube start
  unticked)
- one privacy, category and made-for-kids choice for the batch
- for a schedule, a first time and a gap; each ticked clip's publish time is
  computed in order and shown on its row before anything is sent

Nothing is sent until the button is pressed. That's deliberate: YouTube's API
policies want uploads to be the person's specific choice, with their say over
what is published (III.C.3, III.I.2), and a list of exactly what will go up is
how a batch stays that.

On the press, each ticked clip is handled in order. If its words changed, they
are saved first with `PUT …/seo`, because saving a title renames the file and
the upload must read the renamed one. Then `POST …/youtube` queues it. The
dialog polls every queued id and paints each row: waiting and how many are
ahead, the percentage, then the schedule or YouTube's error. Closing the
dialog doesn't stop anything; reopening it while a batch runs shows the same
batch.

### Reading the upload back

The upload's own response doesn't reliably show the private lock. So when the
person asked for anything other than private, `_run` fetches the video's
`status` once more. If a requested schedule came back without `publishAt`, or a
public or unlisted request came back private, the result is marked
`kept_private` and the page explains the audit instead of saying
"Uploaded" as if nothing happened.

### Held to YouTube's rules, not rewritten to fit them

`build_metadata` refuses anything YouTube would reject, with the reason:

- a title over 100 characters
- a description over 5,000 **bytes**
- tags over 500 characters, counted YouTube's way (a tag with a space costs two
  extra for the quotes YouTube adds, plus one per comma)
- `<` or `>` anywhere
- a schedule under 10 minutes away
- a category outside the list

It never trims or substitutes. Policy III.C.3 says user-provided values must
not be altered without consent, and a silently shortened title is exactly that.
Scheduling sets `privacyStatus: private` with `publishAt`, which is how YouTube
schedules: a private video it makes public at that time.

### Keeping what YouTube hands back no longer than allowed

Policy III.E.4 caps stored API data at 30 days:

- The channel name and id carry `channel_read_at`. `status()` stops showing
  them once they're 30 days old and refreshes them on a background thread.
- Each clip's `youtube` record (video id, time, privacy) is swept by
  `JobStore.forget_youtube(only_expired=True)` at the end of `restore()`.
- `POST /api/youtube/disconnect` revokes the token at Google, deletes it, and
  runs `forget_youtube()` over every run.

## 17. Rough edges and known limits

Everything in this section is true of the code as it stands. An earlier draft of
this document listed four defects it found while being written; those have since
been fixed, and what they were is recorded in [§17.1](#171-fixed-since-this-document-was-written)
because the reasoning is worth keeping.

### The CLI is not the app

`python main.py --mode local` always renders the **face-tracking** layout, never
the stacked one. It writes no SEO metadata, keeps no chunk checkpoints, names
its output `short_NN.mp4`, and ignores `LayoutSpec` entirely.
It doesn't caption or edit its clips either: the edit lives in
`render_highlights()`, which only the app calls.

### Captions and the edit: known limits

- **Word timings come from a second Whisper pass per clip.** That costs roughly
  the render time again on a CPU. Timing words over the whole source instead
  would be free per clip, but costly over a four-hour VOD, and useless for trims
  and exact spans.
- **Emoji words are English.** `EMOJI_WORDS` is a fixed list; on another
  language, emoji are rare rather than wrong.
- **B-roll needs the model to find something concrete**, and on most
  talking-head clips it rightly finds nothing. It is never offered on streams.
- **A cut is only as good as the loudness envelope's 0.25s windows.** A pause
  shorter than about half a second is heard only at its middle, and kept if that
  middle holds speech: a missed cut costs less than a clipped word.
- **Fixing captions changes the words, not the cuts.** They still follow what
  was heard, so typing out an "um" that was cut does not bring it back.

This is a design split rather than a bug — `pipeline.py` is the original
upstream shape, kept working — but it surprises everyone who reads the README's
feature list and then opens `main.py`. The features described everywhere outside
the Quickstart are *app* features.

### `redirect_stdout` is process-global

`_execute()` wraps the whole pipeline in `contextlib.redirect_stdout(sink)`.
Since only one job runs at a time this is correct, but anything else printing on
another thread during a job — an unrelated request handler, say — lands in that
job's log. Harmless today; the first thing to fix before adding a second worker.

### Face detection is YuNet, with Haar underneath

YuNet does the detecting now ([§7.6](#76-finding-faces--facespy)), but the Haar
fallback still depends on `cv2.CascadeClassifier`, which OpenCV 5.x dropped —
hence the `opencv-python>=4.8.0,<5` pin stays. YuNet is far better on profiles,
shadow and headsets, not perfect: a face turned fully away is still missed,
which is why the face crop holds its last position through gaps and the
stacked layout never trusts a single detection.

### The frames can name the wrong game

The vision pass is only as good as the evidence on screen. An indie game with
no HUD, no subtitles and nothing said about it cannot be named with confidence,
and the app does not pretend to: it files the clip by genre and shows the
guess in the Boost panel for a person to confirm. That is a deliberate
trade — an unnamed clip is findable by its genre; a misnamed one is called out
in its own comments.

### The playbook is advice, dated

What `short-form-algorithms.md` says about each platform is only as current as
its `reviewed:` line, and the platforms do not announce every change. It is
written from what the platforms have said publicly, not from measured results on
this channel. Nothing in the app checks it against real retention yet. The
scores that come back also sit high — a real run on two podcast clips put every
title option between 90 and 97 — so the order they give is more useful than the
numbers themselves.

### The kind of video is only as good as its samples

Detection reads three stretches of transcript and the listing. A stream that is
mostly "just chatting", or a vlog filmed while gaming, can land on the other
kind. The log says what it ranked as, and the picker fixes it for the next run.

### Clip filenames are titles, so they change

Naming a clip after its title ([§9](#9-seo-the-packaging-step)) means the file on
disk is renamed whenever the title is rewritten. That is the intent, but it has
consequences worth knowing: a clip you linked to elsewhere, or opened in an
editor, moves out from under that reference. The job manifest is rewritten in the
same step, so the app itself never loses track.

---

### 17.1 Fixed since this document was written

Kept because the failure modes are instructive.

**Several exact cuts garbled the rest of the prompt.** `cut 47:02 to 47:34,
1:31:16 to 1:31:46, … , gameplay only` with five spans rendered webcam-on-top at
4:5. Each span was removed from the prompt as it was found, using offsets
`re.finditer` had measured on the *original* string — so after the first
removal every later slice landed a few characters off, chewing through
"gameplay only" and leaving digits that the aspect-ratio pass read as `4:5`.
One span was always fine, which is why it hid. The fix is to collect the matches
and cut from the last to the first, so no removal moves an offset still to be
used. It was found by taking the README's progress screenshot, not by a test.

**Face-follow decoded the whole stream up to every clip.** `-ss` after `-i` is an
output seek, so each clip's cut decoded the source from its first frame. It
looked like face tracking was slow — the UI even warned it was ~9× slower —
until it was timed stage by stage. One argument moved: 91s → 17s on a CPU.
The lesson is the usual one: measure the stage before blaming the algorithm.

**Packaged builds crashed into CUDA.** CTranslate2 counts an NVIDIA GPU whether
or not cuBLAS is installed, and the published builds leave cuBLAS out, so every
NVIDIA user's transcription started on the GPU, died, and fell back with an
error that read like the app had broken. The libraries are now loaded before
the GPU is chosen ([§7.5](#75-which-processor--accelpy)).

**Pause did not reach transcription.** It suspended ffmpeg children and nothing
else, so the longest stage ran on through a pause. A checkpoint between
segments fixed it ([§16a](#16a-subprocesses-windows-and-stopping-them)).

**The face-tracking crop stuttered.** It detected a face on every frame and
eased 15% of the way toward each new box. That reads as smooth on paper; on
screen, Haar boxes wobble by several pixels between identical frames and a
false positive yanks the window, so the crop never stopped moving. Measured on
a real face cam: 50 direction reversals in 20 seconds. The path is now planned
offline — median filter, dead zone, zero-lag Gaussian — and reverses 4 times.
The lesson: an online filter over a noisy signal inherits the noise as motion;
when the whole signal is known in advance, smooth it as a whole.
[§7.3](#73-facetrack--the-talking-head-crop).

**The stacked layout cropped whatever face was biggest.** On a real VOD it
framed a baby's photo inside the game instead of the streamer, and wherever
the overlay was smaller than five face-widths the cam panel filled with
gameplay and letterbox bars. It now samples eight minutes around the clip,
takes the face the samples agree on, and fits the crop inside a border found
from persistent edges. [§7.2](#72-stacked--the-webcam-over-gameplay-layout).

**A variety stream's clips were all filed under one game.** The subject was
named once per run, from the listing, so a stream titled after The Finals
tagged its Firewatch clips `#thefinals`. Each clip is now looked at, and a game
is named only on evidence — the vision model called a Fears to Fathom clip
Phasmophobia at full confidence. [§9](#9-seo-the-packaging-step).

**Transcripts were cached against the working directory, not the video.** The
cache path came from `LOCAL_OUTPUT_DIR`, which is relative and so resolved
against the cwd, while the video was written under `OUTPUT_ROOT`. Identical by
default; divergent the moment anyone moved their save location — at which point
every transcript was written somewhere the lookup never checked, and each video
silently re-transcribed on every run. The cache now follows the video, still
reads the old locations so nothing is orphaned, and falls back when the video's
folder is read-only.

**Every highlight in a chunk's overlap tail was thrown away.** Each chunk carries
60s of context past its own end so a moment straddling the boundary stays
readable, but it declared its duration as `end - start` regardless. That duration
is the clamp bound sanitisation measures against, so anything the model found in
that tail was clamped to zero length and dropped — silently, because a span
clamped to `start == end` simply disappears. On a 60-minute VOD that was three of
four chunks losing their last minute. Fixed by making the declared duration cover
the tail the chunk actually carries; `PROMPT_VERSION` went to 3 so existing
checkpoints are not resumed onto.

**Clip serving used a string-prefix containment check.** `get_clip` compared
resolved paths with `startswith`, which treats a sibling folder whose name merely
begins with this one's as being inside it. It could not actually be exploited —
`os.path.basename` had already stripped any directory part — but the editor's
`_clip_path` proves containment properly with `relative_to`, and both paths now
use it.

**Two constants named `MAX_CLIP_SECONDS`.** One caps what the ranker may return
(90s), the other the longest span the trim editor will re-cut (300s). The second
is now `MAX_TRIM_SECONDS`.

**Two docs had drifted from the code.** `.env.example` still defaulted
`LLM_PROVIDER` to `openai` when the code says `gemini`, and omitted
`LOCAL_OUTPUT_RESOLUTION`. The README still described clips starting 2–5s
*before* the moment, which `COLD_OPEN_RULES` had reversed.

**Show file opened the wrong folder for most clips.** Explorer was handed
`explorer /select,<path>` — a command line it parses itself, splitting on the
comma that introduces the argument. Once clips were named after their own
titles, and titles are full of commas, everything after the first one was read
as a separate argument and Explorer fell back to Documents. It now goes through
`SHOpenFolderAndSelectItems`, which takes the path as data rather than as text
to be re-parsed. The lesson generalises: a shell-ish call that worked for
`short_01.mp4` is not proof it works for a filename a person would recognise.

**The learning loop is half-built.** Every clip's signal values and both scores
are written into `job.json`, which is the record the rule book's Part 6 needs —
but nothing reads YouTube Analytics back in, so the weights in `signals.py` are
still hand-set seeds rather than anything derived from this channel's own
retention. That is an OAuth flow and a correlation pass away.

---

## 18. HTTP API reference

All routes bind `127.0.0.1`. There is no authentication — binding `0.0.0.0`
exposes an unauthenticated service where every job spends the host's API quota
and CPU, and `webapp/__main__.py` prints a warning when you do.

### Pausing a run

| Route | Purpose |
|---|---|
| `POST /api/jobs/{id}/pause` | Suspend the processes doing the work and hold back the next one |
| `POST /api/jobs/{id}/resume` | Let them continue |

The job snapshot carries `paused`, so a reloaded page agrees with the worker
rather than guessing from what the button last did.

### Version and updates

| Route | Purpose |
|---|---|
| `GET /api/version` | The running build's version. Local only, never touches the network, so the number is there when GitHub is not |
| `GET /api/update/check` | Asks GitHub for the newest release. Returns `current` / `update` / `ahead` / `unavailable`, and never the download URL |
| `POST /api/update/install` | Starts the download. Takes no arguments — the server resolves which file to fetch for itself |
| `GET /api/update/progress` | Bytes done, total, and state: `downloading` / `verifying` / `ready` / `failed` |
| `POST /api/update/apply` | Swaps the verified build in and relaunches. The process exits ~1.5s after replying |

### Setup and settings

| Route | Purpose |
|---|---|
| `GET /api/options` | Aspect ratios, layouts, corners and caption styles for the UI controls |
| `GET /api/fonts/{name}` | One of the bundled caption fonts, so the style picker and the preview show the real type. Only the files a caption preset names are served |
| `GET /api/brand` · `GET`/`POST`/`DELETE /api/brand/logo` · `POST /api/brand/corner` | The channel's logo: whether there is one and its corner, the image itself, upload (PNG or JPEG by magic bytes, ≤ 5 MB), removal, and which corner |
| `PUT /api/jobs/{id}/clips/{file}/captions` | Re-burn a clip's captions from corrected text: retimed onto the heard words, rendered again from the same span with the same edit |
| `POST /api/settings/pexels` | Store (or, empty, remove) the Pexels key B-roll is fetched with. Kept apart from the model keys: it chooses no provider |
| `GET /api/settings` | Whether a key exists, which provider, where it came from, ffmpeg presence, available Gemini models with free-tier limits. **Never returns the key itself** |
| `POST /api/settings` | Save a key / switch provider / pick a model / set a self-imposed daily cap. Gemini models are probe-tested before storing |
| `GET /api/usage` | Today's spend per provider per model, seconds until reset, whether an OpenAI fallback is ready |
| `GET`/`POST /api/locations` | Read or change the save location |
| `GET /api/youtube-access` | The YouTube sign-in choice, every installed browser probed and ranked with a reason for any that can't be used, a discovered `cookies.txt`, the drop folder, and `pinned` when `YTDLP_COOKIES` overrides the setting (`?recheck=true` reads the browsers again). **Never returns cookie values** |
| `POST /api/youtube-access` | Set `mode` — `auto` / `browser` / `file` / `off` — with a browser or a path. The browser is probed and the file checked before anything is saved |
| `GET`/`POST /api/cleanup` | Scan for, then delete, reclaimable source files |
| `POST /api/reveal` | Show a path in the OS file manager |
| `POST /api/open-upload` | Open YouTube's upload page (URL hardcoded, not client-steerable) |

### Creating work

| Route | Purpose |
|---|---|
| `POST /api/resolve` | Classify a pasted link: single video, or a channel to pick from |
| `POST /api/upload` | Accept a dropped video file, return a path to run from |
| `POST /api/layout/preview` | Parse a layout prompt without running anything — powers the live preview. Takes `aspect_ratio` and `content_kind` picks |
| `POST /api/jobs` | Enqueue a job. Returns immediately with an id. `content_kind` (`stream`, `vlog`, `podcast`, `tutorial`, `other`) beats the prompt's words; omit it or send `auto` to leave it to them |

### Following work

| Route | Purpose |
|---|---|
| `GET /api/jobs` | All jobs, newest first |
| `GET /api/jobs/{id}` | One job snapshot |
| `GET /api/jobs/{id}/stream` | **SSE** — one event per state change, `: ping` every ~15s, closes on `done`/`error` |
| `GET /api/library` | Every clip still on disk, rescanning the folder first |
| `POST /api/attention` | The page reporting whether anyone is looking at it. A heartbeat, not a flag — silence counts as no, which is how a closed window is detected |

### Acting on clips

| Route | Purpose |
|---|---|
| `GET /api/jobs/{id}/clips/{file}` | Stream the mp4 |
| `POST …/clips/{file}/trim` | Re-cut from source at new timestamps, optionally muted |
| `POST …/clips/{file}/save` | Copy out of the working folder into the save location |
| `DELETE …/clips/{file}` | Delete the clip and its file |
| `POST /api/jobs/{id}/continue` | Carry on a run the app never finished (distinct from `/resume`, which lifts a pause) |
| `GET /api/processor` | What video encoding and transcription will run on, and why (`?recheck=true` re-tests the encoders) |
| `POST /api/processor` | Set `auto`, `gpu` or `cpu`; applies from the next clip, even on a paused run |
| `POST /api/jobs/{id}/retry` | Run a failed job again from where its caches stop it; `?clips_only=true` re-renders only a finished run's failed clips |
| `POST /api/jobs/{id}/seo` | Write or rewrite upload metadata (`?force=true` to overwrite, `?only={file}` for one clip) |
| `PUT …/clips/{file}/seo` | Save metadata the user typed — YouTube, Reels and TikTok fields — or a corrected `subject`; renames the mp4 to a new title |
| `POST /api/jobs/{id}/reveal` | Show a clip in the file manager |

### Uploading to YouTube

| Route | Purpose |
|---|---|
| `GET /api/youtube` | Which client is in use (`yours` / `builtin` / none), whether a channel is connected and its name, the state of a sign-in in progress, `pinned` when an environment variable sets the client, and the category list. **Never returns a token or secret** |
| `POST /api/youtube/client` | Save the user's Desktop app client from its JSON or a path to it; an empty `client` goes back to the bundled one. Rejects a Web application client |
| `POST /api/youtube/connect` | Open Google's consent page in the real browser, with PKCE and a loopback redirect |
| `GET /api/youtube/callback` | Where Google sends the browser back. Checks `state`, exchanges the code, checks the granted scopes, and renders a page saying what happened |
| `POST /api/youtube/disconnect` | Revoke at Google, delete the token, and forget every clip's upload record |
| `POST …/clips/{file}/youtube` | Upload one clip in the background with the `title`, `description`, `tags`, `privacy`, `publish_at`, `made_for_kids` and `category` sent. Validated first; returns the upload's status with its `id` |
| `GET /api/youtube/uploads/{id}` | Bytes sent, size, `state` (`queued` / `starting` / `uploading` / `checking` / `done` / `error`), and the result: video id, privacy, schedule and `kept_private` |
| `POST /api/youtube/open` | Open an uploaded video in Studio or on YouTube, by a validated id only |

---

## 19. Interview cheat-sheet

Short answers to the questions you'll actually be asked.

**"What does it do?"**
Turns a long video into ranked vertical Shorts with upload-ready titles,
descriptions and tags. Downloads with yt-dlp, transcribes locally with
faster-whisper, ranks moments with an LLM, looks at each winner's frames so its
title names what is on screen, renders with ffmpeg, then edits every clip:
burned-in word-by-word captions, quiet pauses cut, punch-ins on emphasis. Each
clip says why it ranked where it did. Runs on the user's machine and ships as a
Windows .exe, a mac .app and a Linux binary.

**"Walk me through the architecture."**
Four decoupled stages — download, transcribe, rank, render — plus an edit pass
over the finished clips, behind a FastAPI
backend with a single-worker job queue. HTTP requests only enqueue; progress
streams back over SSE. A vanilla-JS SPA drives it, wrapped in a pywebview native
window, packaged by PyInstaller.

**"Why a job queue instead of just doing the work in the request?"**
Transcription outlives any sensible HTTP timeout. `POST /api/jobs` enqueues and
returns an id in milliseconds; the browser follows over SSE. One worker, because
Whisper and ffmpeg are both CPU-bound and racing them makes both slower.

**"Why SSE and not WebSockets?"**
Progress is purely server→client. SSE gives that over plain HTTP with browser
auto-reconnect and no protocol upgrade. The client is one line.

**"How does the progress bar know what's happening?"**
It's derived, not guessed. The pipeline narrates itself to stdout; the runner
captures that stream line by line, maps prefixes like `[transcribe]` onto stages,
and gives each stage a band of the bar sized by how long it takes. An `N/M`
counter in a render line moves the bar to the right spot inside the render band
— with zero coupling between pipeline and UI.

**"What was the hardest bug?"**
Chunk timestamp rebasing. Videos over 30 minutes returned *zero* highlights,
with no error. Each chunk was being handed absolute timestamps but clamped
against a relative duration bound, so everything past the first 20 minutes was
silently discarded. The fix: rebase each chunk to zero, keep the offset, add it
back after ranking.

**"How did you make it work on stream footage when generic tools don't?"**
Two things. The renderer finds the webcam by what makes an overlay an overlay:
it does not move. Twenty frames from eight minutes around the clip, the face
most of them agree on (a face in the game turns up once and is outvoted — it
once won the old largest-face rule as a baby's photo), and a crop fitted inside
the border that persists across all of them. And the ranking prompt teaches the
model to separate streamer speech from game narration by register, then hard-
requires the streamer's own voice in every clip.

**"How does it know which game is in a clip?"**
It looks: four frames per clip go to a vision model before any title is
written. What matters is what it does *not* trust — the model named a Fears to
Fathom clip Phasmophobia at confidence 1.0. So the code asks for the evidence
(text on screen, the clip's words, the listing) and demotes any name without it
to a guess, which the title never prints. Model confidence is not calibrated;
evidence can be checked.

**"How do you handle LLM failures?"**
Inverted retry logic: give up immediately only on errors retrying can never fix
(bad key, 404, spent daily quota) and retry everything else eight times,
honouring the server's own `retry in Xs` hint. On a spent quota or a provider
that stays busy, switch to Groq, then OpenAI, mid-run rather than lose a
download and a transcription. Every finished chunk is checkpointed to disk, and
each pipeline stage retries itself too — so a run that still dies can be sent
round again with **Try again** and resumes where it stopped.

**"How do you keep it cheap?"**
Seven caches — download, transcript, chunk rankings, job manifest, model list,
the words heard in each clip, and B-roll footage —
each guarding a different expense. Keyword-first prompt parsing that only
consults the LLM when it understood nothing. A one-batch SEO call instead of one
per clip. An exact-span path that skips transcription and ranking entirely. And a
usage ledger so the daily budget is visible *before* it runs out.

**"How do the captions know when each word is said?"**
The clip is listened to again, on its own, with Whisper's word timestamps on:
seconds of audio rather than the whole VOD, and on the clip's own timeline, so
trims and exact spans need no mapping. The words become an ASS script for
libass, one event per word with that word recoloured, burned in by ffmpeg's
`subtitles` filter. Fixing a misheard name is sequence alignment: `difflib`
matches the typed words to the heard ones, unchanged words keep their timing,
and rewritten stretches share the time of what they replaced.

**"How does it cut pauses without cutting the good silences?"**
It listens before it cuts. A gap between words is dead air only if the clip's
own loudness envelope is under 30% of its speech level all the way through, so
a laugh or a game explosion in the gap keeps it. The three seconds before the
loudest moment are never cut, because that quiet is the build-up the ranking
rewarded. Every word, and the cold open's peak, is then moved onto the shorter
timeline by a piecewise map over the kept spans.

**"Why no frontend framework?"**
The UI ships inside a PyInstaller bundle. A build step means a toolchain in the
release pipeline. Three static files copied verbatim eliminates that, and the UI
is one page with about eight interactive regions — well inside what vanilla DOM
handles cleanly.

**"How do you know it works?"**
The two worst bugs in this codebase were both silent — they produced no error,
just fewer results — so the honest answer is that the fix isn't done until the
old behaviour has been reproduced. Both were verified by asserting the broken
output first and the corrected output second, and the library filters were
driven through real typing and clicks in the browser rather than by setting
state directly.

**"What would you do next?"**
Rank title options, and weight the scorecard's four parts, against real
retention data rather than an assumed rubric. Dub clips into the interface's
other languages.
Add a regression test suite around `_sanitize_highlights`,
`chunk_transcript` and `layout_spec` parse ordering — all three encode hard-won
ordering constraints that a refactor could silently break, and "silently" is the
recurring theme of every real bug found here. And fix the process-global
`redirect_stdout` before ever adding a second worker.

---

## Quick orientation for a new reader

Reading in this order gets you productive fastest:

1. **`webapp/jobs.py::_execute`** — the real pipeline, top to bottom, ~80 lines.
2. **`shorts_generator/highlights.py`** — the prompts and the scoring. The file
   that decides output quality.
3. **`shorts_generator/local/gaming_layout.py`** — the layout that makes stream
   footage survive a vertical crop.
4. **`shorts_generator/layout_spec.py`** — how plain English becomes a render
   spec, and the ordering constraints that keep it honest.

The most valuable knob to turn first is `CRITERIA_BY_KIND` in `highlights.py`.
Everything else is tuning; that one changes what the app considers worth clipping
at all. The second is `assets/playbook/short-form-algorithms.md`, which changes
how every clip is packaged — and needs no release to reach people.

---

## Author

**Parth Bhadana**

[YouTube](https://www.youtube.com/@ParthBhadana799) &middot; [GitHub](https://github.com/kleZ799) &middot; [LinkedIn](https://www.linkedin.com/in/parth-bhadana-530014202/) &middot; [Discord](https://discord.gg/jnMrGbBz3m) &middot; [Buy me a coffee](https://buymeacoffee.com/parthbhadana)

ClipMint is free, and stays free. If it saves you time,
[buy me a coffee](https://buymeacoffee.com/parthbhadana) — it pays for the hours that keep it working.

Built and maintained by me. If you use it, fork it, or ship anything based on
it, the MIT licence asks one thing in return: keep the copyright notice.

Repository: <https://github.com/kleZ799/clipmint>
