// Git 面板：提交树（SVG 车道图）+ Fetch/Pull/Push。
// 数据源 /api/workspace/git/*（主进程直跑 git，见 web_fastapi/git_ops.py）。
// 渲染：单一 SVG 画车道/连线/节点（行高固定 30px），HTML 行叠在右侧承载
// 徽章/文案/交互；车道分配用 gitgraph 式"活跃槽位"算法（active[lane]=
// 下一个期望的父提交），连线为 commit→commit 贝塞尔，天然覆盖竖向连续。
(function () {
  const panel = document.getElementById('git-panel');
  const statusEl = document.getElementById('git-status');
  const graphEl = document.getElementById('git-graph');
  if (!panel || !statusEl || !graphEl) return;

  const API = '/api/workspace/git';
  const ROW_H = 30;
  const LANE_GAP = 12;
  const LANE_PAD = 9;
  const NODE_R = 4.5;
  // 12 色循环调色板（对齐 VSCode Git Graph 观感）
  const PALETTE = ['#e8564f', '#f09819', '#8bc34a', '#26a69a', '#42a5f5',
    '#7e57c2', '#ec407a', '#d4b106', '#13c2c2', '#722ed1', '#fa8c16', '#52c41a'];

  const state = {
    head: '', commits: [], more: false, skip: 0,
    summary: null, filesCache: {}, expanded: new Set(),
    syncing: false, hiddenForever: false,
  };

  // ---------- 小工具 ----------
  function esc(s) { return escapeHtml(String(s == null ? '' : s)); }
  function laneX(l) { return LANE_PAD + l * LANE_GAP + 6; }

  function relTime(unix) {
    const d = Date.now() / 1000 - Number(unix || 0);
    if (d < 90) return '刚刚';
    if (d < 3600) return Math.floor(d / 60) + ' 分钟前';
    if (d < 86400 * 30) return Math.floor(d / 3600) + ' 小时前';
    const t = new Date(Number(unix) * 1000);
    return `${t.getMonth() + 1}/${t.getDate()}`;
  }

  async function getJSON(url) {
    const r = await fetch(url);
    return { status: r.status, data: r.status === 200 ? await r.json() : null };
  }

  // ---------- 状态行 ----------
  function renderStatus() {
    const s = state.summary;
    if (!s) { statusEl.innerHTML = ''; return; }
    if (!s.is_repo) {
      const outer = s.not_repo_reason === 'outer';
      statusEl.innerHTML =
        `<span class="gt-badge gt-dim" title="${outer
          ? '挂载目录是某个更大仓库的子目录（如托管空间项目），面板不显示外层仓库历史'
          : '当前挂载的工作区目录没有初始化 Git 仓库'}">${outer
          ? '↩ 挂载目录属于外层仓库的子目录'
          : '当前目录不是 Git 仓库'}</span>`;
      ['git-fetch', 'git-pull', 'git-push'].forEach(disableBtn);
      return;
    }
    const parts = [];
    parts.push(`<span class="gt-badge gt-branch${s.ahead || s.behind ? ' gt-dirty' : ''}">${esc(s.branch || '(detached)')}</span>`);
    if (s.upstream) {
      let ab = '';
      if (s.ahead) ab += ` ↑${s.ahead}`;
      if (s.behind) ab += ` ↓${s.behind}`;
      parts.push(`<span class="gt-badge gt-dim">${esc(s.upstream)}${ab}</span>`);
    }
    const dirty = s.staged + s.unstaged + s.untracked;
    if (dirty) {
      const seg = [];
      if (s.staged) seg.push(`${s.staged} 暂存`);
      if (s.unstaged) seg.push(`${s.unstaged} 修改`);
      if (s.untracked) seg.push(`${s.untracked} 未跟踪`);
      parts.push(`<span class="gt-badge gt-dirty" title="${esc(seg.join('，'))}">● ${dirty} 处变更</span>`);
    }
    if (s.no_commits) parts.push('<span class="gt-badge gt-dim">无提交</span>');
    statusEl.innerHTML = parts.join(' ');
  }

  function disableBtn(id) {
    const b = document.getElementById(id);
    if (b) b.disabled = true;
  }

  // ---------- 车道分配（活跃槽位算法）----------
  function assignLanes(commits) {
    const idx = new Map(commits.map((c, i) => [c.hash, i]));
    const active = [];          // active[lane] = 期望到达该 lane 的父 hash
    const laneOf = new Map();
    let maxLane = 0;
    for (const c of commits) {
      let lane = active.indexOf(c.hash);
      if (lane >= 0) {
        // 所有期望此提交的槽位收口，提交落在最左那个
        for (let l = lane + 1; l < active.length; l++) {
          if (active[l] === c.hash) active[l] = null;
        }
        active[lane] = null;
      } else {
        lane = active.indexOf(null);
        if (lane < 0) { lane = active.length; }
      }
      laneOf.set(c.hash, lane);
      maxLane = Math.max(maxLane, lane);
      // 第一父延续本 lane；其余父各开新 lane（向右找空位）
      c.parents.forEach((p, pi) => {
        if (!idx.has(p)) return;               // 窗口外的父：不占槽（画残线）
        let l = pi === 0 ? lane : active.indexOf(null);
        if (pi !== 0 && l < 0) l = active.length;
        if (pi !== 0) { maxLane = Math.max(maxLane, l); }
        active[l] = p;
      });
    }
    return { laneOf, maxLane };
  }

  // ---------- 图渲染 ----------
  function renderGraph() {
    const commits = state.commits;
    graphEl.innerHTML = '';
    if (!commits.length) {
      graphEl.innerHTML = '<div class="gt-empty">（无提交）</div>';
      return;
    }
    const { laneOf, maxLane } = assignLanes(commits);
    const idx = new Map(commits.map((c, i) => [c.hash, i]));
    const svgW = laneX(maxLane) + 10;
    const svgH = commits.length * ROW_H;

    // SVG：每条 child→parent 边一条贝塞尔；颜色取子提交所在 lane 的色
    const NS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('class', 'gt-svg');
    svg.setAttribute('width', String(svgW));
    svg.setAttribute('height', String(svgH));
    svg.setAttribute('viewBox', `0 0 ${svgW} ${svgH}`);

    const yOf = i => i * ROW_H + ROW_H / 2;
    commits.forEach((c, i) => {
      const cl = laneOf.get(c.hash) || 0;
      const x1 = laneX(cl), y1 = yOf(i);
      const color = PALETTE[cl % PALETTE.length];
      c.parents.forEach((p, pi) => {
        const j = idx.get(p);
        const path = document.createElementNS(NS, 'path');
        if (j === undefined) {
          // 父在窗口外：画向下的残线
          path.setAttribute('d', `M ${x1} ${y1} L ${x1} ${y1 + ROW_H * 0.8}`);
          path.setAttribute('opacity', '0.5');
        } else {
          const pl = laneOf.get(p) || 0;
          const x2 = laneX(pl), y2 = yOf(j);
          const my = (y1 + y2) / 2;
          path.setAttribute('d',
            `M ${x1} ${y1} C ${x1} ${my}, ${x2} ${my}, ${x2} ${y2}`);
        }
        path.setAttribute('stroke', pi === 0 ? color : PALETTE[(cl + 5) % PALETTE.length]);
        path.setAttribute('fill', 'none');
        path.setAttribute('stroke-width', '1.8');
        svg.appendChild(path);
      });
      // 节点
      const circle = document.createElementNS(NS, 'circle');
      circle.setAttribute('cx', String(x1));
      circle.setAttribute('cy', String(y1));
      circle.setAttribute('r', String(NODE_R));
      circle.setAttribute('fill', color);
      circle.setAttribute('stroke', 'var(--surface)');
      circle.setAttribute('stroke-width', '1.5');
      svg.appendChild(circle);
      if (c.isHead) {
        const ring = document.createElementNS(NS, 'circle');
        ring.setAttribute('cx', String(x1));
        ring.setAttribute('cy', String(y1));
        ring.setAttribute('r', String(NODE_R + 3));
        ring.setAttribute('fill', 'none');
        ring.setAttribute('stroke', color);
        ring.setAttribute('stroke-width', '1.5');
        svg.appendChild(ring);
      }
    });

    // HTML 行（叠在 SVG 右侧）
    const rows = document.createElement('div');
    rows.className = 'gt-rows';
    rows.style.paddingLeft = String(svgW) + 'px';
    commits.forEach((c, i) => {
      const row = document.createElement('div');
      row.className = 'gt-row' + (c.inHead ? '' : ' gt-muted');
      row.dataset.sha = c.hash;
      row.dataset.idx = String(i);
      // 同名去重（对齐 VSCode）：本地有 X 就不再显示 origin/X，
      // 长徽章不再吃满整行挤压提交信息
      const localNames = new Set(c.heads);
      const badges = [];
      c.heads.forEach(h => badges.push(`<span class="gt-badge gt-branch${h === state.branch ? ' gt-cur' : ''}">${esc(h)}</span>`));
      const remotesShown = c.remotes.filter(r => {
        const short = r.includes('/') ? r.split('/').slice(1).join('/') : r;
        return !localNames.has(short);
      });
      remotesShown.forEach(r => badges.push(`<span class="gt-badge gt-remote">${esc(r)}</span>`));
      c.tags.forEach(t => badges.push(`<span class="gt-badge gt-tag">${esc(t)}</span>`));
      row.innerHTML =
        `<div class="gt-line">${badges.join('')}<span class="gt-subj">${esc(c.subject)}</span></div>` +
        `<div class="gt-meta"><span class="gt-hash">${esc(c.abbrev)}</span>` +
        `<span>${esc(c.author)}</span><span>${esc(relTime(c.date))}</span></div>`;
      rows.appendChild(row);
    });

    const wrap = document.createElement('div');
    wrap.className = 'gt-canvas';
    wrap.appendChild(svg);
    wrap.appendChild(rows);
    graphEl.appendChild(wrap);
    if (state.more) {
      const more = document.createElement('div');
      more.className = 'gt-more';
      more.textContent = '↓ 加载更多';
      more.id = 'gt-load-more';
      graphEl.appendChild(more);
    }
  }

  // ---------- 文件清单（点行展开）----------
  async function toggleFiles(row) {
    const sha = row.dataset.sha;
    const exist = row.nextElementSibling;
    if (exist && exist.classList.contains('gt-files')) {
      exist.remove();
      state.expanded.delete(sha);
      return;
    }
    if (!state.filesCache[sha]) {
      const { status, data } = await getJSON(`${API}/commit/${sha}/files`);
      if (status !== 200) { showToast('获取提交文件失败', 'error'); return; }
      state.filesCache[sha] = data.files || [];
    }
    const box = document.createElement('div');
    box.className = 'gt-files';
    const files = state.filesCache[sha];
    if (!files.length) {
      box.textContent = '（无文件变更或为合并提交）';
    } else {
      files.forEach(f => {
        const ln = document.createElement('div');
        const letter = document.createElement('span');
        letter.className = 'gt-fstat s-' + (f.status || 'M');
        letter.textContent = f.status || 'M';
        const p = document.createElement('span');
        p.textContent = f.path;
        ln.append(letter, p);
        box.appendChild(ln);
      });
    }
    row.after(box);
    state.expanded.add(sha);
  }

  // ---------- 数据加载 ----------
  async function loadSummary() {
    if (state.hiddenForever) return;
    const { status, data } = await getJSON(`${API}/summary`);
    if (status === 409) { panel.hidden = true; state.hiddenForever = true; return; }
    if (status !== 200 || !data) return;
    panel.hidden = false;
    state.summary = data;
    renderStatus();
  }

  async function loadLog(reset) {
    if (state.hiddenForever) return;
    if (reset) { state.skip = 0; state.commits = []; state.filesCache = {}; state.expanded.clear(); }
    const { status, data } = await getJSON(`${API}/log?limit=50&skip=${state.skip}`);
    if (status === 409) { panel.hidden = true; state.hiddenForever = true; return; }
    if (status !== 200 || !data) return;
    panel.hidden = false;
    state.head = data.head || '';
    state.branch = data.branch || '';
    state.commits = state.commits.concat(data.commits || []);
    state.more = !!data.more;
    state.skip = state.commits.length;
    renderGraph();
  }

  async function refreshAll() { await loadSummary(); await loadLog(true); }

  // ---------- 同步（fetch/pull/push：入队 + 轮询账本）----------
  async function sync(action) {
    if (state.syncing) { showToast('已有同步任务在进行'); return; }
    if (!state.summary || !state.summary.is_repo) return;
    state.syncing = true;
    ['git-fetch', 'git-pull', 'git-push'].forEach(disableBtn);
    statusEl.insertAdjacentHTML('beforeend',
      ` <span class="gt-badge gt-syncing" id="gt-sync-flag">${action} 进行中…</span>`);
    try {
      const resp = await fetch(`${API}/sync`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, remote: 'origin' }),
      });
      const body = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        showToast(`git ${action} 入队失败：${body.detail || resp.status}`, 'error');
        return;
      }
      const runId = body.run_id;
      // 轮询账本（复用 GET /api/runs）
      for (let n = 0; n < 220; n++) {
        await new Promise(r => setTimeout(r, 1500));
        let run = null;
        try {
          const rr = await fetch('/api/runs');
          if (rr.ok) {
            run = ((await rr.json()).runs || []).find(x => x.run_id === runId) || null;
          }
        } catch (e) { /* 瞬断继续轮 */ }
        if (!run) continue;
        if (run.status === 'done') {
          showToast(`git ${action} 完成`);
          await refreshAll();
          return;
        }
        if (run.status === 'failed' || run.status === 'interrupted') {
          showToast(`git ${action} 失败：${(run.error || '').slice(0, 160)}`, 'error');
          await refreshAll();
          return;
        }
      }
      showToast(`git ${action} 仍在后台执行，稍后手动刷新查看`, 'error');
    } finally {
      state.syncing = false;
      const flag = document.getElementById('gt-sync-flag');
      if (flag) flag.remove();
      ['git-fetch', 'git-pull', 'git-push'].forEach(id => {
        const b = document.getElementById(id);
        if (b) b.disabled = false;
      });
    }
  }

  // ---------- 折叠 ----------
  function applyFold() {
    const folded = localStorage.getItem('hermes_git_fold') === '1';
    panel.classList.toggle('collapsed', folded);
    const btn = document.getElementById('git-fold');
    if (btn) btn.textContent = folded ? '▸' : '▾';
  }

  // ---------- 事件绑定 ----------
  document.getElementById('git-refresh')?.addEventListener('click', refreshAll);
  document.getElementById('git-fetch')?.addEventListener('click', () => sync('fetch'));
  document.getElementById('git-pull')?.addEventListener('click', () => sync('pull'));
  document.getElementById('git-push')?.addEventListener('click', () => sync('push'));
  document.getElementById('git-fold')?.addEventListener('click', () => {
    localStorage.setItem('hermes_git_fold',
      panel.classList.contains('collapsed') ? '0' : '1');
    applyFold();
  });

  graphEl.addEventListener('click', e => {
    const more = e.target.closest('#gt-load-more');
    if (more) { loadLog(false); return; }
    const row = e.target.closest('.gt-row');
    if (row) toggleFiles(row);
  });
  // 滚动到底自动加载
  graphEl.addEventListener('scroll', () => {
    if (state.more &&
        graphEl.scrollTop + graphEl.clientHeight >= graphEl.scrollHeight - 24) {
      loadLog(false);
    }
  }, { passive: true });

  // agent 会话内 commit / sync 完成后自动刷新（chat.js 防抖派发）
  document.addEventListener('hermes:git-refresh', refreshAll);

  applyFold();
  refreshAll();
})();
