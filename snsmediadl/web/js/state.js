// 共用狀態。**只有這裡宣告，其餘模組 import 它。**
//
// 拆成模組之後最容易出的錯是「各自 import 一份自己的狀態」——
// ES module 的單例語意保證不會，但前提是所有人都 import 這同一個檔案。

export const PAGE = 60;

export const state = {
  view: 'media',
  offset: 0,            // 只有 sort=stars 走 offset，其餘走 keyset
  cursors: [null],      // keyset 游標堆疊，長度 = 目前第幾頁
  hasMore: false,
  nextCursor: null,
  total: null,          // null = 還沒算出來。**不是 0**
  libTotal: null,       // 同一個安全模式下、不帶任何篩選的總數（算百分比用）
  // 被安全模式擋掉幾筆。後端只在「結果是 0」時才算（第二次 COUNT 很貴），
  // 其餘時候是 0 = 沒算，不是「確定沒被擋」。
  hiddenBySafe: 0,
  accounts: [],
  accountOptions: [],   // 帳號篩選 datalist 目前的候選
  accountFilter: '',    // 生效中的 account_id（'' = 全部）
  accountLabel: '',     // 那個 id 對應的顯示名（標籤要寫得出名字）
  creators: [],
  // creator 篩選（沒有下拉，只從帳號頁點進來）—— 比照 accountFilter
  creatorFilter: '',
  creatorLabel: '',
  items: [],              // 目前這頁的媒體，選取與 shift 範圍要用
  selecting: false,
  picked: new Set(),      // media id
  lastPickIndex: null,    // shift 範圍選取的錨點
  acctOffset: 0,          // 帳號頁的分頁位移
  acctTotal: 0,
  acctMode: 'accounts',   // accounts | creators（同一份資料的兩種分組）
  fetchActive: false,     // 抓取佇列還有東西 → 輪詢要維持快節奏
  loadingMedia: false,    // 格線載入中 → 翻頁要鎖住（游標還沒算出來）
  // 最後一次 /api/queue/status 的結果。介面有好幾處要講「下載狀態」，
  // 各自再打一次是白花的 —— 輪詢本來就每幾秒更新一次。
  queue: null,
  settings: null,         // 最後一次 /api/settings
};

// 安全模式預設開啟 —— 預設安全比預設方便重要。
// 只有使用者明確關掉才會記住，重開瀏覽器仍以安全為準若沒設定過。
//
// ⚠️ 匯出成**函式**而不是變數：ES module 匯出的是 live binding，
// import 端讀得到最新值沒問題，但**不能從 import 端指派**。
const stored = localStorage.getItem('safeMode');
let _safeMode = stored === null ? true : stored === 'true';

export const safeMode = () => _safeMode;

// 安全模式現在有**兩個**控制項（header 的開關與設定面板裡的那一份），
// 而且媒體頁要在它變動時重新查詢。用訂閱而不是讓各模組互相 import ——
// 那會形成 media ⇄ header 的循環相依。
const safeListeners = new Set();

export function onSafeModeChange(fn) {
  safeListeners.add(fn);
}

export function setSafeMode(on) {
  if (on === _safeMode) return;
  _safeMode = on;
  localStorage.setItem('safeMode', String(on));
  // 篩選條件變了，上一次算的全庫總數（含 exclude_rating）就不能用了
  state.libTotal = null;
  for (const fn of safeListeners) fn(on);
}
