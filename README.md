# mini-swe-agent-slow

`mini-swe-agent-slow` is a small coding agent for local AI models. It is designed
for machines where asking the model for another response is slow, but searching files,
running tests, and using Git are cheap.

It is a fork of [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent). The
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

Clone this fork and install it in a virtual environment. Use `mini-slow`, not a
separately installed upstream `mini` command.

```bash
git clone https://github.com/azwarners/mini-swe-agent-slow.git
cd mini-swe-agent-slow
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

Check that you are running this fork:

```bash
mini-slow --help
```

The first line should say `mini-swe-agent-slow`. By default, the fork keeps its own
settings and last-run file in `~/.config/mini-swe-agent-slow/`; it does not use the
upstream mini-SWE-agent directory.

## First local-model run

This is the shortest useful setup path. It creates one small settings file, then asks
the agent to inspect the repository you are currently in.

### 1. Go to the repository the agent should work on

Use a clean Git branch or a disposable copy for your first run.

```bash
cd /path/to/your/repository
git status
```

### 2. Create a local model settings file

This file is just your local server address and model name. It is not special, and it
does not need to be committed. Create it inside the target repository:

```bash
mkdir -p .mini-swe-agent-slow
cat > .mini-swe-agent-slow/llama-local.yaml <<'EOF'
model:
  model_name: "YOUR_MODEL_NAME"
  model_kwargs:
    custom_llm_provider: "openai"
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
printf '.mini-swe-agent-slow/\n' >> .git/info/exclude
```

### 3. Run the agent

Run this from the target repository. The first `-c` selects the fork's built-in
slow-local settings. The second `-c` selects the file you just created.

```bash
MSWEA_CONFIGURED=true mini-slow \
  -c slow_local.yaml \
  -c .mini-swe-agent-slow/llama-local.yaml \
  -t 'Inspect this repository and describe its top-level architecture.' \
  -o ./mini-slow-run.traj.json
```

`MSWEA_CONFIGURED=true` skips the optional first-time setup questions for hosted
providers; your local settings file already supplies what this run needs. The saved
`mini-slow-run.traj.json` file records the conversation, commands, and timings.

For a code change, give a direct task and ask for validation:

```bash
MSWEA_CONFIGURED=true mini-slow \
  -c slow_local.yaml \
  -c .mini-swe-agent-slow/llama-local.yaml \
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

Most users only need to edit `.mini-swe-agent-slow/llama-local.yaml`:

| If you need to change… | Change this setting |
| --- | --- |
| Model name | `model.model_name` |
| Server address | `model.model_kwargs.api_base` |
| Server key | `model.model_kwargs.api_key` |
| Default test/build timeout | `environment.timeout` |

`slow_local.yaml` is included with the fork. It is deliberately conservative for a
slow server: it allows a 30-second connection attempt, does not replay uncertain model
requests, and keeps command output compact so logs do not fill the next model prompt.
It does not shorten useful model-written reports or plans.

To change a setting for one run, add another `-c` argument:

```bash
MSWEA_CONFIGURED=true mini-slow \
  -c slow_local.yaml \
  -c .mini-swe-agent-slow/llama-local.yaml \
  -c environment.timeout=1200 \
  -t 'Run the relevant tests and report failures.'
```

## Test the installation

Install the development tools, then run the tests:

```bash
python -m pip install -e '.[dev]'
XDG_CONFIG_HOME="$(mktemp -d)" pytest -q
```

These tests do not contact your model server. There is no checked-in live llama.cpp
test yet; use the first local-model run above once your server is available.

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