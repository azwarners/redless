"""Small adapters for model-specific tool protocols."""

from dataclasses import dataclass
from types import SimpleNamespace

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


def get_tool_protocol(name: str) -> OpenAIToolProtocol:
    if name == "openai":
        return OpenAIToolProtocol()
    raise ValueError(f"Unknown tool protocol '{name}'. Choose 'openai'.")
