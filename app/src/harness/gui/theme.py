"""可切换配色主题（spec §2.1① 配色：默认 / 深色 / 白色 / 深蓝）。

本模块**纯字符串/纯数据**：不 import PySide6、不读盘、不联网——离屏或纯 CLI 环境
都能拿来算色表与样式表；只有界面真正 setStyleSheet 时才需要 Qt。

四个概念：
  · Palette   一个主题的全部语义色（window/card/text/accent/…），dataclass 不可变字段。
  · PALETTES  主题名 → Palette；四套主题是同一套视觉语言（科技感·简约·高级·无 AI 味，
              见《界面与场景自由度_优化文档》§3.6）的四种取值：一个冷色强调 + 中性灰阶，
              细线分隔、方一点的小圆角、留白到位；`默认` 是其中的浅色「仪器」版。
  · SPEAKER_COLORS  主题名 → 说话者配色盘（有序、可回绕）：界面据此给每个说话人上色
              （对白色点 / 冲动条 chunk / 头像圆底），首枚即该主题的 accent，故说话者色
              也**随主题换**、不留旧主题的字面色（见 speaker_colors_for）。
  · theme_qss 主题名 → 整份 Qt 样式表（复刻界面已在用的 objectName 选择器）。

未知名回退：`theme_qss("不存在")` 返回 `默认` 的样式表（**不改写**传入名，只按默认出表），
故调用方从配置里读到脏值也不会抛异常、界面照常可用。

样式表纪律：模板里**只准出现色表字段**，绝不写字面颜色——否则深色主题会漏出白底黑字
（`tests/test_gui_theme.py` 会扫 `#ffffff` 这类残留把这件事钉住）。
"""
from __future__ import annotations

from dataclasses import dataclass

#: 可选主题名（顺序即界面下拉/循环顺序）。
THEMES: list[str] = ["默认", "深色", "白色", "深蓝"]

#: 未知名回退目标。
_DEFAULT_THEME = "默认"

#: 等宽字族（数值一律用它排版：权重/时刻/计数读起来「像仪器」）。
MONO_FONT = '"Menlo", "Consolas", "DejaVu Sans Mono", monospace'


@dataclass(frozen=True)
class Palette:
    """一个主题的语义色（颜色字段全部为 `#rrggbb` 小写十六进制串）。

    命名对齐界面用途：
      window_bg        整窗/根容器底色       card_bg / card_border  卡片面与描边
      text / muted_text  正文与次要文字      accent / accent_hover  强调色与其悬停
      accent_text      强调色上的文字（按钮前景，须与 accent 有足够对比）
      subtle_bg        轻底色（ghost 悬停、条槽等） conversation_bg / thinklog_bg  中栏对白/日志底
      input_bg / input_border  输入框        chip_bg / chip_text    小标签
      human_bubble_bg / human_bubble_edge    真人发言气泡与其描边
      scrollbar_bg     滚动条槽
      （§3.6 新增，视觉语言落地用）
      hairline         1px 细线（卡面/列表描边、分隔条）——比 card_border 更轻，替代厚边框
      divider          稍重的分隔线（表头下沿、滑轨、悬停描边）
      selection_bg / selection_text          列表/表格选中行（**浅底 + 常用字色**，不铺色块）
      danger           破坏性动作（删除）的描边与字色
      log_highlight    场景变更日志行的浅红底（§3.5）
      mono_font        等宽字族（非颜色字段）
    """

    window_bg: str
    card_bg: str
    card_border: str
    text: str
    muted_text: str
    accent: str
    accent_hover: str
    accent_text: str
    subtle_bg: str
    conversation_bg: str
    thinklog_bg: str
    input_bg: str
    input_border: str
    chip_bg: str
    chip_text: str
    human_bubble_bg: str
    human_bubble_edge: str
    scrollbar_bg: str
    # ---- 内容角色色（对白 HTML 与程序化配色的标签用；原先硬编码在 main_window）----
    narrator_text: str      # 场景叙述（居中斜体弱化）
    director_text: str      # 导演/世界事件（灰斜体）
    time_text: str          # 每句前的 HH:MM:SS 时刻戳
    link_text: str          # 叙述行尾「撤销 · 改写」行内链接
    warn_text: str          # 警示小字（禁言徽标、场景诊断）
    error_text: str         # 错误红字（弹窗里的 QLabel#error）
    bar_track: str          # 数值条（QProgressBar）槽底色
    # ---- §3.6 视觉语言新增（追加在末尾：既有字段名一个不动）----
    hairline: str           # 1px 细线：卡面/输入框/列表描边、分隔条
    divider: str            # 稍重的分隔线：表头下沿、滑轨、悬停描边
    selection_bg: str       # 列表/表格选中行底色（浅色一档，不做色块）
    selection_text: str     # 选中行文字
    danger: str             # 破坏性动作（删除…）的字色与描边
    log_highlight: str      # 场景变更日志行的浅红底（§3.5）
    mono_font: str          # 等宽字族（数值字段用；**不是颜色**）


#: 默认——干净的冷调「仪器」浅色：近白面 + 发丝线 + 单一去饱和蓝青强调。
#: （不再是旧版的暖纸色；旧值见 git 历史，2026-09 §3.6 视觉语言统一时替换。）
_DEFAULT = Palette(
    window_bg="#fbfbfc",
    card_bg="#ffffff",
    card_border="#e6e8ec",
    text="#1c1f23",
    muted_text="#6b7280",
    accent="#2f6f7e",
    accent_hover="#265b67",
    accent_text="#ffffff",
    subtle_bg="#f2f3f5",
    conversation_bg="#fbfbfc",
    thinklog_bg="#f5f6f8",
    input_bg="#ffffff",
    input_border="#d7dbe1",
    chip_bg="#f1f3f5",
    chip_text="#4b5563",
    human_bubble_bg="#e3ebed",
    human_bubble_edge="#cdd9dd",
    scrollbar_bg="#ebedf0",
    narrator_text="#7c838d", director_text="#8b919b", time_text="#9aa1aa",
    link_text="#4b7f8c", warn_text="#a16207", error_text="#b42318",
    bar_track="#eceef1",
    hairline="#e8eaee", divider="#dde1e7",
    selection_bg="#e7eff1", selection_text="#1c1f23",
    danger="#b42318", log_highlight="#fdecea", mono_font=MONO_FONT,
)

#: 深色——近黑的蓝灰底（科技暗色）+ 同族冷色强调，正文亮灰；强调色上压深字（对比 ≥ 4.5）。
_DARK = Palette(
    window_bg="#15171b",
    card_bg="#1b1e23",
    card_border="#2a2e35",
    text="#e8eaed",
    muted_text="#9aa3ad",
    accent="#4f9fb0",
    accent_hover="#63b3c4",
    accent_text="#08222a",
    subtle_bg="#22262c",
    conversation_bg="#171a1f",
    thinklog_bg="#15181c",
    input_bg="#1b1e23",
    input_border="#333941",
    chip_bg="#232830",
    chip_text="#aab3bd",
    human_bubble_bg="#1e2f37",
    human_bubble_edge="#2c4450",
    scrollbar_bg="#22262c",
    narrator_text="#9aa3ad", director_text="#8d959f", time_text="#7f8b98",
    link_text="#6fb0bf", warn_text="#e0a33a", error_text="#f08a8a",
    bar_track="#22262c",
    hairline="#262a31", divider="#30353d",
    selection_bg="#24343c", selection_text="#e8eaed",
    danger="#f08a8a", log_highlight="#3a1f22", mono_font=MONO_FONT,
)

#: 白色——白面高键版：更强的发丝线（白底上要看得出边界），蓝青强调略偏冷。
_LIGHT = Palette(
    window_bg="#ffffff",
    card_bg="#ffffff",
    card_border="#d8dce2",
    text="#111827",
    muted_text="#5f6673",
    accent="#17627f",
    accent_hover="#12516a",
    accent_text="#ffffff",
    subtle_bg="#f4f6f9",
    conversation_bg="#fdfdfe",
    thinklog_bg="#f7f9fb",
    input_bg="#ffffff",
    input_border="#ccd2da",
    chip_bg="#eef2f7",
    chip_text="#3f4a58",
    human_bubble_bg="#e8f0f7",
    human_bubble_edge="#cfdceb",
    scrollbar_bg="#e8ebef",
    narrator_text="#6d7583", director_text="#7d8695", time_text="#8f98a6",
    link_text="#2b6f8f", warn_text="#8a5a06", error_text="#a4161a",
    bar_track="#eef1f5",
    hairline="#e4e7ec", divider="#d3d8df",
    selection_bg="#e6eef5", selection_text="#111827",
    danger="#a4161a", log_highlight="#fdf0f1", mono_font=MONO_FONT,
)

#: 深蓝——深夜蓝底 + 同族天蓝强调，正文淡蓝白；强调色上压近黑字（对比 ≥ 4.5）。
_NAVY = Palette(
    window_bg="#0d1626",
    card_bg="#132039",
    card_border="#1f3350",
    text="#dbe6f5",
    muted_text="#8ba0bd",
    accent="#54b6d8",
    accent_hover="#6fc6e4",
    accent_text="#06131f",
    subtle_bg="#17273f",
    conversation_bg="#101c30",
    thinklog_bg="#0e192b",
    input_bg="#132039",
    input_border="#273d5c",
    chip_bg="#1b2e4b",
    chip_text="#9dc0e6",
    human_bubble_bg="#1a3457",
    human_bubble_edge="#294a75",
    scrollbar_bg="#17273f",
    narrator_text="#9db0c7", director_text="#8ea3bd", time_text="#7f95b0",
    link_text="#6fbfe0", warn_text="#e2a83c", error_text="#f2908f",
    bar_track="#17273f",
    hairline="#1b2c45", divider="#233855",
    selection_bg="#1c3557", selection_text="#dbe6f5",
    danger="#f2908f", log_highlight="#3a2028", mono_font=MONO_FONT,
)

#: 主题名 → 色表（键与 THEMES 同序同集）。
PALETTES: dict[str, Palette] = {
    "默认": _DEFAULT,
    "深色": _DARK,
    "白色": _LIGHT,
    "深蓝": _NAVY,
}

#: 浅底两套（默认/白色）共用的说话者色相尾段：深饱和、印在近白底上——配主题的
#: accent_text（白）对比 ≥ 4.5。数值一律压暗到「白字读得清」，不用高亮荧光色。
_LIGHT_SPEAKER_TAIL: tuple[str, ...] = (
    "#6d28d9", "#b45309", "#be123c", "#0369a1", "#4f46e5", "#15803d", "#9d174d")

#: 深底两套（深色/深蓝）共用的说话者色相尾段：同族**提亮**——配主题的 accent_text
#: （近黑）对比 ≥ 4.5；深浅两版不是同一批色（深底印深饱和色等于深字压深底）。
_DARK_SPEAKER_TAIL: tuple[str, ...] = (
    "#c4b5fd", "#fcd34d", "#fda4af", "#7dd3fc", "#a5b4fc", "#86efac", "#f0abfc")

#: 说话者配色盘（主题名 → 有序色表）：界面按演员表顺序给说话人上色（对白色点 / 左栏
#: 冲动条 chunk / 右栏头像圆底），超过盘长**取模回绕**（见 main_window.color_for）。
#: 每套的**第一枚就是该主题自己的 accent**（视觉锚点），其余几枚各自换色相、保持明度，
#: 故四套主题各说各的语言、且同一套里相邻两人颜色必然不同（盘长 ≥ 2）。
#: 色值只在这里定义（theme.py 是界面**唯一**的取色处）：界面侧一律经 speaker_colors_for()。
SPEAKER_COLORS: dict[str, tuple[str, ...]] = {
    "默认": (_DEFAULT.accent, *_LIGHT_SPEAKER_TAIL),
    "白色": (_LIGHT.accent, *_LIGHT_SPEAKER_TAIL),
    "深色": (_DARK.accent, *_DARK_SPEAKER_TAIL),
    "深蓝": (_NAVY.accent, "#a5b4fc", "#fcd34d", "#fda4af", "#7dd3fc", "#c4b5fd",
             "#86efac", "#f0abfc"),
}


def speaker_colors_for(theme: str) -> tuple[str, ...]:
    """主题名 → 该主题的说话者配色盘；未知名（或 None）回退 `默认`——与 palette_for 同口径。

    返回**元组**（不可变）：调用方要改就自己 list() 一份，绝不会串改到全局色表。
    """
    return SPEAKER_COLORS.get(theme, SPEAKER_COLORS[_DEFAULT_THEME])


#: 样式表模板：`{}` 处按 Palette 填充。选择器与界面已有 objectName 一一对应，
#: 不新增/不改名——旧 QSS 能选中的，这份也能选中。
#: 视觉语言（§3.6）：细线优先于盒子、方一点的小圆角（6/8px）、悬停与选中用浅底而非色块。
_QSS_TEMPLATE = """\
QMainWindow, QWidget#AppRoot {{ background: {window_bg}; }}
QLabel#cardTitle {{ font-size:11px; font-weight:700; color:{muted_text}; letter-spacing:2px; }}
QLabel#section {{ font-size:12px; font-weight:600; color:{text}; }}
QFrame#card {{ background-color:{card_bg}; border:1px solid {hairline}; border-radius:8px; }}
QFrame#hairline {{ background:{hairline}; border:none; max-height:1px; }}
QTextBrowser#conversation {{
    background-color:{conversation_bg}; border:1px solid {hairline}; border-radius:8px;
    font-size:14px; color:{text}; selection-background-color:{selection_bg};
    selection-color:{selection_text};
}}
QTextBrowser#thinklog {{
    background-color:{thinklog_bg}; border:1px solid {hairline}; border-radius:8px;
    font-size:13px; color:{text}; selection-background-color:{selection_bg};
    selection-color:{selection_text};
}}
QLineEdit#chat {{
    background:{input_bg}; border:1px solid {input_border}; border-radius:6px;
    padding:8px 11px; font-size:13px; color:{text};
    selection-background-color:{selection_bg}; selection-color:{selection_text};
}}
QLineEdit#chat:focus {{ border:1px solid {accent}; }}
QLineEdit#opening {{
    background:{input_bg}; border:1px solid {input_border}; border-radius:6px;
    padding:5px 8px; font-size:13px; color:{text};
}}
QLineEdit#opening:focus {{ border:1px solid {accent}; }}
QPushButton#primary {{
    background:{accent}; color:{accent_text}; border:1px solid {accent}; border-radius:6px;
    padding:8px 16px; font-weight:600;
}}
QPushButton#primary:hover {{ background:{accent_hover}; border-color:{accent_hover}; }}
QPushButton#primary:pressed {{ background:{accent_hover}; border-color:{accent_hover}; }}
QPushButton#primary:disabled {{ color:{muted_text}; background:{subtle_bg}; border-color:{hairline}; }}
QPushButton#ghost {{
    background:{card_bg}; color:{text}; border:1px solid {input_border}; border-radius:6px;
    padding:7px 12px; font-size:13px;
}}
QPushButton#ghost:hover {{ background:{subtle_bg}; border-color:{divider}; }}
QPushButton#ghost:checked {{ background:{subtle_bg}; border-color:{accent}; }}
QPushButton#ghost:disabled {{ color:{muted_text}; background:{subtle_bg}; border-color:{hairline}; }}
QScrollArea {{ border:none; background:transparent; }}
QScrollArea > QWidget > QWidget {{ background:transparent; }}
QSplitter::handle {{ background:{hairline}; }}
QSplitter::handle:horizontal {{ width:3px; }}
QSplitter::handle:vertical {{ height:3px; }}
QScrollBar:vertical {{
    background:{scrollbar_bg}; width:10px; margin:0; border:none; border-radius:5px;
}}
QScrollBar::handle:vertical {{ background:{card_border}; min-height:24px; border-radius:5px; }}
QScrollBar::handle:vertical:hover {{ background:{muted_text}; }}
QScrollBar:horizontal {{
    background:{scrollbar_bg}; height:10px; margin:0; border:none; border-radius:5px;
}}
QScrollBar::handle:horizontal {{ background:{card_border}; min-width:24px; border-radius:5px; }}
QScrollBar::handle:horizontal:hover {{ background:{muted_text}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width:0; height:0; background:none; border:none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background:none; }}
QToolTip {{
    background:{card_bg}; color:{text}; border:1px solid {hairline};
    padding:4px 6px; font-size:12px;
}}
QMenuBar {{ background:{window_bg}; color:{text}; border:none; }}
QMenuBar::item {{ background:transparent; color:{text}; padding:5px 10px; }}
QMenuBar::item:selected {{ background:{subtle_bg}; }}
QMenu {{ background:{card_bg}; border:1px solid {hairline}; padding:4px; color:{text}; }}
QMenu::item {{ padding:5px 18px 5px 14px; border-radius:4px; }}
QMenu::item:selected {{ background:{subtle_bg}; color:{text}; }}
QMenu::item:disabled {{ color:{muted_text}; }}
QMenu::separator {{ height:1px; background:{hairline}; margin:4px 6px; }}
"""


def palette_for(theme: str) -> Palette:
    """主题名 → 色表；未知名回退到 `默认`（不抛异常，也不改写传入名）。

    界面里**所有**程序化配色（对白 HTML 的正文/时刻戳/叙述/链接色、左右栏标签、
    数值条槽底…）都从这里取色——它们原先硬编码成 `默认` 的几枚十六进制串，切主题时
    不会跟着变（深底上仍是深字）。
    """
    return PALETTES.get(theme, PALETTES[_DEFAULT_THEME])


def theme_qss(theme: str) -> str:
    """返回该主题的**完整** Qt 样式表；未知名回退到 `默认`（不抛异常）。"""
    return _QSS_TEMPLATE.format(**vars(palette_for(theme)))


#: 弹窗专属规则（对话框底 + 表单/列表/表格/分组框/数值控件/按钮）；`{}` 处按 Palette 填。
#: 选择器与 library / main_window 里各弹窗用到的部件类型一一对应——本模块的弹窗
#: **不再自带任何字面颜色**，故深色/深蓝下不会漏出白底黑字的控件。
_DIALOG_QSS_TEMPLATE = """\
QDialog {{ background:{window_bg}; }}
QLabel {{ color:{text}; font-size:13px; }}
QLabel#hint {{ color:{muted_text}; font-size:12px; }}
QLabel#error {{ color:{error_text}; font-size:12px; }}
QLabel#sect {{ color:{muted_text}; font-size:11px; font-weight:600; letter-spacing:1px; }}
QLabel#mono {{ color:{muted_text}; font-size:12px; font-family:{mono_font}; }}
QGroupBox {{
    background:{card_bg}; border:1px solid {hairline}; border-radius:8px;
    margin-top:14px; padding:12px 12px 10px 12px; font-size:12px; color:{muted_text};
}}
QGroupBox::title {{
    subcontrol-origin: margin; subcontrol-position: top left; left:12px; padding:0 4px;
    color:{muted_text}; letter-spacing:1px;
}}
QLineEdit, QPlainTextEdit, QTextEdit {{
    background:{input_bg}; border:1px solid {input_border}; border-radius:6px;
    padding:5px 8px; font-size:13px; color:{text};
    selection-background-color:{selection_bg}; selection-color:{selection_text};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {{ border:1px solid {accent}; }}
QLineEdit:disabled, QPlainTextEdit:disabled, QTextEdit:disabled {{
    color:{muted_text}; background:{subtle_bg}; border-color:{hairline};
}}
QListWidget, QListView, QTableWidget, QTableView {{
    background:{card_bg}; border:1px solid {hairline}; border-radius:8px;
    font-size:13px; color:{text}; padding:2px; outline:none;
    selection-background-color:{selection_bg}; selection-color:{selection_text};
}}
QListWidget::item, QListView::item {{ padding:5px 8px; border-radius:4px; }}
QListWidget::item:hover, QListView::item:hover {{ background:{subtle_bg}; }}
QListWidget::item:selected, QListView::item:selected {{
    background:{selection_bg}; color:{selection_text};
}}
QListWidget::item:disabled, QListView::item:disabled {{ color:{muted_text}; }}
QTableWidget, QTableView {{ gridline-color:{hairline}; }}
QTableWidget::item, QTableView::item {{ padding:4px 8px; border:none; }}
QTableWidget::item:hover, QTableView::item:hover {{ background:{subtle_bg}; }}
QTableWidget::item:selected, QTableView::item:selected {{
    background:{selection_bg}; color:{selection_text};
}}
QHeaderView {{ background:{card_bg}; border:none; }}
QHeaderView::section {{
    background:{card_bg}; color:{muted_text}; border:none;
    border-bottom:1px solid {divider}; padding:5px 8px;
    font-size:11px; font-weight:600; letter-spacing:1px;
}}
QTableCornerButton::section {{ background:{card_bg}; border:none; }}
QSpinBox, QDoubleSpinBox {{
    background:{input_bg}; border:1px solid {input_border}; border-radius:6px;
    padding:4px 6px; font-size:13px; color:{text}; font-family:{mono_font};
    selection-background-color:{selection_bg}; selection-color:{selection_text};
}}
QSpinBox:focus, QDoubleSpinBox:focus {{ border:1px solid {accent}; }}
QSpinBox:disabled, QDoubleSpinBox:disabled {{
    color:{muted_text}; background:{subtle_bg}; border-color:{hairline};
}}
QSpinBox::up-button, QDoubleSpinBox::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border; background:transparent; border:none; width:16px;
}}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
    background:{subtle_bg};
}}
QComboBox {{
    background:{input_bg}; border:1px solid {input_border}; border-radius:6px;
    padding:4px 8px; font-size:13px; color:{text};
}}
QComboBox:hover {{ border-color:{divider}; }}
QComboBox:focus, QComboBox:on {{ border-color:{accent}; }}
QComboBox::drop-down {{ border:none; width:18px; }}
QComboBox QAbstractItemView {{
    background:{card_bg}; border:1px solid {input_border}; border-radius:6px;
    color:{text}; padding:2px; outline:none;
    selection-background-color:{selection_bg}; selection-color:{selection_text};
}}
QCheckBox, QRadioButton {{ color:{text}; font-size:13px; spacing:6px; }}
QCheckBox::indicator, QRadioButton::indicator {{ width:14px; height:14px; }}
QCheckBox::indicator:unchecked, QRadioButton::indicator:unchecked {{
    border:1px solid {input_border}; background:{input_bg};
}}
QCheckBox::indicator:unchecked {{ border-radius:3px; }}
QRadioButton::indicator:unchecked {{ border-radius:7px; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    border:1px solid {accent}; background:{accent};
}}
QCheckBox::indicator:checked {{ border-radius:3px; }}
QRadioButton::indicator:checked {{ border-radius:7px; }}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{
    border-color:{hairline}; background:{subtle_bg};
}}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color:{accent}; }}
QSlider::groove:horizontal {{ height:2px; background:{divider}; border-radius:1px; }}
QSlider::sub-page:horizontal {{ background:{accent}; height:2px; border-radius:1px; }}
QSlider::add-page:horizontal {{ background:{divider}; height:2px; border-radius:1px; }}
QSlider::handle:horizontal {{
    background:{card_bg}; border:1px solid {accent}; border-radius:5px;
    width:10px; height:10px; margin:-5px 0;
}}
QSlider::handle:horizontal:hover {{ background:{accent}; }}
QSlider::groove:horizontal:disabled {{ background:{hairline}; }}
QSlider::handle:horizontal:disabled {{ border-color:{hairline}; background:{subtle_bg}; }}
QStackedWidget {{ background:transparent; }}
QTabWidget::pane {{ background:{card_bg}; border:1px solid {hairline}; border-radius:8px; }}
QTabBar::tab {{
    background:transparent; color:{muted_text}; padding:6px 12px;
    border:none; border-bottom:2px solid transparent;
}}
QTabBar::tab:hover {{ color:{text}; }}
QTabBar::tab:selected {{ color:{text}; border-bottom:2px solid {accent}; }}
QToolButton {{
    background:transparent; color:{text}; border:1px solid transparent;
    border-radius:6px; padding:4px 7px; font-size:12px;
}}
QToolButton:hover {{ background:{subtle_bg}; }}
QToolButton:checked {{ background:{subtle_bg}; border-color:{accent}; }}
QToolButton:disabled {{ color:{muted_text}; }}
QPushButton {{
    background:{card_bg}; color:{text}; border:1px solid {input_border}; border-radius:6px;
    padding:6px 12px; font-size:13px;
}}
QPushButton:hover {{ background:{subtle_bg}; border-color:{divider}; }}
QPushButton:disabled {{ color:{muted_text}; background:{subtle_bg}; border-color:{hairline}; }}
QPushButton#primary {{
    background:{accent}; color:{accent_text}; border:1px solid {accent}; border-radius:6px;
    padding:7px 15px; font-weight:600;
}}
QPushButton#primary:hover {{ background:{accent_hover}; border-color:{accent_hover}; }}
QPushButton#ghost {{
    background:{card_bg}; color:{text}; border:1px solid {input_border};
    border-radius:6px; padding:6px 12px; font-size:13px;
}}
QPushButton#ghost:hover {{ background:{subtle_bg}; border-color:{divider}; }}
QPushButton#danger {{
    background:{card_bg}; color:{danger}; border:1px solid {danger};
    border-radius:6px; padding:6px 12px; font-size:13px;
}}
QPushButton#danger:hover {{ background:{log_highlight}; }}
QPushButton#primary:disabled, QPushButton#ghost:disabled, QPushButton#danger:disabled {{
    color:{muted_text}; background:{subtle_bg}; border-color:{hairline};
}}
QMessageBox {{ background:{window_bg}; }}
QMessageBox QLabel {{ color:{text}; font-size:13px; }}
QMessageBox QPushButton {{
    background:{card_bg}; color:{text}; border:1px solid {input_border};
    border-radius:6px; padding:6px 14px; min-width:64px;
}}
QMessageBox QPushButton:hover {{ background:{subtle_bg}; }}
"""


def dialog_qss(theme: str) -> str:
    """弹窗整份样式表（主题色表 + 弹窗专属规则 + 主题主表）；未知名回退 `默认`。

    各弹窗（library 的角色/场景编辑器、场景库、开场设置、hook 编辑器，主窗口的角色管理/
    高级移入移出/选择器/api 配置）一律用它出表——**不再各自硬编码一份浅色样式**，否则
    深色/深蓝下弹窗仍是白底黑字，与主窗风格割裂。
    """
    return _DIALOG_QSS_TEMPLATE.format(**vars(palette_for(theme))) + theme_qss(theme)
