// 帳號清單、編輯抽屜、創作者檢視。
//
// 設計依據（wiki 的 UI_帳號管理）：
//   · 4,653 筆 —— **搜尋是入口，清單不是**
//   · 卡上只留高頻（♥ ★ 看媒體），低頻與破壞性的全部收進 [編輯] 抽屜
//   · 三個日期欄位有**兩個的資訊量是零**（`last_ingest_at` 4,648/4,653 同一天、
//     `last_fetched_at` 全空），所以卡面改成一行**結論**而不是三行原始資料

import {
  $, esc, fmtWhen, mountDrops, multiDrop, singleDrop, starsHtml, handleStarClick,
} from '../dom.js';
import { api } from '../api.js';
import { state } from '../state.js';
import { openOverlay, confirmDialog } from '../overlay.js';
import { jumpToMedia, paintMoreNotes } from './media.js';
import { RATING_VALUES, CONTENT_VALUES } from '../enums.js';

// 帳號頁一定要分頁。匯入舊資料後這個庫有 4,653 個帳號 ——
// 一次渲染完整份會把瀏覽器凍住（實測 CDP 直接逾時）。
const ACCT_PAGE = 100;
// 一頁 100 張卡也不是一次掛完：先掛 50，捲到底再補下一批。
// 4,653 筆時 DOM 節點數要壓在 1,500 以下。
const CHUNK = 50;

// 非 ok/no_new 就是需要注意的狀態
const FETCH_BAD = ['not_found', 'rate_limited', 'auth_required', 'failed'];

const FETCH_LABEL = {
  ok: '有新的', no_new: '沒有新的', not_found: '找不到（可能改名）',
  rate_limited: '被限速', auth_required: '需要憑證', failed: '失敗', skipped: '已跳過',
};

function accountQuery() {
  const p = new URLSearchParams();
  p.set('sort', aDrops.aSort.get());
  p.set('limit', ACCT_PAGE);
  p.set('offset', state.acctOffset);
  const q = $('aSearch').value.trim();
  if (q) p.set('q', q);
  const platform = aDrops.aPlatform.get();
  if (platform) p.set('platform', platform);
  if ($('aFavOnly').checked) p.set('favorite', 'true');
  for (const v of aDrops.aStars?.get() ?? []) p.append('stars', v);
  // `__unset__` 直接原樣送 —— 空字串在 query string 裡與「不篩選」分不出來
  const dr = aDrops.aDefaultRating.get();
  const dc = aDrops.aDefaultContent.get();
  if (dr) p.set('default_rating', dr);
  if (dc) p.set('default_content_type', dc);
  // 都不勾 = 不篩選（兩者都回）；兩個都勾也是全部，語意等價。
  // 只勾一個才是真的條件。
  const ig = aDrops.aIgnored?.get() ?? [];
  if (ig.length === 1) p.set('ignored', ig[0]);
  // ⚠️ **不可以在前端濾** —— 前端只看得到當頁的 100 筆，使用者會在一頁
  // 全是「從沒檢查過」的清單上看到 0 筆，然後以為沒有任何帳號有問題。
  // 實測就是這樣錯的。所以一律把值送給後端。
  const fs = aDrops.aFetchStatus?.get() ?? [];
  if (fs.length) p.set('fetch_status', fs.join(','));
  return p.toString();
}

/** 帳號頁「更多篩選」的三個多選下拉。`wireAccountFilters()` 之後才有東西。 */
const aDrops = {};

/** 評分：篩**特定星數**不是「幾星以上」（2026-08-19 使用者裁示）。
 *  ⚠️ 沒有「未評分」這一項 —— NULL 不是 0 分。 */
const A_STARS = ['5', '4', '3', '2', '1'];

/** 擷取結果的值域。⚠️ 舊版有一個 `__bad__` 聚合選項（「只看抓取有問題的」）；
 *  改多選之後它沒有存在的必要 —— 使用者直接把那四個勾起來就是同一件事，
 *  而聚合選項與具體選項混在同一張清單裡會讓「全選」的語意講不清楚。 */
const A_FETCH = ['ok', 'no_new', 'not_found', 'rate_limited', 'auth_required', 'failed', 'skipped'];

/** 排序鍵的值域與顯示字。值域**同時是白名單** —— 見 `storedAccountSort()`。 */
const A_SORT = [
  { value: 'favorite', text: '我的最愛 → 評分' },
  { value: 'stars', text: '評分高到低' },
  { value: 'name', text: '名稱' },
  { value: 'last_post', text: '最後發文' },
  { value: 'last_ingest', text: '最後採集' },
  { value: 'last_fetch', text: '最久沒檢查' },
  { value: 'media', text: '媒體數' },
  { value: 'posts', text: '貼文數' },
  { value: 'created', text: '建檔時間' },
  { value: 'id', text: '加入順序' },
];

/** 記住的排序偏好，**經過白名單**。
 *
 *  ⚠️ 原生 `<select>` 時代這裡是直接 `.value = localStorage.…`：塞一個不存在的
 *  值進去，select 會自己變成空字串，錯誤被吞掉。自製下拉沒有那個保護 ——
 *  認不得的值會原樣送給後端。所以驗證從「順手做」變成「非做不可」。
 *
 *  GUI 預設 favorite，而 API 預設是 id（= 舊行為，extension 靠它）。 */
function storedAccountSort() {
  const raw = localStorage.getItem('accountSort') || '';
  return A_SORT.some((o) => o.value === raw) ? raw : 'favorite';
}

/** 「（未設定）」放在值清單的最前面：「哪些我還沒標」才是主要用例。 */
const unsetFirst = (values) =>
  [{ value: '__unset__', text: '（未設定）' }, ...values.map((v) => ({ value: v }))];

export function wireAccountFilters() {
  // ── 單選的四個 ──
  // ⚠️ 這幾個的變動**不能**再靠模組底部那串 addEventListener('change')：
  // 它們現在是 <span>，change 事件永遠不會發生，而且掛得上去、不會報錯。
  const single = (id, label, values, opt = {}) => {
    aDrops[id] = singleDrop($(id), {
      label,
      values,
      // 收起時顯示的就是 label，所以「回到不限」那一項用同一句話。
      emptyText: label,
      onChange: () => {
        // 排序不改變「有哪些帳號符合條件」，所以它不必清掉選取。其餘都要。
        if (id !== 'aSort') clearAcctSelection('換了篩選 —— 選取已清空');
        else localStorage.setItem('accountSort', aDrops.aSort.get());
        state.acctOffset = 0;
        loadAccounts();
      },
      ...opt,
    });
  };
  // 平台選項帶筆數，由 `loadPlatforms()` 之後用 setOptions() 補上。
  single('aPlatform', '全部平台', [], { value: '' });
  // ⚠️ 排序**不給 emptyText**：它一定有值，「不排序」不是一個選項。
  // 給了會多出一個選了等於沒選的空項。
  single('aSort', '排序', A_SORT, { value: storedAccountSort(), emptyText: undefined });
  single('aDefaultRating', '預設分級：全部', unsetFirst(RATING_VALUES));
  single('aDefaultContent', '預設類型：全部', unsetFirst(CONTENT_VALUES));

  const mk = (id, label, values, text) => {
    aDrops[id] = multiDrop($(id), {
      label,
      values: values.map((v) => ({ value: v, text: text ? text(v) : undefined })),
      onChange: () => {
        clearAcctSelection('換了篩選 —— 選取已清空');
        state.acctOffset = 0;
        loadAccounts();
      },
    });
  };
  mk('aStars', '評分', A_STARS, (v) => '★'.repeat(Number(v)));
  mk('aIgnored', '忽略', ['true', 'false'],
     (v) => (v === 'true' ? '已忽略' : '沒被忽略'));
  mk('aFetchStatus', '擷取結果', A_FETCH);
}

const acctName = (a) => a.screen_name || a.platform_user_id;

/** #14 抓取狀態結論行 —— 這是 D2 的**答案**，不是原始資料。
 *
 *  卡面原本印三個日期，其中兩個在正式庫的鑑別力是零。刪掉欄位不對
 *  （使用者開始用抓取功能之後它們就有值了），正解是：沒有值的時候誠實說
 *  「還沒有」，而不是印一個 2026-08-14 讓人以為那代表什麼。 */
function verdict(a) {
  const st = a.last_fetch_status;
  if (!st && !a.last_fetched_at) {
    return { text: '尚未由本工具抓取過', bad: false };
  }
  const when = fmtWhen(a.last_fetched_at);
  const md = when.length >= 10 ? when.slice(5) : when;
  if (FETCH_BAD.includes(st)) {
    // 形狀載體（⚠ 前綴）由 app.css 的 `.bad::before` 統一加 —— 這裡**不要**
    // 自己再寫一個，否則畫面上會出現兩個 ⚠。
    const why = a.last_fetch_note || FETCH_LABEL[st] || st;
    return { text: `上次失敗：${why}`, bad: true, full: a.last_fetched_at };
  }
  const days = a.last_fetched_at
    ? Math.floor((Date.now() - Date.parse(a.last_fetched_at)) / 86400000)
    : null;
  if (days != null && days > 30) {
    return { text: `已 ${days} 天沒檢查（上次 ${md}）`, bad: false, full: a.last_fetched_at };
  }
  if (st === 'ok' && a.last_fetch_new_posts) {
    return { text: `上次 ${md} 抓到 ${a.last_fetch_new_posts} 則`, full: a.last_fetched_at };
  }
  if (st === 'skipped') {
    return { text: `上次 ${md} 跳過：${a.last_fetch_note || '—'}`, full: a.last_fetched_at };
  }
  return { text: `上次 ${md} 檢查，沒有新的`, full: a.last_fetched_at };
}

/** 自動退訂的告示 + 反悔按鈕。
 *
 *  ⚠️ 判斷用後端算好的 `auto_untracked` 布林，**不比對 note 的文字** ——
 *  改一次文案前端就靜默失效。
 *
 *  退訂本身不刪任何資料，這句話一定要寫出來：使用者看到「已移出追蹤」
 *  的第一個念頭是「我的東西還在嗎」。 */
function untrackedHtml(a) {
  if (!a.auto_untracked) return '';
  // ⚠ 前綴由 `.bad::before` 加，見 verdict() 的說明。
  return `<span class="card-verdict bad">已自動移出追蹤（連續 ${
    a.not_found_streak} 次找不到）—— 既有資料一筆都沒動
    <button type="button" class="linkish" data-act="retrack">恢復追蹤</button></span>`;
}

/** 使用者標記的「忽略」。
 *
 *  ⚠️ **與上面那個自動退訂的告示視覺與語氣都要不同。** 兩者的效果相似
 *  （都會被一鍵更新跳過），但一個是我按的、一個是系統做的 ——
 *  而下一步不一樣：系統退訂的該去查是不是改名了，我標的不用管。
 *  所以符號分開（⊘ vs ⚠）、語氣分開（中性陳述 vs 講原因與次數），
 *  **不只靠顏色**（灰階下顏色會消失）。
 *
 *  0 個被忽略是常態（這是新旗標），所以沒有時整塊不佔高度。 */
function ignoredHtml(a) {
  if (!a.is_ignored) return '';
  return `<span class="card-ignored">⊘ 已忽略 —— 一鍵更新會跳過它，資料一筆都沒動
    <button type="button" class="linkish" data-act="unignore">取消忽略</button></span>`;
}

/** 「↗ 在 … 開啟」。網址與問題說明都由後端的 links.py 給 ——
 *  **這裡不拼任何平台網址** —— 寫死某個平台的網址會讓其他平台連到
 *  不存在的位址，那不是報錯而是連到錯的地方，比 404 更難發現。
 *
 *  ⚠️ 刻意**不加 `data-act`**：卡片的 click 委派只處理 `[data-act]`，
 *  沒有它這個 `<a>` 才會走原生導覽。加了反而要自己 `window.open`，
 *  中鍵開新分頁、複製網址那些原生行為就全沒了。 */
function platformLinkHtml(a) {
  if (a.profile_url) {
    return `<a class="ext-link" href="${esc(a.profile_url)}" target="_blank"
              rel="noreferrer">↗ 在 ${esc(a.platform_label || a.platform)} 開啟</a>`;
  }
  // 拼不出來時顯示**原因**而不是一個壞連結。灰階 + ⚠ 字符，不只靠顏色分辨。
  return `<span class="ext-link off" data-tip="${esc(a.link_problem || '')}"
            tabindex="0">⚠ 無法連結</span>`;
}

function cardHtml(a) {
  const v = verdict(a);
  const n = (x) => (x || 0).toLocaleString();
  const defaults = [a.default_rating, a.default_content_type].filter(Boolean).join(' · ');
  // ⚠️ 用 `isPicked()` 不是 `acctPicked.has()` —— 範圍是「全部符合篩選的」時，
  // 本頁的卡也該顯示成已選，而那些 id 在 acctAllIdSet 裡不在 acctPicked 裡。
  const picked = isPicked(a.id);
  // 選取模式時卡片多一個核取方塊，**而且整張卡加框** —— 一頁 100 張時
  // 只靠角落一個小方塊掃不出選了哪些（滿載才是分組必須成立的時候）。
  const box = acctSelecting
    ? `<input type="checkbox" class="acct-pick" data-act="pick"
        ${picked ? 'checked' : ''} aria-label="選取 ${esc(acctName(a))}">`
    : '';
  return `<div class="card${picked ? ' picked' : ''}" data-id="${a.id}">
    <div class="card-head">
      ${box}
      <button type="button" class="fav${a.is_favorite ? ' on' : ''}"
              data-act="fav" data-tip="我的最愛（點了立即生效）">${a.is_favorite ? '♥' : '♡'}</button>
      <h3>${esc(acctName(a))}</h3>
      ${starsHtml(a.stars, 'aStars')}
    </div>
    <div class="card-id">
      <span>${esc(a.platform)} · id ${esc(a.platform_user_id)}</span>
      ${platformLinkHtml(a)}
    </div>
    <div class="card-stats">
      <button type="button" class="linkish" data-act="viewmedia"
              ${a.media_count ? '' : 'disabled'}
              data-tip="${a.media_count
                ? '到媒體頁只看這個帳號' : '這個帳號還沒有任何媒體記錄'}"
              >${n(a.media_count)} 個媒體</button>
      · <span class="num">${n(a.post_count)}</span> 則貼文<br>
      最後發文 ${esc(fmtWhen(a.last_post_at))}
      <span class="card-verdict${v.bad ? ' bad' : ''}"${
        v.full ? ` data-tip="${esc(v.full)}"` : ''}>${esc(v.text)}</span>
      ${untrackedHtml(a)}
      ${ignoredHtml(a)}
    </div>
    ${previewHtml(a)}
    <div class="card-foot">
      <span>預設 ${defaults ? esc(defaults) : '（未設定）'}</span>
      <span class="spacer"></span>
      <span class="card-msg"></span>
      <button type="button" class="ghost" data-act="edit">編輯</button>
    </div>
  </div>`;
}

/** 預覽縮圖。**最新的幾張，不濾分級**（使用者拍板：濾了會出現缺口，
 *  而預覽的用途是快速認出這是誰）。
 *
 *  ⚠️ 影片**現在有縮圖了**（ffmpeg 抽格），所以多數格子會正常顯示。
 *  但載入仍可能失敗：沒裝 ffmpeg（503）、原檔不在（404）、格式沒救（415）。
 *  失敗時顯示 ▶ 佔位而不是破圖。這件事在 SQL 那層不處理 ——
 *  加 `kind='photo'` 會讓查詢從 359 ms 變成 3.6 分鐘
 *  （planner 改從 ix_media_status 驅動）。
 *
 *  src 留空，由 IntersectionObserver 捲到才填（一頁 100 張卡 = 400 張縮圖）。 */
function previewHtml(a) {
  const ids = a.preview || [];
  if (!ids.length) {
    // 空白格與「縮圖載入失敗」長得一樣 —— 要講出是哪一種
    return a.media_count
      ? '<div class="prev-empty">預覽還沒算出來（跑一次 recount-accounts）</div>'
      : '<div class="prev-empty">這個帳號還沒有媒體</div>';
  }
  return `<div class="prev">${ids.map((id) =>
    `<span class="prev-cell"><img alt="" data-src="/api/media/${id}/thumb"></span>`
  ).join('')}</div>`;
}

// 預覽縮圖的延遲載入。與媒體格線同一套做法：捲到才發請求。
let prevObserver = null;

function wirePreviewImages() {
  if (!prevObserver) {
    prevObserver = new IntersectionObserver((entries, obs) => {
      for (const e of entries) {
        if (!e.isIntersecting) continue;
        const img = e.target;
        img.onerror = () => {
          // 影片、或原檔不在了。**不要留破圖** —— 那與「這裡本來就沒東西」
          // 分不出來。▶ 是形狀載體，灰階也看得出是可播放的東西。
          img.replaceWith(Object.assign(document.createElement('span'), {
            className: 'prev-alt', textContent: '▶',
          }));
        };
        img.src = img.dataset.src;
        obs.unobserve(img);
      }
    }, { rootMargin: '300px' });
  }
  for (const img of $('accountList').querySelectorAll('.prev img:not([src])')) {
    prevObserver.observe(img);
  }
}

// ── 清單載入 ───────────────────────────────────────────

let pending = [];        // 還沒掛上去的卡（分批渲染用）
let sentinelObserver = null;
let acctSeq = 0;

function mountChunk() {
  if (!pending.length) return;
  const batch = pending.splice(0, CHUNK);
  // ⚠️ 純字串拼接 + 一次插入，**不逐張綁任何 listener** ——
  // 卡上的每個動作（♥、★、看媒體、編輯）都走 #accountList 上那一個委派。
  $('accountList').insertAdjacentHTML('beforeend', batch.map(cardHtml).join(''));
  wirePreviewImages();
  if (!pending.length && sentinelObserver) sentinelObserver.disconnect();
}

function wireSentinel() {
  if (sentinelObserver) sentinelObserver.disconnect();
  sentinelObserver = new IntersectionObserver((entries) => {
    if (entries.some((e) => e.isIntersecting)) mountChunk();
  }, { rootMargin: '400px' });
  sentinelObserver.observe($('acctSentinel'));
}

function say(card, text, cls) {
  const msg = card.querySelector('.card-msg');
  if (!msg) return;
  msg.textContent = text;
  msg.className = `card-msg ${cls || ''}`;
  if (cls === 'ok') {
    setTimeout(() => { if (msg.textContent === text) msg.textContent = ''; }, 3500);
  }
}

/** 骨架卡：保留版面高度。空白會讓版面塌陷再彈回。 */
function skeletons(k = 8) {
  return Array.from({ length: k }, () => '<div class="skeleton"></div>').join('');
}

export async function loadAccounts() {
  if (state.acctMode === 'creators') return loadCreators();
  const seq = ++acctSeq;
  unsetRating = null;          // 條件變了，上一批的數字就不再成立
  $('accountList').innerHTML = skeletons();
  $('accountCount').textContent = '計算中…';

  let res;
  try {
    res = await fetch(`/api/accounts?${accountQuery()}`);
    if (!res.ok) throw new Error(`${res.status}`);
  } catch (e) {
    if (seq !== acctSeq) return;
    $('accountList').innerHTML = `<p class="empty">載入失敗：${esc(e.message)}</p>`;
    $('accountCount').textContent = '';
    return;
  }
  const list = await res.json();
  if (seq !== acctSeq) return;          // 打字很快時，慢的那個後到會蓋掉正確結果

  state.acctTotal = Number(res.headers.get('X-Total-Count') || 0);
  state.accounts = list;
  paintAccountCount();

  const from = state.acctTotal ? state.acctOffset + 1 : 0;
  $('aPageInfo').textContent = state.acctTotal
    ? `${from}–${Math.min(state.acctOffset + ACCT_PAGE, state.acctTotal)} / ${state.acctTotal}`
    : '—';
  $('aPrev').disabled = state.acctOffset === 0;
  $('aNext').disabled = state.acctOffset + ACCT_PAGE >= state.acctTotal;

  $('accountList').innerHTML = '';
  if (!list.length) {
    $('accountList').innerHTML = emptyAccountsHtml();
    return;
  }
  pending = list.slice();
  mountChunk();
  wireSentinel();
  fetchUnsetCount();
  // 選取列的「全選本頁 N」要用**這一次**載回來的筆數，不是上一次的
  renderSelBar();
}

function emptyAccountsHtml() {
  const q = $('aSearch').value.trim();
  if (q) {
    return `<p class="empty">找不到符合「${esc(q)}」的帳號。<br>
      <button type="button" class="ghost" data-act="clearsearch">清除搜尋</button></p>`;
  }
  const conds = [
    aDrops.aPlatform.get() ? `平台 ${aDrops.aPlatform.get()}` : '',
    $('aFavOnly').checked ? '只看 ♥' : '',
    (aDrops.aStars?.get() ?? []).length
      ? `評分 ${aDrops.aStars.get().map((v) => '★'.repeat(Number(v))).join('、')}` : '',
    aDrops.aDefaultRating.get() ? `預設分級 ${aDrops.aDefaultRating.get()}` : '',
    aDrops.aDefaultContent.get() ? `預設類型 ${aDrops.aDefaultContent.get()}` : '',
    (aDrops.aFetchStatus?.get() ?? []).length
      ? `擷取結果 ${aDrops.aFetchStatus.get().join('、')}` : '',
  ].filter(Boolean);
  if (conds.length) {
    return `<p class="empty">沒有帳號符合目前條件。<br>生效中：${esc(conds.join('、'))}</p>`;
  }
  return '<p class="empty">還沒有任何帳號。<br>到「抓取」貼幾個網址，或用 extension 採集。</p>';
}

// 「還沒設預設值的有幾個」——一次請求、只要 header 上的總數。
// 不擋畫面：它回來之前那半句就先不顯示（**不顯示 0**，0 是另一個意思）。
let unsetRating = null;

/** 清單層級的第 6 題：不只說有幾個帳號，說**整理工作還剩多少**。 */
function paintAccountCount() {
  const el = $('accountCount');
  el.innerHTML = `共 <b class="todo">${state.acctTotal.toLocaleString()}</b> 個帳號`
    + (unsetRating == null ? '' :
      unsetRating
        ? `　<span class="muted">其中 ${unsetRating.toLocaleString()} 個還沒設分級預設值</span>`
        : '　<span class="muted">全部都設過分級預設值了</span>');
}

async function fetchUnsetCount() {
  // ⚠️ **要跟著目前的篩選走。** 一開始這裡問的是全庫，結果套了平台篩選之後
  // 畫面變成「共 12 個帳號　其中 13 個還沒設分級預設值」—— 13 > 12，
  // 使用者只會覺得這個數字是壞的。它回答的是「我眼前這批還剩多少要整理」。
  //
  // 已經在依預設分級篩選時整句不顯示：那時清單本身就是答案。
  if (aDrops.aDefaultRating.get()) { unsetRating = null; paintAccountCount(); return; }
  const p = new URLSearchParams(accountQuery());
  p.set('default_rating', '__unset__');
  p.set('limit', '1');
  p.set('offset', '0');
  p.set('with_stats', 'false');
  try {
    const res = await fetch(`/api/accounts?${p}`);
    if (!res.ok) return;
    unsetRating = Number(res.headers.get('X-Total-Count') || 0);
    paintAccountCount();
  } catch { /* 補充資訊，拿不到就不顯示，不必報錯 */ }
}

// ── 卡片上的動作（事件委派）───────────────────────────
//
// ⚠️ 一個 listener 掛在容器上，不是每張卡各綁一輪。100 張卡 × 4 個動作
// = 400 個 listener，而且每次重畫都會再產生一批。

$('accountList').addEventListener('click', async (ev) => {
  if (ev.target.closest('[data-act="clearsearch"]')) {
    $('aSearch').value = '';
    state.acctOffset = 0;
    loadAccounts();
    return;
  }
  const card = ev.target.closest('.card');
  if (!card) return;
  const a = state.accounts.find((x) => x.id === Number(card.dataset.id));
  if (!a) return;

  // ★ 評分。點了立即生效、失敗會還原（handleStarClick 負責）
  const wasStar = await handleStarClick(
    ev,
    async (stars) => {
      await api(`/api/accounts/${a.id}/prefs`, {
        method: 'PATCH',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ stars }),
      });
      a.stars = stars;
    },
    (e) => say(card, `評分失敗：${e.message}`, 'err'),
  );
  if (wasStar) return;

  // ⚠️ 預覽格沒有 data-act，要在下面那道 `if (!btn) return` **之前**判斷。
  // 點預覽 = 想看這個帳號的東西，與點「N 個媒體」是同一個意圖。
  if (ev.target.closest('.prev')) {
    jumpToMedia({ account: a.id, label: acctName(a) });
    return;
  }

  const btn = ev.target.closest('[data-act]');
  if (!btn) return;

  if (btn.dataset.act === 'pick') {
    // 從「全部符合篩選的」手動改一張 = 範圍退回本頁。**要講出來** ——
    // 不講的話使用者以為還選著 4,653 個，實際上只剩這一頁。
    if (acctPickScope === 'all') {
      acctPickScope = 'page';
      acctPicked.clear();
      for (const x of state.accounts) acctPicked.add(x.id);
      acctAllIds = [];
      acctAllIdSet = new Set();
      $('aSelBar').dataset.note = '手動改了選取 —— 範圍從「全部符合篩選的」退回「本頁」';
    }
    if (btn.checked) acctPicked.add(a.id); else acctPicked.delete(a.id);
    card.classList.toggle('picked', btn.checked);
    renderSelBar();
    return;
  }

  if (btn.dataset.act === 'unignore') {
    // 立即生效，不必按儲存（與卡上的 ♥ ★ 同一個模型）
    btn.disabled = true;
    try {
      await api(`/api/accounts/${a.id}/prefs`, {
        method: 'PATCH',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ is_ignored: false }),
      });
      a.is_ignored = false;
      card.outerHTML = cardHtml(a);
    } catch (e) {
      btn.disabled = false;
      say(card, `取消忽略失敗：${e.message}`, 'err');
    }
    return;
  }

  if (btn.dataset.act === 'viewmedia') {
    jumpToMedia({ account: a.id, label: acctName(a) });
  } else if (btn.dataset.act === 'retrack') {
    // 恢復追蹤。後端會一併把 not_found_streak 歸零 —— 不歸零的話
    // 下一次找不到就是第 3 次，馬上又被退訂。
    btn.disabled = true;
    try {
      await api(`/api/accounts/${a.id}/prefs`, {
        method: 'PATCH',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ is_tracked: true }),
      });
      a.is_tracked = true;
      a.auto_untracked = false;
      a.not_found_streak = 0;
      // 這一張重畫就好 —— 整份重載會讓捲動位置跳掉。
      card.outerHTML = cardHtml(a);
    } catch (e) {
      btn.disabled = false;
      say(card, `恢復追蹤失敗：${e.message}`, 'err');
    }
  } else if (btn.dataset.act === 'edit') {
    openAccountDrawer(a, card);
  } else if (btn.dataset.act === 'fav') {
    // ♥ 立即送出，且**刻意不重新載入清單** —— 排序若是「我的最愛」，
    // reload 會讓剛按下的卡片瞬間跳到別的位置，滑鼠停在原處的使用者
    // 會以為自己點錯了。順序等下次切分頁或改條件時才更新。
    const next = !a.is_favorite;
    btn.classList.toggle('on', next);
    btn.textContent = next ? '♥' : '♡';
    try {
      await api(`/api/accounts/${a.id}/prefs`, {
        method: 'PATCH',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ is_favorite: next }),
      });
      a.is_favorite = next;
    } catch (e) {
      btn.classList.toggle('on', a.is_favorite);
      btn.textContent = a.is_favorite ? '♥' : '♡';
      say(card, `失敗：${e.message}`, 'err');
    }
  }
});

// ── [編輯] 抽屜：低頻 + 破壞性 ────────────────────────

function openAccountDrawer(a, card) {
  // 「（未歸屬）」那一項不必自己加 —— singleDrop 的 emptyText 會補在最前面。
  const creatorOpts = state.creators.map((c) =>
    ({ value: String(c.id), text: c.display_name }));

  openOverlay({
    kind: 'drawer',
    title: acctName(a),
    subtitle: `${a.platform} · id ${a.platform_user_id}`,
    body: `
      <div class="ovl-section">
        <h3>新貼文的預設值</h3>
        <div class="row">
          <span id="dfRating" class="ms-host"></span>
          <span id="dfContent" class="ms-host"></span>
          <span class="spacer"></span>
          <button type="button" id="dfSave">儲存</button>
        </div>
        <p class="note">⚠ 只影響之後抓到的<b>新貼文</b>，不會回溯既有的
          ${(a.post_count || 0).toLocaleString()} 則。</p>
        <p class="note" id="dfMsg"></p>
      </div>

      <div class="ovl-section">
        <h3>回溯既有貼文</h3>
        <p class="note">${a.post_count
          ? `把目前的預設值套用到這個帳號的 ${a.post_count.toLocaleString()} 則貼文`
            + '（不覆蓋人工標記過的）。'
          : '這個帳號目前<b>沒有任何貼文</b>，沒有東西可以重標。'}</p>
        <div class="row">
          <span class="spacer"></span>
          <button type="button" id="dfRetag" ${a.post_count ? '' : 'disabled'}>重標既有</button>
        </div>
        <p class="note" id="dfRetagMsg"></p>
      </div>

      <div class="ovl-section">
        <h3>歸屬到創作者</h3>
        <p class="note">創作者用來把同一位作者的跨平台帳號與小帳串在一起。</p>
        <div class="row">
          <span id="dfCreator" class="ms-host"></span>
          <span id="dfRole" class="ms-host"></span>
          <button type="button" id="dfLink">套用</button>
        </div>
        <div class="row">
          <input id="dfNewCreator" placeholder="新創作者名稱">
          <button type="button" class="ghost" id="dfAddCreator">+ 新建</button>
          <span class="muted">目前有 ${state.creators.length} 位創作者</span>
        </div>
        <p class="note" id="dfLinkMsg"></p>
      </div>

      <div class="ovl-section danger-zone">
        <h3>危險區</h3>
        <p class="note">刪除這個帳號的全部記錄。<b>本機檔案不會被刪除。</b></p>
        <div class="row">
          <span class="spacer"></span>
          <button type="button" class="danger" id="dfDelete">刪除記錄</button>
        </div>
        <p class="note" id="dfDelMsg"></p>
      </div>`,
    onMount: (body, handle) => {
      // 抽屜每次打開都是新的一段 HTML —— 下拉要在這裡建，不是開頁時建一次。
      const d = mountDrops(body, {
        dfRating: {
          label: '分級（未標）', emptyText: '（未標）', ariaLabel: '新貼文的預設分級',
          values: RATING_VALUES.map((v) => ({ value: v })),
          value: a.default_rating || '', onChange: () => {},
        },
        dfContent: {
          label: '類型（未標）', emptyText: '（未標）', ariaLabel: '新貼文的預設類型',
          values: CONTENT_VALUES.map((v) => ({ value: v })),
          value: a.default_content_type || '', onChange: () => {},
        },
        dfCreator: {
          label: '（未歸屬）', emptyText: '（未歸屬）', ariaLabel: '歸屬到哪位創作者',
          values: creatorOpts,
          value: a.creator_id ? String(a.creator_id) : '', onChange: () => {},
        },
        dfRole: {
          label: '（無角色）', emptyText: '（無角色）', ariaLabel: '在該創作者底下的角色',
          values: [{ value: 'main' }, { value: 'alt' }, { value: 'r18_alt' }],
          value: a.role || '', onChange: () => {},
        },
      });
      const note = (id, text, cls = '') => {
        const el = body.querySelector(id);
        el.textContent = text;
        el.className = `note ${cls}`;
      };

      body.querySelector('#dfSave').addEventListener('click', async () => {
        try {
          await api(`/api/accounts/${a.id}/defaults`, {
            method: 'PATCH',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({
              default_rating: d.dfRating.get() || null,
              default_content_type: d.dfContent.get() || null,
            }),
          });
          a.default_rating = d.dfRating.get() || null;
          a.default_content_type = d.dfContent.get() || null;
          // 「送出」與「生效」不共用提示：這裡明說沒有回溯，因為那正是
          // 使用者最容易誤會的地方。
          note('#dfMsg', `已儲存。既有的 ${(a.post_count || 0).toLocaleString()} 則貼文`
            + '不受影響 —— 要回溯請按下面的「重標既有」。', 'good');
          repaintCard(a, card);
        } catch (e) { note('#dfMsg', `失敗：${e.message}`, 'bad'); }
      });

      body.querySelector('#dfRetag').addEventListener('click', async (ev) => {
        const ok = await confirmDialog({
          title: '重標既有貼文？',
          lines: [
            `把「${acctName(a)}」目前的預設值套用到既有貼文。`,
            '',
            `· 會檢查 ${(a.post_count || 0).toLocaleString()} 則貼文`,
            '· 人工標記過的不會被覆蓋',
            '',
            '這個動作會改動資料庫，但不影響磁碟上的檔案。',
          ],
          confirmText: '重標',
        });
        if (!ok) return;
        ev.target.disabled = true;
        note('#dfRetagMsg', '重標中…');
        try {
          const r = await api(`/api/accounts/${a.id}/retag`, {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ overwrite_manual: false }),
          });
          note('#dfRetagMsg', `已重標 ${r.updated} 則（人工標記未被覆蓋）`, 'good');
        } catch (e) {
          note('#dfRetagMsg', `失敗：${e.message}`, 'bad');
        } finally {
          ev.target.disabled = false;
        }
      });

      body.querySelector('#dfAddCreator').addEventListener('click', async () => {
        const name = body.querySelector('#dfNewCreator').value.trim();
        if (!name) { note('#dfLinkMsg', '請先輸入名稱', 'bad'); return; }
        try {
          const c = await api('/api/creators', {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ display_name: name }),
          });
          await loadCreatorList();
          // setOptions 會自己把「（未歸屬）」那一項補在最前面
          d.dfCreator.setOptions(state.creators.map((x) =>
            ({ value: String(x.id), text: x.display_name })));
          d.dfCreator.set(String(c.id));
          body.querySelector('#dfNewCreator').value = '';
          note('#dfLinkMsg', `已建立「${name}」—— 還要按「套用」才會掛上去`, 'good');
        } catch (e) { note('#dfLinkMsg', `建立失敗：${e.message}`, 'bad'); }
      });

      body.querySelector('#dfLink').addEventListener('click', async () => {
        const cid = d.dfCreator.get();
        try {
          if (!cid) {
            await api(`/api/accounts/${a.id}/link`, { method: 'DELETE' });
            a.creator_id = null;
            a.role = null;
            note('#dfLinkMsg', '已解除歸屬', 'good');
          } else {
            await api(`/api/accounts/${a.id}/link`, {
              method: 'POST',
              headers: { 'content-type': 'application/json' },
              body: JSON.stringify({
                creator_id: Number(cid),
                role: d.dfRole.get() || null,
              }),
            });
            a.creator_id = Number(cid);
            a.role = d.dfRole.get() || null;
            note('#dfLinkMsg', '已歸屬', 'good');
          }
          await loadCreatorList();
        } catch (e) { note('#dfLinkMsg', `失敗：${e.message}`, 'bad'); }
      });

      body.querySelector('#dfDelete').addEventListener('click', async () => {
        let p;
        try {
          // 先問「會刪掉什麼」再讓使用者決定 —— 不做「按一下就刪」。
          p = await api(`/api/accounts/${a.id}/deletion-preview`);
        } catch (e) {
          note('#dfDelMsg', `取不到預演：${e.message}`, 'bad');
          return;
        }
        // ⚠️ 這段文字是**產品層摩擦**，一字不減。2.0 唯一的改動是把
        // window.confirm()（會擋住整個分頁、樣式不一致）換成自製 dialog。
        const ok = await confirmDialog({
          title: `刪除「${p.screen_name}」（${p.platform}）的全部記錄？`,
          lines: [
            `· ${p.posts} 則貼文`,
            `· ${p.media} 筆媒體記錄`,
            '',
            '本機的媒體檔案不會被刪除。',
            ...p.warnings.map((w) => `⚠️ ${w}`),
            '',
            '這個動作無法復原。',
          ],
          confirmText: '確定刪除',
          danger: true,
        });
        if (!ok) return;
        try {
          const r = await api(`/api/accounts/${a.id}?confirm=true`, { method: 'DELETE' });
          handle.close();
          say(card, `已刪除 ${r.posts} 則貼文 / ${r.media} 筆媒體記錄，`
            + `${r.downloaded_files_kept} 個檔案留在磁碟上`, 'ok');
          loadAccounts();
        } catch (e) { note('#dfDelMsg', `失敗：${e.message}`, 'bad'); }
      });
    },
  });
}

/** 抽屜裡改過的東西要反映到卡面上，但**不重載整份清單** ——
 *  重載會讓使用者剛才捲到的位置整個跳掉。 */
function repaintCard(a, card) {
  const foot = card.querySelector('.card-foot span');
  const defaults = [a.default_rating, a.default_content_type].filter(Boolean).join(' · ');
  if (foot) foot.textContent = `預設 ${defaults || '（未設定）'}`;
}

// ── 篩選與分頁 ─────────────────────────────────────────

// 搜尋做 debounce：不 debounce 的話打「heikala」是 7 個請求，
// 而且回應順序沒有保證 —— 慢的那個後到就會蓋掉正確結果。
let accountSearchTimer = null;
$('aSearch').addEventListener('input', () => {
  clearTimeout(accountSearchTimer);
  // 換了條件就回第一頁 —— 留在第 20 頁再篩選，多半會看到空白而以為壞了
  state.acctOffset = 0;
  accountSearchTimer = setTimeout(loadAccounts, 250);
});

// ⚠️ 這裡只剩 `aFavOnly` —— 它是**真的** checkbox，change 事件真的會發生。
// 其餘全部改成自製下拉，變動由 `wireAccountFilters()` 裡的 onChange 處理。
//
// 這一段有兩次前科，症狀不一樣但都很惡劣：
//   · id 改名 → `$()` 回 null → 模組頂層 TypeError → 整個 accounts.js 掛掉，
//     main.js 的 init 中斷，畫面「篩選器全變純文字、格線空白」而 console 沒紅字
//   · `<select>` 換成 `<span>` → `$()` 回的是元素**不是 null**，
//     addEventListener 掛得上、不報錯、**永遠不觸發** —— 換了篩選沒反應
$('aFavOnly').addEventListener('change', () => {
  clearAcctSelection('換了篩選 —— 選取已清空');
  state.acctOffset = 0;
  loadAccounts();
});

$('aPrev').addEventListener('click', () => {
  clearAcctSelection('換頁了 —— 選取已清空（不做跨頁記憶：看不見的選取比清掉更危險）');
  state.acctOffset = Math.max(0, state.acctOffset - ACCT_PAGE);
  loadAccounts();
});
$('aNext').addEventListener('click', () => {
  clearAcctSelection('換頁了 —— 選取已清空（不做跨頁記憶：看不見的選取比清掉更危險）');
  state.acctOffset += ACCT_PAGE;
  loadAccounts();
});

// 「擷取結果」這個篩選在正式庫上 100% 篩不出東西（`last_fetch_status`
// 4,653 筆全 NULL）。⚠️ 不給一個空下拉就算了 —— 那是「假預設用途」。
// 展開時才去問，問到 0 就把它 disable 並寫出原因。
let fetchNoteLoaded = false;
$('aMore').addEventListener('toggle', async () => {
  if (!$('aMore').open || fetchNoteLoaded) return;
  fetchNoteLoaded = true;
  const all = ['ok', 'no_new', ...FETCH_BAD, 'skipped'].join(',');
  try {
    const res = await fetch(`/api/accounts?fetch_status=${all}&limit=1&with_stats=false`);
    if (!res.ok) return;
    const n = Number(res.headers.get('X-Total-Count') || 0);
    if (!n) {
      $('aFetchStatus').disabled = true;
      $('aFetchStatus').dataset.tip = '尚未執行過任何抓取';
      $('aFetchNote').textContent = '尚未執行過任何抓取 —— 這個篩選目前選不出東西';
    } else {
      $('aFetchNote').textContent = `${n.toLocaleString()} 個帳號有擷取記錄`;
    }
  } catch { /* 補充說明，拿不到就不寫 */ }
});

// ── 檢視切換：帳號／創作者 ────────────────────────────
//
// 兩者是**同一份資料的兩種分組方式**，不是兩個工作面 —— 所以用 radio，
// 不做成第四個 tab。放成 tab 會讓人以為它們是平行的功能。

document.querySelectorAll('input[name="aView"]').forEach((r) =>
  r.addEventListener('change', () => {
    state.acctMode = r.value;
    $('accountPane').classList.toggle('hidden', r.value !== 'accounts');
    $('creatorPane').classList.toggle('hidden', r.value !== 'creators');
    if (r.value === 'creators') loadCreators();
    else loadAccounts();
  }));

/** 只取資料（媒體頁的 creator 下拉與抽屜都要用），不畫創作者清單。 */
export async function loadCreatorList() {
  const list = await api('/api/creators');
  state.creators = list;
  paintMoreNotes();
  return list;
}

export async function loadCreators() {
  const list = await loadCreatorList();
  $('accountCount').innerHTML = `共 <b class="todo">${list.length}</b> 位創作者`;

  // 正式庫 creators = 0 —— 這是**目前唯一會看到的狀態**，所以它必須解釋
  // 「這東西是幹嘛的、怎麼開始」，而不只是說「沒有資料」。
  $('creatorList').innerHTML = list.length
    ? list.map((c) => `
      <div class="card">
        <h3>${esc(c.display_name)}</h3>
        <div class="card-id">${c.accounts.length} 個帳號</div>
        <div class="row">
          ${c.accounts.map((a) => `<span class="pill">${esc(a.platform)} @${
            esc(a.screen_name || '?')}${a.role ? ` · ${esc(a.role)}` : ''}</span>`).join('')
            || '<span class="muted">尚未掛任何帳號</span>'}
        </div>
        <div class="row">
          <span class="spacer"></span>
          <button type="button" class="ghost" data-creator="${c.id}"
                  data-label="${esc(c.display_name)}">看全部作品</button>
        </div>
      </div>`).join('')
    : `<p class="empty">還沒有任何創作者。<br>
        創作者用來把同一位作者的跨平台帳號與小帳串在一起。<br>
        到任一帳號的 <b>[編輯] → 歸屬到創作者</b>，就會建立第一位。</p>`;
}

$('creatorList').addEventListener('click', (ev) => {
  const btn = ev.target.closest('[data-creator]');
  if (btn) jumpToMedia({ creator: btn.dataset.creator, label: btn.dataset.label });
});

/** 平台下拉的選項。**帶筆數** —— 選了會不會是空的，要在選之前就看得出來。
 *
 *  只在第一次進帳號頁時載一次：平台清單不會在使用中變動（新平台要改程式）。 */
let platformsLoaded = false;

async function loadPlatforms() {
  if (platformsLoaded) return;
  try {
    const d = await api('/api/accounts/platforms');
    // setOptions() 自己保留目前值（不在新清單裡就退回「全部平台」）。
    aDrops.aPlatform.setOptions(d.items.map((it) => ({
      value: it.platform,
      text: `${it.platform}（${it.count.toLocaleString()}）`,
    })));
    platformsLoaded = true;
  } catch { /* 補充選項，拿不到就維持「全部平台」，不必報錯 */ }
}

/** 進入帳號頁時呼叫（nav 的 registry）。 */
export function loadAccountsView() {
  loadPlatforms();
  return loadAccounts();
}

// ── 選取與批次 ─────────────────────────────────────────
//
// ⚠️ 這一整段最危險的一題是「使用者現在選的是哪些」。
// 4,653 筆的清單上，「全選」是**兩個不同的動作**：本頁 100、或篩選後的全部。
// 合成一顆按鈕是批次功能最經典的災難 —— 而批次不可逆。
//
// ⚠️ 第二個硬約束：**SQLite 一次繫結變數上限 999**。4,653 個 id 一次送出
// 會直接 OperationalError，所以要分批 —— 而**分批由前端做**，因為只有它
// 知道要顯示「第 2 / 5 批」。後端默默切的話，那段等待時間裡畫面完全靜止，
// 看起來像當掉。

let acctSelecting = false;
const acctPicked = new Set();
// 'page' = 只有畫面上勾的那些；'all' = 篩選後的全部（含看不到的）。
// 這個值必須寫在畫面上 —— 使用者不會記得自己按過哪顆按鈕。
let acctPickScope = 'page';
// 'all' 模式下的 id 全集。由 /api/accounts/ids 取得。
let acctAllIds = [];
let acctAllIdSet = new Set();
let acctBulkBusy = false;

const BULK_ID_LIMIT = 900;   // 與後端 api/prefs.py 的同名常數一致

/** 批次可改的欄位。`clear` 為 true 的那些多一個「（清除）」選項。 */
const BULK_FIELDS = [
  { key: 'is_ignored', label: '忽略', opts: [
    ['true', '設為忽略'], ['false', '取消忽略']] },
  { key: 'is_tracked', label: '追蹤', opts: [
    ['true', '恢復追蹤'], ['false', '停止追蹤']] },
  { key: 'default_rating', label: '預設分級', clear: true, opts: [
    ['sfw', 'sfw'], ['r18', 'r18']] },
  { key: 'default_content_type', label: '預設類型', clear: true,
    opts: CONTENT_VALUES.map((v) => [v, v]) },
  { key: 'is_favorite', label: '我的最愛', opts: [
    ['true', '加入 ♥'], ['false', '移出 ♥']] },
  { key: 'stars', label: '評分', clear: true, opts: [
    ['5', '★★★★★'], ['4', '★★★★'], ['3', '★★★'], ['2', '★★'], ['1', '★']] },
];

function pickedCount() {
  return acctPickScope === 'all' ? acctAllIds.length : acctPicked.size;
}

function pickedIds() {
  return acctPickScope === 'all' ? acctAllIds.slice() : [...acctPicked];
}

function isPicked(id) {
  return acctPickScope === 'all' ? acctAllIdSet.has(id) : acctPicked.has(id);
}

/** 批次列選了什麼。**值存在這裡，不存在 DOM 上。**
 *
 *  ⚠️ `renderSelBar()` 每次都是整段 `innerHTML` 換掉 —— 值如果只活在
 *  控制項上，任何一次重畫都會把它清掉。原本就有這個 bug：選好欄位再按
 *  「全選本頁」，那排下拉會靜默跳回「—」，而使用者以為還選著。
 *  值搬到這裡之後重畫不再有副作用。 */
const bulkValues = {};

function currentBulkFields() {
  const fields = {};
  for (const [k, v] of Object.entries(bulkValues)) {
    if (v) fields[k] = v;
  }
  return fields;
}

/** 套用完 / 離開選取模式時清掉，否則下一批會沿用上一批的欄位。 */
function clearBulkFields() {
  for (const k of Object.keys(bulkValues)) delete bulkValues[k];
}

function renderSelBar() {
  const bar = $('aSelBar');
  bar.classList.toggle('hidden', !acctSelecting);
  $('aSelectMode').textContent = acctSelecting ? '離開選取' : '選取';
  if (!acctSelecting) return;

  const pageN = state.accounts.length;
  const total = state.acctTotal;
  const n = pickedCount();
  const scope = acctPickScope === 'all'
    ? '（<b>全部符合篩選的</b>，不只這一頁）'
    : '（本頁）';

  // ⚠️ 兩顆按鈕、兩個數字。第二顆在 total ≤ pageN 時**不出現** ——
  // 那時它與第一顆同義，兩顆一樣的按鈕只會製造疑惑。
  const buttons = acctPickScope === 'all'
    ? `<button type="button" class="ghost" data-sel="page">改成只選本頁 ${pageN}</button>`
    : `<button type="button" class="ghost" data-sel="page">全選本頁 ${pageN}</button>`
      + (total > pageN
        ? `<button type="button" class="ghost" data-sel="all">選取全部符合篩選的 ${
            total.toLocaleString()}</button>`
        : '');

  const warn = acctPickScope === 'all' && total > pageN
    ? `<div class="sel-warn">這超出目前這一頁 —— 套用會改到你現在<b>看不到</b>的 ${
        (total - pageN).toLocaleString()} 個帳號。</div>`
    : '';

  const fields = BULK_FIELDS.map((f) =>
    `<label class="chk">${f.label}<span data-bulk="${f.key}" class="ms-host"></span></label>`
  ).join('');

  bar.innerHTML = `
    <div class="sel-row">
      <span class="sel-count">已選 <b>${n.toLocaleString()}</b> 個 ${scope}</span>
      ${buttons}
      <button type="button" class="ghost" data-sel="none">清除選取</button>
    </div>
    ${warn}
    ${bar.dataset.note ? `<div class="sel-warn">${esc(bar.dataset.note)}</div>` : ''}
    <div class="sel-row">
      ${fields}
      <span class="spacer"></span>
      <span class="muted">批次一律「選了再按套用」—— 與卡上 ♥★ 的立即生效不同</span>
      <button type="button" id="aBulkApply"${n ? '' : ' disabled'}
        data-tip="${n ? '' : '先選帳號'}">套用</button>
    </div>`;

  // innerHTML 換完才有佔位元素可以掛。值從 bulkValues 回填 —— 重畫不清空。
  mountDrops(bar, Object.fromEntries(BULK_FIELDS.map((f) => [
    `[data-bulk="${f.key}"]`,
    {
      label: '—',
      emptyText: '—',
      ariaLabel: `批次${f.label}`,
      values: f.opts.map(([v, t]) => ({ value: v, text: t }))
        .concat(f.clear ? [{ value: '__clear__', text: '（清除）' }] : []),
      value: bulkValues[f.key] || '',
      onChange: (v) => { bulkValues[f.key] = v; },
    },
  ])));
}

/** 套用前的預演。**不可逆的動作要先講後果**，而且講的是使用者關心的後果
 *  （「我之後還抓得到東西嗎」「我的檔案還在嗎」），不是筆數。 */
function renderBulkPreview(fields) {
  const box = $('aBulkBox');
  if (!fields) { box.innerHTML = ''; return; }
  const n = pickedCount();
  const LABEL = {
    is_ignored: (v) => (v === 'true' ? '設為「忽略」' : '取消「忽略」'),
    is_tracked: (v) => (v === 'true' ? '恢復追蹤' : '停止追蹤'),
    is_favorite: (v) => (v === 'true' ? '加入我的最愛' : '移出我的最愛'),
    default_rating: (v) => (v === '__clear__' ? '清除預設分級' : `預設分級設為 ${v}`),
    default_content_type: (v) => (v === '__clear__' ? '清除預設類型' : `預設類型設為 ${v}`),
    stars: (v) => (v === '__clear__' ? '清除評分' : `評分設為 ${v} 星`),
  };
  const lines = Object.entries(fields).map((e) => `<li>${esc(LABEL[e[0]](e[1]))}</li>`);
  if (fields.is_ignored === 'true') {
    lines.push('<li>這些帳號之後<b>不會</b>被一鍵更新抓 ——'
      + ' 抓取頁的「可抓 N 個」會跟著變少。</li>');
  }
  const batches = Math.ceil(n / BULK_ID_LIMIT);
  if (batches > 1) {
    lines.push(`<li>會分 <b>${batches}</b> 批寫入 ——`
      + ` SQLite 一次最多 ${BULK_ID_LIMIT} 個 id。</li>`);
  }
  box.innerHTML = `<div class="bulk-box">
    <h4>要改 ${n.toLocaleString()} 個帳號</h4>
    <ul>${lines.join('')}
      <li class="ok-line"><b>一則貼文、一個媒體都不會被刪或改。</b></li>
      <li>沒有復原鍵，但可以再批次一次改回來。</li>
    </ul>
    <div class="row">
      <button type="button" id="aBulkYes">確定，改 ${n.toLocaleString()} 個</button>
      <button type="button" id="aBulkNo" class="ghost">取消</button>
    </div>
    <div id="aBulkProgress" class="muted"></div>
  </div>`;
}

/** 分批送出。**序列，不併發** —— 同一張表的寫入併發沒有好處，
 *  而且併發之後「第 N/M 批」就沒有意義了。 */
async function runBulk(fields) {
  const ids = pickedIds();
  const batches = [];
  for (let i = 0; i < ids.length; i += BULK_ID_LIMIT) {
    batches.push(ids.slice(i, i + BULK_ID_LIMIT));
  }
  const prog = $('aBulkProgress');
  let updated = 0;
  const missing = [];

  for (let b = 0; b < batches.length; b++) {
    // 只有一批時不講「第 1/1 批」—— 那是噪音
    prog.innerHTML = batches.length > 1
      ? `寫入中… 第 ${b + 1} / ${batches.length} 批（${batches[b].length} 個）`
      : '寫入中…';
    const body = { ids: batches[b] };
    for (const [k, v] of Object.entries(fields)) {
      if (v === '__clear__') body[k] = '__clear__';
      else if (k === 'stars') body[k] = Number(v);
      else if (k.indexOf('is_') === 0) body[k] = v === 'true';
      else body[k] = v;
    }
    try {
      const r = await api('/api/accounts/bulk-prefs', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body),
      });
      updated += r.updated;
      missing.push(...(r.missing || []));
    } catch (e) {
      // ⚠️ **不回滾前面幾批**（跨批交易的代價不值），但一定要說清楚
      // 哪些已經生效 —— 讓使用者以為全部沒改，他會再按一次。
      prog.innerHTML = `<span class="err">第 ${b + 1} 批失敗：${esc(e.message)}</span><br>`
        + (b > 0
          ? `<b>前 ${b} 批（${updated.toLocaleString()} 個）已經生效</b>，沒有回滾。`
          : '一筆都還沒改。');
      return;
    }
  }

  // ③ 結果。`missing` 一定要講 —— 只說「改好 4,650 個」而使用者選了 4,653，
  // 那 3 個去哪了沒人講得出來。
  prog.innerHTML = `<span class="ok-line">改好 ${updated.toLocaleString()} 個。</span>`
    + (missing.length
      ? `<br><span class="err">${missing.length} 個沒改到</span>`
        + ` —— 這幾筆已經不存在了（期間被刪）：${
          esc(missing.slice(0, 20).join('、'))}${missing.length > 20 ? ' …' : ''}`
      : '')
    + (fields.is_ignored ? '<br>抓取頁的「可抓 N 個」已經跟著變了。' : '');
}

function exitSelect() {
  acctSelecting = false;
  clearBulkFields();
  acctPicked.clear();
  acctPickScope = 'page';
  acctAllIds = [];
  acctAllIdSet = new Set();
  renderBulkPreview(null);
  renderSelBar();
  loadAccountsView();
}

/** 換頁或改篩選時清空選取，**而且要講出來**。
 *  留著跨頁選取但畫面上看不到它們，比清掉更危險。 */
export function clearAcctSelection(reason) {
  if (!acctPicked.size && acctPickScope !== 'all') return;
  acctPicked.clear();
  acctPickScope = 'page';
  acctAllIds = [];
  acctAllIdSet = new Set();
  renderBulkPreview(null);
  if (acctSelecting && reason) $('aSelBar').dataset.note = reason;
}

/** 只更新卡片的選取外觀，不重新請求。 */
function paintPickedCards() {
  for (const card of document.querySelectorAll('#accountList .card')) {
    const on = isPicked(Number(card.dataset.id));
    card.classList.toggle('picked', on);
    const box = card.querySelector('.acct-pick');
    if (box) box.checked = on;
  }
}

$('aSelectMode').addEventListener('click', () => {
  if (acctSelecting) { exitSelect(); return; }
  // 創作者檢視沒有帳號 id 可以批次 —— 進不去，而且要說得出原因
  if (state.acctMode !== 'accounts') {
    $('aSelectMode').dataset.tip = '創作者不支援批次，切回「帳號」檢視';
    return;
  }
  acctSelecting = true;
  $('aSelBar').dataset.note = '';
  renderSelBar();
  loadAccountsView();
});

$('aSelBar').addEventListener('click', async (ev) => {
  const sel = ev.target.closest('[data-sel]');
  if (sel) {
    const what = sel.dataset.sel;
    if (what === 'none') {
      acctPicked.clear(); acctPickScope = 'page';
      acctAllIds = []; acctAllIdSet = new Set();
    } else if (what === 'page') {
      acctPickScope = 'page';
      acctPicked.clear();
      for (const a of state.accounts) acctPicked.add(a.id);
    } else if (what === 'all') {
      // 只取 id，不取卡片資料 —— 4,653 張卡的 payload 含預覽縮圖陣列，
      // 為了一組 id 付那個成本不划算。
      sel.disabled = true;
      try {
        const p = new URLSearchParams(accountQuery());
        // 這三個與「篩選」無關，帶過去只會讓後端多解析
        p.delete('sort'); p.delete('limit'); p.delete('offset');
        const r = await api(`/api/accounts/ids?${p}`);
        acctAllIds = r.ids;
        acctAllIdSet = new Set(r.ids);
        acctPickScope = 'all';
      } catch (e) {
        sel.textContent = `取不到 id：${e.message}`;
        sel.disabled = false;
        return;
      }
      sel.disabled = false;
    }
    $('aSelBar').dataset.note = '';
    renderBulkPreview(null);
    renderSelBar();
    paintPickedCards();
    return;
  }

  if (ev.target.closest('#aBulkApply')) {
    const fields = currentBulkFields();
    if (!Object.keys(fields).length) {
      // disabled 的兩種理由要分開：沒選帳號 vs 沒選欄位
      $('aSelBar').dataset.note = '先選要改什麼（上面那排下拉還都是「—」）';
      renderBulkPreview(null);
      renderSelBar();
      return;
    }
    renderBulkPreview(fields);
  }
});

$('aBulkBox').addEventListener('click', async (ev) => {
  if (ev.target.closest('#aBulkNo')) { renderBulkPreview(null); return; }
  if (!ev.target.closest('#aBulkYes') || acctBulkBusy) return;
  acctBulkBusy = true;
  const fields = currentBulkFields();
  $('aBulkYes').disabled = true;
  $('aBulkNo').disabled = true;
  try {
    await runBulk(fields);
    // 重新載入才看得到改完的樣子（忽略標記、♥、★ 都在卡上）
    await loadAccountsView();
  } finally {
    acctBulkBusy = false;
    const no = $('aBulkNo');
    if (no) { no.disabled = false; no.textContent = '關閉'; }
  }
});
