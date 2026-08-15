// 對真實 fixture 跑 content.js 的抽取邏輯（載入本體，不複製一份）。
// 用法: node extension/test_extract.mjs
//
// 驗的是解析層，不需要 Chrome。Chrome 只驗得了注入時機與同步。

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const fixture = JSON.parse(
  readFileSync(join(here, 'fixtures/x-usermedia-sample.json'), 'utf8')
);

// --- 把 fixture 包回真實的 GraphQL envelope ---
const envelope = {
  data: {
    user: {
      result: {
        rest_id: '2000000000000000002',
        legacy: { screen_name: 'sample_account' },
        timeline_v2: {
          timeline: {
            instructions: [
              {
                type: 'TimelineAddToModule',
                moduleItems: fixture.moduleItems_sample.map((s) => ({
                  entryId: s.entryId,
                  item: { itemContent: { tweet_results: { result: { legacy: s.legacy } } } },
                })),
              },
              {
                type: 'TimelineAddEntries',
                entries: [
                  { entryId: 'cursor-bottom-1', content: { cursorType: 'Bottom', value: 'x'.repeat(46) } },
                ],
              },
            ],
          },
        },
      },
    },
  },
};

// --- 用 stub 撐起 content.js 需要的執行環境 ---
const captured = [];
let messageHandler = null;

globalThis.chrome = {
  runtime: { sendMessage: async (m) => { captured.push(m); } },
};
globalThis.location = { pathname: '/sample_account/media', search: '?filter=photo' };
globalThis.window = {
  addEventListener: (ev, fn) => { if (ev === 'message') messageHandler = fn; },
};

new Function(readFileSync(join(here, 'content.js'), 'utf8'))();

messageHandler({
  source: globalThis.window,
  data: { __snsmediadl: true, op: 'UserMedia', text: JSON.stringify(envelope), via: 'xhr' },
});

await new Promise((r) => setTimeout(r, 20));

let fail = 0;
const check = (label, got, want) => {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) fail++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}: ${JSON.stringify(got)}${ok ? '' : ` (期望 ${JSON.stringify(want)})`}`);
};

// content.js 現在也會送診斷用的 report 訊息，要挑出真正的採集結果
const msg = captured.find((m) => m.type === 'captured');

if (!msg) {
  console.log('FAIL  完全沒有抽出任何東西');
  console.log('      收到的訊息型別：', captured.map((m) => m.type).join(', ') || '（無）');
  process.exit(1);
}

const posts = msg.posts;
const mediaCount = posts.reduce((n, p) => n + p.media.length, 0);

check('訊息型別', msg.type, 'captured');
check('抽出貼文數', posts.length, 2);
check('媒體總數', mediaCount, 3);
check('screen_name 對應', msg.screenNames['2000000000000000002'], 'sample_account');

const photoOnly = posts.find((p) => p.postId === '1000000000000000001');
check('純圖貼文的媒體數', photoOnly.media.length, 1);
check('圖片 orig 尺寸參數', photoOnly.media[0].orig.endsWith('?name=orig'), true);

const mixed = posts.find((p) => p.postId === '1000000000000000002');
check('圖+影貼文的媒體數', mixed.media.length, 2);

const vid = mixed.media.find((m) => m.kind === 'video');
check('影片挑到最高 bitrate', vid.bitrate, 2176000);
check('影片是 mp4 不是 m3u8', vid.url.endsWith('.mp4'), true);
check('可稽核的候選 bitrate', vid.availableBitrates, [256000, 832000, 2176000]);
check('影片縮圖有保留', vid.thumb.includes('amplify_video_thumb'), true);
check('影片長度', vid.durationMs, 9571);
check('possiblySensitive 有帶出', typeof mixed.possiblySensitive, 'boolean');
check('媒體頁可採集', msg.capturable, true);

// ── 轉推一律丟掉 ────────────────────────────────────────
//
// 轉推的 legacy.user_id_str 是**轉推者**，extended_entities.media 卻是
// **原作者的** —— 所以「只收本頁帳號」那道閘門對它是成立的，它會通過。
// 2026-08-16 實測：X 的 /reposts 分頁上，UserRepostsTimeline 一次就抽出
// 19 個別人的媒體。這裡把它擋在解析層。
captured.length = 0;
const rtLegacy = JSON.parse(JSON.stringify(fixture.moduleItems_sample[0].legacy));
rtLegacy.id_str = '1000000000000000009';
// 轉推者是本頁帳號（所以帳號閘門會放行），但媒體是別人的
rtLegacy.retweeted_status_result = { result: { legacy: { user_id_str: '9999', id_str: '777' } } };
const rtEnvelope = JSON.parse(JSON.stringify(envelope));
rtEnvelope.data.user.result.timeline_v2.timeline.instructions[0].moduleItems.push({
  entryId: 'rt-1',
  item: { itemContent: { tweet_results: { result: { legacy: rtLegacy } } } },
});
messageHandler({
  source: globalThis.window,
  data: { __snsmediadl: true, op: 'UserRepostsTimeline', text: JSON.stringify(rtEnvelope), via: 'xhr' },
});
await new Promise((r) => setTimeout(r, 20));
const rtMsg = captured.find((m) => m.type === 'captured');
check('轉推沒有被送出', rtMsg.posts.some((p) => p.postId === '1000000000000000009'), false);
check('其餘的照收', rtMsg.posts.length, 2);
const rtReport = captured.find((m) => m.type === 'report' && m.context?.retweetsDropped);
check('丟掉幾則要記下來', rtReport?.context.retweetsDropped, 1);
check('op 名稱有記進報告（改名時看得出來）', rtReport?.context.op, 'UserRepostsTimeline');

console.log(fail === 0 ? '\n全部通過' : `\n${fail} 項失敗`);
process.exit(fail === 0 ? 0 : 1);
