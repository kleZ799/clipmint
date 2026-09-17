"""FastAPI app behind the Shorts UI.

Requests never block on the pipeline — POST /api/jobs enqueues and returns
immediately, and the browser follows progress over SSE.

Run it with:
    python -m webapp
"""
import asyncio
import contextlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from shorts_generator.layout_spec import ASPECT_PRESETS, LayoutSpec, parse_layout_prompt
from . import notify, updater
from .jobs import STORE, regenerate_seo, rename_to_title

STATIC_DIR = Path(__file__).parent / "static"
UPLOAD_DIR = Path("webapp_uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

VIDEO_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".ts", ".flv"}

app = FastAPI(title="ClipMint")


@app.on_event("startup")
async def _check_playbook() -> None:
    """Look for a newer growth playbook, once a day, without holding anything up.

    See shorts_generator/playbook.py. The titles written in the meantime use the
    copy already on disk.
    """
    from shorts_generator import playbook
    playbook.refresh_in_background()


# --- request models -------------------------------------------------------

class ResolveRequest(BaseModel):
    url: str


class LayoutPreviewRequest(BaseModel):
    prompt: str = ""
    use_llm: bool = True
    # An explicit pick from the UI toggle. None means "whatever the words say",
    # which is how this worked before the toggle existed.
    aspect_ratio: Optional[str] = None
    content_kind: Optional[str] = None


class JobRequest(BaseModel):
    source: str
    prompt: str = ""
    num_clips: Optional[int] = None
    download_format: str = "best"
    # English rather than None. Left to auto-detect, whisper drifts on game
    # audio and music beds and starts emitting fluent nonsense in whatever
    # language it guessed -- a 3h47m English stream came back 703 of 1097
    # cues in Korean. "auto" is still available for genuinely mixed sources.
    language: Optional[str] = "en"
    use_llm: bool = True
    aspect_ratio: Optional[str] = None
    # The checkbox in the render panel. None means "whatever the prompt said",
    # which is how a client that predates the toggle still behaves.
    hook_replay: Optional[bool] = None
    # The "Kind of video" picker. None or "auto" leaves it to the prompt's own
    # words, and then to the ranker.
    content_kind: Optional[str] = None


# --- routes ---------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(
        (STATIC_DIR / "index.html").read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/api/options")
async def options() -> dict:
    """What the UI needs to render its controls."""
    return {
        "aspect_ratios": [
            {"value": k, "width": v[0], "height": v[1]} for k, v in ASPECT_PRESETS.items()
        ],
        "layouts": [
            {"value": "stacked", "label": "Webcam on top",
             "hint": "Your camera above, gameplay below. Best for streams."},
            {"value": "facetrack", "label": "Follow the face",
             "hint": "One frame, crop tracks the speaker. Best for talking heads."},
            {"value": "center", "label": "Gameplay only",
             "hint": "Plain centre crop, no webcam panel."},
        ],
        "corners": ["bottom-left", "bottom-right", "top-left", "top-right"],
    }


class SettingsRequest(BaseModel):
    provider: str = "gemini"
    api_key: str = ""
    model: Optional[str] = None
    # Store the key but keep the current provider — this is how an OpenAI key
    # gets saved as the thing that takes over when Gemini's day runs out,
    # rather than as a switch away from Gemini.
    as_fallback: bool = False
    # A self-imposed daily request cap, so a provider that bills instead of
    # cutting you off still has a line to draw a meter against. "" clears it.
    daily_limit: Optional[str] = None


def _gemini_model_options() -> list:
    """Models to offer, asked of the API and annotated with known limits.

    The available list has to come from the account: a hardcoded one went
    stale and offered a retired model, which 404'd in the middle of a run.
    The free-tier numbers stay local because the API does not report them —
    so a model we have no number for is offered with none, rather than a
    guess dressed up as fact.
    """
    from shorts_generator import usage
    from shorts_generator.local.llm import list_gemini_models

    known = usage.FREE_TIER_DAILY_LIMITS
    names = list_gemini_models() or list(known)
    opts = [{"value": n, "daily_free": known.get(n)} for n in names]
    # Ones with a confirmed allowance first, biggest first — that is the
    # number someone picking a model is actually shopping for.
    opts.sort(key=lambda o: (o["daily_free"] is None, -(o["daily_free"] or 0), o["value"]))
    return opts


# Groq's own /models endpoint needs a key, and the UI has to render this
# panel before one exists. The free-tier line-up is short and stable enough
# to state, and an unknown model typed into settings.json still works --
# this list is a convenience, not a whitelist.
GROQ_MODEL_OPTIONS = [
    {"value": "openai/gpt-oss-120b", "note": "120B — best judgement, the default"},
    {"value": "qwen/qwen3.8-27b", "note": "27B — strongest at holding a JSON schema"},
    {"value": "qwen/qwen3.6-27b", "note": "27B — previous Qwen"},
    {"value": "openai/gpt-oss-20b", "note": "20B — fastest, weakest ranking"},
]


@app.get("/api/settings")
async def get_settings() -> dict:
    """What the UI needs to decide whether to show first-run setup.

    Never returns the key itself — only whether one is present, and where
    it came from, so the user knows which knob actually controls it.
    """
    from shorts_generator import usage, user_config
    from shorts_generator.config import current_model, current_provider

    provider = current_provider()
    from_env = bool(os.getenv("GEMINI_API_KEY") or os.getenv("OPENAI_API_KEY"))
    return {
        "has_key": user_config.has_llm_key(),
        "provider": provider,
        "model": current_model(provider),
        "source": "environment" if from_env else "settings file",
        "config_path": str(user_config.config_path()),
        "ffmpeg": shutil.which("ffmpeg") is not None,
        # Which providers are already set up, so the UI can offer a switch
        # rather than asking for a key that is on disk already.
        "keys": {
            "gemini": bool(user_config.get("GEMINI_API_KEY")),
            "groq": bool(user_config.get("GROQ_API_KEY")),
            "openai": bool(user_config.get("OPENAI_API_KEY")),
        },
        "daily_limits": {
            "gemini": user_config.get("GEMINI_DAILY_LIMIT"),
            "openai": user_config.get("OPENAI_DAILY_LIMIT"),
        },
        # An env var (or a .env beside the source) outranks the settings file
        # by design. Saying so is the difference between a switch that looks
        # broken and one that explains itself.
        "provider_pinned": bool(os.getenv("LLM_PROVIDER", "").strip()),
        # The free allowance differs enormously between Gemini models — 20 a
        # day on one, hundreds on another — and picking the wrong one is the
        # difference between a working afternoon and a paid API. Offered here
        # so the choice is made with the number in view.
        "gemini_models": _gemini_model_options(),
        "groq_models": GROQ_MODEL_OPTIONS,
        "model_pinned": bool(os.getenv("GEMINI_MODEL", "").strip())
        if provider == "gemini" else bool(os.getenv("GROQ_MODEL", "").strip())
        if provider == "groq" else bool(os.getenv("OPENAI_MODEL", "").strip()),
    }


@app.get("/api/usage")
async def get_usage() -> dict:
    """Today's API spend, so the daily cap is visible before it bites."""
    from shorts_generator import usage, user_config
    from shorts_generator.config import current_model, current_provider

    # Report each provider against the model actually selected for it.
    snap = usage.snapshot({"gemini": current_model("gemini"),
                           "openai": current_model("openai")})
    provider = current_provider()
    snap["provider"] = provider
    snap["model"] = current_model(provider)
    # Whether a spent Gemini day would actually be survivable.
    snap["fallback_ready"] = bool(user_config.get("OPENAI_API_KEY"))

    # A provider that is set up but unused today still has a budget worth
    # showing — the whole point of a meter is to read it *before* spending.
    for name, key in (("gemini", "GEMINI_API_KEY"), ("openai", "OPENAI_API_KEY")):
        if name in snap["providers"] or not user_config.get(key):
            continue
        model = current_model(name)
        limit = usage.daily_limit(name, model)
        snap["providers"][name] = {
            "used": 0, "model": model, "limit": limit or None,
            "remaining": limit or None, "exhausted": False,
        }
    return snap


@app.post("/api/settings")
async def set_settings(req: SettingsRequest) -> dict:
    from shorts_generator import user_config

    provider = (req.provider or "gemini").strip().lower()
    # A mapping rather than a chain of two-way ternaries: with three providers
    # the "gemini or else openai" shape silently files Groq's key under
    # OpenAI's name, which is the kind of bug that only shows up as a
    # confusing auth failure much later.
    prefixes = {"gemini": "GEMINI", "openai": "OPENAI", "groq": "GROQ"}
    if provider not in prefixes:
        raise HTTPException(400, "Provider must be 'gemini', 'groq' or 'openai'.")
    prefix = prefixes[provider]

    key = (req.api_key or "").strip()
    key_name = f"{prefix}_API_KEY"
    # Switching to a provider whose key is already stored must not demand the
    # key again — retyping a secret you already saved is not a security step,
    # it is just a reason to keep the wrong provider selected.
    if not key and not user_config.get(key_name):
        raise HTTPException(400, "Paste an API key first.")

    values = {} if req.as_fallback else {"LLM_PROVIDER": provider}
    if key:
        values[key_name] = key
    if req.model:
        model = req.model.strip()
        if provider == "gemini":
            # Prove it works before storing it, so a dead model is caught here
            # rather than partway through a render that already cost a
            # download and a transcription.
            from shorts_generator.local.llm import check_gemini_model
            problem = await asyncio.to_thread(check_gemini_model, model)
            if problem:
                raise HTTPException(400, problem)
        values[f"{prefix}_MODEL"] = model
    if req.daily_limit is not None:
        cap = req.daily_limit.strip()
        if cap and not cap.isdigit():
            raise HTTPException(400, "The daily cap must be a whole number of requests.")
        values[f"{prefix}_DAILY_LIMIT"] = cap

    path = user_config.save(values)
    return {"saved": True, "config_path": str(path),
            "has_key": user_config.has_llm_key()}


class ProcessorRequest(BaseModel):
    mode: str = "auto"


@app.get("/api/processor")
async def get_processor(recheck: bool = False) -> dict:
    """What transcription and video encoding will run on, and why.

    Finding the video encoder runs a one-second test encode, so it is done in
    a thread and remembered; `recheck` forgets the answer and looks again,
    for someone who has just installed a driver.
    """
    from shorts_generator import accel
    if recheck:
        await asyncio.to_thread(accel.probe_video, True)
    state = await asyncio.to_thread(accel.status)
    state["pinned"] = bool(os.getenv("PROCESSOR", "").strip())
    return state


@app.post("/api/processor")
async def set_processor(req: ProcessorRequest) -> dict:
    """Choose auto, gpu or cpu. Applies from the next clip, even mid-run."""
    from shorts_generator import accel, user_config
    mode = (req.mode or "").strip().lower()
    if mode not in accel.MODES:
        raise HTTPException(400, "Processor must be auto, gpu or cpu.")
    user_config.save({"PROCESSOR": mode})
    return await get_processor()


class YoutubeAccessRequest(BaseModel):
    mode: str = "auto"
    browser: str = ""
    file: str = ""


@app.get("/api/youtube-access")
async def get_youtube_access(recheck: bool = False) -> dict:
    """How the app gets past YouTube's "confirm you're not a bot" gate.

    Probing a browser reads its whole cookie database, so the answers are
    cached; `recheck` throws them away and looks again, for someone who has
    just signed in to a browser or dropped a cookies.txt into the folder.
    """
    from shorts_generator.local import yt_access

    if recheck:
        await asyncio.to_thread(yt_access.forget_probes)

    cfg = yt_access.settings()
    browsers = await asyncio.to_thread(yt_access.working_browsers)
    discovered = await asyncio.to_thread(yt_access.discovered_cookie_file)

    # The configured file is checked; a merely discovered one is not, because
    # the check parses the whole jar and nobody asked about that file yet.
    chosen = cfg["file"]
    check = await asyncio.to_thread(yt_access.check_file, chosen) if chosen else None

    return {
        "mode": cfg["mode"],
        "browser": cfg["browser"],
        "file": chosen,
        "file_check": check,
        "browsers": browsers,
        "discovered": discovered,
        "drop_folder": (yt_access.cookie_file_places() or [""])[0],
        # An env var outranks the settings file, exactly as it does for API
        # keys, and the UI needs to say so rather than let someone change a
        # setting that is being overridden.
        "pinned": bool(os.getenv("YTDLP_COOKIES", "").strip()),
    }


@app.post("/api/youtube-access")
async def set_youtube_access(req: YoutubeAccessRequest) -> dict:
    """Choose how to answer the gate, proving the choice works before saving it.

    Everything here is validated up front. Somebody setting this is already
    dealing with a failed download; storing a browser that cannot be read, or a
    file that turns out to be the wrong format, would hand them the same
    failure again twenty minutes later with nothing new to go on.
    """
    from shorts_generator import user_config
    from shorts_generator.local import yt_access

    mode = (req.mode or "auto").strip().lower()
    if mode not in yt_access.MODES:
        raise HTTPException(400, "Mode must be auto, off, browser or file.")

    values = {"YOUTUBE_COOKIES_MODE": mode}

    if mode == "browser":
        browser = (req.browser or "").strip()
        if not browser:
            raise HTTPException(400, "Pick a browser first.")
        await asyncio.to_thread(yt_access.forget_probes)
        found = await asyncio.to_thread(yt_access.probe, browser.partition(":")[0])
        if not found["ok"]:
            raise HTTPException(400, found["reason"] or
                                f"No YouTube cookies could be read from {browser}.")
        values["YOUTUBE_COOKIES_BROWSER"] = browser

    elif mode == "file":
        check = await asyncio.to_thread(yt_access.check_file, req.file or "")
        if not check["ok"]:
            raise HTTPException(400, check["reason"] or "That file can't be used.")
        values["YOUTUBE_COOKIES_FILE"] = check["path"]

    await asyncio.to_thread(user_config.save, values)
    # The next request should start from the new setting, not from whatever
    # rung happened to work before it was changed.
    await asyncio.to_thread(yt_access.reset)
    return await get_youtube_access()


class LocationRequest(BaseModel):
    path: str


class RevealRequest(BaseModel):
    path: str


@app.get("/api/locations")
async def get_locations() -> dict:
    from shorts_generator import user_config

    return {
        "root": str(user_config.output_root()),
        "source": str(user_config.source_dir()),
        "shorts": str(user_config.shorts_dir()),
    }


@app.post("/api/locations")
async def set_location(req: LocationRequest) -> dict:
    """Move where future videos are saved. Existing files stay put."""
    from shorts_generator import user_config

    path = (req.path or "").strip().strip('"')
    if not path:
        raise HTTPException(400, "Enter a folder path.")
    try:
        root = await asyncio.to_thread(user_config.set_output_root, path)
    except ValueError as e:
        raise HTTPException(400, str(e))

    return {
        "saved": True,
        "root": str(root),
        "source": str(user_config.source_dir()),
        "shorts": str(user_config.shorts_dir()),
    }


# Downloaded sources are the only big thing the app leaves behind: a two-hour
# stream is several GB, and it is dead weight once the clips are rendered.
_CLEANABLE_MEDIA = {".mp4", ".mkv", ".webm", ".mov", ".m4a", ".wav", ".aac"}
_CLEANABLE_PARTIAL = {".part", ".ytdl", ".temp"}


def _cleanup_scan() -> list:
    """Reclaimable files in the source folder.

    Deliberately narrow: the downloaded videos and half-finished downloads,
    nothing else. Rendered clips live in a different folder and are the whole
    point of the app. Transcripts stay too — a .srt is a few hundred KB and
    saves re-transcribing hours of audio on the next run.
    """
    from shorts_generator import user_config

    src = user_config.source_dir().resolve()
    items = []
    for f in sorted(src.iterdir()):
        if not f.is_file():
            continue
        name = f.name.lower()
        if f.suffix.lower() in _CLEANABLE_PARTIAL or name.endswith(".mp4.part"):
            kind = "partial"
        elif f.suffix.lower() in _CLEANABLE_MEDIA:
            kind = "source"
        else:
            continue
        try:
            size = f.stat().st_size
        except OSError:
            continue
        items.append({"name": f.name, "path": str(f), "bytes": size, "kind": kind})
    return items


@app.get("/api/cleanup")
async def cleanup_scan() -> dict:
    """What a cleanup would remove, so the UI can show it before asking."""
    from shorts_generator import user_config

    items = await asyncio.to_thread(_cleanup_scan)
    return {
        "items": items,
        "count": len(items),
        "bytes": sum(i["bytes"] for i in items),
        "folder": str(user_config.source_dir()),
    }


@app.post("/api/cleanup")
async def cleanup_run() -> dict:
    """Delete those files. Refuses while a job is in flight, since the source
    it is reading from is exactly what would be removed."""
    busy = [j for j in STORE.list() if j["status"] in ("queued", "running")]
    if busy:
        raise HTTPException(409, "A job is still running — wait for it to finish, then clear space.")

    items = await asyncio.to_thread(_cleanup_scan)
    freed = 0
    removed, failed = [], []
    for it in items:
        try:
            Path(it["path"]).unlink()
        except OSError as e:
            failed.append(f"{it['name']}: {e.strerror or e}")
            continue
        freed += it["bytes"]
        removed.append(it["name"])
    return {"freed": freed, "removed": removed, "failed": failed, "count": len(removed)}


def _select_in_explorer(target: Path) -> bool:
    """Select one file in Explorer through the shell API. True if it worked.

    Not `explorer /select,<path>`. That form hands Explorer a command line it
    parses itself, splitting on the comma that introduces the argument — so
    the moment a filename contains a comma, Explorer takes everything after it
    as a separate argument and opens Documents instead. Clips are named after
    their own titles now, and titles are full of commas, apostrophes and
    ampersands; the button stopped landing on the file the day that shipped.

    SHOpenFolderAndSelectItems is what Explorer uses internally. It takes the
    path as data rather than as text to be re-parsed, so no character in a
    filename can change what it means.
    """
    import ctypes
    from ctypes import wintypes

    shell32 = ctypes.windll.shell32
    ole32 = ctypes.windll.ole32

    shell32.ILCreateFromPathW.restype = ctypes.c_void_p
    shell32.ILCreateFromPathW.argtypes = [wintypes.LPCWSTR]
    shell32.SHOpenFolderAndSelectItems.argtypes = [
        ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p, wintypes.DWORD,
    ]
    # The id list comes from the COM task allocator, so that is what frees it.
    # (shell32 exports ILFree too, but not on every Windows build.)
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]

    # Explorer is a COM apartment; the call needs one on this thread too.
    ole32.CoInitialize(None)
    item = shell32.ILCreateFromPathW(str(target))
    if not item:
        return False
    try:
        return shell32.SHOpenFolderAndSelectItems(item, 0, None, 0) == 0
    finally:
        ole32.CoTaskMemFree(item)


def _open_in_file_manager(target: Path) -> None:
    """Show `target` in the OS file manager.

    A file gets *selected* in its folder rather than opened — the point is to
    land on it, not to launch it in a video player.
    """
    import subprocess
    import sys as _sys

    is_file = target.is_file()
    if os.name == "nt":
        if is_file:
            try:
                if _select_in_explorer(target):
                    return
            except Exception as e:
                print(f"[reveal] could not select {target.name} ({e}); "
                      f"opening its folder instead", flush=True)
            # Whatever went wrong, landing in the right folder still answers
            # "where is this on my PC" — which is the whole question.
            os.startfile(str(target.parent))    # noqa: S606 - local desktop app
        else:
            os.startfile(str(target))       # noqa: S606 - local desktop app
    elif _sys.platform == "darwin":
        subprocess.run(["open", "-R", str(target)] if is_file else ["open", str(target)],
                       check=True)
    else:
        # No portable "select this file" on Linux; the containing folder is
        # the next best thing.
        subprocess.run(["xdg-open", str(target if not is_file else target.parent)],
                       check=True)


class AttentionRequest(BaseModel):
    watching: bool = False


@app.post("/api/attention")
async def attention(req: AttentionRequest) -> dict:
    """The page reporting whether anybody is actually looking at it.

    A heartbeat rather than a switch. It arrives every twenty seconds while the
    app is on screen and focused, and immediately when that stops being true.

    A run that finishes raises a desktop notification only when these have gone
    quiet, or last said no — which is also exactly what a closed window looks
    like from in here, because a page that is gone stops reporting. That is the
    point: there is no other way for the server to tell the difference between
    somebody watching a render and somebody who left an hour ago.
    """
    notify.mark_watching(req.watching)
    return {"ok": True}


@app.post("/api/reveal")
async def reveal(req: RevealRequest) -> dict:
    """Open a folder in the OS file manager.

    Only paths inside the configured save location are allowed — this runs a
    local command, so it must never be steerable to an arbitrary path.
    """
    from shorts_generator import user_config

    target = Path(req.path or "").expanduser()
    if not target.exists():
        raise HTTPException(404, "That folder doesn't exist yet.")
    if target.is_file():
        target = target.parent

    root = user_config.output_root().resolve()
    try:
        target.resolve().relative_to(root)
    except ValueError:
        raise HTTPException(403, "That folder is outside your save location.")

    try:
        await asyncio.to_thread(_open_in_file_manager, target)
    except Exception as e:
        raise HTTPException(500, f"Could not open the folder: {e}")
    return {"opened": str(target)}


class ShowRequest(BaseModel):
    file: Optional[str] = None


@app.post("/api/jobs/{job_id}/reveal")
async def reveal_clip(job_id: str, req: ShowRequest) -> dict:
    """Show one clip (or its run's folder) in the file manager.

    Addressed by job and filename rather than by a path from the browser: the
    server already knows where a job's clips live, so nothing the page sends
    can point this at somewhere else on the disk. It also means a run made
    before the save location moved still opens where its files actually are.
    """
    job = _job_or_404(job_id)

    if req.file:
        safe = os.path.basename(req.file)
        if STORE.clip(job, safe) is None:
            raise HTTPException(404, "No such clip")
        target = _clip_path(job, safe)
        if not target.exists():
            raise HTTPException(404, "That clip's file is missing — it may have been moved.")
    else:
        target = Path(job.out_dir)
        if not target.exists():
            raise HTTPException(404, "That run's folder is gone.")

    try:
        await asyncio.to_thread(_open_in_file_manager, target)
    except Exception as e:
        raise HTTPException(500, f"Could not show that file: {e}")
    return {"opened": str(target), "folder": str(target.parent if req.file else target)}


# Every external page this app will open, chosen by name. The page sends a
# key, never a URL: a browser-supplied address here would let anything
# rendered in the webview open an arbitrary site in the user's real browser.
EXTERNAL_LINKS = {
    "upload": "https://www.youtube.com/upload",
    "author-youtube": "https://www.youtube.com/@ParthBhadana799",
    "author-github": "https://github.com/kleZ799",
    "author-linkedin": "https://www.linkedin.com/in/parth-bhadana-530014202/",
    "author-discord": "https://discord.gg/jnMrGbBz3m",
    "repo": "https://github.com/kleZ799/clipmint",
    "donate": "https://buymeacoffee.com/parthbhadana",
    "releases": "https://github.com/kleZ799/clipmint/releases/latest",
}


@app.post("/api/open-upload")
async def open_upload(what: str = "upload") -> dict:
    """Open one of a known set of pages in the user's real browser.

    The URL is looked up, never received. This opens a page outside the app's
    own window, so it must not be steerable by anything the page sends.
    """
    import webbrowser

    url = EXTERNAL_LINKS.get(what)
    if url is None:
        raise HTTPException(400, "Unknown link.")
    ok = await asyncio.to_thread(webbrowser.open, url)
    return {"opened": ok, "url": url}


# --- uploading to YouTube -------------------------------------------------
#
# Through YouTube's own API, signed in on Google's own page. See
# webapp/youtube_upload.py for why it is done this way and no other.

class YoutubeClientRequest(BaseModel):
    # The JSON Google hands out for a Desktop app client, pasted or as a path.
    # Empty goes back to the client the build carries.
    client: str = ""


class YoutubeUploadRequest(BaseModel):
    title: str
    description: str = ""
    tags: Optional[object] = None
    privacy: str = "public"
    # An ISO time with its zone. Set means scheduled, whatever privacy says.
    publish_at: Optional[str] = None
    made_for_kids: bool = False
    category: str = "22"


class YoutubeOpenRequest(BaseModel):
    video_id: str
    where: str = "studio"


@app.get("/api/youtube")
async def youtube_status() -> dict:
    from . import youtube_upload
    return await asyncio.to_thread(youtube_upload.status)


@app.post("/api/youtube/client")
async def youtube_client(req: YoutubeClientRequest) -> dict:
    from . import youtube_upload
    try:
        return await asyncio.to_thread(youtube_upload.set_client, req.client)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/youtube/connect")
async def youtube_connect(request: Request) -> dict:
    """Open Google's consent page in the user's real browser.

    Google turns sign-ins away inside embedded webviews, so this is never the
    app window. The redirect comes back to this same local server, on the
    loopback address Google allows for desktop apps.
    """
    import webbrowser
    from . import youtube_upload

    port = request.url.port or 80
    redirect = f"http://127.0.0.1:{port}/api/youtube/callback"
    try:
        url = await asyncio.to_thread(youtube_upload.begin_connect, redirect)
    except youtube_upload.NotConnected as e:
        raise HTTPException(400, str(e))
    await asyncio.to_thread(webbrowser.open, url)
    return await asyncio.to_thread(youtube_upload.status)


@app.get("/api/youtube/callback", response_class=HTMLResponse)
async def youtube_callback(state: str = "", code: str = "", error: str = "") -> HTMLResponse:
    """Where Google sends the browser after the consent page."""
    import html
    from . import youtube_upload

    problem = await asyncio.to_thread(youtube_upload.finish_connect, state, code, error)
    if problem:
        head, text = "YouTube wasn't connected", problem
    else:
        who = youtube_upload.status().get("channel_title")
        head = f"Connected to {who}" if who else "YouTube is connected"
        text = "You can close this tab and go back to ClipMint."
    page = f"""<!doctype html><meta charset="utf-8"><title>ClipMint</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;background:#0f0f0f;color:#f1f1f1;
display:grid;place-items:center;min-height:100vh;margin:0;padding:16px}}
main{{max-width:460px;text-align:center}}h1{{font-size:22px}}p{{color:#aaa}}</style>
<main><h1>{html.escape(head)}</h1><p>{html.escape(text)}</p></main>"""
    return HTMLResponse(page, status_code=200 if not problem else 400)


@app.post("/api/youtube/disconnect")
async def youtube_disconnect() -> dict:
    """Give the permission back, and forget every upload record with it."""
    from . import youtube_upload
    await asyncio.to_thread(STORE.forget_youtube)
    return await asyncio.to_thread(youtube_upload.disconnect)


@app.post("/api/jobs/{job_id}/clips/{filename}/youtube")
async def youtube_upload_clip(job_id: str, filename: str, req: YoutubeUploadRequest) -> dict:
    """Send one clip to the connected channel, in the background.

    The metadata is whatever is in the boxes when Upload is pressed, not what
    was last saved -- what the person can see is what goes to YouTube.
    """
    from . import youtube_upload

    job = _job_or_404(job_id)
    safe = os.path.basename(filename)
    if STORE.clip(job, safe) is None:
        raise HTTPException(404, "No such clip")
    path = _clip_path(job, safe)
    if not path.exists():
        raise HTTPException(404, "That clip's file is missing — it may have been moved.")

    try:
        meta = youtube_upload.build_metadata(
            req.title, req.description, req.tags, req.privacy, req.publish_at,
            req.made_for_kids, req.category)
    except ValueError as e:
        raise HTTPException(400, str(e))

    def record(result: dict) -> None:
        # By file name as it is now: a title saved mid-upload renames the file.
        name = safe if STORE.clip(job, safe) else next(
            (c.get("file") for c in job.clips
             if (c.get("seo") or {}).get("title") == meta["snippet"]["title"]), None)
        if name:
            STORE.replace_clip(job, name, {"youtube": result})

    try:
        uid = youtube_upload.start_upload(f"{job.id}/{safe}", path, meta, record)
    except youtube_upload.NotConnected as e:
        raise HTTPException(409, str(e))
    return youtube_upload.upload_status(uid) or {"id": uid}


@app.get("/api/youtube/uploads/{upload_id}")
async def youtube_upload_progress(upload_id: str) -> dict:
    from . import youtube_upload
    st = youtube_upload.upload_status(upload_id)
    if not st:
        raise HTTPException(404, "No such upload")
    return st


@app.post("/api/youtube/open")
async def youtube_open(req: YoutubeOpenRequest) -> dict:
    """Open an uploaded video in the user's browser, by its id only."""
    import webbrowser

    if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", req.video_id or ""):
        raise HTTPException(400, "That isn't a YouTube video id.")
    url = (f"https://studio.youtube.com/video/{req.video_id}/edit" if req.where == "studio"
           else f"https://youtube.com/shorts/{req.video_id}")
    ok = await asyncio.to_thread(webbrowser.open, url)
    return {"opened": ok, "url": url}


def _override_aspect(spec: LayoutSpec, aspect_ratio: Optional[str]) -> None:
    """Let an explicit UI pick beat whatever the prompt implied.

    A toggle the user clicked is a clearer statement of intent than a word the
    parser inferred, so it wins. An unknown ratio is ignored rather than
    clamped to 9:16, so a stale client cannot silently change the shape of
    someone's clips.
    """
    if aspect_ratio and aspect_ratio in ASPECT_PRESETS:
        spec.aspect_ratio = aspect_ratio
        spec.validate()


def _override_kind(spec: LayoutSpec, kind: Optional[str]) -> None:
    """Let the picker say what kind of video this is, over the prompt's words.

    "Automatic" is not an override: it leaves whatever the words said, the
    same way "From my words" does for the shape.
    """
    from shorts_generator import content_kinds

    kind = content_kinds.normalise(kind)
    if kind == content_kinds.AUTO:
        return
    spec.content_kind = kind
    if spec.apply_kind():
        spec.notes.append(f"framing → follows the face, for a {content_kinds.LABELS[kind]}")
    spec.validate()


@app.post("/api/layout/preview")
async def layout_preview(req: LayoutPreviewRequest) -> dict:
    """Parse a layout prompt without running anything, so the UI can show
    the user what their words actually did before they commit to a render."""
    spec = await asyncio.to_thread(parse_layout_prompt, req.prompt, None, req.use_llm)
    _override_aspect(spec, req.aspect_ratio)
    _override_kind(spec, req.content_kind)
    return {"spec": spec.to_dict(), "summary": spec.describe(),
            "notes": spec.notes, "warning": spec.warning()}


@app.post("/api/resolve")
async def resolve(req: ResolveRequest) -> dict:
    """Classify a pasted link: single video, or a channel to pick from."""
    from shorts_generator.local.downloader import is_channel_or_playlist, list_channel_videos

    url = req.url.strip()
    if not url:
        raise HTTPException(400, "No URL given")

    if not is_channel_or_playlist(url):
        return {"type": "video", "source": url}

    try:
        videos = await asyncio.to_thread(list_channel_videos, url, 12)
    except Exception as e:
        raise HTTPException(400, f"Could not read that channel: {e}")
    if not videos:
        raise HTTPException(400, "No videos found at that link.")
    return {"type": "channel", "videos": videos}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)) -> dict:
    """Accept a dropped video file and return the path to run it from."""
    name = os.path.basename(file.filename or "upload.mp4")
    if Path(name).suffix.lower() not in VIDEO_SUFFIXES:
        raise HTTPException(400, f"{Path(name).suffix or 'That file type'} isn't a video format I can read.")

    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    dest = UPLOAD_DIR / safe
    n = 1
    while dest.exists():
        dest = UPLOAD_DIR / f"{Path(safe).stem}_{n}{Path(safe).suffix}"
        n += 1

    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    return {"type": "video", "source": str(dest.resolve()), "name": dest.name,
            "size": dest.stat().st_size}


@app.post("/api/jobs")
async def create_job(req: JobRequest) -> dict:
    source = req.source.strip()
    if not source:
        raise HTTPException(400, "No source given")

    spec = await asyncio.to_thread(parse_layout_prompt, req.prompt, None, req.use_llm)
    _override_aspect(spec, req.aspect_ratio)
    _override_kind(spec, req.content_kind)
    if req.hook_replay is not None:
        spec.hook_replay = bool(req.hook_replay)
    if req.num_clips:
        spec.num_clips = req.num_clips
        spec.validate()

    job = STORE.create(source, spec, download_format=req.download_format,
                       language=req.language)
    return job.snapshot()


@app.get("/api/jobs")
async def list_jobs() -> dict:
    return {"jobs": STORE.list()}


@app.get("/api/library")
async def library() -> dict:
    """Every clip this app has ever made that is still on disk.

    Rescans the folder first, so clips rendered by an earlier session — or by
    a copy of the app that was closed and reopened — come back rather than
    disappearing with the process that made them.
    """
    await asyncio.to_thread(STORE.restore)
    runs = []
    for snap in STORE.list():
        clips = [c for c in snap["clips"] if c.get("url")]
        if not clips:
            continue
        runs.append({
            "id": snap["id"],
            "created_at": snap["created_at"],
            "source": snap["source"],
            "source_title": snap["source_title"],
            "shorts_dir": snap["shorts_dir"],
            "restored": snap["restored"],
            "clips": clips,
        })
    return {"runs": runs, "count": sum(len(r["clips"]) for r in runs)}


@app.post("/api/jobs/{job_id}/seo")
async def write_seo(job_id: str, force: bool = False, only: Optional[str] = None) -> dict:
    """Write (or rewrite) the upload metadata for a job's clips.

    Runs on demand rather than at listing time: it costs an LLM call, and for
    clips old enough to have no transcript on record it costs a short
    transcription too. `only` names one clip's file to rewrite on its own.
    """
    job = _job_or_404(job_id)
    if not job.clips:
        raise HTTPException(409, "This run has no clips to write metadata for.")
    if only is not None:
        only = os.path.basename(only)
        if STORE.clip(job, only) is None:
            raise HTTPException(404, "No such clip")
    try:
        written = await asyncio.to_thread(regenerate_seo, STORE, job, force or bool(only),
                                          only)
    except Exception as e:
        raise HTTPException(500, f"Could not write the metadata: {e}")
    return {"written": written, "clips": job.clips}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    job = STORE.get(job_id)
    if not job:
        raise HTTPException(404, "No such job")
    return job.snapshot()


@app.get("/api/jobs/{job_id}/stream")
async def stream_job(job_id: str) -> StreamingResponse:
    """Server-sent events: one message per state change, then close."""
    job = STORE.get(job_id)
    if not job:
        raise HTTPException(404, "No such job")

    async def gen():
        last = -1
        idle = 0
        while True:
            snap = job.snapshot()
            if snap["version"] != last:
                last = snap["version"]
                idle = 0
                yield f"data: {json.dumps(snap)}\n\n"
            else:
                idle += 1
                if idle % 30 == 0:      # keep-alive comment every ~15s
                    yield ": ping\n\n"
            if snap["status"] in ("done", "error"):
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


@app.get("/api/jobs/{job_id}/clips/{filename}")
async def get_clip(job_id: str, filename: str) -> FileResponse:
    job = STORE.get(job_id)
    if not job:
        raise HTTPException(404, "No such job")

    # Never let a filename escape the job's own directory. _clip_path is the
    # one containment check in this file: comparing resolved paths as strings
    # treats a sibling folder whose name merely starts with this one's as being
    # inside it, which is the classic way a prefix check lets something through.
    safe = os.path.basename(filename)
    path = _clip_path(job, safe)
    if not path.exists():
        raise HTTPException(404, "No such clip")

    return FileResponse(path, media_type="video/mp4", filename=safe)


# --- clip editing ---------------------------------------------------------
#
# Everything below works on clips a finished job already produced, so the user
# can fix a cut without re-running the whole pipeline.

# The longest span the trim editor will re-cut. Deliberately not the same
# number as highlights.MAX_CLIP_SECONDS, which caps what the *ranker* may
# hand back -- a hand-driven trim is allowed to be longer than anything the
# model is trusted to propose.
MAX_TRIM_SECONDS = 300


class TrimRequest(BaseModel):
    start: float
    end: float
    mute: bool = False


class SaveClipRequest(BaseModel):
    name: Optional[str] = None


def _job_or_404(job_id: str):
    job = STORE.get(job_id)
    if not job:
        raise HTTPException(404, "No such job")
    return job


def _clip_path(job, filename: str) -> Path:
    """Resolve a clip filename inside its job directory, and nowhere else."""
    safe = os.path.basename(filename)
    root = Path(job.out_dir).resolve()
    path = (root / safe).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise HTTPException(403, "That file is outside the job folder.")
    return path


def _strip_audio(path: Path) -> None:
    """Rewrite a clip without its audio track, in place."""
    import subprocess

    tmp = path.with_name(path.stem + "_muted" + path.suffix)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path),
         "-c", "copy", "-an", "-movflags", "+faststart", str(tmp)],
        check=True,
    )
    os.replace(tmp, path)


@app.post("/api/jobs/{job_id}/clips/{filename}/trim")
async def trim_clip(job_id: str, filename: str, req: TrimRequest) -> dict:
    """Re-cut one clip at new timestamps, straight from the downloaded source.

    Re-rendering (rather than trimming the rendered file) means the span can
    grow as well as shrink, and the layout stays exactly what the user asked
    for the first time round.
    """
    from shorts_generator.render import render_highlights

    job = _job_or_404(job_id)
    clip = STORE.clip(job, os.path.basename(filename))
    if clip is None:
        raise HTTPException(404, "No such clip")
    if not job.source_path or not os.path.exists(job.source_path):
        raise HTTPException(409, "The source video for this job is gone — re-run it to edit.")

    start, end = float(req.start), float(req.end)
    if start < 0:
        raise HTTPException(400, "Start can't be before the beginning of the video.")
    if end - start < 1.0:
        raise HTTPException(400, "A clip needs to be at least a second long.")
    if end - start > MAX_TRIM_SECONDS:
        raise HTTPException(400, f"Keep clips under {MAX_TRIM_SECONDS // 60} minutes.")

    old_path = _clip_path(job, clip["file"])
    highlight = {
        "title": clip.get("title") or "Clip",
        "start_time": start,
        "end_time": end,
        "score": clip.get("score"),
        "hook_sentence": clip.get("hook_sentence") or "",
        "virality_reason": clip.get("virality_reason") or "",
    }
    prefix = f"edit_{clip.get('index', 1):02d}_{int(time.time())}"

    def _render():
        out = render_highlights(job.source_path, [highlight], job.spec,
                                out_dir=job.out_dir, name_prefix=prefix)
        return out[0] if out else {}

    try:
        result = await asyncio.to_thread(_render)
    except Exception as e:
        raise HTTPException(500, f"Could not re-cut that clip: {e}")

    new_path = result.get("clip_url")
    if not new_path or not os.path.exists(new_path):
        raise HTTPException(500, result.get("error") or "The re-cut produced no file.")

    if req.mute:
        try:
            await asyncio.to_thread(_strip_audio, Path(new_path))
        except Exception as e:
            raise HTTPException(500, f"Could not mute that clip: {e}")

    # The old render is dead weight once the re-cut exists, and clearing it
    # first frees its name: a re-cut of "My Title.mp4" should be called
    # "My Title.mp4" again, not pushed to "My Title_2.mp4" by its own predecessor.
    if old_path.name != os.path.basename(new_path):
        with contextlib.suppress(OSError):
            old_path.unlink()

    seo = clip.get("seo") or {}
    new_name = rename_to_title(Path(job.out_dir), os.path.basename(new_path),
                               seo.get("title") or clip.get("title") or "")

    updated = STORE.replace_clip(job, clip["file"], {
        "file": new_name,
        "url": f"/api/jobs/{job.id}/clips/{new_name}",
        "start_time": start,
        "end_time": end,
        "duration": round(end - start, 1),
        "muted": bool(req.mute),
        "edited": True,
    })

    return updated or {}


class SeoEditRequest(BaseModel):
    """A clip's upload metadata as the user typed it.

    Every field is optional and only what is sent gets changed, so the title
    box alone can be saved without the description riding along behind it.
    """
    title: Optional[str] = None
    description: Optional[str] = None
    # Accepted as the comma-separated string the tag box actually contains,
    # or as a list, so both the UI and a script can post here.
    tags: Optional[object] = None
    hook_text: Optional[str] = None
    # Instagram Reels and TikTok, as typed. Hashtags arrive as the one line
    # the box shows ("#a #b") or as a list.
    reels_caption: Optional[str] = None
    reels_hashtags: Optional[object] = None
    reels_cover_text: Optional[str] = None
    reels_alt_text: Optional[str] = None
    tiktok_caption: Optional[str] = None
    tiktok_hashtags: Optional[object] = None
    # What the clip is actually about, when the app got it wrong -- "Fear to
    # Fathom", not the game it guessed. Saved on the clip, not in the text,
    # and every rewrite after this files the clip under it. Empty clears it.
    subject: Optional[str] = None


@app.put("/api/jobs/{job_id}/clips/{filename}/seo")
async def edit_seo(job_id: str, filename: str, req: SeoEditRequest) -> dict:
    """Save metadata the user wrote themselves.

    The generated title is a starting point, not a verdict — nobody knows
    their own channel better than the person running it. Saving one renames
    the file too, exactly as a rewrite does, so what is on disk keeps matching
    what will be typed into YouTube.
    """
    from shorts_generator.seo import apply_edit

    job = _job_or_404(job_id)
    safe = os.path.basename(filename)
    clip = STORE.clip(job, safe)
    if clip is None:
        raise HTTPException(404, "No such clip")

    edit = {k: v for k, v in req.model_dump().items() if v is not None}
    if not edit:
        raise HTTPException(400, "Nothing to save.")

    subject = edit.pop("subject", None)
    seo = clip.get("seo") or {}
    if edit:
        try:
            seo = apply_edit(clip.get("seo"), edit)
        except ValueError as e:
            raise HTTPException(400, str(e))
        STORE.set_seo(job, safe, seo)
    if subject is not None:
        STORE.replace_clip(job, safe, {
            "subject_override": re.sub(r"\s+", " ", subject).strip()[:80]})

    updated = STORE.clip(job, safe)
    if "title" in edit:
        new_name = await asyncio.to_thread(
            rename_to_title, Path(job.out_dir), safe, seo.get("title") or "")
        if new_name != safe:
            updated = STORE.replace_clip(job, safe, {
                "file": new_name,
                "url": f"/api/jobs/{job.id}/clips/{new_name}",
            })
    return updated or {}


@app.delete("/api/jobs/{job_id}/clips/{filename}")
async def delete_clip(job_id: str, filename: str) -> dict:
    """Throw a clip away — file and all."""
    job = _job_or_404(job_id)
    safe = os.path.basename(filename)
    if STORE.clip(job, safe) is None:
        raise HTTPException(404, "No such clip")

    path = _clip_path(job, safe)
    with contextlib.suppress(OSError):
        path.unlink()
    STORE.remove_clip(job, safe)
    return {"deleted": safe, "remaining": len(job.clips)}


@app.post("/api/jobs/{job_id}/clips/{filename}/save")
async def save_clip(job_id: str, filename: str, req: SaveClipRequest) -> dict:
    """Copy a clip out of the job folder into the user's save location.

    Job folders are working space; this is how a clip the user actually wants
    ends up somewhere they'll find it later.
    """
    from shorts_generator import user_config

    job = _job_or_404(job_id)
    safe = os.path.basename(filename)
    if STORE.clip(job, safe) is None:
        raise HTTPException(404, "No such clip")

    src = _clip_path(job, safe)
    if not src.exists():
        raise HTTPException(404, "That clip's file is missing.")

    stem = re.sub(r"[^A-Za-z0-9 ._-]", "", (req.name or Path(safe).stem)).strip() or "short"
    dest_dir = user_config.shorts_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{stem}.mp4"
    n = 1
    while dest.exists():
        dest = dest_dir / f"{stem}_{n}.mp4"
        n += 1

    await asyncio.to_thread(shutil.copy2, src, dest)
    STORE.replace_clip(job, safe, {"saved_to": str(dest)})
    return {"saved": True, "path": str(dest), "folder": str(dest_dir)}



# --- trying again ---------------------------------------------------------

@app.post("/api/jobs/{job_id}/continue")
async def continue_job(job_id: str) -> dict:
    """Carry on a run the app never finished.

    Distinct from /resume, which lifts a pause. This one is for a run that
    was going when the app closed: it goes back on the queue and skips
    everything already cached on disk.
    """
    job = _job_or_404(job_id)
    try:
        job = STORE.carry_on(job)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return job.snapshot()


@app.post("/api/jobs/{job_id}/retry")
async def retry_job(job_id: str, clips_only: bool = False) -> dict:
    """Run a failed job again, or only the clips in a finished one that failed.

    Every stage already retries itself before giving up, so reaching here
    means something failed three times -- often something that has since been
    put right (a key fixed, a connection back, disk space freed). The caches
    make the second go start where the first stopped.
    """
    job = _job_or_404(job_id)
    try:
        job = STORE.retry_clips(job) if clips_only else STORE.retry(job)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return job.snapshot()


# --- pausing a run --------------------------------------------------------

@app.post("/api/jobs/{job_id}/pause")
async def pause_job(job_id: str) -> dict:
    """Suspend the work, rather than just declining to start more of it.

    Rendering pins every core it can, and someone whose machine has become
    unusable needs it back now — not after the current clip finishes.
    """
    job = STORE.get(job_id)
    if not job:
        raise HTTPException(404, "No such job")
    if job.status != "running":
        raise HTTPException(400, "That run is not going.")
    from shorts_generator import proc
    proc.pause()
    return {"paused": True}


@app.post("/api/jobs/{job_id}/resume")
async def resume_job(job_id: str) -> dict:
    job = STORE.get(job_id)
    if not job:
        raise HTTPException(404, "No such job")
    from shorts_generator import proc
    proc.resume()
    return {"paused": False}


# --- updates --------------------------------------------------------------
#
# The page never supplies a URL. It asks "is there an update", and asks the
# server to install "the update" -- which the server resolves for itself from
# the repository compiled into the build. Anything rendered in the window is
# therefore unable to point the updater at a file of its choosing.

def _public(state: dict) -> dict:
    """Drop the internal download fields before the state reaches the page."""
    return {k: v for k, v in state.items() if not k.startswith("_")}


@app.get("/api/version")
async def app_version() -> dict:
    """The running build's version. Local only -- no network, never fails."""
    return {"version": updater.APP_VERSION, "can_self_update": updater.can_self_update()}


@app.get("/api/update/check")
async def update_check() -> dict:
    return _public(await asyncio.to_thread(updater.check))


@app.post("/api/update/install")
async def update_install() -> dict:
    state = await asyncio.to_thread(updater.check)
    if state["status"] not in ("update", "rollback") or not state["can_install"]:
        raise HTTPException(400, state["reason"] or "There is nothing to install.")
    if not updater.INSTALL.start(state["_url"], state["_digest"]):
        raise HTTPException(409, "An update is already downloading.")
    return {"started": True, "version": state["latest"], "size": state["size"]}


@app.get("/api/update/progress")
async def update_progress() -> dict:
    return updater.INSTALL.snapshot()


@app.post("/api/update/apply")
async def update_apply() -> dict:
    """Swap in the verified build and relaunch. The app exits right after."""
    try:
        await asyncio.to_thread(updater.INSTALL.apply_and_restart)
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"restarting": True}


class RevalidatingStatic(StaticFiles):
    """StaticFiles that makes the browser check before reusing a file.

    Without a Cache-Control header a browser is free to invent its own
    freshness window, and it does. That is harmless for a website that gets
    reloaded, and wrong for this app: the packaged build keeps its WebView2
    cache across an upgrade, so someone moving from one version to the next
    can keep running the old UI out of cache and never see what changed --
    with no reload button anywhere in the window to break the tie.

    "no-cache" is not "do not store". The file stays cached; the browser just
    has to ask whether it is still current, and StaticFiles answers a matching
    ETag with an empty 304. The cost is one conditional request per file per
    launch, against a server on the same machine.
    """

    def file_response(self, *args, **kwargs) -> Response:
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/static", RevalidatingStatic(directory=str(STATIC_DIR)), name="static")
