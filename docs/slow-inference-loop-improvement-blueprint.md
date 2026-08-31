# Slow-inference loop improvement blueprint

Status: proposed after the Kimi K2.7 Code repository-review validation run. This is an
implementation blueprint, not a claim that the later phases are already present.

## Why this work is needed

The successful small Kimi task proved that very long healthy requests work: the agent
waited, performed tools, and printed a normal final response. The review task exposed a
different cost failure. It made at least 15 model calls, repeatedly used `read_text`, and
grew the server context from about 900 to about 34,000 tokens. The final observed request
spent about 90 minutes on prompt evaluation and about 8 minutes decoding. llama.cpp began
each next request immediately after the preceding tool work, so the long silent periods
were model/server time, not an unexplained delay in the agent process.

The loop must therefore make continued work visible immediately, cap cheap observations
as a group rather than only one at a time, and later offer a measured deterministic prompt
view. It must preserve the small sequential agent loop and the full trajectory.

## Decisions and non-goals

- Tool calls in one response remain **sequential**. This work reduces model round trips;
  it does not introduce concurrent execution.
- The full `self.messages` trajectory remains the audit record and is saved after each
  completed loop iteration. No projection overwrites or summarizes it in place.
- No planner framework, graph executor, persistent shell, vector database, repository map,
  AST/fuzzy editing, Git staging/commit behavior, or new retry subsystem is introduced.
- A normal task is not forcibly stopped merely because it uses many calls. Warnings and
  measurements come before automatic limits.
- Streaming must not alter tool-call parsing or replay an inference request. It is an
  opt-in experiment after status output is reliable.

## Phase A — immediate liveness output

Status: implemented in this change.

### Exact behavior

Add `agent.progress_interval_seconds: float = 60.0` to `AgentConfig`. In
`slow_local.yaml`, set it to `60`; zero disables recurring reports. It only has an effect
when `agent.show_progress` is true.

In `DefaultAgent.query()`:

1. Increment `n_calls` and print the existing `Waiting for model response (call N)…` line.
2. Start a daemon thread with a `threading.Event` before `model.query(self.messages)`.
3. The thread waits for the configured interval. Until the event is set, it prints:
   `[mini-swe-agent-slow] Model request still pending (call N; 60s elapsed).`
4. In `finally`, set the event, join the thread, then account for elapsed model time.
5. Print the usual received-response duration only when `model.query()` returned.

The line means the Python client call has not returned. It deliberately does **not** claim
that llama.cpp is decoding, that a connection is healthy, or that progress is being made.
It gives the operator a reliable answer to the practical question: “is mini-slow still
waiting on this same request?”

Replace the ambiguous duplicate `Task complete.` progress line with `Final response
received; printing result.` The CLI's normal final result remains unchanged.

### Tests

- Use a deterministic test model that sleeps longer than a tiny reporting interval.
- Assert that the initial wait, a pending-request line, and the normal final completion
  are present.
- Assert no extra `model.query()` call is made.
- Assert `progress_interval_seconds: 0` produces no recurring line.

## Phase B — prevent observation-driven context growth

Status: completed 2026-08-24. The aggregate budget is applied after per-observation
shaping and against rendered observation content, while every tool result remains in
the API message sequence and complete raw output remains in trajectory metadata.

Priority: critical next implementation. Complexity: medium. Expected benefit: very high
on CPU inference.

The present `model.tool_output.max_chars` shapes every individual tool observation to
6,000 characters. Four independently successful `read_text` calls can consequently add
up to 24,000 characters in one turn. That is exactly the failure seen in the review run.

### Configuration

Keep the existing, clear `model.tool_output` mapping and add only these two explicit
keys:

```yaml
model:
  tool_output:
    max_chars: 6000                 # existing per-observation cap
    max_turn_chars: 12000           # cap across all tool observations for one model turn
    minimum_chars_per_observation: 256
```

`max_turn_chars: 0` means no aggregate cap. `minimum_chars_per_observation` must be at
least 64; config validation rejects lower values and rejects a positive turn budget too
small to give every returned tool call its minimum. Keep the existing head/tail/error-tail
keys and raw trajectory output semantics.

### Implementation algorithm

Implement the aggregation at the existing tool-observation formatting boundary, after
each raw environment result is captured and before API messages are built.

1. Shape each output with the existing per-observation policy first.
2. Make one response message for **every** tool call; never omit a tool-call ID, because
   OpenAI-compatible tool protocols require a matching result.
3. Reserve `minimum_chars_per_observation` for every rendered result. The reservation
   contains the exit status, action number/tool name, and a deterministic omitted-output
   notice when necessary.
4. Allocate remaining characters to failures first, ordered by action order. Then allocate
   remaining characters to successful results in action order. Use the existing head/tail
   formatter for every reduced body. Do not make a model call to choose excerpts.
5. Emit deterministic metadata in every shortened observation: original character count,
   displayed character count, per-observation truncation, and turn budget. Continue storing
   the complete raw result in trajectory `extra` exactly as today.
6. Count the rendered API observation characters and assert the positive budget is not
   exceeded except for fixed protocol labels. Document those labels as excluded from the
   budget if that is needed for a strict implementation.

### Required tests

- Four successful outputs exceed the aggregate budget but all tool IDs receive a valid,
  nonempty reply.
- A long compiler/test failure retains its error tail ahead of a long successful listing.
- One output retains current per-output behavior when the turn total is ample.
- A zero total leaves current behavior unchanged.
- Raw trajectory fields retain unshaped output.
- The model's own final text is never passed through this formatter or capped by these
  settings.

## Phase C — analysis-task operating rules and warnings

Status: completed 2026-08-24. The slow profile now includes the targeted analysis
guidance and one-shot informational thresholds for model calls and cumulative model
time; warnings include the trajectory path and do not alter execution.

Priority: useful soon. Complexity: low. Expected benefit: high.

### Prompt changes

Append the following compact block to `slow_local.yaml`'s system prompt, replacing no
other safety instruction:

> For review or analysis tasks, search once for candidate files, then read only the
> sections needed to support a conclusion. Do not scan files page-by-page or reread a
> path unless a later edit or new evidence makes it necessary. Collect enough targeted
> evidence to write the requested report; do not keep gathering evidence merely because
> the repository is large.

Do not say “batch everything.” Actions may be combined only when their arguments are
already known. The existing rules for code changes, analysis-only validation, Git safety,
and bounded text tools remain.

### Operator warning

Add two optional agent settings, disabled by default and enabled in `slow_local.yaml`:

```yaml
agent:
  call_warning_threshold: 8
  model_time_warning_seconds: 1800
```

After a response is received, print one warning (once per run) when either threshold is
crossed: call count, cumulative model time, and the trajectory path. It must say this is
a warning, not an error, and must not change the next action, model prompt, retry policy,
or exit status. This gives an operator a clear point to inspect the saved trajectory or
interrupt deliberately.

Tests cover one warning only, no warning when both thresholds are zero, and unchanged
call count/result.

## Phase D — deterministic live-context projection, opt-in

Status: completed 2026-08-24. `full` remains the default and the shipped slow profile
is explicitly lossless; `projected` supplies a deterministic temporary model view with
a serialized action/result ledger while preserving the complete trajectory.

Priority: useful soon only after Phase B is measured. Complexity: medium. Expected
benefit: potentially very high; risk: loss of useful evidence and reduced llama.cpp
prefix-cache reuse.

Do not enable this in the shipped slow profile initially. First benchmark the aggregate
observation cap and prompt rule changes with the same Kimi review task.

### Configuration and state

Add `agent.context_mode: full | projected`, default `full`. Add
`agent.projected_context_max_chars: 24000`, validated at 4,000 or higher. `full` retains
today's exact behavior. `projected` changes only the list supplied to `model.query()`;
the full messages list and trajectory remain unchanged.

Maintain a small deterministic ledger alongside messages:

- successful text reads: path and requested line range;
- successful text changes/creates: path and action kind;
- Bash commands: exact command, exit code, and final one-line status;
- failures/timeouts: tool, exit/exception type, and shaped error tail.

It contains no model-generated summary and no inferred architecture facts. Update it from
tool action/results only. Serialize it under trajectory `info.live_context_ledger`.

### Projection algorithm

1. Keep the original system message and original task message verbatim.
2. Add one user-formatted `Deterministic working ledger` message from the fields above.
3. Append the newest complete assistant/tool cycles that fit the configured character
   budget, preserving whole assistant-to-tool-message groups. Never leave a tool result
   without the assistant tool call that caused it.
4. If the newest cycle alone exceeds budget, retain its assistant message and aggregate-
   shaped tool responses, with the explicit omission metadata from Phase B.
5. Do not inject projection markers into `self.messages`; construct a temporary list for
   `model.query()`.

Tests must prove API ordering validity, full trajectory preservation, deterministic output
for identical inputs, retention of an earlier failed command in the ledger, and no effect
when `context_mode: full`.

## Phase E — streaming model output experiment

Status: completed 2026-08-24 for the direct llama.cpp transport, with focused parser,
stream reconstruction, and model-factory tests. LiteLLM remains non-streaming by design.

Priority: future experiment. Complexity: medium–high. Expected benefit: operator trust;
generation time itself is unchanged.

Heartbeat output is the default reliable answer. Live token streaming is not a small
`print()` change: streaming OpenAI tool calls arrive as fragmented function name/argument
deltas, and an apparent prose answer may be followed by a tool call.

### Scope and defaults

Add `model.response_streaming: off | status | draft`, default `off` for general models.
`status` is reserved for provider transport events. The shipped `slow_local.yaml` explicitly
selects the direct llama.cpp transport and enables `draft` after focused local stream
reconstruction tests; other model classes remain non-streaming.

For `draft`, print received content deltas to stderr, prefixed once with
`[mini-swe-agent-slow] Model draft:`. Label it as provisional. If a tool call later
arrives, print `Model requested tools; draft text is not the final result.` Keep the
normal final result printed once at task completion. Never store a second synthetic draft
message in history.

### Model implementation

The fork's `LlamaCppModel` gains the stream consumption path. It calls llama.cpp's
OpenAI-compatible endpoint directly; LiteLLM is not involved. Call the endpoint with its explicit
streaming option, consume all chunks, and reconstruct one response equivalent to the
current non-stream response:

- concatenate textual deltas in choice order;
- merge tool calls by provider-supplied index/ID;
- concatenate function argument fragments exactly, then parse only after the terminal
  chunk;
- retain usage/cost and raw provider data where LiteLLM provides them;
- use the existing action validation and response formatter after reconstruction.

No retries are added. A stream disconnect after submission is an ambiguous inference
failure and stops once, exactly like the current non-stream request.

### Gate before enabling

Use recorded or locally crafted chunk sequences to prove streamed and non-streamed text,
one tool call, several tool calls, and malformed arguments produce equivalent internal
messages. Then run a tiny live llama.cpp task for each target model. Verify server-side
request count is one, tool arguments are complete, final text is not duplicated, and a
slow silent prefill retains the Phase A heartbeat. If tool streaming differs by model,
leave `draft` disabled for that server instead of adding provider-specific heuristics.

## Phase F — validation protocol

Run the same bounded repository-review task before and after each enabled phase. Record:

| Metric | Source |
| --- | --- |
| model calls and cumulative model time | trajectory `info.model_stats` |
| tool calls, `read_text` count, and raw/shaped observation characters | trajectory metadata |
| prompt token count, prompt-eval time, decode token rate, LCP/cache retention | llama.cpp server log |
| gap from tool completion to the next server request | CLI timestamps plus server log |
| correctness/completeness of final report | human review against task rubric |

Advance from Phase B to D only if the aggregate cap reduces prompt cost without hiding
needed failures. Adopt projection only if it materially lowers prompt-evaluation time or
stops context growth without reducing completion quality or materially damaging cache
reuse. Keep Kimi Code, MiniMax-M3, and llama.cpp tool-stream behavior as explicit live
validation questions.

## Files expected to change

| Phase | Files |
| --- | --- |
| A | `agents/default.py`, `config/slow_local.yaml`, agent tests, README/config README |
| B | `models/litellm_model.py` and response formatter if they own observation rendering, output tests, slow config/docs |
| C | `slow_local.yaml`, `agents/default.py`, agent tests |
| D | `agents/default.py`, trajectory serialization tests, slow config/docs |
| E | `models/litellm_model.py`, model parsing tests, optional live integration documentation |

Each phase must run focused pytest tests, Ruff, and `git diff --check`; phases B, D, and
E additionally require the validation protocol above before their defaults change.
