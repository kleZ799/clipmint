"""Which processor does the heavy lifting: the video encoder and Whisper's device.

Two stages can use a GPU, and they need different things from it.

**Encoding** every clip is ffmpeg's work, and ffmpeg can hand it to the
graphics chip: NVENC on NVIDIA, VideoToolbox on a Mac, Quick Sync on Intel,
AMF on AMD. That needs only the graphics driver the machine already has, and
it frees the CPU for everything else.

**Transcription** runs on CTranslate2, which can use an NVIDIA GPU through
CUDA -- but only if NVIDIA's CUDA maths libraries (cuBLAS, cuDNN) are present.
The published single-file builds leave those out on purpose: they would add
about 2 GB to every download. So on most NVIDIA machines the GPU is *there*,
CTranslate2 counts it, the run starts on it, and then dies looking for
cublas64_12.dll. That fell back to the CPU, but only after printing an error
that read like the app had broken. So the libraries are now checked for
before the GPU is tried, and a missing one is a plain sentence in the log.

The setting has three values, stored as PROCESSOR:

  auto  the fastest thing that actually works here, found by trying it
  gpu   the same search, but said out loud when there is no GPU to use
  cpu   never touch a GPU -- the escape hatch for a driver that misbehaves

Nothing is assumed from a name. An encoder that ffmpeg lists is given a
one-second test encode before it is used, and one that fails mid-run is
dropped for the rest of the run and the clip is redone on the CPU. Every test
goes through proc.run, so Pause holds it like any other ffmpeg call.
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
from typing import Callable, Dict, List, Optional, Tuple

from . import proc

MODES = ("auto", "gpu", "cpu")

# (encoder, what to call it, arguments for a given quality). Tried in this
# order: NVENC is the fastest and most consistent of the hardware encoders,
# VideoToolbox only exists on a Mac, and Quick Sync and AMF are what is left.
# Each maps libx264's CRF onto its own constant-quality control, so a clip
# looks the same whichever chip made it.
_HW: List[Tuple[str, str, Callable[[int], List[str]]]] = [
    ("h264_nvenc", "NVIDIA GPU (NVENC)", lambda q: [
        "-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq",
        "-rc", "vbr", "-cq", str(q), "-b:v", "0", "-pix_fmt", "yuv420p"]),
    ("h264_videotoolbox", "Apple GPU (VideoToolbox)", lambda q: [
        "-c:v", "h264_videotoolbox", "-q:v", str(max(35, min(85, 110 - 2 * q))),
        "-pix_fmt", "yuv420p"]),
    ("h264_qsv", "Intel GPU (Quick Sync)", lambda q: [
        "-c:v", "h264_qsv", "-preset", "medium", "-global_quality", str(q),
        "-pix_fmt", "nv12"]),
    ("h264_amf", "AMD GPU (AMF)", lambda q: [
        "-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp",
        "-qp_i", str(q), "-qp_p", str(q + 2), "-pix_fmt", "yuv420p"]),
]
CPU_ENCODER = "libx264"
CPU_LABEL = "CPU (x264)"

_lock = threading.Lock()
_probed: Optional[Tuple[str, str]] = None       # the working hardware encoder, if any
_probe_done = False
_broken: set = set()                            # hardware encoders that failed mid-run
_cuda_failed = False
_announced: Dict[str, str] = {}


def mode() -> str:
    from .config import current_processor
    m = (current_processor() or "auto").strip().lower()
    return m if m in MODES else "auto"


def _say(key: str, message: str) -> None:
    """Log a choice once, and again only if it changes."""
    if _announced.get(key) != message:
        _announced[key] = message
        print(message, flush=True)


def reset_run() -> None:
    """Forget encoders that failed during the last run; give them another go."""
    global _cuda_failed
    with _lock:
        _broken.clear()
    _cuda_failed = False


# --- video ----------------------------------------------------------------

def _listed_encoders() -> str:
    try:
        return proc.run(["ffmpeg", "-hide_banner", "-encoders"],
                        capture_output=True, text=True, timeout=30).stdout or ""
    except Exception:
        return ""


def _test_encode(args: List[str]) -> bool:
    """One second of test pattern through this encoder, thrown away."""
    try:
        r = proc.run(["ffmpeg", "-hide_banner", "-v", "error",
                      "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30", "-t", "1",
                      *args, "-f", "null", "-"],
                     capture_output=True, text=True, timeout=45)
        return r.returncode == 0
    except Exception:
        return False


def probe_video(force: bool = False) -> Optional[Tuple[str, str]]:
    """The first hardware encoder that really works here, found once."""
    global _probed, _probe_done
    with _lock:
        if _probe_done and not force:
            return _probed
    listed = _listed_encoders()
    found = None
    for name, label, build in _HW:
        if f" {name} " not in listed:
            continue
        if _test_encode(build(23)):
            found = (name, label)
            break
    with _lock:
        _probed, _probe_done = found, True
    return found


def video_encoder() -> Tuple[str, str]:
    """(encoder, label) the next encode will use under the current setting."""
    m = mode()
    if m == "cpu":
        _say("video", "[accel] video encoding on the CPU (Processor: CPU)")
        return CPU_ENCODER, CPU_LABEL
    hw = probe_video()
    if hw and hw[0] not in _broken:
        _say("video", f"[accel] video encoding on the {hw[1]}")
        return hw
    why = ("it failed earlier in this run" if hw else "no working hardware encoder was found")
    _say("video", f"[accel] video encoding on the CPU - {why}"
         + (" (Processor is set to GPU)" if m == "gpu" else ""))
    return CPU_ENCODER, CPU_LABEL


def video_args(crf: int = 23, preset: str = "medium") -> List[str]:
    """ffmpeg output arguments for an h264 encode at this quality."""
    name, _ = video_encoder()
    if name == CPU_ENCODER:
        return ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p"]
    build = next(b for n, _, b in _HW if n == name)
    return build(crf)


def run_encode(build_cmd: Callable[[List[str]], List[str]], what: str,
               crf: int = 23, preset: str = "medium", cwd: Optional[str] = None):
    """Run an ffmpeg encode on the chosen encoder, and on the CPU if that fails.

    `build_cmd` takes the encoder arguments and returns the whole command, so
    a retry is the same command with different arguments in the middle. A
    hardware encoder that fails is marked broken for the rest of the run: an
    NVENC session limit or a driver fault will not clear up on the next clip,
    and trying it ten times costs ten failed encodes.
    """
    name, label = video_encoder()
    try:
        return proc.run_checked(build_cmd(video_args(crf, preset)), what=what, cwd=cwd)
    except RuntimeError as e:
        if name == CPU_ENCODER:
            raise
        with _lock:
            _broken.add(name)
        first = (str(e).strip().splitlines() or [""])[-1][:140]
        print(f"[accel] the {label} could not encode this clip ({first}) - "
              f"redoing it on the CPU, and using the CPU for the rest of this run",
              flush=True)
        cpu = ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p"]
        return proc.run_checked(build_cmd(cpu), what=what, cwd=cwd)


# --- transcription --------------------------------------------------------

# What CTranslate2 4.x needs from NVIDIA to run on CUDA, per platform.
_CUDA_LIBS = {
    "nt": ("cublas64_12.dll", "cudnn64_9.dll"),
    "posix": ("libcublas.so.12", "libcudnn.so.9"),
}


def _missing_cuda_libs() -> List[str]:
    """The CUDA libraries CTranslate2 needs that cannot be loaded here."""
    if sys.platform == "darwin":
        return []
    names = _CUDA_LIBS.get(os.name, ())
    missing = []
    for lib in names:
        try:
            (ctypes.WinDLL if os.name == "nt" else ctypes.CDLL)(lib)
        except OSError:
            missing.append(lib)
    return missing


def mark_cuda_failed() -> None:
    """A real transcription failed on CUDA: stay off it for the rest of the run."""
    global _cuda_failed
    _cuda_failed = True


def whisper_device(register: Optional[Callable[[], None]] = None) -> Tuple[str, str]:
    """("cuda" | "cpu", the reason, in words someone can act on)."""
    from .config import LOCAL_WHISPER_DEVICE
    forced = (LOCAL_WHISPER_DEVICE or "auto").strip().lower()
    if forced not in ("", "auto"):
        return forced, f"LOCAL_WHISPER_DEVICE is set to {forced}"

    m = mode()
    if m == "cpu":
        return "cpu", "Processor is set to CPU"
    if sys.platform == "darwin":
        return "cpu", ("the transcription engine cannot use Apple GPUs - it runs on "
                       "the CPU, and video encoding uses the GPU instead")
    if _cuda_failed:
        return "cpu", "the GPU failed earlier in this run"
    if register:
        register()
    try:
        import ctranslate2  # type: ignore
        count = ctranslate2.get_cuda_device_count()
    except Exception:
        count = 0
    if count <= 0:
        return "cpu", "there is no NVIDIA GPU"
    missing = _missing_cuda_libs()
    if missing:
        return "cpu", ("there is an NVIDIA GPU, but NVIDIA's CUDA libraries are not "
                       f"installed ({', '.join(missing)} missing) - the download leaves "
                       "them out to stay small. Video encoding still uses the GPU")
    return "cuda", "NVIDIA GPU with CUDA"


def status() -> Dict:
    """What each stage will run on, for the Processor box under the preview.

    Finding the encoder means running ffmpeg, and ffmpeg waits while a job is
    paused -- so opening Settings on a paused run would hang the panel on a
    test encode. Until the first check has happened, a paused app reports the
    encoder as unknown instead of looking.
    """
    from .local.transcriber import _register_cuda_dlls
    device, why = whisper_device(_register_cuda_dlls)
    out = {"mode": mode(), "transcribe_device": device, "transcribe_reason": why,
           "checked": _probe_done}
    if proc.is_paused() and not _probe_done:
        out.update({"video_encoder": "", "video_label": "",
                    "gpu_encoder_found": False, "gpu_encoder_label": ""})
        return out
    name, label = video_encoder()
    hw = probe_video()
    out.update({"video_encoder": name, "video_label": label, "checked": True,
                "gpu_encoder_found": bool(hw), "gpu_encoder_label": hw[1] if hw else ""})
    return out
