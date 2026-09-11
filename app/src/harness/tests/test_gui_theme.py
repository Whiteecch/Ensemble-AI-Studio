"""配色主题（theme.py）契约：4 套主题、选择器齐备、色表自洽、对比度达标、无 AI 味。

纯函数级用例：不 import PySide6（theme 是纯字符串/纯数据），离屏或纯 CLI 都能跑。
锁定：
  · THEMES 恰好是 默认/深色/白色/深蓝 四个名字；PALETTES 覆盖同集。
  · 每套主题的 QSS 非空且含界面**全部**在用选择器（换主题不能悄悄丢样式）。
  · `默认` 是 §3.6 的冷调「仪器」浅色（**不再是旧的暖纸色**：旧值 #eeebe5/#0f766e 已换掉）。
  · 样式表里只准出现**该主题色表**里的颜色——深色主题漏一个 #ffffff 就是白底黑字。
  · 未知名回退 `默认`（字符串相等，不抛异常）。
  · 每个 Palette 字段无 None、格式正确；正文 on 底色、强调文字 on 强调色 的对比 ≥ 4.5
    （WCAG AA 正文）；新语义色（选中行、危险动作）同样过 AA。
  · theme.py / library.py 里**不留 emoji**（§3.6 无 AI 味）。
"""
import re
from pathlib import Path

import pytest

from harness.gui import theme as theme_mod
from harness.gui.theme import (
    PALETTES, SPEAKER_COLORS, THEMES, Palette, dialog_qss, palette_for,
    speaker_colors_for, theme_qss)

#: 界面在用的选择器（少一个都算掉样式）。
REQUIRED_SELECTORS = [
    "QMainWindow",
    "QWidget#AppRoot",
    "QLabel#cardTitle",
    "QLabel#section",
    "QFrame#card",
    "QFrame#hairline",
    "QTextBrowser#conversation",
    "QTextBrowser#thinklog",
    "QLineEdit#opening",
    "QLineEdit#chat",
    "QLineEdit#chat:focus",
    "QPushButton#primary",
    "QPushButton#primary:hover",
    "QPushButton#primary:pressed",
    "QPushButton#primary:disabled",
    "QPushButton#ghost",
    "QPushButton#ghost:hover",
    "QPushButton#ghost:disabled",
    "QScrollArea",
    "QSplitter::handle",
    "QScrollBar:vertical",
    "QScrollBar::handle:vertical",
    "QScrollBar:horizontal",
    "QScrollBar::add-line",
    "QMenuBar",
    "QMenu::item",
    "QMenu::item:selected",
    "QToolTip",
]

#: 弹窗（library 各编辑器/场景库 + main_window 各弹窗）真的会用到的控件类型。
#: 任务口径：QDialog/QGroupBox/列表/表格/数值框/combo（含弹出列表）/勾选/滑杆/滚动条/
#: 工具按钮/菜单/Tab/文本域/按钮变体/Label#cardTitle·#section——一处都不能漏。
DIALOG_REQUIRED_SELECTORS = [
    "QDialog",
    "QLabel#hint",
    "QLabel#error",
    "QLabel#sect",
    "QLabel#mono",
    "QGroupBox",
    "QGroupBox::title",
    "QLineEdit",
    "QPlainTextEdit",
    "QTextEdit",
    "QListWidget",
    "QListView",
    "QListWidget::item",
    "QListWidget::item:hover",
    "QListWidget::item:selected",
    "QTableWidget",
    "QTableView",
    "QHeaderView::section",
    "QSpinBox",
    "QDoubleSpinBox",
    "QComboBox",
    "QComboBox QAbstractItemView",
    "QCheckBox::indicator:checked",
    "QRadioButton::indicator:unchecked",
    "QSlider::groove:horizontal",
    "QSlider::handle:horizontal",
    "QScrollBar:vertical",
    "QToolButton",
    "QToolButton:hover",
    "QTabWidget::pane",
    "QTabBar::tab:selected",
    "QPushButton#primary",
    "QPushButton#ghost",
    "QPushButton#danger",
    "QPushButton#danger:hover",
    "QMessageBox",
]

#: 颜色字段之外的非颜色字段（值不是 #rrggbb）。
NON_COLOUR_FIELDS = {"mono_font"}

#: §3.6 新增的字段（都要有，且四套主题都给）。
NEW_FIELDS = ("hairline", "divider", "selection_bg", "selection_text",
              "danger", "log_highlight", "mono_font")

#: 旧「暖纸」默认配色：一个都不许留在 `默认` 里（视觉语言已统一到冷调仪器版）。
_OLD_WARM_PAPER = ("#eeebe5", "#0f766e", "#e7e2d6", "#faf7f1", "#f3f0ea",
                   "#37332e", "#8b8577")

#: emoji/装饰符号扫描（§3.6 无 AI 味）：几何符号与杂项符号区 + 变体选择符 + 彩色 emoji。
#: **不含**箭头（→，U+2192）与带圈数字（①②）——它们在文档与被注释的界面里是排版符号。
_EMOJI_RE = re.compile(
    "[⌀-⏿☀-➿⬀-⯿️\U0001F000-\U0001FAFF]")

_HEX_RE = re.compile(r"#[0-9a-fA-F]{6}")


def _gui_path(name: str) -> Path:
    return Path(theme_mod.__file__).resolve().parent / name


def _channel(value: float) -> float:
    """sRGB 单通道（0..1）→ 线性光（WCAG 2.x 定义）。"""
    return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4


def _luminance(hex_color: str) -> float:
    """`#rrggbb` → 相对亮度（0 全黑，1 全白）。"""
    h = hex_color.lstrip("#")
    assert len(h) == 6, f"颜色须为 #rrggbb：{hex_color!r}"
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def _contrast(fg: str, bg: str) -> float:
    """两色亮度对比比（1..21）。"""
    a, b = _luminance(fg), _luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


# ------------------------------------------------------------------ 主题清单

def test_themes_are_exactly_the_four_spec_names():
    assert THEMES == ["默认", "深色", "白色", "深蓝"]


def test_palettes_cover_every_theme_and_nothing_else():
    assert set(PALETTES) == set(THEMES)
    assert len(PALETTES) == 4


# ------------------------------------------------------------------ 样式表

def test_every_theme_yields_nonempty_qss_with_all_selectors():
    for theme in THEMES:
        qss = theme_qss(theme)
        assert qss.strip(), f"{theme} 主题样式表为空"
        for sel in REQUIRED_SELECTORS:
            assert sel in qss, f"{theme} 主题缺选择器 {sel}"


def test_qss_contains_no_unfilled_template_placeholders():
    """模板漏填会留下 `{field}` 这类占位符——表现为 Qt 静默忽略该条规则。

    注：QSS 花括号本身合法（`QLabel { … }`），只查**紧贴标识符**的 `{name}`。
    """
    for theme in THEMES:
        leftover = re.search(r"\{[a-z_][a-z0-9_]*\}", theme_qss(theme))
        assert leftover is None, f"{theme} 主题样式表残留占位符 {leftover.group(0) if leftover else ''}"


def test_default_theme_is_the_new_cool_instrument_light():
    """`默认` 已换成 §3.6 的冷调「仪器」浅色：近白面 + 发丝线 + 单一去饱和蓝青强调。

    （这是**有意**替换旧暖纸观感的一处断言变更：旧值 #eeebe5 窗口底 / #0f766e 强调
    已被冷调版取代，换主题不再等于「不改默认观感」。）
    """
    qss = theme_qss("默认")
    assert "#fbfbfc" in qss, "默认主题窗口底应是近白的冷调 #fbfbfc"
    assert "#2f6f7e" in qss, "默认主题强调色应是去饱和蓝青 #2f6f7e"


def test_default_palette_matches_the_new_visual_language():
    """默认色表逐项锁定（改色即改观感，故这里钉死；理由见字段注释与文档 §3.6）。"""
    p = PALETTES["默认"]
    assert p.window_bg == "#fbfbfc"
    assert p.card_bg == "#ffffff"
    assert p.card_border == "#e6e8ec"
    assert p.accent == "#2f6f7e"
    assert p.conversation_bg == "#fbfbfc"
    assert p.thinklog_bg == "#f5f6f8"
    assert p.text == "#1c1f23"
    assert p.muted_text == "#6b7280"


def test_default_palette_dropped_every_warm_paper_colour():
    """旧「暖纸」配色的每一枚都不许留在 `默认` 里（否则视觉语言就地分叉）。"""
    p = PALETTES["默认"]
    values = {getattr(p, name) for name in Palette.__dataclass_fields__}
    for old in _OLD_WARM_PAPER:
        assert old not in values, f"默认主题还留着旧暖纸色 {old}"


def test_default_theme_uses_a_cool_accent_not_the_old_teal():
    """强调色只有一个且是冷色：旧 #0f766e（偏暖青绿）不再出现在任何主题的强调位上。"""
    for theme, p in PALETTES.items():
        assert p.accent != "#0f766e", theme
        assert p.accent != p.text, theme


def test_unknown_theme_falls_back_to_default():
    assert theme_qss("不存在的主题") == theme_qss("默认")
    assert theme_qss("") == theme_qss("默认")


def test_distinct_themes_produce_distinct_stylesheets():
    """四套配色不能是同一张表——否则「可切换」是假的。"""
    seen = {theme_qss(t) for t in THEMES}
    assert len(seen) == len(THEMES)


# ------------------------------------------------------------------ 色表取用（G1）

def test_palette_for_returns_the_palette_and_falls_back_for_unknown():
    """取色表的唯一入口：认识的给该主题，不认识的给 `默认`（不抛）。"""
    for theme in THEMES:
        assert palette_for(theme) is PALETTES[theme]
    assert palette_for("不存在的主题") is PALETTES["默认"]
    assert palette_for("") is PALETTES["默认"]


def test_every_theme_has_a_speaker_palette_anchored_on_its_own_accent():
    """说话者配色盘**按主题**给（界面按演员表顺序取色，超长回绕）：四套主题各一套。

    每套的首枚必须是该主题自己的 accent（视觉锚点：说「这场的第一个人」时用该主题的色），
    同一套里不许有重复色（相邻两人不得同色）。
    """
    for theme in THEMES:
        pal = speaker_colors_for(theme)
        assert len(pal) >= 4, f"{theme} 说话者色太少：{pal}"
        assert pal[0] == PALETTES[theme].accent, f"{theme} 首枚应是该主题强调色"
        assert len(set(pal)) == len(pal), f"{theme} 说话者色有重复：{pal}"
        for colour in pal:
            assert _HEX_RE.fullmatch(colour), (theme, colour)


def test_speaker_colours_are_readable_under_their_themes_accent_text():
    """头像圆底 = 说话者色，圆底上的首字母 = 该主题的 accent_text → 四套全过 AA（≥ 4.5）。"""
    for theme, palette in PALETTES.items():
        for colour in speaker_colors_for(theme):
            ratio = _contrast(palette.accent_text, colour)
            assert ratio >= 4.5, f"{theme} 头像字/{colour} 对比仅 {ratio:.2f}"


def test_dark_speaker_colours_are_brightened_not_the_light_set():
    """深底两套不能复用浅底那批深饱和色（近黑底上深字读不清）——两族色值不相交。"""
    light = set(speaker_colors_for("默认")) | set(speaker_colors_for("白色"))
    dark = set(speaker_colors_for("深色")) | set(speaker_colors_for("深蓝"))
    assert not (light & dark), f"深浅两族共用色：{sorted(light & dark)}"


def test_speaker_colours_fall_back_to_default_for_unknown_theme():
    assert speaker_colors_for("不存在的主题") == speaker_colors_for("默认")
    assert speaker_colors_for("") == speaker_colors_for("默认")
    assert "默认" in SPEAKER_COLORS and set(SPEAKER_COLORS) == set(THEMES)


def test_chip_text_on_chip_bg_reaches_wcag_aa():
    """chip 对（模型药丸的 stub 底色/字色）也要能读——它是新用途，不能只保证强调对。"""
    for theme, palette in PALETTES.items():
        ratio = _contrast(palette.chip_text, palette.chip_bg)
        assert ratio >= 4.5, f"{theme} chip 字/底对比仅 {ratio:.2f}"


def test_palette_defines_the_content_roles_the_ui_paints_programmatically():
    """界面程序化配色的每一类（正文/时刻戳/叙述/链接/导演/警示/进度条槽）都要有语义色。

    这些颜色原先硬编码在 main_window 里（BODY_COLOR/TIME_COLOR/…），切主题时内容不跟着
    变——深色底上仍印着默认配色的深字。故它们必须是色表的字段。
    """
    for role in ("narrator_text", "director_text", "time_text", "link_text",
                 "warn_text", "bar_track", "error_text"):
        assert role in Palette.__dataclass_fields__, role
        for theme, palette in PALETTES.items():
            assert getattr(palette, role).startswith("#"), (theme, role)


def test_default_content_colours_are_the_new_cool_set():
    """`默认` 的正文/时刻戳/叙述色也随视觉语言一起换成冷调（旧暖纸值已删）。

    旧值（#37332e 正文 / #b3aca0 时刻戳 / #8a8578 叙述 / #9a948a 导演 / #9c9689 链接 /
    #dce7e5 气泡）在 `默认` 里一个都不该剩——它们与新的近白冷底不是一套语言。
    """
    p = PALETTES["默认"]
    assert p.text == "#1c1f23"
    assert p.time_text == "#9aa1aa"
    assert p.narrator_text == "#7c838d"
    assert p.director_text == "#8b919b"
    assert p.link_text == "#4b7f8c"
    assert p.human_bubble_bg == "#e3ebed" and p.human_bubble_edge == "#cdd9dd"
    for old in ("#37332e", "#b3aca0", "#8a8578", "#9a948a", "#9c9689",
                "#dce7e5", "#c6d9d6"):
        assert old not in {getattr(p, f) for f in Palette.__dataclass_fields__}, old


def test_dark_palettes_keep_body_text_readable_on_dark_surfaces():
    """深色/深蓝：正文与时刻戳在窗口底/对白底上都要够亮（否则转录不可读）。"""
    for theme in ("深色", "深蓝"):
        p = PALETTES[theme]
        assert _contrast(p.text, p.conversation_bg) >= 4.5, theme
        assert _contrast(p.muted_text, p.conversation_bg) >= 3.0, theme
        assert _contrast(p.text, p.card_bg) >= 4.5, theme


# ------------------------------------------------------------------ 弹窗样式表（G1）

def test_dialog_qss_carries_theme_colours_and_every_dialog_selector():
    """弹窗样式表由主题表 + 色表拼出：四个主题各不相同，且都含**全部**在用选择器。"""
    sheets = {}
    for theme in THEMES:
        qss = dialog_qss(theme)
        sheets[theme] = qss
        assert qss.strip(), theme
        for sel in DIALOG_REQUIRED_SELECTORS:
            assert sel in qss, f"{theme} 弹窗样式缺选择器 {sel}"
        # 该主题的独有色要真的出现在弹窗表里（否则换主题弹窗不变色）
        palette = PALETTES[theme]
        assert palette.card_bg in qss and palette.text in qss
    assert len(set(sheets.values())) == len(THEMES), "四套弹窗样式不能是同一张表"


def test_qss_only_ever_uses_colours_from_its_own_palette():
    """样式表里出现的每一枚 `#rrggbb` 都必须来自**该主题色表**。

    这是「深色主题不会漏出白底黑字」的根因级守门：只要某处写死了字面颜色（旧实现里
    各弹窗各自硬编码一份浅色样式），这条就会红——换主题时那个控件不会跟着变色。
    """
    for theme, palette in PALETTES.items():
        allowed = {getattr(palette, f) for f in Palette.__dataclass_fields__
                   if f not in NON_COLOUR_FIELDS}
        for name, qss in (("主表", theme_qss(theme)), ("弹窗表", dialog_qss(theme))):
            stray = sorted(set(_HEX_RE.findall(qss)) - allowed)
            assert stray == [], f"{theme} {name} 里写了不属于该主题的颜色：{stray}"


@pytest.mark.parametrize("theme", ["深色", "深蓝"])
def test_dark_themes_carry_no_light_leftovers(theme):
    """深色两套：样式表里不许出现纯白/近白（否则就是白底黑字的老毛病）。"""
    for qss in (theme_qss(theme), dialog_qss(theme)):
        low = qss.lower()
        for bad in ("#ffffff", "#fff", "#fefefe", "#fbfbfc", "#ffffff "):
            assert bad not in low, f"{theme} 主题样式表残留浅色 {bad}"


def test_dialog_qss_has_no_unfilled_placeholders_and_falls_back():
    for theme in THEMES:
        leftover = re.search(r"\{[a-z_][a-z0-9_]*\}", dialog_qss(theme))
        assert leftover is None, f"{theme} 弹窗样式残留占位符 {leftover}"
    assert dialog_qss("不存在的主题") == dialog_qss("默认")


# ------------------------------------------------------------------ 色表完整性

def test_every_palette_defines_every_field_without_none():
    fields = [f for f in Palette.__dataclass_fields__]
    for theme, palette in PALETTES.items():
        for name in fields:
            value = getattr(palette, name)
            assert value is not None, f"{theme} 主题字段 {name} 为 None"
            assert isinstance(value, str) and value.strip(), f"{theme} 主题字段 {name} 为空"


def test_every_colour_is_wellformed_hex():
    """颜色字段一律 `#rrggbb`（`mono_font` 是字族、不是颜色，按 NON_COLOUR_FIELDS 跳过）。"""
    for theme, palette in PALETTES.items():
        for name in Palette.__dataclass_fields__:
            if name in NON_COLOUR_FIELDS:
                continue
            value = getattr(palette, name)
            assert value.startswith("#") and len(value) == 7, f"{theme}.{name}={value!r}"
            int(value[1:], 16)          # 全是十六进制数字


def test_every_theme_defines_the_new_visual_language_fields():
    """§3.6 新增字段（细线/分隔/选中/危险/浅红高亮/等宽字族）四套主题都得给。"""
    for field in NEW_FIELDS:
        assert field in Palette.__dataclass_fields__, f"Palette 缺字段 {field}"
    for theme, palette in PALETTES.items():
        for field in NEW_FIELDS:
            assert getattr(palette, field).strip(), f"{theme} 缺 {field}"
        # 等宽字族：必须是「等宽」的兜底链，否则数值排版退回比例字体
        assert "monospace" in palette.mono_font, theme
        # 语义色要有区分度：细线比卡片描边轻、分隔比细线重（否则层级糊成一团）
        assert palette.hairline != palette.divider, theme


# ------------------------------------------------------------------ 无 AI 味（§3.6）

@pytest.mark.parametrize("name", ["theme.py", "library.py"])
def test_no_emoji_in_theme_or_library_source(name):
    """§3.6「无 AI 味」：界面源码里**不许有 emoji**（⚠/✅/💡/📄 之类）。

    箭头（→）与带圈数字（①②…）是排版符号，不在扫描范围内——它们在 docstring 里用得很多。
    """
    hits: list[str] = []
    for i, line in enumerate(_gui_path(name).read_text(encoding="utf-8").splitlines(), 1):
        found = _EMOJI_RE.findall(line)
        if found:
            hits.append(f"{name}:{i} {''.join(found)!r}")
    assert hits == [], "源码里还有 emoji：" + "; ".join(hits)


# ------------------------------------------------------------------ 可读性

def test_body_text_contrast_on_window_bg_reaches_wcag_aa():
    for theme, palette in PALETTES.items():
        ratio = _contrast(palette.text, palette.window_bg)
        assert ratio >= 4.5, f"{theme} 正文/窗口底对比仅 {ratio:.2f}（需 ≥ 4.5）"


def test_accent_text_contrast_on_accent_reaches_wcag_aa():
    for theme, palette in PALETTES.items():
        ratio = _contrast(palette.accent_text, palette.accent)
        assert ratio >= 4.5, f"{theme} 强调文字/强调色对比仅 {ratio:.2f}（需 ≥ 4.5）"


def test_ink_on_surface_reaches_wcag_aa_on_all_four():
    """四套主题统一口径：正文 on 窗口底/卡片面 都要 ≥ 4.5（不只深色那两套）。"""
    for theme, palette in PALETTES.items():
        for surface in ("window_bg", "card_bg", "conversation_bg", "thinklog_bg"):
            ratio = _contrast(palette.text, getattr(palette, surface))
            assert ratio >= 4.5, f"{theme} 正文/{surface} 对比仅 {ratio:.2f}"


def test_selection_and_danger_colours_are_readable():
    """新语义色也要能读：选中行文字 on 选中行底、危险动作字色 on 卡片面。"""
    for theme, palette in PALETTES.items():
        sel = _contrast(palette.selection_text, palette.selection_bg)
        assert sel >= 4.5, f"{theme} 选中行文字/底对比仅 {sel:.2f}"
        danger = _contrast(palette.danger, palette.card_bg)
        assert danger >= 4.5, f"{theme} danger/卡片面对比仅 {danger:.2f}"
