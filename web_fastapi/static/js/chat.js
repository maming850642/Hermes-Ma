// SSE 流式对话 + HITL 审批渲染 + 指令拦截 + 历史回显（Apple 风交互）
const msgBox = document.getElementById('messages');
const input = document.getElementById('msg-input');
const sendBtn = document.getElementById('send-btn');
const approvalArea = document.getElementById('approval-area');
const sessionInfo = document.getElementById('session-info');

// ---------- 推理开关（per-send） ----------
// 两个独立开关：🧠主思考 / 🔧子思考。Claude Code 风格：点击切换高亮，影响下一次发送。
// 状态记忆到 localStorage，保留上次选择。
const thinkMainBtn = document.getElementById('think-main-btn');
const thinkSubBtn = document.getElementById('think-sub-btn');
let mainThinking = localStorage.getItem('hermes_think_main') === '1';
let subThinking = localStorage.getItem('hermes_think_sub') === '1';

function syncThinkBtns() {
  thinkMainBtn.classList.toggle('active', mainThinking);
  thinkSubBtn.classList.toggle('active', subThinking);
}
syncThinkBtns();
thinkMainBtn.addEventListener('click', () => {
  mainThinking = !mainThinking;
  localStorage.setItem('hermes_think_main', mainThinking ? '1' : '0');
  syncThinkBtns();
});
thinkSubBtn.addEventListener('click', () => {
  subThinking = !subThinking;
  localStorage.setItem('hermes_think_sub', subThinking ? '1' : '0');
  syncThinkBtns();
});

// ---------- 流式状态锁 ----------
// 发送中禁止重复发送。
// 2026-08-17: 恢复「停止按钮」——worker 侧 chat_stop 已内联化（置 _cancel_event，
// 下一个事件间隙生效），按钮真实可用：先通知 worker 取消，再中断本地读取。
let isStreaming = false;
let _stopBtn = null;
function _ensureStopBtn() {
  if (_stopBtn) return _stopBtn;
  const btn = document.createElement('button');
  btn.id = 'stop-btn';
  // 与 send-btn 同款圆形键，插在发送键前一格——生成中发送键隐藏后，
  // 停止键占据同一视觉位置（同位切换，不并排出现两个键）
  btn.className = 'send-btn btn-primary';
  btn.textContent = '■';
  btn.title = '停止生成';
  btn.style.display = 'none';
  btn.addEventListener('click', stopGeneration);
  if (sendBtn && sendBtn.parentElement) sendBtn.parentElement.insertBefore(btn, sendBtn);
  _stopBtn = btn;
  return btn;
}
async function stopGeneration() {
  try {
    const resp = await fetch('/api/chat/stop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: currentSessionId || '' }),
    });
    if (!resp.ok) {
      // 取消请求失败不再静默——worker 可能仍被占用，用户需要知道
      showToast('停止请求失败（HTTP ' + resp.status + '），生成可能仍在后台进行', 'error');
    }
  } catch (e) {
    showToast('停止请求发送失败，生成可能仍在后台进行', 'error');
  }
  if (_currentAbort) { try { _currentAbort.abort(); } catch (e) {} }
}
function setStreaming(on) {
  isStreaming = on;
  // 流结束不放开发送当且仅当同时处于恢复窗口（_needsSessionRecovery 且
  // 会话 id 未恢复）——busy 重试等待期 keepLock 已另行保锁
  sendBtn.disabled = on || (_needsSessionRecovery && !currentSessionId);
  // 同位切换：生成中隐藏发送键、原位显示停止键；结束后换回
  sendBtn.style.display = on ? 'none' : '';
  const sb = _ensureStopBtn();
  sb.style.display = on ? '' : 'none';
  sb.disabled = false;
}

// ---------- 当前 SSE 流的取消句柄（问题2：切会话前主动取消流）----------
// 提升 reader/abortController 到模块作用域，便于切换会话前 abort + reader.cancel，
// 让浏览器尽快断开 SSE，减少 worker 往已无人读的 stdout 写事件导致管道写满卡死。
let _currentReader = null;
let _currentAbort = null;
function abortCurrentStream() {
  try { _currentAbort && _currentAbort.abort(); } catch (e) {}
  try { _currentReader && _currentReader.cancel(); } catch (e) {}
}

// 输入框 auto-grow：随内容增高，上限 8 行
function autoGrow() {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 8 * 24) + 'px';
}
input.addEventListener('input', autoGrow);

// ---------- 空状态 + 回到底部 ----------
const emptyState = document.getElementById('empty-state');
const scrollBottomBtn = document.getElementById('scroll-bottom-btn');

function refreshEmptyState() {
  if (!emptyState) return;
  const hasMsgs = msgBox.children.length > 0;
  emptyState.classList.toggle('hidden', hasMsgs);
}

// 滚动监听：离底部远时显示「回到底部」+ 维护 sticky-scroll 跟随态
// （2026-09-19：生成中用户上翻不再被强制拽回——同源阈值，一套距离计算）
// P4-4 方向感知吸底：旧逻辑 followBottom = nearBottom（120px 窗口）还有个
// 反直觉后果——用户下翻历史刚滑进窗口，就被每帧 flush 的 scrollBottom 拽回
// 底部，下翻手势被吞（上次只修了上翻方向）。改为：上滚（scrollTop 变小）
// 立即脱离跟随；仅距底 <24px 才重新吸底；两者之间保持原值不抢。程序滚动
// （scrollBottom → scrollTop=scrollHeight，距底 0）仍会把 followBottom 收敛
// 回 true，状态自洽。
const STICKY_THRESHOLD = 120;
const REATTACH_THRESHOLD = 24;   // 重新吸底的贴底距离（下翻到贴近底部自然续上跟随）
let followBottom = true;
let _lastScrollTop = msgBox.scrollTop;
msgBox.addEventListener('scroll', () => {
  const st = msgBox.scrollTop;
  const distBottom = msgBox.scrollHeight - st - msgBox.clientHeight;
  if (st < _lastScrollTop) followBottom = false;                  // 上滚：立即脱离跟随
  else if (distBottom < REATTACH_THRESHOLD) followBottom = true;  // 贴底：重新吸
  /* 其余（下翻进窗但未贴底）：保持原值——不抢用户滚动 */
  _lastScrollTop = st;
  if (!scrollBottomBtn) return;
  scrollBottomBtn.classList.toggle('visible', distBottom >= STICKY_THRESHOLD);
});
if (scrollBottomBtn) {
  scrollBottomBtn.addEventListener('click', () => {
    followBottom = true;
    msgBox.scrollTop = msgBox.scrollHeight;
    scrollBottomBtn.classList.remove('visible');
  });
}
// 建议气泡点击 → 填入输入框并发送
document.querySelectorAll('.suggestion-chip').forEach(chip => {
  chip.addEventListener('click', () => {
    input.value = chip.dataset.q || '';
    autoGrow();
    if (!isStreaming) send();
  });
});

// ---------- 指令定义 ----------
const COMMANDS = {
  '/help':    { desc: '显示可用指令', action: showHelp },
  '/reset':   { desc: '重置当前会话（清空对话历史）', action: cmdReset },
  '/compact': { desc: '手动压缩对话历史', action: cmdCompact },
  '/save':    { desc: '手动保存当前会话', action: cmdSave },
  '/tools':   { desc: '列出 Agent 可用工具', action: cmdTools },
  '/skills':  { desc: '列出可用技能', action: cmdSkills },
  '/memory':  { desc: '查看长期记忆', action: () => location.href = '/memory' },
  '/config':  { desc: '打开设置', action: () => location.href = '/config' },
};

function showHelp() {
  const lines = Object.entries(COMMANDS).map(([cmd, c]) => `**${cmd}** — ${c.desc}`);
  addAssistantMsg('📖 **可用指令**\n\n' + lines.join('\n'));
}

async function cmdReset() {
  const ok = await uiConfirm({
    title: '重置当前会话',
    body: '将切换到新草稿（当前会话保留在侧栏，可随时切回）。',
  });
  if (!ok) return;
  startDraftSession();
  addAssistantMsg('✅ 已切换到新草稿。发出首条消息时才会创建会话并自动命名；旧会话保留在侧栏。');
}

async function cmdCompact() {
  addAssistantMsg('⏳ 正在压缩对话历史...');
  // 记住占位气泡：网络异常/非 JSON 响应等失败路径要移除它并提示，
  // 否则"正在压缩"永久悬挂
  const pendingBubble = msgBox.lastElementChild;
  // P2-14 移交：带当前会话 id，后端按 chat 闸门亲和路由压缩对应桶
  //（无参时后端恒打 main 槽——压缩的是空/旧桶）
  try {
    const r = await fetch('/api/compact?session_id=' + encodeURIComponent(currentSessionId || ''), { method: 'POST' });
    const data = await r.json();
    if (data.ok) {
      if (data.history) {
        msgBox.innerHTML = '';
        for (const m of data.history) {
          if (m.role === 'user') addUserMsg(m.content);
          else if (m.role === 'assistant') addAssistantMsg(m.content);
        }
        refreshEmptyState();
      }
      addAssistantMsg(`✅ 已压缩 ${data.compacted_count} 条早期消息`);
    } else {
      addAssistantMsg('ℹ️ ' + data.message);
    }
  } catch (e) {
    if (pendingBubble && pendingBubble.parentNode === msgBox) pendingBubble.remove();
    addAssistantMsg('⚠️ 压缩失败：' + (e && e.message ? e.message : '网络异常'));
  }
}

async function cmdSave() {
  // 对齐 cmdCompact（P2-14）：带 session_id 让后端亲和路由到本会话的
  // worker 槽，保存的是该会话而非 main 槽当前桶
  const r = await fetch('/api/sessions/save?session_id=' + encodeURIComponent(currentSessionId || ''), { method: 'POST' });
  const data = await r.json();
  addAssistantMsg(`✅ 会话已保存（${data.message_count} 条消息）`);
}

async function cmdTools() {
  const r = await fetch('/api/tools');
  const data = await r.json();
  const lines = data.tools.map(t => `- **${t.name}** — ${t.description}`);
  addAssistantMsg(`🔧 **可用工具（${data.count} 个）**\n\n` + lines.join('\n'));
}

async function cmdSkills() {
  const r = await fetch('/api/skills');
  const data = await r.json();
  if (!data.skills.length) { addAssistantMsg('🎯 暂无可用技能'); return; }
  const lines = data.skills.map(s => `- ${s.source_icon} **${s.name}** — ${s.description}`);
  addAssistantMsg(`🎯 **可用技能（${data.count} 个）**\n\n` + lines.join('\n'));
}

// sticky-scroll（2026-09-19）：仅当用户本就在底部附近才跟随滚动——生成中
// 上翻历史不再被每个 token 拽回底部；force=true 用于用户主动触发的例外
// （发消息、重绘历史、切换会话、点 ↓）。程序滚动会触发 scroll 事件把
// followBottom 重新收敛为 true，状态自洽。
function scrollBottom(force) {
  if (!force && !followBottom) return;
  msgBox.scrollTop = msgBox.scrollHeight;
}

// 长/结构化 markdown 判定：命中则用户气泡切浅色底（蓝底白字渲染表格
// 与代码块的可读性天然崩坏——会话总结粘贴续聊场景的实际反馈）
function isLongMarkdown(t) {
  if (!t) return false;
  if (t.length > 500) return true;
  if (/^#{1,6} \S/m.test(t)) return true;      // md 标题行
  if (/^\s*\|.*\|/m.test(t)) return true;      // 表格行
  if (/```/.test(t)) return true;              // 代码围栏
  return false;
}

function addUserMsg(text, images) {
  const div = document.createElement('div');
  div.className = 'msg user';
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  if (isLongMarkdown(text)) bubble.classList.add('bubble-md');
  // 多模态：先渲染图片缩略图，再渲染文本
  if (images && images.length) {
    const gallery = document.createElement('div');
    gallery.className = 'msg-image-gallery';
    images.forEach(img => {
      const im = document.createElement('img');
      im.src = img.url;
      im.alt = img.filename || '';
      im.addEventListener('click', () => openImageLightbox(img.url));
      // 图片异步解码撑高不触发 scroll 事件——加载完成后按跟随态补一次滚动，
      // 否则 sticky-scroll 下跟丢（内容撑高而视窗停在原地）
      im.addEventListener('load', () => scrollBottom());
      gallery.appendChild(im);
    });
    bubble.appendChild(gallery);
  }
  if (text) {
    const textEl = document.createElement('div');
    textEl.innerHTML = renderMarkdown(text);
    bubble.appendChild(textEl);
  }
  enhanceCodeBlocks(bubble);
  div.appendChild(bubble);
  attachUserMsgActions(div, text);
  msgBox.appendChild(div);
  scrollBottom();
  refreshEmptyState();
  return div;
}

// ---------- user 气泡操作（2026-09-19）：复制 / 编辑重发 / 从此 fork ----------
// 简约线性图标（Feather，MIT），悬浮在气泡下方右侧（DeepSeek 式），hover 显隐。
// 序数在点击时按 DOM 内 .msg.user 顺序现算——历史重绘、续流补插都不用维护
// 计数。原文本存在闭包里作 expect_text 乐观校验（防多标签页/陈旧视图误删）。

const _MSG_ICON_SVGS = {
  // 静态图标串（无动态数据，innerHTML 安全）
  copy: '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
  edit: '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/></svg>',
  branch: '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="6" y1="3" x2="6" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/></svg>',
};

function _mkIconBtn(svgKey, title, fn) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'msg-user-btn';
  b.title = title;
  b.innerHTML = _MSG_ICON_SVGS[svgKey];   // 静态图标，无动态内容
  b.addEventListener('click', (e) => { e.stopPropagation(); fn(); });
  return b;
}

async function copyUserMsgText(text) {
  try {
    await navigator.clipboard.writeText(text || '');
    showToast('已复制');
  } catch (e) {
    showToast('复制失败', 'error');
  }
}

function attachUserMsgActions(div, text) {
  const actions = document.createElement('div');
  actions.className = 'msg-user-actions';
  actions.append(
    _mkIconBtn('copy', '复制', () => copyUserMsgText(text)),
    _mkIconBtn('edit', '编辑后重新发送（该消息之后的历史会被替换）',
      () => startEditUserMsg(div, text)),
    _mkIconBtn('branch', '从此消息 fork 新会话（含该轮回答，原会话不动）',
      () => forkFromUserMsg(div)),
  );
  div.appendChild(actions);
}

function _userMsgOrdinal(div) {
  return [...msgBox.querySelectorAll('.msg.user')].indexOf(div);
}

function _msgActionGuard() {
  if (isStreaming || _pendingTurnActive) {
    showToast('AI 正在回复，请先停止或稍候'); return false;
  }
  if (_needsSessionRecovery && !currentSessionId) {
    showToast('会话恢复中，请稍候…'); return false;
  }
  if (!currentSessionId) {
    showToast('草稿会话还没有可操作的历史'); return false;
  }
  return true;
}

function startEditUserMsg(div, originalText) {
  if (!_msgActionGuard()) return;
  const bubble = div.querySelector('.bubble');
  if (!bubble || div._editing) return;
  div._editing = true;
  div.classList.add('editing');
  bubble.innerHTML = '';
  const ta = document.createElement('textarea');
  ta.className = 'msg-edit-box';
  ta.value = originalText || '';
  const row = document.createElement('div');
  row.className = 'msg-edit-row';
  const cancelBtn = document.createElement('button');
  cancelBtn.type = 'button';
  cancelBtn.className = 'btn-small';
  cancelBtn.textContent = '取消';
  const resendBtn = document.createElement('button');
  resendBtn.type = 'button';
  resendBtn.className = 'btn-small msg-edit-send';
  resendBtn.textContent = '保存并重发';
  row.append(cancelBtn, resendBtn);
  bubble.append(ta, row);
  scrollBottom();

  const restore = () => {
    div._editing = false;
    div.classList.remove('editing');
    bubble.innerHTML = '';
    const contentEl = document.createElement('div');
    contentEl.innerHTML = renderMarkdown(originalText || '');
    bubble.appendChild(contentEl);
    enhanceCodeBlocks(bubble);
  };
  cancelBtn.addEventListener('click', restore);
  resendBtn.addEventListener('click', async () => {
    const newText = ta.value.trim();
    if (!newText) { showToast('内容不能为空'); return; }
    resendBtn.disabled = true;
    try {
      const r = await fetch(
        `/api/sessions/${encodeURIComponent(currentSessionId)}/truncate`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            user_ordinal: _userMsgOrdinal(div),
            expect_text: (originalText || '').trim(),
          }),
        });
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        showToast('截断失败：' + (j.detail || 'HTTP ' + r.status), 'error');
        resendBtn.disabled = false;
        return;
      }
      const data = await r.json().catch(() => ({}));
      // 截断成功：按权威前缀重绘历史，再走普通发送链路（streamMessage
      // 自带 busy 重试/看门狗/流锁收尾；服务端 prelog 链路零改动兼容）
      renderHistory(data.history || []);
      addUserMsg(newText);
      rememberCurrentSession(currentSessionId);
      updateSessionInfo(currentSessionId);
      streamMessage('/api/chat/stream',
        { message: newText, thinking: mainThinking, subagent_thinking: subThinking,
          images: [], session_id: currentSessionId,
          waker: (typeof currentWaker !== 'undefined' ? currentWaker : '') || '' },
        false);
    } catch (e) {
      showToast('截断失败：网络异常', 'error');
      resendBtn.disabled = false;
    }
  });
}

async function forkFromUserMsg(div) {
  if (!_msgActionGuard()) return;
  try {
    const r = await fetch(
      `/api/sessions/${encodeURIComponent(currentSessionId)}/fork`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ up_to_user_ordinal: _userMsgOrdinal(div) }),
      });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      showToast('fork 失败：' + (j.detail || 'HTTP ' + r.status), 'error');
      return;
    }
    const j = await r.json().catch(() => ({}));
    if (!j.session_id) { showToast('fork 失败：响应缺少会话 id', 'error'); return; }
    showToast('已创建分支 #' + j.session_id + '，正在切换…');
    // 切换惯例同侧栏 switchToSession：断流 → 记住新 sid → 整页重载
    abortCurrentStream();
    rememberCurrentSession(j.session_id);
    setTimeout(() => location.reload(), 400);
  } catch (e) {
    showToast('fork 失败：网络异常', 'error');
  }
}

function addAssistantMsg(text) {
  const div = document.createElement('div');
  div.className = 'msg assistant';
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  const contentEl = document.createElement('div');
  contentEl.className = 'bubble-content';
  contentEl.innerHTML = renderMarkdown(text);
  bubble.appendChild(contentEl);
  bubble._contentEl = contentEl;
  enhanceCodeBlocks(contentEl);
  div.appendChild(bubble);
  msgBox.appendChild(div);
  scrollBottom();
  refreshEmptyState();
}

function newAssistantBubble() {
  const div = document.createElement('div');
  div.className = 'msg assistant';
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  // 2026-06-26: 气泡内部分两层——content（正文，默认）+ reasoning（懒插入顶部）。
  // reasoning 块由 getReasoningBlock 在首个 reasoning_token 时插入到 content 之前。
  const contentEl = document.createElement('div');
  contentEl.className = 'bubble-content';
  bubble.appendChild(contentEl);
  bubble._contentEl = contentEl;
  div.appendChild(bubble);
  msgBox.appendChild(div);
  scrollBottom();
  refreshEmptyState();
  return bubble;
}

// 2026-06-26: 推理（思考过程）可折叠块渲染。
// 在气泡内维护两个区域：reasoning（可折叠，默认折叠，正文上方）+ content（正文）。
// 用 getReasoningBlock 懒创建 <details>，首个 reasoning_token 到达时插入气泡顶部。
function getReasoningBlock(bubble) {
  if (bubble._reasoningBlock && bubble.contains(bubble._reasoningBlock)) return bubble._reasoningBlock;
  const det = document.createElement('details');
  det.className = 'reasoning';
  det.open = true;  // 默认展开，避免用户看漏（与工具面板一致）
  const sum = document.createElement('summary');
  sum.textContent = '💭 推理过程';
  det.appendChild(sum);
  const body = document.createElement('div');
  body.className = 'reasoning-body';
  det.appendChild(body);
  bubble.insertBefore(det, bubble.firstChild);
  bubble._reasoningBlock = det;
  bubble._reasoningBody = body;
  return det;
}

  function renderReasoning(bubble, text) {
    // 终态渲染（complete/流结束/兜底）：整文一次，并使增量渲染器失效
    //（下次流式由 _ensureBlockRenderer 重建两段结构）
    const det = getReasoningBlock(bubble);
    bubble._reasoningRenderer = null;
    bubble._reasoningBody.innerHTML = renderMarkdown(text);
    // 始终滚到最新：body 限高 320px 内部滚动，流式期间跟随输出
    if (det.open) bubble._reasoningBody.scrollTop = bubble._reasoningBody.scrollHeight;
  }

function renderContent(bubble, html) {
  // 终态/占位渲染：innerHTML 全量替换（complete 终渲、错误文案、tool_start
  // 清屏、排队提示），并使增量渲染器失效
  bubble._contentRenderer = null;
  if (!bubble._contentEl) {
    const el = document.createElement('div');
    el.className = 'bubble-content';
    bubble.appendChild(el);
    bubble._contentEl = el;
  }
  bubble._contentEl.innerHTML = html;
}

// ---------- 工具调用统一面板 ----------
// 2026-06-26: 不再每个工具一个独立卡片。一轮回复内的所有工具调用收进
// 单个 <details class="tool-panel">：「🔧 工具调用 (N)」可折叠，内部每行一个工具。
// tool_start 创建/复用面板并新增一行；tool_end 回填该行结果。
let _toolPanel = null;   // 当前激活的工具面板（新气泡/新回合时重置）

function resetToolPanel() { _toolPanel = null; }

function getOrCreateToolPanel() {
  if (_toolPanel && document.body.contains(_toolPanel)) return _toolPanel;
  const panel = document.createElement('details');
  panel.className = 'tool-panel';
  // 永不自动展开：跑动的工具显示在标题行 tp-live，完成后归入折叠列表；
  // 之后的展开/收起完全由用户点击掌控
  panel.open = false;
  const summary = document.createElement('summary');
  summary.className = 'tp-summary';
  summary.innerHTML = '🔧 工具调用 <span class="tp-count">0</span><span class="tp-live"></span>';
  panel.appendChild(summary);
  panel._list = document.createElement('div');
  panel._list.className = 'tp-list';
  panel.appendChild(panel._list);
  panel._count = 0;
  msgBox.appendChild(panel);
  _toolPanel = panel;
  scrollBottom();
  return panel;
}

function addToolStart(name, argsStr, isSubagent, toolId) {
  const panel = getOrCreateToolPanel();
  panel._count++;
  panel.querySelector('.tp-count').textContent = panel._count;
  const row = document.createElement('div');
  row.className = 'tp-row tp-running';
  const icon = isSubagent ? '🤖' : '🔧';
  row.innerHTML = `<span class="tp-icon">${icon}</span>
    <span class="tp-name">${escapeHtml(String(name || ''))}</span>
    <span class="tp-args">${escapeHtml(String(argsStr || ''))}</span>
    <span class="tp-result">⏳ 执行中…</span>`;
  row._done = false;
  row._toolId = toolId || null;
  row._subText = '';  // 子agent 流式 token 累积
  panel._list.appendChild(row);
  // 面板展开时内滚列表跟随新行（折叠态进度由标题行 tp-live 显示）
  if (panel.open) panel._list.scrollTop = panel._list.scrollHeight;
  // 折叠态下的可见性：当前正在跑的工具显示在标题行
  const live = panel.querySelector('.tp-live');
  if (live) live.textContent = `⏳ ${String(name || '')} ${String(argsStr || '').slice(0, 60)}`;
  scrollBottom();
}

function renderMemoryPanel(msgBox, d) {
  // 记忆检索诊断面板（CLI 同等信息密度）：query + 每条命中的三分量依据
  const hits = d.hits || [];
  const q = String(d.query || '');
  const qShort = q.length > 24 ? q.slice(0, 24) + '…' : q;
  const panel = document.createElement('details');
  panel.className = 'tool-panel mem-panel';
  panel.open = false;  // 默认折叠：标题行已带命中数与 query，想看再展开
  const summary = document.createElement('summary');
  summary.className = 'tp-summary';
  summary.innerHTML = `🧠 记忆检索 <span class="tp-count">${d.hit_count ?? 0}/${d.raw_count ?? 0}</span>` +
    (q ? ` <span class="tp-args">${escapeHtml(qShort)}</span>` : '');
  panel.appendChild(summary);
  const list = document.createElement('div');
  list.className = 'tp-list';
  if (!hits.length) {
    list.innerHTML = '<div class="tp-row">⚪ 无命中记忆（本次对话不注入长期记忆）</div>';
  } else {
    list.innerHTML = hits.map((h, i) => {
      const mem = String(h.memory || '');
      const short = mem.length > 60 ? mem.slice(0, 60) + '…' : mem;
      const dt = h.detail || {};
      const basis = dt.weak
        ? '弱召回 · 按新近度'
        : `语义 ${Number(dt.vec ?? 0).toFixed(2)} · 词 ${Number(dt.fts ?? 0).toFixed(2)} · 近期 ${Number(dt.recency ?? 0).toFixed(2)}`;
      return `<div class="tp-row"><span class="tp-icon">🧠</span>` +
        `<span class="tp-name">${i + 1}. ${escapeHtml(short)}</span>` +
        `<span class="tp-result">score ${Number(h.score || 0).toFixed(2)}｜${basis}</span></div>`;
    }).join('');
  }
  panel.appendChild(list);
  msgBox.appendChild(panel);
  scrollBottom();
}

// P0-3：全字符集转义（对齐 memory.html:167 的版本）。`"`/`'` 必须转——
// loadSidebarSessions 把会话名/sid 放进 title="..."/data-*="..." 属性上下文，
// 只转 & < > 时属性值里的双引号可闭合属性注入 onfocus= 等事件处理器
// （零交互执行）。文本上下文多转义引号无害（浏览器解析回原字符，显示不变）。
// 2026-09-06: showApproval 的 details 已改为纯 DOM 构造（不经 HTML 解析器），
// 不再是本函数的消费方；剩余调用点见 addToolStart/loadSidebarSessions/renderMemoryPanel。
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function addToolEnd(name, result, toolId) {
  const panel = _toolPanel;
  if (!panel) return;
  const rows = panel._list.querySelectorAll('.tp-row.tp-running');
  for (const row of rows) {
    // M4: 优先 tool_id 配对（并发同名工具调用结果不再张冠李戴），无 id 回退按名字
    const idMatch = toolId && row._toolId === toolId;
    const nameMatch = !toolId && row.querySelector('.tp-name').textContent === name;
    if (idMatch || nameMatch) {
      row._done = true;
      row.classList.remove('tp-running');
      row.classList.add('tp-done');
      const resEl = row.querySelector('.tp-result');
      // 长结果折叠为可点开的详情，展开时独占一整行（不再挤在 240px
      // 的结果列里），再点一次收起
      if (result.length > 80) {
        resEl.innerHTML = '';
        const summary = document.createElement('span');
        summary.textContent = '✅ 完成(' + result.length + '字)';
        summary.style.cursor = 'pointer';
        summary.onclick = () => toggleToolDetail(row, result);
        resEl.appendChild(summary);
      } else {
        resEl.textContent = '✅ ' + result;
      }
      break;
    }
  }
  // 无正在跑的工具时，标题行状态收尾
  const live = panel.querySelector('.tp-live');
  if (live && !panel._list.querySelector('.tp-row.tp-running')) {
    live.textContent = `✅ 本轮 ${panel._count} 次调用已完成`;
  }
  scrollBottom();
}

// 审批结果以工具行格式落进工具面板：批准 → 原行保持执行中（等 tool_end
// 收尾）；拒绝 → 原行就地完结（此后不会有 tool_end）。找不到在跑的行
// （页面刷新重放等）时补一行决策记录，保证审批痕迹始终在列表里。
function recordApprovalResult(d) {
  const approved = d.decision === 'approve';
  const name = String(d.tool_name || '');
  let row = null;
  if (_toolPanel && document.body.contains(_toolPanel)) {
    for (const r of _toolPanel._list.querySelectorAll('.tp-row.tp-running')) {
      if (!name || r.querySelector('.tp-name').textContent === name) { row = r; break; }
    }
  }
  if (approved) {
    if (row) {
      const resEl = row.querySelector('.tp-result');
      if (resEl) resEl.textContent = '🔓 已批准，执行中…';
    }
    // 无在跑行：批准后 tool_start/tool_end 会按正常事件流落行，这里不造
    return;
  }
  const reason = String(d.reason || '');
  const text = '❌ 已拒绝' + (reason ? '：' + reason : '');
  if (row) {
    row._done = true;
    row.classList.remove('tp-running');
    row.classList.add('tp-done', 'tp-rejected');
    const resEl = row.querySelector('.tp-result');
    if (resEl) resEl.textContent = text;
  } else {
    const panel = getOrCreateToolPanel();
    panel._count++;
    panel.querySelector('.tp-count').textContent = panel._count;
    const r2 = document.createElement('div');
    r2.className = 'tp-row tp-done tp-rejected';
    r2.innerHTML = `<span class="tp-icon">🔒</span>
      <span class="tp-name">${escapeHtml(name || '审批')}</span>
      <span class="tp-args"></span>
      <span class="tp-result">${escapeHtml(text)}</span>`;
    panel._list.appendChild(r2);
  }
  const live = _toolPanel && _toolPanel.querySelector('.tp-live');
  if (live && !_toolPanel._list.querySelector('.tp-row.tp-running')) {
    live.textContent = `✅ 本轮 ${_toolPanel._count} 次调用已完成`;
  }
  scrollBottom();
}

function toggleToolDetail(row, result) {  const list = row.parentNode;
  if (row._detailPre) {           // 再点一次：收起
    row._detailPre.remove();
    row._detailPre = null;
    return;
  }
  const pre = document.createElement('pre');
  pre.className = 'tp-result-detail tp-detail-row';
  pre.textContent = result;
  row.after(pre);
  row._detailPre = pre;
}

function updateSessionInfo(sid) {
  if (sessionInfo) sessionInfo.textContent = '会话: ' + (sid || '—');
  // 顶栏徽章：真实会话显示 #sid（草稿态由 startDraftSession/_setSessBadgeDraft 覆盖）
  const badge = document.getElementById('sess-badge');
  if (badge && sid) {
    badge.hidden = false;
    badge.textContent = '#' + sid;
    badge.title = '当前会话 ' + sid + '（侧栏可重命名/删除）';
  }
}

function parseSSE(chunk) {
  const lines = chunk.split('\n');
  let event = 'message', data = {};
  for (const ln of lines) {
    if (ln.startsWith('event: ')) event = ln.slice(7);
    else if (ln.startsWith('data: ')) {
      try { data = JSON.parse(ln.slice(6)); } catch (e) {}
    }
  }
  return { event, data };
}

// ---------- 流式对话 ----------
// isResume=true 时（HITL 审批恢复）不预先创建气泡：resume 流可能只回放工具
// 事件而无新 AI 回复。但一旦出现 token/complete，说明 LLM 在生成新一轮回复，
// 这时必须懒创建气泡，否则回复会被丢弃（bug：审批后"没有下一轮答复"）。
// ---------- 文件树自动刷新（写类工具落盘后） ----------
// 一个 turn 常连写多个文件：600ms 防抖合并成一次树刷新。
const TREE_REFRESH_TOOLS = new Set(['bash']);
let _treeRefreshTimer = null;
function scheduleTreeRefresh(toolName) {
  if (!TREE_REFRESH_TOOLS.has(toolName)) return;
  if (_treeRefreshTimer) clearTimeout(_treeRefreshTimer);
  _treeRefreshTimer = setTimeout(() => {
    _treeRefreshTimer = null;
    document.dispatchEvent(new CustomEvent('hermes:ftree-refresh'));
    // agent 可能 commit/checkout 过 → Git 面板同点刷新
    document.dispatchEvent(new CustomEvent('hermes:git-refresh'));
  }, 600);
}

// 返回值（submitApproval 消费）：false = 本次请求终端失败（HTTP 非 2xx /
// 网络异常——审批决策未被 worker 接受，可安全重发）；true = 成功完结、
// busy 自动重试仍在途、用户主动停止、或 resume 流程内的 worker 级错误
// （决策已受理，重发会导致重复执行）。
async function streamMessage(endpoint, body, isResume, _retry = 0, _reuseBubble = null) {
  // busy 重试复用上一轮的占位气泡（isConnected 防 msgBox 已被整体清场的
  // 极端态）——否则每轮重试 newAssistantBubble 各开一个气泡，堆出一串
  // 「排队等待(N/8)」残影
  let bubble = (_reuseBubble && _reuseBubble.isConnected) ? _reuseBubble
    : (isResume ? null : newAssistantBubble());
  const ensureBubble = () => { if (!bubble) bubble = newAssistantBubble(); return bubble; };
  let text = '';
  let reasoningText = '';  // 2026-06-26: 主 Agent 推理内容累积
  // 本 ReAct 轮是否已在首个 tool_start 处封口。一轮可能并发多个 tool_start，
  // 只在第一次封口（加 ---），避免平行工具之间插出空分隔。
  let roundClosed = false;
  // P3-10：busy 自动重试的 2s 等待窗保持流锁（isStreaming=true、发送按钮
  // 禁用）直到重试流真正建立——否则 finally 的 setStreaming(false) 会在这
  // 2s 间隙放开发送，用户可双发消息抢同一 worker 槽。
  let keepLock = false;
  // 流式渲染节流：SSE chunk 常几十个一组到达，逐 token 渲染是卡顿源。
  // 基础 50ms 合帧刷新；渲染走块级增量（streamContent/streamReasoning：
  // 已闭合块缓存追加 + 仅重渲尾块，光标 ▌ 只在尾块），
  // complete/异常路径强制终渲。
  // P4-3 自适应降频：单次渲染（含 scrollBottom 的强制重排）实测超过单帧预算
  // （60Hz 两帧 ≈32ms）→ 下次间隔翻倍（上限 400ms），把偶发长帧（长闭合块
  // 首渲、长围栏定型等）挡在可交互线外；渲染轻松则逐级减半回落到 50ms 跟手。
  // 只影响下一次调度，「有 dirty 才渲」的语义不变。
  const FLUSH_MS = 50;
  const FLUSH_MAX_MS = 400;
  const FLUSH_SLOW_MS = 32;
  let _flushMs = FLUSH_MS;
  let _dirtyText = false, _dirtyReasoning = false, _renderTimer = null;
    const flushRender = () => {
      _renderTimer = null;
      const dirtyT = _dirtyText, dirtyR = _dirtyReasoning;
      _dirtyText = _dirtyReasoning = false;
      if (!dirtyT && !dirtyR) return;
      const t0 = performance.now();
      const b = ensureBubble();
      if (dirtyR) streamReasoning(b, reasoningText, true);
      if (dirtyT) {
        streamContent(b, text, true);
        // 正文真正开始输出 → 推理过程自动折叠（一次性；
        // 事后用户手动展开不会被再次收起）
        if (text.trim() && !b._reasoningFolded && b._reasoningBlock) {
          b._reasoningFolded = true;
          b._reasoningBlock.open = false;
        }
      }
      scrollBottom();
      const cost = performance.now() - t0;
      if (cost > FLUSH_SLOW_MS) _flushMs = Math.min(_flushMs * 2, FLUSH_MAX_MS);
      else if (_flushMs > FLUSH_MS) _flushMs = Math.max(FLUSH_MS, Math.floor(_flushMs / 2));
    };
  const scheduleRender = () => {
    if (_renderTimer !== null) return;
    _renderTimer = setTimeout(flushRender, _flushMs);
  };
  const cancelRender = () => {
    if (_renderTimer !== null) { clearTimeout(_renderTimer); _renderTimer = null; }
  };
  resetToolPanel();  // 每次新流开始，重置工具面板（上一轮的面板已固定）
  setStreaming(true);
  _currentAbort = new AbortController();
  try {
    const resp = await fetch(endpoint, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: _currentAbort.signal,
    });
    if (!resp.ok) {
      // H3: 非 2xx（503=worker 忙 / 401=登录失效 / 500）——SSE 体里没有事件，
      // 不检查就会静默空气泡。读 JSON 错误体展示。
      let detail = '';
      try { const j = await resp.json(); detail = j.detail || j.message || ''; } catch (e) {}
      renderContent(ensureBubble(), `<p style="color:#e8484c">⚠️ ${escapeHtml(detail || ('请求失败（HTTP ' + resp.status + '）'))}</p>`);
      return false;   // 请求从未被受理（审批重发安全）
    }
    _currentReader = resp.body.getReader();
    const reader = _currentReader;
    const dec = new TextDecoder();
    let buf = '';
    // 无帧看门狗：后端泵对 worker 静默 300s 必发 error/收流（DEFAULT_TIMEOUT），
    // 新服务还有 15s 心跳——正常连接不可能 330s 无任何帧。超时 = 连接已死
    // （代理半开/掐断），此时 reader.read() 会永久挂起、finally 不执行、
    // 流锁卡死（页面停在"生成中"，后续消息发不出去）。主动断开 + 通知
    // 服务端取消（best-effort）+ 释放流锁，用户可立即重发。
    const FRAME_WATCHDOG_MS = 330000;
    let _watchdogTimer = null;
    const readWithWatchdog = () => {
      const p = reader.read();
      p.catch(() => {});   // 看门狗先到时压掉败者的 AbortError 未处理告警
      return Promise.race([
        p,
        new Promise((_r, rej) => {
          _watchdogTimer = setTimeout(() => rej(new Error('watchdog')), FRAME_WATCHDOG_MS);
        }),
      ]);
    };
    while (true) {
      let chunk;
      try {
        chunk = await readWithWatchdog();
      } catch (e) {
        if (_watchdogTimer) { clearTimeout(_watchdogTimer); _watchdogTimer = null; }
        if (e && e.message === 'watchdog') {
          try {
            await fetch('/api/chat/stop', {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ session_id: currentSessionId || '' }),
            });
          } catch (_e) {}
          try { _currentAbort.abort(); } catch (_e) {}
          cancelRender();
          renderContent(ensureBubble(),
            '<p style="color:#e8484c">⚠️ 连接长时间无响应，已自动断开（会话已保存）。请重发消息。</p>');
          return false;
        }
        throw e;
      } finally {
        if (_watchdogTimer) { clearTimeout(_watchdogTimer); _watchdogTimer = null; }
      }
      const { done, value } = chunk;
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const parts = buf.split('\n\n');
      buf = parts.pop();
      for (const part of parts) {
        const evt = parseSSE(part);
        if (!evt) continue;
        if (evt.event === 'reasoning_token') {
          // 2026-06-26: 主 Agent 推理（开 🧠 开关时）
          roundClosed = false;
          reasoningText += evt.data.content || '';
          _dirtyReasoning = true;
          scheduleRender();
        } else if (evt.event === 'token') {
          roundClosed = false;
          text += evt.data.content;
          _dirtyText = true;
          scheduleRender();
          } else if (evt.event === 'tool_start') {
            // 新一轮助手分段：推理与正文都跨轮累计，轮与轮之间加分隔线。
            // 旧逻辑把正文缓冲清空，导致多轮 tool 只看得到最后一轮正文。
            cancelRender();
            if (!roundClosed) {
              roundClosed = true;
              if (reasoningText) {
                reasoningText += '\n\n---\n\n';
                _dirtyReasoning = true;
              }
              if (text && text.trim()) {
                text = text.replace(/\s+$/, '') + '\n\n---\n\n';
                _dirtyText = true;
              }
              const b = ensureBubble();
              if (_dirtyReasoning) streamReasoning(b, reasoningText, false);
              if (_dirtyText) streamContent(b, text, false);
              _dirtyText = _dirtyReasoning = false;
            }
            addToolStart(evt.data.tool_name, JSON.stringify(evt.data.tool_args || {}),
                         evt.data.tool_name === 'task', evt.data.tool_id);
        } else if (evt.event === 'tool_end') {
          addToolEnd(evt.data.tool_name, evt.data.result || '', evt.data.tool_id);
          scheduleTreeRefresh(evt.data.tool_name || '');
        } else if (evt.event === 'approval_result') {
          // 审批结果落进工具面板（工具行格式），不再弹独立消息气泡
          recordApprovalResult(evt.data || {});
        } else if (evt.event === 'human_approval_request') {
          showApproval(evt.data);
          return true;   // resume 产生了新审批（决策已被受理）
        } else if (evt.event === 'auto_compact') {
          // 2026-07-02: 按 token 阈值自动压缩提示。data: {compacted_count, original_count}
          const d = evt.data || {};
          showToast(`🔄 上下文已自动压缩（${d.compacted_count ?? '?'} 条历史 → 摘要，原 ${d.original_count ?? '?'} 条）`);
        } else if (evt.event === 'todos_update') {
          // 2026-07-03: write_todos 推送结构化 todo 列表 → 常驻小窗实时渲染
          renderTodoPanel(evt.data.todos || []);
        } else if (evt.event === 'error') {
          // H2: worker/LLM 级错误（超时、worker 退出、stdout 关闭）——
          // 之前完全没有分支，错误被静默吞掉、气泡悬着光标。
          cancelRender();
          // 忙 = 撞上同槽锁（典型：刚点停止收尾中，或同会话另一 UI 正在
          // 生成）。自动重试最多 8 次 × 2s（覆盖停止收尾与短 turn 排队；
          // 更长的占用仍会明确报错，不无限转圈）。
          if (evt.data.busy && _retry < 8) {
            const b = ensureBubble();
            renderContent(b, `<p style="color:var(--text-tertiary)">⏳ 当前会话正在生成，排队等待中（${_retry + 1}/8）…</p>`);
            keepLock = true;  // P3-10：2s 空窗不放开发送（重试流建立后仍持锁）
            // 重试复用同一占位气泡：排队文案在新流首帧（token/complete/
            // 错误渲染）被整体覆盖，不再每轮新开气泡堆残影
            setTimeout(() => streamMessage(endpoint, body, isResume, _retry + 1, b), 2000);
            return true;
          }
          const b = ensureBubble();
          renderContent(b, `<p style="color:#e8484c">⚠️ ${escapeHtml(evt.data.message || '未知错误')}</p>`);
        } else if (evt.event === 'memory_search') {
          // M15+: 记忆检索诊断面板（toast 只有计数，用户需要看到检索到了
          // 什么、每条凭什么命中——query + 语义/关键词/新近度三分量）
          renderMemoryPanel(msgBox, evt.data || {});
        } else if (evt.event === 'complete') {
          cancelRender();
          const b = ensureBubble();
          if (reasoningText) renderReasoning(b, reasoningText);
          // complete.content 只是末轮终答；跨轮正文以流式累积的 text 为准
          renderContent(b, renderMarkdown(text || evt.data.content || ''));
          enhanceCodeBlocks(b._contentEl || b);
          // 首条消息完结：会话键正常在 send() 时已落（在途可发现），此处
          // 幂等补一次——兜住 /reset 新建会话等未走 send() 的路径
          if (currentSessionId) rememberCurrentSession(currentSessionId);
        }
      }
    }
    // 收尾：流异常终止（无 complete）也去光标 + 渲染已积累内容
    cancelRender();
    const b = ensureBubble();
    if (reasoningText) renderReasoning(b, reasoningText);
    // 增量渲染器在 = 屏上有带光标的流式内容（含 text 为空的纯光标态），
    // 整文终渲一次去除光标；否则气泡本就空着，不动
    if (text || b._contentRenderer) {
      renderContent(b, renderMarkdown(text));
    }
  } catch (e) {
    const userAborted = !!(e && e.name === 'AbortError');
    if (bubble) {
      // 用户点停止（本地 abort）显示"已停止"，与真正的连接中断区分开
      renderContent(bubble, userAborted
        ? `<p style="color:var(--text-tertiary)">⏹️ 已停止</p>`
        : `<p style="color:#e8484c">⚠️ 连接中断</p>`);
    }
    // 用户主动停止 ≠ 请求失败；真网络异常（请求未送达）才算（审批重发安全）
    return userAborted;
  } finally {
    // P3-10：keepLock=true 时保持流锁（busy 重试等待期），重试调用自己的
    // setStreaming(true) 接棒；重试耗尽/正常结束路径 keepLock=false 照常解锁
    setStreaming(keepLock);
    _currentReader = null;
    _currentAbort = null;
  }
  scrollBottom();
  // 流结束后刷新侧边栏(新会话落盘 / 时间戳更新 → 排序变化)
  loadSidebarSessions();
  return true;
}

// ---------- HITL 审批 ----------
// 2026-09-06: 审批详情可见化。此前 details 只塞进 title 属性（悬停才可见，
// 用户实测"看不到要执行什么"），现改为 action 行下方直接渲染全文——等宽
// pre-wrap 块，超过约 3 行折叠为「展开」可点开。
// P0 XSS 纪律（不得回归）：details/action 是 LLM 可控内容，本函数一律
// createElement/textContent/属性赋值构造，零 innerHTML 拼接、零 HTML 插值
// （title 走 DOM 属性赋值，同样不经 HTML 解析器）。按钮/拒绝原因框的 id
// 与提交流程保持不变（submitApproval 按 id 查找，逻辑未动）。
// _pendingApprovalInfo：当前待审批面板的原始 info——submitApproval 瞬时
// 失败（请求未送达 worker）时用它原样重渲染面板。仅 showApproval 覆写，
// 不手动清空：成功路径无需它，新审批到达时自然换代。
let _pendingApprovalInfo = null;
function showApproval(info) {
  // 记录原始 info：submitApproval 瞬时失败时用同一份重渲染审批面板，
  // 用户不必刷新页面就能重试提交（thread_id 等完整保留）
  _pendingApprovalInfo = info;
  const action = String(info.action || '');
  const details = String(info.details || '');

  const box = document.createElement('div');
  box.className = 'approval-box';

  // 第一行：审批对象（title 悬停看全文——保留）
  const head = document.createElement('div');
  head.className = 'ap-head';
  const ask = document.createElement('span');
  ask.className = 'ap-ask';
  ask.textContent = '🔒 审批 ' + action;
  if (details) ask.title = details;
  head.appendChild(ask);
  box.appendChild(head);

  // 第二行：details 全文（长内容折叠，展开态内部滚动对齐 .tp-result-detail）
  let detailsPre = null;
  if (details) {
    const wrap = document.createElement('div');
    wrap.className = 'ap-details-wrap';
    detailsPre = document.createElement('pre');
    detailsPre.className = 'ap-details';
    detailsPre.textContent = details;
    wrap.appendChild(detailsPre);
    box.appendChild(wrap);
  }

  // 第三行：拒绝原因（可选）+ 同意/拒绝（id 不变——submitApproval 依赖）
  const foot = document.createElement('div');
  foot.className = 'ap-foot';
  const reasonInput = document.createElement('input');
  reasonInput.type = 'text';
  reasonInput.id = 'reject-reason';
  reasonInput.className = 'ap-reason';
  reasonInput.placeholder = '拒绝原因（可选）';
  const okBtn = document.createElement('button');
  okBtn.id = 'approve-btn';
  okBtn.className = 'btn-primary';
  okBtn.textContent = '同意';
  const noBtn = document.createElement('button');
  noBtn.id = 'reject-btn';
  noBtn.className = 'danger';
  noBtn.textContent = '拒绝';
  foot.append(reasonInput, okBtn, noBtn);
  box.appendChild(foot);

  approvalArea.innerHTML = '';
  approvalArea.appendChild(box);

  // 折叠判定：上树后实测溢出——仅当内容真被截断才提供「展开」，
  // 短详情自然全显、不出多余按钮
  if (detailsPre && detailsPre.scrollHeight > detailsPre.clientHeight + 1) {
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'ap-toggle';
    toggle.textContent = '展开 ▾';
    toggle.addEventListener('click', () => {
      const open = detailsPre.classList.toggle('ap-expanded');
      toggle.textContent = open ? '收起 ▴' : '展开 ▾';
    });
    detailsPre.parentNode.appendChild(toggle);
  }

  document.getElementById('approve-btn').onclick = () =>
    submitApproval(info.thread_id, 'approve', '');
  document.getElementById('reject-btn').onclick = () => {
    const reason = document.getElementById('reject-reason').value;
    submitApproval(info.thread_id, 'reject', reason);
  };
}

async function submitApproval(threadId, decision, reason) {
  approvalArea.innerHTML = '<p style="color:var(--text-tertiary)">处理中…</p>';
  const ok = await streamMessage('/api/chat/approve',
    { thread_id: threadId, decision, reason }, true);
  // 瞬时失败（网络异常 / HTTP 非 2xx——决策未被 worker 受理）：用原 info
  // 重渲染审批面板，用户可直接重试。此前面板永久消失，只能刷新页面。
  // thread_id 匹配守卫：失败流期间若已出现别的新审批（_pendingApprovalInfo
  // 被覆写），不得用旧 info 盖掉它。
  if (ok === false && _pendingApprovalInfo &&
      _pendingApprovalInfo.thread_id === threadId) {
    showApproval(_pendingApprovalInfo);
    return;
  }
  // 2026-07-08: 只有 resume 流没触发新的审批框时才清空。
  // 如果 resume 后 LLM 又调了 destructive 工具，streamMessage 内部会调
  // showApproval 往 approvalArea 塞新审批框——此时不能清空，否则用户看不到。
  if (!approvalArea.querySelector('.approval-box')) {
    approvalArea.innerHTML = '';
  }
}

// ---------- 发送（含指令拦截） ----------
function send() {
  // 恢复窗口守卫：/current 瞬时失败且会话 id 尚未恢复期间禁止发送——
  // 此时发消息会以空 sid 路由（归一到共享 main 槽），与在途会话失联。
  // sendBtn.disabled 已挡常规点击，这里再守 Enter/建议气泡路径。
  if (_needsSessionRecovery && !currentSessionId) {
    showToast('会话恢复中，请稍候…');
    return;
  }
  const text = input.value.trim();
  // 有图片时允许空文本（直接发图让模型看），否则要求非空
  if (!text && pendingImages.length === 0) return;
  input.value = '';
  followBottom = true;   // 用户发出新消息：必须看到自己的气泡与回复开头

  // 指令拦截：/xxx 走本地处理，不发给 agent（指令不附图片）
  if (text.startsWith('/')) {
    const parts = text.split(/\s+/);
    const cmd = parts[0].toLowerCase();
    const handler = COMMANDS[cmd];
    addUserMsg(text);
    if (handler) {
      handler.action(parts.slice(1).join(' '));
    } else {
      addAssistantMsg(`❌ 未知指令：\`${cmd}\`\n\n输入 **/help** 查看可用指令。`);
    }
    return;
  }

  // 普通消息：流式发给 agent（带推理开关 + 图片 id 列表）
  const sentImages = pendingImages.slice();
  const imageIds = sentImages.map(p => p.id);
  addUserMsg(text, sentImages);
  pendingImages = [];
  renderImagePreview();
  // 首条消息在途可发现（2026-09-05）：发请求的同时立即落本标签页会话键
  // （不再等 complete）——发送后切页马上切回，零锁快路径能直接从
  // sessionStorage 取 sid 直读事件库，发现链闭合。此刻 id 已真实使用
  // （消息在途），不存在"未使用的预生成 id 白 spawn 槽"问题；后续若请求
  // 失败无事件落库，快路径会因事件为空让位，慢路径照常兜底。
  if (currentSessionId) rememberCurrentSession(currentSessionId);
  updateSessionInfo(currentSessionId);   // 徽章从“📝 草稿”切换为“#sid”
  streamMessage('/api/chat/stream',
    { message: text || '请描述这张图片', thinking: mainThinking, subagent_thinking: subThinking, images: imageIds, session_id: currentSessionId || '', waker: (typeof currentWaker !== 'undefined' ? currentWaker : '') || '' },
    false);
}

// 发送按钮
sendBtn.onclick = () => {
  if (!isStreaming) send();
};
// 回车发送，Shift+Enter 换行（textarea 默认行为）
input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    if (!isStreaming) send();
  }
});

// ---------- 页面加载：按当前项目回显会话历史（ADR-0005 会话跟项目） ----------
let currentSessionId = null;  // 当前会话 id，供侧边栏高亮（须先于调用处声明）
// 页面初始化加载「瞬时失败」标记（worker 忙 5xx / 网络异常）。置位后由
// pollBackgroundGeneration 的恢复轮询在 5s 间隔上重试会话恢复，而不是
// 永久停留在「开始对话」空态（修复：生成中切页再返回接不上的缺陷）。
let _needsSessionRecovery = false;

// 恢复窗口输入锁（2026-09-05）：_needsSessionRecovery 且 currentSessionId
// 为空期间禁用发送（按钮 disabled + send() 守卫双保险）——此刻发消息会以
// 空 sid 路由到共享 main 槽，与在途会话失联。恢复轮询收敛（成功恢复 /
// 在途亮条 / 空白新会话）后解锁。解锁分支不得覆盖其他禁用来源：流式锁
// （isStreaming）、在途占位（_pendingTurnActive）、后台生成提示条。
function syncRecoveryInputLock() {
  if (_needsSessionRecovery && !currentSessionId) {
    sendBtn.disabled = true;
  } else if (!isStreaming && !_pendingTurnActive &&
             !(bgBanner && bgBanner.style.display === 'flex')) {
    sendBtn.disabled = false;
  }
}

// 当前激活项目：服务端渲染进 <meta>（切换项目走整页跳转，值天然新鲜）。
// 免项目 / 收件箱形态下为 "inbox"。
const ACTIVE_PROJECT = (document.querySelector('meta[name="active-project"]') || {}).content || 'inbox';
const sessionKey = () => 'hermes_current_session:' + ACTIVE_PROJECT;

// 归属匹配：inbox 激活时空绑定（""）与 "inbox" 同义（与后端 list_sessions 哨兵一致）
function projectMatches(bound) {
  if (ACTIVE_PROJECT === 'inbox') return !bound || bound === 'inbox';
  return bound === ACTIVE_PROJECT;
}

// 恢复放行规则：有归属且匹配 → 放行；从未落盘的空白会话（无快照、无历史）
// 也放行——它没有归属，首条消息保存时就会绑定当前项目。
function restoreAllowed(d) {
  const bound = d.project || '';
  if (!bound && !(d.history && d.history.length)) return true;
  return projectMatches(bound);
}

function rememberCurrentSession(sid) {
  currentSessionId = sid || null;
  try {
    if (sid) sessionStorage.setItem(sessionKey(), sid);
    else sessionStorage.removeItem(sessionKey());
  } catch (e) {}
}

// 生成本标签页专属会话 id（8 位十六进制，与既有桶 sid 同形；后端
// validate_id 只挡路径形态，小写十六进制天然合法）。
// 修复：空白新会话此前以 session_id='' 发首条消息，后端把空 id 归一到
// 共享 main 槽——多标签互相碰撞、停止按钮无法只停自己、"会话: —"无锚点。
// 每个 tab 预生成自己的 id 后三者同时成立；仅在内存生效，send() 发出
// 首条消息时即落 sessionStorage（id 已真实使用，见 send 内注释）——刷新
// /切页回来零锁快路径据此直读事件库，在途轮次立即可发现。
function newTabSessionId() {
  const buf = new Uint8Array(4);
  crypto.getRandomValues(buf);
  return Array.from(buf, b => b.toString(16).padStart(2, '0')).join('');
}

// ---------- 草稿会话（2026-09）：进页面即是草稿，首条消息发出才建会话 ----------
// 语义：新建（＋）/重置（/reset）/删当前会话都只在本地“换纸”——清空消息区
// + 生成新内存 sid + 清标签页会话键，不再调 POST /api/sessions/reset。
// 旧实现点“＋”就建会话：worker IPC + 槽 current 切换，worker 忙时还 503。
// 新实现零后端调用；首条消息发出时后端 _prelog_turn 建 stub，会话才真正诞生
// （id 此刻才首次落盘/落 storage）。刷新页面草稿自然消散（草稿易失语义）。
function startDraftSession() {
  const oldSid = currentSessionId;
  abortCurrentStream();          // 先停旧流，再用旧 sid 释放槽
  currentSessionId = newTabSessionId();
  try { sessionStorage.removeItem(sessionKey()); } catch (e) {}
  if (oldSid) {
    fetch('/api/sessions/' + encodeURIComponent(oldSid) + '/release-worker',
          { method: 'POST' }).catch(() => {});
  }
  msgBox.innerHTML = '';
  resetToolPanel();
  followBottom = true;   // 新草稿空页面落在顶部即底部
  const todoPanel = document.getElementById('todo-panel');
  if (todoPanel) todoPanel.style.display = 'none';
  refreshEmptyState();
  updateSessionInfo(currentSessionId);
  _setSessBadgeDraft();          // 草稿态徽章（盖掉 updateSessionInfo 的 sid 显示）
  loadSidebarSessions();
}

// 顶栏会话状态徽章（base.html 挂载点，仅 chat 页被 chat.js 填充）：
// 草稿显示“📝 草稿”，真实会话显示“# sid”。
function _setSessBadgeDraft() {
  const badge = document.getElementById('sess-badge');
  if (!badge) return;
  badge.hidden = false;
  badge.textContent = '📝 草稿';
  badge.title = '草稿态：发送首条消息后创建会话并按首句自动命名';
}

// 历史渲染（恢复/回显共用）：把 /current 返回的 history 画进消息区。
// 2026-09-05: 渲染前先清场（重入幂等）——零锁快路径与 /current 慢路径会
// 先后两次渲染同一消息区，恢复轮询的 loadCurrentSession 重入也依赖它。
// 空历史同样清场：恢复到空会话时若提前 return，上一会话的内容会残留在
// 消息区，两会话内容交叠。
function renderHistory(history) {
  msgBox.innerHTML = '';
  resetToolPanel();
  refreshEmptyState();
  followBottom = true;   // 会话重绘/切换/加载语义上应落在底部（强制例外点）
  if (!history || !history.length) return;
  let bubble = null;
  for (const h of history) {
    if (h.role === 'user') {
      bubble = null;
      resetToolPanel();  // 新一轮：旧面板留在 DOM（对齐 live 每轮一块）
      // 历史多模态消息：图片 URL 可能已失效，用 🖼️ 占位标记
      if (h.has_images) {
        addUserMsg((h.content || '') + '\n\n*🖼️ [图片]*', null);
      } else {
        addUserMsg(h.content);
      }
    } else if (h.role === 'tool') {
      const body = typeof h.content === 'string' ? h.content : JSON.stringify(h.content);
      addToolEnd(h.name || '', body, h.tool_call_id || null);
    } else if (h.role === 'system') {
      continue;  // compact 摘要不进对话气泡
    } else if (h.llm_error) {
      // LLM 调用失败留底：独立红色警示气泡（对齐 live error 渲染样式）。
      // 此前错误只随 complete 事件显示一次，切页回来就只剩用户消息。
      bubble = newAssistantBubble();
      renderContent(bubble,
        `<p style="color:#e8484c">${escapeHtml(h.content || '⚠️ LLM 调用失败')}</p>`);
    } else {
      // assistant:可能含 tool_calls / reasoning / content
      // 先画气泡再挂工具面板，顺序对齐 live（气泡在上、本轮工具在下）
      const hasContent = h.content && h.content.trim();
      const hasTools = !!(h.tool_calls && h.tool_calls.length);
      if (h.reasoning || hasContent || hasTools) {
        if (!bubble) bubble = newAssistantBubble();
        // 2026-09-05: 走块级渲染器（一次全量 = 全部块首渲）——历史回显与
        // 流式渲染同构；末尾气泡的渲染器与块缓存保留在气泡上，作为在途
        // 轮次 EventSource 续流的种子（补差重放的前缀零重渲）。
        // 同一请求多轮 assistant（夹 tool）共用气泡：推理/正文都按轮用 ---
        // 拼接，避免只留下最后一轮。
        if (h.reasoning) {
          bubble._accReasoning = bubble._accReasoning
            ? bubble._accReasoning + '\n\n---\n\n' + h.reasoning
            : h.reasoning;
          streamReasoning(bubble, bubble._accReasoning, false);
        }
        if (hasContent) {
          bubble._accContent = bubble._accContent
            ? bubble._accContent + '\n\n---\n\n' + h.content
            : h.content;
          streamContent(bubble, bubble._accContent, false);
          // 与生成时一致：有正文的历史消息，推理默认收起（可手动展开）
          if (bubble._reasoningBlock) {
            bubble._reasoningBlock.open = false;
            bubble._reasoningFolded = true;
          }
        }
        if (hasTools) {
          for (const tc of h.tool_calls) {
            addToolStart(
              tc.name, JSON.stringify(tc.args || {}),
              tc.name === 'task', tc.id || null,
            );
          }
        }
      }
    }
  }
  refreshEmptyState();
  return bubble;   // 末尾 assistant 气泡（在途轮次续流的种子气泡，无则 null）
}

// ---------- 零锁快路径：事件库直读 → 历史投影（2026-09-05） ----------
// 用户痛点：发送后切页再切回，/current 要过 worker 锁，生成中阻塞约 5s
// 超时 → 5xx，页面纯空白且要等整轮生成结束才见历史。
// 修法：与 /current 并行走零 worker-IPC 路径——GET /api/sessions?project=
// （主进程直读快照）解析候选会话 + GET /api/sessions/{sid}/events（主进程
// 直读 SQLite 事件库），用事件流在内存重建消息列表立即渲染。投影语义对齐
// worker 侧 _history_messages_for_ui（derive_messages(include_reasoning=True)）
// + _serialize_messages_for_history，/current 成功后两者逐条等价可无缝校验。
// 投影只做显示用：不写 worker、不落 sessionStorage（会话键仍由 /current
// 成功后的 rememberCurrentSession 落盘）。

// 零锁路径已渲染的会话与消息数（供 /current 到达后静默校验；事件流
// append-only，sid 相同且条数相同即内容一致，可跳过重绘免闪屏）
let _fastRender = null;
// 在途轮次占位生效中（_showPendingTurn 置位 / 探测否决时复位）：期间
// 发送保持禁用（syncRecoveryInputLock 解锁条件之一）
let _pendingTurnActive = false;

// 工具结果展示截断（对齐 worker _serialize_messages_for_history 的 500 字）
function _truncToolContent(v) {
  const s = typeof v === 'string' ? v : JSON.stringify(v ?? '');
  return s.length > 500 ? s.slice(0, 500) : s;
}

// durable assistant/message 的 tool_calls 是 OpenAI 格式
// （{id, type, function:{name, arguments(JSON 字符串)}}）→ 前端渲染用
// {name, args}（对齐 worker _parse_tool_args_for_history 的归一）
function _normToolCalls(rawCalls) {
  const out = [];
  if (!Array.isArray(rawCalls)) return out;
  for (const tc of rawCalls) {
    if (!tc || typeof tc !== 'object') continue;
    const fn = tc.function || {};
    let args = fn.arguments;
    if (typeof args === 'string') { try { args = JSON.parse(args); } catch (e) {} }
    const name = fn.name || tc.name || '';
    if (typeof args === 'undefined') args = tc.args;
    out.push({
      id: tc.id || '',
      name,
      args: (args === undefined || args === null) ? {} : args,
    });
  }
  return out;
}

// 事件流 → {history, inFlight}。事件类型与 payload 结构参考
// src/agent/session_log.py：user/message(content)、assistant/message
// (content, tool_calls?, reasoning?)、tool/result(tool_call_id, content)、
// compact/applied(summary, kept_messages?)、turn/start|turn/end、
// llm/error(忽略)。悬空 tool_calls（中断/取消无结果）合成「(中断，无结果)」
// 占位 tool 消息——与 derive_messages 的投影规则一致。
function projectEventsToHistory(events) {
  const history = [];
  let pendingCallIds = [];   // 悬空 tool_call id 追踪
  let inFlight = false;      // 末尾 turn/start 无配对 turn/end → 在途轮次
  // 在途轮次已落盘尾巴（2026-09-05 EventSource 续流的补差去重基准）：
  // segments = 本轮已落盘 assistant 分段 [{content, reasoning, tools}]（tools
  // = 该分段 tool_calls 数），toolCount = 全轮 tool_calls 总数。turn/start
  // 重置、compact 重建时清空；非在途返回 null。
  let tailSegs = [];
  let tailToolCount = 0;
  const flushPending = () => {
    for (const id of pendingCallIds) {
      history.push({ role: 'tool', tool_call_id: id, content: '(中断，无结果)' });
    }
    pendingCallIds = [];
  };
  const pushUser = (content) => {
    flushPending();
    history.push({ role: 'user', content: typeof content === 'string' ? content : '' });
  };
  const pushAssistant = (content, reasoning, rawCalls) => {
    flushPending();
    const entry = { role: 'assistant', content: typeof content === 'string' ? content : '' };
    if (reasoning) entry.reasoning = reasoning;
    const calls = _normToolCalls(rawCalls);
    if (calls.length) {
      entry.tool_calls = calls;
      if (Array.isArray(rawCalls)) {
        for (const tc of rawCalls) {
          if (tc && typeof tc === 'object' && tc.id) pendingCallIds.push(tc.id);
        }
      }
    }
    history.push(entry);
  };
  for (const ev of events) {
    if (!ev || typeof ev !== 'object') continue;
    const t = ev.type || '';
    const p = (ev.payload && typeof ev.payload === 'object') ? ev.payload : {};
    if (t === 'turn/start') {
      inFlight = true;
      tailSegs = []; tailToolCount = 0;
    } else if (t === 'turn/end') {
      inFlight = false;
    } else if (t === 'user/message') {
      pushUser(p.content);
    } else if (t === 'assistant/message') {
      pushAssistant(p.content, p.reasoning, p.tool_calls);
      const nCalls = _normToolCalls(p.tool_calls).length;
      tailSegs.push({
        content: typeof p.content === 'string' ? p.content : '',
        reasoning: typeof p.reasoning === 'string' ? p.reasoning : '',
        tools: nCalls,
      });
      tailToolCount += nCalls;
    } else if (t === 'tool/result') {
      const cid = p.tool_call_id || '';
      const i = pendingCallIds.indexOf(cid);
      if (i >= 0) {
        pendingCallIds.splice(i, 1);
        history.push({ role: 'tool', tool_call_id: cid, content: _truncToolContent(p.content) });
      }
      // 孤儿 tool/result（配对的 assistant 已被 compact 截断等）→ 丢弃，
      // 与 derive_messages 一致
    } else if (t === 'compact/applied') {
      // 压缩点：之前的投影全部作废，重建保留区。kept 里的 system 条目与
      // /current 序列化一致地不进前端渲染（role 非 user/assistant/tool 丢弃）
      history.length = 0;
      pendingCallIds = [];
      tailSegs = []; tailToolCount = 0;
      const kept = Array.isArray(p.kept_messages) ? p.kept_messages : [];
      for (const m of kept) {
        if (!m || typeof m !== 'object') continue;
        if (m.role === 'user') pushUser(m.content);
        else if (m.role === 'assistant') pushAssistant(m.content, '', m.tool_calls);
        else if (m.role === 'tool') {
          history.push({ role: 'tool', tool_call_id: m.tool_call_id || '', content: _truncToolContent(m.content) });
        }
      }
    } else if (t === 'session/truncated') {
      // 编辑重发截断点（2026-09-19）：之前的投影全部作废，重建保留区。
      // 与 compact/applied 同构但无 system 摘要行；非 user/assistant/tool
      // 的保留条目与 /current 序列化一致地不进前端渲染
      history.length = 0;
      pendingCallIds = [];
      inFlight = false;
      tailSegs = []; tailToolCount = 0;
      const kept = Array.isArray(p.kept_messages) ? p.kept_messages : [];
      for (const m of kept) {
        if (!m || typeof m !== 'object') continue;
        if (m.role === 'user') pushUser(m.content);
        else if (m.role === 'assistant') pushAssistant(m.content, '', m.tool_calls);
        else if (m.role === 'tool') {
          history.push({ role: 'tool', tool_call_id: m.tool_call_id || '', content: _truncToolContent(m.content) });
        }
      }
    }
    // 其余类型（tool/call、llm/error、interrupt/*、未知）不参与投影——
    // 工具调用信息由 assistant.tool_calls 承载（与 derive_messages 一致）
  }
  flushPending();
  return {
    history,
    inFlight,
    inFlightTail: inFlight ? { segments: tailSegs, toolCount: tailToolCount } : null,
  };
}

// 零锁渲染主入口：saved 非空时**事件库直读优先**（本标签页记住的会话
// sid 直查 /api/sessions/{sid}/events）；列表请求只用于补 waker 元数据与
// saved 为空（或直读无事件）时的兜底候选。返回渲染成功的 sid（失败返回
// ''，慢路径自然兜底）。
async function _renderFromEventStore(saved) {
  // 渲染指定 sid 的事件流：有可显示内容 → 投影渲染并返回 sid；否则 ''
  const renderEvents = async (sid, waker) => {
    const er = await fetch('/api/sessions/' + encodeURIComponent(sid) + '/events');
    if (!er.ok) return '';
    const events = ((await er.json().catch(() => ({}))) || {}).events;
    if (!Array.isArray(events) || !events.length) return '';
    const { history, inFlight, inFlightTail } = projectEventsToHistory(events);
    if (!history.length && !inFlight) return '';
    // 竞态：慢路径 /current 已先完成渲染 → 快路径让位（/current 是权威）
    if (msgBox.children.length) return '';
    // 仅显示用：currentSessionId 供 active 轮询与发送路由；storage 落盘
    // 仍由慢路径成功后的 rememberCurrentSession 决定
    currentSessionId = sid;
    updateSessionInfo(sid);
    if (waker && typeof syncWakerSelect === 'function') syncWakerSelect(waker);
    const seedBubble = renderHistory(history);
    _fastRender = { sid, count: history.length };
    if (inFlight) _showPendingTurn(sid, inFlightTail, seedBubble);
    return sid;
  };
  try {
    let sid = saved || '';
    // ① 事件直读优先（saved 非空）：生成中的会话快照要等 worker 轮末
    //    _save_bucket 才落盘——项目会话列表是轮末快照，不含生成中的新会话，
    //    列表查不到 saved ≠ 会话失效（事件库从开轮起就实时有 turn/start +
    //    user/message）。先直查事件，有即渲染。**列表未命中 saved 绝不改写
    //    sid**：旧实现在此处把 saved 劫持成项目最新旧会话，currentSessionId
    //    被改写后恢复轮询被 currentSessionId 守卫永久阻断（在途轮次再也
    //    接不上），或渲染空白。
    if (sid) {
      const direct = await renderEvents(sid, '');
      if (direct) {
        // waker 元数据后补（列表请求此时只承担补元数据职责）：仍停在
        // 同一会话才同步，避免慢路径已收敛后的迟响应覆盖选择器
        fetch('/api/sessions?project=' + encodeURIComponent(ACTIVE_PROJECT))
          .then(r => r.ok ? r.json() : { sessions: [] })
          .then(({ sessions = [] }) => {
            const hit = sessions.find(s => s.session_id === sid);
            if (hit && hit.waker && currentSessionId === sid &&
                typeof syncWakerSelect === 'function') syncWakerSelect(hit.waker);
          })
          .catch(() => {});
        return direct;
      }
      // 直读失败/无事件（会话已删、事件库异常、空壳轮）→ ② 兜底。
      // 兜底候选只服务本次渲染的返回值，**不回写 sessionStorage、不覆盖
      // saved 语义**——会话键仍由慢路径成功后的 rememberCurrentSession
      // 维护，避免把「直读暂时失败」放大成「记住的会话被换掉」。
    }
    // ② 兜底候选：saved 为空或直读无事件 → 项目最新会话（与慢路径步骤②同语义）
    const lr = await fetch('/api/sessions?project=' + encodeURIComponent(ACTIVE_PROJECT));
    if (!lr.ok) return '';
    const { sessions = [] } = await lr.json();
    const latest = sessions.reduce((a, b) =>
      !a || (b.updated_at || '') > (a.updated_at || '') ? b : a, null);
    sid = latest ? (latest.session_id || '') : '';
    const waker = latest ? (latest.waker || '') : '';
    if (!sid) return '';
    return await renderEvents(sid, waker);
  } catch (e) {
    return '';
  }
}


// 慢路径（原步骤①②）：/current 走 worker 锁——生成中阻塞至超时（约 5s）
// → 5xx。网络异常归一为瞬时失败（对齐原 catch 语义）。瞬时失败不删
// storage 键、不伪造新 sid，交由恢复轮询重试。
async function _restoreViaCurrent(saved) {
  try {
    // ① 本标签页在该项目里记住的会话（M5：按 sid 路由到专属执行实例并懒水合）
    let data = null;
    if (saved) {
      const r = await fetch('/api/sessions/current?session_id=' + encodeURIComponent(saved));
      if (r.ok) {
        const d = await r.json();
        // 归属校验：别的项目的会话不恢复（同标签页切项目后旧 sid 作废）
        if (restoreAllowed(d)) data = d;
      } else if (r.status >= 500) {
        // worker 忙/异常：瞬时失败——键保留，交给恢复轮询重试
        return { transient: true, data: null };
      }
      if (!data) sessionStorage.removeItem(sessionKey());
    }

    // ② 没记住 → 自动恢复本项目最近一次会话（历史会话跟着项目走）
    if (!data) {
      const lr = await fetch('/api/sessions?project=' + encodeURIComponent(ACTIVE_PROJECT));
      if (lr.ok) {
        const { sessions = [] } = await lr.json();
        const latest = sessions.reduce((a, b) =>
          !a || (b.updated_at || '') > (a.updated_at || '') ? b : a, null);
        if (latest) {
          const r = await fetch('/api/sessions/current?session_id=' + encodeURIComponent(latest.session_id));
          if (r.ok) {
            const d = await r.json();
            if (restoreAllowed(d)) data = d;
          } else if (r.status >= 500) {
            return { transient: true, data: null };
          }
        }
      }
    }
    return { transient: false, data };
  } catch (e) {
    console.error('restoreViaCurrent error:', e);
    // 网络/未知异常同属瞬时失败
    return { transient: true, data: null };
  }
}

async function loadCurrentSession() {
  // 双路并行（2026-09-05）：
  //   慢路径 /current（worker 锁，生成中阻塞至 5s 超时）即刻发出；
  //   同时零锁快路径（会话列表 + 事件库，主进程直读）先把历史画出来。
  // 用户切页回来：历史立即可见（不再等 worker 空闲），在途轮次末尾显示
  // 生成中占位；/current 稍后成功则静默校验——同会话同条数（事件流
  // append-only，一致）忽略不重绘，不一致（worker 内存更新/快照回退的老
  // 会话无事件）以 /current 为准整体替换。无在途生成的常规路径最终 DOM
  // 与原实现完全一致，只是首屏更早。
  let transientFailure = false;
  let saved = '';
  try {
    // 旧版无命名空间键迁移一次（升级兼容），随后按项目隔离
    try {
      const legacy = sessionStorage.getItem('hermes_current_session');
      if (legacy !== null) {
        sessionStorage.removeItem('hermes_current_session');
        if (!sessionStorage.getItem(sessionKey())) sessionStorage.setItem(sessionKey(), legacy);
      }
    } catch (e) {}
    saved = sessionStorage.getItem(sessionKey()) || '';
  } catch (e) {}

  _fastRender = null;
  const slowPromise = _restoreViaCurrent(saved);
  const fastSid = await _renderFromEventStore(saved);
  const slow = await slowPromise;

  try {
    const data = slow.data;
    // ③ 恢复成功（或项目还没有任何会话 → 空白新会话）
    if (data && data.session_id) {
      updateSessionInfo(data.session_id);
      rememberCurrentSession(data.session_id);
      // 同步会话绑定的 waker（会话切换时回显选择器）
      if (typeof syncWakerSelect === 'function') {
        syncWakerSelect(data.waker || '');
      }
      // 静默校验：快路径已渲染同一会话且消息数一致 → 忽略（免重绘闪屏）；
      // 否则 /current 全量渲染（renderHistory 内部先清场）
      if (!(fastSid && _fastRender && _fastRender.sid === data.session_id &&
            _fastRender.count === (data.history || []).length)) {
        renderHistory(data.history);
      }
    } else if (slow.transient) {
      // 瞬时失败：快路径已渲染则历史仍可见；会话键恢复交给既有轮询
      // （快路径没渲染出来时 currentSessionId 为空 → 恢复轮询自愈）
      transientFailure = true;
    } else if (!fastSid) {
      // 该项目还没有任何会话 → 草稿会话（仅内存；send() 发首条消息时才落
      // storage 并由后端建会话，见 startDraftSession / send 内注释）
      startDraftSession();
    }
  } catch (e) {
    console.error('loadCurrentSession error:', e);
    transientFailure = true;
  }
  _needsSessionRecovery = transientFailure;
  syncRecoveryInputLock();   // 恢复窗口锁输入 / 收敛后解锁（项3）
  loadSidebarSessions();
  pollBackgroundGeneration();
}

loadCurrentSession();

// ---------- 输入框顶部把手（2026-09 界面改版）：向上拉 = 拉大 ----------
// textarea 原生 resize 把手位置反直觉（卡在工具行上方），已 CSS resize:none
// 禁用；改用输入容器正中间顶部的小把手，向上拖 = 拉大，向下拉 = 缩小，
// 范围 54px ~ 60vh，双击复位。pointer capture 保证拖出把手也不丢事件。
(function _initPullGrip() {
  const grip = document.getElementById('pull-grip');
  const ta = document.getElementById('msg-input');
  if (!grip || !ta) return;
  let drag = null;
  grip.addEventListener('pointerdown', e => {
    drag = { y: e.clientY, h: ta.offsetHeight };
    grip.classList.add('active');
    try { grip.setPointerCapture(e.pointerId); } catch (err) {}
    e.preventDefault();
  });
  grip.addEventListener('pointermove', e => {
    if (!drag) return;
    const max = Math.round(window.innerHeight * 0.6);
    ta.style.height = Math.max(54, Math.min(drag.h + (drag.y - e.clientY), max)) + 'px';
  });
  const end = () => { drag = null; grip.classList.remove('active'); };
  grip.addEventListener('pointerup', end);
  grip.addEventListener('pointercancel', end);
  grip.addEventListener('dblclick', () => ta.style.height = '');
})();

