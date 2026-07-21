from __future__ import annotations

import base64
import html
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from asgardeo import AsgardeoConfig
from asgardeo_ai import AgentAuthManager, AgentConfig
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

SERVICE_VERSION = "3.0.0"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
STATE_TTL = int(os.getenv("OAUTH_STATE_TTL_SECONDS", "600"))
OBO_TTL = int(os.getenv("OBO_TOKEN_TTL_SECONDS", "3600"))
STEP_UP_TTL = int(os.getenv("STEP_UP_TOKEN_TTL_SECONDS", "600"))
_REFRESH_LEAD_S = 60
_DEFAULT_TTL_S = 3600

ASGARDEO_CONFIG = AsgardeoConfig(
    base_url=os.getenv("ASGARDEO_BASE_URL"),
    client_id=os.getenv("CLIENT_ID"),
    redirect_uri=os.getenv("REDIRECT_URI"),
)
AGENT_CONFIG = AgentConfig(
    agent_id=os.getenv("AGENT_ID"),
    agent_secret=os.getenv("AGENT_SECRET"),
)

# OIDC protocol scopes 
_BASE_SCOPES: list[str] = ["openid", "email"]

_INTERNAL_CALLER = "agent-core"

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format='{"time":"%(asctime)s","level":"%(levelname)s","component":"customer-service-assistant-agent-cell-auth-service","msg":"%(message)s"}',
)
logger = logging.getLogger("customer-service-assistant-agent-cell-auth-service")


oauth_states: dict[str, dict[str, Any]] = {}
obo_tokens: dict[str, dict[str, Any]] = {}
step_up_tokens: dict[str, dict[str, Any]] = {}
_agent_token_cache: dict[str, dict[str, Any]] = {}

auth_manager: AgentAuthManager | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global auth_manager

    logger.info("Starting Customer Service Assistant Agent Cell Auth Service v%s", SERVICE_VERSION)
    auth_manager = AgentAuthManager(ASGARDEO_CONFIG, AGENT_CONFIG)
    await auth_manager.__aenter__()
    logger.info("Auth manager initialized successfully")

    try:
        yield
    finally:
        if auth_manager is not None:
            await auth_manager.__aexit__(None, None, None)
        logger.info("Customer Service Assistant Agent Cell Auth Service shut down")


app = FastAPI(title="Customer Service Assistant Agent Cell Auth Service", version=SERVICE_VERSION, lifespan=lifespan)


def _require_internal_caller(x_source_service: str | None) -> None:
    if x_source_service != _INTERNAL_CALLER:
        raise HTTPException(status_code=403, detail=f"Only {_INTERNAL_CALLER} may call this endpoint")


def _jwt_claim(token: str, claim: str) -> str:
    # Envoy already validated the JWT; this is a claim-extraction shortcut.
    try:
        part = token.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        val = payload.get(claim, "")
    except Exception:
        return ""
    return " ".join(val) if isinstance(val, list) else str(val)


def _get_attr_or_key(obj: Any, name: str, default: Any = None) -> Any:
    if hasattr(obj, name):
        return getattr(obj, name)
    if isinstance(obj, dict):
        return obj.get(name, default)
    return default


def _purge_if_expired(store: dict[str, dict[str, Any]], key: str) -> dict[str, Any] | None:
    entry = store.get(key)
    if entry and time.time() >= entry["expires_at"]:
        store.pop(key, None)
        return None
    return entry


def _token_response(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "access_token": entry["access_token"],
        "token_type": "Bearer",
        "expires_in": int(entry["expires_at"] - time.time()),
        "scope": " ".join(entry["scopes"]),
    }


def _start_consent_flow(
    user_sub: str, scopes: list[str], reason: str, is_step_up: bool,
) -> JSONResponse:
    pkce_kwargs = {"scopes": scopes}
    if is_step_up:
        pkce_kwargs["prompt"] = "consent"

    auth_url, state, code_verifier = auth_manager.get_authorization_url_with_pkce(**pkce_kwargs)
    oauth_states[state] = {
        "code_verifier": code_verifier,
        "request_id": str(uuid.uuid4()),
        "created_at": time.time(),
        "step_up": is_step_up,
        "scopes": scopes,
    }
    logger.info(
        "Consent flow started",
        extra={"user": user_sub, "scopes": scopes, "reason": reason},
    )
    return JSONResponse(
        status_code=401,
        content={
            "error": "consent_required",
            "user_sub": user_sub,
            "consent_url": auth_url,
            "required_scopes": scopes,
        },
    )


_CALLBACK_HTML_TEMPLATE = """<!DOCTYPE html><html><head><title>Authorized</title>
<style>body{{font-family:Arial,sans-serif;display:flex;justify-content:center;
align-items:center;height:100vh;margin:0;background:#f5f5f5}}
.box{{background:#fff;padding:2rem;border-radius:10px;
box-shadow:0 4px 20px rgba(0,0,0,.1);text-align:center}}</style>
</head><body><div class='box'><h2>&#10003; Authorization Successful</h2>
<p>Returning to the application…</p>
<p><small>User: {user_sub_escaped}</small></p></div>
<script>
(function(){{
var isStepUp={is_step_up_js};
if(window.opener){{
try{{window.opener.postMessage({{type:'agent_consent_complete',step_up:isStepUp}},'*');}}catch(e){{}}
setTimeout(function(){{window.close();}},300);
}}
}})();
</script>
</body></html>"""


def _render_callback_page(user_sub: str, is_step_up: bool) -> HTMLResponse:
    return HTMLResponse(_CALLBACK_HTML_TEMPLATE.format(
        user_sub_escaped=html.escape(user_sub),
        is_step_up_js="true" if is_step_up else "false",
    ))


@app.get("/oauth/callback")
async def auth_callback(
    request: Request,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
):
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))

    if error:
        logger.error("IdP returned error", extra={"error": error})
        safe_error = html.escape(error)
        return HTMLResponse(
            f"<!DOCTYPE html><html><body><h1>Authorization Failed</h1>"
            f"<p>{safe_error}</p><p>You may close this window.</p></body></html>",
            status_code=400,
        )

    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state")

    state_data = oauth_states.pop(state, None)
    if not state_data or time.time() - state_data["created_at"] > STATE_TTL:
        raise HTTPException(status_code=400, detail="Invalid or expired state parameter")

    try:
        agent_token = await auth_manager.get_agent_token(state_data["scopes"])
        obo_response = await auth_manager.get_obo_token(
            code,
            agent_token=agent_token,
            code_verifier=state_data["code_verifier"],
        )
    except Exception as exc:
        logger.error("Token exchange failed", extra={"error": str(exc), "request_id": request_id})
        raise HTTPException(status_code=502, detail=f"Token exchange with IdP failed: {exc}")

    access_token = _get_attr_or_key(obo_response, "access_token", "")
    if not access_token:
        raise HTTPException(status_code=502, detail="No access_token in OBO response")

    user_sub = _jwt_claim(access_token, "sub")
    granted_scopes = _jwt_claim(access_token, "scope").split()
    expires_in = _get_attr_or_key(obo_response, "expires_in", OBO_TTL) or OBO_TTL

    is_step_up = bool(state_data["step_up"])
    target_store = step_up_tokens if is_step_up else obo_tokens
    target_ttl = STEP_UP_TTL if is_step_up else expires_in
    target_store[user_sub] = {
        "access_token": access_token,
        "scopes": granted_scopes,
        "expires_at": time.time() + target_ttl,
    }

    logger.info("OBO token cached | key=%s scopes=%s step_up=%s", user_sub, granted_scopes, is_step_up)
    return _render_callback_page(user_sub, is_step_up)


@app.delete("/oauth/logout")
async def logout(x_user_sub: str | None = Header(default=None)):
    if x_user_sub:
        obo_tokens.pop(x_user_sub, None)
        step_up_tokens.pop(x_user_sub, None)
    return {"status": "logged_out", "user_sub": x_user_sub or ""}


class OBOTokenRequest(BaseModel):
    user_sub: str
    scopes: list[str]
    session_id: str = ""
    required_scope: str | None = None
    step_up: bool = False


@app.post("/auth/obo-token")
async def get_obo_token(
    body: OBOTokenRequest,
    x_source_service: str | None = Header(default=None),
):
    # Baseline token is returned even if narrower than requested; step-up handles the rest.
    _require_internal_caller(x_source_service)

    if body.step_up:
        return _handle_step_up_request(body)
    return _handle_baseline_request(body)


def _handle_step_up_request(body: OBOTokenRequest) -> Any:
    logger.info(
        "Step-up token lookup | requested_sub=%s cached_keys=%s",
        body.user_sub, list(step_up_tokens.keys()),
    )
    entry = _purge_if_expired(step_up_tokens, body.user_sub)
    if entry is None or body.required_scope not in entry["scopes"]:
        return _start_consent_flow(
            body.user_sub, _BASE_SCOPES + body.scopes, "step up", is_step_up=True,
        )
    return _token_response(entry)


def _handle_baseline_request(body: OBOTokenRequest) -> Any:
    logger.info(
        "OBO token lookup | requested_sub=%s cached_keys=%s",
        body.user_sub, list(obo_tokens.keys()),
    )
    entry = _purge_if_expired(obo_tokens, body.user_sub)
    if entry is None:
        return _start_consent_flow(
            body.user_sub, _BASE_SCOPES + body.scopes, "initial", is_step_up=False,
        )
    return _token_response(entry)


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
    return {"status": "healthy", "component": "auth-service", "state_backend": "in-memory"}
