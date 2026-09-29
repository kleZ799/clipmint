"""What the channel's own numbers say about the clips it posted.

Every clip carries the numbers it was ranked on -- the model's scores, the
measured signals, what the vision check saw -- and until now none of that was
ever checked against what happened next. The ranking's weights were seeds from
a rule book, the same for every channel, forever.

This module does the checking. Given clips that went up on YouTube, with their
view counts, it asks one question per feature: did the clips that scored
higher on it get more views? And it answers the way a careful person would:

  * Views are compared by rank, not by value. One Short that took off would
    otherwise decide every answer on its own.
  * Only clips at least two days old count. A Short's views come mostly in its
    first days, and yesterday's upload has not had them yet; letting it in
    would make "recent" look like "bad".
  * Every finding is tested against chance (a permutation test: shuffle the
    views across clips thousands of times and see how often a pattern this
    strong appears anyway). With a few dozen clips most apparent patterns are
    noise, and saying so is the most useful thing this can do.

What survives that test does two things. It is shown to the person, in plain
words, as what is and is not working on their channel. And for the measured
signals, it adjusts how much each one counts in the ranking -- only on strong
evidence, only by a bounded amount, and shrunk toward the defaults when there
are few clips, so twelve Shorts can nudge the ranking but not rewrite it.

Nothing here touches the network. The view counts arrive from
webapp/youtube_stats.py, which keeps them under YouTube's 30-day rule; this
module works from whatever is on the clips right now and stores nothing, so
anything it concludes expires with the numbers it came from.
"""
from __future__ import annotations

import math
import random
import statistics
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

# A Short younger than this has not had its views yet.
MIN_AGE = timedelta(days=2)
# Fewer clips than this and nothing is concluded at all.
MIN_CLIPS = 6
# A finding is reported as real below this chance of being noise...
REAL_P = 0.05
# ...and as a hint worth watching below this.
HINT_P = 0.15
# The ranking only moves on this many clips or more, and only on a finding at
# REAL_P. Shrinkage: n / (n + PRIOR_CLIPS) of the evidence is believed.
TUNE_MIN_CLIPS = 12
PRIOR_CLIPS = 20
# How far a signal's weight may move either way.
TUNE_MAX = 0.4
PERMUTATIONS = 4000


def _get(clip: Dict, *path):
    cur = clip
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _num(v) -> Optional[float]:
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _title_written(clip: Dict) -> Optional[bool]:
    seo = clip.get("seo")
    if not isinstance(seo, dict) or not seo:
        return None
    if seo.get("_fallback_for") or seo.get("generated") is False:
        return False
    return True


# Every feature a clip can be judged on: the key, what it is called for a
# person, how to read it off the clip, and, for measured signals, the weight
# in signals.BASE_WEIGHTS it would tune.
FEATURES: List[Tuple[str, str, Callable[[Dict], object], Optional[str]]] = [
    ("score", "ClipMint's overall score", lambda c: _num(c.get("score")), None),
    ("hook", "the hook score (how hard the first line stops a scroll)",
     lambda c: _num(c.get("hook_score")), None),
    ("moment", "the moment score", lambda c: _num(c.get("viral_score") or c.get("model_score")), None),
    ("look", "the Look score (how the frames came across)",
     lambda c: _num(_get(c, "judge", "first_second")), None),
    ("energy", "how loud its peak was", lambda c: _num(_get(c, "signals", "audio_spike")), "audio_spike"),
    ("words", "reaction words in it (\"no way\", \"wait for it\")",
     lambda c: _num(_get(c, "signals", "keyword")), "keyword"),
    ("motion", "how much the picture moved", lambda c: _num(_get(c, "signals", "motion")), "motion"),
    ("buildup", "a quiet beat before the payoff",
     lambda c: _num(_get(c, "signals", "silence_to_peak")), "silence_to_peak"),
    ("chat", "how hard the stream's chat reacted",
     lambda c: _num(_get(c, "signals", "chat_velocity")), "chat_velocity"),
    ("pace", "how fast the talking starts", lambda c: _num(_get(c, "signals", "density")), None),
    ("length", "its length", lambda c: _num(c.get("duration")), None),
    ("title", "a title the AI actually wrote", _title_written, None),
]


def published(clip: Dict) -> Optional[datetime]:
    raw = _get(clip, "performance", "published_at")
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def views(clip: Dict) -> Optional[int]:
    v = _get(clip, "performance", "views")
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def judged(clips: Sequence[Dict], now: Optional[datetime] = None) -> List[Dict]:
    """The clips with views old enough to mean something."""
    now = now or datetime.now(timezone.utc)
    out = []
    for c in clips:
        when = published(c)
        if views(c) is None or when is None or now - when < MIN_AGE:
            continue
        out.append(c)
    return out


def _ranks(xs: Sequence[float]) -> List[float]:
    """Average ranks, ties sharing theirs."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2.0
        i = j + 1
    return r


def _corr(a: Sequence[float], b: Sequence[float]) -> float:
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    den = math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))
    return num / den if den else 0.0


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float:
    return _corr(_ranks(xs), _ranks(ys))


def _p_value(stat: Callable[[Sequence[float]], float], ys: Sequence[float], seed: int) -> float:
    """Two-sided permutation p: how often shuffled views look this strong."""
    observed = abs(stat(ys))
    rng = random.Random(seed)
    pool = list(ys)
    hits = 0
    for _ in range(PERMUTATIONS):
        rng.shuffle(pool)
        if abs(stat(pool)) >= observed - 1e-12:
            hits += 1
    return (hits + 1) / (PERMUTATIONS + 1)


def _fmt_views(v: float) -> str:
    v = float(v)
    if v >= 10000:
        return f"{v / 1000:.0f}K"
    if v >= 1000:
        return f"{v / 1000:.1f}K"
    return f"{v:.0f}" if v == int(v) else f"{v:.1f}"


def _times(hi: float, lo: float) -> str:
    """"5x", "1.4x" -- how many times more, with +1 so zero views compare."""
    ratio = (hi + 1) / (lo + 1)
    return f"{ratio:.0f}x" if ratio >= 3 else f"{ratio:.1f}x"


def analyse(clips: Sequence[Dict], now: Optional[datetime] = None) -> Dict:
    """Everything the channel's numbers say, ready to show and to rank with."""
    rows = judged(clips, now)
    all_views = [views(c) for c in rows]
    out: Dict = {
        "clips": len(rows),
        "waiting": sum(1 for c in clips if views(c) is not None) - len(rows),
        "median_views": statistics.median(all_views) if all_views else None,
        "findings": [],
        "ranking": None,
        "tuning": {},
    }
    if len(rows) < MIN_CLIPS:
        return out

    for n_feat, (key, label, read, weight) in enumerate(FEATURES):
        pairs = [(read(c), views(c)) for c in rows]
        pairs = [(x, y) for x, y in pairs if x is not None]
        if len(pairs) < MIN_CLIPS:
            continue
        xs = [float(x) for x, _ in pairs]
        ys = [float(y) for _, y in pairs]
        if len(set(xs)) < 2:
            continue
        finding = _judge(key, label, xs, ys, seed=1000 + n_feat)
        if finding is None:
            continue
        finding["weight"] = weight
        out["findings"].append(finding)
        if key == "score":
            out["ranking"] = finding

    out["findings"].sort(key=lambda f: (f["p"], -abs(f["rho"])))
    out["tuning"] = tuning(out["findings"], len(rows))
    return out


def _judge(key: str, label: str, xs: List[float], ys: List[float], seed: int) -> Optional[Dict]:
    n = len(xs)
    binary = set(xs) <= {0.0, 1.0}
    if binary:
        hi = [y for x, y in zip(xs, ys) if x == 1.0]
        lo = [y for x, y in zip(xs, ys) if x == 0.0]
        if len(hi) < 2 or len(lo) < 2:
            return None
    else:
        cut = statistics.median(xs)
        hi = [y for x, y in zip(xs, ys) if x > cut]
        lo = [y for x, y in zip(xs, ys) if x <= cut]
        if len(hi) < 2 or len(lo) < 2:
            return None
    rx = _ranks(xs)
    rho = _corr(rx, _ranks(ys))
    p = _p_value(lambda perm: _corr(rx, _ranks(perm)), ys, seed)
    med_hi, med_lo = statistics.median(hi), statistics.median(lo)
    better = rho > 0
    verdict = "real" if p < REAL_P else ("hint" if p < HINT_P else "none")

    if binary:
        what = (f"Clips with {label} got a median of {_fmt_views(med_hi)} views; "
                f"clips without got {_fmt_views(med_lo)}")
    else:
        what = (f"Clips higher on {label} got a median of {_fmt_views(med_hi)} views; "
                f"lower ones got {_fmt_views(med_lo)}")
    if verdict == "none":
        text = f"No clear link between {label} and views yet."
    else:
        big, small = (med_hi, med_lo) if better else (med_lo, med_hi)
        text = f"{what} ({_times(big, small)} {'more' if better else 'fewer'})."
    return {
        "key": key, "label": label, "text": text, "verdict": verdict,
        "rho": round(rho, 3), "p": round(p, 4), "n": n,
        "groups": [len(hi), len(lo)],
        "median_high": med_hi, "median_low": med_lo,
    }


def tuning(findings: Sequence[Dict], n: int) -> Dict[str, float]:
    """How much each measured signal should count, from the channel's numbers.

    A multiplier on its rule-book weight: 1.0 is unchanged. Only a signal with
    a finding that is real moves, only once there are enough clips, and by
    rho shrunk toward zero by how few clips there are, capped at TUNE_MAX.
    """
    if n < TUNE_MIN_CLIPS:
        return {}
    believe = n / (n + PRIOR_CLIPS)
    out: Dict[str, float] = {}
    for f in findings:
        weight = f.get("weight")
        if not weight or f["verdict"] != "real":
            continue
        shift = max(-TUNE_MAX, min(TUNE_MAX, f["rho"] * believe))
        if abs(shift) >= 0.05:
            out[weight] = round(1.0 + shift, 3)
    return out


def describe_tuning(tune: Dict[str, float]) -> str:
    """One line for a run's log."""
    names = {"audio_spike": "loudness", "keyword": "reaction words", "motion": "motion",
             "silence_to_peak": "build-up", "chat_velocity": "chat"}
    return ", ".join(f"{names.get(k, k)} x{v:.2f}" for k, v in sorted(tune.items()))
