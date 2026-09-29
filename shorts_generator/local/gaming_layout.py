"""Stacked webcam-over-gameplay layout for game-stream VODs.

The stock face-tracking crop in clipper.py slides a vertical window across the
frame to keep a face centred. That works for talking-head and podcast footage,
where the speaker fills the frame. It fails on a game stream: the webcam is a
small corner overlay, so the window either misses it entirely or locks onto a
character's face in the game.

This module builds the layout gaming clips actually use:

    +----------------+
    |    webcam      |  top   — the overlay, framed on the streamer's face
    +----------------+
    |                |
    |   gameplay     |  bottom — centre crop of the game area
    |                |
    +----------------+

Finding the webcam is the whole job, and the obvious way to do it is wrong in
two ways that both showed up on real streams. Taking the largest face near the
corner locked onto a baby's photo inside the game. And sizing the crop as a
multiple of the face, with no idea where the overlay ends, filled the webcam
panel with gameplay and black letterbox bars.

Both are fixed by the one thing that tells an overlay from a game: it does not
move. Frames are sampled from several minutes around the clip, not just the
clip itself, so the game underneath changes completely while the overlay stays
put. Then:

  * the streamer is the face that keeps turning up in the same place across
    those samples -- a face in the game turns up once and is outvoted;
  * the overlay's border is an edge that is present in every sample and has
    changing content on the outside and a still room on the inside, which a
    door frame behind the streamer (still on both sides) does not;
  * the crop is fitted inside that border, at the panel's own aspect.

A face that fills a large part of the frame means there is no overlay at all
-- the camera IS the video (a podcast, a just-chatting segment) -- so that clip
is handed to the face-following renderer instead of being split in two.

Rendering is a single ffmpeg pass — no per-frame Python.
"""
import os
import statistics
from typing import Dict, List, Optional, Tuple

from .. import accel, framing, proc
from ..config import LOCAL_OUTPUT_DIR
from ..faces import detect as face_detect
from ..render import LOUDNESS_FILTER

# Fraction of the output height given to the webcam panel.
CAM_PANEL_FRACTION = 0.42

# How much of the webcam to show around the detected face, as a multiple of the
# face's width. ~5x gives a head-and-shoulders framing. The overlay's own
# border caps it, so a small webcam is shown whole rather than padded with game.
FACE_CONTEXT_MULTIPLE = 5.0

# Where the face sits vertically inside the cam panel (0 = top, 1 = bottom).
FACE_VERTICAL_ANCHOR = 0.42

# Frames sampled inside the clip, and from the minutes around it.
SAMPLE_COUNT = 6
CONTEXT_SAMPLES = 14
CONTEXT_SECONDS = 240

# Frames are analysed at this width. A webcam overlay is still a few hundred
# pixels wide here, and the edge maps cost a quarter of what 1080p would.
ANALYSIS_WIDTH = 960

# A face at least this wide, relative to the frame, is a full-frame camera.
FULL_FRAME_FACE = 0.10

# How far into the frame the overlay may reach from its corner.
CORNER_REACH = 0.62

# An overlay border must be at least this strong an edge (Sobel, averaged over
# the samples it persists in), with at least this much more movement outside
# it than inside.
EDGE_MIN = 10.0
CONTRAST_MIN = 6.0

# Pixels pulled in from a found border, so the border itself never shows.
BORDER_INSET = 3


def _probe_dimensions(source_path: str) -> Tuple[int, int]:
    out = proc.run_checked(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", source_path],
        what="ffprobe (source dimensions)", capture_stdout=True,
    ).stdout.strip()
    w, h = out.split("x")[:2]
    return int(w), int(h)


def _probe_duration(source_path: str) -> float:
    try:
        out = proc.run_checked(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", source_path],
            what="ffprobe (source duration)", capture_stdout=True,
        ).stdout
        return float((out or "0").strip())
    except Exception:
        return 0.0


def _grab(source_path: str, t: float):
    """One frame at `t`, scaled to ANALYSIS_WIDTH, as a BGR array (or None)."""
    import cv2
    import numpy as np
    try:
        data = proc.run(
            ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", source_path,
             "-frames:v", "1", "-vf", f"scale={ANALYSIS_WIDTH}:-2",
             "-f", "image2pipe", "-c:v", "bmp", "-"],
            capture_output=True,
        ).stdout
    except Exception:
        return None
    if not data:
        return None
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)


def _sample_times(start: float, end: float, duration: float) -> Tuple[List[float], List[float]]:
    """(times inside the clip, times in the minutes around it)."""
    step = (end - start) / (SAMPLE_COUNT + 1)
    inside = [start + step * i for i in range(1, SAMPLE_COUNT + 1)]
    lo = max(0.0, start - CONTEXT_SECONDS)
    hi = min(duration or end + CONTEXT_SECONDS, end + CONTEXT_SECONDS)
    if hi - lo <= (end - start) + 1:
        return inside, []
    wide = (hi - lo) / (CONTEXT_SAMPLES + 1)
    around = [lo + wide * i for i in range(1, CONTEXT_SAMPLES + 1)]
    return inside, [t for t in around if not (start <= t <= end)]


def _faces(img) -> List[Tuple[float, float, float]]:
    """(centre x, centre y, width) of every face in an analysis frame."""
    return [(cx, cy, fw) for cx, cy, fw, _ in face_detect(img, min_fraction=0.02)]


def _in_corner(cx: float, cy: float, w: int, h: int, corner: str) -> bool:
    ok_y = cy >= h * (1 - CORNER_REACH) if corner.startswith("bottom") else cy <= h * CORNER_REACH
    ok_x = cx <= w * CORNER_REACH if corner.endswith("left") else cx >= w * (1 - CORNER_REACH)
    return ok_x and ok_y


def _recurring_face(faces: List[Tuple[float, float, float]]) -> List[Tuple[float, float, float]]:
    """The detections that agree with each other most, as one cluster.

    Each detection counts how many others sit within a face-width of it; the
    best-supported one and its neighbours are the streamer. A face that is in
    the game turns up in one or two samples and loses the vote.
    """
    if not faces:
        return []
    def near(a, b):
        r = max(a[2], b[2])
        return abs(a[0] - b[0]) <= r and abs(a[1] - b[1]) <= r
    best = max(faces, key=lambda f: sum(1 for g in faces if near(f, g)))
    return [g for g in faces if near(best, g)]


def _border(profile_edge, inside, outside, start: int, stop: int, step: int) -> Optional[int]:
    """Walk outward from the face and return the index of the overlay border.

    `profile_edge[i]` is how strong a persistent edge sits at i; `inside[i]`
    and `outside[i]` are how much the picture changes just inside and just
    outside it. The border is the edge with the most change beyond it -- the
    game -- and the least before it -- the streamer's room.
    """
    best, best_score = None, 0.0
    for i in range(start, stop, step):
        contrast = outside[i] - inside[i]
        if profile_edge[i] < EDGE_MIN or contrast < CONTRAST_MIN:
            continue
        score = profile_edge[i] * contrast
        if score > best_score:
            best, best_score = i, score
    return best


def _overlay_bounds(frames, cx: float, cy: float, fw: float) -> Dict[str, Optional[int]]:
    """Left, top, right, bottom of the webcam overlay around (cx, cy).

    A side with no border found reports the frame edge when the search ran
    all the way to it (an overlay flush against the edge of the screen), and
    None when it did not -- the caller falls back to face-based framing there.
    """
    import cv2
    import numpy as np

    grays = np.stack([cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32) for f in frames])
    h, w = grays.shape[1:]
    gx = np.stack([np.abs(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)) for g in grays])
    gy = np.stack([np.abs(cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)) for g in grays])
    # An edge that is there in at least ~80% of the samples.
    px = np.percentile(gx, 20, axis=0)
    py = np.percentile(gy, 20, axis=0)
    # How much each pixel changes across the samples. Letterbox that is black
    # in every sample does not change either, but it is certainly outside.
    change = grays.std(axis=0) + 60.0 * (grays.max(axis=0) < 24)

    r = int(round(max(8.0, 0.9 * fw)))
    y0, y1 = max(0, int(cy) - r), min(h, int(cy) + r)
    x0, x1 = max(0, int(cx) - r), min(w, int(cx) + r)
    strip = 8

    def cols(edge_map):
        return edge_map[y0:y1].mean(axis=0)

    def rows(edge_map):
        return edge_map[:, x0:x1].mean(axis=1)

    col_edge = np.maximum.reduce([np.roll(cols(px), s) for s in (-1, 0, 1)])
    row_edge = np.maximum.reduce([np.roll(rows(py), s) for s in (-1, 0, 1)])
    col_change = np.median(change[y0:y1], axis=0)
    row_change = np.median(change[:, x0:x1], axis=1)

    def window_mean(v, a, b):
        a, b = max(0, a), min(len(v), b)
        return float(v[a:b].mean()) if b > a else 0.0

    n_c, n_r = len(col_change), len(row_change)
    left_in = [window_mean(col_change, i + 2, i + 2 + strip) for i in range(n_c)]
    left_out = [window_mean(col_change, i - 1 - strip, i - 1) for i in range(n_c)]
    right_in = [window_mean(col_change, i - 1 - strip, i - 1) for i in range(n_c)]
    right_out = [window_mean(col_change, i + 2, i + 2 + strip) for i in range(n_c)]
    top_in = [window_mean(row_change, i + 2, i + 2 + strip) for i in range(n_r)]
    top_out = [window_mean(row_change, i - 1 - strip, i - 1) for i in range(n_r)]
    bot_in = [window_mean(row_change, i - 1 - strip, i - 1) for i in range(n_r)]
    bot_out = [window_mean(row_change, i + 2, i + 2 + strip) for i in range(n_r)]

    near, far = int(0.7 * fw), int(9 * fw)
    out: Dict[str, Optional[int]] = {}
    for side, edge, ins, outs, a, b, stepdir, limit in (
        ("left", col_edge, left_in, left_out, int(cx) - near, int(cx) - far, -1, 0),
        ("right", col_edge, right_in, right_out, int(cx) + near, int(cx) + far, 1, w - 1),
        ("top", row_edge, top_in, top_out, int(cy) - near, int(cy) - far, -1, 0),
        ("bottom", row_edge, bot_in, bot_out, int(cy) + near, int(cy) + far, 1, h - 1),
    ):
        size = w if side in ("left", "right") else h
        a = max(strip + 2, min(size - strip - 3, a))
        stop = b
        reached_edge = (stop <= 0) if stepdir < 0 else (stop >= size - 1)
        stop = max(strip + 1, min(size - strip - 2, stop))
        found = _border(edge, ins, outs, a, stop, stepdir)
        if found is not None:
            out[side] = found
        else:
            out[side] = (0 if stepdir < 0 else size) if reached_edge else None
    out["w"], out["h"] = w, h
    return out


def locate_webcam(source_path: str, start: float, end: float,
                  corner: str = "bottom-left",
                  face_context_multiple: float = FACE_CONTEXT_MULTIPLE,
                  panel_aspect: float = 4 / 3) -> Optional[Dict]:
    """Find the webcam overlay and a crop of it, framed on the streamer.

    Returns None when no face recurs anywhere near the corner, and
    {"full_frame": True, ...} when the recurring face is too large to be an
    overlay -- the camera is the whole picture.
    """
    import cv2

    src_w, src_h = _probe_dimensions(source_path)
    inside, around = _sample_times(start, end, _probe_duration(source_path))
    frames, faces = [], []
    for t in inside + around:
        img = _grab(source_path, t)
        if img is None:
            continue
        frames.append(img)
        faces.extend(_faces(img))
    if not frames:
        return None
    an_h, an_w = frames[0].shape[:2]
    frames = [f for f in frames if f.shape[:2] == (an_h, an_w)]
    to_src = src_w / float(an_w)

    # A large face that keeps coming back, anywhere, is a full-frame camera.
    big = _recurring_face([f for f in faces if f[2] >= FULL_FRAME_FACE * an_w])
    if len(big) >= max(2, len(frames) // 3):
        return {"full_frame": True, "detections": len(big),
                "face_w": int(statistics.median(f[2] for f in big) * to_src)}

    cluster = _recurring_face([f for f in faces if _in_corner(f[0], f[1], an_w, an_h, corner)])
    if len(cluster) < 2:
        return None
    cx = statistics.median(f[0] for f in cluster)
    cy = statistics.median(f[1] for f in cluster)
    fw = statistics.median(f[2] for f in cluster)

    b = _overlay_bounds(frames, cx, cy, fw)
    want_w = fw * face_context_multiple
    left = b["left"] + BORDER_INSET if b["left"] is not None else cx - want_w / 2
    right = b["right"] - BORDER_INSET if b["right"] is not None else cx + want_w / 2
    want_h = want_w / panel_aspect
    top = b["top"] + BORDER_INSET if b["top"] is not None else cy - want_h * FACE_VERTICAL_ANCHOR
    bottom = (b["bottom"] - BORDER_INSET if b["bottom"] is not None
              else cy + want_h * (1 - FACE_VERTICAL_ANCHOR))
    left, top = max(0.0, left), max(0.0, top)
    right, bottom = min(float(an_w), right), min(float(an_h), bottom)

    # As much of the face-based framing as fits inside the overlay, at the
    # panel's aspect so nothing is stretched on the way into it.
    crop_w = min(want_w, right - left, (bottom - top) * panel_aspect)
    crop_h = crop_w / panel_aspect
    if crop_w < fw * 1.2:
        # The borders closed in tighter than a face -- they are not the
        # overlay. Frame on the face alone rather than crop into the head.
        crop_w, crop_h = want_w, want_w / panel_aspect
        left, top, right, bottom = 0.0, 0.0, float(an_w), float(an_h)
    x = min(max(left, cx - crop_w / 2), right - crop_w)
    y = min(max(top, cy - crop_h * FACE_VERTICAL_ANCHOR), bottom - crop_h)

    X, Y = int(x * to_src), int(y * to_src)
    W, H = int(crop_w * to_src), int(crop_h * to_src)
    W, H = min(W, src_w), min(H, src_h)
    X, Y = max(0, min(src_w - W, X)), max(0, min(src_h - H, Y))
    found = [s for s in ("left", "top", "right", "bottom")
             if b[s] is not None and b[s] not in (0, b["w" if s in ("left", "right") else "h"])]
    return {
        "x": X - (X % 2), "y": Y - (Y % 2),
        "w": W - (W % 2), "h": H - (H % 2),
        "detections": len(cluster),
        "samples": len(frames),
        "borders": found,
        "full_frame": False,
    }


def render_stacked_clip(
    source_path: str,
    start: float,
    end: float,
    out_path: str,
    out_w: int = 1080,
    out_h: int = 1920,
    corner: str = "bottom-left",
    cam_panel_fraction: float = CAM_PANEL_FRACTION,
    face_context_multiple: float = FACE_CONTEXT_MULTIPLE,
    log_label: str = "",
    frame: Optional[Dict] = None,
) -> Dict:
    """Cut [start, end] and render it as webcam-over-gameplay in one ffmpeg pass.

    `frame` is where someone put the gameplay window by hand (see framing.py):
    the webcam panel is still found automatically, and only the part of the
    game shown under it moves.
    """
    src_w, src_h = _probe_dimensions(source_path)
    cam_h = int(out_h * cam_panel_fraction)
    cam_h -= cam_h % 2
    game_h = out_h - cam_h
    cam = locate_webcam(source_path, start, end, corner=corner,
                        face_context_multiple=face_context_multiple,
                        panel_aspect=out_w / cam_h)

    # Said here, before the encode, rather than after it. Whether the webcam
    # was found decides which of the layouts below gets rendered, and that
    # is exactly the thing worth knowing when the render then fails. Reported
    # on the way out it vanishes precisely when it matters, leaving a log whose
    # plan line promises a stacked layout above a command that centre-crops.
    if log_label:
        if cam and cam.get("full_frame"):
            said = "fills the frame - following the face instead of stacking"
        elif cam:
            said = (f"{cam['w']}x{cam['h']} at ({cam['x']},{cam['y']}) from "
                    f"{cam['detections']}/{cam['samples']} samples, overlay edges: "
                    f"{', '.join(cam['borders']) or 'none found'}")
        else:
            said = "NOT FOUND — using a centre crop"
        print(f"{log_label} webcam {said}", flush=True)

    if cam and cam.get("full_frame"):
        from .clipper import crop_clip_local
        info = crop_clip_local(source_path, start, end, f"{out_w}:{out_h}", out_path,
                               target_resolution=f"{out_w}x{out_h}", frame=frame)
        return {**info, "cam": cam, "full_frame": True}

    # Gameplay: the widest centre crop matching the bottom panel's aspect,
    # nudged away from the corner the webcam occupies.
    game_crop_w, game_crop_h = framing.gameplay_window(src_w, src_h, out_w, out_h,
                                                       cam_panel_fraction)
    game_x = (src_w - game_crop_w) // 2
    if cam and corner.endswith("left"):
        game_x = max(game_x, min(cam["x"] + cam["w"], src_w - game_crop_w))
    game_x -= game_x % 2
    if cam:
        game_x, _ = framing.place(frame, src_w, src_h, game_crop_w, game_crop_h,
                                  (game_x, 0))

    if cam:
        filt = (
            f"[0:v]crop={cam['w']}:{cam['h']}:{cam['x']}:{cam['y']},"
            f"scale={out_w}:{cam_h}:flags=lanczos,setsar=1[cam];"
            f"[0:v]crop={game_crop_w}:{game_crop_h}:{game_x}:0,"
            f"scale={out_w}:{game_h}:flags=lanczos,setsar=1[game];"
            f"[cam][game]vstack=inputs=2[v]"
        )
    else:
        # No webcam found — fall back to a full-height centre crop.
        fb_w, fb_h = framing.picture_window(src_w, src_h, out_w, out_h)
        fb_x, fb_y = framing.place(frame, src_w, src_h, fb_w, fb_h,
                                   ((src_w - fb_w) // 2, (src_h - fb_h) // 2))
        filt = (
            f"[0:v]crop={fb_w}:{fb_h}:{fb_x}:{fb_y},"
            f"scale={out_w}:{out_h}:flags=lanczos,setsar=1[v]"
        )

    accel.run_encode(lambda enc: [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-i", source_path, "-t", f"{end - start:.3f}",
        "-filter_complex", filt,
        "-map", "[v]", "-map", "0:a:0?",
        "-af", LOUDNESS_FILTER,
        *enc,
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
        out_path,
    ], what="ffmpeg (stacked render)", crf=23)
    manual = bool(framing.normalise(frame))
    if not cam:
        return {"cam": None, "panel": framing.PICTURE, "manual": manual,
                "x": framing.fraction(fb_x, src_w, fb_w),
                "y": framing.fraction(fb_y, src_h, fb_h)}
    return {"cam": cam, "game_x": game_x, "cam_panel_h": cam_h,
            "panel": framing.GAMEPLAY, "manual": manual,
            "x": framing.fraction(game_x, src_w, game_crop_w)}


def render_stacked_highlights(
    source_path: str,
    highlights: List[Dict],
    out_dir: Optional[str] = None,
    corner: str = "bottom-left",
    out_w: int = 1080,
    out_h: int = 1920,
    cam_panel_fraction: float = CAM_PANEL_FRACTION,
    face_context_multiple: float = FACE_CONTEXT_MULTIPLE,
    name_prefix: str = "short",
) -> List[Dict]:
    out_dir = out_dir or LOCAL_OUTPUT_DIR
    os.makedirs(out_dir, exist_ok=True)
    results: List[Dict] = []
    for i, h in enumerate(highlights, 1):
        out_path = os.path.join(out_dir, f"{name_prefix}_{i:02d}.mp4")
        print(f"[stack] {i}/{len(highlights)}: {h.get('title', '(untitled)')}", flush=True)
        try:
            info = render_stacked_clip(
                source_path, float(h["start_time"]), float(h["end_time"]), out_path,
                corner=corner, out_w=out_w, out_h=out_h,
                cam_panel_fraction=cam_panel_fraction,
                face_context_multiple=face_context_multiple,
                log_label=f"[stack] {i}",
                frame=h.get("frame"),
            )
            results.append({**h, "clip_url": out_path, "layout": info})
        except Exception as e:
            print(f"[stack] {i} failed: {e}", flush=True)
            results.append({**h, "clip_url": None, "error": str(e)})
    return results
