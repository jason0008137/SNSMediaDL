// 開發輔助：診斷回報 + 自動重載。
//
// 存在的理由：extension 跑在瀏覽器裡，開發時看不到它的錯誤與狀態，
// 只能靠人用文字轉述症狀 —— 那是很差的回饋迴圈。
// 這支把 extension 的內部狀態接到 backend，並在檔案變動時自己重載。

const BUFFER = [];
const FLUSH_MS = 1500;
const WATCH_MS = 3000;

let flushTimer = null;
let apiFetchRef = null;
let lastFingerprint = null;

/** 記一筆診斷。緩衝後批次送，避免每個事件都打一次網路。 */
export function report(level, event, detail = null, context = {}, where = 'background') {
  BUFFER.push({ level, event, detail, context, where });
  // console 也留一份：backend 沒開時至少瀏覽器看得到
  const tag = `[SNSMediaDL:${where}]`;
  if (level === 'error') console.error(tag, event, detail || '', context);
  else console.log(tag, event, detail || '');

  if (!flushTimer) flushTimer = setTimeout(flushReports, FLUSH_MS);
}

async function flushReports() {
  flushTimer = null;
  if (!BUFFER.length || !apiFetchRef) return;
  const entries = BUFFER.splice(0, BUFFER.length);
  try {
    // 用 apiFetch 但不要讓它再 report 一次，否則失敗會無限迴圈
    await apiFetchRef('/api/ext-log', { method: 'POST', body: { entries }, silent: true });
  } catch {
    /* backend 沒開就算了，console 那份還在 */
  }
}

/** 輪詢 extension 檔案指紋，變了就自己重載。
 *
 * 這樣改完程式碼不需要去 chrome://extensions 手動點重新整理。 */
let watchOnline = null;   // null = 還不知道

export function startDevWatch(apiFetch) {
  apiFetchRef = apiFetch;

  const tick = async () => {
    // silent：這是背景輪詢，「backend 沒開」是日常狀態不是錯誤。
    // 先前沒帶 silent，只逛 x.com 沒開 backend 時 console 被
    // 每 3 秒一筆的 error 灌爆（沙盤 E）。狀態**轉變**才值得記一筆。
    const r = await apiFetch('/api/ext-version', { timeoutMs: 4000, silent: true });

    const nowOnline = !!r?.ok;
    if (nowOnline !== watchOnline) {
      if (nowOnline) {
        report('info', 'backend 已連上', null, {}, 'background');
      } else if (watchOnline !== null) {
        // 離線的那筆只進 console —— backend 不在，送 ext-log 也到不了
        console.warn('[SNSMediaDL:background] backend 離線，dev watch 待命中');
      }
      watchOnline = nowOnline;
    }
    if (!nowOnline || !r.data?.dev_reload) return;

    const fp = r.data.fingerprint;
    if (lastFingerprint === null) {
      lastFingerprint = fp;
      return;
    }
    if (fp !== lastFingerprint) {
      report('info', 'extension 檔案有變動，自動重載', `${lastFingerprint} -> ${fp}`);
      await flushReports();          // 先把日誌送出去，重載會清掉記憶體
      chrome.runtime.reload();
    }
  };

  tick();
  setInterval(tick, WATCH_MS);
}
