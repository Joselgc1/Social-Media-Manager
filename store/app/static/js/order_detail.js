const ORDER_API = '/admin/settings/orders/';
const ORDER_PAYMENT_STATUS_LABELS = {
  pending: 'Pendiente',
  proof_received: 'Comprobante recibido',
  confirmed: 'Confirmado',
  failed: 'Fallido',
  rejected: 'Rechazado',
};
const ORDER_SHIPPING_STATUS_LABELS = {
  pending: 'Pendiente',
  shipped: 'Enviado',
  delivered: 'Entregado',
};
const CUSTOMER_STATE_LABELS = {
  active: 'Activo',
  escalated: 'Escalado',
  blocked: 'Bloqueado',
};
const CHANNEL_LABELS = {
  whatsapp: 'WhatsApp',
  instagram: 'Instagram',
};

let currentOrder = null;

async function apiFetch(url, options = {}) {
  const resp = await fetch(url, {
    credentials: 'same-origin',
    ...options,
    headers: {
      ...options.headers,
    },
  });
  if (resp.status === 401) {
    window.location.href = '/admin/login';
    throw new Error('Unauthorized');
  }
  return resp;
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function toast(msg, color = '#4f46e5') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.style.background = color;
  el.style.display = 'block';
  setTimeout(() => el.style.display = 'none', 3000);
}

function toggleDarkMode() {
  const isDark = document.documentElement.classList.toggle('dark');
  localStorage.setItem('darkMode', isDark);
  document.getElementById('order-dark-toggle').textContent = isDark ? '☀️' : '🌙';
}

function initDarkModeButton() {
  const isDark = document.documentElement.classList.contains('dark');
  document.getElementById('order-dark-toggle').textContent = isDark ? '☀️' : '🌙';
}

function money(value) {
  return `$${Number(value || 0).toFixed(2)}`;
}

function formatDateTime(value) {
  if (!value) return '—';
  const date = new Date(value);
  return `${date.toLocaleDateString()} ${date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
}

function renderInfoGrid(targetId, rows) {
  const html = rows.map(row => `
    <div class="detail-info-row">
      <div class="detail-label">${escapeHtml(row.label)}</div>
      <div class="detail-value">${row.html ?? escapeHtml(row.value ?? '—')}</div>
    </div>
  `).join('');
  document.getElementById(targetId).innerHTML = html;
}

function getPaymentBadgeClass(status) {
  return {
    pending: 'badge-yellow',
    proof_received: 'badge-blue',
    confirmed: 'badge-green',
    failed: 'badge-red',
    rejected: 'badge-red',
  }[status] || 'badge-gray';
}

function getShippingBadgeClass(status) {
  return {
    pending: 'badge-yellow',
    shipped: 'badge-blue',
    delivered: 'badge-green',
  }[status] || 'badge-gray';
}

function renderItems(items, total) {
  if (!items.length) {
    document.getElementById('order-items-table').innerHTML = '<div class="orders-empty">Este pedido no tiene items.</div>';
    return;
  }

  let html = `<table class="w-full orders-table detail-items-table"><thead><tr class="text-left text-gray-500 dark:text-gray-400 border-b">
    <th class="pb-2">Producto</th>
    <th class="pb-2">Talla</th>
    <th class="pb-2">Cantidad</th>
    <th class="pb-2">Precio unitario</th>
    <th class="pb-2">Subtotal</th>
  </tr></thead><tbody>`;

  for (const item of items) {
    const subtotal = Number(item.quantity || 0) * Number(item.unit_price || 0);
    html += `<tr class="border-t border-gray-100 dark:border-gray-700">
      <td class="py-3">
        <div class="font-medium text-gray-900 dark:text-gray-100">${escapeHtml(item.product_name || item.sku || 'Producto')}</div>
        <div class="text-xs text-gray-500 dark:text-gray-400">${escapeHtml(item.sku || 'Sin SKU')}</div>
      </td>
      <td class="py-3">${escapeHtml(item.size || '—')}</td>
      <td class="py-3">${escapeHtml(item.quantity || 0)}</td>
      <td class="py-3">${money(item.unit_price || 0)}</td>
      <td class="py-3 font-medium">${money(subtotal)}</td>
    </tr>`;
  }

  html += `<tr class="border-t border-gray-200 dark:border-gray-600">
    <td class="py-3 font-semibold text-gray-900 dark:text-gray-100" colspan="4">Total</td>
    <td class="py-3 font-semibold text-gray-900 dark:text-gray-100">${money(total)}</td>
  </tr></tbody></table>`;
  document.getElementById('order-items-table').innerHTML = html;
}

function renderCustomer(customer) {
  const wrap = document.getElementById('customer-tags-wrap');

  if (!customer) {
    document.getElementById('customer-detail-grid').innerHTML = '<div class="orders-empty">No hay cliente asociado.</div>';
    wrap.style.display = 'none';
    return;
  }

  renderInfoGrid('customer-detail-grid', [
    { label: 'Nombre', value: customer.display_name || 'Sin nombre' },
    { label: 'Canal', value: CHANNEL_LABELS[customer.channel] || customer.channel || '—' },
    { label: 'WhatsApp', value: customer.phone || '—' },
    { label: 'Instagram', value: customer.instagram_handle ? '@' + customer.instagram_handle.replace(/^@+/, '') : '—' },
    { label: 'ID plataforma', value: customer.platform_id || '—' },
    { label: 'Estado conversación', value: CUSTOMER_STATE_LABELS[customer.conversation_state] || customer.conversation_state || '—' },
    { label: 'Pedidos pagados', value: customer.total_orders || 0 },
    { label: 'Total gastado', value: money(customer.total_spent || 0) },
    { label: 'Primera vez', value: formatDateTime(customer.first_contact) },
    { label: 'Última actividad', value: formatDateTime(customer.last_active) },
  ]);

  const tags = Array.isArray(customer.tags) ? customer.tags : [];
  if (tags.length) {
    wrap.style.display = '';
    document.getElementById('customer-tags-list').innerHTML = tags
      .map(tag => `<span class="badge badge-gray">${escapeHtml(tag)}</span>`)
      .join('');
  } else {
    wrap.style.display = 'none';
  }
}

function renderRecentOrders(orders, currentOrderId) {
  if (!orders.length) {
    document.getElementById('recent-customer-orders').innerHTML = '<div class="orders-empty">No hay otros pedidos recientes.</div>';
    return;
  }

  let html = '<div class="detail-history-list">';
  for (const order of orders) {
    const currentBadge = order.id === currentOrderId ? '<span class="badge badge-blue">Actual</span>' : '';
    html += `
      <a class="detail-history-item" href="/admin/orders/${encodeURIComponent(order.id)}">
        <div class="flex items-center justify-between gap-3">
          <div class="font-medium text-gray-900 dark:text-gray-100">${escapeHtml(order.id.slice(0, 8))}</div>
          ${currentBadge}
        </div>
        <div class="text-sm text-gray-600 dark:text-gray-400 mt-1">${formatDateTime(order.created_at)}</div>
        <div class="flex flex-wrap gap-2 mt-3">
          <span class="badge ${getPaymentBadgeClass(order.payment_status)}">${escapeHtml(ORDER_PAYMENT_STATUS_LABELS[order.payment_status] || order.payment_status || 'Pago')}</span>
          <span class="badge ${getShippingBadgeClass(order.shipping_status)}">${escapeHtml(ORDER_SHIPPING_STATUS_LABELS[order.shipping_status] || order.shipping_status || 'Envío')}</span>
          <span class="badge badge-gray">${money(order.total)}</span>
        </div>
      </a>
    `;
  }
  html += '</div>';
  document.getElementById('recent-customer-orders').innerHTML = html;
}

function populateSelectors(order) {
  document.getElementById('detail-payment-status').innerHTML = Object.entries(ORDER_PAYMENT_STATUS_LABELS)
    .map(([value, label]) => `<option value="${value}" ${value === order.payment_status ? 'selected' : ''}>${label}</option>`)
    .join('');

  document.getElementById('detail-shipping-status').innerHTML = Object.entries(ORDER_SHIPPING_STATUS_LABELS)
    .map(([value, label]) => `<option value="${value}" ${value === order.shipping_status ? 'selected' : ''}>${label}</option>`)
    .join('');

  document.getElementById('detail-tracking-number').value = order.tracking_number || '';
}

function renderOrder(order) {
  currentOrder = order;
  const customer = order.customer;
  const displayName = customer?.display_name || customer?.platform_id || 'Cliente';
  const paymentLabel = ORDER_PAYMENT_STATUS_LABELS[order.payment_status] || order.payment_status || 'Sin estado';
  const shippingLabel = ORDER_SHIPPING_STATUS_LABELS[order.shipping_status] || order.shipping_status || 'Sin estado';

  document.getElementById('order-detail-subtitle').textContent = `Pedido ${order.id.slice(0, 8)} • ${displayName}`;
  document.getElementById('order-total-stat').textContent = money(order.total);
  document.getElementById('order-payment-stat').textContent = paymentLabel;
  document.getElementById('order-shipping-stat').textContent = shippingLabel;
  document.getElementById('order-channel-stat').textContent = CHANNEL_LABELS[customer?.channel] || customer?.channel || '—';

  renderInfoGrid('order-summary-grid', [
    { label: 'ID pedido', value: order.id },
    { label: 'Creado', value: formatDateTime(order.created_at) },
    { label: 'Actualizado', value: formatDateTime(order.updated_at) },
    { label: 'Método de pago', value: order.payment_method || '—' },
    { label: 'Estado de pago', html: `<span class="badge ${getPaymentBadgeClass(order.payment_status)}">${escapeHtml(paymentLabel)}</span>` },
    { label: 'Estado de envío', html: `<span class="badge ${getShippingBadgeClass(order.shipping_status)}">${escapeHtml(shippingLabel)}</span>` },
    { label: 'Items', value: order.items.length },
    { label: 'Unidades', value: order.items.reduce((acc, item) => acc + Number(item.quantity || 0), 0) },
    { label: 'Total', value: money(order.total) },
    { label: 'Totales cliente aplicados', value: order.customer_totals_applied ? 'Sí' : 'No' },
  ]);

  renderItems(order.items || [], order.total || 0);
  renderCustomer(customer);
  renderInfoGrid('payment-shipping-grid', [
    { label: 'Método de envío', value: order.shipping_method || '—' },
    { label: 'Ciudad', value: order.shipping_city || '—' },
    { label: 'Dirección', value: order.shipping_address || '—' },
    { label: 'Tracking', value: order.tracking_number || '—' },
    { label: 'Nota / comprobante', value: order.payment_proof || '—' },
    { label: 'Última dirección cliente', value: customer?.last_shipping_address || '—' },
    { label: 'Última ciudad cliente', value: customer?.last_shipping_city || '—' },
    { label: 'Último envío cliente', value: customer?.last_shipping_method || '—' },
  ]);
  renderRecentOrders(order.recent_customer_orders || [], order.id);
  populateSelectors(order);

  document.getElementById('order-detail-loading').style.display = 'none';
  document.getElementById('order-detail-content').style.display = '';
}

async function loadOrderDetail() {
  const resp = await apiFetch(ORDER_API + encodeURIComponent(window.ORDER_DETAIL_ID));
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    document.getElementById('order-detail-loading').textContent = data.detail || 'No se pudo cargar el pedido.';
    return;
  }

  const data = await resp.json();
  renderOrder(data);
}

async function saveOrderUpdates() {
  if (!currentOrder) return;

  const payload = {
    payment_status: document.getElementById('detail-payment-status').value,
    shipping_status: document.getElementById('detail-shipping-status').value,
    tracking_number: document.getElementById('detail-tracking-number').value.trim(),
  };

  const resp = await apiFetch(ORDER_API + encodeURIComponent(currentOrder.id), {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });

  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    toast(data.detail || 'Error actualizando pedido', '#dc2626');
    return;
  }

  await loadOrderDetail();
  toast('Pedido actualizado');
}

async function deleteCurrentOrder() {
  if (!currentOrder) return;
  if (!confirm('¿Eliminar este pedido? Esto también restaurará el stock del catálogo.')) return;

  const resp = await apiFetch(ORDER_API + encodeURIComponent(currentOrder.id), { method: 'DELETE' });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    toast(data.detail || 'Error eliminando pedido', '#dc2626');
    return;
  }

  window.location.href = '/admin/dashboard';
}

document.addEventListener('DOMContentLoaded', async () => {
  initDarkModeButton();
  await loadOrderDetail();
});
