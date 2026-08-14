'use strict';

const $ = (id) => document.getElementById(id);
const PAGE = 60;

const state = {
  view: 'media',
  offset: 0,
  total: 0,
  accounts: [],
  creators: [],
  items: [],              // 目前這頁的媒體，選取與 shift 範圍要用
  selecting: false,
  picked: new Set(),      // media id
  lastPickIndex: null,    // shift 範圍選取的錨點
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

const fmtBytes = (n) => {
  if (!n) return '—';
  const u = ['B', 'KB', 'MB', 'GB'];
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
  state.offset = 0;
  loadMedia();
});

// ── 分頁切換 ───────────────────────────────────────────
document.querySelectorAll('.tab').forEach((btn) => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach((b) => b.classList.remove('active'));
    btn.classList.add('active');
    state.view = btn.dataset.view;
    document.querySelectorAll('.view').forEach((v) => v.classList.add('hidden'));
    $(`view-${state.view}`).classList.remove('hidden');
    if (state.view === 'media') loadMedia();
    if (state.view === 'fetch') refreshFetchQueue();
    if (state.view === 'accounts') loadAccounts();
    if (state.view === 'creators') loadCreators();
    if (state.view === 'problems') loadProblems();
  });
});

// ── 媒體格線 ───────────────────────────────────────────
function mediaQuery() {
  const p = new URLSearchParams();
  p.set('limit', PAGE);
  p.set('offset', state.offset);
  if (safeMode) p.set('exclude_rating', 'r18');
  p.set('sort', $('fSort').value);
  const map = {
    account_id: $('fAccount').value,
    creator_id: $('fCreator').value,
    rating: $('fRating').value,
    content_type: $('fContent').value,
    kind: $('fKind').value,
    status: $('fStatus').value,
    min_stars: $('fMinStars').value,
  };
  for (const [k, v] of Object.entries(map)) if (v) p.set(k, v);
  return p.toString();
}

function cellHtml(m) {
  const isPhoto = m.kind === 'photo';
  const missing = m.status !== 'done';
  const body = missing
    ? `<div class="missing">${m.status === 'failed' ? '下載失敗' : '尚未下載'}</div>`
    : isPhoto
      ? `<img loading="lazy" src="/api/media/${m.id}/file" alt="" onerror="this.parentNode.innerHTML='&lt;div class=missing&gt;檔案遺失&lt;/div&gt;'">`
      : `<video preload="metadata" muted src="/api/media/${m.id}/file"></video>`;

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

async function loadMedia() {
  try {
    const data = await api(`/api/media?${mediaQuery()}`);
    state.total = data.total;
    state.items = data.items;
    $('grid').innerHTML = data.items.length
      ? data.items.map(cellHtml).join('')
      : '<p class="muted">沒有符合條件的媒體。</p>';
    $('mediaCount').textContent = `共 ${data.total} 個媒體`;
    const from = data.total ? state.offset + 1 : 0;
    const to = Math.min(state.offset + PAGE, data.total);
    $('pageInfo').textContent = `${from}–${to} / ${data.total}`;
    $('prevPage').disabled = state.offset === 0;
    $('nextPage').disabled = state.offset + PAGE >= data.total;

    document.querySelectorAll('.cell').forEach((c, index) => {
      const id = Number(c.dataset.id);
      if (state.picked.has(id)) c.classList.add('picked');
      c.addEventListener('click', (ev) => {
        if (state.selecting) togglePick(id, index, ev.shiftKey);
        else showDetail(id);
      });
    });
    updateSelInfo();
  } catch (e) {
    $('grid').innerHTML = `<p class="muted">載入失敗：${esc(e.message)}</p>`;
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

const MEDIA_FILTERS = ['fAccount', 'fCreator', 'fRating', 'fContent', 'fKind', 'fStatus', 'fMinStars'];

MEDIA_FILTERS.forEach((id) =>
  $(id).addEventListener('change', () => { state.offset = 0; loadMedia(); }));

$('fSort').addEventListener('change', () => {
  localStorage.setItem('mediaSort', $('fSort').value);
  state.offset = 0;
  loadMedia();
});

$('fReset').addEventListener('click', () => {
  // 排序不是篩選 —— 「清除篩選」不該把使用者選的排序也一起打掉
  MEDIA_FILTERS.forEach((id) => { $(id).value = ''; });
  state.offset = 0;
  loadMedia();
});

$('prevPage').addEventListener('click', () => {
  state.offset = Math.max(0, state.offset - PAGE);
  loadMedia();
});
$('nextPage').addEventListener('click', () => {
  state.offset += PAGE;
  loadMedia();
});

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
      loadMedia();   // 安全模式下標成 r18 應該立刻從格線消失
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
      // 格線上的星星角標要跟著更新；若正在依評分排序，順序也會變
      loadMedia();
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

/** 媒體頁的「全部帳號」下拉。**必須是未經篩選的完整清單** ——
 *  跟帳號頁共用一次請求的話，帳號頁一搜尋，媒體頁的下拉就跟著少一半。 */
async function loadAccountOptions() {
  state.accounts = await api('/api/accounts?sort=name');
  $('fAccount').innerHTML = '<option value="">全部帳號</option>'
    + state.accounts.map((a) =>
        `<option value="${a.id}">${esc(a.screen_name || a.platform_user_id)}</option>`).join('');
}

function accountQuery() {
  const p = new URLSearchParams();
  p.set('sort', $('aSort').value);
  const q = $('aSearch').value.trim();
  if (q) p.set('q', q);
  if ($('aFavOnly').checked) p.set('favorite', 'true');
  if ($('aMinStars').value) p.set('min_stars', $('aMinStars').value);
  return p.toString();
}

const fmtWhen = (iso) => (iso ? String(iso).slice(0, 10) : '—');

async function loadAccounts() {
  const list = await api(`/api/accounts?${accountQuery()}`);
  $('accountCount').textContent = `${list.length} 個帳號`;
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
        ${a.post_count} 則貼文 · ${a.media_count} 個媒體 ·
        最後發文 ${fmtWhen(a.last_post_at)} · 最後採集 ${fmtWhen(a.last_ingest_at)}
      </div>
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
  accountSearchTimer = setTimeout(loadAccounts, 250);
});

['aSort', 'aFavOnly', 'aMinStars'].forEach((id) =>
  $(id).addEventListener('change', () => {
    if (id === 'aSort') localStorage.setItem('accountSort', $('aSort').value);
    loadAccounts();
  }));

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
        <button class="ghost cViewMedia" data-id="${c.id}">看全部作品</button>
      </div>
    </div>`).join('') || '<p class="muted">還沒有 creator。</p>';

  document.querySelectorAll('.cViewMedia').forEach((b) => {
    b.addEventListener('click', () => {
      document.querySelector('.tab[data-view="media"]').click();
      $('fCreator').value = b.dataset.id;
      state.offset = 0;
      loadMedia();
    });
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
    parts.push(`<span class="pill done">完成 ${q.done}</span>`);
    if (q.failed) parts.push(`<span class="pill failed">失敗 ${q.failed}</span>`);
    $('queue').innerHTML = parts.join('');

    const badge = $('errBadge');
    badge.textContent = q.failed;
    badge.classList.toggle('hidden', !q.failed);
  } catch {
    $('queue').innerHTML = '<span class="pill failed">backend 無回應</span>';
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

// ── 啟動 ───────────────────────────────────────────────
async function init() {
  applySafeMode();
  // 排序偏好記住。GUI 預設 favorite，而 API 預設是 id（= 舊行為，extension 靠它）
  $('aSort').value = localStorage.getItem('accountSort') || 'favorite';
  $('fSort').value = localStorage.getItem('mediaSort') || 'newest';
  await loadSettings();
  await loadCreators();
  await loadAccountOptions();
  await loadAccounts();
  await loadMedia();
  await refreshQueue();
  await refreshFetchQueue();
  setInterval(refreshQueue, 5000);
  // 抓取佇列跑得比下載慢，但進度要看得到在動
  setInterval(refreshFetchQueue, 3000);
}

init();
