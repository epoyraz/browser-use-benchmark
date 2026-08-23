# Initial harness pilot — 2026-08-23

Status: **historical and superseded** by the later v2-only Luna telemetry campaign.

This pilot followed the original request to compare v1 and v2 with Codex Terra and Claude Opus,
using Luna Max as judge. It ran four cells over one task. The task encountered a CAPTCHA in at
least one arm, so the task-wide exclusion rule removes all four cells from scoring and timing
comparisons. The pilot therefore produced no official benchmark score.

It is retained because a benchmark journal should preserve failed calibration work and because
the run exposed order/site-state confounding, CAPTCHA-policy problems, and lossy score semantics.
It must not be combined with, or presented as evidence for, the later v2-only campaign. No model
from this pilot was invoked while archiving these files.

## Artifacts

- [`harness-benchmark-first-results.html`](harness-benchmark-first-results.html) — immutable
  first-results report and raw audit interpretation.
- [`harness-benchmark-improvement-plan.html`](harness-benchmark-improvement-plan.html) — the
  calibration problems and proposed corrections identified immediately after the pilot.
- `harness-benchmark-assets/` — five privacy-reviewed screenshots referenced by the reports.

Raw cells, decrypted tasks, model logs, and judge prompts remain under ignored `run_data/` or
local temporary storage and are intentionally not committed.
