"""Desktop entry point — ClipMint as an app window, not a website.

Starts the local server on a free port in a background thread, then opens it
in a native window (Edge WebView2 on Windows, WebKit elsewhere) so there is no
address bar, no browser tab, and nothing for the user to "visit".

If a native webview isn't available it falls back to the default browser
rather than failing, because a working browser window beats no app at all.

Run from source:   python desktop.py
Packaged:          ClipMint.exe, ClipMint.app on a Mac, or a bare
                   ClipMint binary on Linux
"""
import os
import socket
import sys
import threading
import time
import traceback
from contextlib import closing
from typing import Optional

APP_NAME = "ClipMint"


def _resource_root() -> str:
    """Directory holding bundled data — differs under PyInstaller."""
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def _bundled_bin() -> Optional[str]:
    """Where this build put ffmpeg, if it shipped one.

    On Windows the bundle is one flat directory, so this is just bin/ beside
    everything else. A mac .app splits itself in two — code in
    Contents/Frameworks, data files in Contents/Resources — and _MEIPASS
    points at the code half. PyInstaller does symlink between them, but
    "usually there is a symlink" is a thin thing to rest the whole
    ffmpeg-is-included promise on, so the other half is checked as well.
    """
    root = _resource_root()
    for path in (os.path.join(root, "bin"),
                 os.path.join(os.path.dirname(root), "Resources", "bin")):
        if os.path.isdir(path):
            return path
    return None


def _media_dir_name() -> str:
    """The folder this platform actually keeps video in.

    macOS has no Videos folder. Inventing one would put every clip somewhere
    Finder never suggests and the user never thinks to look.
    """
    return "Movies" if sys.platform == "darwin" else "Videos"


def _prepare_environment() -> None:
    """Make the app behave the same whether run from source or a bundle."""
    # Bundled ffmpeg/ffprobe, if the build shipped them, take priority.
    bundled_bin = _bundled_bin()
    if bundled_bin:
        os.environ["PATH"] = bundled_bin + os.pathsep + os.environ.get("PATH", "")

    # A packaged app must not write clips inside itself (often Program Files,
    # often read-only; on a Mac, inside the .app bundle, where the next update
    # would take them with it). Work in the user's video folder instead.
    if getattr(sys, "frozen", False):
        home = os.path.expanduser("~")
        base = os.path.join(home, _media_dir_name(), "ClipMint")
        for sub in ("", "output", "webapp_output", "webapp_uploads"):
            os.makedirs(os.path.join(base, sub), exist_ok=True)
        os.chdir(base)
        os.environ.setdefault("LOCAL_OUTPUT_DIR", "output")


LOG_FILENAME = "app.log"


def _stream_is_usable(stream) -> bool:
    """Whether something can be treated as a real output stream."""
    try:
        stream.isatty()
        return True
    except Exception:
        return False


def _attach_streams() -> None:
    """Give the app real stdout/stderr, because a windowed build has none.

    Launched from Explorer or the taskbar there is no console, so PyInstaller
    leaves sys.stdout and sys.stderr as None. print() tolerates that, but any
    library reaching for a stream does not: uvicorn's log config calls
    sys.stdout.isatty() and reports the AttributeError as "Unable to configure
    formatter 'default'", which killed the engine thread before it could bind
    a port -- an icon that spun and then did nothing.

    Pointing both at a log file fixes the crash and leaves something to read
    after a launch goes wrong. Redirected runs already have real streams and
    are left alone, which is exactly why this never showed up in testing.
    """
    if _stream_is_usable(sys.stdout) and _stream_is_usable(sys.stderr):
        return

    try:
        log = open(os.path.join(os.getcwd(), LOG_FILENAME), "a",
                   encoding="utf-8", buffering=1)
        log.write(f"\n--- {APP_NAME} started {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    except OSError:
        log = open(os.devnull, "w")

    if not _stream_is_usable(sys.stdout):
        sys.stdout = log
    if not _stream_is_usable(sys.stderr):
        sys.stderr = log


def _alert(message: str, *, error: bool = True) -> None:
    """Tell the user something, even with no console to tell them through.

    The packaged build is windowed, so stderr goes nowhere a user will ever
    look. Anything worth printing on the way out is worth a dialog box.

    Windows and macOS each have one that is always there. Linux has neither,
    so the two most widely installed are tried and the absence of both is not
    treated as a problem: the message has already reached app.log, and a
    missing dialog costs the user a file to go and find, not the information.
    """
    print(message, file=sys.stderr if error else sys.stdout, flush=True)

    if sys.platform == "darwin":
        try:
            import subprocess

            # AppleScript's only string escapes are these three, and osascript
            # will not take a literal newline inside a quoted string.
            body = (message.replace("\\", "\\\\").replace('"', '\\"')
                           .replace("\n", "\\n"))
            subprocess.run(
                ["osascript", "-e",
                 f'display dialog "{body}" with title "{APP_NAME}" '
                 f'buttons {{"OK"}} default button "OK" '
                 f'with icon {"stop" if error else "note"}'],
                timeout=300,
            )
        except Exception:
            pass    # a missing dialog must not become the reason we can't exit
        return

    if sys.platform.startswith("linux"):
        import shutil
        import subprocess

        for tool, args in (
            ("zenity", ["--error" if error else "--info",
                        f"--title={APP_NAME}", f"--text={message}"]),
            ("kdialog", ["--title", APP_NAME,
                         "--error" if error else "--msgbox", message]),
        ):
            path = shutil.which(tool)
            if not path:
                continue
            try:
                subprocess.run([path, *args], timeout=300)
            except Exception:
                pass    # a missing dialog must not become the reason we can't exit
            return
        return

    if sys.platform != "win32":
        return
    try:
        import ctypes

        MB_ICONERROR, MB_ICONINFORMATION = 0x10, 0x40
        ctypes.windll.user32.MessageBoxW(
            None, message, APP_NAME, MB_ICONERROR if error else MB_ICONINFORMATION
        )
    except Exception:
        pass    # a missing dialog must not become the reason we can't exit


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# A fixed port nobody else is likely to want, bound for the lifetime of the
# process purely as a mutex. Windows refuses the second bind, which is the
# whole trick.
LOCK_PORT = 50507


def _claim_single_instance() -> Optional[socket.socket]:
    """Return a held socket, or None if this app is already running.

    A slow start looks exactly like a dead one, so the natural reaction is to
    click the icon again. Four copies each unpacking themselves and loading
    the same models is how a slow start becomes a failed one -- so only the
    first copy gets to run. SO_REUSEADDR is deliberately not set: the default
    refusal is the lock.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
    except OSError:
        s.close()
        return None
    return s


# A cold start pays for imports of faster-whisper, ctranslate2, cv2 and the
# Google client, read off disk while the virus scanner reads them too. Sixty
# seconds was enough warm and not enough cold, which showed up as a taskbar
# click that spun and then did nothing at all. The engine thread is watched
# separately, so a genuine failure still reports in seconds -- this ceiling
# only has to outlast a slow machine having a bad morning.
STARTUP_TIMEOUT_SECONDS = 300.0


def _wait_until_up(port: int, engine: threading.Thread,
                   timeout: float = STARTUP_TIMEOUT_SECONDS) -> bool:
    """Block until the server accepts connections, or give up.

    Watches the engine thread too: if it died on an import or a bind error
    there is nothing left to wait for, and spending the full timeout on a
    corpse just turns a clear error into a hang.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
            s.settimeout(0.4)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        if not engine.is_alive():
            return False
        time.sleep(0.2)
    return False


# Set by the engine thread when it dies, so main() can say *why* rather than
# reporting a bare timeout. A daemon thread's traceback goes to a stderr the
# windowed build does not have, so it has to be handed back deliberately.
_engine_error: Optional[BaseException] = None


def _serve(port: int) -> None:
    global _engine_error
    try:
        import uvicorn
        from webapp.server import app

        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    except BaseException as e:  # noqa: BLE001 - last chance to report anything
        _engine_error = e
        traceback.print_exc()


def main() -> int:
    _prepare_environment()
    _attach_streams()   # after the chdir, so the log lands in the app's folder

    lock = _claim_single_instance()
    if lock is None:
        _alert(f"{APP_NAME} is already running.\n\n"
               f"Look for its window — if you just clicked the icon, the "
               f"first start can take a moment.", error=False)
        return 0

    # Before anything spawns a child. A windowed build has no console, so
    # every ffmpeg call Windows starts on its behalf gets a brand new one --
    # black windows blinking open and shut for the length of a render, which
    # is what makes a legitimate unsigned exe look like something that should
    # not be trusted.
    try:
        from shorts_generator.proc import silence_console_windows
        silence_console_windows()
    except Exception:
        pass        # cosmetic; never a reason to fail to start

    # An update renames the old exe aside rather than deleting it, because it
    # is still running at that moment. This is the first launch where nothing
    # holds it, so this is where it goes.
    try:
        from webapp.updater import cleanup_previous
        cleanup_previous()
    except Exception:
        pass        # a leftover file is not a reason to fail to start

    port = _free_port()
    url = f"http://127.0.0.1:{port}"

    print(f"[{APP_NAME}] starting engine on {url}", flush=True)
    print(f"[{APP_NAME}] working directory: {os.getcwd()}", flush=True)

    engine = threading.Thread(target=_serve, args=(port,), daemon=True)
    engine.start()

    if not _wait_until_up(port, engine):
        if _engine_error is not None:
            _alert(f"{APP_NAME} could not start its engine:\n\n{_engine_error}")
        else:
            _alert(f"{APP_NAME}'s engine did not start in time.\n\n"
                   f"Try launching it again — the first start after a reboot "
                   f"is the slowest.")
        return 1

    try:
        import webview  # type: ignore
    except ImportError:
        webview = None

    if webview is not None:
        try:
            webview.create_window(APP_NAME, url, width=1280, height=900,
                                  min_size=(900, 640))
            # http_server=False: we already run our own server.
            webview.start()
            return 0
        except Exception as e:
            print(f"Native window unavailable ({e}); opening your browser instead.",
                  flush=True)

    import webbrowser
    print(f"\n  {APP_NAME} is running at {url}\n  Close this window to quit.\n", flush=True)
    webbrowser.open(url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
