# Slow-inference redesign — Phase 0 baseline

Recorded 2026-08-22 from the current checkout, before runtime changes for the
redesign.

## Test baseline

The repository-local environment was created with `uv` in `.venv`; no system
packages were installed. The project requires a writable configuration directory,
so the commands below use a temporary `XDG_CONFIG_HOME`.

The dependency-light baseline passed:

```text
314 passed, 10 skipped in 19.72s
```

Command:

```bash
XDG_CONFIG_HOME=/tmp/mini-swe-agent-config .venv/bin/pytest -q \
  tests/agents tests/config tests/environments/test_init.py \
  tests/environments/test_local.py tests/models/test_actions_toolcall.py \
  tests/models/test_content_string.py tests/models/test_test_models.py \
  tests/models/test_truncation_finish_reason.py tests/utils \
  tests/test_init.py tests/test_fire.py
```

The full collection baseline is not actionable in this execution environment:

- collection stops on missing/incomplete optional dependencies (`contree_sdk`,
  `pyarrow`, `botocore`, `portkey-ai`) and a LiteLLM API mismatch;
- after excluding those collection blockers, 464 tests passed, 59 were skipped,
  and 49 failed;
- the 12 bubblewrap failures are environmental: the kernel denies unprivileged
  user namespaces, so `bwrap` cannot start commands;
- the remaining failures are dependency/CLI-environment failures, not changes
  made by Phase 0.

The full suite should be repeated in a provisioned development environment before
Phase 1 changes are evaluated.

## Representative trajectories

These are the current deterministic trajectories exercised by
`tests/agents/test_default.py` and the tool-call parser tests. They describe the
baseline control flow without introducing a new fixture or runtime hook.

| Scenario | Model calls | Tool calls | Observation shape | Outcome |
| --- | ---: | ---: | --- | --- |
| Successful completion | 2 | 2 | one Bash observation per turn | `Submitted` |
| Several small steps | 4 | 4 | one Bash observation per turn | `Submitted` |
| Timeout and recovery | 2 | 2 | first observation contains partial output and timeout text | `Submitted` |
| Empty action turn | 2 | 1 | first turn has no action, next turn observes Bash | `Submitted` |
| Native multiple tool calls | 1 | 3 | observations preserve declared order and tool IDs | parser/formatter pass |
| Repeated format errors | 2 | 0 | `FormatError` messages are appended to history | `RepeatedFormatError` |

The current trajectory representation is the complete append-only `messages`
list. Existing deterministic metadata includes model-call cost and timestamps;
there is no separate model-vs-tool duration, prompt-size, retry, or truncation
telemetry yet. No telemetry was added in Phase 0 because the available baseline
does not provide a clean runtime-validation signal.

## Phase 0 conclusion

Baseline capture is complete. Phase 1 can be evaluated against the dependency-light
pass count, the full-suite environmental blockers above, and the trajectory
properties in this document. No production behavior was changed.
