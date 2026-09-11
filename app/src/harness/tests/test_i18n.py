"""i18n 框架（spec §7）：语言表 / Translator / 目录完整性 / LLM 语言指令。

纯逻辑、无 IO、无网络、确定性。锁定五件事：
  · LANGUAGES 恰为七个语言码 → 母语名，默认 zh-Hans；
  · Translator：t() 命中目录给译文、缺键**原样返回键**（界面可见地降级，绝不抛），
    fmt 给了才做 str.format 且格式化失败回退原文，set_language 只认已知码；
  · 非主语言目录可残缺——缺的键回落 zh-Hans 主目录；
  · llm_language_directive：七个语言码全给非空中文指令且**含该语言母语名**；
  · zh-Hans 目录完整（键数 > 30），所有目录的键/值皆为非空 str。
"""
import ast
import re
from pathlib import Path

import pytest

from harness import i18n

EXPECTED_LANGUAGES = {
    "zh-Hans": "简体中文",
    "zh-Hant": "繁體中文",
    "en": "English",
    "fr": "Français",
    "de": "Deutsch",
    "ja": "日本語",
    "ko": "한국어",
}

#: 顶栏菜单 + 设置子菜单的键：§7 要求「其余 6 种语言先落地菜单与关键界面」，
#: 故这组键**七种语言都必须有**（缺了就回落中文，菜单看起来会半中半外）。
MENU_KEYS = (
    "menu.settings", "menu.scene", "menu.character", "menu.api_config",
    "menu.theme", "menu.language", "menu.autosave", "menu.autosave_every",
    "menu.open_scene", "menu.manage_scenes", "menu.new_scene",
    "menu.add_character", "menu.remove_character", "menu.new_character",
    "menu.manage_characters", "menu.advanced_cast",
    # §3.3 新增：配置场景 / 保存场景 / 导入场景 / 导入角色（都是顶栏菜单项）
    "menu.configure_scene", "menu.save_scene", "menu.import_scene",
    "menu.import_character",
)

#: 最常见的按钮与状态：同样要求七种语言都有（界面最常看的就是这几处）。
COMMON_KEYS = (
    "app.title", "status.ready", "status.running", "status.paused",
    "status.closed", "status.stopped", "panel.scene", "panel.metrics",
    "btn.pause", "btn.stop", "btn.continue", "btn.save", "btn.cancel",
    "btn.close", "btn.new", "btn.edit", "btn.delete",
    # 空状态页那三个按钮（§3.3：启动直达主界面，用户见到的第一屏）
    "btn.open_scene", "btn.new_scene", "btn.new_character",
)

#: 界面源码（i18n 键的**唯一**消费方）：扫描它们的字面量当回归网。
_GUI_SOURCES = ("main_window.py", "library.py")

#: 「看起来像 i18n 键」的字面量：小写点分标识符（`panel.scene` / `btn.pause`）。
_KEY_LIKE = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+")


def _gui_dir() -> Path:
    return Path(i18n.__file__).resolve().parent / "gui"


def _dotted_literals(path: Path) -> set[str]:
    """源码里全部「小写点分标识符」字符串字面量（含 docstring 与函数默认值）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _KEY_LIKE.fullmatch(node.value):
                out.add(node.value)
    return out


# ---------- LANGUAGES / 默认 ----------

def test_languages_table_is_exactly_the_seven():
    assert i18n.LANGUAGES == EXPECTED_LANGUAGES
    assert len(i18n.LANGUAGES) == 7


def test_default_language_is_zh_hans_and_present():
    assert i18n.DEFAULT_LANGUAGE == "zh-Hans"
    assert i18n.DEFAULT_LANGUAGE in i18n.LANGUAGES


def test_catalogs_only_contain_known_language_codes():
    assert set(i18n.CATALOGS) <= set(i18n.LANGUAGES)
    assert i18n.DEFAULT_LANGUAGE in i18n.CATALOGS


# ---------- Translator 基本行为 ----------

def test_default_translator_uses_zh_hans():
    tr = i18n.Translator()
    assert tr.language == i18n.DEFAULT_LANGUAGE


def test_translator_honours_given_language():
    assert i18n.Translator("en").language == "en"


def test_unknown_initial_language_falls_back_to_default():
    assert i18n.Translator("tlh").language == i18n.DEFAULT_LANGUAGE


def test_t_returns_catalog_value():
    tr = i18n.Translator()
    key = "status.ready"
    assert tr.t(key) == i18n.CATALOGS["zh-Hans"][key]
    assert tr.t(key) != ""


def test_every_available_key_translates_non_empty_in_default():
    tr = i18n.Translator()
    for key in i18n.available_keys():
        assert tr.t(key) == i18n.CATALOGS["zh-Hans"][key]
        assert tr.t(key) != ""


def test_missing_key_returns_the_key_itself():
    tr = i18n.Translator()
    assert tr.t("no.such.key") == "no.such.key"
    assert tr.t("") == ""


def test_t_never_raises_on_odd_keys():
    tr = i18n.Translator()
    for key in ("...", "  ", "没有这个", "a.b.c.d"):
        assert tr.t(key) == key


# ---------- 格式化 ----------

def test_format_substitution_applies_when_fmt_given():
    tr = i18n.Translator()
    out = tr.t("err.card_read_failed", path="a.json", exc="boom")
    assert "a.json" in out and "boom" in out
    assert "{" not in out and "}" not in out


def test_format_is_skipped_when_no_fmt_given():
    tr = i18n.Translator()
    # 模板里有占位符，但不给 fmt → 原样返回，不做 str.format。
    key = "err.card_read_failed"
    assert tr.t(key) == i18n.CATALOGS["zh-Hans"][key]


def test_tokens_template_formats():
    tr = i18n.Translator()
    out = tr.t("metric.tokens", calls=3, prompt=120, completion=45)
    assert "3" in out and "120" in out and "45" in out


def test_malformed_format_returns_raw_template_without_raising():
    tr = i18n.Translator()
    raw = i18n.CATALOGS["zh-Hans"]["err.card_read_failed"]
    # 缺少必需关键字 → str.format 抛 KeyError，必须吞掉并回退原文。
    assert tr.t("err.card_read_failed", wrong_kwarg=1) == raw
    # 多的关键字不影响结果。
    assert tr.t("err.card_read_failed", path="p", exc="e", extra=1) != raw


def test_t_or_returns_fallback_for_missing_key():
    tr = i18n.Translator()
    assert tr.t_or("no.such.key", "兜底") == "兜底"


def test_t_or_prefers_catalog_over_fallback():
    tr = i18n.Translator()
    assert tr.t_or("status.ready", "兜底") == i18n.CATALOGS["zh-Hans"]["status.ready"]


def test_t_or_formats_fallback_when_fmt_given():
    tr = i18n.Translator()
    assert tr.t_or("no.such.key", "共 {n} 条", n=7) == "共 7 条"


def test_t_or_malformed_fallback_returns_raw():
    tr = i18n.Translator()
    assert tr.t_or("no.such.key", "共 {n} 条", bogus=1) == "共 {n} 条"


# ---------- set_language ----------

def test_set_language_switches_known_code_and_returns_true():
    tr = i18n.Translator()
    assert tr.set_language("ja") is True
    assert tr.language == "ja"


def test_set_language_rejects_unknown_and_keeps_current():
    tr = i18n.Translator("fr")
    assert tr.set_language("klingon") is False
    assert tr.language == "fr"
    assert tr.set_language("") is False
    assert tr.language == "fr"


def test_set_language_roundtrip_all_seven():
    tr = i18n.Translator()
    for code in i18n.LANGUAGES:
        assert tr.set_language(code) is True
        assert tr.language == code


# ---------- 残缺目录回落主语言 ----------

def test_partial_catalog_missing_key_falls_back_to_default():
    # "field.corpus" 只在 zh-Hans 主目录里定；切到 en 后应回落中文原文，而非键名。
    tr = i18n.Translator("en")
    assert tr.t("field.corpus") == i18n.CATALOGS["zh-Hans"]["field.corpus"]


def test_language_with_no_catalog_at_all_falls_back_to_default():
    tr = i18n.Translator()
    tr.set_language("ko")
    tr.t("status.ready")  # 不抛即可；有 ko 目录则给 ko，否则回落 zh-Hans
    assert tr.t("field.corpus") == i18n.CATALOGS["zh-Hans"]["field.corpus"]


def test_non_default_catalog_uses_its_own_value_when_present():
    en = i18n.CATALOGS.get("en", {})
    if not en:
        pytest.skip("en 目录暂为占位")
    key = next(iter(en))
    assert i18n.Translator("en").t(key) == en[key]


# ---------- 目录完整性 ----------

def test_zh_hans_catalog_is_complete_and_large():
    cat = i18n.CATALOGS["zh-Hans"]
    assert len(cat) > 30


def test_all_catalog_keys_and_values_are_non_empty_str():
    for code, cat in i18n.CATALOGS.items():
        assert isinstance(code, str)
        for key, value in cat.items():
            assert isinstance(key, str) and key
            assert isinstance(value, str), (code, key)
            assert value != "", (code, key)


def test_keys_are_namespaced_dotted():
    for key in i18n.CATALOGS["zh-Hans"]:
        assert "." in key, key
        assert key == key.strip()


def test_available_keys_is_sorted_str_list_matching_default_catalog():
    keys = i18n.available_keys()
    assert isinstance(keys, list)
    assert all(isinstance(k, str) for k in keys)
    assert keys == sorted(keys)
    assert keys == sorted(i18n.CATALOGS["zh-Hans"])
    assert len(keys) > 30


def test_available_keys_is_deterministic():
    assert i18n.available_keys() == i18n.available_keys()


# ---------- 源码扫描：界面用到的每个键都得在 zh-Hans 主目录里 ----------

@pytest.mark.parametrize("name", _GUI_SOURCES)
def test_every_key_literal_in_the_gui_exists_in_the_zh_hans_catalog(name):
    """回归网：GUI 源码里出现的每个「像键的字面量」都必须是主目录里的键。

    只认前缀命中主目录命名空间的字面量（故 `models.yaml` 这类同形串不会误报）。这一条
    同时覆盖间接用法——`_STATUS_KEYS` 的值、`COLUMN_KEYS`、`_retranslate_menus` 里的
    元组、`_pause_button_key()` 的返回值等，只要写在源码里就会被扫到。
    """
    catalog = i18n.CATALOGS[i18n.DEFAULT_LANGUAGE]
    namespaces = {key.split(".")[0] for key in catalog}
    missing: list[str] = []
    checked = 0
    for literal in sorted(_dotted_literals(_gui_dir() / name)):
        if literal.split(".")[0] not in namespaces:
            continue                      # 与 i18n 同形但它不是键（如 settings.json）
        checked += 1
        if literal not in catalog:
            missing.append(literal)
    assert checked > 60, f"{name}：扫到的键字面量只有 {checked} 个，扫描口径可能坏了"
    assert missing == [], f"{name}：这些键被用了但 zh-Hans 主目录里没有：{missing}"


def test_panel_log_is_present_in_every_catalog_including_the_master():
    """`panel.log` 六个非默认目录都落地了，zh-Hans 主目录也必须有一份。

    主目录缺键时，zh-Hans 界面取它只会原样显示键名 `panel.log`（可见降级）；而其它
    语言都回落主目录——一份缺失会把七种语言一起拖下水。
    """
    for code in i18n.LANGUAGES:
        assert "panel.log" in i18n.CATALOGS[code], f"{code} 缺 panel.log"
        assert i18n.CATALOGS[code]["panel.log"].strip(), code


def test_main_window_has_no_dead_cast_action_labels():
    """`_CAST_ACTION_LABELS` 无人消费（引擎按 add/remove/… 认动作）→ 已删除，别再回来。"""
    source = (_gui_dir() / "main_window.py").read_text(encoding="utf-8")
    assert "_CAST_ACTION_LABELS" not in source


def test_gui_sources_actually_use_the_translator():
    """两个界面文件都得真的接上 i18n（而不是只把键写在文档里）。"""
    for name in _GUI_SOURCES:
        source = (_gui_dir() / name).read_text(encoding="utf-8")
        assert "Translator" in source or "translator" in source, name
        assert ".t(" in source, name


def test_status_vocabulary_keys_exist_in_the_default_catalog():
    """状态词表（窗口把 worker 的中文状态映射到这些键）必须先在主目录里落地。"""
    catalog = i18n.CATALOGS[i18n.DEFAULT_LANGUAGE]
    for key in ("status.ready", "status.running", "status.paused",
                "status.waiting", "status.retry", "status.closed", "status.stopped"):
        assert key in catalog, key


# ---------- 七种语言的菜单 / 常用键落地 ----------

def test_all_seven_languages_define_the_menu_and_common_keys():
    """§7：其余 6 种语言先落地菜单与关键界面——菜单/最常用按钮与状态七种语言都有。"""
    for code in i18n.LANGUAGES:
        catalog = i18n.CATALOGS[code]
        missing = [k for k in MENU_KEYS + COMMON_KEYS if k not in catalog]
        assert missing == [], f"{code} 缺这些键：{missing}"


def test_all_seven_languages_have_non_empty_menu_values():
    for code in i18n.LANGUAGES:
        for key in MENU_KEYS + COMMON_KEYS:
            assert i18n.CATALOGS[code][key].strip(), (code, key)


#: 推进活跃度四档标签（§6.2）。它挂在左栏「场景推进」卡的控件上——七种语言都得落地，
#: 缺哪一档哪一档就露中文；同一语言里四档还必须**互不相同**（否则控件上看不出区别）。
ACTIVITY_KEYS = (
    "narration.activity.low", "narration.activity.mid",
    "narration.activity.more", "narration.activity.max",
)


def test_all_seven_languages_define_the_narrate_activity_levels():
    for code in i18n.LANGUAGES:
        cat = i18n.CATALOGS[code]
        missing = [k for k in ACTIVITY_KEYS if k not in cat]
        assert missing == [], f"{code} 缺这些键：{missing}"
        for key in ACTIVITY_KEYS:
            assert cat[key].strip(), (code, key)
        labels = [cat[key] for key in ACTIVITY_KEYS]
        assert len(set(labels)) == 4, f"{code} 四档标签重复：{labels}"


def test_activity_control_keys_land_in_the_master_catalog():
    """控件自身的标签/提示只在主目录落地（其余语言回落中文 = 可见降级，不是空白）。"""
    cat = i18n.CATALOGS[i18n.DEFAULT_LANGUAGE]
    for key in ("narration.activity", "narration.activity_tip"):
        assert key in cat and cat[key].strip(), key


def test_undo_and_rewrite_warnings_mention_the_truncation():
    """§6.1：撤销/改写会**回溯整段下文**，两条确认文案必须把「丢弃其后的内容」说清楚。"""
    cat = i18n.CATALOGS[i18n.DEFAULT_LANGUAGE]
    for key in ("dlg.undo_narration_title", "dlg.undo_narration_ask",
                "dlg.rewrite_narration_title", "dlg.rewrite_narration_ask"):
        assert key in cat and cat[key].strip(), key
    for key in ("dlg.undo_narration_ask", "dlg.rewrite_narration_ask"):
        assert "它之后的所有内容" in cat[key], key


def test_menu_keys_do_not_silently_fall_back_for_other_languages():
    """菜单标题不能只是「有键但值等于中文」——那等于没翻译（zh-Hant 除外，字形本就相近）。"""
    tr_by_code = {code: i18n.Translator(code) for code in i18n.LANGUAGES}
    tr_by_code[i18n.DEFAULT_LANGUAGE].t("menu.settings")  # 主目录自身当然等于自己
    for code in i18n.LANGUAGES:
        if code == i18n.DEFAULT_LANGUAGE:
            continue
        tr = tr_by_code[code]
        for key in ("menu.settings", "menu.scene", "menu.language"):
            assert tr.t(key) == i18n.CATALOGS[code][key], (code, key)
            assert tr.t(key) != i18n.CATALOGS[i18n.DEFAULT_LANGUAGE][key], (code, key)


def test_partial_languages_still_fall_back_for_untranslated_keys():
    """未覆盖的键（如语料字段标签）仍回落 zh-Hans——可见降级，不是空白。"""
    for code in ("en", "fr", "de", "ja", "ko"):
        assert "field.corpus" not in i18n.CATALOGS[code], code
        assert (i18n.Translator(code).t("field.corpus")
                == i18n.CATALOGS[i18n.DEFAULT_LANGUAGE]["field.corpus"])


# ---------- LLM 语言指令 ----------

def test_llm_directive_non_empty_for_all_seven_and_names_language():
    for code, native in i18n.LANGUAGES.items():
        line = i18n.llm_language_directive(code)
        assert isinstance(line, str) and line.strip()
        assert "\n" not in line
        assert native in line, (code, native, line)


def test_llm_directive_zh_hans_is_the_canonical_line():
    assert i18n.llm_language_directive("zh-Hans") == "你将使用简体中文回答。"


def test_llm_directive_unknown_language_falls_back_not_raises():
    line = i18n.llm_language_directive("klingon")
    assert isinstance(line, str) and line.strip()
    assert line == i18n.llm_language_directive(i18n.DEFAULT_LANGUAGE)


def test_llm_directive_is_stable():
    for code in i18n.LANGUAGES:
        assert i18n.llm_language_directive(code) == i18n.llm_language_directive(code)
