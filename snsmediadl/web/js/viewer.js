// 放大檢視器：原檔、滾輪縮放、拖曳平移、同貼文換張。
//
// **不引入任何 lightbox 套件**：這個前端是零建置的原生 ES module，加一個
// 套件就得加一條建置流程，而縮放與平移本身是幾十行的事。

import { fileErrorText, fmtBytes } from './dom.js';
import { pushDismissable } from './overlay.js';

// 縮放範圍。下限是「符合視窗」的一半（再小就沒有意義了），
// 上限 8 倍 —— 再放大只是看內插出來的像素。
const MIN_SCALE_OF_FIT = 0.5;
const MAX_SCALE = 8;
const WHEEL_STEP = 1.1;

// 超過這個位移就算「拖曳」而不是「點擊」。手不穩的點擊會晃個一兩像素，
// 沒有這條容差的話，那種點擊會被當成拖曳（反過來也一樣糟）。
const DRAG_SLOP = 4;

/** 這一次點擊該不該關掉檢視器？
 *
 *  ⚠️ 必須看 **pointerdown 的目標**，不能看 click 的 `ev.target` ——
 *  拖曳平移時我們對 stage 呼叫 `setPointerCapture`，之後的 pointer 事件
 *  （包含由它們合成的 `click`）全部被重新指向 stage。於是在影像上按著拖曳、
 *  放開滑鼠，click 的 target 會是 stage，一路撞進「點背景 = 關閉」那條分支：
 *  拖曳到一半放手就關掉整個檢視器。純點影像也會誤關，同一個根因。
 *
 *  純函式方便測試（見 `test_viewer.mjs`）。*/
export function shouldCloseOnClick({ downTarget, dragged, stage, root }) {
  if (dragged) return false;
  return downTarget === stage || downTarget === root;
}

// 操作提示只在第一次開啟時顯示。每次都顯示是噪音，永遠不顯示則是
// [[指意才是缺口]] 說的那種「功能存在但沒人知道」。
const HINT_SEEN_KEY = 'snsmediadl.viewerHintSeen';

let current = null;

/** 開啟檢視器。
 *
 *  `media`   detail API 回的 media 物件（要 id / kind / bytes）
 *  `siblings` 同貼文的其他張（`[{id}]`），用來左右換張
 *  `onSwitch` 換張時通知呼叫端（詳情面板要跟著換）*/
export function openViewer({ media, siblings = [], onSwitch } = {}) {
  if (current) current.close();

  // 關閉後焦點要回到原本點的那張圖。少了它，鍵盤使用者關掉檢視器之後
  // 焦點掉回 <body>，等於從頭再 Tab 一次（overlay.js 也是這樣做的）。
  const opener = document.activeElement;

  const el = document.createElement('div');
  el.className = 'viewer';
  el.innerHTML = `
    <button type="button" class="viewer-close" data-act="close"
            aria-label="關閉（Esc）">×</button>
    <button type="button" class="viewer-nav prev" data-act="prev" aria-label="上一張">◀</button>
    <button type="button" class="viewer-nav next" data-act="next" aria-label="下一張">▶</button>
    <div class="viewer-stage" data-act="stage"></div>
    <div class="viewer-bar">
      <span class="dims" data-role="dims">載入中…</span>
      <span class="spacer"></span>
      <span class="zoom" data-role="zoom"></span>
      <button type="button" class="ghost" data-act="reset">重設</button>
    </div>
    <p class="viewer-hint${localStorage.getItem(HINT_SEEN_KEY) ? ' hidden' : ''}">
      滾輪縮放 · 拖曳平移 · 雙擊還原 · ← → 換張 · Esc 關閉</p>`;

  document.getElementById('overlayRoot').appendChild(el);
  localStorage.setItem(HINT_SEEN_KEY, '1');

  const stage = el.querySelector('[data-act="stage"]');
  const dimsEl = el.querySelector('[data-role="dims"]');
  const zoomEl = el.querySelector('[data-role="zoom"]');
  const ids = siblings.length ? siblings.map((s) => s.id) : [media.id];
  let index = Math.max(0, ids.indexOf(media.id));

  // 只有一張時**不顯示**換張按鈕（不是 disable）：這裡沒有「等一下就能用」
  // 的可能，disable 只會讓人一直想去按。
  el.classList.toggle('single', ids.length <= 1);

  const handle = pushDismissable({ close: () => close() });

  function close() {
    handle.release();
    // 視窗縮放的監聽掛在 window 上，不解掉的話每開一次就多一個
    // （而且它們會抓著已經移除的 DOM 節點不放）。
    window.removeEventListener('resize', computeFit);
    el.remove();
    document.body.classList.remove('viewer-open');
    if (current && current.el === el) current = null;
    // 觸發元素可能已經被重新渲染掉了（換張會重畫詳情面板），要檢查還在不在
    if (opener instanceof HTMLElement && opener.isConnected) opener.focus();
  }

  current = { el, close };
  document.body.classList.add('viewer-open');

  // ── 縮放狀態 ────────────────────────────────────────
  let img = null;
  let scale = 1;        // 相對於「符合視窗」的倍率
  let fit = 1;          // 符合視窗時的實際縮放（原始像素 → 螢幕像素）
  let tx = 0;
  let ty = 0;

  function paint() {
    if (!img) return;
    img.style.transform =
      `translate(${tx}px, ${ty}px) scale(${(fit * scale).toFixed(4)})`;
    // 倍率是**相對原始像素**的，不是相對 fit —— 使用者想知道的是
    // 「我看到原始畫素了嗎」，而 100% 正是那條線。
    zoomEl.textContent = `${Math.round(fit * scale * 100)}%`;
  }

  function computeFit() {
    if (!img || !img.naturalWidth) return;
    const box = stage.getBoundingClientRect();
    fit = Math.min(box.width / img.naturalWidth, box.height / img.naturalHeight, 1);
    scale = 1;
    tx = 0;
    ty = 0;
    paint();
  }

  function zoomAt(clientX, clientY, factor) {
    const next = Math.min(
      MAX_SCALE / fit,
      Math.max(MIN_SCALE_OF_FIT, scale * factor),
    );
    if (next === scale) return;
    // 以游標為錨：先算游標相對影像中心的位移，縮放後補回去。
    // 不做這件事的話，放大時畫面會往中心跑，使用者會迷路。
    const box = stage.getBoundingClientRect();
    const cx = clientX - (box.left + box.width / 2) - tx;
    const cy = clientY - (box.top + box.height / 2) - ty;
    const ratio = next / scale;
    tx -= cx * (ratio - 1);
    ty -= cy * (ratio - 1);
    scale = next;
    paint();
  }

  // ── 載入這一張 ──────────────────────────────────────
  function load(mediaId) {
    stage.innerHTML = '';
    img = null;
    zoomEl.textContent = '';
    dimsEl.textContent = '載入中…';

    const playable = media.id === mediaId ? media.kind !== 'photo' : null;
    // 換張時拿不到新的 kind（siblings 只有 id）—— 用 <img> 先試，
    // 失敗再換成 <video>。**不猜**：兩種都試過才說讀不到。
    const node = playable === true
      ? videoNode(mediaId)
      : document.createElement('img');

    if (node.tagName === 'IMG') {
      node.src = `/api/media/${mediaId}/file`;
      node.alt = '';
      node.addEventListener('load', () => {
        img = node;
        dimsEl.textContent = `${node.naturalWidth} × ${node.naturalHeight}`
          + (media.id === mediaId && media.bytes ? ` · ${fmtBytes(media.bytes)}` : '');
        computeFit();
      });
      node.addEventListener('error', () => {
        if (playable === false) { fail(mediaId); return; }
        // 可能是影片（換張時不知道 kind）。換成播放器再試一次。
        stage.innerHTML = '';
        const v = videoNode(mediaId);
        v.addEventListener('error', () => fail(mediaId));
        stage.appendChild(v);
        dimsEl.textContent = '影片';
      });
    } else {
      dimsEl.textContent = '影片';
      node.addEventListener('error', () => fail(mediaId));
    }
    stage.appendChild(node);
  }

  async function fail(mediaId) {
    const box = document.createElement('p');
    box.className = 'viewer-missing';
    box.textContent = '讀不到原檔。';
    stage.innerHTML = '';
    stage.appendChild(box);
    dimsEl.textContent = '—';
    // 原因去問後端，**不要在這裡編一個**（對照表與格線／詳情面板共用）
    const why = await fileErrorText(mediaId);
    if (why) box.textContent = why;
  }

  function videoNode(mediaId) {
    const v = document.createElement('video');
    v.src = `/api/media/${mediaId}/file`;
    v.controls = true;
    v.loop = true;          // 需求 #11：影片與動圖都要循環
    v.muted = true;         // 沒有它 Chrome 會拒絕自動播放
    v.playsInline = true;
    v.autoplay = true;
    return v;
  }

  function go(step) {
    if (ids.length <= 1) return;
    index = (index + step + ids.length) % ids.length;
    load(ids[index]);
    onSwitch?.(ids[index]);
  }

  load(ids[index]);

  // ── 互動 ────────────────────────────────────────────
  // 拖曳狀態要在 click 監聽之前宣告 —— click 的判斷讀得到它們。
  let drag = null;
  let dragged = false;
  let downTarget = null;

  el.addEventListener('click', (ev) => {
    const act = ev.target.closest('[data-act]')?.dataset.act;
    if (act === 'close') close();
    else if (act === 'prev') go(-1);
    else if (act === 'next') go(1);
    else if (act === 'reset') computeFit();
    // 點背景（stage 本身，不是影像）也關閉 —— 與 overlay 的背板一致
    else if (shouldCloseOnClick({ downTarget, dragged, stage, root: el })) close();
  });

  // `passive: false` 是必要的：預設 wheel 是 passive，preventDefault 會被忽略，
  // 結果是縮放的同時背景跟著捲。
  stage.addEventListener('wheel', (ev) => {
    if (!img) return;          // 影片不縮放（會跟原生控制列打架）
    ev.preventDefault();
    // 依 deltaY 的**大小**縮放，不是每個事件固定一格。
    // 滑鼠滾輪一格是 deltaY≈100（正好 1.1 倍），觸控板一次會送出很多個
    // 很小的 delta —— 固定一格的話觸控板會瞬間放到最大。
    // deltaMode 1 是「以行為單位」（某些滑鼠驅動），一行約 16px。
    const px = ev.deltaMode === 1 ? ev.deltaY * 16 : ev.deltaY;
    zoomAt(ev.clientX, ev.clientY, WHEEL_STEP ** (-px / 100));
  }, { passive: false });

  stage.addEventListener('dblclick', () => {
    if (!img) return;
    if (Math.abs(fit * scale - 1) < 0.01) computeFit();   // 已經 100% → 回 fit
    else { scale = 1 / fit; tx = 0; ty = 0; paint(); }     // 否則跳到 100%
  });

  // pointerdown 掛在 el（不是 stage）上：click 的判斷需要知道「按下去的當下
  // 手指在哪」，而按鈕與底部狀態列都在 stage 外面。
  el.addEventListener('pointerdown', (ev) => {
    downTarget = ev.target;
    dragged = false;
    if (!img || !stage.contains(ev.target)) return;
    drag = { x: ev.clientX, y: ev.clientY, tx, ty };
    stage.setPointerCapture(ev.pointerId);
  });
  stage.addEventListener('pointermove', (ev) => {
    if (!drag) return;
    const dx = ev.clientX - drag.x;
    const dy = ev.clientY - drag.y;
    // 超過容差才開始真的平移，否則點擊時的手震會讓畫面跳一下。
    if (!dragged && Math.hypot(dx, dy) <= DRAG_SLOP) return;
    dragged = true;
    tx = drag.tx + dx;
    ty = drag.ty + dy;
    paint();
  });
  const endDrag = () => { drag = null; };
  stage.addEventListener('pointerup', endDrag);
  stage.addEventListener('pointercancel', endDrag);

  // ← → 換張。Esc 不在這裡 —— 它由 overlay.js 的關閉堆疊統一處理，
  // 這樣「一次關一層」的順序才有單一來源。
  el.addEventListener('keydown', (ev) => {
    if (ev.key === 'ArrowLeft') go(-1);
    else if (ev.key === 'ArrowRight') go(1);
  });
  el.tabIndex = -1;
  el.focus();

  window.addEventListener('resize', computeFit);

  return { close };
}
