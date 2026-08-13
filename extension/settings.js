// 設定存取。backend URL 可改，因為使用者可能改 port。

// ⚠️ 沒有 autoSync。送出一律由人按 —— 標籤是送出當下決定的，
// 自動送等於在使用者選標籤之前就入庫，而 ingest 之後改分級不會回溯。
const DEFAULTS = {
  backendUrl: 'http://127.0.0.1:8765',
};

const KEY = 'settings';

export async function getSettings() {
  const stored = await chrome.storage.local.get(KEY);
  return { ...DEFAULTS, ...(stored[KEY] || {}) };
}

export async function setSettings(patch) {
  const current = await getSettings();
  const next = { ...current, ...patch };
  // 去掉結尾斜線，否則會組出 //api/ingest
  if (typeof next.backendUrl === 'string') {
    next.backendUrl = next.backendUrl.trim().replace(/\/+$/, '');
  }
  await chrome.storage.local.set({ [KEY]: next });
  return next;
}
