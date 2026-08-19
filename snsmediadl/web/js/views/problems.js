// 問題與日誌。**2.0 之後不是一級 tab，是 header 狀態區點開的 modal。**
//
// 降級的理由：正式庫失敗 0 筆。常駐一個 tab 等於把例外當常態。
// 但降級不等於消失 —— 失敗數為 0 時，header 的狀態區仍要留一個可點的入口，
// 否則使用者根本不知道有這個地方（那是把「隱藏的預設用途」做成常態）。
//
// 而且 0 筆是**預設會看到的狀態**，所以它必須看起來像正常
// （「目前沒有失敗項目」），而不是像壞掉的空白。

import { esc, mountDrops } from '../dom.js';

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
    box.innerHTML = `<div class="err">讀不到失敗清單：${esc(e.message)}</div>`;
    return;
  }
  const n = errs.items.length;
  body.querySelector('#errCount').textContent = `${n} 筆`;
  const retryAll = body.querySelector('#retryAll');
  retryAll.disabled = !n;
  // disabled 一定要說得出原因，不然看起來就只是壞掉的按鈕
  retryAll.dataset.tip = n ? '把全部失敗的媒體打回佇列' : '沒有失敗項目';

  box.innerHTML = n
    ? errs.items.map((e) => `
      <div class="err-row">
        <div>
          <b>${esc(e.screen_name || '?')}</b> · ${esc(e.post_id)} · ${esc(e.kind)}
          <div class="msg">${esc(e.error || '未知錯誤')}（試過 ${e.attempt_count} 次）</div>
        </div>
        <span class="spacer"></span>
        <button data-retry="${e.media_id}">重試</button>
      </div>`).join('')
    : '<p class="muted">目前沒有失敗項目。</p>';
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
      : '（沒有日誌）';
  } catch (e) {
    box.textContent = `讀不到日誌：${e.message}`;
  }
}

export function openProblems() {
  return openOverlay({
    title: '問題與日誌',
    body: `
      <div class="ovl-section">
        <h3>下載失敗　<span id="errCount" class="muted">—</span></h3>
        <div id="errorList" class="errors"><p class="muted">載入中…</p></div>
        <div class="row">
          <span class="spacer"></span>
          <button id="retryAll" disabled>重試全部失敗</button>
        </div>
        <p id="retryMsg" class="note"></p>
      </div>
      <div class="ovl-section">
        <h3>伺服器日誌</h3>
        <div class="row">
          <span id="logLevel" class="ms-host"></span>
          <button id="refreshLogs" class="ghost">重新整理</button>
        </div>
        <pre id="logs" class="logs">載入中…</pre>
      </div>`,
    onMount: (body) => {
      // 這個面板每次開都重畫 —— 下拉在這裡建。
      // ⚠️ `logLevel` 是模組層變數而不是查 DOM：`loadLogs()` 在面板關掉之後
      // 也可能被呼叫到，那時 querySelector 會回 null。
      ({ logLevel } = mountDrops(body, {
        logLevel: {
          label: '全部等級', emptyText: '全部等級', ariaLabel: '只看哪個等級',
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
          body.querySelector('#retryMsg').textContent = `重試失敗：${e.message}`;
        }
      });

      body.querySelector('#retryAll').addEventListener('click', async (ev) => {
        ev.target.disabled = true;
        try {
          const r = await api('/api/media/retry-failed', { method: 'POST' });
          body.querySelector('#retryMsg').textContent =
            `已把 ${r.requeued} 個重新排入佇列 —— 它們還沒下載，`
            + '要等背景下載開著、或按「立即下載」。';
          await loadErrors(body);
          refreshQueue();
        } catch (e) {
          body.querySelector('#retryMsg').textContent = `失敗：${e.message}`;
          ev.target.disabled = false;
        }
      });

      body.querySelector('#refreshLogs').addEventListener('click', () => loadLogs(body));
    },
  });
}
