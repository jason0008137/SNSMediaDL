// 三個語系檔的一致性。走 testkit，node 與瀏覽器都跑得動。
//
// 為什麼這幾條非有不可：三份 JSON 是**手動維護**的平行結構，而漂移的症狀
// 全部是「靜默壞掉」——
//   · 少一個 key   → 那個語系畫面上出現 ⟦key⟧（會被看到，但只有切到那個語系才會）
//   · 值是空字串   → 畫面上一片空白，**看起來像版面 bug 而不是翻譯漏了**
//   · 少一個 {n}   → 數字整個不見，而且不報錯
// 前兩個靠眼睛還有機會抓到，第三個沒有測試就是抓不到。

import { test, assert, assertEqual, run } from './testkit.js';
import { loadText } from './testkit.js';

const LANGS = ['en', 'zh-Hant', 'ja'];

const dicts = {};
for (const l of LANGS) {
  dicts[l] = JSON.parse(await loadText(`../i18n/${l}.json`, import.meta.url));
}

const keys = (l) => Object.keys(dicts[l]).sort();
/** `{n}` 這種佔位符。漏掉的話畫面上那個數字直接消失。 */
const params = (s) => [...String(s).matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();

test('三個語系檔都載得到，而且不是空的', () => {
  for (const l of LANGS) {
    assert(Object.keys(dicts[l]).length > 0, `${l}.json 是空的`);
  }
});

test('key 集合完全相同', () => {
  const base = keys('en');
  for (const l of LANGS.slice(1)) {
    const missing = base.filter((k) => !(k in dicts[l]));
    const extra = keys(l).filter((k) => !(k in dicts.en));
    assertEqual(missing, [], `${l}.json 少了 key`);
    assertEqual(extra, [], `${l}.json 多了 en.json 沒有的 key`);
  }
});

test('沒有空字串值', () => {
  // 空字串在畫面上與「這裡本來就沒有東西」分不出來 —— 那是最難發現的漏翻。
  for (const l of LANGS) {
    const empty = Object.entries(dicts[l])
      .filter(([, v]) => typeof v !== 'string' || v.trim() === '')
      .map(([k]) => k);
    assertEqual(empty, [], `${l}.json 有空值`);
  }
});

test('每個 key 的插值佔位符三份一致', () => {
  for (const k of keys('en')) {
    const want = params(dicts.en[k]);
    for (const l of LANGS.slice(1)) {
      assertEqual(params(dicts[l][k]), want, `${l}.json 的 ${k} 佔位符對不上`);
    }
  }
});

test('語系檔裡不准有 HTML 標記', () => {
  // applyI18n 一律走 textContent / dataset，不走 innerHTML。翻譯檔是最容易被
  // 隨手編輯的東西，讓它能塞標記等於開一個注入點。要粗體就拆成兩個 key。
  for (const l of LANGS) {
    const tagged = Object.entries(dicts[l])
      // ⚠️ 只認**真的 HTML 標籤名**。`<PHPSESSID>` 是 config.toml 範例裡的
      //    佔位符，不是標記 —— 一竿子打翻的規則會逼人把正確的文案改壞。
      .filter(([, v]) => /<\/?(a|b|i|em|strong|span|div|p|br|button|img|script|style|h[1-6])\b/i.test(v))
      .map(([k]) => k);
    assertEqual(tagged, [], `${l}.json 有 HTML 標記`);
  }
});

test('複數 key 一定有 .other', () => {
  // tn() 找不到該語系的複數類別時會退回 .other —— 那個退回只有在 .other
  // 必定存在時才成立。
  const bases = new Set(keys('en')
    .filter((k) => /\.(zero|one|two|few|many|other)$/.test(k))
    .map((k) => k.replace(/\.\w+$/, '')));
  for (const b of bases) {
    for (const l of LANGS) {
      assert(`${b}.other` in dicts[l], `${l}.json 缺 ${b}.other`);
    }
  }
});

await run('i18n');
