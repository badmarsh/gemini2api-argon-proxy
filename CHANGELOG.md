# Changelog

All notable changes to this project will be documented in this file. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

## [1.7.2] - 2026-10-20 (badmarsh/gemini2api v1.7.2)

### Added
- **Comprehensive console and disconnect regression gates.** Added full key-set parity coverage for all five
  i18n locales, behavioral XSS tests for the Gems, Logs, and Babysit renderers, account quota-card state tests,
  four-protocol mid-stream disconnect tests, polling-lifecycle guards, and light-theme contrast/layout checks.
- **Request tagging via `X-G2A-Run` and `X-G2A-Role` headers.** When present, these headers are validated
  against `^[A-Za-z0-9_.:-]+$` (max 64 chars) and recorded in `LogRecord.tags` and the debug metrics store
  (up to 2 tags, e.g. run ID and caller role). Tags are searchable via `/admin/logs` and visible in
  `/admin/debug-metrics`, enabling attribution between orchestrator, monitor, and agent requests without
  per-agent API keys. Requests without these headers are unaffected.

### Fixed
- **Client disconnects no longer strand account concurrency slots.** Claude SSE and native Gemini streaming now
  explicitly close their keepalive wrappers in outer `finally` blocks, matching the existing OpenAI and Responses
  lifecycle. Abrupt closure now propagates to the account pool on all four protocols, where the neutral disconnect
  release returns `active_requests` to zero without penalizing account health.
- **SPA polling intervals now follow section lifecycle.** Logs, Babysit, dashboard refresh, and dashboard uptime
  timers are stopped with `clearInterval` when their section is hidden and restarted idempotently when activated;
  stale aborted log requests can no longer clear a newer poll's `AbortController`.
- **Admin console two-column and high-volume views polished.** Quick-integration tabs now have equal geometry,
  accessible tab semantics, bounded code panes, responsive single-column behavior, and success/failure copy
  feedback. Quota badges wrap cleanly on narrow account cards, while the 100-row Logs view has a bounded scrolling
  region, sticky aligned headers, stable column widths, and clearer pagination.
- **Four-theme contrast hardened.** Vercel Light and Supabase Light now use explicit WCAG-AA status text, code
  colors, and strong table/code borders instead of inheriting washed-out theme values.
- **Stream protocol drift detection (`saw_frame`).** When Google's stream sends bytes but zero WRB frames
  can be parsed, the stream now logs a warning (without raw body contents), raises `ProtocolDriftError`,
  and classifies the request in diagnostics and metrics as `protocol_drift` instead of silently finishing
  as an empty successful completion. Audit item L.
- **Image generation endpoint now uses the resolved model.** `POST /v1/images/generations` resolved
  `model = _resolve_model(...)` but passed the raw, unresolved `req.model` string to
  `gemini_client.generate()`, bypassing model alias and family mapping. It now passes the resolved model.
  Audit item M.
- **`/v1/responses` no longer returns Google's soft-error phrase as a successful reply.** The router computed
  `is_err_text` (stream and non-stream) and never read it, so with `ARGON_FALLBACK_TO_FLASH=false`, a non-Argon
  model, or an exhausted fallback, "Sorry, something went wrong..." came back as HTTP 200 / `response.completed`
  and a client had no signal to retry. It now answers like chat/completions: a classified error status (with
  `Retry-After` when known) non-stream, `response.failed` on the stream. Audit item K.
- **`evals/state.json` is no longer tracked.** `.gitignore` already declared it local tuning state but the file
  was committed, so the rule was inert and every `evals/tune.py --resume` run dirtied the diff. Decision: keep the
  ignore rule and stop tracking the mutable resume state (`git rm --cached`); the dated `evals/reports/*` files
  stay tracked as the reproducible evidence. Audit item J.
- **Request diagnostics no longer report every non-Argon request as `served_by="argon"`.** `_debug_fallback`
  defaulted to `argon` whenever the Argon-only served-by header was absent, so a plain `gemini-flash` chat was
  recorded as served by Argon - which defeated the requested-vs-served comparison that exposes a silent Flash
  substitution for Argon. A non-Argon request with no marker now reports the model it asked for; Argon requests
  and every explicit marker (header, stream marker, stall breaker) behave as before.
- **The refresher pushes only cookies that changed, and logs why a cookie source was skipped.** A result is
  marked `unchanged` when it equals the pair the pool already holds; such an account is not pushed while
  `/admin/accounts` reports it active and healthy (any doubt still pushes). Every skipped source
  (`browser state`, `pool`, `refresher_accounts.json`) now logs its reason - no usable pair, or the same pair as
  an earlier source (fingerprints only, never values). A silent de-duplication hid the cause of a 61 minute
  0/4 outage. The refresher image must be rebuilt to pick this up.
- **A cookie reload no longer kills non-stream chats that are already in flight.** The stream path leased
  the shared HTTP session, but the non-stream `_send_request` posted through `self._http` directly, so
  `reload_cookies()` (and a TLS fingerprint rotation) closed the session under the request. Observed live: every
  chat in flight on the account being reloaded answered 502 within milliseconds of that account's PUT finishing.
  The POST now holds a lease; the old session is closed by its last user.
- **A cookie push no longer destabilises live chats.** `PUT /admin/accounts/{id}/cookies` now (1) answers
  `{"status":"ok","changed":false}` without reloading when the client is healthy and already uses exactly the
  pushed pair (the refresher re-pushed all four accounts every cycle even when nothing rotated), and (2) runs a
  real reload under the client's `_heal_lock`, so a request arriving while the client is marked unhealthy waits
  for the reload instead of starting a second concurrent one. Observed live: a chat returned 502 a few
  milliseconds after each push finished. Adds `GeminiWebClient.current_cookie_pair()`.
- **Gemini wire-frame decoders now use `json.loads(..., strict=False)`.** Literal newlines/tabs in a valid upstream string no longer get dropped by the internal frame, model-list and response decoders; this is hardening only and does not repair malformed tool calls.
- **Tool-loop regenerations share one per-turn budget.** The promise/stall guard and malformed-JSON retry now consume the same single token, so one logical turn cannot stack both expensive Argon regenerations. Complete tool-call argument strings with an unescaped quote are repaired only after a strict JSON parse succeeds; missing closing structure is never synthesized. Parser diagnostics include the decoder reason, response length and truncation suspicion.
- **Multi-turn context is no longer lost on stored conversations.** The 81-element StreamGenerate payload
  cannot continue a conversation the server already holds (`inner[2]` is the metadata block, spec §4.3), yet
  the router stripped the request down to the newest user message whenever a stored `gemini_conv_id` existed:
  the client's `conversation_id` was "accepted" and the model answered without any history. The full history
  is now sent by default; `SERVER_SIDE_CONTINUATION=true` restores last-message-only once the protocol can
  really carry the id again. The dropped id is reported once in the log (never printed).
- **Companion UUID pairing restored.** `_encode_payload_thinking` mints the per-request UUID that the payload
  carries at `inner[59]`; the `x-goog-ext-525005358-jspb` header must echo that same UUID. The Argon rewrite
  discarded the returned value and minted a second, unrelated UUID for the header (upstream paired them).
  Both call sites use the encoder's UUID again, with a regression test per call site.
- An answer with no text, no images and no reasoning is no longer returned as a silent HTTP 200 with empty
  content once every fallback is exhausted (for example with `ARGON_FALLBACK_TO_FLASH=false`):
  `/v1/chat/completions` (stream and non-stream) and `/v1/responses` answer `502` (or an error event /
  `response.failed`) with a retryable message, so clients can retry instead of treating an empty reply as success.
- Native `streamGenerateContent` with tools now emits `functionCall` parts, like `generateContent`, instead of
  streaming the raw `{"status":"tool_use",...}` envelope as text.
- Order of operations for `ARGON_SYSTEM_PROMPT` / tool-loop work: see the entries below (merge mode,
  `TOOL_LOOP_REMINDER`, stall guard, single Argon->Flash implementation, visible substitutions).
- `.env` robustness on Windows: a UTF-8 BOM at the start of the file (added by Notepad or PowerShell 5.1) no longer breaks the first key, and unknown keys (for example those used by the refresher, which shares the file) no longer crash startup. Startup logs one warning listing the unknown key names (never values) so typos are still noticed.
- The Gemini-to-third-party fallback toggle (`PATCH /admin/api-keys/fallback`) is now persisted in `data/settings-overrides.json`, like the settings panel fields. Under Docker Compose `env_file` the real environment variable beat the container-local `.env`, so the toggle silently reverted after a restart. The write is all-or-nothing: if any layer fails, memory and files are restored.
- Text encoding is now explicit everywhere (`utf-8-sig` when reading hand-edited files, `utf-8` when writing), so the app no longer depends on the Windows locale code page. Persistence is hardened against bad files: conversation history is written atomically and loaded file by file, and a corrupt `accounts.json` is moved to `accounts.json.corrupt-<YYYYmmdd-HHMMSS>` before the pool starts empty instead of being silently overwritten.
- Usage statistics snapshots kept failing on Windows because `Path.rename` raises `FileExistsError` when the target exists; they now use `replace`, so persistence works there.
- An auto-generated API key is now persisted when `.env` has no exact `API_KEY=` line (for example only `ADMIN_API_KEY=`, an `export` prefix, or an empty value). The file is parsed line by line and "saved" is only logged after the write succeeded.
- `POST /admin/restart` flushes the log store before signalling and now answers `501` on Windows, where `os.kill(SIGTERM)` is a hard `TerminateProcess` (no graceful shutdown and nothing starts the service again). The web console's Restart button now passes the required `?confirm=true`; before, it always got `400` and silently did nothing.
- `.env` rewrites (settings panel, fallback toggle, generated API key) are atomic: a temp file next to `.env` is swapped in with `os.replace`, so a crash can no longer leave a truncated file. A read-only `.env` still fails up front with a permission error, the file mode is kept, and a bind-mounted `.env` that cannot be renamed over falls back to a direct write. A newly created `.env` is now private (0600). The parent directory is fsynced after the swap on POSIX (best effort).
- The web console's Restart button no longer claims "Restarting service..." when the request failed: it shows the real error and does not start the reload/health polling. `GET /admin/system-info` reports `restart_supported`, and the button is hidden where restart is unsupported (Windows).
- **`[Tool call: NAME(ARGS)]` text from the model no longer ends the agent turn.** Models sometimes imitate the
  prompt-history rendering of past calls instead of the JSON envelope; the raw string was returned as a final answer
  (`finish_reason=stop`), so e.g. Codex Desktop stopped mid-task. A reply made up solely of complete
  `[Tool call: NAME(JSON)]` blocks is now converted to normal tool calls (arguments decoded with `raw_decode`, so
  `)`/`]` inside strings are safe). A reply that opens this way but is truncated, has trailing text or non-object
  arguments is never forwarded: it takes the malformed path and the existing single regeneration runs with a
  format-correction nudge. Truncated calls are never repaired, and prose that merely mentions the pattern is left
  untouched.

---

### Security
- **Dynamic admin-console markup is now context-safe.** API-derived Gems, structured-log, and Babysit values are
  escaped for text or attribute context before `innerHTML` insertion; log direction and finding-severity CSS
  classes are allowlisted. Behavioral hostile-markup tests cover the three renderers, while log detail data keeps
  credential headers and cookie/API-key fields redacted.
- `/docs`, `/redoc` and `/openapi.json` are now disabled by default (404). FastAPI serves them without checking the API key, and they listed every route including the admin API. Set `DOCS_ENABLED=true` to bring them back; they stay unauthenticated, so use it for local development only.
- The uvicorn access log no longer prints `?token=` values or `sk-...` keys from the request path; a logging filter scrubs them before the line is formatted.
- `POST /admin/api-keys/models` now sends a Gemini key in the `x-goog-api-key` header instead of the `?key=` query string, and no longer echoes upstream exception text (which embedded the request URL) back to the client. Errors report only the HTTP status or the exception type.
- Log scrubbing now masks the whole key token: `sk-ant-...` and `sk-proj-...` keys (letters, digits, `-`, `_`), URL-encoded `sk%2D...` keys, and `?key=` / `?api_key=` / `?apikey=` / `?api-key=` query values. Before, only the first segment of a dashed key was hidden and the tail leaked. The same scrubber backs the request log.
- `PATCH /admin/api-keys/fallback` no longer returns the raw exception text (which could contain local file paths) when persisting fails; the details stay in the server log.
- **Response hardening headers**: every response now carries a `Content-Security-Policy` (default-src/connect-src
  restricted to the two CDNs the console actually uses), `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy`, `Permissions-Policy` and `Cross-Origin-Opener-Policy`. External links in the console carry
  `rel="noopener noreferrer"`.
- **CSRF surface documented and pinned**: the web console authenticates with a bearer token in the `Authorization`
  header and never uses cookies, so no ambient credential exists that a cross-site request could ride on. The
  invariant is pinned by a regression test; no cookie-based session was introduced.
- `POST /admin/accounts/{id}/status` (enable/disable an account) is protected by the admin key, never echoes the
  account id back to the visible UI and never modifies the stored credentials.

### Changed
- Argon timeouts and stalls no longer escape as a bare `500`: `curl_cffi` timeouts (an `OSError`) are classified as `504` (timeout) or `502` (other transport errors) and are caught by the OpenAI, Anthropic and native Gemini routers, so the Flash fallback actually runs.
- An Argon stream that ends without any answer now falls back to Flash on `/v1/chat/completions` and `/v1/responses` (when `ARGON_FALLBACK_TO_FLASH` is on) instead of returning an empty completion.
- `/v1/messages` and the native Gemini endpoints now also fall back from Argon to Flash (same rules as the OpenAI endpoints, before any content was streamed).
- An Argon timeout with the Flash fallback on now goes straight to Flash instead of retrying the other Argon account (which doubled the wait to ~2x360 s). `ARGON_FAILOVER_ON_STALL=true` restores the old behaviour.
- Stall circuit breaker: after an Argon stall, Argon requests are served by Flash for `ARGON_STALL_BREAKER_SECONDS` (default 180) instead of each waiting out the full timeout again. Set it to `0` to disable. Has no effect when `ARGON_FALLBACK_TO_FLASH` is off.
- `ARGON_SYSTEM_PROMPT` now **merges** in front of the caller's own system/developer prompt by default.
  Codex, Cline and OpenCode always send theirs, and the old "only when the caller sent nothing" rule silently
  disabled the Argon rules for exactly those clients (`ARGON_SYSTEM_PROMPT_MODE=only_when_none` restores it).
- One implementation of the Argon -> Flash rules (`app/core/argon_fallback.py`) replaces the four divergent
  copies in `openai.py`, `claude.py`, `gemini.py` and `responses.py`; a failed fallback re-raises the original
  error instead of returning an empty 200 completion.
- First-chunk wait for Argon streams is 150 s (nothing can be emitted before it); the 240 s silence window
  **after** content started is deliberately unchanged (rt=c: a shorter window truncates replies).
- `ARGON_TIMEOUT` makes the Argon request timeout configurable (0 = built-in default).

### Added
- **Agent-loop guard for coding harnesses** (`app/core/agent_loop.py`): an end-of-prompt
  `TOOL_LOOP_REMINDER` ("emit the tool_call now, never a promise") plus detection of a conversational stall
  and exactly one corrective retry (`TOOL_LOOP_STALL_GUARD`). A promise-only reply is what ends a harness'
  turn and leaves the agent idle after 1-2 tool calls; the retry keeps the first reply when it fails.
- **Visible Argon -> Flash substitutions**: `X-Gemini2API-Argon-Served-By` / `-Fallback-Reason` headers on
  non-streaming responses, a `: gemini2api argon_fallback served_by=...` comment frame in SSE streams (an
  NDJSON line for the native Gemini stream), and `/admin/status` now reports the breaker state, the guard
  counters and the number of empty-but-successful Argon streams.
- **Bounded redacted request diagnostics**: `/admin/status`, `/admin/debug-metrics`, `/admin/logs` and usage
  summaries expose TTFT, total latency, inter-chunk gaps, serving model, fallback reason, error class and
  regeneration count without storing cookies, prompts or account identifiers. Account attribution is a short
  one-way hash only, and the in-memory event ring is bounded.
- **Console diagnostics and tool fixture testing**: the dashboard renders the redacted metrics histogram and
  breakdowns, logs show TTFT/serving model, and the Playground can validate a fixture through the same safe
  parser without a regeneration or upstream request. All five locale blocks are kept in sync.
- **Local tuning harness**: `python -m evals.tune` runs deterministic T0, resumable capped T1/T2 HTTP tiers,
  redaction, one-process locking, reports and an exact-match-only proposal contract for the
  `gemini-3.8-flash` worker. It never reads runtime `data/`, `.env` or cookie files.
- `POST /admin/argon/reset` closes the stall breaker by hand instead of waiting out
  `ARGON_STALL_BREAKER_SECONDS`.
- Panel: new "Argon & agent loop" group (`ARGON_SYSTEM_PROMPT_MODE`, `ARGON_TIMEOUT`, `TOOL_LOOP_REMINDER`,
  `TOOL_LOOP_STALL_GUARD`) with labels and hints in all five languages.
- Dashboard v2: the dashboard now loads `/admin/status` and `/admin/system-info` in parallel (`Promise.allSettled`). If only one of them fails, the other card group still renders and a small notice with a Retry button appears for the failed part; the full-section error screen is kept for the case where both fail. The dashboard also refreshes itself every 30 seconds, but only while it is the active section and the browser tab is visible (a background refresh never replaces the page with an error screen and skips the static QR cards). New i18n keys `dashboard.error.status`, `dashboard.error.system`, `dashboard.retry` in all five languages.
- **Prometheus-compatible metrics endpoint** (`GET /admin/metrics`, admin key): request count, latency buckets, TTFT,
  fallback, Argon breaker, account-pool, parser and regeneration counters in the standard text exposition format.
  Label values are bounded (at most 12 distinct values per label, everything else collapses into `other`); there are
  no request-id, account-hash, prompt or client-model labels. The existing redacted JSON diagnostics stay the source
  for the web console.
- **Operational overview (SLO panel)**: 1h/24h/7d request volume and error rate, TTFT p50/p95, the maximum
  inter-chunk gap, the Argon breaker state, the fallback rate, regeneration-budget exhaustion and account-pool
  saturation/slot-leak indicators. The state is explicitly `ok`, `degraded` or `unknown` (with the reasons listed),
  so a missing signal is never rendered as "healthy".
- **Request explorer** (`GET /admin/debug-metrics/{request_id}`): a correlated timeline per request with ingress and
  egress timestamps, TTFT, total duration, inter-chunk gaps, requested vs. actual model, fallback reason, parser
  failure class, regeneration count, stream markers and finish reason. Request and response bodies are never included
  and the timeline contains no `Authorization` header, cookies, prompts or account IDs.
- **Configuration readiness view** (`GET /admin/config`): the effective runtime configuration with the source of
  every value (environment variable, persisted panel override or default), which settings only take effect after a
  restart, validation failures and override drift. Secrets are reported as "configured" only, never as values.
- **Account-pool operations view**: per account slots in use vs. capacity, remaining cooldown, failure/empty streaks,
  session health and last success, plus confirmation-protected actions (check, enable/disable) and a
  confirmation-protected Argon breaker reset. `POST /admin/accounts/{id}/status` disables an account at runtime
  (the rotation skips it, the credentials and counters are kept) and is not persisted to `accounts.json`.
- **Accessible dashboard UX**: keyboard navigation with visible focus, screen-reader labels for the charts and for
  status changes, empty/loading/error states, `prefers-reduced-motion` support, a pause/resume button for the
  auto-refresh (the state survives a reload) and a persisted request-explorer filter. All new UI strings exist in
  all five languages (`zh-CN`, `en-US`, `ja-JP`, `ko-KR`, `zh-TW`).
- **CI gates**: the offline eval suite (`pytest evals -q -p no:cacheprovider`, not collected by the default
  pytest run) is now its own job, and a Python 3.14 job runs ruff + pytest. The 3.14 job installs
  `pydantic>=2.12` because the pinned `pydantic==2.11.3` pulls a `pydantic-core` without a cp314 wheel;
  `requirements.txt` itself is unchanged.

## [1.7.1] - 2026-10-14 (badmarsh/gemini2api v1.7.1)

> Security patch for the static-extension authentication bypass, failover and stream reliability fixes, and secret-handling and console hardening.

### Security
- **Static-extension authentication bypass (present in v1.7.0)**: the public-path check matched any path ending in a static extension regardless of method or prefix, so requests such as `DELETE /admin/accounts/test.png` skipped `verify_api_key` and `verify_admin_key`. The exemption now applies only to `GET`/`HEAD` and never under `/admin`, `/v1`, `/v1beta`, `/openai`, `/claude` or `/gemini`; the `/admin` prefix check is segment-based and `verify_admin_key` no longer exempts any path. Regression tests added.
- Removed `add_cookies.py` (sent no auth header) and `add_account_cookies.py` (printed full cookies); both relied on an undeclared `requests` dependency.
- Admin account status now masks `psidts` in addition to `psid`, and API key listings use the shared `mask_secret` helper.

### Fixed
- **Session lifecycle**: the shared HTTP session is leased per in-flight request; a session retired by a cookie reload or fingerprint rotation is closed by its last user instead of underneath running streams.
- **Timeout failover**: a hung request (builtin/asyncio timeout or libcurl code 28) fails over to the next account immediately instead of consuming further same-account retries. Retry exhaustion raises a dedicated `RetriesExhaustedError`.
- **Stall classification** matches exception types and codes only; free-text matching on error messages could misclassify ordinary 4xx responses.
- **Google error-template detection** is limited to a known sentence plus at most a short single-line tail, so genuine answers that begin with the same words are no longer discarded.
- **Cookie refresher**: reports an expired session when redirected to the Google login page, isolates per-account failures, and no longer passes `--single-process` / `--no-zygote` to the browser.
- `ROTATION_STRATEGY` accepts `round_robin`, `Round-Robin` and padded values, so the README value no longer raises at startup.
- `/admin/check-update` queries `badmarsh/gemini2api` instead of upstream.

### Added
- `WARNING` log with emitted and unparsed buffer sizes when the 120s chunk timeout ends a reply early (behavior unchanged).
- Web console: domain services under `static/app/services/` (account, chat, telemetry, storage, validation), centralized constants, per-section error boundaries with retry, `AbortController` timeouts, and 200-item ring buffers for logs and playground history.
- `safeStorage` wrapper with in-memory fallback and quota protection. The stored console token is obfuscated (XOR mask plus Base64), which is not encryption.
- Unit and integration tests for masking, account pool state transitions, model alias translation, thinking-block extraction, auth bypass, failover classification and in-flight stream reloads.

### Changed
- `mask_secret` now shows the first 6 and last 4 characters (previously 4 and 4); values of 8 characters or fewer are fully masked and values of 9-10 characters show 2 and 2.
- Admin endpoint messages (`/admin/restart`, web-chat cleanup) are in English.
- Bump `fastapi` 0.120.4 -> 0.143.0 and `starlette` 0.49.1 -> 1.8.0 (pinned together; the Starlette >= 0.49.1 floor for CVE-2025-62727 is preserved). Adds `opentelemetry-api` as a transitive dependency of FastAPI.
- Tests are isolated from the real `data/` directory via `DATA_DIR`; the saturated-pool test tolerates Windows timer granularity.
- CI: the `pip-audit` step is report-only and no longer produces red annotations.
- Docs: CHANGELOG, UI templates, stylesheets and scripts localized to English; clone instructions point to the `badmarsh/gemini2api` fork.

---

## [1.7.0] - 2026-10-14 (badmarsh/gemini2api v1.7)

> Gemini 4 Argon Protocol Support, Autonomous Agent Hardening, Robust Simulated Tool Calling, and Default English Web Console.

### Fixed
- 🧰 **Simulated Tool Call Envelope Leak & Non-Strict JSON Parsing**:
  - Added `strict=False` in `json.loads` within `app/utils/tools.py` (`_try_parse` and `_normalize_tool_calls`), tolerating unescaped control characters (ASCII newlines `\n` and tabs `\t`) generated by models in string values.
  - Implemented `_unwrap_text_status_envelope` regex fallback to ensure model replies wrapped in `{"status": "text", "content": "..."}` envelopes never leak raw JSON syntax to Codex Desktop or OpenCode clients even if trailing syntax is broken.
  - Added action-forcing instruction in default tool-calling system prompts to deter models from hedging with plain text when actions or tool invocations are requested.
- 🌐 **Web Console & Documentation Localization (Zero Chinese Defaults)**:
  - Switched default UI language in `static/app/i18n.js` from `zh-CN` to `en-US`.
  - Fully translated `static/login.html`, `static/index.html`, `static/components/` (header, sidebar, dashboard, accounts, api-keys, config, gems, logs, playground, settings, usage-stats), and `SPONSORS.md` to English.
  - Routed dashboard QR cards and image assets locally through `/api-assets` to avoid pulling upstream Chinese cards from raw GitHub.

---

## [1.6.41-argon] - 2026-10-14 (badmarsh/gemini2api fork)

> Dedicated hardened release for **Gemini 4 Argon** mandatory thinking protocols, deep reasoning timeouts, Google Web error template interception, and multi-turn coding agent context retention.

### Added
- 🧠 **Gemini 4 Argon Protocol Adaptation (StreamGenerate 81-element payload)**: Supports the October 2026 Google Gemini Web UI protocol shift. Implements the 81-element thinking payload structure, `f.sid`/`bl` tracking parameters, and 15-element companion headers. Enforces active internal thinking (`inner[80]=2`) on Argon models, eliminating the silent black-hole stall from legacy 4-element payloads.
- 🛡️ **Google Web Error Template Interception & Seamless Failover**: When the Gemini Web interface returns boilerplate errors (such as `"I seem to be encountering an error. Can I try something else for you?"` or `"Sorry, something went wrong"`), they are intercepted as `GoogleWebError`, putting the account into cooldown and automatically failing over to the next healthy account.
- ⏱️ **Stall Detection & Self-Healing (`_is_stall`)**: Introduced connection hang and first-byte timeout detection in `account_pool.py`, quickly releasing and cooling down stalled accounts to prevent blocking subsequent requests.
- 🍪 **Cookie Jar Whitelisting (Eliminating 168-byte Stream Stalls)**: `cookie_jar.py` strictly whitelists `__Secure-1PSID` and `__Secure-1PSIDTS`, filtering out volatile anti-abuse cookies (`NID`, `SIDCC`, `AEC`, `OTZ`) and completely preventing cross-process volatile cookie replay stalls.
- 🚦 **Staggered Startup Rate-Limit Protection**: Adds a 4-second delay between account initializations during container boot, preventing simultaneous `/app` and `batchexecute` bursts from triggering Google's `identity.hfcr` rate limit on shared egress IPs.
- 🛠️ **OpenAI API `developer` Role Mapping**: Messages using the `developer` role (Codex, OpenCode, o1/o3-mini standards) are mapped to `System: <content>`.

### Changed
- ⌛ **180s Extended Reasoning Timeout**: `_GENERATE_TIMEOUT_DEFAULT` and the underlying `AsyncSession` timeout increased from 60s to 180s, accommodating Gemini 4 Argon deep reasoning passes (typically 30–75s) without triggering client or gateway disconnections.
- 💬 **Full Coding Agent Multi-Turn Context Retention**: When external coding agents (Codex, OpenCode, Antigravity, OpenAI SDK) provide full conversation history, the context is preserved in full and stale `gemini_conv_id` references are cleared, preventing cross-account session dropouts.
- 🔑 **Admin Cookie Reset Auto-Activation**: Updating credentials via `/admin/accounts/{id}/cookies` automatically resets account status to `ACTIVE`, clears failure counters, and persists changes immediately.
- 📦 **Dependency Upgrade**: Upgraded `curl_cffi` to `0.16.3`.

---

## [1.6.40] - 2026-09-12

### Fixed
- 🔍 **Playground Error Visibility**: Stream error frames now properly display `Gateway Error (503): <reason>` instead of masking failures as "No response content". Error messages are sanitized to prevent stored XSS.
- 🪵 **Cookie Rotation Lifecycle Logging**: Explicit startup logs show whether the auto-refresh loop has started (`Auto-refresh loop started / NOT started (reason)`).

### Added
- 🩺 **Accurate Account Health Diagnostics**: Account cards export `is_healthy`, `consecutive_failures`, `last_success_at`, and `last_error` (sanitized exception type and HTTP code), ensuring account health is clear even when marked active.
- 📉 **Consecutive Empty Response Tracking**: Consecutive empty responses are tracked separately and trigger error increments only after 3 consecutive occurrences, preventing false-positive account degradation.

---

## [1.6.39] - 2026-09-01

### Changed
- ⚠️ **Settings Override Precedence**: Runtime settings modified via the Web Console are stored in `data/settings-overrides.json` and take precedence over environment variables across restarts.

### Added
- ⚙️ **`LOG_BODIES_ENABLED` Console Toggle**: Added request/response body logging switch directly to the settings dashboard.

### Fixed
- 🧊 **Static Assets Cache Validation**: Added `Cache-Control: no-cache` with ETags to `.js`, `.css`, `.html`, and `.json` static assets to prevent stale script caching.
- 🔢 **Decimal Float Preservations**: Fixed float truncation in cleanup intervals and duration inputs.
- 🛡️ **Invalid Float Rejection**: Rejects `NaN` and `±Infinity` from entering configuration fields.
- 💾 **All-or-Nothing Settings Persistence**: Transactional rollbacks across in-memory state, overrides file, and `.env`.

---

## [1.6.38] - 2026-09-01

### Fixed
- 🍪 **Multi-Domain Cookie Conflict**: Resolved `CookieConflict` exceptions when Google redirects across country domains (`.google.com.hk`, `.com.tw`, `.co.jp`) by reading individual cookie objects and prioritizing `*.google.com`.
- 🔁 **Automatic Retry for Malformed Tool Call JSON**: Automatically retries generation once with live SSE keepalives if simulated tool calling outputs malformed JSON.
- 🛡️ Non-blocking response logging: Logging failures no longer interrupt client responses.

### Added
- 🔍 **Optional Full Request/Response Body Logging (`LOG_BODIES_ENABLED`)**: Kept in-memory only (max 4KB, credentials scrubbed) for debugging tool interactions.

---

## [1.6.37] - 2026-08-30

### Fixed
- 🩺 **Account Pool Lockup Resolution**: Corrected pool selection logic where unhealthy accounts with expired sessions previously caused false `529 All accounts busy` errors. Added non-blocking self-healing reloads.
- 🛑 **Client Disconnect Accounting**: Early client disconnects no longer increment account failure counters or mark accounts as `EXPIRED`.
- 🔄 **Safe Cookie Reload Rollbacks**: Failed cookie reloads roll back cleanly without leaving orphan states.

---

## [1.6.36] - 2026-08-28

### Fixed
- 🚨 **OpenAI Streaming Error Frames**: Emits standard `data: {"error": {...}}` frames instead of disguised successful stops on upstream failures.
- 🚦 Standardized 4xx HTTP error mapping to `invalid_request_error`.
- 📐 Synchronized citation fields between streaming and non-streaming Anthropic endpoints.
- 🛡️ Tolerates non-string message contents without raising `TypeError`.

---

## [1.6.35] - 2026-08-28

### Fixed
- 🛠️ **Image Intent Detection Scope**: Scoped image intent detection strictly to the final user message with boundary matching and idiom exclusions (`draw a conclusion`, `draw a line`), preventing unintended suppression of tools in agent turns.
- 🚨 Standardized `event: error` frame emission on Anthropic streaming errors.
- 🚦 Standardized error mapping via `classify_error` (including 529 `overloaded_error` with `Retry-After`).
- 🌊 Added keepalive heartbeats to `/v1/responses` and native Gemini buffered streaming paths.
- 🔌 Supported camelCase parameters (`systemInstruction`, `generationConfig`, `inlineData`) from official Gemini SDKs.
- 🧰 Retained OpenAI `tool_calls` during flattening and mapped Anthropic tool roles for third-party forwarders.
- 📐 Aligned Anthropic responses with official schemas (Markdown images, token usage fields).

---

## [1.6.34] - 2026-08-28

### Fixed
- 🧹 Anthropic protocol compliance: Added `stop_sequence` field support and gracefully handled null values in tool calling blocks.
- 🧪 Added unit test coverage for tool formatting and streaming cancellations.

---

## [1.6.33] - 2026-08-28

### Fixed
- 🔌 **Claude Code Compatibility**:
  - Supported array-of-blocks format with `cache_control` in `/v1/messages` system prompts.
  - Retained `tool_use` and `tool_result` content blocks in conversation turns.
  - Standardized Anthropic SSE events using `event:` and `data:` frames.
- 🌊 Added SSE keepalive heartbeats to Claude buffered streaming routes.

---

## [1.6.32] - 2026-08-14

### Fixed
- 🧠 **Incremental Streaming of Thoughts**: Streams reasoning thoughts frame-by-frame as `reasoning_content` before final text answers on `/v1/chat/completions`.

---

## [1.6.31] - 2026-08-14

### Fixed
- 🌊 **Streaming Keepalive Heartbeats**: Added periodic keepalive pings (`: ping` and NDJSON blank lines) across all streaming endpoints to prevent gateway timeouts during silent generation phases.
- 📝 Improved network error diagnostics in the Playground.

---

## [1.6.30] - 2026-08-14

### Added
- 🧠 **Playground Thinking Toggle**: Added toggle to enable extended reasoning in Playground sessions.

---

## [1.6.29] - 2026-08-14

### Added
- 🧠 **Extended Thinking (`reasoning_effort`)**: Supported reasoning effort parameters mapped to `reasoning_content` for Gemini models.

---

## [1.6.28] - 2026-07-30

### Added
- 🆕 **`gemini-flash-lite` Model Support**: Added dynamic routing for Gemini 3.5 Flash-Lite models.

---

## [1.6.27] - 2026-07-25

### Changed
- 🎨 **Branding Assets**: Optimized console brand logo and browser favicons.

---

## [1.6.26] - 2026-07-07

### Added
- 🔌 **OpenAI Responses API (`/v1/responses`)**: Supported OpenAI Responses protocol for compatibility with Codex CLI and next-generation agents.

---

## [1.6.25] - 2026-06-23

### Added
- 🎚️ **Third-Party Failover Switch**: Added interactive toggle in the API Keys console for enabling or disabling Gemini-to-third-party failovers.

---

## [1.6.24] - 2026-06-22

### Added
- 🧩 **Custom Gems as Models**: Manage, create, and expose Google Gemini Gems as OpenAI-compatible model names.

---

## [1.6.23] - 2026-06-22

### Added
- 🧠 **Per-Model Reasoning Effort**: Configurable reasoning levels for third-party models in the API Key manager.

---

## [1.6.22] - 2026-06-22

### Added
- 🔁 **Multi-Provider Failover for Identical Models**: Automatic failover across multiple configured providers sharing the same model identifier.

---

## [1.6.21] - 2026-06-21

### Added
- 🔀 **Gemini-to-Third-Party Failover Chain**: Automatically fallback to configured third-party models when upstream Gemini requests fail.

---

## [1.6.20] - 2026-06-19

### Fixed
- 🐳 **Non-Root Docker Permissions**: Re-engineered `docker-entrypoint.sh` to initialize with root, fix `/app/data` volume permissions, and drop privileges to `appuser` (UID 1000) using `gosu`.

---

## [1.6.19] - 2026-06-19

### Security
- 🔒 **Admin Console XSS Hardening**: Escaped all dynamic fields, converted inline event handlers to `data-*` listeners, and utilized `escapeAttr`.
- 🔒 **SSRF Protection**: Blocked private, loopback, and cloud metadata addresses in attachment downloads and third-party Base URLs.
- 🔒 **Input Validation**: Strict validation for runtime settings and path traversal protection for conversation files.

---

## [1.6.18] - 2026-06-19

### Fixed
- 🔧 Extended timeout for image generation requests to 180s and reduced SSE keepalive intervals.

---

## [1.6.17] - 2026-06-19

### Fixed
- 🔧 Streamlined image generation streaming with immediate SSE headers and periodic keepalives.

---

## [1.6.16] - 2026-06-19

### Fixed
- 🔧 Resolved parameter naming error in Deep Research endpoint.
- 🔧 Fixed connection lifecycle handling in third-party streaming forwarders.
- 🔒 Added optional `ADMIN_API_KEY` for dedicated admin authentication.
- 🔒 Masked credentials and API keys in logs and exports.

---

## [1.6.15] - 2026-06-06

### Added
- 🧹 **Automated Gemini Web Chat Cleanup**: Background loop cleans up old web conversations (skipping pinned chats) to prevent account clutter.

---

## [1.6.14] - 2026-06-02

### Fixed
- 🖼️ Enhanced image intent recognition for desire verbs (`want`, `need`).

---

## [1.6.13] - 2026-06-02

### Changed
- 🖼️ Formatted image generation responses with images placed at the top followed by descriptions.

---

## [1.6.12] - 2026-06-02

### Fixed
- 🛠️ Fixed tool call interference during image generation in coding agents.
- 🔧 Proper part formatting for native Gemini `/v1beta` function calls.

---

## [1.6.11] - 2026-06-02

### Added
- 🔁 **Smart 503 Failover**: Instant failover to alternative accounts in the pool upon receiving Google Web 503 rate limits.

---

## [1.6.10] - 2026-06-01

### Added
- ⚡ **True Incremental Streaming**: Incremental streaming directly from Google's `StreamGenerate` RPC for real-time typing feel.
- 🚀 Increased account concurrency and added request queuing.

---

## [1.6.9] - 2026-06-01

### Fixed
- 🖼️ Downloads generated images at full resolution (`=s0`).

---

## [1.6.8] - 2026-06-01

### Fixed
- 🖼️ Filtered raw placeholder URLs from generated image responses.

---

## [1.6.7] - 2026-06-01

### Fixed
- 🖼️ Rendered images directly inside the Playground chat console.

---

## [1.6.6] - 2026-05-31

### Added
- 🖼️ Local image hosting: Generated images served via `/images/{id}` with scheduled cleanup.

---

## [1.6.5] - 2026-05-31

### Added
- 🎨 Image generation support via `POST /v1/images/generations` and multimodal conversation endpoints.

---

## [1.6.4] - 2026-05-31

### Added
- Standard un-prefixed endpoints (`/v1/chat/completions`, `/v1/messages`, `/v1beta/models`).

---

## [1.6.3] - 2026-05-31

### Added
- Multimodal image and file uploads via base64 and remote URLs.
- Dynamic model discovery using Google Web RPCs.

---

## [1.6.2] - 2026-05-19

### Added
- Inactivity session timeout for the web console.

---

## [1.6.1] - 2026-05-18

### Added
- Multi-account failover load balancing strategy.
- Multi-language localization system with dynamic UI translation.
- Third-party model API Key management and aggregation into `/openai/v1/models`.

---

## [0.7.0] - 2026-05-16

### Added
- Structured in-memory LogStore ring buffer (2000 items) with pagination, search, and direction filtering.

---

## [0.6.0] - 2026-05-16

### Added
- Persistent usage statistics, time-series snapshots, and interactive SVG charts.

---

## [0.5.2] - 2026-05-16

### Added
- Batchexecute heartbeat RPC (`otAQ7b`) and enhanced cookie jar response header parsing.

---

## [0.5.1] - 2026-05-16

### Fixed
- Handled cross-domain cookie accumulation and clipboard fallback in non-HTTPS environments.

---

## [0.5.0] - 2026-05-15

### Added
- Anti-detection and browser fingerprinting system (Chrome TLS fingerprinting, dynamic headers, request timing jitter).

---

## [0.4.0] - 2026-05-15

### Changed
- Switched HTTP transport from httpx to `curl_cffi` for realistic TLS fingerprint emulation.

---

## [0.3.0] - 2026-05-15

### Added
- Multi-account load balancing and health check management.

---

## [0.2.0] - 2026-05-15

### Added
- Scheduled account health checks and hot cookie reloading.

---

## [0.1.0] - 2026-05-14

### Added
- Function calling across OpenAI, Claude, and Gemini formats.
- SSE streaming and initial admin console.

---

## [0.0.1] - 2026-05-13

### Added
- Initial release of the Gemini Web reverse proxy.
