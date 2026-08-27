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

If none of the function can be used, point it out. For autonomous repository work, unknown filenames, paths, symbols, modules, repository structure, implementation details, and test locations are not missing parameters: use the available tools to discover them.

You should only return the function call in tools call sections.

If you decide to invoke any of the function(s), you MUST put it in the format of:

<TOOLCALL>[func_name1(params_name1=params_value1, params_name2=params_value2...), func_name2(params)]</TOOLCALL>

You SHOULD NOT include any other text in a response containing a tool call.

Here is a list of functions in JSON format that you can invoke.

The functions below are executable actions in the current repository workspace, not hypothetical examples. If repository contents are relevant, gather evidence with tools before producing a final answer. Do not merely describe which tools you would use: invoke them. Bash may discover information without knowing exact filenames in advance. For example:

<TOOLCALL>[bash(command="git ls-files | head -200")]</TOOLCALL>

Do not return an empty tool-call list. Return one or more valid tool calls, or a final answer only when the task is actually complete.
"""

    def prepare_request(self, messages: list[dict], tools: list[dict]) -> list[dict]:
        functions = [tool["function"] for tool in tools]
        available = f"<AVAILABLE_TOOLS>{json.dumps(functions, separators=(',', ':'))}</AVAILABLE_TOOLS>"
        prepared = []
        for message in messages:
            clean = {k: v for k, v in message.items() if k not in {"extra", "tool_calls", "tool_call_id"}}
            if clean.get("role") == "tool":
                clean = {"role": "user", "content": f"Tool result ({message.get('tool_call_id', '')}):\n{clean.get('content', '')}"}
            prepared.append(clean)
        prompt = (
            f"{self._instruction}{available}\n\n"
            "For repository tasks, use the available inspection tools whenever repository evidence is required.\n\n"
        )
        for message in prepared:
            if message.get("role") == "user":
                message["content"] = f"{prompt}{message.get('content', '')}"
                break
        else:
            prepared.append({"role": "user", "content": prompt})
        return prepared

    def parse_response(self, content: str, tool_calls: list, *, format_error_template: str, finish_reason: str) -> list[dict]:
        if tool_calls:
            raise self._error(content, "Native tool calls are not valid for this content-based protocol.", format_error_template, finish_reason)
        match = re.search(r"<TOOLCALL>(.*?)</TOOLCALL>", content, re.DOTALL)
        if not match:
            if "<TOOLCALL>" in content or "</TOOLCALL>" in content:
                raise self._error(content, "The <TOOLCALL> block is incomplete or malformed.", format_error_template, finish_reason)
            return []
        if content.strip() != match.group(0).strip():
            raise self._error(content, "A response containing a tool call must contain only the tool-call block.", format_error_template, finish_reason)
        try:
            calls = self.response_tool_calls(content)
            return parse_toolcall_actions(
                calls,
                format_error_template=format_error_template,
                template_kwargs={"finish_reason": finish_reason},
            )
        except FormatError as error:
            detail = error.messages[0].get("content", "The tool arguments were invalid.")
            raise self._error(content, detail, format_error_template, finish_reason) from error
        except ValueError as error:
            raise self._error(content, str(error), format_error_template, finish_reason) from error

    def response_tool_calls(self, content: str) -> list[ProtocolToolCall]:
        match = re.search(r"<TOOLCALL>(.*?)</TOOLCALL>", content, re.DOTALL)
        return self._parse_calls(match.group(1)) if match else []

    @staticmethod
    def _error(content: str, error: str, template: str, finish_reason: str) -> FormatError:
        from jinja2 import StrictUndefined, Template

        available = "\n".join(f"- {tool['function']['name']}" for tool in TOOL_DEFINITIONS)
        feedback = (
            "FORMAT ERROR: Your previous response contained an invalid tool-call block for the Nemotron protocol.\n\n"
            f"You returned:\n{content}\n\nReason: {error}\n\n"
            f"Available tools:\n{available}\n\n"
            "If repository information is unknown, use bash or read_text to discover it.\n"
            "Do not explain this error. Return one or more valid tool calls, or a final answer only if the task is actually complete."
        )
        return FormatError({
            "role": "user",
            "content": Template(template, undefined=StrictUndefined).render(
                error=feedback, actions=[], has_tool_calls=True, finish_reason=finish_reason
            ),
            "extra": {"interrupt_type": "FormatError"},
        })

    def _parse_calls(self, text: str) -> list[ProtocolToolCall]:
        text = text.strip()
        if not (text.startswith("[") and text.endswith("]")):
            raise ValueError("Nemotron tool calls must contain a bracketed list.")
        if not text[1:-1].strip():
            raise ValueError("The Nemotron tool-call list is empty.")
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
            if canonical_name == name and not any(tool["function"]["name"] == name for tool in TOOL_DEFINITIONS):
                available = ", ".join(tool["function"]["name"] for tool in TOOL_DEFINITIONS)
                raise ValueError(f"Unknown tool '{name}'. Available tools: {available}.")
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
