# Configs

* `mini.yaml` - Compatibility-default config for the interactive agent.
* `default.yaml` - Default config for the `default.py` agent.
* `slow_local.yaml` - Recommended fork profile for `mini-slow`: tool-call mode,
  10-minute Bash timeout, separate connection/read timeouts, no automatic
  LiteLLM replay, and deterministic tool-output shaping.

`slow_local.yaml` is designed for local OpenAI-compatible servers such as
llama.cpp. Set `model_name` and the endpoint in a small overlay, then run
`mini-slow -c slow_local.yaml -c llama-local.yaml`. Its `max_retries: 0` default
avoids replaying an expensive or ambiguous generation, while `tool_output` limits
only live tool observations; raw output remains available in the trajectory.

Native text tools execute inside the selected environment workspace. Local uses the
shared Python implementation directly; sandbox and remote adapters invoke the same
fixed helper inside their configured workspace and never resolve those paths on the
agent host.

## Benchmarks

* `benchmarks/swebench.yaml` - Config for the `run/benchmarks/swebench.py` entry point.
