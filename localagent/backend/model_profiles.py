"""Per-model recommended generation settings, and what each setting means.

Presets come from the model publishers' cards. The UI shows them so users can pick
"Thinking — coding" instead of guessing sampling numbers.
"""
from __future__ import annotations

import re

# Generation settings that can be set app-wide and overridden per chat.
GEN_KEYS = ("preset", "thinking", "thinking_budget", "temperature", "top_p", "top_k", "min_p", "presence_penalty",
            "repetition_penalty", "max_new_tokens")

PARAM_DOCS = {
    "preset": "A bundle of the settings below, recommended by the model's publisher for a kind of work.",
    "thinking": "Let the model reason privately before answering. Better on multi-step or tricky tasks; slower, "
                "since it writes more.",
    "thinking_budget": "Most tokens the model may spend thinking in one reply. When reached, it is steered to "
                       "wrap up its reasoning and answer. 0 means no limit. Roughly 1 token ≈ ¾ of a word.",
    "temperature": "Randomness. Lower (0.2–0.7) is focused and repeatable; higher (0.8–1.2) is more varied and "
                   "creative but makes more mistakes. 0 always picks the most likely word.",
    "top_p": "Only consider the most likely words that together make up this share of probability. "
             "0.8–0.95 is typical; 1 turns it off.",
    "top_k": "Only consider this many of the most likely next words. 20–40 is typical; 0 turns it off.",
    "min_p": "Drop words less likely than this fraction of the top choice. Small values like 0.05 trim nonsense; "
             "0 turns it off.",
    "presence_penalty": "Discourage reusing words already written in this reply. Helps with loops and repetition; "
                        "too high can make wording odd. 0 is off, up to 2.",
    "repetition_penalty": "An older, stronger anti-repetition control. 1.0 is off; above 1.1 can hurt code and "
                          "structured output.",
    "max_new_tokens": "Longest single reply, including its thinking. If replies stop mid-thought, raise it.",
    "context_tokens": "How much conversation the model sees at once. Bigger remembers more of a long task but "
                      "uses more GPU memory. Keep it where the model still fits in GPU memory: past that point Windows "
                      "quietly borrows system RAM and replies get about 10× slower.",
    "quantization": "Compresses the model to fit in GPU memory. 4-bit: smallest, a little less accurate. "
                    "8-bit: in between. none: most accurate, needs far more memory.",
    "offload": "If the model doesn't fit in the VRAM limit, Auto puts the overflow in system RAM. That part runs "
               "many times slower, and the sidebar warns when it happens. GPU only fails to load instead, so you can "
               "pick a smaller model or lower the context window.",
    "tool_call_format": "How the model writes tool calls. 'auto' detects it; change only if tool calls aren't "
                        "recognized.",
}

LIMITS = {
    "temperature": (0.0, 2.0), "top_p": (0.01, 1.0), "top_k": (0, 200), "min_p": (0.0, 1.0),
    "presence_penalty": (0.0, 2.0), "thinking_budget": (0, 32768), "repetition_penalty": (1.0, 2.0), "max_new_tokens": (64, 32768),
    "context_tokens": (2048, 262144),
}


def _p(label, thinking, temperature, top_p, top_k, min_p=0.0, presence_penalty=0.0, repetition_penalty=1.0, note=""):
    return {"label": label, "thinking": thinking, "temperature": temperature, "top_p": top_p, "top_k": top_k,
            "min_p": min_p, "presence_penalty": presence_penalty, "repetition_penalty": repetition_penalty, "note": note}


PROFILES = [
    {
        "match": r"qwen3\.[5-9]",
        "family": "Qwen3.5 / Qwen3.6+",
        "context_max": 262144,
        "context_note": "Measured on a 16 GB RTX 4060 Ti with the 9B model at 4-bit: up to ~22,000 tokens is fast "
                        "(13 GB peak); 26,000 tokens was 11× slower as memory spilled to system RAM. 20,000 is a safe "
                        "default; more is fine on bigger GPUs.",
        "default_preset": "thinking_coding",
        "source": "https://huggingface.co/Qwen/Qwen3.5-9B",
        "presets": {
            "thinking_coding": _p("Thinking — coding & precise work", True, 0.6, 0.95, 20,
                                  note="Publisher's pick for coding. Used in the benchmark; best for agent tasks."),
            "thinking_general": _p("Thinking — general", True, 1.0, 0.95, 20, presence_penalty=1.5,
                                   note="Publisher's pick for general reasoning and writing."),
            "fast": _p("Fast — no thinking", False, 0.7, 0.8, 20, presence_penalty=1.5,
                       note="Quick answers and simple steps. Weaker on multi-step work."),
        },
    },
    {
        "match": r"qwen3(?![.\d])",
        "family": "Qwen3",
        "context_max": 32768,
        "default_preset": "thinking",
        "source": "https://huggingface.co/Qwen/Qwen3-8B",
        "presets": {
            "thinking": _p("Thinking", True, 0.6, 0.95, 20,
                           note="Publisher's pick for thinking mode. Don't use temperature 0 with thinking."),
            "fast": _p("Fast — no thinking", False, 0.7, 0.8, 20, note="Publisher's pick for non-thinking mode."),
        },
    },
]

GENERIC = {
    "family": "Other model",
    "context_max": 32768,
    "default_preset": "balanced",
    "source": "",
    "presets": {
        "balanced": _p("Balanced", True, 0.7, 0.9, 40),
        "precise": _p("Precise", True, 0.3, 0.9, 20),
        "creative": _p("Creative", True, 1.0, 0.95, 50, presence_penalty=0.5),
    },
}


def profile_for(model_id: str) -> dict:
    name = (model_id or "").lower()
    for prof in PROFILES:
        if re.search(prof["match"], name):
            return {k: v for k, v in prof.items() if k != "match"}
    return dict(GENERIC)


def validate_generation(values: dict) -> list[str]:
    errors = []
    for key, (lo, hi) in LIMITS.items():
        if key in values and values[key] is not None:
            try:
                v = float(values[key])
            except (TypeError, ValueError):
                errors.append(f"{key} must be a number")
                continue
            if not lo <= v <= hi:
                errors.append(f"{key} must be between {lo} and {hi}")
    return errors


def effective_generation(settings, overrides: dict | None) -> dict:
    """App-wide settings with a chat's overrides applied."""
    params = {k: getattr(settings, k) for k in GEN_KEYS if hasattr(settings, k)}
    for k, v in (overrides or {}).items():
        if k in GEN_KEYS and v is not None:
            params[k] = v
    return params
