// 採集佇列。
//
// 採集與傳輸分離：採集永遠成功（寫本機），傳輸可以失敗重來。
//
// ⚠️ 閘門（v0.8.0，2026-08-12）：**只收「目前這一頁的帳號」的貼文**。
//
// 取代 v0.7 的錄製模式。錄製鍵是在「佇列還沒分帳號」的時代想出來的解法；
// 分帳號結構落地之後，它擋的每一件事都已經由結構擋住（標籤隨 payload 送、
// 送出只送單一帳號、popup 編輯區已移除、queue/run 已接上），剩下的產出
// 只有摩擦。
//
// 網址是權威：hover card、側欄推薦、轉推牆上別人的東西天生進不來 ——
// 它們的 userId 不等於這一頁的帳號。判斷點只有一個（enqueue），
// 兩邊各判一次遲早會不一致。
//
// 佇列「依帳號分割」而不是依分頁：
//   - 同一個分頁可以在 SPA 內切換帳號
//   - 兩個分頁看同一個帳號本來就該合併
//   - 分頁 id 關掉就沒了，帳號 id 不會
// 更關鍵的是，不分割會寫壞資料 —— 見 flushAccount() 的註解。
// **顯示**則相反，是每個分頁各自的：badge 由 background.js 依分頁上色，
// 全域 badge 永遠留空。全帳號總和正是「大混池」錯覺的來源。

import { getSettings } from './settings.js';

const STATE_KEY = 'syncState';

// 離線暫存上限，**每個帳號各自計算**。
const MAX_PENDING_PER_ACCOUNT = 2000;

// v0.7 錄製模式的殘留欄位。不清掉的話 debug 時會看到兩套真相。
const LEGACY_KEYS = ['recording', 'lastRecorded', 'skippedWhileIdle', 'pendingTags'];

const emptyState = () => ({
  pending: {},        // userId -> { postId -> post }
  screenNames: {},    // userId -> screenName
  // 「人在帳號頁面上，卻一則都對不起來」。這是唯一**不該發生**的丟棄：
  // 代表 screenName -> userId 的對應沒建立起來，症狀會是「滑了半天數字一直 0」
  // 而沒有任何線索。不可以靜默 —— 見 bar.js renderNotices。
  unresolved: null,   // { screenName, count }
  syncedPosts: 0,
  syncedMedia: 0,
  droppedOverflow: 0,
  online: null,
  lastError: null,
  lastSyncAt: null,
});

/** 舊版的 pending 是 {postId: post}（沒有分帳號）。
 *  直接丟掉會讓使用者失去尚未送出的採集結果，所以就地遷移。 */
function migratePending(pending) {
  if (!pending || typeof pending !== 'object') return {};
  const values = Object.values(pending);
  const looksNested = values.length === 0
    || values.every((v) => v && typeof v === 'object' && !('postId' in v));
  if (looksNested) return pending;

  const migrated = {};
  for (const post of values) {
    if (!post || typeof post !== 'object') continue;
    const uid = String(post.userId || 'unknown');
    (migrated[uid] ||= {})[post.postId] = post;
  }
  return migrated;
}

export async function getState() {
  const r = await chrome.storage.local.get(STATE_KEY);
  const state = { ...emptyState(), ...(r[STATE_KEY] || {}) };
  state.pending = migratePending(state.pending);
  for (const k of LEGACY_KEYS) delete state[k];
  return state;
}

async function setState(state) {
  await chrome.storage.local.set({ [STATE_KEY]: state });
  return state;
}

export async function resetState() {
  return setState(emptyState());
}

export function countPending(state, userId = null) {
  if (userId) return Object.keys(state.pending[userId] || {}).length;
  return Object.values(state.pending)
    .reduce((n, bucket) => n + Object.keys(bucket).length, 0);
}

/** screenName -> userId。這是「這一頁的帳號是誰」唯一的解析方式。
 *
 * 對應由攔到的回應建立（user 物件的 rest_id + screen_name），比從網址猜
 * 可靠；網址只給得起 screen_name。 */
export function userIdFor(state, screenName) {
  if (!screenName) return null;
  const target = String(screenName).toLowerCase();
  const hit = Object.entries(state.screenNames || {})
    .find(([, name]) => (name || '').toLowerCase() === target);
  return hit ? hit[0] : null;
}

// ── 採集 ─────────────────────────────────────────────

/** 把採集到的貼文放進待送佇列。這一步永遠不會失敗。
 *
 * @param pageScreenName 網址判斷出來的「這一頁在看誰」。null（/home、
 *   /explore、貼文詳情以外的非帳號頁）代表**一則都不收**。 */
export async function enqueue(posts, screenNames, pageScreenName = null) {
  const state = await getState();

  // screenName 對應與收不收無關 —— backend 需要它才知道帳號叫什麼名字，
  // 而且本頁帳號的 userId 正是靠這份對應才解析得出來。
  for (const [userId, name] of Object.entries(screenNames || {})) {
    if (name) state.screenNames[userId] = name;
  }

  const targetId = userIdFor(state, pageScreenName);

  let taken = 0;
  let skipped = 0;

  for (const p of posts) {
    const uid = String(p.userId || 'unknown');
    if (!targetId || uid !== targetId) {
      skipped += 1;
      continue;
    }
    (state.pending[uid] ||= {})[p.postId] = p;
    taken += 1;
  }

  // 在帳號頁面上卻一則都對不起來 = 對應建立失敗，**不是「這頁沒東西」**
  if (pageScreenName && !targetId && posts.length) {
    const same = state.unresolved?.screenName === pageScreenName;
    state.unresolved = {
      screenName: pageScreenName,
      count: (same ? state.unresolved.count : 0) + posts.length,
    };
  } else if (taken && state.unresolved) {
    state.unresolved = null;   // 對應補起來了
  }

  // 每個帳號各自套上限，丟最舊的（postId 是遞增雪花 id，字串排序即時間序）
  for (const bucket of Object.values(state.pending)) {
    const ids = Object.keys(bucket);
    if (ids.length > MAX_PENDING_PER_ACCOUNT) {
      const excess = ids.sort().slice(0, ids.length - MAX_PENDING_PER_ACCOUNT);
      for (const id of excess) delete bucket[id];
      state.droppedOverflow += excess.length;
    }
  }

  await setState(state);
  return {
    pending: countPending(state),
    pendingForPage: targetId ? countPending(state, targetId) : 0,
    taken,
    skipped,
    targetId,
  };
}

async function fetchJson(url, options = {}, timeoutMs = 8000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, { ...options, signal: controller.signal });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } finally {
    clearTimeout(timer);
  }
}

/** 只探連線，不送資料。工具列需要這個才知道要不要啟用表單。 */
export async function ping() {
  const settings = await getSettings();
  const state = await getState();
  try {
    await fetchJson(`${settings.backendUrl}/api/health`, {}, 3000);
    state.online = true;
    state.lastError = null;
  } catch (e) {
    state.online = false;
    state.lastError = String(e.message || e);
  }
  await setState(state);
  return state.online;
}

/** 問 backend 哪些已經抓過了。純粹省流量 —— 失敗就照送全部，讓 backend 去重。 */
async function filterKnown(base, postIds) {
  if (!postIds.length) return postIds;
  try {
    const url = `${base}/api/known?platform=x&post_ids=${encodeURIComponent(postIds.join(','))}`;
    const body = await fetchJson(url);
    const known = new Set(body.known || []);
    return postIds.filter((id) => !known.has(id));
  } catch {
    return postIds;   // 省流量的功能失敗，不可以害正確性
  }
}

/** 這批要帶什麼標籤。
 *
 * 兩個來源，都是「使用者對這個帳號的選擇」：
 *   1. override —— 面板送出當下下拉的值（所見即所送）
 *   2. lastTags —— 本機按帳號記憶的上次選擇（popup 送殘留佇列時用）
 *
 * ⚠️ 沒有第三個。不可以有「全域目前選的值」那種來源：送 A 的佇列時
 * 讀到 B 頁面的下拉，就是把 B 的標籤蓋到 A 身上。 */
async function tagsFor(state, userId, override) {
  if (override) return override;
  const name = (state.screenNames[userId] || '').toLowerCase();
  if (!name) return {};
  const r = await chrome.storage.local.get('lastTags');
  return (r.lastTags || {})[name] || {};
}

/** 送出「單一帳號」的佇列。
 *
 * ⚠️ 為什麼一定要一個帳號一次請求：
 * backend 的 ingest 用每則貼文自己的 user_id 建帳號，但 screenName 是整個
 * request 共用的。一次送多個帳號的貼文，後面帳號的 screen_name 會被寫成
 * 第一個帳號的名字 —— 那是寫壞資料，不只是顯示錯。
 */
async function flushAccount(base, state, userId, override = null) {
  const bucket = state.pending[userId] || {};
  const allIds = Object.keys(bucket);
  if (!allIds.length) return { sent: 0, skipped: 0 };

  const toSendIds = await filterKnown(base, allIds);
  for (const id of allIds) {
    if (!toSendIds.includes(id)) delete bucket[id];
  }
  if (!toSendIds.length) {
    delete state.pending[userId];
    return { sent: 0, skipped: allIds.length };
  }

  // 標籤直接蓋在每則貼文上隨 payload 送。
  //
  // ⚠️ 為什麼不用「先 PATCH 帳號預設值、再靠 ingest 繼承」：
  // 帳號還不在 DB 時根本沒有東西可以 PATCH，於是每個新帳號的第一批必然以
  // rating=NULL 入庫，而事後改預設不回溯（沙盤 A）。
  // backend 端 adapters/x.py 讀 rating/contentType、services/ingest.py 標
  // rating_source=manual（優先序第一），本來就支援。
  const tags = await tagsFor(state, userId, override);
  const posts = toSendIds.map((id) => ({
    ...bucket[id],
    ...(tags.rating ? { rating: tags.rating } : {}),
    ...(tags.contentType ? { contentType: tags.contentType } : {}),
  }));
  const screenName = state.screenNames[userId] || null;

  const result = await fetchJson(`${base}/api/ingest`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ platform: 'x', screenName, posts }),
  }, 30000);

  for (const id of toSendIds) delete bucket[id];
  if (!Object.keys(bucket).length) delete state.pending[userId];

  return {
    sent: toSendIds.length,
    skipped: allIds.length - toSendIds.length,
    result,
  };
}

/** 送出佇列。帶 userId 只送該帳號，不帶則逐帳號各送一次。
 *
 * @param tags 只在指定 userId 時有意義 —— 它是「面板上此刻對這個帳號選的
 *   標籤」。不指定帳號的全送（popup 的殘留佇列出口）每個帳號各自讀
 *   自己的 lastTags。 */
export async function flush(userId = null, tags = null) {
  const settings = await getSettings();
  const base = settings.backendUrl;
  const state = await getState();

  const targets = userId ? [userId] : Object.keys(state.pending);

  if (!targets.length) {
    try {
      await fetchJson(`${base}/api/health`, {}, 3000);
      state.online = true;
      state.lastError = null;
    } catch (e) {
      state.online = false;
      state.lastError = String(e.message || e);
    }
    await setState(state);
    return { sent: 0, online: state.online, accounts: 0 };
  }

  let sent = 0;
  let skipped = 0;
  let postsNew = 0;
  let mediaNew = 0;

  try {
    for (const uid of targets) {
      const r = await flushAccount(base, state, uid, userId ? tags : null);
      sent += r.sent;
      skipped += r.skipped;
      postsNew += r.result?.posts_new || 0;
      mediaNew += r.result?.media_new || 0;
    }
  } catch (e) {
    // 已成功的帳號其 bucket 已清空，state 要存回去才不會重送
    state.online = false;
    state.lastError = String(e.message || e);
    await setState(state);
    return { sent, online: false, error: state.lastError, accounts: targets.length };
  }

  state.syncedPosts += postsNew;
  state.syncedMedia += mediaNew;
  state.online = true;
  state.lastError = null;
  state.lastSyncAt = new Date().toISOString();
  await setState(state);

  return { sent, skipped, online: true, accounts: targets.length, postsNew, mediaNew };
}
