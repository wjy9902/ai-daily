"""Request-level diagnostics. Never persist provider messages or request bodies."""

import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import httpx

CODES = {
    "invalid_request_error",
    "invalid_request",
    "context_length_exceeded",
    "invalid_parameter",
    "invalid_parameters",
    "insufficient_quota",
}
PARAMS = {
    "messages",
    "max_tokens",
    "temperature",
    "tools",
    "tool_choice",
    "response_format",
    "reasoning_content",
    "model",
}


@dataclass
class RequestTrace:
    stage: str
    requests: int = 0
    successes: int = 0
    failed_request: int | None = None
    status: int | None = None
    request_id: str | None = None
    error_code: str | None = None
    error_param: str | None = None
    category: str | None = None
    validation: list[str] = field(default_factory=list)
    client: httpx.AsyncClient | None = None

    async def on_request(self, request: httpx.Request) -> None:
        self.status = None
        self.requests += 1
        self.failed_request = self.requests
        # Only record the existence of a validation retry, never its quoted text.
        if b'"role":"tool"' in request.content or b'"role": "tool"' in request.content:
            if "output_retry" not in self.validation:
                self.validation.append("output_retry")

    async def on_response(self, response: httpx.Response) -> None:
        self.status = response.status_code
        if response.is_success:
            self.successes += 1
            self.failed_request = None
            return
        raw_id = response.headers.get("x-request-id", "")
        if re.fullmatch(r"[a-fA-F0-9-]{8,80}|req_[a-zA-Z0-9-]{8,80}", raw_id):
            self.request_id = raw_id
        await response.aread()
        try:
            body = response.json()
        except ValueError:
            self.category = "unknown"
            return
        error = body.get("error", {}) if isinstance(body, dict) else {}
        if not isinstance(error, dict):
            self.category = "unknown"
            return
        code, param = error.get("code"), error.get("param")
        self.error_code = code if isinstance(code, str) and code in CODES else None
        self.error_param = param if isinstance(param, str) and param in PARAMS else None
        message = str(error.get("message", "")).lower()
        if "context length" in message or code == "context_length_exceeded":
            self.category = "context"
        elif "reasoning_content" in message or "tool_call_id" in message:
            self.category = "protocol"
        elif self.error_param or code in ("invalid_parameter", "invalid_parameters"):
            self.category = "parameter"
        else:
            self.category = "unknown"

    def fields(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "http_status": self.status,
            "actual_request_count": self.requests if self.client else None,
            "successful_response_count": self.successes if self.client else None,
            "failed_request_number": self.failed_request,
            "provider_request_id": self.request_id,
            "error_code": self.error_code,
            "error_parameter": self.error_param,
            "error_category": self.category,
            "validation_categories": self.validation,
        }


CURRENT_TRACE: ContextVar[RequestTrace | None] = ContextVar("model_request_trace", default=None)
