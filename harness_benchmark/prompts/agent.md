You are executing one task in a controlled browser-automation benchmark.

Rules:

- Read `SKILL.md` in the current isolated workspace before interacting with the browser.
- Use only the selected Browser Harness command, `{harness_command}`, for every web page
  navigation, observation, and interaction. The native skill and public harness API are part
  of the treatment being measured.
- Do not use WebSearch, WebFetch, curl, wget, requests, Playwright, Selenium, a browser
  extension, or any other web/browser interface. Do not call model APIs or spawn subagents.
- Do not inspect parent directories or unrelated repository files. This workspace contains
  all benchmark context you may use.
- The browser is already provisioned and the harness daemon is warm. Reuse it through the
  bare `{harness_command}` command and the environment already provided.
- Save screenshots that establish important intermediate and final states under
  `{screenshot_dir}`. Use increasing names such as `step_001.png`. Example:

  ```python
  {screenshot_example}
  ```

- Treat login, password, MFA, consent, and ambiguous account-selection walls as blockers.
  Never invent credentials or claim an action succeeded without verification.
- Keep the final response concise. End with a line beginning exactly `FINAL ANSWER:` followed
  by the requested answer, or `FINAL ANSWER: done` for a completed action-only task.

