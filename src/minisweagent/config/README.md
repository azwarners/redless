# Configs

* `mini.yaml` - Default config for `mini`/`agents/interactive.py` agent.
* `default.yaml` - Default config for the `default.py` agent.
* `slow_local.yaml` - Slow local inference profile using the tool-call agent and a 10-minute command timeout.

Native text tools execute inside the selected environment workspace. Local uses the
shared Python implementation directly; sandbox and remote adapters invoke the same
fixed helper inside their configured workspace and never resolve those paths on the
agent host.

## Benchmarks

* `benchmarks/swebench.yaml` - Config for the `run/benchmarks/swebench.py` entry point.
