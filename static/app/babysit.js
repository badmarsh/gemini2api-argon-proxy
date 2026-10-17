/**
 * Babysit Experiment Real-Time Monitor
 * Live tracking of Orchestrator -> Argon 4 -> Monitor agent runs.
 */

import { apiCall } from './auth.js';
import { escapeHtml, showToast, showConfirm } from './utils.js';

let pollTimer = null;
let isPollingActive = true;
let currentRunId = null;
let activeTab = 'tasks';

export function initBabysit() {
    initControls();
    loadBabysit();
    startPolling();
}

function initControls() {
    const runSelect = document.getElementById('babysit-run-select');
    if (runSelect) {
        runSelect.addEventListener('change', (e) => {
            currentRunId = e.target.value;
            loadRunDetails(currentRunId);
        });
    }

    const refreshBtn = document.getElementById('babysit-refresh-btn');
    if (refreshBtn) {
        refreshBtn.addEventListener('click', () => {
            loadBabysit();
            showToast('Refreshed babysit run data', 'info');
        });
    }

    const pollToggle = document.getElementById('babysit-poll-toggle');
    if (pollToggle) {
        pollToggle.addEventListener('click', () => {
            isPollingActive = !isPollingActive;
            const icon = pollToggle.querySelector('i');
            if (isPollingActive) {
                pollToggle.innerHTML = '<i class="fas fa-pause"></i> Auto-poll: On';
                startPolling();
            } else {
                pollToggle.innerHTML = '<i class="fas fa-play"></i> Auto-poll: Paused';
                stopPolling();
            }
        });
    }

    // Kill-switch toggle
    const killBtn = document.getElementById('babysit-kill-switch-btn');
    if (killBtn) {
        killBtn.addEventListener('click', handleKillSwitchToggle);
    }

    // Tab buttons
    document.querySelectorAll('.babysit-tab-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            switchBabysitTab(btn.dataset.tab);
        });
    });
}

function startPolling() {
    stopPolling();
    pollTimer = setInterval(() => {
        const section = document.getElementById('babysit');
        if (section && section.classList.contains('active') && isPollingActive && document.visibilityState === 'visible') {
            if (currentRunId) {
                loadRunDetails(currentRunId, true);
            } else {
                loadBabysit(true);
            }
        }
    }, 4000);
}

function stopPolling() {
    if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
    }
}

function switchBabysitTab(tabId) {
    activeTab = tabId;
    document.querySelectorAll('.babysit-tab-btn').forEach(btn => {
        const isActive = btn.dataset.tab === tabId;
        btn.classList.toggle('active', isActive);
        btn.setAttribute('aria-selected', isActive ? 'true' : 'false');
    });

    document.querySelectorAll('.babysit-panel').forEach(p => {
        p.classList.toggle('active', p.id === `bb-panel-${tabId}`);
    });
}

export async function loadBabysit(isBackground = false) {
    try {
        const data = await apiCall('GET', '/admin/babysit/runs');
        const runs = data.runs || [];
        const select = document.getElementById('babysit-run-select');

        if (!select) return;

        if (runs.length === 0) {
            select.innerHTML = '<option value="">No experiment runs found</option>';
            document.getElementById('babysit-current-run-id').textContent = 'No active runs in /app/runs';
            renderEmptyRun();
            return;
        }

        const prevSelection = currentRunId || select.value;
        select.innerHTML = runs.map(r => `
            <option value="${escapeHtml(r.run_id)}">
                ${escapeHtml(r.run_id)} (${r.task_completed}/${r.task_count} tasks, ${r.stopped ? 'STOPPED' : 'ACTIVE'})
            </option>
        `).join('');

        if (prevSelection && runs.some(r => r.run_id === prevSelection)) {
            currentRunId = prevSelection;
        } else {
            currentRunId = runs[0].run_id;
        }
        select.value = currentRunId;

        await loadRunDetails(currentRunId, isBackground);
    } catch (e) {
        console.error('Failed to load babysit runs:', e);
    }
}

async function loadRunDetails(runId, isBackground = false) {
    if (!runId) return;

    try {
        const details = await apiCall('GET', `/admin/babysit/runs/${encodeURIComponent(runId)}`);
        renderHeroBanner(details);
        renderKPIs(details);
        renderTasks(details.tasks || []);
        renderTrace(details.trace || []);
        renderFindings(details.findings || []);
    } catch (e) {
        console.error(`Failed to load run ${runId}:`, e);
    }
}

function renderHeroBanner(details) {
    const runIdEl = document.getElementById('babysit-current-run-id');
    const badgeEl = document.getElementById('babysit-status-badge');
    const statusTextEl = document.getElementById('babysit-status-text');
    const killBtn = document.getElementById('babysit-kill-switch-btn');

    if (runIdEl) runIdEl.textContent = `Run: ${details.run_id}`;

    const isStopped = Boolean(details.stopped);
    const tasks = details.tasks || [];
    const isCompleted = tasks.length > 0 && tasks.every(t => t.status === 'completed' || t.status === 'failed');

    if (badgeEl && statusTextEl) {
        if (isStopped) {
            badgeEl.className = 'babysit-status-badge status-stopped';
            statusTextEl.textContent = 'STOPPED (KILL-SWITCH)';
        } else if (isCompleted) {
            badgeEl.className = 'babysit-status-badge status-completed';
            statusTextEl.textContent = 'RUN COMPLETED';
        } else {
            badgeEl.className = 'babysit-status-badge status-running';
            statusTextEl.textContent = 'RUNNING';
        }
    }

    if (killBtn) {
        if (isStopped) {
            killBtn.className = 'btn btn-primary';
            killBtn.innerHTML = '<i class="fas fa-play"></i> Resume Run';
            killBtn.dataset.action = 'resume';
        } else {
            killBtn.className = 'btn btn-danger';
            killBtn.innerHTML = '<i class="fas fa-skull-crossbones"></i> Emergency Stop';
            killBtn.dataset.action = 'stop';
        }
    }
}

function renderKPIs(details) {
    const tasks = details.tasks || [];
    const findings = details.findings || [];
    const trace = details.trace || [];

    const total = tasks.length;
    const completed = tasks.filter(t => t.status === 'completed').length;
    const failed = tasks.filter(t => t.status === 'failed').length;

    const totalTasksEl = document.getElementById('bb-total-tasks');
    const compTasksEl = document.getElementById('bb-completed-tasks');
    const failTasksEl = document.getElementById('bb-failed-tasks');
    const findingsEl = document.getElementById('bb-findings-count');

    if (totalTasksEl) totalTasksEl.textContent = String(total);
    if (compTasksEl) compTasksEl.textContent = String(completed);
    if (failTasksEl) failTasksEl.textContent = String(failed);
    if (findingsEl) findingsEl.textContent = String(findings.length);

    const tabTask = document.getElementById('bb-tab-task-count');
    const tabTrace = document.getElementById('bb-tab-trace-count');
    const tabFinding = document.getElementById('bb-tab-finding-count');

    if (tabTask) tabTask.textContent = String(total);
    if (tabTrace) tabTrace.textContent = String(trace.length);
    if (tabFinding) tabFinding.textContent = String(findings.length);
}

function renderTasks(tasks) {
    const container = document.getElementById('bb-task-list');
    if (!container) return;

    if (tasks.length === 0) {
        container.innerHTML = '<div class="babysit-empty-state"><i class="fas fa-tasks"></i><p>No tasks found in tasks.jsonl</p></div>';
        return;
    }

    container.innerHTML = tasks.map(t => {
        const status = t.status || 'in_progress';
        const statusClass = status === 'completed' ? 'badge-status-2xx' : (status === 'failed' ? 'badge-status-5xx' : 'badge-status-4xx');
        const desc = t.description || t.task || 'No description provided';
        const verifyCmd = t.verification_command ? `<code class="code-inline" style="display:inline-block; margin-top:0.35rem;">$ ${escapeHtml(t.verification_command)}</code>` : '';

        return `
            <div class="babysit-task-card">
                <div class="babysit-task-header">
                    <span class="babysit-task-id">${escapeHtml(t.task_id || t.id || 'Task')}</span>
                    <span class="inspector-badge ${statusClass}">${escapeHtml(status.toUpperCase())}</span>
                </div>
                <div class="babysit-task-desc">${escapeHtml(desc)}</div>
                ${verifyCmd}
                <div class="babysit-task-meta">
                    <span><i class="fas fa-clock"></i> ${escapeHtml(t.created_at || t.timestamp || '')}</span>
                    ${t.attempts ? `<span><i class="fas fa-redo"></i> Attempts: ${escapeHtml(String(t.attempts))}</span>` : ''}
                </div>
            </div>
        `;
    }).join('');
}

function renderTrace(traceEvents) {
    const container = document.getElementById('bb-trace-log');
    if (!container) return;

    if (traceEvents.length === 0) {
        container.innerHTML = '<div class="babysit-empty-state"><i class="fas fa-terminal"></i><p>No trace events found in argon_trace.jsonl</p></div>';
        return;
    }

    container.innerHTML = traceEvents.slice().reverse().map(e => {
        const stepNum = e.step ?? e.index ?? '-';
        const action = e.action || e.type || 'step';
        const timeStr = e.timestamp || e.ts || '';
        const thought = e.thought ? `<div style="color:var(--text-secondary); margin-bottom:0.35rem;"><em>${escapeHtml(e.thought)}</em></div>` : '';
        const toolStr = e.tool ? `<span class="babysit-trace-tool-badge">${escapeHtml(e.tool)}</span>` : '';
        const payload = e.input || e.args || e.result || e.output;
        const codePayload = payload ? `<pre class="inspector-code-block" style="margin-top:0.35rem; max-height:160px;">${escapeHtml(typeof payload === 'object' ? JSON.stringify(payload, null, 2) : String(payload))}</pre>` : '';

        return `
            <div class="babysit-trace-item">
                <div class="babysit-trace-item-header">
                    <span><strong>Step ${escapeHtml(String(stepNum))}</strong> ${toolStr}</span>
                    <span>${escapeHtml(timeStr)}</span>
                </div>
                ${thought}
                ${codePayload}
            </div>
        `;
    }).join('');
}

function renderFindings(findings) {
    const container = document.getElementById('bb-findings-feed');
    if (!container) return;

    if (findings.length === 0) {
        container.innerHTML = '<div class="babysit-empty-state"><i class="fas fa-shield-alt"></i><p>No monitor findings in findings.jsonl</p></div>';
        return;
    }

    container.innerHTML = findings.slice().reverse().map(f => {
        const sev = (f.severity || 'INFO').toUpperCase();
        const title = f.title || f.category || 'Observation';
        const desc = f.description || f.message || '';
        const rec = f.recommendation ? `<div class="babysit-finding-rec"><strong>Recommendation:</strong> ${escapeHtml(f.recommendation)}</div>` : '';

        return `
            <div class="babysit-finding-card severity-${sev}">
                <div class="babysit-finding-header">
                    <span class="babysit-finding-title">${escapeHtml(title)}</span>
                    <span class="inspector-badge badge-status-${sev === 'CRITICAL' ? '5xx' : (sev === 'HIGH' ? '4xx' : '2xx')}">${escapeHtml(sev)}</span>
                </div>
                <div class="babysit-finding-desc">${escapeHtml(desc)}</div>
                ${rec}
            </div>
        `;
    }).join('');
}

function renderEmptyRun() {
    const heroEl = document.getElementById('babysit-status-badge');
    const heroText = document.getElementById('babysit-status-text');
    if (heroEl && heroText) {
        heroEl.className = 'babysit-status-badge status-stopped';
        heroText.textContent = 'NO ACTIVE RUNS';
    }
    renderKPIs({});
    renderTasks([]);
    renderTrace([]);
    renderFindings([]);
}

async function handleKillSwitchToggle() {
    const btn = document.getElementById('babysit-kill-switch-btn');
    if (!btn) return;

    const action = btn.dataset.action || 'stop';
    const isStop = action === 'stop';

    const confirmed = await showConfirm(
        isStop
            ? `Trigger Emergency Kill-Switch for run '${currentRunId}'?\nThis will create STOP_BABYSIT and pause both Argon and Collector.`
            : `Release Kill-Switch and resume run '${currentRunId}'?`
    );

    if (!confirmed) return;

    try {
        const res = await apiCall('POST', '/admin/babysit/kill-switch', {
            run_id: currentRunId,
            action: action
        });
        showToast(res.message || 'Kill-switch command dispatched', isStop ? 'warning' : 'success');
        if (currentRunId) {
            await loadRunDetails(currentRunId);
        }
    } catch (e) {
        showToast(`Kill-switch error: ${e.message}`, 'error');
    }
}

export function destroyBabysit() {
    stopPolling();
}
