from __future__ import annotations

import logging
import uuid
from typing import Any

from a2a.helpers.proto_helpers import (
    new_task_from_user_message,
    new_text_artifact_update_event,
    new_text_status_update_event,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import TaskState, TaskStatus, TaskStatusUpdateEvent
from langchain_core.messages import AIMessage, HumanMessage

from src.tools import ToolManager

logger = logging.getLogger("agent2-core")

_RECURSION_LIMIT = 25
_BEARER_PREFIX = "bearer "


def _request_headers(context: RequestContext) -> dict:
    if context.call_context and context.call_context.state:
        return context.call_context.state.get("headers", {}) or {}
    return {}


def _extract_token(headers: dict) -> str | None:
    auth = headers.get("authorization", "")
    return auth[len(_BEARER_PREFIX):] if auth.lower().startswith(_BEARER_PREFIX) else None


def _final_text_from_messages(messages: list) -> str:
    for msg in reversed(messages):
        if not isinstance(msg, AIMessage):
            continue
        # Skip tool-call-only AIMessages; tools_condition routes to END only when tool_calls is empty.
        if getattr(msg, "tool_calls", None):
            continue
        text = _flatten_content(msg.content)
        if text:
            return text
    return ""


def _flatten_content(content: Any) -> str:
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
            else:
                parts.append(str(item))
        return " ".join(p for p in parts if p).strip()
    return str(content).strip() if content else ""


class Agent2Executor(AgentExecutor):
    def __init__(self, agent, tool_manager: ToolManager, agent_id: str, model: str):
        self._agent = agent
        self._tool_manager = tool_manager
        self._agent_id = agent_id
        self._model = model

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if not context.message:
            raise ValueError("No message provided")

        query = context.get_user_input()
        task = context.current_task
        if not task:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)

        await event_queue.enqueue_event(
            new_text_status_update_event(
                task_id=task.id,
                context_id=task.context_id,
                state=TaskState.TASK_STATE_WORKING,
                text="Processing your request…",
            )
        )

        request_id = str(uuid.uuid4())
        headers = _request_headers(context)
        config = {
            "configurable": {
                "thread_id": task.context_id or task.id,
                "model": self._model,
                "request_id": request_id,
                "traceparent": headers.get("traceparent", ""),
                "agent_id": self._agent_id,
                "obo_token": _extract_token(headers),
            },
            "recursion_limit": _RECURSION_LIMIT,
        }

        logger.info(
            "task received: task_id=%s context_id=%s model=%s query=%.120s",
            task.id, task.context_id, self._model, query,
        )

        try:
            logger.info(
                "agent invocation start: request_id=%s task_id=%s thread_id=%s",
                request_id, task.id, config["configurable"]["thread_id"],
            )
            result = await self._agent.ainvoke(
                {"messages": [HumanMessage(content=query)]}, config=config,
            )
            final_text = _final_text_from_messages(result["messages"])

            await event_queue.enqueue_event(
                new_text_artifact_update_event(
                    task_id=task.id,
                    context_id=task.context_id,
                    name="result",
                    text=final_text,
                    last_chunk=True,
                )
            )
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    task_id=task.id,
                    context_id=task.context_id,
                    status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
                )
            )
            logger.info(
                "task completed: task_id=%s request_id=%s response_chars=%d",
                task.id, request_id, len(final_text),
            )

        except Exception as exc:
            logger.error("[%s] agent execution error: %s", request_id, exc, exc_info=True)
            await event_queue.enqueue_event(
                new_text_status_update_event(
                    task_id=task.id,
                    context_id=task.context_id,
                    state=TaskState.TASK_STATE_FAILED,
                    text=f"Error: {exc}",
                )
            )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("cancel is not supported")
