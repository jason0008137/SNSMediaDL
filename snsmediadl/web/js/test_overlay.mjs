// overlay.js 的**關閉堆疊**：Esc 一次關一層、capture 階段、重複 release 安全。
// 用法: node snsmediadl/web/js/test_overlay.mjs
//
// 為什麼需要這支：這裡修掉的 bug 是「Esc 要按好幾次才關得掉詳情面板」，
// 根因是監聽掛在**冒泡**階段而 `<video>` 的原生控制列先攔走了事件。
// 那種東西在瀏覽器外看不出來，但「監聽是不是掛在 capture」「堆疊順序對不對」
// 可以在這裡釘住 —— 它們正是回歸時最先壞掉的兩件事。
//
// **不能取代實機驗收**：真的 <video> 焦點行為、CSS、焦點還原都不在這裡。

import { readFileSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));

// ── 極簡假 DOM ───────────────────────────────────────
// 只做 overlay.js 真的用到的事。

const listeners = [];        // { type, fn, capture }

function makeEl(tag = 'div') {
  const el = {
    tagName: tag.toUpperCase(),
    children: [],
    className: '',
    _html: '',
    classList: {
      _set: new Set(),
      add(c) { this._set.add(c); },
      remove(c) { this._set.delete(c); },
      contains(c) { return this._set.has(c); },
    },
    set innerHTML(v) { this._html = v; },
    get innerHTML() { return this._html; },
    appendChild(child) { this.children.push(child); return child; },
    remove() { removed.push(this); },
    addEventListener() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    focus() {},
  };
  return el;
}

const removed = [];
const overlayRoot = makeEl();

globalThis.document = {
  fullscreenElement: null,
  activeElement: null,
  body: makeEl('body'),
  getElementById: (id) => (id === 'overlayRoot' ? overlayRoot : null),
  createElement: (tag) => makeEl(tag),
  addEventListener(type, fn, capture) {
    // 重複加同一個函式 + 同一個 capture 旗標是 no-op（DOM 規格），
    // 假 DOM 也要照做，否則測不出「不會重複掛」。
    if (listeners.some((l) => l.type === type && l.fn === fn && l.capture === capture)) return;
    listeners.push({ type, fn, capture });
  },
  removeEventListener(type, fn, capture) {
    const i = listeners.findIndex(
      (l) => l.type === type && l.fn === fn && l.capture === capture);
    if (i !== -1) listeners.splice(i, 1);
  },
};
globalThis.HTMLElement = class {};

/** 送一個 Esc。回傳被呼叫到的監聽器數量。 */
function pressEscape() {
  const ev = {
    key: 'Escape',
    stopPropagation() { ev._stopped = true; },
    preventDefault() { ev._prevented = true; },
  };
  for (const l of [...listeners]) if (l.type === 'keydown') l.fn(ev);
  return ev;
}

// ── 載入受測模組 ─────────────────────────────────────

const { pushDismissable } = await import(
  pathToFileURL(join(here, 'overlay.js')).href);

// ── 斷言 ─────────────────────────────────────────────

let failed = 0;
function check(name, cond, extra = '') {
  if (cond) { console.log(`  ok  ${name}`); return; }
  failed += 1;
  console.log(`  FAIL ${name}${extra ? ` —— ${extra}` : ''}`);
}

// 1. 監聽必須掛在 capture 階段。
//    這條就是那個 bug：冒泡階段的話，<video> 的原生控制列會先吃掉 Esc。
const closed = [];
const a = pushDismissable({ close: () => closed.push('a') });
check('Esc 監聽掛在 capture 階段',
      listeners.some((l) => l.type === 'keydown' && l.capture === true));

// 2. Esc 關掉最上層，而且事件被吃掉（不讓底下的元素再處理一次）
const ev1 = pressEscape();
check('Esc 關掉最上層', closed.join() === 'a', `closed=${closed}`);
check('Esc 被 stopPropagation', ev1._stopped === true);
check('Esc 被 preventDefault', ev1._prevented === true);

// 3. 堆疊空了就把監聽解掉（不留殘留監聽）
check('堆疊空了解除監聽', !listeners.some((l) => l.type === 'keydown'));

// 4. 一次只關一層，順序是後進先出
closed.length = 0;
const b = pushDismissable({ close: () => closed.push('b') });
const c = pushDismissable({ close: () => closed.push('c') });
pressEscape();
check('第一次 Esc 只關最上面那一層', closed.join() === 'c', `closed=${closed}`);
pressEscape();
check('第二次 Esc 關下一層', closed.join() === 'c,b', `closed=${closed}`);

// 5. 全螢幕時 Esc 讓給瀏覽器（退出全螢幕），不順手多關一層
closed.length = 0;
const d = pushDismissable({ close: () => closed.push('d') });
document.fullscreenElement = {};
const ev2 = pressEscape();
check('全螢幕時不關面板', closed.length === 0);
check('全螢幕時不吃掉事件', ev2._stopped !== true);
document.fullscreenElement = null;
pressEscape();
check('退出全螢幕之後 Esc 恢復正常', closed.join() === 'd');

// 6. 重複 release 是安全的（呼叫端自己關 + Esc 同時發生）
closed.length = 0;
const e = pushDismissable({ close: () => closed.push('e') });
e.release();
e.release();
pressEscape();
check('release 過的不會再被 Esc 關到', closed.length === 0);

// 7. 沒登記任何東西時，Esc 不該爆炸
check('空堆疊按 Esc 安全', (() => { try { pressEscape(); return true; } catch { return false; } })());

console.log(failed ? `\n${failed} 項失敗` : '\n全部通過');
process.exit(failed ? 1 : 0);
