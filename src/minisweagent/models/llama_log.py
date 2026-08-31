"""Parse the human-readable timing lines emitted by llama-server."""

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LlamaServerStats:
    context_tokens: int | None = None
    context_limit: int | None = None
    prompt_tokens: int | None = None
    prompt_seconds: float | None = None
    prompt_tokens_per_second: float | None = None
    generation_tokens: int | None = None
    generation_seconds: float | None = None
    generation_tokens_per_second: float | None = None
    total_tokens: int | None = None
    total_seconds: float | None = None
    graphs_reused: int | None = None


_PROMPT = re.compile(
    r"prompt eval time\s*=\s*([\d.]+)\s*ms\s*/\s*(\d+)\s*tokens.*?([\d.]+)\s*tokens per second"
)
_EVAL = re.compile(
    r"eval time\s*=\s*([\d.]+)\s*ms\s*/\s*(\d+)\s*tokens.*?([\d.]+)\s*tokens per second"
)
_TOTAL = re.compile(r"total time\s*=\s*([\d.]+)\s*ms\s*/\s*(\d+)\s*tokens")
_GRAPHS = re.compile(r"graphs reused\s*=\s*(\d+)")
_CONTEXT = re.compile(r"n_ctx_slot\s*=\s*(\d+).*?task\.n_tokens\s*=\s*(\d+)")
_STREAM_PROMPT = re.compile(r"prompt processing, n_tokens\s*=\s*(\d+).*?([\d.]+)\s*tokens per second")


def parse_llama_server_log(text: str) -> LlamaServerStats | None:
    """Return the latest completed timing block from a llama-server log."""
    prompt = list(_PROMPT.finditer(text))
    evaluation = list(_EVAL.finditer(text))
    total = list(_TOTAL.finditer(text))
    if not prompt and not evaluation and not total:
        # Older llama.cpp versions expose prompt processing while a request is
        # still active. It is useful as a fallback, but is not a full block.
        partial = list(_STREAM_PROMPT.finditer(text))
        if not partial:
            return None
        match = partial[-1]
        return LlamaServerStats(
            prompt_tokens=int(match.group(1)),
            prompt_tokens_per_second=float(match.group(2)),
        )

    prompt_match = prompt[-1] if prompt else None
    eval_match = evaluation[-1] if evaluation else None
    total_match = total[-1] if total else None
    context_matches = list(_CONTEXT.finditer(text))
    context_match = context_matches[-1] if context_matches else None
    graphs = list(_GRAPHS.finditer(text))
    graph_match = graphs[-1] if graphs else None
    return LlamaServerStats(
        context_tokens=int(context_match.group(2)) if context_match else None,
        context_limit=int(context_match.group(1)) if context_match else None,
        prompt_tokens=int(prompt_match.group(2)) if prompt_match else None,
        prompt_seconds=float(prompt_match.group(1)) / 1000 if prompt_match else None,
        prompt_tokens_per_second=float(prompt_match.group(3)) if prompt_match else None,
        generation_tokens=int(eval_match.group(2)) if eval_match else None,
        generation_seconds=float(eval_match.group(1)) / 1000 if eval_match else None,
        generation_tokens_per_second=float(eval_match.group(3)) if eval_match else None,
        total_tokens=int(total_match.group(2)) if total_match else None,
        total_seconds=float(total_match.group(1)) / 1000 if total_match else None,
        graphs_reused=int(graph_match.group(1)) if graph_match else None,
    )


def read_llama_server_log(path: str | Path | None) -> LlamaServerStats | None:
    if not path:
        return None
    try:
        return parse_llama_server_log(Path(path).read_text(errors="replace"))
    except OSError:
        return None


def format_llama_server_stats(stats: LlamaServerStats) -> str:
    """Format stats for the operator; this string is never a model message."""
    parts = []
    if stats.context_tokens is not None:
        context = f"context={stats.context_tokens}"
        if stats.context_limit:
            context += f"/{stats.context_limit} ({stats.context_tokens / stats.context_limit:.1%})"
        parts.append(context)
    if stats.prompt_tokens is not None:
        parts.append(f"prompt={stats.prompt_tokens} tokens")
    if stats.prompt_tokens_per_second is not None:
        prefill = f"prefill={stats.prompt_tokens_per_second:.2f} tok/s"
        if stats.prompt_seconds is not None:
            prefill += f" ({stats.prompt_seconds:.1f}s)"
        parts.append(prefill)
    if stats.generation_tokens is not None:
        parts.append(f"generated={stats.generation_tokens} tokens")
    if stats.generation_tokens_per_second is not None:
        generation = f"generation={stats.generation_tokens_per_second:.2f} tok/s"
        if stats.generation_seconds is not None:
            generation += f" ({stats.generation_seconds:.1f}s)"
        parts.append(generation)
    if stats.total_seconds is not None:
        parts.append(f"total={stats.total_seconds:.1f}s")
    if stats.graphs_reused is not None:
        parts.append(f"graphs_reused={stats.graphs_reused}")
    return "llama.cpp: " + ", ".join(parts)
