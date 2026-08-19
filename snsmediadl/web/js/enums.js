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
// ⚠ 這裡原本還有一個 `opts()`，用來產生 `<option>` 字串。原生 `<select>`
// 全面退場（改用 dom.js 的 singleDrop / multiDrop）之後它沒有任何呼叫者，
// 已移除 —— 留著會讓下一個人以為「這裡還有 select 可以用」。
//
// ⚠ 上面的 RATINGS / CONTENTS **不要因為看起來沒人用就刪**：
// tests/test_enums_sync.py 直接用正規表示式抽這兩個陣列去跟 db/enums.py 對，
// 刪掉會讓那條「三份值域必須一致」的測試靜默失效。

// 多選篩選用的值域（不含空字串 —— 「不選」就是不選，沒有「全部」這個值）。
//
// ⚠️ `ugoira` 照列，即使 2026-08-15 的分布表沒有它。enum 有這個值就要能篩，
// 「看起來沒資料所以藏起來」會讓真的有資料的那天沒有人發現。
export const KINDS = ['photo', 'video', 'animated_gif', 'ugoira'];
export const RATING_VALUES = ['sfw', 'r18'];
export const CONTENT_VALUES = ['illust', 'irl', 'mod', 'ai', '3d', 'photograph', 'other'];
