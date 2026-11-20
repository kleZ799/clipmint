import os
import sys
from pathlib import Path

from dotenv import load_dotenv


def _env_file_candidates():
    """Directories a packaged run might reasonably keep a .env in.

    A bare load_dotenv() searches the current directory. The packaged build
    chdirs to the user's video folder before anything here is imported, so
    it searches a folder that has never held a .env — and someone who put
    their key in the repo's .env and then ran the .exe gets told the key "is
    not set", which is true only of the directory we happened to be standing
    in. Look beside the executable and in the config directory too.

    The config directory is asked for rather than spelled out here, because
    it is somewhere different on every platform and one wrong copy of that
    answer is how a mac user's .env stops being read.
    """
    seen = []
    if getattr(sys, "frozen", False):
        seen.append(Path(sys.executable).resolve().parent)
    try:
        from . import user_config

        seen.append(user_config.config_dir())
    except Exception:
        pass        # an unreadable config dir is not a reason to fail to start
    return seen


load_dotenv()
for _candidate in _env_file_candidates():
    _env_file = _candidate / ".env"
    if _env_file.is_file():
        # override=False: a real environment variable still wins, as documented.
        load_dotenv(_env_file, override=False)

MUAPI_API_KEY = os.getenv("MUAPI_API_KEY", "").strip()
MUAPI_BASE_URL = os.getenv("MUAPI_BASE_URL", "https://api.muapi.ai/api/v1").rstrip("/")

POLL_INTERVAL_SECONDS = float(os.getenv("MUAPI_POLL_INTERVAL", "5"))
POLL_TIMEOUT_SECONDS = float(os.getenv("MUAPI_POLL_TIMEOUT", "600"))

# Local-mode (--mode local) settings — only consulted when running offline.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
# Optional custom base URL for the OpenAI provider. Set to point at any
# OpenAI-compatible endpoint: LM Studio, Ollama, Together AI, Mistral, etc.
# Leave empty to use the standard OpenAI endpoint (https://api.openai.com/v1).
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
# Probed against a real free-tier key: 3.6-flash returns 429 (allowance spent),
# 3.8-flash answers roughly two times in three, 3.5-flash answered every time.
# The default is the one that replies, not the one with the highest number.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")

# Groq runs open-weight models on its own hardware, free, no card. It exists
# here as a second opinion rather than a better one: when Google is refusing
# requests, a different company's capacity is unaffected by that, which is a
# thing no amount of retrying against Google can achieve.
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

# OpenRouter is one key in front of hundreds of models from every lab, some of
# them free. "openrouter/free" is OpenRouter's own router across the free ones,
# so the default never bills anyone; any model ID from openrouter.ai/models
# works in its place, paid ones spending the user's OpenRouter credit.
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/free")

# Claude, through Anthropic's own API. There is no free tier: every request is
# billed to the user's Anthropic account, so nothing falls back onto it before
# the free providers have been tried. Opus is the default because ranking is a
# judgement call; Sonnet and Haiku are a click away in Settings for anyone who
# would rather pay less.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-5-5")

# Ollama runs open models on this computer: no key, no account, nothing sent
# anywhere. There is no default model on purpose -- the right one depends on
# what has been pulled and how much memory the GPU has -- so it is set up by
# choosing one in Settings, which also records that Ollama is wanted at all.
# OLLAMA_HOST is the variable Ollama itself reads, so a machine already
# pointed at a server elsewhere needs no second setting.
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "").strip()
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "").strip()
# Ollama gives a model a 4k-token window unless asked for more, and quietly
# drops the start of anything longer. A ranking chunk is about 6-7k tokens,
# so without this the instructions at the top are what gets cut.
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "16384") or 16384)

# More Gemini keys, one per line or comma-separated, tried in order once the
# main key's day is spent. Each Google project has its own free allowance, so
# a second key is a second day's worth of runs.
GEMINI_EXTRA_KEYS = os.getenv("GEMINI_EXTRA_KEYS", "")
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").strip().lower()
LOCAL_WHISPER_MODEL = os.getenv("LOCAL_WHISPER_MODEL", "base")
LOCAL_WHISPER_DEVICE = os.getenv("LOCAL_WHISPER_DEVICE", "auto")  # auto / cpu / cuda
LOCAL_OUTPUT_DIR = os.getenv("LOCAL_OUTPUT_DIR", "output")

# Optional final upscale/downscale of each rendered short, e.g. "1080x1920" to
# hit YouTube Shorts / Reels spec exactly. Empty = keep the native crop size.
LOCAL_OUTPUT_RESOLUTION = os.getenv("LOCAL_OUTPUT_RESOLUTION", "").strip().lower()

# VAD (Voice Activity Detection) settings for faster-whisper
# Default threshold is 0.5; lower = more sensitive, higher = less sensitive
# Default min_speech_duration_ms is 250ms; increase to avoid tiny false positives
# Default min_silence_duration_ms is 2000ms; increase to avoid splitting mid-sentence
# DISABLED by default because VAD is too aggressive on mixed speech/music content
LOCAL_WHISPER_VAD_FILTER = os.getenv("LOCAL_WHISPER_VAD_FILTER", "false").strip().lower() == "true"
_vad_params_env = os.getenv("LOCAL_WHISPER_VAD_PARAMETERS", "")
if _vad_params_env:
    import json
    LOCAL_WHISPER_VAD_PARAMETERS = json.loads(_vad_params_env)
else:
    # Match faster-whisper defaults when VAD is enabled
    LOCAL_WHISPER_VAD_PARAMETERS = {
        "threshold": 0.5,
        "min_speech_duration_ms": 250,
        "max_speech_duration_s": float("inf"),
        "min_silence_duration_ms": 2000,
        "speech_pad_ms": 400,
    }


def require_api_key() -> str:
    if not MUAPI_API_KEY:
        raise RuntimeError(
            "MUAPI_API_KEY is not set. Add it to your .env file or export it as an env var."
        )
    return MUAPI_API_KEY


def current_provider() -> str:
    """LLM provider, re-read at call time so UI changes take effect live."""
    from . import user_config
    return (user_config.get("LLM_PROVIDER", LLM_PROVIDER) or "gemini").strip().lower()


def current_processor() -> str:
    """auto / gpu / cpu -- what does the heavy work. See accel.py.

    Re-read at call time like the provider, so a change made in the app --
    even while a run is paused -- applies from the next clip.
    """
    from . import user_config
    return (user_config.get("PROCESSOR", "auto") or "auto").strip().lower()


def current_model(provider: str) -> str:
    from . import user_config
    if provider == "openai":
        return user_config.get("OPENAI_MODEL", OPENAI_MODEL)
    if provider == "groq":
        return user_config.get("GROQ_MODEL", GROQ_MODEL)
    if provider == "openrouter":
        return user_config.get("OPENROUTER_MODEL", OPENROUTER_MODEL)
    if provider == "anthropic":
        return user_config.get("ANTHROPIC_MODEL", ANTHROPIC_MODEL)
    if provider == "ollama":
        return user_config.get("OLLAMA_MODEL", OLLAMA_MODEL)
    return user_config.get("GEMINI_MODEL", GEMINI_MODEL)


def ollama_host() -> str:
    """Where the Ollama server is, as a URL the client can call.

    OLLAMA_HOST is often written the way the server wants it to *listen*
    ("0.0.0.0", "0.0.0.0:11434", "localhost"), which is not something you can
    connect to. Turn any of those into a working address.
    """
    from . import user_config
    raw = (user_config.get("OLLAMA_HOST", OLLAMA_HOST) or "").strip().rstrip("/")
    if not raw:
        return "http://127.0.0.1:11434"
    if "://" not in raw:
        raw = "http://" + raw
    scheme, rest = raw.split("://", 1)
    host = rest.split("/", 1)[0]
    if host.startswith("0.0.0.0"):
        host = "127.0.0.1" + host[len("0.0.0.0"):]
    # Ollama's own rule: no port means 11434, unless it is https behind a proxy.
    if ":" not in host.rsplit("]", 1)[-1] and scheme != "https":
        host += ":11434"
    return f"{scheme}://{host}"


def _where_we_looked(name: str) -> str:
    """Name the places a key could have come from, and why one didn't work.

    "Add it to your .env" is bad advice when the key is already saved and the
    settings file is what broke. Say which file, and say what went wrong.
    """
    from . import user_config

    try:
        path = user_config.config_path()
    except OSError:
        return f" Checked the environment for {name}."

    why = user_config.load_error()
    if why:
        return (
            f" A {name} saved in {path} was NOT used, because that file {why}. "
            f"Fix or delete that file, or set {name} in the environment."
        )
    return f" Checked the environment and {path} for {name}."


def require_openai_key() -> str:
    from . import user_config
    key = user_config.get("OPENAI_API_KEY", OPENAI_API_KEY)
    if key:
        return key
    raise RuntimeError(
        "OPENAI_API_KEY is not set. Local mode needs an OpenAI key for highlight "
        "ranking." + _where_we_looked("OPENAI_API_KEY")
    )


def require_groq_key() -> str:
    from . import user_config
    key = user_config.get("GROQ_API_KEY", GROQ_API_KEY)
    if key:
        return key
    raise RuntimeError(
        "GROQ_API_KEY is not set. Get a free one at https://console.groq.com "
        "— no card required." + _where_we_looked("GROQ_API_KEY")
    )


def require_openrouter_key() -> str:
    from . import user_config
    key = user_config.get("OPENROUTER_API_KEY", OPENROUTER_API_KEY)
    if key:
        return key
    raise RuntimeError(
        "OPENROUTER_API_KEY is not set. Get one at https://openrouter.ai/keys "
        "— the free models need no credit." + _where_we_looked("OPENROUTER_API_KEY")
    )


def require_anthropic_key() -> str:
    from . import user_config
    key = user_config.get("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY)
    if key:
        return key
    raise RuntimeError(
        "ANTHROPIC_API_KEY is not set. Get one at "
        "https://platform.claude.com/settings/keys — Claude is billed per "
        "request, there is no free tier." + _where_we_looked("ANTHROPIC_API_KEY")
    )


def require_gemini_key() -> str:
    from . import user_config
    key = user_config.get("GEMINI_API_KEY", GEMINI_API_KEY)
    if key:
        return key
    raise RuntimeError(
        "GEMINI_API_KEY is not set. Local mode needs a Gemini key when "
        "LLM_PROVIDER=gemini." + _where_we_looked("GEMINI_API_KEY")
    )


PROVIDER_LABELS = {"gemini": "Google Gemini", "groq": "Groq", "openrouter": "OpenRouter",
                   "openai": "OpenAI", "anthropic": "Claude", "ollama": "Ollama"}

# How each provider's keys begin, most specific first: OpenRouter's and
# Anthropic's keys also start with OpenAI's "sk-". Google hands out keys in two
# shapes, the old "AIza..." and the newer "AQ." ones.
_KEY_PREFIXES = (
    ("openrouter", ("sk-or-",)),
    ("anthropic", ("sk-ant-",)),
    ("openai", ("sk-",)),
    ("groq", ("gsk_",)),
    ("gemini", ("AIza", "AQ.")),
)


def key_owner(key: str):
    """The provider a key plainly belongs to, going by how it starts, or None."""
    k = (key or "").strip()
    for name, prefixes in _KEY_PREFIXES:
        if k.startswith(prefixes):
            return name
    return None


def misplaced_key(provider: str, key: str):
    """The provider `key` really belongs to, when that is not `provider`.

    A Gemini key saved as OpenAI's does not fail when it is saved. It fails
    halfway through a run, as a 401 from OpenAI that never says the key came
    from Google -- and if OpenAI was only the fallback, the person never chose
    OpenAI at all. Keys that start some other way are left alone: a provider
    may change its format, and refusing a good key is worse than this.
    """
    owner = key_owner(key)
    return owner if owner and owner != provider else None


def gemini_keys() -> list:
    """Every Gemini key to try, the main one first, without repeats.

    Empty when there is no main key: the extras are spares for it, not a
    replacement, so they never stand in for setting one up.
    """
    from . import user_config
    import re

    main = user_config.get("GEMINI_API_KEY", GEMINI_API_KEY).strip()
    if not main:
        return []
    keys = [main]
    extra = user_config.get("GEMINI_EXTRA_KEYS", GEMINI_EXTRA_KEYS) or ""
    for k in re.split(r"[\s,;]+", extra):
        if k and k not in keys:
            keys.append(k)
    return keys
