from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger("agent-core")

_STEP_UP_ERROR = "step_up_authentication_required"
_WWW_AUTH_PARAM_RE = re.compile(r'(\w+)="([^"]*)"')


def parse_www_authenticate(header: str) -> dict[str, str]:
    return dict(_WWW_AUTH_PARAM_RE.findall(header or ""))


@dataclass(frozen=True, slots=True)
class StepUpRequired:
    tool_name: str
    required_scope: str
    www_authenticate: str
    message: str = ""


def parse_step_up_from_tool_result(tool_name: str, result: dict | str) -> StepUpRequired | None:
    payload = _coerce_to_dict(result)
    if payload is None or payload.get("error") != _STEP_UP_ERROR:
        return None

    www_authenticate = payload.get("www_authenticate", "")
    required_scope = payload.get("required_scope") or parse_www_authenticate(www_authenticate).get("scope", "")

    if not required_scope:
        logger.warning("Step-up challenge from tool %s missing required_scope", tool_name)
        return None

    logger.info("Step-up auth detected: tool=%s required_scope=%s", tool_name, required_scope)

    return StepUpRequired(
        tool_name=tool_name,
        required_scope=required_scope,
        www_authenticate=www_authenticate,
        message=payload.get("message", ""),
    )


def _coerce_to_dict(value: dict | str) -> dict | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None
