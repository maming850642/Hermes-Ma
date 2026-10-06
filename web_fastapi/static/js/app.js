// 通用交互：导航高亮 + toast + 项目空间切换 + 权限模式 / waker 选择器。
// 免认证形态（ADR-0005）：无登录守卫/工作区门禁；顶栏项目下拉由
// /api/projects 驱动，切换即 activate 并进入对话页。

// 导航高亮（/chat 而非 /）
document.querySelectorAll('.nav a').forEach(a => {
  if (location.pathname === a.getAttribute('href')) a.classList.add('active');
});

// Toast（config.html 等页面用到）。第二参 type='error' 时加 #toast.error
// 类（红底，main.css 用 --danger 变量着色），其余值同默认 info 形态。
function showToast(msg, type = 'info') {
  let t = document.getElementById('toast');
  if (!t) {
    t = document.createElement('div');
    t.id = 'toast';
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.classList.toggle('error', type === 'error');   // 非 error 调用要摘掉上一次的类
  t.classList.add('show');
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => t.classList.remove('show'), 2200);
}

// ---- 通用确认弹窗 uiConfirm（Promise 化的 confirm/alert 替代）----
// uiConfirm(opts) → Promise<boolean>。opts：
//   title       标题；body 正文（可空）
//   okText/cancelText  按钮文案（默认 确定/取消）
//   danger=true 确认按钮用 button.danger（红底白字）
//   items       字符串数组，正文下方逐行列出（受影响资源清单）
//   note        一句灰色小字提示
//   requireText 非空时出现输入框，输入与之完全相等才启用确认按钮
//   single=true 只有确认按钮、点遮罩不关闭（alert 替代 / 详情展示）
// 安全纪律同 _popItem：标题/正文/清单均为自由文本，全程 createElement +
// textContent，永不进 HTML 解析器。每次调用新建 <dialog class="dlg">，
// close 后从 DOM 移除；Enter=确认（requireText 满足时），Escape/取消/
// 遮罩=resolve(false)。dialog 的 cancel 事件 preventDefault 后手动收尾，
// 保证 Promise 必然落定，不悬挂待决回调。
function uiConfirm(opts) {
  return new Promise((resolve) => {
    const o = opts || {};
    let settled = false;
    const dlg = document.createElement('dialog');
    dlg.className = 'dlg ui-confirm';
    const finish = (val) => {
      if (settled) return;
      settled = true;
      if (dlg.open) dlg.close();
      dlg.remove();
      resolve(val);
    };

    const h3 = document.createElement('h3');
    h3.textContent = o.title || '确认操作';
    dlg.appendChild(h3);

    if (o.body) {
      const p = document.createElement('p');
      p.className = 'uc-body';
      p.textContent = String(o.body);
      dlg.appendChild(p);
    }

    if (Array.isArray(o.items) && o.items.length) {
      const ul = document.createElement('ul');
      ul.className = 'uc-items';
      for (const it of o.items) {
        const li = document.createElement('li');
        li.textContent = String(it);
        ul.appendChild(li);
      }
      dlg.appendChild(ul);
    }

    if (o.note) {
      const note = document.createElement('p');
      note.className = 'uc-note';
      note.textContent = String(o.note);
      dlg.appendChild(note);
    }

    let input = null;
    if (o.requireText) {
      input = document.createElement('input');
      input.type = 'text';
      input.className = 'uc-require';
      input.placeholder = '输入「' + o.requireText + '」以确认';
      input.setAttribute('autocomplete', 'off');
      input.spellcheck = false;
      dlg.appendChild(input);
    }

    const err = document.createElement('p');
    err.className = 'dlg-error';   // 复用现有红字提示样式（min-height 撑住布局）
    dlg.appendChild(err);

    const actions = document.createElement('div');
    actions.className = 'dlg-actions';
    if (!o.single) {
      const cancelBtn = document.createElement('button');
      cancelBtn.type = 'button';
      cancelBtn.textContent = o.cancelText || '取消';
      cancelBtn.addEventListener('click', () => finish(false));
      actions.appendChild(cancelBtn);
    }
    const okBtn = document.createElement('button');
    okBtn.type = 'button';
    okBtn.className = (o.danger && !o.single) ? 'danger' : 'btn-primary';
    okBtn.textContent = o.okText || '确定';
    okBtn.addEventListener('click', () => {
      if (input && input.value !== o.requireText) {
        err.textContent = '输入不一致：请输入「' + o.requireText + '」后再确认';
        input.focus();
        return;
      }
      finish(true);
    });
    actions.appendChild(okBtn);
    dlg.appendChild(actions);

    if (input) {
      okBtn.disabled = true;
      input.addEventListener('input', () => {
        const met = input.value === o.requireText;
        okBtn.disabled = !met;
        if (met) err.textContent = '';
      });
    }

    // Enter=确认（requireText 未满足时不动作，提示已由按钮禁用态承载）
    dlg.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      if (!okBtn.disabled) okBtn.click();
      else if (input) input.focus();
    });

    if (!o.single) {
      // 点遮罩（click 落在 dialog 元素自身）= 取消；single 模式不允许误点丢失详情
      dlg.addEventListener('click', (e) => { if (e.target === dlg) finish(false); });
    }

    // 原生 Esc 触发 cancel：preventDefault 后手动 close + resolve，防悬挂
    dlg.addEventListener('cancel', (e) => { e.preventDefault(); finish(false); });
    // 兜底：其他任何 close 路径也按取消落定
    dlg.addEventListener('close', () => {
      if (!settled) { settled = true; dlg.remove(); resolve(false); }
    });

    document.body.appendChild(dlg);
    dlg.showModal();
    (input || okBtn).focus();
  });
}

// ---- 输入框工具行：自定义浮层菜单（2026-09 界面改版，取代原生 select）----
// 控件层换popover，业务函数（setPermissionMode/setWaker/setModelProfile）
// 契约不变；popover 开合、选中态同步、选项填充的小助手集中在这里。

function _popSetSel(pop, v) {
  if (!pop) return;
  pop.querySelectorAll('.pop-item').forEach(x => x.classList.toggle('sel', x.dataset.v === v));
}

function _popApplyBtn(prefix, item) {
  if (!item) return;
  const ic = document.getElementById(prefix + '-ic');
  const label = document.getElementById(prefix + '-label');
  if (ic) ic.textContent = item.dataset.ic || '';
  if (label) label.textContent = item.dataset.short || '';
}

function _popItem(v, ic, title, desc) {
  // 安全构造：title/desc 全程 textContent，永不进 HTML 解析器
  // （waker name/description 与模型 display 都是自由文本，同旧 new Option 纪律）
  const div = document.createElement('div');
  div.className = 'pop-item';
  div.dataset.v = v; div.dataset.short = title; div.dataset.ic = ic;
  const i1 = document.createElement('span'); i1.className = 'pi-ic'; i1.textContent = ic;
  const tx = document.createElement('span'); tx.className = 'pi-tx';
  const b = document.createElement('b'); b.textContent = title;
  const sm = document.createElement('small'); sm.textContent = desc;
  tx.append(b, sm);
  const chk = document.createElement('span'); chk.className = 'pi-check'; chk.textContent = '✓';
  div.append(i1, tx, chk);
  return div;
}

function _bindPop(btnId, popId) {
  const btn = document.getElementById(btnId), pop = document.getElementById(popId);
  if (!btn || !pop) return;
  btn.addEventListener('click', e => {
    e.stopPropagation();
    document.querySelectorAll('.popover.show').forEach(p => { if (p !== pop) p.classList.remove('show'); });
    pop.classList.toggle('show');
  });
  // 选中任意项即收起菜单（业务监听另行挂在 pop 上）
  pop.addEventListener('click', e => {
    if (e.target.closest('.pop-item')) pop.classList.remove('show');
  });
}

document.addEventListener('click', e => {
  if (!e.target.closest('.popover') && !e.target.closest('.tool-btn'))
    document.querySelectorAll('.popover.show').forEach(p => p.classList.remove('show'));
});

// ---- V3 权限模式切换（full_access / before_changes / plan）----

const PERMISSION_MODE_LABELS = {
  full_access: '⚡ 完全访问',
  before_changes: '🔒 变更前审批',
  plan: '📋 计划模式',
};

async function setPermissionMode(mode) {
  try {
    const resp = await fetch('/api/config/permission-mode', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode }),
    });
    const data = await resp.json();
    if (data && data.ok) {
      // 与 setWaker→syncWakerSelect 同纪律：成功即同步浮层选中态 + 按钮图标
      const pop = document.getElementById('perm-pop');
      _popSetSel(pop, mode);
      _popApplyBtn('perm', pop ? pop.querySelector('.pop-item.sel') : null);
      showToast(`已切换：${PERMISSION_MODE_LABELS[mode] || mode}`);
    } else {
      showToast('切换权限模式失败：' + (data?.error || '未知错误'), 'error');
      await restorePermissionModeSelect();
    }
  } catch (e) {
    showToast('切换权限模式失败', 'error');
    await restorePermissionModeSelect();
  }
}

// 服务端镜像是唯一事实源（落盘持久化，重启不丢）。只在页面加载时读一次
// 回填浮层选中态——绝不反向自动 PUT：历史上新标签页曾把 sessionStorage 默认值
// 静默 PUT 回服务端，覆盖用户在别处设置的 full_access。
async function restorePermissionModeSelect() {
  const pop = document.getElementById('perm-pop');
  if (!pop) return;
  let mode = 'before_changes';
  try {
    const resp = await fetch('/api/config/permission-mode');
    const data = await resp.json();
    mode = data.permission_mode || 'before_changes';
  } catch (e) {
    mode = 'before_changes';
  }
  _popSetSel(pop, mode);
  _popApplyBtn('perm', pop.querySelector('.pop-item.sel'));
}

document.addEventListener('DOMContentLoaded', () => {
  const pop = document.getElementById('perm-pop');
  if (!pop) return;

  restorePermissionModeSelect();
  _bindPop('perm-btn', 'perm-pop');

  // 显式选择才 PUT（点当前选中项不重复提交）
  pop.addEventListener('click', e => {
    const item = e.target.closest('.pop-item');
    if (!item || item.classList.contains('sel')) return;
    setPermissionMode(item.dataset.v);
  });
});

// ---- 会话 waker 人格切换（chat 选择器）----

// 当前选中的 waker（空=默认助手）。模块级，供 chat.js 的 send() 读取。
let currentWaker = '';

async function loadWakerOptions() {
  const pop = document.getElementById('waker-pop');
  if (!pop) return;
  let wakers = [];
  try {
    const resp = await fetch('/api/waker/items');
    const data = await resp.json();
    wakers = data.wakers || [];
  } catch (e) {
    // 拉取失败保持默认项
  }
  // 安全构造：_popItem 内部全程 textContent（同旧 new Option 纪律）
  pop.innerHTML = '';
  pop.appendChild(_popItem('', '🤖', '默认助手', '不注入人格'));
  wakers.forEach(w => {
    pop.appendChild(_popItem(w.name || '', '🧑‍💼', w.name || '', w.description || ''));
  });
  // 选项就绪后回显当前选中（兼容 syncWakerSelect 先于本函数完成的时序）
  _popSetSel(pop, currentWaker || '');
  _popApplyBtn('waker', pop.querySelector('.pop-item.sel'));
}

async function setWaker(name) {
  try {
    const resp = await fetch('/api/config/waker', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      // P2-14 移交：带当前会话 id，waker 落到该会话的 worker 槽/桶。
      // currentSessionId 由 chat.js 声明（waker 浮层仅存在于 chat 页），
      // typeof 守卫兜底非 chat 页形态——与下方 send() 读 currentWaker 同款。
      body: JSON.stringify({ name, session_id: (typeof currentSessionId !== 'undefined' ? currentSessionId : '') || '' }),
    });
    const data = await resp.json().catch(() => ({}));
    // 草稿会话尚未落盘时后端不再 404（落到 main）；若遇旧进程 404，
    // 本地仍生效——下一条消息 body.waker 会带上。
    if ((resp.ok && data && data.ok !== false) || resp.status === 404) {
      currentWaker = name || '';
      sessionStorage.setItem('hermes_waker', currentWaker);
      syncWakerSelect(currentWaker);   // 按钮图标/简称同步选中态
      showToast(currentWaker ? `已切换 waker：${currentWaker}` : '已切回默认助手');
    } else {
      showToast('切换 waker 失败：' + (data.detail || data.error || ('HTTP ' + resp.status)), 'error');
      syncWakerSelect(currentWaker);   // 回滚到当前值
    }
  } catch (e) {
    showToast('切换 waker 失败', 'error');
  }
}

// 同步 waker 选择器到指定值（会话切换时回显用，不触发 toast）
function syncWakerSelect(name) {
  currentWaker = name || '';
  const pop = document.getElementById('waker-pop');
  _popSetSel(pop, currentWaker);
  if (pop) _popApplyBtn('waker', pop.querySelector('.pop-item.sel'));
  sessionStorage.setItem('hermes_waker', currentWaker);
}

document.addEventListener('DOMContentLoaded', () => {
  const pop = document.getElementById('waker-pop');
  if (!pop) return;

  // 拉 waker 列表填充浮层选项
  loadWakerOptions();
  _bindPop('waker-btn', 'waker-pop');

  // 从 sessionStorage 恢复（首次加载）
  currentWaker = sessionStorage.getItem('hermes_waker') || '';

  // 显式选择才 PUT
  pop.addEventListener('click', e => {
    const item = e.target.closest('.pop-item');
    if (!item || item.classList.contains('sel')) return;
    setWaker(item.dataset.v);
  });
});

// ---- 会话模型档案切换（chat 选择器）----
// 契约（后端并行实现，2026-09-05）：
//   GET  /api/models → [{id, display, model, base_url, context_window, has_key}]
//   PUT  /api/config/model {session_id?, profile_id 或 ""=默认}
// 契约无「当前模型」读端点，选择器每页加载从「默认」起步（切换即 PUT，后端为准）。

let _modelProfiles = [];      // 档案缓存：chat 选择器与设置页共用
let currentModelProfile = ''; // 当前选中档案 id（''=默认 config.yaml）

function modelProfileLabel(p) {
  return (p && (p.display || p.id || p.model)) || '';
}

async function loadModelOptions() {
  const pop = document.getElementById('model-pop');
  if (!pop) return;
  let profiles = [];
  try {
    const resp = await fetch('/api/models');
    if (resp.ok) {
      const data = await resp.json();
      if (Array.isArray(data)) profiles = data;
    }
  } catch (e) {}
  _modelProfiles = profiles;
  // 安全构造：display 是用户自由输入，_popItem 内部全程 textContent（同旧纪律）
  pop.innerHTML = '';
  pop.appendChild(_popItem('', '⚙️', '默认（config.yaml）', '跟随全局配置'));
  profiles.forEach(p => {
    const label = modelProfileLabel(p) || (p.id || '');
    pop.appendChild(_popItem(p.id || '', '🌐', label,
                             p.has_key ? '模型档案' : '未配置 Key'));
  });
  _popSetSel(pop, currentModelProfile || '');
  _popApplyBtn('model', pop.querySelector('.pop-item.sel'));
}

async function setModelProfile(profileId) {
  const pop = document.getElementById('model-pop');
  const prev = currentModelProfile;
  try {
    const resp = await fetch('/api/config/model', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      // 对齐 setWaker：带当前会话 id，后端把模型选择落到该会话
      body: JSON.stringify({
        session_id: (typeof currentSessionId !== 'undefined' ? currentSessionId : '') || '',
        profile_id: profileId || '',
      }),
    });
    const data = await resp.json().catch(() => ({}));
    if (resp.ok && (!data || data.ok !== false)) {
      currentModelProfile = profileId || '';
      _popSetSel(pop, currentModelProfile);
      _popApplyBtn('model', pop ? pop.querySelector('.pop-item.sel') : null);
      const p = _modelProfiles.find(x => (x.id || '') === currentModelProfile);
      const label = profileId ? (modelProfileLabel(p) || profileId) : '默认（config.yaml）';
      showToast(`模型已切换：${label}（下一轮生效）`);
    } else {
      _popSetSel(pop, prev);
      _popApplyBtn('model', pop ? pop.querySelector(`.pop-item[data-v="${prev}"]`) : null);
      const detail = (data && (data.detail || data.error || data.message)) || ('HTTP ' + resp.status);
      showToast('切换模型失败：' + detail, 'error');
    }
  } catch (e) {
    _popSetSel(pop, prev);
    _popApplyBtn('model', pop ? pop.querySelector(`.pop-item[data-v="${prev}"]`) : null);
    showToast('切换模型失败：网络异常', 'error');
  }
}

document.addEventListener('DOMContentLoaded', () => {
  const pop = document.getElementById('model-pop');
  if (!pop) return;

  loadModelOptions();
  _bindPop('model-btn', 'model-pop');

  pop.addEventListener('click', e => {
    const item = e.target.closest('.pop-item');
    if (!item || item.classList.contains('sel')) return;
    setModelProfile(item.dataset.v);
  });
});

// ---- 项目空间切换（ADR-0005）----

function escAttr(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

async function initProjectSwitch() {
  const sel = document.getElementById('project-select');
  if (!sel) return;
  let data;
  try {
    const r = await fetch('/api/projects');
    if (!r.ok) { sel.innerHTML = '<option value="">（项目服务不可用）</option>'; return; }
    data = await r.json();
  } catch (e) {
    sel.innerHTML = '<option value="">（网络异常）</option>';
    return;
  }
  const others = (data.projects || []).filter(p => p.slug !== 'inbox')
    .map(p => `<option value="${escAttr(p.slug)}">${escAttr(p.name || p.slug)}${p.path_exists === false ? ' ⚠️目录缺失' : ''}</option>`)
    .join('');
  sel.innerHTML = `<option value="inbox">💬 收件箱（直接开聊）</option>${others}`;
  const active = data.active || 'inbox';
  sel.value = active;
  sel.addEventListener('change', async () => {
    if (!sel.value || sel.value === active) return;
    try {
      const resp = await fetch(`/api/projects/${encodeURIComponent(sel.value)}/activate`,
                               { method: 'POST' });
      if (resp.ok) location.href = '/chat';
      else {
        const e = await resp.json().catch(() => ({}));
        showToast('切换失败：' + (e.detail || ('HTTP ' + resp.status)), 'error');
        sel.value = active;
      }
    } catch (e) {
      showToast('切换失败：网络异常', 'error');
      sel.value = active;
    }
  });
}

initProjectSwitch();

// ---- 设置页：模型档案管理 ----
// 契约（后端并行实现，2026-09-05）：
//   GET    /api/models → [{id, display, model, base_url, context_window, has_key}]
//   POST   /api/models {display, model, base_url, api_key, context_window?, id?}
//   PUT    /api/models/{id}（api_key 缺省=不变）
//   DELETE /api/models/{id}
//   POST   /api/models/{id}/test → {ok, status, detail, models}
// 列表行/弹窗全部 createElement + textContent 构造，动态数据不进 HTML；
// 行内按钮经 #mp-list 单点事件委托（data-mp-*），无 onclick 拼接。

let _mpEditingId = '';  // 弹窗当前编辑的档案 id（''=新增）

async function loadModelProfiles() {
  const list = document.getElementById('mp-list');
  if (!list) return;
  let profiles = [];
  try {
    const r = await fetch('/api/models');
    if (r.ok) {
      const data = await r.json();
      if (Array.isArray(data)) profiles = data;
    }
  } catch (e) {}
  _modelProfiles = profiles;
  list.innerHTML = '';
  if (!profiles.length) {
    const empty = document.createElement('p');
    empty.className = 'hint';
    empty.textContent = '暂无模型档案，点击「＋ 新增档案」创建。对话页默认模型仍取 config.yaml。';
    list.appendChild(empty);
    return;
  }
  for (const p of profiles) list.appendChild(buildProfileRow(p));
}

function buildProfileRow(p) {
  const id = String(p.id || '');
  const row = document.createElement('div');
  row.className = 'mp-item';

  const info = document.createElement('div');
  info.className = 'mp-info';
  const nameLine = document.createElement('div');
  nameLine.className = 'mp-name';
  // has_key 掩码点：绿=已配 Key，灰=未配（悬停看文字说明）
  const keyDot = document.createElement('span');
  keyDot.className = 'mp-key ' + (p.has_key ? 'ok' : 'missing');
  keyDot.title = p.has_key ? '已配置 API Key' : '未配置 API Key';
  const nameText = document.createElement('span');
  nameText.textContent = p.display || id || '（未命名）';
  nameLine.append(keyDot, nameText);
  if (p.context_window) {
    const ctx = document.createElement('span');
    ctx.className = 'mp-ctx';
    ctx.textContent = Number(p.context_window).toLocaleString() + ' ctx';
    nameLine.appendChild(ctx);
  }
  const sub = document.createElement('div');
  sub.className = 'mp-sub';
  sub.textContent = (p.model || '—') + ' · ' + (p.base_url || '—');
  info.append(nameLine, sub);

  const actions = document.createElement('div');
  actions.className = 'mp-actions';
  const mkBtn = (label, attr, extra) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'btn-small' + (extra ? ' ' + extra : '');
    b.textContent = label;
    b.dataset[attr] = id;
    return b;
  };
  actions.append(
    mkBtn('测试连接', 'mpTest'),
    mkBtn('编辑', 'mpEdit'),
    mkBtn('删除', 'mpDel', 'danger'),
  );

  row.append(info, actions);
  return row;
}

document.addEventListener('DOMContentLoaded', () => {
  const list = document.getElementById('mp-list');
  if (!list) return;

  // 行内按钮事件委托（一次性注册；#mp-list 常驻 DOM，行内容每次重画）
  list.addEventListener('click', async (e) => {
    const tBtn = e.target.closest('[data-mp-test]');
    if (tBtn) { await testModelProfile(tBtn.dataset.mpTest, tBtn); return; }
    const eBtn = e.target.closest('[data-mp-edit]');
    if (eBtn) {
      const p = _modelProfiles.find(x => String(x.id || '') === eBtn.dataset.mpEdit);
      openModelProfileDialog(p || null);
      return;
    }
    const dBtn = e.target.closest('[data-mp-del]');
    if (dBtn) { await deleteModelProfile(dBtn.dataset.mpDel); }
  });

  const addBtn = document.getElementById('mp-add');
  if (addBtn) addBtn.addEventListener('click', () => openModelProfileDialog(null));

  const dlg = document.getElementById('mp-dialog');
  const form = document.getElementById('mp-form');
  if (form) form.addEventListener('submit', (e) => { e.preventDefault(); saveModelProfile(); });
  const cancelBtn = document.getElementById('mp-cancel');
  if (cancelBtn && dlg) cancelBtn.addEventListener('click', () => dlg.close());

  loadModelProfiles();
});

// 测试连接：按钮进入「测试中…」disabled 态，结果经 toast 展示后端 detail。
// confirm_send_key：携带已存 API Key 发起真实鉴权测试（默认只测连通性不带凭据，
// 防止存量 key 被发往任意改过的 base_url）
async function testModelProfile(id, btn) {
  if (!id || !btn) return;
  // 二选一（非危险操作）：true=携带已存 Key 做真实鉴权测试，false=仅测连通性
  const useKey = await uiConfirm({
    title: '测试模型档案',
    body: '是否携带已保存的 API Key 测试？',
    okText: '携带 Key 测试',
    cancelText: '仅测连通性',
    note: '携带 Key 会把已保存的凭据发往该档案当前的 Base URL',
  });
  const origLabel = btn.textContent;
  btn.disabled = true;
  btn.textContent = '测试中…';
  try {
    const r = await fetch(`/api/models/${encodeURIComponent(id)}/test?confirm_send_key=${useKey ? 'true' : 'false'}`, { method: 'POST' });
    const j = await r.json().catch(() => ({}));
    const modelCount = Array.isArray(j.models) ? j.models.length : null;
    if (j.ok) {
      const extra = modelCount === null ? '' : `，可见模型 ${modelCount} 个`;
      showToast(`✅ ${j.detail || '连接成功'}（HTTP ${j.status ?? r.status}${extra}）`);
    } else {
      showToast(`❌ 连接失败：${j.detail || ('HTTP ' + r.status)}`, 'error');
    }
  } catch (e) {
    showToast('连接测试失败：网络异常', 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = origLabel;
  }
}

async function deleteModelProfile(id) {
  if (!id) return;
  const p = _modelProfiles.find(x => String(x.id || '') === id);
  const label = p ? (p.display || id) : id;
  const ok = await uiConfirm({
    title: '删除模型档案',
    body: `确定删除模型档案「${label}」？该档案的对话将回到默认模型。`,
    danger: true,
  });
  if (!ok) return;
  try {
    const r = await fetch(`/api/models/${encodeURIComponent(id)}`, { method: 'DELETE' });
    if (r.ok) {
      showToast(`已删除档案「${label}」`);
      loadModelProfiles();
    } else {
      const j = await r.json().catch(() => ({}));
      showToast('删除失败：' + (j.detail || ('HTTP ' + r.status)), 'error');
    }
  } catch (e) {
    showToast('删除失败：网络异常', 'error');
  }
}

function openModelProfileDialog(profile) {
  const dlg = document.getElementById('mp-dialog');
  if (!dlg) return;
  _mpEditingId = profile ? String(profile.id || '') : '';
  document.getElementById('mp-dialog-title').textContent =
    _mpEditingId ? '编辑模型档案' : '新增模型档案';
  document.getElementById('mp-display').value = profile ? (profile.display || '') : '';
  document.getElementById('mp-model').value = profile ? (profile.model || '') : '';
  document.getElementById('mp-base-url').value = profile ? (profile.base_url || '') : '';
  const keyInput = document.getElementById('mp-api-key');
  keyInput.value = '';
  keyInput.placeholder = _mpEditingId ? '留空 = 不修改' : 'sk-…';
  document.getElementById('mp-context').value =
    profile && profile.context_window ? String(profile.context_window) : '';
  dlg.showModal();
}

async function saveModelProfile() {
  const val = (id) => (document.getElementById(id).value || '').trim();
  const display = val('mp-display');
  const model = val('mp-model');
  const baseUrl = val('mp-base-url');
  const apiKey = val('mp-api-key');
  const ctxRaw = val('mp-context');
  if (!display || !model || !baseUrl) {
    showToast('显示名 / 模型 ID / Base URL 均必填');
    return;
  }
  const body = { display, model, base_url: baseUrl };
  // 契约：api_key 缺省 = 不变。编辑留空就不带该字段；新增空 Key 同样不带
  if (apiKey || !_mpEditingId) body.api_key = apiKey;
  if (ctxRaw) {
    const n = Number(ctxRaw);
    if (!Number.isFinite(n) || n <= 0) {
      showToast('上下文窗口需为正整数（可留空）');
      return;
    }
    body.context_window = Math.round(n);
  }
  const url = _mpEditingId ? `/api/models/${encodeURIComponent(_mpEditingId)}` : '/api/models';
  const method = _mpEditingId ? 'PUT' : 'POST';
  try {
    const r = await fetch(url, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (r.ok) {
      showToast(_mpEditingId ? '档案已更新' : '档案已创建');
      const dlg = document.getElementById('mp-dialog');
      if (dlg) dlg.close();
      loadModelProfiles();
    } else {
      const j = await r.json().catch(() => ({}));
      showToast('保存失败：' + (j.detail || ('HTTP ' + r.status)), 'error');
    }
  } catch (e) {
    showToast('保存失败：网络异常', 'error');
  }
}
