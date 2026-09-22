"""Turn a model config into the SDK's ``model`` value plus environment overrides.

The SDK has no base-URL option: there is no ``base_url`` field anywhere in its
options. The underlying CLI reaches Anthropic through ``ANTHROPIC_BASE_URL``, and
``ClaudeAgentOptions.env`` is merged *over* the inherited environment, so a session
can be pointed elsewhere. The protocol is the catch — the CLI speaks the Anthropic
Messages API, which neither Ollama's native API nor vLLM's OpenAI-compatible API
provides. Non-Anthropic providers therefore point at an Anthropic-compatible
gateway (LiteLLM or claude-code-router). See docs/DESIGN.md 3.6.
"""

from __future__ import annotations

import os

from app.config import settings
from app.services.snapshot import ModelSpec


class ModelConfigError(ValueError):
    pass


def scrubbed_base_env() -> dict[str, str]:
    """The inherited environment minus anything a session has no business seeing."""
    env = {k: v for k, v in os.environ.items() if k not in settings.env_denylist}
    for key in list(env):
        if key.startswith("AWS_"):
            env.pop(key, None)
    return env


def resolve(model: ModelSpec) -> tuple[str | None, dict[str, str]]:
    """Return ``(model_id, env_overrides)`` for the session subprocess."""
    env: dict[str, str] = dict(model.extra_env)

    if model.provider == "anthropic":
        if model.api_key:
            env["ANTHROPIC_API_KEY"] = model.api_key
    else:
        if not model.base_url:
            raise ModelConfigError(
                f"provider {model.provider!r} needs an Anthropic-API-compatible gateway URL; "
                "a raw Ollama or vLLM endpoint will not work"
            )
        env["ANTHROPIC_BASE_URL"] = model.base_url
        env["ANTHROPIC_AUTH_TOKEN"] = model.api_key or "local"
        # A gateway that does not need a key still wants the header present.
        env.setdefault("ANTHROPIC_API_KEY", model.api_key or "local")

    return model.model_id, env
