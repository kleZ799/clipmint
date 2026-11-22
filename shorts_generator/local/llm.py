"""Local LLM backend — Gemini, Groq, OpenRouter, OpenAI, Claude or Ollama.

Every configured key across every provider forms a pool of independent slots.
Rate-limit and transient cooldowns are tracked PER KEY, never globally:

- When key A is rate-limited or errors, key A alone enters its cooldown and the
  scheduler immediately (zero delay) tries key B or the next provider.
- Delay only applies before re-using the SAME key, and only when no other
  ready key is available right now.
- Parallel workers claim distinct idle keys safely under `_pool_lock`, so
  chunk requests never pile onto key #1.
- When `AI_POOL_ENABLED=1`, all saved keys across all providers are tried in the
  user's saved priority order (`AI_POOL_ORDER`).
"""
import hashlib
import json
import re
import threading
import time
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from .. import proc, usage, user_config
from ..version import SITE_URL
from ..config import (
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    PROVIDER_LABELS,
    current_model,
    current_provider,
    gemini_keys,
    misplaced_key,
    require_anthropic_key,
    require_gemini_key,
    require_groq_key,
    require_openai_key,
    require_openrouter_key,
)

PROVIDERS = ("gemini", "groq", "openrouter", "openai", "anthropic", "ollama")

# Default fallback order when AI Pool custom priority is off or for unranked keys.
_DEFAULT_LADDER = ("gemini", "groq", "openrouter", "ollama", "openai", "anthropic")

# OpenRouter model IDs known to support vision (image) input.
# "openrouter/free" and "openrouter/auto" may route to text-only models,
# so they are excluded — a 404 there is permanent, not transient.
OPENROUTER_VISION_MODELS = {
    "google/gemma-4-31b-it:free",
    "google/gemma-3-27b-it:free",
    "meta-llama/llama-4-scout:free",
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "openai/gpt-4o",
    "openai/gpt-4o-mini",
    "anthropic/claude-3.5-sonnet",
    "anthropic/claude-3-haiku",
    "google/gemini-flash-1.5",
    "google/gemini-pro-1.5",
}

# Models whose name says they are not for ranking text.
_NON_TEXT = ("image", "tts", "embed", "robotics", "computer-use", "transcribe",
             "omni", "audio", "veo", "imagen")

_TEXT_TIMEOUT_SEC = 90.0
_VISION_TIMEOUT_SEC = 60.0
_META_TIMEOUT_SEC = 15.0

_model_cache: Tuple[float, List[str]] = (0.0, [])
_MODEL_CACHE_SECONDS = 600


def _gemini_client(api_key: str, timeout_sec: float = _TEXT_TIMEOUT_SEC):
    """Build a Gemini client with finite timeout and SDK retries disabled."""
    try:
        from google import genai  # type: ignore
        from google.genai import types  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "google-genai is required for LLM_PROVIDER=gemini. Install it with:\n"
            "    pip install -r requirements-local.txt"
        ) from e

    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=max(1000, int(timeout_sec * 1000)),
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )


def list_gemini_models(force: bool = False) -> List[str]:
    """Text models this key can actually call, newest-looking first."""
    global _model_cache
    age, cached = _model_cache
    if cached and not force and (time.time() - age) < _MODEL_CACHE_SECONDS:
        return cached

    client = None
    try:
        client = _gemini_client(require_gemini_key(), _META_TIMEOUT_SEC)
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
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    names.sort()
    _model_cache = (time.time(), names)
    return names


def check_gemini_model(model: str) -> Optional[str]:
    """Try the model once. Returns None if it works, else why it does not."""
    client = None
    try:
        client = _gemini_client(require_gemini_key(), _META_TIMEOUT_SEC)
        client.models.generate_content(model=model, contents="hi",
                                       config={"max_output_tokens": 1})
        return None
    except Exception as e:
        msg = str(e)
        if "429" in msg:
            return None
        if "no longer available" in msg or "404" in msg:
            return f"{model} is not available on this API key."
        if "503" in msg:
            return f"{model} is temporarily unavailable — try another."
        return f"{model} could not be used: {msg.splitlines()[0][:120]}"
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


class DailyQuotaExceeded(RuntimeError):
    """The provider's per-day allowance is gone. Waiting will not help."""


# ---------------------------------------------------------------------------
# Provider clients & single-attempt callers (no internal retry sleeps!)
# ---------------------------------------------------------------------------

def _openai_client(api_key: Optional[str] = None, timeout: float = _TEXT_TIMEOUT_SEC):
    """Build an OpenAI client, honouring any custom base URL.

    If a key is provided/stored, it is always used. When a custom base URL is
    set (e.g. LM Studio) without a key, a non-empty placeholder lets the SDK
    construct cleanly while sending no secret.
    """
    from openai import OpenAI  # type: ignore
    base_url = (user_config.get("OPENAI_BASE_URL", OPENAI_BASE_URL) or "").strip() or None
    key = (api_key if api_key is not None else user_config.get("OPENAI_API_KEY", OPENAI_API_KEY)).strip()
    if not key:
        if base_url:
            key = "none"
        else:
            key = require_openai_key()
    kwargs: dict = {"api_key": key, "timeout": timeout, "max_retries": 0}
    if base_url:
        kwargs["base_url"] = base_url
    return OpenAI(**kwargs)


def call_openai_llm(prompt: str, api_key: Optional[str] = None) -> str:
    """OpenAI Chat Completions backend used by --mode local."""
    try:
        import openai  # noqa: F401  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "openai is required for --mode local. Install it with:\n"
            "    pip install -r requirements-local.txt"
        ) from e

    model = current_model("openai")
    client = _openai_client(api_key=api_key, timeout=_TEXT_TIMEOUT_SEC)
    try:
        response = client.chat.completions.create(
            model=model,
            temperature=0.7,
            messages=[{"role": "user", "content": prompt}],
        )
    finally:
        client.close()
    usage.record("openai", model)
    return response.choices[0].message.content or ""


GROQ_BASE_URL = "https://api.groq.com/openai/v1"


def call_groq_llm(prompt: str, api_key: Optional[str] = None) -> str:
    """Groq backend: open-weight models on Groq's own hardware, free tier."""
    try:
        from openai import OpenAI  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "openai is required to talk to Groq (it speaks the same API). "
            "Install it with:\n    pip install -r requirements-local.txt"
        ) from e

    model = current_model("groq")
    key = (api_key if api_key is not None else require_groq_key()).strip()
    client = OpenAI(
        api_key=key,
        base_url=GROQ_BASE_URL,
        timeout=_TEXT_TIMEOUT_SEC,
        max_retries=0,
    )
    try:
        response = client.chat.completions.create(
            model=model,
            temperature=0.2,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
        )
    finally:
        client.close()
    usage.record("groq", model)
    return response.choices[0].message.content or ""


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_OPENROUTER_HEADERS = {"X-Title": "ClipMint", "HTTP-Referer": SITE_URL}


def _openrouter_client(api_key: Optional[str] = None, timeout: float = _TEXT_TIMEOUT_SEC):
    from openai import OpenAI  # type: ignore
    key = (api_key if api_key is not None else require_openrouter_key()).strip()
    return OpenAI(
        api_key=key,
        base_url=OPENROUTER_BASE_URL,
        default_headers=_OPENROUTER_HEADERS,
        timeout=timeout,
        max_retries=0,
    )


def call_openrouter_llm(prompt: str, api_key: Optional[str] = None) -> str:
    """OpenRouter backend: one key in front of models from every lab."""
    try:
        import openai  # noqa: F401  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "openai is required to talk to OpenRouter (it speaks the same API). "
            "Install it with:\n    pip install -r requirements-local.txt"
        ) from e

    model = current_model("openrouter")
    client = _openrouter_client(api_key=api_key, timeout=_TEXT_TIMEOUT_SEC)
    try:
        response = client.chat.completions.create(
            model=model,
            temperature=0.2,
            messages=[{"role": "user", "content": prompt}],
        )
    finally:
        client.close()
    usage.record("openrouter", model)
    text = response.choices[0].message.content or ""
    if not text.strip():
        raise RuntimeError(f"OpenRouter ({model}) returned no text")
    return text


_OLLAMA_PROBE_SECONDS = 3
_OLLAMA_CALL_SECONDS = 30 * 60
_ollama_seen: Tuple[float, bool] = (0.0, False)
_ollama_caps: Dict[str, List[str]] = {}


class OllamaError(RuntimeError):
    pass


def _ollama(path: str, payload: Optional[dict] = None,
            timeout: float = _OLLAMA_PROBE_SECONDS) -> dict:
    import requests
    from ..config import ollama_host

    url = ollama_host() + path
    try:
        if payload is None:
            r = requests.get(url, timeout=timeout)
        else:
            r = requests.post(url, json=payload, timeout=timeout)
    except requests.RequestException as e:
        raise OllamaError(
            f"Ollama isn't answering at {ollama_host()}. Start it (the Ollama app, "
            f"or `ollama serve`), or install it from https://ollama.com/download."
        ) from e
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code >= 400:
        raise OllamaError(f"Ollama: {body.get('error') or r.text[:200] or r.status_code}")
    return body


def list_ollama_models() -> List[dict]:
    """What has been pulled into this Ollama, biggest first, with what each can do."""
    out = []
    for m in _ollama("/api/tags").get("models") or []:
        name = m.get("name") or m.get("model")
        if not name:
            continue
        caps = ollama_capabilities(name)
        if caps and "completion" not in caps:
            continue
        details = m.get("details") or {}
        out.append({"value": name, "size": int(m.get("size") or 0),
                    "params": details.get("parameter_size") or "",
                    "vision": "vision" in caps})
    out.sort(key=lambda m: -m["size"])
    return out


def ollama_capabilities(model: str) -> List[str]:
    """completion / vision / tools / thinking, as Ollama reports them."""
    if model not in _ollama_caps:
        try:
            _ollama_caps[model] = list(_ollama("/api/show", {"model": model})
                                       .get("capabilities") or [])
        except OllamaError:
            return []
    return _ollama_caps[model]


def _ollama_chat(model: str, messages: List[dict], temperature: float = 0.2) -> str:
    from ..config import OLLAMA_NUM_CTX

    body = _ollama("/api/chat", {
        "model": model,
        "messages": messages,
        "stream": False,
        "format": "json",
        "options": {"temperature": temperature, "num_ctx": OLLAMA_NUM_CTX},
    }, timeout=_OLLAMA_CALL_SECONDS)
    usage.record("ollama", model)
    text = (body.get("message") or {}).get("content") or ""
    if not text.strip():
        raise RuntimeError(f"Ollama ({model}) returned no text")
    return text


def call_ollama_llm(prompt: str) -> str:
    """Ollama backend: an open model on this computer."""
    model = current_model("ollama")
    if not model:
        raise RuntimeError("No Ollama model is chosen. Pick one in Settings, "
                           "or set OLLAMA_MODEL.")
    return _ollama_chat(model, [{"role": "user", "content": prompt}])


def _ollama_vision_model() -> Optional[str]:
    pinned = user_config.get("OLLAMA_VISION_MODEL", "")
    if pinned:
        return pinned
    chosen = current_model("ollama")
    if chosen and "vision" in ollama_capabilities(chosen):
        return chosen
    try:
        return next((m["value"] for m in list_ollama_models() if m["vision"]), None)
    except OllamaError:
        return None


def _call_ollama_vision(parts: list) -> str:
    import base64
    model = _ollama_vision_model()
    if not model:
        raise RuntimeError("no Ollama model that can see images is pulled "
                           "(try `ollama pull qwen2.5vl:7b`)")
    messages: List[dict] = []
    for p in parts:
        if isinstance(p, str):
            messages.append({"role": "user", "content": p})
            continue
        if not messages:
            messages.append({"role": "user", "content": ""})
        messages[-1].setdefault("images", []).append(
            base64.b64encode(p[0]).decode("ascii"))
    return _ollama_chat(model, messages, temperature=0.1)


_ANTHROPIC_FALLBACK_BETA = "server-side-fallback-2026-07-01"
_ANTHROPIC_MAX_TOKENS = 32000


def _call_anthropic(content, api_key: Optional[str] = None,
                    timeout: float = _TEXT_TIMEOUT_SEC) -> str:
    """One request to Claude."""
    try:
        import anthropic  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "anthropic is required for LLM_PROVIDER=anthropic. Install it with:\n"
            "    pip install -r requirements-local.txt"
        ) from e

    model = current_model("anthropic")
    key = (api_key if api_key is not None else require_anthropic_key()).strip()
    client = anthropic.Anthropic(api_key=key, timeout=timeout, max_retries=0)
    request = {
        "model": model,
        "max_tokens": _ANTHROPIC_MAX_TOKENS,
        "output_config": {"effort": "medium"},
        "messages": [{"role": "user", "content": content}],
    }
    if "haiku" not in model:
        request["betas"] = [_ANTHROPIC_FALLBACK_BETA]
        request["fallbacks"] = "default"

    try:
        with client.beta.messages.stream(**request) as stream:
            message = stream.get_final_message()
    except anthropic.AuthenticationError as e:
        raise RuntimeError("Anthropic did not accept the Claude API key. Check it "
                           "under Settings.") from e
    except anthropic.PermissionDeniedError as e:
        raise RuntimeError(f"This Claude API key cannot use {model}: {e.message}") from e
    except anthropic.NotFoundError as e:
        raise RuntimeError(f"Claude has no model called {model}. Pick another "
                           "under Settings.") from e
    except anthropic.RateLimitError as e:
        raise RuntimeError(f"Claude is rate limiting this key: {e.message}") from e
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"Claude returned an error ({e.status_code}): {e.message}") from e
    except anthropic.APITimeoutError as e:
        raise RuntimeError(f"Claude ({model}) took too long to answer and the request "
                           f"timed out.") from e
    except anthropic.APIConnectionError as e:
        cause = e.__cause__ or e.__context__
        why = (f" ({type(cause).__name__}: {str(cause).splitlines()[0][:120]})"
               if cause and str(cause).strip() else "")
        raise RuntimeError(f"Could not reach Anthropic{why}. Check the connection, "
                           f"or a firewall or proxy in the way.") from e
    finally:
        client.close()

    usage.record("anthropic", model)
    if message.stop_reason == "refusal":
        category = getattr(message.stop_details, "category", None)
        raise RuntimeError("Claude declined this request"
                           + (f" ({category})" if category else ""))
    text = "".join(b.text for b in message.content if b.type == "text")
    if not text.strip():
        raise RuntimeError(f"Claude ({model}) returned no text "
                           f"(stop_reason={message.stop_reason})")
    return text


def call_anthropic_llm(prompt: str, api_key: Optional[str] = None) -> str:
    """Claude backend, through Anthropic's own SDK."""
    return _call_anthropic(prompt, api_key=api_key)


def _as_anthropic_content(parts: list) -> list:
    import base64
    out = []
    for p in parts:
        if isinstance(p, str):
            out.append({"type": "text", "text": p})
        else:
            out.append({"type": "image", "source": {
                "type": "base64", "media_type": p[1],
                "data": base64.standard_b64encode(p[0]).decode("ascii")}})
    return out


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


def _call_gemini_once(api_key: str, model: str, prompt: str,
                      contents: Optional[list] = None) -> str:
    """Single non-sleeping Gemini request on one key with finite timeout."""
    timeout_sec = _VISION_TIMEOUT_SEC if contents is not None else _TEXT_TIMEOUT_SEC
    config = {
        "temperature": 0.2,
        "response_mime_type": "application/json",
        "max_output_tokens": 32768,
    }
    client = _gemini_client(api_key, timeout_sec=timeout_sec)
    try:
        response = client.models.generate_content(
            model=model,
            contents=contents if contents is not None else prompt,
            config=config,
        )
    finally:
        try:
            client.close()
        except Exception:
            pass
    usage.record("gemini", model)
    text = response.text or ""
    if not text.strip():
        reason = "unknown"
        try:
            cand = (response.candidates or [None])[0]
            reason = str(getattr(cand, "finish_reason", "unknown"))
        except Exception:
            pass
        raise RuntimeError(f"Gemini returned no text (finish_reason={reason})")
    return text


def call_gemini_llm(prompt: str, contents: Optional[list] = None) -> str:
    """Call Gemini using only the Gemini slots from the per-key cooldown scheduler."""
    gslots = [s for s in ordered_pool_slots(vision_images=0) if s["provider"] == "gemini"]
    if not gslots:
        require_gemini_key()
    return _dispatch_pool(prompt=prompt, vision_parts=contents, fixed_slots=gslots)


# ---------------------------------------------------------------------------
# Vision helpers
# ---------------------------------------------------------------------------

GROQ_VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
_IMAGES_PER_REQUEST = {"gemini": 15, "openai": 15, "groq": 5, "openrouter": 5,
                       "anthropic": 15, "ollama": 5}


def _openrouter_model_supports_vision() -> bool:
    m = current_model("openrouter")
    if m in ("openrouter/free", "openrouter/auto"):
        return False
    return m in OPENROUTER_VISION_MODELS


def _openai_compatible_vision(parts: list, provider: str,
                              api_key: Optional[str] = None) -> str:
    from openai import OpenAI  # type: ignore
    if provider == "groq":
        key = (api_key if api_key is not None else require_groq_key()).strip()
        client = OpenAI(
            api_key=key,
            base_url=GROQ_BASE_URL,
            timeout=_VISION_TIMEOUT_SEC,
            max_retries=0,
        )
        model = GROQ_VISION_MODEL
    elif provider == "openrouter":
        client = _openrouter_client(api_key=api_key, timeout=_VISION_TIMEOUT_SEC)
        model = current_model("openrouter")
    else:
        client = _openai_client(api_key=api_key, timeout=_VISION_TIMEOUT_SEC)
        model = current_model("openai")
    try:
        response = client.chat.completions.create(
            model=model,
            temperature=0.1,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": _as_openai_content(parts)}],
        )
    finally:
        client.close()
    usage.record(provider, model)
    return response.choices[0].message.content or ""


def vision_images_per_request() -> int:
    """How many frames one vision request may carry on the highest-priority vision slot."""
    with _pool_lock:
        active = _fallback_provider or current_provider()
    for s in ordered_pool_slots():
        if s["vision"] and _dead_key(s["id"]) not in _key_dead_today:
            return _IMAGES_PER_REQUEST.get(s["provider"], 5)
    return _IMAGES_PER_REQUEST.get(active, 5)


# ---------------------------------------------------------------------------
# Error helpers & final error formatting
# ---------------------------------------------------------------------------

def _is_daily_quota(msg: str) -> bool:
    lowered = msg.lower()
    return ("perday" in lowered.replace("_", "")
            or "per day" in lowered
            or "requestsperday" in lowered.replace("_", ""))


def _is_blocked(e: BaseException) -> bool:
    return (e.__class__.__name__ == "PermissionDeniedError"
            or getattr(e, "status_code", None) == 403)


def _is_auth_error(e: BaseException) -> bool:
    if _is_blocked(e):
        return False
    msg = str(e).lower()
    return (e.__class__.__name__ == "AuthenticationError"
            or "api_key_invalid" in msg
            or "api key not valid" in msg
            or "permission_denied" in msg
            or "unauthenticated" in msg
            or "invalid_api_key" in msg
            or "incorrect api key" in msg
            or "invalid x-api-key" in msg
            or ("401" in msg and ("api key" in msg or "unauthorized" in msg)))


def _is_model_missing(e: BaseException) -> bool:
    msg = str(e).lower()
    return any(t in msg for t in (
        "not_found", "no longer available", "does not exist", "model_not_found",
        "no endpoints found",
    )) or getattr(e, "status_code", None) == 404


def _is_rate_limit(msg: str) -> bool:
    low = msg.lower()
    return any(t in low for t in (
        "429", "rate limit", "rate_limit", "ratelimit", "resource_exhausted",
        "too many requests", "quota", "requests per minute", "tokens per minute",
    ))


def _provider_said(e: BaseException) -> str:
    detail = getattr(e, "message", None)
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        inner = body.get("error", body)
        if isinstance(inner, dict) and inner.get("message"):
            detail = inner["message"]
    detail = str(detail or e).strip().splitlines()[0] if str(detail or e).strip() else ""
    return detail[:200].rstrip(".")


def _blocked_message(provider: str, e: BaseException) -> str:
    label = PROVIDER_LABELS.get(provider, provider)
    said = _provider_said(e)
    low = f"{said} {getattr(e, 'code', '') or ''}".lower()
    if "country" in low or "region" in low or "territory" in low:
        return (f"{label} turned down the request: it does not serve your country or "
                f"region ({said}). The key is fine. In Settings, choose OpenRouter, or "
                f"Ollama on this computer, instead.")
    if "model" in low:
        model = current_model(provider)
        return (f"{label} turned down the request: this key cannot use {model} "
                f"({said}). The key is fine. In Settings, pick another {label} model.")
    return (f"{label} turned down the request (403 {said or 'Forbidden'}). The key was "
            f"read, so pasting it again will not help: {label} blocks some countries "
            f"and networks. Try without a VPN or on another network, or in Settings "
            f"choose OpenRouter, or Ollama on this computer, instead.")


def _short_reason(e: BaseException) -> str:
    if _is_blocked(e):
        return f"403 {_provider_said(e) or 'Forbidden'}"
    return (str(e).strip().splitlines() or [type(e).__name__])[0][:60]


def _masked(key: str) -> str:
    key = (key or "").strip()
    return f"{key[:6]}…{key[-4:]}" if len(key) > 12 else "…"


_KEY_FOR = {"gemini": require_gemini_key, "groq": require_groq_key,
            "openrouter": require_openrouter_key, "openai": require_openai_key,
            "anthropic": require_anthropic_key}


def _final_error(provider: str, e: BaseException,
                 failures_snapshot: Optional[List[str]] = None) -> BaseException:
    label = PROVIDER_LABELS.get(provider, provider)
    hist = list(failures_snapshot) if failures_snapshot is not None else list(_failures)
    if _is_blocked(e):
        msg = _blocked_message(provider, e)
    elif _is_auth_error(e):
        try:
            key = _KEY_FOR[provider]()
        except (KeyError, RuntimeError):
            key = ""
        msg = f"{label} refused the API key saved for it ({_masked(key)})."
        base_url = (user_config.get("OPENAI_BASE_URL", OPENAI_BASE_URL) or "").strip()
        owner = None if (provider == "openai" and base_url) else misplaced_key(provider, key)
        if owner:
            other = PROVIDER_LABELS[owner]
            msg += (f" That key is from {other}, saved under {label} by mistake. In "
                    f"Settings, choose {other} as the provider and paste it there.")
        else:
            msg += f" Check it in your {label} account, then paste it again in Settings."
    elif hist or _warned_misplaced:
        first = (str(e).strip().splitlines() or [type(e).__name__])[0][:160]
        msg = f"{label} failed: {first.rstrip('.')}."
    else:
        return e
    if hist:
        msg += " Before that: " + "; ".join(hist[-5:]) + "."
    for skipped in sorted(_warned_misplaced):
        try:
            owner = misplaced_key(skipped, _KEY_FOR[skipped]())
        except RuntimeError:
            continue
        name, other = PROVIDER_LABELS[skipped], PROVIDER_LABELS.get(owner, owner)
        msg += (f" {name} was not tried: the key saved for it is from {other}. "
                f"In Settings, choose {name} and paste your {name} key.")
    return RuntimeError(msg)


# ---------------------------------------------------------------------------
# Thread-safe Per-Key Cooldown Pool Engine
# ---------------------------------------------------------------------------

_pool_lock = threading.Lock()
_pool_cond = threading.Condition(_pool_lock)

_fallback_provider: Optional[str] = None
_given_up: Set[str] = set()
_failures: List[str] = []
_warned_misplaced: Set[str] = set()

# Independent per-key state (keyed by slot_id)
_key_cooldown_until: Dict[str, float] = {}     # monotonic timestamp
_key_fail_count: Dict[str, int] = {}
_key_dead_today: Set[str] = set()              # f"{quota_day()}|{slot_id}"
_key_in_flight: Set[str] = set()               # slot_ids currently running a request


def _dead_key(slot_id: str) -> str:
    return f"{usage.quota_day()}|{slot_id}"


def _slot_id(provider: str, key: str) -> str:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:10]
    return f"{provider}:{digest}"


def ai_pool_enabled() -> bool:
    """Whether the user enabled priority-ordered multi-key AI pooling."""
    return user_config.get("AI_POOL_ENABLED", "0") == "1"


def _saved_pool_order() -> List[str]:
    raw = (user_config.get("AI_POOL_ORDER", "") or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(x) for x in data if x]
    except Exception:
        pass
    return []


def _ollama_up_cached() -> bool:
    global _ollama_seen
    if not current_model("ollama"):
        return False
    at, up = _ollama_seen
    if time.time() - at < 30:
        return up
    try:
        _ollama("/api/version")
        up = True
    except OllamaError:
        up = False
    _ollama_seen = (time.time(), up)
    return up


def _resolve_pool_slots() -> List[Dict]:
    """Discover every configured key/endpoint across all providers."""
    slots: List[Dict] = []

    # 1. Gemini (primary + spares, in order)
    g_keys = gemini_keys()
    for idx, k in enumerate(g_keys, 1):
        owner = misplaced_key("gemini", k)
        if owner:
            with _pool_lock:
                if "gemini" not in _warned_misplaced:
                    _warned_misplaced.add("gemini")
            continue
        slots.append({
            "id": _slot_id("gemini", k),
            "provider": "gemini",
            "label": f"Google Gemini #{idx}" if len(g_keys) > 1 else "Google Gemini",
            "masked": _masked(k),
            "secret": k,
            "vision": True,
        })

    # 2. Groq
    try:
        gk = require_groq_key().strip()
    except RuntimeError:
        gk = ""
    if gk:
        if misplaced_key("groq", gk):
            with _pool_lock:
                _warned_misplaced.add("groq")
        else:
            slots.append({
                "id": _slot_id("groq", gk),
                "provider": "groq",
                "label": "Groq",
                "masked": _masked(gk),
                "secret": gk,
                "vision": True,
            })

    # 3. OpenRouter
    try:
        ork = require_openrouter_key().strip()
    except RuntimeError:
        ork = ""
    if ork:
        if misplaced_key("openrouter", ork):
            with _pool_lock:
                _warned_misplaced.add("openrouter")
        else:
            slots.append({
                "id": _slot_id("openrouter", ork),
                "provider": "openrouter",
                "label": "OpenRouter",
                "masked": _masked(ork),
                "secret": ork,
                "vision": _openrouter_model_supports_vision(),
            })

    # 4. Ollama (local model chosen)
    ollama_model = (current_model("ollama") or "").strip()
    if ollama_model:
        slots.append({
            "id": "ollama:local",
            "provider": "ollama",
            "label": f"Ollama ({ollama_model})",
            "masked": ollama_model,
            "secret": "",
            "vision": True,  # validated via _ollama_vision_model when used
        })

    # 5. OpenAI (custom base URL works with or without an API key)
    base_url = (user_config.get("OPENAI_BASE_URL", OPENAI_BASE_URL) or "").strip()
    ok = (user_config.get("OPENAI_API_KEY", OPENAI_API_KEY) or "").strip()
    if ok or base_url:
        owner = None if base_url else misplaced_key("openai", ok)
        if owner:
            with _pool_lock:
                _warned_misplaced.add("openai")
        else:
            host = (urlparse(base_url).netloc or base_url) if base_url else ""
            label = f"OpenAI ({host})" if host else "OpenAI"
            sid = _slot_id("openai", ok) if ok else "openai:custom-url"
            slots.append({
                "id": sid,
                "provider": "openai",
                "label": label,
                "masked": _masked(ok) if ok else (host or "custom URL"),
                "secret": ok or "none",
                "vision": True,
            })

    # 6. Anthropic / Claude
    try:
        ak = require_anthropic_key().strip()
    except RuntimeError:
        ak = ""
    if ak:
        if misplaced_key("anthropic", ak):
            with _pool_lock:
                _warned_misplaced.add("anthropic")
        else:
            slots.append({
                "id": _slot_id("anthropic", ak),
                "provider": "anthropic",
                "label": "Claude",
                "masked": _masked(ak),
                "secret": ak,
                "vision": True,
            })

    return slots


def ordered_pool_slots(vision_images: int = 0) -> List[Dict]:
    """Return configured pool slots ordered by AI Pool priority or active provider.

    - When `AI_POOL_ENABLED=1`, respects `AI_POOL_ORDER` strictly across all keys
      and providers, placing newly added keys at the end in default order.
    - Otherwise, puts the active `current_provider()`'s keys first followed by the
      default fallback ladder.
    """
    raw = _resolve_pool_slots()
    default_rank = {p: i for i, p in enumerate(_DEFAULT_LADDER)}

    if ai_pool_enabled():
        saved = _saved_pool_order()
        saved_rank = {sid: i for i, sid in enumerate(saved)}
        ordered = sorted(
            enumerate(raw),
            key=lambda pair: (
                saved_rank.get(pair[1]["id"], 10**6),
                default_rank.get(pair[1]["provider"], 99),
                pair[0],
            ),
        )
        slots = [s for _, s in ordered]
    else:
        pref = _fallback_provider or current_provider()
        ordered = sorted(
            enumerate(raw),
            key=lambda pair: (
                0 if pair[1]["provider"] == pref else 1,
                default_rank.get(pair[1]["provider"], 99),
                pair[0],
            ),
        )
        slots = [s for _, s in ordered]

    if vision_images > 0:
        slots = [
            s for s in slots
            if s["vision"] and _IMAGES_PER_REQUEST.get(s["provider"], 5) >= vision_images
        ]
    return slots


def list_pool_slots() -> List[Dict]:
    """Non-secret metadata for every saved key, ordered by current priority."""
    return [
        {k: v for k, v in s.items() if k != "secret"}
        for s in ordered_pool_slots(vision_images=0)
    ]


def pool_status() -> Dict:
    """Current AI Pool configuration + live per-key state for Settings UI."""
    now = time.monotonic()
    slots = ordered_pool_slots(vision_images=0)
    out = []
    with _pool_lock:
        for s in slots:
            sid = s["id"]
            if _dead_key(sid) in _key_dead_today:
                state, rem = "exhausted", 0.0
            else:
                until = _key_cooldown_until.get(sid, 0.0)
                if until > now:
                    state, rem = "cooldown", round(until - now, 1)
                else:
                    state, rem = "ready", 0.0
            out.append({
                "id": sid,
                "provider": s["provider"],
                "label": s["label"],
                "masked": s["masked"],
                "status": state,
                "cooldown_remaining": rem,
            })
    return {
        "enabled": ai_pool_enabled(),
        "order": [s["id"] for s in out],
        "slots": out,
    }


def reset_fallback() -> None:
    """Reset per-key cooldowns, dead marks, and fallback state for a fresh run."""
    global _fallback_provider
    with _pool_lock:
        _fallback_provider = None
        _given_up.clear()
        _failures.clear()
        _warned_misplaced.clear()
        _key_cooldown_until.clear()
        _key_fail_count.clear()
        _key_dead_today.clear()
        _key_in_flight.clear()
        _pool_cond.notify_all()


def _gemini_is_configured() -> bool:
    try:
        return bool(require_gemini_key()) and not _gemini_spent_today()
    except RuntimeError:
        return False


def _gemini_spent_today() -> bool:
    if not usage.is_exhausted("gemini", current_model("gemini")):
        return False
    return len(gemini_keys()) <= 1


def _openai_is_configured() -> bool:
    base_url = (user_config.get("OPENAI_BASE_URL", OPENAI_BASE_URL) or "").strip()
    if base_url:
        return True
    try:
        key = require_openai_key()
        return bool(key) and not misplaced_key("openai", key)
    except RuntimeError:
        return False


def _groq_is_configured() -> bool:
    try:
        key = require_groq_key()
        return bool(key) and not misplaced_key("groq", key)
    except RuntimeError:
        return False


def _openrouter_is_configured() -> bool:
    try:
        key = require_openrouter_key()
        return bool(key) and not misplaced_key("openrouter", key)
    except RuntimeError:
        return False


def _anthropic_is_configured() -> bool:
    try:
        key = require_anthropic_key()
        return bool(key) and not misplaced_key("anthropic", key)
    except RuntimeError:
        return False


def _ollama_is_configured() -> bool:
    return _ollama_up_cached()


_LADDER = (
    ("gemini", _gemini_is_configured),
    ("groq", _groq_is_configured),
    ("openrouter", _openrouter_is_configured),
    ("ollama", _ollama_is_configured),
    ("openai", _openai_is_configured),
    ("anthropic", _anthropic_is_configured),
)


def no_providers_left() -> bool:
    """True when every configured key slot has been marked dead/exhausted today."""
    slots = ordered_pool_slots()
    if not slots:
        return True
    with _pool_lock:
        return all(_dead_key(s["id"]) in _key_dead_today for s in slots)


def _invoke_slot(slot: Dict, prompt: str = "",
                 vision_parts: Optional[list] = None) -> str:
    """Execute one request against one specific slot with zero internal sleeping."""
    p = slot["provider"]
    key = slot["secret"]
    if vision_parts is not None:
        if p == "gemini":
            return _call_gemini_once(
                key, current_model("gemini"), "", contents=_as_gemini_parts(vision_parts)
            )
        if p == "anthropic":
            return _call_anthropic(
                _as_anthropic_content(vision_parts),
                api_key=key,
                timeout=_VISION_TIMEOUT_SEC,
            )
        if p == "ollama":
            if not _ollama_up_cached():
                raise OllamaError("Ollama is not running")
            return _call_ollama_vision(vision_parts)
        return _openai_compatible_vision(vision_parts, p, api_key=key)

    if p == "gemini":
        return _call_gemini_once(key, current_model("gemini"), prompt)
    if p == "openai":
        return call_openai_llm(prompt, api_key=key)
    if p == "groq":
        return call_groq_llm(prompt, api_key=key)
    if p == "openrouter":
        return call_openrouter_llm(prompt, api_key=key)
    if p == "anthropic":
        return call_anthropic_llm(prompt, api_key=key)
    if p == "ollama":
        if not _ollama_up_cached():
            raise OllamaError("Ollama is not running")
        return call_ollama_llm(prompt)
    raise RuntimeError(f"Unknown provider: {p!r}")


def _cooldown_for(e: BaseException, prior_fails: int) -> Tuple[float, str]:
    msg = str(e)
    low = msg.lower()
    if "timed out" in low or "timeout" in low or "timeout" in type(e).__name__.lower():
        return 15.0, "timed out"
    m = re.search(r"retry in ([0-9.]+)s", msg, re.IGNORECASE)
    if m:
        return max(2.0, min(120.0, float(m.group(1)) + 1.0)), "rate limited"
    if _is_rate_limit(msg):
        return min(60.0, 12.0 * (2 ** min(prior_fails, 3))), "rate limited"
    return min(45.0, 4.0 * (2 ** min(prior_fails, 4))), "transient error"


def _dispatch_pool(prompt: str = "", vision_parts: Optional[list] = None,
                   fixed_slots: Optional[List[Dict]] = None) -> str:
    """Route a request through configured slots using independent per-key cooldowns.

    Zero delay is added between different keys or different providers. A thread
    sleeps ONLY when every live candidate key is either cooling down or busy
    in-flight on another thread, and wakes immediately when any thread finishes.
    """
    global _fallback_provider
    images = sum(1 for p in (vision_parts or []) if not isinstance(p, str))
    slots = list(fixed_slots) if fixed_slots is not None else ordered_pool_slots(vision_images=images)
    if not slots:
        pref = current_provider()
        if pref in _KEY_FOR:
            _KEY_FOR[pref]()  # raises the user-friendly "is not set" message
        raise RuntimeError(
            "No AI provider key is configured. Open Settings and add an API key "
            "(or choose an Ollama model)."
        )

    call_budget = 95.0 if vision_parts is not None else (150.0 if len(slots) > 1 else 240.0)
    deadline = time.monotonic() + call_budget
    attempts = 0
    last_err: Optional[BaseException] = None
    last_provider = slots[0]["provider"]
    saw_daily_quota = False
    saw_non_daily_error = False
    tried_bad_req: Set[str] = set()

    while True:
        proc.wait_if_paused()
        proc.raise_if_cancelled()
        now = time.monotonic()
        if attempts > 0 and now >= deadline:
            break
        chosen: Optional[Dict] = None
        wait_for: Optional[float] = None

        with _pool_lock:
            live = [
                s for s in slots
                if _dead_key(s["id"]) not in _key_dead_today and s["id"] not in tried_bad_req
            ]
            if not live:
                break

            # Pick the highest-priority slot that is neither in-flight nor cooling down.
            for s in live:
                sid = s["id"]
                if sid in _key_in_flight:
                    continue
                if _key_cooldown_until.get(sid, 0.0) <= now:
                    chosen = s
                    break

            if chosen is not None:
                _key_in_flight.add(chosen["id"])
                if chosen["provider"] != current_provider():
                    _fallback_provider = chosen["provider"]
            else:
                # Every live slot is either in-flight in another thread or on cooldown.
                # Compute exact wait until the earliest slot clears (or wakes via cond).
                idle_waits = [
                    max(0.05, _key_cooldown_until.get(s["id"], now) - now)
                    for s in live if s["id"] not in _key_in_flight
                ]
                any_in_flight = any(s["id"] in _key_in_flight for s in live)
                if idle_waits:
                    wait_for = min(idle_waits)
                elif any_in_flight:
                    wait_for = 1.0

                if wait_for is not None:
                    if now >= deadline or (idle_waits and not any_in_flight and now + wait_for > deadline):
                        break
                    _pool_cond.wait(timeout=min(wait_for, max(0.05, deadline - now)))
                continue

        # Run the chosen slot outside _pool_lock so parallel workers run concurrently.
        attempts += 1
        sid = chosen["id"]
        prov = chosen["provider"]
        try:
            text = _invoke_slot(chosen, prompt=prompt, vision_parts=vision_parts)
            with _pool_lock:
                _key_cooldown_until.pop(sid, None)
                _key_fail_count.pop(sid, None)
            return text
        except Exception as e:
            if isinstance(e, proc.Cancelled):
                raise
            last_err = e
            last_provider = prov
            msg = str(e)
            short = _short_reason(e)

            with _pool_lock:
                if isinstance(e, DailyQuotaExceeded) or _is_daily_quota(msg):
                    saw_daily_quota = True
                    _key_dead_today.add(_dead_key(sid))
                    _failures.append(f"{chosen['label']}: daily quota exhausted")
                    if prov == "gemini":
                        model = current_model("gemini")
                        gemini_live = [
                            s for s in slots
                            if s["provider"] == "gemini" and _dead_key(s["id"]) not in _key_dead_today
                        ]
                        if not gemini_live:
                            usage.mark_exhausted("gemini", model)
                    print(f"[llm] {chosen['label']} hit daily quota — switching instantly", flush=True)
                elif _is_auth_error(e) or _is_blocked(e) or _is_model_missing(e):
                    saw_non_daily_error = True
                    _key_dead_today.add(_dead_key(sid))
                    _failures.append(f"{chosen['label']}: {short}")
                    print(f"[llm] {chosen['label']} unusable ({short}) — switching instantly", flush=True)
                elif any(t in msg.upper() for t in ("INVALID_ARGUMENT", "BAD REQUEST")) and not _is_rate_limit(msg):
                    saw_non_daily_error = True
                    tried_bad_req.add(sid)
                    _failures.append(f"{chosen['label']}: {short}")
                    print(f"[llm] {chosen['label']} rejected request ({short}) — trying next", flush=True)
                else:
                    saw_non_daily_error = True
                    fails = _key_fail_count.get(sid, 0)
                    delay, reason = _cooldown_for(e, fails)
                    _key_cooldown_until[sid] = time.monotonic() + delay
                    _key_fail_count[sid] = fails + 1
                    _failures.append(f"{chosen['label']}: {reason} ({short})")
                    print(
                        f"[llm] {chosen['label']} {reason} (key cooldown {delay:.0f}s) — switching immediately",
                        flush=True,
                    )
        finally:
            with _pool_lock:
                _key_in_flight.discard(sid)
                _pool_cond.notify_all()

    if last_err is None:
        raise RuntimeError("No AI provider key is ready right now. Try again in a moment.")
    if saw_daily_quota and not saw_non_daily_error:
        raise DailyQuotaExceeded(
            f"All configured keys exhausted today's quota ({last_provider}: {last_err})."
        ) from last_err
    with _pool_lock:
        snap = list(_failures)
    raise _final_error(last_provider, last_err, failures_snapshot=snap) from last_err


def call_local_llm(prompt: str) -> str:
    """Dispatch to configured keys/providers with zero cross-key delay."""
    return _dispatch_pool(prompt=prompt)


def call_vision_llm(parts: list) -> str:
    """Dispatch vision frames + prompt across configured vision-capable keys/providers."""
    return _dispatch_pool(vision_parts=parts)
