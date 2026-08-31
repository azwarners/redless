"""Deterministic shaping of command observations."""

from collections.abc import Callable
from typing import Any

TURN_OMISSION_NOTICE = "[output omitted by turn budget]\n"


def shape_output(
    output: dict[str, Any], *, max_chars: int = 6000, head_chars: int = 1200, tail_chars: int = 3600, error_tail_chars: int = 4800
) -> dict[str, Any]:
    raw = output.get("output", "") or ""
    original_chars = len(raw)
    is_error = output.get("returncode", 0) != 0 or bool(output.get("exception_info"))
    retained_tail = min(error_tail_chars if is_error else tail_chars, max_chars)
    retained_head = min(head_chars, max_chars - retained_tail)
    if original_chars <= max_chars:
        retained = raw
        truncated = False
    else:
        marker = f"\n...[{original_chars - retained_head - retained_tail} characters truncated]...\n"
        excess = retained_head + len(marker) + retained_tail - max_chars
        if excess > 0:
            retained_head = max(0, retained_head - excess)
            marker = f"\n...[{original_chars - retained_head - retained_tail} characters truncated]...\n"
        retained = raw[:retained_head] + marker + raw[-retained_tail:]
        retained = retained[-max_chars:] if len(retained) > max_chars else retained
        truncated = True
    return {
        **output,
        "output": retained,
        "truncated": truncated,
        "original_chars": original_chars,
        "retained_chars": len(retained),
    }


def shape_outputs_for_turn(
    outputs: list[dict[str, Any]],
    config: dict[str, int] | None = None,
    render: Callable[[dict[str, Any]], str] | None = None,
) -> list[dict[str, Any]]:
    """Apply per-output shaping and an optional aggregate budget for one turn."""
    settings = config or {}
    shape_settings = {
        key: settings[key]
        for key in ("max_chars", "head_chars", "tail_chars", "error_tail_chars")
        if key in settings
    }
    max_turn_chars = settings.get("max_turn_chars", 0)
    minimum_chars = settings.get("minimum_chars_per_observation", 256)
    shaped = [shape_output(output, **shape_settings) for output in outputs]
    if not max_turn_chars:
        return [
            {
                **output,
                "turn_budget": 0,
                "turn_truncated": False,
                "displayed_chars": len(output.get("output", "")),
            }
            for output in shaped
        ]
    if max_turn_chars < len(outputs) * minimum_chars:
        raise ValueError("tool_output.max_turn_chars is too small for the configured observation minimum")
    measure = render or (lambda output: output["output"])
    total = sum(len(measure(output)) for output in shaped)
    if total <= max_turn_chars:
        targets = [len(measure(output)) for output in shaped]
    else:
        minimum_outputs = [shape_output(source, max_chars=minimum_chars, **{key: value for key, value in shape_settings.items() if key != "max_chars"}) for source in outputs]
        targets = [len(measure(output)) for output in minimum_outputs]
        remaining = max_turn_chars - sum(targets)
        if remaining < 0:
            raise ValueError("tool_output.max_turn_chars is too small for rendered observations")
        order = [
            index
            for index, output in enumerate(shaped)
            if output.get("returncode", 0) != 0 or output.get("exception_info")
        ] + [
            index
            for index, output in enumerate(shaped)
            if not (output.get("returncode", 0) != 0 or output.get("exception_info"))
        ]
        for index in order:
            extra = min(len(measure(shaped[index])) - targets[index], remaining)
            targets[index] += extra
            remaining -= extra
    result = []
    for source, output, target in zip(outputs, shaped, targets):
        if render:
            low, high = 0, shape_settings.get("max_chars", 6000)
            reduced = shape_output(source, max_chars=low + 1, **{key: value for key, value in shape_settings.items() if key != "max_chars"})
            while low <= high:
                midpoint = (low + high) // 2
                candidate = shape_output(source, max_chars=max(1, midpoint), **{key: value for key, value in shape_settings.items() if key != "max_chars"})
                if len(render(candidate)) <= target:
                    reduced = candidate
                    low = midpoint + 1
                else:
                    high = midpoint - 1
        else:
            reduced = shape_output(
                source,
                max_chars=max(1, target),
                **{key: value for key, value in shape_settings.items() if key != "max_chars"},
            )
        turn_truncated = len(measure(reduced)) < len(measure(output))
        if turn_truncated and TURN_OMISSION_NOTICE not in reduced["output"]:
            available = max(0, reduced["retained_chars"] - len(TURN_OMISSION_NOTICE))
            reduced["output"] = (
                TURN_OMISSION_NOTICE + reduced["output"][-available:]
                if available
                else TURN_OMISSION_NOTICE[:target]
            )
            if render:
                low, high = 0, max(0, len(reduced["output"]) - len(TURN_OMISSION_NOTICE))
                while low <= high:
                    midpoint = (low + high) // 2
                    candidate = {
                        **reduced,
                        "output": TURN_OMISSION_NOTICE + reduced["output"][-midpoint:] if midpoint else TURN_OMISSION_NOTICE,
                    }
                    if len(render(candidate)) <= target:
                        reduced = candidate
                        low = midpoint + 1
                    else:
                        high = midpoint - 1
            reduced["retained_chars"] = len(reduced["output"])
        result.append(
            {
                **reduced,
                "turn_budget": max_turn_chars,
                "turn_truncated": turn_truncated,
                "displayed_chars": reduced["retained_chars"],
            }
        )
    if render and sum(len(render(output)) for output in result) > max_turn_chars:
        raise AssertionError("rendered tool observations exceed tool_output.max_turn_chars")
    return result
