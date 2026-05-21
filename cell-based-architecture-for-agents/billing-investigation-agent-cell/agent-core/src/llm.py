from __future__ import annotations

import os

from langchain_core.language_models import BaseChatModel

EGRESS_GW_URL = os.getenv("EGRESS_GW_URL")

_PROVIDER_PREFIXES: tuple[tuple[str, str], ...] = (
    ("gpt-", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("o4", "openai"),
    ("claude-", "anthropic"),
    ("gemini-", "gemini"),
)
_DEFAULT_PROVIDER = "gemini"


def resolve_provider(model: str) -> str:
    lower = model.lower()
    return next(
        (provider for prefix, provider in _PROVIDER_PREFIXES if lower.startswith(prefix)),
        _DEFAULT_PROVIDER,
    )


def _build_headers(provider: str, request_id: str, agent_id: str, traceparent: str) -> dict[str, str]:
    headers = {
        "x-llm-provider": provider,
        "x-request-id": request_id,
        "x-source-service": "agent2-core",
        "x-agent-id": agent_id,
    }
    if traceparent:
        headers["traceparent"] = traceparent
    return headers


def create_llm(
    model: str,
    request_id: str = "",
    agent_id: str = "",
    traceparent: str = "",
) -> BaseChatModel:
    provider = resolve_provider(model)
    headers = _build_headers(provider, request_id, agent_id, traceparent)

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=model,
            anthropic_api_url=EGRESS_GW_URL,
            anthropic_api_key="gw-managed",
            default_headers=headers,
            max_tokens=4096,
            max_retries=3,
        )

    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=model,
        base_url=f"{EGRESS_GW_URL}/v1",
        api_key="gw-managed",
        default_headers=headers,
        max_retries=3,
    )
