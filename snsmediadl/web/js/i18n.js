// 三語系（en 預設 / zh-Hant / ja）。**沒有建置步驟、沒有函式庫。**
//
// 這個專案是純 ES modules 由 FastAPI 靜態送出，沒有 npm 也沒有打包器
// （公司機連 node 都沒有）。為了 i18n 引進一整條工具鏈，代價遠大於
// 「一個 fetch + 一張表」。
//
// ⚠️ **這支是葉子模組，什麼都不 import。** 幾乎每一個模組都會用到它，
// 一旦它反過來 import 別人（例如為了 setLang 而拉 queue.js），就會出現
// 循環相依 —— 那正是拆模組前 main → problems → main 的老問題。
// 「改語言」的動作放在設定頁，不放這裡。

/** 值域。加語系要動這裡、加一個 JSON、並更新 test_i18n 的清單。 */
export const LANGS = ['en', 'zh-Hant', 'ja'];

/** 選單上顯示的名字。**用該語言自己的名字**，不是「英文 / 中文 / 日文」——
 *  看不懂目前介面語言的人，正是最需要找到自己語言的那個人。 */
/* i18n-exempt: 各語言自己的名字，在任何語系下都一樣 —— 見上面那段說明 */
export const LANG_NAMES = { en: 'English', 'zh-Hant': '正體中文', ja: '日本語' };

let lang = 'en';
let dict = {};

export const currentLang = () => lang;

/** 載入一個語系。**語言從哪裡來不由這支決定** —— 呼叫端（main.js）先向
 *  `/api/settings` 拿 `language`，這裡只負責載入。
 *
 *  ⚠️ 只載入選中的那一個。三份都載等於為了「之後可能會切」付出每次開頁
 *  三倍的傳輸，而切語言是極低頻動作（而且會整頁重載）。 */
export async function initI18n(l, preloaded) {
  lang = LANGS.includes(l) ? l : 'en';
  // 已經有字典就直接用。node 裡沒有 fetch 也沒有 document，測試只能走這條；
  // 但這不是測試專用的後門 —— 手上已經有資料還再抓一次本來就沒道理。
  if (preloaded) {
    dict = preloaded;
    if (typeof document !== 'undefined') document.documentElement.lang = lang;
    return lang;
  }
  const res = await fetch(`i18n/${lang}.json`);
  if (!res.ok) {
    // ⚠️ **不要靜默退回英文。** 那會讓「ja.json 打錯字了」看起來像
    // 「這個功能只是還沒翻譯」，而沒有任何人會發現。
    // ⚠️ 訊息用英文：它會經由 main.js 的 boot-error 橫幅出現在**畫面上**，
    //    而那個時候語系檔正好就是載不到的那個東西 —— t() 是不可能的。
    throw new Error(`Cannot load i18n/${lang}.json (HTTP ${res.status})`);
  }
  dict = await res.json();
  document.documentElement.lang = lang;
  return lang;
}

/** 取一個字串。`{name}` 樣式的插值。
 *
 *  ⚠️ **缺 key 不退回中文、也不回 key 名裝作沒事**，而是印 `⟦key⟧` 並
 *  `console.error`。靜默退回的下場是「英文版偷偷混著中文」，而且沒有任何
 *  錯誤訊息 —— 那正是根因原則禁止的那種兜底。
 *  `⟦⟧` 用的是不會出現在正常文案裡的字符，掃畫面一眼就找得到。
 *
 *  @param key    扁平 key，例 `media.sort.label`
 *  @param params `{ n: 12 }` → 把 `{n}` 換成 12
 */
export function t(key, params) {
  let s = dict[key];
  if (s === undefined) {
    console.error(`[i18n] missing key: ${key} (${lang})`);
    return `⟦${key}⟧`;
  }
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      s = s.replaceAll(`{${k}}`, v);
    }
  }
  return s;
}

/** 這個語系有沒有這個 key。
 *
 *  ⚠️ 存在的理由只有一個：**後端的錯誤 code 是開放集合。** 它可以回一個
 *  前端還沒翻譯的 code，那時該退回顯示後端的英文原文 —— 那是預期內的路徑，
 *  不是缺漏。用 `t()` 去試會在 console 灌一堆假的「缺 key」錯誤，把真正的
 *  缺 key 淹掉。
 *
 *  ⚠️ 這**不是**給一般文案用的。畫面上的字缺 key 就該印 `⟦key⟧` 並報錯 ——
 *  用 `hasKey()` 去兜一個預設值正是根因原則禁止的那種掩蓋。
 */
export const hasKey = (key) => dict[key] !== undefined;

/** 帶數量的字串。英文有單複數，中日文沒有。
 *
 *  ⚠️ **不要用 `n > 1 ? 's' : ''` 那種土砲。** 它在英文以外都是錯的
 *  （俄文有 3 種、阿拉伯文有 6 種），而且加第四個語系那天沒有人會想到要改它。
 *  `Intl.PluralRules` 是瀏覽器內建的，零成本。
 *
 *  key 寫成 `accounts.count.one` / `.other`；中日文兩個 key 填一樣的字。
 */
const pluralRules = new Map();

export function tn(keyBase, n, params) {
  if (!pluralRules.has(lang)) pluralRules.set(lang, new Intl.PluralRules(lang));
  const cat = pluralRules.get(lang).select(n);
  const key = `${keyBase}.${cat}`;
  // 該語系用不到的複數類別（中日文只有 other）沒必要每個 key 都填一份，
  // 找不到就退回 .other —— **這不是掩蓋**：`.other` 在每個語系都必定存在，
  // 而 key 集合比對測試會保證它在。
  const s = dict[key] !== undefined ? key : `${keyBase}.other`;
  return t(s, { n: fmt.num(n), ...params });
}

/** 掃 DOM 把字填進去。動態產生的 DOM 也要呼叫（傳它的容器）。
 *
 *  四個掛法，對應四個「字會被讀到的地方」：
 *    `data-i18n`      → textContent
 *    `data-i18n-tip`  → dataset.tip（自製 tooltip）
 *    `data-i18n-ph`   → placeholder
 *    `data-i18n-aria` → aria-label
 *
 *  ⚠️ 沒有 `data-i18n-html`。往 innerHTML 塞語系檔的內容等於讓翻譯檔
 *  可以注入標記，而翻譯檔是最容易被隨手編輯的東西。要粗體就拆成兩個 key，
 *  或在樣板側包 `<b>`。
 */
export function applyI18n(root = document) {
  for (const el of root.querySelectorAll('[data-i18n]')) {
    el.textContent = t(el.dataset.i18n);
  }
  for (const el of root.querySelectorAll('[data-i18n-tip]')) {
    el.dataset.tip = t(el.dataset.i18nTip);
  }
  for (const el of root.querySelectorAll('[data-i18n-ph]')) {
    el.placeholder = t(el.dataset.i18nPh);
  }
  for (const el of root.querySelectorAll('[data-i18n-aria]')) {
    el.setAttribute('aria-label', t(el.dataset.i18nAria));
  }
}

// ── 格式化 ─────────────────────────────────────────────
//
// ⚠️ **全站禁止不帶參數的 `toLocaleString()`。** 不帶參數用的是**瀏覽器**的
// 語系，不是使用者在這個 App 選的語系。選了英文卻看到日文格式的日期，
// 而且那是 locale 一開放就必然出現的錯，不是偶發。

const cache = new Map();
const memo = (kind, make) => {
  const k = `${kind}:${lang}`;
  if (!cache.has(k)) cache.set(k, make());
  return cache.get(k);
};

export const fmt = {
  /** 千分位。取代所有 `Number.prototype.toLocaleString()`。 */
  num: (n) => memo('num', () => new Intl.NumberFormat(lang)).format(n ?? 0),

  /** 年月日。`null` 回破折號 —— **不回空字串**，空白與「沒有值」分不出來。 */
  date(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '—';
    return memo('date', () => new Intl.DateTimeFormat(lang, {
      year: 'numeric', month: '2-digit', day: '2-digit',
    })).format(d);
  },

  /** 「3 天前」。給卡片上的「多久沒檢查」用。 */
  rel(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '—';
    const days = Math.round((d.getTime() - Date.now()) / 86400000);
    return memo('rel', () => new Intl.RelativeTimeFormat(lang, { numeric: 'auto' }))
      .format(days, 'day');
  },
};
