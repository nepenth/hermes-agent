"""ACP auth helpers — detect available LLM providers for authentication."""

from __future__ import annotations

import os
from typing import Optional


def has_provider() -> bool:
    """Return True if any supported LLM provider API key is configured."""
    return bool(
        os.environ.get("OPENROUTER_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
    )


def detect_provider() -> Optional[str]:
    """Return the name of the first available provider, or None."""
    if os.environ.get("OPENROUTER_API_KEY"):
        return "openrouter"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return None
