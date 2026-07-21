from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

from src.a2a_client import RemoteAgentConnection, make_a2a_tools
from src.agent import build_agent
from src.step_up_helpers import StepUpRequired
from src.tools import ToolManager

INTERCELL_GW_URL = os.getenv("INTERCELL_GW_URL")
BILLING_AGENT_URL = os.getenv("BILLING_AGENT_URL")
AUTH_SERVICE_URL = os.getenv("AUTH_SERVICE_URL")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
MODEL = os.getenv("MODEL", "gemini-2.5-flash")
AGENT_ID = os.getenv("AGENT_ID")

AGENT_BASE_SCOPES: list[str] = os.getenv("AGENT_SCOPES", "").split()

TOOL_ALLOWLIST: list[str] = [
    "get_customer_profile",
    "update_customer_profile",
    "create_support_ticket",
    "trigger_escalation",
    "issue_refund",
]

RECURSION_LIMIT = 25
HTTP_TIMEOUT_S = 10.0

A2A_RESOLVE_DELAYS_S: tuple[float, ...] = (1, 2, 4, 8, 16, 30)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format='{"time":"%(asctime)s","level":"%(levelname)s","component":"agent-core","msg":"%(message)s"}',
)
logger = logging.getLogger("agent-core")


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=16000)
    session_id: str | None = None
    model: str = Field(default=MODEL, pattern=r"^[a-zA-Z0-9._-]+$")


class ChatResponse(BaseModel):
    session_id: str
    request_id: str
    message: str
    model: str
    latency_ms: float


class AuthContext(BaseModel):
    """User identity forwarded from the Ingress GW."""
    user_sub: str
    user_role: str
    request_id: str
    traceparent: str = ""


@dataclass(frozen=True, slots=True)
class TokenResult:
    token: str | None
    consent_url: str | None


def get_auth_context(request: Request) -> AuthContext:
    user_sub = request.headers.get("x-user-sub", "")
    if not user_sub:
        raise HTTPException(
            status_code=401,
            detail="Missing x-user-sub header — request did not pass through Ingress GW",
        )
    return AuthContext(
        user_sub=user_sub,
        user_role=request.headers.get("x-user-role", ""),
        request_id=request.headers.get("x-request-id", str(uuid.uuid4())),
        traceparent=request.headers.get("traceparent", ""),
    )


async def _request_obo_token(http: httpx.AsyncClient, payload: dict) -> TokenResult:
    """Single code path for both baseline and step-up OBO requests."""
    try:
        resp = await http.post(
            f"{AUTH_SERVICE_URL}/auth/obo-token",
            json=payload,
            headers={"x-source-service": "agent-core"},
        )
    except httpx.HTTPError as exc:
        logger.error("obo-token call failed: payload=%s %s", payload, exc)
        return TokenResult(token=None, consent_url=None)

    if resp.status_code == 200:
        return TokenResult(token=resp.json().get("access_token"), consent_url=None)
    if resp.status_code == 401:
        try:
            data = resp.json()
        except ValueError:
            data = {}
        return TokenResult(token=None, consent_url=data.get("consent_url"))

    logger.error("obo-token unexpected status: %d body=%s", resp.status_code, resp.text[:200])
    return TokenResult(token=None, consent_url=None)


async def _get_step_up_token(
    http: httpx.AsyncClient, user_sub: str, session_id: str, required_scope: str,
) -> TokenResult:
    # Callback hits auth-service directly; no token traverses agent-core or the client.
    return await _request_obo_token(
        http,
        {
            "user_sub": user_sub,
            "session_id": session_id,
            "scopes": [required_scope],
            "required_scope": required_scope,
            "step_up": True,
        },
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    tool_manager = ToolManager(INTERCELL_GW_URL, allowlist=TOOL_ALLOWLIST)
    await tool_manager.connect()

    a2a_tools = await _build_a2a_tools()
    http_client = httpx.AsyncClient(timeout=HTTP_TIMEOUT_S)
    checkpointer = MemorySaver()

    app.state.agent = build_agent(tool_manager, checkpointer, extra_tools=a2a_tools)
    app.state.tool_manager = tool_manager
    app.state.http_client = http_client

    logger.info(
        "Agent Core started: tools=%d sub_agents=%d agent_id=%s",
        len(tool_manager.tools), len(a2a_tools), AGENT_ID,
    )
    try:
        yield
    finally:
        await tool_manager.disconnect()
        await http_client.aclose()
        logger.info("Agent Core shut down")


async def _build_a2a_tools():
    if not INTERCELL_GW_URL or not BILLING_AGENT_URL:
        return []

    conn = RemoteAgentConnection(
        canonical_url=BILLING_AGENT_URL,
        egress_url=INTERCELL_GW_URL.rstrip("/") + "/a2a/",
    )
    card = await _resolve_card_with_retry(conn)
    if card is None:
        return []
    return make_a2a_tools({card.name: conn})


async def _resolve_card_with_retry(conn: RemoteAgentConnection):
    attempts = len(A2A_RESOLVE_DELAYS_S) + 1
    for attempt in range(1, attempts + 1):
        try:
            return await conn.resolve_card(agent_id=AGENT_ID or "")
        except (SystemExit, KeyboardInterrupt):
            raise
        except BaseException as exc:
            if attempt == attempts:
                logger.warning(
                    "Could not resolve A2A agent card after %d attempts: %s",
                    attempts, exc,
                )
                return None
            delay = A2A_RESOLVE_DELAYS_S[attempt - 1]
            logger.info(
                "A2A card resolution attempt %d/%d failed (%s); retrying in %.0fs",
                attempt, attempts, exc, delay,
            )
            await asyncio.sleep(delay)
    return None


app = FastAPI(title="Agent Core", version="3.0.0", lifespan=lifespan)


@app.post("/api/agents/customer_service/chat", response_model=ChatResponse)
async def chat(body: ChatRequest, auth: AuthContext = Depends(get_auth_context)):
    start = time.monotonic()
    session_id = body.session_id or str(uuid.uuid4())
    http = app.state.http_client

    logger.info(
        "[%s] chat user=%s role=%s session=%s model=%s",
        auth.request_id, auth.user_sub, auth.user_role, session_id, body.model,
    )

    obo_result = await _request_obo_token(
        http,
        {"user_sub": auth.user_sub, "scopes": AGENT_BASE_SCOPES, "session_id": session_id},
    )
    if not obo_result.token:
        return _consent_required_response(auth, session_id, obo_result.consent_url)

    config = _build_run_config(body.model, session_id, obo_result.token, auth)

    pending = _pending_interrupt(app.state.agent.get_state(config))
    try:
        if pending is not None:
            # Prior turn paused on a step-up challenge — resume it instead of
            # appending a fresh HumanMessage on top of the interrupted run.
            result = await _resume_or_respond(pending, config, auth, session_id)
        else:
            result = await app.state.agent.ainvoke(
                {"messages": [HumanMessage(content=body.message)]}, config=config,
            )
    except Exception as exc:
        logger.error("[%s] agent invocation failed: %s", auth.request_id, exc)
        return _agent_error_response(session_id, auth.request_id)

    while True:
        if isinstance(result, JSONResponse):
            return result
        challenge = _interrupt_value(result)
        if challenge is None:
            break
        result = await _resume_or_respond(challenge, config, auth, session_id)

    if isinstance(result, JSONResponse):
        return result

    final_message = _extract_final_text(result["messages"])

    return ChatResponse(
        session_id=session_id,
        request_id=auth.request_id,
        message=final_message,
        model=body.model,
        latency_ms=round((time.monotonic() - start) * 1000, 1),
    )


def _consent_required_response(auth: AuthContext, session_id: str, consent_url: str | None) -> JSONResponse:
    logger.info("[%s] Consent required for user=%s", auth.request_id, auth.user_sub)
    return JSONResponse(
        status_code=401,
        content={
            "error": "consent_required",
            "session_id": session_id,
            "request_id": auth.request_id,
            "consent_url": consent_url or f"{AUTH_SERVICE_URL}/oauth/agent_consent",
            "required_scopes": AGENT_BASE_SCOPES,
            "message": (
                "The agent requires your authorization to access the requested scopes. "
                "Please complete the consent flow at the provided URL and retry."
            ),
        },
        headers={
            "WWW-Authenticate": f'Bearer error="insufficient_scope" scope="{" ".join(AGENT_BASE_SCOPES)}"',
        },
    )


def _agent_error_response(session_id: str, request_id: str, message: str | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={
            "error": "agent_error",
            "session_id": session_id,
            "request_id": request_id,
            "message": message or "The agent encountered an error. Please try again.",
        },
    )


def _step_up_required_response(
    challenge: StepUpRequired, session_id: str, request_id: str, consent_url: str | None,
) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={
            "error": "step_up_authentication_required",
            "session_id": session_id,
            "request_id": request_id,
            "tool": challenge.tool_name,
            "required_scope": challenge.required_scope,
            "www_authenticate": challenge.www_authenticate,
            "consent_url": consent_url or "",
            "message": challenge.message,
        },
        headers={"WWW-Authenticate": challenge.www_authenticate},
    )


def _build_run_config(model: str, session_id: str, obo_token: str, auth: AuthContext) -> dict[str, Any]:
    return {
        "configurable": {
            "thread_id": session_id,
            "model": model,
            "request_id": auth.request_id,
            "traceparent": auth.traceparent,
            "user_sub": auth.user_sub,
            "user_role": auth.user_role,
            "agent_id": AGENT_ID,
            "scopes": AGENT_BASE_SCOPES,
            "obo_token": obo_token,
        },
        "recursion_limit": RECURSION_LIMIT,
    }


def _interrupt_value(result: dict | None) -> dict | None:
    """Step-up payload from the most recent ainvoke result, if the graph paused."""
    if not isinstance(result, dict):
        return None
    interrupts = result.get("__interrupt__")
    if not interrupts:
        return None
    raw = interrupts[0]
    value = raw.get("value") if isinstance(raw, dict) else getattr(raw, "value", None)
    return value if isinstance(value, dict) and value.get("type") == "step_up_required" else None


def _pending_interrupt(state) -> dict | None:
    """Step-up payload from a checkpointed StateSnapshot, if a task is paused."""
    for task in getattr(state, "tasks", ()) or ():
        for intr in getattr(task, "interrupts", ()) or ():
            value = getattr(intr, "value", None)
            if isinstance(value, dict) and value.get("type") == "step_up_required":
                return value
    return None


async def _resume_or_respond(
    challenge: dict,
    config: dict[str, Any],
    auth: AuthContext,
    session_id: str,
) -> dict | JSONResponse:
    """Fetch an elevated OBO token and resume the paused graph, or return 401."""
    required_scope = challenge.get("required_scope", "")
    tool_name = challenge.get("tool", "")
    logger.info(
        "[%s] Step-up interrupt: tool=%s scope=%s",
        auth.request_id, tool_name, required_scope,
    )

    step_up = await _get_step_up_token(
        app.state.http_client, auth.user_sub, session_id, required_scope,
    )
    if not step_up.token:
        return _step_up_required_response(
            StepUpRequired(
                tool_name=tool_name,
                required_scope=required_scope,
                www_authenticate=challenge.get("www_authenticate", ""),
                message=challenge.get("message", ""),
            ),
            session_id, auth.request_id, step_up.consent_url,
        )

    return await app.state.agent.ainvoke(Command(resume=step_up.token), config=config)


def _extract_final_text(messages: list) -> str:
    for msg in reversed(messages):
        if not (isinstance(msg, AIMessage) and msg.content):
            continue
        content = msg.content
        if isinstance(content, list):
            return " ".join(_iter_text_blocks(content))
        return content
    return ""


def _iter_text_blocks(blocks: list) -> list[str]:
    out: list[str] = []
    for block in blocks:
        if isinstance(block, dict):
            if block.get("type") == "text":
                out.append(block.get("text", ""))
        else:
            out.append(str(block))
    return out


@app.get("/health")
async def health():
    return {"status": "healthy", "component": "agent-core"}


@app.get("/ready")
async def ready():
    return {"status": "ready", "component": "agent-core"}
