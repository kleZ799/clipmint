"""Hindi captions in Hinglish -- the Roman script people actually read them in.

Whisper writes Hindi in Devanagari, and it writes every word of a Hinglish
sentence that way too: "वीडियो", "गेम", "ब्रो". That is a faithful transcript
and the wrong caption. Shorts in Hindi are captioned the way the audience
types in chats and comments -- "bhai ye game kya hai yaar" -- and a wall of
Devanagari over a clip reads as foreign to most of the people it is for.

So the words are rewritten, one for one, before anything is drawn. One for
one matters: every word carries the time it was spoken, and captions, cuts
and the caption editor all lean on that, so the list that comes out has the
same length and the same timings as the list that went in. Only the text
changes, and only for words with Devanagari in them.

There are two ways to do it, and the better one is not always there:

- **The LLM**, when one is configured. It knows that "गेम" is the English
  word "game" and not "gem", and it spells Hindi the way people casually do
  rather than the way a scheme says to.
- **Rules**, always. A transliterator with Hindi's schwa deletion (करना is
  "karna", not "karana") and a list of the words that come up constantly,
  English loanwords from streams included. It never fails, so it is what any
  word the LLM got wrong, or every word when there is no LLM, falls back to.
"""
from __future__ import annotations

import json
import re
from typing import Callable, Dict, List, Optional

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")

# The ones that come up in nearly every sentence, spelled the way they are
# typed rather than the way the rules would get them (नहीं is "nahi", not
# "nahin"), and the English that streamers speak most, so the rules can get
# those right without an LLM.
_COMMON = {
    "है": "hai", "हैं": "hain", "में": "mein", "मैं": "main", "नहीं": "nahi",
    "नही": "nahi", "यह": "yeh", "वह": "woh", "ये": "ye", "वो": "wo",
    "क्या": "kya", "क्यों": "kyun", "क्यूँ": "kyun", "क्यूं": "kyun",
    "कि": "ki", "तो": "toh", "भी": "bhi", "और": "aur", "हाँ": "haan",
    "हां": "haan", "भाई": "bhai", "यार": "yaar", "अच्छा": "accha",
    "अच्छी": "acchi", "अच्छे": "acche", "ठीक": "theek", "कुछ": "kuch",
    "कैसे": "kaise", "ऐसे": "aise", "वैसे": "waise", "ऐसा": "aisa",
    "वैसा": "waisa", "बहुत": "bahut", "सब": "sab", "हम": "hum", "तुम": "tum",
    "आप": "aap", "मुझे": "mujhe", "तुझे": "tujhe", "हूँ": "hoon", "हूं": "hoon",
    "था": "tha", "थी": "thi", "थे": "the", "वाला": "wala", "वाली": "wali",
    "वाले": "wale", "फिर": "phir", "लेकिन": "lekin", "क्योंकि": "kyunki",
    "मतलब": "matlab", "वाह": "wah", "अरे": "arre", "ना": "na", "न": "na",
    "एक": "ek", "पता": "pata", "चलो": "chalo", "कहाँ": "kahan", "यहाँ": "yahan",
    "वहाँ": "wahan", "कहां": "kahan", "यहां": "yahan", "वहां": "wahan",
    # English, as Whisper writes it in Devanagari.
    "ओके": "okay", "ओह": "oh", "हेलो": "hello", "हैलो": "hello",
    "थैंक्यू": "thank you", "सॉरी": "sorry", "गाइज़": "guys", "गाइज": "guys",
    "गाइस": "guys", "ब्रो": "bro", "वीडियो": "video", "वीडियोज़": "videos",
    "गेम": "game", "गेम्स": "games", "स्ट्रीम": "stream", "चैनल": "channel",
    "सब्सक्राइब": "subscribe", "लाइक": "like", "कमेंट": "comment",
    "लाइव": "live", "चैट": "chat", "प्लीज़": "please", "प्लीज": "please",
    "फ्रेंड": "friend", "फ्रेंड्स": "friends", "टीम": "team", "मैच": "match",
    "लेवल": "level", "सीरियसली": "seriously", "एक्चुअली": "actually",
    "बेसिकली": "basically", "लिटरली": "literally", "वेट": "wait",
    "ओएमजी": "omg", "ट्राई": "try", "किल": "kill", "बॉस": "boss", "डेड": "dead",
}

_VOWELS = {  # independent vowel: (short, long) -- see _vowel()
    "अ": "a", "आ": "aa", "इ": "i", "ई": "ee", "उ": "u", "ऊ": "oo", "ऋ": "ri",
    "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au", "ऑ": "o", "ऍ": "e",
}
_MATRAS = {
    "ा": "aa", "ि": "i", "ी": "ee", "ु": "u", "ू": "oo",
    "ृ": "ri", "े": "e", "ै": "ai", "ो": "o", "ौ": "au",
    "ॉ": "o", "ॅ": "e",
}
_CONSONANTS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n", "च": "ch", "छ": "chh",
    "ज": "j", "झ": "jh", "ञ": "n", "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh",
    "ण": "n", "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n", "प": "p",
    "फ": "ph", "ब": "b", "भ": "bh", "म": "m", "य": "y", "र": "r", "ल": "l",
    "व": "v", "श": "sh", "ष": "sh", "स": "s", "ह": "h", "ळ": "l",
    # Nukta forms, precomposed.
    "क़": "q", "ख़": "kh", "ग़": "gh", "ज़": "z", "ड़": "d", "ढ़": "dh",
    "फ़": "f", "य़": "y",
}
# The same with the nukta written as its own mark.
_NUKTA = {"क": "q", "ख": "kh", "ग": "gh", "ज": "z", "ड": "d", "ढ": "dh",
          "फ": "f", "य": "y"}
_LABIALS = {"p", "ph", "b", "bh", "m", "f"}
_DIGITS = {chr(0x0966 + i): str(i) for i in range(10)}

_VIRAMA, _NUKTA_SIGN = "्", "़"
_ANUSVARA, _CHANDRABINDU, _VISARGA = "ं", "ँ", "ः"


def has_devanagari(text: str) -> bool:
    return bool(_DEVANAGARI.search(text or ""))


def _units(word: str) -> List[Dict]:
    """The word as consonants and vowels: [{c, v, nasal}].

    `c` is the consonant ("" for a vowel standing alone), `v` its vowel --
    "a" for the inherent one nobody writes, None when a virama joins it to
    the next consonant -- and `nasal` whether an anusvara or chandrabindu
    follows it.
    """
    units: List[Dict] = []
    i = 0
    while i < len(word):
        ch = word[i]
        if ch in _CONSONANTS:
            c = _CONSONANTS[ch]
            if i + 1 < len(word) and word[i + 1] == _NUKTA_SIGN:
                c = _NUKTA.get(ch, c)
                i += 1
            units.append({"c": c, "v": "a", "inherent": True, "nasal": False,
                          "raw": ch})
        elif ch in _VOWELS:
            units.append({"c": "", "v": _VOWELS[ch], "inherent": False,
                          "nasal": False, "raw": ch})
        elif ch in _MATRAS and units:
            units[-1]["v"], units[-1]["inherent"] = _MATRAS[ch], False
        elif ch == _VIRAMA and units:
            units[-1]["v"], units[-1]["inherent"] = None, False
        elif ch in (_ANUSVARA, _CHANDRABINDU) and units:
            units[-1]["nasal"] = True
        elif ch == _VISARGA and units:
            units[-1]["visarga"] = True
        else:
            units.append({"c": _DIGITS.get(ch, ch), "v": None, "inherent": False,
                          "nasal": False, "raw": ch, "other": True})
        i += 1
    return units


def _delete_schwas(units: List[Dict]) -> None:
    """Drop the inherent vowels Hindi does not say, in place.

    The last one goes unless it ends a conjunct into र, य or व (मित्र is
    "mitra", सत्य "satya", but दोस्त is "dost"), and a
    medial one goes between a vowel and a consonant that has one of its own
    -- worked right to left, so करना loses the one after र and समझना the
    one after म but not both. A syllable closed by a nasal keeps the next
    one: ज़िंदगी is "zindagi".
    """
    cons = [u for u in units if not u.get("other")]
    if len(cons) < 2:
        return
    last = cons[-1]
    if last["inherent"] and last["c"] and not last["nasal"]:
        prev = cons[-2]
        if prev["v"] is not None or last["c"] not in ("r", "y", "v"):
            last["v"] = None
    for i in range(len(cons) - 2, 0, -1):
        u, before, after = cons[i], cons[i - 1], cons[i + 1]
        if (u["inherent"] and u["c"] and not u["nasal"]
                and before["v"] is not None and not before["nasal"]
                and after["c"] and after["v"] is not None):
            u["v"] = None


def _vowel(v: str, final: bool, short: bool, nasal: bool) -> str:
    """Long vowels as they are casually typed.

    Doubled ("aa", "ee", "oo") only in a one-syllable word that does not end
    on the vowel -- "baat", "theek", "bhool", "haan" -- and single
    everywhere else: "pani", "jaldi", "khana", "tha".
    """
    if v in ("aa", "ee", "oo") and (short or (final and not nasal)):
        return v[0] if v != "ee" else "i"
    return v


def romanize(word: str) -> str:
    """One word of Devanagari in Hinglish. Anything else comes back as it was."""
    if not has_devanagari(word):
        return word
    word = word.replace("॥", ".").replace("।", ".")
    m = re.match(r"^([^\w\u0900-\u097F]*)(.*?)([^\w\u0900-\u097F]*)$", word, re.S)
    lead, core, trail = (m.group(1), m.group(2), m.group(3)) if m else ("", word, "")
    if core in _COMMON:
        return lead + _COMMON[core] + trail

    units = _units(core)
    _delete_schwas(units)
    syllables = sum(1 for u in units if u["v"])
    out: List[str] = []
    for i, u in enumerate(units):
        if u.get("other"):
            out.append(u["c"])
            continue
        nxt = units[i + 1] if i + 1 < len(units) else None
        c = u["c"]
        # च before a joined च or छ is the "c" of "accha", "baccha".
        if c == "ch" and u["v"] is None and nxt and nxt["c"] in ("ch", "chh"):
            c = "c"
        if c == "chh" and i > 0 and units[i - 1]["c"] in ("ch", "c"):
            c = "ch"
        # व is "w" after a consonant -- "swagat", "dwara" -- and "v" elsewhere.
        if c == "v" and i > 0 and units[i - 1]["v"] is None and units[i - 1]["c"]:
            c = "w"
        out.append(c)
        if u["v"]:
            final = nxt is None or nxt.get("other")
            out.append(_vowel(u["v"], final, syllables > 1 and u["c"] != "", u["nasal"]))
        # Silent before a nasal consonant -- मैंने is "maine", not "mainne".
        if u["nasal"] and not (nxt and nxt["c"] in ("n", "m")):
            out.append("m" if nxt and nxt["c"] in _LABIALS else "n")
        if u.get("visarga"):
            out.append("h")
    return lead + "".join(out) + trail


def _ask(words: List[str], llm_fn: Callable[[str], str]) -> Optional[List[str]]:
    """The LLM's Hinglish for `words`, one for one, or None if it fell short."""
    lines = "\n".join(f"{i + 1}. {w}" for i, w in enumerate(words))
    prompt = (
        "These are the words of a spoken Hindi/Hinglish clip, in order, as a "
        "speech-to-text model wrote them in Devanagari. They become burned-in "
        "captions on a YouTube Short.\n\n"
        "Rewrite every word in Hinglish: Roman script, spelled the casual way "
        "Indians type Hindi in chats and YouTube comments (kya, nahi, yaar, "
        "accha, bhai, matlab). Any English word that was spoken in Hindi script "
        "is written as the English word (वीडियो -> video, गेम -> game). Words "
        "already in Roman script stay as they are. Keep each word's punctuation. "
        "Do not translate, merge, split, add or drop words.\n\n"
        f"Return only a JSON array of exactly {len(words)} strings, one per "
        f"word, in the same order.\n\n{lines}"
    )
    raw = (llm_fn(prompt) or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end == -1:
        return None
    got = json.loads(raw[start:end + 1])
    if not isinstance(got, list) or len(got) != len(words):
        return None
    return [str(g).strip() if isinstance(g, (str, int, float)) else "" for g in got]


def words_to_hinglish(words: List[Dict],
                      llm_fn: Optional[Callable[[str], str]] = None) -> List[Dict]:
    """`words` ([{start, end, word, ...}]) with any Devanagari in Hinglish.

    Same length, same order, same timings; only `word` changes. Never raises
    -- a caption in the wrong script is better than no clip.
    """
    todo = [i for i, w in enumerate(words) if has_devanagari(str(w.get("word", "")))]
    if not todo:
        return words
    out = [dict(w) for w in words]
    answer: Optional[List[str]] = None
    if llm_fn is not None:
        try:
            answer = _ask([str(words[i]["word"]) for i in todo], llm_fn)
        except Exception as e:  # noqa: BLE001 - the rules still work
            print(f"[captions] Hinglish from the LLM failed "
                  f"({str(e).splitlines()[0][:120] if str(e) else e.__class__.__name__})"
                  f" - spelling it by rule", flush=True)
    by_rule = 0
    for k, i in enumerate(todo):
        said = answer[k] if answer else ""
        if not said or has_devanagari(said):
            said = romanize(str(words[i]["word"]))
            by_rule += 1
        out[i]["word"] = said
    how = "by rule" if by_rule == len(todo) else (
        "with the LLM" if not by_rule else f"with the LLM, {by_rule} by rule")
    print(f"[captions] {len(todo)} Hindi word(s) written in Hinglish {how}", flush=True)
    return out
