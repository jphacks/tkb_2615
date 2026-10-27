"""Environment-driven Claude routing, call limits, pricing estimates, and test gates."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path


class LLMMode(StrEnum):
    """Provider behavior selected per run."""

    AUTO = "auto"
    OFFLINE = "offline"
    RECORD = "record"
    REPLAY = "replay"


class LLMErrorPolicy(StrEnum):
    """Action when an API, budget, or validation boundary fails."""

    FALLBACK = "fallback"
    STOP = "stop"


@dataclass(frozen=True)
class ModelPrice:
    """Standard Anthropic USD per million input/output token estimates."""

    input_usd_per_million: float
    output_usd_per_million: float

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.input_usd_per_million)
            or not math.isfinite(self.output_usd_per_million)
            or self.input_usd_per_million < 0
            or self.output_usd_per_million < 0
        ):
            raise ValueError("model token prices cannot be negative")

    @property
    def cache_creation_5m_usd_per_million(self) -> float:
        return self.input_usd_per_million * 1.25

    @property
    def cache_creation_1h_usd_per_million(self) -> float:
        return self.input_usd_per_million * 2.0

    @property
    def cache_read_usd_per_million(self) -> float:
        return self.input_usd_per_million * 0.05


_OFFICIAL_MODEL_PRICE_FLOORS = {
    "claude-opus-5-5": ModelPrice(4.0, 20.0),
    "claude-opus-5": ModelPrice(5.0, 25.0),
    "claude-sonnet-5-5": ModelPrice(2.0, 10.0),
    "claude-sonnet-5": ModelPrice(1.0, 5.0),
    "claude-haiku-5-5": ModelPrice(0.1, 0.5),
    "claude-haiku-4-5-20251001": ModelPrice(0.5, 2.5),
}


@dataclass(frozen=True)
class LLMSettings:
    """Cost-bounded Claude routing settings; secrets are never included in repr/logs."""

    mode: LLMMode = LLMMode.AUTO
    api_key: str | None = field(default=None, repr=False, compare=False)
    opus_model: str = "claude-opus-5-5"
    sonnet_model: str = "claude-sonnet-5-5"
    haiku_model: str = "claude-haiku-5-5"
    prepare_model_family: str = "opus"
    intervention_model_family: str = "opus"
    review_model_family: str = "opus"
    max_api_calls: int = 10
    max_input_tokens: int = 16_384
    max_output_tokens: int = 8_192
    max_concurrency: int = 1
    timeout_seconds: float = 20.0
    budget_usd: float = 10.00
    error_policy: LLMErrorPolicy = LLMErrorPolicy.FALLBACK
    cache_path: Path = Path(".sheltersim/llm-tape.json")
    live_tests_allowed: bool = False
    live_test_budget_usd: float = 0.01
    live_test_max_calls: int = 2
    opus_price: ModelPrice = ModelPrice(4.0, 20.0)
    sonnet_price: ModelPrice = ModelPrice(2.0, 10.0)
    haiku_price: ModelPrice = ModelPrice(0.1, 0.5)

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", LLMMode(self.mode))
        object.__setattr__(self, "error_policy", LLMErrorPolicy(self.error_policy))
        if not self.opus_model or not self.sonnet_model or not self.haiku_model:
            raise ValueError("Claude model IDs cannot be empty")
        for name in ("prepare_model_family", "intervention_model_family", "review_model_family"):
            if getattr(self, name) not in {"opus", "sonnet"}:
                raise ValueError(f"{name} must be opus or sonnet")
        for name, value in (
            ("max_api_calls", self.max_api_calls),
            ("max_input_tokens", self.max_input_tokens),
            ("max_output_tokens", self.max_output_tokens),
            ("max_concurrency", self.max_concurrency),
            ("live_test_max_calls", self.live_test_max_calls),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if (
            not math.isfinite(self.budget_usd)
            or not math.isfinite(self.live_test_budget_usd)
            or self.budget_usd < 0
            or self.live_test_budget_usd <= 0
        ):
            raise ValueError("budgets must be non-negative and live-test budget positive")

    @classmethod
    def from_env(cls) -> LLMSettings:
        """Read all routing policy from environment variables without exposing the key."""
        pytest_case = os.getenv("PYTEST_CURRENT_TEST", "")
        under_pytest = bool(pytest_case)
        opted_in_live_test = os.getenv(
            "SHELTERSIM_ALLOW_LIVE_API_TESTS"
        ) == "1" and "tests/live/test_live_api.py::test_live_" in pytest_case.replace("\\", "/")
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip() or None
        if under_pytest and not opted_in_live_test:
            api_key = None
        return cls(
            mode=os.getenv("SHELTERSIM_LLM_MODE", "auto"),
            api_key=api_key,
            opus_model=os.getenv("SHELTERSIM_OPUS_MODEL_ID", "claude-opus-5-5"),
            sonnet_model=os.getenv("SHELTERSIM_SONNET_MODEL_ID", "claude-sonnet-5-5"),
            haiku_model=os.getenv("SHELTERSIM_HAIKU_MODEL_ID", "claude-haiku-5-5"),
            prepare_model_family=os.getenv("SHELTERSIM_PREPARE_MODEL_FAMILY", "opus").casefold(),
            intervention_model_family=os.getenv(
                "SHELTERSIM_INTERVENTION_MODEL_FAMILY", "opus"
            ).casefold(),
            review_model_family=os.getenv("SHELTERSIM_REVIEW_MODEL_FAMILY", "opus").casefold(),
            max_api_calls=_env_int("SHELTERSIM_LLM_MAX_API_CALLS", 10),
            max_input_tokens=_env_int("SHELTERSIM_LLM_MAX_INPUT_TOKENS", 16_384),
            max_output_tokens=_env_int("SHELTERSIM_LLM_MAX_OUTPUT_TOKENS", 8_192),
            max_concurrency=_env_int("SHELTERSIM_LLM_MAX_CONCURRENCY", 1),
            timeout_seconds=_env_float("SHELTERSIM_LLM_TIMEOUT_SECONDS", 20.0),
            budget_usd=_env_float("SHELTERSIM_LLM_BUDGET_USD", 10.00),
            error_policy=os.getenv("SHELTERSIM_LLM_ERROR_POLICY", "fallback"),
            cache_path=Path(os.getenv("SHELTERSIM_LLM_CACHE_PATH", ".sheltersim/llm-tape.json")),
            live_tests_allowed=os.getenv("SHELTERSIM_ALLOW_LIVE_API_TESTS") == "1",
            live_test_budget_usd=_env_float("SHELTERSIM_LIVE_TEST_BUDGET_USD", 0.01),
            live_test_max_calls=_env_int("SHELTERSIM_LIVE_TEST_MAX_CALLS", 2),
            opus_price=ModelPrice(
                _env_float("SHELTERSIM_OPUS_INPUT_USD_PER_MTOK", 4.0),
                _env_float("SHELTERSIM_OPUS_OUTPUT_USD_PER_MTOK", 20.0),
            ),
            sonnet_price=ModelPrice(
                _env_float("SHELTERSIM_SONNET_INPUT_USD_PER_MTOK", 2.0),
                _env_float("SHELTERSIM_SONNET_OUTPUT_USD_PER_MTOK", 10.0),
            ),
            haiku_price=ModelPrice(
                _env_float("SHELTERSIM_HAIKU_INPUT_USD_PER_MTOK", 0.1),
                _env_float("SHELTERSIM_HAIKU_OUTPUT_USD_PER_MTOK", 0.5),
            ),
        )

    @property
    def effective_mode(self) -> LLMMode:
        """Use the API automatically when a key is present, offline otherwise."""
        if self.mode is not LLMMode.AUTO:
            return self.mode
        return LLMMode.RECORD if self.api_key else LLMMode.OFFLINE

    def model_for(self, stage: str) -> str:
        """Route bounded preparation, intervention, review and runtime stages."""
        if stage == "runtime":
            return self.haiku_model
        family = {
            "prepare": self.prepare_model_family,
            "intervention": self.intervention_model_family,
            "review": self.review_model_family,
        }.get(stage)
        if family is None:
            raise ValueError(f"unsupported LLM stage: {stage}")
        return self.sonnet_model if family == "sonnet" else self.opus_model

    def price_for(self, model_id: str) -> ModelPrice:
        """Use no less than official rates; unknown models fail closed before a call."""
        if model_id == self.opus_model:
            configured = self.opus_price
        elif model_id == self.sonnet_model:
            configured = self.sonnet_price
        elif model_id == self.haiku_model:
            configured = self.haiku_price
        else:
            raise ValueError("no verified price is configured for this Claude model ID")
        floor = _OFFICIAL_MODEL_PRICE_FLOORS.get(model_id)
        if floor is None:
            raise ValueError("no official price floor is available for this Claude model ID")
        return ModelPrice(
            max(configured.input_usd_per_million, floor.input_usd_per_million),
            max(configured.output_usd_per_million, floor.output_usd_per_million),
        )

    def for_live_integration_tests(self) -> LLMSettings:
        """Apply the dedicated low-cost test ceiling while retaining approved model IDs."""
        return replace(
            self,
            mode=LLMMode.RECORD,
            max_api_calls=self.live_test_max_calls,
            max_input_tokens=min(self.max_input_tokens, 256),
            max_output_tokens=min(self.max_output_tokens, 64),
            max_concurrency=1,
            budget_usd=self.live_test_budget_usd,
        )


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc
