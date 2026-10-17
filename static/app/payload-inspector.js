/**
 * Payload Inspector Modal
 * Antigravity-styled deep inspection drawer for In / Forwarded / Out payloads.
 */

import { escapeHtml } from './utils.js';

let modalContainer = null;
let currentRecord = null;
let activeTab = 'in';

function ensureModal() {
    if (modalContainer && document.body.contains(modalContainer)) return;

    modalContainer = document.createElement('div');
    modalContainer.id = 'payload-inspector-container';
    modalContainer.className = 'inspector-overlay';
    modalContainer.setAttribute('role', 'dialog');
    modalContainer.setAttribute('aria-modal', 'true');
    modalContainer.setAttribute('aria-labelledby', 'inspector-title');

    modalContainer.innerHTML = `
        <div class="inspector-dialog">
            <div class="inspector-header">
                <div class="inspector-header-top">
                    <div class="inspector-badge-row">
                        <span id="insp-badge-status" class="inspector-badge badge-status-2xx">200</span>
                        <span id="insp-badge-method" class="inspector-badge badge-method">POST</span>
                        <span id="insp-badge-protocol" class="inspector-badge badge-protocol">Gemini</span>
                        <span id="insp-badge-route" class="inspector-badge badge-model-route">-- &rarr; --</span>
                        <span id="insp-badge-account" class="inspector-badge badge-model-route"><i class="fas fa-user-circle"></i> --</span>
                    </div>
                    <button id="insp-close-btn" class="inspector-close-btn" aria-label="Close inspector">
                        <i class="fas fa-times"></i>
                    </button>
                </div>
                <div style="font-family:monospace; font-size:0.8125rem; color:var(--text-secondary); word-break:break-all;" id="insp-header-path">
                    /v1beta/models/...
                </div>
                <div class="inspector-metrics-strip">
                    <div class="inspector-metric-item">
                        <span class="inspector-metric-label">Tokens (In / Out)</span>
                        <span class="inspector-metric-val" id="insp-tokens">In: -- | Out: --</span>
                    </div>
                    <div class="inspector-metric-item">
                        <span class="inspector-metric-label">Cache</span>
                        <span class="inspector-metric-val highlight" id="insp-cache">0</span>
                    </div>
                    <div class="inspector-metric-item">
                        <span class="inspector-metric-label">Latency</span>
                        <span class="inspector-metric-val" id="insp-latency">--</span>
                    </div>
                    <div class="inspector-metric-item">
                        <span class="inspector-metric-label">TTFT</span>
                        <span class="inspector-metric-val" id="insp-ttft">--</span>
                    </div>
                    <div class="inspector-metric-item">
                        <span class="inspector-metric-label">Time</span>
                        <span class="inspector-metric-val" id="insp-time">--</span>
                    </div>
                </div>
            </div>

            <nav class="inspector-nav" role="tablist">
                <button class="inspector-tab active" data-tab="in" role="tab" aria-selected="true">
                    <i class="fas fa-arrow-right"></i> Request (In)
                </button>
                <button class="inspector-tab" data-tab="forwarded" role="tab" aria-selected="false">
                    <i class="fas fa-share"></i> Forwarded Upstream
                </button>
                <button class="inspector-tab" data-tab="out" role="tab" aria-selected="false">
                    <i class="fas fa-arrow-left"></i> Response (Out)
                </button>
                <button class="inspector-tab" data-tab="meta" role="tab" aria-selected="false">
                    <i class="fas fa-info-circle"></i> Diagnostics & Meta
                </button>
            </nav>

            <div class="inspector-body">
                <!-- Request (In) Panel -->
                <div class="inspector-panel active" id="insp-panel-in" role="tabpanel">
                    <div class="inspector-code-toolbar">
                        <span class="inspector-code-info">Incoming Client Request Payload</span>
                        <button class="inspector-copy-btn" data-copy-target="insp-code-in">
                            <i class="fas fa-copy"></i> Copy
                        </button>
                    </div>
                    <pre class="inspector-code-block" id="insp-code-in"></pre>
                </div>

                <!-- Forwarded Upstream Panel -->
                <div class="inspector-panel" id="insp-panel-forwarded" role="tabpanel">
                    <div class="inspector-code-toolbar">
                        <span class="inspector-code-info">Forwarded Upstream Headers & Target</span>
                        <button class="inspector-copy-btn" data-copy-target="insp-code-forwarded">
                            <i class="fas fa-copy"></i> Copy
                        </button>
                    </div>
                    <pre class="inspector-code-block" id="insp-code-forwarded"></pre>
                </div>

                <!-- Response (Out) Panel -->
                <div class="inspector-panel" id="insp-panel-out" role="tabpanel">
                    <div class="inspector-code-toolbar">
                        <span class="inspector-code-info">Returned Model Output / Generated Response</span>
                        <button class="inspector-copy-btn" data-copy-target="insp-code-out">
                            <i class="fas fa-copy"></i> Copy
                        </button>
                    </div>
                    <pre class="inspector-code-block" id="insp-code-out"></pre>
                </div>

                <!-- Diagnostics & Meta Panel -->
                <div class="inspector-panel" id="insp-panel-meta" role="tabpanel">
                    <div class="inspector-code-toolbar">
                        <span class="inspector-code-info">Request Timing, Fallback, and System Diagnostics</span>
                        <button class="inspector-copy-btn" data-copy-target="insp-code-meta">
                            <i class="fas fa-copy"></i> Copy
                        </button>
                    </div>
                    <pre class="inspector-code-block" id="insp-code-meta"></pre>
                </div>
            </div>
        </div>
    `;

    document.body.appendChild(modalContainer);

    // Bind events
    modalContainer.querySelector('#insp-close-btn').addEventListener('click', closePayloadInspector);
    modalContainer.addEventListener('click', (e) => {
        if (e.target === modalContainer) closePayloadInspector();
    });

    modalContainer.querySelectorAll('.inspector-tab').forEach(tab => {
        tab.addEventListener('click', () => {
            switchTab(tab.dataset.tab);
        });
    });

    modalContainer.querySelectorAll('.inspector-copy-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const targetId = btn.dataset.copyTarget;
            const el = document.getElementById(targetId);
            if (el && el.textContent) {
                navigator.clipboard.writeText(el.textContent).then(() => {
                    const originalHtml = btn.innerHTML;
                    btn.innerHTML = '<i class="fas fa-check"></i> Copied!';
                    setTimeout(() => { btn.innerHTML = originalHtml; }, 2000);
                });
            }
        });
    });

    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && modalContainer && modalContainer.classList.contains('visible')) {
            closePayloadInspector();
        }
    });
}

function switchTab(tabId) {
    activeTab = tabId;
    if (!modalContainer) return;

    modalContainer.querySelectorAll('.inspector-tab').forEach(t => {
        const isActive = t.dataset.tab === tabId;
        t.classList.toggle('active', isActive);
        t.setAttribute('aria-selected', isActive ? 'true' : 'false');
    });

    modalContainer.querySelectorAll('.inspector-panel').forEach(p => {
        p.classList.toggle('active', p.id === `insp-panel-${tabId}`);
    });
}

function formatTokens(n) {
    if (n === null || n === undefined || isNaN(n)) return null;
    if (n >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, '') + 'k';
    return String(n);
}

function sanitizeSecrets(obj) {
    if (!obj || typeof obj !== 'object') return obj;
    const clone = JSON.parse(JSON.stringify(obj));

    const maskVal = (val) => {
        if (typeof val === 'string' && val.length > 8) {
            return val.slice(0, 4) + '...' + val.slice(-4);
        }
        return '***';
    };

    const walk = (item) => {
        if (!item || typeof item !== 'object') return;
        if (item.headers && typeof item.headers === 'object') {
            for (const k of Object.keys(item.headers)) {
                if (['authorization', 'cookie', 'x-api-key', 'x-goog-api-key'].includes(k.toLowerCase())) {
                    item.headers[k] = '[REDACTED_SECRET]';
                }
            }
        }
        for (const secretKey of ['psid', 'psidts', 'api_key', 'access_token']) {
            if (item[secretKey]) item[secretKey] = maskVal(item[secretKey]);
        }
        for (const val of Object.values(item)) {
            if (typeof val === 'object') walk(val);
        }
    };

    walk(clone);
    return clone;
}

export function openPayloadInspector(record) {
    ensureModal();
    currentRecord = record || {};

    const path = String(currentRecord.path || currentRecord.endpoint || '');
    let protocol = 'Gemini';
    if (path.includes('/v1/chat/completions') || path.includes('/v1/models') || path.includes('/v1/embeddings')) {
        protocol = 'OpenAI';
    } else if (path.includes('/v1/messages')) {
        protocol = 'Claude';
    }

    const status = currentRecord.status || 200;
    const statusClass = status < 300 ? 'badge-status-2xx' : (status < 400 ? 'badge-status-3xx' : (status < 500 ? 'badge-status-4xx' : 'badge-status-5xx'));
    const statusEl = document.getElementById('insp-badge-status');
    statusEl.className = `inspector-badge ${statusClass}`;
    statusEl.textContent = String(status);

    document.getElementById('insp-badge-method').textContent = (currentRecord.method || 'POST').toUpperCase();
    document.getElementById('insp-badge-protocol').textContent = protocol;

    const reqModel = currentRecord.model || currentRecord.requested_model || 'default';
    const srvModel = currentRecord.served_by || currentRecord.actual_model || reqModel;
    document.getElementById('insp-badge-route').innerHTML = `${escapeHtml(reqModel)} &rarr; <strong>${escapeHtml(srvModel)}</strong>`;

    const account = currentRecord.account_hash || currentRecord.account || 'pool-assigned';
    document.getElementById('insp-badge-account').innerHTML = `<i class="fas fa-user-circle"></i> ${escapeHtml(account)}`;

    document.getElementById('insp-header-path').textContent = path || '-';

    // Metrics calculation
    const reqTokens = currentRecord.prompt_tokens || currentRecord.input_tokens || currentRecord.in_tokens;
    const resTokens = currentRecord.completion_tokens || currentRecord.output_tokens || currentRecord.out_tokens;
    const cachedTokens = currentRecord.cached_tokens || currentRecord.cache_read_input_tokens || 0;

    let inFmt = formatTokens(reqTokens);
    let outFmt = formatTokens(resTokens);
    if (!inFmt && currentRecord.request) {
        const textLen = JSON.stringify(currentRecord.request).length;
        inFmt = '~' + formatTokens(Math.round(textLen / 4));
    }
    if (!outFmt && currentRecord.response) {
        const textLen = JSON.stringify(currentRecord.response).length;
        outFmt = '~' + formatTokens(Math.round(textLen / 4));
    }

    document.getElementById('insp-tokens').textContent = `In: ${inFmt || '--'} | Out: ${outFmt || '--'}`;

    const cacheEl = document.getElementById('insp-cache');
    if (cachedTokens > 0) {
        const cachePct = reqTokens ? ((cachedTokens / (reqTokens + cachedTokens)) * 100).toFixed(1) : '100';
        cacheEl.textContent = `⚡ ${formatTokens(cachedTokens)} (${cachePct}%)`;
    } else {
        cacheEl.textContent = '0';
    }

    const latencyMs = currentRecord.latency_ms ?? currentRecord.total_latency_ms;
    document.getElementById('insp-latency').textContent = latencyMs !== null && latencyMs !== undefined ? `${(latencyMs / 1000).toFixed(2)}s` : '--';

    const ttftMs = currentRecord.ttft_ms;
    document.getElementById('insp-ttft').textContent = ttftMs !== null && ttftMs !== undefined ? `${(ttftMs / 1000).toFixed(2)}s` : '--';

    const ts = currentRecord.ts || currentRecord.ingress_ts;
    const timeStr = ts ? new Date(ts).toLocaleTimeString() : '--';
    document.getElementById('insp-time').textContent = timeStr;

    // Fill tabs
    const safeReq = sanitizeSecrets(currentRecord.request || currentRecord.request_body);
    const reqCode = safeReq ? JSON.stringify(safeReq, null, 2) : (
        currentRecord.prompt ? JSON.stringify({ prompt: currentRecord.prompt }, null, 2) : '// No incoming payload recorded for this request'
    );
    document.getElementById('insp-code-in').textContent = reqCode;

    // Forwarded Upstream
    const forwardedData = {
        upstream_target: protocol === 'Gemini' ? 'Google Generative Language Web API' : `Forwarded ${protocol} Upstream`,
        requested_model: reqModel,
        served_by: srvModel,
        stream_mode: Boolean(currentRecord.stream),
        request_id: currentRecord.request_id || currentRecord.id,
        tags: currentRecord.tags || [],
        forwarded_headers: {
            "x-g2a-run": currentRecord.run_id || (currentRecord.tags ? currentRecord.tags.find(t => t.startsWith('run:')) : null),
            "x-g2a-role": currentRecord.role || (currentRecord.tags ? currentRecord.tags.find(t => t.startsWith('role:')) : null),
            "content-type": "application/json"
        }
    };
    document.getElementById('insp-code-forwarded').textContent = JSON.stringify(forwardedData, null, 2);

    // Out
    const safeResp = sanitizeSecrets(currentRecord.response || currentRecord.response_body);
    let respCode = safeResp ? JSON.stringify(safeResp, null, 2) : null;
    if (!respCode) {
        if (currentRecord.stream) {
            respCode = '// Streaming response streamGenerateContent completed\n// (Full SSE frames streamed directly to client without buffering in memory)';
        } else {
            respCode = '// Response body not captured or empty';
        }
    }
    document.getElementById('insp-code-out').textContent = respCode;

    // Meta & Diagnostics
    const metaData = {
        request_id: currentRecord.request_id || currentRecord.id,
        ingress_timestamp: currentRecord.ingress_ts || currentRecord.ts,
        completion_timestamp: currentRecord.ts,
        total_latency_ms: latencyMs,
        ttft_ms: ttftMs,
        max_inter_chunk_gap_ms: currentRecord.max_inter_chunk_gap_ms,
        error_class: currentRecord.error_class || null,
        fallback_used: Boolean(currentRecord.fallback_used),
        fallback_reason: currentRecord.fallback_reason || null,
        regenerations: currentRecord.regenerations || 0,
        finish_reason: currentRecord.finish_reason || 'stop',
        parser_repaired: Boolean(currentRecord.parser_repaired),
        truncated_suspected: Boolean(currentRecord.truncated_suspected)
    };
    document.getElementById('insp-code-meta').textContent = JSON.stringify(metaData, null, 2);

    switchTab('in');
    modalContainer.classList.add('visible');
}

export function closePayloadInspector() {
    if (modalContainer) {
        modalContainer.classList.remove('visible');
    }
}
