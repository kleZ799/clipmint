"""Local transcription via faster-whisper.

Reads a local media file and returns the same shape the highlight generator
expects: {duration, segments[start, end, text]}.
"""
import json
import math
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

import sys

from .. import proc, user_config
from ..config import LOCAL_OUTPUT_DIR, current_whisper_model


def _register_cuda_dlls() -> None:
    """Put the pip-installed CUDA libraries on Windows' DLL search path.

    nvidia-cublas-cu12 and nvidia-cudnn-cu12 drop their DLLs under
    site-packages/nvidia/*/bin, which Python 3.8+ does NOT search -- and
    CTranslate2, unlike torch, never registers them. The result is a GPU that
    is present, a model that constructs, and an inference call that dies on
    "cublas64_12.dll is not found". Registering the directories is the whole
    fix; without it, installing the wheels changes nothing at all.

    Frozen builds keep the same layout under the unpack root, so both are
    checked. We also check the CUDA Toolkit install on Windows (via the
    CUDA_PATH environment variable or common install locations), so the app
    works even when only the toolkit is installed rather than the pip wheels.
    Missing directories are ordinary: a CPU-only install has none, and the
    caller falls back on its own.
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

    # In a frozen PyInstaller executable, user site-packages is not on sys.path,
    # so `import nvidia` fails even when `pip install nvidia-cublas-cu12` was run.
    # Scan user and system Python site-packages directories on Windows:
    import glob
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        for p in glob.glob(os.path.join(appdata, "Python", "Python*", "site-packages", "nvidia")):
            roots.append(Path(p))
    localappdata = os.environ.get("LOCALAPPDATA", "")
    if localappdata:
        for p in glob.glob(os.path.join(localappdata, "Programs", "Python", "Python*", "Lib", "site-packages", "nvidia")):
            roots.append(Path(p))
    for p in glob.glob(r"C:\Python*\Lib\site-packages\nvidia"):
        roots.append(Path(p))
    # Also check an active virtualenv or the project's venv when running a
    # frozen dist\ClipMint.exe from the repo on this machine.
    venv_env = os.environ.get("VIRTUAL_ENV", "").strip()
    if venv_env:
        roots.append(Path(venv_env) / "Lib" / "site-packages" / "nvidia")
    exe_dir = Path(sys.executable).resolve().parent
    roots.append(exe_dir / "venv" / "Lib" / "site-packages" / "nvidia")
    roots.append(exe_dir.parent / "venv" / "Lib" / "site-packages" / "nvidia")
    roots.append(Path(__file__).resolve().parent.parent.parent / "venv" / "Lib" / "site-packages" / "nvidia")

    for root in roots:
        for lib in ("cublas", "cudnn", "cuda_nvrtc", "cuda_runtime"):
            for sub in ("bin", "lib"):
                d = root / lib / sub
                if d.is_dir():
                    try:
                        os.add_dll_directory(str(d))
                        os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
                    except OSError:
                        pass    # already registered, or gone between check and use

    # Also register the CUDA Toolkit bin directory on Windows.
    # The pip wheels (nvidia-cublas-cu12 / nvidia-cudnn-cu12) are the preferred
    # source because they include cuDNN, but if only the CUDA Toolkit is
    # installed we can still find cuBLAS there and give a clearer error message
    # about the missing cuDNN rather than a generic "DLL not found".
    _cuda_toolkit_bin = _find_cuda_toolkit_bin()
    if _cuda_toolkit_bin:
        try:
            os.add_dll_directory(str(_cuda_toolkit_bin))
        except OSError:
            pass


def _find_cuda_toolkit_bin() -> "Optional[Path]":
    """Return the CUDA Toolkit bin directory on Windows, or None."""
    if os.name != "nt":
        return None
    # Honour CUDA_PATH if the user or installer set it.
    cuda_path = os.environ.get("CUDA_PATH", "").strip()
    if cuda_path:
        p = Path(cuda_path) / "bin"
        if p.is_dir():
            return p
    # Fall back to scanning common CUDA Toolkit install locations.
    _base = Path(r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA")
    for _ver in ("12.8", "12.7", "12.6", "12.5", "12.4", "12.3", "12.2",
                 "12.1", "12.0", "11.8", "11.7"):
        _candidate = _base / f"v{_ver}" / "bin"
        if _candidate.is_dir():
            return _candidate
    return None


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


# Whisper turns the whole audio into one spectrogram before it decodes a word:
# 201 complex64 values per 10 ms, about 0.58 GB an hour on top of the samples
# themselves. A 32-hour stream asked for 17.3 GB in a single array and died
# with ArrayMemoryError, then again on every retry. So a long source goes
# through in slices, each read by ffmpeg straight to 16 kHz mono, and memory
# stays at what half an hour needs however long the video is.
SLICE_SECONDS = 1800


def _source_seconds(media_path: str) -> float:
    """How long the source is, or 0 when ffprobe cannot say.

    Read as bytes: ffprobe's messages quote the file name, and decoding a name
    like "GTA 6 ⧸ 【PS5 PRO】" in the Windows code page crashed the reader.
    """
    try:
        r = proc.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                      "-of", "csv=p=0", media_path], capture_output=True)
        return float((r.stdout or b"").decode("ascii", "replace").strip() or 0)
    except (ValueError, OSError):
        return 0.0


def _read_slice(media_path: str, start: float, length: float):
    """One slice of the first audio track, as Whisper's float32 16 kHz mono."""
    import numpy as np
    r = proc.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{start:.3f}",
                  "-i", media_path, "-t", f"{length:.3f}", "-map", "0:a:0", "-vn",
                  "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                 capture_output=True)
    if r.returncode != 0:
        said = (r.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(f"ffmpeg could not read the audio at {_mmss(start)}: "
                           f"{said[-1] if said else f'exit status {r.returncode}'}")
    return np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def _partial_path(media_path: str) -> Path:
    """Where finished slices are kept until the whole transcript is written."""
    return _transcript_cache_path(media_path).with_suffix(".partial.json")


def _load_partial(path: Path, total: float, model_name: str) -> Dict:
    """Slices an earlier attempt finished, if they were cut the same way."""
    fresh = {"slice": SLICE_SECONDS, "total": total, "model": model_name,
             "language": None, "done": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fresh
    if (data.get("slice") != SLICE_SECONDS or data.get("model") != model_name
            or abs(float(data.get("total", 0)) - total) > 1
            or not isinstance(data.get("done"), dict)):
        return fresh
    return data


def _transcribe_audio_with_pipeline(faster_whisper, model, audio_input, kwargs: Dict,
                                    device: str):
    """Use BatchedInferencePipeline on CUDA for 4x+ speedup; fallback to model.transcribe."""
    if device == "cuda" and hasattr(faster_whisper, "BatchedInferencePipeline"):
        try:
            pipe = faster_whisper.BatchedInferencePipeline(model=model)
            vad_params = kwargs.get("vad_parameters") or {
                "threshold": 0.35, "min_silence_duration_ms": 1000, "speech_pad_ms": 300,
            }
            call_kw = {
                k: v for k, v in kwargs.items()
                if k not in ("audio", "condition_on_previous_text", "vad_filter", "vad_parameters")
            }
            return pipe.transcribe(
                audio_input,
                batch_size=8,
                vad_filter=True,
                vad_parameters=vad_params,
                **call_kw,
            )
        except Exception as be:
            print(f"[transcribe/local] batched pipeline fallback ({str(be).splitlines()[0][:80]})", flush=True)
    call_kw = {k: v for k, v in kwargs.items() if k != "audio"}
    return model.transcribe(audio_input, **call_kw)


def _transcribe_in_slices(media_path: str, total: float, kwargs: Dict,
                          device: str, compute_type: str, model_name: str) -> Dict:
    """Transcribe a long source half an hour at a time, saving each slice."""
    import faster_whisper  # type: ignore
    WhisperModel = faster_whisper.WhisperModel

    partial = _partial_path(media_path)
    state = _load_partial(partial, total, model_name)
    done = state["done"]
    count = math.ceil(total / SLICE_SECONDS)
    print(f"[transcribe/local] {_mmss(total)} of audio - transcribing it in {count} "
          f"slices of {SLICE_SECONDS // 60} minutes, so memory stays at what one "
          f"slice needs", flush=True)
    if done:
        print(f"[transcribe/local] resuming: {len(done)} of {count} slices were "
              f"already transcribed", flush=True)

    model = None
    last_said = 0.0
    for i in range(count):
        if str(i) in done:
            continue
        start = i * SLICE_SECONDS
        audio = _read_slice(media_path, start, min(SLICE_SECONDS, total - start))
        while True:
            if model is None:
                m_name = current_whisper_model(device) if device != "cuda" else model_name
                model = WhisperModel(m_name, device=device, compute_type=compute_type)
            try:
                slice_kw = dict(kwargs, language=kwargs.get("language") or state.get("language"))
                segments_iter, info = _transcribe_audio_with_pipeline(
                    faster_whisper, model, audio, slice_kw, device
                )
                out = []
                for s in segments_iter:
                    proc.wait_if_paused()
                    at = start + float(s.end)
                    now = time.time()
                    if now - last_said >= 2.5:
                        last_said = now
                        print(f"[transcribe/local] {min(99, int(100 * at / total))}% "
                              f"- {_mmss(at)} of {_mmss(total)}", flush=True)
                    out.append({"start": start + float(s.start), "end": at,
                                "text": (s.text or "").strip()})
                break
            except Exception as e:
                if device != "cuda":
                    raise
                from .. import accel
                accel.mark_cuda_failed()
                print(f"[transcribe/local] the GPU stopped partway "
                      f"({str(e).splitlines()[0][:120]}) - finishing on the CPU",
                      flush=True)
                device, compute_type, model = "cpu", "int8", None
        if (not state.get("language") and not kwargs.get("language")
                and float(getattr(info, "language_probability", 0) or 0) >= 0.8):
            state["language"] = getattr(info, "language", None)
        done[str(i)] = out
        try:
            partial.write_text(json.dumps(state), encoding="utf-8")
        except OSError:
            pass

    segments = [seg for i in range(count) for seg in done.get(str(i), [])]
    return {"duration": total, "segments": segments}


def transcribe_local(media_path: str, language: Optional[str] = None,
                     initial_prompt: Optional[str] = None) -> Dict:
    """Run faster-whisper on a local file path, caching the result as .srt."""
    cache_path = _find_cached_transcript(media_path)
    if cache_path is not None:
        source_mtime = os.path.getmtime(media_path)
        cache_mtime = cache_path.stat().st_mtime
        if cache_mtime >= source_mtime:
            print(f"[transcribe/local] reusing cached transcript: {cache_path}", flush=True)
            cached = _load_srt_cache(cache_path)
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
        _register_cuda_dlls()
        import faster_whisper  # type: ignore
        WhisperModel = faster_whisper.WhisperModel
    except ImportError as e:
        raise RuntimeError(
            "faster-whisper is required for --mode local. Install it with:\n"
            "    pip install -r requirements-local.txt"
        ) from e

    device = _resolve_device()
    model_name = current_whisper_model(device)
    compute_type = "int8_float16" if device == "cuda" else "int8"
    print(f"[transcribe/local] faster-whisper model={model_name} device={device} ({compute_type})", flush=True)

    from ..config import LOCAL_WHISPER_VAD_FILTER, LOCAL_WHISPER_VAD_PARAMETERS

    transcribe_kwargs: Dict = {
        "audio": media_path,
        "language": language,
        "beam_size": 5 if device == "cuda" else 3,
        "condition_on_previous_text": False,
    }
    prompt_clean = (initial_prompt or "").strip()[:220]
    if prompt_clean:
        transcribe_kwargs["initial_prompt"] = prompt_clean
    if LOCAL_WHISPER_VAD_FILTER:
        transcribe_kwargs["vad_filter"] = True
        transcribe_kwargs["vad_parameters"] = LOCAL_WHISPER_VAD_PARAMETERS
    else:
        transcribe_kwargs["vad_filter"] = False

    total_seconds = _source_seconds(media_path)
    if total_seconds > SLICE_SECONDS * 1.5:
        transcript = _transcribe_in_slices(media_path, total_seconds, transcribe_kwargs,
                                           device, compute_type, model_name)
        print(f"[transcribe/local] {len(transcript['segments'])} segments, "
              f"{total_seconds:.0f}s of audio", flush=True)
        cache_path = _write_srt_cache(media_path, transcript)
        print(f"[transcribe/local] wrote cache: {cache_path}", flush=True)
        try:
            _partial_path(media_path).unlink(missing_ok=True)
        except OSError:
            pass
        return transcript

    def _run(dev: str, ct: str):
        m_name = model_name if dev == device else current_whisper_model(dev)
        model = WhisperModel(m_name, device=dev, compute_type=ct)
        kw = dict(transcribe_kwargs)
        kw["beam_size"] = 5 if dev == "cuda" else 3
        segments_iter, info = _transcribe_audio_with_pipeline(
            faster_whisper, model, media_path, kw, dev
        )
        total = float(getattr(info, "duration", 0.0) or 0.0)
        last_said = 0.0
        out = []
        for s in segments_iter:
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
