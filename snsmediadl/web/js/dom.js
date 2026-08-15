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
