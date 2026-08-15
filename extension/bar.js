// x.com 頁面上的浮動面板。由 content.js 呼叫 mount()。
//
// 為什麼是「浮動小鈕 + 展開面板」而不是頂端固定列：
// 固定列會蓋住 x.com 的內容。收合時只剩一顆小鈕，佔用面積接近零。
//
// 為什麼不插進 x.com 的 DOM 流：那是 React + 大量 fixed 定位，
// 插進去會被重繪清掉，也可能弄壞它的版面。浮動不依賴對方的 DOM 結構。
//
// content_scripts 不支援 ES module，所以掛在 isolated world 的 window 上。

const HOST_ID = 'snsmediadl-panel';

const CSS = `
:host { all: initial; }
* { box-sizing: border-box; }

.root {
  position: fixed; z-index: 2147483647;
  font: 12px/1.45 system-ui, "Noto Sans TC", sans-serif;
  color: #e7e9ea;
}

/* 收合態：一顆小鈕，不擋內容 */
.fab {
  width: 44px; height: 44px; border-radius: 50%;
  background: #1d9bf0; color: #fff; border: 0; cursor: pointer;
  box-shadow: 0 3px 12px rgba(0,0,0,.45);
  display: flex; align-items: center; justify-content: center;
  font-size: 16px; font-weight: 700; position: relative;
}
.fab:hover { filter: brightness(1.1); }
.fab.dl { background: #00ba7c; }
.fab .badge {
  position: absolute; top: -4px; right: -4px;
  min-width: 20px; height: 20px; padding: 0 5px; border-radius: 10px;
  background: #ff7a00; color: #fff; font-size: 11px;
  display: flex; align-items: center; justify-content: center;
}
.fab .badge.hidden { display: none; }

.panel {
  width: 320px; background: #15202b;
  border: 1px solid #38444d; border-radius: 12px;
  box-shadow: 0 8px 28px rgba(0,0,0,.5);
  overflow: hidden;
}
.head {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 10px; background: #1c2b38; cursor: move; user-select: none;
}
.head .title { font-weight: 700; color: #1d9bf0; flex: 1; }
.head button {
  background: none; border: 0; color: #8b98a5; cursor: pointer;
  font-size: 15px; padding: 2px 6px;
}
.body { padding: 10px; display: flex; flex-direction: column; gap: 8px; }

.dot { width: 8px; height: 8px; border-radius: 50%; background: #536471; flex: none; }
.dot.on { background: #00ba7c; }
.dot.off { background: #f4212e; }

.row { display: flex; gap: 6px; align-items: center; }
.acct { font-weight: 700; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.count { font-variant-numeric: tabular-nums; font-weight: 700; }
.count.has { color: #ff7a00; }

select {
  flex: 1; min-width: 0; background: #0e1621; color: #e7e9ea;
  border: 1px solid #38444d; border-radius: 6px; padding: 4px 6px;
  font-size: 12px; font-family: inherit;
}
select:disabled { opacity: .4; }

button.act {
  background: #1d9bf0; color: #fff; border: 0; border-radius: 6px;
  padding: 6px 10px; font-size: 12px; cursor: pointer; font-family: inherit;
}
button.act:hover { filter: brightness(1.1); }
button.act:disabled { opacity: .4; cursor: default; }

.msg { font-size: 11px; color: #8b98a5; min-height: 15px; word-break: break-all; }
.msg.err { color: #f4212e; }
.msg.ok { color: #00ba7c; }
.msg.warn { color: #ff7a00; }
.hint { font-size: 11px; color: #8b98a5; }
.hidden { display: none !important; }
`;

const HTML = `
<div class="root" id="root">
  <button class="fab" id="fab" title="SNSMediaDL">
    <span id="fabIcon">⬇</span><span class="badge hidden" id="fabBadge">0</span>
  </button>

  <div class="panel hidden" id="panel">
    <div class="head" id="head">
      <span class="dot" id="dot"></span>
      <span class="title">SNSMediaDL</span>
      <button id="collapse" title="收合">－</button>
    </div>
    <div class="body">
      <div class="row">
        <span class="acct" id="acct">—</span>
        <span style="flex:1"></span>
        <span class="hint">待送</span><span class="count" id="count">0</span>
      </div>

      <div id="form">
        <div class="row" style="margin-bottom:6px">
          <select id="rating">
            <option value="">分級（未設）</option>
            <option value="sfw">sfw</option>
            <option value="r18">r18</option>
          </select>
          <select id="content">
            <option value="">類型（未設）</option>
            <option value="illust">illust</option>
            <option value="irl">irl</option>
            <option value="mod">mod</option>
            <option value="ai">ai</option>
            <option value="3d">3d</option>
            <option value="photograph">photograph</option>
            <option value="other">other</option>
          </select>
        </div>
        <div class="row">
          <select id="creator"><option value="">creator（未歸屬）</option></select>
          <select id="role">
            <option value="">角色</option>
            <option value="main">main</option>
            <option value="alt">alt</option>
            <option value="r18_alt">r18_alt</option>
          </select>
        </div>
      </div>

      <div class="row">
        <button class="act" id="primary" style="flex:1">送出並下載</button>
      </div>

      <div class="hint hidden" id="others"></div>
      <div class="msg" id="msg"></div>
    </div>
  </div>
</div>
`;

let shadow = null;
let account = null;          // backend 的 account 物件
let pageScreenName = null;   // 從網址判斷的帳號 —— 這是權威來源
let expanded = false;
let creatorSig = '';        // options 只在真的變動時重建
let timer = null;
let downloading = null;     // 送出後的下載進度 { pending, done, failed }

const $ = (id) => shadow.getElementById(id);

/** 對 backend 的請求一律走 service worker。
 *
 * ⚠️ 不可以在這裡直接 fetch：content script 的跨來源請求帶的是頁面的 origin
 * （x.com），受頁面 CORS 管，會被擋掉並出現 "Failed to fetch"。
 * service worker 的 origin 是 chrome-extension://，且有 host_permissions。 */
async function api(path, options = {}) {
  const r = await chrome.runtime.sendMessage({ type: 'apiFetch', path, options });
  if (!r) throw new Error('service worker 無回應');
  if (!r.ok) throw new Error(r.error || `HTTP ${r.status}`);
  return r.data;
}

function tell(level, event, detail, context = {}) {
  chrome.runtime.sendMessage({
    type: 'report', level, event, detail, context, where: 'bar',
  }).catch(() => {});
}

const jsonBody = (body, method = 'POST') => ({ method, body });

function say(text, cls = '') {
  $('msg').textContent = text;
  $('msg').className = `msg ${cls}`;
  if (text && cls === 'ok') {
    setTimeout(() => { if ($('msg').textContent === text) $('msg').textContent = ''; }, 2500);
  }
}

/** 網址變了（含 SPA 內部導航）。由 content.js 呼叫。
 *
 * 網址是「這個分頁在看誰」的權威來源 —— 不依賴有沒有攔到請求，
 * 也不會被其他分頁影響。面板上的每一個數字、標籤、送出目標都掛在它身上。 */
function setPageScreenName(name, opts = {}) {
  const nextCapturable = opts.capturable !== false;
  if (name === pageScreenName && nextCapturable === capturable) return;
  const sameAccount = name === pageScreenName;
  pageScreenName = name;
  capturable = nextCapturable;
  mediaUrl = opts.mediaUrl || null;
  if (!sameAccount) {
    account = null;      // 換帳號了，強制重讀
    creatorSig = '';
  }
  refresh();
}

/** 每次採集回報後即時更新，不必等 5 秒輪詢 —— 數字要跟著滑動跳。 */
function onCaptured() {
  if (shadow) refresh();
}

// 這一頁能不能採集（媒體分頁才行）。與 pageScreenName 是兩回事 ——
// 站在 /reposts 上「這一頁在看誰」有答案，但那裡的東西不是他的作品。
let capturable = true;
let mediaUrl = null;

// ── 展開 / 收合 ──────────────────────────────────────
async function setExpanded(on) {
  expanded = on;
  $('panel').classList.toggle('hidden', !on);
  $('fab').classList.toggle('hidden', on);
  // 面板比 FAB 大得多，展開瞬間可能超出視窗（FAB 被拖到左上角時）
  if (on) clampToViewport();
  await chrome.storage.local.set({ panelExpanded: on });
  restartTimer();
  if (on) refresh();
}

function restartTimer() {
  if (timer) clearInterval(timer);
  // 展開時要跟得上，收合時沒必要一直打
  timer = setInterval(refresh, expanded ? 5000 : 10000);
}

// ── 拖曳與定位 ───────────────────────────────────────
// 錨定一律是「距右、距下」—— FAB 與面板**共用右下角**這一點：
// 收合 = FAB 出現在面板的右下角，展開 = 面板從 FAB 往左上長。
//
// ⚠️ 先前拖曳後存的是 left/top（左上角錨定），於是收合時 FAB 跳到
// 面板的左上角、展開又往右下長 —— 重複收放，FAB 一路往左上漂移。

function applyAnchor(root, pos) {
  root.style.right = `${Math.max(4, pos.right)}px`;
  root.style.bottom = `${Math.max(4, pos.bottom)}px`;
  root.style.left = 'auto';
  root.style.top = 'auto';
}

/** 把目前的實際位置換算成右下錨定並存起來。 */
function saveAnchor(root) {
  const r = root.getBoundingClientRect();
  const pos = {
    right: Math.max(4, Math.round(window.innerWidth - r.right)),
    bottom: Math.max(4, Math.round(window.innerHeight - r.bottom)),
  };
  applyAnchor(root, pos);
  chrome.storage.local.set({ panelPos: pos });
}

/** 展開後若面板超出視窗左緣或上緣，把錨點拉回來。 */
function clampToViewport() {
  const root = $('root');
  const r = root.getBoundingClientRect();
  if (r.left >= 4 && r.top >= 4) return;
  applyAnchor(root, {
    right: Math.min(parseFloat(root.style.right) || 18,
      Math.max(4, window.innerWidth - r.width - 4)),
    bottom: Math.min(parseFloat(root.style.bottom) || 18,
      Math.max(4, window.innerHeight - r.height - 4)),
  });
}

// FAB 拖曳後緊接著的 click 要吞掉一次，否則放開手就展開面板
let suppressClick = false;

/** 讓 handle 可以拖動 root。handle 同時是按鈕時（FAB），
 *  以 4px 位移門檻區分「點擊」與「拖曳」。 */
function enableDrag(root, handle, { isButton = false } = {}) {
  let pressed = false;
  let moved = false;
  let ox = 0;
  let oy = 0;
  let w = 0;
  let h = 0;
  let sx = 0;
  let sy = 0;

  handle.addEventListener('mousedown', (e) => {
    pressed = true;
    moved = false;
    const r = root.getBoundingClientRect();
    ox = e.clientX - r.left;
    oy = e.clientY - r.top;
    w = r.width;
    h = r.height;
    sx = e.clientX;
    sy = e.clientY;
    e.preventDefault();
  });

  document.addEventListener('mousemove', (e) => {
    if (!pressed) return;
    if (!moved) {
      if (Math.abs(e.clientX - sx) < 4 && Math.abs(e.clientY - sy) < 4) return;
      moved = true;
    }
    // 拖曳過程用 left/top 平滑跟手，放開時 saveAnchor 再換算回右下錨定
    const x = Math.max(4, Math.min(window.innerWidth - w - 4, e.clientX - ox));
    const y = Math.max(4, Math.min(window.innerHeight - h - 4, e.clientY - oy));
    root.style.left = `${x}px`;
    root.style.top = `${y}px`;
    root.style.right = 'auto';
    root.style.bottom = 'auto';
  });

  document.addEventListener('mouseup', () => {
    if (!pressed) return;
    pressed = false;
    if (!moved) return;
    if (isButton) suppressClick = true;
    saveAnchor(root);
  });
}

// ── 掛載 ─────────────────────────────────────────────
function mount() {
  if (document.getElementById(HOST_ID)) return;

  const host = document.createElement('div');
  host.id = HOST_ID;
  shadow = host.attachShadow({ mode: 'open' });
  const style = document.createElement('style');
  style.textContent = CSS;
  shadow.append(style);
  const wrap = document.createElement('div');
  wrap.innerHTML = HTML;
  shadow.append(wrap);
  (document.body || document.documentElement).append(host);

  const root = $('root');
  applyAnchor(root, { right: 18, bottom: 18 });

  $('fab').addEventListener('click', () => {
    if (suppressClick) { suppressClick = false; return; }   // 剛拖完，不是點擊
    setExpanded(true);
  });
  $('collapse').addEventListener('click', () => setExpanded(false));
  enableDrag(root, $('head'));
  enableDrag(root, $('fab'), { isButton: true });   // 收合態也要能拖

  $('primary').addEventListener('click', doSend);

  // 分級與類型是「送這個帳號時要蓋上的標籤」，不是帳號預設值 —— 只記在本機，
  // 送出時隨 payload 一起走（見 sync.js flushAccount 的註解）。
  for (const id of ['rating', 'content']) $(id).addEventListener('change', saveTags);
  for (const id of ['creator', 'role']) $(id).addEventListener('change', saveLink);

  chrome.storage.local.get(['panelExpanded', 'panelPos']).then((r) => {
    const p = r.panelPos;
    if (typeof p?.right === 'number') {
      applyAnchor(root, p);
    } else if (p?.left) {
      // 舊格式存的是左上角。先套上，再由 saveAnchor 換算成右下錨定回存
      root.style.left = p.left;
      root.style.top = p.top;
      root.style.right = 'auto';
      root.style.bottom = 'auto';
      saveAnchor(root);
    }
    setExpanded(!!r.panelExpanded);
  });

  refresh();
  restartTimer();
}

// ── 送出 ─────────────────────────────────────────────

async function doSend() {
  const uid = targetUserId();
  if (!uid || pendingCount === 0) return;

  $('primary').disabled = true;
  say('送出中…');
  try {
    // sendMessage 本身也可能 reject（service worker 正在重啟）——
    // 不接的話錯誤只進 unhandledrejection，訊息停在「送出中…」
    const r = await chrome.runtime
      .sendMessage({
        type: 'syncNow',
        userId: uid,
        // 所見即所送：帶的是此刻下拉的值，不是任何「先前凍結」的東西
        tags: { rating: $('rating').value || null, contentType: $('content').value || null },
      })
      .catch((e) => ({ online: false, error: String(e?.message || e) }));
    if (!r?.online) {
      say(`失敗：${r?.error || '離線'}`, 'err');
      return;
    }
    if (r.downloadError) {
      // 已入庫但沒能啟動下載。這件事必須說出來 ——
      // 先前它是靜默的，使用者看到綠色訊息卻永遠等不到檔案。
      say(`已入庫 ${r.sent} 則，但無法啟動下載：${r.downloadError}`, 'err');
      return;
    }
    // backend 順手把匯入的舊帳號（只有名字、沒有平台 id）補上真實 id 時，
    // **一定要講** —— 那是把幾千筆歷史記錄重新歸戶，使用者按一次送出就發生了。
    const healed = (r.healed || [])
      .map((h) => `@${h.screen_name}（併入 ${h.posts} 則舊記錄）`).join('、');
    if (!r.sent) {
      say(healed ? `沒有新資料，但補上了 ${healed} 的平台 id` : '沒有新資料（可能都抓過了）', 'ok');
      return;
    }
    say(`已送出 ${r.sent} 則${healed ? `；補上了 ${healed} 的平台 id` : ''}，開始下載…`, 'ok');
    watchDownload();
  } finally {
    $('primary').disabled = false;
    refresh();
  }
}

/** 送出後盯著下載進度。歸零就停 —— 沒有進度顯示的話，
 *  使用者無從分辨「正在下載」與「根本沒開始」。 */
function watchDownload() {
  let ticks = 0;
  let misses = 0;
  const poll = async () => {
    const r = await chrome.runtime
      .sendMessage({ type: 'queueStatus' })
      .catch(() => null);
    if (!r?.ok) {
      // 單次失敗不放棄（backend 忙碌中查詢可能超時），連續失敗才收尾 ——
      // 先前失敗一次就 return，downloading 殘留非 null，
      // renderNotices 從此被它擋住，面板再也不顯示任何提示。
      misses += 1;
      if (misses < 5) { setTimeout(poll, 2000); return; }
      downloading = null;   // 不經 finishDownload：舊資料的 last_run 會蓋掉警告
      say('查不到下載進度 —— backend 可能已關閉', 'warn');
      refresh();
      return;
    }
    misses = 0;
    downloading = r.data;
    ticks += 1;
    renderDownload();
    const busy = downloading.running || downloading.pending > 0
      || downloading.downloading > 0;
    // 上限只是保險：正常情況下 busy 會轉成 false
    if (busy && ticks < 600) setTimeout(poll, 1000);
    else finishDownload();
  };
  poll();
}

function finishDownload() {
  const last = downloading?.last_run;
  if (last) {
    const parts = [`下載完成 ${last.done}`];
    if (last.skipped) parts.push(`已存在 ${last.skipped}`);
    if (last.failed) parts.push(`失敗 ${last.failed}`);
    say(parts.join(' / '), last.failed ? 'err' : 'ok');
  }
  downloading = null;
  refresh();
}

function renderDownload() {
  if (!downloading) return;
  const left = (downloading.pending || 0) + (downloading.downloading || 0);
  say(`下載中… 剩 ${left}`, '');
  $('fab').classList.add('dl');
  $('fabIcon').textContent = '⬇';
  $('fabBadge').textContent = left;
  $('fabBadge').classList.toggle('hidden', !left);
}

async function saveTags() {
  const tags = {
    rating: $('rating').value || null,
    contentType: $('content').value || null,
  };
  const key = (pageScreenName || '').toLowerCase();
  if (key) {
    const r = await chrome.storage.local.get('lastTags');
    await chrome.storage.local.set({ lastTags: { ...(r.lastTags || {}), [key]: tags } });
  }
  say('標籤已設，送出這個帳號時套用', 'ok');
}

async function saveLink() {
  if (!account) return;
  try {
    const cid = $('creator').value;
    if (!cid) {
      await api(`/api/accounts/${account.id}/link`, { method: 'DELETE' });
      say('已解除歸屬', 'ok');
    } else {
      await api(`/api/accounts/${account.id}/link`,
        jsonBody({ creator_id: Number(cid), role: $('role').value || null }));
      say('已更新關聯帳號', 'ok');
    }
  } catch (e) { say(`存檔失敗：${e.message}`, 'err'); }
}

/** 只在使用者沒在操作該元素時才寫入 —— 否則輪詢會蓋掉正在選的值 */
function setIfIdle(id, value) {
  const el = $(id);
  if (el !== shadow.activeElement) el.value = value || '';
}

let pendingCount = 0;
let pageUserId = null;      // 本頁帳號的 userId（由 state.screenNames 反查）

/** 送出目標**永遠是這一頁的帳號**，沒有第二種可能。
 *
 * 它的 id 有兩個來源，都指向同一個人：
 *   1. state.screenNames —— 攔到的回應建立的對應（新帳號也有）
 *   2. account.platform_user_id —— backend 認識這個帳號時才有
 * ⚠️ 不可以只靠 2：帳號要先在 DB 才查得到，而「第一次收一個新帳號」
 * 正是主要使用情境 —— 只靠它的話那時佇列還在但送出入口會消失。 */
function targetUserId() {
  return pageUserId || account?.platform_user_id || null;
}

async function refresh() {
  if (!shadow) return;

  let info;
  try {
    info = await chrome.runtime.sendMessage({
      type: 'getState', withPing: expanded, userId: null,
    });
  } catch { return; }   // service worker 睡著，下一輪再試

  const state = info.state;

  // 顯示的帳號一律以網址為準 —— 絕不退回全域的 lastUserId，
  // 那會顯示成別的分頁正在看的帳號。
  $('acct').textContent = pageScreenName ? `@${pageScreenName}` : '不在帳號頁面';

  $('dot').className = 'dot' + (state.online === true ? ' on'
    : state.online === false ? ' off' : '');

  // ⚠️ 數字一律是「**這一頁的帳號**待送幾則」，絕不是全帳號總和。
  // 總和會讓分好的資料看起來像大混池（A 頁 6、B 頁 10 卻顯示 16），
  // 使用者由此合理推論「在 B 設標籤會影響 16 則」—— 實際不會，
  // 但顯示既然那樣講，錯的是顯示。收合時也要算，FAB 上就有數字。
  pageUserId = userIdFor(state, pageScreenName);
  const uid = targetUserId();
  pendingCount = uid ? Object.keys(state.pending?.[uid] || {}).length : 0;

  if (!downloading) {
    $('fab').classList.remove('dl');
    $('fabIcon').textContent = '⬇';
    $('fabBadge').textContent = pendingCount;
    $('fabBadge').classList.toggle('hidden', !pendingCount);
  }

  if (!expanded) return;   // 收合時不用打 backend 拉帳號資料

  $('count').textContent = pendingCount;
  $('count').className = pendingCount ? 'count has' : 'count';

  renderPrimary();
  renderOthers(state);

  // 分級與類型永遠可用 —— 它們是「送這批時要蓋的標籤」，不需要帳號已在 DB。
  // 這正是「新帳號第一批必然 rating=NULL」的根因解（沙盤 A）。
  $('rating').disabled = false;
  $('content').disabled = false;

  await renderAccountArea(state);
  renderNotices(state);
}

/** screenName -> userId。與 sync.js 的 userIdFor 同一套規則。
 *  這裡不 import 是因為 content script 不支援 ES module。 */
function userIdFor(state, screenName) {
  if (!screenName) return null;
  const target = String(screenName).toLowerCase();
  const hit = Object.entries(state.screenNames || {})
    .find(([, name]) => (name || '').toLowerCase() === target);
  return hit ? hit[0] : null;
}

function renderPrimary() {
  const btn = $('primary');
  if (!pageScreenName) {
    btn.textContent = '送出並下載';
    btn.disabled = true;
    return;
  }
  // 帳號名一定要寫出來：這顆鈕只會送這一個帳號，講清楚才不會有混池的錯覺
  btn.textContent = pendingCount
    ? `送出並下載 ${pendingCount} 則（@${pageScreenName}）`
    : `@${pageScreenName}：滑動頁面即採集`;
  btn.disabled = pendingCount === 0;
}

/** 其他帳號還有待送的東西時說一聲，但**不混進上面的數字**。
 *  沒有這一行的話，切走的帳號等於人間蒸發（要靠 popup 才找得回來）。 */
function renderOthers(state) {
  const uid = targetUserId();
  const others = Object.entries(state.pending || {})
    .filter(([k, bucket]) => k !== uid && Object.keys(bucket).length)
    .map(([k, bucket]) => `@${state.screenNames?.[k] || k} ${Object.keys(bucket).length}`);
  $('others').textContent = others.length
    ? `其他帳號另有待送：${others.join('、')}（去該帳號頁面送出）` : '';
  $('others').classList.toggle('hidden', !others.length);
}

async function renderAccountArea(state) {
  if (state.online === false) {
    $('creator').disabled = true;
    $('role').disabled = true;
    return;
  }

  try {
    const [accounts, creators] = await Promise.all([
      api('/api/accounts?platform=x'),
      api('/api/creators'),
    ]);

    // 用 screen_name 比對，不用 userId —— 網址只給得起 screen_name，
    // 而且這樣在還沒攔到任何請求時就能找到帳號。
    const target = (pageScreenName || '').toLowerCase();
    account = target
      ? accounts.find((a) => (a.screen_name || '').toLowerCase() === target) || null
      : null;

    // options 只在真的變動時重建，否則每次輪詢都會打斷正在選的下拉
    const sig = creators.map((c) => `${c.id}:${c.display_name}`).join('|');
    if (sig !== creatorSig) {
      creatorSig = sig;
      const keep = $('creator').value;
      $('creator').innerHTML = '<option value="">creator（未歸屬）</option>'
        + creators.map((c) => `<option value="${c.id}">${c.display_name}</option>`).join('');
      $('creator').value = keep;
    }

    // creator 歸屬是帳號層級的屬性，不是這批的 —— 帳號還沒進 DB 就沒得掛。
    const hasAccount = !!account;
    $('creator').disabled = !hasAccount;
    $('role').disabled = !hasAccount;
    if (hasAccount) {
      setIfIdle('creator', account.creator_id ? String(account.creator_id) : '');
      setIfIdle('role', account.role);
    }

    await prefillTags();
  } catch (e) {
    $('creator').disabled = true;
    $('role').disabled = true;
    say(`讀取失敗：${e.message}`, 'err');
    tell('error', '讀取帳號資料失敗', String(e.message || e),
      { pageScreenName, path: location.pathname, expanded });
  }
}

/** 標籤預填順序：本機記的上次選擇 -> 帳號預設值 -> 空白。
 *  setIfIdle 保證不會蓋掉使用者正在改的下拉。 */
async function prefillTags() {
  const key = (pageScreenName || '').toLowerCase();
  if (!key) return;
  const r = await chrome.storage.local.get('lastTags');
  const remembered = (r.lastTags || {})[key];
  setIfIdle('rating', remembered?.rating ?? account?.default_rating ?? '');
  setIfIdle('content', remembered?.contentType ?? account?.default_content_type ?? '');
}

/** 該說的話。優先序：對不起來的帳號 > 丟棄 > 離線 > 提示。 */
function renderNotices(state) {
  if ($('msg').textContent && $('msg').className.includes('ok')) return;
  if (downloading) return;

  if (state.unresolved) {
    // 人在帳號頁面上、貼文也攔到了，卻一則都收不進來 = 對應建立失敗。
    // 這是唯一「不該發生」的丟棄，而症狀（數字一直 0）本身沒有任何線索，
    // 所以一定要講出來，而且排在最前面。
    say(`@${state.unresolved.screenName} 對不出 userId，`
      + `已略過 ${state.unresolved.count} 則 —— 重新整理頁面再試`, 'err');
  } else if (state.droppedOverflow) {
    // 先前這個只有 popup 看得到，等於靜默丟棄（沙盤 G）
    say(`暫存超過上限，已丟棄最舊的 ${state.droppedOverflow} 則`, 'err');
  } else if (state.online === false) {
    say('backend 離線 —— 採集照常，送出時才需要它', 'warn');
  } else if (pageScreenName && !capturable) {
    // ⚠️ **不可以只是數字停在 0。** 使用者以為在採集、滑了半天沒動靜，
    // 那與「壞掉」長得一模一樣。要講出為什麼，而且要給得出去處。
    sayLink(`這一頁不採集 —— 只有媒體分頁的東西確定是 @${pageScreenName} 自己的`,
      mediaUrl, '去他的相片頁');
  } else if (!pageScreenName) {
    say('開啟某個帳號的媒體分頁就會開始採集');
  } else {
    // ⚠️ **沒話要說的時候要把上一句清掉。**
    // 少了這一條，離開 /reposts 之後「這一頁不採集」會一直掛在那裡 ——
    // 使用者在媒體頁上正常採集，畫面卻告訴他不採集。
    say('');
  }
}

/** 帶一個連結的提示。純文字說「去媒體頁」沒有用 —— 使用者還要自己拼網址，
 *  而正確的網址是 `?filter=photo`（裸 /media 現在是影片分頁）。 */
function sayLink(text, href, linkText) {
  const el = $('msg');
  if (!el) return;
  el.className = 'msg warn';
  el.textContent = `${text} `;
  if (href) {
    const a = document.createElement('a');
    a.href = href;
    a.textContent = linkText;
    a.style.color = 'inherit';
    el.append(a);
  }
}

window.addEventListener('error', (e) => {
  tell('error', '面板未攔截的例外', String(e.message), {
    file: e.filename, line: e.lineno,
  });
});

window.__SNSMediaDLBar = { mount, setPageScreenName, onCaptured };
