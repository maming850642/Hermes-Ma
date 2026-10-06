// 用量看板（ADR-0005 D2）：拉 /api/usage/summary + /api/usage/records 渲染
// 汇总卡 / 按日 SVG 堆叠柱状图（零依赖 createElementNS，手法同 gittree.js；
// 2026-09-19 起柱内按模型分段着色 + 全页模型筛选）/
// 按 scope 合计表 / 明细分页表。时间窗与模型筛选状态只存内存（切走即丢）。
// 安全纪律：所有动态文本一律 createElement + textContent（不拼 innerHTML），
// 图表节点走 createElementNS + <title> 悬停提示。

const USAGE_API = '/api/usage';
const REC_PAGE_SIZE = 20;

const usageState = { days: 7, page: 1, total: 0, model: '' };   // 窗口/模型/分页
let modelOptions = [];   // 时间窗内的可选模型（summary 响应带来，未过滤）

// ---------- 小工具 ----------

function fmtInt(n) {
  return Number(n || 0).toLocaleString('zh-CN');
}

function fmtTime(ts) {
  if (!ts) return '';
  return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false });
}

function fmtDuration(ms) {
  if (ms === null || ms === undefined) return '—';
  const v = Number(ms) || 0;
  return v < 1000 ? Math.round(v) + ' ms' : (v / 1000).toFixed(1) + ' s';
}

// scope 展示名（未收录的原样展示；textContent 输出，无需转义）
function scopeLabel(s) {
  return { chat: '💬 对话', waker: '⏰ Waker', wakerflow: '🔀 WakerFlow' }[s] || (s || '—');
}

// 模型短名：剥 org 前缀（ornith-ai/Ornith-… → Ornith-…），超长截断
function shortModelName(m) {
  if (!m) return '—';
  const s = String(m).split('/').pop();
  return s.length > 20 ? s.slice(0, 19) + '…' : s;
}

// 模型配色（usage.html 定义 --m1..--m4，超出 4 个循环取用）
function modelColor(i) {
  return `var(--m${(i % 4) + 1})`;
}

// ---------- 数据加载 ----------

async function fetchJson(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}

function modelQuery() {
  return usageState.model ? `&model=${encodeURIComponent(usageState.model)}` : '';
}

async function loadAll() {
  const body = document.getElementById('usage-body');
  const empty = document.getElementById('usage-empty');
  try {
    const [sum, recs] = await Promise.all([
      fetchJson(`${USAGE_API}/summary?days=${usageState.days}${modelQuery()}`),
      fetchJson(`${USAGE_API}/records?page=${usageState.page}&page_size=${REC_PAGE_SIZE}${modelQuery()}`),
    ]);
    // 模型下拉（models 未过滤，跟随时间窗联动）；当前选中不在列表里则回落全部
    modelOptions = sum.models || [];
    rebuildModelFilter();
    const noData = !(sum.rows || []).length && !(recs.total > 0);
    empty.hidden = !noData;
    body.hidden = noData;
    if (noData) return;

    renderTotals(sum.totals || {});
    renderChart(sum.rows || [], sum.days || usageState.days);
    renderScope(sum.rows || []);
    usageState.total = Number(recs.total) || 0;
    renderRecords(recs.records || []);
  } catch (e) {
    body.hidden = false;
    empty.hidden = true;
    showToast('用量数据加载失败：' + (e && e.message ? e.message : '网络异常'), 'error');
  }
}

// 模型筛选下拉：选项来自 summary 响应（窗口内出现过的模型，未过滤）
function rebuildModelFilter() {
  const sel = document.getElementById('model-filter');
  if (!sel) return;
  const prev = usageState.model;
  sel.innerHTML = '';
  const all = document.createElement('option');
  all.value = '';
  all.textContent = '全部模型';
  sel.appendChild(all);
  for (const m of modelOptions) {
    const opt = document.createElement('option');
    opt.value = m;
    opt.textContent = shortModelName(m);
    opt.title = m;
    sel.appendChild(opt);
  }
  if (prev && modelOptions.includes(prev)) sel.value = prev;
  else { sel.value = ''; usageState.model = ''; }
}

// ---------- 汇总卡 ----------

function renderTotals(t) {
  const inEl = document.getElementById('st-in');
  const outEl = document.getElementById('st-out');
  const callsEl = document.getElementById('st-calls');
  const errEl = document.getElementById('st-errors');
  inEl.textContent = fmtInt(t.tokens_in);
  outEl.textContent = fmtInt(t.tokens_out);
  callsEl.textContent = fmtInt(t.calls);
  errEl.textContent = fmtInt(t.errors);
  errEl.classList.toggle('bad', Number(t.errors || 0) > 0);
}

// ---------- 按日 SVG 堆叠柱状图（两根柱：输入/输出，柱内按模型分段着色） ----------

const NS = 'http://www.w3.org/2000/svg';
const CHART_H = 200;        // 柱区高
const LABEL_H = 28;         // 日期标签区高
const GROUP_W = 64;         // 每天一组（两根柱）占宽
const BAR_W = 20;           // 单柱宽

function renderChart(rows, days) {
  const box = document.getElementById('usage-chart');
  box.innerHTML = '';
  const sub = document.getElementById('chart-sub');
  if (sub) sub.textContent = `近 ${days} 天`;

  // 后端 day 倒序返回；画图改为从左到右由旧到新。
  // day → (model → {tin, tout})：柱内按模型分段堆叠（2026-09-19），
  // 输入柱实色、输出柱同色减淡——几何与旧双柱完全一致，模型只是分段。
  const dayModels = new Map();
  for (const r of rows) {
    if (!dayModels.has(r.day)) dayModels.set(r.day, new Map());
    const mm = dayModels.get(r.day);
    const key = r.model || '—';
    const cur = mm.get(key) || { tin: 0, tout: 0 };
    cur.tin += Number(r.tokens_in) || 0;
    cur.tout += Number(r.tokens_out) || 0;
    mm.set(key, cur);
  }
  const dayList = [...dayModels.keys()].sort();
  // 颜色序：时间窗内出现过的模型（全量列表 modelOptions），按字典序固定
  // 分配，选中筛选时堆叠自然退化为单段
  const order = (modelOptions.length ? modelOptions : []);
  const colorOf = m => modelColor(Math.max(0, order.indexOf(m)));

  // 图例：模型色板（title 放全名）+ 实色/浅色说明
  const legend = document.getElementById('usage-legend-models');
  if (legend) {
    legend.innerHTML = '';
    const models = order.length ? order : ['—'];
    for (const m of models) {
      const sp = document.createElement('span');
      sp.className = 'lg-model';
      sp.title = m;
      const sw = document.createElement('i');
      sw.style.background = order.length ? colorOf(m) : 'var(--border)';
      sp.append(sw, document.createTextNode(shortModelName(m)));
      legend.appendChild(sp);
    }
    const hint = document.createElement('span');
    hint.className = 'lg-hint';
    hint.textContent = '实色 = 输入 · 浅色 = 输出';
    legend.appendChild(hint);
  }
  let maxVal = 0;
  for (const mm of dayModels.values()) {
    let tin = 0, tout = 0;
    for (const v of mm.values()) { tin += v.tin; tout += v.tout; }
    maxVal = Math.max(maxVal, tin, tout);
  }
  if (!dayList.length || maxVal <= 0) {
    const p = document.createElement('div');
    p.className = 'usage-chart-empty';
    p.textContent = '窗口内暂无 token 数据';
    box.appendChild(p);
    return;
  }

  const svgW = dayList.length * GROUP_W;
  const svgH = CHART_H + LABEL_H;
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('class', 'usage-svg');
  svg.setAttribute('width', String(svgW));
  svg.setAttribute('height', String(svgH));
  svg.setAttribute('viewBox', `0 0 ${svgW} ${svgH}`);

  const hOf = v => maxVal > 0 ? Math.round(v / maxVal * (CHART_H - 12)) : 0;

  // 一根堆叠柱：segments = [{model, v, color, solid}]，自底向上堆
  const drawBar = (g, x, mm, key, solid) => {
    let yBottom = CHART_H;
    for (const [m, v] of mm) {
      const val = v[key];
      if (val <= 0) continue;
      const h = Math.max(1, hOf(val));
      const rect = document.createElementNS(NS, 'rect');
      rect.setAttribute('x', String(x));
      rect.setAttribute('y', String(yBottom - h));
      rect.setAttribute('width', String(BAR_W));
      rect.setAttribute('height', String(h));
      rect.setAttribute('rx', '2');
      rect.setAttribute('fill', colorOf(m));
      if (!solid) rect.setAttribute('opacity', '0.4');
      const title = document.createElementNS(NS, 'title');
      title.textContent = `${shortModelName(m)} ${solid ? '输入' : '输出'} ${fmtInt(val)}`;
      rect.appendChild(title);
      g.appendChild(rect);
      yBottom -= h;
    }
  };

  dayList.forEach((day, i) => {
    const mm = dayModels.get(day);
    const g = document.createElementNS(NS, 'g');
    const cx = i * GROUP_W + GROUP_W / 2;
    drawBar(g, cx - BAR_W - 3, mm, 'tin', true);    // 输入柱（实色）
    drawBar(g, cx + 3, mm, 'tout', false);          // 输出柱（同色减淡）
    // 悬停整组显示合计（分段各自还有 <title>）
    let tin = 0, tout = 0;
    for (const v of mm.values()) { tin += v.tin; tout += v.tout; }
    const title = document.createElementNS(NS, 'title');
    title.textContent = `${day} · 输入 ${fmtInt(tin)} / 输出 ${fmtInt(tout)}`;
    g.appendChild(title);
    // 底线（悬停可视反馈）
    const hit = document.createElementNS(NS, 'rect');
    hit.setAttribute('x', String(i * GROUP_W));
    hit.setAttribute('y', '0');
    hit.setAttribute('width', String(GROUP_W));
    hit.setAttribute('height', String(svgH));
    hit.setAttribute('fill', 'transparent');
    g.appendChild(hit);
    g.addEventListener('mouseenter', () => g.setAttribute('opacity', '0.75'));
    g.addEventListener('mouseleave', () => g.removeAttribute('opacity'));
    svg.appendChild(g);

    // 日期标签：MM-DD（年份看板顶部窗口已说明）
    const label = document.createElementNS(NS, 'text');
    label.setAttribute('x', String(cx));
    label.setAttribute('y', String(CHART_H + 18));
    label.setAttribute('text-anchor', 'middle');
    label.setAttribute('font-size', '11');
    label.setAttribute('fill', 'var(--text-tertiary)');
    label.textContent = String(day).slice(5);
    svg.appendChild(label);
  });

  // 基线
  const base = document.createElementNS(NS, 'line');
  base.setAttribute('x1', '0');
  base.setAttribute('y1', String(CHART_H));
  base.setAttribute('x2', String(svgW));
  base.setAttribute('y2', String(CHART_H));
  base.setAttribute('stroke', 'var(--border)');
  base.setAttribute('stroke-width', '1');
  svg.appendChild(base);

  box.appendChild(svg);
}

// ---------- 按 scope 合计表 ----------

function renderScope(rows) {
  const box = document.getElementById('usage-scope');
  box.innerHTML = '';
  const agg = new Map();
  for (const r of rows) {
    const a = agg.get(r.scope) || { calls: 0, tin: 0, tout: 0, errors: 0 };
    a.calls += Number(r.calls) || 0;
    a.tin += Number(r.tokens_in) || 0;
    a.tout += Number(r.tokens_out) || 0;
    a.errors += Number(r.errors) || 0;
    agg.set(r.scope, a);
  }
  if (!agg.size) {
    const p = document.createElement('p');
    p.className = 'hint';
    p.textContent = '窗口内暂无数据';
    box.appendChild(p);
    return;
  }
  const table = document.createElement('table');
  const thead = document.createElement('thead');
  const headRow = document.createElement('tr');
  for (const t of ['场景', '调用次数', '输入 tokens', '输出 tokens', '错误']) {
    const th = document.createElement('th');
    th.textContent = t;
    headRow.appendChild(th);
  }
  thead.appendChild(headRow);
  table.appendChild(thead);
  const tbody = document.createElement('tbody');
  const items = [...agg.entries()].sort((a, b) => b[1].calls - a[1].calls);
  for (const [scope, a] of items) {
    const tr = document.createElement('tr');
    const cells = [
      { v: scopeLabel(scope) },
      { v: fmtInt(a.calls) },
      { v: fmtInt(a.tin) },
      { v: fmtInt(a.tout) },
      { v: fmtInt(a.errors), bad: a.errors > 0 },
    ];
    for (const c of cells) {
      const td = document.createElement('td');
      td.textContent = c.v;
      if (c.bad) td.className = 'st-error';
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  box.appendChild(table);
}

// ---------- 明细表（分页） ----------

// 场景列展示：子代理发起的调用（caller=task:名字）带 🤖 标识
function sceneCellLabel(r) {
  const base = (scopeLabel(r.scope) || '').trim();
  const caller = r.caller || '';
  if (caller.startsWith('task:')) {
    return `${base} · 🤖 ${caller.slice(5)}`;
  }
  return base;
}

function renderRecords(records) {
  const box = document.getElementById('usage-records');
  box.innerHTML = '';
  const badge = document.getElementById('rec-total-badge');
  if (badge) badge.textContent = usageState.total ? `共 ${fmtInt(usageState.total)} 条` : '';

  if (!records.length) {
    const p = document.createElement('p');
    p.className = 'hint';
    p.textContent = '这一页没有记录';
    box.appendChild(p);
    updatePager();
    return;
  }

  const table = document.createElement('table');
  const thead = document.createElement('thead');
  const headRow = document.createElement('tr');
  for (const t of ['时间', '场景', '工具', '模型', '输入', '输出', '耗时', '状态', '详情']) {
    const th = document.createElement('th');
    th.textContent = t;
    headRow.appendChild(th);
  }
  thead.appendChild(headRow);
  table.appendChild(thead);
  const tbody = document.createElement('tbody');
  for (const r of records) {
    const tr = document.createElement('tr');
    const cells = [
      fmtTime(r.ts),
      sceneCellLabel(r),
      { v: r.tools || '—', title: r.tools || '' },
      r.model || '—',
      r.tokens_in === null || r.tokens_in === undefined ? '—' : fmtInt(r.tokens_in),
      r.tokens_out === null || r.tokens_out === undefined ? '—' : fmtInt(r.tokens_out),
      fmtDuration(r.duration_ms),
    ];
    for (const c of cells) {
      const td = document.createElement('td');
      if (c && typeof c === 'object') {
        td.textContent = c.v;
        if (c.title) td.title = c.title;
      } else {
        td.textContent = c;
      }
      tr.appendChild(td);
    }
    const stTd = document.createElement('td');
    const isErr = r.status === 'error';
    stTd.textContent = isErr ? '✗ 错误' : '✓ 成功';
    stTd.className = isErr ? 'st-error' : 'st-ok';   // error 行状态标红
    tr.appendChild(stTd);
    // 👀 详情按钮：拉单条调用全文（请求消息/推理/最终输出）弹窗展示
    const opTd = document.createElement('td');
    const viewBtn = document.createElement('button');
    viewBtn.type = 'button';
    viewBtn.className = 'btn-small';
    viewBtn.textContent = '👀';
    viewBtn.title = '查看本次调用详情（请求 / 推理 / 输出）';
    viewBtn.addEventListener('click', () => openUsageDetail(r.id));
    opTd.appendChild(viewBtn);
    tr.appendChild(opTd);
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  box.appendChild(table);
  updatePager();
}

function updatePager() {
  const pages = Math.max(1, Math.ceil(usageState.total / REC_PAGE_SIZE));
  const prev = document.getElementById('rec-prev');
  const next = document.getElementById('rec-next');
  const info = document.getElementById('rec-pageinfo');
  prev.disabled = usageState.page <= 1;
  next.disabled = usageState.page >= pages;
  info.textContent = `第 ${usageState.page} / ${pages} 页`;
}

// ---------- 👀 调用详情弹窗（2026-09-19 重设计：徽章/状态 pill/指标 chips/
// 分节卡片 + details 折叠，对齐 DeepSeek / OpenAI platform 的详情观感）----------

let _usageDetailDlg = null;

function ensureUsageDetailDialog() {
  if (_usageDetailDlg) return _usageDetailDlg;
  _usageDetailDlg = document.createElement('dialog');
  _usageDetailDlg.className = 'dlg usage-detail-dlg';
  const head = document.createElement('div');
  head.className = 'ud-head';
  const line1 = document.createElement('div');
  line1.className = 'ud-head-line';
  const badge = document.createElement('span');
  badge.className = 'ud-badge';
  const pill = document.createElement('span');
  pill.className = 'ud-pill';
  const idSpan = document.createElement('span');
  idSpan.className = 'ud-id';
  line1.append(badge, pill, idSpan);
  const line2 = document.createElement('div');
  line2.className = 'ud-sub';
  head.append(line1, line2);
  const chips = document.createElement('div');
  chips.className = 'ud-chips';
  const list = document.createElement('div');
  list.className = 'ud-list';
  const actions = document.createElement('div');
  actions.className = 'dlg-actions';
  const closeBtn = document.createElement('button');
  closeBtn.type = 'button';
  closeBtn.textContent = '关闭';
  closeBtn.addEventListener('click', () => _usageDetailDlg.close());
  actions.appendChild(closeBtn);
  _usageDetailDlg.append(head, chips, list, actions);
  // 点击遮罩（dialog 本体）关闭
  _usageDetailDlg.addEventListener('click', (e) => {
    if (e.target === _usageDetailDlg) _usageDetailDlg.close();
  });
  document.body.appendChild(_usageDetailDlg);
  return _usageDetailDlg;
}

// 分节卡片：<details> 折叠（summary 标题 + mono 正文）；opts.open 默认展开，
// opts.danger 错误样式，opts.note 提示卡（无正文）
function _udSection(title, text, opts = {}) {
  const det = document.createElement('details');
  det.className = 'ud-sec' + (opts.danger ? ' ud-sec-danger' : '')
    + (opts.note ? ' ud-sec-note' : '');
  if (opts.open) det.open = true;
  const sum = document.createElement('summary');
  sum.textContent = title;
  det.appendChild(sum);
  if (!opts.note) {
    const v = document.createElement('pre');
    v.className = 'ud-v';
    v.textContent = (text && text.trim()) ? text : '—';
    det.appendChild(v);
  } else {
    const note = document.createElement('div');
    note.className = 'ud-note';
    note.textContent = text || '';
    det.appendChild(note);
  }
  return det;
}

// 指标 chip：标签 + 等宽数字
function _udChip(label, value) {
  const c = document.createElement('div');
  c.className = 'ud-chip';
  const k = document.createElement('span');
  k.className = 'ud-chip-k';
  k.textContent = label;
  const v = document.createElement('span');
  v.className = 'ud-chip-v';
  v.textContent = value;
  c.append(k, v);
  return c;
}

// 请求留底（req_messages JSON：[{role, content}]）→ 按角色分节渲染。
// 历史行/解析失败时原样兜底展示。System/User 展开，上下文折叠。
function _udRenderMessages(list, raw) {
  let msgs = null;
  try {
    const parsed = JSON.parse(raw || 'null');
    if (Array.isArray(parsed)) msgs = parsed;
  } catch (e) {}
  if (msgs === null) {
    if (raw) {
      // 旧版对 JSON 硬截断导致的解析失败（新调用已改为丢最旧消息保
      // JSON 合法）——不把原始 JSON 糊一脸，先给说明、原文折叠兜底
      list.appendChild(_udSection('留底解析失败',
        '该行的请求留底被旧版截断（JSON 不完整），无法按消息分节展示。新版已改为丢最旧消息保证 JSON 合法，新调用不受影响。',
        { open: true, note: true }));
      list.appendChild(_udSection('请求消息（原始）', raw));
    }
    return;
  }
  const sys = [], usr = [], others = [];
  for (const m of msgs) {
    const role = (m && m.role) || '?';
    const content = (m && typeof m.content === 'string') ? m.content : '';
    if (role === 'system') sys.push(content);
    else if (role === 'user') usr.push(content);
    else others.push(`[${role}] ${content}`);
  }
  list.appendChild(_udSection('System · 系统提示', sys.join('\n───────\n'), { open: true }));
  list.appendChild(_udSection(
    usr.length > 1 ? `User · 用户输入（${usr.length} 条，按序）` : 'User · 用户输入',
    usr.join('\n───────\n'), { open: true }));
  if (others.length) {
    list.appendChild(_udSection(`上下文（assistant/tool · ${others.length} 条）`,
      others.join('\n───────\n')));
  }
}

async function openUsageDetail(id) {
  if (!id) return;
  let r;
  try { r = await fetch(`${USAGE_API}/records/${encodeURIComponent(id)}/detail`); }
  catch (e) { showToast('详情读取失败：网络异常', 'error'); return; }
  if (!r.ok) {
    let detail = '';
    try { detail = (await r.json()).detail || ''; } catch (e) {}
    showToast('详情读取失败：' + (detail || 'HTTP ' + r.status), 'error');
    return;
  }
  const j = await r.json().catch(() => ({}));
  const rec = j.record || {};
  const isErr = rec.status === 'error';
  const dlg = ensureUsageDetailDialog();

  // 头部：模型徽章 + 状态 pill + #id；第二行元信息
  dlg.querySelector('.ud-badge').textContent = shortModelName(rec.model);
  dlg.querySelector('.ud-badge').title = rec.model || '';
  const pill = dlg.querySelector('.ud-pill');
  pill.textContent = isErr ? '✗ 失败' : '✓ 成功';
  pill.className = 'ud-pill' + (isErr ? ' ud-pill-bad' : '');
  dlg.querySelector('.ud-id').textContent = `#${rec.id}`;
  dlg.querySelector('.ud-sub').textContent = [
    fmtTime(rec.ts),
    (scopeLabel(rec.scope) || '').trim(),
    rec.session_id ? `会话 ${rec.session_id}` : '',
    rec.caller
      ? (rec.caller.startsWith('task:')
          ? `🤖 子代理 ${rec.caller.slice(5)}`
          : rec.caller)
      : '',
  ].filter(Boolean).join(' · ');

  // 指标 chips
  const chips = dlg.querySelector('.ud-chips');
  chips.innerHTML = '';
  chips.append(
    _udChip('输入 tokens', rec.tokens_in === null || rec.tokens_in === undefined ? '—' : fmtInt(rec.tokens_in)),
    _udChip('输出 tokens', rec.tokens_out === null || rec.tokens_out === undefined ? '—' : fmtInt(rec.tokens_out)),
    _udChip('耗时', fmtDuration(rec.duration_ms)),
  );
  if (rec.tools) chips.append(_udChip('工具', rec.tools));

  const list = dlg.querySelector('.ud-list');
  list.innerHTML = '';
  const output = rec.output || '';
  if (rec.has_detail === false) {
    // v5（2026-09-19）上线的旧行：三列本来就没存
    list.appendChild(_udSection('无详情留底',
      '该调用早于「详情留底」功能上线（2026-09-19），只有 token 计量。发起新对话或等待新调用后即可看到请求/推理/输出全文。',
      { open: true, note: true }));
  } else {
    _udRenderMessages(list, rec.req_messages || '');
    list.appendChild(_udSection('推理 · Reasoning（模型思考过程）',
      rec.reasoning || ''));
    if (!output.trim()) {
      // 工具调用轮：tokens/思考都花了但没正文——现在新行 output 已带
      // 工具摘要，这里是兜底（上线初期的中间轮）
      list.appendChild(_udSection('最终输出 · Output',
        '本轮为工具调用轮：模型未产生文本输出，仅发起工具调用（思考过程见上方「推理」）。新调用已自动留底工具摘要。',
        { open: true, note: true }));
    } else {
      list.appendChild(_udSection('最终输出 · Output', output, { open: true }));
    }
  }
  if (isErr && rec.error) {
    list.appendChild(_udSection('错误信息', rec.error, { open: true, danger: true }));
  }
  // 已 open 的 dialog 重复 showModal 会抛 InvalidStateError——内容已原地替换
  if (!dlg.open) {
    try { dlg.showModal(); } catch (e) {}
  }
}

// ---------- 交互 ----------

// 时间窗切换：更新选中态 + 重拉汇总（models/图表跟随窗口联动；明细不分窗口）
document.getElementById('usage-windows').addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-days]');
  if (!btn) return;
  const days = Number(btn.dataset.days);
  if (!days || days === usageState.days) return;
  usageState.days = days;
  document.querySelectorAll('#usage-windows button').forEach(b =>
    b.classList.toggle('active', b === btn));
  fetchJson(`${USAGE_API}/summary?days=${days}${modelQuery()}`)
    .then(sum => {
      modelOptions = sum.models || [];
      rebuildModelFilter();
      renderTotals(sum.totals || {});
      renderChart(sum.rows || [], sum.days || days);
      renderScope(sum.rows || []);
    })
    .catch(() => showToast('汇总数据加载失败', 'error'));
});

// 模型筛选：全页生效（汇总卡/趋势/场景/明细），页码归位
document.getElementById('model-filter').addEventListener('change', (e) => {
  usageState.model = e.target.value || '';
  usageState.page = 1;
  loadAll();
});

document.getElementById('rec-prev').addEventListener('click', () => {
  if (usageState.page <= 1) return;
  usageState.page -= 1;
  loadRecordsOnly();
});
document.getElementById('rec-next').addEventListener('click', () => {
  const pages = Math.max(1, Math.ceil(usageState.total / REC_PAGE_SIZE));
  if (usageState.page >= pages) return;
  usageState.page += 1;
  loadRecordsOnly();
});

async function loadRecordsOnly() {
  try {
    const recs = await fetchJson(
      `${USAGE_API}/records?page=${usageState.page}&page_size=${REC_PAGE_SIZE}${modelQuery()}`);
    usageState.total = Number(recs.total) || 0;
    renderRecords(recs.records || []);
  } catch (e) {
    showToast('明细加载失败：' + (e && e.message ? e.message : '网络异常'), 'error');
  }
}

loadAll();
