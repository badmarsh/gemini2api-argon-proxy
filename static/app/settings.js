import { apiCall } from './auth.js';
import { showToast, escapeHtml } from './utils.js';
import { t } from './i18n.js';
import { validateSettingsPayload } from './services/validationService.js';

let originalSettings = {};
let modelMappings = {};

// Double-quote attribute escaping: escapeHtml (via textContent) does not escape ASCII double quotes,
// so directly concatenating into value="..." or data-*="..." could allow `"` injection.
function escapeAttr(str) {
  return escapeHtml(String(str ?? '')).replace(/"/g, '&quot;');
}

function getGroupTitle(groupKey) {
  const map = {
    performance: 'settings.group.performance',
    rate_limiting: 'settings.group.rateLimit',
    health_check: 'settings.group.healthCheck',
    account_management: 'settings.group.accounts',
    usage_stats: 'settings.group.stats',
    chat_cleanup: 'settings.group.chatCleanup',
    logging: 'settings.group.logging',
    argon: 'settings.group.argon'
  };
  return t(map[groupKey] || groupKey);
}

const GROUP_ICONS = {
  performance: 'fa-bolt',
  rate_limiting: 'fa-shield-alt',
  health_check: 'fa-heartbeat',
  account_management: 'fa-users-cog',
  usage_stats: 'fa-chart-line',
  chat_cleanup: 'fa-broom',
  logging: 'fa-file-alt'
};

function getFieldLabel(key) {
  const map = {
    refresh_interval: 'settings.field.refreshInterval',
    max_retries: 'settings.field.maxRetries',
    jitter_enabled: 'settings.field.jitterEnabled',
    rate_limit_enabled: 'settings.field.rateLimitEnabled',
    rate_limit_window: 'settings.field.rateLimitWindow',
    rate_limit_max: 'settings.field.rateLimitMax',
    health_check_enabled: 'settings.field.healthCheckEnabled',
    health_check_interval: 'settings.field.healthCheckInterval',
    rotation_strategy: 'settings.field.rotationStrategy',
    max_concurrent_per_account: 'settings.field.maxConcurrent',
    usage_stats_enabled: 'settings.field.usageStatsEnabled',
    usage_stats_interval: 'settings.field.usageStatsInterval',
    usage_stats_retention_days: 'settings.field.usageStatsRetention',
    version_sync_enabled: 'settings.field.versionSyncEnabled',
    extended_thinking_enabled: 'settings.field.extendedThinkingEnabled',
    chat_cleanup_enabled: 'settings.field.chatCleanupEnabled',
    chat_cleanup_keep_hours: 'settings.field.chatCleanupKeepHours',
    chat_cleanup_interval_hours: 'settings.field.chatCleanupInterval',
    chat_cleanup_skip_pinned: 'settings.field.chatCleanupSkipPinned',
    log_bodies_enabled: 'settings.field.logBodiesEnabled',
    argon_system_prompt_mode: 'settings.field.argonMode',
    argon_timeout: 'settings.field.argonTimeout',
    tool_loop_reminder: 'settings.field.toolLoopReminder',
    tool_loop_stall_guard: 'settings.field.toolLoopGuard'
  };
  return map[key] ? (map[key].startsWith('settings.') ? t(map[key]) : map[key]) : key;
}

// Optional field-level hint explanations.
const FIELD_HINTS = {
  log_bodies_enabled: 'settings.hint.logBodies',
  argon_system_prompt_mode: 'settings.hint.argonMode',
  tool_loop_reminder: 'settings.hint.toolLoopReminder',
  tool_loop_stall_guard: 'settings.hint.toolLoopGuard'
};

function getFieldHint(key) {
  return FIELD_HINTS[key] ? t(FIELD_HINTS[key]) : '';
}

// Enum-valued fields render as a select (typing a raw enum value by hand is how settings get broken).
const SELECT_OPTIONS = {
  argon_system_prompt_mode: [
    { value: 'merge', labelKey: 'settings.mode.merge' },
    { value: 'only_when_none', labelKey: 'settings.mode.onlyWhenNone' },
    { value: 'replace', labelKey: 'settings.mode.replace' }
  ]
};

const ROTATION_OPTIONS = [
  { value: 'round-robin', labelKey: 'settings.strategy.roundRobin' },
  { value: 'failover', labelKey: 'settings.strategy.failover' }
];

// Fields declared as float in backend app/core/settings_overrides.py FIELD_TYPES.
// Kept in sync with backend FIELD_TYPES (guarded by tests/unit/test_settings_float_fields.py).
const FLOAT_FIELDS = new Set([
  'chat_cleanup_keep_hours',
  'chat_cleanup_interval_hours'
]);

function isFloatField(key) {
  return FLOAT_FIELDS.has(String(key).split('.').pop());
}

function createFieldInput(key, value) {
  const type = typeof value;
  const escapedKey = escapeAttr(key);

  if (type === 'boolean') {
    return `
      <label class="toggle-switch">
        <input type="checkbox" data-key="${escapedKey}" ${value ? 'checked' : ''}>
      </label>
    `;
  }

  const fieldName = String(key).split('.').pop();
  if (SELECT_OPTIONS[fieldName]) {
    const options = SELECT_OPTIONS[fieldName].map(opt =>
      `<option value="${escapeAttr(opt.value)}" ${value === opt.value ? 'selected' : ''}>${escapeHtml(t(opt.labelKey))}</option>`
    ).join('');
    return `<select class="form-control" data-key="${escapedKey}">${options}</select>`;
  }

  if (key.endsWith('.rotation_strategy')) {
    const radios = ROTATION_OPTIONS.map(opt =>
      `<label class="radio-option">
        <input type="radio" name="rotation_strategy" data-key="${escapedKey}" value="${escapeAttr(opt.value)}" ${value === opt.value ? 'checked' : ''}>
        <span>${escapeHtml(t(opt.labelKey))}</span>
      </label>`
    ).join('');
    return `<div class="radio-group">${radios}</div>`;
  }

  if (type === 'number') {
    // step is both input constraint and signal for collectFormValues (parseFloat vs parseInt)
    const step = isFloatField(key) ? 'any' : '1';
    return `<input type="number" class="form-control" data-key="${escapedKey}" step="${step}" value="${escapeAttr(value)}">`;
  }

  return `<input type="text" class="form-control" data-key="${escapedKey}" value="${escapeAttr(value)}">`;
}

function renderSettings(settings) {
  const container = document.getElementById('settings-container');
  if (!container) return;

  let html = '';

  for (const [groupKey, groupSettings] of Object.entries(settings)) {
    const groupTitle = getGroupTitle(groupKey);
    const groupIcon = GROUP_ICONS[groupKey] || 'fa-cog';

    html += '<div class="settings-group">';
    html += '<h3><i class="fas ' + escapeAttr(groupIcon) + '"></i> ' + escapeHtml(groupTitle) + '</h3>';
    html += '<div class="settings-fields">';

    for (const [key, value] of Object.entries(groupSettings)) {
      const label = getFieldLabel(key);
      const fullKey = groupKey + '.' + key;
      const input = createFieldInput(fullKey, value);
      const hint = getFieldHint(key);
      html += '<div class="setting-field"><label>' + escapeHtml(label) + '</label>' + input +
        (hint ? '<small class="form-hint">' + escapeHtml(hint) + '</small>' : '') + '</div>';
    }

    html += '</div></div>';
  }

  container.innerHTML = html;
}

function renderModelMapping() {
  const container = document.getElementById('model-mapping-container');
  if (!container) return;

  const entries = Object.entries(modelMappings);

  let html = '<div class="settings-group">';
  html += '<h3><i class="fas fa-exchange-alt"></i> <span data-i18n="settings.modelMapping">' + t('settings.modelMapping') + '</span></h3>';
  html += '<p class="mapping-desc" data-i18n="settings.modelMappingDesc">' + t('settings.modelMappingDesc') + '</p>';
  html += '<div class="mapping-header"><span data-i18n="settings.requestModel">' + t('settings.requestModel') + '</span><span data-i18n="settings.actualModel">' + t('settings.actualModel') + '</span><span></span></div>';

  for (const [alias, target] of entries) {
    // Security: alias/target are user-controlled and persisted; escape before inserting.
    const aliasEsc = escapeAttr(alias);
    const targetEsc = escapeAttr(target);
    html += '<div class="mapping-row" data-alias="' + aliasEsc + '">';
    html += '<input type="text" class="form-control mapping-alias" value="' + aliasEsc + '" readonly>';
    html += '<input type="text" class="form-control mapping-target" value="' + targetEsc + '">';
    html += '<button class="btn-icon btn-delete-mapping" data-alias="' + aliasEsc + '"><i class="fas fa-trash"></i></button>';
    html += '</div>';
  }

  html += '<div class="mapping-row mapping-new">';
  html += '<input type="text" class="form-control" id="new-mapping-alias" data-i18n-placeholder="settings.aliasPlaceholder" placeholder="' + t('settings.aliasPlaceholder') + '">';
  html += '<input type="text" class="form-control" id="new-mapping-target" data-i18n-placeholder="settings.targetPlaceholder" placeholder="' + t('settings.targetPlaceholder') + '">';
  html += '<button class="btn-icon btn-add-mapping" id="btn-add-mapping"><i class="fas fa-plus"></i></button>';
  html += '</div>';
  html += '</div>';
  html += '<div class="mapping-actions">';
  html += '<button class="btn btn-primary" id="btn-save-mapping"><span data-i18n="settings.saveMapping">' + t('settings.saveMapping') + '</span></button>';
  html += '</div>';
  html += '</div>';

  container.innerHTML = html;
  bindMappingEvents();
}

function bindMappingEvents() {
  document.getElementById('btn-add-mapping')?.addEventListener('click', addMapping);
  document.getElementById('btn-save-mapping')?.addEventListener('click', saveAllMappings);
  document.querySelectorAll('.btn-delete-mapping').forEach(btn => {
    btn.addEventListener('click', () => deleteMapping(btn.dataset.alias));
  });
  document.getElementById('new-mapping-target')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') addMapping();
  });
}

async function addMapping() {
  const aliasInput = document.getElementById('new-mapping-alias');
  const targetInput = document.getElementById('new-mapping-target');
  const alias = aliasInput.value.trim();
  const target = targetInput.value.trim();

  if (!alias || !target) {
    showToast('Please fill in alias and target model', 'warning');
    return;
  }
  if (alias === target) {
    showToast('Alias cannot be identical to target', 'warning');
    return;
  }

  try {
    await apiCall('POST', '/admin/model-mapping', { alias, target });
    showToast('Mapping added successfully', 'success');
    await loadModelMapping();
  } catch (error) {
    showToast('Failed to add mapping: ' + error.message, 'error');
  }
}

async function deleteMapping(alias) {
  try {
    await apiCall('DELETE', '/admin/model-mapping/' + encodeURIComponent(alias));
    showToast('Mapping deleted', 'success');
    await loadModelMapping();
  } catch (error) {
    showToast('Failed to delete mapping: ' + error.message, 'error');
  }
}

async function saveAllMappings() {
  const rows = document.querySelectorAll('.mapping-row:not(.mapping-new)');
  let updated = 0;

  for (const row of rows) {
    const alias = row.dataset.alias;
    const target = row.querySelector('.mapping-target').value.trim();
    if (target && target !== modelMappings[alias]) {
      try {
        await apiCall('POST', '/admin/model-mapping', { alias, target });
        updated++;
      } catch (error) {
        showToast('Failed to update ' + alias + ': ' + error.message, 'error');
      }
    }
  }

  if (updated > 0) {
    showToast('Updated ' + updated + ' mapping(s)', 'success');
  } else {
    showToast('No changes', 'info');
  }
  await loadModelMapping();
}

async function loadModelMapping() {
  try {
    const data = await apiCall('GET', '/admin/model-mapping');
    modelMappings = data.mappings || {};
    renderModelMapping();
  } catch (error) {
    showToast('Failed to load model mappings: ' + error.message, 'error');
  }
}

function collectFormValues() {
  const values = {};

  document.querySelectorAll('#settings-container [data-key]').forEach(input => {
    const key = input.dataset.key;

    if (input.type === 'radio') {
      if (input.checked) {
        values[key] = input.value;
      }
      return;
    }

    let value;
    if (input.type === 'checkbox') {
      value = input.checked;
    } else if (input.type === 'number') {
      // Float fields must use parseFloat to prevent truncation of fractional values
      value = isFloatField(key) ? parseFloat(input.value) : parseInt(input.value, 10);
    } else {
      value = input.value;
    }

    values[key] = value;
  });

  return values;
}

function getChangedSettings(current, original) {
  const changed = {};
  for (const [key, value] of Object.entries(current)) {
    if (original[key] !== value) {
      changed[key] = value;
    }
  }
  return changed;
}

function flattenSettings(settings) {
  const flat = {};
  for (const [groupKey, groupSettings] of Object.entries(settings)) {
    for (const [key, value] of Object.entries(groupSettings)) {
      flat[groupKey + '.' + key] = value;
    }
  }
  return flat;
}

export async function loadSettings() {
  try {
    const data = await apiCall('GET', '/admin/settings');
    originalSettings = Object.freeze(flattenSettings(data));
    renderSettings(data);
    await loadModelMapping();
  } catch (error) {
    showToast('Failed to load settings: ' + error.message, 'error');
  }
}

async function saveSettings() {
  const currentValues = collectFormValues();
  const changedSettings = getChangedSettings(currentValues, originalSettings);

  if (Object.keys(changedSettings).length === 0) {
    showToast(t('settings.noChanges') || 'No changes to save', 'info');
    return;
  }

  const apiSettings = {};
  for (const [key, value] of Object.entries(changedSettings)) {
    const fieldName = key.split('.').pop();
    apiSettings[fieldName] = value;
  }

  try {
    validateSettingsPayload({ settings: apiSettings });
    await apiCall('POST', '/admin/settings', { settings: apiSettings });
    showToast(t('settings.saved'), 'success');
    await loadSettings();
  } catch (error) {
    showToast(t('settings.saveFailed') + ': ' + error.message, 'error');
  }
}

async function resetSettings() {
  await loadSettings();
  showToast(t('settings.resetDone'), 'info');
}

export function initSettings() {
  const saveBtn = document.getElementById('settings-save-btn');
  const resetBtn = document.getElementById('settings-reset-btn');

  if (saveBtn) {
    saveBtn.addEventListener('click', saveSettings);
  }

  if (resetBtn) {
    resetBtn.addEventListener('click', resetSettings);
  }
}
