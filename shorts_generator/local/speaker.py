"""Who is talking: follow the speaker when more than one person is in frame.

The face-following crop used to follow the biggest face. On a one-person
video that is right. On a two-person podcast shot wide, both people are in
frame, a vertical crop only fits one of them, and the biggest face -- usually
whoever sits nearer the camera -- got the whole clip, including every line
the other person said.

The picture says who is talking. A speaker's jaw moves; a listener's mostly
does not. So at every sample the crop already takes, each person's mouth is
compared with the same mouth one frame earlier:

  anchor   the mouth box sits on the mouth corners the face detector reports,
           smoothed over a second so the box itself does not jitter
  align    the two patches are shifted onto each other by phase correlation
           before they are compared, so the head moving is not read as the
           mouth moving
  compare  the mean difference of the two brightness-normalised patches

One frame, not one sample: across a whole sample gap the head has moved as
well. Measured both ways, the one-frame gap was right markedly more often.

Measured on two episodes of a two-person podcast, inside 30-second clips as
the app cuts them. On the first, with the voices as ground truth, it picked
the speaker in 86% of two-shot moments and caught 34 of the 41 lines the
second person said; the biggest face caught none of them. On the second,
labelled by hand and never tuned on, it was right 96% of the time, against
10% for the biggest face, which sat on the nearer person while the other did
nearly all the talking.

Several alternatives did worse on the same footage: optical flow inside the
mouth, plain patch differences with no alignment, correlating the mouth with
the audio (no better than chance at 8 samples a second), and normalising each
person against their own typical movement, which looks principled and is
harmful: in a clip where one person talks throughout, it makes the talker and
the listener look the same.

The decision is an editor's, not a meter's. Activity is averaged over two
seconds centred on each sample -- the plan is made before rendering, so it
can look ahead and cut as a person starts talking rather than a second after.
The shot moves to someone else only when they are clearly more active, and
no shot is shorter than two seconds, so a laugh or a nod does not flick the
frame across the table.
"""
from __future__ import annotations

import itertools
from typing import Dict, List, Optional, Sequence, Tuple

# How long activity is averaged over, centred on each sample.
WINDOW_SECONDS = 2.0
# How much more active someone has to be than the person on screen before
# the shot goes to them.
SWITCH_MARGIN = 1.15
# The shortest a shot may be. Anything briefer is folded into the one before.
MIN_SHOT_SECONDS = 2.0
# A person has to be in at least this share of the samples to be a candidate:
# a face that flickers in for a moment is a false detection or someone
# walking past.
MIN_PRESENCE = 0.25
# A candidate's face has to be at least this fraction of the largest
# person's. Two hosts at one table are within a few tens of percent of each
# other; a face in a video playing on a stream, a poster, or the tiny figures
# in a split-screen's wide panel are far smaller, and must not be able to
# pull the shot off the person the clip is about.
MIN_RELATIVE_SIZE = 0.5
# Two candidates have to share at least this share of samples for there to be
# a choice to make. Below it, the video cuts between people on its own (one
# camera per host) and following whoever is visible already does the job.
MIN_TOGETHER = 0.2

# The mouth patch, relative to face width, around the mouth corners' midpoint.
# Wider than the lips and further down than up: speech is the jaw dropping.
_PATCH_HALF_W = 0.32
_PATCH_UP = 0.20
_PATCH_DOWN = 0.30
_PATCH_SIZE = (64, 50)
# The region stored per sample, so the smoothed patch can be cut from it
# later: the patch plus room for the smoothing to move it.
_REGION_HALF_W = 0.6
_REGION_UP = 0.45
_REGION_DOWN = 0.55
# Regions are kept at most this many pixels per face width. A 1440p podcast
# face is 300px across, and keeping two full-size regions per face per sample
# ran to hundreds of megabytes on a long clip; the patch is 64 pixels wide, so
# 128 per face width is still twice the detail it is cut to.
_REGION_FACE_PX = 128


class Sighting:
    """One face at one sample, with the pixels needed to measure its mouth."""

    __slots__ = ("cx", "cy", "fw", "mx", "my", "region", "before", "ox", "oy", "scale")

    def __init__(self, cx: float, cy: float, fw: float, mx: float, my: float,
                 region, before, ox: int, oy: int, scale: float) -> None:
        self.cx, self.cy, self.fw = cx, cy, fw
        self.mx, self.my = mx, my
        # The same pixels in this frame and in the one just before it, from
        # (ox, oy) in the frame, shrunk by `scale`.
        self.region, self.before = region, before
        self.ox, self.oy, self.scale = ox, oy, scale


def sighting(frame, before, cx: float, cy: float, fw: float,
             mouth: Optional[Tuple[float, float]]) -> Sighting:
    """Cut the gray region around a face's mouth out of a full-size frame,
    and the same region out of the frame just before it (`before`, or None).

    `mouth` is the mouth corners' midpoint in frame pixels, or None when the
    detector gave no landmarks; then the mouth is placed where it usually is.
    """
    import cv2  # type: ignore
    mx, my = mouth if mouth else (cx, cy + 0.27 * fw)
    h, w = frame.shape[:2]
    x0 = max(0, int(mx - _REGION_HALF_W * fw))
    x1 = min(w, int(mx + _REGION_HALF_W * fw))
    y0 = max(0, int(my - _REGION_UP * fw))
    y1 = min(h, int(my + _REGION_DOWN * fw))
    scale = min(1.0, _REGION_FACE_PX / max(1.0, fw))

    def gray(img):
        if img is None or x1 - x0 < 8 or y1 - y0 < 8 or img.shape[:2] != (h, w):
            return None
        crop = img[y0:y1, x0:x1]
        crop = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop.copy()
        if scale < 1.0:
            crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        return crop

    return Sighting(cx, cy, fw, mx, my, gray(frame), gray(before), x0, y0, scale)


def assign(samples: Sequence[Sequence[Sighting]], src_w: int) -> List[List[Optional[Sighting]]]:
    """Group sightings into people by where they sit, one list per person.

    Nearest-centre matching against where each person was last seen. People
    on a podcast stay in their chairs, so position is identity -- and when the
    source cuts to a single camera, whoever is in its centre is matched to
    whoever sat nearest that spot, which is the right person often enough and
    harmless when it is not, because only one face is on screen to follow.
    """
    people: List[List[Optional[Sighting]]] = []
    last: List[Sighting] = []
    for i, found in enumerate(samples):
        taken = set()
        for p in people:
            p.append(None)
        pairs = sorted(
            ((abs(s.cx - l.cx) + abs(s.cy - l.cy), si, li)
             for si, s in enumerate(found) for li, l in enumerate(last)),
            key=lambda x: x[0])
        used_s = set()
        for dist, si, li in pairs:
            if si in used_s or li in taken:
                continue
            s = found[si]
            if dist > max(1.0 * s.fw, 0.08 * src_w):
                continue
            people[li][i] = s
            last[li] = s
            used_s.add(si)
            taken.add(li)
        for si, s in enumerate(found):
            if si not in used_s:
                people.append([None] * i + [s])
                last.append(s)
    return people


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    return ordered[n // 2] if n % 2 else 0.5 * (ordered[n // 2 - 1] + ordered[n // 2])


def _patch(s: Sighting, region, mx: float, my: float, fw: float):
    """The mouth patch at a smoothed anchor, cut from a stored region."""
    import cv2  # type: ignore
    if region is None:
        return None
    k = s.scale
    x0 = int(round((mx - _PATCH_HALF_W * fw - s.ox) * k))
    x1 = int(round((mx + _PATCH_HALF_W * fw - s.ox) * k))
    y0 = int(round((my - _PATCH_UP * fw - s.oy) * k))
    y1 = int(round((my + _PATCH_DOWN * fw - s.oy) * k))
    rh, rw = region.shape[:2]
    if x0 < 0 or y0 < 0 or x1 > rw or y1 > rh or x1 - x0 < 6 or y1 - y0 < 6:
        return None
    return cv2.resize(region[y0:y1, x0:x1], _PATCH_SIZE,
                      interpolation=cv2.INTER_AREA).astype("float32")


def mouth_activity(person: Sequence[Optional[Sighting]], per_second: float) -> List[Optional[float]]:
    """How much this person's mouth moved in the frame before each sample.

    One frame, not one sample: across a whole sample gap (an eighth of a
    second) the head has moved as well, and the listener nodding along reads
    as talking. Measured both ways on the same podcast, the one-frame gap was
    right about the speaker markedly more often.
    """
    import cv2  # type: ignore
    import numpy as np

    n = len(person)
    half = max(1, int(round(per_second / 2)))
    out: List[Optional[float]] = [None] * n
    for i, s in enumerate(person):
        if s is None or s.before is None:
            continue
        near = [p for p in person[max(0, i - half):i + half + 1] if p is not None]
        mx = _median([p.mx for p in near])
        my = _median([p.my for p in near])
        fw = _median([p.fw for p in near])
        patch = _patch(s, s.region, mx, my, fw)
        prev = _patch(s, s.before, mx, my, fw)
        if patch is not None and prev is not None:
            (dx, dy), _ = cv2.phaseCorrelate(prev, patch)
            shift = np.float32([[1, 0, -dx], [0, 1, -dy]])
            aligned = cv2.warpAffine(patch, shift, _PATCH_SIZE, borderMode=cv2.BORDER_REPLICATE)
            a, b = aligned[4:-4, 4:-4], prev[4:-4, 4:-4]
            a = (a - a.mean()) / (a.std() + 1e-3)
            b = (b - b.mean()) / (b.std() + 1e-3)
            out[i] = float(np.abs(a - b).mean())
    return out


def _smooth(values: Sequence[Optional[float]], width: int) -> List[Optional[float]]:
    """Centred mean over `width` samples, where at least three were measured."""
    half = width // 2
    out: List[Optional[float]] = []
    for i in range(len(values)):
        near = [v for v in values[max(0, i - half):i + half + 1] if v is not None]
        out.append(sum(near) / len(near) if len(near) >= 3 else None)
    return out


def choose(activity: Sequence[Sequence[Optional[float]]], per_second: float) -> List[Optional[int]]:
    """Who the shot is on at each sample, as an index into `activity`."""
    n = len(activity[0]) if activity else 0
    smooth = [_smooth(a, max(3, int(round(WINDOW_SECONDS * per_second)))) for a in activity]
    picks: List[Optional[int]] = []
    cur: Optional[int] = None
    for i in range(n):
        live = [(v[i], k) for k, v in enumerate(smooth) if v[i] is not None]
        if not live:
            picks.append(cur)
            continue
        best_v, best = max(live)
        here = smooth[cur][i] if cur is not None else None
        if cur is None or here is None or best_v > here * SWITCH_MARGIN:
            cur = best
        picks.append(cur)

    # No shot shorter than MIN_SHOT_SECONDS: a short run joins the shot before
    # it, or, at the very start, the one after.
    shortest = MIN_SHOT_SECONDS * per_second
    runs = [(k, len(list(g))) for k, g in itertools.groupby(picks)]
    out: List[Optional[int]] = []
    held: Optional[int] = None
    for j, (k, length) in enumerate(runs):
        if length < shortest and held is not None:
            out.extend([held] * length)
            continue
        if length < shortest and held is None and j + 1 < len(runs):
            k = runs[j + 1][0]
        out.extend([k] * length)
        held = k
    return out


def follow_speaker(samples: Sequence[Sequence[Sighting]], per_second: float,
                   src_w: int) -> Optional[Tuple[List[Optional[Tuple[float, float, float]]], Dict]]:
    """The face to frame at each sample, following whoever is talking.

    Returns None when there is no one to choose between -- one person, or
    people who are never on screen together -- so the caller keeps its own
    single-face following, which is right for those.
    """
    n = len(samples)
    if n < 3:
        return None
    people = [p for p in assign(samples, src_w)
              if sum(1 for s in p if s is not None) >= MIN_PRESENCE * n]
    if len(people) < 2:
        return None
    size = [_median([s.fw for s in p if s is not None]) for p in people]
    people = [p for p, fw in zip(people, size) if fw >= MIN_RELATIVE_SIZE * max(size)]
    if len(people) < 2:
        return None
    together = sum(1 for i in range(n) if sum(1 for p in people if p[i] is not None) >= 2)
    if together < MIN_TOGETHER * n:
        return None

    activity = [mouth_activity(p, per_second) for p in people]
    picks = choose(activity, per_second)
    track: List[Optional[Tuple[float, float, float]]] = []
    for i, k in enumerate(picks):
        s = people[k][i] if k is not None else None
        if s is None:
            # The chosen person is not in this sample. If someone else is --
            # the source has cut to a single camera -- follow them; otherwise
            # hold, and the planner keeps the last position.
            others = [p[i] for p in people if p[i] is not None]
            s = max(others, key=lambda o: o.fw) if others else None
        track.append((s.cx, s.cy, s.fw) if s is not None else None)

    shots = sum(1 for a, b in zip(picks, picks[1:]) if a != b) + 1
    at_once = max(sum(1 for p in people if p[i] is not None) for i in range(n))
    info = {"people": at_once, "together": together / n, "shots": shots}
    return track, info
