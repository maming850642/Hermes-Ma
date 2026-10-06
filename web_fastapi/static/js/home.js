// 欢迎页逻辑（ADR-0005 草图1）：项目卡片流 + 直接开聊/新建/打开目录/克隆。
// 依赖 app.js 的 showToast()；表单走原生 <dialog>。

const grid = document.getElementById('project-grid');
const cloneStatus = document.getElementById('clone-status');
const dlg = document.getElementById('dlg-form');

let cards = [];   // 最近一次 /api/projects 的 projects 列表

async function loadProjects() {
  try {
    const r = await fetch('/api/projects');
    if (!r.ok) { grid.innerHTML = `<div class="grid-placeholder">项目服务不可用（HTTP ${r.status}）</div>`; return; }
    const d = await r.json();
    renderCards(d.projects || []);
  } catch (e) {
    grid.innerHTML = '<div class="grid-placeholder">网络异常，无法加载项目</div>';
  }
}

function renderCards(projects) {
  cards = projects;
  if (!projects.length) {
    grid.innerHTML = '<div class="grid-placeholder">还没有项目空间——新建一个、打开本地目录或克隆仓库。</div>';
    return;
  }
  grid.innerHTML = projects.map(p => {
    const icon = p.type === 'inbox' ? '💬' : '📁';
    const subBits = [];
    if (p.type !== 'inbox') {
      subBits.push(`<span class="pcard-kind">${p.type === 'mounted' ? '本地目录' : '托管空间'}</span>`);
      if (p.path_exists === false) subBits.push('<span class="warn">⚠️ 目录缺失</span>');
      else if (p.path) subBits.push(`<span class="path" title="${p.path.replace(/"/g, '&quot;')}">${p.path}</span>`);
    } else {
      subBits.push('<span class="pcard-kind">免项目的随手对话历史</span>');
    }
    const when = p.last_opened_at ? new Date(p.last_opened_at * 1000).toLocaleString('zh-CN', { hour12: false }) : '';
    const del = p.slug === 'inbox'
      ? ''
      : `<button class="pcard-del" data-slug="${p.slug}" title="删除项目记录">✕</button>`;
    return `
      <div class="pcard ${p.path_exists === false ? 'pcard-broken' : ''}" data-slug="${p.slug}">
        <div class="pcard-head"><span class="pcard-icon">${icon}</span><span class="pcard-name">${escHtml(p.name || p.slug)}</span>${del}</div>
        <div class="pcard-sub">${subBits.join(' · ')}</div>
        <div class="pcard-time">${when}</div>
      </div>`;
  }).join('');
}

function escHtml(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

async function activate(slug, dest = '/chat') {
  const r = await fetch(`/api/projects/${encodeURIComponent(slug)}/activate`, { method: 'POST' });
  if (r.ok) location.href = dest;
  else {
    const e = await r.json().catch(() => ({}));
    showToast(`进入项目失败：${e.detail || ('HTTP ' + r.status)}`, 'error');
  }
}

grid.addEventListener('click', async (ev) => {
  const delBtn = ev.target.closest('.pcard-del');
  if (delBtn) {
    ev.stopPropagation();
    const slug = delBtn.dataset.slug;
    // 项目名/路径从 grid 渲染缓存（loadProjects 存下的 cards）取，清单逐行列出
    const p = cards.find(c => c.slug === slug) || {};
    const items = [p.name || slug];
    if (p.path) items.push(p.path);
    const ok = await uiConfirm({
      title: '删除项目记录',
      body: `确定删除项目「${p.name || slug}」的记录？`,
      danger: true,
      items,
      note: '不会删除磁盘上的任何文件',
    });
    if (!ok) return;
    const r = await fetch(`/api/projects/${encodeURIComponent(slug)}`, { method: 'DELETE' });
    if (r.ok) { showToast('已删除'); loadProjects(); }
    else { const e = await r.json().catch(() => ({})); showToast('删除失败：' + (e.detail || r.status), 'error'); }
    return;
  }
  const card = ev.target.closest('.pcard');
  if (card && !card.classList.contains('pcard-broken')) activate(card.dataset.slug);
});

document.getElementById('btn-direct-chat').addEventListener('click', () => activate('inbox'));

/* ---- 失败任务徽标（ADR-0003 死信可视化） ---- */
async function loadCloneStatus() {
  try {
    const r = await fetch('/api/runs');
    if (!r.ok) return;
    const d = await r.json();
    const rows = (d.runs || []).filter(x => x.kind === 'git_clone')
      .sort((a, b) => (b.finished_at || b.started_at || 0) - (a.finished_at || a.started_at || 0))
      .slice(0, 5);
    if (!rows.length) { cloneStatus.innerHTML = ''; return; }
    cloneStatus.innerHTML = rows.map(x => {
      const ok = x.status === 'done';
      const cls = ok ? 'ok' : (x.status === 'failed' ? 'bad' : 'pending');
      const label = { done: '✅ 克隆完成', failed: '❌ 克隆失败', running: '⏳ 克隆中', queued: '… 排队中', interrupted: '⚠️ 已中断' }[x.status] || x.status;
      const detail = x.error ? ` title="${escHtml(x.label)}：${escHtml(x.error)}"` : ` title="${escHtml(x.label)}"`;
      const rid = escHtml(x.run_id || '');
      const canDrop = x.status === 'failed' || x.status === 'done' || x.status === 'interrupted';
      const xbtn = canDrop ? `<button type="button" class="runchip-x" data-run="${rid}" title="关掉这条">×</button>` : '';
      return `<span class="runchip ${cls}" data-run="${rid}"${detail}>${label} · ${escHtml((x.label || '').split('/').pop())}${xbtn}</span>`;
    }).join('');
  } catch (e) { /* 忽略 */ }
}
cloneStatus.addEventListener('click', async (e) => {
  const xbtn = e.target.closest('.runchip-x');
  if (xbtn) {
    e.stopPropagation();
    const id = xbtn.dataset.run;
    if (!id) return;
    const r = await fetch('/api/runs/' + encodeURIComponent(id), { method: 'DELETE' });
    if (r.ok) loadCloneStatus();
    else {
      const d = await r.json().catch(() => ({}));
      showToast('关掉失败：' + (d.detail || r.status), 'error');
    }
    return;
  }
  const bad = e.target.closest('.runchip.bad');
  if (bad && bad.title) {
    // alert 替代：single 模式只有「知道了」，点遮罩不关闭（长文本错误详情不误丢）
    uiConfirm({ title: '克隆失败详情', body: bad.title, single: true, okText: '知道了' });
  }
});

/* ---- 三种新建流程共用弹窗 ---- */
let dlgMode = null;   // 'new-space' | 'open-local' | 'clone'

function openDialog(mode) {
  dlgMode = mode;
  document.getElementById('dlg-error').textContent = '';
  ['f-name', 'f-main', 'f-slug'].forEach(id => document.getElementById(id).value = '');
  const rowName = document.getElementById('row-name');
  const rowSlug = document.getElementById('row-slug');
  // 「浏览…」只在打开本地目录时出现：调后端弹系统原生目录选择器
  document.getElementById('btn-browse').style.display =
    mode === 'open-local' ? '' : 'none';
  if (mode === 'new-space') {
    document.getElementById('dlg-title').textContent = '新建项目空间';
    rowName.style.display = ''; rowSlug.style.display = '';
    const main = document.getElementById('row-main'); main.style.display = 'none';
  } else if (mode === 'open-local') {
    document.getElementById('dlg-title').textContent = '打开本地目录';
    const main = document.getElementById('row-main');
    main.style.display = ''; main.querySelector('input').placeholder = 'D:\\projects\\my-work（绝对路径）';
    rowName.style.display = ''; rowSlug.style.display = 'none';
  } else {
    document.getElementById('dlg-title').textContent = '克隆 Git 仓库';
    const main = document.getElementById('row-main');
    main.style.display = ''; main.querySelector('input').placeholder = 'https://github.com/user/repo.git';
    rowName.style.display = ''; rowSlug.style.display = '';
  }
  dlg.showModal();
}

document.getElementById('btn-new-space').addEventListener('click', () => openDialog('new-space'));
document.getElementById('btn-open-local').addEventListener('click', () => openDialog('open-local'));
document.getElementById('btn-clone').addEventListener('click', () => openDialog('clone'));

// 浏览…：让服务端弹原生目录选择器（本机同屏），选中后自动回填
document.getElementById('btn-browse').addEventListener('click', async () => {
  const inp = document.getElementById('f-main');
  const errEl = document.getElementById('dlg-error');
  errEl.textContent = '';
  inp.placeholder = '正在打开系统目录选择器…';
  try {
    const r = await fetch('/api/projects/pick-dir', { method: 'POST' });
    const d = await r.json().catch(() => ({}));
    if (r.ok && d.path) {
      inp.value = d.path;
    } else if (!r.ok) {
      errEl.textContent = d.detail || '系统目录选择器不可用，请手动输入路径';
    } // r.ok 且 path 为 null = 用户取消，静默
  } catch {
    errEl.textContent = '目录选择器请求失败，请手动输入路径';
  } finally {
    inp.placeholder = 'D:\\projects\\my-work（绝对路径）';
  }
});

document.getElementById('dlg-ok').addEventListener('click', async () => {
  const errEl = document.getElementById('dlg-error');
  errEl.textContent = '';
  const name = document.getElementById('f-name').value.trim();
  const mainVal = document.getElementById('f-main').value.trim();
  const slug = document.getElementById('f-slug').value.trim();
  try {
    let r;
    if (dlgMode === 'new-space') {
      r = await fetch('/api/projects', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name || '未命名空间', type: 'hosted', slug: slug || null }),
      });
    } else if (dlgMode === 'open-local') {
      if (!mainVal) { errEl.textContent = '请填写目录绝对路径'; return; }
      // 先创建 mounted 项目记录，再激活（激活时做完整挂载校验）
      r = await fetch('/api/projects', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name || mainVal.split(/[\\/]/).pop(), type: 'mounted', path: mainVal }),
      });
      if (r.ok) {
        const created = (await r.json()).project;
        dlg.close();
        await activate(created.slug);
        return;
      }
    } else if (dlgMode === 'clone') {
      if (!mainVal.startsWith('https://')) { errEl.textContent = '仅支持 https:// 的仓库地址'; return; }
      r = await fetch('/api/projects/clone', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: mainVal, name: name || null, slug: slug || null }),
      });
      if (r.ok) {
        dlg.close();
        showToast('克隆任务已开始，完成后卡片自动出现');
        setTimeout(() => { loadProjects(); loadCloneStatus(); }, 2500);
        return;
      }
    }
    if (r && !r.ok) {
      const e = await r.json().catch(() => ({}));
      errEl.textContent = e.detail || `失败（HTTP ${r.status}）`;
      return;
    }
    dlg.close();
    loadProjects();
  } catch (e) {
    errEl.textContent = '网络异常';
  }
});

loadProjects();
loadCloneStatus();
