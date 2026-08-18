// 帳號清單、編輯抽屜、創作者檢視。
//
// 設計依據（wiki 的 UI_帳號管理）：
//   · 4,653 筆 —— **搜尋是入口，清單不是**
//   · 卡上只留高頻（♥ ★ 看媒體），低頻與破壞性的全部收進 [編輯] 抽屜
//   · 三個日期欄位有**兩個的資訊量是零**（`last_ingest_at` 4,648/4,653 同一天、
//     `last_fetched_at` 全空），所以卡面改成一行**結論**而不是三行原始資料

import { $, esc, fmtWhen, starsHtml, handleStarClick } from '../dom.js';
import { api } from '../api.js';
import { state } from '../state.js';
import { openOverlay, confirmDialog } from '../overlay.js';
import { jumpToMedia, paintMoreNotes } from './media.js';
import { RATINGS, CONTENTS, opts } from '../enums.js';

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
  p.set('sort', $('aSort').value);
  p.set('limit', ACCT_PAGE);
  p.set('offset', state.acctOffset);
  const q = $('aSearch').value.trim();
  if (q) p.set('q', q);
  if ($('aPlatform').value) p.set('platform', $('aPlatform').value);
  if ($('aFavOnly').checked) p.set('favorite', 'true');
  if ($('aMinStars').value) p.set('min_stars', $('aMinStars').value);
  // `__unset__` 直接原樣送 —— 空字串在 query string 裡與「不篩選」分不出來
  if ($('aDefaultRating').value) p.set('default_rating', $('aDefaultRating').value);
  if ($('aDefaultContent').value) p.set('default_content_type', $('aDefaultContent').value);
  const fs = $('aFetchStatus').value;
  // `__bad__` 展開成後端認得的多值。**不可以在前端濾** —— 前端只看得到
  // 當頁的 100 筆，使用者會在一頁全是「從沒檢查過」的清單上看到 0 筆，
  // 然後以為沒有任何帳號有問題。實測就是這樣錯的。
  if (fs === '__bad__') p.set('fetch_status', FETCH_BAD.join(','));
  else if (fs) p.set('fetch_status', fs);
  return p.toString();
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
    // ⚠️ 是形狀載體，不可以只靠紅字（灰階列印要仍分得出來）
    const why = a.last_fetch_note || FETCH_LABEL[st] || st;
    return { text: `⚠ 上次失敗：${why}`, bad: true, full: a.last_fetched_at };
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
  return `<span class="card-verdict bad">⚠ 已自動移出追蹤（連續 ${
    a.not_found_streak} 次找不到）—— 既有資料一筆都沒動
    <button type="button" class="linkish" data-act="retrack">恢復追蹤</button></span>`;
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
  return `<div class="card" data-id="${a.id}">
    <div class="card-head">
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
}

function emptyAccountsHtml() {
  const q = $('aSearch').value.trim();
  if (q) {
    return `<p class="empty">找不到符合「${esc(q)}」的帳號。<br>
      <button type="button" class="ghost" data-act="clearsearch">清除搜尋</button></p>`;
  }
  const conds = [
    $('aPlatform').value ? `平台 ${$('aPlatform').value}` : '',
    $('aFavOnly').checked ? '只看 ♥' : '',
    $('aMinStars').value ? `評分 ${$('aMinStars').value} 星以上` : '',
    $('aDefaultRating').value ? `預設分級 ${$('aDefaultRating').value}` : '',
    $('aDefaultContent').value ? `預設類型 ${$('aDefaultContent').value}` : '',
    $('aFetchStatus').value ? `擷取結果 ${$('aFetchStatus').value}` : '',
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
  if ($('aDefaultRating').value) { unsetRating = null; paintAccountCount(); return; }
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
  const creatorOpts = ['<option value="">（未歸屬）</option>']
    .concat(state.creators.map((c) =>
      `<option value="${c.id}">${esc(c.display_name)}</option>`)).join('');

  openOverlay({
    kind: 'drawer',
    title: acctName(a),
    subtitle: `${a.platform} · id ${a.platform_user_id}`,
    body: `
      <div class="ovl-section">
        <h3>新貼文的預設值</h3>
        <div class="row">
          <select id="dfRating">${opts(RATINGS, a.default_rating)}</select>
          <select id="dfContent">${opts(CONTENTS, a.default_content_type)}</select>
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
          <select id="dfCreator">${creatorOpts}</select>
          <select id="dfRole">
            <option value="">（無角色）</option>
            <option value="main">main</option>
            <option value="alt">alt</option>
            <option value="r18_alt">r18_alt</option>
          </select>
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
      if (a.creator_id) body.querySelector('#dfCreator').value = String(a.creator_id);
      if (a.role) body.querySelector('#dfRole').value = a.role;
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
              default_rating: body.querySelector('#dfRating').value || null,
              default_content_type: body.querySelector('#dfContent').value || null,
            }),
          });
          a.default_rating = body.querySelector('#dfRating').value || null;
          a.default_content_type = body.querySelector('#dfContent').value || null;
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
          const sel = body.querySelector('#dfCreator');
          sel.innerHTML = ['<option value="">（未歸屬）</option>']
            .concat(state.creators.map((x) =>
              `<option value="${x.id}">${esc(x.display_name)}</option>`)).join('');
          sel.value = String(c.id);
          body.querySelector('#dfNewCreator').value = '';
          note('#dfLinkMsg', `已建立「${name}」—— 還要按「套用」才會掛上去`, 'good');
        } catch (e) { note('#dfLinkMsg', `建立失敗：${e.message}`, 'bad'); }
      });

      body.querySelector('#dfLink').addEventListener('click', async () => {
        const cid = body.querySelector('#dfCreator').value;
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
                role: body.querySelector('#dfRole').value || null,
              }),
            });
            a.creator_id = Number(cid);
            a.role = body.querySelector('#dfRole').value || null;
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

['aSort', 'aFavOnly', 'aMinStars', 'aFetchStatus',
 'aDefaultRating', 'aDefaultContent', 'aPlatform'].forEach((id) =>
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
  $('fCreator').innerHTML = '<option value="">全部 creator</option>'
    + list.map((c) => `<option value="${c.id}">${esc(c.display_name)}</option>`).join('');
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
    const cur = $('aPlatform').value;
    $('aPlatform').innerHTML = '<option value="">全部平台</option>'
      + d.items.map((it) =>
        `<option value="${esc(it.platform)}">${esc(it.platform)}（${
          it.count.toLocaleString()}）</option>`).join('');
    $('aPlatform').value = cur;
    platformsLoaded = true;
  } catch { /* 補充選項，拿不到就維持「全部平台」，不必報錯 */ }
}

/** 進入帳號頁時呼叫（nav 的 registry）。 */
export function loadAccountsView() {
  loadPlatforms();
  return loadAccounts();
}
