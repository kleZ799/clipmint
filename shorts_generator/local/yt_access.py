"""Getting past YouTube's "confirm you're not a bot" gate.

YouTube does not always hand yt-dlp a video. Sometimes it hands back a consent
wall instead -- the same interstitial a browser gets when an IP looks automated,
reported as:

    Sign in to confirm you're not a bot. Use --cookies-from-browser or --cookies

It is not a bug in this app and it is not a broken URL. It is YouTube deciding,
on that request, from that IP, that it wants to see a signed-in human. It fires
on VPNs and shared connections, after a run of downloads from one address, and
on anything age- or region-gated. Left alone it kills the run at 0% in about a
second, which reads to the user as the app being broken.

There are two ways through, and this module does both:

  Ask differently.  yt-dlp can speak to YouTube as any of a dozen clients -- the
  website, the mobile site, the TV app, a VR headset. They are gated separately
  and inconsistently, so a request the web client is refused often goes straight
  through as the TV app. This costs nothing and needs no setup from the user,
  which is why it is tried first.

  Ask as somebody.  Hand yt-dlp the YouTube cookies out of a browser on this PC
  and the request stops being anonymous, which is what the error is actually
  asking for. This works when nothing else does, and is the only reliable answer
  for a flagged IP.

So the download tries the cheap thing, then the next cheap thing, and only then
reaches for cookies -- and once something works it is remembered for the rest of
the session, so the ladder is climbed once per run and not once per request.

A word on the cookies, because it is worth saying plainly: they are the
signed-in session of whichever browser profile they come from. Nothing leaves
this PC -- yt-dlp reads them locally and sends them to YouTube, which is where
they came from -- but YouTube does get to see that the downloads belong to that
account, and accounts have been restricted for automated access before. If that
matters, sign a throwaway account into one browser profile and point this at it.
"""
import os
import sys
from typing import Callable, Dict, List, Optional, Tuple

from .. import user_config

# What the gate looks like coming back out of yt-dlp. Matched case-insensitively
# against the whole message, so a wording change on YouTube's side that keeps any
# one of these phrases still lands.
#
# "confirm you're not a bot" is the gate itself. The others are the same refusal
# wearing different clothes: yt-dlp reports the missing-formats symptom rather
# than the cause when the interstitial comes back as an empty player response.
_GATE_MARKERS = (
    "confirm you're not a bot",
    "confirm you are not a bot",
    "sign in to confirm",
    "please sign in",
    "not a bot",
    "requested format is not available",
    "no video formats found",
    "unable to extract player response",
    "failed to extract any player response",
    "the following content is not available on this app",
)

# Age gates and members-only videos also say "sign in", and no amount of client
# juggling fixes those -- they need an account that is actually allowed to watch.
# Worth telling apart so the message at the end names the real problem.
_ACCOUNT_MARKERS = (
    "members-only",
    "join this channel",
    "inappropriate for some users",
    "age-restricted",
    "confirm your age",
)


def is_gate(err: BaseException) -> bool:
    """Is this yt-dlp being refused by YouTube, rather than a real failure?"""
    msg = str(err).lower()
    return any(m in msg for m in _GATE_MARKERS) or any(m in msg for m in _ACCOUNT_MARKERS)


def needs_an_account(err: BaseException) -> bool:
    """Is this the kind of refusal only a signed-in account can satisfy?"""
    msg = str(err).lower()
    return any(m in msg for m in _ACCOUNT_MARKERS)


# ---------------------------------------------------------------- cookie source

# Browsers yt-dlp can read cookies out of, most likely first. Safari is macOS
# only and yt-dlp rejects it elsewhere, so it is filtered by platform below.
_BROWSERS = ("firefox", "chrome", "edge", "brave", "opera", "vivaldi",
             "chromium", "whale", "safari")

# Cookies that only exist once you are actually signed in. Used to tell "this
# browser has been to YouTube" apart from "this browser is signed in to
# YouTube", which are very different answers to give somebody.
_LOGIN_COOKIES = ("SID", "__Secure-1PSID", "__Secure-3PSID", "LOGIN_INFO")

MODES = ("auto", "off", "browser", "file")


def settings() -> Dict:
    """What the user has chosen, with the defaults filled in.

    "auto" is the default and means: don't touch cookies until YouTube asks for
    them, then try whatever browser on this PC can supply them. It needs no
    setup, which matters -- the people who hit this error are not the people who
    want to read about exporting cookie jars.
    """
    mode = (user_config.get("YOUTUBE_COOKIES_MODE", "auto") or "auto").lower()
    if mode not in MODES:
        mode = "auto"
    return {
        "mode": mode,
        "browser": user_config.get("YOUTUBE_COOKIES_BROWSER", ""),
        "file": user_config.get("YOUTUBE_COOKIES_FILE", ""),
    }


def _browser_spec(value: str) -> Optional[Tuple]:
    """Parse "firefox" or "firefox:Profile Name" into yt-dlp's tuple form.

    yt-dlp wants (browser, profile, keyring, container). Only the first two are
    worth exposing: keyring is a Linux desktop detail it works out itself, and
    containers are a Firefox feature almost nobody downloading a stream VOD is
    using.
    """
    value = (value or "").strip()
    if not value:
        return None
    name, _, profile = value.partition(":")
    name = name.strip().lower()
    if name not in _BROWSERS:
        return None
    return (name, profile.strip() or None, None, None)


# Names a cookies.txt export tends to arrive under. The two long ones are what
# the popular browser extensions save as, so a file dragged straight out of
# Downloads is recognised without being renamed first.
_COOKIE_FILE_NAMES = (
    "cookies.txt",
    "youtube_cookies.txt",
    "www.youtube.com_cookies.txt",
    "youtube.com_cookies.txt",
)


def cookie_file_places() -> List[str]:
    """Folders a dropped-in cookies.txt is looked for, in order.

    Exporting cookies is already more than most people want to do; making them
    then find a settings panel and paste a path is where the rest give up. So a
    file left in either folder the app already talks about -- the one holding
    settings, and the one holding their clips -- is picked up on its own.
    """
    places = []
    try:
        places.append(str(user_config.config_dir()))
    except Exception:
        pass
    try:
        places.append(str(user_config.output_root()))
    except Exception:
        pass
    return places


def discovered_cookie_file() -> Optional[str]:
    """A cookies.txt somebody has dropped into one of those folders, if any."""
    for folder in cookie_file_places():
        for name in _COOKIE_FILE_NAMES:
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                return path
    return None


def _cookie_file() -> Optional[str]:
    """A cookies.txt to use, from the environment, the settings file, or a drop.

    The environment wins, matching how every other setting in this app behaves,
    and gives someone running from source a way to point at a jar without
    touching the UI. A discovered file comes last: an explicit choice always
    beats a guess.
    """
    path = (os.getenv("YTDLP_COOKIES", "").strip()
            or user_config.get("YOUTUBE_COOKIES_FILE", ""))
    if path and os.path.isfile(os.path.expanduser(path)):
        return os.path.expanduser(path)
    return discovered_cookie_file()


def check_file(path: str) -> Dict:
    """Read a cookies.txt and say whether it is any use, before a run depends on it.

    Saving a path that turns out to be an empty file, the wrong site's cookies,
    or an export that expired two months ago should fail here, in a settings
    panel, with a sentence saying which -- not forty minutes into a run as the
    same bot-check error it was meant to prevent.
    """
    import time

    result = {"ok": False, "cookies": 0, "signed_in": False,
              "expired": 0, "reason": "", "path": path}
    full = os.path.expanduser((path or "").strip().strip('"'))
    result["path"] = full
    if not full:
        result["reason"] = "No file chosen."
        return result
    if not os.path.isfile(full):
        result["reason"] = "There's no file at that path."
        return result

    try:
        from yt_dlp.cookies import YoutubeDLCookieJar
        jar = YoutubeDLCookieJar(full)
        jar.load(ignore_discard=True, ignore_expires=True)
    except Exception as e:
        # The usual cause is the browser's own JSON export rather than the
        # Netscape format yt-dlp reads, and "not a cookies file" is a more
        # useful thing to be told than a parser's line number.
        return {**result,
                "reason": f"That file isn't a cookies.txt yt-dlp can read ({e})."}

    now = time.time()
    names = set()
    for c in jar:
        if "youtube.com" not in (c.domain or ""):
            continue
        if c.expires and c.expires < now:
            result["expired"] += 1
            continue
        result["cookies"] += 1
        names.add(c.name)

    result["signed_in"] = any(n in names for n in _LOGIN_COOKIES)
    result["ok"] = result["cookies"] > 0

    if not result["cookies"] and result["expired"]:
        result["reason"] = ("Every YouTube cookie in that file has expired. "
                            "Export a fresh one from the browser.")
    elif not result["cookies"]:
        result["reason"] = ("That file has no YouTube cookies in it. Export it "
                            "again with youtube.com open in the browser.")
    elif not result["signed_in"]:
        result["reason"] = ("Those cookies aren't from a signed-in session. They "
                            "may still work, but signing in first is what YouTube "
                            "is actually asking for.")
    return result


# The copy yt-dlp is actually given. Never the user's own export -- see below.
_WORKING_COPY = "youtube-cookies-in-use.txt"


def working_copy_of(path: str) -> str:
    """A private copy of `path` for yt-dlp to use, refreshed when the original changes.

    yt-dlp writes the cookie jar back to whatever file it was handed, keeping it
    current as YouTube rotates the session. That is useful and it is also how it
    quietly ruins an export: measured here, a run against a jar YouTube didn't
    accept came back with the login cookies stripped out of the file. Somebody
    who exported cookies.txt once and pointed us at it would find it degraded,
    with nothing to say what had happened to it.

    So the export is treated as read-only and yt-dlp gets a copy. The freshening
    still works -- it happens to the copy -- and re-exporting over the original
    is still picked up, because a newer original is copied again.

    Falls back to the original path if the copy can't be made. A cookie file
    that works and might get rewritten beats no cookie file at all.
    """
    source = os.path.expanduser(path)
    try:
        dest = str(user_config.config_dir() / _WORKING_COPY)
    except Exception:
        return source
    if os.path.abspath(source) == os.path.abspath(dest):
        return dest
    try:
        stale = (not os.path.exists(dest)
                 or os.path.getmtime(source) > os.path.getmtime(dest))
        if stale:
            import shutil
            shutil.copyfile(source, dest)
            os.chmod(dest, 0o600)   # it is a session token; keep it owner-only
    except OSError as e:
        print(f"[download/local] couldn't copy {source} ({e}) — using it directly",
              flush=True)
        return source
    return dest


def _opts_for_file(path: str) -> Dict:
    return {"cookiefile": working_copy_of(path)}


def _opts_for_browser(spec: Tuple) -> Dict:
    return {"cookiesfrombrowser": spec}


# ------------------------------------------------------------- browser probing

class _QuietLogger:
    """Swallows yt-dlp's cookie chatter while we are only asking a question.

    Probing a browser that cannot be read prints warnings about failed decryption
    which, at that moment, are the answer rather than a problem -- and printing
    them into the run log would have somebody debugging Chrome's key storage when
    all that happened is that we moved on to Firefox.
    """
    def debug(self, message): pass
    def info(self, message): pass
    def warning(self, message, only_once=False): pass
    def error(self, message): pass


def installed_browsers() -> List[str]:
    """Browsers that appear to be installed, in the order worth trying them.

    Existence of the profile folder only -- this says nothing about whether the
    cookies inside can be read, which is what probe() is for. Anything we cannot
    work out the folder for is kept in the list rather than dropped: offering a
    browser that turns out not to be there is a much smaller failure than hiding
    one that is.
    """
    try:
        from yt_dlp import cookies as ytc
    except ImportError:
        return []

    found = []
    for name in _BROWSERS:
        if name == "safari" and sys.platform != "darwin":
            continue
        if name == "firefox":
            try:
                if any(os.path.isdir(d) for d in ytc._firefox_browser_dirs()):
                    found.append(name)
            except Exception:
                found.append(name)
            continue
        if name == "safari":
            found.append(name)
            continue
        try:
            settings_ = ytc._get_chromium_based_browser_settings(name)
            if os.path.isdir(settings_["browser_dir"]):
                found.append(name)
        except Exception:
            continue
    return found


# Probing reads a browser's whole cookie database, so the answer is kept rather
# than worked out again for every video in a channel run.
_probe_cache: Dict[str, Dict] = {}


def probe(browser: str) -> Dict:
    """Can we actually read YouTube cookies out of this browser, and are they signed in?

    Returns {ok, cookies, signed_in, reason}. Never raises: every way this can
    fail is a thing to report, not a thing to crash on.
    """
    key = (browser or "").strip().lower()
    if key in _probe_cache:
        return _probe_cache[key]

    result = {"browser": key, "ok": False, "cookies": 0,
              "signed_in": False, "reason": ""}
    spec = _browser_spec(key)
    if not spec:
        result["reason"] = "Not a browser yt-dlp can read."
        _probe_cache[key] = result
        return result

    try:
        from yt_dlp import cookies as ytc
        jar = ytc.extract_cookies_from_browser(
            spec[0], spec[1], _QuietLogger(), keyring=spec[2], container=spec[3])
    except Exception as e:
        result["reason"] = _explain_probe_failure(key, e)
        _probe_cache[key] = result
        return result

    names = set()
    for c in jar:
        if "youtube.com" in (c.domain or ""):
            result["cookies"] += 1
            names.add(c.name)
    result["signed_in"] = any(n in names for n in _LOGIN_COOKIES)
    result["ok"] = result["cookies"] > 0

    if not result["cookies"]:
        result["reason"] = _no_cookies_reason(key)
    elif not result["signed_in"]:
        result["reason"] = (
            f"{key.title()} has YouTube cookies but is not signed in. That is "
            f"often enough on its own — sign in there if the gate comes back.")
    _probe_cache[key] = result
    return result


def _no_cookies_reason(browser: str) -> str:
    """Why a readable browser handed back nothing for YouTube."""
    if browser in ("chrome", "edge", "brave", "chromium", "opera", "vivaldi", "whale") \
            and sys.platform in ("win32", "cygwin"):
        # Chrome 127+ encrypts its cookie store so that only Chrome itself can
        # decrypt it (App-Bound Encryption), and the Chromium browsers have
        # followed. yt-dlp reads the file and gets nothing usable out of it.
        # Nothing here can fix that, so say so and point at the way round it.
        return (f"{browser.title()}'s cookies are locked to {browser.title()} "
                f"itself on Windows, so they can't be read. Use Firefox, or "
                f"export a cookies.txt and point this at the file.")
    return (f"No YouTube cookies in {browser.title()}. Open YouTube in it once, "
            f"signed in, then try again.")


def _explain_probe_failure(browser: str, err: Exception) -> str:
    """Turn yt-dlp's reason into something the user can act on.

    Three of these come up constantly on Windows and each has a different
    answer, so they are worth telling apart rather than printing the library's
    own wording at somebody.
    """
    name = browser.title()
    msg = str(err).strip() or err.__class__.__name__
    low = msg.lower()

    if "dpapi" in low or "app-bound" in low or "app bound" in low:
        # Chrome 127 introduced App-Bound Encryption and the other Chromium
        # browsers followed: the cookie file is now encrypted with a key only
        # that browser's own process can unwrap. Nothing outside it can read
        # them, this app included, and no amount of retrying changes that.
        return (f"{name} encrypts its cookies so only {name} itself can read "
                f"them. Use Firefox instead, or export a cookies.txt file from "
                f"{name} and point this at the file.")
    if "could not copy" in low or "locked" in low or "database is locked" in low:
        return (f"{name}'s cookie file was busy — it is usually open because "
                f"{name} is running. Close {name} fully and try again.")
    if "could not find" in low or "not found" in low or isinstance(err, FileNotFoundError):
        return f"{name} doesn't look installed on this PC."
    if "permission" in low or "denied" in low or "operation not permitted" in low:
        if sys.platform == "darwin":
            # macOS privacy protection, not file permissions: Safari's cookie
            # store sits in a container nothing may read without Full Disk
            # Access, and the other browsers' profiles can be refused the same
            # way. Closing the browser does nothing for this one.
            if browser == "safari":
                return ("macOS keeps Safari's cookies behind Full Disk Access. "
                        "Allow ClipMint in System Settings → Privacy & Security "
                        "→ Full Disk Access, then open it again — or sign in "
                        "to YouTube in Firefox instead.")
            return (f"macOS wouldn't let the app read {name}'s cookies. Allow "
                    f"ClipMint under System Settings → Privacy & Security → "
                    f"Full Disk Access, then open it again.")
        system = "Windows" if sys.platform in ("win32", "cygwin") else "The system"
        return (f"{system} wouldn't let the app read {name}'s cookies. "
                f"Closing {name} sometimes helps.")
    return f"Couldn't read {name}'s cookies ({msg})."


def working_browsers() -> List[Dict]:
    """Every installed browser, probed, best first.

    Signed-in browsers sort above ones that merely have cookies, which sort
    above ones that gave us nothing -- so the first entry is the one to use and
    the rest explain themselves.
    """
    results = [probe(b) for b in installed_browsers()]
    results.sort(key=lambda r: (not r["signed_in"], not r["ok"]))
    return results


def forget_probes() -> None:
    """Drop the cached answers, for when the user has just signed a browser in."""
    _probe_cache.clear()


# --------------------------------------------------------------- the ladder

# Groups of YouTube clients to ask as, in the order worth asking. Each rung is
# handed to yt-dlp as extractor_args, replacing its own default choice.
#
#   (none)      yt-dlp's default -- currently the VR and web clients. Right
#               almost always, and the only rung that costs nothing.
#   tv_simply   the barest of the TV-app clients: no player script, no proof of
#               origin, and gated separately from the website. The single most
#               useful fallback when the web client is refused.
#   android_vr  app clients, which carry their own signing and are refused on a
#   + ios       different schedule again. Neither can use cookies, so they only
#               appear on the anonymous rungs.
#   web_embedded the embedded player, which historically survives some gates the
#   + tv        main site does not, and both accept cookies.
_ANON_RUNGS: Tuple[Tuple[str, Optional[str]], ...] = (
    ("as normal", None),
    ("as the TV app", "tv_simply"),
    ("as a phone app", "android_vr,ios"),
    ("as an embedded player", "web_embedded,tv"),
)

# Clients to use once cookies are in play. Anything that cannot carry cookies is
# pointless here -- yt-dlp would warn and send the request anonymously anyway.
_COOKIE_CLIENTS = "web,tv,mweb"


def _clients(names: Optional[str]) -> Dict:
    if not names:
        return {}
    return {"extractor_args": {"youtube": {"player_client": names.split(",")}}}


def _merge(base: Dict, extra: Dict) -> Dict:
    """base + extra, with extractor_args merged a level deeper.

    A plain dict update would have a rung's player_client throw away any other
    extractor_args the caller set. Nothing does today, but the next thing to
    need one would fail quietly and strangely.
    """
    merged = dict(base)
    for key, value in extra.items():
        if key == "extractor_args" and isinstance(base.get(key), dict):
            combined = {k: dict(v) for k, v in base[key].items()}
            for section, args in value.items():
                combined.setdefault(section, {}).update(args)
            merged[key] = combined
        else:
            merged[key] = value
    return merged


def _cookie_rungs() -> List[Tuple[str, Dict]]:
    """Cookie attempts to make, in order, given what the user has configured.

    "off" means off -- somebody who turned this off gets no cookie attempts even
    when YouTube asks for them, and the message at the end says as much rather
    than silently overriding them.
    """
    cfg = settings()
    if cfg["mode"] == "off":
        return []

    rungs: List[Tuple[str, Dict]] = []

    if cfg["mode"] == "file" or _cookie_file():
        path = _cookie_file()
        if path:
            rungs.append((f"with cookies from {os.path.basename(path)}",
                          _opts_for_file(path)))

    if cfg["mode"] == "browser":
        spec = _browser_spec(cfg["browser"])
        if spec:
            rungs.append((f"with cookies from {spec[0].title()}",
                          _opts_for_browser(spec)))
        return rungs

    if cfg["mode"] == "auto":
        # Only browsers that actually hand over YouTube cookies. Asking yt-dlp
        # for cookies it cannot read costs a slow read of a locked database and
        # a confusing warning, and then fails for a reason unrelated to YouTube.
        for found in working_browsers():
            if found["ok"]:
                spec = _browser_spec(found["browser"])
                if spec:
                    rungs.append((f"with cookies from {found['browser'].title()}",
                                  _opts_for_browser(spec)))
    return rungs


def _configured_rung() -> Optional[Tuple[str, Dict]]:
    """The cookie source the user picked deliberately, to use from the start.

    Somebody who went into settings and chose a browser did it because the gate
    keeps appearing. Making them climb the whole ladder first on every video
    would waste the setting -- so a deliberate choice leads, and the anonymous
    rungs become the fallback rather than the other way round.
    """
    cfg = settings()
    if cfg["mode"] == "browser":
        spec = _browser_spec(cfg["browser"])
        if spec:
            return (f"with cookies from {spec[0].title()}", _opts_for_browser(spec))
    if cfg["mode"] == "file":
        path = _cookie_file()
        if path:
            return (f"with cookies from {os.path.basename(path)}", _opts_for_file(path))
    return None


def _rungs() -> List[Tuple[str, Dict]]:
    rungs: List[Tuple[str, Dict]] = []
    chosen = _configured_rung()
    if chosen:
        rungs.append(chosen)
    for label, clients in _ANON_RUNGS:
        rungs.append((label, _clients(clients)))
    for label, opts in _cookie_rungs():
        if not any(label == seen for seen, _ in rungs):
            rungs.append((label, opts))
    return rungs


# What got through last time, so the rest of this run skips straight to it. A
# channel job asks YouTube a dozen times; climbing the ladder once per video
# would turn one gate into minutes of retries.
_winner: Optional[Tuple[str, Dict]] = None


def reset() -> None:
    """Forget what worked. Called at the start of a run, like accel.reset_run."""
    global _winner
    _winner = None
    forget_probes()


def run(yt_dlp, opts: Dict, call: Callable, what: str = "this video"):
    """Do `call(ydl)` against YouTube, climbing the ladder until something works.

    `call` receives a live YoutubeDL and returns whatever the caller wants; it is
    re-run from the top on each rung, so it must not depend on anything the
    previous attempt left behind.

    Only YouTube's gate moves this on to the next rung. A genuine failure --
    ffmpeg refusing a merge, a disk filling up, a video that really is private --
    is raised on the spot, because trying it four more ways only makes the person
    wait longer to read the same thing.
    """
    global _winner

    rungs = _rungs()
    if _winner:
        # Not just first: the winner is already in the list under the same
        # label, and running it twice would double the wait when it stops
        # working.
        rungs = [_winner] + [r for r in rungs if r[0] != _winner[0]]

    last: Optional[BaseException] = None
    for index, (label, extra) in enumerate(rungs):
        if index and label:
            print(f"[download/local] YouTube wouldn't serve {what} — asking {label}",
                  flush=True)
        try:
            with yt_dlp.YoutubeDL(_merge(opts, extra)) as ydl:
                result = call(ydl)
        except Exception as e:
            if not is_gate(e):
                raise
            last = e
            continue
        if index:
            print(f"[download/local] {label} worked", flush=True)
        _winner = (label, extra)
        return result

    raise RuntimeError(explain(last, what))


def _why_browsers_refuse() -> str:
    """The usual reason no browser gave anything up, for the platform we are on.

    The reasons really are different. On Windows the Chromium browsers encrypt
    their cookies so only they can read them. A Mac has no such lock on Chrome,
    but keeps Safari behind Full Disk Access. Linux has neither, so an empty
    result there almost always means nobody is signed in.
    """
    if sys.platform in ("win32", "cygwin"):
        return "on Windows, Chrome and Edge lock theirs so only they can read them."
    if sys.platform == "darwin":
        return ("on a Mac, Safari's are behind Full Disk Access, which ClipMint "
                "doesn't have unless you allow it in System Settings.")
    return "usually because no browser here is signed in to YouTube."


def explain(err: Optional[BaseException], what: str = "this video") -> str:
    """The message the user actually reads when every rung failed.

    The yt-dlp original is kept on the end -- it is the only thing worth pasting
    into a bug report -- but it goes last, after the part that tells somebody
    what to do about it.
    """
    cfg = settings()
    used_cookies = bool(_cookie_rungs()) or bool(_configured_rung())

    if err is not None and needs_an_account(err):
        head = (f"YouTube will only serve {what} to a signed-in account that is "
                f"allowed to watch it — it's age-restricted, members-only, or "
                f"private.")
        fix = ("Sign a browser in to an account that can watch it, then pick that "
               "browser under Settings, under YouTube sign-in.")
    else:
        head = (f"YouTube asked this download to prove it isn't a bot, and kept "
                f"asking however the app introduced itself.")
        if cfg["mode"] == "off":
            fix = ("YouTube sign-in is switched off in Settings. Turning it back "
                   "on lets the app answer with cookies from a browser on this "
                   "PC, which is what YouTube is asking for.")
        elif used_cookies:
            fix = ("Cookies from this PC were tried and refused too, which usually "
                   "means the browser they came from isn't signed in to YouTube. "
                   "Sign it in and try again — or wait 20 minutes, or switch "
                   "network, since this often follows one IP rather than one "
                   "account.")
        else:
            # Naming the folder matters here. "Export a cookies.txt" is the
            # advice yt-dlp already gave them in the message that got them here,
            # and it is not the part they are stuck on -- where to put it is.
            where = (cookie_file_places() or [""])[0]
            fix = ("No browser on this computer could hand over YouTube "
                   "cookies — " + _why_browsers_refuse() + " Two ways round "
                   "it: sign in to YouTube in Firefox, or export a cookies.txt "
                   "from any browser"
                   + (f" and save it into\n{where}" if where else "")
                   + ". Either one is picked up on its own.")

    detail = str(err).strip() if err is not None else ""
    if detail:
        detail = detail.splitlines()[0]
    return f"{head}\n\n{fix}" + (f"\n\nYouTube said: {detail}" if detail else "")
