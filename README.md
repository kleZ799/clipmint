<div align="center">

# ✂️ ClipMint

### Turn any long video into Shorts, Reels and TikToks — on your own PC

An all-in-one short-form clip generator for stream VODs, gaming videos, vlogs,
podcasts and tutorials. It works out what kind of video it is, finds the moments
worth posting, cuts them to 9:16, and writes the title, captions and hashtags
for **YouTube Shorts, Instagram Reels and TikTok**.

No subscription, no per-clip credits, no watermark, and nothing is uploaded —
transcription and ranking both run locally.

[![Download](https://img.shields.io/badge/⬇_Download_for_Windows-219_MB-ff0033?style=for-the-badge)](https://github.com/kleZ799/clipmint/releases/latest/download/ClipMint.exe)
[![License](https://img.shields.io/badge/license-MIT-green?style=for-the-badge)](LICENSE)

[![Download for Mac](https://img.shields.io/badge/⬇_Download_for_Mac-BETA_·_Apple_Silicon-f59e0b?style=for-the-badge)](https://github.com/kleZ799/clipmint/releases/latest/download/ClipMint-macOS-arm64.zip)

**The Mac build is a beta and has never been run on a Mac** — I don't own one.
It is built and checked by GitHub's macOS runners, not by me. It may not start
at all. [Tell me what happens](https://github.com/kleZ799/clipmint/issues/new)
and I'll fix it. Windows is the tested build.

[![Download for Linux](https://img.shields.io/badge/⬇_Download_for_Linux-x86--64-3b82f6?style=for-the-badge)](https://github.com/kleZ799/clipmint/releases/latest/download/ClipMint-linux-x86_64)

**The Linux build has not been tested on a real Linux desktop.** It is built
and run under WSL here, and every published build is started by the release
workflow and asked for its interface before the release exists — so it does
start, it does serve, and its ffmpeg does work. Nobody has yet sat at a
desktop distribution and made clips with it.
[Tell me what happens](https://github.com/kleZ799/clipmint/issues/new) and I'll fix it. It opens in your
browser rather than in a window of its own.

<!-- These read GitHub live, so a new release renames them on its own and
     there is no version number in this file to go stale. -->
[![Latest version](https://img.shields.io/github/v/release/kleZ799/clipmint?style=flat-square&label=latest%20build&color=ff0033)](https://github.com/kleZ799/clipmint/releases/latest)
[![Released](https://img.shields.io/github/release-date/kleZ799/clipmint?style=flat-square&label=released&color=555)](https://github.com/kleZ799/clipmint/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/kleZ799/clipmint/total?style=flat-square&label=downloads&color=555)](https://github.com/kleZ799/clipmint/releases)

**Built by [Parth Bhadana](https://github.com/kleZ799)**
&nbsp;·&nbsp; [YouTube](https://www.youtube.com/@ParthBhadana799)
&nbsp;·&nbsp; [GitHub](https://github.com/kleZ799)
&nbsp;·&nbsp; [LinkedIn](https://www.linkedin.com/in/parth-bhadana-530014202/)
&nbsp;·&nbsp; [Discord](https://discord.gg/jnMrGbBz3m)

**No Python. No ffmpeg. Nothing to install.** Double-click and go.

<img src="assets/screenshots/01-create.png" alt="The create screen: a layout prompt, the Shape and Kind of video pickers with Stream or gaming chosen, and a live 9:16 preview on the right" width="880">

<img src="assets/screenshots/00-welcome.png" alt="The launch card: what the app is, who made it, links to the channel, repo, Discord and email, and a donate button" width="640">

</div>

---

## What it does

### Describe the layout. Don't configure it.

Type *"webcam at the top, vertical for Shorts, 5 clips"* and the frame updates as
you type. Want one exact moment instead? Say *"cut 14:45 to 15:30"* and it skips
the ranking entirely. The chips are shortcuts for phrases it already understands.

### Streams, vlogs, podcasts, tutorials — it knows which

A stream, a vlog, a podcast and a tutorial are good for different reasons, so
they are not ranked by the same rules. The app works out which one it is looking
at, from the start, middle and end of what is said plus the video's own title
and description, and picks the moments that kind of video is known for:

- **Stream or gaming:** your reaction to the game, never a stretch of game
  narration with you nowhere in it.
- **Vlog:** the payoff of what happened, a plan falling apart, a first time.
- **Podcast or interview:** the hot take, the confession, the argument while
  it is still heated, opened on the answer rather than the question.
- **Tutorial:** one complete tip, or a common mistake and its fix.

Or tell it: **Kind of video** under the prompt, or just *"my vlog"* in it. A
vlog or podcast is framed on the face from the start.

### It keeps going when the AI provider does not

Ranking a long VOD is a dozen calls to one company's servers, and free tiers get
busy. A real run reached chunk 9 of 12 and started getting `503 UNAVAILABLE`,
with an 11.9 GB download and a 29-minute transcription already paid for.

Retrying Google does not fix Google being busy. So there is a fallback ladder:
**Gemini → Groq → OpenAI**, free before paid, switching on a spent quota *or* on
the retry budget running out. A [free Groq key](https://console.groq.com) takes a
minute and no card, and is the single best insurance for a long run.

### It speaks your language — and hears the right one

The interface ships in English, Hindi, Spanish, Portuguese, French, German and
Japanese, switchable in Settings. A fresh install is always English, and the
browser's locale is deliberately ignored — a machine set to another language
should not hand a first-run user an interface nobody chose.

Separately, the *spoken* language of the video is pinned to English by default
and changeable per run. Those are two different settings on purpose: you might
run an English interface over a Hindi stream.

### It watches the whole VOD so you don't have to

Transcribes the audio locally with faster-whisper, then ranks every moment for
what actually travels: hooks, revelations, opinion bombs, story peaks. You get
told which stage it's on, because a three-hour VOD is not a two-second wait.

<img src="assets/screenshots/02-progress.png" alt="A job mid-run, rendering its clips, with the log open showing the webcam overlay being located" width="880">

### Pause it when you need your machine back

Rendering takes every core it can get. If that makes the PC unusable, **Pause**
suspends the work where it stands — ffmpeg is stopped, not asked politely to
finish the current clip — and the CPU comes back immediately. Resume picks up
where it left off; nothing is lost and nothing is re-done. It holds
transcription and face tracking too, on the CPU or the GPU.

### Use the GPU — or don't

**Processor**, right under the live preview on the Create page, decides what
does the heavy work: **Automatic** (the
fastest thing this computer really has), **GPU**, or **CPU only**.

On Automatic, video is encoded on the graphics chip — NVIDIA, Intel, AMD or a
Mac's — which roughly halves render time, and transcription uses an NVIDIA GPU
when NVIDIA's CUDA libraries are there. Each is tested before it is used, so a
driver that claims more than it can do falls back to the CPU instead of
failing, and the panel says in plain words what is being used and why. If a
graphics driver keeps misbehaving, **CPU only** never touches it. The choice
applies from the next clip, even on a paused run.

<img src="assets/screenshots/06-processor.png" alt="The Processor box under the live preview: Automatic selected, video encoding on the NVIDIA GPU (NVENC), transcription on the NVIDIA GPU (CUDA)" width="880">

### Your clips folder is readable

Runs are filed under the **title of the video they came from**, with the date,
not a random id. Downloaded sources are named after the video too, with its
YouTube id in brackets so two videos with the same title stay apart.

The app also leaves a short note in that folder explaining what each part is,
which files are safe to delete, and which one to leave alone — because the big
downloads are the thing worth clearing out, and the manifest is the thing worth
keeping.

### Light or dark, your choice

The app was dark only. There is a switch in the top bar now, next to Settings:
a sun on the dark theme, a moon on the light one, showing where it will take
you rather than where you are.

It is remembered per machine, so the laptop and the desktop can disagree. A
machine that has never been told follows the system, and keeps following it —
so a Mac or a PC that switches itself to light at sunset takes the app along,
until you press the button once and make the choice yours.

### No windows flashing at you

ffmpeg is a console program, and a windowed app starting one makes Windows open
a console for it. A long render used to mean dozens of black windows blinking
open and shut. They are hidden now. If you saw those and wondered what they
were: that was the video tool doing the cutting, and hiding it was overdue.

### It updates itself

The app is one .exe people download once, so a fix that ships is a fix that has
to reach them. It asks GitHub for the newest release at launch, again every
half hour while it is open, and whenever you come back to the window — so a
build published at noon reaches someone who started work at nine, without
restarting anything.

When there is one, a banner offers it. The download is checked against the
SHA-256 GitHub publishes for that file, the running exe is replaced in place,
and the app reopens on the new version. Same folder, same filename, and the
build it replaced is deleted rather than left sitting on your disk. Nothing is
touched until the checksum matches, so a failed download leaves the working
app exactly as it was.

There is a **Check for updates** button in the top bar and in the sidebar, and
the version you are running is shown in all three places, for when something
has gone wrong and you need to say which build you are on.

The mac app checks on the same schedule but stops at telling you: it is a
bundle of hundreds of files signed as one unit, and replacing that under a
running process leaves a signature that no longer matches its contents — the
app macOS then refuses to open being the one the update was meant to deliver.
So it says a version is out, and you download it.

The Linux build updates itself exactly as Windows does — more easily, in
fact. Linux will replace a running binary outright, because the kernel is
holding the file rather than the name; Windows insists on the rename first.
It is done the same way on both anyway, so that the path which runs on
every update is the path that gets exercised.

> Builds before v1.5.0 were compiled before any of this existed and cannot be
> told about new versions. Those need one manual download — the last one.

### It tells you when it's finished

A long VOD is tens of minutes of work. Nobody watches that, so the window ends
up behind a game or minimised — and until now the only way to learn the clips
existed was to go back and look, which meant a run that finished at 2am was
found at nine.

When a run ends and you are not looking at the app, it raises a desktop
notification: a toast on Windows, Notification Centre on a Mac, and whatever
your desktop uses on Linux. When you *are* looking at it, it stays quiet,
because a notification for something already on your screen is just noise. It
knows which by asking the page every twenty seconds whether it is actually on
screen — and a window that has been closed stops answering, which is the same
answer.

Failed runs say so too, and name what went wrong.

### Clips come back ranked

Each card carries its score and the exact span it was cut from.

<img src="assets/screenshots/03-clips.png" alt="Finished clips from a Deadpool stream in a grid, each with its rank, score, length, the game it is filed under, and its title" width="880">

### The title knows what is on screen

Every clip comes back with a title, a description, tags and an on-screen hook.
Before any of it is written, the app looks at frames from the clip and works
out what it actually is — which game, or whether it is a podcast, a story told
to camera, a tutorial — so the Firewatch clip from a stream titled after The
Finals is filed under Firewatch. A game is only named when something on screen
or in the clip confirms it. When the app can only guess, the title says "this
horror game" rather than a name that might be wrong, and **What's in this
clip** lets you type the real one.

### Packaged for YouTube Shorts, Instagram Reels and TikTok

One clip gets posted to three apps, and they don't reward the same packaging, so
**Boost** has a tab for each:

- **YouTube Shorts:** five titles on different angles — the search phrase, the
  curiosity gap, the reaction, the exact detail, the stakes — plus two
  descriptions, all ranked by score, and scored tags to tap in and out.
- **Instagram Reels:** two captions with the hook and keywords inside the first
  125 characters, where Instagram cuts to "more", one written for search and one
  written to be sent to a friend. Plus hashtags, cover text for the 3:4 grid, and
  alt text.
- **TikTok:** a caption built around what people search, and hashtags without
  the `#fyp` filler.

Every option is scored by the AI and checked again by the app, which counts
things a model gets wrong: length, whether the subject is named where people will
see it, and hashtags that don't belong on that app. All of it is editable.
**Save changes** keeps your wording, and the mp4 on your PC is renamed to match
the new title, so what is in the folder is always what goes into YouTube's title
box.

<img src="assets/screenshots/05-boost.png" alt="Boost on a Firewatch clip, on the YouTube Shorts tab: what the clip is filed under, the title, and five ranked title options with their scores and angles" width="880">

<img src="assets/screenshots/07-reels.png" alt="Boost on the same clip, on the Instagram Reels tab: the caption with its character count, the note about the 125-character fold, two ranked caption options, and hashtags" width="880">

### Written from a playbook that keeps up with the algorithms

The packaging is written by an AI working as a social media strategist, from a
**growth playbook**: what YouTube Shorts, Instagram Reels and TikTok measure
right now (swipe-aways and replays, sends and saves, search) and what to do
about it. The playbook is a plain Markdown file,
[`assets/playbook/short-form-algorithms.md`](assets/playbook/short-form-algorithms.md).
When a platform changes how it ranks videos, that file is updated here, and
every copy of the app picks up the new version within a day, without a new
download. Keep your own notes instead by putting a `playbook.md` in the app's
settings folder.

### You can see it working

A long video used to sit at 0% for ten minutes with nothing to look at. Now
every stage reports itself:

- **Fetching** shows how much of the video has arrived, how fast it is coming
  and how long that leaves — "42% of 1.2GB at 8.4MB/s, 1m21s left".
- **Transcribing** counts through the audio — "42% — 14m21s of 34m12s" — which
  is the longest stage and used to be the most silent.
- **Ranking** and **Rendering** count their chunks and clips.
- Each stage animates while it is the live one, and the header shows **time so
  far and about how long is left**, worked out from how long this run has
  actually taken rather than a guess baked in months ago.

### If the app closes mid-run, carry on

Close the app, lose power, or have it crash halfway through a two-hour VOD,
and the next launch offers the run back: **"A run didn't finish… Resume"**.
Carrying on skips everything already done — the download, the transcript, and
the ranking chunk by chunk — so it picks up near where it stopped instead of
starting the whole thing again.

### When something fails, it tries again

A download that times out, a transcription that runs out of memory once, a
clip whose render trips over a locked file: each stage tries again by itself,
up to three times, before a run gives up. If it still fails, **Try again**
picks up where it stopped — nothing already downloaded, transcribed or ranked
is done twice — and a run that finished with a clip missing offers **Retry
failed clips**, which renders just those. Errors that trying again cannot fix,
like a wrong API key, fail straight away so you are not kept waiting for them.

### When a render fails, it says what failed

Open **Show the log** and a failed clip tells you what went wrong in the words
of the tool that failed — the video encoder's own complaint about your file,
not a number. Errors like a full disk are spelled out in English.

It cannot rescue a clip that will not render. It can hand you something worth
pasting into an [issue](https://github.com/kleZ799/clipmint/issues/new),
which is the difference between a bug that gets fixed and one that does not.

### Fix any cut without re-running anything

Click a clip and it opens in a player. Move the in and out points, mute it, save
it, or throw it away. Trimming re-cuts straight from the downloaded source, so
the span can **grow** as well as shrink — something you cannot do by trimming the
rendered file.

<img src="assets/screenshots/04-player-trim.png" alt="The clip player with the trim panel open, showing in and out handles" width="880">

---

## Get it

[**⬇ Download ClipMint.exe**](https://github.com/kleZ799/clipmint/releases/latest/download/ClipMint.exe) — 220 MB, Windows, self-contained.

[**⬇ Download ClipMint-macOS-arm64.zip**](https://github.com/kleZ799/clipmint/releases/latest/download/ClipMint-macOS-arm64.zip) — 165 MB, Apple Silicon. **Beta.**

[**⬇ Download ClipMint-linux-x86_64**](https://github.com/kleZ799/clipmint/releases/latest/download/ClipMint-linux-x86_64) — 278 MB, x86-64, self-contained. **Untested on a desktop.**

> ### ⚠️ Read this before you download the Mac build
>
> **It has never been run on a Mac.** I develop on Windows and don't own one.
> The Mac app is built and verified by GitHub's macOS runners — the build is
> signed, its version is checked, and the bundled ffmpeg is confirmed to run —
> but no human has ever double-clicked it.
>
> So: it might not start. If it doesn't, that is a bug in this project, not
> something you did wrong, and I want to hear about it —
> [open an issue](https://github.com/kleZ799/clipmint/issues/new) with
> what you saw and the contents of `~/Movies/ClipMint/app.log` if that
> file exists. That is how this stops being a beta.
>
> **Windows is the tested build.** If you have both machines, use that one.

Double-click it. On first run it asks for a [free Gemini API key](https://aistudio.google.com/apikey), which is stored only on your machine.

**On Windows and Linux, this is the only time you download by hand.** From v1.5.0 the app
updates itself: it notices new releases, checks them, and replaces itself in
place.

**On a Mac**, unzip the download and drag the app to Applications. Apple
Silicon only — an M1 or later; there is no Intel build. It is the same app
doing the same work, with two differences worth knowing before you start:
macOS will not open it on the first try (below), and it tells you about new
versions rather than installing them, so an update means downloading it again.

<details>
<summary><b>Using it on a Mac — the whole thing, step by step</b></summary>

**Expect step 4 to fail.** That is normal, it happens to every unsigned app,
and it is not the beta part.

**1. Download.** Take `ClipMint-macOS-arm64.zip` from the
[latest release](https://github.com/kleZ799/clipmint/releases/latest).
Apple Silicon only — an M1 or later. There is no Intel build.

**2. Unzip.** Double-click the zip. You get a `ClipMint` folder with
the app in it and `READ ME FIRST.txt` beside it — the same steps as these,
for when you come back to this in six months and the tab is long closed.

**3. Drag it to Applications.** It runs from anywhere, but Applications is
where updates and Spotlight expect it.

**4. Double-click it, and let macOS refuse.** It says the app is damaged, or
that Apple cannot check it for malware. Nothing is damaged.

**5. Allow it, once.** **System Settings → Privacy & Security**, scroll down
to the line about ClipMint being blocked, click **Open Anyway**, confirm.
It opens normally every time after this. (Right-click → Open, the old shortcut
for this, stopped working in macOS 15.)

**6. Wait for the first start.** It unpacks and loads the transcription models
before the window appears — the first launch after a reboot is the slowest.
There is no browser and no address bar; it is its own window.

**7. Paste a Gemini key.** It asks on first run.
[Get a free one](https://aistudio.google.com/apikey). It is stored only on your
machine, at `~/Library/Application Support/ClipMint/settings.json`.

**8. Give it a video.** Paste a YouTube URL, or drag a file straight into the
window. Paste a *channel* URL and it lists recent videos to pick from.

**9. Describe the layout in words** — "webcam bottom left, gameplay above it",
"just the speaker, filling the frame" — pick how many clips you want and how
long, and start it. Everything from here runs on your Mac: it downloads,
transcribes, ranks the moments, and cuts them to 9:16.

**10. Find the clips.** They land in `~/Movies/ClipMint/shorts/`, in a
folder named after the video, and the app's **Reveal** button opens Finder
right on them. Sources and transcripts go to `~/Movies/ClipMint/output/`.

**11. Updating.** The app tells you when a new version is out but cannot
install it — download the new zip and replace the app in Applications.

### What is different from the Windows build

- **Transcribing runs on the CPU**, and it is the slow step. There is no CUDA
  on a Mac, and the transcription engine has no Metal backend, so an M-series
  CPU is doing all of it. It works; a long VOD takes a while.
- **Files live in mac places** — `~/Movies/ClipMint` for output,
  `~/Library/Application Support/ClipMint` for settings — instead of
  `Videos` and `%APPDATA%`.
- **No self-update**, as above.
- Everything else is the same app: same ranking, same layouts, same editing,
  same pause button, and ffmpeg is bundled so there is nothing to install.

### If it doesn't open, or opens and does nothing

That part *is* the beta, and it is worth reporting. Two things help:

- `~/Movies/ClipMint/app.log` — the app writes startup errors here,
  including ones it has no window to show you in.
- Running it from Terminal, so errors print where you can see them:

  ```bash
  /Applications/ClipMint.app/Contents/MacOS/ClipMint
  ```

[Open an issue](https://github.com/kleZ799/clipmint/issues/new) with
either of those and your macOS version. I have no Mac to reproduce it on, so a
paste of the actual error is the whole difference between fixed and not.

**Why macOS blocks it at all.** The app is signed, but not by Apple —
notarising costs $99 a year and this is free software. Gatekeeper is reporting
a missing Apple signature, not a finding about the file. What you can check
instead: every line here is public, the app is built from this repository by
GitHub's own runners with a
[readable build log](https://github.com/kleZ799/clipmint/actions), and
each release publishes a SHA-256 for the file.

</details>

**On Linux**, make the download executable and run it. It is the same app doing
the same work, with two differences worth knowing first: it opens in your
browser instead of a window of its own, and it needs glibc 2.35 or newer.

<details>
<summary><b>Using it on Linux — the whole thing, step by step</b></summary>

**Nobody has run this on a desktop distribution yet.** It is built and
exercised under WSL, and the release workflow starts every published build
and fetches the interface out of it — which proves it unpacks, imports,
binds a port and serves. It does not prove a full render works on Fedora, or
that your file manager opens where it should. If something is wrong, that is
a bug here rather than something you did: [say so](https://github.com/kleZ799/clipmint/issues/new).

**1. Download.** Take `ClipMint-linux-x86_64` from the
[latest release](https://github.com/kleZ799/clipmint/releases/latest). x86-64 only — there is no ARM build.

`ClipMint-linux-README.txt` is published beside it and says everything
below. A separate file rather than an archive around the binary, because the
updater downloads that binary and swaps it into place — putting it in a
tarball would mean teaching the update path to unwrap one on every release, to
solve what a second file solves for nothing.

**2. Make it executable, and run it.** A download arrives without the execute
bit. That is normal and not something you did:

```bash
chmod +x ClipMint-linux-x86_64
./ClipMint-linux-x86_64
```

**3. Wait for the first start.** It unpacks itself into `/tmp` and loads the
transcription models before anything appears. Every launch unpacks again —
that is the price of being one file that can replace itself.

**4. It opens your browser.** Not a tab you have to go and find: it opens on
its own, at a `127.0.0.1` address that exists only on your machine. If nothing
opens — a server with no desktop, an SSH session — the address is printed in
the terminal and works from any browser on that machine.

**5. Paste a Gemini key.** It asks on first run.
[Get a free one](https://aistudio.google.com/apikey). It is stored at
`~/.config/ClipMint/settings.json` and goes nowhere else.

**6. Give it a video.** Paste a YouTube URL, or drag a file straight in. Paste
a *channel* URL and it lists recent videos to pick from.

**7. Find the clips.** They land in `~/Videos/ClipMint/shorts/`, in a
folder named after the video, and **Reveal** opens your file manager on them.
Sources and transcripts go to `~/Videos/ClipMint/output/`.

**8. Updating.** It notices new releases, checks them, and replaces itself in
place — the same as Windows. The download above is the only one you do by hand.

### What is different from the Windows build

- **It runs in your browser rather than in a window of its own.** pywebview's
  Linux backend is WebKit2GTK, and WebKit2GTK cannot be bundled and carried:
  its typelibs and two hundred shared libraries would all have to come along,
  and even then WebKit renders pages in a separate process it locates by a path
  compiled into the library when *your* distribution built it. A window that
  comes up blank is worse than no window, so the app opens something that
  works. Run it from source with `python3-gi` and `gir1.2-webkit2-4.1`
  installed and you get a real window, because there it uses your system's own.
- **Transcribing runs on the CPU.** The pip CUDA libraries unpack somewhere the
  dynamic linker was never told about, and a process cannot add to its own
  library path once it has started — so bundling them would ship two gigabytes
  that nothing is able to load. Windows can register those directories at
  runtime, which is the whole reason it gets the GPU and this does not.
- **Files live in Linux places** — `~/Videos/ClipMint` for output,
  `~/.config/ClipMint` for settings, instead of `Videos` and `%APPDATA%`.
- **Finished-run notifications need `notify-send`.** Most desktops have it
  already, as part of `libnotify-bin`; KDE's `kdialog` is used instead when
  that is what is installed. With neither, the app says so in its log and
  carries on — Windows and macOS both have a notifier that is always there,
  and Linux is the one platform where that cannot be assumed.
- Everything else is the same app: same ranking, same layouts, same editing,
  same pause button. ffmpeg is bundled and statically linked, so it does not
  care what your distribution ships or whether it ships one at all.

### What it needs

glibc 2.35 or newer — Ubuntu 22.04, Debian 12, Fedora 36, and anything after
them. No Python and no ffmpeg: both are inside.

One library, and only one: `libGL.so.1`, which OpenCV wants for the face
tracking. Every desktop already has it, so on a normal installation there is
nothing to do. On a headless box — a server you are running this on over SSH —
`sudo apt install libgl1` (or `dnf install mesa-libGL`) is the whole fix. It is
not bundled because a graphics library belongs to the machine's own driver
stack; shipping one would be shipping the wrong one.

Without it the app still starts, serves and downloads. It fails at the point it
first looks for a face, which is a confusing place to find out, so it is worth
installing up front if you are not on a desktop.

That floor is chosen rather than inherited. The release builds inside an Ubuntu
22.04 container and refuses to publish a build that asks for more, because
PyInstaller does not bundle libc — it links against whatever the build machine
had, and glibc only promises to work forwards.

</details>

<details>
<summary>Windows says it isn't safe — is it?</summary>

Windows shows **"Windows protected your PC"** because the exe is not
code-signed. Certificates cost a few hundred dollars a year and this is free
software; SmartScreen is reporting the missing signature, not a finding about
the file. It says the same about most independent software on release day.

What you can check instead of taking that on trust:

- **Every line is public**, and the exe is built from this repository by
  GitHub's own runners — the [build log](https://github.com/kleZ799/clipmint/actions)
  is readable by anyone.
- **Each release publishes a SHA-256** for the exe, and the app verifies it
  when updating itself.
- **It works with the network off.** Transcribing and ranking run on your
  machine. It contacts YouTube to fetch a video, your AI provider to rank
  moments, and GitHub to check for updates. Your video never leaves the PC.

To run it: **More info → Run anyway**.

</details>

<details>
<summary>First-run details</summary>

- **Windows will warn you.** The exe isn't code-signed, so SmartScreen shows *"Windows protected your PC"*. Click **More info → Run anyway**. On a Mac it is Gatekeeper instead — see above.
- **First launch is slow.** It's a single file that unpacks itself each time. The mac app is a normal bundle and starts faster.
- **Where things go.** The key lives at `%APPDATA%\ClipMint\settings.json`; clips go to `%USERPROFILE%\Videos\ClipMint`, changeable in Settings. On a Mac: `~/Library/Application Support/ClipMint/settings.json` and `~/Movies/ClipMint`.
- **ffmpeg is bundled**, so there is nothing else to install — on both platforms.
- **The downloadable .exe transcribes on the CPU.** The CUDA runtime is 2GB, and a single-file exe re-unpacks its whole payload on every launch — so bundling it would cost every user a slow start for something only NVIDIA owners can use. If you have an NVIDIA card and want the ~5x faster transcription, build the one-folder version from source: `pip install -r requirements-nvidia.txt` then `python build_exe.py` (CUDA is the default there; `--no-cuda` opts out).

</details>

Everything below is for running from source, which you only need if you want to
change how it works.

> 📚 **Want to understand the internals?** [HOW_IT_WORKS.md](HOW_IT_WORKS.md) is a
> study companion to this repo — the pipeline stage by stage, the ranking prompts,
> the three renderers, the job runner, the frontend, and why each is built the way
> it is. Written to be read end-to-end.
>
> 🎓 **Preparing to explain this to someone?** [CONCEPTS.md](CONCEPTS.md) covers the
> ideas rather than the files — the AI/ML and computer-science concepts this project
> actually uses, each anchored to a real decision in the code, plus the questions an
> interviewer is likely to ask about it.

---

## What I built

This began as a fork of [Anil-matcha/AI-Youtube-Shorts-Generator](https://github.com/Anil-matcha/AI-Youtube-Shorts-Generator) (MIT), a
command-line script that crops talking-head videos. That origin is why GitHub
lists its authors as contributors here — their commits are genuinely in this
repo's history, and the licence keeps them credited.

Rather than assert a boundary, here is the measured one. `git blame` over every
text file in the current tree, 25,072 lines (re-measured at v1.12.0):

| | Lines | Share |
|---|---:|---:|
| **Parth Bhadana** | **23,659** | **94.4%** |
| Anil Matcha (base) | 1,097 | 4.4% |
| Arael Espinosa | 194 | 0.8% |
| LathissKhumar | 122 | 0.5% |

Code only, excluding documentation: **93.7%** mine. Since the fork point
(`c30376e`, 29 Jul 2026): **198 commits**, **+23,648 / −429 lines**, and
**64 of the 85 files** now in the repo did not exist before.

Run `git blame` yourself — that is rather the point of quoting a number instead
of a claim.

The boundary is easy to draw. **Upstream gave a CLI that face-crops a single
speaker. Everything that makes this a stream tool, and everything that makes it
an application, is mine:**

**The application** — none of this existed upstream
- A **desktop app**: a FastAPI server on a free port, run from a background thread, behind a native WebView2 window. No browser, no address bar, no terminal.
- A **job runner** — queued work, one CPU-bound job at a time, progress streamed to the browser over SSE.
- A **clip editor** — re-cut, mute, save or delete a finished clip without re-running the pipeline.
- **Stages that retry themselves**, and a Try again that resumes a failed run from its caches.
- The **interface**, built on YouTube's own layout so the audience already knows how to use it.
- A **single-file Windows build** with ffmpeg bundled, so a non-technical user installs nothing.

**The stream intelligence** — upstream ranks any talking-head video; this one understands streams
- **`local/gaming_layout.py`** — the entire webcam-over-gameplay renderer: the overlay located from twenty frames across eight minutes (the face the samples agree on, the border that persists), a crop fitted inside it, single-pass ffmpeg `vstack`.
- **`vision.py`** — frames from every clip shown to a vision model, so a variety stream's clips are filed under the game actually on screen, and a game is named only when something confirms it.
- **Ranked packaging for three apps** — five titles and two descriptions per clip for YouTube Shorts, two captions with cover and alt text for Instagram Reels, and a TikTok caption, each scored on a rubric and checked in code, written from a growth playbook that updates without a release.
- **Kinds of video** — a stream, a vlog, a podcast and a tutorial each ranked by their own rules, detected from the video or chosen by the user.
- **A planned camera path** for full-frame face cams — dead zone, zero-lag easing, cuts kept as cuts — instead of a crop chasing a jittery detector.
- **`faces.py` and `accel.py`** — YuNet face detection with Haar underneath, and a Processor setting that test-encodes each GPU encoder before trusting it and checks for CUDA's libraries before starting on the GPU.
- **`STREAM_VIRALITY_CRITERIA`** — a ranking prompt that separates streamer speech from game narration on one mixed track, and refuses any clip without the streamer in it.
- **Natural-language layout parsing** — "webcam top, 5 clips" or "cut 14:45 to 15:30" resolves to a render spec, with an exact-span path that skips transcription and ranking entirely.

**The bugs that made it actually work**
- **Chunk timestamp rebasing** — long videos returned *zero* highlights before this; every chunk past the first had its timestamps clamped away.
- **High-resolution clipper fixes** — non-contiguous OpenCV slices, Windows file-handle races, downscaled Haar detection.
- **Gemini support** — provider dispatch, a token budget that survives the model's internal reasoning, and 429 backoff that reads the server's own retry hint.
- **`opencv-python<5` pin** — 5.x removed `CascadeClassifier`, which the face tracking depends on.

---

## The problem this solves

I stream story games and post Shorts. The math of that is brutal: a 35-minute session has maybe five clippable moments in it, and finding them means scrubbing the whole VOD twice.

Worse, every off-the-shelf clipper fails on stream footage for the same two reasons:

1. **They crop to the wrong thing.** Auto-croppers slide a vertical window around hunting for a face. On a stream, the biggest face on screen is usually a *game character* — so the clip ends up centred on a cutscene with my commentary playing over it from off-frame.
2. **They can't tell me from the game.** A story game's audio is narration, dialogue, and score, all mixed onto the same track as my mic. Generic highlight detection happily hands back 45 seconds of beautifully-written game narration with zero streamer in it. That's not my content. That's the studio's.

This fixes both.

---

## The layout that actually works

Every clip renders as **webcam over gameplay**, because that's the format that survives a vertical crop:

```
┌──────────────────────┐
│                      │
│       WEBCAM         │   42% — auto-located, cropped to
│    (your reaction)   │        head-and-shoulders
│                      │
├──────────────────────┤
│                      │
│                      │
│      GAMEPLAY        │   58% — centre crop, nudged away
│                      │        from the webcam corner
│                      │
│                      │
└──────────────────────┘
        1080 × 1920
```

The webcam isn't a hardcoded rectangle. Each clip gets its overlay **located from scratch**, using the one thing that tells an overlay from a game — it doesn't move:

- Sample ~20 frames: six inside the clip, the rest from the minutes around it, so the game underneath changes completely while the overlay stays put
- Faces are found with **YuNet**, a small learned detector that ships inside the app — it catches turned heads, dim rooms and headsets that the old detector missed
- The streamer is the face that **keeps turning up in the same place** — a face in the game shows up once and is outvoted
- The overlay's **border** is an edge that is there in every sample, with moving game outside it and a still room inside
- Crop head-and-shoulders **inside that border**, at the panel's own shape — no gameplay and no letterbox bars in the webcam panel

Then the whole thing renders in **one ffmpeg pass** — crop, crop, scale, `vstack`. No per-frame Python. Clips render in seconds instead of minutes.

If the face fills the frame, there is no overlay — it's a podcast or a just-chatting segment — and that clip gets the face-following crop instead, which holds still until you actually move and then eases after you. If no face turns up anywhere, it falls back to a centre crop and says so in the log rather than silently shipping garbage.

---

## How a VOD becomes Shorts

```mermaid
flowchart LR
    A[VOD<br/>URL or local file] --> B[yt-dlp<br/>cached by video id]
    B --> C[faster-whisper<br/>cached as .srt]
    C --> D{over 30 min?}
    D -->|yes| E[chunk: 20 min<br/>60s overlap]
    D -->|no| F[LLM ranking<br/>stream-aware prompt]
    E --> F
    B --> J[loudness envelope<br/>spikes, silences, peaks]
    J --> F
    F --> G[snap to sentences<br/>hook first, length enforced]
    G --> K[dedupe<br/>drop >50% overlap]
    K --> V[vision: 4 frames per clip<br/>what is on screen]
    V --> S[5 ranked titles, tags<br/>filed under the real game]
    S --> H[ffmpeg vstack<br/>webcam over gameplay]
    H --> I[hook cold open<br/>-14 LUFS]
    I --> L[titled mp4s<br/>1080×1920]
```

Four stages, and **every expensive one is cached.**

### 1. Get the file

Already have the VOD on disk? Pass the path — it's used as-is, nothing downloads. Otherwise yt-dlp grabs it as `source_<videoid>.mp4`, and if that id is already in `output/` it gets reused. Reruns don't re-download.

### 2. Transcribe

faster-whisper, on your CPU (`int8`) or GPU (`float16`) — auto-detected. The transcript is cached beside the video as an `.srt`, validated by modification time.

**The spoken language is pinned to English by default**, changeable per run in the Render panel, with an explicit `auto` for genuinely mixed sources. This matters more than it sounds: left on auto-detect, whisper drifts on game audio and music beds and starts emitting fluent nonsense in a language nobody spoke. One 3h47m English stream came back with 703 of its 1097 cues in hallucinated Korean — and one of those cues became a clip title.

**GPU detection asks CTranslate2, not torch.** faster-whisper runs on CTranslate2; torch is not installed and is explicitly excluded from the build, so probing `torch.cuda.is_available()` silently sent every machine down the CPU path. If you have an NVIDIA card, `pip install -r requirements-nvidia.txt` — the packaged one-folder build already ships them. On Linux the libraries also have to be on `LD_LIBRARY_PATH` before Python starts; that file shows how. Measured on an RTX 5060 (8GB): 900s of audio with the `small` model, **104.2s on CPU → 20.8s on CUDA**.

**This is the slowest step in the pipeline and you pay it exactly once per VOD.** Every re-rank and re-render after that is free.

### 3. Rank the highlights

First it settles what kind of video this is — **stream, vlog, podcast, tutorial
or other** — from the Kind of video picker, the prompt's own words, or a quick
read of the start, middle and end of the transcript plus the listing. Each kind
has its own rules for what counts as a good moment. The rest of this section is
the stream rules, which are the most specialised.

The model is told, explicitly, that it's reading a single mixed audio track with no speaker labels, and taught to separate the two voices by register:

> **Game narration** reads like written prose — literary, past tense, polished, no filler words, never addresses anyone.
>
> **The streamer** sounds spoken — reactions, false starts, laughter, swearing, questions, talking to chat.

And then the hard rule: **every highlight must contain the streamer's own speech.** A story beat only counts when you react to it, talk over it, or respond after it.

Ranking prioritises, in order: reactions to story beats → raw unscripted spikes → fails and disasters → hot takes → chat interaction → personal tangents → quotable one-liners → sincerity. Dead air, loading screens, and stream housekeeping are explicitly skipped, and clips start *on* the hook rather than the run-up — a Short is judged in its first second, so the opening line has to earn the watch by itself.

**But a transcript cannot hear anything.** The model reads words on a page; it
never meets the scream, the laugh, or the half-second of silence before the
punchline, and two moments that read identically can be worlds apart in the
audio. So the source's loudness is measured too — once per run, streamed
through ffmpeg into one number per quarter-second — and four measured signals
are folded into the rank alongside the model's opinion:

| Signal | What it catches |
|---|---|
| **Audio spike** | The peak of the clip against the video's *own* baseline, so one loud scene can't make every span look like a hook |
| **Trigger phrases** | A short fixed list — *no way*, *wait for it*, *I can't believe* — weighted by how hard each lands, and counted double in the opening line |
| **Silence-to-peak** | A quiet beat right before the spike. Build-up → payoff reads as a moment; a flat loud run-up reads as noise |
| **Dialogue density** | Words per second in the first two seconds. Below the floor is dead air, which is the single most reliable way to lose a viewer |

How much those move a rank is scaled by how much of them was actually
measurable, so a video with no readable audio leans on the model rather than on
one keyword list. Every clip keeps its own numbers in `job.json` beside it —
the model's score, the measured one, each sub-signal — so the weights can one
day be corrected against real retention instead of being trusted forever.

**Then the span is snapped to something real.** Ask for 30-second clips and a
model hands back 19s, 24s, 47s: it is estimating durations from timestamps it
half remembers while also writing JSON. Length is arithmetic, so it is done in
code — the span is walked out to whole transcript segments until it lands in
the band you asked for, aiming near the middle when the model stopped short and
keeping as much of the payoff as fits when it ran long. Snapping to sentence
boundaries is what stops that from cutting mid-word. The opening is placed the
same way: by finding the model's quoted hook line in the transcript rather than
trusting the timestamp it paired with it, which routinely lands seconds early on
the throat-clear before it.

Long VODs get chunked into 20-minute windows with 60s of overlap, each rebased to zero and offset back afterward. Anything overlapping >50% with a higher-scoring pick is dropped, so you never get two near-identical clips.

Every clip comes back with a score, a title, and a one-line reason it should work.

### 4. Render

Before anything renders, four frames from each chosen clip are shown to a vision model, which says what the clip actually is — which game, or whether it is a podcast or a story told to camera. The packaging is written for that: five ranked titles, two ranked descriptions and scored tags for YouTube Shorts, then Reels captions, cover and alt text and a TikTok caption, all from the growth playbook.

Cut and stack in one ffmpeg pass, straight to 1080×1920 h264 with `+faststart`. Upload-ready for Shorts, Reels, and TikTok with no server-side re-encode. The webcam panel is cropped inside your overlay's own border, found from the minutes around each clip; a clip whose camera fills the whole frame gets the face-following crop instead.

Two things happen on the way out. Audio is normalised to **-14 LUFS**, the
target all three platforms mix toward — a Short that plays quieter than the one
before it in the feed reads as lower production value before a word of it is
heard. And a clip whose loudest moment lands late opens with **a second of that
moment first**, then plays in full: the payoff arrives before the viewer has
decided whether to stay. It skips itself when the peak is already at the front,
where a replay would just be a stutter, and the length it adds is reserved
before the cut is chosen — so 30-second clips are still 30 seconds with it on.
Turn it off with the switch in the Render panel, or by writing *no hook repeat*
in the prompt.

---

## Quickstart

**Prerequisites:** Python 3.10+, `ffmpeg` on your PATH, and a [free Gemini API key](https://aistudio.google.com/apikey).

```bash
git clone https://github.com/kleZ799/clipmint.git
cd clipmint
python -m venv venv
venv\Scripts\activate
pip install -r requirements-local.txt
```

On macOS or Linux, activate with `source venv/bin/activate` instead. That is the
command-line tool; for the app itself, use your platform's file below.

### Which requirements file

| You want | Install |
|---|---|
| The app from source, on **Windows** | `requirements-windows.txt` |
| The app from source, on **macOS** | `requirements-mac.txt` |
| The app from source, on **Linux** | `requirements-linux.txt` |
| Transcription on an **NVIDIA GPU** (Windows, Linux) | add `requirements-nvidia.txt` |
| To **build** the app with PyInstaller | add `requirements-build.txt` |
| The command-line tool only, `--mode local` | `requirements-local.txt` |
| The command-line tool only, `--mode api` | `requirements.txt` |

Add-ons go on the same line: `pip install -r requirements-windows.txt -r requirements-nvidia.txt`.
Each platform file also lists, at the top, what pip cannot install for you:
ffmpeg everywhere, the WebView2 runtime on older Windows 10, GTK and WebKit for a
native window on Linux, and `libGL` on a Linux server with no desktop.

Copy `.env.example` to `.env` and fill it in:

```ini
LLM_PROVIDER=gemini
GEMINI_API_KEY=your_key_here
GEMINI_MODEL=gemini-3.6-flash
LOCAL_WHISPER_MODEL=base
LOCAL_WHISPER_DEVICE=auto
LOCAL_OUTPUT_DIR=output
LOCAL_OUTPUT_RESOLUTION=1080x1920
```

Then point it at a VOD:

```bash
python main.py "https://www.youtube.com/watch?v=YOUR_VOD" --mode local --num-clips 5 --format 1080 --output-json result.json
```

Or run it against a file you already have:

```bash
python main.py "D:/streams/session-14.mp4" --mode local --num-clips 5
```

Clips land in `output/` as `short_01.mp4` … `short_05.mp4`, alongside a `result.json` holding the full transcript, every candidate considered, and the winning picks.

---

## Using it

**Running from source:**

```bash
pip install -r requirements-windows.txt     # or requirements-mac.txt / requirements-linux.txt
python desktop.py
```

Prefer it in a browser instead? `python -m webapp` serves it at http://127.0.0.1:8000.

**Building the executable yourself:**

```bash
pip install -r requirements-windows.txt -r requirements-build.txt
python build_exe.py --onefile --clean
```

Put `ffmpeg.exe` and `ffprobe.exe` in a `./bin` folder first and they get bundled,
which is how the published build needs nothing installed. That's what makes it
220 MB; without them it's 153 MB and ffmpeg has to be on the user's PATH. The
YuNet face model in `assets/models` and the growth playbook in `assets/playbook` are bundled either way. Drop
`--onefile` for a folder build that starts faster but has to be zipped to share.

On a Mac the same command without `--onefile` produces `dist/ClipMint.app`:
the icon is rendered from `assets/icon.png`, the version is written into
Info.plist, and the bundle is signed ad-hoc so Apple Silicon will run it at all.
Put static `ffmpeg` and `ffprobe` binaries in `./bin` — a Homebrew ffmpeg links
against dylibs in `/opt/homebrew` and would only work on your own machine.
PyInstaller cannot cross-compile, so the published mac build is made on a macOS
runner by [the release workflow](.github/workflows/release.yml), which is also
where those two download URLs live.

On Linux the same `--onefile` command produces `dist/ClipMint`, with no
extension. Put static `ffmpeg` and `ffprobe` in `./bin` — a distribution's own
ffmpeg links against that distribution's libraries and would only run on your
own machine, the same trap as Homebrew on a Mac. No webview backend is bundled,
so the built app opens a browser; `python desktop.py` from source with
`python3-gi` installed opens a real window instead.

Whichever glibc you build against becomes the oldest one your build will run
on — and the binary will not tell you which that is. `objdump` on a one-file
build reports what the bootloader needs, which is nothing much; the libraries
that set the real floor are compressed inside it and invisible until it runs.
Measured on Ubuntu 26.04, the bootloader claimed 2.14 while the payload wanted
2.43. So the release workflow builds inside an Ubuntu 22.04 container and reads
the floor off the unpacked payload while a copy of the app is running, that
being the only moment the payload exists.

**Drop a file or paste a link.** Drag a VOD straight in, or paste a YouTube URL. Paste a *channel* link and it lists the 12 most recent videos as a grid to pick from.

**Clips come out at the source's real quality.** The renderer measures the crop it's actually going to take and picks the highest standard size that crop genuinely supports — a stacked 1440p stream renders at 1440×2560 rather than being flattened to 1080p. It won't upscale past what the footage holds, because inventing pixels only grows the file.

**Describe the layout in plain English.** A live preview redraws as you type — the real frame shape, the real webcam panel height — so you can see your words land before spending a single second of render time:

| Type this | You get |
|---|---|
| `30 second clips` | the length every clip is cut to — enforced in code, not requested of the model |
| `no hook repeat` | turn off the cold open that puts the payoff first |
| `webcam at the top` | the stacked layout |
| `my webcam is bottom right` | which corner to hunt for your overlay |
| `square, bigger webcam` | 1:1, panel at 55% |
| `gameplay only, no webcam` | plain centre crop |
| `follow my face` | face-tracking crop |
| `3 clips` | how many to make |
| `cut 14:45 to 15:30` | **exact span, no AI ranking** |
| `my vlog` / `podcast` / `tutorial` | what kind of video it is: decides what counts as a good moment, and frames a vlog or podcast on the face |

Combine them freely — `cut 14:45 to 15:30, gameplay only, square` does all three.

Under the prompt, **Shape** and **Kind of video** do the same jobs with a click, and a click beats the words.

Parsing is keyword-first and runs in about 70ms, so the preview keeps up with typing and costs no quota. Only genuinely novel phrasing falls through to the LLM.

### Naming an exact span

Give it a timecode and it **skips transcription and ranking entirely** and cuts exactly what you asked for. `14:45 to 15:30`, `1:30-2:45`, `00:14:45 - 00:15:28`, `885s to 928s`, or several at once with `14:45-15:30 and 24:55-25:40`.

This is the fast path: no Whisper, no LLM, straight to ffmpeg. Seconds instead of minutes, and it costs nothing. Use it when you already know where the moment is — which, after you've watched your own stream, is most of the time.

Jobs run one at a time on a background worker, because Whisper and ffmpeg are both CPU-bound and racing them makes both slower. Progress streams live with the pipeline log.

> Everything runs on your machine and binds to localhost only. Your VODs are never uploaded anywhere — the only thing that leaves is the transcript text sent to the ranking model, and even that is skipped entirely when you name an exact span.
>
> If you serve it to your network with `python -m webapp --host 0.0.0.0`, note there's no authentication and every job spends **your** API quota and **your** CPU.

---

## The workflow I actually use

Ranking and rendering are separate on purpose, because they fail for different reasons and cost different amounts.

**First pass** — transcribe, rank, render. Slow, once per VOD.

**Then read the picks.** They're plain JSON with timestamps. Nudge a start time back three seconds, drop the one that didn't land, retitle the good ones.

**Re-render from the edited list** — with no LLM calls at all. On Gemini's free tier this matters: re-ranking a long VOD burns quota, and once you've hand-picked five timestamps, re-ranking is pure waste. The rate-limit handler parses the `retry in Xs` hint out of a 429 and honours it rather than dropping the whole run — but the best fix is not making the call.

One trick worth stealing: **transcribe from the 720p download, render from a 1440p one.** Whisper doesn't care about resolution and CPU transcription is the bottleneck, so you get cheap transcription and a sharp render out of the same session.

---

## Tuning

The knobs that change output quality most, in order:

| Knob | Where | What it does |
|---|---|---|
| Kind of video | The picker under the prompt, or `--kind` on the CLI | Stream, vlog, podcast, tutorial or other. **The single biggest lever** — each kind is ranked by its own rules. Leave it on *Work it out* and the app decides |
| `CRITERIA_BY_KIND` | `shorts_generator/highlights.py` | The rules each kind of video is ranked by. Edit one to change what the app thinks is worth clipping in that kind of video |
| Growth playbook | `assets/playbook/short-form-algorithms.md`, or your own `playbook.md` in the settings folder | What the packaging writer knows about how Shorts, Reels and TikTok rank videos. Raise the `reviewed:` date when you change it |
| `DESCRIPTION_OPTIONS` / `CAPTION_OPTIONS` | `shorts_generator/seo.py` | Descriptions and Reels captions written per clip to choose from, `2` each |
| `corner` | `local/gaming_layout.py` | Which corner your webcam overlay sits in. `bottom-left` by default |
| `CAM_PANEL_FRACTION` | `local/gaming_layout.py` | Webcam panel height, `0.42` by default |
| `FACE_CONTEXT_MULTIPLE` | `local/gaming_layout.py` | Webcam zoom. Lower is tighter on your face |
| `MAX_CLIP_SECONDS` | `shorts_generator/highlights.py` | Hard reject above 90s. The prompt separately targets 18–35s, because the completion bar gets stricter the longer a clip runs |
| `MODEL_WEIGHT` | `shorts_generator/signals.py` | How much of the rank is the model's opinion versus the measured audio. `0.62` by default — lower it if the ranking keeps picking moments that read well and land flat |
| `TRIGGER_PHRASES` | `shorts_generator/signals.py` | The reaction phrases that score as a hook, weighted. Add the ones **you** actually say |
| `TARGET_BY_KIND` | `shorts_generator/boundaries.py` | Clip length per content type, when the prompt names no length of its own |
| `REPLAY_SECONDS` | `shorts_generator/hook_open.py` | How long the hook cold open runs, `1.9s` by default |
| `CONTEXT_SECONDS` | `local/gaming_layout.py` | How far either side of a clip to look when locating your webcam overlay, `240s` by default. Lower it if your layout changes often mid-stream |
| `DEAD_ZONE` / `EASE_SECONDS` | `local/clipper.py` | Face-follow: how far you can move before the frame follows (`12%` of it), and how gently it eases after you (`0.45s`) |
| `FRAMES_PER_CLIP` | `shorts_generator/vision.py` | Frames shown to the vision model per clip, `4` by default |
| `TITLE_OPTIONS` | `shorts_generator/seo.py` | Titles written per clip for you to choose from, `5` by default |
| `STAGE_ATTEMPTS` / `CLIP_ATTEMPTS` | `webapp/jobs.py` | How many times a stage, or one clip's render, is tried before a run gives up, `3` each |
| Processor | Under the live preview, or `PROCESSOR` in `.env` | `auto` (default), `gpu` or `cpu` — what encodes video and runs transcription. `LOCAL_WHISPER_DEVICE`, if set, still pins transcription |
| `SCORE_THRESHOLD` | `shorts_generator/faces.py` | How sure YuNet must be to count a face, `0.7`. Raise it if a crop keeps catching faces in the game |
| Provider | Settings | Gemini, Groq or OpenAI. Add a **free Groq key** as a fallback so a busy Gemini cannot end a run |
| `LOCAL_WHISPER_MODEL` | `.env` | `base` is plenty for ranking. `small` reads better and hallucinates less — and on a GPU it is *faster* than `base`, so use it if you have one |
| Spoken language | Render panel | English by default. Pinning it is the fix for whisper inventing text in another language |
| Interface language | Settings | English, Hindi, Spanish, Portuguese, French, German, Japanese |

---

## Two modes

| | `--mode local` | `--mode api` |
|---|---|---|
| **Download** | yt-dlp | MuAPI |
| **Transcription** | faster-whisper, on your machine | MuAPI Whisper |
| **Ranking** | Gemini or OpenAI, your key | MuAPI |
| **Cropping** | ffmpeg + OpenCV, your machine | MuAPI auto-crop |
| **VODs leave your machine?** | Only the transcript text | Yes, the whole video |
| **Cost** | Free tier covers a lot | Per-call |

Local mode is what this repo is built around. API mode is inherited from upstream and still works if you'd rather not run anything locally.

---

## Under the hood

### The app

```mermaid
flowchart TB
    W[WebView2 window<br/>no browser, no address bar] -->|http| S[FastAPI<br/>127.0.0.1, free port]
    S -->|enqueue, return now| Q[Job queue<br/>one worker thread]
    Q --> P[pipeline<br/>download - transcribe - rank - render]
    P -.->|stdout parsed into stages| Q
    Q -.->|SSE, one event per change| W
    S --> E[clip editing<br/>trim / mute / save / delete]
```

**Requests never block on the pipeline.** Transcribing alone outlives any sensible
HTTP timeout, so `POST /api/jobs` only ever enqueues and returns an id; the
browser follows along over server-sent events. Jobs run **one at a time on a
single worker thread** on purpose — Whisper and ffmpeg are both CPU-bound, and
running two at once makes both slower than running them in sequence.

**Progress is derived, not guessed.** The pipeline already narrates itself to
stdout, so the runner captures it line by line, maps prefixes like `[transcribe]`
or `[stack] 2/5` onto stages, and gives each stage a band of the bar. A render
that reports `3/5` moves the bar to the right place inside the render band
without the pipeline knowing a UI exists.

**Updating replaces the running exe with itself.** Windows will not let a
running .exe be overwritten, but it will let it be *renamed* — so the running
file is moved aside, the verified download takes its name, and the app
relaunches from the same path. The rename happens only after the SHA-256
matches, so a bad download never becomes the thing that runs; if the second
move fails the original is put straight back. The replaced build cannot be
deleted immediately — the process that was running it is still shutting down
and still holding it open — so cleanup retries in the background until the
handover completes.

The page never handles a download URL. It asks the server to install *the*
update, and the server resolves what that means from the repository compiled
into the build, so nothing rendered in the window can aim the updater at a file
of its choosing.

**Editing re-cuts from the source, not the render.** Trimming a clip re-runs the
renderer over the original download with new timestamps, which is why the span
can grow as well as shrink — trimming the rendered file could only ever remove.
Clip filenames are resolved against the job's own directory and rejected if they
escape it.

### The engine

Both modes share one highlight engine. They agree on a single transcript shape:

```python
{"duration": 2130.0, "segments": [{"start": 12.4, "end": 15.1, "text": "..."}]}
```

Whichever transcriber ran, `highlights.py` can't tell the difference. The LLM is injected the same way — `get_highlights(transcript, llm_fn=...)` takes *the function that calls a model* as an argument, so swapping Gemini for OpenAI for MuAPI touches one line, and the ranking logic stays a single copy that can't drift.

```
shorts_generator/
├── pipeline.py            # orchestrator — picks local vs api
├── highlights.py          # the brain: prompts, chunking, dedupe
├── signals.py             # loudness envelope + trigger phrases → measured hook score
├── boundaries.py          # snap spans to sentences; enforce the length asked for
├── hook_open.py           # the cold open that puts a late payoff first
├── content_kinds.py       # stream / vlog / podcast / tutorial / other
├── vision.py              # what each clip shows, from its frames
├── seo.py                 # Shorts, Reels and TikTok packaging, all ranked
├── playbook.py            # the growth playbook: bundled, fetched daily, overridable
└── local/
    ├── downloader.py      # yt-dlp + download cache
    ├── transcriber.py     # faster-whisper + .srt cache
    ├── llm.py             # Gemini / Groq / OpenAI, text and vision, with backoff
    ├── clipper.py         # face-following crop on a planned camera path
    └── gaming_layout.py   # webcam-over-gameplay stack (streams)
```

`highlights.py` is still the only place that talks to a model about ranking.
What changed is that its answer is no longer the last word: `finalize()` runs
the model's candidates through `boundaries.refine()` and then `signals.rescore()`
before deduping, so the spans that reach the renderer are ones snapped to real
sentence boundaries and ranked partly on what the audio did.

## Staying in sync with upstream

This repo is standalone, but it keeps a link back to the project it grew out of, so upstream fixes can be pulled in whenever they're worth having.

One-time setup after cloning:

```bash
git remote add upstream https://github.com/Anil-matcha/AI-Youtube-Shorts-Generator.git
```

Then, whenever you want upstream's changes:

```bash
git fetch upstream
git merge upstream/main
```

Conflicts, when they happen, land almost entirely in `highlights.py` — upstream edits the generic virality prompt while this repo runs the stream-aware one. **Keep `CRITERIA_BY_KIND` routing each kind to its own criteria** and take upstream's changes everywhere else. `local/gaming_layout.py` doesn't exist upstream, so it never conflicts.

---

## License

MIT — see [LICENSE](LICENSE). Upstream work © Anil Chandra Naidu Matcha; modifications © Parth Bhadana.

The published `ClipMint.exe` also carries ffmpeg and ffprobe (the
[gyan.dev](https://www.gyan.dev/ffmpeg/builds/) essentials build), which are licensed under the
GPL v3 — not MIT. That covers the bundled binaries only; this repository's own
source stays MIT, and building from source pulls in no ffmpeg at all.

---

## Credits

**[Anil Chandra Naidu Matcha](https://github.com/Anil-matcha)** — the original
project this forks. The CLI pipeline and the highlight-ranking idea are his, and
1,175 of his lines survive here. Worth being precise about where: the largest
blocks are in `highlights.py`, `local/clipper.py`, `pipeline.py` and `muapi.py`.

**[Arael Espinosa](https://github.com/cl8dep)** — *"add gemini local llm and
local caches"*. 87 lines in `local/transcriber.py`, 65 in `local/downloader.py`,
29 in `local/llm.py`. That commit is the seed of local mode: the Gemini path
this app still runs on, and the one the Groq fallback was later built beside.

**LathissKhumar** — two commits hardening local mode: 77 lines making the
highlight JSON parsing survive bad model output, plus the VAD-off default and a
CUDA fallback in `local/transcriber.py`. The VAD default is still what ships.

Their work is in this repository because it earned its place, and the MIT
licence keeps their names on it. Everything else — the desktop application, the
stream-aware ranking, the renderers, the job runner, the interface, the
packaging — is mine.

---

## Author

**Parth Bhadana**

[YouTube](https://www.youtube.com/@ParthBhadana799) &middot; [GitHub](https://github.com/kleZ799) &middot; [LinkedIn](https://www.linkedin.com/in/parth-bhadana-530014202/) &middot; [Discord](https://discord.gg/jnMrGbBz3m)

Built and maintained by me. If you use it, fork it, or ship anything based on
it, the MIT licence asks one thing in return: keep the copyright notice.

Repository: <https://github.com/kleZ799/clipmint>
