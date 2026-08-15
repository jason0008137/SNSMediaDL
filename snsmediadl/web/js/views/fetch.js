// 抓取：貼網址批次抓、一鍵更新、佇列與整批評價。
//
// 這是三個畫面裡**模型負載最高**的一頁 —— 唯一真正非同步的地方。
// 非同步流程一定要畫三格：
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
    return `<tr><td>${esc(ln.raw)}</td><td>${esc(ln.target.label)}</td>
            <td>${ln.in_db ? '已在資料庫（會做增量）' : '新帳號'}</td></tr>`;
  });

  const ok = body.lines.filter((l) => !l.error && !l.duplicate).length;
  const wrongTool = body.lines.filter((l) => l.unsupported_platform).length;
  const bad = body.lines.filter((l) => l.error && !l.unsupported_platform).length;

  // 結論先講，逐行在後面。
  const summary = [
    `可抓 ${ok} 個`,
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
  cannot_fetch: '只能由 extension 採集（X）',
  untracked: '已取消追蹤',
  pixiv_excluded: '這次沒有包含 pixiv',
  no_credentials: '缺憑證（config.toml 的 platform_credentials）',
  already_queued: '已經在佇列裡',
};

const mins = (sec) => (sec < 60 ? `${Math.round(sec)} 秒` : `${Math.round(sec / 60)} 分鐘`);

/** 按下去**之前**就要看得見「可抓幾個、抓不動幾個」。
 *  正式庫 4,211 個帳號（90.5%）backend 抓不動 —— 那是多數情況，
 *  不是送出後才報「跳過」的邊緣狀況。 */
export async function refreshScope() {
  const box = $('refreshScope');
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

const capped = (job) => String(job.result?.stopped_because || '').includes('上限');

/** 一列一個容器（共域）—— 12 個 job × 3 欄位，靠間距會在滿載時串行。
 *  狀態用**符號**打頭（✓ ⟳ ⚠ ✗ ⊘），不只靠左邊那條顏色：灰階仍可辨識。 */
function jobHtml(job) {
  const r = job.result || {};
  if (job.state === 'done') {
    const hitCap = capped(job);
    return `<div class="job done"><span class="sym">${hitCap ? '⚠' : '✓'}</span>
      <b>${esc(job.label)}</b>
      <span>新增 ${r.posts_new ?? 0} 則 / ${r.media_new ?? 0} 個媒體</span>
      <span class="${hitCap ? 'capped' : 'muted'}">${esc(r.stopped_because || '')}</span></div>`;
  }
  if (job.state === 'running') {
    return `<div class="job running"><span class="sym">⟳</span><b>${esc(job.label)}</b>
      <span>抓取中…${r.pages ? `第 ${r.pages} 頁` : ''}</span></div>`;
  }
  if (job.state === 'skipped') {
    return `<div class="job skipped"><span class="sym">⊘</span><b>${esc(job.label)}</b>
      <span class="muted">跳過：${esc(job.reason || '')}</span></div>`;
  }
  if (job.state === 'failed') {
    return `<div class="job failed"><span class="sym">✗</span><b>${esc(job.label)}</b>
      <span class="bad">${esc(job.error || '')}</span></div>`;
  }
  return `<div class="job"><span class="sym">⋯</span><b>${esc(job.label)}</b>
    <span class="muted">排隊中</span></div>`;
}

/** ③ 整批評價。**這是本頁最重要的設計。**
 *
 *  現況只說「已排入 12 個帳號」—— 那回答的是第 5 題（系統狀態），
 *  完全沒回答第 6 題（好還是壞、要不要再做什麼）。
 *  三件事一定要講：撞到頁數上限＝沒抓完、失敗的原因、**抓到的還沒下載**。 */
function renderVerdict(st) {
  const box = $('batchVerdict');
  const c = st.counts;
  const active = c.queued + c.running;
  const finished = st.recent.filter((j) => j.state !== 'queued' && j.state !== 'running');
  if (active > 0 || !finished.length) {
    box.classList.add('hidden');
    return;
  }

  const done = finished.filter((j) => j.state === 'done');
  const cappedJobs = done.filter(capped);
  const failed = finished.filter((j) => j.state === 'failed');
  const skipped = finished.filter((j) => j.state === 'skipped');
  const posts = done.reduce((n, j) => n + (j.result?.posts_new || 0), 0);
  const media = done.reduce((n, j) => n + (j.result?.media_new || 0), 0);

  const lines = [];
  if (cappedJobs.length) {
    lines.push(`<div class="warn">⚠ 有 ${cappedJobs.length} 個帳號<b>沒有抓完</b> ——
      撞到頁數上限。要抓完請對它們單獨再跑一次（會從上次的位置繼續）。</div>`);
  }
  if (failed.length) {
    lines.push(`<div class="warn">⚠ ${failed.length} 個失敗 —— 原因見下面逐筆。</div>`);
  }
  if (skipped.length) {
    lines.push(`<div class="muted">⊘ ${skipped.length} 個跳過（多半是只能用 extension 採集的 X 帳號）。</div>`);
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

  const total = active + c.done + c.failed + c.skipped;
  const finishedN = c.done + c.failed + c.skipped;
  $('fetchQueueSummary').textContent = active
    ? `第 ${finishedN + 1} / ${total}　完成 ${c.done}　失敗 ${c.failed}　跳過 ${c.skipped}`
    : (total ? `完成 ${c.done}　失敗 ${c.failed}　跳過 ${c.skipped}` : '佇列是空的');
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
  if (st.running) parts.push(jobHtml(st.running));
  parts.push(...st.queued.map(jobHtml));
  parts.push(...st.recent.map(jobHtml));
  $('fetchQueue').innerHTML = parts.join('')
    || `<div class="empty">佇列是空的 —— 還沒抓過任何東西。<br>
        貼幾個網址，或按「開始更新」讓追蹤中的帳號各跑一次增量。</div>`;

  renderVerdict(st);
}

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
