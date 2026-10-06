// 图片上传/预览/灯箱（多模态）——自 chat.js 首批分块外置拆出，逐字搬移（P2-4）
// ---------- 图片上传（多模态） ----------
// 三种入口：📎按钮选择、粘贴图片、拖拽图片到聊天区
const attachBtn = document.getElementById('attach-btn');
const fileInput = document.getElementById('file-input');
const imagePreview = document.getElementById('image-preview');
let pendingImages = [];  // [{id, url, filename}]

async function uploadImage(file) {
  if (!file || !file.type.startsWith('image/')) return;
  if (file.size > 10 * 1024 * 1024) {
    showToast('图片过大（上限 10MB）', 'error');
    return;
  }
  const fd = new FormData();
  fd.append('file', file);
  try {
    const resp = await fetch('/api/upload', { method: 'POST', body: fd });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: resp.statusText }));
      showToast('上传失败: ' + (err.detail || '未知错误'), 'error');
      return;
    }
    const data = await resp.json();
    pendingImages.push(data);
    renderImagePreview();
  } catch (e) {
    showToast('上传失败: ' + e.message, 'error');
  }
}

function renderImagePreview() {
  imagePreview.innerHTML = '';
  pendingImages.forEach((img, idx) => {
    const chip = document.createElement('div');
    chip.className = 'img-chip';
    // 点缩略图 → 预览大图（不是删除）
    const thumb = document.createElement('img');
    thumb.src = img.url;
    thumb.alt = img.filename;
    thumb.title = '点击预览';
    thumb.addEventListener('click', () => openImageLightbox(img.url));
    chip.appendChild(thumb);
    // × 按钮 → 删除（独立，不触发预览）
    const rm = document.createElement('button');
    rm.className = 'img-chip-remove';
    rm.textContent = '×';
    rm.title = '移除';
    rm.addEventListener('click', (e) => {
      e.stopPropagation();
      pendingImages.splice(idx, 1);
      renderImagePreview();
    });
    chip.appendChild(rm);
    imagePreview.appendChild(chip);
  });
}

// 📎按钮 → 触发隐藏的 file input
attachBtn.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', () => {
  for (const f of fileInput.files) uploadImage(f);
  fileInput.value = '';  // 允许重复选同一文件
});

// 粘贴图片（Ctrl+V）：从剪贴板取 image blob
input.addEventListener('paste', (e) => {
  const items = e.clipboardData?.items;
  if (!items) return;
  for (const item of items) {
    if (item.type.startsWith('image/')) {
      const file = item.getAsFile();
      if (file) uploadImage(file);
    }
  }
});

// 拖拽图片到聊天区
const chatWrap = document.querySelector('.chat-wrap');
chatWrap.addEventListener('dragover', (e) => {
  if (e.dataTransfer.types.includes('Files')) {
    e.preventDefault();
    chatWrap.classList.add('drag-over');
  }
});
chatWrap.addEventListener('dragleave', (e) => {
  if (e.target === chatWrap) chatWrap.classList.remove('drag-over');
});
chatWrap.addEventListener('drop', (e) => {
  // 始终 preventDefault，阻止浏览器默认 drop 行为（文本拖拽导航等）
  e.preventDefault();
  chatWrap.classList.remove('drag-over');
  // 仅处理图片文件，非文件拖拽直接忽略
  if (!e.dataTransfer.files || !e.dataTransfer.files.length) return;
  for (const f of e.dataTransfer.files) uploadImage(f);
});

// ---------- 图片灯箱（点击放大查看，有明确关闭按钮） ----------
function openImageLightbox(url) {
  const overlay = document.createElement('div');
  overlay.className = 'img-lightbox';
  // 关闭按钮（右上角）
  const closeBtn = document.createElement('button');
  closeBtn.className = 'img-lightbox-close';
  closeBtn.textContent = '×';
  closeBtn.title = '关闭';
  overlay.appendChild(closeBtn);
  const im = document.createElement('img');
  im.src = url;
  overlay.appendChild(im);
  // P3-10：三条关闭路径（×按钮/遮罩点击/Escape）统一走 dismiss，并在
  // dismiss 内移除 keydown 监听——原先只有 Escape 路径自移除，点 × 或
  // 遮罩关闭后监听器泄漏，每开一张图累积一个常驻 keydown 监听。
  const onKey = (e) => { if (e.key === 'Escape') dismiss(); };
  const dismiss = () => {
    document.removeEventListener('keydown', onKey);
    overlay.remove();
  };
  closeBtn.addEventListener('click', dismiss);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) dismiss(); });
  document.addEventListener('keydown', onKey);
  document.body.appendChild(overlay);
}
