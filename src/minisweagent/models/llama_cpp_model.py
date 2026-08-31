"""Direct streaming client for llama-server's OpenAI-compatible endpoint."""

import json
import sys
import time
from dataclasses import dataclass
from typing import Any, Literal

import requests

from minisweagent.exceptions import ModelStreamError
from minisweagent.models.litellm_model import LitellmModelConfig
from minisweagent.models.llama_log import format_llama_server_stats, read_llama_server_log
from minisweagent.models.utils.actions_toolcall import (
    TOOL_DEFINITIONS,
    format_toolcall_observation_messages,
)
from minisweagent.models.utils.openai_multimodal import expand_multimodal_content
from minisweagent.models.utils.tool_protocol import get_tool_protocol


class LlamaCppModelConfig(LitellmModelConfig):
    response_streaming: Literal["off", "status", "draft"] = "draft"
    """Direct llama.cpp transport mode. ``draft`` prints provisional text to stderr."""
    tool_protocol: Literal["openai"] = "openai"
    """Tool syntax used by the model."""


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

    _STREAM_FRAGMENT_LIMIT = 1200

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
        print(f"[{timestamp}] [REDLESS] {message}", file=sys.stderr, flush=True)

    @staticmethod
    def _print_stream_prefix(label: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        print(f"[{timestamp}] [REDLESS] {label}: ", file=sys.stderr, end="", flush=True)

    @classmethod
    def _stream_text(cls, label: str, text: str, state: dict[str, Any]) -> None:
        """Print streamed text with timestamps on every physical output line."""
        if not text:
            return
        for part in text.splitlines(keepends=True):
            if not state.get("open") or state.get("label") != label:
                if state.get("open"):
                    print(file=sys.stderr, flush=True)
                cls._print_stream_prefix(label)
                state.update(open=True, label=label)
            print(part, file=sys.stderr, end="", flush=True)
            if part.endswith(("\n", "\r")):
                state["open"] = False

    @classmethod
    def _stream_fragment(cls, payload: str) -> str:
        if len(payload) <= cls._STREAM_FRAGMENT_LIMIT:
            return payload
        half = cls._STREAM_FRAGMENT_LIMIT // 2
        return f"{payload[:half]}…<truncated {len(payload) - 2 * half} chars>…{payload[-half:]}"

    @staticmethod
    def _looks_incomplete(error: json.JSONDecodeError, payload: str) -> bool:
        return error.msg.startswith("Unterminated string") or error.pos >= len(payload.rstrip())

    def query(self, messages: list[dict[str, str]], **kwargs) -> dict:
        started = time.monotonic()
        protocol = get_tool_protocol(self.config.tool_protocol)
        request = {
            "model": self.config.model_name,
            "messages": protocol.prepare_request(messages, TOOL_DEFINITIONS),
            **self.config.model_kwargs,
            **kwargs,
            "stream": True,
        }
        # Keep the agent's tool protocol authoritative. In particular, a
        # stale ``tools`` value in model_kwargs must not disable tools.
        request["tools"] = TOOL_DEFINITIONS
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
        finish_reason = "stop"
        stream_state: dict[str, Any] = {"open": False, "label": ""}

        def consume_chunk(chunk: dict) -> None:
            nonlocal finish_reason
            usage.update(chunk.get("usage") or {})
            for choice in chunk.get("choices", []):
                finish_reason = choice.get("finish_reason") or finish_reason
                delta = choice.get("delta") or {}
                # Non-streaming Chat Completions responses use ``message``;
                # treating it as a delta also makes local mock servers useful.
                if not delta:
                    delta = choice.get("message") or {}
                text = delta.get("content") or ""
                if text:
                    content.append(text)
                    if self.config.response_streaming == "draft":
                        self._stream_text("Model draft", text, stream_state)
                reasoning = (
                    delta.get("reasoning_content")
                    or delta.get("reasoning")
                    or delta.get("thinking")
                    or ""
                )
                if reasoning and self.config.response_streaming == "draft":
                    self._stream_text("Model reasoning", reasoning, stream_state)
                for tool in delta.get("tool_calls") or []:
                    index = int(tool.get("index", 0))
                    call = calls.setdefault(index, _ToolCall(tool.get("id") or "", _Function()))
                    call.id = tool.get("id") or call.id
                    function = tool.get("function") or {}
                    call.function.name += function.get("name") or ""
                    call.function.arguments += function.get("arguments") or ""

        saw_chunk = False
        pending_payload = ""
        pending_line = 0

        def stream_error(message: str, *, payload: str, line_number: int, error: Exception | None = None) -> ModelStreamError:
            diagnostics: dict[str, str | int] = {
                "line": line_number,
                "payload_fragment": self._stream_fragment(payload),
            }
            if error:
                diagnostics["parser_error"] = str(error)
            return ModelStreamError(message, diagnostics)

        def consume_payload(payload: str, line_number: int) -> bool:
            try:
                consume_chunk(json.loads(payload))
            except json.JSONDecodeError as error:
                if self._looks_incomplete(error, payload):
                    return False
                raise stream_error(
                    "Malformed JSON in llama.cpp SSE record.", payload=payload, line_number=line_number, error=error
                ) from error
            return True

        try:
            for line_number, raw_line in enumerate(response.iter_lines(decode_unicode=True), start=1):
                if not raw_line:
                    if pending_payload:
                        raise stream_error(
                            "llama.cpp SSE record ended before its JSON payload was complete.",
                            payload=pending_payload,
                            line_number=pending_line,
                        )
                    continue
                line = raw_line.decode() if isinstance(raw_line, bytes) else raw_line
                line = line.lstrip()
                if line.startswith(("event:", "id:", "retry:", ":")):
                    continue
                if line.startswith("data:"):
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        if pending_payload:
                            raise stream_error(
                                "llama.cpp ended the SSE stream before its JSON payload was complete.",
                                payload=pending_payload,
                                line_number=pending_line,
                            )
                        break
                else:
                    # Be liberal with OpenAI-compatible servers that ignore
                    # ``stream=true`` and return one ordinary JSON response.
                    payload = line.strip()
                if not payload:
                    continue
                if pending_payload:
                    # A later data line is only treated as a continuation when it
                    # cannot plausibly begin a fresh JSON event. This permits a
                    # server that split one record across parser-visible lines,
                    # without merging independent malformed events.
                    if payload.startswith(("{", "[")):
                        raise stream_error(
                            "llama.cpp started a new SSE record before the previous JSON payload was complete.",
                            payload=pending_payload,
                            line_number=pending_line,
                        )
                    payload = pending_payload + payload
                    line_number = pending_line
                    pending_payload = ""
                if consume_payload(payload, line_number):
                    saw_chunk = True
                else:
                    pending_payload = payload
                    pending_line = line_number
        except requests.RequestException as error:
            raise stream_error(
                "llama.cpp stream disconnected before the response completed.",
                payload=pending_payload,
                line_number=pending_line,
                error=error,
            ) from error
        if pending_payload:
            raise stream_error(
                "llama.cpp stream ended before its JSON payload was complete.",
                payload=pending_payload,
                line_number=pending_line,
            )
        if not saw_chunk and hasattr(response, "json"):
            body = response.json()
            if isinstance(body, dict):
                consume_chunk(body)
        if self.config.response_streaming == "draft":
            if stream_state.get("open"):
                print(file=sys.stderr, flush=True)
        tool_calls = [calls[index] for index in sorted(calls)]
        if tool_calls and self.config.response_streaming == "draft" and content:
            self._print("Model requested tools; draft text is not the final result.")
        stats = read_llama_server_log(self.config.llama_log_path)
        if stats:
            self._print(format_llama_server_stats(stats))
        try:
            protocol_calls = tool_calls
            actions = protocol.parse_response(
                "".join(content), tool_calls,
                format_error_template=self.config.format_error_template,
                finish_reason=finish_reason,
            )
        except Exception as error:
            if hasattr(error, "messages"):
                error.messages[0].setdefault("extra", {}).update(
                    {"request_elapsed_seconds": time.monotonic() - started, "response": repr(tool_calls)}
                )
            raise
        # ``tool_calls`` contains only native OpenAI calls, so finality is
        # determined by the absence of tool calls and presence of content.
        final = not protocol_calls and bool("".join(content).strip())
        return {
            "role": "assistant",
            "content": "".join(content) or None,
            "tool_calls": [
                {"id": call.id, "type": "function", "function": {"name": call.name, "arguments": call.arguments}}
                if hasattr(call, "name") else
                {"id": call.id, "type": call.type, "function": {"name": call.function.name, "arguments": call.function.arguments}}
                for call in protocol_calls
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
        results = format_toolcall_observation_messages(
            actions=message.get("extra", {}).get("actions", []),
            outputs=outputs,
            observation_template=self.config.observation_template,
            template_vars=template_vars,
            multimodal_regex=self.config.multimodal_regex,
            output_config=self.config.tool_output,
        )
        return get_tool_protocol(self.config.tool_protocol).format_tool_result(results)

    def get_template_vars(self, **kwargs) -> dict[str, Any]:
        return self.config.model_dump()

    def serialize(self) -> dict:
        return {"info": {"config": {"model": self.config.model_dump(mode="json"), "model_type": f"{self.__class__.__module__}.{self.__class__.__name__}"}}}
