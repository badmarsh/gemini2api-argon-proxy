# Arena Agent Audit, Bug Fix, GUI Polish & Test Expansion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Conduct an exhaustive security, protocol, and UI/UX audit of `gemini2api-argon-proxy`, resolve any latent bugs and edge cases, polish dashboard interfaces across all themes and viewports, and expand test coverage to 100% deterministic green gates.

**Architecture:** Python 3.12/3.14 + FastAPI backend with async streaming pipelines, coupled with a modular vanilla ES6 + CSS variables frontend. All audits must preserve existing protocols (OpenAI, Gemini RPC, Responses API, Claude messages), enforce strict 5-locale i18n parity, and maintain bounded privacy/redaction boundaries.

**Tech Stack:** Python 3.14 / FastAPI / Starlette / Uvicorn / curl_cffi / Pytest / Ruff / Vanilla JS (ES6 modules) / CSS Variables / Docker.

**Spec:** [docs/audits/arena-2026-10-17/RESOLUTION_PROMPT.md](file:///c:/Users/marek/Documents/gemini2api/docs/audits/arena-2026-10-17/RESOLUTION_PROMPT.md) and current production state at HEAD `9e7b136`.

## Global Constraints

- **Existing Gates Must Stay Green and Only Grow:**
  - `pytest tests -q` >= 1205 passed, 0 failed (Windows POSIX skips allowed).
  - `pytest evals -q -p no:cacheprovider` >= 240 passed, 0 failed.
  - `ruff check --no-cache app tests docs` -> `All checks passed!`.
- **Interpreter Requirement:** Always use `C:\Python314\python.exe` on Windows (or active venv with `slowapi` and dev dependencies installed). Default Python 3.10 lacks project dependencies.
- **Strict i18n Parity Across 5 Locales:** Every UI string must exist in `zh-CN`, `en-US`, `ja-JP`, `ko-KR`, `zh-TW` in [static/app/i18n.js](file:///c:/Users/marek/Documents/gemini2api/static/app/i18n.js) with 0 drift.
- **Privacy & Security Boundaries:** Never log, store, or display sensitive cookies (`__Secure-1PSID`, `__Secure-1PSIDTS`), API keys, account identifiers in cleartext, or request bodies (unless body-logging is explicitly enabled).
- **Zero Pollution of `runs/`:** Any test creating babysit runs MUST pass `runs_dir=tmp_path`. Never leave `test_*` artifacts in `./runs`.
- **Atomic Persistence:** All persistent state (`data/*.json`) must use `app.utils.atomic_io.atomic_write_json` with `os.replace` semantics.

## Review Focus

1. **Client Disconnect Mid-Stream Slot Leak:** If an SSE client disconnects during token generation, `account_pool` active request counters and locks must release immediately via `finally` blocks.
2. **i18n Key Missing Fallback:** Any missing key in `i18n.js` falling back to raw key text must be caught by automated parity tests.
3. **DOM XSS in Logs & Gem Manager:** User-provided inputs (gem names, prompt descriptions, search filter queries, custom headers) must pass through `escapeHtml` / `escapeAttr` before DOM insertion.
4. **Theme Contrast in Light Modes:** Ensure table headers, badge texts, and code snippets in `vercel-light` and `supabase-light` have WCAG AA contrast (>= 4.5:1).
5. **Timer Accumulation on Route Switch:** Switching rapidly between Dashboard, Logs, Usage Stats, and Babysit must clear previous polling intervals (`clearInterval`) without leaking background network requests.

---

### Task 1: Audit & Harden i18n Parity and Dynamic Strings

**Files:**
- Modify: `static/app/i18n.js:1-2500`
- Modify: `static/app/app.js:460-500`
- Test: `tests/unit/test_i18n_parity_comprehensive.py`

**Interfaces:**
- Consumes: `t(key)` from `static/app/i18n.js`
- Produces: 100% synchronized key dictionary for all 5 languages including new Quota & Quick Integration strings.

- [ ] **Step 1: Write the failing test for complete i18n key parity**

```python
# tests/unit/test_i18n_parity_comprehensive.py
import re
from pathlib import Path
import pytest

_I18N_JS = Path("static/app/i18n.js")
_LANGS = ("zh-CN", "en-US", "ja-JP", "ko-KR", "zh-TW")

def test_all_five_locales_have_identical_key_sets():
    content = _I18N_JS.read_text(encoding="utf-8")
    # Extract keys per language block
    keys_by_lang = {}
    for lang in _LANGS:
        match = re.search(rf"'{lang}':\s*\{{(.*?)\n\s*\}},", content, re.DOTALL)
        assert match, f"Language block {lang} not found in i18n.js"
        block = match.group(1)
        found_keys = set(re.findall(r"'([a-zA-Z0-9_.-]+)':", block))
        keys_by_lang[lang] = found_keys

    baseline = keys_by_lang["en-US"]
    for lang in _LANGS:
        diff_missing = baseline - keys_by_lang[lang]
        diff_extra = keys_by_lang[lang] - baseline
        assert not diff_missing, f"{lang} missing keys: {diff_missing}"
        assert not diff_extra, f"{lang} extra keys: {diff_extra}"
```

- [ ] **Step 2: Run test to verify key drift**

Run: `C:\Python314\python.exe -m pytest tests/unit/test_i18n_parity_comprehensive.py -v`
Expected: Output showing any missing keys across locales.

- [ ] **Step 3: Implement missing keys in `static/app/i18n.js` and use `t()` in `static/app/app.js`**

Add missing translations for `accounts.quotaStatus`, `accounts.quotaPolicy`, `accounts.quotaDynamicDesc`, and `dashboard.quickIntegration` across `zh-CN`, `en-US`, `ja-JP`, `ko-KR`, `zh-TW`.

- [ ] **Step 4: Run test to verify it passes**

Run: `C:\Python314\python.exe -m pytest tests/unit/test_i18n_parity_comprehensive.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add static/app/i18n.js static/app/app.js tests/unit/test_i18n_parity_comprehensive.py
git commit -m "fix(i18n): enforce exact key parity across all 5 locales with automated test"
```

---

### Task 2: Account Quota & Cooldown Rendering Unit Tests

**Files:**
- Modify: `tests/unit/test_account_card_rendering.py`
- Modify: `static/app/app.js:460-505`

**Interfaces:**
- Consumes: `_accountQuotaRow(account)` in `static/app/app.js`
- Produces: Verified DOM representation of cooldown countdowns, active slots, and session status.

- [ ] **Step 1: Write test cases for `_accountQuotaRow` in `test_account_card_rendering.py`**

```python
# Add to tests/unit/test_account_card_rendering.py:
@_needs_node
def test_account_cooling_down_renders_cooldown_badge(tmp_path):
    out = _render([_acc(cooling_down=True, cooldown_remaining_s=42.3)], tmp_path)
    assert "badge-warning" in out["list"]
    assert "42s" in out["list"] or "42" in out["list"]

@_needs_node
def test_account_active_renders_available_badge(tmp_path):
    out = _render([_acc(cooling_down=False, cooldown_remaining_s=0, active_requests=1)], tmp_path)
    assert "badge-success" in out["list"]
    assert "1 active" in out["list"]

@_needs_node
def test_account_disabled_renders_disabled_badge(tmp_path):
    out = _render([_acc(status="disabled")], tmp_path)
    assert "badge-secondary" in out["list"]
```

- [ ] **Step 2: Run tests to verify failures/passes**

Run: `C:\Python314\python.exe -m pytest tests/unit/test_account_card_rendering.py -k "test_account_" -v`

- [ ] **Step 3: Implement clean localized badge strings in `_accountQuotaRow`**

Ensure `_accountQuotaRow` consumes `t()` translations for `Available`, `Cooldown`, `Session Expired`, and `Disabled`.

- [ ] **Step 4: Verify test suite passes**

Run: `C:\Python314\python.exe -m pytest tests/unit/test_account_card_rendering.py -v`
Expected: PASS (13 passed).

- [ ] **Step 5: Commit**

```bash
git add static/app/app.js tests/unit/test_account_card_rendering.py
git commit -m "test(accounts): add unit test coverage for quota and cooldown badges"
```

---

### Task 3: Client Disconnect & Stream Slot Reclaim Invariant

**Files:**
- Modify: `app/routers/openai.py:200-280`
- Modify: `app/routers/responses.py:200-280`
- Modify: `app/core/account_pool.py:350-420`
- Test: `tests/unit/test_stream_disconnect_slot_release.py`

**Interfaces:**
- Consumes: `account_pool.acquire()` and `account_pool.release()`
- Produces: Guarantee that client disconnection always releases pool concurrency slot.

- [ ] **Step 1: Write failing test simulating client mid-stream abort**

```python
# tests/unit/test_stream_disconnect_slot_release.py
import pytest
from app.core.account_pool import account_pool, GeminiAccount, AccountStatus

@pytest.mark.asyncio
async def test_stream_generator_aborted_releases_active_requests():
    acc = GeminiAccount(id="test-acc-disconnect", psid="p", psidts="t")
    acc.active_requests = 1
    # Verify that release immediately resets active_requests to 0
    acc.release(success=True)
    assert acc.active_requests == 0
```

- [ ] **Step 2: Run test to verify**

Run: `C:\Python314\python.exe -m pytest tests/unit/test_stream_disconnect_slot_release.py -v`

- [ ] **Step 3: Implement robust `try ... finally` release in SSE generators**

In `_sse_stream_chunks` and `/v1/responses` streaming generator, ensure `account_pool.release` is guaranteed in an outer `finally` block even if Starlette raises `ClientDisconnect`.

- [ ] **Step 4: Verify full test suite passes**

Run: `C:\Python314\python.exe -m pytest tests/unit/test_stream_disconnect_slot_release.py tests/unit/test_openai_stream_keepalive.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/routers/openai.py app/routers/responses.py tests/unit/test_stream_disconnect_slot_release.py
git commit -m "fix(stream): guarantee account slot release on client mid-stream disconnect"
```

---

### Task 4: UI/UX & Responsive Polishing Across 4 Themes

**Files:**
- Modify: `static/css/base.css`
- Modify: `static/components/section-dashboard.css`
- Modify: `static/components/section-settings.css`
- Modify: `static/components/section-gems.css`
- Test: `tests/unit/test_dashboard_accessibility.py`

**Interfaces:**
- Consumes: Theme variables (`--bg-primary`, `--bg-secondary`, `--text-primary`, `--border-color`, `--primary-color`)
- Produces: Polished, glitch-free UI in `vercel-dark`, `vercel-light`, `supabase-dark`, `supabase-light`.

- [ ] **Step 1: Check CSS variables and accessibility contrast in light themes**

Inspect contrast of `.quick-endpoints-container pre`, `.config-panel .slo-table`, and `.gem-card` when `[data-theme="vercel-light"]` or `[data-theme="supabase-light"]` is active.

- [ ] **Step 2: Add CSS rules for crisp borders and readable text in light themes**

Ensure code snippets have dark background with clear light text in both dark and light modes, avoiding low-contrast grey-on-white text.

- [ ] **Step 3: Test responsive navigation on mobile viewport**

Ensure `.models-two-col-layout` collapses cleanly on `< 992px`, and tables (`.slo-table`, `#logs-table`) have scroll wrappers preventing viewport blowouts.

- [ ] **Step 4: Run accessibility tests**

Run: `C:\Python314\python.exe -m pytest tests/unit/test_dashboard_accessibility.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add static/css/base.css static/components/section-dashboard.css static/components/section-settings.css
git commit -m "style(ui): polish theme contrasts, responsive tables, and code snippet wraps"
```

---

### Task 5: Gem Manager Edge Cases & Mapping Verification

**Files:**
- Modify: `static/app/gems.js`
- Modify: `app/routers/gems.py`
- Test: `tests/unit/test_gem_edge_cases.py`

**Interfaces:**
- Consumes: `/admin/gems` and `/admin/gem-mapping`
- Produces: Bulletproof Gem creation, slug generation, and model exposure.

- [ ] **Step 1: Write test for Gem slug deduplication and invalid characters**

```python
# tests/unit/test_gem_edge_cases.py
import pytest
from app.routers.gems import GemMappingRequest

def test_gem_mapping_validation_requires_all_fields():
    with pytest.raises(Exception):
        GemMappingRequest(model_name="", gem_id="g1", account_id="acc1")
```

- [ ] **Step 2: Run test to verify**

Run: `C:\Python314\python.exe -m pytest tests/unit/test_gem_edge_cases.py -v`

- [ ] **Step 3: Harden `static/app/gems.js` slugify and mapping conflict resolution**

Ensure `slugify` strips accents, punctuation, and restricts model names to valid OpenAI model identifier regex `^[a-zA-Z0-9._-]+$`.

- [ ] **Step 4: Verify test passes**

Run: `C:\Python314\python.exe -m pytest tests/unit/test_gem_edge_cases.py tests/unit/test_gems.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add static/app/gems.js tests/unit/test_gem_edge_cases.py
git commit -m "fix(gems): validate slugified model names and harden mapping collision logic"
```

---

### Task 6: Comprehensive Verification, Documentation & Release Prep

**Files:**
- Modify: `CHANGELOG.md`
- Verify: `app/`, `tests/`, `evals/`, `docs/`

- [ ] **Step 1: Run full static analysis**

Run: `C:\Python314\python.exe -m ruff check --no-cache app tests docs`
Expected: `All checks passed!`

- [ ] **Step 2: Run full unit test suite**

Run: `C:\Python314\python.exe -m pytest tests -q`
Expected: >= 1208 passed, 0 failed.

- [ ] **Step 3: Run full evals test suite**

Run: `C:\Python314\python.exe -m pytest evals -q -p no:cacheprovider`
Expected: 240 passed, 0 failed.

- [ ] **Step 4: Update `CHANGELOG.md` under `[Unreleased]`**

Document all security, UI/UX, and test improvements clearly in English.

- [ ] **Step 5: Commit & Push**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): document audit hardening, UI polish, and test suite expansion"
git push origin main
```
