"""桌面主窗口：三栏布局（场景/指标/冲动/运行控制 | 自主对白 | 角色卡），多语言（§7）。

**「未打开场景」是一等状态**（§3.3 取消启动小窗）：窗口起手就是空的——中央＝空状态页
（一句话 headline + 三个居中按钮「打开场景」「新建场景」「新建角色」，留白充足），
对白/日志窗格与输入条**建好但收起**；任一打开路径成功开场（sig_scene_info）即换成正常
三栏对白界面，`close_scene()` 回到空状态。左侧「运行控制」里的「场景库…」与
场景菜单的「管理场景库…」都开同一个 `library.LibraryDialog`（只列场景）。

**场景持有阵容**（§3.1）：打开场景**不再要求先选人**——走 `_characters_for_scene()`，
按场景文件自己的 characters/participants 在角色库里配卡；**空阵容是合法场次**（无人
活动也能开场），故素材校验对空表改用「场景声明口径」（见 `_validate_scene_materials`），
`library.validate_materials` 的「至少一名角色」那条旧守门不再拦切换。

中央是角色自主持续对白的转录（worker 驱动、逐条 live 追加，无需用户按键推进）；
顶部「日志」开关把中央列分栏：上半=各角色 think 只读日志（以 `·` 起首逐条渲染）、下半=
对白，仅人类可检视（角色私有思考不给其它角色看）；「日志」左侧依次是「保存场景」与
「配置场景」按钮：「保存场景」走 worker.save_scene 落 sidecar（成功经 sig_saved 报
「已保存：<文件名>」；没落盘的场景文件点了只报「当前场景未落盘，无法保存」，绝不假装
保存）；「配置场景」拿**当前场景文件**开 SceneEditorDialog，保存后把新配置推给运行中的
场景（worker.apply_scene_config，缺这个方法时只落盘并说明）并按增删调 worker 的演员表
API，左栏场景卡随即刷新。底部输入条是「随时打断」的人类插话（非必需回合）。

**场景外壳接线（S6，§2.1①/§2.3/§5）**：
· 保存：按钮只在本场开场后可用（没场次无可存），落盘动作全在 worker/引擎侧；
· 自动保存周期：菜单一改即投 `worker.set_autosave_every(5/20/50/100)`，启动时把设置里
  的值推一次（0 = 关闭照样认），真落盘由引擎在块末按周期做；
· 续演：打开场景前若 sidecar 有存档，弹「接着上次继续演吗」（默认 Yes，非破坏性），
  Yes → 开场成功后 `worker.restore_scene()`，No → 清掉存档从头演；**所有打开路径共用一个
  `_maybe_resume`**（switch_scene 内调 → 覆盖打开场景菜单/空状态页/管理场景库弹窗/新建场景，
  app.py 的 `--autostart` 启动路径另调同一函数）；
· 重置：场景 →「重置场景运行上下文…」（确认后 worker.reset_scene()，清对话与思考上下文，
  保留配置与在场角色；与场景编辑器里那个清「入场记录」的「重置场景」是两回事）；
· api 配置：保存过的 url/key/模型名在启动与「模型 api 配置」弹窗保存后推给
  `worker.set_api_config`，下一次建引擎（开场/重开/切场）真正生效；key 只在内存里流转。

**左栏整栏可滚动**（QScrollArea，内容超高即滚，不挤压变形部件；卡序：场景 → 运行指标
→ 冲动 bid（实时）→ 运行控制）。「场景」卡已并入原「场景时间」卡，且**不含任何餐厅
绑定**：只显示场景名、日期（场景设了才显示该行）、开始、当前（HH:MM:SS）、在场人数与
中性措辞的「边界」行（场景有时间硬边界才给 HH:MM，否则「—」，界面上绝不写死「打烊」）。
开场设定卡已删——开场由开始界面/场景库负责（AppConfig.opening 仍供开场用），「开新场」
与「场景库…」移入运行控制卡。冲动卡按在场者每名角色一行（色点+名+细条+数值），
显示其当前**数值 bid**（谁当下最可能拿话筒，随 dynamics_snapshot 实时移动，条截在 cap
处仍显真实值）。

**右栏角色卡只保留**：姓名（+色点）与「实时分量（随情景演化）」块——冲动 bid / 沉默
轮数 / 相关度 / 唤醒 / 邻接 / 目标压力 / 场景压力 / 待回应压力 同源实时刷新，并保留被
点名(回应对象)（仍欠答时后缀「（第 N 轮未答）」）；卡右上「查看详情」灰色按钮弹出
CharacterDetailDialog，以只读方式展示设定/能力/关系/性格权重/情绪衰减/语料。

场景推进（场景=一等 agent）：叙述行（speaker_type=="narrator"）在对白区渲染成**居中斜体
弱化块**，行尾带「撤销 / 改写」两个行内链接（href = scene:undo:<id> / scene:edit:<id>，
经 anchorClicked 回指到具体那一条）；左栏「场景推进」卡给自动推进开关（反映引擎
narration_state()["auto"]）、**推进活跃度**四档下拉（少/中/多/极多 = 0.2/0.5/0.8/1.0，
§6.2：改档即落盘 settings.narrate_activity 并投 worker.set_narrate_activity，启动推一次、
worker 载荷带了当前值时以它为准）与「推进一下」手动按钮，其下状态行显示距上次推进的块数
与上次触发理由。对白区维护本地留存 _conv_msgs 并整屏重绘，故撤销/改写即刻生效。

撤回/改写都是**回溯式**（§6.1：丢掉该条及其之后的全部内容），界面据此对齐两处：
① 两个锚点动作都先确认（`library.confirm`，文案直说「它之后的所有内容一并丢弃」），
取消则零改动；② 撤回/改写让对白区**从该条起截断**（`_drop_from`）——sig_retracted 只报
被撤的那一个 id，只按 id 摘一行会把回溯产生的整段尾巴留在屏上，故改成就地截断留存。

**顶栏菜单（仿 VSCode，设计文档 §2.1）**：`设置 / 场景 / 角色` 三段，顺序固定。
· 设置：模型 api 配置…（弹窗，含**后台线程**拉取 `{url}/models`）、配色、语言、
  自动保存周期——三项子菜单都是**互斥勾选**组，选中即落盘 SettingsStore 并即时生效
  （配色当场 setStyleSheet；语言**当场重译整个界面**：见 `_retranslate`）。
· 场景（顺序固定，§3.3）：打开场景（子菜单列场景库）/ **配置场景…** / **保存场景** /
  重置场景运行上下文… / 管理场景库…（LibraryDialog）/ 新建场景 / **导入场景…** / 分隔线。
· 角色（顺序固定，§3.3）：添加角色（子菜单列角色库里**还不在场**的人）/ 移出角色
  （子菜单列在场者）/ 新建角色 / **导入角色…** / 管理角色…（CharacterManagerDialog：
  本场演员表的增·移·编辑·删）/ 高级移入/移出…（AdvancedCastDialog：选人 + 移入或移出 +
  N 回合后执行 + 可多选的通知对象）。
  未开场（worker.can_cast() 为假）或还没收到演员表时，「添加/移出」两项禁用并给出原因。
· 导入（场景/角色同一个流程）：文件选择 → `template_import.import_template_file`（落进
  角色库/场景库目录）→ 结果框（含「以下字段模板里没填，已用默认值：…」）→ 刷新菜单。
  解析失败只弹一句带行号/字段名的中文说明，绝不把 traceback 摊到界面上。

**场景变更进日志**（§3.5）：worker 的 `sig_scene_changed(list)`（字段名/旧值/新值）到达
时，每条在日志窗格里落一行「场景变更 · <字段> → <新值>」，用**浅红**高亮（按日志底色明暗
选深浅两档，见 `scene_change_color`），与 think 行、诊断行区分开；隐式（visible=False）
变更不进对话，这条日志就是它唯一的可见痕迹。
菜单里的弹窗一律非模态安全：exec() 只跑嵌套事件循环，worker 线程与信号照常推进；**所有
人事动作一律经 worker（add_character / remove_character / schedule_cast_change…），界面
绝不直接调引擎**。

**演员表随 sig_cast 实时重画**（§3.3 角色可插拔）：窗口镜像一份 engine.cast_state()——
被移出者的左栏冲动行与右栏角色卡当场消失、新进场者当场补一行一卡、禁言者在两侧各缀一条
「禁言中（剩 N 回合）」；场景卡的在场名单同源刷新。运行期新加入的人不在开场载荷
（sig_scene_info 只在开场/重开各发一次）里，其右栏卡与详情弹窗的数据就地按角色库里的卡
现补一份（库里也没有就只剩名字 + 实时分量）。

**多语言（§7，S5 接线）**：窗口持一枚 `Translator`（`self._t`，初值取设置里的语言）。
所有**静态**文案（窗口标题/菜单/卡片标题/按钮/左右栏标签/占位文本/状态词表）都经
`t()` 取字并登记进 `_i18n_texts`/`_dyn_texts`；`apply_language(code)` 换语言后调
`_retranslate()` **就地**重设全部文本（不重建窗口），同时写 app 属性 `language`、
发 `language_changed`，并把语言码投给 `worker.set_language`（防御式 getattr）让引擎把
「你将使用<语言>回答。」注入所有提示词。动态内容（对白/叙述/think 日志/角色名/数值）
是**内容**不是界面文案，不参与重译。zh-Hans 下逐字与接线前完全一致。

样式基调：冷调「仪器」浅色（近白面 + 发丝线 + 单一去饱和蓝青强调，§3.6），圆角、留白克制。
配色全部由当前主题的色表给出（`theme.palette_for(主题)`）：窗口样式表走 `theme_qss`，
**内容**则用色表取值——对白/日志 HTML 的颜色、左右栏程序化配色的标签（键值行/冲动行/
角色卡名/数值条）、说话者色（`theme.SPEAKER_COLORS`，按主题取）与模型药丸的底/字，
在渲染那一刻取色，故 `apply_theme` 除了重铺样式表还要**重绘内容**（`_rerender_conversation`
重绘对白、`_rerender_log` 重排日志、`_apply_palette_styles` 重刷登记过的标签、
`_set_model_badge` 重画药丸）。
全部数据经 SceneWorker 信号进入（sig_scene_info / sig_message / sig_metrics /
sig_status / sig_dynamics / sig_think / sig_narration / sig_retracted / sig_finished /
sig_saved / sig_cast / sig_auto_paused / sig_scene_diag）。
"""
from __future__ import annotations

import html
import inspect
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame,
    QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QProgressBar,
    QPushButton, QRadioButton, QScrollArea, QSlider, QSpinBox, QSplitter,
    QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget,
)

from .. import scenestore as scenestore_mod
from ..i18n import DEFAULT_LANGUAGE, LANGUAGES, Translator
from ..loaders import (list_character_paths, list_scene_paths,
                       load_character_card, load_json, load_scene)
from ..template_import import TemplateError, import_template_file
from .library import (
    CharacterDetailDialog, CharacterEditorDialog, LibraryDialog,
    SceneEditorDialog, app_theme, confirm, import_result_text, info,
    translator as gui_translator, validate_materials, warn)
from .settings import (AUTOSAVE_EVERY, NARRATE_ACTIVITIES, AppSettings,
                       SettingsStore, snap_narrate_activity)
from .theme import (THEMES, dialog_qss, palette_for, speaker_colors_for,
                    theme_qss)

#: app/ 根（内置 characters/、scenes/ 素材目录所在），任意 cwd 下都能回落到演示素材。
_APP_DIR = Path(__file__).resolve().parents[3]

#: `默认` / `深色` 两套主题的说话者配色盘（按角色顺序分配；色值定义在 theme.SPEAKER_COLORS
#: ——界面不留字面色）。取名只为可读性：**实际取色一律走 speaker_palette(主题)**。
PALETTE = list(speaker_colors_for("默认"))
PALETTE_DARK = list(speaker_colors_for("深色"))

#: 程序化配色的**语义角色** → 样式表生成器（以当前主题的 Palette 填色）。
#: 界面里凡是用 setStyleSheet 手工上色的部件都按角色登记（见 MainWindow._pstyle）：
#: 切主题时整批按新色表重刷——颜色不能在建窗那一刻烘死（G1）。
_LABEL_STYLE_ROLES: dict[str, object] = {
    # 左栏键值行的「键」/次要小字
    "muted12": lambda p, **_: f"color:{p.muted_text};font-size:12px;",
    # 键值行的「值」/正文级小字
    "text13": lambda p, **_: f"color:{p.text};font-size:13px;",
    "text12": lambda p, **_: f"color:{p.text};font-size:12px;",
    # 数值（沉默轮数/bid 等右侧数字）
    "chip12": lambda p, **_: f"color:{p.chip_text};font-size:12px;",
    # 警示小字（禁言徽标 / 场景诊断）
    "warn11": lambda p, **_: f"color:{p.warn_text};font-size:11px;",
    "warn12": lambda p, **_: f"color:{p.warn_text};font-size:12px;",
    # 中栏页眉 / 角色卡姓名
    "header": lambda p, **_: f"font-size:18px;font-weight:700;color:{p.text};",
    "name": lambda p, **_: f"font-size:15px;font-weight:700;color:{p.text};",
    # 数值条：槽底取主题色，chunk 取该角色自己的说话者色（accent 由调用方给；缺省=主题强调色）
    "bar": lambda p, accent=None, **_: (
        f"QProgressBar{{background:{p.bar_track};border:none;border-radius:3px;}}"
        f"QProgressBar::chunk{{background:{accent or p.accent};border-radius:3px;}}"),
    # 空状态页（§3.3）：headline 与副文案（colors 取色表 → 切主题随 _pstyle 重刷）
    "emptyHeadline": lambda p, **_: (
        f"color:{p.text};font-size:19px;font-weight:600;"),
    "emptyHint": lambda p, **_: (
        f"color:{p.muted_text};font-size:13px;"),
    # 说话者色点（左栏冲动行）与其头像圆底（右栏角色卡）；accent = 该角色的说话者色
    # （由调用方给；缺省=主题强调色，故切主题时这两个角色也不会漏出旧主题的色）
    "dot": lambda p, accent=None, **_: (
        f"background:{accent or p.accent};border-radius:4px;"),
    "avatar": lambda p, accent=None, **_: (
        f"QLabel{{background:{accent or p.accent};color:{p.accent_text};border-radius:17px;"
        f"font-size:15px;font-weight:700;}}"),
}


def _label_style(role: str, palette, **extra) -> str:
    """语义角色 + 色表 → 样式表串（未知角色给空串：不因写错角色把界面刷崩）。"""
    maker = _LABEL_STYLE_ROLES.get(role)
    return maker(palette, **extra) if maker is not None else ""

#: 对白区 HTML 里叙述行两个行内链接的锚点前缀（QTextBrowser.anchorClicked 解析）。
ANCHOR_UNDO = "scene:undo:"
ANCHOR_EDIT = "scene:edit:"

#: 左栏「推进活跃度」四档：**界面标签（i18n 键）→ 数值**。数值本身不在这里定，
#: 而是取 settings.NARRATE_ACTIVITIES——那是全工程活跃度数值的唯一出处（校验吸附同源），
#: 这里只把四个标签按顺序挂上去（少/中/多/极多 ↔ 0.2/0.5/0.8/1.0）。
NARRATE_ACTIVITY_LEVELS: tuple[tuple[str, float], ...] = tuple(zip(
    ("narration.activity.low", "narration.activity.mid",
     "narration.activity.more", "narration.activity.max"),
    NARRATE_ACTIVITIES))


#: 计数改用 k 单位的门槛：≥ 此值（十万）才缩写成「NNNk」，低于则原样显示。
K_UNIT_THRESHOLD = 100_000


def fmt_count(n: int | str) -> str:
    """计数 → 显示串：**低于 10 万原样**，≥ 10 万改用 k 单位（如 273123 → "273k"）。

    取整规则是**向下截断到千位**（`v // 1000`），绝不四舍五入：显示值只会 ≤ 真实值，
    不会把 999_999 说成 "1000k" 这种夸大。负值/非数值原样字符串化（计数不该出现，
    但显示层不因脏数据抛）。
    """
    try:
        v = int(n)
    except (TypeError, ValueError):
        return str(n)
    if v >= K_UNIT_THRESHOLD:
        return f"{v // 1000}k"
    return str(v)


def speaker_palette(theme: str | None = None) -> list[str]:
    """该主题该用的说话者配色盘（四套主题各一套；未知名/None 回退 `默认`）。

    色值全部来自 `theme.SPEAKER_COLORS`——每套的首枚是该主题的 accent，其余四套各自
    成组；浅底两套用深饱和色（配白字）、深底两套用提亮色（配近黑字），故头像圆底上的
    首字母在四套主题下都读得清。返回**新列表**：调用方拿到的不是全局色表本身。
    """
    return list(speaker_colors_for(theme))


def color_for(index: int, palette: list[str] | None = None) -> str:
    """第 index 位（按演员表顺序）说话者的配色：**取模回绕**，选超过配色盘长度的人
    也不越界、不撞无色；相邻两人的颜色仍然不同（盘长 ≥2 时）。

    palette 缺省即浅底配色盘（默认主题）；深色主题由调用方传 speaker_palette(主题)。
    """
    pal = PALETTE if palette is None else palette
    return pal[index % len(pal)]


def assign_name_colors(names: list[str],
                       palette: list[str] | None = None) -> dict[str, str]:
    """演员表（说话者名，按序）→ {name: 颜色}，颜色按序从配色盘取（超长回绕）。

    色表与右栏角色卡的强调色同源（都用 color_for），故同一人在对白、左栏冲动行、
    右栏卡上颜色一致；palette 缺省即浅底配色盘（深色主题传 speaker_palette(主题)）。
    """
    return {name: color_for(i, palette) for i, name in enumerate(names)}


# ====================================================== 场景变更日志（§3.5）
#: Scene 字段名 → 日志里用的中文短名（i18n 键）。未知字段原样显示字段名（不吞信息）。
SCENE_FIELD_KEYS: dict[str, str] = {
    "name": "scene.field.name",
    "date": "scene.field.date",
    "background": "scene.field.background",
    "description": "scene.field.description",
    "description_mutable": "scene.field.description_mutable",
    "plot_direction": "scene.field.plot_direction",
    "hard_boundary": "scene.field.hard_boundary",
    "start_time": "scene.field.start_time",
    "characters": "scene.field.participants",
    "participants": "scene.field.participants",
}

#: 场景变更行的**浅红**高亮：浅底主题一档、深底主题一档（都是压过的暗红，不刺眼）。
SCENE_CHANGE_RED_LIGHT = "#c2565c"      # 浅底（日志底色亮）
SCENE_CHANGE_RED_DARK = "#e6929a"       # 深底（提亮，深底上才读得清）


def _luminance(color: str) -> float:
    """`#rrggbb` → 相对亮度 0..1（认不出/脏值当亮色，绝不抛）。"""
    text = str(color or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        return 1.0
    try:
        r, g, b = (int(text[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    except ValueError:
        return 1.0
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def scene_change_color(palette) -> str:
    """场景变更行的浅红高亮色：按**日志底色**的明暗二选一（不看主题名，未知主题也对版）。

    浅底用 `#c2565c`，深色/深蓝那类深底用提亮的 `#e6929a`——两个都是压过的暗红，
    与 think 行（角色色 + 弱化灰）和诊断行（warn 琥珀）都不会看混。
    """
    base = getattr(palette, "thinklog_bg", "") or getattr(palette, "window_bg", "")
    return SCENE_CHANGE_RED_DARK if _luminance(base) < 0.5 else SCENE_CHANGE_RED_LIGHT


def scene_field_label(field: str, tr: Translator) -> str:
    """Scene 字段名 → 日志里显示的中文短名（未知字段原样返回，不吞信息）。"""
    key = SCENE_FIELD_KEYS.get(str(field or "").strip())
    return tr.t(key) if key is not None else str(field or "")


def scene_change_value(value, tr: Translator) -> str:
    """变更值 → 一行可读文本（空串 = 该值「没有内容」，由调用方给占位）。

    值可能是字符串/数字/真假/名单/硬边界对象（engine 侧口径）：布尔给「是/否」、
    名单用「、」连、硬边界给出「类型 值」（type="none" 视为无边界 → 空）。
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return tr.t("label.yes") if value else tr.t("label.no")
    if isinstance(value, (list, tuple, set)):
        return "、".join(str(v) for v in value)
    if isinstance(value, dict):
        kind = value.get("type")
        if kind is not None:
            return "" if str(kind) == "none" else f"{kind} {value.get('value') or ''}".strip()
        return "、".join(f"{k}:{v}" for k, v in value.items())
    kind = getattr(value, "type", None)
    if kind is not None:                 # HardBoundary 一类的模型对象
        if str(kind) == "none":
            return ""
        return f"{kind} {getattr(value, 'value', '') or ''}".strip()
    return str(value)


def scene_change_text(change: dict, tr: Translator) -> str:
    """一条场景变更（{field, old, new}）→ 日志行文本「场景变更 · 描述 → <新值>」。

    `new` 为空给「（空）」占位——空串会让整行看起来像没写完。字段名是**数据**（引擎口径），
    只在认识时译成中文短名。
    """
    field = scene_field_label(str((change or {}).get("field") or ""), tr)
    text = scene_change_value((change or {}).get("new"), tr).strip()
    return tr.t("log.scene_changed", field=field,
                value=text or tr.t("log.scene_value_empty"))


#: worker 状态串开头的**警告记号**（worker 侧数据，不是界面文案）：显示前剥掉它，
#: 界面因此不出现任何符号（§3.6 无 emoji）；正文一字不改。
_STATUS_MARK_PREFIX = "⚠"

#: worker 状态串里表示「出错了」的记号：旧口径带上面的前缀，新口径直接给「开场失败：…」
#: ——两种都认，状态药丸照样刷红（不靠单一符号，也就不会随 worker 改文案失灵）。
_STATUS_ERROR_MARKS = (_STATUS_MARK_PREFIX, "开场失败", "保存场景失败", "失败：")


def is_error_status(text: str) -> bool:
    """worker 状态串是不是「出错」那条（决定状态药丸刷红）。"""
    raw = str(text or "")
    return any(raw.startswith(m) if m == _STATUS_MARK_PREFIX else m in raw
               for m in _STATUS_ERROR_MARKS)


def strip_status_mark(text: str) -> str:
    """worker 状态串 → 去掉开头的警告记号再显示（界面里不留符号）。

    只动**开头**那一个记号与紧随的空格；正文一字不改（worker 的现场信息照原样透出）。
    """
    out = str(text or "")
    if out.startswith(_STATUS_MARK_PREFIX):
        return out[len(_STATUS_MARK_PREFIX):].lstrip()
    return out


def pick_template_file(parent, title: str, start_dir: str, file_filter: str) -> str:
    """挑一个模板 .md 的路径（取消 → 空串）。

    独立成函数便于离屏测试打桩（与 ask_resume / library.confirm 同款做法），不弹真文件框。
    """
    filename, _selected_filter = QFileDialog.getOpenFileName(
        parent, title, start_dir, file_filter)
    return str(filename or "")


def ask_resume(parent, title: str, text: str) -> bool:
    """续演询问框（§5「打开场景」时问要不要接着上次演）。

    独立成函数便于离屏测试打桩（与 library.confirm 同款做法），不弹真模态。
    **默认 Yes**：接着演是非破坏性选项，回车不该等于「丢掉存档从头来」。
    """
    btn = QMessageBox.question(
        parent, title, text,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.Yes)
    return btn == QMessageBox.StandardButton.Yes


#: 日志视图（角色 think 只读检视）：QTextDocument 最大块数（超出丢最旧整行）。
THINK_PANE_CAP = 400

#: 全局流速档位（×真实时间；默认 1× 近实时，21:30→22:00 约 30 真实分钟）。
RATE_OPTIONS = [0.25, 0.5, 1, 2, 4, 8, 16, 32, 60]

#: 左栏滚动区内容的最小宽度（「场景名/在场人数」等键 + 值同排不挤成豆腐块；
#: 值标签一律 wordWrap，超窄时换行而不是截断）。
LEFT_PANEL_MIN_WIDTH = 252
#: 卡内「键」栏统一宽度（与 _add_kv 的缺省 key_width 同源）。
KEY_WIDTH = 64

#: 状态 → (chip 底色, chip 前景)
_STATUS_STYLE = {
    "就绪":   ("#e7e5e4", "#57534e"),
    "进行中": ("#d1fae5", "#065f46"),
    "已暂停": ("#fef3c7", "#92400e"),
    "已收束": ("#e2e8f0", "#475569"),
    "已停止": ("#fee2e2", "#991b1b"),   # 用户主动停止（区别于自然收束）→ 红
}

#: worker 报来的**已格式化**状态串 → i18n 键（§7：词表按已知状态映射，未知原样放过）。
#: 带数字/原因的状态（「已达 N 块，点继续以续演」「开场失败：…」）不在表里 → 原样显示，
#: 既不误译也不吞掉 worker 的现场信息。
_STATUS_KEYS: dict[str, str] = {
    "就绪": "status.ready",
    "进行中": "status.running",
    "已暂停": "status.paused",
    "等待中…可随时插话": "status.waiting",
    "模型暂不可用，稍候重试…": "status.retry",
    "已收束": "status.closed",
    "已停止": "status.stopped",
}


@dataclass
class AppConfig:
    """应用启动配置（app.py 解析 CLI 后组装；默认即内置演示素材）。"""
    scene: Path
    characters: list[Path]
    models: Path            # 基础 stub 模型 yaml；live 时 worker 切 models.live.yaml
    bid: Path
    run_root: Path
    live: bool = True       # 是否优先真实模型（worker 视 key 可用性自动回落 stub）
    api_key: str | None = None
    closing_at_block: int | None = None   # None=禁用块数收束（默认）：App 永不按块数结束，
                                          # 只由虚拟钟走到打烊（worker ticker）或用户停止收束
    opening: str = ""       # 开场设定初值（留空用引擎默认开场）
    #: 演员表以**所选角色卡**为准（默认）：界面上选中多少人，这场就有多少人在场——
    #: 场景文件的 participants 只当默认值。False 才回落「场景名单说了算」的旧语义。
    cast_from_cards: bool = True


# ====================================================== 模型列表拉取（设置①弹窗）
def models_url(base_url: str) -> str:
    """服务地址 → 模型列表地址：`{base_url}/models`（尾随 `/` 先去掉）。

    OpenAI 兼容服务（Anthropic/DeepSeek 一类）都在这个地址列模型，故「拉取模型」
    按钮请求的就是它。
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise ValueError(gui_translator().t("err.url_empty"))
    return f"{base}/models"


def extract_model_ids(payload) -> list[str]:
    """OpenAI 兼容响应 → 模型 id 列表：取 `data[].id`（也容忍 data 直接是字符串表）。

    形状不对/缺失一律返回 []（不抛）——「没有模型」由调用方给出人话提示。
    """
    data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(data, list):
        return []
    out: list[str] = []
    for item in data:
        mid = item.get("id") if isinstance(item, dict) else item
        if isinstance(mid, str) and mid.strip():
            out.append(mid.strip())
    return out


def fetch_model_ids(base_url: str, timeout: float = 10.0) -> list[str]:
    """GET `{base_url}/models` → data[].id 列表。**同步**函数，失败抛 RuntimeError。

    调用方必须放进后台线程（见 _ModelFetchThread）——GUI 线程里直接调会卡住界面。
    全部失败路径都转成可直接给用户看的中文 RuntimeError，绝不静默返回空表。
    """
    t = gui_translator()
    url = models_url(base_url)
    import httpx                      # httpx 是本项目核心依赖（见 pyproject）
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url)
    except Exception as exc:          # noqa: BLE001 - 网络异常种类繁多，统一转人话
        raise RuntimeError(t.t("err.connect_failed", url=url, exc=exc)) from exc
    status = int(getattr(resp, "status_code", 0) or 0)
    if status >= 400:
        raise RuntimeError(t.t("err.http_status", status=status, url=url))
    try:
        payload = resp.json()
    except Exception as exc:          # noqa: BLE001 - 非 JSON 响应同样要给人话
        raise RuntimeError(t.t("err.bad_json", exc=exc)) from exc
    ids = extract_model_ids(payload)
    if not ids:
        raise RuntimeError(t.t("err.no_ids_in_reply", url=url))
    return ids


class _ModelFetchThread(QThread):
    """「拉取模型」的后台线程：跑同步的 fetch(url)，结果经信号回 GUI 线程。

    线程**绝不碰界面**：成功 emit fetched(ids)，失败 emit failed(原因)。任何异常都在
    这里被转成信号，故一次网络故障不会掀翻 Qt 事件循环。
    """

    fetched = Signal(list)
    failed = Signal(str)

    def __init__(self, url: str, fetch=None, parent=None) -> None:
        super().__init__(parent)
        self._url = url
        # 调用时才解析模块级 fetch_model_ids（便于测试注入；缺省即真实网络实现）。
        self._fetch = fetch if fetch is not None else fetch_model_ids

    def run(self) -> None:            # noqa: D102 - QThread 入口
        try:
            ids = [str(i) for i in (self._fetch(self._url) or [])]
        except Exception as exc:      # noqa: BLE001 - 线程内异常必须变信号，否则静默
            self.failed.emit(str(exc) or exc.__class__.__name__)
            return
        self.fetched.emit(ids)


def autosave_label(every: int) -> str:
    """自动保存周期项文案（zh-Hans 口径）：`每 20 轮`（设计文档 §2.1①）。

    这是**纯函数 + 中文规范文案**（与 i18n 的 `menu.autosave_every` 同值）：菜单项本身
    走 t()，这里保留给「不依赖 QApplication 的调用方/测试」当对照基准。
    """
    return f"每 {int(every)} 轮"


# ====================================================== 演员表（角色可插拔 §3.3）
#: 「高级移入/移出」延时执行的回合上限（0 = 立即，见 AdvancedCastDialog）。
CAST_DELAY_MAX = 99

#: 通知对象里的「场景」——引擎把 notify 当**认知轴 knows 名单**（只有名单里的人看得见
#: 这条通知行），场景（叙述者，speaker="场景"）本来就读全部消息、不做 knows 过滤，故
#: 这一项表达的是「这条通知不进任何角色的视野」（§4.1② 通知对象 = 在场者 + 自己 + 场景）。
#: **故意不翻译**：它是投给引擎的**数据**（引擎按这个名字认叙述者），不是界面文案。
SCENE_NOTIFY_NAME = "场景"

#: 右栏/左栏表格里「在场」列的勾（纯展示，真值一律以 sig_cast 为准）。
CAST_PRESENT_MARK = "✓"


def mute_badge_text(muted: dict | None, name: str, tr: Translator | None = None) -> str:
    """禁言状态 → 徽标文案（左栏冲动行与右栏角色卡同源）：

    未禁言（名字不在表里）= ""（不显示徽标）；永久禁言（值 None）=「禁言中」；
    临时禁言（值 = 剩余块数）=「禁言中（剩 N 回合）」。纯函数（tr 不给就按当前界面
    语言），便于单测。
    """
    t = tr if tr is not None else gui_translator()
    table = muted or {}
    if name not in table:
        return ""
    left = table[name]
    if left is None or left == "":
        return t.t("card.muted")
    try:
        return t.t("card.muted_turns", n=int(left))
    except (TypeError, ValueError):
        return t.t("card.muted")


def _scan_character_cards(directory: Path) -> list[tuple[Path, str]]:
    """角色库目录 → [(卡路径, 卡名)]（坏卡用文件名当名字；目录缺失 → []）。"""
    try:
        paths = list(list_character_paths(Path(directory)))
    except OSError:
        return []
    out: list[tuple[Path, str]] = []
    for path in paths:
        try:
            data = load_json(path)
        except (OSError, ValueError):
            data = None
        name = (str(data.get("name") or "").strip()
                if isinstance(data, dict) else "")
        out.append((path, name or path.stem))
    return out


class ApiConfigDialog(QDialog):
    """模型 api 配置弹窗（设置 →「模型 api 配置…」）。

    字段：服务地址 / API Key（密码态 + 「显示」开关）/ 模型名称（手填）+「拉取模型」
    按钮拉一份 `{url}/models` 的 id 列表供选。保存 → SettingsStore.update(...)，取消
    什么都不动。拉取走 _ModelFetchThread 后台线程，失败只在弹窗里红字提示。

    程序化填充用部件名：url_edit / key_edit / show_key_check / model_edit /
    model_combo / fetch_btn / save_btn / cancel_btn / error_label / hint_label。
    """

    def __init__(self, settings: AppSettings | None = None, parent=None, *,
                 store: SettingsStore | None = None, theme: str = THEMES[0],
                 fetch=None) -> None:
        super().__init__(parent)
        t = gui_translator()
        current = settings if settings is not None else AppSettings()
        self._store = store if store is not None else SettingsStore()
        self._fetch = fetch                    # None → 用模块级 fetch_model_ids
        self._thread: _ModelFetchThread | None = None
        #: 保存后的设置快照（未保存过为 None）——调用方读它刷新自己那份。
        self.saved_settings: AppSettings | None = None

        self.setWindowTitle(t.t("dlg.api_title"))
        self.setMinimumWidth(520)
        self.setStyleSheet(self._dialog_qss(theme))

        grid = QGridLayout()
        grid.setContentsMargins(16, 16, 16, 12)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)

        self.url_edit = QLineEdit(current.api_base_url)
        self.url_edit.setObjectName("opening")
        self.url_edit.setPlaceholderText(t.t("label.please_url"))
        grid.addWidget(QLabel(t.t("label.service_url")), 0, 0)
        grid.addWidget(self.url_edit, 0, 1, 1, 2)

        self.key_edit = QLineEdit(current.api_key)
        self.key_edit.setObjectName("opening")
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.show_key_check = QCheckBox(t.t("toggle.show_key"))
        self.show_key_check.setToolTip(t.t("tip.show_key"))
        self.show_key_check.toggled.connect(self._on_show_key)
        key_row = QHBoxLayout()
        key_row.setSpacing(6)
        key_row.addWidget(self.key_edit, 1)
        key_row.addWidget(self.show_key_check)
        grid.addWidget(QLabel("API Key"), 1, 0)
        grid.addLayout(key_row, 1, 1, 1, 2)

        self.model_edit = QLineEdit(current.model_name)
        self.model_edit.setObjectName("opening")
        self.model_edit.setPlaceholderText(t.t("label.please_model"))
        self.fetch_btn = QPushButton(t.t("btn.fetch_models"))
        self.fetch_btn.setObjectName("ghost")
        self.fetch_btn.setToolTip(t.t("tip.fetch_models"))
        self.fetch_btn.clicked.connect(self._on_fetch_models)
        model_row = QHBoxLayout()
        model_row.setSpacing(6)
        model_row.addWidget(self.model_edit, 1)
        model_row.addWidget(self.fetch_btn)
        grid.addWidget(QLabel(t.t("label.model_name")), 2, 0)
        grid.addLayout(model_row, 2, 1, 1, 2)

        self.model_combo = QComboBox()
        self.model_combo.setPlaceholderText(t.t("combo.models_placeholder"))
        self.model_combo.setToolTip(t.t("tip.models_combo"))
        self.model_combo.activated.connect(self._on_model_activated)
        grid.addWidget(self.model_combo, 3, 1, 1, 2)

        self.hint_label = QLabel("")
        self.hint_label.setWordWrap(True)
        self.hint_label.setObjectName("hint")
        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setObjectName("error")
        grid.addWidget(self.hint_label, 4, 1, 1, 2)
        grid.addWidget(self.error_label, 5, 1, 1, 2)

        self.save_btn = QPushButton(t.t("btn.save"))
        self.save_btn.setObjectName("primary")
        self.save_btn.clicked.connect(self.accept)
        self.cancel_btn = QPushButton(t.t("btn.cancel"))
        self.cancel_btn.setObjectName("ghost")
        self.cancel_btn.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_btn)
        buttons.addWidget(self.save_btn)
        grid.addLayout(buttons, 6, 0, 1, 3)
        grid.setColumnStretch(1, 1)
        self.setLayout(grid)

    # ---------------------------------------------------------------- 样式
    @staticmethod
    def _dialog_qss(theme: str) -> str:
        """弹窗样式：主题色表 + 弹窗专属规则（库/主窗各弹窗共用同一份，见 theme.dialog_qss）。

        已打开的**其它**弹窗不会跟着换色（Qt 不做全局重刷）——换主题只影响之后 new
        出来的弹窗；本弹窗在构造时取当前主题，故总是对版。
        """
        return dialog_qss(theme)

    # ---------------------------------------------------------------- 交互
    def _on_show_key(self, shown: bool) -> None:
        """「显示」开关：切 API Key 输入框的明文/密码态。"""
        self.key_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password)

    def _on_model_activated(self, index: int) -> None:
        """从拉取结果里选一个 → 填进「模型名称」（手填的内容不因此受限）。"""
        text = self.model_combo.itemText(index).strip()
        if text:
            self.model_edit.setText(text)

    def _set_error(self, text: str) -> None:
        self.error_label.setText(text)

    def _on_fetch_models(self) -> None:
        """「拉取模型」：起后台线程请求 `{url}/models`（GUI 全程不阻塞）。"""
        if self._thread is not None and self._thread.isRunning():
            return                          # 已有一次在飞：忽略重复点击
        t = gui_translator()
        url = self.url_edit.text().strip()
        if not url:
            self._set_error(t.t("err.need_url"))
            return
        self._set_error("")
        self.hint_label.setText(t.t("status.fetching"))
        self.fetch_btn.setEnabled(False)
        self.model_combo.clear()
        thread = _ModelFetchThread(url, self._fetch, self)
        thread.fetched.connect(self._on_models_fetched)
        thread.failed.connect(self._on_fetch_failed)
        self._thread = thread
        thread.start()

    def _on_models_fetched(self, ids: list) -> None:
        t = gui_translator()
        self._thread = None
        self.fetch_btn.setEnabled(True)
        if not ids:
            self.hint_label.setText("")
            self._set_error(t.t("err.no_model_ids"))
            return
        for mid in ids:
            self.model_combo.addItem(str(mid))
        self.model_combo.setCurrentIndex(-1)
        self.hint_label.setText(t.t("status.fetched", n=len(ids)))

    def _on_fetch_failed(self, reason: str) -> None:
        t = gui_translator()
        self._thread = None
        self.fetch_btn.setEnabled(True)
        self.hint_label.setText("")
        self._set_error(t.t("err.fetch_failed", reason=reason))

    def done(self, code: int) -> None:      # noqa: D102 - QDialog 收尾
        # 关窗前等在飞的拉取线程落地：线程是子对象，随手销毁会撞
        # "QThread: Destroyed while thread is still running"。
        if self._thread is not None and self._thread.isRunning():
            self._thread.wait(3000)
        super().done(code)

    def accept(self) -> None:               # noqa: D102 - 保存
        self.saved_settings = self._store.update(
            api_base_url=self.url_edit.text().strip(),
            api_key=self.key_edit.text().strip(),
            model_name=self.model_edit.text().strip())
        super().accept()


# ====================================================== 角色菜单的弹窗（§2.1③）
def _prepare_dialog(dlg: QDialog, *, size: tuple[int, int] = (0, 0)):
    """三个角色弹窗共用的骨架：在给定对话框上铺样式 + 返回 (body, 底部按钮行) 两层布局。

    样式与库里的弹窗同源（`theme.dialog_qss`：主题色表 + 弹窗专属规则 + 主表），按
    **打开那一刻**的界面主题出表——不再各自硬编码一份浅色样式（深色下会白底黑字）。
    对话框以主窗口为 parent，主窗口样式表也会级联下来，这里补的是 QDialog 底色与
    列表/表格/分组框的观感。
    """
    dlg.setStyleSheet(dialog_qss(app_theme()))
    if size != (0, 0):
        dlg.setMinimumSize(*size)
    outer = QVBoxLayout(dlg)
    outer.setContentsMargins(14, 12, 14, 12)
    outer.setSpacing(10)
    body = QVBoxLayout()
    body.setSpacing(8)
    outer.addLayout(body, 1)
    foot = QHBoxLayout()
    foot.setSpacing(8)
    outer.addLayout(foot)
    return body, foot


class CharacterPickerDialog(QDialog):
    """从角色库里挑一个人（「管理角色…」的添加角色 / 高级移入移出共用的小选择器）。

    names 为空时给一句禁用占位说明、确定按钮保持禁用（不假装有人可选）。选中的名字放在
    `chosen`（取消 / 未选 = None）。程序化填充与测试用部件名：list / ok_btn / cancel_btn。
    """

    def __init__(self, names: list[str], parent=None, *,
                 title: str | None = None, hint: str = ""):
        super().__init__(parent)
        t = gui_translator()
        self.chosen: str | None = None
        self._names = [str(n) for n in (names or [])]
        self.setWindowTitle(title if title is not None else t.t("dlg.pick_title"))
        self._body, self._foot = _prepare_dialog(self, size=(320, 360))

        if hint:
            lb = QLabel(hint)
            lb.setObjectName("hint")
            lb.setWordWrap(True)
            self._body.addWidget(lb)
        self.list = QListWidget()
        self.list.setObjectName("list")
        self.list.addItems(self._names)
        if self._names:
            self.list.setCurrentRow(0)
        else:
            self.list.addItem(t.t("dlg.pick_empty"))
            self.list.setEnabled(False)
        self.list.itemDoubleClicked.connect(lambda _it: self._on_ok())
        self._body.addWidget(self.list, 1)

        self.ok_btn = QPushButton(t.t("btn.ok"))
        self.ok_btn.setObjectName("primary")
        self.ok_btn.setEnabled(bool(self._names))
        self.ok_btn.clicked.connect(self._on_ok)
        self.cancel_btn = QPushButton(t.t("btn.cancel"))
        self.cancel_btn.setObjectName("ghost")
        self.cancel_btn.clicked.connect(self.reject)
        self._foot.addStretch(1)
        self._foot.addWidget(self.cancel_btn)
        self._foot.addWidget(self.ok_btn)

    def selected_name(self) -> str | None:
        """当前高亮的人名（没得选 / 未选中 → None）。"""
        item = self.list.currentItem()
        if item is None or not self._names or not self.list.isEnabled():
            return None
        row = self.list.currentRow()
        return self._names[row] if 0 <= row < len(self._names) else None

    def _on_ok(self) -> None:
        t = gui_translator()
        name = self.selected_name()
        if name is None:
            warn(self, t.t("dlg.pick_title"), t.t("err.pick_one"))
            return
        self.chosen = name
        self.accept()


class CharacterManagerDialog(QDialog):
    """角色 →「管理角色…」：本场演员表的**增 / 移 / 编辑 / 删**（§2.1③）。

    表格列出本场演员表（姓名 / 在场 ✓ / 禁言状态）：在场者在前、已移出者在后（「在场」列
    留空）。所有增删一律经 worker 的人事 API，**界面绝不直接调引擎**；表格内容是
    sig_cast（engine.cast_state 全量）的镜像——窗口收到新载荷即调 update_cast() 重画，
    故「谁在场、禁言剩几块」永远以引擎为准，不靠对话框自记。底部另有设计文档要求的
    「添加角色」按钮（与顶部那枚同一个动作）。

    程序化填充与测试用部件名：table / add_btn / remove_btn / edit_btn / delete_btn /
    bottom_add_btn / close_btn；selected_name() 取当前选中行的人名。
    """

    #: 表格列（顺序即设计文档 §2.1③ 的 姓名 / 在场 / 禁言状态）；文案走 i18n 键。
    COLUMN_KEYS = ("col.name", "col.present", "col.muted")

    def __init__(self, cast: dict | None = None, worker=None,
                 characters_dir: Path | None = None, parent=None):
        super().__init__(parent)
        t = gui_translator()
        self._cast = dict(cast or {})
        self._worker = worker
        self._characters_dir = (Path(characters_dir) if characters_dir
                                else Path("."))
        self.setWindowTitle(t.t("dlg.manage_cast_title"))
        self._body, self._foot = _prepare_dialog(self, size=(560, 420))

        hint = QLabel(t.t("dlg.manage_cast_hint"))
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        self._body.addWidget(hint)

        self.table = QTableWidget(0, len(self.COLUMN_KEYS))
        self.table.setHorizontalHeaderLabels([t.t(k) for k in self.COLUMN_KEYS])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._sync_buttons)
        self._body.addWidget(self.table, 1)

        self.add_btn = QPushButton(t.t("btn.add_character"))
        self.add_btn.setObjectName("ghost")
        self.add_btn.setToolTip(t.t("tip.manage_add_character"))
        self.add_btn.clicked.connect(self._on_add)
        self.remove_btn = QPushButton(t.t("btn.remove_character"))
        self.remove_btn.setObjectName("ghost")
        self.remove_btn.setToolTip(t.t("tip.manage_remove_character"))
        self.remove_btn.clicked.connect(self._on_remove)
        self.edit_btn = QPushButton(t.t("btn.edit_character"))
        self.edit_btn.setObjectName("ghost")
        self.edit_btn.setToolTip(t.t("tip.manage_edit_character"))
        self.edit_btn.clicked.connect(self._on_edit)
        self.delete_btn = QPushButton(t.t("btn.delete_character"))
        self.delete_btn.setObjectName("ghost")
        self.delete_btn.setToolTip(t.t("tip.manage_delete_character"))
        self.delete_btn.clicked.connect(self._on_delete)
        for btn in (self.add_btn, self.remove_btn, self.edit_btn, self.delete_btn):
            self._foot.addWidget(btn)
        self._foot.addStretch(1)
        self.bottom_add_btn = QPushButton(t.t("btn.add_character"))
        self.bottom_add_btn.setObjectName("primary")
        self.bottom_add_btn.clicked.connect(self._on_add)
        self.close_btn = QPushButton(t.t("btn.close"))
        self.close_btn.setObjectName("ghost")
        self.close_btn.clicked.connect(self.accept)
        self._foot.addWidget(self.close_btn)
        self._foot.addWidget(self.bottom_add_btn)

        self.update_cast(self._cast)

    # ---------------------------------------------------------------- 数据
    def update_cast(self, cast: dict | None) -> None:
        """sig_cast 载荷到达 → 重画表格（在场的打勾、禁言者给出剩余块数）。"""
        t = gui_translator()
        self._cast = dict(cast or {})
        keep = self.selected_name()
        active = [str(n) for n in (self._cast.get("active") or [])]
        inactive = [str(n) for n in (self._cast.get("inactive") or [])]
        muted = self._cast.get("muted") or {}
        rows = active + [n for n in inactive if n not in active]
        self.table.setRowCount(0)
        for name in rows:
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, QTableWidgetItem(name))
            present = CAST_PRESENT_MARK if name in active else ""
            self.table.setItem(r, 1, QTableWidgetItem(present))
            self.table.setItem(r, 2, QTableWidgetItem(mute_badge_text(muted, name, t)))
        if keep:                       # 重画后尽量保持选中同一人（操作不跳行）
            for r in range(self.table.rowCount()):
                if self.table.item(r, 0).text() == keep:
                    self.table.selectRow(r)
                    break
        self._sync_buttons()

    def _row_name(self, row: int) -> str | None:
        item = self.table.item(row, 0)
        return item.text() if item is not None else None

    def selected_name(self) -> str | None:
        """当前选中行的人名（未选中 → None）。"""
        rows = self.table.selectionModel().selectedRows() if self.table.model() else []
        if not rows:
            return None
        return self._row_name(rows[0].row())

    def active_names(self) -> list[str]:
        return [str(n) for n in (self._cast.get("active") or [])]

    def card_path(self, name: str) -> Path | None:
        """人名 → 角色库里的卡路径（库里没有这张卡 → None）。"""
        return dict((n, p) for p, n in _scan_character_cards(self._characters_dir)) \
            .get(str(name))

    def _sync_buttons(self) -> None:
        name = self.selected_name()
        self.remove_btn.setEnabled(name is not None and name in self.active_names())
        self.edit_btn.setEnabled(name is not None)
        self.delete_btn.setEnabled(name is not None)

    # ---------------------------------------------------------------- 动作
    def _on_add(self) -> None:
        """「添加角色」：列角色库里**不在场**的人；在弹窗里选中即 worker.add_character。"""
        t = gui_translator()
        if self._worker is None or not hasattr(self._worker, "add_character"):
            warn(self, t.t("btn.add_character"), t.t("err.no_engine_scene_menu"))
            return
        active = set(self.active_names())
        names = [n for _p, n in _scan_character_cards(self._characters_dir)
                 if n not in active]
        dlg = CharacterPickerDialog(
            names, self, title=t.t("btn.add_character"),
            hint=t.t("dlg.add_character_hint"))
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.chosen:
            return
        self._worker.add_character(dlg.chosen)

    def _on_remove(self) -> None:
        """「移出角色」：让选中的人离场（历史保留、退出竞价——语义全在引擎侧）。"""
        t = gui_translator()
        name = self.selected_name()
        if name is None:
            warn(self, t.t("btn.remove_character"), t.t("err.pick_row"))
            return
        if self._worker is None or not hasattr(self._worker, "remove_character"):
            warn(self, t.t("btn.remove_character"), t.t("err.no_engine"))
            return
        self._worker.remove_character(name)

    def _on_edit(self) -> None:
        """「编辑角色」：拿库里的卡开现有编辑器（改的是角色卡，不动本场演员表）。"""
        t = gui_translator()
        name = self.selected_name()
        if name is None:
            warn(self, t.t("btn.edit_character"), t.t("err.pick_row"))
            return
        path = self.card_path(name)
        if path is None:
            warn(self, t.t("btn.edit_character"), t.t("err.card_missing", name=name))
            return
        try:
            card = load_character_card(path)
        except (OSError, ValueError) as exc:
            warn(self, t.t("btn.edit_character"), t.t("err.card_unreadable", exc=exc))
            return
        dlg = CharacterEditorDialog(card, self._characters_dir, self)
        dlg.exec()

    def _on_delete(self) -> None:
        """「删除角色」：**确认之后**才删磁盘上的卡文件（绝不静默删素材）。"""
        t = gui_translator()
        name = self.selected_name()
        if name is None:
            warn(self, t.t("btn.delete_character"), t.t("err.pick_row"))
            return
        path = self.card_path(name)
        if path is None:
            warn(self, t.t("btn.delete_character"),
                 t.t("err.card_missing_plain", name=name))
            return
        if not confirm(self, t.t("btn.delete_character"),
                       t.t("dlg.confirm_delete_cast_card", name=name, path=path)):
            return
        try:
            Path(path).unlink()
        except OSError as exc:
            warn(self, t.t("btn.delete_character"),
                 t.t("err.delete_failed", exc=exc))
            return
        self.update_cast(self._cast)


class AdvancedCastDialog(QDialog):
    """角色 →「高级移入/移出…」（§2.1③）：选人 + 移入/移出 + N 回合后执行 + 通知。

    「通知」勾上后可**多选**通知对象 = 在场角色 + 该角色自己 + 「场景」（引擎把 notify 当
    认知轴 knows 名单：只有名单里的人看得见这条通知行；「场景」= 不进任何角色的视野，见
    SCENE_NOTIFY_NAME）。点「确定」→ worker.schedule_cast_change(...)（延时执行全在引擎
    侧排队，界面只投递意图，绝不直接调引擎）。

    程序化填充与测试用部件名：name_combo / add_radio / remove_radio / rounds_spin /
    notify_check / recipients / notify_text_edit / ok_btn / cancel_btn；
    chosen_action() / checked_recipients() 供断言。
    """

    def __init__(self, worker, names: list[str], active: list[str] | None = None,
                 parent=None):
        super().__init__(parent)
        t = gui_translator()
        self._worker = worker
        self._names = [str(n) for n in (names or [])]
        self._active = [str(n) for n in (active or [])]
        self.setWindowTitle(t.t("dlg.advanced_cast_title"))
        self._body, self._foot = _prepare_dialog(self, size=(460, 460))

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel(t.t("field.hook_character")))
        self.name_combo = QComboBox()
        self.name_combo.addItems(self._names)
        self.name_combo.setEnabled(bool(self._names))
        self.name_combo.currentIndexChanged.connect(lambda _i: self._refresh_recipients())
        row.addWidget(self.name_combo, 1)
        self._body.addLayout(row)
        if not self._names:
            hint = QLabel(t.t("dlg.no_cards_yet"))
            hint.setObjectName("hint")
            self._body.addWidget(hint)

        act_box = QGroupBox(t.t("grp.action"))
        act_row = QHBoxLayout(act_box)
        self.add_radio = QRadioButton(t.t("radio.add"))
        self.add_radio.setChecked(True)
        self.remove_radio = QRadioButton(t.t("radio.remove"))
        act_row.addWidget(self.add_radio)
        act_row.addWidget(self.remove_radio)
        act_row.addStretch(1)
        self._body.addWidget(act_box)

        when_box = QGroupBox(t.t("grp.after_rounds"))
        when_row = QHBoxLayout(when_box)
        self.rounds_spin = QSpinBox()
        self.rounds_spin.setRange(0, CAST_DELAY_MAX)
        self.rounds_spin.setValue(0)
        self.rounds_spin.setSuffix(t.t("spin.rounds_suffix"))
        self.rounds_spin.setSpecialValueText(t.t("spin.immediate"))
        self.rounds_spin.setToolTip(t.t("tip.rounds"))
        when_row.addWidget(self.rounds_spin)
        tip = QLabel(t.t("hint.rounds"))
        tip.setObjectName("hint")
        when_row.addWidget(tip, 1)
        self._body.addWidget(when_box)

        note_box = QGroupBox(t.t("grp.notify"))
        note_v = QVBoxLayout(note_box)
        self.notify_check = QCheckBox(t.t("toggle.notify"))
        self.notify_check.setToolTip(t.t("tip.notify"))
        self.notify_check.toggled.connect(self._on_notify_toggled)
        note_v.addWidget(self.notify_check)
        note_tip = QLabel(t.t("hint.notify_recipients"))
        note_tip.setObjectName("hint")
        note_v.addWidget(note_tip)
        self.recipients = QListWidget()
        self.recipients.setMaximumHeight(130)
        self.recipients.setEnabled(False)
        note_v.addWidget(self.recipients)
        self.notify_text_edit = QLineEdit()
        self.notify_text_edit.setPlaceholderText(t.t("input.notify_text"))
        self.notify_text_edit.setEnabled(False)
        note_v.addWidget(self.notify_text_edit)
        self._body.addWidget(note_box)
        self._body.addStretch(1)

        self.ok_btn = QPushButton(t.t("btn.ok"))
        self.ok_btn.setObjectName("primary")
        self.ok_btn.setEnabled(bool(self._names))
        self.ok_btn.clicked.connect(self._on_ok)
        self.cancel_btn = QPushButton(t.t("btn.cancel"))
        self.cancel_btn.setObjectName("ghost")
        self.cancel_btn.clicked.connect(self.reject)
        self._foot.addStretch(1)
        self._foot.addWidget(self.cancel_btn)
        self._foot.addWidget(self.ok_btn)

        self._refresh_recipients()

    # ---------------------------------------------------------------- 数据
    def chosen_name(self) -> str | None:
        """下拉里选中的角色名（库为空 → None）。"""
        if not self._names:
            return None
        i = self.name_combo.currentIndex()
        return self._names[i] if 0 <= i < len(self._names) else None

    def chosen_action(self) -> str:
        """'add' / 'remove'（引擎的 action 词表，见 engine._CAST_ACTIONS）。"""
        return "add" if self.add_radio.isChecked() else "remove"

    def checked_recipients(self) -> list[str]:
        """勾上的通知对象（「通知」未勾 → []）。"""
        if not self.notify_check.isChecked():
            return []
        out: list[str] = []
        for i in range(self.recipients.count()):
            item = self.recipients.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                out.append(item.text())
        return out

    def set_recipients(self, names: list[str]) -> None:
        """程序化勾选通知对象（测试与「恢复上次选择」用）。"""
        want = set(str(n) for n in (names or []))
        for i in range(self.recipients.count()):
            item = self.recipients.item(i)
            item.setCheckState(Qt.CheckState.Checked if item.text() in want
                               else Qt.CheckState.Unchecked)

    def _recipient_candidates(self) -> list[str]:
        """通知对象候选：在场角色 + 该角色自己 + 「场景」（去重、保持顺序）。"""
        out: list[str] = []
        for name in self._active + [self.chosen_name(), SCENE_NOTIFY_NAME]:
            if name and name not in out:
                out.append(str(name))
        return out

    def _refresh_recipients(self) -> None:
        """按当前选中的人重建通知对象清单（已勾上的名字尽量保留）。"""
        keep = set(self.checked_recipients())
        self.recipients.clear()
        for name in self._recipient_candidates():
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if name in keep
                               else Qt.CheckState.Unchecked)
            self.recipients.addItem(item)

    def _on_notify_toggled(self, on: bool) -> None:
        self.recipients.setEnabled(bool(on))
        self.notify_text_edit.setEnabled(bool(on))

    # ---------------------------------------------------------------- 动作
    def _on_ok(self) -> None:
        """确定 → worker.schedule_cast_change（延时 + 通知全在引擎侧排队）。"""
        t = gui_translator()
        name = self.chosen_name()
        if name is None:
            warn(self, t.t("dlg.advanced_cast_title"), t.t("err.no_cards_at_all"))
            return
        if self._worker is None or not hasattr(self._worker, "schedule_cast_change"):
            warn(self, t.t("dlg.advanced_cast_title"), t.t("err.no_engine_open"))
            return
        notify = self.checked_recipients()
        # 没勾「通知」就不带文案：引擎只在 notify 非空时落通知行（§4.1②），带一句用不上
        # 的话只会让预约队列里多一份无意义的载荷。
        text = self.notify_text_edit.text().strip() if notify else ""
        self._worker.schedule_cast_change(
            name, self.chosen_action(), int(self.rounds_spin.value()),
            notify=notify, notify_text=text)
        self.accept()


class MainWindow(QMainWindow):
    """三栏主窗口。构造不含任何引擎动作；连接好 worker 信号后由 start_session() 开场。"""

    #: 语言切换广播（设置 → 语言）：窗口自己即时重译界面（_retranslate），同时把语言码
    #: 广播出去（任何模块都能跟上；app 属性 `language` 也同步写入）。
    language_changed = Signal(str)

    def __init__(self, worker, cfg: AppConfig, settings: AppSettings | None = None,
                 store: SettingsStore | None = None) -> None:
        super().__init__()
        self._worker = worker
        self._cfg = cfg
        # 设置存储放在**构造时**解析缺省路径（SettingsStore() → default_settings_path()）：
        # 这样测试 monkeypatch 默认路径即刻生效，也保证「一份设置贯穿整个窗口生命周期」。
        self._store = store if store is not None else SettingsStore()
        self._settings = settings if settings is not None else self._store.load()
        self._theme = self._settings.theme
        #: 当前主题的色表（程序化配色与对白 HTML 的唯一取色处；apply_theme 会换掉它）。
        self._pal = palette_for(self._theme)
        #: 按语义角色登记的程序化配色部件（切主题时整批重刷；死部件在重刷时剔除）。
        #: 按**容器**分组：static（左/中栏常驻标签）、urge（左栏冲动行）、card（右栏角色卡）。
        #: 后两组随各自容器重建整批作废（否则每块重建一次会把登记表撑成无界）。
        self._palette_groups: dict[str, list[tuple[object, str, dict]]] = {
            "static": [], "urge": [], "card": []}
        self._language = self._settings.language
        #: 本窗口的译者（§7）：构造即用保存的语言，apply_language 换语言后重译全界面。
        self._t = Translator(self._language)
        self._autosave_every = self._settings.autosave_every
        #: 场景推进活跃度（§6.2 四档：少/中/多/极多 = 0.2/0.5/0.8/1.0）。启动取设置，
        #: 改档即落盘 + 投 worker；worker 的叙述状态载荷里带了当前值就以此为准（镜像）。
        self._narrate_activity = self._settings.narrate_activity
        # 本场场景文件（落盘坐标，§5）：开场信息（sig_scene_info 的 scene_path）到达时
        # 记下；为 None = 这一场没有可写的场景文件 →「保存场景」只给提示，不假装保存。
        self._scene_path: Path | None = None
        #: 最近一次成功落盘的存档路径（sig_saved 报来的）。
        self._saved_path: Path | None = None
        #: 「接着上次演吗」已答 Yes、等开场成功后再调 worker.restore_scene() 的那一场
        #: （§5 续演；由 _maybe_resume 置位、_on_scene_info 消费）。
        self._resume_pending: Path | None = None
        self._name_colors: dict[str, str] = {}
        #: 语言切换要重设文本的**静态**部件登记表（label/button/checkbox 皆有 setText）：
        #: (部件, i18n 键)。部件被删（右栏角色卡重建）时 setText 会抛 RuntimeError，
        #: _retranslate 逐条吞掉——登记表只增不删，重建的卡片自己往 _dyn_texts 登记。
        self._i18n_texts: list[tuple[object, str]] = []
        #: 右栏角色卡内的静态标签（随卡片重建整批作废，_rebuild_right_cards 会清空）。
        self._dyn_texts: list[tuple[object, str]] = []
        #: 占位文本与 tooltip 的登记表（G5）：这两类文案都是**建窗那一刻**设一次的，
        #: 不登记就会在切语言后停在旧语言（菜单换了、输入框与悬停提示还是中文）。
        self._i18n_placeholders: list[tuple[object, str]] = []
        self._i18n_tooltips: list[tuple[object, str]] = []
        #: 当前场景名（窗口标题/中栏页眉用；"" = 还没开场 → 用静态占位文案）。
        self._scene_name = ""
        #: 是否已打开场景（§3.3）：False = 中栏是**空状态页**，对白/日志/输入条收起。
        #: 由 sig_scene_info 置真、close_scene() 置假——与 _started 的区别是它描述
        #: 「界面上有没有一个场次」而不是「这一场跑没跑起来」。
        self._scene_open = False
        #: 状态药丸的**原始**（worker 口径）文本与显式样式：切语言时按同一原文重译。
        self._status_raw = "就绪"
        self._status_style: tuple[str, str] | None = None
        #: 最近一次 sig_scene_info 报的后端名（真实模型/离线 stub 药丸按语言重画）。
        self._backend = "stub"
        # 每张角色卡「实时分量」区的部件句柄：name → {bid/turns/relevance/arousal/
        # adjacency/goal_pressure/scene_pressure/pending: {bar, num}, respond: QLabel}。
        # 随 _repopulate_right 重建清空。
        self._dyn_state: dict[str, dict] = {}
        # 左栏「冲动 bid（实时）」卡每行句柄：name → {bar, num, dot, cap, decimals}。
        # 行按本场参与者名单在 _repopulate_left_urge 重建（_build_left 只建空卡）。
        self._urge_rows: dict[str, dict] = {}
        self._urge_body: QVBoxLayout | None = None
        # 每张角色卡最近一次场景信息里的原始载荷（name → dict）：右栏「查看详情」弹窗
        # 的全部数据都取自这里（只含卡本身的公开设定，绝无私有/运行期数据）。运行期新
        # 加入的人不在开场载荷里，_card_payload_for 会按角色库里的卡补一份补进这里。
        self._char_payloads: dict[str, dict] = {}
        # sig_cast 的镜像（engine.cast_state 全量：active/inactive/muted）。None = 还没
        # 收到过任何演员表状态（未开场/开场信息刚到），左右栏此时按 sig_scene_info 的
        # characters 兜底，人事菜单保持禁用。
        self._cast: dict | None = None
        # 正在打开的「管理角色…」弹窗（sig_cast 一到就喂给它重画表格；关掉即置回 None）。
        self._cast_manager: CharacterManagerDialog | None = None
        # 本场场景是否带 time 硬边界（「边界」行只认 time；其余类型/无边界一律「—」）。
        self._boundary_is_time = False
        # 对白区已渲染消息的**本地留存**（结构化，非 HTML）：撤销/改写要按 id 摘行并整屏
        # 重绘，human 气泡也在这里（worker 不派发 human 行，界面自己是唯一真源）。
        self._conv_msgs: list[dict] = []
        # 最近一次 sig_narration 快照（左栏「场景推进」状态行据此刷新；指标刷新时复读）。
        self._narration: dict = {}
        #: worker 报过的作废 id 集合（§6.1 回溯式撤回；含 sig_retracted 与叙述状态载荷里
        #: 的权威 `retracted` 全量）：对白区据此摘行，且**换场/重置即清**（新场从零起算）。
        self._retracted_ids: set[int] = set()
        # 最近一次 sig_scene_diag 快照 + 已展示过的诊断/隐式事件去重签名（G9）：每条诊断
        # 只提示一次、隐式事件只在出现新的一条时写日志（否则每块都刷会把日志糊满）。
        self._scene_diag: dict = {}
        self._diag_seen: dict[str, str] = {}
        self._implicit_seen: tuple | None = None
        #: 日志窗格的**结构化留存**（think 条目 / 诊断行）：切主题要重排上色，故留原文
        #: 而不是渲染后的 HTML（HTML 的颜色烘死在字符串里，重排等于没换色）。
        self._log_entries: list[dict] = []
        # 「已启动过/正在会话」与「输入区可发话」拆开（评审 #1）：
        # _session_active 在首次 start_session 起即为真；_started 在首次成功开场
        # (sig_scene_info) 后为真且保持到换场——自然收束只停输入，绝不动这两者，
        # 使「开新场/切模型」在收束后依旧可用。
        self._session_active = False
        self._started = False
        self._finished = False
        self._paused = False
        #: worker 的成本守卫自动暂停旗标（机器可读，sig_auto_paused）：暂停的**缘起**是
        #: 守卫还是用户手动——决定状态药丸用不用琥珀色，也决定「点继续」这句文案该不该
        #: 被当成暂停信号（旧实现就是靠子串猜中文，见 _on_status）。
        self._auto_paused = False
        self._worker_status = "就绪"   # 最近一次 worker 状态（收束按钮文案以其终态为准）
        self.setMinimumSize(1000, 640)
        self.resize(1200, 760)
        self.setWindowTitle(self._t.t("app.title"))

        root = QWidget()
        root.setObjectName("AppRoot")
        # 配色在**显示窗口前**就按保存的设置铺好（起手即是用户上次的观感，不闪一下默认色）。
        self.setStyleSheet(theme_qss(self._theme))
        # 主题也写进 app 属性：库里的弹窗（无窗口上下文）按它取当前主题出样式表。
        app = QApplication.instance()
        if app is not None:
            app.setProperty("theme", self._theme)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setChildrenCollapsible(False)
        self._splitter.addWidget(self._build_left())
        self._splitter.addWidget(self._build_center())
        self._splitter.addWidget(self._build_right())
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setStretchFactor(2, 0)
        self._splitter.setSizes([280, 640, 300])
        outer.addWidget(self._splitter)
        self.setCentralWidget(root)

        self._connect_worker()
        # 设置里的 api 配置在**开场前**就推给 worker（§2.1①）：worker 只是记下期望值
        # （此刻线程还没起），第一场建引擎时即带上——保存过的 url/key/模型名从此真正生效。
        self._push_api_config()
        # 推进活跃度也在开场前推给 worker（与 api 配置同款）：worker 记期望值，第一场
        # 建引擎时即带上——「上次调到多高」从此开局就生效，不必再动一次控件。
        self.apply_narrate_activity(self._narrate_activity, persist=False)
        self._enable_input(False)
        self._set_status_chip("就绪")
        self._set_model_badge("stub")
        # 顶栏菜单最后建（各部件已在位）：勾选态按**已加载的设置**对齐，且不回写盘。
        self._build_menus()
        # 起手是「未打开场景」：中栏显空状态页（app.py 不再先弹开场设置窗，用户见到的
        # 第一屏就是它）。放在菜单建好之后调——它会顺手把场景相关的菜单项置灰。
        self._show_empty_state()
        # 全部部件与菜单到位后再整屏重译一次：apply_language 在菜单半成品时就跑过一回，
        # 这里补齐后半段（场景/角色菜单项、动态子菜单）。
        self._retranslate()

    # ============================================================ 语言重译（§7）
    def _reg(self, widget, key: str, *, dynamic: bool = False):
        """登记一枚静态文案部件（label/button/checkbox 都有 setText）→ 按当前语言设文本。

        登记过的部件在每次 _retranslate 时按 key 重设文本；dynamic=True 进右栏专用表
        （角色卡重建时整批作废，见 _rebuild_right_cards）。
        """
        widget.setText(self._t.t(key))
        (self._dyn_texts if dynamic else self._i18n_texts).append((widget, key))
        return widget

    def _reg_placeholder(self, widget, key: str):
        """登记一枚**占位文本**（QLineEdit.setPlaceholderText）→ 切语言时随 _retranslate 重设。"""
        widget.setPlaceholderText(self._t.t(key))
        self._i18n_placeholders.append((widget, key))
        return widget

    def _reg_tooltip(self, widget, key: str):
        """登记一枚**tooltip**（setToolTip）→ 切语言时随 _retranslate 重设。

        只登记**静态**提示；随状态变的 tooltip（如「真实模型」开关的禁用原因）走各自的
        刷新函数（见 _refresh_live_check），不在这里登记，免得把动态文案钉成静态串。
        """
        widget.setToolTip(self._t.t(key))
        self._i18n_tooltips.append((widget, key))
        return widget

    def _pstyle(self, widget, role: str, *, group: str = "static", **extra):
        """按**语义角色**给部件上色并登记（G1）：切主题时按新色表整批重刷。

        颜色不能在建窗那一刻烘死——否则深色/深蓝主题下这些标签仍是默认配色的深字。
        数值条这类还要额外参数的（"bar" 的角色色）走 extra。
        group 标明部件属于哪个容器（static/urge/card）：随容器重建整批作废，见
        _repopulate_left_urge / _rebuild_right_cards。
        speaker=<角色名> 表示这枚部件的强调色是**该角色的说话者色**：切主题时说话者
        配色盘也会换（深底两套用提亮色），届时就地按新色重算（见 _apply_palette_styles）。
        """
        widget.setStyleSheet(_label_style(role, self._pal, **extra))
        self._palette_groups.setdefault(group, []).append(
            (widget, role, dict(extra)))
        return widget

    def _speaker_palette(self) -> list[str]:
        """当前主题该用的说话者配色盘（四套各一套：浅底用深饱和色、深底用提亮色）。"""
        return speaker_palette(self._theme)

    def _apply_palette_styles(self) -> None:
        """按当前色表重刷全部登记过的程序化配色部件；顺手剔除已销毁的（卡片/行重建）。

        带 speaker 的部件（色点/头像/数值条）还要用**当前**说话者配色重算强调色——
        切主题时配色盘本身也换了，不能留着旧主题的说话者色。
        """
        for group, entries in list(self._palette_groups.items()):
            alive: list[tuple[object, str, dict]] = []
            for widget, role, extra in entries:
                kwargs = dict(extra)
                speaker = kwargs.pop("speaker", None)
                if speaker is not None:
                    kwargs["accent"] = self._color_of(str(speaker))
                try:
                    widget.setStyleSheet(_label_style(role, self._pal, **kwargs))
                except RuntimeError:      # 部件已被 deleteLater → 丢弃这条登记
                    continue
                alive.append((widget, role, extra))
            self._palette_groups[group] = alive

    def _retranslate(self) -> None:
        """按当前语言重设**全部静态文案**（§7）。

        覆盖：窗口标题、中栏页眉、菜单标题与各项、卡片标题、按钮/开关、左右栏静态标签、
        占位文本、状态药丸词表。幂等且可随时调用（构造末尾、切语言、换场改标题后）。
        动态内容（对白/叙述/think 日志/角色名/数值）不在此列——它们是**内容**不是界面文案。
        """
        t = self._t
        self.setWindowTitle(f"{self._scene_name} · {t.t('app.title')}"
                            if self._scene_name else t.t("app.title"))
        header = getattr(self, "_header_scene", None)
        if header is not None:
            header.setText(self._scene_name or t.t("panel.dialogue"))
            # 没打开场景时中栏是空状态页 → 页眉（「自主对话中…」）不占位，免得像在说谎。
            header.setVisible(bool(self._scene_name))
        self._retranslate_menus()
        for registry, apply in ((list(self._i18n_texts) + list(self._dyn_texts),
                                 lambda w, text: w.setText(text)),
                                (self._i18n_placeholders,
                                 lambda w, text: w.setPlaceholderText(text)),
                                (self._i18n_tooltips,
                                 lambda w, text: w.setToolTip(text))):
            for widget, key in list(registry):
                try:
                    apply(widget, t.t(key))
                except RuntimeError:      # 部件已被 deleteLater（角色卡重建）→ 跳过
                    continue
        self._retranslate_live_state()

    def _retranslate_live_state(self) -> None:
        """重译**当前态**决定的文案：后端药丸、日志开关、暂停/继续/收束按钮、状态药丸。

        这些不是固定静态串（随会话状态变），故从窗口自己的镜像状态重算，而不是登记表。
        """
        t = self._t
        self._set_model_badge(self._backend)
        log_btn = getattr(self, "_log_btn", None)
        if log_btn is not None:
            log_btn.setText(t.t("btn.log_on") if log_btn.isChecked() else t.t("btn.log"))
        pause_btn = getattr(self, "_pause_btn", None)
        if pause_btn is not None:
            pause_btn.setText(t.t(self._pause_button_key()))
        # 「真实模型」的原因提示随语言走（它不在 _i18n_tooltips 里：随 key 有无而变）。
        self._refresh_live_check()
        # 推进活跃度的四个档位标签（QComboBox 的条目文本不是 setText，登记表覆盖不到）。
        self._retranslate_activity_combo()
        self._set_status_chip(self._status_raw, style=self._status_style)

    def _pause_button_key(self) -> str:
        """暂停按钮当前该显示的文案键（终态 → 已收束/已停止；暂停中 → 继续；否则 → 暂停）。"""
        if self._finished:
            return ("status.stopped" if self._worker_status == "已停止"
                    else "status.closed")
        return "btn.continue" if self._paused else "btn.pause"

    def _retranslate_menus(self) -> None:
        """重设菜单标题与（能静态给出的）菜单项文本；动态子菜单重扫一次即按新语言出字。"""
        t = self._t
        for menu, key in ((getattr(self, "_menu_settings", None), "menu.settings"),
                          (getattr(self, "_menu_scene", None), "menu.scene"),
                          (getattr(self, "_menu_character", None), "menu.character"),
                          (getattr(self, "_theme_menu", None), "menu.theme"),
                          (getattr(self, "_language_menu", None), "menu.language"),
                          (getattr(self, "_autosave_menu", None), "menu.autosave"),
                          (getattr(self, "_open_scene_menu", None), "menu.open_scene"),
                          (getattr(self, "_add_char_menu", None), "menu.add_character"),
                          (getattr(self, "_remove_char_menu", None), "menu.remove_character")):
            if menu is not None:
                menu.setTitle(t.t(key))
        for action, key in ((getattr(self, "_action_api_config", None), "menu.api_config"),
                            (getattr(self, "_action_configure_scene", None), "menu.configure_scene"),
                            (getattr(self, "_action_save_scene", None), "menu.save_scene"),
                            (getattr(self, "_action_manage_scenes", None), "menu.manage_scenes"),
                            (getattr(self, "_action_new_scene", None), "menu.new_scene"),
                            (getattr(self, "_action_import_scene", None), "menu.import_scene"),
                            (getattr(self, "_action_reset_scene", None), "menu.reset_scene"),
                            (getattr(self, "_action_new_character", None), "menu.new_character"),
                            (getattr(self, "_action_import_character", None), "menu.import_character"),
                            (getattr(self, "_action_manage_characters", None), "menu.manage_characters"),
                            (getattr(self, "_action_advanced_cast", None), "menu.advanced_cast")):
            if action is not None:
                action.setText(t.t(key))
        action = getattr(self, "_action_manage_scenes", None)
        if action is not None:
            action.setToolTip(t.t("tip.manage_scenes"))
        action = getattr(self, "_action_configure_scene", None)
        if action is not None:
            action.setToolTip(t.t("tip.configure_scene"))
        action = getattr(self, "_action_save_scene", None)
        if action is not None:
            action.setToolTip(t.t("tip.save_scene_menu"))
        action = getattr(self, "_action_import_scene", None)
        if action is not None:
            action.setToolTip(t.t("tip.import_scene"))
        action = getattr(self, "_action_import_character", None)
        if action is not None:
            action.setToolTip(t.t("tip.import_character"))
        action = getattr(self, "_action_new_scene", None)
        if action is not None:
            action.setToolTip(t.t("tip.new_scene"))
        action = getattr(self, "_action_new_character", None)
        if action is not None:
            action.setToolTip(t.t("tip.new_character"))
        action = getattr(self, "_action_manage_characters", None)
        if action is not None:
            action.setToolTip(t.t("tip.manage_characters"))
        action = getattr(self, "_action_advanced_cast", None)
        if action is not None:
            action.setToolTip(t.t("tip.advanced_cast"))
        action = getattr(self, "_action_reset_scene", None)
        if action is not None:
            action.setToolTip(t.t("tip.reset_scene"))
        for every, act in getattr(self, "_autosave_actions", {}).items():
            act.setText(t.t("menu.autosave_every", n=every))
        # 动态子菜单（场景库 / 添加·移出角色）重扫一次：项文本与禁用理由按新语言重建。
        if getattr(self, "_open_scene_menu", None) is not None:
            self._refresh_scene_menu()
        if getattr(self, "_add_char_menu", None) is not None:
            self._refresh_cast_menus()

    # ================================================================== 菜单栏
    def _build_menus(self) -> None:
        """建顶栏：`设置 / 场景 / 角色`（顺序固定，见设计文档 §2.1）。"""
        bar = self.menuBar()
        self._menu_settings = bar.addMenu(self._t.t("menu.settings"))
        self._menu_scene = bar.addMenu(self._t.t("menu.scene"))
        self._menu_character = bar.addMenu(self._t.t("menu.character"))
        self._build_settings_menu(self._menu_settings)
        self._build_scene_menu(self._menu_scene)
        self._build_character_menu(self._menu_character)

    def _build_settings_menu(self, menu: QMenu) -> None:
        """设置菜单：模型 api 配置… + 配色/语言/自动保存周期三组互斥勾选子菜单。"""
        self._action_api_config = QAction(self._t.t("menu.api_config"), self)
        self._action_api_config.triggered.connect(self._on_api_config)
        menu.addAction(self._action_api_config)
        menu.addSeparator()

        # ---- 配色（THEMES 四项，互斥）----
        self._theme_menu = menu.addMenu(self._t.t("menu.theme"))
        self._theme_group = QActionGroup(self)
        self._theme_group.setExclusive(True)
        self._theme_actions: dict[str, QAction] = {}
        for theme in THEMES:
            act = QAction(theme, self)
            act.setCheckable(True)
            act.setData(theme)
            act.triggered.connect(
                lambda _checked=False, t=theme: self.apply_theme(t))
            self._theme_group.addAction(act)
            self._theme_menu.addAction(act)
            self._theme_actions[theme] = act

        # ---- 语言（7 种，标签＝母语名，互斥）----
        self._language_menu = menu.addMenu(self._t.t("menu.language"))
        self._language_group = QActionGroup(self)
        self._language_group.setExclusive(True)
        self._language_actions: dict[str, QAction] = {}
        for code, native in LANGUAGES.items():
            act = QAction(native, self)
            act.setCheckable(True)
            act.setData(code)
            act.triggered.connect(
                lambda _checked=False, c=code: self.apply_language(c))
            self._language_group.addAction(act)
            self._language_menu.addAction(act)
            self._language_actions[code] = act

        # ---- 自动保存周期（5/20/50/100 轮，互斥）----
        self._autosave_menu = menu.addMenu(self._t.t("menu.autosave"))
        self._autosave_group = QActionGroup(self)
        self._autosave_group.setExclusive(True)
        self._autosave_actions: dict[int, QAction] = {}
        for every in AUTOSAVE_EVERY:
            act = QAction(self._t.t("menu.autosave_every", n=int(every)), self)
            act.setCheckable(True)
            act.setData(int(every))
            act.triggered.connect(
                lambda _checked=False, n=int(every): self.apply_autosave(n))
            self._autosave_group.addAction(act)
            self._autosave_menu.addAction(act)
            self._autosave_actions[int(every)] = act

        # 勾选态 = 已加载的设置（**不**回写盘：这只是镜像启动值）。
        self.apply_theme(self._theme, persist=False)
        self.apply_language(self._language, persist=False)
        self.apply_autosave(self._autosave_every, persist=False)

    def _build_scene_menu(self, menu: QMenu) -> None:
        """场景菜单（顺序固定，§3.3）：打开场景 / 配置场景… / 保存场景 / 重置运行上下文… /
        管理场景库… / 新建场景 / 导入场景… / 分隔线（以后可扩展）。
        """
        self._open_scene_menu = menu.addMenu(self._t.t("menu.open_scene"))
        self._open_scene_menu.setToolTip(self._t.t("tip.open_scene_submenu"))
        # 每次展开都重扫目录：新建/删除场景后不必重启界面。
        self._open_scene_menu.aboutToShow.connect(self._refresh_scene_menu)
        self._refresh_scene_menu()

        # 「配置场景…」= 开**当前场景**的编辑器（与中栏那枚按钮同一个动作，§3.4）。
        self._action_configure_scene = QAction(self._t.t("menu.configure_scene"), self)
        self._action_configure_scene.setToolTip(self._t.t("tip.configure_scene"))
        self._action_configure_scene.setEnabled(False)   # 没有打开的场次就没什么可配置
        self._action_configure_scene.triggered.connect(self._on_configure_scene)
        menu.addAction(self._action_configure_scene)

        # 「保存场景」与中栏那枚按钮同源（worker.save_scene 落 sidecar）。
        self._action_save_scene = QAction(self._t.t("menu.save_scene"), self)
        self._action_save_scene.setToolTip(self._t.t("tip.save_scene_menu"))
        self._action_save_scene.setEnabled(False)        # 开场成功后才可用（没场次无可存）
        self._action_save_scene.triggered.connect(self._on_save_scene)
        menu.addAction(self._action_save_scene)

        # 「重置场景运行上下文…」（§3.4/§5）：清对话与思考上下文，**保留**配置与在场角色。
        # 与场景编辑器里那个「重置场景」（清的是场景文件里的入场记录）是两回事，故这里
        # 用带「运行上下文」的措辞，并单独确认——误点不该把一场对话清空。
        self._action_reset_scene = QAction(self._t.t("menu.reset_scene"), self)
        self._action_reset_scene.setToolTip(self._t.t("tip.reset_scene"))
        self._action_reset_scene.setEnabled(False)      # 开场成功后才可用（没场次无可重置）
        self._action_reset_scene.triggered.connect(self._on_reset_scene)
        menu.addAction(self._action_reset_scene)

        # 「管理场景库…」= 场景库弹窗（只列场景：打开/编辑/复制/删除/新建/导入）。
        self._action_manage_scenes = QAction(self._t.t("menu.manage_scenes"), self)
        self._action_manage_scenes.setToolTip(self._t.t("tip.manage_scenes"))
        self._action_manage_scenes.triggered.connect(self._on_open_library)
        menu.addAction(self._action_manage_scenes)

        self._action_new_scene = QAction(self._t.t("menu.new_scene"), self)
        self._action_new_scene.setToolTip(self._t.t("tip.new_scene"))
        self._action_new_scene.triggered.connect(self._on_new_scene)
        menu.addAction(self._action_new_scene)

        # 「导入场景…」：选一个按模板填好的 .md → 解析入库 → 报结果（未填字段照实列出）。
        self._action_import_scene = QAction(self._t.t("menu.import_scene"), self)
        self._action_import_scene.setToolTip(self._t.t("tip.import_scene"))
        self._action_import_scene.triggered.connect(
            lambda _checked=False: self._on_import_template("scene"))
        menu.addAction(self._action_import_scene)

        menu.addSeparator()      # 以后可扩展（新增的场景项加在这条线下面）

    def _build_character_menu(self, menu: QMenu) -> None:
        """角色菜单（顺序固定，§2.1③/§3.3）：添加角色 / 移出角色 / 新建角色 / 导入角色… /
        管理角色… / 高级移入·移出…。

        「添加角色」「移出角色」是两个**动态子菜单**（每次展开前重扫角色库与在场名单：
        添加列库里还不在场的人、移出列在场者）；没有打开的场次就整项禁用并给出原因
        （tooltip）。点击一律走 worker 的人事 API——界面绝不直接调引擎。
        """
        self._add_char_menu = menu.addMenu(self._t.t("menu.add_character"))
        self._add_char_menu.aboutToShow.connect(self._refresh_cast_menus)
        self._action_add_character = self._add_char_menu.menuAction()
        self._remove_char_menu = menu.addMenu(self._t.t("menu.remove_character"))
        self._remove_char_menu.aboutToShow.connect(self._refresh_cast_menus)
        self._action_remove_character = self._remove_char_menu.menuAction()

        self._action_new_character = QAction(self._t.t("menu.new_character"), self)
        self._action_new_character.setToolTip(self._t.t("tip.new_character"))
        self._action_new_character.triggered.connect(self._on_new_character)
        menu.addAction(self._action_new_character)

        # 「导入角色…」：与「导入场景…」同一流程，只是落进**角色库**目录。
        self._action_import_character = QAction(self._t.t("menu.import_character"), self)
        self._action_import_character.setToolTip(self._t.t("tip.import_character"))
        self._action_import_character.triggered.connect(
            lambda _checked=False: self._on_import_template("character"))
        menu.addAction(self._action_import_character)

        self._action_manage_characters = QAction(self._t.t("menu.manage_characters"), self)
        self._action_manage_characters.setToolTip(self._t.t("tip.manage_characters"))
        self._action_manage_characters.triggered.connect(self._on_manage_characters)
        menu.addAction(self._action_manage_characters)

        self._action_advanced_cast = QAction(self._t.t("menu.advanced_cast"), self)
        self._action_advanced_cast.setToolTip(self._t.t("tip.advanced_cast"))
        self._action_advanced_cast.triggered.connect(self._on_advanced_cast)
        menu.addAction(self._action_advanced_cast)

        self._refresh_cast_menus()

    # ------------------------------------------------------- 演员表菜单（动态）
    def _disabled_item(self, text: str, tip: str = "") -> QAction:
        """菜单里的禁用占位项（没有可选对象时用，别让菜单看起来是空的）。

        QAction 必须挂到 self 名下（PySide 不会替无父的临时 QAction 接管所有权，
        局部变量一回收菜单项就凭空消失）。
        """
        act = QAction(text, self)
        act.setEnabled(False)
        if tip:
            act.setToolTip(tip)
        return act

    def _refresh_cast_menus(self) -> None:
        """重建「添加角色 / 移出角色」子菜单，并按当前状态（can_cast + 是否收到演员表）
        开关这两项——没有打开的场次时禁用并说明原因，而不是让点了没反应。
        """
        active = self._active_names()
        active_set = set(active)

        t = self._t
        add_menu = self._add_char_menu
        add_menu.clear()
        for _path, name in self._character_cards():
            if name in active_set:
                continue                    # 已在场的人不出现在「添加」里
            act = QAction(name, self)
            act.setData(str(_path))
            act.setToolTip(t.t("tip.add_character", name=name))
            act.triggered.connect(lambda _c=False, n=name: self._on_add_character(n))
            add_menu.addAction(act)
        if add_menu.isEmpty():
            add_menu.addAction(self._disabled_item(
                t.t("menu.no_addable_characters"), t.t("tip.no_addable_characters")))

        remove_menu = self._remove_char_menu
        remove_menu.clear()
        for name in active:
            act = QAction(name, self)
            act.setToolTip(t.t("tip.remove_character", name=name))
            act.triggered.connect(lambda _c=False, n=name: self._on_remove_character(n))
            remove_menu.addAction(act)
        if remove_menu.isEmpty():
            remove_menu.addAction(self._disabled_item(t.t("menu.no_active_characters")))

        self._sync_cast_menu_state()

    def _sync_cast_menu_state(self) -> None:
        """「添加/移出角色」的可用态：没开场（worker.can_cast() 假）或还没收到演员表
        （sig_cast 未到）就禁用，并用 tooltip 说明为什么点不了。
        """
        t = self._t
        can_cast = bool(self._worker.can_cast()) if self._worker is not None else False
        known = can_cast and self._cast is not None
        self._action_add_character.setEnabled(known)
        self._action_add_character.setToolTip(
            "" if known else t.t("tip.cast_needs_scene"))
        has_active = known and bool(self._active_names())
        self._action_remove_character.setEnabled(has_active)
        self._action_remove_character.setToolTip(
            "" if has_active else (t.t("tip.cast_no_active") if known
                                   else t.t("tip.cast_needs_scene_short")))

    def _active_names(self) -> list[str]:
        """当前在场名单（sig_cast 镜像；还没收到过演员表 → []）。"""
        return [str(n) for n in ((self._cast or {}).get("active") or [])]

    def _inactive_names(self) -> list[str]:
        """已移出本场、但记录与历史仍保留者（sig_cast 镜像；顺序 = 移出顺序）。"""
        return [str(n) for n in ((self._cast or {}).get("inactive") or [])]

    def _muted_map(self) -> dict:
        """{名字: 剩余块数|None}（None = 永久禁言）——只含在场者（引擎口径）。"""
        return dict((self._cast or {}).get("muted") or {})

    def _color_of(self, name: str) -> str:
        """角色名 → 配色：已分配过就沿用（对白/左栏/右栏三处同色），新面孔续取一枚。

        开场按 sig_scene_info 的 characters 一次性建表；运行期新加入的人不在那张表里，
        就按「已分配过的人数」续取一枚并记下——保证同一个人左右栏颜色一致。
        """
        key = str(name)
        color = self._name_colors.get(key)
        if color is None:
            color = color_for(len(self._name_colors), self._speaker_palette())
            self._name_colors[key] = color
        return color

    # -------------------------------------------------------------- 设置动作
    def apply_theme(self, theme: str, *, persist: bool = True) -> None:
        """切配色：重铺样式表 + **重刷内容**（HTML/程序化标签）+ 勾中该项（+ 落盘）。

        内容必须一起重来：对白区是 setHtml 渲染的（颜色在渲染那一刻烘进文档），左右栏
        标签的颜色也是 setStyleSheet 手工上的——只换窗口样式表的话，深色主题下内容仍是
        默认配色的深字。故这里换色表 → 重刷登记过的标签 → 整屏重绘对白区。

        已经打开的**其它**弹窗保持旧配色（Qt 不做全局重刷）——新开的弹窗才用新主题。
        """
        name = theme if theme in THEMES else THEMES[0]
        self._theme = name
        self._pal = palette_for(name)
        self.setStyleSheet(theme_qss(name))
        app = QApplication.instance()
        if app is not None:               # 之后 new 出来的弹窗按它出主题样式
            app.setProperty("theme", name)
        # 说话者配色盘也随主题换（深底两套是提亮色）：按原来的名字顺序重算同一批人的颜色，
        # 再重刷样式/重绘对白——三处（对白 HTML / 左栏冲动行 / 右栏卡）才会同色。
        self._name_colors = assign_name_colors(list(self._name_colors),
                                               self._speaker_palette())
        self._apply_palette_styles()
        self._set_model_badge(self._backend)   # 药丸底色/字色也随主题换（不再是烘死的色）
        self._rerender_conversation()     # 对白/日志的颜色是渲染时烘死的 → 重绘
        self._rerender_log()
        act = self._theme_actions.get(name)
        if act is not None and not act.isChecked():
            act.setChecked(True)          # 互斥组自动取消同组其它项
        if persist:
            self._store.update(theme=name)

    def _rerender_log(self) -> None:
        """按**结构化留存**重绘日志窗格（think / 诊断 / 隐式事件行）。

        日志行的颜色同样是渲染时烘死的，故留存原始条目、切主题时按新色表重排一遍
        （存渲染后的 HTML 会把旧颜色一起存下来，重排等于没换）。
        """
        self._log_view.clear()
        for item in self._log_entries:
            self._log_view.append(self._render_log_item(item))
        bar = self._log_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def apply_language(self, code: str, *, persist: bool = True) -> None:
        """切语言：**立即重译界面** + 勾中该项 + 写 app 属性 + 广播 + 告诉引擎（+ 落盘）。

        三件事一次做全（§7「切语言界面与台词同步变化」）：
        ① 界面：`self._t` 换语言 → `_retranslate()` 把全部静态文案重设为新语言；
        ② 其它模块：写 app 属性 `language`（library 弹窗按它取文案）+ 发 `language_changed`；
        ③ 引擎：调 worker.set_language(code)（防御式 getattr——worker 侧可能还没落地），
           由它把「你将使用<语言>回答。」注入所有提示词，让台词/叙述/判定同步换语言。
        未知码回落默认语言。
        """
        code = code if code in LANGUAGES else DEFAULT_LANGUAGE
        self._language = code
        self._t.set_language(code)
        act = getattr(self, "_language_actions", {}).get(code)
        if act is not None and not act.isChecked():
            act.setChecked(True)
        app = QApplication.instance()
        if app is not None:               # 应用级属性：任何模块都能读到当前语言
            app.setProperty("language", code)
        self._retranslate()
        self.language_changed.emit(code)
        if persist:
            self._store.update(language=code)
        # 语言也送到引擎侧（防御式：worker 可能还没实现 set_language）。
        setter = getattr(self._worker, "set_language", None)
        if callable(setter):
            setter(code)

    def apply_autosave(self, every: int, *, persist: bool = True) -> None:
        """切自动保存周期：勾中该项 + 落盘 + **投给 worker**（真落盘由引擎在块末做）。

        0 = 关闭（设置文件里不出现，但 worker/引擎侧照样认——keep 0/disabled 可用）；
        值记得住、读得到（self._autosave_every 与 settings.json 两侧都有），并且立刻就
        告诉 worker：新周期下一块起生效，重开/切场也带着走（worker 记期望值）。
        """
        value = int(every) if int(every) in AUTOSAVE_EVERY else AUTOSAVE_EVERY[0]
        self._autosave_every = value
        act = self._autosave_actions.get(value)
        if act is not None and not act.isChecked():
            act.setChecked(True)
        if persist:
            self._store.update(autosave_every=value)
        # 防御式（与 apply_language 同）：替身 worker / 早期版本可能还没这个方法。
        setter = getattr(self._worker, "set_autosave_every", None)
        if callable(setter):
            setter(value)

    def apply_narrate_activity(self, level: float, *, persist: bool = True) -> None:
        """切推进活跃度（§6.2）：吸附到四档 + 对齐控件 + 落盘 + **投给 worker**。

        吸附口径统一走 settings.snap_narrate_activity（四档外就近；非数字回落 0.5），故
        这里接什么都落在一个合法档上——控件、设置文件、worker 三处永远不会各说各话。
        persist=False 用于启动回读（值是设置里来的，不该反手再写一次盘）。
        """
        value = snap_narrate_activity(level)
        self._narrate_activity = value
        self._select_activity(value)
        if persist:
            self._store.update(narrate_activity=value)
        # 防御式（与 apply_autosave 同）：替身 worker / 早期版本可能还没这个方法。
        setter = getattr(self._worker, "set_narrate_activity", None)
        if callable(setter):
            setter(value)

    def _select_activity(self, level: float) -> None:
        """把控件切到该档（阻塞信号：这是镜像同步，不该反手再投一次给 worker）。"""
        combo = getattr(self, "_activity_combo", None)
        if combo is None:
            return
        idx = self._activity_index(level)
        if idx >= 0 and combo.currentIndex() != idx:
            combo.blockSignals(True)
            combo.setCurrentIndex(idx)
            combo.blockSignals(False)

    @staticmethod
    def _activity_index(level: float) -> int:
        """档位数值 → 控件下标（脏值/不在档上给 -1，调用方自行回落）。"""
        for i, (_key, value) in enumerate(NARRATE_ACTIVITY_LEVELS):
            if abs(float(level) - value) < 1e-9:
                return i
        return -1

    def _on_activity_changed(self, index: int) -> None:
        """控件改档 → 落盘 + 投 worker（引擎侧据此缩放触发线与冷却，下一块起生效）。"""
        if 0 <= index < len(NARRATE_ACTIVITY_LEVELS):
            self.apply_narrate_activity(NARRATE_ACTIVITY_LEVELS[index][1])

    def _mirror_narrate_activity(self, st: dict) -> None:
        """worker 的叙述状态载荷带了当前活跃度 → 以此为准对齐控件（不落盘、不回头再投）。

        换场/重建引擎后 worker 手里的期望值才是真相（它可能来自设置，也可能来自上一场
        的运行期改动），界面据此保持诚实；载荷没带这个字段（旧 worker）就维持原样。
        """
        raw = st.get("activity", st.get("narrate_activity"))
        if raw is None or isinstance(raw, bool):
            return
        try:
            value = snap_narrate_activity(float(raw))
        except (TypeError, ValueError):
            return
        self._narrate_activity = value
        self._select_activity(value)

    def _retranslate_activity_combo(self) -> None:
        """按当前语言重设活跃度控件的**四个档位标签**（下标与档位数值不变）。"""
        combo = getattr(self, "_activity_combo", None)
        if combo is None:
            return
        for i, (key, _value) in enumerate(NARRATE_ACTIVITY_LEVELS):
            if i < combo.count():
                combo.setItemText(i, self._t.t(key))

    def _push_api_config(self) -> None:
        """把**保存过的** api 配置推给 worker（§2.1①）：url / key / 模型名三项。

        在启动与「模型 api 配置」弹窗保存后各推一次，worker 只是记期望值，下次建引擎
        （开场/重开/切场）即生效。key 只在内存里流转：不进日志、不进场景载荷、不落任何
        文件（settings.json 的读写归 SettingsStore，窗口只读它）。
        """
        s = self._settings
        setter = getattr(self._worker, "set_api_config", None)
        if callable(setter):
            setter(s.api_base_url, s.api_key, s.model_name)

    def _api_key(self) -> str | None:
        """本次运行可用的 api key：**设置里的优先**（§2.1① 设置是主来源），CLI/环境变量的
        那份（AppConfig.api_key）只作回落。空串一律当没有（= 这一场走 stub）。"""
        return (self._settings.api_key.strip()
                or str(self._cfg.api_key or "").strip() or None)

    def _refresh_live_check(self) -> None:
        """按**当前**是否有 key 刷新「真实模型」开关的可用态与原因提示（G4）。

        判据只有一处（`_api_key()`：设置优先、CLI/环境变量回落），故建窗、保存 api 配置、
        切语言三处都调它——用户在 设置→模型 api 配置 里存过 key 后不必重启，开关当场可用、
        悬停提示里的「未检测到 key」也当场消失。
        """
        check = getattr(self, "_live_check", None)
        if check is None:
            return
        if self._api_key():
            check.setEnabled(True)
            check.setToolTip("")
        else:
            check.setEnabled(False)
            check.setToolTip(self._t.t("tip.no_api_key"))

    def _on_api_config(self) -> None:
        """设置 →「模型 api 配置…」：用当前设置预填弹窗，保存后刷新本地快照并推给 worker。

        非模态安全：exec() 只跑嵌套事件循环，worker 线程与其跨线程信号照常被派发，
        开弹窗期间角色不会停演。
        """
        dlg = ApiConfigDialog(self._settings, self, store=self._store,
                              theme=self._theme)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.saved_settings:
            self._settings = dlg.saved_settings
            # 保存即生效：推给 worker（下一次建引擎带上），用户不必重启软件。
            self._push_api_config()
            # 刚存了 key → 「真实模型」开关当场解锁并清掉「未检测到 key」的旧提示（G4）。
            self._refresh_live_check()

    # -------------------------------------------------------------- 场景动作
    def _refresh_scene_menu(self) -> None:
        """重建「打开场景」子菜单：`list_scene_paths(场景库目录)` 逐个读 name 当标签。

        读不出来的文件（坏 JSON/非字典/没名字）也照样列出来，标签＝「（坏文件）」+ 文件名
        ——点了照样走 switch_scene，由那里的校验给出具体原因（不静默、不假装能开）。
        """
        menu = self._open_scene_menu
        menu.clear()
        for path in self._scene_paths():
            name, ok = self._scene_label(path)
            act = QAction(name if ok else self._t.t("menu.bad_scene", name=name), self)
            act.setData(str(path))
            act.setToolTip(str(path) if ok
                           else self._t.t("tip.bad_scene_file", path=path))
            act.triggered.connect(
                lambda _checked=False, p=path: self._open_scene_from_menu(p))
            menu.addAction(act)
        if menu.isEmpty():
            empty = QAction(self._t.t("menu.no_scenes"), self)
            empty.setEnabled(False)
            menu.addAction(empty)

    def _scene_paths(self) -> list[Path]:
        """场景库里的场景文件（目录不存在/无权限 → []，菜单照常可用）。"""
        try:
            return list(list_scene_paths(self._scenes_dir()))
        except OSError:
            return []

    @staticmethod
    def _scene_label(path: Path) -> tuple[str, bool]:
        """场景文件 → (菜单标签, 是否读得出来源信息)：name 缺失/坏文件回落文件名。"""
        try:
            data = load_json(path)
        except (OSError, ValueError):
            return path.stem, False
        if not isinstance(data, dict):
            return path.stem, False
        name = str(data.get("name") or "").strip()
        return (name, True) if name else (path.stem, False)

    def _character_cards(self) -> list[tuple[Path, str]]:
        """角色库目录 → [(卡路径, 卡名)]（坏卡用文件名当名字；目录缺失 → []）。"""
        return _scan_character_cards(self._characters_dir())

    def _characters_for_scene(self, scene_path: Path) -> list[Path]:
        """按**场景自己的名单**去角色库里配卡（顺序＝场景声明顺序，§3.1 场景持有阵容）。

        走 `load_scene`（权威字段是 characters，旧的 participants 由 schema 自动迁移），
        故新老场景文件都认——**不要求用户先选人**，空名单就是合法的空场（返回 []）。
        配不上的（卡不在库里、坏文件）不进名单：切场前的 `_validate_scene_materials`
        会指明是谁缺卡并拒绝切换，绝不带着半套素材开场。
        """
        try:
            scene = load_scene(scene_path)
        except (OSError, ValueError):
            return []
        by_name = dict((name, path) for path, name in self._character_cards())
        out: list[Path] = []
        for name in scene.participants:
            path = by_name.get(str(name))
            if path is not None and path not in out:
                out.append(path)
        return out

    def _open_scene_from_menu(self, scene_path: Path) -> None:
        """「打开场景」子菜单点击：直接走窗口既有的切场路径（switch_scene → 校验 + 重投
        开场），不另写一套切换逻辑。阵容取**场景自己的**名单在库中配到的卡（§3.1）。
        """
        self.switch_scene(Path(scene_path))

    def _on_new_scene(self) -> None:
        """场景 →「新建场景」（空状态页那枚按钮同此）：空白场景编辑器 → 存盘（编辑器内部走
        loaders.save_scene）→ 切到该场（同样经 switch_scene 的校验与重投开场）。"""
        dlg = SceneEditorDialog(None, self._scenes_dir(), self._characters_dir(), self)
        if dlg.exec() != QDialog.DialogCode.Accepted or dlg.saved_path is None:
            return
        path = Path(dlg.saved_path)
        self._refresh_scene_menu()            # 新场立刻出现在「打开场景」里
        self.switch_scene(path)

    def _on_empty_open_scene(self) -> None:
        """空状态页「打开场景」：开场景库弹窗（只列场景）→ 选中即走 switch_scene。

        「取消」或没选场景 → 什么都不换（仍然停在空状态页）。这是 `_on_open_library`
        的同一条路，只是从第一屏进来。
        """
        self._on_open_library()

    def _on_configure_scene(self) -> None:
        """「配置场景」（§3.4）：开**当前场景**的编辑器 → 落盘 → 就地应用到运行中的场景。

        没有打开的场次（或本场没有可写的场景文件）就只给一句状态说明，绝不假装能配置。
        编辑器保存即写盘（`SceneEditorDialog.save`），这里读回场景对象后：
          ① 把可热更新的字段推给 worker（`apply_scene_config`，缺这个方法就只落盘并说明）；
          ② 阵容按增删处理（worker 的 add_character / remove_character，界面绝不直接调引擎）；
          ③ 左栏场景卡按新配置刷新（改名/改日期当场可见）。
        """
        if not self._scene_open:
            self._set_status_chip(self._t.t("err.no_open_scene"),
                                  style=("#fef3c7", "#92400e"))
            return
        if self._scene_path is None:
            # 场次开着但没有可写的场景文件（内存里的临时场）→ 与「保存场景」同一口径：
            # 说清楚「没落盘就配置不了」，不假装打开了编辑器。
            self._set_status_chip(self._t.t("status.configure_no_file"),
                                  style=("#fef3c7", "#92400e"))
            return
        scene_path = Path(self._scene_path)
        try:
            current = load_scene(scene_path)
        except (OSError, ValueError) as exc:
            warn(self, self._t.t("err.cannot_edit"),
                 self._t.t("err.scene_parse_failed", path=scene_path, exc=exc))
            return
        dlg = SceneEditorDialog(current, self._scenes_dir(), self._characters_dir(),
                                self, scene_path)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._refresh_scene_menu()
        edited = self._read_edited_scene(dlg)
        if edited is None:
            return
        pushed = self._push_scene_config(edited)
        self._reconcile_scene_cast([str(m.name) for m in edited.characters])
        self._refresh_scene_card_from(edited)
        self._set_status_chip(
            self._t.t("status.scene_configured", name=edited.name or scene_path.name)
            if pushed else self._t.t("status.scene_configured_unpushed",
                                     name=edited.name or scene_path.name),
            style=("#d1fae5", "#065f46") if pushed else ("#fef3c7", "#92400e"))

    def _read_edited_scene(self, dlg):
        """编辑器 accept 之后的场景对象：优先按**落盘文件**回读（编辑器保存即真相），
        回读不了才退回内存里的 `build_scene()`（离屏测试的替身编辑器只给得出后者）。"""
        saved = getattr(dlg, "saved_path", None)
        if saved is not None:
            try:
                return load_scene(Path(saved))
            except (OSError, ValueError):
                pass
        builder = getattr(dlg, "build_scene", None)
        if callable(builder):
            try:
                return builder()
            except Exception:            # noqa: BLE001 - 替身编辑器可能只实现了一半
                return None
        return None

    def _push_scene_config(self, scene) -> bool:
        """把编辑后的场景配置推给运行中的场景（§3.4）；返回 worker 是否真的接下了。

        字段口径与引擎的热更新面一致（name/date/background/description/
        description_mutable/plot_direction/hard_boundary/start_time）。worker 侧还没落地
        `apply_scene_config` 时返回 False——**文件已经写好**，界面照实说明「下次开场以文件
        为准」，不假装已经在演的这一场变了。
        """
        fields = {
            "name": scene.name,
            "date": scene.date,
            "background": scene.background,
            "description": scene.description,
            "description_mutable": bool(scene.description_mutable),
            "plot_direction": scene.plot_direction,
            "hard_boundary": scene.hard_boundary,
            "start_time": scene.start_time,
        }
        pusher = getattr(self._worker, "apply_scene_config", None)
        if not callable(pusher):
            return False
        try:
            pusher(**fields)
        except TypeError:
            # 签名略有出入（并行开发期）：退一步按「整份字段 dict」再试一次。
            try:
                pusher(fields)
            except Exception:            # noqa: BLE001 - 推不过去只影响热更新，不该掀翻界面
                return False
        except Exception:                # noqa: BLE001
            return False
        return True

    def _reconcile_scene_cast(self, wanted: list[str]) -> None:
        """配置后的阵容差异 → 逐个人事调用（进场走 add_character、离场走 remove_character）。

        只发**差集**（同一批人不动），失败不抛：worker 侧的可见状态自会说明原因。
        """
        current = self._active_names()
        wanted_set = {str(n) for n in (wanted or [])}
        for name in wanted or []:
            if str(name) not in current:
                self._call_cast_api("add_character", str(name))
        for name in current:
            if name not in wanted_set:
                self._call_cast_api("remove_character", name)

    def _call_cast_api(self, method: str, name: str) -> None:
        """调 worker 的一个人事 API（缺方法/调用失败一律吞掉：界面不因并行开发期崩）。"""
        fn = getattr(self._worker, method, None)
        if not callable(fn):
            return
        try:
            fn(name)
        except Exception:                # noqa: BLE001
            return

    def _refresh_scene_card_from(self, scene) -> None:
        """把编辑后的场景对象刷到左栏场景卡（名/日期/边界/开始时间；时间行按新起点重置）。"""
        name = str(getattr(scene, "name", "") or "").strip()
        date = str(getattr(scene, "date", "") or "").strip()
        self._apply_scene_fields({"name": name, "date": date})
        boundary = getattr(scene, "hard_boundary", None)
        self._boundary_is_time = getattr(boundary, "type", None) == "time"
        self._boundary_value.setText(
            self._boundary_text(self._boundary_is_time, getattr(boundary, "value", "")))
        start = str(getattr(scene, "start_time", "") or "").strip()
        if start:
            self._clock_time["start"].setText(start)
            self._clock_time["now"].setText(start)
        self._participants_value.setText("、".join(self._active_names()) or "—")

    # ------------------------------------------------------------ 模板导入（§3.3）
    def _on_import_template(self, kind: str) -> None:
        """「导入场景…」/「导入角色…」：选一个填好的 .md → 解析入库 → 报结果 → 刷新菜单。

        角色卡/场景卡由**文件内容**自动判定（template_import.detect_kind），落盘守门也在
        那里；解析失败只弹一句带行号/字段名的中文说明，绝不把 traceback 摊到界面上。
        成功后刷新两个动态菜单，新素材当场可用。
        """
        t = self._t
        into_scene = str(kind) == "scene"
        start_dir = self._scenes_dir() if into_scene else self._characters_dir()
        filename = pick_template_file(self, t.t("dlg.import_pick"), str(start_dir),
                                     t.t("dlg.import_filter"))
        if not filename:
            return
        try:
            result = import_template_file(
                Path(filename), characters_dir=self._characters_dir(),
                scenes_dir=self._scenes_dir())
        except TemplateError as exc:
            # TemplateError.__str__ 就是「第 N 行附近 字段【X】：消息」，可直接给用户看。
            warn(self, t.t("dlg.import_failed"), str(exc) or exc.__class__.__name__)
            return
        except OSError as exc:
            warn(self, t.t("dlg.import_failed"), str(exc))
            return
        self._refresh_scene_menu()
        self._refresh_cast_menus()
        info(self, t.t("dlg.import_done"), import_result_text(result))

    # ------------------------------------------------------------ 空状态 ↔ 会话区
    def _show_empty_state(self) -> None:
        """切到「未打开场景」（§3.3）：中栏显空状态页，对白/日志/输入条收起。

        同步把依赖场次的入口置灰（保存场景/配置场景/重置运行上下文，按钮与菜单项两处），
        免得点下去没反应却说不出原因。
        """
        self._scene_open = False
        if getattr(self, "_empty_state", None) is not None:
            self._empty_state.setVisible(True)
        if getattr(self, "_session_area", None) is not None:
            self._session_area.setVisible(False)
        for widget in (getattr(self, "_save_scene_btn", None),
                       getattr(self, "_configure_scene_btn", None)):
            if widget is not None:
                widget.setEnabled(False)
        for action in (getattr(self, "_action_save_scene", None),
                       getattr(self, "_action_configure_scene", None),
                       getattr(self, "_action_reset_scene", None)):
            if action is not None:
                action.setEnabled(False)
        self._enable_input(False)

    def _show_session_ui(self) -> None:
        """切到「场景已开」：收起空状态页，显出对白/日志窗格与输入条。"""
        self._scene_open = True
        if getattr(self, "_empty_state", None) is not None:
            self._empty_state.setVisible(False)
        if getattr(self, "_session_area", None) is not None:
            self._session_area.setVisible(True)

    def close_scene(self) -> None:
        """回到「未打开场景」：清空对白/日志/演员表镜像，中栏换回空状态页。

        与场景菜单的「重置场景运行上下文…」是两回事——那个保留场次继续从头演，这个把
        窗口退回起手那一屏。**收束（自然/手动停止）不算关闭场景**：收束后的转录仍留在
        屏上，只有这里才清。场景文件坐标与 cfg 素材不动，随时能再从空状态页打开。

        纯界面侧的动作（不投 worker）：调用方若要连引擎一起收，先自己调 stop_now()。
        """
        self._view.clear()
        self._log_view.clear()
        self._log_entries = []
        self._conv_msgs = []
        self._retracted_ids = set()      # 屏上已空 → 作废集从零起算
        self._narration = {}
        self._scene_diag = {}
        self._diag_seen = {}
        self._implicit_seen = None
        self._cast = None
        self._char_payloads = {}
        self._scene_path = None
        self._scene_name = ""
        self._resume_pending = None
        # 会话标记也归零：没有场次就没有「开新场/切模型重开」这回事（再打开场景时
        # switch_scene → start_session 会重新置真）。
        self._session_active = False
        self._started = False
        self._finished = False
        self._paused = False
        self._auto_paused = False
        self._worker_status = "就绪"
        self._participants_value.setText("…")
        self._scene_value.setText("…")
        self._date_value.setText("—")
        self._date_row.hide()
        self._boundary_is_time = False
        self._boundary_value.setText("—")
        self._clock_time["start"].setText("…")
        self._clock_time["now"].setText("…")
        self._repopulate_left_urge([])
        self._rebuild_right_cards([])
        self._refresh_cast_menus()
        self._show_empty_state()
        self._set_status_chip("就绪")
        self._set_model_badge("stub")
        self._pause_btn.setEnabled(False)
        self._stop_btn.setEnabled(False)
        self._narrate_btn.setEnabled(False)
        self._retranslate()              # 标题/页眉回到「未打开场景」那一屏的文案

    # -------------------------------------------------------------- 角色动作
    def _on_new_character(self) -> None:
        """角色 →「新建角色」：现有角色编辑器，存进角色库目录。

        只落卡、不动本场演员表：要让新卡上场，用「添加角色」把它加进这一场（或下一场
        开场时选它）。保存成功后顺手刷新「添加角色」子菜单，新卡当场可选。
        """
        dlg = CharacterEditorDialog(None, self._characters_dir(), self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.saved_path is not None:
            self._set_status_chip(
                self._t.t("status.saved_card", name=Path(dlg.saved_path).name),
                style=("#d1fae5", "#065f46"))
            self._refresh_cast_menus()

    def _on_add_character(self, name: str) -> None:
        """「添加角色」子菜单点击：让该角色进场（经 worker，界面绝不直接调引擎）。

        引擎侧会落一条播报行并广播 sig_cast，左右栏与在场名单随之重画；卡未装载等失败
        由 worker 转成可见状态说明（这里不吞也不抛）。
        """
        self._worker.add_character(str(name))

    def _on_remove_character(self, name: str) -> None:
        """「移出角色」子菜单点击：让该角色离场（历史保留、退出竞价，语义全在引擎侧）。"""
        self._worker.remove_character(str(name))

    def _on_manage_characters(self) -> None:
        """角色 →「管理角色…」：本场演员表的增/移/编辑/删。

        弹窗是模态的但**不阻塞 worker**（exec() 只跑嵌套事件循环）：弹窗里每个动作都
        只是 worker 调用；worker 事后广播的 sig_cast 会经 _on_cast 喂回弹窗重画表格。
        """
        dlg = CharacterManagerDialog(self._cast or {}, self._worker,
                                     self._characters_dir(), self)
        self._cast_manager = dlg
        try:
            dlg.exec()
        finally:
            self._cast_manager = None
            self._refresh_cast_menus()     # 期间可能新建/删除了角色卡

    def _on_advanced_cast(self) -> None:
        """角色 →「高级移入/移出…」：选人 + 移入/移出 + N 回合后执行 + 通知对象多选。

        确定后只投一次 worker.schedule_cast_change（延时排队与通知落行都在引擎侧），
        到时执行的仍是引擎的人事路径 → 照常广播 sig_cast → 左右栏当场重画。
        """
        names = [n for _p, n in self._character_cards()]
        dlg = AdvancedCastDialog(self._worker, names, self._active_names(), self)
        dlg.exec()

    # ------------------------------------------------------------------ 面板
    def _build_left(self) -> QWidget:
        """左栏：**整栏包在 QScrollArea 里**（内容超高即滚动，不挤压/变形部件）。

        外层只放滚动区；卡一律加在滚动区内容部件上（最小宽度见 LEFT_PANEL_MIN_WIDTH），
        横向滚动条禁用、背景透明（与右栏同款）。卡序：场景 → 运行指标 → 冲动 bid
        （实时）→ 运行控制。
        """
        outer = QWidget()
        # 外层也设最小宽度（= 内容宽 + 滚动条 + 内边距）：横向滚动条是关的，若整栏被
        # 分栏拖得比内容还窄，内容只会被裁掉——用最小宽度把「拖到挤出字」这条路堵死。
        outer.setMinimumWidth(LEFT_PANEL_MIN_WIDTH + 34)
        olay = QVBoxLayout(outer)
        olay.setContentsMargins(12, 12, 2, 12)
        olay.setSpacing(0)

        scroll = QScrollArea()
        scroll.setObjectName("leftScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)     # 无边框 → 观感同右栏（无凹陷框）
        self._left_scroll = scroll

        w = QWidget()
        w.setMinimumWidth(LEFT_PANEL_MIN_WIDTH)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        # ---- 场景（原「场景时间」已并入本卡；时间/边界都是场景自己的属性）。
        # 只放通用、与题材无关的行：场景名 / 日期（设了才有一行）/ 开始 / 当前 / 在场人数
        # / 边界（有 time 硬边界才给 HH:MM，否则「—」——界面上不写死任何「打烊」概念）。
        card, v = self._card("panel.scene")
        self._scene_value = self._add_kv(v, "scene.name", "…")
        self._date_row, self._date_value = self._add_kv_widget(v, "scene.date", "—")
        self._date_row.hide()              # 没设日期 → 整行不占位（不留空行/空标签）
        self._clock_time = {}
        self._clock_time["start"] = self._add_kv(v, "scene.start", "…")
        self._clock_time["now"] = self._add_kv(v, "scene.now", "…")
        self._boundary_value = self._add_kv(v, "scene.boundary", "—")
        self._participants_value = self._add_kv(v, "scene.present", "…")
        lay.addWidget(card)

        # ---- 运行指标（token/块数 ≥ 10 万走 k 单位，见 fmt_count）----
        card, v = self._card("panel.metrics")
        self._metric = {}
        self._metric["uptime"] = self._add_kv(v, "metric.elapsed", "—")
        self._metric["messages"] = self._add_kv(v, "metric.messages", "0")
        self._metric["blocks"] = self._add_kv(v, "metric.blocks", "0")
        self._metric["think"] = self._add_kv(v, "metric.think", "—")
        self._metric["speak"] = self._add_kv(v, "metric.speak", "—")
        lay.addWidget(card)

        # ---- 冲动 bid（实时）：在场每名角色一行的当前**数值 bid** 实时条（谁当下最可能
        # 拿话筒）。行由 sig_scene_info 到场后 _repopulate_left_urge 按参与者名单填充
        # （此处只建卡与容纳行的空 body）；数值与右栏角色卡「实时分量·冲动 bid」同源
        # （sig_dynamics → engine.dynamics_snapshot），随每块推进实时移动。
        card, v = self._card("metric.bid")
        body = QVBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(5)
        self._urge_body = body
        v.addLayout(body)
        lay.addWidget(card)

        # ---- 运行控制 ----（「开场设定」卡已删：开场由开始界面/场景库负责；「开新场」
        # 与库入口移到这里，仍在左栏可及）
        card, v = self._card("panel.control")
        ctrl = QHBoxLayout()
        ctrl.setSpacing(8)
        self._pause_btn = self._reg(QPushButton(), "btn.pause")
        self._pause_btn.setObjectName("primary")
        self._pause_btn.clicked.connect(self._on_toggle_pause)
        ctrl.addWidget(self._pause_btn)
        self._stop_btn = self._reg(QPushButton(), "btn.stop")
        self._stop_btn.setObjectName("ghost")
        self._reg_tooltip(self._stop_btn, "btn.stop_now")
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._on_stop)
        ctrl.addWidget(self._stop_btn)
        self._live_check = self._reg(QCheckBox(), "toggle.real_model")
        self._live_check.setChecked(self._cfg.live)
        # 「没有 key」判据含**设置里那份**（§2.1① 设置是 api 配置的主来源）：填过 key 的
        # 用户不该看到开关是灰的。判据集中在一处（_refresh_live_check），保存 api 配置后
        # 重跑一次即可当场解锁（G4）——不再只按建窗那一刻的快照定生死。
        self._refresh_live_check()
        self._live_check.toggled.connect(self._on_live_toggled)
        ctrl.addWidget(self._live_check)
        ctrl.addStretch(1)
        v.addLayout(ctrl)

        rate_row = QHBoxLayout()
        rate_row.setSpacing(8)
        rate_lab = self._reg(QLabel(), "metric.speed")
        self._pstyle(rate_lab, "muted12")
        self._rate_combo = QComboBox()
        for rate in RATE_OPTIONS:
            self._rate_combo.addItem(f"{rate:g}×", rate)
        self._rate_combo.setCurrentIndex(
            RATE_OPTIONS.index(1.0))                     # 默认 1×
        self._rate_combo.currentIndexChanged.connect(self._on_rate_changed)
        rate_row.addWidget(rate_lab)
        rate_row.addWidget(self._rate_combo)
        rate_row.addStretch(1)
        v.addLayout(rate_row)

        speed_row = QHBoxLayout()
        speed_row.setSpacing(8)
        speed_lab = self._reg(QLabel(), "metric.rate")
        self._pstyle(speed_lab, "muted12")
        self._speed_label = QLabel(self._t.t("metric.speed_value", sec="0.8"))
        self._pstyle(self._speed_label, "chip12")
        self._speed_slider = QSlider(Qt.Orientation.Horizontal)
        self._speed_slider.setRange(500, 3000)          # 0.5s ~ 3.0s / 句
        self._speed_slider.setValue(800)
        self._speed_slider.valueChanged.connect(self._on_speed)
        speed_row.addWidget(speed_lab)
        speed_row.addWidget(self._speed_slider, 1)
        speed_row.addWidget(self._speed_label)
        v.addLayout(speed_row)

        # 开新场 + 库入口（原「开场设定」卡的位置；开场文本改由开始流程给，见 _current_opening）
        open_row = QHBoxLayout()
        open_row.setSpacing(8)
        reopen = self._reg(QPushButton(), "btn.new_session")
        reopen.setObjectName("ghost")
        self._reg_tooltip(reopen, "btn.new_session_tip")
        reopen.clicked.connect(self._on_reopen)
        self._reopen_btn = reopen
        open_row.addWidget(reopen)
        # 场景库入口：就地打开库（可编辑/新建/复制/删除），选一套即切场。
        lib_btn = self._reg(QPushButton(), "btn.library")
        lib_btn.setObjectName("ghost")
        self._reg_tooltip(lib_btn, "tip.library")
        lib_btn.clicked.connect(self._on_open_library)
        self._library_btn = lib_btn
        open_row.addWidget(lib_btn, 1)
        v.addLayout(open_row)
        lay.addWidget(card)

        # ---- 场景推进（场景=一等 agent：复读/停滞/临近打烊时由世界推进一步）----
        card, v = self._card("panel.narration")
        narr_row = QHBoxLayout()
        narr_row.setSpacing(8)
        self._auto_scene_check = self._reg(QCheckBox(), "toggle.auto_advance")
        self._auto_scene_check.setChecked(True)      # 引擎缺省 auto=True；开场按引擎回读
        self._reg_tooltip(self._auto_scene_check, "toggle.auto_advance_tip")
        self._auto_scene_check.toggled.connect(self._on_auto_narrate_toggled)
        narr_row.addWidget(self._auto_scene_check)
        self._narrate_btn = self._reg(QPushButton(), "btn.advance")
        self._narrate_btn.setObjectName("ghost")
        self._reg_tooltip(self._narrate_btn, "btn.advance_tip")
        self._narrate_btn.setEnabled(False)          # 开场（sig_scene_info）后可用
        self._narrate_btn.clicked.connect(self._on_narrate_now)
        narr_row.addWidget(self._narrate_btn, 1)
        v.addLayout(narr_row)
        # 推进活跃度（§6.2）：频率**可调**，不再是一个写死的魔数。四档标签挂在
        # settings.NARRATE_ACTIVITIES 的四个数值上（见 NARRATE_ACTIVITY_LEVELS）；
        # 改档即落盘 + 立刻投 worker（引擎侧缩放触发线与冷却，下一块起生效）。
        act_row = QHBoxLayout()
        act_row.setSpacing(8)
        act_lab = self._reg(QLabel(), "narration.activity")
        self._pstyle(act_lab, "muted12")
        self._activity_combo = QComboBox()
        self._activity_combo.setObjectName("activityCombo")
        for key, level in NARRATE_ACTIVITY_LEVELS:
            self._activity_combo.addItem(self._t.t(key), level)
        self._reg_tooltip(self._activity_combo, "narration.activity_tip")
        self._activity_combo.currentIndexChanged.connect(self._on_activity_changed)
        act_row.addWidget(act_lab)
        act_row.addWidget(self._activity_combo, 1)
        v.addLayout(act_row)
        self._narration_status = QLabel("—")
        self._narration_status.setWordWrap(True)
        self._pstyle(self._narration_status, "muted12")
        v.addWidget(self._narration_status)
        lay.addWidget(card)
        lay.addStretch(1)

        scroll.setWidget(w)
        olay.addWidget(scroll)
        return outer

    def _build_center(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 12, 4, 12)
        lay.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(10)
        self._header_scene = QLabel(self._t.t("panel.dialogue"))
        self._pstyle(self._header_scene, "header")
        self._model_badge = QWidget()       # 容器：每次填充一枚「真实模型 / 离线 stub」药丸
        self._status_chip = QLabel("")
        top.addWidget(self._header_scene)
        top.addWidget(self._model_badge)
        top.addWidget(self._status_chip)
        top.addStretch(1)
        # 「保存场景」固定占在「日志」左边（§2.3）：落盘走 worker.save_scene（§5），
        # 开场成功（sig_scene_info）之前没有可存的场次，故此按钮先禁用。
        self._save_scene_btn = self._reg(QPushButton(), "btn.save_scene")
        self._save_scene_btn.setObjectName("ghost")
        self._reg_tooltip(self._save_scene_btn, "tip.save_scene")
        self._save_scene_btn.setEnabled(False)
        self._save_scene_btn.clicked.connect(self._on_save_scene)
        top.addWidget(self._save_scene_btn)
        # 「配置场景」紧挨着「保存场景」（§3.4）：开**当前场景**的编辑器，保存即就地生效。
        # 没有打开的场次就没有可配置的对象 → 先禁用（菜单项同步禁用，见 _show_empty_state）。
        self._configure_scene_btn = self._reg(QPushButton(), "btn.configure_scene")
        self._configure_scene_btn.setObjectName("ghost")
        self._reg_tooltip(self._configure_scene_btn, "tip.configure_scene")
        self._configure_scene_btn.setEnabled(False)
        self._configure_scene_btn.clicked.connect(self._on_configure_scene)
        top.addWidget(self._configure_scene_btn)
        self._log_btn = self._reg(QPushButton(), "btn.log")
        self._log_btn.setObjectName("ghost")
        self._log_btn.setCheckable(True)
        self._reg_tooltip(self._log_btn, "tip.log")
        self._log_btn.toggled.connect(self._on_toggle_log)
        top.addWidget(self._log_btn)
        lay.addLayout(top)

        # ---- 中栏两种状态（§3.3）：空状态页 ↔ 会话区，同一时刻只显一个。
        lay.addWidget(self._build_empty_state())

        # 会话语区：日志(上) + 对白(下) 共用一竖分栏，其下是输入条。建好即存在（信号槽、
        # 主题重绘都照常），只是**未打开场景时整块收起**（见 _show_empty_state）。
        self._session_area = QWidget()
        sa = QVBoxLayout(self._session_area)
        sa.setContentsMargins(0, 0, 0, 0)
        sa.setSpacing(10)
        self._center_split = QSplitter(Qt.Orientation.Vertical)
        self._center_split.setChildrenCollapsible(False)
        self._log_view = QTextBrowser()
        self._log_view.setObjectName("thinklog")
        self._log_view.setOpenExternalLinks(False)
        self._log_view.document().setMaximumBlockCount(THINK_PANE_CAP)
        self._view = QTextBrowser()
        self._view.setObjectName("conversation")
        self._view.setOpenExternalLinks(False)
        # 只对白区接锚点：叙述行尾的「撤销 / 改写」行内链接（scene:undo:<id> / scene:edit:<id>）。
        self._view.anchorClicked.connect(self._on_anchor_clicked)
        self._center_split.addWidget(self._log_view)
        self._center_split.addWidget(self._view)
        self._log_view.hide()
        sa.addWidget(self._center_split, 1)

        bottom_w = QWidget()
        bottom = QHBoxLayout(bottom_w)
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.setSpacing(8)
        self._input_edit = QLineEdit()
        self._input_edit.setObjectName("chat")
        self._reg_placeholder(self._input_edit, "input.interject")
        self._input_edit.returnPressed.connect(self._on_send)
        send_btn = self._reg(QPushButton(), "input.send")
        send_btn.setObjectName("primary")
        send_btn.clicked.connect(self._on_send)
        self._send_btn = send_btn
        bottom.addWidget(self._input_edit, 1)
        bottom.addWidget(send_btn)
        sa.addWidget(bottom_w)
        self._session_bottom = bottom_w
        lay.addWidget(self._session_area, 1)
        return w

    def _build_empty_state(self) -> QWidget:
        """中栏空状态页（§3.3）：一句话 headline + 副文案 + 三个居中按钮，四周大留白。

        这是用户打开软件见到的**第一屏**，故不塞任何别的控件：只有说明与三条出口
        （打开场景 / 新建场景 / 新建角色）。三个按钮都经 `_reg` 登记，切语言时随
        `_retranslate` 就地换字；配色走 `_pstyle` 的语义角色，切主题时重刷。
        """
        page = QWidget()
        page.setObjectName("emptyState")
        v = QVBoxLayout(page)
        v.setContentsMargins(32, 24, 32, 24)
        v.setSpacing(0)
        v.addStretch(3)

        headline = QLabel()
        headline.setObjectName("emptyHeadline")
        headline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._reg(headline, "empty.headline")
        self._pstyle(headline, "emptyHeadline")
        v.addWidget(headline)

        hint = QLabel()
        hint.setObjectName("emptyHint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setWordWrap(True)
        self._reg(hint, "empty.hint")
        self._pstyle(hint, "emptyHint")
        v.addSpacing(10)
        v.addWidget(hint)
        v.addSpacing(30)

        row = QHBoxLayout()
        row.setSpacing(12)
        row.addStretch(1)
        self._empty_open_btn = self._empty_button(
            "btn.open_scene", "tip.empty_open_scene", self._on_empty_open_scene,
            primary=True)
        self._empty_new_scene_btn = self._empty_button(
            "btn.new_scene", "tip.empty_new_scene", self._on_new_scene)
        self._empty_new_character_btn = self._empty_button(
            "btn.new_character", "tip.empty_new_character", self._on_new_character)
        for btn in (self._empty_open_btn, self._empty_new_scene_btn,
                    self._empty_new_character_btn):
            row.addWidget(btn)
        row.addStretch(1)
        v.addLayout(row)
        self._empty_button_row = row     # 给测试看「按钮行两侧有弹簧」＝居中
        v.addStretch(4)

        self._empty_state = page
        return page

    def _empty_button(self, text_key: str, tip_key: str, slot, *,
                      primary: bool = False) -> QPushButton:
        """空状态页的一枚按钮：文案/tooltip 走 i18n，`primary` 那枚用主色（唯一强调）。"""
        btn = self._reg(QPushButton(), text_key)
        btn.setObjectName("primary" if primary else "ghost")
        btn.setMinimumWidth(132)
        btn.setMinimumHeight(34)
        self._reg_tooltip(btn, tip_key)
        btn.clicked.connect(slot)
        return btn

    def _build_right(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(2, 12, 12, 12)
        lay.setSpacing(8)
        head = self._reg(QLabel(), "panel.character_card")
        head.setObjectName("cardTitle")
        lay.addWidget(head)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        self._right_cards_layout = QVBoxLayout(body)
        self._right_cards_layout.setContentsMargins(0, 0, 0, 0)
        self._right_cards_layout.setSpacing(12)
        self._right_cards_layout.addStretch(1)
        scroll.setWidget(body)
        lay.addWidget(scroll, 1)
        return w

    # ------------------------------------------------------------ 信号连线
    def _connect_worker(self) -> None:
        self._worker.sig_scene_info.connect(self._on_scene_info)
        self._worker.sig_message.connect(self._on_message)
        self._worker.sig_metrics.connect(self._on_metrics)
        self._worker.sig_status.connect(self._on_status)
        self._worker.sig_finished.connect(self._on_finished)
        self._worker.sig_dynamics.connect(self._on_dynamics)
        self._worker.sig_think.connect(self._on_think)
        self._worker.sig_narration.connect(self._on_narration)
        self._worker.sig_retracted.connect(self._on_retracted)
        self._worker.sig_cast.connect(self._on_cast)
        # 存档落盘成功（§5）：替身 worker（测试）可能还没有这条信号，防御式连接。
        sig_saved = getattr(self._worker, "sig_saved", None)
        if sig_saved is not None:
            sig_saved.connect(self._on_saved)
        # 成本守卫自动暂停旗标（G6）：替身 worker 可能还没有这条信号，防御式连接。
        sig_auto_paused = getattr(self._worker, "sig_auto_paused", None)
        if sig_auto_paused is not None:
            sig_auto_paused.connect(self._on_auto_paused)
        # 场景自身状态/诊断（G9）：同样防御式连接。
        sig_scene_diag = getattr(self._worker, "sig_scene_diag", None)
        if sig_scene_diag is not None:
            sig_scene_diag.connect(self._on_scene_diag)
        # 场景变更（§3.5）：场景 agent 改了场景信息 → 日志面板一条浅红记录。同样是新加的
        # 信号，替身 worker / 未落地的版本可能没有，故防御式连接。
        sig_scene_changed = getattr(self._worker, "sig_scene_changed", None)
        if sig_scene_changed is not None:
            sig_scene_changed.connect(self._on_scene_changed)

    # ------------------------------------------------------------ 公共入口
    def start_session(self) -> None:
        """向 worker 发起开场（app 启动后调用一次；空开场回退引擎默认）。

        演员表以**所选角色卡**为准（cast_from_cards，App 恒为真）：选中几张卡就有几人
        在场；切场（library → switch_scene）走同一入口，口径天然一致。
        """
        self._session_active = True          # 会话已开启：此后收束不阻断开新场/切模型
        opening = self._current_opening()
        # api key 与 live 都按「设置优先、CLI/环境变量回落」的口径给（§2.1①）：设置里存过
        # key 的用户，不必再设环境变量；worker 侧还会用自己收到的那份覆盖它。
        api_key = self._api_key()
        self._worker.start_scene(
            scene=self._cfg.scene,
            characters=self._cfg.characters,
            models_yaml=self._cfg.models,
            live=bool(self._cfg.live and api_key),
            bid=self._cfg.bid,
            opening=opening,
            run_root=self._cfg.run_root,
            api_key=api_key,
            closing_at_block=self._cfg.closing_at_block,
            cast_from_cards=bool(self._cfg.cast_from_cards),
            # 角色库目录（§3.2 按需装卡）：引擎据此在**任何时候**把库里的卡装进场——
            # 空场开场（cfg.characters 为空）时更要显式给，不然引擎没有库可查，
            # 「添加角色」就会以「库里没有这张卡」收场。
            characters_dir=self._characters_dir())

    def _current_opening(self) -> str | None:
        """本场的开场设定（None=用引擎默认）。

        左栏的「开场设定」输入框已随 S2a 删除——开场文本由开始流程（开场设置/场景库，
        见 S2b）写进 AppConfig.opening，这里只读它；空/纯空白回 None。
        """
        return (self._cfg.opening or "").strip() or None

    def closeEvent(self, event) -> None:  # noqa: N802
        # 收尾等待放宽（评审 #5）：close 时等在引擎 loop 上撤 autoplay + aclose + join，
        # 确保线程先于窗口/worker 析构结束，杜绝 destroy-while-running。
        if self._worker is not None and self._worker.isRunning():
            self._worker.shutdown(8000)
        super().closeEvent(event)

    # ------------------------------------------------------------ worker 槽
    def _on_scene_info(self, info: dict) -> None:
        scene = info.get("scene") or {}
        chars = info.get("characters") or []
        backend = info.get("backend") or "stub"
        # 场景信息一到即视为已开始：清空旧对白/旧 think 日志、重建角色色表与右栏卡。
        self._view.clear()
        self._log_view.clear()
        self._log_entries = []          # 日志留存同步清空（换场不串内容）
        self._conv_msgs = []            # 本地留存同步清空（换场不串内容）
        self._retracted_ids = set()     # 作废集同清：新场的 id 从零重排，旧作废 id 无意义
        self._name_colors = assign_name_colors(
            [ch.get("name") for ch in chars], self._speaker_palette())
        name = scene.get("name") or self._cfg.scene.stem
        self._scene_name = name            # 标题/页眉跟着场景名走（切语言时重译）
        self._header_scene.setText(name)
        self._header_scene.setVisible(True)
        self.setWindowTitle(f"{name} · {self._t.t('app.title')}")
        # 场景真的开了 → 中栏从空状态页换成正常三栏对白界面（§3.3）。
        self._show_session_ui()
        self._backend = backend
        self._set_model_badge(backend)
        self._scene_value.setText(name)
        self._participants_value.setText("、".join(scene.get("participants") or []) or "—")
        # 日期行只在场景设了日期时出现（S3 起场景才有 date 字段；没设=整行隐藏）。
        date = str(scene.get("date") or "").strip()
        self._date_value.setText(date or "—")
        self._date_row.setVisible(bool(date))
        # 边界：只有 time 硬边界才给 HH:MM，其余（无边界/非 time 类型）一律「—」。
        self._boundary_is_time = (scene.get("hard_boundary") or {}).get("type") == "time"
        self._boundary_value.setText(self._boundary_text(
            self._boundary_is_time, info.get("boundary_time")))
        # 换场：旧的演员表镜像先清（新场的 sig_cast 紧跟着就发，见 worker._launch），
        # 这中间左右栏按本载荷的 characters 兜底。
        self._cast = None
        self._repopulate_right(info)
        self._repopulate_left_urge(scene.get("participants") or [])
        self._refresh_cast_menus()
        self._started = True                # 成功开场（含重开）：一旦为真保持到换场
        self._finished = False
        self._paused = False
        self._auto_paused = False           # 新场：守卫暂停态清零（等本场的旗标）
        self._worker_status = "进行中"
        self._pause_btn.setText(self._t.t("btn.pause"))
        self._pause_btn.setEnabled(True)
        self._stop_btn.setEnabled(True)
        # 场次已开 → 「保存场景」「配置场景」「重置场景运行上下文…」可用（按钮与菜单项
        # 两处一起放开）；场景文件坐标按本场载荷记下（None = 这一场没有可写的场景文件，
        # 保存按钮点了只报「未落盘」）。
        self._save_scene_btn.setEnabled(True)
        self._configure_scene_btn.setEnabled(True)
        self._action_save_scene.setEnabled(True)
        self._action_configure_scene.setEnabled(True)
        self._action_reset_scene.setEnabled(True)
        scene_path = info.get("scene_path")
        self._scene_path = Path(str(scene_path)) if scene_path else None
        # 续演（§5）：打开场景时问过「接着上次演吗」（_maybe_resume）→ 此刻场景确实开了，
        # 才消费那个决定。仍是同一场（路径对得上）才恢复，免得开场失败后隔一场误恢复。
        pending, self._resume_pending = self._resume_pending, None
        if pending is not None and pending == self._scene_path:
            self._worker.restore_scene()
        # 「场景」卡的时间行：开场即显示开始；当前(HH:MM:SS)随指标刷新（虚拟钟秒走）。
        start = info.get("start_time")
        self._clock_time["start"].setText(start or "…")
        self._clock_time["now"].setText(start or "…")
        # 「场景推进」卡：复选框按**引擎**回读的 auto 对齐（开场/切场都以引擎为准；
        # 期间阻塞信号——这是镜像同步，不该反手再投一次 set_auto_narrate）。
        self._narration = {}            # 换场：旧的叙述状态先清，等新场的 sig_narration
        self._scene_diag = {}           # 换场：旧场的诊断状态一并清（新场重新起算）
        self._diag_seen = {}
        self._implicit_seen = None
        auto = info.get("auto_narrate")
        if auto is not None:
            self._auto_scene_check.blockSignals(True)
            self._auto_scene_check.setChecked(bool(auto))
            self._auto_scene_check.blockSignals(False)
        self._narrate_btn.setEnabled(True)
        self._render_narration_status()
        self._enable_input(True)

    def _on_cast(self, cast: dict) -> None:
        """sig_cast：演员表运行期状态到达 → 场景卡在场名单 / 左栏冲动行 / 右栏角色卡同步重画。

        引擎是唯一权威（谁在场、谁被禁言剩几块都听它），界面只做镜像：被移出者的左行与
        右卡**当场消失**，新进场者当场补一行一卡，禁言者在两侧各缀一条「禁言中（剩 N
        回合）」。正在开着的「管理角色…」弹窗也同步重画（表格内容同源）。
        """
        data = dict(cast) if isinstance(cast, dict) else {}
        self._cast = data
        active = self._active_names()
        muted = self._muted_map()
        self._participants_value.setText("、".join(active) or "—")
        self._repopulate_left_urge(active, muted=muted)
        self._rebuild_right_cards(active, muted=muted)
        if self._cast_manager is not None:
            self._cast_manager.update_cast(data)
        self._sync_cast_menu_state()

    def _on_message(self, msg: dict) -> None:
        """一条新消息：先存进本地留存（撤销/改写/重绘的真源），再整屏重绘。

        整屏 setHtml 而非逐条 append：Qt 的 append 会**继承上一段的段落对齐**——叙述行
        是居中的，之后 append 的角色行会跟着居中（气泡行的右对齐同理）。每次从留存重建
        才能保证每段的 align 各归各位（消息总量对单机演示级转录无性能压力）。
        """
        self._conv_msgs.append(dict(msg))
        self._rerender_conversation()

    def _last_msg_id(self) -> int | None:
        """本地留存里**最后一条带 id 的**消息 id（没有则 None；人类气泡不算）。"""
        for m in reversed(self._conv_msgs):
            if isinstance(m.get("id"), int):
                return int(m["id"])
        return None

    def _rerender_conversation(self) -> None:
        """按本地留存整屏重绘对白区，并滚到底（撤销/改写后立即生效）。"""
        self._view.setHtml("".join(self._format_message(m) for m in self._conv_msgs))
        bar = self._view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _drop_message(self, mid: int) -> bool:
        """从本地留存摘掉某条消息并重绘；返回是否真的摘掉了（幂等，重复调用无害）。"""
        keep = [m for m in self._conv_msgs if m.get("id") != mid]
        if len(keep) == len(self._conv_msgs):
            return False
        self._conv_msgs = keep
        self._rerender_conversation()
        return True

    def _drop_from(self, mid: int) -> bool:
        """**截断式**摘除：从该条起（含）把它与留存里其后的一切行丢掉并重绘（§6.1）。

        只用在**发起撤回/改写的那一刻**（_undo_narration / _edit_narration，本地即时反馈，
        不等 worker 往返）：此刻"该条之后"与引擎将要丢弃的那一段**恰好是同一批**（对白区
        只装当前一场的内容，换场即清空），故位置截断既准又快，连没有 id 的人类气泡也一并
        清掉。找不到该 id（已被摘掉/只在日志里）→ 什么都不动，返回 False。
        **不要在收到 sig_retracted 时用它**：改写是「截断 + 重新落一行」，新行在上屏顺序上
        排在旧 id 之后，位置截断会把刚落地的新叙述误杀（见 _on_retracted）。
        """
        idx = next((i for i, m in enumerate(self._conv_msgs) if m.get("id") == mid), None)
        if idx is None:
            return False
        del self._conv_msgs[idx:]
        self._rerender_conversation()
        return True

    def _reconcile_retracted(self, ids) -> bool:
        """按**权威作废集**整屏对齐（worker 叙述状态载荷里的 `retracted` 全量）。

        有 id 的行按 id 摘；人类气泡（界面自己上的屏，没有 id）看它的 `after_id`——即它
        落屏时**最近一条有 id 的行**——若那条也被作废，说明这句是写在"已被回溯的下文"上
        的，一并摘掉；否则保留（改写之后用户再插的话，其 after_id 是改写后的新行，不会
        被误伤）。这正是「worker 只报单条 id」时的兜底：以引擎的作废集为准刷新，而不是
        只认那一个 id（也顺带覆盖别处发起、界面没参与的那类撤回）。
        """
        fresh = {int(i) for i in (ids or []) if isinstance(i, int)}
        if not fresh:
            return False
        self._retracted_ids |= fresh
        keep = [m for m in self._conv_msgs
                if m.get("id") not in self._retracted_ids
                and m.get("after_id") not in self._retracted_ids]
        if len(keep) == len(self._conv_msgs):
            return False
        self._conv_msgs = keep
        self._rerender_conversation()
        return True

    def _on_scene_diag(self, diag: dict) -> None:
        """sig_scene_diag：场景自身状态（活字段 + 诊断 + 隐式事件）到达（G9）。

        三件事：
        ① 场景卡按**活字段**刷新——hook/场景工具改过场景名/日期之后，卡片不再停在开场值；
        ② 新出现的 hook/tool/save 诊断写进日志区并在状态区提示一次（此前完全不可见）；
        ③ 新出现的隐式事件（visible=False 的钩子事件，不进对白）也写进日志区一行。
        去重：同一条诊断/同一条隐式事件只提示一次（该信号每块都可能到）。
        """
        data = dict(diag) if isinstance(diag, dict) else {}
        self._scene_diag = data
        self._apply_scene_fields(data.get("fields") or {})
        for key in ("hook_error", "tool_error", "save_error"):
            text = str(data.get(key) or "").strip()
            if not text or self._diag_seen.get(key) == text:
                continue
            self._diag_seen[key] = text
            self._note_scene_diag(text)
        events = list(data.get("implicit_events") or [])
        if events:
            last = dict(events[-1] or {})
            sig = (last.get("hook_id"), last.get("turn"),
                   str(last.get("content") or ""))
            if sig != self._implicit_seen:
                self._implicit_seen = sig
                self._log_append({"kind": "text", "role": "muted",
                                  "text": str(last.get("content") or ""),
                                  "key": "log.implicit_event"})

    def _apply_scene_fields(self, fields: dict) -> None:
        """把场景自己的**活字段**刷到左栏场景卡（名/日期）——含窗口标题与中栏页眉。

        场景名也更新 `_scene_name`：标题、页眉、切语言后的重译都以它为准（改过名之后
        不该还挂着开场时的旧名）。
        """
        name = str(fields.get("name") or "").strip()
        if name and name != self._scene_name:
            self._scene_name = name
            self._scene_value.setText(name)
            self._header_scene.setText(name)
            self.setWindowTitle(f"{name} · {self._t.t('app.title')}")
        date = str(fields.get("date") or "").strip()
        self._date_value.setText(date or "—")
        self._date_row.setVisible(bool(date))

    def _on_scene_changed(self, changes: list) -> None:
        """sig_scene_changed（§3.5）：场景信息被改动 → 每条在日志窗格落一行**浅红**记录。

        每一条（含 `visible=False` 的隐式变更）都写：隐式变更不进对话，这条日志就是它唯一
        的可见痕迹。载荷可以是 `[{field, old, new}]`，也容忍单个 dict / 别的形状——认不出
        形状就整条不写（不抛进 Qt 事件循环）。
        """
        items = changes
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, (list, tuple)):
            return
        for change in items:
            if not isinstance(change, dict) or not change.get("field"):
                continue
            # 留存**结构化**条目（引擎的 field/new 语气），渲染时才译字段名——切语言/
            # 切主题时 _rerender_log 按同一份数据重排，不会把旧语言的字符串留下。
            self._log_append({"kind": "change",
                              "field": str(change.get("field")),
                              "new": change.get("new")})

    def _note_scene_diag(self, text: str) -> None:
        """一条新诊断：日志区留档 + 状态区提示一次（可见，不必翻日志窗格）。"""
        self._log_append({"kind": "text", "role": "warn", "text": text,
                          "key": "log.scene_diag"})
        self._set_status_chip(self._t.t("status.scene_diag", text=text),
                              style=("#fef3c7", "#92400e"))

    def _log_append(self, item: dict) -> None:
        """往日志窗格（人类只读区）追一条：留存结构化条目 + 上屏（与 think 日志同处一地）。"""
        self._log_entries.append(dict(item))
        if len(self._log_entries) > THINK_PANE_CAP:
            del self._log_entries[:len(self._log_entries) - THINK_PANE_CAP]
        self._log_view.append(self._render_log_item(item))
        bar = self._log_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _render_log_item(self, item: dict) -> str:
        """一条日志留存条目 → HTML（think 走 _format_think、场景变更走浅红行、其余按角色上色）。"""
        kind = (item or {}).get("kind")
        if kind == "think":
            return self._format_think(item.get("entry") or {})
        if kind == "change":
            # 场景变更（§3.5）：浅红高亮 + 起首一个极简几何记号，与 think 行、诊断行分得开。
            color = scene_change_color(self._pal)
            text = html.escape(scene_change_text(item, self._t))
            return (f'<p style="margin:0 0 5px 0;line-height:1.5;'
                    f'color:{color};font-size:12px;">· {text}</p>')
        role = "warn" if (item or {}).get("role") == "warn" else "muted"
        color = self._pal.warn_text if role == "warn" else self._pal.muted_text
        text = html.escape(str((item or {}).get("text") or ""))
        text = self._t.t(item.get("key") or "log.scene_diag", text=text)
        return (f'<p style="margin:0 0 5px 0;line-height:1.5;'
                f'color:{color};font-size:12px;">{text}</p>')

    def _on_dynamics(self, dyn: dict) -> None:
        """sig_dynamics：把每名角色的数值动态分量 + 数值 bid 刷到右栏角色卡实时块。

        只读人类 UI 展示：全部实时分量（冲动 bid / 沉默轮数 / 相关度 / 唤醒 / 邻接 /
        目标压力 / 场景压力 / 自报 urge / 待回应压力）都来自引擎数值 Dynamics 随情景演化的状态（worker
        每轮读 dynamics_snapshot 广播）；旧 think 字段（addressed 等）后向兼容存在时仍用于
        刷新「被点名/回应对象」，并在仍欠答（turns_pending>0）时后缀「（第 N 轮未答）」。
        缺失角色/字段一律不动（保持占位）。
        """
        for name, st in dyn.items():
            self._update_left_urge(name, st)     # 左栏冲动(bid)卡与右卡同源同步刷新
            refs = self._dyn_state.get(name)
            if not refs:
                continue
            self._set_dyn_value(refs["bid"], self._num(st, "bid"))
            self._set_dyn_value(refs["turns"], self._num(st, "turns_since_spoke"))
            self._set_dyn_value(refs["relevance"], self._num(st, "relevance"))
            self._set_dyn_value(refs["arousal"], self._num(st, "arousal"))
            self._set_dyn_value(refs["adjacency"], self._num(st, "adjacency"))
            self._set_dyn_value(refs["goal_pressure"], self._num(st, "goal_pressure"))
            self._set_dyn_value(refs["scene_pressure"], self._num(st, "scene_pressure"))
            self._set_dyn_value(refs["self_urge"], self._num(st, "self_urge"))
            self._set_dyn_value(refs["pending"], self._num(st, "pending_reply"))
            addressed = st.get("addressed")
            text = str(addressed) if addressed else "—"
            pending_rounds = self._num(st, "turns_pending")
            if pending_rounds >= 1:              # 被点名后一直没答 → 标注欠了几轮
                text = f"{text}（第 {int(pending_rounds)} 轮未答）"
            refs["respond"].setText(text)

    def _on_think(self, entry: dict) -> None:
        """sig_think：追加一条角色 think 日志到上半日志窗格（人类 UI 检视）。"""
        self._log_append({"kind": "think", "entry": dict(entry or {})})

    def _on_toggle_log(self, on: bool) -> None:
        """「日志」开关：开 → 中央列分栏，上半日志窗格与下半对白同时可见；
        关 → 收起日志窗格只显对白。对白内容不受影响（只是分栏可见性）。"""
        if on:
            self._log_view.show()
            half = max(140, self.height() // 2)   # 上半日志 / 下半对白，均分
            self._center_split.setSizes([half, half])
            self._log_btn.setText(self._t.t("btn.log_on"))
        else:
            self._log_view.hide()
            self._log_btn.setText(self._t.t("btn.log"))

    @staticmethod
    def _num(st: dict, key: str) -> float:
        """取动态值并兜底为 float（缺省 0.0；非法值也按 0 处理）。"""
        try:
            v = st.get(key)
            return float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _set_dyn_value(cls, refs: dict, value: float) -> None:
        """刷新一根实时数值条：进度条映射到 0..100%，文本按 decimals 显示真实值。

        两种标尺（handle 里二选一）：
        · 区间标尺 lo/hi（如 bid 取 -2..6）：pct = (value-lo)/(hi-lo)，条在 lo 处空、
          hi 处满——bid 已含负项（recency 惩罚）与正向累积，0 不再适合当空档；
        · 截断标尺 cap（0..cap）：旧行为，用于 0..1 分量/沉默轮数等单向量。
        两者都只截视觉，右侧数字永远显真实值。refs 由 _add_dyn_value_row/_add_urge_row
        提供 {"bar", "num", "cap", "lo", "hi", "decimals"}。
        """
        decimals = int(refs.get("decimals", 2))
        lo, hi = refs.get("lo"), refs.get("hi")
        if lo is not None and hi is not None and float(hi) > float(lo):
            span = float(hi) - float(lo)
            pct = (value - float(lo)) / span * 100.0
        else:
            cap = float(refs.get("cap", 1.0))
            pct = (value / cap * 100.0) if cap else 0.0
        refs["bar"].setValue(round(max(0.0, min(100.0, pct))))
        refs["num"].setText(f"{value:.{decimals}f}")

    def _update_left_urge(self, name: str, st: dict) -> None:
        """把 sig_dynamics 里某角色的**数值 bid** 刷到左栏冲动卡对应行（与右卡同源）。

        数值 Dynamics 冷启动即给全员初值 → 行通常总有值；bid 字段缺失时不动该行。
        """
        row = self._urge_rows.get(name)
        if row is None or st.get("bid") is None:
            return
        self._set_dyn_value(row, self._num(st, "bid"))

    def _on_metrics(self, m: dict) -> None:
        try:
            self._metric["uptime"].setText(f"{float(m.get('uptime_s', 0)):.1f} s")
        except (TypeError, ValueError):
            pass
        self._metric["messages"].setText(fmt_count(m.get("messages", 0)))
        self._metric["blocks"].setText(fmt_count(m.get("blocks", 0)))
        self._metric["think"].setText(self._fmt_usage(m.get("think")))
        self._metric["speak"].setText(self._fmt_usage(m.get("speak")))
        # 「场景」卡的时间行（worker 时间指标）：当前 HH:MM:SS 高速刷新（虚拟钟秒走）；
        # 边界沿用同一份 payload，但只在场景确有 time 边界时才显示（否则保持「—」）。
        clock_hhmmss = m.get("clock_hhmmss")
        if clock_hhmmss:
            self._clock_time["now"].setText(str(clock_hhmmss))
        if m.get("start_hhmm"):
            self._clock_time["start"].setText(str(m["start_hhmm"]))
        if self._boundary_is_time:
            self._boundary_value.setText(self._boundary_text(True, m.get("boundary_hhmm")))
        # 指标随每块刷新 → 顺带重画「场景推进」状态行（块数/理由与引擎同步推进）。
        self._render_narration_status()

    def _on_narration(self, state: dict) -> None:
        """sig_narration：缓存叙述者状态快照 → 刷新左栏状态行（自动/块数/上次理由）。

        载荷带了当前活跃度就顺手对齐控件（换场/重建引擎后以 worker 手里的值为准），带了
        作废集就按它整屏对齐对白区（回溯丢掉的尾巴不留在屏上，见 _reconcile_retracted）。
        """
        self._narration = dict(state or {})
        self._mirror_narrate_activity(self._narration)
        self._reconcile_retracted(self._narration.get("retracted"))
        self._render_narration_status()

    def _on_retracted(self, mid: int) -> None:
        """sig_retracted：该条已作废 → 从对白区摘掉它（**只按 id**，不做位置截断）。

        回溯式撤回（§6.1）由 worker 把被丢弃的**整段 id 逐个**报来（见
        worker._retract_message / _edit_narration），故逐条按 id 摘即可把整段尾巴摘干净；
        尾巴里的人类气泡由 _on_narration 的权威作废集兜底。
        **绝不在这里"从该 id 起截断"**：改写是「截断 + 重新落一行」，worker 先 flush 新行
        再逐 id 报作废——新行的 id 比被丢的那批更大、且已经排在屏上，位置截断会把刚落地
        的新叙述一并误杀（实测：改写后新行确实被吃掉）。
        """
        self._retracted_ids.add(int(mid))
        self._drop_message(int(mid))

    def _render_narration_status(self) -> None:
        """左栏「场景推进」状态行：自动开关态 + 距上次推进的块数 + 上次触发理由。

        数据全来自 sig_narration（引擎 narration_state()）；未开场/未判过则给占位。
        """
        t = self._t
        st = self._narration
        if not st:
            self._narration_status.setText("—")
            return
        auto = t.t("narration.auto_on") if st.get("auto") else t.t("narration.auto_off")
        blocks = st.get("blocks_since")
        rep = st.get("last_report") or {}
        reasons = "；".join(rep.get("reasons") or []) or t.t("narration.no_trigger")
        score = rep.get("score")
        line = t.t("narration.status_line", auto=auto,
                   n=blocks if blocks is not None else "—")
        line += "\n" + t.t("narration.last_reason", reasons=reasons)
        if score is not None:
            try:
                line += t.t("narration.score", score=f"{float(score):.2f}")
            except (TypeError, ValueError):
                pass
        self._narration_status.setText(line)

    # ------------------------------------------------------ 叙述行：撤销 / 改写
    def _on_anchor_clicked(self, url: QUrl) -> None:
        """对白区行内链接唯一入口：只认 scene:undo:<id> / scene:edit:<id>，其余忽略。"""
        text = url.toString() if isinstance(url, QUrl) else str(url)
        if text.startswith(ANCHOR_UNDO):
            mid = self._parse_anchor_id(text, ANCHOR_UNDO)
            if mid is not None:
                self._undo_narration(mid)
            return
        if text.startswith(ANCHOR_EDIT):
            mid = self._parse_anchor_id(text, ANCHOR_EDIT)
            if mid is not None:
                self._edit_narration(mid)

    @staticmethod
    def _parse_anchor_id(text: str, prefix: str) -> int | None:
        """锚点尾部 → 消息 id（非数字/缺失一律 None，绝不因脏 href 抛进 Qt 事件循环）。"""
        try:
            return int(text[len(prefix):])
        except (TypeError, ValueError):
            return None

    def _undo_narration(self, mid: int) -> None:
        """撤销一条叙述：**先确认**（会丢弃其后的全部内容）→ 界面截断 + 投 worker。

        撤回已是回溯式（§6.1），后果不可逆且用户看不见边界之外的代价，故必须先问一句；
        选「取消」则什么都不做（不摘行、不投递）。
        """
        mid = int(mid)
        if not confirm(self, self._t.t("dlg.undo_narration_title"),
                       self._t.t("dlg.undo_narration_ask")):
            return
        # 立刻截断（该条与其后的行一起消失，不等引擎往返）+ 投 worker 落共享态。
        self._drop_from(mid)
        self._worker.retract_message(mid)

    def _edit_narration(self, mid: int) -> None:
        """改写一条叙述：多行输入框预填**当前文本** → 空白拒绝 → **再确认回溯** → 投 worker。

        非空才投 worker（引擎侧 = 先回溯到该节点、再追加改写后的新行，见 edit_narration）；
        改写同样会丢掉该条之后的全部内容，故在改文之后、应用之前再问一次（文案直说代价）；
        选「取消」则原样不动（不截断、不投递、不留半个改动）。
        """
        mid = int(mid)
        old = next((m for m in self._conv_msgs if m.get("id") == mid), None)
        if old is None:
            return                             # 该行已不在屏上（被撤/换场）→ 无从改写
        text, ok = QInputDialog.getMultiLineText(
            self, self._t.t("narration.rewrite_title"),
            self._t.t("narration.rewrite_prompt"),
            str(old.get("content") or ""))
        if not ok:
            return
        if not (text or "").strip():
            self._set_status_chip(self._t.t("status.edit_empty"),
                                  style=("#fef3c7", "#92400e"))
            return
        # 「改文」与「丢弃下文」是同一枚按钮的两件事，说清代价再动手（取消 = 零改动）。
        if not confirm(self, self._t.t("dlg.rewrite_narration_title"),
                       self._t.t("dlg.rewrite_narration_ask")):
            return
        # 本地先截断：回溯立刻可见，随后 worker 派发的新叙述行再整屏追加（不留旧尾巴）。
        self._drop_from(mid)
        self._worker.edit_narration(mid, text.strip())

    # ------------------------------------------------------ 场景推进控制
    def _on_auto_narrate_toggled(self, checked: bool) -> None:
        """「自动推进」开关 → 投 worker（关掉后「推进一下」仍可用）。"""
        self._worker.set_auto_narrate(bool(checked))

    def _on_narrate_now(self) -> None:
        """「推进一下」：手动让场景推进一步（无视自动开关，即刻上屏）。"""
        if not self._started or self._finished:
            return
        self._worker.narrate_now()

    def _on_auto_paused(self, on: bool) -> None:
        """成本守卫自动暂停旗标（G6）：机器可读地告诉界面「这次暂停是守卫触发的」。

        与状态文案解耦：worker 的可见文案一字不改，但界面判定不再靠「状态串里有没有
        点继续」猜（换语言/改文案就会失灵）。旗标一到即同步暂停镜像与按钮；守卫解除
        （手动续演）后回普通态。
        """
        self._auto_paused = bool(on)
        if on:
            self._paused = True
            self._pause_btn.setText(self._t.t("btn.continue"))
        elif not self._finished:
            self._paused = False
            self._pause_btn.setText(self._t.t(self._pause_button_key()))

    def _on_status(self, text: str) -> None:
        # 记录 worker 终态供收束按钮文案使用（以 worker 为准，避免本地 _user_stopped
        # 与自然收束抢先后文案打架）；错误状态（worker 旧口径的记号前缀或「…失败：…」
        # 这类说明，见 is_error_status）→ 状态区红字（评审 #2 不静默）。
        self._worker_status = text
        if is_error_status(text):
            self._set_status_chip(text, style=("#fee2e2", "#991b1b"))
        elif self._auto_paused:
            # 成本守卫已置旗标 → 这条状态用琥珀色（「点继续」提示）；暂停镜像与按钮
            # 由 _on_auto_paused 负责，这里只管药丸底色。
            self._set_status_chip(text, style=("#fef3c7", "#92400e"))
        else:
            self._set_status_chip(text)

    def _on_finished(self) -> None:
        self._finished = True
        self._auto_paused = False             # 收束/停止后不再是「守卫暂停」态
        self._enable_input(False)
        self._pause_btn.setEnabled(False)
        self._stop_btn.setEnabled(False)
        self._narrate_btn.setEnabled(False)   # 已收束：世界不再推进（开新场再启用）
        # 文案以 worker 终态为准：sig_status(已停止/已收束) 先于 sig_finished 到达。
        self._pause_btn.setText(self._t.t(self._pause_button_key()))

    # ------------------------------------------------------------ 用户操作
    def _on_send(self) -> None:
        """人类插话。先即时上屏自己的「你」气泡（不等角色下一句，worker 扫描不会再画
        一次），随后才交给 worker；收束/引擎间隙不清空并给状态提示（评审 #3/#4）。"""
        text = self._input_edit.text().strip()
        if not text:
            return
        if self._finished:
            self._set_status_chip(self._t.t("err.scene_closed"),
                                  style=("#fee2e2", "#991b1b"))
            return
        if not self._worker.can_say():
            self._set_status_chip(self._t.t("err.scene_not_ready"),
                                  style=("#fef3c7", "#92400e"))
            return
        self._append_human(text, self._clock_now_hhmmss())   # 立即渲染，不等 worker
        self._input_edit.clear()
        if self._paused:                    # 暂停中发送 → worker 也会解除暂停，窗口同步
            self._paused = False
            self._pause_btn.setText(self._t.t("btn.pause"))
        self._worker.say(text)

    def _on_save_scene(self) -> None:
        """中栏「保存场景」按钮（§5）：交给 worker 落 sidecar，成功后由 sig_saved 报状态。

        没有可写的场景文件（本场未落盘）就不投递——连点也不会有副作用，直接在状态区
        说清楚（绝不假装保存）。真正的收尾（写盘 + sig_saved）在 worker/引擎侧。
        """
        if self._scene_path is None:
            self._set_status_chip(self._t.t("status.save_no_file"),
                                  style=("#fef3c7", "#92400e"))
            return
        self._worker.save_scene()

    def _on_saved(self, path: str) -> None:
        """sig_saved：存档已落盘（§5）——记住路径 + 在状态区报「已保存：<文件名>」。

        报的是**场景文件**名（sidecar 名形如 `<场景>.runtime.json`，那层内部后缀对用户
        没有意义）；本场连场景文件坐标都没有时才退回存档名。
        """
        self._saved_path = Path(str(path)) if str(path or "").strip() else None
        if self._saved_path is None:
            return
        name = (self._scene_path.name if self._scene_path is not None
                else self._saved_path.name)
        self._set_status_chip(self._t.t("status.scene_saved", name=name),
                              style=("#d1fae5", "#065f46"))

    def _on_reset_scene(self) -> None:
        """场景 →「重置场景运行上下文…」（§3.4/§5）：确认后清空对话与思考上下文。

        确认框默认「否」（破坏性动作，误点不该把一场对话清空）；确定后由 worker 落到
        引擎——**配置与在场角色保留**，只是这一场从头再演。
        """
        if not confirm(self, self._t.t("dlg.reset_scene_title"),
                       self._t.t("dlg.reset_scene_ask")):
            return
        self._worker.reset_scene()
        self._set_status_chip(self._t.t("status.scene_reset"),
                              style=("#d1fae5", "#065f46"))

    def _maybe_resume(self, scene_path: Path | str | None) -> None:
        """打开场景时的「续演」询问（§5）：**所有打开路径共用这一处**。

        有 sidecar 存档才问（没存档直返，绝不打扰）；答 Yes → 记下待恢复的那一场，
        等开场成功（sig_scene_info，场景真的开了）再调 worker.restore_scene()；答 No →
        清掉存档、从头演（清档是用户明确的选择，不是静默丢弃）。

        调用点：switch_scene（覆盖「打开场景」菜单 / 管理场景弹窗 / 新建场景三条路径）
        与 app.py 启动路径（构造窗口后、开场前调一次）。
        """
        if scene_path is None:
            return
        path = Path(scene_path)
        if not scenestore_mod.has_runtime(path):
            return
        if ask_resume(self, self._t.t("dlg.resume_title"), self._t.t("dlg.resume_ask")):
            self._resume_pending = path
        else:
            scenestore_mod.clear_runtime(path)

    def _on_reopen(self) -> None:
        """开新场：收束后也有效（评审 #1）。立即恢复可输入与运行态，随后等新场景信息落地。"""
        if not self._session_active:
            return
        self._finished = False
        self._paused = False
        self._auto_paused = False
        self._pause_btn.setText(self._t.t("btn.pause"))
        self._pause_btn.setEnabled(True)
        self._enable_input(True)
        self._worker.restart(self._current_opening(),
                             live=self._live_check.isChecked())

    def _on_toggle_pause(self) -> None:
        if not self._started or self._finished:
            return
        self._paused = not self._paused
        self._worker.set_paused(self._paused)
        self._pause_btn.setText(self._t.t(self._pause_button_key()))

    def _on_stop(self) -> None:
        """停止按钮：立即收束本场。停止后暂停/继续失效，但开新场保留可用。"""
        if not self._started or self._finished:
            return
        self._stop_btn.setEnabled(False)     # 停止不可撤销；worker 收束后 sig_finished
        self._pause_btn.setEnabled(False)    # 已停止 → 暂停/继续不再有意义
        self._worker.stop_now()

    def _on_speed(self, ms: int) -> None:
        sec = ms / 1000.0
        self._speed_label.setText(self._t.t("metric.speed_value", sec=f"{sec:.1f}"))
        self._worker.set_pace(sec)

    def _on_rate_changed(self, index: int) -> None:
        """流速档（0.25×…60×）：改全局流速，worker 侧折现当前时刻再换 rate（不丢时间）。"""
        rate = float(self._rate_combo.itemData(index))
        self._worker.set_rate(rate)

    def _on_live_toggled(self, checked: bool) -> None:
        """真实模型开关：改配置后用新后端重开一场（收束后依旧可用）。"""
        self._cfg.live = bool(checked)
        if self._session_active:
            self._worker.restart(self._current_opening(), live=checked)

    # ------------------------------------------------------------ 场景库
    def _characters_dir(self) -> Path:
        """角色库目录（目录即库）：当前装载卡所在目录。

        空场（本场一张卡都没有 → `_cfg.characters` 为空）时依次回落：场景目录的**兄弟**
        `characters/`（内置素材与常见工程布局都是 scenes/ 与 characters/ 并列）→ 内置
        `app/characters/`。这样「库里任何角色都能在任意时刻加进本场」（§3.2）在空场下
        仍然成立——引擎拿到的角色库目录不会是 None。
        """
        if self._cfg.characters:
            return Path(self._cfg.characters[0]).parent
        scene = self._cfg.scene
        if scene is not None:
            sibling = Path(scene).parent.parent / "characters"
            if sibling.is_dir():
                return sibling
        return _APP_DIR / "characters"

    def _scenes_dir(self) -> Path:
        """场景库目录：当前场景所在目录（无则回落到内置 scenes/）。"""
        if self._cfg.scene is not None:
            return Path(self._cfg.scene).parent
        return _APP_DIR / "scenes"

    def _on_open_library(self) -> None:
        """「管理场景库…」/ 空状态页「打开场景」：场景库弹窗里选中一个场景 → 立即切场。

        切场是用户的明确动作，不再二次确认；取消（Rejected）或没选场景则什么都不动。
        阵容取**场景自己的**名单（§3.1）；若弹窗还提供 `chosen_characters`（旧的
        「角色/场景库」写法的遗留口径，只为兼容老调用方），才用它当显式选择。
        """
        dlg = self._make_library_dialog()
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        scene = getattr(dlg, "chosen_scene", None)
        if scene is None:
            return
        chosen = [Path(p) for p in (getattr(dlg, "chosen_characters", None) or [])]
        self.switch_scene(Path(scene), chosen or None)

    def _make_library_dialog(self):
        """造一个库弹窗：兼容「(角色目录, 场景目录, parent)」与「(场景目录, parent)」两种签名。

        另一个 agent 正在把 LibraryDialog 改成**只列场景**的「场景库」；按签名参数个数选
        调用式，故两种实现在并行期都能跑（签名数不出来时按新口径试）。
        """
        try:
            params = list(inspect.signature(LibraryDialog.__init__).parameters.values())
        except (TypeError, ValueError):
            params = []
        positional = [p for p in params
                      if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
                      and p.name != "self"]
        if len(positional) >= 3:
            return LibraryDialog(self._characters_dir(), self._scenes_dir(), self)
        return LibraryDialog(self._scenes_dir(), self)

    def _validate_scene_materials(self, scene: Path, characters: list[Path]) -> str:
        """切场前的素材校验（空串 = 可开场）：按**场景自己的名单**把关（§3.1/§3.3）。

        用「场景声明口径」（cast_from_cards=False）：场景里点名的每一个人都得有可读的卡
        ——缺卡/坏卡/重名卡在切换前就拦下并指明是谁（比切过去才「开场失败」清楚得多），
        时间字段照旧校验。**空阵容天然通过**：没有人要卡，空场照样开得起来。
        characters 是这些名字在库里配到的卡（打开场景时由 `_characters_for_scene` 配齐）。
        """
        return validate_materials(scene, characters, cast_from_cards=False)

    def switch_scene(self, scene: Path, characters: list[Path] | None = None) -> bool:
        """切到另一套场景/角色：先校验、再更新配置 → 清空对白/日志区 → 重新投递开场。

        **阵容由场景自己持有**（§3.1）：characters 缺省即该场景的 characters/participants
        在角色库里配到的卡（`_characters_for_scene`），**不要求用户先选人**；空阵容是合法
        场次（无人活动，场景照样能开场）。显式传 characters 只留给仍然提供角色选择的旧
        调用方（库弹窗的兼容路径）。

        沿用现有的模型/竞价/run_root/live/api_key/closing_at_block 与开场输入框当前值
        （与 start_session 同一入口，故配置口径天然一致）；旧场的对白与 think 日志在
        新场景信息落地前先清空，避免两场内容混在一屏。

        先校验（能读、cast 闭环、时间合法，见 _validate_scene_materials）再动手：素材
        打不开时只弹提示、当前场次原样保留（不清面板、不改 cfg、不重投开场），否则切过去
        才发现开场失败，正在进行的对话已经没了、也无法回滚。返回是否真的切了。
        """
        scene = Path(scene)
        if characters is None:
            characters = self._characters_for_scene(scene)
        characters = [Path(p) for p in characters]
        err = self._validate_scene_materials(scene, characters)
        if err:
            warn(self, self._t.t("err.cannot_switch"), err)
            return False
        self._cfg.scene = scene
        self._cfg.characters = list(characters)
        self._cfg.live = bool(self._live_check.isChecked())
        # 续演询问（§5）：所有「打开场景」路径都汇到 switch_scene，故只在这一处问一次
        # （打开场景菜单 / 管理场景弹窗 / 新建场景三条路径自动同款）。
        self._maybe_resume(scene)
        self._view.clear()
        self._log_view.clear()
        self._log_entries = []
        self._conv_msgs = []
        self._retracted_ids = set()
        self._narration = {}
        self._finished = False
        self._paused = False
        self._auto_paused = False
        self._pause_btn.setText(self._t.t("btn.pause"))
        self._pause_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self.start_session()
        return True

    # ------------------------------------------------------------ 部件工厂
    def _card(self, title_key: str | None) -> tuple[QFrame, QVBoxLayout]:
        """一张白卡片；title_key 给了就加一行卡标题（i18n 键，切语言时随 _retranslate 重设）。"""
        card = QFrame()
        card.setObjectName("card")
        v = QVBoxLayout(card)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(8)
        if title_key:
            t = QLabel()
            t.setObjectName("cardTitle")
            self._reg(t, title_key)
            v.addWidget(t)
        return card, v

    def _add_kv(self, layout: QVBoxLayout, key: str, value: str,
                key_width: int = KEY_WIDTH) -> QLabel:
        """一行「键　值」→ 值标签（左栏各卡的静态行统一走这里；key 是 i18n 键）。"""
        return self._add_kv_widget(layout, key, value, key_width)[1]

    def _add_kv_widget(self, layout: QVBoxLayout, key: str, value: str,
                       key_width: int = KEY_WIDTH) -> tuple[QWidget, QLabel]:
        """一行「键　值」→ (行部件, 值标签)。

        比 _add_kv 多给出**整行部件**，供需要整行显隐的行使用（如「日期」只在场景设了
        日期时才出现——隐藏整行而不是留一个空标签）。键固定宽度、值一律 wordWrap，
        故窄栏下换行而不是截断。键文本走 t() 且登记进重译表（切语言即换字）。
        """
        row_w = QWidget()
        row = QHBoxLayout(row_w)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        k = self._reg(QLabel(), key)
        k.setFixedWidth(key_width)
        self._pstyle(k, "muted12")
        val = QLabel(value)
        val.setWordWrap(True)
        self._pstyle(val, "text13")
        val.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(k)
        row.addWidget(val, 1)
        layout.addWidget(row_w)
        return row_w, val

    # ------------------------------------------------------------ 富文本/徽标
    def _format_message(self, m: dict) -> str:
        """一条消息 → 对白区 HTML（四种：场景叙述 / 导演 / 人类气泡 / 角色台词）。

        每段都写死 align（叙述居中、其余左/右对齐）：对白区整屏重绘时逐段各归各位，
        不靠 Qt 的段落继承。锚点 href 携带消息 id，供撤销/改写回指到具体那一条。
        """
        content = html.escape(m.get("content", ""))
        content = content.replace("\r", "").replace("\n", "<br>")
        stype = m.get("speaker_type")
        # 配色取当前主题的色表（切主题时 apply_theme 会整屏重绘，颜色不留旧主题的）。
        pal = self._pal
        # 虚拟钟时刻戳（worker 派发时补的 time_hhmmss = "HH:MM:SS"）：灰字置前。
        stamp = ""
        if m.get("time_hhmmss"):
            stamp = (f'<span style="color:{pal.time_text};font-size:12px;">'
                     f'{html.escape(str(m["time_hhmmss"]))} </span>')
        if stype == "narrator":
            # 场景叙述：居中斜体弱化块，无说话人名，尾随两个行内链接（撤销 / 改写）。
            return (f'<p align="center" style="text-align:center;margin:8px 0;'
                    f'color:{pal.narrator_text};font-style:italic;font-size:13px;">'
                    f'{stamp}{content}{self._narration_links(m)}</p>')
        if stype == "director":
            return (f'<p align="left" style="text-align:left;margin:5px 0;'
                    f'color:{pal.director_text};font-style:italic;font-size:13px;">'
                    f'{stamp}{content}</p>')
        if stype == "human":
            you = html.escape(str(m.get("speaker") or self._t.t("speaker.you")))
            return (f'<p align="right" style="text-align:right;margin:0 0 8px 0;">'
                    f'<span style="background-color:{pal.human_bubble_bg};'
                    f'color:{pal.text};border:1px solid {pal.human_bubble_edge};'
                    f'border-radius:12px;padding:6px 12px;line-height:1.55;">'
                    f'{stamp}<span style="color:{pal.text};font-weight:700;">'
                    f'{you}</span>'
                    f'<span style="color:{pal.text};">　{content}</span></span></p>')
        name = html.escape(str(m.get("speaker", "")))
        color = self._name_colors.get(m.get("speaker"), pal.chip_text)
        return (f'<p align="left" style="text-align:left;margin:0 0 6px 0;'
                f'line-height:1.55;">'
                f'{stamp}<span style="color:{color};font-weight:700;">{name}</span>'
                f'<span style="color:{pal.text};">　{content}</span></p>')

    def _narration_links(self, m: dict) -> str:
        """叙述行尾「撤销 · 改写」两个行内链接（href = scene:undo/edit:<id>）。

        id 缺失（非落盘消息）时不画链接——没有回指目标，点了也无从下手。
        """
        mid = m.get("id")
        if not isinstance(mid, int):
            return ""
        style = (f"color:{self._pal.link_text};font-style:normal;"
                 f"text-decoration:none;")
        undo = self._t.t("narration.undo")
        rewrite = self._t.t("narration.rewrite")
        return (f'<br><a href="{ANCHOR_UNDO}{mid}" style="{style}">{undo}</a>'
                f'<span style="{style}"> · </span>'
                f'<a href="{ANCHOR_EDIT}{mid}" style="{style}">{rewrite}</a>')

    @staticmethod
    def _local_hhmmss(iso: str | None) -> str:
        """think 条目的 UTC ISO `at` → 本地 HH:MM:SS（解析失败给空串，免崩）。"""
        if not iso:
            return ""
        try:
            dt = datetime.fromisoformat(str(iso)).astimezone()
            return dt.strftime("%H:%M:%S")
        except (ValueError, TypeError, OverflowError):
            return ""

    def _format_think(self, e: dict) -> str:
        """把一条 think 只读日志渲染成与对白同一家族的对齐样式（单段落）。

        [HH:MM:SS] · <角色名>（思考）: 冲动 x.xx · 唤醒 x.xx · 目标 x.xx
          （可选次行）被点名… / 印象… / 听到…
        角色名用其专属配色；`at` 是 UTC ISO，转本地 HH:MM:SS 显示。
        起首的「·」是**极简几何记号**（§3.6：不用 emoji），与场景变更行同款。
        """
        pal = self._pal
        speaker = str(e.get("speaker", ""))
        res = e.get("result") or {}
        hh = html.escape(self._local_hhmmss(e.get("at")))
        color = self._name_colors.get(speaker, pal.chip_text)

        def _fmt(v) -> str:
            try:
                return f"{float(v):.2f}"
            except (TypeError, ValueError):
                return "—"
        urge = _fmt(res.get("urge"))
        aroused = _fmt(res.get("aroused"))
        goal = _fmt(res.get("goal_progress"))
        stamp = (f'<span style="color:{pal.time_text};font-size:12px;">'
                 f'[{hh}] </span>' if hh else "")
        head = (f'{stamp}<span style="color:{pal.muted_text};">· </span>'
                f'<span style="color:{color};font-weight:700;">'
                f'{html.escape(speaker)}</span>'
                f'<span style="color:{pal.text};">（思考）: 冲动 {urge}'
                f' · 唤醒 {aroused} · 目标 {goal}</span>')
        extras: list[str] = []
        addressed = res.get("addressed")
        if addressed:
            extras.append(f"被点名 {html.escape(str(addressed))}")
        imp = res.get("impression_of_speaker")
        if imp:
            extras.append(f"印象 {html.escape(str(imp))}")
        heard = e.get("heard")
        if heard:
            extras.append(f"听到 {html.escape(str(heard))[:60]}")
        sub = ""
        if extras:
            inner = ("　".join(
                f'<span style="color:{pal.muted_text};">{x}</span>' for x in extras))
            sub = (f'<br><span style="color:{pal.time_text};font-size:12px;">'
                   f'{inner}</span>')
        return (f'<p style="margin:0 0 5px 0;line-height:1.5;">'
                f'{head}{sub}</p>')

    def _clock_now_hhmmss(self) -> str:
        """取左栏「当前」正在展示的虚拟钟时刻作人类气泡的时间戳（HH:MM:SS）。

        平时 label 已是 HH:MM:SS（worker 指标刷新）；刚开场时可能是 HH:MM（起始值），
        补 :00；其它占位/空值返回 ""（此时通常也还没到 can_say，不会走到气泡）。
        """
        now = (self._clock_time["now"].text() or "").strip()
        if re.fullmatch(r"\d{2}:\d{2}:\d{2}", now):
            return now
        if re.fullmatch(r"\d{2}:\d{2}", now):
            return f"{now}:00"
        return ""

    def _append_human(self, text: str, hhmmss: str) -> None:
        """把「你」的发话即时上屏为右侧圆角气泡 + 当前虚拟钟 HH:MM:SS 前缀。

        与角色/导演的左对齐段落明显区分：右对齐、浅青绿底色气泡、你 标签加粗。
        文本经 html.escape 防注入；调用方只负责在 can_say 通过后调用（本方法不再判态）。
        同样进本地留存（无 id，撤销/改写只针对有 id 的叙述行）并整屏重绘，故叙述被撤销
        重绘时这些气泡原样保留。
        """
        # after_id = 落屏时最近一条**有 id** 的行（见 _reconcile_retracted）：回溯把那条
        # 上下文作废时，写在那段上下文上的这句气泡也该一起消失。
        self._conv_msgs.append({"speaker": "你", "speaker_type": "human",
                                "content": text, "time_hhmmss": hhmmss,
                                "after_id": self._last_msg_id()})
        self._rerender_conversation()

    def _pill(self, text: str, bg: str, fg: str) -> QLabel:
        lb = QLabel(text)
        lb.setStyleSheet(
            f"QLabel{{background:{bg};color:{fg};border-radius:9px;"
            f"padding:2px 10px;font-size:11px;}}")
        return lb

    def _status_text(self, text: str) -> str:
        """worker 状态串 → 当前语言：**已知**词表项按 key 翻，未知去记号后原样显示。

        worker 报来的是已格式化的中文串（就绪/进行中/已达 N 块，点继续…/开场失败：…），
        窗口不重写 worker，只把认识的那几个按 _STATUS_KEYS 映射；带数字/原因的原样显示。
        未知串先经 `strip_status_mark` 摘掉开头的记号（worker 侧数据里可能带的旧前缀）——
        界面因此不出现任何符号（§3.6），worker 的现场信息一字不丢。
        """
        key = _STATUS_KEYS.get(text)
        return self._t.t(key) if key is not None else strip_status_mark(text)

    def _set_status_chip(self, text: str, style: tuple[str, str] | None = None) -> None:
        """状态区药丸：style=(底色, 前景) 显式指定（错误/提示），否则按**原始文**查表。

        原始文与样式都记下来：切语言时 _retranslate 按同一份原文重译（词表映射），
        故换语言后状态药丸仍是对的，且未知状态不会被改写。
        """
        self._status_raw = text
        self._status_style = style
        bg, fg = style if style is not None else _STATUS_STYLE.get(text, ("#e7e5e4", "#57534e"))
        self._status_chip.setStyleSheet(
            f"QLabel{{background:{bg};color:{fg};border-radius:9px;"
            f"padding:2px 10px;font-size:11px;font-weight:600;}}")
        self._status_chip.setText(self._status_text(text))

    def _set_model_badge(self, backend: str) -> None:
        """模型药丸（真实模型 / 离线 stub）：底色与字色**全部取自当前主题色表**。

        真实模型 = 主题的 accent + accent_text（「在用真东西」的强调），stub = chip_bg +
        chip_text（中性小标签）——四套主题下都自洽且过 AA（accent 对与 chip 对本就各有
        对比度用例）。切主题时 apply_theme 会重调本方法，故药丸不会留着旧主题的暖纸色。
        """
        self._backend = backend
        p = self._pal
        if backend == "deepseek":
            bg, fg, text = p.accent, p.accent_text, self._t.t("badge.real_model")
        else:
            bg, fg, text = p.chip_bg, p.chip_text, self._t.t("badge.stub")
        style = self._pill(text, bg, fg)
        layout = self._model_badge.layout()
        if layout is None:
            layout = QHBoxLayout(self._model_badge)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)
            self._model_badge_layout = layout
        self._clear_layout(layout)
        layout.addWidget(style)
        self._model_badge.setMinimumHeight(22)

    @staticmethod
    def _boundary_text(is_time: bool, hhmm) -> str:
        """「边界」行的中性文案：有 time 硬边界 → "HH:MM"；无 / 非 time / 值缺失 → "—"。

        界面不写死任何「打烊」这类餐厅词：边界是场景自己的属性（可以是无），这里只把
        worker 给的时刻原样摆出来。
        """
        if not is_time:
            return "—"
        return str(hhmm).strip() or "—"

    def _fmt_usage(self, role: dict) -> str:
        """一次模型调用统计 → 「N 次 · token P+C」（token 数 ≥ 10 万走 k 单位）。"""
        calls = prompt = completion = 0
        for u in (role or {}).values():
            calls += int(u.get("calls", 0) or 0)
            prompt += int(u.get("prompt_tokens", 0) or 0)
            completion += int(u.get("completion_tokens", 0) or 0)
        return self._t.t("metric.tokens", calls=calls, prompt=fmt_count(prompt),
                         completion=fmt_count(completion))

    # ------------------------------------------------------------ 左栏冲动卡
    def _repopulate_left_urge(self, participants: list[str], *,
                              muted: dict | None = None) -> None:
        """左栏「冲动 bid（实时）」卡按**在场名单**重建：每名角色一行
        （色点 + 名 + 细实时条 + 数值占位）+ 行下一枚禁言徽标（未禁言时收起）。

        开场/重开随 sig_scene_info 填，此后每次 sig_cast 按 engine.cast_state 的 active
        重填：被移出者的行当场消失、新人当场补一行、禁言者在行下缀「禁言中（剩 N 回合）」。
        旧行句柄随之清空（_urge_rows 从零记录）；尚无在场者时留一行灰占位。
        """
        self._urge_rows = {}
        if self._urge_body is None:
            return
        self._clear_layout(self._urge_body)
        self._palette_groups["urge"] = []    # 旧行整批作废（登记表随重建清空）
        if not participants:
            ph = QLabel(self._t.t("panel.no_participants"))
            self._pstyle(ph, "muted12", group="urge")
            self._urge_body.addWidget(ph)
            return
        table = muted or {}
        for name in participants:
            row = self._add_urge_row(name, self._color_of(name))
            self._set_mute_badge(row.get("mute"),
                                 mute_badge_text(table, name, self._t))
            self._urge_rows[name] = row

    def _add_urge_row(self, name: str, color: str, *,
                      lo: float | None = None, hi: float | None = None) -> dict:
        """左栏冲动(bid)卡的一行：说话者色点 + 角色名 + 细进度条 + 右侧数值。

        返回 {"bar", "num", "dot", "cap", "lo", "hi", "decimals"} 句柄供 _set_dyn_value
        刷新。bid 用**区间标尺** lo..hi（缺省 -2..6）：bid 含负项（recency 惩罚）且正向
        分量累积后常到 2~6，用 0..cap 会把大半条挤在满格；条只截视觉，右侧显真实值。
        """
        row = QHBoxLayout()
        row.setSpacing(6)
        dot = QLabel("")
        dot.setFixedSize(8, 8)
        self._pstyle(dot, "dot", group="urge", speaker=name, accent=color)
        lab = QLabel(name)
        lab.setFixedWidth(64)
        lab.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._pstyle(lab, "text12", group="urge")
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(0)
        bar.setTextVisible(False)
        bar.setFixedHeight(6)
        self._pstyle(bar, "bar", group="urge", speaker=name, accent=color)
        num = QLabel("—")
        num.setFixedWidth(44)
        num.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._pstyle(num, "chip12", group="urge")
        row.addWidget(dot)
        row.addWidget(lab)
        row.addWidget(bar, 1)
        row.addWidget(num)
        self._urge_body.addLayout(row)
        # 禁言徽标占**行下**一行（默认收起）：行里已排满（点/名/条/值），再塞一枚徽标会把
        # 实时条挤成一根火柴；收起时不占任何高度（Qt 不给隐藏部件几何）。
        mute = QLabel("")
        mute.setWordWrap(True)
        self._pstyle(mute, "warn11", group="urge")
        mute.hide()
        self._urge_body.addWidget(mute)
        return {"bar": bar, "num": num, "dot": dot, "lab": lab, "cap": 2.0,
                "lo": -2.0 if lo is None else float(lo),
                "hi": 6.0 if hi is None else float(hi),
                "decimals": 2, "mute": mute}

    @staticmethod
    def _set_mute_badge(label: QLabel | None, text: str) -> None:
        """禁言徽标：有文案就显示（tooltip 里也放一份，窄栏被挤时仍可悬停读到）。"""
        if label is None:
            return
        label.setText(text)
        label.setToolTip(text)
        label.setVisible(bool(text))

    # ------------------------------------------------------------ 右栏角色卡
    def _repopulate_right(self, info: dict) -> None:
        """sig_scene_info 到达：收下每张卡的原始载荷，再按在场名单重建右栏角色卡。

        载荷只描述**开场那一刻**在场的人（sig_scene_info 开场/重开各发一次）；还没收到
        sig_cast 时（或本场 worker 不发 cast）就按载荷里的 characters 建卡，行为与引进
        演员表之前一致。运行期新加入的人由 _card_payload_for 就地补一份载荷。
        """
        chars = info.get("characters") or []
        self._char_payloads = {}
        for ch in chars:
            if ch.get("name"):
                self._char_payloads[str(ch["name"])] = dict(ch)
        active = self._active_names() or [str(ch.get("name")) for ch in chars
                                          if ch.get("name")]
        self._rebuild_right_cards(active)

    def _rebuild_right_cards(self, names: list[str], *,
                             muted: dict | None = None) -> None:
        """右栏按**在场名单**重建：被移出者的卡消失、新进场者补一张、禁言者缀徽标。

        数据来源见 _card_payload_for（开场载荷优先，缺则按角色库里的卡现补）。
        """
        self._dyn_state = {}               # 角色卡重建 → 实时状态句柄从零记录
        self._dyn_texts = []               # 卡内静态标签登记表同步作废（旧部件将被删）
        self._palette_groups["card"] = []  # 卡内配色登记表同步作废
        self._clear_layout(self._right_cards_layout)
        table = muted or {}
        for name in names:
            card = self._build_char_card(self._card_payload_for(name),
                                         self._color_of(name))
            self._right_cards_layout.addWidget(card)
            refs = self._dyn_state.get(str(name))
            if refs is not None:
                self._set_mute_badge(refs.get("mute"),
                                     mute_badge_text(table, name, self._t))
        self._right_cards_layout.addStretch(1)

    def _card_payload_for(self, name: str) -> dict:
        """在场者 → 右栏卡/详情弹窗的载荷：开场载荷优先，缺则按角色库里的卡现补一份。

        运行期新加入的人不在开场载荷里（sig_scene_info 只发一次），而他的卡多半就在角色
        库里——就地按与 worker.build_scene_payload 相同的字段补一份（只含卡本身的公开
        设定），右栏卡与「查看详情」都能正常用；库里也没有（卡被删了）才只剩名字 + 实时
        分量，「查看详情」会给一句「没有详情可看」。
        """
        key = str(name)
        ch = self._char_payloads.get(key)
        if ch is not None:
            return ch
        for path, card_name in self._character_cards():
            if card_name != key:
                continue
            try:
                card = load_character_card(path)
            except (OSError, ValueError):
                break
            payload = {
                "name": card.name or key,
                "personality": dict(card.personality or {}),
                "abilities": list(getattr(card, "abilities", None) or []),
                "relationships": dict(card.relationships or {}),
                "weights": card.weights.model_dump(),
                "emotion_decay_rate": float(card.emotion_decay_rate),
                "corpus": card.corpus.model_dump(),
            }
            self._char_payloads[key] = payload
            return payload
        return {"name": key}

    def _build_char_card(self, ch: dict, accent: str) -> QFrame:
        """右栏角色卡（S2a 瘦身）：只留姓名（+色点）与「实时分量（随情景演化）」。

        设定/能力/关系/性格权重（w1…w7）/情绪衰减/语料全部移出卡片，改由卡右上角的
        灰色「查看详情」按钮弹 CharacterDetailDialog 只读展示——卡片只承担「谁在场、
        此刻多想说话」这件实时的事，静态资料按需查看。
        """
        card, v = self._card(None)
        v.setSpacing(6)

        head = QHBoxLayout()
        head.setSpacing(8)
        speaker = str(ch.get("name") or "")      # 空名不登记（不给「无名者」染色表项）
        avatar = QLabel(speaker[:1] or "?")
        avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        avatar.setFixedSize(34, 34)
        # 头像圆底＝该角色的说话者色（accent 由 _color_of 给，切主题时随配色盘重算），
        # 圆内首字取主题的「强调色上文字」——浅底主题是白字，深色主题是压近黑字。
        self._pstyle(avatar, "avatar", group="card", speaker=speaker or None,
                     accent=accent)
        name = QLabel(ch.get("name", ""))
        self._pstyle(name, "name", group="card")
        head.addWidget(avatar)
        head.addWidget(name)
        head.addStretch(1)
        detail = self._reg(QPushButton(), "btn.details", dynamic=True)
        detail.setObjectName("ghost")
        detail.setToolTip(self._t.t("tip.details"))
        detail.clicked.connect(
            lambda _checked=False, n=str(ch.get("name") or ""): self._on_show_detail(n))
        head.addWidget(detail)
        v.addLayout(head)
        # 禁言徽标（默认收起）：sig_cast 报出该角色被禁言时展开「禁言中（剩 N 回合）」。
        mute = QLabel("")
        mute.setWordWrap(True)
        self._pstyle(mute, "warn12", group="card")
        mute.hide()
        v.addWidget(mute)

        # ---- 实时分量（随情景演化；worker 每轮广播 sig_dynamics→数值快照刷新）。
        # 相关度/唤醒/邻接/目标/场景等是“实时变量”，只在这里展示；固定性格系数
        # （w1..w7、情绪衰减）已移到「查看详情」，避免同一概念两处重复。
        self._add_heading(v, "card.components")
        name = str(ch.get("name") or "")

        def _row(label_key: str, **kw) -> dict:
            """本卡的实时数值行（强调色＝该角色的说话者色，随主题重算）。"""
            return self._add_dyn_value_row(v, label_key, accent, speaker=name, **kw)

        refs: dict = {
            "bid": _row("card.bid", lo=-2.0, hi=6.0),
            "turns": _row("card.silence", cap=5.0, decimals=0),
            "relevance": _row("card.relevance"),
            "arousal": _row("card.arousal"),
            "adjacency": _row("card.adjacency"),
            "goal_pressure": _row("card.goal_pressure"),
            "scene_pressure": _row("card.scene_pressure"),
            # 自报冲动（think.urge，-1~2）：按 urge_gain 加权计入 bid，负数=本人不想说。
            # 只读展示；满格参考取 2（schema 上限），条对负值截在 0 而文本仍显真值。
            "self_urge": _row("card.urge", cap=2.0),
            # 被点名欠下的回应义务（未答每轮翻倍）——义务可涨到 64，条以 8 为满格参考。
            "pending": _row("card.pending_pressure", cap=8.0),
            "respond": self._add_dyn_text(v, "card.addressed"),
            "mute": mute,
        }
        if name:
            self._dyn_state[name] = refs
        return card

    def _add_heading(self, v: QVBoxLayout, key: str) -> None:
        """小节标题（i18n 键）——右栏角色卡内，随卡片重建重登记。"""
        h = self._reg(QLabel(), key, dynamic=True)
        h.setObjectName("section")
        v.addWidget(h)

    # ------------------------------------------------------ 角色详情（只读弹窗）
    def _open_character_detail(self, name: str) -> CharacterDetailDialog | None:
        """构造某角色的「查看详情」弹窗（不 exec；调用方负责 exec，便于复用/测试）。

        数据取本场 sig_scene_info 里那张卡的原始载荷（_char_payloads）——只含卡本身的
        公开设定，绝不放私有思考/实时运行态。查无此人（如已换场）返回 None。
        """
        ch = self._char_payloads.get(str(name))
        if ch is None:
            return None
        return CharacterDetailDialog(ch, self)

    def _on_show_detail(self, name: str) -> None:
        """角色卡右上「查看详情」：模态只读展示该卡的完整资料（设定/能力/关系/权重/语料）。"""
        dlg = self._open_character_detail(name)
        if dlg is None:
            self._set_status_chip(self._t.t("err.no_details", name=name),
                                  style=("#fef3c7", "#92400e"))
            return
        dlg.exec()

    def _add_dyn_value_row(self, v: QVBoxLayout, label_key: str, accent: str, *,
                           cap: float = 1.0, decimals: int = 2,
                           lo: float | None = None, hi: float | None = None,
                           speaker: str | None = None) -> dict:
        """实时数值条（实时分量用）：初值占位「—」，_set_dyn_value 之后才显数值。

        label_key 是 i18n 键（右栏卡内标签，随卡片重建登记进 _dyn_texts）。
        speaker 给出所属角色名时，条的角色色随主题重算（切主题换说话者配色盘）。

        返回 {"bar": QProgressBar, "num": QLabel, "cap": float, "decimals": int}
        句柄，供 sig_dynamics 刷新。cap 控制进度条满格参考值：0..1 分量 cap=1，
        bid 可超 1 取 cap=2，沉默轮数取 cap=5（文本按其整数显示），待回应压力取 cap=8
        （义务翻倍可涨到 64，条满格后仍显真实文本）。
        """
        row = QHBoxLayout()
        row.setSpacing(6)
        lab = self._reg(QLabel(), label_key, dynamic=True)
        lab.setFixedWidth(78)
        self._pstyle(lab, "muted12", group="card")
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(0)
        bar.setTextVisible(False)
        bar.setFixedHeight(6)
        self._pstyle(bar, "bar", group="card", speaker=speaker, accent=accent)
        num = QLabel("—")
        num.setFixedWidth(40)
        num.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._pstyle(num, "chip12", group="card")
        row.addWidget(lab)
        row.addWidget(bar, 1)
        row.addWidget(num)
        v.addLayout(row)
        return {"bar": bar, "num": num, "lab": lab, "cap": cap, "decimals": decimals,
                "lo": lo, "hi": hi}

    def _add_dyn_text(self, v: QVBoxLayout, label_key: str) -> QLabel:
        """实时状态文本行（被点名/回应对象：addressed）：换行小字 + 占位「—」。
        label_key 是 i18n 键；返回 QLabel 句柄供刷新。"""
        row = QHBoxLayout()
        row.setSpacing(6)
        lab = self._reg(QLabel(), label_key, dynamic=True)
        lab.setFixedWidth(96)              # 容纳「被点名/回应对象」8 字长标签
        self._pstyle(lab, "muted12", group="card")
        val = QLabel("—")
        val.setWordWrap(True)
        self._pstyle(val, "chip12", group="card")
        val.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        row.addWidget(lab)
        row.addWidget(val, 1)
        v.addLayout(row)
        return val

    # ------------------------------------------------------------ 状态切换
    def _enable_input(self, enabled: bool) -> None:
        """只控制输入框与发送按钮；不再影响「会话已启动」标记（评审 #1 拆分）。"""
        self._input_edit.setEnabled(enabled)
        self._send_btn.setEnabled(enabled)

    def _clear_layout(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
            elif item.layout() is not None:
                self._clear_layout(item.layout())
