// markdown 渲染/消毒/块级增量渲染器——自 chat.js 首批分块外置拆出，逐字搬移（P2-4）
// ---------- 渲染辅助 ----------
// D. XSS 防护：marked 默认不转义原始 HTML，LLM 输出可能含 <script>/<img onerror>。
//    先用 marked 解析 markdown，再移除危险的标签/属性。
function sanitizeHtml(html) {
  // 用 DOMParser 解析后剔除危险节点，比正则可靠
  const doc = new DOMParser().parseFromString(html, 'text/html');
  // 移除 script/style/iframe/object/embed/link/meta，及 noscript/noembed/noframes。
  // mXSS：后三者（noscript 在 scripting enabled 的活文档中）内容按 raw text
  // 解析，而 DOMParser 惰性文档 scripting disabled 时按标记解析——消毒文档
  // 里藏在属性值内的 `</noscript><img onerror=...>` 序列化后写回活文档，
  // noscript 提前闭合、payload 物化成真元素，逃过全部属性清洗。三个标签
  // 整节点移除（连带子树），使输出中不存在任何 scripting 标志依赖的
  // raw-text 容器，解析上下文不再随文档而变。
  doc.querySelectorAll('script, style, iframe, object, embed, link, meta, base, form, noscript, noembed, noframes').forEach(el => el.remove());
  // 移除所有 on* 事件属性（onclick/onerror/onload...）
  // P1-8：javascript: 判定改用 new URL（WHATWG URL 规范）——此前在 DOMParser
  // 实体解码后的字符串上做 /^\s*javascript:/ 前缀匹配，`jav&#x09;ascript:`
  // 解码为 `jav\tascript:` 不命中正则，而浏览器导航时会剥掉值里的 \t\r\n
  // 仍按 javascript: 执行。new URL 与导航走同一套解析规则（剥 \t\r\n 与
  // 首尾 C0 空白），判定与执行语义一致即无边界差。
  // 属性名单从 href/src 扩到一切能发起导航/表单提交的属性。
  const URL_ATTRS = ['href', 'src', 'xlink:href', 'formaction', 'action'];
  doc.querySelectorAll('*').forEach(el => {
    [...el.attributes].forEach(attr => {
      if (/^on/i.test(attr.name)) { el.removeAttribute(attr.name); return; }
      if (URL_ATTRS.includes(attr.name) && isJavascriptUrl(attr.value)) {
        el.removeAttribute(attr.name);
      }
    });
  });
  return doc.body.innerHTML;
}

// P1-8：与浏览器导航同规范的 javascript: 判定。相对 URL 正常解析（非
// javascript: 协议一律放行）；连 URL 解析都抛错的值，浏览器同样无法
// 导航执行，按安全放行。
function isJavascriptUrl(v) {
  try {
    return new URL(v, location.href).protocol === 'javascript:';
  } catch (e) {
    return false;
  }
}

function renderMarkdown(text) {
  let html;
  try {
    html = (window.marked && marked.parse) ? marked.parse(text) : text;
  } catch (e) {
    html = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/\n/g, '<br>');
  }
  html = sanitizeHtml(html);
  return html;
}

// ---------- 块级增量渲染器（2026-09-05 渲染优化） ----------
// 痛点：流式 flush 每 50ms 对累积全文 marked.parse + sanitizeHtml（DOMParser
// 全文消毒）+ innerHTML 全量替换 + scrollBottom 强制布局——O(n²)，文本越长
// 单帧越慢，正文无界高度放大布局成本。改法：按块级边界（双换行；``` / ~~~
// 围栏内不切，让代码块在围栏闭合时整块定型，未闭合围栏整体留在尾块）把
// 累积文本拆为「已闭合块 + 未闭合尾块」，渲染容器改两段：
//   mount = .blk-done（已闭合块容器：只追加新块，单块渲染结果缓存复用）
//         + .blk-tail（活动尾块容器：每次 flush 仅重渲这一段，光标 ▌ 只在这）
// 每 flush 成本 = 新闭合块首渲 + 尾块重渲（典型尾块 = 当前段落，有界）。
// complete 终态弃结构全量终渲一次（renderMarkdown 整文 + enhanceCodeBlocks）
// ——跨块上下文（loose list 合并 / 引用式链接定义）在终渲回归精确，流式期
// 为逐块近似。覆盖式重放（EventSource 续流补差重发全轮 token）靠块缓存：
// 已渲染前缀命中缓存零重渲，只有尾块付出渲染成本。
function splitMarkdownBlocks(text) {
  const closed = [];
  let buf = [];
  let fence = false, fenceCh = '';   // 围栏内不切（不同围栏字符互不闭合）
  const lines = String(text).split('\n');
  for (const ln of lines) {
    const m = ln.match(/^ {0,3}(`{3,}|~{3,})/);
    if (m) {
      if (!fence) { fence = true; fenceCh = m[1][0]; }
      else if (m[1][0] === fenceCh) { fence = false; fenceCh = ''; }
    }
    if (!fence && ln.trim() === '') {
      if (buf.length) { closed.push(buf.join('\n')); buf = []; }
      continue;   // 连续空行折叠
    }
    buf.push(ln);
  }
  return { closed, tail: buf.join('\n') };
}

function _lensSum(blocks, n) {
  let s = 0;
  for (let i = 0; i < n && i < blocks.length; i++) s += blocks[i].length;
  return s;
}

function createBlockRenderer(mount) {
  const doneEl = document.createElement('div');
  doneEl.className = 'blk-done';
  const tailEl = document.createElement('div');
  tailEl.className = 'blk-tail';
  mount.textContent = '';   // 重建场景：清掉终渲 HTML / 上一代结构
  mount.appendChild(doneEl);
  mount.appendChild(tailEl);
  const cache = new Map();   // 块原文 → 渲染 HTML（随渲染器整体丢弃）
  let rendered = [];         // 已上 DOM 的闭合块原文（append-only 前缀判定）
  let renderedLen = 0;       // rendered 各块长度和（快速前缀校验）
  const appendBlock = (block) => {
    let html = cache.get(block);
    if (html === undefined) { html = renderMarkdown(block); cache.set(block, html); }
    const div = document.createElement('div');
    div.className = 'blk';
    div.innerHTML = html;
    doneEl.appendChild(div);
  };
  return {
    mount, doneEl, tailEl,   // doneEl/tailEl 供 _ensureBlockRenderer 做在文档内校验
    // 覆盖式渲染累积全文：token 流只追加 → 闭合块前缀稳定，仅追加新块 +
    // 重渲尾块；前缀失配（写入另一段文本，如 renderHistory 复用气泡跨消息
    // 覆盖）→ 清空重建（缓存保留，同文块命中免重渲）
    stream(text, cursor) {
      const { closed, tail } = splitMarkdownBlocks(text);
      const n = closed.length, r = rendered.length;
      const prefixOk = n >= r && (r === 0 ||
        (_lensSum(closed, r) === renderedLen && closed[r - 1] === rendered[r - 1]));
      if (prefixOk) {
        for (let i = r; i < n; i++) { appendBlock(closed[i]); renderedLen += closed[i].length; }
        rendered = closed.slice();
      } else {
        doneEl.textContent = '';
        rendered = []; renderedLen = 0;
        for (const b of closed) { appendBlock(b); renderedLen += b.length; }
        rendered = closed.slice();
      }
      // 光标 ▌ 只放尾块（文本恰以块边界结尾时尾块为空 → 光标独立成段，
      // 与旧「全文 HTML + ▌」在块边界后的视觉表现一致）
      tailEl.innerHTML = renderMarkdown(tail + (cursor ? '▌' : ''));
    },
  };
}

// 渲染器挂载点（content→bubble-content / reasoning→reasoning-body）。
// 终渲/占位路径（renderContent/renderReasoning）的 innerHTML 全量替换会拆掉
// 两段容器——靠 doneEl/tailEl 脱离文档判定失效，下次流式自动重建。
function _ensureBlockRenderer(bubble, kind) {
  const key = kind === 'reasoning' ? '_reasoningRenderer' : '_contentRenderer';
  const mount = kind === 'reasoning' ? bubble._reasoningBody : bubble._contentEl;
  const cur = bubble[key];
  if (cur && cur.mount === mount && mount.contains(cur.doneEl) && mount.contains(cur.tailEl)) return cur;
  const r = createBlockRenderer(mount);
  bubble[key] = r;
  return r;
}

// 流式正文（覆盖式 + 尾块光标）。flushRender / EventSource 续流共用。
function streamContent(bubble, text, cursor) {
  if (!bubble._contentEl) {
    const el = document.createElement('div');
    el.className = 'bubble-content';
    bubble.appendChild(el);
    bubble._contentEl = el;
  }
  _ensureBlockRenderer(bubble, 'content').stream(text, cursor);
}

// 流式推理（覆盖式 + 尾块光标）；reasoning-body 限高内滚，跟随最新输出。
function streamReasoning(bubble, text, cursor) {
  const det = getReasoningBlock(bubble);
  _ensureBlockRenderer(bubble, 'reasoning').stream(text, cursor);
  if (det.open) bubble._reasoningBody.scrollTop = bubble._reasoningBody.scrollHeight;
}

// C. 给渲染后内容里的 <pre> 加一键复制按钮（在 innerHTML 设置后调用）
function enhanceCodeBlocks(container) {
  if (!container) return;
  container.querySelectorAll('pre').forEach(pre => {
    if (pre.querySelector('.copy-btn')) return;  // 已加过
    const btn = document.createElement('button');
    btn.className = 'copy-btn';
    btn.textContent = '复制';
    btn.addEventListener('click', async () => {
      const code = pre.querySelector('code') ? pre.querySelector('code').textContent : pre.textContent;
      try {
        await navigator.clipboard.writeText(code);
        btn.textContent = '✓ 已复制';
        setTimeout(() => btn.textContent = '复制', 1500);
      } catch (e) {
        btn.textContent = '✗ 失败';
        setTimeout(() => btn.textContent = '复制', 1500);
      }
    });
    pre.style.position = 'relative';
    pre.appendChild(btn);
  });
}
