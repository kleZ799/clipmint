"""Every external program this app runs goes through here.

Two problems, one place to solve them.

**No console windows.** A windowed build has no console of its own, so every
time Windows starts a console program from it — ffmpeg, ffprobe, yt-dlp's own
ffmpeg calls — it helpfully creates a new one. The result is black windows
flashing open and shut throughout a run, dozens of them on a long video. People
reasonably read that as malware, and a tool that looks like malware does not get
run twice. CREATE_NO_WINDOW stops it.

**Pause.** Rendering saturates the CPU, which is fine until someone wants to use
their machine for something else. Pausing a job means actually suspending the
processes doing the work, not just declining to start the next one: ffmpeg holds
every core it can get, and asking it to stop politely between clips is no help
to someone whose machine is unusable *now*.

So this module keeps a registry of the children it started, suspends them on
request, and blocks the worker before it starts another.
"""
from __future__ import annotations

import ctypes
import errno
import os
import signal
import subprocess
import sys
import threading
from typing import List, Optional

# --- no console windows ---------------------------------------------------

if os.name == "nt":
    NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
else:
    NO_WINDOW = 0


def _hidden(kwargs: dict) -> dict:
    """Add the flags that keep a console program from opening a window."""
    if os.name != "nt":
        return kwargs
    kwargs = dict(kwargs)
    kwargs["creationflags"] = kwargs.get("creationflags", 0) | NO_WINDOW
    si = kwargs.get("startupinfo") or subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    kwargs["startupinfo"] = si
    return kwargs


def hidden_kwargs(**kwargs) -> dict:
    """Popen keyword arguments that keep a console program windowless.

    The same flags silence_console_windows() applies wholesale, for callers
    that want them on one specific call rather than on everything. Harmless
    and empty off Windows.
    """
    return _hidden(kwargs)


_patched = False


def silence_console_windows() -> None:
    """Make *every* subprocess in this process start without a window.

    Our own calls go through run() below and do not need this. yt-dlp's do:
    it spawns ffmpeg itself, from inside a library, with no way to pass
    creationflags in. Rather than fork it or give up on the worst offender —
    a download spawns several ffmpeg calls — the default is changed underneath
    it.

    Deliberately narrow: Windows only, and only when there is no console to
    inherit, so running from a terminal still behaves normally and nothing
    about this affects a developer watching output scroll past.
    """
    global _patched
    if _patched or os.name != "nt":
        return
    if sys.stdout is not None and sys.stdout.isatty():
        return          # a real console: leave well alone

    original = subprocess.Popen.__init__

    def patched(self, *args, **kwargs):
        return original(self, *args, **_hidden(kwargs))

    subprocess.Popen.__init__ = patched
    _patched = True


# --- pause and resume -----------------------------------------------------

_lock = threading.Lock()
_children: List[subprocess.Popen] = []
_paused = threading.Event()         # set == paused
_resume = threading.Event()
_resume.set()


def _suspend_pid(pid: int) -> None:
    if os.name == "nt":
        # NtSuspendProcess is the only way to stop a process wholesale on
        # Windows; there is no SIGSTOP. It is undocumented but has been in
        # ntdll since NT and is what every process explorer uses.
        PROCESS_SUSPEND_RESUME = 0x0800
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
        if handle:
            try:
                ctypes.windll.ntdll.NtSuspendProcess(handle)
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
    else:
        os.kill(pid, signal.SIGSTOP)


def _resume_pid(pid: int) -> None:
    if os.name == "nt":
        PROCESS_SUSPEND_RESUME = 0x0800
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
        if handle:
            try:
                ctypes.windll.ntdll.NtResumeProcess(handle)
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
    else:
        os.kill(pid, signal.SIGCONT)


def pause() -> bool:
    """Suspend everything running now, and hold the next thing back."""
    with _lock:
        if _paused.is_set():
            return False
        _paused.set()
        _resume.clear()
        for child in list(_children):
            if child.poll() is None:
                try:
                    _suspend_pid(child.pid)
                except Exception:
                    pass        # already gone, or not ours to suspend
        return True


def resume() -> bool:
    with _lock:
        if not _paused.is_set():
            return False
        for child in list(_children):
            if child.poll() is None:
                try:
                    _resume_pid(child.pid)
                except Exception:
                    pass
        _paused.clear()
        _resume.set()
        return True


def is_paused() -> bool:
    return _paused.is_set()


def wait_if_paused() -> None:
    """Block here while paused. Called before starting each child."""
    _resume.wait()


def clear() -> None:
    """Drop any pause state. Called when a job ends, so the next one is free."""
    with _lock:
        _children.clear()
    _paused.clear()
    _resume.set()


# --- running things -------------------------------------------------------

def run(cmd, *, check: bool = False, capture_output: bool = False,
        text: Optional[bool] = None, timeout: Optional[float] = None,
        **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run, with no console window and honouring pause.

    A paused job stops *before* the next program starts as well as suspending
    the one already running, so pausing during a five-clip render does not
    quietly let the remaining four begin.
    """
    wait_if_paused()

    popen_kwargs = _hidden(dict(kwargs))
    if capture_output:
        popen_kwargs.setdefault("stdout", subprocess.PIPE)
        popen_kwargs.setdefault("stderr", subprocess.PIPE)
    if text is not None:
        popen_kwargs["text"] = text

    proc = subprocess.Popen(cmd, **popen_kwargs)
    with _lock:
        _children.append(proc)
        # A paused job may have started this one in the gap between the wait
        # above and here. Suspend it immediately rather than letting it run.
        if _paused.is_set():
            try:
                _suspend_pid(proc.pid)
            except Exception:
                pass
    try:
        out, err = proc.communicate(timeout=timeout)
    finally:
        with _lock:
            if proc in _children:
                _children.remove(proc)

    result = subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, out, err)
    return result


# --- failing children -----------------------------------------------------

# ffmpeg does not exit with 1. It exits with its own AVERROR code, a negative
# number that Windows reports back as unsigned 32-bit -- which is how a failed
# render comes to be logged as "exit status 3752568763". Those four bytes are
# ASCII: 3752568763 is -MKTAG('E','X','T',' '), AVERROR_EXTERNAL. Nobody
# reading a bug report should have to do that arithmetic by hand.
_AVERROR_TAGS = {
    "BUG!": ("AVERROR_BUG", "an internal ffmpeg bug"),
    "BUG ": ("AVERROR_BUG2", "an internal ffmpeg bug"),
    "BUFS": ("AVERROR_BUFFER_TOO_SMALL", "a buffer was too small"),
    "EOF ": ("AVERROR_EOF", "the input ended earlier than expected"),
    "EXIT": ("AVERROR_EXIT", "ffmpeg was asked to stop"),
    "EXT ": ("AVERROR_EXTERNAL",
             "a library ffmpeg calls failed -- in an encode, that is libx264"),
    "INDA": ("AVERROR_INVALIDDATA", "invalid data in the input file"),
    "PAWE": ("AVERROR_PATCHWELCOME", "ffmpeg does not implement what was asked for"),
    "UNKN": ("AVERROR_UNKNOWN", "an unknown error"),
}

# The same family, for the codes whose first byte is 0xF8 rather than a letter.
_AVERROR_MISSING = {
    "BSF": "a bitstream filter", "DEC": "a decoder", "DEM": "a demuxer",
    "ENC": "an encoder", "FIL": "a filter", "MUX": "a muxer",
    "OPT": "an option", "PRO": "a protocol", "STR": "a stream",
}


def explain_exit_status(code: Optional[int]) -> str:
    """Turn an ffmpeg exit code into a sentence, or "" if it is not one."""
    if code is None or 0 <= code < 256:
        return ""                       # an ordinary small exit code
    signed = code - (1 << 32) if code > 0x7FFFFFFF else code
    n = -signed
    if not 0 < n < (1 << 32):
        return ""

    if n < 256:                         # AVERROR(errno)
        name = errno.errorcode.get(n, str(n))
        return f"exit status {code} is AVERROR({name}): {os.strerror(n)}"

    raw = bytes(((n >> shift) & 0xFF for shift in (0, 8, 16, 24)))
    if raw[0] == 0xF8:
        missing = _AVERROR_MISSING.get(raw[1:].decode("latin-1"))
        return (f"exit status {code} is ffmpeg reporting that it could not "
                f"find {missing}") if missing else ""
    try:
        tag = raw.decode("ascii")
    except UnicodeDecodeError:
        return ""
    known = _AVERROR_TAGS.get(tag)
    if not known:
        return ""
    name, meaning = known
    return f"exit status {code} is ffmpeg's {name}: {meaning}"


def run_checked(cmd, *, what: str = "ffmpeg", capture_stdout: bool = False,
                tail: int = 12, cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    """Run a child that has to succeed, and if it does not, say why.

    check=True raises CalledProcessError, whose message is the command and a
    number -- and the number is the AVERROR above, which reads as noise. The
    actual reason was printed to stderr, and there it dies: a windowed build
    has no console, so the one line explaining the failure is written to a
    handle that goes nowhere. Every render failure then looks identical from
    the outside, which is no use to anyone reporting one.

    So stderr is captured and put in the exception instead. -loglevel error
    means there are a handful of lines at most, and communicate() drains the
    pipe, so nothing here can fill a buffer and stall a long encode.
    """
    kwargs = {"stderr": subprocess.PIPE, "text": True}
    if capture_stdout:
        kwargs["stdout"] = subprocess.PIPE
    # A working folder lets a filter name its own files (a subtitles script,
    # a fonts folder) relatively. An absolute path inside a filtergraph needs
    # escaping at two levels, and a Windows drive colon or an apostrophe in a
    # user's name gets one of them wrong sooner or later.
    if cwd:
        kwargs["cwd"] = cwd

    result = run(cmd, **kwargs)
    if result.returncode == 0:
        return result

    said = [line for line in (result.stderr or "").splitlines() if line.strip()]
    parts = ["\n".join(said[-tail:])] if said else []
    hint = explain_exit_status(result.returncode)
    if hint:
        parts.append(f"({hint})")
    if not parts:
        parts.append(f"it exited with status {result.returncode} and said nothing")
    raise RuntimeError(f"{what} failed: " + "\n".join(parts))


def popen(cmd, **kwargs) -> subprocess.Popen:
    """Start a child and hand it back, still running.

    run() above covers every call that just needs an exit code. Reading a
    long stream out of ffmpeg while it is still writing needs the process
    itself -- but it still has to be hidden, still has to be suspendable, and
    still has to be dropped from the registry when it ends, so it goes through
    here rather than reaching for subprocess directly.
    """
    wait_if_paused()
    child = subprocess.Popen(cmd, **_hidden(dict(kwargs)))
    with _lock:
        _children.append(child)
        if _paused.is_set():
            try:
                _suspend_pid(child.pid)
            except Exception:
                pass
    return child


def forget(child: subprocess.Popen) -> None:
    """Drop a popen() child from the pause registry once it has finished."""
    with _lock:
        if child in _children:
            _children.remove(child)
