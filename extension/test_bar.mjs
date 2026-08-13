// bar.js 的渲染邏輯：本頁帳號的數字、送出鈕文案、其他帳號提示、錯誤優先序。
// 用法: node extension/test_bar.mjs
//
// 為什麼需要這支：bar.js 只在瀏覽器裡跑，先前每次改它都得靠人工實機才發現
// ReferenceError。這裡用一個極簡假 DOM 把它載起來，至少讓「面板打得開、
// 數字算得對」不必等到實機。**不能取代實機驗收**（拖曳、Shadow DOM、
// 真的 x.com 版面都不在這裡）。

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));

// ── 極簡假 DOM ───────────────────────────────────────
// 只做 bar.js 真的用到的事。innerHTML 不解析 HTML，只把裡面的 id 註冊起來。

const registry = new Map();

function classList(el) {
  const set = new Set();
  return {
    add: (c) => set.add(c),
    remove: (c) => set.delete(c),
    contains: (c) => set.has(c),
    toggle: (c, on) => (on === undefined ? (set.has(c) ? set.delete(c) : set.add(c))
      : (on ? set.add(c) : set.delete(c))),
    has: (c) => set.has(c),
  };
}

function makeEl(id = null) {
  const el = {
    id, textContent: '', value: '', disabled: false, style: {},
    _class: '',
    classList: null,
    children: [],
    addEventListener(type, fn) { ((el._on ||= {})[type] ||= []).push(fn); },
    append(...kids) { el.children.push(...kids); },
    matches: () => false,
    getBoundingClientRect: () => ({
      left: 100, top: 100, right: 300, bottom: 300, width: 200, height: 200,
    }),
    get className() { return el._class; },
    set className(v) { el._class = v; },
    set innerHTML(html) {
      el._html = html;
      for (const m of String(html).matchAll(/id="([^"]+)"/g)) {
        if (!registry.has(m[1])) registry.set(m[1], makeEl(m[1]));
      }
    },
    get innerHTML() { return el._html || ''; },
  };
  el.classList = classList(el);
  el.attachShadow = () => {
    const shadow = makeEl('#shadow');
    shadow.getElementById = (x) => registry.get(x) || null;
    return shadow;
  };
  return el;
}

globalThis.document = {
  body: makeEl('body'),
  documentElement: makeEl('html'),
  createElement: () => makeEl(),
  getElementById: () => null,   // host 還沒掛上
  addEventListener() {},
};
globalThis.window = { innerWidth: 1280, innerHeight: 800, addEventListener() {} };
globalThis.location = { pathname: '/alice/media' };
globalThis.setInterval = () => 0;
globalThis.clearInterval = () => {};

// ── 假 backend / service worker ──────────────────────

let syncState = null;
let sent = null;
const storage = { panelExpanded: true };

globalThis.chrome = {
  storage: {
    local: {
      get: async (key) => {
        const keys = Array.isArray(key) ? key : [key];
        const out = {};
        for (const k of keys) if (k in storage) out[k] = storage[k];
        return out;
      },
      set: async (obj) => { Object.assign(storage, obj); },
    },
  },
  runtime: {
    sendMessage: async (msg) => {
      if (msg.type === 'getState') {
        return { state: syncState, settings: {}, pendingTotal: 99 };
      }
      if (msg.type === 'apiFetch') {
        if (msg.path.startsWith('/api/accounts')) return { ok: true, data: [] };
        if (msg.path.startsWith('/api/creators')) return { ok: true, data: [] };
        return { ok: true, data: {} };
      }
      if (msg.type === 'syncNow') { sent = msg; return { online: true, sent: 1 }; }
      return {};
    },
  },
};

new Function(readFileSync(join(here, 'bar.js'), 'utf8'))();
const bar = globalThis.window.__SNSMediaDLBar;

let fail = 0;
const check = (label, got, want) => {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) fail++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}: ${JSON.stringify(got)}${ok ? '' : ` (期望 ${JSON.stringify(want)})`}`);
};

const $ = (id) => registry.get(id);
const settle = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
const click = async (id) => {
  for (const fn of $(id)._on?.click || []) await fn();
  await settle();
};
// 成功訊息會擋住後續提示（renderNotices 刻意不蓋掉綠字），測試間要清乾淨
const clearMsg = () => { $('msg').textContent = ''; $('msg').className = 'msg'; };

const state = (over = {}) => ({
  pending: {}, screenNames: {}, unresolved: null, droppedOverflow: 0,
  online: true, ...over,
});

// --- 掛載 ---
syncState = state();
bar.mount();
await settle();
check('面板掛得起來（沒有拋例外）', !!$('primary'), true);

// --- 1. 在 alice 頁面，佇列有 6 則 -> 只顯示 6 ---
syncState = state({
  pending: { u1: { 1: {}, 2: {}, 3: {}, 4: {}, 5: {}, 6: {} } },
  screenNames: { u1: 'alice' },
});
bar.setPageScreenName('alice');
await settle();
check('帳號顯示', $('acct').textContent, '@alice');
check('待送數是本頁帳號的', $('count').textContent, 6);
check('送出鈕寫明帳號', $('primary').textContent, '送出並下載 6 則（@alice）');
check('送出鈕可按', $('primary').disabled, false);
check('沒有其他帳號提示', $('others').classList.has('hidden'), true);

// --- 2. ⚠️ 切到 bob（10 則）—— 不可以顯示 16 ---
// 這正是使用者回報「像大混池」的那一幕：顯示混池會讓人以為資料也混池。
syncState = state({
  pending: {
    u1: { 1: {}, 2: {}, 3: {}, 4: {}, 5: {}, 6: {} },
    u2: Object.fromEntries(Array.from({ length: 10 }, (_, i) => [i, {}])),
  },
  screenNames: { u1: 'alice', u2: 'bob' },
});
bar.setPageScreenName('bob');
await settle();
check('B 頁只顯示 B 的數字', $('count').textContent, 10);
check('FAB badge 也是 B 的', $('fabBadge').textContent, 10);
check('送出鈕是 B', $('primary').textContent, '送出並下載 10 則（@bob）');
check('A 的待送另外提示', $('others').textContent,
  '其他帳號另有待送：@alice 6（去該帳號頁面送出）');

// --- 3. 送出只送本頁帳號，且帶當下的下拉值 ---
$('rating').value = 'r18';
$('content').value = 'illust';
await click('primary');
check('送出目標是 B 的 userId', sent.userId, 'u2');
check('標籤是此刻下拉的值', sent.tags, { rating: 'r18', contentType: 'illust' });

// --- 4. 不在帳號頁 -> 不採集也不給送 ---
clearMsg();
syncState = state({ pending: {}, screenNames: {} });
bar.setPageScreenName(null);
await settle();
check('顯示不在帳號頁面', $('acct').textContent, '不在帳號頁面');
check('送出鈕停用', $('primary').disabled, true);
check('提示怎麼開始', $('msg').textContent, '開啟某個帳號的頁面就會開始採集');

// --- 5. 帳號還沒進 backend DB 也要能送（新帳號的第一批）---
syncState = state({
  pending: { u9: { 1: {} } },
  screenNames: { u9: 'newbie' },
});
bar.setPageScreenName('newbie');
await settle();
check('新帳號（account 為 null）仍有送出入口', $('primary').disabled, false);
check('新帳號的數字也對', $('count').textContent, 1);

// --- 6. 對不出 userId 要吵，而且排在最前面 ---
clearMsg();
syncState = state({
  pending: {}, screenNames: {}, droppedOverflow: 5,
  unresolved: { screenName: 'ghost', count: 12 },
});
bar.setPageScreenName('ghost');
await settle();
check('unresolved 蓋過溢位警告', $('msg').textContent,
  '@ghost 對不出 userId，已略過 12 則 —— 重新整理頁面再試');
check('是紅色的', $('msg').className, 'msg err');

console.log(fail === 0 ? '\n全部通過' : `\n${fail} 項失敗`);
process.exit(fail === 0 ? 0 : 1);
