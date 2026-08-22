// 放大檢視器：原檔、滾輪縮放、拖曳平移、同貼文換張。
//
// **不引入任何 lightbox 套件**：這個前端是零建置的原生 ES module，加一個
// 套件就得加一條建置流程，而縮放與平移本身是幾十行的事。

import { esc, fileErrorText, fmtBytes } from './dom.js';
import { t } from './i18n.js';
import { pushDismissable } from './overlay.js';
import { createUgoiraPlayer } from './ugoira.js';

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
 *  `siblings` 同貼文的其他張（`[{id, kind}]`），用來左右換張。
 *            **kind 要帶**，否則換張時只能用試錯法猜型別
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
            aria-label="${esc(t('viewer.close.aria'))}">×</button>
    <button type="button" class="viewer-nav prev" data-act="prev" aria-label="${esc(t('viewer.prev.aria'))}">◀</button>
    <button type="button" class="viewer-nav next" data-act="next" aria-label="${esc(t('viewer.next.aria'))}">▶</button>
    <div class="viewer-stage" data-act="stage"></div>
    <div class="viewer-bar">
      <span class="dims" data-role="dims">${esc(t('viewer.loading'))}</span>
      <span class="spacer"></span>
      <span class="zoom" data-role="zoom"></span>
      <button type="button" class="ghost" data-act="reset">${esc(t('viewer.reset'))}</button>
    </div>
    <p class="viewer-hint${localStorage.getItem(HINT_SEEN_KEY) ? ' hidden' : ''}">
      ${esc(t('viewer.hints'))}</p>`;

  document.getElementById('overlayRoot').appendChild(el);
  localStorage.setItem(HINT_SEEN_KEY, '1');

  const stage = el.querySelector('[data-act="stage"]');
  const bar = el.querySelector('.viewer-bar');
  const dimsEl = el.querySelector('[data-role="dims"]');
  const zoomEl = el.querySelector('[data-role="zoom"]');
  // ⚠️ **要留住 kind，不要只取 id。** 後端 `query.py` 的 siblings 本來就在回
  // kind —— 舊版在這裡 `.map((s) => s.id)` 把它丟掉，於是換張時只能「先試
  // `<img>`，失敗再換 `<video>`」：每看一次影片都先付一次失敗的原檔請求，
  // 而 ugoira 是**兩種都失敗**，最後顯示「讀不到」。
  const items = siblings.length
    ? siblings.map((s) => ({ id: s.id, kind: s.kind }))
    : [{ id: media.id, kind: media.kind }];
  let index = Math.max(0, items.findIndex((s) => s.id === media.id));

  // 只有一張時**不顯示**換張按鈕（不是 disable）：這裡沒有「等一下就能用」
  // 的可能，disable 只會讓人一直想去按。
  el.classList.toggle('single', items.length <= 1);

  const handle = pushDismissable({ close: () => close() });

  function close() {
    handle.release();
    // ugoira 是 rAF + 逐格 fetch，元素移除**不會**讓它停下來
    destroyPlayer();
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
  // `img` 是「被縮放的那個元素」，可能是 `<img>` 也可能是 ugoira 的
  // `<canvas>` —— 兩者的原始尺寸屬性名不同，一律走這兩支取。
  const natW = (n) => n?.naturalWidth ?? n?.width ?? 0;
  const natH = (n) => n?.naturalHeight ?? n?.height ?? 0;
  let img = null;
  let player = null;

  function destroyPlayer() {
    player?.destroy();
    player = null;
  }

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
    if (!img || !natW(img)) return;
    const box = stage.getBoundingClientRect();
    fit = Math.min(box.width / natW(img), box.height / natH(img), 1);
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
  /** `item` 是 `{id, kind}`。kind 拿得到就直接分派三條路的其中一條 ——
   *  **不再賭「先試圖片，失敗再試影片」**：那條路對 ugoira 是兩種都失敗，
   *  而對影片是每次都先付一次註定失敗的原檔請求。 */
  function load(item) {
    stage.innerHTML = '';
    destroyPlayer();
    img = null;
    zoomEl.textContent = '';
    dimsEl.textContent = t('viewer.loading');

    const kind = item.kind ?? (item.id === media.id ? media.kind : null);
    if (kind === 'ugoira') loadUgoira(item.id);
    else if (kind && kind !== 'photo') loadVideo(item.id);
    else loadImage(item.id, kind);
  }

  function loadImage(mediaId, kind) {
    const node = document.createElement('img');
    node.src = `/api/media/${mediaId}/file`;
    node.alt = '';
    node.addEventListener('load', () => {
      img = node;
      dimsEl.textContent = `${node.naturalWidth} × ${node.naturalHeight}`
        + (media.id === mediaId && media.bytes ? ` · ${fmtBytes(media.bytes)}` : '');
      computeFit();
    });
    node.addEventListener('error', () => {
      // kind 已知是 photo 就是真的讀不到；完全不知道 kind（舊呼叫端沒帶）
      // 才退回舊行為，換成播放器再試一次。
      if (kind) { fail(mediaId); return; }
      stage.innerHTML = '';
      loadVideo(mediaId);
    });
    stage.appendChild(node);
  }

  function loadVideo(mediaId) {
    const v = videoNode(mediaId);
    v.addEventListener('error', () => fail(mediaId));
    dimsEl.textContent = t('viewer.video');
    stage.appendChild(v);
  }

  /** ugoira：zip 裝一堆 jpg，沒有任何原生元素吃得下。canvas 逐格畫。
   *
   *  ⚠️ 這裡**給控制列**，與詳情面板不同。詳情面板是「順便看一眼」，
   *  進到放大檢視器就是要細看 —— 逐格看是真需求。
   *  控制列放進 viewer-bar，**不能包進 stage** —— stage 裡那個元素會被
   *  縮放與平移，控制列跟著縮放就沒法按了。 */
  function loadUgoira(mediaId) {
    player = createUgoiraPlayer({
      mediaId,
      controls: true,
      autoplay: true,
      onReady: ({ width, height }) => {
        img = player.canvas;
        dimsEl.textContent = `${width} × ${height}`
          + (media.id === mediaId && media.bytes ? ` · ${fmtBytes(media.bytes)}` : '');
        computeFit();
      },
      // 後端說得出原因（缺幀資料 / 格數對不上），照它說的顯示。
      onError: (e) => failWith(e.message),
    });
    stage.appendChild(player.canvas);
    bar.insertBefore(player.bar, bar.querySelector('.spacer'));
  }

  /** 把 stage 換成一句話。回傳那個節點，讓呼叫端還能改寫它。 */
  function failWith(msg) {
    const box = document.createElement('p');
    box.className = 'viewer-missing';
    box.textContent = msg;
    stage.innerHTML = '';
    stage.appendChild(box);
    dimsEl.textContent = '—';
    zoomEl.textContent = '';
    img = null;
    return box;
  }

  async function fail(mediaId) {
    const box = failWith(t('viewer.missing'));
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
    if (items.length <= 1) return;
    index = (index + step + items.length) % items.length;
    load(items[index]);
    onSwitch?.(items[index].id);
  }

  load(items[index]);

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
