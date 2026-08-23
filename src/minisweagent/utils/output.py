"""Deterministic shaping of command observations."""

from typing import Any


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
