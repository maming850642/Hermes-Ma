// 会话侧栏（会话列表/收起展开/重命名/切换/复制分支/按需总结/「⋯」菜单/新建草稿入口）——自 chat.js 顺延外置拆出，逐字搬移（split02，源区间 chat.js L2099-2379）
// ---------- 侧边栏会话列表（2026-06-26 方案 A） ----------
const sessionList = document.getElementById('session-list');
const newSessionBtn = document.getElementById('new-session-btn');

// ---------- 侧边栏收起/展开 ----------
const appShell = document.getElementById('app-shell');
const collapseBtn = document.getElementById('sidebar-collapse');
const expandBtn = document.getElementById('sidebar-expand');
if (collapseBtn) collapseBtn.addEventListener('click', () => appShell.classList.add('collapsed'));
if (expandBtn) expandBtn.addEventListener('click', () => appShell.classList.remove('collapsed'));

async function loadSidebarSessions() {
  if (!sessionList) return;
  hideSessionMoreMenu();  // 侧栏重渲染后旧菜单锚点已失效，先收起
  try {
    // 只列当前激活项目的会话（历史会话跟着项目走，ADR-0005）
    const r = await fetch('/api/sessions?project=' + encodeURIComponent(ACTIVE_PROJECT));
    if (!r.ok) return;
    const { sessions = [] } = await r.json();
    if (!sessions.length) {
      sessionList.innerHTML = '<div class="s-empty">暂无历史会话，点击 ＋ 新建</div>';
      return;
    }
    sessionList.innerHTML = sessions.map(s => {
      const active = s.session_id === currentSessionId ? ' active' : '';
      // 2026-09-05: 名称/sid 一律 escapeHtml 全字符集转义后再进模板——
      // 原实现只转 <，`"`/`'` 可在 title/data-* 属性上下文逃逸注入
      const name = escapeHtml(s.name || '未命名会话');
      const sid = escapeHtml(s.session_id || '');
      const time = (s.updated_at || '').slice(5, 16).replace('T', ' ');
      const cnt = s.message_count || 0;
      return `
        <div class="session-item${active}" data-id="${sid}">
          <div class="s-name" data-id="${sid}" title="双击重命名">${name}</div>
          <div class="s-meta">
            <span>${time} · ${cnt}条</span>
            <span class="s-actions">
              <button class="s-act" title="重命名" data-rename="${sid}">✏️</button>
              <button class="s-act" title="更多操作（复制分支 / 事件流）" data-more="${sid}">⋯</button>
              <button class="s-act" title="删除会话" data-del="${sid}">🗑</button>
            </span>
          </div>
        </div>`;
    }).join('');
  } catch (e) {}
}

// 重命名会话(内联编辑:名字变输入框,回车保存,Esc 取消)
function renameSessionInline(id) {
  const nameEl = sessionList.querySelector(`.s-name[data-id="${id}"]`);
  if (!nameEl) return;
  const oldName = nameEl.textContent;
  const input = document.createElement('input');
  input.type = 'text'; input.value = oldName;
  input.style.cssText = 'width:100%;font-size:15px;padding:2px 4px;border:1px solid var(--accent);border-radius:6px;background:#fff';
  nameEl.replaceWith(input);
  input.focus(); input.select();
  let saved = false;
  const save = async () => {
    if (saved) return; saved = true;
    const newName = input.value.trim();
    if (newName && newName !== oldName) {
      try {
        const r = await fetch(`/api/sessions/${id}/rename`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: newName }),
        });
        if (!r.ok) showToast('重命名失败（HTTP ' + r.status + '）', 'error');
      } catch (e) { showToast('重命名失败：网络异常', 'error'); }
    }
    loadSidebarSessions();
  };
  input.addEventListener('blur', save);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
    else if (e.key === 'Escape') { saved = true; loadSidebarSessions(); }
  });
}

// 会话项交互（事件委托）
if (sessionList) {
  sessionList.addEventListener('click', async (e) => {
    const delBtn = e.target.closest('[data-del]');
    if (delBtn) {
      e.stopPropagation();
      const id = delBtn.dataset.del;
      const ok = await uiConfirm({
        title: '删除会话',
        body: '确定删除这个会话？不可恢复！',
        danger: true,
      });
      if (!ok) return;
      await fetch(`/api/sessions/${id}`, { method: 'DELETE' });
      // 删除的是当前会话则本地换新草稿（不再调 /reset 重建，不 reload——
      // startDraftSession 已清屏 + 换内存 sid + 刷侧栏）
      if (id === currentSessionId) {
        startDraftSession();
      } else {
        loadSidebarSessions();
      }
      return;
    }
    const renameBtn = e.target.closest('[data-rename]');
    if (renameBtn) {
      e.stopPropagation();
      renameSessionInline(renameBtn.dataset.rename);
      return;
    }
    // 「⋯」更多菜单（2026-09-05 会话页下线，复制分支/事件流迁入侧栏）
    const moreBtn = e.target.closest('[data-more]');
    if (moreBtn) {
      e.stopPropagation();
      const menu = ensureSessionMoreMenu();
      if (!menu.hidden && menu.dataset.sid === moreBtn.dataset.more) {
        hideSessionMoreMenu();   // 再点同一 ⋯ 收起
      } else {
        showSessionMoreMenu(moreBtn, moreBtn.dataset.more);
      }
      return;
    }
    const item = e.target.closest('.session-item');
    if (!item) return;
    const id = item.dataset.id;
    if (id === currentSessionId) return;  // 已是当前会话
    // 切会话：只断本页 SSE（生成继续后台跑），不调 /load 抢 worker 锁——
    // 旧实现 503「AI 正在思考请稍候再切换」会把人卡在当前页。
    abortCurrentStream();
    rememberCurrentSession(id);
    location.reload();
  });

  // 双击会话名重命名
  sessionList.addEventListener('dblclick', (e) => {
    const name = e.target.closest('.s-name');
    if (name) renameSessionInline(name.dataset.id);
  });
}

// ---------- 会话条目「⋯」菜单（2026-09-05 会话页下线能力迁入） ----------
// 产品落点：✏️ 重命名与 🗑 删除保留行内直触（高频操作、删除已有 confirm），
// 「⋯」收纳低频操作：📋 复制分支 / 📜 事件流 / 📝 生成总结。
// 会话总结按需触发（2026-09-08）：不再随会话结束自动跑，只有这里的
// 显式入口才会总结（POST /api/sessions/{sid}/summary）。
// 菜单常驻 body（fixed 定位），当前会话 sid 挂在菜单 data-sid 上，菜单项
// 事件委托消费——无 onclick 拼接、无插值进 HTML。
let _moreMenu = null;

function ensureSessionMoreMenu() {
  if (_moreMenu) return _moreMenu;
  _moreMenu = document.createElement('div');
  _moreMenu.className = 's-more-menu';
  _moreMenu.hidden = true;
  const ITEMS = [
    { action: 'fork', label: '📋 复制分支' },
    { action: 'events', label: '📜 事件流' },
    { action: 'summarize', label: '📝 生成总结' },
  ];
  for (const it of ITEMS) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 's-more-item';
    b.textContent = it.label;
    b.dataset.action = it.action;
    _moreMenu.appendChild(b);
  }
  _moreMenu.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-action]');
    if (!btn) return;
    const sid = _moreMenu.dataset.sid || '';
    hideSessionMoreMenu();
    if (btn.dataset.action === 'fork') forkSessionBranch(sid);
    else if (btn.dataset.action === 'events') showSessionEvents(sid);
    else if (btn.dataset.action === 'summarize') summarizeSessionOnDemand(sid);
  });
  document.body.appendChild(_moreMenu);
  // 点击菜单/锚点以外任意处收起；Escape 收起（各注册一次）
  document.addEventListener('click', (e) => {
    if (_moreMenu.hidden) return;
    if (e.target.closest('.s-more-menu') || e.target.closest('[data-more]')) return;
    hideSessionMoreMenu();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !_moreMenu.hidden) hideSessionMoreMenu();
  });
  // 打开期间视口变化（任意容器滚动 / 窗口缩放）：fixed 定位立即失锚，
  // 菜单悬在错误位置——直接关闭。scroll 不冒泡，capture 在 window 上才能
  // 接住所有滚动容器（会话列表 / 消息区 / 页面自身）。
  window.addEventListener('scroll', () => {
    if (!_moreMenu.hidden) hideSessionMoreMenu();
  }, true);
  window.addEventListener('resize', () => {
    if (!_moreMenu.hidden) hideSessionMoreMenu();
  });
  return _moreMenu;
}

function showSessionMoreMenu(anchorBtn, sid) {
  const menu = ensureSessionMoreMenu();
  menu.dataset.sid = sid || '';
  menu.hidden = false;
  // 定位：按钮正下方、右缘对齐按钮右缘；贴近视口底部时翻转到按钮上方
  const r = anchorBtn.getBoundingClientRect();
  const mw = menu.offsetWidth, mh = menu.offsetHeight;
  let left = Math.max(8, r.right - mw);
  let top = r.bottom + 4;
  if (top + mh > window.innerHeight - 8) top = Math.max(8, r.top - mh - 4);
  menu.style.left = left + 'px';
  menu.style.top = top + 'px';
}

function hideSessionMoreMenu() {
  if (_moreMenu && !_moreMenu.hidden) _moreMenu.hidden = true;
}

// 切换到指定会话：断本页 SSE（生成继续后台）→ 记下 sid → 重载。
// 不调 /load：那条路径会抢 worker 锁，生成中 503 把人卡死。
async function switchToSession(id) {
  if (!id) return;
  abortCurrentStream();
  rememberCurrentSession(id);
  location.reload();
}

// 复制分支：POST /api/sessions/{sid}/fork（缺省全量复制）→ 成功后刷新侧栏
// 并切换到新分支会话（后端返回 {session_id, event_count, up_to_event_id}）
async function forkSessionBranch(id) {
  if (!id) return;
  let r;
  try {
    r = await fetch(`/api/sessions/${encodeURIComponent(id)}/fork`, { method: 'POST' });
  } catch (e) {
    showToast('创建分支失败：网络异常', 'error');
    return;
  }
  if (!r.ok) {
    let detail = '';
    try { detail = (await r.json()).detail || ''; } catch (e) {}
    showToast('创建分支失败：' + (detail || 'HTTP ' + r.status), 'error');
    return;
  }
  const j = await r.json().catch(() => ({}));
  const newSid = j.session_id || '';
  if (!newSid) {
    showToast('创建分支失败：响应缺少新会话 ID', 'error');
    return;
  }
  showToast(`已创建分支（复制 ${j.event_count ?? 0} 条事件），正在切换…`);
  await switchToSession(newSid);
}

// 按需生成会话总结（2026-09-08）：总结只在此显式触发，不再随会话结束自动跑。
// LLM 总结耗时 10-30s，先给"正在总结"提示；结果入记忆库（记忆页可按
// 「会话总结」来源筛选查看），这里只回报事实条数。
async function summarizeSessionOnDemand(id) {
  if (!id) return;
  showToast('正在总结会话（LLM 生成，约需 10-30 秒）…');
  let r;
  try {
    r = await fetch(`/api/sessions/${encodeURIComponent(id)}/summary`, { method: 'POST' });
  } catch (e) {
    showToast('总结失败：网络异常', 'error');
    return;
  }
  if (!r.ok) {
    let detail = '';
    try { detail = (await r.json()).detail || ''; } catch (e) {}
    showToast('总结失败：' + (detail || 'HTTP ' + r.status), 'error');
    return;
  }
  const j = await r.json().catch(() => ({}));
  if (j.summary_stored) {
    showToast(`总结已存入记忆（提取事实 ${j.facts_count ?? 0} 条），可在记忆页查看`);
  } else {
    showToast('本会话没有可沉淀的内容，未生成总结');
  }
}

// 新建会话（2026-09 草稿化）：本地换纸，零后端调用——首条消息发出时
// 才由后端 _prelog_turn 建 stub 真正建会话（详见 startDraftSession）
if (newSessionBtn) {
  newSessionBtn.addEventListener('click', () => {
    if (isStreaming) return;  // 流式中禁止新建
    startDraftSession();
    showToast('已开新草稿：发出首条消息时才会创建会话');
  });
}
