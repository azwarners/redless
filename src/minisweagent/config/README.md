# Configs

* `mini.yaml` - Compatibility-default config for the interactive agent.
* `default.yaml` - Default config for the `default.py` agent.
* `slow_local.yaml` - Recommended fork profile for `mini-slow`: tool-call mode,
  visible progress (including a pending-request update every minute), a 10-minute Bash timeout, separate connection/read timeouts, no
  automatic request replay, no local cost lookup, and compact tool output.

`slow_local.yaml` is designed for local llama.cpp servers. It uses the fork's direct
`llama_cpp` transport (not LiteLLM), so streamed draft tokens can be shown while a
request is running. Set `model_name` and the endpoint in a small local settings file, then run
`mini-slow -c slow_local.yaml -c llama-local.yaml`. Its `max_retries: 0` default
avoids replaying an expensive or ambiguous generation, while `tool_output` limits
only live tool observations; raw output remains available in the trajectory.
The shipped profile has no model-call limit; set `agent.step_limit` to a positive
number when a run needs one.
The profile caps one turn's rendered observations at 12,000 characters and reserves
256 characters for each returned observation; set `max_turn_chars: 0` to disable the
aggregate cap.

The slow profile also warns once after 8 model calls or 1,800 cumulative model
seconds. The warning includes the trajectory path and does not change retry policy or
exit status.

Set `model.llama_log_path` to an externally captured llama-server log when associating
server timing/context evidence with a run. The fork does not start, redirect, or
mutate the llama-server process or log.

Set `model.response_streaming` to `off`, `status`, or `draft`; the shipped local profile
uses `draft`, which prints provisional content to stderr and never adds it to the model
history. `model.llama_log_path` points at a separately redirected llama-server log. After
each response, the client reports the latest context, prompt-evaluation rate, generation
rate, total time, and graph reuse when those timing lines are present. These statistics
are operator output only and are never sent back to the model.

Context projection is opt-in: use `-c agent.context_mode=projected` and optionally
`-c agent.projected_context_max_chars=24000` for a deterministic temporary model view.
The full message trajectory remains the saved source of truth, and the shipped
profile keeps `context_mode: full` until measured validation supports changing it.

Native text tools execute inside the selected environment workspace. Local uses the
shared Python implementation directly; sandbox and remote adapters invoke the same
fixed helper inside their configured workspace and never resolve those paths on the
agent host.

## Benchmarks

* `benchmarks/swebench.yaml` - Config for the `run/benchmarks/swebench.py` entry point.
