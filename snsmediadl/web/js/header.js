// header：背景活動區、安全模式開關、設定與問題兩個入口。
//
// 這一區的設計依據是 wiki 的 UI_系統模型：**三條背景流程互不相干**
// （下載 worker / 抓取佇列 / extension 採集）。任何一條在跑不代表另外兩條
// 也在跑，所以它們不能擠在同一個角落用同一種樣式顯示 —— 展開後三條分列。
//
// 三條都閒著時要**明說閒置**，不能整區變空白：空白看起來跟壞掉一樣。

import { $, esc } from './dom.js';
import { fmt, t } from './i18n.js';
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
  btn.innerHTML = t(on ? 'safe.on' : 'safe.off');
  btn.dataset.tip = t(on ? 'safe.on.tip' : 'safe.off.tip');
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
    label.textContent = t('activity.backend.down');
    const dead = (k) => `<b>${esc(t(k))}</b><span class="err">${
      esc(t('activity.nostatus'))}</span>`;
    $('flowDownload').innerHTML = dead('activity.down');
    $('flowFetch').innerHTML = dead('activity.fetch');
    $('flowExt').innerHTML = dead('activity.ext');
    $('flowFailed').textContent = t('header.failed');
    btn.dataset.tip = t('activity.backend.tip');
    return;
  }

  const auto = state.settings?.auto_download;
  const pending = q.pending || 0;
  const failed = q.failed || 0;

  // ① 下載 worker
  $('flowDownload').innerHTML = `<b>${esc(t('activity.down'))}</b><span>${
    esc(t(auto === undefined ? 'activity.auto.unknown'
      : auto ? 'activity.auto.on' : 'activity.auto.off'))
  }${pending ? `&ensp;${esc(t('activity.pending'))} <b class="num">${fmt.num(pending)}</b>` : ''}${
    q.running ? `&ensp;<b>${esc(t('activity.downloading.now'))}</b>` : ''}</span>`;

  // ② 抓取佇列
  $('flowFetch').innerHTML = `<b>${esc(t('activity.fetch'))}</b><span>${
    esc(t(state.fetchActive ? 'activity.running' : 'activity.empty'))}</span>`;

  // ③ extension 採集。**只在記憶體裡** —— 重啟後歸零，所以文案要講清楚
  // 「自 backend 啟動以來」，不可以講成「從來沒有」。
  const ing = q.last_ingest;
  $('flowExt').innerHTML = `<b>${esc(t('activity.ext'))}</b><span>${
    esc(ing
      ? t('activity.ext.last', { time: time(ing.at),
                                 posts: fmt.num(ing.posts_new),
                                 media: fmt.num(ing.media_new) })
      : t('activity.ext.none'))}</span>`;

  $('flowFailed').innerHTML = failed
    ? `<span class="err">${esc(t('activity.failed.n', { n: fmt.num(failed) }))}</span>`
    : esc(t('activity.failed.none'));

  // 摘要一句。優先序：壞消息 > 在跑 > 閒置。
  if (failed) {
    btn.classList.add('bad');
    label.textContent = t('activity.summary.failed', { n: fmt.num(failed) });
  } else if (q.running || q.downloading || state.fetchActive) {
    btn.classList.add('busy');
    label.textContent = state.fetchActive && !q.running
      ? t('activity.summary.fetching')
      : t('activity.summary.downloading',
          { n: q.downloading ? fmt.num(q.downloading) : '' }).trim();
  } else if (pending) {
    label.textContent = t('activity.summary.pending', { n: fmt.num(pending) });
  } else {
    label.textContent = t('activity.summary.idle');
  }
  btn.dataset.tip = t('activity.tip');
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
