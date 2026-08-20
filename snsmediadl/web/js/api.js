// HTTP 包裝。錯誤一律拋出 —— 呼叫端自己決定怎麼呈現，
// 這一層不做「失敗就回空陣列」那種會把問題藏起來的事。

import { hasKey, t } from './i18n.js';

/** 後端回的錯誤。`code` 是契約，`detail` 是後備的英文原文。
 *
 *  ⚠️ `message` 已經是**翻譯好的**字 —— 三十幾個顯示點都是直接印
 *  `e.message`，翻譯散在那三十幾處就會漂移（而漏掉的那一處沒有人會發現，
 *  它只是「有時候會冒出一句英文」）。查表在這裡做一次。
 */
export class ApiError extends Error {
  constructor(status, code, detail) {
    super(errorText(code, detail, status));
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

/** code → 這個語系的說法。
 *
 *  ⚠️ **查不到就顯示後端的英文原文，絕不吐「發生未知錯誤」。**
 *  原文至少說得出是什麼事；「未知錯誤」把唯一的線索也吃掉了，而且看起來
 *  像程式壞了而不是像參數給錯了。沒有 code 也沒有 detail 時才退到狀態碼 ——
 *  那時狀態碼是我們手上僅有的東西。
 */
export function errorText(code, detail, status) {
  if (code && hasKey(`err.${code}`)) return t(`err.${code}`);
  if (detail) return detail;
  return t('err.http', { status });
}

/** 一個失敗的 `Response` → `ApiError`。
 *
 *  分出來是給**需要讀 header 的呼叫端**用的（帳號清單要 `X-Total-Count`，
 *  所以它不能走 `api()` —— 那支只回 body）。少了這個，那幾處就會各自寫一份
 *  「丟一個只有狀態碼的 Error」，畫面上就是一句「載入失敗：422」。
 */
export async function toApiError(res) {
  // ⚠️ 回應不一定是 JSON：靜態檔 mount 吃掉的 404、uvicorn 自己回的 502
  // 都是純文字或 HTML。解不開時把前 200 字當 detail —— 那仍然比
  // 「無法解析錯誤」有用。
  let code = null;
  let detail = '';
  const text = await res.text().catch(() => '');
  try {
    const body = JSON.parse(text);
    code = body.code ?? null;
    detail = body.detail ?? '';
  } catch {
    detail = text.slice(0, 200);
  }
  return new ApiError(res.status, code, detail);
}

export async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) throw await toApiError(res);
  return res.status === 204 ? null : res.json();
}
