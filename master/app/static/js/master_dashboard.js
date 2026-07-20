// Master Dashboard JS
// Auth: relies on the HTTP-only session cookie set by /login.
// The cookie is sent automatically with same-origin requests.

const HEADERS = { 'Content-Type': 'application/json' };

// ── State ───────────────────────────────────────────────────
let stores = [];
let selectedStoreId = null;
let currentTab = 'overview';
let costDays = 1;
let usageDays = 1;
const MOBILE_BREAKPOINT = 768;
let lastMobileViewport = window.innerWidth < MOBILE_BREAKPOINT;

function isMobileViewport() {
    return window.innerWidth < MOBILE_BREAKPOINT;
}

// ── Dark mode ───────────────────────────────────────────────
function initDarkMode() {
    const saved = localStorage.getItem('master-dark-mode');
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    if (saved === 'true' || (saved === null && prefersDark)) {
        document.documentElement.classList.add('dark');
    }
    const btn = document.getElementById('dark-toggle');
    if (btn) btn.textContent = document.documentElement.classList.contains('dark') ? '☀️' : '🌙';
}

function toggleDarkMode() {
    const isDark = document.documentElement.classList.toggle('dark');
    localStorage.setItem('master-dark-mode', isDark);
    const btn = document.getElementById('dark-toggle');
    if (btn) btn.textContent = isDark ? '☀️' : '🌙';
}

// ── API helpers ─────────────────────────────────────────────
async function api(path, options = {}) {
    const resp = await fetch(path, {
        credentials: 'same-origin',
        headers: HEADERS,
        ...options,
    });
    if (resp.status === 401) {
        toast('Session expired — redirecting to login...', 'error');
        setTimeout(() => {
            window.location.href = '/login';
        }, 500);
        throw new Error('Unauthorized');
    }
    if (!resp.ok) {
        const err = await resp.text();
        throw new Error(err);
    }
    return resp.json();
}

async function logout() {
    await fetch('/logout', { method: 'POST', credentials: 'same-origin' });
    window.location.href = '/login';
}

async function apiPost(path, body) {
    return api(path, { method: 'POST', body: JSON.stringify(body) });
}

async function apiPut(path, body) {
    return api(path, { method: 'PUT', body: JSON.stringify(body) });
}

async function apiDelete(path) {
    return api(path, { method: 'DELETE' });
}

// ── Toast ───────────────────────────────────────────────────
function toast(msg, type = 'success') {
    const el = document.createElement('div');
    el.className = `toast ${type === 'error' ? 'bg-red-600' : 'bg-green-600'}`;
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 3000);
}

// ── Tab switching ───────────────────────────────────────────
function switchTab(tab) {
    currentTab = tab;
    document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.toggle('hidden', c.id !== `tab-${tab}`));

    if (tab === 'overview') loadStores();
    if (tab === 'audit') loadAuditLog();
}

window.addEventListener('resize', () => {
    const mobile = isMobileViewport();
    if (mobile === lastMobileViewport) return;
    lastMobileViewport = mobile;

    if (currentTab === 'overview' && stores.length) {
        renderStoreCards();
    } else if (currentTab === 'detail' && selectedStoreId) {
        loadStoreDetail();
    } else if (currentTab === 'audit') {
        loadAuditLog();
    }
});

// ── Stores Overview ─────────────────────────────────────────
async function loadStores() {
    try {
        stores = await api('/api/stores/');
        updateHeaderStatus();
        renderStoreCards();
        // Load stats for each store in parallel
        const statsPromises = stores.map(s =>
            api(`/api/stores/${s.id}/stats`).catch(() => ({ error: 'unavailable' }))
        );
        const allStats = await Promise.all(statsPromises);
        stores.forEach((s, i) => s.stats = allStats[i]);
        updateHeaderStatus();
        renderStoreCards();
        // Load platform costs
        loadPlatformCosts();
    } catch (e) {
        toast('Error loading stores: ' + e.message, 'error');
    }
}

function updateHeaderStatus() {
    const total = stores.length;
    const active = stores.filter(s => s.status === 'active').length;
    const healthEl = document.getElementById('master-health-status');
    const storesEl = document.getElementById('master-stores-badge');
    const activeEl = document.getElementById('master-active-badge');
    if (healthEl) healthEl.textContent = active === total && total > 0
        ? 'All systems operational'
        : total === 0 ? 'No stores registered' : `${active}/${total} stores online`;
    if (storesEl) storesEl.textContent = `${total} store${total !== 1 ? 's' : ''}`;
    if (activeEl) activeEl.textContent = `${active} active`;
}

// ── Platform LLM Costs ─────────────────────────────────────
function togglePlatformCosts() {
    const content = document.getElementById('platform-costs-content');
    const btn = document.getElementById('platform-costs-toggle');
    const visible = content.style.display !== 'none';
    content.style.display = visible ? 'none' : 'block';
    btn.innerHTML = visible ? '&#x25BC;' : '&#x25B2;';
}

function setCostDays(d) {
    costDays = d;
    loadPlatformCosts();
}

function rangeToggle(activeVal, options, onClickFn) {
    return `<div class="range-toggle">${options.map(o =>
        `<button class="range-btn ${o.val === activeVal ? 'active' : ''}" onclick="${onClickFn}(${o.val})">${o.label}</button>`
    ).join('')}</div>`;
}

async function loadPlatformCosts() {
    const wrapper = document.getElementById('platform-costs');
    const container = document.getElementById('platform-costs-content');
    if (!wrapper || !container) return;

    const rangeLabel = costDays === 1 ? 'Today' : `Last ${costDays} Days`;

    try {
        const data = await api(`/api/stores/llm-costs/aggregate?days=${costDays}`);
        wrapper.classList.remove('hidden');

        // Update heading
        const heading = wrapper.querySelector('h3');
        if (heading) heading.textContent = `Platform LLM Costs (${rangeLabel})`;

        if (!data.stores || data.stores.length === 0) {
            container.innerHTML = '<p class="text-gray-500">No stores registered.</p>';
            return;
        }

        const storeRows = data.stores.map(s => {
            if (s.error) {
                return `<tr class="border-t dark:border-gray-700">
                    <td class="py-2 font-semibold">${esc(s.store_name)}</td>
                    <td class="py-2 text-red-500" colspan="3">${esc(s.error)}</td>
                </tr>`;
            }
            const detail = (s.breakdown || []).map(b =>
                `<span class="text-xs text-gray-400">${esc(b.provider)}/${esc(b.model)}: ${b.calls} calls</span>`
            ).join(' &bull; ');
            return `<tr class="border-t dark:border-gray-700">
                <td class="py-2">
                    <div class="font-semibold">${esc(s.store_name)}</div>
                    ${detail ? `<div class="mt-1">${detail}</div>` : ''}
                </td>
                <td class="py-2 text-center">${s.calls || 0}</td>
                <td class="py-2 text-right font-mono">$${s.estimated_cost_usd.toFixed(4)}</td>
            </tr>`;
        }).join('');

        container.innerHTML = `
            <div class="flex items-center justify-between mb-3">
                ${rangeToggle(costDays, [{val:1,label:'Today'},{val:7,label:'7 Days'},{val:30,label:'30 Days'}], 'setCostDays')}
                <div class="text-sm text-gray-500">${data.platform_total_calls || 0} total calls</div>
            </div>
            <table class="w-full text-sm">
                <thead>
                    <tr class="text-left border-b dark:border-gray-700">
                        <th class="pb-2">Store</th>
                        <th class="pb-2 text-center">Calls</th>
                        <th class="pb-2 text-right">Cost (USD)</th>
                    </tr>
                </thead>
                <tbody>${storeRows}</tbody>
                <tfoot>
                    <tr class="border-t-2 dark:border-gray-600 font-bold">
                        <td class="pt-3">Platform Total</td>
                        <td class="pt-3"></td>
                        <td class="pt-3 text-right font-mono text-lg">$${data.platform_total_cost_usd.toFixed(4)}</td>
                    </tr>
                </tfoot>
            </table>`;
    } catch (e) {
        wrapper.classList.remove('hidden');
        container.innerHTML = `<p class="text-gray-500">Could not load cost data.</p>`;
    }
}

function renderStoreCards() {
    const container = document.getElementById('stores-grid');
    if (!stores.length) {
        container.innerHTML = `
            <div class="card col-span-full text-center py-12">
                <p class="text-gray-500 dark:text-gray-400 text-lg mb-4">No stores registered yet</p>
                <button onclick="showAddStoreModal()" class="btn btn-primary">Add Your First Store</button>
            </div>`;
        return;
    }

    container.innerHTML = stores.map(s => {
        const stats = s.stats || {};
        const statusClass = s.status === 'active' ? 'active' : s.status === 'paused' ? 'paused' : 'error';
        const lastSeen = s.last_seen ? new Date(s.last_seen).toLocaleString() : 'Never';
        return `
            <div class="card store-card fade-in" onclick="selectStore('${s.id}')">
                <div class="flex items-center justify-between mb-3">
                    <h3 class="font-bold text-lg">${esc(s.name)}</h3>
                    <span class="status-dot ${statusClass}" title="${s.status}"></span>
                </div>
                <p class="text-sm text-gray-500 dark:text-gray-400 mb-1">Owner: ${esc(s.owner_name || '—')}</p>
                <p class="text-sm text-gray-500 dark:text-gray-400 mb-3">Last seen: ${lastSeen}</p>
                ${stats.error ? `<p class="text-sm text-red-500">Stats unavailable</p>` : `
                <div class="grid grid-cols-3 gap-2 text-center">
                    <div>
                        <div class="stat-number text-base">${stats.today_conversations ?? '—'}</div>
                        <div class="text-xs text-gray-500">Chats</div>
                    </div>
                    <div>
                        <div class="stat-number text-base">${stats.today_orders ?? '—'}</div>
                        <div class="text-xs text-gray-500">Orders</div>
                    </div>
                    <div>
                        <div class="stat-number text-base">${stats.total_customers ?? '—'}</div>
                        <div class="text-xs text-gray-500">Customers</div>
                    </div>
                </div>
                <div class="mt-3 flex items-center gap-2">
                    <span class="badge ${stats.ai_enabled === 'true' || stats.ai_enabled === true ? 'badge-green' : 'badge-red'}">
                        AI ${stats.ai_enabled === 'true' || stats.ai_enabled === true ? 'ON' : 'OFF'}
                    </span>
                    <span class="badge badge-blue">${esc(stats.llm_provider || '?')}/${esc(stats.llm_model || '?')}</span>
                    <span class="badge badge-gray">${esc(stats.ai_orchestration_mode || 'legacy')}</span>
                </div>`}
            </div>`;
    }).join('');
}

// ── Store Detail ────────────────────────────────────────────
async function selectStore(storeId) {
    selectedStoreId = storeId;
    switchTab('detail');
    await loadStoreDetail();
}

async function loadStoreDetail() {
    if (!selectedStoreId) return;
    try {
        // Fetch store info, stats, and credentials in parallel
        const [store, stats, creds] = await Promise.all([
            api(`/api/stores/${selectedStoreId}`),
            api(`/api/stores/${selectedStoreId}/stats`).catch(() => ({})),
            api(`/api/stores/${selectedStoreId}/credentials`),
        ]);
        renderStoreDetail(store, stats, creds);
        // Load runtime settings, usage, and Railway status in parallel
        const secondaryLoads = [loadRuntimeSettings(selectedStoreId), loadLLMUsage(selectedStoreId)];
        if (store.railway_service_id) secondaryLoads.push(loadRailwayStatus(selectedStoreId));
        await Promise.all(secondaryLoads);
    } catch (e) {
        toast('Error loading store detail: ' + e.message, 'error');
    }
}

function renderStoreDetail(store, stats, creds) {
    const container = document.getElementById('store-detail-content');
    container.innerHTML = `
        <div class="store-detail-toolbar flex items-center justify-between mb-6">
            <div class="store-detail-copy">
                <button onclick="switchTab('overview')" class="btn btn-secondary mb-2">&larr; Back</button>
                <h2 class="text-2xl font-bold">${esc(store.name)}</h2>
                <p class="text-gray-500 dark:text-gray-400">Owner: ${esc(store.owner_name || '—')} &bull; ${esc(store.owner_contact || '—')}</p>
            </div>
            <div class="store-detail-actions flex gap-2">
                ${store.app_url ? `<a href="${esc(store.app_url)}/admin/dashboard" target="_blank" class="btn btn-primary">Open Store Dashboard &rarr;</a>` : ''}
                <button onclick="showEditStoreModal('${store.id}')" class="btn btn-secondary">Edit</button>
                <button onclick="confirmDeleteStore('${store.id}', '${esc(store.name)}')" class="btn btn-danger">Delete</button>
            </div>
        </div>

        <!-- Stats -->
        ${stats.error ? `<div class="card mb-4"><p class="text-red-500">Stats unavailable: ${esc(stats.error)}</p></div>` : `
        <div class="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
            <div class="card text-center">
                <div class="stat-number">${stats.today_conversations ?? 0}</div>
                <div class="text-sm text-gray-500">Today's Messages</div>
            </div>
            <div class="card text-center">
                <div class="stat-number">${stats.today_orders ?? 0}</div>
                <div class="text-sm text-gray-500">Today's Orders</div>
            </div>
            <div class="card text-center">
                <div class="stat-number">${stats.total_customers ?? 0}</div>
                <div class="text-sm text-gray-500">Total Customers</div>
            </div>
            <div class="card text-center">
                <span id="ai-status-badge" class="badge ${stats.ai_enabled === 'true' || stats.ai_enabled === true ? 'badge-green' : 'badge-red'} text-base cursor-pointer" onclick="toggleStoreAi('${store.id}')" title="Click to toggle AI on/off">
                    AI ${stats.ai_enabled === 'true' || stats.ai_enabled === true ? 'ON' : 'OFF'}
                </span>
                <div class="text-sm text-gray-500 mt-1">${esc(stats.llm_provider || '?')} / ${esc(stats.llm_model || '?')} / ${esc(stats.ai_orchestration_mode || 'legacy')}</div>
            </div>
        </div>`}

        <!-- API Keys -->
        ${renderApiKeysPanel(store, stats, creds)}

        <!-- AI + Usage -->
        <div class="grid md:grid-cols-2 gap-4 mb-4">
            <div class="card" id="ai-settings-panel">
                <div class="flex items-center justify-between mb-3">
                    <h3 class="font-bold text-lg">AI Settings</h3>
                    <button onclick="loadRuntimeSettings('${store.id}')" class="btn btn-secondary text-xs">Refresh</button>
                </div>
                <div id="ai-settings-content">
                    <p class="text-gray-500">Loading AI settings...</p>
                </div>
            </div>
            <div class="card" id="llm-usage-panel">
                <div class="flex items-center justify-between mb-3">
                    <h3 class="font-bold text-lg" id="llm-usage-heading">LLM Usage (Today)</h3>
                    <button onclick="loadLLMUsage('${store.id}')" class="btn btn-secondary text-xs">Refresh</button>
                </div>
                <div id="llm-usage-content">
                    <p class="text-gray-500">Loading usage data...</p>
                </div>
                <div class="mt-4">
                    <button onclick="openConversationViewer('${store.id}')" class="btn btn-primary w-full">View details</button>
                </div>
            </div>
        </div>

        <div class="card mb-4" id="scheduler-settings-panel">
            <div class="flex items-center justify-between mb-3">
                <h3 class="font-bold text-lg">Scheduled Jobs</h3>
                <button onclick="loadRuntimeSettings('${store.id}')" class="btn btn-secondary text-xs">Refresh</button>
            </div>
            <div id="scheduler-settings-content">
                <p class="text-gray-500">Loading scheduler settings...</p>
            </div>
        </div>

        <div class="card mb-4" id="exchange-rates-panel">
            <div class="flex items-center justify-between mb-3">
                <h3 class="font-bold text-lg">Exchange Rates</h3>
                <div class="flex gap-2">
                    <button onclick="loadRuntimeSettings('${store.id}')" class="btn btn-secondary text-xs">Refresh</button>
                    <button id="exchange-rates-refresh-btn" onclick="refreshExchangeRates('${store.id}')" class="btn btn-primary text-xs">Update Now</button>
                </div>
            </div>
            <div id="exchange-rates-content">
                <p class="text-gray-500">Loading exchange rates...</p>
            </div>
        </div>

        <!-- Credentials (grouped) -->
        <div class="card mb-4">
            <div class="flex items-center justify-between mb-4">
                <h3 class="font-bold text-lg">Environment Variables</h3>
                <div class="flex gap-2">
                    <button onclick="showAddCredentialModal('${store.id}')" class="btn btn-primary">+ Add Variable</button>
                    ${creds.length > 0 && store.railway_service_id ? `<button onclick="deployCredentials('${store.id}')" class="btn btn-success" id="deploy-btn">Deploy to Railway</button>` : ''}
                </div>
            </div>
            ${renderGroupedCredentials(store.id, creds)}
        </div>

        <!-- Railway Status -->
        <div class="card mb-4" id="railway-section">
            <div class="flex items-center justify-between mb-3">
                <h3 class="font-bold text-lg">Railway Deployment</h3>
                <button onclick="loadRailwayStatus('${store.id}')" class="btn btn-secondary text-xs">Refresh</button>
            </div>
            <div id="railway-status-content">
                ${store.railway_service_id ?
                    `<p class="text-gray-500">Loading Railway status...</p>` :
                    `<p class="text-gray-500">No Railway service linked. Add a Railway Service ID in store settings to enable deployment management.</p>`}
            </div>
        </div>

        <!-- Store Info -->
        <div class="card">
            <h3 class="font-bold text-lg mb-3">Store Info</h3>
            <div class="grid grid-cols-1 md:grid-cols-2 gap-3 text-sm">
                <div><span class="text-gray-500">App URL:</span> <span class="font-mono">${esc(store.app_url || '—')}</span></div>
                <div><span class="text-gray-500">DB URL:</span> <span class="font-mono">${esc(store.db_url_masked || '—')}</span></div>
                <div><span class="text-gray-500">Railway Service:</span> <span class="font-mono">${esc(store.railway_service_id || '—')}</span></div>
                <div><span class="text-gray-500">Railway Project:</span> <span class="font-mono">${esc(store.railway_project_id || '—')}</span></div>
                <div><span class="text-gray-500">Status:</span> <span class="badge badge-${store.status === 'active' ? 'green' : store.status === 'paused' ? 'yellow' : 'red'}">${store.status}</span></div>
                <div><span class="text-gray-500">Created:</span> ${new Date(store.created_at).toLocaleString()}</div>
            </div>
        </div>`;
}

// ── API Keys Panel ─────────────────────────────────────────
function renderApiKeysPanel(store, stats, creds) {
    const openaiKey = creds.find(c => c.key === 'OPENAI_API_KEY');
    const anthropicKey = creds.find(c => c.key === 'ANTHROPIC_API_KEY');
    const activeProvider = (stats.llm_provider || '').replace(/"/g, '');

    function keyCard(providerName, providerKey, credObj) {
        const isActive = activeProvider === providerKey;
        const isSet = !!credObj;
        const statusClass = isActive ? 'active' : isSet ? 'configured' : 'not-set';
        const badgeClass = isActive ? 'badge-green' : isSet ? 'badge-blue' : 'badge-red';
        const badgeText = isActive ? 'Active' : isSet ? 'Configured' : 'Not Set';
        const envKey = providerKey === 'openai' ? 'OPENAI_API_KEY' : 'ANTHROPIC_API_KEY';

        return `
            <div class="api-key-card ${statusClass}">
                <div class="flex items-center justify-between mb-2">
                    <span class="font-bold">${providerName}</span>
                    <span class="badge ${badgeClass}">${badgeText}</span>
                </div>
                <div class="text-sm text-gray-500 dark:text-gray-400 font-mono mb-3">
                    ${isSet ? esc(credObj.value_masked) : 'No key configured'}
                </div>
                <button onclick="showEditCredentialModal('${store.id}', '${envKey}')" class="btn ${isSet ? 'btn-secondary' : 'btn-primary'} text-xs w-full">
                    ${isSet ? 'Update Key' : 'Set Key'}
                </button>
            </div>`;
    }

    return `
        <div class="card mb-6">
            <div class="flex items-center justify-between mb-4">
                <h3 class="font-bold text-lg">LLM API Keys</h3>
                <span class="text-xs text-gray-400">Deploy to Railway after changes</span>
            </div>
            <div class="grid md:grid-cols-2 gap-4">
                ${keyCard('OpenAI', 'openai', openaiKey)}
                ${keyCard('Anthropic', 'anthropic', anthropicKey)}
            </div>
        </div>`;
}

// ── Grouped Credentials ────────────────────────────────────
const CRED_GROUPS = [
    { name: 'LLM API Keys', test: k => /^(OPENAI_API_KEY|ANTHROPIC_API_KEY)$/.test(k) },
    { name: 'Channel Backend', test: k => /^CHANNEL_BACKEND$/.test(k) },
    { name: 'Kommo', test: k => /^KOMMO_/.test(k) },
    { name: 'Meta Channels', test: k => /^(WHATSAPP_|INSTAGRAM_|META_)/.test(k) },
    { name: 'Telegram', test: k => /^TELEGRAM_/.test(k) },
    { name: 'Infrastructure', test: k => /^(DATABASE_URL|GOOGLE_SHEETS_|APP_BASE_URL|PRODUCT_SHEET_ID)/.test(k) },
    { name: 'Customization', test: k => /^(STORE_NAME|OWNER_NAME|ADMIN_PASSWORD|SYSTEM_PROMPT_OVERRIDE|LLM_MANAGED_EXTERNALLY)/.test(k) },
];

function renderGroupedCredentials(storeId, creds) {
    if (creds.length === 0) {
        return '<p class="text-gray-500">No environment variables configured yet. Add API keys, channel variables, and infrastructure variables to prepare the store.</p>';
    }

    const grouped = {};
    const used = new Set();

    for (const g of CRED_GROUPS) {
        const matches = creds.filter(c => g.test(c.key));
        if (matches.length > 0) {
            grouped[g.name] = matches;
            matches.forEach(c => used.add(c.key));
        }
    }

    const uncategorized = creds.filter(c => !used.has(c.key));
    if (uncategorized.length > 0) {
        grouped['Other'] = uncategorized;
    }

    function credRow(c) {
        return `
            <div class="cred-row">
                <span class="font-mono text-sm font-semibold flex-1">${esc(c.key)}</span>
                <span class="text-sm text-gray-500 font-mono">${esc(c.value_masked)}</span>
                <button onclick="showEditCredentialModal('${storeId}', '${esc(c.key)}')" class="btn btn-secondary text-xs">Edit</button>
                <button onclick="confirmDeleteCredential('${storeId}', '${esc(c.key)}')" class="btn btn-danger text-xs">Delete</button>
            </div>`;
    }

    return Object.entries(grouped).map(([groupName, items]) =>
        `<div class="cred-group-header">${esc(groupName)}</div>` +
        items.map(credRow).join('')
    ).join('');
}

// ── Modals ───────────────────────────────────────────────────
function showModal(html) {
    const backdrop = document.createElement('div');
    backdrop.className = 'modal-backdrop';
    backdrop.onclick = (e) => { if (e.target === backdrop) backdrop.remove(); };
    backdrop.innerHTML = `<div class="modal">${html}</div>`;
    document.body.appendChild(backdrop);
}

function closeModal() {
    document.querySelector('.modal-backdrop')?.remove();
}

function showAddStoreModal() {
    showModal(`
        <h3 class="font-bold text-lg mb-4">Add New Store</h3>
        <form onsubmit="submitAddStore(event)">
            <div class="grid gap-3">
                <div><label class="block text-sm font-semibold mb-1">Store Name *</label>
                    <input name="name" required class="w-full" placeholder="Eva - Tienda de Carla"></div>
                <div><label class="block text-sm font-semibold mb-1">Owner Name</label>
                    <input name="owner_name" class="w-full" placeholder="Carla"></div>
                <div><label class="block text-sm font-semibold mb-1">Owner Contact</label>
                    <input name="owner_contact" class="w-full" placeholder="Phone or email"></div>
                <div><label class="block text-sm font-semibold mb-1">App URL</label>
                    <input name="app_url" class="w-full" placeholder="https://store-carla.railway.app"></div>
                <div><label class="block text-sm font-semibold mb-1">Database URL *</label>
                    <input name="db_url" required class="w-full" placeholder="postgresql://..."></div>
                <div><label class="block text-sm font-semibold mb-1">Railway Service ID</label>
                    <input name="railway_service_id" class="w-full"></div>
                <div><label class="block text-sm font-semibold mb-1">Railway Project ID</label>
                    <input name="railway_project_id" class="w-full"></div>
            </div>
            <div class="flex justify-end gap-2 mt-4">
                <button type="button" onclick="closeModal()" class="btn btn-secondary">Cancel</button>
                <button type="submit" class="btn btn-primary">Add Store</button>
            </div>
        </form>`);
}

async function submitAddStore(e) {
    e.preventDefault();
    const form = e.target;
    const data = Object.fromEntries(new FormData(form));
    try {
        await apiPost('/api/stores/', data);
        closeModal();
        toast('Store added!');
        loadStores();
    } catch (err) {
        toast('Error: ' + err.message, 'error');
    }
}

function showEditStoreModal(storeId) {
    const store = stores.find(s => s.id === storeId);
    if (!store) return;
    showModal(`
        <h3 class="font-bold text-lg mb-4">Edit Store: ${esc(store.name)}</h3>
        <form onsubmit="submitEditStore(event, '${storeId}')">
            <div class="grid gap-3">
                <div><label class="block text-sm font-semibold mb-1">Store Name</label>
                    <input name="name" class="w-full" value="${esc(store.name)}"></div>
                <div><label class="block text-sm font-semibold mb-1">Owner Name</label>
                    <input name="owner_name" class="w-full" value="${esc(store.owner_name || '')}"></div>
                <div><label class="block text-sm font-semibold mb-1">Owner Contact</label>
                    <input name="owner_contact" class="w-full" value="${esc(store.owner_contact || '')}"></div>
                <div><label class="block text-sm font-semibold mb-1">App URL</label>
                    <input name="app_url" class="w-full" value="${esc(store.app_url || '')}"></div>
                <div><label class="block text-sm font-semibold mb-1">Railway Service ID</label>
                    <input name="railway_service_id" class="w-full" value="${esc(store.railway_service_id || '')}"></div>
                <div><label class="block text-sm font-semibold mb-1">Railway Project ID</label>
                    <input name="railway_project_id" class="w-full" value="${esc(store.railway_project_id || '')}"></div>
                <div><label class="block text-sm font-semibold mb-1">Status</label>
                    <select name="status" class="w-full">
                        <option value="active" ${store.status === 'active' ? 'selected' : ''}>Active</option>
                        <option value="paused" ${store.status === 'paused' ? 'selected' : ''}>Paused</option>
                    </select></div>
            </div>
            <div class="flex justify-end gap-2 mt-4">
                <button type="button" onclick="closeModal()" class="btn btn-secondary">Cancel</button>
                <button type="submit" class="btn btn-primary">Save Changes</button>
            </div>
        </form>`);
}

async function submitEditStore(e, storeId) {
    e.preventDefault();
    const data = Object.fromEntries(new FormData(e.target));
    // Remove empty strings so we only update provided fields
    Object.keys(data).forEach(k => { if (!data[k]) delete data[k]; });
    try {
        await apiPut(`/api/stores/${storeId}`, data);
        closeModal();
        toast('Store updated!');
        loadStores();
    } catch (err) {
        toast('Error: ' + err.message, 'error');
    }
}

async function confirmDeleteStore(storeId, name) {
    if (!confirm(`Are you sure you want to delete "${name}"? This will remove the store and all its credentials from the master registry. The store's own database and deployment will NOT be affected.`)) return;
    try {
        await apiDelete(`/api/stores/${storeId}`);
        toast('Store deleted');
        switchTab('overview');
    } catch (err) {
        toast('Error: ' + err.message, 'error');
    }
}

// ── Credential Modals ───────────────────────────────────────
function showAddCredentialModal(storeId) {
    const commonKeys = [
        'CHANNEL_BACKEND',
        'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'DATABASE_URL',
        'WHATSAPP_ACCESS_TOKEN', 'WHATSAPP_PHONE_NUMBER_ID', 'WHATSAPP_VERIFY_TOKEN',
        'META_APP_SECRET', 'INSTAGRAM_ACCESS_TOKEN', 'INSTAGRAM_VERIFY_TOKEN',
        'KOMMO_SUBDOMAIN', 'KOMMO_ACCESS_TOKEN', 'KOMMO_INTEGRATION_ID',
        'KOMMO_INTEGRATION_SECRET', 'KOMMO_SALESBOT_ID', 'KOMMO_WEBHOOK_SECRET',
        'KOMMO_AI_MODE_FIELD_ID', 'KOMMO_AI_ACTIVE_ENUM_ID', 'KOMMO_AI_HUMAN_ENUM_ID',
        'KOMMO_AI_PAUSED_ENUM_ID', 'KOMMO_DEFAULT_RESPONSIBLE_USER_ID',
        'GOOGLE_SHEETS_CREDENTIALS_B64', 'PRODUCT_SHEET_ID',
        'TELEGRAM_BOT_TOKEN', 'TELEGRAM_ADMIN_CHAT_ID',
        'STORE_NAME', 'OWNER_NAME', 'APP_BASE_URL',
        'ADMIN_PASSWORD', 'SYSTEM_PROMPT_OVERRIDE', 'LLM_MANAGED_EXTERNALLY',
    ];
    showModal(`
        <h3 class="font-bold text-lg mb-4">Add Credential</h3>
        <form onsubmit="submitCredential(event, '${storeId}')">
            <div class="grid gap-3">
                <div>
                    <label class="block text-sm font-semibold mb-1">Key</label>
                    <select name="key_select" class="w-full mb-2" onchange="document.querySelector('[name=key]').value = this.value">
                        <option value="">— Select common key or type below —</option>
                        ${commonKeys.map(k => `<option value="${k}">${k}</option>`).join('')}
                    </select>
                    <input name="key" required class="w-full" placeholder="ENV_VAR_NAME">
                </div>
                <div><label class="block text-sm font-semibold mb-1">Value</label>
                    <textarea name="value" required class="w-full" rows="3" placeholder="Secret value..."></textarea></div>
            </div>
            <div class="flex justify-end gap-2 mt-4">
                <button type="button" onclick="closeModal()" class="btn btn-secondary">Cancel</button>
                <button type="submit" class="btn btn-primary">Save Credential</button>
            </div>
        </form>`);
}

function showEditCredentialModal(storeId, key) {
    showModal(`
        <h3 class="font-bold text-lg mb-4">Update: ${esc(key)}</h3>
        <form onsubmit="submitCredential(event, '${storeId}')">
            <input type="hidden" name="key" value="${esc(key)}">
            <div class="mb-3">
                <label class="block text-sm font-semibold mb-1">New Value</label>
                <textarea name="value" required class="w-full" rows="3" placeholder="New value..."></textarea>
            </div>
            <div class="flex justify-end gap-2 mt-4">
                <button type="button" onclick="closeModal()" class="btn btn-secondary">Cancel</button>
                <button type="submit" class="btn btn-primary">Update</button>
            </div>
        </form>`);
}

async function submitCredential(e, storeId) {
    e.preventDefault();
    const data = Object.fromEntries(new FormData(e.target));
    try {
        await apiPost(`/api/stores/${storeId}/credentials`, { key: data.key, value: data.value });
        closeModal();
        toast(`Credential ${data.key} saved!`);
        loadStoreDetail();
    } catch (err) {
        toast('Error: ' + err.message, 'error');
    }
}

async function confirmDeleteCredential(storeId, key) {
    if (!confirm(`Delete credential "${key}"?`)) return;
    try {
        await apiDelete(`/api/stores/${storeId}/credentials/${key}`);
        toast('Credential deleted');
        loadStoreDetail();
    } catch (err) {
        toast('Error: ' + err.message, 'error');
    }
}

function exchangeRateDefinitions() {
    return [
        { key: 'usd_bcv', label: 'Dólar BCV', setting: 'exchange_rate_usd_bcv', effective: 'exchange_rate_usd_bcv_effective_at', fetched: 'exchange_rate_usd_bcv_fetched_at', source: 'exchange_rate_usd_bcv_source', unit: 'USD' },
        { key: 'eur_bcv', label: 'Euro BCV', setting: 'exchange_rate_eur_bcv', effective: 'exchange_rate_eur_bcv_effective_at', fetched: 'exchange_rate_eur_bcv_fetched_at', source: 'exchange_rate_eur_bcv_source', unit: 'EUR' },
        { key: 'usdt_binance', label: 'USDT Binance', setting: 'exchange_rate_usdt_binance', effective: 'exchange_rate_usdt_binance_effective_at', fetched: 'exchange_rate_usdt_binance_fetched_at', source: 'exchange_rate_usdt_binance_source', unit: 'USDT' },
    ];
}

function formatExchangeRateValue(value) {
    const text = String(value ?? '').trim();
    if (!text) return '';
    const numeric = Number(text.replace(',', '.'));
    if (!Number.isFinite(numeric)) return text;
    return new Intl.NumberFormat('es-VE', {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
    }).format(numeric);
}

function formatExchangeRateTimestamp(value) {
    const text = String(value ?? '').trim();
    if (!text) return 'sin fecha';
    const parsed = new Date(text);
    if (Number.isNaN(parsed.getTime())) return text;
    return parsed.toLocaleString('es-VE', { dateStyle: 'short', timeStyle: 'short' });
}

function renderExchangeRatesSettings(s) {
    const selected = s.exchange_rate_reference || 'usd_bcv';
    const rows = exchangeRateDefinitions().map(def => {
        const rate = formatExchangeRateValue(s[def.setting]);
        const source = String(s[def.source] || '').trim() || 'source not recorded';
        const fetched = formatExchangeRateTimestamp(s[def.fetched]);
        const effective = formatExchangeRateTimestamp(s[def.effective]);
        const selectedBadge = selected === def.key ? '<span class="badge badge-blue text-xs">Selected</span>' : '';
        return `
            <div class="exchange-rate-row">
                <div>
                    <div class="font-semibold flex items-center gap-2">${esc(def.label)} ${selectedBadge}</div>
                    <div class="text-xs text-gray-500 dark:text-gray-400">Source: ${esc(source)}</div>
                </div>
                <div class="text-right">
                    <div class="font-mono ${rate ? 'text-gray-900 dark:text-gray-100' : 'text-red-600 dark:text-red-400'}">
                        ${rate ? `${esc(rate)} Bs/${esc(def.unit)}` : 'Not available'}
                    </div>
                    <div class="text-xs text-gray-500 dark:text-gray-400">Updated: ${esc(fetched)}</div>
                    <div class="text-xs text-gray-500 dark:text-gray-400">Effective: ${esc(effective)}</div>
                </div>
            </div>`;
    }).join('');

    const manualRate = String(s.manual_exchange_rate || '').trim();
    const manualNotice = selected === 'manual'
        ? `<div class="mt-3 rounded-lg bg-yellow-50 dark:bg-yellow-950/30 text-yellow-800 dark:text-yellow-200 p-3 text-sm">
            This store currently uses a manual rate${manualRate ? `: ${esc(formatExchangeRateValue(manualRate))} Bs/USD` : ', but no manual value is set'}.
        </div>`
        : '';

    return `
        <div class="space-y-2">${rows}</div>
        <div class="mt-3 pt-3 border-t border-gray-200 dark:border-gray-700 text-sm text-gray-500 dark:text-gray-400">
            Synced to this store: ${esc(formatExchangeRateTimestamp(s.exchange_rates_last_synced_at))}
        </div>
        ${manualNotice}
        <p class="mt-3 text-xs text-gray-500 dark:text-gray-400">Update Now refreshes DolarVZLA centrally and syncs the latest values into this store.</p>`;
}

async function refreshExchangeRates(storeId) {
    const btn = document.getElementById('exchange-rates-refresh-btn');
    const previousText = btn?.textContent;
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Updating...';
    }
    try {
        const result = await apiPost(`/api/stores/${storeId}/exchange-rates/refresh`, {});
        const status = result.refresh?.status || 'unknown';
        const synced = result.sync?.synced ? 'synced to this store' : 'not synced';
        const toastType = status === 'failed' || !result.sync?.synced ? 'error' : 'success';
        toast(`Exchange rates refreshed (${status}) and ${synced}.`, toastType);
        await loadRuntimeSettings(storeId);
    } catch (e) {
        toast('Error updating exchange rates: ' + e.message, 'error');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.textContent = previousText || 'Update Now';
        }
    }
}

// ── Runtime Settings & Usage ───────────────────────────────
async function loadRuntimeSettings(storeId) {
    const aiContainer = document.getElementById('ai-settings-content');
    const schedulerContainer = document.getElementById('scheduler-settings-content');
    const exchangeRatesContainer = document.getElementById('exchange-rates-content');
    if (!aiContainer && !schedulerContainer && !exchangeRatesContainer) return;

    try {
        const data = await api(`/api/stores/${storeId}/settings`);
        const s = data.settings || {};
        const models = data.available_models || {};

        const providerOptions = Object.keys(models).map(p =>
            `<option value="${p}" ${(s.llm_provider || 'openai') === p ? 'selected' : ''}>${p}</option>`
        ).join('');

        const currentProvider = s.llm_provider || 'openai';
        const modelOptions = (models[currentProvider] || []).map(m =>
            `<option value="${m}" ${(s.llm_model || '') === m ? 'selected' : ''}>${m}</option>`
        ).join('');

        const fbProvider = s.fallback_provider || 'anthropic';
        const fbProviderOptions = Object.keys(models).map(p =>
            `<option value="${p}" ${fbProvider === p ? 'selected' : ''}>${p}</option>`
        ).join('');
        const fbModelOptions = (models[fbProvider] || []).map(m =>
            `<option value="${m}" ${(s.fallback_model || '') === m ? 'selected' : ''}>${m}</option>`
        ).join('');
        const orchestrationMode = s.ai_orchestration_mode || 'legacy';

        if (aiContainer) {
            aiContainer.innerHTML = `
            <div class="space-y-3">
                <div class="grid grid-cols-2 gap-3">
                    <div>
                        <label class="block text-xs text-gray-500 mb-1">Provider</label>
                        <select id="llm-provider" class="w-full" onchange="onMasterProviderChange()">
                            ${providerOptions}
                        </select>
                    </div>
                    <div>
                        <label class="block text-xs text-gray-500 mb-1">Model</label>
                        <select id="llm-model" class="w-full">${modelOptions}</select>
                    </div>
                </div>
                <div class="grid grid-cols-3 gap-3">
                    <div>
                        <label class="block text-xs text-gray-500 mb-1">Temperature</label>
                        <input id="llm-temp" type="number" step="0.1" min="0" max="1" value="${s.llm_temperature ?? 0.7}" class="w-full">
                    </div>
                    <div>
                        <label class="block text-xs text-gray-500 mb-1">Max Tokens</label>
                        <input id="llm-max-tokens" type="number" step="50" min="100" max="2000" value="${s.llm_max_tokens ?? 500}" class="w-full">
                    </div>
                    <div>
                        <label class="block text-xs text-gray-500 mb-1">Historial (5–50 msgs)</label>
                        <input id="llm-max-history" type="number" step="1" min="5" max="50" value="${s.max_conversation_history ?? 20}" class="w-full">
                    </div>
                </div>
                <hr class="dark:border-gray-700">
                <div class="grid grid-cols-2 gap-3">
                    <div>
                        <label class="block text-xs text-gray-500 mb-1">Fallback Provider</label>
                        <select id="llm-fb-provider" class="w-full" onchange="onMasterFbProviderChange()">
                            ${fbProviderOptions}
                        </select>
                    </div>
                    <div>
                        <label class="block text-xs text-gray-500 mb-1">Fallback Model</label>
                        <select id="llm-fb-model" class="w-full">${fbModelOptions}</select>
                    </div>
                </div>
                <div class="flex items-center gap-4">
                    <label class="flex items-center gap-2 cursor-pointer">
                        <input id="llm-auto-fallback" type="checkbox" ${s.auto_fallback ? 'checked' : ''}>
                        <span class="text-sm">Auto Fallback</span>
                    </label>
                </div>
                <div>
                    <label class="block text-xs text-gray-500 mb-1">AI Orchestration Mode</label>
                    <select id="ai-orchestration-mode" class="w-full">
                        <option value="legacy" ${orchestrationMode === 'legacy' ? 'selected' : ''}>legacy</option>
                        <option value="shadow" ${orchestrationMode === 'shadow' ? 'selected' : ''}>shadow</option>
                        <option value="multi_agent" ${orchestrationMode === 'multi_agent' ? 'selected' : ''}>multi_agent</option>
                    </select>
                    <p class="text-xs text-gray-500 mt-1">Roll out in order: legacy, shadow, then multi_agent. Legacy is the rollback path.</p>
                </div>
                <button onclick="saveAiSettings('${storeId}')" class="btn btn-primary w-full">Save AI Settings</button>
            </div>`;
        }

        if (schedulerContainer) {
            schedulerContainer.innerHTML = `
            <div class="space-y-4">
                <p class="text-sm text-gray-500">All scheduler times are stored per store and applied by the store app within about one minute. Daily jobs run in UTC.</p>
                <div class="grid md:grid-cols-3 gap-3">
                    <div>
                        <label class="block text-xs text-gray-500 mb-1">Catalog Refresh (minutes)</label>
                        <input id="sched-catalog-refresh" type="number" step="1" min="1" max="1440" value="${s.catalog_refresh_minutes ?? 15}" class="w-full">
                    </div>
                    <div>
                        <label class="block text-xs text-gray-500 mb-1">Broadcast Check (minutes)</label>
                        <input id="sched-broadcast-check" type="number" step="1" min="1" max="60" value="${s.broadcast_check_interval_minutes ?? 1}" class="w-full">
                    </div>
                    <div>
                        <label class="block text-xs text-gray-500 mb-1">Catalog PDF Refresh (hours)</label>
                        <input id="sched-catalog-pdf" type="number" step="1" min="1" max="168" value="${s.catalog_pdf_interval_hours ?? 24}" class="w-full">
                    </div>
                </div>
                <div class="grid md:grid-cols-2 gap-4">
                    <div>
                        <label class="block text-xs text-gray-500 mb-1">Token Reminder (UTC)</label>
                        <div class="grid grid-cols-2 gap-3">
                            <input id="sched-token-hour" type="number" step="1" min="0" max="23" value="${s.token_reminder_hour ?? 3}" class="w-full" placeholder="Hour">
                            <input id="sched-token-minute" type="number" step="1" min="0" max="59" value="${s.token_reminder_minute ?? 0}" class="w-full" placeholder="Minute">
                        </div>
                    </div>
                    <div>
                        <label class="block text-xs text-gray-500 mb-1">Daily Analytics (UTC)</label>
                        <div class="grid grid-cols-2 gap-3">
                            <input id="sched-analytics-hour" type="number" step="1" min="0" max="23" value="${s.daily_analytics_hour ?? 1}" class="w-full" placeholder="Hour">
                            <input id="sched-analytics-minute" type="number" step="1" min="0" max="59" value="${s.daily_analytics_minute ?? 0}" class="w-full" placeholder="Minute">
                        </div>
                    </div>
                </div>
                <button onclick="saveSchedulerSettings('${storeId}')" class="btn btn-primary w-full">Save Scheduled Jobs</button>
            </div>`;
        }

        if (exchangeRatesContainer) {
            exchangeRatesContainer.innerHTML = renderExchangeRatesSettings(s);
        }

        // Store available models globally for provider change handlers
        window._llmModels = models;
    } catch (e) {
        if (aiContainer) {
            aiContainer.innerHTML = `<p class="text-red-500">Could not load AI settings: ${esc(e.message)}</p>`;
        }
        if (schedulerContainer) {
            schedulerContainer.innerHTML = `<p class="text-red-500">Could not load scheduler settings: ${esc(e.message)}</p>`;
        }
        if (exchangeRatesContainer) {
            exchangeRatesContainer.innerHTML = `<p class="text-red-500">Could not load exchange rates: ${esc(e.message)}</p>`;
        }
    }
}

function onMasterProviderChange() {
    const provider = document.getElementById('llm-provider').value;
    const sel = document.getElementById('llm-model');
    sel.innerHTML = (window._llmModels?.[provider] || []).map(m =>
        `<option value="${m}">${m}</option>`
    ).join('');
}

function onMasterFbProviderChange() {
    const provider = document.getElementById('llm-fb-provider').value;
    const sel = document.getElementById('llm-fb-model');
    sel.innerHTML = (window._llmModels?.[provider] || []).map(m =>
        `<option value="${m}">${m}</option>`
    ).join('');
}

async function saveAiSettings(storeId) {
    const payload = {
        llm_provider: document.getElementById('llm-provider').value,
        llm_model: document.getElementById('llm-model').value,
        llm_temperature: parseFloat(document.getElementById('llm-temp').value),
        llm_max_tokens: parseInt(document.getElementById('llm-max-tokens').value),
        fallback_provider: document.getElementById('llm-fb-provider').value,
        fallback_model: document.getElementById('llm-fb-model').value,
        auto_fallback: document.getElementById('llm-auto-fallback').checked,
        max_conversation_history: parseInt(document.getElementById('llm-max-history').value),
        ai_orchestration_mode: document.getElementById('ai-orchestration-mode').value,
    };

    if (isNaN(payload.llm_temperature) || payload.llm_temperature < 0 || payload.llm_temperature > 1) {
        toast('Temperature must be 0.0 - 1.0', 'error'); return;
    }
    if (isNaN(payload.llm_max_tokens) || payload.llm_max_tokens < 100 || payload.llm_max_tokens > 2000) {
        toast('Max tokens must be 100 - 2000', 'error'); return;
    }
    if (isNaN(payload.max_conversation_history) || payload.max_conversation_history < 5 || payload.max_conversation_history > 50) {
        toast('Conversation history must be 5 - 50', 'error'); return;
    }
    try {
        await apiPut(`/api/stores/${storeId}/settings`, payload);
        toast('AI settings saved!');
        await loadStoreDetail();
    } catch (e) {
        toast('Error saving AI settings: ' + e.message, 'error');
    }
}

function parseScheduleValue(id, label, min, max) {
    const value = parseInt(document.getElementById(id).value);
    if (isNaN(value) || value < min || value > max) {
        throw new Error(`${label} must be ${min} - ${max}`);
    }
    return value;
}

async function saveSchedulerSettings(storeId) {
    let payload;
    try {
        payload = {
            catalog_refresh_minutes: parseScheduleValue('sched-catalog-refresh', 'Catalog refresh', 1, 1440),
            broadcast_check_interval_minutes: parseScheduleValue('sched-broadcast-check', 'Broadcast check interval', 1, 60),
            catalog_pdf_interval_hours: parseScheduleValue('sched-catalog-pdf', 'Catalog PDF interval', 1, 168),
            token_reminder_hour: parseScheduleValue('sched-token-hour', 'Token reminder hour', 0, 23),
            token_reminder_minute: parseScheduleValue('sched-token-minute', 'Token reminder minute', 0, 59),
            daily_analytics_hour: parseScheduleValue('sched-analytics-hour', 'Daily analytics hour', 0, 23),
            daily_analytics_minute: parseScheduleValue('sched-analytics-minute', 'Daily analytics minute', 0, 59),
        };
    } catch (e) {
        toast(e.message, 'error');
        return;
    }

    try {
        await apiPut(`/api/stores/${storeId}/settings`, payload);
        toast('Scheduled jobs saved!');
        await loadStoreDetail();
    } catch (e) {
        toast('Error saving scheduled jobs: ' + e.message, 'error');
    }
}

function setUsageDays(d) {
    usageDays = d;
    if (selectedStoreId) loadLLMUsage(selectedStoreId);
}

async function loadLLMUsage(storeId) {
    const container = document.getElementById('llm-usage-content');
    const heading = document.getElementById('llm-usage-heading');
    if (!container) return;

    const rangeLabel = usageDays === 1 ? 'Today' : `Last ${usageDays} Days`;
    if (heading) heading.textContent = `LLM Usage (${rangeLabel})`;

    try {
        const data = await api(`/api/stores/${storeId}/llm-usage?days=${usageDays}`);

        if (data.error) {
            container.innerHTML = `<p class="text-red-500">${esc(data.error)}</p>`;
            return;
        }

        const toggle = rangeToggle(usageDays, [{val:1,label:'Today'},{val:7,label:'7d'},{val:30,label:'30d'}], 'setUsageDays');

        if (!data.breakdown || data.breakdown.length === 0) {
            container.innerHTML = `<div class="mb-3">${toggle}</div><p class="text-gray-500">No API calls in this period.</p>`;
            return;
        }

        const rows = data.breakdown.map(r => `
            <tr class="border-t dark:border-gray-700">
                <td class="py-1">${esc(r.provider)}/${esc(r.model)}</td>
                <td class="py-1 text-center">${r.calls}</td>
                <td class="py-1 text-right font-mono">${r.input_tokens}/${r.output_tokens}</td>
                <td class="py-1 text-right font-mono">$${r.estimated_cost_usd.toFixed(4)}</td>
            </tr>`).join('');

        container.innerHTML = `
            <div class="mb-3">${toggle}</div>
            <table class="w-full text-sm">
                <thead>
                    <tr class="text-left border-b dark:border-gray-700 text-xs text-gray-500">
                        <th class="pb-1">Provider/Model</th>
                        <th class="pb-1 text-center">Calls</th>
                        <th class="pb-1 text-right">In/Out Tokens</th>
                        <th class="pb-1 text-right">Cost</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
                <tfoot>
                    <tr class="border-t-2 dark:border-gray-600 font-bold">
                        <td class="pt-2" colspan="3">Total</td>
                        <td class="pt-2 text-right font-mono">$${data.total_estimated_cost_usd.toFixed(4)}</td>
                    </tr>
                </tfoot>
            </table>`;
    } catch (e) {
        container.innerHTML = `<p class="text-red-500">Could not load usage: ${esc(e.message)}</p>`;
    }
}

// ── Railway Deploy ──────────────────────────────────────────
async function deployCredentials(storeId) {
    if (!confirm('This will push ALL stored credentials to Railway and trigger a redeploy. Continue?')) return;

    const btn = document.getElementById('deploy-btn');
    if (btn) { btn.disabled = true; btn.textContent = 'Deploying...'; }

    try {
        const result = await apiPost(`/api/stores/${storeId}/deploy`, {});
        toast(`Deployed ${result.credentials_pushed} credentials. Redeploy triggered!`);
        loadRailwayStatus(storeId);
    } catch (err) {
        toast('Deploy failed: ' + err.message, 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Deploy Changes'; }
    }
}

async function loadRailwayStatus(storeId) {
    const container = document.getElementById('railway-status-content');
    if (!container) return;

    try {
        const data = await api(`/api/stores/${storeId}/railway/status`);

        if (data.status === 'not_configured') {
            container.innerHTML = `<p class="text-yellow-600">Railway API token not configured. Set RAILWAY_API_TOKEN in master .env.</p>`;
        } else if (data.status === 'not_linked') {
            container.innerHTML = `<p class="text-gray-500">No Railway service linked. Add a Railway Service ID in store settings.</p>`;
        } else if (data.status === 'error') {
            container.innerHTML = `<p class="text-red-500">Railway error: ${esc(data.message)}</p>`;
        } else if (data.status === 'linked') {
            const svc = data.service || {};
            const dep = data.latest_deployment || {};
            const envs = data.environments || [];
            const warning = data.deployment_warning || '';
            const depStatus = dep.status || 'unknown';
            const depBadge = depStatus === 'SUCCESS' ? 'badge-green' :
                             depStatus === 'FAILED' ? 'badge-red' : 'badge-yellow';

            container.innerHTML = `
                <div class="grid grid-cols-1 md:grid-cols-2 gap-3 text-sm">
                    <div><span class="text-gray-500">Service:</span> <span class="font-semibold">${esc(svc.name || svc.id || '—')}</span></div>
                    <div><span class="text-gray-500">Last Deploy:</span> <span class="badge ${depBadge}">${esc(depStatus)}</span>
                        ${dep.createdAt ? `<span class="text-gray-400 ml-2">${new Date(dep.createdAt).toLocaleString()}</span>` : ''}
                    </div>
                    <div><span class="text-gray-500">Environments:</span> ${envs.map(e => `<span class="badge badge-blue mr-1">${esc(e.name)}</span>`).join('') || '—'}</div>
                    <div><span class="text-gray-500">Project ID:</span> <span class="font-mono text-xs">${esc(svc.projectId || '—')}</span></div>
                </div>
                ${warning ? `<p class="mt-3 text-sm text-yellow-600">${esc(warning)}</p>` : ''}`;
        }
    } catch (e) {
        container.innerHTML = `<p class="text-red-500">Could not load Railway status: ${esc(e.message)}</p>`;
    }
}

// ── Audit Log ───────────────────────────────────────────────
const _ACTION_LABELS = {
    create_store: ['Create store', 'badge-green'],
    update_store: ['Update store', 'badge-blue'],
    delete_store: ['Delete store', 'badge-red'],
    set_credential: ['Set credential', 'badge-blue'],
    delete_credential: ['Delete credential', 'badge-red'],
    update_runtime_settings: ['Runtime settings', 'badge-purple'],
    deploy_credentials: ['Deploy', 'badge-green'],
    deploy_failed: ['Deploy failed', 'badge-red'],
    refresh_exchange_rates: ['Exchange rates', 'badge-blue'],
};

function _populateAuditStoreFilter() {
    const sel = document.getElementById('audit-store-filter');
    if (!sel || sel.options.length > 1) return;
    stores.forEach(s => {
        const opt = document.createElement('option');
        opt.value = s.id;
        opt.textContent = s.name;
        sel.appendChild(opt);
    });
}

async function loadAuditLog() {
    _populateAuditStoreFilter();
    const actionFilter = document.getElementById('audit-action-filter')?.value || '';
    const storeFilter = document.getElementById('audit-store-filter')?.value || '';
    const searchFilter = document.getElementById('audit-search')?.value?.trim() || '';
    const dateFrom = document.getElementById('audit-date-from')?.value || '';
    const dateTo = document.getElementById('audit-date-to')?.value || '';

    const params = new URLSearchParams();
    if (actionFilter) params.set('action', actionFilter);
    if (storeFilter) params.set('store_id', storeFilter);
    if (searchFilter) params.set('search', searchFilter);
    if (dateFrom) params.set('date_from', dateFrom);
    if (dateTo) params.set('date_to', dateTo);
    const qs = params.toString();

    try {
        const logs = await api(`/api/stores/audit/log${qs ? '?' + qs : ''}`);
        const container = document.getElementById('audit-log-content');
        if (!logs.length) {
            container.innerHTML = '<p class="text-gray-500">No entries match the current filters.</p>';
            return;
        }
        if (isMobileViewport()) {
            container.innerHTML = `
                <div class="mobile-card-list">
                    ${logs.map(l => {
                        const [label, cls] = _ACTION_LABELS[l.action] || [l.action, 'badge-blue'];
                        return `
                            <div class="card mobile-data-card">
                                <div class="mobile-card-header">
                                    <span class="badge ${cls}">${esc(label)}</span>
                                    <span class="text-xs text-gray-500 dark:text-gray-400">${new Date(l.created_at).toLocaleString()}</span>
                                </div>
                                <div class="text-sm font-semibold text-gray-900 dark:text-gray-100">${esc(l.store_name || '—')}</div>
                                <div class="text-sm text-gray-500 dark:text-gray-400 mt-2">${esc(l.detail || '')}</div>
                            </div>`;
                    }).join('')}
                </div>`;
            return;
        }
        container.innerHTML = `
            <table class="w-full text-sm">
                <thead>
                    <tr class="text-left border-b dark:border-gray-700">
                        <th class="pb-2">Time</th>
                        <th class="pb-2">Action</th>
                        <th class="pb-2">Store</th>
                        <th class="pb-2">Detail</th>
                    </tr>
                </thead>
                <tbody>
                    ${logs.map(l => {
                        const [label, cls] = _ACTION_LABELS[l.action] || [l.action, 'badge-blue'];
                        return `
                        <tr class="border-b dark:border-gray-700">
                            <td class="py-2 text-gray-500 whitespace-nowrap">${new Date(l.created_at).toLocaleString()}</td>
                            <td class="py-2"><span class="badge ${cls}">${esc(label)}</span></td>
                            <td class="py-2">${esc(l.store_name || '—')}</td>
                            <td class="py-2 text-gray-500">${esc(l.detail || '')}</td>
                        </tr>`;
                    }).join('')}
                </tbody>
            </table>`;
    } catch (e) {
        toast('Error loading audit log: ' + e.message, 'error');
    }
}

function toggleAuditFilters() {
    const panel = document.getElementById('audit-filters');
    const btn = document.getElementById('audit-filter-toggle');
    const visible = panel.style.display !== 'none';
    panel.style.display = visible ? 'none' : (isMobileViewport() ? 'grid' : 'flex');
    btn.innerHTML = visible ? 'Filters &#x25BC;' : 'Filters &#x25B2;';
}

function clearAuditFilters() {
    ['audit-action-filter', 'audit-store-filter', 'audit-search', 'audit-date-from', 'audit-date-to']
        .forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
    loadAuditLog();
}

// ── AI Toggle ───────────────────────────────────────────────
async function toggleStoreAi(storeId) {
    const badge = document.getElementById('ai-status-badge');
    const isOn = badge && badge.textContent.trim().includes('ON');
    const newVal = !isOn;
    if (!confirm(`Are you sure you want to turn AI ${newVal ? 'ON' : 'OFF'} for this store?`)) return;
    try {
        await apiPut(`/api/stores/${storeId}/settings`, { ai_enabled: newVal });
        toast(`AI ${newVal ? 'enabled' : 'disabled'} for this store`);
        await loadStoreDetail();
    } catch (e) {
        toast('Error toggling AI: ' + e.message, 'error');
    }
}

// ── Conversation Viewer ─────────────────────────────────────
let _convStoreId = null;
let _convOffset = 0;
const _convLimit = 50;

function openConversationViewer(storeId) {
    _convStoreId = storeId;
    _convOffset = 0;
    // Replace the detail content with the conversation viewer
    const container = document.getElementById('store-detail-content');
    const storeName = stores.find(s => s.id === storeId)?.name || 'Store';
    container.innerHTML = `
        <div class="flex items-center justify-between mb-4">
            <div class="flex items-center gap-3">
                <button onclick="loadStoreDetail()" class="btn btn-secondary">&larr; Back to Store</button>
                <h2 class="text-xl font-bold">Conversations — ${esc(storeName)}</h2>
            </div>
            <button id="conv-filter-toggle" class="btn btn-secondary text-sm" onclick="toggleConvFilters()">Filters &#x25BC;</button>
        </div>
        <div id="conv-filters" class="card mb-4" style="display:none">
            <div class="flex flex-wrap items-center gap-3">
                <div>
                    <label class="block text-xs text-gray-500 mb-1">Customer</label>
                    <select id="conv-customer" class="text-sm rounded border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-200 px-2 py-1" onchange="loadConversations()">
                        <option value="">All customers</option>
                    </select>
                </div>
                <div>
                    <label class="block text-xs text-gray-500 mb-1">Channel</label>
                    <select id="conv-channel" class="text-sm rounded border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-200 px-2 py-1" onchange="loadConversations()">
                        <option value="">All</option>
                        <option value="whatsapp">WhatsApp</option>
                        <option value="instagram">Instagram</option>
                    </select>
                </div>
                <div>
                    <label class="block text-xs text-gray-500 mb-1">From</label>
                    <input id="conv-date-from" type="date" class="text-sm rounded border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-200 px-2 py-1" onchange="loadConversations()">
                </div>
                <div>
                    <label class="block text-xs text-gray-500 mb-1">To</label>
                    <input id="conv-date-to" type="date" class="text-sm rounded border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-200 px-2 py-1" onchange="loadConversations()">
                </div>
                <div>
                    <label class="block text-xs text-gray-500 mb-1">Search</label>
                    <input id="conv-search" type="text" placeholder="Search messages..." class="text-sm rounded border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-200 px-2 py-1 w-40" onkeydown="if(event.key==='Enter')loadConversations()">
                </div>
                <div class="flex items-end">
                    <button class="btn btn-secondary text-sm" onclick="clearConvFilters()">Clear</button>
                </div>
            </div>
        </div>
        <div id="conv-content"><p class="text-gray-500">Loading...</p></div>
        <div id="conv-pagination" class="flex items-center justify-between mt-4" style="display:none"></div>`;
    loadConversations();
}

function toggleConvFilters() {
    const panel = document.getElementById('conv-filters');
    const btn = document.getElementById('conv-filter-toggle');
    const visible = panel.style.display !== 'none';
    panel.style.display = visible ? 'none' : 'block';
    btn.innerHTML = visible ? 'Filters &#x25BC;' : 'Filters &#x25B2;';
}

function clearConvFilters() {
    ['conv-customer', 'conv-channel', 'conv-date-from', 'conv-date-to', 'conv-search']
        .forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
    loadConversations();
}

async function loadConversations() {
    _convOffset = 0;
    await _fetchConversations();
}

async function _fetchConversations() {
    const container = document.getElementById('conv-content');
    if (!container) return;

    const params = new URLSearchParams();
    const custVal = document.getElementById('conv-customer')?.value || '';
    const chanVal = document.getElementById('conv-channel')?.value || '';
    const dfVal = document.getElementById('conv-date-from')?.value || '';
    const dtVal = document.getElementById('conv-date-to')?.value || '';
    const srchVal = document.getElementById('conv-search')?.value?.trim() || '';
    if (custVal) params.set('customer_id', custVal);
    if (chanVal) params.set('channel', chanVal);
    if (dfVal) params.set('date_from', dfVal);
    if (dtVal) params.set('date_to', dtVal);
    if (srchVal) params.set('search', srchVal);
    params.set('limit', _convLimit);
    params.set('offset', _convOffset);

    try {
        const data = await api(`/api/stores/${_convStoreId}/conversations?${params.toString()}`);

        // Populate customer dropdown on first load
        const custSel = document.getElementById('conv-customer');
        if (custSel && custSel.options.length <= 1 && data.customers) {
            data.customers.forEach(c => {
                const opt = document.createElement('option');
                opt.value = c.id;
                opt.textContent = `${c.display_name} (${c.channel})`;
                custSel.appendChild(opt);
            });
        }

        if (!data.messages.length) {
            container.innerHTML = '<p class="text-gray-500">No messages match the current filters.</p>';
            document.getElementById('conv-pagination').style.display = 'none';
            return;
        }

        // Group messages by customer_id into conversation threads
        const grouped = _groupByCustomer(data.messages);
        container.innerHTML = grouped.map(thread => _renderThread(thread)).join('');

        // Pagination
        const pagDiv = document.getElementById('conv-pagination');
        const total = data.total;
        const showing = Math.min(_convOffset + _convLimit, total);
        pagDiv.style.display = 'flex';
        pagDiv.innerHTML = `
            <span class="text-sm text-gray-500">Showing ${_convOffset + 1}–${showing} of ${total}</span>
            <div class="flex gap-2">
                <button class="btn btn-secondary text-sm" onclick="convPage(-1)" ${_convOffset === 0 ? 'disabled' : ''}>&#x25C0; Prev</button>
                <button class="btn btn-secondary text-sm" onclick="convPage(1)" ${showing >= total ? 'disabled' : ''}>Next &#x25B6;</button>
            </div>`;
    } catch (e) {
        container.innerHTML = `<p class="text-red-500">Error: ${esc(e.message)}</p>`;
    }
}

function convPage(dir) {
    _convOffset = Math.max(0, _convOffset + dir * _convLimit);
    _fetchConversations();
}

function _groupByCustomer(messages) {
    // Messages come DESC — reverse to chronological, then group by customer
    const chrono = [...messages].reverse();
    const map = new Map();
    for (const m of chrono) {
        const key = m.customer_id || 'unknown';
        if (!map.has(key)) map.set(key, { customer_id: key, customer_name: m.customer_name, channel: m.channel, messages: [] });
        map.get(key).messages.push(m);
    }
    return [...map.values()];
}

function _renderThread(thread) {
    const channelBadge = thread.channel === 'whatsapp' ? 'badge-green' : 'badge-purple';
    const aiMsgs = thread.messages.filter(m => m.role === 'assistant' && m.usage);
    const threadCost = aiMsgs.reduce((s, m) => s + (m.usage?.estimated_cost_usd || 0), 0);
    const threadTokensIn = aiMsgs.reduce((s, m) => s + (m.usage?.input_tokens || 0), 0);
    const threadTokensOut = aiMsgs.reduce((s, m) => s + (m.usage?.output_tokens || 0), 0);

    const msgs = thread.messages.map(m => {
        const isUser = m.role === 'user';
        const align = isUser ? 'mr-auto' : 'ml-auto';
        const bg = isUser
            ? 'bg-gray-100 dark:bg-gray-700'
            : 'bg-indigo-50 dark:bg-indigo-900/30';
        const label = isUser ? thread.customer_name : 'AI';
        const labelColor = isUser ? 'text-emerald-600 dark:text-emerald-400' : 'text-indigo-600 dark:text-indigo-400';
        const time = new Date(m.created_at).toLocaleString();

        let toolInfo = '';
        if (m.function_calls) {
            try {
                const calls = typeof m.function_calls === 'string' ? JSON.parse(m.function_calls) : m.function_calls;
                if (Array.isArray(calls) && calls.length) {
                    toolInfo = `<div class="mt-1 text-xs text-gray-400"><span class="badge badge-gray">Tools: ${calls.map(c => esc(c.name || c.function || '?')).join(', ')}</span></div>`;
                }
            } catch {}
        }

        let usageInfo = '';
        if (!isUser && m.usage) {
            const u = m.usage;
            usageInfo = `
                <div class="mt-1 flex flex-wrap gap-1">
                    <span class="badge badge-blue text-xs">${esc(u.provider)}/${esc(u.model)}</span>
                    <span class="badge badge-gray text-xs">${u.input_tokens}in / ${u.output_tokens}out</span>
                    <span class="badge badge-yellow text-xs">$${u.estimated_cost_usd.toFixed(6)}</span>
                    ${u.response_time_ms ? `<span class="badge badge-gray text-xs">${u.response_time_ms}ms</span>` : ''}
                    ${u.was_fallback ? '<span class="badge badge-red text-xs">Fallback</span>' : ''}
                </div>`;
        }

        return `
            <div class="${align} max-w-[80%] mb-3">
                <div class="flex items-center gap-2 mb-1">
                    <span class="text-xs font-semibold ${labelColor}">${esc(label)}</span>
                    <span class="text-xs text-gray-400">${time}</span>
                </div>
                <div class="${bg} rounded-lg px-3 py-2 text-sm whitespace-pre-wrap">${esc(m.content)}</div>
                ${toolInfo}
                ${usageInfo}
            </div>`;
    }).join('');

    const costSummary = aiMsgs.length ? `
        <div class="flex flex-wrap gap-2 text-xs">
            <span class="badge badge-yellow">${aiMsgs.length} AI calls &mdash; $${threadCost.toFixed(4)}</span>
            <span class="badge badge-gray">${threadTokensIn} in / ${threadTokensOut} out tokens</span>
        </div>` : '';

    return `
        <div class="card mb-4">
            <div class="flex items-center justify-between mb-3 pb-2 border-b dark:border-gray-700">
                <div class="flex items-center gap-2">
                    <span class="font-semibold">${esc(thread.customer_name)}</span>
                    <span class="badge ${channelBadge} text-xs">${esc(thread.channel)}</span>
                    <span class="text-xs text-gray-400">${thread.messages.length} messages</span>
                </div>
                ${costSummary}
            </div>
            <div class="space-y-1">${msgs}</div>
        </div>`;
}

// ── Utility ─────────────────────────────────────────────────
function esc(s) {
    const d = document.createElement('div');
    d.textContent = s || '';
    return d.innerHTML;
}

// ── Init ────────────────────────────────────────────────────
initDarkMode();
loadStores();
