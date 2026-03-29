const API = '/admin/settings';
let MODELS = {};

// -- Sorting state --
let _customersData = [];
let _ordersData = [];
let _broadcastsData = [];
let _sort = { customers: {col: null, asc: true}, orders: {col: null, asc: true}, broadcasts: {col: null, asc: true} };

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
document.addEventListener('DOMContentLoaded', async () => {
  initDarkModeButton();
  MODELS = await fetch(API + '/providers').then(r => r.json());
  const settings = await fetch(API + '/').then(r => r.json());
  updateAiToggleUI(settings.ai_enabled !== false);
  loadOverview();
  loadSettings();
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

  await fetch(API + '/ai_enabled', {
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
    const stats = await fetch(API + '/stats/conversations').then(r => r.json());
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

  // Usage
  try {
    const usage = await fetch(API + '/usage-summary').then(r => r.json());
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

// -- Customers --
function customerSortGetter(c, col) {
  if (col === 'name') return c.display_name || c.platform_id || '';
  if (col === 'channel') return c.channel || '';
  if (col === 'orders') return c.total_orders || 0;
  if (col === 'spent') return c.total_spent || 0;
  if (col === 'state') return c.conversation_state || 'active';
  return '';
}

function sortCustomers(col) {
  toggleSort('customers', col, renderCustomers, customerSortGetter);
}

async function loadCustomers() {
  const tag = document.getElementById('customer-search')?.value || '';
  const url = tag ? API + '/customers?tag=' + encodeURIComponent(tag) : API + '/customers';

  try {
    _customersData = await fetch(url).then(r => r.json());
    if (!_customersData.length) {
      document.getElementById('customers-list').textContent = 'No hay clientes';
      return;
    }
    renderCustomers(_customersData);
  } catch(e) {
    document.getElementById('customers-list').textContent = 'Error cargando clientes';
  }
}

function renderCustomers(customers) {
  const hasEscalated = customers.some(c => c.conversation_state === 'escalated');
  let html = '';
  if (hasEscalated) {
    html += '<div class="mb-3"><button class="btn btn-primary text-sm" onclick="resolveAllCustomers()">Resolver todas las escalaciones</button></div>';
  }
  html += `<table class="w-full"><thead><tr class="text-left text-gray-500 dark:text-gray-400 border-b">
    <th class="pb-2 sortable" onclick="sortCustomers('name')">Cliente${sortArrow('customers','name')}</th>
    <th class="sortable" onclick="sortCustomers('channel')">Canal${sortArrow('customers','channel')}</th>
    <th>Tags</th>
    <th class="sortable" onclick="sortCustomers('orders')">Pedidos${sortArrow('customers','orders')}</th>
    <th class="sortable" onclick="sortCustomers('spent')">Gastado${sortArrow('customers','spent')}</th>
    <th class="sortable" onclick="sortCustomers('state')">Estado${sortArrow('customers','state')}</th>
    <th></th></tr></thead><tbody>`;
  for (const c of customers) {
    const tags = (typeof c.tags === 'string' ? JSON.parse(c.tags) : c.tags) || [];
    const tagHtml = tags.map(t =>
      `<span class="badge badge-blue" style="cursor:pointer" title="Click para eliminar" onclick="removeTag('${c.id}','${t}')">${t} ✕</span>`
    ).join(' ') + ` <span class="badge badge-gray" style="cursor:pointer" onclick="promptAddTag('${c.id}')" title="Agregar tag">+</span>`;
    const resolveBtn = c.conversation_state === 'escalated'
      ? `<button class="btn btn-secondary text-xs" onclick="resolveCustomer('${c.id}')">Resolver</button>`
      : '';
    html += `<tr class="border-t border-gray-100 dark:border-gray-700">
      <td class="py-2">${c.display_name || c.platform_id}</td>
      <td>${c.channel}</td>
      <td>${tagHtml}</td>
      <td>${c.total_orders || 0}</td>
      <td>$${(c.total_spent || 0).toFixed(2)}</td>
      <td><span class="badge ${c.conversation_state === 'escalated' ? 'badge-red' : 'badge-green'}">${c.conversation_state || 'active'}</span></td>
      <td>${resolveBtn}</td>
    </tr>`;
  }
  html += '</tbody></table>';
  document.getElementById('customers-list').innerHTML = html;
}

async function removeTag(customerId, tag) {
  if (!confirm(`¿Eliminar tag "${tag}"?`)) return;
  await fetch(API + '/customers/' + customerId + '/tags/' + encodeURIComponent(tag), {method: 'DELETE'});
  toast('Tag eliminado');
  loadCustomers();
}

function promptAddTag(customerId) {
  const input = prompt('Tags a agregar (separados por coma):');
  if (!input) return;
  const tags = input.split(',').map(t => t.trim()).filter(Boolean);
  if (!tags.length) return;
  fetch(API + '/customers/' + customerId + '/tags', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({tags}),
  }).then(() => { toast('Tags agregados'); loadCustomers(); });
}

async function resolveCustomer(id) {
  if (!confirm('¿Resolver esta escalación? El AI volverá a responder a este cliente.')) return;
  await fetch(API + '/customers/' + id + '/resolve', {method: 'POST'});
  toast('Escalación resuelta');
  loadCustomers();
}

async function resolveAllCustomers() {
  if (!confirm('¿Resolver TODAS las escalaciones? El AI volverá a responder a todos los clientes.')) return;
  const resp = await fetch(API + '/customers/resolve-all', {method: 'POST'}).then(r => r.json());
  toast(`${resp.resolved} escalación(es) resuelta(s)`);
  loadCustomers();
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
  toggleSort('orders', col, renderOrders, orderSortGetter);
}

async function loadOrders() {
  try {
    _ordersData = await fetch(API + '/orders').then(r => r.json());
    if (!_ordersData.length) {
      document.getElementById('orders-list').textContent = 'No hay pedidos';
      return;
    }
    renderOrders(_ordersData);
  } catch(e) {
    document.getElementById('orders-list').textContent = 'Error cargando pedidos';
  }
}

function renderOrders(orders) {
  let html = `<table class="w-full"><thead><tr class="text-left text-gray-500 dark:text-gray-400 border-b">
    <th class="pb-2 sortable" onclick="sortOrders('name')">Cliente${sortArrow('orders','name')}</th>
    <th>Items</th>
    <th class="sortable" onclick="sortOrders('total')">Total${sortArrow('orders','total')}</th>
    <th class="sortable" onclick="sortOrders('payment')">Pago${sortArrow('orders','payment')}</th>
    <th class="sortable" onclick="sortOrders('status')">Estado${sortArrow('orders','status')}</th>
    <th class="sortable" onclick="sortOrders('date')">Fecha${sortArrow('orders','date')}</th>
  </tr></thead><tbody>`;
  for (const o of orders) {
    const items = (typeof o.items === 'string' ? JSON.parse(o.items) : o.items) || [];
    const itemSummary = items.map(i => `${i.product_name} (${i.size})`).join(', ');
    const statusBadge = {pending:'badge-yellow', proof_received:'badge-blue', confirmed:'badge-green', rejected:'badge-red'}[o.payment_status] || 'badge-gray';
    const date = new Date(o.created_at).toLocaleDateString();
    html += `<tr class="border-t border-gray-100 dark:border-gray-700">
      <td class="py-2">${o.display_name || o.platform_id}</td>
      <td class="truncate max-w-xs" title="${itemSummary}">${itemSummary}</td>
      <td>$${(o.total || 0).toFixed(2)}</td>
      <td>${o.payment_method || '-'}</td>
      <td><span class="badge ${statusBadge}">${o.payment_status}</span></td>
      <td>${date}</td>
    </tr>`;
  }
  html += '</tbody></table>';
  document.getElementById('orders-list').innerHTML = html;
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
    _broadcastsData = await fetch(API.replace('/settings', '') + '/broadcasts/list').then(r => r.json());
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
  let html = `<table class="w-full"><thead><tr class="text-left text-gray-500 dark:text-gray-400 border-b">
    <th class="pb-2 sortable" onclick="sortBroadcasts('name')">Nombre${sortArrow('broadcasts','name')}</th>
    <th class="sortable" onclick="sortBroadcasts('template')">Plantilla${sortArrow('broadcasts','template')}</th>
    <th>Tags</th>
    <th class="sortable" onclick="sortBroadcasts('recipients')">Enviados${sortArrow('broadcasts','recipients')}</th>
    <th class="sortable" onclick="sortBroadcasts('status')">Estado${sortArrow('broadcasts','status')}</th>
    <th></th>
  </tr></thead><tbody>`;
  for (const b of data) {
    const statusBadge = {draft:'badge-gray', scheduled:'badge-yellow', sending:'badge-blue', sent:'badge-green', failed:'badge-red'}[b.status] || 'badge-gray';
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

  const resp = await fetch(API.replace('/settings', '') + '/broadcasts/preview', {
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

  await fetch(API.replace('/settings', '') + '/broadcasts/create', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name, template_name: template, target_tags: tags, template_params: params.length ? params : null}),
  });

  toast('Broadcast creado como borrador');
  loadBroadcasts();
}

async function sendBroadcast(id) {
  if (!confirm('Enviar este broadcast ahora?')) return;
  await fetch(API.replace('/settings', '') + `/broadcasts/${id}/send`, {method: 'POST'});
  toast('Broadcast enviado');
  loadBroadcasts();
}

async function resetBroadcast(id) {
  if (!confirm('¿Resetear este broadcast a borrador?')) return;
  await fetch(API.replace('/settings', '') + `/broadcasts/${id}/reset`, {method: 'POST'});
  toast('Broadcast reseteado a borrador');
  loadBroadcasts();
}

// -- Catalog PDF --
async function loadCatalogPdfStatus() {
  try {
    const status = await fetch(API + '/catalog/pdf-status').then(r => r.json());
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
    const resp = await fetch(API + '/catalog/generate-pdf', { method: 'POST' }).then(r => r.json());
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

async function savePdfInterval() {
  const hours = parseInt(document.getElementById('pdf-interval').value);
  if (isNaN(hours) || hours < 1 || hours > 168) {
    toast('Intervalo debe ser entre 1 y 168 horas', '#dc2626');
    return;
  }
  await fetch(API + '/catalog_pdf_interval_hours', {
    method: 'PUT', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({value: hours}),
  });
  toast('Intervalo de PDF guardado');
}

// -- Settings --
async function loadSettings() {
  const settings = await fetch(API + '/').then(r => r.json());
  document.getElementById('pdf-interval').value = settings.catalog_pdf_interval_hours || 24;
  loadCatalogPdfStatus();

  document.getElementById('set-provider').value = settings.llm_provider || 'openai';
  document.getElementById('set-temp').value = settings.llm_temperature || 0.7;
  document.getElementById('set-fallback').checked = settings.auto_fallback === true;
  document.getElementById('set-fb-provider').value = settings.fallback_provider || 'anthropic';

  document.getElementById('set-max-tokens').value = settings.llm_max_tokens || 500;
  document.getElementById('set-max-history').value = settings.max_conversation_history || 20;
  document.getElementById('set-abtest').checked = settings.ab_test_enabled === true;

  populateModels('set-model', settings.llm_provider || 'openai', settings.llm_model);
  populateModels('set-fb-model', settings.fallback_provider || 'anthropic', settings.fallback_model);
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

  await fetch(API + '/switch-provider?provider=' + provider + '&model=' + model, {method: 'POST'});
  await fetch(API + '/llm_temperature', {
    method: 'PUT', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({value: temp}),
  });
  await fetch(API + '/llm_max_tokens', {
    method: 'PUT', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({value: maxTokens}),
  });
  await fetch(API + '/max_conversation_history', {
    method: 'PUT', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({value: maxHistory}),
  });

  toast('Configuracion guardada');
  loadOverview();
}

async function saveFallbackSettings() {
  const enabled = document.getElementById('set-fallback').checked;
  const provider = document.getElementById('set-fb-provider').value;
  const model = document.getElementById('set-fb-model').value;
  const abTest = document.getElementById('set-abtest').checked;

  await fetch(API + '/auto_fallback', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({value:enabled})});
  await fetch(API + '/fallback_provider', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({value:provider})});
  await fetch(API + '/fallback_model', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({value:model})});
  await fetch(API + '/ab_test_enabled', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({value:abTest})});

  toast('Fallback y A/B testing guardado');
}
