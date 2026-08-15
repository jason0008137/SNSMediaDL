// HTTP 包裝。錯誤一律拋出 —— 呼叫端自己決定怎麼呈現，
// 這一層不做「失敗就回空陣列」那種會把問題藏起來的事。

export async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`${res.status} ${text.slice(0, 200)}`);
  }
  return res.status === 204 ? null : res.json();
}
