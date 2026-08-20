// 問題與日誌。**2.0 之後不是一級 tab，是 header 狀態區點開的 modal。**
//
// 降級的理由：正式庫失敗 0 筆。常駐一個 tab 等於把例外當常態。
// 但降級不等於消失 —— 失敗數為 0 時，header 的狀態區仍要留一個可點的入口，
// 否則使用者根本不知道有這個地方（那是把「隱藏的預設用途」做成常態）。
//
// 而且 0 筆是**預設會看到的狀態**，所以它必須看起來像正常
// （「目前沒有失敗項目」），而不是像壞掉的空白。

import { esc, mountDrops } from '../dom.js';
import { fmt, t } from '../i18n.js';

/** 日誌等級下拉的握把。⚠ 模組層而不是查 DOM：面板每次開都重畫，
 *  而 loadLogs() 也可能在面板還沒建好時被呼叫。 */
let logLevel = null;
import { api } from '../api.js';
import { openOverlay } from '../overlay.js';
import { refreshQueue } from '../queue.js';

async function loadErrors(body) {
  const box = body.querySelector('#errorList');
  let errs;
  try {
    errs = await api('/api/errors');
  } catch (e) {
    box.innerHTML = `<div class="err">${
      esc(t('problems.errors.load', { msg: e.message }))}</div>`;
    return;
  }
  const n = errs.items.length;
  body.querySelector('#errCount').textContent = t('problems.errors.count',
                                                    { n: fmt.num(n) });
  const retryAll = body.querySelector('#retryAll');
  retryAll.disabled = !n;
  // disabled 一定要說得出原因，不然看起來就只是壞掉的按鈕
  retryAll.dataset.tip = t(n ? 'problems.retryall.tip' : 'problems.retryall.none');

  box.innerHTML = n
    ? errs.items.map((e) => `
      <div class="err-row">
        <div>
          <b>${esc(e.screen_name || '?')}</b> · ${esc(e.post_id)} · ${esc(e.kind)}
          <div class="msg">${esc(e.error || t('problems.err.unknown'))}${
            esc(t('problems.err.attempts', { n: fmt.num(e.attempt_count) }))}</div>
        </div>
        <span class="spacer"></span>
        <button data-retry="${e.media_id}">${esc(t('problems.retry'))}</button>
      </div>`).join('')
    : `<p class="muted">${esc(t('problems.errors.none'))}</p>`;
}

async function loadLogs(body) {
  const level = logLevel ? logLevel.get() : '';
  const box = body.querySelector('#logs');
  try {
    const data = await api(`/api/logs?limit=200${level ? `&level=${level}` : ''}`);
    box.innerHTML = data.items.length
      ? data.items.map((r) =>
          `<span class="${esc(r.level)}">${esc(r.ts.slice(11, 19))} [${esc(r.level)}] ${esc(r.message)}</span>`
        ).join('\n')
      : t('problems.logs.none');
  } catch (e) {
    box.textContent = t('problems.logs.load', { msg: e.message });
  }
}

export function openProblems() {
  return openOverlay({
    title: t('problems.title'),
    body: `
      <div class="ovl-section">
        <h3>${esc(t('problems.errors.title'))}&ensp;<span id="errCount" class="muted">—</span></h3>
        <div id="errorList" class="errors"><p class="muted">${
          esc(t('common.loading'))}</p></div>
        <div class="row">
          <span class="spacer"></span>
          <button id="retryAll" disabled>${esc(t('problems.retryall'))}</button>
        </div>
        <p id="retryMsg" class="note"></p>
      </div>
      <div class="ovl-section">
        <h3>${esc(t('problems.logs.title'))}</h3>
        <div class="row">
          <span id="logLevel" class="ms-host"></span>
          <button id="refreshLogs" class="ghost">${esc(t('problems.logs.refresh'))}</button>
        </div>
        <pre id="logs" class="logs">${esc(t('common.loading'))}</pre>
      </div>`,
    onMount: (body) => {
      // 這個面板每次開都重畫 —— 下拉在這裡建。
      // ⚠️ `logLevel` 是模組層變數而不是查 DOM：`loadLogs()` 在面板關掉之後
      // 也可能被呼叫到，那時 querySelector 會回 null。
      ({ logLevel } = mountDrops(body, {
        logLevel: {
          label: t('problems.logs.level'), emptyText: t('problems.logs.level'),
          ariaLabel: t('problems.logs.level.aria'),
          values: [{ value: 'ERROR' }, { value: 'WARNING' }, { value: 'INFO' }],
          onChange: () => loadLogs(body),
        },
      }));
      loadErrors(body);
      loadLogs(body);

      // 事件委派：重試按鈕是動態產生的，逐一綁定會在每次重畫後留下一批舊的
      body.querySelector('#errorList').addEventListener('click', async (ev) => {
        const btn = ev.target.closest('[data-retry]');
        if (!btn) return;
        btn.disabled = true;
        try {
          await api(`/api/media/${btn.dataset.retry}/retry`, { method: 'POST' });
          await loadErrors(body);
          refreshQueue();
        } catch (e) {
          btn.disabled = false;
          body.querySelector('#retryMsg').textContent =
            t('problems.retry.failed', { msg: e.message });
        }
      });

      body.querySelector('#retryAll').addEventListener('click', async (ev) => {
        ev.target.disabled = true;
        try {
          const r = await api('/api/media/retry-failed', { method: 'POST' });
          // 「送出成功 ≠ 事情成功」的分離提示。⚠️ 兩句都留在畫面上 ——
          // 收進氣泡的話，使用者會以為按完就在下載了。
          body.querySelector('#retryMsg').innerHTML =
            `${esc(t('problems.requeued', { n: fmt.num(r.requeued) }))}<br>`
            + esc(t('problems.requeued.rest'));
          await loadErrors(body);
          refreshQueue();
        } catch (e) {
          body.querySelector('#retryMsg').textContent =
            t('common.failed.msg', { msg: e.message });
          ev.target.disabled = false;
        }
      });

      body.querySelector('#refreshLogs').addEventListener('click', () => loadLogs(body));
    },
  });
}
