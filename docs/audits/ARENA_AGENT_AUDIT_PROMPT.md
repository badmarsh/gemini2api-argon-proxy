# Arena Agent Work Order: Exhaustive Audit, Bug Hunting, GUI Polishing & Test Expansion

> **Instructions for Arena Agent:**
> Copy and paste this prompt directly into your Arena.ai Agent Mode session for repository `badmarsh/gemini2api-argon-proxy` on branch `main`.
> You are operating as a Senior Staff Systems & Frontend Engineer performing an autonomous verification pass. Follow the hard constraints and acceptance gates strictly.

---

## 1. Verified Baseline & Environment

- **Repository:** `badmarsh/gemini2api-argon-proxy` (branch: `main`, commit: `9e7b136` or later).
- **Service:** High-concurrency OpenAI / Gemini RPC / Responses API proxy converting Google Gemini Web sessions into unified API endpoints with Gemini 4 Argon thinking support.
- **Current Gates (Verified Green):**
  - Unit tests: `pytest tests -q` >= **1205 passed**, 4 skipped (POSIX permissions on Windows).
  - Evals test suite: `pytest evals -q` = **240 passed**, 0 failed.
  - Linter: `ruff check --no-cache app tests docs` = **All checks passed!**.
- **Python Environment Gotcha:** Always invoke the interpreter with installed project dependencies (e.g. `python -m pytest tests -q` or `C:\Python314\python.exe`). Ensure `slowapi` and dev dependencies are present.

---

## 2. Hard Invariants (Violating Any Fails the Task)

1. **Gates Must Only Grow, Never Shrink:** Never delete, skip, or weaken an existing test. Unit tests must stay >= 1205, evals >= 240, ruff 100% clean.
2. **Strict i18n Parity (5 Locales):** `static/app/i18n.js` has identical key sets across `zh-CN`, `en-US`, `ja-JP`, `ko-KR`, and `zh-TW`. If you introduce any new user-facing string, provide translations in all 5 languages in the same commit.
3. **Absolute Privacy Boundaries:** Never render, log, or export cleartext cookies (`__Secure-1PSID`, `__Secure-1PSIDTS`), API keys, user chat messages, or full account emails in public logs.
4. **Clean Runs Directory:** Babysit benchmark tests must use `runs_dir=tmp_path`. Never leave `test_*` folders in `./runs`.
5. **No Broken Protocols:** Maintain the 81-element Argon thinking payload, SSE keepalive interval at 3.0s, and backward-compatible OpenAI/Gemini/Responses stream frames.

---

## 3. Work Packages & Action Items

### Wave 1: Security & Protocol Robustness Audit
1. **Client Disconnection Slot Recovery:**
   - Audit `app/routers/openai.py`, `app/routers/responses.py`, `app/routers/gemini.py`, and `app/routers/claude.py`.
   - Ensure that if an HTTP client drops an active SSE connection mid-stream (`ClientDisconnect` / `ConnectionResetError`), the account lease in `account_pool.release(...)` is unconditionally executed in a guaranteed `finally` block, decrementing `active_requests`.
2. **Dynamic DOM XSS Audit:**
   - Audit `static/app/gems.js`, `static/app/logs.js`, and `static/app/babysit.js`.
   - Verify every dynamic string injected via `.innerHTML` is sanitized using `escapeHtml(...)` or `escapeAttr(...)`.
3. **Event Listener & Timer Leaks in SPA:**
   - Verify that switching tabs (`#dashboard`, `#logs`, `#usage-stats`, `#babysit`, `#gems`, `#settings`) clears background timers (`clearInterval`) to prevent memory leaks and stacked polling.

### Wave 2: GUI & UX Polishing
1. **Available Models & Quick Integration (2-Column Polish):**
   - Verify responsive behavior of `.models-two-col-layout` in `static/components/section-dashboard.html` and `.css`.
   - Ensure snippet tabs (OpenAI cURL, Python, Gemini cURL) match container height and offer smooth copy feedback.
2. **Account Quota Badges (#accounts):**
   - Ensure the newly added Quota / Rate Limit badges (`Available`, `Cooldown XXs`, `Session Expired`) render seamlessly across all screen sizes and themes.
3. **Theme Contrast Across 4 Developer Themes:**
   - Verify contrast ratios in `vercel-dark`, `vercel-light`, `supabase-dark`, `supabase-light`.
   - In light themes, ensure table borders, code blocks in pre tags, and status badges maintain WCAG AA contrast (>= 4.5:1).
4. **Logs Section Experience (#logs):**
   - Ensure the default 100 entries table renders with smooth virtual scrolling or pagination controls and crisp column headers.
   - Inspect JSON detail modals for zero-overflow formatting.

### Wave 3: Test Suite Expansion
1. **Add Comprehensive i18n Automated Parity Test:**
   - Create `tests/unit/test_i18n_parity_comprehensive.py` verifying that all 5 locales share the exact same key set.
2. **Add Unit Tests for Account Quota Cards:**
   - Expand `tests/unit/test_account_card_rendering.py` to assert rendering of `_accountQuotaRow` across active, cooldown, and error states.
3. **Add Tests for Stream Disconnect Slot Release:**
   - Add a test verifying `account.active_requests` drops to 0 when generator is abruptly closed.

### Wave 4: Documentation & Delivery
1. Update `CHANGELOG.md` under `[Unreleased]` with clear summaries of fixed bugs, security hardening, and GUI improvements.
2. Run full verification pass:
   ```bash
   ruff check --no-cache app tests docs
   pytest tests -q
   pytest evals -q
   ```
3. Commit cleanly with conventional commit messages and push to branch.
