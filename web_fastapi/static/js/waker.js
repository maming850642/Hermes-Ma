const API = '/api/waker';

// ---- 工具函数 ----
function escapeHtml(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function fmtTime(s){ return s ? s : '—'; }
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
  if(st==='ok') return '<span class="badge badge-green">✓ 成功</span>';
  if(st==='running') return '<span class="badge badge-blue">▶ 运行中</span>';
  if(st==='error'||st==='failed') return '<span class="badge badge-red">✗ 失败</span>';
  if(st==='interrupted') return '<span class="badge badge-orange">⚠ 中断</span>';
  return '<span class="badge">'+escapeHtml(st||'—')+'</span>';
}
function mdLinkAllowed(href){
  try {
    const proto = new URL(String(href), location.href).protocol;
    return proto === 'http:' || proto === 'https:' || proto === 'mailto:';
  } catch (e) { return false; }
}
function mdSafe(text){
  const src = (text == null) ? '' : String(text);
  let html = '';
  if (typeof marked !== 'undefined') {
    try { html = marked.parse(escapeHtml(src)); } catch (e) { html = ''; }
  }
  if (!html) html = '<pre style="white-space:pre-wrap">' + escapeHtml(src) + '</pre>';
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
function scheduleSummary(w){
  if(w.schedule_type==='interval') return `⏱ 每 ${w.interval_minutes||'?'} 分钟`;
  if(w.schedule_type==='daily') return `⏱ 每天 ${w.daily_at||'?'}`;
  return '⏱ 仅手动';
}
function statusBadges(w){
  const en = w.enabled
    ? '<span class="badge badge-green">启用</span>'
    : '<span class="badge badge-orange">停用</span>';
  let st = '<span class="badge">—</span>';
  if(w.last_status==='ok') st='<span class="badge badge-green">ok</span>';
  else if(w.last_status==='error'||w.last_status==='failed') st='<span class="badge badge-orange">error</span>';
  else if(w.last_status) st=`<span class="badge">${escapeHtml(w.last_status)}</span>`;
  return en + ' ' + st;
}
// 调度延迟标记：后端判定 enabled + next_run_at 已过 120s 容差且未在运行
function overdueChip(w){
  return w.overdue ? '<span class="badge badge-red" title="计划时间已过仍未执行（手动触发不刷新下次运行时间，含容差）">⏰ 调度延迟中</span>' : '';
}

// ---- 列表加载 ----
async function loadWakers(){
  const list = document.getElementById('waker-list');
  try {
    const r = await fetch(`${API}/items`);
    if(!r.ok) throw new Error('HTTP '+r.status);
    const j = await r.json();
    const arr = j.wakers || [];
    if(!arr.length){ list.innerHTML = '<p class="hint">暂无 Waker，点上方按钮新建。</p>'; return; }
    list.innerHTML = arr.map(w=>{
      // 名称做 URL 编码后嵌入 onclick，避免引号/特殊字符破坏 JS 字符串
      const ne = encodeURIComponent(w.name);
      const tools = (w.tools && w.tools.length) ? w.tools.join(', ') : '全部';
      const runs = w.max_runs ? `${w.run_count||0}/${w.max_runs}` : `${w.run_count||0}`;
      const isActive = w.last_status === 'running' && w.active_run_id;
      const activeProgress = isActive
        ? `<div id="wprogress-${escapeHtml(w.active_run_id)}" class="waker-progress">▶ 运行中...</div>`
        : '';
      const cardStyle = isActive ? 'border-color:var(--accent,#0066cc);box-shadow:0 0 0 2px rgba(0,102,204,0.15);' : '';
      return `<section class="card" style="${cardStyle}">
        <h3>${escapeHtml(w.name)} ${statusBadges(w)}</h3>
        <p class="hint" style="margin-bottom:0.4rem;">${escapeHtml(w.description||'(无描述)')}</p>
        <div class="waker-meta">
          <span>${scheduleSummary(w)}</span>
          <span>📁 ${escapeHtml(w.working_dir||'默认目录')}</span>
          <span>🛠 ${escapeHtml(tools)}</span>
          <span>🔒 ${escapeHtml(w.permission_mode||'—')}</span>
          <span>📡 ${w.api_enabled?'API 开':'API 关'}</span>
        </div>
        <div class="waker-meta">
          <span>下次运行：${fmtTime(w.next_run_at)}${overdueChip(w)}</span>
          <span>上次运行：${fmtTime(w.last_run_at)}</span>
          <span>运行次数：${runs}</span>
          ${w.expire_at?`<span>过期：${fmtTime(w.expire_at)}</span>`:''}
          ${w.api_token?`<span>token：<code>${escapeHtml(w.api_token)}</code></span>`:''}
        </div>
        ${activeProgress}
        <div class="waker-actions">
          <button onclick="toggleEnabled('${ne}', ${!w.enabled})" class="btn-small">${w.enabled?'停用':'启用'}</button>
          <button onclick="runNow('${ne}')" class="btn-small" ${isActive?'disabled':''}>▶ 立即运行</button>
          <button onclick="showLogs('${ne}', '${escapeHtml(w.name)}')" class="btn-small">📜 运行记录</button>
          <button onclick="openEditForm('${ne}')" class="btn-small">✏ 编辑</button>
          <button onclick="deleteWaker('${ne}', '${escapeHtml(w.name)}')" class="btn-small danger">🗑 删除</button>
        </div>
      </section>`;
    }).join('');
    // 启动所有活跃 waker 的进度轮询
    arr.filter(w => w.last_status === 'running' && w.active_run_id).forEach(w => {
      pollWakerProgress(decodeURIComponent(w.name), w.active_run_id);
    });
  } catch(e) {
    list.innerHTML = `<p class="hint">加载失败：${escapeHtml(e.message)}（后端 API 尚未就绪？）</p>`;
  }
}

// 单个活跃 waker 的进度轮询（更新卡片进度行）
const _wakerPolling = new Set();
function pollWakerProgress(wakerName, runId) {
  if (_wakerPolling.has(runId)) return;
  _wakerPolling.add(runId);
  const tick = async () => {
    try {
      const ne = encodeURIComponent(wakerName);
      const r = await fetch(`${API}/items/${ne}/runs/${encodeURIComponent(runId)}`);
      const el = document.getElementById('wprogress-' + runId);
      if (!el) { _wakerPolling.delete(runId); return; }
      if (r.status === 404) { setTimeout(tick, 2500); return; }  // jsonl 尚未落盘
      if (!r.ok) throw new Error('HTTP '+r.status);
      const d = await r.json();
      const st = d.status || '';
      if (st === 'ok' || st === 'error' || st === 'failed' || st === 'interrupted') {
        if (st === 'ok') {
          el.innerHTML = `<span style="color:#27ae60;">✓ 完成</span>`;
        } else {
          el.innerHTML = `<span style="color:#e74c3c;">✗ ${escapeHtml(st)}</span>`;
        }
        _wakerPolling.delete(runId);
        setTimeout(()=>loadWakers(), 1500);
        return;
      }
      const toolCalls = d.tool_calls || [];
      const toolStr = toolCalls.length ? ` · 🔧${toolCalls.map(escapeHtml).join(',')}` : '';
      el.innerHTML = `<span style="color:var(--accent,#0066cc);">▶ 运行中 · 💭${d.token_count||0} token${toolStr}</span>`;
      setTimeout(tick, 2500);
    } catch (e) {
      setTimeout(tick, 3500);
    }
  };
  tick();
}

// ---- 表单：新建/编辑 ----
// 加载工具列表（从 /api/tools 拉真实工具名+描述），填充 checkbox 多选区
let _toolsLoaded = false;
async function loadToolOptions(){
  if(_toolsLoaded) return;
  const box = document.getElementById('f-tools-list');
  if(!box) return;
  try {
    const r = await fetch('/api/tools');
    if(!r.ok) throw new Error('HTTP '+r.status);
    const j = await r.json();
    const tools = j.tools || [];
    if(!tools.length){ box.innerHTML='<span class="hint">（无工具）</span>'; return; }
    box.innerHTML = tools.map(t=>{
      const name = t.name || '';
      const desc = t.description || t.desc || '';
      return `<label title="${escapeHtml(desc)}"><input type="checkbox" class="f-tool-cb" value="${escapeHtml(name)}"> <span>${escapeHtml(name)}</span>${desc?`<span class="tool-desc">${escapeHtml(desc.slice(0,30))}</span>`:''}</label>`;
    }).join('');
    _toolsLoaded = true;
  } catch(e) {
    box.innerHTML = `<span class="hint">工具列表加载失败：${escapeHtml(e.message)}</span>`;
  }
}

// 回填勾选：编辑模式时把 waker.tools 勾上
function _setToolSelection(selected){
  document.querySelectorAll('.f-tool-cb').forEach(cb=>{
    cb.checked = (selected||[]).includes(cb.value);
  });
}
// 读取勾选：提交时取选中的工具名
function _getToolSelection(){
  return Array.from(document.querySelectorAll('.f-tool-cb:checked')).map(cb=>cb.value);
}

function openCreateForm(){
  document.getElementById('form-mode').value='create';
  document.getElementById('waker-form').reset();
  document.getElementById('f-name').readOnly=false;
  document.getElementById('f-name').required=true;
  document.getElementById('form-summary-text').textContent='📝 新建 Waker';
  onScheduleChange();
  loadToolOptions();  // 拉工具列表
  // 新建时全不勾（=允许全部）
  setTimeout(()=>_setToolSelection([]), 300);
  document.getElementById('waker-form-details').style.display='block';
  document.getElementById('waker-form-details').scrollIntoView({behavior:'smooth', block:'start'});
  document.getElementById('f-name').focus();
}

async function openEditForm(nameEncoded){
  try {
    const r = await fetch(`${API}/items/${nameEncoded}`);
    if(!r.ok) throw new Error('HTTP '+r.status);
    const j = await r.json();
    const w = j.waker;
    if(!w) throw new Error('响应缺少 waker 字段');
    document.getElementById('form-mode').value='edit';
    document.getElementById('f-name').value=w.name||'';
    document.getElementById('f-name').readOnly=true;
    document.getElementById('f-name').required=false;
    document.getElementById('f-description').value=w.description||'';
    document.getElementById('f-identity').value=w.identity||'';
    document.getElementById('f-persona').value=w.persona||'';
    document.getElementById('f-bible').value=w.bible||'';
    document.getElementById('f-working_dir').value=w.working_dir||'';
    document.getElementById('f-task_prompt').value=w.task_prompt||'';
    document.getElementById('f-permission_mode').value=w.permission_mode||'before_changes';
    document.getElementById('f-schedule_type').value=w.schedule_type||'none';
    document.getElementById('f-interval_minutes').value=w.interval_minutes||60;
    // daily_at 可能 "HH:MM" 或 "HH:MM:SS"，time 输入只接受 "HH:MM"
    document.getElementById('f-daily_at').value=(w.daily_at||'09:00').slice(0,5);
    // 工具白名单：回填勾选（w.tools 数组）
    loadToolOptions();
    setTimeout(()=>_setToolSelection(w.tools||[]), 300);
    document.getElementById('f-max_runs').value=w.max_runs||0;
    // expire_at 可能 "YYYY-MM-DD HH:MM:SS"，datetime-local 要 "YYYY-MM-DDTHH:MM"
    document.getElementById('f-expire_at').value=(w.expire_at||'').replace(' ','T').slice(0,16);
    document.getElementById('f-api_enabled').checked=!!w.api_enabled;
    document.getElementById('form-summary-text').textContent=`📝 编辑 Waker：${w.name}`;
    onScheduleChange();
    document.getElementById('waker-form-details').style.display='block';
    document.getElementById('waker-form-details').scrollIntoView({behavior:'smooth', block:'start'});
    showToast('已加载「'+(w.name||'')+'」配置');
  } catch(e) {
    showToast('加载失败：'+e.message, 'error');
  }
}

function closeForm(){
  document.getElementById('waker-form-details').style.display='none';
}

function onScheduleChange(){
  const t = document.getElementById('f-schedule_type').value;
  document.getElementById('f-interval-wrap').style.display = (t==='interval')?'':'none';
  document.getElementById('f-daily-wrap').style.display = (t==='daily')?'':'none';
}

document.getElementById('waker-form').addEventListener('submit', async (e)=>{
  e.preventDefault();
  const mode = document.getElementById('form-mode').value;
  const name = document.getElementById('f-name').value.trim();
  if(!name){ showToast('请填名称'); return; }
  // 工具白名单：取勾选的（全不勾=允许全部，送 undefined 让后端留空）
  const toolsArr = _getToolSelection();
  // 创建模式：空字段省略（后端补默认）；编辑模式：空字段送 null（显式清除）
  const blank = mode==='create' ? undefined : null;
  // interval_minutes：空/0 不送（后端校验 interval 模式须正整数，0 会触发 400）
  const intervalVal = +document.getElementById('f-interval_minutes').value;
  const body = {
    name,
    description: document.getElementById('f-description').value.trim() || blank,
    identity:    document.getElementById('f-identity').value.trim()    || blank,
    persona:     document.getElementById('f-persona').value.trim()     || blank,
    bible:       document.getElementById('f-bible').value.trim()       || blank,
    working_dir: document.getElementById('f-working_dir').value.trim() || blank,
    task_prompt: document.getElementById('f-task_prompt').value.trim() || blank,
    permission_mode: document.getElementById('f-permission_mode').value,
    schedule_type:   document.getElementById('f-schedule_type').value,
    interval_minutes: intervalVal > 0 ? intervalVal : undefined, // 0/空不送，后端用默认 60
    daily_at: document.getElementById('f-daily_at').value || blank,
    tools: toolsArr.length ? toolsArr : undefined, // 全不勾=不限工具（不送，后端留空=允许全部）
    max_runs: +document.getElementById('f-max_runs').value || 0,
    expire_at: document.getElementById('f-expire_at').value || blank,
    api_enabled: document.getElementById('f-api_enabled').checked,
  };
  // 清掉 undefined（编辑模式保留 null 以显式清空字段）
  Object.keys(body).forEach(k=> body[k]===undefined && delete body[k]);
  try {
    let r, j;
    if(mode==='create'){
      r = await fetch(`${API}/items`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
      j = await r.json();
      if(!r.ok || !j.ok) throw new Error(j.detail||j.message||('HTTP '+r.status));
      showToast(`已创建「${name}」`);
      const tok = j.waker && j.waker.api_token;
      closeForm();
      loadWakers();
      if(tok) showToken(tok); // 创建响应里含明文 token，弹窗展示
    } else {
      // 编辑：不送 name（path 已含）
      const {name:_n, ...patch} = body;
      r = await fetch(`${API}/items/${encodeURIComponent(name)}`, {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify(patch)});
      j = await r.json();
      if(!r.ok || !j.ok) throw new Error(j.detail||j.message||('HTTP '+r.status));
      showToast(`已更新「${name}」`);
      closeForm();
      loadWakers();
    }
  } catch(e) {
    showToast('保存失败：'+e.message, 'error');
  }
});

// ---- 卡片操作 ----
async function toggleEnabled(nameEncoded, enabled){
  try {
    const r = await fetch(`${API}/items/${nameEncoded}/enabled`, {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({enabled})});
    const j = await r.json();
    if(!r.ok||!j.ok) throw new Error(j.message||('HTTP '+r.status));
    showToast(`已${enabled?'启用':'停用'}`);
    loadWakers();
  } catch(e) { showToast('切换失败：'+e.message, 'error'); }
}

async function deleteWaker(nameEncoded, displayName){
  const ok = await uiConfirm({title:'删除 Waker', body:'确定删除「'+displayName+'」？不可逆！', danger:true, okText:'删除'});
  if(!ok) return;
  try {
    const r = await fetch(`${API}/items/${nameEncoded}`, {method:'DELETE'});
    const j = await r.json();
    if(!r.ok||!j.ok) throw new Error(j.message||('HTTP '+r.status));
    showToast(`已删除「${displayName}」`);
    loadWakers();
  } catch(e) { showToast('删除失败：'+e.message, 'error'); }
}

const _runNowInflight = new Set();
async function runNow(nameEncoded){
  if (_runNowInflight.has(nameEncoded)) return;  // L20: 防双击重复提交
  _runNowInflight.add(nameEncoded);
  try {
    const r = await fetch(`${API}/items/${nameEncoded}/invoke`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({})});
    const j = await r.json();
    if(!r.ok||!j.ok) throw new Error(j.message||('HTTP '+r.status));
    showToast(`已提交运行，run_id=${j.run_id||'?'}`);
    setTimeout(()=>loadWakers(), 1500);
  } catch(e) { showToast('运行失败：'+e.message, 'error'); }
  finally { _runNowInflight.delete(nameEncoded); }
}

// ---- 运行记录（上表下详情） ----
const _logCtx = { nameEncoded:'', displayName:'', selectedId:'', tab:'result', timer:null, fp:'' };

async function showLogs(nameEncoded, displayName){
  _logCtx.nameEncoded = nameEncoded;
  _logCtx.displayName = displayName;
  _logCtx.selectedId = '';
  _logCtx.tab = 'result';
  _logCtx.fp = '';
  document.getElementById('log-overlay').style.display='flex';
  document.getElementById('log-title').textContent = '运行记录：' + displayName;
  document.getElementById('run-table-body').innerHTML = '<tr><td colspan="5" class="hint">加载中……</td></tr>';
  document.getElementById('run-detail-result').innerHTML = '<p class="hint">加载中……</p>';
  document.getElementById('run-detail-events').innerHTML = '';
  document.getElementById('run-detail-meta').textContent = '';
  setRunTab('result');
  await refreshLogModal(true);
}

function closeLogs(){
  document.getElementById('log-overlay').style.display='none';
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
  return `<tr data-run="${escapeHtml(rid)}"${selected?' class="selected"':''}>
    <td>${runStatusBadge(rec.status)}</td>
    <td>${escapeHtml(fmtDateTime(rec.started_at))}</td>
    <td>${escapeHtml(fmtDateTime(rec.ended_at))}</td>
    <td>${escapeHtml(fmtDuration(rec.duration_s))}</td>
    <td class="rid">${escapeHtml(rid)}</td>
  </tr>`;
}

async function refreshLogModal(selectLatest){
  const tbody = document.getElementById('run-table-body');
  try {
    const r = await fetch(`${API}/items/${_logCtx.nameEncoded}/runs?limit=50`);
    if(!r.ok) throw new Error('HTTP '+r.status);
    const j = await r.json();
    const runs = j.runs || [];
    const fp = runs.map(x => (x.run_id||'')+':'+(x.status||'')+':'+(x.ended_at||'')).join('|');
    if (fp !== _logCtx.fp) {
      _logCtx.fp = fp;
      if (!runs.length) {
        tbody.innerHTML = '<tr><td colspan="5" class="hint">暂无运行记录。点「▶ 立即运行」跑一次后会出现在这里。</td></tr>';
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
    tbody.innerHTML = `<tr><td colspan="5" class="hint">加载失败：${escapeHtml(e.message)}</td></tr>`;
  }
}

async function loadRunDetail(runId){
  const resEl = document.getElementById('run-detail-result');
  const evEl = document.getElementById('run-detail-events');
  const meta = document.getElementById('run-detail-meta');
  try {
    const r = await fetch(`${API}/items/${_logCtx.nameEncoded}/runs/${encodeURIComponent(runId)}`);
    if(!r.ok) throw new Error('HTTP '+r.status);
    const d = await r.json();
    const nTools = (d.tool_calls||[]).length;
    meta.textContent = (d.run_id||'') + ' · ' + (d.status||'—') + (nTools ? ' · '+nTools+' 次工具' : '');
    if (d.status === 'running') {
      const tools = (d.tool_calls||[]).map(escapeHtml).join(', ');
      resEl.innerHTML = `<p class="hint">▶ 运行中 · 💭${d.token_count||0} token${tools?' · 🔧'+tools:''}</p>`;
    } else if (d.result) {
      resEl.innerHTML = '<div class="md-body">'+mdSafe(d.result)+'</div>';
    } else if (d.error) {
      resEl.innerHTML = '<p class="hint">失败：'+escapeHtml(d.error)+'</p>';
    } else if (d.status === 'interrupted') {
      resEl.innerHTML = '<p class="hint">这次运行没有正常收尾（进程中断或崩溃）。</p>';
    } else {
      resEl.innerHTML = '<p class="hint">（本次没有可展示的交付结果）</p>';
    }
    const evs = d.events || [];
    if (!evs.length) {
      evEl.innerHTML = '<p class="hint">（无关键事件）</p>';
    } else {
      evEl.innerHTML = evs.map(fmtEventLine).join('');
    }
  } catch(e) {
    resEl.innerHTML = `<p class="hint">加载详情失败：${escapeHtml(e.message)}</p>`;
    evEl.innerHTML = '';
    meta.textContent = '';
  }
}

function fmtEventLine(e){
  const t = e.type || '';
  const ts = fmtDateTime(e.ts||'').split(' ').pop() || '';
  const tsHtml = ts && ts!=='—' ? `<span class="ts">${escapeHtml(ts)}</span>` : '';
  const tool = e.tool_name || e.name || e.tool || '';
  const sum = e.summary || '';
  let body = escapeHtml(t);
  if (t==='run_start') body = '开始';
  else if (t==='run_end') body = '结束 · '+escapeHtml(e.status||'');
  else if (t==='run_error') body = '错误 · '+escapeHtml(e.message||'');
  else if (t==='tool_start' || t==='tool_call') {
    body = '🔧 '+escapeHtml(tool||'tool');
    if (sum) body += '  <span class="hint" style="margin:0">'+escapeHtml(sum)+'</span>';
  }
  else if (t==='todos_update') body = '☑ 待办'+(sum?' · '+escapeHtml(sum):'');
  else if (t==='complete') body = '完成';
  else if (t==='approval_auto_rejected') body = '审批自动拒绝'+(sum?' · '+escapeHtml(sum):'');
  else if (t==='human_approval_request') body = '等待审批'+(sum?' · '+escapeHtml(sum):'');
  return `<div class="run-event-line">${tsHtml}${body}</div>`;
}

// ---- API Token 弹窗 ----
function showToken(token){
  document.getElementById('token-overlay').style.display='flex';
  document.getElementById('token-value').value=token;
}
function closeToken(){ document.getElementById('token-overlay').style.display='none'; }
function copyToken(){
  const inp = document.getElementById('token-value');
  inp.select();
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(inp.value).then(()=>showToast('已复制')).catch(()=>{
      document.execCommand('copy'); showToast('已复制');
    });
  } else {
    document.execCommand('copy'); showToast('已复制');
  }
}

// 关闭遮罩：点遮罩空白处也可关
document.getElementById('log-overlay').addEventListener('click', e=>{ if(e.target.id==='log-overlay') closeLogs(); });
document.getElementById('token-overlay').addEventListener('click', e=>{ if(e.target.id==='token-overlay') closeToken(); });

// 运行记录表：点行看详情（run_id 走 dataset，不进 JS 字符串拼接）
document.getElementById('run-table-body').addEventListener('click', e=>{
  const tr = e.target.closest('tr[data-run]');
  if(!tr) return;
  const rid = tr.dataset.run;
  if(!rid || rid===_logCtx.selectedId) return;
  _logCtx.selectedId = rid;
  document.querySelectorAll('#run-table-body tr').forEach(row => row.classList.toggle('selected', row===tr));
  loadRunDetail(rid);
});
document.querySelector('.run-detail-head').addEventListener('click', e=>{
  const btn = e.target.closest('.run-tab');
  if(!btn) return;
  setRunTab(btn.dataset.tab);
});

// 初始加载
loadWakers();
