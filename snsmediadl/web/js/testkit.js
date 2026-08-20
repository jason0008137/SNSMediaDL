// 雙模測試 harness —— 同一支測試在 node 與瀏覽器裡都跑得動。
//
// 為什麼需要它：公司機**沒有 node**（見 skill `/office-dev`）。既有的 7 支
// `.mjs` 測試在那台機器上一支都跑不了，而那不是「少驗一點」——
// 是「完全沒驗」。瀏覽器是那台機器上唯一的 JS 執行環境，而 GUI 本來就
// 由 uvicorn 送出，所以把測試也送出去跑是零額外成本的。
//
// ⚠️ node 模式的輸出格式**沿用既有測試**（`  ok  名稱` / `  FAIL 名稱`），
// 不要改 —— 家裡的迴路與 CI 讀的是那個格式。
//
// ⚠️ 這裡**不做斷言框架**。專案既有的測試就是一串 `check(name, cond)`，
// 換成 describe/it 只會讓兩種風格並存。這支只補三件 node 專屬的事：
// 讀檔、輸出、結束碼。

const isNode = typeof window === 'undefined';

const cases = [];      // { name, fn }
let out = null;        // 瀏覽器模式下的結果容器

/** 註冊一個測試。`fn` 可以是 async。 */
export function test(name, fn) {
  cases.push({ name, fn });
}

export function assert(cond, msg = '斷言失敗') {
  if (!cond) throw new Error(msg);
}

export function assertEqual(got, want, msg = '') {
  // 物件與陣列用 JSON 比 —— 這些測試比的都是純資料，深比較不必自己寫。
  const a = typeof got === 'object' && got !== null ? JSON.stringify(got) : got;
  const b = typeof want === 'object' && want !== null ? JSON.stringify(want) : want;
  if (a !== b) throw new Error(`${msg}${msg ? '：' : ''}got ${a}，want ${b}`);
}

/** 讀一個與測試檔同目錄（或相對路徑）的文字檔。
 *
 *  ⚠️ 兩種模式讀法完全不同，這正是測試不能雙模的**唯一**原因：
 *  node 用 `node:fs`（瀏覽器裡連 import 都會炸），瀏覽器用 `fetch`。 */
export async function loadText(relPath, importMetaUrl) {
  if (isNode) {
    const { readFileSync } = await import('node:fs');
    const { fileURLToPath } = await import('node:url');
    const { dirname, join } = await import('node:path');
    return readFileSync(join(dirname(fileURLToPath(importMetaUrl)), relPath), 'utf8');
  }
  const url = new URL(relPath, importMetaUrl);
  const res = await fetch(url);
  if (!res.ok) throw new Error(`讀不到 ${relPath}（HTTP ${res.status}）`);
  return res.text();
}

/** 瀏覽器模式：把結果掛到哪個容器。`tests.html` 開頭呼叫一次。 */
export function mount(el) {
  out = el;
}

function line(ok, name, why) {
  if (isNode) {
    console.log(ok ? `  ok  ${name}` : `  FAIL ${name}${why ? ` —— ${why}` : ''}`);
    return;
  }
  const row = document.createElement('div');
  row.className = ok ? 'tk-ok' : 'tk-fail';
  row.textContent = `${ok ? 'ok' : 'FAIL'}  ${name}${why ? ` —— ${why}` : ''}`;
  (out || document.body).appendChild(row);
}

/** 跑完所有註冊的測試。回傳失敗數。 */
export async function run(title = '') {
  if (title) {
    if (isNode) console.log(title);
    else if (out) {
      const h = document.createElement('h2');
      h.textContent = title;
      out.appendChild(h);
    }
  }
  let failed = 0;
  for (const c of cases) {
    try {
      await c.fn();
      line(true, c.name);
    } catch (e) {
      failed += 1;
      // 堆疊要看得見 —— 只印訊息的話，「got undefined」查不出是哪一行。
      line(false, c.name, isNode ? e.message : `${e.message}\n${e.stack || ''}`);
    }
  }
  cases.length = 0;
  const summary = failed ? `${failed} 項失敗` : '全部通過';
  if (isNode) {
    console.log(`\n${summary}`);
    // eslint-disable-next-line no-undef
    process.exit(failed ? 1 : 0);
  } else {
    const el = document.createElement('div');
    el.className = failed ? 'tk-summary tk-fail' : 'tk-summary tk-ok';
    el.textContent = summary;
    (out || document.body).appendChild(el);
  }
  return failed;
}
