// 会话恢复调度（/active 有界重试判据 + 在途轮次恢复分发 + EventSource 续流 + 后台生成探测 + 会话恢复轮询 + 提示条停止）——自 chat.js 顺延外置拆出，逐字搬移（split02，源区间 chat.js L1341-1782 与 L1892-2095，中段 _restoreViaCurrent/loadCurrentSession 仍留 chat.js）
// —— /active 探测 false 的有界重试判据（防 spawn 窗口盲区一次性放弃）——
// worker 首轮 spawn（最长 60s）与泵首帧之间 wp.streaming 恒 false，
// /api/chat/active 一次 false ≠ 无在途轮次。探测 false 时查事件库的
// **轻量端点 /api/sessions/{sid}/last_event**（主进程直读 SQLite 零锁，
// 只取最后一条事件，绝不拉全量——旧实现每轮询一次就触发一次 /events
// 全表扫描 + 逐行 json 解析，长会话上 3s×6 次把主进程拖垮）：满足其一
// 即值得重试——
//   (a) 最后一条事件距 < 20s（本轮刚开始/预写刚落盘，正处 spawn 窗口；
//       turn/start 先于本轮一切事件落盘，末条新鲜 ⟹ 开轮也新鲜）；
//   (b) 最后一条事件还停在 turn/start（本轮已开轮、尚未落任何分段——
//       生成早期常态；已见 turn/end 的死轮不满足，照旧收敛）。
// 判定与旧全量扫描同向且只宽不严：旧「距最近 turn/start < 20s」在此
// 收窄为「距最后一条事件 < 20s」（turn/start.ts ≤ 末条 ts，末条新鲜必
// 蕴含开轮新鲜）；多出的重试（如刚结束的轮）仍被 3s×6 有界收敛，
// 方向保守。调用方以 3s × ACTIVE_RETRY_MAX 有界重试，超限回落既有
// dismiss/停止逻辑（不会死循环）。事件请求失败按「不可判定」→ 不重试，
// 保守收敛。
const ACTIVE_RETRY_INTERVAL_MS = 3000;
const ACTIVE_RETRY_MAX = 6;
const ACTIVE_RETRY_WINDOW_MS = 20000;
async function _activeRetryEligible(sid) {
  try {
    const er = await fetch('/api/sessions/' + encodeURIComponent(sid) + '/last_event');
    if (!er.ok) return false;
    const ev = ((await er.json().catch(() => ({}))) || {}).last_event;
    if (!ev || typeof ev !== 'object') return false;   // 空会话/降级 → 不可判定，不重试
    if (((ev || {}).type || '') === 'turn/start') return true;
    const ts = Number(ev.ts || 0);
    return ts > 0 && (Date.now() / 1000 - ts) * 1000 < ACTIVE_RETRY_WINDOW_MS;
  } catch (e) {
    return false;
  }
}

// 在途轮次恢复：历史（用户消息 + 已落盘助手分段）已经由 renderHistory 画好。
// 不再往消息区塞「⏳ 后台生成中」空气泡（那是内部探测态，不像对话）。
// 顶栏细条 bg-gen-banner 提示在途；active=true → EventSource 续流接到
// seedBubble（已有助手气泡）或新建真实助手气泡。active=false 有界重试
// spawn 窗口，超限收条恢复输入。
function _showPendingTurn(sid, inFlightTail, seedBubble) {
  sendBtn.disabled = true;
  _pendingTurnActive = true;
  refreshEmptyState();
  if (bgBanner) bgBanner.style.display = 'flex';
  const dummy = document.createElement('div');   // 给 ES 路径 dropHint 用，不进消息区
  const hideBanner = () => {
    if (bgBanner && bgBanner.style.display !== 'none') bgBanner.style.display = 'none';
  };
  const dismiss = () => {
    hideBanner();
    _pendingTurnActive = false;
    syncRecoveryInputLock();
    refreshEmptyState();
  };
  const attachEs = () => {
    hideBanner();
    _resumeViaEventSource(sid, inFlightTail || { segments: [], toolCount: 0 },
                          seedBubble || null, dummy);
  };
  let activeRetries = 0;
  const probeActive = () => {
    fetch('/api/chat/active?session_id=' + encodeURIComponent(sid))
      .then(r => r.ok ? r.json() : { active: false })
      .then(async d => {
        if (d.active) {
          attachEs();
        } else if (activeRetries < ACTIVE_RETRY_MAX &&
                   await _activeRetryEligible(sid)) {
          activeRetries++;
          setTimeout(probeActive, ACTIVE_RETRY_INTERVAL_MS);
        } else {
          dismiss();
        }
      })
      .catch(dismiss);
  };
  // 屏上已有对话（用户消息/部分回复）→ 立刻接 ES，探测并行；
  // 空历史的开轮窗口仍先探测，避免空页空气泡。
  if (seedBubble || (msgBox && msgBox.querySelector('.msg'))) {
    attachEs();
  } else {
    probeActive();
  }
}

// ---------- EventSource 续流（2026-09-05）：切回 chat 的在途恢复升级 ----------
// 契约：GET /api/chat/stream/{sid}?after=<seq> → SSE，帧 = 既有事件类型
//（token/reasoning_token/tool_start/tool_end/human_approval_request/complete/
// error 等，worker 事件名原样透传），每帧带 id:<seq>；无在途时补差后发
// event: done 关流。恢复用简化契约：after=0 全量补差（seq 是帧号非字符数，
// 无法表达「已渲染进度」），前端做已渲染内容去重。
//
// 关键难点：总线是 per-session 环形缓冲（2000 帧，turn 终态后 TTL 5min）——
// 上一轮结束后 5 分钟内切回，after=0 的补差 = 上一轮尾巴 + 本轮全量帧。
// 状态机三分区逐帧归位：
//   ① 补差区（本轮已落盘分段）：token 与分段落盘 content 做前缀比对，
//     是前缀 → 抑制渲染（屏上已是该段终态）；tool_start/tool_end 计数
//     ≤ 已落盘 tool_calls 数 → 面板行已由历史投影渲染，跳过（只消费分段
//     边界：正文封口进 closedC + 分段推进）；reasoning 只累积不渲染。
//   ② 实时区（engage）：token 超出落盘分段（前缀成立 + 有增量）→ 覆盖式
//     渲染（块缓存命中，已渲染前缀零重渲），此后逐帧增量；tool/面板全按
//     流式语义渲染。
//   ③ 上一轮残留（foreign）：内容前缀失配证实 → 整段忽略，直到该轮终态帧。
// 终态帧（complete/error）与审批帧悬决（deferred）：后续还有数据帧 = 属上
// 一轮 → 重置状态机并回滚本连接渲染（工具行撤除、气泡内容回滚种子）；
// 后随 done（或再无数据帧）= 属本轮 → 收尾。恢复期间发送锁定
//（_pendingTurnActive → syncRecoveryInputLock），一次订阅内至多一轮生成。
//
// complete/done 收尾：关 ES + 终渲（complete 带权威 content；未 engage 则
// 屏上已是快路径终态，不重渲）+ enhanceCodeBlocks + 刷新侧栏与会话键——
// 不再 location.reload，内容已全量在手。
// 传输层 error（e.data 无值：服务重启/网络断）：关 ES 回退既有「提示条 +
// 3s 轮询 + 结束 reload」兜底；后端 error 帧（e.data 有值）按流式错误语义
// 渲染收尾。心跳为 SSE 注释（: keepalive），EventSource 不派发，无影响。
function _teardownBgPoll() {
  if (bgPollTimer) { clearInterval(bgPollTimer); bgPollTimer = null; }
  if (bgBanner && bgBanner.style.display !== 'none') bgBanner.style.display = 'none';
}

function _resumeViaEventSource(sid, tail, seedBubble, pendingEl) {
  const durableSegs = (tail && Array.isArray(tail.segments)) ? tail.segments : [];
  const durableTools = (tail && tail.toolCount) || 0;
  // 分段累计 tool_calls 数：token 补差 guard 对齐「当前正在补差的落盘分段」
  const cumTools = [];
  { let a = 0; for (const s of durableSegs) { a += (s && s.tools) || 0; cumTools.push(a); } }
  const seedJoinedC = durableSegs
    .map(s => (s && typeof s.content === 'string') ? s.content : '')
    .filter(c => c.trim())
    .join('\n\n---\n\n');
  const seedJoinedR = durableSegs
    .map(s => (s && typeof s.reasoning === 'string') ? s.reasoning : '')
    .filter(c => c.trim())
    .join('\n\n---\n\n');
  const displayContent = () => {
    const a = (closedC || '').replace(/\s+$/, '');
    const btxt = tmp || '';
    if (a && btxt) return a + '\n\n---\n\n' + btxt;
    return a || btxt;
  };

  let es = null, finished = false;
  let bubble = (seedBubble && seedBubble.isConnected) ? seedBubble : null;
  let tmp = '', rTmp = '';        // 当前分段 token / 跨段 reasoning 累积
  let closedC = '';               // 已封口的正文段（tool_start 之前各轮）
  let roundClosed = false;        // 本轮首个 tool_start 已封口（平行工具不重复 ---）
  let segIdx = 0;                 // 正在补差的落盘分段下标
  let toolStarts = 0;             // 已见 tool_start 帧数（≤ durableTools 判补差）
  let esStarts = 0, esEnds = 0;   // 本连接实际渲染的 tool 起/止数（配对，重置随回滚）
  let esRows = [];                // 本连接加的工具面板行（重置时撤）
  let panelOurs = false;          // 工具面板由本连接创建（回滚且空时移除）
  let engaged = false;            // 已进入实时区（②）
  let foreign = false;            // 上一轮残留（③）
  let deferred = null;            // 悬决帧 {type:'complete'|'error'|'approval', d}
  let renderedC = false, renderedR = false;   // 自上次重置起是否动过正文/推理
  let renderedRLen = 0;           // reasoning 已渲字符高水位（engage 起维护）
  let hintGone = false, released = false;
  let flushTimer = null, dirtyC = false, dirtyR = false;
  const seenApprovals = new Set();

  // 占位提示退场：真实增量开始渲染时移除（占位气泡本身是流式目标时保留
  // 容器——首渲会清掉 ⏳ 文案，容器留下承载流式内容）
  const dropHint = () => {
    if (hintGone) return;
    hintGone = true;
    if (!(bubble && pendingEl.contains(bubble))) { try { pendingEl.remove(); } catch (e) {} }
  };
  // 收尾解锁（只跑一次；统一锁可能同时处于其他禁用来源下，不直接放开按钮）
  const releasePending = () => {
    dropHint();
    if (released) return;
    released = true;
    _pendingTurnActive = false;
    syncRecoveryInputLock();
    refreshEmptyState();
  };
  const ensureBubble = () => {
    if (bubble && bubble.isConnected) {
      if (!bubble._contentEl) {
        bubble._contentEl = bubble.querySelector('.bubble-content');
        if (!bubble._contentEl) {
          const el = document.createElement('div');
          el.className = 'bubble-content';
          bubble.appendChild(el);
          bubble._contentEl = el;
        }
      }
      return bubble;
    }
    const pb = (pendingEl && pendingEl.isConnected) ? pendingEl.querySelector('.bubble') : null;
    if (pb) { bubble = pb; return ensureBubble(); }   // 占位气泡即本轮气泡
    bubble = newAssistantBubble();
    return bubble;
  };
  // 回滚本连接自上次重置起的渲染（悬决帧判定属上一轮时）：工具行撤除、
  // 气泡内容/推理回滚到快路径种子态
  const undoEsRenders = () => {
    for (const it of esRows) {
      try { it.row.remove(); } catch (e) {}
      if (it.panel && it.panel.isConnected) {
        it.panel._count = Math.max(0, it.panel._count - 1);
        const c = it.panel.querySelector('.tp-count');
        if (c) c.textContent = it.panel._count;
      }
    }
    esRows = [];
    if (panelOurs) {
      const p = _toolPanel;
      resetToolPanel();
      if (p && p.isConnected && !p._list.children.length) { try { p.remove(); } catch (e) {} }
      panelOurs = false;
    }
    const b = bubble;
    if ((renderedC || renderedR) && b && b.isConnected) {
      if (renderedC) {
        if (seedJoinedC && b === seedBubble) streamContent(b, seedJoinedC, false);
        else renderContent(b, '');
      }
      if (renderedR) {
        if (seedJoinedR && b === seedBubble) streamReasoning(b, seedJoinedR, false);
        else if (b._reasoningBlock) {
          b._reasoningBlock.remove();
          b._reasoningBlock = null; b._reasoningBody = null; b._reasoningRenderer = null;
        }
      }
    }
  };
  const resetTurnState = () => {
    deferred = null;
    tmp = ''; rTmp = ''; closedC = ''; roundClosed = false;
    segIdx = 0; toolStarts = 0;
    esStarts = 0; esEnds = 0;
    engaged = false; foreign = false;
    seenApprovals.clear();
    undoEsRenders();
    renderedC = renderedR = false;
    dirtyC = dirtyR = false;
  };
  // 越过补差区（→②）：累积文本是屏上内容的超集，覆盖式渲染（前缀幂等，
  // 块缓存命中），reasoning 整段补齐（对齐 live 的跨段累积语义），此后逐帧增量
  const engage = () => {
    if (engaged) return;
    engaged = true;
    const b = ensureBubble();   // 先定目标气泡，dropHint 才能识别「占位即目标」
    dropHint();
    if (closedC || tmp) { streamContent(b, displayContent(), true); renderedC = true; }
    if (rTmp) { renderedRLen = 0; streamReasoning(b, rTmp, true); renderedR = true; renderedRLen = rTmp.length; }
    scrollBottom(true);   // 刚切回页面首次接流：强制到底一次（其后 flush 恢复跟随态闸门）
  };
  const flush = () => {
    flushTimer = null;
    if (finished || !engaged) return;
    if (!dirtyC && !dirtyR) return;
    const b = ensureBubble();
    if (dirtyC) { streamContent(b, displayContent(), true); renderedC = true; }
    if (dirtyR) { streamReasoning(b, rTmp, true); renderedR = true; renderedRLen = rTmp.length; }
    dirtyC = dirtyR = false;
    scrollBottom();
  };
  const scheduleFlush = () => { if (flushTimer === null) flushTimer = setTimeout(flush, 50); };
  const closeEs = () => {
    if (flushTimer !== null) { clearTimeout(flushTimer); flushTimer = null; }
    try { es && es.close(); } catch (e) {}
  };

  // 收尾（终态帧/done/审批采纳）。engaged=false 时屏上即快路径终态，不重渲
  //（悬决帧可能属上一轮——渲染其内容会污染本轮视图）
  const finalize = (term) => {
    finished = true; closeEs();
    _esResumeSid = null;
    const b = (bubble && bubble.isConnected) ? bubble : null;
    if (term && term.type === 'complete') {
      if (engaged && b) {
        if (rTmp) renderReasoning(b, rTmp);
        renderContent(b, renderMarkdown(displayContent() || term.d.content || ''));
      }
      if (b) enhanceCodeBlocks(b._contentEl || b);
      if (currentSessionId) rememberCurrentSession(currentSessionId);   // 与 streamMessage complete 同语义
    } else if (term && term.type === 'error') {
      if (engaged && b) {
        renderContent(b, `<p style="color:#e8484c">⚠️ ${escapeHtml(term.d.message || '未知错误')}</p>`);
      }
    } else if (!term && engaged && b) {
      // done 且无终态帧（worker 崩溃/中断收流）：终渲已积累内容去光标
      if (rTmp) renderReasoning(b, rTmp);
      renderContent(b, renderMarkdown(displayContent()));
      enhanceCodeBlocks(b._contentEl || b);
    }
    if (term && term.type === 'approval') {
      if (term.d && typeof term.d === 'object' && term.d.action) showApproval(term.d);
    }
    releasePending(); _teardownBgPoll(); loadSidebarSessions();
    scrollBottom();
  };
  // 终态帧一律悬决（不做 engaged 快路径）：真终态帧后端必立刻关流发 done
  //（TERMINAL_EVENTS break → done，中间无数据帧）→ done 采纳收尾；上一轮的
  // 终态帧后必随本轮数据帧 → 重置回滚。engaged 与否不影响裁决正确性。
  const tryTerminal = (type, d) => { deferred = { type, d }; };

  const onToken = (d) => {
    if (foreign || typeof d.content !== 'string' || !d.content) return;
    roundClosed = false;
    tmp += d.content;
    if (engaged) { dirtyC = true; scheduleFlush(); return; }
    // 分段推进：该分段有 tool_calls（分段链式推进的唯一依据）且已全部补差
    // → 后续 token 属下一段/实时区。0-tool 末段（终答段）永不越过——它的
    // token 永远与本段比对（越过后等长终态会被误判实时增量）
    while (segIdx < durableSegs.length) {
      const cum = cumTools[segIdx] || 0;
      const prev = segIdx > 0 ? (cumTools[segIdx - 1] || 0) : 0;
      if (cum > prev && toolStarts >= cum) { segIdx++; continue; }
      break;
    }
    const seg = durableSegs[segIdx];
    const segC = seg === undefined ? null : String(seg.content || '');
    if (segC === null || segC === '') { engage(); return; }   // 越过落盘区 / 空段：实时
    if (tmp.length > segC.length) {
      if (tmp.startsWith(segC)) { engage(); return; }         // 落盘前缀 + 实时增量
      foreign = true; return;                                  // 前缀失配：上一轮残留
    }
    if (tmp.length === segC.length ? tmp !== segC : !segC.startsWith(tmp)) foreign = true;
    // 否则：补差进行中，保持抑制（屏上已是该段落的落盘终态）
  };
  const onReasoning = (d) => {
    if (foreign || typeof d.content !== 'string' || !d.content) return;
    roundClosed = false;
    rTmp += d.content;
    // 补差期只累积不渲染（屏上 reasoning 保持快路径尾段），engage 时整段补齐
    if (engaged && rTmp.length > renderedRLen) { dirtyR = true; scheduleFlush(); }
  };
  const onToolStart = (d) => {
    toolStarts++;
    if (!roundClosed) {
      roundClosed = true;
      if (tmp && tmp.trim()) {
        const piece = tmp.replace(/\s+$/, '');
        closedC = closedC ? closedC.replace(/\s+$/, '') + '\n\n---\n\n' + piece : piece;
      }
      tmp = ''; dirtyC = false;
      if (rTmp) rTmp += '\n\n---\n\n';
    }
    if (!engaged && toolStarts <= durableTools) return;   // ①补差帧：面板行已由历史投影渲染
    // ②实时 tool 调用：封口上一轮正文（保留），不再清屏替换
    engaged = true;
    const b = ensureBubble();   // 先定目标气泡，dropHint 才能识别「占位即目标」
    dropHint();
    if (closedC) { streamContent(b, closedC, false); renderedC = true; }
    const panelExisted = !!(_toolPanel && document.body.contains(_toolPanel));
    addToolStart(d.tool_name, JSON.stringify(d.tool_args || {}), d.tool_name === 'task', d.tool_id);
    renderedC = true;
    esStarts++;
    if (!panelExisted) panelOurs = true;
    if (_toolPanel && _toolPanel._list) {
      esRows.push({ panel: _toolPanel, row: _toolPanel._list.lastElementChild });
    }
    scrollBottom();
  };
  const onToolEnd = (d) => {
    if (esEnds >= esStarts) {
      // 对应 tool_start 未由本连接渲染（补差帧）：结果行一般已由历史投影回填
      //——例外：快路径抓取时结果尚未落盘、行仍 ⏳，按名字收尾一次
      if (!engaged && !foreign && _toolPanel && _toolPanel._list) {
        const row = Array.from(_toolPanel._list.querySelectorAll('.tp-row.tp-running'))
          .find(r => r.querySelector('.tp-name') && r.querySelector('.tp-name').textContent === d.tool_name);
        if (row) { esEnds++; addToolEnd(d.tool_name, d.result || '', d.tool_id); }
      }
      return;
    }
    esEnds++;
    addToolEnd(d.tool_name, d.result || '', d.tool_id);
    scheduleTreeRefresh(d.tool_name || '');
  };
  // 审批帧：同终态帧悬决裁决——worker 在审批帧处 break（流终止），GET 订阅
  // 随之收流发 done → done 采纳出审批面板（本轮真在等人决策）；上一轮的
  // 过期审批帧后必随本轮数据帧 → 重置丢弃。实时区重放按 thread_id 去重
  const onApproval = (d) => {
    const key = d.thread_id || '';
    if (seenApprovals.has(key)) return;
    seenApprovals.add(key);
    deferred = { type: 'approval', d };
  };
  const onTransportError = () => {
    if (finished) return;
    finished = true; closeEs();
    _esResumeSid = null;
    // 回退既有兜底：提示条 + 3s 轮询 + 结束 reload（占位与输入锁保持）
    if (bgBanner) {
      bgBanner.style.display = 'flex';
      sendBtn.disabled = true;
      if (!bgPollTimer) bgPollTimer = setInterval(pollBackgroundGeneration, 3000);
    }
  };

  try {
    es = new EventSource('/api/chat/stream/' + encodeURIComponent(sid) + '?after=0');
  } catch (e) { onTransportError(); return; }
  es.onopen = () => {
    // ES 接管在途监测：撤并发的兜底轮询（loadCurrentSession 末尾的一次性
    // active 探测可能已亮条起表）
    _esResumeSid = sid;
    _teardownBgPoll();
  };
  const on = (type, fn) => es.addEventListener(type, (e) => {
    if (finished) return;
    if (currentSessionId !== sid) {   // 用户已新建/切换会话：静默收线
      finished = true; closeEs(); _esResumeSid = null; releasePending(); return;
    }
    let d = {};
    try { d = e.data ? JSON.parse(e.data) : {}; } catch (err) {}
    // 悬决帧裁决：其后还有数据帧 → 那一帧属上一轮（重置回滚），当前帧正常
    // 处理。done 例外——它采纳悬决帧（见 onDone），不得先重置
    if (deferred && type !== 'done') resetTurnState();
    fn(d);
  });
  on('token', onToken);
  on('reasoning_token', onReasoning);
  on('tool_start', onToolStart);
  on('tool_end', onToolEnd);
  on('human_approval_request', onApproval);
  on('approval_request', onApproval);   // 契约文档名兼容（worker 实际发前者）
  on('complete', (d) => tryTerminal('complete', d));
  // 非流帧透传（对齐 streamMessage 语义；todos 全量替换天然幂等）
  on('todos_update', (d) => renderTodoPanel(d.todos || []));
  on('auto_compact', (d) => showToast(`🔄 上下文已自动压缩（${d.compacted_count ?? '?'} 条历史 → 摘要，原 ${d.original_count ?? '?'} 条）`));
  on('memory_search', (d) => { if (engaged) renderMemoryPanel(msgBox, d || {}); });
  on('done', () => {
    if (finished) return;
    const term = deferred;
    deferred = null;
    finalize(term);   // 有悬决终态/审批帧 → 采纳；否则屏上已是终态，纯收尾
  });
  // error 双语义必须单点判别：传输层错误与 SSE 命名 error 帧都会派发到
  // 'error' 监听——e.data 有无是唯一判据（命名帧必有 data 行，传输层无）。
  // 不能同时走 on('error')（其 wrapper 会把传输错误当命名帧误终渲）
  es.addEventListener('error', (e) => {
    if (e.data === undefined) { onTransportError(); return; }
    if (finished) return;
    if (currentSessionId !== sid) {
      finished = true; closeEs(); _esResumeSid = null; releasePending(); return;
    }
    let d = {};
    try { d = JSON.parse(e.data); } catch (err) {}
    if (deferred) resetTurnState();
    tryTerminal('error', d);
  });
}

// ——以下为 chat.js L1892-2095：后台生成探测 + 会话恢复轮询 + 提示条显式停止——
// ---------- 后台生成探测：切页期间对话继续，回到本页自动接上 ----------
// 语义（断开 ≠ 停止）：生成中去别的页面，worker 照常跑完并落盘；
// 回到本页时探测到"在途"就亮提示条 + 3s 轮询，结束后重载读完整历史。
// 2026-09-05 分层：页面加载的历史首屏已由零锁快路径（事件库直读投影，
// 见 _renderFromEventStore）承接——回到本页立即见历史 + 在途占位气泡，
// 本轮询只负责「生成结束后自动接上」（active→false 时 reload 全量重载）。
// 下方「会话恢复轮询」（scheduleSessionRecovery/tryRecoverSession）降级为
// 兜底：仅当快路径也没渲染出来（事件库与会话列表皆空）且 /current 撞
// worker 锁 5s 超时 → currentSessionId 为空时，5s 间隔重试会话恢复（候选
// sid 取 sessionStorage 按项目键，缺失时兜底取项目最近会话），校验用零
// worker-IPC 的轻量端点——/api/chat/active（主进程内存读，在途生成的硬
// 证据）与 /api/sessions/{sid}/events（主进程直读 SQLite，已落盘证据）。
const bgBanner = document.getElementById('bg-gen-banner');
let bgPollTimer = null;
// /active 探测 false 的有界重试计数（防 spawn 窗口盲区一次性放弃，判据见
// _activeRetryEligible）：active=true 或换探测目标时归零，连续超限回落
// 既有 give-up 逻辑。重试期间不亮提示条——提示条亮过 give-up 就走
// location.reload，而真死轮（事件停在 turn/start）每次加载都满足判据 (b)，
// 亮条 + reload 会复活旧注释严防的「探测即 false → reload → 仍 false」
// 死循环；不亮条时 give-up 只是停轮询静置，天然收敛。
let _activeRetryCount = 0;
let _activeRetrySid = '';
// EventSource 续流接管中的会话（es.onopen 置位，error/收尾清空）：期间
// 兜底轮询让位——否则 loadCurrentSession 末尾的一次性 active 探测会在 ES
// 渲染完成后重新亮条起表，inactive 分支的 reload 打断「不重载」收尾
let _esResumeSid = null;

function pollBackgroundGeneration() {
  if (!bgBanner) return;
  if (_esResumeSid) return;   // ES 续流已接管在途监测（断连时自会回退本轮询）
  if (!currentSessionId) {
    // 会话 id 尚未恢复（初始化瞬时失败）：先恢复会话，再谈 active 探测
    if (_needsSessionRecovery) scheduleSessionRecovery();
    return;
  }
  const sid = currentSessionId;
  fetch('/api/chat/active?session_id=' + encodeURIComponent(sid))
    .then(r => r.ok ? r.json() : { active: false })
    .then(async d => {
      if (d.active) {
        _activeRetryCount = 0; _activeRetrySid = sid;
        bgBanner.style.display = 'flex';
        sendBtn.disabled = true;             // 同槽串行，排队无意义
        if (!bgPollTimer) bgPollTimer = setInterval(pollBackgroundGeneration, 3000);
        return;
      }
      // false：spawn 窗口盲区有界重试（见 _activeRetryEligible），超限回落
      // 既有 give-up；换探测目标（会话切换）计数归零
      if (_activeRetrySid !== sid) { _activeRetrySid = sid; _activeRetryCount = 0; }
      if (_activeRetryCount < ACTIVE_RETRY_MAX && await _activeRetryEligible(sid)) {
        _activeRetryCount++;
        if (!bgPollTimer) bgPollTimer = setInterval(pollBackgroundGeneration, 3000);
        return;
      }
      _activeRetryCount = 0;
      if (bgBanner.style.display !== 'none') {
        clearInterval(bgPollTimer); bgPollTimer = null;
        bgBanner.style.display = 'none';
        location.reload();                   // 后台内容已落盘，重载读完整历史
      } else if (bgPollTimer) {
        // 加载期一次性探测（无亮条）give-up：停掉为重试起的轮询，页面静置
        clearInterval(bgPollTimer); bgPollTimer = null;
      }
    })
    .catch(() => {});
}

// ---------- 会话恢复轮询（currentSessionId 为空时的自愈路径） ----------
// 有界收敛：单个候选连续 RECOVER_MAX_MISSES 次「不可判定」即换兜底候选；
// 兜底也失效则按 loadCurrentSession 步骤③语义收敛为空白新会话，
// 页面恢复可用——不会无限轮询，也不会反复打忙 worker。
const RECOVER_INTERVAL_MS = 5000;  // 恢复重试间隔：避开 worker 锁 5s 阻塞窗
const RECOVER_MAX_MISSES = 4;      // 候选不可判定宽限（≈20s，覆盖刚结束尚在落盘的竞态）
let _recoverTimer = null;
let _recoverSource = 'storage';    // 候选来源推进：storage → latest（兜底）→ blank（收敛）
let _recoverSid = '';
let _recoverMisses = 0;

function scheduleSessionRecovery() {
  if (!bgBanner || !_needsSessionRecovery || currentSessionId) return;
  if (_recoverTimer) return;  // 已有待触发的重试，不叠加
  _recoverTimer = setTimeout(() => { _recoverTimer = null; tryRecoverSession(); }, RECOVER_INTERVAL_MS);
}

// 恢复收敛为「空白新会话」（与 loadCurrentSession 步骤③同语义）：
// 项目确实没有可恢复的会话时让页面恢复可用，而非永久空态
function _finishRecoveryAsBlank() {
  _needsSessionRecovery = false;
  _recoverSource = 'blank';
  currentSessionId = newTabSessionId();
  updateSessionInfo(currentSessionId);
  refreshEmptyState();
  syncRecoveryInputLock();   // 恢复收敛：解锁发送
}

async function tryRecoverSession() {
  if (currentSessionId || !bgBanner || !_needsSessionRecovery) return;

  // ① 取候选 sid：优先本标签页按项目记住的键；键缺失（失败路径已被清、
  //    或异常发生在清键之后）则兜底取项目最近一次会话——/api/sessions
  //    主进程直读磁盘快照，不经 worker 锁，必不 busy
  if (_recoverSource === 'storage') {
    try { _recoverSid = sessionStorage.getItem(sessionKey()) || ''; } catch (e) { _recoverSid = ''; }
    if (!_recoverSid) _recoverSource = 'latest';
  }
  if (_recoverSource === 'latest' && !_recoverSid) {
    try {
      const lr = await fetch('/api/sessions?project=' + encodeURIComponent(ACTIVE_PROJECT));
      if (lr.ok) {
        const { sessions = [] } = await lr.json();
        const latest = sessions.reduce((a, b) =>
          !a || (b.updated_at || '') > (a.updated_at || '') ? b : a, null);
        _recoverSid = latest ? (latest.session_id || '') : '';
      }
    } catch (e) {}
    if (!_recoverSid) { _finishRecoveryAsBlank(); return; }
  }
  const sid = _recoverSid;
  if (!sid) { _finishRecoveryAsBlank(); return; }

  // ② 轻量校验：两端点都不经 worker 锁，busy 期间也快速返回
  let active = false, persisted = false;
  try {
    const ar = await fetch('/api/chat/active?session_id=' + encodeURIComponent(sid));
    if (ar.ok) active = (await ar.json()).active === true;
  } catch (e) {}
  if (!active) {
    try {
      const er = await fetch('/api/sessions/' + encodeURIComponent(sid) + '/events');
      if (er.ok) persisted = ((await er.json()).events || []).length > 0;
    } catch (e) {}
  }

  if (active) {
    // 在途生成：恢复会话 id，亮既有提示条（chat.html 内置「后台生成中」
    // 文案 + 停止按钮），复用既有 3s active 轮询——结束后统一
    // location.reload() 重走完整加载，与「留在本页」场景收尾完全一致
    _needsSessionRecovery = false;
    _recoverSource = 'blank';
    rememberCurrentSession(sid);
    bgBanner.style.display = 'flex';
    sendBtn.disabled = true;   // 同槽串行，排队无意义（与既有 active 分支一致）
    if (!bgPollTimer) bgPollTimer = setInterval(pollBackgroundGeneration, 3000);
    return;
  }
  if (persisted) {
    // 已结束且已落盘：补齐 storage 键后重走正常加载（此刻 worker 已空闲，
    // /current 必成功、历史完整回显）。若再遇瞬时失败，_needsSessionRecovery
    // 仍为 true，pollBackgroundGeneration 会重新进入恢复轮询
    _recoverSource = 'blank';
    try { sessionStorage.setItem(sessionKey(), sid); } catch (e) {}
    loadCurrentSession();
    return;
  }

  // ③ 不可判定（未激活也未落盘）：可能是刚结束尚在落盘（宽限覆盖），
  //    或候选已失效（会话被删/空会话残渣）。宽限计数，超限换兜底候选；
  //    兜底也失效则收敛空白新会话。
  //    收敛为 blank 前最后一次事件直读兜底：blank 会生成全新随机 sid 并
  //    清掉当前候选——若此刻候选会话的事件其实已落库（早期直读撞上
  //    spawn 窗口/主从复制延迟返回空），blank 收敛会让在途/已完成会话
  //    与本页永久失联（用户须手动翻侧栏）。有事件 → 走 persisted 路径。
  if (++_recoverMisses >= RECOVER_MAX_MISSES) {
    _recoverMisses = 0;
    if (_recoverSource === 'latest') {
      // latest 兜底也失败前的最后一查：候选 sid 直读事件（上一步 active
      // false 分支只在 !active 时查 persisted；这里补 active=true 竞态与
      // 直读为空的最终确认）
      try {
        const er = await fetch('/api/sessions/' + encodeURIComponent(sid) + '/events');
        if (er.ok) {
          const events = ((await er.json().catch(() => ({}))) || {}).events;
          if (Array.isArray(events) && events.length) {
            _recoverSource = 'blank';
            try { sessionStorage.setItem(sessionKey(), sid); } catch (e) {}
            loadCurrentSession();   // 事件在 → 权威恢复（同 persisted 分支）
            return;
          }
        }
      } catch (e) {}
    }
    if (_recoverSource === 'storage') {
      _recoverSource = 'latest';
      _recoverSid = '';
      try { sessionStorage.removeItem(sessionKey()); } catch (e) {}  // 失效残渣清掉
      scheduleSessionRecovery();   // 5s 后用兜底候选再试
      return;
    }
    _finishRecoveryAsBlank();
    return;
  }
  scheduleSessionRecovery();
}

// 提示条上的显式停止：走 /chat/stop，轮询探到 inactive 后统一重载
const bgGenStopBtn = document.getElementById('bg-gen-stop');
if (bgGenStopBtn) {
  bgGenStopBtn.addEventListener('click', () => {
    fetch('/api/chat/stop', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: currentSessionId || '' }),
    }).catch(() => {});
  });
}
