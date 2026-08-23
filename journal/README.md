# Benchmark journal

This directory keeps dated, reviewable benchmark reports and the minimal aggregate evidence
needed to interpret them. Each entry must distinguish raw results from audited/recomputed
views, name the exact model and harness axes, state exclusions, and preserve failed or negative
experiments instead of reporting only wins.

Do not commit `run_data/` or judge workspaces here. Those contain decrypted task text, ground
truth, model trajectories, and other local-only evidence. A journal entry may include aggregate
JSON, privacy-reviewed screenshots, and a reproducible report generator when those artifacts do
not disclose the task corpus.

## Entries

- [`2026-08-23-initial-pilot/`](2026-08-23-initial-pilot/) — historical four-cell v1/v2 pilot;
  every cell was excluded because the selected task encountered CAPTCHA in at least one arm.
- [`2026-08-23-v2-luna-telemetry/`](2026-08-23-v2-luna-telemetry/) — definitive first v2-only
  five-category telemetry campaign, matched candidate reruns, code-efficiency analysis, and
  post-run live validation.
