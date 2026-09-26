"""Background job runner for the web UI.

The pipeline takes minutes, not milliseconds — transcription alone can run
longer than any sensible HTTP timeout. So a request only ever *enqueues* work;
progress is streamed separately over SSE.

Jobs run one at a time on a single worker thread. That's deliberate: Whisper
and ffmpeg are both CPU-bound, and running two at once makes both slower than
running them in sequence.

Finished jobs are written to disk as a `job.json` beside their clips, and read
back at startup. Without that, the store was purely in-memory: close the app
and every clip you had ever made became unreachable through the UI even though
the mp4s were still sitting in the folder.
"""
import contextlib
import io
import json
import os
import queue
import re
import threading
import time
import traceback
import unicodedata
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from shorts_generator import proc, scorecard
from shorts_generator.layout_spec import LayoutSpec

from shorts_generator import user_config
from . import notify

# Written into each job's folder so the run can be reconstructed later.
MANIFEST_NAME = "job.json"
CLIP_SUFFIXES = (".mp4", ".mkv", ".webm", ".mov")


def _title_from_filename(name: str) -> str:
    """A readable title for a clip we only know by its filename.

    "short_03.mp4" is what an older run left behind; "Clip 3" is what a person
    can actually pick out of a grid.
    """
    stem = os.path.splitext(name)[0]
    m = re.fullmatch(r"(?:short|edit)_(\d+)(?:_\d+)?", stem)
    if m:
        return f"Clip {int(m.group(1))}"
    # Clips are named after their own title now, so the stem usually *is* the
    # title and re-casing it would only damage it. Only the underscore-joined
    # shape older runs left behind still needs unpacking.
    if "_" in stem and " " not in stem:
        return re.sub(r"[_-]+", " ", stem).strip().title() or stem
    return stem


# Long enough to stay recognisable, short enough that the whole path survives
# Windows' 260-character limit once it sits inside a job folder.
MAX_STEM_CHARS = 80

# Reserved device names on Windows. A file called "con.mp4" cannot be created.
_RESERVED_STEMS = {"con", "prn", "aux", "nul",
                   *(f"com{i}" for i in range(1, 10)),
                   *(f"lpt{i}" for i in range(1, 10))}

# Punctuation worth keeping in a filename. Everything outside this and
# alphanumerics goes, which drops the emoji the titles are written with and the
# characters Windows forbids (\ / : * ? " < > |) in the same pass. `#` and `%`
# are excluded deliberately too: the filename becomes a URL path segment, and
# a `#` there truncates it into a fragment.
_KEEP_PUNCT = set(" -_'(),.!&+")


def safe_stem(title: str, fallback: str = "clip") -> str:
    """A filesystem-safe filename stem from a clip's generated title.

    Keeps alphanumerics from any script -- isalnum() is Unicode-aware, so a
    Hindi or Japanese title survives -- and a little punctuation, then drops
    everything else rather than trusting the model not to return a slash.
    """
    # Combining marks have to survive alongside the letters they attach to:
    # they are not alphanumeric on their own, and dropping them turns Hindi's
    # "क्या" into "क य" -- every Indic, Thai and Arabic title quietly mangled.
    kept = [ch if (ch.isalnum() or ch in _KEEP_PUNCT
                   or unicodedata.category(ch).startswith("M")) else " "
            for ch in (title or "")]
    stem = re.sub(r"\s+", " ", "".join(kept)).strip(" .-_")

    if len(stem) > MAX_STEM_CHARS:
        cut = stem[:MAX_STEM_CHARS]
        # Prefer a word boundary, but only if one is near the end -- otherwise
        # a title with no spaces would be cut back to almost nothing.
        space = cut.rfind(" ")
        stem = (cut[:space] if space > MAX_STEM_CHARS - 20 else cut).strip(" .-_")

    if not stem or stem.lower() in _RESERVED_STEMS:
        return fallback
    return stem


def run_folder_name(title: str, when: float, job_id: str) -> str:
    """The name a run's folder should carry.

    Job ids are what the code needs; "0f83f76b5623" is not what a person needs
    when ten of them are sitting in one window and they are trying to find the
    clips from a particular video. The title they already recognise goes first,
    the date disambiguates two runs of the same video, and the id is dropped
    entirely -- the manifest inside still carries it, so nothing depends on
    reading it off the folder.
    """
    stamp = time.strftime("%Y-%m-%d", time.localtime(when))
    stem = safe_stem(title or "", fallback="")
    return f"{stem} ({stamp})" if stem else f"run {stamp} {job_id[:6]}"


def unique_dir(parent: Path, name: str) -> Path:
    """`name`, then `name (2)`, … — the first folder name not already taken."""
    candidate = parent / name
    n = 2
    while candidate.exists():
        candidate = parent / f"{name} ({n})"
        n += 1
    return candidate


def _unique_path(folder: Path, stem: str, suffix: str) -> Path:
    """`stem.mp4`, then `stem_2.mp4`, … — the first name not already taken."""
    candidate = folder / f"{stem}{suffix}"
    n = 2
    while candidate.exists():
        candidate = folder / f"{stem}_{n}{suffix}"
        n += 1
    return candidate


def rename_to_title(folder: Path, current: str, title: str) -> str:
    """Rename a rendered clip to its title. Returns the name it ends up with.

    A clip called short_03.mp4 tells you nothing in a folder of thirty; the
    title the SEO writer produced is the one thing that identifies it at a
    glance, and it is what gets uploaded anyway. Never raises -- a clip that
    cannot be renamed is still a clip, so it keeps the name it has.
    """
    src = folder / current
    if not src.exists():
        return current

    suffix = src.suffix or ".mp4"
    stem = safe_stem(title, fallback=src.stem)
    if stem == src.stem:
        return current

    dest = _unique_path(folder, stem, suffix)
    try:
        src.rename(dest)
    except OSError as e:
        print(f"[jobs] could not rename {current} to {dest.name}: {e}", flush=True)
        return current
    return dest.name


def _fmt_clock(seconds: float) -> str:
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

# Rough share of total runtime per stage, used to drive the progress bar.
# Transcription dominates on CPU, which is why it owns the widest band.
_STAGE_BANDS = {
    "queued":     (0.0, 0.0),
    "download":   (0.0, 0.15),
    "transcribe": (0.15, 0.55),
    "rank":       (0.55, 0.70),
    "render":     (0.70, 0.86),
    # Captions and the edit: a short Whisper pass and one encode per clip,
    # about as long again as the render when it is on. When it is off the
    # bar jumps from the render straight to done, which is fine.
    "edit":       (0.86, 1.0),
    "done":       (1.0, 1.0),
}

_STAGE_LABELS = {
    "queued": "Queued",
    "download": "Fetching the video",
    "transcribe": "Transcribing audio",
    "rank": "Finding the best moments",
    "render": "Rendering clips",
    "edit": "Captions and edits",
    "done": "Done",
}

# Map a log line's prefix to the stage it belongs to.
_PREFIX_STAGE = [
    (r"^\[download", "download"),
    (r"^\[transcribe", "transcribe"),
    (r"^\[highlights|^\[llm|^\[rank", "rank"),
    (r"^\[stack|^\[clip/local|^\[center|^\[render", "render"),
    (r"^\[edit|^\[broll", "edit"),
]

# How many times a stage is tried before the run gives up on it. A download
# from a busy host, a transcription that runs out of memory once, a render
# that trips over a file Windows has not let go of yet -- none of these fail
# the same way twice as a rule, and a run that has already paid for every
# stage before the one that failed should not be lost to it. Backing off 5s,
# then 15s.
STAGE_ATTEMPTS = 3
# A clip that fails to render gets this many tries of its own.
CLIP_ATTEMPTS = 3

# Failures that trying again cannot fix. Retrying these only makes someone
# wait longer to read the same message, so they fail straight away -- the
# Retry button is still there once whatever it was has been put right.
_PERMANENT = (
    "api_key_invalid", "api key not valid", "permission_denied", "unauthenticated",
    "private video", "video unavailable", "unsupported url", "is not a valid url",
    "sign in to confirm", "members-only", "join this channel",
    "local file path does not exist",
    # YouTube's bot gate, after yt_access has already tried every way round it
    # -- as the TV app, as a phone, and with whatever cookies this PC could
    # offer. Three more goes at the same ladder is three more minutes of the
    # same answer. The two phrases below are the opening line of the messages
    # yt_access.explain() builds, so they are deliberate, not incidental.
    "prove it isn't a bot",
    "only serve this video to a signed-in account",
    "only serve this channel to a signed-in account",
)


def _is_permanent(e: BaseException) -> bool:
    from shorts_generator.local.llm import DailyQuotaExceeded
    if isinstance(e, DailyQuotaExceeded):
        return True
    msg = str(e).lower()
    return any(t in msg for t in _PERMANENT)


@dataclass
class Job:
    id: str
    source: str
    spec: LayoutSpec
    download_format: str = "best"
    language: Optional[str] = None
    status: str = "queued"          # queued | running | done | error
    stage: str = "queued"
    progress: float = 0.0
    message: str = "Queued"
    error: Optional[str] = None
    source_path: Optional[str] = None
    clips: List[Dict] = field(default_factory=list)
    highlights: List[Dict] = field(default_factory=list)
    video_meta: Dict = field(default_factory=dict)
    # What the whole video is about, worked out once and reused for every
    # title in the run -- see seo.detect_subject. Kept on the job so a rewrite
    # months later files the clips under the same name as the original run.
    subject: Dict = field(default_factory=dict)
    log: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    # Where this job's clips actually live. Set for jobs read back from disk,
    # so moving the save location can't strand the ones already rendered.
    folder: Optional[str] = None
    restored: bool = False
    # When the work actually started and stopped, so the page can show a
    # clock and a guess at what is left rather than a still 0%.
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    # "full" runs every stage; "clips" re-renders only the clips that failed.
    mode: str = "full"
    _version: int = 0

    @property
    def out_dir(self) -> str:
        if self.folder:
            return self.folder
        # Resolved live, so changing the save location takes effect immediately.
        return str(user_config.shorts_dir() / self.id)

    @property
    def source_title(self) -> str:
        return str(self.video_meta.get("title") or "")

    def snapshot(self) -> Dict:
        return {
            "id": self.id,
            "status": self.status,
            "stage": self.stage,
            "stage_label": _STAGE_LABELS.get(self.stage, self.stage),
            "progress": round(self.progress, 4),
            "message": self.message,
            "error": self.error,
            "spec": self.spec.to_dict(),
            "spec_summary": self.spec.describe(),
            "clips": self.clips,
            "source": self.source,
            "source_path": self.source_path,
            "source_title": self.source_title,
            "shorts_dir": self.out_dir,
            "created_at": self.created_at,
            "restored": self.restored,
            # What the page needs to offer a retry: how many clips did not
            # render, and whether the video they come from is still here.
            "failed": sum(1 for c in self.clips
                          if not c.get("url") and c.get("start_time") is not None),
            "source_on_disk": bool(self.source_path and os.path.exists(self.source_path)),
            # A run the app never finished, which still knows what it was
            # making. The page offers to carry it on.
            "resumable": self.status == "interrupted" and bool(self.source),
            "started_at": self.started_at,
            "elapsed": (round((self.finished_at or time.time()) - self.started_at, 1)
                        if self.started_at else None),
            "eta_seconds": self.eta_seconds(),
            "paused": self.status == "running" and proc.is_paused(),
            "log": self.log[-60:],
            "version": self._version,
        }

    def eta_seconds(self) -> Optional[int]:
        """Seconds left, from how long the work so far has actually taken.

        Deliberately naive -- elapsed x remaining/done -- because the stage
        bands are already sized by how long each stage usually takes, and a
        guess that corrects itself every second beats a model that is
        confidently wrong. Nothing is offered until there is enough progress
        for the number to mean anything: an estimate from 2% is noise, and
        noise in a number somebody is waiting on is worse than no number.
        """
        if self.status != "running" or not self.started_at:
            return None
        done = max(0.0, min(1.0, self.progress))
        elapsed = time.time() - self.started_at
        if done < 0.04 or elapsed < 8:
            return None
        return int(elapsed * (1 - done) / done)

    def manifest(self) -> Dict:
        """Everything needed to rebuild this job in a later session.

        Written when the run starts and at every stage, not only when it
        finishes -- a run that was going when the app closed has to leave
        enough behind to be offered back. `status` and `stage` are what say
        so: a manifest still reading "running" on the next launch means the
        app did not get to finish it.
        """
        return {
            "id": self.id,
            "created_at": self.created_at,
            "status": self.status,
            "stage": self.stage,
            "source": self.source,
            "source_path": self.source_path,
            "download_format": self.download_format,
            "language": self.language,
            "video_meta": self.video_meta,
            "subject": self.subject,
            "spec": self.spec.to_dict(),
            "clips": self.clips,
        }


class JobStore:
    """Thread-safe registry. Single worker, FIFO queue."""

    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._worker = threading.Thread(target=self._run_forever, daemon=True)
        self._worker.start()
        self.restore()

    # --- public API -------------------------------------------------------

    def create(self, source: str, spec: LayoutSpec, download_format: str = "best",
               language: Optional[str] = None) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], source=source, spec=spec,
                  download_format=download_format, language=language)
        with self._lock:
            self._jobs[job.id] = job
        os.makedirs(job.out_dir, exist_ok=True)
        # On record before any work starts, so a crash in the first minute
        # still leaves a run that can be carried on rather than a folder
        # nobody can explain.
        self._persist(job)
        self._queue.put(job.id)
        depth = self._queue.qsize()
        if depth > 1:
            self._update(job, message=f"Queued — {depth - 1} job(s) ahead")
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> List[Dict]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        # A finished run with nothing left in it is just an empty folder — the
        # user deleted every clip. Don't show it as a row with no contents.
        return [j.snapshot() for j in jobs
                if j.clips or j.status in ("queued", "running", "error", "interrupted")]

    def clip(self, job: Job, filename: str) -> Optional[Dict]:
        with self._lock:
            for c in job.clips:
                if c.get("file") == filename:
                    return dict(c)
        return None

    def replace_clip(self, job: Job, filename: str, updates: Dict) -> Optional[Dict]:
        """Swap in an edited clip's details, keeping its place in the list."""
        found = None
        with self._lock:
            for c in job.clips:
                if c.get("file") == filename:
                    c.update(updates)
                    job._version += 1
                    found = dict(c)
                    break
        if found:
            self._persist(job)
        return found

    def remove_clip(self, job: Job, filename: str) -> bool:
        with self._lock:
            keep = [c for c in job.clips if c.get("file") != filename]
            if len(keep) == len(job.clips):
                return False
            job.clips = keep
            job._version += 1
        self._persist(job)
        return True

    def set_seo(self, job: Job, filename: str, seo: Dict) -> Optional[Dict]:
        """Attach freshly written upload metadata to one clip."""
        return self.replace_clip(job, filename, {"seo": seo})

    def forget_youtube(self, only_expired: bool = False) -> int:
        """Drop what YouTube handed back about uploads from every clip.

        YouTube's policies cap how long that may be kept, and say it goes when
        the user disconnects. `only_expired` keeps anything younger than the
        cap, for the sweep made at startup.
        """
        from .youtube_upload import expired

        dropped = 0
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            changed = False
            with self._lock:
                for c in job.clips:
                    rec = c.get("youtube")
                    if rec and (not only_expired or expired(rec)):
                        del c["youtube"]
                        changed = True
                        dropped += 1
                if changed:
                    job._version += 1
            if changed:
                self._persist(job)
        return dropped

    def retry(self, job: Job) -> Job:
        """Run a failed job again, from wherever its caches let it pick up.

        Nothing already paid for is paid again: the download is cached, the
        transcript is cached beside it, and the ranking checkpoints every
        chunk. So "again" costs only the stage that failed and what follows.
        """
        with self._lock:
            if job.status in ("queued", "running"):
                raise ValueError("That run is still going.")
            if job.status != "error":
                raise ValueError("That run finished. Retry its failed clips instead.")
            job.mode = "full"
            job.status, job.stage, job.progress = "queued", "queued", 0.0
            job.error = None
            job.message = "Queued — trying again"
            job.clips = []
            job.log.append("--- trying again ---")
            job._version += 1
        self._queue.put(job.id)
        return job

    def carry_on(self, job: Job) -> Job:
        """Finish a run the app never finished.

        Nothing special happens here, and that is the point: the stages are
        idempotent and every expensive one is cached, so putting the same job
        back on the queue skips whatever it had already done -- the download,
        the transcript, the chunks of ranking that were checkpointed -- and
        picks up at the first thing it had not.
        """
        with self._lock:
            if job.status in ("queued", "running"):
                raise ValueError("That run is already going.")
            if job.status not in ("interrupted", "error"):
                raise ValueError("That run finished — there is nothing to carry on.")
            if not job.source:
                raise ValueError("That run is too old to carry on: it does not record "
                                 "what it was made from. Start it again from the video.")
            job.mode = "full"
            job.status, job.stage, job.progress = "queued", "queued", 0.0
            job.error = None
            job.message = "Queued — carrying on where it stopped"
            job.log.append("--- carrying on after the app closed ---")
            job._version += 1
        self._queue.put(job.id)
        return job

    def retry_clips(self, job: Job) -> Job:
        """Render again only the clips in a run that failed to render."""
        with self._lock:
            if job.status in ("queued", "running"):
                raise ValueError("That run is still going.")
            failed = [c for c in job.clips
                      if not c.get("url") and c.get("start_time") is not None]
            if not failed:
                raise ValueError("Nothing in that run failed.")
            if not job.source_path or not os.path.exists(job.source_path):
                raise ValueError("The video those clips come from is no longer on this "
                                 "PC, so they can't be rendered again. Run it again from "
                                 "the start instead.")
            job.mode = "clips"
            job.status, job.stage, job.progress = "queued", "render", 0.0
            job.error = None
            job.message = f"Queued — rendering {len(failed)} clip(s) again"
            job.log.append("--- rendering the failed clips again ---")
            job._version += 1
        self._queue.put(job.id)
        return job

    # --- persistence ------------------------------------------------------

    def _persist(self, job: Job) -> None:
        """Write the job's manifest beside its clips. Never raises.

        Losing a manifest costs the library a row; letting the write throw
        would cost the user a render they already waited for.
        """
        try:
            folder = Path(job.out_dir)
            folder.mkdir(parents=True, exist_ok=True)
            with self._lock:
                data = job.manifest()
            tmp = folder / (MANIFEST_NAME + ".tmp")
            tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
            os.replace(tmp, folder / MANIFEST_NAME)
        except Exception as e:
            print(f"[jobs] could not save the manifest for {job.id}: {e}", flush=True)

    def restore(self) -> int:
        """Adopt every run already sitting in the clips folder.

        Two shapes turn up there. A folder with a manifest rebuilds completely —
        titles, scores, spans, SEO. A folder holding nothing but mp4s (anything
        rendered before manifests existed) is adopted from the filenames alone,
        so those clips are at least playable, savable and deletable again
        instead of being invisible to the app that made them.
        """
        try:
            root = user_config.shorts_dir()
        except Exception as e:
            print(f"[jobs] could not read the clips folder: {e}", flush=True)
            return 0

        found = 0
        try:
            folders = sorted((d for d in root.iterdir() if d.is_dir()),
                             key=lambda d: d.stat().st_mtime, reverse=True)
        except OSError as e:
            print(f"[jobs] could not list {root}: {e}", flush=True)
            return 0

        for folder in folders:
            with self._lock:
                if folder.name in self._jobs:
                    continue
            try:
                job = self._job_from_folder(folder)
            except Exception as e:
                print(f"[jobs] skipping {folder.name}: {e}", flush=True)
                continue
            if job is None:
                continue
            with self._lock:
                self._jobs[job.id] = job
            found += 1

        if found:
            print(f"[jobs] restored {found} earlier run(s) from {root}", flush=True)
            try:
                self.forget_youtube(only_expired=True)
            except Exception as e:
                print(f"[jobs] could not clear old YouTube upload records: {e}", flush=True)
        return found

    def _job_from_folder(self, folder: Path) -> Optional[Job]:
        manifest: Dict = {}
        path = folder / MANIFEST_NAME
        if path.exists():
            try:
                manifest = json.loads(path.read_text(encoding="utf-8-sig"))
            except Exception as e:
                print(f"[jobs] {folder.name}: unreadable manifest ({e}) — "
                      f"reading the folder instead", flush=True)

        job_id = str(manifest.get("id") or folder.name)
        on_disk = {p.name for p in folder.iterdir()
                   if p.is_file() and p.suffix.lower() in CLIP_SUFFIXES}

        clips: List[Dict] = []
        for c in manifest.get("clips") or []:
            if not isinstance(c, dict):
                continue
            name = c.get("file")
            if not name or name not in on_disk:
                continue        # deleted from the folder by hand since the run
            on_disk.discard(name)
            clips.append({**c, "job_id": job_id,
                          "url": f"/api/jobs/{job_id}/clips/{name}"})
        # Runs from before the scorecard existed carry every number it is
        # built from, so they get one on the way in.
        scorecard.attach(clips)

        # Anything left is a file the manifest didn't know about — an older
        # run, or a clip dropped in by hand. Take it at face value.
        for i, name in enumerate(sorted(on_disk), len(clips) + 1):
            clips.append({
                "index": i,
                "rank": i,
                "title": _title_from_filename(name),
                "score": None,
                "start_time": None,
                "end_time": None,
                "duration": None,
                "file": name,
                "url": f"/api/jobs/{job_id}/clips/{name}",
                "job_id": job_id,
                "seo": None,
            })

        # A manifest still reading "running" or "queued" belongs to a run the
        # app never finished -- it closed, crashed, or the machine went down.
        # Worth restoring even with no clips yet: the download, the transcript
        # and the ranking checkpoint it already paid for are all still on
        # disk, so carrying on costs only what was left.
        unfinished = str(manifest.get("status") or "") in ("queued", "running")
        if not clips and not unfinished:
            return None

        try:
            created = float(manifest.get("created_at") or folder.stat().st_mtime)
        except (TypeError, ValueError, OSError):
            created = time.time()

        source_path = manifest.get("source_path")
        job = Job(
            id=job_id,
            source=str(manifest.get("source") or ""),
            spec=LayoutSpec.from_dict(manifest.get("spec")),
            download_format=str(manifest.get("download_format") or "best"),
            language=manifest.get("language"),
            status="interrupted" if unfinished else "done",
            stage=str(manifest.get("stage") or "queued") if unfinished else "done",
            progress=0.0 if unfinished else 1.0,
            message=(
                "Interrupted — the app closed while "
                + _STAGE_LABELS.get(str(manifest.get("stage") or ""), "working").lower()
                if unfinished else f"{len(clips)} clip(s) ready"
            ),
            source_path=source_path if source_path and os.path.exists(source_path) else None,
            clips=clips,
            video_meta=manifest.get("video_meta") or {},
            subject=manifest.get("subject") or {},
            created_at=created,
            folder=str(folder),
            restored=True,
        )
        return job

    # --- internals --------------------------------------------------------

    def _update(self, job: Job, *, stage: Optional[str] = None,
                frac: Optional[float] = None, message: Optional[str] = None) -> None:
        """Advance a job's reported state. `frac` is progress *within* the stage."""
        moved = False
        with self._lock:
            if stage:
                moved = stage != job.stage
                job.stage = stage
            if message:
                job.message = message
            lo, hi = _STAGE_BANDS.get(job.stage, (0.0, 1.0))
            if frac is None:
                job.progress = max(job.progress, lo)
            else:
                job.progress = max(job.progress, lo + (hi - lo) * max(0.0, min(1.0, frac)))
            job._version += 1
        # Each stage boundary is a place worth being able to come back to, and
        # there are four of them in a run -- cheap to write, and the
        # difference between resuming a crashed run and starting over.
        if moved:
            self._persist(job)

    def _log(self, job: Job, line: str) -> None:
        line = line.rstrip()
        if not line:
            return
        with self._lock:
            job.log.append(line)
            job._version += 1

        for pattern, stage in _PREFIX_STAGE:
            if re.match(pattern, line):
                # Download and transcription each report a percentage of their
                # own now, and the download also says how fast it is going --
                # which is the number somebody watching a multi-gigabyte VOD
                # actually wants. Without these two the bar sat at 0% through
                # the two longest stages in the run.
                if stage in ("download", "transcribe"):
                    pct = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%", line)
                    if not pct:
                        self._update(job, stage=stage,
                                     message=_STAGE_LABELS.get(stage, stage))
                        break
                    frac = min(1.0, float(pct.group(1)) / 100.0)
                    message = f"{_STAGE_LABELS.get(stage, stage)} {int(float(pct.group(1)))}%"
                    speed = re.search(r"\bat\s+([\d.]+[KMG]?B/s)", line)
                    if speed:
                        message += f" at {speed.group(1)}"
                    left = re.search(r"-\s*([\dhms]+)\s*left", line)
                    if left:
                        message += f", {left.group(1)} left"
                    self._update(job, stage=stage, frac=frac, message=message)
                    break
                # "[stack] 2/5: ..." gives exact render progress, and
                # "[highlights] chunk 2/12" gives the same for a chunked
                # rank. Both patterns are narrow on purpose. The rank one has
                # to name "chunk": that stage also logs "(attempt 1/5)" while
                # retrying a 503, and a bare N/M search would read a retry as
                # progress and walk the bar backwards. The render one anchors
                # on the "[tag] N/M:" that every clip-progress line is printed
                # as, because that stage also reports a webcam found "from 4/6
                # samples" -- and a bare search takes a detection score for a
                # clip count.
                if stage in ("render", "edit"):
                    m = re.match(r"^\[[^\]]+\]\s+(\d+)\s*/\s*(\d+)\s*:", line)
                elif stage == "rank":
                    m = re.search(r"\bchunk\s+(\d+)\s*/\s*(\d+)\b", line)
                else:
                    m = None
                frac = None
                message = _STAGE_LABELS.get(stage, stage)
                if m:
                    done, total = int(m.group(1)), int(m.group(2))
                    frac = (done - 1) / max(1, total)
                    # A twelve-chunk rank holds one stage for minutes.
                    # Without the counter the label reads as a hang.
                    message = f"{message} ({done}/{total})"
                self._update(job, stage=stage, frac=frac, message=message)
                break

    def _run_forever(self) -> None:
        while True:
            job_id = self._queue.get()
            job = self.get(job_id)
            if job is None:
                continue
            with self._lock:
                job.started_at = time.time()
                job.finished_at = None
            try:
                self._execute(job)
            except Exception as e:
                with self._lock:
                    job.status = "error"
                    job.error = str(e)
                    job.message = f"Failed: {e}"
                    job.log.append(traceback.format_exc())
                    job._version += 1
            finally:
                with self._lock:
                    job.finished_at = time.time()
                # A job that ended while paused must not leave the gate shut,
                # or the next one starts and immediately blocks on a pause
                # nobody can see or lift.
                proc.clear()
                self._announce(job)
                self._queue.task_done()

    def _announce(self, job: Job) -> None:
        """Raise a desktop notification for a run that has just ended.

        This is the only moment in the app worth interrupting somebody for. A
        VOD takes tens of minutes, nobody watches that, and until now the only
        way to learn the clips existed was to go back and check -- which means
        the ones finished at 2am were found at 9.

        Silent when the app is on screen, because a notification for something
        already visible is noise. notify decides that; this only decides what
        it would say.

        Whatever happens here, the job is over and the queue moves on: it runs
        inside the finally that also clears the pause gate.
        """
        try:
            with self._lock:
                status, message, error = job.status, job.message, job.error
                source = job.source_title

            if status == "done":
                title = message or "Your clips are ready"
            elif status == "error":
                title = "That run did not finish"
                message = error or message
            else:
                return          # never started, or cancelled: nothing to say

            body = source or message or ""
            if status == "error" and source:
                body = f"{source} — {message}" if message else source

            if notify.send_if_away(title, body):
                print(f"[notify] {title}", flush=True)
        except Exception as e:  # noqa: BLE001 - a finished run stays finished
            print(f"[notify] skipped ({e.__class__.__name__}: {e})", flush=True)

    def _attempt(self, what: str, fn, *args, **kwargs):
        """Run one stage, trying again when it fails on something that might
        not fail twice. Called inside the job's stdout capture, so every retry
        is in the run's log with the reason for it."""
        for attempt in range(1, STAGE_ATTEMPTS + 1):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                if attempt == STAGE_ATTEMPTS or _is_permanent(e):
                    raise
                wait = 5 * 3 ** (attempt - 1)
                why = (str(e).strip().splitlines() or [e.__class__.__name__])[0][:160]
                print(f"[retry] {what} failed ({why}) - trying again in {wait}s "
                      f"(attempt {attempt + 1}/{STAGE_ATTEMPTS})", flush=True)
                time.sleep(wait)

    def _render(self, job: Job, source_path: str, top: List[Dict]) -> List[Dict]:
        """Render every clip, then give each one that failed tries of its own.

        The renderers already keep one broken clip from sinking the rest; this
        stops it sinking itself. A retry renders under its own name, so a
        half-written file from the failed attempt cannot be mistaken for it,
        and _finalize renames every clip to its title anyway.
        """
        from shorts_generator.local.llm import call_local_llm
        from shorts_generator.render import render_highlights

        extra = {"language": job.language, "kind": job_kind(job), "llm_fn": call_local_llm}
        shorts = render_highlights(source_path, top, job.spec, out_dir=job.out_dir, **extra)
        for attempt in range(2, CLIP_ATTEMPTS + 1):
            failed = [i for i, s in enumerate(shorts) if not s.get("clip_url")]
            if not failed:
                break
            wait = 5 * (attempt - 1)
            print(f"[retry] {len(failed)} clip(s) did not render - trying again in "
                  f"{wait}s (attempt {attempt}/{CLIP_ATTEMPTS})", flush=True)
            time.sleep(wait)
            for i in failed:
                again = render_highlights(source_path, [top[i]], job.spec,
                                          out_dir=job.out_dir,
                                          name_prefix=f"short_{i + 1:02d}_try{attempt}",
                                          **extra)
                shorts[i] = again[0]
        return shorts

    def _execute_clips(self, job: Job) -> None:
        """Render a finished run's failed clips again, and put them in place."""
        from shorts_generator import accel
        accel.reset_run()
        with self._lock:
            job.status = "running"
            job._version += 1
            failed = [(k, dict(c)) for k, c in enumerate(job.clips)
                      if not c.get("url") and c.get("start_time") is not None]
        self._update(job, stage="render", frac=0.0, message=_STAGE_LABELS["render"])

        keys = ("title", "start_time", "end_time", "score", "hook_sentence", "first_line",
                "virality_reason", "hook_peak", "seo", "scene")
        sink = _JobStdout(self, job)
        with contextlib.redirect_stdout(sink):
            shorts = self._render(job, job.source_path,
                                  [{k: c.get(k) for k in keys} for _, c in failed])

        fixed = 0
        for (k, old), s in zip(failed, shorts):
            path = s.get("clip_url")
            if path:
                name = rename_to_title(Path(job.out_dir), os.path.basename(path),
                                       (old.get("seo") or {}).get("title")
                                       or old.get("title") or "")
                updates = {"file": name, "url": f"/api/jobs/{job.id}/clips/{name}",
                           "error": None,
                           "hook_replay_seconds": s.get("hook_replay_seconds"),
                           "edit": s.get("edit"),
                           "heard_words": s.get("heard_words"),
                           "duration": clip_length(s)}
                fixed += 1
            else:
                updates = {"error": s.get("error")}
            with self._lock:
                job.clips[k].update(updates)

        ok = sum(1 for c in job.clips if c.get("url"))
        left = len(failed) - fixed
        with self._lock:
            job.status = "done" if ok else "error"
            job.stage = "done"
            job.progress = 1.0
            job.message = f"{ok} clip(s) ready" + (f" — {left} still failed" if left else "")
            job.error = None if ok else "no clips rendered"
            job._version += 1
        if ok:
            self._persist(job)

    def _execute(self, job: Job) -> None:
        if job.mode == "clips":
            self._execute_clips(job)
            return

        from shorts_generator.boundaries import report as report_cuts
        from shorts_generator.highlights import get_highlights
        from shorts_generator.hook_open import budget as hook_budget
        from shorts_generator.judge import judge as judge_clips, pool_size as judge_pool
        from shorts_generator.local.llm import call_vision_llm, vision_images_per_request
        from shorts_generator.seo import clip_words
        from shorts_generator.local.downloader import (
            download_youtube_local, fetch_video_meta,
        )
        from shorts_generator.local.llm import call_local_llm, reset_fallback
        from shorts_generator.local.transcriber import transcribe_local
        from shorts_generator.render import render_highlights
        from shorts_generator.seo import attach_seo, detect_subject
        from shorts_generator.signals import analyse_audio

        # A previous run may have fallen back to OpenAI. Start this one on the
        # provider the user actually chose — its quota may well have reset.
        reset_fallback()
        # Likewise a hardware encoder, or the GPU for transcription, that gave
        # up last run: a driver hiccup is worth one more chance per run.
        from shorts_generator import accel
        accel.reset_run()
        # And likewise however the last run ended up talking to YouTube. What
        # got through an hour ago may be gated now, and a browser the user has
        # signed in to since is worth looking at again.
        from shorts_generator.local import yt_access
        yt_access.reset()

        with self._lock:
            job.status = "running"
            job._version += 1
        self._update(job, stage="download", message=_STAGE_LABELS["download"])

        sink = _JobStdout(self, job)
        with contextlib.redirect_stdout(sink):
            source_path = self._attempt(
                "the download", download_youtube_local,
                job.source, fmt=job.download_format,
                out_dir=str(user_config.source_dir()),
            )
            with self._lock:
                job.source_path = source_path
                job._version += 1

            # What the video is *about*, straight from its own listing. The
            # transcript says what was said; this says who said it and where,
            # which is what keeps the titles and tags factual.
            meta = fetch_video_meta(job.source)
            if meta:
                with self._lock:
                    job.video_meta = meta
                    job._version += 1
                self._name_folder(job)

            # Explicit spans: the user already told us what to cut, so there is
            # nothing to transcribe and nothing to rank. Straight to rendering.
            if job.spec.time_ranges:
                print(f"[render] {len(job.spec.time_ranges)} exact cut(s) requested — "
                      "skipping transcription and ranking", flush=True)
                top = [
                    {
                        "title": f"Clip {i} · {_fmt_clock(a)}–{_fmt_clock(b)}",
                        "start_time": a,
                        "end_time": b,
                        "score": None,
                        "hook_sentence": "",
                        "virality_reason": "Exact span you asked for",
                    }
                    for i, (a, b) in enumerate(job.spec.time_ranges, 1)
                ]
                all_highlights = list(top)
                subject = detect_subject(job.video_meta, None, job.source, call_local_llm)
                if job.spec.content_kind != "auto":
                    subject["content_kind"] = job.spec.content_kind
                with self._lock:
                    job.subject = subject
                    job._version += 1
                _look_at_clips(job, top, source_path, None, subject)
                attach_seo(top, transcript=None, video_meta=job.video_meta,
                           source=job.source, llm_fn=call_local_llm, subject=subject)
                self._update(job, stage="render", frac=0.0,
                             message=_STAGE_LABELS["render"])
                shorts = self._render(job, source_path, top)
                self._finalize(job, shorts, all_highlights)
                return

            self._update(job, stage="transcribe", message=_STAGE_LABELS["transcribe"])
            # "auto" is the one value that means "let whisper decide"; every
            # other value pins it, so a stream in one language cannot drift
            # into another halfway through.
            spoken = (job.language or "").strip().lower()
            transcript = self._attempt(
                "transcription", transcribe_local,
                source_path, language=None if spoken in ("", "auto") else spoken)
            if not transcript["segments"]:
                raise RuntimeError(
                    "No speech found in this video — nothing to build clips from."
                )

            self._update(job, stage="rank", message=_STAGE_LABELS["rank"])
            # What the transcript cannot say. Measured once for the whole
            # source and reused for every candidate: it is what lets the rank
            # hear a reaction, and what tells each clip where its own loudest
            # moment is so the cold open can start there.
            audio = analyse_audio(source_path, transcript.get("duration", 0))
            # Room held back for that cold open, so a request for 30-second
            # clips still produces 30-second files once it is on the front.
            reserve = hook_budget(job.spec.hook_replay)

            # Beside the video, next to its .srt, so resuming works the same
            # way transcript reuse already does: point at the same source and
            # the work you already paid for is still there.
            checkpoint = Path(source_path).with_suffix(".highlights.json")
            # Checkpointed per chunk, so a retry here re-asks only for the
            # chunks that never came back.
            result = self._attempt("ranking", get_highlights,
                                    transcript, num_clips=job.spec.num_clips,
                                    llm_fn=call_local_llm,
                                    checkpoint_path=checkpoint,
                                    clip_seconds=job.spec.clip_seconds,
                                    brief=job.spec.brief,
                                    audio=audio,
                                    reserve_seconds=reserve,
                                    kind=job.spec.content_kind,
                                    video_meta=job.video_meta,
                                    source_path=source_path)
            all_highlights = result.get("highlights", [])
            content = result.get("content") or {}
            if not all_highlights:
                raise RuntimeError("The ranker found no usable moments in this video.")

            # Then look before cutting. The ranker chose from words and
            # measurements; the best of its candidates are shown to a model
            # that can see, as a stranger scrolling past would meet them, and
            # re-ranked on that -- see shorts_generator/judge.py.
            self._update(job, frac=0.5, message="Watching the best moments like a stranger would")
            pool = sorted(all_highlights, key=lambda h: int(h.get("score", 0)),
                          reverse=True)[:judge_pool(job.spec.num_clips)]
            try:
                judge_clips(pool, source_path, call_vision_llm,
                            lambda h: clip_words(h, transcript),
                            kind=content.get("kind") or "",
                            images_per_request=vision_images_per_request())
            except Exception as e:  # noqa: BLE001 - a second opinion is never worth the run
                print(f"[judge] skipped ({e})", flush=True)

            # Best first, and it stays that way all the way to the grid: the
            # order the user sees is the order worth posting in.
            top = sorted(all_highlights, key=lambda h: int(h.get("score", 0)),
                         reverse=True)[:job.spec.num_clips]
            print(f"[rank] {len(top)} clip(s) chosen, best first: "
                  + ", ".join(str(h.get("score", "?")) for h in top), flush=True)
            # Every cut, with what moved it. A run's framing decisions are
            # otherwise invisible until the clips are watched one by one.
            report_cuts(top)

            self._update(job, frac=0.6, message="Writing titles, tags and hooks")
            # Name the subject once for the whole batch. Every title, tag and
            # hashtag in this run is then filed under the same thing, instead
            # of ten clips inventing ten ways to say it.
            subject = detect_subject(job.video_meta, transcript, job.source,
                                     call_local_llm)
            # Kept with the subject, so the packaging is written for the kind
            # of video this is -- now, and on every rewrite after.
            subject["content_kind"] = content.get("kind") or ""
            with self._lock:
                job.subject = subject
                job._version += 1
            # Then what each clip actually shows. The listing names one game;
            # the frames say which one this clip is, and whether it is a game
            # at all -- see shorts_generator/vision.py.
            self._update(job, frac=0.65, message="Looking at what is on screen")
            _look_at_clips(job, top, source_path, transcript, subject)
            self._update(job, frac=0.8, message="Writing titles, tags and hooks")
            attach_seo(top, transcript=transcript, video_meta=job.video_meta,
                       source=job.source, llm_fn=call_local_llm, subject=subject)

            self._update(job, stage="render", frac=0.0, message=_STAGE_LABELS["render"])
            shorts = self._render(job, source_path, top)

        self._finalize(job, shorts, all_highlights)

    def _name_folder(self, job: Job) -> None:
        """Give this run a folder named after the video, before it renders.

        Called once the metadata is in and nothing has been written yet, so
        this is a choice of name rather than a rename of something in use.
        """
        title = job.source_title
        if not title or job.folder:
            return
        try:
            root = user_config.shorts_dir()
            wanted = unique_dir(root, run_folder_name(title, job.created_at, job.id))
            existing = Path(user_config.shorts_dir() / job.id)
            if existing.exists():
                existing.rename(wanted)     # nothing in it yet, but be tidy
            else:
                wanted.mkdir(parents=True, exist_ok=True)
            with self._lock:
                job.folder = str(wanted)
                job._version += 1
            print(f"[jobs] clips for this run go to: {wanted.name}", flush=True)
        except OSError as e:
            # A name is a convenience; losing it must not cost the run.
            print(f"[jobs] could not name the folder after the video ({e}) — "
                  f"using the run id", flush=True)

    def _finalize(self, job: Job, shorts: List[Dict], all_highlights: List[Dict]) -> None:
        rendered = []
        for i, s in enumerate(shorts, 1):
            path = s.get("clip_url")
            # Name the file after the title it will be uploaded under. SEO runs
            # before rendering, so the title is already there by the time the
            # mp4 exists.
            name = os.path.basename(path) if path else None
            if name:
                seo = s.get("seo") or {}
                name = rename_to_title(Path(job.out_dir), name,
                                       seo.get("title") or s.get("title") or "")
            rendered.append({
                "index": i,
                # `shorts` arrives best-first, so position is the ranking.
                "rank": i,
                "title": s.get("title"),
                "score": s.get("score"),
                "start_time": s.get("start_time"),
                "end_time": s.get("end_time"),
                "duration": clip_length(s),
                "hook_sentence": s.get("hook_sentence"),
                "hook_score": s.get("hook_score"),
                "viral_score": s.get("viral_score"),
                "first_line": s.get("first_line"),
                "virality_reason": s.get("virality_reason"),
                # What this clip was actually chosen on. Kept per clip and
                # written into the manifest so that the day real retention
                # numbers exist, there is a record to correlate them against
                # -- the ranking cannot be tuned against outcomes nobody
                # wrote down.
                "model_score": s.get("model_score"),
                "signal_score": s.get("signal_score"),
                "signals": s.get("signals"),
                "opening_density": s.get("opening_density"),
                "opening_penalty": s.get("opening_penalty"),
                "visual": s.get("visual"),
                "visual_penalty": s.get("visual_penalty"),
                "judge": s.get("judge"),
                "pre_judge_score": s.get("pre_judge_score"),
                "on_screen": s.get("on_screen"),
                "boundary_notes": s.get("boundary_notes"),
                "hook_replay_seconds": s.get("hook_replay_seconds"),
                # Where its loudest moment is, so a clip rendered again later
                # still gets its cold open.
                "hook_peak": s.get("hook_peak"),
                # What the clip's frames showed. Kept so a rewrite months
                # later files it under the same thing without looking again.
                "scene": s.get("scene"),
                "seo": s.get("seo"),
                # What the edit did to it: captions, cuts, punch-ins, emoji,
                # B-roll. See shorts_generator/autoedit.py.
                "edit": s.get("edit"),
                # What was heard, word by word, so the captions can be
                # corrected and burned again without listening twice.
                "heard_words": s.get("heard_words"),
                "error": s.get("error"),
                "job_id": job.id,
                "file": name,
                "url": f"/api/jobs/{job.id}/clips/{name}" if name else None,
            })
        scorecard.attach(rendered)

        ok = [c for c in rendered if c["url"]]
        with self._lock:
            job.clips = rendered
            job.highlights = all_highlights
            job.status = "done" if ok else "error"
            job.stage = "done"
            job.progress = 1.0
            job.message = (f"{len(ok)} clip(s) ready" if ok
                           else "Every clip failed to render — see the log")
            if not ok:
                job.error = "no clips rendered"
            job._version += 1

        # Record the run next to its clips, so it is still here next launch.
        if ok:
            self._persist(job)


def job_kind(job: Job) -> str:
    """What kind of video a run turned out to be: chosen, or else detected."""
    from shorts_generator import content_kinds

    chosen = content_kinds.normalise(job.spec.content_kind)
    if chosen != content_kinds.AUTO:
        return chosen
    detected = content_kinds.normalise((job.subject or {}).get("content_kind"))
    return detected if detected != content_kinds.AUTO else content_kinds.OTHER


def clip_length(clip: Dict) -> Optional[float]:
    """How long the rendered file runs: its span, less the pauses cut out of
    it, plus the cold open put in front of it."""
    try:
        length = float(clip.get("end_time", 0)) - float(clip.get("start_time", 0))
    except (TypeError, ValueError):
        return None
    edit = clip.get("edit") or {}
    length -= float(edit.get("removed_seconds") or 0)
    length += float(clip.get("hook_replay_seconds") or 0)
    return round(max(0.0, length), 1)


def _clip_words(job: Job, clip: Dict) -> str:
    """What is actually said inside one clip, for the SEO writer to work from.

    Cheapest source first. A run that transcribed its source left an .srt
    behind, and slicing the clip's span out of it costs nothing. Only when
    there is no cache — an old folder whose source is long deleted — does this
    fall back to transcribing the clip file itself, which is quick because a
    Short is under a minute.
    """
    from shorts_generator.local.transcriber import _load_srt_cache, transcribe_local

    start, end = clip.get("start_time"), clip.get("end_time")
    if job.source_path and start is not None and end is not None:
        stem = Path(job.source_path).stem
        for candidate in (
            Path(job.source_path).with_suffix(".srt"),
            user_config.source_dir() / f"{stem}.srt",
        ):
            if not candidate.exists():
                continue
            try:
                cached = _load_srt_cache(candidate)
            except Exception:
                continue
            words = " ".join(
                str(s.get("text", "")).strip() for s in cached.get("segments", [])
                if float(s.get("end", 0)) >= float(start) and float(s.get("start", 0)) <= float(end)
            ).strip()
            if words:
                return words[:1600]

    path = Path(job.out_dir) / str(clip.get("file") or "")
    if not path.exists():
        return ""
    try:
        result = transcribe_local(str(path))
    except Exception as e:
        print(f"[seo] could not transcribe {path.name} ({e})", flush=True)
        return ""
    return " ".join(str(s.get("text", "")).strip()
                    for s in result.get("segments", [])).strip()[:1600]


def _look_at_clips(job: Job, highlights: List[Dict], source_path: Optional[str],
                   transcript: Optional[Dict], subject: Optional[Dict]) -> None:
    """Attach a `scene` -- what the frames show -- to each highlight, in place.

    Frames come from the source when it is still on disk, at the clip's span,
    and otherwise from the rendered clip itself. Best-effort: a clip that
    cannot be looked at keeps no scene and is written from its words.
    """
    from shorts_generator.local.llm import call_vision_llm, vision_images_per_request
    from shorts_generator.seo import clip_words, describe_video
    from shorts_generator.vision import describe_clips

    items, idx = [], []
    for i, h in enumerate(highlights):
        if h.get("scene"):
            continue
        start, end = h.get("start_time"), h.get("end_time")
        if source_path and os.path.exists(source_path) and start is not None and end is not None:
            items.append({"path": source_path, "start": float(start), "end": float(end)})
        elif h.get("_clip_path") and os.path.exists(h["_clip_path"]):
            items.append({"path": h["_clip_path"], "start": 0.0, "end": 0.0})
        else:
            continue
        items[-1]["said"] = clip_words(h, transcript)
        idx.append(i)
    if not items:
        return

    subject = subject or {}
    candidates = [subject.get("subject") or ""] + list(subject.get("all_subjects") or [])
    try:
        scenes = describe_clips(items, call_vision_llm,
                                video_context=describe_video(job.video_meta, job.source),
                                candidates=candidates,
                                images_per_request=vision_images_per_request())
    except Exception as e:  # noqa: BLE001 - never worth losing a run over
        print(f"[vision] skipped ({e})", flush=True)
        return
    for i, scene in zip(idx, scenes):
        if scene:
            highlights[i]["scene"] = scene


def regenerate_seo(store: "JobStore", job: Job, force: bool = False,
                   only: Optional[str] = None) -> int:
    """Write upload metadata for a finished job's clips. Returns how many.

    This is what gives clips made before any of this existed a title, a
    description and tags — including ones whose only remaining trace is the
    mp4 itself. `only` rewrites a single clip by filename, keeping the rest of
    the run's titles in view so the new one does not open the same way.
    """
    from shorts_generator.local.llm import call_local_llm
    from shorts_generator.seo import generate_seo

    with store._lock:
        if only:
            targets = [dict(c) for c in job.clips if c.get("file") == only]
        else:
            targets = [dict(c) for c in job.clips
                       if c.get("file") and (force or not c.get("seo"))]
        others = [(c.get("seo") or {}).get("title") or "" for c in job.clips
                  if only and c.get("file") != only]
    if not targets:
        return 0

    print(f"[seo] preparing metadata for {len(targets)} clip(s)", flush=True)
    highlights = []
    for c in targets:
        start = c.get("start_time")
        end = c.get("end_time")
        highlights.append({
            "title": c.get("title") or "",
            "start_time": float(start) if start is not None else 0.0,
            "end_time": float(end) if end is not None else float(c.get("duration") or 0.0),
            "score": c.get("score"),
            "hook_sentence": c.get("hook_sentence") or "",
            "first_line": c.get("first_line") or "",
            "virality_reason": c.get("virality_reason") or "",
            "transcript_text": _clip_words(job, c),
            "scene": c.get("scene"),
            "subject_override": c.get("subject_override") or "",
            "_clip_path": str(Path(job.out_dir) / str(c.get("file") or "")),
        })

    # Clips made before the app looked at frames get looked at now, once; the
    # answer is saved on the clip so the next rewrite does not pay for it.
    missing = [h for h in highlights if not h.get("scene")]
    if missing:
        source = job.source_path if job.source_path and os.path.exists(job.source_path) else None
        if source is None:
            for h in missing:
                h["start_time"], h["end_time"] = None, None   # read the clip file instead
        _look_at_clips(job, missing, source, None, job.subject or None)
        for c, h in zip(targets, highlights):
            if h.get("scene") and not c.get("scene"):
                store.replace_clip(job, c["file"], {"scene": h["scene"]})
            if h["start_time"] is None:
                h["start_time"] = float(c.get("start_time") or 0.0)
                h["end_time"] = float(c.get("end_time") or c.get("duration") or 0.0)

    errors: List[str] = []
    written = generate_seo(highlights, video_meta=job.video_meta,
                           source=job.source, llm_fn=call_local_llm,
                           errors=errors, subject=job.subject or None,
                           avoid_titles=[t for t in others if t])

    # Only keep what a model actually wrote, or fill a clip that had nothing at
    # all. Writing a fallback over metadata that was generated properly would
    # quietly downgrade a good title into a filename — worst on the "Rewrite"
    # button, whose whole job is to improve what is already there.
    kept = 0
    for clip, seo in zip(targets, written):
        if seo.get("generated") or not clip.get("seo"):
            store.set_seo(job, clip["file"], seo)
        if not seo.get("generated"):
            continue
        kept += 1
        # A rewritten title is a rewritten filename. Leaving the old name on
        # disk is how a folder ends up full of clips named after titles that
        # were replaced hours ago.
        new_name = rename_to_title(Path(job.out_dir), clip["file"],
                                   seo.get("title") or "")
        if new_name != clip["file"]:
            store.replace_clip(job, clip["file"], {
                "file": new_name,
                "url": f"/api/jobs/{job.id}/clips/{new_name}",
            })

    if not kept:
        # Returning a count of fallbacks read as success all the way to the UI,
        # which then reported titles were "ready" when nothing had changed.
        raise RuntimeError(
            errors[0] if errors else
            "The metadata writer returned nothing usable for these clips."
        )
    return kept


class _JobStdout(io.TextIOBase):
    """Captures pipeline prints into the job log, line by line."""

    def __init__(self, store: "JobStore", job: Job) -> None:
        self._store = store
        self._job = job
        self._buf = ""

    def write(self, s: str) -> int:
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._store._log(self._job, line)
        return len(s)

    def flush(self) -> None:
        if self._buf.strip():
            self._store._log(self._job, self._buf)
            self._buf = ""


STORE = JobStore()
