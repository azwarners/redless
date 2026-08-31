# REDLESS

Reusable Extendable Digital Labor Execution Software Subsystem

REDLESS is a small coding agent for local AI models. It is designed
for machines where asking the model for another response is slow, but searching files,
running tests, and using Git are cheap.

REDLESS is a fork of [mini-SWE-agent](https://github.com/SWE-agent/mini-swe-agent). The
agent stays intentionally small: one model, one repository, and ordinary command-line
tools. It is being tested with local models served by llama.cpp, including Kimi Code
and MiniMax-M3.

## What you need

- Linux or another system with Python 3.10 or newer
- Git
- A local model server that offers an OpenAI-compatible `/v1` endpoint
- A repository you are happy for the agent to inspect, modify, and test

For the example below, llama.cpp is already running at
`http://127.0.0.1:8080/v1`.

## Install

Clone this fork and install it in a virtual environment. Use `redless`, not a
separately installed upstream `mini` command.

```bash
git clone https://github.com/azwarners/redless.git
cd redless
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

Check that you are running this fork:

```bash
redless --help
```

The first line should say `REDLESS`. By default, REDLESS keeps its own
settings and last-run file in `~/.config/redless/`; it does not use the
upstream mini-SWE-agent directory.

## First local-model run

This is the shortest useful setup path. It creates one small settings file, then asks
the agent to inspect the repository you are currently in.

### 1. Create a workspace

For an existing project, clone it and add your local-model settings with one command:

```bash
redless-workspace clone https://github.com/OWNER/REPOSITORY.git \
  ~/mini-workspaces/project \
  --model YOUR_MODEL_NAME \
  --api-base http://127.0.0.1:8080/v1
```

For a new task, create a safe empty workspace instead:

```bash
redless-workspace init ~/mini-workspaces/first-task \
  --model YOUR_MODEL_NAME \
  --api-base http://127.0.0.1:8080/v1
```

It creates a new Git workspace and its local llama.cpp settings file, and it refuses
to use an existing directory. Then enter it:

```bash
cd ~/mini-workspaces/first-task
```

### 2. Create or check local model settings

This file is just your local server address and model name. It is not special, and it
does not need to be committed. If you used `redless-workspace`, it already exists.
Otherwise, create it inside the target repository:

```bash
mkdir -p .redless
cat > .redless/llama-local.yaml <<'EOF'
model:
  model_name: "YOUR_MODEL_NAME"
  response_streaming: draft
  llama_log_path: ".redless/llama-server.log"
  model_kwargs:
    api_base: "http://127.0.0.1:8080/v1"
    api_key: "llama.cpp-placeholder"
EOF
```

Replace `YOUR_MODEL_NAME` with the model name accepted by your server. If you do not
know it, try:

```bash
curl http://127.0.0.1:8080/v1/models
```

Use the `id` shown in the response. The placeholder API key is normal for an
unauthenticated llama.cpp server. If your server requires a real key, put it in this
file and keep the file out of Git:

```bash
printf '.redless/\n' >> .git/info/exclude
```

### 3. Capture llama.cpp server output

Start `llama-server` from the target repository and tee its output into the
workspace directory. Replace the model path and server options as needed:

```bash
mkdir -p .redless
llama-server \
  --model /path/to/GLM-5.2-754B.gguf \
  --host 127.0.0.1 --port 8080 \
  2>&1 | tee -a .redless/llama-server.log
```

Keep that process running in its own terminal. REDLESS only reads the
log; it does not start, redirect, truncate, or modify the server process. The
`llama_log_path` setting above makes each completed response report context usage,
prompt-evaluation speed, generation speed, total time, and graph reuse when those
lines are present. The log may contain sensitive prompt/server details, so keep the
workspace directory excluded from Git.

### 4. Run the agent

Run this from the target repository. The first `-c` selects the fork's built-in
slow-local settings. The second `-c` selects the file you just created.

```bash
MSWEA_CONFIGURED=true redless \
  -c slow_local.yaml \
  -c .redless/llama-local.yaml \
  -t 'Inspect this repository and describe its top-level architecture.' \
  -o ./redless-run.traj.json
```

`MSWEA_CONFIGURED=true` skips the optional first-time setup questions for hosted
providers; your local settings file already supplies what this run needs. The saved
`redless-run.traj.json` file records the conversation, commands, and timings. The
terminal shows when the model is working, when an action runs, and the final answer.

For a code change, give a direct task and ask for validation:

```bash
MSWEA_CONFIGURED=true redless \
  -c slow_local.yaml \
  -c .redless/llama-local.yaml \
  -t 'Change greeting() to return "hello, slow world". Update its test, run pytest, and show the Git diff.'
```

After a changing task, inspect the result yourself:

```bash
git diff
git status
```

## What the agent can do

The agent can run Bash commands, search the repository, use Git, run builds and
tests, and work with text files. It uses a few safer file operations in addition to
Bash:

- `read_text` reads a small range of a text file.
- `replace_text` changes text only when the old text appears exactly once. If it
  finds zero or multiple matches, it leaves the file unchanged.
- `create_text` creates a new file and refuses to overwrite an existing one.

The normal command timeout is ten minutes. This protects against stuck tests and
builds. It does **not** limit a healthy, slow model response: local model prompt
processing and generation have no fork-imposed deadline by default.

## Useful settings

Most users only need to edit `.redless/llama-local.yaml`:

| If you need to change… | Change this setting |
| --- | --- |
| Model name | `model.model_name` |
| Server address | `model.model_kwargs.api_base` |
| Server key | `model.model_kwargs.api_key` |
| Stream tokens | `model.response_streaming` (`draft`, `status`, or `off`) |
| Model tool protocol | `model.tool_protocol` (`openai`) |
| llama.cpp log | `model.llama_log_path` |
| Default test/build timeout | `environment.timeout` |

`slow_local.yaml` is included with the fork. It is deliberately conservative for a
slow server: it allows a 30-second connection attempt, does not replay uncertain model
requests, and keeps command output compact so logs do not fill the next model prompt.
Each observation is capped at 6,000 characters and one turn is capped at 12,000
observation characters, with a 256-character minimum reservation per result. It does
not shorten useful model-written reports or plans.

The slow profile warns once after 8 model calls or 1,800 cumulative model seconds.
Warnings include the trajectory path and are informational only; they do not stop or
alter the run.

The shipped profile has no model-call limit. Set `agent.step_limit` to a positive
number when a run should have a ceiling. Action progress is printed as a single
timestamped line followed by a one-line description of the command or file operation.
Pending-request heartbeat lines are disabled; a slow request remains open without
periodic client-side claims about its state.

The shipped profile uses direct llama.cpp transport and provisional stderr token streaming.
When llama.cpp provides `reasoning_content`, `reasoning`, or `thinking` stream deltas,
they are displayed as timestamped `Model reasoning` output; they are not added as a
synthetic message to the conversation.

### Local model tool protocols

Tool syntax belongs to the model as well as the server. `openai` (the default) uses
llama.cpp's native OpenAI-compatible tool calls. llama.cpp reporting
`supports_tools: false` describes the active chat template's native support; it does
not necessarily mean that the underlying model cannot use tools.

A future model-specific protocol can be added as another small adapter at the
llama.cpp model boundary. The agent continues to receive the same normalized actions.
The shipped profile uses the lossless `agent.context_mode=full`. Projection can be
measured explicitly with `-c agent.context_mode=projected`; it changes only the
temporary model input and preserves the full trajectory and deterministic ledger.

While a model request is pending, `redless` prints an update every minute. This confirms
that the client request is still open; it does not claim the server is actively generating
tokens. Set `agent.progress_interval_seconds=0` in an additional `-c` option to disable it.

To change a setting for one run, add another `-c` argument:

```bash
MSWEA_CONFIGURED=true redless \
  -c slow_local.yaml \
  -c .redless/llama-local.yaml \
  -c environment.timeout=1200 \
  -t 'Run the relevant tests and report failures.'
```

## Test the installation

Install the development tools, then run the tests:

```bash
python -m pip install -e '.[dev]'
XDG_CONFIG_HOME=/tmp/redless-tests pytest -q
```

These tests do not contact your model server. There is no checked-in live llama.cpp
test yet; use the first local-model run above once your server is available.

## First CPU validation

The fork's first documented CPU-only Kimi K2.7 Code (UD-IQ4_XS) run completed a deterministic
single-file Hangman task on an HPE ProLiant DL380 Gen9. It used seven model calls and
about 75 minutes of model time while deterministic tool work took under one second.
This is an early development result, not a benchmark comparison. See the sanitized
[run record](docs/testing_output/first-kimi-k2.7-code-run.md) for the task, outcome,
and llama.cpp timing/cache observations.

## Current limits

This is an experimental local-model fork. It does not yet provide checkpoint/resume,
repository indexing, prompt-cache or llama.cpp slot management, or application-specific
features for DesignDiviner and ConjurePR.

For container or sandbox use, select a supported environment configuration. The text
tools operate inside that selected environment; they do not require exposing the
container workspace to the host.

## For operators and contributors

The detailed slow-inference design, timeout policy, and future work live in
[the redesign blueprint](docs/slow-inference-redesign-blueprint.md). That document is
for implementation and architecture decisions; this README is the day-to-day setup
guide.

## Upstream and license

This fork retains mini-SWE-agent's small, understandable architecture. It is licensed
under the [MIT License](LICENSE.md), including the upstream copyright notice. It does
not claim upstream benchmark results as results for this fork.

For the original project, see [SWE-agent/mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent)
and the [SWE-agent paper](https://arxiv.org/abs/2405.15793).

The idea of adding text editing to mini-swe-agent to reduce the potential for error came from mini-swe-agent-plus https://github.com/Kwai-Klear/mini-swe-agent-plus .
