from __future__ import annotations

import os
from pathlib import Path

import pytest

from frameworks import harness_variants as variants


def _fake_root(tmp_path: Path, name: str, project: str, cli_name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "{project}"\nversion = "1.2.3"\n', encoding="utf-8"
    )
    (root / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    scripts = root / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    scripts.mkdir(parents=True)
    (scripts / ("python.exe" if os.name == "nt" else "python")).write_text("")
    (scripts / (f"{cli_name}.exe" if os.name == "nt" else cli_name)).write_text("")
    return root


def test_v1_v2_resolve_to_distinct_contracts(tmp_path, monkeypatch):
    v1 = _fake_root(tmp_path, "v1", "browser-harness", "browser-harness")
    v2 = _fake_root(tmp_path, "v2", "browser-harness-v2", "bh")
    shas = {v1.resolve(): "1" * 40, v2.resolve(): "2" * 40}
    monkeypatch.setattr(variants, "_git_metadata", lambda root: (shas[root], False))
    monkeypatch.setattr(variants, "_run", lambda command, timeout=30: "cli 1.2.3")

    specs = variants.resolve_harnesses({"v1": str(v1), "v2": str(v2)})

    assert specs["v1"].root != specs["v2"].root
    assert specs["v1"].cli.name != specs["v2"].cli.name
    assert specs["v1"].skill_sha256 != specs["v2"].skill_sha256
    assert specs["v1"].git_sha != specs["v2"].git_sha
    assert specs["v1"].daemon_command("x") != specs["v2"].daemon_command("x")


def test_dirty_checkout_fails_before_cli_execution(tmp_path, monkeypatch):
    root = _fake_root(tmp_path, "v1", "browser-harness", "browser-harness")
    monkeypatch.setattr(variants, "_git_metadata", lambda _root: ("a" * 40, True))
    called = False

    def fake_run(command, timeout=30):
        nonlocal called
        called = True
        return "unused"

    monkeypatch.setattr(variants, "_run", fake_run)
    with pytest.raises(variants.HarnessValidationError, match="dirty"):
        variants.resolve_harness("v1", root)
    assert called is False


def test_expected_revision_changes_validation(tmp_path, monkeypatch):
    root = _fake_root(tmp_path, "v1", "browser-harness", "browser-harness")
    monkeypatch.setattr(variants, "_git_metadata", lambda _root: ("a" * 40, False))
    monkeypatch.setattr(variants, "_run", lambda command, timeout=30: "0.1.9")

    variants.resolve_harness("v1", root, expected_git_sha="aaaaaaa")
    with pytest.raises(variants.HarnessValidationError, match="revision mismatch"):
        variants.resolve_harness("v1", root, expected_git_sha="bbbbbbb")


def test_named_values_reject_duplicates_and_unknown_names():
    assert variants.parse_named_values("v1=one,v2=C:\\two", allowed={"v1", "v2"}) == {
        "v1": "one",
        "v2": "C:\\two",
    }
    with pytest.raises(variants.HarnessValidationError, match="Duplicate"):
        variants.parse_named_values("v1=one,v1=two", allowed={"v1", "v2"})
    with pytest.raises(variants.HarnessValidationError, match="Unknown"):
        variants.parse_named_values("v3=three", allowed={"v1", "v2"})
