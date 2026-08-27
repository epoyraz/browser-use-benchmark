"""Run one benchmark task through browser-harness **v2** — or v1, as the control.

This adapter exists to answer one question: does v2 need fewer model calls than v1 for the
same task, at the same or better reliability and speed? Every other framework here
benchmarks a whole agent stack. This one holds the stack fixed and moves a single part.

    same Codex CLI, same model, same task, same judge, same browser
                          only `harness=v1|v2` differs

That is why v1 is reachable from this adapter rather than from `codex_harness/`. The
existing `codex-harness` adapter drives v1 against **browser-use-cloud**, so comparing it
with a local v2 run would confound the harness with the browser it talks to. Here both arms
attach to one scratch Chrome launched per task, pinned by `BU_CDP_WS` — the env var both
harnesses already honour as an explicit endpoint.

The Codex event schema, price map and step formatting are *imported* from
`frameworks.codex_harness.run_task`, not copied. A second transcription of that schema
would let the two arms drift, and a difference in reported steps has to mean the agent
behaved differently — never that we parsed its output differently.

Prerequisites:
    - Codex CLI on PATH, authenticated (`~/.codex/auth.json`) or `CODEX_API_KEY` set.
    - browser-harness v2 checked out with its venv (`uv sync`) — `harness_dir` below.
    - For `harness=v1`, browser-harness v1 likewise, at `harness_v1_dir`.
    - Google Chrome installed. Nothing is provisioned remotely; no provider key is used.
    - `GOOGLE_API_KEY` for the shared judge. Without it the run still executes and writes
      every metric, and only the score is unavailable — see `run_and_judge`.

Example:
    uv run python run_framework_eval.py --framework bh-harness --model gpt-5 --tasks 5
    uv run python run_framework_eval.py --framework bh-harness --model gpt-5 --tasks 5 \
        --params harness=v1
"""

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Add project root to path for sibling imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from dotenv import load_dotenv
from frameworks import (
    ExecutionResult,
    interleave,
    load_tasks,
    parse_params,
    run_and_judge,
    validate_params,
)

# One definition of Codex's event schema for both arms — see the module docstring.
from frameworks.codex_harness.run_task import (
    FINAL_ANSWER_RE,
    _build_codex_cmd,
    _drain_stderr,
    _format_item,
    _model_price,
)

load_dotenv()

DEV_ROOT = Path.home() / "Desktop" / "Dev" / "browser-harness"

ACCEPTED_PARAMS: dict[str, str] = {
    "harness": "Which harness the agent drives: v2 (default) or v1. The only intended "
               "difference between the two arms of the comparison.",
    "harness_dir": f"browser-harness v2 checkout (default: {DEV_ROOT / 'v2'}).",
    "harness_v1_dir": f"browser-harness v1 checkout (default: {DEV_ROOT / 'v1'}).",
    "task_timeout": "Per-task wall-clock timeout in seconds, sets TASK_TIMEOUT for "
                    "run_and_judge (default: 1800).",
    "sandbox": "Codex sandbox policy (read-only | workspace-write | danger-full-access; "
               "default: danger-full-access).",
    "headless": "1 to run Chrome headless (default: 0). Headful is the default because "
                "that is what both harnesses are normally used against.",
    "keep_profile": "1 to keep the scratch Chrome profile after the task, for debugging.",
}

PROMPTS = Path(__file__).resolve().parent


class Arm:
    """One side of the comparison: a harness checkout and how an agent invokes it."""

    def __init__(self, key: str, params: dict[str, str]):
        if key not in ("v1", "v2"):
            raise ValueError(f"harness must be 'v1' or 'v2', not {key!r}")
        self.key = key
        default = DEV_ROOT / key
        param = "harness_dir" if key == "v2" else "harness_v1_dir"
        self.dir = Path(params.get(param, str(default))).expanduser()
        self.command = "bh" if key == "v2" else "browser-harness"
        self.prompt_file = PROMPTS / f"system_prompt_{key}.md"

    @property
    def venv_bin(self) -> Path:
        return self.dir / ".venv" / "bin"

    def check(self) -> None:
        """Fail here rather than inside Codex, where it reads as a task failure.

        A missing console script is the difference between "v2 scored 0/5" and "v2 was
        never run", and only one of those is a benchmark result.
        """
        if not self.dir.is_dir():
            raise FileNotFoundError(f"harness {self.key} checkout not found: {self.dir}")
        if not (self.venv_bin / self.command).exists():
            raise FileNotFoundError(
                f"{self.command!r} not found in {self.venv_bin} — run `uv sync` in "
                f"{self.dir} so the console script exists")
        if not self.prompt_file.exists():
            raise FileNotFoundError(f"missing system prompt: {self.prompt_file}")


def _shots_dir(task_index: str) -> Path:
    """Screenshots, scoped per task.

    `codex_harness` drains a single `/tmp/shots`, which is correct for one task at a time
    and silently mixes traces under `--parallel`. The judge sees these images, so a
    neighbour's screenshots are not a cosmetic problem.
    """
    return Path(tempfile.gettempdir()) / f"bh-shots-{task_index}"


def _reset(directory: Path) -> None:
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)


def _collect_screenshots(directory: Path) -> list[str]:
    if not directory.exists():
        return []
    return [base64.b64encode(p.read_bytes()).decode()
            for p in sorted(p for p in directory.glob("*.png") if p.is_file())]


def _launch_chrome(profile: Path, *, headless: bool) -> str:
    """Start one scratch Chrome and return its CDP WebSocket URL.

    Launching is v2's `tests/live/_browser.py` rather than a fresh `subprocess.Popen`
    here: that module encodes why Chrome must not be a child of the test process on
    macOS (an unanswered TCC prompt revokes the *terminal's* file access, and the
    failure surfaces hours later as a venv read error). Reimplementing the launch to
    avoid an import is how that lesson gets paid for twice.

    A scratch profile per task is also not hygiene. Chrome M144 grants remote-debugging
    consent per websocket and refuses a second one to an already-authorised browser, so
    two tasks sharing a profile is two tasks where the second cannot connect.
    """
    v2 = Path(os.environ["BH_V2_DIR"])
    sys.path.insert(0, str(v2))
    sys.path.insert(0, str(v2 / "tests" / "live"))
    import _browser  # type: ignore
    from harness.connect.endpoint import read_active_port  # type: ignore

    os.environ["BH_HEADLESS"] = "1" if headless else "0"
    _browser.launch(profile, window="1400,1000")
    active = read_active_port(profile)
    if active is None:
        _browser.kill(profile)
        raise RuntimeError(f"Chrome started but published no DevToolsActivePort in {profile}")
    port, path = active
    return f"ws://127.0.0.1:{port}{path}"


def _kill_chrome(profile: Path) -> None:
    try:
        import _browser  # type: ignore

        _browser.kill(profile)
    except Exception as error:                                   # noqa: BLE001
        print(f"[bh-runner] failed to stop Chrome for {profile}: {error}", flush=True)


def _kill_daemon(bu_name: str) -> None:
    """Both harnesses leave a daemon behind that outlives its client on purpose.

    v2's exits when its browser dies, so killing Chrome is normally enough. `normally` is
    not a teardown guarantee, and a daemon per task accumulates: 38 orphans from one unit
    test were what surfaced this. Match on the daemon's own name so the blast radius is
    this task and nothing else.
    """
    for pattern in (f"daemon {bu_name}", f"--name {bu_name}"):
        subprocess.run(["/usr/bin/pkill", "-f", "--", pattern], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _compose_prompt(arm: Arm, task_description: str, shots: Path) -> str:
    """Codex has no `--append-system-prompt-file`; the rules ride the user prompt.

    The shots directory is substituted rather than hardcoded, because it is per task.
    """
    system = arm.prompt_file.read_text(encoding="utf-8").replace("__SHOTS_DIR__", str(shots))
    return f"{system}\n\n---\n\nTask:\n{task_description}\n"


async def execute(task_description: str) -> ExecutionResult:
    params = validate_params(parse_params(), ACCEPTED_PARAMS)
    model_name = os.environ["MODEL"]
    task_index = os.environ.get("TASK_INDEX", "0")
    arm = Arm(params.get("harness", "v2"), params)
    arm.check()

    # `_launch_chrome` reads this: the launcher lives in the v2 tree and is used by both
    # arms, since what it knows about starting Chrome is not v2-specific.
    os.environ["BH_V2_DIR"] = str(Path(params.get("harness_dir", str(DEV_ROOT / "v2"))))

    bu_name = f"eval-{arm.key}-{task_index}"
    shots = _shots_dir(task_index)
    profile = Path(tempfile.gettempdir()) / f"bh-eval-{arm.key}-{task_index}"
    _reset(shots)
    _reset(profile)

    ws_url = _launch_chrome(profile, headless=params.get("headless", "0") == "1")
    journal = shots.parent / f"{bu_name}.journal.jsonl"

    try:
        env = {
            **os.environ,
            # The pin is the experiment's control: both harnesses honour BU_CDP_WS as an
            # explicit endpoint, so neither arm gets to discover a different browser.
            "BU_CDP_WS": ws_url,
            "BU_NAME": bu_name,
            "BH_JOURNAL": str(journal),
            # v1 prints "update available -> run browser-harness --update" on startup.
            # That is noise in the transcript and, worse, an instruction to do something
            # the task rules forbid; an agent that follows it spends steps we would then
            # be attributing to the harness.
            "BH_UPDATE_CHECK": "0",
            "CODEX_API_KEY": os.environ.get("CODEX_API_KEY")
            or os.environ.get("OPENAI_API_KEY", ""),
            # Codex does not inherit the `uv run` PATH boost, and an agent that has to
            # rediscover its own CLI spends real steps doing it.
            "PATH": os.pathsep.join([str(arm.venv_bin), os.environ.get("PATH", "")]),
        }
        cmd = _build_codex_cmd(model_name, params.get("sandbox", "danger-full-access"))
        prompt = _compose_prompt(arm, task_description, shots)
    except Exception:
        _kill_chrome(profile)
        raise

    start = time.time()
    steps: list[str] = []
    final_text = ""
    tokens = {"input": 0, "cached_input": 0, "output": 0, "reasoning": 0}
    counts = {"turns": 0, "commands": 0, "messages": 0, "reasoning_items": 0}
    turn_failed_error: str | None = None
    error_events: list[str] = []
    stderr_buf: list[str] = []

    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(arm.dir),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=256 * 1024 * 1024,
        )
        assert proc.stdin is not None
        proc.stdin.write(prompt.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()
        stderr_task = asyncio.create_task(_drain_stderr(proc, stderr_buf))
    except Exception:
        if proc is not None and proc.returncode is None:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                pass
        _kill_chrome(profile)
        _kill_daemon(bu_name)
        raise

    async def _lines():
        """One JSONL line at a time, with no per-line cap: a `command_execution` item
        carries the command's aggregated output, which for a page dump is large."""
        assert proc.stdout is not None
        buf = bytearray()
        while True:
            chunk = await proc.stdout.read(1 << 16)
            if not chunk:
                if buf:
                    yield bytes(buf)
                return
            buf.extend(chunk)
            while (nl := buf.find(b"\n")) >= 0:
                line = bytes(buf[:nl])
                del buf[: nl + 1]
                yield line

    try:
        async for raw in _lines():
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                print(f"[codex-stdout-raw] {line}", flush=True)
                continue

            etype = event.get("type")
            if etype == "item.completed":
                item = event.get("item") or {}
                if formatted := _format_item(item):
                    steps.append(formatted)
                    print(f"[step {len(steps):>3}] {formatted[:500]}", flush=True)
                itype = item.get("type")
                if itype == "command_execution":
                    counts["commands"] += 1
                elif itype == "reasoning":
                    counts["reasoning_items"] += 1
                elif itype == "agent_message":
                    counts["messages"] += 1
                    if text := (item.get("text") or "").strip():
                        final_text = text
            elif etype == "turn.completed":
                usage = event.get("usage") or {}
                counts["turns"] += 1
                tokens["input"] += int(usage.get("input_tokens") or 0)
                tokens["cached_input"] += int(usage.get("cached_input_tokens") or 0)
                tokens["output"] += int(usage.get("output_tokens") or 0)
                tokens["reasoning"] += int(usage.get("reasoning_output_tokens") or 0)
            elif etype == "turn.failed":
                turn_failed_error = json.dumps(event.get("error") or {}, default=str)[:500]
                print(f"[codex-turn-failed] {turn_failed_error}", flush=True)
            elif etype == "error":
                message = event.get("message") or json.dumps(event, default=str)
                error_events.append(str(message)[:500])
                print(f"[codex-error] {message}", flush=True)

        try:
            await asyncio.wait_for(proc.wait(), timeout=60)
        except asyncio.TimeoutError:
            print("[bh-runner] codex did not exit within 60s of stdout close; killing",
                  flush=True)
            proc.kill()
            await proc.wait()
        try:
            await asyncio.wait_for(stderr_task, timeout=10)
        except asyncio.TimeoutError:
            stderr_task.cancel()
    finally:
        if proc.returncode is None:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                pass
        if not stderr_task.done():
            stderr_task.cancel()
        _kill_chrome(profile)
        _kill_daemon(bu_name)
        if params.get("keep_profile") != "1":
            shutil.rmtree(profile, ignore_errors=True)

    duration = time.time() - start
    stderr_tail = "\n".join(stderr_buf[-50:])

    prices = _model_price(model_name)
    if prices:
        non_cached = max(0, tokens["input"] - tokens["cached_input"])
        cost = (non_cached * prices["input"]
                + tokens["cached_input"] * prices.get("cached_input", prices["input"])
                + tokens["output"] * prices["output"])
    else:
        # Not a guess. An unpriced model reports zero rather than an invented rate; the
        # comparison this adapter exists for is denominated in calls and tokens anyway.
        cost = 0.0

    _write_metrics(arm, task_index, counts, tokens, duration, journal, ws_url, model_name)

    match = FINAL_ANSWER_RE.search(final_text or "")
    answer = match.group(1).strip() if match else (final_text.strip() or "")
    if turn_failed_error and not answer:
        final_result = f"[codex_turn_failed] {turn_failed_error}"
    elif error_events and not answer:
        final_result = f"[codex_error] {error_events[-1]}"
    elif not final_text:
        if proc.returncode not in (0, None):
            raise RuntimeError(
                f"codex exited with code {proc.returncode} and emitted no agent_message. "
                f"steps_captured={len(steps)} duration={duration:.1f}s "
                f"stderr_tail:\n{stderr_tail[-2000:]}")
        final_result = "Agent did not emit any output"
    else:
        final_result = answer or "Agent did not emit FINAL ANSWER line"

    return ExecutionResult(
        final_result=final_result,
        steps=steps,
        screenshots_b64=_collect_screenshots(shots),
        num_steps=len(steps),
        duration_seconds=duration,
        cost=cost,
    )


def _journal_summary(path: Path) -> dict[str, int] | None:
    """What the harness itself did, when it says so.

    v2 journals every helper call and CDP round trip; v1 has no equivalent, so this is
    present for one arm only and must never be compared across arms. The numbers that
    *are* comparable — commands, turns, tokens, duration — come from the agent transcript,
    which both arms produce identically.
    """
    if not path.exists():
        return None
    kinds: dict[str, int] = {}
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    kind = json.loads(line).get("kind")
                except json.JSONDecodeError:
                    continue
                if isinstance(kind, str):
                    kinds[kind] = kinds.get(kind, 0) + 1
    except OSError:
        return None
    return kinds or None


def _write_metrics(arm: Arm, task_index: str, counts: dict, tokens: dict, duration: float,
                   journal: Path, ws_url: str, model_name: str) -> None:
    """A sidecar, because `ExecutionResult` has no room and is shared with every adapter.

    `steps` in the benchmark summary counts formatted transcript entries, which mixes
    reasoning with commands. The question this adapter exists to answer needs them apart.
    """
    run_data = os.environ.get("RUN_DATA_DIR")
    if not run_data:
        return
    payload = {
        "harness": arm.key,
        "harness_dir": str(arm.dir),
        "model": model_name,
        "task_index": int(task_index),
        # ws_url is a capability, so only its shape is recorded — enough to confirm both
        # arms attached to a local scratch browser, not enough to drive one.
        "browser": "local-scratch-chrome" if ws_url.startswith("ws://127.0.0.1:") else "other",
        "agent_turns": counts["turns"],
        "commands": counts["commands"],
        "agent_messages": counts["messages"],
        "reasoning_items": counts["reasoning_items"],
        "tokens": tokens,
        "duration_seconds": round(duration, 2),
        "harness_journal": _journal_summary(journal),
    }
    path = Path(run_data) / f"task_{task_index}.{arm.key}.metrics.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"[bh-metrics] {json.dumps({k: v for k, v in payload.items() if k != 'tokens'})}",
          flush=True)


async def main():
    task_index = int(os.environ["TASK_INDEX"])
    benchmark = os.environ.get("BENCHMARK", "BU_Bench_V1")

    early_params = parse_params()
    if "task_timeout" in early_params:
        os.environ["TASK_TIMEOUT"] = early_params["task_timeout"]

    tasks = load_tasks(benchmark)
    if len(tasks) == 100:
        tasks = interleave(tasks)
    task = tasks[task_index]
    task["_index"] = task_index

    await run_and_judge(task, execute)


if __name__ == "__main__":
    asyncio.run(main())
