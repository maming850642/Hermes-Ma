// 会话事件流弹窗——自 chat.js 首批分块外置拆出，逐字搬移（P2-4）
// ---------- 事件流弹窗（原 /sessions 页能力迁入） ----------
// SessionLog.events 原样返回 [{id, type, payload}]（无时间戳字段）：首列
// 存在 ts 类字段则显示时间，否则回退事件 id；摘要从 payload 常见文本键
// 提取、退化为截断 JSON。全部经 createElement/textContent 渲染，动态数据
// 永不进 HTML 解析器（esc 纪律的最强形态）。
let _eventsDlg = null;

function ensureEventsDialog() {
  if (_eventsDlg) return _eventsDlg;
  _eventsDlg = document.createElement('dialog');
  _eventsDlg.className = 'dlg events-dlg';
  const h = document.createElement('h3');
  _eventsDlg.appendChild(h);
  const list = document.createElement('div');
  list.className = 'events-list';
  _eventsDlg.appendChild(list);
  const actions = document.createElement('div');
  actions.className = 'dlg-actions';
  const closeBtn = document.createElement('button');
  closeBtn.type = 'button';
  closeBtn.textContent = '关闭';
  closeBtn.addEventListener('click', () => _eventsDlg.close());
  actions.appendChild(closeBtn);
  _eventsDlg.appendChild(actions);
  // 点击遮罩（dialog 本体）关闭
  _eventsDlg.addEventListener('click', (e) => {
    if (e.target === _eventsDlg) _eventsDlg.close();
  });
  document.body.appendChild(_eventsDlg);
  return _eventsDlg;
}

function _eventSummary(ev) {
  const p = (ev && ev.payload && typeof ev.payload === 'object') ? ev.payload : {};
  for (const k of ['content', 'message', 'text', 'name', 'tool_name', 'query', 'title', 'reason']) {
    const v = p[k];
    if (typeof v === 'string' && v.trim()) {
      return v.length > 200 ? v.slice(0, 200) + '…' : v;
    }
  }
  let s = '';
  try { s = JSON.stringify(p); } catch (e) {}
  if (!s || s === '{}') return '';
  return s.length > 200 ? s.slice(0, 200) + '…' : s;
}

function _eventStamp(ev) {
  for (const k of ['ts', 'timestamp', 'created_at', 'time']) {
    const v = ev ? ev[k] : null;
    if (typeof v === 'string' && v) return v.slice(0, 19).replace('T', ' ');
    if (typeof v === 'number' && v) {
      // SQLite 存秒级 epoch，Date 只吃毫秒：<1e12 视为秒补乘 1000
      try { return new Date(v < 1e12 ? v * 1000 : v).toLocaleString('sv-SE').replace('T', ' ').slice(0, 19); } catch (e) {}
    }
  }
  return '#' + (ev && ev.id !== undefined ? ev.id : '—');
}

// 请求代际 token：用户快速连点多个会话的「事件流」时，多个 fetch 并发在
// 途，后返回的旧响应会覆盖新会话的内容（标题/列表错位）。每次调用递增
// 计数，响应落地前校验代际，过期即整包丢弃。
let _eventsDlgGen = 0;

async function showSessionEvents(id) {
  if (!id) return;
  const gen = ++_eventsDlgGen;
  let r;
  try { r = await fetch(`/api/sessions/${encodeURIComponent(id)}/events`); }
  catch (e) {
    if (gen === _eventsDlgGen) showToast('事件流读取失败：网络异常', 'error');
    return;
  }
  if (!r.ok) {
    let detail = '';
    try { detail = (await r.json()).detail || ''; } catch (e) {}
    if (gen === _eventsDlgGen) {
      showToast('事件流读取失败：' + (detail || 'HTTP ' + r.status), 'error');
    }
    return;
  }
  const j = await r.json().catch(() => ({}));
  if (gen !== _eventsDlgGen) return;   // 旧响应：已有更新的请求在渲染
  const events = Array.isArray(j.events) ? j.events : [];
  const dlg = ensureEventsDialog();
  dlg.querySelector('h3').textContent = `📜 事件流 — ${id}`;
  const list = dlg.querySelector('.events-list');
  list.innerHTML = '';
  if (!events.length) {
    const empty = document.createElement('div');
    empty.className = 'ev-empty';
    empty.textContent = '该会话暂无事件';
    list.appendChild(empty);
  } else {
    for (const ev of events) {
      const row = document.createElement('div');
      row.className = 'ev-row';
      const t = document.createElement('span');
      t.className = 'ev-time';
      t.textContent = _eventStamp(ev);
      const ty = document.createElement('span');
      ty.className = 'ev-type';
      ty.textContent = String((ev && ev.type) || '—');
      const sum = document.createElement('span');
      sum.className = 'ev-summary';
      sum.textContent = _eventSummary(ev) || '—';
      row.append(t, ty, sum);
      list.appendChild(row);
    }
  }
  // 已 open 的 dialog 重复 showModal 会抛 InvalidStateError——列表内容已
  // 原地替换，无需重开（模态栈位置不变）
  if (!dlg.open) {
    try { dlg.showModal(); } catch (e) {}
  }
}
