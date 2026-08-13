# SNSMediaDL 一鍵更新。由 update.bat 呼叫。
#
# 設計前提：**絕不動使用者的資料**。DB、config.toml、downloads/ 全都在 .gitignore 裡，
# git 不會碰它們；schema 有變時會先備份 DB 再 migrate。
#
# ⚠️ 這個檔案必須存成 UTF-8 with BOM。Windows PowerShell 5.1 對沒有 BOM 的檔案
#    用系統 ANSI codepage 解讀，底下的中文會變成亂碼。
param(
    [string] $RepoUrl = 'https://github.com/jason0008137/SNSMediaDL.git',
    [string] $Branch  = 'main'
)

$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# $hints 標成 [string[]]：只有一行時 git 的輸出是單一字串而不是陣列，
# 不標型別的話 `字串 + @(...)` 會變成字串串接，整段訊息塌成一行。
function Fail([string] $msg, [string[]] $hints) {
    Write-Host ""
    Write-Host "[X] $msg" -ForegroundColor Red
    foreach ($h in $hints) { Write-Host "    $h" -ForegroundColor Red }
    Write-Host ""
    Read-Host "按 Enter 關閉"
    exit 1
}

function Done($msg) {
    Write-Host ""
    Write-Host $msg -ForegroundColor Green
    Write-Host ""
    Read-Host "按 Enter 關閉"
    exit 0
}

Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  SNSMediaDL 更新" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

# ---------------------------------------------------------------- git

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Fail "找不到 git。" @(
        "更新需要 git。到 https://git-scm.com/download/win 安裝後再跑一次。",
        "安裝時全部用預設值即可。"
    )
}

# ---------------------------------------------------------------- 這是 git clone 來的嗎

git rev-parse --is-inside-work-tree 2>$null | Out-Null
$isRepo = ($LASTEXITCODE -eq 0)

if (-not $isRepo) {
    # 從 GitHub 下載 ZIP 解壓縮的版本沒有 .git，沒有版本資訊可以比對。
    # 就地接上遠端可以解決，而且不會碰到資料 —— DB / config.toml / downloads/
    # 都是未追蹤檔，git checkout 不會刪未追蹤檔。
    Write-Host "這個資料夾不是用 git 取得的（多半是下載 ZIP 解壓縮的）。" -ForegroundColor Yellow
    Write-Host "沒有版本記錄就無法判斷該更新什麼。" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "可以就地接上更新來源：" -ForegroundColor Yellow
    Write-Host "  · 你的資料不會動 —— DB、config.toml、downloads/ 都不在版控裡"
    Write-Host "  · 但**你自己改過的程式碼會被官方版本覆蓋**"
    Write-Host ""
    $ans = Read-Host "要接上嗎？(y/N)"
    if ($ans -ne 'y' -and $ans -ne 'Y') {
        Done "沒有做任何事。"
    }

    git init -q
    if ($LASTEXITCODE -ne 0) { Fail "git init 失敗。" @() }
    git remote add origin $RepoUrl
    Write-Host ""
    Write-Host "下載中..." -ForegroundColor DarkGray
    git fetch --depth 1 origin $Branch
    if ($LASTEXITCODE -ne 0) { Fail "連不到 $RepoUrl。" @("檢查網路，或稍後再試。") }
    git checkout -f -B $Branch "origin/$Branch"
    if ($LASTEXITCODE -ne 0) { Fail "切換到最新版失敗。" @() }
    git branch --set-upstream-to="origin/$Branch" $Branch 2>$null | Out-Null
    Write-Host "已接上更新來源。" -ForegroundColor Green
    $oldHead = ""
} else {
    $current = (git rev-parse --abbrev-ref HEAD).Trim()
    if ($current -ne 'HEAD') { $Branch = $current }

    $remote = git remote 2>$null
    if (-not $remote) { Fail "這個 repo 沒有設定更新來源。" @("git remote add origin $RepoUrl") }

    # 使用者自己改過的東西不能被無聲蓋掉。這裡不代做 stash ——
    # 自動搬走改動會讓人搞不清楚東西跑哪去了。
    #
    # ⚠️ --untracked-files=no：只看**被追蹤檔案**的修改。未追蹤檔（使用者自己丟進
    #    資料夾的筆記、匯出的東西）不在 merge --ff-only 的影響範圍內，
    #    拿它們擋更新只是找麻煩，而且 `git checkout -- .` 也清不掉它們。
    $dirty = git status --porcelain --untracked-files=no
    if ($dirty) {
        Fail "有還沒存檔的改動，先處理掉才能更新：" (
            @($dirty | ForEach-Object { "  $_" }) + @(
                "",
                "不想要這些改動就執行：git checkout -- .",
                "（這只會影響程式碼檔案，不會動到你的 DB、設定與下載的檔案）"
            )
        )
    }

    Write-Host "檢查更新..." -ForegroundColor DarkGray
    git fetch --quiet origin $Branch
    if ($LASTEXITCODE -ne 0) { Fail "連不到更新來源。" @("檢查網路，或稍後再試。") }

    $oldHead = (git rev-parse HEAD).Trim()
    $newHead = (git rev-parse "origin/$Branch").Trim()

    if ($oldHead -eq $newHead) {
        Done "已經是最新版本，沒有東西要更新。"
    }

    Write-Host ""
    Write-Host "有新版本：" -ForegroundColor Green
    git --no-pager log --oneline --no-decorate "HEAD..origin/$Branch" | ForEach-Object { Write-Host "  $_" }
    Write-Host ""

    # --ff-only：只快轉。萬一本機有自己的 commit，這裡會失敗而不是產生
    # 一個看不懂的 merge —— 失敗比亂合併好。
    git merge --ff-only "origin/$Branch"
    if ($LASTEXITCODE -ne 0) {
        Fail "無法直接更新（本機有自己的修改記錄）。" @(
            "要放棄本機修改、完全採用官方版本："
            "  git reset --hard origin/$Branch"
            "（一樣不會動到你的 DB、設定與下載的檔案）"
        )
    }
    Write-Host "程式碼已更新。" -ForegroundColor Green
}

# ---------------------------------------------------------------- 有變的東西

function Changed($pathspec) {
    if (-not $oldHead) { return $true }   # 剛接上遠端，一切視為有變
    $out = git --no-pager diff --name-only $oldHead HEAD -- $pathspec
    return [bool]$out
}

$depsChanged      = Changed 'pyproject.toml'
$migrationsAdded  = Changed 'alembic'
$extensionChanged = Changed 'extension'

# ---------------------------------------------------------------- 相依套件

Write-Host ""
if ($depsChanged) {
    Write-Host "[1/3] 相依套件有更新，安裝中..."
    python -m pip install -e ".[dev]"
    if ($LASTEXITCODE -ne 0) { Fail "套件安裝失敗。" @("先執行 start.bat 看完整錯誤訊息。") }
} else {
    python -c "import fastapi, sqlalchemy, alembic, httpx" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[1/3] 套件還沒裝，安裝中..."
        python -m pip install -e ".[dev]"
        if ($LASTEXITCODE -ne 0) { Fail "套件安裝失敗。" @("先執行 start.bat 看完整錯誤訊息。") }
    } else {
        Write-Host "[1/3] 相依套件無變動"
    }
}

# ---------------------------------------------------------------- 資料庫

if ($migrationsAdded) {
    Write-Host "[2/3] 資料庫結構有更新"

    # 備份放在 DB 真正的所在位置（config.toml 可以改 db_path）。
    $dbPath = (python -c "from snsmediadl.config import load_config; print(load_config().db_path)" 2>$null)
    if ($LASTEXITCODE -ne 0 -or -not $dbPath) {
        Fail "讀不到資料庫位置，不敢動 schema。" @("先執行 start.bat，看它報什麼錯。")
    }
    $dbPath = $dbPath.Trim()

    if (Test-Path $dbPath) {
        $bak = "$dbPath.bak-" + (Get-Date -Format 'yyyyMMdd-HHmmss')
        Copy-Item $dbPath $bak -Force
        Write-Host "      已備份：$(Split-Path -Leaf $bak)" -ForegroundColor DarkGray
    }

    python -m alembic upgrade head
    if ($LASTEXITCODE -ne 0) {
        Fail "資料庫更新失敗。" @(
            "你的資料還在，備份也還在（副檔名 .bak-*）。",
            "把上面的錯誤訊息回報給開發者。"
        )
    }
    Write-Host "      資料庫已更新"
} else {
    Write-Host "[2/3] 資料庫結構無變動"
}

# ---------------------------------------------------------------- 完成

Write-Host "[3/3] 完成"
Write-Host ""
Write-Host "  版本  $(git --no-pager log -1 --format='%h  %s')" -ForegroundColor DarkGray

if ($extensionChanged) {
    Write-Host ""
    Write-Host "  ⚠ Chrome 擴充功能也有更新。" -ForegroundColor Yellow
    Write-Host "    backend 跑起來之後它會自己重載；沒反應的話到" -ForegroundColor Yellow
    Write-Host "    chrome://extensions 按這個擴充功能的重新整理鈕。" -ForegroundColor Yellow
}

Done "更新完成，執行 start.bat 就可以用了。"
