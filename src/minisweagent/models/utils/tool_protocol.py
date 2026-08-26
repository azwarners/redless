"""Small adapters for model-specific tool protocols."""

import ast
import json
import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from minisweagent.exceptions import FormatError
from minisweagent.models.utils.actions_toolcall import TOOL_DEFINITIONS, parse_toolcall_actions


@dataclass
class ProtocolToolCall:
    id: str
    name: str
    arguments: str

    @property
    def function(self) -> SimpleNamespace:
        return SimpleNamespace(name=self.name, arguments=self.arguments)


class OpenAIToolProtocol:
    """The llama.cpp/OpenAI-compatible native tool protocol."""

    name = "openai"

    def prepare_request(self, messages: list[dict], tools: list[dict]) -> list[dict]:
        return [{k: v for k, v in message.items() if k != "extra"} for message in messages]

    def parse_response(self, content: str, tool_calls: list, *, format_error_template: str, finish_reason: str) -> list[dict]:
        if tool_calls:
            return parse_toolcall_actions(
                tool_calls,
                format_error_template=format_error_template,
                template_kwargs={"finish_reason": finish_reason},
            )
        if finish_reason == "tool_calls":
            return parse_toolcall_actions(
                [], format_error_template=format_error_template, template_kwargs={"finish_reason": finish_reason}
            )
        return []

    def format_tool_result(self, messages: list[dict]) -> list[dict]:
        return messages

    def response_tool_calls(self, content: str) -> list[ProtocolToolCall]:
        return []


class NemotronToolProtocol:
    """NVIDIA's textual ``<AVAILABLE_TOOLS>``/``<TOOLCALL>`` protocol."""

    name = "nemotron"
    _instruction = """You are an expert in composing functions. You are given a question and a set of possible functions.

Based on the question, you will need to make one or more function/tool calls to achieve the purpose.

If none of the function can be used, point it out. If the given question lacks the parameters required by the function, also point it out.

You should only return the function call in tools call sections.

If you decide to invoke any of the function(s), you MUST put it in the format of:

<TOOLCALL>[func_name1(params_name1=params_value1, params_name2=params_value2...), func_name2(params)]</TOOLCALL>

You SHOULD NOT include any other text in the response.

Here is a list of functions in JSON format that you can invoke.
"""

    def prepare_request(self, messages: list[dict], tools: list[dict]) -> list[dict]:
        functions = [tool["function"] for tool in tools]
        available = f"<AVAILABLE_TOOLS>{json.dumps(functions, separators=(',', ':'))}</AVAILABLE_TOOLS>"
        system = (
            f"{self._instruction}{available}\n\n"
            "For repository tasks, use the available inspection tools whenever repository evidence is required."
        )
        prepared = []
        for message in messages:
            clean = {k: v for k, v in message.items() if k not in {"extra", "tool_calls", "tool_call_id"}}
            if clean.get("role") == "tool":
                clean = {"role": "user", "content": f"Tool result ({message.get('tool_call_id', '')}):\n{clean.get('content', '')}"}
            prepared.append(clean)
        if prepared and prepared[0].get("role") == "system":
            prepared[0] = {**prepared[0], "content": f"{prepared[0].get('content', '')}\n\n{system}"}
        else:
            prepared.insert(0, {"role": "system", "content": system})
        return prepared

    def parse_response(self, content: str, tool_calls: list, *, format_error_template: str, finish_reason: str) -> list[dict]:
        if tool_calls:
            raise FormatError({"role": "user", "content": "Native tool calls are not valid for the Nemotron protocol.", "extra": {"interrupt_type": "FormatError"}})
        match = re.search(r"<TOOLCALL>(.*?)</TOOLCALL>", content, re.DOTALL)
        if not match:
            if "<TOOLCALL>" in content or "</TOOLCALL>" in content:
                raise self._error(format_error_template, "Malformed <TOOLCALL> syntax.", finish_reason)
            return []
        try:
            calls = self.response_tool_calls(content)
            return parse_toolcall_actions(
                calls,
                format_error_template=format_error_template,
                template_kwargs={"finish_reason": finish_reason},
            )
        except FormatError:
            raise
        except ValueError as error:
            raise self._error(format_error_template, str(error), finish_reason) from error

    def response_tool_calls(self, content: str) -> list[ProtocolToolCall]:
        match = re.search(r"<TOOLCALL>(.*?)</TOOLCALL>", content, re.DOTALL)
        return self._parse_calls(match.group(1)) if match else []

    @staticmethod
    def _error(template: str, error: str, finish_reason: str) -> FormatError:
        from jinja2 import StrictUndefined, Template

        return FormatError({
            "role": "user",
            "content": Template(template, undefined=StrictUndefined).render(
                error=error, actions=[], has_tool_calls=True, finish_reason=finish_reason
            ),
            "extra": {"interrupt_type": "FormatError"},
        })

    def _parse_calls(self, text: str) -> list[ProtocolToolCall]:
        text = text.strip()
        if not (text.startswith("[") and text.endswith("]")):
            raise ValueError("Nemotron tool calls must contain a bracketed list.")
        calls = []
        for index, call_text in enumerate(self._split(text[1:-1], ",")):
            match = re.fullmatch(r"\s*([A-Za-z_][\w]*)\s*\((.*)\)\s*", call_text, re.DOTALL)
            if not match:
                raise ValueError(f"Malformed Nemotron tool call: {call_text.strip()}")
            args = {}
            for parameter in self._split(match.group(2), ",") if match.group(2).strip() else []:
                if "=" not in parameter:
                    raise ValueError(f"Malformed argument in {match.group(1)}: {parameter.strip()}")
                key, value = parameter.split("=", 1)
                if not re.fullmatch(r"[A-Za-z_][\w]*", key.strip()):
                    raise ValueError(f"Malformed argument name: {key.strip()}")
                args[key.strip()] = self._value(value.strip())
            name = match.group(1)
            canonical_name = next(
                (tool["function"]["name"] for tool in TOOL_DEFINITIONS if tool["function"]["name"].lower() == name.lower()),
                name,
            )
            calls.append(ProtocolToolCall(f"nemotron-{index + 1}", canonical_name, json.dumps(args)))
        if not calls:
            raise ValueError("The Nemotron tool call list is empty.")
        return calls

    @staticmethod
    def _split(text: str, delimiter: str) -> list[str]:
        parts, start, depth, quote, escaped = [], 0, 0, "", False
        for index, char in enumerate(text):
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = ""
            elif char in "'\"":
                quote = char
            elif char in "[{(":
                depth += 1
            elif char in "]})":
                depth -= 1
            elif char == delimiter and depth == 0:
                parts.append(text[start:index])
                start = index + 1
        parts.append(text[start:])
        return parts

    @staticmethod
    def _value(value: str) -> Any:
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            try:
                return ast.literal_eval(value)
            except (ValueError, SyntaxError):
                return value

    def format_tool_result(self, messages: list[dict]) -> list[dict]:
        return [
            {"role": "user", "content": f"Tool result ({message.get('tool_call_id', '')}):\n{message.get('content', '')}"}
            if message.get("role") == "tool" else message
            for message in messages
        ]


def get_tool_protocol(name: str) -> OpenAIToolProtocol | NemotronToolProtocol:
    if name == "openai":
        return OpenAIToolProtocol()
    if name == "nemotron":
        return NemotronToolProtocol()
    raise ValueError(f"Unknown tool protocol '{name}'. Choose 'openai' or 'nemotron'.")
