# The concepts behind ClipMint

By **Parth Bhadana** — [YouTube](https://www.youtube.com/@ParthBhadana799) · [GitHub](https://github.com/kleZ799) · [LinkedIn](https://www.linkedin.com/in/parth-bhadana-530014202/) · [Discord](https://discord.gg/jnMrGbBz3m)

**What this document is for.** [HOW_IT_WORKS.md](HOW_IT_WORKS.md) explains *this
codebase*. This one explains the *ideas* the codebase is made of — the AI/ML and
computer-science concepts you are actually using — so that when someone asks
"what is this and how does it work", you can answer at whatever depth they push
to.

**How to use it.** Every concept below is anchored to a real file and a real
decision in this repo. That anchoring is the point: "I used a producer–consumer
queue" is a phrase anyone can memorise, but "I used a single-worker queue
because ffmpeg and Whisper are both CPU-saturating, so a second worker would
have made both jobs slower rather than finishing either sooner" is an answer
that survives a follow-up question.

**The honest framing.** You did not invent Whisper or Gemini. What you built is
a *system* around them, and systems work is where almost every interesting
decision in this project lives — caching, failure handling, concurrency,
security boundaries, prompt design, degradation. Say that plainly. It is a
stronger position than pretending otherwise, and it is where your real
engineering is.

---

## Contents

1. [The 60-second answer](#1-the-60-second-answer)
2. [The system in one picture](#2-the-system-in-one-picture)
3. [AI/ML: speech recognition](#3-aiml-speech-recognition)
4. [AI/ML: working with LLMs](#4-aiml-working-with-llms)
5. [AI/ML: computer vision](#5-aiml-computer-vision)
5a. [AI/ML: audio signal processing and feature fusion](#5a-aiml-audio-signal-processing-and-feature-fusion)
6. [CS: concurrency and the job model](#6-cs-concurrency-and-the-job-model)
7. [CS: the web layer](#7-cs-the-web-layer)
8. [CS: algorithms actually used here](#8-cs-algorithms-actually-used-here)
9. [CS: caching and idempotency](#9-cs-caching-and-idempotency)
10. [CS: reliability and failure design](#10-cs-reliability-and-failure-design)
11. [CS: security boundaries](#11-cs-security-boundaries)
12. [CS: operating systems and packaging](#12-cs-operating-systems-and-packaging)
    - 12a. [Process control, and trust as a UX property](#12a-cs-process-control-and-trust-as-a-ux-property)
    - 12b. [Indirection, and the things it must not reach](#12b-cs-indirection-and-the-things-it-must-not-reach)
    - 12c. [Software distribution and self-update](#12c-cs-software-distribution-and-self-update)
    - 12d. [Absence as a signal](#12d-cs-absence-as-a-signal)
13. [CS: internationalisation](#13-cs-internationalisation)
14. [Questions you should expect](#14-questions-you-should-expect)
15. [What you would do next](#15-what-you-would-do-next)

---

## 1. The 60-second answer

> It takes a long video — a multi-hour stream VOD, a vlog, a podcast, a
> tutorial — and produces vertical clips packaged for YouTube Shorts, Instagram
> Reels and TikTok. Three stages: transcribe the audio with Whisper, have an LLM
> work out what kind of video it is and rank the transcript for clippable
> moments by that kind's rules, then cut and re-frame those spans with ffmpeg
> into 9:16: the webcam stacked over the gameplay for a stream, a crop that
> follows the face for a vlog or podcast.
>
> Everything runs locally except one LLM call. It ships as a desktop app for
> Windows, macOS and Linux — a FastAPI server behind a native webview window,
> or behind the browser on Linux, where that window cannot be bundled and
> carried. Packaged with PyInstaller, and it raises a desktop notification
> when a run finishes and nobody is watching it.
>
> The interesting parts aren't the models, they're everything around them:
> a 3h47m VOD does not fit in a context window, so ranking is chunked and
> checkpointed; the LLM is unreliable, so there's a retry budget and a
> degradation path; transcription is the expensive step, so there are five
> layers of caching that make every re-rank free.

If they want less, stop after the first paragraph. If they want more, the
answer to "what was hard?" is in [§14](#14-questions-you-should-expect).

---

## 2. The system in one picture

```
YouTube URL ──yt-dlp──> source.mp4 ──ffmpeg──> 16kHz mono audio
                                                     │
                                          faster-whisper (CTranslate2)
                                                     │
                                              transcript (.srt)  ←── cached
                                                     │
                                    ┌────────────────┴──────────────┐
                                    │  too long for one LLM call    │
                                    │  → chunk into 20-min windows  │
                                    │    with 60s overlap           │
                                    └────────────────┬──────────────┘
                                                     │
                                        LLM ranks each chunk  ←── checkpointed
                                                     │
                                     greedy overlap dedupe → top N
                                                     │
                         vision LLM looks at 4 frames per clip ──> what it shows
                                                     │
                              LLM writes 5 ranked titles, tags, description
                                                     │
                 OpenCV finds the overlay and the face ──> crop geometry
                                                     │
                                    ffmpeg: cut + stack + scale
                                                     │
                                            1080×1920 h264 .mp4
```

The single most important property of this diagram: **cost increases sharply
left to right in time, and the expensive stages are cached.** Downloading is
minutes, transcription is minutes-to-tens-of-minutes, ranking is seconds-to-
minutes of API time, rendering is ~30s per clip. So the caches are placed to
make the *second* run of anything nearly free.

---

## 3. AI/ML: speech recognition

**File:** `shorts_generator/local/transcriber.py`

### What Whisper is

An **encoder–decoder Transformer** trained for **automatic speech recognition
(ASR)**. The encoder consumes a log-Mel spectrogram of 30-second audio windows;
the decoder autoregressively emits text tokens. It is a **sequence-to-sequence**
model, not a classifier — which is exactly why it can hallucinate: nothing
constrains it to have heard anything.

**faster-whisper** is a reimplementation on **CTranslate2**, an inference engine
that applies operator fusion, batching and quantisation. It is not a different
model, it is the same weights executed more efficiently.

### Quantisation

The code picks `float16` on GPU and `int8` on CPU.

- **float16** — half-precision floats. GPUs have dedicated hardware for these;
  half the memory bandwidth of fp32 for near-identical accuracy.
- **int8** — 8-bit integers. Weights are mapped to a small integer range with a
  scale factor. Roughly 4x smaller than fp32 and much faster on CPUs with SIMD
  integer instructions, at a small accuracy cost.

This is **post-training quantisation**: the model was trained in higher
precision and is being *executed* in lower precision. No retraining involved.

### Beam search

`beam_size=5`. At each decoding step the decoder keeps the 5 highest-probability
partial sequences rather than committing to the single best token (**greedy
decoding**). This is a **heuristic search over the output space** — it does not
guarantee the globally optimal sequence, it just explores more of it. Cost is
roughly linear in beam width.

### Hallucination, and the bug it caused here

`condition_on_previous_text=False`. Normally Whisper feeds its previous output
back in as context, which improves coherence — and creates a feedback loop where
one hallucinated phrase repeats forward through an entire VOD. Disabling it
limits *propagation*.

It does not prevent *occurrence*, which this project learned the hard way. With
language on auto-detect, Whisper performs **language identification** and
re-decides on unclear audio. On a game stream — music beds, effects, silence —
it drifted, and then generated fluent, confident Korean. A real 3h47m English
VOD returned **703 of 1097 cues in a language nobody spoke.**

Two mitigations, both in the repo now:

1. **Pin the language** (`language="en"`). Removes the decision entirely.
2. **Use a bigger model.** `small` hallucinates less than `base`, and on GPU it
   is also *faster* than `base` — so the accuracy costs nothing.

> **Concept to name in an interview:** this is a **silent failure**. The system
> produced confident, well-formed, completely wrong output and reported success.
> Distinguish it from a crash: crashes are cheap because they are loud.

### Voice activity detection (VAD)

VAD segments audio into speech and non-speech to skip silence. It is **off** by
default here, deliberately: on a stream with game audio under the mic it is too
aggressive and discards real speech. A precision/recall tradeoff resolved
against the default.

---

## 4. AI/ML: working with LLMs

**Files:** `shorts_generator/highlights.py`, `shorts_generator/seo.py`,
`shorts_generator/local/llm.py`

### The context window problem

A 3h47m transcript is far larger than a usable context window, and even where it
fits, attention quality degrades over very long inputs ("lost in the middle").

**Solution: chunking with overlap.** 20-minute windows, 60-second overlap. The
overlap exists because a moment straddling a boundary would otherwise be cut in
half and scored as two weak fragments. Each chunk's timestamps are rebased to
zero, then offset back after ranking — so the model always reasons about a
small, self-consistent timeline.

This is the same **sliding window** idea used in signal processing and in
document chunking for RAG.

### Prompt engineering, concretely

The ranking prompt is not "find good clips". It contains:

- **Role framing** — "elite short-form video editor".
- **Domain knowledge the model lacks** — how to distinguish streamer speech from
  game narration in a single mixed audio track with no speaker labels, by
  register (spoken filler vs written prose).
- **A hard constraint** — every highlight must contain the streamer's own
  speech, because the game's audio is not the channel's content.
- **A ranked rubric** — eight virality signals in priority order.
- **Output-shape enforcement** — exact JSON schema, no prose.

### Classify, then route to a specialised prompt

One prompt cannot be right for every kind of video. The stream rubric's hard
rule, that every clip must contain the streamer talking over the game, is
exactly right for a stream and nonsense for a travel vlog. Rather than one
compromise prompt, the app runs **a cheap classifier first and routes to a
specialised prompt** (`CRITERIA_BY_KIND`) — stream, vlog, podcast, tutorial,
or general. It is the same idea as a router in front of expert models: spend a
small call deciding which expertise applies, then apply it undiluted.

Three details make it hold up:

- **Coarse classes over fine labels.** The classifier distinguishes interview
  from podcast and lecture from tutorial, but the router folds them to five
  kinds. Each extra class is another prompt to keep in step, and classes that
  are good for the *same reasons* would just be copies drifting apart.
- **A human label beats the classifier.** The user's pick or words pin the class;
  the classifier still runs, and a disagreement is logged rather than silently
  resolved. Labels a person gives are ground truth for that run.
- **A classification is state, not a function.** A model can classify the same
  video differently twice. Inside a resumable run that nondeterminism is a bug:
  a different class invalidates every checkpointed chunk. So the first answer
  is persisted and reused — **memoising a nondeterministic call** is what makes
  the pipeline idempotent again.

### Knowledge that goes stale belongs in data, not code

What a model knows about how YouTube or Instagram rank videos is frozen at its
training cutoff, and the platforms change several times a year. So the app
hands the packaging writer a **growth playbook** — a dated Markdown file that
says what each platform measures and how to package for it — instead of baking
that knowledge into the prompt text. This is **grounding**: supplying current
facts in context rather than trusting parametric memory. It is retrieval
without a vector store, because the whole corpus is one short document.

Keeping it in a file turns an algorithm change into **a configuration change,
not a release**. The app looks for a newer copy in the repository once a day,
with three rules taken from how remote configuration is done safely:

- **Monotonic versions.** A fetched copy is used only when its review date is
  newer than the bundled one, so a stale cache can never roll a fresh build
  backwards.
- **Local override wins.** A `playbook.md` the user wrote beats both, which is
  the same precedence as environment variables over config files.
- **Fail static.** Any fetch error leaves the last good copy in charge, and a
  run never waits for the network.

### One content, three targets

A clip goes to three apps that reward different things: a searchable title on
YouTube, the first 125 characters of a caption and shares on Instagram, search
keywords on TikTok. Writing all three in one call would mean one huge JSON
reply, and one failure would take out the part the run cannot do without. So
it is **two passes**. The second is conditioned on the first's decisions (its
chosen title and search phrase), which keeps the three consistent, and it
**degrades independently**: if it fails, the YouTube text is transformed into a
safe fallback (hashtags moved out of the sentence, `#shorts` removed) rather
than the whole step failing.

### Score calibration — a real bug worth telling

The prompt asked for a 0–100 score. Across 96 candidates from one real VOD,
every score landed between **73 and 95**, standard deviation 5.15, nothing below
73.

That is **score inflation**, and it made the ranking useless: the difference
between the clip that shipped 5th and the one that placed 25th was three points,
well inside the model's own run-to-run variance. The top-5 cut was effectively
random from a tie.

**Fix: anchor the scale.** Give explicit bands, state that most moments are a
30–60, and cap the model at one 90+ per chunk. This is **rubric calibration** —
the same reason human graders get a rubric instead of "score it out of 100".

> **Generalisable claim:** an unanchored numeric scale from an LLM is a *ranking
> signal with unknown units*. If you are going to threshold or sort on it, you
> have to pin the scale to something.

### Structured output

`response_mime_type: "application/json"` plus a schema in the prompt, plus a
tolerant parser (`_parse_json_loose`) that survives markdown fences, plus a
retry that re-asks more forcefully on a parse failure. **Defence in depth**: the
model is asked nicely, constrained by the API, and then not trusted anyway.

### Asking for several answers, then ranking them

A model asked for one title returns its first idea. Asked for five on named
angles (search, curiosity, reaction, detail, stakes), it explores the space,
and the best of five beats a single draw — the same reason sampling several
candidates and picking one (best-of-n) works in general.

Ranking them uses two scorers with different blind spots. The model scores
each option on a **rubric** — hook, clarity, searchability, truth — because
judging whether a sentence would stop a stranger scrolling is language work.
The code scores it too (`score_title`), on what language models are
notoriously bad at: counting characters, noticing a required word is missing,
spotting a name that must not be there. The final rank blends them, 70/30, and
an option the model itself rated as not fully true is dropped rather than
down-weighted. A rubric turns "which is best" into several smaller judgements,
each easier to get right and to audit afterwards.

The same pattern ranks descriptions and Instagram captions, each with its own
rubric and check. Tags are scored differently, because a model's tag list has
no rubric to lean on: each tag's score is a **weighted blend of a prior and a
position** — 60% what kind of term it is (the exact subject is worth more than a
format word) and 40% where the model placed it. The prior encodes domain
knowledge, and the position keeps the model's own judgement in the rank.

### Temperature

`0.2`. Low but nonzero. This is a judgement task where you want consistency, not
creativity — but not fully deterministic either, since the retry path benefits
from a genuinely different attempt.

### The degradation path

If the SEO call fails, `attach_seo` falls back to deriving metadata from the
clip's own hook sentence and marks `generated: False`.

**This is graceful degradation, and this project also shows its failure mode.**
When the LLM was unavailable during a real run, the fallback used a *hallucinated
Korean transcript line* as the title — which then became the filename on disk.
Degrading gracefully means the pipeline survives; it does not mean the output is
good. Anything downstream that treats fallback output as equivalent to real
output will propagate the degradation.

---

## 5. AI/ML: computer vision

**Files:** `shorts_generator/local/gaming_layout.py`, `clipper.py`, `shorts_generator/faces.py`, `shorts_generator/accel.py`, `shorts_generator/vision.py`

**Haar cascade classifiers** (`cv2.CascadeClassifier` with
`haarcascade_frontalface_default.xml`). This is *classical* CV, not deep
learning, and the distinction is worth being able to draw:

- **Haar features** — sums of pixel intensities in adjacent rectangles, capturing
  edge and line patterns. Computed in constant time using an **integral image**
  (a summed-area table, so any rectangle sum is 4 lookups).
- **AdaBoost** — an ensemble of thousands of these weak classifiers, each barely
  better than chance, combined into a strong one.
- **Cascade** — the classifiers are ordered into stages; a window failing any
  stage is rejected immediately. Since most of an image is not a face, most
  windows die in stage 1. That is where the speed comes from.

**Why Haar was first, and why YuNet replaced it:** Haar runs fast on CPU and
ships as a small XML file inside OpenCV — the right call when the task was
"find the approximate face box in a webcam overlay". It stopped being the
right call once the cost of its misses showed up: a twenty-frame vote to stay
on the streamer, a second cascade for profiles, and faces lost to a turned
head. **YuNet** is a small convolutional network — about 230 KB of ONNX weights
— that OpenCV runs natively, with no deep-learning framework in the bundle. It
is trained on faces at angles and in poor light, returns a confidence per
face, and runs at ~12 ms a frame on a laptop CPU. The deployment cost that once
justified Haar turned out to be one small file. Haar stays as the fallback, so
a missing model degrades detection rather than breaking it.

### Trusting a test over a name

A GPU video encoder is only worth using if it works *on this machine*, and
nothing the system says is proof. ffmpeg lists the encoders it was *built*
with, not the hardware present — this laptop's list includes AMD's encoder with
no AMD GPU in it. CTranslate2 counts an NVIDIA GPU whether or not the CUDA
libraries it needs are installed. Both are capability claims, and both were
wrong often enough to cause real failures.

So `accel.py` treats them as hypotheses to test: a one-second encode per listed
encoder, and a `ctypes` load of each CUDA library, before anything is chosen.
And because a test can pass and the real thing still fail later (a session
limit, a driver fault), the chosen encoder is also allowed to fail at run time,
once, before the clip is redone on the CPU. That is the general shape of
**capability detection**: probe with the real operation, cache the answer, and
keep a fallback for when the probe was not the whole story.

**Sampling:** the face is located from several frames, not one, and the results
are aggregated (`from 14/20 samples` in the logs). One frame can catch a blink, a
turn, or a transition; sampling is cheap variance reduction.

### Consensus instead of "the biggest one"

A face detector answers "where are the faces", not "which one is the
streamer". Taking the largest face in the webcam's corner locked onto a baby's
photo inside a game. The fix is a vote: across ~20 frames spread over eight
minutes, each detection counts how many others sit within one face-width of
it, and the best-supported cluster wins (`_recurring_face`). This is the same
idea as RANSAC or a mode estimate — the true answer is the one most
observations agree on, and an outlier that shows up once cannot outvote it
however large it is.

### Temporal statistics: telling an overlay from a game

In any single frame, the webcam overlay's border is just an edge among
thousands. Across many frames it is special in two ways, and the locator uses
both:

- **Persistence.** The 20th percentile of the gradient magnitude at each pixel,
  across all samples, is high only where an edge exists in ~80% of them. A
  percentile rather than the mean, because the mean is dragged up by one frame
  with a strong edge in it.
- **Change on one side only.** The per-pixel standard deviation over the
  samples is high where the picture keeps changing (the game) and low where it
  does not (the streamer's room). The overlay border is where a persistent edge
  has high change outside and low change inside. A door frame behind the
  streamer is persistent too, but still on both sides, so it scores nothing.

Neither needs a model: it is two reductions over a stack of frames, which is
why it runs in about five seconds per clip.

### Planning a camera path, not chasing a detection

A crop that follows each new detection inherits the detector's noise as camera
shake. The face-following renderer instead treats the detections as a noisy
signal and plans the whole path first — the offline version of what a camera
operator does:

- **Median filter** — removes single-sample spikes (a false positive) without
  delaying real moves, which a moving average would smear.
- **Dead zone with hysteresis** — the window only moves when the face leaves
  the middle 24% of it, so a head nodding in place produces no motion at all.
- **Zero-phase Gaussian smoothing** — convolving the whole path forward *and*
  centred, possible because the path is known in advance. An online filter
  (like the old exponential blend) always lags behind the face; a centred one
  starts easing *before* the move, which is what makes a pan look intentional.
- **Segmenting at cuts** — smoothing across a hard cut in the source would pan
  through it. Each stretch between cuts is smoothed on its own.

Measured on the same clip: direction reversals fell from 50 to 4.

### Vision-language models, and why confidence is not evidence

`vision.py` sends frames to a multimodal LLM — a model whose input can be
images as well as text — and asks what the clip is. It is good at this, and
wrong in one predictable way: a game that looks like other games gets named
anyway, with full confidence. Model-reported confidence is not calibrated, so
the code does not use it. It asks for the *evidence* instead (`named_by`: text
on screen, speech, the listing, unmistakable) and demotes any name without a
confirming kind to a guess. That turns a probability the system cannot trust
into a category it can check — the same move as asking a witness what they saw
rather than how sure they are.

---

## 5a. AI/ML: audio signal processing and feature fusion

**Files:** `shorts_generator/signals.py`, `boundaries.py`

The ranking model reads a transcript. A transcript is a lossy projection of the
thing you actually care about: it keeps the words and throws away the volume,
the timing, and the silence. Two moments can be identical on the page and
opposite in the room. So a second, cheaper modality is measured directly.

### The envelope: from a waveform to 60,000 numbers

Audio at 48 kHz stereo is far more information than a hook detector needs. What
is wanted is an **amplitude envelope** — how loud, over time — so the signal is
reduced hard before anything looks at it:

1. **Downmix and downsample** to mono 16-bit at 4 kHz. Loudness is a
   low-frequency-of-change property; the Nyquist limit this violates for
   *listening* is irrelevant for *measuring energy*.
2. **Frame** into 0.25s windows and take the **RMS** of each —
   `sqrt(mean(x²))`, the standard energy measure, rather than a peak, because a
   single sample spike is not loudness.
3. **Stream it.** A four-hour VOD is gigabytes of PCM even at 4 kHz. Reading it
   through a pipe in blocks and keeping only the per-window result means memory
   stays flat regardless of source length — the array that survives is one
   float per quarter second.

### Normalising against the signal's own distribution

Comparing raw RMS between videos is meaningless — microphones, mixes and
mastering differ. Comparing against the video's **own** distribution is not.
The naive choice is min/max scaling, and it is wrong here: one clipped frame
sets the maximum and compresses every real moment into a narrow band at the
bottom. **Percentile anchors** (p50 for the "quiet" threshold, p95 for the
spike scale) are robust to exactly that outlier.

This is the same reasoning behind robust statistics elsewhere in the project —
the webcam rectangle is a **median** of several detections, not a mean, so one
bad frame cannot drag the framing off.

### Weighted feature fusion, and the missing-feature problem

Five signals, each normalised to 0–1, combined by fixed weights into one score.
Two of them (live-chat velocity, facial reaction) are not obtainable in this
system. The interesting question is what to do about that.

- **Scoring them zero is wrong.** It silently lowers the ceiling: the best clip
  in a video with no chat log could never score above 80, so scores stop being
  comparable across videos — which is the one thing a score has to be.
- **Redistributing their weight** across the available signals keeps the scale
  intact. An 80 means the same thing either way.

But redistribution has a failure mode of its own, and it is worth being able to
name: **it preserves the range while inflating the confidence.** With no audio,
the entire score collapses onto one keyword list, and a clip containing the
words *"no way"* scores a flat 100 — outranking everything the model actually
understood. So a second term is needed:

```python
coverage = measured_weight / obtainable_weight     # 1.0 with audio, 0.385 without
share    = SIGNAL_WEIGHT * coverage
blended  = (1 - share) * model_score + share * measured_score
```

Redistribution fixes the *scale*; coverage fixes the *authority*. Evidence you
could not gather should move your conclusion less, not differently.

### Hard constraints are not features

Dialogue density in the opening two seconds is deliberately **not** in the
weighted sum. It is a disqualifier: below the floor, the blend is scaled down by
up to 35%. A weighted average lets a strong signal buy off a fatal one — a very
loud clip could out-vote the fact that its first two seconds are silence. Some
conditions are not tradeable, and modelling them as weights says the opposite.

The mirror of that: zero words counted when there is *no transcript* means "we
did not look", not "there is silence". A missing measurement and a measurement
of zero must never share a representation.

### Post-hoc enforcement over prompt compliance

The clearest lesson in this part of the system is about the division of labour
between a model and the code around it.

Asked for 30-second clips, an LLM returns 19s, 24s, 47s. This is not
disobedience — it is being asked to do arithmetic over timestamps it half
remembers while simultaneously holding a JSON schema and making an editorial
judgement. Adding *"this is a HARD requirement"* to the prompt buys a little
compliance and no guarantee.

The fix is to split the request by what each side is actually good at:

| Asked of the model | Enforced in code |
|---|---|
| *Which* moment is worth clipping | How long the clip runs |
| *Which line* it should open on | Where that line is in the timeline |
| Why it works, and how to title it | That it does not cut mid-sentence |

The second column is deterministic, testable, and free. The first is the part
only a model can do. Notice too that the model's own answer is used to derive
the timing — `first_line` is quoted accurately even when the timestamp beside it
is wrong, so the code searches the transcript for the line rather than trusting
the number. **Take the part of an answer a model is reliable at, and compute the
rest.**

---

## 6. CS: concurrency and the job model

**File:** `webapp/jobs.py`

### Producer–consumer with a single worker

A `queue.Queue` of job ids and **one** daemon `threading.Thread` draining it.

Interviewers will ask why not a pool. The answer: **the work is CPU- and
IO-saturating already.** Whisper saturates the CPU (or GPU); ffmpeg saturates
the CPU. Two concurrent jobs would contend for the same resource and both finish
later — no throughput gain, worse latency, and much harder progress reporting.
Serial execution is the right call for this workload.

### Daemon threads

`daemon=True` means the thread does not keep the process alive at exit. Correct
for a desktop app: closing the window should close the app, not block on a
half-finished render.

### The GIL, and why it does not bite

Python's **Global Interpreter Lock** allows one thread to execute bytecode at a
time, which normally makes threads useless for CPU-bound work. It is not a
problem here because the heavy work happens *outside* the interpreter:
CTranslate2 is C++, ffmpeg is a subprocess, OpenCV is C++. Each releases the GIL
while working. **Threads are fine when the CPU-bound work is not in Python.**

### asyncio and thread offloading

FastAPI handlers are `async`. An async function that blocks stalls the **entire
event loop** — every other request included. So blocking calls are pushed to a
thread pool with `await asyncio.to_thread(...)`.

Know the distinction cold: **`async` is for IO concurrency on one thread;
threads are for not blocking that thread.** They solve different problems and
this codebase uses both.

### Progress without shared-memory bugs

The worker thread mutates job state under a `threading.Lock`, and every mutation
bumps a `_version` integer. The SSE endpoint polls that version and only emits
when it changes. A **monotonic version counter** is a cheap, correct way to say
"something changed" without diffing state or racing on it.

---

## 7. CS: the web layer

**File:** `webapp/server.py`

### Why a local web server in a desktop app

The UI is HTML/CSS/JS in a native webview (`pywebview`), talking to `127.0.0.1`.
You get browser rendering and devtools without shipping Electron. The tradeoffs
— a bound port, a same-origin story, needing single-instance locking — are the
cost.

### Server-Sent Events (SSE)

`/api/jobs/{id}/stream` returns `text/event-stream`; the client uses
`EventSource`.

Be able to justify it over the alternatives:

| | polling | **SSE** | WebSocket |
|---|---|---|---|
| direction | client pulls | **server → client** | bidirectional |
| complexity | trivial | **low** | higher |
| reconnect | manual | **automatic** | manual |
| fits progress updates | wastefully | **exactly** | overkill |

Progress is strictly one-directional, so SSE is the right size of tool.

### HTTP range requests

Clip playback returns **`206 Partial Content`** with `Accept-Ranges: bytes` and
a `Content-Range` header. This is what lets a `<video>` element seek without
downloading a 56 MB file first — the browser requests byte ranges on demand.

### A real bug that taught the failure mode

After a title rewrite renames a clip's file, the frontend used to look its own
clip up **by filename** in the response — but the response already carried the
*new* names. Nothing matched, the update was silently skipped, and the `<video>`
kept a URL pointing at a file that no longer existed. The browser surfaced that
404 as `MEDIA_ERR_SRC_NOT_SUPPORTED` — a black player.

**The concept: never use a mutable field as an identity key.** The fix was to
match on `index`, which a rename does not touch. This is the same reason
databases use surrogate primary keys rather than natural ones.

---

## 8. CS: algorithms actually used here

### Greedy interval scheduling (dedupe)

`dedupe_highlights()` — sort candidates by score descending, then walk the list
keeping a clip only if it overlaps ≤50% with everything already kept.

- **Paradigm:** greedy. Locally optimal choice (take the best remaining), never
  reconsidered.
- **Complexity:** O(n log n) to sort, then O(n·k) for k kept clips. With n≈96
  and k≈5–30 that is trivially fast; an interval tree would be the move if n
  grew by orders of magnitude.
- **Overlap arithmetic:** `max(start₁,start₂)` to `min(end₁,end₂)`, positive
  length means they intersect. Standard interval intersection.
- **Not optimal, and that is fine.** Weighted interval scheduling has an exact
  DP solution. The greedy answer is good enough because the scores are noisy
  estimates anyway — optimising precisely against a noisy objective is false
  precision.

### Sliding window with overlap (chunking)

Covered in [§4](#4-aiml-working-with-llms). Windows of 1200s, stride 1140s.

### Fingerprint-based cache validation

`_checkpoint_fingerprint()` hashes prompt version, duration, chunk count, clip
count and length constraints into a string. If any input to the computation
changes, the fingerprint changes, and cached results are correctly discarded.

This is **cache invalidation by input identity** — the same principle behind
content-addressed storage and build-system caching. When the ranking rubric was
recalibrated, bumping `PROMPT_VERSION` invalidated every cached chunk, because
results scored under the old scale are not comparable to results under the new
one.

---

## 9. CS: caching and idempotency

Five layers, each with its own validity rule:

| Layer | Key | Invalidated by |
|---|---|---|
| Source video | video id | never (reused across runs) |
| Transcript `.srt` | path beside video | source mtime newer than cache |
| Highlight chunks | fingerprint | any fingerprint input changing |
| Job state | job id | process restart (restored from disk) |
| Rendered clips | filename | explicit re-render |

**Cache invalidation by modification time** is the classic approach (it is what
`make` does). Its weakness is worth knowing: mtime can lie — clock skew, a
restored backup, a file copied with metadata preserved. Content hashing is
correct but requires reading the whole file, which for an 11.9 GB video is
absurd. mtime is the right tradeoff *here*, and you should be able to say why.

**Checkpointing** is what makes long ranking survivable. Each chunk's result is
written the moment it succeeds, so a failure on chunk 10 of 12 costs one chunk,
not eleven. This is the same idea as checkpointing in long ML training runs.

**Idempotency:** re-running a job with the same inputs reuses everything cached
and produces the same output. That property is what makes retrying safe.

---

## 10. CS: reliability and failure design

**File:** `shorts_generator/local/llm.py`

### Exponential backoff

Retry delays of 5s, 10s, 20s, 40s, then capped at 60s. Doubling each time.

**Why exponential rather than fixed:** a service returning 503 is overloaded.
Retrying at a constant rate keeps the load on and can turn a blip into an
outage — the **thundering herd**. Backing off exponentially gives it room to
recover.

**Jitter** — randomising the delay — is the standard companion, to stop many
clients synchronising their retries. Not implemented here; worth naming as a
known gap if asked, since this is a single-user desktop app where the herd is
one.

### Retry budget, sized against reality

Originally 5 attempts ≈ 75 seconds of patience. A real Gemini capacity spike
outlasted it and nearly destroyed a run that had already paid for an 11.9 GB
download and a 29-minute transcription. Raised to 8 attempts ≈ 4.2 minutes.

**The principle: a retry budget should be sized against how long the failure
actually lasts, and against the cost of losing the work in progress.** Not
picked as a round number.

### Classifying errors

The retry loop inverts the usual rule: **give up immediately only on errors
retrying can never fix** (bad API key, malformed request, exhausted daily
quota), and retry everything else. That is the correct default when failure
modes are diverse and do not present uniformly in the error string.

Know the taxonomy: **transient** (retry), **permanent** (fail fast), and
**ambiguous** (the interesting case — did the request actually take effect?).

### Retrying a whole stage, and why it is cheap here

Retries usually live at the network call. This project also retries whole
stages — a download, a transcription, one clip's render — and lets the user
send a failed run round again. That is only affordable because every stage is
**idempotent and resumable**: running it twice produces the same result, and
a second run finds the first one's work in the caches (the download, the
`.srt`, the per-chunk ranking checkpoint) and skips it. So "retry the job" is
just "run the job", with no special recovery code at all. Designing the stages
that way first is what made the retry button a few lines.

The same classification as above decides what gets retried. A transient error
is worth another go; a permanent one (a wrong key, a missing file) fails
immediately, because retrying it only makes someone wait longer to read the
same message.

### Provider fallback, and the ceiling on retrying

Gemini → Groq → OpenAI. A **fallback chain**, with usage accounting to know when
to switch.

The idea worth carrying away: **retrying has a ceiling.** Exponential backoff
handles a *blip*. It does nothing for a provider that is genuinely saturated —
if Google is still refusing after four minutes, the ninth request to Google is
not more likely to succeed than the eighth. What changes the outcome is asking
somewhere whose capacity is *uncorrelated* with the thing that failed.

That word is the whole point. Two Gemini models share Google's capacity, so
switching between them buys nothing during an outage. Groq is a different
company on different hardware, so its availability is independent. In reliability
terms you are removing a **single point of failure** by adding a path with no
**shared fate**.

The ladder is ordered **free before paid**, so an automatic failover cannot
quietly start spending money — a small design decision that matters a lot the
first time it triggers unattended.

Two implementation details worth being able to defend:

- **Groq speaks the OpenAI API.** So the client is the `openai` package with a
  different `base_url` — no new dependency, no second response parser to keep
  correct. Reusing a *protocol* rather than writing an *integration*.
- **The recursion terminates.** `_next_provider()` returns `None` when no
  configured provider remains, which is what stops the dispatch calling itself
  forever. Any fallback that re-enters its own entry point needs a provable
  base case, and "the list ran out" is that case.

### Failing over on the right signal

The switch fires on two conditions that look similar and are not:

| | means | retry? |
|---|---|---|
| **429**, per-day quota | allowance gone until reset | no — switch now |
| **503**, after the full retry budget | provider is busy | no — already waited 4 min |

Originally only the quota case was handled, which is exactly why a capacity
spike could still kill a run. **Classifying a failure correctly is what decides
whether the response to it is right** — the same distinction as transient vs
permanent in [the taxonomy above](#10-cs-reliability-and-failure-design).

### Proving a device works

Restructuring the GPU fallback taught something general. A CUDA device that
enumerates, and a model that constructs on it, are **both worthless as proof** —
the failure only appears on the first real inference call. So the fallback has
to wrap the whole operation, not the setup:

```python
try:
    segments, info = _run("cuda", "float16")   # drains the generator
except Exception:
    segments, info = _run("cpu", "int8")       # redo everything
```

> **The concept: a health check that does not exercise the real path is not a
> health check.** This is the same reason a database "connection successful"
> check tells you very little about whether queries will work.

---

### Diagnosability as a reliability property

A failure you cannot explain is worse than one you can, even when they fail
identically. Every clip in a user's run failed, and the whole report was
`returned non-zero exit status 3752568763` — a true statement carrying no
information.

Two separate losses produced that, and they are both general.

**An error channel that goes nowhere.** ffmpeg had written one line explaining
itself, to stderr. A windowed process has no console, so a child that inherits
its handles writes into nothing. Suppressing a subprocess's output is cheap to
write and costs exactly once — the first failure on a machine you cannot reach.
The fix is to capture the stream rather than inherit it, which is also why
`communicate()` matters: it drains both pipes concurrently, where naively
reading one while the other fills its buffer deadlocks the child.

**An error code nobody can read.** ffmpeg does not exit with 1. It exits with an
`AVERROR` — a *namespaced* code, built by packing four ASCII bytes into an
integer and negating it, so `AVERROR_EXTERNAL` is `-MKTAG('E','X','T',' ')`.
Small negative values are reserved for `errno`, so the two namespaces share one
signed integer without colliding. Then the process exit status carries it as
unsigned 32-bit, and the negation wraps into a nine-digit positive number.

> **The concept: an error is only as useful as its decoder.** Compact tagged
> codes — FourCC here, `HRESULT` on Windows, `errno` everywhere — are cheap to
> return and unreadable without the table that interprets them. Ship the table
> with the thing that surfaces the error, or you have logged a checksum of the
> problem rather than the problem.

---

## 11. CS: security boundaries

### Path traversal

Clips are served by filename from a URL. Without containment, `../../` in that
filename reads arbitrary files off the disk — **directory traversal**, one of
the oldest web vulnerabilities there is.

The check in `_clip_path()`:

```python
root = Path(job.out_dir).resolve()
path = (root / safe).resolve()
path.relative_to(root)          # raises ValueError if outside
```

**Both paths are resolved first**, then compared structurally.

The subtlety worth knowing: comparing resolved paths **as strings** with
`startswith` is a classic bug, because `/data/jobs-evil` starts with
`/data/jobs`. `Path.relative_to` compares path *components*, which is why it is
correct. `os.path.basename` strips directory components as a second layer.

### Secrets

API keys live in `%APPDATA%\ClipMint\settings.json`, outside the repo,
never in source. Precedence is environment first, then that file — so CI or a
power user can override without editing anything.

**Known weakness, and say it before they find it:** the file is plaintext.
Proper handling would be the Windows Credential Manager / DPAPI. The mitigation
today is filesystem permissions and the fact that it never leaves the machine.

### Not trusting the model's output

Every field the LLM returns is coerced, clamped and validated before use —
timestamps bounded to the video duration, clip lengths rejected above
`MAX_CLIP_SECONDS`, scores clamped to 0–100. **Model output is untrusted input.**
It is generated text, not a contract, no matter what the schema said.

---

## 12. CS: operating systems and packaging

### Dynamic linking and the DLL search path

The most instructive bug in this project. `pip install nvidia-cublas-cu12
nvidia-cudnn-cu12` installs DLLs under `site-packages/nvidia/*/bin`. Python 3.8+
**removed `PATH` and the current directory from the DLL search order on Windows**
(a security hardening — it closed a DLL-hijacking vector). Libraries are expected
to call `os.add_dll_directory` explicitly. Torch does. CTranslate2 does not.

Result: installing the libraries changed **nothing**. The GPU enumerated, the
model constructed, and the first inference died on `cublas64_12.dll is not
found`.

Concepts in play: **dynamic linking**, **shared library resolution order**,
**security hardening breaking implicit behaviour**, and — the meta-lesson — *a
dependency resolved by search path is invisible to any tool that only reads
imports*, which is exactly why PyInstaller also needed to be told about it
explicitly.

### Freezing a Python app

PyInstaller bundles interpreter + libraries + code into a distributable.

- **onedir** — a folder. Starts fast, already unpacked.
- **onefile** — a single exe that extracts its entire payload to a temp
  directory **on every launch**.

That difference drove a real decision here. The CUDA runtime is ~2 GB. In the
onedir build that is disk space and nothing else. In a onefile build it would be
re-extracted on every start, by every user — including the majority with no
NVIDIA card who cannot use it. So the builds deliberately differ: onedir ships
CUDA, onefile does not, behind an explicit `--cuda/--no-cuda` flag.

**`sys._MEIPASS`** is how frozen code finds its bundled data, since paths
relative to `__file__` no longer mean anything.

### Single-instance locking

Two copies would fight over the port, the job store and the output directory. A
lock file / named mutex enforces one. This is **mutual exclusion at process
scope** rather than thread scope.

---

## 12a. CS: process control, and trust as a UX property

**The concept.** A process is something you can start, signal, suspend and
account for — not just something you launch and hope about. Operating systems
differ in what they offer, and the differences leak.

**Where it shows up here.** `shorts_generator/proc.py`.

**Suspension.** Stopping work that is already running is not the same as
declining to start more of it. POSIX has `SIGSTOP` and `SIGCONT`; Windows has
no signals and the equivalent is `NtSuspendProcess`, undocumented but stable
for decades. Knowing that gap exists is the difference between a pause button
that works and one that waits politely for a five-minute encode to finish.

The design point underneath: a pause needs *both* a way to stop what is running
and a gate on what starts next. Either alone leaves half the work going, and
the race between them — a child started in the instant after the gate was
checked — has to be closed deliberately rather than assumed away.

And suspension only reaches *processes*. Work done inside the app itself — here
Whisper's transcription and the face-tracking pass — has no process to suspend,
so it needs **cooperative** pausing instead: a checkpoint in its loop that
blocks on the same gate. Whisper's generator is lazy, so blocking between
segments stops it decoding the next window, on a CPU or a GPU alike. That is
the general split: pre-emptive control for what the OS owns, cooperative
checkpoints for what your own loop owns, and a pause button is only honest if
it has both.

**Inherited environment.** A GUI process has no console, so the OS creates one
whenever it starts a console program. That is not a bug in either program; it
is what happens when a design assumption (programs have a terminal) meets a
context that breaks it. The general lesson is that a child inherits more from
its parent than its arguments, and packaging changes what it inherits.

**Silence is the same problem.** A stage that says nothing for ten minutes is
indistinguishable from a stage that has hung, and people kill processes they
believe are hung. Two of the four stages here used to be silent — the download
because the library's own progress bar was switched off, transcription because
it only spoke when it finished — so the fix was to make each one narrate: a
percentage, a speed, a position in the audio. The general rule: **work that
takes longer than a few seconds owes the person a signal that is changing**,
and it has to come from the work itself rather than from an animation that
would keep spinning if the work had died.

An estimate is a stronger claim than a heartbeat, so it is made carefully:
elapsed x remaining/done, recomputed every second, shown only past a few
percent, and rounded to "about 7 min" because a guess that says 6m 51s is
lying about its own precision.

**Trust is a user-facing property, not a technical one.** Black windows
appearing and vanishing during a render broke nothing. It also made a
legitimate program look like malware to the people running it, which is a real
failure with a real cost — software that looks untrustworthy does not get run
twice. The same is true of an unsigned binary: the honest response is not to
insist it is fine, but to give people things they can check for themselves —
public source, reproducible builds, published checksums, observable network
behaviour.

**Naming as an interface.** A folder of `0f83f76b5623` directories is correct
and unusable. Identifiers serve the program; names serve the person, and when
output lands somewhere a human will browse, the filesystem *is* part of the
interface. Keeping both — a readable name outside, the id in a manifest inside
— costs nothing and is why the rename did not break anything reading it.

---

## 12b. CS: indirection, and the things it must not reach

**The concept.** A theme is the textbook use of one level of indirection:
name every colour, then swap what the names point at. The interesting part is
not the swap, it is that indirection is only safe where the *meaning* is
stable. Where the name means something different in the two worlds, the
indirection is the bug.

**Where it shows up here.** `webapp/static/style.css`.

Most of it is the easy case. `--text` means "text on the page background", the
page background moves, and so does the text. Twenty faint overlays collapsed
to `rgba(var(--fg-rgb), a)` — one token holding *what sits on the background*,
alphas untouched — which is indirection paying for itself: one line changed,
twenty rules followed.

**The exceptions are where the thinking is.** The duration pill on a video
thumbnail was `var(--text)`. That was never right, it was only *accidentally*
right: black text on a light page, white text on a dark page, and the pill
happens to sit on a video, which is black in both. Flip the theme and it
becomes black on black. The token named a relationship the element did not
have.

The general shape: **a variable is a claim about what something is for.**
`var(--text)` claims "this is text on the page". The pill is text on a video.
While there was one theme, both claims produced the same colour and the wrong
one cost nothing — the second theme is what turns a sloppy name into a defect.
This is the same reason the `--warn` yellow used as a badge on a video needed
a different token from the `--warn` used as a word on the page: one colour,
two jobs, and only one of them changes when the page does.

**Also worth knowing: contrast is not symmetric.** The grey that reads
comfortably on black is too faint on white — same colour, same ratio arithmetic,
different result, because the surround differs. Themes are not inversions.

---

## 12c. CS: software distribution and self-update

**The concept.** Getting software onto a machine and keeping it current are
different problems. Packaging solves the first. The second is only interesting
when there is no package manager underneath you — and a single unsigned .exe
someone downloaded from a GitHub release has nothing underneath it at all.

**Where it shows up here.** `webapp/updater.py`.

**Distribution as a trust problem.** Fetching and executing a binary is the
riskiest thing this app does. Three separate controls, because none is
sufficient alone:

- *Transport* — HTTPS to hosts on a fixed allowlist, checked before the request
  and again after redirects.
- *Integrity* — SHA-256 from the releases API, compared before anything is
  replaced. This proves the bytes match what the API described. It does not
  prove who built them; that needs code signing, which needs a certificate.
- *Authority* — the repository is compiled into the build. The page can ask to
  install "the update" but cannot say what the update is. Anything rendered in
  a webview is untrusted input, and the way to keep it from choosing a download
  target is to never let it name one.

The interview question hiding here is *what does a checksum actually prove?*
If the server that publishes the file and the server that publishes the hash
are the same server, a compromise of that server defeats both. It defends
against corruption and interception, not against the publisher.

**Atomicity under a hostile filesystem.** Windows will not overwrite a running
executable but will rename one. That single fact shapes the design: rename the
running file aside, move the verified download into its name, relaunch. The
ordering gives you the property you want — every failure leaves a working app,
because nothing is disturbed until the hash matches, and a failed second move
puts the original back.

**Where the same design does not port.** A macOS `.app` is a directory signed
as one unit, so "replace the file" has no single file to replace, and a
half-swapped bundle is worse than no update: its signature no longer matches
its contents, and the app the OS then refuses to open is the one the update was
delivering. The general lesson is that self-update is a property of the
*packaging format*, not of the program — so the mac build keeps the half that
carries most of the value and cannot go wrong (noticing that a version exists)
and drops the half that can (installing it).

**Resource cleanup is where this gets subtle.** The replaced build cannot be
deleted by the process that replaced it: the old process is still exiting and
still holds the file. A naive delete fails, and if you swallow the error you
have silently left a few hundred megabytes on a user's disk. The fix is to
retry until the handover completes rather than to try once at the wrong moment.

A related trap: `os.replace` is a rename, except when the OS decides to satisfy
it by copying, at which point the source survives and you have two copies of a
229 MB file. Not trusting an operation to have the side effect you wanted, and
checking, costs one syscall.

**Version identity.** The updater compares a tag against a version compiled
into the binary, which makes those two numbers a single logical value stored in
three files. Left unenforced, they drift, and the failure is not cosmetic: a
build that misreports its version offers every user an update to the version
they are already running, forever. So the build refuses to run when they
disagree, and CI checks the tag before building and the binary after.

**The bootstrap problem.** No update mechanism can reach the versions that
shipped before it existed. Those installs are unreachable by construction and
need one manual download. Worth stating plainly rather than discovering.

---

## 12d. CS: absence as a signal

**The problem.** A render finishes. Should the app interrupt you about it? Only
if you are not already looking at it — and a server process cannot see its own
window. Worse, the two states it has to distinguish look identical from inside:
somebody watching the progress bar, and somebody who closed the window an hour
ago and went to bed.

**Why a flag does not work.** The obvious design is a boolean the page sets:
`true` when it becomes visible, `false` when it is hidden. It fails on the case
that matters most. A window that is closed, or a tab shut mid-render, never
sends the `false`. It leaves a `true` behind, and the app stays quiet forever at
exactly the moment it should speak.

**Heartbeats.** The fix is to make the signal expire rather than persist. The
page sends its state every twenty seconds whether or not anything changed, and
the server treats a report older than forty-five seconds as no report at all.
Presence has to be continuously renewed; absence is what happens by default,
including when the reporter is no longer able to report anything.

This is a lease. It is the same idea as a TTL on a cache entry, a lock lease in
a distributed system, or TCP keepalive: rather than trusting a peer to announce
that it has gone, assume it has gone unless it keeps saying otherwise. The
reason is always the same — a peer that crashes cannot send its own obituary, so
a system that needs to detect crashes must never ask peers to announce them.

**The same mechanism, pointed both ways.** The SSE stream in section 7 sends a
`: ping` comment every fifteen seconds for the mirror-image purpose: to stop a
proxy concluding from silence that a live connection is dead. One heartbeat
proves liveness; the other detects its absence. Identical machinery — the
difference is entirely in what you do when the beats stop.

**Where it lands in the code.** `webapp/notify.py` holds the lease and the
three platform notifiers; `POST /api/attention` feeds it; the `finally` in
`JobManager._run_forever` asks it one question, once, when a run has ended.

## 13. CS: internationalisation

**File:** `webapp/static/i18n.js`

### Keying by source string

Translations are keyed by the **English string itself**, not by an invented key
like `nav.create`.

- **Upside:** markup stays readable, and a missing translation falls back to
  English automatically instead of rendering a raw key at the user.
- **Downside:** editing English copy orphans its translations.

A real tradeoff with a real cost — name both sides.

### Preserving the source

Each translated DOM node keeps the English it was born with, in a `WeakMap`.
Without that, switching Hindi → Japanese would look up *Hindi* text in the
Japanese table, find nothing, and leave the page stuck. **A transformation you
intend to reapply must be applied to the original, not to its own output.**

`WeakMap` specifically because keys are DOM nodes — entries are garbage
collected when the nodes are removed, so it cannot leak.

### Translating what does not exist yet

Most of this UI is rendered dynamically after load. Rather than teaching every
render function to translate, a **`MutationObserver`** watches for inserted
nodes and translates them — the **observer pattern**, with a reentrancy guard,
because the observer's own DOM writes would otherwise retrigger it infinitely.

### Defaulting

English on a fresh install, and the browser locale is deliberately **not**
consulted — a machine set to another language should not hand a first-run user
an interface nobody chose. A **product** decision, not a technical one, and
being able to distinguish those is itself a signal.

---

## 14. Questions you should expect

**"Walk me through what happens when I paste a URL."**
Follow [§2](#2-the-system-in-one-picture) top to bottom. Mention the caches.

**"Why is it slow?"**
Transcription dominates — it is the only stage proportional to *video length*
rather than clip count. Which is why it is cached, and why the GPU path was
worth fixing (104s → 21s on 900s of audio). Rendering is the rest, and the
Processor setting moves it onto the graphics chip where one really works —
measured 2× on the stacked layout. But the biggest single speed-up was not a
GPU at all: face-follow was decoding the whole stream up to every clip because
of where one `-ss` sat, and moving it made that layout 5× faster. Profile the
stage before buying hardware for it.

**"How do you handle a 4-hour video when the context window is smaller?"**
Chunking with overlap, per-chunk checkpointing, then dedupe across chunks.
Then the honest limitation: independently-scored chunks are not strictly
comparable, so the global top-N is an approximation. See
[§15](#15-what-you-would-do-next).

**"What happens when the API fails?"**
Layered: retry with exponential backoff (8 attempts, ~4 min), then provider
fallback, then graceful degradation to non-LLM metadata, and checkpointing so
partial work survives regardless. Around all of that, each pipeline stage
retries itself, and a run that still fails can be sent round again — which
resumes from its caches, because the stages were built to be idempotent. Then
tell them what degradation *cost* — the Korean title — because that shows you
followed it through.

**"How does one app handle a stream, a vlog and a podcast?"**
It classifies first, then routes: a cheap call reads the start, middle and end
of the transcript plus the video's listing, and picks one of five specialised
ranking prompts. Then the parts that make it reliable: the user's pick beats the
classifier, the classification is saved so a resumed run cannot change its mind,
and only a *chosen* kind changes the framing — a detected one never does, because
getting that wrong on a stream would put a face-follow crop on a webcam overlay.

**"How do you keep the SEO advice current when the algorithms keep changing?"**
It isn't in the prompt. It's a dated playbook file, bundled with every build and
refreshed from the repository at most once a day. A newer review date wins, a
user's own copy wins over both, and every failure keeps the last good copy. So an
algorithm change is an edit and a push, not a release. The honest limit: the
playbook is what the platforms have said publicly, not something measured
against this channel's own retention yet.

**"How does it know which game is in a clip?"**
It looks: four frames per clip go to a vision model. The interesting part is
what it does *not* trust. The model named a Fears to Fathom clip Phasmophobia
at confidence 1.0 — model confidence is not calibrated — so the code asks for
the *evidence* instead (text on screen, speech, the listing) and demotes any
name without it to a guess. An unnamed clip is filed by genre; a misnamed one
is called out in its own comments.

**"What was the hardest bug?"**
The GPU one is the best story: three independent silent failures stacked
(wrong library probed, DLLs unregistered, not bundled), each of which
individually produced "works, but on CPU" with no error. Second best: score
inflation, because it required *noticing that correct-looking output was
meaningless* — the system reported five clips at 94–95 and looked fine.

**"What would you do differently?"**
See [§15](#15-what-you-would-do-next). Have a real answer; "nothing" is a bad one.

**"Is this just an API wrapper?"**
Meet it head-on. The models are off-the-shelf; the engineering is the system
around them — chunking strategy, checkpointing, cache design, failure taxonomy,
the security boundary on file serving, the packaging tradeoffs. Then give one
concrete example in depth. The score-calibration bug is the strongest, because
it required understanding *why* an LLM's numeric output was untrustworthy rather
than just calling the API.

**"How do you know the clips are actually good?"**
The honest answer, which is more impressive than a fake one: there is no
automated quality metric. Ranking quality is judged manually. Building an
evaluation set — clips labelled by actual retention data from published Shorts —
is the obvious next step and the only way to make the rubric empirical rather
than assumed.

---

## 15. What you would do next

Ordered by value, with the reasoning that makes each defensible:

1. **A final cross-chunk ranking pass.** Today each 20-minute chunk is scored
   independently, so a 90 in chunk 3 and a 90 in chunk 9 are not really
   comparable — they were assigned by separate calls with separate implicit
   baselines. One final comparison pass over the surviving candidates would make
   the global top-N meaningful rather than approximate.

2. **A local LLM as the last rung.** Groq now covers the common case — a free
   provider with independent capacity. What it does *not* cover is being
   offline, or wanting the app to work with no API key at all. Ollama would.
   Be precise about the tradeoff: on 8 GB of VRAM you can run an 8–14B model,
   and it is genuinely **worse** than either cloud provider at nuanced judgement
   over a long transcript. It is the right *last resort*, not the right primary.

3. **Evaluation against real retention data.** Everything about the ranking
   rubric is currently assumed. Published Shorts produce retention curves; those
   are labels. Without them, "viral potential" is an untested hypothesis.

   Half of this now exists: every clip's feature vector and both of its scores
   are written into `job.json` beside it, so the training set is accumulating
   whether or not anything reads it yet. What is missing is the other half —
   an OAuth flow to the YouTube Analytics API, and a correlation pass that
   re-derives the weights in `signals.py` from *this channel's* median
   retention rather than from a rule book. Note the shape of that problem: it
   is not a modelling challenge, it is a plumbing one, and the recording had to
   come first because you cannot correlate against outcomes nobody wrote down.

4. **Surface degraded output in the UI.** `generated: False` is recorded but a
   degraded run looks identical to a good one until you notice the filenames.

5. **Jitter on the retry backoff**, and Credential Manager for the API key.
   Both small, both known gaps.

6. **Transcription on non-NVIDIA GPUs.** Done since this list was written: a
   learned face detector (YuNet) and GPU video encoding on every vendor. What
   is left is the slowest stage: CTranslate2 only accelerates on NVIDIA, so a
   Mac or an AMD/Intel machine transcribes on the CPU. A Whisper backend on
   Apple's Metal or on DirectML would close that — at the cost of a second
   engine to keep in step with the first.

7. **Rank titles against outcomes too.** Title options are scored by a rubric
   and a code check, both assumed. The same retention data as item 3 would say
   which *angle* actually travels for this channel.

---

## Related reading

- [HOW_IT_WORKS.md](HOW_IT_WORKS.md) — this codebase, file by file
- [README.md](README.md) — what it does and how to run it

---

## Author

**Parth Bhadana**

[YouTube](https://www.youtube.com/@ParthBhadana799) &middot; [GitHub](https://github.com/kleZ799) &middot; [LinkedIn](https://www.linkedin.com/in/parth-bhadana-530014202/) &middot; [Discord](https://discord.gg/jnMrGbBz3m)

Built and maintained by me. If you use it, fork it, or ship anything based on
it, the MIT licence asks one thing in return: keep the copyright notice.

Repository: <https://github.com/kleZ799/clipmint>
