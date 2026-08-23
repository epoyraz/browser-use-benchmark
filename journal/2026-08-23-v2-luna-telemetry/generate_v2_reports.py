from __future__ import annotations

import html
import json
import shutil
import sys
from collections import defaultdict
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

TMP = Path(__file__).resolve().parent
BENCH = Path(__file__).resolve().parents[2]
ROOT = BENCH.parent
RUNS = BENCH / "run_data" / "harness_comparisons"
BASELINE_PATTERN = "v2-luna-telemetry-20260823-b*"
FIRST_CANDIDATE_ID = "v2-luna-efficiency-ab-20260823-c01"
CANDIDATE_ID = "v2-luna-efficiency-ab-20260823-c02"
REJUDGE_PATH = TMP / "baseline-rejudge-v2-luna-full-response.json"
ASSETS = TMP / "assets"

sys.path.insert(0, str(BENCH))
from harness_benchmark.telemetry import summarize_cell_telemetry

CATEGORY_ORDER = [
    "GAIA",
    "WebBenchREAD",
    "InteractionTests",
    "BrowseComp",
    "OM2W2",
]


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def result_files(run: Path) -> list[Path]:
    return sorted(run.glob("cells/*/result.json"))


def load_results(pattern: str) -> list[tuple[Path, dict[str, Any]]]:
    rows: list[tuple[Path, dict[str, Any]]] = []
    for run in sorted(RUNS.glob(pattern)):
        for path in result_files(run):
            rows.append((path.parent, load_json(path)))
    return rows


def refresh_telemetry(
    rows: list[tuple[Path, dict[str, Any]]],
) -> list[tuple[Path, dict[str, Any]]]:
    """Re-summarize immutable raw evidence with the current de-duplication rules."""

    refreshed: list[tuple[Path, dict[str, Any]]] = []
    for cell, original in rows:
        row = deepcopy(original)
        row.setdefault("metrics", {})["harness_telemetry"] = summarize_cell_telemetry(cell)
        refreshed.append((cell, row))
    return refreshed


def num(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def integer(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) else 0


def metric(row: dict[str, Any], *keys: str) -> Any:
    value: Any = row
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def task_index(row: dict[str, Any]) -> int:
    return integer(metric(row, "provenance", "benchmark", "benchmark_index"))


def category(row: dict[str, Any]) -> str:
    return str(metric(row, "provenance", "benchmark", "category") or "unknown")


def captcha(row: dict[str, Any]) -> bool:
    return bool(metric(row, "judgement", "reached_captcha"))


def impossible(row: dict[str, Any]) -> bool:
    return bool(metric(row, "judgement", "impossible_task"))


def passed(row: dict[str, Any]) -> bool:
    return row.get("score") == 1


def human_int(value: float | None) -> str:
    return f"{int(value or 0):,}"


def seconds(value: float | None) -> str:
    value = float(value or 0)
    if value >= 3600:
        return f"{value / 3600:.2f} h"
    if value >= 60:
        return f"{value / 60:.1f} min"
    return f"{value:.1f} s"


def pct(value: float | None, digits: int = 0) -> str:
    return "n/a" if value is None else f"{value * 100:.{digits}f}%"


def delta_pct(before: float, after: float) -> float | None:
    return None if not before else (after - before) / before


def delta_label(value: float | None, inverse_good: bool = True) -> str:
    if value is None:
        return "n/a"
    good = value < 0 if inverse_good else value > 0
    cls = "good" if good else "bad" if value else "neutral"
    sign = "+" if value > 0 else ""
    return f'<span class="delta {cls}">{sign}{value * 100:.1f}%</span>'


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def summarize(rows: list[tuple[Path, dict[str, Any]]]) -> dict[str, Any]:
    valid = [(p, r) for p, r in rows if not captcha(r)]
    eligible = [(p, r) for p, r in valid if not impossible(r)]
    return {
        "attempted": len(rows),
        "captcha": sum(captcha(r) for _, r in rows),
        "valid": len(valid),
        "impossible": sum(impossible(r) for _, r in valid),
        "eligible": len(eligible),
        "passes": sum(passed(r) for _, r in valid),
        "eligible_passes": sum(passed(r) for _, r in eligible),
        "agent_seconds": sum(num(metric(r, "timing", "agent_seconds")) for _, r in valid),
        "total_seconds": sum(num(metric(r, "timing", "total_seconds")) for _, r in valid),
        "commands": sum(integer(metric(r, "metrics", "command_executions")) for _, r in valid),
        "input_tokens": sum(integer(metric(r, "metrics", "tokens", "input_tokens")) for _, r in valid),
        "cached_tokens": sum(integer(metric(r, "metrics", "tokens", "cached_input_tokens")) for _, r in valid),
        "output_tokens": sum(integer(metric(r, "metrics", "tokens", "output_tokens")) for _, r in valid),
        "reasoning_tokens": sum(integer(metric(r, "metrics", "tokens", "reasoning_output_tokens")) for _, r in valid),
        "cdp": sum(integer(metric(r, "metrics", "harness_telemetry", "protocol", "calls")) for _, r in valid),
        "helpers": sum(integer(metric(r, "metrics", "harness_telemetry", "helpers", "calls")) for _, r in valid),
        "frames": sum(integer(metric(r, "metrics", "harness_telemetry", "recordings", "frames")) for _, r in valid),
        "valid_rows": valid,
        "eligible_rows": eligible,
    }


def aggregate_nested(
    rows: list[tuple[Path, dict[str, Any]]], section: str, name_key: str
) -> list[dict[str, Any]]:
    out: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for _, row in rows:
        if captcha(row):
            continue
        entries = metric(row, "metrics", "harness_telemetry", section, "by_function" if section == "helpers" else "by_method") or []
        for entry in entries:
            name = str(entry.get(name_key) or "unknown")
            for key in ("calls", "failed", "total_ms", "cdp_total"):
                out[name][key] += num(entry.get(key))
    return [
        {"name": name, **values}
        for name, values in sorted(out.items(), key=lambda item: item[1]["total_ms"], reverse=True)
    ]


def step_analysis(rows: list[tuple[Path, dict[str, Any]]]) -> dict[str, int]:
    total_chars = page_chars = page_commands = skill_reads = skill_chars = 0
    bash_heredocs = open_page_commands = 0
    for cell, row in rows:
        if captcha(row):
            continue
        commands: list[tuple[str, str]] = []
        raw_log = cell / "agent.stdout.jsonl"
        if raw_log.is_file():
            for line in raw_log.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                item = event.get("item") or {}
                if event.get("type") != "item.completed" or item.get("type") != "command_execution":
                    continue
                command = str(item.get("command") or "")
                output = str(item.get("aggregated_output") or item.get("output") or "")
                commands.append((command, output))
        else:
            for step in metric(row, "execution", "steps") or []:
                if not str(step).startswith("command:"):
                    continue
                command, marker, output = str(step).partition("\n-> ")
                commands.append((command, output if marker else ""))
        for command, output in commands:
            output_len = len(output)
            total_chars += output_len
            if "page_text(" in command:
                page_commands += 1
                page_chars += output_len
            if "open_page(" in command:
                open_page_commands += 1
            if "Get-Content -Raw" in command and "SKILL.md" in command:
                skill_reads += 1
                skill_chars += output_len
            if "bh <<'PY'" in command or 'bh <<"PY"' in command:
                bash_heredocs += 1
    return {
        "command_output_chars": total_chars,
        "page_text_output_chars": page_chars,
        "page_text_commands": page_commands,
        "skill_reads": skill_reads,
        "skill_output_chars": skill_chars,
        "bash_heredocs": bash_heredocs,
        "open_page_commands": open_page_commands,
    }


def category_rows(rows: list[tuple[Path, dict[str, Any]]]) -> list[dict[str, Any]]:
    out = []
    for name in CATEGORY_ORDER:
        group = [r for _, r in rows if category(r) == name and not captcha(r)]
        eligible = [r for r in group if not impossible(r)]
        out.append(
            {
                "category": name,
                "valid": len(group),
                "passes": sum(passed(r) for r in group),
                "eligible": len(eligible),
                "eligible_passes": sum(passed(r) for r in eligible),
                "seconds": sum(num(metric(r, "timing", "agent_seconds")) for r in group),
                "tokens": sum(integer(metric(r, "metrics", "tokens", "input_tokens")) for r in group),
                "cdp": sum(integer(metric(r, "metrics", "harness_telemetry", "protocol", "calls")) for r in group),
            }
        )
    return out


def deep_telemetry(rows: list[tuple[Path, dict[str, Any]]]) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    peaks: dict[str, float] = defaultdict(float)
    for _, row in rows:
        if captcha(row):
            continue
        telemetry = metric(row, "metrics", "harness_telemetry") or {}
        protocol = telemetry.get("protocol") or {}
        diagnostics = telemetry.get("diagnostics") or {}
        recordings = telemetry.get("recordings") or {}
        processes = telemetry.get("processes") or {}
        transport = telemetry.get("transport") or {}
        totals["protocol_request_bytes"] += num(protocol.get("request_bytes"))
        totals["protocol_response_bytes"] += num(protocol.get("response_bytes"))
        totals["diagnostic_files"] += num(diagnostics.get("files"))
        totals["diagnostic_events"] += num(diagnostics.get("events"))
        totals["recording_bytes"] += num(recordings.get("frame_bytes"))
        totals["peer_evictions"] += num(transport.get("peer_evictions"))
        totals["peer_overflows"] += num(transport.get("overflows"))
        totals["transport_enqueued_frames"] += num(transport.get("enqueued_frames"))
        totals["transport_sent_frames"] += num(transport.get("sent_frames"))
        peaks["transport_queue_frames"] = max(
            peaks["transport_queue_frames"], num(transport.get("peak_queued_frames"))
        )
        peaks["transport_buffer_bytes"] = max(
            peaks["transport_buffer_bytes"], num(transport.get("peak_buffered_bytes"))
        )
        for label, process in (processes.get("roots") or {}).items():
            peaks[f"{label}_rss"] = max(peaks[f"{label}_rss"], num(process.get("peak_rss_bytes")))
            peaks[f"{label}_processes"] = max(
                peaks[f"{label}_processes"], num(process.get("peak_processes"))
            )
            totals[f"{label}_cpu"] += num(process.get("cpu_seconds_delta"))
            totals[f"{label}_read"] += num(process.get("read_bytes_delta"))
            totals[f"{label}_write"] += num(process.get("write_bytes_delta"))
        peaks["host_memory_percent"] = max(
            peaks["host_memory_percent"], num(processes.get("host_memory_peak_percent"))
        )
        peaks["resource_count"] = max(
            peaks["resource_count"], num(diagnostics.get("resource_count_max"))
        )
        peaks["resource_transfer_bytes"] = max(
            peaks["resource_transfer_bytes"], num(diagnostics.get("resource_transfer_bytes_max"))
        )
        peaks["event_loop_ms"] = max(
            peaks["event_loop_ms"], num(diagnostics.get("event_loop_delay_max_ms"))
        )
    return {**totals, **peaks}


def helper_failure_count(
    rows: list[tuple[Path, dict[str, Any]]], failure_class: str
) -> int:
    total = 0
    for _, row in rows:
        if captcha(row):
            continue
        classes = metric(
            row, "metrics", "harness_telemetry", "helpers", "failure_classes"
        ) or []
        for item in classes:
            if len(item) >= 2 and str(item[0]) == failure_class:
                total += integer(item[1])
    return total


def svg_bars(
    rows: list[tuple[str, float]], title: str, *, unit: str = "", color: str = "#4dd4ac"
) -> str:
    rows = [(label, value) for label, value in rows if value >= 0]
    width, left, right, bar_h, gap = 780, 180, 90, 24, 14
    height = 56 + len(rows) * (bar_h + gap)
    max_value = max((value for _, value in rows), default=1) or 1
    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="{esc(title)}">',
        f'<text x="0" y="22" class="chart-title">{esc(title)}</text>',
    ]
    for index, (label, value) in enumerate(rows):
        y = 45 + index * (bar_h + gap)
        bar_width = (width - left - right) * value / max_value
        shown = f"{value:,.1f}{unit}" if not float(value).is_integer() else f"{int(value):,}{unit}"
        parts.extend(
            [
                f'<text x="{left - 10}" y="{y + 17}" text-anchor="end" class="chart-label">{esc(label)}</text>',
                f'<rect x="{left}" y="{y}" width="{width-left-right}" height="{bar_h}" rx="6" class="track"/>',
                f'<rect x="{left}" y="{y}" width="{bar_width:.1f}" height="{bar_h}" rx="6" fill="{color}"/>',
                f'<text x="{left + bar_width + 8:.1f}" y="{y + 17}" class="chart-value">{esc(shown)}</text>',
            ]
        )
    parts.append("</svg>")
    return "".join(parts)


def table(headers: list[str], rows: list[list[str]], classes: str = "") -> str:
    head = "".join(f"<th>{esc(value)}</th>" for value in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{value}</td>" for value in row) + "</tr>"
        for row in rows
    )
    return f'<div class="table-wrap"><table class="{classes}"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


CSS = r"""
:root{--bg:#08110f;--panel:#101c19;--panel2:#14241f;--ink:#eef8f4;--muted:#9ab0a8;--line:#29433a;--accent:#4dd4ac;--accent2:#86a8ff;--warn:#ffc66d;--bad:#ff7d88;--good:#54e39a;--max:1180px;color-scheme:dark}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:radial-gradient(circle at 80% -10%,#183b33 0,transparent 35%),var(--bg);color:var(--ink);font:15px/1.55 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}.wrap{max-width:var(--max);margin:auto;padding:0 28px}.hero{padding:76px 0 42px;border-bottom:1px solid var(--line)}.eyebrow{color:var(--accent);font-size:12px;font-weight:800;letter-spacing:.14em;text-transform:uppercase}.hero h1{font-size:clamp(38px,7vw,76px);line-height:.98;letter-spacing:-.055em;max-width:960px;margin:18px 0}.hero p{max-width:820px;font-size:19px;color:#bdd0c9}.meta{display:flex;flex-wrap:wrap;gap:10px;margin-top:25px}.pill{border:1px solid var(--line);background:#0d1815;border-radius:999px;padding:7px 11px;color:var(--muted);font-size:12px}.pill strong{color:var(--ink)}nav{position:sticky;top:0;z-index:10;background:#08110fe8;backdrop-filter:blur(15px);border-bottom:1px solid var(--line)}nav .wrap{display:flex;gap:18px;align-items:center;overflow:auto;padding-top:12px;padding-bottom:12px}nav a{white-space:nowrap;font-size:13px;color:#b8cbc4}section{padding:54px 0;border-bottom:1px solid #1d302a}h2{font-size:32px;letter-spacing:-.035em;margin:0 0 12px}h3{font-size:19px;margin:0 0 8px}.lead{color:var(--muted);max-width:850px;margin:0 0 26px}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.card{padding:20px;border:1px solid var(--line);background:linear-gradient(145deg,var(--panel2),var(--panel));border-radius:16px;min-height:125px}.card .value{font-size:33px;line-height:1;font-weight:800;letter-spacing:-.04em;margin:8px 0}.card .label{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}.card small{color:var(--muted)}.grid2{display:grid;grid-template-columns:1fr 1fr;gap:20px}.callout{border-left:4px solid var(--accent);background:#10221d;padding:18px 20px;border-radius:4px 14px 14px 4px}.callout.warn{border-color:var(--warn)}.callout.bad{border-color:var(--bad)}.chart{display:block;width:100%;height:auto;background:#0d1815;border:1px solid var(--line);border-radius:16px;padding:18px}.chart-title{fill:var(--ink);font-weight:700;font-size:15px}.chart-label,.chart-value{fill:#b8cbc4;font-size:12px}.track{fill:#21332d}.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:14px}table{border-collapse:collapse;width:100%;min-width:720px;background:#0d1815}th,td{padding:11px 13px;border-bottom:1px solid #20362f;text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}th{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);background:#13221e;position:sticky;top:0}tr:last-child td{border-bottom:0}td{font-variant-numeric:tabular-nums}.delta{font-weight:800}.delta.good{color:var(--good)}.delta.bad{color:var(--bad)}.delta.neutral{color:var(--muted)}.tag{display:inline-block;border-radius:999px;padding:3px 8px;font-size:11px;font-weight:700}.tag.pass{background:#123c2d;color:#70efb5}.tag.fail{background:#412027;color:#ffabb3}.tag.exclude{background:#45371e;color:#ffd68e}.gallery{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}.figure{background:var(--panel);border:1px solid var(--line);border-radius:14px;overflow:hidden}.figure img{display:block;width:100%;aspect-ratio:16/10;object-fit:cover;background:#050807}.figure figcaption{padding:12px;color:var(--muted);font-size:12px}.code-list{counter-reset:item;display:grid;gap:12px}.code-item{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px}.code-item strong{color:var(--accent)}.foot{padding:38px 0 70px;color:var(--muted);font-size:12px}.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace}.small{font-size:12px;color:var(--muted)}ul.clean{padding-left:20px}ul.clean li{margin:7px 0}.verdict{font-size:21px;max-width:900px}.score-ring{width:120px;height:120px;border-radius:50%;display:grid;place-items:center;background:conic-gradient(var(--accent) 0 var(--score),#21342e var(--score) 100%);position:relative}.score-ring:before{content:"";position:absolute;inset:12px;border-radius:50%;background:var(--panel)}.score-ring span{position:relative;font-size:24px;font-weight:800}.hero-grid{display:grid;grid-template-columns:1fr auto;gap:30px;align-items:end}
@media(max-width:850px){.cards{grid-template-columns:repeat(2,1fr)}.grid2,.hero-grid{grid-template-columns:1fr}.gallery{grid-template-columns:1fr}.score-ring{display:none}}
@media(max-width:520px){.wrap{padding:0 17px}.cards{grid-template-columns:1fr}.hero{padding-top:48px}.hero h1{font-size:42px}}
"""


def shell(title: str, body: str, *, description: str, links: list[tuple[str, str]]) -> str:
    nav = "".join(f'<a href="#{esc(anchor)}">{esc(label)}</a>' for anchor, label in links)
    generated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{esc(title)}</title><meta name=\"description\" content=\"{esc(description)}\">"
        f"<style>{CSS}</style></head><body><nav><div class=\"wrap\">{nav}</div></nav>"
        f"{body}<footer class=\"foot\"><div class=\"wrap\">Generated locally {esc(generated)}. "
        "Contains benchmark-derived telemetry; keep the raw run_data private.</div></footer></body></html>"
    )


def copy_gallery(
    baseline: list[tuple[Path, dict[str, Any]]],
    candidate: list[tuple[Path, dict[str, Any]]],
) -> list[tuple[str, str]]:
    ASSETS.mkdir(parents=True, exist_ok=True)
    choices: list[tuple[str, list[tuple[Path, dict[str, Any]]], int, str]] = [
        ("baseline-browse-pass.png", baseline, 44, "Baseline BrowseComp pass: evidence screenshot"),
        ("baseline-captcha.png", baseline, 41, "Excluded CAPTCHA example: preserved for audit"),
        ("baseline-interaction.png", baseline, 22, "Interaction task evidence"),
        ("candidate-gaia.png", candidate, 23, "Optimized v2 GAIA evidence"),
        ("candidate-browse.png", candidate, 44, "Optimized v2 BrowseComp evidence"),
        ("candidate-om.png", candidate, 11, "Optimized v2 OM2W2 evidence"),
    ]
    copied: list[tuple[str, str]] = []
    for filename, rows, index, caption in choices:
        found = next(((cell, row) for cell, row in rows if task_index(row) == index), None)
        if not found:
            continue
        cell, row = found
        shots = row.get("screenshots") or []
        if not shots:
            continue
        source = cell / str(shots[-1])
        if not source.is_file():
            continue
        destination = ASSETS / filename
        shutil.copy2(source, destination)
        copied.append((f"assets/{filename}", caption))
    return copied


def render_gallery(items: list[tuple[str, str]]) -> str:
    if not items:
        return '<p class="small">No screenshot artifacts were available.</p>'
    return '<div class="gallery">' + "".join(
        f'<figure class="figure"><img loading="lazy" src="{esc(path)}" alt="{esc(caption)}"><figcaption>{esc(caption)}</figcaption></figure>'
        for path, caption in items
    ) + "</div>"


def build() -> tuple[Path, Path, Path]:
    baseline = refresh_telemetry(load_results(BASELINE_PATTERN))
    first_candidate = refresh_telemetry(load_results(FIRST_CANDIDATE_ID))
    candidate = refresh_telemetry(load_results(CANDIDATE_ID))
    if not baseline:
        raise RuntimeError("No baseline results found")
    if len(candidate) < 5:
        raise RuntimeError(f"Candidate run incomplete: found {len(candidate)} of 5 results")
    if len(first_candidate) < 5:
        raise RuntimeError(
            f"First candidate run incomplete: found {len(first_candidate)} of 5 results"
        )

    base = summarize(baseline)
    first_cand = summarize(first_candidate)
    cand = summarize(candidate)
    if cand["valid"] != 5:
        raise RuntimeError(
            f"Final candidate has {cand['valid']} non-CAPTCHA results; a replacement is required"
        )
    base_steps, cand_steps = step_analysis(baseline), step_analysis(candidate)
    base_deep, cand_deep = deep_telemetry(baseline), deep_telemetry(candidate)
    first_disconnects = helper_failure_count(first_candidate, "browser_disconnected")
    base_categories = category_rows(baseline)
    helpers = aggregate_nested(baseline, "helpers", "fn")
    methods = aggregate_nested(baseline, "protocol", "method")
    candidate_methods = aggregate_nested(candidate, "protocol", "method")
    gallery = copy_gallery(baseline, candidate)

    baseline_by_index = {task_index(row): row for _, row in baseline if not captcha(row)}
    candidate_by_index = {
        task_index(row): row for _, row in candidate if not captcha(row)
    }
    first_candidate_by_index = {
        task_index(row): row for _, row in first_candidate if not captcha(row)
    }
    first_web_seconds = num(
        metric(first_candidate_by_index.get(5, {}), "timing", "agent_seconds")
    )
    final_web_seconds = num(metric(candidate_by_index.get(5, {}), "timing", "agent_seconds"))
    rejudge: dict[str, Any] = load_json(REJUDGE_PATH) if REJUDGE_PATH.is_file() else {}
    rejudge_by_index = {int(row["task_index"]): row for row in rejudge.get("results", [])}
    audited_baseline_passes = sum(
        int(rejudge_by_index.get(index, {}).get("score", baseline_by_index[index].get("score")) == 1)
        for index in candidate_by_index
    )
    final_disconnects = helper_failure_count(candidate, "browser_disconnected")
    repeated_setup_methods = {
        "Page.addScriptToEvaluateOnNewDocument",
        "Runtime.addBinding",
        "Log.enable",
        "Performance.enable",
    }
    repeated_setup_calls = sum(
        int(row["calls"]) for row in candidate_methods if row["name"] in repeated_setup_methods
    )

    pair_rows: list[list[str]] = []
    matched_task_analysis: list[dict[str, Any]] = []
    duration_graph: list[tuple[str, float]] = []
    token_graph: list[tuple[str, float]] = []
    for index in sorted(candidate_by_index):
        before, after = baseline_by_index[index], candidate_by_index[index]
        btime = num(metric(before, "timing", "agent_seconds"))
        atime = num(metric(after, "timing", "agent_seconds"))
        btok = integer(metric(before, "metrics", "tokens", "input_tokens"))
        atok = integer(metric(after, "metrics", "tokens", "input_tokens"))
        bcdp = integer(metric(before, "metrics", "harness_telemetry", "protocol", "calls"))
        acdp = integer(metric(after, "metrics", "harness_telemetry", "protocol", "calls"))
        audited = rejudge_by_index.get(index, {})
        baseline_score = audited.get("score", before.get("score"))
        score_note = "audit" if index in rejudge_by_index else "raw"
        pair_rows.append(
            [
                f"#{index} · {esc(category(after))}",
                f"{seconds(btime)} → {seconds(atime)} {delta_label(delta_pct(btime, atime))}",
                f"{human_int(btok)} → {human_int(atok)} {delta_label(delta_pct(btok, atok))}",
                f"{human_int(bcdp)} → {human_int(acdp)} {delta_label(delta_pct(bcdp, acdp))}",
                f"{baseline_score} ({score_note}) → {after.get('score')}",
            ]
        )
        matched_task_analysis.append(
            {
                "task_index": index,
                "category": category(after),
                "baseline_agent_seconds": btime,
                "candidate_agent_seconds": atime,
                "baseline_input_tokens": btok,
                "candidate_input_tokens": atok,
                "baseline_cdp_calls": bcdp,
                "candidate_cdp_calls": acdp,
                "baseline_raw_score": before.get("score"),
                "baseline_audited_score": baseline_score,
                "candidate_score": after.get("score"),
            }
        )
        duration_graph.extend([(f"#{index} before", btime), (f"#{index} after", atime)])
        token_graph.extend([(f"#{index} before", btok / 1000), (f"#{index} after", atok / 1000)])

    bpair = summarize([(Path("."), baseline_by_index[index]) for index in candidate_by_index])
    pair_time_delta = delta_pct(bpair["agent_seconds"], cand["agent_seconds"])
    pair_token_delta = delta_pct(bpair["input_tokens"], cand["input_tokens"])
    pair_cdp_delta = delta_pct(bpair["cdp"], cand["cdp"])
    pair_command_delta = delta_pct(bpair["commands"], cand["commands"])

    category_table = table(
        ["Category", "Valid", "Raw pass", "Eligible pass", "Agent time", "Input tokens", "CDP"],
        [
            [
                esc(row["category"]),
                str(row["valid"]),
                f'{row["passes"]}/{row["valid"]}',
                f'{row["eligible_passes"]}/{row["eligible"]}',
                seconds(row["seconds"]),
                human_int(row["tokens"]),
                human_int(row["cdp"]),
            ]
            for row in base_categories
        ],
    )

    task_table = table(
        ["Task", "Category", "Outcome", "Agent time", "Commands", "Input tokens", "CDP", "Helpers"],
        [
            [
                f"#{task_index(row)}",
                esc(category(row)),
                '<span class="tag pass">pass</span>' if passed(row) else (
                    '<span class="tag exclude">impossible</span>' if impossible(row)
                    else '<span class="tag fail">fail</span>'
                ),
                seconds(metric(row, "timing", "agent_seconds")),
                human_int(metric(row, "metrics", "command_executions")),
                human_int(metric(row, "metrics", "tokens", "input_tokens")),
                human_int(metric(row, "metrics", "harness_telemetry", "protocol", "calls")),
                human_int(metric(row, "metrics", "harness_telemetry", "helpers", "calls")),
            ]
            for _, row in sorted(base["valid_rows"], key=lambda item: task_index(item[1]))
        ],
    )

    helper_chart = svg_bars(
        [(row["name"], row["total_ms"] / 1000) for row in helpers[:10]],
        "Baseline helper wall time (summed across valid tasks)",
        unit=" s",
    )
    method_chart = svg_bars(
        [(row["name"], row["calls"]) for row in sorted(methods, key=lambda x: x["calls"], reverse=True)[:12]],
        "Most frequent CDP methods",
        color="#86a8ff",
    )
    category_pass_chart = svg_bars(
        [(row["category"], row["passes"] / row["valid"] * 100 if row["valid"] else 0) for row in base_categories],
        "Raw baseline pass rate by category",
        unit="%",
        color="#ffc66d",
    )

    goto_ms = next((row["total_ms"] for row in helpers if row["name"] == "goto"), 0)
    nav_ms = next((row["total_ms"] for row in methods if row["name"] == "Page.navigate"), 0)
    wait_overhead_ms = max(0, goto_ms - nav_ms)
    skill_old = 22888
    skill_new = (ROOT / "v2" / "SKILL.md").read_text(encoding="utf-8").__len__()
    score_rate = base["eligible_passes"] / base["eligible"] if base["eligible"] else 0

    report_body = f"""
<header class="hero" id="top"><div class="wrap hero-grid"><div><div class="eyebrow">Browser Harness v2 · first full telemetry campaign</div><h1>Efficiency benchmark report</h1><p>ChatGPT/Codex gpt-5.6-luna at max effort drove v2; the same model at max effort judged it. No Claude model, process, or API was used. CAPTCHA tasks were excluded and replaced until every category had five valid tasks.</p><div class="meta"><span class="pill"><strong>Baseline</strong> 7cc604a</span><span class="pill"><strong>Browser</strong> Edge 152 headless</span><span class="pill"><strong>Scope</strong> v2 only</span><span class="pill"><strong>Telemetry</strong> CDP + journal + diagnostics + processes</span></div></div><div class="score-ring" style="--score:{score_rate*100:.1f}%"><span>{pct(score_rate)}</span></div></div></header>
<main>
<section id="outcome"><div class="wrap"><h2>Outcome</h2><p class="lead">The first 25-task baseline produced rich telemetry, but the five-task candidate did <strong>not</strong> beat it: agent time rose {pct(pair_time_delta)}, input tokens {pct(pair_token_delta)}, CDP calls {pct(pair_cdp_delta)}, and commands {pct(pair_command_delta)}. Candidate quality was 3/5 versus an audited matched baseline of {audited_baseline_passes}/5. This is a diagnosis and iteration report, not a victory lap.</p><div class="cards"><div class="card"><div class="label">Attempted cells</div><div class="value">{base['attempted']}</div><small>{base['captcha']} CAPTCHA exclusions</small></div><div class="card"><div class="label">Valid task set</div><div class="value">{base['valid']}</div><small>5 in each of 5 categories</small></div><div class="card"><div class="label">Capability-eligible pass</div><div class="value">{base['eligible_passes']}/{base['eligible']}</div><small>{base['impossible']} judge-marked impossible</small></div><div class="card"><div class="label">Baseline input tokens</div><div class="value">{base['input_tokens']/1_000_000:.1f}M</div><small>{base['commands']} shell/model tool commands</small></div></div><div class="callout warn" style="margin-top:18px"><strong>Read this score carefully.</strong> Raw baseline pass is {base['passes']}/{base['valid']}; capability-eligible pass is {base['eligible_passes']}/{base['eligible']}. Two audited baseline answers became passes when the judge received the full delivered response. CAPTCHA cells are exclusions, and impossible or safety-conflicted tasks are shown separately.</div></div></section>
<section id="method"><div class="wrap"><h2>Method and provenance</h2><div class="grid2"><div class="callout"><h3>Fixed axes</h3><ul class="clean"><li>Candidate and judge: <span class="mono">gpt-5.6-luna</span>, reasoning <span class="mono">max</span>.</li><li>Only v2 executed; v1 was source-inspected, never run.</li><li>Local subscription CLI, no model API keys.</li><li>Fresh 1440×1000 headless Edge profile per cell.</li><li>Search discovery still navigated through v2.</li><li>One-second owned-process-tree sampling.</li></ul></div><div class="callout"><h3>Recorded evidence</h3><ul class="clean"><li>Every harness helper span and typed outcome.</li><li>Sanitized CDP method, latency, request/response byte counts.</li><li>Bounded page/network/performance diagnostics.</li><li>Action screenshots and journal-linked recordings.</li><li>Agent, daemon, browser CPU/RSS/I/O samples.</li><li>Candidate worktree diff and status fingerprints.</li></ul></div></div><p class="small" style="margin-top:16px">The encrypted suite contains 100 tasks (20 per category). This campaign selected a 25-task valid slice, five per category, using category-matched replacements for CAPTCHA exclusions. Baseline runs: v2-luna-telemetry-20260823-b01 through b04. Candidate run: {esc(CANDIDATE_ID)}. Raw task content stays only in run_data and is not reproduced here.</p></div></section>
<section id="baseline"><div class="wrap"><h2>Baseline results</h2><p class="lead">The campaign stopped only after obtaining five non-CAPTCHA tasks per category. Thirteen blocked attempts were excluded, not converted into zeros.</p>{category_pass_chart}<div style="height:18px"></div>{category_table}<div style="height:24px"></div>{task_table}</div></section>
<section id="telemetry"><div class="wrap"><h2>Where the time and tokens went</h2><div class="cards"><div class="card"><div class="label">Valid-cell agent time</div><div class="value">{base['agent_seconds']/3600:.2f} h</div><small>{base['valid']} valid tasks</small></div><div class="card"><div class="label">CDP calls</div><div class="value">{human_int(base['cdp'])}</div><small>{human_int(base['helpers'])} helper spans</small></div><div class="card"><div class="label">Recorded frames</div><div class="value">{human_int(base['frames'])}</div><small>{base_deep['recording_bytes']/1_000_000:.1f} MB JPEG evidence</small></div><div class="card"><div class="label">Captured command output</div><div class="value">{base_steps['command_output_chars']/1_000_000:.1f}M</div><small>characters replayed into context</small></div></div><div class="grid2" style="margin-top:20px">{helper_chart}{method_chart}</div><div class="grid2" style="margin-top:20px"><div class="callout bad"><h3>Navigation waiting dominated the harness</h3><p><strong>{seconds(wait_overhead_ms/1000)}</strong> was spent inside <span class="mono">goto</span> beyond the measured <span class="mono">Page.navigate</span> protocol time. Raw navigation was not the main problem; waiting for lifecycle completion after content was useful was.</p></div><div class="callout warn"><h3>Context was overfed</h3><p><strong>{human_int(base_steps['page_text_output_chars'])}</strong> captured characters came from {base_steps['page_text_commands']} commands containing <span class="mono">page_text()</span>. The old 22.9 KB skill was also read {base_steps['skill_reads']} times and then replayed in the growing agent context.</p></div></div><div style="height:20px"></div>{table(['Telemetry dimension','Observed baseline'],[
['Sanitized CDP payload bytes',f"{base_deep['protocol_request_bytes']/1_000_000:.1f} MB request / {base_deep['protocol_response_bytes']/1_000_000:.1f} MB response"],
['Diagnostics',f"{human_int(base_deep['diagnostic_files'])} snapshots / {human_int(base_deep['diagnostic_events'])} retained failure-lifecycle events"],
['Peak browser tree RSS',f"{base_deep['browser_rss']/1024**3:.2f} GiB across up to {human_int(base_deep['browser_processes'])} owned processes"],
['Peak agent tree RSS',f"{base_deep['agent_rss']/1024**2:.0f} MiB"],
['Summed browser CPU',f"{base_deep['browser_cpu']/3600:.2f} CPU-hours"],
['Largest page resource set',f"{human_int(base_deep['resource_count'])} resources / {base_deep['resource_transfer_bytes']/1_000_000:.1f} MB transferred"],
['Worst sampled event-loop delay',f"{base_deep['event_loop_ms']:.1f} ms"],
['Peak host memory use',f"{base_deep['host_memory_percent']:.1f}%"],
])}</div></section>
<section id="changes"><div class="wrap"><h2>General-purpose v2 changes</h2><p class="lead">Every change targets measured cross-site behavior. The first six were present in candidate c02; the remainder were implemented after examining c02 and therefore have live-browser and test evidence, but no benchmark score claim yet.</p><div class="code-list"><div class="code-item"><strong>1 · Early usable navigation · benchmarked.</strong> <span class="mono">goto</span> can return a parsed, useful document after a conservative grace period while preserving strict lifecycle semantics through <span class="mono">usable_after=None</span>.</div><div class="code-item"><strong>2 · Bounded semantic reads · benchmarked.</strong> <span class="mono">open_page</span> returns landing metadata, main text, links, truncation, and structured challenge evidence in one operation. <span class="mono">page_text</span> defaults to 12,000 characters with explicit paging.</div><div class="code-item"><strong>3 · Smaller decision context · benchmarked.</strong> The operating skill shrank from {human_int(skill_old)} to {human_int(skill_new)} characters ({(1-skill_new/skill_old)*100:.1f}% smaller), and makes the rich bounded helpers the documented first choice.</div><div class="code-item"><strong>4 · Cheaper evidence and diagnostics · benchmarked.</strong> Screenshot metadata shares one evaluation, and diagnostic aggregation avoids redundant Runtime/Network setup.</div><div class="code-item"><strong>5 · Navigation-burst resilience · benchmarked.</strong> The daemon queue now absorbs bounded CDP bursts with a hard byte ceiling. Candidate c02 survived queue peaks above the former 64-frame ceiling.</div><div class="code-item"><strong>6 · Exact transport attribution · benchmarked.</strong> Queue peaks, frames, overflow, close reason, and peer fan-out drops are journaled so a disconnect is diagnosable rather than generic.</div><div class="code-item"><strong>7 · Session-scoped runtime setup · post-run live-validated.</strong> Document scripts, binding, and Log/Performance domains are armed once per live CDP session generation. Candidate c02 spent {human_int(repeated_setup_calls)} CDP calls on just those four repeated setup methods.</div><div class="code-item"><strong>8 · Lazy isolated worlds · post-run live-validated.</strong> Ordinary reads no longer create an isolated execution world; refs and wait operations still request one when they need durable node identity.</div><div class="code-item"><strong>9 · Constant-cost fresh clients · post-run live-validated.</strong> A lifecycle-maintained target cache lets sequential CLI processes adopt the verified live page without listing all targets again. Five separate read-only processes used 17 actual CDP calls total; four were adoption-cache hits, and every later process performed only its useful evaluation.</div><div class="code-item"><strong>10 · Concurrent research with one output ceiling · post-run live-validated.</strong> <span class="mono">open_pages</span> reads independent public URLs concurrently with a total text budget. A three-page real-browser check completed in 2.37 seconds and emitted 1,928 characters under a 3,000-character ceiling.</div><div class="code-item"><strong>11 · More useful digests · post-run live-validated.</strong> Main-content links are prioritized and large select-option lists collapse to the selected value. This exposed the configured search service’s explicit “no results” message instead of spending the text budget on language chrome.</div><div class="code-item"><strong>12 · Remove duplicate recording waits · post-run live-validated.</strong> Recording no longer adds a fixed 150 ms after helpers that already wait. Task #11 recorded 171 <span class="mono">goto</span> calls, so this removes a theoretical 25.7 seconds of duplicate sleeps on that shape without deleting screenshots.</div></div><div class="callout" style="margin-top:20px"><strong>Validation boundary:</strong> 229 focused tests passed, Ruff is clean, and the full v2 suite reported 550 passed, 4 skipped, and 10 known Windows-assumption failures. The post-run work still needs the next paired benchmark before any population-level speed or quality claim.</div></div></section>
<section id="iteration"><div class="wrap"><h2>The failed first optimization was useful</h2><p class="lead">Candidate c01 exposed a latent transport-pressure bug; c02 repaired that bug but still did not win the matched sample. Both runs remain visible rather than being averaged away.</p><div class="grid2"><div class="callout bad"><h3>Candidate c01: event-burst disconnects</h3><p>{first_disconnects} helper failures were classified as <span class="mono">browser_disconnected</span> across four non-CAPTCHA cells. It passed {first_cand['passes']}/{first_cand['valid']}, spent {seconds(first_cand['agent_seconds'])}, and used {human_int(first_cand['input_tokens'])} input tokens. One OM task hit CAPTCHA and was excluded.</p></div><div class="callout"><h3>Candidate c02: transport recovery, not benchmark victory</h3><p>The queue peaked at {human_int(cand_deep.get('transport_queue_frames'))} frames—well above the former 64-frame limit—with {human_int(cand_deep.get('peer_overflows'))} overflows and {final_disconnects} disconnected helpers. The {human_int(cand_deep.get('peer_evictions'))} peer-eviction records were post-close fan-out races, not buffer overflow. Task #5 fell from {seconds(first_web_seconds)} to {seconds(final_web_seconds)}, but c02 was still slower and more token-heavy than matched baseline overall.</p></div></div></div></section>
<section id="ab"><div class="wrap"><h2>Matched five-category sample</h2><p class="lead">One previously valid task per category was rerun with c02. All four efficiency endpoints moved the wrong way, and candidate quality was 3/5 versus {audited_baseline_passes}/5 after matched baseline rejudging. With one live-web observation per task, this is a clear “do not claim a win,” not a precise causal estimate.</p><div class="cards"><div class="card"><div class="label">Agent time</div><div class="value">{delta_label(pair_time_delta)}</div><small>{seconds(bpair['agent_seconds'])} → {seconds(cand['agent_seconds'])}</small></div><div class="card"><div class="label">Input tokens</div><div class="value">{delta_label(pair_token_delta)}</div><small>{human_int(bpair['input_tokens'])} → {human_int(cand['input_tokens'])}</small></div><div class="card"><div class="label">CDP calls</div><div class="value">{delta_label(pair_cdp_delta)}</div><small>{human_int(bpair['cdp'])} → {human_int(cand['cdp'])}</small></div><div class="card"><div class="label">Commands</div><div class="value">{delta_label(pair_command_delta)}</div><small>{bpair['commands']} → {cand['commands']}</small></div></div><div style="height:20px"></div>{table(['Task','Agent time','Input tokens','CDP calls','Score'],pair_rows)}<div class="grid2" style="margin-top:20px">{svg_bars(duration_graph,'Matched task duration',unit=' s')}{svg_bars(token_graph,'Matched task input tokens',unit='k',color='#86a8ff')}</div><div class="callout warn" style="margin-top:20px"><strong>Two benchmark confounds became concrete.</strong> Task #5 stopped at an ordinary cookie banner because the benchmark prompt mislabeled all consent as a blocker; that rule is now corrected. Task #11 spent nearly the full allowance researching after the configured HTML search endpoint returned an explicit no-results page and a supplied official URL was stale. Those defects explain why the run is diagnostically valuable, but they do not retroactively turn its score into a pass.</div><p class="small">Baseline scores marked “audit” were rejudged with the same full-response input contract as the candidate. Original raw verdicts and result files remain untouched; this report re-summarizes raw journals only to remove duplicated daemon/client CDP evidence.</p></div></section>
<section id="evidence"><div class="wrap"><h2>Visual evidence</h2><p class="lead">Selected frames from the local action recordings. They are evidence of rendered state, not automatic proof of task correctness.</p>{render_gallery(gallery)}</div></section>
<section id="validity"><div class="wrap"><h2>Validity assessment</h2><div class="grid2"><div class="callout bad"><h3>Not leaderboard-ready</h3><ul class="clean"><li>13/38 attempted cells encountered CAPTCHA.</li><li>Several tasks were impossible, stale, or conflicted with the product’s no-submit safety boundary.</li><li>The configured search page was reachable but semantically unusable: it returned no results during live validation.</li><li>One run per task cannot estimate model or live-web variance.</li><li>The original judge input omitted answer detail and produced two demonstrated false negatives in six audited tasks.</li></ul></div><div class="callout"><h3>Still highly useful</h3><ul class="clean"><li>The five-category sample exposed repeatable hot paths and a real queue-overflow defect.</li><li>Telemetry separates model time, helper time, actual CDP, browser resources, recording, and transport pressure.</li><li>CAPTCHA exclusions are explicit and auditable.</li><li>Matched reruns use immutable task IDs and candidate fingerprints.</li><li>Failures revealed benchmark-contract defects that a pass-rate table would conceal.</li></ul></div></div><p class="verdict" style="margin-top:22px"><strong>Verdict:</strong> keep the benchmark as an engineering telemetry suite, but semantically preflight task dependencies, freeze judging, and add paired repetition before treating the aggregate score as product truth.</p></div></section>
</main>
"""

    report_path = TMP / "v2-luna-telemetry-benchmark-report.html"
    report_path.write_text(
        shell(
            "Browser Harness v2 telemetry benchmark",
            report_body,
            description="Full v2-only Luna Max benchmark and optimization report",
            links=[("top", "Top"), ("outcome", "Outcome"), ("method", "Method"), ("baseline", "Baseline"), ("telemetry", "Telemetry"), ("changes", "Changes"), ("iteration", "Iteration"), ("ab", "A/B"), ("evidence", "Evidence"), ("validity", "Validity"), ("reflection", "Reflection →")],
        ).replace('href="#reflection"', 'href="v2-benchmark-efficiency-reflection.html"'),
        encoding="utf-8",
        newline="\n",
    )

    reflection_body = f"""
<header class="hero" id="top"><div class="wrap"><div class="eyebrow">Post-run reflection</div><h1>How to make this benchmark harder—and fairer</h1><p>The run succeeded at its real first purpose: it generated enough telemetry to distinguish browser work from agent-loop waste. It also showed why an initial benchmark should be treated as an instrument under calibration, not a scoreboard.</p><div class="meta"><span class="pill"><strong>Focus</strong> speed by efficiency</span><span class="pill"><strong>Also</strong> reliability + token discipline</span><span class="pill"><strong>Constraint</strong> no task/site overfitting</span></div></div></header>
<main>
<section id="reflection"><div class="wrap"><h2>What just happened</h2><div class="grid2"><div class="callout"><h3>The useful surprise</h3><p>The browser protocol itself was usually fast. Waste clustered one layer higher: lifecycle waits after useful DOM existed, repeated runtime setup across short CLI processes, repeated model decisions, broad text dumps, and oversized instructions replayed through context.</p></div><div class="callout warn"><h3>The uncomfortable result</h3><p>The first clean five-task candidate was 14.3% slower, used 80.2% more input tokens, made 1.2% more actual CDP calls, and scored 3/5 versus an audited 4/5 baseline. CAPTCHA density, stale dependencies, a broken search source, ordinary-cookie semantics, and the original lossy judge contract also made the score less stable than the telemetry.</p></div></div><p class="verdict" style="margin-top:24px">The right optimization target is <strong>decisions per verified fact</strong>, not raw CDP cleverness. The failed A/B prevented a premature claim; its telemetry then led to session-scoped setup, cached page adoption, batch research, tighter digests, and removal of duplicate waits.</p></div></section>
<section id="principles"><div class="wrap"><h2>Optimization principles</h2><div class="code-list"><div class="code-item"><strong>Return rich, bounded evidence by default.</strong> A digest should say what it omitted, expose links, and identify challenge state. Rich does not mean unbounded.</div><div class="code-item"><strong>Wait for usefulness, not ceremony.</strong> Lifecycle events are evidence, but “load” is not always the state the caller needs. Preserve a strict option for workflows that truly require it.</div><div class="code-item"><strong>Batch at semantic boundaries.</strong> Navigation + read, schema + form plan, and multi-URL fetches are stable general operations. Task-specific shortcuts are not.</div><div class="code-item"><strong>Make the cheap path the documented path.</strong> A capability unused by the agent has zero benchmark effect. Instruction size and example order are part of system performance.</div><div class="code-item"><strong>Never buy speed by hiding failure.</strong> Every early exit carries lifecycle, truncation, challenge, and typed outcome evidence.</div></div></div></section>
<section id="implemented"><div class="wrap"><h2>What the telemetry already changed</h2><p class="lead">These are code changes, not roadmap wishes. The late items were validated in a real owned Edge session after c02, so they are evidence-backed but deliberately excluded from the c02 score claim.</p><div class="cards"><div class="card"><div class="label">Fresh-process live check</div><div class="value">17 CDP</div><small>5 read-only processes; 4 cache hits</small></div><div class="card"><div class="label">Concurrent reading</div><div class="value">2.37 s</div><small>3 pages, 1,928 output chars</small></div><div class="card"><div class="label">Repeated c02 setup</div><div class="value">{human_int(repeated_setup_calls)}</div><small>four methods now session-scoped</small></div><div class="card"><div class="label">Focused validation</div><div class="value">229</div><small>tests passed; Ruff clean</small></div></div><div class="code-list" style="margin-top:20px"><div class="code-item"><strong>Persistent daemon state now earns its keep.</strong> Runtime scripts, binding, diagnostic domains, and the verified page target survive short-lived client processes and re-arm on CDP session replacement.</div><div class="code-item"><strong>Read paths do only read work.</strong> A subsequent fresh process no longer lists targets, installs scripts, creates an isolated world, or re-enables diagnostic domains before one evaluation.</div><div class="code-item"><strong>Research is bounded globally.</strong> <span class="mono">open_pages</span> applies one output budget across the batch, and the compact digest prioritizes main content over giant navigation/select chrome.</div><div class="code-item"><strong>Telemetry now distinguishes work from observation.</strong> Client-forwarded and daemon-internal CDP have explicit origins; old duplicated journals are re-summarized without mutating raw results.</div></div></div></section>
<section id="backlog"><div class="wrap"><h2>Next code opportunities</h2><p class="lead">Prioritized by expected real-world effect and confidence. None depends on a benchmark task ID or target website.</p>{table(['Priority','Idea','Expected effect','Confidence','Risk / guardrail'],[
['P0','Digest delta cache','Return unchanged/versioned blocks by reference instead of replaying the same page text','High','Invalidate on navigation and meaningful DOM mutation; explicit full-read override'],
['P0','Output budget telemetry','Record emitted characters, semantic blocks, and truncation per invocation','High','Store counts and digests, never sensitive output values'],
['P1','Adaptive navigation grace','Learn per-origin usable-vs-load timing locally and clamp to safe bounds','Medium','No cross-user telemetry; strict mode unchanged'],
['P1','Stable semantic cursors','Page meaningful content by heading/list/table blocks instead of character offsets','Medium','Expose DOM version so stale cursors fail typed'],
['P1','Recording profiles','Evidence / review / cinematic modes with measured screenshot costs','High','Benchmark fixes the profile in its manifest'],
['P1','Action-result fusion','Return the changed digest region and validation state with click/type/select','Medium','Hard output ceiling; never infer success without DOM evidence'],
['P2','Resource-aware parallelism','Throttle worker creation from measured RSS/renderer pressure','Medium','Never exceed caller cap; deterministic telemetry'],
['P2','Read-only network extraction','Promote discovered same-origin JSON endpoints into one counted fetch_all call','Medium','GET/HEAD only; response and byte bounds'],
['Reject','Site- or answer-specific selectors','May raise benchmark score without improving the product','High','Keep URL/task IDs out of production logic'],
])}</div></section>
<section id="benchmark"><div class="wrap"><h2>Benchmark changes before the next leaderboard run</h2><div class="code-list"><div class="code-item"><strong>1 · Semantically preflight eligibility.</strong> HTTP success is insufficient. Confirm that CAPTCHA/auth walls are absent, supplied URLs have the promised content, and the configured search page returns at least one real result. Publish typed exclusion reasons and use a predeclared category replacement queue.</div><div class="code-item"><strong>2 · Separate ordinary consent from authorization.</strong> Cookie/privacy banners should be declined or reduced to necessary-only and the task should continue; OAuth, identity, account, payment, and submission consent remain blockers. This prompt defect is already corrected for the next run.</div><div class="code-item"><strong>3 · Separate three scores.</strong> Report browser capability, whole-product safety/completion, and task availability. A safe submit refusal is good product behavior but not evidence of failed DOM navigation.</div><div class="code-item"><strong>4 · Freeze and calibrate judging.</strong> Hash the exact judge contract, always include the full delivered response, and calibrate on human-labelled answers. Full-response input is now implemented; both arms must still be judged under one immutable version.</div><div class="code-item"><strong>5 · Add paired repeats.</strong> Run at least three randomized repeats per task and report medians, dispersion, and paired confidence intervals. One live-web observation is diagnostic, not a product estimate.</div><div class="code-item"><strong>6 · Version task truth and dependencies.</strong> Timestamp dynamic facts, pin required sources where lawful, record dependency health, and refresh or retire stale premises.</div><div class="code-item"><strong>7 · Measure observability overhead.</strong> Pair telemetry-on and telemetry-light calibration cells. Task #11 alone recorded 178 screenshots; evidence has value, but its time, CDP, disk, and model-context cost must be visible.</div><div class="code-item"><strong>8 · Publish task-shape coverage.</strong> Stratify search, extraction, synthesis, pagination, download, interaction, and authenticated workflows—not only five broad source categories.</div></div></div></section>
<section id="risks"><div class="wrap"><h2>Risks in the current optimization</h2><div class="grid2"><div class="callout warn"><h3>Early usable can be too early</h3><p>An SPA may render a header before its data. The grace period and strict override reduce this risk; the next test should measure completeness on delayed-content fixtures and real pages.</p></div><div class="callout warn"><h3>Bounded text can omit the answer</h3><p>12,000 characters is a guardrail, not a relevance claim. Truncation and cursors must stay visible, and batch totals must not silently starve later pages.</p></div><div class="callout warn"><h3>Persistent caches can go stale</h3><p>Runtime and target caches are keyed to live session generations and are invalidated on target/session events. Crashes, OOPIF replacement, and rapid close/reopen sequences deserve sustained stress coverage.</p></div><div class="callout warn"><h3>Live-web variance remains</h3><p>Cache, site state, source availability, and anti-bot behavior changed across time. Even after matched baseline rejudging, five-task quality and wall-time deltas are not population estimates.</p></div></div></div></section>
<section id="next"><div class="wrap"><h2>Recommended next experiment</h2><p class="verdict">Freeze the current post-run v2 tree as c03. Semantically preflight a 15-task non-CAPTCHA set (three per category), then compare the saved v2 baseline snapshot with c03 in randomized paired order with three repeats. Execute v2 only and use one immutable full-response Luna judge contract. Primary endpoints: median agent time, input tokens, actual CDP calls, model command count, and pass preservation.</p><div class="callout" style="margin-top:20px"><strong>Success criterion:</strong> fewer model decisions and browser calls per completed task with no statistically credible reliability loss. Treat any lower pass result as a stop signal until adjudicated; do not trade correctness for a prettier latency mean.</div></div></section>
</main>
"""
    reflection_path = TMP / "v2-benchmark-efficiency-reflection.html"
    reflection_path.write_text(
        shell(
            "Browser Harness v2 benchmark reflection",
            reflection_body,
            description="Reflection and improvement roadmap for the v2 browser benchmark",
            links=[("top", "Top"), ("reflection", "What happened"), ("principles", "Principles"), ("implemented", "Implemented"), ("backlog", "Code backlog"), ("benchmark", "Benchmark"), ("risks", "Risks"), ("next", "Next run"), ("report", "Full report →")],
        ).replace('href="#report"', 'href="v2-luna-telemetry-benchmark-report.html"'),
        encoding="utf-8",
        newline="\n",
    )

    analysis = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "baseline": {key: value for key, value in base.items() if not key.endswith("_rows")},
        "candidate": {key: value for key, value in cand.items() if not key.endswith("_rows")},
        "first_candidate": {
            key: value for key, value in first_cand.items() if not key.endswith("_rows")
        },
        "baseline_steps": base_steps,
        "candidate_steps": cand_steps,
        "baseline_categories": base_categories,
        "baseline_helpers": helpers,
        "baseline_protocol": methods,
        "candidate_protocol": candidate_methods,
        "baseline_deep_telemetry": base_deep,
        "candidate_deep_telemetry": cand_deep,
        "matched": {
            "baseline": {key: value for key, value in bpair.items() if not key.endswith("_rows")},
            "candidate": {key: value for key, value in cand.items() if not key.endswith("_rows")},
            "agent_time_delta": pair_time_delta,
            "input_token_delta": pair_token_delta,
            "cdp_delta": pair_cdp_delta,
            "command_delta": pair_command_delta,
            "audited_baseline_passes": audited_baseline_passes,
            "tasks": matched_task_analysis,
        },
        "post_run_live_validation": {
            "status": "not included in candidate c02 benchmark scores",
            "fresh_read_only_processes": 5,
            "actual_cdp_calls": 17,
            "adoption_cache_hits": 4,
            "target_listings": 1,
            "later_process_behavior": "one useful Runtime.evaluate each",
            "open_pages_urls": 3,
            "open_pages_wall_seconds": 2.37,
            "open_pages_output_chars": 1928,
            "open_pages_output_budget_chars": 3000,
            "candidate_c02_repeated_setup_calls_now_session_scoped": repeated_setup_calls,
        },
        "validation": {
            "focused_tests_passed": 229,
            "full_tests_passed": 550,
            "full_tests_skipped": 4,
            "full_tests_failed": 10,
            "full_test_failure_scope": "pre-existing Windows-only socket/path assumptions",
            "ruff": "clean",
            "benchmark_tests_passed": 19,
        },
        "baseline_rejudge": rejudge,
    }
    analysis_path = TMP / "v2-telemetry-analysis.json"
    analysis_path.write_text(
        json.dumps(analysis, indent=2), encoding="utf-8", newline="\n"
    )
    return report_path, reflection_path, analysis_path


if __name__ == "__main__":
    for output in build():
        print(output)
