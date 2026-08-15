// 啟動、輪詢節奏、view 註冊。**這是唯一的進入點**（index.html 只載這一支）。

import { $ } from './dom.js';
import { state } from './state.js';
import { registerView, wireTabs } from './nav.js';
import { refreshQueue, loadSettings, onQueueChange } from './queue.js';
import { initTooltips } from './tooltip.js';
import { wireHeader } from './header.js';
import { loadMedia, wireAccountPicker, paintMoreNotes } from './views/media.js';
import { loadAccountsView, loadCreatorList } from './views/accounts.js';
import { loadFetchView, refreshFetchQueue } from './views/fetch.js';

// ── 輪詢：有事才快，沒事就慢，看不到就停 ─────────────
//
// 三段式：
//   有東西在跑     → 3 秒（要看得到進度在動）
//   全部閒置       → 30 秒
//   分頁在背景     → 完全不問，切回來時立刻補一次

const POLL_BUSY = 3000;
const POLL_IDLE = 30000;
let pollTimer = null;

function scheduleNextPoll(delay) {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(pollOnce, delay);
}

/** `force` 用於啟動與「切回分頁」——那兩個時刻使用者是真的在等畫面。
 *
 *  ⚠️ 沒有 `force` 的話有個真實破口：在**背景分頁**開啟 GUI（例如按中鍵
 *  開新分頁）時 `document.hidden` 一開始就是 true，pollOnce 直接 return，
 *  佇列區就一直是空白 —— 而空白看起來跟壞掉一模一樣。 */
async function pollOnce({ force = false } = {}) {
  if (document.hidden && !force) return;   // 切回來時由 visibilitychange 接手
  const [queue] = await Promise.all([
    refreshQueue(),
    // 抓取佇列只在記憶體裡，很便宜；但人不在抓取頁時沒必要一直重畫它，
    // 只有徽章與輪詢節奏需要它 —— 所以仍然問，但只在那一頁才會看到細節。
    refreshFetchQueue(),
  ]);
  const busy = queue && (queue.active > 0 || queue.running);
  scheduleNextPoll(busy || state.fetchActive ? POLL_BUSY : POLL_IDLE);
}

document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    clearTimeout(pollTimer);
  } else {
    // 回到分頁的第一件事是把畫面補到最新 —— 使用者剛離開一段時間，
    // 顯示的數字很可能已經過期
    pollOnce({ force: true });
  }
});

// ── 啟動 ───────────────────────────────────────────────
async function init() {
  initTooltips();

  // 各 view 的載入函式註冊給 nav（見 nav.js 的循環相依與快取說明）。
  // 只有目前這一個會被載入 —— 開頁時不再把四個 view 的資料全撈一遍。
  registerView('media', () => loadMedia());
  registerView('accounts', () => loadAccountsView());
  registerView('fetch', () => loadFetchView());
  wireTabs();
  wireHeader();

  // 排序偏好記住。GUI 預設 favorite，而 API 預設是 id（= 舊行為，extension 靠它）
  $('aSort').value = localStorage.getItem('accountSort') || 'favorite';
  $('fSort').value = localStorage.getItem('mediaSort') || 'newest';

  // 佇列狀態更新時，媒體頁「更多篩選」裡那句「目前沒有待下載或失敗的項目」
  // 要跟著變 —— 它讀的就是這份資料，不另外請求。
  onQueueChange(paintMoreNotes);

  // 首屏只等媒體格線那一個請求。其餘並行且不擋畫面 ——
  // 舊版序列 await 六個請求，最慢的那個決定了「開頁到看見東西」的時間。
  wireAccountPicker();
  const rest = Promise.all([
    loadSettings(),
    // creator 清單很小，但媒體頁的下拉與帳號抽屜都要用它
    loadCreatorList().catch(() => {}),
    pollOnce({ force: true }),
  ]);
  await loadMedia();
  await rest;
}

// ⚠️ **啟動失敗必須看得見。**
// `init()` 是 async，裡面任何一個沒接住的例外都只會變成一個 unhandled
// rejection —— 畫面停在空白，console 也可能沒有紅字（實測遇過）。
// 使用者看到的是「打開就是空的」，而那跟「資料庫是空的」長得一模一樣。
init().catch((err) => {
  console.error('SNSMediaDL 啟動失敗', err);
  document.body.insertAdjacentHTML('afterbegin',
    `<div class="boot-error">GUI 啟動失敗：${err.message}
     <br><small>詳情看瀏覽器主控台。這通常代表前端有 bug，不是資料的問題。</small></div>`);
});
