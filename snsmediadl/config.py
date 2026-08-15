"""設定載入。優先序：環境變數 > config.toml > 內建預設。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # 3.10 走 tomli
    import tomli as tomllib  # type: ignore[no-redef]

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = PROJECT_ROOT / "config.toml"

ENV_PREFIX = "SNSMEDIADL_"


@dataclass
class Config:
    """執行期設定。"""

    # 本機單機前提：預設只綁 loopback。改成 0.0.0.0 等於把下載歷史對外開放，
    # 而且本專案刻意不做認證 —— 改之前請先看 README 的「使用前提」。
    host: str = "127.0.0.1"
    port: int = 8765

    db_path: Path = field(default_factory=lambda: PROJECT_ROOT / "snsmediadl.db")

    # 新下載的檔案落在哪。改這個**不會**搬移既有檔案 ——
    # `media.local_path` 存的是絕對路徑，舊檔留在原處、記錄依然正確。
    output_root: Path = field(default_factory=lambda: PROJECT_ROOT / "downloads")

    # 額外允許提供給前端的根目錄：以前用過的 output_root、匯入進來的舊媒體庫。
    #
    # 為什麼需要這個：`api/files.py` 只提供 output_root 底下的檔案（擋任意檔案
    # 讀取）。少了這份清單，改一次下載目錄就會讓**所有**既有檔案在 GUI 變成
    # 403 —— 檔案還在、DB 也對，只是看不到了。
    extra_media_roots: list[Path] = field(default_factory=list)

    # 縮圖快取放哪。預設 `<output_root>/thumb`（見 `thumb_dir`）。
    #
    # 為什麼需要快取：格線一頁 60 格，原檔平均 600 KB、最大 446 MB ——
    # 直接吐原檔是一頁上百 MB 的 I/O。縮成 320px WebP 之後一頁不到 1 MB。
    #
    # ⚠️ **按需生成，不做批次預生成。** 媒體檔散在三顆碟上共 224 萬個，
    # 掃一遍的成本遠超過收益，而且那是使用者的媒體庫，不該由程式自行遍歷。
    thumb_root: Path | None = None

    # 下載節流。數值取自使用者提供的 WFDownloader 設定 ——
    # X 超速會鎖整個帳號約一天，所以預設保守：同時 4 個、每次開始至少隔 1 秒。
    concurrency: int = 4
    download_delay_seconds: float = 1.0

    timeout_seconds: float = 60.0
    max_attempts: int = 3

    # serve 時是否自動把佇列裡的媒體抓下來。
    # 預設關閉 —— 會自己對外發請求的東西，預設應該是關的，尤其平台有速率限制。
    # 執行期可透過 PATCH /api/settings 或 GUI 的開關切換，不需重啟。
    auto_download: bool = False
    poll_interval_seconds: float = 5.0

    # ── Fediverse 抓取（Misskey / Mastodon）──────────────
    # 列舉的節流與下載分開：列舉打 API host，下載打媒體 CDN，限制不同。
    # Mastodon 官方預設是 300 req / 5 分鐘，1 秒一頁很安全。
    fetch_delay_seconds: float = 1.0
    fetch_page_size: int = 40
    # 帳號與帳號之間的間隔。`fetch_delay_seconds` 只管同一個帳號的翻頁，
    # 管不到這裡 —— 實測一批 misskey 帳號是 200 ms 一個，等於對同一台
    # 伺服器連續叩門。
    # ⚠️ 這**不是**修 HTTP 400 的（那是拿 `sn:` 哨符當 userId 查造成的，
    # 見 `services/identity.py`），純粹是禮貌。
    fetch_account_delay_seconds: float = 2.0
    # 上限保護：第一次對大帳號抓會抓到天亮。撞到上限**會明講**，不假裝抓完了。
    fetch_max_pages: int = 20

    # id 清單式平台（pixiv）一次 ingest 幾個作品。
    # 分批的理由是中斷保護：3000 個作品要跑 90 分鐘，
    # 中途中斷不該把前面 80 分鐘的成果丟掉。
    fetch_batch_size: int = 20

    # 每個 instance 的 access token。公開內容不需要，baraag.net 之類的
    # 站台部分內容需要。
    #
    # ⚠️ 這是憑證：只從 config.toml（已 gitignore）或環境變數進來，
    # 絕不寫進程式碼、wiki 或測試 fixture。
    instance_tokens: dict[str, str] = field(default_factory=dict)

    # 每個平台的登入憑證，鍵是平台名。目前只有 pixiv 要（`PHPSESSID`）。
    #
    # 與 instance_tokens 分開的理由：那個的鍵是 instance host（Fediverse 同一個
    # 平台跑在很多站上），這個的鍵是平台本身。混用會讓 pixiv 需要一個
    # 假的 host 當鍵。
    #
    # ⚠️ 同樣是憑證：`SNSMEDIADL_PLATFORM_CREDENTIALS=pixiv=<PHPSESSID>`
    # 或 config.toml 的 `platform_credentials = { pixiv = "..." }`。
    # 絕不進版控、不進 wiki、不進測試 fixture。
    platform_credentials: dict[str, str] = field(default_factory=dict)

    # 開發用：extension 偵測到檔案變動就自己 chrome.runtime.reload()。
    # 正式使用時關掉即可（它會一直輪詢版本端點）。
    dev_reload: bool = True

    filename_format: str = "[%date%] %post_id%_%ordinal%.%ext%"
    # 輸出目錄結構：<output_root>/<platform>/<screen_name>/<檔名>
    group_by_account: bool = True

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"

    @property
    def thumb_dir(self) -> Path:
        """縮圖快取目錄。預設 `<output_root>/thumb`。

        ⚠️ 縮圖是**可重建的衍生物**，整個目錄刪掉不會損失任何資料，
        下次瀏覽到會重新生成。備份時可以整個跳過。
        """
        return self.thumb_root or (self.output_root / "thumb")

    @property
    def media_roots(self) -> list[Path]:
        """允許提供給前端的所有根目錄。白名單的唯一來源。

        output_root 一定在裡面；重複的（例如舊根目錄沒清掉）只留一份。
        """
        roots = [self.output_root, *self.extra_media_roots]
        seen: set[str] = set()
        out: list[Path] = []
        for root in roots:
            key = os.path.normcase(str(root))
            if key not in seen:
                seen.add(key)
                out.append(root)
        return out


def ensure_output_root(root: Path) -> Path:
    """確認下載目錄存在且可寫，回傳解析後的絕對路徑。

    刻意**不** fallback 到預設目錄 —— 使用者以為在寫 D 槽、檔案卻默默堆進
    專案資料夾，是最糟的失敗方式：等發現時已經抓了幾十 GB 到錯的地方。
    """
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"下載目錄無法建立：{root} —— {exc}") from exc

    # mkdir 成功不等於寫得進去（唯讀掛載、ACL、磁碟滿）。實際寫一次才算數。
    probe = root / ".snsmediadl-write-test"
    try:
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        raise RuntimeError(f"下載目錄不可寫：{root} —— {exc}") from exc

    return root.resolve()


# 型別是 `Path | None` 的設定項。
#
# ⚠️ 載入器靠 `isinstance(current, Path)` 判斷要不要轉成 Path，而這些欄位的
# 預設值是 `None` —— 判斷會失敗，值會被原封不動存成 str，然後在第一次拿去
# 拼路徑時才炸（或更糟：`str + str` 拼出一個看起來對的錯路徑）。
# 新增 `X | None` 的路徑設定時**必須**加進這裡。
_OPTIONAL_PATH_FIELDS = frozenset({"thumb_root"})


def _coerce(value: str, sample: object) -> object:
    if isinstance(sample, bool):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(sample, int):
        return int(value)
    if isinstance(sample, float):
        return float(value)
    if isinstance(sample, Path):
        return Path(value)
    if isinstance(sample, list):
        # 用 os.pathsep 分隔（Windows 是 ';'）。不能用 ':'，Windows 路徑
        # 本身就含 ':'（`D:\...`），會被切成兩半。
        return _parse_path_list(value.split(os.pathsep))
    return value


def _parse_path_list(values: object) -> list[Path]:
    """把 toml 的字串陣列或環境變數切出來的片段轉成 Path 清單。"""
    if isinstance(values, (str, Path)):
        raise ValueError(f"設定值應為路徑清單，收到單一值 {values!r}")
    out: list[Path] = []
    for item in values:
        text = str(item).strip()
        if text:  # 允許尾隨分隔符與 toml 陣列裡的空字串
            out.append(Path(text))
    return out


def load_config(config_file: Path | None = None) -> Config:
    """載入設定。config.toml 缺席是正常情況，不是錯誤。"""
    cfg = Config()

    path = config_file if config_file is not None else CONFIG_FILE
    if path.exists():
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        for key, value in data.items():
            if not hasattr(cfg, key):
                raise ValueError(f"config.toml 有未知設定項: {key!r}")
            current = getattr(cfg, key)
            if isinstance(current, Path) or key in _OPTIONAL_PATH_FIELDS:
                value = Path(value)
            elif isinstance(current, list):
                value = _parse_path_list(value)
            setattr(cfg, key, value)

    for key in vars(cfg):
        env_key = f"{ENV_PREFIX}{key.upper()}"
        if env_key not in os.environ:
            continue
        current = getattr(cfg, key)
        if key in _OPTIONAL_PATH_FIELDS:
            setattr(cfg, key, Path(os.environ[env_key]))
        elif isinstance(current, dict):
            # dict 型的設定（instance_tokens）用 `host=token,host2=token2`
            setattr(cfg, key, _parse_pairs(os.environ[env_key]))
        else:
            setattr(cfg, key, _coerce(os.environ[env_key], current))

    return cfg


def _parse_pairs(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for chunk in raw.split(","):
        if not chunk.strip():
            continue
        if "=" not in chunk:
            raise ValueError(f"設定值 {chunk!r} 缺少 '='，格式應為 host=token")
        host, _, token = chunk.partition("=")
        out[host.strip()] = token.strip()
    return out
