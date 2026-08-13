# SNSMediaDL

SNS 媒體下載器。**本機單機**運作 —— backend 只綁 `127.0.0.1`，不做認證，不對外開放。

## ⚠ 使用前提

讀完再決定要不要用：

- **這是個人存檔工具，不是服務。** backend 綁 loopback 且**刻意不做認證** ——
  任何能連到那個 port 的東西都能讀你的完整下載歷史、也能叫它去下載。
  把 `host` 改成 `0.0.0.0` 或往外做 port forwarding 等於把這些直接公開，別做。
- **抓取行為的責任在你。** 各平台的服務條款、著作權、以及你抓下來的東西怎麼用，
  都由使用者自負。本專案不提供繞過付費牆或存取權限的手段，也不會去碰非公開內容
  以外的東西 —— 它讀的是「你已經登入的瀏覽器本來就看得到的回應」。
- **限速預設值請不要調高。** X 超速會鎖整個帳號約一天，被鎖的是你的帳號。
  見下方「下載限速」。
- **憑證不要進 git。** `config.toml` 已在 `.gitignore` 裡；pixiv 的 `PHPSESSID`
  寫在那個檔案裡，不要貼到別處。

## 架構

三層，職責切得很死：

```
Chrome Extension  跑在已登入的 x.com 頁面內 —— 攔網站自己的 GraphQL 回應，
       │           抽出媒體 URL + metadata，POST 給 backend。不存檔、不記歷史。
       │ HTTP (localhost)
Backend           FastAPI + SQLite —— 去重、排隊、實際抓檔、命名、存檔。
       │           DB 是歷史的唯一真實來源。
Web GUI           瀏覽歷史、管理帳號與 creator、監控佇列。backend 直出。
```

**採集與下載分離**是核心決定：extension 只做「必須在登入頁面內才做得到」的事，
因為 `chrome.downloads` 控制不了檔名、丟不進指定目錄、也無法後處理；而多數平台的
媒體 URL（`pbs.twimg.com`、`video.twimg.com`）不帶認證就能抓，pixiv 只需要
`Referer` header —— 這些 backend 自己來就好。

推論：**backend 不依賴 extension 也能獨立運作**。已知 URL 的下載、重試、
misskey / mastodon / pixiv 的直抓、GUI 上的一切操作，都不需要瀏覽器開著。

平台是 **adapter 化**的（`snsmediadl/adapters/`）：核心流程（列舉貼文 → 抽媒體 URL →
命名 → 下載 → 記錄）共用，新增平台不動核心。

## 現況

| 元件 | 狀態 |
|------|------|
| Chrome extension（X 採集）| 可用，在帳號頁滑動即採集 → 選標籤 → 送出並下載，`extension/` |
| Backend（DB / API / 下載）| 可用 |
| Web GUI | 可用，`http://127.0.0.1:8765/` |
| 平台 | X（extension 採集）；misskey / mastodon（backend 直抓，實機驗證過）；pixiv（已實作，尚未對真站驗證） |

> 版號規則：backend（`pyproject.toml`，0.1.0）與 extension（`extension/manifest.json`）
> **各自獨立演版**，不對齊數字 —— extension 的版號跟著 manifest 走。

## GUI

backend 直出的網頁，**無建置步驟、無 npm、無 CDN**（離線可用是刻意設計）。

| 分頁 | 功能 |
|------|------|
| 媒體 | 縮圖牆、篩選、**選取模式批次標記**、重新整理、點開看詳情與標記 |
| 抓取 | **貼一堆網址批次抓**（先預覽再送出）、**一鍵更新**已有帳號、佇列進度 |
| 帳號 | 設定預設分級、掛到 creator（含 main / alt / r18_alt 角色）|
| Creators | 建立、瀏覽該作者跨平台跨帳號的全部作品 |
| 問題 | 下載失敗清單與原因、一鍵重試、伺服器日誌 |

**背景下載開關**在頂部，預設關閉 —— 會自己對外發請求的東西預設應該是關的。
切換即時生效，不用重啟。

**選取模式**支援點選、Shift 範圍選取、全選本頁。選取列會同時顯示
「N 個媒體（M 則貼文）」—— 分級掛在 post 不掛 media，選了同一貼文的多張圖
只會影響一則，不講清楚會讓人誤判影響範圍。

格線**不會自動重載**（避免捲動位置亂跳），下載完成後按「⟳ 重新整理」更新狀態。

**工作安全模式預設開啟** —— 排除所有 R18，且頁面頂端有綠色色帶標示。
不做成一個容易被忘記的 checkbox，是因為「以為在安全模式而其實不是」的代價不對稱。

未做縮圖產生（不引入 Pillow），直接給原圖讓瀏覽器縮放；影片用
`<video preload="metadata">` 不自動播放。媒體量大時這是已知取捨。

## 快速開始（Windows）

雙擊 **`start.bat`** —— 會自動檢查 Python、缺套件才安裝、跑 DB migration，然後啟動伺服器。

然後開瀏覽器到 **http://127.0.0.1:8765/** 就是 GUI。

| 檔案 | 用途 |
|------|------|
| `start.bat` | 一鍵啟動伺服器 + GUI（extension 按「送出並下載」即入庫並下載）|
| `status.bat` | 看佇列狀態 |

> `.bat` 本身是純 ASCII，實際邏輯在 `scripts/*.ps1`。
> 原因：cmd.exe 用系統 ANSI codepage 解析 `.bat`，中文寫在裡面會被拆成亂碼指令。

## 手動安裝

```bash
python -m pip install -e ".[dev]"
python -m alembic upgrade head
```

Python 3.10+。

## 用法

### 從 extension 倒出的 JSON 匯入並下載

```bash
python -m snsmediadl.cli import path/to/capture.json --screen-name someone
```

**重跑不會重抓** —— 增量是預設行為。已存在的貼文整則跳過，檔案還在且 hash
相符就不重新下載。

### 從 Misskey / Mastodon / pixiv 直接抓（不經 extension）

```bash
python -m snsmediadl.cli fetch <帳號> --platform misskey --host misskey.io
python -m snsmediadl.cli fetch <帳號> --platform mastodon --host baraag.net
python -m snsmediadl.cli fetch <數字id> --platform pixiv     # 需要 PHPSESSID，見 config
```

公開內容免認證。增量是預設行為：碰到已抓過的貼文就停。

### 批次抓 / 一鍵更新

```bash
# 一行一個網址的檔案（或 - 讀 stdin）。預設是預演，--yes 才真的抓
python -m snsmediadl.cli fetch-urls urls.txt
python -m snsmediadl.cli fetch-urls urls.txt --yes

# 把 DB 裡追蹤中的帳號各跑一次增量
python -m snsmediadl.cli refresh --yes
```

網址吃這些形式（認不得的一律報錯，**不猜**）：

```
https://misskey.io/@someone
https://baraag.net/@artist/media
https://www.pixiv.net/users/12345
@artist@baraag.net
misskey|https://misskey.design/@someone     # 表裡沒有的 instance 這樣指定
```

- **X 的網址會被拒絕**並說明要用 extension —— backend 抓不動它
- 一鍵更新用**平台 user id** 解析，所以帳號改過名也更新得到
- 抓不動 / 沒追蹤 / 缺憑證的帳號會**逐類列出來**，不會靜默跳過
- 佇列**一次跑一個帳號**：併發列舉同一個站台就是自己把自己打成 429

### 其他指令

```bash
python -m snsmediadl.cli download        # 把佇列裡待下載的抓完
python -m snsmediadl.cli status          # 佇列統計
python -m snsmediadl.cli serve           # 啟動 API（預設 127.0.0.1:8765）
python -m snsmediadl.cli delete-account <id>   # 刪帳號記錄（預設預演，--yes 才動手；不刪檔案）
```

API 文件在 `http://127.0.0.1:8765/docs`。

## 設定

優先序：環境變數 > `config.toml` > 內建預設。

```toml
# config.toml
output_root = "D:/media"
concurrency = 4
download_delay_seconds = 1.0
filename_format = "[%date%] %post_id%_%ordinal%.%ext%"
group_by_account = true
```

### ⚠ 下載限速

X 超速會**鎖整個帳號約一天**。預設值刻意保守，取自 WFDownloader 的設定：

| 設定 | 預設 | 意義 |
|------|------|------|
| `concurrency` | 4 | 同時下載數 |
| `download_delay_seconds` | 1.0 | 任兩次下載「開始」的最小間隔 |

只有併發限制是不夠的 —— 四個並行工作一完成就立刻抓下一批，實際速率只受頻寬限制。
兩個都要。

**收到 HTTP 429 時會立刻停止該輪下載**，該媒體標回 `pending`（不是 `failed` ——
檔案沒壞，只是現在不能抓），並在 GUI 的問題頁記一筆 ERROR。
**刻意不自動重試** —— 只有你知道帳號現在的狀態。

環境變數加前綴 `SNSMEDIADL_`，例如 `SNSMEDIADL_OUTPUT_ROOT`。

輸出路徑為 `<output_root>/<平台>/<帳號>/<檔名>`（`group_by_account = false` 則平鋪）。

### 檔名 token

`%post_id%`、`%date%`、`%ordinal%`、`%kind%`、`%filename%`、`%ext%`、
`%user_id%`、`%user_screen_name%`、`%platform%`

> 預設 format 帶 `%post_id%` 是刻意的：不同貼文可能用到同名檔案。

## 分類

兩個**正交**欄位，不是單一 tag：

| 欄位 | 值 |
|------|-----|
| `rating` | `sfw` / `r18` / `NULL`（未知）|
| `content_type` | `illust` / `irl` / `mod` / `ai` / `other` / `NULL` |

拆開才表達得出「AI 生的 R18」「R18 mod」，且「避開所有 R18」永遠是
`?exclude_rating=r18` 一個條件。

**`NULL` 代表未知，不是 sfw。** 沒有線索時不猜 —— 猜錯的方向不對稱。

`rating_source` 記錄是誰標的（`manual` / `account_default` / `auto`），
讓機器猜的值不會和人工確認的混在一起。

改帳號預設值**不會回溯**既有貼文；要回溯呼叫 `POST /api/accounts/{id}/retag`，
且預設不覆蓋人工標記。

## Creators

同一位創作者的多個帳號掛在同一個 `creator` 底下 —— 跨平台，也包含**同平台小帳**：

```
creator: 某畫師
  ├─ x     @artist       role=main
  ├─ x     @artist_r18   role=r18_alt
  ├─ pixiv 12345         role=main
  └─ misskey @artist     role=main
```

`GET /api/creators/{id}/media` 一次撈完跨平台跨帳號的全部媒體。

## 測試

```bash
python -m pytest
```

**測試完全不打網路** —— 下載層走 `httpx.MockTransport`，且 `conftest.py`
有 autouse fixture 攔截真實網路請求，忘記塞 mock 會直接失敗。

extension 那側是 Node 跑的（載入原始碼本體，不複製一份），清單見
`extension/README.md`：

```bash
node extension/test_extract.mjs
```

## 授權

Apache-2.0（見 `LICENSE`）。檔名 token 系統與模組切分概念改作自
[twitter_media_downloader](https://github.com/Spark-NF/twitter_media_downloader)
（Spark-NF，Apache-2.0），歸屬聲明見 `NOTICE`。

## 已知限制

- X 的 `platform_media_key` extension 未送出，媒體識別暫時走
  `(post_id, ordinal)`（misskey / mastodon / pixiv 有送）
- pixiv adapter 尚未對真站驗證（測試 fixture 是依 PixivBatchDownloader
  的分析手寫的）
- 不做影片 muxing / 轉檔 / EXIF 寫入；只有 m3u8（無 mp4 variant）的
  X 影片會略過並記 error log
- 沒有排程與自動輪詢
- 刪除帳號記錄後重新採集，既有檔案會被下載成 `xxx (1).jpg` 副本
  （刪除時會明確告知；掃描磁碟回填 DB 是未實作的獨立功能）
