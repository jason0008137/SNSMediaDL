// pixiv 動圖（ugoira）播放器：canvas 逐格畫，不轉檔。
//
// ugoira 下載下來是一包 zip 裝一堆 jpg，瀏覽器沒有任何原生元素吃得下它。
// 後端把它拆成「幀表 + 逐格圖片」（`api/ugoira.py`），這裡負責照時間表畫。
//
// **不引入任何套件**：前端是零建置的原生 ES module，加一個套件就得加一條
// 建置流程 —— 而後端逐格供應之後，這裡根本不需要看得懂 zip。

import { api } from './api.js';
import { esc } from './dom.js';
import { t } from './i18n.js';

// 預載視窗。往前 8 格 ≈ 25 fps 下的 0.3 秒，足夠蓋過本機磁碟的抖動；
// 往後留 1 格是為了循環回繞的那一瞬間不會空白。
//
// ⚠️ **不可以改成「全部預載」。** 解碼後的 ImageBitmap 是未壓縮的 RGBA：
// 一格 800×566 約 1.8 MB，96 格就是 174 MB，而 1920×1080 的 600 格動圖
// 會到 5 GB。原始 jpg 只有 8.6 MB —— 差距全在「解碼後」這三個字。
const AHEAD = 8;
const BEHIND = 1;

/** 每格的**結束**時間（累積毫秒）。`frames` 是 `[{delay}, …]`。
 *
 *  分出來是純函式，因為時間推進正是這支模組唯一會算錯的東西
 *  （見 `test_ugoira.mjs`）。 */
export function buildCumulative(frames) {
  const out = [];
  let acc = 0;
  for (const f of frames) {
    acc += f.delay;
    out.push(acc);
  }
  return out;
}

/** 播到第 `elapsedMs` 毫秒時，畫面上該是第幾格？
 *
 *  ⚠️ **依時間查表，不是每格排一個 setTimeout。** setTimeout 的誤差會累積，
 *  而且分頁切到背景時被瀏覽器降頻 —— 回來會慢半拍，且再也追不回來。
 *  依時間查表的另一個好處是**落後時自動跳格**：維持總時長正確，比每一格
 *  都畫出來重要。
 *
 *  `loop: false` 時走到底停在最後一格（不是消失、也不是回到第一格）。 */
export function frameAt(cumulative, elapsedMs, { loop = true } = {}) {
  const last = cumulative.length - 1;
  if (last < 0) return 0;
  const total = cumulative[last];
  // 總長 0（每格 delay 都是 0）不該讓 `% total` 變成 NaN
  if (!(total > 0)) return 0;

  let e = elapsedMs;
  if (e < 0) e = 0;
  if (e >= total) {
    if (!loop) return last;
    // 取餘數而不是自己減一輪 —— elapsed 可能已經超過好幾輪
    // （分頁在背景時 rAF 不跑，回來時一次跳很多）
    e %= total;
  }
  // 格數不多，線性掃就夠；二分搜尋在這個量級只是多一份會寫錯的程式碼
  for (let i = 0; i <= last; i += 1) {
    if (e < cumulative[i]) return i;
  }
  return last;
}

/** 建一個播放器。
 *
 *  `mediaId`   要播哪一筆
 *  `controls`  true 會回一個控制列（放大檢視器要，詳情面板不要 ——
 *              ugoira 語意上就是 GIF，給控制列反而不對）
 *  `autoplay`  載好就開始播
 *  `onReady`   第一格畫出來時通知（呼叫端要重算縮放）
 *  `onError`   載入失敗。**訊息用後端給的**，這裡不自己編一個原因
 *
 *  回 `{ canvas, bar, play, pause, seek, destroy }`。
 *  `canvas` 與 `bar` 由呼叫端各自安置 —— 包成一個 wrapper 的話，
 *  放大檢視器對它做 transform 會連控制列一起縮放。 */
export function createUgoiraPlayer({
  mediaId, controls = false, autoplay = true, onReady, onError,
} = {}) {
  const canvas = document.createElement('canvas');
  canvas.className = 'ugoira-canvas';
  const ctx = canvas.getContext('2d');

  const abort = new AbortController();

  let cumulative = [];
  let total = 0;
  let loop = true;
  let playing = false;
  let dead = false;
  let elapsed = 0;       // 播到第幾毫秒（不是 wall clock）
  let lastTick = 0;
  let raf = 0;
  let drawn = -1;        // 目前畫在 canvas 上的是第幾格
  let scrubbing = false;
  const bitmaps = new Map();   // index → ImageBitmap
  const inflight = new Map();  // index → Promise

  // ── 控制列 ──────────────────────────────────────────

  function buildBar() {
    const el = document.createElement('div');
    el.className = 'ugoira-bar';
    el.innerHTML = `
      <button type="button" class="ghost" data-ug="toggle"
              aria-label="${esc(t('ugoira.play'))}">&#9654;</button>
      <input type="range" class="ugoira-seek" data-ug="seek" min="0" max="0" value="0"
             aria-label="${esc(t('ugoira.seek.aria'))}">
      <span class="ugoira-count" data-ug="count"></span>
      <label class="ugoira-loop"><input type="checkbox" data-ug="loop" checked>
        ${esc(t('ugoira.loop'))}</label>`;
    return el;
  }

  const bar = controls ? buildBar() : null;

  function paintBar() {
    if (!bar) return;
    const btn = bar.querySelector('[data-ug="toggle"]');
    btn.textContent = playing ? '❚❚' : '▶';
    btn.setAttribute('aria-label', t(playing ? 'ugoira.pause' : 'ugoira.play'));
    if (!scrubbing) bar.querySelector('[data-ug="seek"]').value = String(elapsed);
    bar.querySelector('[data-ug="count"]').textContent = t('ugoira.frame', {
      n: (drawn < 0 ? 0 : drawn + 1), total: cumulative.length,
    });
  }

  // ── 取格 ────────────────────────────────────────────

  function want(i) {
    if (dead || bitmaps.has(i) || inflight.has(i)) return;
    const p = fetch(`/api/media/${mediaId}/ugoira/${i}`, { signal: abort.signal })
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.blob();
      })
      .then((b) => createImageBitmap(b))
      .then((bm) => {
        // destroy() 與這個 promise 賽跑：晚到的 bitmap 必須自己關掉，
        // 否則它會一直佔著記憶體直到 GC（而 GC 不保證回收 ImageBitmap）
        if (dead) { bm.close(); return; }
        bitmaps.set(i, bm);
      })
      .catch((e) => {
        if (e.name !== 'AbortError' && !dead) onError?.(e);
      })
      .finally(() => inflight.delete(i));
    inflight.set(i, p);
  }

  /** 只留視窗內的格，其餘立刻 close。 */
  function slide(centre) {
    const n = cumulative.length;
    if (!n) return;
    for (let d = -BEHIND; d <= AHEAD; d += 1) want(((centre + d) % n + n) % n);
    for (const [i, bm] of bitmaps) {
      const gap = ((i - centre) % n + n) % n;
      if (gap > AHEAD && gap < n - BEHIND) {
        bm.close();
        bitmaps.delete(i);
      }
    }
  }

  function draw(i) {
    const bm = bitmaps.get(i);
    if (!bm) return false;
    if (canvas.width !== bm.width || canvas.height !== bm.height) {
      canvas.width = bm.width;
      canvas.height = bm.height;
    }
    ctx.drawImage(bm, 0, 0);
    drawn = i;
    return true;
  }

  // ── 推進 ────────────────────────────────────────────

  function tick(now) {
    raf = 0;
    if (dead) return;
    const next = elapsed + (now - lastTick);
    lastTick = now;

    const i = frameAt(cumulative, next, { loop });
    if (bitmaps.has(i)) {
      // ⚠️ 只有畫得出來才把時間記進去。畫不出來時**時間不前進** ——
      // 否則磁碟卡一下，動畫會靜止一秒然後跳過那一秒的內容。
      elapsed = next;
      draw(i);
    }
    slide(i);
    paintBar();

    if (!loop && next >= total) { playing = false; paintBar(); return; }
    if (playing) raf = requestAnimationFrame(tick);
  }

  function play() {
    if (dead || playing || !cumulative.length) return;
    if (!loop && elapsed >= total) elapsed = 0;   // 播完再按 = 重播
    playing = true;
    lastTick = performance.now();
    raf = requestAnimationFrame(tick);
    paintBar();
  }

  function pause() {
    playing = false;
    if (raf) { cancelAnimationFrame(raf); raf = 0; }
    paintBar();
  }

  /** 分頁切到背景時瀏覽器**完全不跑 rAF**（2026-08-22 實測：`document.hidden`
   *  為真時 500 ms 內 0 次）。回到前景的第一個 tick 會拿到「離開了多久」
   *  那麼大的 delta，於是動畫一瞬間跳到很後面的一格，還連帶把整個預載視窗
   *  換掉、重抓十張圖。
   *
   *  回來時把計時基準歸零 —— 動畫從離開的地方接著播。 */
  function onVisibility() {
    if (!document.hidden) lastTick = performance.now();
  }
  document.addEventListener('visibilitychange', onVisibility);

  function seek(ms) {
    if (!cumulative.length) return;
    elapsed = Math.max(0, Math.min(ms, total));
    const i = frameAt(cumulative, elapsed, { loop });
    slide(i);
    if (!draw(i)) {
      // 還沒解到那一格 —— 等它到了再畫，不要留著上一格假裝已經跳過去了。
      //
      // ⚠️ **畫完要重畫控制列。** 少了這一步，畫面換成新的一格、幀號卻還停在
      // 上一格（下面那次 `paintBar()` 是在畫成功之前跑的）—— 2026-08-22 實測
      // 拖曳進度條時逐次落後一格，看起來像是「拖了但沒反應」。
      inflight.get(i)?.then(() => {
        if (!dead && drawn !== i && draw(i)) paintBar();
      });
    }
    lastTick = performance.now();
    paintBar();
  }

  function destroy() {
    dead = true;
    playing = false;
    if (raf) { cancelAnimationFrame(raf); raf = 0; }
    document.removeEventListener('visibilitychange', onVisibility);
    // 在途的請求要中止 —— 不然關掉面板之後還會繼續抓完剩下的 90 格
    abort.abort();
    for (const bm of bitmaps.values()) bm.close();
    bitmaps.clear();
    inflight.clear();
    canvas.remove();
    bar?.remove();
  }

  if (bar) {
    bar.addEventListener('click', (ev) => {
      if (ev.target.closest('[data-ug="toggle"]')) (playing ? pause : play)();
    });
    bar.addEventListener('change', (ev) => {
      if (ev.target.matches('[data-ug="loop"]')) loop = ev.target.checked;
    });
    // 拖曳時先停，放開才續播 —— 邊拖邊播會跟拖曳打架
    bar.addEventListener('pointerdown', (ev) => {
      if (!ev.target.matches('[data-ug="seek"]')) return;
      scrubbing = playing ? 'was-playing' : true;
      if (playing) pause();
    });
    bar.addEventListener('input', (ev) => {
      if (ev.target.matches('[data-ug="seek"]')) seek(Number(ev.target.value));
    });
    const stopScrub = () => {
      if (!scrubbing) return;
      const resume = scrubbing === 'was-playing';
      scrubbing = false;
      seek(Number(bar.querySelector('[data-ug="seek"]').value));
      if (resume) play();
    };
    bar.addEventListener('pointerup', stopScrub);
    bar.addEventListener('pointercancel', stopScrub);
  }

  // ── 起手 ────────────────────────────────────────────

  (async () => {
    let meta;
    try {
      meta = await api(`/api/media/${mediaId}/ugoira`);
    } catch (e) {
      // ⚠️ 訊息用後端給的（`api.js` 已經查過 `err.<code>` 表）。
      // 前端自己編一個原因的話，「缺幀資料」會被說成「檔案讀不到」。
      onError?.(e);
      return;
    }
    if (dead) return;
    cumulative = buildCumulative(meta.frames);
    total = cumulative[cumulative.length - 1] || 0;
    if (bar) bar.querySelector('[data-ug="seek"]').max = String(total);

    slide(0);
    await inflight.get(0);
    if (dead) return;
    if (draw(0)) onReady?.({ width: canvas.width, height: canvas.height });
    paintBar();
    if (autoplay) play();
  })();

  return { canvas, bar, play, pause, seek, destroy };
}
