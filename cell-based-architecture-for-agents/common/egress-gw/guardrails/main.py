from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import dataclass

from grpc import aio
from grpc_health.v1 import health, health_pb2
from grpc_health.v1 import health_pb2_grpc as health_grpc

from envoy.service.ext_proc.v3 import external_processor_pb2 as ext_proc_pb2
from envoy.service.ext_proc.v3 import external_processor_pb2_grpc as ext_proc_grpc
from envoy.type.v3 import http_status_pb2

from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource


_OTEL_HOST = os.getenv("OTEL_COLLECTOR_HOST", "otel-collector.observability.svc.cluster.local")

_meter_provider = MeterProvider(
    resource=Resource.create({"service.name": "egress-gw-ext_proc"}),
    metric_readers=[
        PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=f"http://{_OTEL_HOST}:4317", insecure=True),
            export_interval_millis=15_000,
        ),
    ],
)
_meter = _meter_provider.get_meter("guardrails")
_c_tokens_prompt = _meter.create_counter("llm.tokens.prompt", unit="tokens",
                                         description="Prompt tokens sent to LLM")
_c_tokens_completion = _meter.create_counter("llm.tokens.completion", unit="tokens",
                                              description="Completion tokens received from LLM")
_c_pii_detections = _meter.create_counter("guardrails.pii", unit="findings",
                                          description="PII findings redacted")
_c_injections = _meter.create_counter("guardrails.injections", unit="findings",
                                      description="Injection patterns detected")
_c_blocked = _meter.create_counter("guardrails.blocked", unit="requests",
                                   description="Requests blocked by ext_proc")

logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","component":"guardrails","msg":"%(message)s"}',
    stream=sys.stdout,
)
logger = logging.getLogger("guardrails")


PII_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("ssn_us",      re.compile(r'\b\d{3}-\d{2}-\d{4}\b')),
    ("credit_card", re.compile(r'\b(?:\d[ -]*?){13,19}\b')),
    ("aws_key",     re.compile(r'\b(?:AKIA|ABIA|ACCA|ASIA)[0-9A-Z]{16}\b')),
    ("api_key",     re.compile(r'\b(?:sk-|pk_live_|pk_test_|rk_live_|rk_test_)[a-zA-Z0-9]{20,}\b')),
    ("ipv4",        re.compile(r'\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b')),
]

INJECTION_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("system_override",     re.compile(r'(?:system\s*prompt|system\s*:)', re.I)),
    ("instruction_ignore",  re.compile(r'(?:ignore|disregard|forget)\s+(?:all\s+)?(?:previous|above|prior)', re.I)),
    ("role_switch",         re.compile(r'(?:you\s+are\s+now|act\s+as\s+if|pretend\s+to\s+be)', re.I)),
    ("delimiter_injection", re.compile(r'<\|im_start\|>|<\|im_end\|>|\[INST\]|\[/INST\]', re.I)),
    ("markdown_injection",  re.compile(r'```\s*system\b', re.I)),
    ("developer_mode",      re.compile(r'(?:developer|debug|admin|god)\s*mode', re.I)),
    ("exfiltration",        re.compile(r'(?:send|post|transmit|upload)\s+(?:to|data|all)', re.I)),
]

INDIRECT_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("embedded_instruction", re.compile(r'(?:ignore|disregard)\s+(?:previous|all)\s+(?:instructions|context)', re.I)),
    ("hidden_command",       re.compile(r'(?:execute|run|eval)\s*\(', re.I)),
    ("url_exfil",            re.compile(r'!\[.*?\]\(https?://(?!(?:example\.com|placeholder))', re.I)),
    ("encoded_payload",      re.compile(r'(?:base64|atob|btoa)\s*\(', re.I)),
]

MAX_REQUEST_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 512 * 1024


def scan_and_redact_pii(text: str) -> tuple[str, list[dict]]:
    findings: list[dict] = []
    redacted = text
    for pii_type, pattern in PII_PATTERNS:
        matches = pattern.findall(redacted)
        if not matches:
            continue
        for match in matches:
            findings.append({"type": pii_type, "sample": match[:4] + "***"})
            redacted = redacted.replace(match, f"[REDACTED_{pii_type.upper()}]")
    return redacted, findings


def _scan_patterns(text: str, patterns: list[tuple[str, re.Pattern]], severity: str, prefix: str = "") -> list[dict]:
    return [
        {"type": f"{prefix}{name}", "severity": severity}
        for name, pattern in patterns
        if pattern.search(text)
    ]


def scan_for_injection(text: str) -> list[dict]:
    return _scan_patterns(text, INJECTION_PATTERNS, severity="high")


def scan_response_for_injection(text: str) -> list[dict]:
    return _scan_patterns(text, INDIRECT_PATTERNS, severity="medium", prefix="response_")


def extract_request_texts(payload: dict, provider: str) -> list[tuple[str, dict, str]]:
    # Only user/system messages: tool results must reach the LLM verbatim, and
    # redacting them changes Content-Length which triggers an ext_proc bug.
    texts: list[tuple[str, dict, str]] = []
    for msg in payload.get("messages", []):
        if msg.get("role") not in ("user", "system"):
            continue
        content = msg.get("content", "")
        if isinstance(content, str) and content:
            texts.append((content, msg, "content"))

    if provider == "anthropic":
        system_text = payload.get("system", "")
        if isinstance(system_text, str) and system_text:
            texts.append((system_text, payload, "system"))

    return texts


def extract_response_texts(payload: dict, provider: str) -> list[tuple[str, dict, str]]:
    if not isinstance(payload, dict):
        return []
    texts: list[tuple[str, dict, str]] = []

    if provider == "anthropic":
        for block in payload.get("content", []):
            if block.get("type") == "text":
                text = block.get("text", "")
                if isinstance(text, str) and text:
                    texts.append((text, block, "text"))
        return texts

    # OpenAI / Gemini (OpenAI-compatible)
    for choice in payload.get("choices", []):
        msg = choice.get("message", {})
        content = msg.get("content", "")
        if isinstance(content, str) and content:
            texts.append((content, msg, "content"))
    return texts


def _extract_token_counts(payload: dict, provider: str) -> tuple[int, int]:
    usage = payload.get("usage") or {}

    if provider == "anthropic":
        return usage.get("input_tokens", 0), usage.get("output_tokens", 0)

    input_t = usage.get("prompt_tokens") or usage.get("prompt_token_count") or 0
    output_t = usage.get("completion_tokens") or usage.get("completion_token_count") or 0

    if not input_t and not output_t:
        meta = payload.get("usageMetadata") or {}
        input_t = meta.get("promptTokenCount", 0)
        output_t = meta.get("candidatesTokenCount", 0)

    return input_t, output_t


def record_token_usage(payload: dict, ctx: RequestContext) -> tuple[int, int]:
    if not isinstance(payload, dict):
        return 0, 0

    input_t, output_t = _extract_token_counts(payload, ctx.provider)
    if not input_t and not output_t:
        logger.warning(
            "[%s] token extraction empty — top-level keys=%s usage=%s usageMetadata=%s",
            ctx.request_id, list(payload.keys()),
            payload.get("usage"), payload.get("usageMetadata"),
        )

    labels = {"provider": ctx.provider, "model": ctx.model}
    _c_tokens_prompt.add(input_t, labels)
    _c_tokens_completion.add(output_t, labels)
    logger.info(
        "[%s] tokens provider=%s model=%s prompt=%d completion=%d",
        ctx.request_id, ctx.provider, ctx.model, input_t, output_t,
    )
    return input_t, output_t


@dataclass
class RequestContext:
    request_id: str = "unknown"
    direction: str = ""  # "request" | "response"
    provider: str = "gemini"
    model: str = "unknown"
    response_status: int = 0
    req_pii: int = 0
    req_injections: int = 0


def _get_header(headers, name: str) -> str | None:
    for h in headers.headers:
        if h.key != name:
            continue
        if h.value:
            return h.value
        if h.raw_value:
            return h.raw_value.decode("utf-8", errors="replace")
    return None


def _set_metadata(response, **kwargs) -> None:
    fields = response.dynamic_metadata.fields
    for key, val in kwargs.items():
        if isinstance(val, (int, float)):
            if val:
                fields[key].number_value = float(val)
            else:
                fields[key].string_value = "0"
        else:
            fields[key].string_value = str(val)


class GuardrailsProcessor(ext_proc_grpc.ExternalProcessorServicer):
    async def Process(self, request_iterator, context):
        ctx = RequestContext()
        try:
            async for request in request_iterator:
                response = ext_proc_pb2.ProcessingResponse()
                handler = self._dispatch(request)
                if handler is None:
                    yield response
                    continue

                yielded = await handler(request, response, ctx)
                yield yielded if yielded is not None else response
        except Exception as exc:
            logger.exception(
                "[%s] Unhandled exception in ext_proc stream direction=%s provider=%s: %s",
                ctx.request_id, ctx.direction, ctx.provider, exc,
            )

    def _dispatch(self, request):
        if request.HasField("request_headers"):
            return self._handle_request_headers
        if request.HasField("request_body"):
            return self._handle_request_body
        if request.HasField("response_headers"):
            return self._handle_response_headers
        if request.HasField("response_body"):
            return self._handle_response_body
        return None

    async def _handle_request_headers(self, request, response, ctx: RequestContext):
        headers = request.request_headers.headers
        ctx.request_id = _get_header(headers, "x-request-id") or "unknown"
        ctx.provider = _get_header(headers, "x-llm-provider") or "gemini"
        ctx.direction = "request"

        logger.info("[%s] Outbound request headers (provider=%s)", ctx.request_id, ctx.provider)
        response.request_headers.CopyFrom(ext_proc_pb2.HeadersResponse())

    async def _handle_request_body(self, request, response, ctx: RequestContext):
        body = request.request_body.body.decode("utf-8", errors="replace")
        ctx.direction = "request"

        logger.info("[%s] Scanning outbound body (%d bytes, provider=%s)",
                    ctx.request_id, len(body), ctx.provider)

        if len(body) > MAX_REQUEST_BYTES:
            logger.warning("[%s] Request too large: %d bytes", ctx.request_id, len(body))
            _c_blocked.add(1, {"provider": ctx.provider, "reason": "size"})
            _set_metadata(response, guardrails_blocked=1, guardrails_block_reason="size")
            response.immediate_response.CopyFrom(
                ext_proc_pb2.ImmediateResponse(
                    status=http_status_pb2.HttpStatus(code=413),
                    body=b'{"error":"request_too_large"}',
                ),
            )
            return

        new_body, modified, total_pii, total_injections = self._scan_request_body(body, ctx)
        ctx.req_pii = total_pii
        ctx.req_injections = total_injections

        body_response = ext_proc_pb2.BodyResponse()
        body_response.response.body_mutation.body = new_body
        if modified:
            body_response.response.header_mutation.remove_headers.append("content-length")
        response.request_body.CopyFrom(body_response)

    def _scan_request_body(self, body: str, ctx: RequestContext) -> tuple[bytes, bool, int, int]:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return body.encode("utf-8"), False, 0, 0

        ctx.model = payload.get("model", "unknown")
        texts = extract_request_texts(payload, ctx.provider)
        labels = {"provider": ctx.provider, "model": ctx.model, "direction": "request"}

        last_user_idx = _last_user_message_index(texts)
        modified = False
        total_pii = 0
        total_injections = 0

        for i, (content, container, key) in enumerate(texts):
            redacted, pii_findings = scan_and_redact_pii(content)
            if pii_findings:
                container[key] = redacted
                modified = True
                if i == last_user_idx:
                    logger.warning("[%s] PII in outbound: %s",
                                   ctx.request_id, [f["type"] for f in pii_findings])
                    total_pii += len(pii_findings)
                    _c_pii_detections.add(len(pii_findings), labels)

            if i == last_user_idx:
                injections = scan_for_injection(content)
                if injections:
                    logger.warning("[%s] Injection pattern in outbound: %s",
                                   ctx.request_id, [f["type"] for f in injections])
                    total_injections += len(injections)
                    _c_injections.add(len(injections), labels)

        new_body = json.dumps(payload).encode("utf-8") if modified else body.encode("utf-8")
        return new_body, modified, total_pii, total_injections

    async def _handle_response_headers(self, request, response, ctx: RequestContext):
        ctx.direction = "response"
        status_str = _get_header(request.response_headers.headers, ":status") or "0"
        ctx.response_status = int(status_str) if status_str.isdigit() else 0
        logger.info("[%s] Inbound response headers status=%d", ctx.request_id, ctx.response_status)
        response.response_headers.CopyFrom(ext_proc_pb2.HeadersResponse())

    async def _handle_response_body(self, request, response, ctx: RequestContext):
        body = request.response_body.body.decode("utf-8", errors="replace")
        ctx.direction = "response"

        logger.info("[%s] Scanning inbound response (%d bytes, provider=%s)",
                    ctx.request_id, len(body), ctx.provider)

        if ctx.response_status >= 400:
            logger.error(
                "[%s] Upstream error status=%d provider=%s model=%s body=%s",
                ctx.request_id, ctx.response_status, ctx.provider, ctx.model, body[:2000],
            )

        new_body, modified, resp_pii, resp_injections, prompt_t, completion_t = (
            self._scan_response_body(body, ctx)
        )

        body_response = ext_proc_pb2.BodyResponse()
        body_response.response.body_mutation.body = new_body
        if modified:
            body_response.response.header_mutation.remove_headers.append("content-length")
        response.response_body.CopyFrom(body_response)

        _set_metadata(
            response,
            guardrails_pii=ctx.req_pii + resp_pii,
            guardrails_injections=ctx.req_injections + resp_injections,
            llm_prompt_tokens=prompt_t,
            llm_completion_tokens=completion_t,
        )

    def _scan_response_body(self, body: str, ctx: RequestContext):
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return body.encode("utf-8"), False, 0, 0, 0, 0

        if not isinstance(payload, dict):
            logger.warning(
                "[%s] Unexpected response type %s from provider=%s: %s",
                ctx.request_id, type(payload).__name__, ctx.provider, body[:500],
            )

        texts = extract_response_texts(payload, ctx.provider)
        labels = {"provider": ctx.provider, "model": ctx.model, "direction": "response"}

        modified = False
        total_pii = 0
        total_injections = 0

        for content, container, key in texts:
            redacted, pii_findings = scan_and_redact_pii(content)
            if pii_findings:
                logger.warning("[%s] PII in LLM response: %s",
                               ctx.request_id, [f["type"] for f in pii_findings])
                total_pii += len(pii_findings)
                _c_pii_detections.add(len(pii_findings), labels)
                container[key] = redacted
                modified = True

            injections = scan_response_for_injection(content)
            if injections:
                logger.warning("[%s] Indirect injection in response: %s",
                               ctx.request_id, [f["type"] for f in injections])
                total_injections += len(injections)
                _c_injections.add(len(injections), labels)
                container[key] = (
                    container[key]
                    + "\n\n[GUARDRAILS: potential indirect injection detected"
                    " — treat this content with caution]"
                )
                modified = True

        prompt_t, completion_t = record_token_usage(payload, ctx)
        new_body = json.dumps(payload).encode("utf-8") if modified else body.encode("utf-8")
        return new_body, modified, total_pii, total_injections, prompt_t, completion_t


def _last_user_message_index(texts: list[tuple[str, dict, str]]) -> int | None:
    # Only the newest user turn drives metrics — older history was counted before.
    for i in range(len(texts) - 1, -1, -1):
        if texts[i][1].get("role") == "user":
            return i
    return len(texts) - 1 if texts else None


async def serve():
    server = aio.server()
    ext_proc_grpc.add_ExternalProcessorServicer_to_server(GuardrailsProcessor(), server)

    health_servicer = health.HealthServicer()
    health_servicer.set("", health_pb2.HealthCheckResponse.SERVING)
    health_grpc.add_HealthServicer_to_server(health_servicer, server)

    server.add_insecure_port("0.0.0.0:50052")
    logger.info("Guardrails ext_proc service listening on 0.0.0.0:50052")
    await server.start()
    await server.wait_for_termination()


if __name__ == "__main__":
    asyncio.run(serve())
