# SNSMediaDL — X Collector（Chrome extension）

攔截 x.com 自身的 GraphQL 回應，抽出媒體清單，送到本機 backend。

在 x.com 上只讀不寫：不改請求、不自動捲動、不下載檔案。
下載始終是 backend 的職責。

> **v0.8.0 起是自動採集，但只收「你正在看的那個帳號」的貼文。**
> 不用按錄製（v0.7 的錄製鍵已移除），滑就對了。
> 側欄推薦、hover card、轉推牆上別人的東西天生進不來 —— 它們的作者
> 不是網址上的那個人。

## 安裝

1. 先把 backend 跑起來：
   ```bash
   python -m snsmediadl.cli serve
   ```
2. Chrome 開 `chrome://extensions`
3. 開啟「**開發人員模式**」
4. 「**載入未封裝項目**」→ 選這個 `extension/` 資料夾
   （拿到 zip 的話先解壓縮，Chrome 不吃 zip）

## 用法

開啟 `https://x.com/<帳號>/media` —— **右下角會出現一顆小圓鈕**，點它展開面板。
面板可拖曳（拖標題列），位置與展開狀態都會記住。收合時只剩那顆鈕，不擋內容。

```
往下滑 → 數字自己往上跳（只算這一頁的帳號）
 └ 選好「分級 / 類型」（不需要這個帳號已經在資料庫裡）
     └ 按「送出並下載 N 則（@xxx）」→ 面板顯示下載進度直到歸零
```

三件事值得知道：

- **數字永遠是「這一頁的帳號」的**。A 頁 6 則、B 頁 10 則，就各自顯示 6 和 10，
  不會顯示 16。工具列圖示上的 badge 也是每個分頁各自算的。
- **一顆按鈕只送一個帳號**。切走的帳號還有待送時，面板會多一行
  「其他帳號另有待送：@x N」，回那個帳號的頁面就能送（或用 popup 一次送完）。
- **非帳號頁（`/home`、`/explore`…）一則都不收**。那裡的東西是「剛好滑過的」，
  不是「你想要的」。

### 分級與類型是「送這個帳號時要蓋的標籤」

選好的值會**跟著這批貼文一起送出**（`rating` / `contentType` 寫在 payload 裡，
backend 標成 `rating_source=manual`）。

> **為什麼不是改帳號預設值再靠繼承**：帳號還不在資料庫時根本沒有東西可以改，
> 於是每個新帳號的第一批必然以 `rating=NULL` 入庫，而事後改預設**不回溯**。
> 標籤隨 payload 走就沒有這個先有雞還是先有蛋的問題。

送出時帶的是**你此刻在面板上看到的那兩個下拉**（所見即所送）。
下拉的值按帳號記住，換回同一個帳號會自動預填。

### 多個帳號

佇列**依帳號分割**，送出一次只送一個帳號。要送第二個就切過去再按一次。

> 這不只是顯示問題。送出時 backend 是用**每則貼文自己的 user id** 建帳號，
> 但 `screenName` 是整個請求共用的 —— 一次送多個帳號的貼文會把後面帳號的名字
> 寫成第一個帳號的。所以一定是「一個帳號一次請求」。

點工具列圖示看 popup（狀態與設定）：

| 顯示 | 意義 |
|------|------|
| 綠點 + 「backend 已連線」| 一切正常 |
| 紅點 + 「backend 離線」| backend 沒跑，資料**暫存在本機不會遺失** |
| 橘色 badge | **這個分頁**的帳號待送幾則（不是全部帳號的總和）|

backend 沒開時**照樣會採集**，資料留在 extension 裡；backend 起來後按送出就會補上。

## 送出之後真的會下載

送出成功後 extension 會接著打 `POST /api/queue/run`，面板顯示剩餘筆數直到歸零。

> **v0.7.0 之前這件事是壞的**：那顆按鈕只打 `/api/ingest`，而 ingest 只入庫排隊。
> 唯一會抓檔的是 backend 的背景下載迴圈，而它預設是關的 ——
> 按鈕回報綠色的「已送出 N 則」，檔案卻永遠不落地。
> 現在觸發失敗會明講「已入庫 N 則，但無法啟動下載：…」。

`auto_download` 預設仍然關閉（會自己對外發請求的東西預設該是關的），
送出時的觸發是**明確動作**，不受它影響。

## creator 歸屬

面板下排的 creator / 角色下拉是**帳號層級**的屬性（不是這一批的），
所以要等帳號進了資料庫才能設 —— 送出第一批之後就會亮起來。

同一位作者的跨平台帳號與小帳掛同一個 creator，GUI 就能一次看完全部作品。
新增 creator 請到 Web GUI。

## 設定

popup 底部可改 backend URL（預設 `http://127.0.0.1:8765`）。

## 運作方式

```
inject.js   MAIN world, document_start —— patch XHR/fetch，攔 GraphQL 回應
   ↓ window.postMessage
content.js  ISOLATED world —— 抽出貼文與媒體、蒐集 screen_name 對應
   ↓ chrome.runtime.sendMessage
background.js + sync.js  service worker —— 採集閘門、緩衝、去重、POST 到 backend
                                          每分頁 badge 也在 background.js
   ↓
bar.js      ISOLATED world —— 頁面右下角的面板（數字、標籤、送出、進度）
```

幾個不明顯但重要的點：

- **採集閘門在 `sync.js` 的 `enqueue()`，不在攔截層**。`inject.js` 的 patch
  維持常開：patch / unpatch 會有競態，而且攔了不用的成本接近零。
  判斷點只有一個，兩邊各判一次遲早會不一致。
- **「這一頁在看誰」只由網址決定**（`content.js` 的 `screenNameFromUrl`），
  不是由攔到的內容決定 —— 側欄與 hover card 都會帶別人的 user 物件。
- **badge 用 `chrome.action.setBadgeText({tabId})` 每分頁各自設**，全域 badge
  永遠留空。顯示全帳號總和會讓分好的資料看起來像大混池。
  分頁與帳號的對應存在 `chrome.storage.session`（service worker 會被回收）。
- **在帳號頁面上卻對不出 `userId` 會明講**（面板紅字）。那代表
  `screenName → userId` 的對應沒建立起來，症狀（數字一直 0）本身沒有線索。

- **X.com 走 XHR 不走 fetch**。只 patch `fetch` 會一筆都攔不到，且沒有錯誤訊息。
- **用 manifest 宣告 `world: "MAIN"`**，不是動態注入 `<script>` ——
  由 Chrome 保證跑在頁面任何腳本之前，沒有注入競態。實測未捲動即可攔到首屏。
- **送出前問 `/api/known`** 省流量，但**查詢失敗時照送全部** ——
  省流量的功能不可以影響正確性，backend 的去重才是最終防線。
- **離線暫存上限 2000 則**（每個帳號各自算），超過丟最舊的並**在面板標示**。

## 開發流程（重要）

### 自動重載 —— 不用手動點「重新整理」

backend 跑著的時候，extension 每 3 秒問一次 `/api/ext-version`。
那個端點回傳 `extension/` 目錄的**檔案指紋**（mtime + 大小），
一改動就會變，extension 偵測到就自己呼叫 `chrome.runtime.reload()`。

所以改完程式碼**什麼都不用做** —— 幾秒後瀏覽器就是新版了。
（重新整理 x.com 分頁才會重新注入 content script。）

要關掉：`config.toml` 設 `dev_reload = false`。

### 診斷回報 —— 讓開發者看得到擴充功能內部

extension 把錯誤與狀態 POST 到 `/api/ext-log`，可以直接讀：

```bash
curl http://127.0.0.1:8765/api/ext-log
curl "http://127.0.0.1:8765/api/ext-log?level=error"
curl -X DELETE http://127.0.0.1:8765/api/ext-log   # 清空重來
```

回報內容：未攔截的例外、每次 API 請求失敗、面板狀態、攔截到的 operation
與筆數。backend 沒開時這些一樣會進瀏覽器 console（前綴 `[SNSMediaDL:*]`）。

> 這解決的是「擴充功能跑在瀏覽器裡，開發時看不到它出了什麼事」——
> 沒有這個就只能靠人用文字轉述症狀。

## ⚠️ 為什麼所有請求都走 service worker

MV3 的 **content script 發跨來源請求時帶的是「頁面的 origin」**（這裡是 x.com），
受頁面的 CORS 規範管，不是擴充功能的。所以 content script 直接
`fetch('http://127.0.0.1:8765/...')` 會被擋掉，症狀是紅字 `Failed to fetch`，
連帶讓面板抓不到帳號資料而把整組下拉停用。

service worker 的 origin 是 `chrome-extension://` 且有 `host_permissions`，
所以**所有** backend 請求都經由 `chrome.runtime.sendMessage({type:'apiFetch'})`
轉給它。新增功能時請沿用，不要在 content script 裡直接 fetch。

## 測試

```bash
node extension/test_extract.mjs     # 抽取邏輯（吃真實 fixture）
node extension/test_sync.mjs        # 傳輸：分帳號、離線、溢位、降級
node extension/test_url.mjs         # 從網址判斷帳號 + 換帳號要通知 badge
node extension/test_capture.mjs     # 採集閘門：只收本頁帳號、標籤歸屬、對不出來要吵
node extension/test_bar.mjs         # 面板渲染：每頁數字、送出鈕、錯誤優先序
```

全部載入原始碼本體執行，不是複製一份，所以不會有雙份程式碼漂移。

## 除錯

| 症狀 | 檢查 |
|------|------|
| 滑了半天數字不動 | 在帳號頁面嗎？`/home` 不收。面板紅字若說「對不出 userId」就重新整理 |
| 一則都沒收到 | 頁面走快取沒發新請求 —— 重新整理後再滑一次 |
| 送出成功但沒檔案 | 面板會講「無法啟動下載」；沒講的話看 `/api/queue/status` |
| badge 一直空白 | `chrome://extensions` → 本擴充功能 → 「Service Worker」看 console |
| popup 顯示離線 | backend 有沒有跑？URL 對不對？ |
| 頁面壞掉 / 圖片不出來 | response body 被消耗掉了 —— fetch 分支的 clone 沒做對 |

頁面 console 有 `[SNSMediaDL]` 前綴訊息，記錄每次攔截抽到幾則貼文、幾個媒體。

## 已知限制

- 只支援 x.com
- 已載入過的分頁走快取不再發請求 —— 要換帳號或捲到未載入處才有新資料
- `platform_media_key` 尚未送出，媒體識別暫時走 `(post_id, ordinal)`
- operation ID hash 會隨前端 build 輪換，但本擴充功能用 **operation 名稱**比對，不受影響
