"""Paired Browser Harness v1/v2 orchestration and comparison reporting."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import random
import re
import shutil
import statistics
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from cryptography.fernet import Fernet

from frameworks.harness_variants import HarnessSpec

from .agents import AgentExecution, AgentSpec, run_agent
from .browser import BrowserConfig, LocalChromiumSession
from .judge import judge_execution, judge_manifest
from .process import (
    process_group_kwargs,
    run_captured,
    subscription_only_env,
    terminate_process_tree,
)

ROOT_DIR = Path(__file__).resolve().parent.parent
PROMPT_TEMPLATE_PATH = Path(__file__).resolve().parent / "prompts" / "agent.md"
WORKSPACE_OVERRIDE_PATH = (
    Path(__file__).resolve().parent / "prompts" / "AGENTS.override.md"
)
RESULT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class BenchmarkTask:
    benchmark_index: int
    raw_index: int
    task_id: str
    category: str
    description: str
    ground_truth: object = None

    def public_manifest(self) -> dict[str, object]:
        return {
            "benchmark_index": self.benchmark_index,
            "raw_index": self.raw_index,
            "task_id": self.task_id,
            "category": self.category,
        }


@dataclass(frozen=True)
class CellPlan:
    pair_id: str
    cell_id: str
    pair_ordinal: int
    order: int
    repetition: int
    agent_name: str
    harness_name: str
    task: BenchmarkTask

    def public_manifest(self) -> dict[str, object]:
        return {
            "pair_id": self.pair_id,
            "cell_id": self.cell_id,
            "pair_ordinal": self.pair_ordinal,
            "order": self.order,
            "repetition": self.repetition,
            "agent": self.agent_name,
            "harness": self.harness_name,
            "task_index": self.task.benchmark_index,
            "task_id": self.task.task_id,
            "category": self.task.category,
        }


@dataclass(frozen=True)
class ComparisonConfig:
    comparison_id: str
    benchmark: str
    harnesses: dict[str, HarnessSpec]
    agents: dict[str, AgentSpec]
    judge: AgentSpec | None
    browser: BrowserConfig
    tasks: list[BenchmarkTask]
    repeats: int
    paired_order: str
    seed: int
    execution_mode: str
    workers: int
    task_timeout_seconds: float
    judge_timeout_seconds: float
    codex_sandbox: str
    claude_max_turns: int
    measurement_scope: str
    output_root: Path
    auth_status: dict[str, dict[str, object]]
    no_interleave: bool = False


def _encrypted_task_file(benchmark: str) -> Path:
    candidates = [
        ROOT_DIR / f"{benchmark}.enc",
        ROOT_DIR / f"{benchmark.upper()}.enc",
        ROOT_DIR / "benchmarks" / f"{benchmark}.enc",
    ]
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError(
        f"Could not find encrypted benchmark artifact for {benchmark}"
    )


def load_benchmark_tasks(
    benchmark: str, *, no_interleave: bool = False
) -> list[BenchmarkTask]:
    path = _encrypted_task_file(benchmark)
    key = base64.urlsafe_b64encode(hashlib.sha256(benchmark.encode()).digest())
    raw: list[dict[str, Any]] = json.loads(
        Fernet(key).decrypt(base64.b64decode(path.read_text()))
    )
    indexed = [(index, task) for index, task in enumerate(raw)]
    if not no_interleave and len(indexed) == 100:
        indexed = [
            indexed[group * 20 + offset] for offset in range(20) for group in range(5)
        ]
    return [
        BenchmarkTask(
            benchmark_index=benchmark_index,
            raw_index=raw_index,
            task_id=str(task.get("task_id") or f"task_{raw_index}"),
            category=str(task.get("category") or "unknown"),
            description=str(task["confirmed_task"]),
            ground_truth=task.get("answer"),
        )
        for benchmark_index, (raw_index, task) in enumerate(indexed)
    ]


def select_tasks(
    tasks: list[BenchmarkTask],
    *,
    category: str,
    task_indices: str,
    task_start: int,
    count: int | None,
) -> list[BenchmarkTask]:
    by_index = {task.benchmark_index: task for task in tasks}
    if task_indices.strip():
        requested = [
            int(value.strip()) for value in task_indices.split(",") if value.strip()
        ]
        if len(requested) != len(set(requested)):
            raise ValueError("--task-indices may not contain duplicates")
        missing = [index for index in requested if index not in by_index]
        if missing:
            raise ValueError(f"Task index out of range: {missing[:5]}")
        selected = [by_index[index] for index in requested]
        if category != "all":
            wrong = [
                task.benchmark_index for task in selected if task.category != category
            ]
            if wrong:
                raise ValueError(
                    f"Explicit task indices {wrong} do not belong to requested category {category}"
                )
        return selected

    eligible = (
        tasks
        if category == "all"
        else [task for task in tasks if task.category == category]
    )
    if task_start < 0:
        raise ValueError("--task-start must be >= 0")
    end = None if count is None else task_start + count
    selected = eligible[task_start:end]
    if not selected:
        raise ValueError("Task selection is empty")
    return selected


def default_comparison_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"bh-ab-{stamp}-{uuid.uuid4().hex[:8]}"


def _safe_part(value: str, limit: int = 48) -> str:
    return (re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-") or "unknown")[:limit]


def ordered_harnesses(
    policy: str, *, pair_ordinal: int, repetition: int, seed: int
) -> list[str]:
    if policy == "fixed":
        return ["v1", "v2"]
    if policy == "alternate":
        return ["v1", "v2"] if pair_ordinal % 2 == 0 else ["v2", "v1"]
    if policy == "randomized":
        digest = hashlib.sha256(f"{seed}:{pair_ordinal}:{repetition}".encode()).digest()
        rng = random.Random(int.from_bytes(digest[:8], "big"))
        values = ["v1", "v2"]
        rng.shuffle(values)
        return values
    raise ValueError(f"Unknown paired order: {policy}")


def build_cell_plans(config: ComparisonConfig) -> list[list[CellPlan]]:
    pairs: list[list[CellPlan]] = []
    pair_ordinal = 0
    for agent_name in config.agents:
        for repetition in range(config.repeats):
            for task in config.tasks:
                pair_id = f"{_safe_part(agent_name)}-r{repetition:02d}-t{task.benchmark_index:03d}"
                order = ordered_harnesses(
                    config.paired_order,
                    pair_ordinal=pair_ordinal,
                    repetition=repetition,
                    seed=config.seed,
                )
                pairs.append(
                    [
                        CellPlan(
                            pair_id=pair_id,
                            cell_id=f"{pair_id}-o{position}-{harness_name}",
                            pair_ordinal=pair_ordinal,
                            order=position,
                            repetition=repetition,
                            agent_name=agent_name,
                            harness_name=harness_name,
                            task=task,
                        )
                        for position, harness_name in enumerate(order)
                    ]
                )
                pair_ordinal += 1
    return pairs


def _system_prompt(harness: HarnessSpec, screenshot_dir: Path) -> str:
    template = PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")
    example = harness.screenshot_example.format(
        path=repr(str(screenshot_dir / "step_001.png"))
    )
    return template.format(
        harness_command=harness.command_name,
        screenshot_dir=str(screenshot_dir),
        screenshot_example=example,
    )


def _atomic_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    os.replace(temporary, path)


def _codex_ambient_instruction_manifest() -> dict[str, object]:
    codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    for filename in ("AGENTS.override.md", "AGENTS.md"):
        source = codex_home / filename
        try:
            payload = source.read_bytes()
        except OSError:
            continue
        if payload.strip():
            return {
                "present": True,
                "source_name": filename,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "superseded_for_browser_instructions": True,
            }
    return {
        "present": False,
        "source_name": None,
        "sha256": None,
        "superseded_for_browser_instructions": True,
    }


class ManagedHarnessDaemon:
    """A foreground daemon owned by the cell, so teardown always has an exact PID."""

    def __init__(
        self, harness: HarnessSpec, name: str, cell_dir: Path, env: Mapping[str, str]
    ):
        self.harness = harness
        self.name = name
        self.cell_dir = cell_dir
        self.env = dict(env)
        self.proc: asyncio.subprocess.Process | None = None
        self.log_path = cell_dir / "harness-daemon.log"
        self._log_handle: IO[bytes] | None = None

    async def start(self) -> None:
        self._log_handle = self.log_path.open("wb")
        self.proc = await asyncio.create_subprocess_exec(
            *self.harness.daemon_command(self.name),
            cwd=str(self.cell_dir / "workspace"),
            env=subscription_only_env(self.env),
            stdout=self._log_handle,
            stderr=self._log_handle,
            **process_group_kwargs(),
        )
        # Do not let the first CLI prewarm race the foreground daemon.  Both harnesses
        # auto-spawn when no endpoint exists; invoking immediately can therefore create a
        # second detached daemon, overwrite the port file, and strand the PID we own.
        runtime_dir = Path(self.env["BH_RUNTIME_DIR"])
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.proc.returncode is not None:
                raise RuntimeError(
                    f"Harness daemon exited with {self.proc.returncode}; see {self.log_path}"
                )
            endpoints = [
                path
                for pattern in ("*.port", "*.sock")
                for path in runtime_dir.glob(pattern)
                if path.exists()
            ]
            if endpoints:
                return
            await asyncio.sleep(0.05)
        raise RuntimeError(
            f"Harness daemon did not publish an endpoint; see {self.log_path}"
        )

    async def finish(self, runtime_dir: Path, *, timeout: float = 15.0) -> list[str]:
        errors: list[str] = []
        if self.proc is not None and self.proc.returncode is None:
            # The daemon is deliberately a foreground child in its own process group.
            # Stopping that exact tree is deterministic on both harness versions and
            # avoids v1's `--reload` Windows wait on a child handle retained by us.
            await terminate_process_tree(self.proc, grace_seconds=min(timeout, 5.0))
        if self.proc is not None and self.proc.returncode is None:
            errors.append("owned daemon process did not terminate")
        remaining = [
            path
            for pattern in (
                "*.port",
                "*.port.tmp",
                "*.sock",
                "*.pid",
                "*.token",
                "*.binding",
            )
            for path in runtime_dir.glob(pattern)
            if path.exists()
        ]
        for endpoint in remaining:
            try:
                endpoint.unlink()
            except OSError as exc:
                errors.append(
                    f"could not remove daemon endpoint {endpoint.name}: {exc}"
                )
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None
        return errors


def _classification(
    execution: AgentExecution,
    *,
    setup_error: str | None,
    teardown_errors: list[str],
) -> str | None:
    if setup_error:
        lower = setup_error.lower()
        if "chrom" in lower or "cdp" in lower:
            return "browser_setup"
        if "daemon" in lower or "prewarm" in lower:
            return "harness_setup"
        return "setup"
    if execution.timed_out:
        return "agent_timeout"
    if execution.error:
        lower = execution.error.lower()
        if "auth" in lower or "login" in lower:
            return "agent_auth"
        return "agent_cli"
    if teardown_errors:
        return "cleanup"
    return None


_SAFETY_PATTERNS = (
    re.compile(r"dry[- ]run", re.IGNORECASE),
    re.compile(r"(?:blocked|refused).{0,80}(?:submit|mutation|request)", re.IGNORECASE),
    re.compile(r"no[- ]submit", re.IGNORECASE),
)


def _safety_refusal(execution: AgentExecution) -> bool:
    text = "\n".join([execution.final_result, *execution.steps])
    return any(pattern.search(text) for pattern in _SAFETY_PATTERNS)


async def _run_cell(
    config: ComparisonConfig, plan: CellPlan, run_dir: Path
) -> dict[str, Any]:
    total_started = time.perf_counter()
    harness = config.harnesses[plan.harness_name]
    agent = config.agents[plan.agent_name]
    cell_dir = run_dir / "cells" / plan.cell_id
    workspace = cell_dir / "workspace"
    screenshots_dir = workspace / "screenshots"
    runtime_dir = cell_dir / "runtime"
    temp_dir = cell_dir / "tmp"
    for directory in (workspace, screenshots_dir, runtime_dir, temp_dir):
        directory.mkdir(parents=True, exist_ok=True)
    shutil.copy2(harness.skill, workspace / "SKILL.md")
    shutil.copy2(WORKSPACE_OVERRIDE_PATH, workspace / "AGENTS.override.md")
    (workspace / "agent_helpers.py").write_text(
        '"""Intentionally empty: identical isolated helper surface for both harness arms."""\n',
        encoding="utf-8",
    )
    system_prompt = _system_prompt(harness, screenshots_dir)
    (workspace / "benchmark-system-prompt.md").write_text(
        system_prompt, encoding="utf-8"
    )

    daemon_name = _safe_part(
        f"{config.comparison_id[-8:]}-{plan.harness_name}-{plan.agent_name[:2]}-"
        f"r{plan.repetition}-t{plan.task.benchmark_index}",
        limit=64,
    )
    path_value = os.pathsep.join([str(harness.cli.parent), os.environ.get("PATH", "")])
    harness_env: dict[str, str] = {
        "PATH": path_value,
        "BU_NAME": daemon_name,
        "BH_RUNTIME_DIR": str(runtime_dir),
        "BH_TMP_DIR": str(temp_dir),
        "BH_AGENT_WORKSPACE": str(workspace),
        # Keep v1's optional saved Browser Use cloud credential store outside the
        # benchmark. The file is intentionally absent, so even an accidental cloud
        # administration command cannot recover a globally saved provider key.
        "BH_AUTH_PATH": str(cell_dir / "browser-harness-auth.json"),
        "BH_JOURNAL": str(cell_dir / "harness-journal.jsonl"),
        "BH_JOURNAL_DIR": str(cell_dir),
        "BH_RECORD": "0",
        "BH_DOMAIN_SKILLS": "0",
        "BH_TELEMETRY": "0",
        "BROWSER_HARNESS_TELEMETRY": "0",
        "ANONYMIZED_TELEMETRY": "0",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }
    browser = LocalChromiumSession(config.browser, cell_dir)
    daemon = ManagedHarnessDaemon(harness, daemon_name, cell_dir, harness_env)
    execution = AgentExecution(error="Agent did not start")
    setup_error: str | None = None
    teardown_errors: list[str] = []
    setup_started = time.perf_counter()
    setup_duration = 0.0
    teardown_duration = 0.0
    browser_started = False
    daemon_started = False
    prewarm: dict[str, object] = {}

    try:
        cdp_url = await browser.start()
        browser_started = True
        harness_env["BU_CDP_URL"] = cdp_url
        daemon.env.update(harness_env)
        await daemon.start()
        daemon_started = True
        code, stdout, stderr, timed_out = await run_captured(
            [str(harness.cli)],
            cwd=workspace,
            env=subscription_only_env(harness_env),
            input_text=harness.prewarm_script,
            timeout=60,
        )
        prewarm = {
            "return_code": code,
            "timed_out": timed_out,
            "stdout": stdout[-2_000:],
            "stderr": stderr[-2_000:],
        }
        if code != 0 or timed_out:
            raise RuntimeError(
                f"Harness prewarm failed ({code}): {(stderr or stdout)[-1_000:]}"
            )
        setup_duration = time.perf_counter() - setup_started
        execution = await run_agent(
            agent,
            task_description=plan.task.description,
            system_prompt=system_prompt,
            workspace=workspace,
            env=harness_env,
            timeout_seconds=config.task_timeout_seconds,
            codex_sandbox=config.codex_sandbox,
            claude_max_turns=config.claude_max_turns,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - setup failures become cell result data
        setup_duration = time.perf_counter() - setup_started
        setup_error = f"{type(exc).__name__}: {exc}"
        execution = AgentExecution(error=setup_error)
    finally:
        teardown_started = time.perf_counter()
        if daemon_started or daemon.proc is not None:
            try:
                teardown_errors.extend(await daemon.finish(runtime_dir))
            except Exception as exc:  # noqa: BLE001 - continue with browser cleanup
                teardown_errors.append(f"daemon stop: {type(exc).__name__}: {exc}")
        if browser_started or browser.proc is not None:
            try:
                await browser.stop()
            except Exception as exc:  # noqa: BLE001 - preserve the primary cell result
                teardown_errors.append(f"browser stop: {type(exc).__name__}: {exc}")
        teardown_duration = time.perf_counter() - teardown_started

    screenshots = sorted(
        path for path in screenshots_dir.glob("*.png") if path.is_file()
    )
    judgement = await judge_execution(
        config.judge,
        task_description=plan.task.description,
        ground_truth=plan.task.ground_truth,
        execution=execution,
        screenshots=screenshots,
        cell_dir=cell_dir,
        env=harness_env,
        timeout_seconds=config.judge_timeout_seconds,
    )
    score = judgement.score
    score_override_reason: str | None = None
    technical_failure = _classification(
        execution, setup_error=setup_error, teardown_errors=teardown_errors
    )
    if technical_failure and score == 1:
        score = 0
        score_override_reason = f"Technical failure: {technical_failure}"
    if execution.web_searches and score == 1:
        score = 0
        score_override_reason = "Agent bypassed the selected harness with a web search"

    total_duration = time.perf_counter() - total_started
    result: dict[str, Any] = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "comparison_id": config.comparison_id,
        "pair_id": plan.pair_id,
        "cell_id": plan.cell_id,
        "score": score,
        "score_override_reason": score_override_reason,
        "provenance": {
            "harness": harness.to_manifest(),
            "agent": agent.to_manifest(),
            "browser": {
                **config.browser.to_manifest(),
                "version": browser.version,
            },
            "judge": judge_manifest(config.judge),
            "benchmark": {
                "name": config.benchmark,
                **plan.task.public_manifest(),
                "repetition": plan.repetition,
                "order": plan.order,
                "measurement_scope": config.measurement_scope,
            },
        },
        "timing": {
            "setup_seconds": setup_duration,
            "agent_seconds": execution.duration_seconds,
            "teardown_seconds": teardown_duration,
            "judge_seconds": judgement.duration_seconds,
            "total_seconds": total_duration,
        },
        "metrics": {
            "agent_turns": execution.turns,
            "command_executions": execution.command_executions,
            "tokens": execution.tokens,
            "screenshot_count": len(screenshots),
            "reported_cost_usd": execution.reported_cost_usd,
            "cost_basis": "CLI-reported estimate only; model ran on subscription",
            "safety_refusal": _safety_refusal(execution),
            "technical_failure_class": technical_failure,
            "harness_bypass_web_searches": execution.web_searches,
        },
        "execution": execution.to_json(),
        "judgement": judgement.to_json(),
        "setup_error": setup_error,
        "teardown_errors": teardown_errors,
        "prewarm": prewarm,
        "screenshots": [str(path.relative_to(cell_dir)) for path in screenshots],
    }
    trace = {
        "warning": "Contains decrypted benchmark task content; do not publish.",
        "task": {
            **plan.task.public_manifest(),
            "description": plan.task.description,
            "ground_truth": plan.task.ground_truth,
        },
        "final_result": execution.final_result,
        "steps": execution.steps,
    }
    _atomic_json(cell_dir / "trace.json", trace)
    _atomic_json(cell_dir / "result.json", result)
    score_text = "unjudged" if score is None else str(score)
    print(
        f"[{plan.cell_id}] score={score_text} agent={execution.duration_seconds:.1f}s "
        f"total={total_duration:.1f}s",
        flush=True,
    )
    return result


def build_manifest(
    config: ComparisonConfig, pairs: list[list[CellPlan]]
) -> dict[str, Any]:
    prompt_digest = hashlib.sha256(PROMPT_TEMPLATE_PATH.read_bytes()).hexdigest()
    workspace_override_digest = hashlib.sha256(
        WORKSPACE_OVERRIDE_PATH.read_bytes()
    ).hexdigest()
    uses_codex = "codex" in config.agents or (
        config.judge is not None and config.judge.name == "codex"
    )
    codex_ambient = _codex_ambient_instruction_manifest() if uses_codex else None
    fixed_axes = {
        "benchmark": config.benchmark,
        "task_ids": [task.task_id for task in config.tasks],
        "agents": {name: spec.to_manifest() for name, spec in config.agents.items()},
        "browser": config.browser.to_manifest(),
        "judge": judge_manifest(config.judge),
        "limits": {
            "task_timeout_seconds": config.task_timeout_seconds,
            "judge_timeout_seconds": config.judge_timeout_seconds,
            "claude_max_turns": config.claude_max_turns,
            "codex_sandbox": config.codex_sandbox,
            "codex_shell_environment": "inherit-all-after-api-credential-stripping",
            "codex_forced_login_method": "chatgpt",
            "codex_web_search": "disabled",
            "ambient_browser_instructions": "superseded-by-fixed-workspace-and-developer-policy",
            "subagents": "disabled",
        },
        "prompt_template_sha256": prompt_digest,
        "workspace_instruction_override_sha256": workspace_override_digest,
        "codex_ambient_instruction": codex_ambient,
    }
    fixed_digest = hashlib.sha256(
        json.dumps(fixed_axes, sort_keys=True, default=str).encode()
    ).hexdigest()
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "comparison_id": config.comparison_id,
        "created_at": datetime.now(UTC).isoformat(),
        "api_policy": {
            "task_agents": "local Codex CLI and/or local Claude CLI only",
            "judge": "local subscription CLI or none",
            "model_api_keys_removed_from_children": True,
            "browser_provider_credentials_removed_from_children": True,
            "browser_provider_cloud_selectors_removed_from_children": True,
            "browser_provider_saved_auth_isolated_per_cell": True,
            "browser_provider_api_used": False,
        },
        "measurement_scope": config.measurement_scope,
        "benchmark": {
            "name": config.benchmark,
            "interleaved": not config.no_interleave,
            "tasks": [task.public_manifest() for task in config.tasks],
        },
        "harnesses": {
            name: spec.to_manifest() for name, spec in config.harnesses.items()
        },
        "agents": {name: spec.to_manifest() for name, spec in config.agents.items()},
        "auth": config.auth_status,
        "browser": config.browser.to_manifest(),
        "judge": judge_manifest(config.judge),
        "execution": {
            "mode": config.execution_mode,
            "workers": 1 if config.execution_mode == "sequential" else config.workers,
            "parallel_unit": "independent task pair",
            "pair_arms_overlap": False,
            "repeats": config.repeats,
            "paired_order": config.paired_order,
            "random_seed": config.seed,
            "task_timeout_seconds": config.task_timeout_seconds,
            "judge_timeout_seconds": config.judge_timeout_seconds,
        },
        "fixed_axes": fixed_axes,
        "fixed_axes_sha256": fixed_digest,
        "cells": [cell.public_manifest() for pair in pairs for cell in pair],
        "warnings": [
            "run_data contains decrypted task text and screenshots; do not publish it",
            *(
                [
                    (
                        "Codex may load a global AGENTS file; its digest is recorded, and "
                        "the fixed workspace AGENTS.override plus developer instructions "
                        "supersede browser-specific personal guidance."
                    )
                ]
                if codex_ambient and codex_ambient["present"]
                else []
            ),
            *(
                [
                    (
                        "This is a whole-product comparison: v2 safety refusals are observed "
                        "product behavior, not isolated browser-capability failures."
                    )
                ]
                if config.measurement_scope == "whole-product"
                else []
            ),
        ],
    }


def _mean(values: list[float]) -> float | None:
    return statistics.mean(values) if values else None


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def build_comparison_report(
    config: ComparisonConfig,
    results: list[dict[str, Any]],
    *,
    wall_clock_seconds: float,
) -> dict[str, Any]:
    aggregates: list[dict[str, Any]] = []
    for agent_name in config.agents:
        for harness_name in ("v1", "v2"):
            group = [
                result
                for result in results
                if result["provenance"]["agent"]["name"] == agent_name
                and result["provenance"]["harness"]["name"] == harness_name
            ]
            judged = [result for result in group if result.get("score") in (0, 1)]
            agent_times = [float(result["timing"]["agent_seconds"]) for result in group]
            total_times = [float(result["timing"]["total_seconds"]) for result in group]
            token_totals: dict[str, int] = {}
            for result in group:
                for key, value in result["metrics"].get("tokens", {}).items():
                    if isinstance(value, int):
                        token_totals[key] = token_totals.get(key, 0) + value
            aggregates.append(
                {
                    "agent": agent_name,
                    "harness": harness_name,
                    "runs": len(group),
                    "judged_runs": len(judged),
                    "passes": sum(int(result["score"]) for result in judged),
                    "pass_rate": (
                        sum(int(result["score"]) for result in judged) / len(judged)
                        if judged
                        else None
                    ),
                    "mean_agent_seconds": _mean(agent_times),
                    "median_agent_seconds": _median(agent_times),
                    "mean_total_seconds": _mean(total_times),
                    "mean_agent_turns": _mean(
                        [float(result["metrics"]["agent_turns"]) for result in group]
                    ),
                    "mean_command_executions": _mean(
                        [
                            float(result["metrics"]["command_executions"])
                            for result in group
                        ]
                    ),
                    "mean_screenshot_count": _mean(
                        [
                            float(result["metrics"]["screenshot_count"])
                            for result in group
                        ]
                    ),
                    "token_totals": token_totals,
                    "technical_failures": sum(
                        1
                        for result in group
                        if result["metrics"].get("technical_failure_class")
                    ),
                    "safety_refusals": sum(
                        1 for result in group if result["metrics"].get("safety_refusal")
                    ),
                    "captcha_runs": sum(
                        1
                        for result in group
                        if result["judgement"].get("reached_captcha")
                    ),
                    "impossible_tasks": sum(
                        1
                        for result in group
                        if result["judgement"].get("impossible_task")
                    ),
                }
            )

    paired: dict[str, dict[str, int]] = {}
    paired_timing: dict[str, dict[str, float | int | None]] = {}
    for agent_name in config.agents:
        counts = {
            "v1_pass_v2_pass": 0,
            "v1_pass_v2_fail": 0,
            "v1_fail_v2_pass": 0,
            "v1_fail_v2_fail": 0,
            "unjudged_pairs": 0,
        }
        pair_ids = {
            result["pair_id"]
            for result in results
            if result["provenance"]["agent"]["name"] == agent_name
        }
        agent_deltas: list[float] = []
        total_deltas: list[float] = []
        for pair_id in pair_ids:
            arm_results = {
                result["provenance"]["harness"]["name"]: result
                for result in results
                if result["pair_id"] == pair_id
            }
            if set(arm_results) == {"v1", "v2"}:
                agent_deltas.append(
                    float(arm_results["v2"]["timing"]["agent_seconds"])
                    - float(arm_results["v1"]["timing"]["agent_seconds"])
                )
                total_deltas.append(
                    float(arm_results["v2"]["timing"]["total_seconds"])
                    - float(arm_results["v1"]["timing"]["total_seconds"])
                )
            arms = {name: result.get("score") for name, result in arm_results.items()}
            if arms.get("v1") not in (0, 1) or arms.get("v2") not in (0, 1):
                counts["unjudged_pairs"] += 1
                continue
            left = "pass" if arms["v1"] == 1 else "fail"
            right = "pass" if arms["v2"] == 1 else "fail"
            counts[f"v1_{left}_v2_{right}"] += 1
        paired[agent_name] = counts
        paired_timing[agent_name] = {
            "pairs": len(agent_deltas),
            "mean_agent_seconds_delta_v2_minus_v1": _mean(agent_deltas),
            "median_agent_seconds_delta_v2_minus_v1": _median(agent_deltas),
            "mean_total_seconds_delta_v2_minus_v1": _mean(total_deltas),
        }

    per_task = [
        {
            "pair_id": result["pair_id"],
            "cell_id": result["cell_id"],
            "agent": result["provenance"]["agent"]["name"],
            "harness": result["provenance"]["harness"]["name"],
            "task_index": result["provenance"]["benchmark"]["benchmark_index"],
            "task_id": result["provenance"]["benchmark"]["task_id"],
            "repetition": result["provenance"]["benchmark"]["repetition"],
            "score": result.get("score"),
            "setup_seconds": result["timing"]["setup_seconds"],
            "agent_seconds": result["timing"]["agent_seconds"],
            "teardown_seconds": result["timing"]["teardown_seconds"],
            "judge_seconds": result["timing"]["judge_seconds"],
            "total_seconds": result["timing"]["total_seconds"],
            "agent_turns": result["metrics"].get("agent_turns"),
            "command_executions": result["metrics"].get("command_executions"),
            "tokens": result["metrics"].get("tokens"),
            "screenshot_count": result["metrics"].get("screenshot_count"),
            "technical_failure_class": result["metrics"].get("technical_failure_class"),
            "safety_refusal": result["metrics"].get("safety_refusal"),
            "reached_captcha": result["judgement"].get("reached_captcha"),
            "impossible_task": result["judgement"].get("impossible_task"),
        }
        for result in results
    ]
    return {
        "comparison_id": config.comparison_id,
        "measurement_scope": config.measurement_scope,
        "wall_clock_seconds": wall_clock_seconds,
        "cells_completed": len(results),
        "aggregate": aggregates,
        "paired_outcomes": paired,
        "paired_timing": paired_timing,
        "per_task": per_task,
    }


def _report_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Browser Harness comparison `{report['comparison_id']}`",
        "",
        f"Scope: **{report['measurement_scope']}**  ",
        f"Wall clock: **{report['wall_clock_seconds']:.1f}s**  ",
        f"Cells completed: **{report['cells_completed']}**",
        "",
        "| Agent | Harness | Passes | Judged | Pass rate | Mean agent time | Mean total time |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in report["aggregate"]:
        rate = "n/a" if row["pass_rate"] is None else f"{100 * row['pass_rate']:.1f}%"
        agent_time = (
            "n/a"
            if row["mean_agent_seconds"] is None
            else f"{row['mean_agent_seconds']:.1f}s"
        )
        total_time = (
            "n/a"
            if row["mean_total_seconds"] is None
            else f"{row['mean_total_seconds']:.1f}s"
        )
        lines.append(
            f"| {row['agent']} | {row['harness']} | {row['passes']} | "
            f"{row['judged_runs']} | {rate} | {agent_time} | {total_time} |"
        )
    lines.extend(["", "## Paired outcomes", ""])
    for agent, counts in report["paired_outcomes"].items():
        lines.append(
            f"- {agent}: "
            + ", ".join(f"{key}={value}" for key, value in counts.items())
        )
    lines.extend(["", "## Paired timing", ""])
    for agent, timing in report["paired_timing"].items():
        agent_delta = timing["mean_agent_seconds_delta_v2_minus_v1"]
        total_delta = timing["mean_total_seconds_delta_v2_minus_v1"]
        rendered_agent = "n/a" if agent_delta is None else f"{agent_delta:+.1f}s"
        rendered_total = "n/a" if total_delta is None else f"{total_delta:+.1f}s"
        lines.append(
            f"- {agent}: v2-v1 mean agent time {rendered_agent}; "
            f"mean total time {rendered_total} ({timing['pairs']} pairs)"
        )
    lines.extend(
        [
            "",
            "## Per-task results",
            "",
            "| Task | Rep | Agent | Harness | Score | Agent time | Total time | Failure |",
            "| ---: | ---: | --- | --- | ---: | ---: | ---: | --- |",
        ]
    )
    for row in report["per_task"]:
        score = "n/a" if row["score"] is None else str(row["score"])
        failure = row["technical_failure_class"] or ""
        lines.append(
            f"| {row['task_index']} | {row['repetition']} | {row['agent']} | "
            f"{row['harness']} | {score} | {row['agent_seconds']:.1f}s | "
            f"{row['total_seconds']:.1f}s | {failure} |"
        )
    lines.append("")
    return "\n".join(lines)


async def run_comparison(config: ComparisonConfig) -> tuple[Path, dict[str, Any]]:
    pairs = build_cell_plans(config)
    run_dir = (config.output_root / config.comparison_id).resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    manifest = build_manifest(config, pairs)
    manifest_path = run_dir / "manifest.json"
    with manifest_path.open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, ensure_ascii=False)
    print(json.dumps(manifest, indent=2, ensure_ascii=False), flush=True)

    workers = 1 if config.execution_mode == "sequential" else config.workers
    semaphore = asyncio.Semaphore(workers)
    results: list[dict[str, Any]] = []
    started = time.perf_counter()

    async def run_pair(pair: list[CellPlan]) -> None:
        async with semaphore:
            # Intentional: pair arms never overlap. Independent tasks are the parallel unit.
            for cell in pair:
                results.append(await _run_cell(config, cell, run_dir))

    await asyncio.gather(*(run_pair(pair) for pair in pairs))
    wall_clock = time.perf_counter() - started
    plan_order = {
        cell.cell_id: index
        for index, cell in enumerate(cell for pair in pairs for cell in pair)
    }
    results.sort(key=lambda result: plan_order[result["cell_id"]])
    report = build_comparison_report(config, results, wall_clock_seconds=wall_clock)
    _atomic_json(run_dir / "comparison.json", report)
    (run_dir / "comparison.md").write_text(_report_markdown(report), encoding="utf-8")
    print(
        f"Comparison complete: {len(results)} cells in {wall_clock:.1f}s\n"
        f"Report: {run_dir / 'comparison.md'}",
        flush=True,
    )
    return run_dir, report
