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

/** 背景下載開關。回傳新的設定；失敗就拋，呼叫端負責把畫面還原。 */
export async function setAutoDownload(on) {
  const s = await api('/api/settings', {
    method: 'PATCH',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ auto_download: on }),
  });
  if (state.settings) state.settings.auto_download = s.auto_download;
  notify();
  return s;
}
