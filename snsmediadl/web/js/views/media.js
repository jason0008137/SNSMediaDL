// 媒體瀏覽：篩選、格線、分頁、詳情面板、選取與批次。

import { $, esc, fmtBytes, starsHtml, wireStars } from '../dom.js';
import { api } from '../api.js';
import { PAGE, state, safeMode, onSafeModeChange } from '../state.js';
import { showView, invalidateView } from '../nav.js';
import { RATINGS, CONTENTS, opts } from '../enums.js';

// 安全模式的兩個控制項都在別處（header 與設定面板）。這裡只訂閱結果。
// 人不在媒體頁時不必立刻重查 —— 但快取要作廢，否則切回來會看到一份
// 用舊安全模式篩出來的畫面，而開關卻顯示新的狀態。
onSafeModeChange(() => {
  resetMediaPaging();
  if (state.view === 'media') loadMedia();
  else invalidateView('media');
});

// ── 篩選 ───────────────────────────────────────────────

/** 篩選下拉的定義。標籤文字與取值集中在一處 —— 篩選列、標籤列、
 *  「清除這一個」三處都讀它，各寫一份必然會漂移。 */
const FILTERS = [
  { id: 'fRating', param: 'rating', label: '分級' },
  { id: 'fContent', param: 'content_type', label: '類型' },
  { id: 'fKind', param: 'kind', label: '型別' },
  { id: 'fCreator', param: 'creator_id', label: 'creator', text: (el) => el.selectedOptions[0]?.text },
  { id: 'fStatus', param: 'status', label: '下載狀態' },
  { id: 'fMinStars', param: 'min_stars', label: '評分', text: (v) => `${v} 星以上` },
];

/** 只有篩選條件，不含分頁與排序。**清單與總數共用**，兩邊各組一次會對不上。 */
export function mediaFilters() {
  const p = new URLSearchParams();
  if (safeMode()) p.set('exclude_rating', 'r18');
  if (state.accountFilter) p.set('account_id', state.accountFilter);
  for (const f of FILTERS) {
    const v = $(f.id).value;
    if (v) p.set(f.param, v);
  }
  return p;
}

/** 生效中的條件，給標籤列用。安全模式**不在裡面** —— 它不是篩選列上的條件，
 *  它的狀態在 header，而它擋掉幾筆由筆數那一行負責講。 */
function activeConditions() {
  const out = [];
  if (state.accountFilter) {
    out.push({ kind: 'account', label: '帳號', value: state.accountLabel || state.accountFilter });
  }
  for (const f of FILTERS) {
    const el = $(f.id);
    if (!el.value) continue;
    const value = typeof f.text === 'function'
      ? f.text(f.id === 'fMinStars' ? el.value : el)
      : el.value;
    out.push({ kind: 'select', id: f.id, label: f.label, value });
  }
  return out;
}

function renderChips() {
  const bar = $('chipBar');
  const conds = activeConditions();
  bar.classList.toggle('hidden', conds.length === 0);
  if (!conds.length) { bar.innerHTML = ''; return; }
  bar.innerHTML = '<span class="lead">生效中：</span>'
    + conds.map((c) => `<span class="chip">${esc(c.label)}
        <b>${esc(c.value)}</b>
        <button type="button" data-clear="${esc(c.kind === 'account' ? 'account' : c.id)}"
                aria-label="移除這個條件">×</button></span>`).join('')
    + '<span class="spacer"></span>'
    + '<button type="button" class="ghost small" data-clear="__all__">全部清除</button>';
}

function clearCondition(what) {
  if (what === '__all__') {
    for (const f of FILTERS) $(f.id).value = '';
    setAccountFilter('', '');
  } else if (what === 'account') {
    setAccountFilter('', '');
  } else {
    $(what).value = '';
  }
  // 排序不是篩選 —— 「全部清除」不該把使用者選的排序也一起打掉
  resetMediaPaging();
  loadMedia();
}

$('chipBar').addEventListener('click', (ev) => {
  const btn = ev.target.closest('[data-clear]');
  if (btn) clearCondition(btn.dataset.clear);
});

/** `sort=stars` 的排序鍵是 (stars, id) 複合又含 NULL，後端不支援 keyset。 */
const usesKeyset = () => $('fSort').value !== 'stars';

function mediaQuery() {
  const p = mediaFilters();
  p.set('limit', PAGE);
  p.set('sort', $('fSort').value);
  if (usesKeyset()) {
    // 游標堆疊的最後一個 = 這一頁的起點。第一頁是 null（不帶游標）。
    const cursor = state.cursors[state.cursors.length - 1];
    if (cursor != null) p.set($('fSort').value === 'oldest' ? 'after_id' : 'before_id', cursor);
  } else {
    p.set('offset', state.offset);
  }
  return p.toString();
}

// ── 總數：獨立、非阻塞、可取消 ─────────────────────────
//
// 總數在正式庫上要 1.3 秒（COUNT 掃 224 萬列，安全模式的 exclude_rating
// 選擇性只有 5%，沒有索引救得了）。擋著畫面等它，等於為了一個「共 N 個」
// 讓每次翻頁都卡 1.3 秒。
//
// ⚠️ 算不出來時**不顯示 0**。那是拿假資料填空窗，使用者會以為真的沒東西。

let countAbort = null;

/** 「共 N 個媒體」+ 縮小到多少 + 被安全模式擋掉幾筆。
 *
 *  只給筆數答得出第 5 題（現在是什麼狀態），答不出第 6 題（我的條件是不是
 *  下得太窄）。所以有篩選時要能跟全庫比。 */
function paintCount() {
  const el = $('mediaCount');
  if (state.total == null) return;
  const parts = [`共 <b class="todo">${state.total.toLocaleString()}</b> 個媒體`];
  const filtered = activeConditions().length > 0;
  // 0 筆時不講百分比 —— 「全庫的 0.00%」是噪音，那時該講的是**為什麼是 0**
  // （下面那句「另有 N 筆因安全模式隱藏」與空狀態）。
  if (filtered && state.total && state.libTotal) {
    const pct = (state.total / state.libTotal) * 100;
    parts.push(`<span class="muted">（目前條件下的全庫 ${state.libTotal.toLocaleString()} 的 ${
      pct >= 1 ? pct.toFixed(0) : pct.toFixed(2)}%）</span>`);
  }
  if (state.hiddenBySafe) {
    parts.push(`<span class="hidden-note">另有 ${state.hiddenBySafe.toLocaleString()}`
      + ' 筆因安全模式隱藏</span>');
  }
  el.innerHTML = parts.join(' ');
  el.className = 'count-line muted';
}

async function refreshMediaCount() {
  // 前一個還在跑就取消：慢的那個後到會蓋掉正確結果（換了篩選尤其明顯）
  if (countAbort) countAbort.abort();
  countAbort = new AbortController();
  const signal = countAbort.signal;

  $('mediaCount').textContent = '計算總數…';
  $('mediaCount').className = 'count-line muted';
  state.hiddenBySafe = 0;
  try {
    const data = await api(`/api/media/count?${mediaFilters()}`, { signal });
    if (signal.aborted) return;
    state.total = data.total;
    // 後端只在**結果是 0** 時才付第二次 COUNT 的成本；其餘時候回 null，
    // 而 null 的意思是「沒算」，不是「沒有被擋掉的」。
    state.hiddenBySafe = data.hidden_by_safe_mode || 0;
    // 沒有任何篩選時，這一次的結果就是全庫總數 —— 順手記起來，
    // 之後套篩選才有東西可比，不必額外再掃一次 224 萬列。
    if (!activeConditions().length) state.libTotal = data.total;
    paintCount();
    if (!state.items.length) $('grid').innerHTML = emptyGridHtml();
    if (activeConditions().length && state.libTotal == null) fetchLibTotal();
  } catch (e) {
    if (e.name === 'AbortError') return;
    state.total = null;
    $('mediaCount').textContent = `總數算不出來：${e.message}`;
    $('mediaCount').className = 'count-line muted err';
  }
}

/** 全庫（同一個安全模式下、不帶任何篩選）的總數。只在需要比較時才算，
 *  而且完全不擋畫面 —— 它與主查詢一樣是一次 COUNT。 */
async function fetchLibTotal() {
  const p = new URLSearchParams();
  if (safeMode()) p.set('exclude_rating', 'r18');
  try {
    const d = await api(`/api/media/count?${p}`);
    state.libTotal = d.total;
    paintCount();
  } catch { /* 比較用的數字，算不出來就不顯示百分比，不必報錯 */ }
}

/** 影片與 ugoira 沒有縮圖（那要 ffmpeg）。格線顯示佔位，點開才載入播放器。 */
const PLAYABLE = new Set(['video', 'animated_gif', 'ugoira']);

function cellHtml(m) {
  const missing = m.status !== 'done';
  let body;
  if (missing) {
    body = `<div class="missing">${m.status === 'failed' ? '下載失敗' : '尚未下載'}</div>`;
  } else if (PLAYABLE.has(m.kind)) {
    // ⚠️ **刻意不建立 `<video>` 元素。**
    // 舊版每格掛一個 `preload="metadata"`，一頁 60 格 = 60 次跨磁碟開檔讀
    // moov box，而檔案散在三顆碟上。佔位只花一個 div。
    body = `<div class="placeholder"><span class="play">▶</span>
            <span class="sz">${fmtBytes(m.bytes)}</span></div>`;
  } else {
    // src 留空，由 IntersectionObserver 在捲進視窗時才填（見 wireGridImages）。
    // 縮圖是 320px WebP，不是原檔 —— 正式庫單檔最大 446 MB。
    body = `<img class="thumb" alt="" data-src="/api/media/${m.id}/thumb">`;
  }

  return `<div class="cell st-${esc(m.status)}" data-id="${m.id}" data-post="${m.post_id}">
    ${body}${ratingTagHtml(m.rating)}${
      m.kind === 'photo' ? '' : `<span class="kind">${esc(m.kind)}</span>`}${
      // 只在有評分時顯示。空的星星角標會讓每一格都變吵。
      m.stars ? `<span class="star-badge">${'★'.repeat(m.stars)}</span>` : ''
    }<span class="pick">✓</span>
  </div>`;
}

/** 分級角標。⚠️ **r18 不可以只靠紅底** —— 灰階列印下紅底與黑底幾乎一樣。
 *  文字 + 圖示 + 外框，三重載體。 */
function ratingTagHtml(rating) {
  if (!rating) return '';
  return rating === 'r18'
    ? '<span class="tag r18">⚠ r18</span>'
    : `<span class="tag">${esc(rating)}</span>`;
}

// ── 格線圖片的延遲載入 ────────────────────────────────
//
// `loading="lazy"` 對「一次 60 格且大多在視窗內」幫助有限，瀏覽器仍會很早
// 就全部排隊。用 IntersectionObserver 自己控，捲到才發請求。

let gridObserver = null;

function wireGridImages() {
  if (gridObserver) gridObserver.disconnect();
  gridObserver = new IntersectionObserver((entries, obs) => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      const img = e.target;
      const url = img.dataset.src;
      img.onerror = () => {
        // 縮圖失敗的原因要看得出來，不可以留一個破圖圖示了事。
        // `<img>` 的 error 事件**拿不到狀態碼**，所以補一次 HEAD 去問 ——
        // 只有失敗的那幾格會付這個成本，而分辨得出來很重要：
        //   404 = 這個路徑現在讀不到（檔案被刪了，**或是那顆碟沒插**）
        //   415 = 這個格式生不出縮圖（影片）
        //   500 = 原檔壞了
        // 「被刪了還是碟沒插」是系統模型那五題裡目前答不出來的第 4 題。
        const box = Object.assign(document.createElement('div'), {
          className: 'missing', textContent: '縮圖失敗',
        });
        img.replaceWith(box);
        fetch(url, { method: 'HEAD' }).then((r) => {
          box.textContent = {
            404: '讀不到原檔\n（被刪除，或那顆碟沒插）',
            415: '這個格式生不出縮圖',
            500: '原檔壞了',
          }[r.status] || `縮圖失敗（${r.status}）`;
        }).catch(() => { /* 連 HEAD 都失敗就維持「縮圖失敗」 */ });
      };
      img.src = url;
      obs.unobserve(img);
    }
  }, { rootMargin: '200px' });   // 提前一點開始載，捲動時才不會看到空格

  document.querySelectorAll('#grid img.thumb').forEach((img) => gridObserver.observe(img));
}

// ── 局部更新格線 ──────────────────────────────────────
//
// 詳情面板每存一次檔就重載整頁的話，就是每次都付一次查詢 + 一次 COUNT
// （正式庫 1.3 秒）—— 而畫面上只有一格需要變。

/** 更新某格的星星角標。 */
function patchCellStars(mediaId, stars) {
  const cell = document.querySelector(`.cell[data-id="${mediaId}"]`);
  if (!cell) return;
  cell.querySelector('.star-badge')?.remove();
  // 未評分不顯示角標 —— 每格都掛一個空星星會讓整片格線變吵
  if (stars) {
    cell.insertAdjacentHTML('beforeend',
      `<span class="star-badge">${'★'.repeat(stars)}</span>`);
  }
  const item = state.items.find((x) => x.id === mediaId);
  if (item) item.stars = stars;
}

/** 更新同一則貼文的所有格子的分級標籤。分級掛在 post，會影響同則的每一張。 */
function patchCellsForPost(postId, { rating }) {
  const cells = document.querySelectorAll(`.cell[data-post="${postId}"]`);
  for (const cell of cells) {
    // 安全模式下標成 r18 就該從畫面消失 —— 那正是安全模式的意義。
    // 移除而不是重載：重載會讓使用者正在看的位置整個跳掉。
    if (safeMode() && rating === 'r18') {
      cell.remove();
      if (state.total != null) {
        state.total -= 1;
        paintCount();
      }
      continue;
    }
    cell.querySelector('.tag')?.remove();
    cell.insertAdjacentHTML('beforeend', ratingTagHtml(rating));
  }
  if (!$('grid').querySelector('.cell')) {
    // 整頁都被濾掉了。空白畫面看起來像壞掉，要講出發生了什麼。
    $('grid').innerHTML =
      '<p class="empty">本頁的媒體都被標記後隱藏了。按「⟳」載入下一批。</p>';
  }
}

/** 重設分頁狀態。改篩選／改排序／切安全模式時都要呼叫。 */
export function resetMediaPaging() {
  state.cursors = [null];
  state.offset = 0;
}

/** 空狀態。**要說出為什麼空，不能只說「沒有」**，而且三種情境不可共用一句。
 *
 *  實測抓到的具體情境：帳號頁按下「684 個媒體」跳過來，畫面顯示「共 0 個媒體」。
 *  那個 0 是對的 —— 那個帳號的 684 筆全是 r18，而工作安全模式開著。但使用者
 *  剛剛才看到 684 這個數字，畫面卻空了，他只會覺得功能壞了。 */
function emptyGridHtml() {
  const conds = activeConditions();
  if (safeMode() && state.hiddenBySafe) {
    return `<p class="empty">沒有符合條件的媒體。<br>
      <b>工作安全模式開著</b> —— 符合條件的
      <b>${state.hiddenBySafe.toLocaleString()}</b> 筆 r18 不會顯示在這裡。<br>
      關掉右上角的開關就看得到。</p>`;
  }
  if (safeMode() && state.hiddenBySafe === 0 && conds.length) {
    return `<p class="empty">沒有符合條件的媒體，安全模式也沒有擋掉任何一筆。<br>
      試著移除一些條件：${esc(conds.map((c) => `${c.label} ${c.value}`).join('、'))}</p>`;
  }
  if (conds.length) {
    return `<p class="empty">沒有符合條件的媒體。試著移除一些條件：<br>
      ${esc(conds.map((c) => `${c.label} ${c.value}`).join('、'))}</p>`;
  }
  return '<p class="empty">還沒有任何媒體。到「抓取」分頁貼幾個帳號網址開始。</p>';
}

// 請求序號。**慢的那個後到會蓋掉正確結果** —— 這不是理論問題：
// 從帳號頁按「N 個媒體」時，切分頁本身會先發一次未篩選的請求（60 筆、要讀
// 60 張縮圖），接著才是篩選後的請求（可能 0 筆、瞬間回來）。先發的後到，
// 畫面就會停在**上一個帳號的內容**，而篩選標籤卻寫著新帳號。
let mediaReqSeq = 0;

/** 載入格線。`withCount` 為 false 時不重算總數（翻頁時總數沒變）。 */
export async function loadMedia({ withCount = true } = {}) {
  if (!state.cursors) resetMediaPaging();
  renderChips();
  if (withCount) refreshMediaCount();   // 刻意不 await —— 畫面不等它

  const seq = ++mediaReqSeq;
  // 載入中就把翻頁鎖住（見 pageTurn）。用 disabled 而不是靜默忽略 ——
  // 「按了沒反應」正是要避免的那種失敗。
  state.loadingMedia = true;
  $('prevPage').disabled = true;
  $('nextPage').disabled = true;
  // 載入中保留上一批內容 + 上方細進度條。**不清空** —— 清空會讓畫面跳動，
  // 而且切換條件時使用者常常想比對。
  $('grid').classList.add('loading');
  try {
    const data = await api(`/api/media?${mediaQuery()}`);
    if (seq !== mediaReqSeq) return;    // 有更新的請求出去了，這份已經過期
    state.items = data.items;
    state.hasMore = data.has_more;
    state.nextCursor = data.next_before_id ?? data.next_after_id ?? null;
    $('grid').innerHTML = data.items.length
      ? data.items.map(cellHtml).join('')
      : emptyGridHtml();

    // 頁碼用「第 N 頁」而不是「1–60 / 總數」—— keyset 分頁下 offset 不存在，
    // 而總數是另一個請求、可能還沒到。
    const page = usesKeyset() ? state.cursors.length : Math.floor(state.offset / PAGE) + 1;
    $('pageInfo').textContent = data.items.length
      ? `第 ${page} 頁　本頁 ${data.items.length} 個`
      : '沒有資料';
    $('prevPage').disabled = usesKeyset() ? state.cursors.length <= 1 : state.offset === 0;
    $('nextPage').disabled = !data.has_more;

    for (const c of document.querySelectorAll('#grid .cell')) {
      if (state.picked.has(Number(c.dataset.id))) c.classList.add('picked');
    }
    wireGridImages();
    updateSelInfo();
  } catch (e) {
    if (seq === mediaReqSeq) {
      $('grid').innerHTML = `<p class="empty">載入失敗：${esc(e.message)}</p>`;
      // 失敗也要把翻頁放開 —— 否則使用者被卡在一個壞掉的畫面上，
      // 連退回上一頁都做不到
      $('prevPage').disabled = usesKeyset() ? state.cursors.length <= 1 : !state.offset;
      $('nextPage').disabled = false;
    }
  } finally {
    // 只有最後一個請求負責解鎖 —— 舊的那個解鎖會讓還在飛的那次變成可按
    if (seq === mediaReqSeq) {
      state.loadingMedia = false;
      $('grid').classList.remove('loading');
    }
  }
}

// ── 選取 ───────────────────────────────────────────────
//
// ⚠️ 格線用**事件委派**：一個 listener 掛在 #grid 上，不是每格各一個。
// 一頁 60 格看不出差別，但同一份程式在帳號頁是 100 張卡 × 好幾顆按鈕，
// 而且每次重畫都會再綁一輪。
$('grid').addEventListener('click', (ev) => {
  const cell = ev.target.closest('.cell');
  if (!cell) return;
  const id = Number(cell.dataset.id);
  const index = state.items.findIndex((m) => m.id === id);
  if (state.selecting) togglePick(id, index, ev.shiftKey);
  else showDetail(id);
});

function togglePick(id, index, shift) {
  if (shift && state.lastPickIndex !== null) {
    const [a, b] = [state.lastPickIndex, index].sort((x, y) => x - y);
    for (let i = a; i <= b; i++) state.picked.add(state.items[i].id);
  } else {
    if (state.picked.has(id)) state.picked.delete(id);
    else state.picked.add(id);
    state.lastPickIndex = index;
  }
  document.querySelectorAll('#grid .cell').forEach((c) => {
    c.classList.toggle('picked', state.picked.has(Number(c.dataset.id)));
  });
  updateSelInfo();
}

/** 選取的是媒體，但分級掛在 post —— 要把兩個數字都講清楚，
 *  否則使用者選了同一貼文的三張圖，會以為自己只改了三張其中一張。 */
function pickedPostIds() {
  const ids = new Set();
  for (const m of state.items) if (state.picked.has(m.id)) ids.add(m.post_id);
  return [...ids];
}

function updateSelInfo() {
  const n = state.picked.size;
  const posts = pickedPostIds().length;
  $('selInfo').textContent = n
    ? `已選 ${n} 個媒體（${posts} 則貼文）`
    : '已選 0 個媒體';
  $('bulkApply').disabled = !n;
  $('bulkApply').dataset.tip = n ? '' : '請先選取媒體';
}

$('selectMode').addEventListener('click', () => {
  state.selecting = !state.selecting;
  document.body.classList.toggle('selecting', state.selecting);
  $('selBar').classList.toggle('hidden', !state.selecting);
  $('selectMode').textContent = state.selecting ? '離開選取' : '選取模式';
  if (!state.selecting) {
    state.picked.clear();
    state.lastPickIndex = null;
    document.querySelectorAll('.cell.picked').forEach((c) => c.classList.remove('picked'));
  }
  updateSelInfo();
});

$('selAll').addEventListener('click', () => {
  state.items.forEach((m) => state.picked.add(m.id));
  document.querySelectorAll('#grid .cell').forEach((c) => c.classList.add('picked'));
  updateSelInfo();
});

$('selNone').addEventListener('click', () => {
  state.picked.clear();
  state.lastPickIndex = null;
  document.querySelectorAll('.cell.picked').forEach((c) => c.classList.remove('picked'));
  updateSelInfo();
});

function bulkMsg(text, cls = '') {
  $('bulkMsg').textContent = text;
  $('bulkMsg').className = `muted ${cls}`;
  if (text && cls === 'ok') {
    setTimeout(() => { if ($('bulkMsg').textContent === text) $('bulkMsg').textContent = ''; }, 3000);
  }
}

function buildBulkBody() {
  const body = { post_ids: pickedPostIds() };
  const r = $('bulkRating').value;
  const c = $('bulkContent').value;
  if (r) body.rating = r === '__clear__' ? null : r;
  if (c) body.content_type = c === '__clear__' ? null : c;
  return body;
}

/** 批次評分的 body，或 null（沒選）。
 *
 *  與分級的關鍵差別：**stars 掛在 media，不掛 post**，所以送的是 media_ids
 *  而不是去重過的 post_ids。選了同則貼文的三張圖就是改三張，不會波及第四張。 */
function buildBulkStarsBody() {
  const s = $('bulkStars').value;
  if (!s) return null;
  return { media_ids: [...state.picked], stars: s === '__clear__' ? null : Number(s) };
}

$('bulkApply').addEventListener('click', () => {
  const body = buildBulkBody();
  const starsBody = buildBulkStarsBody();
  if (!state.picked.size) return;
  const hasTags = 'rating' in body || 'content_type' in body;
  if (!hasTags && !starsBody) {
    bulkMsg('請先選擇要套用的分級、類型或評分。', 'err');
    return;
  }
  // 行內確認，不用 confirm()。兩種作用範圍不同，必須分開講 —— 使用者只選了
  // 3 張圖卻改到 1 則貼文的分級，那是他該事先知道的事。
  const parts = [];
  if (hasTags) parts.push(`分級／類型 → ${body.post_ids.length} 則貼文（含沒選到的張數）`);
  if (starsBody) parts.push(`評分 → ${state.picked.size} 個媒體`);
  $('bulkConfirmText').textContent = `${parts.join('；')}？`;
  $('bulkConfirm').classList.remove('hidden');
  $('bulkApply').disabled = true;
  bulkMsg('');
});

$('bulkNo').addEventListener('click', () => {
  $('bulkConfirm').classList.add('hidden');
  $('bulkApply').disabled = false;
});

$('bulkYes').addEventListener('click', async () => {
  const body = buildBulkBody();
  const starsBody = buildBulkStarsBody();
  const hasTags = 'rating' in body || 'content_type' in body;
  $('bulkConfirm').classList.add('hidden');
  bulkMsg('套用中…');
  try {
    const done = [];
    if (hasTags) {
      const res = await api('/api/posts/bulk-tags', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body),
      });
      done.push(`${res.updated} 則貼文的分級`);
    }
    if (starsBody) {
      const res = await api('/api/media/bulk-stars', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(starsBody),
      });
      done.push(`${res.updated} 個媒體的評分`);
    }
    state.picked.clear();
    state.lastPickIndex = null;
    $('bulkRating').value = '';
    $('bulkContent').value = '';
    $('bulkStars').value = '';
    await loadMedia();
    bulkMsg(`已更新 ${done.join(' 與 ')}`, 'ok');
  } catch (e) {
    // 兩個請求是分開送的，前一個可能已經成功了 —— 不要回報成整批失敗
    bulkMsg(`批次套用失敗：${e.message}（部分變更可能已生效，請重新整理確認）`, 'err');
    await loadMedia();
  } finally {
    $('bulkApply').disabled = false;
  }
});

$('refresh').addEventListener('click', () => loadMedia());

for (const f of FILTERS) {
  $(f.id).addEventListener('change', () => { resetMediaPaging(); loadMedia(); });
}

$('fSort').addEventListener('change', () => {
  localStorage.setItem('mediaSort', $('fSort').value);
  resetMediaPaging();
  loadMedia();
});

// ── 「更多篩選」裡那三個在真實資料上鑑別力為零的下拉 ──
//
// ⚠️ 收起來**不等於**解決。空的下拉是「假預設用途」：看起來可選，點開空無一物。
// 誠實的做法是直接寫出為什麼是空的。這幾個數字只在使用者展開時才去問 ——
// 沒展開就不付這個成本。
let moreNotesLoaded = false;

$('fMore').addEventListener('toggle', () => {
  if (!$('fMore').open || moreNotesLoaded) return;
  moreNotesLoaded = true;
  paintMoreNotes();
  // 評分：`stars >= 1` 走 (stars, id) 索引，全 NULL 時是空掃描，很便宜
  api('/api/media/count?min_stars=1')
    .then((d) => {
      $('fStarsNote').textContent = d.total
        ? `目前有 ${d.total.toLocaleString()} 個已評分`
        : '尚未評分任何項目 —— 選了會是 0 筆';
    })
    .catch(() => { $('fStarsNote').textContent = ''; });
});

/** creator 與下載狀態的說明。兩者的資料本來就在手上（creators 清單、
 *  輪詢中的佇列狀態），不必額外請求。 */
export function paintMoreNotes() {
  const n = state.creators.length;
  $('fCreatorNote').textContent = n ? `目前 ${n} 位` : '目前 0 位 —— 還沒建過創作者';
  const q = state.queue;
  if (!q) { $('fStatusNote').textContent = ''; return; }
  const active = (q.pending || 0) + (q.downloading || 0) + (q.failed || 0);
  $('fStatusNote').textContent = active
    ? `待下載 ${q.pending || 0}／失敗 ${q.failed || 0}`
    : '目前沒有待下載或失敗的項目';
}

// 翻頁不重算總數 —— 條件沒變，總數就沒變，而它要 1.3 秒。
//
// ⚠️ **載入中不接受翻頁。** keyset 的下一頁游標是「這一頁最後一筆的 id」，
// 而那個值要等回應回來才知道。上一頁還在飛的時候按下一頁，推進去的是
// **上上頁**的游標 —— 實測結果是游標堆疊變成
// `[null, 1273350, 1273290, 1273290, 1273290]`：頁碼顯示第 5 頁，畫面上是第 3 頁。
function pageTurn(fn) {
  if (state.loadingMedia) return;
  fn();
  loadMedia({ withCount: false });
}

$('prevPage').addEventListener('click', () => pageTurn(() => {
  if (usesKeyset()) {
    if (state.cursors.length > 1) state.cursors.pop();
  } else {
    state.offset = Math.max(0, state.offset - PAGE);
  }
}));

$('nextPage').addEventListener('click', () => pageTurn(() => {
  if (usesKeyset()) {
    if (state.nextCursor == null) return;
    state.cursors.push(state.nextCursor);
  } else {
    state.offset += PAGE;
  }
}));

// ── 詳情面板 ───────────────────────────────────────────

/** `rating_source` 的誠實顯示。
 *
 *  ⚠️ 正式庫 163 萬則貼文的 `rating_source` 是 `manual`，但那是**匯入器寫的**，
 *  不是人工確認過的。直接顯示「manual」會讓使用者以為「我標過了，不用再看」。
 *  真正的修法是匯入器改寫一個 `import` 值 —— 那是資料層改動，另案處理，
 *  這裡先用不會騙人的文案。 */
function sourceText(src) {
  if (!src) return '尚未標記';
  if (src === 'manual') return 'manual（人工或匯入時分類 —— 目前分不出來）';
  if (src === 'auto') return 'auto（機器猜測，尚未人工確認）';
  if (src === 'account_default') return 'account_default（套用帳號預設值）';
  return src;
}

export async function showDetail(mediaId) {
  $('detail').classList.remove('hidden');
  $('detailBody').innerHTML = '<p class="muted">載入中…</p>';

  let detail;
  try {
    detail = await api(`/api/media/${mediaId}`);
  } catch (e) {
    $('detailBody').innerHTML = `<p class="muted">載入失敗：${esc(e.message)}</p>`;
    return;
  }
  const m = detail.media;
  const p = detail.post;
  const acct = detail.account;
  const sibs = detail.siblings || [];
  // ⚠️ 預覽讀不到原檔時**不能只留一個空白框**。DB 記的 `local_path` 是匯入
  // 當下記下的字串，從沒驗證過檔案還在不在 —— 而 224 萬筆記錄指向三顆碟，
  // 「碟沒插」與「檔案被刪了」在畫面上長得一模一樣。至少要說讀不到。
  const preview = m.status === 'done'
    ? (m.kind === 'photo'
        ? `<img src="/api/media/${m.id}/file" alt="" id="dPreview">`
        : `<video src="/api/media/${m.id}/file" controls preload="metadata" id="dPreview"></video>`)
    : `<p class="muted">狀態：${esc(m.status)}${m.error ? `　${esc(m.error)}` : ''}</p>`;

  const link = acct?.screen_name
    ? `<a href="https://x.com/${esc(acct.screen_name)}/status/${esc(p.platform_post_id)}" target="_blank" rel="noreferrer">在 x.com 開啟</a>`
    : '—';

  $('detailBody').innerHTML = `
    ${preview}

    <!-- 評分掛媒體、分級掛貼文 —— 作用範圍不同，所以是兩個容器，不是兩行。 -->
    <div class="scope-card">
      <h4>這一張</h4>
      <div class="row">
        ${starsHtml(m.stars, 'dStars')}
        <span id="dStarSaved" class="saved"></span>
      </div>
      <p class="note muted">評分只影響這一個媒體。</p>
    </div>

    <div class="scope-card">
      <h4>整則貼文${sibs.length > 1 ? `（${sibs.length} 張）` : ''}</h4>
      <div class="row">
        <select id="dRating">${opts(RATINGS, p.rating)}</select>
        <select id="dContent">${opts(CONTENTS, p.content_type)}</select>
        <span id="dSaved" class="saved"></span>
      </div>
      ${sibs.length > 1
        ? `<p class="note muted">⚠ 改這裡會套用到全部 ${sibs.length} 張。</p>` : ''}
      <div class="src-note" id="dSource">來源：${esc(sourceText(p.rating_source))}</div>
      ${sibs.length > 1 ? `<div class="siblings">${sibs.map((s, i) =>
        `<button type="button" data-sib="${s.id}"
                 class="${s.id === m.id ? 'cur' : ''}">${i + 1}</button>`).join('')}</div>` : ''}
    </div>

    <dl class="kv">
      <dt>貼文</dt><dd>${esc(p.platform_post_id)}</dd>
      <dt>帳號</dt><dd>${esc(acct?.screen_name || p.account_id)}</dd>
      <dt>時間</dt><dd>${esc(p.posted_at || '—')}</dd>
      <dt>型別</dt><dd>${esc(m.kind)}</dd>
      <dt>大小</dt><dd>${fmtBytes(m.bytes)}</dd>
      <dt>狀態</dt><dd>${esc(m.status)}</dd>
      <dt>本機路徑</dt>
      <dd class="path-line">
        <span class="val" id="dPath">${esc(m.local_path || '—')}</span>
        ${m.local_path ? '<button type="button" class="ghost" id="dPathToggle">展開</button>' : ''}
      </dd>
      <dt>SHA-256</dt><dd>${esc((m.file_hash || '—').slice(0, 24))}…</dd>
      <dt>來源</dt><dd>${esc(m.source_url)}</dd>
      <dt>原貼文</dt><dd>${link}</dd>
    </dl>`;

  $('dPreview')?.addEventListener('error', async () => {
    const box = document.createElement('p');
    box.className = 'missing-preview';
    box.textContent = '讀不到原檔。';
    $('dPreview').replaceWith(box);
    try {
      const r = await fetch(`/api/media/${m.id}/file`, { method: 'HEAD' });
      box.textContent = r.status === 404
        ? '讀不到原檔（404）—— 檔案被刪除，或那顆碟沒插。'
          + '\nDB 記的路徑是匯入當下記下的字串，沒有驗證過檔案還在不在。'
        : `讀不到原檔（${r.status}）。`;
    } catch { /* 連 HEAD 都失敗就維持第一句 */ }
  }, { once: true });

  // 正式庫最長路徑 282 字元，含中文與深層巢狀 —— 預設一行截斷，要看才展開。
  $('dPathToggle')?.addEventListener('click', (ev) => {
    const open = $('dPath').classList.toggle('open');
    ev.target.textContent = open ? '收起' : '展開';
  });

  // 同貼文其他張。**194 張是實際存在的最大值**，所以是橫向捲動的一排小按鈕。
  $('detailBody').querySelector('.siblings')?.addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-sib]');
    if (btn) showDetail(Number(btn.dataset.sib));
  });

  // 自動儲存。沒有回饋的自動儲存比手動按鈕更糟 —— 使用者無從確認有沒有生效，
  // 所以成功要有短暫提示，失敗一定要還原並說明。
  const autoSave = async (el, field, previous) => {
    const value = el.value;
    if (value === previous) return;
    const flash = $('dSaved');
    flash.textContent = '儲存中…';
    flash.className = 'saved';
    try {
      const r = await api(`/api/posts/${p.id}/tags`, {
        method: 'PATCH',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ [field]: value || null }),
      });
      p[field] = r[field];
      p.rating_source = r.rating_source;
      $('dSource').textContent = `來源：${sourceText(r.rating_source)}`;
      flash.textContent = '已儲存';
      flash.className = 'saved ok';
      setTimeout(() => { flash.textContent = ''; }, 1600);
      // 只更新受影響的格子，**不重載整頁**。
      patchCellsForPost(p.id, { rating: r.rating });
    } catch (e) {
      el.value = previous;   // 還原，不要讓畫面顯示一個沒存進去的值
      flash.textContent = `儲存失敗：${e.message}`;
      flash.className = 'saved err';
    }
  };

  wireStars(
    $('detailBody').querySelector('.dStars'),
    async (stars) => {
      const flash = $('dStarSaved');
      flash.textContent = '儲存中…';
      flash.className = 'saved';
      await api(`/api/media/${m.id}/stars`, {
        method: 'PATCH',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ stars }),
      });
      m.stars = stars;
      flash.textContent = '已儲存';
      flash.className = 'saved ok';
      setTimeout(() => { flash.textContent = ''; }, 1600);
      // ⚠️ 依評分排序時，改了評分之後**順序其實已經不對了**。不自動重排是
      // 刻意的：格子在腳下跳走比順序暫時不準更糟（見帳號頁 ♥ 的同款決定）。
      patchCellStars(m.id, stars);
    },
    (e) => {
      $('dStarSaved').textContent = `儲存失敗：${e.message}`;
      $('dStarSaved').className = 'saved err';
    },
  );

  const ratingEl = $('dRating');
  const contentEl = $('dContent');
  let lastRating = ratingEl.value;
  let lastContent = contentEl.value;

  ratingEl.addEventListener('change', async () => {
    const prev = lastRating;
    lastRating = ratingEl.value;
    await autoSave(ratingEl, 'rating', prev);
  });
  contentEl.addEventListener('change', async () => {
    const prev = lastContent;
    lastContent = contentEl.value;
    await autoSave(contentEl, 'content_type', prev);
  });
}

function closeDetail() { $('detail').classList.add('hidden'); }
$('closeDetail').addEventListener('click', closeDetail);
document.addEventListener('keydown', (ev) => {
  if (ev.key === 'Escape' && !$('detail').classList.contains('hidden')) closeDetail();
});

// ── 媒體頁的帳號篩選：可搜尋，不預載 ─────────────────
//
// 舊做法是開頁時撈 2,000 個 <option> 塞進 `<select>`。兩個問題：
//   1. 開頁就付一次請求（實測 85 ms）+ 2,000 個 DOM 節點
//   2. 4,653 個帳號的下拉**本來就找不到東西** —— 沒有搜尋，只能滾
//
// 改成 `<input list>` + `<datalist>`：打字才查，一次最多 20 筆。
// 用原生 datalist 而不是自製 combobox —— 鍵盤操作、無障礙、行動裝置
// 都由瀏覽器處理好了，自己做只會做得比較差。

let acctPickTimer = null;
let acctPickAbort = null;

/** 使用者選的帳號 id。datalist 選的是**顯示名稱**，要對回 id。 */
function selectedAccountId() {
  const typed = $('fAccountInput').value.trim();
  if (!typed) return '';
  const hit = state.accountOptions.find(
    (a) => (a.screen_name || a.platform_user_id) === typed);
  return hit ? String(hit.id) : '';
}

async function searchAccountOptions(q) {
  if (acctPickAbort) acctPickAbort.abort();
  acctPickAbort = new AbortController();
  try {
    // with_stats=false：這個下拉只要名字，聚合欄是純浪費
    const list = await api(
      `/api/accounts?sort=name&limit=20&with_stats=false&q=${encodeURIComponent(q)}`,
      { signal: acctPickAbort.signal });
    state.accountOptions = list;
    $('fAccountList').innerHTML = list.map((a) =>
      `<option value="${esc(a.screen_name || a.platform_user_id)}">`).join('');
  } catch (e) {
    if (e.name !== 'AbortError') $('fAccountList').innerHTML = '';
  }
}

export function wireAccountPicker() {
  const input = $('fAccountInput');
  input.addEventListener('input', () => {
    const q = input.value.trim();
    clearTimeout(acctPickTimer);
    // 清空 = 取消篩選，要立刻生效，不必等 debounce
    if (!q) {
      setAccountFilter('', '');
      resetMediaPaging();
      loadMedia();
      return;
    }
    acctPickTimer = setTimeout(() => searchAccountOptions(q), 250);
  });
  // 選定（或按 Enter）才套用篩選。每打一個字就重查媒體是沒必要的。
  input.addEventListener('change', () => {
    const id = selectedAccountId();
    if (input.value.trim() && !id) {
      // 打了字但沒對到任何帳號 —— 講出來，不要默默當成「全部帳號」
      $('fAccountHint').textContent = '找不到這個帳號';
      return;
    }
    $('fAccountHint').textContent = '';
    setAccountFilter(id, input.value.trim());
    resetMediaPaging();
    loadMedia();
  });
}

/** 設定帳號篩選。標籤要寫得出名字，所以 id 與顯示名一起存。 */
export function setAccountFilter(accountId, label) {
  state.accountFilter = accountId ? String(accountId) : '';
  state.accountLabel = accountId ? (label || '') : '';
  $('fAccountInput').value = accountId ? (label || '') : '';
  $('fAccountHint').textContent = '';
}

// ── 跳到媒體頁並套上篩選 ──────────────────────────────
//
// ⚠️ 光是把下拉的值改掉**不算數**。使用者剛按的按鈕在另一個分頁上，
// 跳過去之後畫面整個換掉 —— 他要能一眼看出「現在只看得到這個帳號的東西」，
// 否則會以為媒體庫變小了。所以除了套篩選，標籤列上也要出現可移除的標籤。

export function jumpToMedia({ account, creator, label }) {
  // load: false —— 條件還沒設好，先發一次舊條件的請求純粹是浪費，
  // 而且它會跟下面那次併發（見 showView 的說明）
  showView('media', { load: false });

  // 一次只套一種，另一種要清掉 —— 否則會疊加成「這個 creator 底下的這個帳號」，
  // 而使用者沒有要求那個
  $('fCreator').value = creator || '';
  setAccountFilter(account || '', account ? label : '');

  resetMediaPaging();
  loadMedia();
}
