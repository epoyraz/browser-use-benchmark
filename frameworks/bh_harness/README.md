# bh-harness — browser-harness v2 against v1, on one variable

Every other adapter in this repo benchmarks a whole agent stack. This one holds the stack
fixed and moves a single part, to answer one question:

> Does v2 need fewer model calls than v1 for the same task, at the same or better
> reliability and speed?

Same Codex CLI, same model, same task, same judge, **same browser** — only `harness=v1|v2`
differs.

## Why v1 is reachable from here

`codex-harness` already drives v1, but against **browser-use-cloud**. Comparing that with a
local v2 run would confound the harness with the browser it talks to. Both arms here attach
to one scratch Chrome launched per task and pinned with `BU_CDP_WS`, the env var v1 and v2
already honour as an explicit endpoint. Verified: both attach to the same pinned browser.

The Codex event schema, price map and step formatting are **imported** from
`frameworks/codex_harness/run_task.py`, never copied — a difference in reported steps must
mean the agent behaved differently, not that we parsed its output differently.

## Prerequisites

- Codex CLI on `PATH`, authenticated (`~/.codex/auth.json`) or `CODEX_API_KEY`.
- v2 and v1 checked out with their venvs (`uv sync` in each), so `bh` and
  `browser-harness` exist as console scripts. Override locations with `harness_dir` /
  `harness_v1_dir`.
- Google Chrome. Nothing is provisioned remotely and no browser-provider key is used.
- `GOOGLE_API_KEY` for the shared judge. **Without it every task scores 0** whatever
  happened, because `run_and_judge` treats the judge failure as a task failure. The run
  still executes and every metric is still written — see below.
- A model your Codex auth actually serves. A ChatGPT-account login refuses `gpt-5` with
  `invalid_request_error`; the run then reports zero commands, which reads exactly like a
  harness that did nothing.

## Running it

```bash
uv run python run_framework_eval.py --framework bh-harness --model <model> \
    --task-indices 0 --params harness=v2
uv run python run_framework_eval.py --framework bh-harness --model <model> \
    --task-indices 0 --params harness=v1
uv run python frameworks/bh_harness/compare.py run_data
```

`--parallel` is safe: each task gets its own Chrome profile, daemon name and screenshot
directory. (`codex-harness` drains a single `/tmp/shots`, which mixes traces under
`--parallel` — and the judge sees those images.)

Useful params: `headless=1`, `keep_profile=1`, `sandbox=`, `task_timeout=`.

## What gets measured, and where

The benchmark's own `results/` file carries **score**, and that is where reliability lives.

Its `steps` figure counts formatted transcript entries, mixing reasoning with commands, and
`cost` is zero for any model absent from the shared price map. So each task also writes
`run_data/<run>/task_<n>.<arm>.metrics.json`:

| field | meaning |
| --- | --- |
| `commands` | shell invocations — how many times the agent had to go back to the browser |
| `agent_turns`, `agent_messages`, `reasoning_items` | model output events |
| `tokens` | input / cached / output / reasoning |
| `duration_seconds` | wall clock for the agent process |
| `harness_journal` | **v2 only** — its own call/CDP counts. Never differenced against v1, which has no equivalent |

The sidecar exists because `run_and_judge` zeroes `steps` and `duration` in the results file
when anything downstream of execution fails, including a missing judge key. The execution
happened; the summary just stops describing it.

## Reading the output honestly

`compare.py` pairs on `(model, task index)` and reports only tasks both arms attempted. It
prints `n`, because a handful of tasks is a smoke test and not a result — the two arms can
return different answers on the same task, and without a judge neither is scored.
