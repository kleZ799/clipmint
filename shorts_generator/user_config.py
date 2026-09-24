"""Per-user settings stored outside the repo.

The .env file works fine when you cloned the source, but someone running a
packaged build has no repo to edit. This keeps their API key in the platform's
normal per-user config location instead, so the app can ask for it once in the
UI and remember it.

Precedence is deliberate: a real environment variable always wins, so an
existing .env setup keeps behaving exactly as before.
"""
import json
import os
import shutil
from pathlib import Path
from typing import Dict, Optional

# What this folder was called before the app was renamed to ClipMint. Anyone
# who installed 1.14.1 or earlier has their API key in there.
_LEGACY_DIR_NAME = "StreamToShorts"


def _adopt_legacy_config(base: Path, new: Path) -> None:
    """Copy a pre-rename config folder into the new one, once.

    Someone upgrading from StreamToShorts has their key, their save location
    and their usage ledger in the old folder. Renaming the app must not read
    to them as being logged out, so the first run under the new name brings
    the old settings across.

    It copies rather than moves: if this build turns out to be broken and
    they go back to the one they had, that install still finds its own
    config exactly where it left it. The cost is a few kilobytes duplicated.

    Only ever on a genuinely first run -- an existing new-name folder is the
    user's current truth and is never overwritten by a stale old one. Any
    failure here is silent on purpose; a missing copy costs them one paste
    of an API key, while a crash on startup costs them the app.
    """
    old = base / _LEGACY_DIR_NAME
    if new.exists() or not old.is_dir():
        return
    try:
        shutil.copytree(old, new)
        print(f"[config] carried settings over from {old}", flush=True)
    except OSError as e:
        print(f"[config] could not copy old settings from {old} ({e})", flush=True)


def config_dir() -> Path:
    """Per-user config directory, created on demand."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif os.uname().sysname == "Darwin":  # type: ignore[attr-defined]
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    d = Path(base) / "ClipMint"
    _adopt_legacy_config(Path(base), d)
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    return config_dir() / "settings.json"


# Why the last load() failed, if it did. This file holds the user's API key:
# reading it as {} because of a bad byte or a locked handle turns a fixable
# problem into "GEMINI_API_KEY is not set" twenty minutes into a run, pointing
# the user at a .env that was never involved. Remember the reason so the
# message that finally reaches them can name it.
_load_error: Optional[str] = None


def load_error() -> Optional[str]:
    """Why the stored config last failed to load, or None if it read cleanly."""
    return _load_error


def _note_load_failure(reason: Optional[str]) -> None:
    """Record why load() gave up, printing each distinct reason once."""
    global _load_error
    was, _load_error = _load_error, reason
    if reason and reason != was:
        print(f"[config] {reason}", flush=True)


def load() -> Dict:
    """Read the stored config, or {} if there is nothing readable there.

    A missing file is ordinary — a first run has none, and that is not worth
    a word. Everything else is: a file we can see but cannot use is a bug or
    a broken install, and staying quiet about it only moves the failure
    somewhere less obvious.
    """
    try:
        path = config_path()
    except OSError as e:
        _note_load_failure(f"config directory unavailable ({e.strerror or e})")
        return {}

    try:
        # utf-8-sig so a BOM left by a hand-edit doesn't read as a corrupt file.
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        _note_load_failure(None)
        return {}
    except OSError as e:
        _note_load_failure(f"{path} could not be read ({e.strerror or e})")
        return {}
    except (LookupError, UnicodeError) as e:
        _note_load_failure(f"{path} could not be decoded ({type(e).__name__}: {e})")
        return {}

    try:
        data = json.loads(raw)
    except ValueError as e:
        _note_load_failure(f"{path} is not valid JSON ({e})")
        return {}

    if not isinstance(data, dict):
        _note_load_failure(
            f"{path} holds {type(data).__name__}, expected a JSON object"
        )
        return {}

    _note_load_failure(None)
    return data


def save(values: Dict) -> Path:
    """Merge `values` into the stored config and write it back.

    Refuses to write when a config file exists but won't parse: this file holds
    the user's API key, and merging onto a silently-empty dict would drop it.
    """
    path = config_path()
    current = load()
    if not current and path.exists() and path.stat().st_size > 0:
        raise RuntimeError(
            f"{path} exists but {load_error() or 'could not be read'}. Refusing to "
            f"overwrite it and lose the settings it holds — fix or move the file, "
            f"then retry."
        )
    current.update({k: v for k, v in values.items() if v is not None})
    path.write_text(json.dumps(current, indent=2), encoding="utf-8")
    try:
        # The file holds an API key — keep it owner-only where that's meaningful.
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def get(key: str, default: str = "") -> str:
    """Environment first, then the stored config."""
    env = os.getenv(key, "").strip()
    if env:
        return env
    val = load().get(key)
    return str(val).strip() if val else default


# Where things land under the chosen root. Source videos keep the historical
# "output" name so transcripts already cached beside them stay valid.
SOURCE_SUBDIR = "output"
SHORTS_SUBDIR = "shorts"


def output_root() -> Path:
    """Root folder for everything this app writes. Defaults to the cwd."""
    configured = get("OUTPUT_ROOT")
    if configured:
        return Path(configured).expanduser()
    return Path.cwd()


def set_output_root(path: str) -> Path:
    """Point the app at a new save location, after proving we can write there."""
    p = Path(path).expanduser()
    if p.exists() and not p.is_dir():
        raise ValueError(f"{p} is a file, not a folder.")
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".clipmint-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        raise ValueError(f"Can't write to {p} ({e.strerror or e}).") from e

    resolved = p.resolve()
    save({"OUTPUT_ROOT": str(resolved)})
    return resolved


def source_dir() -> Path:
    """Where full downloaded videos and their transcripts live."""
    raw = os.getenv("LOCAL_OUTPUT_DIR", "").strip()
    if raw and Path(raw).is_absolute():
        d = Path(raw)
    else:
        d = output_root() / (raw or SOURCE_SUBDIR)
    d.mkdir(parents=True, exist_ok=True)
    return d


def shorts_dir() -> Path:
    """Where generated clips live, one subfolder per run."""
    d = output_root() / SHORTS_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    _write_folder_guide()
    return d


_GUIDE_NAME = "READ ME - what is in here.txt"

_GUIDE = """ClipMint keeps everything it makes in this folder.

  shorts
      Your finished clips, one folder per video, named after that video.
      This is the folder you want. Each run folder also holds a clips.json,
      which is how the app remembers titles, scores and spans -- delete it
      and the clips still play, but the app forgets what they were.

  output
      The full videos downloaded to cut those clips from, named after the
      video with its YouTube id in brackets, plus the transcripts made from
      them (a .json beside each video).

      These are the big files. Deleting them is safe and frees the most
      space -- clips you have already made are untouched. Re-running the same
      video downloads it again.

      output\b-roll holds stock footage fetched from Pexels for B-roll, if
      you turned that on. Safe to delete; it is fetched again when needed.

SAFE TO DELETE
  Anything inside "output". Any run folder inside "shorts" whose clips you no
  longer want. Settings has a "Clear space" button that does the first of
  those for you.

BEST LEFT ALONE
  clips.json inside a run folder, unless you are happy to lose the titles and
  rankings for those clips.

All of it was made on this PC. Nothing here is uploaded anywhere unless you
connect your YouTube channel in the app and press Upload on a clip.
"""


def _write_folder_guide() -> None:
    """Leave a short note in the output root saying what each folder is.

    People find this folder through Explorer long before they think to look
    for documentation, and a folder of multi-gigabyte files with no
    explanation is one people either hoard forever or clear out along with
    the clips they wanted to keep.
    """
    try:
        guide = output_root() / _GUIDE_NAME
        if not guide.exists():
            guide.write_text(_GUIDE, encoding="utf-8")
    except OSError:
        pass        # a note is a nicety, never a reason to fail


def has_llm_key() -> bool:
    provider = (get("LLM_PROVIDER", "gemini") or "gemini").lower()
    if provider == "openai":
        return bool(get("OPENAI_API_KEY"))
    return bool(get("GEMINI_API_KEY"))
