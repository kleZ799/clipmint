"""Tell the user a run has finished, when they are not there to see it.

A long VOD takes tens of minutes to transcribe, rank and render. Nobody sits
and watches that. The window goes behind a game, or gets minimised, or the tab
it opened is three deep -- and the clips sit there finished, with nothing to
say so, until somebody thinks to go and look.

This is the "somebody thinks to look" part, moved into the app.

Three platforms, three mechanisms, and not one of them a new dependency:

    Windows   a toast, raised through PowerShell's WinRT bindings
    macOS     osascript, which every Mac has
    Linux     notify-send, which most desktops have and some do not

Every one of them is allowed to fail. A notification that never arrives is a
disappointment. A render that dies because a notification did not arrive would
be a bug -- so nothing in this module raises, and the work it does happens on a
thread of its own so that a slow PowerShell start cannot hold up the queue.

Quiet when someone is looking
-----------------------------
The page reports whether it is actually on screen and focused, and a run that
finishes while somebody is watching it raises nothing: they can already see it.
The report is a heartbeat rather than a flag, so a window that was closed, or a
tab that went away without saying goodbye, stops counting on its own.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from typing import List, Optional

from shorts_generator import proc

APP_NAME = "ClipMint"

MAC = sys.platform == "darwin"
LINUX = sys.platform.startswith("linux")
WINDOWS = os.name == "nt"

# How long a report of "yes, I am looking at this" stays true. Long enough to
# outlast the heartbeat comfortably, short enough that a closed window is not
# still silencing notifications a minute later.
WATCH_GRACE_SECONDS = 45.0

# Nothing here is worth waiting on. PowerShell is the slow one, and even a cold
# start beats this comfortably; anything longer has gone wrong in a way that
# waiting will not fix.
TIMEOUT_SECONDS = 20


# --- is anybody looking ---------------------------------------------------

_lock = threading.Lock()
_last_report = 0.0
_watching = False


def mark_watching(watching: bool) -> None:
    """Record what the page just said about whether it is being looked at."""
    global _last_report, _watching
    with _lock:
        _last_report = time.monotonic()
        _watching = bool(watching)


def someone_is_watching() -> bool:
    """Whether the app is, as far as it knows, on somebody's screen right now.

    Both halves matter. A page that said "hidden" is not being watched; a page
    that said "visible" and then went silent is not being watched either, and
    that second case is the common one -- it is what a closed window looks
    like from here.
    """
    with _lock:
        if not _watching:
            return False
        return (time.monotonic() - _last_report) < WATCH_GRACE_SECONDS


# --- raising one ----------------------------------------------------------

def _windows_command(title: str, body: str) -> Optional[List[str]]:
    """A toast, via the WinRT bindings PowerShell can reach.

    The AppUserModelID is PowerShell's own, registered by the shell. A toast
    needs an ID that Windows already knows, and this app -- unsigned, often run
    from wherever it was downloaded to, with no Start Menu shortcut -- has no
    such thing to offer. The cost is that Action Center files the notification
    under Windows PowerShell. The alternative was no notification.
    """
    def ps_string(text: str) -> str:
        # PowerShell's single-quoted strings escape one character, by doubling.
        return "'" + text.replace("'", "''") + "'"

    app_id = ("{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}"
              "\\WindowsPowerShell\\v1.0\\powershell.exe")

    script = "; ".join([
        "$ErrorActionPreference = 'Stop'",
        "[void][Windows.UI.Notifications.ToastNotificationManager, "
        "Windows.UI.Notifications, ContentType = WindowsRuntime]",
        "[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, "
        "ContentType = WindowsRuntime]",
        "$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02)",
        "$t = $x.GetElementsByTagName('text')",
        f"[void]$t.Item(0).AppendChild($x.CreateTextNode({ps_string(title)}))",
        f"[void]$t.Item(1).AppendChild($x.CreateTextNode({ps_string(body)}))",
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($x)",
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        f"{ps_string(app_id)}).Show($toast)",
    ])
    return ["powershell", "-NoProfile", "-NonInteractive",
            "-WindowStyle", "Hidden", "-Command", script]


def _mac_command(title: str, body: str) -> Optional[List[str]]:
    """osascript, the one notifier every Mac is guaranteed to have."""
    def as_string(text: str) -> str:
        # AppleScript has three escapes and will not take a literal newline
        # inside a quoted string.
        return (text.replace("\\", "\\\\").replace('"', '\\"')
                    .replace("\n", "\\n"))

    return ["osascript", "-e",
            f'display notification "{as_string(body)}" '
            f'with title "{as_string(APP_NAME)}" '
            f'subtitle "{as_string(title)}"']


def _linux_command(title: str, body: str) -> Optional[List[str]]:
    """notify-send, and KDE's own if that is what this desktop has.

    Neither is guaranteed. Linux has no notifier that is always installed, and
    a headless machine has nowhere to put one anyway -- so returning None here
    is an ordinary outcome, not a failure.
    """
    send = shutil.which("notify-send")
    if send:
        return [send, "--app-name", APP_NAME, "--", title, body]
    kdialog = shutil.which("kdialog")
    if kdialog:
        return [kdialog, "--title", APP_NAME, "--passivepopup",
                f"{title}\n{body}", "10"]
    return None


def _command(title: str, body: str) -> Optional[List[str]]:
    if WINDOWS:
        return _windows_command(title, body)
    if MAC:
        return _mac_command(title, body)
    if LINUX:
        return _linux_command(title, body)
    return None


def _raise_it(title: str, body: str) -> None:
    cmd = _command(title, body)
    if not cmd:
        print(f"[notify] nothing on this system can raise one: {title}",
              flush=True)
        return
    try:
        # hidden_kwargs, because a windowed build that flashes a black console
        # every time a render finishes looks broken in exactly the way an
        # unsigned exe can least afford.
        done = subprocess.run(cmd, timeout=TIMEOUT_SECONDS,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE,
                              **proc.hidden_kwargs())
        if done.returncode != 0:
            why = (done.stderr or b"").decode("utf-8", "replace").strip()
            print(f"[notify] {cmd[0]} refused ({done.returncode}): "
                  f"{why.splitlines()[0] if why else 'no reason given'}",
                  flush=True)
    except Exception as e:      # noqa: BLE001 - never worth failing a run over
        print(f"[notify] could not raise one ({e.__class__.__name__}: {e})",
              flush=True)


def send(title: str, body: str) -> None:
    """Raise a desktop notification. Returns immediately; never raises."""
    threading.Thread(target=_raise_it, args=(title, body),
                     name="notify", daemon=True).start()


def send_if_away(title: str, body: str) -> bool:
    """Raise one only if nobody appears to be looking at the app.

    Returns whether it was sent, which is worth having in the log: "finished
    while you were watching" and "notification failed" look identical from the
    outside otherwise.
    """
    if someone_is_watching():
        return False
    send(title, body)
    return True
