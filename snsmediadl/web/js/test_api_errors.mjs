// 後端錯誤怎麼變成畫面上那一句話。走 testkit，node 與瀏覽器都跑得動
// （公司機沒有 node，見 skill `/office-dev`）。
//
// 為什麼是這一件事：`errorText()` 是**三十幾個顯示點共用的唯一出口**。
// 它退回錯了，症狀不是報錯 —— 是某一種錯誤下畫面顯示一片空白，或是一句
// 「發生未知錯誤」把後端唯一講清楚的線索吃掉。兩種都沒有人回報得出來。
//
// **不能取代實機驗收**：真的送出請求、真的把字寫進 `.card-msg` —— 那些要
// DOM 與 backend，在 GUI 上驗。

import { test, assert, assertEqual, run, loadText } from './testkit.js';
import { initI18n } from './i18n.js';
import { errorText } from './api.js';

// 用**真的**語系檔不是假資料 —— 這樣連「err.* 的 key 有沒有真的存在」
// 都一起驗到了。
await initI18n('ja', JSON.parse(await loadText('../i18n/ja.json', import.meta.url)));

test('認得的 code 走語系檔，不是後端那句英文', () => {
  const got = errorText('media.not_found', 'No such media.', 404);
  assertEqual(got, 'このメディアが見つかりません。');
});

test('不認得的 code 顯示後端的英文原文', () => {
  // ⚠️ 這是**刻意**的：參數驗證類的 detail 帶著具體的值（可用的排序鍵有哪些），
  // 翻成一句通則等於把唯一有用的資訊丟掉。
  const detail = 'sort must be one of added / posted / stars.';
  assertEqual(errorText('query.bad_sort', detail, 422), detail);
});

test('完全沒有 code 也顯示 detail', () => {
  assertEqual(errorText(null, 'Not Found', 404), 'Not Found');
});

test('code 與 detail 都沒有時才退到狀態碼', () => {
  const got = errorText(null, '', 502);
  assert(got.includes('502'), `狀態碼要出現在訊息裡，got ${got}`);
});

test('永遠不吐「未知錯誤」那種吃掉訊息的字', () => {
  // 這一條擋的是一個很容易被「順手加上」的退化：某人補一個 catch-all
  // 預設值，於是每一種沒分類的錯誤都變成同一句廢話。
  for (const [code, detail] of [
    ['query.bad_sort', 'sort must be one of added / posted.'],
    [null, 'Not Found'],
    ['nope.nope', 'Something specific happened.'],
  ]) {
    assertEqual(errorText(code, detail, 400), detail);
  }
});

test('缺 key 的查詢不會在 console 灌假的錯誤', () => {
  // `t()` 缺 key 會 console.error —— 那對畫面上的字是對的，但錯誤 code 是
  // **開放集合**，查不到是預期內的。用 `t()` 去試會把真正的缺 key 淹掉。
  const errors = [];
  const real = console.error;
  console.error = (...a) => errors.push(a.join(' '));
  try {
    errorText('nope.not.a.real.code', 'detail', 400);
  } finally {
    console.error = real;
  }
  assertEqual(errors.length, 0, `不該有 console.error，got ${JSON.stringify(errors)}`);
});

await run('api errors');
