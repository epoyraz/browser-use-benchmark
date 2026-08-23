"""Benchmark judging through a selected local subscription CLI, never an SDK/API key."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .agents import AgentExecution, AgentSpec, codex_isolation_args
from .process import run_captured, subscription_only_env

JUDGE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "reasoning": {"type": "string"},
        "verdict": {"type": "boolean"},
        "failure_reason": {"type": "string"},
        "impossible_task": {"type": "boolean"},
        "reached_captcha": {"type": "boolean"},
    },
    "required": [
        "reasoning",
        "verdict",
        "failure_reason",
        "impossible_task",
        "reached_captcha",
    ],
    "additionalProperties": False,
}


JUDGE_SYSTEM_PROMPT = """You are an exacting judge of a browser automation benchmark.
Decide whether the agent fully completed the requested task. Treat the supplied ground truth,
when present, as authoritative. A partial answer is a failure. Do not trust self-reported
success when the trajectory or screenshots contradict it. CAPTCHA, missing authentication,
an inaccessible site, or an actually impossible task still receives verdict=false; report the
corresponding flags. Browser crashes, page-load failures, safety refusals, and inability to
interact with the required UI are failures. Respond only with the requested JSON object."""

JUDGE_WORKSPACE_OVERRIDE = """# Controlled Benchmark Judge

These workspace-specific rules supersede global or personal browser and agent instructions.
Act only as the benchmark judge described by the supplied developer instructions. Do not
navigate the web, operate a browser, call APIs, use plugins, or spawn subagents. Read only the
execution artifacts explicitly supplied by the judge prompt.
"""

# Manifested separately from the system policy because changing which execution fields the
# judge sees can change a score even when the judging policy itself is byte-identical.
JUDGE_INPUT_CONTRACT = """task; ground_truth; agent_trajectory; full final_response;
compact final_answer_marker; technical_error; up to five attached screenshots. Judge the
full delivered response; the compact marker cannot erase details present there."""


@dataclass
class Judgement:
    score: int | None
    reasoning: str = ""
    failure_reason: str = ""
    impossible_task: bool = False
    reached_captcha: bool = False
    duration_seconds: float = 0.0
    judge: str = "none"
    model: str = "cli-default"
    reasoning_effort: str = "cli-default"
    error: str | None = None

    def to_json(self) -> dict[str, object]:
        return asdict(self)


def judge_manifest(spec: AgentSpec | None) -> dict[str, object]:
    if spec is None:
        return {"name": "none"}
    return {
        **spec.to_manifest(),
        "prompt_sha256": hashlib.sha256(JUDGE_SYSTEM_PROMPT.encode()).hexdigest(),
        "schema_sha256": hashlib.sha256(
            json.dumps(JUDGE_SCHEMA, sort_keys=True).encode()
        ).hexdigest(),
        "workspace_override_sha256": hashlib.sha256(
            JUDGE_WORKSPACE_OVERRIDE.encode()
        ).hexdigest(),
        "input_contract_sha256": hashlib.sha256(
            JUDGE_INPUT_CONTRACT.encode()
        ).hexdigest(),
    }


def _truncate(text: str | None, limit: int = 40_000) -> str:
    value = text or ""
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n...[truncated {len(value) - limit} characters]"


def _judge_prompt(
    *,
    task_description: str,
    ground_truth: object,
    execution: AgentExecution,
    screenshots: list[Path],
) -> str:
    steps = "\n".join(execution.steps)
    shot_text = "\n".join(f"- {path}" for path in screenshots) or "- none"
    return f"""Evaluate this browser-agent execution.

<task>
{_truncate(task_description)}
</task>

<ground_truth>
{_truncate(str(ground_truth) if ground_truth is not None else "No ground truth supplied")}
</ground_truth>

<agent_trajectory>
{_truncate(steps) or "No trajectory captured"}
</agent_trajectory>

<final_response>
{_truncate(execution.final_message)}
</final_response>

<final_answer_marker>
{_truncate(execution.final_result)}
</final_answer_marker>

<technical_error>
{execution.error or "none"}
</technical_error>

The following screenshots are local files from this exact execution. Inspect them when
available; do not assume they show success merely because they exist:
{shot_text}

Judge what the user actually received in <final_response>. The marker is a compact
extraction aid and must not erase correct details that are present in the full response.
"""


def _find_judgement(value: object) -> dict[str, Any] | None:
    if isinstance(value, dict):
        if "verdict" in value:
            return value
        for key in ("structured_output", "result", "output", "completion"):
            if key in value:
                found = _find_judgement(value[key])
                if found:
                    return found
        for nested in value.values():
            found = _find_judgement(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_judgement(nested)
            if found:
                return found
    elif isinstance(value, str):
        text = value.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(lines[1:-1]) if len(lines) >= 3 else text
        try:
            return _find_judgement(json.loads(text))
        except json.JSONDecodeError:
            return None
    return None


def _validate_judgement(data: dict[str, Any]) -> dict[str, Any]:
    required = {
        "reasoning": str,
        "verdict": bool,
        "failure_reason": str,
        "impossible_task": bool,
        "reached_captcha": bool,
    }
    for key, expected in required.items():
        if key not in data or not isinstance(data[key], expected):
            raise ValueError(
                f"Judge output field {key!r} is missing or not {expected.__name__}"
            )
    return data


def _codex_judge_command(
    spec: AgentSpec,
    *,
    schema_path: Path,
    output_path: Path,
    screenshots: list[Path],
) -> list[str]:
    command = [
        str(spec.cli),
        "exec",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        *codex_isolation_args(developer_instructions=JUDGE_SYSTEM_PROMPT),
    ]
    if spec.model:
        command.extend(["--model", spec.model])
    if spec.reasoning_effort:
        command.extend(
            [
                "--config",
                "model_reasoning_effort=" + json.dumps(spec.reasoning_effort),
            ]
        )
    if screenshots:
        command.extend(["--image", *[str(path) for path in screenshots]])
    command.extend(
        [
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
            "-",
        ]
    )
    return command


def _claude_judge_command(spec: AgentSpec) -> list[str]:
    command = [
        str(spec.cli),
        "-p",
        "--safe-mode",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(JUDGE_SCHEMA, separators=(",", ":")),
        "--dangerously-skip-permissions",
        "--no-session-persistence",
        "--no-chrome",
        "--tools",
        "Read",
        "--system-prompt",
        JUDGE_SYSTEM_PROMPT,
    ]
    if spec.model:
        command.extend(["--model", spec.model])
    if spec.reasoning_effort:
        command.extend(["--effort", spec.reasoning_effort])
    return command


async def judge_execution(
    judge_spec: AgentSpec | None,
    *,
    task_description: str,
    ground_truth: object,
    execution: AgentExecution,
    screenshots: list[Path],
    cell_dir: Path,
    env: Mapping[str, str],
    timeout_seconds: float,
) -> Judgement:
    if judge_spec is None:
        return Judgement(score=None)

    judge_dir = cell_dir / "judge"
    judge_dir.mkdir(parents=True, exist_ok=True)
    schema_path = judge_dir / "schema.json"
    output_path = judge_dir / "output.json"
    prompt_path = judge_dir / "prompt.txt"
    schema_path.write_text(json.dumps(JUDGE_SCHEMA, indent=2), encoding="utf-8")
    (judge_dir / "AGENTS.override.md").write_text(
        JUDGE_WORKSPACE_OVERRIDE, encoding="utf-8"
    )
    prompt = _judge_prompt(
        task_description=task_description,
        ground_truth=ground_truth,
        execution=execution,
        screenshots=screenshots,
    )
    prompt_path.write_text(prompt, encoding="utf-8")
    selected_shots = screenshots[-5:]
    command = (
        _codex_judge_command(
            judge_spec,
            schema_path=schema_path,
            output_path=output_path,
            screenshots=selected_shots,
        )
        if judge_spec.name == "codex"
        else _claude_judge_command(judge_spec)
    )
    started = time.perf_counter()
    code, stdout, stderr, timed_out = await run_captured(
        command,
        cwd=judge_dir,
        env=subscription_only_env(env),
        input_text=prompt,
        timeout=timeout_seconds,
    )
    duration = time.perf_counter() - started
    (judge_dir / "stdout.log").write_text(stdout, encoding="utf-8")
    (judge_dir / "stderr.log").write_text(stderr, encoding="utf-8")

    try:
        if timed_out:
            raise RuntimeError(f"Judge timed out after {timeout_seconds:.1f}s")
        if code != 0:
            raise RuntimeError(f"Judge exited with code {code}: {stderr[-1_000:]}")
        raw = (
            output_path.read_text(encoding="utf-8") if output_path.exists() else stdout
        )
        try:
            decoded: object = json.loads(raw)
        except json.JSONDecodeError:
            decoded = raw
        parsed = _find_judgement(decoded)
        if parsed is None:
            parsed = _find_judgement(raw)
        if parsed is None:
            raise ValueError("Judge output did not contain a verdict object")
        data = _validate_judgement(parsed)
        return Judgement(
            score=1 if data["verdict"] else 0,
            reasoning=data["reasoning"],
            failure_reason=data["failure_reason"],
            impossible_task=data["impossible_task"],
            reached_captcha=data["reached_captcha"],
            duration_seconds=duration,
            judge=judge_spec.name,
            model=judge_spec.model or "cli-default",
            reasoning_effort=judge_spec.reasoning_effort or "cli-default",
        )
    except Exception as exc:  # noqa: BLE001 - a broken judge must leave an auditable result
        return Judgement(
            score=None,
            duration_seconds=duration,
            judge=judge_spec.name,
            model=judge_spec.model or "cli-default",
            reasoning_effort=judge_spec.reasoning_effort or "cli-default",
            error=f"{type(exc).__name__}: {exc}",
        )
