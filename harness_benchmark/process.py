"""Subprocess helpers shared by agent, judge, browser, and daemon lifecycles."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
from collections.abc import Mapping
from pathlib import Path

_MODEL_CREDENTIAL_NAMES = {
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_VERTEX",
    "CODEX_ACCESS_TOKEN",
    "OPENAI_BASE_URL",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "GOOGLE_APPLICATION_CREDENTIALS",
}

_PROVIDER_ENV_PREFIXES = (
    "ANTHROPIC_",
    "AWS_",
    "AZURE_OPENAI_",
    "OPENAI_",
    "VERTEXAI_",
)

_CLAUDE_CODE_ENV_ALLOWLIST = {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    "CLAUDE_CODE_GIT_BASH_PATH",
}

_CODEX_ENV_ALLOWLIST = {
    "CODEX_CA_CERTIFICATE",
    "CODEX_HOME",
}

_BROWSER_PROVIDER_ENV_NAMES = {
    # v1 treats BU_BROWSER_ID as an instruction to attach to a Browser Use cloud
    # browser, and BU_AUTOSPAWN can provision one when credentials are present.
    "BU_AUTOSPAWN",
    "BU_BROWSER_ID",
    # A parent websocket endpoint takes precedence over the benchmark-owned local
    # HTTP endpoint in both harnesses.  It therefore must never cross the boundary.
    "BU_CDP_WS",
    "BROWSER_USE_CLOUD_API_URL",
}


def subscription_only_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return an environment that cannot silently switch either CLI to API billing.

    Saved CLI login state remains available. Explicit credentials and provider switches
    do not. Benchmark tasks are browser-only, so removing all ``*_API_KEY`` variables
    also prevents a model from bypassing the harness with an unrelated API. Browser
    cloud selectors are removed while the runner's explicit local ``BU_CDP_URL`` is
    retained.
    """

    env = dict(os.environ)
    if extra:
        env.update({str(key): str(value) for key, value in extra.items()})
    for key in list(env):
        upper = key.upper()
        if (
            upper in _MODEL_CREDENTIAL_NAMES
            or upper in _BROWSER_PROVIDER_ENV_NAMES
            or upper == "CLAUDECODE"
            or (upper.startswith("CODEX_") and upper not in _CODEX_ENV_ALLOWLIST)
            or (
                upper.startswith("CLAUDE_CODE_")
                and upper not in _CLAUDE_CODE_ENV_ALLOWLIST
            )
            or upper.startswith(_PROVIDER_ENV_PREFIXES)
            or upper.endswith(("_API_KEY", "_AUTH_TOKEN", "_ACCESS_TOKEN"))
        ):
            env.pop(key, None)
    return env


def process_group_kwargs() -> dict[str, object]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


async def terminate_process_tree(
    proc: asyncio.subprocess.Process | None,
    *,
    grace_seconds: float = 5.0,
) -> None:
    """Stop exactly one spawned process group/tree, escalating after a short grace."""

    if proc is None or proc.returncode is not None:
        return
    if os.name == "nt":
        taskkill = shutil.which("taskkill")
        if taskkill:
            killer = await asyncio.create_subprocess_exec(
                taskkill,
                "/PID",
                str(proc.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(killer.wait(), timeout=grace_seconds)
            except TimeoutError:
                killer.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
            except TimeoutError:
                proc.kill()
            return
        proc.terminate()
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
    except TimeoutError:
        if os.name == "nt":
            proc.kill()
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
        except TimeoutError:
            pass


async def run_captured(
    command: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    input_text: str = "",
    timeout: float = 60.0,
) -> tuple[int, str, str, bool]:
    """Run a bounded command and capture UTF-8 output without invoking a shell."""

    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            env=dict(env),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=256 * 1024 * 1024,
            **process_group_kwargs(),
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input_text.encode("utf-8")), timeout=timeout
        )
        return (
            int(proc.returncode or 0),
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
            False,
        )
    except TimeoutError:
        await terminate_process_tree(proc)
        return -1, "", f"Timed out after {timeout:.1f}s", True
    except asyncio.CancelledError:
        await terminate_process_tree(proc)
        raise
