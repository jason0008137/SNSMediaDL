// 擴充功能圖示的 popup：連線狀態、統計、backend URL、殘留佇列出口。
//
// ⚠️ 這裡**不做**帳號標記。先前有一個帳號編輯區，用全域的 lastUserId 判斷
// 「目前帳號」—— 那個值會被其他分頁蓋掉，而該區塊能改預設分級與 creator
// 歸屬，設錯就是資料汙染。面板（bar.js）用網址判斷，是唯一正確的來源。

const $ = (id) => document.getElementById(id);

function say(text, isError = false) {
  $('msg').textContent = text;
  $('msg').className = isError ? 'err' : '';
}

async function render() {
  const info = await chrome.runtime.sendMessage({ type: 'getState' });
  const { state, settings } = info;
  const pending = info.pendingTotal ?? 0;

  $('syncedMedia').textContent = state.syncedMedia || 0;
  $('syncedPosts').textContent = state.syncedPosts || 0;
  $('pending').textContent = pending;
  $('lastSync').textContent = state.lastSyncAt
    ? new Date(state.lastSyncAt).toLocaleTimeString()
    : '—';

  if (!$('backend').matches(':focus')) $('backend').value = settings.backendUrl;

  // 連線狀態要一眼看得出來 —— 使用者最需要知道的不是「採集了多少」，
  // 而是「東西到底有沒有進 backend」。
  const dot = $('dot');
  dot.className = 'dot';
  if (state.online === true) {
    dot.classList.add('on');
    $('conn').textContent = 'backend 已連線';
  } else if (state.online === false) {
    dot.classList.add('off');
    $('conn').textContent = 'backend 離線';
  } else {
    $('conn').textContent = '尚未連線';
  }

  if (state.lastError) {
    say(`最後錯誤：${state.lastError}`, true);
  } else if (state.droppedOverflow) {
    say(`暫存超過上限，已丟棄最舊的 ${state.droppedOverflow} 則`, true);
  } else if (state.unresolved) {
    say(`@${state.unresolved.screenName} 對不出 userId，已略過 `
      + `${state.unresolved.count} 則`, true);
  } else if (pending > 0 && state.online === false) {
    say(`backend 沒開，${pending} 則等待送出（不會遺失）`);
  } else {
    say('');
  }
}

$('sync').addEventListener('click', async () => {
  say('送出中…');
  const r = await chrome.runtime.sendMessage({ type: 'syncNow' });
  if (!r?.online) {
    say(`同步失敗：${r?.error || '未知錯誤'}`, true);
  } else if (r.downloadError) {
    say(`已入庫 ${r.sent} 則，但無法啟動下載：${r.downloadError}`, true);
  } else {
    say(r.sent ? `已送出 ${r.sent} 則，下載已啟動` : '沒有新資料要送');
  }
  render();
});

$('reset').addEventListener('click', async () => {
  await chrome.runtime.sendMessage({ type: 'reset' });
  say('已清空');
  render();
});

$('backend').addEventListener('change', async (e) => {
  await chrome.runtime.sendMessage({
    type: 'setSettings',
    patch: { backendUrl: e.target.value },
  });
  say('已更新 backend URL');
  render();
});

render();
setInterval(render, 2000);
