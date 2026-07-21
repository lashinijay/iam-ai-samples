from __future__ import annotations

import asyncio
import json
import logging
from contextvars import ContextVar
from typing import Awaitable, Callable

import httpx
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.interceptors import MCPToolCallRequest
from mcp.types import CallToolResult, TextContent

from src.step_up_helpers import (
    parse_step_up_from_tool_result,
    parse_www_authenticate,
)

logger = logging.getLogger("agent-core")

invoker_context: ContextVar[dict | None] = ContextVar("invoker_context", default=None)

_STEP_UP_ERROR = "step_up_authentication_required"


def _unwrap_http_error(exc: BaseException) -> httpx.HTTPStatusError | None:
    # MCP SSE transport raises through a TaskGroup, wrapping in an ExceptionGroup.
    if isinstance(exc, httpx.HTTPStatusError):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            found = _unwrap_http_error(sub)
            if found is not None:
                return found
    return None


def _safe_json(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _gateway_error_result(request: MCPToolCallRequest, http_exc: httpx.HTTPStatusError) -> CallToolResult:
    # Must never re-raise: an escaping ExceptionGroup surfaces as "unhandled errors
    # in a TaskGroup", losing the actual gateway status the LLM needs to see.
    status = http_exc.response.status_code
    www_auth = http_exc.response.headers.get("www-authenticate", "")
    body = _safe_json(http_exc.response)
    www_auth_params = parse_www_authenticate(www_auth)

    error_type = body.get("error") or www_auth_params.get("error") or f"Authorization failed with HTTP {status}"
    message = body.get("message") or www_auth_params.get("error_description") or f"Unauthorized tool call failed with HTTP {status}"

    if error_type == _STEP_UP_ERROR or "Step-up consent required" in message:
        return _build_step_up_payload(request, body, www_auth, www_auth_params, message)

    logger.warning("HTTP %d from gateway: tool=%s error=%s", status, request.name, error_type)
    payload: dict = {
        "error": error_type,
        "tool": request.name,
        "http_status": status,
        "message": message,
    }
    return _wrap_json(payload)


def _build_step_up_payload(
    request: MCPToolCallRequest,
    body: dict,
    www_auth: str,
    www_auth_params: dict[str, str],
    message: str,
) -> CallToolResult:
    required_scope = body.get("required_scope") or www_auth_params.get("scope") or ""
    logger.info("Step-up challenge: tool=%s scope=%s", request.name, required_scope)

    payload: dict = {"error": _STEP_UP_ERROR, "tool": request.name, "message": message}
    if required_scope:
        payload["required_scope"] = required_scope
    if www_auth:
        payload["www_authenticate"] = www_auth
    return _wrap_json(payload)


def _wrap_json(payload: dict) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))])


def _auth_headers(ctx: dict) -> dict[str, str]:
    headers: dict[str, str] = {}
    if ctx.get("obo_token"):
        headers["Authorization"] = f"Bearer {ctx['obo_token']}"
    for key, header in (("agent_id", "x-agent-id"), ("request_id", "x-request-id"), ("traceparent", "traceparent")):
        if ctx.get(key):
            headers[header] = ctx[key]
    return headers


async def _auth_and_step_up_interceptor(
    request: MCPToolCallRequest,
    handler: Callable[[MCPToolCallRequest], Awaitable[CallToolResult]],
) -> CallToolResult:
    ctx = invoker_context.get() or {}
    headers = _auth_headers(ctx)
    if headers:
        request = request.override(headers=headers)

    try:
        result = await handler(request)
    except (httpx.HTTPStatusError, BaseExceptionGroup) as exc:
        http_exc = _unwrap_http_error(exc)
        if http_exc is None:
            raise
        return _gateway_error_result(request, http_exc)

    _log_embedded_step_up(request.name, result)
    return result


def _log_embedded_step_up(tool_name: str, result: CallToolResult) -> None:
    for item in result.content or []:
        if not hasattr(item, "text"):
            continue
        step_up = parse_step_up_from_tool_result(tool_name, item.text)
        if step_up is not None:
            logger.info("Step-up in tool result: tool=%s scope=%s", tool_name, step_up.required_scope)
            return


class ToolManager:
    
    def __init__(self, url: str | None, allowlist: list[str] | None = None):
        self._base_url = url.rstrip("/") if url else None
        self._client: MultiServerMCPClient | None = None
        self._tools: list = []
        self._allowlist: frozenset[str] | None = (
            frozenset(allowlist) if allowlist else None
        )

    async def connect(self) -> None:
        if not self._base_url:
            logger.info("No INTERCELL_GW_URL configured — starting without MCP tools")
            return

        # Retry on startup so a not-yet-ready intercell-gw doesn't leave the
        # agent permanently toolless. 
        attempts, delay, max_delay = 8, 1.0, 30.0
        for attempt in range(1, attempts + 1):
            try:
                client = MultiServerMCPClient(
                    {"tools": {"transport": "http", "url": f"{self._base_url}/mcp"}},
                    tool_interceptors=[_auth_and_step_up_interceptor],
                )
                discovered = await client.get_tools()
                self._tools = self._apply_allowlist(discovered)
                self._client = client
                logger.info(
                    "Connected to MCP aggregator at %s, loaded %d tools: %s",
                    self._base_url, len(self._tools), [t.name for t in self._tools],
                )
                return
            except (SystemExit, KeyboardInterrupt):
                raise
            except BaseException as exc:
                # Clear pending cancels so a failed MCP handshake doesn't kill
                # startup or poison the next retry attempt.
                current = asyncio.current_task()
                if current is not None:
                    while current.cancelling() > 0:
                        current.uncancel()

                if attempt < attempts:
                    logger.warning(
                        "MCP connect attempt %d/%d failed (%s); retrying in %.1fs",
                        attempt, attempts, exc.__class__.__name__, delay,
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, max_delay)
                    continue

                logger.exception(
                    "Failed to connect to MCP aggregator after %d attempts, starting without tools",
                    attempts,
                )
                self._tools = []
                return

    async def disconnect(self) -> None:
        self._client = None
        self._tools = []

    def _apply_allowlist(self, tools: list) -> list:
        if self._allowlist is None:
            return list(tools)
        kept = [t for t in tools if t.name in self._allowlist]
        dropped = sorted({t.name for t in tools} - self._allowlist)
        if dropped:
            logger.info("Tool allowlist filtered out: %s", dropped)
        return kept

    @property
    def tools(self) -> list:
        return list(self._tools)
