"""Basic agent class. See https://mini-swe-agent.com/latest/advanced/control_flow/ for visual explanation
or https://minimal-agent.com for a tutorial on the basic building principles.
"""

import json
import logging
import sys
import time
import traceback
from pathlib import Path
from typing import Literal

from jinja2 import StrictUndefined, Template
from pydantic import BaseModel, Field

from minisweagent import FORK_NAME, UPSTREAM_VERSION, Environment, Model, __version__
from minisweagent.exceptions import FormatError, InterruptAgentFlow, LimitsExceeded, Submitted, TimeExceeded
from minisweagent.utils.serialize import recursive_merge


class AgentConfig(BaseModel):
    """Check the config files in minisweagent/config for example settings."""

    system_template: str
    """Template for the system message (the first message)."""
    instance_template: str
    """Template for the first user message specifying the task (the second message overall)."""
    step_limit: int = 0
    """Maximum number of steps the agent can take."""
    cost_limit: float = 3.0
    """Stop agent after exceeding (!) this cost."""
    wall_time_limit_seconds: int = 0
    """Stop agent after this many seconds of wall-clock time. 0 means no limit."""
    max_consecutive_format_errors: int = 3
    """Exit after this many format errors in a row (0 = no limit)."""
    output_path: Path | None = None
    """Save the trajectory to this path."""
    show_progress: bool = False
    """Print concise model and tool progress to stderr."""
    progress_interval_seconds: float = 60.0
    """Repeat pending-model status after this interval. Set 0 to disable it."""
    call_warning_threshold: int = 0
    """Warn once after this many successful model calls. Set 0 to disable it."""
    model_time_warning_seconds: float = 0.0
    """Warn once after this much cumulative model time. Set 0 to disable it."""
    context_mode: Literal["full", "projected"] = "full"
    """Choose the full trajectory or a deterministic temporary model context."""
    projected_context_max_chars: int = Field(default=24000, ge=4000)
    """Maximum projected context size. The full trajectory is always preserved."""


class DefaultAgent:
    def __init__(self, model: Model, env: Environment, *, config_class: type = AgentConfig, **kwargs):
        """See the `AgentConfig` class for permitted keyword arguments."""
        self.config = config_class(**kwargs)
        self.messages: list[dict] = []
        self.model = model
        self.env = env
        self.extra_template_vars = {}
        self.logger = logging.getLogger("agent")
        self.cost = 0.0
        self.n_calls = 0
        self.n_tool_calls = 0
        self.model_elapsed_seconds = 0.0
        self.tool_elapsed_seconds = 0.0
        self.n_consecutive_format_errors = 0
        self._operator_warning_emitted = False
        self.live_context_ledger: list[dict] = []
        self._start_time = time.time()

    def get_template_vars(self, **kwargs) -> dict:
        return recursive_merge(
            self.config.model_dump(),
            self.env.get_template_vars(),
            self.model.get_template_vars(),
            {
                "n_model_calls": self.n_calls,
                "model_cost": self.cost,
                "elapsed_seconds": int(time.time() - self._start_time),
                "model_elapsed_seconds": self.model_elapsed_seconds,
                "tool_elapsed_seconds": self.tool_elapsed_seconds,
                "n_tool_calls": self.n_tool_calls,
            },
            self.extra_template_vars,
            kwargs,
        )

    def _render_template(self, template: str) -> str:
        return Template(template, undefined=StrictUndefined).render(**self.get_template_vars())

    @staticmethod
    def _message_chars(message: dict) -> int:
        return len(json.dumps(message, sort_keys=True, default=str))

    @staticmethod
    def _is_assistant_message(message: dict) -> bool:
        return message.get("role") == "assistant" or message.get("object") == "response"

    def _working_ledger_message(self) -> dict:
        return self.model.format_message(
            role="user",
            content="Deterministic working ledger\n" + json.dumps(self.live_context_ledger, indent=2, sort_keys=True),
        )

    def _message_cycles(self) -> list[list[dict]]:
        cycles: list[list[dict]] = []
        current: list[dict] = []
        for message in self.messages[2:]:
            if self._is_assistant_message(message) and current:
                cycles.append(current)
                current = []
            current.append(message)
        if current:
            cycles.append(current)
        return cycles

    def _compact_cycle(self, cycle: list[dict], budget: int) -> list[dict]:
        if not cycle:
            return []
        result = [cycle[0]]
        remaining = max(0, budget - self._message_chars(cycle[0]))
        for message in cycle[1:]:
            if self._message_chars(message) <= remaining:
                result.append(message)
                remaining -= self._message_chars(message)
                continue
            compact = dict(message)
            content = compact.get("content", compact.get("output", ""))
            if isinstance(content, str):
                compact_content = content[-max(0, remaining) :]
                if "content" in compact:
                    compact["content"] = "[older content omitted from projected context]\n" + compact_content
                else:
                    compact["output"] = "[older content omitted from projected context]\n" + compact_content
            result.append(compact)
        return result

    def _messages_for_model(self) -> list[dict]:
        if self.config.context_mode == "full" or len(self.messages) <= 2:
            return self.messages
        ledger = self._working_ledger_message()
        projected = [*self.messages[:2], ledger]
        budget = self.config.projected_context_max_chars
        used = sum(self._message_chars(message) for message in projected)
        for cycle in reversed(self._message_cycles()):
            cycle_chars = sum(self._message_chars(message) for message in cycle)
            if used + cycle_chars <= budget:
                projected[3:3] = cycle
                used += cycle_chars
            elif len(projected) == 3:
                projected.extend(self._compact_cycle(cycle, max(0, budget - used)))
                break
        return projected

    def _record_ledger_entry(self, action: dict, output: dict) -> None:
        tool = action.get("tool", "bash")
        returncode = output.get("returncode")
        exception_type = output.get("extra", {}).get("exception_type")
        failed = returncode not in (0, None) or bool(exception_type) or output.get("timeout", False)
        if failed:
            raw = output.get("output", "") or ""
            self.live_context_ledger.append(
                {
                    "kind": "failure",
                    "tool": tool,
                    "returncode": returncode,
                    "exception_type": exception_type,
                    "timeout": output.get("timeout", False),
                    "error_tail": raw[-480:],
                }
            )
            return
        if tool == "bash":
            lines = [line.strip() for line in (output.get("output", "") or "").splitlines() if line.strip()]
            self.live_context_ledger.append(
                {
                    "kind": "bash",
                    "command": action.get("command", ""),
                    "returncode": returncode,
                    "status": lines[-1] if lines else "completed",
                }
            )
        elif tool == "read_text":
            self.live_context_ledger.append(
                {
                    "kind": "text_read",
                    "path": action.get("path", ""),
                    "start_line": action.get("start_line"),
                    "end_line": action.get("end_line"),
                }
            )
        else:
            self.live_context_ledger.append({"kind": tool, "path": action.get("path", "")})

    def add_messages(self, *messages: dict) -> list[dict]:
        self.logger.debug(messages)  # set log level to debug to see
        self.messages.extend(messages)
        return list(messages)

    def _progress(self, message: str, *, end: str = "\n") -> None:
        if self.config.show_progress:
            print(f"[{time.strftime('%H:%M:%S')}] [mini-swe-agent-slow] {message}", file=sys.stderr, end=end, flush=True)

    def _progress_append(self, message: str) -> None:
        if self.config.show_progress:
            print(message, file=sys.stderr, end="", flush=True)

    @staticmethod
    def _action_description(action: dict, output: dict) -> str:
        tool = action.get("tool", "bash")
        if tool == "bash":
            detail = f"command: {action.get('command', '')}"
        elif tool == "read_text":
            detail = (
                f"file: {action.get('path', '')}"
                f" (lines {action.get('start_line')}-{action.get('end_line')})"
            )
        elif tool == "replace_text":
            detail = (
                f"file: {action.get('path', '')}; replaced "
                f"{len(action.get('old_text', ''))} chars with {len(action.get('new_text', ''))} chars"
            )
        elif tool == "create_text":
            detail = f"file: {action.get('path', '')}; created {len(action.get('content', ''))} chars"
        else:
            detail = str(action)
        detail = " ".join(detail.split())
        return f"Action description: {detail}"[:240]

    def _warn_if_threshold_crossed(self) -> None:
        if self._operator_warning_emitted:
            return
        call_threshold_crossed = (
            self.config.call_warning_threshold > 0 and self.n_calls >= self.config.call_warning_threshold
        )
        time_threshold_crossed = (
            self.config.model_time_warning_seconds > 0
            and self.model_elapsed_seconds >= self.config.model_time_warning_seconds
        )
        if not (call_threshold_crossed or time_threshold_crossed):
            return
        trajectory = str(self.config.output_path) if self.config.output_path else "<not configured>"
        print(
            f"[{time.strftime('%H:%M:%S')}] [mini-swe-agent-slow] WARNING: model-call threshold reached; "
            f"calls={self.n_calls}, cumulative_model_time={self.model_elapsed_seconds:.1f}s, "
            f"trajectory={trajectory}. This is a warning, not an error; inspect the saved "
            "trajectory or interrupt deliberately if needed.",
            file=sys.stderr,
            flush=True,
        )
        self._operator_warning_emitted = True

    def handle_uncaught_exception(self, e: Exception) -> list[dict]:
        return self.add_messages(
            self.model.format_message(
                role="exit",
                content=str(e),
                extra={
                    "exit_status": type(e).__name__,
                    "submission": "",
                    "exception_str": str(e),
                    "traceback": traceback.format_exc(),
                },
            )
        )

    def run(self, task: str = "", **kwargs) -> dict:
        """Run step() until agent is finished. Returns dictionary with exit_status, submission keys."""
        self.extra_template_vars |= {"task": task, **kwargs}
        self.messages = []
        self.add_messages(
            self.model.format_message(role="system", content=self._render_template(self.config.system_template)),
            self.model.format_message(role="user", content=self._render_template(self.config.instance_template)),
        )
        while True:
            try:
                self.step()
                self.n_consecutive_format_errors = 0  # reset on any clean step
            except FormatError as e:
                # The call was billed before parsing failed, so query() never got to charge it.
                self.cost += e.messages[0].get("extra", {}).get("cost", 0.0)
                self.n_consecutive_format_errors += 1
                if 0 < self.config.max_consecutive_format_errors <= self.n_consecutive_format_errors:
                    self.add_messages(
                        *e.messages,
                        {
                            "role": "exit",
                            "content": "RepeatedFormatError",
                            "extra": {"exit_status": "RepeatedFormatError", "submission": ""},
                        },
                    )
                else:
                    self.add_messages(*e.messages)
            except InterruptAgentFlow as e:
                self.add_messages(*e.messages)
            except Exception as e:
                self.handle_uncaught_exception(e)
                raise
            finally:
                self.save(self.config.output_path)
            if self.messages[-1].get("role") == "exit":
                break
        return self.messages[-1].get("extra", {})

    def step(self) -> list[dict]:
        """Query the LM, execute actions."""
        message = self.query()
        if message.get("extra", {}).get("is_final"):
            final_text = message["extra"]["final_text"]
            self._progress("Final response received; printing result.")
            raise Submitted(
                {
                    "role": "exit",
                    "content": final_text,
                    "extra": {"exit_status": "Submitted", "submission": final_text},
                }
            )
        return self.execute_actions(message)

    def query(self) -> dict:
        """Query the model and return model messages. Override to add hooks."""
        if 0 < self.config.step_limit <= self.n_calls or 0 < self.config.cost_limit <= self.cost:
            raise LimitsExceeded(
                {
                    "role": "exit",
                    "content": "LimitsExceeded",
                    "extra": {"exit_status": "LimitsExceeded", "submission": ""},
                }
            )
        if 0 < self.config.wall_time_limit_seconds <= int(time.time() - self._start_time):
            raise TimeExceeded(
                {
                    "role": "exit",
                    "content": "TimeExceeded",
                    "extra": {"exit_status": "TimeExceeded", "submission": ""},
                }
            )
        self.n_calls += 1
        self._progress(f"Waiting for model response (call {self.n_calls})…")
        started = time.monotonic()
        try:
            message = self.model.query(self._messages_for_model())
        finally:
            self.model_elapsed_seconds += time.monotonic() - started
        self.cost += message.get("extra", {}).get("cost", 0.0)
        self.add_messages(message)
        self._progress(f"Received model response in {time.monotonic() - started:.1f}s.")
        self._warn_if_threshold_crossed()
        return message

    def execute_actions(self, message: dict) -> list[dict]:
        """Execute actions in message, add observation messages, return them."""
        outputs = []
        actions = message.get("extra", {}).get("actions", [])
        for index, action in enumerate(actions, start=1):
            self._progress(f"Running {action.get('tool', 'bash')} action {index}/{len(actions)}…", end="")
            started = time.monotonic()
            if action.get("tool", "bash") == "bash":
                output = self.env.execute(action, timeout=action.get("timeout_seconds"))
            else:
                output = self.env.execute_text(action)
            output["duration_seconds"] = time.monotonic() - started
            if output.get("extra", {}).get("exception_type") == "TimeoutExpired":
                output["timeout"] = True
            outputs.append(output)
            self._record_ledger_entry(action, output)
            self.tool_elapsed_seconds += output["duration_seconds"]
            self.n_tool_calls += 1
            self._progress_append(f" Action {index}/{len(actions)} finished in {output['duration_seconds']:.1f}s.\n")
            self._progress(self._action_description(action, output))
        return self.add_messages(*self.model.format_observation_messages(message, outputs, self.get_template_vars()))

    def serialize(self, *extra_dicts) -> dict:
        """Serialize agent state to a json-compatible nested dictionary for saving."""
        last_message = self.messages[-1] if self.messages else {}
        last_extra = last_message.get("extra", {})
        agent_data = {
            "info": {
                "model_stats": {
                    "instance_cost": self.cost,
                    "api_calls": self.n_calls,
                    "tool_calls": self.n_tool_calls,
                    "model_elapsed_seconds": self.model_elapsed_seconds,
                    "tool_elapsed_seconds": self.tool_elapsed_seconds,
                },
                "config": {
                    "agent": self.config.model_dump(mode="json"),
                    "agent_type": f"{self.__class__.__module__}.{self.__class__.__name__}",
                },
                "mini_version": __version__,
                "fork_name": FORK_NAME,
                "upstream_version": UPSTREAM_VERSION,
                "live_context_ledger": self.live_context_ledger,
                "exit_status": last_extra.get("exit_status", ""),
                "submission": last_extra.get("submission", ""),
            },
            "messages": self.messages,
            "trajectory_format": "mini-swe-agent-1.1",
        }
        return recursive_merge(agent_data, self.model.serialize(), self.env.serialize(), *extra_dicts)

    def save(self, path: Path | None, *extra_dicts) -> dict:
        """Save the trajectory of the agent to a file if path is given. Returns full serialized data.
        You can pass additional dictionaries with extra data to be (recursively) merged into the output data.
        """
        data = self.serialize(*extra_dicts)
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2))
        return data
