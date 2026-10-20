/**
 * Babysit Experiment Real-Time Monitor
 * Live tracking of Orchestrator -> Argon 4 -> Monitor agent runs.
 */

import { apiCall } from './auth.js';
import { escapeHtml, showToast, showConfirm } from './utils.js';

function escapeAttr(value) {
    return escapeHtml(String(value ?? '')).replace(/"/g, '&quot;');
}

let pollTimer = null;
// Two independent gates control the interval: SPA visibility and the operator's toggle.
// Keep the toggle preference when navigating away so returning does not silently resume a
// poller that the operator explicitly paused.
let isSectionActive = false;
let isAutoPollingEnabled = true;
let currentRunId = null;
let currentView = 'experiment';
let activeTab = 'tasks';

export function initBabysit() {
    initControls();
}

function syncPollingState() {
    if (isSectionActive && isAutoPollingEnabled) {
        startPolling();
    } else {
        stopPolling();
    }
}

// SPA lifecycle hook. Hidden sections must not retain polling intervals or stack new ones
// each time the operator returns to the monitor.
export function setBabysitPollingActive(active) {
    isSectionActive = Boolean(active);
    syncPollingState();
}

function initControls() {
    // Top-Level View Switcher (Experimenty vs Zadanie vs Audit)
    document.querySelectorAll('.babysit-top-nav-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            switchBabysitView(btn.dataset.view);
        });
    });

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
            showToast('Údaje experimentu boli obnovené', 'info');
        });
    }

    const pollToggle = document.getElementById('babysit-poll-toggle');
    if (pollToggle) {
        pollToggle.addEventListener('click', () => {
            isAutoPollingEnabled = !isAutoPollingEnabled;
            pollToggle.innerHTML = isAutoPollingEnabled
                ? '<i class="fas fa-pause"></i> Auto-poll: Zapnutý'
                : '<i class="fas fa-play"></i> Auto-poll: Pozastavený';
            syncPollingState();
        });
    }

    // Kill-switch toggle
    const killBtn = document.getElementById('babysit-kill-switch-btn');
    if (killBtn) {
        killBtn.addEventListener('click', handleKillSwitchToggle);
    }

    // Contextual Tab buttons (Tasks, Trace, Findings)
    document.querySelectorAll('.babysit-tab-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            switchBabysitTab(btn.dataset.tab);
        });
    });
}

function switchBabysitView(viewId) {
    currentView = viewId;
    document.querySelectorAll('.babysit-top-nav-btn').forEach(btn => {
        const isActive = btn.dataset.view === viewId;
        btn.classList.toggle('active', isActive);
        btn.setAttribute('aria-selected', isActive ? 'true' : 'false');
    });

    document.querySelectorAll('.babysit-view').forEach(view => {
        view.classList.toggle('active', view.id === `bb-view-${viewId}`);
    });
}

function startPolling() {
    stopPolling();
    pollTimer = setInterval(() => {
        const section = document.getElementById('babysit');
        if (
            isSectionActive &&
            isAutoPollingEnabled &&
            section?.classList.contains('active') &&
            document.visibilityState === 'visible'
        ) {
            if (currentRunId && currentView === 'experiment') {
                loadRunDetails(currentRunId, true);
            } else if (currentView === 'experiment') {
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
            select.innerHTML = '<option value="">Žiadne experimenty na disku</option>';
            document.getElementById('babysit-current-run-id').textContent = 'Žiadne behy v /app/runs';
            renderEmptyRun();
            return;
        }

        const prevSelection = currentRunId || select.value;
        select.innerHTML = runs.map(r => {
            const isSel = (r.run_id === prevSelection) ? 'selected' : '';
            const label = r.display_label || `${r.run_id} (${r.task_completed}/${r.task_count} úloh)`;
            return `<option value="${escapeAttr(r.run_id)}" ${isSel}>${escapeHtml(label)}</option>`;
        }).join('');

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
        renderTasks(details.tasks || [], details.completed_task_ids || []);
        renderTrace(details.trace || []);
        renderFindings(details.findings || []);
    } catch (e) {
        console.error(`Failed to load details for run ${runId}:`, e);
        if (!isBackground) {
            showToast(`Chyba pri načítaní detailov runu ${runId}`, 'error');
        }
    }
}

function renderHeroBanner(details) {
    const runIdEl = document.getElementById('babysit-current-run-id');
    const badgeEl = document.getElementById('babysit-status-badge');
    const statusTextEl = document.getElementById('babysit-status-text');
    const killBtn = document.getElementById('babysit-kill-switch-btn');
    const specialNotice = document.getElementById('bb-special-notice');
    const specialNoticeText = document.getElementById('bb-special-notice-text');

    if (runIdEl) runIdEl.textContent = `Run: ${details.run_id}`;

    const isStopped = Boolean(details.stopped);
    const tasks = details.tasks || [];
    const completedIds = details.completed_task_ids || [];
    const isEmpty = Boolean(details.is_empty) || (tasks.length === 0 && completedIds.length === 0 && (details.trace || []).length === 0);
    const isSmoke = details.run_id === 'smoke-run-01';
    const isCompleted = (tasks.length > 0 && tasks.every(t => t.status === 'completed' || t.status === 'failed' || completedIds.includes(t.task_id || t.id)))
        || (tasks.length === 0 && completedIds.length > 0);

    // Callout notice for empty or smoke runs
    if (specialNotice && specialNoticeText) {
        if (isEmpty) {
            specialNotice.style.display = 'flex';
            specialNoticeText.innerHTML = '<strong>Prázdny priečinok:</strong> Tento beh neobsahuje žiadne definície úloh ani telemetriu. Ide o testovací priečinok vytvorený pri inicializácii API.';
        } else if (isSmoke) {
            specialNotice.style.display = 'flex';
            specialNoticeText.innerHTML = '<strong>Archívny Smoke Test:</strong> Počiatočný overovací beh zo 17. októbra 2026 na otestovanie promptov orchestrátora a monitora.';
        } else {
            specialNotice.style.display = 'none';
        }
    }

    if (badgeEl && statusTextEl) {
        if (isStopped) {
            badgeEl.className = 'babysit-status-badge status-stopped';
            statusTextEl.textContent = 'STOPPED (KILL-SWITCH)';
        } else if (isEmpty) {
            badgeEl.className = 'babysit-status-badge status-empty';
            statusTextEl.textContent = 'PRÁZDNY BEH';
        } else if (isSmoke) {
            badgeEl.className = 'babysit-status-badge status-completed';
            statusTextEl.textContent = 'SMOKE TEST (17.10.)';
        } else if (isCompleted) {
            badgeEl.className = 'babysit-status-badge status-completed';
            statusTextEl.textContent = (details.run_id === '20261020-exp04') ? 'RUN COMPLETED (3S KEEPALIVE)' : (details.run_id === '20261019-exp02') ? 'RUN COMPLETED (OVERENÝ)' : 'RUN COMPLETED';
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
            killBtn.disabled = false;
        } else if (isEmpty || isSmoke || isCompleted) {
            killBtn.className = 'btn btn-outline';
            killBtn.innerHTML = '<i class="fas fa-check"></i> Run Completed';
            killBtn.dataset.action = 'none';
            killBtn.disabled = true;
        } else {
            killBtn.className = 'btn btn-danger';
            killBtn.innerHTML = '<i class="fas fa-skull-crossbones"></i> Emergency Stop';
            killBtn.dataset.action = 'stop';
            killBtn.disabled = false;
        }
    }
}

function renderKPIs(details) {
    const tasks = details.tasks || [];
    const findings = details.findings || [];
    const trace = details.trace || [];
    const completedIds = details.completed_task_ids || [];

    const total = Math.max(tasks.length, completedIds.length);
    const completed = completedIds.length > 0 ? completedIds.length : tasks.filter(t => t.status === 'completed').length;
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

function renderTasks(tasks, completedIds = []) {
    const container = document.getElementById('bb-task-list');
    if (!container) return;

    if (tasks.length === 0) {
        container.innerHTML = '<div class="babysit-empty-state"><i class="fas fa-tasks"></i><p>Žiadne úlohy pre tento experiment.</p></div>';
        return;
    }

    container.innerHTML = tasks.map(t => {
        const isComp = t.status === 'completed' || completedIds.includes(t.task_id || t.id);
        const status = isComp ? 'completed' : (t.status || 'in_progress');
        const statusClass = status === 'completed' ? 'badge-status-2xx' : (status === 'failed' ? 'badge-status-5xx' : 'badge-status-4xx');
        const title = t.title || t.task_id || t.id || 'Task';
        const desc = t.prompt || t.description || t.task || 'Bez popisu';
        const catBadge = t.category ? `<span class="inspector-badge badge-status-default" style="font-size:0.75rem; margin-left:0.5rem; text-transform:none;">${escapeHtml(t.category)}</span>` : '';
        const diffBadge = t.difficulty ? `<span class="inspector-badge badge-status-default" style="font-size:0.7rem; opacity:0.8; margin-left:0.25rem;">${escapeHtml(t.difficulty)}</span>` : '';
        const toolsCount = Array.isArray(t.tools) ? `<span style="margin-left:auto; font-size:0.75rem; color:var(--text-secondary);"><i class="fas fa-wrench"></i> ${escapeHtml(String(t.tools.length))} nástrojov</span>` : '';
        const verifyCmd = t.verification_command ? `<code class="code-inline" style="display:inline-block; margin-top:0.35rem;">$ ${escapeHtml(t.verification_command)}</code>` : '';

        let criteriaHtml = '';
        if (t.expected_behavior?.success_criteria) {
            const sc = t.expected_behavior.success_criteria;
            if (Array.isArray(sc) && sc.length > 0) {
                criteriaHtml = `<div style="margin-top:0.35rem; font-size:0.8rem; color:var(--text-secondary);"><i class="fas fa-clipboard-check"></i> <strong>Kritériá:</strong> ${escapeHtml(sc.join('; '))}</div>`;
            }
        }

        return `
            <div class="babysit-task-card">
                <div class="babysit-task-header">
                    <span class="babysit-task-id">${escapeHtml(title)}</span>
                    <span class="inspector-badge ${statusClass}">${escapeHtml(status.toUpperCase())}</span>
                    ${catBadge}
                    ${diffBadge}
                    ${toolsCount}
                </div>
                <div class="babysit-task-desc">${escapeHtml(desc)}</div>
                ${verifyCmd}
                ${criteriaHtml}
                <div class="babysit-task-meta">
                    <span><i class="fas fa-fingerprint"></i> ${escapeHtml(t.task_id || '')}</span>
                    ${t.expected_behavior?.timeout_seconds ? `<span><i class="fas fa-stopwatch"></i> ${escapeHtml(String(t.expected_behavior.timeout_seconds))}s timeout</span>` : ''}
                </div>
            </div>
        `;
    }).join('');
}

function getToolMeta(fnName) {
    if (fnName === 'python_eval') {
        return {
            icon: 'fab fa-python',
            badgeClass: 'tool-badge-python',
            label: 'python_eval'
        };
    }
    if (fnName === 'run_command' || fnName === 'execute_command') {
        return {
            icon: 'fas fa-terminal',
            badgeClass: 'tool-badge-command',
            label: fnName
        };
    }
    if (fnName === 'list_dir' || fnName === 'read_file' || fnName === 'write_file') {
        return {
            icon: 'fas fa-folder-open',
            badgeClass: 'tool-badge-fs',
            label: fnName
        };
    }
    return {
        icon: 'fas fa-wrench',
        badgeClass: 'tool-badge-default',
        label: fnName || 'tool'
    };
}

function renderTrace(traceEvents) {
    const container = document.getElementById('bb-trace-log');
    if (!container) return;

    if (!traceEvents || traceEvents.length === 0) {
        container.innerHTML = '<div class="babysit-empty-state"><i class="fas fa-terminal"></i><p>Žiadne trace udalosti pre tento experiment.</p></div>';
        return;
    }

    container.innerHTML = traceEvents.slice().reverse().map(e => {
        if (Array.isArray(e.turns)) {
            const taskId = e.task_id || 'Task Trace';
            const totalTime = e.total_time_seconds ? `${Number(e.total_time_seconds).toFixed(2)}s` : '';
            const turnsHtml = e.turns.map(t => {
                const turnNum = t.turn ?? 1;
                const lat = t.latency_seconds ? `${Number(t.latency_seconds).toFixed(2)}s` : '';
                const choice = t.response?.choices?.[0]?.message;
                const toolCalls = choice?.tool_calls || [];
                const finishReason = t.response?.choices?.[0]?.finish_reason || 'stop';
                const status = t.proxy_telemetry?.status || (t.response?.error ? 422 : 200);
                const isError = !String(status).startsWith('2');
                const hasTools = toolCalls.length > 0;

                let cardClass = 'turn-tool-use';
                if (isError) cardClass = 'turn-error';
                else if (finishReason === 'stop') cardClass = 'turn-completed';

                const statusBadge = `<span class="turn-status-badge ${isError ? 'status-err' : 'status-2xx'}"><i class="fas ${isError ? 'fa-exclamation-triangle' : 'fa-check'}"></i> ${escapeHtml(String(status))} OK</span>`;

                const finishBadge = finishReason === 'tool_calls'
                    ? `<span class="turn-finish-badge finish-tool-calls"><i class="fas fa-wrench"></i> tool_calls</span>`
                    : `<span class="turn-finish-badge finish-stop"><i class="fas fa-check-circle"></i> ${escapeHtml(finishReason)}</span>`;

                const latencyBadge = lat ? `<span class="turn-latency-badge"><i class="fas fa-stopwatch"></i> ${escapeHtml(lat)}</span>` : '';

                let thoughtHtml = '';
                if (choice?.content) {
                    thoughtHtml = `
                        <div class="babysit-trace-thought-box">
                            <div class="babysit-trace-subhead"><i class="fas fa-brain"></i> Model Response & Intent</div>
                            <div>${escapeHtml(choice.content)}</div>
                        </div>
                    `;
                }

                let toolCallsHtml = '';
                if (hasTools) {
                    const toolsList = toolCalls.map(tc => {
                        const fn = tc.function?.name || 'tool';
                        const meta = getToolMeta(fn);
                        const args = tc.function?.arguments || '{}';
                        let formattedArgs = args;
                        try {
                            const parsed = JSON.parse(args);
                            formattedArgs = JSON.stringify(parsed, null, 2);
                        } catch (_) {}

                        return `
                            <div class="babysit-tool-call-block">
                                <span class="babysit-tool-fn-badge ${meta.badgeClass}"><i class="${meta.icon}"></i> ${escapeHtml(fn)}</span>
                                <pre class="babysit-tool-args-code">${escapeHtml(formattedArgs)}</pre>
                            </div>
                        `;
                    }).join('');

                    toolCallsHtml = `
                        <div class="babysit-trace-tool-calls">
                            <div class="babysit-trace-subhead"><i class="fas fa-terminal"></i> Volané nástroje (${escapeHtml(String(toolCalls.length))})</div>
                            ${toolsList}
                        </div>
                    `;
                }

                let errorHtml = '';
                if (t.response?.error?.message) {
                    errorHtml = `
                        <div style="font-size:0.85rem; color:var(--danger-color); padding:0.5rem; background:rgba(239,68,68,0.1); border-radius:4px;">
                            <i class="fas fa-exclamation-circle"></i> <strong>Chyba:</strong> ${escapeHtml(t.response.error.message)}
                        </div>
                    `;
                }

                return `
                    <div class="babysit-turn-card ${cardClass}">
                        <div class="babysit-turn-header">
                            <div class="babysit-turn-meta-left">
                                <span class="turn-badge-pill"><i class="fas fa-layer-group"></i> Turn ${escapeHtml(String(turnNum))}</span>
                                ${latencyBadge}
                                ${finishBadge}
                            </div>
                            ${statusBadge}
                        </div>
                        ${thoughtHtml}
                        ${toolCallsHtml}
                        ${errorHtml}
                    </div>
                `;
            }).join('');

            return `
                <div class="babysit-trace-task-card">
                    <div class="babysit-trace-task-header">
                        <div class="babysit-trace-task-title">
                            <i class="fas fa-code-branch" style="color:var(--primary-color);"></i>
                            <strong>${escapeHtml(taskId)}</strong>
                            <span class="inspector-badge badge-status-default" style="font-size:0.75rem;">${escapeHtml(String(e.turns.length))} turn${e.turns.length > 1 ? 'y' : ''}</span>
                            ${totalTime ? `<span class="turn-latency-badge" style="font-size:0.75rem;"><i class="fas fa-clock"></i> ${escapeHtml(totalTime)}</span>` : ''}
                        </div>
                        <span style="font-size:0.75rem; color:var(--text-tertiary); font-family:monospace;">${escapeHtml(e.run_id || '')}</span>
                    </div>
                    <div class="babysit-trace-turns-container">
                        ${turnsHtml}
                    </div>
                </div>
            `;
        }

        const stepNum = e.step ?? e.index ?? '-';
        const timeStr = e.timestamp || e.ts || '';
        const thought = e.thought ? `<div style="color:var(--text-secondary); margin-bottom:0.35rem;"><em>${escapeHtml(e.thought)}</em></div>` : '';
        const toolStr = e.tool ? `<span class="babysit-tool-fn-badge tool-badge-command"><i class="fas fa-wrench"></i> ${escapeHtml(e.tool)}</span>` : '';
        const payload = e.input || e.args || e.result || e.output;
        const codePayload = payload ? `<pre class="babysit-tool-args-code" style="max-height:160px;">${escapeHtml(typeof payload === 'object' ? JSON.stringify(payload, null, 2) : String(payload))}</pre>` : '';

        return `
            <div class="babysit-turn-card">
                <div class="babysit-turn-header">
                    <span><strong>Krok ${escapeHtml(String(stepNum))}</strong> ${toolStr}</span>
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
        container.innerHTML = '<div class="babysit-empty-state"><i class="fas fa-shield-alt"></i><p>Žiadne nálezy monitora pre tento beh.</p></div>';
        return;
    }

    container.innerHTML = findings.slice().reverse().map(f => {
        const sev = String(f.severity || 'INFO').toUpperCase();
        const severityCardClass = ['CRITICAL', 'ERROR', 'HIGH', 'WARNING', 'MEDIUM', 'LOW', 'INFO'].includes(sev)
            ? sev
            : 'INFO';
        const sevClass = (sev === 'CRITICAL' || sev === 'ERROR') ? 'badge-status-5xx' : (sev === 'WARNING' ? 'badge-status-4xx' : 'badge-status-2xx');
        const title = f.summary || f.title || f.category || 'Pozorovanie';
        const desc = (f.description || f.message || '');
        const descHtml = (desc && desc !== title) ? `<div class="babysit-finding-desc" style="margin-top:0.35rem; font-size:0.85rem;">${escapeHtml(desc)}</div>` : '';
        const categoryBadge = f.category ? `<span class="babysit-trace-tool-badge" style="font-size:0.75rem;">${escapeHtml(f.category)}</span>` : '';
        const findingId = f.finding_id ? `<span style="font-size:0.75rem; color:var(--text-secondary); margin-left:0.5rem;">${escapeHtml(f.finding_id)}</span>` : '';
        const taskId = f.task_id ? `<span style="font-size:0.75rem; color:var(--text-secondary);"><i class="fas fa-tag"></i> ${escapeHtml(f.task_id)}</span>` : '';
        const confBadge = (typeof f.confidence === 'number') ? `<span style="font-size:0.75rem; color:var(--text-secondary); margin-left:0.35rem;">(${escapeHtml(String(Math.round(f.confidence * 100)))}% conf)</span>` : '';

        let fixHtml = '';
        const proposedFix = f.proposed_fix || f.recommendation || f.fix;
        if (proposedFix) {
            fixHtml = `
                <div class="babysit-finding-rec">
                    <strong><i class="fas fa-wrench"></i> Návrh opravy:</strong> ${escapeHtml(proposedFix)}
                </div>
            `;
        }

        let rootCauseHtml = '';
        if (f.root_cause_hypothesis) {
            rootCauseHtml = `
                <div style="font-size:0.8rem; color:var(--text-secondary); margin-top:0.25rem;">
                    <strong><i class="fas fa-lightbulb"></i> Root cause:</strong> ${escapeHtml(f.root_cause_hypothesis)}
                </div>
            `;
        }

        let evidenceHtml = '';
        if (f.evidence) {
            let evText = typeof f.evidence === 'object' ? JSON.stringify(f.evidence) : String(f.evidence);
            if (f.evidence.quoted_log) {
                evText = f.evidence.quoted_log;
            }
            evidenceHtml = `
                <div style="margin-top:0.25rem;">
                    <span style="font-size:0.75rem; color:var(--text-tertiary);"><i class="fas fa-quote-left"></i> Dôkaz:</span>
                    <code class="code-inline" style="font-size:0.75rem; display:block; margin-top:0.15rem; word-break:break-all;">${escapeHtml(evText)}</code>
                </div>
            `;
        }

        return `
            <div class="babysit-finding-card severity-${severityCardClass}">
                <div class="babysit-finding-header">
                    <div style="display:flex; align-items:center; flex-wrap:wrap; gap:0.5rem;">
                        <span class="inspector-badge ${sevClass}">${escapeHtml(sev)}</span>
                        ${categoryBadge}
                        ${findingId}
                        ${confBadge}
                    </div>
                    ${taskId}
                </div>
                <div class="babysit-finding-title">${escapeHtml(title)}</div>
                ${descHtml}
                ${rootCauseHtml}
                ${evidenceHtml}
                ${fixHtml}
            </div>
        `;
    }).join('');
}

async function handleKillSwitchToggle(e) {
    const btn = e.currentTarget;
    const action = btn.dataset.action;

    if (!action || action === 'none') {
        showToast('Tento experiment je už dokončený', 'info');
        return;
    }

    const isStopping = action === 'stop';
    const confirmMsg = isStopping
        ? 'Naozaj chceš aktivovať núdzový Kill-Switch a zastaviť bežiaci babysit experiment?'
        : 'Chceš uvoľniť Kill-Switch a obnoviť povolenie pre beh experimentov?';

    const confirmed = await showConfirm(confirmMsg, isStopping ? 'Aktivovať Kill-Switch' : 'Uvoľniť Kill-Switch');
    if (!confirmed) return;

    btn.disabled = true;
    try {
        const payload = {
            run_id: currentRunId || null,
            action: isStopping ? 'stop' : 'resume'
        };
        const resp = await apiCall('POST', '/admin/babysit/kill-switch', payload);
        if (resp.status === 'ok') {
            showToast(isStopping ? 'Kill-Switch aktivovaný: STOP_BABYSIT vytvorený' : 'Kill-Switch uvoľnený', isStopping ? 'warning' : 'success');
            if (currentRunId) {
                await loadRunDetails(currentRunId);
            }
        } else {
            showToast(resp.message || 'Chyba kill-switchu', 'error');
        }
    } catch (err) {
        showToast(`Zlyhalo volanie kill-switchu: ${err.message}`, 'error');
    } finally {
        btn.disabled = false;
    }
}

function renderEmptyRun() {
    renderKPIs({ tasks: [], findings: [], trace: [] });
    renderTasks([]);
    renderTrace([]);
    renderFindings([]);
}
