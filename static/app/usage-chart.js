/**
 * Usage Stats Chart - Pure SVG chart renderer (responsive, Chinese UI)
 */

import { apiCall } from './auth.js';
import { t } from './i18n.js';
import { formatNumber, showToast } from './utils.js';

let currentGranularity = 'hourly';
let currentHours = 24;

export function initUsageStats() {
    initControls();
    loadUsageStats();
}

function initControls() {
    const granBtns = document.querySelectorAll('#us-granularity .btn');
    granBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            granBtns.forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            currentGranularity = btn.dataset.value;
            applyRangeFilter();
            loadHistory();
        });
    });

    const timeBtns = document.querySelectorAll('#us-timerange .btn');
    timeBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            timeBtns.forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            currentHours = btn.dataset.value === 'all' ? 'all' : parseInt(btn.dataset.value);
            loadHistory();
        });
    });

    const refreshBtn = document.getElementById('us-refresh-btn');
    if (refreshBtn) {
        refreshBtn.addEventListener('click', loadUsageStats);
    }

    applyRangeFilter();
}

function applyRangeFilter() {
    const timeBtns = document.querySelectorAll('#us-timerange .btn');
    timeBtns.forEach(btn => {
        const val = btn.dataset.value;
        const hours = val === 'all' ? Infinity : parseInt(val);
        let hide = false;

        if (currentGranularity === 'daily' && hours <= 24) hide = true;
        if (currentGranularity === 'five_min' && hours > 24) hide = true;
        if (currentGranularity === 'hourly' && hours < 6) hide = true;

        btn.style.display = hide ? 'none' : '';
    });

    const activeBtn = document.querySelector('#us-timerange .btn.active');
    if (activeBtn && activeBtn.style.display === 'none') {
        const visible = document.querySelector('#us-timerange .btn:not([style*="none"])');
        if (visible) {
            document.querySelectorAll('#us-timerange .btn').forEach(b => b.classList.remove('active'));
            visible.classList.add('active');
            currentHours = visible.dataset.value === 'all' ? 'all' : parseInt(visible.dataset.value);
        }
    }
}

export async function loadUsageStats() {
    await Promise.all([loadSummary(), loadHistory()]);
}

async function loadSummary() {
    try {
        const data = await apiCall('GET', '/admin/usage-stats/summary');
        const reqEl = document.getElementById('us-total-requests');
        const errEl = document.getElementById('us-error-rate');
        const latEl = document.getElementById('us-avg-latency');
        const rotEl = document.getElementById('us-rotation-rate');

        if (reqEl) reqEl.textContent = formatNumber(data.request_count || 0);

        if (errEl) {
            const total = data.request_count || 0;
            const errors = data.error_count || 0;
            const rate = total > 0 ? ((errors / total) * 100).toFixed(1) : '0';
            errEl.textContent = rate + '%';
        }

        if (latEl) latEl.textContent = (data.avg_latency_ms || 0).toFixed(0) + 'ms';

        if (rotEl) {
            const rs = data.rotation_success || 0;
            const rf = data.rotation_failure || 0;
            const total = rs + rf;
            const rate = total > 0 ? ((rs / total) * 100).toFixed(0) : '0';
            rotEl.textContent = rate + '%';
        }

        let modelData = data.model_requests || {};
        if (Object.keys(modelData).length === 0) {
            try {
                const status = await apiCall('GET', '/admin/status');
                const accounts = status.accounts || [];
                accounts.forEach(a => {
                    if (a.models && Array.isArray(a.models)) {
                        a.models.forEach(m => { modelData[m] = modelData[m] || 0; });
                    }
                });
            } catch (ignored) {}
        }
        renderModelTable(modelData);
    } catch (e) {
        console.error('Load usage summary failed:', e);
    }
}

function updateContextualSummary(data) {
    const reqEl = document.getElementById('us-total-requests');
    const errEl = document.getElementById('us-error-rate');
    const latEl = document.getElementById('us-avg-latency');
    const rotEl = document.getElementById('us-rotation-rate');

    if (!data || data.length === 0) return;

    let totalRequests = 0;
    let totalErrors = 0;
    let totalLatencySum = 0;
    let latencyCount = 0;
    let rotSuccess = 0;
    let rotFailure = 0;
    const modelTotals = {};

    for (const b of data) {
        const rc = b.request_count || 0;
        const ec = b.error_count || 0;
        totalRequests += rc;
        totalErrors += ec;

        if (b.avg_latency_ms && rc > 0) {
            totalLatencySum += b.avg_latency_ms * rc;
            latencyCount += rc;
        } else if (b.avg_latency_ms) {
            totalLatencySum += b.avg_latency_ms;
            latencyCount += 1;
        }

        rotSuccess += b.rotation_success || 0;
        rotFailure += b.rotation_failure || 0;

        if (b.model_requests) {
            for (const [m, c] of Object.entries(b.model_requests)) {
                modelTotals[m] = (modelTotals[m] || 0) + c;
            }
        }
    }

    if (reqEl) reqEl.textContent = formatNumber(totalRequests);

    if (errEl) {
        const errRate = totalRequests > 0 ? ((totalErrors / totalRequests) * 100).toFixed(1) : '0';
        errEl.textContent = errRate + '%';
    }

    if (latEl) {
        const avgLat = latencyCount > 0 ? Math.round(totalLatencySum / latencyCount) : 0;
        latEl.textContent = avgLat + 'ms';
    }

    if (rotEl) {
        const rotTotal = rotSuccess + rotFailure;
        const rotRate = rotTotal > 0 ? Math.round((rotSuccess / rotTotal) * 100) : 0;
        rotEl.textContent = rotRate + '%';
    }

    if (Object.keys(modelTotals).length > 0) {
        renderModelTable(modelTotals);
    }
}

async function loadHistory() {
    const container = document.getElementById('us-chart-container');
    if (!container) return;

    container.innerHTML = '<div class="chart-loading"><i class="fas fa-spinner fa-spin"></i> Loading...</div>';

    try {
        let url = '/admin/usage-stats/history?granularity=' + currentGranularity;
        if (currentHours !== 'all') {
            url += '&hours=' + currentHours;
        }
        const data = await apiCall('GET', url);
        if (!data || data.length === 0) {
            container.innerHTML = '<div class="empty-chart"><i class="fas fa-chart-bar"></i><p>No data</p></div>';
            return;
        }
        updateContextualSummary(data);
        renderChart(container, data);
    } catch (e) {
        container.innerHTML = '<div class="empty-chart"><i class="fas fa-exclamation-circle"></i><p>Failed to load</p></div>';
        console.error('Load usage history failed:', e);
    }
}

function renderChart(container, data) {
    const containerWidth = container.clientWidth || 800;
    const W = Math.max(600, containerWidth);
    const H = 320;
    const pad = { top: 30, right: 60, bottom: 40, left: 55 };
    const cw = W - pad.left - pad.right;
    const ch = H - pad.top - pad.bottom;

    // Get theme-aware colors from CSS variables
    const styles = getComputedStyle(document.documentElement);
    const borderColor = styles.getPropertyValue('--border-color').trim();
    const textSecondary = styles.getPropertyValue('--text-secondary').trim();
    const primaryColor = styles.getPropertyValue('--primary-color').trim();
    const warningColor = styles.getPropertyValue('--warning-color').trim();

    const maxReq = Math.max(...data.map(d => d.request_count), 1);
    const maxLat = Math.max(...data.map(d => d.avg_latency_ms), 1);
    const gap = cw / data.length;
    const barW = Math.max(2, Math.min(24, gap * 0.7));
    let svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto;min-height:320px;display:block">';

    for (let i = 0; i <= 4; i++) {
        const y = pad.top + ch - (ch * i / 4);
        const val = Math.round(maxReq * i / 4);
        svg += '<line x1="' + pad.left + '" y1="' + y + '" x2="' + (W - pad.right) + '" y2="' + y + '" stroke="' + borderColor + '" stroke-dasharray="2,2"/>';
        svg += '<text x="' + (pad.left - 8) + '" y="' + (y + 4) + '" text-anchor="end" font-size="10" fill="' + textSecondary + '">' + val + '</text>';
    }

    data.forEach((d, i) => {
        const x = pad.left + i * gap + (gap - barW) / 2;
        const h = (d.request_count / maxReq) * ch;
        const y = pad.top + ch - h;
        svg += '<rect x="' + x + '" y="' + y + '" width="' + barW + '" height="' + h + '" fill="' + primaryColor + '" opacity="0.7" rx="2"/>';
    });

    let points = data.map((d, i) => {
        const x = pad.left + i * gap + gap / 2;
        const y = pad.top + ch - (d.avg_latency_ms / maxLat) * ch;
        return x + ',' + y;
    }).join(' ');
    svg += '<polyline points="' + points + '" fill="none" stroke="' + warningColor + '" stroke-width="2" stroke-linejoin="round"/>';

    for (let i = 0; i <= 4; i++) {
        const y = pad.top + ch - (ch * i / 4);
        const val = Math.round(maxLat * i / 4);
        svg += '<text x="' + (W - pad.right + 8) + '" y="' + (y + 4) + '" font-size="10" fill="' + warningColor + '">' + val + 'ms</text>';
    }

    const step = Math.max(1, Math.floor(data.length / 8));
    data.forEach((d, i) => {
        if (i % step !== 0) return;
        const x = pad.left + i * gap + gap / 2;
        const t = new Date(d.timestamp);
        const hh = t.getHours().toString().padStart(2, '0');
        const mm = t.getMinutes().toString().padStart(2, '0');
        let label = hh + ':' + mm;
        if (currentGranularity === 'daily' || currentHours === 'all' || (typeof currentHours === 'number' && currentHours > 48)) {
            label = (t.getMonth() + 1) + '/' + t.getDate() + ' ' + hh + ':' + mm;
        }
        svg += '<text x="' + x + '" y="' + (H - pad.bottom + 16) + '" text-anchor="middle" font-size="10" fill="' + textSecondary + '">' + label + '</text>';
    });

    svg += '<rect x="' + pad.left + '" y="8" width="10" height="10" fill="' + primaryColor + '" opacity="0.7" rx="2"/>';
    svg += '<text x="' + (pad.left + 14) + '" y="17" font-size="11" fill="' + textSecondary + '">Requests</text>';
    svg += '<line x1="' + (pad.left + 75) + '" y1="13" x2="' + (pad.left + 87) + '" y2="13" stroke="' + warningColor + '" stroke-width="2"/>';
    svg += '<text x="' + (pad.left + 91) + '" y="17" font-size="11" fill="' + textSecondary + '">Latency</text>';

    svg += '</svg>';
    container.innerHTML = svg;
}

function renderModelTable(modelRequests) {
    const container = document.getElementById('us-model-table');
    if (!container) return;

    const entries = Object.entries(modelRequests).sort((a, b) => b[1] - a[1]);
    if (entries.length === 0) {
        container.innerHTML = '<div class="empty-chart"><i class="fas fa-cube"></i><p>No model data</p></div>';
        return;
    }

    const maxCount = Math.max(entries[0][1], 1);
    const total = entries.reduce((s, e) => s + e[1], 0);
    let html = `<table><thead><tr><th>${t('playground.model')}</th><th>${t('accounts.requests')}</th><th>${t('usage.proportion')}</th></tr></thead><tbody>`;

    entries.forEach(([model, count]) => {
        const pct = total > 0 ? ((count / total) * 100).toFixed(1) : '0.0';
        const barPct = maxCount > 0 ? ((count / maxCount) * 100).toFixed(0) : '0';
        html += '<tr>';
        html += '<td>' + model + '</td>';
        html += '<td>' + formatNumber(count) + '</td>';
        html += '<td><div style="display:flex;align-items:center;gap:0.5rem">';
        html += '<div class="model-bar-container"><div class="model-bar" style="width:' + barPct + '%"></div></div>';
        html += '<span style="font-size:0.8rem;white-space:nowrap">' + pct + '%</span>';
        html += '</div></td>';
        html += '</tr>';
    });

    html += '</tbody></table>';
    container.innerHTML = html;
}
