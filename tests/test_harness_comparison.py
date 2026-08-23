from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from frameworks.harness_variants import HarnessSpec
from harness_benchmark import agents as agent_module
from harness_benchmark.agents import (
    AgentExecution,
    AgentSpec,
    AgentValidationError,
    build_claude_command,
    build_codex_command,
    validate_subscription_auth,
)
from harness_benchmark.browser import BrowserConfig
from harness_benchmark.judge import (
    _claude_judge_command,
    _codex_judge_command,
    _judge_prompt,
)
from harness_benchmark.process import subscription_only_env
from harness_benchmark.runner import (
    BenchmarkTask,
    ComparisonConfig,
    _report_markdown,
    _system_prompt,
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
    codex = AgentSpec(
        "codex", Path("codex"), "1", "gpt-5.6-terra", reasoning_effort="high"
    )
    claude = AgentSpec(
        "claude", Path("claude"), "1", "claude-opus-5", reasoning_effort="max"
    )
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
    assert "gpt-5.6-terra" in codex_cmd
    assert 'model_reasoning_effort="high"' in codex_cmd
    assert "--safe-mode" in claude_cmd
    assert "--bare" not in claude_cmd
    assert "--no-chrome" in claude_cmd
    assert "claude-opus-5" in claude_cmd
    assert claude_cmd[claude_cmd.index("--effort") + 1] == "max"


def test_judge_commands_apply_reasoning_effort(tmp_path):
    codex = AgentSpec(
        "codex", Path("codex"), "1", "gpt-5.6-luna", reasoning_effort="max"
    )
    claude = AgentSpec(
        "claude", Path("claude"), "1", "claude-opus-5", reasoning_effort="high"
    )
    codex_cmd = _codex_judge_command(
        codex,
        schema_path=tmp_path / "schema.json",
        output_path=tmp_path / "output.json",
        screenshots=[],
    )
    claude_cmd = _claude_judge_command(claude)
    assert "gpt-5.6-luna" in codex_cmd
    assert 'model_reasoning_effort="max"' in codex_cmd
    assert claude_cmd[claude_cmd.index("--effort") + 1] == "high"


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


def test_v2_only_plan_and_report_have_no_paired_metrics(tmp_path):
    config = _config(tmp_path)
    config = ComparisonConfig(
        **{
            **config.__dict__,
            "harnesses": {"v2": config.harnesses["v2"]},
            "repeats": 1,
            "record_actions": True,
            "trace_cdp": True,
            "capture_diagnostics": True,
            "sample_processes": True,
        }
    )
    plans = build_cell_plans(config)
    assert len(plans) == 1
    assert [cell.harness_name for cell in plans[0]] == ["v2"]
    manifest = build_manifest(config, plans)
    assert manifest["run_mode"] == "single-harness-telemetry"
    assert manifest["fixed_axes"]["telemetry"]["cdp_round_trip_trace"] is True

    report = build_comparison_report(
        config,
        [_result("codex", "v2", "p", 1, 2.0)],
        wall_clock_seconds=3.0,
    )
    assert report["run_mode"] == "single-harness-telemetry"
    assert len(report["aggregate"]) == 1
    assert report["paired_outcomes"] == {}
    assert report["paired_timing"] == {}
    assert report["category_aggregate"][0]["scored_runs"] == 1
    rendered = _report_markdown(report)
    assert "Raw audit paired outcomes" not in rendered
    assert "single-harness-telemetry" in rendered


def test_search_endpoint_is_explicit_prompt_and_manifest_axis(tmp_path):
    config = _config(tmp_path)
    endpoint = "https://search.example/search?q={query}"
    config = ComparisonConfig(
        **{
            **config.__dict__,
            "harnesses": {"v2": config.harnesses["v2"]},
            "repeats": 1,
            "search_endpoint": endpoint,
        }
    )
    manifest = build_manifest(config, build_cell_plans(config))
    assert manifest["fixed_axes"]["discovery"]["search_endpoint"] == endpoint

    prompt = _system_prompt(
        config.harnesses["v2"], tmp_path / "screenshots", search_endpoint=endpoint
    )
    assert endpoint in prompt
    assert "configured HTML search page" in prompt
    assert "benchmark-probed" not in prompt
    assert "URL-encoded query" in prompt
    assert "through `bh`" in prompt
    assert "do not import helpers from the `harness` package" in prompt
    assert "ordinary cookie/privacy banner is not such a" in prompt
    assert "reject non-essential cookies" in prompt


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


def _result(
    agent: str,
    harness: str,
    pair: str,
    score: int,
    seconds: float,
    *,
    order: int = 0,
    captcha: bool = False,
    failure_reason: str = "",
    task_id: str = "task-a",
    task_index: int = 0,
    repetition: int = 0,
):
    return {
        "pair_id": pair,
        "cell_id": f"{pair}-{harness}",
        "score": score,
        "provenance": {
            "agent": {"name": agent},
            "harness": {"name": harness},
            "benchmark": {
                "benchmark_index": task_index,
                "task_id": task_id,
                "category": "WebBenchREAD",
                "repetition": repetition,
                "order": order,
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
        "judgement": {
            "reached_captcha": captcha,
            "impossible_task": False,
            "failure_reason": failure_reason,
            "error": None,
        },
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
    assert report["task_scoring"]["scored_tasks"] == 1
    assert report["paired_timing"]["codex"]["pairs"] == 1


def test_report_separates_captcha_from_capability_and_flags_order(tmp_path):
    config = _config(tmp_path)
    config = ComparisonConfig(**{**config.__dict__, "repeats": 1})
    report = build_comparison_report(
        config,
        [
            _result("codex", "v1", "p", 1, 2.0, order=0),
            _result(
                "codex",
                "v2",
                "p",
                0,
                1.0,
                order=1,
                captcha=True,
                failure_reason="site challenge",
            ),
        ],
        wall_clock_seconds=4.0,
    )

    assert report["paired_outcomes"]["codex"]["v1_pass_v2_fail"] == 1
    assert (
        report["capability_paired_outcomes"]["codex"]["excluded_task_pairs"]
        == 1
    )
    rows = {(row["agent"], row["harness"]): row for row in report["aggregate"]}
    assert rows[("codex", "v1")]["capability_eligible_runs"] == 0
    assert rows[("codex", "v2")]["capability_eligible_runs"] == 0
    assert rows[("codex", "v2")]["capability_pass_rate"] is None
    assert report["order_effects"][0]["captcha_rate"] == 0.0
    assert report["order_effects"][1]["captcha_rate"] == 1.0
    assert report["task_scoring"]["selected_tasks"] == 1
    assert report["task_scoring"]["scored_tasks"] == 0
    assert report["task_scoring"]["excluded_task_count"] == 1
    assert report["per_task"][0]["capability_blocker"] == "captcha-task"
    assert report["per_task"][1]["capability_blocker"] == "captcha-task"
    assert all(row["excluded_from_scoring"] for row in report["per_task"])
    assert report["paired_timing"]["codex"]["pairs"] == 0
    assert report["paired_timing"]["codex"]["raw_pairs"] == 1
    assert any("order confound" in warning for warning in report["validity_warnings"])
    assert any("no benchmark score" in warning.lower() for warning in report["validity_warnings"])


def test_captcha_excludes_task_across_agents_and_clean_cells(tmp_path):
    config = _config(tmp_path)
    config = ComparisonConfig(
        **{
            **config.__dict__,
            "agents": {
                "codex": AgentSpec("codex", Path("codex"), "1", "model-a"),
                "claude": AgentSpec("claude", Path("claude"), "1", "model-b"),
            },
            "repeats": 1,
        }
    )
    report = build_comparison_report(
        config,
        [
            _result("codex", "v1", "codex-p", 1, 2.0, order=0),
            _result(
                "codex", "v2", "codex-p", 0, 1.0, order=1, captcha=True
            ),
            _result("claude", "v2", "claude-p", 1, 2.5, order=0),
            _result("claude", "v1", "claude-p", 1, 3.0, order=1),
        ],
        wall_clock_seconds=9.0,
    )

    assert report["task_scoring"]["excluded_task_count"] == 1
    assert len(report["task_scoring"]["excluded_tasks"][0]["captcha_cells"]) == 1
    assert all(row["capability_eligible_runs"] == 0 for row in report["aggregate"])
    assert (
        report["capability_paired_outcomes"]["claude"]["excluded_task_pairs"]
        == 1
    )
    assert all(row["excluded_from_scoring"] for row in report["per_task"])


def test_judge_receives_the_full_delivered_response_not_only_the_marker():
    execution = AgentExecution(
        final_message=(
            "Three verified items:\n- Alpha: https://example.test/a\n"
            "- Beta: https://example.test/b\nFINAL ANSWER: Example Place"
        ),
        final_result="Example Place",
        steps=["text: researched the requested items"],
    )
    prompt = _judge_prompt(
        task_description="Return the place and item links",
        ground_truth=None,
        execution=execution,
        screenshots=[],
    )

    assert "<final_response>" in prompt
    assert "Alpha: https://example.test/a" in prompt
    assert "<final_answer_marker>\nExample Place" in prompt
    assert "must not erase correct details" in prompt
