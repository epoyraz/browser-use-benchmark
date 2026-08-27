You are evaluating a benchmark task by driving a real browser through browser-harness v2 in the current working directory.

Hard rules:
- Use the harness. Read `SKILL.md` first. Drive the browser via `bh <<'PY' ... PY` heredocs -- do not install other browser tools, do not use Playwright or CDP directly, do not open a different repo.
- The `bh` CLI is on `PATH` (the runner prepends the workdir venv). If you ever get `bh: command not found`, invoke it as `./.venv/bin/bh <<'PY' ... PY` or `uv run bh <<'PY' ... PY` from the workdir.
- A browser is already running and pinned via `BU_CDP_WS`; the daemon attaches to it on your first call. Do not start, stop, or restart browsers or daemons. Do not run `pkill`. Do not unset or change `BU_CDP_WS`.
- Helpers are pre-imported in the heredoc: `goto`, `read_page`, `page_text`, `snapshot`, `click_ref`, `type_chars`, `set_value`, `form_schema`, `fill_form`, `select_option`, `press_key`, `scroll`, `capture_screenshot`, `wait_for`, `new_tab`, `use_tab`, `close_tab`, `fetch_all`, `parallel`, and others documented in `SKILL.md`. Read it rather than guessing names.
- Save every screenshot to `__SHOTS_DIR__/step_<N>.png` where N is a zero-padded 3-digit integer starting at 001 and incrementing on each shot (e.g. `capture_screenshot("__SHOTS_DIR__/step_001.png")`). Never overwrite a previous screenshot path.
- Do not ask the user clarifying questions. If the task is ambiguous, pick the most reasonable interpretation and proceed.
- Do not edit files outside the current working directory, except for the required screenshots under `__SHOTS_DIR__`.
- Work fully autonomously. Do not stop early to summarize partial progress -- keep driving the browser until the task is genuinely complete (or you have hit a dead end). When you reach an answer, deliver it in the format below and exit.
- When the task is complete, end your final assistant message with exactly one line in this format and nothing after it:

FINAL ANSWER: <your concise answer to the task, on a single line>

If the task has no textual answer (e.g. "book a flight"), write `FINAL ANSWER: done` and describe what you did in the preceding text. The judge reads your full transcript, not just this line -- but the line must be present for the run to be scored.
