# Slow-inference foundation redesign blueprint

Status: proposal for review. This document deliberately makes no runtime changes.

## Decision summary

The fork should retain mini-SWE-agent's small linear control flow, independent shell
commands, YAML configuration, and interchangeable model/environment classes. The
main change in orientation is to treat a model generation as the scarce resource and
normal computation as cheap. The desired rhythm is one well-informed model turn that
launches several related deterministic actions, followed by one compact observation.

Do not create a planner service, a shell session, a retrieval database, an AST editing
framework, or a second agent loop. Add a small tool registry with `bash` and a
failure-safe `text` sibling. Consider an optional deterministic prompt view and a
small, atomically persisted run state only after real-model validation supports them.

## Current architecture relevant to slow inference

| Area | Current behavior | Slow-inference consequence |
| --- | --- | --- |
| Agent loop | `DefaultAgent.step()` always does one model query, executes all returned actions serially, appends observations, then queries again. | Multiple tool calls already amortize one generation, but the default prompt teaches a one-action, step-by-step cadence. |
| History | `self.messages` is both the complete trajectory and the exact next-model input. | Simple and debuggable, but all prior assistant content and observations are resent forever. |
| Tools | Tool-call models expose only `bash`; text models parse exactly one fenced Bash action. Parsed tool calls may contain several Bash calls. | Safe arbitrary computation is available, but source edits are commonly fragile shell quoting or `sed` replacements. Text-mode configuration blocks batching. |
| Environment | Local/Docker/Singularity/Bubblewrap/SWE-ReX environments independently run each command; most default to a 30-second command timeout. | Independent commands are a deliberate stability choice, but normal builds/tests are killed too early and timeout override is not exposed in the Bash schema. |
| Model I/O | `LitellmModel` calls `litellm.completion(..., tools=[BASH_TOOL])`; its response counterpart uses the Responses API. Tenacity retries non-abort exceptions up to ten times with exponential delay. | No explicit request/read/connect timeout policy is owned by this project. A transient-looking client timeout can repeat an expensive, potentially still-running inference. |
| Prompts | `mini.yaml` asks for at least one Bash tool call, permits multiple calls, but repeatedly describes think-command-observe-next-command. `default.yaml` and `mini_textbased.yaml` require exactly one Bash action and include lengthy `sed` tutorials. | This maximizes small turns and prompt tokens—opposite of the target operating model. |
| Output | YAML Jinja templates include output below 10,000 characters; above it, retain 5,000 characters each from head/tail. Raw output is retained in message `extra` and serialized. | The model gets a large fixed slice even when only errors matter; prompt history continues to carry every already-seen output. |
| Limits and saving | Step/cost/wall limits are checked immediately before the next query. The trajectory is rewritten after every loop iteration. `run()` starts with a fresh message list. | Saving is helpful, but not atomic and not resumable; agent wall time can terminate a healthy long inference at the next boundary. |

The fork is close to upstream at the inspected `DefaultAgent` and LiteLLM boundaries.
The linear history, Bash-only interface, and independent command execution are
intentional upstream design choices, not accidental omissions. This fork should make
only the narrow deviations necessary for slow local inference. The initial core must
be validated on real slow models before adding checkpoint/resume or live-context
projection machinery.

## Problems discovered

1. **Critical: default behavior trains tiny turns.** The instructions say to work
   step-by-step; the text modes require one action; and examples model one `ls` or
   `sed` command. A slow model pays a full generation for each such micro-decision.
2. **Critical: ordinary work has a 30-second command deadline.** Local, Docker,
   Singularity, Bubblewrap, and SWE-ReX Docker defaults are 30 seconds. The tool
   schema accepts no requested timeout, even though environment methods do.
3. **Critical: unsafe/fragile editing is prompt-endorsed.** `sed` examples can match
   the wrong occurrence, escape incorrectly, or make a partial change. A repair turn
   after a broken edit is exceptionally expensive.
4. **High: the linear transcript grows without a prompt budget.** It is excellent as
   a durable audit trail but unnecessarily expensive as the live context; `extra` is
   stripped for the API, but all rendered observations remain.
5. **High: output trimming is blunt.** A successful long test output consumes up to
   10,000 characters; a long failure can lose its decisive middle. There is no
   standard machine-readable truncation record or targeted read operation.
6. **High: retry semantics can duplicate expensive work.** The generic ten-attempt
   retry does not distinguish pre-request connection failure from an ambiguous
   post-request/read timeout. LiteLLM/default HTTP timeout behavior is implicit.
7. **High: progress is durable only as a transcript.** A crash after an edit or a
   tool result can leave a useful trajectory, but there is no supported continue path,
   stable state summary, atomic checkpoint, or record of an in-flight request.
8. **Medium: wall-clock protection is too coarse.** It is checked only at query
   boundaries and includes model generation. The default is unbounded, but benchmark
   configurations set finite wall limits without distinguishing model time.
9. **Medium: compatibility must be handled deliberately.** Tool-call mode can expose
   multiple tools and multiple calls today; text mode cannot without a new compact
   grammar. llama.cpp OpenAI-compatible servers vary in tool-call support.
10. **Medium: benchmark mechanics can discard work.** SWE-bench removes an existing
    trajectory before a rerun and treats a run as all-or-nothing rather than offering
    an explicit resume policy.

## Ranked proposal

| Priority | Change | Expected benefit for very slow inference | Complexity |
| --- | --- | --- | --- |
| Critical for v1 | Replace the default prompt cadence; allow grouped independent tool calls | Very high | Low |
| Critical for v1 | Add exact-match `text.replace_text` plus targeted `read_text`/safe `create_text` | Very high | Low–medium |
| Critical for v1 | Explicit model timeout/retry policy and practical command timeouts with per-call override | Very high | Medium |
| Critical for v1 | Predictable output budget and compact observations | High | Low–medium |
| Critical for v1 | Timing/call instrumentation and a representative validation corpus | High for decisions | Low–medium |
| Useful soon | Stable structured working state, atomic checkpoints, explicit resume | High when interruptions are frequent | Medium |
| Useful soon | Optional deterministic live-context projection while retaining full trajectory | High for long tasks | Medium |
| Useful soon | Text-mode multi-tool grammar, only if local models need it | Medium–high | Medium |
| Future experiment | Server-specific prompt-cache handling and adaptive context budgets | Medium | Medium |
| Future experiment | Deterministic repository map/index | Variable | Medium |

## Proposed v1 shape

Keep `DefaultAgent` as the sole loop. Generalize an action from “a Bash command” to
`{"tool": "bash" | "text", ...}` and preserve the existing `Environment.execute()`
path for Bash. Make text operations a small **environment-owned capability**:
`DefaultAgent` dispatches an action to `Environment.execute_text(action)`, and that
environment performs the operation in its own workspace filesystem. Tool parsing and
observation formatting become tool-agnostic but remain in their present
model-specific files. No plugin manager or generic workflow engine is required.

This boundary is essential. A host-side editor must never resolve or write a container
workspace path such as `/testbed`. The same exact text tool must work inside a
responsibly isolated Docker, Singularity, Bubblewrap, or SWE-ReX environment, without
requiring that environment to expose its workspace to the agent host.

One assistant response may issue several independent tool calls. Execute them in the
model's declared order, not concurrently: ordering is deterministic and permits an
inspect-then-edit sequence if the model intentionally uses it. The prompt should
prefer a single Bash call containing related shell work when shell state is not
required, and multiple native tool calls when their semantics are clearer. Do not
infer parallel safety.

Suggested files (introduced in the phase that needs them):

- `src/minisweagent/agents/default.py`: small dispatcher to `execute()` or
  `execute_text()`; later, narrowly scoped state/checkpoint hooks if validation
  justifies them.
- `src/minisweagent/tools/text.py` (new): shared exact text semantics, the bounded
  result format, and a small helper payload for environments that must execute it
  inside a sandbox.
- `src/minisweagent/models/utils/actions_toolcall.py` and
  `actions_toolcall_response.py`: schemas and strict action parsing for `bash` and
  `text`.
- `src/minisweagent/models/litellm_model.py`, `litellm_response_model.py`, and
  `utils/retry.py`: request policy, idempotency/ambiguity-aware retries, telemetry.
- `src/minisweagent/environments/*.py`: centralized timeout convention; accept the
  parsed Bash action timeout; implement `execute_text()` in the environment's own
  filesystem context.
- `src/minisweagent/config/slow_local.yaml` (new) and `mini.yaml`: concise slow-mode
  defaults. Leave benchmark configs explicit rather than silently changing them.
- `src/minisweagent/utils/{output,state,context}.py` (at most three small modules;
  merge them if implementation stays short): output shaping first; checkpoint format
  and optional context projection only in later phases.
- `src/minisweagent/run/mini.py`: a slow-local config/documented path; add `--resume`
  only if Phase 3 validation supports Phase 4.

## Text tool design

### v1 API

Expose three functions to tool-capable models:

```json
replace_text({"path": "relative/or/absolute", "old_text": "...", "new_text": "..."})
read_text({"path": "...", "start_line": 1, "end_line": 80})
create_text({"path": "...", "content": "..."})
```

`replace_text` is required. `read_text` belongs in v1 because it gives line-bounded,
consistent source viewing without fragile shell pipelines. `create_text` belongs in
v1 because creation is common and can be made as safe and explicit as replacement.
It always fails if the target already exists. Existing-file modification goes through
`replace_text`; do not add an overwrite flag or an unrestricted write operation. The
three operations are intentionally orthogonal: read existing text, safely replace a
unique existing fragment, or create a new file.

### Exact replacement semantics

1. Resolve `path` beneath the **selected environment's** working directory (reject
   paths escaping it; reject absolute paths in v1).
2. Read bytes, reject undecodable UTF-8 text instead of guessing, and count exact
   non-overlapping occurrences of `old_text`.
3. Zero matches: return `ok: false`, `reason: "not_found"`, `matches: 0`; do not
   write.
4. More than one match: return `ok: false`, `reason: "ambiguous"`, `matches: N`; do
   not write.
5. Exactly one: replace that occurrence and atomically write the new content in the
   same directory. Preserve all bytes outside the replaced string, including newline
   style and final newline. Return `ok: true`, path, and a bounded changed-region
   excerpt with line numbers (for example, 3 lines before and after, capped by a
   small character budget).
6. An empty `old_text` is invalid. Reject a directory, inaccessible file, and decode
   error explicitly without modification.

The successful excerpt avoids a verification reread; the model should still use Bash
or tests to validate behavioral changes. Results must have the same normalized output
envelope as Bash (`ok`, `summary`, structured fields) so output policy applies once.

### Environment execution contract

`execute_text(action)` is part of the Phase 1 environment contract, not a host-only
utility. It receives the validated `read_text`, `replace_text`, or `create_text`
action and returns the normalized result envelope. It must execute where the target
workspace exists:

- `LocalEnvironment` may call the shared Python implementation directly, rooted at
  its configured `cwd`.
- Docker, Singularity, Bubblewrap, SWE-ReX, Contree, and Modal adapters must invoke
  the same deterministic helper inside their own execution context and configured
  `cwd`. The helper receives a serialized action as data (for example base64 JSON),
  never interpolated source text into a shell program. It performs UTF-8 byte reads,
  exact occurrence counting, and atomic write/create there, then emits one JSON
  result. It must not use `sed`, regex replacement, fuzzy matching, or a host path.
- The helper may require an explicitly documented in-environment runtime (initially
  Python 3's standard library is the pragmatic choice). If unavailable, return a
  concise structured `text_tool_unavailable` observation; do not fall back to a
  host-side write or silently use `sed`.

Keep the shared semantic logic in one small module and use a tiny runner/serialization
adapter rather than maintaining divergent Docker and local editing algorithms. The
agent must not branch on `isinstance(LocalEnvironment)`, and tool descriptions must
not claim that text tools are local-only. Environment adapters may differ only in how
they deliver the fixed helper and action data into their own sandbox.

`read_text` must reject requests above 200 lines and results above 12,000 characters
in v1. The helper should stop reading once it can determine that a requested limit is
exceeded; it should not read an entire huge file merely to reject the request.

Text-only models should initially continue using Bash. Do not invent a multi-block
markdown language in v1. If Kimi/MiniMax tool calling proves unreliable, add a small
JSON action-list text protocol later, with strict parsing and identical tool semantics.

## Context and output management

### Preserve two representations

Keep the existing complete, append-only trajectory as the audit/debug record. Add a
separate live prompt view; never overwrite or summarize the source trajectory.

The proposed working state for the later resume/context phases is a dataclass/JSON
object, updated entirely by deterministic code except for an optional `plan` field
supplied by the model:

```text
task; plan; files_examined; files_changed; tests_run; test_results;
important_facts; unresolved_problems; last_failure; model_calls; tool_calls
```

Cap every list/string deterministically. Record paths and concise command/result
summaries, not full source or logs. Update it from tool arguments and normalized
results; do not ask the model to summarize merely to compact context. The prompt
includes it in a stable location after the immutable system/task prefix.

### Prompt projection policy

Start conservatively:

- System prompt and task remain byte-stable across turns where possible.
- Include the structured state plus only the latest assistant action and its compact
  observations. Retain the full recent transcript only while under a configurable
  character/token budget.
- When over budget, retain the original system/task, state, and a bounded recent
  tail. Add an explicit marker saying older transcript is available in the trajectory
  but omitted from this request.
- Never silently remove tool-call/result pairs required by a provider's protocol.
  Use provider-specific preparation only when it preserves valid history; otherwise
  fall back to the current full linear history and warn in telemetry.

This is a later opt-in phase (`context_mode: linear | compact`, default `linear` for
compatibility). `slow_local.yaml` remains linear initially. Phase 3 must measure
actual prompt evaluation, cache reuse, repeated-token cost, and behavior before this
is implemented or considered for a default.

### Output policy

Replace duplicated YAML truncation templates with a small formatter configured by:

```yaml
output:
  max_chars: 6000
  head_chars: 1200
  tail_chars: 3600
  error_tail_chars: 4800
```

Use a clear envelope: return code, timeout/duration, exception, `truncated`, original
character count, and retained head/tail. On nonzero return or exception, prioritize
the tail; on success preserve a small head plus tail. The formatter must never drop
exception information. Preserve raw output only in the persisted trajectory, not in
the live prompt projection. `read_text` supplies a targeted alternative to a large
file dump. Later, a small deterministic test-log extractor may identify failed test
names and traceback tails, but v1 should not guess at arbitrary log formats.

## Prompt changes

Make a short stable system instruction, then task-specific text. Suggested rules:

1. A model generation is expensive. Before acting, gather enough related evidence to
   make a meaningful plan.
2. In one turn, batch safe independent searches, inspections, status checks, and
   validations. Use one Bash command with clear separators for related shell work.
3. Do not reread unchanged files or rerun a test whose result is already known.
4. Use `text.replace_text` for source edits when an exact unique old fragment is
   known. On an ambiguous/no-match result, inspect before trying again.
5. Use Bash for search, Git, builds, tests, and shell-native operations. Let a test
   run to its requested timeout when it is useful; use targeted output afterwards.
6. After edits, run deterministic verification before requesting more reasoning.
7. Keep narrative brief; make each turn accomplish substantial work. Finish only
   after a final status/diff/test check appropriate to the task.

Remove the lengthy `sed` and heredoc tutorials from the general prompt. Keep concise
tool descriptions in schemas. Replace “exactly one action” in text configurations
only when their parser is upgraded; otherwise keep it and document that tool-call
mode is the supported slow-local path.

## Timeout audit and proposed policy

| Protection | Found behavior | v1 policy |
| --- | --- | --- |
| Model connection/request/read | No project-owned setting; delegated to LiteLLM/client/model kwargs. | Add named `connect_timeout_seconds` (default 30) and `model_timeout_seconds` (default 0/unbounded) configuration, mapped to the provider-supported LiteLLM timeout mechanism after integration tests. A value of 0 means no agent-imposed generation/read deadline. |
| Model retry | Tenacity retries up to 10, waits 4–60 seconds, broadly retries non-abort exceptions. | Default to one attempt for ambiguous request/read failures. Retry a small configurable number only for classified pre-request/connect/rate-limit failures, with logged reason. Never automatically retry a timeout after request bytes may have reached a local server. |
| Local/Docker/Singularity/Bubblewrap/SWE-ReX command | Mostly 30 seconds; ProgramBench 180; SWE-bench 60. | Set the interactive slow-local default to 10 minutes, retain a finite environment default, and permit validated per-Bash-call `timeout_seconds` capped by optional environment maximum. Keep benchmark values explicit. |
| Docker pull/start | Docker `pull_timeout` is 120 seconds; container lifetime 2h. | Keep distinct; provide documented slow-local overrides. Do not use model timeout settings for environment startup. |
| Singularity build | retries but no explicit subprocess timeout. | Audit separately before changing: add a build timeout only with cleanup semantics and a practical default. |
| Modal | startup 60; runtime/deployment 3600; stop waits 10. | Leave provider-specific limits unchanged in v1 except document them. |
| Agent wall clock | checked before model call; default 0, ProgramBench 6h. | Keep an optional total run deadline, but state clearly it includes model time. In slow-local config default to 0. Add distinct telemetry for model vs deterministic elapsed time. |

Use monotonic time for durations. A timed-out command returns a normal observation with
partial output and `timeout: true`; it is not a model failure. Do not make every
timeout infinite: connection setup needs a bound, commands need a default safety
boundary, and users need explicit override control.

## LiteLLM and local-server considerations

- Keep LiteLLM as the adapter rather than creating a llama.cpp-specific client.
  Configure `api_base`, provider, and `model_kwargs` in a dedicated local YAML file.
- Pass only documented, verified timeout arguments. LiteLLM/provider parameter names
  vary; cover both OpenAI-compatible llama.cpp and the intended Kimi/MiniMax serving
  path with integration tests before claiming the mapping works.
- Keep `drop_params: true` only when it is known not to discard required local-server
  controls; log the effective supported parameters at debug level without secrets.
- Let local models use `cost_tracking: ignore_errors` and treat `n_model_calls`, input
  tokens (when reported), output tokens, and elapsed generation time as first-class
  metrics. Dollar cost is often meaningless locally.
- Test tool schema compatibility. If the server cannot reliably emit multiple native
  calls or preserve tool IDs, use `litellm_textbased` temporarily and postpone the
  text protocol expansion until measured evidence supports it.
- Preserve a stable system/task prefix and stable tool schemas to maximize any
  llama.cpp server prompt-cache reuse. Whether append-only history actually reuses a
  cache is an empirical server/configuration question.

## Failure recovery and resumability

This is useful soon, not part of the initial slow-inference core. Phase 3 real-model
validation must first establish that interruptions/lost work justify it and that the
following design fits observed failure modes.

After every model response, after every normalized tool result, and on controlled
interrupt, atomically write a checkpoint beside the trajectory (temporary file,
flush/fsync where practical, then `Path.replace`). The checkpoint holds full
trajectory, working state, counters, config snapshot, and a `phase` value:
`before_query`, `after_model`, `after_tool`, or `finished`.

`mini --resume PATH` must validate the checkpoint version and configuration
compatibility, reconstruct the agent, and continue from the last completed phase. It
must never replay an action known to have a completed result. If a crash occurred in
`before_query`, ask the model anew. If it occurred during model I/O, mark the request
as `unknown_outcome` and require an explicit `--resume-ambiguous-request` choice (or
interactive confirmation) before issuing a potentially duplicate query. That small
bit of friction is preferable to silently spending another 20 minutes.

Model malformed responses remain `FormatError` observations with the billed response
preserved. Text-tool failures are ordinary, concise observations—not exceptions—and
therefore retain the chance to correct with the next model turn. `KeyboardInterrupt`
should checkpoint, report an interrupted status, and return without deleting the
workspace or trajectory. Resume is a CLI/run concern initially; do not promise exact
cross-environment resume for ephemeral Docker/Modal containers.

## Testing strategy

Unit tests, with no live model required:

- `TextTool`: successful exact replacement; zero match; multiple match; empty old
  text; absent/invalid paths; unchanged bytes after every failure; exact bytes outside
  the one replacement; bounded confirmation excerpt; create collision behavior; and
  200-line/12,000-character read limits without loading a huge file unnecessarily.
- Environment text execution: prove that local and at least Docker execute against
  their own configured workspace; prove a container action cannot modify the host
  workspace; cover helper-unavailable results; and assert normalized results are
  identical across environments for the same fixture. Add analogous adapter tests as
  each supported environment gains the capability.
- Action parsing: strict Bash and text schemas; unknown tool/extra required field
  errors; ordered multiple calls; text tool observations for both Chat Completions
  and Responses API.
- Output formatter: short output; truncation marker/count; error-tail preference;
  exception preservation; raw output absent from compact prompt but present in saved
  data.
- Timeouts: defaults for every environment; per-action override reaches each
  environment; model timeout configuration mapping is passed correctly; monotonic
  duration; a timeout preserves partial output.
- Retry: retry known connect failures; do not retry ambiguous response/read timeout;
  exact attempt counts and backoff configuration.
- State/resume (Phase 4 only): checkpoint atomic-write helper; state updates; no
  action replay after a persisted tool result; controlled interrupt; compatibility
  rejection; explicit ambiguous-request handling.
- Context (Phase 5 only): linear mode byte-for-byte preserves current API messages;
  compact mode retains task/state/latest exchange, has deterministic output, obeys
  its budget, and preserves required tool-call/result pairs.
- Configuration/prompt: default and slow-local YAML load; rendered prompts contain
  the batching/editor rules but not stale `sed` tutorials; all existing configs still
  instantiate their selected agent/model/environment.

Run the current test suite before and after each phase. Add an integration matrix
behind an opt-in marker for a local OpenAI-compatible llama.cpp server: basic chat,
one/multiple tool calls, no-tool format error, long generation, server disconnect,
and timeout mapping. Kimi Code and MiniMax-M3 get their own optional entries once the
actual serving API is chosen.

## Practical slow-inference benchmark

Add a small deterministic fixture corpus, not a SWE-bench replacement. Each fixture
is a tiny Git repository plus a scripted model response sequence (and later an
optional live-model runner):

1. repository understanding: find a call chain and produce a Markdown outline;
2. exact edit: change a uniquely identifiable function then run focused tests;
3. ambiguous edit: receive an ambiguous replacement, inspect, then correct;
4. noisy failing test: extract the failure without rereading a giant log;
5. interruption/resume after an edit and after a tool result (Phase 4 only).

Report per task and aggregate:

- success and validation status;
- model calls; tool calls per model call; model vs deterministic elapsed seconds;
- request input/output tokens when the server reports them;
- repeated input characters/tokens (hash prompt prefixes and count resend);
- output characters admitted to the live prompt and truncation events;
- exact-edit failure/recovery rate; rereads of identical `(path, content hash)`;
- retries by class and ambiguous request count.

The scripted run makes regressions cheap and repeatable. The live run establishes
real throughput/cache behavior. Do not optimize a change solely because it lowers
SWE-bench steps; require an improvement in these slow-inference metrics or a clearly
explained reliability benefit.

## Suggested implementation phases

### Phase 0 — Baseline

**Completed 2026-08-22.** See
[`slow-inference-phase-0-baseline.md`](slow-inference-phase-0-baseline.md).

The dependency-light baseline passed with 314 passed and 10 skipped. The recorded
full-suite blockers are missing/incomplete optional dependencies, a LiteLLM API
mismatch, and unavailable unprivileged Bubblewrap namespaces; they are not Phase 0
runtime changes. The document also records representative deterministic trajectories
and confirms that no telemetry or production behavior was added. Re-run the full
suite in a provisioned development environment before using it as a Phase 1 gate.

### Phase 1 — Slow-inference core

**Reopen until environment-backed text execution is complete.** Implement the
slow-local prompt/config; `read_text`, exact `replace_text`, and create-only
`create_text`; multiple useful tool calls per model turn where supported; per-action
Bash timeout; longer slow-local command defaults; and tests. Execute tool calls
sequentially in declared order, not concurrently. Do not yet compact history or alter
the default compatibility config.

For text operations, first add the small `Environment.execute_text(action)` capability
and route the agent dispatcher through it. Implement exact semantics once in the
shared text module. `LocalEnvironment` calls it directly. Each sandbox/container
adapter executes the fixed helper inside its own configured `cwd`, passing action data
without shell interpolation. The host must never access a container workspace path,
and a missing in-sandbox helper runtime must produce `text_tool_unavailable`, not a
host fallback or a `sed` fallback. Cap reads at 200 lines and 12,000 characters,
including efficient early rejection. Add local and Docker regression tests proving
the text action affects only the selected environment and preserves exact-match
failure safety.

### Phase 2 — Request reliability and output efficiency

Add explicit model timeout policy, safe retry classification, and no automatic retry
of ambiguous long-running inference. Add compact deterministic tool-output shaping,
model/tool timing and call-count instrumentation, and token metrics when available.

### Phase 3 — Real-model validation

Stop feature work and run representative tasks with Kimi Code and MiniMax-M3 through
llama.cpp/LiteLLM. Validate tool-call behavior and LiteLLM timeout handling; measure
model calls, elapsed inference time, tool calls per model turn, repeated prompt/input
content or tokens, edit failures, prompt-evaluation time, and cache behavior. Record
shortcomings before adding more architecture. This phase is responsible for answering
as many assumptions below as practical and deciding whether the proposed resumability
and compact-context designs still fit the observed workload.

### Phase 4 — Resumability

Implement checkpoint/resume only if Phase 3 confirms the value and the current design
still fits observed failure modes. Preserve the trajectory inspector format or version
it deliberately with a migration/reader test.

### Phase 5 — Compact live context

Implement and benchmark deterministic context projection. Compare it with linear
history and adopt it only if it materially improves slow inference without harming
reliability. The full trajectory remains the source of truth.

### Phase 6 — Experiments

Try llama.cpp-specific cache/slot work, a deterministic repository map, a text-mode
multi-tool protocol, or other measured optimizations only when evidence shows the
current approach is insufficient.

## Explicit non-goals for now

- No LangChain, graph executor, planner/reviewer multi-agent system, or background
  orchestration framework.
- No stateful persistent shell; independent command execution remains a valuable
  isolation property.
- No regex/fuzzy/AST editor, patch language, automatic conflict resolution, or
  guessed occurrence selection.
- No automatic repository-wide indexing/vector database. Start with `rg`, `git`,
  targeted reads, and a possible future deterministic map.
- No automatic test selection AI, autonomous commit/PR workflow, or specialization
  for DesignDiviner versus ConjurePR.
- No silent infinite timeouts, automatic retry of ambiguous generations, or implicit
  resume that can replay edits/actions.
- No replacement of the full trajectory with summaries; it remains the source of
  truth for inspection/debugging.

## Assumptions requiring validation

1. Kimi Code and MiniMax-M3 through the selected local serving stack reliably support
   the OpenAI Chat Completions tool schema, multiple calls per response, and tool-call
   IDs. If not, tool-call mode cannot be the universal default.
2. llama.cpp/server configuration has prompt caching that benefits from an invariant
   prefix plus append-only/compact message evolution; measure cache-hit and prompt
   evaluation time rather than assume it.
3. LiteLLM can cleanly separate connect from full generation/read timeout for each
   chosen provider. If it cannot, a narrowly scoped local adapter may be warranted,
   but only after tests demonstrate the need.
4. The models can accurately copy a source fragment into `old_text`; success rate and
   ambiguity rate should be measured. `read_text` excerpts must be sized to make this
   practical.
5. The local machine's filesystem and tests really are cheap relative to inference.
   Benchmark timing should verify this; very large builds may deserve command timeout
   defaults tailored to that environment.
6. A durable local filesystem path is available for checkpointing. Container/remote
   runs need an explicit mounted output directory before resume is promised.
