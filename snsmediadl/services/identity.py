"""帳號身分：哨符 id 與治好它的路徑。

## `sn:` 哨符是什麼

匯入既有媒體庫時，多數檔案只帶得出**帳號名**，帶不出平台的數字 id
（離線盤點工具只能從資料夾名推帳號）。那時寫進
`platform_user_id` 的是 `sn:<screen_name>`，意思是「這個位置還沒有真正的身分」。

哨符本身是對的 —— 真的沒有 id 就不該編一個。問題出在**別的地方把它當成真 id 用**：

    plan_refresh → fetch_account(user_id="sn:had")
                 → POST users/show {"userId": "sn:had"}   ← misskey 回 400

正式庫實測（2026-08-15）：9 個 misskey 帳號裡，唯一有真實 id 的那個抓成功，
8 個哨符全部 400。當時被誤判成「掃太快要加限速」—— 限速的特徵是「前面成功、
後面開始失敗」，而這裡是依帳號決定的，跟順序無關。

## 治好它

哨符帳號改用名字解析（adapter 本來就有 `resolve_account`），拿到真 id 之後
**就地寫回同一列**。不可以讓 `upsert_account(真id)` 去新建第二列 ——
那會把同一個人的記錄劈成兩半，匯入來的貼文全留在哨符那一列，
而使用者完全看不出發生了這件事。

## `identity_heals.kind` 的三種值

前兩種由這個模組寫，第三種**不是**：

| kind | 誰寫 | 意思 | moved_posts |
|------|------|------|-------------|
| `rename` | `heal_placeholder_account()` | 哨符列就地換成真 id，沒有第二列 | 0 |
| `merge` | `heal_placeholder_account()` | 哨符列與真 id 列並存，貼文搬過去後刪掉哨符列 | N |
| `rename_merge` | 離線的媒體庫盤點工具 | **兩個哨符列之間**的合併 | 0 |

`rename_merge` 是這個模組**做不到**的那一種，原因值得記著：這裡的配對一律
以 `screen_name` 為鍵（`_find_ghost`），而使用者在平台上改過 handle 之後，
舊名字下的 `sn:舊名` 與新名字下的 `sn:新名` 是兩個不同的鍵，永遠不會相遇；
去平台問也沒用 —— 舊 handle 已經不存在，只會拿到 404。

它的證據來自**離線的掃描檔**（兩個資料夾的推文集合是子集、首則推文相同、
多出來的全在後面），不是平台回應，所以走腳本、由人點頭，不走自動治療。
`moved_posts` 是 0 且那個 0 有意義：貼文本來就在本尊底下，一則都不用搬 ——
空殼之所以空，正是因為 `posts` 的唯一鍵不含 account_id。
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..db.models import Account, IdentityHeal, Post
from . import counters

log = logging.getLogger("snsmediadl")

# 「這個位置還沒有真正的平台 id」。改這個前綴要同時改匯入器。
PLACEHOLDER_PREFIX = "sn:"


def is_placeholder(platform_user_id: str | None) -> bool:
    """這個 id 是哨符（不是平台給的）嗎？

    集中成一個函式而不是在各處寫 `startswith("sn:")` —— 這是一個**跨模組的
    資料約定**，散在三個地方的話，改前綴時漏掉一處就是靜默失效。
    """
    return bool(platform_user_id) and platform_user_id.startswith(PLACEHOLDER_PREFIX)


def placeholder_id(screen_name: str) -> str:
    return f"{PLACEHOLDER_PREFIX}{screen_name}"


def heal_placeholder_account(
    session: Session,
    platform: str,
    instance_host: str,
    screen_name: str,
    real_id: str,
) -> Account | None:
    """把 `sn:<screen_name>` 那一列換成真正的 `platform_user_id`。

    回傳被治好（或合併後留下）的那一列；沒有哨符列就回 `None`。

    三種情況：

    1. **沒有哨符列** —— 什麼都不做。這是最常見的（帳號本來就有真 id）。
    2. **只有哨符列** —— 直接改它的 `platform_user_id`。列的 id 不變，
       貼文、媒體、我的最愛、評分、預設值全部原地保留。
    3. **哨符列與真 id 列都在** —— 把貼文搬到真 id 那一列，刪掉哨符列。
       這不是假想情況：正式庫的 pixiv `東西` 就是 `sn:東西`（23 個媒體）
       ＋ `16347608`（5 個媒體）兩列並存。

    ⚠️ **每一種情況都要 log。** 這是會改動帳號身分的操作，靜默做掉的話，
    使用者下次發現帳號少了一個會完全無從查起。

    ⚠️ 不 commit —— 呼叫端多半已經在一個交易裡（抓取正要開始寫貼文）。
    """
    if is_placeholder(real_id):
        # 平台不可能回一個哨符。走到這裡代表呼叫端把哨符又傳回來了。
        raise ValueError(f"real_id 不可以是哨符：{real_id!r}")

    def _find(user_id: str) -> Account | None:
        return session.scalar(
            select(Account).where(
                Account.platform == platform,
                Account.instance_host == instance_host,
                Account.platform_user_id == user_id,
            )
        )

    def _find_ghost(name: str) -> Account | None:
        """找哨符列，**大小寫不敏感**。

        ⚠️ 這一條是實測逼出來的。正式庫裡有 `sn:Lbf5n` 與真 id 列的
        `screen_name = lbf5n` 並存 —— 匯入時的名字來自**檔名**（保留原始大小寫），
        而平台回的是它自己的正規化寫法。用精確比對的話這種帳號永遠治不好，
        而且症狀是「合併沒發生」這種不會報錯的靜默失敗。

        用 `lower()` 相等而不是 `ilike`：screen_name 常含底線（`_KIKURAKU_`），
        而 `_` 在 LIKE 裡是萬用字元 —— 不跳脫的話會比對到別人。
        """
        target = placeholder_id(name).lower()
        return session.scalar(
            select(Account).where(
                Account.platform == platform,
                Account.instance_host == instance_host,
                func.lower(Account.platform_user_id) == target,
            )
        )

    ghost = _find_ghost(screen_name)
    if ghost is None:
        return None

    real = _find(real_id)

    if real is None:
        # ⚠️ warning 不是 info：這是**改動帳號身分**的操作。
        # 每次啟動都看到一長串代表匯入的資料正在被大量重新歸戶，那要看得見。
        log.warning(
            "帳號身分補上了：%s@%s 的 %s → %s（同一列，貼文與媒體不動）",
            screen_name, instance_host or platform, ghost.platform_user_id, real_id,
        )
        _record(session, platform, instance_host, screen_name,
                ghost.platform_user_id, real_id, "rename", 0)
        ghost.platform_user_id = real_id
        session.flush()
        # ⚠️ 聚合欄可能是舊的（`counters.recompute` 是 bulk UPDATE，不經過
        # identity map）。呼叫端會拿這幾個數字**講給使用者聽**
        # （「併入 N 則舊記錄」），報一個過期的數字比不報更糟。
        session.refresh(ghost)
        return ghost

    if real.id == ghost.id:        # 理論上碰不到，防呆
        return ghost

    # 合併。貼文的唯一鍵是 (platform, instance_host, platform_post_id)，
    # 不含 account_id —— 所以同一則貼文不可能同時掛在兩列底下，搬過去不會撞鍵。
    moved = session.execute(
        update(Post).where(Post.account_id == ghost.id).values(account_id=real.id)
    ).rowcount
    # 使用者手動標過的偏好不該因為合併而消失：真 id 那列沒設過的才從哨符列繼承。
    for field in ("is_favorite", "stars", "default_rating", "default_content_type",
                  "creator_id", "role"):
        if not getattr(real, field) and getattr(ghost, field):
            setattr(real, field, getattr(ghost, field))
    _record(session, platform, instance_host, screen_name,
            ghost.platform_user_id, real_id, "merge", moved)
    session.delete(ghost)
    session.flush()
    counters.recompute(session, [real.id])
    # ⚠️ `recompute` 與搬貼文都是 bulk UPDATE —— 它們不經過 identity map，
    # 手上這個物件的聚合欄還是舊值（而且很可能是 0）。回傳一個「數字是錯的」
    # 的物件比回傳 None 更糟：呼叫端不會知道要自己 refresh。
    session.refresh(real)
    log.warning(
        "帳號合併：%s@%s 的哨符列（id=%s）併進真 id %s（id=%s），搬了 %s 則貼文",
        screen_name, instance_host or platform, ghost.id, real_id, real.id, moved,
    )
    return real


def _record(session: Session, platform: str, instance_host: str, screen_name: str,
            placeholder_id_: str, real_id: str, kind: str, moved: int) -> None:
    """把治療記下來。**log 會捲掉，這張表不會。**"""
    session.add(IdentityHeal(
        platform=platform, instance_host=instance_host, screen_name=screen_name,
        placeholder_id=placeholder_id_, real_id=real_id, kind=kind, moved_posts=moved,
    ))
