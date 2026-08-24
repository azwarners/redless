"""Parse actions & format observations with toolcalls"""

import json
import time

from jinja2 import StrictUndefined, Template

from minisweagent.exceptions import FormatError
from minisweagent.models.utils.openai_multimodal import expand_multimodal_content
from minisweagent.utils.output import shape_outputs_for_turn

BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": "Execute a bash command",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The bash command to execute",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Optional timeout for this command, in seconds",
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
}

TEXT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "replace_text",
            "description": "Replace one exact, unique text fragment in a UTF-8 file in the selected environment",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_text",
            "description": "Read a bounded range of lines from a UTF-8 file in the selected environment",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "start_line": {"type": "integer", "minimum": 1}, "end_line": {"type": "integer", "minimum": 1}},
                "required": ["path", "start_line", "end_line"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_text",
            "description": "Create a new UTF-8 text file in the selected environment; fail if it already exists",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
]

TOOL_DEFINITIONS = [BASH_TOOL, *TEXT_TOOLS]


def parse_action_arguments(tool_name: str, args: object) -> dict:
    """Validate a supported tool's JSON arguments and return normalized action fields."""
    if not isinstance(args, dict):
        raise ValueError("Tool arguments must be a JSON object.")
    schemas = {
        "bash": ({"command", "timeout_seconds"}, {"command"}),
        "replace_text": ({"path", "old_text", "new_text"}, {"path", "old_text", "new_text"}),
        "read_text": ({"path", "start_line", "end_line"}, {"path", "start_line", "end_line"}),
        "create_text": ({"path", "content"}, {"path", "content"}),
    }
    if tool_name not in schemas:
        raise ValueError(f"Unknown tool '{tool_name}'.")
    allowed, required = schemas[tool_name]
    missing = required - args.keys()
    extra = args.keys() - allowed
    if missing:
        raise ValueError(f"Missing {', '.join(repr(key) for key in sorted(missing))} argument(s) for {tool_name}.")
    if extra:
        raise ValueError(f"Unexpected argument(s) for {tool_name}: {', '.join(sorted(extra))}.")
    if tool_name == "bash" and (
        not isinstance(args["command"], str)
        or ("timeout_seconds" in args and (isinstance(args["timeout_seconds"], bool) or not isinstance(args["timeout_seconds"], int) or args["timeout_seconds"] < 1))
    ):
        raise ValueError("'command' must be a string and 'timeout_seconds' must be a positive integer.")
    if tool_name != "bash" and any(not isinstance(args[key], str) for key in required if key != "start_line" and key != "end_line"):
        raise ValueError(f"Arguments for {tool_name} must use strings for path and text values.")
    if tool_name == "read_text" and any(
        isinstance(args[key], bool) or not isinstance(args[key], int) or args[key] < 1 for key in ("start_line", "end_line")
    ):
        raise ValueError("'start_line' and 'end_line' must be positive integers.")
    return {"command": args["command"], **({"timeout_seconds": args["timeout_seconds"]} if "timeout_seconds" in args else {})} if tool_name == "bash" else {"tool": tool_name, **args}


def parse_toolcall_actions(
    tool_calls: list, *, format_error_template: str, template_kwargs: dict | None = None
) -> list[dict]:
    """Parse tool calls from the response. Raises FormatError if unknown tool or invalid args.

    ``template_kwargs`` are extra variables exposed to ``format_error_template`` (e.g.
    ``{"finish_reason": ...}`` so a template can distinguish a real format mistake from a
    ``max_tokens`` truncation).
    """
    template_kwargs = template_kwargs or {}
    if not tool_calls:
        raise FormatError(
            {
                "role": "user",
                "content": Template(format_error_template, undefined=StrictUndefined).render(
                    error="No tool calls found in the response. Every response MUST include at least one tool call.",
                    actions=[],
                    has_tool_calls=False,
                    **template_kwargs,
                ),
                "extra": {"interrupt_type": "FormatError"},
            }
        )
    actions = []
    for tool_call in tool_calls:
        error_msg = ""
        args = {}
        try:
            args = json.loads(tool_call.function.arguments)
        except Exception as e:
            error_msg = f"Error parsing tool call arguments: {e}."
        try:
            action = parse_action_arguments(tool_call.function.name, args)
        except ValueError as e:
            error_msg += str(e)
        if error_msg:
            raise FormatError(
                {
                    "role": "user",
                    "content": Template(format_error_template, undefined=StrictUndefined).render(
                        actions=[], error=error_msg.strip(), has_tool_calls=True, **template_kwargs
                    ),
                    "extra": {"interrupt_type": "FormatError"},
                }
            )
        actions.append({**action, "tool_call_id": tool_call.id})
    return actions


def format_toolcall_observation_messages(
    *,
    actions: list[dict],
    outputs: list[dict],
    observation_template: str,
    template_vars: dict | None = None,
    multimodal_regex: str = "",
    output_config: dict | None = None,
) -> list[dict]:
    """Format execution outputs into tool result messages."""
    not_executed = {"output": "", "returncode": -1, "exception_info": "action was not executed"}
    padded_outputs = outputs + [not_executed] * (len(actions) - len(outputs))
    def render(output: dict) -> str:
        return Template(observation_template, undefined=StrictUndefined).render(
            output=output, **(template_vars or {})
        )
    results = []
    shaped_outputs = shape_outputs_for_turn(padded_outputs, output_config, render=render)
    for action, source, output in zip(actions, padded_outputs, shaped_outputs):
        raw_output = source.get("output", "")
        content = render(output)
        msg = {
            "content": content,
            "extra": {
                "raw_output": raw_output,
                "returncode": output.get("returncode"),
                "timestamp": time.time(),
                "exception_info": output.get("exception_info"),
                "timeout": output.get("timeout", False),
                "duration_seconds": output.get("duration_seconds"),
                "truncated": output.get("truncated", False),
                "original_chars": output.get("original_chars"),
                "displayed_chars": output.get("displayed_chars"),
                "turn_budget": output.get("turn_budget", 0),
                "turn_truncated": output.get("turn_truncated", False),
                **output.get("extra", {}),
            },
        }
        if "tool_call_id" in action:
            msg["tool_call_id"] = action["tool_call_id"]
            msg["role"] = "tool"
        else:
            msg["role"] = "user"  # human issued commands
        if multimodal_regex:
            msg = expand_multimodal_content(msg, pattern=multimodal_regex)
        results.append(msg)
    return results
