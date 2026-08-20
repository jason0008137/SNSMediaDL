// 生效條件標籤列 —— 媒體頁與帳號頁共用。
//
// 為什麼是共用模組而不是兩份：帳號頁與媒體頁上一次就是各寫一份控制項
// 才漂移成兩套不同的東西（差異全表見 wiki 的 UI_帳號頁篩選與排序 第零節）。
// 渲染、`__all__`、事件委派留在這裡；**值域與清除動作由各頁自己提供**，
// 那才是兩頁真正不同的部分。
//
// ⚠️ 這一列顯示**全部**條件，不是只顯示「畫面上看不見的」。篩選用的是下拉，
// 收起來時摘要只寫得下「photo…（3）」——「哪三個」答不出來。
// 標籤列是唯一逐一列出每個值的地方，所以不能省。

import { esc } from './dom.js';
import { t } from './i18n.js';

/** 建一個標籤列。
 *
 *  @param host    容器元素（`.chip-bar`）
 *  @param sources 回傳 `[{ kind, id, label, value }]` 的函式。
 *                 清除時傳回 `onClear` 的識別是 `id ?? kind` —— 下拉類的條件
 *                 用它自己的 id，其餘（帳號、creator、搜尋字串）用 `kind`。
 *  @param onClear `(what) => void`。`what` 是 `'__all__'` 或上面那個識別。
 *                 **重查由呼叫端自己做** —— 兩頁的分頁重置方式不一樣
 *                 （媒體頁是游標堆疊，帳號頁是 offset）。
 *  @returns `{ render }`
 */
export function makeChipBar({ host, sources, onClear }) {
  host.addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-clear]');
    if (btn) onClear(btn.dataset.clear);
  });

  function render() {
    const conds = sources();
    host.classList.toggle('hidden', conds.length === 0);
    if (!conds.length) { host.innerHTML = ''; return; }
    host.innerHTML = `<span class="lead">${esc(t('chips.lead'))}</span>`
      + conds.map((c) => `<span class="chip">${esc(c.label)}
        <b>${esc(c.value)}</b>
        <button type="button" data-clear="${esc(c.id || c.kind)}"
                aria-label="${esc(t('chips.remove.aria'))}">×</button></span>`).join('')
      + '<span class="spacer"></span>'
      + `<button type="button" class="ghost small" data-clear="__all__">${
        esc(t('chips.clearall'))}</button>`;
  }

  return { render };
}
