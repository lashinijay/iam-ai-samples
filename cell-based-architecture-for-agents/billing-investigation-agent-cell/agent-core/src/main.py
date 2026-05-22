from __future__ import annotations

import asyncio
import logging
import os

import uvicorn
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    HTTPAuthSecurityScheme,
    SecurityRequirement,
)
from langgraph.checkpoint.memory import MemorySaver
from starlette.applications import Starlette
from starlette.responses import JSONResponse

from src.agent import build_agent
from src.agent_executor import Agent2Executor
from src.tools import ToolManager

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
INTERCELL_GW_URL = os.getenv("INTERCELL_GW_URL")
MODEL = os.getenv("MODEL", "google/gemini-2.5-flash")
AGENT_ID = os.getenv("AGENT_ID")

TOOL_ALLOWLIST: list[str] = [
    "get_billing_history",
    "get_charge_details",
]
AGENT_A2A_URL = os.getenv(
    "AGENT_A2A_URL",
    "http://intercell-gw.billing-investigation-agent-cell.svc.cluster.local:8080/a2a/",
)
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))

_HEALTH_PATHS = frozenset({"/health", "/ready"})

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format='{"time":"%(asctime)s","level":"%(levelname)s","component":"agent2-core","msg":"%(message)s"}',
)
logger = logging.getLogger("agent2-core")


class HealthMiddleware:
    def __init__(self, app):
        self._app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path") in _HEALTH_PATHS:
            response = JSONResponse({"status": "healthy", "component": "agent2-core"})
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)


def build_agent_card() -> AgentCard:
    skill = AgentSkill(
        id="billing-investigation",
        name="Billing Investigation",
        description=(
            "Retrieve billing history, itemise charges, and identify discrepancies "
            "for a given customer account to support dispute resolution and charge verification."
        ),
        tags=["billing", "billing:read"],
        examples=[
            "Investigate billing for customer cust_12345. They dispute a $49.99 charge on 2026-03-01.",
            "Retrieve the last 3 months of billing history for customer cust_67890.",
        ],
    )

    card = AgentCard(
        name="Billing Investigation Agent",
        description=(
            "Retrieves and analyses billing history and charge details for a given customer. "
            "Surfaces findings to the delegating agent to support dispute resolution, charge "
            "verification, and billing account reviews."
        ),
        version="1.0.0",
        supported_interfaces=[
            AgentInterface(protocol_binding="JSONRPC", url=AGENT_A2A_URL),
        ],
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        skills=[skill],
    )

    card.security_schemes["Bearer"].http_auth_security_scheme.CopyFrom(
        HTTPAuthSecurityScheme(
            scheme="bearer",
            bearer_format="JWT",
            description="OAuth 2.0 JWT token with 'billing:read' scope required",
        )
    )

    requirement = SecurityRequirement()
    requirement.schemes["Bearer"].list.extend(["billing:read"])
    card.security_requirements.append(requirement)

    return card


async def build_asgi_app():
    tool_manager = ToolManager(INTERCELL_GW_URL, allowlist=TOOL_ALLOWLIST)
    if INTERCELL_GW_URL:
        await tool_manager.connect()

    agent = build_agent(tool_manager, MemorySaver())
    agent_card = build_agent_card()
    executor = Agent2Executor(agent, tool_manager, AGENT_ID, MODEL)

    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
        agent_card=agent_card,
    )

    routes = [
        *create_agent_card_routes(agent_card),
        *create_jsonrpc_routes(handler, rpc_url="/"),
    ]
    starlette_app = Starlette(routes=routes)

    logger.info(
        "Agent2 Core initialised — tools=%d agent_id=%s url=%s",
        len(tool_manager.tools), AGENT_ID, AGENT_A2A_URL,
    )
    return HealthMiddleware(starlette_app)


async def _serve() -> None:
    asgi_app = await build_asgi_app()
    config = uvicorn.Config(app=asgi_app, host=HOST, port=PORT, log_level=LOG_LEVEL.lower())
    logger.info("Starting Agent2 Core on %s:%d", HOST, PORT)
    await uvicorn.Server(config).serve()


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
