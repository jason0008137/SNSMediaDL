// DOM 小工具與共用元件。

import { fmt, t } from './i18n.js';

export const $ = (id) => document.getElementById(id);

export const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// ── (?) 說明鈕 ─────────────────────────────────────────
//
// **全站唯一的獨立說明入口符號。** 不新增第二套（資訊圖示、燈泡、括號 i）——
// 兩套符號並存的
// 意思是使用者每次都得先判斷「這兩個是不是同一件事」。
//
// ⚠️ 統一的是**獨立說明入口**，不是「所有說明都要有符號」。
// 直接掛在控制項上的 `data-tip`（hover 控制項本身就會出現）是對的模式，
// 不要為了「統一」給它們也配一顆 `?`。
//
// ⚠️ **什麼能收進來**：背景知識、原理、格式規則、低頻補充。
// 約束、作用範圍、破壞性後果**一律留在畫面上** —— 那些是使用者做決定
// 當下需要的東西，藏起來就等於沒有（見 js/tooltip.js 開頭）。

/** 一顆 `?` 說明鈕的 HTML 字串。
 *
 *  ⚠️ 各 view **不要自己手寫這一串**。手寫的那幾顆遲早會漏掉 `type="button"`
 *  （在 `<form>` 裡會變成送出鈕）或 `aria-label`（讀屏只唸得到「問號」）。
 *
 *  多行用 `
` 就好 —— 這裡走 `dataset`，不是 HTML 屬性字面值，
 *  不需要 `&#10;` 那一套實體。
 */
export function hint(text) {
  return `<button type="button" class="hintbtn" aria-label="${esc(t('help.aria'))}"
    data-tip="${esc(text)}">?</button>`;
}

// ── 五星評分元件 ───────────────────────────────────────
// ⚠️ 這是「評分」，與 rating（sfw / r18 分級）是**兩件事**。
// 後端欄位叫 stars，前端也一律用 stars，不要混用 rating 這個字。

/** 五顆星的 HTML。`value` 為 null 代表未評分（不是 0 分）。 */
export function starsHtml(value, cls = '') {
  const stars = [1, 2, 3, 4, 5].map((n) =>
    `<button type="button" class="star${value && n <= value ? ' on' : ''}" data-n="${n}"
             aria-label="${esc(t('stars.n.aria', { n }))}">★</button>`).join('');
  // 原生 title 已全站淘汰（見 js/tooltip.js）：這一句是「怎麼清除評分」的
  // 唯一說明，掛在 title 上等於鍵盤與觸控使用者永遠看不到。
  return `<span class="stars ${cls}" data-stars="${value ?? ''}"
                data-tip="${esc(t('stars.tip'))}">${stars}</span>`;
}

export function paintStars(root, value) {
  root.dataset.stars = value ?? '';
  root.querySelectorAll('.star').forEach((b) => {
    b.classList.toggle('on', value !== null && Number(b.dataset.n) <= value);
  });
}

/** 處理一次點擊。回傳 false = 這一下不是點在星星上，呼叫端自己接手。
 *
 *  拆出來是為了**事件委派**：清單頁一頁 100 張卡，每張綁 5 顆星就是 500 個
 *  listener，而且每次重畫都會再產生一批。容器上一個 listener 就夠。 */
export async function handleStarClick(ev, onSet, onError) {
  const btn = ev.target.closest?.('.star');
  if (!btn) return false;
  // 帳號卡與媒體格子本身都有 click handler，不擋的話會順便開詳情／切換選取
  ev.stopPropagation();
  ev.preventDefault();
  const root = btn.closest('.stars');
  const before = root.dataset.stars ? Number(root.dataset.stars) : null;
  const n = Number(btn.dataset.n);
  // 再點同一顆 = 清除。這是唯一的清除方式，所以元件的氣泡要寫出來。
  const next = before === n ? null : n;
  paintStars(root, next);
  try {
    await onSet(next);
  } catch (e) {
    paintStars(root, before);   // 還原，不要顯示一個沒存進去的值
    if (onError) onError(e);
  }
  return true;
}

/** 綁定**單一**五星元件（詳情面板那種只有一個的場合）。
 *  清單頁請改用 `handleStarClick` 做委派。 */
export function wireStars(root, onSet, onError) {
  root.addEventListener('click', (ev) => handleStarClick(ev, onSet, onError));
}

// TB 是必要的，不是防禦性的：正式庫總計 1.27 TB。
// 少了 TB 這一級，它會顯示成「1305.7 GB」—— 讀得懂但沒人看得快。
export const fmtBytes = (n) => {
  if (!n) return '—';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
};

// `fmtWhen()` 已刪除。它回的是 ISO 前 10 碼（`2026-08-14`），那是**一種語系的
// 寫法**，不是中立格式 —— 換 locale 之後日英日三邊的日期順序不同，同一串數字
// 讀起來是不同的日子。日期一律走 `i18n.js` 的 `fmt.date()`。

// ── 讀不到檔案時，問出**為什麼** ─────────────────────
//
// `<img>` / `<video>` 的 error 事件**拿不到狀態碼**，所以要補一次 HEAD。
// 只有已經失敗的那幾格會付這個成本。
//
// ⚠️ 三個呼叫端（格線縮圖、詳情預覽、放大檢視）共用同一份對照表。
// 各寫一份的結果是文案漂移 —— 而這幾句話正是使用者唯一能拿到的診斷：
// 「檔案被刪了」與「那顆碟沒插」在畫面上長得一模一樣，措辭一旦不同步，
// 就沒人知道哪一句才是真的。
// ⚠️ 值是 **key 不是文字** —— 這張表在模組載入時就建好，那時 i18n
// 還沒載完。查表拿到 key、要用的時候才 t()。
//
// ⚠️ 503 與 415 是**兩件事**：一個是「裝一下 ffmpeg 就好」，一個是
// 「這個檔沒救」。混用的話使用者永遠不會去裝。
const FILE_ERRORS = {
  404: 'fileerr.404',
  403: 'fileerr.403',
  409: 'fileerr.409',
  415: 'fileerr.415',
  500: 'fileerr.500',
  503: 'fileerr.503',
};

/** 回一句可行動的原因。問不到就回 null —— **不要猜**。 */
export async function fileErrorText(mediaId, { thumb = false } = {}) {
  const url = `/api/media/${mediaId}/${thumb ? 'thumb' : 'file'}`;
  try {
    const r = await fetch(url, { method: 'HEAD' });
    // ⚠️ 後端說「拿得到」但畫面顯示失敗，是**另一種**故障，不可以套用
    // 上面那幾句（尤其不能說「檔案被刪除」—— 它明明還在）。
    // 實際成因通常是瀏覽器解不了那個編碼，或檔案下載到一半就中斷了。
    if (r.ok) {
      return t('fileerr.ok');
    }
    return FILE_ERRORS[r.status]
      ? t(FILE_ERRORS[r.status])
      : t('fileerr.other', { status: r.status });
  } catch {
    // 連 HEAD 都發不出去 —— 後端沒在跑，或網路斷了。這也是答案。
    return null;
  }
}

// ── 多選下拉（篩選用）────────────────────────────────────
//
// 用原生 `<details>`：鍵盤操作、`aria-expanded` 由瀏覽器免費提供。
//
// ⚠️ **每勾一次就呼叫 onChange，不是面板關閉才呼叫。** `336ad1c` 回朔的
// 那一版是「關閉時才重查」，理由是省 COUNT（正式庫一次 1.3 秒）——
// 但慢的只有總數，結果本身只要 1 ms。延後重查等於把唯一的即時回饋換掉。
//
// ⚠️ `<details>` **沒有**「點外面就收起」與 Esc 關閉，原生 `<select>` 有。
// 自製的要自己補，否則面板會一直開著蓋住底下的內容。

const openDrops = new Set();

// 全域只掛一次。點到任何一個開著的面板之外就收起它。
//
// ⚠️ **不在模組頂層直接掛。** 頂層跑 `document.addEventListener` 會讓
// dom.js 在沒有 document 的環境（node 跑的 test_viewer.mjs）一載入就
// ReferenceError —— 而且是連帶把所有 import 它的模組一起拖下水，
// 症狀是「測試根本跑不起來」而不是「某條斷言失敗」。
// 改成第一次真的建出下拉時才掛，行為完全一樣（沒有下拉就沒有要收的東西）。
let dropListenersBound = false;

function bindDropListeners() {
  if (dropListenersBound) return;
  dropListenersBound = true;
  document.addEventListener('click', (ev) => {
    for (const d of openDrops) {
      if (!d.contains(ev.target)) d.open = false;
    }
  });
  document.addEventListener('keydown', (ev) => {
    if (ev.key !== 'Escape') return;
    for (const d of openDrops) d.open = false;
  });
}

/** 讓一個 `<details>` 納入「點外面收起 / Esc 收起 / 一次只開一個」。
 *
 *  ⚠️ 靜態寫在 HTML 裡的 `<details>`（例如「更多篩選」）**也要呼叫這個**。
 *  原生 `<details>` 沒有這些行為，原生 `<select>` 才有 —— 而使用者對一顆
 *  展開的下拉的預期就是 select 的預期：點別的地方它應該收起來。
 *  漏掉的症狀是「拉開後按其他地方不會收回」，面板會一直蓋住底下的內容。
 */
export function autoClose(d) {
  bindDropListeners();
  d.addEventListener('toggle', () => {
    if (d.open) {
      // 一次只開一個 —— 兩個面板疊在一起沒有任何好處。
      //
      // ⚠️ **巢狀的要放過**：「更多篩選」裡面還有評分／下載狀態這些下拉，
      // 打開內層時若把外層一起關掉，使用者才剛按下去的面板整個消失 ——
      // 看起來就像點一下就自己收起來。`contains()` 認得祖先關係。
      for (const other of openDrops) {
        if (other !== d && !other.contains(d)) other.open = false;
      }
      openDrops.add(d);
    } else {
      openDrops.delete(d);
    }
  });
}

// ── 整組不可用（不是「某個選項不可選」）─────────────────
//
// 創作者模式下 `/api/creators` 不吃任何篩選與排序 —— 但現況那些控制項仍然
// 亮著、可點、改了什麼都不會發生。這一段是把那件事**誠實說出來**的機制，
// 不是繞過：日後 API 支援了，把呼叫拿掉即可，沒有任何掩蓋要拆。
//
// ⚠️ **不用原生 `disabled`。** 原生 disabled 的元素在 Chrome 收不到
// mouseover —— 而「說得出原因」正是這整段存在的理由，滑鼠停上去卻什麼都
// 沒有的話就只剩「變灰了」。改用 `aria-disabled` + 退出 Tab 順序 + 自己擋
// 掉互動，三件事一起做（少任何一件的症狀都是「看起來不能用但其實還能用」）。
//
// ⚠️ 原因走**兩條路**：`data-tip` 給滑鼠，常駐的 `aria-describedby` 給讀屏
// （拿不到焦點的元素是用瀏覽模式讀過去的，等不到 focus 事件）。

let offSeq = 0;

/** 把原因掛成常駐的無障礙說明。`reason` 為空就拆掉。 */
function setOffReason(el, reason) {
  const id = el.dataset.offWhyId;
  if (!reason) {
    if (id) {
      document.getElementById(id)?.remove();
      delete el.dataset.offWhyId;
      el.removeAttribute('aria-describedby');
    }
    return;
  }
  let span = id ? document.getElementById(id) : null;
  if (!span) {
    span = document.createElement('span');
    span.className = 'sr-only';
    span.id = `off-why-${++offSeq}`;
    el.after(span);
    el.dataset.offWhyId = span.id;
  }
  span.textContent = reason;
  el.setAttribute('aria-describedby', span.id);
}

/** 滑鼠那條路。還原時把原本那句放回去（下拉本來就有自己的說明）。 */
function setOffTip(el, reason) {
  if (reason) {
    if (el.dataset.tipWas === undefined) el.dataset.tipWas = el.dataset.tip ?? '';
    el.dataset.tip = reason;
  } else if (el.dataset.tipWas !== undefined) {
    if (el.dataset.tipWas) el.dataset.tip = el.dataset.tipWas;
    else delete el.dataset.tip;
    delete el.dataset.tipWas;
  }
}

function markOff(el, reason) {
  if (reason) {
    el.setAttribute('aria-disabled', 'true');
    el.setAttribute('tabindex', '-1');
  } else {
    el.removeAttribute('aria-disabled');
    el.removeAttribute('tabindex');
  }
  setOffReason(el, reason);
  setOffTip(el, reason);
}

/** 攔掉一個已標記 off 的元素上的互動。只掛一次。 */
function guardOff(el) {
  if (el.dataset.offGuard) return;
  el.dataset.offGuard = '1';
  const block = (ev) => {
    if (el.getAttribute('aria-disabled') !== 'true') return;
    ev.preventDefault();
    ev.stopPropagation();
  };
  // capture 階段擋在原生行為之前。冒泡階段來不及 —— 那時 <details>
  // 已經開了、輸入框也已經吃到那個字。
  el.addEventListener('click', block, true);
  el.addEventListener('keydown', (ev) => {
    if (el.getAttribute('aria-disabled') !== 'true') return;
    // Tab 要放行，否則焦點會卡在這裡出不去。
    if (ev.key !== 'Tab') block(ev);
  }, true);
}

/** 讓一個 `<details>`（自製下拉、或「更多篩選」）整組不可用。
 *
 *  @param d      `<details>` 元素
 *  @param reason 原因字串；`null` / `''` = 恢復
 */
export function setDetailsOff(d, reason) {
  const sum = d.querySelector('summary');
  if (reason) d.open = false;
  d.classList.toggle('off', Boolean(reason));
  markOff(sum, reason);
  guardOff(sum);
}

/** 讓一個 button / input 整組不可用。
 *
 *  ⚠️ 輸入框另外要 `readOnly` —— 只擋 keydown 擋不掉貼上與輸入法。 */
export function setFieldOff(el, reason) {
  markOff(el, reason);
  guardOff(el);
  if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') el.readOnly = Boolean(reason);
}

/** 建一個多選下拉。
 *
 *  @param host     容器元素（會被清空）
 *  @param label    收起時、且完全沒選時顯示的字（例如「型別」）
 *  @param values   `[{ value, text? }]`
 *  @param onChange 每次勾選／取消都呼叫，參數是目前選中的值陣列
 *  @returns `{ get, set, setDisabled, clear }`
 */
export function multiDrop(host, { label, values, onChange }) {
  bindDropListeners();
  const picked = new Set();
  const disabled = new Map();   // value -> 原因字串

  host.innerHTML = '';
  const d = document.createElement('details');
  d.className = 'msdrop';

  const sum = document.createElement('summary');
  sum.innerHTML = '<span class="ms-text"></span><span class="ms-caret" aria-hidden="true">▾</span>';
  d.appendChild(sum);

  const panel = document.createElement('div');
  panel.className = 'ms-panel';
  d.appendChild(panel);

  const why = document.createElement('div');
  why.className = 'ms-why hidden';
  panel.appendChild(why);

  const boxes = new Map();
  for (const v of values) {
    const row = document.createElement('label');
    row.className = 'ms-row';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.value = v.value;
    cb.addEventListener('change', () => {
      if (cb.checked) picked.add(v.value); else picked.delete(v.value);
      paint();
      onChange([...picked]);
    });
    row.appendChild(cb);
    row.appendChild(document.createTextNode(' ' + (v.text ?? v.value)));
    panel.appendChild(row);
    boxes.set(v.value, cb);
  }
  host.appendChild(d);

  // 追蹤開闔，讓上面那個全域 listener 只需要看真的開著的那些
  autoClose(d);

  function paint() {
    const list = [...picked];
    // ⚠️ 這個區域變數以前叫 `t`，正好遮蔽 i18n 的 `t()` —— 在這個函式裡
    // 呼叫 t('...') 會變成「對一個 DOM 元素做函式呼叫」。改名 `txt`。
    const txt = sum.querySelector('.ms-text');
    // ⚠️ 收起來時**看不見選了哪些**，這是下拉本身的限制。
    // 補償的地方是條件標籤列（它把每個值逐一列出來），所以那一列
    // 在這個方案下必須顯示**全部**條件，不能只顯示「看不見的」。
    if (!list.length) txt.textContent = label;
    else if (list.length === 1) txt.textContent = list[0];
    else txt.textContent = t('common.multi.summary', { first: list[0], n: fmt.num(list.length) });
    d.classList.toggle('ms-on', list.length > 0);
    for (const [v, cb] of boxes) {
      cb.checked = picked.has(v);
      const reason = disabled.get(v);
      cb.disabled = Boolean(reason);
      cb.closest('.ms-row').classList.toggle('off', Boolean(reason));
    }
    const reasons = [...disabled.values()].filter(Boolean);
    why.textContent = reasons[0] || '';
    why.classList.toggle('hidden', reasons.length === 0);
  }

  paint();

  return {
    get: () => [...picked],
    set(list) { picked.clear(); for (const v of list) picked.add(v); paint(); },
    clear() { picked.clear(); paint(); },
    /** disabled 一定要說得出原因 —— 只是點不動的核取方塊看起來就是壞的。 */
    setDisabled(value, reason) {
      if (reason) { disabled.set(value, reason); picked.delete(value); }
      else disabled.delete(value);
      paint();
    },
    /** ⚠️ 與 `setDisabled` 是兩件事：那個是「這個**值**選不得」，
     *  這個是「整組控制項現在不適用」。值**不清空** —— 切回去要原樣還在。 */
    setOff(reason) { setDetailsOff(d, reason); },
  };
}

/** 在一段**動態產生**的 HTML 裡把佔位元素換成自製下拉。
 *
 *  抽屜、詳情面板、批次列的內容都是每次重畫就整段 `innerHTML` 換掉的 ——
 *  那些地方沒有一個「開頁時建一次」的時機，所以下拉必須跟著每次重畫重建。
 *  這支就是那個「重畫之後負責重建」的人。
 *
 *  ⚠️ **找不到佔位元素就丟例外，不是安靜跳過。** 少一個下拉的畫面看起來
 *  只是「那個欄位不見了」，而值會靜默變成 undefined —— 送出去之後才發現
 *  少改了一個欄位。這正是本專案「禁止用 fallback 掩蓋問題」要防的那種故障。
 *
 *  @param root  剛渲染好的容器（抽屜的 body、選取列…）
 *  @param specs `{ 佔位元素的 id 或 selector: singleDrop 的參數 }`
 *  @returns 同樣的鍵對到各自的握把 `{ get, set, setOptions, clear }`
 */
export function mountDrops(root, specs) {
  const out = {};
  for (const [key, cfg] of Object.entries(specs)) {
    const sel = key.startsWith('#') || key.startsWith('[') ? key : `#${key}`;
    const host = root.querySelector(sel);
    if (!host) {
      // ⚠️ 這一句**刻意不進語系檔**：它是開發者錯誤（樣板改了、這裡沒跟著改），
      //    不是使用者要讀的文案。翻譯它只會讓 key 表多一條沒有人會看到的字。
      // ⚠️ 訊息用英文：它會經由 main.js 的 boot-error 橫幅出現在**畫面上**，
      //    而畫面可能是三個語系裡的任何一個。註解維持中文，訊息不行。
      throw new Error(`mountDrops: placeholder ${sel} not found - `
        + 'the template changed but this did not; that field disappears silently.');
    }
    out[key] = singleDrop(host, cfg);
  }
  return out;
}

/** 建一個**單選**下拉 —— 原生 `<select>` 的替代品。
 *
 *  為什麼不用原生 select：它的下拉箭頭與展開後的清單是**作業系統畫的**，
 *  完全不受 M3 token 控制。同一列裡放一個原生 select 和一個 `multiDrop()`，
 *  兩者長得完全不一樣 —— 這是「不像 M3」最直接的來源。
 *
 *  與 `multiDrop()` 共用同一套視覺（`.msdrop` / `.ms-panel` / `.ms-row`）與
 *  同一個收合機制（`openDrops`），差別只在語意：radio、選完就收起。
 *
 *  @param host      容器元素（會被清空）
 *  @param label     沒選任何值時顯示的字（例如「排序」）
 *  @param values    `[{ value, text? }]`
 *  @param emptyText 「回到不限」那一項的字（例如「全部平台」）。給了就會自動
 *                   插在清單最前面。
 *                   ⚠️ **不給就沒有回頭路** —— 原生 `<select>` 是靠一個
 *                   `<option value="">` 提供這件事的，換成自製下拉時很容易
 *                   整個漏掉：選了某個平台之後永遠切不回「全部」。
 *                   只有「本來就一定有值」的控制項（排序鍵）才該省略。
 *  @param value     初始值
 *  @param ariaLabel 沒有可見標籤時給的無障礙名稱（例如批次列那三個）。
 *                   省略就不加 —— 旁邊已經有 `.ms-label` 的不需要。
 *  @param onChange  每次改變都呼叫，參數是目前的值（字串）
 *  @returns `{ get, set, setOptions, clear }`
 */
export function singleDrop(host, { label, values, value = '', emptyText, ariaLabel, onChange }) {
  bindDropListeners();
  let current = value;
  // radio 要分組，否則同一頁的多個下拉會互相搶選取
  const group = `sd-${Math.random().toString(36).slice(2, 9)}`;

  host.innerHTML = '';
  const d = document.createElement('details');
  d.className = 'msdrop';

  const sum = document.createElement('summary');
  sum.innerHTML = '<span class="ms-text"></span><span class="ms-caret" aria-hidden="true">▾</span>';
  d.appendChild(sum);

  const panel = document.createElement('div');
  panel.className = 'ms-panel';
  d.appendChild(panel);

  let opts = [];
  const radios = new Map();

  function build(list) {
    // 「不限」永遠排第一，而且每次重建（setOptions）都要補回去。
    opts = emptyText ? [{ value: '', text: emptyText }, ...list] : list;
    radios.clear();
    panel.innerHTML = '';
    for (const v of opts) {
      const row = document.createElement('label');
      row.className = 'ms-row';
      const rb = document.createElement('input');
      rb.type = 'radio';
      rb.name = group;
      rb.value = v.value;
      rb.addEventListener('change', () => {
        if (!rb.checked) return;
        current = v.value;
        paint();
        d.open = false;          // 單選：選完就收起，不必再點一次
        onChange(current);
      });
      row.appendChild(rb);
      row.appendChild(document.createTextNode(' ' + (v.text ?? v.value)));
      panel.appendChild(row);
      radios.set(v.value, rb);
    }
  }

  build(values);
  host.appendChild(d);

  autoClose(d);

  function paint() {
    const hit = opts.find((o) => o.value === current);
    const txt = sum.querySelector('.ms-text');   // 見 multiDrop：不要叫 t
    // 選了「不限」（value: ''）時顯示 label 本身，與 multiDrop 的空狀態一致
    txt.textContent = current && hit ? (hit.text ?? hit.value) : label;
    // filter chip 的填底語意是「從中性狀態收窄了」。沒有中性狀態的控制項
    // （排序鍵：任何一個值都同樣正當）不該永遠亮著 —— 那會讓使用者以為
    // 自己套了一個篩選條件。
    d.classList.toggle('ms-on', Boolean(current) && Boolean(emptyText));
    for (const [v, rb] of radios) rb.checked = v === current;
    // ⚠️ aria-label 會**取代**可見文字成為無障礙名稱，所以要把目前值也帶上 ——
    // 只寫「批次分級」的話，讀屏使用者聽不到現在選的是什麼。
    if (ariaLabel) {
      sum.setAttribute('aria-label',
        t('common.aria.value', { label: ariaLabel, value: txt.textContent }));
    }
  }

  paint();

  return {
    get: () => current,
    set(v) { current = v ?? ''; paint(); },
    clear() { current = ''; paint(); },
    /** 選項是後端載進來的時候用（例如 creator 清單）。保留目前值，
     *  但如果它已經不在新清單裡就退回「不限」—— 留一個選不到的值在那裡，
     *  畫面會顯示一個篩選條件而清單卻是空的。 */
    setOptions(list) {
      build(list);
      if (current && !opts.some((o) => o.value === current)) current = '';
      paint();
    },
    /** 整組不適用（例如創作者模式下的排序鍵）。值不清空。 */
    setOff(reason) { setDetailsOff(d, reason); },
  };
}
