# Codex support plan

## Recommendation

Add Codex as a second, explicit backend in this project rather than creating a separate
optimizer. Keep Claude as the default and preserve the current command-line, stdout,
artifact, retry, and selection contracts. The optimization algorithm is largely
provider-neutral; candidate installation, process transport, event interpretation,
model naming, and provider configuration are the boundaries that need to vary.

Do not implement the Codex transport until an executable spike proves that Codex
exposes a stable, machine-readable signal for **skill selection**. Prefer `codex exec`
if its JSONL events distinguish loading the candidate skill from ordinary file reads.
Use app-server only if it provides that distinction more reliably. If neither surface
does, stop rather than infer triggering from final-answer text or create a behaviorally
different Codex-only tool.

## Compatibility contract

The first release must be additive:

- Existing invocations without a backend flag continue to use `claude`. Installing
  another CLI must never silently change results.
- Add `--backend {claude,codex}`, defaulting to `claude`.
- Preserve `best_description` and all existing stdout/report fields. Add `backend` and
  generic call-estimate fields while retaining `estimated_claude_calls` for Claude.
- Keep stdout as one JSON object, stderr as progress only, `--report none` as headless
  mode, and empty stdout for fatal preflight or child-process failures.
- Keep the eval-set format, seeded stratified holdout, tri-state results, one retry,
  selection policy, artifacts, `--write`, Python floor, and compatible aliases.
- Enforce the universal 1,024-character description ceiling for both backends. A
  smaller `--max-desc-chars` remains valid; values above 1,024 must be rejected.
- Use one backend for evaluation and improvement in a run. Mixed-provider runs can be
  considered later only with explicit flags and report provenance.

## Phase 0: capability spike and decision gate

Use a temporary fixture (not the checked-in end-to-end skill) and capture raw
transcripts from the installed Codex version.

1. Verify non-interactive authentication and enumerate the actual options and event
   schemas for `codex exec --json` and app-server. Record the Codex version with each
   fixture; do not design against remembered event names.
2. Install one uniquely named candidate as a real Agent Skill in an isolated temporary
   Codex home/project. Give its body a unique inert marker and ensure ambient user and
   project skills are not discoverable.
3. Probe a clear positive, clear negative, a positive that uses another tool first,
   malformed events, an empty stream, timeout, non-zero exit, and a competing skill.
   Repeat probes to expose nondeterministic ordering.
4. Identify a semantic event for selected-skill loading. A generic command/file-read
   event is acceptable only when the isolated canonical path and event semantics make
   false attribution impossible.
5. Compare transports on signal quality, early cancellation, model and reasoning
   controls, concurrency, startup cost, sandbox behavior, and version stability.

The gate passes only if a parser can produce the same states as the Claude path without
examining natural-language output:

- `True`: an unambiguous candidate selection/load event was observed.
- `False`: a documented terminal event arrived without candidate selection.
- `None`: timeout, process/protocol failure, or an incomplete stream prevented a
  decision.

Prefer `codex exec` after a pass because its one-shot lifecycle matches `claude -p` and
isolates concurrent probes. Select app-server only if it is the smallest public, stable
surface supplying the missing event. If both fail, publish the spike findings and
pause; do not weaken the scoring proxy.

## Phase 1: isolate provider boundaries without changing Claude

Refactor behind a small internal backend protocol while keeping aggregation and
selection provider-independent:

```text
Backend
  preflight() -> availability/version or one-line failure
  resolve_models(value) -> model ids
  estimate_calls(plan) -> estimate fields
  evaluate_probe(candidate, query, model, timeout, options) -> True | False | None
  improve(prompt, model, effort, timeout, budget) -> raw process result
```

Suggested layout:

```text
src/skill_optimizer/backends/
  base.py                 # protocol and neutral process-result types
  claude.py               # current transport, settings, and aliases
  codex.py                # isolated home, argv/protocol, and event adapter
```

Keep event interpretation pure and separate from subprocess lifecycle. First move the
Claude behavior with characterization tests, so any contract change is visible rather
than incidental. Retain existing public imports or compatibility re-exports.

Use generic internal names (`agent_calls`, `agent_bin`, `backend_options`) without
removing published Claude-specific fields or environment variables. Add
`SKILL_OPTIMIZER_CODEX_BIN` for deterministic tests, parallel to the Claude override.

## Phase 2: Codex evaluation

For every probe:

1. Create a fresh temporary home/project and install exactly one candidate using
   Codex's documented Agent Skill layout. Preserve the skill body and replace only the
   description being evaluated.
2. Disable ambient discovery with documented configuration or isolated homes. Never
   move or mutate a user's installed skills.
3. Build argv as a list with no shell, constrain the environment, and pass a model only
   when requested. Retain the bounded worker pool and per-probe timeout.
4. Parse JSONL incrementally, stop at a decisive state, then kill and reap the process.
   Ignore unknown events for forward compatibility. Treat completion as decisive only
   when the pinned protocol defines the terminal event.
5. Keep raw Codex diagnostics in private attempt artifacts; never expose prompts,
   stderr, temporary paths, or raw events in the public envelope.

Initially support only model and timeout plus a reasoning setting if the spike proves a
stable mapping. Do not reinterpret Claude's `--disable-plugin`: reject it clearly on a
Codex run until a documented equivalent exists.

## Phase 3: Codex improvement

Reuse prompt construction, JSON extraction, shortening, retry taxonomy, launch budget,
and 1,024-character validation. Make the prompt say “Agent Skill” and identify the
selected evaluator backend rather than teaching Codex about Claude commands.

Prefer native structured output if the chosen Codex transport documents it, while
retaining defensive parsing for wrapper metadata. Record backend, CLI version, model,
reasoning setting, redacted argv, return code, stdout, and stderr in raw artifacts.
Public retry records remain limited to the current allowlisted messages.

Keep evaluator and improver parsers separate: final improvement JSON and skill-selection
events are distinct protocols even if they share process helpers.

## Phase 4: CLI, output, and documentation

Add:

- `--backend claude|codex` with `claude` as the default.
- `--codex-reasoning <value>` only if the pinned CLI documents stable values.
- Backend-aware preflight errors such as `codex CLI not found or not executable:`.
- `backend`, `backend_version`, and `estimated_agent_calls` in dry-run and real output.
  Keep `estimated_claude_calls` for Claude and omit it, rather than lie, for Codex.

Keep model precedence unchanged: `--model` sets evaluator and improver, while
`--models` and `--improver-model` override it. Resolve aliases within one backend only;
never send Claude aliases to Codex or silently substitute a model. Echo resolved IDs
and CLI version for reproducibility.

Update installation, invocation, cost, caveat, and end-to-end documentation. Retain and
label the canonical Claude invocation as the default, then show its Codex equivalent.
Change package metadata only when support is implemented, not during the spike.

## Phase 5: tests and release gates

Maintain 100% line and branch coverage and extend the fake-binary approach so routine
tests need no credentials or tokens:

- Recorded Codex JSONL for trigger, no-trigger, unrelated first action, unknown or
  malformed events, terminal events, timeout, and non-zero exit.
- Pure parser tests plus fake-`codex` integration tests for argv, isolated layout,
  environment, early termination, retries, and cleanup.
- Cross-backend contract tests asserting equivalent aggregation, selection, envelope,
  artifacts, redaction, and writes from equivalent probe outcomes.
- Regression tests proving omitted `--backend` takes the Claude path and all current
  aliases and invocation forms still work.
- Description boundaries at 1,023, 1,024, and 1,025 characters, including multiline
  and Unicode. Keep Python code-point counting unless the Agent Skills specification
  explicitly requires bytes or grapheme clusters.
- Dry-run tests proving no temp path, artifact, or provider process is created.

After mocked gates pass, add Codex live gates parallel to Claude's: environment/version,
dry-run, real improvement, flag surface, invocation forms, packaging, and wheel install.
Record the tested Codex version and expand the compatibility range only after testing
multiple versions. Do not release based on the spike alone.

## Risks and stop conditions

- **No selection event:** stop; final-answer heuristics cannot reproduce the metric.
- **No skills in non-interactive execution:** try app-server, then stop if it also lacks
  the capability. A separate tool would not fix an unobservable trigger.
- **Ambient skill leakage:** stop if documented isolation cannot prevent competition.
- **Protocol changes:** unknown events may be ignored, but changed selection/terminal
  semantics fail closed as `None`, never as false negatives.
- **App-server state bleed:** use one isolated session per probe or prove reset
  semantics before enabling concurrency.
- **Different model behavior:** scores across providers are not directly comparable;
  retain backend/model provenance and select within one backend per run.

## Delivery sequence

1. Capability fixtures and a decision record choosing exec, app-server, or no build.
2. Provider protocol and behavior-preserving Claude adapter.
3. Codex evaluator and pure event interpreter.
4. Codex improver and structured-output parser.
5. CLI/output additions, docs, mocked matrix, and live Codex gates.
6. Beta release after both unchanged Claude gates and new Codex gates pass cleanly.

This sequence makes the decisive unknown—the availability of a trustworthy skill
trigger signal—the first test, before any broad refactor can threaten Claude
compatibility.
