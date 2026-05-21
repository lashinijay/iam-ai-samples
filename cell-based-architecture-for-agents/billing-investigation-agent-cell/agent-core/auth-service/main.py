"""
This cell is a backend-only A2A sub-agent. Delegated tokens arrive on
the Authorization header from the parent cell and are forwarded straight
through to downstream tools by agent-core. The current implementation is 
to mint agent-identity tokens for flows where the agent 
must act as itself rather than on behalf of a user.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

from asgardeo import AsgardeoConfig
from asgardeo_ai import AgentAuthManager, AgentConfig
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

SERVICE_VERSION = "2.0.0"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
_REFRESH_LEAD_S = 60
_DEFAULT_TTL_S = 3600

ASGARDEO_CONFIG = AsgardeoConfig(
    base_url=os.getenv("ASGARDEO_BASE_URL"),
    client_id=os.getenv("CLIENT_ID"),
    client_secret=os.getenv("CLIENT_SECRET"),
)
AGENT_CONFIG = AgentConfig(
    agent_id=os.getenv("AGENT_ID"),
    agent_secret=os.getenv("AGENT_SECRET"),
)

_INTERNAL_CALLER = "agent2-core"

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format='{"time":"%(asctime)s","level":"%(levelname)s","component":"billing-investigation-agent-cell-auth-service","msg":"%(message)s"}',
)
logger = logging.getLogger("billing-investigation-agent-cell-auth-service")


auth_manager: AgentAuthManager | None = None
_agent_token_cache: dict[str, dict[str, Any]] = {}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global auth_manager

    logger.info("Starting Billing Investigation Agent Cell Auth Service", extra={"version": SERVICE_VERSION})
    auth_manager = AgentAuthManager(ASGARDEO_CONFIG, AGENT_CONFIG)
    await auth_manager.__aenter__()
    logger.info("Auth manager initialized successfully")

    try:
        yield
    finally:
        if auth_manager is not None:
            await auth_manager.__aexit__(None, None, None)
        logger.info("Billing Investigation Agent Cell Auth Service shut down")


app = FastAPI(title="Billing Investigation Agent Cell Auth Service", version=SERVICE_VERSION, lifespan=lifespan)


def _require_internal_caller(x_source_service: str | None) -> None:
    if x_source_service != _INTERNAL_CALLER:
        raise HTTPException(status_code=403, detail=f"Only {_INTERNAL_CALLER} may call this endpoint")


class AgentTokenRequest(BaseModel):
    scopes: list[str]


async def _refresh_token(scopes: list[str]) -> None:
    key = AGENT_CONFIG.agent_id
    token = await auth_manager.get_agent_token(scopes)
    if isinstance(token, dict):
        access_token = token.get("access_token", "")
        expires_in = token.get("expires_in", _DEFAULT_TTL_S) or _DEFAULT_TTL_S
    else:
        access_token = getattr(token, "access_token", None) or str(token)
        expires_in = getattr(token, "expires_in", _DEFAULT_TTL_S) or _DEFAULT_TTL_S
    _agent_token_cache[key] = {
        "access_token": access_token,
        "expires_at": time.time() + expires_in,
    }
    logger.info("Agent token refreshed, expires_at=%.0f", _agent_token_cache[key]["expires_at"])


@app.post("/auth/agent-token")
async def get_agent_token(
    body: AgentTokenRequest,
    x_source_service: str | None = Header(default=None),
):
    _require_internal_caller(x_source_service)

    key = AGENT_CONFIG.agent_id
    scope_list = ["openid"] + body.scopes

    try:
        entry = _agent_token_cache.get(key)
        if entry is None or time.time() >= entry["expires_at"] - _REFRESH_LEAD_S:
            await _refresh_token(scope_list)
            entry = _agent_token_cache[key]
    except Exception as exc:
        logger.error("get_agent_token failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=502, detail="Failed to obtain agent token")

    return {
        "access_token": entry["access_token"],
        "token_type": "Bearer",
        "expires_in": int(entry["expires_at"] - time.time()),
    }


@app.get("/health")
async def health():
    return {"status": "healthy", "component": "billing-investigation-agent-cell-auth-service"}
