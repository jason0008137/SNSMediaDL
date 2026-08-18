// DOM 小工具與共用元件。

export const $ = (id) => document.getElementById(id);

export const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// ── 五星評分元件 ───────────────────────────────────────
// ⚠️ 這是「評分」，與 rating（sfw / r18 分級）是**兩件事**。
// 後端欄位叫 stars，前端也一律用 stars，不要混用 rating 這個字。

/** 五顆星的 HTML。`value` 為 null 代表未評分（不是 0 分）。 */
export function starsHtml(value, cls = '') {
  const stars = [1, 2, 3, 4, 5].map((n) =>
    `<button type="button" class="star${value && n <= value ? ' on' : ''}" data-n="${n}"
             aria-label="${n} 星">★</button>`).join('');
  // 原生 title 已全站淘汰（見 js/tooltip.js）：這一句是「怎麼清除評分」的
  // 唯一說明，掛在 title 上等於鍵盤與觸控使用者永遠看不到。
  return `<span class="stars ${cls}" data-stars="${value ?? ''}"
                data-tip="點星星評分；再點同一顆可清除">${stars}</span>`;
}

export function paintStars(root, value) {
  root.dataset.stars = value ?? '';
  root.querySelectorAll('.star').forEach((b) => {
    b.classList.toggle('on', value !== null && Number(b.dataset.n) <= value);
  });
}

/** 處理一次點擊。回傳 false = 這一下不是點在星星上，呼叫端自己接手。
 *
 *  拆出來是為了**事件委派**：清單頁一頁 100 張卡，每張綁 5 顆星就是 500 個
 *  listener，而且每次重畫都會再產生一批。容器上一個 listener 就夠。 */
export async function handleStarClick(ev, onSet, onError) {
  const btn = ev.target.closest?.('.star');
  if (!btn) return false;
  // 帳號卡與媒體格子本身都有 click handler，不擋的話會順便開詳情／切換選取
  ev.stopPropagation();
  ev.preventDefault();
  const root = btn.closest('.stars');
  const before = root.dataset.stars ? Number(root.dataset.stars) : null;
  const n = Number(btn.dataset.n);
  // 再點同一顆 = 清除。這是唯一的清除方式，所以元件的氣泡要寫出來。
  const next = before === n ? null : n;
  paintStars(root, next);
  try {
    await onSet(next);
  } catch (e) {
    paintStars(root, before);   // 還原，不要顯示一個沒存進去的值
    if (onError) onError(e);
  }
  return true;
}

/** 綁定**單一**五星元件（詳情面板那種只有一個的場合）。
 *  清單頁請改用 `handleStarClick` 做委派。 */
export function wireStars(root, onSet, onError) {
  root.addEventListener('click', (ev) => handleStarClick(ev, onSet, onError));
}

// TB 是必要的，不是防禦性的：正式庫總計 1.27 TB。
// 少了 TB 這一級，它會顯示成「1305.7 GB」—— 讀得懂但沒人看得快。
export const fmtBytes = (n) => {
  if (!n) return '—';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
};

export const fmtWhen = (iso) => (iso ? String(iso).slice(0, 10) : '—');

// ── 讀不到檔案時，問出**為什麼** ─────────────────────
//
// `<img>` / `<video>` 的 error 事件**拿不到狀態碼**，所以要補一次 HEAD。
// 只有已經失敗的那幾格會付這個成本。
//
// ⚠️ 三個呼叫端（格線縮圖、詳情預覽、放大檢視）共用同一份對照表。
// 各寫一份的結果是文案漂移 —— 而這幾句話正是使用者唯一能拿到的診斷：
// 「檔案被刪了」與「那顆碟沒插」在畫面上長得一模一樣，措辭一旦不同步，
// 就沒人知道哪一句才是真的。
const FILE_ERRORS = {
  404: '讀不到原檔（404）—— 檔案被刪除，或那顆碟沒插。\n'
     + 'DB 記的路徑是匯入當下記下的字串，沒有驗證過檔案還在不在。',
  403: '這個檔案不在允許的媒體目錄內（403）—— 換過下載目錄的話，\n'
     + '把舊目錄加進 config.toml 的 extra_media_roots。',
  409: '這一筆還沒下載完成。',
  415: '這個格式生不出縮圖。',
  500: '原檔壞了（縮圖產不出來）。',
  // ⚠️ 503 與 415 是**兩件事**：一個是「裝一下 ffmpeg 就好」，
  // 一個是「這個檔沒救」。混用的話使用者永遠不會去裝。
  503: '影片縮圖需要 ffmpeg，或縮圖排隊逾時。\n'
     + '設定頁有偵測結果；圖片與 ugoira 動圖不受影響。',
};

/** 回一句可行動的原因。問不到就回 null —— **不要猜**。 */
export async function fileErrorText(mediaId, { thumb = false } = {}) {
  const url = `/api/media/${mediaId}/${thumb ? 'thumb' : 'file'}`;
  try {
    const r = await fetch(url, { method: 'HEAD' });
    // ⚠️ 後端說「拿得到」但畫面顯示失敗，是**另一種**故障，不可以套用
    // 上面那幾句（尤其不能說「檔案被刪除」—— 它明明還在）。
    // 實際成因通常是瀏覽器解不了那個編碼，或檔案下載到一半就中斷了。
    if (r.ok) {
      return '檔案讀得到（HTTP 200），但瀏覽器顯示不出來。\n'
           + '可能是不支援的編碼，或檔案不完整。';
    }
    return FILE_ERRORS[r.status] || `讀不到（HTTP ${r.status}）。`;
  } catch {
    // 連 HEAD 都發不出去 —— 後端沒在跑，或網路斷了。這也是答案。
    return null;
  }
}
