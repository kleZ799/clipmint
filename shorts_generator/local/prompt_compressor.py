"""Compress transcripts to fit small-context-window models.

Some free or local models have context windows as small as 4-8K tokens.
Sending a 2-hour transcript verbatim silently truncates it, causing the
model to miss the second half of the video entirely.  This module detects
when a transcript is too long for the active model and symmetrically
removes the middle section, keeping the opening (which sets the topic)
and the closing (which is statistically richer for viral moments).
"""
from __future__ import annotations

# Approximate token limits per model (characters ≈ tokens × 4 for English).
# Unknown models fall back to _DEFAULT_CONTEXT — conservative but safe.
MODEL_CONTEXT_TOKENS: dict[str, int] = {
    # OpenRouter free routing (hits 8K text models most often)
    "openrouter/free": 8_192,
    "openrouter/auto": 32_768,
    # Groq hosted
    "openai/gpt-oss-20b": 8_192,
    "openai/gpt-oss-120b": 32_768,
    # Gemini (effectively unlimited for our purposes)
    "gemini-3.5-flash": 1_000_000,
    "gemini-3.6-flash": 1_000_000,
    "gemini-3.8-flash": 1_000_000,
    # OpenAI
    "gpt-4o-mini": 128_000,
    "gpt-4o": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-3.5-turbo": 16_385,
}
_DEFAULT_CONTEXT = 32_768
# Characters reserved for the system prompt, instructions, and JSON overhead.
PROMPT_OVERHEAD_TOKENS = 2_500

_SNIP = (
    "\n\n[... middle of transcript omitted to fit model context window ...]\n\n"
)


def compress_for_model(text: str, model: str) -> str:
    """Truncate *text* to fit *model*'s context window.

    If the text is short enough it is returned unchanged.  Otherwise the
    middle is removed symmetrically so the model still sees how the video
    opens and how it closes — the two regions that determine whether a
    highlight is worth extracting.

    Args:
        text:  The full transcript text (timestamped lines).
        model: The model identifier string (e.g. ``"gemini-3.5-flash"``).

    Returns:
        The original text, or a compressed version with a snip marker.
    """
    limit = MODEL_CONTEXT_TOKENS.get(model, _DEFAULT_CONTEXT)
    char_limit = (limit - PROMPT_OVERHEAD_TOKENS) * 4  # rough chars-per-token
    if len(text) <= char_limit:
        return text

    half = char_limit // 2
    compressed = text[:half] + _SNIP + text[-half:]
    removed = len(text) - len(compressed) + len(_SNIP)
    print(
        f"[highlights] transcript compressed: {len(text):,} → {len(compressed):,} chars "
        f"({removed:,} chars removed) for {model} ({limit:,}-token context)",
        flush=True,
    )
    return compressed
