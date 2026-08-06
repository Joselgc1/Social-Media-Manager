const API = '/admin/settings/customers/';
const id = document.body.dataset.customerId;
const esc = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
const states = {active: 'Activo', escalated: 'Escalado', blocked: 'Bloqueado'};
const payments = {pending: 'Pendiente', proof_received: 'Comprobante recibido', confirmed: 'Confirmado', failed: 'Fallido', rejected: 'Rechazado'};

async function load() {
  const response = await fetch(`${API}${encodeURIComponent(id)}/detail`, {credentials: 'same-origin'});
  if (!response.ok) return;
  const customer = await response.json();
  document.getElementById('customer-name').textContent = customer.display_name || customer.platform_id;
  const rows = [['Canal', customer.channel === 'whatsapp' ? 'WhatsApp' : 'Instagram'], ['Teléfono', customer.phone], ['Instagram', customer.instagram_handle], ['Estado', states[customer.conversation_state] || '—'], ['Pedidos', customer.total_orders], ['Gastado', `$${Number(customer.total_spent).toFixed(2)}`], ['Última actividad', customer.last_active ? new Date(customer.last_active).toLocaleString('es-VE') : '—']];
  document.getElementById('customer-info').innerHTML = rows.map(([label, value]) => `<div class="detail-info-row"><div class="detail-label">${esc(label)}</div><div class="detail-value">${esc(value || '—')}</div></div>`).join('');
  document.getElementById('customer-state').value = customer.conversation_state || 'active';
  document.getElementById('customer-tags').innerHTML = (customer.tags || []).map(tag => `<span class="badge badge-blue">${esc(tag)}</span>`).join('');
  document.getElementById('marketing-consent').innerHTML = customer.channel === 'whatsapp' ? `<button class="badge ${customer.marketing_opt_in ? 'badge-green' : 'badge-gray'}" onclick="setMarketing(${!customer.marketing_opt_in})">Marketing: ${customer.marketing_opt_in ? 'Autorizado' : 'Sin permiso'}</button>` : '';
  document.getElementById('customer-orders').innerHTML = (customer.orders || []).map(order => `<a class="detail-history-item" href="/admin/orders/${encodeURIComponent(order.id)}">${esc(order.id.slice(0, 8))} · $${Number(order.total).toFixed(2)} · ${esc(payments[order.payment_status] || '—')}</a>`).join('') || 'Sin pedidos';
}
async function addTag() { const input = document.getElementById('new-tag'); const tag = input.value.trim(); if (!tag) return; await fetch(`${API}${encodeURIComponent(id)}/tags`, {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({tags: [tag]})}); input.value = ''; load(); }
async function setMarketing(marketing_opt_in) { await fetch(`${API}${encodeURIComponent(id)}`, {method: 'PUT', credentials: 'same-origin', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({marketing_opt_in})}); load(); }
async function saveState() { await fetch(`${API}${encodeURIComponent(id)}`, {method: 'PUT', credentials: 'same-origin', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({conversation_state: document.getElementById('customer-state').value})}); load(); }
load();
