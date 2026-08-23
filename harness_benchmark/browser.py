"""Fresh, benchmark-owned local Chromium sessions for isolated comparison cells."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from .process import process_group_kwargs, terminate_process_tree


class BrowserLaunchError(RuntimeError):
    pass


def _candidate_paths() -> list[Path]:
    candidates: list[Path] = []
    for name in (
        "chrome",
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "msedge",
    ):
        if found := shutil.which(name):
            candidates.append(Path(found))
    if os.name == "nt":
        roots = [
            os.environ.get("PROGRAMFILES"),
            os.environ.get("PROGRAMFILES(X86)"),
            os.environ.get("LOCALAPPDATA"),
        ]
        for raw in roots:
            if not raw:
                continue
            base = Path(raw)
            candidates.extend(
                [
                    base / "Google" / "Chrome" / "Application" / "chrome.exe",
                    base / "Google" / "Chrome SxS" / "Application" / "chrome.exe",
                    base / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                    base / "Chromium" / "Application" / "chrome.exe",
                ]
            )
    elif sys.platform == "darwin":
        candidates.extend(
            [
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
                Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            ]
        )
    return candidates


def find_chromium(explicit: str | Path | None = None) -> Path:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise BrowserLaunchError(f"Chromium executable does not exist: {path}")
        return path
    if configured := os.environ.get("BH_BENCH_CHROME_PATH"):
        return find_chromium(configured)
    seen: set[Path] = set()
    for candidate in _candidate_paths():
        try:
            path = candidate.expanduser().resolve()
        except OSError:
            continue
        if path not in seen and path.is_file():
            return path
        seen.add(path)
    raise BrowserLaunchError(
        "Could not find Chrome, Chromium, or Edge. Pass --chrome-path or set "
        "BH_BENCH_CHROME_PATH."
    )


@dataclass(frozen=True)
class BrowserConfig:
    executable: Path
    headless: bool = True
    startup_timeout_seconds: float = 30.0
    viewport: str = "1440,1000"

    def to_manifest(self) -> dict[str, object]:
        return {
            "provider": "benchmark-owned-local-chromium",
            "executable": str(self.executable),
            "headless": self.headless,
            "viewport": self.viewport,
            "fresh_profile_per_cell": True,
            "uses_browser_provider_api": False,
        }


class LocalChromiumSession:
    """One fresh Chrome profile/process; safe to use for one comparison cell only."""

    def __init__(self, config: BrowserConfig, cell_dir: Path):
        self.config = config
        self.cell_dir = cell_dir.resolve()
        self.profile_dir = (self.cell_dir / "browser-profile").resolve()
        self.log_path = self.cell_dir / "browser.log"
        self.proc: asyncio.subprocess.Process | None = None
        self.cdp_url: str | None = None
        self.version: dict[str, object] = {}
        self._log_handle: IO[bytes] | None = None

    async def start(self) -> str:
        self.profile_dir.mkdir(parents=True, exist_ok=False)
        self._log_handle = self.log_path.open("wb")
        args = [
            str(self.config.executable),
            "--remote-debugging-port=0",
            f"--user-data-dir={self.profile_dir}",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-mode",
            "--disable-component-update",
            "--disable-default-apps",
            "--disable-features=Translate,OptimizationHints",
            "--disable-sync",
            f"--window-size={self.config.viewport}",
        ]
        if self.config.headless:
            args.append("--headless=new")
        args.append("about:blank")
        self.proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(self.cell_dir),
            stdout=self._log_handle,
            stderr=self._log_handle,
            **process_group_kwargs(),
        )

        active_port = self.profile_dir / "DevToolsActivePort"
        deadline = time.monotonic() + self.config.startup_timeout_seconds
        last_error = "DevToolsActivePort was not written"
        while time.monotonic() < deadline:
            if self.proc.returncode is not None:
                raise BrowserLaunchError(
                    f"Chromium exited with {self.proc.returncode}; see {self.log_path}"
                )
            try:
                lines = active_port.read_text(encoding="utf-8").splitlines()
                port = int(lines[0])
                self.cdp_url = f"http://127.0.0.1:{port}"
                self.version = await asyncio.to_thread(
                    self._fetch_version, self.cdp_url
                )
                return self.cdp_url
            except (
                FileNotFoundError,
                IndexError,
                ValueError,
                OSError,
                json.JSONDecodeError,
            ) as exc:
                last_error = str(exc)
                await asyncio.sleep(0.1)
        await self.stop(clean_profile=False)
        raise BrowserLaunchError(
            f"Chromium did not expose CDP in {self.config.startup_timeout_seconds:.1f}s: "
            f"{last_error}; see {self.log_path}"
        )

    @staticmethod
    def _fetch_version(cdp_url: str) -> dict[str, object]:
        with urllib.request.urlopen(f"{cdp_url}/json/version", timeout=2) as response:
            data = json.loads(response.read())
        if not isinstance(data, dict) or not data.get("webSocketDebuggerUrl"):
            raise BrowserLaunchError("CDP /json/version response had no websocket URL")
        return data

    async def stop(self, *, clean_profile: bool = True) -> None:
        await terminate_process_tree(self.proc)
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None
        if clean_profile and self.profile_dir.exists():
            # The target is constructed beneath this cell and resolved before deletion.
            if not self.profile_dir.is_relative_to(self.cell_dir):
                raise BrowserLaunchError(
                    f"Refusing to remove browser profile outside cell: {self.profile_dir}"
                )
            for attempt in range(5):
                try:
                    shutil.rmtree(self.profile_dir)
                    break
                except OSError:
                    if attempt == 4:
                        break
                    await asyncio.sleep(0.2)
