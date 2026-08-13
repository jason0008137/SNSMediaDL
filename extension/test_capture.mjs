// 採集閘門：只收「本頁帳號」、每帳號隔離、標籤在送出時決定。
// 用法: node extension/test_capture.mjs
//
// 取代 test_recording.mjs（v0.7 錄製狀態機）。

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
  countPending, enqueue, flush, getState, resetState, userIdFor,
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

const jsonRes = (body) => ({ ok: true, status: 200, json: async () => body });
const okBackend = (url) => {
  if (url.includes('/api/known')) return jsonRes({ known: [] });
  return jsonRes({ posts_new: 1, media_new: 1 });
};
const ingestCalls = () => fetchLog
  .filter((f) => f.url.includes('/api/ingest'))
  .map((f) => JSON.parse(f.options.body));

const setTags = async (screenName, tags) => {
  const r = await chrome.storage.local.get('lastTags');
  await chrome.storage.local.set({
    lastTags: { ...(r.lastTags || {}), [screenName.toLowerCase()]: tags },
  });
};

// --- 1. 不在帳號頁面 -> 一則都不收 ---
// /home、/explore 上滑過的東西不是「你想要的」，是「剛好經過的」。
await reset();
let r = await enqueue([post('1'), post('2')], { u1: 'alice' }, null);
check('非帳號頁收 0 則', r.taken, 0);
check('非帳號頁略過 2 則', r.skipped, 2);
check('佇列是空的', countPending(await getState()), 0);
check('screenName 對應照樣留著', (await getState()).screenNames.u1, 'alice');
check('非帳號頁不算 unresolved', (await getState()).unresolved, null);

// --- 2. 在帳號頁面 -> 只收這一頁的帳號 ---
await reset();
r = await enqueue(
  [post('1', 'u1'), post('2', 'u2'), post('3', 'u1')],
  { u1: 'alice', u2: 'bob' }, 'alice',
);
check('只收本頁帳號', r.taken, 2);
check('別人的被略過', r.skipped, 1);
check('badge 用的每頁數字', r.pendingForPage, 2);
check('u1 進佇列', countPending(await getState(), 'u1'), 2);
check('u2 沒進佇列', countPending(await getState(), 'u2'), 0);

// --- 3. hover card / 側欄推薦進不來（沙盤 D）---
// 滑過 bob 的頭像會帶進 bob 的 user 物件與貼文，但本頁仍是 alice。
r = await enqueue([post('9', 'u2')], { u2: 'bob', u9: 'carol' }, 'alice');
check('hover 到的貼文沒收', r.taken, 0);
check('u2 還是不在佇列', countPending(await getState(), 'u2'), 0);
check('對應本身照收（backend 需要名字）', (await getState()).screenNames.u9, 'carol');

// --- 4. 網址大小寫不影響對應 ---
await reset();
r = await enqueue([post('1', 'u1')], { u1: 'Alice' }, 'alice');
check('大小寫不敏感', r.taken, 1);
check('userIdFor 反查', userIdFor(await getState(), 'ALICE'), 'u1');

// --- 5. 在帳號頁卻對不出 userId -> 必須留下痕跡，不可靜默 ---
// 症狀是「滑了半天數字一直 0」，本身沒有任何線索。
await reset();
r = await enqueue([post('1', 'u1'), post('2', 'u1')], {}, 'alice');
check('對不出來就收 0 則', r.taken, 0);
let st = await getState();
check('記下對不出來的帳號', st.unresolved?.screenName, 'alice');
check('記下略過幾則', st.unresolved?.count, 2);

// 對應補起來之後要自己清掉，否則錯誤訊息會永遠掛著
r = await enqueue([post('3', 'u1')], { u1: 'alice' }, 'alice');
check('補上對應就收得到', r.taken, 1);
check('unresolved 已清除', (await getState()).unresolved, null);

// --- 6. 每帳號隔離：在 B 設標籤不會動到 A（使用者最擔心的事）---
await reset();
await enqueue([post('1', 'u1')], { u1: 'alice' }, 'alice');
await enqueue([post('2', 'u2')], { u2: 'bob' }, 'bob');
await setTags('alice', { rating: 'sfw', contentType: 'illust' });
await setTags('bob', { rating: 'r18' });

fetchImpl = okBackend;
await flush();
let byName = Object.fromEntries(ingestCalls().map((c) => [c.screenName, c.posts[0]]));
check('分成兩次 ingest', ingestCalls().length, 2);
check('alice 是 sfw', byName.alice.rating, 'sfw');
check('alice 的 contentType 也對', byName.alice.contentType, 'illust');
check('bob 是 r18', byName.bob.rating, 'r18');
check('bob 沒被 alice 的 contentType 污染', byName.bob.contentType, undefined);

// --- 7. 送出當下的下拉值（override）優先於記憶值 ---
// 面板送出時帶的是「你現在看到的那兩個下拉」，所見即所送。
await reset();
await enqueue([post('1', 'u1')], { u1: 'alice' }, 'alice');
await setTags('alice', { rating: 'sfw' });
fetchImpl = okBackend;
await flush('u1', { rating: 'r18', contentType: 'ai' });
check('override 蓋過記憶值', ingestCalls()[0].posts[0].rating, 'r18');
check('override 的 contentType 也送出', ingestCalls()[0].posts[0].contentType, 'ai');

// --- 8. override 只作用在指定帳號，全送時不可外溢 ---
await reset();
await enqueue([post('1', 'u1')], { u1: 'alice' }, 'alice');
await enqueue([post('2', 'u2')], { u2: 'bob' }, 'bob');
await setTags('bob', { rating: 'r18' });
fetchImpl = okBackend;
await flush(null, { rating: 'sfw' });   // 不指定帳號 -> override 一律忽略
byName = Object.fromEntries(ingestCalls().map((c) => [c.screenName, c.posts[0]]));
check('全送時 alice 沒被塞標籤', byName.alice.rating, undefined);
check('全送時 bob 用自己的記憶值', byName.bob.rating, 'r18');

// --- 9. 沒設過標籤就不亂填 ---
await reset();
await enqueue([post('1', 'u1')], { u1: 'alice' }, 'alice');
fetchImpl = okBackend;
await flush('u1');
check('沒有標籤就不帶欄位', ingestCalls()[0].posts[0].rating, undefined);
check('完全沒打過帳號預設值端點',
  fetchLog.filter((f) => f.url.includes('/defaults')).length, 0);

// --- 10. 溢位丟棄仍然要記（沙盤 G）---
await reset();
await enqueue(
  Array.from({ length: 2100 }, (_, i) => post(String(1000000 + i), 'u1')),
  { u1: 'alice' }, 'alice',
);
st = await getState();
check('套用每帳號上限', countPending(st, 'u1'), 2000);
check('有記錄丟棄數', st.droppedOverflow, 100);
check('丟掉的是最舊的', st.pending.u1['1000000'], undefined);

// --- 11. v0.7 的錄製欄位要被清掉，不留兩套真相 ---
await reset();
store.syncState = {
  pending: { u1: { 1: post('1', 'u1') } },
  screenNames: { u1: 'alice' },
  recording: { userId: 'u1', count: 3 },
  lastRecorded: { userId: 'u1' },
  skippedWhileIdle: 7,
  pendingTags: { u1: { rating: 'r18' } },
};
st = await getState();
check('佇列本身保留', countPending(st, 'u1'), 1);
check('recording 已清', st.recording, undefined);
check('lastRecorded 已清', st.lastRecorded, undefined);
check('skippedWhileIdle 已清', st.skippedWhileIdle, undefined);
check('pendingTags 已清', st.pendingTags, undefined);

console.log(fail === 0 ? '\n全部通過' : `\n${fail} 項失敗`);
process.exit(fail === 0 ? 0 : 1);
