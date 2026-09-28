"""Face detection, done one way for the whole app.

Two renderers need faces: the stacked layout, to find the streamer in the
webcam overlay, and the face-following crop, to keep a speaker in frame. Both
used Haar cascades, and Haar is the weakest part of either. It is a 2001
detector of frontal faces: a head turned to talk to a co-host, a dim room, a
large headset or a hand over the mouth and it sees nothing -- which is why the
face crop needed a second, profile cascade run in both directions, and why the
stacked layout needed a twenty-frame vote to stay on the right person.

YuNet is a small learned detector (about 230 KB, from OpenCV's own model zoo)
that OpenCV runs natively through FaceDetectorYN. It finds faces at angles and
in light that Haar cannot, gives each one a confidence, and runs in about 12 ms
on a 960px frame on a laptop CPU. The model ships inside every build, under
assets/models, so nothing is downloaded at run time.

Haar stays as the fallback. If the model file is missing, or this OpenCV has no
FaceDetectorYN, or the model will not load, detection drops back to exactly
what it was before rather than failing -- and says so once in the log.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional, Tuple

MODEL_NAME = "face_detection_yunet_2023mar.onnx"
# YuNet's confidence floor. Its own default is 0.9, tuned for photographs;
# webcam faces in a corner overlay are small and softly lit, and the renderers
# already vote across many frames, so a lower floor that keeps real faces
# costs less than a higher one that drops them.
SCORE_THRESHOLD = 0.7
NMS_THRESHOLD = 0.3

# (centre x, centre y, width, confidence), in the pixels of the image given.
Face = Tuple[float, float, float, float]

_yunet = None
_yunet_size: Optional[Tuple[int, int]] = None
_yunet_unusable = False
_haar = None
_announced = False


def model_path() -> Optional[Path]:
    """Where the YuNet model is, in a source checkout or a packaged build."""
    candidates = []
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidates.append(Path(bundle) / "assets" / "models" / MODEL_NAME)
    candidates.append(Path(__file__).resolve().parent.parent / "assets" / "models" / MODEL_NAME)
    return next((p for p in candidates if p.exists()), None)


def _say_once(message: str) -> None:
    global _announced
    if not _announced:
        _announced = True
        print(message, flush=True)


def _get_yunet(width: int, height: int):
    """The YuNet detector, sized for this frame, or None if it cannot be had."""
    global _yunet, _yunet_size, _yunet_unusable
    if _yunet_unusable:
        return None
    if _yunet is None:
        import cv2  # type: ignore
        path = model_path()
        if path is None or not hasattr(cv2, "FaceDetectorYN"):
            _yunet_unusable = True
            _say_once("[faces] YuNet is not available here - using the older Haar detector")
            return None
        try:
            _yunet = cv2.FaceDetectorYN.create(str(path), "", (width, height),
                                               SCORE_THRESHOLD, NMS_THRESHOLD, 50)
        except Exception as e:  # noqa: BLE001 - a detector must never sink a render
            _yunet_unusable = True
            _say_once(f"[faces] YuNet would not load ({str(e).splitlines()[0][:100]}) "
                      f"- using the older Haar detector")
            return None
        _say_once("[faces] finding faces with YuNet")
    if _yunet_size != (width, height):
        _yunet.setInputSize((width, height))
        _yunet_size = (width, height)
    return _yunet


def _haar_faces(img, min_side: int) -> List[Face]:
    """Frontal faces, then profiles in both directions if there were none."""
    import cv2  # type: ignore
    global _haar
    if _haar is None:
        root = cv2.data.haarcascades
        _haar = (cv2.CascadeClassifier(root + "haarcascade_frontalface_default.xml"),
                 cv2.CascadeClassifier(root + "haarcascade_profileface.xml"))
    frontal, profile = _haar
    gray = cv2.equalizeHist(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)) if img.ndim == 3 else img
    w = gray.shape[1]
    boxes = [tuple(map(float, f)) for f in frontal.detectMultiScale(
        gray, scaleFactor=1.1, minNeighbors=6, minSize=(min_side, min_side))]
    if not boxes:
        for flipped in (False, True):
            src = cv2.flip(gray, 1) if flipped else gray
            for (x, y, fw, fh) in profile.detectMultiScale(
                    src, scaleFactor=1.1, minNeighbors=6, minSize=(min_side, min_side)):
                x = (w - x - fw) if flipped else x
                boxes.append((float(x), float(y), float(fw), float(fh)))
    return [(x + fw / 2, y + fh / 2, fw, 1.0) for (x, y, fw, fh) in boxes]


def detect(img, min_fraction: float = 0.02) -> List[Face]:
    """Every face in a BGR frame, smallest ignored.

    `min_fraction` is the smallest face kept, as a share of the frame's width:
    a webcam face in a corner overlay is ~3-6% of a 1080p frame, and anything
    far smaller is noise in the game.
    """
    if img is None or img.size == 0:
        return []
    h, w = img.shape[:2]
    min_side = max(12, int(w * min_fraction))
    detector = _get_yunet(w, h) if img.ndim == 3 else None
    if detector is None:
        return _haar_faces(img, max(20, min_side))
    _, found = detector.detect(img)
    out: List[Face] = []
    for f in (found if found is not None else []):
        x, y, fw, fh = (float(v) for v in f[:4])
        if fw < min_side:
            continue
        out.append((x + fw / 2, y + fh / 2, fw, float(f[14])))
    return out


def detect_with_mouths(img, min_fraction: float = 0.02
                       ) -> List[Tuple[Face, Optional[Tuple[float, float]]]]:
    """Every face, each with the midpoint of its mouth, or None.

    The mouth is what says who is talking: see local/speaker.py. YuNet reports
    the two mouth corners along with the face (landmarks 10-13), so the point
    follows the lips through a turned head. Haar reports only a square, and
    gets None: the caller then places the mouth where it usually sits.
    """
    if img is None or img.size == 0:
        return []
    h, w = img.shape[:2]
    min_side = max(12, int(w * min_fraction))
    detector = _get_yunet(w, h) if img.ndim == 3 else None
    if detector is None:
        return [(f, None) for f in _haar_faces(img, max(20, min_side))]
    _, found = detector.detect(img)
    out: List[Tuple[Face, Optional[Tuple[float, float]]]] = []
    for f in (found if found is not None else []):
        x, y, fw, fh = (float(v) for v in f[:4])
        if fw < min_side:
            continue
        rx, ry, lx, ly = (float(v) for v in f[10:14])
        out.append(((x + fw / 2, y + fh / 2, fw, float(f[14])),
                    ((rx + lx) / 2, (ry + ly) / 2)))
    return out


def backend() -> str:
    """"yunet" or "haar" -- which detector the next call will use."""
    if _yunet is not None:
        return "yunet"
    if _yunet_unusable:
        return "haar"
    try:
        import cv2  # type: ignore
        return "yunet" if model_path() and hasattr(cv2, "FaceDetectorYN") else "haar"
    except ImportError:
        return "haar"
