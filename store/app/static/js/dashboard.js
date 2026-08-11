const API = '/admin/settings';
const BROADCAST_API = '/admin/broadcasts';
const INSTAGRAM_CONTENT_API = '/admin/instagram-content';
const INSTAGRAM_CONTENT_TYPE_LABELS = {
  post: 'Post',
  carousel: 'Carrusel',
  reel: 'Reel',
  story: 'Historia',
};
let MODELS = {};

// -- Authenticated fetch wrapper --
// Sends the session cookie (same-origin) on every admin API call.
// On 401, prompts re-authentication instead of silently failing.
async function apiFetch(url, options = {}) {
  let resp;
  try {
    resp = await fetch(url, {
      credentials: 'same-origin',
      ...options,
      headers: {
        ...options.headers,
      },
    });
  } catch (error) {
    toast('No se pudo conectar con el servidor', '#dc2626');
    throw error;
  }
  if (resp.status === 401) {
    toast('Sesión expirada — redirigiendo al login...', '#dc2626');
    setTimeout(() => {
      window.location.href = '/admin/login';
    }, 500);
    throw new Error('Unauthorized');
  }
  if (!resp.ok) {
    const rawBody = await resp.text().catch(() => '');
    let payload = {};
    try {
      payload = rawBody ? JSON.parse(rawBody) : {};
    } catch (_error) {
      payload = {};
    }
    let detail = payload.detail || payload.message || rawBody;
    if (Array.isArray(detail)) {
      detail = detail.map(item => item.msg || String(item)).join('; ');
    }
    const message = detail || (resp.status === 403 ? 'Acceso denegado' : `Error del servidor (${resp.status})`);
    toast(message, '#dc2626');
    const error = new Error(message);
    error.status = resp.status;
    throw error;
  }
  return resp;
}

async function saveSettingsBatch(settings) {
  return apiFetch(API + '/batch', {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({settings}),
  });
}

async function logout() {
  await fetch('/admin/logout', { method: 'POST', credentials: 'same-origin' });
  window.location.href = '/admin/login';
}

// -- Sorting state --
let _customersData = [];
let _ordersData = [];
let _selectedOrderIds = new Set();
let _selectedCustomerIds = new Set();
let _broadcastsData = [];
let _instagramMappingsData = [];
let _instagramProductsData = [];
let _instagramProductsAvailable = true;
let _instagramContextStatus = null;
let _instagramEditingId = null;
let _sort = { customers: {col: null, asc: true}, orders: {col: null, asc: true}, broadcasts: {col: null, asc: true}, instagram: {col: null, asc: true} };
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

function renderCustomerTags(customerId, tags) {
  const safeCustomerId = escapeHtml(customerId);
  const tagChips = tags.map(tag => {
    const safeTag = escapeHtml(tag);
    return `<button type="button" class="badge badge-blue tag-chip tag-remove-chip" data-customer-id="${safeCustomerId}" data-tag="${safeTag}" title="Quitar tag">${safeTag} ✕</button>`;
  }).join('');
  return `<div class="customer-tags">${tagChips}<button type="button" class="badge badge-gray tag-add-chip" data-customer-id="${safeCustomerId}" title="Agregar tag">+ Tag</button></div>`;
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

document.addEventListener('click', event => {
  const removeChip = event.target.closest('.tag-remove-chip');
  if (removeChip) {
    removeTag(removeChip.dataset.customerId, removeChip.dataset.tag);
  }
  const addChip = event.target.closest('.tag-add-chip');
  if (addChip) {
    promptAddTag(addChip.dataset.customerId);
  }
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
  if (document.getElementById('tab-instagram')?.style.display !== 'none' && _instagramMappingsData.length) {
    renderInstagramMappings();
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
  else if (name === 'instagram') loadInstagramMappings();
  else if (name === 'settings') loadSettings();
}

// -- Instagram content mappings --
async function loadInstagramMappings() {
  const [productsResult, mappingsResult, contextStatusResult] = await Promise.allSettled([
    apiFetch(INSTAGRAM_CONTENT_API + '/products').then(r => r.json()),
    apiFetch(INSTAGRAM_CONTENT_API).then(r => r.json()),
    apiFetch(API + '/meta-instagram-context/status').then(r => r.json()),
  ]);
  if (mappingsResult.status === 'rejected') throw mappingsResult.reason;
  _instagramProductsAvailable = productsResult.status === 'fulfilled';
  _instagramProductsData = _instagramProductsAvailable ? productsResult.value : [];
  _instagramMappingsData = mappingsResult.value;
  _instagramContextStatus = contextStatusResult.status === 'fulfilled' ? contextStatusResult.value : null;
  renderInstagramProductOptions();
  renderInstagramMappings();
}

function renderInstagramProductOptions() {
  const select = document.getElementById('instagram-product-skus');
  if (!select) return;
  const selected = new Set([...select.selectedOptions].map(option => option.value));
  select.innerHTML = _instagramProductsData.map(product => `<option value="${escapeHtml(product.sku)}">${escapeHtml(instagramProductLabel(product))}</option>`).join('');
  [...select.options].forEach(option => { option.selected = selected.has(option.value); });
  const picker = document.getElementById('instagram-product-picker');
  if (picker) picker.innerHTML = _instagramProductsAvailable ? _instagramProductsData.map(product => `<label class="flex cursor-pointer items-center gap-2 rounded px-2 py-2 hover:bg-gray-50 dark:hover:bg-gray-800"><input type="checkbox" value="${escapeHtml(product.sku)}" ${selected.has(product.sku) ? 'checked' : ''} onchange="toggleInstagramProduct(this.value, this.checked)"><span class="text-sm">${escapeHtml(instagramProductLabel(product))}</span></label>`).join('') : '<p class="p-2 text-sm text-gray-500">Catálogo temporalmente no disponible</p>';
  renderSelectedInstagramProducts();
}

function instagramProductLabel(product) {
  return [product.name, product.brand, product.sku].filter(Boolean).join(' - ');
}

function toggleInstagramProduct(sku, selected) {
  const option = [...document.getElementById('instagram-product-skus').options].find(item => item.value === sku);
  if (option) option.selected = selected;
  renderSelectedInstagramProducts();
}

function showInstagramMappingForm() {
  const form = document.getElementById('instagram-mapping-form');
  if (form) form.style.display = '';
  form?.scrollIntoView({behavior: 'smooth', block: 'start'});
}

function renderSelectedInstagramProducts() {
  const select = document.getElementById('instagram-product-skus');
  const container = document.getElementById('instagram-selected-products');
  if (!select || !container) return;
  const selected = [...select.selectedOptions];
  container.innerHTML = selected.map((option, index) => (
    `<span class="badge badge-blue inline-flex items-center gap-1">${escapeHtml(option.textContent)}<button type="button" class="font-bold" aria-label="Quitar producto" onclick="removeInstagramProduct(${index})">×</button></span>`
  )).join('');
}

function removeInstagramProduct(selectedIndex) {
  const select = document.getElementById('instagram-product-skus');
  if (!select) return;
  const option = [...select.selectedOptions][selectedIndex];
  if (option) option.selected = false;
  const checkbox = document.querySelector(`#instagram-product-picker input[value="${CSS.escape(option?.value || '')}"]`);
  if (checkbox) checkbox.checked = false;
  renderSelectedInstagramProducts();
}

function toggleInstagramFilters() {
  const panel = document.getElementById('instagram-filters');
  const btn = document.getElementById('instagram-filter-toggle');
  if (!panel || !btn) return;
  const visible = panel.style.display !== 'none';
  panel.style.display = visible ? 'none' : 'grid';
  btn.innerHTML = visible ? 'Filtros &#x25BC;' : 'Filtros &#x25B2;';
}

function resetInstagramFilters() {
  const search = document.getElementById('instagram-filter-search');
  const type = document.getElementById('instagram-filter-type');
  const mapping = document.getElementById('instagram-filter-mapping');
  const lifecycle = document.getElementById('instagram-filter-lifecycle');
  if (search) search.value = '';
  if (type) type.value = '';
  if (mapping) mapping.value = '';
  if (lifecycle) lifecycle.value = 'current';
  applyInstagramFilters();
}

function applyInstagramFilters() {
  renderInstagramMappings();
}

function getVisibleInstagramMappings() {
  const searchValue = (document.getElementById('instagram-filter-search')?.value || '').trim().toLowerCase();
  const typeValue = document.getElementById('instagram-filter-type')?.value || '';
  const mappingValue = document.getElementById('instagram-filter-mapping')?.value || '';
  const lifecycleValue = document.getElementById('instagram-filter-lifecycle')?.value || 'current';

  let visibleMappings = _instagramMappingsData.filter(mapping => {
    const lifecycle = mapping.lifecycle_status || mapping.status || 'active';
    const mapped = mapping.mapping_status === 'mapped' && (mapping.products || []).length > 0;
    if (typeValue && mapping.content_type !== typeValue) return false;
    if (mappingValue === 'mapped' && !mapped) return false;
    if (mappingValue === 'unmapped' && mapped) return false;
    if (lifecycleValue === 'current' && lifecycle === 'archived') return false;
    if (!['', 'all', 'current'].includes(lifecycleValue) && lifecycle !== lifecycleValue) return false;
    if (!searchValue) return true;
    const products = mapping.products || [];
    const searchBlob = [
      mapping.shortcode || '',
      mapping.normalized_url || '',
      mapping.post_url || '',
      mapping.media_id || '',
      ...products.flatMap(product => [product.name || '', product.sku || '']),
    ].join(' ').toLowerCase();
    return searchBlob.includes(searchValue);
  });

  const sortState = _sort.instagram;
  if (sortState.col) {
    visibleMappings = sortData(visibleMappings, sortState.col, sortState.asc, instagramMappingSortGetter);
  }
  return visibleMappings;
}

function instagramMappingSortGetter(mapping, col) {
  if (col === 'content') return mapping.shortcode || mapping.normalized_url || mapping.media_id || '';
  if (col === 'type') return mapping.content_type || '';
  if (col === 'product') return (mapping.products || []).map(product => product.name || '').join(' ');
  if (col === 'status') return mapping.lifecycle_status || mapping.status || 'active';
  return '';
}

function sortInstagramMappings(col) {
  const sortState = _sort.instagram;
  if (sortState.col === col) sortState.asc = !sortState.asc;
  else { sortState.col = col; sortState.asc = true; }
  renderInstagramMappings();
}

function instagramMappingView(mapping) {
  const products = mapping.products || [];
  const lifecycleStatus = mapping.lifecycle_status || mapping.status || 'active';
  const mapped = mapping.mapping_status === 'mapped' && products.length > 0;
  return {
    products,
    lifecycleStatus,
    mapped,
    contentLabel: mapping.shortcode || mapping.normalized_url || mapping.post_url || mapping.media_id || 'Contenido de Instagram',
    contentTypeLabel: INSTAGRAM_CONTENT_TYPE_LABELS[mapping.content_type] || mapping.content_type,
    statusClass: lifecycleStatus === 'active' ? 'badge-green' : (lifecycleStatus === 'expired' ? 'badge-yellow' : 'badge-gray'),
    statusLabel: lifecycleStatus === 'active' ? 'Activo' : lifecycleStatus === 'expired' ? 'Expirado' : 'Archivado',
    productNames: products.map(product => product.name || 'Producto no disponible').join(', ') || 'Sin productos',
    productSkus: products.map(product => product.sku).filter(Boolean).join(', ') || 'Sin SKU',
    prices: products.map(product => product.price === null ? 'N/D' : `$${Number(product.price).toFixed(2)}`).join(', ') || 'N/D',
    stocks: products.map(product => product.stock === null ? 'N/D' : String(product.stock)).join(', ') || 'N/D',
  };
}

function instagramMappingActions(mapping) {
  const statusButton = mapping.status === 'active'
    ? `<button class="btn btn-secondary text-xs" onclick="archiveInstagramMapping('${escapeHtml(mapping.id)}')">Archivar</button>`
    : `<button class="btn btn-secondary text-xs" onclick="restoreInstagramMapping('${escapeHtml(mapping.id)}')">Restaurar</button>`;
  return `<div class="flex flex-wrap justify-end gap-2"><button class="btn btn-secondary text-xs" onclick="editInstagramMapping('${escapeHtml(mapping.id)}')">Editar</button>${statusButton}</div>`;
}

function renderInstagramMappings() {
  const container = document.getElementById('instagram-mappings-list');
  const count = document.getElementById('instagram-mappings-count');
  if (!container || !count) return;
  const visibleMappings = getVisibleInstagramMappings();
  const totalMappings = _instagramMappingsData.length;
  count.textContent = totalMappings
    ? `${visibleMappings.length} de ${totalMappings} contenido${totalMappings === 1 ? '' : 's'}`
    : 'No hay contenidos';
  const unmappedStories = visibleMappings.filter(mapping => mapping.content_type === 'story' && mapping.status === 'active' && !mapping.is_expired && (mapping.mapping_status === 'assignment_required' || !(mapping.products || []).length));
  const storyAlert = unmappedStories.length ? `<div class="mb-4 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">Hay ${unmappedStories.length} Historia${unmappedStories.length === 1 ? '' : 's'} de Instagram sin productos asignados. <button class="font-semibold underline" onclick="editInstagramMapping('${escapeHtml(unmappedStories[0].id)}')">Mapear ahora</button></div>` : '';
  const storyDisabledAlert = _instagramContextStatus && !_instagramContextStatus.story_enabled ? '<div class="mb-4 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">El descubrimiento automático de Historias está desactivado. Solo puedes mapear una Historia actual mediante su URL si las credenciales de Meta para esta cuenta están configuradas.</div>' : '';
  if (!visibleMappings.length) {
    container.innerHTML = `${storyDisabledAlert}<div class="orders-empty">No hay contenidos que coincidan con los filtros.</div>`;
    return;
  }
  if (isMobileViewport()) {
    const cards = visibleMappings.map(mapping => {
      const view = instagramMappingView(mapping);
      const contentLink = mapping.normalized_url
        ? `<a class="font-semibold text-indigo-600 dark:text-indigo-400 break-all" href="${escapeHtml(mapping.normalized_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(view.contentLabel)}</a>`
        : `<span class="font-semibold text-gray-900 dark:text-gray-100 break-all">${escapeHtml(view.contentLabel)}</span>`;
      return `<div class="card mobile-data-card">
        <div class="mobile-card-header"><div class="min-w-0">${contentLink}<div class="flex flex-wrap gap-2 mt-2"><span class="badge badge-blue">${escapeHtml(view.contentTypeLabel)}</span><span class="badge ${view.statusClass}">${escapeHtml(view.statusLabel)}</span></div></div>${mapping.content_type === 'story' && mapping.preview_url ? `<img src="${escapeHtml(mapping.preview_url)}" alt="Vista previa de la Historia" class="w-14 h-20 rounded-lg object-cover border border-gray-200 dark:border-gray-700">` : ''}</div>
        <div class="mt-4"><div class="text-xs text-gray-500 dark:text-gray-400">Productos</div><div class="font-medium mt-1">${escapeHtml(view.productNames)}</div><div class="text-xs text-gray-500 dark:text-gray-400 mt-1">${escapeHtml(view.productSkus)}</div></div>
        <div class="mobile-card-metrics"><div class="mobile-card-metric"><span class="text-xs text-gray-500 dark:text-gray-400">Precio</span><div class="font-semibold mt-1">${escapeHtml(view.prices)}</div></div><div class="mobile-card-metric"><span class="text-xs text-gray-500 dark:text-gray-400">Stock</span><div class="font-semibold mt-1">${escapeHtml(view.stocks)}</div></div><div class="mobile-card-metric"><span class="text-xs text-gray-500 dark:text-gray-400">Mapeo</span><div class="mt-1"><span class="badge ${view.mapped ? 'badge-green' : 'badge-yellow'}">${view.mapped ? 'Mapeado' : 'Sin productos'}</span></div></div></div>
        <div class="mt-4">${instagramMappingActions(mapping)}</div>
      </div>`;
    }).join('');
    container.innerHTML = `${storyDisabledAlert}${storyAlert}<div class="mobile-card-list">${cards}</div>`;
    return;
  }

  const rows = visibleMappings.map(mapping => {
    const view = instagramMappingView(mapping);
    const contentLink = mapping.normalized_url
      ? `<a class="font-semibold text-indigo-600 dark:text-indigo-400 break-all" href="${escapeHtml(mapping.normalized_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(view.contentLabel)}</a>`
      : `<span class="font-semibold text-gray-900 dark:text-gray-100 break-all">${escapeHtml(view.contentLabel)}</span>`;
    return `<tr>
      <td class="instagram-content-cell"><div class="flex items-start gap-3">${mapping.content_type === 'story' && mapping.preview_url ? `<img src="${escapeHtml(mapping.preview_url)}" alt="" class="w-10 h-14 rounded object-cover border border-gray-200 dark:border-gray-700">` : ''}<div class="min-w-0">${contentLink}<div class="flex flex-wrap gap-2 mt-2"><span class="badge badge-blue">${escapeHtml(view.contentTypeLabel)}</span><span class="text-xs text-gray-500 dark:text-gray-400">${escapeHtml(mapping.media_id || 'ID de Meta pendiente')}</span></div></div></div></td>
      <td class="instagram-products-cell"><div class="font-medium text-gray-900 dark:text-gray-100">${escapeHtml(view.productNames)}</div><div class="text-xs text-gray-500 dark:text-gray-400 mt-1">${escapeHtml(view.productSkus)}</div></td>
      <td class="instagram-inventory-cell"><div>${escapeHtml(view.prices)}</div><div class="text-xs text-gray-500 dark:text-gray-400 mt-1">Stock: ${escapeHtml(view.stocks)}</div></td>
      <td class="instagram-status-cell"><div class="flex flex-col items-start gap-2"><span class="badge ${view.statusClass}">${escapeHtml(view.statusLabel)}</span><span class="badge ${view.mapped ? 'badge-green' : 'badge-yellow'}">${view.mapped ? 'Mapeado' : 'Sin productos'}</span></div></td>
      <td class="instagram-actions-cell">${instagramMappingActions(mapping)}</td>
    </tr>`;
  }).join('');
  container.innerHTML = `${storyDisabledAlert}${storyAlert}<div class="overflow-x-auto"><table class="w-full customers-table instagram-mappings-table"><thead><tr class="text-left text-gray-500 dark:text-gray-400 border-b"><th class="sortable" onclick="sortInstagramMappings('content')">Contenido${sortArrow('instagram', 'content')}</th><th class="sortable" onclick="sortInstagramMappings('product')">Productos${sortArrow('instagram', 'product')}</th><th>Precio / Stock</th><th class="sortable" onclick="sortInstagramMappings('status')">Estado${sortArrow('instagram', 'status')}</th><th class="text-right">Acciones</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function formatInstagramContentDate(value) {
  if (!value) return 'No disponible';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('es-VE');
}

async function saveInstagramMapping() {
  const postUrl = document.getElementById('instagram-post-url').value.trim();
  const productSkus = [...document.getElementById('instagram-product-skus').selectedOptions].map(option => option.value);
  const editing = Boolean(_instagramEditingId);
  const mapping = editing ? _instagramMappingsData.find(item => item.id === _instagramEditingId) : null;
  const editingStory = mapping?.content_type === 'story';
  if (!productSkus.length || (!editingStory && !postUrl)) {
    toast(editingStory ? 'Selecciona al menos un producto' : 'Ingresa la URL y selecciona al menos un producto', '#dc2626');
    return;
  }
  if (productSkus.length > 20) {
    toast('Selecciona un máximo de 20 productos', '#dc2626');
    return;
  }
  const url = editing ? `${INSTAGRAM_CONTENT_API}/${encodeURIComponent(_instagramEditingId)}` : INSTAGRAM_CONTENT_API;
  const body = {product_skus: productSkus};
  if (!editingStory) body.post_url = postUrl;
  await apiFetch(url, {
    method: editing ? 'PUT' : 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  toast(editing ? 'Mapeo actualizado' : 'Mapeo creado');
  cancelInstagramEdit();
  await loadInstagramMappings();
}

function editInstagramMapping(contentId) {
  showInstagramMappingForm();
  const mapping = _instagramMappingsData.find(item => item.id === contentId);
  if (!mapping) return;
  _instagramEditingId = contentId;
  const postUrlInput = document.getElementById('instagram-post-url');
  const urlHelp = document.getElementById('instagram-url-help');
  const editingStory = mapping.content_type === 'story';
  postUrlInput.value = mapping.post_url || '';
  postUrlInput.disabled = editingStory;
  if (urlHelp) urlHelp.textContent = editingStory
    ? 'La Historia se identifica por su Story ID; su enlace público no se requiere ni se modifica.'
    : 'Edita la URL pública del post o Reel.';
  const selectedSkus = new Set(mapping.product_skus || []);
  [...document.getElementById('instagram-product-skus').options].forEach(option => {
    option.selected = selectedSkus.has(option.value);
  });
  renderSelectedInstagramProducts();
  document.getElementById('instagram-form-title').textContent = 'Editar mapeo';
  document.getElementById('instagram-save-btn').textContent = 'Guardar cambios';
  document.getElementById('instagram-cancel-btn').style.display = 'inline-flex';
  (editingStory ? document.getElementById('instagram-product-skus') : postUrlInput).focus();
}

function cancelInstagramEdit() {
  _instagramEditingId = null;
  const postUrlInput = document.getElementById('instagram-post-url');
  postUrlInput.value = '';
  postUrlInput.disabled = false;
  const urlHelp = document.getElementById('instagram-url-help');
  if (urlHelp) urlHelp.textContent = 'Obligatoria al crear manualmente un post o Reel.';
  [...document.getElementById('instagram-product-skus').options].forEach(option => { option.selected = false; });
  renderSelectedInstagramProducts();
  document.getElementById('instagram-form-title').textContent = 'Mapear contenido de Instagram';
  document.getElementById('instagram-save-btn').textContent = 'Guardar mapeo';
  document.getElementById('instagram-cancel-btn').style.display = 'none';
  document.getElementById('instagram-mapping-form').style.display = 'none';
}

async function archiveInstagramMapping(contentId) {
  if (!window.confirm('¿Archivar este mapeo?')) return;
  await apiFetch(`${INSTAGRAM_CONTENT_API}/${encodeURIComponent(contentId)}`, {method: 'DELETE'});
  if (_instagramEditingId === contentId) cancelInstagramEdit();
  toast('Mapeo archivado');
  await loadInstagramMappings();
}

async function restoreInstagramMapping(contentId) {
  await apiFetch(`${INSTAGRAM_CONTENT_API}/${encodeURIComponent(contentId)}`, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({status: 'active'}),
  });
  toast('Mapeo restaurado');
  await loadInstagramMappings();
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
  renderBulkCustomerControls(customers);
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
      const tagHtml = renderCustomerTags(c.id, tags);

      html += `
        <div class="card mobile-data-card">
          <div class="mobile-card-header">
            <div>
              <label class="inline-flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400 mb-2">
                <input type="checkbox" ${_selectedCustomerIds.has(c.id) ? 'checked' : ''} onchange="toggleCustomerSelection('${c.id}', this.checked)">
                Seleccionar
              </label>
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
    <th class="pb-2"><input type="checkbox" aria-label="Seleccionar clientes visibles" ${customers.length && customers.every(c => _selectedCustomerIds.has(c.id)) ? 'checked' : ''} onchange="toggleVisibleCustomerSelection(this.checked)"></th>
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
    const tagHtml = renderCustomerTags(c.id, tags);
    const currentState = c.conversation_state || 'active';
    const statusBadge = getCustomerStateBadgeClass(currentState);
    const statusLabel = CUSTOMER_STATE_LABELS[currentState] || currentState || 'Activo';
    const contactValue = getCustomerContactValue(c);
    html += `<tr class="border-t border-gray-100 dark:border-gray-700">
      <td><input type="checkbox" aria-label="Seleccionar cliente" ${_selectedCustomerIds.has(c.id) ? 'checked' : ''} onchange="toggleCustomerSelection('${c.id}', this.checked)"></td>
      <td class="customer-name-cell cursor-pointer" onclick="openCustomerDetail('${c.id}')">
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

function openCustomerDetail(customerId) {
  window.location.href = `/admin/customers/${encodeURIComponent(customerId)}`;
}

function toggleBulkCustomerState() {
  const panel = document.getElementById('customers-bulk-controls');
  const btn = document.getElementById('customers-bulk-toggle');
  if (!panel || !btn) return;
  const visible = panel.style.display !== 'none';
  panel.style.display = visible ? 'none' : 'grid';
  btn.innerHTML = visible ? 'Cambiar Estados &#x25BC;' : 'Cambiar Estados &#x25B2;';
}

function renderBulkCustomerControls(visibleCustomers) {
  const container = document.getElementById('customers-bulk-controls');
  if (!container) return;
  const selectedCount = _selectedCustomerIds.size;
  container.innerHTML = `
    <div class="flex items-center justify-between gap-2"><span class="text-sm font-medium text-gray-700 dark:text-gray-200">${selectedCount} seleccionado${selectedCount === 1 ? '' : 's'}</span><button class="btn btn-secondary text-xs" type="button" onclick="clearCustomerSelection()" ${selectedCount ? '' : 'disabled'}>Limpiar</button></div>
    <button class="btn btn-secondary text-xs" type="button" onclick="toggleVisibleCustomerSelection(true)" ${visibleCustomers.length ? '' : 'disabled'}>Seleccionar visibles</button>
    <select id="bulk-customer-state" class="text-sm" ${selectedCount ? '' : 'disabled'}>
      <option value="">Cambiar estado...</option>
      <option value="active">Activo</option>
      <option value="escalated">Escalado</option>
      <option value="blocked">Bloqueado</option>
    </select>
    <button class="btn btn-primary text-xs" type="button" onclick="applyBulkCustomerState()" ${selectedCount ? '' : 'disabled'}>Aplicar</button><button class="btn btn-secondary text-xs" type="button" onclick="exportCustomers(true)" ${selectedCount ? '' : 'disabled'}>Exportar selección</button>`;
}

function toggleCustomerSelection(customerId, selected) {
  if (selected) _selectedCustomerIds.add(customerId);
  else _selectedCustomerIds.delete(customerId);
  renderCustomers(getVisibleCustomers());
}

function toggleVisibleCustomerSelection(selected) {
  getVisibleCustomers().forEach(customer => {
    if (selected) _selectedCustomerIds.add(customer.id);
    else _selectedCustomerIds.delete(customer.id);
  });
  renderCustomers(getVisibleCustomers());
}

function clearCustomerSelection() {
  _selectedCustomerIds.clear();
  renderCustomers(getVisibleCustomers());
}

async function applyBulkCustomerState() {
  const conversationState = document.getElementById('bulk-customer-state')?.value;
  const customerIds = [..._selectedCustomerIds];
  if (!conversationState) { toast('Selecciona el nuevo estado', '#dc2626'); return; }
  if (!customerIds.length) return;
  if (!confirm(`¿Cambiar el estado de ${customerIds.length} cliente(s)?`)) return;
  const response = await apiFetch(API + '/customers/bulk/state', {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({customer_ids: customerIds, conversation_state: conversationState}),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    toast(data.detail || 'Error actualizando clientes', '#dc2626');
    return;
  }
  const selected = new Set(customerIds);
  _customersData.forEach(customer => {
    if (selected.has(customer.id)) customer.conversation_state = conversationState;
  });
  _selectedCustomerIds.clear();
  renderCustomers(getVisibleCustomers());
  toast('Estados actualizados');
}

async function exportCustomers(selectedOnly = false) { await downloadExport(`${API}/customers/export`, selectedOnly ? [..._selectedCustomerIds] : []); }

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
  const resp = await apiFetch(API + '/customers/' + id + '/resolve', {method: 'POST'});
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    toast(data.detail || 'Error resolviendo escalación', '#dc2626');
    return;
  }
  toast('Escalación resuelta');
  loadCustomers();
}

async function resolveAllCustomers() {
  if (!confirm('¿Resolver TODAS las escalaciones? El AI volverá a responder a todos los clientes.')) return;
  const response = await apiFetch(API + '/customers/resolve-all', {method: 'POST'});
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    toast(data.detail || 'Error resolviendo escalaciones', '#dc2626');
    return;
  }
  const resp = await response.json();
  if (resp.failed) {
    toast(`${resp.resolved} resuelta(s), ${resp.failed} fallida(s)`, '#d97706');
  } else {
    toast(`${resp.resolved} escalación(es) resuelta(s)`);
  }
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

async function changeCustomerMarketingConsent(customerId, marketingOptIn) {
  const customer = _customersData.find(item => item.id === customerId);
  if (!customer || customer.channel !== 'whatsapp') return;
  if (marketingOptIn && !confirm('Confirma que este cliente autorizó explícitamente recibir mensajes de marketing por WhatsApp.')) return;

  const resp = await apiFetch(API + '/customers/' + customerId, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({marketing_opt_in: marketingOptIn}),
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    toast(data.detail || 'Error actualizando consentimiento', '#dc2626');
    return;
  }

  const payload = await resp.json().catch(() => ({}));
  Object.assign(customer, payload.customer || {marketing_opt_in: marketingOptIn});
  renderCustomers(getVisibleCustomers());
  toast(marketingOptIn ? 'Consentimiento registrado' : 'Consentimiento retirado');
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

function toggleBulkOrderStatus() {
  const panel = document.getElementById('orders-bulk-controls');
  const btn = document.getElementById('orders-bulk-toggle');
  if (!panel || !btn) return;

  const visible = panel.style.display !== 'none';
  panel.style.display = visible ? 'none' : 'grid';
  btn.innerHTML = visible
    ? 'Cambiar Estados de Pago &#x25BC;'
    : 'Cambiar Estados de Pago &#x25B2;';
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

  renderBulkOrderControls(orders);

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
            <label class="inline-flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400 mb-2" onclick="event.stopPropagation()">
              <input type="checkbox" ${_selectedOrderIds.has(o.id) ? 'checked' : ''} onchange="toggleOrderSelection('${o.id}', this.checked)">
              Seleccionar
            </label>
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
    <th class="pb-2"><input type="checkbox" aria-label="Seleccionar pedidos visibles" ${orders.length && orders.every(o => _selectedOrderIds.has(o.id)) ? 'checked' : ''} onchange="toggleVisibleOrderSelection(this.checked)"></th>
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
      <td><input type="checkbox" aria-label="Seleccionar pedido" ${_selectedOrderIds.has(o.id) ? 'checked' : ''} onclick="event.stopPropagation()" onchange="toggleOrderSelection('${o.id}', this.checked)"></td>
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

function renderBulkOrderControls(visibleOrders) {
  const container = document.getElementById('orders-bulk-controls');
  if (!container) return;
  const selectedCount = _selectedOrderIds.size;
  container.innerHTML = `
    <div class="flex items-center justify-between gap-2"><span class="text-sm font-medium text-gray-700 dark:text-gray-200">${selectedCount} seleccionado${selectedCount === 1 ? '' : 's'}</span><button class="btn btn-secondary text-xs" type="button" onclick="clearOrderSelection()" ${selectedCount ? '' : 'disabled'}>Limpiar</button></div>
    <button class="btn btn-secondary text-xs" type="button" onclick="toggleVisibleOrderSelection(true)" ${visibleOrders.length ? '' : 'disabled'}>Seleccionar visibles</button>
    <select id="bulk-order-payment-status" class="text-sm" ${selectedCount ? '' : 'disabled'}>
      <option value="">Cambiar estado de pago...</option>
      ${renderOrderPaymentOptions('')}
    </select>
    <button class="btn btn-primary text-xs" type="button" onclick="applyBulkOrderStatus()" ${selectedCount ? '' : 'disabled'}>Aplicar</button><button class="btn btn-secondary text-xs" type="button" onclick="exportOrders(true)" ${selectedCount ? '' : 'disabled'}>Exportar selección</button>`;
}

function toggleOrderSelection(orderId, selected) {
  if (selected) _selectedOrderIds.add(orderId);
  else _selectedOrderIds.delete(orderId);
  renderOrders(getVisibleOrders());
}

function toggleVisibleOrderSelection(selected) {
  getVisibleOrders().forEach(order => {
    if (selected) _selectedOrderIds.add(order.id);
    else _selectedOrderIds.delete(order.id);
  });
  renderOrders(getVisibleOrders());
}

function clearOrderSelection() {
  _selectedOrderIds.clear();
  renderOrders(getVisibleOrders());
}

async function applyBulkOrderStatus() {
  const paymentStatus = document.getElementById('bulk-order-payment-status')?.value;
  const orderIds = [..._selectedOrderIds];
  if (!paymentStatus) { toast('Selecciona el nuevo estado de pago', '#dc2626'); return; }
  if (!orderIds.length) return;
  if (!confirm(`¿Cambiar el estado de pago de ${orderIds.length} pedido(s)?`)) return;

  const response = await apiFetch(API + '/orders/bulk/payment-status', {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({order_ids: orderIds, payment_status: paymentStatus}),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    toast(data.detail || 'Error actualizando pedidos', '#dc2626');
    return;
  }
  const selected = new Set(orderIds);
  _ordersData.forEach(order => {
    if (selected.has(order.id)) order.payment_status = paymentStatus;
  });
  _selectedOrderIds.clear();
  populateOrderFilters(_ordersData);
  renderOrders(getVisibleOrders());
  toast('Estados actualizados');
}

async function exportOrders(selectedOnly = false) { await downloadExport(`${API}/orders/export`, selectedOnly ? [..._selectedOrderIds] : []); }

async function downloadExport(url, ids) {
  const response = await apiFetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ids})});
  if (!response.ok) { toast('No se pudo generar el archivo', '#dc2626'); return; }
  const link = document.createElement('a');
  link.href = URL.createObjectURL(await response.blob());
  link.download = url.includes('/customers/') ? 'clientes.xlsx' : 'pedidos.xlsx';
  link.click();
  URL.revokeObjectURL(link.href);
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
      <td class="py-2 font-medium">${escapeHtml(b.name)}</td>
      <td>${escapeHtml(b.template_name || '')}</td>
      <td>${escapeHtml(tags)}</td>
      <td>${b.recipients || 0}</td>
      <td><span class="badge ${statusBadge}">${escapeHtml(b.status)}</span></td>
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

function shippingRateCard(content) {
  return `<div class="shipping-rate-card rounded-lg border border-gray-200/80 dark:border-gray-700 bg-white/80 dark:bg-gray-900/40 p-3">${content}</div>`;
}

function renderShippingPolicy(policy = {}) {
  const cities = policy.home_delivery_cities || [];
  const zones = policy.home_delivery_zones || [];
  const rates = policy.courier_destination_rates || [];
  const citiesList = document.getElementById('home-delivery-cities-list');
  const zonesList = document.getElementById('home-delivery-zones-list');
  const ratesList = document.getElementById('courier-destination-rates-list');
  if (!citiesList || !zonesList || !ratesList) return;

  citiesList.innerHTML = cities.map(city => homeDeliveryCityMarkup(city)).join('');
  zonesList.innerHTML = zones.map(zone => homeDeliveryZoneMarkup(zone, cities)).join('');
  ratesList.innerHTML = rates.map(rate => courierDestinationRateMarkup(rate)).join('');
  updateShippingCollectionStates();
}

function homeDeliveryCityMarkup(city = {}) {
  return shippingRateCard(`
    <div class="flex items-start gap-3">
      <div class="flex-1 grid sm:grid-cols-[minmax(160px,1fr)_minmax(220px,1.25fr)] gap-3">
        <div>
          <label class="block text-xs font-medium text-gray-600 dark:text-gray-300 mb-1">Ciudad o municipio</label>
          <input class="w-full shipping-home-city-name" value="${escapeHtml(city.name || '')}" placeholder="Ej. Valencia" oninput="refreshHomeDeliveryZoneCityOptions()">
        </div>
        <div>
          <label class="block text-xs font-medium text-gray-600 dark:text-gray-300 mb-1">Alias <span class="font-normal text-gray-400">opcional, separados por coma</span></label>
          <input class="w-full shipping-home-city-aliases" value="${escapeHtml((city.aliases || []).join(', '))}" placeholder="Ej. Valencia, Carabobo">
        </div>
      </div>
      <button type="button" class="btn btn-danger btn-icon text-xs shrink-0" onclick="removeHomeDeliveryCity(this)" title="Eliminar ciudad" aria-label="Eliminar ciudad">${renderDeleteIcon('Eliminar ciudad')}</button>
    </div>
  `);
}

function homeDeliveryZoneMarkup(zone = {}, cities = homeDeliveryCitiesFromInputs()) {
  return shippingRateCard(`
    <div class="flex items-start gap-3">
      <div class="flex-1 grid sm:grid-cols-[minmax(145px,.8fr)_minmax(145px,1fr)_minmax(100px,.55fr)] gap-3">
        <div>
          <label class="block text-xs font-medium text-gray-600 dark:text-gray-300 mb-1">Ciudad</label>
          <select class="w-full shipping-zone-city">${homeDeliveryCityOptions(cities, zone.city)}</select>
        </div>
        <div>
          <label class="block text-xs font-medium text-gray-600 dark:text-gray-300 mb-1">Zona</label>
          <input class="w-full shipping-zone-name" value="${escapeHtml(zone.name || '')}" placeholder="Ej. El Viñedo">
        </div>
        <div>
          <label class="block text-xs font-medium text-gray-600 dark:text-gray-300 mb-1">USD</label>
          <input type="number" min="0" max="100000" step="0.01" class="w-full shipping-zone-fee" value="${escapeHtml(zone.fee_usd ?? '')}" placeholder="0.00">
        </div>
      </div>
      <button type="button" class="btn btn-danger btn-icon text-xs shrink-0" onclick="removeShippingRateCard(this)" title="Eliminar zona" aria-label="Eliminar zona">${renderDeleteIcon('Eliminar zona')}</button>
    </div>
  `);
}

function courierDestinationRateMarkup(rate = {}) {
  return shippingRateCard(`
    <div class="flex items-start gap-3">
      <div class="flex-1 grid sm:grid-cols-[minmax(145px,1fr)_minmax(90px,.55fr)_minmax(90px,.55fr)] gap-3">
        <div>
          <label class="block text-xs font-medium text-gray-600 dark:text-gray-300 mb-1">Ciudad de destino</label>
          <input class="w-full shipping-courier-city" value="${escapeHtml(rate.city || '')}" placeholder="Ej. Caracas">
        </div>
        <div>
          <label class="block text-xs font-medium text-gray-600 dark:text-gray-300 mb-1">MRW USD</label>
          <input type="number" min="0" max="100000" step="0.01" class="w-full shipping-mrw-fee" value="${escapeHtml(rate.mrw_fee_usd ?? '')}" placeholder="0.00">
        </div>
        <div>
          <label class="block text-xs font-medium text-gray-600 dark:text-gray-300 mb-1">Zoom USD</label>
          <input type="number" min="0" max="100000" step="0.01" class="w-full shipping-zoom-fee" value="${escapeHtml(rate.zoom_fee_usd ?? '')}" placeholder="0.00">
        </div>
      </div>
      <button type="button" class="btn btn-danger btn-icon text-xs shrink-0" onclick="removeShippingRateCard(this)" title="Eliminar ciudad" aria-label="Eliminar ciudad">${renderDeleteIcon('Eliminar ciudad')}</button>
    </div>
  `);
}

function addHomeDeliveryCity(city = {}) {
  const list = document.getElementById('home-delivery-cities-list');
  if (!list) return;
  list.insertAdjacentHTML('beforeend', homeDeliveryCityMarkup(city));
  refreshHomeDeliveryZoneCityOptions();
  list.lastElementChild?.querySelector('.shipping-home-city-name')?.focus();
}

function addHomeDeliveryZone(zone = {}) {
  const cities = homeDeliveryCitiesFromInputs();
  if (!cities.length) {
    toast('Primero agrega una ciudad con entrega a domicilio', '#dc2626');
    return;
  }
  const list = document.getElementById('home-delivery-zones-list');
  if (!list) return;
  list.insertAdjacentHTML('beforeend', homeDeliveryZoneMarkup({city: cities[0].name, ...zone}, cities));
  updateShippingCollectionStates();
  list.lastElementChild?.querySelector('.shipping-zone-name')?.focus();
}

function addCourierDestinationRate(rate = {}) {
  const list = document.getElementById('courier-destination-rates-list');
  if (!list) return;
  list.insertAdjacentHTML('beforeend', courierDestinationRateMarkup(rate));
  updateShippingCollectionStates();
  list.lastElementChild?.querySelector('.shipping-courier-city')?.focus();
}

function removeHomeDeliveryCity(trigger) {
  trigger.closest('.shipping-rate-card')?.remove();
  refreshHomeDeliveryZoneCityOptions();
}

function removeShippingRateCard(trigger) {
  trigger.closest('.shipping-rate-card')?.remove();
  updateShippingCollectionStates();
}

function homeDeliveryCitiesFromInputs() {
  return Array.from(document.querySelectorAll('#home-delivery-cities-list .shipping-rate-card'))
    .map(card => ({
      name: card.querySelector('.shipping-home-city-name')?.value?.trim() || '',
      aliases: shippingAliases(card.querySelector('.shipping-home-city-aliases')?.value),
    }))
    .filter(city => city.name);
}

function homeDeliveryCityOptions(cities, selectedCity) {
  const selected = String(selectedCity || '').trim();
  const selectedExists = cities.some(city => city.name === selected);
  const placeholder = selected && !selectedExists
    ? `Ciudad eliminada: ${selected}`
    : 'Selecciona una ciudad';
  const options = cities.map(city => `<option value="${escapeHtml(city.name)}" ${city.name === selected ? 'selected' : ''}>${escapeHtml(city.name)}</option>`).join('');
  return `<option value="" ${selectedExists ? '' : 'selected'}>${escapeHtml(placeholder)}</option>${options}`;
}

function refreshHomeDeliveryZoneCityOptions() {
  const cities = homeDeliveryCitiesFromInputs();
  document.querySelectorAll('.shipping-zone-city').forEach(select => {
    const selected = select.value;
    select.innerHTML = homeDeliveryCityOptions(cities, selected);
  });
  updateShippingCollectionStates();
}

function updateShippingCollectionStates() {
  const cityCount = document.querySelectorAll('#home-delivery-cities-list .shipping-rate-card').length;
  const zoneCount = document.querySelectorAll('#home-delivery-zones-list .shipping-rate-card').length;
  const rateCount = document.querySelectorAll('#courier-destination-rates-list .shipping-rate-card').length;
  const hasCities = homeDeliveryCitiesFromInputs().length > 0;
  const zoneButton = document.getElementById('add-home-delivery-zone-btn');

  const setVisible = (id, visible) => {
    const element = document.getElementById(id);
    if (element) element.style.display = visible ? '' : 'none';
  };

  setVisible('home-delivery-cities-empty', cityCount === 0);
  setVisible('home-delivery-zones-empty', zoneCount === 0);
  setVisible('courier-destination-rates-empty', rateCount === 0);
  if (zoneButton) zoneButton.disabled = !hasCities;
}

function shippingAliases(value) {
  return String(value || '').split(',').map(alias => alias.trim()).filter(Boolean);
}

function shippingFee(input) {
  const raw = input?.value?.trim();
  if (!raw) return null;
  const value = Number(raw);
  return Number.isFinite(value) && value >= 0 ? value : null;
}

function collectShippingPolicy() {
  const homeDeliveryCities = Array.from(document.querySelectorAll('#home-delivery-cities-list .shipping-rate-card')).map(card => ({
    name: card.querySelector('.shipping-home-city-name')?.value?.trim() || '',
    aliases: shippingAliases(card.querySelector('.shipping-home-city-aliases')?.value),
  }));
  const homeDeliveryZones = Array.from(document.querySelectorAll('#home-delivery-zones-list .shipping-rate-card')).map(card => ({
    city: card.querySelector('.shipping-zone-city')?.value?.trim() || '',
    name: card.querySelector('.shipping-zone-name')?.value?.trim() || '',
    aliases: [],
    fee_usd: shippingFee(card.querySelector('.shipping-zone-fee')),
  }));
  const courierDestinationRates = Array.from(document.querySelectorAll('#courier-destination-rates-list .shipping-rate-card')).map(card => ({
    city: card.querySelector('.shipping-courier-city')?.value?.trim() || '',
    aliases: [],
    mrw_fee_usd: shippingFee(card.querySelector('.shipping-mrw-fee')),
    zoom_fee_usd: shippingFee(card.querySelector('.shipping-zoom-fee')),
  }));
  return {
    currency: 'USD',
    home_delivery_cities: homeDeliveryCities,
    home_delivery_zones: homeDeliveryZones,
    courier_destination_rates: courierDestinationRates,
  };
}

async function saveShippingPolicy() {
  const policy = collectShippingPolicy();
  const homeCityNames = new Set(policy.home_delivery_cities.map(city => city.name));
  const invalid = [
    ...policy.home_delivery_cities.filter(city => !city.name),
    ...policy.home_delivery_zones.filter(zone => !zone.city || !homeCityNames.has(zone.city) || !zone.name || zone.fee_usd === null),
    ...policy.courier_destination_rates.filter(rate => !rate.city || rate.mrw_fee_usd === null || rate.zoom_fee_usd === null),
  ];
  if (invalid.length) {
    toast('Completa los nombres y tarifas de todas las filas', '#dc2626');
    return;
  }
  const response = await apiFetch(API + '/shipping-policy', {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({shipping_policy: policy}),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    toast(data.detail || 'No se pudieron guardar las tarifas', '#dc2626');
    return;
  }
  toast('Tarifas de entrega guardadas');
  await loadSettings();
}

function exchangeRateDefinitions() {
  return [
    { key: 'usd_bcv', label: 'Dólar BCV', setting: 'exchange_rate_usd_bcv', effective: 'exchange_rate_usd_bcv_effective_at', fetched: 'exchange_rate_usd_bcv_fetched_at', source: 'exchange_rate_usd_bcv_source', unit: 'USD' },
    { key: 'eur_bcv', label: 'Euro BCV', setting: 'exchange_rate_eur_bcv', effective: 'exchange_rate_eur_bcv_effective_at', fetched: 'exchange_rate_eur_bcv_fetched_at', source: 'exchange_rate_eur_bcv_source', unit: 'EUR' },
    { key: 'usdt_binance', label: 'USDT Binance', setting: 'exchange_rate_usdt_binance', effective: 'exchange_rate_usdt_binance_effective_at', fetched: 'exchange_rate_usdt_binance_fetched_at', source: 'exchange_rate_usdt_binance_source', unit: 'USDT' },
  ];
}

function formatSyncedRate(value) {
  const text = String(value ?? '').trim();
  if (!text) return '';
  const numeric = Number(text.replace(',', '.'));
  if (!Number.isFinite(numeric)) return text;
  return new Intl.NumberFormat('es-VE', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(numeric);
}

function rateInputValue(value) {
  const text = String(value ?? '').trim();
  const match = text.match(/\d[\d.,]*/);
  if (!match) return '';
  return match[0].replace(',', '.');
}

function formatRateTimestamp(value) {
  const text = String(value ?? '').trim();
  if (!text) return 'sin fecha';
  const parsed = new Date(text);
  if (Number.isNaN(parsed.getTime())) return text;
  return parsed.toLocaleString('es-VE', { dateStyle: 'short', timeStyle: 'short' });
}

function toggleManualExchangeRateInput() {
  const selected = document.getElementById('set-exchange-rate-reference')?.value || 'usd_bcv';
  const wrap = document.getElementById('manual-exchange-rate-wrap');
  if (wrap) {
    wrap.style.display = selected === 'manual' ? 'block' : 'none';
  }
}

function renderExchangeRateSummary(settings) {
  const container = document.getElementById('exchange-rate-summary');
  if (!container) return;

  const selected = settings.exchange_rate_reference || 'usd_bcv';
  const rows = exchangeRateDefinitions().map(def => {
    const rate = formatSyncedRate(settings[def.setting]);
    const available = Boolean(rate);
    const fetched = settings[def.fetched] ? formatRateTimestamp(settings[def.fetched]) : 'sin fecha';
    const selectedMark = selected === def.key
      ? '<span class="inline-block h-2.5 w-2.5 rounded-full bg-blue-500 align-middle ml-2" title="Seleccionada"></span>'
      : '';
    return `
      <div class="flex items-start justify-between gap-3">
        <span>${escapeHtml(def.label)}${selectedMark}</span>
        <span class="text-right ${available ? 'text-gray-700 dark:text-gray-300' : 'text-red-600 dark:text-red-400'}">
          ${available ? `${escapeHtml(rate)} Bs/${escapeHtml(def.unit)}<br><span class="text-gray-400">Actualizado: ${escapeHtml(fetched)}</span>` : 'No disponible'}
        </span>
      </div>`;
  }).join('');

  const selectedDef = exchangeRateDefinitions().find(def => def.key === selected);
  const selectedAvailable = selected === 'manual'
    ? Boolean(String(settings.manual_exchange_rate || '').trim())
    : Boolean(selectedDef && String(settings[selectedDef.setting] || '').trim());
  const unavailableNotice = selectedAvailable ? '' : `
    <div class="mt-2 rounded-md bg-red-50 dark:bg-red-950/30 text-red-700 dark:text-red-300 p-2">
      La referencia seleccionada no tiene un valor disponible.
    </div>`;

  container.innerHTML = `
    ${rows}
    <div class="pt-2 mt-2 border-t border-gray-200 dark:border-gray-700">
      Sincronizado en la tienda: ${escapeHtml(formatRateTimestamp(settings.exchange_rates_last_synced_at))}
    </div>
    ${unavailableNotice}`;
}

async function saveExchangeRateSetting() {
  const reference = document.getElementById('set-exchange-rate-reference')?.value || 'usd_bcv';
  const manualValue = document.getElementById('set-manual-exchange-rate')?.value?.trim() || '';

  if (reference === 'manual' && manualValue) {
    const numeric = Number(manualValue.replace(',', '.'));
    if (!Number.isFinite(numeric) || numeric <= 0) {
      toast('La tasa manual debe ser mayor a 0', '#dc2626');
      return;
    }
  }

  const updates = {exchange_rate_reference: reference};
  if (reference === 'manual') updates.manual_exchange_rate = manualValue;
  await saveSettingsBatch(updates);

  toast('Referencia de tasa guardada');
  await loadSettings();
}

async function saveDiscountSettings() {
  const percentInput = document.getElementById('set-order-discount-percent');
  const thresholdInput = document.getElementById('set-order-discount-threshold');
  const percent = Number(percentInput?.value ?? 0);
  const threshold = Number(thresholdInput?.value ?? 0);

  if (Number.isNaN(percent) || percent < 0 || percent > 100) {
    toast('El porcentaje debe estar entre 0 y 100', '#dc2626');
    return;
  }
  if (Number.isNaN(threshold) || threshold < 0) {
    toast('El monto mínimo debe ser 0 o mayor', '#dc2626');
    return;
  }

  await saveSettingsBatch({
    order_discount_percent: percent,
    order_discount_threshold_usd: threshold,
  });

  toast('Descuento guardado');
  await loadSettings();
}

async function saveStorePhoneSetting() {
  const phoneInput = document.getElementById('set-store-phone-number');
  const phone = phoneInput?.value?.replace(/\s+/g, ' ').trim() || '';

  if (phone && (!/[0-9]/.test(phone) || !/^[+0-9 ().-]+$/.test(phone) || phone.length > 40)) {
    toast('El WhatsApp debe ser un número público válido', '#dc2626');
    return;
  }

  await apiFetch(API + '/store_phone_number', {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({value: phone}),
  });

  toast('Contacto público guardado');
  await loadSettings();
}

async function saveEscalationTimeoutSetting() {
  const timeoutInput = document.getElementById('set-automatic-escalation-timeout');
  const minutes = Number(timeoutInput?.value ?? 180);

  if (!Number.isInteger(minutes) || (minutes !== 0 && (minutes < 5 || minutes > 10080))) {
    toast('La pausa debe ser 0 o entre 5 y 10080 minutos', '#dc2626');
    return;
  }

  await apiFetch(API + '/automatic_escalation_timeout_minutes', {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({value: minutes}),
  });

  toast('Tiempo de pausa guardado');
  await loadSettings();
}

// -- Settings --
async function loadSettings() {
  const [settings, paymentData, shippingData] = await Promise.all([
    apiFetch(API + '/').then(r => r.json()),
    apiFetch(API + '/payment-methods').then(r => r.json()),
    apiFetch(API + '/shipping-policy').then(r => r.json()),
  ]);
  const pdfInterval = settings.catalog_pdf_interval_hours || 24;
  const pdfIntervalDisplay = document.getElementById('pdf-interval-display');
  if (pdfIntervalDisplay) {
    pdfIntervalDisplay.textContent = `${pdfInterval} horas`;
  }
  const exchangeRateReference = document.getElementById('set-exchange-rate-reference');
  if (exchangeRateReference) {
    exchangeRateReference.value = settings.exchange_rate_reference || 'usd_bcv';
  }
  const manualExchangeRateInput = document.getElementById('set-manual-exchange-rate');
  if (manualExchangeRateInput) {
    manualExchangeRateInput.value = rateInputValue(settings.manual_exchange_rate || settings.accepted_exchange_rate || '');
  }
  toggleManualExchangeRateInput();
  renderExchangeRateSummary(settings);
  const discountPercentInput = document.getElementById('set-order-discount-percent');
  if (discountPercentInput) {
    discountPercentInput.value = settings.order_discount_percent ?? 10;
  }
  const discountThresholdInput = document.getElementById('set-order-discount-threshold');
  if (discountThresholdInput) {
    discountThresholdInput.value = settings.order_discount_threshold_usd ?? 350;
  }
  const escalationTimeoutInput = document.getElementById('set-automatic-escalation-timeout');
  if (escalationTimeoutInput) {
    escalationTimeoutInput.value = settings.automatic_escalation_timeout_minutes ?? 180;
  }
  const storePhoneInput = document.getElementById('set-store-phone-number');
  if (storePhoneInput) {
    storePhoneInput.value = settings.store_phone_number || '';
  }
  renderPaymentMethods(paymentData.payment_methods || []);
  renderShippingPolicy(shippingData.shipping_policy || {});
  loadCatalogPdfStatus();

  // Hide LLM controls when managed from master
  const section = document.getElementById('llm-settings-section');
  if (settings._llm_managed_externally) {
    if (section) {
      section.innerHTML = '<div class="rounded-xl border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900/30 lg:col-span-2"><p class="text-gray-500 dark:text-gray-400 text-center py-8">La configuración de AI está gestionada por su Administrador.</p></div>';
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
    document.getElementById('set-orchestration-mode').value = settings.ai_orchestration_mode || 'legacy';

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
  const orchestrationMode = document.getElementById('set-orchestration-mode').value;

  if (isNaN(maxTokens) || maxTokens < 100 || maxTokens > 2000) {
    toast('Max tokens debe ser entre 100 y 2000', '#dc2626'); return;
  }
  if (isNaN(maxHistory) || maxHistory < 5 || maxHistory > 50) {
    toast('Historial debe ser entre 5 y 50', '#dc2626'); return;
  }

  await saveSettingsBatch({
    llm_provider: provider,
    llm_model: model,
    llm_temperature: temp,
    llm_max_tokens: maxTokens,
    max_conversation_history: maxHistory,
    ai_orchestration_mode: orchestrationMode,
  });

  toast('Configuracion guardada');
  await loadSettings();
  loadOverview();
}

async function saveFallbackSettings() {
  const enabled = document.getElementById('set-fallback').checked;
  const provider = document.getElementById('set-fb-provider').value;
  const model = document.getElementById('set-fb-model').value;

  await saveSettingsBatch({
    auto_fallback: enabled,
    fallback_provider: provider,
    fallback_model: model,
  });

  toast('Fallback guardado');
  await loadSettings();
}
