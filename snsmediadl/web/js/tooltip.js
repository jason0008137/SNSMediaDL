// 自製 tooltip。**取代原生 `title`，全站不再使用 title 屬性。**
//
// 原生 title 的四個問題，每一個在這個專案都真的發生過：
//   1. hover 約一秒才出現，而且**不能換行** —— 多行說明會被壓成一長條
//   2. 鍵盤 focus 拿不到 —— Tab 過去什麼都沒有
//   3. 觸控裝置完全看不到
//   4. 樣式不可控（淺色小方框壓在深色介面上）
// 而 2.0 之前的 index.html 把**整個系統模型**（背景下載與 extension 的關係）
// 塞在一個 title 裡。那段文字現在是設定面板裡的一等公民，不是 hover 才有。
//
// 用法：任何元素加 `data-tip="文字"`，多行用 \n（HTML 裡寫 &#10;）。
// 動態產生的 DOM 不必註冊 —— 這裡用事件委派，掛在 document 上。
//
// ⚠️ **氣泡只放低頻的補充說明。** 約束（X 不能由 backend 抓）、作用範圍
// （分級會改整則貼文）、破壞性警告一律留在畫面上 —— 那些是使用者做決定
// **當下**需要的東西，藏起來就等於沒有。

const DELAY = 150;      // hover 觸發延遲。太短會在滑過時亂閃，太長就等於沒有

let tipEl = null;
let current = null;     // 目前顯示氣泡的那個元素
let timer = null;
let seq = 0;            // 給 aria-describedby 用的唯一 id

function ensureEl() {
  if (!tipEl) {
    tipEl = document.createElement('div');
    tipEl.className = 'tip hidden';
    tipEl.setAttribute('role', 'tooltip');
    document.body.appendChild(tipEl);
  }
  return tipEl;
}

function place(target) {
  const r = target.getBoundingClientRect();
  const t = tipEl.getBoundingClientRect();
  const gap = 8;
  // 預設放下面；下面放不下就翻到上面。
  let top = r.bottom + gap;
  if (top + t.height > window.innerHeight - 8) top = Math.max(8, r.top - t.height - gap);
  // 水平置中對齊觸發元素，再夾回視窗內 —— 靠右的 header 按鈕不夾的話會被切掉
  let left = r.left + r.width / 2 - t.width / 2;
  left = Math.min(Math.max(8, left), window.innerWidth - t.width - 8);
  tipEl.style.top = `${top}px`;
  tipEl.style.left = `${left}px`;
}

function show(target) {
  const text = target.dataset.tip;
  if (!text) return;
  const el = ensureEl();
  el.textContent = text;
  el.classList.remove('hidden');
  if (!el.id) el.id = `tip-${++seq}`;
  // 螢幕閱讀器要唸得到。原生 title 至少有這個，自製的不能倒退。
  target.setAttribute('aria-describedby', el.id);
  current = target;
  place(target);
}

export function hideTip() {
  clearTimeout(timer);
  if (current) current.removeAttribute('aria-describedby');
  current = null;
  if (tipEl) tipEl.classList.add('hidden');
}

function target(ev) {
  return ev.target instanceof Element ? ev.target.closest('[data-tip]') : null;
}

export function initTooltips() {
  document.addEventListener('mouseover', (ev) => {
    const el = target(ev);
    if (!el || el === current) return;
    hideTip();
    clearTimeout(timer);
    timer = setTimeout(() => show(el), DELAY);
  });

  document.addEventListener('mouseout', (ev) => {
    const el = target(ev);
    // 移到氣泡自己身上不算離開（氣泡是 pointer-events:none，這裡只是保險）
    if (el && el === current && ev.relatedTarget && el.contains(ev.relatedTarget)) return;
    if (el) hideTip();
  });

  // 鍵盤：Tab 到就出現，不用等 150 ms（使用者不是「滑過」，是刻意停在這裡）
  document.addEventListener('focusin', (ev) => {
    const el = target(ev);
    if (!el) return;
    // :focus-visible = 鍵盤操作。滑鼠點一下按鈕也會 focus，那時不該彈氣泡
    if (el.matches(':focus-visible')) show(el);
  });
  document.addEventListener('focusout', (ev) => {
    if (target(ev) === current) hideTip();
  });

  // 觸控：沒有 hover 這個概念，點一下當作要看說明。
  // 只認 touch —— 滑鼠的 click 由 focus 那條處理，不要重複彈。
  document.addEventListener('pointerdown', (ev) => {
    if (ev.pointerType !== 'touch') { hideTip(); return; }
    const el = target(ev);
    if (el && el !== current) show(el);
    else hideTip();
  });

  // Esc 關閉。氣泡遮住底下的東西時要有辦法趕走它。
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') hideTip();
  });

  // 捲動與縮放後座標就不對了。重新定位不划算（氣泡本來就是暫時的），直接關掉。
  window.addEventListener('scroll', hideTip, true);
  window.addEventListener('resize', hideTip);
}
