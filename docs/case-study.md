# ClipMint: an engineering case study

By **Parth Bhadana** — [GitHub](https://github.com/kleZ799) · [LinkedIn](https://www.linkedin.com/in/parth-bhadana-530014202/) · [parthbhadana57@gmail.com](mailto:parthbhadana57@gmail.com)

A five-minute read for engineers, interviewers and anyone hiring. The
[README](https://github.com/kleZ799/clipmint#readme) is what the app does;
[HOW_IT_WORKS.md](https://github.com/kleZ799/clipmint/blob/main/HOW_IT_WORKS.md)
is the full map of the code; this is the short version of how it was built and
what was hard.

## In one paragraph

ClipMint is a desktop app that turns any long video (a podcast, interview,
vlog, tutorial, gaming video or stream VOD) into captioned vertical Shorts,
Reels and TikToks, on the user's own PC. It transcribes locally with Whisper,
ranks every moment with an LLM against rules for that kind of video, checks the
best ones with a vision model, frames them for 9:16, burns in word-by-word
captions, cuts the dead air, writes the titles and uploads to YouTube. Then it
reads back how each Short did and tunes its own ranking when the evidence is
strong enough. I designed, built, shipped and support it alone.

## The numbers

| | |
|---|---|
| Releases | 38 since v1.0.0 on 31 Aug 2026, each built by CI for Windows, macOS and Linux |
| Code | ~19,400 lines of Python, ~6,000 of JavaScript, ~2,700 of CSS |
| Authorship | 94% of the lines in the tree by `git blame` (started as a fork of a 1,100-line CLI script) |
| Install | One 224 MB file. No Python, no ffmpeg, nothing else to install |
| Cost to the user | Free. Runs on Gemini's or Groq's free tier |
| Interface | 7 languages |

## Architecture

```
  Native window (pywebview / WebView2)
          │ HTTP + Server-Sent Events
          ▼
  FastAPI on 127.0.0.1, free port ──► job queue, one worker thread
                                            │
      ┌─────────────┬──────────────┬────────┴─────┬───────────────┐
      ▼             ▼              ▼              ▼               ▼
  download      transcribe       rank          render          publish
  yt-dlp,       faster-whisper   LLM + audio   ffmpeg filter   YouTube API,
  quality-aware (CTranslate2),   signals +     graphs, OpenCV  OAuth PKCE,
  cache         CPU or CUDA      vision judge  face tracking   resumable upload
                                                                   │
                         views read back, permutation-tested ◄─────┘
                         before they change any ranking weight
```

Requests never block on the pipeline: `POST /api/jobs` enqueues and returns an
id, and the window follows along over SSE. Every stage checkpoints to disk, so a
crash or a closed window resumes from where it stopped instead of starting over.

## Five problems worth talking about

### 1. Finding the streamer's face when the game is full of faces

Off-the-shelf auto-croppers follow the biggest face. On gameplay that is
usually a game character, so the Short ends up centred on a cutscene with the
streamer talking from off-screen.

**Approach.** Use the one property that separates a webcam overlay from a game:
it doesn't move. Sample ~20 frames across eight minutes, detect faces with a
230 KB learned detector (YuNet) shipped in the build, and keep the face that
keeps turning up in the same place. The overlay's border is the edge present in
every sample. Then render the whole layout in one ffmpeg pass (crop, crop,
scale, `vstack`) with no frames crossing into Python.

**Result.** Clips render in seconds instead of minutes, and a face in the game
appears once and is outvoted.

### 2. Framing whoever is talking

A vertical crop fits one person. With two people in a shot the crop used to stay
on whoever sat nearer the camera, through every line the other person said.

**Approach.** Watch each person's mouth movement frame against frame, plan a
camera path that cuts to the speaker the way an editor would, and hold every
shot for at least two seconds so a laugh doesn't flick the frame.

**Result.** Measured on two two-person podcast episodes cut into 30-second clips:
the speaker was in frame **86% and 96%** of the time, against **73% and 10%** for
"biggest face".

### 3. Long videos returned zero highlights

Long transcripts are ranked in chunks. Every chunk past the first came back with
timestamps relative to the chunk, which the validator then clamped away, so a
two-hour video produced nothing.

**Approach.** Rebase every chunk's timestamps onto the source timeline before
validation, and checkpoint each ranked chunk so a quota error halfway through
resumes rather than re-billing the whole video.

**Result.** Multi-hour VODs rank end to end, and a run interrupted by an API
limit picks up at the chunk where it stopped.

### 4. Learning from a handful of numbers without fooling myself

v1.24.0 reads the views on each posted Short back from YouTube and lets them
adjust the ranking. A small channel has a few dozen data points, and with that
many, some signal will correlate with views by chance.

**Approach.** Match uploads to clips, test each signal with a permutation test,
and change a weight only with at least 12 clips and p < 0.05. Everything else is
reported as "no clear link yet" rather than acted on.

**Result.** On my own channel (28 matched clips), the only real finding was that
clips with an AI-written title got a median of **42.5 views against 3.5** for
fallback titles (p = 0.0002). No ranking signal passed, so the weights stayed
where they were. The system correctly declined to tune itself on noise, and the
finding pointed at the real fix: the title step was running out of AI quota.

### 5. Shipping desktop software to people who will never open a terminal

**Approach.**
- PyInstaller single-file builds with ffmpeg bundled; the CI release workflow
  builds all three platforms from one tag, checks each binary reports the right
  version, and starts the Linux build and asks it for its interface before the
  release is published.
- **Self-update on Windows**, which won't let a running exe be overwritten but
  will let it be renamed: move the running file aside, verify the download's
  SHA-256, put it in place, relaunch, and roll back if any step fails.
- **GPU encoding that can't strand anyone**: each hardware encoder (NVENC,
  Quick Sync, AMF, VideoToolbox) is test-encoded before it's trusted, with
  libx264 underneath.
- **YouTube uploads** over OAuth with PKCE and a loopback redirect, in 8 MB
  resumable chunks, so a dropped connection carries on instead of resending
  200 MB.
- **A fallback ladder for the AI**: Gemini, then Groq, then OpenAI, free before
  paid, switching on a spent quota or an exhausted retry budget, so a busy
  provider at chunk 9 of 12 doesn't throw away a 29-minute transcription.

## Stack

Python · FastAPI · uvicorn · Pydantic · Server-Sent Events · faster-whisper
(CTranslate2) · Gemini, Groq and OpenAI APIs, including vision · OpenCV (YuNet)
· NumPy · ffmpeg filter graphs · NVENC / VideoToolbox / Quick Sync / AMF ·
yt-dlp · YouTube Data API v3 · OAuth 2.0 + PKCE · pywebview · PyInstaller ·
GitHub Actions · vanilla JavaScript and CSS (no build step, on purpose)

Why each one was chosen is in
[HOW_IT_WORKS.md §3](https://github.com/kleZ799/clipmint/blob/main/HOW_IT_WORKS.md#3-tech-stack-and-why-each-choice).

## What I'd do differently

- **Tests earlier.** CI checks version consistency and starts every built app,
  but most of the pipeline is verified by running it on real footage. The
  speaker-follow evaluation above should be a regression suite, not a one-off.
- **The Mac build is unverified.** I don't own a Mac. It is built and checked by
  GitHub's macOS runners, and the README says so plainly.
- **Measure before tuning, from day one.** The views loop showed that the thing
  I'd spent the most time on (ranking) was not what limited views; the titles
  were.

## Read more

- [README](https://github.com/kleZ799/clipmint#readme): every feature, with screenshots
- [HOW_IT_WORKS.md](https://github.com/kleZ799/clipmint/blob/main/HOW_IT_WORKS.md): the codebase, module by module
- [CONCEPTS.md](https://github.com/kleZ799/clipmint/blob/main/CONCEPTS.md): the AI/ML and CS concepts behind it
- [How ClipMint compares](compare.md) with Opus Clip, Klap, Vizard and the open-source alternatives
- [Release notes](https://github.com/kleZ799/clipmint/releases)

I'm open to software engineering roles and collaborations. Reach me at
[parthbhadana57@gmail.com](mailto:parthbhadana57@gmail.com) or on
[LinkedIn](https://www.linkedin.com/in/parth-bhadana-530014202/).
