// 佇列與設定的**資料層**：只負責取值、存進 state、通知訂閱者。
//
// ⚠️ 這裡刻意不碰 DOM。header 的背景活動區、設定面板、問題面板三處都要用
// 同一份佇列狀態，如果由某一個畫面模組去抓，其他兩個就得 import 它 ——
// 那正是拆模組前 main → problems → main 那種循環相依的來源。
// 資料層當葉子模組，畫面各自訂閱。

import { api } from './api.js';
import { state } from './state.js';

const listeners = new Set();

/** 佇列狀態或設定有更新時被呼叫。畫面模組用它重繪，不必自己輪詢。 */
export function onQueueChange(fn) {
  listeners.add(fn);
}

function notify() {
  for (const fn of listeners) fn();
}

/** 每幾秒被呼叫一次。**失敗回 null 並讓畫面講出來** —— 不要靜默保留舊值，
 *  「backend 掛了」與「佇列是空的」在畫面上長得一模一樣，那正是要避免的。 */
export async function refreshQueue() {
  try {
    state.queue = await api('/api/queue/status');
  } catch {
    state.queue = null;
  }
  notify();
  return state.queue;
}

export async function loadSettings() {
  try {
    state.settings = await api('/api/settings');
  } catch {
    state.settings = null;   // 佇列列那邊已經會顯示 backend 無回應，不重複報錯
  }
  notify();
  return state.settings;
}

/** 改一個會被記住的設定。回傳新的值與來源；失敗就拋，呼叫端負責還原畫面。
 *
 *  ⚠️ 這一個 PATCH 會**寫進磁碟**（`prefs.json`），不只是改記憶體。
 *  原本只改記憶體，症狀是每次重開 backend 都要重設一次 —— 使用者回報的
 *  就是這件事。 */
export async function patchSetting(patch) {
  const s = await api('/api/settings', {
    method: 'PATCH',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(patch),
  });
  mergeSettings(s);
  return s;
}

/** 背景下載開關。 */
export const setAutoDownload = (on) => patchSetting({ auto_download: on });

/** 把一個設定從偏好檔移除，值回到 config.toml／內建預設的那個。
 *
 *  這是「我改了 config.toml 卻沒生效」的出口 —— 沒有它，使用者只能自己去
 *  找那個 JSON 檔並手動編輯，而他根本不知道有那個檔案。 */
export async function resetSetting(key) {
  const s = await api(`/api/settings/${encodeURIComponent(key)}`, { method: 'DELETE' });
  mergeSettings(s);
  return s;
}

/** 把 PATCH / DELETE 回來的片段併進手上那份設定。
 *
 *  ⚠️ `sources` 與 `config_values` 一定要一起換掉：只更新值而讓來源留著舊的，
 *  畫面會說「這是 config.toml 決定的」而其實剛剛才被你改掉。 */
function mergeSettings(s) {
  if (state.settings) {
    for (const k of Object.keys(s)) state.settings[k] = s[k];
  }
  notify();
}
