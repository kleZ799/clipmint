"""Local LLM backend — Gemini, Groq, OpenRouter or OpenAI, selected by LLM_PROVIDER."""
import re
import time
from typing import Dict, List, Optional, Set, Tuple

from .. import usage
from ..config import (
    current_model,
    current_provider,
    gemini_keys,
    require_gemini_key,
    require_groq_key,
    require_openai_key,
    require_openrouter_key,
)

PROVIDERS = ("gemini", "groq", "openrouter", "openai")

# Models whose name says they are not for ranking text.
_NON_TEXT = ("image", "tts", "embed", "robotics", "computer-use", "transcribe",
             "omni", "audio", "veo", "imagen")

# ListModels is a different quota from generateContent, but it is still a
# network round trip on a settings load, so it is cached for the process.
_model_cache: Tuple[float, List[str]] = (0.0, [])
_MODEL_CACHE_SECONDS = 600


def list_gemini_models(force: bool = False) -> List[str]:
    """Text models this key can actually call, newest-looking first.

    Asked of the API rather than hardcoded, because Google retires models:
    `gemini-2.0-flash` was in a hand-written list here and had already been
    withdrawn, so offering it produced a 404 mid-run. A list that comes from
    the account itself cannot drift out of date.
    """
    global _model_cache
    age, cached = _model_cache
    if cached and not force and (time.time() - age) < _MODEL_CACHE_SECONDS:
        return cached

    try:
        from google import genai  # type: ignore
        client = genai.Client(api_key=require_gemini_key())
        names = []
        for m in client.models.list():
            name = str(m.name or "").replace("models/", "")
            if not name.startswith("gemini"):
                continue
            if "generateContent" not in (m.supported_actions or []):
                continue
            if any(bad in name for bad in _NON_TEXT):
                continue
            names.append(name)
    except Exception as e:
        print(f"[llm] could not list models ({e}); using the built-in list", flush=True)
        return []

    names.sort()
    _model_cache = (time.time(), names)
    return names


def check_gemini_model(model: str) -> Optional[str]:
    """Try the model once. Returns None if it works, else why it does not.

    ListModels is not proof of anything: it happily returns models that answer
    "no longer available to new users" when you actually call them, and that
    404 used to surface nine chunks into a run. One tiny request at the moment
    of choosing is far cheaper than discovering it later.
    """
    try:
        from google import genai  # type: ignore
        client = genai.Client(api_key=require_gemini_key())
        client.models.generate_content(model=model, contents="hi",
                                       config={"max_output_tokens": 1})
        return None
    except Exception as e:
        msg = str(e)
        if "429" in msg:
            return None     # it exists and answers; today's allowance is just spent
        if "no longer available" in msg or "404" in msg:
            return f"{model} is not available on this API key."
        if "503" in msg:
            return f"{model} is temporarily unavailable — try another."
        return f"{model} could not be used: {msg.splitlines()[0][:120]}"


class DailyQuotaExceeded(RuntimeError):
    """The provider's per-day allowance is gone. Waiting will not help."""


def call_openai_llm(prompt: str) -> str:
    """OpenAI Chat Completions backend used by --mode local."""
    try:
        from openai import OpenAI  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "openai is required for --mode local. Install it with:\n"
            "    pip install -r requirements-local.txt"
        ) from e

    model = current_model("openai")
    client = OpenAI(api_key=require_openai_key())
    response = client.chat.completions.create(
        model=model,
        temperature=0.7,
        messages=[{"role": "user", "content": prompt}],
    )
    usage.record("openai", model)
    return response.choices[0].message.content or ""


GROQ_BASE_URL = "https://api.groq.com/openai/v1"


def call_groq_llm(prompt: str) -> str:
    """Groq backend: open-weight models on Groq's own hardware, free tier.

    Groq speaks the OpenAI Chat Completions API, so this reuses the `openai`
    client with a different base_url rather than hand-rolling HTTP. No new
    dependency, no second response parser to keep correct.

    The point of this provider is not that it is better than Gemini -- on the
    ranking task it is roughly comparable and on paper slightly behind. The
    point is that it is *somewhere else*. When Google returns 503 because
    Google is busy, no amount of retrying Google helps, and Groq's capacity
    has nothing to do with Google's.

    Free-tier limits worth knowing, because they shape what fits: 30 requests
    per minute, 1000 per day, and 8000 tokens per minute. That last one binds
    first -- one ranking chunk is roughly 6-7k tokens in and out together, so
    this runs at about one chunk a minute.
    """
    try:
        from openai import OpenAI  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "openai is required to talk to Groq (it speaks the same API). "
            "Install it with:\n    pip install -r requirements-local.txt"
        ) from e

    model = current_model("groq")
    client = OpenAI(api_key=require_groq_key(), base_url=GROQ_BASE_URL)
    response = client.chat.completions.create(
        model=model,
        temperature=0.2,          # same as Gemini's: judgement, not invention
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompt}],
    )
    usage.record("groq", model)
    return response.choices[0].message.content or ""


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# OpenRouter shows these on its own dashboard, so a user can see which of
# their requests came from this app.
_OPENROUTER_HEADERS = {"X-Title": "ClipMint",
                       "HTTP-Referer": "https://klez799.github.io/clipmint/"}


def _openrouter_client():
    from openai import OpenAI  # type: ignore
    return OpenAI(api_key=require_openrouter_key(), base_url=OPENROUTER_BASE_URL,
                  default_headers=_OPENROUTER_HEADERS)


def call_openrouter_llm(prompt: str) -> str:
    """OpenRouter backend: one key in front of models from every lab.

    Same Chat Completions API as OpenAI and Groq, so the same client. No
    response_format here: the free router hands the request to whichever free
    model is up, and not all of them accept a JSON mode. Every caller already
    pulls the JSON object out of plain text, as it does for OpenAI.
    """
    try:
        import openai  # noqa: F401  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "openai is required to talk to OpenRouter (it speaks the same API). "
            "Install it with:\n    pip install -r requirements-local.txt"
        ) from e

    model = current_model("openrouter")
    response = _openrouter_client().chat.completions.create(
        model=model,
        temperature=0.2,
        messages=[{"role": "user", "content": prompt}],
    )
    usage.record("openrouter", model)
    text = response.choices[0].message.content or ""
    if not text.strip():
        raise RuntimeError(f"OpenRouter ({model}) returned no text")
    return text


def _is_daily_quota(msg: str) -> bool:
    """Tell a per-day cap apart from a per-minute one.

    They arrive as the same 429. The per-minute one clears in a minute and is
    worth sleeping through; the per-day one does not clear until midnight
    Pacific, so sleeping on it just wastes five minutes before failing anyway.
    Google names the quota in the payload -- GenerateRequestsPerDayPerProject
    -- which is the only reliable way to tell them apart.
    """
    lowered = msg.lower()
    return "perday" in lowered.replace("_", "") or "per day" in lowered


def call_gemini_llm(prompt: str, contents: Optional[list] = None) -> str:
    """Gemini backend used by --mode local when LLM_PROVIDER=gemini.

    `contents`, when given, is sent instead of `prompt` -- a list of text and
    image parts, for the calls that have to look at frames.
    """
    try:
        from google import genai  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "google-genai is required for LLM_PROVIDER=gemini. Install it with:\n"
            "    pip install -r requirements-local.txt"
        ) from e

    model = current_model("gemini")
    keys = gemini_keys() or [require_gemini_key()]
    while True:
        index = _first_live_key(keys, model)
        try:
            return _call_gemini_with(genai.Client(api_key=keys[index]), model,
                                     prompt, contents)
        except _KeySpent as spent:
            _spent_keys.add(_key_mark(keys[index], model))
            if _first_live_key(keys, model, strict=True) is None:
                if spent.daily:
                    usage.mark_exhausted("gemini", model)
                    raise DailyQuotaExceeded(
                        f"Gemini's free-tier daily quota for {model} is used up"
                        + (f" on all {len(keys)} keys" if len(keys) > 1 else "")
                        + ". It resets at midnight US Pacific."
                    ) from spent.__cause__
                raise spent.__cause__
            nxt = _first_live_key(keys, model)
            print(f"[llm] Gemini key {index + 1} of {len(keys)} {spent.why} — "
                  f"trying key {nxt + 1}", flush=True)


# Extra keys are spares for the main one. A key that is spent for today, or
# that Google refuses outright, is passed over for the rest of the day; the
# mark carries the day so a run left open overnight starts on key 1 again.
_spent_keys: Set[str] = set()


def _key_mark(key: str, model: str) -> str:
    return f"{usage.quota_day()}|{model}|{key[-8:]}"


def _first_live_key(keys: List[str], model: str, strict: bool = False) -> Optional[int]:
    """The first key not passed over today. With nothing left, the last key
    (strict=False, so a call still gets made and reports the real error) or
    None (strict=True, so the caller knows to stop)."""
    for i, k in enumerate(keys):
        if _key_mark(k, model) not in _spent_keys:
            return i
    return None if strict else len(keys) - 1


class _KeySpent(Exception):
    """This key cannot be used again today; another one might."""

    def __init__(self, why: str, daily: bool):
        super().__init__(why)
        self.why, self.daily = why, daily


def _call_gemini_with(client, model: str, prompt: str, contents: Optional[list]) -> str:
    # Gemini 3.x spends part of the output budget on internal reasoning before
    # emitting any JSON, so 8192 truncates long-chunk responses mid-object.
    config = {
        "temperature": 0.2,
        "response_mime_type": "application/json",
        "max_output_tokens": 32768,
    }

    # By the time we get here the run has already paid for a download and a
    # transcription, so a blip must not sink it. Gemini fails transiently in
    # several ways — free-tier rate limits, capacity spikes, and plain network
    # timeouts — and none of them read the same in the error string. So the
    # rule is inverted: give up immediately only on errors that retrying can
    # never fix (bad key, bad request, a spent daily allowance), and retry
    # everything else.
    #
    # Eight, not five: five backs off 5+10+20+40s and gives up after barely a
    # minute, which is shorter than a Gemini capacity spike routinely lasts.
    # Losing a chunk that late costs the whole ranking pass on a long video,
    # because a chunk is only checkpointed once it succeeds. The extra three
    # attempts sit at the 60s cap, so patience runs to about four minutes.
    attempts = 8
    last_error = None
    for attempt in range(attempts):
        try:
            response = client.models.generate_content(
                model=model, contents=contents if contents is not None else prompt,
                config=config,
            )
            break
        except Exception as e:
            last_error = e
            msg = str(e)

            if _is_daily_quota(msg):
                raise _KeySpent("has used today's free quota", daily=True) from e

            # A refused key is that key's problem, so another key may work.
            # A bad request or a missing model would fail on every key alike.
            if any(t in msg for t in ("API_KEY_INVALID", "API key not valid",
                                      "PERMISSION_DENIED", "UNAUTHENTICATED",
                                      "401", "403")):
                raise _KeySpent("was refused by Google", daily=False) from e

            permanent = any(t in msg for t in ("INVALID_ARGUMENT", "NOT_FOUND", "404"))
            if permanent or attempt == attempts - 1:
                raise

            m = re.search(r"retry in ([0-9.]+)s", msg)
            if m:
                # The rate limiter told us exactly how long to wait.
                delay, reason = float(m.group(1)) + 1, "rate limited"
            else:
                delay = min(60.0, 5.0 * (2 ** attempt))
                reason = "transient error"

            short = msg.splitlines()[0][:120]
            print(f"[llm] {reason}; retrying in {delay:.0f}s "
                  f"(attempt {attempt + 1}/{attempts}) — {short}", flush=True)
            time.sleep(delay)
    else:
        raise last_error

    usage.record("gemini", model)

    text = response.text or ""
    if not text.strip():
        # Surface *why* it came back empty instead of failing as "invalid JSON".
        reason = "unknown"
        try:
            cand = (response.candidates or [None])[0]
            reason = str(getattr(cand, "finish_reason", "unknown"))
        except Exception:
            pass
        raise RuntimeError(f"Gemini returned no text (finish_reason={reason})")
    return text


# Once a run has switched providers there is no point asking the spent one
# again on every remaining chunk, so the choice sticks for the process.
_fallback_provider: Optional[str] = None
_given_up: Set[str] = set()


def _openai_is_configured() -> bool:
    try:
        return bool(require_openai_key())
    except RuntimeError:
        return False


def _groq_is_configured() -> bool:
    try:
        return bool(require_groq_key())
    except RuntimeError:
        return False


def _openrouter_is_configured() -> bool:
    try:
        return bool(require_openrouter_key())
    except RuntimeError:
        return False


# Who to try when the preferred provider gives up, in order. The free ones
# before OpenAI, because OpenAI has no free tier: falling back should not
# quietly start spending money. OpenRouter after Groq because its default is
# the free router, which is slower and less predictable than Groq's own model.
_LADDER = (
    ("groq", _groq_is_configured),
    ("openrouter", _openrouter_is_configured),
    ("openai", _openai_is_configured),
)


def _switch_to(provider: str, why: str) -> None:
    global _fallback_provider
    _fallback_provider = provider
    print(f"[llm] {why} — continuing on {provider} "
          f"({current_model(provider)})", flush=True)


def _next_provider(why: str) -> Optional[str]:
    """Move to the first configured fallback, or None if there is none."""
    # Never back to one this run already gave up on, or two failing free
    # tiers would hand the run back and forth for ever.
    _given_up.add(_fallback_provider or current_provider())
    for name, configured in _LADDER:
        if name not in _given_up and configured():
            _switch_to(name, why)
            return name
    return None


def _switch_to_openai(why: str) -> None:
    # Kept as the old name for callers outside this module.
    _switch_to("openai", why)


# Groq's text model cannot see. This one can, and takes at most five images
# per request, which is why the vision batch size asks who it is talking to.
GROQ_VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
# OpenRouter's limit is whichever model the request lands on; five is what the
# smallest of the free vision models take.
_IMAGES_PER_REQUEST = {"gemini": 20, "openai": 20, "groq": 5, "openrouter": 5}


def _as_gemini_parts(parts: list) -> list:
    from google.genai import types  # type: ignore
    return [p if isinstance(p, str) else types.Part.from_bytes(data=p[0], mime_type=p[1])
            for p in parts]


def _as_openai_content(parts: list) -> list:
    import base64
    out = []
    for p in parts:
        if isinstance(p, str):
            out.append({"type": "text", "text": p})
        else:
            data = base64.b64encode(p[0]).decode("ascii")
            out.append({"type": "image_url", "image_url": {"url": f"data:{p[1]};base64,{data}"}})
    return out


def _openai_compatible_vision(parts: list, provider: str) -> str:
    from openai import OpenAI  # type: ignore
    if provider == "groq":
        client, model = OpenAI(api_key=require_groq_key(), base_url=GROQ_BASE_URL), GROQ_VISION_MODEL
    elif provider == "openrouter":
        client, model = _openrouter_client(), current_model("openrouter")
    else:
        client, model = OpenAI(api_key=require_openai_key()), current_model("openai")
    response = client.chat.completions.create(
        model=model,
        temperature=0.1,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": _as_openai_content(parts)}],
    )
    usage.record(provider, model)
    return response.choices[0].message.content or ""


def _gemini_is_configured() -> bool:
    try:
        return bool(require_gemini_key()) and not _gemini_spent_today()
    except RuntimeError:
        return False


def _gemini_spent_today() -> bool:
    """Whether today's Gemini quota is known to be gone on every key.

    The ledger's mark is per model, not per key. With spare keys it may have
    been written before a spare was added, so trust it only with one key; with
    several, a spent key answers its 429 at once and the next one is tried.
    """
    if not usage.is_exhausted("gemini", current_model("gemini")):
        return False
    return len(gemini_keys()) <= 1


def vision_images_per_request() -> int:
    """How many frames one vision request may carry on the provider in use."""
    return _IMAGES_PER_REQUEST.get(_fallback_provider or current_provider(), 5)


def call_vision_llm(parts: list) -> str:
    """Ask a model that can see about text and images together.

    `parts` is a list of strings and (bytes, mime type) images, in order. The
    provider in use is asked first, then any other configured one -- looking
    at a clip is worth one failover, since the alternative is a title written
    blind -- and a provider that cannot take this many images is skipped.
    """
    images = sum(1 for p in parts if not isinstance(p, str))
    first = _fallback_provider or current_provider()
    order = [first] + [p for p in ("gemini", "openai", "groq", "openrouter") if p != first]
    configured = {"gemini": _gemini_is_configured, "openai": _openai_is_configured,
                  "groq": _groq_is_configured, "openrouter": _openrouter_is_configured}
    last: Optional[Exception] = None
    for name in order:
        if not configured.get(name, lambda: False)():
            continue
        if images > _IMAGES_PER_REQUEST[name]:
            continue
        try:
            if name == "gemini":
                return call_gemini_llm("", contents=_as_gemini_parts(parts))
            return _openai_compatible_vision(parts, name)
        except Exception as e:
            last = e
            print(f"[vision] {name} could not look at the frames "
                  f"({str(e).splitlines()[0][:100]})", flush=True)
    raise last or RuntimeError("no provider that can look at images is configured")


def reset_fallback() -> None:
    """Forget a previous switch, so a new run re-checks the preferred provider."""
    global _fallback_provider
    _fallback_provider = None
    _given_up.clear()


def call_local_llm(prompt: str) -> str:
    """Dispatch to the configured local LLM provider.

    When Gemini stops answering mid-run, hand the rest of the run to whoever
    else is configured rather than losing a download and a transcription.
    There are two distinct ways it stops, and they need different handling:

    - **Quota (429).** The allowance is gone until it resets. Retrying is
      pointless, so switch immediately.
    - **Capacity (503).** Google is busy. `call_gemini_llm` already retries
      this for about four minutes, and if it is *still* refusing after that,
      more retries against the same busy service will not help either. A
      different provider will, because its capacity is unrelated.

    That second case is the one that matters. A real run reached chunk 9 of 12
    with an 11.9GB download and a 29-minute transcription already paid for,
    and came within one attempt of losing all of it to a capacity spike.
    """
    provider = _fallback_provider or current_provider()

    if provider == "openai":
        return call_openai_llm(prompt)
    if provider in ("groq", "openrouter"):
        call = call_groq_llm if provider == "groq" else call_openrouter_llm
        try:
            return call(prompt)
        except Exception as e:
            # The free tiers are the likeliest to be busy or rate limited, and
            # a run that has paid for a transcription should not end on that.
            if not _next_provider(f"{provider} failed ({str(e).splitlines()[0][:60]})"):
                raise
            return call_local_llm(prompt)
    if provider != "gemini":
        raise RuntimeError(
            f"Unknown LLM_PROVIDER={provider!r}. Use 'gemini', 'groq', "
            f"'openrouter' or 'openai'."
        )

    if _gemini_spent_today():
        if _next_provider("today's Gemini quota is already spent"):
            return call_local_llm(prompt)
        raise DailyQuotaExceeded(
            "Gemini's free-tier daily quota is already used up for today. It "
            "resets at midnight US Pacific — or add a free Groq key "
            "(https://console.groq.com) to keep going."
        )

    try:
        return call_gemini_llm(prompt)
    except DailyQuotaExceeded:
        if not _next_provider("Gemini's daily quota ran out"):
            raise
        return call_local_llm(prompt)
    except Exception as e:
        # Everything else reaching here has already survived the full retry
        # budget inside call_gemini_llm, so it is not a blip. Permanent errors
        # -- a bad key, a withdrawn model -- would fail on another provider
        # too, but failing over costs one request and losing the run costs
        # half an hour, so the trade is worth making either way.
        if not _next_provider(f"Gemini kept failing ({str(e).splitlines()[0][:60]})"):
            raise
        return call_local_llm(prompt)
