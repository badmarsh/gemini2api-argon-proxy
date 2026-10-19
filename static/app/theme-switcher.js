/**
 * Theme switcher module
 * Supports 4 modern developer-grade themes:
 * - vercel-dark
 * - vercel-light
 * - supabase-dark
 * - supabase-light
 */

import { safeStorage } from './services/storageService.js';
import { APP_CONSTANTS } from './config/constants.js';

export const THEMES = [
    { id: 'vercel-dark', name: 'Vercel Dark', icon: 'fas fa-moon', metaColor: '#000000', hint: 'Monochrome Dark' },
    { id: 'vercel-light', name: 'Vercel Light', icon: 'fas fa-sun', metaColor: '#ffffff', hint: 'Monochrome Light' },
    { id: 'supabase-dark', name: 'Supabase Dark', icon: 'fas fa-bolt', metaColor: '#1c1c1c', hint: 'Emerald Dark' },
    { id: 'supabase-light', name: 'Supabase Light', icon: 'fas fa-leaf', metaColor: '#ffffff', hint: 'Emerald Light' }
];

const THEME_KEY = APP_CONSTANTS.STORAGE_KEYS.THEME;
export const THEME_LIGHT = 'vercel-light';
export const THEME_DARK = 'vercel-dark';

function normalizeTheme(theme) {
    if (theme === 'dark') return 'vercel-dark';
    if (theme === 'light') return 'vercel-light';
    if (THEMES.some(t => t.id === theme)) return theme;
    return 'vercel-dark';
}

export function getCurrentTheme() {
    const saved = safeStorage.getItem(THEME_KEY);
    if (saved) {
        return normalizeTheme(saved);
    }
    if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {
        return 'vercel-dark';
    }
    return 'vercel-light';
}

export function setTheme(themeId) {
    const normalized = normalizeTheme(themeId);
    const root = document.documentElement;

    root.setAttribute('data-theme', normalized);
    safeStorage.setItem(THEME_KEY, normalized);

    const themeConfig = THEMES.find(t => t.id === normalized) || THEMES[0];
    updateMetaThemeColor(themeConfig.metaColor);
    updateThemeButtonUI(normalized);

    window.dispatchEvent(new CustomEvent('themechange', { detail: { theme: normalized } }));
}

function updateMetaThemeColor(color) {
    const metaThemeColor = document.querySelector('meta[name="theme-color"]');
    if (metaThemeColor) {
        metaThemeColor.setAttribute('content', color);
    }
}

function updateThemeButtonUI(currentThemeId) {
    const btn = document.getElementById('themeToggleBtn');
    if (!btn) return;

    const currentIdx = THEMES.findIndex(t => t.id === currentThemeId);
    const nextTheme = THEMES[(currentIdx + 1) % THEMES.length];
    const curr = THEMES[currentIdx] || THEMES[0];

    btn.title = `Theme: ${curr.name} (Click for ${nextTheme.name})`;
    btn.setAttribute('aria-label', `Current theme: ${curr.name}`);

    // Update icon dynamically
    let iconEl = btn.querySelector('i.theme-icon-indicator');
    if (!iconEl) {
        btn.innerHTML = `<i class="theme-icon-indicator ${curr.icon}"></i>`;
    } else {
        iconEl.className = `theme-icon-indicator ${curr.icon}`;
        const badge = btn.querySelector('.theme-name-badge');
        if (badge) badge.remove();
    }
}

export function toggleTheme() {
    const current = getCurrentTheme();
    const idx = THEMES.findIndex(t => t.id === current);
    const next = THEMES[(idx + 1) % THEMES.length];
    setTheme(next.id);
    return next.id;
}

export function initThemeSwitcher(toggleButtonId = 'themeToggleBtn') {
    const active = getCurrentTheme();
    setTheme(active);

    const toggleBtn = document.getElementById(toggleButtonId);
    if (toggleBtn) {
        toggleBtn.addEventListener('click', (e) => {
            e.preventDefault();
            toggleTheme();
        });
    }

    if (window.matchMedia) {
        const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');
        mediaQuery.addEventListener('change', (e) => {
            const saved = safeStorage.getItem(THEME_KEY);
            if (!saved) {
                setTheme(e.matches ? 'vercel-dark' : 'vercel-light');
            }
        });
    }
}

export function isDarkMode() {
    const t = getCurrentTheme();
    return t.includes('dark');
}
