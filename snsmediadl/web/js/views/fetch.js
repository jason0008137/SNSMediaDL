// 抓取：貼網址批次抓、一鍵更新、佇列與整批評價。
//
// 這是三個畫面裡**模型負載最高**的一頁 —— 唯一真正非同步的地方。
// 非同步流程一定要畫三格（wiki 的 UI_抓取控制 第六節）：
//   ① 送出當下：「已排入 N 個」，並立刻把視線帶到佇列區
//   ② 執行中：第 N/M、進度、目前在跑誰、能不能停
//   ③ 結果：**評價**，不只是計數
//
// ⚠️ ①與③的文案**不共用提示區塊**。「已排入」不等於「已抓到」，
// 而「已抓到」也不等於「檔案在磁碟上了」—— 抓取只產生 pending，
// 要①下載 worker 或明確觸發才會落地。這個專案有前科：`/api/queue/run`
// 這個端點存在的理由就是曾經「回報成功但什麼都沒下載」。

import { $, esc, hint } from '../dom.js';
import { fmt, t } from '../i18n.js';
import { api } from '../api.js';
import { state } from '../state.js';
import { refreshQueue } from '../queue.js';
// 失敗的列要能把使用者送到**根因所在的畫面**（缺憑證 → 設定頁、
// 找不到 → 帳號頁）。只說「缺憑證」而不給路，等於要他自己去找。
import { showView } from '../nav.js';
import { openSettings } from './settings.js';

// ── 貼網址批次抓 ───────────────────────────────────────

/** 解析結果只是預覽 —— 這一步**不會寫入任何東西**。
 *  理由跟刪除功能的預演一樣：打錯字不該直接變成一筆垃圾帳號記錄。 */
async function parseUrls() {
  const text = $('fetchUrls').value;
  $('submitUrls').disabled = true;
  if (!text.trim()) {
    $('parseResult').innerHTML = `<div class="muted">${esc(t('fetch.parse.empty'))}</div>`;
    return;
  }
  $('parseUrls').disabled = true;
  let body;
  try {
    body = await api('/api/fetch/parse', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ text }),
    });
  } catch (err) {
    $('parseResult').innerHTML = `<div class="bad">${
      esc(t('fetch.parse.failed', { msg: err.message }))}</div>`;
    return;
  } finally {
    $('parseUrls').disabled = false;
  }

  const rows = body.lines.map((ln) => {
    // ⚠️ 「貼對了但要換工具」與「打錯字」是**兩種結論**，不可混為一談 ——
    // 正式庫 90.5% 的帳號是 X。後端用 unsupported_platform 明確標出來，
    // 前端不去比對錯誤訊息裡有沒有「extension」那幾個字。
    if (ln.unsupported_platform) {
      return `<tr class="wrongtool"><td>${esc(ln.raw)}</td>
              <td>${esc(ln.unsupported_platform)}</td>
              <td>⚠ ${esc(ln.error)}</td></tr>`;
    }
    if (ln.error) {
      return `<tr class="bad"><td>${esc(ln.raw)}</td><td>—</td>
              <td>✗ ${esc(ln.error)}</td></tr>`;
    }
    if (ln.duplicate) {
      return `<tr class="muted"><td>${esc(ln.raw)}</td>
              <td>${esc(ln.target.label)}</td><td>${esc(t('fetch.parse.dup'))}</td></tr>`;
    }
    // 看得懂、也抓得動，但憑證沒設 —— 現在送出一定失敗。
    // 與「看不懂」分成兩種結論：這一行本身是對的。
    if (ln.needs_credential) {
      return `<tr class="wrongtool"><td>${esc(ln.raw)}</td>
              <td>${esc(ln.target.label)}</td>
              <td>${esc(t('fetch.parse.nocred',
                { platform: ln.needs_credential }))}</td></tr>`;
    }
    return `<tr><td>${esc(ln.raw)}</td><td>${esc(ln.target.label)}</td>
            <td>${esc(t(ln.in_db ? 'fetch.parse.indb' : 'fetch.parse.new'))}</td></tr>`;
  });

  const ok = body.lines.filter((l) => !l.error && !l.duplicate).length;
  const noCred = body.lines.filter((l) => l.needs_credential).length;
  const wrongTool = body.lines.filter((l) => l.unsupported_platform).length;
  const bad = body.lines.filter((l) => l.error && !l.unsupported_platform).length;

  // 結論先講，逐行在後面。
  const summary = [
    t('fetch.parse.ok.n', { n: fmt.num(ok) }),
    noCred ? t('fetch.parse.nocred.n', { n: fmt.num(noCred) }) : '',
    wrongTool ? t('fetch.parse.wrongtool.n', { n: fmt.num(wrongTool) }) : '',
    bad ? t('fetch.parse.bad.n', { n: fmt.num(bad) }) : '',
  ].filter(Boolean).join(' · ');

  $('parseResult').innerHTML = `<div class="${ok ? 'good' : 'bad'}">${esc(summary)}</div>`
    + `<table class="parse-table"><tbody>${rows.join('')}</tbody></table>`;
  // disabled 的**理由寫在按鈕上**，不進氣泡（PLAN 3-4(c)：不能做的原因要可見）
  $('submitUrls').disabled = ok === 0;
  $('submitUrls').textContent = ok
    ? t('fetch.submit.n', { n: fmt.num(ok) })
    : t('fetch.submit.none');
}

async function submitUrls() {
  $('submitUrls').disabled = true;
  try {
    const body = await api('/api/fetch/batch', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        text: $('fetchUrls').value,
        full: $('fetchFull').checked,
        download: $('fetchDownload').checked,
      }),
    });
    // ① 送出當下。文案一律用「已排入」，而且立刻把視線帶到佇列區 ——
    // 停在一句話上的話，使用者不知道東西在哪裡跑。
    $('parseResult').innerHTML =
      `<div class="good">${esc(t('fetch.queued.n', { n: fmt.num(body.queued) }))}</div>`
      + (body.rejected.length
        ? `<div class="bad">${esc(t('fetch.rejected.n',
          { n: fmt.num(body.rejected.length) }))}</div>` : '')
      + (body.already_queued.length
        ? `<div class="muted">${esc(t('fetch.alreadyqueued',
          { names: body.already_queued.join(t('common.listsep')) }))}</div>` : '');
    await refreshFetchQueue();
    $('fetchQueue').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } catch (err) {
    $('parseResult').innerHTML = `<div class="bad">${
      esc(t('fetch.submit.failed', { msg: err.message }))}</div>`;
    $('submitUrls').disabled = false;
  }
}

// ── 一鍵更新 ───────────────────────────────────────────

const SKIP_REASONS = {
  // ⚠️ 不可以寫死成 X。`cannot_fetch` 的意思是「backend 沒有這個平台的抓取
  // 實作」，X 只是其中最大的一群。實測有過一次：12 個 baraag 帳號因為平台名
  // 對不上註冊表而被歸到這裡，畫面卻告訴使用者「只能由 extension 採集（X）」
  // —— 那句話讓真正的原因完全查不到。
  cannot_fetch: ['skip.cannot_fetch.1', 'skip.cannot_fetch.2'],
  // ⚠️ **不可與 untracked 合併。** 這一個是使用者自己標的、他自己改得回來；
  // untracked 可能是系統連續找不到兩次自動退訂的，下一步是去查改名。
  // 合成一行「不可抓 N 個」使用者就分不出來了。
  ignored: ['skip.ignored.1', 'skip.ignored.2'],
  untracked: ['skip.untracked'],
  pixiv_excluded: ['skip.pixiv_excluded'],
  no_credentials: ['skip.no_credentials'],
  already_queued: ['skip.already_queued'],
};

/** 原因的每一句各佔一行。
 *
 *  ⚠️ 值是**句子陣列**不是字串，而且逐句 esc() 之後才用 <br> 接起來 ——
 *  把 <br> 寫進資料裡的話，那串就得整個跳過跳脫，等於為了換行開一個
 *  注入缺口。這幾句都是 A 桶（約束與可逆性宣告），一句都不能收進氣泡，
 *  但單句要進得了 24 全形字，所以只能拆行。 */
const skipReason = (k) => (SKIP_REASONS[k] || [k]).map((x) => esc(t(x))).join('<br>');

const mins = (sec) => (sec < 60
  ? t('common.seconds', { n: fmt.num(Math.round(sec)) })
  : t('common.minutes', { n: fmt.num(Math.round(sec / 60)) }));

/** 按下去**之前**就要看得見「可抓幾個、抓不動幾個」。
 *  正式庫 4,211 個帳號（90.5%）backend 抓不動 —— 那是多數情況，
 *  不是送出後才報「跳過」的邊緣狀況。 */
/** 沒設 PHPSESSID 就不讓勾「包含 pixiv」，**並且把原因寫在旁邊**。
 *
 *  只 disable 不說原因，使用者只會覺得那個勾選框壞了。
 *  ⚠️ 這裡讀的是 `state.settings`（由 queue.js 載入）—— 還沒載到時
 *  不動它，寧可讓它可勾，也不要用「我還不知道」去擋人。 */
function applyPixivCredentialGate() {
  const s = state.settings;
  if (!s?.credentials) return;
  const box = $('includePixiv');
  const has = s.credentials.pixiv;
  box.disabled = !has;
  const label = box.closest('label');
  if (!has) {
    box.checked = false;
    label.dataset.tip = t('fetch.pixiv.nocred.tip');
    if (!label.querySelector('.credwhy')) {
      label.insertAdjacentHTML('beforeend',
        `<span class="credwhy muted">${esc(t('fetch.pixiv.nocred.note'))}</span>`);
    }
  } else {
    label.querySelector('.credwhy')?.remove();
  }
}

export async function refreshScope() {
  const box = $('refreshScope');
  applyPixivCredentialGate();
  try {
    const s = await api(`/api/fetch/refresh-preview?include_pixiv=${$('includePixiv').checked}`);
    const by = Object.entries(s.by_platform)
      .map(([p, n]) => `${p} ${n}`).join(' / ') || '—';
    const skipped = Object.entries(s.skipped)
      .filter(([, n]) => n)
      .map(([k, n]) => `<div class="cant">${esc(t('fetch.scope.cannot'))} <span
        class="num">${fmt.num(n)}</span> —— ${skipReason(k)}</div>`).join('');
    box.innerHTML = `<div>${esc(t('fetch.scope.can'))} <span class="num">${
      fmt.num(s.fetchable)}</span>${esc(t('common.paren', { x: by }))}</div>`
      + skipped
      + (s.fetchable
        // 「至少」是真的下限（每個帳號至少一個請求、pixiv 每請求 1.8 秒），
        // 不是預估完成時間 —— 有新東西時每一頁／每一件都要再一個請求。
        ? `<div class="muted">${esc(t('fetch.scope.mintime',
          { time: mins(s.min_seconds) }))}</div>`
        : '');
    $('refreshAll').disabled = !s.fetchable;
    $('refreshAll').dataset.tip = s.fetchable ? '' : t('fetch.scope.nothing');
  } catch (e) {
    box.innerHTML = `<div class="bad">${
      esc(t('fetch.scope.failed', { msg: e.message }))}</div>`;
  }
}

async function refreshAll() {
  $('refreshAll').disabled = true;
  try {
    const body = await api('/api/fetch/refresh-all', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        include_pixiv: $('includePixiv').checked,
        download: $('fetchDownload').checked,
      }),
    });
    // ⚠️ 跳過的一定要列出來。只說「已排入 N 個」的話，
    // 使用者會以為 X 的帳號也更新過了。
    const skips = Object.entries(body.skipped || {}).map(([k, names]) =>
      // skipReason() 每一句自己 esc 過（它要保留 <br>），所以這裡不再包一層。
      `<div class="muted">${t('fetch.skipped.n',
        { n: fmt.num(names.length), why: skipReason(k) })}</div>`);
    $('refreshResult').innerHTML =
      `<div class="good">${esc(t('fetch.queued.n', { n: fmt.num(body.queued) }))}</div>`
      + skips.join('');
    await refreshFetchQueue();
    $('fetchQueue').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } catch (err) {
    $('refreshResult').innerHTML = `<div class="bad">${
      esc(t('common.failed.short', { msg: err.message }))}</div>`;
  } finally {
    $('refreshAll').disabled = false;
  }
}

// ── 佇列 ───────────────────────────────────────────────

const capped = (job) => job.resumable === true;

/** 失敗分類的顯示名。**一律讀 `fetch_status`，不解析 error 字串** ——
 *  比對錯誤訊息裡有沒有「429」是平台文案一改就失效的耦合。 */
// ⚠️ 值是 key 不是文字（模組載入時 i18n 還沒載完）。
const CATEGORY = {
  rate_limited: 'cat.rate_limited',
  not_found: 'cat.not_found',
  auth_required: 'cat.auth_required',
  failed: 'cat.failed',
  skipped: 'cat.skipped',
};
const catText = (k) => (CATEGORY[k] ? t(CATEGORY[k]) : k);

/** 這一類重試多半不會過，要先修原因。單筆仍可「仍要重試」（使用者覆寫）。 */
const FIX_FIRST = {
  auth_required: { key: 'fix.settings', view: 'settings' },
  not_found: { key: 'fix.accounts', view: 'accounts' },
};

/** 同一個帳號的多次嘗試**摺疊成一列**（使用者裁示 2026-08-19）。
 *
 *  ⚠️ 摺疊之後，逐 job 算的 `st.counts` 會大於畫面上的列數
 *  （重試 8 個修好 5 個 = 13 筆計數但只有 8 列）。所以摘要與評價一律用
 *  這個函式的結果重算，**不用 `st.counts`**（後端那個欄位 CLI 與測試在用，
 *  維持原樣）。 */
function foldJobs(st) {
  const all = [];
  if (st.running) all.push(st.running);
  all.push(...st.queued, ...st.recent);
  const byKey = new Map();
  for (const job of all) {
    const k = job.key || `${job.platform}|${job.host}|${job.acct.toLowerCase()}`;
    const prev = byKey.get(k);
    // 留 attempt 最大的那一筆；同 attempt 時留先看到的
    // （running / queued 排在 recent 之前，那正是「最新狀態」）。
    if (!prev || (job.attempt || 1) > (prev.job.attempt || 1)) {
      byKey.set(k, { job, history: [] });
    }
  }
  // 第二輪把同 key 的其餘嘗試收成歷史，讓徽章展開時有東西可看
  for (const job of all) {
    const k = job.key || `${job.platform}|${job.host}|${job.acct.toLowerCase()}`;
    const slot = byKey.get(k);
    if (slot && slot.job !== job) slot.history.push(job);
  }
  for (const slot of byKey.values()) {
    slot.history.sort((a, b) => (a.attempt || 1) - (b.attempt || 1));
  }
  return [...byKey.values()];
}

const expandedJobs = new Set();

function attemptBadge(row) {
  const n = row.job.attempt || 1;
  // 第 1 次不顯示 —— 「第 1 次」沒有任何資訊量
  if (n < 2) return '';
  return `<button type="button" class="attempt" data-expand="${row.job.id}"
    data-tip="${esc(t('job.attempt.tip'))}">${esc(t('job.attempt.n',
      { n: fmt.num(n) }))}</button>`;
}

function historyHtml(row) {
  if (!expandedJobs.has(row.job.id) || !row.history.length) return '';
  return `<div class="job-history">${row.history.map((h) =>
    `<div>${esc(t('job.attempt.n', { n: fmt.num(h.attempt || 1) }))} · ${
      esc(h.fetch_status ? catText(h.fetch_status) : '—')}
      · ${esc(h.error || h.reason || '')}</div>`).join('')}</div>`;
}

/** 一列一個容器（共域）—— 12 個 job × 3 欄位，靠間距會在滿載時串行。
 *  狀態用**符號**打頭（✓ ⟳ ⚠ ✗ ⊘），不只靠左邊那條顏色：灰階仍可辨識。 */
function jobHtml(row) {
  const job = row.job;
  const r = job.result || {};
  const badge = attemptBadge(row);
  const hist = historyHtml(row);

  if (job.state === 'done') {
    const hitCap = capped(job);
    // 撞上限**不是失敗**，所以給的是「繼續抓」不是「重試」，
    // 而且兩者不會同時出現在同一列。
    const btn = hitCap
      ? `<button type="button" class="rowbtn" data-resume="${job.id}"
           data-tip="${esc(t('job.resume.tip'))}">${esc(t('job.resume'))}</button>`
      : '';
    return `<div class="job done"><span class="sym">${hitCap ? '⚠' : '✓'}</span>
      <b>${esc(job.label)}</b>
      <span>${esc(t('job.added', { posts: fmt.num(r.posts_new ?? 0),
                                   media: fmt.num(r.media_new ?? 0) }))}</span>
      <span class="${hitCap ? 'capped' : 'muted'}">${esc(r.stopped_because || '')}</span>
      <span class="spacer"></span>${badge}${btn}${hist}</div>`;
  }
  if (job.state === 'running') {
    return `<div class="job running"><span class="sym">⟳</span><b>${esc(job.label)}</b>
      <span>${esc(t('job.running'))}${r.pages
        ? esc(t('job.running.page', { n: fmt.num(r.pages) })) : ''}</span>
      <span class="spacer"></span>${badge}</div>`;
  }
  if (job.state === 'queued') {
    return `<div class="job"><span class="sym">⋯</span><b>${esc(job.label)}</b>
      <span class="muted">${esc(t('job.queued'))}</span><span
        class="spacer"></span>${badge}</div>`;
  }

  // failed / skipped —— 這兩種才有重試
  const cat = CATEGORY[job.fetch_status] || '';
  const fix = FIX_FIRST[job.fetch_status];
  const msg = job.error || job.reason || '';
  const retryBtn = fix
    // 不可重試的類別**不做成 disabled** —— 使用者剛填完憑證、剛確認過改名時
    // 他知道的比系統多，disabled 會擋掉唯一合理的使用情境。文案不同就好。
    ? `<button type="button" class="rowbtn override" data-retry="${job.id}"
         data-tip="${esc(t('job.retry.anyway.tip'))}">${
             esc(t('job.retry.anyway'))}</button>`
    : `<button type="button" class="rowbtn" data-retry="${job.id}"
         data-tip="${esc(t('job.retry.tip'))}">${esc(t('job.retry'))}</button>`;
  const fixBtn = fix
    ? `<button type="button" class="rowbtn" data-goto="${fix.view}">${fix.text}</button>`
    : '';
  const cls = job.state === 'skipped' ? 'skipped' : 'failed';
  const sym = job.state === 'skipped' ? '⊘' : '✗';
  return `<div class="job ${cls}"><span class="sym">${sym}</span>
    <b>${esc(job.label)}</b>
    ${cat ? `<span class="jobcat">${esc(cat)}</span>` : ''}
    <span class="${cls === 'failed' ? 'bad' : 'muted'}">${esc(msg)}</span>
    <span class="spacer"></span>${badge}${retryBtn}${fixBtn}${hist}</div>`;
}

/** ③ 整批評價。**這是本頁最重要的設計。**
 *
 *  現況只說「已排入 12 個帳號」—— 那回答的是第 5 題（系統狀態），
 *  完全沒回答第 6 題（好還是壞、要不要再做什麼）。
 *  三件事一定要講：撞到頁數上限＝沒抓完、失敗的原因、**抓到的還沒下載**。 */
/** 失敗分類明細 + 兩顆行動按鈕。**這一段回答的是「我下一步該做什麼」。**
 *
 *  只列筆數（「8 個失敗」）回答的是第 5 題（系統狀態）。使用者要的是
 *  「哪些再試一次會過、哪些得先去修原因」—— 那不該由他自己讀 8 行錯誤訊息
 *  歸納出來。 */
function breakdownHtml(rows, st) {
  const bad = rows.filter((r) => r.job.state === 'failed' || r.job.state === 'skipped');
  if (!bad.length) return '';

  const byCat = new Map();
  for (const r of bad) {
    const k = r.job.fetch_status || 'failed';
    byCat.set(k, (byCat.get(k) || 0) + 1);
  }
  const ACT = {
    skipped: { can: true, key: 'act.skipped' },
    rate_limited: { can: true, key: 'act.rate_limited' },
    failed: { can: true, key: 'act.failed' },
    auth_required: { can: false, key: 'act.auth_required' },
    not_found: { can: false, key: 'act.not_found' },
  };
  const detail = [...byCat.entries()].map(([k, n]) => {
    const a = ACT[k] || { can: true, key: 'act.failed' };
    return `<div class="bdrow"><span class="bdn">${
      esc(t('breakdown.n', { n: fmt.num(n) }))}</span>
      <span class="bdcat">${esc(catText(k))}</span>
      <span class="${a.can ? 'good' : 'warn'}">${esc(t(a.key))}</span></div>`;
  }).join('');

  const retryN = bad.filter((r) => r.job.retryable).length;
  const capN = rows.filter((r) => capped(r.job)).length;
  const limited = Object.keys(st.rate_limited || {});

  const actions = [];
  if (retryN) {
    actions.push(`<button type="button" id="retryFailed">${
      esc(t('fetch.retry.n', { n: fmt.num(retryN) }))}</button>`);
    if (limited.length) {
      // **預設不勾。** 自動解除等於用 fallback 掩蓋「對方在擋我們」，
      // 而且解除窗口我們不知道，猜錯就是再撞一次。
      actions.push(`<label class="chk" data-tip="${esc(t('fetch.clearrate.tip'))}">
        <input type="checkbox" id="retryClearRate"> ${
          esc(t('fetch.clearrate', { sites: limited.join(t('common.listsep')) }))}</label>`);
    }
  }
  if (capN) {
    actions.push(`<button type="button" id="resumeCapped" class="ghost">${
      esc(t('fetch.resume.n', { n: fmt.num(capN) }))}</button>`);
  }

  return `<div class="breakdown">${detail}</div>`
    + (actions.length ? `<div class="row retry-row">${actions.join('')}</div>` : '')
    + (retryN && capN
      ? `<div class="muted">${esc(t('fetch.retryvsresume.1'))}<br>${
        esc(t('fetch.retryvsresume.2'))}</div>`
      : '');
}

function renderVerdict(st, rows) {
  const box = $('batchVerdict');
  const c = st.counts;
  const active = c.queued + c.running;
  // ⚠️ 依**帳號的最終狀態**算，不是逐 job 算。摺疊之後兩者不一樣，
  // 而數字與畫面上的列數對不上會讓人以為畫面漏顯示了。
  const finished = rows.filter(
    (r) => r.job.state !== 'queued' && r.job.state !== 'running',
  );
  if (active > 0 || !finished.length) {
    box.classList.add('hidden');
    return;
  }

  const done = finished.filter((r) => r.job.state === 'done');
  const cappedJobs = done.filter((r) => capped(r.job));
  const failed = finished.filter((r) => r.job.state === 'failed');
  const skipped = finished.filter((r) => r.job.state === 'skipped');
  const posts = done.reduce((n, r) => n + (r.job.result?.posts_new || 0), 0);
  const media = done.reduce((n, r) => n + (r.job.result?.media_new || 0), 0);
  // 這一輪有重試過的帳號（attempt ≥ 2）。有的話，評價要能跟上一輪比。
  const retried = finished.filter((r) => (r.job.attempt || 1) >= 2);

  const lines = [];
  if (retried.length) {
    const fixed = retried.filter((r) => r.job.state === 'done').length;
    const still = retried.length - fixed;
    lines.push(`<div class="${still ? 'warn' : 'good'}">${
      esc(t('verdict.retried', { n: fmt.num(retried.length), fixed: fmt.num(fixed) }))}${
      still ? esc(t('verdict.retried.still', { n: fmt.num(still) })) : ''}</div>`);
    if (still) {
      // 沒有這句，重試按鈕會變成使用者的無限迴圈 —— 而每一圈都是真的
      // 打到平台的請求。
      lines.push(`<div class="warn">${esc(t('verdict.samecause'))}<br>${
        esc(t('verdict.samecause.2'))}</div>`);
    }
  }
  if (st.history_full) {
    lines.push(`<div class="warn">${
      esc(t('verdict.historyfull', { n: fmt.num(st.history_limit) }))}<br>${
      esc(t('verdict.historyfull.2'))}</div>`);
  }
  if (cappedJobs.length) {
    lines.push(`<div class="warn">${
      esc(t('verdict.capped', { n: fmt.num(cappedJobs.length) }))}<br>${
      esc(t('verdict.capped.2'))}${hint(t('verdict.capped.tip'))}</div>`);
  }
  if (failed.length) {
    lines.push(`<div class="warn">${
      esc(t('verdict.failed.n', { n: fmt.num(failed.length) }))}</div>`);
  }
  if (skipped.length) {
    // 逐項的**真**原因就在下面的分類列裡；這裡那句括號只是推測，進氣泡。
    lines.push(`<div class="muted">${
      esc(t('verdict.skipped.n', { n: fmt.num(skipped.length) }))}${
      hint(t('verdict.skipped.tip'))}</div>`);
  }
  lines.push(breakdownHtml(rows, st));
  // 自動退訂一定要講。靜默退訂就算技術上正確，使用者也只會覺得
  // 帳號自己不見了 —— 然後在帳號頁上找半天。
  const untracked = st.auto_untracked || [];
  if (untracked.length) {
    lines.push(`<div class="warn">${esc(t('verdict.untracked', {
      n: fmt.num(untracked.length),
      names: untracked.join(t('common.listsep')) }))}<br>${
      esc(t('verdict.untracked.2'))}</div>`);
  }
  if (!cappedJobs.length && !failed.length && done.length) {
    // **沒有壞消息本身就是一則消息** —— 不說的話使用者得自己逐行檢查。
    lines.push(`<div class="good">${
      esc(t('verdict.allclear', { n: fmt.num(done.length) }))}</div>`);
  }

  // 「新增 N 個媒體」一定要接「還沒下載」，否則使用者會以為檔案已經在磁碟上。
  const pending = state.queue?.pending || 0;
  if (media) {
    const auto = state.settings?.auto_download;
    lines.push(`<div class="${pending ? 'warn' : 'muted'}">
      ${esc(pending
        ? t('verdict.pending', { n: fmt.num(pending),
            state: t(auto ? 'verdict.pending.auto.on' : 'verdict.pending.auto.off') })
        : t('verdict.pending.none'))}</div>`
      + (pending && !auto
        ? `<div class="row"><button type="button" id="runQueueNow">${
            esc(t('verdict.downloadnow'))}</button></div>`
        : ''));
  }

  box.classList.remove('hidden');
  box.innerHTML = `<h3>${esc(t('verdict.head', { n: fmt.num(finished.length),
    posts: fmt.num(posts), media: fmt.num(media) }))}</h3>`
    + lines.join('');
}

$('batchVerdict').addEventListener('click', async (ev) => {
  if (!ev.target.closest('#runQueueNow')) return;
  ev.target.disabled = true;
  ev.target.textContent = t('verdict.downloading');
  try {
    await api('/api/queue/run', { method: 'POST' });
    // ①「已開始」與③「下載完了」不共用提示：進度在 header 的背景活動區
    refreshQueue();
  } catch (e) {
    ev.target.textContent = t('common.failed.short', { msg: e.message });
    ev.target.disabled = false;
  }
});

export async function refreshFetchQueue() {
  let st;
  try {
    st = await api('/api/fetch/queue');
  } catch {
    return;   // header 的背景活動區已經會顯示 backend 無回應，不重複報錯
  }
  const c = st.counts;
  const active = c.queued + c.running;
  // 輪詢節奏要看這個：抓取佇列有東西時，即使下載佇列是空的也得繼續盯
  state.fetchActive = active > 0;
  $('fetchBadge').textContent = active;
  $('fetchBadge').classList.toggle('hidden', !active);

  // 摺疊：同一個帳號的多次嘗試合成一列（使用者裁示）。
  // ⚠️ 摘要的計數也要跟著改成**依帳號最終狀態**，不能用逐 job 的 st.counts ——
  // 否則「完成 5 失敗 8」加起來會大於畫面上的 8 列，看起來像漏顯示。
  const rows = foldJobs(st);
  const tally = { done: 0, failed: 0, skipped: 0 };
  for (const r of rows) {
    if (r.job.state in tally) tally[r.job.state] += 1;
  }
  const total = active + tally.done + tally.failed + tally.skipped;
  const finishedN = tally.done + tally.failed + tally.skipped;
  $('fetchQueueSummary').textContent = active
    ? t('queue.progress', { i: fmt.num(finishedN + 1), total: fmt.num(total),
        done: fmt.num(tally.done), failed: fmt.num(tally.failed),
        skipped: fmt.num(tally.skipped) })
    : (total
      ? t('queue.tally', { done: fmt.num(tally.done), failed: fmt.num(tally.failed),
          skipped: fmt.num(tally.skipped) })
      : t('queue.empty.short'));
  // 佇列非空時才提醒它不持久 —— 空的時候講這件事沒有意義。
  $('fetchVolatile').classList.toggle('hidden', !active);

  const bar = $('fetchProgress');
  bar.classList.toggle('hidden', !total || !active);
  if (total) bar.firstElementChild.style.width = `${(finishedN / total) * 100}%`;

  $('clearFetchQueue').disabled = !c.queued;
  $('clearFetchQueue').dataset.tip = c.queued ? '' : t('queue.clear.none');

  const limited = Object.entries(st.rate_limited || {});
  $('clearRateLimit').classList.toggle('hidden', limited.length === 0);

  const parts = [];
  if (limited.length) {
    parts.push(`<div class="bad">${esc(t('queue.ratelimited', {
      sites: limited.map(([k, why]) => k + t('common.paren', { x: why }))
        .join(t('common.listsep')) }))}
      ${esc(t('queue.ratelimited.2'))}</div>`);
  }
  parts.push(...rows.map(jobHtml));
  $('fetchQueue').innerHTML = parts.join('')
    || `<div class="empty">${esc(t('queue.empty.1'))}<br>
        ${esc(t('queue.empty.2'))}</div>`;

  renderVerdict(st, rows);
}

// ── 重試與續抓 ─────────────────────────────────────────
//
// ⚠️ ①「已重新排入」與 ③「修好了 N 個」不共用提示區塊。而且這裡多一層：
// **「已排入」也不等於「它真的跑了」** —— 站台旗標還掛著時，重排的 job
// 會在後端 `_process()` 開頭就被標成 skipped。所以 ① 一定要帶
// `will_be_skipped` 的數字，否則使用者會看到「已排入 201 個」然後幾秒內
// 全部變 ⊘，而完全不知道發生了什麼。

function submitNote(html) {
  const box = $('retryResult');
  box.classList.remove('hidden');
  box.innerHTML = html;
}

async function postRetry(url, body) {
  return api(url, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
}

$('batchVerdict').addEventListener('click', async (ev) => {
  const retryBtn = ev.target.closest('#retryFailed');
  const resumeBtn = ev.target.closest('#resumeCapped');
  if (!retryBtn && !resumeBtn) return;
  const btn = retryBtn || resumeBtn;
  btn.disabled = true;

  try {
    if (retryBtn) {
      const clear = $('retryClearRate')?.checked || false;
      const r = await postRetry('/api/fetch/queue/retry-failed', {
        clear_rate_limit: clear,
      });
      const bits = [`<div class="good">${
        esc(t('retry.requeued', { n: fmt.num(r.requeued) }))} ——
        ${esc(t('retry.requeued.2'))}</div>`];
      if (r.will_be_skipped) {
        bits.push(`<div class="warn">${
          esc(t('retry.willskip', { n: fmt.num(r.will_be_skipped) }))}<br>${
          esc(t('retry.willskip.2'))}</div>`);
      }
      if (r.refused?.length) {
        // 靜默少排幾個就是這個專案禁止的靜默漏抓 —— 逐筆講。
        bits.push(`<div class="muted">${esc(t('retry.refused.n', {
          n: fmt.num(r.refused.length),
          why: r.refused.map((x) => x.label + t('common.paren', { x: x.reason }))
            .join(t('common.listsep')) }))}</div>`);
      }
      submitNote(bits.join(''));
    } else {
      const r = await postRetry('/api/fetch/queue/resume-capped');
      submitNote(`<div class="good">${
        esc(t('resume.queued', { n: fmt.num(r.requeued) }))} ——
        ${esc(t('resume.queued.2'))}</div>`
        + (r.refused?.length
          ? `<div class="muted">${esc(t('retry.refused.queued',
            { n: fmt.num(r.refused.length) }))}</div>` : ''));
    }
    await refreshFetchQueue();
  } catch (e) {
    submitNote(`<div class="bad">${
      esc(t('fetch.submit.failed', { msg: e.message }))}</div>`);
    btn.disabled = false;
  }
});

$('fetchQueue').addEventListener('click', async (ev) => {
  const expand = ev.target.closest('[data-expand]');
  if (expand) {
    const id = Number(expand.dataset.expand);
    if (expandedJobs.has(id)) expandedJobs.delete(id); else expandedJobs.add(id);
    refreshFetchQueue();
    return;
  }
  const goto = ev.target.closest('[data-goto]');
  if (goto) {
    // 指向**根因所在的畫面**。只說「缺憑證」而不給路，等於要使用者自己找。
    if (goto.dataset.goto === 'settings') openSettings();
    else showView('accounts');
    return;
  }
  const btn = ev.target.closest('[data-retry], [data-resume]');
  if (!btn) return;
  const isResume = btn.dataset.resume != null;
  const id = Number(btn.dataset.resume ?? btn.dataset.retry);
  btn.disabled = true;
  try {
    const r = await postRetry(
      `/api/fetch/queue/${id}/${isResume ? 'resume' : 'retry'}`,
    );
    if (!r.requeued) {
      // 拒絕的理由要寫出來。按了沒反應是最糟的失敗。
      btn.textContent = r.refused_reason || t('retry.refused.btn');
      return;
    }
    btn.textContent = t(isResume ? 'retry.done.resume' : 'retry.done.retry');
    await refreshFetchQueue();
  } catch (e) {
    btn.textContent = t('common.failed.short', { msg: e.message });
    btn.disabled = false;
  }
});

/** 進入抓取頁時呼叫（nav 的 registry）。 */
export function loadFetchView() {
  refreshScope();
  return refreshFetchQueue();
}

$('parseUrls').addEventListener('click', parseUrls);
$('submitUrls').addEventListener('click', submitUrls);
$('refreshAll').addEventListener('click', refreshAll);
// 勾了 pixiv，規模與預估時間會差很多 —— 要**即時**反映在按鈕旁，
// 不是按下去才發現要跑好幾小時。
$('includePixiv').addEventListener('change', refreshScope);
// 改了輸入就讓上一份預覽失效 —— 送出走的是伺服器端重新解析，
// 但畫面上留著舊結果會讓人以為送的是那份
$('fetchUrls').addEventListener('input', () => {
  $('submitUrls').disabled = true;
  $('submitUrls').textContent = t('fetch.submit.needparse');
});

$('clearFetchQueue').addEventListener('click', async () => {
  const r = await api('/api/fetch/queue', { method: 'DELETE' });
  $('fetchQueueSummary').textContent = t('queue.cleared', { n: fmt.num(r.cleared) });
  refreshFetchQueue();
});

$('clearRateLimit').addEventListener('click', async () => {
  await api('/api/fetch/rate-limit', { method: 'DELETE' });
  refreshFetchQueue();
});
