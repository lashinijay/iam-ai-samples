"""Tools Service — MCP server for Customer Support + Billing tools.

Scope → tool mapping (enforced by OPA):
    crm:read           — get_customer_profile
    crm:write          — update_customer_profile
    billing:read       — get_billing_history, get_charge_details
    billing:refund     — issue_refund          (step-up required)
    tickets:write      — create_support_ticket
    escalation:trigger — trigger_escalation    (step-up required)
"""

from __future__ import annotations

import json
import logging
import os
import random
from datetime import datetime, timedelta, timezone

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","component":"tools-service","msg":"%(message)s"}',
)
logger = logging.getLogger("tools-service")

mcp = FastMCP(
    "Tools Service",
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

MAX_OUTPUT_BYTES = 32 * 1024
_CUSTOMER_ID_MAX = 64
_CHARGE_ID_MAX = 128
_TICKET_ID_MAX = 128
_REASON_MAX = 1000
_REFUND_MAX_USD = 10_000

ALLOWED_CRM_FIELDS = frozenset({"email", "phone", "name", "plan", "notes"})
VALID_PRIORITIES = frozenset({"low", "normal", "high", "urgent"})
VALID_ESCALATION_LEVELS = frozenset({"tier2", "tier3", "manager", "legal", "fraud"})
SLA_RESPONSE_HOURS = {"tier2": 4, "tier3": 2, "manager": 1, "legal": 24, "fraud": 1}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _err(tool: str, message: str) -> str:
    return json.dumps({"tool": tool, "error": message})


def _ok(payload: dict) -> str:
    return json.dumps(payload)


def _validate_id(value: str, *, field_name: str, max_len: int) -> str | None:
    if not value or len(value) > max_len:
        return f"Invalid {field_name}"
    return None


def _validate_string_field(value: str, *, field_name: str, max_len: int) -> str | None:
    if not value or len(value) > max_len:
        return f"{field_name} must be 1-{max_len} characters"
    return None


@mcp.tool()
def get_customer_profile(customer_id: str) -> str:
    """Retrieve a customer's profile from the CRM.

    Args:
        customer_id: The unique customer identifier (e.g. "cust_12345").
    """
    err = _validate_id(customer_id, field_name="customer_id", max_len=_CUSTOMER_ID_MAX)
    if err:
        return _err("get_customer_profile", err)

    logger.info("get_customer_profile: customer_id='%s'", customer_id)
    return _ok({
        "tool": "get_customer_profile",
        "customer_id": customer_id,
        "name": "Jane Smith",
        "email": "jane.smith@example.com",
        "phone": "+1-555-0100",
        "plan": "Pro",
        "account_status": "active",
        "customer_since": "2022-03-15",
        "lifetime_value_usd": 1480.00,
    })


@mcp.tool()
def update_customer_profile(customer_id: str, field: str, value: str) -> str:
    """Update a field on a customer's CRM record.

    Args:
        customer_id: The unique customer identifier.
        field: CRM field to update. Allowed: email, phone, name, plan, notes.
        value: New value for the field (max 500 characters).
    """
    err = _validate_id(customer_id, field_name="customer_id", max_len=_CUSTOMER_ID_MAX)
    if err:
        return _err("update_customer_profile", err)
    if field not in ALLOWED_CRM_FIELDS:
        return _err(
            "update_customer_profile",
            f"Field '{field}' is not updatable. Allowed fields: {sorted(ALLOWED_CRM_FIELDS)}",
        )
    err = _validate_string_field(value, field_name="value", max_len=500)
    if err:
        return _err("update_customer_profile", err)

    logger.info("update_customer_profile: customer_id='%s' field='%s'", customer_id, field)
    return _ok({
        "tool": "update_customer_profile",
        "success": True,
        "customer_id": customer_id,
        "updated_field": field,
        "new_value": value,
    })


@mcp.tool()
def get_billing_history(customer_id: str, months: int = 3) -> str:
    """Retrieve the billing history for a customer.

    Args:
        customer_id: The unique customer identifier.
        months: Number of past months to include (1-12, default 3).
    """
    err = _validate_id(customer_id, field_name="customer_id", max_len=_CUSTOMER_ID_MAX)
    if err:
        return _err("get_billing_history", err)
    months = max(1, min(12, months))

    logger.info("get_billing_history: customer_id='%s' months=%d", customer_id, months)
    today = _utcnow()
    charges = [
        {
            "charge_id": f"ch_{customer_id}_{i:03d}",
            "date": (today - timedelta(days=30 * i)).strftime("%Y-%m-%d"),
            "description": "Pro Plan — Monthly Subscription",
            "amount_usd": 49.99,
            "status": "paid",
            "payment_method": "Visa ending 4242",
        }
        for i in range(months)
    ]
    return _ok({"tool": "get_billing_history", "customer_id": customer_id, "charges": charges})


@mcp.tool()
def get_charge_details(customer_id: str, charge_id: str) -> str:
    """Retrieve full details for a specific charge on a customer's account.

    Args:
        customer_id: The unique customer identifier.
        charge_id: The charge identifier (e.g. "ch_cust_12345_001").
    """
    err = (
        _validate_id(customer_id, field_name="customer_id", max_len=_CUSTOMER_ID_MAX)
        or _validate_id(charge_id, field_name="charge_id", max_len=_CHARGE_ID_MAX)
    )
    if err:
        return _err("get_charge_details", err)

    logger.info("get_charge_details: customer_id='%s' charge_id='%s'", customer_id, charge_id)
    if customer_id not in charge_id:
        return _err("get_charge_details", "Charge not found for this customer")

    now = _utcnow()
    return _ok({
        "tool": "get_charge_details",
        "charge_id": charge_id,
        "customer_id": customer_id,
        "date": now.strftime("%Y-%m-%d"),
        "description": "Pro Plan — Monthly Subscription",
        "amount_usd": 49.99,
        "status": "paid",
        "payment_method": "Visa ending 4242",
        "invoice_url": f"https://billing.example.com/invoices/{charge_id}",
        "refundable": True,
        "refund_deadline": (now + timedelta(days=30)).strftime("%Y-%m-%d"),
    })


@mcp.tool()
def issue_refund(customer_id: str, charge_id: str, amount_usd: float, reason: str) -> str:
    """Issue a (partial or full) refund for a charge on a customer's account.

    Args:
        customer_id: The unique customer identifier.
        charge_id: The charge to refund.
        amount_usd: Refund amount in USD (must be > 0 and <= original charge amount).
        reason: Human-readable reason for the refund (max 500 characters).
    """
    err = (
        _validate_id(customer_id, field_name="customer_id", max_len=_CUSTOMER_ID_MAX)
        or _validate_id(charge_id, field_name="charge_id", max_len=_CHARGE_ID_MAX)
    )
    if err:
        return _err("issue_refund", err)
    if amount_usd <= 0 or amount_usd > _REFUND_MAX_USD:
        return _err("issue_refund", f"amount_usd must be between 0 and {_REFUND_MAX_USD}")
    err = _validate_string_field(reason, field_name="reason", max_len=500)
    if err:
        return _err("issue_refund", err)
    if customer_id not in charge_id:
        return _err("issue_refund", "Charge not found for this customer")

    logger.info(
        "issue_refund: customer_id='%s' charge_id='%s' amount=%.2f",
        customer_id, charge_id, amount_usd,
    )
    return _ok({
        "tool": "issue_refund",
        "success": True,
        "refund_id": f"re_{charge_id}",
        "customer_id": customer_id,
        "charge_id": charge_id,
        "amount_refunded_usd": amount_usd,
        "reason": reason,
        "estimated_arrival": (_utcnow() + timedelta(days=5)).strftime("%Y-%m-%d"),
    })


@mcp.tool()
def create_support_ticket(
    customer_id: str,
    subject: str,
    description: str,
    priority: str = "normal",
) -> str:
    """Create a support ticket for a customer interaction.

    Args:
        customer_id: The unique customer identifier.
        subject: Short summary of the issue (max 200 characters).
        description: Full description of the issue and steps taken (max 4000 characters).
        priority: Ticket priority — low, normal, high, or urgent (default: normal).
    """
    err = (
        _validate_id(customer_id, field_name="customer_id", max_len=_CUSTOMER_ID_MAX)
        or _validate_string_field(subject, field_name="subject", max_len=200)
        or _validate_string_field(description, field_name="description", max_len=4000)
    )
    if err:
        return _err("create_support_ticket", err)
    if priority not in VALID_PRIORITIES:
        return _err(
            "create_support_ticket",
            f"Invalid priority. Must be one of: {sorted(VALID_PRIORITIES)}",
        )

    logger.info(
        "create_support_ticket: customer_id='%s' priority='%s' subject='%s'",
        customer_id, priority, subject[:60],
    )
    return _ok({
        "tool": "create_support_ticket",
        "success": True,
        "ticket_id": f"tkt_{customer_id}_{random.randint(10000, 99999)}",
        "customer_id": customer_id,
        "subject": subject,
        "priority": priority,
        "status": "open",
        "created_at": _utcnow().isoformat().replace("+00:00", "Z"),
    })


@mcp.tool()
def trigger_escalation(ticket_id: str, reason: str, escalation_level: str = "tier2") -> str:
    """Escalate an existing support ticket to a higher support tier or team.

    Args:
        ticket_id: The ticket to escalate (e.g. "tkt_cust_12345_55123").
        reason: Why escalation is needed (max 1000 characters).
        escalation_level: Target team — tier2, tier3, manager, legal, fraud (default: tier2).
    """
    err = (
        _validate_id(ticket_id, field_name="ticket_id", max_len=_TICKET_ID_MAX)
        or _validate_string_field(reason, field_name="reason", max_len=_REASON_MAX)
    )
    if err:
        return _err("trigger_escalation", err)
    if escalation_level not in VALID_ESCALATION_LEVELS:
        return _err(
            "trigger_escalation",
            f"Invalid escalation_level. Must be one of: {sorted(VALID_ESCALATION_LEVELS)}",
        )

    logger.info("trigger_escalation: ticket_id='%s' level='%s'", ticket_id, escalation_level)
    return _ok({
        "tool": "trigger_escalation",
        "success": True,
        "ticket_id": ticket_id,
        "escalated_to": escalation_level,
        "reason": reason,
        "escalated_at": _utcnow().isoformat().replace("+00:00", "Z"),
        "sla_response_hours": SLA_RESPONSE_HOURS[escalation_level],
    })


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


if __name__ == "__main__":
    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "8090"))
    uvicorn.run(mcp.streamable_http_app(), host=host, port=port)
