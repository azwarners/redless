# REDLESS Roadmap

REDLESS is a small autonomous software-engineering execution engine for local language models.

Its intended contract is deliberately narrow:

> A caller prepares a workspace, supplies a task and configuration, and starts REDLESS. REDLESS autonomously inspects the workspace, uses tools, performs the requested work, validates it where appropriate, and exits with a useful result.

REDLESS is intended to be easy for higher-level applications to invoke programmatically.

REDLESS is **not** intended to become a scheduler, workflow engine, model lifecycle manager, multi-agent framework, durable job queue, repository orchestrator, or general application platform. Those responsibilities belong to callers built around REDLESS.

This roadmap focuses on finishing the transition from the inherited mini-SWE-agent-derived application into a clean, dependable execution primitive.

---

## Phase 2 — Complete the Public REDLESS Rename

Finish changing the product identity from `mini-swe-agent-slow` / `mini-slow` to REDLESS.

This phase is primarily an implementation task and should avoid unrelated architectural changes.

### Work

Update public-facing names where appropriate, including:

- command-line executable:
  - `mini-slow` → `redless`
- configuration directories
- per-workspace configuration paths
- log and trajectory default names
- package metadata
- help and version output
- progress/status prefixes
- installation and usage documentation
- examples
- scripts
- tests and fixtures that represent the public product name

Preserve references to upstream `mini-SWE-agent` when they describe project ancestry, compatibility, or inherited implementation.

Do **not** rename the inherited internal Python namespace solely for branding consistency unless a concrete technical need is identified.

### Exit criteria

- Users invoke the application as `redless`.
- Normal documentation consistently calls the product REDLESS.
- REDLESS configuration and workspace paths use REDLESS naming.
- Remaining references to old product names are reviewed and intentionally retained.
- Existing execution behavior remains unchanged.
- Tests pass.

---

## Phase 3 — Harden Autonomous Execution and Workspace Containment

Before removing inherited interactive behavior, strengthen the tests and safety boundaries around the execution model REDLESS intends to keep.

This phase creates the safety net for subsequent refactoring.

Interactive functionality should still be tested while it still exists. Interactive-specific tests should not be removed merely because that functionality is scheduled for deletion in the next phase.

### Autonomous behavior to strengthen

Tests should cover, where applicable:

- task submission through command-line arguments
- workspace/current-directory behavior
- configuration loading and layering
- missing or invalid task input
- invalid configuration
- model request failures
- malformed model responses
- tool-call parsing
- execution of tool calls
- observation return to the model
- continuation into a second model call after tools execute
- multiple tool calls in one model response
- final prose terminating a run
- output/trajectory creation
- output truncation behavior
- model-call and execution limits
- non-TTY execution
- redirected stdout/stderr
- streaming modes
- recoverable format errors
- malformed or interrupted streamed model responses
- command/tool failures
- preservation of useful failure information

Tests that cross the model → agent → tool → observation → model boundary are especially valuable. Parser-only tests are insufficient for behavior that depends on the complete execution loop.

### Workspace containment

Treat the assigned workspace as an execution boundary rather than merely as the initial working directory.

By default, an autonomous REDLESS task must not be permitted to modify files outside its assigned workspace.

The Bash tool should inspect commands before execution and reject obvious attempts to mutate filesystem locations outside the workspace.

This includes, where practical:

- absolute paths outside the workspace
- relative paths using `..` that escape the workspace
- commands that `cd` outside the workspace before performing mutations
- source/destination combinations that write into another repository or directory
- shell expressions whose resolved mutation target is known to be outside the workspace
- symlink-based escapes where the target can be determined safely

Workspace checks must use resolved/canonical paths where possible rather than simple string-prefix matching.

A rejected command should return a useful observation to the model explaining that the operation violates the workspace policy.

Example:

    Bash command rejected: write target resolves outside the assigned workspace.
    Allowed workspace: /path/to/workspace

Static inspection should be treated as a guardrail rather than assumed to be a complete shell-security mechanism. Shell syntax, expansion, subshells, interpreted scripts, and symlinks make perfect static analysis impractical.

Investigate stronger OS-level containment for Bash execution so that, where feasible:

- the assigned workspace is writable
- unrelated repositories are not writable
- system locations are not writable
- external filesystem locations are read-only or inaccessible unless explicitly allowed

The safe default should remain workspace-only mutation.

Any future mechanism for allowing external writable paths should require explicit configuration.

### Regression scenarios

Add tests based on real failure modes, including attempts such as:

    cp ./README.md /another/repository/README.md

and:

    cd /another/repository && touch file

These operations must not succeed under the default workspace policy.

### Contract to establish

Where practical, begin enforcing:

- progress and diagnostics are emitted separately from the final result
- execution does not require a human terminal
- autonomous task execution remains deterministic enough to exercise from subprocess tests
- tools cannot silently escape the assigned workspace for filesystem mutation
- workspace-policy violations are observable and diagnosable

### Exit criteria

- The autonomous execution path has substantially stronger integration coverage.
- Known failure modes discovered during slow-model testing are represented by regression tests.
- Attempts to modify files outside the assigned workspace are rejected under the default policy.
- The suite provides confidence for removal of the interactive path.
- Workspace containment is strong enough that REDLESS can proceed toward unattended use.

---

## Phase 4 — Blueprint and Remove the Interactive CLI

REDLESS is not intended to be an interactive chat application.

Before implementation, perform a focused architecture/design pass against the actual repository to identify the smallest clean execution path that remains when interactive human conversation is removed.

### Blueprint questions

The design pass should identify:

- the current CLI entry points
- where interactive behavior begins
- interactive agent classes and helpers
- shared code used by both interactive and autonomous execution
- setup/configuration behavior that assumes a human is present
- tests specifically tied to interactive operation
- documentation tied to interactive operation
- abstractions that become unnecessary after interaction is removed
- the minimum reusable boundary between argument parsing and autonomous execution

The architectural target is:

> workspace + task + configuration → autonomous execution → result/failure

### Implementation

After the blueprint is reviewed:

- remove the interactive CLI path
- remove interactive-only classes and helpers
- remove interactive-only tests
- remove interactive-only documentation and configuration
- simplify shared abstractions where the removal exposes unnecessary complexity
- preserve the autonomous execution path
- eliminate environment-variable or setup workarounds whose only purpose is suppressing interactive prompts
- avoid adding replacement interaction abstractions

### Exit criteria

- REDLESS never requires interactive human participation during normal execution.
- No inherited interactive CLI remains.
- Autonomous execution continues to work.
- The test suite passes.

---

## Phase 5 — Prove the Headless Execution and Containment Contract

After interactive support is removed, explicitly verify REDLESS as a headless execution engine.

This phase is a validation milestone rather than a feature expansion.

### Required scenarios

Run REDLESS from automated/subprocess tests with:

- no TTY
- no interactive stdin
- redirected stdout/stderr
- an explicit workspace
- an explicit task
- explicit configuration

Test at least:

- successful completion
- model failure
- malformed model output
- interrupted or malformed model stream
- tool failure
- configured model-call/step limits
- termination by signal where practical
- partial trajectory/result preservation where practical
- attempted mutation outside the workspace
- attempted workspace escape through relative paths
- attempted mutation after changing directories
- attempted escape through symlinks where practical

### Containment contract

Verify that the supported execution environment enforces:

> An autonomous REDLESS run may modify its assigned workspace but cannot modify arbitrary host filesystem locations under the default policy.

Workspace-policy failures should be distinguishable from ordinary command failures and should preserve useful diagnostics.

### Exit criteria

A caller can reliably perform:

> prepare workspace → invoke REDLESS → wait → inspect status/result

without participating during execution.

An autonomous REDLESS run cannot modify files outside its assigned workspace under the default supported execution policy.

At the end of this phase, REDLESS should be suitable for serious unattended use.

---

## Phase 6 — Improve Installation, Updating, and Workspace Setup

Make the operator experience boring, predictable, and documented.

REDLESS should be straightforward to install on a clean supported Linux system, update safely, prepare for use inside a repository, and validate after changes.

### Dedicated REDLESS user

Recommend and document running REDLESS under a dedicated unprivileged Linux account.

The standard installation path should use an account such as:

    redless

The documentation should explain that autonomous code execution should not normally run under a user's primary login account.

The `redless` account should:

- be unprivileged
- not have unrestricted `sudo` access
- own the REDLESS installation where appropriate
- own or have explicit access to REDLESS workspace directories
- avoid write access to unrelated repositories and system locations
- have only the permissions required for intended REDLESS workloads

Provide straightforward setup instructions for creating and entering the account on supported Linux systems.

Example:

    sudo useradd --create-home --shell /bin/bash redless

    sudo -iu redless

The dedicated account is an outer operating-system safety boundary. REDLESS workspace containment remains an additional inner boundary.

### Installation

Review and improve installation tooling so that it:

- installs the expected dependencies
- creates or uses the Python environment consistently
- installs REDLESS correctly
- exposes the `redless` command
- works cleanly under the dedicated `redless` account
- can be rerun safely where practical
- performs a lightweight post-install verification

### Updating

Provide a safe update workflow.

The updater should detect and clearly report conditions such as:

- dirty working tree
- unexpected branch
- divergent Git history
- failed fetch/pull
- dependency update failure
- post-update test or smoke-check failure

It should avoid destructive Git behavior.

Where useful, support a non-destructive check mode that reports whether an update can proceed safely.

### Workspace setup

Improve the workspace initialization experience.

A workspace setup helper should:

- create only the minimal REDLESS-specific structure required
- avoid overwriting existing configuration without explicit permission
- provide useful starter configuration
- make the expected relationship between global and workspace configuration obvious
- make ownership and permissions appropriate for the `redless` account
- make the active workspace boundary obvious

Prefer a simple interface such as:

    redless init

if that fits the resulting CLI cleanly.

### Documentation

Document one straightforward lifecycle:

> Create the REDLESS user → install REDLESS → prepare a workspace → configure a model → run a task → update REDLESS later

### Exit criteria

A new user can create the recommended runtime account, install, configure, initialize, run, and later update REDLESS without repository archaeology or undocumented commands.

The default installation guidance does not encourage autonomous execution under a privileged or primary user account.

---

## Phase 7 — Formalize Logging, Results, and Machine-Facing Output

Higher-level callers need a stable way to observe REDLESS without parsing an interactive transcript.

Do not turn REDLESS into a daemon or process supervisor.

### Logging

Provide explicit, configurable destinations for:

- progress logs
- model-call progress
- tool execution information
- trajectory data

Ensure output is flushed appropriately for long-running/background execution.

### Result contract

Define stable behavior for:

- final result
- successful completion
- execution failure
- configuration failure
- model failure
- model-stream failure
- tool failure
- workspace-policy violation
- safety-limit termination
- externally terminated runs

Use meaningful process exit codes.

Where useful, provide an optional structured result artifact.

### Schema versioning

Any machine-consumed trajectory or result format should include an explicit schema version, for example:

    {
      "schema_version": 1
    }

External callers should not have to depend accidentally on undocumented internal serialization details.

### Background execution

REDLESS should run correctly when launched using external process supervision such as:

- shell background execution
- `nohup`
- `systemd-run`
- Python `subprocess`

REDLESS itself does not need to implement daemonization unless a future real-world requirement demonstrates otherwise.

### Exit criteria

- REDLESS can run unattended with durable logs.
- Callers can determine success or failure without scraping human-oriented output.
- Machine-facing result/trajectory formats are versioned.
- Workspace-policy violations are represented clearly.
- Background execution requires no attached terminal.

---

## Phase 8 — Add Cooperative Steering Between Model Calls

Add an optional mechanism allowing an external caller to influence a long-running REDLESS task between expensive model generations.

This must **not** reintroduce the interactive CLI.

Steering is asynchronous input to an autonomous process.

### Intended control flow

    model generation
          ↓
    tool execution
          ↓
    record observations
          ↓
    optional steering window
          ↓
    consume pending steering
          ↓
    next model generation

### Initial design

Support an optional configurable delay between model calls.

Example concept:

    agent:
      inter_call_delay_seconds: 0

A value of `0` preserves current fully autonomous behavior.

Allow an external caller to place steering input into a simple inbox mechanism. A filesystem-based implementation is preferred initially because it is easy to inspect, test, and automate.

Example concept:

    agent:
      steering_file: .redless/steer.txt

Between model calls, REDLESS can:

1. check for pending steering
2. atomically consume it
3. append it to the conversation as explicit steering/user input
4. record it in the trajectory
5. continue autonomous execution

Steering files must remain within the workspace or another explicitly approved REDLESS-owned state location.

Possible later extensions may include a pause sentinel or indefinite inter-call hold if a real consumer needs it.

### Explicit non-goals

Do not initially add:

- socket servers
- HTTP control APIs
- event buses
- interactive stdin prompts
- GUI-specific control protocols

### Exit criteria

- Fully autonomous behavior remains the default.
- Callers can optionally steer long-running work between model calls.
- Steering events are visible in the trajectory.
- Steering does not weaken workspace containment or reintroduce interactive execution.

---

## Phase 9 — Freeze the External Contract

At this point, stop adding speculative REDLESS features.

Document the small external contract that callers may rely upon.

This should cover:

- executable/invocation
- workspace assumptions
- workspace containment policy
- task input
- configuration
- logging
- trajectory/result artifacts
- exit semantics
- steering, if enabled
- supported runtime assumptions
- what REDLESS explicitly does not manage

The stable core remains:

> one workspace + one task + one autonomous execution

### Exit criteria

- REDLESS has a concise documented external contract.
- Supported machine-facing behavior is explicit.
- Internal implementation details are not accidentally treated as public API.
- Future REDLESS changes are driven primarily by concrete execution-engine requirements rather than speculative orchestration features.

---

# Near-Term Non-Goals

Unless real downstream use demonstrates a need, REDLESS should not add:

- scheduler or workflow engine
- durable job queue
- multi-agent orchestration
- model lifecycle manager
- model downloader/registry
- multi-repository execution semantics
- daemon/service framework
- embedded web server
- database
- plugin marketplace
- GUI-specific behavior
- complex IPC system
- internal repository handoff orchestration

These belong outside REDLESS.

---

# Guiding Principle

When deciding whether functionality belongs in REDLESS, ask:

> Does this help one autonomous model complete one task safely inside one prepared workspace?

If yes, it may belong in REDLESS.

If it coordinates tasks, repositories, models, machines, users, schedules, or long-lived application state, it probably belongs in an orchestrator built around REDLESS.
