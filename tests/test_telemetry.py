from __future__ import annotations

import json

from harness_benchmark.telemetry import summarize_cell_telemetry


def test_cell_telemetry_rolls_up_recordings_diagnostics_and_processes(tmp_path):
    recording = tmp_path / "recordings" / "rec-1"
    recording.mkdir(parents=True)
    (recording / "meta.json").write_text("{}", encoding="utf-8")
    (recording / "0001.jpg").write_bytes(b"frame")
    entries = [
        {
            "kind": "call",
            "fn": "goto",
            "ms": 20,
            "cdp": 2,
            "frame": "0001.jpg",
            "outcome": {"ok": True},
        },
        {
            "kind": "cdp",
            "method": "Page.navigate",
            "ms": 10,
            "request_bytes": 4,
            "response_bytes": 8,
            "ok": True,
        },
        {
            "kind": "note",
            "event": "navigation_wait",
            "wait_ms": 12.5,
            "lifecycle": "usable",
        },
        {"kind": "invoke", "ok": True, "ms_total": 30, "source_lines": 2},
    ]
    (recording / "session.jsonl").write_text(
        "".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8"
    )
    (tmp_path / "harness-journal.jsonl").write_text(
        "\n".join(
            json.dumps(entry)
            for entry in (
                {
                    "kind": "daemon",
                    "event": "client_closed",
                    "enqueued_frames": 80,
                    "sent_frames": 80,
                    "peak_frames": 72,
                    "peak_bytes": 4096,
                    "overflows": 0,
                },
                {
                    "kind": "daemon",
                    "event": "peer_evicted",
                    "method": "Network.dataReceived",
                    "overflows": 1,
                },
                {
                    # The c02 daemon captured this same call as the client recording.
                    # It must not turn one browser round trip into two in the rollup.
                    "kind": "cdp",
                    "method": "Page.navigate",
                    "ms": 10,
                    "request_bytes": 4,
                    "response_bytes": 8,
                    "ok": True,
                },
                {
                    # New daemons retain only browser work introduced behind the client
                    # boundary, which has no duplicate in a client recording.
                    "kind": "cdp",
                    "origin": "daemon_internal",
                    "method": "Target.getTargets",
                    "ms": 2,
                    "request_bytes": 2,
                    "response_bytes": 10,
                    "ok": True,
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )
    diagnostics = tmp_path / "diagnostics"
    diagnostics.mkdir()
    (diagnostics / "one.json").write_text(
        json.dumps(
            {
                "targets": [
                    {
                        "diagnostics": {
                            "events": [{"method": "Network.loadingFailed"}],
                            "events_dropped": False,
                            "event_loop_delay_ms": 3.5,
                            "resources": {
                                "count": 7,
                                "transfer_bytes": 99,
                                "longest_ms": 15,
                            },
                        }
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "process-telemetry.jsonl").write_text(
        json.dumps(
            {
                "roots": {
                    "browser": {
                        "alive_processes": 2,
                        "rss_bytes": 100,
                        "cpu_seconds": 1,
                        "read_bytes": 2,
                        "write_bytes": 3,
                        "threads": 4,
                        "handles": 5,
                    }
                },
                "host": {"cpu_percent": 25, "used_memory_percent": 50},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = summarize_cell_telemetry(tmp_path)

    assert result["helpers"]["calls"] == 1
    assert result["protocol"]["calls"] == 2
    assert result["notes"]["navigation"]["early_usable"] == 1
    assert result["notes"]["navigation"]["total_wait_ms"] == 12.5
    assert result["recordings"]["frames"] == 1
    assert result["transport"]["client_connections_closed"] == 1
    assert result["transport"]["peer_evictions"] == 1
    assert result["transport"]["peak_queued_frames"] == 72
    assert result["transport"]["eviction_methods"] == [("Network.dataReceived", 1)]
    assert result["diagnostics"]["events"] == 1
    assert result["processes"]["roots"]["browser"]["peak_processes"] == 2
