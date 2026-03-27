const chat = document.getElementById('chat');

function escapeHtml(text) {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function renderMarkdown(text) {
  const escaped = escapeHtml(text);
  const lines = escaped.split('\n');
  const result = [];
  let inList = false;

  for (const line of lines) {
    const listMatch = line.match(/^[\*\-]\s+(.+)/);
    if (listMatch) {
      if (!inList) { result.push('<ul>'); inList = true; }
      result.push('<li>' + applyInline(listMatch[1]) + '</li>');
    } else {
      if (inList) { result.push('</ul>'); inList = false; }
      result.push(line === '' ? '<br>' : '<p>' + applyInline(line) + '</p>');
    }
  }
  if (inList) result.push('</ul>');
  return result.join('');
}

function applyInline(text) {
  return text
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>');
}

function addMsg(text, cls, meta) {
  const div = document.createElement('div');
  div.className = 'msg ' + cls;
  div.innerHTML = renderMarkdown(text);
  if (meta) {
    const m = document.createElement('div');
    m.className = 'meta';
    m.textContent = meta;
    div.appendChild(m);
  }
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  return div;
}

function addSystem(text) { addMsg(text, 'system'); }

function addCatalogPdf(caption, meta) {
  const div = document.createElement('div');
  div.className = 'msg bot';
  div.innerHTML = `
    <div style="display:flex;align-items:center;gap:10px;padding:6px 0">
      <span style="font-size:2em">📄</span>
      <div>
        <div style="font-weight:600;margin-bottom:2px">Catalogo VS.pdf</div>
        <div style="font-size:0.85em;color:#555;margin-bottom:6px">${escapeHtml(caption || '')}</div>
        <a href="/static/catalog/catalog.pdf" target="_blank"
           style="background:#d05d8c;color:#fff;padding:4px 12px;border-radius:6px;font-size:0.8em;text-decoration:none">
          Ver PDF
        </a>
      </div>
    </div>`;
  if (meta) {
    const m = document.createElement('div');
    m.className = 'meta';
    m.textContent = meta;
    div.appendChild(m);
  }
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
}

async function sendMsg() {
  const input = document.getElementById('input');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';

  addMsg(text, 'user', new Date().toLocaleTimeString());

  const typing = document.createElement('div');
  typing.className = 'typing';
  typing.textContent = 'Luna esta escribiendo...';
  chat.appendChild(typing);
  chat.scrollTop = chat.scrollHeight;

  const t0 = Date.now();

  try {
    const resp = await fetch('/test/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        message: text,
        sender: document.getElementById('sender').value,
        channel: document.getElementById('channel').value,
      }),
    });
    const data = await resp.json();

    typing.remove();

    const ms = Date.now() - t0;

    if (data.paused && !window._pausedShown) {
      addSystem('AI pausado - el mensaje fue enviado al propietario');
      window._pausedShown = true;
      return;
    } else if (data.paused) { return; }

    if (data.escalated && !window._escalatedShown) {
      addSystem('Conversacion escalada al propietario - el mensaje fue enviado al propietario');
      window._escalatedShown = true;
      return;
    } else if (data.escalated) { return; }

    if (data.catalog_pdf && data.catalog_pdf.type === 'catalog_pdf') {
      addCatalogPdf(data.catalog_pdf.caption, ms + 'ms');
    }

    if (data.interactive) {
      const btns = data.interactive.buttons || [];
      addMsg(data.interactive.body_text, 'bot', ms + 'ms');
      addSystem('Botones: ' + btns.join(' | '));
    }

    if (data.reply) {
      addMsg(data.reply, 'bot', ms + 'ms');
    }

  } catch(e) {
    typing.remove();
    addSystem('Error: ' + e.message);
  }
}

async function resetChat() {
  if (!confirm('Borrar toda la conversacion y datos del cliente?')) return;
  const sender = document.getElementById('sender').value;
  const channel = document.getElementById('channel').value;
  await fetch('/test/reset?sender=' + sender + '&channel=' + channel, {method: 'DELETE'});
  chat.innerHTML = '';
  window._pausedShown = false;
  window._escalatedShown = false;
  addSystem('Chat reseteado para ' + sender);
}

// Load existing history on page load
async function loadHistory() {
  const sender = document.getElementById('sender').value;
  const channel = document.getElementById('channel').value;
  try {
    const resp = await fetch('/test/history?sender=' + sender + '&channel=' + channel);
    const data = await resp.json();
    if (data.messages && data.messages.length) {
      addSystem('Historial cargado (' + data.messages.length + ' mensajes) | Tags: ' + JSON.stringify(data.tags));
      for (const msg of data.messages) {
        addMsg(msg.content, msg.role === 'user' ? 'user' : 'bot');
      }
    } else {
      addSystem('Nueva conversacion. Escribe "Hola" para empezar.');
    }
  } catch(e) {
    addSystem('No se pudo cargar historial. Empieza a chatear.');
  }
}

loadHistory();
