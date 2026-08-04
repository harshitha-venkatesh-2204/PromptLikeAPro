"""Production LLM gateway for the Prompt Like A PRO gaming app.

An internal FastAPI service that game servers call to reach Claude Sonnet.
Anthropic API keys are read only from the environment / a secret manager and are
never exposed to the game client, never hardcoded, and never logged.

This package is intentionally separate from the booth app (``app.py``), which
stays single-file and dependency-free. See ``gateway/README.md``.
"""
from __future__ import annotations

__all__ = ["__version__"]
__version__ = "1.0.0"
