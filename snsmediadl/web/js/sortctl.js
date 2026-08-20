// 排序控制 —— 鍵下拉 + 獨立方向鈕 + 註記行 + 偏好存檔。媒體頁與帳號頁共用。
//
// 方向是**看得見的獨立按鈕**，不是「在鍵上再按一次」。後者的指意只寫得進
// hover 提示，鍵盤與觸控使用者永遠看不到。
//
// ⚠️ 白名單驗證是**非做不可**的，不是順手做的防禦。原生 `<select>` 吃到
// 不存在的值會自己變成空字串（錯誤被吞掉但不會外洩）；自製下拉沒有那層
// 保護 —— 認不得的值會原樣送給後端，變成一個靜默的壞查詢。

import { singleDrop } from './dom.js';
import { t } from './i18n.js';

const ORDERS = ['desc', 'asc'];

/** 某個鍵的方向語彙。`{ desc, asc }` 兩個 **key**，句子由這裡組。
 *  各頁只提供詞（`dir.newold`），不重複寫「目前：…。按一下改成…」那個模板。
 *
 *  ⚠️ 進來的是 key 不是文字。各頁的 `DIR_WORDS` 是**模組載入時**建好的常數，
 *  那時字典還沒載完 —— 存文字會整批變成 `⟦key⟧`。t() 在這裡（要用的時候）做。 */
export const dirSentence = (words, order) =>
  t('sort.dir.sentence', {
    now: t(words[order]),
    next: t(words[order === 'desc' ? 'asc' : 'desc']),
  });

/** 存檔字串 → `{ key, order }`。**白名單在這裡，而且是純函式** ——
 *  拆出來不是為了好看：它是整個排序控制唯一會把外部字串（localStorage，
 *  使用者改得到）送進查詢參數的路徑，必須能在沒有 DOM 的地方直接測。
 *
 *  認得三種輸入：
 *    · `key:order`（現行格式）
 *    · 裸鍵（帳號頁的舊格式）—— 方向用該鍵的預設值，那正是舊行為
 *    · `legacy` 表裡的別名（媒體頁分段控制時代的 `newest` / `oldest`）
 *  其餘一律退回預設。**絕不原樣往外送。**
 *
 *  ⚠️ 一筆存檔是**不可分割的**：鍵不合法就連方向一起丟掉，不是「鍵退回預設、
 *  方向照用」。`banana:asc` 那個 `asc` 是屬於 banana 的，套到預設鍵上會得到
 *  一個沒有人選過的順序（例如「我的最愛」變成最愛排在後面），而使用者完全
 *  不知道自己看到的是半筆爛資料拼出來的狀態。 */
export function parseStoredSort(raw, { keys, defaultKey, defaultOrder, legacy = {} }) {
  const [k, o] = String(legacy[raw] ?? raw ?? '').split(':');
  if (!keys.includes(k)) return { key: defaultKey, order: defaultOrder[defaultKey] };
  return { key: k, order: ORDERS.includes(o) ? o : defaultOrder[k] };
}

/** 建一組排序控制。
 *
 *  @param keyHost     排序鍵下拉的佔位元素
 *  @param dirBtn      方向鈕（`.dirbtn`）
 *  @param noteEl      註記行（`.sort-note`，**永遠佔位**）
 *  @param keys        鍵的值域，**同時是白名單**
 *  @param labels      `{ key: 顯示字 }`。⚠️ 顯示字**不含方向**
 *  @param defaultOrder `{ key: 'desc'|'asc' }`。換鍵時套用它，不沿用上一個鍵的
 *  @param dirWords    `{ key: { desc, asc } }` 方向語彙
 *  @param notes       `{ key: 字串 }`，或 `{ key: { desc, asc } }`（方向會改變
 *                     語意的鍵，例如帳號頁的 `last_fetch`）
 *  @param storageKey  localStorage 的鍵名
 *  @param legacy      `{ 舊字串: 'key:order' }`。**裸鍵不必列** —— 它天然會被
 *                     解析成 `key` + 預設方向
 *  @param defaultKey  認不得存檔時用的鍵。預設是 `keys[0]`
 *  @param label       下拉收起時的字（沒有可見標籤時的後備）
 *  @param onChange    `(reason)` —— `'key'` 或 `'dir'`。**重查由呼叫端做**
 *  @returns `{ key, order, drop, paint, restore }`
 */
export function makeSortControl({
  keyHost, dirBtn, noteEl, keys, labels, defaultOrder, dirWords, notes = {},
  storageKey, legacy = {}, defaultKey = keys[0], label, onChange,
}) {
  // 同 overlay.js：預設值不寫在參數列，那會在模組載入時求值。
  // ⚠️ 這一句仍然在**模組載入時**跑（`makeSortControl` 是各頁的頂層呼叫），
  // 所以 `label` 只能是後備 —— 真正顯示的字由 `restore()` 之後的 `relabel()`
  // 補上。排序永遠有值，`singleDrop` 的 label 其實顯示不到，留著只是為了
  // 「沒有值時也不是空白」。
  label = label ?? 'sort.label';
  const key = () => drop.get();
  const order = () => (dirBtn.dataset.order === 'asc' ? 'asc' : 'desc');

  function paint() {
    const k = key();
    const o = order();
    dirBtn.textContent = o === 'desc' ? '↓' : '↑';
    const why = dirSentence(dirWords[k], o);
    dirBtn.setAttribute('aria-label', t('sort.dir.aria', { why }));
    dirBtn.dataset.tip = why;
    const note = notes[k];
    // 註記可以隨方向變（`last_fetch` 升冪時 NULL 排最前，降冪排最後 ——
    // 那是兩句不同的話，不是同一句）。
    // ⚠️ 表裡放的是 key。空字串 = 這個鍵沒有要講的話，那時**不可以** t()——
    // `t('')` 會印出 `⟦⟧` 並在 console 報一個不存在的問題。
    const noteKey = (note && typeof note === 'object' ? note[o] : note) || '';
    noteEl.textContent = noteKey ? t(noteKey) : '';
  }

  function save() {
    localStorage.setItem(storageKey, `${key()}:${order()}`);
  }

  /** 選項的顯示字。**每次都重新 t()** —— `labels` 裡放的是 key。 */
  const options = () => keys.map((v) => ({ value: v, text: t(labels[v]) }));

  const drop = singleDrop(keyHost, {
    label,
    // 建構時字典還沒載完（各頁在模組頂層呼叫 makeSortControl），
    // 所以這裡先放 key，`restore()` 再用 `setOptions(options())` 換成真正的字。
    values: keys.map((v) => ({ value: v, text: labels[v] })),
    value: defaultKey,
    // ⚠️ 排序**不給 emptyText**：它一定有值，「不排序」不是一個選項。
    // 給了會多出一個選了等於沒選的空項。
    onChange: () => {
      // 換鍵時套用**該鍵的預設方向**，不沿用上一個鍵的。
      // 「評分 · 低→高」不是任何人想要的第一眼。
      dirBtn.dataset.order = defaultOrder[key()] || 'desc';
      save();
      paint();
      onChange('key');
    },
  });

  dirBtn.addEventListener('click', () => {
    // ⚠️ 不能只靠 dom.js 的 guard：那個 listener 是整組被關掉時才掛上去的，
    // 掛的時候這一個早就在了 —— 而同一個元素上的 listener 依**註冊順序**觸發，
    // capture 旗標救不了。所以這裡自己再問一次。
    if (dirBtn.getAttribute('aria-disabled') === 'true') return;
    dirBtn.dataset.order = order() === 'desc' ? 'asc' : 'desc';
    save();
    paint();
    onChange('dir');
  });

  /** 還原偏好。**白名單驗證** —— 認不得就用預設。
   *
   *  這裡有前科：分段控制那版存的是 `added:desc`，回朔後的舊 `<select>`
   *  吃到會變成空值，然後送出 `sort=`（一個靜默的空條件，不會報錯也不會
   *  生效）。所以絕不直接把 localStorage 的字串塞進控制項。 */
  function restore() {
    const { key: okKey, order: okOrder } = parseStoredSort(
      localStorage.getItem(storageKey), { keys, defaultKey, defaultOrder, legacy },
    );
    // ⚠️ **這一行是排序鍵下拉唯一會被翻譯的時機。** 兩頁都在模組頂層建
    // 控制項（`$('aSortKey')` 那時就要在），而 `initI18n()` 是在 `main.js`
    // 的 `init()` 裡才 await 完的 —— 建構當下 t() 只會拿到 `⟦key⟧`。
    // `restore()` 由兩頁在 i18n 就緒之後呼叫，所以字在這裡補。
    drop.setOptions(options());
    drop.set(okKey);
    dirBtn.dataset.order = okOrder;
    paint();
  }

  return { key, order, drop, paint, restore };
}
