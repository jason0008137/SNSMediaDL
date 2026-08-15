// 分頁切換：懶載入、短期快取、切走時停掉該 view 的背景工作。
//
// ⚠️ **這個模組刻意不 import 任何 view。**
// view 需要 showView（例如從帳號頁跳到媒體頁），而 showView 又要呼叫 view 的
// 載入函式 —— 直接互 import 會形成循環。改用 registry：main.js 啟動時把各
// view 的載入函式註冊進來，nav 只認名字。

import { $ } from './dom.js';
import { state } from './state.js';

const views = new Map();
const loadedAt = new Map();

// 切回來如果不到這個時間就不重載。
// 30 秒是配合輪詢節奏（閒置 30 秒問一次佇列）—— 比它短會變成「切分頁就重查」，
// 比它長會讓使用者剛在別的分頁改過東西、切回來卻看到舊資料。
const CACHE_MS = 30000;

/** main.js 啟動時呼叫。`name` 要與 tab 的 data-view 及 section 的 id 後綴一致。
 *
 *  `stop` 是選用的收尾（例如停掉這個 view 自己的計時器）—— 由 view 自己註冊，
 *  nav 不需要知道每個 view 在背景做什麼。 */
export function registerView(name, loader, { stop } = {}) {
  views.set(name, { loader, stop });
}

/** 這個 view 的快取失效，下次切過去一定重載。改了資料的地方要呼叫。 */
export function invalidateView(name) {
  loadedAt.delete(name);
}

/** 切到某個 view。
 *
 *  `load: false` 是給「切過去之後馬上要用不同條件重載」的呼叫端用的
 *  （例如從帳號頁跳過來看某個帳號）。少了它，切分頁會先用**舊條件**發一次
 *  請求，然後才是新條件那一次 —— 兩個併發，先發的後到就會蓋掉正確結果。
 *
 *  `force: true` 忽略快取（使用者按 tab 的期待是「看最新的」時用）。 */
export function showView(name, { load = true, force = false } = {}) {
  const prev = state.view;
  if (prev !== name) views.get(prev)?.stop?.();

  document.querySelectorAll('.tab').forEach((b) =>
    b.classList.toggle('active', b.dataset.view === name));
  state.view = name;
  document.querySelectorAll('.view').forEach((v) => v.classList.add('hidden'));
  $(`view-${name}`).classList.remove('hidden');

  if (!load) return;
  const fresh = Date.now() - (loadedAt.get(name) ?? -Infinity) < CACHE_MS;
  if (fresh && !force) return;      // 剛看過，不重發請求
  loadedAt.set(name, Date.now());
  views.get(name)?.loader?.();
}

export function wireTabs() {
  // 事件委派：一個 listener，不是每個 tab 各一個。
  document.querySelector('.tabs').addEventListener('click', (ev) => {
    const btn = ev.target.closest('.tab');
    if (btn) showView(btn.dataset.view);
  });
}
