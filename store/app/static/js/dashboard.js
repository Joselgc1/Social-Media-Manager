const API = '/admin/settings';
const BROADCAST_API = '/admin/broadcasts';
let MODELS = {};

// -- Authenticated fetch wrapper --
// Sends the session cookie (same-origin) on every admin API call.
// On 401, prompts re-authentication instead of silently failing.
async function apiFetch(url, options = {}) {
  const resp = await fetch(url, {
    credentials: 'same-origin',
    ...options,
    headers: {
      ...options.headers,
    },
  });
  if (resp.status === 401) {
    toast('Sesión expirada — redirigiendo al login...', '#dc2626');
    setTimeout(() => {
      window.location.href = '/admin/login';
    }, 500);
    throw new Error('Unauthorized');
  }
  if (resp.status === 403) {
    toast('Acceso denegado', '#dc2626');
    throw new Error('Forbidden');
  }
  return resp;
}

async function logout() {
  await fetch('/admin/logout', { method: 'POST', credentials: 'same-origin' });
  window.location.href = '/admin/login';
}

// -- Sorting state --
let _customersData = [];
let _ordersData = [];
let _broadcastsData = [];
let _sort = { customers: {col: null, asc: true}, orders: {col: null, asc: true}, broadcasts: {col: null, asc: true} };
const MOBILE_BREAKPOINT = 768;
let _lastMobileViewport = window.innerWidth < MOBILE_BREAKPOINT;
const ORDER_PAYMENT_STATUS_LABELS = {
  pending: 'Pendiente',
  proof_received: 'Comprobante recibido',
  confirmed: 'Confirmado',
  failed: 'Fallido',
  rejected: 'Rechazado',
};
const CUSTOMER_STATE_LABELS = {
  active: 'Activo',
  escalated: 'Escalado',
  blocked: 'Bloqueado',
};
const CUSTOMER_CHANNEL_LABELS = {
  whatsapp: 'WhatsApp',
  instagram: 'Instagram',
};
const DELETE_ICON_SRC = '/static/icons/delete.svg';

function sortData(data, col, asc, getter) {
  return [...data].sort((a, b) => {
    let va = getter(a, col), vb = getter(b, col);
    if (typeof va === 'string') va = va.toLowerCase();
    if (typeof vb === 'string') vb = vb.toLowerCase();
    if (va < vb) return asc ? -1 : 1;
    if (va > vb) return asc ? 1 : -1;
    return 0;
  });
}

function toggleSort(table, col, renderFn, getter) {
  const s = _sort[table];
  if (s.col === col) { s.asc = !s.asc; } else { s.col = col; s.asc = true; }
  const dataMap = { customers: _customersData, orders: _ordersData, broadcasts: _broadcastsData };
  const sorted = sortData(dataMap[table], col, s.asc, getter);
  renderFn(sorted);
}

function sortArrow(table, col) {
  const s = _sort[table];
  if (s.col !== col) return ' ↕';
  return s.asc ? ' ↑' : ' ↓';
}

function isMobileViewport() {
  return window.innerWidth < MOBILE_BREAKPOINT;
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function renderDeleteIcon(label) {
  return `<img src="${DELETE_ICON_SRC}" alt="" class="btn-icon-image"><span class="sr-only">${escapeHtml(label)}</span>`;
}

// -- Dark Mode --
function toggleDarkMode() {
  const isDark = document.documentElement.classList.toggle('dark');
  localStorage.setItem('darkMode', isDark);
  document.getElementById('dark-toggle').textContent = isDark ? '☀️' : '🌙';
}

function initDarkModeButton() {
  const isDark = document.documentElement.classList.contains('dark');
  document.getElementById('dark-toggle').textContent = isDark ? '☀️' : '🌙';
}

// -- Init --
let _llmManagedExternally = false;
let _statsDays = 1;

document.addEventListener('DOMContentLoaded', async () => {
  initDarkModeButton();
  MODELS = await apiFetch(API + '/providers').then(r => r.json());
  const settings = await apiFetch(API + '/').then(r => r.json());
  updateAiToggleUI(settings.ai_enabled !== false);

  if (settings._llm_managed_externally) {
    _llmManagedExternally = true;
    document.getElementById('llm-cost-card')?.remove();
    document.getElementById('llm-usage-card')?.remove();
    const grid = document.getElementById('stats-grid');
    if (grid) {
      grid.classList.remove('md:grid-cols-4');
      grid.classList.add('md:grid-cols-3');
    }
  }

  loadOverview();
  loadSettings();
});

document.addEventListener('click', () => {
  closeOrderStatusDropdowns();
  closeCustomerStateDropdowns();
  closeCustomerChannelDropdowns();
});

window.addEventListener('resize', () => {
  const mobile = isMobileViewport();
  if (mobile === _lastMobileViewport) return;
  _lastMobileViewport = mobile;
  rerenderResponsiveSections();
});

// -- Tabs --
function switchTab(name, el) {
  document.querySelectorAll('.tab-panel').forEach(p => p.style.display = 'none');
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById('tab-' + name).style.display = 'block';
  el.classList.add('active');
  currentTab = name;
  switchTabByName(name);
}

function rerenderResponsiveSections() {
  if (document.getElementById('tab-customers')?.style.display !== 'none' && _customersData.length) {
    renderCustomers(getVisibleCustomers());
  }
  if (document.getElementById('tab-orders')?.style.display !== 'none' && _ordersData.length) {
    renderOrders(getVisibleOrders());
  }
  if (document.getElementById('tab-broadcasts')?.style.display !== 'none' && _broadcastsData.length) {
    renderBroadcasts(_broadcastsData);
  }
}

// -- Refresh --
let currentTab = 'overview';

function refreshCurrentTab() {
  switchTabByName(currentTab);
  toast('Datos actualizados');
}

function switchTabByName(name) {
  if (name === 'overview') loadOverview();
  else if (name === 'customers') loadCustomers();
  else if (name === 'orders') loadOrders();
  else if (name === 'broadcasts') loadBroadcasts();
  else if (name === 'settings') loadSettings();
}

// -- Toast --
function toast(msg, color = '#4f46e5') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.style.background = color;
  el.style.display = 'block';
  setTimeout(() => el.style.display = 'none', 3000);
}

// -- AI Toggle --
let aiEnabled = true;

function updateAiToggleUI(enabled) {
  aiEnabled = enabled;
  const btn = document.getElementById('ai-toggle');
  if (enabled) {
    btn.textContent = '🤖 AI Activo';
    btn.style.background = '#059669';
    btn.style.color = '#fff';
  } else {
    btn.textContent = '⏸️ AI Pausado';
    btn.style.background = '#dc2626';
    btn.style.color = '#fff';
  }
}

async function toggleAi() {
  const newState = !aiEnabled;
  const action = newState ? 'activar' : 'pausar';
  if (!confirm(`¿${newState ? 'Activar' : 'Pausar'} las respuestas automáticas del AI?`)) return;

  await apiFetch(API + '/ai_enabled', {
    method: 'PUT', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({value: newState}),
  });
  updateAiToggleUI(newState);
  toast(newState ? 'AI activado - respuestas automáticas ON' : 'AI pausado - responde manualmente');
}

// -- Overview --
async function loadOverview() {
  // Health
  const health = await fetch('/health').then(r => r.json());
  document.getElementById('health-status').textContent =
    health.status === 'healthy' ? 'Sistema funcionando correctamente' : 'Problemas detectados';
  document.getElementById('provider-badge').textContent =
    (health.active_provider || '?') + ' / ' + (health.active_model || '?');
  document.getElementById('catalog-badge').textContent =
    (health.catalog_products || 0) + ' productos';

  // Stats
  try {
    const stats = await apiFetch(API + '/stats/conversations?days=' + _statsDays).then(r => r.json());
    let totalMsgs = 0, totalCustomers = 0;
    for (const [ch, data] of Object.entries(stats.conversations || {})) {
      totalMsgs += data.total_messages || 0;
      totalCustomers += data.unique_customers || 0;
    }
    document.getElementById('stat-messages').textContent = totalMsgs;
    document.getElementById('stat-customers').textContent = totalCustomers;

    const orders = stats.orders || {};
    document.getElementById('stat-orders').textContent = orders.total_orders || 0;

    // Channel breakdown
    const wa = stats.conversations?.whatsapp || {};
    const ig = stats.conversations?.instagram || {};
    document.getElementById('wa-stats').innerHTML = wa.total_messages
      ? `${wa.unique_customers} clientes, ${wa.customer_messages} recibidos, ${wa.bot_messages} enviados`
      : 'Sin mensajes hoy';
    document.getElementById('ig-stats').innerHTML = ig.total_messages
      ? `${ig.unique_customers} clientes, ${ig.customer_messages} recibidos, ${ig.bot_messages} enviados`
      : 'Sin mensajes hoy';
  } catch(e) { console.error('Stats error:', e); }

  // Usage (hidden when LLM is managed externally)
  if (!_llmManagedExternally) try {
    const usage = await apiFetch(API + '/usage-summary?days=' + _statsDays).then(r => r.json());
    document.getElementById('stat-cost').textContent = '$' + (usage.total_estimated_cost_usd || 0).toFixed(3);

    if (usage.breakdown && usage.breakdown.length) {
      let html = '<table class="w-full"><thead><tr class="text-left text-gray-500 dark:text-gray-400"><th class="pb-2">Proveedor</th><th>Llamadas</th><th>Tokens</th><th>Costo</th></tr></thead><tbody class="dark:text-gray-300">';
      for (const row of usage.breakdown) {
        html += `<tr class="border-t border-gray-100 dark:border-gray-700"><td class="py-2">${row.provider}/${row.model}</td><td>${row.calls}</td><td>${row.input_tokens}/${row.output_tokens}</td><td>$${row.estimated_cost_usd.toFixed(4)}</td></tr>`;
      }
      html += '</tbody></table>';
      document.getElementById('usage-table').innerHTML = html;
    } else {
      document.getElementById('usage-table').textContent = 'Sin uso de LLM hoy';
    }
  } catch(e) { document.getElementById('stat-cost').textContent = '$0'; }
}

function setStatsRange(days) {
  _statsDays = days;

  // Update button styles
  [1, 7, 30].forEach(d => {
    const btn = document.getElementById('range-btn-' + d);
    if (!btn) return;
    btn.className = d === days ? 'btn btn-primary' : 'btn btn-secondary';
  });

  // Update stat card labels
  const suffix = days === 1 ? ' hoy' : ` (${days} días)`;
  const labelMessages = document.getElementById('label-messages');
  const labelCustomers = document.getElementById('label-customers');
  const labelOrders    = document.getElementById('label-orders');
  const labelCost      = document.getElementById('label-cost');
  if (labelMessages) labelMessages.textContent = 'Mensajes' + suffix;
  if (labelCustomers) labelCustomers.textContent = 'Clientes únicos' + suffix;
  if (labelOrders)    labelOrders.textContent    = 'Pedidos' + suffix;
  if (labelCost)      labelCost.textContent      = 'Costo LLM' + suffix;

  loadOverview();
}

// -- Customers --
function customerSortGetter(c, col) {
  if (col === 'name') return getCustomerPrimaryName(c);
  if (col === 'channel') return c.channel || '';
  if (col === 'orders') return c.total_orders || 0;
  if (col === 'spent') return c.total_spent || 0;
  if (col === 'state') return c.conversation_state || 'active';
  if (col === 'last_active') return c.last_active || '';
  return '';
}

function sortCustomers(col) {
  const s = _sort.customers;
  if (s.col === col) s.asc = !s.asc;
  else { s.col = col; s.asc = true; }
  renderCustomers(getVisibleCustomers());
}

async function loadCustomers() {
  try {
    _customersData = await apiFetch(API + '/customers?limit=200').then(r => r.json());
    renderCustomers(getVisibleCustomers());
  } catch(e) {
    document.getElementById('customers-list').textContent = 'Error cargando clientes';
    const countEl = document.getElementById('customers-count');
    if (countEl) countEl.textContent = 'No se pudieron cargar los clientes';
  }
}

function getCustomerPrimaryName(customer) {
  return customer.display_name || getCustomerContactValue(customer) || customer.platform_id || 'Sin nombre';
}

function getCustomerContactValue(customer) {
  if (customer.channel === 'whatsapp') {
    return customer.phone || customer.platform_id || '';
  }
  if (customer.instagram_handle) {
    return customer.instagram_handle.startsWith('@') ? customer.instagram_handle : '@' + customer.instagram_handle;
  }
  return customer.platform_id || '';
}

function getCustomerSecondaryLabel(customer) {
  const contact = getCustomerContactValue(customer);
  const platformId = customer.platform_id || '';
  if (customer.channel === 'whatsapp') {
    return contact && contact !== getCustomerPrimaryName(customer) ? contact : '';
  }
  if (contact && platformId && contact !== platformId) {
    return `${contact} · ID ${platformId}`;
  }
  return platformId && platformId !== getCustomerPrimaryName(customer) ? `ID ${platformId}` : '';
}

function toggleCustomersFilters() {
  const panel = document.getElementById('customers-filters');
  const btn = document.getElementById('customers-filter-toggle');
  if (!panel || !btn) return;

  const visible = panel.style.display !== 'none';
  panel.style.display = visible ? 'none' : 'grid';
  btn.innerHTML = visible ? 'Filtros &#x25BC;' : 'Filtros &#x25B2;';
}

function resetCustomerFilters() {
  const fields = [
    document.getElementById('customer-filter-search'),
    document.getElementById('customer-filter-tag'),
    document.getElementById('customer-filter-channel'),
    document.getElementById('customer-filter-state'),
  ];
  fields.forEach(field => {
    if (!field) return;
    field.value = '';
  });
  applyCustomerFilters();
}

function applyCustomerFilters() {
  closeCustomerStateDropdowns();
  closeCustomerChannelDropdowns();
  renderCustomers(getVisibleCustomers());
}

function getVisibleCustomers() {
  const searchValue = (document.getElementById('customer-filter-search')?.value || '').trim().toLowerCase();
  const tagValue = (document.getElementById('customer-filter-tag')?.value || '').trim().toLowerCase();
  const channelValue = document.getElementById('customer-filter-channel')?.value || '';
  const stateValue = document.getElementById('customer-filter-state')?.value || '';

  let visibleCustomers = (_customersData || []).filter(customer => {
    if (channelValue && (customer.channel || '') !== channelValue) return false;
    if (stateValue && (customer.conversation_state || 'active') !== stateValue) return false;

    const tags = (typeof customer.tags === 'string' ? JSON.parse(customer.tags) : customer.tags) || [];
    if (tagValue && !tags.some(tag => String(tag).toLowerCase().includes(tagValue))) return false;

    if (!searchValue) return true;

    const searchBlob = [
      customer.display_name || '',
      customer.phone || '',
      customer.instagram_handle || '',
      customer.platform_id || '',
      ...tags,
    ].join(' ').toLowerCase();
    return searchBlob.includes(searchValue);
  });

  const sortState = _sort.customers;
  if (sortState.col) {
    visibleCustomers = sortData(visibleCustomers, sortState.col, sortState.asc, customerSortGetter);
  }
  return visibleCustomers;
}

function renderCustomers(customers) {
  const totalCustomers = _customersData.length;
  const countEl = document.getElementById('customers-count');
  if (countEl) {
    countEl.textContent = totalCustomers
      ? `${customers.length} de ${totalCustomers} cliente${totalCustomers === 1 ? '' : 's'}`
      : 'No hay clientes';
  }

  const hasEscalated = _customersData.some(c => c.conversation_state === 'escalated');
  let html = '';
  if (hasEscalated) {
    html += '<div class="mb-3"><button class="btn btn-primary text-sm" onclick="resolveAllCustomers()">Resolver todas las escalaciones</button></div>';
  }
  if (!customers.length) {
    html += '<div class="orders-empty">No hay clientes que coincidan con los filtros.</div>';
    document.getElementById('customers-list').innerHTML = html;
    return;
  }

  if (isMobileViewport()) {
    html += `<div class="mobile-card-list">`;
    for (const c of customers) {
      const tags = (typeof c.tags === 'string' ? JSON.parse(c.tags) : c.tags) || [];
      const primaryName = getCustomerPrimaryName(c);
      const secondaryLabel = getCustomerSecondaryLabel(c);
      const tertiaryLabel = getCustomerContactValue(c);
      const currentState = c.conversation_state || 'active';
      const statusBadge = getCustomerStateBadgeClass(currentState);
      const statusLabel = CUSTOMER_STATE_LABELS[currentState] || currentState || 'Activo';
      const channelLabel = CUSTOMER_CHANNEL_LABELS[c.channel] || c.channel || 'Sin canal';
      const tagHtml = `<div class="customer-tags">${
        tags.map(t =>
          `<span class="badge badge-blue tag-chip" title="Click para eliminar" onclick="removeTag('${c.id}','${t}')">${t} ✕</span>`
        ).join('')
      }<span class="badge badge-gray tag-add-chip" onclick="promptAddTag('${c.id}')" title="Agregar tag">+</span></div>`;

      html += `
        <div class="card mobile-data-card">
          <div class="mobile-card-header">
            <div>
              <div class="font-semibold text-gray-900 dark:text-gray-100">${escapeHtml(primaryName)}</div>
              ${secondaryLabel ? `<div class="text-xs text-gray-500 dark:text-gray-400 mt-1">${escapeHtml(secondaryLabel)}</div>` : ''}
              ${tertiaryLabel && tertiaryLabel !== secondaryLabel ? `<div class="text-xs text-gray-500 dark:text-gray-400 mt-1">${escapeHtml(tertiaryLabel)}</div>` : ''}
            </div>
            <button class="btn btn-danger btn-icon text-xs" onclick="deleteCustomer('${c.id}')" title="Eliminar cliente" aria-label="Eliminar cliente">${renderDeleteIcon('Eliminar cliente')}</button>
          </div>
          <div class="mobile-card-metrics">
            <div class="mobile-card-metric">
              <span class="text-xs text-gray-500 dark:text-gray-400">Canal</span>
              <div class="status-dropdown-wrap mt-1">
                <button type="button" class="badge ${getCustomerChannelBadgeClass(c.channel)} status-pill-button" onclick="event.stopPropagation(); toggleCustomerChannelDropdown('${c.id}')">
                  ${escapeHtml(channelLabel)}
                </button>
                <div id="customer-channel-menu-${c.id}" class="status-dropdown hidden" onclick="event.stopPropagation()">
                  <label class="block text-xs text-gray-500 dark:text-gray-400 mb-2">Cambiar canal</label>
                  <select class="w-full" onchange="changeCustomerChannel('${c.id}', this.value)" onblur="scheduleCloseCustomerChannelDropdown('${c.id}')">
                    ${renderCustomerChannelOptions(c.channel)}
                  </select>
                </div>
              </div>
            </div>
            <div class="mobile-card-metric">
              <span class="text-xs text-gray-500 dark:text-gray-400">Pedidos</span>
              <div class="font-semibold mt-1">${c.total_orders || 0}</div>
            </div>
            <div class="mobile-card-metric">
              <span class="text-xs text-gray-500 dark:text-gray-400">Gastado</span>
              <div class="font-semibold mt-1">$${(c.total_spent || 0).toFixed(2)}</div>
            </div>
            <div class="mobile-card-metric">
              <span class="text-xs text-gray-500 dark:text-gray-400">Estado</span>
              <div class="status-dropdown-wrap mt-1">
                <button type="button" class="badge ${statusBadge} status-pill-button" onclick="event.stopPropagation(); toggleCustomerStateDropdown('${c.id}')">
                  ${escapeHtml(statusLabel)}
                </button>
                <div id="customer-status-menu-${c.id}" class="status-dropdown hidden" onclick="event.stopPropagation()">
                  <label class="block text-xs text-gray-500 dark:text-gray-400 mb-2">Cambiar estado</label>
                  <select class="w-full" onchange="changeCustomerState('${c.id}', this.value)" onblur="scheduleCloseCustomerStateDropdown('${c.id}')">
                    ${renderCustomerStateOptions(currentState)}
                  </select>
                </div>
              </div>
            </div>
          </div>
          <div class="mt-4">
            <div class="text-xs text-gray-500 dark:text-gray-400 mb-2">Tags</div>
            ${tagHtml}
          </div>
        </div>`;
    }
    html += `</div>`;
    document.getElementById('customers-list').innerHTML = html;
    return;
  }

  html += `<table class="w-full orders-table customers-table"><thead><tr class="text-left text-gray-500 dark:text-gray-400 border-b">
    <th class="pb-2 sortable" onclick="sortCustomers('name')">Cliente${sortArrow('customers','name')}</th>
    <th>Tags</th>
    <th class="sortable" onclick="sortCustomers('channel')">Canal${sortArrow('customers','channel')}</th>
    <th class="sortable" onclick="sortCustomers('orders')">Pedidos${sortArrow('customers','orders')}</th>
    <th class="sortable" onclick="sortCustomers('spent')">Gastado${sortArrow('customers','spent')}</th>
    <th class="sortable" onclick="sortCustomers('state')">Estado${sortArrow('customers','state')}</th>
    <th></th></tr></thead><tbody>`;
  for (const c of customers) {
    const tags = (typeof c.tags === 'string' ? JSON.parse(c.tags) : c.tags) || [];
    const primaryName = getCustomerPrimaryName(c);
    const secondaryLabel = getCustomerSecondaryLabel(c);
    const tertiaryLabel = getCustomerContactValue(c);
    const tagHtml = `<div class="customer-tags">${
      tags.map(t =>
        `<span class="badge badge-blue tag-chip" title="Click para eliminar" onclick="removeTag('${c.id}','${t}')">${t} ✕</span>`
      ).join('')
    }<span class="badge badge-gray tag-add-chip" onclick="promptAddTag('${c.id}')" title="Agregar tag">+</span></div>`;
    const currentState = c.conversation_state || 'active';
    const statusBadge = getCustomerStateBadgeClass(currentState);
    const statusLabel = CUSTOMER_STATE_LABELS[currentState] || currentState || 'Activo';
    const contactValue = getCustomerContactValue(c);
    html += `<tr class="border-t border-gray-100 dark:border-gray-700">
      <td class="customer-name-cell">
        <div class="font-medium text-gray-900 dark:text-gray-100">${escapeHtml(primaryName)}</div>
        ${secondaryLabel ? `<div class="text-xs text-gray-500 dark:text-gray-400 mt-1">${escapeHtml(secondaryLabel)}</div>` : ''}
        ${tertiaryLabel && tertiaryLabel !== secondaryLabel ? `<div class="text-xs text-gray-500 dark:text-gray-400 mt-1">${escapeHtml(tertiaryLabel)}</div>` : ''}
      </td>
      <td class="customer-tags-cell">${tagHtml}</td>
      <td class="customer-channel-cell">
        <div class="status-dropdown-wrap">
          <button type="button" class="badge ${getCustomerChannelBadgeClass(c.channel)} status-pill-button" onclick="event.stopPropagation(); toggleCustomerChannelDropdown('${c.id}')">
            ${escapeHtml(CUSTOMER_CHANNEL_LABELS[c.channel] || c.channel || 'Sin canal')}
          </button>
          <div id="customer-channel-menu-${c.id}" class="status-dropdown hidden" onclick="event.stopPropagation()">
            <label class="block text-xs text-gray-500 dark:text-gray-400 mb-2">Cambiar canal</label>
            <select class="w-full" onchange="changeCustomerChannel('${c.id}', this.value)" onblur="scheduleCloseCustomerChannelDropdown('${c.id}')">
              ${renderCustomerChannelOptions(c.channel)}
            </select>
          </div>
        </div>
      </td>
      <td class="customer-number-cell">${c.total_orders || 0}</td>
      <td class="customer-number-cell">$${(c.total_spent || 0).toFixed(2)}</td>
      <td class="customer-state-cell">
        <div class="status-dropdown-wrap">
          <button type="button" class="badge ${statusBadge} status-pill-button" onclick="event.stopPropagation(); toggleCustomerStateDropdown('${c.id}')">
            ${escapeHtml(statusLabel)}
          </button>
          <div id="customer-status-menu-${c.id}" class="status-dropdown hidden" onclick="event.stopPropagation()">
            <label class="block text-xs text-gray-500 dark:text-gray-400 mb-2">Cambiar estado</label>
            <select class="w-full" onchange="changeCustomerState('${c.id}', this.value)" onblur="scheduleCloseCustomerStateDropdown('${c.id}')">
              ${renderCustomerStateOptions(currentState)}
            </select>
          </div>
        </div>
      </td>
      <td class="customer-actions-cell">
        <button class="btn btn-danger btn-icon text-xs" onclick="deleteCustomer('${c.id}')" title="Eliminar cliente" aria-label="Eliminar cliente">${renderDeleteIcon('Eliminar cliente')}</button>
      </td>
    </tr>`;
  }
  html += '</tbody></table>';
  document.getElementById('customers-list').innerHTML = html;
}

function getCustomerStateBadgeClass(state) {
  return {active:'badge-green', escalated:'badge-red', blocked:'badge-yellow'}[state || 'active'] || 'badge-gray';
}

function getCustomerChannelBadgeClass(channel) {
  return {whatsapp:'badge-green', instagram:'badge-blue'}[channel] || 'badge-gray';
}

function renderCustomerStateOptions(current) {
  const options = ['active', 'escalated', 'blocked'];
  return options.map(state =>
    `<option value="${state}" ${state === current ? 'selected' : ''}>${CUSTOMER_STATE_LABELS[state] || state}</option>`
  ).join('');
}

function renderCustomerChannelOptions(current) {
  const options = ['whatsapp', 'instagram'];
  return options.map(channel =>
    `<option value="${channel}" ${channel === current ? 'selected' : ''}>${CUSTOMER_CHANNEL_LABELS[channel] || channel}</option>`
  ).join('');
}

function toggleCustomerStateDropdown(customerId) {
  const menu = document.getElementById(`customer-status-menu-${customerId}`);
  if (!menu) return;

  const isHidden = menu.classList.contains('hidden');
  closeCustomerStateDropdowns(customerId);
  if (isHidden) {
    menu.classList.remove('hidden');
    const select = menu.querySelector('select');
    window.setTimeout(() => select?.focus(), 0);
  }
}

function closeCustomerStateDropdown(customerId) {
  const menu = document.getElementById(`customer-status-menu-${customerId}`);
  if (menu) menu.classList.add('hidden');
}

function closeCustomerStateDropdowns(exceptCustomerId = null) {
  document.querySelectorAll('[id^="customer-status-menu-"]').forEach(menu => {
    if (exceptCustomerId && menu.id === `customer-status-menu-${exceptCustomerId}`) return;
    menu.classList.add('hidden');
  });
}

function scheduleCloseCustomerStateDropdown(customerId) {
  window.setTimeout(() => closeCustomerStateDropdown(customerId), 150);
}

function toggleCustomerChannelDropdown(customerId) {
  const menu = document.getElementById(`customer-channel-menu-${customerId}`);
  if (!menu) return;

  const isHidden = menu.classList.contains('hidden');
  closeCustomerChannelDropdowns(customerId);
  if (isHidden) {
    menu.classList.remove('hidden');
    const select = menu.querySelector('select');
    window.setTimeout(() => select?.focus(), 0);
  }
}

function closeCustomerChannelDropdown(customerId) {
  const menu = document.getElementById(`customer-channel-menu-${customerId}`);
  if (menu) menu.classList.add('hidden');
}

function closeCustomerChannelDropdowns(exceptCustomerId = null) {
  document.querySelectorAll('[id^="customer-channel-menu-"]').forEach(menu => {
    if (exceptCustomerId && menu.id === `customer-channel-menu-${exceptCustomerId}`) return;
    menu.classList.add('hidden');
  });
}

function scheduleCloseCustomerChannelDropdown(customerId) {
  window.setTimeout(() => closeCustomerChannelDropdown(customerId), 150);
}

async function removeTag(customerId, tag) {
  if (!confirm(`¿Eliminar tag "${tag}"?`)) return;
  await apiFetch(API + '/customers/' + customerId + '/tags/' + encodeURIComponent(tag), {method: 'DELETE'});
  toast('Tag eliminado');
  loadCustomers();
}

function promptAddTag(customerId) {
  const input = prompt('Tags a agregar (separados por coma):');
  if (!input) return;
  const tags = input.split(',').map(t => t.trim()).filter(Boolean);
  if (!tags.length) return;
  apiFetch(API + '/customers/' + customerId + '/tags', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({tags}),
  }).then(() => { toast('Tags agregados'); loadCustomers(); });
}

async function resolveCustomer(id) {
  if (!confirm('¿Resolver esta escalación? El AI volverá a responder a este cliente.')) return;
  await apiFetch(API + '/customers/' + id + '/resolve', {method: 'POST'});
  toast('Escalación resuelta');
  loadCustomers();
}

async function resolveAllCustomers() {
  if (!confirm('¿Resolver TODAS las escalaciones? El AI volverá a responder a todos los clientes.')) return;
  const resp = await apiFetch(API + '/customers/resolve-all', {method: 'POST'}).then(r => r.json());
  toast(`${resp.resolved} escalación(es) resuelta(s)`);
  loadCustomers();
}

async function changeCustomerChannel(customerId, channel) {
  closeCustomerChannelDropdown(customerId);

  const customer = _customersData.find(item => item.id === customerId);
  if (!customer || customer.channel === channel) return;

  const previousChannel = customer.channel;
  const resp = await apiFetch(API + '/customers/' + customerId, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({channel}),
  });

  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    toast(data.detail || 'Error actualizando canal', '#dc2626');
    return;
  }

  const payload = await resp.json().catch(() => ({}));
  Object.assign(customer, payload.customer || {channel});
  renderCustomers(getVisibleCustomers());
  toast('Canal actualizado');
}

async function changeCustomerState(customerId, conversationState) {
  closeCustomerStateDropdown(customerId);

  const customer = _customersData.find(item => item.id === customerId);
  if (!customer || customer.conversation_state === conversationState) return;

  const resp = await apiFetch(API + '/customers/' + customerId, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({conversation_state: conversationState}),
  });

  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    toast(data.detail || 'Error actualizando estado', '#dc2626');
    return;
  }

  const payload = await resp.json().catch(() => ({}));
  Object.assign(customer, payload.customer || {conversation_state: conversationState});
  renderCustomers(getVisibleCustomers());
  toast('Estado actualizado');
}

async function deleteCustomer(customerId) {
  closeCustomerStateDropdowns();
  if (!confirm('¿Eliminar este cliente? Sus conversaciones se borrarán y sus pedidos quedarán sin cliente asociado.')) return;

  const resp = await apiFetch(API + '/customers/' + customerId, {method: 'DELETE'});
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    toast(data.detail || 'Error eliminando cliente', '#dc2626');
    return;
  }

  _customersData = _customersData.filter(customer => customer.id !== customerId);
  renderCustomers(getVisibleCustomers());
  toast('Cliente eliminado');
}

// -- Orders --
function orderSortGetter(o, col) {
  if (col === 'name') return o.display_name || o.platform_id || '';
  if (col === 'total') return o.total || 0;
  if (col === 'payment') return o.payment_method || '';
  if (col === 'status') return o.payment_status || '';
  if (col === 'date') return o.created_at || '';
  return '';
}

function sortOrders(col) {
  const s = _sort.orders;
  if (s.col === col) s.asc = !s.asc;
  else { s.col = col; s.asc = true; }
  renderOrders(getVisibleOrders());
}

async function loadOrders() {
  try {
    _ordersData = await apiFetch(API + '/orders').then(r => r.json());
    populateOrderFilters(_ordersData);
    renderOrders(getVisibleOrders());
  } catch(e) {
    document.getElementById('orders-list').textContent = 'Error cargando pedidos';
    const countEl = document.getElementById('orders-count');
    if (countEl) countEl.textContent = 'No se pudieron cargar los pedidos';
  }
}

function populateOrderFilters(orders) {
  const paymentSelect = document.getElementById('order-filter-payment');
  const statusSelect = document.getElementById('order-filter-status');
  if (!paymentSelect || !statusSelect) return;

  const previousPayment = paymentSelect.value;
  const previousStatus = statusSelect.value;

  const paymentMethods = [...new Set((orders || []).map(o => (o.payment_method || '').trim()).filter(Boolean))].sort((a, b) => a.localeCompare(b));
  paymentSelect.innerHTML = `<option value="">Todos</option>${paymentMethods.map(method =>
    `<option value="${escapeHtml(method)}">${escapeHtml(method)}</option>`
  ).join('')}`;
  paymentSelect.value = paymentMethods.includes(previousPayment) ? previousPayment : '';

  const statuses = [...new Set((orders || []).map(o => o.payment_status || '').filter(Boolean))];
  const orderedStatuses = Object.keys(ORDER_PAYMENT_STATUS_LABELS).filter(status => statuses.includes(status));
  statusSelect.innerHTML = `<option value="">Todos</option>${orderedStatuses.map(status =>
    `<option value="${status}">${ORDER_PAYMENT_STATUS_LABELS[status] || status}</option>`
  ).join('')}`;
  statusSelect.value = orderedStatuses.includes(previousStatus) ? previousStatus : '';
}

function resetOrderFilters() {
  const search = document.getElementById('order-filter-search');
  const payment = document.getElementById('order-filter-payment');
  const status = document.getElementById('order-filter-status');
  if (search) search.value = '';
  if (payment) payment.value = '';
  if (status) status.value = '';
  applyOrderFilters();
}

function toggleOrdersFilters() {
  const panel = document.getElementById('orders-filters');
  const btn = document.getElementById('orders-filter-toggle');
  if (!panel || !btn) return;

  const visible = panel.style.display !== 'none';
  panel.style.display = visible ? 'none' : 'grid';
  btn.innerHTML = visible ? 'Filtros &#x25BC;' : 'Filtros &#x25B2;';
}

function applyOrderFilters() {
  closeOrderStatusDropdowns();
  renderOrders(getVisibleOrders());
}

function getVisibleOrders() {
  const searchValue = (document.getElementById('order-filter-search')?.value || '').trim().toLowerCase();
  const paymentValue = document.getElementById('order-filter-payment')?.value || '';
  const statusValue = document.getElementById('order-filter-status')?.value || '';

  let visibleOrders = (_ordersData || []).filter(order => {
    if (paymentValue && (order.payment_method || '') !== paymentValue) return false;
    if (statusValue && (order.payment_status || '') !== statusValue) return false;

    if (!searchValue) return true;

    const items = (typeof order.items === 'string' ? JSON.parse(order.items) : order.items) || [];
    const searchBlob = [
      order.display_name || '',
      order.platform_id || '',
      order.payment_method || '',
      ...items.map(item => `${item.product_name || ''} ${item.sku || ''} ${item.size || ''}`),
    ].join(' ').toLowerCase();

    return searchBlob.includes(searchValue);
  });

  const sortState = _sort.orders;
  if (sortState.col) {
    visibleOrders = sortData(visibleOrders, sortState.col, sortState.asc, orderSortGetter);
  }
  return visibleOrders;
}

function renderOrders(orders) {
  const totalOrders = _ordersData.length;
  const countEl = document.getElementById('orders-count');
  if (countEl) {
    countEl.textContent = totalOrders
      ? `${orders.length} de ${totalOrders} pedido${totalOrders === 1 ? '' : 's'}`
      : 'No hay pedidos';
  }

  if (!orders.length) {
    document.getElementById('orders-list').innerHTML = `<div class="orders-empty">No hay pedidos que coincidan con los filtros.</div>`;
    return;
  }

  if (isMobileViewport()) {
    let html = `<div class="mobile-card-list">`;
    for (const o of orders) {
      const items = (typeof o.items === 'string' ? JSON.parse(o.items) : o.items) || [];
      const itemSummary = items.map(i => {
        const size = i.size ? ` (${i.size})` : '';
        return `${i.product_name || i.sku || 'Producto'}${size}`;
      }).join(', ');
      const statusBadge = getOrderStatusBadgeClass(o.payment_status);
      const statusLabel = ORDER_PAYMENT_STATUS_LABELS[o.payment_status] || o.payment_status || 'Sin estado';
      const date = new Date(o.created_at).toLocaleDateString();
      html += `
        <div class="card mobile-data-card order-clickable-card" onclick="openOrderDetail('${o.id}')">
          <div class="mobile-card-header">
            <div>
              <div class="font-semibold text-gray-900 dark:text-gray-100">${escapeHtml(o.display_name || o.platform_id)}</div>
              <div class="text-xs text-gray-500 dark:text-gray-400 mt-1">${date}</div>
            </div>
            <div class="flex gap-2">
              <button class="btn btn-secondary text-xs" onclick="event.stopPropagation(); openOrderDetail('${o.id}')">Ver</button>
              <button class="btn btn-danger btn-icon text-xs" onclick="event.stopPropagation(); deleteOrder('${o.id}')" title="Eliminar pedido" aria-label="Eliminar pedido">${renderDeleteIcon('Eliminar pedido')}</button>
            </div>
          </div>
          <div class="text-sm text-gray-700 dark:text-gray-300 mt-3">${escapeHtml(itemSummary)}</div>
          <div class="mobile-card-metrics mt-4">
            <div class="mobile-card-metric">
              <span class="text-xs text-gray-500 dark:text-gray-400">Total</span>
              <div class="font-semibold mt-1">$${(o.total || 0).toFixed(2)}</div>
            </div>
            <div class="mobile-card-metric">
              <span class="text-xs text-gray-500 dark:text-gray-400">Pago</span>
              <div class="font-semibold mt-1">${escapeHtml(o.payment_method || '-')}</div>
            </div>
          </div>
          <div class="mt-4">
            <div class="text-xs text-gray-500 dark:text-gray-400 mb-2">Estado de pago</div>
            <div class="status-dropdown-wrap">
              <button type="button" class="badge ${statusBadge} status-pill-button" onclick="event.stopPropagation(); toggleOrderStatusDropdown('${o.id}')">
                ${escapeHtml(statusLabel)}
              </button>
              <div id="order-status-menu-${o.id}" class="status-dropdown hidden" onclick="event.stopPropagation()">
                <label class="block text-xs text-gray-500 dark:text-gray-400 mb-2">Cambiar estado</label>
                <select id="order-payment-${o.id}" class="w-full" onchange="changeOrderStatus('${o.id}', this.value)" onblur="scheduleCloseOrderStatusDropdown('${o.id}')">
                  ${renderOrderPaymentOptions(o.payment_status)}
                </select>
              </div>
            </div>
          </div>
        </div>`;
    }
    html += `</div>`;
    document.getElementById('orders-list').innerHTML = html;
    return;
  }

  let html = `<table class="w-full orders-table"><thead><tr class="text-left text-gray-500 dark:text-gray-400 border-b">
    <th class="pb-2 sortable" onclick="sortOrders('name')">Cliente${sortArrow('orders','name')}</th>
    <th>Items</th>
    <th class="sortable" onclick="sortOrders('total')">Total${sortArrow('orders','total')}</th>
    <th class="sortable" onclick="sortOrders('payment')">Pago${sortArrow('orders','payment')}</th>
    <th class="sortable" onclick="sortOrders('status')">Estado pago${sortArrow('orders','status')}</th>
    <th class="sortable" onclick="sortOrders('date')">Fecha${sortArrow('orders','date')}</th>
    <th></th>
  </tr></thead><tbody>`;
  for (const o of orders) {
    const items = (typeof o.items === 'string' ? JSON.parse(o.items) : o.items) || [];
    const itemSummary = items.map(i => {
      const size = i.size ? ` (${i.size})` : '';
      return `${i.product_name || i.sku || 'Producto'}${size}`;
    }).join(', ');
    const statusBadge = getOrderStatusBadgeClass(o.payment_status);
    const statusLabel = ORDER_PAYMENT_STATUS_LABELS[o.payment_status] || o.payment_status || 'Sin estado';
    const date = new Date(o.created_at).toLocaleDateString();
    html += `<tr class="border-t border-gray-100 dark:border-gray-700 order-clickable-row" onclick="openOrderDetail('${o.id}')">
      <td class="py-2"><span class="font-medium text-gray-900 dark:text-gray-100">${escapeHtml(o.display_name || o.platform_id)}</span></td>
      <td class="order-items-cell" title="${escapeHtml(itemSummary)}">${escapeHtml(itemSummary)}</td>
      <td>$${(o.total || 0).toFixed(2)}</td>
      <td>${escapeHtml(o.payment_method || '-')}</td>
      <td>
        <div class="status-dropdown-wrap">
          <button type="button" class="badge ${statusBadge} status-pill-button" onclick="event.stopPropagation(); toggleOrderStatusDropdown('${o.id}')">
            ${escapeHtml(statusLabel)}
          </button>
          <div id="order-status-menu-${o.id}" class="status-dropdown hidden" onclick="event.stopPropagation()">
            <label class="block text-xs text-gray-500 dark:text-gray-400 mb-2">Cambiar estado</label>
            <select id="order-payment-${o.id}" class="w-full" onchange="changeOrderStatus('${o.id}', this.value)" onblur="scheduleCloseOrderStatusDropdown('${o.id}')">
              ${renderOrderPaymentOptions(o.payment_status)}
            </select>
          </div>
        </div>
      </td>
      <td>${date}</td>
      <td class="order-actions-cell">
        <div class="flex gap-2 justify-end">
          <button class="btn btn-secondary text-xs" onclick="event.stopPropagation(); openOrderDetail('${o.id}')">Ver</button>
          <button class="btn btn-danger btn-icon text-xs" onclick="event.stopPropagation(); deleteOrder('${o.id}')" title="Eliminar pedido" aria-label="Eliminar pedido">${renderDeleteIcon('Eliminar pedido')}</button>
        </div>
      </td>
    </tr>`;
  }
  html += '</tbody></table>';
  document.getElementById('orders-list').innerHTML = html;
}

function openOrderDetail(orderId) {
  window.location.href = `/admin/orders/${encodeURIComponent(orderId)}`;
}

function getOrderStatusBadgeClass(status) {
  return {pending:'badge-yellow', proof_received:'badge-blue', confirmed:'badge-green', failed:'badge-red', rejected:'badge-red'}[status] || 'badge-gray';
}

function renderOrderPaymentOptions(current) {
  const options = ['pending', 'proof_received', 'confirmed', 'failed', 'rejected'];
  return options.map(status =>
    `<option value="${status}" ${status === current ? 'selected' : ''}>${ORDER_PAYMENT_STATUS_LABELS[status] || status}</option>`
  ).join('');
}

function toggleOrderStatusDropdown(orderId) {
  const menu = document.getElementById(`order-status-menu-${orderId}`);
  if (!menu) return;

  const isHidden = menu.classList.contains('hidden');
  closeOrderStatusDropdowns(orderId);
  if (isHidden) {
    menu.classList.remove('hidden');
    const select = menu.querySelector('select');
    window.setTimeout(() => select?.focus(), 0);
  }
}

function closeOrderStatusDropdown(orderId) {
  const menu = document.getElementById(`order-status-menu-${orderId}`);
  if (menu) menu.classList.add('hidden');
}

function closeOrderStatusDropdowns(exceptOrderId = null) {
  document.querySelectorAll('[id^="order-status-menu-"]').forEach(menu => {
    if (exceptOrderId && menu.id === `order-status-menu-${exceptOrderId}`) return;
    menu.classList.add('hidden');
  });
}

function scheduleCloseOrderStatusDropdown(orderId) {
  window.setTimeout(() => closeOrderStatusDropdown(orderId), 150);
}

async function changeOrderStatus(orderId, paymentStatus) {
  closeOrderStatusDropdown(orderId);

  const order = _ordersData.find(item => item.id === orderId);
  if (!order || order.payment_status === paymentStatus) return;

  const resp = await apiFetch(API + '/orders/' + orderId, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      payment_status: paymentStatus,
    }),
  });

  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    toast(data.detail || 'Error actualizando pedido', '#dc2626');
    return;
  }

  order.payment_status = paymentStatus;
  renderOrders(getVisibleOrders());
  toast('Estado actualizado');
}

async function deleteOrder(orderId) {
  closeOrderStatusDropdowns();
  if (!confirm('¿Eliminar este pedido? Esto también restaurará el stock del catálogo.')) return;

  const resp = await apiFetch(API + '/orders/' + orderId, {method: 'DELETE'});
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    toast(data.detail || 'Error eliminando pedido', '#dc2626');
    return;
  }

  toast('Pedido eliminado');
  _ordersData = _ordersData.filter(order => order.id !== orderId);
  populateOrderFilters(_ordersData);
  renderOrders(getVisibleOrders());
}

// -- Broadcasts --
function broadcastSortGetter(b, col) {
  if (col === 'name') return b.name || '';
  if (col === 'template') return b.template_name || '';
  if (col === 'status') return b.status || '';
  if (col === 'recipients') return b.recipients || 0;
  return '';
}

function sortBroadcasts(col) {
  toggleSort('broadcasts', col, renderBroadcasts, broadcastSortGetter);
}

async function loadBroadcasts() {
  try {
    _broadcastsData = await apiFetch(BROADCAST_API + '/list').then(r => r.json());
    if (!_broadcastsData.length) {
      document.getElementById('broadcasts-list').textContent = 'No hay broadcasts';
      return;
    }
    renderBroadcasts(_broadcastsData);
  } catch(e) {
    document.getElementById('broadcasts-list').textContent = 'Usar Telegram /broadcast para ver broadcasts';
  }
}

function renderBroadcasts(data) {
  if (isMobileViewport()) {
    const html = `<div class="mobile-card-list">${data.map(b => {
      const statusBadge = {draft:'badge-gray', scheduled:'badge-yellow', sending:'badge-blue', sent:'badge-green', partial:'badge-yellow', failed:'badge-red'}[b.status] || 'badge-gray';
      const tags = JSON.parse(b.target_tags || '[]').join(', ');
      let actionHtml = '';
      if (b.status === 'draft') actionHtml = `<button class="btn btn-primary text-xs" onclick="sendBroadcast('${b.id}')">Enviar</button>`;
      else if (b.status === 'sending' || b.status === 'failed') actionHtml = `<button class="btn btn-danger text-xs" onclick="resetBroadcast('${b.id}')">Resetear</button>`;
      return `
        <div class="card mobile-data-card">
          <div class="mobile-card-header">
            <div>
              <div class="font-semibold text-gray-900 dark:text-gray-100">${escapeHtml(b.name)}</div>
              <div class="text-xs text-gray-500 dark:text-gray-400 mt-1">${escapeHtml(b.template_name || '')}</div>
            </div>
            <span class="badge ${statusBadge}">${escapeHtml(b.status)}</span>
          </div>
          <div class="mt-3 text-sm text-gray-700 dark:text-gray-300">
            <div><span class="text-gray-500 dark:text-gray-400">Tags:</span> ${escapeHtml(tags || '—')}</div>
            <div class="mt-1"><span class="text-gray-500 dark:text-gray-400">Enviados:</span> ${b.recipients || 0}</div>
          </div>
          ${actionHtml ? `<div class="mt-4">${actionHtml}</div>` : ''}
        </div>`;
    }).join('')}</div>`;
    document.getElementById('broadcasts-list').innerHTML = html;
    return;
  }
  let html = `<table class="w-full"><thead><tr class="text-left text-gray-500 dark:text-gray-400 border-b">
    <th class="pb-2 sortable" onclick="sortBroadcasts('name')">Nombre${sortArrow('broadcasts','name')}</th>
    <th class="sortable" onclick="sortBroadcasts('template')">Plantilla${sortArrow('broadcasts','template')}</th>
    <th>Tags</th>
    <th class="sortable" onclick="sortBroadcasts('recipients')">Enviados${sortArrow('broadcasts','recipients')}</th>
    <th class="sortable" onclick="sortBroadcasts('status')">Estado${sortArrow('broadcasts','status')}</th>
    <th></th>
  </tr></thead><tbody>`;
  for (const b of data) {
    const statusBadge = {draft:'badge-gray', scheduled:'badge-yellow', sending:'badge-blue', sent:'badge-green', partial:'badge-yellow', failed:'badge-red'}[b.status] || 'badge-gray';
    const tags = JSON.parse(b.target_tags || '[]').join(', ');
    let sendBtn = '';
    if (b.status === 'draft') sendBtn = `<button class="btn btn-primary text-xs" onclick="sendBroadcast('${b.id}')">Enviar</button>`;
    else if (b.status === 'sending' || b.status === 'failed') sendBtn = `<button class="btn btn-danger text-xs" onclick="resetBroadcast('${b.id}')">Resetear</button>`;
    html += `<tr class="border-t border-gray-100 dark:border-gray-700">
      <td class="py-2 font-medium">${b.name}</td>
      <td>${b.template_name}</td>
      <td>${tags}</td>
      <td>${b.recipients || 0}</td>
      <td><span class="badge ${statusBadge}">${b.status}</span></td>
      <td>${sendBtn}</td>
    </tr>`;
  }
  html += '</tbody></table>';
  document.getElementById('broadcasts-list').innerHTML = html;
}

async function previewBroadcast() {
  const tags = document.getElementById('bc-tags').value.split(',').map(t => t.trim()).filter(Boolean);
  if (!tags.length) { toast('Ingresa al menos un tag', '#dc2626'); return; }

  const resp = await apiFetch(BROADCAST_API + '/preview', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({target_tags: tags}),
  }).then(r => r.json());

  document.getElementById('bc-preview').innerHTML =
    `<strong>${resp.matching_customers}</strong> clientes coinciden. Costo estimado: <strong>$${resp.estimated_cost_usd}</strong>`;
}

async function createBroadcast() {
  const name = document.getElementById('bc-name').value;
  const template = document.getElementById('bc-template').value;
  const tags = document.getElementById('bc-tags').value.split(',').map(t => t.trim()).filter(Boolean);
  const params = document.getElementById('bc-params').value.split(',').map(t => t.trim()).filter(Boolean);

  if (!name || !template || !tags.length) { toast('Completa todos los campos', '#dc2626'); return; }

  await apiFetch(BROADCAST_API + '/create', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name, template_name: template, target_tags: tags, template_params: params.length ? params : null}),
  });

  toast('Broadcast creado como borrador');
  loadBroadcasts();
}

async function sendBroadcast(id) {
  if (!confirm('Enviar este broadcast ahora?')) return;
  await apiFetch(BROADCAST_API + `/${id}/send`, {method: 'POST'});
  toast('Broadcast enviado');
  loadBroadcasts();
}

async function resetBroadcast(id) {
  if (!confirm('¿Resetear este broadcast a borrador?')) return;
  await apiFetch(BROADCAST_API + `/${id}/reset`, {method: 'POST'});
  toast('Broadcast reseteado a borrador');
  loadBroadcasts();
}

// -- Catalog PDF --
async function loadCatalogPdfStatus() {
  try {
    const status = await apiFetch(API + '/catalog/pdf-status').then(r => r.json());
    const el = document.getElementById('pdf-status');
    const dlBtn = document.getElementById('pdf-download-btn');

    if (status.exists) {
      const date = status.generated_at
        ? new Date(status.generated_at).toLocaleString()
        : 'Desconocido';
      el.innerHTML = `<span class="badge badge-green">Disponible</span> &nbsp; ${status.product_count} productos &nbsp; | &nbsp; Generado: ${date}`;
      dlBtn.style.display = 'inline-block';
    } else {
      el.textContent = 'PDF no generado aún. Haz clic en "Generar PDF ahora".';
      dlBtn.style.display = 'none';
    }
  } catch(e) {
    document.getElementById('pdf-status').textContent = 'Error al obtener estado del PDF';
  }
}

async function generateCatalogPdf() {
  const btn = event.target;
  btn.disabled = true;
  btn.textContent = 'Generando...';

  try {
    const resp = await apiFetch(API + '/catalog/generate-pdf', { method: 'POST' }).then(r => r.json());
    if (resp.status === 'generated') {
      toast(`PDF generado: ${resp.product_count} productos`);
      loadCatalogPdfStatus();
    } else {
      toast(resp.detail || 'Error generando PDF', '#dc2626');
    }
  } catch(e) {
    toast('Error al generar PDF', '#dc2626');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Generar PDF ahora';
  }
}

function renderPaymentMethods(paymentMethods) {
  const list = document.getElementById('payment-methods-list');
  if (!list) return;

  list.innerHTML = (paymentMethods || []).map((method, index) => `
    <div class="payment-method-card border border-gray-200 dark:border-gray-700 rounded-xl p-4" data-payment-id="${escapeHtml(method.id || '')}">
      <div class="flex items-center justify-between gap-3 mb-3">
        <div class="text-sm font-semibold text-gray-700 dark:text-gray-200">
          Método <span class="payment-method-number">${index + 1}</span>
        </div>
        <button type="button" class="btn btn-danger btn-icon text-sm" onclick="removePaymentMethod(this)" title="Eliminar método" aria-label="Eliminar método">${renderDeleteIcon('Eliminar método')}</button>
      </div>
      <div class="grid md:grid-cols-[minmax(220px,280px)_1fr] gap-4">
        <div>
          <label class="block text-xs text-gray-500 mb-1">Nombre</label>
          <input type="text" class="w-full payment-method-name" value="${escapeHtml(method.name || '')}" placeholder="Ej. Zelle, Pago móvil, Wise">
        </div>
        <div>
          <label class="block text-xs text-gray-500 mb-1">Información</label>
          <textarea rows="3" class="w-full payment-field payment-method-info" placeholder="Correo, número, instrucciones o cuenta">${escapeHtml(method.information || '')}</textarea>
        </div>
      </div>
    </div>
  `).join('');

  updatePaymentMethodsState();
}

function updatePaymentMethodsState() {
  const cards = Array.from(document.querySelectorAll('.payment-method-card'));
  cards.forEach((card, index) => {
    const number = card.querySelector('.payment-method-number');
    if (number) number.textContent = String(index + 1);
  });

  const empty = document.getElementById('payment-methods-empty');
  if (empty) {
    empty.style.display = cards.length ? 'none' : 'block';
  }
}

function addPaymentMethod(method = {}) {
  const list = document.getElementById('payment-methods-list');
  if (!list) return;

  const nextIndex = list.querySelectorAll('.payment-method-card').length;
  list.insertAdjacentHTML('beforeend', `
    <div class="payment-method-card border border-gray-200 dark:border-gray-700 rounded-xl p-4" data-payment-id="${escapeHtml(method.id || '')}">
      <div class="flex items-center justify-between gap-3 mb-3">
        <div class="text-sm font-semibold text-gray-700 dark:text-gray-200">
          Método <span class="payment-method-number">${nextIndex + 1}</span>
        </div>
        <button type="button" class="btn btn-danger btn-icon text-sm" onclick="removePaymentMethod(this)" title="Eliminar método" aria-label="Eliminar método">${renderDeleteIcon('Eliminar método')}</button>
      </div>
      <div class="grid md:grid-cols-[minmax(220px,280px)_1fr] gap-4">
        <div>
          <label class="block text-xs text-gray-500 mb-1">Nombre</label>
          <input type="text" class="w-full payment-method-name" value="${escapeHtml(method.name || '')}" placeholder="Ej. Zelle, Pago móvil, Wise">
        </div>
        <div>
          <label class="block text-xs text-gray-500 mb-1">Información</label>
          <textarea rows="3" class="w-full payment-field payment-method-info" placeholder="Correo, número, instrucciones o cuenta">${escapeHtml(method.information || '')}</textarea>
        </div>
      </div>
    </div>
  `);

  updatePaymentMethodsState();
  list.lastElementChild?.querySelector('.payment-method-name')?.focus();
}

function removePaymentMethod(trigger) {
  trigger.closest('.payment-method-card')?.remove();
  updatePaymentMethodsState();
}

function collectPaymentMethods() {
  return Array.from(document.querySelectorAll('.payment-method-card')).map(card => ({
    id: card.dataset.paymentId || undefined,
    name: card.querySelector('.payment-method-name')?.value?.trim() || '',
    information: card.querySelector('.payment-method-info')?.value?.trim() || '',
  }));
}

async function savePaymentSettings() {
  const paymentMethods = collectPaymentMethods();
  const missing = paymentMethods.find(method => !method.name || !method.information);
  if (missing) {
    toast('Cada método necesita nombre e información', '#dc2626');
    return;
  }

  const seen = new Set();
  for (const method of paymentMethods) {
    const key = method.name.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase().trim();
    if (seen.has(key)) {
      toast('Los nombres de métodos no pueden repetirse', '#dc2626');
      return;
    }
    seen.add(key);
  }

  await apiFetch(API + '/payment-methods', {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({payment_methods: paymentMethods}),
  });

  toast('Métodos de pago guardados');
  await loadSettings();
}

async function saveExchangeRateSetting() {
  const value = document.getElementById('set-exchange-rate')?.value?.trim() || '';
  await apiFetch(API + '/accepted_exchange_rate', {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({value}),
  });
  toast('Tasa guardada');
  await loadSettings();
}

// -- Settings --
async function loadSettings() {
  const [settings, paymentData] = await Promise.all([
    apiFetch(API + '/').then(r => r.json()),
    apiFetch(API + '/payment-methods').then(r => r.json()),
  ]);
  const pdfInterval = settings.catalog_pdf_interval_hours || 24;
  const pdfIntervalDisplay = document.getElementById('pdf-interval-display');
  if (pdfIntervalDisplay) {
    pdfIntervalDisplay.textContent = `${pdfInterval} horas`;
  }
  const exchangeRateInput = document.getElementById('set-exchange-rate');
  if (exchangeRateInput) {
    exchangeRateInput.value = settings.accepted_exchange_rate || '';
  }
  renderPaymentMethods(paymentData.payment_methods || []);
  loadCatalogPdfStatus();

  // Hide LLM controls when managed from master
  const section = document.getElementById('llm-settings-section');
  if (settings._llm_managed_externally) {
    if (section) {
      section.innerHTML = '<div class="card md:col-span-2"><p class="text-gray-500 dark:text-gray-400 text-center py-4">Para cualquier cambio en la configuracion de AI, contacta a tu administrador.</p></div>';
    }
  } else if (section && !section.querySelector('#set-provider')) {
    window.location.reload();
    return;
  }

  if (!settings._llm_managed_externally) {
    document.getElementById('set-provider').value = settings.llm_provider || 'openai';
    document.getElementById('set-temp').value = settings.llm_temperature || 0.7;
    document.getElementById('set-fallback').checked = settings.auto_fallback === true;
    document.getElementById('set-fb-provider').value = settings.fallback_provider || 'anthropic';
    document.getElementById('set-max-tokens').value = settings.llm_max_tokens || 500;
    document.getElementById('set-max-history').value = settings.max_conversation_history || 20;

    populateModels('set-model', settings.llm_provider || 'openai', settings.llm_model);
    populateModels('set-fb-model', settings.fallback_provider || 'anthropic', settings.fallback_model);
  }
}

function populateModels(selectId, provider, selectedModel) {
  const sel = document.getElementById(selectId);
  sel.innerHTML = '';
  for (const m of MODELS[provider] || []) {
    const opt = document.createElement('option');
    opt.value = m.id; opt.textContent = m.label;
    if (m.id === selectedModel) opt.selected = true;
    sel.appendChild(opt);
  }
}

function onProviderChange() {
  const provider = document.getElementById('set-provider').value;
  populateModels('set-model', provider, null);
}

function onFallbackProviderChange() {
  const provider = document.getElementById('set-fb-provider').value;
  populateModels('set-fb-model', provider, null);
}

async function saveProviderSettings() {
  const provider = document.getElementById('set-provider').value;
  const model = document.getElementById('set-model').value;
  const temp = parseFloat(document.getElementById('set-temp').value);
  const maxTokens = parseInt(document.getElementById('set-max-tokens').value);
  const maxHistory = parseInt(document.getElementById('set-max-history').value);

  if (isNaN(maxTokens) || maxTokens < 100 || maxTokens > 2000) {
    toast('Max tokens debe ser entre 100 y 2000', '#dc2626'); return;
  }
  if (isNaN(maxHistory) || maxHistory < 5 || maxHistory > 50) {
    toast('Historial debe ser entre 5 y 50', '#dc2626'); return;
  }

  await apiFetch(API + '/switch-provider?provider=' + provider + '&model=' + model, {method: 'POST'});
  await apiFetch(API + '/llm_temperature', {
    method: 'PUT', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({value: temp}),
  });
  await apiFetch(API + '/llm_max_tokens', {
    method: 'PUT', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({value: maxTokens}),
  });
  await apiFetch(API + '/max_conversation_history', {
    method: 'PUT', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({value: maxHistory}),
  });

  toast('Configuracion guardada');
  await loadSettings();
  loadOverview();
}

async function saveFallbackSettings() {
  const enabled = document.getElementById('set-fallback').checked;
  const provider = document.getElementById('set-fb-provider').value;
  const model = document.getElementById('set-fb-model').value;

  await apiFetch(API + '/auto_fallback', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({value:enabled})});
  await apiFetch(API + '/fallback_provider', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({value:provider})});
  await apiFetch(API + '/fallback_model', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({value:model})});

  toast('Fallback guardado');
  await loadSettings();
}
