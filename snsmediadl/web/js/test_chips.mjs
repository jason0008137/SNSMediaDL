// chips.js（生效條件標籤列）。走 testkit，node 與瀏覽器都跑得動。
//
// 這一份是媒體頁與帳號頁**共用**的模組，所以它壞掉是兩頁一起壞。
// 覆蓋三件事：渲染出來的清除識別對不對、`__all__`、以及沒有條件時整條隱藏。
//
// ⚠️ 瀏覽器模式用真的 DOM 與真的 click 事件（事件委派是這支模組的核心，
// 用假物件測等於沒測到）；node 模式退回極簡假 DOM —— 家裡的迴路照舊。

import { test, assert, assertEqual, run } from './testkit.js';
import { makeChipBar } from './chips.js';

const REAL_DOM = typeof document !== 'undefined';

/** 一個「夠 chips.js 用」的容器。真實 DOM 有就用真的。 */
function makeHost() {
  if (REAL_DOM) return document.createElement('div');
  let handler = null;
  return {
    innerHTML: '',
    _classes: new Set(),
    classList: {
      toggle: (c, on) => { if (on) hostClasses.add(c); else hostClasses.delete(c); },
      contains: (c) => hostClasses.has(c),
    },
    addEventListener: (_t, fn) => { handler = fn; },
    _fire: (clearValue) => handler({
      target: { closest: () => ({ dataset: { clear: clearValue } }) },
    }),
  };
}
// node 假物件的 classList 需要一個外部集合（物件字面值裡取不到 this）
const hostClasses = new Set();

/** 按下某個 `data-clear` 的按鈕。 */
function clickClear(host, value) {
  if (!REAL_DOM) { host._fire(value); return; }
  host.querySelector(`[data-clear="${value}"]`).dispatchEvent(
    new MouseEvent('click', { bubbles: true }));
}

const hidden = (host) => (REAL_DOM ? host.classList.contains('hidden') : hostClasses.has('hidden'));

function setup(conds) {
  hostClasses.clear();
  const host = makeHost();
  const cleared = [];
  const bar = makeChipBar({ host, sources: () => conds, onClear: (w) => cleared.push(w) });
  bar.render();
  return { host, cleared };
}

test('沒有條件時整條隱藏，而且不留舊內容', () => {
  const { host } = setup([{ kind: 'fav', label: '只看', value: '♥' }]);
  assert(!hidden(host), '有條件時不該隱藏');
  hostClasses.clear();
  const empty = setup([]);
  assert(hidden(empty.host), '沒有條件時要隱藏');
  assertEqual(empty.host.innerHTML, '', '隱藏時要清空 —— 留著舊標籤，下次顯示會閃一下舊條件');
});

test('多選條件的清除識別用下拉的 id，不是 kind', () => {
  // 一頁上可能有好幾組多選，kind 全是 'multi' —— 用 kind 會清錯組。
  const { host } = setup([{ kind: 'multi', id: 'aFetchStatus', label: '擷取結果', value: '失敗' }]);
  assert(host.innerHTML.includes('data-clear="aFetchStatus"'), host.innerHTML);
});

test('沒有 id 的條件（帳號、creator、搜尋）用 kind', () => {
  const { host } = setup([
    { kind: 'account', label: '帳號', value: 'someone' },
    { kind: 'search', label: '搜尋', value: 'abc' },
  ]);
  assert(host.innerHTML.includes('data-clear="account"'), host.innerHTML);
  assert(host.innerHTML.includes('data-clear="search"'), host.innerHTML);
});

test('每個標籤都有自己的 ×，另外有一顆「全部清除」', () => {
  const { host } = setup([
    { kind: 'single', id: 'aPlatform', label: '平台', value: 'pixiv' },
    { kind: 'fav', label: '只看', value: '♥' },
  ]);
  const n = (host.innerHTML.match(/data-clear=/g) || []).length;
  assertEqual(n, 3, '兩個標籤 + 一顆全部清除');
  assert(host.innerHTML.includes('data-clear="__all__"'));
});

test('按 × 把該欄位的識別交回呼叫端', () => {
  const { host, cleared } = setup([
    { kind: 'multi', id: 'aStars', label: '評分', value: '★★★' },
  ]);
  clickClear(host, 'aStars');
  assertEqual(cleared, ['aStars']);
});

test('按「全部清除」交回 __all__', () => {
  const { host, cleared } = setup([{ kind: 'fav', label: '只看', value: '♥' }]);
  clickClear(host, '__all__');
  assertEqual(cleared, ['__all__']);
});

test('值裡的 HTML 被跳脫，不會變成標記', () => {
  // 搜尋字串是使用者直接打進去的，它會原樣進到這一列。
  const { host } = setup([{ kind: 'search', label: '搜尋', value: '<img src=x onerror=1>' }]);
  assert(!host.innerHTML.includes('<img'), '使用者輸入被當成標記塞進 DOM 了');
  assert(host.innerHTML.includes('&lt;img'), host.innerHTML);
});

await run('chips');
