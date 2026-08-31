import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import litellm
from pydantic import BaseModel, ConfigDict, Field, field_validator

from minisweagent.exceptions import FormatError
from minisweagent.models import GLOBAL_MODEL_STATS
from minisweagent.models.utils.actions_toolcall import (
    TOOL_DEFINITIONS,
    format_toolcall_observation_messages,
    parse_toolcall_actions,
)
from minisweagent.models.utils.anthropic_utils import _reorder_anthropic_thinking_blocks
from minisweagent.models.utils.cache_control import set_cache_control
from minisweagent.models.utils.openai_multimodal import expand_multimodal_content
from minisweagent.models.utils.retry import make_request_timeout

logger = logging.getLogger("litellm_model")


class LitellmModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_name: str
    """Model name. Highly recommended to include the provider in the model name, e.g., `anthropic/claude-sonnet-4-5-20250929`."""
    model_kwargs: dict[str, Any] = {}
    """Additional arguments passed to the API."""
    litellm_model_registry: Path | str | None = os.getenv("LITELLM_MODEL_REGISTRY_PATH")
    """Model registry for cost tracking and model metadata. See the local model guide (https://mini-swe-agent.com/latest/models/local_models/) for more details."""
    set_cache_control: Literal["default_end"] | None = None
    """Set explicit cache control markers, for example for Anthropic models"""
    cost_tracking: Literal["default", "ignore_errors", "disabled"] = os.getenv("MSWEA_COST_TRACKING", "default")
    """Cost tracking mode. ``disabled`` keeps token metrics without calling LiteLLM's cost calculator."""
    format_error_template: str = "{{ error }}"
    """Template used when the LM's output is not in the expected format."""
    observation_template: str = (
        "{% if output.exception_info %}<exception>{{output.exception_info}}</exception>\n{% endif %}"
        "<returncode>{{output.returncode}}</returncode>\n<output>\n{{output.output}}</output>"
    )
    """Template used to render the observation after executing an action."""
    multimodal_regex: str = ""
    """Regex to extract multimodal content. Empty string disables multimodal processing."""
    connect_timeout_seconds: int = Field(default=30, ge=1)
    """TCP/TLS connection deadline only; it does not limit request reads or generation."""
    model_timeout_seconds: int = Field(default=0, ge=0)
    """Read deadline. Zero means no mini-SWE-agent-imposed prefill or generation deadline."""
    llama_log_path: Path | str | None = None
    """Optional externally captured llama-server log path for operator analysis."""
    max_retries: int = Field(default=0, ge=0)
    """LiteLLM/OpenAI-compatible retries after the original request. Slow-local defaults to zero."""
    tool_output: dict[str, int] = Field(
        default_factory=lambda: {
            "max_chars": 6000,
            "head_chars": 1200,
            "tail_chars": 3600,
            "error_tail_chars": 4800,
            "max_turn_chars": 0,
            "minimum_chars_per_observation": 256,
        }
    )
    """Character budget for tool observations only; it never truncates model responses."""

    @field_validator("tool_output")
    @classmethod
    def validate_tool_output(cls, value: dict[str, int]) -> dict[str, int]:
        minimum = value.get("minimum_chars_per_observation", 256)
        if minimum < 64:
            raise ValueError("tool_output.minimum_chars_per_observation must be at least 64")
        if value.get("max_turn_chars", 0) < 0:
            raise ValueError("tool_output.max_turn_chars must be non-negative")
        return value


class LitellmModel:
    def __init__(self, *, config_class: Callable = LitellmModelConfig, **kwargs):
        self.config = config_class(**kwargs)
        if self.config.litellm_model_registry and Path(self.config.litellm_model_registry).is_file():
            litellm.utils.register_model(json.loads(Path(self.config.litellm_model_registry).read_text()))

    def _request_kwargs(self, **kwargs) -> dict[str, Any]:
        request_kwargs = self.config.model_kwargs | kwargs
        request_kwargs["timeout"] = make_request_timeout(
            self.config.connect_timeout_seconds, self.config.model_timeout_seconds
        )
        # LiteLLM forwards this to the OpenAI-compatible client.  Do not let its
        # default retry policy replay an ambiguous, expensive local inference.
        request_kwargs["max_retries"] = self.config.max_retries
        return request_kwargs

    def _query(self, messages: list[dict[str, str]], **kwargs):
        try:
            return litellm.completion(
                model=self.config.model_name,
                messages=messages,
                tools=TOOL_DEFINITIONS,
                **self._request_kwargs(**kwargs),
            )
        except litellm.exceptions.AuthenticationError as e:
            e.message += " You can permanently set your API key with `mini-extra config set KEY VALUE`."
            raise e

    def _prepare_messages_for_api(self, messages: list[dict]) -> list[dict]:
        prepared = [{k: v for k, v in msg.items() if k != "extra"} for msg in messages]
        prepared = _reorder_anthropic_thinking_blocks(prepared)
        return set_cache_control(prepared, mode=self.config.set_cache_control)

    def query(self, messages: list[dict[str, str]], **kwargs) -> dict:
        request_started = time.monotonic()
        response = self._query(self._prepare_messages_for_api(messages), **kwargs)
        cost_output = self._calculate_cost(response) | {"request_elapsed_seconds": time.monotonic() - request_started}
        GLOBAL_MODEL_STATS.add(cost_output["cost"])
        # Note: all model.query() implementations must persist the response and cost on FormatError.
        try:
            is_final = self._is_final_response(response)
            actions = [] if is_final else self._parse_actions(response)
        except FormatError as e:
            e.messages[0]["extra"].update(cost_output)
            try:
                e.messages[0]["extra"]["response"] = response.model_dump(mode="json")
            except Exception:
                # model_dump failed (e.g. unserializable object); fall back to repr
                # so the spec contract ("response MUST be persisted") holds unconditionally.
                e.messages[0]["extra"]["response"] = repr(response)
            raise
        message = response.choices[0].message.model_dump()
        message["extra"] = {
            "actions": actions,
            "is_final": is_final,
            "final_text": message.get("content", "") if is_final else "",
            "response": response.model_dump(),
            **cost_output,
            "timestamp": time.time(),
        }
        return message

    @staticmethod
    def _is_final_response(response) -> bool:
        message = response.choices[0].message
        return not message.tool_calls and isinstance(message.content, str) and bool(message.content.strip())

    def _calculate_cost(self, response) -> dict[str, float]:
        if self.config.cost_tracking == "disabled":
            cost = 0.0
        else:
            try:
                cost = litellm.cost_calculator.completion_cost(response, model=self.config.model_name)
                if cost <= 0.0:
                    raise ValueError(f"Cost must be > 0.0, got {cost}")
            except Exception as e:
                cost = 0.0
                if self.config.cost_tracking != "ignore_errors":
                    msg = (
                        f"Error calculating cost for model {self.config.model_name}: {e}, perhaps it's not registered? "
                        "You can ignore this issue from your config file with cost_tracking: 'ignore_errors' or "
                        "globally with export MSWEA_COST_TRACKING='ignore_errors'. "
                        "Alternatively check the 'Cost tracking' section in the documentation at "
                        "https://klieret.short.gy/mini-local-models. "
                        " Still stuck? Please open a github issue at https://github.com/SWE-agent/mini-swe-agent/issues/new/choose!"
                    )
                    logger.critical(msg)
                    raise RuntimeError(msg) from e
        usage = response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
        usage = usage.model_dump() if hasattr(usage, "model_dump") else usage or {}
        return {
            "cost": cost,
            "input_tokens": usage.get("input_tokens", usage.get("prompt_tokens")),
            "output_tokens": usage.get("output_tokens", usage.get("completion_tokens")),
            "total_tokens": usage.get("total_tokens"),
        }

    def _parse_actions(self, response) -> list[dict]:
        """Parse tool calls from the response. Raises FormatError if unknown tool."""
        tool_calls = response.choices[0].message.tool_calls or []
        return parse_toolcall_actions(
            tool_calls,
            format_error_template=self.config.format_error_template,
            template_kwargs={"finish_reason": response.choices[0].finish_reason},
        )

    def format_message(self, **kwargs) -> dict:
        return expand_multimodal_content(kwargs, pattern=self.config.multimodal_regex)

    def format_observation_messages(
        self, message: dict, outputs: list[dict], template_vars: dict | None = None
    ) -> list[dict]:
        """Format execution outputs into tool result messages."""
        actions = message.get("extra", {}).get("actions", [])
        return format_toolcall_observation_messages(
            actions=actions,
            outputs=outputs,
            observation_template=self.config.observation_template,
            template_vars=template_vars,
            multimodal_regex=self.config.multimodal_regex,
            output_config=self.config.tool_output,
        )

    def get_template_vars(self, **kwargs) -> dict[str, Any]:
        return self.config.model_dump()

    def serialize(self) -> dict:
        return {
            "info": {
                "config": {
                    "model": self.config.model_dump(mode="json"),
                    "model_type": f"{self.__class__.__module__}.{self.__class__.__name__}",
                },
            }
        }
