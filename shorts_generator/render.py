"""One entry point for every layout.

The repo grew two renderers with different shapes — the face-tracking crop in
local/clipper.py and the stacked webcam layout in local/gaming_layout.py — plus
a plain centre crop that had no home. This dispatches a LayoutSpec to whichever
one it asks for, so callers (CLI, web UI) never branch on layout themselves.
"""
import json
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional

from . import accel, proc
from .layout_spec import LayoutSpec

# Loudness target for every export. -14 LUFS is what YouTube, TikTok and
# Instagram all normalise toward, so hitting it means nothing is turned down
# on upload and nothing plays quiet next to whatever the feed shows first.
LOUDNESS_FILTER = "loudnorm=I=-14:TP=-1.5:LRA=11"
VOICE_POLISH_FILTER = "highpass=f=75"


def measure_loudnorm_filter(path: str, start: Optional[float] = None,
                            duration: Optional[float] = None) -> str:
    """2-pass linear EBU R128 loudness filter + subtle rumble filter.

    Single-pass `loudnorm` compresses dynamics noticeably on shouts/pauses.
    Measuring integrated LUFS + true peak in a fast null pass (~0.3s) enables
    transparent `linear=true` mastering, falling back cleanly on silence/error.
    """
    fallback = f"{VOICE_POLISH_FILTER},{LOUDNESS_FILTER}"
    cmd = ["ffmpeg", "-hide_banner", "-v", "info"]
    if start is not None and start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    if duration is not None and duration > 0:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-i", path, "-vn", "-af", f"{VOICE_POLISH_FILTER},{LOUDNESS_FILTER}:print_format=json",
            "-f", "null", "-"]
    try:
        res = proc.run(cmd, capture_output=True, text=True, timeout=120)
        stderr = res.stderr or ""
        matches = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr, re.DOTALL)
        if not matches:
            return fallback
        stats = json.loads(matches[-1])
        vals = {}
        for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset"):
            v = float(stats[k])
            if not math.isfinite(v):
                return fallback
            vals[k] = v
        return (
            f"{VOICE_POLISH_FILTER},"
            f"loudnorm=I=-14:TP=-1.5:LRA=11:"
            f"measured_I={vals['input_i']:.2f}:"
            f"measured_TP={vals['input_tp']:.2f}:"
            f"measured_LRA={vals['input_lra']:.2f}:"
            f"measured_thresh={vals['input_thresh']:.2f}:"
            f"offset={vals['target_offset']:.2f}:"
            f"linear=true"
        )
    except Exception:
        return fallback


def _render_center_clip(source_path: str, start: float, end: float, out_path: str,
                        out_w: int, out_h: int, frame: Optional[Dict] = None,
                        is_intermediate: bool = False) -> Dict:
    """Widest centre crop at the target ratio, in one ffmpeg pass."""
    from . import framing
    from .local.gaming_layout import _probe_dimensions

    src_w, src_h = _probe_dimensions(source_path)
    crop_w, crop_h = framing.picture_window(src_w, src_h, out_w, out_h)
    x, y = framing.place(frame, src_w, src_h, crop_w, crop_h,
                         ((src_w - crop_w) // 2, (src_h - crop_h) // 2))

    filt = (
        f"[0:v]crop={crop_w}:{crop_h}:{x}:{y},"
        f"scale={out_w}:{out_h}:flags=lanczos,setsar=1[v]"
    )
    dur = max(0.1, end - start)
    if is_intermediate:
        audio_args = ["-c:a", "aac", "-b:a", "256k"]
    else:
        af = measure_loudnorm_filter(source_path, start=start, duration=dur)
        audio_args = ["-af", af, "-c:a", "aac", "-b:a", "192k"]

    accel.run_encode(lambda enc: [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-i", source_path, "-t", f"{dur:.3f}",
        "-filter_complex", filt,
        "-map", "[v]", "-map", "0:a:0?",
        *enc,
        *audio_args,
        "-movflags", "+faststart",
        out_path,
    ], what="ffmpeg (centre crop render)", crf=19, intermediate=is_intermediate)
    return {"crop": {"x": x, "y": y, "w": crop_w, "h": crop_h},
            "panel": framing.PICTURE, "manual": bool(framing.normalise(frame)),
            "x": framing.fraction(x, src_w, crop_w), "y": framing.fraction(y, src_h, crop_h)}


def _render_center_highlights(source_path: str, highlights: List[Dict], out_dir: str,
                              out_w: int, out_h: int, name_prefix: str = "short",
                              is_intermediate: bool = False) -> List[Dict]:
    os.makedirs(out_dir, exist_ok=True)

    def _one(item):
        i, h = item
        out_path = os.path.join(out_dir, f"{name_prefix}_{i:02d}.mp4")
        print(f"[center] {i}/{len(highlights)}: {h.get('title', '(untitled)')}", flush=True)
        try:
            info = _render_center_clip(
                source_path, float(h["start_time"]), float(h["end_time"]),
                out_path, out_w, out_h, frame=h.get("frame"),
                is_intermediate=is_intermediate,
            )
            return {**h, "clip_url": out_path, "layout": info}
        except Exception as e:
            print(f"[center] {i} failed: {e}", flush=True)
            return {**h, "clip_url": None, "error": str(e)}

    indexed = list(enumerate(highlights, 1))
    if len(indexed) > 1:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="center") as pool:
            return list(pool.map(_one, indexed))
    return [_one(indexed[0])] if indexed else []


def render_highlights(
    source_path: str,
    highlights: List[Dict],
    spec: LayoutSpec,
    out_dir: Optional[str] = None,
    name_prefix: str = "short",
    language: Optional[str] = None,
    kind: Optional[str] = None,
    llm_fn: Optional[Callable[[str], str]] = None,
    initial_prompt: Optional[str] = None,
) -> List[Dict]:
    """Render `highlights` from `source_path` according to `spec`."""
    from .autoedit import options_from_spec
    from .config import LOCAL_OUTPUT_DIR
    from .layout_spec import pick_output_size
    from .local.gaming_layout import _probe_dimensions

    out_dir = out_dir or LOCAL_OUTPUT_DIR
    os.makedirs(out_dir, exist_ok=True)

    out_w, out_h = spec.width, spec.height
    if spec.match_source_quality:
        try:
            src_w, src_h = _probe_dimensions(source_path)
            out_w, out_h = pick_output_size(src_w, src_h, spec.aspect_ratio, spec.layout)
            if (out_w, out_h) != (spec.width, spec.height):
                print(f"[render] source is {src_w}x{src_h} — rendering at "
                      f"{out_w}x{out_h} instead of {spec.width}x{spec.height}", flush=True)
        except Exception as e:
            print(f"[render] could not probe source ({e}); using {out_w}x{out_h}", flush=True)

    print(f"[render] {spec.layout} · {spec.describe()} · {out_w}x{out_h}", flush=True)

    opts = options_from_spec(spec, kind)
    will_edit = opts.anything
    results = _render_by_layout(source_path, highlights, spec, out_dir,
                                out_w, out_h, name_prefix, is_intermediate=will_edit)
    if will_edit:
        _edit(results, opts, language, llm_fn,
              initial_prompt=initial_prompt, from_intermediate=will_edit)
    if spec.hook_replay:
        _add_hook_openings(results)
    return results


def _edit(results: List[Dict], opts, language: Optional[str],
          llm_fn, initial_prompt: Optional[str] = None,
          from_intermediate: bool = False) -> None:
    """Captions, jump cuts, punch-ins, emoji, B-roll and merged cold open."""
    from .autoedit import polish
    polish(results, opts, language=language, llm_fn=llm_fn,
           initial_prompt=initial_prompt, from_intermediate=from_intermediate)


def _add_hook_openings(results: List[Dict]) -> None:
    """Fallback standalone cold open for clips that did not run through autoedit."""
    from .hook_open import apply

    for r in results:
        if r.pop("_hook_applied_in_edit", False):
            continue
        path = r.get("clip_url")
        if not path or not os.path.exists(path):
            continue
        target = r
        offset = r.pop("hook_peak_offset", None)
        if offset is not None and r.get("start_time") is not None:
            target = {**r, "hook_peak": float(r["start_time"]) + float(offset)}
        added = apply(path, target)
        if added:
            r["hook_replay_seconds"] = round(added, 2)


def _render_by_layout(source_path: str, highlights: List[Dict], spec: LayoutSpec,
                      out_dir: str, out_w: int, out_h: int,
                      name_prefix: str, is_intermediate: bool = False) -> List[Dict]:
    if spec.layout == "stacked":
        from .local.gaming_layout import render_stacked_highlights
        return render_stacked_highlights(
            source_path, highlights, out_dir=out_dir,
            corner=spec.webcam_corner,
            out_w=out_w, out_h=out_h,
            cam_panel_fraction=spec.cam_panel_fraction,
            face_context_multiple=spec.face_zoom,
            name_prefix=name_prefix,
            is_intermediate=is_intermediate,
        )

    if spec.layout == "facetrack":
        from .local.clipper import crop_highlights_local
        return crop_highlights_local(
            source_path, highlights,
            aspect_ratio=spec.aspect_ratio,
            out_dir=out_dir,
            target_resolution=f"{out_w}x{out_h}",
            name_prefix=name_prefix,
            is_intermediate=is_intermediate,
        )

    return _render_center_highlights(
        source_path, highlights, out_dir, out_w, out_h,
        name_prefix=name_prefix, is_intermediate=is_intermediate,
    )
