/**
 * Component Loader - For dynamically loading HTML component fragments
 * Equipped with Granular Section Error Boundaries to prevent whole-shell crashes.
 */

// Component cache
const componentCache = new Map();

/**
 * Load a single component
 * @param {string} componentPath - Component file path
 * @returns {Promise<string>} - Component HTML content
 */
async function loadComponent(componentPath) {
    if (componentCache.has(componentPath)) {
        return componentCache.get(componentPath);
    }

    try {
        const response = await fetch(componentPath);
        if (!response.ok) {
            throw new Error(`Failed to load component: ${componentPath} (${response.status})`);
        }
        const html = await response.text();
        componentCache.set(componentPath, html);
        return html;
    } catch (error) {
        throw error;
    }
}

/**
 * Insert component into specified container with error boundary fallback
 * @param {string} componentPath - Component file path
 * @param {string|HTMLElement} container - Container selector or element
 * @param {string} position - Insertion position: 'replace', 'append', 'prepend', 'beforeend', 'afterbegin'
 * @param {string} fallbackSectionId - Optional section ID for error boundary rendering
 * @returns {Promise<void>}
 */
async function insertComponent(componentPath, container, position = 'beforeend', fallbackSectionId = null) {
    const containerElement = typeof container === 'string'
        ? document.querySelector(container)
        : container;

    if (!containerElement) {
        throw new Error(`Container not found: ${container}`);
    }

    try {
        const html = await loadComponent(componentPath);
        if (position === 'replace') {
            containerElement.innerHTML = html;
        } else {
            containerElement.insertAdjacentHTML(position, html);
        }
    } catch (err) {
        if (fallbackSectionId) {
            const fallbackHtml = `
                <section id="${fallbackSectionId}" class="section error-boundary-section" style="padding: 3rem 1.5rem; text-align: center;">
                    <div style="max-width: 480px; margin: 0 auto; background: var(--bg-secondary, #1e293b); padding: 2rem; border-radius: 12px; border: 1px solid var(--border-color, #334155);">
                        <i class="fas fa-exclamation-triangle" style="font-size: 2.5rem; color: var(--warning-color, #f59e0b); margin-bottom: 1rem;"></i>
                        <h3 style="margin-bottom: 0.5rem; color: var(--text-primary, #f8fafc);">Section Unavailable</h3>
                        <p style="color: var(--text-secondary, #94a3b8); font-size: 0.875rem; margin-bottom: 1.5rem;">Failed to load template for ${fallbackSectionId}. Other sections remain operational.</p>
                        <button class="btn btn-primary btn-sm" data-retry-section="${fallbackSectionId}">
                            <i class="fas fa-redo"></i> Retry
                        </button>
                    </div>
                </section>
            `;
            containerElement.insertAdjacentHTML(position, fallbackHtml);
            const retryBtn = containerElement.querySelector(`[data-retry-section="${fallbackSectionId}"]`);
            if (retryBtn) {
                retryBtn.addEventListener('click', () => {
                    componentCache.clear();
                    window.location.reload();
                });
            }
        } else {
            throw err;
        }
    }
}

/**
 * Batch load multiple components with resilient error boundaries
 * @param {Array<{path: string, container: string, position?: string, fallbackId?: string}>} components - Component configs
 * @returns {Promise<void>}
 */
async function loadComponents(components) {
    const promises = components.map(({ path, container, position, fallbackId }) =>
        insertComponent(path, container, position, fallbackId)
    );
    await Promise.allSettled(promises);
}

/**
 * Initialize page components
 * Loads all page components and inserts them into appropriate layout slots
 * @returns {Promise<void>}
 */
async function initializeComponents() {
    const basePath = 'components/';

    try {
        // Load header first
        await insertComponent(`${basePath}header.html`, '.container', 'afterbegin');

        // Load sidebar next
        await insertComponent(`${basePath}sidebar.html`, '#sidebar-container', 'replace');

        // Load all section components with granular error boundaries
        const sectionComponents = [
            { path: `${basePath}section-dashboard.html`, container: '#content-container', position: 'beforeend', fallbackId: 'dashboard' },
            { path: `${basePath}section-accounts.html`, container: '#content-container', position: 'beforeend', fallbackId: 'accounts' },
            { path: `${basePath}section-playground.html`, container: '#content-container', position: 'beforeend', fallbackId: 'playground' },
            { path: `${basePath}section-usage-stats.html`, container: '#content-container', position: 'beforeend', fallbackId: 'usage-stats' },
            { path: `${basePath}section-logs.html`, container: '#content-container', position: 'beforeend', fallbackId: 'logs' },
            { path: `${basePath}section-babysit.html`, container: '#content-container', position: 'beforeend', fallbackId: 'babysit' },
            { path: `${basePath}section-api-keys.html`, container: '#content-container', position: 'beforeend', fallbackId: 'api-keys' },
            { path: `${basePath}section-gems.html`, container: '#content-container', position: 'beforeend', fallbackId: 'gems' },
            { path: `${basePath}section-settings.html`, container: '#content-container', position: 'beforeend', fallbackId: 'settings' },
        ];

        await loadComponents(sectionComponents);

        window.dispatchEvent(new CustomEvent('componentsLoaded'));

    } catch (error) {
        throw error;
    }
}

/**
 * Clear component cache
 */
function clearComponentCache() {
    componentCache.clear();
}

// Export functions
export {
    loadComponent,
    insertComponent,
    loadComponents,
    initializeComponents,
    clearComponentCache
};
