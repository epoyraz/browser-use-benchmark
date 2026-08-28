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
import tempfile
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
from .telemetry import ProcessTelemetrySampler, summarize_cell_telemetry

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
    record_actions: bool = False
    trace_cdp: bool = False
    capture_diagnostics: bool = False
    sample_processes: bool = False
    process_sample_interval_seconds: float = 1.0
    search_endpoint: str | None = None


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
    harness_names = [name for name in ("v1", "v2") if name in config.harnesses]
    if not harness_names:
        raise ValueError("At least one harness must be configured")
    for agent_name in config.agents:
        for repetition in range(config.repeats):
            for task in config.tasks:
                pair_id = f"{_safe_part(agent_name)}-r{repetition:02d}-t{task.benchmark_index:03d}"
                order = (
                    ordered_harnesses(
                        config.paired_order,
                        pair_ordinal=pair_ordinal,
                        repetition=repetition,
                        seed=config.seed,
                    )
                    if len(harness_names) == 2
                    else harness_names
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


def _system_prompt(
    harness: HarnessSpec,
    screenshot_dir: Path,
    *,
    search_endpoint: str | None = None,
) -> str:
    template = PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")
    example = harness.screenshot_example.format(
        path=repr(str(screenshot_dir / "step_001.png"))
    )
    discovery_guidance = ""
    if search_endpoint:
        discovery_guidance = (
            "- For general web discovery, use the configured HTML search page "
            f"`{search_endpoint}` through `{harness.command_name}`. Replace `{{query}}` "
            "with a URL-encoded query, then open primary result pages through the harness. "
            "The usual public search providers are known to return CAPTCHA or automated-"
            "query blocks from this benchmark environment.\n"
        )
    return template.format(
        harness_command=harness.command_name,
        screenshot_dir=str(screenshot_dir),
        screenshot_example=example,
        discovery_guidance=discovery_guidance,
    )


def _benchmark_helper_source(diagnostics_dir: Path | None) -> str:
    if diagnostics_dir is None:
        return '"""No benchmark-specific helpers are enabled for this cell."""\n'
    rendered_dir = json.dumps(str(diagnostics_dir), ensure_ascii=False)
    return f'''"""Benchmark-owned v2 diagnostics instrumentation.

This captures bounded privacy-safe diagnostics around each bh invocation. It does not
record URLs, page text, headers, bodies, form values, or screenshot payloads.
"""
import json as _benchmark_json
import os as _benchmark_os
import time as _benchmark_time
from pathlib import Path as _BenchmarkPath

_benchmark_diagnostics_dir = _BenchmarkPath({rendered_dir})
_benchmark_diagnostics_dir.mkdir(parents=True, exist_ok=True)
_benchmark_started_targets = {{}}
_benchmark_target_snapshots = []
_benchmark_closing = False


def _benchmark_start_target(tab_obj=None):
    target = tab_obj or session.tab()
    target_id = str(target.target_id)
    if target_id not in _benchmark_started_targets:
        _benchmark_started_targets[target_id] = target.start_diagnostics()
    return target


def _benchmark_capture_target(target_id):
    try:
        target = session.tab(target_id)
        _benchmark_target_snapshots.append({{
            "target_id": str(target_id),
            "diagnostics": target.diagnostics(),
        }})
    except Exception as error:
        _benchmark_target_snapshots.append({{
            "target_id": str(target_id),
            "error_class": type(error).__name__,
        }})


_benchmark_original_goto = goto
def goto(*args, **kwargs):
    _benchmark_start_target()
    return _benchmark_original_goto(*args, **kwargs)


_benchmark_original_new_tab = new_tab
def new_tab(*args, **kwargs):
    target = _benchmark_original_new_tab(*args, **kwargs)
    _benchmark_start_target(target)
    return target


_benchmark_original_use_tab = use_tab
def use_tab(*args, **kwargs):
    target = _benchmark_original_use_tab(*args, **kwargs)
    _benchmark_start_target(target)
    return target


_benchmark_original_close = session.close
def _benchmark_close_with_diagnostics():
    global _benchmark_closing
    if _benchmark_closing:
        return _benchmark_original_close()
    _benchmark_closing = True
    for target_id in list(_benchmark_started_targets):
        _benchmark_capture_target(target_id)
    payload = {{
        "pid": _benchmark_os.getpid(),
        "captured_at": round(_benchmark_time.time(), 3),
        "started": _benchmark_started_targets,
        "targets": _benchmark_target_snapshots,
    }}
    destination = _benchmark_diagnostics_dir / (
        f"diagnostics-{{_benchmark_os.getpid()}}-{{_benchmark_time.time_ns()}}.json"
    )
    temporary = destination.with_suffix(".json.tmp")
    try:
        temporary.write_text(
            _benchmark_json.dumps(payload, separators=(",", ":")), encoding="utf-8"
        )
        temporary.replace(destination)
        journal.write(
            "note",
            event="benchmark_diagnostics_captured",
            target_snapshots=len(_benchmark_target_snapshots),
        )
    except OSError:
        pass
    return _benchmark_original_close()


session.close = _benchmark_close_with_diagnostics
try:
    _benchmark_start_target()
except Exception:
    pass
'''


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


#: AF_UNIX caps a socket path at 104 bytes on macOS and 108 on Linux; take the smaller.
_SOCKET_PATH_LIMIT = 104


def _runtime_dir(cell_id: str, daemon_name: str) -> Path:
    """A short home for the daemon's `.sock`/`.port`, outside the deep run tree.

    `run_data/harness_comparisons/<comparison>/cells/<cell_id>/runtime/<daemon>.sock` is
    around 175 bytes, well past the AF_UNIX limit. v2 fails closed and says so exactly; v1
    would bind a silently truncated path instead, which is the worse of the two.

    Nothing archival is written here — the runner only globs for endpoints and removes
    them at teardown — so moving it out of the cell directory costs no evidence.
    """
    digest = hashlib.sha256(cell_id.encode()).hexdigest()[:10]
    budget = _SOCKET_PATH_LIMIT - len(f"/{daemon_name}.sock".encode())
    roots = [Path(tempfile.gettempdir())]
    if os.name != "nt":
        roots.append(Path("/tmp"))          # shorter, and the fallback the error suggests
    for root in roots:
        candidate = root / f"bh-rt-{digest}"
        if len(str(candidate).encode()) <= budget:
            return candidate
    raise RuntimeError(
        f"no temporary directory leaves room for a {_SOCKET_PATH_LIMIT}-byte socket path "
        f"with daemon name {daemon_name!r}")


async def _run_cell(
    config: ComparisonConfig, plan: CellPlan, run_dir: Path
) -> dict[str, Any]:
    total_started = time.perf_counter()
    harness = config.harnesses[plan.harness_name]
    agent = config.agents[plan.agent_name]
    cell_dir = run_dir / "cells" / plan.cell_id
    workspace = cell_dir / "workspace"
    screenshots_dir = workspace / "screenshots"
    daemon_name = _safe_part(
        f"{config.comparison_id[-8:]}-{plan.harness_name}-{plan.agent_name[:2]}-"
        f"r{plan.repetition}-t{plan.task.benchmark_index}",
        limit=64,
    )
    runtime_dir = _runtime_dir(plan.cell_id, daemon_name)
    temp_dir = cell_dir / "tmp"
    diagnostics_dir = cell_dir / "diagnostics"
    recordings_dir = cell_dir / "recordings"
    for directory in (
        workspace,
        screenshots_dir,
        runtime_dir,
        temp_dir,
        diagnostics_dir,
        recordings_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    shutil.copy2(harness.skill, workspace / "SKILL.md")
    shutil.copy2(WORKSPACE_OVERRIDE_PATH, workspace / "AGENTS.override.md")
    helper_source = _benchmark_helper_source(
        diagnostics_dir
        if config.capture_diagnostics and plan.harness_name == "v2"
        else None
    )
    # v1 discovers agent_helpers.py through BH_AGENT_WORKSPACE. v2 deliberately uses
    # BH_HELPERS for its editable extension surface, so give it an explicit benchmark-owned
    # file rather than relying on the workspace convention of the other implementation.
    (workspace / "agent_helpers.py").write_text(helper_source, encoding="utf-8")
    benchmark_helpers = cell_dir / "benchmark_helpers.py"
    benchmark_helpers.write_text(helper_source, encoding="utf-8")
    system_prompt = _system_prompt(
        harness, screenshots_dir, search_endpoint=config.search_endpoint
    )
    (workspace / "benchmark-system-prompt.md").write_text(
        system_prompt, encoding="utf-8"
    )

    path_value = os.pathsep.join([str(harness.cli.parent), os.environ.get("PATH", "")])
    harness_env: dict[str, str] = {
        "PATH": path_value,
        "BU_NAME": daemon_name,
        "BH_RUNTIME_DIR": str(runtime_dir),
        "BH_TMP_DIR": str(temp_dir),
        "BH_AGENT_WORKSPACE": str(workspace),
        "BH_HELPERS": str(benchmark_helpers),
        # Keep v1's optional saved Browser Use cloud credential store outside the
        # benchmark. The file is intentionally absent, so even an accidental cloud
        # administration command cannot recover a globally saved provider key.
        "BH_AUTH_PATH": str(cell_dir / "browser-harness-auth.json"),
        "BH_JOURNAL": str(cell_dir / "harness-journal.jsonl"),
        "BH_JOURNAL_DIR": str(cell_dir),
        "BH_CDP_TRACE": "1" if config.trace_cdp else "0",
        "BH_RECORD": "1" if config.record_actions else "0",
        "BH_RECORDINGS": str(recordings_dir),
        "BH_RECORDING_KEEP": "1000",
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
    telemetry_errors: list[str] = []
    setup_started = time.perf_counter()
    setup_duration = 0.0
    teardown_duration = 0.0
    browser_started = False
    daemon_started = False
    prewarm: dict[str, object] = {}
    sampler = (
        ProcessTelemetrySampler(
            cell_dir / "process-telemetry.jsonl",
            interval_seconds=config.process_sample_interval_seconds,
        )
        if config.sample_processes
        else None
    )
    sampler_started = False

    try:
        cdp_url = await browser.start()
        browser_started = True
        harness_env["BU_CDP_URL"] = cdp_url
        daemon.env.update(harness_env)
        await daemon.start()
        daemon_started = True
        if sampler is not None:
            sampler.add_root("browser", browser.proc.pid if browser.proc else None)
            sampler.add_root("harness_daemon", daemon.proc.pid if daemon.proc else None)
            try:
                await sampler.start()
                sampler_started = True
            except Exception as exc:  # noqa: BLE001 - telemetry cannot break the cell
                telemetry_errors.append(
                    f"process telemetry start: {type(exc).__name__}: {exc}"
                )
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
            on_process_started=(
                lambda pid: sampler.add_root("agent", pid)
                if sampler is not None
                else None
            ),
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
            # It is outside the cell directory now, so nothing else will collect it.
            shutil.rmtree(runtime_dir, ignore_errors=True)
        if browser_started or browser.proc is not None:
            try:
                await browser.stop()
            except Exception as exc:  # noqa: BLE001 - preserve the primary cell result
                teardown_errors.append(f"browser stop: {type(exc).__name__}: {exc}")
        if sampler_started and sampler is not None:
            try:
                await sampler.stop()
            except Exception as exc:  # noqa: BLE001 - telemetry cannot break the cell
                telemetry_errors.append(
                    f"process telemetry stop: {type(exc).__name__}: {exc}"
                )
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

    try:
        harness_telemetry = summarize_cell_telemetry(cell_dir)
    except Exception as exc:  # noqa: BLE001 - telemetry cannot break the cell
        telemetry_errors.append(f"telemetry summary: {type(exc).__name__}: {exc}")
        harness_telemetry = {"error": telemetry_errors[-1]}
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
            "telemetry_capture": {
                "action_recording": config.record_actions,
                "cdp_round_trip_trace": config.trace_cdp,
                "bounded_page_diagnostics": config.capture_diagnostics,
                "process_tree_sampling": config.sample_processes,
                "process_sample_interval_seconds": (
                    config.process_sample_interval_seconds
                    if config.sample_processes
                    else None
                ),
            },
            "harness_telemetry": harness_telemetry,
        },
        "execution": execution.to_json(),
        "judgement": judgement.to_json(),
        "setup_error": setup_error,
        "teardown_errors": teardown_errors,
        "telemetry_errors": telemetry_errors,
        "prewarm": prewarm,
        "screenshots": [str(path.relative_to(cell_dir)) for path in screenshots],
        "telemetry_artifacts": {
            "recordings": "recordings",
            "diagnostics": "diagnostics",
            "process_samples": "process-telemetry.jsonl",
        },
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
    limits: dict[str, object] = {
        "task_timeout_seconds": config.task_timeout_seconds,
        "judge_timeout_seconds": config.judge_timeout_seconds,
        "codex_sandbox": config.codex_sandbox,
        "codex_shell_environment": "inherit-all-after-api-credential-stripping",
        "codex_forced_login_method": "chatgpt",
        "codex_web_search": "disabled",
        "ambient_browser_instructions": (
            "superseded-by-fixed-workspace-and-developer-policy"
        ),
        "subagents": "disabled",
    }
    if "claude" in config.agents or (
        config.judge is not None and config.judge.name == "claude"
    ):
        limits["claude_max_turns"] = config.claude_max_turns
    fixed_axes = {
        "benchmark": config.benchmark,
        "task_ids": [task.task_id for task in config.tasks],
        "agents": {name: spec.to_manifest() for name, spec in config.agents.items()},
        "browser": config.browser.to_manifest(),
        "judge": judge_manifest(config.judge),
        "limits": limits,
        "telemetry": {
            "action_recording": config.record_actions,
            "cdp_round_trip_trace": config.trace_cdp,
            "bounded_page_diagnostics": config.capture_diagnostics,
            "process_tree_sampling": config.sample_processes,
            "process_sample_interval_seconds": (
                config.process_sample_interval_seconds
                if config.sample_processes
                else None
            ),
            "external_telemetry": "disabled",
        },
        "discovery": {
            "search_endpoint": config.search_endpoint,
            "delivery": "browser-navigation-through-selected-harness",
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
        "run_mode": (
            "paired-comparison"
            if set(config.harnesses) == {"v1", "v2"}
            else "single-harness-telemetry"
        ),
        "api_policy": {
            "task_agents": "local subscription CLI: "
            + ", ".join(sorted(config.agents)),
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
            "parallel_unit": (
                "independent task pair"
                if len(config.harnesses) == 2
                else "independent task cell"
            ),
            "pair_arms_overlap": False if len(config.harnesses) == 2 else None,
            "repeats": config.repeats,
            "paired_order": config.paired_order,
            "random_seed": config.seed,
            "task_timeout_seconds": config.task_timeout_seconds,
            "judge_timeout_seconds": config.judge_timeout_seconds,
            "telemetry": fixed_axes["telemetry"],
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


def _task_key(result: dict[str, Any]) -> tuple[int, str]:
    benchmark = result["provenance"]["benchmark"]
    return int(benchmark["benchmark_index"]), str(benchmark["task_id"])


def _captcha_excluded_task_keys(
    results: list[dict[str, Any]],
) -> set[tuple[int, str]]:
    return {
        _task_key(result)
        for result in results
        if (result.get("judgement") or {}).get("reached_captcha")
    }


def _capability_blocker(
    result: dict[str, Any], captcha_excluded_tasks: set[tuple[int, str]]
) -> str | None:
    if _task_key(result) in captcha_excluded_tasks:
        return "captcha-task"
    judgement = result.get("judgement") or {}
    if judgement.get("impossible_task"):
        return "impossible-task"
    return None


def build_comparison_report(
    config: ComparisonConfig,
    results: list[dict[str, Any]],
    *,
    wall_clock_seconds: float,
) -> dict[str, Any]:
    paired_mode = set(config.harnesses) == {"v1", "v2"}
    captcha_excluded_tasks = _captcha_excluded_task_keys(results)
    task_exclusions: list[dict[str, Any]] = []
    for task_key in sorted(captcha_excluded_tasks):
        affected = [result for result in results if _task_key(result) == task_key]
        sample = affected[0]["provenance"]["benchmark"]
        task_exclusions.append(
            {
                "task_index": task_key[0],
                "task_id": task_key[1],
                "category": sample.get("category", "unknown"),
                "reason": "captcha",
                "captcha_cells": [
                    result["cell_id"]
                    for result in affected
                    if (result.get("judgement") or {}).get("reached_captcha")
                ],
                "observed_cells": len(affected),
            }
        )

    aggregates: list[dict[str, Any]] = []
    for agent_name in config.agents:
        for harness_name in config.harnesses:
            group = [
                result
                for result in results
                if result["provenance"]["agent"]["name"] == agent_name
                and result["provenance"]["harness"]["name"] == harness_name
            ]
            judged = [result for result in group if result.get("score") in (0, 1)]
            capability_eligible = [
                result
                for result in judged
                if _capability_blocker(result, captcha_excluded_tasks) is None
            ]
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
                    "capability_eligible_runs": len(capability_eligible),
                    "capability_passes": sum(
                        int(result["score"]) for result in capability_eligible
                    ),
                    "capability_pass_rate": (
                        sum(int(result["score"]) for result in capability_eligible)
                        / len(capability_eligible)
                        if capability_eligible
                        else None
                    ),
                    "capability_mean_agent_seconds": _mean(
                        [
                            float(result["timing"]["agent_seconds"])
                            for result in capability_eligible
                        ]
                    ),
                    "capability_mean_total_seconds": _mean(
                        [
                            float(result["timing"]["total_seconds"])
                            for result in capability_eligible
                        ]
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
    capability_paired: dict[str, dict[str, int]] = {}
    paired_timing: dict[str, dict[str, float | int | None]] = {}
    for agent_name in (config.agents if paired_mode else ()):
        counts = {
            "v1_pass_v2_pass": 0,
            "v1_pass_v2_fail": 0,
            "v1_fail_v2_pass": 0,
            "v1_fail_v2_fail": 0,
            "unjudged_pairs": 0,
        }
        capability_counts = {
            "v1_pass_v2_pass": 0,
            "v1_pass_v2_fail": 0,
            "v1_fail_v2_pass": 0,
            "v1_fail_v2_fail": 0,
            "excluded_task_pairs": 0,
            "blocked_pairs": 0,
            "unjudged_pairs": 0,
        }
        pair_ids = {
            result["pair_id"]
            for result in results
            if result["provenance"]["agent"]["name"] == agent_name
        }
        raw_agent_deltas: list[float] = []
        raw_total_deltas: list[float] = []
        scored_agent_deltas: list[float] = []
        scored_total_deltas: list[float] = []
        for pair_id in pair_ids:
            arm_results = {
                result["provenance"]["harness"]["name"]: result
                for result in results
                if result["pair_id"] == pair_id
            }
            if set(arm_results) == {"v1", "v2"}:
                raw_agent_deltas.append(
                    float(arm_results["v2"]["timing"]["agent_seconds"])
                    - float(arm_results["v1"]["timing"]["agent_seconds"])
                )
                raw_total_deltas.append(
                    float(arm_results["v2"]["timing"]["total_seconds"])
                    - float(arm_results["v1"]["timing"]["total_seconds"])
                )
            task_is_excluded = bool(arm_results) and any(
                _task_key(result) in captcha_excluded_tasks
                for result in arm_results.values()
            )
            arms = {name: result.get("score") for name, result in arm_results.items()}
            if arms.get("v1") not in (0, 1) or arms.get("v2") not in (0, 1):
                counts["unjudged_pairs"] += 1
                if task_is_excluded:
                    capability_counts["excluded_task_pairs"] += 1
                else:
                    capability_counts["unjudged_pairs"] += 1
                continue
            left = "pass" if arms["v1"] == 1 else "fail"
            right = "pass" if arms["v2"] == 1 else "fail"
            counts[f"v1_{left}_v2_{right}"] += 1
            if task_is_excluded:
                capability_counts["excluded_task_pairs"] += 1
                continue
            if any(
                _capability_blocker(result, captcha_excluded_tasks)
                for result in arm_results.values()
            ):
                capability_counts["blocked_pairs"] += 1
                continue
            capability_counts[f"v1_{left}_v2_{right}"] += 1
            scored_agent_deltas.append(
                float(arm_results["v2"]["timing"]["agent_seconds"])
                - float(arm_results["v1"]["timing"]["agent_seconds"])
            )
            scored_total_deltas.append(
                float(arm_results["v2"]["timing"]["total_seconds"])
                - float(arm_results["v1"]["timing"]["total_seconds"])
            )
        paired[agent_name] = counts
        capability_paired[agent_name] = capability_counts
        paired_timing[agent_name] = {
            "pairs": len(scored_agent_deltas),
            "raw_pairs": len(raw_agent_deltas),
            "mean_agent_seconds_delta_v2_minus_v1": _mean(scored_agent_deltas),
            "median_agent_seconds_delta_v2_minus_v1": _median(scored_agent_deltas),
            "mean_total_seconds_delta_v2_minus_v1": _mean(scored_total_deltas),
            "raw_mean_agent_seconds_delta_v2_minus_v1": _mean(raw_agent_deltas),
            "raw_mean_total_seconds_delta_v2_minus_v1": _mean(raw_total_deltas),
        }

    order_effects: list[dict[str, Any]] = []
    for order in sorted(
        {int(result["provenance"]["benchmark"]["order"]) for result in results}
    ):
        group = [
            result
            for result in results
            if int(result["provenance"]["benchmark"]["order"]) == order
        ]
        judged = [result for result in group if result.get("score") in (0, 1)]
        captcha_runs = sum(
            1 for result in group if result["judgement"].get("reached_captcha")
        )
        order_effects.append(
            {
                "order": order,
                "runs": len(group),
                "judged_runs": len(judged),
                "passes": sum(int(result["score"]) for result in judged),
                "pass_rate": (
                    sum(int(result["score"]) for result in judged) / len(judged)
                    if judged
                    else None
                ),
                "captcha_runs": captcha_runs,
                "captcha_rate": captcha_runs / len(group) if group else None,
                "mean_agent_seconds": _mean(
                    [float(result["timing"]["agent_seconds"]) for result in group]
                ),
            }
        )

    validity_warnings: list[str] = []
    if config.repeats < 3:
        validity_warnings.append(
            f"Only {config.repeats} repetition(s) were run; treat outcomes as exploratory."
        )
    captcha_total = sum(
        1 for result in results if result["judgement"].get("reached_captcha")
    )
    if captcha_total:
        validity_warnings.append(
            f"CAPTCHA affected {captcha_total}/{len(results)} cells; "
            f"{len(task_exclusions)}/{len(config.tasks)} selected tasks were excluded "
            "from every comparison score."
        )
    if config.tasks and len(task_exclusions) == len(config.tasks):
        validity_warnings.append(
            "No tasks remain after CAPTCHA exclusion; this run has no benchmark score."
        )
    if len(order_effects) >= 2:
        first, second = order_effects[0], order_effects[1]
        if (
            first["captcha_rate"] is not None
            and second["captcha_rate"] is not None
            and abs(float(first["captcha_rate"]) - float(second["captcha_rate"]))
            >= 0.5
        ):
            validity_warnings.append(
                "Strong order confound: CAPTCHA rate was "
                f"{100 * float(first['captcha_rate']):.1f}% at order {first['order']} and "
                f"{100 * float(second['captcha_rate']):.1f}% at order {second['order']}."
            )
    if paired_mode:
        comparable_pairs = sum(
            sum(
                count
                for key, count in counts.items()
                if key.startswith("v1_")
            )
            for counts in capability_paired.values()
        )
        total_pairs = sum(len(config.tasks) * config.repeats for _ in config.agents)
        if comparable_pairs < total_pairs:
            validity_warnings.append(
                f"Only {comparable_pairs}/{total_pairs} pairs remained "
                "capability-comparable after task-wide CAPTCHA exclusion and "
                "impossible-task blocking."
            )

    category_aggregate: list[dict[str, Any]] = []
    for category in sorted({task.category for task in config.tasks}):
        for agent_name in config.agents:
            for harness_name in config.harnesses:
                group = [
                    result
                    for result in results
                    if result["provenance"]["benchmark"]["category"] == category
                    and result["provenance"]["agent"]["name"] == agent_name
                    and result["provenance"]["harness"]["name"] == harness_name
                ]
                eligible = [
                    result
                    for result in group
                    if result.get("score") in (0, 1)
                    and _capability_blocker(result, captcha_excluded_tasks) is None
                ]
                category_aggregate.append(
                    {
                        "category": category,
                        "agent": agent_name,
                        "harness": harness_name,
                        "runs": len(group),
                        "scored_runs": len(eligible),
                        "passes": sum(int(result["score"]) for result in eligible),
                        "pass_rate": (
                            sum(int(result["score"]) for result in eligible)
                            / len(eligible)
                            if eligible
                            else None
                        ),
                        "captcha_excluded_tasks": len(
                            {_task_key(result) for result in group}
                            & captcha_excluded_tasks
                        ),
                        "mean_agent_seconds": _mean(
                            [
                                float(result["timing"]["agent_seconds"])
                                for result in eligible
                            ]
                        ),
                    }
                )

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
            "capability_score": (
                result.get("score")
                if _capability_blocker(result, captcha_excluded_tasks) is None
                else None
            ),
            "capability_blocker": _capability_blocker(
                result, captcha_excluded_tasks
            ),
            "excluded_from_scoring": _task_key(result) in captcha_excluded_tasks,
            "exclusion_reason": (
                "captcha" if _task_key(result) in captcha_excluded_tasks else None
            ),
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
            "failure_reason": result["judgement"].get("failure_reason"),
            "judge_error": result["judgement"].get("error"),
            "harness_telemetry": result["metrics"].get("harness_telemetry"),
        }
        for result in results
    ]
    return {
        "comparison_id": config.comparison_id,
        "run_mode": (
            "paired-comparison" if paired_mode else "single-harness-telemetry"
        ),
        "measurement_scope": config.measurement_scope,
        "wall_clock_seconds": wall_clock_seconds,
        "cells_completed": len(results),
        "task_scoring": {
            "policy": (
                "If any cell reaches CAPTCHA or human verification, exclude the "
                "entire task across all agents, harnesses, and repetitions."
            ),
            "selected_tasks": len(config.tasks),
            "scored_tasks": len(config.tasks) - len(task_exclusions),
            "excluded_task_count": len(task_exclusions),
            "excluded_tasks": task_exclusions,
        },
        "aggregate": aggregates,
        "category_aggregate": category_aggregate,
        "paired_outcomes": paired,
        "capability_paired_outcomes": capability_paired,
        "paired_timing": paired_timing,
        "order_effects": order_effects,
        "validity_warnings": validity_warnings,
        "per_task": per_task,
    }


def _report_markdown(report: dict[str, Any]) -> str:
    task_scoring = report["task_scoring"]
    lines = [
        f"# Browser Harness run `{report['comparison_id']}`",
        "",
        f"Mode: **{report['run_mode']}**  ",
        f"Scope: **{report['measurement_scope']}**  ",
        f"Wall clock: **{report['wall_clock_seconds']:.1f}s**  ",
        f"Cells completed: **{report['cells_completed']}**  ",
        (
            f"Scored tasks: **{task_scoring['scored_tasks']} / "
            f"{task_scoring['selected_tasks']}**  "
        ),
        f"CAPTCHA-excluded tasks: **{task_scoring['excluded_task_count']}**",
        "",
        "## Validity warnings",
        "",
        *[f"- {warning}" for warning in report.get("validity_warnings", [])],
        "",
        "## CAPTCHA task exclusions",
        "",
    ]
    if task_scoring["excluded_tasks"]:
        lines.extend(
            [
                "| Task | Category | Reason | CAPTCHA cells |",
                "| ---: | --- | --- | ---: |",
            ]
        )
        for exclusion in task_scoring["excluded_tasks"]:
            lines.append(
                f"| {exclusion['task_index']} | {exclusion['category']} | "
                f"{exclusion['reason']} | {len(exclusion['captcha_cells'])} |"
            )
    else:
        lines.append("No tasks were excluded by the CAPTCHA rule.")
    lines.extend(
        [
            "",
            "## Scored aggregate",
            "",
            "| Agent | Harness | Scored passes | Eligible cells | Scored pass rate | Scored mean agent time | Scored mean total time | Raw audit passes | Raw judged | Raw pass rate |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in report["aggregate"]:
        rate = "n/a" if row["pass_rate"] is None else f"{100 * row['pass_rate']:.1f}%"
        capability_rate = (
            "n/a"
            if row["capability_pass_rate"] is None
            else f"{100 * row['capability_pass_rate']:.1f}%"
        )
        capability_agent_time = (
            "n/a"
            if row["capability_mean_agent_seconds"] is None
            else f"{row['capability_mean_agent_seconds']:.1f}s"
        )
        capability_total_time = (
            "n/a"
            if row["capability_mean_total_seconds"] is None
            else f"{row['capability_mean_total_seconds']:.1f}s"
        )
        lines.append(
            f"| {row['agent']} | {row['harness']} | {row['capability_passes']} | "
            f"{row['capability_eligible_runs']} | {capability_rate} | "
            f"{capability_agent_time} | {capability_total_time} | {row['passes']} | "
            f"{row['judged_runs']} | {rate} |"
        )
    lines.extend(
        [
            "",
            "## Category aggregate",
            "",
            "| Category | Agent | Harness | Scored passes | Scored runs | Pass rate | CAPTCHA exclusions | Mean agent time |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in report["category_aggregate"]:
        rate = (
            "n/a"
            if row["pass_rate"] is None
            else f"{100 * row['pass_rate']:.1f}%"
        )
        mean_agent_time = (
            "n/a"
            if row["mean_agent_seconds"] is None
            else f"{row['mean_agent_seconds']:.1f}s"
        )
        lines.append(
            f"| {row['category']} | {row['agent']} | {row['harness']} | "
            f"{row['passes']} | {row['scored_runs']} | {rate} | "
            f"{row['captcha_excluded_tasks']} | "
            f"{mean_agent_time} |"
        )
    if report["paired_outcomes"]:
        lines.extend(["", "## Raw audit paired outcomes", ""])
        for agent, counts in report["paired_outcomes"].items():
            lines.append(
                f"- {agent}: "
                + ", ".join(f"{key}={value}" for key, value in counts.items())
            )
        lines.extend(["", "## Scored paired outcomes", ""])
        for agent, counts in report["capability_paired_outcomes"].items():
            lines.append(
                f"- {agent}: "
                + ", ".join(f"{key}={value}" for key, value in counts.items())
            )
        lines.extend(["", "## Scored paired timing", ""])
        for agent, timing in report["paired_timing"].items():
            agent_delta = timing["mean_agent_seconds_delta_v2_minus_v1"]
            total_delta = timing["mean_total_seconds_delta_v2_minus_v1"]
            rendered_agent = "n/a" if agent_delta is None else f"{agent_delta:+.1f}s"
            rendered_total = "n/a" if total_delta is None else f"{total_delta:+.1f}s"
            lines.append(
                f"- {agent}: v2-v1 mean agent time {rendered_agent}; "
                f"mean total time {rendered_total} ({timing['pairs']} scored pairs; "
                f"{timing['raw_pairs']} raw audit pairs)"
            )
    lines.extend(
        [
            "",
            "## Order diagnostics",
            "",
            "| Pair order | Runs | Passes | Pass rate | CAPTCHA runs | CAPTCHA rate | Mean agent time |",
            "| ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in report["order_effects"]:
        rate = "n/a" if row["pass_rate"] is None else f"{100 * row['pass_rate']:.1f}%"
        captcha_rate = (
            "n/a"
            if row["captcha_rate"] is None
            else f"{100 * row['captcha_rate']:.1f}%"
        )
        agent_time = (
            "n/a"
            if row["mean_agent_seconds"] is None
            else f"{row['mean_agent_seconds']:.1f}s"
        )
        lines.append(
            f"| {row['order']} | {row['runs']} | {row['passes']} | {rate} | "
            f"{row['captcha_runs']} | {captcha_rate} | {agent_time} |"
        )
    lines.extend(
        [
            "",
            "## Per-task results",
            "",
            "| Task | Rep | Agent | Harness | Raw audit score | Scored score | Agent time | Total time | Invokes | Helpers | CDP | Frames | Failure |",
            "| ---: | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for row in report["per_task"]:
        score = "n/a" if row["score"] is None else str(row["score"])
        if row["excluded_from_scoring"]:
            capability_score = "excluded (captcha task)"
        elif row["capability_blocker"]:
            capability_score = f"blocked ({row['capability_blocker']})"
        elif row["capability_score"] is None:
            capability_score = "n/a"
        else:
            capability_score = str(row["capability_score"])
        failure = (
            row["technical_failure_class"]
            or row["judge_error"]
            or row["failure_reason"]
            or ""
        )
        failure = str(failure).replace("|", "\\|").replace("\n", " ")
        telemetry = row.get("harness_telemetry") or {}
        invokes = (telemetry.get("invocations") or {}).get("count", 0)
        helpers = (telemetry.get("helpers") or {}).get("calls", 0)
        protocol = (telemetry.get("protocol") or {}).get("calls", 0)
        frames = (telemetry.get("recordings") or {}).get("frames", 0)
        lines.append(
            f"| {row['task_index']} | {row['repetition']} | {row['agent']} | "
            f"{row['harness']} | {score} | {capability_score} | "
            f"{row['agent_seconds']:.1f}s | {row['total_seconds']:.1f}s | "
            f"{invokes} | {helpers} | {protocol} | {frames} | {failure} |"
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
