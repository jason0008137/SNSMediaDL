"""Alembic 環境。DB URL 一律從 snsmediadl.config 取，不寫在 alembic.ini。"""

from __future__ import annotations

import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import event

from snsmediadl.config import load_config
from snsmediadl.db.models import Base
from snsmediadl.db.session import make_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=load_config().db_url,
        target_metadata=target_metadata,
        literal_binds=True,
        # SQLite 不支援大部分的 ALTER，必須用 batch 模式重建表
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _foreign_keys_off(dbapi_conn, _record) -> None:
    """migration 期間關掉 FK 強制。**這不是繞過檢查，是必要條件。**

    batch 模式重建表的流程是「建暫存表 → 搬資料 → DROP 舊表 → rename」。
    SQLite 在 `PRAGMA foreign_keys=ON` 時，`DROP TABLE` 會先做一次隱含的
    `DELETE FROM`，而那個 delete **會觸發 ON DELETE CASCADE**。

    後果實測過：`a1b2c3d4e5f6` 重建 `accounts` 時，把 962 則 posts 與
    1188 筆 media 整個 cascade 刪光，migration 本身回報成功、沒有任何錯誤訊息。
    只有 accounts 活下來（它是父表）。空的 DB 測不出來 —— 沒有列可以 cascade。

    SQLite 官方的 12-step ALTER TABLE 程序第一步就是關掉它，最後一步是
    `PRAGMA foreign_key_check`（見下方 `_assert_fk_intact`）。

    pragma 必須下在 connect 事件裡：它在交易中是 no-op，而 `engine.connect()`
    之後 SQLAlchemy 隨時可能已經開了交易。這個 listener 掛在
    `_configure_sqlite`（把 FK 打開的那個）後面，所以蓋得過它。
    """
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=OFF")
    cur.close()


def _assert_fk_intact(connection) -> None:
    """migration 跑完檢查 FK 完整性，有孤兒就大聲炸掉。

    關掉 FK 是為了讓重建表不觸發 cascade，不是為了容忍壞資料。
    不檢查就等於把「靜默刪光」換成「靜默留下孤兒」。
    """
    rows = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    if rows:
        raise RuntimeError(
            f"migration 後有 {len(rows)} 筆 FK 違規（前 5 筆：{rows[:5]}）—— "
            "資料已經不一致，請從備份還原後回報這個 migration"
        )


def _backup_before_upgrade(cfg) -> None:
    """upgrade 之前自動備份。**不依賴人記得。**

    這不是多餘的謹慎：本 repo 的 `_foreign_keys_off` docstring 記著一次真實
    事故 —— `a1b2c3d4e5f6` 把 962 則 posts 與 1188 筆 media 整個 cascade 刪光，
    而 migration 回報成功、沒有任何錯誤訊息。那次靠的是手動備份救回來的。

    只在 upgrade 時備份。downgrade 與 `current` / `history` 這類唯讀指令不備 ——
    每次查個版本號都複製 700 MB 是另一種災難。

    設 `SNSMEDIADL_SKIP_MIGRATION_BACKUP=1` 可跳過（測試用；正式流程別關）。
    """
    import os

    if os.environ.get("SNSMEDIADL_SKIP_MIGRATION_BACKUP") == "1":
        return
    if not cfg.db_path.exists():
        return                        # 全新的庫，沒東西可備
    # alembic 沒有把「這次是什麼指令」放進 context，只能看 argv。
    # 只有 upgrade 才備份 —— downgrade 之前使用者本來就該自己備，而
    # `current` / `history` 這類唯讀指令每次都複製 700 MB 是另一種災難。
    if "upgrade" not in sys.argv:
        return

    from snsmediadl.db import recovery

    probe = make_engine(cfg)
    try:
        target = recovery.backup(probe, cfg.db_path, tag="premigrate")
        print(f"[alembic] 已自動備份 → {target}")
    finally:
        probe.dispose()


def run_migrations_online() -> None:
    cfg = load_config()
    _backup_before_upgrade(cfg)
    engine = make_engine(cfg)
    event.listen(engine, "connect", _foreign_keys_off)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        _assert_fk_intact(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
