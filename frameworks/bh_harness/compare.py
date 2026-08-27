"""Aggregate `bh-harness` runs into the v1-vs-v2 comparison they exist to produce.

The benchmark's own results file records score, steps, duration and cost per run. Two of
those do not answer the question this adapter was built for: `steps` counts formatted
transcript entries, which mixes the agent's reasoning with the commands it actually ran,
and `cost` is zero for any model absent from the shared price map. The per-task sidecars
this adapter writes keep them apart, and this reads them back.

    uv run python frameworks/bh_harness/compare.py run_data/BU_Bench_V1_framework_bh-*

Pairs are matched on task index, so only tasks both arms attempted are compared. A metric
present for one arm only — v2's harness journal, which v1 has no equivalent of — is
reported per arm and never differenced.
"""

import json
import sys
from collections import defaultdict
from pathlib import Path


def load(paths: list[str]) -> dict[str, dict[int, dict[str, dict]]]:
    """model -> task index -> harness -> metrics, newest run of each triple winning.

    Keyed on the model, not only the task, because the first version of this was not: it
    matched a v2 run of one model against a v1 run of another and reported the difference
    as if the harness had caused it. That is the exact confound the adapter is built to
    remove, so the aggregator must not reintroduce it.

    Newest by file mtime rather than by sorted path, for the same reason the bug was
    possible at all — run directories carry the model in their name, so `gpt-5.6-terra`
    sorts before `gpt-5` and "last one wins" silently meant "whichever name sorts last".
    """
    seen: dict[tuple[str, int, str], tuple[float, dict]] = {}
    for root in paths:
        for path in Path(root).rglob("task_*.metrics.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                stamp = path.stat().st_mtime
            except (OSError, json.JSONDecodeError) as error:
                print(f"skipping {path}: {error}", file=sys.stderr)
                continue
            key = (str(data.get("model", "?")), int(data["task_index"]),
                   str(data["harness"]))
            if key not in seen or stamp > seen[key][0]:
                seen[key] = (stamp, data)

    by_model: dict[str, dict[int, dict[str, dict]]] = defaultdict(lambda: defaultdict(dict))
    for (model, index, arm), (_, data) in seen.items():
        by_model[model][index][arm] = data
    return by_model


def _delta(v2: float, v1: float) -> str:
    if not v1:
        return "n/a"
    return f"{(v2 - v1) / v1:+.0%}"


def main(paths: list[str]) -> int:
    by_model = load(paths)
    comparable = {model: {i: arms for i, arms in sorted(tasks.items())
                          if "v1" in arms and "v2" in arms}
                  for model, tasks in by_model.items()}
    comparable = {model: tasks for model, tasks in comparable.items() if tasks}
    if not comparable:
        print("No task was run through both arms on the same model — nothing to compare.",
              file=sys.stderr)
        return 1
    for model in sorted(comparable):
        _report(model, comparable[model], by_model[model])
    return 0


def _report(model: str, paired: dict[int, dict[str, dict]],
            all_tasks: dict[int, dict[str, dict]]) -> None:
    unpaired = sorted(set(all_tasks) - set(paired))
    print(f"\nmodel {model}")
    print(f"{'task':>5} {'commands v2/v1':>16} {'seconds v2/v1':>18} {'output tok v2/v1':>20}")
    totals = {arm: defaultdict(float) for arm in ("v1", "v2")}
    for index, arms in paired.items():
        row = []
        for key, fmt in (("commands", "{:.0f}"), ("duration_seconds", "{:.0f}"),
                         ("tokens.output", "{:.0f}")):
            values = {}
            for arm in ("v2", "v1"):
                data = arms[arm]
                value = (data["tokens"]["output"] if key == "tokens.output"
                         else float(data[key]))
                values[arm] = value
                totals[arm][key] += value
            row.append(f"{fmt.format(values['v2'])}/{fmt.format(values['v1'])} "
                       f"({_delta(values['v2'], values['v1'])})")
        print(f"{index:>5} {row[0]:>16} {row[1]:>18} {row[2]:>20}")

    print(f"\n{len(paired)} paired task(s)"
          + (f"; {len(unpaired)} run through one arm only: {unpaired}" if unpaired else ""))
    for key, label in (("commands", "commands"), ("duration_seconds", "seconds"),
                       ("tokens.output", "output tokens")):
        v2, v1 = totals["v2"][key], totals["v1"][key]
        print(f"  total {label:<14} v2={v2:>10,.0f}  v1={v1:>10,.0f}  {_delta(v2, v1)}")

    # Scores live in the benchmark's own results file, not here: reliability is the
    # judge's verdict, and inventing a proxy for it would be the easiest way to publish a
    # number that means nothing. n is printed above so a two-task run cannot read as one.
    print("\nReliability is not in this table — read `score` from results/ for that, and\n"
          "note that a judge-less run scores 0 for every task regardless of what happened.")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] or ["run_data"]))
