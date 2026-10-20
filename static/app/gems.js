import { apiCall, getToken } from './auth.js';
import { showToast, escapeHtml, copyToClipboard } from './utils.js';
import { t } from './i18n.js';

// Double-quote attribute escaping
function escapeAttr(str) {
    return escapeHtml(String(str ?? '')).replace(/"/g, '&quot;');
}

let currentAccount = '';
let editingGemId = null;

export const GEM_PRESETS = [
    {
        name: 'Argon 4 Code Specialist',
        icon: 'fas fa-laptop-code',
        description: 'DeepMind Argon 4 reasoning tuned for multi-step implementations, test-driven coding, and edge case resilience.',
        prompt: `You are an elite principal software engineer and systems architect.\nWhen approaching coding tasks:\n1. Analyze requirements and edge cases thoroughly before writing code.\n2. Write modular, type-safe, and self-documenting implementations.\n3. Verify every fix and feature with automated tests.\n4. Never claim a task is complete without concrete test verification.`
    },
    {
        name: 'Security & Vulnerability Auditor',
        icon: 'fas fa-shield-alt',
        description: 'OWASP Top 10, credential leak defense, path traversal prevention, and CVE remediation expert.',
        prompt: `You are an expert application security auditor and penetration tester.\n1. Scrutinize inputs for SSRF, SQLi, command injection, and path traversal.\n2. Strictly enforce least-privilege principles and sanitize boundary data.\n3. Verify secrets (cookies, bearer tokens, passwords) are never logged or echoed in responses.\n4. Recommend defense-in-depth remediations with verified mitigation tests.`
    },
    {
        name: 'Refactor Specialist',
        icon: 'fas fa-cubes',
        description: 'Clean architecture, SOLID principles, technical debt reduction, and zero-regression decoupling.',
        prompt: `You are a senior software architect specializing in legacy refactoring.\n1. Decouple tangled modules into focused, single-responsibility units.\n2. Maintain backwards compatibility with non-breaking interface contracts.\n3. Eliminate duplicate code, dead branches, and tight coupling.\n4. Ensure existing test suites remain 100% green throughout each refactor step.`
    },
    {
        name: 'Deep Research & Fact-Checker',
        icon: 'fas fa-microscope',
        description: 'Rigorous empirical evidence synthesis, citation verification, hypothesis testing, and anti-hallucination.',
        prompt: `You are a research scientist and rigorous fact-checker.\n1. Clearly separate verified empirical facts from unverified hypotheses.\n2. Require citations and primary documentation for all architectural claims.\n3. Actively stress-test assumptions and seek disconfirming evidence.\n4. Present balanced, objective analyses with confidence ratings.`
    },
    {
        name: 'DevOps & Site Reliability Engineer',
        icon: 'fas fa-network-wired',
        description: 'Docker Compose orchestration, CI/CD pipelines, SLO/SLA observability, and incident post-mortems.',
        prompt: `You are a lead Site Reliability Engineer (SRE) and DevOps specialist.\n1. Optimize container deployments (Docker Compose, minimal attack surface, non-root).\n2. Implement resilient architectures (circuit breakers, exponential backoff, health probes).\n3. Establish actionable observability (SLOs, Prometheus metrics, structured logging).\n4. Formulate blameless post-mortem analyses with automated regression guards.`
    }
];

function renderPresets() {
    const grid = document.getElementById('gem-presets-grid');
    if (!grid) return;
    grid.innerHTML = GEM_PRESETS.map((p, idx) => `
        <div class="gem-preset-card">
            <div class="gem-preset-top">
                <div class="gem-preset-icon"><i class="${escapeAttr(p.icon)}"></i></div>
                <div class="gem-preset-info">
                    <h5>${escapeHtml(p.name)}</h5>
                    <p>${escapeHtml(p.description)}</p>
                </div>
            </div>
            <button class="btn btn-sm btn-outline gem-preset-use-btn" data-preset-idx="${escapeAttr(idx)}">
                <i class="fas fa-magic"></i> Use Template
            </button>
        </div>
    `).join('');

    grid.querySelectorAll('.gem-preset-use-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const idx = parseInt(btn.dataset.presetIdx, 10);
            const preset = GEM_PRESETS[idx];
            if (preset) {
                openPanel({
                    id: null,
                    name: preset.name,
                    description: preset.description,
                    prompt: preset.prompt
                });
                document.getElementById('gem-name')?.focus();
            }
        });
    });
}

let _gemsControlsBound = false;

export function initGems() {
    renderPresets();
    const addBtn = document.getElementById('gem-add-btn');
    const cancelBtn = document.getElementById('gem-cancel');
    const confirmBtn = document.getElementById('gem-confirm');
    const selectEl = document.getElementById('gem-account-select');

    if (!_gemsControlsBound) {
        if (addBtn) {
            addBtn.addEventListener('click', () => openPanel(null));
        }
        if (cancelBtn) {
            cancelBtn.addEventListener('click', closePanel);
        }
        if (confirmBtn) {
            confirmBtn.addEventListener('click', saveGem);
        }
        if (selectEl) {
            selectEl.addEventListener('change', (e) => {
                currentAccount = e.target.value;
                loadGems();
            });
        }
        if (addBtn && cancelBtn && confirmBtn && selectEl) {
            _gemsControlsBound = true;
        }
    }
}

export async function loadAccountsThenGems() {
    try {
        const sel = document.getElementById('gem-account-select');
        if (!sel) return;
        const data = await apiCall('GET', '/admin/accounts');
        const accounts = (data && Array.isArray(data.accounts)) ? data.accounts : [];
        if (accounts.length === 0) {
            sel.innerHTML = `<option value="">${escapeHtml(t('accounts.noAccounts') || 'No Accounts')}</option>`;
            currentAccount = '';
            const box = document.getElementById('gem-list-container');
            if (box) {
                box.innerHTML = `<div class="empty">${escapeHtml(t('accounts.noAccounts') || 'No accounts available')}</div>`;
            }
            return;
        }
        sel.innerHTML = accounts.map(a =>
            `<option value="${escapeAttr(a.id)}">${escapeHtml(a.label || a.id)}</option>`).join('');
        if (!currentAccount || !accounts.some(a => a.id === currentAccount)) {
            currentAccount = accounts[0].id;
        }
        sel.value = currentAccount;
        await loadGems();
    } catch (e) {
        showToast((t('gems.loadFail') || 'Failed to load gems') + ': ' + e.message, 'error');
    }
}

let currentMappings = {};

function slugify(text) {
    if (!text) return 'gem';
    const normalized = text.normalize('NFD').replace(/[\u0300-\u036f]/g, '');
    const slug = normalized
        .toLowerCase()
        .trim()
        .replace(/[^a-z0-9]+/g, '-')
        .replace(/^-+|-+$/g, '');
    return slug || 'custom';
}

async function loadGems() {
    if (!currentAccount) return;
    const box = document.getElementById('gem-list-container');
    try {
        const [gemsResp, mapResp] = await Promise.all([
            apiCall('GET', `/admin/gems?account_id=${encodeURIComponent(currentAccount)}`),
            apiCall('GET', '/admin/gem-mapping'),
        ]);
        currentMappings = mapResp.mappings || {};
        render(gemsResp.gems || [], currentMappings);
    } catch (e) {
        box.innerHTML = `<div class="error">${escapeHtml(e.message)}</div>`;
    }
}

function modelNameForGem(gemId, mappings) {
    if (!mappings) return '';
    for (const [name, info] of Object.entries(mappings)) {
        if (info.gem_id === gemId && (!currentAccount || !info.account_id || info.account_id === currentAccount)) return name;
    }
    return '';
}

function render(gems, mappings) {
    currentMappings = mappings || {};
    const box = document.getElementById('gem-list-container');
    if (!gems.length) { box.innerHTML = `<div class="empty">${escapeHtml(t('gems.empty'))}</div>`; return; }
    box.innerHTML = gems.map(g => {
        const modelName = modelNameForGem(g.id, mappings);
        const unexposeBtn = modelName
            ? `<button class="btn btn-sm btn-warning gem-unexpose-btn" data-model="${escapeAttr(modelName)}">${escapeHtml(t('gems.unexpose'))}</button>`
            : '';
        const curlBtn = modelName
            ? `<button class="btn btn-sm btn-outline gem-curl-btn" data-model="${escapeAttr(modelName)}" title="Copy cURL snippet"><i class="fas fa-terminal"></i> cURL</button>`
            : '';
        return `<div class="gem-card" data-id="${escapeAttr(g.id)}">
      <div class="gem-info">
        <b>${escapeHtml(g.name)}</b>
        <small>${escapeHtml(g.description || '')}</small>
      </div>
      <div class="gem-expose">
        <input class="gem-model-name" value="${escapeAttr(modelName)}" placeholder="${escapeAttr(t('gems.modelName'))}">
        <button class="btn btn-sm btn-outline gem-expose-btn">${escapeHtml(t('gems.expose'))}</button>
        ${unexposeBtn}
      </div>
      <div class="gem-actions">
        <button class="btn btn-sm btn-primary gem-use-btn" title="Use in Playground"><i class="fas fa-play"></i> Use in Playground</button>
        ${curlBtn}
        <button class="btn btn-sm gem-edit" title="Edit Gem"><i class="fas fa-pen"></i></button>
        <button class="btn btn-sm btn-danger gem-del" title="Delete Gem"><i class="fas fa-trash"></i></button>
      </div>
    </div>`;
    }).join('');
    bindRowEvents(gems);
}

function bindRowEvents(gems) {
    const byId = Object.fromEntries(gems.map(g => [g.id, g]));
    document.querySelectorAll('.gem-card').forEach(card => {
        const id = card.dataset.id;
        const gem = byId[id];
        card.querySelector('.gem-del')?.addEventListener('click', () => removeGem(id));
        card.querySelector('.gem-edit')?.addEventListener('click', () => openPanel(gem));
        card.querySelector('.gem-expose-btn')?.addEventListener('click', () => {
            const name = card.querySelector('.gem-model-name').value.trim();
            exposeGem(id, name);
        });
        card.querySelector('.gem-unexpose-btn')?.addEventListener('click', (e) => {
            const modelName = e.currentTarget.dataset.model;
            unexposeGem(modelName);
        });
        card.querySelector('.gem-use-btn')?.addEventListener('click', () => useGemInPlayground(gem));
        card.querySelector('.gem-curl-btn')?.addEventListener('click', (e) => {
            const model = e.currentTarget.dataset.model;
            copyGemCurl(model);
        });
    });
}

async function copyGemCurl(modelName) {
    if (!modelName) return;
    const origin = (window.location.origin && window.location.origin.startsWith('http'))
        ? window.location.origin
        : 'http://localhost:5918';
    const token = getToken() || 'YOUR_API_KEY';
    const curl = `curl ${origin}/v1/chat/completions \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer ${token}" \\
  -d '{
    "model": "${modelName}",
    "messages": [
      {"role": "user", "content": "Hello!"}
    ]
  }'`;

    const ok = await copyToClipboard(curl);
    if (ok) {
        showToast(`cURL command for "${modelName}" copied to clipboard!`, 'success');
    } else {
        showToast('Failed to copy to clipboard', 'error');
    }
}

async function useGemInPlayground(gem) {
    if (!gem) return;
    let modelName = modelNameForGem(gem.id, currentMappings);
    if (!modelName) {
        const baseSlug = slugify(gem.name);
        let candidate = 'gem-' + baseSlug;
        let counter = 1;
        while (currentMappings[candidate]) {
            candidate = `gem-${baseSlug}-${counter++}`;
        }
        const accountId = currentAccount || document.getElementById('gem-account-select')?.value || '';
        if (!accountId) {
            showToast('No active account selected for Gem mapping', 'error');
            return;
        }
        try {
            await apiCall('POST', '/admin/gem-mapping', {
                model_name: candidate,
                gem_id: gem.id,
                base_model: 'gemini-pro',
                account_id: accountId
            });
            modelName = candidate;
            await loadGems();
        } catch (e) {
            showToast('Failed to map gem model: ' + e.message, 'error');
            return;
        }
    }

    // 1. Navigate to #playground
    const pgNav = document.querySelector('.nav-item[data-section="playground"]');
    if (pgNav) {
        pgNav.click();
    } else {
        window.location.hash = 'playground';
    }

    // 2. Set the #pg-model select to that model name with sync
    if (window.app && typeof window.app.updatePlaygroundModels === 'function') {
        await window.app.updatePlaygroundModels(null, modelName);
    } else {
        const select = document.getElementById('pg-model');
        if (select) {
            let opt = Array.from(select.options).find(o => o.value === modelName);
            if (!opt) {
                opt = document.createElement('option');
                opt.value = modelName;
                opt.textContent = `💎 ${modelName} (Gem)`;
                select.appendChild(opt);
            }
            select.value = modelName;
            select.dispatchEvent(new Event('change'));
        }
    }

    // 3. Focus #pg-message
    const msgInput = document.getElementById('pg-message');
    if (msgInput) {
        setTimeout(() => msgInput.focus(), 100);
    }

    showToast(`Model set to "${modelName}" in Playground`, 'success');
}

function openPanel(gem) {
    const panel = document.getElementById('gem-add-panel');
    if (!panel) return;
    editingGemId = gem ? gem.id : null;
    const nameEl = document.getElementById('gem-name');
    const descEl = document.getElementById('gem-desc');
    const promptEl = document.getElementById('gem-prompt');
    if (nameEl) nameEl.value = gem ? gem.name : '';
    if (descEl) descEl.value = gem ? (gem.description || '') : '';
    if (promptEl) promptEl.value = gem ? (gem.prompt || '') : '';
    panel.style.display = 'block';
}

function closePanel() {
    const panel = document.getElementById('gem-add-panel');
    if (panel) panel.style.display = 'none';
}

async function saveGem() {
    const nameEl = document.getElementById('gem-name');
    const descEl = document.getElementById('gem-desc');
    const promptEl = document.getElementById('gem-prompt');
    if (!currentAccount) {
        showToast(t('gems.noAccountSelected') || 'No account selected', 'error');
        return;
    }
    const body = {
        account_id: currentAccount,
        name: nameEl ? nameEl.value.trim() : '',
        description: descEl ? descEl.value.trim() : '',
        prompt: promptEl ? promptEl.value.trim() : '',
    };
    if (!body.name || !body.prompt) { showToast(t('gems.namePromptRequired'), 'error'); return; }
    try {
        if (editingGemId) await apiCall('PUT', `/admin/gems/${encodeURIComponent(editingGemId)}`, body);
        else await apiCall('POST', '/admin/gems', body);
        showToast(t('gems.saved'), 'success');
        closePanel();
        await loadGems();
    } catch (e) { showToast(t('gems.saveFail') + ': ' + e.message, 'error'); }
}

async function removeGem(gemId) {
    if (!confirm(t('gems.confirmDelete'))) return;
    try {
        await apiCall('DELETE', `/admin/gems/${encodeURIComponent(gemId)}?account_id=${encodeURIComponent(currentAccount)}`);
        showToast(t('gems.deleted'), 'success');
        await loadGems();
    } catch (e) { showToast(e.message, 'error'); }
}

async function exposeGem(gemId, modelName) {
    if (!modelName) { showToast(t('gems.modelNameRequired'), 'error'); return; }
    try {
        await apiCall('POST', '/admin/gem-mapping', {
            model_name: modelName, gem_id: gemId,
            base_model: 'gemini-pro', account_id: currentAccount,
        });
        showToast(t('gems.exposed'), 'success');
        await loadGems();
    } catch (e) { showToast(e.message, 'error'); }
}

async function unexposeGem(modelName) {
    if (!modelName) return;
    try {
        await apiCall('DELETE', '/admin/gem-mapping/' + encodeURIComponent(modelName));
        showToast(t('gems.unexposed'), 'success');
        await loadGems();
    } catch (e) { showToast(e.message, 'error'); }
}
