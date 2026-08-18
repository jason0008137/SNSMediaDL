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

/** pixiv 憑證：**只說有沒有設，永遠不顯示值的任何片段。**
 *
 *  沒設的時候要說「會怎樣 + 怎麼填」。只寫「未設定」的話，使用者仍然
 *  會去抓一輪，然後撞上一個看起來像 Cloudflare 擋人的 403。 */
function credentialRow(s) {
  const has = s.credentials?.pixiv;
  if (has) return '已設定';
  return `<span class="warn">未設定</span> —— pixiv 抓取一定會失敗。<br>
    在 <code>config.toml</code> 填
    <code>platform_credentials = { pixiv = "&lt;PHPSESSID&gt;" }</code>
    後重啟 backend（填法見 <code>config.toml.example</code>）。`;
}

/** 偵測到的來源。**一定要講出來，不能只說「已安裝」** ——
 *  三層偵測（你指定的 / 系統 PATH / pip 帶的）命中哪一層，決定了你在用
 *  哪個版本的 ffmpeg，而那正是「這個檔為什麼抽不出影格」的第一個問題。 */
const FFMPEG_SOURCE = {
  config: 'config.toml 指定的',
  path: '系統 PATH 上的',
  bundled: 'imageio-ffmpeg 隨套件帶的',
};

/** ffmpeg 偵測結果。沒裝的時候要說**影響範圍** ——
 *  「未安裝」三個字不足以讓人判斷要不要去裝。 */
function ffmpegRow(s) {
  const f = s.ffmpeg;
  if (!f) return '（後端沒有回報）';
  if (!f.available) {
    return `<span class="warn">未安裝</span> —— 影片縮圖不可用（格線會顯示原因）。
       圖片與 ugoira 動圖<b>不受影響</b>。`;
  }
  // 來源不認得時就不硬掰一個標籤 —— 路徑本身仍然是完整的答案。
  const from = FFMPEG_SOURCE[f.source];
  return `${esc(f.path)}${from ? `<br><span class="note">來源：${esc(from)}</span>` : ''}`;
}

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
      <dt>pixiv 憑證</dt><dd>${credentialRow(s)}</dd>
      <dt>ffmpeg</dt><dd>${ffmpegRow(s)}</dd>
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
        <h3>帳號身分補齊</h3>
        <p class="note">匯入進來的帳號多半只有名字、沒有平台 id。採集到它們時
          backend 會就地補上，必要時把重複的兩列合併。<br>
          ⚠️ 判斷依據是帳號名，而平台的帳號名會被釋出再被別人註冊 ——
          <b>這裡是唯一能回溯歸錯戶的地方</b>。</p>
        <div id="setHeals">讀取中…</div>
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
      api('/api/identity/heals?limit=20')
        .then((d) => {
          const box = body.querySelector('#setHeals');
          const pending = `<p class="note">還有 <b>${d.pending.toLocaleString()}</b> 個帳號只有名字。</p>`;
          box.innerHTML = pending + (d.items.length
            ? `<dl class="kv">${d.items.map((h) => `
                <dt>${esc(h.at.slice(0, 16).replace('T', ' '))}</dt>
                <dd>${esc(h.platform)} @${esc(h.screen_name)} —— ${
                  h.kind === 'merge'
                    ? `合併兩列，搬了 ${h.moved_posts} 則貼文`
                    : '補上平台 id'}<br>
                  <span class="muted">${esc(h.placeholder_id)} → ${esc(h.real_id)}</span></dd>`).join('')}</dl>`
            // 0 筆是常態（還沒採集過），要看起來像正常而不是壞掉
            : '<p class="note">還沒有補齊過任何帳號。</p>');
        })
        .catch((e) => {
          body.querySelector('#setHeals').textContent = `讀不到：${e.message}`;
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
