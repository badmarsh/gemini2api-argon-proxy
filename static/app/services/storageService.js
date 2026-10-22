/**
 * Safe Storage Service
 * Resilient localStorage wrapper with in-memory fallback, quota safety, and sensitive data obfuscation.
 * NOTE: SafeStorage obfuscation is lightweight visual masking for local storage inspection,
 * not a cryptographic confidentiality boundary against arbitrary same-origin script execution (XSS).
 */

class MemoryStorage {
    constructor() {
        this._data = new Map();
    }
    getItem(key) {
        return this._data.has(key) ? this._data.get(key) : null;
    }
    setItem(key, value) {
        this._data.set(key, String(value));
    }
    removeItem(key) {
        this._data.delete(key);
    }
    clear() {
        this._data.clear();
    }
    get length() {
        return this._data.size;
    }
}

class SafeStorage {
    constructor() {
        this._memory = new MemoryStorage();
        this._useMemory = false;
        this._salt = 'g2a-v1-obf';
        this._initCheck();
    }

    _initCheck() {
        try {
            if (typeof window === 'undefined' || !window.localStorage) {
                this._useMemory = true;
                return;
            }
            const testKey = '__storage_test__';
            window.localStorage.setItem(testKey, '1');
            window.localStorage.removeItem(testKey);
        } catch (_) {
            this._useMemory = true;
        }
    }

    _getBackend() {
        return this._useMemory ? this._memory : window.localStorage;
    }

    getItem(key, defaultValue = null) {
        try {
            const raw = this._getBackend().getItem(key);
            if (raw === null || raw === undefined) return defaultValue;
            return raw;
        } catch (_) {
            return defaultValue;
        }
    }

    getJson(key, defaultValue = null) {
        const raw = this.getItem(key);
        if (raw === null) return defaultValue;
        try {
            return JSON.parse(raw);
        } catch (_) {
            return defaultValue;
        }
    }

    setItem(key, value) {
        const valStr = typeof value === 'string' ? value : JSON.stringify(value);
        if (this._useMemory) {
            this._memory.setItem(key, valStr);
            return true;
        }

        try {
            window.localStorage.setItem(key, valStr);
            return true;
        } catch (err) {
            // Check for QuotaExceededError
            if (err.name === 'QuotaExceededError' || err.code === 22 || err.code === 1014) {
                this._handleQuotaExceeded();
                try {
                    window.localStorage.setItem(key, valStr);
                    return true;
                } catch (_) {
                    // Fallback to memory if still full
                    this._memory.setItem(key, valStr);
                    return true;
                }
            }
            // Other storage failure: use memory
            this._memory.setItem(key, valStr);
            return true;
        }
    }

    removeItem(key) {
        try {
            this._getBackend().removeItem(key);
        } catch (_) {
            this._memory.removeItem(key);
        }
    }

    _handleQuotaExceeded() {
        try {
            // Prune oldest non-critical entries (e.g. caches or log buffers)
            const removablePrefixes = ['gemini2api_cache_', 'gemini2api_tmp_', 'gemini2api_logs_'];
            const keysToRemove = [];
            for (let i = 0; i < window.localStorage.length; i++) {
                const k = window.localStorage.key(i);
                if (k && removablePrefixes.some(p => k.startsWith(p))) {
                    keysToRemove.push(k);
                }
            }
            keysToRemove.forEach(k => window.localStorage.removeItem(k));
        } catch (_) {
            // Ignore failure during prune
        }
    }

    /**
     * Encode sensitive credential before persistence (Zero plain-text in browser storage)
     */
    setSecureItem(key, secretValue) {
        if (!secretValue) {
            this.removeItem(key);
            return;
        }
        try {
            const encoder = new TextEncoder();
            const rawBytes = encoder.encode(String(secretValue));
            const saltBytes = encoder.encode(this._salt);
            const masked = new Uint8Array(rawBytes.length);
            for (let i = 0; i < rawBytes.length; i++) {
                masked[i] = rawBytes[i] ^ saltBytes[i % saltBytes.length];
            }
            let binary = '';
            for (let i = 0; i < masked.length; i++) {
                binary += String.fromCharCode(masked[i]);
            }
            this.setItem(key, `enc:v1:${btoa(binary)}`);
        } catch (_) {
            // If encoding fails, fallback to standard memory storage to avoid raw persistence
            this._memory.setItem(key, String(secretValue));
        }
    }

    /**
     * Decode sensitive credential from persistence
     */
    getSecureItem(key) {
        const raw = this.getItem(key);
        if (!raw) return null;
        if (typeof raw === 'string' && raw.startsWith('enc:v1:')) {
            try {
                const b64 = raw.slice(7);
                const binary = atob(b64);
                const encoder = new TextEncoder();
                const saltBytes = encoder.encode(this._salt);
                const unmasked = new Uint8Array(binary.length);
                for (let i = 0; i < binary.length; i++) {
                    unmasked[i] = binary.charCodeAt(i) ^ saltBytes[i % saltBytes.length];
                }
                const decoder = new TextDecoder();
                return decoder.decode(unmasked);
            } catch (_) {
                // Fallback for legacy UTF-8 escape decode
                try {
                    const b64 = raw.slice(7);
                    const decoded = decodeURIComponent(escape(atob(b64)));
                    let legacyUnmasked = '';
                    for (let i = 0; i < decoded.length; i++) {
                        const charCode = decoded.charCodeAt(i) ^ this._salt.charCodeAt(i % this._salt.length);
                        legacyUnmasked += String.fromCharCode(charCode);
                    }
                    return legacyUnmasked;
                } catch (__) {
                    return null;
                }
            }
        }
        // Backward compatibility if plain token was stored previously: migrate to secure
        if (raw && !raw.startsWith('enc:v1:')) {
            this.setSecureItem(key, raw);
        }
        return raw;
    }
}

export const safeStorage = new SafeStorage();
