// 啟動、輪詢節奏、view 註冊。**這是唯一的進入點**（index.html 只載這一支）。

import { $ } from './dom.js';
import { applyI18n, initI18n } from './i18n.js';
import { state } from './state.js';
import { registerView, wireTabs } from './nav.js';
import { refreshQueue, loadSettings, onQueueChange } from './queue.js';
import { initTooltips } from './tooltip.js';
import { wireHeader } from './header.js';
import {
  loadMedia, wireAccountPicker, paintMoreNotes, wireFilters, restoreSort,
} from './views/media.js';
import { loadAccountsView, loadCreatorList, wireAccountFilters } from './views/accounts.js';
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
  // ⚠️ **語言要在任何 view 渲染之前決定並載入完成**，否則第一屏會閃一次
  // 空字串或 key 名。localhost 一次往返可以忽略，而「首屏閃一下」是那種
  // 每次開頁都會看到、又不值得回報的爛體驗。
  //
  // 語言存在後端偏好（`prefs.json`，與 `config.toml` 分開：一份是人寫的、
  // 一份是程式寫的），不是 localStorage —— 兩個真實來源必然漂移，
  // 症狀是「換了語言，重開又變回去」。
  //
  // ⚠️ backend 掛掉時用 'en'：那是 Config.language 的預設值，不是猜的。
  // 而且「backend 沒回應」已經由佇列列與設定頁各講一次，這裡不必再報一次。
  const boot = await loadSettings();
  await initI18n(boot?.language);
  applyI18n();

  initTooltips();

  // 各 view 的載入函式註冊給 nav（見 nav.js 的循環相依與快取說明）。
  // 只有目前這一個會被載入 —— 開頁時不再把四個 view 的資料全撈一遍。
  registerView('media', () => loadMedia());
  registerView('accounts', () => loadAccountsView());
  registerView('fetch', () => loadFetchView());
  wireTabs();
  wireHeader();

  // ⚠️ 順序：篩選下拉要在第一次 loadMedia() **之前**建好 —— 那支會讀
  // drops 去組查詢參數與條件標籤。晚一步的症狀是首屏送出一份沒有篩選的查詢，
  // 而畫面上的控制項看起來是有選的。
  //
  // 排序鍵與批次列那三個現在也是自製下拉，一併在這裡建。
  wireFilters();
  // 帳號頁的篩選與排序下拉。同樣要在第一次 loadAccounts() 之前建好 ——
  // `accountQuery()` 讀 aDrops 組查詢參數。
  // 記住的排序偏好也在裡面還原（**經過白名單**，見 storedAccountSort()）。
  wireAccountFilters();
  // 存的是「鍵:方向」（例如 added:desc）。還原時白名單驗證，認不得就用預設 ——
  // 直接套用會送出 `sort=`，那是一個不報錯也不生效的空條件。
  restoreSort();

  // 佇列狀態更新時，媒體頁「更多篩選」裡那句「目前沒有待下載或失敗的項目」
  // 要跟著變 —— 它讀的就是這份資料，不另外請求。
  onQueueChange(paintMoreNotes);

  // 首屏只等媒體格線那一個請求。其餘並行且不擋畫面 ——
  // 舊版序列 await 六個請求，最慢的那個決定了「開頁到看見東西」的時間。
  wireAccountPicker();
  const rest = Promise.all([
    // ⚠️ `loadSettings()` 已經在最上面 await 過（語言要它）—— 這裡不再問一次。
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
  console.error('SNSMediaDL failed to start', err);
  document.body.insertAdjacentHTML('afterbegin',
    // ⚠️ 這裡**不能**用 t()：啟動失敗的原因很可能就是 i18n 自己沒載成功。
    //    英文寫死 —— 這是全站唯一一處刻意不走語系檔的使用者可見字串。
    `<div class="boot-error">GUI failed to start: ${err.message}
     <br><small>See the browser console. This usually means a frontend bug,
     not a data problem.</small></div>`);
});
