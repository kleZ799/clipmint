"""One entry point for every layout.

The repo grew two renderers with different shapes — the face-tracking crop in
local/clipper.py and the stacked webcam layout in local/gaming_layout.py — plus
a plain centre crop that had no home. This dispatches a LayoutSpec to whichever
one it asks for, so callers (CLI, web UI) never branch on layout themselves.
"""
import os
import subprocess
from typing import Callable, Dict, List, Optional

from . import accel, proc
from .layout_spec import LayoutSpec

# Loudness target for every export. -14 LUFS is what YouTube, TikTok and
# Instagram all normalise toward, so hitting it means nothing is turned down
# on upload and nothing plays quiet next to whatever the feed shows first.
LOUDNESS_FILTER = "loudnorm=I=-14:TP=-1.5:LRA=11"


def _render_center_clip(source_path: str, start: float, end: float, out_path: str,
                        out_w: int, out_h: int) -> Dict:
    """Widest centre crop at the target ratio, in one ffmpeg pass.

    No face detection at all — for footage where the webcam is irrelevant or
    the user explicitly asked for gameplay only.
    """
    from .local.gaming_layout import _probe_dimensions

    src_w, src_h = _probe_dimensions(source_path)
    target = out_w / out_h

    if target < src_w / src_h:
        crop_h = src_h
        crop_w = int(crop_h * target)
    else:
        crop_w = src_w
        crop_h = int(crop_w / target)
    crop_w = min(crop_w - (crop_w % 2), src_w)
    crop_h = min(crop_h - (crop_h % 2), src_h)
    x = ((src_w - crop_w) // 2) & ~1
    y = ((src_h - crop_h) // 2) & ~1

    filt = (
        f"[0:v]crop={crop_w}:{crop_h}:{x}:{y},"
        f"scale={out_w}:{out_h}:flags=lanczos,setsar=1[v]"
    )
    accel.run_encode(lambda enc: [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-i", source_path, "-t", f"{end - start:.3f}",
        "-filter_complex", filt,
        "-map", "[v]", "-map", "0:a:0?",
        # Every Short lands in a feed between two videos mastered by someone
        # else. A clip that plays quieter than what came before it reads as
        # lower production value before a word of it is heard, so the output
        # is normalised to the same target the platforms mix to.
        "-af", LOUDNESS_FILTER,
        *enc,
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
        out_path,
    ], what="ffmpeg (centre crop render)", crf=23)
    return {"crop": {"x": x, "y": y, "w": crop_w, "h": crop_h}}


def _render_center_highlights(source_path: str, highlights: List[Dict], out_dir: str,
                              out_w: int, out_h: int, name_prefix: str = "short") -> List[Dict]:
    os.makedirs(out_dir, exist_ok=True)
    results: List[Dict] = []
    for i, h in enumerate(highlights, 1):
        out_path = os.path.join(out_dir, f"{name_prefix}_{i:02d}.mp4")
        print(f"[center] {i}/{len(highlights)}: {h.get('title', '(untitled)')}", flush=True)
        try:
            info = _render_center_clip(
                source_path, float(h["start_time"]), float(h["end_time"]),
                out_path, out_w, out_h,
            )
            results.append({**h, "clip_url": out_path, "layout": info})
        except Exception as e:
            print(f"[center] {i} failed: {e}", flush=True)
            results.append({**h, "clip_url": None, "error": str(e)})
    return results


def render_highlights(
    source_path: str,
    highlights: List[Dict],
    spec: LayoutSpec,
    out_dir: Optional[str] = None,
    name_prefix: str = "short",
    language: Optional[str] = None,
    kind: Optional[str] = None,
    llm_fn: Optional[Callable[[str], str]] = None,
) -> List[Dict]:
    """Render `highlights` from `source_path` according to `spec`.

    Returns the highlights with `clip_url` set (or `error` on failure) —
    the same shape every renderer in this repo already produces.

    `language` is the spoken language, for the captions; `kind` is what sort
    of video this turned out to be, which decides how hard pauses are cut;
    `llm_fn` is only used to place B-roll, and B-roll is skipped without it.
    """
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

    results = _render_by_layout(source_path, highlights, spec, out_dir,
                                out_w, out_h, name_prefix)
    _edit(results, spec, language, kind, llm_fn)
    if spec.hook_replay:
        _add_hook_openings(results)
    return results


def _edit(results: List[Dict], spec: LayoutSpec, language: Optional[str],
          kind: Optional[str], llm_fn) -> None:
    """Captions, jump cuts, punch-ins, emoji and B-roll -- see autoedit.

    After the layout, so all three renderers get it from one place, and before
    the cold open, so the replayed moment carries its captions with it.
    """
    from .autoedit import options_from_spec, polish

    opts = options_from_spec(spec, kind)
    if not opts.anything:
        return
    polish(results, opts, language=language, llm_fn=llm_fn)


def _add_hook_openings(results: List[Dict]) -> None:
    """Give each rendered clip a cold open on its own loudest moment.

    Done after rendering rather than inside each renderer, so all three
    layouts get it from one place and none of them has to know it exists. It
    no-ops on any clip whose hook is already at the front.
    """
    from .hook_open import apply

    for r in results:
        path = r.get("clip_url")
        if not path or not os.path.exists(path):
            continue
        target = r
        # Cutting pauses moved the peak earlier in the clip. The cold open
        # has to point at where it is now, not where it was in the source.
        offset = r.pop("hook_peak_offset", None)
        if offset is not None and r.get("start_time") is not None:
            target = {**r, "hook_peak": float(r["start_time"]) + float(offset)}
        added = apply(path, target)
        if added:
            r["hook_replay_seconds"] = round(added, 2)


def _render_by_layout(source_path: str, highlights: List[Dict], spec: LayoutSpec,
                      out_dir: str, out_w: int, out_h: int,
                      name_prefix: str) -> List[Dict]:
    if spec.layout == "stacked":
        from .local.gaming_layout import render_stacked_highlights
        return render_stacked_highlights(
            source_path, highlights, out_dir=out_dir,
            corner=spec.webcam_corner,
            out_w=out_w, out_h=out_h,
            cam_panel_fraction=spec.cam_panel_fraction,
            face_context_multiple=spec.face_zoom,
            name_prefix=name_prefix,
        )

    if spec.layout == "facetrack":
        from .local.clipper import crop_highlights_local
        return crop_highlights_local(
            source_path, highlights,
            aspect_ratio=spec.aspect_ratio,
            out_dir=out_dir,
            target_resolution=f"{out_w}x{out_h}",
            name_prefix=name_prefix,
        )

    return _render_center_highlights(
        source_path, highlights, out_dir, out_w, out_h, name_prefix=name_prefix
    )
