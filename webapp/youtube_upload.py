"""Uploading a finished clip straight to the user's YouTube channel.

This goes through YouTube's official Data API, and only that. The obvious
shortcut -- drive YouTube Studio's upload page with a scripted browser -- is
against YouTube's terms, breaks whenever Studio moves a button, and puts the
channel at risk. An API upload is the thing YouTube built for exactly this.

How the pieces fit:

  Signing in.  The user clicks Connect, their real browser opens Google's own
  consent page, and Google redirects back to this app's local server with a
  one-time code. The app never sees a password. What it keeps is a refresh
  token, in the per-user config folder beside the API keys, which can upload
  and read the channel's name and nothing else -- no deleting, no editing, no
  comments. The user can withdraw it at myaccount.google.com at any time.

  The client.  Google identifies an app by an OAuth client ID. A release
  build carries ClipMint's own, or the user pastes the JSON for one they made
  themselves. ClipMint's is never in this repository: YouTube's developer
  policies (III.D.1) forbid embedding API credentials in open source projects,
  so the release workflow writes it into the build from a GitHub secret. See
  build_exe.py and docs/youtube-upload.md.

  The policies.  Uploads happen only when the person presses Upload, with the
  words they can see in the boxes, and those words are never trimmed or
  rewritten on the way (III.C.3) -- anything YouTube would reject is refused
  here with the reason instead. Channel details are refreshed or dropped after
  30 days, and disconnecting forgets everything YouTube handed back (III.E.4).

  Uploading.  Google's resumable protocol: announce the file and its metadata,
  then send it in chunks, asking where to resume after anything goes wrong. A
  dropped connection halfway through a 200 MB clip costs one chunk, not the
  whole upload.

The one thing no code here can fix: a Google Cloud project that has not passed
YouTube's API audit has every upload locked to private, whatever was asked for.
The upload still succeeds, so after it finishes the app reads the video back
and says plainly when YouTube kept it private.
"""
import base64
import hashlib
import json
import os
import re
import secrets
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import urlencode

import requests

from shorts_generator import user_config
from shorts_generator.version import APP_VERSION

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
API_URL = "https://www.googleapis.com/youtube/v3"

# The least that does the job. upload is the whole point; readonly is there so
# the app can say which channel is connected -- someone with a brand channel
# and a personal one needs to see that before a clip lands on the wrong one --
# and so it can read a finished upload back to see whether YouTube kept it
# private.
SCOPE_UPLOAD = "https://www.googleapis.com/auth/youtube.upload"
SCOPE_READ = "https://www.googleapis.com/auth/youtube.readonly"
SCOPES = [SCOPE_UPLOAD, SCOPE_READ]

# Where a build keeps ClipMint's own Google client. Written by build_exe.py from
# the CLIPMINT_YOUTUBE_CLIENT secret and ignored by git; absent from a source
# checkout unless someone puts their own there.
BUILTIN_CLIENT_FILE = "youtube_client.json"

# How long anything YouTube handed back may be kept before it is refreshed or
# dropped. YouTube API Services Developer Policies, III.E.4.
KEEP_DAYS = 30

PRIVACY = ("public", "unlisted", "private")

# YouTube's categories that a Short plausibly belongs in, by the id the API
# wants. The full list varies by region; these exist everywhere.
CATEGORIES = {
    "20": "Gaming",
    "22": "People & Blogs",
    "24": "Entertainment",
    "23": "Comedy",
    "27": "Education",
    "26": "Howto & Style",
    "28": "Science & Technology",
    "17": "Sports",
    "10": "Music",
    "25": "News & Politics",
}

TITLE_LIMIT = 100
DESCRIPTION_BYTES = 5000
TAGS_TOTAL = 500

# 8 MiB. Resumable chunks must be a multiple of 256 KiB, and this is big enough
# that a normal clip goes in a handful of requests but small enough that a
# failed one is cheap to resend.
CHUNK = 8 * 1024 * 1024
RETRIES = 8

HTTP_TIMEOUT = 60

_lock = threading.RLock()


class NotConnected(Exception):
    """No usable YouTube sign-in: never connected, withdrawn, or expired."""


class UploadError(Exception):
    """An upload YouTube refused, with a message a person can act on."""


# --- the client -----------------------------------------------------------

def _builtin() -> Optional[Dict]:
    bundle = getattr(sys, "_MEIPASS", None)
    here = Path(bundle) / "webapp" if bundle else Path(__file__).parent
    path = here / BUILTIN_CLIENT_FILE
    if not path.is_file():
        return None
    try:
        return parse_client(str(path))
    except ValueError as e:
        print(f"[youtube] ignoring the bundled client ({e})", flush=True)
        return None


def client() -> Optional[Dict]:
    """The Google OAuth client to sign in with: the user's own, else the built-in."""
    cid = user_config.get("YOUTUBE_CLIENT_ID")
    secret = user_config.get("YOUTUBE_CLIENT_SECRET")
    if cid and secret:
        return {"id": cid, "secret": secret, "source": "yours"}
    built = _builtin()
    if built:
        return {**built, "source": "builtin"}
    return None


def parse_client(text: str) -> Dict:
    """Read a client from the JSON Google hands out, pasted or as a path to the file.

    Google's download is {"installed": {...}} for a Desktop app client. A Web
    application client looks almost the same and fails much later, at the
    redirect, with an error that names neither -- so it is caught here, where
    the fix can be spelled out.
    """
    raw = (text or "").strip().strip('"')
    if not raw:
        raise ValueError("Paste the client JSON, or the path to the file.")
    if not raw.startswith("{"):
        path = Path(raw).expanduser()
        if not path.is_file():
            raise ValueError(f"There's no file at {path}.")
        try:
            raw = path.read_text(encoding="utf-8-sig")
        except OSError as e:
            raise ValueError(f"Couldn't read {path} ({e.strerror or e}).")
    try:
        data = json.loads(raw)
    except ValueError:
        raise ValueError("That isn't the JSON file Google gave you — it doesn't parse.")
    if not isinstance(data, dict):
        raise ValueError("That isn't the JSON file Google gave you.")
    if "web" in data and "installed" not in data:
        raise ValueError("That is a \"Web application\" client. ClipMint needs one of type "
                         "\"Desktop app\" — create another in Google Cloud, under "
                         "APIs & Services, Credentials.")
    inner = data.get("installed", data)
    cid = str(inner.get("client_id") or "").strip()
    secret = str(inner.get("client_secret") or "").strip()
    if not cid.endswith(".apps.googleusercontent.com") or not secret:
        raise ValueError("That JSON has no Desktop app client ID and secret in it.")
    return {"id": cid, "secret": secret}


def set_client(text: str) -> Dict:
    """Save the user's own client, or clear it with an empty string.

    A sign-in belongs to the client that issued it. Swapping clients leaves a
    refresh token the new one cannot use, so the old sign-in goes with it.
    """
    if (text or "").strip():
        c = parse_client(text)
        values = {"YOUTUBE_CLIENT_ID": c["id"], "YOUTUBE_CLIENT_SECRET": c["secret"]}
    else:
        values = {"YOUTUBE_CLIENT_ID": "", "YOUTUBE_CLIENT_SECRET": ""}
    user_config.save(values)

    tok = _load_token()
    now = client()
    if tok and (not now or tok.get("client_id") != now["id"]):
        disconnect()
    return status()


# --- the stored sign-in ---------------------------------------------------

def _token_path() -> Path:
    return user_config.config_dir() / "youtube_token.json"


def _load_token() -> Optional[Dict]:
    try:
        data = json.loads(_token_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("refresh_token") else None


def _save_token(tok: Dict) -> None:
    path = _token_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(tok, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)       # it can upload to someone's channel
    except OSError:
        pass
    os.replace(tmp, path)


def _forget_token() -> None:
    try:
        _token_path().unlink()
    except OSError:
        pass


def _google_error(r: requests.Response) -> Dict:
    """The reason and message out of a Google error body, whichever shape it has."""
    try:
        body = r.json()
    except ValueError:
        return {"reason": "", "message": r.text[:300]}
    err = body.get("error")
    if isinstance(err, str):            # the OAuth endpoints
        return {"reason": err, "message": body.get("error_description") or err}
    err = err or {}
    items = err.get("errors") or [{}]
    reason = items[0].get("reason") or ""
    for d in err.get("details") or []:
        reason = reason or d.get("reason") or ""
    return {"reason": reason, "message": err.get("message") or ""}


def _access_token(force: bool = False) -> str:
    """A live access token, refreshed from the stored sign-in when it is due."""
    with _lock:
        tok = _load_token()
        if not tok:
            raise NotConnected("YouTube isn't connected. Connect it in Settings.")
        if not force and tok.get("access_token") and tok.get("expires_at", 0) > time.time() + 90:
            return tok["access_token"]

        c = client()
        if not c or c["id"] != tok.get("client_id"):
            _forget_token()
            raise NotConnected("The Google client changed since YouTube was connected. "
                               "Connect again.")
        try:
            r = requests.post(TOKEN_URL, data={
                "client_id": c["id"],
                "client_secret": c["secret"],
                "refresh_token": tok["refresh_token"],
                "grant_type": "refresh_token",
            }, timeout=HTTP_TIMEOUT)
        except requests.RequestException as e:
            raise UploadError(f"Couldn't reach Google to renew the sign-in ({e}).")

        if r.status_code in (400, 401):
            err = _google_error(r)
            if err["reason"] in ("invalid_grant", "unauthorized_client", "invalid_client"):
                _forget_token()
                raise NotConnected(
                    "YouTube's permission was withdrawn or has expired. Connect again. "
                    "If this keeps happening every week, your Google project is still in "
                    "Testing — publish it to Production (see the setup guide).")
            raise UploadError(f"Google refused to renew the sign-in: {err['message']}")
        if not r.ok:
            raise UploadError(f"Google refused to renew the sign-in ({r.status_code}).")

        data = r.json()
        tok["access_token"] = data["access_token"]
        tok["expires_at"] = time.time() + int(data.get("expires_in", 3600))
        _save_token(tok)
        return tok["access_token"]


def _headers(token: str) -> Dict:
    return {"Authorization": f"Bearer {token}", "User-Agent": f"ClipMint/{APP_VERSION}"}


# --- connecting -----------------------------------------------------------

# Sign-ins that have been started and not yet come back, by their state value.
_pending: Dict[str, Dict] = {}
_connect = {"state": "idle", "error": ""}


def begin_connect(redirect_uri: str) -> str:
    """Start a sign-in and return the Google page to send the user to.

    PKCE, so a code intercepted on the way back to the loopback address is
    useless to anyone without the verifier held here; and a random state, so a
    callback this app did not ask for is thrown away.
    """
    c = client()
    if not c:
        raise NotConnected("No Google client is set up yet. Add yours under "
                           "\"Use your own Google client\" — the setup guide shows how.")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)

    with _lock:
        now = time.time()
        for k in [k for k, v in _pending.items() if now - v["started"] > 900]:
            _pending.pop(k, None)
        _pending[state] = {"verifier": verifier, "redirect_uri": redirect_uri,
                           "client": c, "started": now}
        _connect.update(state="waiting", error="")

    return AUTH_URL + "?" + urlencode({
        "client_id": c["id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        # offline + consent is what makes Google hand over a refresh token
        # every time, rather than only on the very first sign-in.
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })


def _fail_connect(message: str) -> str:
    with _lock:
        _connect.update(state="error", error=message)
    return message


def finish_connect(state: str, code: str, error: str) -> Optional[str]:
    """Complete a sign-in from Google's redirect. Returns an error message, or None."""
    with _lock:
        pending = _pending.pop(state or "", None)
    if not pending:
        return _fail_connect("That sign-in link is stale. Click Connect in ClipMint again.")
    if error:
        if error == "access_denied":
            return _fail_connect("Sign-in was cancelled, so nothing was connected.")
        return _fail_connect(f"Google stopped the sign-in: {error}.")
    c = pending["client"]

    try:
        r = requests.post(TOKEN_URL, data={
            "code": code,
            "client_id": c["id"],
            "client_secret": c["secret"],
            "redirect_uri": pending["redirect_uri"],
            "grant_type": "authorization_code",
            "code_verifier": pending["verifier"],
        }, timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        return _fail_connect(f"Couldn't reach Google to finish signing in ({e}).")
    if not r.ok:
        return _fail_connect(f"Google refused the sign-in: {_google_error(r)['message']}")
    data = r.json()

    granted = set((data.get("scope") or "").split())
    if SCOPE_UPLOAD not in granted:
        # Google's consent page lets people untick individual permissions.
        return _fail_connect("Google was not given permission to upload videos. Connect "
                             "again and leave the \"upload\" box ticked.")
    if not data.get("refresh_token"):
        return _fail_connect("Google didn't hand over a lasting sign-in. Remove ClipMint at "
                             "myaccount.google.com/permissions, then connect again.")

    tok = {
        "client_id": c["id"],
        "refresh_token": data["refresh_token"],
        "access_token": data["access_token"],
        "expires_at": time.time() + int(data.get("expires_in", 3600)),
        "scopes": sorted(granted),
        "connected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    if SCOPE_READ in granted:
        try:
            tok.update(_fetch_channel(data["access_token"]))
        except UploadError as e:
            return _fail_connect(str(e))
        except requests.RequestException:
            pass        # the name is a nicety; the sign-in itself worked

    with _lock:
        _save_token(tok)
        _connect.update(state="connected", error="")
    return None


def _fetch_channel(access_token: str) -> Dict:
    """The connected channel's id and name, stamped with when they were read."""
    r = requests.get(f"{API_URL}/channels", params={"part": "snippet", "mine": "true"},
                     headers=_headers(access_token), timeout=HTTP_TIMEOUT)
    if not r.ok:
        if _google_error(r)["reason"] in ("accessNotConfigured", "SERVICE_DISABLED"):
            raise UploadError(_API_OFF)
        return {}
    items = r.json().get("items") or []
    if not items:
        raise UploadError("That Google account has no YouTube channel. Create one at "
                          "youtube.com, then connect again.")
    return {"channel_id": items[0]["id"],
            "channel_title": items[0]["snippet"].get("title", ""),
            "channel_read_at": time.time()}


def _refresh_channel() -> None:
    """Re-read the channel's name once the copy on disk is 30 days old, or drop it."""
    with _lock:
        tok = _load_token()
        if not tok or time.time() - tok.get("channel_read_at", 0) < KEEP_DAYS * 86400:
            return
        for k in ("channel_id", "channel_title", "channel_read_at"):
            tok.pop(k, None)
        _save_token(tok)
    try:
        fresh = _fetch_channel(_access_token())
    except (NotConnected, UploadError, requests.RequestException):
        return
    with _lock:
        tok = _load_token()
        if tok:
            tok.update(fresh)
            _save_token(tok)


def expired(record: Optional[Dict]) -> bool:
    """Is an upload record kept on a clip older than YouTube lets it be kept?"""
    if not record:
        return False
    try:
        when = datetime.fromisoformat(record.get("uploaded_at", ""))
    except ValueError:
        return True
    return datetime.now(timezone.utc) - when > timedelta(days=KEEP_DAYS)


def disconnect() -> Dict:
    """Withdraw the sign-in at Google, then forget it here."""
    with _lock:
        tok = _load_token()
        _forget_token()
        _connect.update(state="idle", error="")
    if tok:
        try:
            requests.post(REVOKE_URL, data={"token": tok["refresh_token"]},
                          timeout=15)
        except requests.RequestException:
            pass        # forgotten locally either way; Google expires unused ones
    return status()


def status() -> Dict:
    c = client()
    tok = _load_token()
    if tok and c and tok.get("client_id") != c["id"]:
        tok = None
    if (tok and "channel_title" in tok
            and time.time() - tok.get("channel_read_at", 0) >= KEEP_DAYS * 86400):
        threading.Thread(target=_refresh_channel, daemon=True).start()
        tok = {k: v for k, v in tok.items() if not k.startswith("channel_")}
    return {
        "client": c["source"] if c else None,
        "client_id": c["id"] if c else "",
        "builtin_available": _builtin() is not None,
        "connected": bool(tok),
        "channel_title": (tok or {}).get("channel_title", ""),
        "channel_id": (tok or {}).get("channel_id", ""),
        "connected_at": (tok or {}).get("connected_at", ""),
        "connect_state": _connect["state"],
        "connect_error": _connect["error"],
        "pinned": bool(os.getenv("YOUTUBE_CLIENT_ID", "").strip()),
        # A list, not the dict: a browser orders numeric-looking keys by number,
        # which would sort Music above Gaming.
        "categories": list(CATEGORIES.items()),
    }


# --- metadata -------------------------------------------------------------

def _check_angles(text: str, what: str) -> None:
    # YouTube rejects < and > anywhere in a title, description or tag, outright.
    if "<" in text or ">" in text:
        raise ValueError(f"YouTube doesn't allow < or > in a {what}. Take them out and "
                         f"try again.")


def build_metadata(title: str, description: str, tags: object, privacy: str,
                   publish_at: Optional[str], made_for_kids: bool,
                   category: str) -> Dict:
    """The body of a videos.insert call, held to YouTube's own limits.

    Everything YouTube would reject is refused here, with the reason, so a clip
    does not upload for two minutes and then bounce. Nothing is trimmed or
    rewritten to make it fit: YouTube's policies (III.C.3) say what the person
    typed is what gets sent, so a title that is too long is theirs to shorten.
    Only whitespace around the ends, which nobody can see, is dropped.
    """
    title = str(title or "").strip()
    if not title:
        raise ValueError("A video needs a title.")
    if len(title) > TITLE_LIMIT:
        raise ValueError(f"YouTube titles are {TITLE_LIMIT} characters at most — "
                         f"that one is {len(title)}.")
    _check_angles(title, "title")

    desc = str(description or "").strip()
    size = len(desc.encode("utf-8"))
    if size > DESCRIPTION_BYTES:
        raise ValueError(f"The description is too long for YouTube: {size} bytes, and "
                         f"the limit is {DESCRIPTION_BYTES}.")
    _check_angles(desc, "description")

    # The tag box is comma-separated, so splitting on commas is reading it,
    # not changing it.
    items = tags if isinstance(tags, list) else str(tags or "").split(",")
    clean: List[str] = [str(t).strip() for t in items if str(t).strip()]
    for t in clean:
        _check_angles(t, "tag")
    # YouTube counts a tag with a space in it as if it were quoted, and the
    # commas between tags, against its 500.
    total = sum(len(t) + (2 if " " in t else 0) for t in clean) + max(0, len(clean) - 1)
    if total > TAGS_TOTAL:
        raise ValueError(f"The tags come to {total} characters the way YouTube counts "
                         f"them, and the limit is {TAGS_TOTAL}. Remove a few.")

    privacy = (privacy or "public").lower()
    status: Dict = {"selfDeclaredMadeForKids": bool(made_for_kids)}
    if publish_at:
        try:
            when = datetime.fromisoformat(publish_at.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("That schedule time can't be read.")
        if when.tzinfo is None:
            raise ValueError("The schedule time has no time zone.")
        if when < datetime.now(timezone.utc) + timedelta(minutes=10):
            raise ValueError("Pick a time at least 10 minutes from now.")
        if when > datetime.now(timezone.utc) + timedelta(days=365 * 2):
            raise ValueError("That schedule time is too far away.")
        # YouTube only schedules a private video: it flips it public at the time.
        status["privacyStatus"] = "private"
        status["publishAt"] = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    else:
        if privacy not in PRIVACY:
            raise ValueError("Pick public, unlisted, private or a schedule.")
        status["privacyStatus"] = privacy

    category = str(category or "22")
    if category not in CATEGORIES:
        raise ValueError("Pick a category from the list.")

    return {
        "snippet": {"title": title, "description": desc, "tags": clean,
                    "categoryId": category},
        "status": status,
    }


# --- uploading ------------------------------------------------------------

_uploads: Dict[str, Dict] = {}


def _explain(r: requests.Response) -> str:
    err = _google_error(r)
    reason, message = err["reason"], err["message"]
    known = {
        "quotaExceeded": "This Google project has used up today's YouTube allowance. It "
                         "resets at midnight Pacific time.",
        "rateLimitExceeded": "YouTube asked to slow down. Wait a minute and try again.",
        "uploadLimitExceeded": "YouTube says this channel has uploaded as much as it can for "
                               "today. Try again in 24 hours.",
        "youtubeSignupRequired": "That Google account has no YouTube channel. Create one at "
                                 "youtube.com first.",
        "invalidTitle": "YouTube didn't accept the title.",
        "invalidDescription": "YouTube didn't accept the description.",
        "invalidTags": "YouTube didn't accept the tags. Try fewer or shorter ones.",
        "invalidCategoryId": "YouTube didn't accept that category.",
        "invalidPublishAt": "YouTube didn't accept the schedule time.",
        "forbidden": "YouTube refused the upload for this account.",
        "insufficientPermissions": "ClipMint wasn't given permission to upload. Disconnect "
                                   "and connect again, leaving the upload box ticked.",
        "accessNotConfigured": _API_OFF,
        "SERVICE_DISABLED": _API_OFF,
    }
    if reason in known:
        return known[reason]
    return f"YouTube refused the upload ({r.status_code}): {message or reason or 'no reason given'}"


_API_OFF = ("The YouTube Data API v3 isn't turned on in your Google Cloud project. Turn it on "
            "under APIs & Services, Library, wait a minute, and try again.")


def _resume_offset(session: str, size: int) -> Optional[int]:
    """Ask the upload session how much of the file it already has."""
    r = requests.put(session, headers={**_headers(_access_token()),
                                       "Content-Length": "0",
                                       "Content-Range": f"bytes */{size}"},
                     timeout=HTTP_TIMEOUT)
    if r.status_code in (200, 201):
        return None                         # already complete
    if r.status_code == 308:
        rng = r.headers.get("Range", "")
        m = re.match(r"bytes=0-(\d+)", rng)
        return int(m.group(1)) + 1 if m else 0
    raise UploadError(_explain(r))


def _run(uid: str, path: Path, meta: Dict, on_done: Optional[Callable[[Dict], None]]) -> None:
    st = _uploads[uid]
    try:
        size = path.stat().st_size
        st.update(size=size, state="starting")
        mime = "video/mp4" if path.suffix.lower() in (".mp4", ".m4v") else "application/octet-stream"
        init_headers = {"Content-Type": "application/json; charset=UTF-8",
                        "X-Upload-Content-Length": str(size),
                        "X-Upload-Content-Type": mime}
        params = {"uploadType": "resumable", "part": "snippet,status"}

        r = requests.post(UPLOAD_URL, params=params, json=meta,
                          headers={**_headers(_access_token()), **init_headers},
                          timeout=HTTP_TIMEOUT)
        if r.status_code == 401:
            r = requests.post(UPLOAD_URL, params=params, json=meta,
                              headers={**_headers(_access_token(force=True)), **init_headers},
                              timeout=HTTP_TIMEOUT)
        if not r.ok or "Location" not in r.headers:
            raise UploadError(_explain(r))
        session = r.headers["Location"]

        st["state"] = "uploading"
        offset, failures, video = 0, 0, None
        with open(path, "rb") as f:
            while video is None:
                f.seek(offset)
                chunk = f.read(CHUNK)
                end = offset + len(chunk) - 1
                try:
                    r = requests.put(session, data=chunk, headers={
                        **_headers(_access_token()),
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {offset}-{end}/{size}",
                    }, timeout=HTTP_TIMEOUT * 3)
                except requests.RequestException:
                    r = None

                if r is not None and r.status_code in (200, 201):
                    video = r.json()
                    st["sent"] = size
                    break
                if r is not None and r.status_code == 308:
                    m = re.match(r"bytes=0-(\d+)", r.headers.get("Range", ""))
                    offset = int(m.group(1)) + 1 if m else 0
                    st["sent"] = offset
                    failures = 0
                    continue
                if r is not None and r.status_code == 401 and failures < 2:
                    failures += 1
                    _access_token(force=True)
                    continue
                if r is not None and r.status_code == 404:
                    raise UploadError("YouTube dropped the upload session. Start the upload again.")
                if r is not None and r.status_code < 500:
                    raise UploadError(_explain(r))

                # A 5xx or a dropped connection: back off, then ask where to resume.
                failures += 1
                if failures > RETRIES:
                    raise UploadError("The connection to YouTube kept failing. Check your "
                                      "internet and try again.")
                st["note"] = f"Connection trouble — retrying ({failures}/{RETRIES})"
                time.sleep(min(60, 2 ** failures))
                try:
                    resumed = _resume_offset(session, size)
                except requests.RequestException:
                    continue
                if resumed is None:
                    # Complete already; the response carrying the video was lost.
                    raise UploadError("The upload finished but YouTube's reply was lost. "
                                      "Check YouTube Studio before uploading again.")
                offset = resumed
                st["sent"] = offset
                st["note"] = ""

        video_id = video["id"]
        st.update(state="checking", video_id=video_id, note="")
        asked = meta["status"]
        result = {
            "video_id": video_id,
            "url": f"https://youtube.com/shorts/{video_id}",
            "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "privacy": asked.get("privacyStatus"),
            "publish_at": asked.get("publishAt", ""),
            "channel_title": (video.get("snippet") or {}).get("channelTitle", ""),
            "kept_private": False,
        }

        # An unaudited Google project gets its uploads locked private. The upload
        # response does not always say so, so read the video back.
        wanted_public = asked.get("privacyStatus") != "private" or asked.get("publishAt")
        if wanted_public:
            got = (video.get("status") or {})
            try:
                vr = requests.get(f"{API_URL}/videos", params={"part": "status", "id": video_id},
                                  headers=_headers(_access_token()), timeout=HTTP_TIMEOUT)
                if vr.ok and vr.json().get("items"):
                    got = vr.json()["items"][0].get("status") or got
            except (requests.RequestException, NotConnected, UploadError):
                pass
            lost_schedule = bool(asked.get("publishAt")) and not got.get("publishAt")
            went_private = (asked.get("privacyStatus") != "private"
                            and got.get("privacyStatus") == "private")
            if lost_schedule or went_private:
                result["kept_private"] = True
                result["privacy"] = "private"

        st.update(state="done", result=result)
        if on_done:
            try:
                on_done(result)
            except Exception as e:      # the upload itself succeeded regardless
                print(f"[youtube] could not record the upload on the clip ({e})", flush=True)

    except NotConnected as e:
        st.update(state="error", error=str(e), reconnect=True)
    except (UploadError, ValueError) as e:
        st.update(state="error", error=str(e))
    except requests.RequestException as e:
        st.update(state="error", error=f"Couldn't reach YouTube ({e}).")
    except OSError as e:
        st.update(state="error", error=f"Couldn't read the clip file ({e.strerror or e}).")
    except Exception as e:
        st.update(state="error", error=f"The upload failed: {e}")
    finally:
        st["finished"] = time.time()


def start_upload(key: str, path: Path, meta: Dict,
                 on_done: Optional[Callable[[Dict], None]] = None) -> str:
    """Upload in the background. `key` names the clip, so it is not sent twice at once."""
    if not _load_token():
        raise NotConnected("YouTube isn't connected. Connect it in Settings.")
    with _lock:
        for uid, st in _uploads.items():
            if st["key"] == key and st["state"] not in ("done", "error"):
                return uid
        now = time.time()
        for uid in [u for u, s in _uploads.items() if s.get("finished") and now - s["finished"] > 3600]:
            _uploads.pop(uid, None)
        uid = uuid.uuid4().hex
        _uploads[uid] = {"id": uid, "key": key, "state": "queued", "sent": 0, "size": 0,
                         "note": "", "error": "", "result": None, "started": now}
    threading.Thread(target=_run, args=(uid, path, meta, on_done),
                     name=f"youtube-upload-{uid[:6]}", daemon=True).start()
    return uid


def upload_status(uid: str) -> Optional[Dict]:
    st = _uploads.get(uid)
    return dict(st) if st else None


def active_for(key: str) -> Optional[Dict]:
    for st in _uploads.values():
        if st["key"] == key and st["state"] not in ("done", "error"):
            return dict(st)
    return None
