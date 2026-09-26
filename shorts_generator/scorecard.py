"""Why a clip ranked where it did, in a form a person can read at a glance.

Every clip already carries the numbers it was ranked on: the model's verdict
on its opening line and on the moment as a whole, and what the audio and the
words measured (signals.py). They were kept for the day retention data exists
to tune against, and until now nobody could see them. A score with no reason
behind it is a number to argue with; a score split into what it is made of,
with a sentence saying why, is one you can act on -- "strong moment, weak
hook" tells you to post it with a better cover line, not to skip it.

Five parts, each 0-100, each only shown when it was actually measured:

  Hook    how hard the first line stops a scroll        (the model)
  Moment  how strong the moment is as a whole           (the model)
  Look    how a stranger scrolling past would see it    (the vision judge)
  Energy  how loud its peak is against the whole video  (the audio)
  Pace    how quickly the talking starts                (the words)
"""
from typing import Dict, List, Optional

# Conversational speech runs about 2.5 words a second; a clip that opens at
# that rate is as quick off the mark as talk gets.
_BRISK_WORDS_PER_SECOND = 2.5

GRADES = (
    (85, "top", "Top pick"),
    (70, "strong", "Strong"),
    (55, "good", "Worth a look"),
    (0, "weak", "Long shot"),
)


def _int(value) -> Optional[int]:
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return None


def grade(score: Optional[int]) -> Optional[Dict]:
    if score is None:
        return None
    for floor, key, label in GRADES:
        if score >= floor:
            return {"key": key, "label": label}
    return None


def build(clip: Dict) -> Optional[Dict]:
    """The scorecard for one clip record, or None when it was never scored.

    Exact spans the user typed in and clips adopted from a bare folder have no
    score, and inventing one for them would be worse than showing nothing.
    """
    score = _int(clip.get("score"))
    if score is None:
        return None

    signals = clip.get("signals") or {}
    parts: List[Dict] = []

    hook = _int(clip.get("hook_score"))
    if hook is not None:
        parts.append({"key": "hook", "label": "Hook", "value": hook,
                      "why": "How hard the first line stops a scroll"})

    moment = _int(clip.get("viral_score"))
    if moment is None:
        moment = _int(clip.get("model_score"))
    if moment is not None:
        parts.append({"key": "moment", "label": "Moment", "value": moment,
                      "why": "How strong the moment is as a whole"})

    # The vision judge's three answers, as one number: what the frames said
    # about the opening, the visible payoff and whether it stands alone.
    seen = clip.get("judge") or {}
    looked = [_int(seen.get(k)) for k in ("first_second", "payoff_visible", "standalone")]
    looked = [v for v in looked if v is not None]
    if looked:
        parts.append({"key": "look", "label": "Look",
                      "value": _int(sum(looked) / len(looked)),
                      "why": "How a stranger scrolling past would see it"})

    spike = signals.get("audio_spike")
    if spike is not None:
        parts.append({"key": "energy", "label": "Energy", "value": _int(100 * float(spike)),
                      "why": "How loud its peak is against the rest of the video"})

    density = signals.get("density", clip.get("opening_density"))
    if density is not None:
        parts.append({"key": "pace", "label": "Pace",
                      "value": _int(100 * float(density) / _BRISK_WORDS_PER_SECOND),
                      "why": "How quickly the talking starts"})

    notes: List[str] = []
    if hook is not None and hook >= 80:
        notes.append("Opens on a line that stops the scroll")
    elif hook is not None and hook < 45:
        notes.append("The first line is weak, so the payoff has to carry it")
    if float(signals.get("keyword") or 0) >= 0.6:
        notes.append("Says something people react to")
    if float(signals.get("silence_to_peak") or 0) >= 0.5:
        notes.append("A quiet beat before the payoff")
    if spike is not None and float(spike) >= 0.9:
        notes.append("One of the loudest moments in the video")
    if seen.get("verdict") == "cut":
        notes.append("A stranger would likely swipe past it")
    if clip.get("visual_penalty"):
        notes.append("Opens dark or still, which cost it points")
    if clip.get("opening_penalty"):
        notes.append("Slow first seconds cost it points")

    reason = str(clip.get("virality_reason") or "").strip()
    return {
        "score": score,
        "grade": grade(score),
        "parts": parts,
        "reason": reason,
        "notes": notes[:3],
    }


def attach(clips: List[Dict]) -> None:
    """Give every clip in a list its scorecard, in place."""
    for c in clips:
        card = build(c)
        if card:
            c["scorecard"] = card
        else:
            c.pop("scorecard", None)
