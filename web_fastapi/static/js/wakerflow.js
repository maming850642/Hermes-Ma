// ════════════════════════════════════════════════════════════
// 全局状态
// ════════════════════════════════════════════════════════════
let editingName = null;  // null=新建模式，非空=编辑模式
let wakerOptions = [];   // [{name, description}] 已有 waker 列表
let toolOptions = [];    // [{name, description}] 可用工具列表
let _blockIdCounter = 0; // 临时 id 生成（仅前端，保存时以后端校验为准）

// ════════════════════════════════════════════════════════════
// Flow 列表
// ════════════════════════════════════════════════════════════
async function loadFlows() {
  try {
    const r = await fetch('/api/wakerflow/items');
    const d = await r.json();
    renderFlows(d.flows || []);
  } catch (e) { showToast('加载 flow 失败: ' + e.message, 'error'); }
}

function renderFlows(flows) {
  const el = document.getElementById('flow-list');
  if (!flows.length) {
    el.innerHTML = '<p class="hint">暂无 Flow，点上方按钮新建。</p>';
    return;
  }
  el.innerHTML = flows.map(f => {
    const schedBadge = f.schedule_type && f.schedule_type !== 'none'
      ? `<span>⏰ ${f.schedule_type === 'interval' ? '每'+(f.interval_minutes||60)+'分' : '每天'+(f.daily_at||'09:00')}</span>`
      : '<span>⏰ 手动</span>';
    const enabledBadge = f.enabled === false
      ? '<span style="color:var(--text-secondary)">⏸ 已禁用</span>'
      : '<span style="color:#27ae60">● 启用</span>';
    const nextRun = f.next_run_at ? `<span>⏭ 下次：${esc(f.next_run_at)}</span>` : '';
    const isActive = f.last_status === 'running' && f.active_run_id;
    // 运行中：显示进度占位 div（供轮询更新），且整个卡片加高亮边框
    const activeProgress = isActive
      ? `<div id="progress-${esc(f.active_run_id)}" class="flow-progress">▶ 运行中...</div>`
      : '';
    const cardBorder = isActive ? 'border-color:var(--accent,#0066cc);box-shadow:0 0 0 2px rgba(0,102,204,0.15);' : '';
    return `
    <div class="card" style="margin-bottom:1rem;${cardBorder}">
      <h3 style="margin:0 0 0.3rem;">${esc(f.name)}
        ${statusBadge(f.last_status)}
      </h3>
      <div class="flow-meta">
        <span>📝 ${f.steps_count || 0} 步</span>
        ${schedBadge}
        ${enabledBadge}
        ${nextRun}
        <span>🕐 上次：${f.last_run || '—'}</span>
        <span>📊 ${f.last_status || '—'}</span>
      </div>
      ${f.description ? `<p class="hint" style="margin:0.4rem 0;">${esc(f.description)}</p>` : ''}
      ${activeProgress}
      <div class="flow-actions">
        <!-- P2-mXSS 同规约（见下方 #flow-list 委托注释）：f.name 只进
             data-flow 属性（esc 防属性逃逸），不进 onclick JS 编译器 -->
        <button data-flow="${esc(f.name)}" data-act="run" class="btn-primary btn-small" ${isActive?'disabled':''}>▶ 运行</button>
        <button data-flow="${esc(f.name)}" data-act="log" class="btn-small">📜 运行记录</button>
        <button data-flow="${esc(f.name)}" data-act="toggle" data-flow-enable="${f.enabled === false ? '1' : '0'}" class="btn-small">${f.enabled === false ? '▶ 启用' : '⏸ 禁用'}</button>
        <button data-flow="${esc(f.name)}" data-act="edit" class="btn-small">✏ 编辑</button>
        <button data-flow="${esc(f.name)}" data-act="delete" class="btn-small danger">🗑 删除</button>
      </div>
    </div>
  `;}).join('');

  // 启动所有活跃 flow 的进度轮询
  flows.filter(f => f.last_status === 'running' && f.active_run_id).forEach(f => {
    pollProgress(f.name, f.active_run_id);
  });
}

// 单个活跃 run 的进度轮询（更新卡片上的进度行）
const _pollingRuns = new Set();  // 防止重复轮询同一 run
function pollProgress(flowName, runId) {
  if (_pollingRuns.has(runId)) return;
  _pollingRuns.add(runId);
  // 对齐 invokeAndPoll：最多轮询 5 分钟。run 卡在 running 态（worker 崩溃
  // 等）时此前会永久 2s 一次轮询下去。超时后原地收尾（不调 loadFlows——
  // 列表刷新会对 running flow 重新拉起轮询，绕过 deadline）。
  const deadline = Date.now() + 5 * 60 * 1000;
  const tick = async () => {
    if (Date.now() > deadline) {
      _pollingRuns.delete(runId);
      const el = document.getElementById('progress-' + runId);
      if (el) el.innerHTML = '<span style="color:var(--text-secondary)">⏱ 已运行超 5 分钟，停止进度轮询（flow 仍在后台），可用「刷新」/「📜 日志」查看</span>';
      showToast('flow「' + flowName + '」进度轮询超时（5 分钟），已停止', 'error');
      return;
    }
    try {
      const r = await fetch('/api/wakerflow/items/' + encodeURIComponent(flowName) + '/runs/' + encodeURIComponent(runId) + '/status');
      const d = await r.json();
      const el = document.getElementById('progress-' + runId);
      if (!el) { _pollingRuns.delete(runId); return; }  // 卡片不在了，停
      const total = d.total_nodes || '?';
      const done = d.completed_nodes || 0;
      const cur = d.current_node ? ` · 当前 ${esc(d.current_node)}` : '';
      const approval = d.pending_approval ? ' · 🟡 待审批' : '';
      if (d.status === 'completed') {
        el.innerHTML = `<span style="color:#27ae60;">✓ 完成（${done}/${total} 步）</span>`;
        el.classList.add('done');
        _pollingRuns.delete(runId);
        loadFlows();  // 刷新整列表（去掉高亮）
        return;
      }
      if (d.status === 'failed' || d.status === 'error') {
        el.innerHTML = `<span style="color:#e74c3c;">✗ ${esc(d.status)}${d.error ? ': ' + esc(d.error.slice(0,80)) : ''}</span>`;
        _pollingRuns.delete(runId);
        loadFlows();
        return;
      }
      el.innerHTML = `<span style="color:var(--accent,#0066cc);">▶ 运行中 ${done}/${total} 步${cur}${approval}</span>`;
      setTimeout(tick, 2000);
    } catch (e) {
      setTimeout(tick, 3000);
    }
  };
  tick();
}

async function toggleFlowEnabled(name, enable) {
  try {
    const r = await fetch('/api/wakerflow/items/' + encodeURIComponent(name) + '/enabled', {
      method: 'PATCH', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({enabled: enable}),
    });
    if (!r.ok) {
      const d = await r.json().catch(()=>({}));
      showToast('切换失败: ' + (d.detail || r.status), 'error');
      return;
    }
    showToast(enable ? '已启用调度' : '已禁用调度');
    loadFlows();
  } catch (e) { showToast('切换失败: ' + e.message, 'error'); }
}

function statusBadge(s) {
  if (s === 'completed') return '<span class="badge badge-green">completed</span>';
  if (s === 'failed') return '<span class="badge badge-red">failed</span>';
  if (s === 'running') return '<span class="badge badge-orange">running</span>';
  return '<span class="badge">—</span>';
}

// ════════════════════════════════════════════════════════════
// 新建 / 编辑
// ════════════════════════════════════════════════════════════
function openCreateForm() {
  editingName = null;
  document.getElementById('f-name').value = '';
  document.getElementById('f-name').readOnly = false;
  document.getElementById('f-description').value = '';
  document.getElementById('blocks-list').innerHTML = '';
  document.getElementById('f-inputs-list').innerHTML = '';
  fillSchedule({});  // 重置调度配置为默认
  hideValidation();
  document.getElementById('flow-form-details').style.display = 'block';
  document.getElementById('flow-form-details').scrollIntoView({behavior:'smooth', block:'start'});
  document.getElementById('f-name').focus();
  reindexBlocks();
}

async function editFlow(name) {
  try {
    const r = await fetch('/api/wakerflow/items/' + encodeURIComponent(name));
    if (!r.ok) throw new Error('加载失败');
    const d = await r.json();
    const f = d.flow;
    editingName = name;
    document.getElementById('f-name').value = name;
    document.getElementById('f-name').readOnly = true;
    document.getElementById('f-description').value = f.description || '';
    // 渲染积木块
    document.getElementById('blocks-list').innerHTML = '';
    (f.blocks || []).forEach(b => appendBlockEl('blocks-list', b));
    // 渲染输入参数
    renderInputs(f.inputs || []);
    // 回填调度配置
    fillSchedule(f.schedule || {});
    hideValidation();
    document.getElementById('flow-form-details').style.display = 'block';
    document.getElementById('flow-form-details').scrollIntoView({behavior:'smooth', block:'start'});
    reindexBlocks();
  } catch (e) { showToast('加载 flow 失败: ' + e.message, 'error'); }
}

function closeForm() {
  document.getElementById('flow-form-details').style.display = 'none';
}

// ════════════════════════════════════════════════════════════
// 积木块：添加 / 渲染 / 序列化
// ════════════════════════════════════════════════════════════
const BLOCK_DEFAULTS = {
  worker:   () => ({type:'worker', id:'', waker:'', task:'', tools:null, permission_mode:null, if:''}),
  parallel: () => ({type:'parallel', id:'', children:[], if:''}),
  pipeline: () => ({type:'pipeline', id:'', children:[], if:''}),
  ask_user: () => ({type:'ask_user', id:'', question:'', options:[{label:'是',value:'yes'},{label:'否',value:'no'}], timeout:300, default:'yes', if:''}),
  action:   () => ({type:'action', id:'', method:'POST', url:'', headers:{}, body:'', if:''}),
};

const BLOCK_LABELS = {
  worker:'Worker', parallel:'Parallel', pipeline:'Pipeline',
  ask_user:'Ask User', action:'Action',
};

// 生成默认 id（用户可改）
function genId(type) {
  _blockIdCounter++;
  const prefix = {worker:'step', parallel:'parallel', pipeline:'pipeline', ask_user:'ask', action:'action'}[type] || 'step';
  return prefix + _blockIdCounter;
}

// 添加一个块到指定容器（顶层=blocks-list，子层=容器内）
function addBlock(type, containerId='blocks-list') {
  const block = BLOCK_DEFAULTS[type]();
  block.id = genId(type);
  appendBlockEl(containerId, block);
  reindexBlocks();
}

// 把 block data 追加为 DOM 元素
function appendBlockEl(containerId, block) {
  const container = document.getElementById(containerId);
  const el = document.createElement('div');
  el.className = 'block';
  el.dataset.type = block.type;
  renderBlockInner(el, block);
  container.appendChild(el);
}

// 渲染单个块内部 HTML
function renderBlockInner(el, block) {
  const isContainer = block.type === 'parallel' || block.type === 'pipeline';
  el.dataset.type = block.type;
  el.innerHTML = `
    <div class="block-header">
      <span class="block-handle" title="拖拽排序">☰</span>
      <span class="block-idx"></span>
      <span class="block-type">${BLOCK_LABELS[block.type]||block.type}</span>
      <span class="block-id-hint"></span>
      <div class="block-actions">
        ${isContainer ? '<button onclick="toggleCollapse(this)" title="折叠/展开">▾</button>' : ''}
        <button onclick="removeBlock(this)" title="删除">✕</button>
      </div>
    </div>
    <div class="block-body">${renderBlockForm(block)}</div>
  `;
  // 回填 id hint
  const idInput = el.querySelector('.bf-id');
  if (idInput) {
    el.querySelector('.block-id-hint').textContent = idInput.value || '(未命名)';
    idInput.addEventListener('input', () => {
      el.querySelector('.block-id-hint').textContent = idInput.value || '(未命名)';
    });
  }
  // 容器块：渲染子块 + 初始化嵌套 Sortable
  if (isContainer) {
    const childContainer = el.querySelector('.block-children-container');
    (block.children || []).forEach(c => {
      const childEl = document.createElement('div');
      childEl.className = 'block';
      childEl.dataset.type = c.type;
      renderBlockInner(childEl, c);
      childContainer.appendChild(childEl);
    });
    initSortable(childContainer);
  }
}

// 5 种块各自的表单 HTML
function renderBlockForm(block) {
  const idField = `<label>步骤 ID <span class="hint">（其他步骤用 {{steps.这里.result}} 引用）</span></label>
    <input type="text" class="bf-id" value="${esc(block.id||'')}" placeholder="如：review">`;
  const ifField = `<label>条件（可选）<span class="hint">（为真才执行，如 {{steps.x.answer}} == yes）</span></label>
    <input type="text" class="bf-if" value="${esc(block.if||'')}" placeholder="{{steps.x.answer}} == yes">`;

  if (block.type === 'worker') {
    const wakerOpts = wakerOptions.map(w =>
      `<option value="${esc(w.name)}" ${w.name===block.waker?'selected':''}>${esc(w.name)}${w.description?' — '+esc(w.description.slice(0,40)):''}</option>`
    ).join('');
    const toolCheckboxes = toolOptions.map(t =>
      `<label title="${esc(t.description||'')}"><input type="checkbox" class="bf-tool" value="${esc(t.name)}" ${(block.tools||[]).includes(t.name)?'checked':''}> <span>${esc(t.name)}</span></label>`
    ).join('');
    return `${idField}
      <div class="block-row">
        <div><label>调用哪个 waker</label>
          <select class="bf-waker"><option value="">（选择）</option>${wakerOpts}</select></div>
        <div><label>权限模式</label>
          <select class="bf-perm">
            <option value="" ${!block.permission_mode?'selected':''}>(继承默认)</option>
            <option value="full_access" ${block.permission_mode==='full_access'?'selected':''}>full_access（自动）</option>
            <option value="before_changes" ${block.permission_mode==='before_changes'?'selected':''}>before_changes（改前确认）</option>
            <option value="plan" ${block.permission_mode==='plan'?'selected':''}>plan（只规划）</option>
          </select></div>
      </div>
      <label>任务描述 <span class="hint">（可用 {{inputs.x}} {{steps.y.result}} 插值）</span></label>
      <textarea class="bf-task" placeholder="审查 {{inputs.target}} 的业务逻辑">${esc(block.task||'')}</textarea>
      <label>工具白名单 <span class="hint">（全不勾=允许全部）</span></label>
      <div class="tools-checkbox-list">${toolCheckboxes || '<span class="hint">（加载中...）</span>'}</div>
      ${ifField}`;
  }
  if (block.type === 'parallel' || block.type === 'pipeline') {
    const hint = block.type === 'parallel'
      ? '并发执行所有子步骤（互不依赖）'
      : '串行执行子步骤（上游结果自动喂下游）';
    return `${idField}
      <p class="hint" style="margin:0.3rem 0;">${hint}</p>
      <div class="block-children-container"></div>
      <div class="children-add-row">
        <button onclick="addChildBlock(this,'worker')" class="btn-small">＋ worker</button>
        <button onclick="addChildBlock(this,'ask_user')" class="btn-small">＋ ask_user</button>
        <button onclick="addChildBlock(this,'action')" class="btn-small">＋ action</button>
      </div>
      ${ifField}`;
  }
  if (block.type === 'ask_user') {
    const opts = (block.options||[]).map((o,i) =>
      `<div class="option-row">
        <input type="text" class="bf-opt-label" value="${esc(o.label||'')}" placeholder="显示文本">
        <input type="text" class="bf-opt-value" value="${esc(String(o.value??''))}" placeholder="值">
        <button onclick="removeOptionRow(this)" title="删除">✕</button>
      </div>`
    ).join('');
    return `${idField}
      <label>问题</label>
      <textarea class="bf-question" placeholder="是否生成报告？">${esc(block.question||'')}</textarea>
      <label>选项 <span class="hint">（label 显示给用户，value 用于条件和引用）</span></label>
      <div class="bf-options">${opts}</div>
      <button onclick="addOptionRow(this)" class="btn-small" style="font-size:0.75rem;">＋ 添加选项</button>
      <div class="block-row" style="margin-top:0.4rem;">
        <div><label>超时（秒）</label><input type="number" class="bf-timeout" value="${block.timeout??300}" min="0"></div>
        <div><label>超时默认值 <span class="hint">（无则超时失败）</span></label><input type="text" class="bf-default" value="${esc(String(block.default??''))}" placeholder="如 yes"></div>
      </div>
      ${ifField}`;
  }
  if (block.type === 'action') {
    return `${idField}
      <div class="block-row">
        <div><label>HTTP 方法</label>
          <select class="bf-method">
            ${['POST','GET','PUT','DELETE','PATCH'].map(m=>`<option ${block.method===m?'selected':''}>${m}</option>`).join('')}
          </select></div>
        <div><label>URL</label><input type="text" class="bf-url" value="${esc(block.url||'')}" placeholder="https://..."></div>
      </div>
      <label>Body <span class="hint">（字符串或 JSON，可用 {{}} 插值）</span></label>
      <textarea class="bf-body" placeholder='{"result": "{{steps.report.result}}"}'>${esc(typeof block.body==='object'?JSON.stringify(block.body,null,2):(block.body||''))}</textarea>
      ${ifField}`;
  }
  return '';
}

// 容器块添加子步骤
function addChildBlock(btn, type) {
  const block = btn.closest('.block');
  const childContainer = block.querySelector('.block-children-container');
  const child = BLOCK_DEFAULTS[type]();
  child.id = genId(type);
  const childEl = document.createElement('div');
  childEl.className = 'block';
  childEl.dataset.type = type;
  renderBlockInner(childEl, child);
  childContainer.appendChild(childEl);
  reindexBlocks();
}

// ask_user 选项增删
function addOptionRow(btn) {
  const container = btn.closest('.block-body').querySelector('.bf-options');
  const row = document.createElement('div');
  row.className = 'option-row';
  row.innerHTML = `<input type="text" class="bf-opt-label" placeholder="显示文本">
    <input type="text" class="bf-opt-value" placeholder="值">
    <button onclick="removeOptionRow(this)" title="删除">✕</button>`;
  container.appendChild(row);
}
function removeOptionRow(btn) { btn.closest('.option-row').remove(); }

// 删除块
function removeBlock(btn) {
  btn.closest('.block').remove();
  reindexBlocks();
}
// 折叠/展开容器块
function toggleCollapse(btn) {
  const block = btn.closest('.block');
  const body = block.querySelector('.block-body');
  body.style.display = body.style.display === 'none' ? '' : 'none';
  btn.textContent = body.style.display === 'none' ? '▸' : '▾';
}

// 重新编号所有块（顶层 + 子层各自编号）
function reindexBlocks() {
  document.querySelectorAll('#blocks-list > .block').forEach((el, i) => {
    el.querySelector('.block-idx').textContent = i + 1;
    // 子块编号
    el.querySelectorAll('.block-children-container > .block').forEach((c, j) => {
      const idx = c.querySelector('.block-idx');
      if (idx) idx.textContent = (i+1) + '.' + (j+1);
    });
  });
}

// 从 DOM 序列化出 blocks JSON（顶层）
function serializeBlocks(containerId='blocks-list') {
  const container = document.getElementById(containerId);
  return Array.from(container.querySelectorAll(':scope > .block')).map(el => serializeBlockEl(el));
}

// 单个块 DOM → data dict（递归 children）
function serializeBlockEl(el) {
  const type = el.dataset.type;
  const block = {type};
  const idVal = el.querySelector('.bf-id')?.value.trim();
  if (idVal) block.id = idVal;
  const ifVal = el.querySelector('.bf-if')?.value.trim();
  if (ifVal) block.if = ifVal;

  if (type === 'worker') {
    block.waker = el.querySelector('.bf-waker')?.value || '';
    block.task = el.querySelector('.bf-task')?.value || '';
    const tools = Array.from(el.querySelectorAll('.bf-tool:checked')).map(c => c.value);
    block.tools = tools.length ? tools : null;
    const perm = el.querySelector('.bf-perm')?.value;
    block.permission_mode = perm || null;
  } else if (type === 'parallel' || type === 'pipeline') {
    block.children = Array.from(el.querySelectorAll(':scope > .block-body > .block-children-container > .block'))
      .map(c => serializeBlockEl(c));
  } else if (type === 'ask_user') {
    block.question = el.querySelector('.bf-question')?.value || '';
    block.options = Array.from(el.querySelectorAll('.option-row')).map(r => ({
      label: r.querySelector('.bf-opt-label').value,
      value: r.querySelector('.bf-opt-value').value,
    })).filter(o => o.label || o.value);
    block.timeout = parseInt(el.querySelector('.bf-timeout')?.value) || 86400;
    const def = el.querySelector('.bf-default')?.value;
    block.default = def !== '' ? def : null;
  } else if (type === 'action') {
    block.method = el.querySelector('.bf-method')?.value || 'POST';
    block.url = el.querySelector('.bf-url')?.value || '';
    const bodyStr = el.querySelector('.bf-body')?.value || '';
    if (bodyStr.trim()) {
      // 尝试解析为 JSON，失败则保留字符串
      try { block.body = JSON.parse(bodyStr); } catch { block.body = bodyStr; }
    } else { block.body = null; }
  }
  return block;
}

// 序列化输入参数
function serializeInputs() {
  return Array.from(document.querySelectorAll('#f-inputs-list .input-field-row')).map(row => {
    const f = {
      name: row.querySelector('.if-name').value.trim(),
      type: row.querySelector('.if-type').value,
      required: row.querySelector('.if-required').checked,
    };
    const defVal = row.querySelector('.if-default').value;
    if (defVal !== '') {
      if (f.type === 'number') f.default = Number(defVal);
      else if (f.type === 'boolean') f.default = defVal === 'true';
      else f.default = defVal;
    }
    const enumStr = row.querySelector('.if-enum').value.trim();
    if (enumStr) f.enum = enumStr.split(',').map(s => s.trim()).filter(Boolean);
    return f;
  }).filter(f => f.name);
}

// 渲染输入参数列表
function renderInputs(inputs) {
  const container = document.getElementById('f-inputs-list');
  container.innerHTML = '';
  inputs.forEach(f => appendInputField(f));
}

function addInputField() { appendInputField({name:'',type:'string',required:false}); }

// 调度类型联动显隐
function onScheduleChange() {
  const t = document.getElementById('f-schedule_type').value;
  document.getElementById('f-interval-wrap').style.display = (t === 'interval') ? '' : 'none';
  document.getElementById('f-daily-wrap').style.display = (t === 'daily') ? '' : 'none';
}

// 序列化调度配置（saveFlow 用）
function serializeSchedule() {
  return {
    enabled: document.getElementById('f-enabled').checked,
    schedule_type: document.getElementById('f-schedule_type').value,
    interval_minutes: parseInt(document.getElementById('f-interval_minutes').value) || 60,
    daily_at: document.getElementById('f-daily_at').value || '09:00',
    api_enabled: document.getElementById('f-api_enabled').checked,
    max_runs: parseInt(document.getElementById('f-max_runs').value) || 0,
    expire_at: document.getElementById('f-expire_at').value || '',
  };
}

// 回填调度配置（editFlow 用）
function fillSchedule(s) {
  s = s || {};
  document.getElementById('f-enabled').checked = s.enabled !== false;
  document.getElementById('f-schedule_type').value = s.schedule_type || 'none';
  document.getElementById('f-interval_minutes').value = s.interval_minutes || 60;
  document.getElementById('f-daily_at').value = (s.daily_at || '09:00').slice(0,5);
  document.getElementById('f-api_enabled').checked = !!s.api_enabled;
  document.getElementById('f-max_runs').value = s.max_runs || 0;
  document.getElementById('f-expire_at').value = (s.expire_at || '').replace(' ', 'T').slice(0,16);
  onScheduleChange();
}
function appendInputField(f) {
  const row = document.createElement('div');
  row.className = 'input-field-row';
  row.style.cssText = 'display:grid;grid-template-columns:2fr 1fr 1fr 2fr 2fr auto;gap:0.3rem;margin-bottom:0.3rem;align-items:center;';
  row.innerHTML = `
    <input type="text" class="if-name" value="${esc(f.name||'')}" placeholder="参数名" style="padding:0.2rem 0.4rem;font-size:0.82rem;border:1px solid var(--border);border-radius:4px;">
    <select class="if-type" style="padding:0.2rem;font-size:0.82rem;">
      ${['string','number','boolean'].map(t=>`<option ${f.type===t?'selected':''}>${t}</option>`).join('')}
    </select>
    <label style="display:flex;align-items:center;gap:0.2rem;font-size:0.78rem;">
      <input type="checkbox" class="if-required" ${f.required?'checked':''} style="width:auto;">必填
    </label>
    <input type="text" class="if-default" value="${esc(f.default!=null?String(f.default):'')}" placeholder="默认值" style="padding:0.2rem 0.4rem;font-size:0.82rem;border:1px solid var(--border);border-radius:4px;">
    <input type="text" class="if-enum" value="${esc((f.enum||[]).join(', '))}" placeholder="枚举(逗号分隔)" style="padding:0.2rem 0.4rem;font-size:0.82rem;border:1px solid var(--border);border-radius:4px;">
    <button onclick="this.closest('.input-field-row').remove()" style="background:none;border:none;color:var(--danger,#c62828);cursor:pointer;">✕</button>
  `;
  document.getElementById('f-inputs-list').appendChild(row);
}

// SortableJS 初始化
function initSortable(container) {
  if (typeof Sortable === 'undefined') return;
  Sortable.create(container, {
    group: {name: 'blocks', pull: false, put: false},  // 禁止跨容器拖（子块只在父容器内排序）
    handle: '.block-handle',
    animation: 150,
    ghostClass: 'sortable-ghost',
    chosenClass: 'sortable-chosen',
    dragClass: 'sortable-drag',
    onEnd: () => reindexBlocks(),
  });
}

async function saveFlow(silentAfterSave) {
  const name = document.getElementById('f-name').value.trim();
  if (!name) { showToast('名称必填'); return null; }
  const blocks = serializeBlocks();
  const description = document.getElementById('f-description').value;
  const inputs = serializeInputs();
  const schedule = serializeSchedule();
  // 先做前端轻校验：worker 必须有 waker 和 task，块必须有 id
  const err = validateBlocksFrontend(blocks);
  if (err) { showValidation('err', err); return null; }
  hideValidation();
  try {
    let r;
    if (editingName) {
      r = await fetch('/api/wakerflow/items/' + encodeURIComponent(editingName), {
        method: 'PUT', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({blocks, description, inputs, ...schedule}),
      });
    } else {
      r = await fetch('/api/wakerflow/items', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({name, blocks, description, inputs, ...schedule}),
      });
    }
    const d = await r.json();
    if (!r.ok) {
      showValidation('err', d.detail || '保存失败');
      return null;
    }
    if (!silentAfterSave) {
      showToast(editingName ? '已更新' : '已创建「' + name + '」');
    }
    // 新建后切换为编辑模式
    if (!editingName) {
      editingName = name;
      document.getElementById('f-name').readOnly = true;
    }
    loadFlows();
    return name;
  } catch (e) { showToast('保存失败: ' + e.message, 'error'); return null; }
}

// 前端轻校验（详细错误交给后端，这里只拦明显空字段）
function validateBlocksFrontend(blocks) {
  const ids = new Set();
  function check(list, path) {
    for (let i = 0; i < list.length; i++) {
      const b = list[i];
      const where = `${path}[${i+1}]`;
      if (!b.id) return `${where}: 缺少步骤 ID`;
      if (ids.has(b.id)) return `${where}: ID 重复「${b.id}」`;
      ids.add(b.id);
      if (b.type === 'worker') {
        if (!b.waker) return `${where}「${b.id}」: worker 未选择 waker`;
        if (!b.task) return `${where}「${b.id}」: worker 任务描述为空`;
      }
      if (b.type === 'ask_user') {
        if (!b.question) return `${where}「${b.id}」: ask_user 问题为空`;
        if (!b.options || b.options.length === 0) return `${where}「${b.id}」: ask_user 至少一个选项`;
      }
      if (b.type === 'action' && !b.url) return `${where}「${b.id}」: action 缺少 URL`;
      if ((b.type === 'parallel' || b.type === 'pipeline') && (!b.children || b.children.length === 0)) {
        return `${where}「${b.id}」: ${b.type} 至少一个子步骤`;
      }
      if (b.children) {
        const sub = check(b.children, where + '.children');
        if (sub) return sub;
      }
    }
    return null;
  }
  return check(blocks, '步骤');
}

// 保存并运行
async function runEditingFlow() {
  const name = await saveFlow(true);
  if (!name) return;
  await runFlow(name);
}

// 导出当前编辑的 flow 为 YAML（调后端转）
async function exportYaml() {
  const blocks = serializeBlocks();
  const name = document.getElementById('f-name').value.trim() || 'exported';
  const description = document.getElementById('f-description').value;
  const inputs = serializeInputs();
  // 借用保存接口校验 + 拿 YAML：直接 GET 已存的，或临时 POST
  // 简化：直接前端不转，提示用导入导出弹窗看
  try {
    // W4/H1: 走非持久化 /preview 转换（旧路借 __export_tmp__ 持久化中转，
    // 而 FlowStore 拒绝 _ 开头的名字——导出从未成功过）
    const err = validateBlocksFrontend(blocks);
    if (err) { showValidation('err', err); return; }
    let r = await fetch('/api/wakerflow/preview', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({name, blocks, description, inputs, ...serializeSchedule()}),
    });
    const d = await r.json();
    if (!r.ok) { showValidation('err', d.detail || '导出失败'); return; }
    // 下载
    const yaml = d.yaml;
    const blob = new Blob([yaml], {type: 'text/yaml'});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = name + '.yaml';
    a.click();
    URL.revokeObjectURL(a.href);
    showToast('YAML 已下载');
  } catch (e) { showToast('导出失败: ' + e.message, 'error'); }
}

// 从 YAML 导入弹窗
function showImportYaml() {
  document.getElementById('yaml-import-text').value = '';
  document.getElementById('yaml-import-msg').style.display = 'none';
  document.getElementById('yaml-overlay').style.display = 'flex';
}
function closeYamlOverlay() {
  document.getElementById('yaml-overlay').style.display = 'none';
}
async function doImportYaml() {
  const yaml = document.getElementById('yaml-import-text').value;
  const msg = document.getElementById('yaml-import-msg');
  if (!yaml.trim()) { msg.className='validation-msg err'; msg.textContent='YAML 为空'; msg.style.display='block'; return; }
  // W4/H1: 走非持久化 /preview 转换（旧路借 __import_tmp__ 持久化中转，
  // FlowStore 拒绝 _ 开头名字——导入从未成功过）
  try {
    let r = await fetch('/api/wakerflow/preview', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({name: 'import-preview', yaml}),
    });
    const d = await r.json();
    if (!r.ok) { msg.className='validation-msg err'; msg.textContent=d.detail||'解析失败'; msg.style.display='block'; return; }
    const f = d;
    // 填回编辑器
    document.getElementById('f-name').value = editingName || f.name || '';
    document.getElementById('f-description').value = f.description || '';
    document.getElementById('blocks-list').innerHTML = '';
    (f.blocks || []).forEach(b => appendBlockEl('blocks-list', b));
    renderInputs(f.inputs || []);
    reindexBlocks();
    closeYamlOverlay();
    showToast('已从 YAML 导入，检查后点保存');
  } catch (e) {
    msg.className='validation-msg err'; msg.textContent='导入失败: ' + e.message; msg.style.display='block';
  }
}

async function deleteFlow(name) {
  const ok = await uiConfirm({
    title: '删除 Flow',
    body: `删除 flow「${name}」？此操作不可恢复。`,
    danger: true,
  });
  if (!ok) return;
  try {
    const r = await fetch('/api/wakerflow/items/' + encodeURIComponent(name), {method:'DELETE'});
    if (!r.ok) throw new Error('删除失败');
    showToast('已删除');
    loadFlows();
  } catch (e) { showToast(e.message, 'error'); }
}

// ════════════════════════════════════════════════════════════
// 运行 + 日志（异步：flow 在主进程后台跑，不阻塞 chat）
// ════════════════════════════════════════════════════════════
let _pendingRunFlow = null;  // 待运行的 flow 名（等 inputs 收集）

async function runFlow(name) {
  // 先拉 flow 定义，看有无需要收集的 inputs
  let inputsDecl = [];
  try {
    const r = await fetch('/api/wakerflow/items/' + encodeURIComponent(name));
    const d = await r.json();
    inputsDecl = (d.flow && d.flow.inputs) || [];
  } catch (e) { /* 拉不到就直接空 inputs 跑 */ }

  if (inputsDecl.length === 0) {
    // 无 inputs，直接跑
    await invokeAndPoll(name, {});
    return;
  }
  // 有 inputs：弹窗收集
  _pendingRunFlow = name;
  document.getElementById('run-inputs-name').textContent = name;
  const fieldsEl = document.getElementById('run-inputs-fields');
  fieldsEl.innerHTML = inputsDecl.map(f => {
    const reqStar = f.required ? '<span style="color:var(--danger,#c62828)">*</span>' : '';
    const defVal = f.default != null ? String(f.default) : '';
    let inputHtml;
    if (f.type === 'boolean') {
      inputHtml = `<select class="ri-input" data-name="${esc(f.name)}" data-type="boolean">
        <option value="false" ${defVal!=='true'?'selected':''}>false</option>
        <option value="true" ${defVal==='true'?'selected':''}>true</option>
      </select>`;
    } else if (f.enum && f.enum.length) {
      inputHtml = `<select class="ri-input" data-name="${esc(f.name)}" data-type="${esc(f.type)}">
        ${f.enum.map(v => `<option value="${esc(String(v))}" ${String(f.default)===String(v)?'selected':''}>${esc(String(v))}</option>`).join('')}
      </select>`;
    } else {
      inputHtml = `<input type="${f.type==='number'?'number':'text'}" class="ri-input" data-name="${esc(f.name)}" data-type="${esc(f.type)}" value="${esc(defVal)}" placeholder="${esc(f.name)}">`;
    }
    return `<div><label style="font-size:0.82rem;">${esc(f.name)} ${reqStar} <span class="hint">(${esc(f.type)})</span></label>${inputHtml}</div>`;
  }).join('');
  document.getElementById('run-inputs-overlay').style.display = 'flex';
}

function closeRunInputs() {
  document.getElementById('run-inputs-overlay').style.display = 'none';
  _pendingRunFlow = null;
}

async function doRunWithInputs() {
  const name = _pendingRunFlow;
  if (!name) return;
  // 收集 inputs
  const inputs = {};
  let missing = [];
  document.querySelectorAll('.ri-input').forEach(el => {
    const fname = el.dataset.name;
    const ftype = el.dataset.type;
    let val = el.value;
    if (ftype === 'number') val = val === '' ? null : Number(val);
    else if (ftype === 'boolean') val = val === 'true';
    if (val === '' || val === null) {
      // 留空：不传（后端按 required 校验）
      return;
    }
    inputs[fname] = val;
  });
  // 检查必填（轻校验，详细由后端）
  const r = await fetch('/api/wakerflow/items/' + encodeURIComponent(name));
  const d = await r.json();
  const decl = (d.flow && d.flow.inputs) || [];
  for (const f of decl) {
    if (f.required && !(f.name in inputs)) missing.push(f.name);
  }
  if (missing.length) {
    showToast('缺少必填参数：' + missing.join(', '));
    return;
  }
  closeRunInputs();
  await invokeAndPoll(name, inputs);
}

async function invokeAndPoll(name, inputs) {
  showToast('flow「' + name + '」已提交，后台运行中...');
  let runId = null;
  try {
    const r = await fetch('/api/wakerflow/items/' + encodeURIComponent(name) + '/invoke', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({inputs}),
    });
    const d = await r.json();
    if (!r.ok) { showToast('运行失败: ' + (d.detail || ''), 'error'); return; }
    runId = d.run_id;
  } catch (e) { showToast('运行失败: ' + e.message, 'error'); return; }

  // 轮询 status（每 2s），最多轮询 5 分钟
  const deadline = Date.now() + 5 * 60 * 1000;
  const poll = async () => {
    if (Date.now() > deadline) {
      showToast('flow 仍在运行（超时停止轮询），run_id=' + runId + '。可查日志。');
      loadFlows();
      return;
    }
    try {
      const r = await fetch('/api/wakerflow/items/' + encodeURIComponent(name) + '/runs/' + encodeURIComponent(runId) + '/status');
      const d = await r.json();
      if (d.status === 'completed') {
        showToast('flow「' + name + '」完成 ✓ 点「📜 运行记录」查看产出');
        loadFlows();
        loadApprovals();
        return;
      }
      if (d.status === 'failed' || d.status === 'error') {
        showToast('flow「' + name + '」' + d.status + (d.error ? ': ' + d.error : ''), 'error');
        loadFlows();
        loadApprovals();
        return;
      }
      // running / pending / unknown → 继续轮询
      setTimeout(poll, 2000);
    } catch (e) {
      setTimeout(poll, 3000);
    }
  };
  setTimeout(poll, 1500);
  loadApprovals();  // 立即刷一次（flow 可能很快产生审批）
}

const _logCtx = { name:'', selectedId:'', tab:'result', timer:null, fp:'' };

function fmtDateTime(s){
  if(!s) return '—';
  const m = String(s).match(/(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?/);
  if(!m) return String(s);
  return m[2]+'-'+m[3]+' '+m[4]+':'+m[5]+(m[6]?':'+m[6]:'');
}
function fmtDuration(s){
  if(s==null || s==='') return '—';
  const n = Number(s);
  if(!Number.isFinite(n) || n<0) return '—';
  const sec = Math.round(n);
  if(sec<60) return sec+'s';
  const m = Math.floor(sec/60), r = sec%60;
  if(m<60) return m+'m'+(r?r+'s':'');
  return Math.floor(m/60)+'h'+(m%60)+'m';
}
function runStatusBadge(st){
  if(st==='completed'||st==='ok') return '<span class="badge badge-green">✓ 成功</span>';
  if(st==='running') return '<span class="badge badge-blue">▶ 运行中</span>';
  if(st==='failed'||st==='error') return '<span class="badge badge-red">✗ 失败</span>';
  if(st==='interrupted') return '<span class="badge badge-orange">⚠ 中断</span>';
  return '<span class="badge">'+esc(st||'—')+'</span>';
}

async function showLog(name) {
  _logCtx.name = name;
  _logCtx.selectedId = '';
  _logCtx.tab = 'result';
  _logCtx.fp = '';
  document.getElementById('log-flow-name').textContent = name;
  document.getElementById('log-overlay').style.display = 'flex';
  document.getElementById('run-table-body').innerHTML = '<tr><td colspan="5" class="hint">加载中……</td></tr>';
  document.getElementById('run-detail-result').innerHTML = '<p class="hint">加载中……</p>';
  document.getElementById('run-detail-events').innerHTML = '';
  document.getElementById('run-detail-meta').textContent = '';
  setRunTab('result');
  await refreshLogModal(true);
}
function showResult(name) { return showLog(name); }

function closeLog() {
  document.getElementById('log-overlay').style.display = 'none';
  if (_logCtx.timer) { clearTimeout(_logCtx.timer); _logCtx.timer = null; }
}

function setRunTab(tab){
  _logCtx.tab = tab;
  document.querySelectorAll('.run-tab').forEach(b => b.classList.toggle('active', b.dataset.tab===tab));
  document.getElementById('run-detail-result').style.display = tab==='result' ? '' : 'none';
  document.getElementById('run-detail-events').style.display = tab==='events' ? '' : 'none';
}

function _runRowHtml(rec, selected){
  const rid = rec.run_id || '';
  return `<tr data-run="${esc(rid)}"${selected?' class="selected"':''}>
    <td>${runStatusBadge(rec.status)}</td>
    <td>${esc(fmtDateTime(rec.started_at))}</td>
    <td>${esc(fmtDateTime(rec.ended_at))}</td>
    <td>${esc(fmtDuration(rec.duration_s))}</td>
    <td class="rid">${esc(rid)}</td>
  </tr>`;
}

async function refreshLogModal(selectLatest){
  const tbody = document.getElementById('run-table-body');
  try {
    const r = await fetch('/api/wakerflow/items/' + encodeURIComponent(_logCtx.name) + '/runs?limit=50');
    if(!r.ok) throw new Error('HTTP '+r.status);
    const j = await r.json();
    const runs = j.runs || [];
    const fp = runs.map(x => (x.run_id||'')+':'+(x.status||'')+':'+(x.ended_at||'')).join('|');
    if (fp !== _logCtx.fp) {
      _logCtx.fp = fp;
      if (!runs.length) {
        tbody.innerHTML = '<tr><td colspan="5" class="hint">暂无运行记录。点「▶ 运行」跑一次后会出现在这里。</td></tr>';
        document.getElementById('run-detail-result').innerHTML = '<p class="hint">暂无运行记录</p>';
        document.getElementById('run-detail-events').innerHTML = '';
        document.getElementById('run-detail-meta').textContent = '';
        _logCtx.selectedId = '';
      } else {
        if (selectLatest || !_logCtx.selectedId || !runs.some(x => x.run_id === _logCtx.selectedId)) {
          _logCtx.selectedId = runs[0].run_id;
        }
        tbody.innerHTML = runs.map(rec => _runRowHtml(rec, rec.run_id===_logCtx.selectedId)).join('');
      }
    }
    if (_logCtx.selectedId) await loadRunDetail(_logCtx.selectedId);
    const hasRunning = runs.some(x => x.status === 'running');
    if (_logCtx.timer) { clearTimeout(_logCtx.timer); _logCtx.timer = null; }
    if (hasRunning && document.getElementById('log-overlay').style.display !== 'none') {
      _logCtx.timer = setTimeout(() => refreshLogModal(false), 2500);
    }
  } catch(e) {
    tbody.innerHTML = `<tr><td colspan="5" class="hint">加载失败：${esc(e.message)}</td></tr>`;
  }
}

async function loadRunDetail(runId){
  const resEl = document.getElementById('run-detail-result');
  const evEl = document.getElementById('run-detail-events');
  const meta = document.getElementById('run-detail-meta');
  try {
    const r = await fetch('/api/wakerflow/items/' + encodeURIComponent(_logCtx.name) + '/runs/' + encodeURIComponent(runId));
    if(!r.ok) throw new Error('HTTP '+r.status);
    const d = await r.json();
    meta.textContent = (d.run_id||'') + ' · ' + (d.status||'—');
    resEl.innerHTML = renderFlowResult(d);
    const evs = d.events || [];
    evEl.innerHTML = evs.length ? evs.map(fmtEventLine).join('') : '<p class="hint">（无关键事件）</p>';
  } catch(e) {
    resEl.innerHTML = `<p class="hint">加载详情失败：${esc(e.message)}</p>`;
    evEl.innerHTML = '';
    meta.textContent = '';
  }
}

function renderFlowResult(d){
  if (d.status === 'running') {
    return '<p class="hint">▶ 运行中，节点完成后这里会显示产出。</p>';
  }
  if (d.status === 'interrupted' && !d.returns && !(d.nodes||[]).length) {
    return '<p class="hint">这次运行没有正常收尾（进程中断或崩溃）。</p>';
  }
  const returns = d.returns || {};
  const returnKeys = Object.keys(returns);
  const nodes = d.nodes || [];
  let html = '';
  if (returnKeys.length) {
    html += '<h4>🎯 最终产出（returns）</h4>';
    html += returnKeys.map(k => {
      const v = returns[k] || '';
      return `<div style="margin-bottom:0.8rem;border-left:3px solid var(--accent,#0066cc);padding-left:0.7rem;">
        <div class="hint" style="font-weight:600;">${esc(k)}</div>
        <div class="md-body">${mdSafe(v)}</div>
      </div>`;
    }).join('');
  }
  if (nodes.length) {
    html += '<h4>📝 各节点产出（' + nodes.length + '）</h4>';
    html += nodes.map(n => `
      <details style="margin-bottom:0.4rem;border:1px solid var(--border);border-radius:6px;padding:0.4rem 0.7rem;">
        <summary style="cursor:pointer;font-size:0.85rem;">
          <strong>${esc(n.node_id)}</strong>
          <span class="badge ${n.status==='ok'?'badge-green':'badge-red'}" style="margin-left:0.5rem;">${esc(n.status||'—')}</span>
        </summary>
        <div class="md-body" style="margin-top:0.4rem;max-height:400px;overflow-y:auto;">${mdSafe(n.result)}</div>
      </details>`).join('');
  }
  if (!html) {
    if (d.error) return '<p class="hint">失败：'+esc(d.error)+'</p>';
    return '<p class="hint">（本次没有可展示的产出）</p>';
  }
  return html;
}

function fmtEventLine(e){
  const t = e.type || '';
  const ts = fmtDateTime(e.ts||'').split(' ').pop() || '';
  const tsHtml = ts && ts!=='—' ? `<span class="ts">${esc(ts)}</span>` : '';
  const nid = e.node_id || '';
  let body = esc(t);
  if (t==='flow_start') body = '开始';
  else if (t==='flow_end') body = '结束 · '+esc(e.status||'');
  else if (t==='node_start') body = '▶ 节点 '+esc(nid)+(e.node_type?'（'+esc(e.node_type)+'）':'');
  else if (t==='node_end') body = (e.status==='ok'?'✓ ':'✗ ')+'节点 '+esc(nid)+' · '+esc(e.status||'');
  else if (t==='node_result') body = '产出 '+esc(nid);
  else if (t==='worker_spawn') body = 'fork '+esc(e.waker||'')+(nid?' · '+esc(nid):'');
  else if (t==='worker_ready') body = '就绪 '+esc(e.waker||'');
  return `<div class="run-event-line">${tsHtml}${body}</div>`;
}

// ════════════════════════════════════════════════════════════
// 审批
// ════════════════════════════════════════════════════════════
async function loadApprovals() {
  try {
    const r = await fetch('/api/wakerflow/approvals');
    const d = await r.json();
    const list = d.approvals || [];
    const badge = document.getElementById('approval-badge');
    badge.textContent = '审批 ' + list.length;
    badge.classList.toggle('empty', list.length === 0);
    return list;
  } catch (e) { return []; }
}

async function showApprovals() {
  const list = await loadApprovals();
  const el = document.getElementById('approval-list');
  if (!list.length) {
    el.innerHTML = '<p class="hint">无待审批项。运行含 ask_user 节点的 flow 后，审批卡片会出现在这里。</p>';
  } else {
    el.innerHTML = list.map(a => {
      // 前置节点产出（审批上下文）
      const ctxHtml = (a.context && a.context.length) ? `
        <details style="margin:0.6rem 0;" open>
          <summary class="hint" style="cursor:pointer;">📋 前置节点产出（${a.context.length} 个，已跑完）</summary>
          <div style="margin-top:0.4rem;">
            ${a.context.map(c => `
              <div style="margin-bottom:0.6rem;border-left:3px solid ${c.status==='ok'?'#27ae60':'#e74c3c'};padding-left:0.6rem;">
                <div class="hint"><strong>${esc(c.node_id)}</strong> · ${esc(c.status)}</div>
                <div class="md-body" style="font-size:0.84rem;max-height:240px;overflow-y:auto;margin-top:0.2rem;">${mdSafe(c.result)}</div>
              </div>
            `).join('')}
          </div>
        </details>
      ` : '';
      return `
      <div class="card" style="margin-bottom:1rem;padding:1rem;">
        <div class="hint" style="margin-bottom:0.3rem;">🔀 flow: <strong>${esc(a.flow_name||'')}</strong> · run: ${esc((a.run_id||'').slice(-12))} · ${esc(a.created_ts||'')}</div>
        <div class="q" style="font-size:1.05rem;margin:0.6rem 0;">${esc(a.question||'')}</div>
        ${ctxHtml}
        <div class="opts" style="margin-top:0.8rem;">
          ${(a.options||[]).map(o => `
            <button data-run="${esc(a.run_id)}" data-answer="${esc(o.value)}" class="btn-small opt-btn">${esc(o.label)}</button>
          `).join('')}
          <button data-cancel-run="${esc(a.run_id)}" class="btn-small" style="color:var(--danger,#c62828);">✕ 取消运行</button>
        </div>
      </div>
    `;}).join('');
  }
  document.getElementById('approval-overlay').style.display = 'flex';
}

function closeApproval() {
  document.getElementById('approval-overlay').style.display = 'none';
}

// 审批/取消按钮（事件委托，一次性注册；#approval-list 在静态 DOM）。
// P2-23：原 onclick="submitApproval('${esc(...)}',...)" 的属性值会先被 HTML
// 实体解码再当 JS 编译（esc 把 ' 转成 &#39; 也拦不住——解码回 ' 后字符串
// 照常闭合），o.value 经 YAML 导入无字符集校验即可注入。改 data-* 后
// run_id/answer 只作纯数据经 dataset 传递，全程不进 JS 编译器。
// 取消按钮同规约：run_id 走 data-cancel-run 属性，不走 onclick 拼接。
document.getElementById('approval-list').addEventListener('click', e => {
  const cancelBtn = e.target.closest('button[data-cancel-run]');
  if (cancelBtn) {
    cancelApproval(cancelBtn.dataset.cancelRun);
    return;
  }
  const btn = e.target.closest('button[data-run]');
  if (!btn) return;
  submitApproval(btn.dataset.run, btn.dataset.answer);
});

// Flow 卡片按钮（事件委托，一次性注册；#flow-list 在静态 DOM）。
// 与上方审批委托同规约：原 onclick="runFlow('${esc(f.name)}')" 把 flow 名
// 当 JS 源码编译——blocks 模式创建的 flow 名无字符集校验（YAML 导入/积木块
// 自由输入），名字里的 ' 经实体解码后照样闭合字符串，尾随部分成为可执行
// JS（esc 只管 HTML 上下文，管不住 JS 上下文）。改 data-flow + data-act 后
// 名字只作纯数据经 dataset 传递，全程不进 JS 编译器。
document.getElementById('flow-list').addEventListener('click', e => {
  const btn = e.target.closest('button[data-flow]');
  if (!btn) return;
  const name = btn.dataset.flow;
  switch (btn.dataset.act) {
    case 'run': runFlow(name); break;
    case 'result':
    case 'log': showLog(name); break;
    case 'toggle': toggleFlowEnabled(name, btn.dataset.flowEnable === '1'); break;
    case 'edit': editFlow(name); break;
    case 'delete': deleteFlow(name); break;
  }
});

document.getElementById('log-overlay').addEventListener('click', e => {
  if (e.target.id === 'log-overlay') closeLog();
});
document.getElementById('run-table-body').addEventListener('click', e => {
  const tr = e.target.closest('tr[data-run]');
  if (!tr) return;
  const rid = tr.dataset.run;
  if (!rid || rid === _logCtx.selectedId) return;
  _logCtx.selectedId = rid;
  document.querySelectorAll('#run-table-body tr').forEach(row => row.classList.toggle('selected', row === tr));
  loadRunDetail(rid);
});
document.querySelector('.run-detail-head').addEventListener('click', e => {
  const btn = e.target.closest('.run-tab');
  if (!btn) return;
  setRunTab(btn.dataset.tab);
});

function closeResult() { closeLog(); }

async function submitApproval(runId, answer) {
  try {
    const r = await fetch('/api/wakerflow/approvals/' + encodeURIComponent(runId), {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({answer}),
    });
    if (!r.ok) throw new Error('提交失败');
    showToast('已响应：' + answer);
    showApprovals();  // 刷新
    loadApprovals();  // 更新 badge
  } catch (e) { showToast(e.message, 'error'); }
}

// 取消挂起中的审批：审批文件写 cancelled，flow 以 failed 终态收尾
//（不必干等 ask 超时，防重入键随终态释放）。
async function cancelApproval(runId) {
  try {
    const r = await fetch('/api/wakerflow/approvals/' + encodeURIComponent(runId) + '/cancel', {
      method: 'POST',
    });
    if (!r.ok) throw new Error('取消失败');
    showToast('已取消该次运行');
    showApprovals();  // 刷新
    loadApprovals();  // 更新 badge
  } catch (e) { showToast(e.message, 'error'); }
}

// ════════════════════════════════════════════════════════════
// 辅助
// ════════════════════════════════════════════════════════════
function esc(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
// 前端审查(高)：节点产出/returns/审批上下文三处 markdown 渲染原先是
// 「marked 解析 esc 后文本」直插 innerHTML——esc 挡住了原始 HTML 注入，
// 但 markdown 链接语法
// [x](javascript:...) 经 marked 会生成可点击的 javascript: URL（esc 不改
// 括号内的 scheme 文本，data:/vbscript: 同理）。chat.js 的 sanitizeHtml 归
// 聊天页所有（并行任务禁碰），此处就地等价收口：marked 渲染结果挂到
// detached div（不进文档流），遍历 a[href] 用 mdLinkAllowed 白名单判定，
// 不通过的中和为 href="#"。
function mdLinkAllowed(href) {
  // 白名单判定：与浏览器导航同一套 WHATWG URL 解析——new URL 先剥
  // \t\r\n 与首尾 C0 空白再取 scheme，HTML 实体解码后的 `jav&#x09;ascript:`
  // 混淆不成立（chat.js P1-8 同一结论，那边是黑名单方向，这里白名单）。
  // 相对 URL 按 location.href 解析成 http/https 放行；连 URL 都解析抛错的
  // 值按拒绝处理（黑名单方向下浏览器同样无法导航，判安全；白名单方向
  // 判拒绝更保守）。纯函数（无 DOM 依赖），node 可直测。
  try {
    const proto = new URL(String(href), location.href).protocol;
    return proto === 'http:' || proto === 'https:' || proto === 'mailto:';
  } catch (e) {
    return false;
  }
}
function mdSafe(text) {
  const src = (text == null) ? '' : String(text);
  let html = '';
  if (typeof marked !== 'undefined') {
    try { html = marked.parse(esc(src)); } catch (e) { html = ''; }
  }
  if (!html) html = '<pre style="white-space:pre-wrap">' + esc(src) + '</pre>';
  const holder = document.createElement('div');
  holder.innerHTML = html;
  holder.querySelectorAll('a[href]').forEach(a => {
    if (!mdLinkAllowed(a.getAttribute('href'))) {
      a.setAttribute('href', '#');
      a.setAttribute('title', '已拦截不安全的链接目标（仅允许 http/https/mailto）');
      a.removeAttribute('target');
    }
  });
  return holder.innerHTML;
}
function showValidation(type, text) {
  const el = document.getElementById('validation-msg');
  el.className = 'validation-msg ' + type;
  el.textContent = text;
  el.style.display = 'block';
}
function hideValidation() {
  document.getElementById('validation-msg').style.display = 'none';
}

// ════════════════════════════════════════════════════════════
// 初始化
// ════════════════════════════════════════════════════════════
async function loadWakerOptions() {
  try {
    const r = await fetch('/api/waker/items');
    const d = await r.json();
    wakerOptions = (d.wakers || []).map(w => ({name: w.name, description: w.description || ''}));
  } catch (e) { wakerOptions = []; }
}

async function loadToolOptions() {
  try {
    const r = await fetch('/api/tools');
    const d = await r.json();
    toolOptions = (d.tools || []).map(t => ({name: t.name, description: t.description || ''}));
  } catch (e) { toolOptions = []; }
}

async function init() {
  await Promise.all([loadWakerOptions(), loadToolOptions()]);
  loadFlows();
  loadApprovals();
  // 初始化顶层 Sortable
  initSortable(document.getElementById('blocks-list'));
  // 每 15s 刷新审批 badge
  setInterval(loadApprovals, 15000);
}
init();
