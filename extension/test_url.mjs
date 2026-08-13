// content.js 的網址解析：判斷「這個分頁在看誰」。
// 用法: node extension/test_url.mjs
//
// 這個檔案存在的原因：先前面板靠「攔截到貼文才知道帳號」，頁面走快取沒發請求時
// 會退回全域的 lastUserId，於是在 sample_account 的頁面顯示成 sample_streamer。
// 網址是不需要等任何請求的權威來源。

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));

// --- 撐起 content.js 需要的環境 ---
const notified = [];
const messages = [];
globalThis.chrome = { runtime: { sendMessage: async (m) => { messages.push(m); } } };
globalThis.window = {
  addEventListener: () => {},
  __SNSMediaDLBar: {
    setPageScreenName: (n) => notified.push(n),
    setTabAccount: () => {},
  },
};
globalThis.location = { pathname: '/' };
globalThis.document = { body: null, addEventListener: () => {} };

// setInterval 會讓 node 不結束，也會干擾測試
const timers = [];
globalThis.setInterval = (fn) => { timers.push(fn); return 0; };

new Function(readFileSync(join(here, 'content.js'), 'utf8'))();

const tick = () => timers.forEach((fn) => fn());

let fail = 0;
const check = (label, got, want) => {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) fail++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}: ${JSON.stringify(got)}${ok ? '' : ` (期望 ${JSON.stringify(want)})`}`);
};

const visit = (path) => {
  notified.length = 0;
  globalThis.location.pathname = path;
  tick();
  return notified.length ? notified[notified.length - 1] : '（沒有通知）';
};

// --- 帳號頁 ---
check('帳號媒體頁', visit('/sample_account/media'), 'sample_account');
check('帳號主頁', visit('/sample_streamer'), 'sample_streamer');
check('貼文詳情頁', visit('/someartist/status/12345'), 'someartist');
check('回覆頁', visit('/someartist/with_replies'), 'someartist');
check('喜歡頁', visit('/someartist/likes'), 'someartist');

// --- 非帳號頁：不可誤判成帳號 ---
check('首頁', visit('/home'), null);
check('探索', visit('/explore'), null);
check('通知', visit('/notifications'), null);
check('訊息', visit('/messages'), null);
check('設定', visit('/settings/profile'), null);
check('i 路徑', visit('/i/bookmarks'), null);
check('搜尋', visit('/search'), null);   // pathname 不含 query string
check('根路徑', visit('/'), null);

// --- SPA 內切換帳號要跟著變 ---
visit('/artist_a/media');
check('切到另一個帳號', visit('/artist_b/media'), 'artist_b');

// --- badge 是每分頁各自的：換帳號一定要通知 service worker ---
// 不通知的話數字會停在上一個帳號，直到剛好又攔到一個回應。
const changes = (path) => {
  messages.length = 0;
  visit(path);
  return messages.filter((m) => m.type === 'pageChanged').map((m) => m.pageScreenName);
};
check('換帳號有通知', changes('/artist_d/media'), ['artist_d']);
check('離開帳號頁也通知（數字要歸零）', changes('/home'), [null]);

// --- 同帳號內換分頁不重複通知 ---
visit('/artist_c');
notified.length = 0;
globalThis.location.pathname = '/artist_c';
tick();
check('路徑沒變就不通知', notified.length, 0);

console.log(fail === 0 ? '\n全部通過' : `\n${fail} 項失敗`);
process.exit(fail === 0 ? 0 : 1);
