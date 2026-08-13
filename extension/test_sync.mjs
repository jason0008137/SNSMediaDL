// sync.js 的傳輸行為：分帳號歸戶、離線容錯、溢位上限、known 降級。
// 用法: node extension/test_sync.mjs
//
// ⚠️ 採集只收「本頁帳號」的貼文，所以這裡的 enqueue 一律用 collect() 包起來
// （它會一個帳號一頁地走）。閘門本身的測試在 test_capture.mjs。

let store = {};

globalThis.chrome = {
  storage: {
    local: {
      get: async (key) => {
        if (Array.isArray(key)) {
          const out = {};
          for (const k of key) if (k in store) out[k] = store[k];
          return out;
        }
        return key in store ? { [key]: store[key] } : {};
      },
      set: async (obj) => { Object.assign(store, obj); },
    },
  },
};

let fetchLog = [];
let fetchImpl = null;
globalThis.fetch = async (url, options) => {
  fetchLog.push({ url: String(url), options });
  return fetchImpl(String(url), options);
};

const {
  countPending, enqueue, flush, getState, resetState,
} = await import('./sync.js');

let fail = 0;
const check = (label, got, want) => {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) fail++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}: ${JSON.stringify(got)}${ok ? '' : ` (期望 ${JSON.stringify(want)})`}`);
};

const reset = async () => { store = {}; fetchLog = []; await resetState(); };

const post = (id, userId = 'u1') => ({
  postId: id, userId, media: [{ kind: 'photo', url: 'x' }],
});

/** 逐帳號採集。閘門只收「本頁帳號」的貼文，要讓多個帳號都進佇列，
 *  就得一個帳號一頁地走，跟真實使用一樣。 */
const collect = async (posts, screenNames = {}) => {
  const names = { ...screenNames };
  for (const p of posts) names[p.userId] ||= p.userId;   // 沒給名字就拿 id 當名字
  await enqueue([], names, null);              // 先建立 userId -> screenName 對應
  const byUser = {};
  for (const p of posts) (byUser[p.userId] ||= []).push(p);
  for (const [uid, group] of Object.entries(byUser)) {
    await enqueue(group, {}, names[uid]);
  }
};

const jsonRes = (body) => ({ ok: true, status: 200, json: async () => body });
const errRes = (status) => ({ ok: false, status, json: async () => ({}) });

const ingestCalls = () => fetchLog
  .filter((f) => f.url.includes('/api/ingest'))
  .map((f) => JSON.parse(f.options.body));

// --- 1. 正常同步 ---
await reset();
fetchImpl = (url) => {
  if (url.includes('/api/known')) return jsonRes({ known: [] });
  if (url.includes('/api/ingest')) return jsonRes({ posts_new: 2, media_new: 2 });
  return jsonRes({});
};
await collect([post('1'), post('2')], { u1: 'artist' });
let r = await flush();
check('送出筆數', r.sent, 2);
check('連線狀態', r.online, true);
check('pending 已清空', countPending(await getState()), 0);
check('已同步媒體累計', (await getState()).syncedMedia, 2);
check('ingest 帶了 screenName', ingestCalls()[0].screenName, 'artist');

// --- 2. ⚠️ 混帳號：每個帳號各發一次，screenName 各自正確 ---
// 舊版取 posts[0].userId 當整批的 screenName，會把第二個帳號的名字寫錯。
await reset();
fetchImpl = (url) => {
  if (url.includes('/api/known')) return jsonRes({ known: [] });
  if (url.includes('/api/ingest')) return jsonRes({ posts_new: 1, media_new: 1 });
  return jsonRes({});
};
await collect([post('1', 'u1'), post('2', 'u2')], { u1: 'alice', u2: 'bob' });
r = await flush();

const calls = ingestCalls();
check('分成兩次 ingest', calls.length, 2);
check('涵蓋兩個帳號', r.accounts, 2);
const byName = Object.fromEntries(calls.map((c) => [c.screenName, c.posts.map((p) => p.userId)]));
check('alice 只收到 u1 的貼文', byName.alice, ['u1']);
check('bob 只收到 u2 的貼文', byName.bob, ['u2']);
check('全部清空', countPending(await getState()), 0);

// --- 3. 只送指定帳號 ---
await reset();
fetchImpl = (url) => {
  if (url.includes('/api/known')) return jsonRes({ known: [] });
  return jsonRes({ posts_new: 1, media_new: 1 });
};
await collect([post('1', 'u1'), post('2', 'u2')], { u1: 'alice', u2: 'bob' });
r = await flush('u1');
check('只送一個帳號', r.sent, 1);
check('另一個帳號留著', countPending(await getState(), 'u2'), 1);
check('被送的清空了', countPending(await getState(), 'u1'), 0);

// --- 4. 離線：pending 保留 ---
await reset();
fetchImpl = () => { throw new Error('Failed to fetch'); };
await collect([post('1'), post('2')], {});
r = await flush();
check('離線時送出 0 筆', r.sent, 0);
check('離線狀態', r.online, false);
check('離線後 pending 保留', countPending(await getState()), 2);
// badge 不再是 sync.js 的事 —— 它是每分頁各自的，由 background.js 上色

// --- 5. 恢復後補送 ---
fetchImpl = (url) => {
  if (url.includes('/api/known')) return jsonRes({ known: [] });
  return jsonRes({ posts_new: 2, media_new: 2 });
};
r = await flush();
check('恢復後補送成功', r.sent, 2);
check('補送後清空', countPending(await getState()), 0);

// --- 6. known 過濾 ---
await reset();
fetchImpl = (url) => {
  if (url.includes('/api/known')) return jsonRes({ known: ['1'] });
  if (url.includes('/api/ingest')) return jsonRes({ posts_new: 1, media_new: 1 });
  return jsonRes({});
};
await collect([post('1'), post('2')], {});
r = await flush();
check('known 過濾後只送 1 筆', r.sent, 1);
check('送出的是未知的那則', ingestCalls()[0].posts.map((p) => p.postId), ['2']);

// --- 7. known 掛掉時照送全部 ---
await reset();
fetchImpl = (url) => {
  if (url.includes('/api/known')) return errRes(500);
  if (url.includes('/api/ingest')) return jsonRes({ posts_new: 2, media_new: 2 });
  return jsonRes({});
};
await collect([post('1'), post('2')], {});
r = await flush();
check('known 失敗仍送出全部', r.sent, 2);

// --- 8. 溢位上限：每個帳號各自算 ---
await reset();
const manyA = Array.from({ length: 2100 }, (_, i) => post(String(1000000 + i), 'u1'));
const manyB = Array.from({ length: 5 }, (_, i) => post(String(9000000 + i), 'u2'));
await collect([...manyA, ...manyB], {});
let st = await getState();
check('u1 套用上限', countPending(st, 'u1'), 2000);
check('u2 不受 u1 影響', countPending(st, 'u2'), 5);
check('有記錄丟棄數', st.droppedOverflow, 100);
check('丟掉的是最舊的', st.pending.u1['1000000'], undefined);
check('保留最新的', st.pending.u1['1002099'] !== undefined, true);

// --- 9. ingest 失敗時 pending 不可被清掉 ---
await reset();
fetchImpl = (url) => {
  if (url.includes('/api/known')) return jsonRes({ known: [] });
  return errRes(500);
};
await collect([post('1')], {});
r = await flush();
check('ingest 失敗回報離線', r.online, false);
check('ingest 失敗 pending 保留', countPending(await getState()), 1);

// --- 10. 舊格式遷移（沒有分帳號的 pending）---
await reset();
store.syncState = {
  pending: { '111': post('111', 'u1'), '222': post('222', 'u2') },
  screenNames: { u1: 'alice', u2: 'bob' },
};
st = await getState();
check('舊格式已遷移總數', countPending(st), 2);
check('u1 歸戶正確', countPending(st, 'u1'), 1);
check('u2 歸戶正確', countPending(st, 'u2'), 1);

console.log(fail === 0 ? '\n全部通過' : `\n${fail} 項失敗`);
process.exit(fail === 0 ? 0 : 1);
