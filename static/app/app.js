/**
 * Gemini2API Admin Panel - Main Application Entry
 */

import { initializeComponents } from './component-loader.js';
import { initThemeSwitcher } from './theme-switcher.js';
import { initAuth, apiCall, logout } from './auth.js';
import { showToast, formatNumber, getStatusBadge, maskString, copyToClipboard, showConfirm, formatDate } from './utils.js';
import { initUsageStats, loadUsageStats } from './usage-chart.js';
import { initLogs, loadLogs, setLogsPollingActive } from './logs.js';
import { initSettings, loadSettings } from './settings.js';
import { initApiKeys, loadApiKeys } from './api-keys.js';
import { initGems, loadAccountsThenGems } from './gems.js';
import { initBabysit, loadBabysit, setBabysitPollingActive } from './babysit.js';
import { initI18n, t } from './i18n.js';
import { initLanguageSwitcher } from './language-switcher.js';
import { APP_CONSTANTS } from './config/constants.js';
import { safeStorage } from './services/storageService.js';
import { validateAccountPayload } from './services/validationService.js';
import { accountService } from './services/accountService.js';
import { telemetryService } from './services/telemetryService.js';
import { CanvasEngine } from './canvas-engine.js';
import { openPayloadInspector } from './payload-inspector.js';

let isAppInitialized = false;

const elements = {
    navItems: null,
    sections: null
};

function initElements() {
    elements.navItems = document.querySelectorAll('.nav-item');
    elements.sections = document.querySelectorAll('.section');
}

function initNavigation() {
    if (!elements.navItems || !elements.sections) return;

    elements.navItems.forEach(item => {
        item.addEventListener('click', (e) => {
            e.preventDefault();
            const sectionId = item.dataset.section;
            activateSection(sectionId);
        });
    });

    window.addEventListener('hashchange', () => {
        const sectionId = window.location.hash.slice(1) || 'dashboard';
        activateSection(sectionId, false);
    });

    const initialSectionId = window.location.hash.slice(1) || 'dashboard';
    activateSection(initialSectionId, false);
}

function activateSection(sectionId, updateHash = true) {
    elements.navItems.forEach(nav => {
        nav.classList.remove('active');
        if (nav.dataset.section === sectionId) {
            nav.classList.add('active');
        }
    });

    elements.sections.forEach(section => {
        section.classList.remove('active');
        if (section.id === sectionId) {
            section.classList.add('active');
        }
    });

    if (updateHash) {
        window.location.hash = sectionId;
    }

    // A single-page navigation must also be a polling lifecycle boundary.  Stop every
    // hidden section first; each activation function is idempotent, so hashchange + click
    // cannot stack intervals.
    setLogsPollingActive(sectionId === 'logs');
    setBabysitPollingActive(sectionId === 'babysit');
    if (sectionId === 'dashboard') {
        startDashboardAutoRefresh();
    } else {
        stopDashboardAutoRefresh();
        stopUptimeTimer();
    }

    loadSectionData(sectionId);
    window.scrollTo(0, 0);
    focusSectionHeading(sectionId);
}

// Move the keyboard focus to the heading of the section that was just opened, so
// a screen-reader or keyboard user continues inside the new view instead of at the
// top of the navigation.
function focusSectionHeading(sectionId) {
    const section = document.getElementById(sectionId);
    if (!section) return;
    const heading = section.querySelector('h2, h3');
    if (!heading) return;
    if (!heading.hasAttribute('tabindex')) {
        heading.setAttribute('tabindex', '-1');
    }
    try {
        heading.focus({ preventScroll: true });
    } catch (error) {
        heading.focus();
    }
}

function renderSectionErrorBoundary(sectionId, error) {
    const sectionEl = document.getElementById(sectionId);
    if (!sectionEl) return;
    sectionEl.querySelectorAll('.section-error-boundary-banner').forEach(el => el.remove());

    const banner = document.createElement('div');
    banner.className = 'section-error-boundary-banner';
    banner.style.cssText = 'padding: 2rem; margin: 1.5rem 0; border-radius: 12px; background: rgba(239, 68, 68, 0.08); border: 1px solid var(--danger-color, #ef4444); color: var(--text-primary); text-align: center;';
    banner.innerHTML = `
        <i class="fas fa-exclamation-triangle" style="color: var(--danger-color, #ef4444); font-size: 2rem; margin-bottom: 0.75rem;" aria-hidden="true"></i>
        <h3 style="margin: 0.25rem 0 0.5rem; font-size: 1.125rem;">Section Failed to Load (${escapeHtml(sectionId)})</h3>
        <p style="font-size: 0.875rem; color: var(--text-secondary); margin: 0 0 1.25rem; max-width: 450px; display: inline-block;">${escapeHtml(error.message || 'An unexpected error occurred while loading this section. Other sections remain fully operational.')}</p>
        <div>
            <button class="btn btn-sm btn-primary retry-btn">
                <i class="fas fa-redo"></i> Retry Section
            </button>
        </div>
    `;
    banner.querySelector('.retry-btn')?.addEventListener('click', () => {
        banner.remove();
        loadSectionData(sectionId);
    });
    sectionEl.prepend(banner);
}

async function loadSectionData(sectionId) {
    const sectionEl = document.getElementById(sectionId);
    if (sectionEl) {
        sectionEl.querySelectorAll('.section-error-boundary-banner').forEach(el => el.remove());
    }
    try {
        switch (sectionId) {
            case 'dashboard':
                await loadDashboard();
                break;
            case 'accounts':
                await loadAccounts();
                break;
            case 'usage-stats':
                await loadUsageStats();
                break;
            case 'logs':
                await loadLogs();
                break;
            case 'settings':
                await loadSettings();
                if (typeof loadConfigReadiness === 'function') {
                    loadConfigReadiness();
                }
                break;
            case 'api-keys':
                await loadApiKeys();
                break;
            case 'gems':
                initGems();
                await loadAccountsThenGems();
                break;
            case 'playground':
                await updatePlaygroundModels();
                break;
            case 'babysit':
                await loadBabysit();
                break;
        }
    } catch (error) {
        renderSectionErrorBoundary(sectionId, error);
    }
}

// ============================================================================
// Dashboard
// ============================================================================

// Pure: reduce the two settled dashboard requests to a plain view-model (never throws).
function settleDashboardResults(statusRes, sysRes) {
    const errorOf = (r) => (r.status === 'rejected')
        ? ((r.reason && r.reason.message) ? String(r.reason.message) : 'Unknown error')
        : '';
    return {
        status: statusRes.status === 'fulfilled' ? statusRes.value : null,
        statusError: errorOf(statusRes),
        sys: sysRes.status === 'fulfilled' ? sysRes.value : null,
        sysError: errorOf(sysRes),
    };
}

// Pure: should the periodic dashboard refresh run right now?
function shouldAutoRefresh(s) {
    if (!s) return false;
    return s.activeSection === 'dashboard' && !s.hidden && !s.inFlight && !s.paused
        && (s.now - s.lastLoadAt) >= s.intervalMs;
}

// Is the periodic dashboard refresh paused by the operator? The choice is stored
// so it survives a reload.
function isDashboardRefreshPaused() {
    try {
        return safeStorage.getItem(APP_CONSTANTS.STORAGE_KEYS.DASHBOARD_REFRESH_PAUSED) === '1';
    } catch (error) {
        return false;
    }
}

// Pause or resume the periodic dashboard refresh and remember the choice.
function setDashboardRefreshPaused(paused) {
    try {
        if (paused) {
            safeStorage.setItem(APP_CONSTANTS.STORAGE_KEYS.DASHBOARD_REFRESH_PAUSED, '1');
        } else {
            safeStorage.removeItem(APP_CONSTANTS.STORAGE_KEYS.DASHBOARD_REFRESH_PAUSED);
        }
    } catch (error) {
        // A storage failure must not break the toggle itself.
    }
    updateRefreshToggleButton();
}

// Keep the toggle button in sync with the current state (label + aria-pressed).
function updateRefreshToggleButton() {
    const btn = document.getElementById('dashboardRefreshToggleBtn');
    if (!btn) return;
    const paused = isDashboardRefreshPaused();
    btn.setAttribute('aria-pressed', paused ? 'true' : 'false');
    const label = btn.querySelector('span[data-i18n]');
    if (label) {
        label.textContent = paused ? t('dashboard.resumeRefresh') : t('dashboard.pauseRefresh');
    }
    btn.setAttribute('title', paused ? t('dashboard.resumeRefresh') : t('dashboard.pauseRefresh'));
}

// Toggle the periodic dashboard refresh.
function toggleDashboardRefresh() {
    setDashboardRefreshPaused(!isDashboardRefreshPaused());
}

// Per-card error notice: shown when only one of the dashboard requests failed, so the other
// card group keeps working. An empty message removes the notice.
function setDashboardCardError(key, message) {
    const section = document.getElementById('dashboard');
    if (!section) return;
    const id = 'dashboard-card-error-' + key;
    const old = document.getElementById(id);
    if (old) old.remove();
    if (!message) return;
    const el = document.createElement('div');
    el.id = id;
    el.className = 'dashboard-card-error';
    el.setAttribute('role', 'alert');
    const text = document.createElement('span');
    text.textContent = t('dashboard.error.' + key) + ': ' + message;
    const btn = document.createElement('button');
    btn.className = 'btn btn-sm';
    btn.textContent = t('dashboard.retry');
    btn.addEventListener('click', () => loadSectionData('dashboard'));
    el.append(text, btn);
    section.prepend(el);
}

let _dashboardLoading = false;
let _dashboardLastLoadAt = 0;

async function loadDashboard(opts) {
    const background = !!(opts && opts.background === true);
    _dashboardLoading = true;
    try {
        // The requests are independent: run them in parallel, and let one failing leave the
        // other card group intact.
        const [statusRes, sysRes] = await Promise.allSettled([
            telemetryService.getAdminStatus(),
            telemetryService.getSystemInfo(),
        ]);
        const view = settleDashboardResults(statusRes, sysRes);
        if (!view.status && !view.sys && !background) {
            // Nothing could be loaded: fall through to the section error boundary (with retry).
            throw new Error(view.statusError);
        }
        setDashboardCardError('status', view.statusError);
        setDashboardCardError('system', view.sysError);

        if (view.status) {
            const data = view.status;
            const accounts = data.accounts || [];

            const activeCount = accounts.filter(a => a.status === 'active').length;
            const totalRequests = accounts.reduce((sum, a) => sum + (a.request_count || 0), 0);
            const modelsSet = new Set();
            accounts.forEach(a => {
                if (a.models && Array.isArray(a.models)) {
                    a.models.forEach(m => modelsSet.add(m));
                }
            });

            setText('stat-total', formatNumber(accounts.length));
            setText('stat-active', formatNumber(activeCount));
            setText('stat-requests', formatNumber(totalRequests));
            setText('stat-models', formatNumber(modelsSet.size || accounts.reduce((s, a) => s + (a.models_count || 0), 0)));

            const strategyMap = { 'round-robin': t('settings.strategy.roundRobin'), 'least-used': t('settings.strategy.leastUsed'), 'failover': t('settings.strategy.failover') };
            setText('info-strategy', strategyMap[data.strategy] || data.strategy);
            setText('info-concurrent', data.max_concurrent_per_account || '-');

            renderAccountStatusGrid(accounts, data);
            renderModelsList(modelsSet);
            updatePlaygroundModels(modelsSet);
            if (typeof renderDebugMetrics === 'function') renderDebugMetrics(data.debug_metrics);
            if (typeof renderSloOverview === 'function') renderSloOverview(data.slo);
        }
        if (typeof loadRequestExplorerData === 'function') {
            // Fire and forget: a failing explorer must not fail the dashboard.
            loadRequestExplorerData();
        }
        if (view.sys) applySystemInfo(view.sys);

        // Check for an update once, on the first dashboard load. The click handler is bound in
        // initEventListeners; binding it here as well made every click run it twice.
        const updateBtn = document.getElementById('checkUpdateBtn');
        if (updateBtn && !updateBtn._autoChecked) {
            updateBtn._autoChecked = true;
            checkForUpdate();
        }
        const debugRefresh = document.getElementById('debugMetricsRefreshBtn');
        if (debugRefresh && !debugRefresh._bound) {
            debugRefresh._bound = true;
            debugRefresh.addEventListener('click', () => loadDashboard({ background: true }));
        }
    } finally {
        _dashboardLoading = false;
        _dashboardLastLoadAt = Date.now();
    }
}

const DASHBOARD_REFRESH_MS = 30000;
const DASHBOARD_REFRESH_CHECK_MS = 5000;
let _dashboardRefreshTimer = null;

// Background refresh: runs only while the dashboard is the active section and the browser tab is
// visible. A tab that was hidden catches up within one check interval after it becomes visible.
function startDashboardAutoRefresh() {
    if (_dashboardRefreshTimer) return;
    _dashboardRefreshTimer = setInterval(() => {
        const active = document.querySelector('.section.active');
        if (shouldAutoRefresh({
            activeSection: active ? active.id : '',
            hidden: document.hidden,
            inFlight: _dashboardLoading,
            paused: isDashboardRefreshPaused(),
            lastLoadAt: _dashboardLastLoadAt,
            now: Date.now(),
            intervalMs: DASHBOARD_REFRESH_MS,
        })) {
            loadDashboard({ background: true }).catch(() => {});
        }
    }, DASHBOARD_REFRESH_CHECK_MS);
}

function stopDashboardAutoRefresh() {
    if (_dashboardRefreshTimer) {
        clearInterval(_dashboardRefreshTimer);
        _dashboardRefreshTimer = null;
    }
}

let uptimeInterval = null;

function stopUptimeTimer() {
    if (uptimeInterval) {
        clearInterval(uptimeInterval);
        uptimeInterval = null;
    }
}

function startUptimeTimer(initialSeconds) {
    stopUptimeTimer();
    let seconds = initialSeconds;
    function update() {
        const d = Math.floor(seconds / 86400);
        const h = Math.floor((seconds % 86400) / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        const s = seconds % 60;
        setText('uptime-display', d + t('time.days') + ' ' + h + t('time.hours') + ' ' + m + t('time.minutes') + ' ' + s + t('time.seconds'));
        seconds++;
    }
    update();
    uptimeInterval = setInterval(update, 1000);
}

// Restart helpers. Kept small and dependency-free so the node tests can run them as-is.
// Hide the button only when the backend says restart is unsupported (older backends keep it).
function restartButtonVisible(sysInfo) {
    return !(sysInfo && sysInfo.restart_supported === false);
}

// Run the restart request; never throws. On failure carries the real error message.
async function requestRestart(call) {
    try {
        await call();
        return { ok: true, message: '' };
    } catch (e) {
        return { ok: false, message: (e && e.message) ? String(e.message) : '' };
    }
}

function applySystemInfo(data) {
    setText('sys-version', 'v' + data.version);
    setText('sys-python', data.python_version);
    setText('sys-time', data.server_time);
    setText('sys-os', data.os);
    setText('sys-memory', data.memory_usage + ' MB / ' + data.memory_total + ' MB');
    setText('sys-cpu', data.cpu_percent + '%');
    setText('sys-mode', data.run_mode);
    const restartBtnEl = document.getElementById('restartBtn');
    if (restartBtnEl) restartBtnEl.style.display = restartButtonVisible(data) ? '' : 'none';
    if (data.uptime_seconds !== undefined) {
        const dashboard = document.getElementById('dashboard');
        // Real section elements always have classList; the fallback keeps compatibility with
        // lightweight embedded/test DOMs while hidden real sections remain stopped.
        if (!dashboard || !dashboard.classList || dashboard.classList.contains('active')) {
            startUptimeTimer(data.uptime_seconds);
        }
    }
}

// Health badge on account card (pure function).
// Exposes real session health and consecutive failures to make degraded accounts visible.
// Returns null when healthy with no failures to avoid cluttering cards.
function _accountHealthNote(account) {
    if (!account) return null;
    if (account.is_healthy === false) {
        // Session unhealthy while status still marked active
        return {
            cls: account.status === 'active' ? 'text-danger' : 'text-muted',
            text: account.status === 'active'
                ? t('accounts.healthMismatch')
                : t('accounts.sessionUnhealthy')
        };
    }
    const streak = Number(account.consecutive_failures) || 0;
    if (streak > 0) {
        return { cls: 'text-warning', text: `${t('accounts.consecutiveFailures')}: ${streak}` };
    }
    return null;
}

// Aligned with app/core/account_pool.EMPTY_STREAK_THRESHOLD: streak required before marking broken
const EMPTY_STREAK_THRESHOLD = 3;

// Empty response indicator (pure function).
// Tracks upstream HTTP 200 responses with empty bodies that would otherwise pass unnoticed.
// Returns null if account has never emitted an empty response.
function _accountEmptyNote(account) {
    const total = Number(account?.empty_count) || 0;
    if (total <= 0) return null;
    const streak = Number(account.consecutive_empty) || 0;
    return {
        cls: streak >= EMPTY_STREAK_THRESHOLD ? 'text-danger' : 'text-warning',
        text: streak > 0 ? `${total} (${t('accounts.consecutiveNow')} ${streak})` : String(total)
    };
}

// Render empty response detail row; returns empty string if no warning.
function _accountEmptyRow(account) {
    const note = _accountEmptyNote(account);
    if (!note) return '';
    return `
            <div class="account-detail">
                <span class="label">${t('accounts.emptyResponses')}</span>
                <span class="value ${note.cls}">${escapeHtml(note.text)}</span>
            </div>`;
}

// Display text for recent error: error summary + timestamp.
// Renders last_error_at from get_status() to differentiate stale errors from current issues.
function _accountLastErrorText(account) {
    const msg = escapeHtml(String(account?.last_error ?? ''));
    if (!account?.last_error_at) return msg;
    return `${msg} (${escapeHtml(formatDate(account.last_error_at))})`;
}

// Render health note row; returns empty string if healthy. Values are escaped via escapeHtml.
function _accountHealthRow(account) {
    const note = _accountHealthNote(account);
    if (!note) return '';
    return `
            <div class="account-detail">
                <span class="label">${t('accounts.health')}</span>
                <span class="value ${note.cls}">${escapeHtml(note.text)}</span>
            </div>`;
}

// Render quota / cooldown status row for accounts page.  Every dynamic value is escaped
// because this string is inserted with innerHTML by both account-card renderers.
function _accountQuotaRow(account) {
    if (!account) return '';
    const cooldown = Math.max(0, Number(account.cooldown_remaining_s) || 0);
    const isCooling = account.cooling_down || cooldown > 0;
    const activeReqs = Math.max(0, Number(account.active_requests) || 0);

    let badgeClass;
    let iconClass;
    let badgeText;
    if (isCooling) {
        badgeClass = 'badge-warning';
        iconClass = 'fa-hourglass-half';
        badgeText = `${t('accounts.quotaCooldown')} (${Math.round(cooldown)}s)`;
    } else if (account.is_healthy === false || account.status === 'expired') {
        badgeClass = 'badge-danger';
        iconClass = 'fa-exclamation-triangle';
        badgeText = t('accounts.quotaSessionExpired');
    } else if (account.status === 'disabled') {
        badgeClass = 'badge-secondary';
        iconClass = 'fa-ban';
        badgeText = t('accounts.quotaDisabled');
    } else {
        badgeClass = 'badge-success';
        iconClass = 'fa-check-circle';
        badgeText = `${t('accounts.quotaAvailable')} (${activeReqs} ${t('accounts.quotaActive')})`;
    }

    const policyHint = t('accounts.quotaPolicyHint');
    return `
            <div class="account-detail account-detail--quota">
                <span class="label">${escapeHtml(t('accounts.quotaStatus'))}</span>
                <span class="value"><span class="badge account-quota-badge ${badgeClass}" role="status"><i class="fas ${iconClass}" aria-hidden="true"></i> ${escapeHtml(badgeText)}</span></span>
            </div>
            <div class="account-detail account-detail--quota-policy">
                <span class="label">${escapeHtml(t('accounts.quotaPolicy'))}</span>
                <span class="value text-muted account-quota-policy" title="${escapeAttr(policyHint)}">
                    ${escapeHtml(t('accounts.quotaWebSession'))} <i class="fas fa-info-circle" aria-hidden="true"></i>
                </span>
            </div>`;
}

// Operational view of one account: slots in use, cooldown, session health.
// Returns the extra rows for the account card (never the raw account id).
function _accountOperationsRows(account, maxConcurrent) {
    if (!account) return '';
    const limit = Number(maxConcurrent) || 0;
    const active = Number(account.active_requests) || 0;
    const cooldown = Number(account.cooldown_remaining_s) || 0;
    const rows = [];
    if (limit > 0) {
        rows.push(`<div class="account-detail">
            <span class="label">${escapeHtml(t('accounts.slotsInUse'))}</span>
            <span class="value">${escapeHtml(String(active))} / ${escapeHtml(String(limit))}</span>
        </div>`);
    }
    if (cooldown > 0) {
        rows.push(`<div class="account-detail">
            <span class="label">${escapeHtml(t('accounts.cooldownRemaining'))}</span>
            <span class="value">${escapeHtml(String(Math.round(cooldown)))} s</span>
        </div>`);
    }
    if (account.is_healthy === false) {
        rows.push(`<div class="account-detail">
            <span class="label">${escapeHtml(t('accounts.sessionHealth'))}</span>
            <span class="value text-danger">${escapeHtml(t('accounts.sessionUnhealthy'))}</span>
        </div>`);
    }
    return rows.join('');
}

// Confirmation-protected actions for one account. The account id never reaches
// the visible text: it only travels in the data attribute the click handler uses.
function _accountActionButtons(account) {
    if (!account || !account.id) return '';
    const disabled = account.status === 'disabled';
    return `<div class="account-actions">
        <button type="button" class="btn btn-sm btn-outline" data-account-action="check" data-account-id="${escapeAttr(account.id)}">${escapeHtml(t('accounts.actionCheck'))}</button>
        <button type="button" class="btn btn-sm btn-outline" data-account-action="toggle" data-account-enabled="${disabled ? 'false' : 'true'}" data-account-id="${escapeAttr(account.id)}">${escapeHtml(disabled ? t('accounts.actionEnable') : t('accounts.actionDisable'))}</button>
    </div>`;
}

function renderAccountStatusGrid(accounts, pool) {
    const container = document.getElementById('accountStatusGrid');
    if (!container) return;

    if (accounts.length === 0) {
        container.innerHTML = `<div class="empty-state"><i class="fas fa-server"></i> ${escapeHtml(t('accounts.noAccounts'))}</div>`;
        return;
    }

    const maxConcurrent = (pool && pool.max_concurrent_per_account) || 0;

    // Security: escape account.label before rendering to prevent stored XSS
    container.innerHTML = accounts.map(account => `
        <div class="provider-card">
            <div class="provider-card-header">
                <h4>${escapeHtml(account.label || account.id)}</h4>
                ${getStatusBadge(account.status)}
            </div>
            <div class="account-detail">
                <span class="label">${t('accounts.requests')}</span>
                <span class="value">${formatNumber(account.request_count || 0)}</span>
            </div>
            <div class="account-detail">
                <span class="label">${t('accounts.errors')}</span>
                <span class="value">${formatNumber(account.error_count || 0)}</span>
            </div>
            <div class="account-detail">
                <span class="label">${t('accounts.concurrency')}</span>
                <span class="value">${account.active_requests || 0}</span>
            </div>
            <div class="account-detail">
                <span class="label">${t('accounts.models')}</span>
                <span class="value">${account.models_count || 0}</span>
            </div>
            <div class="account-detail">
                <span class="label">${t('accounts.lastSuccess')}</span>
                <span class="value">${escapeHtml(formatDate(account.last_success_at))}</span>
            </div>${_accountOperationsRows(account, maxConcurrent)}${_accountHealthRow(account)}${_accountEmptyRow(account)}
            ${_accountActionButtons(account)}
        </div>
    `).join('');

    container.querySelectorAll('[data-account-action]').forEach(button => {
        button.addEventListener('click', () => handleAccountAction(
            button.dataset.accountAction, button.dataset.accountId, button));
    });
}

// Run one account action after the operator confirmed it.
async function handleAccountAction(action, accountId, button) {
    if (!action || !accountId) return;
    if (action === 'check') {
        try {
            await apiCall('GET', `${APP_CONSTANTS.ROUTES.ADMIN_ACCOUNTS}/${encodeURIComponent(accountId)}/check`);
        } catch (error) {
            showToast(String(error && error.message ? error.message : error), 'error');
        }
        loadDashboard({ background: true });
        return;
    }
    if (action === 'toggle') {
        // The current state travels in the data attribute of the clicked button,
        // so no account id ever has to be embedded into a CSS selector.
        const currentlyEnabled = !button || button.getAttribute('data-account-enabled') !== 'false';
        const confirmed = await showConfirm({
            title: t('accounts.actionToggleTitle'),
            message: t('accounts.actionToggleMessage'),
            confirmText: t('accounts.actionToggleConfirm'),
            type: 'warning'
        });
        if (!confirmed) return;
        try {
            await apiCall('POST', `${APP_CONSTANTS.ROUTES.ADMIN_ACCOUNTS}/${encodeURIComponent(accountId)}/status`,
                { enabled: !currentlyEnabled });
        } catch (error) {
            showToast(String(error && error.message ? error.message : error), 'error');
        }
        loadDashboard({ background: true });
    }
}

function renderModelsList(modelsSet) {
    const container = document.getElementById('modelsList');
    if (!container) return;

    const models = Array.from(modelsSet).sort();
    if (models.length === 0) {
        container.innerHTML = `<span class="text-muted">${t('accounts.noModels')}</span>`;
        return;
    }

    // Security: use data-model attribute + addEventListener instead of inline onclick JS strings;
    // model is escaped via escapeAttr/escapeHtml
    container.innerHTML = models.map(model => `
        <span class="model-tag" data-model="${escapeAttr(model)}">
            <i class="fas fa-cube"></i> ${escapeHtml(model)}
        </span>
    `).join('');
    container.querySelectorAll('.model-tag').forEach(tag => {
        tag.addEventListener('click', () => copyModel(tag.dataset.model));
    });
}

let _lastModelsSet = new Set();

export async function updatePlaygroundModels(modelsSet, preferredModel = null) {
    if (modelsSet && (modelsSet instanceof Set || Array.isArray(modelsSet))) {
        _lastModelsSet = new Set(modelsSet);
    }
    const select = document.getElementById('pg-model');
    if (!select) return;

    let gemMappings = {};
    try {
        const resp = await apiCall('GET', '/admin/gem-mapping');
        if (resp && resp.mappings) {
            gemMappings = resp.mappings;
        }
    } catch (_) {
        // Gem mapping fetch failure shouldn't prevent standard models from rendering
    }

    if (_lastModelsSet.size === 0) {
        try {
            const data = await apiCall('GET', '/v1/models');
            if (data && Array.isArray(data.data)) {
                data.data.forEach(m => {
                    if (m && m.id && !gemMappings[m.id]) {
                        _lastModelsSet.add(m.id);
                    }
                });
            }
        } catch (_) {
            try {
                const status = await telemetryService.getAdminStatus();
                const accounts = status?.accounts || [];
                accounts.forEach(a => {
                    if (a.models && Array.isArray(a.models)) {
                        a.models.forEach(m => _lastModelsSet.add(m));
                    }
                });
            } catch (_) {}
        }
    }

    const currentVal = preferredModel || select.value;
    const baseModels = Array.from(_lastModelsSet || []).filter(m => !gemMappings[m]).sort();
    const gemNames = Object.keys(gemMappings).sort();

    if (baseModels.length === 0 && gemNames.length === 0 && !currentVal) {
        select.innerHTML = `<option value="">${t('accounts.noModels')}</option>`;
        return;
    }

    let html = '';
    if (baseModels.length > 0) {
        html += baseModels.map(model =>
            `<option value="${escapeAttr(model)}">${escapeHtml(model)}</option>`
        ).join('');
    }
    if (gemNames.length > 0) {
        html += gemNames.map(name =>
            `<option value="${escapeAttr(name)}">💎 ${escapeHtml(name)} (Gem)</option>`
        ).join('');
    }

    if (currentVal && !baseModels.includes(currentVal) && !gemNames.includes(currentVal)) {
        const isGem = currentVal.startsWith('gem-') || !!gemMappings[currentVal];
        const label = isGem ? `💎 ${escapeHtml(currentVal)} (Gem)` : escapeHtml(currentVal);
        html += `<option value="${escapeAttr(currentVal)}">${label}</option>`;
    }

    select.innerHTML = html;
    if (currentVal && Array.from(select.options).some(o => o.value === currentVal)) {
        select.value = currentVal;
        select.dispatchEvent(new Event('change'));
    }
}

function renderDebugMetrics(summary) {
    const data = summary && typeof summary === 'object' ? summary : {};
    setText('debug-metric-requests', Number(data.record_count) || 0);
    setText('debug-metric-fallbacks', Number(data.fallback_count) || 0);
    setText('debug-metric-errors', Object.keys(data.error_classes || {}).filter(k => k !== 'ok').length);
    setText('debug-metric-regenerations', Number(data.regenerations) || 0);

    const histogram = document.getElementById('debug-ttft-histogram');
    const buckets = data.ttft_histogram_ms && typeof data.ttft_histogram_ms === 'object'
        ? Object.entries(data.ttft_histogram_ms) : [];
    if (histogram) {
        const max = Math.max(...buckets.map(([, value]) => Number(value) || 0), 1);
        histogram.innerHTML = buckets.length
            ? buckets.map(([label, value]) => {
                const count = Number(value) || 0;
                const height = Math.max(2, Math.round((count / max) * 100));
                // Screen readers get the bucket and its count as one label instead of
                // three disconnected numbers.
                const accessible = label + ': ' + count;
                return `<div class="debug-histogram-item" role="listitem" title="${escapeAttr(String(count))}" aria-label="${escapeAttr(accessible)}">
                    <span>${escapeHtml(String(count))}</span>
                    <span class="debug-histogram-bar" style="height:${height}%" aria-hidden="true"></span>
                    <span>${escapeHtml(label)}</span>
                </div>`;
            }).join('')
            : `<span class="text-muted">${escapeHtml(t('dashboard.debugNoData'))}</span>`;
    }

    const renderBreakdown = (id, values) => {
        const el = document.getElementById(id);
        if (!el) return;
        const entries = values && typeof values === 'object' ? Object.entries(values) : [];
        el.innerHTML = entries.length
            ? entries.map(([label, value]) => `<li><span>${escapeHtml(String(label))}</span><strong>${escapeHtml(String(Number(value) || 0))}</strong></li>`).join('')
            : `<li class="text-muted">${escapeHtml(t('dashboard.debugNoData'))}</li>`;
    };
    renderBreakdown('debug-served-by', data.served_by);
    renderBreakdown('debug-error-classes', data.error_classes);
}

// ============================================================================
// Operational overview (SLO)
// ============================================================================

// Map a backend SLO reason to its translation key. An unknown reason is rendered
// as the raw id instead of a missing-key placeholder.
const SLO_REASON_KEYS = {
    breaker_open: 'dashboard.sloReasonBreaker',
    error_rate: 'dashboard.sloReasonErrorRate',
    fallback_rate: 'dashboard.sloReasonFallbackRate',
    ttft_p95: 'dashboard.sloReasonTtft',
    inter_chunk_gap: 'dashboard.sloReasonGap',
    pool_saturated: 'dashboard.sloReasonSaturated',
    pool_slot_leak: 'dashboard.sloReasonSlotLeak',
    regeneration_budget_exhausted: 'dashboard.sloReasonBudget'
};

// Pure: map the backend SLO state to its label key and CSS modifier. Anything the
// backend does not know is rendered as "unknown" -- never as a green "ok".
function sloStateDescriptor(state) {
    const known = ['ok', 'degraded', 'unknown'];
    const normalized = known.indexOf(state) >= 0 ? state : 'unknown';
    const suffix = normalized.charAt(0).toUpperCase() + normalized.slice(1);
    return {
        state: normalized,
        labelKey: 'dashboard.sloState' + suffix,
        className: 'slo-state slo-state--' + normalized
    };
}

// Pure: a ratio (0..1) as a percentage string.
function formatRatio(ratio) {
    const value = Number(ratio);
    if (!isFinite(value)) return '-';
    return (value * 100).toFixed(1) + '%';
}

// Pure: milliseconds as a short human duration.
function formatDurationMs(value) {
    const ms = Number(value);
    if (!isFinite(ms) || ms <= 0) return '-';
    if (ms < 1000) return Math.round(ms) + ' ms';
    if (ms < 60000) return (ms / 1000).toFixed(1) + ' s';
    return (ms / 60000).toFixed(1) + ' min';
}

// Render a list of label/value pairs into a <dl>.
function renderSloMetrics(id, items) {
    const el = document.getElementById(id);
    if (!el) return;
    el.innerHTML = (items || []).map(item =>
        `<div class="slo-metric"><dt>${escapeHtml(item.label)}</dt><dd>${escapeHtml(item.value)}</dd></div>`
    ).join('');
}

// Reset the Argon stall breaker by hand. Confirmation protected: the next
// request tries Argon again and may wait for the full timeout once more.
async function resetArgonBreaker() {
    const confirmed = await showConfirm({
        title: t('dashboard.breakerResetTitle'),
        message: t('dashboard.breakerResetMessage'),
        confirmText: t('dashboard.breakerResetConfirm'),
        type: 'warning'
    });
    if (!confirmed) return;
    try {
        await apiCall('POST', APP_CONSTANTS.ROUTES.ADMIN_ARGON_RESET);
    } catch (error) {
        showToast(String(error && error.message ? error.message : error), 'error');
    }
    loadDashboard({ background: true });
}

// Render the operational overview: request volume, error/fallback rates, latency
// percentiles, breaker state and account-pool capacity. Bounded and redacted --
// no prompts, no account identifiers, no request bodies.
function renderSloOverview(slo) {
    const data = (slo && typeof slo === 'object') ? slo : {};
    const descriptor = sloStateDescriptor(data.state);

    const chip = document.getElementById('slo-state');
    if (chip) {
        chip.className = descriptor.className;
        chip.textContent = t(descriptor.labelKey);
    }

    const reasons = document.getElementById('slo-reasons');
    if (reasons) {
        const list = Array.isArray(data.reasons) ? data.reasons : [];
        reasons.textContent = list.length
            ? t('dashboard.sloReasonsLabel') + ': ' + list.map(reason => {
                const key = SLO_REASON_KEYS[reason];
                return key ? t(key) : String(reason);
            }).join(', ')
            : '';
    }

    const windows = (data.windows && typeof data.windows === 'object') ? data.windows : {};
    const rows = document.getElementById('slo-window-rows');
    if (rows) {
        const order = ['1h', '24h', '7d'];
        const extra = Object.keys(windows).filter(key => order.indexOf(key) < 0);
        const keys = order.filter(key => windows[key]).concat(extra);
        rows.innerHTML = keys.length
            ? keys.map(key => {
                const window = windows[key] || {};
                return `<tr>
                    <th scope="row">${escapeHtml(key)}</th>
                    <td>${escapeHtml(formatNumber(window.requests || 0))}</td>
                    <td>${escapeHtml(formatRatio(window.error_rate))}</td>
                    <td>${escapeHtml(formatRatio(window.fallback_rate))}</td>
                </tr>`;
            }).join('')
            : `<tr><td colspan="4" class="text-muted">${escapeHtml(t('dashboard.sloNoData'))}</td></tr>`;
    }

    const current = windows['1h'] || {};
    const ttft = current.ttft_ms || {};
    const latency = current.latency_ms || {};
    renderSloMetrics('slo-latency', [
        { label: t('dashboard.sloTtftP50'), value: formatDurationMs(ttft.p50) },
        { label: t('dashboard.sloTtftP95'), value: formatDurationMs(ttft.p95) },
        { label: t('dashboard.sloLatencyP50'), value: formatDurationMs(latency.p50) },
        { label: t('dashboard.sloLatencyP95'), value: formatDurationMs(latency.p95) },
        { label: t('dashboard.sloMaxGap'), value: formatDurationMs(current.max_inter_chunk_gap_ms) }
    ]);

    const pool = data.pool || {};
    const breaker = data.breaker || {};
    const budget = data.budget || {};

    // The breaker reset is only offered while the breaker is actually open.
    const breakerBtn = document.getElementById('argonBreakerResetBtn');
    if (breakerBtn) {
        breakerBtn.hidden = !breaker.open;
    }
    renderSloMetrics('slo-pool', [
        {
            label: t('dashboard.sloBreaker'),
            value: breaker.open ? t('dashboard.sloBreakerOpen') : t('dashboard.sloBreakerClosed')
        },
        {
            label: t('dashboard.sloCapacity'),
            value: formatNumber(pool.active_requests || 0) + ' / ' + formatNumber(pool.capacity || 0)
        },
        { label: t('dashboard.sloSaturation'), value: formatRatio(pool.saturation) },
        { label: t('dashboard.sloCooldown'), value: formatNumber(pool.cooling_accounts || 0) },
        { label: t('dashboard.sloSlotLeak'), value: formatNumber(pool.slot_leak_accounts || 0) },
        {
            label: t('dashboard.sloRegenerations'),
            value: formatNumber(budget.allowed || 0) + ' / ' + formatNumber(budget.blocked || 0)
        }
    ]);
}

// ============================================================================
// Request explorer
// ============================================================================

// The retained diagnostics records the explorer renders (bounded by the backend).
let _explorerRecords = [];
let _explorerSelectedId = '';

// Pure: does a record match the filter text? The filter looks at operational
// fields only -- never at a body, a prompt or an account identifier.
function explorerMatchesFilter(record, query) {
    const needle = String(query || '').trim().toLowerCase();
    if (!needle) return true;
    if (!record || typeof record !== 'object') return false;
    const fields = [
        'request_id', 'model', 'served_by', 'status', 'error_class',
        'fallback_reason', 'parser_failure', 'finish_reason', 'path'
    ];
    return fields.some(key => String(record[key] == null ? '' : record[key]).toLowerCase().indexOf(needle) >= 0);
}

// Pure: the table body for the given records.
function explorerRows(records, query, selectedId) {
    const filtered = (records || []).filter(record => explorerMatchesFilter(record, query));
    if (!filtered.length) {
        return {
            html: `<tr><td colspan="6" class="text-muted">${escapeHtml(t('dashboard.explorerNoData'))}</td></tr>`,
            count: 0
        };
    }
    const rows = filtered.slice(-50).reverse();
    const html = rows.map(record => {
        const id = String(record.request_id || '');
        const selected = id && id === selectedId;
        return `<tr class="explorer-row${selected ? ' explorer-row--selected' : ''}">
            <td>${escapeHtml(formatExplorerTime(record.ingress_ts || record.ts))}</td>
            <td><button type="button" class="explorer-open" data-request-id="${escapeAttr(id)}">${escapeHtml(id || '-')}</button></td>
            <td>${escapeHtml(String(record.model || '-'))}</td>
            <td>${escapeHtml(String(record.status == null ? '-' : record.status))}</td>
            <td>${escapeHtml(formatDurationMs(record.ttft_ms))}</td>
            <td>${escapeHtml(formatDurationMs(record.total_latency_ms))}</td>
        </tr>`;
    }).join('');
    return { html, count: filtered.length };
}

// Pure: format an ISO timestamp as a short local time (or the raw value).
function formatExplorerTime(value) {
    const text = String(value || '');
    if (!text) return '-';
    const parsed = new Date(text);
    if (isNaN(parsed.getTime())) return text;
    return parsed.toISOString().replace('T', ' ').replace(/\.\d+Z$/, 'Z');
}

// Render the table and keep the row buttons wired to the drill-down.
function renderRequestExplorer(records) {
    _explorerRecords = Array.isArray(records) ? records : [];
    const filterInput = document.getElementById('explorerFilter');
    const query = filterInput ? filterInput.value : '';
    const tbody = document.getElementById('explorer-rows');
    if (!tbody) return;
    const result = explorerRows(_explorerRecords, query, _explorerSelectedId);
    tbody.innerHTML = result.html;
    const counter = document.getElementById('explorerCount');
    if (counter) {
        counter.textContent = result.count + ' / ' + _explorerRecords.length;
    }
    tbody.querySelectorAll('.explorer-open').forEach(button => {
        button.addEventListener('click', () => openRequestDetail(button.dataset.requestId));
    });
}

// Render the correlated timeline of one request.
function renderRequestDetail(timeline) {
    const el = document.getElementById('explorer-detail');
    if (!el) return;
    if (!timeline || typeof timeline !== 'object') {
        el.innerHTML = `<p class="text-muted">${escapeHtml(t('dashboard.explorerNoSelection'))}</p>`;
        return;
    }
    const events = (timeline.events || []).map(event =>
        `<li><span class="explorer-event-kind">${escapeHtml(String(event.kind))}</span>` +
        `<span class="explorer-event-at">+${escapeHtml(formatDurationMs(event.at_ms))}</span></li>`
    ).join('');
    const markers = (timeline.markers || []).map(marker =>
        `<li><span class="explorer-marker-kind">${escapeHtml(String(marker.kind))}</span>` +
        `<span class="explorer-marker-detail">${escapeHtml(String(marker.detail || ''))}</span></li>`
    ).join('');
    const rows = [
        { label: t('dashboard.explorerIngress'), value: String(timeline.ingress_ts || '-') },
        { label: t('dashboard.explorerEgress'), value: String(timeline.egress_ts || '-') },
        { label: t('dashboard.explorerTtft'), value: formatDurationMs(timeline.ttft_ms) },
        { label: t('dashboard.explorerDuration'), value: formatDurationMs(timeline.duration_ms) },
        { label: t('dashboard.sloMaxGap'), value: formatDurationMs(timeline.max_inter_chunk_gap_ms) },
        { label: t('dashboard.explorerRequestedModel'), value: String(timeline.requested_model || '-') },
        { label: t('dashboard.explorerServingModel'), value: String(timeline.serving_model || '-') },
        { label: t('dashboard.explorerFallback'), value: String((timeline.fallback && timeline.fallback.reason) || '-') },
        { label: t('dashboard.explorerParser'), value: String((timeline.parser && timeline.parser.failure_class) || '-') },
        { label: t('dashboard.explorerRegenerations'), value: formatNumber(timeline.regenerations || 0) },
        { label: t('dashboard.explorerErrorClass'), value: String(timeline.error_class || '-') },
        { label: t('dashboard.explorerFinishReason'), value: String(timeline.finish_reason || '-') },
        { label: t('dashboard.explorerStream'), value: timeline.stream ? 'yes' : 'no' }
    ];
    el.innerHTML = `
        <h4>${escapeHtml(String(timeline.request_id || ''))}</h4>
        <h5>${escapeHtml(t('dashboard.explorerTimeline'))}</h5>
        <ul class="explorer-events">${events || `<li class="text-muted">${escapeHtml(t('dashboard.explorerNoData'))}</li>`}</ul>
        <h5>${escapeHtml(t('dashboard.explorerMarkers'))}</h5>
        <ul class="explorer-markers">${markers || `<li class="text-muted">${escapeHtml(t('dashboard.explorerNoData'))}</li>`}</ul>
        <dl class="slo-metrics">${rows.map(row =>
            `<div class="slo-metric"><dt>${escapeHtml(row.label)}</dt><dd>${escapeHtml(row.value)}</dd></div>`
        ).join('')}</dl>
        <div style="margin-top: 1rem;">
            <button class="btn btn-sm btn-primary explorer-inspect-btn" id="explorerInspectBtn">
                <i class="fas fa-search-plus"></i> Inspect Payload (In / Forwarded / Out)
            </button>
        </div>`;

    const inspectBtn = el.querySelector('#explorerInspectBtn');
    if (inspectBtn) {
        inspectBtn.addEventListener('click', async () => {
            let recordToInspect = timeline;
            if (timeline.request_id) {
                try {
                    const logsData = await apiCall('GET', `/admin/logs?search=${encodeURIComponent(timeline.request_id)}&limit=1`);
                    if (logsData && logsData.records && logsData.records.length > 0) {
                        const fullLog = await apiCall('GET', `/admin/logs/${encodeURIComponent(logsData.records[0].id)}`);
                        if (fullLog) recordToInspect = fullLog;
                    }
                } catch (_) {}
            }
            openPayloadInspector(recordToInspect);
        });
    }
}

// ============================================================================
// Configuration readiness
// ============================================================================

// Render the effective runtime configuration: value, source (environment, panel
// override or default), restart-required markers, feature switches, credential
// presence and validation issues. Secrets are never rendered.
function renderConfigReadiness(report) {
    const data = (report && typeof report === 'object') ? report : {};
    const fields = (data.fields && typeof data.fields === 'object') ? data.fields : {};
    const issues = (data.validation && Array.isArray(data.validation.issues)) ? data.validation.issues : [];
    const drift = Array.isArray(data.drift) ? data.drift : [];

    const state = issues.length ? 'degraded' : 'ok';
    const chip = document.getElementById('config-state');
    if (chip) {
        chip.className = sloStateDescriptor(state).className;
        chip.textContent = t(sloStateDescriptor(state).labelKey);
    }

    const rows = document.getElementById('config-rows');
    if (rows) {
        const names = Object.keys(fields).sort();
        rows.innerHTML = names.length
            ? names.map(name => {
                const field = fields[name] || {};
                const source = String(field.source || 'default');
                const sourceKey = 'dashboard.configSource'
                    + source.charAt(0).toUpperCase() + source.slice(1);
                const value = field.value === null || field.value === undefined
                    ? '-' : String(field.value);
                return `<tr>
                    <th scope="row">${escapeHtml(name)}</th>
                    <td>${escapeHtml(value)}</td>
                    <td>${escapeHtml(t(sourceKey) || source)}</td>
                    <td>${field.restart_required ? escapeHtml(t('dashboard.configRestartRequired')) : ''}</td>
                </tr>`;
            }).join('')
            : `<tr><td colspan="4" class="text-muted">${escapeHtml(t('dashboard.configNoData'))}</td></tr>`;
    }

    const features = data.features && typeof data.features === 'object' ? data.features : {};
    renderSloMetrics('config-features', Object.keys(features).sort().map(name => ({
        label: name,
        value: features[name] ? t('dashboard.configEnabled') : t('dashboard.configDisabled')
    })));

    const secrets = data.secrets && typeof data.secrets === 'object' ? data.secrets : {};
    renderSloMetrics('config-secrets', Object.keys(secrets).sort().map(name => {
        const secret = secrets[name] || {};
        const state = secret.configured ? t('dashboard.configConfigured') : t('dashboard.configNotConfigured');
        return { label: name, value: state + ' (' + String(secret.source || '-') + ')' };
    }));

    const issueList = document.getElementById('config-issues');
    if (issueList) {
        const entries = issues.map(issue => String(issue.field) + ': ' + String(issue.issue))
            .concat(drift.map(item => String(item.field) + ': ' + String(item.detail || item.kind)));
        issueList.innerHTML = entries.length
            ? entries.map(entry => `<li><span class="explorer-marker-detail">${escapeHtml(entry)}</span></li>`).join('')
            : `<li class="text-muted">${escapeHtml(t('dashboard.configNoIssues'))}</li>`;
    }
}

// Load the configuration readiness report. Failures are contained.
async function loadConfigReadiness() {
    try {
        const data = await apiCall('GET', APP_CONSTANTS.ROUTES.ADMIN_CONFIG);
        renderConfigReadiness(data);
    } catch (error) {
        renderConfigReadiness(null);
    }
}

// Load the retained diagnostics records for the explorer. Failures are contained:
// the rest of the dashboard keeps working without the explorer.
async function loadRequestExplorerData() {    try {
        const data = await telemetryService.getDebugMetrics();
        renderRequestExplorer((data && data.records) || []);
    } catch (error) {
        renderRequestExplorer([]);
    }
}

// Load the drill-down for one request ID and render it.
async function openRequestDetail(requestId) {    if (!requestId) return;
    _explorerSelectedId = requestId;
    renderRequestExplorer(_explorerRecords);
    const el = document.getElementById('explorer-detail');
    if (el) {
        el.innerHTML = `<p class="text-muted">${escapeHtml(t('dashboard.loading'))}</p>`;
    }
    try {
        const data = await apiCall('GET', `${APP_CONSTANTS.ROUTES.ADMIN_DEBUG_METRICS}/${encodeURIComponent(requestId)}`);
        renderRequestDetail(data && data.request);
    } catch (error) {
        if (el) {
            el.innerHTML = `<p class="text-muted">${escapeHtml(String(error && error.message ? error.message : error))}</p>`;
        }
    }
    if (el && el.focus) {
        // The keyboard focus follows the drill-down so the timeline is reachable
        // without hunting for it.
        try {
            el.focus({ preventScroll: true });
        } catch (error) {
            el.focus();
        }
    }
}

// ============================================================================
// Accounts
// ============================================================================

async function loadAccounts() {
    try {
        const data = (typeof accountService !== 'undefined' && accountService.listAccounts)
            ? await accountService.listAccounts()
            : await apiCall('GET', '/admin/accounts');
        const accounts = data.accounts || [];

        const container = document.getElementById('accountsList');
        if (!container) return;

        if (accounts.length === 0) {
            container.innerHTML = `
                <div class="empty-state">
                    <i class="fas fa-users"></i>
                    <p>${t('accounts.noAccounts')}</p>
                    <button class="btn btn-primary" onclick="window.app.openAddAccountModal()">
                        <i class="fas fa-plus"></i> ${t('accounts.noAccounts')}
                    </button>
                </div>`;
            return;
        }

        // Security: account.label is user-controlled; id/label are escaped for text (escapeHtml)
        // and attributes (escapeAttr). Uses data-* attributes + addEventListener to avoid inline JS strings.
        container.innerHTML = accounts.map(account => {
            const idEsc = escapeAttr(account.id);
            const labelEsc = escapeAttr(account.label || '');
            return `
            <div class="account-card">
                <div class="account-card-header">
                    <div>
                        <h4>${escapeHtml(account.label || t('accounts.unnamed'))}</h4>
                        <span class="text-muted">ID: ${escapeHtml(String(account.id ?? ''))}</span>
                    </div>
                    ${getStatusBadge(account.status)}
                </div>
                <div class="account-detail">
                    <span class="label">PSID</span>
                    <span class="value">${escapeHtml(maskString(account.psid || '', 6, 4))}</span>
                </div>
                <div class="account-detail">
                    <span class="label">${t('accounts.requests')}</span>
                    <span class="value">${formatNumber(account.request_count || 0)}</span>
                </div>
                <div class="account-detail">
                    <span class="label">${t('accounts.errors')}</span>
                    <span class="value">${formatNumber(account.error_count || 0)}</span>
                </div>
                <div class="account-detail">
                    <span class="label">${t('accounts.models')}</span>
                    <span class="value">${(account.models || []).length || account.models_count || 0}</span>
                </div>
                <div class="account-detail">
                    <span class="label">${t('accounts.lastSuccess')}</span>
                    <span class="value">${escapeHtml(formatDate(account.last_success_at))}</span>
                </div>
${_accountQuotaRow(account)}${_accountHealthRow(account)}${_accountEmptyRow(account)}
                ${account.last_error ? `
                <div class="account-detail">
                    <span class="label">${t('accounts.lastError')}</span>
                    <span class="value text-muted">${_accountLastErrorText(account)}</span>
                </div>` : ''}
                <div class="account-actions">
                    <button class="btn btn-sm btn-outline acc-check-btn" data-account-id="${idEsc}">
                        <i class="fas fa-heartbeat"></i> ${t('accounts.check')}
                    </button>
                    <button class="btn btn-sm btn-outline acc-cookie-btn" data-account-id="${idEsc}" data-account-label="${labelEsc}">
                        <i class="fas fa-cookie-bite"></i> ${t('accounts.updateCookie')}
                    </button>
                    <button class="btn btn-sm btn-danger acc-remove-btn" data-account-id="${idEsc}">
                        <i class="fas fa-trash"></i> ${t('accounts.delete')}
                    </button>
                </div>
            </div>
        `;
        }).join('');

        // Bind events via dataset to prevent user data in inline JS strings
        container.querySelectorAll('.acc-check-btn').forEach(btn => {
            btn.addEventListener('click', () => checkAccount(btn.dataset.accountId));
        });
        container.querySelectorAll('.acc-cookie-btn').forEach(btn => {
            btn.addEventListener('click', () => openUpdateCookieModal(btn.dataset.accountId, btn.dataset.accountLabel || ''));
        });
        container.querySelectorAll('.acc-remove-btn').forEach(btn => {
            btn.addEventListener('click', () => removeAccount(btn.dataset.accountId));
        });
    } catch (error) {
        console.error('Failed to load accounts:', error);
    }
}

async function checkAccount(accountId) {
    try {
        showToast(t('accounts.checking') || 'Checking account...', 'info');
        const result = await accountService.checkAccount(accountId);
        const msg = result.valid ? (t('accounts.valid') || 'Account valid') : (t('accounts.invalid') || 'Account invalid');
        showToast(`${msg} | Models: ${result.models_count || 0}`, result.valid ? 'success' : 'error');
        await loadAccounts();
        await loadDashboard();
    } catch (error) {
        showToast(`${t('accounts.checkFailed')}: ${error.message}`, 'error');
    }
}

async function removeAccount(accountId) {
    const confirmed = await showConfirm({
        title: t('confirm.delete.title'),
        message: t('confirm.delete.message'),
        confirmText: t('confirm.delete.btn'),
        cancelText: t('confirm.cancel'),
        type: 'danger'
    });
    if (!confirmed) return;

    try {
        showToast(t('accounts.deleting'), 'info');
        await accountService.removeAccount(accountId);
        showToast(t('accounts.deleted'), 'success');
        await loadAccounts();
        await loadDashboard();
    } catch (error) {
        showToast(`${t('accounts.deleteFailed')}: ${error.message}`, 'error');
    }
}

function openAddAccountModal() {
    const modal = document.getElementById('addAccountModal');
    if (modal) modal.classList.add('active');
}

function closeAddAccountModal() {
    const modal = document.getElementById('addAccountModal');
    if (modal) {
        modal.classList.remove('active');
        const inputs = modal.querySelectorAll('input');
        inputs.forEach(input => { input.value = ''; });
    }
}

async function submitAddAccount() {
    const psid = document.getElementById('add-psid')?.value.trim();
    const psidts = document.getElementById('add-psidts')?.value.trim() || '';
    const label = document.getElementById('add-label')?.value.trim() || '';

    try {
        showToast(t('accounts.adding') || 'Adding account...', 'info');
        await accountService.addAccount({ psid, psidts, label });
        showToast(t('accounts.added') || 'Account added', 'success');
        closeAddAccountModal();
        await loadAccounts();
        await loadDashboard();
    } catch (error) {
        showToast(`${t('accounts.addFailed') || 'Failed to add account'}: ${error.message}`, 'error');
    }
}

// ============================================================================
// Update Cookie Modal
// ============================================================================

let updateCookieAccountId = null;

function openUpdateCookieModal(accountId, label) {
    updateCookieAccountId = accountId;
    const modal = document.getElementById('updateCookieModal');
    const title = document.getElementById('updateCookieTitle');
    if (title) title.textContent = `Update Cookie - ${label || accountId}`;
    if (modal) modal.classList.add('active');
}

function closeUpdateCookieModal() {
    const modal = document.getElementById('updateCookieModal');
    if (modal) {
        modal.classList.remove('active');
        const inputs = modal.querySelectorAll('input');
        inputs.forEach(input => { input.value = ''; });
    }
    updateCookieAccountId = null;
}

async function submitUpdateCookie() {
    const psid = document.getElementById('update-psid')?.value.trim();
    const psidts = document.getElementById('update-psidts')?.value.trim() || '';

    if (!updateCookieAccountId) {
        showToast('No account selected', 'error');
        return;
    }

    try {
        showToast('Updating Cookie...', 'info');
        await accountService.updateAccountCookies(updateCookieAccountId, { psid, psidts });
        showToast('Cookie updated successfully', 'success');
        closeUpdateCookieModal();
        await loadAccounts();
        await loadDashboard();
    } catch (error) {
        showToast(`Update failed: ${error.message}`, 'error');
    }
}

// ============================================================================
// Playground
// ============================================================================

let _pgConversationId = '';
let _pgMessages = [];
let _pgImages = [];  // Images pending upload [{dataUrl, name}]

// Aligned with backend app/utils/tools.py, used for image generation waiting state UI
const _PG_IMAGE_INTENT_PATTERNS = [
    '画一', '画个', '画张', '画幅', '画副', '帮我画', '给我画', '画图', '画一张', '画一幅',
    '画个图', '画张图', '画幅图', '画一只', '画只', '画头', '画条',
    '生成图片', '生成一张图', '生成图像', '生成一幅', '生成一张照片', '生成张图', '生成个图',
    '生成海报', '生成插画', '生成一张海报',
    '做一张图', '做个图', '做张图', '做一张海报', '做个海报', '做张海报', '做一张照片',
    '设计一张海报', '设计海报', '设计一张图', '设计张海报', '设计个海报', '设计一幅',
    '画一张海报', '画张海报', '画个海报', '画海报',
    '绘制', '绘一', '画出', '出一张图', '出张图', 'p一张', 'p个图', '做幅',
    '一张海报', '张海报', '幅海报', '来张图', '来一张图', '来个图',
    'draw a', 'draw an', 'draw me', 'generate an image', 'generate a picture',
    'generate an picture', 'generate a poster', 'create an image', 'create a picture',
    'create a photo', 'create a poster', 'design a poster', 'make an image',
    'make a picture', 'make a poster', 'an image of', 'a picture of', 'a photo of',
    'a poster of', 'image of a', 'picture of a', 'poster of',
];
const _PG_IMG_NOUNS = ['图', '图片', '图像', '海报', '插画', '照片', '壁纸', 'logo', '头像', '封面',
    'image', 'picture', 'poster', 'photo', 'drawing', 'illustration', 'wallpaper', 'avatar'];
const _PG_IMG_VERBS = ['画', '生成', '绘', '做', '设计', '出', '整', '来', '搞', '弄', '制作', '帮我', '给我',
    '想要', '要', '想', '需要', '求', '来一', '来个', '来张',
    'draw', 'generate', 'create', 'make', 'design', 'render', 'want', 'need'];

function _pgIsImageGenerationIntent(text) {
    if (!text) return false;
    const low = text.toLowerCase();
    return _PG_IMAGE_INTENT_PATTERNS.some((p) => low.includes(p));
}

function _pgMaybeImageGenerationIntent(text) {
    if (!text) return false;
    if (_pgIsImageGenerationIntent(text)) return true;
    const low = text.toLowerCase();
    return _PG_IMG_NOUNS.some((n) => low.includes(n)) && _PG_IMG_VERBS.some((v) => low.includes(v));
}

function _pgShowGeneratingWait(aiMsg, aiBubble, chatContainer) {
    aiMsg.classList.add('pg-generating');
    aiBubble.setAttribute('role', 'status');
    aiBubble.setAttribute('aria-live', 'polite');
    aiBubble.setAttribute('aria-busy', 'true');
    aiBubble.innerHTML = `
        <span class="pg-wait-text">${escapeHtml(t('playground.generatingImage'))}</span>
        <span class="pg-wait-hint">${escapeHtml(t('playground.generatingHint'))}</span>
        <span class="pg-wait-pulse" aria-hidden="true"></span>
    `;
    chatContainer.scrollTop = chatContainer.scrollHeight;
}

function _pgClearGeneratingState(aiMsg, aiBubble) {
    aiMsg.classList.remove('pg-generating');
    aiBubble.removeAttribute('aria-busy');
    aiBubble.removeAttribute('aria-live');
    aiBubble.removeAttribute('role');
}

function _pgShowStillWorking(aiBubble) {
    const textEl = aiBubble.querySelector('.pg-wait-text');
    if (textEl) textEl.textContent = t('playground.stillWorking');
}

function _pgFormatPlaygroundError(error, status) {
    const raw = (error?.message || '').toLowerCase();
    const statusHint = status ? String(status) : '';
    if (raw.includes('network') || raw.includes('failed to fetch') ||
        raw.includes('520') || raw.includes('502') || raw.includes('504') ||
        statusHint === '520' || statusHint === '502' || statusHint === '504') {
        return t('playground.networkError');
    }
    if (error?.message) {
        return `${t('playground.requestFailed')}: ${error.message}`;
    }
    return `${t('playground.requestFailed')} (${status || 'unknown'})`;
}

// Classify SSE frame (pure function).
// Handles top-level {"error":{message,type,code}} frames from backend without choices.
function _pgClassifyChunk(chunk) {
    if (chunk && typeof chunk === 'object' && chunk.error && typeof chunk.error === 'object') {
        return {
            kind: 'error',
            message: chunk.error.message == null ? '' : String(chunk.error.message),
            // Error frames on responses/gemini/claude routes may omit code; tolerate undefined/null
            code: chunk.error.code == null ? null : chunk.error.code
        };
    }
    const delta = chunk?.choices?.[0]?.delta;
    return {
        kind: 'delta',
        role: delta?.role || '',
        content: delta?.content || '',
        reasoning: delta?.reasoning_content || '',
        toolCalls: delta?.tool_calls || null
    };
}

// Extract display text from error frame (pure function).
// Clamps length and collapses whitespace; callers must render via textContent.
const _PG_ERR_MAX = 500;
function _pgFormatStreamError(err) {
    const raw = String(err?.message ?? '').replace(/\s+/g, ' ').trim();
    const msg = raw.length > _PG_ERR_MAX ? `${raw.slice(0, _PG_ERR_MAX)}…` : raw;
    const head = err?.code == null
        ? t('playground.gatewayError')
        : `${t('playground.gatewayError')} (${err.code})`;
    return msg ? `${head}: ${msg}` : head;
}

// Finalize streaming bubble display (pure function):
//   "error"         - Error before any content streamed: replace entire bubble with error
//   "partial-error" - Error after partial content streamed: preserve content and append error
//   "empty"         - No content and no error: valid empty response
//   "content"       - Normal completed stream
function _pgStreamOutcome(content, streamError) {
    if (streamError) {
        return {
            mode: content ? 'partial-error' : 'error',
            content: content || '',
            errorText: _pgFormatStreamError(streamError)
        };
    }
    if (!content) return { mode: 'empty', content: '', errorText: '' };
    return { mode: 'content', content, errorText: '' };
}

// Construct error DOM via textContent to prevent HTML injection.
function _pgErrorSpan(text) {
    const span = document.createElement('span');
    span.className = 'text-danger';
    span.textContent = text;
    return span;
}

function _pgSetSendBusy(busy) {
    const pgSend = document.getElementById('pg-send');
    if (!pgSend) return;
    pgSend.disabled = busy;
    if (busy) pgSend.setAttribute('aria-busy', 'true');
    else pgSend.removeAttribute('aria-busy');
}

let _canvasEngine = null;

function _getCanvasEngine() {
    if (!_canvasEngine) {
        const root = document.getElementById('pg-canvas-root');
        if (root) {
            _canvasEngine = new CanvasEngine({ container: root });
            _canvasEngine.render();
        }
    }
    return _canvasEngine;
}

function _pgAttachArtifactCards(container, content) {
    if (!content) return;
    const artifacts = CanvasEngine.extractArtifacts(content);
    if (!artifacts || artifacts.length === 0) return;

    const engine = _getCanvasEngine();
    if (engine) {
        engine.loadArtifact(artifacts[artifacts.length - 1]);
    }

    artifacts.forEach(art => {
        const card = document.createElement('div');
        card.className = 'chat-artifact-card';
        const iconClass = art.type === 'svg' ? 'bezier-curve' : (art.type === 'mermaid' ? 'project-diagram' : (art.type === 'js' ? 'code' : 'cube'));
        card.innerHTML = `
            <div class="chat-artifact-info">
                <i class="fas fa-${iconClass}"></i>
                <div>
                    <div class="chat-artifact-title">${escapeHtml(art.title)}</div>
                    <div class="chat-artifact-subtitle">${escapeHtml(art.lang || art.type)} • Click to inspect in Canvas</div>
                </div>
            </div>
            <button class="btn btn-sm btn-outline"><i class="fas fa-play"></i> Open</button>
        `;
        card.addEventListener('click', () => {
            const ce = _getCanvasEngine();
            if (ce) {
                ce.loadArtifact(art);
                const split = document.getElementById('playground-split-container');
                if (split && !split.classList.contains('has-canvas')) {
                    document.getElementById('pg-mode-studio')?.click();
                }
            }
        });
        container.appendChild(card);
    });
}

async function validatePlaygroundToolJson() {
    const input = document.getElementById('pg-tool-json');
    const result = document.getElementById('pg-tool-result');
    const button = document.getElementById('pg-tool-validate');
    if (!input || !result || !button) return;
    const text = input.value.trim();
    if (!text) {
        result.className = 'pg-tool-result is-invalid';
        result.textContent = t('playground.toolJsonEmpty');
        return;
    }
    button.disabled = true;
    result.className = 'pg-tool-result';
    result.textContent = t('playground.toolChecking');
    try {
        const data = await telemetryService.validateToolJson(text);
        const length = Number(data.response_length) || text.length;
        if (data.valid) {
            const repair = data.repaired ? ` ${t('playground.toolRepaired')}` : '';
            result.className = 'pg-tool-result is-valid';
            result.textContent = `${t('playground.toolValid')} (${data.tool_call_count || 0} ${t('playground.toolCalls')}, ${length} ${t('playground.toolChars')})${repair}`;
        } else {
            const suffix = data.truncated_suspected ? ` ${t('playground.toolTruncated')}` : '';
            result.className = 'pg-tool-result is-invalid';
            result.textContent = `${t('playground.toolInvalid')}: ${data.failure_class || 'json_syntax'} (${length} ${t('playground.toolChars')})${suffix}`;
        }
    } catch (error) {
        result.className = 'pg-tool-result is-invalid';
        result.textContent = `${t('playground.toolCheckFailed')}: ${error.message || ''}`;
    } finally {
        button.disabled = false;
    }
}

function initPlaygroundToolTest() {
    const toggle = document.getElementById('pg-tool-test');
    const input = document.getElementById('pg-tool-json');
    const button = document.getElementById('pg-tool-validate');
    if (!toggle || !input || !button || button._bound) return;
    const sync = () => {
        input.disabled = !toggle.checked;
        button.disabled = !toggle.checked;
    };
    toggle.addEventListener('change', sync);
    button.addEventListener('click', validatePlaygroundToolJson);
    button._bound = true;
    sync();
}

let _pgAbortController = null;

async function sendPlaygroundRequest() {
    const pgSend = document.getElementById('pg-send');
    if (pgSend?.disabled) return;

    const message = document.getElementById('pg-message')?.value.trim();
    const model = document.getElementById('pg-model')?.value;
    const chatContainer = document.getElementById('pg-chat');

    if (!message && _pgImages.length === 0) {
        showToast(t('playground.emptyMessage'), 'warning');
        return;
    }

    // Cancel any previous stream
    if (_pgAbortController) {
        _pgAbortController.abort();
        _pgAbortController = null;
    }
    _pgAbortController = new AbortController();

    const placeholder = chatContainer?.querySelector('.chat-placeholder');
    if (placeholder) placeholder.remove();

    const isImageGen = _pgMaybeImageGenerationIntent(message);

    // Assemble multimodal content: use array format when images are present, string for plain text
    let userContent;
    if (_pgImages.length > 0) {
        userContent = [];
        if (message) userContent.push({ type: 'text', text: message });
        for (const img of _pgImages) {
            userContent.push({ type: 'image_url', image_url: { url: img.dataUrl } });
        }
    } else {
        userContent = message;
    }
    _pgMessages.push({ role: 'user', content: userContent });
    // Enforce ring-buffer bounds (FIFO eviction on client heap)
    while (_pgMessages.length > APP_CONSTANTS.LIMITS.MAX_RING_BUFFER_MESSAGES) {
        _pgMessages.shift();
    }

    const imgHtml = _pgImages.map(img => `<img src="${img.dataUrl}" class="chat-img" alt="${escapeHtml(img.name)}">`).join('');
    const userMsg = document.createElement('div');
    userMsg.className = 'chat-message user';
    userMsg.innerHTML = `
        <div class="chat-avatar"><i class="fas fa-user"></i></div>
        <div class="chat-bubble">${escapeHtml(message)}${imgHtml}</div>
    `;
    chatContainer.appendChild(userMsg);

    const aiMsg = document.createElement('div');
    aiMsg.className = 'chat-message assistant';
    aiMsg.innerHTML = `
        <div class="chat-avatar"><i class="fas fa-microchip"></i></div>
        <div class="chat-content"><div class="chat-bubble"><span class="typing-cursor"></span></div></div>
    `;
    chatContainer.appendChild(aiMsg);
    const aiBubble = aiMsg.querySelector('.chat-bubble');

    // Enforce DOM node bounds (FIFO eviction)
    while (chatContainer.children.length > APP_CONSTANTS.LIMITS.MAX_RING_BUFFER_MESSAGES) {
        chatContainer.removeChild(chatContainer.firstElementChild);
    }
    chatContainer.scrollTop = chatContainer.scrollHeight;

    if (isImageGen) {
        _pgShowGeneratingWait(aiMsg, aiBubble, chatContainer);
    }

    document.getElementById('pg-message').value = '';
    _pgImages = [];
    _renderPgImagePreview();
    _pgSetSendBusy(true);

    const token = safeStorage.getSecureItem(APP_CONSTANTS.STORAGE_KEYS.TOKEN);
    const reqBody = {
        model: model || APP_CONSTANTS.DEFAULT_MODELS.FALLBACK_MODEL,
        messages: [..._pgMessages],
        stream: true
    };
    if (_pgConversationId) {
        reqBody.conversation_id = _pgConversationId;
    }
    if (document.getElementById('pg-thinking')?.checked) {
        reqBody.reasoning_effort = 'high';
    }

    const streamTimeoutId = setTimeout(() => {
        if (_pgAbortController) {
            _pgAbortController.abort(new Error(`Stream timed out after ${APP_CONSTANTS.TIMEOUTS.STREAM_DEFAULT / 1000}s`));
        }
    }, APP_CONSTANTS.TIMEOUTS.STREAM_DEFAULT);

    try {
        const resp = await fetch(APP_CONSTANTS.ROUTES.CHAT_COMPLETIONS, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${token}`
            },
            body: JSON.stringify(reqBody),
            signal: _pgAbortController.signal
        });

        clearTimeout(streamTimeoutId);

        if (!resp.ok) {
            _pgMessages.pop();
            const err = await resp.json().catch(() => null);
            const msg = err?.error?.message || _pgFormatPlaygroundError(null, resp.status);
            _pgClearGeneratingState(aiMsg, aiBubble);
            aiBubble.innerHTML = `<span class="text-danger">${escapeHtml(msg)}</span>`;
            return;
        }

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let content = '';
        let gotContent = false;
        let streamError = null;
        let reasoningBody = null;
        const ensureReasoningBlock = () => {
            if (reasoningBody) return reasoningBody;
            const det = document.createElement('details');
            det.className = 'pg-reasoning';
            det.open = true;
            const summary = document.createElement('summary');
            summary.textContent = t('playground.reasoningTitle');
            const body = document.createElement('div');
            body.className = 'pg-reasoning-body';
            det.appendChild(summary);
            det.appendChild(body);
            aiBubble.parentNode.insertBefore(det, aiBubble);   // .chat-content column: reasoning ABOVE the answer bubble
            reasoningBody = body;
            return reasoningBody;
        };

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop();

            for (const rawLine of lines) {
                const line = rawLine.replace(/\r$/, '');
                if (line.startsWith(':')) {
                    if (isImageGen && !gotContent) _pgShowStillWorking(aiBubble);
                    continue;
                }
                if (!line.startsWith('data: ')) continue;
                const data = line.slice(6).trim();
                if (data === '[DONE]') break;
                try {
                    const chunk = JSON.parse(data);
                    const info = _pgClassifyChunk(chunk);
                    if (info.kind === 'error') {
                        // Record error without breaking loop: error frames are followed by [DONE], finalized on completion
                        streamError = info;
                        continue;
                    }
                    if (info.role && !info.content && isImageGen && !gotContent) {
                        _pgShowStillWorking(aiBubble);
                        continue;
                    }
                    const piece = info.content;
                    if (piece) {
                        gotContent = true;
                        _pgClearGeneratingState(aiMsg, aiBubble);
                        content += piece;
                        aiBubble.textContent = content;
                        chatContainer.scrollTop = chatContainer.scrollHeight;
                    }
                    const rPiece = info.reasoning;
                    if (rPiece) {
                        const rb = ensureReasoningBlock();
                        rb.textContent += rPiece;
                        chatContainer.scrollTop = chatContainer.scrollHeight;
                    }
                } catch {}
            }
        }

        const outcome = _pgStreamOutcome(content, streamError);
        _pgClearGeneratingState(aiMsg, aiBubble);
        if (outcome.mode === 'error') {
            _pgMessages.pop();
            aiBubble.textContent = '';
            aiBubble.appendChild(_pgErrorSpan(outcome.errorText));
        } else if (outcome.mode === 'partial-error') {
            aiBubble.innerHTML = _pgRenderContent(outcome.content);
            aiBubble.appendChild(document.createElement('br'));
            aiBubble.appendChild(_pgErrorSpan(outcome.errorText));
            chatContainer.scrollTop = chatContainer.scrollHeight;
            _pgMessages.push({ role: 'assistant', content: outcome.content });
            while (_pgMessages.length > (typeof APP_CONSTANTS !== 'undefined' ? APP_CONSTANTS.LIMITS.MAX_RING_BUFFER_MESSAGES : 200)) {
                _pgMessages.shift();
            }
        } else if (outcome.mode === 'empty') {
            aiBubble.textContent = t('playground.noContent');
        } else {
            aiBubble.innerHTML = _pgRenderContent(outcome.content);
            if (typeof _pgAttachArtifactCards === 'function') _pgAttachArtifactCards(aiBubble, outcome.content);
            chatContainer.scrollTop = chatContainer.scrollHeight;
            _pgMessages.push({ role: 'assistant', content: outcome.content });
            while (_pgMessages.length > (typeof APP_CONSTANTS !== 'undefined' ? APP_CONSTANTS.LIMITS.MAX_RING_BUFFER_MESSAGES : 200)) {
                _pgMessages.shift();
            }
        }
    } catch (error) {
        clearTimeout(streamTimeoutId);
        _pgMessages.pop();
        _pgClearGeneratingState(aiMsg, aiBubble);
        aiBubble.innerHTML = `<span class="text-danger">${escapeHtml(_pgFormatPlaygroundError(error))}</span>`;
    } finally {
        clearTimeout(streamTimeoutId);
        _pgAbortController = null;
        _pgSetSendBusy(false);
    }
}

function escapeHtml(text) {
    return String(text == null ? '' : text)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

// Double-quote attribute escaping: escapeHtml does not escape ASCII double quotes;
// escape for src="..." and alt="..." attributes to prevent event handler injection.
function escapeAttr(text) {
    return escapeHtml(text).replace(/"/g, '&quot;');
}

// Render AI response as HTML: convert markdown images and data URIs to <img>, escaping remaining text
function _pgRenderContent(text) {
    if (!text) return '';
    const imgs = [];
    let s = text;
    // 1) Markdown images: ![alt](url)
    s = s.replace(/!\[[^\]]*\]\((data:image\/[^)]+|https?:\/\/[^)\s]+)\)/g, (m, url) => {
        const i = imgs.push(url) - 1;
        return `\u0000IMG${i}\u0000`;
    });
    // 2) Standalone data:image or /images/ URL
    s = s.replace(/(data:image\/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=]+)/g, (m, url) => {
        const i = imgs.push(url) - 1;
        return `\u0000IMG${i}\u0000`;
    });
    s = s.replace(/(https?:\/\/[^\s)]+\/images\/[^\s)]+\.(?:png|jpg|jpeg|webp|gif))/gi, (m, url) => {
        const i = imgs.push(url) - 1;
        return `\u0000IMG${i}\u0000`;
    });
    // Escape remaining text and restore <img> tags
    let html = escapeHtml(s);
    imgs.forEach((url, i) => {
// Security: URL comes from model output (untrusted). Validate protocol http(s)/data:image
// and escape attributes using escapeAttr to prevent XSS.
        const safeUrl = /^(https?:|data:image\/)/i.test(url) ? escapeAttr(url) : '';
        html = html.replace(`\u0000IMG${i}\u0000`,
            `<img src="${safeUrl}" class="chat-img" alt="generated image">`);
    });
    return html;
}

function clearPlayground() {
    if (_pgAbortController) {
        _pgAbortController.abort();
        _pgAbortController = null;
    }
    const chatContainer = document.getElementById('pg-chat');
    if (chatContainer) {
        chatContainer.innerHTML = `<div class="chat-placeholder"><i class="fas fa-comment-dots"></i><p>${escapeHtml(t('playground.startChat'))}</p></div>`;
    }
    const message = document.getElementById('pg-message');
    if (message) message.value = '';
    _pgConversationId = '';
    _pgMessages = [];
    _pgImages = [];
    _renderPgImagePreview();
    if (_canvasEngine) {
        _canvasEngine.currentArtifact = null;
        _canvasEngine.logs = [];
        _canvasEngine.render();
    }
}

function _renderPgImagePreview() {
    const box = document.getElementById('pg-image-preview');
    if (!box) return;
    box.innerHTML = _pgImages.map((img, i) =>
        `<span class="pg-img-thumb"><img src="${img.dataUrl}" alt="${escapeHtml(img.name)}"><button type="button" class="pg-img-del" data-idx="${i}">&times;</button></span>`
    ).join('');
    box.querySelectorAll('.pg-img-del').forEach(btn => {
        btn.addEventListener('click', () => {
            _pgImages.splice(parseInt(btn.dataset.idx), 1);
            _renderPgImagePreview();
        });
    });
}

function _handlePgImageSelect(files) {
    for (const file of files) {
        if (!file.type.startsWith('image/')) continue;
        const reader = new FileReader();
        reader.onload = (e) => {
            _pgImages.push({ dataUrl: e.target.result, name: file.name });
            _renderPgImagePreview();
        };
        reader.readAsDataURL(file);
    }
}

// ============================================================================
// Utilities
// ====================================================================================

function setText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
}

async function copyModel(model) {
    const ok = await copyToClipboard(model);
    showToast(ok ? `${t('toast.copied') || 'Copied'}: ${model}` : (t('toast.error') || 'Failed to copy'), ok ? 'success' : 'error');
}

// ============================================================================
// Update Check
// ============================================================================

let updateInfo = null;

async function checkForUpdate() {
    try {
        const data = await apiCall('GET', '/admin/check-update');
        updateInfo = data;
        updateCheckButton(data);
    } catch (error) {
        console.error('Failed to check for updates:', error);
    }
}

function updateCheckButton(data) {
    const btn = document.getElementById('checkUpdateBtn');
    if (!btn) return;

    const text = btn.querySelector('span');

    if (data.has_update) {
        btn.classList.remove('btn-outline');
        btn.classList.add('btn-success');
        if (text) text.textContent = t('dashboard.updateAvailable') + ' v' + data.latest;
    } else {
        if (text) text.textContent = t('dashboard.upToDate');
    }
}

function extractLocalizedNotes(body) {
    if (!body) return '';
    const lang = localStorage.getItem('gemini2api_lang') || 'zh-CN';
    const marker = `<!-- ${lang} -->`;
    const idx = body.indexOf(marker);
    if (idx === -1) {
        const enIdx = body.indexOf('<!-- en-US -->');
        if (enIdx !== -1) {
            const start = enIdx + '<!-- en-US -->'.length;
            const nextMarker = body.indexOf('<!--', start);
            return (nextMarker === -1 ? body.slice(start) : body.slice(start, nextMarker)).trim();
        }
        return body.replace(/<!--.*?-->/g, '').trim().split('\n').slice(0, 10).join('\n');
    }
    const start = idx + marker.length;
    const nextMarker = body.indexOf('<!--', start);
    return (nextMarker === -1 ? body.slice(start) : body.slice(start, nextMarker)).trim();
}

async function handleCheckUpdate() {
    const btn = document.getElementById('checkUpdateBtn');
    if (!btn) return;

    if (updateInfo && updateInfo.has_update) {
        const updateCmd = 'docker compose pull && docker compose up -d';
        const releaseNotes = extractLocalizedNotes(updateInfo.release_notes || '');

        const overlay = document.createElement('div');
        overlay.className = 'modal-overlay active';
        overlay.style.bottom = '0';
        overlay.innerHTML = `
            <div style="background:var(--bg-primary);border-radius:var(--radius-xl,16px);width:90%;max-width:460px;box-shadow:0 20px 60px rgba(0,0,0,0.3);animation:modalIn 0.2s ease">
                <div style="padding:1.5rem 2rem;text-align:center">
                    <div style="width:56px;height:56px;border-radius:50%;background:var(--primary-color)15;display:inline-flex;align-items:center;justify-content:center;margin-bottom:1rem">
                        <i class="fas fa-cloud-download-alt" style="font-size:1.5rem;color:var(--primary-color)"></i>
                    </div>
                    <h3 style="margin:0 0 0.5rem;font-size:1.125rem;color:var(--text-primary)">${t('confirm.update.title')} v${updateInfo.latest}</h3>
                    ${releaseNotes ? `<div style="max-height:120px;overflow-y:auto;text-align:left;padding:0.75rem;margin:0.75rem 0;background:var(--bg-tertiary);border-radius:var(--radius-lg,10px);border:1px solid var(--border-color);font-size:0.8rem;color:var(--text-secondary);line-height:1.5;white-space:pre-wrap">${escapeHtml(releaseNotes)}</div>` : ''}
                    <p style="margin:0.75rem 0 0.5rem;color:var(--text-secondary);font-size:0.85rem">${t('confirm.update.message')}</p>
                    <div style="display:flex;align-items:center;background:var(--bg-tertiary);border-radius:var(--radius-lg,10px);padding:0.6rem 1rem;margin:0.5rem 0;border:1px solid var(--border-color)">
                        <code style="flex:1;font-size:0.8rem;color:var(--text-primary);font-family:monospace;word-break:break-all">${updateCmd}</code>
                        <button class="update-copy-btn" style="flex-shrink:0;margin-left:0.5rem;background:none;border:none;color:var(--text-secondary);cursor:pointer;padding:0.25rem;border-radius:4px;transition:all 0.2s" title="${t('toast.copied')}">
                            <i class="fas fa-copy" style="font-size:0.9rem"></i>
                        </button>
                    </div>
                </div>
                <div style="display:flex;gap:0.75rem;padding:0 2rem 1.5rem;justify-content:center">
                    <button class="update-close-btn" style="flex:1;padding:0.625rem 1.25rem;border-radius:var(--radius-lg,10px);border:1px solid var(--border-color);background:var(--bg-tertiary);color:var(--text-primary);cursor:pointer;font-size:0.875rem;font-weight:500;transition:all 0.2s">${t('confirm.cancel')}</button>
                </div>
            </div>
        `;

        const close = () => {
            overlay.style.opacity = '0';
            setTimeout(() => overlay.remove(), 200);
        };

        overlay.querySelector('.update-copy-btn').onclick = async () => {
            try {
                await navigator.clipboard.writeText(updateCmd);
            } catch (e) {
                const ta = document.createElement('textarea');
                ta.value = updateCmd;
                document.body.appendChild(ta);
                ta.select();
                document.execCommand('copy');
                document.body.removeChild(ta);
            }
            const icon = overlay.querySelector('.update-copy-btn i');
            icon.className = 'fas fa-check';
            setTimeout(() => { icon.className = 'fas fa-copy'; }, 1500);
            showToast(t('toast.copied'), 'success');
        };

        overlay.querySelector('.update-close-btn').onclick = close;
        overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });

        document.body.appendChild(overlay);
    } else {
        // Check for update
        btn.disabled = true;
        await checkForUpdate();
        btn.disabled = false;
    }
}

// ============================================================================
// Event Listeners
// ============================================================================

function initEventListeners() {
    // Add account button
    const addAccountBtn = document.getElementById('addAccountBtn');
    if (addAccountBtn) {
        addAccountBtn.addEventListener('click', openAddAccountModal);
    }

    // Request explorer: filter and manual refresh
    const explorerFilter = document.getElementById('explorerFilter');
    if (explorerFilter && !explorerFilter._bound) {
        explorerFilter._bound = true;
        // The filter is a saved filter: it survives a reload.
        try {
            const saved = safeStorage.getItem(APP_CONSTANTS.STORAGE_KEYS.EXPLORER_FILTER);
            if (saved) explorerFilter.value = saved;
        } catch (error) {
            // A storage failure only costs the saved filter.
        }
        explorerFilter.addEventListener('input', () => {
            try {
                safeStorage.setItem(APP_CONSTANTS.STORAGE_KEYS.EXPLORER_FILTER, explorerFilter.value || '');
            } catch (error) {
                // Ignore: the filter still works for this session.
            }
            renderRequestExplorer(_explorerRecords);
        });
    }
    const explorerRefresh = document.getElementById('explorerRefreshBtn');
    if (explorerRefresh && !explorerRefresh._bound) {
        explorerRefresh._bound = true;
        explorerRefresh.addEventListener('click', () => loadRequestExplorerData());
    }

    // Pause / resume the periodic dashboard refresh
    const refreshToggle = document.getElementById('dashboardRefreshToggleBtn');
    if (refreshToggle && !refreshToggle._bound) {
        refreshToggle._bound = true;
        refreshToggle.addEventListener('click', () => toggleDashboardRefresh());
    }
    updateRefreshToggleButton();

    // Reset the Argon stall breaker by hand (confirmation protected)
    const breakerReset = document.getElementById('argonBreakerResetBtn');
    if (breakerReset && !breakerReset._bound) {
        breakerReset._bound = true;
        breakerReset.addEventListener('click', () => resetArgonBreaker());
    }

    // Endpoint snippets tab switching & copy
    const snippetTabs = document.querySelectorAll('#endpoint-snippet-tabs .btn');
    snippetTabs.forEach(btn => {
        if (!btn._bound) {
            btn._bound = true;
            btn.addEventListener('click', () => {
                snippetTabs.forEach(b => {
                    b.classList.remove('active');
                    b.setAttribute('aria-selected', 'false');
                });
                btn.classList.add('active');
                btn.setAttribute('aria-selected', 'true');
                const targetSnippet = btn.dataset.snippet;
                document.querySelectorAll('.quick-endpoint-snippet').forEach(s => {
                    s.hidden = s.id !== `snippet-${targetSnippet}`;
                });
            });
        }
    });

    document.querySelectorAll('.snippet-copy-btn').forEach(btn => {
        if (!btn._bound) {
            btn._bound = true;
            btn.addEventListener('click', async () => {
                const pre = btn.closest('.snippet-code-wrap')?.querySelector('pre');
                if (!pre || !pre.textContent) return;
                const icon = btn.querySelector('i');
                const label = btn.querySelector('span');
                try {
                    const copied = await copyToClipboard(pre.textContent.trim());
                    if (!copied) throw new Error('clipboard unavailable');
                    if (icon) icon.className = 'fas fa-check';
                    if (label) label.textContent = t('dashboard.copied');
                    btn.classList.add('is-copied');
                    clearTimeout(btn._copyFeedbackTimer);
                    btn._copyFeedbackTimer = setTimeout(() => {
                        if (icon) icon.className = 'fas fa-copy';
                        if (label) label.textContent = t('dashboard.copy');
                        btn.classList.remove('is-copied');
                    }, 2000);
                } catch (error) {
                    showToast(t('dashboard.copyFailed'), 'error');
                }
            });
        }
    });

    // Confirm add account
    const confirmBtn = document.getElementById('confirmAddAccount');
    if (confirmBtn) {
        confirmBtn.addEventListener('click', submitAddAccount);
    }

    // Add account modal close buttons
    const addModal = document.getElementById('addAccountModal');
    if (addModal) {
        addModal.querySelectorAll('.modal-close, .modal-cancel').forEach(btn => {
            btn.addEventListener('click', closeAddAccountModal);
        });
        addModal.addEventListener('click', (e) => {
            if (e.target === addModal) closeAddAccountModal();
        });
    }

    // Update cookie modal
    const updateModal = document.getElementById('updateCookieModal');
    if (updateModal) {
        updateModal.querySelectorAll('.modal-close, .modal-cancel').forEach(btn => {
            btn.addEventListener('click', closeUpdateCookieModal);
        });
        updateModal.addEventListener('click', (e) => {
            if (e.target === updateModal) closeUpdateCookieModal();
        });
        const confirmUpdateBtn = document.getElementById('confirmUpdateCookie');
        if (confirmUpdateBtn) {
            confirmUpdateBtn.addEventListener('click', submitUpdateCookie);
        }
    }

    // Playground send
    const pgSend = document.getElementById('pg-send');
    if (pgSend) {
        pgSend.addEventListener('click', sendPlaygroundRequest);
    }
    initPlaygroundToolTest();

    // Playground textarea: Enter to send, Shift+Enter for newline
    const pgMessage = document.getElementById('pg-message');
    if (pgMessage) {
        pgMessage.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendPlaygroundRequest();
            }
        });
    }

    // Playground clear
    const pgClear = document.getElementById('pg-clear-btn');
    if (pgClear) {
        pgClear.addEventListener('click', clearPlayground);
    }

    // Playground Studio mode selector
    const modeStudio = document.getElementById('pg-mode-studio');
    const modeChat = document.getElementById('pg-mode-chat');
    const splitContainer = document.getElementById('playground-split-container');
    if (modeStudio && modeChat && splitContainer) {
        modeStudio.addEventListener('click', () => {
            modeStudio.classList.add('active', 'btn-primary');
            modeStudio.classList.remove('btn-outline');
            modeChat.classList.remove('active', 'btn-primary');
            modeChat.classList.add('btn-outline');
            splitContainer.classList.add('has-canvas');
            const ce = _getCanvasEngine();
            if (ce && !ce.currentArtifact) {
                ce.render();
            }
        });
        modeChat.addEventListener('click', () => {
            modeChat.classList.add('active', 'btn-primary');
            modeChat.classList.remove('btn-outline');
            modeStudio.classList.remove('active', 'btn-primary');
            modeStudio.classList.add('btn-outline');
            splitContainer.classList.remove('has-canvas');
        });
        // Initialize Canvas Engine on startup if container is present
        _getCanvasEngine();
    }

    // Playground image upload
    const pgImageBtn = document.getElementById('pg-image-btn');
    const pgImageInput = document.getElementById('pg-image-input');
    if (pgImageBtn && pgImageInput) {
        pgImageBtn.addEventListener('click', () => pgImageInput.click());
        pgImageInput.addEventListener('change', (e) => {
            _handlePgImageSelect(e.target.files);
            e.target.value = '';
        });
    }

    // Restart
    const restartBtn = document.getElementById('restartBtn');
    if (restartBtn) {
        restartBtn.addEventListener('click', async () => {
            const confirmed = await showConfirm({
                title: t('confirm.restart.title'),
                message: t('confirm.restart.message'),
                confirmText: t('confirm.restart.btn'),
                cancelText: t('confirm.cancel'),
                type: 'warning'
            });
            if (!confirmed) return;
            restartBtn.classList.add('restarting');
            const result = await requestRestart(() => apiCall('POST', '/admin/restart?confirm=true'));
            if (!result.ok) {
                restartBtn.classList.remove('restarting');
                showToast(result.message || t('toast.error'), 'error');
                return;
            }
            showToast(t('toast.restarting'), 'success');
            setTimeout(() => {
                const check = setInterval(async () => {
                    try {
                        const resp = await fetch('/health');
                        if (resp.ok) {
                            clearInterval(check);
                            window.location.reload();
                        }
                    } catch (e) {}
                }, 1500);
                setTimeout(() => {
                    clearInterval(check);
                    window.location.reload();
                }, 15000);
            }, 2000);
        });
    }

    // Logout
    const logoutBtn = document.getElementById('logoutBtn');
    if (logoutBtn) {
        logoutBtn.addEventListener('click', async () => {
            const confirmed = await showConfirm({
                title: t('confirm.logout.title'),
                message: t('confirm.logout.message'),
                confirmText: t('confirm.logout.btn'),
                cancelText: t('confirm.cancel'),
                type: 'info'
            });
            if (confirmed) logout();
        });
    }

    // Refresh account status
    const refreshBtn = document.getElementById('refreshAccountStatusBtn');
    if (refreshBtn) {
        refreshBtn.addEventListener('click', loadDashboard);
    }

    // Check update button
    const checkUpdateBtn = document.getElementById('checkUpdateBtn');
    if (checkUpdateBtn) {
        checkUpdateBtn.addEventListener('click', handleCheckUpdate);
    }

    // Mobile menu toggle
    const mobileToggle = document.getElementById('mobileMenuToggle');
    if (mobileToggle) {
        mobileToggle.addEventListener('click', () => {
            const sidebar = document.querySelector('.sidebar');
            if (sidebar) sidebar.classList.toggle('active');
        });
    }
}

// ============================================================================
// Initialization
// ============================================================================

async function initApp() {
    if (isAppInitialized) return;
    isAppInitialized = true;

    initElements();
    initThemeSwitcher();
    initI18n();
    initLanguageSwitcher();
    initNavigation();
    initEventListeners();
    initLogs();
    initUsageStats();
    initSettings();
    initApiKeys();
    initGems();
    initBabysit();

    // initNavigation already established the lifecycle; this is an idempotent compatibility
    // guard for embedded shells that mark the dashboard active during later initialization.
    const activeSection = document.querySelector('.section.active');
    if (activeSection && activeSection.id === 'dashboard') {
        startDashboardAutoRefresh();
    }
}

// Expose functions for onclick handlers
window.app = {
    checkAccount,
    removeAccount,
    openAddAccountModal,
    closeAddAccountModal,
    openUpdateCookieModal,
    closeUpdateCookieModal,
    sendPlaygroundRequest,
    clearPlayground,
    copyModel,
    updatePlaygroundModels,
    reloadCurrentSection() {
        const activeSection = document.querySelector('.section.active');
        if (activeSection) {
            loadSectionData(activeSection.id);
        }
    },
    retrySectionLoad(sectionId) {
        // A section whose template failed to load is only an error placeholder; reloading its data
        // cannot help. Re-fetch the whole shell so the template and its event bindings come back.
        const el = document.getElementById(sectionId);
        if (el && el.classList.contains('error-boundary-section')) {
            window.location.reload();
            return;
        }
        loadSectionData(sectionId);
    }
};

// Wait for components to load, then initialize
window.addEventListener('componentsLoaded', async () => {
    const authSuccess = await initAuth();
    if (authSuccess) {
        await initApp();
    }
});

// Fallback: if components are already in DOM (static HTML)
document.addEventListener('DOMContentLoaded', () => {
    setTimeout(async () => {
        if (!isAppInitialized) {
            const sidebar = document.querySelector('.sidebar');
            if (sidebar) {
                const authSuccess = await initAuth();
                if (authSuccess) await initApp();
            }
        }
    }, 500);
});
