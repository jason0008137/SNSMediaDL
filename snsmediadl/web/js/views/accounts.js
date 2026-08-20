// 帳號清單、編輯抽屜、創作者檢視。
//
// 設計依據（wiki 的 UI_帳號管理）：
//   · 4,653 筆 —— **搜尋是入口，清單不是**
//   · 卡上只留高頻（♥ ★ 看媒體），低頻與破壞性的全部收進 [編輯] 抽屜
//   · 三個日期欄位有**兩個的資訊量是零**（`last_ingest_at` 4,648/4,653 同一天、
//     `last_fetched_at` 全空），所以卡面改成一行**結論**而不是三行原始資料

import {
  $, esc, hint, mountDrops, multiDrop, setDetailsOff, setFieldOff,
  singleDrop, starsHtml, handleStarClick,
} from '../dom.js';
import { fmt, t, tn } from '../i18n.js';
import { api, toApiError } from '../api.js';
import { makeChipBar } from '../chips.js';
import { makeSortControl } from '../sortctl.js';
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

/** 擷取結果的顯示字。
 *
 *  ⚠️ **值是 key 不是文字** —— 這張表在模組載入時就建好，那時 i18n 還沒載完。
 *  查表拿到 key，要用的時候才 `t()`。存文字會整批變成 `⟦key⟧`。
 *
 *  ⚠️ 與 `fetch.js` 的 `cat.*` 是**同一個值域、不同的話**：那邊講的是
 *  「這一批抓取失敗的分類」，這邊講的是「這個帳號上次檢查的結果」。
 *  所以這裡的 `not_found` 多一句「可能改名」—— 帳號頁上那正是下一步。 */
const FETCH_LABEL = {
  ok: 'accounts.fetch.ok', no_new: 'accounts.fetch.no_new',
  not_found: 'accounts.fetch.not_found', rate_limited: 'accounts.fetch.rate_limited',
  auth_required: 'accounts.fetch.auth_required', failed: 'accounts.fetch.failed',
  skipped: 'accounts.fetch.skipped',
};

function accountQuery() {
  const p = new URLSearchParams();
  // ⚠️ `order` 從來沒被送過（後端 query.py 早就收了）—— 排序方向以前是烘在
  // 選項文字裡的，十個鍵有七個的方向根本改不了。
  p.set('sort', sortCtl.key());
  p.set('order', sortCtl.order());
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

/** 排序鍵的值域。**同時是白名單**（見 sortctl.js 的 `restore()`）。
 *  順序與後端 `query.py` 的 `sorts` 一致。 */
const A_SORT_KEYS = [
  'favorite', 'stars', 'name', 'last_post', 'last_ingest',
  'last_fetch', 'media', 'posts', 'created', 'id',
];

/** 顯示字。⚠️ **不含方向** —— 方向是隔壁那顆獨立的按鈕。
 *  舊的「評分高到低」「最久沒檢查」「我的最愛 → 評分」把方向烘進文字裡，
 *  結果是十個鍵有七個的方向改不了，而那三個看起來可改的其實也只有一種。 */
// ⚠️ 值是 key。makeSortControl 拿到之後才 t()（見它的 `restore()`）。
const A_SORT_TEXT = {
  favorite: 'accounts.sort.favorite', stars: 'accounts.sort.stars',
  name: 'accounts.sort.name', last_post: 'accounts.sort.last_post',
  last_ingest: 'accounts.sort.last_ingest', last_fetch: 'accounts.sort.last_fetch',
  media: 'accounts.sort.media', posts: 'accounts.sort.posts',
  created: 'accounts.sort.created', id: 'accounts.sort.id',
};

/** 每個鍵的預設方向。與後端 `sorts` 表的 `default_desc` 逐項對齊 ——
 *  對不齊的症狀是首次選到某個鍵時箭頭與實際順序相反。 */
const A_DEFAULT_ORDER = {
  favorite: 'desc', stars: 'desc', name: 'asc', last_post: 'desc',
  last_ingest: 'desc', last_fetch: 'asc', media: 'desc', posts: 'desc',
  created: 'desc', id: 'asc',
};

/** 方向鈕的語彙（句子由 sortctl.js 組）。 */
const A_DIR_WORDS = {
  favorite: { desc: 'accounts.dir.favfirst', asc: 'accounts.dir.favlast' },
  stars: { desc: 'dir.highlow', asc: 'dir.lowhigh' },
  name: { desc: 'dir.za', asc: 'dir.az' },
  last_post: { desc: 'dir.newold', asc: 'dir.oldnew' },
  last_ingest: { desc: 'dir.newold', asc: 'dir.oldnew' },
  last_fetch: { desc: 'accounts.dir.checkedrecent', asc: 'accounts.dir.checkedstale' },
  media: { desc: 'dir.manyfew', asc: 'dir.fewmany' },
  posts: { desc: 'dir.manyfew', asc: 'dir.fewmany' },
  created: { desc: 'dir.newold', asc: 'dir.oldnew' },
  id: { desc: 'dir.newold', asc: 'dir.oldnew' },
};

/** 註記。空字串 = 沒有要講的，但那一行仍然佔位。
 *
 *  ⚠️ `last_fetch` 是唯一**兩個方向要講不同話**的鍵：後端對它刻意反轉
 *  NULL 規則（`query.py` 的 `if sort == "last_fetch"`）—— 升冪時「從沒檢查過」
 *  排最前而不是沉底，因為「從沒查過 = 最該查」。方向鈕一按那批就從最上面
 *  跳到最下面，沒有這句話的話看起來就是 bug。 */
const A_SORT_NOTE = {
  favorite: 'accounts.sortnote.favorite',
  stars: 'accounts.sortnote.stars',
  last_post: 'accounts.sortnote.norecord',
  last_ingest: 'accounts.sortnote.norecord',
  last_fetch: {
    asc: 'accounts.sortnote.last_fetch.asc',
    desc: 'accounts.sortnote.last_fetch.desc',
  },
};

/** 排序控制。鍵下拉 + 方向鈕 + 註記 + 存檔白名單全在 sortctl.js（媒體頁共用）。
 *
 *  ⚠️ localStorage 從**裸鍵**（`favorite`）改成 `key:order`。舊值不必特別處理 ——
 *  裸鍵會被解析成 key + 該鍵的預設方向，那正是舊行為。認不得的值退回預設，
 *  **絕不原樣送給後端**（原生 `<select>` 會自己吞掉錯誤，自製下拉不會）。
 *
 *  GUI 預設 favorite，而 API 預設是 id（= 舊行為，extension 靠它）。 */
const sortCtl = makeSortControl({
  keyHost: $('aSortKey'),
  dirBtn: $('aSortDir'),
  noteEl: $('aSortNote'),
  keys: A_SORT_KEYS,
  labels: A_SORT_TEXT,
  defaultOrder: A_DEFAULT_ORDER,
  dirWords: A_DIR_WORDS,
  notes: A_SORT_NOTE,
  storageKey: 'accountSort',
  defaultKey: 'favorite',
  onChange: () => {
    // 排序不改變「有哪些帳號符合條件」，所以它**不清掉選取**。其餘篩選都要。
    state.acctOffset = 0;
    loadAccounts();
  },
});

/** 「（未設定）」放在值清單的最前面：「哪些我還沒標」才是主要用例。
 *
 *  ⚠️ `__unset__` 是**送給後端的 sentinel**，不是給人看的字。顯示字在這裡
 *  定義一次，下拉與標籤列共用 —— 各寫一份的下場是標籤列上出現
 *  「預設類型 __unset__」（實測過，看起來像資料壞了）。 */
/** ⚠️ **是函式不是常數。** 常數會在模組載入時求值，那時 i18n 還沒載完 ——
 *  下拉與標籤列上會出現 `⟦accounts.unset⟧`。兩個呼叫端（`unsetFirst()` 與
 *  `acctConditions()`）都是在 i18n 就緒之後才跑。 */
const unsetText = () => t('accounts.unset');
const unsetFirst = (values) =>
  [{ value: '__unset__', text: unsetText() }, ...values.map((v) => ({ value: v }))];

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
        clearAcctSelection(t('accounts.sel.cleared.filter'));
        state.acctOffset = 0;
        loadAccounts();
      },
      ...opt,
    });
  };
  // 平台選項帶筆數，由 `loadPlatforms()` 之後用 setOptions() 補上。
  single('aPlatform', t('accounts.platform.all'), [], { value: '' });
  single('aDefaultRating', t('accounts.defrating.all'), unsetFirst(RATING_VALUES));
  single('aDefaultContent', t('accounts.defcontent.all'), unsetFirst(CONTENT_VALUES));

  const mk = (id, label, values, text) => {
    aDrops[id] = multiDrop($(id), {
      label,
      values: values.map((v) => ({ value: v, text: text ? text(v) : undefined })),
      onChange: () => {
        clearAcctSelection(t('accounts.sel.cleared.filter'));
        state.acctOffset = 0;
        loadAccounts();
      },
    });
  };
  mk('aStars', t('filter.stars'), A_STARS, (v) => '★'.repeat(Number(v)));
  mk('aIgnored', t('accounts.ignored'), ['true', 'false'],
     (v) => t(v === 'true' ? 'accounts.ignored.yes' : 'accounts.ignored.no'));
  mk('aFetchStatus', t('accounts.fetchstatus'), A_FETCH, (v) => t(FETCH_LABEL[v]));
  // 記住的排序偏好要在第一次 loadAccounts() 之前還原（main.js 保證了順序）。
  sortCtl.restore();
  // 開頁就跑一次：帳號模式下它什麼都不關，但 dom.js 的互動 guard 會在這時
  // 掛好，而不是等到第一次切到創作者才掛。
  applyModeGate();
}

// ── 生效條件標籤列 ─────────────────────────────────────
//
// 現況只有在**篩到 0 筆**時才把條件串成一句話。也就是說篩到 3,000 筆的時候
// 完全看不見自己選了什麼 —— 而那正是最容易誤判的情況（「怎麼比昨天少」）。
//
// ⚠️ 渲染與事件委派在 `chips.js`，與媒體頁**同一份**。上一次兩頁就是各寫
// 一份控制項才漂移成兩套不同的東西，再寫一份就是下一次漂移的起點。

/** 目前生效的條件。**清單與空狀態共用**（`emptyAccountsHtml()` 也讀它）——
 *  兩邊各串一次的結果是標籤列說七個條件、空狀態說五個。 */
function acctConditions() {
  const out = [];
  const q = $('aSearch').value.trim();
  if (q) out.push({ kind: 'search', label: t('chips.search'), value: q });
  const single = (id, label) => {
    const v = aDrops[id].get();
    if (v) out.push({ kind: 'single', id, label, value: v === '__unset__' ? unsetText() : v });
  };
  single('aPlatform', t('chips.platform'));
  single('aDefaultRating', t('chips.defrating'));
  single('aDefaultContent', t('chips.defcontent'));
  const stars = aDrops.aStars?.get() ?? [];
  // 同一組內是 OR。**「或」要看得見** —— 不寫出來的話使用者會以為勾兩個是
  // 「同時符合」，然後奇怪為什麼筆數變多了。
  if (stars.length) {
    out.push({ kind: 'multi', id: 'aStars', label: t('filter.stars'),
               value: stars.map((v) => '★'.repeat(Number(v))).join(t('chips.or')) });
  }
  if ($('aFavOnly').checked) out.push({ kind: 'fav', label: t('chips.only'), value: '♥' });
  const ig = aDrops.aIgnored?.get() ?? [];
  if (ig.length) {
    out.push({ kind: 'multi', id: 'aIgnored', label: t('accounts.ignored'),
               value: ig.map((v) =>
                 t(v === 'true' ? 'accounts.ignored.yes' : 'accounts.ignored.no'))
                 .join(t('chips.or')) });
  }
  const fs = aDrops.aFetchStatus?.get() ?? [];
  if (fs.length) {
    out.push({ kind: 'multi', id: 'aFetchStatus', label: t('accounts.fetchstatus'),
               value: fs.map((v) => (FETCH_LABEL[v] ? t(FETCH_LABEL[v]) : v))
                 .join(t('chips.or')) });
  }
  return out;
}

function clearAcctCondition(what) {
  if (what === '__all__') {
    $('aSearch').value = '';
    $('aFavOnly').checked = false;
    for (const id of ['aPlatform', 'aDefaultRating', 'aDefaultContent',
                      'aStars', 'aIgnored', 'aFetchStatus']) aDrops[id]?.clear();
  } else if (what === 'search') {
    $('aSearch').value = '';
  } else if (what === 'fav') {
    $('aFavOnly').checked = false;
  } else {
    // 標籤的 × 一次清掉**整個欄位**（不是其中一個值）——
    // 一顆 × 只清一個值的話，勾了四個擷取結果就得按四次。
    aDrops[what]?.clear();
  }
  // 排序不是篩選 —— 「全部清除」不該把使用者選的排序也一起打掉。
  clearAcctSelection(t('accounts.sel.cleared.filter'));
  state.acctOffset = 0;
  loadAccounts();
}

const acctChips = makeChipBar({
  host: $('aChipBar'),
  sources: acctConditions,
  onClear: clearAcctCondition,
});

const acctName = (a) => a.screen_name || a.platform_user_id;

/** #14 抓取狀態結論行 —— 這是 D2 的**答案**，不是原始資料。
 *
 *  卡面原本印三個日期，其中兩個在正式庫的鑑別力是零。刪掉欄位不對
 *  （使用者開始用抓取功能之後它們就有值了），正解是：沒有值的時候誠實說
 *  「還沒有」，而不是印一個 2026-08-14 讓人以為那代表什麼。 */
function verdict(a) {
  const st = a.last_fetch_status;
  if (!st && !a.last_fetched_at) {
    return { text: t('accounts.verdict.never'), bad: false };
  }
  // ⚠️ 只印月日的舊寫法（`when.slice(5)`）在換 locale 之後就不成立了 ——
  // 「08-14」在日期順序不同的語系裡讀起來是另一天。整條走 `fmt.date()`。
  const when = fmt.date(a.last_fetched_at);
  if (FETCH_BAD.includes(st)) {
    // 形狀載體（⚠ 前綴）由 app.css 的 `.bad::before` 統一加 —— 這裡**不要**
    // 自己再寫一個，否則畫面上會出現兩個 ⚠。
    // ⚠️ `last_fetch_note` 是後端寫的原文（目前是英文），沒有 key 可查 ——
    // 顯示原文比顯示 `⟦…⟧` 誠實，而且看得出來它沒被翻譯。
    const why = a.last_fetch_note || (FETCH_LABEL[st] ? t(FETCH_LABEL[st]) : st);
    return { text: t('accounts.verdict.lastfail', { why }), bad: true, full: a.last_fetched_at };
  }
  const days = a.last_fetched_at
    ? Math.floor((Date.now() - Date.parse(a.last_fetched_at)) / 86400000)
    : null;
  if (days != null && days > 30) {
    return { text: tn('accounts.verdict.stale', days, { when }),
             bad: false, full: a.last_fetched_at };
  }
  if (st === 'ok' && a.last_fetch_new_posts) {
    return { text: tn('accounts.verdict.got', a.last_fetch_new_posts, { when }),
             full: a.last_fetched_at };
  }
  if (st === 'skipped') {
    return { text: t('accounts.verdict.skipped', { when, why: a.last_fetch_note || '—' }),
             full: a.last_fetched_at };
  }
  return { text: t('accounts.verdict.nonew', { when }), full: a.last_fetched_at };
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
  return `<span class="card-verdict bad">${
    esc(t('accounts.untracked.head', { n: fmt.num(a.not_found_streak) }))}<br>${
    esc(t('accounts.untracked.safe'))}
    <button type="button" class="linkish" data-act="retrack">${
      esc(t('accounts.retrack'))}</button></span>`;
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
  // ⊘ 留在樣板側 —— 語系檔不准帶標記，符號也不該散進三份翻譯裡各寫一次。
  return `<span class="card-ignored">⊘ ${esc(t('accounts.ignoredmark'))}
    <button type="button" class="linkish" data-act="unignore">${
      esc(t('accounts.unignore'))}</button></span>`;
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
    // 「在 … 開啟」與詳情面板是同一句話，共用 `detail.openon`。
    return `<a class="ext-link" href="${esc(a.profile_url)}" target="_blank"
              rel="noreferrer">↗ ${esc(t('detail.openon',
                { platform: a.platform_label || a.platform }))}</a>`;
  }
  // 拼不出來時顯示**原因**而不是一個壞連結。灰階 + ⚠ 字符，不只靠顏色分辨。
  return `<span class="ext-link off" data-tip="${esc(a.link_problem || '')}"
            tabindex="0">⚠ ${esc(t('detail.nolink'))}</span>`;
}

function cardHtml(a) {
  const v = verdict(a);
  const defaults = [a.default_rating, a.default_content_type].filter(Boolean).join(' · ');
  // ⚠️ 用 `isPicked()` 不是 `acctPicked.has()` —— 範圍是「全部符合篩選的」時，
  // 本頁的卡也該顯示成已選，而那些 id 在 acctAllIdSet 裡不在 acctPicked 裡。
  const picked = isPicked(a.id);
  // 選取模式時卡片多一個核取方塊，**而且整張卡加框** —— 一頁 100 張時
  // 只靠角落一個小方塊掃不出選了哪些（滿載才是分組必須成立的時候）。
  const box = acctSelecting
    ? `<input type="checkbox" class="acct-pick" data-act="pick"
        ${picked ? 'checked' : ''} aria-label="${
          esc(t('accounts.pick.aria', { name: acctName(a) }))}">`
    : '';
  return `<div class="card${picked ? ' picked' : ''}" data-id="${a.id}">
    <div class="card-head">
      ${box}
      <button type="button" class="fav${a.is_favorite ? ' on' : ''}"
              data-act="fav" data-tip="${esc(t('accounts.fav.tip'))}">${
                a.is_favorite ? '♥' : '♡'}</button>
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
              data-tip="${esc(t(a.media_count
                ? 'accounts.viewmedia.tip' : 'accounts.viewmedia.none'))}"
              >${esc(tn('accounts.media', a.media_count || 0))}</button>
      · <span class="num">${esc(tn('accounts.posts', a.post_count || 0))}</span><br>
      ${esc(t('accounts.lastpost', { when: fmt.date(a.last_post_at) }))}
      <span class="card-verdict${v.bad ? ' bad' : ''}"${
        v.full ? ` data-tip="${esc(v.full)}"` : ''}>${esc(v.text)}</span>
      ${untrackedHtml(a)}
      ${ignoredHtml(a)}
    </div>
    ${previewHtml(a)}
    <div class="card-foot">
      <span>${esc(t('accounts.defaults', { what: defaults || unsetText() }))}</span>
      <span class="spacer"></span>
      <span class="card-msg"></span>
      <button type="button" class="ghost" data-act="edit">${esc(t('accounts.edit'))}</button>
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
    return `<div class="prev-empty">${esc(t(a.media_count
      ? 'accounts.prev.notcomputed' : 'accounts.prev.nomedia'))}</div>`;
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
  acctChips.render();
  const seq = ++acctSeq;
  unsetRating = null;          // 條件變了，上一批的數字就不再成立
  $('accountList').innerHTML = skeletons();
  $('accountCount').textContent = t('common.calculating');

  let res;
  try {
    res = await fetch(`/api/accounts?${accountQuery()}`);
    // ⚠️ 這一支不能走 `api()`：它要讀 `X-Total-Count`，而 `api()` 只回 body。
    // 但錯誤仍然要走同一條路，否則畫面上會是一句「載入失敗：422」。
    if (!res.ok) throw await toApiError(res);
  } catch (e) {
    if (seq !== acctSeq) return;
    $('accountList').innerHTML = `<p class="empty">${
      esc(t('media.load.failed', { msg: e.message }))}</p>`;
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
    ? `${fmt.num(from)}–${fmt.num(Math.min(state.acctOffset + ACCT_PAGE, state.acctTotal))} / ${
        fmt.num(state.acctTotal)}`
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
    return `<p class="empty">${esc(t('accounts.empty.search', { q }))}<br>
      <button type="button" class="ghost" data-act="clearsearch">${
        esc(t('accounts.empty.clearsearch'))}</button></p>`;
  }
  // 與標籤列讀同一份值域 —— 兩邊各串一次的結果是這裡少講兩個條件。
  const conds = acctConditions();
  if (conds.length) {
    // ⚠️ 逗號式的頓號不是全語系共用的：英文是 `, `、中日文是 `、`。
    // 寫死 `、` 的話英文介面上會冒出一個中文標點。
    return `<p class="empty">${esc(t('accounts.empty.conds'))}<br>${
      esc(t('chips.lead'))}${
      esc(conds.map((c) => `${c.label} ${c.value}`).join(t('common.listsep')))}</p>`;
  }
  return `<p class="empty">${esc(t('accounts.empty.none.1'))}<br>${
    esc(t('accounts.empty.none.2'))}</p>`;
}

// 「還沒設預設值的有幾個」——一次請求、只要 header 上的總數。
// 不擋畫面：它回來之前那半句就先不顯示（**不顯示 0**，0 是另一個意思）。
let unsetRating = null;

/** 清單層級的第 6 題：不只說有幾個帳號，說**整理工作還剩多少**。 */
function paintAccountCount() {
  const el = $('accountCount');
  // ⚠️ 數字包在 <b> 裡，所以把整段標記當**參數**傳進去 —— 語系檔不准帶標記
  // （與媒體頁的 `paintCount()` 同一個做法）。
  el.innerHTML = tn('accounts.count', state.acctTotal, {
    n: `<b class="todo">${fmt.num(state.acctTotal)}</b>`,
  })
    + (unsetRating == null ? '' :
      // ⚠️ 原本這裡是一個全形空白 `　`。它在英文與日文介面上都是一個
      // 中文排版習慣的字元，換成 `&ensp;` —— 寬度一樣，但不屬於任何語系。
      `&ensp;&ensp;<span class="muted">${esc(unsetRating
        ? tn('accounts.count.unsetrating', unsetRating)
        : t('accounts.count.allset'))}</span>`);
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
    (e) => say(card, t('accounts.stars.failed', { msg: e.message }), 'err'),
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
      $('aSelBar').dataset.note = t('accounts.sel.scope.back');
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
      say(card, t('accounts.unignore.failed', { msg: e.message }), 'err');
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
      say(card, t('accounts.retrack.failed', { msg: e.message }), 'err');
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
      say(card, t('common.failed.msg', { msg: e.message }), 'err');
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
    // ⚠️ 有粗體的句子一律**把 `<b>…</b>` 當參數傳進 `t()`**，不把標記寫進
    //    語系檔（語系檔是最容易被隨手編輯的東西，能塞標記等於開一個注入點）。
    //    也不拆成 pre/b/post 三個 key —— 強調詞在句中的位置三個語系都不同，
    //    拆了就等於逼翻譯去遷就中文的語序。
    body: `
      <div class="ovl-section">
        <h3>${esc(t('accounts.drawer.defaults.title'))}</h3>
        <div class="row">
          <span id="dfRating" class="ms-host"></span>
          <span id="dfContent" class="ms-host"></span>
          <span class="spacer"></span>
          <button type="button" id="dfSave">${esc(t('common.save'))}</button>
        </div>
        <p class="note">⚠ ${t('accounts.drawer.defaults.note', {
          what: `<b>${esc(t('accounts.drawer.defaults.note.b'))}</b>`,
          n: fmt.num(a.post_count || 0),
        })}</p>
        <p class="note" id="dfMsg"></p>
      </div>

      <div class="ovl-section">
        <h3>${esc(t('accounts.drawer.retag.title'))}</h3>
        <p class="note">${a.post_count
          ? `${esc(t('accounts.drawer.retag.n', { n: fmt.num(a.post_count) }))}`
            + `<br><b>${esc(t('accounts.drawer.retag.nomanual'))}</b>`
          : t('accounts.drawer.retag.noposts', {
              what: `<b>${esc(t('accounts.drawer.retag.noposts.b'))}</b>`,
            })}</p>
        <div class="row">
          <span class="spacer"></span>
          <button type="button" id="dfRetag" ${a.post_count ? '' : 'disabled'}>${
            esc(t('accounts.drawer.retag.btn'))}</button>
        </div>
        <p class="note" id="dfRetagMsg"></p>
      </div>

      <div class="ovl-section">
        <h3>${esc(t('accounts.drawer.creator.title'))}${
          hint(t('accounts.drawer.creator.tip'))}</h3>
        <div class="row">
          <span id="dfCreator" class="ms-host"></span>
          <span id="dfRole" class="ms-host"></span>
          <button type="button" id="dfLink">${esc(t('bulk.apply'))}</button>
        </div>
        <div class="row">
          <input id="dfNewCreator" placeholder="${
            esc(t('accounts.drawer.creator.ph'))}">
          <button type="button" class="ghost" id="dfAddCreator">${
            esc(t('accounts.drawer.creator.add'))}</button>
          <span class="muted">${esc(tn('accounts.creators.have',
            state.creators.length))}</span>
        </div>
        <p class="note" id="dfLinkMsg"></p>
      </div>

      <div class="ovl-section danger-zone">
        <h3>${esc(t('accounts.drawer.danger.title'))}</h3>
        <p class="note">${esc(t('accounts.drawer.delete.note'))}<b>${
          esc(t('accounts.drawer.delete.keep'))}</b></p>
        <div class="row">
          <span class="spacer"></span>
          <button type="button" class="danger" id="dfDelete">${
            esc(t('accounts.drawer.delete.btn'))}</button>
        </div>
        <p class="note" id="dfDelMsg"></p>
      </div>`,
    onMount: (body, handle) => {
      // 抽屜每次打開都是新的一段 HTML —— 下拉要在這裡建，不是開頁時建一次。
      const d = mountDrops(body, {
        dfRating: {
          label: t('detail.rating.label'), emptyText: t('detail.untagged'),
          ariaLabel: t('accounts.drawer.rating.aria'),
          values: RATING_VALUES.map((v) => ({ value: v })),
          value: a.default_rating || '', onChange: () => {},
        },
        dfContent: {
          label: t('detail.content.label'), emptyText: t('detail.untagged'),
          ariaLabel: t('accounts.drawer.content.aria'),
          values: CONTENT_VALUES.map((v) => ({ value: v })),
          value: a.default_content_type || '', onChange: () => {},
        },
        dfCreator: {
          label: t('accounts.drawer.creator.none'),
          emptyText: t('accounts.drawer.creator.none'),
          ariaLabel: t('accounts.drawer.creator.aria'),
          values: creatorOpts,
          value: a.creator_id ? String(a.creator_id) : '', onChange: () => {},
        },
        dfRole: {
          label: t('accounts.drawer.role.none'),
          emptyText: t('accounts.drawer.role.none'),
          ariaLabel: t('accounts.drawer.role.aria'),
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
          note('#dfMsg', t('accounts.drawer.saved', {
            n: fmt.num(a.post_count || 0),
            retag: t('accounts.drawer.retag.btn'),
          }), 'good');
          repaintCard(a, card);
        } catch (e) { note('#dfMsg', t('common.failed.msg', { msg: e.message }), 'bad'); }
      });

      body.querySelector('#dfRetag').addEventListener('click', async (ev) => {
        const ok = await confirmDialog({
          title: t('accounts.retag.confirm.title'),
          lines: [
            t('accounts.retag.confirm.1', { name: acctName(a) }),
            '',
            `· ${tn('accounts.retag.confirm.2', a.post_count || 0)}`,
            `· ${t('accounts.retag.confirm.3')}`,
            '',
            t('accounts.retag.confirm.4'),
          ],
          confirmText: t('accounts.retag.confirm.btn'),
        });
        if (!ok) return;
        ev.target.disabled = true;
        note('#dfRetagMsg', t('accounts.retag.running'));
        try {
          const r = await api(`/api/accounts/${a.id}/retag`, {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ overwrite_manual: false }),
          });
          note('#dfRetagMsg', t('accounts.retag.done', { n: fmt.num(r.updated) }), 'good');
        } catch (e) {
          note('#dfRetagMsg', t('common.failed.msg', { msg: e.message }), 'bad');
        } finally {
          ev.target.disabled = false;
        }
      });

      body.querySelector('#dfAddCreator').addEventListener('click', async () => {
        const name = body.querySelector('#dfNewCreator').value.trim();
        if (!name) {
          note('#dfLinkMsg', t('accounts.drawer.creator.needname'), 'bad');
          return;
        }
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
          note('#dfLinkMsg', t('accounts.drawer.creator.created',
                             { name, apply: t('bulk.apply') }), 'good');
        } catch (e) {
          note('#dfLinkMsg', t('accounts.drawer.creator.addfailed', { msg: e.message }), 'bad');
        }
      });

      body.querySelector('#dfLink').addEventListener('click', async () => {
        const cid = d.dfCreator.get();
        try {
          if (!cid) {
            await api(`/api/accounts/${a.id}/link`, { method: 'DELETE' });
            a.creator_id = null;
            a.role = null;
            note('#dfLinkMsg', t('accounts.drawer.creator.unlinked'), 'good');
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
            note('#dfLinkMsg', t('accounts.drawer.creator.linked'), 'good');
          }
          await loadCreatorList();
        } catch (e) {
          note('#dfLinkMsg', t('common.failed.msg', { msg: e.message }), 'bad');
        }
      });

      body.querySelector('#dfDelete').addEventListener('click', async () => {
        let p;
        try {
          // 先問「會刪掉什麼」再讓使用者決定 —— 不做「按一下就刪」。
          p = await api(`/api/accounts/${a.id}/deletion-preview`);
        } catch (e) {
          note('#dfDelMsg', t('accounts.delete.nopreview', { msg: e.message }), 'bad');
          return;
        }
        // ⚠️ 這段文字是**產品層摩擦**，一字不減。2.0 唯一的改動是把
        // window.confirm()（會擋住整個分頁、樣式不一致）換成自製 dialog。
        const ok = await confirmDialog({
          title: t('accounts.delete.confirm.title',
                   { name: p.screen_name, platform: p.platform }),
          lines: [
            `· ${tn('accounts.posts', p.posts)}`,
            `· ${tn('accounts.mediarows', p.media)}`,
            '',
            t('accounts.delete.confirm.keep'),
            // ⚠️ `warnings` 是後端寫的原文，沒有 key 可查。顯示原文比顯示
            // `⟦…⟧` 誠實 —— 而且看得出來它還沒被翻譯。
            ...p.warnings.map((w) => `⚠️ ${w}`),
            '',
            t('accounts.delete.confirm.final'),
          ],
          confirmText: t('accounts.delete.confirm.btn'),
          danger: true,
        });
        if (!ok) return;
        try {
          const r = await api(`/api/accounts/${a.id}?confirm=true`, { method: 'DELETE' });
          handle.close();
          // 三個數字各自有單複數，所以三段各自 tn() 之後才組句。
          say(card, t('accounts.delete.done', {
            posts: tn('accounts.posts', r.posts),
            media: tn('accounts.mediarows', r.media),
            kept: tn('accounts.files', r.downloaded_files_kept),
          }), 'ok');
          loadAccounts();
        } catch (e) { note('#dfDelMsg', t('common.failed.msg', { msg: e.message }), 'bad'); }
      });
    },
  });
}

/** 抽屜裡改過的東西要反映到卡面上，但**不重載整份清單** ——
 *  重載會讓使用者剛才捲到的位置整個跳掉。 */
function repaintCard(a, card) {
  const foot = card.querySelector('.card-foot span');
  const defaults = [a.default_rating, a.default_content_type].filter(Boolean).join(' · ');
  if (foot) foot.textContent = t('accounts.defaults', { what: defaults || unsetText() });
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
  clearAcctSelection(t('accounts.sel.cleared.filter'));
  state.acctOffset = 0;
  loadAccounts();
});

// ⟳ 與媒體頁的 #refresh 同語意：**保留篩選與頁碼**，只是重新問一次。
// 它不吃篩選，所以創作者模式下仍然可用（applyModeGate 沒有把它關掉）。
$('aRefresh').addEventListener('click', () => {
  if (state.acctMode === 'creators') loadCreators();
  else loadAccounts();
});

$('aPrev').addEventListener('click', () => {
  clearAcctSelection(t('accounts.sel.cleared.page'));
  state.acctOffset = Math.max(0, state.acctOffset - ACCT_PAGE);
  loadAccounts();
});
$('aNext').addEventListener('click', () => {
  clearAcctSelection(t('accounts.sel.cleared.page'));
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
      $('aFetchStatus').dataset.tip = t('accounts.fetchnote.never');
      $('aFetchNote').textContent = t('accounts.fetchnote.never.long');
    } else {
      $('aFetchNote').textContent = tn('accounts.fetchnote.n', n);
    }
  } catch { /* 補充說明，拿不到就不寫 */ }
});

// ── 檢視切換：帳號／創作者 ────────────────────────────
//
// 兩者是**同一份資料的兩種分組方式**，不是兩個工作面 —— 不做成第四個 tab，
// 放成 tab 會讓人以為它們是平行的功能。
//
// 但它換的是**資料集本身**（不同的表、不同的 API、不同的可用篩選值域），
// 層級高於篩選與排序：它一變，右邊那些控制項全部失去意義。所以它在
// filter-bar 的**最左端**，而不是跟排序搶右端。閱讀順序 = 支配順序。
//
// 元件是 M3 single-select segmented button：兩個選項互斥且永遠有一個成立。
// 鍵盤走 roving tabindex（Tab 只進出群組一次，群組內用方向鍵）——
// 那是 `role="radiogroup"` 該有的行為，一堆 `<button>` 不會自己長出來。

const VIEW_BTNS = () => [...$('aViewMode').querySelectorAll('[role="radio"]')];

function setViewMode(mode, { focus = false } = {}) {
  if (state.acctMode === mode) return;
  state.acctMode = mode;
  for (const b of VIEW_BTNS()) {
    const on = b.dataset.mode === mode;
    b.setAttribute('aria-checked', String(on));
    // roving tabindex：只有選中那格在 Tab 順序裡。
    if (on) { b.removeAttribute('tabindex'); if (focus) b.focus(); }
    else b.setAttribute('tabindex', '-1');
  }
  $('accountPane').classList.toggle('hidden', mode !== 'accounts');
  $('creatorPane').classList.toggle('hidden', mode !== 'creators');
  applyModeGate();
  if (mode === 'creators') loadCreators();
  else loadAccounts();
}

$('aViewMode').addEventListener('click', (ev) => {
  const btn = ev.target.closest('[role="radio"]');
  if (btn) setViewMode(btn.dataset.mode);
});

$('aViewMode').addEventListener('keydown', (ev) => {
  const step = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -1, ArrowDown: 1 }[ev.key];
  if (!step) return;
  ev.preventDefault();
  const btns = VIEW_BTNS();
  const i = btns.findIndex((b) => b.getAttribute('aria-checked') === 'true');
  // radiogroup 的方向鍵是**繞回**的，不是走到底就停。
  setViewMode(btns[(i + step + btns.length) % btns.length].dataset.mode, { focus: true });
});

/** 創作者模式下哪些控制項不適用，以及**為什麼**。
 *
 *  ⚠️ 這不是 fallback，是把「後端不支援」誠實顯示出來。現況才是掩蓋：
 *  `loadCreators()` 一個參數都不傳，但整條 filter-bar 仍然亮著、可點、
 *  改了什麼都不會發生，畫面上沒有任何說明。日後 `/api/creators` 支援篩選了，
 *  把這張表刪掉即可，沒有任何機制要拆。
 *
 *  ⚠️ **不是隱藏。** 隱藏會讓人以為控制項不見了，而它切回來又會出現 ——
 *  看起來像閃爍的 bug。disabled + 原因才回答得了「為什麼現在不能用」。
 *
 *  ⚠️ **值不清空。** disabled 期間只是不能改，切回帳號要原樣還在。 */
// ⚠️ 值是 key。這張表在模組載入時建好，那時 i18n 還沒載完 —— `why()` 才 t()。
const MODE_GATE = {
  aSearch: 'accounts.gate.search',
  aPlatform: 'accounts.gate.platform',
  aDefaultRating: 'accounts.gate.accountlevel',
  aDefaultContent: 'accounts.gate.accountlevel',
  aMore: 'accounts.gate.more',
  aSortKey: 'accounts.gate.sort',
  aSortDir: 'accounts.gate.sort',
  aSelectMode: 'accounts.gate.select',
};

function applyModeGate() {
  const off = state.acctMode === 'creators';
  const why = (id) => (off ? t(MODE_GATE[id]) : null);
  setFieldOff($('aSearch'), why('aSearch'));
  setFieldOff($('aSortDir'), why('aSortDir'));
  setFieldOff($('aSelectMode'), why('aSelectMode'));
  setDetailsOff($('aMore'), why('aMore'));
  for (const id of ['aPlatform', 'aDefaultRating', 'aDefaultContent']) {
    aDrops[id].setOff(why(id));
  }
  sortCtl.drop.setOff(why('aSortKey'));
  // ⟳ 是唯一仍然可用的 —— 它不吃篩選，重新載入永遠是合理的。
  // 標籤列整條隱藏：那些條件當下不生效，顯示它們就是說謊。
  $('aChipBar').classList.toggle('hidden', off);
  if (off) $('aSortNote').textContent = t('accounts.gate.sortnote');
  else sortCtl.paint();
}

/** 只取資料（媒體頁的 creator 下拉與抽屜都要用），不畫創作者清單。 */
export async function loadCreatorList() {
  const list = await api('/api/creators');
  state.creators = list;
  paintMoreNotes();
  return list;
}

export async function loadCreators() {
  const list = await loadCreatorList();
  $('accountCount').innerHTML = tn('accounts.creators.count', list.length, {
    n: `<b class="todo">${fmt.num(list.length)}</b>`,
  });

  // 正式庫 creators = 0 —— 這是**目前唯一會看到的狀態**，所以它必須解釋
  // 「這東西是幹嘛的、怎麼開始」，而不只是說「沒有資料」。
  $('creatorList').innerHTML = list.length
    ? list.map((c) => `
      <div class="card">
        <h3>${esc(c.display_name)}</h3>
        <div class="card-id">${esc(tn('accounts.creators.accounts', c.accounts.length))}</div>
        <div class="row">
          ${c.accounts.map((a) => `<span class="pill">${esc(a.platform)} @${
            esc(a.screen_name || '?')}${a.role ? ` · ${esc(a.role)}` : ''}</span>`).join('')
            || `<span class="muted">${esc(t('accounts.creators.noaccounts'))}</span>`}
        </div>
        <div class="row">
          <span class="spacer"></span>
          <button type="button" class="ghost" data-creator="${c.id}"
                  data-label="${esc(c.display_name)}">${
                    esc(t('accounts.creators.viewall'))}</button>
        </div>
      </div>`).join('')
    : `<p class="empty">${esc(t('accounts.creators.empty'))}<br>
        ${esc(t('accounts.creators.what'))}<br>
        ${t('accounts.creators.howto', {
          where: `<b>${esc(t('accounts.creators.howto.b', {
            edit: t('accounts.edit'),
            link: t('accounts.drawer.creator.title'),
          }))}</b>`,
        })}</p>`;
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
      // 括號也是語系的一部分：中日文用全形（），英文用半形 ( )。
      text: t('accounts.platform.count', { platform: it.platform, n: fmt.num(it.count) }),
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
// ⚠️ `label` 與 `opts` 的顯示字**都是 key**（`opts` 的第一欄是送給後端的值，
//    那個不動）。這張表在模組載入時建好，t() 要等到 `renderSelBar()` 才做。
//    分級（sfw / r18）與類型的值本身是資料不是文案，維持原樣。
const BULK_FIELDS = [
  { key: 'is_ignored', label: 'accounts.ignored', opts: [
    ['true', 'accounts.bulk.ignore.on'], ['false', 'accounts.bulk.ignore.off']] },
  { key: 'is_tracked', label: 'accounts.bulk.tracked', opts: [
    ['true', 'accounts.bulk.track.on'], ['false', 'accounts.bulk.track.off']] },
  { key: 'default_rating', label: 'chips.defrating', clear: true, opts: [
    ['sfw', null], ['r18', null]] },
  { key: 'default_content_type', label: 'chips.defcontent', clear: true,
    opts: CONTENT_VALUES.map((v) => [v, null]) },
  { key: 'is_favorite', label: 'accounts.fav', opts: [
    ['true', 'accounts.bulk.fav.on'], ['false', 'accounts.bulk.fav.off']] },
  { key: 'stars', label: 'filter.stars', clear: true, opts: [
    ['5', null], ['4', null], ['3', null], ['2', null], ['1', null]] },
];

/** 批次下拉的一個選項要顯示什麼。`null` = 顯示值本身（`sfw` / `r18` /
 *  類型那些是資料，不翻譯）；`stars` 例外，畫成星星。 */
function bulkOptText(fieldKey, value, textKey) {
  if (fieldKey === 'stars') return '★'.repeat(Number(value));
  return textKey ? t(textKey) : value;
}

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
  $('aSelectMode').textContent = t(acctSelecting
    ? 'accounts.selectmode.exit' : 'accounts.selectmode');
  if (!acctSelecting) return;

  const pageN = state.accounts.length;
  const total = state.acctTotal;
  const n = pickedCount();
  // 粗體當參數傳進去 —— 語系檔不准帶標記，而強調詞在句中的位置各語系不同。
  const scope = acctPickScope === 'all'
    ? t('accounts.sel.scope.all', { what: `<b>${esc(t('accounts.sel.scope.all.b'))}</b>` })
    : t('accounts.sel.scope.page');

  // ⚠️ 兩顆按鈕、兩個數字。第二顆在 total ≤ pageN 時**不出現** ——
  // 那時它與第一顆同義，兩顆一樣的按鈕只會製造疑惑。
  const buttons = acctPickScope === 'all'
    ? `<button type="button" class="ghost" data-sel="page">${
        esc(t('accounts.sel.onlypage', { n: fmt.num(pageN) }))}</button>`
    : `<button type="button" class="ghost" data-sel="page">${
        esc(t('accounts.sel.allpage', { n: fmt.num(pageN) }))}</button>`
      + (total > pageN
        ? `<button type="button" class="ghost" data-sel="all">${
            esc(t('accounts.sel.allmatching', { n: fmt.num(total) }))}</button>`
        : '');

  const warn = acctPickScope === 'all' && total > pageN
    ? `<div class="sel-warn">${esc(t('accounts.sel.warn.1'))}<br>${
        t('accounts.sel.warn.2', {
          n: fmt.num(total - pageN),
          cannot: `<b>${esc(t('accounts.sel.warn.2.b'))}</b>`,
        })}</div>`
    : '';

  const fields = BULK_FIELDS.map((f) =>
    `<label class="chk">${esc(t(f.label))}<span data-bulk="${
      f.key}" class="ms-host"></span></label>`
  ).join('');

  bar.innerHTML = `
    <div class="sel-row">
      <span class="sel-count">${t('accounts.sel.count', {
        n: `<b>${fmt.num(n)}</b>`, scope })}</span>
      ${buttons}
      <button type="button" class="ghost" data-sel="none">${
        esc(t('media.sel.clear'))}</button>
    </div>
    ${warn}
    ${bar.dataset.note ? `<div class="sel-warn">${esc(bar.dataset.note)}</div>` : ''}
    <div class="sel-row">
      ${fields}
      <span class="spacer"></span>
      <span class="muted">${esc(t('accounts.sel.applynote'))}</span>
      <button type="button" id="aBulkApply"${n ? '' : ' disabled'}
        data-tip="${n ? '' : esc(t('accounts.sel.pickaccounts'))}">${
          esc(t('bulk.apply'))}</button>
    </div>`;

  // innerHTML 換完才有佔位元素可以掛。值從 bulkValues 回填 —— 重畫不清空。
  mountDrops(bar, Object.fromEntries(BULK_FIELDS.map((f) => [
    `[data-bulk="${f.key}"]`,
    {
      label: '—',
      emptyText: '—',
      ariaLabel: t('accounts.bulk.aria', { what: t(f.label) }),
      values: f.opts.map(([v, key]) => ({ value: v, text: bulkOptText(f.key, v, key) }))
        .concat(f.clear ? [{ value: '__clear__', text: t('bulk.clear') }] : []),
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
  // 與批次列的下拉是**同一組字**（`accounts.bulk.*`）—— 選的時候與確認的時候
  // 講法不一致，使用者會以為自己按到了別的東西。
  const LABEL = {
    is_ignored: (v) => t(v === 'true' ? 'accounts.bulk.ignore.on' : 'accounts.bulk.ignore.off'),
    is_tracked: (v) => t(v === 'true' ? 'accounts.bulk.track.on' : 'accounts.bulk.track.off'),
    is_favorite: (v) => t(v === 'true' ? 'accounts.bulk.fav.on' : 'accounts.bulk.fav.off'),
    default_rating: (v) => (v === '__clear__'
      ? t('accounts.bulk.prev.rating.clear')
      : t('accounts.bulk.prev.rating.set', { v })),
    default_content_type: (v) => (v === '__clear__'
      ? t('accounts.bulk.prev.content.clear')
      : t('accounts.bulk.prev.content.set', { v })),
    stars: (v) => (v === '__clear__'
      ? t('accounts.bulk.prev.stars.clear')
      : t('accounts.bulk.prev.stars.set', { n: fmt.num(Number(v)) })),
  };
  const lines = Object.entries(fields).map((e) => `<li>${esc(LABEL[e[0]](e[1]))}</li>`);
  if (fields.is_ignored === 'true') {
    lines.push(`<li>${t('accounts.bulk.prev.ignored.note', {
      not: `<b>${esc(t('accounts.bulk.prev.ignored.note.b'))}</b>`,
    })}</li>`);
  }
  const batches = Math.ceil(n / BULK_ID_LIMIT);
  if (batches > 1) {
    lines.push(`<li>${t('accounts.bulk.prev.batches', {
      n: `<b>${fmt.num(batches)}</b>`, limit: fmt.num(BULK_ID_LIMIT),
    })}</li>`);
  }
  box.innerHTML = `<div class="bulk-box">
    <h4>${esc(tn('accounts.bulk.head', n))}</h4>
    <ul>${lines.join('')}
      <li class="ok-line"><b>${esc(t('accounts.bulk.safe'))}</b></li>
      <li>${esc(t('accounts.bulk.noundo'))}</li>
    </ul>
    <div class="row">
      <button type="button" id="aBulkYes">${
        esc(t('accounts.bulk.confirm', { n: fmt.num(n) }))}</button>
      <button type="button" id="aBulkNo" class="ghost">${esc(t('confirm.no'))}</button>
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
    prog.innerHTML = esc(batches.length > 1
      ? t('accounts.bulk.batch', {
          i: fmt.num(b + 1), total: fmt.num(batches.length), n: fmt.num(batches[b].length),
        })
      : t('accounts.bulk.writing'));
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
      prog.innerHTML = `<span class="err">${esc(t('accounts.bulk.batchfail', {
        i: fmt.num(b + 1), msg: e.message }))}</span><br>`
        + (b > 0
          ? t('accounts.bulk.partial', {
              done: `<b>${esc(t('accounts.bulk.partial.b', {
                b: fmt.num(b), n: fmt.num(updated) }))}</b>`,
            })
          : esc(t('accounts.bulk.nonechanged')));
      return;
    }
  }

  // ③ 結果。`missing` 一定要講 —— 只說「改好 4,650 個」而使用者選了 4,653，
  // 那 3 個去哪了沒人講得出來。
  prog.innerHTML = `<span class="ok-line">${
      esc(t('accounts.bulk.done', { n: fmt.num(updated) }))}</span>`
    + (missing.length
      ? `<br><span class="err">${
          esc(tn('accounts.bulk.missing', missing.length))}</span>`
        + esc(t('accounts.bulk.missing.why', {
            ids: missing.slice(0, 20).join(t('common.listsep'))
              + (missing.length > 20 ? ' …' : ''),
          }))
      : '')
    + (fields.is_ignored ? `<br>${esc(t('accounts.bulk.fetchable.changed'))}` : '');
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
  // 創作者檢視沒有帳號 id 可以批次。原因由 applyModeGate() 掛在按鈕上
  // （data-tip + 常駐的 aria-describedby），這裡只負責不作用 ——
  // 同一個元素上的 listener 依註冊順序觸發，dom.js 的 guard 比這一個晚掛。
  if ($('aSelectMode').getAttribute('aria-disabled') === 'true') return;
  if (acctSelecting) { exitSelect(); return; }
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
        sel.textContent = t('accounts.sel.idsfailed', { msg: e.message });
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
      $('aSelBar').dataset.note = t('accounts.sel.pickfield');
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
    if (no) { no.disabled = false; no.textContent = t('accounts.bulk.close'); }
  }
});
