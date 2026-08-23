# Browser Harness v1/v2 Benchmark Feasibility Study

Status: implemented by `run_harness_comparison.py`

Reviewed: 2026-08-22

Implemented: 2026-08-23

Benchmark revision: `bc6fd7849aa102c4204509de0a83da680212b22d`

## Executive summary

Making Browser Harness a benchmark variable is feasible. It requires a moderate
adapter refactor rather than a new argument alone.

The public framework runner already treats the benchmark, framework, browser,
model, task selection, concurrency, timeout, and free-form framework parameters
as variables. Browser Harness is different: the harness-backed adapters currently
assume the historical Python harness layout, command, system prompt, browser
provisioning functions, and working directory.

The implemented design is a paired A/B runner in which the agent, model, local
browser, tasks, limits, judge, and prompt policy are fixed while a first-class
`HarnessSpec` selects either v1 or v2. Each harness should run from a clean,
pinned checkout and its own virtual environment. Browser provisioning should be
owned by the benchmark rather than either harness.

The implementation intentionally uses only the installed Codex and Claude CLIs
with their saved subscription logins. It removes API credentials from child
environments and launches a fresh local Chromium process for every cell; there is
no model SDK or browser-provider API path in this runner. It also removes nested
CLI session identifiers and Browser Use cloud selectors, isolates the harness's saved
cloud-auth path, disables Codex hosted web/apps and subagents, uses Claude safe mode,
and installs a fixed workspace instruction override so ambient personal browser
guidance cannot select the treatment.

A read-only task slice should be the first reported comparison. Browser Harness
v2 mechanically blocks form submission and mutating page requests by default, so
the complete 100-task suite measures both browser capability and product safety
policy. That is a valid product comparison, but it is not a pure capability
comparison.

## Scope reviewed

The review covered:

- `run_framework_eval.py` and the shared framework registry;
- the Claude Code, Codex, and pi Browser Harness adapters;
- Browser Use Cloud provisioning in `browsers/browser_use_cloud.py`;
- result and trace naming;
- the benchmark judge path;
- the historical Browser Harness result convention;
- current Browser Harness v1 and v2 command, package, prompt, and target-routing
  contracts.

The current local harness revisions used to validate the interface differences
were:

- v1: `41108b8676d4bdb58b26ab3b079c0b7b0f8f3926`, package version
  `0.1.9`;
- v2: `7cc604a50c7458e880156ea24f016d9431d86f6f`, package version
  `0.1.0`.

The published historical Claude Code Harness filenames identify v1 revision
`d1d6b59`. That revision exposed top-level `admin`, `helpers`, `run`, and `daemon`
modules and therefore matches the assumptions still present in the public
adapters. Current v1 exposes `browser_harness.admin` instead. A reproducible
comparison must consequently identify “v1” by an exact revision rather than by
an unqualified repository name.

## Current state

### What is already variable

`run_framework_eval.py` already accepts:

- `--benchmark`;
- `--framework`;
- `--framework-ref`;
- `--browser`;
- `--model`;
- `--params`;
- task count, start, or explicit indices;
- task concurrency and timeout;
- interleaved or raw task order.

It starts one runner subprocess per task and passes most of those values through
environment variables. This is a good base for a paired harness dimension.

### Where the harness is hard-coded

The `claude_code_harness`, `codex_harness`, and `pi_harness` adapters each assume:

- `HARNESS_DIR = "/tmp/browser-harness"`;
- the `browser-harness` executable;
- top-level `admin.start_remote_daemon()` and `admin.stop_remote_daemon()`;
- a v1-specific system prompt;
- a shared `/tmp/shots` screenshot directory;
- execution with the harness repository as the agent working directory.

V2 instead installs the `bh` command and the `harness` Python package. It can
attach to an explicit `BU_CDP_URL` or `BU_CDP_WS`, but it does not expose v1's
Browser Use Cloud administration functions.

`--framework-ref` does not currently install or select a framework revision. It
is printed and written into the run summary, but it is not passed to the task
runner. Two invocations with different `--framework-ref` values can therefore
execute the same installed code.

### Result identity is insufficient

The result and trace keys currently contain benchmark, framework, browser, and
model. They do not contain a harness name, harness revision, skill digest, agent
CLI version, or judge model. V1 and v2 runs using the same other axes would share
one result filename and could not be audited reliably later.

### Parallel screenshot collection in the legacy adapters is unsafe

Every harness-backed task process deletes and recreates `/tmp/shots`. With
`--parallel` greater than one, tasks can erase or collect each other's evidence.
The dedicated comparison runner does not use that shared path: every cell has a
unique screenshot directory, and parallel scheduling operates on independent task
pairs while keeping the v1/v2 arms of each pair sequential.

## Implemented design

### 1. Add a first-class harness descriptor

The shared `frameworks/harness_variants.py` module provides:

```python
@dataclass(frozen=True)
class HarnessSpec:
    name: str
    root: Path
    cli: Path
    skill: Path
    git_sha: str
    package_version: str
    skill_sha256: str
    prewarm_script: str
    screenshot_example: str
```

The registry describes:

| Contract | v1 | v2 |
| --- | --- | --- |
| CLI | `browser-harness` | `bh` |
| Python package | `browser_harness` or historical top-level modules | `harness` |
| Skill | selected checkout's `SKILL.md` | selected checkout's `SKILL.md` |
| Screenshot helper | `capture_screenshot(...)` | `capture_screenshot(...)` |
| Explicit CDP endpoint | `BU_CDP_URL` | `BU_CDP_URL` |
| Fresh-process target continuity | daemon-global cursor | explicit lease/default adoption |

The selected root must be a clean checkout at the requested SHA. Each root
should have its own virtual environment. The benchmark process should not import
either selected harness, which avoids package collisions and accidental use of
whatever happens to be installed in the benchmark environment.

### 2. Move browser lifecycle ownership into the benchmark

The implementation creates a fresh benchmark-owned local Chromium process independently
of either harness. This differs from the original cloud-provider proposal so the comparison
can run entirely from Codex and Claude subscriptions without any API key:

1. launch local Chromium with an ephemeral profile and retain its CDP URL;
2. create a unique daemon name containing comparison id, harness, repetition, and
   task id;
3. export the same browser configuration and `BU_CDP_URL` to the selected harness;
4. prewarm the selected daemon before starting measured agent time;
5. run the agent;
6. stop the exact benchmark-owned browser process tree;
7. wait for the harness daemon to exit and verify that its endpoint is gone.

This holds browser provisioning constant and prevents v1's convenience API from
becoming an unintentional part of the treatment.

Each A/B arm receives a separate fresh browser with the same local configuration.
Sharing a browser between arms would leak cookies, tabs, history, and mutations from
the first arm into the second.

### 3. Use an isolated agent workspace

Do not run the coding agent in the harness source checkout. Codex can automatically
load repository instruction files, and the two harness repositories do not contain
identical context. Instead, create one clean workspace per comparison cell containing:

- the selected, digest-recorded `SKILL.md`;
- an identical benchmark system prompt with only the harness command and examples
  substituted;
- a unique screenshot directory;
- no user or repository context files.

The native skill and public API are part of the harness treatment. The surrounding
rules, final-answer format, permissions, task, and agent configuration remain fixed.

### 4. Add a paired comparison dimension

Extend the public runner with:

- `--harnesses v1=<path>,v2=<path>`;
- `--repeats N`;
- `--paired-order fixed|alternate|randomized`;
- `--judge-model`;
- `--codex-effort`, `--claude-effort`, and `--judge-effort`;
- optionally `--task-category` or a reviewed task-capability filter.

Resolve the task indices once, then run both harnesses for every task and repetition.
Alternate A/B versus B/A ordering to reduce time and website-drift bias. Fail before
execution if any fixed axis differs between the two cells.

### 5. Record complete provenance

Every run and task artifact should include:

```json
{
  "comparison_id": "...",
  "harness": {
    "name": "v2",
    "git_sha": "...",
    "package_version": "...",
    "skill_sha256": "..."
  },
  "agent": {
    "framework": "codex-harness",
    "cli_version": "...",
    "model": "...",
    "params": {}
  },
  "browser": {
    "provider": "benchmark-owned-local-chromium",
    "config": {}
  },
  "judge": {
    "model": "...",
    "prompt_version": "..."
  },
  "benchmark": {
    "name": "BU_Bench_V1",
    "task_index": 0,
    "repetition": 0,
    "order": 0
  }
}
```

Result filenames should include a comparison id rather than trying to encode all
of this state into a filename. The manifest becomes the source of truth.

## Implemented command

First validate both checkouts, CLI subscription logins, browser discovery, and the fully
resolved manifest without spending model time:

```bash
uv run python run_harness_comparison.py \
  --harnesses v1=../v1,v2=../v2 \
  --expected-shas v1=41108b8676d4bdb58b26ab3b079c0b7b0f8f3926,v2=7cc604a50c7458e880156ea24f016d9431d86f6f \
  --agents codex,claude \
  --dry-run
```

Then run a paired read-only comparison:

```bash
uv run python run_harness_comparison.py \
  --harnesses v1=../v1,v2=../v2 \
  --benchmark BU_Bench_V1 \
  --task-category WebBenchREAD \
  --agents codex,claude \
  --judge claude \
  --tasks 20 \
  --repeats 3 \
  --paired-order alternate \
  --parallel 3 \
  --task-timeout 1800
```

Use `--execution-mode sequential` (the default) or `--parallel N`. Parallelism is
applied to independent task pairs; the v1 and v2 arms of one pair never overlap.
`--codex-model`, `--claude-model`, and `--judge-model` override CLI defaults without
changing the saved-login authentication path. The corresponding `--codex-effort`,
`--claude-effort`, and `--judge-effort` options make reasoning effort explicit in the
commands and manifest.

The runner prints the fully resolved comparison manifest and requires explicit
confirmation or a `--yes` flag if either checkout is dirty, unpinned, or does not
match the requested revision.

## Experimental boundary

BU Bench V1 contains five groups of 20 tasks:

- `WebBenchREAD`;
- `OM2W2`;
- `InteractionTests`;
- `GAIA`;
- `BrowseComp`.

A conservative keyword screen flags 24 of the 100 task instructions as plausibly
action-oriented, including 16 of the 20 `InteractionTests`. This is a warning,
not a sufficient semantic annotation.

Browser Harness v2 is dry-run by default with no submit override. It blocks form
submissions and mutating `fetch`, XHR, and beacon requests. Therefore two reports
answer different questions:

1. **Read-only capability comparison.** Start with `WebBenchREAD`, then extend to
   manually reviewed tasks that require no external mutation.
2. **Whole-product comparison.** Run all 100 tasks and count v2 safety refusals as
   observed product behavior. Do not describe this as isolated browser capability.

For the complete suite, add reviewed task metadata such as `read_only`,
`browser_state_only`, `download`, and `external_side_effect`. Do not classify tasks
at runtime with keyword heuristics.

V2 also uses explicit target leases when a workflow must retain one exact tab across
fresh processes. Its native skill documents that contract. Multi-tab failures should
record whether a lease was used so target-routing failures can be separated from page
interaction failures.

## Metrics and analysis

The primary metric remains judge success per task, but the analysis should be paired:

- v1 pass / v2 pass;
- v1 pass / v2 fail;
- v1 fail / v2 pass;
- v1 fail / v2 fail.

Also report:

- wall-clock duration;
- agent turns and command executions;
- input, cached input, output, and reasoning tokens where available;
- CLI-reported token counts and cost estimate when available (informational only for
  subscription runs);
- screenshot count;
- technical failure class;
- safety refusal count;
- captcha and impossible-task counts.

For read-only capability reports, retain strict scores only for auditability. If any cell
reaches CAPTCHA or human verification, exclude that entire task from scored aggregates,
paired outcomes, and paired timing across all agents, harnesses, and repetitions in the
run. This avoids retaining clean cells from a task whose accessibility changed during the
experiment. Impossible-task blockers remain excluded at the affected pair level. Report
excluded task identifiers, outcomes by within-pair order, and the judge's failure reason.
A concentrated blocker rate in the second arm is an experimental-validity warning, not
evidence that the corresponding harness is worse.

Aggregate averages alone are insufficient because live websites and model sampling are
noisy. Use at least three repetitions for a report intended to support a product claim.
Alternating pair order reduces temporal bias but cannot eliminate website drift or model
nondeterminism.

## Rollout sequence

Implementation and local smoke validation cover steps 1–6. The larger pilot and CI
work in steps 7–10 remain deliberate follow-up operations; they are not run
automatically because they consume subscription model time.

1. Add `HarnessSpec`, validation, provenance capture, and isolated workspaces.
2. Decouple local Chromium provisioning from v1 `admin` imports.
3. Support the harness dimension in both Codex and Claude Code CLI adapters.
4. Namespace screenshots and daemon names; add teardown verification.
5. Add paired execution and comparison output.
6. Run a one-task smoke test for both harnesses.
7. Run the 20-task read-only pilot with one repetition.
8. Add repetitions and only then increase task concurrency.
9. Generalize the shared lifecycle to the other two harness-backed adapters.
10. Add or extend GitHub Actions only after the local paired path is stable.

## Validation

Unit tests cover:

- selecting v1 and v2 resolves different commands, roots, skills, and versions;
- dirty or revision-mismatched roots failing before CLI execution;
- removal of model credentials, nested-session state, and browser cloud selectors;
- subscription-safe Codex and Claude command construction;
- deterministic pairing, alternating arm order, and non-colliding cell identities;
- complete fixed-axis manifest identity; and
- paired quality, duration, and token aggregation.

No-model real-browser smoke validation demonstrated:

- one fresh local Chromium browser per arm;
- successful v1 and v2 attachment through the same benchmark-owned CDP path;
- a simple navigation and screenshot from each harness;
- separate-process prewarm and browser operations; and
- no leaked browsers or daemon endpoints after completion.

## Effort and risk

An MVP supporting one coding-agent adapter, local harness paths, unique artifacts,
and a paired read-only pilot is a small-to-medium change. A robust implementation
covering all three adapters, clean checkout provisioning, CI, provenance, repeated
statistics, and a comparison report is a medium-sized change.

The likely implementation size is approximately 300–500 lines plus tests, depending
on how much duplicated lifecycle code is removed from the three adapters.

The largest risks are experimental rather than mechanical:

- treating v2 safety refusals as capability failures without labeling them;
- silently executing the wrong harness revision;
- allowing repository-specific agent context to differ;
- screenshot contamination under parallel execution;
- target identity loss across fresh v2 invocations;
- drawing conclusions from one live run per task.

None of these is a fundamental blocker. They need to be made explicit and fail-closed
in the benchmark runner.

## Acceptance criteria

The harness-variable work is complete when one command:

1. resolves and validates two pinned harness checkouts;
2. prints and stores one immutable comparison manifest;
3. executes the same task list with the same agent, model, browser configuration,
   limits, and judge for both harnesses;
4. isolates workspaces, browser sessions, screenshots, daemons, and artifacts;
5. records the selected harness and exact revision on every task result;
6. tears down every external resource;
7. produces paired per-task and aggregate results;
8. clearly labels whether the run measures read-only capability or whole-product
   behavior.
