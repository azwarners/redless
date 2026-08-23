# mini-swe-agent-slow

A fork of [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) optimized
for slow inference: model generations may take minutes, while filesystem work,
search, Git, builds, and tests are comparatively cheap.

It keeps mini-SWE-agent's deliberately small agent loop, independent shell commands,
YAML configuration, and LiteLLM model adapter. It changes the default operating style
so that one model turn should launch substantial deterministic work instead of many
tiny model round trips.

## Why this fork exists

For a large model running locally—such as Kimi Code or MiniMax-M3 through
llama.cpp—waiting for another generated token can cost far more than running a search
or test suite. This fork therefore encourages grouped inspection and validation work,
uses safer deterministic source edits, and keeps observations compact.

Current differences from upstream include:

- `slow_local.yaml`, a profile with a slow-inference prompt and a 10-minute default
  Bash-command timeout;
- native `read_text`, `replace_text`, and `create_text` tools alongside Bash;
- exact-match source replacement: zero or multiple matches leave the file unchanged;
- per-Bash-call timeout overrides;
- separately configured connection and model-read timeouts for LiteLLM;
- no automatic inference replay by default, including LiteLLM's OpenAI-compatible
  client retry setting;
- deterministic head/tail output shaping while retaining raw tool output in the
  saved trajectory; and
- model/tool call counts, elapsed times, and LiteLLM token metrics when reported.

This is an experimental fork under active validation with slow local models. Kimi
Code and MiniMax-M3 are current validation targets, not exclusive requirements.

## Requirements

- Linux or another supported Python environment;
- Python 3.10 or newer;
- Git;
- a model served through an OpenAI-compatible endpoint; and
- for the local example below, a running llama.cpp server exposing a Chat
  Completions-compatible endpoint such as `http://127.0.0.1:8080/v1`.

The slow-local profile uses native function tools. Use a model/server combination
that supports OpenAI-style tool calls for the best result.

## Installation

Install this fork from source in a virtual environment. The recommended executable
is `mini-slow`; the older `mini` and `mini-swe-agent` names remain compatibility
aliases.

```bash
git clone https://github.com/azwarners/mini-swe-agent-slow.git
cd mini-swe-agent-slow
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

Editable installation makes the `mini-slow` command use changes in this checkout,
which is useful while validating or developing the fork.

## Verify the installation

```bash
mini-slow --help
mini-extra --help
```

Normal startup identifies `mini-swe-agent-slow`, reports the fork and upstream
versions, and loads state from `~/.config/mini-swe-agent-slow/` by default. This
directory contains fork-owned credentials and the last trajectory; set
`MSWEA_GLOBAL_CONFIG_DIR` only when intentionally selecting another state directory.

Run the test suite after installing the development extra:

```bash
XDG_CONFIG_HOME="$(mktemp -d)" pytest -q
```

Some environment integrations need Docker, Singularity, Bubblewrap permissions, or
optional provider packages. If your machine cannot run the full suite, run the core
slow-inference coverage instead:

```bash
XDG_CONFIG_HOME="$(mktemp -d)" pytest -q \
  tests/tools tests/agents tests/config tests/environments/test_init.py \
  tests/environments/test_local.py tests/models/test_actions_toolcall.py \
  tests/models/test_content_string.py tests/models/test_retry_policy.py \
  tests/models/test_test_models.py tests/models/test_truncation_finish_reason.py \
  tests/utils tests/test_init.py tests/test_fire.py
```

## Using a local llama.cpp model

Start llama.cpp separately. This README assumes its OpenAI-compatible server is at
`http://127.0.0.1:8080/v1`; substitute your hostname, port, and server model name.

Create an overlay next to the repository you want the agent to work on:

```bash
cat > llama-local.yaml <<'EOF'
model:
  model_name: "your-server-model-name"
  model_kwargs:
    custom_llm_provider: "openai"
    api_base: "http://127.0.0.1:8080/v1"
    api_key: "llama.cpp-placeholder"
EOF
```

These are LiteLLM settings passed through the fork's `model_kwargs` configuration.
`api_key` is a placeholder for clients that require one even when the local server
does not authenticate. If your server requires authentication, replace it with its
actual key. `model_name` must be a name the server accepts.

The slow profile is the built-in `slow_local.yaml` configuration. It supplies:

- `environment.timeout: 600` for normal Bash commands;
- `model.connect_timeout_seconds: 30`;
- `model.model_timeout_seconds: 0`, meaning no agent-imposed model read deadline;
- `model.max_retries: 0`, meaning no automatic LiteLLM replay after the original
  request; and
- `model.tool_output`, a 6,000-character deterministic head/tail tool-observation
  budget while preserving raw output in the trajectory.

## First run

This creates a disposable repository and asks the agent for a small, observable edit.
Run it only after the llama.cpp server is accepting requests.

```bash
mkdir -p /tmp/mini-swe-agent-slow-smoke
cd /tmp/mini-swe-agent-slow-smoke
git init
printf 'def greeting():\n    return "hello"\n' > greeting.py
cat > test_greeting.py <<'EOF'
from greeting import greeting


def test_greeting():
    assert greeting() == "hello"
EOF
```

Copy `llama-local.yaml` into this directory (or use its absolute path), then run:

```bash
MSWEA_CONFIGURED=true mini-slow \
  -c slow_local.yaml \
  -c llama-local.yaml \
  -t 'Change greeting() to return "hello, slow world" and update the test. Run pytest, inspect git diff, then submit.' \
  -o ./slow-smoke.traj.json
```

`MSWEA_CONFIGURED=true` skips the CLI's first-time hosted-provider setup wizard for
this invocation; the two YAML files already provide the model settings. The slow
profile uses `DefaultAgent`, so it runs without interactive command confirmation.
The saved trajectory includes the model/tool metrics and raw tool output.

To run against an existing local repository, activate the virtual environment, change
into that repository, and use the same command with an appropriate task. Prefer a
clean working tree or a disposable branch: the agent can modify files and run
commands in its selected environment.

## Slow-inference behavior

The prompt asks the model to gather related evidence and batch safe, useful work per
turn. Tool calls are executed sequentially in their declared order; this reduces
model round trips without introducing concurrent command execution.

Use Bash for repository search, Git, builds, tests, and shell-native operations. The
native text tools are for deterministic text operations in the selected environment:

- `read_text(path, start_line, end_line)` reads a bounded UTF-8 range (at most 200
  lines and 12,000 characters).
- `replace_text(path, old_text, new_text)` changes a file only when `old_text` has
  exactly one match. Zero or multiple matches fail without modifying the file.
- `create_text(path, content)` creates a new UTF-8 file and fails if it already
  exists.

Text operations run inside the selected environment. In container or sandbox
environments, the helper executes in that environment's workspace rather than
resolving container paths on the agent host. If the environment lacks Python 3 for
the helper, the tool returns `text_tool_unavailable`; it does not fall back to host
filesystem access or `sed`.

## Configuration

`src/minisweagent/config/slow_local.yaml` is the source configuration shipped with
this fork. Once installed, use it by name with `-c slow_local.yaml`; configuration
lookup includes the package's built-in config directory.

Pass multiple `-c` arguments to merge settings. For example, the first-run command
merges the slow profile with `llama-local.yaml`. You can also use individual command
line overrides:

```bash
MSWEA_CONFIGURED=true mini-slow -c slow_local.yaml -c llama-local.yaml \
  -c environment.timeout=1200 \
  -c model.model_timeout_seconds=0 \
  -c model.connect_timeout_seconds=30 \
  -c model.max_retries=0 \
  -c model.tool_output.max_chars=6000 \
  -t 'Inspect this repository and describe its top-level architecture.'
```

Important slow-local settings:

| Setting | Purpose |
| --- | --- |
| `model.model_name` | Model name accepted by the server. |
| `model.model_kwargs.custom_llm_provider` | LiteLLM provider; use `openai` for the example llama.cpp endpoint. |
| `model.model_kwargs.api_base` | OpenAI-compatible server base, including `/v1`. |
| `model.model_kwargs.api_key` | Server key or a client-required placeholder. |
| `environment.timeout` | Default Bash-command timeout in seconds. |
| `model.connect_timeout_seconds` | TCP/TLS connection timeout only; it does not limit writes, pooling, prefill, or generation. |
| `model.model_timeout_seconds` | HTTP read timeout; `0` leaves prompt prefill and generation unbounded by this fork. |
| `model.max_retries` | Additional LiteLLM/OpenAI-compatible retries; the slow profile uses `0` to avoid replaying expensive inference. |
| `model.tool_output.max_chars` | Maximum shaped tool-observation size; it does not cap model content. The profile also sets head, tail, and error-tail budgets. |

## Testing

The commands in [Verify the installation](#verify-the-installation) test the local
implementation without calling a model. There is not yet a checked-in live llama.cpp
integration test; real-model validation is currently performed against a running
local inference server using the first-run workflow above.

## Current limitations / not yet implemented

The current tested core does not include:

- checkpoint/resume;
- compact live-context projection (the full linear trajectory remains the prompt
  history and saved source of truth);
- llama.cpp slot-aware routing or prompt-cache management;
- repository indexing/vector retrieval; or
- specialized DesignDiviner or ConjurePR behavior.

These remain validation-driven follow-on work, not promises of current behavior.

## Upstream and attribution

This project is a fork of [SWE-agent/mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent).
It retains the upstream project's small, understandable architecture while changing
the operating assumptions for slow local inference.

The repository is licensed under the [MIT License](LICENSE.md), including the
upstream copyright notice. This fork does not claim upstream benchmark results as
results for `mini-swe-agent-slow`.

For the underlying SWE-agent work, see the upstream repository and its referenced
[SWE-agent paper](https://arxiv.org/abs/2405.15793).
