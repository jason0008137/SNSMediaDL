// Service worker（非持久）。訊息轉接、同步排程、對 backend 的**所有**網路請求。
//
// ⚠️ 為什麼所有請求都必須從這裡發：
// MV3 的 content script 發跨來源請求時，帶的是「頁面的 origin」（x.com），
// 受頁面的 CORS 規範管，不是擴充功能的。所以 content script 直接 fetch
// 本機 backend 會被 CORS 擋掉，症狀是 "Failed to fetch"。
// service worker 的 origin 是 chrome-extension://，且有 host_permissions。

import {
  countPending, enqueue, flush, getState, ping, resetState, userIdFor,
} from './sync.js';
import { getSettings, setSettings } from './settings.js';
import { report, startDevWatch } from './dev.js';

/** 對 backend 的唯一出口。content script 透過訊息呼叫它。 */
async function apiFetch(path, options = {}) {
  const { backendUrl } = await getSettings();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), options.timeoutMs || 15000);
  try {
    const res = await fetch(`${backendUrl}${path}`, {
      method: options.method || 'GET',
      headers: options.body ? { 'content-type': 'application/json' } : undefined,
      body: options.body ? JSON.stringify(options.body) : undefined,
      signal: controller.signal,
    });
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = text; }
    // silent 用於回報端點自己，否則回報失敗會再產生一筆回報 -> 無限迴圈
    if (!res.ok && !options.silent) {
      report('error', 'apiFetch 非 2xx', `${res.status} ${path}`, { path, status: res.status });
    }
    return { ok: res.ok, status: res.status, data };
  } catch (e) {
    if (!options.silent) report('error', 'apiFetch 失敗', String(e.message || e), { path });
    return { ok: false, status: 0, error: String(e.message || e) };
  } finally {
    clearTimeout(timer);
  }
}

// ── 每分頁 badge ─────────────────────────────────────
//
// badge 顯示的是「**這一個分頁**在看的帳號待送幾則」，不是全帳號總和。
// 總和讓分好的資料看起來像大混池：A 頁 6 則、B 頁 10 則卻顯示 16，
// 使用者合理推論「在 B 設標籤會影響全部 16 則」（實際不會）。
// 顯示要跟資料的切分方式一致，否則正確的結構會被錯誤的顯示推翻。
//
// 全域 badge 永遠留空 —— 沒有一個「所有分頁通用」的數字是誠實的。

const TAB_PAGES = 'tabPages';   // storage.session: tabId -> screenName | null

async function readTabPages() {
  const r = await chrome.storage.session.get(TAB_PAGES);
  return r[TAB_PAGES] || {};
}

/** service worker 會被回收，這份對應要能重建 ——
 *  存 storage.session（隨瀏覽器 session 存活，不落磁碟）。 */
async function rememberTabPage(tabId, screenName) {
  if (tabId == null) return;
  const pages = await readTabPages();
  const key = String(tabId);
  const value = screenName || null;
  if (pages[key] === value) return;
  pages[key] = value;
  await chrome.storage.session.set({ [TAB_PAGES]: pages });
}

async function setBadge(tabId, n) {
  if (tabId == null) return;
  try {
    await chrome.action.setBadgeText({ tabId, text: n ? String(n) : '' });
    if (n) await chrome.action.setBadgeBackgroundColor({ tabId, color: '#ff7a00' });
  } catch {
    /* 分頁已關掉，沒什麼好做的 */
  }
}

async function paintTab(tabId, screenName, state) {
  const uid = userIdFor(state, screenName);
  await setBadge(tabId, uid ? countPending(state, uid) : 0);
}

/** 送出之後所有分頁的數字都可能變了（同一個帳號可能開在兩個分頁）。 */
async function paintAllTabs(state) {
  const pages = await readTabPages();
  await Promise.all(Object.entries(pages)
    .map(([tabId, name]) => paintTab(Number(tabId), name, state)));
}

chrome.tabs.onRemoved.addListener(async (tabId) => {
  const pages = await readTabPages();
  if (!(String(tabId) in pages)) return;
  delete pages[String(tabId)];
  await chrome.storage.session.set({ [TAB_PAGES]: pages });
});

// ── 訊息 ─────────────────────────────────────────────

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg?.type === 'apiFetch') {
    (async () => sendResponse(await apiFetch(msg.path, msg.options)))();
    return true;
  }

  if (msg?.type === 'captured') {
    (async () => {
      const page = msg.pageScreenName || null;
      const r = await enqueue(msg.posts || [], msg.screenNames || {}, page);
      await rememberTabPage(sender.tab?.id, page);
      await setBadge(sender.tab?.id, r.pendingForPage);
      sendResponse({ ok: true, ...r });
    })();
    return true;
  }

  // SPA 內換帳號不會重載頁面，數字要跟著換
  if (msg?.type === 'pageChanged') {
    (async () => {
      const page = msg.pageScreenName || null;
      await rememberTabPage(sender.tab?.id, page);
      await paintTab(sender.tab?.id, page, await getState());
      sendResponse({ ok: true });
    })();
    return true;
  }

  if (msg?.type === 'syncNow') {
    (async () => {
      const r = await flush(msg.userId || null, msg.tags || null);
      report(r.online ? 'info' : 'error', '手動同步', r.error || `送出 ${r.sent}`, r);

      // 送出只是入庫排隊 —— backend 的 /api/ingest 不下載任何東西。
      // 不接著觸發，那顆按鈕就會像先前那樣「回報成功但檔案永遠不落地」。
      if (r.online && r.sent > 0) {
        const run = await apiFetch('/api/queue/run', { method: 'POST' });
        if (run.ok) {
          r.download = run.data;
        } else {
          // 觸發失敗要說出來。這正是先前那種靜默失敗。
          r.downloadError = run.error || `HTTP ${run.status}`;
          report('error', '已入庫但無法啟動下載', r.downloadError, {});
        }
      }
      await paintAllTabs(await getState());
      sendResponse(r);
    })();
    return true;
  }

  if (msg?.type === 'queueStatus') {
    (async () => sendResponse(await apiFetch('/api/queue/status', { silent: true })))();
    return true;
  }

  if (msg?.type === 'getState') {
    (async () => {
      if (msg.withPing) await ping();
      const state = await getState();
      sendResponse({
        state,
        settings: await getSettings(),
        pendingTotal: countPending(state),
        pendingForUser: msg.userId ? countPending(state, msg.userId) : null,
      });
    })();
    return true;
  }

  if (msg?.type === 'setSettings') {
    (async () => sendResponse(await setSettings(msg.patch || {})))();
    return true;
  }

  if (msg?.type === 'reset') {
    (async () => {
      await resetState();
      await paintAllTabs(await getState());
      sendResponse({ ok: true });
    })();
    return true;
  }

  // content script 回報自己的錯誤與狀態
  if (msg?.type === 'report') {
    report(msg.level, msg.event, msg.detail, msg.context, msg.where);
    sendResponse({ ok: true });
    return true;
  }

  return false;
});

self.addEventListener('unhandledrejection', (e) => {
  report('error', 'service worker 未攔截的 rejection', String(e.reason), {}, 'background');
});

/** v0.7 會設全域 badge（全帳號總和 / 錄製計數）。升級後那個數字不再有人更新，
 *  不清掉就會有一個永遠不變的殘影掛在圖示上。 */
async function clearGlobalBadge() {
  try { await chrome.action.setBadgeText({ text: '' }); } catch { /* 沒什麼好做的 */ }
}

chrome.runtime.onStartup.addListener(() => { clearGlobalBadge(); });
chrome.runtime.onInstalled.addListener(() => {
  report('info', 'extension 已載入', chrome.runtime.getManifest().version, {}, 'background');
  clearGlobalBadge();
});

startDevWatch(apiFetch);
