"""Word-level timing for a rendered clip.

The source transcript is made once, for ranking, and it is made in sentences:
that is all the ranker needs, and asking faster-whisper for word timings over a
four-hour VOD costs time nobody gets back. Captions and jump cuts need the
opposite -- every word, with where it starts and stops -- but only for the
minute or so that actually became a clip.

So each rendered clip is listened to again on its own, with word timestamps
on. That is a Whisper pass over thirty to ninety seconds rather than hours,
and it has one property the source transcript could never have: its times
are the clip's own. A clip that was trimmed, re-cut, or has a cold open in
front of it is transcribed as it is, not as a span of something else that has
to be mapped back.

The model is loaded once and kept for the batch -- loading it is most of the
cost of a short clip -- and let go with release() when the render is done, so
it does not sit in memory between runs.
"""
from __future__ import annotations

import threading
from typing import Dict, List, Optional

from . import proc

_lock = threading.Lock()
_model = None
_model_device: Optional[str] = None

# Whisper's own line for "this segment was not speech". A clip of gameplay
# with nobody talking still comes back with words if nothing filters them,
# and captions of hallucinated words are worse than no captions.
_NO_SPEECH = 0.6
_LOW_LOGPROB = -1.0


def _load(device: str):
    """The Whisper model on `device`, loaded once for the whole batch."""
    global _model, _model_device
    if _model is not None and _model_device == device:
        return _model

    from .config import LOCAL_WHISPER_MODEL
    from .local.transcriber import _register_cuda_dlls
    _register_cuda_dlls()
    from faster_whisper import WhisperModel  # type: ignore

    compute_type = "float16" if device == "cuda" else "int8"
    _model = WhisperModel(LOCAL_WHISPER_MODEL, device=device, compute_type=compute_type)
    _model_device = device
    return _model


def release() -> None:
    """Let go of the model. The next clip loads it again."""
    global _model, _model_device
    with _lock:
        _model = None
        _model_device = None


def _device() -> str:
    from . import accel
    from .local.transcriber import _register_cuda_dlls
    device, _ = accel.whisper_device(_register_cuda_dlls)
    return device


def _listen(path: str, language: Optional[str], device: str) -> List[Dict]:
    model = _load(device)
    segments, _info = model.transcribe(
        path,
        language=language,
        beam_size=5,
        word_timestamps=True,
        condition_on_previous_text=False,
    )
    words: List[Dict] = []
    for seg in segments:
        # Held between segments, the same way the source transcription is:
        # Pause cannot suspend a model running inside this process, but it
        # can stop it being asked for the next window.
        proc.wait_if_paused()
        if (float(getattr(seg, "no_speech_prob", 0.0) or 0.0) > _NO_SPEECH
                and float(getattr(seg, "avg_logprob", 0.0) or 0.0) < _LOW_LOGPROB):
            continue
        for w in getattr(seg, "words", None) or []:
            text = str(getattr(w, "word", "") or "").strip()
            if not text:
                continue
            start, end = float(w.start), float(w.end)
            if end <= start:
                end = start + 0.05
            words.append({"start": round(start, 3), "end": round(end, 3), "word": text,
                          "p": round(float(getattr(w, "probability", 1.0) or 0.0), 3)})
    return words


def transcribe_words(path: str, language: Optional[str] = None) -> List[Dict]:
    """Every spoken word in `path`: [{start, end, word, p}], in clip seconds.

    Returns [] when faster-whisper is not installed, when the clip has no
    speech, or when listening fails -- captions and cuts are garnish on a
    finished clip, and losing the clip over them would be the wrong trade.
    A GPU that fails partway is marked broken and the clip is done again on
    the CPU, the same way the source transcription behaves.
    """
    lang = (language or "").strip().lower() or None
    if lang == "auto":
        lang = None

    with _lock:
        try:
            device = _device()
        except Exception:
            device = "cpu"
        try:
            return _listen(path, lang, device)
        except ImportError:
            print("[words] faster-whisper is not installed - no captions or cuts",
                  flush=True)
            return []
        except Exception as e:
            if device != "cuda":
                print(f"[words] could not listen to the clip ({str(e).splitlines()[0][:140]})",
                      flush=True)
                return []
            from . import accel
            accel.mark_cuda_failed()
            print(f"[words] the GPU stopped ({str(e).splitlines()[0][:120]}) - "
                  f"listening on the CPU instead", flush=True)
        try:
            return _listen(path, lang, "cpu")
        except Exception as e:
            print(f"[words] could not listen to the clip ({str(e).splitlines()[0][:140]})",
                  flush=True)
            return []
