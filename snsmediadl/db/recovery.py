"""crash 之後的收拾工作。

## 為什麼需要這個

正式庫裡量到 **4 筆 `media.status = 'downloading'`**。那是 backend 在下載途中
被中斷（關掉、當掉、斷電）留下的：狀態改成 downloading 了，但沒有任何人會
再回頭處理它們。

- 下載 worker 只撿 `status = 'pending'` → 這 4 筆永遠不會被下載
- GUI 的佇列列只顯示 pending / downloading / failed → 它們會一直掛著 4
- 沒有任何錯誤訊息

也就是說：**媒體少了 4 個，而系統從頭到尾沒說過一句話。** 那正是本專案
最忌諱的靜默失敗。

## 為什麼不做 `synchronous=NORMAL`

原本規劃要用 `synchronous=NORMAL` 換效能，並配一套 crash 保險。實測後否決 ——
NORMAL 省下的是每次點擊 1.5 ms，量不出來，卻要拿「可能丟掉最後幾筆交易」去換 ——
而那幾筆恰好是使用者剛按下的評分與分級，全庫唯一不可重建的資料。
同一個決定寫在 `db/session.py` 設定 PRAGMA 的地方。

所以這個模組處理的不是「我們新引入的風險」，而是**已經在發生的**那個。
"""

from __future__ import annotations

import logging

from sqlalchemy import Engine, text, update
from sqlalchemy.orm import Session, sessionmaker

from .enums import MediaStatus
from .models import Media

log = logging.getLogger("snsmediadl")


def reclaim_orphan_downloads(session: Session) -> int:
    """把卡在 `downloading` 的媒體打回 `pending`。回收幾筆就回幾。

    只在啟動時呼叫。**執行中不可以呼叫** —— 那會把正在下載的那幾筆搶走，
    造成同一個檔案被兩條路徑同時寫。

    ⚠️ **回收動作一定要記 log，不可以靜默修正。**
    正常情況下這個數字是 0（乾淨關閉）或個位數（關掉時剛好在下載）。
    如果每次啟動都回收上百筆，那代表 worker 有別的問題 —— 而唯一會透露
    這件事的就是這行 log。默默修好等於把線索也一起清掉。
    """
    result = session.execute(
        update(Media)
        .where(Media.status == MediaStatus.DOWNLOADING.value)
        .values(status=MediaStatus.PENDING.value, error=None)
        .execution_options(synchronize_session=False)
    )
    n = result.rowcount or 0
    if n:
        session.commit()
        # bulk UPDATE 走 `synchronize_session=False`，所以 session 裡已載入的
        # Media 物件仍帶著舊的 status。不 expire 的話，同一個 session 之後
        # 讀到的是「回收前」的值 —— 而回收的重點正是別人要看到新值。
        session.expire_all()
        log.warning(
            "啟動回收：%s 筆媒體卡在 downloading，已打回 pending。"
            "那是上次結束時正在下載的 —— 若這個數字持續偏大，要查 worker。", n,
        )
    else:
        session.rollback()
    return n


def checkpoint_wal(engine: Engine) -> None:
    """把 WAL 併回主檔並截斷。正常關閉時呼叫。

    WAL 在長時間執行下會一路長大，而**它變大不會有任何徵兆** —— 直到磁碟滿了。
    正常關閉時併一次，成本很低。

    用 TRUNCATE 而不是 PASSIVE：PASSIVE 遇到還有讀者時會直接放棄，
    等於這個函式白呼叫。關閉當下沒有其他讀者，該用最徹底的那個。
    """
    try:
        with engine.connect() as conn:
            row = conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        # 回傳是 (busy, log_pages, checkpointed_pages)。busy=1 代表沒做完。
        if row and row[0]:
            log.warning("WAL checkpoint 沒做完（還有其他連線在讀）：%s", tuple(row))
    except Exception:  # noqa: BLE001
        # 關閉流程不該因為收尾失敗而炸掉，但也不能默默吞掉
        log.exception("WAL checkpoint 失敗")


def on_startup(engine: Engine, maker: sessionmaker[Session]) -> dict:
    """啟動時的收拾。回摘要，方便測試與日誌。

    ⚠️ **這裡只做便宜的事。** 實測（正式庫 706 MB）：

    | 動作 | 耗時 |
    |------|-----:|
    | 孤兒回收（`status='downloading'` 的 index seek）| **0 ms** |
    | `PRAGMA quick_check(1)` | **16.2 s** |

    quick_check 一度被放在這裡，結果是 backend 每次啟動都要等 16 秒才回應 ——
    而它幾乎永遠回 "ok"。完整性檢查是**診斷工具**，不是啟動步驟：
    需要時打 `snsmediadl check-db`。

    這條的一般教訓：「安全檢查」放進必經路徑之前要先量它多久。
    一個沒人等得起的檢查，最後會被關掉，那比沒有還糟。
    """
    with maker() as session:
        orphans = reclaim_orphan_downloads(session)
    return {"reclaimed_downloads": orphans}


def quick_check(engine: Engine) -> str:
    """`PRAGMA quick_check` —— 比 integrity_check 快得多，抓得到結構損壞。

    回 "ok" 或錯誤描述。**損壞時不自動做任何修復**：那需要人來決定要
    從哪個備份還原，程式擅自動手只會把情況弄得更難救。
    """
    try:
        with engine.connect() as conn:
            row = conn.exec_driver_sql("PRAGMA quick_check(1)").fetchone()
        result = row[0] if row else "unknown"
    except Exception as exc:  # noqa: BLE001
        log.exception("quick_check 執行失敗")
        return f"check failed: {exc}"

    if result != "ok":
        log.error(
            "⚠️ 資料庫完整性檢查沒過：%s\n"
            "**不要繼續寫入。** 請從備份還原（`snsmediadl backup` 產生的 .bak-*）。",
            result,
        )
    return result


def backup_path(db_path, tag: str = "manual"):
    """備份檔名。帶時間戳與用途標籤，才看得出是哪一次、為什麼備的。"""
    from datetime import datetime
    from pathlib import Path

    db_path = Path(db_path)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return db_path.with_name(f"{db_path.name}.bak-{tag}-{stamp}")


def backup(engine: Engine, db_path, tag: str = "manual"):
    """用 SQLite 自己的線上備份 API 複製整個資料庫。回備份檔路徑。

    ⚠️ **不用 `shutil.copy`。** WAL 模式下主檔可能落後於 WAL，直接複製檔案
    會得到一個少了最近交易的備份 —— 而且看起來完全正常，直到你需要它。
    `sqlite3.Connection.backup()` 走的是引擎內部，拿到的是一致的快照。
    """
    import sqlite3
    from pathlib import Path

    target = Path(backup_path(db_path, tag))
    raw = engine.raw_connection()
    try:
        src = raw.driver_connection
        if not isinstance(src, sqlite3.Connection):  # pragma: no cover - 非 SQLite
            raise RuntimeError("backup 只支援 SQLite")
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        raw.close()
    log.info("已備份資料庫 → %s", target)
    return target
