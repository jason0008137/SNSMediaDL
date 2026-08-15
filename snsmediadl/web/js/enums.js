// 前端用得到的值域，與 `db/enums.py` 對應。
//
// ⚠️ 加值時**三個地方都要改**：這裡、`db/enums.py`、以及一個 alembic migration
// （CHECK constraint 不會自己跟著 enum 改）。少改一處的症狀是「選了新選項存不進去」。
//
// 這一份被媒體詳情面板與帳號卡共用 —— 拆模組時它一度只留在 media.js，
// 結果帳號頁整片空白（`ReferenceError: opts is not defined`）。

export const RATINGS = ['', 'sfw', 'r18'];
// 要與 db/enums.py 的 ContentType 一致。加值時記得也要有 alembic migration ——
// CHECK constraint 不會自己跟著 enum 改。
export const CONTENTS = ['', 'illust', 'irl', 'mod', 'ai', '3d', 'photograph', 'other'];
export const opts = (list, cur) =>
  list.map((v) => `<option value="${v}" ${v === (cur || '') ? 'selected' : ''}>${v || '（未標）'}</option>`).join('');
