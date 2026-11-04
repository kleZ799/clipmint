"""Channel profiles: one app, several channels, each set up its own way.

Asked for by testers running more than one channel. A profile keeps what
belongs to a channel rather than to the app: the YouTube sign-in uploads go
to, the logo and its corner, the views read back from that channel, and (in
the page) the edit choices. Switching profile switches all of them at once.

The API keys, the clips folder and the clips themselves stay shared. The keys
pay for the work whichever channel it is for, and a clip is not tied to a
channel until it is uploaded -- the upload record already says which one.

The first profile is the folder the app has always used, so everything set up
before profiles existed is simply "Main", with nothing moved or copied. Every
other profile gets a folder of its own under profiles/.
"""
import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Dict, List

from . import user_config

MAIN = "main"
LIST_KEY = "PROFILES"
ACTIVE_KEY = "ACTIVE_PROFILE"
MAX_PROFILES = 12
NAME_CHARS = 40


def _clean_name(name: str) -> str:
    name = re.sub(r"\s+", " ", str(name or "")).strip()[:NAME_CHARS]
    if not name:
        raise ValueError("Give the profile a name, like the channel's.")
    return name


def _stored() -> List[Dict]:
    raw = user_config.load().get(LIST_KEY)
    try:
        items = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except ValueError:
        items = []
    out = [{"id": MAIN, "name": "Main"}]
    for p in items if isinstance(items, list) else []:
        if isinstance(p, dict) and p.get("id") == MAIN and p.get("name"):
            out[0]["name"] = str(p["name"])[:NAME_CHARS]
        elif (isinstance(p, dict) and re.fullmatch(r"[a-f0-9]{8}", str(p.get("id") or ""))
              and p.get("name")):
            out.append({"id": p["id"], "name": str(p["name"])[:NAME_CHARS]})
    return out


def _save(items: List[Dict]) -> None:
    user_config.save({LIST_KEY: json.dumps(items)})


def all_profiles() -> List[Dict]:
    return _stored()


def active_id() -> str:
    pid = user_config.get(ACTIVE_KEY, MAIN) or MAIN
    return pid if any(p["id"] == pid for p in _stored()) else MAIN


def active() -> Dict:
    pid = active_id()
    return next(p for p in _stored() if p["id"] == pid)


def folder(pid: str = "") -> Path:
    """Where a profile keeps its own files. Main is the settings folder itself."""
    pid = pid or active_id()
    base = user_config.config_dir()
    if pid == MAIN:
        return base
    path = base / "profiles" / pid
    path.mkdir(parents=True, exist_ok=True)
    return path


def key(name: str, pid: str = "") -> str:
    """A setting's name for this profile. Main keeps the plain name it always had."""
    pid = pid or active_id()
    return name if pid == MAIN else f"{name}__{pid}"


def create(name: str) -> Dict:
    items = _stored()
    if len(items) >= MAX_PROFILES:
        raise ValueError(f"{MAX_PROFILES} profiles is the most this app keeps.")
    profile = {"id": uuid.uuid4().hex[:8], "name": _clean_name(name)}
    items.append(profile)
    _save(items)
    return profile


def rename(pid: str, name: str) -> Dict:
    items = _stored()
    for p in items:
        if p["id"] == pid:
            p["name"] = _clean_name(name)
            _save(items)
            return p
    raise ValueError("No such profile.")


def switch(pid: str) -> Dict:
    if not any(p["id"] == pid for p in _stored()):
        raise ValueError("No such profile.")
    user_config.save({ACTIVE_KEY: pid})
    return active()


def remove(pid: str) -> None:
    """Forget a profile and delete its folder. Main cannot be removed.

    The caller withdraws its YouTube sign-in at Google first (see
    webapp.server), since only it knows how.
    """
    if pid == MAIN:
        raise ValueError("Main is the profile everything else falls back to, so it stays.")
    items = [p for p in _stored() if p["id"] != pid]
    if len(items) == len(_stored()):
        raise ValueError("No such profile.")
    _save(items)
    if active_id() == pid or user_config.get(ACTIVE_KEY) == pid:
        user_config.save({ACTIVE_KEY: MAIN})
    path = user_config.config_dir() / "profiles" / pid
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    # Its per-profile settings, so a later profile can never inherit them.
    current = user_config.load()
    stale = [k for k in current if k.endswith(f"__{pid}")]
    if stale:
        user_config.save({k: "" for k in stale})
