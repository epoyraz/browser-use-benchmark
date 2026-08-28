"""Resolve and validate Browser Harness checkouts for benchmark runs.

The benchmark never imports either harness package.  A :class:`HarnessSpec`
points at the console script inside that checkout's own virtual environment,
which prevents v1/v2 package collisions and makes the selected source tree an
actual execution input rather than metadata.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path


class HarnessValidationError(ValueError):
    """Raised before a benchmark can spend browser or model time."""


@dataclass(frozen=True)
class _HarnessContract:
    project_name: str
    cli_name: str
    daemon_module: str | None
    prewarm_script: str
    screenshot_example: str


_CONTRACTS = {
    "v1": _HarnessContract(
        project_name="browser-harness",
        cli_name="browser-harness",
        daemon_module="browser_harness.daemon",
        prewarm_script='print(cdp("Browser.getVersion"))\n',
        screenshot_example="capture_screenshot({path})",
    ),
    "v2": _HarnessContract(
        project_name="browser-harness-v2",
        cli_name="bh",
        daemon_module=None,
        prewarm_script='print(cdp("Browser.getVersion"))\n',
        screenshot_example="capture_screenshot({path})",
    ),
}


@dataclass(frozen=True)
class HarnessSpec:
    """Immutable, fully resolved harness execution contract."""

    name: str
    root: Path
    cli: Path
    python: Path
    skill: Path
    git_sha: str
    package_version: str
    skill_sha256: str
    prewarm_script: str
    screenshot_example: str
    command_name: str
    daemon_module: str | None
    dirty: bool = False
    expected_git_sha: str | None = None
    worktree_diff_sha256: str | None = None
    worktree_diff_bytes: int = 0
    worktree_status_sha256: str | None = None

    def daemon_command(self, daemon_name: str) -> list[str]:
        if self.daemon_module:
            return [str(self.python), "-m", self.daemon_module]
        return [str(self.cli), "daemon", daemon_name]

    def to_manifest(self) -> dict[str, object]:
        data = asdict(self)
        for key in ("root", "cli", "python", "skill"):
            data[key] = str(data[key])
        return data


def parse_named_values(
    raw: str, *, allowed: Iterable[str] | None = None
) -> dict[str, str]:
    """Parse ``name=value,name=value`` without treating Windows drive colons specially."""

    allowed_set = set(allowed or ())
    values: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        name, sep, value = part.partition("=")
        name, value = name.strip(), value.strip()
        if not sep or not name or not value:
            raise HarnessValidationError(
                f"Invalid named value {part!r}; expected name=value"
            )
        if allowed_set and name not in allowed_set:
            raise HarnessValidationError(
                f"Unknown name {name!r}; expected one of {', '.join(sorted(allowed_set))}"
            )
        if name in values:
            raise HarnessValidationError(f"Duplicate value for {name!r}")
        values[name] = value
    return values


def _run(command: list[str], *, timeout: float = 30) -> str:
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise HarnessValidationError(f"Command not found: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise HarnessValidationError(f"Command timed out: {' '.join(command)}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise HarnessValidationError(
            f"Command failed ({exc.returncode}): {' '.join(command)}"
            + (f"\n{detail}" if detail else "")
        ) from exc
    return (result.stdout or result.stderr or "").strip()


def _venv_paths(root: Path, cli_name: str) -> tuple[Path, Path]:
    if os.name == "nt":
        scripts = root / ".venv" / "Scripts"
        return scripts / "python.exe", scripts / f"{cli_name}.exe"
    scripts = root / ".venv" / "bin"
    return scripts / "python", scripts / cli_name


def prepare_harness(root: Path, cli_name: str) -> tuple[Path, Path]:
    """Install a checkout into its own venv without changing its lock/source files."""

    python, cli = _venv_paths(root, cli_name)
    uv = shutil.which("uv")
    if not uv:
        raise HarnessValidationError(
            "uv is required for --prepare-harnesses; install uv or prepare .venv manually"
        )
    if not python.is_file():
        _run([uv, "venv", str(root / ".venv"), "--python", sys.executable], timeout=180)
    _run(
        [uv, "pip", "install", "--python", str(python), "--reinstall", str(root)],
        timeout=600,
    )
    if not cli.is_file():
        raise HarnessValidationError(
            f"Harness install completed but did not create expected CLI: {cli}"
        )
    return python, cli


def _git_metadata(root: Path) -> tuple[str, bool]:
    git = shutil.which("git")
    if not git:
        raise HarnessValidationError(
            "git is required to validate pinned harness checkouts"
        )
    sha = _run([git, "-C", str(root), "rev-parse", "HEAD"])
    if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
        raise HarnessValidationError(
            f"Invalid git revision reported for {root}: {sha!r}"
        )
    status = _run(
        [git, "-C", str(root), "status", "--porcelain", "--untracked-files=all"]
    )
    return sha.lower(), bool(status.strip())


def _worktree_fingerprint(root: Path) -> tuple[str, int, str]:
    """Hash, but never persist, a dirty tracked diff and complete porcelain status.

    The patch hash makes an explicitly allowed dirty candidate reproducible enough to
    distinguish two runs at the same HEAD. Porcelain also fingerprints untracked path
    names without reading potentially private untracked contents.
    """
    git = shutil.which("git")
    if not git:
        raise HarnessValidationError("git is required to fingerprint a dirty checkout")
    patch = _run(
        [git, "-C", str(root), "diff", "--binary", "--no-ext-diff", "HEAD", "--", "."]
    )
    status = _run(
        [git, "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"]
    )
    encoded = patch.encode("utf-8")
    return (
        hashlib.sha256(encoded).hexdigest(),
        len(encoded),
        hashlib.sha256(status.encode("utf-8")).hexdigest(),
    )


def resolve_harness(
    name: str,
    root: str | Path,
    *,
    expected_git_sha: str | None = None,
    prepare: bool = False,
    allow_dirty: bool = False,
    allow_unpinned: bool = False,
    allow_sha_mismatch: bool = False,
) -> HarnessSpec:
    """Resolve one harness and fail closed on an ambiguous execution source."""

    if name not in _CONTRACTS:
        raise HarnessValidationError(
            f"Unknown harness {name!r}; expected one of {', '.join(sorted(_CONTRACTS))}"
        )
    contract = _CONTRACTS[name]
    path = Path(root).expanduser().resolve()
    if not path.is_dir():
        raise HarnessValidationError(f"Harness root does not exist: {path}")

    pyproject = path / "pyproject.toml"
    skill = path / "SKILL.md"
    if not pyproject.is_file():
        raise HarnessValidationError(f"Missing harness pyproject.toml: {pyproject}")
    if not skill.is_file():
        raise HarnessValidationError(f"Missing harness skill: {skill}")

    project = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {})
    actual_project = project.get("name")
    if actual_project != contract.project_name:
        raise HarnessValidationError(
            f"{name} expects project {contract.project_name!r}, got {actual_project!r} in {pyproject}"
        )
    declared_version = str(project.get("version") or "unknown")

    unpinned = False
    try:
        git_sha, dirty = _git_metadata(path)
    except HarnessValidationError:
        if not allow_unpinned:
            raise
        git_sha, dirty, unpinned = "unversioned", True, True

    if dirty and not allow_dirty and not unpinned:
        raise HarnessValidationError(
            f"Harness checkout is dirty: {path}. Commit/stash changes, or use "
            "--allow-dirty --yes to record and accept the risk."
        )
    if expected_git_sha and git_sha != "unversioned":
        expected = expected_git_sha.lower()
        if not git_sha.startswith(expected) and not allow_sha_mismatch:
            raise HarnessValidationError(
                f"{name} revision mismatch: expected {expected_git_sha}, found {git_sha}. "
                "Use the requested checkout, or explicitly pass --allow-sha-mismatch --yes."
            )

    diff_sha256 = status_sha256 = None
    diff_bytes = 0
    if dirty and not unpinned:
        diff_sha256, diff_bytes, status_sha256 = _worktree_fingerprint(path)

    python, cli = _venv_paths(path, contract.cli_name)
    if prepare:
        python, cli = prepare_harness(path, contract.cli_name)
    if not python.is_file() or not cli.is_file():
        raise HarnessValidationError(
            f"{name} needs its own installed virtual environment. Missing {cli}. "
            "Run again with --prepare-harnesses or install the checkout into {path / '.venv'}."
        )

    version_output = _run([str(cli), "--version"])
    package_version = version_output or declared_version
    skill_sha256 = hashlib.sha256(skill.read_bytes()).hexdigest()
    return HarnessSpec(
        name=name,
        root=path,
        cli=cli.resolve(),
        # `.absolute()`, never `.resolve()`: a venv's `bin/python` is a symlink to the base
        # interpreter, so resolving it hands back a Python that cannot import the harness.
        # v1 launches its daemon as `python -m browser_harness.daemon`, which then died
        # with ModuleNotFoundError before a single task ran — the venv was defeated by the
        # act of naming it precisely.
        python=Path(os.path.abspath(python)),
        skill=skill.resolve(),
        git_sha=git_sha,
        package_version=package_version,
        skill_sha256=skill_sha256,
        prewarm_script=contract.prewarm_script,
        screenshot_example=contract.screenshot_example,
        command_name=contract.cli_name,
        daemon_module=contract.daemon_module,
        dirty=dirty,
        expected_git_sha=expected_git_sha,
        worktree_diff_sha256=diff_sha256,
        worktree_diff_bytes=diff_bytes,
        worktree_status_sha256=status_sha256,
    )


def resolve_harnesses(
    roots: dict[str, str],
    *,
    expected_shas: dict[str, str] | None = None,
    prepare: bool = False,
    allow_dirty: bool = False,
    allow_unpinned: bool = False,
    allow_sha_mismatch: bool = False,
) -> dict[str, HarnessSpec]:
    if not roots or not set(roots).issubset(_CONTRACTS):
        raise HarnessValidationError(
            "--harnesses must provide v1=<path>, v2=<path>, or both"
        )
    expected_shas = expected_shas or {}
    return {
        name: resolve_harness(
            name,
            roots[name],
            expected_git_sha=expected_shas.get(name),
            prepare=prepare,
            allow_dirty=allow_dirty,
            allow_unpinned=allow_unpinned,
            allow_sha_mismatch=allow_sha_mismatch,
        )
        for name in ("v1", "v2")
        if name in roots
    }
