"""The growth playbook the packaging writer works from.

How YouTube Shorts, Instagram Reels and TikTok decide who sees a video changes
several times a year. What the metadata writer knows about that should not be
frozen into whichever build someone happened to download, so it lives in a
Markdown file -- assets/playbook/short-form-algorithms.md -- rather than in
the prompt, and a running app can pick up a newer one without a release.

Three copies, most specific first:

1. `playbook.md` in the app's settings folder. Someone who keeps their own
   notes on what works for their channel gets exactly those, always.
2. A copy fetched from the repository's main branch, cached beside the
   settings. Taken only when its `reviewed:` date is newer than the bundled
   one, so an old cache can never roll a fresh build backwards.
3. The copy bundled with the build.

The fetch is the same trust as the updater: one fixed URL in this project's own
repository, text only, size-capped, and every failure silently leaves the copy
already on disk in charge. A playbook is advice to a model, and a run must
never wait on it or fail because GitHub is unreachable.
"""
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

from . import user_config
from .version import APP_VERSION, UPDATE_REPO

FILE_NAME = "short-form-algorithms.md"
REMOTE_URL = (f"https://raw.githubusercontent.com/{UPDATE_REPO}/main/"
              f"assets/playbook/{FILE_NAME}")
ALLOWED_HOST = "raw.githubusercontent.com"
OVERRIDE_NAME = "playbook.md"
CACHE_NAME = "playbook-cache.md"
# Algorithms change over months, not minutes. Once a day is plenty, and keeps
# the app from asking GitHub on every launch.
REFRESH_EVERY = 24 * 3600
MAX_BYTES = 64_000
TIMEOUT = 10

_REVIEWED = re.compile(r"^\s*reviewed:\s*(\d{4}-\d{2}-\d{2})\s*$", re.MULTILINE)
_lock = threading.Lock()


def bundled_path() -> Optional[Path]:
    """The copy shipped with this build, in a packaged app or a checkout."""
    candidates = []
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidates.append(Path(bundle) / "assets" / "playbook" / FILE_NAME)
    candidates.append(Path(__file__).resolve().parent.parent / "assets" / "playbook" / FILE_NAME)
    return next((p for p in candidates if p.exists()), None)


def reviewed(text: str) -> str:
    """The playbook's review date as YYYY-MM-DD, or "" when it has none."""
    m = _REVIEWED.search(text[:400] if text else "")
    return m.group(1) if m else ""


def _valid(text: Optional[str]) -> bool:
    return bool(text) and len(text.encode("utf-8")) <= MAX_BYTES and bool(reviewed(text))


def _read(path: Optional[Path]) -> Optional[str]:
    if not path or not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return None
    return text if _valid(text) else None


def load() -> Tuple[str, str]:
    """The playbook to use now, and where it came from."""
    try:
        own = _read(user_config.config_dir() / OVERRIDE_NAME)
        cached = _read(user_config.config_dir() / CACHE_NAME)
    except OSError:
        own = cached = None
    if own:
        return own, "your playbook.md"
    bundled = _read(bundled_path())
    if cached and (not bundled or reviewed(cached) > reviewed(bundled)):
        return cached, f"updated playbook ({reviewed(cached)})"
    if bundled:
        return bundled, f"bundled playbook ({reviewed(bundled)})"
    return "", "no playbook"


def prompt_text() -> str:
    """The playbook as the writer should read it: the advice, not the upkeep.

    The review date and the note on how to update the file are for whoever
    maintains it. To a model they are noise at best, and at worst an
    instruction to go and edit something.
    """
    text, _ = load()
    text = _REVIEWED.sub("", text, count=1)
    text = re.sub(r"\*\*Keeping it current\.\*\*.*?(?:\n\s*\n)", "", text, count=1, flags=re.DOTALL)
    return text.strip()


def refresh(force: bool = False) -> bool:
    """Fetch a newer playbook from the repository. True when one was saved."""
    if urllib.parse.urlsplit(REMOTE_URL).hostname != ALLOWED_HOST:
        return False
    with _lock:
        try:
            cache = user_config.config_dir() / CACHE_NAME
            if not force and cache.exists() and time.time() - cache.stat().st_mtime < REFRESH_EVERY:
                return False
            req = urllib.request.Request(REMOTE_URL, headers={
                "User-Agent": f"ClipMint/{APP_VERSION}"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read(MAX_BYTES + 1)
            text = raw.decode("utf-8")
        except Exception:
            return False

        if not _valid(text):
            return False
        have = max(reviewed(_read(bundled_path()) or ""), reviewed(_read(cache) or ""))
        try:
            if reviewed(text) > have:
                cache.write_text(text, encoding="utf-8")
                print(f"[seo] growth playbook updated to the {reviewed(text)} review", flush=True)
                return True
            # Nothing newer, but the check happened: wait a day before the next.
            if cache.exists():
                cache.touch()
            else:
                cache.write_text(_read(bundled_path()) or text, encoding="utf-8")
        except OSError:
            pass
        return False


def refresh_in_background() -> None:
    threading.Thread(target=refresh, name="playbook-refresh", daemon=True).start()
