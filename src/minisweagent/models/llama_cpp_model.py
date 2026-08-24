"""Direct streaming client for llama-server's OpenAI-compatible endpoint."""

import json
import sys
import time
from dataclasses import dataclass
from typing import Any, Literal

import requests

from minisweagent.models.litellm_model import LitellmModelConfig
from minisweagent.models.llama_log import format_llama_server_stats, read_llama_server_log
from minisweagent.models.utils.actions_toolcall import (
    TOOL_DEFINITIONS,
    format_toolcall_observation_messages,
    parse_toolcall_actions,
)
from minisweagent.models.utils.openai_multimodal import expand_multimodal_content


class LlamaCppModelConfig(LitellmModelConfig):
    response_streaming: Literal["off", "status", "draft"] = "draft"
    """Direct llama.cpp transport mode. ``draft`` prints provisional text to stderr."""


@dataclass
class _Function:
    name: str = ""
    arguments: str = ""


@dataclass
class _ToolCall:
    id: str
    function: _Function
    type: str = "function"


class LlamaCppModel:
    """Use llama-server directly; LiteLLM is deliberately not involved."""

    def __init__(self, **kwargs):
        self.config = LlamaCppModelConfig(**kwargs)

    def _endpoint(self) -> str:
        base = self.config.model_kwargs.get("api_base", "http://127.0.0.1:8080/v1")
        return str(base).rstrip("/") + "/chat/completions"

    def _headers(self) -> dict[str, str]:
        key = self.config.model_kwargs.get("api_key")
        return {"Authorization": f"Bearer {key}"} if key else {}

    def _request_timeout(self) -> float | tuple[float, float] | None:
        connect = self.config.connect_timeout_seconds
        read = self.config.model_timeout_seconds or None
        return (connect, read)

    @staticmethod
    def _print(message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        print(f"[{timestamp}] [mini-swe-agent-slow] {message}", file=sys.stderr, flush=True)

    def query(self, messages: list[dict[str, str]], **kwargs) -> dict:
        started = time.monotonic()
        request = {
            "model": self.config.model_name,
            "messages": [{k: v for k, v in msg.items() if k != "extra"} for msg in messages],
            "tools": TOOL_DEFINITIONS,
            **self.config.model_kwargs,
            **kwargs,
            "stream": True,
        }
        request.pop("api_base", None)
        request.pop("api_key", None)
        response = requests.post(
            self._endpoint(),
            headers=self._headers(),
            json=request,
            timeout=self._request_timeout(),
            stream=True,
        )
        response.raise_for_status()
        content: list[str] = []
        calls: dict[int, _ToolCall] = {}
        usage: dict[str, Any] = {}
        self._print("Model draft: ") if self.config.response_streaming == "draft" else None
        for raw_line in response.iter_lines(decode_unicode=True):
            if not raw_line or not raw_line.startswith("data:"):
                continue
            payload = raw_line[5:].strip()
            if payload == "[DONE]":
                break
            chunk = json.loads(payload)
            usage.update(chunk.get("usage") or {})
            for choice in chunk.get("choices", []):
                delta = choice.get("delta") or {}
                text = delta.get("content") or ""
                if text:
                    content.append(text)
                    if self.config.response_streaming == "draft":
                        print(text, file=sys.stderr, end="", flush=True)
                for tool in delta.get("tool_calls") or []:
                    index = int(tool.get("index", 0))
                    call = calls.setdefault(index, _ToolCall(tool.get("id", f"call_{index}"), _Function()))
                    call.id = tool.get("id") or call.id
                    function = tool.get("function") or {}
                    call.function.name += function.get("name") or ""
                    call.function.arguments += function.get("arguments") or ""
        if self.config.response_streaming == "draft":
            print(file=sys.stderr, flush=True)
        tool_calls = [calls[index] for index in sorted(calls)]
        if tool_calls and self.config.response_streaming == "draft" and content:
            self._print("Model requested tools; draft text is not the final result.")
        stats = read_llama_server_log(self.config.llama_log_path)
        if stats:
            self._print(format_llama_server_stats(stats))
        try:
            actions = parse_toolcall_actions(
                tool_calls,
                format_error_template=self.config.format_error_template,
                template_kwargs={"finish_reason": "tool_calls" if tool_calls else "stop"},
            ) if tool_calls else []
        except Exception as error:
            if hasattr(error, "messages"):
                error.messages[0].setdefault("extra", {}).update(
                    {"request_elapsed_seconds": time.monotonic() - started, "response": repr(tool_calls)}
                )
            raise
        final = not tool_calls and bool("".join(content).strip())
        return {
            "role": "assistant",
            "content": "".join(content) or None,
            "tool_calls": [
                {"id": call.id, "type": call.type, "function": {"name": call.function.name, "arguments": call.function.arguments}}
                for call in tool_calls
            ],
            "extra": {
                "actions": actions,
                "is_final": final,
                "final_text": "".join(content) if final else "",
                "cost": 0.0,
                "input_tokens": usage.get("prompt_tokens", usage.get("input_tokens")),
                "output_tokens": usage.get("completion_tokens", usage.get("output_tokens")),
                "total_tokens": usage.get("total_tokens"),
                "request_elapsed_seconds": time.monotonic() - started,
                "timestamp": time.time(),
            },
        }

    def format_message(self, **kwargs) -> dict:
        return expand_multimodal_content(kwargs, pattern=self.config.multimodal_regex)

    def format_observation_messages(self, message: dict, outputs: list[dict], template_vars: dict | None = None) -> list[dict]:
        return format_toolcall_observation_messages(
            actions=message.get("extra", {}).get("actions", []),
            outputs=outputs,
            observation_template=self.config.observation_template,
            template_vars=template_vars,
            multimodal_regex=self.config.multimodal_regex,
            output_config=self.config.tool_output,
        )

    def get_template_vars(self, **kwargs) -> dict[str, Any]:
        return self.config.model_dump()

    def serialize(self) -> dict:
        return {"info": {"config": {"model": self.config.model_dump(mode="json"), "model_type": f"{self.__class__.__module__}.{self.__class__.__name__}"}}}
