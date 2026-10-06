// Todo 常驻小窗 + 面板拖动/列表拖拽排序——自 chat.js 首批分块外置拆出，逐字搬移（P2-4）
// ---------- Todo 常驻小窗 ----------
// write_todos 执行时后端推送 todos_update 事件，
// 这里全量渲染 todo 清单到右上角常驻小窗。
// - 空/全完成 → 自动隐藏
// - 用户点 × → 手动隐藏，直到下一次新 todos_update 再出现
// - 新一轮 write_todos → 全量替换（write_todos 本身就是全量替换语义）
// - 2026-09-06: 拖拽重排——vendor/Sortable.min.js 绑定 #todo-list（仅
//   ⋮⋮ 把手可拖，类名对齐 wakerflow），onEnd 把新顺序交给本函数走同一
//   全量重渲路径（status/content 原样，仅调序）。渲染改 createElement/
//   textContent 构造——todo content 是 LLM 可控内容，零 HTML 插值
//   （比原 escapeHtml 模板更强的注入免疫形态）。
let _todoDismissed = false;   // 用户手动收起标记；新 todos_update 时重置
let _todoFadeTimer = null;    // 全部完成时的自动淡出定时器
let _currentTodos = [];       // 面板当前列表（拖拽回写的数据源，独立副本）
let _todoSortableInited = false;  // Sortable 只 create 一次（#todo-list 是静态元素）
let _todoRenderGen = 0;       // renderTodoPanel 全量重渲代际（每次渲染递增）
let _todoDragGen = null;      // 拖拽开始时的代际快照（onStart 记 / onEnd 验）

// status 类名白名单（后端已强制，前端兜底镜像一份，防脏类名注入 class 表）
const TODO_STATUSES = new Set(['pending', 'in_progress', 'completed', 'cancelled']);

function renderTodoPanel(todos) {
  const panel = document.getElementById('todo-panel');
  if (!panel) return;

  // 空列表 → 隐藏
  if (!todos || todos.length === 0) {
    panel.style.display = 'none';
    _todoDismissed = false;
    _currentTodos = [];
    return;
  }

  // 深拷贝：面板持有独立副本，拖拽重排只动这份，不反写事件流数据
  _currentTodos = todos.map(t => ({
    id: t.id || '',
    content: String(t.content || ''),
    status: TODO_STATUSES.has(t.status) ? t.status : 'pending',
  }));
  const current = _currentTodos;
  const ICONS = { pending: '○', in_progress: '◐', completed: '●', cancelled: '✕' };
  const done = current.filter(t => t.status === 'completed').length;
  const allDone = done === current.length;

  // 新的 todos_update 到达 → 重置手动收起标记，恢复显示；代际递增
  // （拖拽中收到推送重渲时，进行中的 Sortable onEnd 据此作废索引）
  _todoDismissed = false;
  _todoRenderGen++;
  if (_todoFadeTimer) { clearTimeout(_todoFadeTimer); _todoFadeTimer = null; }
  panel.classList.remove('todo-fading');

  document.getElementById('todo-header-text').textContent =
    allDone ? `📋 待办 ${done}/${current.length} ✓` : `📋 待办 ${done}/${current.length}`;
  const list = document.getElementById('todo-list');
  list.innerHTML = '';   // 全量替换渲染（write_todos 全量替换语义）
  for (const t of current) {
    const li = document.createElement('li');
    li.className = 'todo-item todo-' + t.status;   // 白名单内的安全插值
    const handle = document.createElement('span');
    handle.className = 'todo-handle';
    handle.textContent = '⋮⋮';
    handle.title = '拖拽排序';
    const icon = document.createElement('span');
    icon.className = 'todo-icon';
    icon.textContent = ICONS[t.status] || '○';
    const text = document.createElement('span');
    text.className = 'todo-text';
    text.textContent = t.content;
    li.append(handle, icon, text);
    list.appendChild(li);
  }

  ensureTodoSortable();
  panel.style.display = '';
  // 自定义位置存在时重夹取（display:none 期间 offsetWidth=0，初次恢复的
  // 夹取可能偏宽；重新显示时按真实尺寸修正）
  if (panel.style.left) _clampTodoPanelPos();

  // 全部完成 → 展示 3 秒后自动淡出隐藏
  if (allDone) {
    _todoFadeTimer = setTimeout(() => {
      panel.classList.add('todo-fading');
      setTimeout(() => { panel.style.display = 'none'; }, 600); // 等 CSS 过渡完成
    }, 3000);
  }
}

// 拖拽重排（vendor/Sortable.min.js，与 wakerflow 同源同类名）：仅 ⋮⋮ 把手
// 可拖，避免误触；onEnd 把新顺序经 renderTodoPanel 全量重渲——这是本文件
// 唯一的 todo 更新路径，status/content 不变仅调序。后端 write_todos 是
// 全量替换语义，agent 下一次推送天然覆盖面板顺序，无增量同步问题。
function ensureTodoSortable() {
  if (_todoSortableInited || typeof Sortable === 'undefined') return;
  const list = document.getElementById('todo-list');
  if (!list) return;
  Sortable.create(list, {
    handle: '.todo-handle',
    animation: 150,
    ghostClass: 'sortable-ghost',
    chosenClass: 'sortable-chosen',
    dragClass: 'sortable-drag',
    onStart() {
      // 记录拖拽开始时的列表代际：拖拽期间若 todos_update 全量重渲过
      // （renderTodoPanel 递增 _todoRenderGen），onEnd 的索引不再可信
      _todoDragGen = _todoRenderGen;
    },
    onEnd(evt) {
      const genAtDragStart = _todoDragGen;
      _todoDragGen = null;
      const { oldIndex, newIndex } = evt;
      if (oldIndex === newIndex || oldIndex == null || newIndex == null) return;
      // 代际校验：拖拽期间 renderTodoPanel 全量重渲过 → DOM 顺序已被推送
      // 数据重建，本次 splice 基于过期索引，直接作废（新数据已是权威顺序）
      if (genAtDragStart === null || genAtDragStart !== _todoRenderGen) return;
      // 长度边界兜底（代际一致时长度也应一致，防御性保留）
      if (oldIndex >= _currentTodos.length || newIndex >= _currentTodos.length) return;
      const moved = _currentTodos.splice(oldIndex, 1)[0];
      if (moved) _currentTodos.splice(newIndex, 0, moved);
      renderTodoPanel(_currentTodos);
    },
  });
  _todoSortableInited = true;
}

// ---------- Todo 面板整体拖动（2026-09-05，用户实测反馈） ----------
// 「todo的拖拽是指整个待办面板」：把面板做成可拖动的浮动面板——标题栏
// 作把手（cursor: move），拖动改 fixed left/top，位置存 localStorage
// （hermes_todo_panel_pos，全局），加载时恢复并视口夹取；双击标题栏或 ⤾
// 按钮回 CSS 默认位。与 #todo-list 的 Sortable 列表项排序不冲突：把手只在
// 标题栏，列表项的 ⋮⋮ 把手另属 Sortable（互不监听对方区域）。
const TODO_POS_KEY = 'hermes_todo_panel_pos';

function _todoPanelEl() { return document.getElementById('todo-panel'); }

// 应用位置（视口内夹取：左右不出界，下方至少完整露出标题栏）
function _applyTodoPanelPos(left, top) {
  const panel = _todoPanelEl();
  if (!panel) return;
  const w = panel.offsetWidth || 240;
  const maxX = Math.max(8, window.innerWidth - w - 8);
  const maxY = Math.max(8, window.innerHeight - 44);   // 44px ≈ 标题栏高度
  left = Math.min(Math.max(8, left), maxX);
  top = Math.min(Math.max(8, top), maxY);
  panel.style.position = 'fixed';
  panel.style.left = Math.round(left) + 'px';
  panel.style.top = Math.round(top) + 'px';
  panel.style.right = 'auto';   // 顶掉 CSS 默认的 right:1.5rem 锚定
}

// 已有内联位置 → 按当前视口/尺寸重夹取（resize / 面板重新显示时）
function _clampTodoPanelPos() {
  const panel = _todoPanelEl();
  if (!panel || !panel.style.left) return;
  _applyTodoPanelPos(parseFloat(panel.style.left) || 0, parseFloat(panel.style.top) || 0);
}

// 回默认位置：清 storage + 摘除内联定位（回落 CSS 的 top:4.5rem; right:1.5rem）
function _resetTodoPanelPos() {
  const panel = _todoPanelEl();
  if (!panel) return;
  try { localStorage.removeItem(TODO_POS_KEY); } catch (e) {}
  panel.style.position = '';
  panel.style.left = '';
  panel.style.top = '';
  panel.style.right = '';
}

(function _initTodoPanelDrag() {
  const panel = _todoPanelEl();
  const header = panel ? panel.querySelector('.todo-header') : null;
  if (!panel || !header) return;

  // 把手交互属性用内联样式兜底（CSS 可能被 ?v 缓存命中而不新鲜——
  // main.css 的版本号在 base.html，本轮不可改，内联保证三属性必定生效）
  header.style.cursor = 'move';        // 移动面板的视觉把手语义
  header.style.userSelect = 'none';    // 拖动不选中文本
  header.style.touchAction = 'none';   // 触屏拖动不被页面滚动劫持成 pointercancel

  // 恢复持久化位置（夹取防屏幕外）
  try {
    const pos = JSON.parse(localStorage.getItem(TODO_POS_KEY) || 'null');
    if (pos && typeof pos.left === 'number' && typeof pos.top === 'number') {
      _applyTodoPanelPos(pos.left, pos.top);
    }
  } catch (e) {}

  // 视口尺寸变化时重夹取，避免面板被留在屏幕外
  window.addEventListener('resize', _clampTodoPanelPos);

  let drag = null;   // {startX, startY, baseL, baseT, moved}
  const SKIP = '.todo-close, .todo-reset-pos';   // 关闭/复位按钮不触发拖动
  header.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 || e.target.closest(SKIP)) return;
    const rect = panel.getBoundingClientRect();
    drag = { startX: e.clientX, startY: e.clientY, baseL: rect.left, baseT: rect.top, moved: false };
    try { header.setPointerCapture(e.pointerId); } catch (err) {}
  });
  header.addEventListener('pointermove', (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.startX, dy = e.clientY - drag.startY;
    if (!drag.moved) {
      if (Math.abs(dx) < 3 && Math.abs(dy) < 3) return;   // 3px 死区，留给双击
      drag.moved = true;
      panel.classList.add('todo-dragging');
    }
    _applyTodoPanelPos(drag.baseL + dx, drag.baseT + dy);
  });
  const endDrag = (e) => {
    if (!drag) return;
    const moved = drag.moved;
    drag = null;
    panel.classList.remove('todo-dragging');
    try { header.releasePointerCapture(e.pointerId); } catch (err) {}
    if (moved) {
      const r = panel.getBoundingClientRect();
      try {
        localStorage.setItem(TODO_POS_KEY,
          JSON.stringify({ left: Math.round(r.left), top: Math.round(r.top) }));
      } catch (err) {}
    }
  };
  header.addEventListener('pointerup', endDrag);
  header.addEventListener('pointercancel', endDrag);

  // 双击标题栏 → 回默认位置（⤾ 按钮同效）
  header.addEventListener('dblclick', (e) => {
    if (e.target.closest(SKIP)) return;
    _resetTodoPanelPos();
  });
  const resetBtn = document.getElementById('todo-reset-pos');
  if (resetBtn) resetBtn.addEventListener('click', _resetTodoPanelPos);
})();

// 关闭按钮：手动收起，直到下一次新 todos_update
document.addEventListener('DOMContentLoaded', () => {
  const btn = document.getElementById('todo-close');
  if (btn) btn.addEventListener('click', () => {
    const panel = document.getElementById('todo-panel');
    if (panel) panel.style.display = 'none';
    _todoDismissed = true;
    if (_todoFadeTimer) { clearTimeout(_todoFadeTimer); _todoFadeTimer = null; }
  });
});
