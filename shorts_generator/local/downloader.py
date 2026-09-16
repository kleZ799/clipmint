"""Local YouTube download via yt-dlp.

Returns a local mp4 path so the rest of the local pipeline can read it
directly off disk.
"""
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from typing import Dict, List, Optional

from . import yt_access
from .. import proc
from ..config import LOCAL_OUTPUT_DIR


def _import_ytdlp():
    try:
        import yt_dlp  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "yt-dlp is required for --mode local. Install it with:\n"
            "    pip install -r requirements-local.txt"
        ) from e
    return yt_dlp


def _size(n: Optional[float]) -> str:
    """Bytes as something a person reads: 1.2GB, 840MB, 12MB."""
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit in ("B", "KB") else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def _clock(seconds: Optional[float]) -> str:
    try:
        s = int(float(seconds or 0))
    except (TypeError, ValueError):
        return "?"
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else (f"{m}m{sec:02d}s" if m else f"{sec}s")


# Printed at most this often. A fragment hook fires dozens of times a second
# on a fast connection, and every line is a line in the run's log.
_PROGRESS_EVERY = 1.0
_last_progress = [0.0]

# How far back the rate is measured over. yt-dlp's own d["speed"] is an
# instantaneous reading, and the first one it hands out is computed over a
# few milliseconds of a connection that has not opened its window yet -- so
# it lands somewhere around a hundred KB/s no matter how fast the line is.
# On a 25 MB file that produced "0.0% at 361KB/s - 1m11s left" for a download
# that finished in three seconds; on a 7 GB stream VOD the same first sample
# reads "at 126KB/s - 21h46m left", which is what gets screenshotted and
# reported as the app being slow.
#
# So the rate here is measured over a trailing window instead. Long enough to
# be stable, short enough to still show a real slowdown when YouTube starts
# throttling -- which does happen, and is worth seeing.
_RATE_WINDOW = 20.0
_MIN_SAMPLE_SECONDS = 2.0
_MIN_SAMPLE_BYTES = 1 << 20

# (when, bytes-so-far) for the file currently in flight.
_samples: list = []


def _note_sample(done: float) -> None:
    """Record where the download had got to, and forget what fell out of the window."""
    now = time.time()
    # downloaded_bytes going backwards means yt-dlp moved on to the next file
    # of the pair (video, then audio). That is a new transfer, not a stall.
    if _samples and done < _samples[-1][1]:
        _samples.clear()
    _samples.append((now, done))
    cutoff = now - _RATE_WINDOW
    while len(_samples) > 2 and _samples[0][0] < cutoff:
        _samples.pop(0)


def _rate() -> Optional[float]:
    """Bytes per second across the window, or None while it is too early to say."""
    if len(_samples) < 2:
        return None
    span = _samples[-1][0] - _samples[0][0]
    moved = _samples[-1][1] - _samples[0][1]
    if span < _MIN_SAMPLE_SECONDS or moved < _MIN_SAMPLE_BYTES:
        return None
    rate = moved / span
    return rate if rate > 0 else None


def _progress_line(d: Dict) -> None:
    """One readable line about a download in flight: how far, how fast, how long left.

    Never raises: a progress line that throws would take the download with it.
    """
    try:
        status = d.get("status")
        if status == "finished":
            _last_progress[0] = 0.0
            _samples.clear()
            print(f"[download] got {_size(d.get('total_bytes') or d.get('downloaded_bytes'))}"
                  f" in {_clock(d.get('elapsed'))}", flush=True)
            return
        if status != "downloading":
            return

        done = float(d.get("downloaded_bytes") or 0)
        _note_sample(done)                      # every callback, not every line

        now = time.time()
        if now - _last_progress[0] < _PROGRESS_EVERY:
            return
        _last_progress[0] = now

        total = float(d.get("total_bytes") or d.get("total_bytes_estimate") or 0)
        pct = f"{100 * done / total:.1f}%" if total else _size(done)
        parts = [f"[download] {pct}"]
        if total:
            parts.append(f"of {_size(total)}")
        # No rate and no estimate until the window has enough in it to mean
        # something. A line that says only how far along it is tells the truth;
        # a number made up from the first few milliseconds does not.
        rate = _rate()
        if rate:
            parts.append(f"at {_size(rate)}/s")
            if total > done:
                parts.append(f"- {_clock((total - done) / rate)} left")
        print(" ".join(parts), flush=True)
    except Exception:
        pass


def _format_for(fmt: str) -> str:
    """Map our '720' / '1080' / 'best' shorthand to a yt-dlp format selector.

    'best' takes whatever the source actually offers, which is what you want
    when the clips are meant to come out at the source's real quality.
    """
    fmt = (fmt or "").strip().lower()
    if fmt in ("best", "max", "source", "auto", ""):
        # Deliberately not restricted to mp4-friendly codecs. Above 1080p
        # YouTube only offers VP9 and AV1, so demanding H.264 here would
        # quietly cap "best" at 1080p. The container is what gives instead --
        # see merge_output_format below.
        return "bestvideo+bestaudio/best"
    try:
        height = int(fmt)
    except ValueError:
        height = 720
    return (
        # H.264 video with AAC audio first: that pair goes into an mp4 with a
        # plain remux, no re-encode and nothing for ffmpeg to refuse. Only if
        # the video has no such pair does this fall back to whatever exists.
        f"bestvideo[height<={height}][vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
        f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo[height<={height}]+bestaudio/"
        f"best[height<={height}]/best"
    )


def _extract_youtube_video_id(source: str) -> Optional[str]:
    """Best-effort extraction of a YouTube video id from a URL."""
    parsed = urlparse(source)
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]

    if host in ("youtu.be", "www.youtu.be"):
        video_id = parsed.path.lstrip("/").split("/", 1)[0]
        return video_id or None

    if "youtube.com" in host:
        if parsed.path.startswith("/watch"):
            qs = parse_qs(parsed.query)
            video_id = qs.get("v", [""])[0]
            return video_id or None
        match = re.search(r"/(?:shorts|embed|live)/([^/?#&]+)", parsed.path)
        if match:
            return match.group(1)

    return None


def _resolve_local_path(source: str) -> Optional[str]:
    """Return a local filesystem path if the input already points at one."""
    parsed = urlparse(source)
    if parsed.scheme == "file":
        raw_path = unquote(parsed.path)
        if parsed.netloc and parsed.netloc not in ("", "localhost"):
            raw_path = f"//{parsed.netloc}{raw_path}"
        candidate = Path(raw_path).expanduser()
        if candidate.exists() and candidate.is_file():
            return str(candidate.resolve())
        raise RuntimeError(f"Local file URL does not exist: {source}")

    if parsed.scheme in ("http", "https"):
        return None

    candidate = Path(source).expanduser()
    if candidate.exists() and candidate.is_file():
        return str(candidate.resolve())

    if any(sep in source for sep in (os.sep, "/")) or source.startswith("~") or source.startswith("."):
        raise RuntimeError(f"Local file path does not exist: {source}")

    return None


def _probe_height(path: str) -> int:
    """Video height of a local file, or 0 if ffprobe can't tell us."""
    try:
        out = proc.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=height", "-of", "csv=p=0", path],
            capture_output=True, text=True, check=True,
        ).stdout.strip().splitlines()[0]
        return int(float(out))
    except Exception:
        return 0


def _existing_download(out_dir: str, video_id: str) -> Optional[str]:
    """Return a cached download path if we already have this YouTube id.

    Prefers the highest-resolution copy on disk, so asking for better quality
    after a low-quality run doesn't silently hand back the old file.

    Both naming schemes are searched. Downloads are now named after the video
    with its id in brackets, but anyone upgrading has a folder full of
    `source_<id>.mp4` from before that, and re-downloading gigabytes because
    the pattern changed would be a poor way to thank them for updating.
    """
    # Scanned rather than globbed: a video id can contain - and _, and glob
    # reads [...] as a character class, so the bracketed form would match
    # filenames it has no business matching.
    candidates = []
    try:
        names = os.listdir(out_dir)
    except OSError:
        return None
    for name in names:
        stem, ext = os.path.splitext(name)
        if ext.lower() not in (".mp4", ".mkv", ".webm"):
            continue
        if f"[{video_id}]" in stem or stem == f"source_{video_id}" \
                or stem.startswith(f"source_{video_id}_"):
            candidates.append(os.path.join(out_dir, name))

    best, best_h = None, -1
    for path in sorted(set(candidates)):
        h = _probe_height(path)
        if h > best_h:
            best, best_h = path, h
    return best


def _cache_is_good_enough(path: str, fmt: str) -> bool:
    """Does this cached file already meet the requested quality?"""
    have = _probe_height(path)
    if have <= 0:
        return True          # can't tell — don't re-download on a guess
    fmt = (fmt or "").strip().lower()
    if fmt in ("best", "max", "source", "auto", ""):
        # Anything 1440p or above is treated as already-best; below that it is
        # worth checking whether the source offers more.
        return have >= 1440
    try:
        return have >= int(fmt) * 0.95
    except ValueError:
        return True


def is_channel_or_playlist(url: str) -> bool:
    """True if the URL points at a collection rather than a single video."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.netloc or "").lower().replace("www.", "")
    if "youtube.com" not in host and host != "youtu.be":
        return False
    if _extract_youtube_video_id(url):
        return False
    path = parsed.path.rstrip("/")
    if "list=" in (parsed.query or ""):
        return True
    return (
        path.startswith("/@")
        or path.startswith("/channel/")
        or path.startswith("/c/")
        or path.startswith("/user/")
        or path.startswith("/playlist")
        or path.endswith(("/videos", "/streams", "/shorts"))
    )


def list_channel_videos(url: str, limit: int = 12) -> List[Dict]:
    """List recent videos on a channel or playlist without downloading them.

    Uses yt-dlp's flat extraction, so this is a metadata call — it does not
    pull any media. Returns newest-first.
    """
    yt_dlp = _import_ytdlp()

    # Channel roots don't list videos directly; /videos and /streams do.
    parsed = urlparse(url)
    probe_url = url
    path = parsed.path.rstrip("/")
    if (path.startswith("/@") or path.startswith("/channel/")
            or path.startswith("/c/") or path.startswith("/user/")):
        if not path.endswith(("/videos", "/streams", "/shorts")):
            probe_url = f"{url.rstrip('/')}/videos"

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "playlistend": limit,
    }

    info = yt_access.run(yt_dlp, ydl_opts,
                         lambda ydl: ydl.extract_info(probe_url, download=False),
                         what="this channel")

    entries = info.get("entries") or []
    videos: List[Dict] = []
    for e in entries[:limit]:
        if not e or e.get("_type") == "playlist":
            continue
        vid = e.get("id")
        if not vid:
            continue
        videos.append({
            "id": vid,
            "title": e.get("title") or "(untitled)",
            "url": e.get("url") if str(e.get("url", "")).startswith("http")
                   else f"https://www.youtube.com/watch?v={vid}",
            "duration": e.get("duration"),
            "thumbnail": (e.get("thumbnails") or [{}])[-1].get("url"),
        })
    return videos


def fetch_video_meta(video_url: str) -> Dict:
    """Title, channel, description and tags for a URL, without downloading it.

    Feeds the SEO writer: a clip's own transcript says what was said, and this
    says what the video is *about* — the channel, the topic, the words the
    uploader already ranks for. Metadata only, and best-effort: a failure here
    costs a little context, never the run.
    """
    if _resolve_local_path(video_url):
        return {}

    try:
        yt_dlp = _import_ytdlp()
        opts = {"quiet": True, "no_warnings": True, "skip_download": True}
        info = yt_access.run(
            yt_dlp, opts,
            lambda ydl: ydl.extract_info(video_url, download=False) or {}) or {}
    except Exception as e:
        print(f"[download/local] could not read video details ({e})", flush=True)
        return {}

    meta = {
        "title": info.get("title") or "",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "description": (info.get("description") or "")[:2000],
        "tags": [str(t) for t in (info.get("tags") or [])][:30],
        "categories": [str(c) for c in (info.get("categories") or [])][:5],
        "duration": info.get("duration"),
        "webpage_url": info.get("webpage_url") or video_url,
    }
    if meta["title"]:
        print(f"[download/local] source: {meta['title']}"
              + (f" — {meta['uploader']}" if meta["uploader"] else ""), flush=True)
    return meta


def download_youtube_local(video_url: str, fmt: str = "720", out_dir: Optional[str] = None) -> str:
    """Download a remote URL or return a local file path unchanged."""
    local_path = _resolve_local_path(video_url)
    if local_path:
        print(f"[download/local] using local file: {local_path}", flush=True)
        return local_path

    yt_dlp = _import_ytdlp()
    out_dir = out_dir or LOCAL_OUTPUT_DIR
    os.makedirs(out_dir, exist_ok=True)

    video_id = _extract_youtube_video_id(video_url)
    cached = _existing_download(out_dir, video_id) if video_id else None
    if cached:
        if _cache_is_good_enough(cached, fmt):
            print(f"[download/local] reusing cached download: {cached} "
                  f"({_probe_height(cached)}p)", flush=True)
            return cached
        print(f"[download/local] cached copy is only {_probe_height(cached)}p — "
              f"fetching a better one for '{fmt}'", flush=True)

    # Tag the filename with the requested quality so a higher-quality re-fetch
    # sits alongside the old copy instead of overwriting it (and invalidating
    # the transcript cached against it).
    tag = "" if fmt in ("720",) else f"_{(fmt or 'best').strip().lower()}"
    print(f"[download/local] {video_url} @ {fmt} → {out_dir}/", flush=True)
    ydl_opts = {
        "format": _format_for(fmt),
        # Named after the video, with the id kept in brackets. "source_dQw4w9
        # WgXcQ.mp4" tells a person nothing about which video it is, and this
        # folder fills up with multi-gigabyte files they will eventually want
        # to sort through and delete. The id stays because the cache lookup
        # needs it, and because two videos can share a title.
        #
        # restrictfilenames is off deliberately -- it strips non-ASCII, which
        # would reduce a Hindi or Japanese title to nothing. yt-dlp already
        # replaces characters the filesystem rejects.
        "outtmpl": os.path.join(out_dir, f"%(title).80B [%(id)s]{tag}.%(ext)s"),
        "windowsfilenames": True,
        # "mp4/mkv", not "mp4". yt-dlp merges the separate video and audio
        # streams by handing them to ffmpeg, and YouTube serves Opus audio
        # with VP9 and AV1 video. Opus in mp4 is barely supported, so forcing
        # mp4 makes ffmpeg refuse the merge outright and the whole download
        # dies with "Postprocessing: Conversion failed!" -- on some videos and
        # not others, which is a miserable thing to debug from a bug report.
        #
        # Naming mkv as the fallback lets those land in a container that
        # accepts them. Nothing downstream cares: the renderer hands files to
        # ffmpeg, which reads mkv perfectly well.
        "merge_output_format": "mp4/mkv",
        "quiet": True,
        "no_warnings": True,
        # yt-dlp's own carriage-return progress bar is useless here -- it is
        # captured line by line into a log -- but the numbers behind it are
        # exactly what somebody staring at a stalled-looking app wants. So its
        # bar stays off and the hook below prints a line a second instead.
        "noprogress": True,
        "progress_hooks": [_progress_line],
        # Only some formats arrive as fragments -- measured against a 4-hour
        # VOD, YouTube serves the H.264 streams this asks for as one
        # continuous file, and this setting does nothing for those. It still
        # earns its place on the fragmented formats (live VODs, and the VP9
        # and AV1 ladders "best" reaches for above 1080p), where fetching one
        # fragment at a time leaves most of the connection idle. Four at once
        # is the usual sweet spot, and not so many that YouTube throttles.
        "concurrent_fragment_downloads": 4,
        # The continuous formats are the ones that go slowly, and they go
        # slowly in a specific way: YouTube rate-limits a single long-lived
        # connection, so a download that starts fast decays to a crawl and
        # reports hours remaining. Asking for the file in 10 MB ranges makes
        # each chunk a fresh request and sidesteps that.
        #
        # On a healthy connection this measures the same as leaving it off --
        # it is not a speed-up, it is insurance against the throttled case,
        # which is the one people actually report.
        "http_chunk_size": 10 * 1024 * 1024,
    }

    def _run(opts):
        def once(ydl):
            info = ydl.extract_info(video_url, download=True)
            return ydl.prepare_filename(info), info
        return yt_access.run(yt_dlp, opts, once)

    try:
        path, info = _run(ydl_opts)
    except Exception as first:
        # Before anything else: is there already a copy on disk we turned down?
        #
        # Reaching here with `cached` set means the file was usable and we went
        # looking for a better one anyway -- a 1080p copy when the run asked for
        # "best", say. That was an optimisation, and an optimisation that fails
        # must not take the run with it. Measured on a real run: a finished
        # 10 GB download, its transcript and its ranked highlights were all
        # thrown away because YouTube refused to serve a *higher* quality copy
        # of a video already sitting in the folder.
        #
        # So the upgrade is abandoned and the copy we have is used. Anything
        # that was cached against that exact file -- the transcript especially,
        # which costs an hour of GPU on a long VOD -- stays valid, because it
        # is the same file it was always keyed to.
        if cached:
            why = (str(first).strip().splitlines() or ["unknown error"])[0]
            print(f"[download/local] couldn't fetch a better copy ({why})",
                  flush=True)
            print(f"[download/local] using the {_probe_height(cached)}p copy "
                  f"already downloaded: {cached}", flush=True)
            return cached

        # "Postprocessing: Conversion failed!" is yt-dlp reporting that ffmpeg
        # refused to combine the streams it downloaded, and the reason lives in
        # ffmpeg's stderr -- which quiet/no_warnings throws away, leaving a bug
        # report that says only that something failed.
        #
        # So: say what we know, then try again without dictating a container.
        # Asking for a specific one is what usually causes this, and letting
        # yt-dlp keep the streams in whatever holds them natively costs a
        # different file extension, which nothing downstream cares about.
        if "postprocessing" not in str(first).lower():
            raise
        print(f"[download/local] merge into mp4 failed: {first}", flush=True)
        print("[download/local] retrying without forcing a container", flush=True)
        retry = dict(ydl_opts)
        retry.pop("merge_output_format", None)
        retry["quiet"] = False          # let ffmpeg's own reason reach the log
        retry["no_warnings"] = False
        try:
            path, info = _run(retry)
        except Exception as second:
            raise RuntimeError(
                "Could not download this video. ffmpeg would not combine its "
                "video and audio streams.\n\n"
                f"First attempt: {first}\n"
                f"Retry without a forced container: {second}\n\n"
                "If this happens on every video, ffmpeg is likely missing or "
                "broken in this install."
            ) from second
        # merge_output_format may rename the extension after merge
        if not os.path.exists(path):
            stem, _ = os.path.splitext(path)
            for ext in (".mp4", ".mkv", ".webm"):
                if os.path.exists(stem + ext):
                    path = stem + ext
                    break

    print(f"[download/local] ready: {path}", flush=True)
    return path
