/**
 * Centralized Configuration and Constants
 * Enterprise-grade immutable configuration for Gemini2API Console
 */

function deepFreeze(obj) {
    Object.keys(obj).forEach(prop => {
        if (typeof obj[prop] === 'object' && obj[prop] !== null && !Object.isFrozen(obj[prop])) {
            deepFreeze(obj[prop]);
        }
    });
    return Object.freeze(obj);
}

export const APP_CONSTANTS = deepFreeze({
    VERSION: '1.7.2',
    TIMEOUTS: {
        API_DEFAULT: 30000,       // 30s timeout for standard REST
        STREAM_DEFAULT: 180000,   // 180s to first byte for streaming / deep reasoning
        STREAM_IDLE: 120000,      // 120s without any chunk/keep-alive aborts a stalled stream
        HEALTH_CHECK: 10000,      // 10s timeout for health checks
        POLL_LOGS: 2500,          // 2.5s structured logs polling
        SESSION_IDLE: 300000,     // 5m idle session timeout
        TOAST_AUTO_DISMISS: 4000  // 4s toast duration
    },
    LIMITS: {
        MAX_RING_BUFFER_MESSAGES: 200, // Enforce FIFO limit on chat messages
        MAX_RING_BUFFER_LOGS: 200,     // Enforce FIFO limit on logs
        MAX_MESSAGE_LENGTH: 100000,    // Max characters per playground message
        MAX_LABEL_LENGTH: 128,         // Max label length for accounts/keys
        MAX_EFFORT_LENGTH: 32,         // Max reasoning_effort length
        PAGE_SIZE_LOGS: 100,           // Default logs table page size
        MAX_IMG_PREVIEWS: 5            // Max multimodal images in single message
    },
    MASKING: {
        PREFIX_LEN: 6,
        SUFFIX_LEN: 4,
        MASK_SYMBOL: '...',
        SHORT_KEY_THRESHOLD: 10
    },
    STORAGE_KEYS: {
        TOKEN: 'gemini2api_token',
        THEME: 'gemini2api_theme',
        LANGUAGE: 'gemini2api_language',
        PG_MODEL: 'gemini2api_pg_model',
        PG_THINKING: 'gemini2api_pg_thinking',
        LOGS_PAUSED: 'gemini2api_logs_paused',
        DASHBOARD_REFRESH_PAUSED: 'gemini2api_dashboard_refresh_paused',
        EXPLORER_FILTER: 'gemini2api_explorer_filter'
    },
    ROUTES: {
        HEALTH: '/health',
        ADMIN_VERIFY: '/admin/verify',
        ADMIN_STATUS: '/admin/status',
        ADMIN_SYSTEM_INFO: '/admin/system-info',
        ADMIN_ACCOUNTS: '/admin/accounts',
        ADMIN_API_KEYS: '/admin/api-keys',
        ADMIN_API_KEYS_CATALOG: '/admin/api-keys/catalog',
        ADMIN_GEMS: '/admin/gems',
        ADMIN_LOGS: '/admin/logs',
        ADMIN_LOG_RECORD: '/admin/logs',
        ADMIN_DEBUG_METRICS: '/admin/debug-metrics',
        ADMIN_TOOL_JSON_TEST: '/admin/debug/tool-json',
        ADMIN_CONFIG: '/admin/config',
        ADMIN_METRICS: '/admin/metrics',
        ADMIN_ARGON_RESET: '/admin/argon/reset',
        ADMIN_SETTINGS: '/admin/settings',
        ADMIN_USAGE_SUMMARY: '/admin/usage-stats/summary',
        ADMIN_USAGE_HOURLY: '/admin/usage-stats/history',
        ADMIN_CHECK_UPDATE: '/admin/check-update',
        CHAT_COMPLETIONS: '/openai/v1/chat/completions',
        MODELS: '/openai/v1/models'
    },
    DEFAULT_MODELS: {
        FALLBACK_MODEL: 'gemini-3-flash',
        ARGON_MODEL: 'gemini-4-argon'
    }
});
