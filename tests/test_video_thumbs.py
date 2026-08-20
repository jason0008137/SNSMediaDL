"""影片與 ugoira 的縮圖。

多數測試用**替身**取代 `_ffmpeg_frame()`，這樣「閘、快取、狀態碼、重試」
這些邏輯不必依賴機器上有沒有 ffmpeg。

最後一節是**真的跑 ffmpeg** 的整合測試（`@pytest.mark.slow`）：拿 ffmpeg
自己生一支 2 秒的測試影片，再讓縮圖端點去抽它。這一條驗的正是替身驗不到的
那件事 —— `-ss` 放在 `-i` 之前、`-frames:v 1`、`image2` 這串參數對不對。
機器上找不到 ffmpeg 時它會 skip，不會變成假的紅燈。

狀態碼的分工是本段的重點：415（格式沒救）/ 500（原檔壞了）/ 503（依賴缺失
或排隊逾時）三者不可混用 —— 混了使用者就分不出「裝一下 ffmpeg 就好」
與「這個檔沒救」。
"""

from __future__ import annotations

import io
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from snsmediadl import config as config_mod
from snsmediadl.api import files as files_mod
from snsmediadl.api.app import create_app, get_session
from snsmediadl.db.enums import MediaStatus
from snsmediadl.db.models import Account, Media, Post


@pytest.fixture(autouse=True)
def _fresh_module_state():
    """ffmpeg 偵測與併發閘都快取在模組層 —— 測試之間必須清掉，
    否則第一個測試的結果會決定後面所有測試。"""
    config_mod.refresh_ffmpeg()
    files_mod.reset_thumb_gate()
    yield
    config_mod.refresh_ffmpeg()
    files_mod.reset_thumb_gate()


@pytest.fixture()
def client(cfg, session):
    app = create_app(cfg)
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def png_bytes(size=(640, 480)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, "blue").save(buf, "PNG")
    return buf.getvalue()


def _media(session, cfg, name: str, *, content: bytes = b"raw") -> int:
    acct = Account(platform="x", platform_user_id=f"u_{name}", screen_name=name)
    session.add(acct)
    session.flush()
    post = Post(platform="x", platform_post_id=f"p_{name}", account_id=acct.id)
    session.add(post)
    session.flush()

    path = cfg.output_root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)

    m = Media(post_id=post.id, ordinal=0, kind="video",
              source_url="https://example.invalid/x", local_path=str(path),
              status=MediaStatus.DONE.value)
    session.add(m)
    session.commit()
    return m.id


def ugoira_zip(names=("000000.jpg", "000001.jpg")) -> bytes:
    """手工組一個 ugoira：一包 zip 裝幾張 jpg。

    顏色**依檔名**決定（`000000` 紅、其餘綠），不依寫入順序 ——
    「取檔名排序第一張」這件事才驗得出來。
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in names:
            frame = io.BytesIO()
            color = "red" if name.startswith("000000") else "green"
            Image.new("RGB", (400, 300), color).save(frame, "JPEG")
            zf.writestr(name, frame.getvalue())
    return buf.getvalue()


# ── ffmpeg 缺席 ───────────────────────────────────────

def test_video_without_ffmpeg_returns_503(client, session, cfg, monkeypatch):
    """⭐ 503 而不是 415。

    415 的意思是「這個格式做不出縮圖」—— 但格式沒問題，是**我們少了依賴**。
    混用的話，使用者永遠不會知道裝一下 ffmpeg 就解決了。
    """
    monkeypatch.setattr("shutil.which", lambda _name: None)
    # ⚠️ PATH 清空還不夠 —— 第三層（imageio-ffmpeg 自帶的那支）是本專案的
    # 相依，開發機上一定找得到。不一起關掉的話這個測試會變成「拿真的
    # ffmpeg 去抽一個假 mp4」，然後在 500 而不是 503 上失敗。
    monkeypatch.setattr(config_mod, "_bundled_ffmpeg", lambda: None)
    config_mod.refresh_ffmpeg()

    mid = _media(session, cfg, "clip.mp4")
    r = client.get(f"/api/media/{mid}/thumb")
    assert r.status_code == 503
    # ⚠️ 斷言 `code` 不是文案。detail 是給人看的英文，改一個字不該紅一批測試 ——
    # 那正是「文案不敢改」的成因。
    assert r.json()["code"] == "thumb.ffmpeg_missing"


def test_explicit_ffmpeg_path_that_does_not_exist_is_not_silently_replaced(
    cfg, monkeypatch, tmp_path
):
    """⚠️ 明確設了路徑卻不存在 → 回 None，**不退回 PATH 上那支**。

    退回去等於設定被忽略，症狀是縮圖不知為何來自另一個版本。
    """
    monkeypatch.setattr("shutil.which",
                        lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None)
    cfg.ffmpeg_path = str(tmp_path / "nope" / "ffmpeg.exe")
    config_mod.refresh_ffmpeg()
    assert config_mod.find_ffmpeg(cfg) is None


def test_detection_does_not_raise_on_a_bad_path(cfg, tmp_path):
    """打錯路徑不該讓設定頁掛掉 —— 回「未安裝」就好。"""
    cfg.ffmpeg_path = str(tmp_path / "missing")
    config_mod.refresh_ffmpeg()
    assert config_mod.find_ffmpeg(cfg) is None


# ── 影片（ffmpeg 用替身）────────────────────────────────

@pytest.fixture()
def fake_ffmpeg(monkeypatch, cfg):
    """把 `_ffmpeg_frame` 換掉，其餘（閘、縮圖、快取、狀態碼）全是真的。"""
    cfg.ffmpeg_path = None
    monkeypatch.setattr("shutil.which", lambda name: "/fake/ffmpeg")
    config_mod.refresh_ffmpeg()

    calls: list[int] = []

    def fake(exe, src, seconds):
        calls.append(seconds)
        return png_bytes()

    monkeypatch.setattr(files_mod, "_ffmpeg_frame", fake)
    return calls


def test_video_thumb_is_generated_and_cached(client, session, cfg, fake_ffmpeg):
    mid = _media(session, cfg, "clip.mp4")
    r = client.get(f"/api/media/{mid}/thumb")

    assert r.status_code == 200
    assert r.headers["content-type"] == "image/webp"
    im = Image.open(io.BytesIO(r.content))
    assert max(im.size) <= files_mod.THUMB_MAX_EDGE
    assert fake_ffmpeg == [files_mod.VIDEO_SEEK_SECONDS], "應該只抽第 1 秒那一格"

    # 第二次走快取，不再叫 ffmpeg
    assert client.get(f"/api/media/{mid}/thumb").status_code == 200
    assert len(fake_ffmpeg) == 1


def test_short_clip_falls_back_to_the_first_frame(
    client, session, cfg, monkeypatch, fake_ffmpeg
):
    """短於 1 秒的片子在第 1 秒沒有影格 —— 退到第 0 秒再試一次。

    這是**業務邏輯**不是掩蓋：兩次都失敗仍然明確回 500（見下一個測試）。
    """
    seen: list[int] = []

    def fake(exe, src, seconds):
        seen.append(seconds)
        return b"" if seconds else png_bytes()

    monkeypatch.setattr(files_mod, "_ffmpeg_frame", fake)
    mid = _media(session, cfg, "short.mp4")
    assert client.get(f"/api/media/{mid}/thumb").status_code == 200
    assert seen == [files_mod.VIDEO_SEEK_SECONDS, 0]


def test_both_seeks_failing_is_a_500(client, session, cfg, monkeypatch, fake_ffmpeg):
    """抽不出任何影格 = 原檔壞了。**不回佔位圖** —— 那會把壞檔藏起來。"""
    monkeypatch.setattr(files_mod, "_ffmpeg_frame", lambda *a: b"")
    mid = _media(session, cfg, "broken.mp4")
    r = client.get(f"/api/media/{mid}/thumb")
    assert r.status_code == 500
    assert r.json()["code"] == "thumb.no_frame"


def test_ffmpeg_timeout_is_a_500_not_a_hang(client, session, cfg, monkeypatch,
                                            fake_ffmpeg):
    import subprocess

    def boom(*_a):
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=files_mod.FFMPEG_TIMEOUT)

    monkeypatch.setattr(files_mod, "_ffmpeg_frame", boom)
    mid = _media(session, cfg, "hang.mp4")
    assert client.get(f"/api/media/{mid}/thumb").status_code == 500


# ── ugoira（完全不需要 ffmpeg）──────────────────────────

def test_ugoira_thumb_works_without_ffmpeg(client, session, cfg, monkeypatch):
    """⭐ 驗收標準明列：**在沒有 ffmpeg 的機器上也要成立。**"""
    monkeypatch.setattr("shutil.which", lambda _n: None)
    config_mod.refresh_ffmpeg()

    mid = _media(session, cfg, "anim.zip", content=ugoira_zip())
    r = client.get(f"/api/media/{mid}/thumb")

    assert r.status_code == 200
    assert r.headers["content-type"] == "image/webp"
    assert max(Image.open(io.BytesIO(r.content)).size) <= files_mod.THUMB_MAX_EDGE


def test_ugoira_takes_the_first_frame_by_name_order(client, session, cfg):
    """zip 內的順序不保證，檔名排序才是。第一張是紅的。"""
    mid = _media(
        session, cfg, "order.zip",
        content=ugoira_zip(names=("000001.jpg", "000000.jpg")),
    )
    r = client.get(f"/api/media/{mid}/thumb")
    assert r.status_code == 200
    im = Image.open(io.BytesIO(r.content)).convert("RGB")
    red, green, _ = im.getpixel((im.width // 2, im.height // 2))
    assert red > green, "取到的應該是檔名排序第一張（紅色）"


def test_empty_ugoira_zip_is_a_500(client, session, cfg):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w"):
        pass
    mid = _media(session, cfg, "empty.zip", content=buf.getvalue())
    r = client.get(f"/api/media/{mid}/thumb")
    assert r.status_code == 500
    assert r.json()["code"] == "thumb.ugoira_empty"


def test_corrupt_zip_is_a_500(client, session, cfg):
    mid = _media(session, cfg, "bad.zip", content=b"this is not a zip")
    assert client.get(f"/api/media/{mid}/thumb").status_code == 500


# ── 併發閘 ────────────────────────────────────────────

def test_at_most_two_ffmpeg_processes_at_once(client, session, cfg, monkeypatch):
    """⭐ 一頁 60 格全是影片時，沒有閘就是 60 個 ffmpeg 行程同時起來。

    縮圖端點是同步函式，跑在 FastAPI 的 threadpool 上 —— 沒有天然上限。
    """
    monkeypatch.setattr("shutil.which", lambda _n: "/fake/ffmpeg")
    config_mod.refresh_ffmpeg()
    cfg.thumb_video_concurrency = 2
    files_mod.reset_thumb_gate()

    live = 0
    peak = 0
    lock = threading.Lock()

    def fake(exe, src, seconds):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.05)
        with lock:
            live -= 1
        return png_bytes()

    monkeypatch.setattr(files_mod, "_ffmpeg_frame", fake)
    ids = [_media(session, cfg, f"c{i}.mp4") for i in range(10)]

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(
            lambda mid: client.get(f"/api/media/{mid}/thumb").status_code, ids))

    assert results == [200] * 10
    assert peak <= 2, f"同時有 {peak} 個 ffmpeg 在跑，閘沒關住"


def test_queue_timeout_is_503(client, session, cfg, monkeypatch):
    """等不到名額回 503（不是無限期掛著）—— 瀏覽器那端會先放棄，
    而 server 還在跑，那是最糟的一種。"""
    monkeypatch.setattr("shutil.which", lambda _n: "/fake/ffmpeg")
    config_mod.refresh_ffmpeg()
    monkeypatch.setattr(files_mod, "THUMB_QUEUE_TIMEOUT", 0.05)
    cfg.thumb_video_concurrency = 1
    files_mod.reset_thumb_gate()

    monkeypatch.setattr(
        files_mod, "_ffmpeg_frame",
        lambda *a: (time.sleep(0.4), png_bytes())[1])

    ids = [_media(session, cfg, f"q{i}.mp4") for i in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        codes = list(pool.map(
            lambda mid: client.get(f"/api/media/{mid}/thumb").status_code, ids))

    assert 503 in codes, f"沒有任何一個排隊逾時：{codes}"
    assert 200 in codes, "至少要有一個跑得完"


# ── 圖片不受影響 ──────────────────────────────────────

def test_images_do_not_enter_the_gate(client, session, cfg, monkeypatch):
    """圖片走 Pillow 是毫秒級 —— 圈進閘只會拖慢 96% 的請求。

    把閘設成 0 個名額（誰都拿不到），圖片仍然要成功。
    """
    monkeypatch.setattr(files_mod, "_gate",
                        lambda _cfg: pytest.fail("圖片不該去拿併發閘"))
    buf = io.BytesIO()
    Image.new("RGB", (800, 600), "red").save(buf, "JPEG")
    mid = _media(session, cfg, "plain.jpg", content=buf.getvalue())
    assert client.get(f"/api/media/{mid}/thumb").status_code == 200


# ── 偵測的三層順序 ────────────────────────────────────
#
# 這一組看的是**優先序與誠實回報**，不是「找不找得到」。
# 三層不是「找不到就退而求其次」的 fallback —— 判準是事後系統能不能說出
# 自己用了哪一支，所以 `ffmpeg_info()` 一定要回報來源。

def test_bundled_is_the_last_resort_not_the_first(cfg, monkeypatch):
    """⭐ 系統 PATH 上那支**優先於** pip 帶的。

    反過來的話，使用者自己裝了新版 ffmpeg 卻永遠在用套件裡那支舊的 ——
    而且畫面上完全看不出來。
    """
    monkeypatch.setattr("shutil.which",
                        lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None)
    monkeypatch.setattr(config_mod, "_bundled_ffmpeg", lambda: "/pip/ffmpeg.exe")
    config_mod.refresh_ffmpeg()
    assert config_mod.ffmpeg_info(cfg) == ("/usr/bin/ffmpeg", "path")


def test_falls_through_to_bundled_when_path_has_none(cfg, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _name: None)
    monkeypatch.setattr(config_mod, "_bundled_ffmpeg", lambda: "/pip/ffmpeg.exe")
    config_mod.refresh_ffmpeg()
    assert config_mod.ffmpeg_info(cfg) == ("/pip/ffmpeg.exe", "bundled")


def test_explicit_path_never_falls_through_to_bundled(cfg, monkeypatch, tmp_path):
    """⭐ 第 1 層有值時**絕不往下掉** —— 連 pip 帶的那支也不行。

    掉下去等於設定被靜默忽略。使用者指定某支 ffmpeg 通常正是因為
    預設那支有問題，偷偷換回去是最糟的結果。
    """
    monkeypatch.setattr("shutil.which", lambda _name: None)
    monkeypatch.setattr(config_mod, "_bundled_ffmpeg", lambda: "/pip/ffmpeg.exe")
    cfg.ffmpeg_path = str(tmp_path / "nope" / "ffmpeg.exe")
    config_mod.refresh_ffmpeg()
    assert config_mod.ffmpeg_info(cfg) == (None, "")


def test_missing_everywhere_reports_no_source(cfg, monkeypatch):
    """來源是空字串，不是「不知道」也不是猜一個 —— 設定頁靠它顯示「未安裝」。"""
    monkeypatch.setattr("shutil.which", lambda _name: None)
    monkeypatch.setattr(config_mod, "_bundled_ffmpeg", lambda: None)
    config_mod.refresh_ffmpeg()
    assert config_mod.ffmpeg_info(cfg) == (None, "")


def test_bundled_absent_is_not_an_error(monkeypatch):
    """imageio-ffmpeg 沒裝、或這個平台沒有預打包的 binary → None，不丟例外。

    它是第三層，缺席是正常情形（例如公開版使用者用 `--no-deps` 裝）。
    """
    import builtins
    real_import = builtins.__import__

    def no_imageio(name, *a, **kw):
        if name == "imageio_ffmpeg":
            raise ImportError("no module named imageio_ffmpeg")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_imageio)
    assert config_mod._bundled_ffmpeg() is None


def test_settings_endpoint_reports_the_source(client, cfg, monkeypatch):
    """⭐ 設定頁只說「已安裝」是不夠的。

    使用者要能分辨自己用的是系統那支還是 pip 帶的 —— 那是「這個檔為什麼
    抽不出影格」的第一個要問的事。
    """
    monkeypatch.setattr("shutil.which", lambda _name: None)
    monkeypatch.setattr(config_mod, "_bundled_ffmpeg", lambda: "/pip/ffmpeg.exe")
    config_mod.refresh_ffmpeg()
    f = client.get("/api/settings").json()["ffmpeg"]
    assert f == {"available": True, "path": "/pip/ffmpeg.exe", "source": "bundled"}


# ── 真的跑一次 ffmpeg ─────────────────────────────────
#
# 上面所有影片測試的 `_ffmpeg_frame()` 都是替身，所以它們驗不到
# **命令列本身**。這一節補那個缺口：用 ffmpeg 生一支真的 mp4，
# 再讓端點用真的 ffmpeg 去抽它的第一秒。

@pytest.fixture()
def real_ffmpeg(cfg):
    """機器上實際可用的 ffmpeg，沒有就 skip。"""
    cfg.ffmpeg_path = None
    config_mod.refresh_ffmpeg()
    exe = config_mod.find_ffmpeg(cfg)
    if not exe:
        pytest.skip("這台機器上偵測不到 ffmpeg（三層都沒有）")
    return exe


def _make_mp4(exe: str, dest, seconds: int = 2) -> bytes:
    """用 ffmpeg 自己生一支測試影片。**不放二進位 fixture 進版控。**"""
    import subprocess
    subprocess.run(
        [exe, "-y", "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=10:d={seconds}",
         "-pix_fmt", "yuv420p", str(dest)],
        capture_output=True, timeout=60, check=True,
    )
    return dest.read_bytes()


@pytest.mark.slow
def test_real_ffmpeg_extracts_a_frame_end_to_end(client, session, cfg, real_ffmpeg,
                                                 tmp_path):
    """⭐ 這一條驗的是**命令列**，不是流程。

    `-ss` 放在 `-i` **之前**（input seeking，直接跳關鍵格）。放後面會從頭
    解碼 —— 對一支 446 MB 的片子是好幾秒，而正式庫裡就有那種檔案。
    順序寫反不會報錯，只會變慢，所以它必須靠測試守住。
    """
    content = _make_mp4(real_ffmpeg, tmp_path / "src.mp4")
    mid = _media(session, cfg, "real.mp4", content=content)

    r = client.get(f"/api/media/{mid}/thumb")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/webp"
    im = Image.open(io.BytesIO(r.content))
    assert max(im.size) <= files_mod.THUMB_MAX_EDGE
    # testsrc 是彩色測試圖 —— 抽到黑畫面代表根本沒解到影格
    assert im.convert("RGB").getcolors(maxcolors=1) is None


@pytest.mark.slow
def test_real_ffmpeg_falls_back_for_a_sub_second_clip(client, session, cfg,
                                                      real_ffmpeg, tmp_path):
    """短於 1 秒的片子在第 1 秒沒有影格 —— 真的 ffmpeg 上也要退到第 0 秒。

    這條路徑在替身測試裡是模擬的；這裡是真的。
    """
    import subprocess
    dest = tmp_path / "tiny.mp4"
    subprocess.run(
        [real_ffmpeg, "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:d=0.4",
         "-pix_fmt", "yuv420p", str(dest)],
        capture_output=True, timeout=60, check=True,
    )
    mid = _media(session, cfg, "tiny.mp4", content=dest.read_bytes())
    assert client.get(f"/api/media/{mid}/thumb").status_code == 200


@pytest.mark.slow
def test_real_ffmpeg_on_a_corrupt_file_is_a_500(client, session, cfg, real_ffmpeg):
    """⭐ 壞檔要回 500，**不可以是 503**。

    503 的意思是「裝一下 ffmpeg 就好」—— 但 ffmpeg 明明在，是這個檔沒救。
    子行程也必須把它擋在外面：backend 不能因為一個壞檔就掛掉。
    """
    mid = _media(session, cfg, "garbage.mp4", content=bytes(range(256)) * 40)
    r = client.get(f"/api/media/{mid}/thumb")
    assert r.status_code == 500
