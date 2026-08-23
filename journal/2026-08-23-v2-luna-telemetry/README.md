# v2 Luna telemetry campaign — 2026-08-23

This is the durable record of the first full v2-only harness telemetry campaign and the
efficiency work it motivated.

## Fixed axes

- Harness executed: v2 only. V1 was source-inspected but never run in this campaign.
- Candidate: Codex `gpt-5.6-luna`, reasoning `max`.
- Judge: Codex `gpt-5.6-luna`, reasoning `max`.
- CAPTCHA cells were excluded and replaced until each of five categories contained five valid
  tasks.
- No Claude process, model, or API was used in this campaign.

The baseline attempted 38 cells: 13 were excluded for CAPTCHA, leaving 25 valid tasks. Raw pass
was 7/25; capability-eligible pass was 7/21 after four judge-marked impossible tasks. A six-task
full-response audit found two false negatives in the original marker-only judge input.

Candidate c02 did **not** beat its five matched baseline tasks: agent time increased 14.3%, input
tokens 80.2%, actual CDP calls 1.2%, and model/shell commands 8.8%. It scored 3/5 versus an
audited matched baseline of 4/5. The report deliberately retains this negative result.

Post-run code changes were validated separately and are not attributed to c02. In the strongest
live check, five sequential fresh read-only clients used 17 actual CDP calls in total, four page
adoptions were cache hits, and every later client performed only its useful evaluation. A
three-page `open_pages` check completed in 2.37 seconds and emitted 1,928 characters under a
3,000-character total budget.

## Reports and evidence

- [`v2-luna-telemetry-benchmark-report.html`](v2-luna-telemetry-benchmark-report.html) — full
  baseline, telemetry, implementation, matched A/B, validity, graphs, and screenshot evidence.
- [`v2-benchmark-efficiency-reflection.html`](v2-benchmark-efficiency-reflection.html) — benchmark
  calibration findings and the remaining general-purpose efficiency backlog.
- [`v2-telemetry-analysis.json`](v2-telemetry-analysis.json) — machine-readable aggregate used by
  the reports.
- [`baseline-rejudge-v2-luna-full-response.json`](baseline-rejudge-v2-luna-full-response.json) —
  six-task full-response audit; original result files were not rewritten.
- `assets/` — six privacy-reviewed action frames embedded by the full report.
- `qa/` — final rendered-page checks for the report top, matched A/B, evidence gallery, reflection
  top, and implemented-work section.

## Reproduction

The raw benchmark evidence remains ignored under `run_data/harness_comparisons/`. With that local
evidence and the sibling `../v2` checkout present, regenerate the reports from the repository
root with:

```powershell
uv run python journal/2026-08-23-v2-luna-telemetry/generate_v2_reports.py
```

The generator re-summarizes raw journals with current CDP-origin de-duplication rules but does
not modify raw result files. Re-running the full-response judge audit is optional and spends
subscription model time:

```powershell
uv run python journal/2026-08-23-v2-luna-telemetry/rejudge_baseline_full_response.py
```

Detailed judge workspaces are written beneath ignored `run_data/rejudge_audits/`, not this
journal entry.

Validation at publication: 229 focused v2 tests passed; Ruff was clean; the full v2 suite was
550 passed, 4 skipped, and 10 pre-existing Windows socket/path-assumption failures. The benchmark
repository suite passed 19 tests. A new paired c03 run is required before claiming that the
post-run v2 code beats baseline.
