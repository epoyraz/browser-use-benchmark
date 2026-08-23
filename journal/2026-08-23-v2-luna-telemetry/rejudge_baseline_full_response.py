from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from harness_benchmark.agents import AgentExecution, AgentSpec
from harness_benchmark.judge import judge_execution

TMP = Path(__file__).resolve().parent
BENCH = Path(__file__).resolve().parents[2]
RUNS = BENCH / "run_data" / "harness_comparisons"
INDICES = {5, 11, 22, 23, 44, 56}
OUTPUT = TMP / "baseline-rejudge-v2-luna-full-response.json"
AUDIT_ROOT = BENCH / "run_data" / "rejudge_audits" / "v2-luna-full-response"


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def baseline_cells() -> list[tuple[Path, dict[str, Any]]]:
    selected: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(RUNS.glob("v2-luna-telemetry-20260823-b*/cells/*/result.json")):
        result = load(path)
        index = int(result["provenance"]["benchmark"]["benchmark_index"])
        if index not in INDICES or result["judgement"].get("reached_captcha"):
            continue
        selected.append((path.parent, result))
    by_index = {
        int(result["provenance"]["benchmark"]["benchmark_index"]): (cell, result)
        for cell, result in selected
    }
    missing = sorted(INDICES - set(by_index))
    if missing:
        raise RuntimeError(f"Missing baseline cells: {missing}")
    return [by_index[index] for index in sorted(by_index)]


async def main() -> None:
    cells = baseline_cells()
    manifest = load(cells[0][0].parents[1] / "manifest.json")
    judge = manifest["judge"]
    if judge["name"] != "codex" or judge["model"] != "gpt-5.6-luna":
        raise RuntimeError("Refusing to rejudge with anything except Codex gpt-5.6-luna")
    spec = AgentSpec(
        name="codex",
        cli=Path(judge["cli"]),
        version=str(judge["cli_version"]),
        model="gpt-5.6-luna",
        reasoning_effort="max",
    )
    audit_root = AUDIT_ROOT
    prior = load(OUTPUT) if OUTPUT.is_file() else {}
    results = {
        int(row["task_index"]): row for row in prior.get("results", [])
        if int(row["task_index"]) in INDICES
    }
    for cell, result in cells:
        trace = load(cell / "trace.json")
        execution = AgentExecution(**result["execution"])
        screenshots = [cell / path for path in result.get("screenshots") or []]
        index = int(result["provenance"]["benchmark"]["benchmark_index"])
        if index in results:
            print(f"task {index}: reused prior full-response audit", flush=True)
            continue
        judgement = await judge_execution(
            spec,
            task_description=trace["task"]["description"],
            ground_truth=trace["task"].get("ground_truth"),
            execution=execution,
            screenshots=screenshots,
            cell_dir=audit_root / f"task-{index:03d}",
            env=os.environ,
            timeout_seconds=600,
        )
        results[index] = {
            "task_index": index,
            "category": result["provenance"]["benchmark"]["category"],
            "original_score": result.get("score"),
            "score": judgement.score,
            "judgement": judgement.to_json(),
        }
        print(f"task {index}: {result.get('score')} -> {judgement.score}", flush=True)
    OUTPUT.write_text(
        json.dumps(
            {
                "generated_at": datetime.now().astimezone().isoformat(),
                "judge": {"name": "codex", "model": "gpt-5.6-luna", "effort": "max"},
                "input_contract": "full final response plus compact marker",
                "results": [results[index] for index in sorted(results)],
            },
            indent=2,
        ),
        encoding="utf-8",
        newline="\n",
    )
    print(OUTPUT)


if __name__ == "__main__":
    asyncio.run(main())
