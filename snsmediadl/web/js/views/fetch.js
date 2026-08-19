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

import { $, esc } from '../dom.js';
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
    $('parseResult').innerHTML = '<div class="muted">先貼一些網址</div>';
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
    $('parseResult').innerHTML = `<div class="bad">解析失敗：${esc(err.message)}</div>`;
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
              <td>${esc(ln.target.label)}</td><td>這批裡重複</td></tr>`;
    }
    // 看得懂、也抓得動，但憑證沒設 —— 現在送出一定失敗。
    // 與「看不懂」分成兩種結論：這一行本身是對的。
    if (ln.needs_credential) {
      return `<tr class="wrongtool"><td>${esc(ln.raw)}</td>
              <td>${esc(ln.target.label)}</td>
              <td>⚠ 會失敗 —— 尚未設定 ${esc(ln.needs_credential)} 憑證
                  （設定頁有填法）</td></tr>`;
    }
    return `<tr><td>${esc(ln.raw)}</td><td>${esc(ln.target.label)}</td>
            <td>${ln.in_db ? '已在資料庫（會做增量）' : '新帳號'}</td></tr>`;
  });

  const ok = body.lines.filter((l) => !l.error && !l.duplicate).length;
  const noCred = body.lines.filter((l) => l.needs_credential).length;
  const wrongTool = body.lines.filter((l) => l.unsupported_platform).length;
  const bad = body.lines.filter((l) => l.error && !l.unsupported_platform).length;

  // 結論先講，逐行在後面。
  const summary = [
    `可抓 ${ok} 個`,
    noCred ? `${noCred} 行缺憑證會失敗` : '',
    wrongTool ? `${wrongTool} 行只能用 extension` : '',
    bad ? `${bad} 行看不懂` : '',
  ].filter(Boolean).join(' · ');

  $('parseResult').innerHTML = `<div class="${ok ? 'good' : 'bad'}">${esc(summary)}</div>`
    + `<table class="parse-table"><tbody>${rows.join('')}</tbody></table>`;
  // disabled 的**理由寫在按鈕上**，不進氣泡（PLAN 3-4(c)：不能做的原因要可見）
  $('submitUrls').disabled = ok === 0;
  $('submitUrls').textContent = ok ? `送出 ${ok} 個` : '送出（這批沒有可抓的）';
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
      `<div class="good">已排入 ${body.queued} 個帳號 —— 結果會出現在下面的佇列</div>`
      + (body.rejected.length
        ? `<div class="bad">${body.rejected.length} 行沒有排入</div>` : '')
      + (body.already_queued.length
        ? `<div class="muted">${esc(body.already_queued.join('、'))} 已經在佇列裡</div>` : '');
    await refreshFetchQueue();
    $('fetchQueue').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } catch (err) {
    $('parseResult').innerHTML = `<div class="bad">送出失敗：${esc(err.message)}</div>`;
    $('submitUrls').disabled = false;
  }
}

// ── 一鍵更新 ───────────────────────────────────────────

const SKIP_REASONS = {
  // ⚠️ 不可以寫死成 X。`cannot_fetch` 的意思是「backend 沒有這個平台的抓取
  // 實作」，X 只是其中最大的一群。實測有過一次：12 個 baraag 帳號因為平台名
  // 對不上註冊表而被歸到這裡，畫面卻告訴使用者「只能由 extension 採集（X）」
  // —— 那句話讓真正的原因完全查不到。
  cannot_fetch: 'backend 沒有這個平台的抓取實作（X 只能用 extension 採集）',
  // ⚠️ **不可與 untracked 合併。** 這一個是使用者自己標的、他自己改得回來；
  // untracked 可能是系統連續找不到兩次自動退訂的，下一步是去查改名。
  // 合成一行「不可抓 N 個」使用者就分不出來了。
  ignored: '你標記為忽略 —— 帳號頁可以取消（只影響一鍵更新，資料沒動）',
  untracked: '已取消追蹤',
  pixiv_excluded: '這次沒有包含 pixiv',
  no_credentials: '缺憑證（config.toml 的 platform_credentials）',
  already_queued: '已經在佇列裡',
};

const mins = (sec) => (sec < 60 ? `${Math.round(sec)} 秒` : `${Math.round(sec / 60)} 分鐘`);

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
    label.dataset.tip = '尚未設定 pixiv 憑證（PHPSESSID），抓了一定會失敗';
    if (!label.querySelector('.credwhy')) {
      label.insertAdjacentHTML('beforeend',
        '<span class="credwhy muted"> —— 未設定憑證，見設定頁</span>');
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
      .map(([k, n]) => `<div class="cant">不可抓 <span class="num">${n}</span> 個 —— ${
        esc(SKIP_REASONS[k] || k)}</div>`).join('');
    box.innerHTML = `<div>可抓 <span class="num">${s.fetchable}</span> 個（${esc(by)}）</div>`
      + skipped
      + (s.fetchable
        // 「至少」是真的下限（每個帳號至少一個請求、pixiv 每請求 1.8 秒），
        // 不是預估完成時間 —— 有新東西時每一頁／每一件都要再一個請求。
        ? `<div class="muted">至少要跑 ${mins(s.min_seconds)}；實際有新東西時會更久</div>`
        : '');
    $('refreshAll').disabled = !s.fetchable;
    $('refreshAll').dataset.tip = s.fetchable ? '' : '目前沒有 backend 抓得動的帳號';
  } catch (e) {
    box.innerHTML = `<div class="bad">問不到可抓的帳號：${esc(e.message)}</div>`;
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
      `<div class="muted">跳過 ${names.length} 個 —— ${esc(SKIP_REASONS[k] || k)}</div>`);
    $('refreshResult').innerHTML =
      `<div class="good">已排入 ${body.queued} 個帳號 —— 結果會出現在下面的佇列</div>`
      + skips.join('');
    await refreshFetchQueue();
    $('fetchQueue').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } catch (err) {
    $('refreshResult').innerHTML = `<div class="bad">失敗：${esc(err.message)}</div>`;
  } finally {
    $('refreshAll').disabled = false;
  }
}

// ── 佇列 ───────────────────────────────────────────────

const capped = (job) => job.resumable === true;

/** 失敗分類的顯示名。**一律讀 `fetch_status`，不解析 error 字串** ——
 *  比對錯誤訊息裡有沒有「429」是平台文案一改就失效的耦合。 */
const CATEGORY = {
  rate_limited: '限速 429',
  not_found: '找不到',
  auth_required: '缺憑證',
  failed: '其他錯誤',
  skipped: '限速跳過',
};

/** 這一類重試多半不會過，要先修原因。單筆仍可「仍要重試」（使用者覆寫）。 */
const FIX_FIRST = {
  auth_required: { text: '去設定頁', view: 'settings' },
  not_found: { text: '帳號頁', view: 'accounts' },
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
    data-tip="展開歷次的失敗原因">第 ${n} 次</button>`;
}

function historyHtml(row) {
  if (!expandedJobs.has(row.job.id) || !row.history.length) return '';
  return `<div class="job-history">${row.history.map((h) =>
    `<div>第 ${h.attempt || 1} 次 · ${esc(CATEGORY[h.fetch_status] || h.fetch_status || '—')}
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
           data-tip="從上次停下來的游標接下去，不是從第 1 頁重來">繼續抓</button>`
      : '';
    return `<div class="job done"><span class="sym">${hitCap ? '⚠' : '✓'}</span>
      <b>${esc(job.label)}</b>
      <span>新增 ${r.posts_new ?? 0} 則 / ${r.media_new ?? 0} 個媒體</span>
      <span class="${hitCap ? 'capped' : 'muted'}">${esc(r.stopped_because || '')}</span>
      <span class="spacer"></span>${badge}${btn}${hist}</div>`;
  }
  if (job.state === 'running') {
    return `<div class="job running"><span class="sym">⟳</span><b>${esc(job.label)}</b>
      <span>抓取中…${r.pages ? `第 ${r.pages} 頁` : ''}</span>
      <span class="spacer"></span>${badge}</div>`;
  }
  if (job.state === 'queued') {
    return `<div class="job"><span class="sym">⋯</span><b>${esc(job.label)}</b>
      <span class="muted">排隊中</span><span class="spacer"></span>${badge}</div>`;
  }

  // failed / skipped —— 這兩種才有重試
  const cat = CATEGORY[job.fetch_status] || '';
  const fix = FIX_FIRST[job.fetch_status];
  const msg = job.error || job.reason || '';
  const retryBtn = fix
    // 不可重試的類別**不做成 disabled** —— 使用者剛填完憑證、剛確認過改名時
    // 他知道的比系統多，disabled 會擋掉唯一合理的使用情境。文案不同就好。
    ? `<button type="button" class="rowbtn override" data-retry="${job.id}"
         data-tip="這個原因重試多半不會過；修好原因再按">仍要重試</button>`
    : `<button type="button" class="rowbtn" data-retry="${job.id}"
         data-tip="重新排進佇列尾端，不會插隊">重試</button>`;
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
    skipped: { can: true, text: '可重試 —— 它們根本沒跑過' },
    rate_limited: { can: true, text: '可重試（建議先等一陣子）' },
    failed: { can: true, text: '可重試' },
    auth_required: { can: false, text: '先去設定頁填憑證' },
    not_found: { can: false, text: '帳號頁確認後再單筆重試' },
  };
  const detail = [...byCat.entries()].map(([k, n]) => {
    const a = ACT[k] || { can: true, text: '可重試' };
    return `<div class="bdrow"><span class="bdn">${n} 個</span>
      <span class="bdcat">${esc(CATEGORY[k] || k)}</span>
      <span class="${a.can ? 'good' : 'warn'}">${esc(a.text)}</span></div>`;
  }).join('');

  const retryN = bad.filter((r) => r.job.retryable).length;
  const capN = rows.filter((r) => capped(r.job)).length;
  const limited = Object.keys(st.rate_limited || {});

  const actions = [];
  if (retryN) {
    actions.push(`<button type="button" id="retryFailed">重試可重試的 ${retryN} 個</button>`);
    if (limited.length) {
      // **預設不勾。** 自動解除等於用 fallback 掩蓋「對方在擋我們」，
      // 而且解除窗口我們不知道，猜錯就是再撞一次。
      actions.push(`<label class="chk" data-tip="解除後這些會真的跑，但可能再次被限速">
        <input type="checkbox" id="retryClearRate"> 一併解除限速標記（${
          esc(limited.join('、'))}）</label>`);
    }
  }
  if (capN) {
    actions.push(`<button type="button" id="resumeCapped" class="ghost">繼續抓 ${capN} 個沒抓完的</button>`);
  }

  return `<div class="breakdown">${detail}</div>`
    + (actions.length ? `<div class="row retry-row">${actions.join('')}</div>` : '')
    + (retryN && capN
      ? '<div class="muted">「重試」與「繼續抓」是兩件事：撞頁數上限不是失敗。</div>'
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
    lines.push(`<div class="${still ? 'warn' : 'good'}">重試的 ${retried.length} 個裡，
      <b>修好了 ${fixed} 個</b>${still ? `，還有 ${still} 個仍然失敗` : ''}。</div>`);
    if (still) {
      // 沒有這句，重試按鈕會變成使用者的無限迴圈 —— 而每一圈都是真的
      // 打到平台的請求。
      lines.push(`<div class="warn">同一個原因連續兩次失敗，多半不是暫時性的：
        <b>再按重試不會變好</b>，先去排除原因。</div>`);
    }
  }
  if (st.history_full) {
    lines.push(`<div class="warn">佇列只留得到最後 ${st.history_limit} 筆 ——
      更早的結果已經沒了，「全部重試」涵蓋不到它們。</div>`);
  }
  if (cappedJobs.length) {
    lines.push(`<div class="warn">有 ${cappedJobs.length} 個帳號<b>沒有抓完</b> ——
      撞到頁數上限。用下面的「繼續抓」從上次停下來的地方接下去
      （<b>再跑一次是沒用的</b>：增量會在第 1 頁碰到已知貼文就停）。</div>`);
  }
  if (failed.length) {
    lines.push(`<div class="warn">${failed.length} 個失敗 —— 分類與處置見下。</div>`);
  }
  if (skipped.length) {
    lines.push(`<div class="muted">⊘ ${skipped.length} 個跳過（多半是只能用 extension 採集的 X 帳號，或站台被限速）。</div>`);
  }
  lines.push(breakdownHtml(rows, st));
  // 自動退訂一定要講。靜默退訂就算技術上正確，使用者也只會覺得
  // 帳號自己不見了 —— 然後在帳號頁上找半天。
  const untracked = st.auto_untracked || [];
  if (untracked.length) {
    lines.push(`<div class="warn">本輪自動退訂 ${untracked.length} 個帳號
      （連續找不到）：${esc(untracked.join('、'))}。<br>
      資料一筆都沒動，只是不再自動抓；帳號頁上可以「恢復追蹤」。</div>`);
  }
  if (!cappedJobs.length && !failed.length && done.length) {
    // **沒有壞消息本身就是一則消息** —— 不說的話使用者得自己逐行檢查。
    lines.push(`<div class="good">${done.length} 個全部抓完，沒有撞上限。</div>`);
  }

  // 「新增 N 個媒體」一定要接「還沒下載」，否則使用者會以為檔案已經在磁碟上。
  const pending = state.queue?.pending || 0;
  if (media) {
    const auto = state.settings?.auto_download;
    lines.push(`<div class="${pending ? 'warn' : 'muted'}">
      ${pending
        ? `⚠ 還有 ${pending} 個媒體<b>還沒下載</b>。背景下載目前是${auto ? '開的，會自己撿' : '關的'}。`
        : '抓到的媒體都已經下載完了。'}</div>`
      + (pending && !auto
        ? '<div class="row"><button type="button" id="runQueueNow">立即下載</button></div>'
        : ''));
  }

  box.classList.remove('hidden');
  box.innerHTML = `<h3>跑完 ${finished.length} 個帳號 · 新增 ${posts} 則貼文 / ${media} 個媒體</h3>`
    + lines.join('');
}

$('batchVerdict').addEventListener('click', async (ev) => {
  if (!ev.target.closest('#runQueueNow')) return;
  ev.target.disabled = true;
  ev.target.textContent = '已開始下載…';
  try {
    await api('/api/queue/run', { method: 'POST' });
    // ①「已開始」與③「下載完了」不共用提示：進度在 header 的背景活動區
    refreshQueue();
  } catch (e) {
    ev.target.textContent = `失敗：${e.message}`;
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
    ? `第 ${finishedN + 1} / ${total}　完成 ${tally.done}　失敗 ${tally.failed}　跳過 ${tally.skipped}`
    : (total ? `完成 ${tally.done}　失敗 ${tally.failed}　跳過 ${tally.skipped}` : '佇列是空的');
  // 佇列非空時才提醒它不持久 —— 空的時候講這件事沒有意義。
  $('fetchVolatile').classList.toggle('hidden', !active);

  const bar = $('fetchProgress');
  bar.classList.toggle('hidden', !total || !active);
  if (total) bar.firstElementChild.style.width = `${(finishedN / total) * 100}%`;

  $('clearFetchQueue').disabled = !c.queued;
  $('clearFetchQueue').dataset.tip = c.queued ? '' : '沒有還沒開始的工作';

  const limited = Object.entries(st.rate_limited || {});
  $('clearRateLimit').classList.toggle('hidden', limited.length === 0);

  const parts = [];
  if (limited.length) {
    parts.push(`<div class="bad">⚠ 被限速：${limited
      .map(([k, why]) => `${esc(k)}（${esc(why)}）`).join('、')}
      —— 解除後會再試，可能再次被限速。</div>`);
  }
  parts.push(...rows.map(jobHtml));
  $('fetchQueue').innerHTML = parts.join('')
    || `<div class="empty">佇列是空的 —— 還沒抓過任何東西。<br>
        貼幾個網址，或按「開始更新」讓追蹤中的帳號各跑一次增量。</div>`;

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
      const bits = [`<div class="good">已重新排入 ${r.requeued} 個 ——
        <b>排在佇列尾端</b>，會照序列慢慢跑。</div>`];
      if (r.will_be_skipped) {
        bits.push(`<div class="warn">⚠ 其中 ${r.will_be_skipped} 個屬於<b>仍被限速</b>的站台，
          會直接被跳過。要它們真的跑，先勾「一併解除限速標記」再重試一次。</div>`);
      }
      if (r.refused?.length) {
        // 靜默少排幾個就是這個專案禁止的靜默漏抓 —— 逐筆講。
        bits.push(`<div class="muted">${r.refused.length} 個沒有排入：${
          esc(r.refused.map((x) => `${x.label}（${x.reason}）`).join('、'))}</div>`);
      }
      submitNote(bits.join(''));
    } else {
      const r = await postRetry('/api/fetch/queue/resume-capped');
      submitNote(`<div class="good">已排入 ${r.requeued} 個續抓 ——
        從上次停下來的<b>游標</b>接下去，不是從第 1 頁重來。</div>`
        + (r.refused?.length
          ? `<div class="muted">${r.refused.length} 個沒有排入（已經在佇列裡）</div>` : ''));
    }
    await refreshFetchQueue();
  } catch (e) {
    submitNote(`<div class="bad">送出失敗：${esc(e.message)}</div>`);
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
      btn.textContent = r.refused_reason || '排不進去';
      return;
    }
    btn.textContent = isResume ? '已排入續抓' : '已重新排入';
    await refreshFetchQueue();
  } catch (e) {
    btn.textContent = `失敗：${e.message}`;
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
  $('submitUrls').textContent = '送出（請先按解析）';
});

$('clearFetchQueue').addEventListener('click', async () => {
  const r = await api('/api/fetch/queue', { method: 'DELETE' });
  $('fetchQueueSummary').textContent = `已清掉 ${r.cleared} 個還沒開始的`;
  refreshFetchQueue();
});

$('clearRateLimit').addEventListener('click', async () => {
  await api('/api/fetch/rate-limit', { method: 'DELETE' });
  refreshFetchQueue();
});
