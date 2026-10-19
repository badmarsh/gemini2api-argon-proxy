# Operations Runbook: Gemini 4 Argon Babysitting Experiment

This runbook guides the human operator (Marek) through starting, configuring, monitoring, stopping, and inspecting the Argon 4 babysitting experiment.

---

## 1. Pre-Flight Verification

Ensure `gemini2api` and `gemini2api-refresher` are running and the account pool is active:
```powershell
docker ps --filter "name=gemini2api"
```

Verify pool health (must report `active: 4` or at least active > 0):
```powershell
docker exec gemini2api python -c "
import urllib.request, json, os
key = os.environ.get('API_KEY')
req = urllib.request.Request('http://127.0.0.1:5918/admin/status', headers={'Authorization': f'Bearer {key}'})
with urllib.request.urlopen(req) as resp:
    data = json.loads(resp.read().decode())
print('Total:', data['total'], 'Active:', data['active'])
"
```

Verify experiment configuration (D1 fix must be active: fallback false, breaker 0):
```powershell
docker exec gemini2api python -c "
import urllib.request, json, os
key = os.environ.get('API_KEY')
req = urllib.request.Request('http://127.0.0.1:5918/admin/config', headers={'Authorization': f'Bearer {key}'})
with urllib.request.urlopen(req) as resp:
    data = json.loads(resp.read().decode())
fields = data.get('fields', {})
print('ARGON_FALLBACK_TO_FLASH:', fields.get('argon_fallback_to_flash', {}).get('value'))
print('ARGON_STALL_BREAKER_SECONDS:', fields.get('argon_stall_breaker_seconds', {}).get('value'))
"
```
Both values MUST report `false` and `0` respectively.

---

## 2. Launching the Collector

In a dedicated terminal, launch the timeline collector:
```powershell
python docs/babysit/collector.py --run-id "20261019-exp03"
```
The collector will:
- Poll `/admin/debug-metrics` every 4s (preventing the 200-record buffer from overflowing).
- Poll `/admin/logs` and stream refresher events.
- Track streaming metrics (TTFT, inter-chunk gap stalls, parser errors).
- Write merged telemetry into `runs/20261019-exp03/timeline.jsonl`.
- Write metrics snapshots into `runs/20261019-exp03/metrics.jsonl`.

---

## 3. Running the Experiment

The runner executes tasks sequentially (strict concurrency 1) with automatic sandbox seeding and semantic invariant checks:

### Standard Execution (Chat Completions, Non-Streaming):
```powershell
python docs/babysit/runner.py --run-id "20261019-exp03" --max-tasks 5
```

### Streaming Execution (SSE Evaluation for TTFT & Refusal Leaks):
```powershell
python docs/babysit/runner.py --run-id "20261019-exp03-stream" --max-tasks 5 --stream
```

### Responses API Execution (Testing OpenAI /v1/responses Protocol):
```powershell
python docs/babysit/runner.py --run-id "20261019-exp03-resp" --max-tasks 5 --protocol responses --stream
```

### Resuming an Interrupted Run:
If a run stops or crashes, resume it without repeating finished tasks:
```powershell
python docs/babysit/runner.py --run-id "20261019-exp03"
# Runner picks up from the same run-id, skipping task IDs found in completed_tasks.json
```

---

## 4. Emergency Stop & Kill Switch

To immediately halt the experiment safely:
1. Create a kill-switch file:
   ```powershell
   New-Item -Path "STOP_BABYSIT" -ItemType File
   # OR inside the run folder:
   New-Item -Path "runs/20261019-exp03/STOP" -ItemType File
   ```
2. The collector and runner check for this file on every cycle and terminate gracefully within 4 seconds.
3. Remove the kill-switch file before starting a new run:
   ```powershell
   Remove-Item -Path "STOP_BABYSIT" -Force -ErrorAction SilentlyContinue
   ```

---

## 5. Where to Inspect Results

- **Findings (Evidence-based & Semantic Invariants):** `runs/<run-id>/findings.jsonl`
  Check for invariant types:
  - `leaked_refusal_phrase` (Google Drive / Workspace soft refusal)
  - `internal_code_interpreter_leak` (code_reference / code_stdout)
  - `prose_refusal_instead_of_tool` (conversational refusal when tools were expected)
  - `tool_repetition_loop` (repeated identical failing calls)
  - `stream_chunk_stall` (inter-chunk delays > 15s)
  - `artifact_missing` (failed output generation in sandbox)
- **Argon Full Conversational & Tool Traces:** `runs/<run-id>/argon_trace.jsonl`
- **Correlated Timeline:** `runs/<run-id>/timeline.jsonl`
- **Metrics Snapshots:** `runs/<run-id>/metrics.jsonl`
- **Admin GUI Web Dashboard:** `http://127.0.0.1:5918/` (Debug Metrics and Babysit runs panel).

---

## 6. How to Revert Configuration After the Experiment (T1 Rollback)

Once the experiment is concluded, restore normal production settings (Argon fallback to Flash enabled, stall breaker 180s):

1. Edit `.env` to remove the appended lines:
   ```powershell
   (Get-Content .env) | Where-Object { $_ -notmatch '^ARGON_FALLBACK_TO_FLASH=' -and $_ -notmatch '^ARGON_STALL_BREAKER_SECONDS=' } | Set-Content .env
   ```
2. Recreate the `gemini2api` container:
   ```powershell
   docker compose up -d --no-deps --force-recreate gemini2api
   ```
3. Verify `/admin/config`:
   `argon_fallback_to_flash` will revert to `true` (default), and `argon_stall_breaker_seconds` will revert to `180` (default).
