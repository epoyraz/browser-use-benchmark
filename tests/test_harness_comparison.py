from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from frameworks.harness_variants import HarnessSpec
from harness_benchmark import agents as agent_module
from harness_benchmark.agents import (
    AgentSpec,
    AgentValidationError,
    build_claude_command,
    build_codex_command,
    validate_subscription_auth,
)
from harness_benchmark.browser import BrowserConfig
from harness_benchmark.process import subscription_only_env
from harness_benchmark.runner import (
    BenchmarkTask,
    ComparisonConfig,
    build_cell_plans,
    build_comparison_report,
    build_manifest,
    ordered_harnesses,
)


def _harness(tmp_path: Path, name: str, sha: str) -> HarnessSpec:
    root = tmp_path / name
    root.mkdir()
    skill = root / "SKILL.md"
    skill.write_text(name, encoding="utf-8")
    command = "browser-harness" if name == "v1" else "bh"
    return HarnessSpec(
        name=name,
        root=root,
        cli=root / command,
        python=root / "python",
        skill=skill,
        git_sha=sha,
        package_version="test",
        skill_sha256=name * 8,
        prewarm_script="print(1)",
        screenshot_example="capture_screenshot({path})",
        command_name=command,
        daemon_module="browser_harness.daemon" if name == "v1" else None,
    )


def _config(tmp_path: Path) -> ComparisonConfig:
    task = BenchmarkTask(0, 0, "task-a", "WebBenchREAD", "read a page")
    return ComparisonConfig(
        comparison_id="comparison-test",
        benchmark="BU_Bench_V1",
        harnesses={
            "v1": _harness(tmp_path, "v1", "1" * 40),
            "v2": _harness(tmp_path, "v2", "2" * 40),
        },
        agents={"codex": AgentSpec("codex", Path("codex"), "1", "model-a")},
        judge=None,
        browser=BrowserConfig(Path("chrome")),
        tasks=[task],
        repeats=2,
        paired_order="alternate",
        seed=7,
        execution_mode="parallel",
        workers=2,
        task_timeout_seconds=10,
        judge_timeout_seconds=10,
        codex_sandbox="danger-full-access",
        claude_max_turns=10,
        measurement_scope="read-only-capability",
        output_root=tmp_path,
        auth_status={"codex": {"method": "chatgpt"}},
    )


def test_subscription_environment_removes_all_api_credentials(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setenv("CODEX_API_KEY", "secret")
    monkeypatch.setenv("CODEX_SESSION_ID", "parent-session")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("ANTHROPIC_MODEL", "api-routed-model")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("BU_AUTOSPAWN", "1")
    monkeypatch.setenv("BU_BROWSER_ID", "cloud-browser")
    monkeypatch.setenv("BU_CDP_WS", "wss://cloud.example/devtools/browser/1")
    monkeypatch.setenv("BROWSER_USE_CLOUD_API_URL", "https://cloud.example")
    env = subscription_only_env(
        {
            "SOME_VENDOR_API_KEY": "also-secret",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CODEX_HOME": "saved-login-home",
            "BU_CDP_URL": "http://127.0.0.1:12345",
            "KEEP_ME": "yes",
        }
    )
    assert "OPENAI_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "CODEX_API_KEY" not in env
    assert "CODEX_SESSION_ID" not in env
    assert "CLAUDECODE" not in env
    assert "ANTHROPIC_MODEL" not in env
    assert "CLAUDE_CODE_USE_BEDROCK" not in env
    assert "BU_AUTOSPAWN" not in env
    assert "BU_BROWSER_ID" not in env
    assert "BU_CDP_WS" not in env
    assert "BROWSER_USE_CLOUD_API_URL" not in env
    assert "SOME_VENDOR_API_KEY" not in env
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert env["CODEX_HOME"] == "saved-login-home"
    assert env["BU_CDP_URL"] == "http://127.0.0.1:12345"
    assert env["KEEP_ME"] == "yes"


def test_claude_auth_rejects_non_first_party_provider(monkeypatch):
    payload = {
        "loggedIn": True,
        "subscriptionType": "max",
        "authMethod": "claude.ai",
        "apiProvider": "bedrock",
    }
    monkeypatch.setattr(
        agent_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=agent_module.json.dumps(payload), stderr=""
        ),
    )

    with pytest.raises(AgentValidationError, match="first-party"):
        validate_subscription_auth(AgentSpec("claude", Path("claude"), "1"))


def test_agent_commands_are_local_subscription_safe():
    codex = AgentSpec("codex", Path("codex"), "1")
    claude = AgentSpec("claude", Path("claude"), "1")
    codex_cmd = build_codex_command(
        codex, sandbox="danger-full-access", system_prompt="fixed-system-prompt"
    )
    claude_cmd = build_claude_command(claude, system_prompt="fixed", max_turns=3)
    assert codex_cmd[:2] == ["codex", "exec"]
    assert "--ignore-user-config" in codex_cmd
    assert "--ephemeral" in codex_cmd
    assert "shell_environment_policy.inherit=all" in codex_cmd
    assert 'forced_login_method="chatgpt"' in codex_cmd
    assert 'web_search="disabled"' in codex_cmd
    assert "agents.enabled=false" in codex_cmd
    assert "features.multi_agent=false" in codex_cmd
    assert "features.apps=false" in codex_cmd
    assert 'developer_instructions="fixed-system-prompt"' in codex_cmd
    assert "--safe-mode" in claude_cmd
    assert "--bare" not in claude_cmd
    assert "--no-chrome" in claude_cmd


def test_pair_order_is_deterministic_and_arms_never_share_identity(tmp_path):
    config = _config(tmp_path)
    pairs = build_cell_plans(config)
    assert len(pairs) == 2
    assert [cell.harness_name for cell in pairs[0]] == ["v1", "v2"]
    assert [cell.harness_name for cell in pairs[1]] == ["v2", "v1"]
    assert len({cell.cell_id for pair in pairs for cell in pair}) == 4
    assert ordered_harnesses(
        "randomized", pair_ordinal=8, repetition=2, seed=4
    ) == ordered_harnesses("randomized", pair_ordinal=8, repetition=2, seed=4)


def test_manifest_records_noncolliding_harness_revision_identity(tmp_path):
    config = _config(tmp_path)
    manifest = build_manifest(config, build_cell_plans(config))
    assert (
        manifest["harnesses"]["v1"]["git_sha"] != manifest["harnesses"]["v2"]["git_sha"]
    )
    assert manifest["api_policy"]["model_api_keys_removed_from_children"] is True
    assert (
        manifest["api_policy"]["browser_provider_cloud_selectors_removed_from_children"]
        is True
    )
    assert manifest["api_policy"]["browser_provider_api_used"] is False
    assert len(manifest["fixed_axes"]["workspace_instruction_override_sha256"]) == 64
    assert manifest["fixed_axes"]["limits"]["codex_forced_login_method"] == "chatgpt"
    assert len({cell["cell_id"] for cell in manifest["cells"]}) == len(
        manifest["cells"]
    )


def _result(agent: str, harness: str, pair: str, score: int, seconds: float):
    return {
        "pair_id": pair,
        "cell_id": f"{pair}-{harness}",
        "score": score,
        "provenance": {
            "agent": {"name": agent},
            "harness": {"name": harness},
            "benchmark": {
                "benchmark_index": 0,
                "task_id": "task-a",
                "repetition": 0,
            },
        },
        "timing": {
            "setup_seconds": 0.25,
            "agent_seconds": seconds,
            "teardown_seconds": 0.25,
            "judge_seconds": 0.5,
            "total_seconds": seconds + 1,
        },
        "metrics": {
            "agent_turns": 2,
            "command_executions": 3,
            "tokens": {"input_tokens": 10, "output_tokens": 4},
            "screenshot_count": 1,
            "technical_failure_class": None,
            "safety_refusal": False,
        },
        "judgement": {"reached_captcha": False, "impossible_task": False},
    }


def test_report_contains_paired_quality_and_time(tmp_path):
    config = _config(tmp_path)
    config = ComparisonConfig(**{**config.__dict__, "repeats": 1})
    report = build_comparison_report(
        config,
        [_result("codex", "v1", "p", 1, 2.0), _result("codex", "v2", "p", 0, 4.0)],
        wall_clock_seconds=6.0,
    )
    assert report["paired_outcomes"]["codex"]["v1_pass_v2_fail"] == 1
    assert (
        report["paired_timing"]["codex"]["mean_agent_seconds_delta_v2_minus_v1"] == 2.0
    )
    rows = {(row["agent"], row["harness"]): row for row in report["aggregate"]}
    assert rows[("codex", "v1")]["mean_agent_seconds"] == 2.0
    assert rows[("codex", "v2")]["pass_rate"] == 0.0
    assert rows[("codex", "v1")]["token_totals"]["input_tokens"] == 10
