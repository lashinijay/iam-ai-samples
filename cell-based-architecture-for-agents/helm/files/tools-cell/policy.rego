# Tools Cell — OPA Authorization Policy
#
# Every request entering through the tools-ingress-gw is evaluated here
# before reaching the Tools Service (MCP server). 
#
# Defence layers:
#
#   1. Transport identity  — X.509-SVID (SPIRE mTLS) validated by Envoy at the
#      TLS handshake. Only the specific inter-cell gateway SVIDs are allowed
#      (exact SAN match in Envoy downstream TLS config). OPA sees only requests
#      that already passed this gate.
#
#   2. Agent identity      — The OBO JWT's act.sub claim (RFC 8693 actor).
#      Identifies the calling agent.
#
#   3. Agent permissions   — Hardcoded rules: which agent can invoke which tool.
#
#   4. Scope enforcement   — The OBO JWT's scope claim. 
#      Each tool requires a specific OAuth scope from the IdP.
#
#   5. Role enforcement    — The OBO JWT's roles claim. Certain tools require the 
#      end-user to have a specific role.
#
#   6. Step-up scope      — For high-risk tools (billing:refund,
#      escalation:trigger), the elevated scope must be present in the JWT.
#      The scope is only issued via the step-up consent flow.
#
#   7. Resource constraints — Per-tool parameter validation (refund caps,
#      allowed escalation levels).
#
# Deny responses:
#   The `allow` rule returns an object (not a boolean) on deny, so Envoy
#   sends a structured JSON body + headers back to the caller. This lets
#   agent-core's ToolManager know exactly which scope/role is missing and
#   whether a step-up consent flow is needed.

package envoy.authz

import rego.v1

# ─── DEFAULT DENY (object form for structured responses) ─────────────
# OPA Envoy plugin: when `allow` is an object with "allowed: false",
# it uses http_status, headers, and body to build the ext_authz deny.

default allow := {
    "allowed": false,
    "http_status": 403,
    "headers": {"Content-Type": "application/json"},
    "body": "{\"error\":\"authorization_denied\",\"message\":\"request denied by policy\"}",
}


# ─── AGENT IDENTITY & PERMISSIONS ────────────────────────────────────
# Agent identity comes from the OBO JWT agent_id claim.
# Permissions map: agent_id -> set of tools the agent may invoke.
#
# ${CSA_AGENT_ID} and ${BIL_AGENT_ID} are substituted at pod startup by the
# envsubst init container from the tools-opa-agents Kubernetes Secret.
# The same values come from scripts/.env (CSA_AGENT_ID, BIL_AGENT_ID) so the
# agent IDs you register in your IdP are the single source of truth.

agent_permissions := {
    # Customer Service Assistant Agent: full access to all customer-support + billing tools
    "${CSA_AGENT_ID}": {
        "get_customer_profile":    {"invoke": true},
        "update_customer_profile": {"invoke": true},
        "get_billing_history":     {"invoke": true},
        "get_charge_details":      {"invoke": true},
        "issue_refund":            {"invoke": true},
        "create_support_ticket":   {"invoke": true},
        "trigger_escalation":      {"invoke": true},
    },
    # Billing Investigation Agent: read-only billing tools only
    "${BIL_AGENT_ID}": {
        "get_billing_history":  {"invoke": true},
        "get_charge_details":   {"invoke": true},
    },
}

# ─── TOOL REQUIRED SCOPES ────────────────────────────────────────────

tool_required_scopes := {
    "get_customer_profile":    "crm:read",
    "update_customer_profile": "crm:write",
    "get_billing_history":     "billing:read",
    "get_charge_details":      "billing:read",
    "issue_refund":            "billing:refund",
    "create_support_ticket":   "tickets:write",
    "trigger_escalation":      "escalation:trigger",
}

# ─── TOOL REQUIRED ROLES ─────────────────────────────────────────────

tool_required_roles := {
    "issue_refund":            {"customer_service_admin", "admin"},
    "trigger_escalation":      {"customer_service_admin", "admin"},
}

# ─── HIGH-RISK TOOLS (require step-up consent) ───────────────────────

high_risk_scopes := {
    "billing:refund",
    "escalation:trigger",
}

# ─── RESOURCE CONSTRAINTS (per-agent) ────────────────────────────────

refund_max_amount := {
    "${CSA_AGENT_ID}": 10000,
}

escalation_allowed_levels := {
    "${CSA_AGENT_ID}": {"tier2", "tier3", "manager", "legal", "fraud"},
}

# ─── MCP PROTOCOL METHODS (no tool authorization required) ───────────

mcp_protocol_methods := {
    "initialize",
    "ping",
    "tools/list",
    "notifications/initialized",
}

# ─── INPUT HELPERS ───────────────────────────────────────────────────

http_request := input.attributes.request.http

# JWT claims forwarded by Envoy's jwt_authn filter via
# ext_authz.metadata_context_namespaces. Reading from metadata preserves the
# original claim types (arrays stay arrays, etc.), avoids the lossy header
# round-trip, and keeps the JWT payload out of headers sent to upstream.
jwt_payload := object.get(
    input,
    ["attributes", "metadataContext", "filterMetadata", "envoy.filters.http.jwt_authn", "jwt_payload"],
    {},
)

# Agent identity: innermost RFC 8693 actor claim. For a single hop this is just
# act.sub; for a delegation chain (act nested inside act inside act …) it walks
# down to the actor that actually made the call. The outer `act` entries describe
# the prior links in the chain — useful for forensics but not the active actor.
#
# Rego v1 forbids function recursion, so the walk p → p.act → p.act.act → … is
# unrolled to a fixed depth. 5 hops is far beyond what real OBO chains use.
effective_actor(p) := id if {
    p.act.act.act.act.act
    id := object.get(p.act.act.act.act.act, "sub", "")
} else := id if {
    p.act.act.act.act
    id := object.get(p.act.act.act.act, "sub", "")
} else := id if {
    p.act.act.act
    id := object.get(p.act.act.act, "sub", "")
} else := id if {
    p.act.act
    id := object.get(p.act.act, "sub", "")
} else := id if {
    p.act
    id := object.get(p.act, "sub", "")
} else := id if {
    id := object.get(p, "sub", "")
}

agent_id := effective_actor(object.get(jwt_payload, "act", {}))

# OAuth 2.0 `scope` claim — space-delimited string per RFC 6749.
jwt_scopes := {s | some s in split(object.get(jwt_payload, "scope", ""), " "); s != ""}

# `roles` claim — native JSON array, no parsing needed.
jwt_roles := {r | some r in object.get(jwt_payload, "roles", []); r != ""}

# JSON-RPC body (present on POST /mcp)
rpc_body := json.unmarshal(http_request.body)

tool_name := rpc_body.params.name if {
    rpc_body.method == "tools/call"
}

tool_args := rpc_body.params.arguments if {
    rpc_body.method == "tools/call"
}

# ─── ALLOW RULES (return {"allowed": true} on success) ──────────────

# Allow GET /mcp — MCP streamable-HTTP SSE listen stream (server-to-client notifications)
allow := {"allowed": true} if {
    http_request.method == "GET"
    startswith(http_request.path, "/mcp")
}

# Allow POST /mcp — MCP streamable-HTTP transport
allow := {"allowed": true} if {
    http_request.method == "POST"
    startswith(http_request.path, "/mcp")
    allow_rpc
}

# ─── STRUCTURED DENY RULES ──────────────────────────────────────────
# These override the default deny with specific error details.
# Order: most specific first. OPA picks the first matching rule.

# Missing JWT for tool invocation — HTTP 401
# Protocol methods (initialize, tools/list) are allowed without a token;
# tools/call requires a valid OBO token.
allow := response if {
    not is_allowed
    rpc_body.method == "tools/call"
    agent_id == ""
    response := {
        "allowed": false,
        "http_status": 401,
        "headers": {
            "WWW-Authenticate": "Bearer error=\"unauthorized\" error_description=\"Tool invocation requires a valid Authorization token\"",
            "Content-Type": "application/json",
        },
        "body": json.marshal({
            "error": "unauthorized",
            "message": "Tool invocation requires a valid Authorization token",
        }),
    }
}

# High-risk tool with scope absent — HTTP 401 step-up required.
# billing:refund and escalation:trigger can only be granted via explicit user
# consent; a missing scope means the user must approve the step-up flow.
# Role is verified before this fires — no step-up prompt for unauthorised roles.
allow := response if {
    not is_allowed
    is_high_risk_missing_scope
    required := tool_required_scopes[tool_name]
    response := {
        "allowed": false,
        "http_status": 403,
        "headers": {
            "WWW-Authenticate": sprintf(
                "Bearer scope=\"%s\" resource=\"%s\" error=\"insufficient_scope\" error_description=\"Step-up consent required to grant scope for tool %s\"",
                [required, tool_name, tool_name],
            ),
            "Content-Type": "application/json",
        },
        "body": json.marshal({
            "error": "step_up_authentication_required",
            "tool": tool_name,
            "required_scope": required,
            "message": sprintf("Tool '%s' requires elevated scope '%s'. Initiate a step-up consent flow to acquire this scope.", [tool_name, required]),
        }),
    }
}

# Insufficient scope denial — HTTP 403 (token lacks required scope, non-high-risk tools only)
allow := response if {
    not is_allowed
    not is_high_risk_missing_scope
    is_scope_denial
    required := tool_required_scopes[tool_name]
    response := {
        "allowed": false,
        "http_status": 403,
        "headers": {"Content-Type": "application/json"},
        "body": json.marshal({
            "error": "forbidden",
            "tool": tool_name,
            "message": sprintf("User doesn't have permission to invoke tool '%s'", [tool_name]),
        }),
    }
}

# Agent permission denial — HTTP 403
allow := response if {
    not is_allowed
    not is_missing_jwt
    not is_high_risk_missing_scope
    not is_scope_denial
    is_agent_permission_denial
    response := {
        "allowed": false,
        "http_status": 403,
        "headers": {"Content-Type": "application/json"},
        "body": json.marshal({
            "error": "forbidden",
            "tool": tool_name,
            "agent_id": agent_id,
            "message": sprintf("Agent '%s' is not permitted to invoke tool '%s'", [agent_id, tool_name]),
        }),
    }
}

# Generic denial — HTTP 403 with all deny reasons (resource constraints, bad path/method)
allow := response if {
    not is_allowed
    not is_missing_jwt
    not is_high_risk_missing_scope
    not is_scope_denial
    not is_agent_permission_denial
    reasons := deny_reasons
    msg := concat("; ", reasons)
    response := {
        "allowed": false,
        "http_status": 403,
        "headers": {"Content-Type": "application/json"},
        "body": json.marshal({
            "error": "authorization_denied",
            "reasons": reasons,
            "message": msg,
        }),
    }
}

# ─── RPC-LEVEL AUTHORIZATION ────────────────────────────────────────

# Protocol messages (handshake, ping, tool listing) — pass through
allow_rpc if {
    rpc_body.method in mcp_protocol_methods
}

# Tool invocation — full authorization chain
allow_rpc if {
    rpc_body.method == "tools/call"
    agent_has_permission           # Layer 2+3: Agent identity + permission
    scope_satisfied                # Layer 4: JWT scope check
    role_satisfied                 # Layer 5: Role enforcement
    step_up_satisfied              # Layer 6: Step-up consent (high-risk tools only)
    resource_constraints_met       # Layer 7: Parameter-level checks
}

# ─── HELPER: is the request fully allowed? ───────────────────────────

is_allowed if {
    http_request.method == "GET"
    startswith(http_request.path, "/mcp")
}

is_allowed if {
    http_request.method == "POST"
    startswith(http_request.path, "/mcp")
    allow_rpc
}

# ─── DENY TYPE CLASSIFIERS ──────────────────────────────────────────

# Missing JWT denial: tools/call with no token
is_missing_jwt if {
    rpc_body.method == "tools/call"
    agent_id == ""
}

# Scope denial: scope missing for non-high-risk tools (non-high-risk tools only).
# High-risk with role → is_high_risk_missing_scope
is_scope_denial if {
    rpc_body.method == "tools/call"
    agent_has_permission
    not scope_satisfied
    not is_high_risk_missing_scope
}

# High-risk missing scope: agent permitted, role present, but billing:refund or
# escalation:trigger scope is absent. Step-up consent is the only path to acquire
# these scopes — role is verified here so we don't prompt unauthorised users.
is_high_risk_missing_scope if {
    rpc_body.method == "tools/call"
    agent_has_permission
    role_satisfied
    not scope_satisfied
    required_scope := tool_required_scopes[tool_name]
    required_scope in high_risk_scopes
}

# Agent permission denial: agent_id is present but agent is not allowed to invoke the tool
is_agent_permission_denial if {
    rpc_body.method == "tools/call"
    agent_id != ""
    not agent_has_permission
}

# ─── LAYER 2+3 — AGENT IDENTITY & PERMISSION (OBO JWT agent_id) ─────

agent_has_permission if {
    agent_perms := agent_permissions[agent_id]
    tool_perms := agent_perms[tool_name]
    tool_perms["invoke"] == true
}

# ─── LAYER 4 — SCOPE ENFORCEMENT (OBO JWT scope claim) ──────────────

scope_satisfied if {
    not tool_required_scopes[tool_name]
}

scope_satisfied if {
    required_scope := tool_required_scopes[tool_name]
    required_scope in jwt_scopes
}

# ─── LAYER 5 — ROLE ENFORCEMENT (OBO JWT role claim) ────────────────

role_satisfied if {
    not tool_required_roles[tool_name]
}

role_satisfied if {
    required_roles := tool_required_roles[tool_name]
    count(jwt_roles & required_roles) > 0
}

# ─── LAYER 6 — STEP-UP SCOPE (high-risk tools) ──────────────────────
# High-risk tools (billing:refund, escalation:trigger) require the elevated
# scope to be present in the JWT. The scope is only issued via the step-up
# consent flow, so its presence implicitly confirms user consent.

step_up_satisfied if {
    required_scope := tool_required_scopes[tool_name]
    not required_scope in high_risk_scopes
}

step_up_satisfied if {
    not tool_required_scopes[tool_name]
}

step_up_satisfied if {
    required_scope := tool_required_scopes[tool_name]
    required_scope in high_risk_scopes
    required_scope in jwt_scopes
}

# ─── LAYER 7 — RESOURCE CONSTRAINT VALIDATION ───────────────────────

resource_constraints_met if {
    not tool_name == "issue_refund"
    not tool_name == "trigger_escalation"
}

resource_constraints_met if {
    tool_name == "issue_refund"
    requested := to_number(object.get(tool_args, "amount_usd", 0))
    max_amount := refund_max_amount[agent_id]
    requested <= max_amount
    requested > 0
}

resource_constraints_met if {
    tool_name == "trigger_escalation"
    level   := object.get(tool_args, "escalation_level", "")
    allowed := escalation_allowed_levels[agent_id]
    level in allowed
}

# ─── DENY REASONS (for generic deny + audit logs) ───────────────────

deny_reasons contains "HTTP method not allowed" if {
    not http_request.method in {"GET", "POST"}
}

deny_reasons contains "unknown MCP path" if {
    not startswith(http_request.path, "/mcp")
    not startswith(http_request.path, "/health")
}

deny_reasons contains msg if {
    rpc_body.method == "tools/call"
    not agent_has_permission
    msg := sprintf("agent permission denied: agent_id='%s' tool='%s'", [agent_id, tool_name])
}

deny_reasons contains msg if {
    rpc_body.method == "tools/call"
    agent_has_permission
    not role_satisfied
    msg := sprintf("role denied: agent_id='%s' tool='%s' user_roles=%v", [agent_id, tool_name, jwt_roles])
}

deny_reasons contains msg if {
    rpc_body.method == "tools/call"
    agent_has_permission
    scope_satisfied
    role_satisfied
    step_up_satisfied
    not resource_constraints_met
    msg := sprintf("resource constraint violated: agent_id='%s' tool='%s' args=%v", [agent_id, tool_name, tool_args])
}
