// 跑在 MAIN world（頁面 context），run_at: document_start。
//
// 用 manifest 的 world:"MAIN" 宣告，Chrome 保證在頁面任何腳本之前執行 ——
// 比「content script 動態注入 <script>」可靠，沒有注入競態。
//
// ⚠️ 實測：X.com 的 GraphQL 走 XMLHttpRequest，不走 fetch。
//    兩個都掛，因為前端改版可能換回 fetch。

(() => {
  const TAG = '[SNSMediaDL-spike]';
  const isGql = (u) => typeof u === 'string' && u.includes('/i/api/graphql/');

  const opOf = (url) => {
    try {
      return url.split('/i/api/graphql/')[1].split('?')[0].split('/')[1];
    } catch {
      return '<unknown>';
    }
  };

  const emit = (op, text, via) => {
    // 只往同一個 window 丟，content script 在 ISOLATED world 收
    window.postMessage({ __snsmediadl: true, op, text, via }, '*');
  };

  // --- XMLHttpRequest：X.com 實際使用的路徑 ---
  const O = XMLHttpRequest.prototype.open;
  const S = XMLHttpRequest.prototype.send;

  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this.__snsUrl = url;
    return O.call(this, method, url, ...rest);
  };

  XMLHttpRequest.prototype.send = function (...args) {
    this.addEventListener('load', () => {
      try {
        if (isGql(this.__snsUrl)) {
          emit(opOf(this.__snsUrl), this.responseText, 'xhr');
        }
      } catch {
        /* 攔截失敗絕不能影響頁面 */
      }
    });
    return S.apply(this, args);
  };

  // --- fetch：目前 X.com 沒用，成本低所以一起掛 ---
  const origFetch = window.fetch;
  window.fetch = async function (...args) {
    const res = await origFetch.apply(this, args);
    try {
      const u = typeof args[0] === 'string' ? args[0] : args[0]?.url || '';
      if (isGql(u)) {
        // 必須 clone 再讀：直接讀會消耗 body，X.com 自己就讀不到 → 頁面壞掉
        res.clone().text().then((t) => emit(opOf(u), t, 'fetch')).catch(() => {});
      }
    } catch {
      /* 同上 */
    }
    return res;
  };

  console.log(`${TAG} MAIN world patched at document_start`);
})();
