// 媒體瀏覽：篩選、格線、分頁、詳情面板、選取與批次。

// ⚠️ `fileErrorText` 一度漏在這份 import 之外 —— 格線與詳情面板的
// 「為什麼讀不到」因此每次都丟 ReferenceError，畫面永遠停在「縮圖失敗」
// 四個字。之所以沒被發現，是因為當時影片走佔位 div 不走 <img>，
// onerror 幾乎不會觸發。
import {
  $, autoClose, esc, fileErrorText, fmtBytes, mountDrops, multiDrop, singleDrop,
  starsHtml, wireStars,
} from '../dom.js';
import { api } from '../api.js';
import { PAGE, state, safeMode, onSafeModeChange } from '../state.js';
import { showView, invalidateView } from '../nav.js';
import {
  KINDS, RATING_VALUES, CONTENT_VALUES,
} from '../enums.js';
import { pushDismissable } from '../overlay.js';
import { openViewer } from '../viewer.js';

// 安全模式的兩個控制項都在別處（header 與設定面板）。這裡只訂閱結果。
// 人不在媒體頁時不必立刻重查 —— 但快取要作廢，否則切回來會看到一份
// 用舊安全模式篩出來的畫面，而開關卻顯示新的狀態。
onSafeModeChange(() => {
  // 安全模式一開，r18 那格就要立刻變成不可選並寫出原因 —— 不能等到
  // 使用者點開下拉才發現。`drops` 還沒建好時跳過（wireFilters() 會補做）。
  if (drops.fRating) applySafeModeGate();
  resetMediaPaging();
  if (state.view === 'media') loadMedia();
  else invalidateView('media');
});

// ── 篩選 ───────────────────────────────────────────────

/** 篩選下拉的定義。標籤文字與取值集中在一處 —— 篩選列、標籤列、
 *  「清除這一個」三處都讀它，各寫一份必然會漂移。
 *
 *  ⚠️ **全部都是多選**，所以這裡沒有 `multi` 旗標了。原本有一條
 *  「單選就讀 `$(id).value`」的分支，在原生 `<select>` 退場之後不但沒人用，
 *  留著還有害：那些 id 現在是 `<span>`，`.value` 回 undefined ——
 *  條件會靜默消失，不報錯、標籤列也不顯示。 */
const FILTERS = [
  { id: 'fRating', param: 'rating', label: '分級' },
  { id: 'fContent', param: 'content_type', label: '類型' },
  { id: 'fKind', param: 'kind', label: '型別' },
  { id: 'fStatus', param: 'status', label: '下載狀態' },
  // ⚠️ 評分是**篩特定星數**（`stars=3,5`），不是「幾星以上」。
  // 舊版是 min_stars（`>=`），2026-08-19 使用者裁示改掉：
  // 「不要用幾星以上，直接篩那個星數，讓我多選就可以了」。
  // 後端也一起換了，**沒有留 min_stars** —— 兩套語意並存遲早會用錯。
  { id: 'fStars', param: 'stars', label: '評分' },
];

/** 評分下拉的選項。值是數字字串（後端的 `stars` 吃 1–5），顯示是星星。
 *  ⚠️ 沒有「未評分」這一項 —— NULL 不是 0 分。要篩它得另開參數。 */
export const STAR_VALUES = ['5', '4', '3', '2', '1'];
const STAR_TEXT = (v) => '★'.repeat(Number(v));

/** 所有自製下拉的實例 —— 篩選（多選）、排序鍵與批次列（單選）都放這裡。
 *  `wireFilters()` 建好之後才有東西。 */
const drops = {};

/** 那一組目前選了哪些。 */
function filterValues(f) {
  return drops[f.id] ? drops[f.id].get() : [];
}

/** 只有篩選條件，不含分頁與排序。**清單與總數共用**，兩邊各組一次會對不上。
 *
 *  ⚠️ 多值用 `append` 不是 `set` —— `set` 會把前一個蓋掉，症狀是
 *  「勾了三個型別，結果只篩到最後一個」，而且畫面上的標籤是對的，
 *  所以看起來像後端壞了。 */
export function mediaFilters() {
  const p = new URLSearchParams();
  if (safeMode()) p.set('exclude_rating', 'r18');
  if (state.accountFilter) p.set('account_id', state.accountFilter);
  // creator 沒有自己的下拉 —— 它只從帳號頁點進來（2026-08-19 使用者裁示）。
  // 走與帳號篩選同一套：state 存值、標籤列上出現一個可移除的標籤。
  if (state.creatorFilter) p.set('creator_id', state.creatorFilter);
  for (const f of FILTERS) {
    for (const v of filterValues(f)) p.append(f.param, v);
  }
  return p;
}

/** 生效中的條件，給標籤列用。安全模式**不在裡面** —— 它不是篩選列上的條件，
 *  它的狀態在 header，而它擋掉幾筆由筆數那一行負責講。
 *
 *  ⚠️ 這一列顯示**全部**條件（不是只顯示「畫面上看不見的」）。
 *  篩選用的是下拉，收起來時摘要只寫得下「photo…（3）」——「哪三個」答不出來。
 *  標籤列是唯一逐一列出每個值的地方，所以不能省。 */
function activeConditions() {
  const out = [];
  if (state.accountFilter) {
    out.push({ kind: 'account', label: '帳號', value: state.accountLabel || state.accountFilter });
  }
  if (state.creatorFilter) {
    out.push({ kind: 'creator', label: 'creator', value: state.creatorLabel || state.creatorFilter });
  }
  for (const f of FILTERS) {
    const vals = filterValues(f);
    if (!vals.length) continue;
    // 同一組內是 OR。**「或」要看得見** —— 不寫出來的話，
    // 使用者會以為勾兩個是「同時符合」，然後奇怪為什麼筆數變多了。
    out.push({ kind: 'multi', id: f.id, label: f.label, value: vals.join(' 或 ') });
  }
  return out;
}

/** 某一組被勾滿了嗎。勾滿 ≠ 不勾：`rating IN ('sfw','r18')` 會濾掉 NULL。 */
function fullySelected(id, all) {
  return drops[id] && drops[id].get().length === all.length;
}

function renderChips() {
  const bar = $('chipBar');
  const conds = activeConditions();
  bar.classList.toggle('hidden', conds.length === 0);
  if (!conds.length) { bar.innerHTML = ''; return; }
  bar.innerHTML = '<span class="lead">生效中：</span>'
    + conds.map((c) => `<span class="chip">${esc(c.label)}
        <b>${esc(c.value)}</b>
        <button type="button" data-clear="${esc(c.kind === 'account' || c.kind === 'creator' ? c.kind : c.id)}"
                aria-label="移除這個條件">×</button></span>`).join('')
    + '<span class="spacer"></span>'
    + '<button type="button" class="ghost small" data-clear="__all__">全部清除</button>';
}

function clearCondition(what) {
  const clearOne = (f) => drops[f.id].clear();
  if (what === '__all__') {
    for (const f of FILTERS) clearOne(f);
    setAccountFilter('', '');
    setCreatorFilter('', '');
  } else if (what === 'account') {
    setAccountFilter('', '');
  } else if (what === 'creator') {
    setCreatorFilter('', '');
  } else {
    // 標籤的 × 一次清掉**整個欄位**（不是其中一個值）——
    // 一顆 × 只清一個值的話，勾了四個型別就得按四次。
    const f = FILTERS.find((x) => x.id === what);
    if (f) clearOne(f); else $(what).value = '';
  }
  // 排序不是篩選 —— 「全部清除」不該把使用者選的排序也一起打掉
  resetMediaPaging();
  loadMedia();
}

$('chipBar').addEventListener('click', (ev) => {
  const btn = ev.target.closest('[data-clear]');
  if (btn) clearCondition(btn.dataset.clear);
});

// ── 排序：鍵 + 方向兩個獨立控制項 ──────────────────────
//
// 方向是**看得見的獨立按鈕**，不是「在鍵上再按一次」。後者的指意只寫得進
// hover 提示，鍵盤與觸控使用者永遠看不到。

const SORT_KEYS = ['added', 'posted', 'stars'];
const SORT_ORDERS = ['desc', 'asc'];

/** 排序鍵下拉的顯示字。值域就是 `SORT_KEYS`，不另立一份。 */
const SORT_KEY_TEXT = { added: '加入順序', posted: '推文時間', stars: '評分' };

/** 每個鍵的預設方向。換鍵時套用它，而不是沿用上一個鍵的方向 ——
 *  「評分 · 低→高」不是任何人想要的第一眼。 */
const DEFAULT_ORDER = { added: 'desc', posted: 'desc', stars: 'desc' };

/** 方向鈕的完整語意。按鈕上只有箭頭（使用者裁示：文字太長），
 *  所以兩可的地方靠 aria-label 與提示補：說的是**目前**的順序。 */
const DIR_TEXT = {
  added: { desc: '目前：新→舊。按一下改成舊→新', asc: '目前：舊→新。按一下改成新→舊' },
  posted: { desc: '目前：新→舊。按一下改成舊→新', asc: '目前：舊→新。按一下改成新→舊' },
  stars: { desc: '目前：高→低。按一下改成低→高', asc: '目前：低→高。按一下改成高→低' },
};

/** 選到某個鍵時要講的話。空字串 = 沒有要講的，但那一列仍然佔位。 */
const SORT_NOTE = {
  added: '',
  posted: '時間未知的媒體固定排在最後（升冪、降冪都一樣）',
  stars: '目前全庫幾乎都還沒評分，這個順序的鑑別力很低',
};

/** ⚠️ `drops.fSortKey` 由 `wireFilters()` 建立 —— 這個函式在那之前不能呼叫。
 *  `main.js` 的順序（wireFilters → restoreSort → loadMedia）已經保證了。 */
const sortKey = () => drops.fSortKey.get();
const sortOrder = () => ($('fSortDir').dataset.order === 'asc' ? 'asc' : 'desc');

/** `sort=stars` 的排序鍵是 (stars, id) 複合又含 NULL，後端不支援 keyset。 */
const usesKeyset = () => sortKey() !== 'stars';

function paintSortControls() {
  const key = sortKey();
  const order = sortOrder();
  const btn = $('fSortDir');
  btn.textContent = order === 'desc' ? '↓' : '↑';
  const why = DIR_TEXT[key][order];
  btn.setAttribute('aria-label', `排序方向。${why}`);
  btn.dataset.tip = why;
  $('sortNote').textContent = SORT_NOTE[key];
}

function mediaQuery() {
  const p = mediaFilters();
  p.set('limit', PAGE);
  p.set('sort', sortKey());
  p.set('order', sortOrder());
  if (usesKeyset()) {
    // 游標堆疊的最後一個 = 這一頁的起點。第一頁是 null（不帶游標）。
    const cursor = state.cursors[state.cursors.length - 1];
    if (cursor != null) {
      // `posted` 走兩段式游標（`p:<iso>|<id>` / `n:<id>`），因為 NULL 進不了
      // `(posted_at, id) < (?, ?)` 這種比較 —— 詳見 api/query.py 的 _posted_page()。
      if (sortKey() === 'posted') p.set('cursor', cursor);
      else p.set(sortOrder() === 'asc' ? 'after_id' : 'before_id', cursor);
    }
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
  // ⚠️ **勾滿 ≠ 不勾。** `rating IN ('sfw','r18')` 會濾掉 rating 是 NULL 的那些，
  // 不勾才是全都要。不做「勾滿自動視為不勾」的貼心處理 —— 那會讓未標記的
  // 筆數靜默消失，正是根因原則禁止的兜底。改成把差異講出來。
  const full = [];
  if (fullySelected('fRating', RATING_VALUES)) full.push('分級');
  if (fullySelected('fContent', CONTENT_VALUES)) full.push('類型');
  if (fullySelected('fKind', KINDS)) full.push('型別');
  if (full.length) {
    parts.push(`<span class="hidden-note">${esc(full.join('、'))}已勾滿 ——`
      + ' 但「未標記」的不在任何一格，全部取消勾選才是全都要</span>');
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

/** 會動的東西。**現在有縮圖了**（影片走 ffmpeg 抽格、ugoira 取 zip 第一張），
 *  但仍然不掛 `<video>` —— 縮圖是一張 320px WebP，播放器是跨碟開檔。 */
const PLAYABLE = new Set(['video', 'animated_gif', 'ugoira']);

function cellHtml(m) {
  const missing = m.status !== 'done';
  let body;
  if (missing) {
    body = `<div class="missing">${m.status === 'failed' ? '下載失敗' : '尚未下載'}</div>`;
  } else if (PLAYABLE.has(m.kind)) {
    // ⚠️ **仍然刻意不建立 `<video>` 元素。**
    // 舊版每格掛一個 `preload="metadata"`，一頁 60 格 = 60 次跨磁碟開檔讀
    // moov box，而檔案散在三顆碟上。縮圖走的是同一支 /thumb（320px WebP）。
    // ▶ 角標留著：那是「這一格會動」的唯一指意。
    body = `<img class="thumb" alt="" data-id="${m.id}" data-src="/api/media/${m.id}/thumb">
            <span class="play-badge">▶</span>
            <span class="sz">${fmtBytes(m.bytes)}</span>`;
  } else {
    // src 留空，由 IntersectionObserver 在捲進視窗時才填（見 wireGridImages）。
    // 縮圖是 320px WebP，不是原檔 —— 正式庫單檔最大 446 MB。
    body = `<img class="thumb" alt="" data-id="${m.id}" data-src="/api/media/${m.id}/thumb">`;
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
      img.onerror = async () => {
        // 縮圖失敗的原因要看得出來，不可以留一個破圖圖示了事。
        // 對照表在 dom.js 的 `fileErrorText()`，三個呼叫端共用同一份文案。
        // 「被刪了還是碟沒插」是系統模型那五題裡目前答不出來的第 4 題。
        const box = Object.assign(document.createElement('div'), {
          className: 'missing', textContent: '縮圖失敗',
        });
        // 影片格的 ▶ 角標是絕對定位在正中央的 —— 留著會蓋住錯誤說明，
        // 而錯誤說明才是這一格現在唯一要傳達的事。
        const cell = img.closest('.cell');
        cell?.querySelector('.play-badge')?.remove();
        img.replaceWith(box);
        const why = await fileErrorText(Number(img.dataset.id), { thumb: true });
        if (why) box.textContent = why;
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
    // `posted` 的游標是字串（`p:…` / `n:…`），`added` 的是 id。
    // ⚠️ 順序不可以顛倒：`added` 的回應裡 next_cursor 也有值（就是 id 的字串
    // 形式），先讀它會讓 before_id 變成字串 —— 目前後端吃得下，但那是巧合。
    state.nextCursor = sortKey() === 'posted'
      ? (data.next_cursor ?? null)
      : (data.next_before_id ?? data.next_after_id ?? null);
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
  const r = drops.bulkRating.get();
  const c = drops.bulkContent.get();
  if (r) body.rating = r === '__clear__' ? null : r;
  if (c) body.content_type = c === '__clear__' ? null : c;
  return body;
}

/** 批次評分的 body，或 null（沒選）。
 *
 *  與分級的關鍵差別：**stars 掛在 media，不掛 post**，所以送的是 media_ids
 *  而不是去重過的 post_ids。選了同則貼文的三張圖就是改三張，不會波及第四張。 */
function buildBulkStarsBody() {
  const s = drops.bulkStars.get();
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
    drops.bulkRating.clear();
    drops.bulkContent.clear();
    drops.bulkStars.clear();
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

// ── 篩選與排序的建立與綁定 ─────────────────────────────

/** 每次勾選都重查。**不是面板關閉才查** —— `336ad1c` 回朔的就是那一版。
 *
 *  慢的只有總數（正式庫一次 COUNT 約 1.3 秒），而它本來就是非阻塞、
 *  可取消的（`refreshMediaCount()` 的 AbortController）。結果本身只要 1 ms，
 *  所以每勾一次就重查完全撐得住 —— 而且那是使用者唯一的即時回饋。 */
function onFilterChanged() {
  resetMediaPaging();
  loadMedia();
}

function buildDrop(id, label, values, text) {
  drops[id] = multiDrop($(id), {
    label,
    values: values.map((v) => ({ value: v, text: text ? text(v) : undefined })),
    onChange: onFilterChanged,
  });
}

/** 安全模式開著時 r18 不可選。**不是把它藏掉** —— 藏掉就答不出
 *  「為什麼我看不到 r18」。disabled + 面板裡寫出原因。 */
function applySafeModeGate() {
  drops.fRating.setDisabled(
    'r18', safeMode() ? '安全模式開著，r18 不可選（開關在右上角）' : null,
  );
}

/** 排序鍵換了要做的事。原本掛在 `<select>` 的 change 上；改成自製下拉之後
 *  必須改走 `onChange`。
 *
 *  ⚠️ 這裡是這一輪最容易靜默失敗的地方：`$('fSortKey')` 現在回傳的是一個
 *  `<span>`，**不是 null** —— 舊的 `addEventListener('change', …)` 掛得上去、
 *  不會報錯，只是永遠不會觸發。症狀是「換排序沒反應」，console 全白。 */
function onSortKeyChanged() {
  // 換鍵時套用**該鍵的預設方向**，不沿用上一個鍵的。
  $('fSortDir').dataset.order = DEFAULT_ORDER[sortKey()] || 'desc';
  saveSort();
  paintSortControls();
  // ⚠️ 游標堆疊一定要清空：換了排序鍵之後，舊游標指的是另一種順序裡的位置，
  // 拿去翻頁會拿到一頁莫名其妙的東西（而且看起來像資料壞了）。
  resetMediaPaging();
  // 排序不改變筆數 —— **不重算總數**，省掉一次 1.3 秒的 COUNT。
  loadMedia({ withCount: false });
}

/** 批次列的三個。**單選是對的** —— 批次是「把選中的媒體改成這個值」，
 *  不是「篩出這些值」。這裡不要順手改成多選。
 *
 *  值域取自 `enums.js`，不再在 index.html 裡抄第二份（那份 test_enums_sync
 *  掃不到，加了新的 content type 只會發現「這裡選得到、那裡選不到」）。 */
function wireBulkDrops() {
  const mk = (id, label, ariaLabel, values) => {
    drops[id] = singleDrop($(id), {
      label, ariaLabel, values, onChange: () => {},
      // 「不變」= 這一批不動這個欄位。原生 select 的 `<option value="">`
      // 就是幹這個的；沒有它，選錯了就再也回不到「不套用」。
      emptyText: '不變',
    });
  };
  const clear = (text) => ({ value: '__clear__', text });
  mk('bulkRating', '分級…', '批次分級',
     [...RATING_VALUES.map((v) => ({ value: v })), clear('（清除）')]);
  mk('bulkContent', '類型…', '批次類型',
     CONTENT_VALUES.map((v) => ({ value: v })));
  mk('bulkStars', '評分…', '批次評分',
     [...STAR_VALUES.map((v) => ({ value: v, text: STAR_TEXT(v) })),
      clear('（清除評分）')]);
}

export function wireFilters() {
  // 排序鍵。值域是 SORT_KEYS，顯示字由 SORT_KEY_TEXT 給。
  drops.fSortKey = singleDrop($('fSortKey'), {
    label: '排序',
    values: SORT_KEYS.map((v) => ({ value: v, text: SORT_KEY_TEXT[v] })),
    value: 'added',
    onChange: onSortKeyChanged,
  });
  wireBulkDrops();
  buildDrop('fRating', '分級', RATING_VALUES);
  buildDrop('fContent', '類型', CONTENT_VALUES);
  buildDrop('fKind', '型別', KINDS);
  // 「更多篩選」裡的三個 —— 與主篩選列同一種形態。
  buildDrop('fStatus', '下載狀態', ['done', 'pending', 'failed']);
  buildDrop('fStars', '評分', STAR_VALUES, STAR_TEXT);
  // 「更多篩選」是靜態寫在 HTML 裡的 <details> —— 原生 <details> 沒有
  // 「點外面收起」，要自己接上（症狀是拉開之後按別的地方它一直開著）。
  // 兩個分頁各有一個，一起接。
  autoClose($('fMore'));
  autoClose($('aMore'));
  applySafeModeGate();
}

// 全部的篩選器都是多選下拉了（2026-08-19 使用者裁示）。
// 先前這裡留了一段「更多篩選那三個維持單選」的理由 ——「真實資料上鑑別力
// 為零，升級是為零筆結果付複雜度」。那個判斷是拿 dev 的空資料做的；
// 正式庫有 4,653 個帳號，creator 與評分都是真的有東西可篩。

$('fSortDir').addEventListener('click', () => {
  $('fSortDir').dataset.order = sortOrder() === 'desc' ? 'asc' : 'desc';
  saveSort();
  paintSortControls();
  resetMediaPaging();
  loadMedia({ withCount: false });
});

function saveSort() {
  localStorage.setItem('mediaSort', `${sortKey()}:${sortOrder()}`);
}

/** 還原偏好。**白名單驗證** —— 認不得就用預設。
 *
 *  這裡有前科：分段控制那版存的是 `added:desc`，回朔後的舊 `<select>` 吃到
 *  會變成空值，然後送出 `sort=`（一個靜默的空條件，不會報錯也不會生效）。
 *  所以絕不直接把 localStorage 的字串塞進控制項。 */
export function restoreSort() {
  const raw = localStorage.getItem('mediaSort') || '';
  // 舊值相容：`newest` / `oldest` / `stars` 是分段控制之前那一版存的
  const legacy = { newest: 'added:desc', oldest: 'added:asc', stars: 'stars:desc' };
  const [key, order] = (legacy[raw] || raw).split(':');
  const okKey = SORT_KEYS.includes(key) ? key : 'added';
  const okOrder = SORT_ORDERS.includes(order) ? order : DEFAULT_ORDER[okKey];
  drops.fSortKey.set(okKey);
  $('fSortDir').dataset.order = okOrder;
  paintSortControls();
}

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

/** 下載狀態的說明。資料本來就在手上（輪詢中的佇列狀態），不必額外請求。 */
export function paintMoreNotes() {
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
 *  真正的修法是匯入器改寫一個 `import` 值（那是資料層改動，見
 *  那是資料層改動，尚未做），這裡先用不會騙人的文案。 */
function sourceText(src) {
  if (!src) return '尚未標記';
  if (src === 'manual') return 'manual（人工或匯入時分類 —— 目前分不出來）';
  if (src === 'auto') return 'auto（機器猜測，尚未人工確認）';
  if (src === 'account_default') return 'account_default（套用帳號預設值）';
  return src;
}

/** 自動播放前的大小上限。正式庫單檔最大 446 MB —— 一開詳情就自動開始傳
 *  那種檔案，等於每點一次就吃掉幾百 MB 的磁碟與頻寬。 */
const AUTOPLAY_MAX_BYTES = 50 * 1024 * 1024;

/** 影片與動圖的預覽。
 *
 *  `animated_gif`（X 存成無聲 mp4）與 `ugoira`（pixiv 動圖）**語意上就是 GIF**：
 *  自己循環播放、沒有控制列。給它們控制列反而不對。
 *
 *  真影片保留控制列，但一樣循環播放。
 *
 *  ⚠️ `muted` 不是可選的：沒有它 Chrome 會直接拒絕自動播放，症狀是
 *  「有時候會播、有時候不會」—— 那比不會播更難查。 */
function videoHtml(m) {
  const gifLike = m.kind !== 'video';
  const tooBig = (m.bytes || 0) > AUTOPLAY_MAX_BYTES;
  const attrs = [
    `src="/api/media/${m.id}/file"`,
    'id="dPreview"',
    'loop', 'muted', 'playsinline',
    // 動圖不給控制列 —— 但**大到不自動播的時候一定要給**，
    // 否則畫面上是一個沒有任何辦法播放的靜止畫格。
    (!gifLike || tooBig) ? 'controls' : '',
    tooBig ? 'preload="metadata"' : 'autoplay',
  ].filter(Boolean).join(' ');
  const note = tooBig
    ? `<p class="note muted">檔案 ${fmtBytes(m.bytes)}，按播放才會開始載入。</p>`
    : '';
  return `<video ${attrs}></video>${note}`;
}

/** 詳情面板在關閉堆疊上的登記。開著的時候不是 null。
 *
 *  ⚠️ 換張（siblings）會重新呼叫 `showDetail()`，**不可以重複登記** ——
 *  疊了兩層的話 Esc 要按兩次才關得掉，正是這次要修的症狀。 */
let detailDismiss = null;

export async function showDetail(mediaId) {
  $('detail').classList.remove('hidden');
  detailDismiss = detailDismiss || pushDismissable({ close: closeDetail });
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
        // tabindex + role：`<img>` 預設不可聚焦，於是「點圖放大」就只有滑鼠
        // 能用，而且關閉檢視器後焦點無處可回。兩件事同一個修法。
        ? `<img src="/api/media/${m.id}/file" alt="" id="dPreview" class="zoomable"
                tabindex="0" role="button" aria-label="放大檢視">`
        : videoHtml(m))
    : `<p class="muted">狀態：${esc(m.status)}${m.error ? `　${esc(m.error)}` : ''}</p>`;

  // 網址由後端的 links.py 產生。**前端不拼平台網址** —— 這裡原本寫死
  // `https://x.com/...`，於是 misskey / pixiv 的貼文會連到 x.com 上不存在的
  // 位址：不是報錯，是連到錯的地方，比 404 更難發現。
  const link = p.post_url
    ? `<a href="${esc(p.post_url)}" target="_blank" rel="noreferrer">在 ${
        esc(p.platform_label || p.platform)} 開啟</a>`
    : `<span class="muted">${esc(p.link_problem || '無法連結')}</span>`;

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
        <span id="dRating" class="ms-host"></span>
        <span id="dContent" class="ms-host"></span>
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
    // 問不出原因就維持第一句 —— 不編一個聽起來很具體的理由。
    const why = await fileErrorText(m.id);
    if (why) box.textContent = why;
  }, { once: true });

  // 點預覽圖 → 放大檢視。**只掛在詳情面板這一層**：格線的點擊已經是
  // 「開詳情」，再加一個手勢會兩者打架。
  //
  // 影片不掛：它的整個表面都是原生控制列，點下去應該是播放／暫停。
  if (m.kind === 'photo' && m.status === 'done') {
    const zoom = () => openViewer({
      media: m,
      siblings: sibs,
      // 檢視器裡換張時，背後的詳情面板跟著換 —— 關掉之後看到的
      // 才是同一張，否則會覺得自己關錯了東西。
      onSwitch: (id) => { if (id !== m.id) showDetail(id); },
    });
    $('dPreview')?.addEventListener('click', zoom);
    $('dPreview')?.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); zoom(); }
    });
  }

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
  // ⚠️ 第一個參數是**下拉握把**不是 DOM 元素：詳情面板每次開都重畫，
  // 那兩個欄位現在是自製下拉，讀寫要走 get() / set()。
  const autoSave = async (drop, field, previous) => {
    const value = drop.get();
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
      drop.set(previous);    // 還原，不要讓畫面顯示一個沒存進去的值
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

  // ⚠️ 不可以再對 $('dRating') 掛 change —— 它現在是 <span>，
  // addEventListener 掛得上、不報錯、永遠不觸發（「改了沒存到」）。
  let lastRating = p.rating || '';
  let lastContent = p.content_type || '';
  const d = mountDrops($('detailBody'), {
    dRating: {
      label: '分級（未標）', emptyText: '（未標）', ariaLabel: '整則貼文的分級',
      values: RATING_VALUES.map((v) => ({ value: v })),
      value: lastRating,
      onChange: async () => {
        const prev = lastRating;
        lastRating = d.dRating.get();
        await autoSave(d.dRating, 'rating', prev);
      },
    },
    dContent: {
      label: '類型（未標）', emptyText: '（未標）', ariaLabel: '整則貼文的類型',
      values: CONTENT_VALUES.map((v) => ({ value: v })),
      value: lastContent,
      onChange: async () => {
        const prev = lastContent;
        lastContent = d.dContent.get();
        await autoSave(d.dContent, 'content_type', prev);
      },
    },
  });
}

function closeDetail() {
  detailDismiss?.release();
  detailDismiss = null;
  $('detail').classList.add('hidden');
  // 影片沒有停下來的話，關掉面板之後聲音／頻寬還在跑（面板只是 hidden，
  // 元素還在 DOM 裡）。
  const v = $('detailBody').querySelector('video');
  if (v) v.pause();
}
$('closeDetail').addEventListener('click', closeDetail);

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

/** 設定 creator 篩選。與帳號篩選同一套：畫面上沒有下拉，只有標籤列上
 *  那個可移除的標籤 —— 入口是帳號頁的「看這位的媒體」。 */
export function setCreatorFilter(creatorId, label) {
  state.creatorFilter = creatorId ? String(creatorId) : '';
  state.creatorLabel = creatorId ? (label || '') : '';
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
  setCreatorFilter(creator || '', creator ? label : '');
  setAccountFilter(account || '', account ? label : '');

  resetMediaPaging();
  loadMedia();
}
