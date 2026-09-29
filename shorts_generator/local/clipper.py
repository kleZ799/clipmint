"""Local clipping: ffmpeg subclip + face-following vertical crop.

Two stages per highlight:
  1. Cut the source video to [start, end] with ffmpeg (re-encoded, audio kept).
  2. Reframe the cut to the target aspect ratio, with a crop window that
     follows the speaker's face the way a camera operator would.

The second stage used to chase a Haar detection on every frame, easing 15% of
the way toward wherever the newest detection landed. That looks smooth on
paper and stutters on screen: Haar boxes wobble by several pixels between
identical frames, a false positive in the background yanks the window for a
few frames, and a crop that is always moving a little reads as a shaky camera.

So the path is now planned before a single frame is rendered:

  detect    faces sampled ~8 times a second, following one person and ignoring
            a face that appears for a frame or two -- or, with two people in
            shot together, whoever is talking (see speaker.py)
  clean     gaps held, spikes removed with a median filter
  operate   the window does not move while the face stays inside a dead zone;
            when it leaves, the window re-centres on it
  ease      the re-centres are smoothed with a zero-lag Gaussian, so every
            move eases in and out and nothing trails behind the face
  render    one ffmpeg pass, the crop driven frame by frame from the plan

A hard cut in the source (the camera switching to the other podcast host) is
kept as a cut rather than swept across as a pan, which is what an editor would
do too.
"""
import os
import shutil
import tempfile
import time
from typing import Dict, List, Optional, Tuple

from .. import accel, framing, proc
from ..config import LOCAL_OUTPUT_DIR, LOCAL_OUTPUT_RESOLUTION
from ..faces import detect_with_mouths as face_detect
from ..render import LOUDNESS_FILTER
from . import speaker

# How often faces are looked for. The plan is interpolated between samples,
# so detecting on every frame buys nothing but time.
DETECT_PER_SECOND = 8
# Detection runs on a copy this wide; faces in a full-frame camera are large.
DETECT_WIDTH = 640
# How far the face may drift from the window's centre, as a fraction of the
# window, before the window moves. The larger this is, the stiller the shot.
DEAD_ZONE = 0.12
# How long a re-centre takes to ease in and out, as the Gaussian's sigma.
EASE_SECONDS = 0.45
# A jump bigger than this fraction of the window between two samples is a cut
# in the source, not movement, and is kept as a cut.
CUT_JUMP = 0.55
# Samples a newly seen face must persist for before the window follows it.
CONFIRM_SAMPLES = 3
# Where the face sits vertically when the window can move vertically at all.
FACE_VERTICAL_ANCHOR = 0.40


def _safe_remove(path: str, attempts: int = 5) -> None:
    """Delete a temp file, tolerating Windows' lazy handle release.

    Cleanup must never raise: on Windows a lingering handle turns a real
    encoding error into a confusing WinError 32 from the finally block.
    """
    for i in range(attempts):
        try:
            if os.path.exists(path):
                os.remove(path)
            return
        except OSError:
            if i == attempts - 1:
                print(f"[clip/local] warning: could not delete temp file {path}", flush=True)
                return
            time.sleep(0.3)


def _ratio(aspect_ratio: str) -> float:
    """Parse '9:16' → 9/16, '1:1' → 1.0."""
    try:
        w, h = aspect_ratio.split(":")
        return float(w) / float(h)
    except (ValueError, ZeroDivisionError):
        return 9.0 / 16.0


def _cut_subclip(source_path: str, start: float, end: float, out_path: str) -> str:
    """ffmpeg -ss start -to end → re-encoded mp4 with audio."""
    # Intermediate only — the reframe step re-encodes this, so favour speed
    # at near-transparent quality instead of spending time on compression.
    #
    # -ss before -i, and a duration rather than an end time. After -i it is an
    # output seek: ffmpeg decodes the source from the very beginning and
    # throws frames away until it reaches the start -- on a clip 28 minutes
    # into a stream, that was most of a minute spent on nothing, on every
    # face-follow clip. Before -i it seeks the input, and a re-encode still
    # starts on the exact frame.
    accel.run_encode(lambda enc: [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-i", source_path,
        "-t", f"{max(0.1, end - start):.3f}",
        *enc,
        "-c:a", "aac", "-b:a", "128k",
        out_path,
    ], what="ffmpeg (cut subclip)", crf=18, preset="ultrafast")
    return out_path


def _crop_size(src_w: int, src_h: int, target_ratio: float) -> Tuple[int, int]:
    """The largest window at the target ratio that fits inside the frame."""
    if target_ratio < src_w / src_h:
        crop_h = src_h
        crop_w = int(crop_h * target_ratio)
    else:
        crop_w = src_w
        crop_h = int(crop_w / target_ratio)
    return max(2, crop_w - (crop_w % 2)), max(2, crop_h - (crop_h % 2))


def track_faces(path: str) -> Tuple[List[float], List[Optional[Tuple[float, float, float]]],
                                    float, int, int, int]:
    """Sample the clip and follow one face through it.

    Returns (sample times, one (cx, cy, width) or None per sample, fps,
    frame count, width, height), all in source pixels.
    """
    import cv2  # type: ignore

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError(f"could not open {path}")
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    step = max(1, int(round(fps / DETECT_PER_SECOND)))
    scale = DETECT_WIDTH / src_w if src_w > DETECT_WIDTH else 1.0

    times: List[float] = []
    track: List[Optional[Tuple[float, float, float]]] = []
    seen: List[List[speaker.Sighting]] = []
    last: Optional[Tuple[float, float, float]] = None
    pending: List[Tuple[float, float, float]] = []
    far = 0.25 * src_w
    index = 0
    # The frame just before each sample. A mouth is measured across that one
    # frame gap, where the jaw has moved and the head has barely started to:
    # see local/speaker.py. It is already decoded by grab(); keeping it costs
    # a colour conversion.
    before = None
    try:
        while True:
            if not cap.grab():
                break
            if index % step:
                if index % step == step - 1:
                    ok, before = cap.retrieve()
                    before = before if ok else None
                index += 1
                continue
            ok, frame = cap.retrieve()
            index += 1
            if not ok:
                break
            # This pass runs in-process, where Pause cannot suspend it the way
            # it suspends ffmpeg; holding here between frames is how it stops.
            proc.wait_if_paused()
            small = (cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                     if scale < 1.0 else frame)
            found = face_detect(small, min_fraction=0.04)
            faces = [(cx / scale, cy / scale, fw / scale) for (cx, cy, fw, _), _ in found]
            times.append((index - 1) / fps)
            # Every face, with the pixels around its mouth, for choosing
            # between people once the whole clip has been seen.
            seen.append([
                speaker.sighting(frame, before, cx, cy, fw,
                                 (mouth[0] / scale, mouth[1] / scale) if mouth else None)
                for (cx, cy, fw), (_, mouth) in zip(faces, found)])
            # With a step of one, the previous sample is the frame before.
            before = frame if step == 1 else None

            if not faces:
                track.append(None)
                continue
            if last is None:
                last = max(faces, key=lambda f: f[2])
                track.append(last)
                continue

            # Stay on the person already being followed. A face somewhere else
            # has to hold for a few samples before the window goes to it, so
            # a poster on the wall or a face in a game does not steal the shot.
            near = min(faces, key=lambda f: abs(f[0] - last[0]) + abs(f[1] - last[1]))
            if abs(near[0] - last[0]) + abs(near[1] - last[1]) <= far:
                last, pending = near, []
                track.append(near)
                continue
            biggest = max(faces, key=lambda f: f[2])
            if pending and abs(biggest[0] - pending[-1][0]) > far:
                pending = []
            pending.append(biggest)
            if len(pending) >= CONFIRM_SAMPLES:
                last, pending = biggest, []
                track.append(biggest)
            else:
                track.append(None)
    finally:
        frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or index
        cap.release()

    # Two people in the frame together: follow whoever is talking, not the
    # biggest face. With one person, or people only ever shown one at a time,
    # this declines and the single-face track above stands.
    try:
        chosen = speaker.follow_speaker(seen, fps / step, src_w)
    except Exception as e:  # noqa: BLE001 - a choice of person must never sink a render
        print(f"[clip/local] could not tell who was talking ({e}) - "
              f"following the main face", flush=True)
        chosen = None
    if chosen is not None:
        track, info = chosen
        print(f"[clip/local] {info['people']} people in shot together for "
              f"{info['together']:.0%} of the clip - following whoever is talking, "
              f"{info['shots']} shot(s)", flush=True)
    return times, track, fps, max(frames, index), src_w, src_h


def _gaussian(values, sigma: float):
    """Zero-lag Gaussian smoothing, edges held rather than faded to zero."""
    import numpy as np
    if sigma <= 0 or len(values) < 3:
        return np.asarray(values, float)
    radius = int(3 * sigma) + 1
    k = np.exp(-0.5 * (np.arange(-radius, radius + 1) / sigma) ** 2)
    k /= k.sum()
    padded = np.pad(np.asarray(values, float), radius, mode="edge")
    return np.convolve(padded, k, mode="valid")


def _operate(values, dead_zone: float, cut_jump: float):
    """Hold the window still until the target leaves the dead zone.

    Returns the held positions and the sample indices where the source cut.
    """
    held = [float(values[0])]
    cuts = []
    for i in range(1, len(values)):
        v, cur = float(values[i]), held[-1]
        if abs(v - float(values[i - 1])) > cut_jump:
            cuts.append(i)
            held.append(v)
        elif abs(v - cur) > dead_zone:
            held.append(v)
        else:
            held.append(cur)
    return held, cuts


def plan_path(times: List[float], track: List[Optional[Tuple[float, float, float]]],
              fps: float, frames: int, src_w: int, src_h: int,
              crop_w: int, crop_h: int) -> List[Tuple[int, int]]:
    """The window's top-left corner for every frame of the clip."""
    import numpy as np

    centre = ((src_w - crop_w) // 2, (src_h - crop_h) // 2)
    known = [i for i, t in enumerate(track) if t is not None]
    if not known or not times:
        return [centre] * frames

    # Hold through gaps: the last place the face was is the best guess for
    # where it is while the detector blinks.
    xs, ys = np.empty(len(track)), np.empty(len(track))
    first = track[known[0]]
    prev = first
    for i, t in enumerate(track):
        prev = t if t is not None else prev
        xs[i], ys[i] = prev[0], prev[1]

    # A median over ~0.6s removes single-sample spikes without delaying moves.
    win = max(1, int(round(0.6 * DETECT_PER_SECOND)) | 1)
    if len(xs) >= win:
        pad = win // 2
        xs = np.array([np.median(w) for w in np.lib.stride_tricks.sliding_window_view(
            np.pad(xs, pad, mode="edge"), win)])
        ys = np.array([np.median(w) for w in np.lib.stride_tricks.sliding_window_view(
            np.pad(ys, pad, mode="edge"), win)])

    # Window positions wanted, then the operator pass, then the ease.
    want_x = xs - crop_w / 2
    want_y = ys - crop_h * FACE_VERTICAL_ANCHOR
    sigma = EASE_SECONDS * DETECT_PER_SECOND
    paths = []
    for want, span, limit in ((want_x, crop_w, src_w - crop_w),
                              (want_y, crop_h, src_h - crop_h)):
        want = np.clip(want, 0, max(0, limit))
        held, cuts = _operate(want, DEAD_ZONE * span, CUT_JUMP * span)
        held = np.asarray(held)
        # Smooth each stretch between cuts on its own, so a cut stays a cut.
        smooth = np.empty_like(held)
        bounds = [0] + cuts + [len(held)]
        for a, b in zip(bounds, bounds[1:]):
            smooth[a:b] = _gaussian(held[a:b], sigma)
        paths.append(np.clip(smooth, 0, max(0, limit)))

    t = np.asarray(times)
    frame_t = np.arange(frames) / fps
    # Interpolating across a cut would sweep through it, so each frame takes
    # the value of its stretch rather than blending two.
    px = np.interp(frame_t, t, paths[0])
    py = np.interp(frame_t, t, paths[1])
    for cut in [i for i in range(1, len(paths[0]))
                if abs(paths[0][i] - paths[0][i - 1]) > CUT_JUMP * crop_w * 0.5
                or abs(paths[1][i] - paths[1][i - 1]) > CUT_JUMP * crop_h * 0.5]:
        lo, hi = t[cut - 1], t[cut]
        mid = (lo + hi) / 2
        span = (frame_t > lo) & (frame_t < hi)
        px[span] = np.where(frame_t[span] < mid, paths[0][cut - 1], paths[0][cut])
        py[span] = np.where(frame_t[span] < mid, paths[1][cut - 1], paths[1][cut])

    return [(int(round(x)) & ~1, int(round(y)) & ~1) for x, y in zip(px, py)]


def _filter_path(path: str) -> str:
    """A file path as an ffmpeg filter option value, on any OS.

    The drive colon on Windows would otherwise end the option; this is the
    same escaping ffmpeg's own docs use for the subtitles filter.
    """
    return path.replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def _write_commands(plan: List[Tuple[int, int]], fps: float, out: str) -> None:
    """One sendcmd line per frame where the window actually moves.

    Each command is sent half a frame early. Sent on the frame's own time, a
    timestamp that rounds up by a fraction of a millisecond lands after the
    frame it was meant for, and that frame renders with the previous position.
    """
    with open(out, "w", encoding="utf-8") as fh:
        prev = plan[0] if plan else (0, 0)
        for i, (x, y) in enumerate(plan):
            if (x, y) == prev and i:
                continue
            fh.write(f"{max(0.0, (i - 0.5) / fps):.4f} crop@fx x {x}, crop@fx y {y};\n")
            prev = (x, y)


def _reframe_vertical(in_path: str, out_path: str, aspect_ratio: str,
                      target_resolution: Optional[str] = None,
                      frame: Optional[Dict] = None) -> Dict:
    """Crop the cut clip to the target aspect ratio, following the face.

    `frame` is a position someone chose by hand (framing.normalise): then
    nothing is tracked and the window stays exactly where they put it.
    Returns what framing.summary keeps on the clip.
    """
    try:
        import cv2  # type: ignore  # noqa: F401
        import numpy  # type: ignore  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "opencv-python is required for --mode local. Install it with:\n"
            "    pip install -r requirements-local.txt"
        ) from e

    frame = framing.normalise(frame)
    if frame:
        cap = cv2.VideoCapture(in_path)
        src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        cap.release()
        crop_w, crop_h = _crop_size(src_w, src_h, _ratio(aspect_ratio))
        fixed = framing.place(frame, src_w, src_h, crop_w, crop_h,
                              ((src_w - crop_w) // 2, (src_h - crop_h) // 2))
        plan = [fixed]
        print(f"[clip/local] framed by hand at {fixed[0]},{fixed[1]} - "
              f"not following faces", flush=True)
    else:
        times, track, fps, frames, src_w, src_h = track_faces(in_path)
        crop_w, crop_h = _crop_size(src_w, src_h, _ratio(aspect_ratio))
        plan = plan_path(times, track, fps, frames, src_w, src_h, crop_w, crop_h)
        seen = sum(1 for t in track if t is not None)
        moves = sum(1 for a, b in zip(plan, plan[1:]) if a != b)
        print(f"[clip/local] face in {seen}/{len(track)} samples; window moves on "
              f"{moves}/{max(1, len(plan))} frames", flush=True)

    resolution = target_resolution if target_resolution is not None else LOCAL_OUTPUT_RESOLUTION
    scale = ""
    if resolution:
        w, h = resolution.split("x")
        # lanczos preserves detail on the upscale from the native crop size
        scale = f",scale={int(w)}:{int(h)}:flags=lanczos"

    x0, y0 = plan[0] if plan else ((src_w - crop_w) // 2, (src_h - crop_h) // 2)
    work = tempfile.mkdtemp(prefix="facetrack_")
    try:
        cmds = os.path.join(work, "path.cmd")
        _write_commands(plan, fps, cmds)
        graph = (f"[0:v]sendcmd=f='{_filter_path(cmds)}',"
                 f"crop@fx=w={crop_w}:h={crop_h}:x={x0}:y={y0}{scale},setsar=1[v]")
        accel.run_encode(lambda enc: [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", in_path,
            "-filter_complex", graph,
            "-map", "[v]", "-map", "0:a:0?",
            "-af", LOUDNESS_FILTER,
            *enc,
            "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart",
            out_path,
        ], what="ffmpeg (face-tracked reframe)", crf=20)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    info: Dict = {"panel": framing.PICTURE}
    if frame:
        info.update(manual=True, x=framing.fraction(plan[0][0], src_w, crop_w),
                    y=framing.fraction(plan[0][1], src_h, crop_h))
    elif plan and all(p == plan[0] for p in plan):
        # A window that never moved has a place worth showing in the editor;
        # one that followed someone around does not have just one.
        info.update(x=framing.fraction(plan[0][0], src_w, crop_w),
                    y=framing.fraction(plan[0][1], src_h, crop_h))
    return info


def crop_clip_local(
    source_path: str,
    start_time: float,
    end_time: float,
    aspect_ratio: str,
    out_path: str,
    target_resolution: Optional[str] = None,
    frame: Optional[Dict] = None,
) -> Dict:
    """Cut + reframe one highlight into `out_path`; returns how it was framed."""
    cut_path = out_path + ".cut.mp4"
    try:
        _cut_subclip(source_path, start_time, end_time, cut_path)
        return _reframe_vertical(cut_path, out_path, aspect_ratio,
                                 target_resolution=target_resolution, frame=frame)
    finally:
        _safe_remove(cut_path)


def crop_highlights_local(
    source_path: str,
    highlights: List[Dict],
    aspect_ratio: str = "9:16",
    out_dir: Optional[str] = None,
    target_resolution: Optional[str] = None,
    name_prefix: str = "short",
) -> List[Dict]:
    out_dir = out_dir or LOCAL_OUTPUT_DIR
    os.makedirs(out_dir, exist_ok=True)
    results: List[Dict] = []
    for i, h in enumerate(highlights, 1):
        out_path = os.path.join(out_dir, f"{name_prefix}_{i:02d}.mp4")
        print(f"[clip/local] {i}/{len(highlights)}: {h.get('title', '(untitled)')}", flush=True)
        try:
            info = crop_clip_local(
                source_path,
                float(h["start_time"]),
                float(h["end_time"]),
                aspect_ratio,
                out_path,
                target_resolution=target_resolution,
                frame=h.get("frame"),
            )
            results.append({**h, "clip_url": out_path, "layout": info})
        except Exception as e:
            print(f"[clip/local] {i} failed: {e}", flush=True)
            results.append({**h, "clip_url": None, "error": str(e)})
    return results
