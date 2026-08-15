// modal / 側邊抽屜 / 確認對話框。**取代 `window.confirm()`。**
//
// 為什麼不用 confirm()：它會**擋住整個分頁**（連背景輪詢都停），樣式跟介面
// 完全不搭，而且在多行文字上排版很差 —— 而這個專案最重要的一段確認文字
// （刪除帳號的預演：幾則貼文、幾筆媒體、幾個檔案會留在磁碟上）正是多行的。
//
// ⚠️ 換掉 confirm() 的**唯一目的是換實作，不是換文案**。刪除預演屬於
// 產品層摩擦，一字不減。
//
// 這裡集中處理四件每個呼叫端都會忘記的事：
//   1. focus trap（Tab 不會跑到背後的畫面去）
//   2. Esc 關閉
//   3. 開啟時背景不捲動
//   4. 關閉後 focus 回到原本的觸發元素 —— 少了它，鍵盤使用者關掉抽屜之後
//      焦點會掉回 <body>，等於從頭再 Tab 一次

import { esc } from './dom.js';

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]),'
  + ' select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

// 可以疊：帳號抽屜裡按下「刪除記錄」會在抽屜之上再開一個確認框。
const stack = [];

function root() {
  return document.getElementById('overlayRoot');
}

function trapKeys(ev) {
  const top = stack[stack.length - 1];
  if (!top) return;
  if (ev.key === 'Escape') {
    ev.stopPropagation();
    top.close();
    return;
  }
  if (ev.key !== 'Tab') return;
  const items = [...top.el.querySelectorAll(FOCUSABLE)].filter((n) => n.offsetParent !== null);
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  if (ev.shiftKey && document.activeElement === first) {
    ev.preventDefault();
    last.focus();
  } else if (!ev.shiftKey && document.activeElement === last) {
    ev.preventDefault();
    first.focus();
  }
}

/** 開一個 overlay。回傳 `{ el, body, close }`。
 *
 *  `kind`：'modal'（置中）｜'drawer'（右側滑入）｜'small'（窄的置中，確認框用）
 *  `body`：HTML 字串。`onMount(bodyEl, handle)` 在插入 DOM 之後呼叫，綁事件用。 */
export function openOverlay({ kind = 'modal', title, subtitle = '', body = '',
                             foot = '', onMount, onClose } = {}) {
  const opener = document.activeElement;
  const back = document.createElement('div');
  back.className = `ovl-backdrop ${kind === 'modal' ? '' : kind}`;
  back.innerHTML = `
    <div class="ovl-panel" role="dialog" aria-modal="true" aria-label="${esc(title)}">
      <div class="ovl-head">
        <div>
          <h2>${esc(title)}</h2>
          ${subtitle ? `<div class="sub">${esc(subtitle)}</div>` : ''}
        </div>
        <button type="button" class="close" data-ovl-close aria-label="關閉">×</button>
      </div>
      <div class="ovl-body">${body}</div>
      ${foot ? `<div class="ovl-foot">${foot}</div>` : ''}
    </div>`;

  const handle = {
    el: back,
    body: back.querySelector('.ovl-body'),
    close() {
      const i = stack.indexOf(handle);
      if (i === -1) return;            // 已經關過了（例如 Esc 與按鈕同時觸發）
      stack.splice(i, 1);
      back.remove();
      if (!stack.length) {
        document.body.classList.remove('overlay-open');
        document.removeEventListener('keydown', trapKeys, true);
      }
      // 焦點回到原本的觸發元素。它可能已經被重新渲染掉了，所以要檢查還在不在。
      if (opener instanceof HTMLElement && opener.isConnected) opener.focus();
      if (onClose) onClose();
    },
  };

  // 點背板關閉，但點面板內部不關 —— 拖曳選字時滑鼠放開在背板上不該關掉。
  back.addEventListener('mousedown', (ev) => {
    if (ev.target === back) handle.close();
  });
  back.addEventListener('click', (ev) => {
    if (ev.target instanceof Element && ev.target.closest('[data-ovl-close]')) handle.close();
  });

  root().appendChild(back);
  if (!stack.length) {
    document.body.classList.add('overlay-open');
    document.addEventListener('keydown', trapKeys, true);
  }
  stack.push(handle);

  if (onMount) onMount(handle.body, handle);
  // 開啟後把焦點送進去，否則 Tab 會從 <body> 開始跑到背後的畫面
  const firstField = handle.body.querySelector(FOCUSABLE)
    || back.querySelector('.ovl-foot ' + FOCUSABLE)
    || back.querySelector('[data-ovl-close]');
  firstField?.focus();
  return handle;
}

/** 破壞性動作的確認。回傳 Promise<boolean>。
 *
 *  `lines` 是**逐行原文**，一字不減地顯示。呼叫端負責內容，這裡不做摘要 ——
 *  「精簡」正是產品層摩擦最容易被誤刪的地方。 */
export function confirmDialog({ title, lines = [], confirmText = '確定',
                                cancelText = '取消', danger = false }) {
  return new Promise((resolve) => {
    let answered = false;
    const done = (value) => {
      if (answered) return;
      answered = true;
      resolve(value);
    };
    const handle = openOverlay({
      kind: 'small',
      title,
      body: `<p class="confirm-lines">${esc(lines.join('\n'))}</p>`,
      foot: `<button type="button" class="ghost" data-act="no">${esc(cancelText)}</button>
             <button type="button" class="${danger ? 'danger' : ''}" data-act="yes">${esc(confirmText)}</button>`,
      // 用 Esc 或點背板關掉 = 取消。**不可以當成確定** —— 破壞性動作的
      // 預設答案永遠是「不做」。
      onClose: () => done(false),
    });
    handle.el.querySelector('[data-act="no"]').addEventListener('click', () => {
      done(false);
      handle.close();
    });
    handle.el.querySelector('[data-act="yes"]').addEventListener('click', () => {
      done(true);
      handle.close();
    });
    // 焦點預設落在取消上：Enter 連按不會意外刪掉東西。
    handle.el.querySelector('[data-act="no"]').focus();
  });
}
