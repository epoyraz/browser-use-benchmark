#!/usr/bin/env python
"""Run one or both Browser Harness versions with local subscription CLIs.

No model SDK is imported and no model API-key path exists in this runner.  Every task and
optional judgement launches the installed ``codex`` or ``claude`` executable after verifying
subscription-backed auth and removing API credentials from the child environment.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

from frameworks.harness_variants import (
    HarnessValidationError,
    parse_named_values,
    resolve_harnesses,
)
from harness_benchmark.agents import (
    AgentValidationError,
    resolve_agent,
    validate_subscription_auth,
)
from harness_benchmark.browser import BrowserConfig, BrowserLaunchError, find_chromium
from harness_benchmark.runner import (
    ComparisonConfig,
    build_cell_plans,
    build_manifest,
    default_comparison_id,
    load_benchmark_tasks,
    run_comparison,
    select_tasks,
)

ROOT_DIR = Path(__file__).resolve().parent


def _csv_names(raw: str, *, allowed: set[str]) -> list[str]:
    names = [value.strip() for value in raw.split(",") if value.strip()]
    unknown = [name for name in names if name not in allowed]
    if unknown:
        raise ValueError(
            f"Unknown value(s): {', '.join(unknown)}; expected {', '.join(sorted(allowed))}"
        )
    if not names:
        raise ValueError("At least one task agent is required")
    if len(names) != len(set(names)):
        raise ValueError("Task agents may not be repeated")
    return names


def _confirm_risks(args: argparse.Namespace, harnesses) -> None:
    risks: list[str] = []
    for name, spec in harnesses.items():
        if spec.git_sha == "unversioned":
            risks.append(f"{name} is not a pinned git checkout")
        elif spec.dirty:
            risks.append(f"{name} checkout is dirty")
        if spec.expected_git_sha and not spec.git_sha.startswith(
            spec.expected_git_sha.lower()
        ):
            risks.append(
                f"{name} is at {spec.git_sha}, not expected {spec.expected_git_sha}"
            )
    if not risks:
        return
    detail = "\n".join(f"- {risk}" for risk in risks)
    if args.yes:
        print(f"Accepted harness provenance risks:\n{detail}", file=sys.stderr)
        return
    if not sys.stdin.isatty():
        raise ValueError(
            f"Harness provenance requires confirmation:\n{detail}\nPass --yes to accept."
        )
    response = (
        input(f"Harness provenance risks:\n{detail}\nContinue? [y/N] ").strip().lower()
    )
    if response not in {"y", "yes"}:
        raise ValueError("Cancelled before browser/model execution")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Browser Harness benchmark via local subscription CLIs"
        )
    )
    parser.add_argument(
        "--harnesses",
        default="v1=../v1,v2=../v2",
        help="v1=<checkout>, v2=<checkout>, or both",
    )
    parser.add_argument(
        "--expected-shas",
        default="",
        help="Optional expected revisions for the selected harnesses",
    )
    parser.add_argument("--prepare-harnesses", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--allow-unpinned", action="store_true")
    parser.add_argument("--allow-sha-mismatch", action="store_true")
    parser.add_argument(
        "--yes", action="store_true", help="Accept explicitly allowed provenance risks"
    )

    parser.add_argument("--benchmark", default="BU_Bench_V1")
    parser.add_argument(
        "--task-category",
        default="WebBenchREAD",
        help="Task category or 'all'; defaults to the conservative read-only slice",
    )
    parser.add_argument(
        "--tasks", type=int, default=1, help="Tasks after category filtering"
    )
    parser.add_argument("--task-start", type=int, default=0)
    parser.add_argument(
        "--task-indices",
        default="",
        help="Explicit indices in the effective (interleaved by default) task order",
    )
    parser.add_argument("--no-interleave", action="store_true")
    parser.add_argument(
        "--measurement-scope",
        choices=["auto", "read-only-capability", "whole-product"],
        default="auto",
    )

    parser.add_argument("--agents", default="codex,claude")
    parser.add_argument(
        "--codex-only",
        action="store_true",
        help="Fail closed unless every contestant and the judge use the Codex CLI",
    )
    parser.add_argument("--codex-model", default="", help="Empty uses the CLI default")
    parser.add_argument(
        "--codex-effort",
        choices=["none", "low", "medium", "high", "xhigh", "max"],
        default=None,
        help="Codex reasoning effort; empty uses the CLI default",
    )
    parser.add_argument("--claude-model", default="", help="Empty uses the CLI default")
    parser.add_argument(
        "--claude-effort",
        choices=["low", "medium", "high", "xhigh", "max"],
        default=None,
        help="Claude effort; empty uses the CLI default",
    )
    parser.add_argument(
        "--judge",
        choices=["codex", "claude", "none"],
        default="claude",
        help="Fixed local subscription CLI used to score every arm",
    )
    parser.add_argument(
        "--judge-model", default="", help="Empty uses the judge CLI default"
    )
    parser.add_argument(
        "--judge-effort",
        choices=["none", "low", "medium", "high", "xhigh", "max"],
        default=None,
        help="Judge reasoning effort; empty uses the judge CLI default",
    )
    parser.add_argument("--task-timeout", type=float, default=1800)
    parser.add_argument("--judge-timeout", type=float, default=600)
    parser.add_argument("--claude-max-turns", type=int, default=100)
    parser.add_argument(
        "--codex-sandbox",
        choices=["read-only", "workspace-write", "danger-full-access"],
        default="danger-full-access",
    )

    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument(
        "--paired-order",
        choices=["fixed", "alternate", "randomized"],
        default="alternate",
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--execution-mode", choices=["sequential", "parallel"], default="sequential"
    )
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument(
        "--parallel",
        type=int,
        default=None,
        metavar="N",
        help="Compatibility shortcut: 1 is sequential; N>1 is parallel with N task pairs",
    )

    parser.add_argument(
        "--browser-mode",
        choices=["local-headless", "local-headful"],
        default="local-headless",
    )
    parser.add_argument("--chrome-path", default="")
    parser.add_argument(
        "--search-endpoint",
        default="",
        help=(
            "Optional manifest-recorded HTML search URL template containing {query}; "
            "agents must still open it through the selected harness"
        ),
    )
    parser.add_argument(
        "--extensive-telemetry",
        action="store_true",
        help=(
            "Enable v2 action recordings, sanitized CDP tracing, bounded page "
            "diagnostics, and owned-process sampling"
        ),
    )
    parser.add_argument(
        "--process-sample-interval",
        type=float,
        default=1.0,
        help="Seconds between owned-process samples in extensive telemetry mode",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=ROOT_DIR / "run_data" / "harness_comparisons",
    )
    parser.add_argument("--comparison-id", default="")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print the fully resolved manifest without launching a browser/model",
    )
    return parser


def resolve_config(args: argparse.Namespace) -> ComparisonConfig:
    if args.tasks is not None and args.tasks < 1:
        raise ValueError("--tasks must be >= 1")
    if args.repeats < 1:
        raise ValueError("--repeats must be >= 1")
    if args.workers < 1:
        raise ValueError("--workers must be >= 1")
    if args.task_timeout <= 0 or args.judge_timeout <= 0:
        raise ValueError("Timeouts must be positive")
    if args.claude_max_turns < 1:
        raise ValueError("--claude-max-turns must be >= 1")
    if args.process_sample_interval <= 0:
        raise ValueError("--process-sample-interval must be positive")
    if args.search_endpoint and "{query}" not in args.search_endpoint:
        raise ValueError("--search-endpoint must contain the literal {query} placeholder")
    if args.comparison_id and not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", args.comparison_id
    ):
        raise ValueError(
            "--comparison-id must be 1-128 characters using letters, digits, '.', '_', or '-'"
        )

    if args.parallel is not None:
        if args.parallel < 1:
            raise ValueError("--parallel must be >= 1")
        args.execution_mode = "sequential" if args.parallel == 1 else "parallel"
        args.workers = args.parallel

    roots = parse_named_values(args.harnesses, allowed={"v1", "v2"})
    expected = parse_named_values(args.expected_shas, allowed={"v1", "v2"})
    harnesses = resolve_harnesses(
        roots,
        expected_shas=expected,
        prepare=args.prepare_harnesses,
        allow_dirty=args.allow_dirty,
        allow_unpinned=args.allow_unpinned,
        allow_sha_mismatch=args.allow_sha_mismatch,
    )
    _confirm_risks(args, harnesses)

    agent_names = _csv_names(args.agents, allowed={"codex", "claude"})
    if args.codex_only and (
        agent_names != ["codex"] or args.judge != "codex"
    ):
        raise ValueError(
            "--codex-only requires --agents codex and --judge codex"
        )
    models = {
        "codex": args.codex_model or None,
        "claude": args.claude_model or None,
    }
    efforts = {
        "codex": args.codex_effort,
        "claude": args.claude_effort,
    }
    agents = {
        name: resolve_agent(name, models[name], efforts[name]) for name in agent_names
    }
    if args.judge == "claude" and args.judge_effort == "none":
        raise ValueError("Claude does not support --judge-effort none")
    judge = (
        None
        if args.judge == "none"
        else resolve_agent(
            args.judge,
            args.judge_model or None,
            args.judge_effort,
        )
    )
    auth_specs = dict(agents)
    if judge is not None:
        auth_specs.setdefault(judge.name, judge)
    auth_status = {
        name: validate_subscription_auth(spec) for name, spec in auth_specs.items()
    }

    all_tasks = load_benchmark_tasks(args.benchmark, no_interleave=args.no_interleave)
    categories = sorted({task.category for task in all_tasks})
    if args.task_category != "all" and args.task_category not in categories:
        raise ValueError(
            f"Unknown task category {args.task_category!r}; expected all or {', '.join(categories)}"
        )
    tasks = select_tasks(
        all_tasks,
        category=args.task_category,
        task_indices=args.task_indices,
        task_start=args.task_start,
        count=args.tasks,
    )
    if args.measurement_scope == "auto":
        scope = (
            "read-only-capability"
            if all(task.category == "WebBenchREAD" for task in tasks)
            else "whole-product"
        )
    else:
        scope = args.measurement_scope

    executable = find_chromium(args.chrome_path or None)
    browser = BrowserConfig(
        executable=executable,
        headless=args.browser_mode == "local-headless",
    )
    return ComparisonConfig(
        comparison_id=args.comparison_id or default_comparison_id(),
        benchmark=args.benchmark,
        harnesses=harnesses,
        agents=agents,
        judge=judge,
        browser=browser,
        tasks=tasks,
        repeats=args.repeats,
        paired_order=args.paired_order,
        seed=args.seed,
        execution_mode=args.execution_mode,
        workers=args.workers,
        task_timeout_seconds=args.task_timeout,
        judge_timeout_seconds=args.judge_timeout,
        codex_sandbox=args.codex_sandbox,
        claude_max_turns=args.claude_max_turns,
        measurement_scope=scope,
        output_root=args.output_root.expanduser().resolve(),
        auth_status=auth_status,
        no_interleave=args.no_interleave,
        record_actions=args.extensive_telemetry,
        trace_cdp=args.extensive_telemetry,
        capture_diagnostics=args.extensive_telemetry,
        sample_processes=args.extensive_telemetry,
        process_sample_interval_seconds=args.process_sample_interval,
        search_endpoint=args.search_endpoint or None,
    )


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        config = resolve_config(args)
        if args.dry_run:
            manifest = build_manifest(config, build_cell_plans(config))
            print(json.dumps(manifest, indent=2, ensure_ascii=False))
            return 0
        run_dir, report = asyncio.run(run_comparison(config))
        print(
            json.dumps(
                {
                    "comparison_id": report["comparison_id"],
                    "wall_clock_seconds": report["wall_clock_seconds"],
                    "report": str(run_dir / "comparison.md"),
                },
                indent=2,
            )
        )
        return 0
    except (
        ValueError,
        HarnessValidationError,
        AgentValidationError,
        BrowserLaunchError,
    ) as exc:
        parser.error(str(exc))
        return 2
    except KeyboardInterrupt:
        print("Interrupted; in-flight cell cleanup was requested.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
