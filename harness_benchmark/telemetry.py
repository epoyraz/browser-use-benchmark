"""Local, privacy-safe telemetry capture and aggregation for harness cells."""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import psutil


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]


def _read_jsonl(path: Path):
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(value, dict):
                    yield value
    except OSError:
        return


def _process_tree(pid: int) -> list[psutil.Process]:
    try:
        root = psutil.Process(pid)
        return [root, *root.children(recursive=True)]
    except (psutil.Error, OSError):
        return []


def _process_snapshot(pid: int) -> dict[str, Any]:
    rows = _process_tree(pid)
    rss = cpu = read_bytes = write_bytes = threads = handles = 0
    alive = 0
    for process in rows:
        try:
            memory = process.memory_info()
            times = process.cpu_times()
            io = process.io_counters()
            rss += int(memory.rss)
            cpu += float(times.user + times.system)
            read_bytes += int(io.read_bytes)
            write_bytes += int(io.write_bytes)
            threads += int(process.num_threads())
            if hasattr(process, "num_handles"):
                handles += int(process.num_handles())
            alive += 1
        except (psutil.Error, OSError):
            continue
    return {
        "pid": pid,
        "alive_processes": alive,
        "rss_bytes": rss,
        "cpu_seconds": round(cpu, 3),
        "read_bytes": read_bytes,
        "write_bytes": write_bytes,
        "threads": threads,
        "handles": handles,
    }


class ProcessTelemetrySampler:
    """Sample owned process trees without recording command lines or arguments."""

    def __init__(self, path: Path, *, interval_seconds: float = 1.0):
        self.path = path
        self.interval_seconds = interval_seconds
        self.started = time.perf_counter()
        self.roots: dict[str, int] = {}
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def add_root(self, label: str, pid: int | None) -> None:
        if pid:
            self.roots[label] = int(pid)

    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.to_thread(self._sample)
            if self._stop.is_set():
                return
            try:
                await asyncio.wait_for(self._stop.wait(), self.interval_seconds)
            except TimeoutError:
                pass

    def _sample(self) -> None:
        roots = dict(self.roots)
        try:
            virtual_memory = psutil.virtual_memory()
            host = {
                "cpu_percent": psutil.cpu_percent(interval=None),
                "available_memory_bytes": int(virtual_memory.available),
                "used_memory_percent": float(virtual_memory.percent),
            }
        except (psutil.Error, OSError):
            host = {}
        entry = {
            "offset_seconds": round(time.perf_counter() - self.started, 3),
            "roots": {label: _process_snapshot(pid) for label, pid in roots.items()},
            "host": host,
        }
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, separators=(",", ":")) + "\n")
        except OSError:
            pass


def _summarize_process_samples(path: Path) -> dict[str, Any]:
    samples = list(_read_jsonl(path))
    by_label: dict[str, list[dict[str, Any]]] = defaultdict(list)
    host_cpu: list[float] = []
    host_memory: list[float] = []
    for sample in samples:
        for label, row in (sample.get("roots") or {}).items():
            if isinstance(row, dict) and row.get("alive_processes"):
                by_label[str(label)].append(row)
        host = sample.get("host") or {}
        if isinstance(host.get("cpu_percent"), (int, float)):
            host_cpu.append(float(host["cpu_percent"]))
        if isinstance(host.get("used_memory_percent"), (int, float)):
            host_memory.append(float(host["used_memory_percent"]))

    roots: dict[str, Any] = {}
    for label, rows in by_label.items():
        cpu_values = [float(row.get("cpu_seconds") or 0) for row in rows]
        read_values = [int(row.get("read_bytes") or 0) for row in rows]
        write_values = [int(row.get("write_bytes") or 0) for row in rows]
        roots[label] = {
            "samples": len(rows),
            "peak_processes": max(int(row.get("alive_processes") or 0) for row in rows),
            "peak_rss_bytes": max(int(row.get("rss_bytes") or 0) for row in rows),
            "peak_threads": max(int(row.get("threads") or 0) for row in rows),
            "peak_handles": max(int(row.get("handles") or 0) for row in rows),
            "cpu_seconds_delta": round(max(cpu_values) - min(cpu_values), 3),
            "read_bytes_delta": max(read_values) - min(read_values),
            "write_bytes_delta": max(write_values) - min(write_values),
        }
    return {
        "samples": len(samples),
        "roots": roots,
        "host_cpu_p50_percent": _percentile(host_cpu, 0.5),
        "host_cpu_p95_percent": _percentile(host_cpu, 0.95),
        "host_memory_peak_percent": max(host_memory) if host_memory else None,
    }


def summarize_cell_telemetry(cell_dir: Path) -> dict[str, Any]:
    """Summarize journals, recordings, diagnostics, and process samples for one cell."""

    journal_candidates = [cell_dir / "harness-journal.jsonl"]
    journal_candidates.extend((cell_dir / "recordings").glob("**/session.jsonl"))
    journals = sorted({path.resolve() for path in journal_candidates if path.is_file()})

    kinds: Counter[str] = Counter()
    helper_calls: Counter[str] = Counter()
    helper_failures: Counter[str] = Counter()
    failure_classes: Counter[str] = Counter()
    helper_ms: dict[str, list[float]] = defaultdict(list)
    helper_cdp: Counter[str] = Counter()
    protocol_calls: Counter[str] = Counter()
    protocol_failures: Counter[str] = Counter()
    protocol_ms: dict[str, list[float]] = defaultdict(list)
    protocol_request_bytes = protocol_response_bytes = frames = 0
    invocations: list[dict[str, Any]] = []
    note_events: Counter[str] = Counter()
    navigation_waits: list[dict[str, Any]] = []
    daemon_events: Counter[str] = Counter()
    transport_closes: list[dict[str, Any]] = []
    transport_evictions: list[dict[str, Any]] = []

    for journal in journals:
        # Candidate c02 captured the daemon's CDP stream in this operational journal as
        # well as in each short-lived client's recording. Those entries are duplicate
        # evidence, not additional browser work. Preserve daemon/note events and count
        # protocol work once from the client journals. New v2 foreground daemons no longer
        # emit the duplicate, but this keeps older raw runs honestly re-summarizable.
        daemon_operational_stream = journal.name == "harness-journal.jsonl"
        for entry in _read_jsonl(journal):
            kind = str(entry.get("kind") or "unknown")
            if (
                daemon_operational_stream
                and kind == "cdp"
                and entry.get("origin") != "daemon_internal"
            ):
                continue
            kinds[kind] += 1
            if kind == "call":
                fn = str(entry.get("fn") or "unknown")
                helper_calls[fn] += 1
                if isinstance(entry.get("ms"), (int, float)):
                    helper_ms[fn].append(float(entry["ms"]))
                if isinstance(entry.get("cdp"), int):
                    helper_cdp[fn] += int(entry["cdp"])
                if entry.get("frame"):
                    frames += 1
                outcome = entry.get("outcome") or {}
                if not outcome.get("ok"):
                    helper_failures[fn] += 1
                    failure_classes[str(outcome.get("class") or "unknown")] += 1
            elif kind == "cdp":
                method = str(entry.get("method") or "unknown")
                protocol_calls[method] += 1
                if not entry.get("ok"):
                    protocol_failures[method] += 1
                if isinstance(entry.get("ms"), (int, float)):
                    protocol_ms[method].append(float(entry["ms"]))
                protocol_request_bytes += int(entry.get("request_bytes") or 0)
                protocol_response_bytes += int(entry.get("response_bytes") or 0)
            elif kind == "invoke":
                invocations.append(entry)
            elif kind == "note":
                event = str(entry.get("event") or "unknown")
                note_events[event] += 1
                if event == "navigation_wait" and isinstance(
                    entry.get("wait_ms"), (int, float)
                ):
                    navigation_waits.append(entry)
            elif kind == "daemon":
                event = str(entry.get("event") or "unknown")
                daemon_events[event] += 1
                if event == "client_closed":
                    transport_closes.append(entry)
                elif event == "peer_evicted":
                    transport_evictions.append(entry)

    helper_rows = []
    for fn, calls in helper_calls.most_common():
        durations = helper_ms[fn]
        helper_rows.append(
            {
                "fn": fn,
                "calls": calls,
                "failed": helper_failures[fn],
                "p50_ms": _percentile(durations, 0.5),
                "p95_ms": _percentile(durations, 0.95),
                "total_ms": round(sum(durations), 1),
                "cdp_total": helper_cdp[fn],
                "cdp_per_call": round(helper_cdp[fn] / calls, 2),
            }
        )
    protocol_rows = []
    for method, calls in protocol_calls.most_common():
        durations = protocol_ms[method]
        protocol_rows.append(
            {
                "method": method,
                "calls": calls,
                "failed": protocol_failures[method],
                "p50_ms": _percentile(durations, 0.5),
                "p95_ms": _percentile(durations, 0.95),
                "total_ms": round(sum(durations), 1),
            }
        )

    diagnostic_files = sorted((cell_dir / "diagnostics").glob("*.json"))
    diagnostic_targets: list[dict[str, Any]] = []
    for path in diagnostic_files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for target in payload.get("targets") or []:
            if isinstance(target, dict):
                diagnostic_targets.append(target)
    event_loop = [
        float((row.get("diagnostics") or {}).get("event_loop_delay_ms") or 0)
        for row in diagnostic_targets
    ]
    resources = [
        (row.get("diagnostics") or {}).get("resources") or {}
        for row in diagnostic_targets
    ]
    events = [
        event
        for row in diagnostic_targets
        for event in ((row.get("diagnostics") or {}).get("events") or [])
        if isinstance(event, dict)
    ]
    diagnostic_summary = {
        "files": len(diagnostic_files),
        "target_snapshots": len(diagnostic_targets),
        "events": len(events),
        "events_dropped_snapshots": sum(
            bool((row.get("diagnostics") or {}).get("events_dropped"))
            for row in diagnostic_targets
        ),
        "event_methods": Counter(str(event.get("method") or "unknown") for event in events).most_common(),
        "event_loop_delay_p50_ms": _percentile(event_loop, 0.5),
        "event_loop_delay_p95_ms": _percentile(event_loop, 0.95),
        "event_loop_delay_max_ms": max(event_loop) if event_loop else None,
        "resource_count_max": max((int(row.get("count") or 0) for row in resources), default=0),
        "resource_transfer_bytes_max": max(
            (int(row.get("transfer_bytes") or 0) for row in resources), default=0
        ),
        "resource_longest_ms_max": max(
            (int(row.get("longest_ms") or 0) for row in resources), default=0
        ),
    }

    recording_dirs = sorted(
        path.parent for path in (cell_dir / "recordings").glob("**/meta.json")
    )
    jpgs = [path for directory in recording_dirs for path in directory.glob("*.jpg")]
    recordings = {
        "sessions": len(recording_dirs),
        "frames": len(jpgs),
        "frame_bytes": sum(path.stat().st_size for path in jpgs),
        "journal_bytes": sum(path.stat().st_size for path in journals),
    }

    invoke_ms = [float(row.get("ms_total") or 0) for row in invocations]
    navigation_ms = [float(row["wait_ms"]) for row in navigation_waits]
    navigation_lifecycle = Counter(
        str(row.get("lifecycle") or "unknown") for row in navigation_waits
    )
    return {
        "journals": len(journals),
        "journal_kinds": dict(kinds),
        "invocations": {
            "count": len(invocations),
            "failed": sum(not row.get("ok", True) for row in invocations),
            "total_ms": round(sum(invoke_ms), 1),
            "p50_ms": _percentile(invoke_ms, 0.5),
            "p95_ms": _percentile(invoke_ms, 0.95),
            "source_lines": sum(int(row.get("source_lines") or 0) for row in invocations),
        },
        "helpers": {
            "calls": sum(helper_calls.values()),
            "failed": sum(helper_failures.values()),
            "failure_classes": failure_classes.most_common(),
            "by_function": helper_rows,
        },
        "protocol": {
            "calls": sum(protocol_calls.values()),
            "failed": sum(protocol_failures.values()),
            "request_bytes": protocol_request_bytes,
            "response_bytes": protocol_response_bytes,
            "by_method": protocol_rows,
        },
        "notes": {
            "events": note_events.most_common(),
            "navigation": {
                "calls": len(navigation_waits),
                "total_wait_ms": round(sum(navigation_ms), 1),
                "p50_wait_ms": _percentile(navigation_ms, 0.5),
                "p95_wait_ms": _percentile(navigation_ms, 0.95),
                "lifecycles": navigation_lifecycle.most_common(),
                "early_usable": navigation_lifecycle["usable"],
            },
        },
        "transport": {
            "daemon_events": daemon_events.most_common(),
            "client_connections_closed": len(transport_closes),
            "peer_evictions": len(transport_evictions),
            "overflows": sum(int(row.get("overflows") or 0) for row in transport_closes),
            "enqueued_frames": sum(
                int(row.get("enqueued_frames") or 0) for row in transport_closes
            ),
            "sent_frames": sum(int(row.get("sent_frames") or 0) for row in transport_closes),
            "peak_queued_frames": max(
                (int(row.get("peak_frames") or 0) for row in transport_closes), default=0
            ),
            "peak_buffered_bytes": max(
                (int(row.get("peak_bytes") or 0) for row in transport_closes), default=0
            ),
            "eviction_methods": Counter(
                str(row.get("method") or "unknown") for row in transport_evictions
            ).most_common(),
        },
        "diagnostics": diagnostic_summary,
        "recordings": recordings,
        "processes": _summarize_process_samples(cell_dir / "process-telemetry.jsonl"),
    }
