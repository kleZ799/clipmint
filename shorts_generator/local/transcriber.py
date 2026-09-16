"""Local transcription via faster-whisper.

Reads a local media file and returns the same shape the highlight generator
expects: {duration, segments[start, end, text]}.
"""
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

import sys

from .. import proc, user_config
from ..config import LOCAL_OUTPUT_DIR, LOCAL_WHISPER_DEVICE, LOCAL_WHISPER_MODEL


def _register_cuda_dlls() -> None:
    """Put the pip-installed CUDA libraries on Windows' DLL search path.

    nvidia-cublas-cu12 and nvidia-cudnn-cu12 drop their DLLs under
    site-packages/nvidia/*/bin, which Python 3.8+ does NOT search -- and
    CTranslate2, unlike torch, never registers them. The result is a GPU that
    is present, a model that constructs, and an inference call that dies on
    "cublas64_12.dll is not found". Registering the directories is the whole
    fix; without it, installing the wheels changes nothing at all.

    Frozen builds keep the same layout under the unpack root, so both are
    checked. Missing directories are ordinary: a CPU-only install has none,
    and the caller falls back on its own.
    """
    if os.name != "nt" or not hasattr(os, "add_dll_directory"):
        return

    roots = []
    try:
        import nvidia  # type: ignore
        roots.extend(Path(p) for p in nvidia.__path__)
    except ImportError:
        pass
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        roots.append(Path(bundled) / "nvidia")

    for root in roots:
        for lib in ("cublas", "cudnn", "cuda_nvrtc", "cuda_runtime"):
            d = root / lib / "bin"
            if d.is_dir():
                try:
                    os.add_dll_directory(str(d))
                except OSError:
                    pass    # already registered, or gone between check and use


def _cache_candidates(media_path: str) -> List[Path]:
    """Every place this file's transcript could live, best first.

    The cache used to be built from LOCAL_OUTPUT_DIR alone, which is relative
    and therefore resolves against the *current working directory* — while the
    video it belongs to is written under OUTPUT_ROOT. Those are the same folder
    by default and diverge the moment someone moves their save location, at
    which point the transcript is written somewhere the lookup never checks and
    every video re-transcribes from scratch. Twenty minutes, silently, again.

    So the cache follows the video. The older locations are still *read*, so an
    existing transcript is never orphaned by this change.
    """
    media = Path(media_path)
    name = media.stem + ".srt"

    candidates = [media.with_suffix(".srt")]
    try:
        candidates.append(user_config.source_dir() / name)
    except OSError:
        pass                        # unreadable config dir is not fatal here
    candidates.append(Path(LOCAL_OUTPUT_DIR) / name)

    out: List[Path] = []
    seen = set()
    for path in candidates:
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def _find_cached_transcript(media_path: str) -> Optional[Path]:
    """An existing .srt for this media file, wherever an older run left it."""
    for path in _cache_candidates(media_path):
        if path.exists():
            return path
    return None


def _transcript_cache_path(media_path: str) -> Path:
    """Where to write this media file's .srt cache.

    Prefers the folder holding the video, so the pair stays together. Falls
    back through the configured folders when that directory cannot be written
    to — a read-only share or a mounted drive must cost the cache, not the run.
    """
    existing = _find_cached_transcript(media_path)
    if existing:
        return existing

    candidates = _cache_candidates(media_path)
    for path in candidates:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            probe = path.parent / ".clipmint-write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return path
        except OSError:
            continue
    return candidates[0]


def _format_srt_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _parse_srt_timestamp(value: str) -> float:
    match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})", value.strip())
    if not match:
        raise ValueError(f"Invalid SRT timestamp: {value!r}")
    hours, minutes, seconds, millis = map(int, match.groups())
    return hours * 3600 + minutes * 60 + seconds + (millis / 1000.0)


def _write_srt_cache(media_path: str, transcript: Dict) -> Path:
    cache_path = _transcript_cache_path(media_path)
    lines = []
    for idx, segment in enumerate(transcript.get("segments", []), start=1):
        start = _format_srt_timestamp(float(segment["start"]))
        end = _format_srt_timestamp(float(segment["end"]))
        text = str(segment.get("text", "")).strip().replace("\r", "").replace("\n", " ")
        lines.append(str(idx))
        lines.append(f"{start} --> {end}")
        lines.append(text)
        lines.append("")

    cache_path.write_text("\n".join(lines), encoding="utf-8")
    return cache_path


def _load_srt_cache(cache_path: Path) -> Dict:
    content = cache_path.read_text(encoding="utf-8-sig").strip()
    if not content:
        return {"duration": 0.0, "segments": []}

    segments = []
    for block in re.split(r"\n\s*\n", content):
        lines = [line.strip("\ufeff") for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        if "-->" not in lines[0] and len(lines) > 1 and "-->" in lines[1]:
            lines = lines[1:]
        if not lines or "-->" not in lines[0]:
            continue
        start_raw, end_raw = [part.strip() for part in lines[0].split("-->", 1)]
        text = "\n".join(lines[1:]).strip()
        segments.append(
            {
                "start": _parse_srt_timestamp(start_raw),
                "end": _parse_srt_timestamp(end_raw),
                "text": text,
            }
        )

    duration = segments[-1]["end"] if segments else 0.0
    return {"duration": duration, "segments": segments}


def _mmss(seconds: float) -> str:
    """Seconds as 34m12s / 12s, for progress lines."""
    s = int(float(seconds or 0))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else (f"{m}m{sec:02d}s" if m else f"{sec}s")


def _resolve_device() -> str:
    """Where transcription runs, under the Processor setting -- see accel.py.

    The old check asked CTranslate2 whether it could *count* a GPU, which it
    can on any NVIDIA machine, and then the run died looking for cuBLAS on
    every copy of the app that ships without it. accel also checks that the
    libraries load, so a missing one is a sentence here instead of a crash
    halfway through the first minute of audio.
    """
    from .. import accel
    device, why = accel.whisper_device(_register_cuda_dlls)
    print(f"[transcribe/local] transcribing on the {'GPU' if device == 'cuda' else 'CPU'}"
          f" - {why}", flush=True)
    return device


def transcribe_local(media_path: str, language: Optional[str] = None) -> Dict:
    """Run faster-whisper on a local file path, caching the result as .srt."""
    cache_path = _find_cached_transcript(media_path)
    if cache_path is not None:
        source_mtime = os.path.getmtime(media_path)
        cache_mtime = cache_path.stat().st_mtime
        if cache_mtime >= source_mtime:
            print(f"[transcribe/local] reusing cached transcript: {cache_path}", flush=True)
            cached = _load_srt_cache(cache_path)
            # Treat empty cache as invalid (likely from a failed/partial run) — delete and re-transcribe
            if not cached["segments"] or cached["duration"] <= 0.0:
                print(f"[transcribe/local] cache is empty/invalid, deleting: {cache_path}", flush=True)
                cache_path.unlink(missing_ok=True)
            else:
                print(
                    f"[transcribe/local] {len(cached['segments'])} cached segments, "
                    f"{cached['duration']:.0f}s of audio",
                    flush=True,
                )
                return cached

    try:
        _register_cuda_dlls()   # the import itself resolves CUDA libraries
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "faster-whisper is required for --mode local. Install it with:\n"
            "    pip install -r requirements-local.txt"
        ) from e

    device = _resolve_device()
    compute_type = "float16" if device == "cuda" else "int8"
    print(f"[transcribe/local] faster-whisper model={LOCAL_WHISPER_MODEL} device={device}", flush=True)

    from ..config import LOCAL_WHISPER_VAD_FILTER, LOCAL_WHISPER_VAD_PARAMETERS

    transcribe_kwargs = {
        "audio": media_path,
        "language": language,
        "beam_size": 5,
        "condition_on_previous_text": False,
    }
    if LOCAL_WHISPER_VAD_FILTER:
        transcribe_kwargs["vad_filter"] = True
        transcribe_kwargs["vad_parameters"] = LOCAL_WHISPER_VAD_PARAMETERS
    else:
        transcribe_kwargs["vad_filter"] = False

    def _run(dev: str, ct: str):
        """Transcribe end to end on one device, returning finished segments.

        The whole loop lives in here because CTranslate2 does not fail where
        you would expect. A CUDA device can count, and a model can construct
        on it, and the run still dies on the first encode with "Library
        cublas64_12.dll is not found" -- the GPU is present, the maths library
        behind it is not. Only draining the generator proves the device works,
        so the retry has to be able to redo the whole thing.
        """
        model = WhisperModel(LOCAL_WHISPER_MODEL, device=dev, compute_type=ct)
        segments_iter, info = model.transcribe(**transcribe_kwargs)
        # The longest stage by far, and until now it said nothing at all
        # between "transcribing on the CPU" and its result -- half an hour of
        # an app that looks stuck. The segments arrive in order, so each one
        # says how far in we are.
        total = float(getattr(info, "duration", 0.0) or 0.0)
        last_said = 0.0
        out = []
        for s in segments_iter:
            # Transcription runs inside this process, so Pause cannot suspend
            # it the way it suspends ffmpeg. The generator is lazy, though:
            # holding here stops the model decoding the next window, on the
            # CPU or the GPU alike, until the run is resumed.
            proc.wait_if_paused()
            now = time.time()
            if total and now - last_said >= 2.5:
                last_said = now
                print(f"[transcribe/local] {min(99, int(100 * float(s.end) / total))}% "
                      f"- {_mmss(s.end)} of {_mmss(total)}", flush=True)
            out.append({
                "start": float(s.start),
                "end": float(s.end),
                "text": (s.text or "").strip(),
            })
        return out, info

    try:
        segments, info = _run(device, compute_type)
    except Exception as e:
        # The run has already paid for a download. Finish it slowly on the
        # CPU rather than not at all.
        if device != "cuda":
            raise
        from .. import accel
        accel.mark_cuda_failed()
        print(f"[transcribe/local] the GPU stopped partway "
              f"({str(e).splitlines()[0][:120]}) - finishing on the CPU. If this "
              f"keeps happening, set Processor to CPU only, under the live "
              f"preview", flush=True)
        device, compute_type = "cpu", "int8"
        segments, info = _run(device, compute_type)

    duration = float(getattr(info, "duration", 0.0)) or (segments[-1]["end"] if segments else 0.0)
    print(f"[transcribe/local] {len(segments)} segments, {duration:.0f}s of audio", flush=True)
    transcript = {"duration": duration, "segments": segments}
    cache_path = _write_srt_cache(media_path, transcript)
    print(f"[transcribe/local] wrote cache: {cache_path}", flush=True)
    return transcript
