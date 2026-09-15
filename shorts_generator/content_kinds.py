"""What kind of video this is, as far as choosing moments goes.

A stream, a vlog, a podcast and a tutorial are good for different reasons. A
stream's best moment is the streamer reacting to the game; a vlog's is the
payoff of something that happened; a podcast's is a hot take or a confession;
a tutorial's is one complete tip. A ranking prompt written for one of them
reads the others wrong -- told that every video is a game stream, the model
goes looking for game narration in a travel vlog.

So the ranker is told which it is looking at. The user can say so (the picker,
or "this is a vlog" in the prompt); otherwise it is worked out from the
transcript and the video's own listing.

Five kinds on purpose. The content-type detector distinguishes more than this
(interview from podcast, lecture from tutorial), but those pairs are good for
the same reasons, and a separate prompt per label would be five copies of one
rule book drifting apart.
"""
from typing import Optional

AUTO = "auto"
STREAM = "stream"
VLOG = "vlog"
PODCAST = "podcast"
TUTORIAL = "tutorial"
OTHER = "other"

KINDS = (STREAM, VLOG, PODCAST, TUTORIAL, OTHER)

# The detector's labels, folded onto the kinds above.
_FROM_CONTENT_TYPE = {
    "gaming_stream": STREAM,
    "gameplay": STREAM,
    "gameplay_commentary": STREAM,
    "just_chatting": STREAM,
    "stream": STREAM,
    "vlog": VLOG,
    "irl": VLOG,
    "travel": VLOG,
    "podcast": PODCAST,
    "interview": PODCAST,
    "debate": PODCAST,
    "tutorial": TUTORIAL,
    "lecture": TUTORIAL,
    "explainer": TUTORIAL,
}


def normalise(kind: Optional[str]) -> str:
    """A kind the rest of the app understands, or AUTO for anything else."""
    k = (kind or "").strip().lower()
    return k if k in KINDS else AUTO


def from_content_type(content_type: Optional[str]) -> str:
    """Fold a detector label ("interview", "gaming_stream") onto a kind."""
    ct = (content_type or "").strip().lower().replace(" ", "_").replace("-", "_")
    return _FROM_CONTENT_TYPE.get(ct, OTHER)


def default_layout(kind: str) -> Optional[str]:
    """The framing a kind wants when nobody asked for one, or None to leave it.

    Only vlogs and podcasts move. Their camera is the picture, so following the
    face is the right crop from the start. Streams keep the stacked layout, and
    tutorials usually are a screen recording with a webcam in the corner, which
    is exactly what stacked is for. Anything else keeps stacked too, which
    already falls back to following a face that fills the frame, and to a
    centre crop when there is no face at all.
    """
    return "facetrack" if kind in (VLOG, PODCAST) else None
