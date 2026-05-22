#!/usr/bin/env bash
#
# Create all Kubernetes Secrets required by the Helm chart.
# Reads values from scripts/.env (copy from scripts/.env.example).
#
# Idempotent: re-running updates existing secrets in place.
# Skips LLM-provider secrets whose API key is blank.
#
# Usage:
#   bash scripts/create-secrets.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "error: ${ENV_FILE} not found. Copy scripts/.env.example to scripts/.env and fill it in." >&2
  exit 1
fi

# shellcheck disable=SC1090
set -a; source "${ENV_FILE}"; set +a

required_vars=(
  ASGARDEO_BASE_URL JWT_ISSUER JWKS_URI JWKS_HOST
  CSA_JWT_AUDIENCE CORS_ALLOW_ORIGIN CSA_CLIENT_ID CSA_AGENT_ID CSA_AGENT_SECRET REDIRECT_URI
  BIL_JWT_AUDIENCE BIL_CLIENT_ID BIL_CLIENT_SECRET BIL_REDIRECT_URI BIL_AGENT_ID BIL_AGENT_SECRET
  TOOLS_JWT_AUDIENCE
)

for v in "${required_vars[@]}"; do
  val="${!v:-}"
  if [[ -z "${val}" || "${val}" =~ ^\<.*\>$ ]]; then
    echo "error: required env var ${v} is missing or still a placeholder in ${ENV_FILE}" >&2
    exit 1
  fi
done

command -v kubectl >/dev/null || { echo "error: kubectl not on PATH" >&2; exit 1; }

# Secret + namespace names — keep in sync with helm/values.yaml.
CSA_NS="customer-service-assistant-agent-cell"
BIL_NS="billing-investigation-agent-cell"
TOOLS_NS="tools-cell"

# Ensure namespaces exist (apply is a no-op if they're already there).
for ns in "${CSA_NS}" "${BIL_NS}" "${TOOLS_NS}"; do
  kubectl create namespace "${ns}" --dry-run=client -o yaml | kubectl apply -f -
done

# apply_generic <namespace> <secret-name> <key=val> [<key=val> ...]
apply_generic() {
  local ns="$1" name="$2"; shift 2
  local args=()
  for kv in "$@"; do args+=("--from-literal=${kv}"); done
  kubectl -n "${ns}" create secret generic "${name}" "${args[@]}" \
    --dry-run=client -o yaml | kubectl apply -f -
}

apply_tls() {
  local ns="$1" name="$2" cert="$3" key="$4"
  if [[ ! -f "${cert}" || ! -f "${key}" ]]; then
    echo "warn: TLS cert/key not found (${cert}, ${key}) — skipping ${name}" >&2
    return
  fi
  kubectl -n "${ns}" create secret tls "${name}" \
    --cert="${cert}" --key="${key}" \
    --dry-run=client -o yaml | kubectl apply -f -
}

apply_llm_key() {
  local ns="$1" name="$2" key="$3"
  if [[ -z "${key}" ]]; then
    echo "info: skipping ${ns}/${name} (api key blank)"
    return
  fi
  apply_generic "${ns}" "${name}" "api-key=${key}"
}

# ─── customer-service-assistant-agent-cell ───────────────────────────────────
apply_tls "${CSA_NS}" "ingress-tls" "${TLS_CERT_PATH}" "${TLS_KEY_PATH}"

apply_generic "${CSA_NS}" "ingress-jwt-validation-config" \
  "JWT_ISSUER=${JWT_ISSUER}" \
  "JWT_AUDIENCE=${CSA_JWT_AUDIENCE}" \
  "JWKS_URI=${JWKS_URI}" \
  "JWKS_HOST=${JWKS_HOST}" \
  "CORS_ALLOW_ORIGIN=${CORS_ALLOW_ORIGIN}"

apply_generic "${CSA_NS}" "agent-oauth-credentials" \
  "ASGARDEO_BASE_URL=${ASGARDEO_BASE_URL}" \
  "CLIENT_ID=${CSA_CLIENT_ID}" \
  "REDIRECT_URI=${REDIRECT_URI}" \
  "AGENT_ID=${CSA_AGENT_ID}" \
  "AGENT_SECRET=${CSA_AGENT_SECRET}"

apply_llm_key "${CSA_NS}" "llm-api-key-openai"    "${OPENAI_API_KEY}"
apply_llm_key "${CSA_NS}" "llm-api-key-anthropic" "${ANTHROPIC_API_KEY}"
apply_llm_key "${CSA_NS}" "llm-api-key-gemini"    "${GEMINI_API_KEY}"

# ─── billing-investigation-agent-cell ────────────────────────────────────────
apply_generic "${BIL_NS}" "agent-oauth-credentials" \
  "ASGARDEO_BASE_URL=${ASGARDEO_BASE_URL}" \
  "CLIENT_ID=${BIL_CLIENT_ID}" \
  "CLIENT_SECRET=${BIL_CLIENT_SECRET}" \
  "REDIRECT_URI=${BIL_REDIRECT_URI}" \
  "AGENT_ID=${BIL_AGENT_ID}" \
  "AGENT_SECRET=${BIL_AGENT_SECRET}"

apply_llm_key "${BIL_NS}" "llm-api-key-openai"    "${OPENAI_API_KEY}"
apply_llm_key "${BIL_NS}" "llm-api-key-anthropic" "${ANTHROPIC_API_KEY}"
apply_llm_key "${BIL_NS}" "llm-api-key-gemini"    "${GEMINI_API_KEY}"

apply_generic "${BIL_NS}" "intercell-jwt-validation-config" \
  "JWT_ISSUER=${JWT_ISSUER}" \
  "JWT_AUDIENCE=${BIL_JWT_AUDIENCE}" \
  "JWKS_URI=${JWKS_URI}" \
  "JWKS_HOST=${JWKS_HOST}"

# ─── tools-cell ──────────────────────────────────────────────────────────────
apply_generic "${TOOLS_NS}" "tools-ingress-jwt-validation-config" \
  "JWT_ISSUER=${JWT_ISSUER}" \
  "JWT_AUDIENCE=${TOOLS_JWT_AUDIENCE}" \
  "JWKS_URI=${JWKS_URI}" \
  "JWKS_HOST=${JWKS_HOST}"

# Agent IDs substituted into the OPA policy at pod startup by an envsubst
# init container. Same source of truth as the agent OAuth credentials above.
apply_generic "${TOOLS_NS}" "tools-opa-agents" \
  "CSA_AGENT_ID=${CSA_AGENT_ID}" \
  "BIL_AGENT_ID=${BIL_AGENT_ID}"

echo "done."
