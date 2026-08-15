// header：背景活動區、安全模式開關、設定與問題兩個入口。
//
// 這一區的設計依據是 wiki 的 UI_系統模型：**三條背景流程互不相干**
// （下載 worker / 抓取佇列 / extension 採集）。任何一條在跑不代表另外兩條
// 也在跑，所以它們不能擠在同一個角落用同一種樣式顯示 —— 展開後三條分列。
//
// 三條都閒著時要**明說閒置**，不能整區變空白：空白看起來跟壞掉一樣。

import { $, esc } from './dom.js';
import { state, safeMode, setSafeMode, onSafeModeChange } from './state.js';
import { onQueueChange } from './queue.js';
import { openProblems } from './views/problems.js';
import { openSettings } from './views/settings.js';

// ── 安全模式 ───────────────────────────────────────────
//
// ⚠️ 訊號方向：**開著是預設、是受保護的狀態，不需要一直提醒**；
// 關著才是例外，才需要顯眼。2.0 之前是反過來的（開著時給一條整頁寬的
// 綠色橫幅），那條橫幅已經刪掉。
export function renderSafeToggle() {
  const on = safeMode();
  const btn = $('safeBtn');
  btn.setAttribute('aria-pressed', String(on));
  // 關閉時三重載體：圖示 + 文字 + 加粗的邊框。灰階列印仍可辨識。
  btn.innerHTML = on
    ? '👁 安全模式'
    : '⚠ 安全模式關閉 — 顯示 R18';
  btn.dataset.tip = on
    ? '開著：媒體頁不顯示標為 r18 的貼文。\n這只影響媒體查詢，帳號頁與抓取頁不受影響。'
    : '關閉中：媒體頁會顯示 r18 內容。\n點一下改回安全模式。';
  document.body.classList.toggle('safe', on);
}

// ── 背景活動區 ─────────────────────────────────────────

const time = (iso) => (iso ? String(iso).slice(11, 16) : '—');

function renderActivity() {
  const q = state.queue;
  const btn = $('activityBtn');
  const label = $('activityLabel');
  btn.classList.remove('busy', 'bad');

  if (!q) {
    // backend 無回應要講出來，而且要能與「閒置」分辨 ——
    // 兩者都是「沒有東西在跑」，但一個是正常、一個是完全不能用。
    btn.classList.add('bad');
    label.textContent = 'backend 無回應';
    $('flowDownload').innerHTML = '<b>① 下載</b><span class="err">問不到狀態</span>';
    $('flowFetch').innerHTML = '<b>② 抓取佇列</b><span class="err">問不到狀態</span>';
    $('flowExt').innerHTML = '<b>③ Extension</b><span class="err">問不到狀態</span>';
    $('flowFailed').textContent = '失敗 —';
    btn.dataset.tip = 'backend 沒有回應。\n它沒在跑的話，extension 送出的東西也不會進來。';
    return;
  }

  const auto = state.settings?.auto_download;
  const pending = q.pending || 0;
  const failed = q.failed || 0;

  // ① 下載 worker
  $('flowDownload').innerHTML = `<b>① 下載</b><span>${
    auto === undefined ? '設定未知'
      : auto ? '開 —— 每幾秒自己撿 pending 來抓' : '關（不會自己抓）'
  }${pending ? `　待下載 <b class="num">${pending}</b> 筆` : ''}${
    q.running ? '　<b>正在下載</b>' : ''}</span>`;

  // ② 抓取佇列
  $('flowFetch').innerHTML = `<b>② 抓取佇列</b><span>${
    state.fetchActive ? '執行中' : '空'}</span>`;

  // ③ extension 採集。**只在記憶體裡** —— 重啟後歸零，所以文案要講清楚
  // 「自 backend 啟動以來」，不可以講成「從來沒有」。
  const ing = q.last_ingest;
  $('flowExt').innerHTML = `<b>③ Extension</b><span>${
    ing
      ? `上次 ${time(ing.at)} 送入 ${ing.posts_new} 則 / ${ing.media_new} 個媒體`
      : '自 backend 啟動以來沒有收到'}</span>`;

  $('flowFailed').innerHTML = failed
    ? `<span class="err">⚠ 失敗 ${failed} 筆</span>`
    : '失敗 0 筆';

  // 摘要一句。優先序：壞消息 > 在跑 > 閒置。
  if (failed) {
    btn.classList.add('bad');
    label.textContent = `⚠ 失敗 ${failed}`;
  } else if (q.running || q.downloading || state.fetchActive) {
    btn.classList.add('busy');
    label.textContent = state.fetchActive && !q.running ? '抓取中' : `下載中 ${q.downloading || ''}`.trim();
  } else if (pending) {
    label.textContent = `待下載 ${pending}`;
  } else {
    label.textContent = '閒置';
  }
  btn.dataset.tip = '三條背景流程各自獨立。點開看各自的狀態。';
}

function togglePop(open) {
  const pop = $('activityPop');
  const want = open ?? pop.classList.contains('hidden');
  pop.classList.toggle('hidden', !want);
  $('activityBtn').setAttribute('aria-expanded', String(want));
}

export function wireHeader() {
  renderSafeToggle();
  renderActivity();
  onQueueChange(renderActivity);
  onSafeModeChange(renderSafeToggle);

  $('safeBtn').addEventListener('click', () => setSafeMode(!safeMode()));
  $('activityBtn').addEventListener('click', (ev) => {
    ev.stopPropagation();
    togglePop();
  });
  $('settingsBtn').addEventListener('click', () => openSettings());
  $('openProblems').addEventListener('click', () => {
    togglePop(false);
    openProblems();
  });

  // 點別處收起來。氣泡式面板不收的話會擋住底下的內容。
  document.addEventListener('click', (ev) => {
    if (!$('activityPop').classList.contains('hidden')
        && !ev.target.closest('#activityPop')) togglePop(false);
  });
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') togglePop(false);
  });
}
