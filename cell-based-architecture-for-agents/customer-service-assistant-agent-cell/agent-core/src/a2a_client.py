from __future__ import annotations

import logging
from uuid import uuid4

import httpx
from pydantic import BaseModel, Field

from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers import get_stream_response_text, new_text_message
from a2a.types import AgentCard, SendMessageRequest, TaskState
from a2a.types.a2a_pb2 import Role
from langchain_core.tools import StructuredTool

from src.tools import invoker_context

logger = logging.getLogger("agent-core")

_CARD_TIMEOUT_S = 10.0
_TASK_TIMEOUT_S = 120.0


def _identity_headers(obo_token: str | None = None, agent_id: str = "", traceparent: str = "") -> dict[str, str]:
    headers: dict[str, str] = {}
    if obo_token:
        headers["Authorization"] = f"Bearer {obo_token}"
    if agent_id:
        headers["x-agent-id"] = agent_id
    if traceparent:
        headers["traceparent"] = traceparent
    return headers


class RemoteAgentConnection:
    """Connection to one A2A agent. resolve_card() must be called eagerly so the tool description is ready before the first request."""

    def __init__(self, base_url: str):
        self._base_url = base_url.rstrip("/")
        self._card: AgentCard | None = None
        self._required_scopes: list[str] = []

    async def resolve_card(self, agent_id: str = "", traceparent: str = "") -> AgentCard:
        headers = _identity_headers(agent_id=agent_id, traceparent=traceparent)
        async with httpx.AsyncClient(headers=headers, timeout=_CARD_TIMEOUT_S) as http:
            return await self._ensure_card(http)

    async def _ensure_card(self, http: httpx.AsyncClient) -> AgentCard:
        if self._card is not None:
            return self._card

        resolver = A2ACardResolver(http, self._base_url)
        card = await resolver.get_agent_card()

        scopes: set[str] = set()
        for requirement in card.security_requirements:
            for scope_list in requirement.schemes.values():
                scopes.update(scope_list.list)
        self._card = card
        self._required_scopes = sorted(scopes)

        logger.info(
            "A2A card resolved: name=%s url=%s required_scopes=%s",
            card.name, self._base_url, self._required_scopes,
        )
        return card

    @property
    def card(self) -> AgentCard | None:
        return self._card

    @property
    def required_scopes(self) -> list[str]:
        return self._required_scopes

    async def send_message(
        self,
        task: str,
        obo_token: str | None,
        agent_id: str = "",
        context_id: str = "",
        traceparent: str = "",
    ) -> str:
        # TODO: exchange obo_token for a down-scoped token (limited to this
        # callee's required_scopes) before forwarding, instead of passing the
        # caller's broader token through unchanged.
        headers = _identity_headers(obo_token=obo_token, agent_id=agent_id, traceparent=traceparent)

        async with httpx.AsyncClient(headers=headers, timeout=_CARD_TIMEOUT_S) as http:
            card = await self._ensure_card(http)

        config = ClientConfig(
            httpx_client=httpx.AsyncClient(headers=headers, timeout=_TASK_TIMEOUT_S),
        )
        msg = new_text_message(
            text=task,
            context_id=context_id or uuid4().hex,
            role=Role.ROLE_USER,
        )
        request = SendMessageRequest(message=msg)

        logger.debug("A2A send_message → %s context_id=%s", self._base_url, msg.context_id)

        chunks: list[str] = []
        last_status_text = ""
        failed = False
        async with await create_client(card, client_config=config) as client:
            async for chunk in client.send_message(request):
                if chunk.HasField("artifact_update") or chunk.HasField("message"):
                    text = get_stream_response_text(chunk)
                    if text:
                        chunks.append(text)
                elif chunk.HasField("status_update"):
                    text = get_stream_response_text(chunk)
                    if text:
                        last_status_text = text
                    state = chunk.status_update.status.state
                    if state == TaskState.TASK_STATE_FAILED:
                        failed = True

        if chunks:
            return "".join(chunks)
        if failed or last_status_text:
            detail = last_status_text or "no detail provided"
            logger.warning("A2A sub-agent returned no artifact: %s", detail)
            return f"Sub-agent did not produce a result: {detail}"
        return ""


class _SendMessageInput(BaseModel):
    agent_name: str = Field(..., description="Name of the sub-agent (from list_remote_agents)")
    task: str = Field(
        ...,
        description=(
            "Self-contained task description with all context the sub-agent needs. "
            "It has no memory of prior turns."
        ),
    )


def make_a2a_tools(agents: dict[str, RemoteAgentConnection]) -> list[StructuredTool]:
    async def send(agent_name: str, task: str) -> str:
        conn = agents.get(agent_name)
        if conn is None:
            available = ", ".join(agents) or "none"
            return f"Unknown agent '{agent_name}'. Available: {available}."
        ctx = invoker_context.get() or {}
        return await conn.send_message(
            task=task,
            obo_token=ctx.get("obo_token"),
            agent_id=ctx.get("agent_id", ""),
            context_id=ctx.get("thread_id", ""),
            traceparent=ctx.get("traceparent", ""),
        )

    def list_agents() -> list[dict]:
        return [
            {"name": name, "description": card_tool_description(conn.card)}
            for name, conn in agents.items()
            if conn.card
        ]

    return [
        StructuredTool.from_function(
            coroutine=send,
            name="send_message_to_agent",
            description=(
                "Synchronously invoke a sub-agent and return its complete reply. "
                "Blocks until the sub-agent finishes. "
                "Call list_remote_agents first if you are unsure which agent to use."
            ),
            args_schema=_SendMessageInput,
            return_direct=False,
        ),
        StructuredTool.from_function(
            func=list_agents,
            name="list_remote_agents",
            description="List available sub-agents with their names and capabilities.",
            return_direct=False,
        ),
    ]


def card_tool_description(card: AgentCard) -> str:
    parts: list[str] = [card.description or f"Delegate tasks to {card.name}."]
    skill_names = [s.name for s in card.skills if s.name]
    if skill_names:
        parts.append(f"Skills: {', '.join(skill_names)}.")
    parts.append("Pass a clear, self-contained task description with all necessary context.")
    return " ".join(parts)
