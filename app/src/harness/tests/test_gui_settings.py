"""用户设置持久化（AppSettings / SettingsStore）：纯逻辑 + 临时目录，离屏快速。

覆盖 §2.1① 与 §6：往返读写、缺文件/坏 JSON 一律回落默认且不抛、单字段非法枚举
只回落该字段、to_dict/from_dict 往返、默认路径为「用户数据目录」下的绝对路径且
不落在仓库内。本模块不依赖 PySide6，也不联网。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from harness.gui.settings import (
    APP_DIR_NAME,
    AUTOSAVE_EVERY,
    DEFAULT_NARRATE_ACTIVITY,
    LANGUAGES,
    NARRATE_ACTIVITIES,
    THEMES,
    AppSettings,
    SettingsStore,
    default_settings_path,
    snap_narrate_activity,
)

REPO_ROOT = Path(__file__).resolve().parents[4]  # app/src/harness/tests/... -> 仓库根


# ------------------------------------------------------------------ AppSettings
def test_defaults():
    s = AppSettings()
    assert s.api_base_url == ""
    assert s.api_key == ""
    assert s.model_name == ""
    assert s.theme == "默认"
    assert s.language == "zh-Hans"
    assert s.autosave_every == 20
    assert s.narrate_activity == 0.5, "推进活跃度默认中档"


def test_to_dict_from_dict_round_trip():
    s = AppSettings(api_base_url="https://api.deepseek.com", api_key="sk-x",
                    model_name="deepseek-v4-flash", theme="深色",
                    language="ja", autosave_every=50, narrate_activity=0.8,
                    knowledge_enabled=False, stream_speak=False)
    d = s.to_dict()
    assert d == {
        "api_base_url": "https://api.deepseek.com",
        "api_key": "sk-x",
        "model_name": "deepseek-v4-flash",
        "theme": "深色",
        "language": "ja",
        "autosave_every": 50,
        "narrate_activity": 0.8,
        "knowledge_enabled": False,
        # 流式开口（§二．8）：产品缺省=开，但**存过什么就原样带出来**（新增字段必须进
        # 白名单，否则用户关掉的值会在下一次 update 时被静默丢弃）
        "stream_speak": False,
    }
    assert AppSettings.from_dict(d) == s
    # to_dict 返回的是快照（改它不影响原对象）
    d["theme"] = "白色"
    assert s.theme == "深色"


def test_from_dict_ignores_unknown_keys_and_bad_shape():
    assert AppSettings.from_dict({}) == AppSettings()
    assert AppSettings.from_dict(None) == AppSettings()          # type: ignore[arg-type]
    assert AppSettings.from_dict([1, 2]) == AppSettings()        # type: ignore[arg-type]
    s = AppSettings.from_dict({"神秘字段": 1, "theme": "深蓝"})
    assert s.theme == "深蓝"
    assert not hasattr(s, "神秘字段")


def test_from_dict_bad_types_fall_back_per_field():
    s = AppSettings.from_dict({"api_base_url": 123, "api_key": None,
                               "model_name": ["x"], "theme": 5,
                               "language": False, "autosave_every": "很多"})
    assert s == AppSettings()


@pytest.mark.parametrize("bad", ["²", "①", "⁰", "³", "½"])
def test_digit_like_strings_never_raise_from_load(tmp_path, bad):
    """`str.isdigit()` 为真但 `int()` 会抛的字符（上标/圆圈数字）不能让启动崩。

    设置文件是用户可手改的文本：`{"autosave_every": "²"}` 这种脏值必须回落默认值，
    绝不让异常冒出 SettingsStore.load()（app.py 在 QApplication 之前就调它）。
    """
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"autosave_every": bad}, ensure_ascii=False),
                 encoding="utf-8")
    assert SettingsStore(p).load().autosave_every == 20


def test_zero_autosave_every_falls_back_and_load_never_raises(tmp_path):
    """0x0（=0，不在 5/20/50/100 里）回落默认；from_dict 也绝不抛。"""
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"autosave_every": 0x0}, ensure_ascii=False),
                 encoding="utf-8")
    assert SettingsStore(p).load().autosave_every == 20
    assert AppSettings.from_dict({"autosave_every": "²"}).autosave_every == 20
    assert AppSettings.from_dict({"autosave_every": object()}) == AppSettings()


@pytest.mark.parametrize("root", ['"字符串"', "42", "true", "[]", "null",
                                  '{"autosave_every": {"深": 1}}'])
def test_non_dict_roots_and_odd_values_never_raise(tmp_path, root):
    """根不是字典（或字段值形状古怪）→ 一律默认值，绝不从 load() 冒异常。"""
    p = tmp_path / "settings.json"
    p.write_text(root, encoding="utf-8")
    assert SettingsStore(p).load() == AppSettings()


@pytest.mark.parametrize("theme", THEMES)
def test_valid_themes_accepted(theme):
    assert AppSettings.from_dict({"theme": theme}).theme == theme


@pytest.mark.parametrize("lang", LANGUAGES)
def test_valid_languages_accepted(lang):
    assert AppSettings.from_dict({"language": lang}).language == lang


@pytest.mark.parametrize("n", AUTOSAVE_EVERY)
def test_valid_autosave_accepted(n):
    assert AppSettings.from_dict({"autosave_every": n}).autosave_every == n


def test_enum_sets_match_spec():
    assert THEMES == ("默认", "深色", "白色", "深蓝")
    assert LANGUAGES == ("zh-Hans", "zh-Hant", "en", "fr", "de", "ja", "ko")
    assert AUTOSAVE_EVERY == (5, 20, 50, 100)
    assert NARRATE_ACTIVITIES == (0.2, 0.5, 0.8, 1.0)
    assert DEFAULT_NARRATE_ACTIVITY == 0.5


# ------------------------------------------------------- 推进活跃度（§6.2）
@pytest.mark.parametrize("level", NARRATE_ACTIVITIES)
def test_valid_narrate_activity_accepted_and_survives_round_trip(tmp_path, level):
    """四档都是合法值：from_dict 原样收，落盘再读仍一模一样（浮点不漂）。"""
    p = tmp_path / "settings.json"
    store = SettingsStore(p)
    store.save(AppSettings(narrate_activity=level))
    assert store.load().narrate_activity == level
    assert AppSettings.from_dict({"narrate_activity": level}).narrate_activity == level


@pytest.mark.parametrize("bad,expected", [
    (0.2, 0.2),
    (0.21, 0.2), (0.3, 0.2),           # 就近吸附
    (0.51, 0.5), (0.7, 0.8), (0.81, 0.8),
    (0.95, 1.0), (0.0, 0.2), (-3.0, 0.2), (7.5, 1.0),   # 越界夹到端点档
    (0.9, 0.8),                        # 并列（各差 0.1）取较小档，确定性
    ("0.8", 0.8), (" 0.5 ", 0.5),      # 数字串容错
    ("极多", DEFAULT_NARRATE_ACTIVITY),  # 非数字文字 → 默认
    ("", DEFAULT_NARRATE_ACTIVITY),
    (None, DEFAULT_NARRATE_ACTIVITY),
    (True, DEFAULT_NARRATE_ACTIVITY),   # bool 是 int 的子类，但不当数字用
    (False, DEFAULT_NARRATE_ACTIVITY),
    ([0.8], DEFAULT_NARRATE_ACTIVITY),
    ({"v": 0.8}, DEFAULT_NARRATE_ACTIVITY),
    (float("nan"), DEFAULT_NARRATE_ACTIVITY),
    (float("inf"), DEFAULT_NARRATE_ACTIVITY),
    ("nan", DEFAULT_NARRATE_ACTIVITY),
    ("²", DEFAULT_NARRATE_ACTIVITY),
])
def test_narrate_activity_snaps_or_falls_back(bad, expected):
    """只认四档：数字就近吸附（越界夹端点），非数字/bool/NaN 一律回落 0.5。"""
    assert AppSettings.from_dict({"narrate_activity": bad}).narrate_activity == expected
    assert snap_narrate_activity(bad) == expected


def test_narrate_activity_snap_never_raises_on_odd_input():
    """脏输入绝不从 load()/snap 冒异常（设置文件是用户可手改的文本）。"""
    for bad in (object(), b"0.8", float("-inf"), "1e999", "   ", "0.5.5"):
        assert snap_narrate_activity(bad) in NARRATE_ACTIVITIES


def test_narrate_activity_bad_value_in_file_only_resets_that_field(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"api_key": "sk-keep", "narrate_activity": "经常",
                             "theme": "深色"}, ensure_ascii=False), encoding="utf-8")
    s = SettingsStore(p).load()
    assert s.narrate_activity == DEFAULT_NARRATE_ACTIVITY
    assert s.api_key == "sk-keep" and s.theme == "深色"   # 其余字段不受牵连


def test_update_persists_narrate_activity_and_snaps_out_of_range(tmp_path):
    """控件改档 → SettingsStore.update：落盘；越界值吸附到最近档而不是写进文件。"""
    p = tmp_path / "settings.json"
    store = SettingsStore(p)
    assert store.update(narrate_activity=1.0).narrate_activity == 1.0
    assert store.load().narrate_activity == 1.0
    assert store.update(narrate_activity=0.83).narrate_activity == 0.8
    assert json.loads(p.read_text(encoding="utf-8"))["narrate_activity"] == 0.8
    assert store.update(narrate_activity="乱写").narrate_activity == 0.5


# ------------------------------------------ 信息库检索 开/关（§10.2） ---------- --

def test_knowledge_switch_defaults_to_on():
    """「信息库检索」缺省**开**（§10.2）：不给任何设置时就是今天的新行为（有库就查库）。"""
    from harness.gui.settings import DEFAULT_KNOWLEDGE_ENABLED

    assert DEFAULT_KNOWLEDGE_ENABLED is True
    assert AppSettings().knowledge_enabled is True
    assert AppSettings.from_dict({}).knowledge_enabled is True


def test_knowledge_switch_is_in_the_field_whitelist(tmp_path):
    """开关必须**进字段白名单**——不加进去会被 `update` 静默丢弃（§10.2 点名的坑）。

    白名单不是一张手写表，而是 dataclass 的字段集合（`update` 用它过滤 kwargs）。故这里
    钉两件事：字段在 `fields(AppSettings)` 里，且 `update(knowledge_enabled=False)` 真的
    落进文件——"界面点了关、下一场照样查库"正是这个坑的症状。
    """
    from dataclasses import fields

    from harness.gui.settings import DEFAULT_KNOWLEDGE_ENABLED  # noqa: F401  （存在即可）

    assert "knowledge_enabled" in {f.name for f in fields(AppSettings)}

    p = tmp_path / "settings.json"
    store = SettingsStore(p)
    assert store.update(knowledge_enabled=False).knowledge_enabled is False
    assert store.load().knowledge_enabled is False, "关掉必须能读回来"
    assert json.loads(p.read_text(encoding="utf-8"))["knowledge_enabled"] is False
    assert store.update(knowledge_enabled=True).knowledge_enabled is True
    assert store.load().knowledge_enabled is True


@pytest.mark.parametrize("bad,expected", [
    ("false", False), ("0", False), ("no", False), ("off", False), ("False", False),
    ("true", True), ("1", True), ("yes", True), ("YES", True),
    (0, False), (1, True), (False, False), (True, True),
    ("久了", True), (None, True), ([1], True), (2, True),  # 读不出来 → 默认（开）
])
def test_knowledge_switch_accepts_loose_writings_and_falls_back_to_on(bad, expected):
    """手改 settings.json 是常态：true/false、0/1、yes/no（大小写随意）都认；认不出来按默认（开）。

    与 `knowledgestore._as_bool` 同一套路。**读盘绝不抛**：坏值只回落这一格的默认值。
    """
    assert AppSettings.from_dict({"knowledge_enabled": bad}).knowledge_enabled is expected


def test_knowledge_switch_bad_value_only_resets_that_field(tmp_path):
    """一格坏值只回落它自己，其余字段照旧（与其它字段同一纪律）。"""
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"api_key": "sk-keep", "knowledge_enabled": "大概吧",
                             "theme": "深色"}, ensure_ascii=False), encoding="utf-8")
    s = SettingsStore(p).load()
    assert s.knowledge_enabled is True
    assert s.api_key == "sk-keep" and s.theme == "深色"


# ------------------------------------------------------ default_settings_path
def test_default_settings_path_absolute_and_outside_repo():
    p = default_settings_path()
    assert p.is_absolute()
    assert APP_DIR_NAME in p.parts
    assert p.name == "settings.json"
    assert REPO_ROOT not in p.parents
    assert not str(p).startswith(str(REPO_ROOT))


def test_default_settings_path_uses_user_data_dir(monkeypatch, tmp_path):
    if sys.platform == "win32":
        monkeypatch.setenv("APPDATA", str(tmp_path))
        expected = tmp_path / APP_DIR_NAME / "settings.json"
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.delenv("APPDATA", raising=False)
        expected = tmp_path / APP_DIR_NAME / "settings.json"
    assert default_settings_path() == expected


def test_default_settings_path_falls_back_when_env_missing(monkeypatch):
    for var in ("APPDATA", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)
    p = default_settings_path()
    assert p.is_absolute()
    assert APP_DIR_NAME in p.parts


# ------------------------------------------------------------- SettingsStore
def test_store_defaults_to_default_settings_path():
    """缺省路径取自**模块内**的 default_settings_path（故 conftest 的沙箱夹具能同时
    重定向 Store 与函数——测试模块若持有导入时的原函数引用，两者会不一致）。"""
    from harness.gui import settings as settings_mod
    assert SettingsStore().path == settings_mod.default_settings_path()


def test_store_accepts_explicit_path(tmp_path):
    p = tmp_path / "sub" / "settings.json"
    assert SettingsStore(p).path == p


def test_missing_file_returns_defaults_without_creating_it(tmp_path):
    p = tmp_path / "settings.json"
    store = SettingsStore(p)
    assert store.load() == AppSettings()
    assert not p.exists()  # 只读不落盘


def test_save_then_load_round_trip(tmp_path):
    p = tmp_path / "nested" / "deep" / "settings.json"
    store = SettingsStore(p)
    s = AppSettings(api_base_url="https://api.deepseek.com", api_key="sk-1",
                    model_name="deepseek-v4-flash", theme="深蓝",
                    language="de", autosave_every=100)
    written = store.save(s)
    assert written == p
    assert p.exists()
    assert store.load() == s


def test_save_writes_pretty_utf8_json(tmp_path):
    p = tmp_path / "settings.json"
    SettingsStore(p).save(AppSettings(theme="深色"))
    raw = p.read_bytes()
    text = raw.decode("utf-8")               # 必须是 UTF-8
    assert "深色" in text                     # 中文不转义
    assert "\\u6df1" not in text
    assert "\n" in text                       # 美化输出（非单行）
    assert json.loads(text)["theme"] == "深色"


def test_corrupt_json_returns_defaults_and_does_not_raise(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("{这不是 JSON,,,", encoding="utf-8")
    assert SettingsStore(p).load() == AppSettings()


def test_non_dict_json_returns_defaults(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    assert SettingsStore(p).load() == AppSettings()
    p.write_text("null", encoding="utf-8")
    assert SettingsStore(p).load() == AppSettings()


def test_empty_file_returns_defaults(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("", encoding="utf-8")
    assert SettingsStore(p).load() == AppSettings()


def test_unreadable_path_returns_defaults(tmp_path):
    # 目录当文件读 → OSError，仍不得抛出
    d = tmp_path / "settings.json"
    d.mkdir()
    assert SettingsStore(d).load() == AppSettings()


def test_unknown_enum_values_fall_back_per_field_keeping_rest(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({
        "api_base_url": "https://api.deepseek.com",
        "model_name": "m1",
        "theme": "霓虹",              # 非法 → 默认
        "language": "klingon",       # 非法 → 默认
        "autosave_every": 7,         # 非法 → 默认
    }, ensure_ascii=False), encoding="utf-8")
    s = SettingsStore(p).load()
    assert s.theme == "默认"
    assert s.language == "zh-Hans"
    assert s.autosave_every == 20
    assert s.api_base_url == "https://api.deepseek.com"   # 其余字段保留
    assert s.model_name == "m1"


def test_load_fills_missing_fields_with_defaults(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"theme": "白色"}, ensure_ascii=False), encoding="utf-8")
    s = SettingsStore(p).load()
    assert s.theme == "白色"
    assert s.language == "zh-Hans"


# -------------------------------------------------------------- update/save
def test_update_replaces_known_fields_and_persists(tmp_path):
    p = tmp_path / "settings.json"
    store = SettingsStore(p)
    store.save(AppSettings(api_key="sk-old", theme="默认", autosave_every=20))

    s = store.update(theme="深色", autosave_every=50)
    assert s.theme == "深色"
    assert s.autosave_every == 50
    assert s.api_key == "sk-old"                  # 未提及的字段不动
    assert store.load() == s                      # 已落盘


def test_update_on_missing_file_starts_from_defaults(tmp_path):
    p = tmp_path / "settings.json"
    store = SettingsStore(p)
    s = store.update(model_name="deepseek-v4-flash")
    assert s.model_name == "deepseek-v4-flash"
    assert s.theme == "默认"
    assert p.exists()


def test_update_ignores_unknown_kwargs(tmp_path):
    p = tmp_path / "settings.json"
    store = SettingsStore(p)
    s = store.update(不存在="x", theme="深色")
    assert s.theme == "深色"
    assert not hasattr(s, "不存在")
    assert store.load() == s


def test_update_with_invalid_enum_falls_back_to_default(tmp_path):
    p = tmp_path / "settings.json"
    store = SettingsStore(p)
    store.save(AppSettings(language="ja"))
    s = store.update(language="不存在的语言")
    assert s.language == "zh-Hans"
    assert store.load().language == "zh-Hans"


def test_update_does_not_clobber_on_corrupt_file(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("坏掉的内容", encoding="utf-8")
    s = SettingsStore(p).update(theme="白色")
    assert s.theme == "白色"
    assert s.language == "zh-Hans"
    assert SettingsStore(p).load() == s           # 覆盖成合法 JSON


def test_save_and_load_round_trip_with_all_languages(tmp_path):
    p = tmp_path / "settings.json"
    store = SettingsStore(p)
    for lang in LANGUAGES:
        store.save(AppSettings(language=lang))
        assert SettingsStore(p).load().language == lang


def test_no_repo_side_effects(tmp_path, monkeypatch):
    # 显式路径存储不得在真实用户目录写任何东西
    monkeypatch.setenv("APPDATA", str(tmp_path / "apdata"))
    p = tmp_path / "settings.json"
    SettingsStore(p).save(AppSettings())
    assert not (tmp_path / "apdata").exists()
    assert os.listdir(tmp_path) == ["settings.json"]
