// 設定面板（header 的齒輪）。
//
// 這裡是**系統模型的家**。2.0 之前，「背景下載開著會發生什麼、它與 extension
// 的『送出並下載』有什麼不同」這段話被塞在一個 `title` 屬性裡 —— hover 一秒
// 才出現、不能換行、鍵盤與觸控都拿不到。那是整個系統模型的核心，現在是
// 這個面板裡的一等公民文字。
//
// ⚠️ 設定不是工作面，所以它不佔一級位置；但它也不准縮成一排沒有說明的開關。

import { esc } from '../dom.js';
import { api } from '../api.js';
import { state, safeMode, setSafeMode, onSafeModeChange } from '../state.js';
import { openOverlay } from '../overlay.js';
import { loadSettings, setAutoDownload } from '../queue.js';

function readonlyRows(s) {
  if (!s) return '<div class="err">讀不到設定（backend 沒有回應）</div>';
  const extra = s.extra_media_roots || [];
  return `
    <dl class="kv">
      <dt>下載目錄</dt><dd>${esc(s.output_root)}</dd>
      <dt>縮圖快取</dt><dd>${esc(s.thumb_root)}</dd>
      <dt>額外媒體目錄</dt><dd>${extra.length
        ? `${extra.length} 個<br>${extra.map(esc).join('<br>')}`
        : '（沒有）'}</dd>
      <dt>抓取頁數上限</dt><dd>${esc(s.fetch_max_pages)} 頁／帳號</dd>
    </dl>
    <p class="note">改這些要編輯 <code>config.toml</code> 並重啟 backend。
      不是漏做的執行期開關 —— 它們決定檔案落在哪裡，執行到一半換掉會讓
      同一批媒體散在兩個地方。</p>`;
}

export async function openSettings() {
  const handle = openOverlay({
    title: '設定',
    body: `
      <div class="ovl-section">
        <h3>背景下載</h3>
        <label class="chk"><input type="checkbox" id="setAutoDl"> <span id="setAutoDlLabel">讀取中…</span></label>
        <p class="note">開啟後每幾秒自己撿待下載的媒體來抓。<br>
          ⚠ 這<b>不影響</b> extension 的「送出並下載」，那是明確觸發
          （<code>/api/queue/run</code>），不受這個開關影響。<br>
          抓取頁的「抓完就下載」也一樣是明確觸發。</p>
      </div>

      <div class="ovl-section">
        <h3>工作安全模式</h3>
        <label class="chk"><input type="checkbox" id="setSafe"> 開啟時媒體頁不顯示標為 r18 的內容</label>
        <p class="note">只影響<b>媒體查詢</b> —— 帳號頁與抓取頁不受它影響。<br>
          目前庫內的 r18：<span id="setR18">計算中…</span></p>
      </div>

      <div class="ovl-section">
        <h3>唯讀資訊</h3>
        <div id="setReadonly">讀取中…</div>
      </div>`,
    onMount: (body) => {
      const dl = body.querySelector('#setAutoDl');
      const safe = body.querySelector('#setSafe');

      const paintAuto = (on) => {
        dl.checked = !!on;
        body.querySelector('#setAutoDlLabel').textContent =
          on ? '開啟中 —— 會自己撿 pending 的媒體來下載' : '關閉中 —— 不會自己抓';
      };
      const paintSafe = () => { safe.checked = safeMode(); };
      paintSafe();
      // header 的開關與這裡是**同一個狀態的兩個控制項**，必須即時同步
      onSafeModeChange(paintSafe);
      safe.addEventListener('change', () => setSafeMode(safe.checked));

      dl.addEventListener('change', async () => {
        const want = dl.checked;
        try {
          const s = await setAutoDownload(want);
          paintAuto(s.auto_download);
        } catch (e) {
          paintAuto(!want);   // 沒切成功就別讓畫面顯示已切換
          body.querySelector('#setAutoDlLabel').textContent = `切換失敗：${e.message}`;
        }
      });

      // 設定值與 r18 筆數各自載入 —— r18 那個是一次 COUNT（正式庫約一秒），
      // 不該讓整個面板等它。
      loadSettings().then((s) => {
        paintAuto(s?.auto_download);
        body.querySelector('#setReadonly').innerHTML = readonlyRows(s);
      });
      api('/api/media/count?rating=r18')
        .then((d) => {
          body.querySelector('#setR18').textContent = `${d.total.toLocaleString()} 筆`;
        })
        .catch((e) => {
          body.querySelector('#setR18').textContent = `算不出來：${e.message}`;
        });
    },
  });
  // 已經載過就先用手上的值畫，避免面板一開是空的
  if (state.settings) {
    handle.body.querySelector('#setReadonly').innerHTML = readonlyRows(state.settings);
  }
  return handle;
}
