"""Codex and Claude Code subscription-CLI execution adapters."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import IO, Any

from .process import process_group_kwargs, subscription_only_env, terminate_process_tree


class AgentValidationError(ValueError):
    pass


CODEX_ISOLATION_CONFIG = (
    'forced_login_method="chatgpt"',
    'web_search="disabled"',
    "agents.enabled=false",
    "features.apps=false",
    "features.hooks=false",
    "features.memories=false",
    "features.multi_agent=false",
    "features.remote_plugin=false",
    "shell_environment_policy.inherit=all",
    "tools.web_search=false",
)


def codex_isolation_args(*, developer_instructions: str | None = None) -> list[str]:
    values = list(CODEX_ISOLATION_CONFIG)
    if developer_instructions:
        values.append(
            "developer_instructions="
            + json.dumps(developer_instructions, ensure_ascii=False)
        )
    return [part for value in values for part in ("--config", value)]


@dataclass(frozen=True)
class AgentSpec:
    name: str
    cli: Path
    version: str
    model: str | None = None

    def to_manifest(self) -> dict[str, object]:
        return {
            "name": self.name,
            "cli": str(self.cli),
            "cli_version": self.version,
            "model": self.model or "cli-default",
            "authentication": "saved-subscription-login",
            "model_api_keys_allowed": False,
        }


@dataclass
class AgentExecution:
    final_result: str = ""
    final_message: str = ""
    steps: list[str] = field(default_factory=list)
    turns: int = 0
    command_executions: int = 0
    web_searches: int = 0
    tokens: dict[str, int] = field(default_factory=dict)
    duration_seconds: float = 0.0
    return_code: int | None = None
    timed_out: bool = False
    error: str | None = None
    result_subtype: str | None = None
    reported_cost_usd: float | None = None

    def to_json(self) -> dict[str, object]:
        return asdict(self)


def _version(command: list[str]) -> str:
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            env=subscription_only_env(),
        )
    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        raise AgentValidationError(f"Could not run {' '.join(command)}: {exc}") from exc
    return (result.stdout or result.stderr or "unknown").strip()


def resolve_agent(name: str, model: str | None = None) -> AgentSpec:
    if name not in {"codex", "claude"}:
        raise AgentValidationError(
            f"Unknown local agent {name!r}; expected codex or claude"
        )
    found = shutil.which(name)
    if not found:
        raise AgentValidationError(f"Required local CLI is not on PATH: {name}")
    flag = "--version"
    return AgentSpec(
        name=name,
        cli=Path(found).resolve(),
        version=_version([found, flag]),
        model=model,
    )


def validate_subscription_auth(spec: AgentSpec) -> dict[str, object]:
    """Fail unless the CLI reports account/subscription auth rather than API-key auth."""

    env = subscription_only_env()
    try:
        if spec.name == "codex":
            result = subprocess.run(
                [str(spec.cli), "login", "status"],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                env=env,
            )
            status = (result.stdout or result.stderr or "").strip()
            if "chatgpt" not in status.lower() or "api key" in status.lower():
                raise AgentValidationError(
                    "Codex CLI is not using ChatGPT subscription auth. Run `codex logout`, "
                    "then `codex login` and choose ChatGPT login."
                )
            return {"method": "chatgpt", "status": status}

        result = subprocess.run(
            [str(spec.cli), "auth", "status"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            env=env,
        )
        data = json.loads(result.stdout)
        subscription = data.get("subscriptionType")
        auth_method = str(data.get("authMethod") or "").lower()
        api_provider = str(data.get("apiProvider") or "").lower()
        if (
            not data.get("loggedIn")
            or not subscription
            or api_provider != "firstparty"
            or auth_method
            not in {
                "claude.ai",
                "oauth",
            }
        ):
            raise AgentValidationError(
                "Claude Code is not using a first-party Claude subscription login. "
                "Run `claude auth logout`, then `claude auth login` without --console."
            )
        return {
            "method": auth_method,
            "subscription_type": subscription,
            "api_provider": data.get("apiProvider"),
        }
    except json.JSONDecodeError as exc:
        raise AgentValidationError(
            "Claude auth status did not return valid JSON"
        ) from exc
    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        raise AgentValidationError(
            f"Could not validate {spec.name} subscription auth: {exc}"
        ) from exc


def build_codex_command(
    spec: AgentSpec, *, sandbox: str, system_prompt: str = ""
) -> list[str]:
    command = [
        str(spec.cli),
        "exec",
        "--json",
        "--ephemeral",
        "--sandbox",
        sandbox,
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        *codex_isolation_args(developer_instructions=system_prompt),
    ]
    if spec.model:
        command.extend(["--model", spec.model])
    command.append("-")
    return command


def build_claude_command(
    spec: AgentSpec,
    *,
    system_prompt: str,
    max_turns: int,
) -> list[str]:
    # Do not use --bare: current Claude Code intentionally disables OAuth/keychain auth
    # in bare mode and would force ANTHROPIC_API_KEY.  --safe-mode removes ambient project
    # customization while preserving the user's subscription login.
    command = [
        str(spec.cli),
        "-p",
        "--safe-mode",
        "--output-format",
        "stream-json",
        "--verbose",
        "--max-turns",
        str(max_turns),
        "--dangerously-skip-permissions",
        "--no-session-persistence",
        "--no-chrome",
        "--tools",
        "Bash,Read",
        "--system-prompt",
        system_prompt,
    ]
    if spec.model:
        command.extend(["--model", spec.model])
    return command


def _extract_final_answer(message: str) -> str:
    matches = re.findall(r"^FINAL ANSWER:\s*(.*)$", message or "", flags=re.MULTILINE)
    return matches[-1].strip() if matches else (message or "").strip()


def _compact(value: object, limit: int = 2_000) -> str:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError, OverflowError):
            text = str(value)
    return text.strip()[:limit]


def _codex_event_parser(result: AgentExecution) -> Callable[[dict[str, Any]], None]:
    def parse(event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "turn.completed":
            result.turns += 1
            for key, value in (event.get("usage") or {}).items():
                if isinstance(value, (int, float)):
                    result.tokens[key] = result.tokens.get(key, 0) + int(value)
            return
        if event_type == "turn.failed":
            result.error = f"codex turn failed: {_compact(event.get('error') or event)}"
            return
        if event_type == "error":
            result.error = f"codex error: {_compact(event.get('message') or event)}"
            return
        if event_type != "item.completed":
            return
        item = event.get("item") or {}
        item_type = item.get("type")
        if item_type == "agent_message":
            text = _compact(item.get("text") or "", 40_000)
            if text:
                result.final_message = text
                result.steps.append(f"text: {text[:2_000]}")
        elif item_type == "command_execution":
            result.command_executions += 1
            command = _compact(item.get("command") or "", 1_500)
            output = _compact(
                item.get("aggregated_output") or item.get("output") or "", 500
            )
            result.steps.append(
                f"command: {command}" + (f"\n-> {output}" if output else "")
            )
        elif item_type == "web_search":
            result.web_searches += 1
            result.steps.append(f"web_search: {_compact(item.get('query') or '', 500)}")
        elif item_type:
            result.steps.append(f"{item_type}: {_compact(item, 1_500)}")

    return parse


def _claude_event_parser(result: AgentExecution) -> Callable[[dict[str, Any]], None]:
    def parse(event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "assistant":
            result.turns += 1
            message = event.get("message") or {}
            usage = message.get("usage") or {}
            for key, value in usage.items():
                if isinstance(value, (int, float)):
                    result.tokens[key] = result.tokens.get(key, 0) + int(value)
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text = _compact(block.get("text") or "", 40_000)
                    if text:
                        result.steps.append(f"text: {text[:2_000]}")
                elif block.get("type") == "tool_use":
                    name = str(block.get("name") or "tool")
                    if name.lower() == "bash":
                        result.command_executions += 1
                    if name.lower() in {"websearch", "webfetch"}:
                        result.web_searches += 1
                    result.steps.append(
                        f"{name}: {_compact(block.get('input') or {}, 1_500)}"
                    )
            return
        if event_type == "result":
            result.result_subtype = str(event.get("subtype") or "success")
            result.final_message = _compact(event.get("result") or "", 40_000)
            if event.get("total_cost_usd") is not None:
                try:
                    result.reported_cost_usd = float(event["total_cost_usd"])
                except (TypeError, ValueError):
                    pass
            if event.get("is_error"):
                result.error = (
                    f"claude result {result.result_subtype}: "
                    f"{_compact(event.get('errors') or event.get('result') or event)}"
                )

    return parse


async def _iter_lines(stream: asyncio.StreamReader):
    buffer = bytearray()
    while True:
        chunk = await stream.read(1 << 16)
        if not chunk:
            if buffer:
                yield bytes(buffer)
            return
        buffer.extend(chunk)
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                break
            line = bytes(buffer[:newline])
            del buffer[: newline + 1]
            yield line


async def _consume_jsonl(
    stream: asyncio.StreamReader,
    log: IO[bytes],
    parser: Callable[[dict[str, Any]], None],
) -> None:
    async for raw in _iter_lines(stream):
        log.write(raw + b"\n")
        log.flush()
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            parser(event)


async def _drain(stream: asyncio.StreamReader, log: IO[bytes]) -> None:
    while True:
        chunk = await stream.read(1 << 16)
        if not chunk:
            return
        log.write(chunk)
        log.flush()


async def run_agent(
    spec: AgentSpec,
    *,
    task_description: str,
    system_prompt: str,
    workspace: Path,
    env: Mapping[str, str],
    timeout_seconds: float,
    codex_sandbox: str,
    claude_max_turns: int,
) -> AgentExecution:
    result = AgentExecution()
    command = (
        build_codex_command(spec, sandbox=codex_sandbox, system_prompt=system_prompt)
        if spec.name == "codex"
        else build_claude_command(
            spec, system_prompt=system_prompt, max_turns=claude_max_turns
        )
    )
    prompt = task_description
    parser = (
        _codex_event_parser(result)
        if spec.name == "codex"
        else _claude_event_parser(result)
    )
    stdout_path = workspace.parent / "agent.stdout.jsonl"
    stderr_path = workspace.parent / "agent.stderr.log"
    child_env = subscription_only_env(env)
    proc: asyncio.subprocess.Process | None = None
    started = time.perf_counter()
    try:
        with stdout_path.open("wb") as stdout_log, stderr_path.open("wb") as stderr_log:
            proc = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(workspace),
                env=child_env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=256 * 1024 * 1024,
                **process_group_kwargs(),
            )
            assert (
                proc.stdin is not None
                and proc.stdout is not None
                and proc.stderr is not None
            )
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()

            async def consume() -> None:
                await asyncio.gather(
                    _consume_jsonl(proc.stdout, stdout_log, parser),
                    _drain(proc.stderr, stderr_log),
                    proc.wait(),
                )

            try:
                await asyncio.wait_for(consume(), timeout=timeout_seconds)
            except TimeoutError:
                result.timed_out = True
                result.error = f"Agent timed out after {timeout_seconds:.1f}s"
                await terminate_process_tree(proc)
            result.return_code = proc.returncode
    except asyncio.CancelledError:
        await terminate_process_tree(proc)
        raise
    except Exception as exc:  # noqa: BLE001 - process boundary failures are result data
        await terminate_process_tree(proc)
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        result.duration_seconds = time.perf_counter() - started

    result.final_result = _extract_final_answer(result.final_message)
    if not result.final_result:
        result.final_result = "Agent did not emit a final answer"
    if result.return_code not in (0, None) and not result.error:
        result.error = f"{spec.name} exited with code {result.return_code}"
    return result
