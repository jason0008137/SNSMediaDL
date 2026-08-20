// sortctl.js 的**白名單**與方向語句。走 testkit，node 與瀏覽器都跑得動
// （公司機沒有 node，見 skill `/office-dev`）。
//
// 為什麼是這兩件事：
//   · `parseStoredSort` 是整個排序控制唯一會把**外部字串**（localStorage，
//     使用者改得到）送進查詢參數的路徑。原生 `<select>` 吃到不存在的值會
//     自己變成空字串把錯誤吞掉，自製下拉沒有那層保護 —— 認不得的值會原樣
//     送給後端，變成一個不報錯也不生效的壞查詢。
//   · 方向語句是兩可箭頭的唯一語意來源（按鈕上只有 ↑ / ↓）。
//
// **不能取代實機驗收**：下拉的建立、換鍵時方向鈕真的翻、`order` 真的進
// URLSearchParams —— 那些要 DOM，在 GUI 上驗。

import { test, assertEqual, run, loadText } from './testkit.js';
import { initI18n } from './i18n.js';
import { parseStoredSort, dirSentence } from './sortctl.js';

// `dirSentence` 現在走 i18n，所以要先有字典。**用真的語系檔**不是假資料 ——
// 這樣連「那句模板的佔位符有沒有寫對」都一起驗到了。
await initI18n('zh-Hant', JSON.parse(await loadText('../i18n/zh-Hant.json', import.meta.url)));

// 帳號頁的設定（線框 UI_帳號頁篩選與排序 第 3-1 節）
const ACCT = {
  keys: ['favorite', 'stars', 'name', 'last_fetch', 'id'],
  defaultKey: 'favorite',
  defaultOrder: { favorite: 'desc', stars: 'desc', name: 'asc', last_fetch: 'asc', id: 'asc' },
};

// 媒體頁的設定（有 legacy 別名）
const MEDIA = {
  keys: ['added', 'posted', 'stars'],
  defaultKey: 'added',
  defaultOrder: { added: 'desc', posted: 'desc', stars: 'desc' },
  legacy: { newest: 'added:desc', oldest: 'added:asc' },
};

test('現行格式 key:order 原樣認得', () => {
  assertEqual(parseStoredSort('stars:asc', ACCT), { key: 'stars', order: 'asc' });
});

test('裸鍵（帳號頁舊格式）→ 該鍵的預設方向', () => {
  // 這是舊行為：以前存的就是裸鍵，方向烘在選項文字裡不可改。
  assertEqual(parseStoredSort('name', ACCT), { key: 'name', order: 'asc' });
  assertEqual(parseStoredSort('last_fetch', ACCT), { key: 'last_fetch', order: 'asc' });
  assertEqual(parseStoredSort('stars', ACCT), { key: 'stars', order: 'desc' });
});

test('legacy 別名（媒體頁分段控制時代）', () => {
  assertEqual(parseStoredSort('newest', MEDIA), { key: 'added', order: 'desc' });
  assertEqual(parseStoredSort('oldest', MEDIA), { key: 'added', order: 'asc' });
});

test('不存在的鍵退回預設，不外洩', () => {
  assertEqual(parseStoredSort('banana', ACCT), { key: 'favorite', order: 'desc' });
  assertEqual(parseStoredSort('banana:asc', ACCT), { key: 'favorite', order: 'desc' });
});

test('不存在的方向退回該鍵的預設', () => {
  assertEqual(parseStoredSort('name:sideways', ACCT), { key: 'name', order: 'asc' });
  assertEqual(parseStoredSort('stars:SIDEWAYS', ACCT), { key: 'stars', order: 'desc' });
});

test('空值 / null / undefined 都退回預設', () => {
  for (const raw of ['', null, undefined]) {
    assertEqual(parseStoredSort(raw, ACCT), { key: 'favorite', order: 'desc' },
                `raw=${String(raw)}`);
  }
});

test('注入字串不會被原樣帶出去', () => {
  // 使用者在 devtools 裡塞什麼都一樣：出口只可能是白名單裡的值。
  const evil = "id;DROP TABLE accounts--:desc";
  assertEqual(parseStoredSort(evil, ACCT), { key: 'favorite', order: 'desc' });
});

// ⚠️ `words` 進去的是 **key** 不是文字。各頁的 `DIR_WORDS` 是模組載入時建好的
// 常數，那時字典還沒載完 —— 存文字的話整批會變成 `⟦key⟧`（實際發生過：
// 媒體頁的排序下拉一度顯示 `media.sort.posted` 這個 key 本身）。
test('方向語句講的是「目前」與「按下去會變成」', () => {
  const words = { desc: 'dir.newold', asc: 'dir.oldnew' };
  assertEqual(dirSentence(words, 'desc'), '目前：新→舊。按一下改成舊→新');
  assertEqual(dirSentence(words, 'asc'), '目前：舊→新。按一下改成新→舊');
});

await run('sortctl');
