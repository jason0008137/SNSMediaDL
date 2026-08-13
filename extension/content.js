// 跑在 ISOLATED world，run_at: document_start。
// 收 inject.js 從 MAIN world 丟過來的 GraphQL 回應，抽出媒體後交給 service worker。

const TAG = '[SNSMediaDL]';

// legacy 有時在 result.legacy，有時在 result.tweet.legacy（受限貼文）
const legacyOf = (r) => r?.legacy || r?.tweet?.legacy || null;

/** 從 timeline instructions 收集所有 tweet 的 legacy 物件。
 *  UserTweets 用 TimelineAddEntries，UserMedia 用 TimelineAddToModule —— 兩種都要處理。 */
function collectLegacies(instructions) {
  const out = [];
  const cursors = [];

  const fromItemContent = (ic) => {
    const lg = legacyOf(ic?.tweet_results?.result);
    if (lg) out.push(lg);
  };

  for (const ins of instructions || []) {
    // UserMedia：媒體在 moduleItems，entries 裡只有 cursor
    for (const mi of ins.moduleItems || []) {
      fromItemContent(mi.item?.itemContent);
    }

    for (const e of ins.entries || []) {
      if (e.entryId?.startsWith('cursor')) {
        cursors.push({ type: e.content?.cursorType, value: e.content?.value });
        continue;
      }
      fromItemContent(e.content?.itemContent);
      for (const it of e.content?.items || []) {
        fromItemContent(it.item?.itemContent);
      }
    }
  }
  return { legacies: out, cursors };
}

/** 影片挑最高 bitrate 的 mp4（m3u8 沒有 bitrate，排除） */
function bestVariant(variants = []) {
  const mp4 = variants.filter((v) => v.content_type === 'video/mp4' && v.bitrate != null);
  if (!mp4.length) return null;
  return mp4.reduce((a, b) => (b.bitrate > a.bitrate ? b : a));
}

function extractPost(lg) {
  const media = [];
  for (const m of lg.extended_entities?.media || []) {
    if (m.type === 'photo') {
      media.push({ kind: 'photo', url: m.media_url_https, orig: `${m.media_url_https}?name=orig` });
    } else {
      const variants = m.video_info?.variants || [];
      const v = bestVariant(variants);
      if (!v) {
        // 只有 m3u8、沒有 mp4 的影片。目前無法處理（backend 不會拆 HLS），
        // 但**不可以靜默略過** —— 記一筆，讓「到底有沒有這種東西」
        // 從通靈變成可查（沙盤 H，也是 91 vs 92 落差的懷疑對象之一）。
        tell('error', '影片沒有可用的 mp4 variant，已略過', lg.id_str, {
          postId: lg.id_str,
          types: variants.map((x) => x.content_type),
        });
      }
      if (v) {
        media.push({
          kind: m.type, // video | animated_gif
          url: v.url,
          bitrate: v.bitrate,
          // 留下所有候選 bitrate，讓「有沒有挑到最高」可以事後稽核。
          availableBitrates: variants
            .filter((x) => x.content_type === 'video/mp4')
            .map((x) => x.bitrate)
            .sort((a, b) => a - b),
          thumb: m.media_url_https,
          durationMs: m.video_info?.duration_millis,
        });
      }
    }
  }
  if (!media.length) return null;
  return {
    postId: lg.id_str,
    createdAt: lg.created_at,
    userId: lg.user_id_str,
    isRetweet: !!lg.retweeted_status_result, // ⚠️ v1.1 叫 retweeted_status，這裡多了 _result
    // 弱訊號，backend 只拿來當 rating 的 auto 猜測，不當權威值
    possiblySensitive: !!lg.possibly_sensitive,
    media,
  };
}

/** 從任意回應裡撈 user 物件，建立 userId -> screenName 的權威對應。
 *  比從 URL 猜可靠 —— URL 是使用者當下在看的頁面，未必等於貼文作者。 */
function collectScreenNames(json) {
  const map = {};
  const visit = (node, depth) => {
    if (!node || typeof node !== 'object' || depth > 8) return;
    if (node.rest_id && node.core?.screen_name) {
      map[String(node.rest_id)] = node.core.screen_name;
    }
    if (node.rest_id && node.legacy?.screen_name) {
      map[String(node.rest_id)] = node.legacy.screen_name;
    }
    for (const v of Object.values(node)) {
      if (v && typeof v === 'object') visit(v, depth + 1);
    }
  };
  visit(json, 0);
  return map;
}

function handle(op, text) {
  let json;
  try {
    json = JSON.parse(text);
  } catch {
    return;
  }

  const screenNames = collectScreenNames(json);

  const result = json?.data?.user?.result;
  const tl = result?.timeline_v2 || result?.timeline;
  const instructions = tl?.timeline?.instructions;

  if (!instructions) {
    // 沒有 timeline 的回應（例如 UserByScreenName、hover card）仍可能帶
    // screen_name，光是這個對應就值得送回去 —— 本頁帳號的 userId
    // 正是靠它才解析得出來。
    //
    // ⚠️ 先前這裡還會「只有一個 user 就當成本分頁的帳號」。那是錯的：
    // 滑過任何人的頭像都會發 UserByScreenName（查的是被滑到的人）。
    // 現在「這一頁在看誰」只由網址決定，這條路徑不再影響任何判斷。
    if (Object.keys(screenNames).length) {
      chrome.runtime.sendMessage({
        type: 'captured', posts: [], screenNames, pageScreenName: screenNameFromUrl(),
      }).catch(() => {});
    }
    return;
  }

  const { legacies } = collectLegacies(instructions);
  const posts = legacies.map(extractPost).filter(Boolean);
  const mediaCount = posts.reduce((n, p) => n + p.media.length, 0);

  console.log(`${TAG} ${op}: ${posts.length} posts / ${mediaCount} media`);
  tell('info', '攔截到 GraphQL 回應', op,
    { op, posts: posts.length, media: mediaCount, path: location.pathname });

  // 收不收由 sync.js 判斷（只收 pageScreenName 這個帳號的貼文）。
  // 判斷點只有一個，兩邊各判一次遲早會不一致 —— 這裡只負責把
  // 「網址說這一頁在看誰」誠實地帶過去。
  chrome.runtime.sendMessage({
    type: 'captured', posts, screenNames, pageScreenName: screenNameFromUrl(),
  })
    .then((r) => { if (r) window.__SNSMediaDLBar?.onCaptured(r); })
    .catch(() => { /* service worker 可能還沒醒，資料已在下次一併送出 */ });
}

// x.com 上這些第一段路徑不是帳號
const NON_ACCOUNT_PATHS = new Set([
  'home', 'explore', 'notifications', 'messages', 'i', 'settings', 'compose',
  'search', 'hashtag', 'tos', 'privacy', 'login', 'signup', 'about', 'download',
  'jobs', 'topics', 'lists', 'bookmarks', 'communities', 'premium_sign_up',
]);

/** 從網址判斷「這個分頁在看誰」。
 *
 * 網址是最權威的答案，而且不用等任何請求 —— 先前靠「攔截到貼文才知道」，
 * 頁面走快取沒發請求時就會退回全域的 lastUserId，顯示成別的分頁的帳號。 */
function screenNameFromUrl() {
  const seg = location.pathname.split('/').filter(Boolean)[0];
  if (!seg || NON_ACCOUNT_PATHS.has(seg.toLowerCase())) return null;
  return seg;
}

let lastPath = null;

function checkUrl() {
  if (location.pathname === lastPath) return;
  lastPath = location.pathname;
  const name = screenNameFromUrl();
  window.__SNSMediaDLBar?.setPageScreenName(name);
  // 工具列 badge 是每分頁各自的，換帳號要立刻換數字 ——
  // 不通知的話它會停在上一個帳號的數字直到下一次攔到回應。
  chrome.runtime.sendMessage({ type: 'pageChanged', pageScreenName: name })
    .catch(() => { /* service worker 可能還沒醒 */ });
}

// x.com 是 SPA，換帳號不會重載頁面，所以要輪詢網址
checkUrl();
setInterval(checkUrl, 1000);

function tell(level, event, detail, context = {}) {
  chrome.runtime.sendMessage({
    type: 'report', level, event, detail, context, where: 'content',
  }).catch(() => {});
}

window.addEventListener('error', (e) => {
  tell('error', 'content script 未攔截的例外', String(e.message),
    { file: e.filename, line: e.lineno });
});

window.addEventListener('message', (ev) => {
  if (ev.source !== window) return;
  const d = ev.data;
  if (!d || d.__snsmediadl !== true) return;
  handle(d.op, d.text);
});

// 工具列。document_start 時 body 還不存在，所以等 DOM 就緒再掛。
function mountBarWhenReady() {
  const bar = window.__SNSMediaDLBar;
  if (!bar) return;
  if (document.body) bar.mount();
  else document.addEventListener('DOMContentLoaded', () => bar.mount(), { once: true });
}
mountBarWhenReady();

console.log(`${TAG} ISOLATED world bridge ready`);
