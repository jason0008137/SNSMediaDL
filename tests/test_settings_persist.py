"""GUI 改得動的設定要活過重啟，而且看得出來這個值是誰決定的。

觸發點：使用者回報「每次重開服務都要重新打開背景下載」。查證屬實 ——
`patch_settings()` 只改記憶體，而且那是**刻意**的（原 docstring 寫著
「這是『現在要不要跑』的暫時決定」）。他每一次都要它開著，那就不是暫時決定。

⭐ 四個核心性質，各對應一組測試：

1. **優先序是 `預設 < config.toml < prefs.json < 環境變數`。**
   prefs 贏過 config.toml，因為 GUI 上按下去是最近一次的明確意圖；
   但贏不過環境變數，那是部署層的覆寫。
2. **可持久化的鍵是白名單。** 憑證與路徑永遠不准寫進 prefs.json ——
   前者是多開一個外洩點，後者會讓同一批媒體散在兩個地方。
3. **壞掉的偏好檔不吞。** 靜默當作「沒有偏好」的症狀就是「我的設定又不見了」，
   那正是這整套機制要修的東西。
4. **寫入是原子的。** 半截 JSON 的下場同上。
"""

from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from snsmediadl import config as C
from snsmediadl.api.app import create_app, get_session
from snsmediadl.config import Config, load_config


@pytest.fixture()
def client(cfg, session):
    app = create_app(cfg)
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch):
    """環境變數會蓋掉一切，測試預設要在乾淨狀態下跑。

    需要驗環境變數優先序的那條自己 setenv。
    """
    for key in C.PERSISTABLE:
        monkeypatch.delenv(f"{C.ENV_PREFIX}{key.upper()}", raising=False)


def write_toml(tmp_path, text: str = ""):
    """寫一份測試用的 config.toml。

    ⚠️ **一律把 db_path 釘在 tmp_path。** 偏好檔的位置由 db_path 決定，
    沒釘的話 load_config() 會去讀專案根目錄的**真實** prefs.json ——
    測試結果就會隨開發者自己按過什麼而變。實測撞到過：跑完一次端對端、
    根目錄留下一個 prefs.json，兩條測試當場變紅。
    """
    p = tmp_path / "config.toml"
    p.write_text(f'db_path = "{(tmp_path / "x.db").as_posix()}"' + chr(10) + text,
                 encoding="utf-8")
    return p


# ───────────────────────────────── 1. 優先序


def test_default_when_nothing_is_set(tmp_path):
    cfg = load_config(config_file=write_toml(tmp_path))

    assert cfg.auto_download is False
    assert cfg.language == "en"
    assert C.setting_source(cfg, "auto_download") == "default"


def test_config_toml_beats_default(tmp_path):
    cfg = load_config(config_file=write_toml(tmp_path, "auto_download = true"))

    assert cfg.auto_download is True
    assert C.setting_source(cfg, "auto_download") == "config"


def test_prefs_beats_config_toml(tmp_path):
    """GUI 上按下去的是最近一次的明確意圖，該贏過幾個月前寫在設定檔裡的預設值。"""
    toml = write_toml(tmp_path, 'auto_download = true')
    (tmp_path / "prefs.json").write_text('{"auto_download": false}', encoding="utf-8")

    cfg = load_config(config_file=toml)

    assert cfg.auto_download is False
    assert C.setting_source(cfg, "auto_download") == "prefs"


def test_env_beats_prefs(tmp_path, monkeypatch):
    """⚠️ 環境變數仍然最大 —— 那是部署層的覆寫，改它的人知道自己在做什麼。"""
    toml = write_toml(tmp_path)
    (tmp_path / "prefs.json").write_text('{"auto_download": true}', encoding="utf-8")
    monkeypatch.setenv("SNSMEDIADL_AUTO_DOWNLOAD", "0")

    cfg = load_config(config_file=toml)

    assert cfg.auto_download is False
    assert C.setting_source(cfg, "auto_download") == "env"


def test_with_prefs_false_ignores_prefs(tmp_path):
    """`base_config()` 要看得到「沒有偏好的那份設定」，否則「改回 config.toml
    的值」不知道值該回到哪裡。"""
    toml = write_toml(tmp_path, 'auto_download = true')
    (tmp_path / "prefs.json").write_text('{"auto_download": false}', encoding="utf-8")

    assert load_config(config_file=toml).auto_download is False
    assert load_config(config_file=toml, with_prefs=False).auto_download is True


# ───────────────────────────────── 2. 白名單


@pytest.mark.parametrize("key", [
    "output_root", "thumb_root", "extra_media_roots", "db_path",
    "platform_credentials", "instance_tokens", "host", "port",
])
def test_dangerous_keys_are_not_persistable(key):
    """⚠️ 憑證與路徑**永遠**不准進偏好檔。

    憑證寫出去是多開一個外洩點；路徑決定檔案落在哪裡，執行到一半換掉會讓
    同一批媒體散在兩個地方。這條測試的用途是：有人把它們加進 `PERSISTABLE`
    的那一天，這裡會紅。
    """
    assert key not in C.PERSISTABLE


def test_save_pref_refuses_keys_outside_whitelist(tmp_path):
    cfg = Config(db_path=tmp_path / "x.db")

    with pytest.raises(ValueError, match="不可持久化"):
        C.save_pref(cfg, "output_root", "/somewhere/else")

    assert not (tmp_path / "prefs.json").exists()


def test_hand_written_non_persistable_key_is_ignored(tmp_path, caplog):
    """手動塞進去的東西不生效，但也不能安靜地消失。"""
    p = tmp_path / "prefs.json"
    p.write_text('{"auto_download": true, "output_root": "/evil"}', encoding="utf-8")

    data, err = C.load_prefs(p)

    assert data == {"auto_download": True}
    assert err is None
    assert any("output_root" in r.message for r in caplog.records)


# ───────────────────────────────── 3. 壞掉的偏好檔


def test_broken_json_does_not_crash_and_is_reported(tmp_path, caplog):
    p = tmp_path / "prefs.json"
    p.write_text("{", encoding="utf-8")

    data, err = C.load_prefs(p)

    assert data == {}
    assert err and "JSONDecodeError" in err
    assert caplog.records, "壞掉的偏好檔一定要留下 warning"


def test_broken_prefs_surfaces_on_config(tmp_path):
    """backend 照樣起得來，但 `prefs_error` 要帶著錯誤，設定頁才講得出來。"""
    toml = write_toml(tmp_path)
    (tmp_path / "prefs.json").write_text("not json at all", encoding="utf-8")

    cfg = load_config(config_file=toml)

    assert cfg.prefs_error is not None
    assert cfg.auto_download is False          # 退回預設，但**有講**


def test_prefs_that_is_not_an_object_is_rejected(tmp_path):
    p = tmp_path / "prefs.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")

    data, err = C.load_prefs(p)

    assert data == {}
    assert err


# ───────────────────────────────── 4. 寫入


def test_save_pref_round_trips(tmp_path):
    cfg = Config(db_path=tmp_path / "x.db")

    C.save_pref(cfg, "auto_download", True)

    assert cfg.auto_download is True
    assert json.loads((tmp_path / "prefs.json").read_text(encoding="utf-8")) \
        == {"auto_download": True}


def test_save_pref_leaves_a_complete_json_file(tmp_path):
    """原子寫入：結束之後檔案一定是完整的 JSON，而且暫存檔不留下來。"""
    cfg = Config(db_path=tmp_path / "x.db")

    C.save_pref(cfg, "auto_download", True)
    C.save_pref(cfg, "language", "zh-Hant")

    json.loads((tmp_path / "prefs.json").read_text(encoding="utf-8"))  # 解不開就炸
    assert not list(tmp_path.glob("*.tmp")), "暫存檔沒有清掉"


def test_save_pref_keeps_other_keys(tmp_path):
    cfg = Config(db_path=tmp_path / "x.db")
    C.save_pref(cfg, "language", "ja")

    C.save_pref(cfg, "auto_download", True)

    assert json.loads((tmp_path / "prefs.json").read_text(encoding="utf-8")) \
        == {"language": "ja", "auto_download": True}


def test_clear_pref_falls_back_to_base(tmp_path):
    cfg = Config(db_path=tmp_path / "x.db")
    C.save_pref(cfg, "auto_download", True)

    C.clear_pref(cfg, "auto_download", Config())

    assert cfg.auto_download is False
    assert json.loads((tmp_path / "prefs.json").read_text(encoding="utf-8")) == {}


# ───────────────────────────────── API


def test_settings_endpoint_reports_source(client):
    body = client.get("/api/settings").json()

    assert body["sources"]["auto_download"] == "default"
    assert body["language"] == "en"
    assert body["prefs_error"] is None


def test_patch_persists_and_survives_a_restart(client, cfg):
    """⭐ 這一條就是使用者回報的那件事。"""
    client.patch("/api/settings", json={"auto_download": True})

    # 「重啟」= 用同一份 db_path 重新載入設定
    reloaded = Config(db_path=cfg.db_path)
    prefs, err = C.load_prefs(C.prefs_path(reloaded))

    assert err is None
    assert prefs == {"auto_download": True}


def test_patch_reports_source_as_prefs(client):
    body = client.patch("/api/settings", json={"auto_download": True}).json()

    assert body["auto_download"] is True
    assert body["sources"]["auto_download"] == "prefs"


def test_patch_language(client, cfg):
    body = client.patch("/api/settings", json={"language": "zh-Hant"}).json()

    assert body["language"] == "zh-Hant"
    assert cfg.language == "zh-Hant"


def test_delete_resets_to_base(client, cfg):
    client.patch("/api/settings", json={"auto_download": True})

    body = client.delete("/api/settings/auto_download").json()

    assert body["auto_download"] is False
    assert body["sources"]["auto_download"] == "default"
    assert C.load_prefs(C.prefs_path(cfg))[0] == {}


def test_delete_rejects_unknown_key(client):
    """不默默忽略 —— 打錯字看起來會像「這個設定重設不了」。"""
    r = client.delete("/api/settings/output_root")

    assert r.status_code == 422


def test_config_values_is_empty_when_there_is_no_conflict(client):
    """沒有衝突時不回任何東西 —— 前端才不必自己判斷要不要顯示那句話。"""
    assert client.get("/api/settings").json()["config_values"] == {}


def test_overriding_a_mere_default_is_not_a_conflict(client):
    """⚠️ prefs 蓋掉**內建預設**不是衝突，那只是「你設了一個值」。

    少了這條判斷，沒有 config.toml 的人把背景下載打開之後會看到
    「config.toml 寫的是關閉」—— 而那個檔案根本不存在。實測撞到過。
    """
    client.patch("/api/settings", json={"auto_download": True})

    assert client.get("/api/settings").json()["config_values"] == {}


def test_conflict_is_reported_when_config_toml_really_says_otherwise(tmp_path, session):
    """config.toml **真的寫了**而且不一樣 —— 這一次才要講。"""
    from snsmediadl.api.app import create_app, get_session

    # ⚠️ db_path 一定要寫在 toml 裡，不能在 load_config 之後才指派 ——
    #    偏好檔的位置由 db_path 決定，晚一步就會去讀專案根目錄的真檔案。
    toml = write_toml(tmp_path, 'auto_download = true')
    cfg = load_config(config_file=toml)
    app = create_app(cfg)
    app.dependency_overrides[get_session] = lambda: session
    c = TestClient(app)

    c.patch("/api/settings", json={"auto_download": False})
    body = c.get("/api/settings").json()

    assert body["auto_download"] is False
    assert body["sources"]["auto_download"] == "prefs"
    assert body["config_values"] == {"auto_download": True}


def test_credentials_are_never_written_to_prefs(client, cfg, tmp_path):
    """PATCH 只認 model 上的欄位；憑證連送都送不進來。"""
    client.patch("/api/settings", json={"platform_credentials": {"pixiv": "SECRET"}})

    raw = C.prefs_path(cfg)
    text = raw.read_text(encoding="utf-8") if raw.exists() else ""
    assert "SECRET" not in text
    assert "platform_credentials" not in text
