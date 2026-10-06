// chat 页右侧只读文件树（ADR-0005 草图2）。
// 数据源：GET /api/workspace/tree?path=<相对路径>（懒加载单层）。
// 文件点击 → 只读预览弹窗；目录点击 → 原位导航（面包屑返回上级）。
// 409/异常 → 整列隐藏（收件箱、未挂载工作区没有文件树语义）。

(function () {
  const panel = document.getElementById('file-tree-panel');
  if (!panel) return;
  const bodyEl = document.getElementById('ftree-body');
  const crumbEl = document.getElementById('ftree-crumb');
  const emptyEl = document.getElementById('ftree-empty');

  let currentPath = '';        // 相对当前项目根的路径，''=根
  let hiddenForever = false;   // 409 后不再尝试

  function escHtml(s) {
    return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  async function load(path) {
    if (hiddenForever) return;
    let r;
    try {
      r = await fetch(`/api/workspace/tree?path=${encodeURIComponent(path)}`);
    } catch (e) { hidePanel(); return; }
    if (r.status === 409) {
      // 无激活根：整列隐藏（保持布局干净）
      panel.style.display = 'none';
      hiddenForever = true;
      return;
    }
    if (!r.ok) {
      bodyEl.innerHTML = `<div class="ftree-error">加载失败（HTTP ${r.status}）</div>`;
      return;
    }
    const j = await r.json();
    currentPath = path;
    renderCrumb();
    bodyEl.innerHTML = (j.entries || []).map(e => `
      <div class="ftree-node" data-name="${escHtml(e.name)}" data-type="${e.type}"
           ${e.collapsed ? 'data-collapsed="1"' : ''}>
        <span class="ftree-icon">${e.type === 'dir' ? (e.collapsed ? '📦' : '📁') : guessIcon(e.name)}</span>
        <span class="ftree-name">${escHtml(e.name)}</span>
      </div>`).join('') ||
      '<div class="ftree-empty-inner">（空目录）</div>';
  }

  function guessIcon(name) {
    const ext = name.split('.').pop().toLowerCase();
    if (['py', 'js', 'ts', 'java', 'go', 'rs', 'c', 'cpp', 'sh'].includes(ext)) return '📜';
    if (['md', 'txt', 'rst'].includes(ext)) return '📄';
    if (['json', 'yaml', 'yml', 'toml', 'ini', 'cfg'].includes(ext)) return '⚙️';
    if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'].includes(ext)) return '🖼️';
    return '📄';
  }

  function renderCrumb() {
    const parts = currentPath ? currentPath.split('/').filter(Boolean) : [];
    let html = '<span class="crumb-root">根目录</span>';
    html += parts.map((p, i) =>
      ` <span class="crumb-sep">›</span> <a href="#" data-idx="${i}" class="crumb-part">${escHtml(p)}</a>`).join('');
    crumbEl.innerHTML = html;
    crumbEl.querySelectorAll('.crumb-part').forEach(a => {
      a.addEventListener('click', (ev) => {
        ev.preventDefault();
        load(parts.slice(0, Number(a.dataset.idx) + 1).join('/'));
      });
    });
    if (parts.length) {
      const up = document.createElement('a');
      up.href = '#'; up.className = 'crumb-up'; up.textContent = '⬆ 上级';
      up.addEventListener('click', (ev) => {
        ev.preventDefault();
        load(parts.length <= 1 ? '' : parts.slice(0, -1).join('/'));
      });
      crumbEl.appendChild(up);
    }
  }

  bodyEl.addEventListener('click', (ev) => {
    const node = ev.target.closest('.ftree-node');
    if (!node) return;
    const name = node.dataset.name;
    const nextPath = currentPath ? `${currentPath}/${name}` : name;
    if (node.dataset.type === 'dir') {
      if (node.dataset.collapsed) { showToast(`${name} 为大目录/内部目录，不在树中展开`); return; }
      load(nextPath);
    } else {
      openPreview(nextPath, name);
    }
  });

  /* ---- 只读预览 ---- */
  // 尺寸记忆：用户拖拽右下角调整后，下次打开沿用（localStorage 持久化）
  const PV_SIZE_KEY = 'hermes_preview_size';
  const PV_DEFAULT = { w: 720, h: 600 };

  function _loadPreviewSize() {
    try {
      const s = JSON.parse(localStorage.getItem(PV_SIZE_KEY) || 'null');
      if (s && s.w >= 360 && s.h >= 300) return s;
    } catch (e) {}
    return { ...PV_DEFAULT };
  }

  function _applyPreviewSize(dlg) {
    const s = _loadPreviewSize();
    // 视口钳制：小屏/转屏后不越界
    const w = Math.min(s.w, Math.floor(window.innerWidth * 0.95));
    const h = Math.min(s.h, Math.floor(window.innerHeight * 0.92));
    dlg.style.width = Math.max(360, w) + 'px';
    dlg.style.height = Math.max(300, h) + 'px';
  }

  function _watchPreviewResize(dlg) {
    if (dlg._pvResizeObs) return;
    let timer = null;
    const obs = new ResizeObserver(() => {
      if (!dlg.open) return;
      clearTimeout(timer);
      timer = setTimeout(() => {
        try {
          localStorage.setItem(PV_SIZE_KEY, JSON.stringify(
            { w: dlg.offsetWidth, h: dlg.offsetHeight }));
        } catch (e) {}
      }, 300);
    });
    obs.observe(dlg);
    dlg._pvResizeObs = obs;
  }

  async function openPreview(relPath, name) {
    let r;
    try {
      r = await fetch(`/api/workspace/tree/preview?path=${encodeURIComponent(relPath)}`);
    } catch (e) { showToast('预览加载失败', 'error'); return; }
    if (r.status === 415) { showToast(`${name} 是二进制文件，不支持预览`); return; }
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      showToast(`预览失败：${e.detail || r.status}`, 'error');
      return;
    }
    const j = await r.json();
    // 按扩展名分流：md 走渲染管线，html 用沙箱 iframe 直开原型，其余纯文本
    const lower = (name || '').toLowerCase();
    const isMd = lower.endsWith('.md') || lower.endsWith('.markdown');
    const isHtml = lower.endsWith('.html') || lower.endsWith('.htm');
    let dlg = document.getElementById('preview-dialog');
    if (!dlg) {
      dlg = document.createElement('dialog');
      dlg.id = 'preview-dialog';
      dlg.className = 'dlg preview-dlg';
      dlg.innerHTML = `
        <div class="preview-head">
          <strong id="pv-title"></strong>
          <span id="pv-meta" class="preview-meta"></span>
          <button onclick="document.getElementById('preview-dialog').close()" class="head-btn">×</button>
        </div>
        <div id="pv-warn" class="pv-warn" style="display:none;"></div>
        <pre id="pv-body"></pre>
        <div id="pv-md" class="pv-md" style="display:none;"></div>
        <iframe id="pv-frame" class="preview-frame" style="display:none;" sandbox="allow-scripts"></iframe>`;
      document.body.appendChild(dlg);
    }
    document.getElementById('pv-title').textContent = name;
    document.getElementById('pv-meta').textContent =
      `${j.size} 字节${j.truncated ? ' · 已截断到上限' : ''}`;
    const content = j.content || '';
    const warn = document.getElementById('pv-warn');
    if (j.truncated) {
      warn.textContent = `⚠️ 文件共 ${j.size} 字节，超过预览上限，仅加载前 ${content.length} 字节——内容不完整`;
      warn.style.display = 'block';
    } else {
      warn.style.display = 'none';
    }
    const preEl = document.getElementById('pv-body');
    const mdEl = document.getElementById('pv-md');
    const frameEl = document.getElementById('pv-frame');
    preEl.style.display = mdEl.style.display = frameEl.style.display = 'none';
    if (isHtml) {
      // 原型 HTML 直开：opaque origin 沙箱，脚本可运行但拿不到应用
      // cookie/DOM。已知限制：相对路径的本地 css/js 不会加载、
      // localStorage 在沙箱内抛错——原型自包含才可完整交互。
      // 注意：必须重建 iframe 节点——复用旧节点改 srcdoc 在部分
      // Chromium 上不触发重载，第二个文件会白屏。
      const fresh = document.createElement('iframe');
      fresh.id = 'pv-frame';
      fresh.className = 'preview-frame';
      fresh.setAttribute('sandbox', 'allow-scripts');
      fresh.style.display = 'block';
      fresh.srcdoc = content;
      frameEl.replaceWith(fresh);
    } else if (isMd && typeof renderMarkdown === 'function') {
      // chat.js 先于本脚本加载，renderMarkdown（marked+sanitize）全局可用；
      // 守卫兜底：万一缺失回退纯文本
      mdEl.innerHTML = renderMarkdown(content);
      mdEl.style.display = 'block';
    } else {
      preEl.textContent = content;
      preEl.style.display = 'block';
    }
    _applyPreviewSize(dlg);   // 恢复上次手动调整的尺寸（含视口钳制）
    dlg.showModal();
    _watchPreviewResize(dlg);
  }

  function hidePanel() {
    panel.style.display = 'none';
  }

  document.getElementById('ftree-refresh').addEventListener('click', () => load(currentPath));
  // agent 写盘后自动刷新：chat 流收到写类工具 tool_end 时派发该事件
  //（chat.js 侧防抖），新建/修改的文件不再需要手动 ⟳ 才出现
  document.addEventListener('hermes:ftree-refresh', () => {
    if (!hiddenForever) load(currentPath);
  });

  // 收起后的恢复入口：右缘常驻竖排拉手（就在面板原来的位置），
  // 不再塞进左侧会话栏头部——那是全页最不该找开关的地方。
  function showReopenTab() {
    if (hiddenForever) return;   // 409 无工作区语义：没有"恢复"可言
    let tab = document.getElementById('ftree-reopen');
    if (!tab) {
      tab = document.createElement('button');
      tab.id = 'ftree-reopen';
      tab.title = '打开工作区文件树';
      tab.textContent = '📁 工作区';
      tab.addEventListener('click', () => {
        tab.classList.remove('visible');
        panel.style.display = '';
        load(currentPath);
      });
      document.body.appendChild(tab);
    }
    tab.classList.add('visible');
  }

  document.getElementById('ftree-close').addEventListener('click', () => {
    panel.style.display = 'none';
    showReopenTab();
  });

  load('');
})();
