'use strict';

const $ = (id) => document.getElementById(id);
const PAGE = 60;

const state = {
  view: 'media',
  offset: 0,            // 只有 sort=stars 走 offset，其餘走 keyset
  cursors: [null],      // keyset 游標堆疊，長度 = 目前第幾頁
  hasMore: false,
  nextCursor: null,
  total: null,          // null = 還沒算出來。**不是 0**
  accounts: [],
  accountOptions: [],   // 帳號篩選 datalist 目前的候選
  accountFilter: '',    // 生效中的 account_id（'' = 全部）
  creators: [],
  items: [],              // 目前這頁的媒體，選取與 shift 範圍要用
  selecting: false,
  picked: new Set(),      // media id
  lastPickIndex: null,    // shift 範圍選取的錨點
  acctOffset: 0,          // 帳號頁的分頁位移
  acctTotal: 0,
  fetchActive: false,     // 抓取佇列還有東西 → 輪詢要維持快節奏
  loadingMedia: false,    // 格線載入中 → 翻頁要鎖住（游標還沒算出來）
};

// 安全模式預設開啟 —— 預設安全比預設方便重要。
// 只有使用者明確關掉才會記住，重開瀏覽器仍以安全為準若沒設定過。
const safeStored = localStorage.getItem('safeMode');
let safeMode = safeStored === null ? true : safeStored === 'true';

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`${res.status} ${text.slice(0, 200)}`);
  }
  return res.status === 204 ? null : res.json();
}

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// ── 五星評分元件 ───────────────────────────────────────
// ⚠️ 這是「評分」，與 rating（sfw / r18 分級）是**兩件事**。
// 後端欄位叫 stars，前端也一律用 stars，不要混用 rating 這個字。

/** 五顆星的 HTML。`value` 為 null 代表未評分（不是 0 分）。 */
function starsHtml(value, cls = '') {
  const stars = [1, 2, 3, 4, 5].map((n) =>
    `<button type="button" class="star${value && n <= value ? ' on' : ''}" data-n="${n}"
             aria-label="${n} 星">★</button>`).join('');
  return `<span class="stars ${cls}" data-stars="${value ?? ''}"
                title="點星星評分；再點同一顆可清除">${stars}</span>`;
}

function paintStars(root, value) {
  root.dataset.stars = value ?? '';
  root.querySelectorAll('.star').forEach((b) => {
    b.classList.toggle('on', value !== null && Number(b.dataset.n) <= value);
  });
}

/** 綁定五星元件。`onSet(value|null)` 要回傳 Promise；失敗會還原畫面。 */
function wireStars(root, onSet, onError) {
  root.querySelectorAll('.star').forEach((btn) => {
    btn.addEventListener('click', async (ev) => {
      // 帳號卡與媒體格子本身都有 click handler，不擋的話會順便開詳情／切換選取
      ev.stopPropagation();
      ev.preventDefault();
      const before = root.dataset.stars ? Number(root.dataset.stars) : null;
      const n = Number(btn.dataset.n);
      // 再點同一顆 = 清除。這是唯一的清除方式，所以元件的 title 要寫出來。
      const next = before === n ? null : n;
      paintStars(root, next);
      try {
        await onSet(next);
      } catch (e) {
        paintStars(root, before);   // 還原，不要顯示一個沒存進去的值
        if (onError) onError(e);
      }
    });
  });
}

// TB 是必要的，不是防禦性的：正式庫總計 1.27 TB。
// 少了 TB 這一級，它會顯示成「1305.7 GB」—— 讀得懂但沒人看得快。
const fmtBytes = (n) => {
  if (!n) return '—';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
};

// ── 安全模式 ───────────────────────────────────────────
function applySafeMode() {
  document.body.classList.toggle('safe', safeMode);
  $('safeMode').checked = safeMode;
}

$('safeMode').addEventListener('change', (e) => {
  safeMode = e.target.checked;
  localStorage.setItem('safeMode', String(safeMode));
  applySafeMode();
  resetMediaPaging();
  loadMedia();
});

// ── 分頁切換 ───────────────────────────────────────────

/** 切到某個 view。`load` 為 false 時只換畫面、不發請求。
 *
 *  ⚠️ `load: false` 是給「切過去之後馬上要用不同條件重載」的呼叫端用的
 *  （例如從帳號頁跳過來看某個帳號）。少了它，切分頁會先用**舊條件**發一次
 *  請求，然後才是新條件那一次 —— 兩個併發，先發的後到就會蓋掉正確結果。 */
function showView(name, { load = true } = {}) {
  document.querySelectorAll('.tab').forEach((b) =>
    b.classList.toggle('active', b.dataset.view === name));
  state.view = name;
  document.querySelectorAll('.view').forEach((v) => v.classList.add('hidden'));
  $(`view-${name}`).classList.remove('hidden');
  if (!load) return;
  if (name === 'media') loadMedia();
  if (name === 'fetch') refreshFetchQueue();
  if (name === 'accounts') loadAccounts();
  if (name === 'creators') loadCreators();
  if (name === 'problems') loadProblems();
}

document.querySelectorAll('.tab').forEach((btn) => {
  btn.addEventListener('click', () => showView(btn.dataset.view));
});

// ── 媒體格線 ───────────────────────────────────────────

/** 只有篩選條件，不含分頁與排序。**清單與總數共用**，兩邊各組一次會對不上。 */
function mediaFilters() {
  const p = new URLSearchParams();
  if (safeMode) p.set('exclude_rating', 'r18');
  const map = {
    account_id: state.accountFilter,
    creator_id: $('fCreator').value,
    rating: $('fRating').value,
    content_type: $('fContent').value,
    kind: $('fKind').value,
    status: $('fStatus').value,
    min_stars: $('fMinStars').value,
  };
  for (const [k, v] of Object.entries(map)) if (v) p.set(k, v);
  return p;
}

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

async function refreshMediaCount() {
  // 前一個還在跑就取消：慢的那個後到會蓋掉正確結果（換了篩選尤其明顯）
  if (countAbort) countAbort.abort();
  countAbort = new AbortController();
  const signal = countAbort.signal;

  $('mediaCount').textContent = '計算總數…';
  $('mediaCount').className = 'muted';
  try {
    const data = await api(`/api/media/count?${mediaFilters()}`, { signal });
    if (signal.aborted) return;
    state.total = data.total;
    $('mediaCount').textContent = `共 ${data.total.toLocaleString()} 個媒體`;
  } catch (e) {
    if (e.name === 'AbortError') return;
    state.total = null;
    $('mediaCount').textContent = `總數算不出來：${e.message}`;
    $('mediaCount').className = 'muted err';
  }
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

  const rating = m.rating
    ? `<span class="tag ${m.rating === 'r18' ? 'r18' : ''}">${esc(m.rating)}</span>`
    : '';
  const kind = m.kind === 'photo' ? '' : `<span class="kind">${esc(m.kind)}</span>`;
  // 只在有評分時顯示。空的星星角標會讓每一格都變吵。
  const stars = m.stars ? `<span class="star-badge">${'★'.repeat(m.stars)}</span>` : '';

  return `<div class="cell st-${esc(m.status)}" data-id="${m.id}" data-post="${m.post_id}">
    ${body}${rating}${kind}${stars}<span class="pick">✓</span>
  </div>`;
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
      img.src = img.dataset.src;
      img.onerror = () => {
        // 縮圖失敗的原因要看得出來，不可以留一個破圖圖示了事：
        // 404 = 檔案真的不見了；415 = 這格式生不出縮圖；500 = 原檔壞了。
        img.replaceWith(Object.assign(document.createElement('div'), {
          className: 'missing', textContent: '縮圖失敗',
        }));
      };
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
    if (safeMode && rating === 'r18') {
      cell.remove();
      if (state.total != null) {
        state.total -= 1;
        $('mediaCount').textContent = `共 ${state.total.toLocaleString()} 個媒體`;
      }
      continue;
    }
    cell.querySelector('.tag')?.remove();
    if (rating) {
      cell.insertAdjacentHTML('beforeend',
        `<span class="tag ${rating === 'r18' ? 'r18' : ''}">${esc(rating)}</span>`);
    }
  }
  if (!$('grid').querySelector('.cell')) {
    // 整頁都被濾掉了。空白畫面看起來像壞掉，要講出發生了什麼。
    $('grid').innerHTML =
      '<p class="muted">本頁的媒體都被標記後隱藏了。按「重新整理」載入下一批。</p>';
  }
}

/** 重設分頁狀態。改篩選／改排序／切安全模式時都要呼叫。 */
function resetMediaPaging() {
  state.cursors = [null];
  state.offset = 0;
}

/** 空狀態。**要說出為什麼空，不能只說「沒有」。**
 *
 *  實測抓到的具體情境：帳號頁按下「684 個媒體」跳過來，畫面顯示「共 0 個媒體」。
 *  那個 0 是對的 —— 那個帳號的 684 筆全是 r18，而工作安全模式開著。但使用者
 *  剛剛才看到 684 這個數字，畫面卻空了，他只會覺得功能壞了。
 *
 *  這是評估鴻溝的缺口（Norman 的行動七階段）：顯示「0」只回答了「現在畫面上
 *  是什麼」，回答不了「我剛做的事成功了嗎」與「這對我的目標是好是壞」。 */
function emptyGridHtml() {
  const hasFilter = [...mediaFilters().keys()].some((k) => k !== 'exclude_rating');
  if (safeMode) {
    return '<p class="muted">沒有符合條件的媒體。<br>'
      + '<b>工作安全模式開著</b> —— 符合條件的 r18 內容不會顯示在這裡。'
      + '關掉右上角的開關就看得到。</p>';
  }
  return hasFilter
    ? '<p class="muted">沒有符合條件的媒體。試著放寬篩選條件。</p>'
    : '<p class="muted">還沒有任何媒體。到「抓取」分頁貼幾個帳號網址開始。</p>';
}

// 請求序號。**慢的那個後到會蓋掉正確結果** —— 這不是理論問題：
// 從帳號頁按「N 個媒體」時，切分頁本身會先發一次未篩選的請求（60 筆、要讀
// 60 張縮圖），接著才是篩選後的請求（可能 0 筆、瞬間回來）。先發的後到，
// 畫面就會停在**上一個帳號的內容**，而篩選標籤卻寫著新帳號。
//
// 總數那邊用 AbortController 解掉了同樣的問題；清單這邊用序號 ——
// 因為要保留「已經回來的資料」判斷，不是單純取消。
let mediaReqSeq = 0;

/** 載入格線。`withCount` 為 false 時不重算總數（翻頁時總數沒變）。 */
async function loadMedia({ withCount = true } = {}) {
  if (!state.cursors) resetMediaPaging();
  if (withCount) refreshMediaCount();   // 刻意不 await —— 畫面不等它

  const seq = ++mediaReqSeq;
  // 載入中就把翻頁鎖住（見 pageTurn）。用 disabled 而不是靜默忽略 ——
  // 「按了沒反應」正是要避免的那種失敗。
  state.loadingMedia = true;
  $('prevPage').disabled = true;
  $('nextPage').disabled = true;
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
    // 而總數是另一個請求、可能還沒到。硬要湊出 "x–y / z" 就得等總數，
    // 那正是這次改動要消掉的等待。
    const page = usesKeyset() ? state.cursors.length : Math.floor(state.offset / PAGE) + 1;
    $('pageInfo').textContent = data.items.length
      ? `第 ${page} 頁　本頁 ${data.items.length} 個`
      : '沒有資料';
    $('prevPage').disabled = usesKeyset() ? state.cursors.length <= 1 : state.offset === 0;
    $('nextPage').disabled = !data.has_more;

    document.querySelectorAll('.cell').forEach((c, index) => {
      const id = Number(c.dataset.id);
      if (state.picked.has(id)) c.classList.add('picked');
      c.addEventListener('click', (ev) => {
        if (state.selecting) togglePick(id, index, ev.shiftKey);
        else showDetail(id);
      });
    });
    wireGridImages();
    updateSelInfo();
  } catch (e) {
    if (seq === mediaReqSeq) {
      $('grid').innerHTML = `<p class="muted">載入失敗：${esc(e.message)}</p>`;
      // 失敗也要把翻頁放開 —— 否則使用者被卡在一個壞掉的畫面上，
      // 連退回上一頁都做不到
      $('prevPage').disabled = usesKeyset() ? state.cursors.length <= 1 : !state.offset;
      $('nextPage').disabled = false;
    }
  } finally {
    // 只有最後一個請求負責解鎖 —— 舊的那個解鎖會讓還在飛的那次變成可按
    if (seq === mediaReqSeq) state.loadingMedia = false;
  }
}

// ── 選取 ───────────────────────────────────────────────
function togglePick(id, index, shift) {
  if (shift && state.lastPickIndex !== null) {
    const [a, b] = [state.lastPickIndex, index].sort((x, y) => x - y);
    for (let i = a; i <= b; i++) state.picked.add(state.items[i].id);
  } else {
    if (state.picked.has(id)) state.picked.delete(id);
    else state.picked.add(id);
    state.lastPickIndex = index;
  }
  document.querySelectorAll('.cell').forEach((c) => {
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
  document.querySelectorAll('.cell').forEach((c) => c.classList.add('picked'));
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
  // 行內確認，不用 confirm()：那會擋住整個分頁。
  // 兩種作用範圍不同，必須分開講 —— 使用者只選了 3 張圖卻改到 1 則貼文的
  // 分級，那是他該事先知道的事。
  const parts = [];
  if (hasTags) parts.push(`分級／類型 → ${body.post_ids.length} 則貼文`);
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

// fAccount 不在裡面 —— 它現在是可搜尋的 input，有自己的 handler（wireAccountPicker）
const MEDIA_FILTERS = ['fCreator', 'fRating', 'fContent', 'fKind', 'fStatus', 'fMinStars'];

MEDIA_FILTERS.forEach((id) =>
  $(id).addEventListener('change', () => { resetMediaPaging(); loadMedia(); }));

$('fSort').addEventListener('change', () => {
  localStorage.setItem('mediaSort', $('fSort').value);
  resetMediaPaging();
  loadMedia();
});

$('fReset').addEventListener('click', () => {
  // 排序不是篩選 —— 「清除篩選」不該把使用者選的排序也一起打掉
  MEDIA_FILTERS.forEach((id) => { $(id).value = ''; });
  setAccountFilter('', '');
  resetMediaPaging();
  loadMedia();
});

// 翻頁不重算總數 —— 條件沒變，總數就沒變，而它要 1.3 秒。
//
// ⚠️ **載入中不接受翻頁。** keyset 的下一頁游標是「這一頁最後一筆的 id」，
// 而那個值要等回應回來才知道。上一頁還在飛的時候按下一頁，推進去的是
// **上上頁**的游標 —— 實測結果是游標堆疊變成
// `[null, 1273350, 1273290, 1273290, 1273290]`：頁碼顯示第 5 頁，畫面上是第 3 頁。
//
// 不用「排隊等一下再送」而是直接擋掉：使用者按了沒反應才是要避免的，
// 所以按鈕會 disabled（看得出來不能按），而不是靜默忽略。
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
const RATINGS = ['', 'sfw', 'r18'];
// 要與 db/enums.py 的 ContentType 一致。加值時記得也要有 alembic migration ——
// CHECK constraint 不會自己跟著 enum 改。
const CONTENTS = ['', 'illust', 'irl', 'mod', 'ai', '3d', 'photograph', 'other'];
const opts = (list, cur) =>
  list.map((v) => `<option value="${v}" ${v === (cur || '') ? 'selected' : ''}>${v || '（未標）'}</option>`).join('');

async function showDetail(mediaId) {
  $('detail').classList.remove('hidden');
  $('detailBody').innerHTML = '<p class="muted">載入中…</p>';

  // 直接抓這一筆。先前是抓 500 筆清單再從裡面找，媒體一多就必然找不到。
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
  const preview = m.status === 'done'
    ? (m.kind === 'photo'
        ? `<img src="/api/media/${m.id}/file" alt="">`
        : `<video src="/api/media/${m.id}/file" controls preload="metadata"></video>`)
    : `<p class="muted">狀態：${esc(m.status)}${m.error ? `　${esc(m.error)}` : ''}</p>`;

  const link = acct?.screen_name
    ? `<a href="https://x.com/${esc(acct.screen_name)}/status/${esc(p.platform_post_id)}" target="_blank" rel="noreferrer">在 x.com 開啟</a>`
    : '—';

  $('detailBody').innerHTML = `
    ${preview}
    <div class="row" style="margin-top:12px">
      <select id="dRating">${opts(RATINGS, p.rating)}</select>
      <select id="dContent">${opts(CONTENTS, p.content_type)}</select>
      <span id="dSaved" class="saved"></span>
    </div>
    <div class="row">
      ${starsHtml(m.stars, 'dStars')}
      <span class="muted small">評分只影響這一個媒體，不影響同則貼文的其他張</span>
      <span id="dStarSaved" class="saved"></span>
    </div>
    <div class="src-note" id="dSource">目前來源：${esc(p.rating_source || '未標記')}${
      p.rating_source === 'auto' ? '（機器猜測，尚未人工確認）' : ''}</div>
    <dl>
      <dt>貼文</dt><dd>${esc(p.platform_post_id)}</dd>
      <dt>帳號</dt><dd>${esc(acct?.screen_name || p.account_id)}</dd>
      <dt>時間</dt><dd>${esc(p.posted_at || '—')}</dd>
      <dt>型別</dt><dd>${esc(m.kind)}</dd>
      <dt>大小</dt><dd>${fmtBytes(m.bytes)}</dd>
      <dt>狀態</dt><dd>${esc(m.status)}</dd>
      <dt>本機路徑</dt><dd>${esc(m.local_path || '—')}</dd>
      <dt>SHA-256</dt><dd>${esc((m.file_hash || '—').slice(0, 24))}…</dd>
      <dt>來源</dt><dd>${esc(m.source_url)}</dd>
      <dt>原貼文</dt><dd>${link}</dd>
    </dl>`;

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
      $('dSource').textContent = `目前來源：${r.rating_source || '未標記'}`;
      flash.textContent = '已儲存';
      flash.className = 'saved ok';
      setTimeout(() => { flash.textContent = ''; }, 1600);
      // 只更新受影響的格子，**不重載整頁**。
      // 舊版在這裡呼叫 loadMedia()，等於每改一個下拉就重跑一次查詢 +
      // 一次 COUNT（正式庫 1.3 秒）—— 而畫面上只有一格需要變。
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
      // 只更新這一格的星星角標。
      // ⚠️ 依評分排序時，改了評分之後**順序其實已經不對了**。不自動重排是
      // 刻意的：格子在腳下跳走比順序暫時不準更糟（見帳號頁 ♥ 的同款決定）。
      // 使用者按「重新整理」就會重排。
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

$('closeDetail').addEventListener('click', () => $('detail').classList.add('hidden'));

// ── 帳號 ───────────────────────────────────────────────

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

function wireAccountPicker() {
  const input = $('fAccountInput');
  input.addEventListener('input', () => {
    const q = input.value.trim();
    clearTimeout(acctPickTimer);
    // 清空 = 取消篩選，要立刻生效，不必等 debounce
    if (!q) {
      state.accountFilter = '';
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
    state.accountFilter = id;
    resetMediaPaging();
    loadMedia();
  });
}

/** 從別處（帳號頁的「看媒體」）跳過來時，把篩選設好並顯示出來。 */
function setAccountFilter(accountId, label) {
  state.accountFilter = accountId ? String(accountId) : '';
  $('fAccountInput').value = label || '';
  $('fAccountHint').textContent = '';
}

// 帳號頁一定要分頁。匯入舊資料後這個庫有 4,653 個帳號 ——
// 一次渲染完整份會把瀏覽器凍住（實測 CDP 直接逾時）。
const ACCT_PAGE = 100;

function accountQuery() {
  const p = new URLSearchParams();
  p.set('sort', $('aSort').value);
  p.set('limit', ACCT_PAGE);
  p.set('offset', state.acctOffset);
  const q = $('aSearch').value.trim();
  if (q) p.set('q', q);
  if ($('aFavOnly').checked) p.set('favorite', 'true');
  if ($('aMinStars').value) p.set('min_stars', $('aMinStars').value);
  // `__unset__` 直接原樣送 —— 空字串在 query string 裡與「不篩選」分不出來
  if ($('aDefaultRating').value) p.set('default_rating', $('aDefaultRating').value);
  if ($('aDefaultContent').value) {
    p.set('default_content_type', $('aDefaultContent').value);
  }
  const fs = $('aFetchStatus').value;
  // `__bad__` 展開成後端認得的多值。**不可以在前端濾** —— 前端只看得到
  // 當頁的 100 筆，使用者會在一頁全是「從沒檢查過」的清單上看到 0 筆，
  // 然後以為沒有任何帳號有問題。實測就是這樣錯的。
  if (fs === '__bad__') p.set('fetch_status', FETCH_BAD.join(','));
  else if (fs) p.set('fetch_status', fs);
  return p.toString();
}

// 非 ok/no_new 就是需要注意的狀態
const FETCH_BAD = ['not_found', 'rate_limited', 'auth_required', 'failed'];

const FETCH_LABEL = {
  ok: '有新的', no_new: '沒有新的', not_found: '找不到（可能改名）',
  rate_limited: '被限速', auth_required: '需要憑證', failed: '失敗', skipped: '已跳過',
};

function fetchBadge(a) {
  if (!a.last_fetch_status) return '<span class="muted">從沒檢查過</span>';
  const bad = FETCH_BAD.includes(a.last_fetch_status);
  const n = a.last_fetch_new_posts;
  const extra = a.last_fetch_status === 'ok' && n ? ` +${n}` : '';
  return `<span class="fetch-badge${bad ? ' bad' : ''}" title="${esc(a.last_fetch_note || '')}">`
    + `${esc(FETCH_LABEL[a.last_fetch_status] || a.last_fetch_status)}${extra}</span>`;
}

const fmtWhen = (iso) => (iso ? String(iso).slice(0, 10) : '—');

async function loadAccounts() {
  const res = await fetch(`/api/accounts?${accountQuery()}`);
  if (!res.ok) throw new Error(`${res.status}`);
  state.acctTotal = Number(res.headers.get('X-Total-Count') || 0);
  const list = await res.json();
  $('accountCount').textContent = `共 ${state.acctTotal} 個帳號`;
  const from = state.acctTotal ? state.acctOffset + 1 : 0;
  $('aPageInfo').textContent =
    `${from}–${Math.min(state.acctOffset + ACCT_PAGE, state.acctTotal)} / ${state.acctTotal}`;
  $('aPrev').disabled = state.acctOffset === 0;
  $('aNext').disabled = state.acctOffset + ACCT_PAGE >= state.acctTotal;
  const creatorOpts = ['<option value="">（未歸屬）</option>']
    .concat(state.creators.map((c) => `<option value="${c.id}">${esc(c.display_name)}</option>`)).join('');

  $('accountList').innerHTML = list.map((a) => `
    <div class="card" data-id="${a.id}">
      <div class="card-head">
        <button type="button" class="fav${a.is_favorite ? ' on' : ''}"
                title="我的最愛">${a.is_favorite ? '♥' : '♡'}</button>
        <h3>${esc(a.screen_name || a.platform_user_id)}</h3>
        ${starsHtml(a.stars, 'aStars')}
      </div>
      <div class="muted">${esc(a.platform)} · id ${esc(a.platform_user_id)}</div>
      <div class="muted small">
        ${a.post_count} 則貼文 ·
        <button type="button" class="aViewMedia linkish"
                ${a.media_count ? '' : 'disabled'}
                title="${a.media_count
                  ? '到媒體頁只看這個帳號'
                  : '這個帳號還沒有任何媒體記錄'}">${a.media_count} 個媒體</button> ·
        最後發文 ${fmtWhen(a.last_post_at)} · 最後採集 ${fmtWhen(a.last_ingest_at)}
      </div>
      <div class="muted small">最後檢查 ${fmtWhen(a.last_fetched_at)} ${fetchBadge(a)}</div>
      <div class="row">
        <select class="aRating">${opts(RATINGS, a.default_rating)}</select>
        <select class="aContent">${opts(CONTENTS, a.default_content_type)}</select>
        <button class="aSaveDefaults">存預設</button>
        <button class="aRetag ghost">重標既有</button>
        <span class="card-msg"></span>
      </div>
      <div class="row">
        <select class="aCreator">${creatorOpts}</select>
        <select class="aRole">
          <option value="">（無角色）</option>
          <option value="main">main</option>
          <option value="alt">alt</option>
          <option value="r18_alt">r18_alt</option>
        </select>
        <button class="aLink">歸屬</button>
        <button class="aDelete ghost danger">刪除帳號資料</button>
      </div>
    </div>`).join('') || '<p class="muted">還沒有任何帳號。</p>';

  list.forEach((a) => {
    const card = document.querySelector(`.card[data-id="${a.id}"]`);
    if (a.creator_id) card.querySelector('.aCreator').value = String(a.creator_id);
    if (a.role) card.querySelector('.aRole').value = a.role;

    const msg = card.querySelector('.card-msg');
    const say = (text, cls) => {
      msg.textContent = text;
      msg.className = `card-msg ${cls || ''}`;
      if (cls === 'ok') setTimeout(() => { if (msg.textContent === text) msg.textContent = ''; }, 3500);
    };

    // ♥ 與 ★ 立即送出，且**刻意不重新載入清單** —— 排序若是「我的最愛」，
    // reload 會讓剛按下的卡片瞬間跳到別的位置，滑鼠停在原處的使用者
    // 會以為自己點錯了。順序等下次切分頁或改條件時才更新。
    // 「N 個媒體」本身就是入口 —— 使用者想看的正是那 N 個東西，
    // 再多一顆「看媒體」按鈕只是把同一件事講兩次。
    card.querySelector('.aViewMedia').addEventListener('click', () => {
      jumpToMedia({
        account: a.id,
        label: a.screen_name || a.platform_user_id,
      });
    });

    const fav = card.querySelector('.fav');
    fav.addEventListener('click', async () => {
      const next = !a.is_favorite;
      fav.classList.toggle('on', next);
      fav.textContent = next ? '♥' : '♡';
      try {
        await api(`/api/accounts/${a.id}/prefs`, {
          method: 'PATCH',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ is_favorite: next }),
        });
        a.is_favorite = next;
      } catch (e) {
        fav.classList.toggle('on', a.is_favorite);
        fav.textContent = a.is_favorite ? '♥' : '♡';
        say(`失敗：${e.message}`, 'err');
      }
    });

    wireStars(
      card.querySelector('.aStars'),
      async (stars) => {
        await api(`/api/accounts/${a.id}/prefs`, {
          method: 'PATCH',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ stars }),
        });
        a.stars = stars;
      },
      (e) => say(`評分失敗：${e.message}`, 'err'),
    );

    card.querySelector('.aSaveDefaults').addEventListener('click', async () => {
      try {
        await api(`/api/accounts/${a.id}/defaults`, {
          method: 'PATCH',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({
            default_rating: card.querySelector('.aRating').value || null,
            default_content_type: card.querySelector('.aContent').value || null,
          }),
        });
        say('已存（既有貼文不受影響，要回溯按「重標既有」）', 'ok');
      } catch (e) { say(`失敗：${e.message}`, 'err'); }
    });

    card.querySelector('.aRetag').addEventListener('click', async () => {
      try {
        const r = await api(`/api/accounts/${a.id}/retag`, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ overwrite_manual: false }),
        });
        say(`已重標 ${r.updated} 則（人工標記未被覆蓋）`, 'ok');
      } catch (e) { say(`失敗：${e.message}`, 'err'); }
    });

    card.querySelector('.aLink').addEventListener('click', async () => {
      const cid = card.querySelector('.aCreator').value;
      if (!cid) {
        await api(`/api/accounts/${a.id}/link`, { method: 'DELETE' });
      } else {
        await api(`/api/accounts/${a.id}/link`, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({
            creator_id: Number(cid),
            role: card.querySelector('.aRole').value || null,
          }),
        });
      }
      loadAccounts();
      loadCreators();
    });

    card.querySelector('.aDelete').addEventListener('click', async () => {
      try {
        // 先問「會刪掉什麼」再讓使用者決定 —— 不做「按一下就刪」。
        const p = await api(`/api/accounts/${a.id}/deletion-preview`);
        const lines = [
          `刪除「${p.screen_name}」（${p.platform}）的全部記錄？`,
          '',
          `· ${p.posts} 則貼文`,
          `· ${p.media} 筆媒體記錄`,
          '',
          '本機的媒體檔案不會被刪除。',
          ...p.warnings.map((w) => `⚠️ ${w}`),
          '',
          '這個動作無法復原。',
        ];
        if (!window.confirm(lines.join('\n'))) return;

        const r = await api(`/api/accounts/${a.id}?confirm=true`, { method: 'DELETE' });
        say(`已刪除 ${r.posts} 則貼文 / ${r.media} 筆媒體記錄，${r.downloaded_files_kept} 個檔案留在磁碟上`, 'ok');
        loadAccounts();
      } catch (e) { say(`失敗：${e.message}`, 'err'); }
    });
  });
}

// 搜尋做 debounce：不 debounce 的話打「heikala」是 7 個請求，
// 而且回應順序沒有保證 —— 慢的那個後到就會蓋掉正確結果。
let accountSearchTimer = null;
$('aSearch').addEventListener('input', () => {
  clearTimeout(accountSearchTimer);
  // 換了條件就回第一頁 —— 留在第 20 頁再篩選，多半會看到空白而以為壞了
  state.acctOffset = 0;
  accountSearchTimer = setTimeout(loadAccounts, 250);
});

['aSort', 'aFavOnly', 'aMinStars', 'aFetchStatus',
 'aDefaultRating', 'aDefaultContent'].forEach((id) =>
  $(id).addEventListener('change', () => {
    if (id === 'aSort') localStorage.setItem('accountSort', $('aSort').value);
    state.acctOffset = 0;
    loadAccounts();
  }));

$('aPrev').addEventListener('click', () => {
  state.acctOffset = Math.max(0, state.acctOffset - ACCT_PAGE);
  loadAccounts();
});
$('aNext').addEventListener('click', () => {
  state.acctOffset += ACCT_PAGE;
  loadAccounts();
});

// ── Creators ───────────────────────────────────────────
async function loadCreators() {
  const list = await api('/api/creators');
  state.creators = list;

  $('fCreator').innerHTML = '<option value="">全部 creator</option>'
    + list.map((c) => `<option value="${c.id}">${esc(c.display_name)}</option>`).join('');

  $('creatorList').innerHTML = list.map((c) => `
    <div class="card">
      <h3>${esc(c.display_name)}</h3>
      <div class="muted">${c.accounts.length} 個帳號</div>
      <div class="row">
        ${c.accounts.map((a) => `<span class="pill">${esc(a.platform)} @${esc(a.screen_name || '?')}${a.role ? ` · ${esc(a.role)}` : ''}</span>`).join('') || '<span class="muted">尚未掛任何帳號</span>'}
      </div>
      <div class="row">
        <button class="ghost cViewMedia" data-id="${c.id}"
                data-label="${esc(c.display_name)}">看全部作品</button>
      </div>
    </div>`).join('') || '<p class="muted">還沒有 creator。</p>';

  document.querySelectorAll('.cViewMedia').forEach((b) => {
    b.addEventListener('click', () =>
      jumpToMedia({ creator: b.dataset.id, label: b.dataset.label }));
  });
}

// ── 跳到媒體頁並套上篩選 ──────────────────────────────
//
// ⚠️ 光是把下拉的值改掉**不算數**。使用者剛按的按鈕在另一個分頁上，
// 跳過去之後畫面整個換掉 —— 他要能一眼看出「現在只看得到這個帳號的東西」，
// 否則會以為媒體庫變小了 —— 他得看得出「我剛按的那下成功了嗎」。
//
// 所以除了套篩選，還要在篩選列上放一個講清楚的可移除標籤。

function jumpToMedia({ account, creator, label }) {
  // load: false —— 條件還沒設好，先發一次舊條件的請求純粹是浪費，
  // 而且它會跟下面那次併發（見 showView 的說明）
  showView('media', { load: false });

  // 一次只套一種，另一種要清掉 —— 否則會疊加成「這個 creator 底下的這個帳號」，
  // 而使用者沒有要求那個
  $('fCreator').value = creator || '';
  setAccountFilter(account || '', account ? label : '');

  showJumpChip(label, account ? 'account' : 'creator');
  resetMediaPaging();
  loadMedia();
}

function showJumpChip(label, kind) {
  const bar = $('jumpChip');
  if (!label) { bar.innerHTML = ''; bar.classList.add('hidden'); return; }
  bar.classList.remove('hidden');
  bar.innerHTML = `<span class="chip">只看${kind === 'account' ? '帳號' : ' creator'}
    <b>${esc(label)}</b><button type="button" id="jumpClear"
    aria-label="取消這個篩選">×</button></span>`;
  $('jumpClear').addEventListener('click', () => {
    $('fCreator').value = '';
    setAccountFilter('', '');
    showJumpChip(null);
    resetMediaPaging();
    loadMedia();
  });
}

$('addCreator').addEventListener('click', async () => {
  const name = $('newCreator').value.trim();
  if (!name) return;
  await api('/api/creators', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ display_name: name }),
  });
  $('newCreator').value = '';
  loadCreators();
});

// ── 問題 console ───────────────────────────────────────
async function loadProblems() {
  const errs = await api('/api/errors');
  $('errorList').innerHTML = errs.items.length ? errs.items.map((e) => `
    <div class="err-row">
      <div>
        <b>${esc(e.screen_name || '?')}</b> · ${esc(e.post_id)} · ${esc(e.kind)}
        <div class="msg">${esc(e.error || '未知錯誤')}（試過 ${e.attempt_count} 次）</div>
      </div>
      <span class="spacer"></span>
      <button data-retry="${e.media_id}">重試</button>
    </div>`).join('') : '<p class="muted">目前沒有失敗項目。</p>';

  document.querySelectorAll('[data-retry]').forEach((b) => {
    b.addEventListener('click', async () => {
      await api(`/api/media/${b.dataset.retry}/retry`, { method: 'POST' });
      loadProblems();
      refreshQueue();
    });
  });

  await loadLogs();
}

async function loadLogs() {
  const level = $('logLevel').value;
  const data = await api(`/api/logs?limit=200${level ? `&level=${level}` : ''}`);
  $('logs').innerHTML = data.items.length
    ? data.items.map((r) =>
        `<span class="${esc(r.level)}">${esc(r.ts.slice(11, 19))} [${esc(r.level)}] ${esc(r.message)}</span>`
      ).join('\n')
    : '（沒有日誌）';
}

$('refreshLogs').addEventListener('click', loadLogs);
$('logLevel').addEventListener('change', loadLogs);

$('retryAll').addEventListener('click', async () => {
  const r = await api('/api/media/retry-failed', { method: 'POST' });
  $('errorList').insertAdjacentHTML('beforebegin',
    `<p class="muted" id="retryMsg">已把 ${r.requeued} 個重新排入佇列。</p>`);
  setTimeout(() => $('retryMsg')?.remove(), 3500);
  loadProblems();
  refreshQueue();
});

// ── 佇列列 ─────────────────────────────────────────────
async function refreshQueue() {
  try {
    const q = await api('/api/queue/status');
    const parts = [];
    if (q.pending) parts.push(`<span class="pill pending">待下載 ${q.pending}</span>`);
    if (q.downloading) parts.push(`<span class="pill">下載中 ${q.downloading}</span>`);
    if (q.failed) parts.push(`<span class="pill failed">失敗 ${q.failed}</span>`);
    // 「完成 N」那顆藥丸拿掉了：N 是整個媒體庫的大小（正式庫 224 萬），
    // 不是「這批完成幾個」，它回答不了任何決策，卻要 412 ms 去數。
    // 佇列空的時候要明講是空的，不能整條列變空白 —— 那看起來像壞了。
    if (!parts.length) parts.push('<span class="pill done">佇列空的</span>');
    $('queue').innerHTML = parts.join('');

    const badge = $('errBadge');
    badge.textContent = q.failed;
    badge.classList.toggle('hidden', !q.failed);
    return q;      // 呼叫端用 active / running 決定下次多久再問
  } catch {
    $('queue').innerHTML = '<span class="pill failed">backend 無回應</span>';
    return null;
  }
}

// ── 背景下載開關 ───────────────────────────────────────
function renderDownloadToggle(on) {
  $('autoDownload').checked = on;
  $('dlLabel').textContent = `背景下載：${on ? '開' : '關'}`;
  $('dlToggleWrap').classList.toggle('on', on);
}

async function loadSettings() {
  try {
    const s = await api('/api/settings');
    renderDownloadToggle(s.auto_download);
  } catch { /* backend 沒回應時佇列列已經會顯示，不重複報錯 */ }
}

$('autoDownload').addEventListener('change', async (e) => {
  const want = e.target.checked;
  try {
    const s = await api('/api/settings', {
      method: 'PATCH',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ auto_download: want }),
    });
    renderDownloadToggle(s.auto_download);
  } catch (err) {
    renderDownloadToggle(!want);   // 沒切成功就別讓畫面顯示已切換
    $('dlLabel').textContent = `切換失敗：${err.message}`;
  }
});

// ── 批次抓取 ───────────────────────────────────────────

/** 解析結果只是預覽 —— 這一步**不會寫入任何東西**。
 *  理由跟刪除功能的預演一樣：打錯字不該直接變成一筆垃圾帳號記錄。 */
async function parseUrls() {
  const text = $('fetchUrls').value;
  $('submitUrls').disabled = true;
  if (!text.trim()) {
    $('parseResult').innerHTML = '<div class="muted">先貼一些網址</div>';
    return;
  }
  let body;
  try {
    body = await api('/api/fetch/parse', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ text }),
    });
  } catch (err) {
    $('parseResult').innerHTML = `<div class="bad">解析失敗：${esc(err.message)}</div>`;
    return;
  }

  const rows = body.lines.map((ln) => {
    if (ln.error) {
      return `<tr class="bad"><td>${esc(ln.raw)}</td><td>—</td>
              <td>${esc(ln.error)}</td></tr>`;
    }
    if (ln.duplicate) {
      return `<tr class="muted"><td>${esc(ln.raw)}</td>
              <td>${esc(ln.target.label)}</td><td>這批裡重複</td></tr>`;
    }
    return `<tr><td>${esc(ln.raw)}</td><td>${esc(ln.target.label)}</td>
            <td>${ln.in_db ? '已在資料庫（會做增量）' : '新帳號'}</td></tr>`;
  });

  const ok = body.lines.filter((l) => !l.error && !l.duplicate).length;
  const bad = body.lines.filter((l) => l.error).length;
  $('parseResult').innerHTML =
    `<div class="muted">可抓 ${ok} 個${bad ? `，${bad} 行看不懂` : ''}</div>`
    + `<table class="parse-table"><tbody>${rows.join('')}</tbody></table>`;
  $('submitUrls').disabled = ok === 0;
}

async function submitUrls() {
  $('submitUrls').disabled = true;
  try {
    const body = await api('/api/fetch/batch', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        text: $('fetchUrls').value,
        full: $('fetchFull').checked,
        download: $('fetchDownload').checked,
      }),
    });
    $('parseResult').innerHTML =
      `<div class="good">已排入 ${body.queued} 個帳號</div>`
      + (body.rejected.length
        ? `<div class="bad">${body.rejected.length} 行沒有排入</div>` : '')
      + (body.already_queued.length
        ? `<div class="muted">${esc(body.already_queued.join('、'))} 已經在佇列裡</div>` : '');
    refreshFetchQueue();
  } catch (err) {
    $('parseResult').innerHTML = `<div class="bad">送出失敗：${esc(err.message)}</div>`;
    $('submitUrls').disabled = false;
  }
}

const SKIP_REASONS = {
  cannot_fetch: '只能由 extension 採集（X）',
  untracked: '已取消追蹤',
  pixiv_excluded: '這次沒有包含 pixiv',
  no_credentials: '缺憑證（config.toml 的 platform_credentials）',
  already_queued: '已經在佇列裡',
};

async function refreshAll() {
  $('refreshAll').disabled = true;
  try {
    const body = await api('/api/fetch/refresh-all', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        include_pixiv: $('includePixiv').checked,
        download: $('fetchDownload').checked,
      }),
    });
    // ⚠️ 跳過的一定要列出來。只說「已排入 N 個」的話，
    // 使用者會以為 X 的帳號也更新過了。
    const skips = Object.entries(body.skipped || {}).map(([k, names]) =>
      `<div class="muted">跳過 ${names.length} 個 —— ${esc(SKIP_REASONS[k] || k)}`
      + `：${esc(names.join('、'))}</div>`);
    $('refreshResult').innerHTML =
      `<div class="good">已排入 ${body.queued} 個帳號</div>` + skips.join('');
    refreshFetchQueue();
  } catch (err) {
    $('refreshResult').innerHTML = `<div class="bad">失敗：${esc(err.message)}</div>`;
  } finally {
    $('refreshAll').disabled = false;
  }
}

function jobHtml(job) {
  const r = job.result || {};
  if (job.state === 'done') {
    // 「達到頁數上限」很容易被讀成「抓完了」—— 標出來
    const capped = String(r.stopped_because || '').includes('上限');
    return `<div class="job done"><b>${esc(job.label)}</b>
      <span>新增貼文 ${r.posts_new ?? 0}　新增媒體 ${r.media_new ?? 0}</span>
      <span class="${capped ? 'bad' : 'muted'}">${esc(r.stopped_because || '')}</span></div>`;
  }
  if (job.state === 'running') {
    return `<div class="job running"><b>${esc(job.label)}</b><span>抓取中…</span></div>`;
  }
  if (job.state === 'skipped') {
    return `<div class="job skipped"><b>${esc(job.label)}</b>
      <span class="muted">${esc(job.reason || '')}</span></div>`;
  }
  if (job.state === 'failed') {
    return `<div class="job failed"><b>${esc(job.label)}</b>
      <span class="bad">${esc(job.error || '')}</span></div>`;
  }
  return `<div class="job"><b>${esc(job.label)}</b><span class="muted">排隊中</span></div>`;
}

async function refreshFetchQueue() {
  let st;
  try {
    st = await api('/api/fetch/queue');
  } catch {
    return;
  }
  const c = st.counts;
  const active = c.queued + c.running;
  // 輪詢節奏要看這個：抓取佇列有東西時，即使下載佇列是空的也得繼續盯
  state.fetchActive = active > 0;
  $('fetchBadge').textContent = active;
  $('fetchBadge').classList.toggle('hidden', !active);

  const total = active + c.done + c.failed + c.skipped;
  $('fetchQueueSummary').textContent = active
    ? `第 ${c.done + c.failed + c.skipped + 1} / ${total}　`
      + `完成 ${c.done}　失敗 ${c.failed}　跳過 ${c.skipped}`
    : (total ? `完成 ${c.done}　失敗 ${c.failed}　跳過 ${c.skipped}` : '佇列是空的');

  const limited = Object.entries(st.rate_limited || {});
  $('clearRateLimit').classList.toggle('hidden', limited.length === 0);

  const parts = [];
  if (limited.length) {
    parts.push(`<div class="bad">被限速：${limited
      .map(([k, why]) => `${esc(k)}（${esc(why)}）`).join('、')}</div>`);
  }
  if (st.running) parts.push(jobHtml(st.running));
  parts.push(...st.queued.map(jobHtml));
  parts.push(...st.recent.map(jobHtml));
  $('fetchQueue').innerHTML = parts.join('') || '<div class="muted">還沒有抓過東西</div>';
}

$('parseUrls').addEventListener('click', parseUrls);
$('submitUrls').addEventListener('click', submitUrls);
$('refreshAll').addEventListener('click', refreshAll);
// 改了輸入就讓上一份預覽失效 —— 送出走的是伺服器端重新解析，
// 但畫面上留著舊結果會讓人以為送的是那份
$('fetchUrls').addEventListener('input', () => { $('submitUrls').disabled = true; });

$('clearFetchQueue').addEventListener('click', async () => {
  const r = await api('/api/fetch/queue', { method: 'DELETE' });
  $('fetchQueueSummary').textContent = `已清掉 ${r.cleared} 個還沒開始的`;
  refreshFetchQueue();
});

$('clearRateLimit').addEventListener('click', async () => {
  await api('/api/fetch/rate-limit', { method: 'DELETE' });
  refreshFetchQueue();
});

// ── 輪詢：有事才快，沒事就慢，看不到就停 ─────────────
//
// 舊做法是固定 `setInterval(refreshQueue, 5000)`，切到別的分頁也照跑。
// 那支端點原本要 412 ms（現在 2 ms），但即使便宜了，對一個閒置的佇列
// 每 5 秒問一次「有事嗎」仍然只是在燒電。
//
// 三段式：
//   有東西在跑     → 3 秒（要看得到進度在動）
//   全部閒置       → 30 秒
//   分頁在背景     → 完全不問，切回來時立刻補一次

const POLL_BUSY = 3000;
const POLL_IDLE = 30000;
let pollTimer = null;

function scheduleNextPoll(delay) {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(pollOnce, delay);
}

async function pollOnce() {
  if (document.hidden) return;          // 切回來時由 visibilitychange 接手
  const [queue] = await Promise.all([
    refreshQueue(),
    // 抓取佇列只在記憶體裡，很便宜，但沒必要在背景一直跑
    refreshFetchQueue(),
  ]);
  const busy = queue && (queue.active > 0 || queue.running);
  scheduleNextPoll(busy || state.fetchActive ? POLL_BUSY : POLL_IDLE);
}

document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    clearTimeout(pollTimer);
  } else {
    // 回到分頁的第一件事是把畫面補到最新 —— 使用者剛離開一段時間，
    // 顯示的數字很可能已經過期
    pollOnce();
  }
});

// ── 啟動 ───────────────────────────────────────────────
async function init() {
  applySafeMode();
  // 排序偏好記住。GUI 預設 favorite，而 API 預設是 id（= 舊行為，extension 靠它）
  $('aSort').value = localStorage.getItem('accountSort') || 'favorite';
  $('fSort').value = localStorage.getItem('mediaSort') || 'newest';

  // 首屏只等媒體格線那一個請求。其餘並行且不擋畫面 ——
  // 舊版序列 await 六個請求，最慢的那個決定了「開頁到看見東西」的時間。
  wireAccountPicker();
  const rest = Promise.all([
    loadSettings(),
    loadCreators(),
    pollOnce(),
  ]);
  await loadMedia();
  await rest;
}

init();
