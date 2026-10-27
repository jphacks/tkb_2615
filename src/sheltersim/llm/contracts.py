"""Typed tasks, provider results, JSON contract validation, and auditable call records."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import urlparse


class LLMStage(StrEnum):
    """Task routing stage used to select an approved Claude model family."""

    PREPARE = "prepare"
    RUNTIME = "runtime"
    INTERVENTION = "intervention"
    REVIEW = "review"


class LLMProviderError(RuntimeError):
    """Sanitized adapter failure; API keys and response bodies are excluded."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        failure_kind: str | None = None,
        response_metadata: ProviderResponse | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.failure_kind = failure_kind
        self.response_metadata = response_metadata


class LLMContractError(ValueError):
    """Model response could not be parsed or did not satisfy its output contract."""


class LLMAllowanceError(LLMProviderError):
    """Shared durable preflight refused a request before the provider was called."""


@dataclass(frozen=True)
class LLMTask:
    """Prompt and state identity for one bounded, schema-checked request."""

    stage: LLMStage
    input_text: str
    state_hash: str
    prompt_version: str
    schema_version: int = 1
    output_schema: dict[str, Any] | None = None
    fallback_text: str = ""
    max_output_tokens: int | None = None
    web_search_allowed_domains: tuple[str, ...] = ()
    max_web_searches: int = 0
    # A task whose bounded context outgrows the generic per-call guard declares its own
    # UTF-8 byte limit. It changes no response, so it is not part of the cache key.
    input_byte_limit: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "stage", LLMStage(self.stage))
        if self.input_byte_limit is not None and (
            not isinstance(self.input_byte_limit, int)
            or isinstance(self.input_byte_limit, bool)
            or self.input_byte_limit <= 0
        ):
            raise ValueError("input_byte_limit must be positive")
        if not self.prompt_version:
            raise ValueError("prompt_version cannot be empty")
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version <= 0
        ):
            raise ValueError("schema_version must be positive")
        if self.max_output_tokens is not None and (
            not isinstance(self.max_output_tokens, int)
            or isinstance(self.max_output_tokens, bool)
            or self.max_output_tokens <= 0
        ):
            raise ValueError("max_output_tokens must be positive")
        if (
            not isinstance(self.max_web_searches, int)
            or isinstance(self.max_web_searches, bool)
            or not 0 <= self.max_web_searches <= 3
        ):
            raise ValueError("max_web_searches must be between zero and three")
        if len(self.web_search_allowed_domains) > 12 or any(
            not isinstance(domain, str)
            or not domain
            or len(domain) > 120
            or "/" in domain
            or ":" in domain
            for domain in self.web_search_allowed_domains
        ):
            raise ValueError("web search domains must be bounded bare host names")
        if bool(self.web_search_allowed_domains) != bool(self.max_web_searches):
            raise ValueError("web search requires both an allowlist and a positive use limit")
        if self.output_schema is not None:
            _validate_schema_definition(self.output_schema)

    @property
    def input_hash(self) -> str:
        """Hash the exact prompt input without storing it in usage logs."""
        return _digest(self.input_text)

    def cache_key(self, model_id: str) -> str:
        """Bind replay entries to state, prompt, model, and output contract versions."""
        identity = {
            "stage": self.stage.value,
            "model_id": model_id,
            "state_hash": self.state_hash,
            "input_hash": self.input_hash,
            "prompt_version": self.prompt_version,
            "schema_version": self.schema_version,
            "output_schema": self.output_schema,
            "max_output_tokens": self.max_output_tokens,
        }
        # Preserve every historical no-search tape/checkpoint key exactly. Search
        # policy is added only for tasks that can incur hosted-search fees.
        if self.max_web_searches:
            identity["web_search_allowed_domains"] = self.web_search_allowed_domains
            identity["max_web_searches"] = self.max_web_searches
        return _digest(identity)


@dataclass(frozen=True)
class ProviderCitation:
    """A bounded citation returned by Anthropic's hosted web-search tool."""

    url: str
    title: str
    cited_text: str = ""

    def __post_init__(self) -> None:
        parsed = urlparse(self.url)
        if parsed.scheme != "https" or not parsed.hostname or len(self.url) > 2048:
            raise ValueError("provider citation URL must be HTTPS")
        if not self.title or len(self.title) > 240 or len(self.cited_text) > 500:
            raise ValueError("provider citation text exceeds its bounded size")


@dataclass(frozen=True)
class ProviderResponse:
    """Sanitized successful response metadata from one provider request."""

    text: str
    model_id: str
    input_tokens: int
    output_tokens: int
    elapsed_ms: float
    request_id: str | None = None
    cache_creation_5m_input_tokens: int = 0
    cache_creation_1h_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    web_search_requests: int = 0
    citations: tuple[ProviderCitation, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise ValueError("provider response text must be a string")
        if not isinstance(self.model_id, str) or not self.model_id:
            raise ValueError("provider response model_id cannot be empty")
        for name, value in (
            ("input_tokens", self.input_tokens),
            ("output_tokens", self.output_tokens),
            ("cache_creation_5m_input_tokens", self.cache_creation_5m_input_tokens),
            ("cache_creation_1h_input_tokens", self.cache_creation_1h_input_tokens),
            ("cache_read_input_tokens", self.cache_read_input_tokens),
            ("web_search_requests", self.web_search_requests),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"provider response {name} must be a non-negative integer")
        if not math.isfinite(self.elapsed_ms) or self.elapsed_ms < 0:
            raise ValueError("provider response elapsed_ms must be finite and non-negative")
        citations = tuple(
            item if isinstance(item, ProviderCitation) else ProviderCitation(**item)
            for item in self.citations
        )
        if len(citations) > 25:
            raise ValueError("provider citations must be a bounded list of citation records")
        object.__setattr__(self, "citations", citations)


@dataclass(frozen=True)
class LLMCallRecord:
    """PII-free observability record for API, cache, fallback, and budget outcomes."""

    call_index: int
    stage: str
    outcome: str
    model_id: str
    input_hash: str
    state_hash: str
    prompt_version: str
    schema_version: int
    api_request_count: int
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    elapsed_ms: float
    cache_hit: bool
    fallback_used: bool
    request_id: str | None = None
    error_kind: str | None = None
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    web_search_requests: int = 0
    estimated_search_cost_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-safe data that excludes prompts, keys, and generated PII."""
        return {
            "call_index": self.call_index,
            "stage": self.stage,
            "outcome": self.outcome,
            "model_id": self.model_id,
            "input_hash": self.input_hash,
            "state_hash": self.state_hash,
            "prompt_version": self.prompt_version,
            "schema_version": self.schema_version,
            "api_request_count": self.api_request_count,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
            "elapsed_ms": self.elapsed_ms,
            "cache_hit": self.cache_hit,
            "fallback_used": self.fallback_used,
            "request_id": self.request_id,
            "error_kind": self.error_kind,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "web_search_requests": self.web_search_requests,
            "estimated_search_cost_usd": self.estimated_search_cost_usd,
        }


def validate_task_output(task: LLMTask, output_text: str) -> None:
    """Parse and validate structured JSON outputs against a small strict schema subset."""
    if not isinstance(output_text, str) or not output_text.strip():
        raise LLMContractError("response contains no text content")
    if task.output_schema is None:
        return
    try:
        value = json.loads(output_text, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise LLMContractError("response is not valid JSON") from exc
    _validate_value(value, task.output_schema, path="$")


def _validate_schema_definition(schema: dict[str, Any]) -> None:
    if schema.get("type") != "object" or not isinstance(schema.get("properties"), dict):
        raise ValueError("output schema must define an object with properties")
    if not isinstance(schema.get("required", []), list):
        raise ValueError("output schema required must be a list")
    if schema.get("additionalProperties", True) is not False:
        raise ValueError("output schema must reject additional properties")


def _validate_value(
    value: Any, schema: dict[str, Any], *, path: str, root: dict[str, Any] | None = None
) -> None:
    # Pydantic-generated schemas share definitions through "#/$defs/..." references.
    root = schema if root is None else root
    if "$ref" in schema:
        _validate_value(value, _resolve_ref(schema["$ref"], root, path), path=path, root=root)
        return
    if "anyOf" in schema:
        for candidate in schema["anyOf"]:
            try:
                _validate_value(value, candidate, path=path, root=root)
                return
            except LLMContractError:
                pass
        raise LLMContractError(f"{path} does not match an allowed structured type")
    if "oneOf" in schema:
        matches = 0
        for candidate in schema["oneOf"]:
            try:
                _validate_value(value, candidate, path=path, root=root)
                matches += 1
            except LLMContractError:
                pass
        if matches != 1:
            raise LLMContractError(f"{path} must match exactly one structured type")
        return
    expected_type = schema.get("type")
    if expected_type is not None:
        type_valid = {
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
            "null": value is None,
        }.get(expected_type, False)
        if not type_valid:
            raise LLMContractError(f"{path} must be {expected_type}")
    if "const" in schema and (
        value != schema["const"] or isinstance(value, bool) != isinstance(schema["const"], bool)
    ):
        raise LLMContractError(f"{path} must equal its fixed value")
    if "enum" in schema and value not in schema["enum"]:
        raise LLMContractError(f"{path} is outside the allowed values")
    if expected_type in ("integer", "number"):
        if isinstance(value, float) and not math.isfinite(value):
            raise LLMContractError(f"{path} must be finite")
        if value < schema.get("minimum", value) or value > schema.get("maximum", value):
            raise LLMContractError(f"{path} is outside the permitted range")
        if ("exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]) or (
            "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]
        ):
            raise LLMContractError(f"{path} is outside the permitted range")
    if expected_type == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        extra_schema = schema.get("additionalProperties")
        # Unknown fields stay rejected unless a schema explicitly types them.
        if set(value) - set(properties) and not isinstance(extra_schema, dict):
            raise LLMContractError(f"{path} contains unknown fields")
        if set(required) - set(value):
            raise LLMContractError(f"{path} is missing required fields")
        for key, item in value.items():
            item_schema = properties.get(key, extra_schema)
            _validate_value(item, item_schema, path=f"{path}.{key}", root=root)
    if expected_type == "array":
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", 1_000_000):
            raise LLMContractError(f"{path} has an invalid number of items")
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, item in enumerate(value):
                _validate_value(item, item_schema, path=f"{path}[{index}]", root=root)
    if expected_type == "string":
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 1_000_000):
            raise LLMContractError(f"{path} has an invalid length")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            raise LLMContractError(f"{path} does not match its required pattern")


def _resolve_ref(reference: Any, root: dict[str, Any], path: str) -> dict[str, Any]:
    for prefix, container in (("#/$defs/", "$defs"), ("#/definitions/", "definitions")):
        if isinstance(reference, str) and reference.startswith(prefix):
            target = root.get(container, {}).get(reference[len(prefix) :])
            if isinstance(target, dict):
                return target
    raise LLMContractError(f"{path} references an unknown schema definition")


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant {value!r}")
