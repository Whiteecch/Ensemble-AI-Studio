"""应用内编辑器与场景库：角色卡编辑器 + 场景编辑器 + 场景库（多语言 §7）。

五个对话框（只被 gui 包内 app.py / main_window.py 调用，不反向依赖它们）：
  CharacterEditorDialog  编辑一张角色卡（含语料 corpus：出处/风格/思维/口头禅/样例）
  SceneEditorDialog      编辑一个场景（基本信息/背景与设定/出场角色/hooks/硬边界）
  HookEditorDialog       「场景编辑器」里编辑一条 hook（条件/事件类型/分类字段/显隐）
  LibraryDialog          **场景库**（§3.3）：只列场景——打开/新建/编辑/复制/删除/导入
  CharacterDetailDialog  角色卡「查看详情」（只读）：右栏角色卡瘦身后，设定/能力/关系/
                         性格权重/情绪衰减/语料改由它展示

开场设置小窗已随 §3.3 删除——启动直达主界面，场景由空状态页 /「场景」菜单自己挑，
故本模块不再有「开场选择器」。

「场景库」里**不再选角色**（§3.3 摒弃两列库）：初始阵容只在场景编辑器的候选列表里
勾，之后增删走顶栏「角色」菜单（main_window 的角色管理弹窗）。角色卡的编辑/新建仍由
CharacterEditorDialog 承担，它不依赖任何列表。

场景按《场景编排与桌面外壳_设计文档》§3 的新模型编辑：场景名与文件名**解耦**（文件名
新建时按场景名生成一次安全 slug，此后由用户自由改；改场景名不动文件名），可选日期
（勾选框 + 年/月/日），背景/描述(含「可改变的」)/剧情设定，出场角色走**候选列表**
（按角色卡的 corpus.source 筛「出场作品」，超过 6 行即定高滚动），hooks 是带校验的
列表（hooks.validate），硬边界类型含「无」。对话圈（§3.2 已随模型删除）在本模块不留
任何界面或数据。`reset_scene_runtime` 是「重置场景」的纯函数内核，导出可复用。

素材即文件：库内容由 loaders.list_character_paths / list_scene_paths 扫目录得到
（往 app/characters、app/scenes 丢一个 json 即被收录）；保存一律经 loaders.save_*
（UTF-8、不转义、缩进 2），落盘即可被扫描到 → 目录即插即用。坏 json 不该让对话框
崩掉：列举/预览一律回退文件名 stem，错误留到真正装载时暴露。

角色卡仍是「名字即文件名」，故角色卡的落盘入口先过 filename_error：拒绝路径分隔符、
`..`、前导/尾随点、Windows 非法字符与保留设备名，只有 material_path 这一处把「显示名」
换算成「目录/安全名字.json」，绝不让名字把文件写到库目录之外。场景则按 §3.4 把场景名
与文件名**分开**：落盘一律走 loaders.scene_path_for(scene_dir, filename)，场景名只作
显示、改名不动文件。存储前还要过两关（都在保存前拦下、只就地报错不落盘）：编辑器自身
校验（名非空、文件名安全、时间 HH:MM、每个勾上的角色都得有卡、每条 hook 过
hooks.validate），以及 validate_materials——切场前对「文件是否真能开场」的一次预检
（主窗口用，避免切过去才发现开场失败、对话已丢）。


样式复用主窗口的 QSS object name（primary/ghost/danger/section/hairline）；对话框以主窗口
为 parent，故主窗口样式表自会级联下来，这里再按**当前配色主题**补一份弹窗专属样式
（`theme.dialog_qss`：对话框底 + 输入控件/列表/表格/分组框/数值控件外观 + 主题主表）——
深色/深蓝主题下弹窗不会白底黑字，与主窗风格一致。本模块**不留任何字面颜色**：需要色的
地方一律取自 `palette()`（当前主题色表），故四套主题下观感一致、且数值一律等宽排版
（§3.6 视觉语言：科技感 · 简约 · 高级 · 无 AI 味，不用 emoji）。

**多语言（§7）**：对话框都是**短命**的（打开→关掉），故按「打开那一刻的界面语言」一次性
取字即可——每个 `__init__` 开头 `t = translator()`，全部静态文案走 `t.t(键)`；模块级
`app_language()` 读的是主窗口 `apply_language` 写下的 app 属性 `language`（无 Qt 应用
或属性未设 → zh-Hans）。纯函数里的用户可见文案（`filename_error` / `validate_materials`
的错误串）走模块级 `_t(键)`，同样按当前语言。zh-Hans 下逐字与接线前完全一致。
"""
from __future__ import annotations

import dataclasses
import re
from pathlib import Path

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QFileDialog,
    QFrame, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSlider, QSpinBox,
    QStackedWidget, QVBoxLayout, QWidget,
)

from .. import hooks as hooks_mod
from ..hooks import Hook
from ..i18n import DEFAULT_LANGUAGE, LANGUAGES, Translator
from ..loaders import (
    list_character_paths, list_scene_paths, load_character_card, load_scene,
    save_character_card, save_scene, scene_filename_error, scene_path_for)
from ..sceneclock import parse_hhmm
from ..schemas import (
    CharacterCard, Corpus, HardBoundary, Scene, SceneCastMember, Weights)
from ..template_import import (
    ImportResult, TemplateError, import_template_file)
from .settings import SettingsStore
from .theme import THEMES, dialog_qss, palette_for


# ------------------------------------------------------------------ 多语言（§7）
def app_language() -> str:
    """当前界面语言：优先 app 动态属性 `language`（主窗口 apply_language 写入）。

    没有 QApplication（纯逻辑测试 / CLI）或属性值不是已知语言码 → 回落 zh-Hans。
    绝不抛：属性读不到也只是回落默认，界面永远有字可显示。
    """
    try:
        app = QApplication.instance()
        code = app.property("language") if app is not None else None
    except Exception:                       # noqa: BLE001 - Qt 未初始化/已销毁
        code = None
    return code if isinstance(code, str) and code in LANGUAGES else DEFAULT_LANGUAGE


def app_theme() -> str:
    """当前界面主题名（`默认/深色/白色/深蓝`）。

    取值顺序：① app 动态属性 `theme`（MainWindow 构造与 apply_theme 都写它——窗口活着
    时它才是权威）；② app.py 启动时也会先写一次，故 MainWindow 之前的任何弹窗同样拿得到；
    ③ 都取不到（纯逻辑测试 / CLI / 属性非法）→ 读**保存的设置**里的主题；
    ④ 读盘也失败 → `默认`。任何一步都不抛，弹窗永远有样式可出。
    """
    try:
        app = QApplication.instance()
        code = app.property("theme") if app is not None else None
    except Exception:                       # noqa: BLE001 - Qt 未初始化/已销毁
        code = None
    if isinstance(code, str) and code in THEMES:
        return code
    try:
        return SettingsStore().load().theme
    except Exception:                       # noqa: BLE001 - 设置读不到也给个主题
        return THEMES[0]


def translator(language: str | None = None) -> Translator:
    """取一个译者：给了语言就用它，否则按当前界面语言（app_language）。"""
    return Translator(language) if language is not None else Translator(app_language())


def _t(key: str, **fmt) -> str:
    """按当前界面语言取文案（模块级纯函数，供无对话框上下文的校验/命名错误用）。"""
    return translator().t(key, **fmt)


#: 心理权重字段 → 短标签（w1…w7 的唯一短名表：编辑器滑杆与「查看详情」都用它，省横向空间）。
WEIGHT_FIELDS = [
    ("w1_relevance", "相关 w1"),
    ("w2_arousal", "唤醒 w2"),
    ("w3_adjacency", "邻接 w3"),
    ("w4_goal_pressure", "目标 w4"),
    ("w5_talkativeness", "话痨 w5"),
    ("w6_inhibition", "抑制 w6"),
    ("w7_scene_pressure", "场景 w7"),
]

#: 硬边界类型（combo 显示名, 值）。「无 none」是缺省（§3.1 新增），与 hard_boundary=None
#: 同义；其余类型才用值/描述。
BOUNDARY_TYPES = [("无 none", "none"), ("时间 time", "time"),
                  ("物理 physical", "physical"), ("社会 social", "social"),
                  ("目标 goal", "goal")]

#: hook 事件类型（combo 显示名, 值）。
HOOK_KINDS = [("上下文事件", "context"), ("角色事件", "character"), ("场景事件", "scene")]

#: 场景事件（hook.scene_patch）能改的字段：**引擎真键** → 界面上给人类看的中文名。
#: 键集合必须与引擎的 `engine._SCENE_HOOK_KEYS` 完全一致——引擎只认这些键，其它键
#: （含「描述」这类展示名）一律静默忽略并把补丁丢进诊断。故组装 hook 时一律换回真键。
SCENE_PATCH_FIELDS: list[tuple[str, str]] = [
    ("name", "场景名"),
    ("description", "场景描述"),
    ("background", "场景背景"),
    ("plot_direction", "剧情走向"),
    ("date", "日期"),
]

#: 中文展示名 → 引擎真键（用户照着界面标签敲「场景描述」也照样生效）。
SCENE_PATCH_LABELS: dict[str, str] = {label: key for key, label in SCENE_PATCH_FIELDS}

#: 旧占位符教过 / 口语化的简称 → 引擎真键（兼容老用户的手感，不是新字段）。
SCENE_PATCH_ALIASES: dict[str, str] = {
    "描述": "description", "背景": "background",
    "剧情": "plot_direction", "剧情设定": "plot_direction",
    "场景": "name", "名字": "name", "日期": "date",
}


def scene_patch_pairs(text: str) -> dict[str, str]:
    """多行「键：值」→ 场景补丁 dict：展示名/真键都认，统一换成引擎的字段真键。

    未知键原样保留（不改写用户输入）：引擎会以「想改不可改的字段」记一条可见诊断，
    比在这里静默丢弃更诚实。空键/空行由 `_pairs` 统一过滤。
    """
    out: dict[str, str] = {}
    for key, value in _pairs(text).items():
        out[SCENE_PATCH_LABELS.get(key)
            or SCENE_PATCH_ALIASES.get(key)
            or key] = value
    return out


def scene_patch_hint() -> str:
    """字段说明里列出的**真键**清单（name / description / …），随界面语言取字段名。"""
    return " / ".join(key for key, _label in SCENE_PATCH_FIELDS)


#: 校验新钩子（还没有 id）时顶替空 id 的占位——只要合法即可，不进任何落盘数据。
_VALID_HOOK_ID_PROBE = "new_hook"

#: hook 角色动作（combo 显示名, 值）。
HOOK_ACTIONS = [("加入", "add"), ("离场", "remove"), ("静默 N 轮", "mute_turns"),
                ("长期静默", "mute"), ("解除静默", "unmute")]

#: 出场角色候选列表的定高口径：最多显示 6 行，超出即固定高度 + 滚动（§3.4）。
_CAST_LIST_ROWS = 6
_CAST_LIST_ROW_PX = 24
_CAST_LIST_PAD_PX = 8

#: HH:MM（两位零填充、00:00~23:59）——编辑器写盘用的严格口径（引擎 parse_hhmm 更宽松，
#: 手工改过的旧文件不该被我们的守门拦下，见 validate_materials）。
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

#: 场景日期（编辑器写盘口径）：年-月-日，分隔符宽容（旧文件可能写成 2024/5/1 这种）。
_DATE_RE = re.compile(r"^\s*(\d{4})\D+(\d{1,2})\D+(\d{1,2})\s*$")

#: Windows 下不允许出现在文件名里的字符（保守起见 Linux/macOS 也一并禁）。
_ILLEGAL_NAME_CHARS = frozenset(':*?"<>|')
#: Windows 保留设备名：CON.json 之类无法正常读写。
_RESERVED_STEMS = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)] + [f"LPT{i}" for i in range(1, 10)])


def palette():
    """当前主题的色表（按 `app_theme()` 取）——弹窗里程序化配色的标签都从这里取色。

    弹窗都是短命的：打开那一刻取一次色，之后不再变（换主题只影响之后 new 出来的弹窗）。
    """
    return palette_for(app_theme())


def _dialog_stylesheet() -> str:
    """本模块各弹窗的样式表：主题色表 + 弹窗专属规则（见 theme.dialog_qss）。

    按**打开那一刻**的界面主题出表（`app_theme()`）——不再各自硬编码一份浅色样式，
    否则深色/深蓝主题下弹窗仍是白底黑字。主窗口里的弹窗走同一个函数。
    """
    return dialog_qss(app_theme())


# ------------------------------------------------------------------ 小工具
def _lines(text: str) -> list[str]:
    """多行文本 → 去空行去首尾空白的行表（GUI 的行式列表字段统一走这里）。"""
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def _split_pair(line: str) -> tuple[str, str]:
    """「键：值」→ (键, 值)；全/半角冒号都认，取最先出现的那个；无冒号则值取空串。"""
    best = -1
    for sep in ("：", ":"):
        i = line.find(sep)
        if i != -1 and (best == -1 or i < best):
            best = i
    if best == -1:
        return line.strip(), ""
    return line[:best].strip(), line[best + 1:].strip()


def _pairs(text: str) -> dict[str, str]:
    """多行「键：值」→ dict（无冒号行按空值收入，避免用户敲错就静默丢数据）。"""
    out: dict[str, str] = {}
    for ln in _lines(text):
        k, v = _split_pair(ln)
        if k:
            out[k] = v
    return out


def _join_lines(items: list[str]) -> str:
    return "\n".join(items or [])


def _join_pairs(d: dict[str, str]) -> str:
    return "\n".join(f"{k}：{v}" for k, v in (d or {}).items())


def filename_error(name: str) -> str:
    """名字能否安全用作素材文件名——空串 = 可以，否则中文原因。

    素材按名字命名（目录即库），名字直接决定落盘路径：拒绝路径分隔符、`..`、前导/尾随
    `.`、非法字符、控制字符与 Windows 保留设备名，杜绝写到库目录之外或建出子目录。
    """
    name = name or ""
    if not name.strip():
        return _t("err.name_empty")
    if name != name.strip():
        return _t("err.name_blank")
    if name.startswith("."):
        return _t("err.name_dot")
    if ".." in name:
        return _t("err.name_dotdot")
    if "/" in name or "\\" in name:
        return _t("err.name_sep")
    bad = sorted({c for c in name if c in _ILLEGAL_NAME_CHARS})
    if bad:
        return _t("err.name_illegal", chars=" ".join(bad))
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        return _t("err.name_control")
    if name.endswith("."):
        return _t("err.name_trailing_dot")
    stem = name.split(".")[0]
    if stem.upper() in _RESERVED_STEMS:
        return _t("err.name_reserved", stem=stem)
    return ""


def material_path(directory: Path, name: str) -> Path:
    """名字 → 素材文件路径；不安全的名字抛 ValueError（文案可直接给用户看）。

    显示名与文件名分离的唯一换算口：界面可以展示任意显示名，落盘一律经这里变成
    「目录 / 安全名字.json」，绝不拿名字直接拼路径。
    """
    err = filename_error(name)
    if err:
        raise ValueError(err)
    return Path(directory) / f"{name}.json"


def _unique_name(directory: Path, base: str) -> str:
    """目录下尚未占用的名字：base / base2 / base3 …（与落盘文件一一对应）。"""
    name, i = base, 2
    while (Path(directory) / f"{name}.json").exists():
        name = f"{base}{i}"
        i += 1
    return name


def _safe_stem(name: str) -> str:
    """场景名 → 可用作文件名的安全主干（不保证唯一，也不带 .json）。"""
    stem = (name or "").strip()
    stem = re.sub(r'[\\/:*?"<>|]', "_", stem)          # 路径分隔符与 Windows 非法字符
    stem = "".join("_" if (ord(c) < 32 or ord(c) == 127) else c for c in stem)
    stem = stem.strip().strip(".").strip()             # 前导/尾随点（隐藏文件、尾点）
    stem = stem[:60].strip()
    if not stem:
        return "场景"
    if stem.split(".")[0].upper() in _RESERVED_STEMS:  # CON/NUL 之类留不出文件
        stem = f"{stem}_"
    return stem


def default_scene_filename(name: str, directory: Path | None = None) -> str:
    """场景名 → **缺省**文件名（新建时用一次；此后文件名由用户自己改，改名不动文件）。

    场景名与文件名解耦（§3.4），但新场景总得有个起点：把场景名折算成安全 slug 当缺省
    文件名，带上 `.json` 后缀、必要时避开已占用的名字（directory 给了才查重），保证这个
    缺省值一次就能存得下去。用户在编辑器里改成什么都行，之后再改场景名也不会跟着变。
    """
    stem = _safe_stem(name)
    if directory is not None:
        stem = _unique_name(Path(directory), stem)
    return f"{stem}.json"


def reset_scene_runtime(scene: Scene) -> Scene:
    """「重置场景」的纯函数内核：返回运行期部分已清零的**副本**（入参一字不改，§5）。

    保留场景配置（名/日期/背景/描述/剧情/hooks/硬边界/开始时间）与在场角色名单，只把
    每个角色的入场记录清零（entered_at ""、entered_round 0）。§5 要清的是「对话与思考
    上下文」——本阶段对话历史还不挂在 Scene 上（transcript 由引擎持有），故这里能清的
    只有入场记录；对话本身的清空由引擎在后续阶段执行，届时它会复用本函数的结果。
    """
    return scene.model_copy(update={
        "characters": [SceneCastMember(name=m.name, entered_at="", entered_round=0)
                       for m in scene.characters]})


def validate_materials(scene_path: Path, character_paths: list[Path],
                       *, cast_from_cards: bool = False) -> str:
    """开场前校验（切场守门）：空串 = 可开场，否则中文原因。

    缺省（cast_from_cards=False）与引擎同一套口径（缺卡/重名卡/时间非法都在开场前拦下，
    见 SceneEngine._validate_cast 与 sceneclock.parse_hhmm），但只看文件不建引擎，故能在
    GUI 线程即时调用——不合格就不切场，免得把正在进行的对话丢掉。时间这里用引擎的宽松
    口径（parse_hhmm），手工改过的旧文件不该被编辑器之外的理由拦。场景**可以没有角色**
    （§3.3），故「没写参与者」本身不是拦下的理由；对话圈已随模型删除（§3.2），不再有
    任何圈相关的判定。

    cast_from_cards=True（桌面端 App 恒用）：演员表以**所选角色卡**为准，引擎会按选择
    改写场景（loaders.scene_with_cast → characters 换成选择），故「场景声明的某人缺卡」
    不再表示开不了场——只要求「文件读得出来 + 选择本身自洽（≥1 张卡、卡可读且不重名）
    + 时钟合法」。
    """
    try:
        scene = load_scene(scene_path)
    except (OSError, ValueError) as exc:
        return _t("err.scene_read_failed", scene_path=scene_path, exc=exc)
    cards: dict[str, Path] = {}
    for path in character_paths:
        try:
            card = load_character_card(path)
        except (OSError, ValueError) as exc:
            return _t("err.card_read_failed", path=path, exc=exc)
        if card.name in cards:
            return (f"角色卡姓名重复：{card.name}\n"
                    f"{cards[card.name]}\n{path}")
        cards[card.name] = path
    if cast_from_cards:
        # 演员表 = 选择本身：场景声明的 characters 一律不参与判定（引擎按选择改写）。
        if not cards:
            return _t("err.materials_no_characters")
    else:
        # 缺省口径：场景声明的角色都得有卡（缺卡则开场必失败）。
        missing = [n for n in scene.participants if n not in cards]
        if missing:
            return _t("err.materials_missing", names="、".join(missing))
    try:
        parse_hhmm(scene.start_time)
    except ValueError:
        return _t("err.materials_bad_start", value=repr(scene.start_time))
    hb = scene.hard_boundary
    if hb is not None and hb.type == "time":
        try:
            parse_hhmm(hb.value)
        except ValueError:
            return _t("err.materials_bad_boundary", value=repr(hb.value))
    return ""


def _scene_name(path: Path) -> str:
    """场景显示名：场景内 name，坏文件回退文件名 stem。"""
    try:
        return load_scene(path).name or path.stem
    except (OSError, ValueError):
        return path.stem


def _template_error_text(exc: TemplateError) -> str:
    """TemplateError → 界面文案（有行号/字段名就带上，照《使用说明》指出该改哪一处）。"""
    where = []
    if exc.line:
        where.append(f"第 {exc.line} 行附近")
    if exc.field:
        where.append(f"字段【{exc.field}】")
    return (" ".join(where) + "：" if where else "") + exc.message


def import_result_text(result: ImportResult) -> str:
    """导入结果 → 提示框正文（已导入的对象 + 未填字段 + 解析警告）。"""
    t = translator()
    kind = t.t("kind.scene") if result.kind == "scene" else t.t("kind.character")
    text = t.t("dlg.import_saved", kind=kind, name=result.name,
               path=result.path)
    if result.empty_fields:
        text += t.t("dlg.import_empty", fields="、".join(result.empty_fields))
    if result.warnings:
        text += t.t("dlg.import_warn", warnings="\n".join(result.warnings))
    return text


def _scan_cards(directory: Path | None) -> list[tuple[Path, str, str]]:
    """目录 → [(卡路径, 卡名, 出处/作品)]（排序沿用 loaders）。

    出场作品的筛选（§3.4）要按 corpus.source 归类，故这里一次把出处也带出来；坏卡
    仍不炸——名字回退文件名 stem、出处留空（编辑器的列举绝不因坏素材崩掉）。
    """
    if directory is None:
        return []
    out: list[tuple[Path, str, str]] = []
    for path in list_character_paths(directory):
        try:
            card = load_character_card(path)
        except (OSError, ValueError):
            out.append((path, path.stem, ""))
            continue
        out.append((path, card.name or path.stem, (card.corpus.source or "").strip()))
    return out


def confirm(parent, title: str, text: str) -> bool:
    """删除确认框（默认「否」）。独立成函数便于离屏测试打桩，不弹模态。"""
    btn = QMessageBox.question(
        parent, title, text,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No)
    return btn == QMessageBox.StandardButton.Yes


def warn(parent, title: str, text: str) -> None:
    """提示框（拒绝保存/复制、拒绝切场等）。独立成函数便于离屏测试打桩。"""
    QMessageBox.warning(parent, title, text)


def info(parent, title: str, text: str) -> None:
    """告知框（导入结果等，无「拒绝」语义）。独立成函数便于离屏测试打桩。"""
    QMessageBox.information(parent, title, text)


def _text_edit(value: str = "", placeholder: str = "", rows: int = 3,
               parent: QWidget | None = None) -> QPlainTextEdit:
    ed = QPlainTextEdit(parent)
    ed.setPlainText(value or "")
    if placeholder:
        ed.setPlaceholderText(placeholder)
    ed.setMinimumHeight(rows * 22)
    return ed


def _label(text: str, object_name: str = "hint") -> QLabel:
    lb = QLabel(text)
    lb.setObjectName(object_name)
    lb.setWordWrap(True)
    return lb


def _kv(text: str, muted: bool = False) -> QLabel:
    """只读的「键：值」行文本（详情弹窗用）：可换行、可选中复制；空值行弱化显灰。"""
    lb = QLabel(text)
    lb.setWordWrap(True)
    pal = palette()                    # 颜色随主题（深色下不能是默认配色的深字）
    lb.setStyleSheet(
        f"color:{pal.muted_text if muted else pal.chip_text};"
        f"font-size:13px;line-height:1.5;")
    lb.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return lb


def _num_row(layout: QVBoxLayout, label: str, value: str) -> None:
    """一行「名：值」——值等宽、右对齐（§3.6：数值读起来像仪器，不与正文混排）。"""
    row = QHBoxLayout()
    row.setSpacing(8)
    row.addWidget(_kv(label), 1)
    num = _mono(value, color=palette().text)
    num.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    row.addWidget(num)
    layout.addLayout(row)


def _num_text(value) -> str:
    """数值 → 两位小数文本（非数值原样字符串化，显示层不因脏数据抛）。"""
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def _ghost(text: str) -> QPushButton:
    b = QPushButton(text)
    b.setObjectName("ghost")
    return b


def _primary(text: str) -> QPushButton:
    b = QPushButton(text)
    b.setObjectName("primary")
    return b


def _danger(text: str) -> QPushButton:
    """破坏性动作按钮（删除…）：描边与字色用 danger，悬停铺一层浅红（§3.6）。"""
    b = QPushButton(text)
    b.setObjectName("danger")
    return b


def _hairline() -> QFrame:
    """1px 分隔线（§3.6：细线 + 分隔组织信息，不用盒子/阴影堆叠）。"""
    line = QFrame()
    line.setObjectName("hairline")
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFixedHeight(1)
    return line


def _mono(text: str, *, size: int = 12, color: str | None = None) -> QLabel:
    """等宽数值标签（权重/时刻/计数一律等宽排版，读起来「像仪器」，§3.6）。"""
    lb = QLabel(text)
    lb.setObjectName("mono")
    pal = palette()
    lb.setStyleSheet(
        f"color:{color or pal.muted_text};font-size:{size}px;"
        f"font-family:{pal.mono_font};")
    return lb


def _scroll_form(inner: QWidget) -> QScrollArea:
    """把长表单塞进竖向滚动区（角色卡字段多，小窗口下不至于溢出屏幕）。"""
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setFrameShape(QScrollArea.Shape.NoFrame)
    area.setWidget(inner)
    return area


# ====================================================== 角色卡编辑器
class CharacterEditorDialog(QDialog):
    """编辑一张 CharacterCard：姓名/出处/语料（风格·思维·口头禅·样例）/性格/能力/
    关系/已知边界/权重 w1..w7/情绪衰减。保存 = 校验姓名非空 → save_character_card(
    characters_dir/姓名.json) → accept()。

    测试/程序化填充用部件名：name_edit / source_edit / style_edit / thinking_edit /
    quirks_edit / samples_edit / personality_edit / abilities_edit /
    relationships_edit / boundary_edit / weight_sliders[field] / decay_slider /
    error_label。
    """

    def __init__(self, card: CharacterCard | None = None,
                 characters_dir: Path | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        t = translator()
        card = card or CharacterCard(name="")
        self._characters_dir = Path(characters_dir) if characters_dir else None
        self.saved_path: Path | None = None
        self.setStyleSheet(_dialog_stylesheet())
        self.setWindowTitle(t.t("dlg.character_title", name=card.name) if card.name
                            else t.t("dlg.character_new"))
        self.setMinimumSize(560, 620)

        inner = QWidget()
        form = QVBoxLayout(inner)
        form.setContentsMargins(14, 12, 14, 12)
        form.setSpacing(10)

        # ---- 基本 ----
        base = QGroupBox(t.t("grp.basic"))
        bv = QVBoxLayout(base)
        bv.setSpacing(6)
        bv.addWidget(_label(t.t("field.name"), "sect"))
        self.name_edit = QLineEdit(card.name)
        self.name_edit.setPlaceholderText(t.t("label.please_name"))
        bv.addWidget(self.name_edit)
        bv.addWidget(_label(t.t("field.origin"), "sect"))
        self.source_edit = QLineEdit(card.corpus.source)
        self.source_edit.setPlaceholderText(t.t("label.please_source"))
        bv.addWidget(self.source_edit)
        form.addWidget(base)

        # ---- 语料 ----
        corp = QGroupBox(t.t("field.corpus"))
        cv = QVBoxLayout(corp)
        cv.setSpacing(6)
        cv.addWidget(_label(t.t("field.style"), "sect"))
        self.style_edit = _text_edit(card.corpus.style, t.t("label.please_style"), 3)
        cv.addWidget(self.style_edit)
        cv.addWidget(_label(t.t("field.thinking"), "sect"))
        self.thinking_edit = _text_edit(card.corpus.thinking,
                                        t.t("label.please_thinking"), 3)
        cv.addWidget(self.thinking_edit)
        cv.addWidget(_label(t.t("field.catchphrase"), "sect"))
        self.quirks_edit = _text_edit(_join_lines(card.corpus.quirks),
                                      t.t("label.please_quirks"), 2)
        cv.addWidget(self.quirks_edit)
        cv.addWidget(_label(t.t("field.samples"), "sect"))
        self.samples_edit = _text_edit(_join_lines(card.corpus.samples),
                                       t.t("label.please_samples"), 4)
        cv.addWidget(self.samples_edit)
        form.addWidget(corp)

        # ---- 设定 ----
        prof = QGroupBox(t.t("grp.profile"))
        pv = QVBoxLayout(prof)
        pv.setSpacing(6)
        pv.addWidget(_label(t.t("field.personality"), "sect"))
        self.personality_edit = _text_edit(_join_pairs(card.personality),
                                           t.t("label.please_personality"), 3)
        pv.addWidget(self.personality_edit)
        pv.addWidget(_label(t.t("field.abilities"), "sect"))
        self.abilities_edit = _text_edit(_join_lines(card.abilities),
                                         t.t("label.please_one_per_line"), 2)
        pv.addWidget(self.abilities_edit)
        pv.addWidget(_label(t.t("field.relations"), "sect"))
        self.relationships_edit = _text_edit(_join_pairs(card.relationships),
                                             t.t("label.please_relationship"), 2)
        pv.addWidget(self.relationships_edit)
        pv.addWidget(_label(t.t("field.boundaries"), "sect"))
        self.boundary_edit = _text_edit(_join_lines(card.knowledge_boundary),
                                        t.t("label.please_one_per_line"), 2)
        pv.addWidget(self.boundary_edit)
        form.addWidget(prof)

        # ---- 权重 ----
        wbox = QGroupBox(t.t("field.weights"))
        wv = QVBoxLayout(wbox)
        wv.setSpacing(6)
        wdata = card.weights.model_dump()
        self.weight_sliders: dict[str, QSlider] = {}
        self.weight_labels: dict[str, QLabel] = {}
        for field, label in WEIGHT_FIELDS:
            slider, num = self._weight_row(wv, label, float(wdata.get(field, 0.5)))
            self.weight_sliders[field] = slider
            self.weight_labels[field] = num
        self.decay_slider, self.decay_label = self._weight_row(
            wv, t.t("field.decay"), float(card.emotion_decay_rate))
        form.addWidget(wbox)
        form.addStretch(1)

        self.error_label = QLabel("")
        self.error_label.setObjectName("error")
        self.error_label.setWordWrap(True)
        self.error_label.hide()

        self.cancel_btn = _ghost(t.t("btn.cancel"))
        self.cancel_btn.clicked.connect(self.reject)
        self.save_btn = _primary(t.t("btn.save"))
        self.save_btn.clicked.connect(self.save)

        foot = QWidget()
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(14, 8, 14, 12)
        fl.addWidget(self.error_label, 1)
        fl.addWidget(self.cancel_btn)
        fl.addWidget(self.save_btn)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(_scroll_form(inner), 1)
        root.addWidget(foot)
        self.name_edit.setFocus()

    # ---------------------------------------------------------------- 部件
    @staticmethod
    def _weight_row(v: QVBoxLayout, label: str, value: float) -> tuple[QSlider, QLabel]:
        """一根 0~1 滑杆 + 实时数值标签（滑杆整数域 0~100，显示 /100 两位小数）。"""
        row = QHBoxLayout()
        row.setSpacing(8)
        lb = _label(label, "sect")
        lb.setFixedWidth(70)
        lb.setWordWrap(False)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, 100)
        slider.setValue(max(0, min(100, round(value * 100))))
        # 数值右对齐 + 等宽（§3.6）：滑杆旁的读数与滑杆在同一视觉基线上。
        pal = palette()
        num = _mono(f"{slider.value() / 100:.2f}", color=pal.chip_text)
        num.setFixedWidth(40)
        num.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        slider.valueChanged.connect(lambda v, n=num: n.setText(f"{v / 100:.2f}"))
        row.addWidget(lb)
        row.addWidget(slider, 1)
        row.addWidget(num)
        v.addLayout(row)
        return slider, num

    def _error(self, msg: str) -> None:
        self.error_label.setText(msg)   # 无前缀符号：错误由 #error 的红字承担（§3.6 不用 emoji）
        self.error_label.show()

    # ---------------------------------------------------------------- 组装
    def build_card(self) -> CharacterCard:
        """按部件当前值组装 CharacterCard（滑杆 0~100 → 权重 0~1）。"""
        return CharacterCard(
            name=self.name_edit.text().strip(),
            personality=_pairs(self.personality_edit.toPlainText()),
            abilities=_lines(self.abilities_edit.toPlainText()),
            relationships=_pairs(self.relationships_edit.toPlainText()),
            knowledge_boundary=_lines(self.boundary_edit.toPlainText()),
            weights=Weights(**{f: self.weight_sliders[f].value() / 100.0
                               for f, _ in WEIGHT_FIELDS}),
            emotion_decay_rate=self.decay_slider.value() / 100.0,
            corpus=Corpus(
                source=self.source_edit.text().strip(),
                style=self.style_edit.toPlainText().strip(),
                thinking=self.thinking_edit.toPlainText().strip(),
                quirks=_lines(self.quirks_edit.toPlainText()),
                samples=_lines(self.samples_edit.toPlainText())))

    # ---------------------------------------------------------------- 保存
    def save(self) -> None:
        """校验 → 落盘 characters_dir/姓名.json → accept()；不合法就地报错不关窗。

        姓名既是卡名也是文件名，故先过 filename_error（拒绝路径分隔符/`..`/前导点等），
        绝不让名字把文件写到库目录之外或建出子目录。
        """
        name = self.name_edit.text().strip()
        if not name:
            self._error(_t("err.card_name_required"))
            return
        if self._characters_dir is None:
            self._error(_t("err.no_character_dir"))
            return
        try:
            path = material_path(self._characters_dir, name)
        except ValueError as exc:
            self._error(str(exc))
            return
        card = self.build_card()
        self.saved_path = save_character_card(card, path)
        self.accept()


# ====================================================== 场景编辑器
class SceneEditorDialog(QDialog):
    """编辑一个 Scene（新模型，§3）：基本信息（名/文件名/日期/开始时间/硬边界）+
    背景与设定（背景/描述 + 可改变的/剧情设定）+ 出场角色（候选列表）+ hooks 列表。

    场景名与文件名**解耦**（§3.4）：文件名在新建时按场景名的安全 slug 生成一次并随用户
    敲名字实时跟随（一旦用户自己改过文件名、或编辑的是既有场景，就再也不自动改），存盘
    走 loaders.scene_path_for(scene_dir, filename)。出场角色走**候选列表**（勾选 = 在场）：
    按角色卡的 corpus.source 筛「出场作品」，最多 6 行、超出即定高滚动。保存时留着的
    仍按原在场顺序排前，新勾的按列表序追加；库里无卡的名字照样列出并保留（旧场景可能
    引用别处的卡），但每个勾上的角色都**必须**有卡，缺卡原地报错（引擎缺卡开不了场）。
    hooks 逐条过 hooks.validate，任何一条不合法都拦住保存并就地显示原因。

    程序化填充用：name_edit / filename_edit / date_check / date_year / date_month /
    date_day / start_time_edit / boundary_type / boundary_value / boundary_desc /
    background_edit / description_edit / description_mutable / plot_edit /
    filter_combo / participant_list / participant_items / hooks / hook_list /
    reset_btn / reset_done / error_label。
    """

    def __init__(self, scene: Scene | None = None,
                 scenes_dir: Path | None = None,
                 characters_dir: Path | None = None,
                 parent: QWidget | None = None,
                 scene_path: Path | None = None):
        super().__init__(parent)
        t = translator()
        new_scene = scene is None
        scene = scene or Scene(name="")
        self._scene = scene
        self._scenes_dir = Path(scenes_dir) if scenes_dir else None
        self.saved_path: Path | None = None
        self.reset_done = False
        self.setStyleSheet(_dialog_stylesheet())
        self.setWindowTitle(t.t("dlg.scene_title", name=scene.name) if scene.name
                            else t.t("dlg.scene_new"))
        self.setMinimumSize(620, 680)

        # 角色卡库存：卡名 → 路径 / 卡名 → 出处（保存时校验「每个勾上的角色都有卡」）。
        self._cards_by_name: dict[str, Path] = {}
        self._source_by_name: dict[str, str] = {}
        for path, card_name, source in _scan_cards(characters_dir):
            self._cards_by_name[card_name] = path
            self._source_by_name[card_name] = source
        #: 原在场名单（其顺序 = 保存时「留着的角色」的顺序基准）。
        self._cast_order = list(scene.participants)

        inner = QWidget()
        form = QVBoxLayout(inner)
        form.setContentsMargins(14, 12, 14, 12)
        form.setSpacing(10)

        # ---- 基本信息 ----
        base = QGroupBox(t.t("grp.scene_basic"))
        bv = QVBoxLayout(base)
        bv.setSpacing(6)
        bv.addWidget(_label(t.t("field.scene_name"), "sect"))
        self.name_edit = QLineEdit(scene.name)
        self.name_edit.setPlaceholderText(t.t("label.please_scene_name"))
        bv.addWidget(self.name_edit)
        bv.addWidget(_label(t.t("field.scene_filename"), "sect"))
        # 新建（无既有文件）时文件名跟随场景名，用户一改文件名就停（下面的 textChanged）。
        self._filename_auto = new_scene and not scene.name
        self.filename_edit = QLineEdit(
            Path(scene_path).name if scene_path is not None
            else default_scene_filename(scene.name, self._scenes_dir))
        self.filename_edit.setPlaceholderText(t.t("label.please_filename"))
        bv.addWidget(self.filename_edit)

        # 日期：不勾 = 不设日期（存空串）；勾了才用年/月/日三个数字框。
        date_row = QHBoxLayout()
        date_row.setSpacing(6)
        self.date_check = QCheckBox(t.t("field.scene_date"))
        self.date_year = QSpinBox()
        self.date_year.setRange(1, 9999)
        self.date_year.setSuffix(t.t("spin.year_suffix"))
        self.date_month = QSpinBox()
        self.date_month.setRange(1, 12)
        self.date_month.setSuffix(t.t("spin.month_suffix"))
        self.date_day = QSpinBox()
        self.date_day.setRange(1, 31)
        self.date_day.setSuffix(t.t("spin.day_suffix"))
        self._date_raw = (scene.date or "").strip()
        match = _DATE_RE.match(self._date_raw)
        if match:
            year, month, day = (int(match.group(1)), int(match.group(2)),
                                int(match.group(3)))
        else:                                   # 认不出的写法/没写：给今天当起点
            today = QDate.currentDate()
            year, month, day = today.year(), today.month(), today.day()
        #: 三个数字框的初值——原样不动就保留 _date_raw 的写法（含旧文件的自由文本）。
        self._date_initial = (year, month, day)
        self.date_year.setValue(year)
        self.date_month.setValue(month)
        self.date_day.setValue(day)
        self.date_check.setChecked(bool(self._date_raw))
        date_row.addWidget(self.date_check)
        date_row.addWidget(self.date_year)
        date_row.addWidget(self.date_month)
        date_row.addWidget(self.date_day)
        date_row.addStretch(1)
        self.date_check.toggled.connect(self.date_year.setEnabled)
        self.date_check.toggled.connect(self.date_month.setEnabled)
        self.date_check.toggled.connect(self.date_day.setEnabled)
        for spin in (self.date_year, self.date_month, self.date_day):
            spin.setEnabled(self.date_check.isChecked())
        bv.addLayout(date_row)

        bv.addWidget(_label(t.t("field.scene_start"), "sect"))
        self.start_time_edit = QLineEdit(scene.start_time or "21:30")
        self.start_time_edit.setPlaceholderText("21:30")
        bv.addWidget(self.start_time_edit)
        # 场景名 → 缺省文件名（只在「文件名还没被用户碰过」时跟随）。
        self.name_edit.textChanged.connect(self._on_name_changed)
        self.filename_edit.textChanged.connect(self._on_filename_changed)
        form.addWidget(base)

        # ---- 背景与设定 ----
        prof = QGroupBox(t.t("grp.background"))
        pv = QVBoxLayout(prof)
        pv.setSpacing(6)
        pv.addWidget(_label(t.t("field.background"), "sect"))
        self.background_edit = _text_edit(scene.background,
                                          t.t("label.please_background"), 4)
        pv.addWidget(self.background_edit)
        pv.addWidget(_label(t.t("field.scene_description"), "sect"))
        self.description_edit = _text_edit(scene.description,
                                           t.t("label.please_description"), 3)
        pv.addWidget(self.description_edit)
        self.description_mutable = QCheckBox(t.t("toggle.description_mutable"))
        self.description_mutable.setChecked(bool(scene.description_mutable))
        pv.addWidget(self.description_mutable)
        pv.addWidget(_label(t.t("field.plot"), "sect"))
        self.plot_edit = _text_edit(scene.plot_direction, t.t("label.please_plot"), 3)
        pv.addWidget(self.plot_edit)
        form.addWidget(prof)

        # ---- 出场角色（候选列表：勾选 = 在场）----
        part = QGroupBox(t.t("grp.cast"))
        cv = QVBoxLayout(part)
        cv.setSpacing(4)
        cv.addWidget(_label(t.t("field.source_filter"), "sect"))
        self.filter_combo = QComboBox()
        self.filter_combo.addItem(t.t("combo.all_sources"), "")
        for source in sorted({s for s in self._source_by_name.values() if s}):
            self.filter_combo.addItem(source, source)
        self.filter_combo.currentIndexChanged.connect(lambda _i: self._apply_filter())
        cv.addWidget(self.filter_combo)
        self.participant_list = QListWidget()
        self.participant_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.participant_list.setToolTip(t.t("hint.cast_list"))
        self.participant_items: dict[str, QListWidgetItem] = {}
        # 候选 = 库里的卡（按扫描序）+ 场景原有但库里没卡的名字（照样列出，不悄悄丢人）。
        names = list(self._cards_by_name)
        for extra in self._cast_order:
            if extra not in names:
                names.append(extra)
        for card_name in names:
            item = QListWidgetItem(card_name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if card_name in self._cast_order
                else Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, card_name)
            path = self._cards_by_name.get(card_name)
            item.setToolTip(str(path) if path else t.t("tip.no_card_for_name"))
            self.participant_list.addItem(item)
            self.participant_items[card_name] = item
        if not names:
            self.participant_list.addItem(QListWidgetItem(t.t("dlg.no_cards_in_dir")))
        cv.addWidget(self.participant_list)
        self._apply_filter()
        form.addWidget(part)

        # ---- 硬边界 ----
        bnd = QGroupBox(t.t("grp.boundary"))
        dv = QVBoxLayout(bnd)
        dv.setSpacing(6)
        dv.addWidget(_label(t.t("field.boundary_type"), "sect"))
        self.boundary_type = QComboBox()
        for label, value in BOUNDARY_TYPES:
            self.boundary_type.addItem(label, value)
        hb = scene.hard_boundary
        if hb is not None:
            idx = next((i for i, (_l, v) in enumerate(BOUNDARY_TYPES)
                        if v == hb.type), 0)
            self.boundary_type.setCurrentIndex(idx)
        dv.addWidget(self.boundary_type)
        dv.addWidget(_label(t.t("field.boundary_value"), "sect"))
        self.boundary_value = QLineEdit(hb.value if hb else "")
        self.boundary_value.setPlaceholderText("22:00")
        dv.addWidget(self.boundary_value)
        dv.addWidget(_label(t.t("field.boundary_desc"), "sect"))
        self.boundary_desc = QLineEdit(hb.desc if hb else "")
        self.boundary_desc.setPlaceholderText(t.t("label.please_boundary_desc"))
        dv.addWidget(self.boundary_desc)
        self.boundary_type.currentIndexChanged.connect(
            lambda _i: self._on_boundary_type_changed())
        self._on_boundary_type_changed()
        form.addWidget(bnd)

        # ---- Hooks ----
        hbox = QGroupBox(t.t("grp.hooks"))
        hv = QVBoxLayout(hbox)
        hv.setSpacing(4)
        self.hook_list = QListWidget()
        self.hook_list.setFixedHeight(4 * _CAST_LIST_ROW_PX + _CAST_LIST_PAD_PX)
        self.hook_list.itemDoubleClicked.connect(lambda _i: self.edit_hook())
        hv.addWidget(self.hook_list)
        hrow = QHBoxLayout()
        hrow.setSpacing(6)
        self.new_hook_btn = _ghost(t.t("btn.new"))
        self.new_hook_btn.clicked.connect(self.new_hook)
        self.edit_hook_btn = _ghost(t.t("btn.edit"))
        self.edit_hook_btn.clicked.connect(self.edit_hook)
        self.delete_hook_btn = _danger(t.t("btn.delete"))
        self.delete_hook_btn.clicked.connect(self.delete_hook)
        for btn in (self.new_hook_btn, self.edit_hook_btn, self.delete_hook_btn):
            hrow.addWidget(btn)
        hrow.addStretch(1)
        hv.addLayout(hrow)
        form.addWidget(hbox)
        form.addStretch(1)

        self.hooks: list[Hook] = list(scene.hooks)
        self._refresh_hook_list()

        self.error_label = QLabel("")
        self.error_label.setObjectName("error")
        self.error_label.setWordWrap(True)
        self.error_label.hide()

        self.reset_btn = _ghost(t.t("btn.reset_scene"))
        self.reset_btn.clicked.connect(self.reset_scene)
        self.cancel_btn = _ghost(t.t("btn.cancel"))
        self.cancel_btn.clicked.connect(self.reject)
        self.save_btn = _primary(t.t("btn.save"))
        self.save_btn.clicked.connect(self.save)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(_scroll_form(inner), 1)
        foot = QWidget()
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(14, 8, 14, 12)
        fl.addWidget(self.reset_btn)
        fl.addWidget(self.error_label, 1)
        fl.addWidget(self.cancel_btn)
        fl.addWidget(self.save_btn)
        root.addWidget(foot)

    # ------------------------------------------------------- 文件名/日期/筛选
    def _on_name_changed(self, text: str) -> None:
        """场景名变化 → 只在「文件名还没被用户碰过」时跟随改缺省文件名（§3.4）。"""
        if self._filename_auto:
            self.filename_edit.setText(default_scene_filename(text, self._scenes_dir))

    def _on_filename_changed(self, text: str) -> None:
        """用户（或程序）把文件名改成了别的值 → 停止自动跟随，此后手动为准。"""
        if self._filename_auto and text != default_scene_filename(
                self.name_edit.text(), self._scenes_dir):
            self._filename_auto = False

    def _on_boundary_type_changed(self) -> None:
        """类型为「无」时，边界值与描述一并停用（存下去也只有 type=none）。"""
        none = self.boundary_type.currentData() == "none"
        self.boundary_value.setEnabled(not none)
        self.boundary_desc.setEnabled(not none)

    def _date_value(self) -> str:
        """日期部件 → 落盘字符串（未勾 = ""；勾了 = 年-月-日，原样保留旧文件写法）。"""
        if not self.date_check.isChecked():
            return ""
        picked = (self.date_year.value(), self.date_month.value(), self.date_day.value())
        if self._date_raw and picked == self._date_initial:
            return self._date_raw          # 没动过：保留用户/旧文件原来的写法
        return f"{picked[0]:04d}-{picked[1]:02d}-{picked[2]:02d}"

    # ---------------------------------------------------------------- 出场角色
    def _apply_filter(self) -> None:
        """按「出场作品」筛选候选列表：不匹配的行隐藏（勾选状态保留，切回即见）。"""
        want = self.filter_combo.currentData() or ""
        for card_name, item in self.participant_items.items():
            item.setHidden(bool(want) and self._source_by_name.get(card_name, "") != want)
        self._fit_participant_list()

    def _fit_participant_list(self) -> None:
        """候选列表定高：最多 6 行，超出即固定高度 + 滚动条（§3.4，不随角色数无限拉长）。"""
        rows = sum(1 for i in self.participant_items.values() if not i.isHidden())
        rows = max(1, min(rows, _CAST_LIST_ROWS))
        self.participant_list.setFixedHeight(rows * _CAST_LIST_ROW_PX + _CAST_LIST_PAD_PX)

    def checked_characters(self) -> list[str]:
        """当前勾选的角色名，顺序 = 保存进 characters 的顺序。

        留着的按**原在场顺序**排前（编辑旧场景不因列表排布被重排），新勾的按列表序追加。
        """
        checked = [n for n, item in self.participant_items.items()
                   if item.checkState() == Qt.CheckState.Checked]
        ordered = [n for n in self._cast_order if n in checked]
        ordered += [n for n in checked if n not in ordered]
        return ordered

    # ---------------------------------------------------------------- hooks
    def _refresh_hook_list(self) -> None:
        """重绘 hook 列表：每行 = hooks.describe(hook)（条件 + 效果 + 显隐 + 停用标记）。"""
        self.hook_list.clear()
        for hook in self.hooks:
            self.hook_list.addItem(QListWidgetItem(hooks_mod.describe(hook)))

    def _next_hook_id(self) -> str:
        used = {h.id for h in self.hooks}
        i = len(self.hooks) + 1
        while f"h{i}" in used:
            i += 1
        return f"h{i}"

    def add_hook(self, hook: Hook) -> Hook:
        """把一条 hook 加进列表（id 空的补一个），并重绘；返回实际加入的那条。"""
        if not hook.id:
            hook.id = self._next_hook_id()
        self.hooks.append(hook)
        self._refresh_hook_list()
        return hook

    def new_hook(self) -> Hook | None:
        """「新建」：开子对话框编一条新 hook，接受才列表；取消则什么都不加。"""
        dlg = HookEditorDialog(None, cast=self.checked_characters(), parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        return self.add_hook(dlg.build_hook())

    def edit_hook(self) -> None:
        """「编辑」：改当前选中的那条 hook（id 不变）。"""
        row = self.hook_list.currentRow()
        if row < 0 or row >= len(self.hooks):
            return
        dlg = HookEditorDialog(self.hooks[row], cast=self.checked_characters(), parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        hook = dlg.build_hook()
        hook.id = self.hooks[row].id
        self.hooks[row] = hook
        self._refresh_hook_list()

    def delete_hook(self) -> None:
        """「删除」：删掉当前选中的那条 hook（未落盘前只是内存里的改动）。"""
        row = self.hook_list.currentRow()
        if row < 0 or row >= len(self.hooks):
            return
        del self.hooks[row]
        self._refresh_hook_list()
        self.hook_list.setCurrentRow(min(row, len(self.hooks) - 1))

    # ---------------------------------------------------------------- 组装
    def build_scene(self) -> Scene:
        """按部件当前值组装 Scene（硬边界恒有：类型「无」即 type="none"）。

        保留原本就在场的角色的入场记录（entered_at/entered_round 不因编辑被清零），
        新勾上的按新成员（entered_round=0）加入。
        """
        existing = {m.name: m for m in self._scene.characters}
        characters = [existing[n].model_copy() if n in existing else SceneCastMember(name=n)
                      for n in self.checked_characters()]
        return Scene(
            name=self.name_edit.text().strip(),
            date=self._date_value(),
            background=self.background_edit.toPlainText().strip(),
            description=self.description_edit.toPlainText().strip(),
            description_mutable=self.description_mutable.isChecked(),
            plot_direction=self.plot_edit.toPlainText().strip(),
            hooks=list(self.hooks),
            characters=characters,
            hard_boundary=HardBoundary(type=self.boundary_type.currentData(),
                                       value=self.boundary_value.text().strip(),
                                       desc=self.boundary_desc.text().strip()),
            start_time=self.start_time_edit.text().strip() or "21:30")

    def _error(self, msg: str) -> None:
        self.error_label.setText(msg)   # 无前缀符号：错误由 #error 的红字承担（§3.6 不用 emoji）
        self.error_label.show()

    def _target_path(self) -> Path | None:
        """当前文件名 → 落盘路径；没有场景目录或文件名不合法则 None（原因见 validate）。"""
        if self._scenes_dir is None:
            return None
        try:
            return scene_path_for(self._scenes_dir, self.filename_edit.text().strip())
        except ValueError:
            return None

    # ---------------------------------------------------------------- 校验/保存
    def validate(self) -> str:
        """返回错误文案；空串 = 通过。

        按引擎开场口径把关，全部在保存前拦下（免得存出一套切换时才报「开场失败」的素材）：
        场景名非空、文件名能安全落盘（§3.4 文件名自定义，故这里是**文件名**的规矩）且以
        .json 结尾、开始时间是 HH:MM、时间边界的值也是 HH:MM、每个勾上的角色都有角色卡、
        每条 hook 过 hooks.validate。场景可以一个角色都没有（§3.3），故不要求至少一人。
        """
        if not self.name_edit.text().strip():
            return _t("err.scene_name_required")
        err = scene_filename_error(self.filename_edit.text().strip())
        if err:
            return err
        if not _HHMM_RE.match(self.start_time_edit.text().strip()):
            return _t("err.bad_start_hhmm",
                      value=repr(self.start_time_edit.text().strip()))
        if (self.boundary_type.currentData() == "time"
                and (self.boundary_value.text().strip()
                     or self.boundary_desc.text().strip())):
            value = self.boundary_value.text().strip()
            if not _HHMM_RE.match(value):
                return _t("err.bad_boundary_hhmm", value=repr(value))
        missing = [n for n in self.checked_characters() if n not in self._cards_by_name]
        if missing:
            return _t("err.missing_cards", names="、".join(missing))
        for i, hook in enumerate(self.hooks, start=1):
            errors = hooks_mod.validate(hook)
            if errors:
                return _t("err.hook_invalid", n=i, errors="；".join(errors))
        return ""

    def save(self) -> None:
        """校验 → 落盘 scenes_dir/文件名.json → accept()；不合法就地报错不关窗。"""
        err = self.validate()
        if err:
            self._error(err)
            return
        path = self._target_path()
        if path is None:
            self._error(_t("err.no_scene_dir_save"))
            return
        scene = self.build_scene()
        self._scene = scene
        self.saved_path = save_scene(scene, path)
        self.accept()

    def reset_scene(self) -> bool:
        """「重置场景」按钮：确认 → 清零运行期 → 写回当前文件名指向的文件（§5）。

        清的是各角色的入场记录（reset_scene_runtime）；对话历史的清空由引擎在后续阶段
        执行（本阶段 transcript 不挂在 Scene 上），按钮只负责把「配置与在场角色不变、
        运行期归零」的场景落盘。返回是否真的写盘。
        """
        if not confirm(self, _t("btn.reset_scene"), _t("dlg.confirm_reset_scene")):
            return False
        err = self.validate()
        if err:
            self._error(err)
            return False
        path = self._target_path()
        if path is None:
            self._error(_t("err.no_scene_dir_reset"))
            return False
        scene = reset_scene_runtime(self.build_scene())
        self._scene = scene
        self.saved_path = save_scene(scene, path)
        self.reset_done = True
        return True


# ====================================================== Hook 编辑器
class HookEditorDialog(QDialog):
    """编辑一条 Hook（§4）：条件 + 事件类型 + 该类型的字段 + 显式/隐式 + 启用 + 备注。

    事件分三类，各自只露出自己的字段（上下文文本 / 角色·动作·轮数 / 场景字段改动），
    切换类型不丢已填内容（各页面各自持有部件）。保存前过 hooks.validate，不合法就地
    报错、不关窗——「条件为空」「角色事件没填角色名」这类问题在编辑器里就拦住。

    程序化填充用：condition_edit / kind_combo / context_edit / character_combo /
    action_combo / turns_spin / patch_edit / visible_check / enabled_check /
    note_edit / error_label。
    """

    def __init__(self, hook: Hook | None = None, cast: list[str] | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        t = translator()
        hook = hook or Hook(id="", condition="", event_kind="context")
        self._hook_id = hook.id
        self.setStyleSheet(_dialog_stylesheet())
        self.setWindowTitle(t.t("dlg.hook_title"))
        self.setMinimumSize(520, 520)

        inner = QWidget()
        form = QVBoxLayout(inner)
        form.setContentsMargins(14, 12, 14, 12)
        form.setSpacing(8)

        form.addWidget(_label(t.t("field.condition"), "sect"))
        self.condition_edit = _text_edit(hook.condition, t.t("label.please_condition"), 3)
        form.addWidget(self.condition_edit)

        form.addWidget(_label(t.t("field.event_kind"), "sect"))
        self.kind_combo = QComboBox()
        for label, value in HOOK_KINDS:
            self.kind_combo.addItem(label, value)
        idx = next((i for i, (_l, v) in enumerate(HOOK_KINDS) if v == hook.event_kind), 0)
        self.kind_combo.setCurrentIndex(idx)
        form.addWidget(self.kind_combo)

        # ---- 分类字段（三页各自持有部件，切类型不丢内容）----
        self.kind_stack = QStackedWidget()
        self.kind_stack.addWidget(self._context_page(hook.context_text))
        self.kind_stack.addWidget(self._character_page(hook, cast or []))
        self.kind_stack.addWidget(self._scene_page(hook.scene_patch))
        self.kind_stack.setCurrentIndex(idx)
        self.kind_combo.currentIndexChanged.connect(self.kind_stack.setCurrentIndex)
        form.addWidget(self.kind_stack)

        # ---- 显隐 / 启用 / 备注 ----
        self.visible_check = QCheckBox(t.t("toggle.visible_hook"))
        self.visible_check.setChecked(bool(hook.visible))
        form.addWidget(self.visible_check)
        self.enabled_check = QCheckBox(t.t("toggle.enabled_hook"))
        self.enabled_check.setChecked(bool(hook.enabled))
        form.addWidget(self.enabled_check)
        form.addWidget(_label(t.t("field.note"), "sect"))
        self.note_edit = QLineEdit(hook.note)
        self.note_edit.setPlaceholderText(t.t("label.please_note"))
        form.addWidget(self.note_edit)
        form.addStretch(1)

        self.error_label = QLabel("")
        self.error_label.setObjectName("error")
        self.error_label.setWordWrap(True)
        self.error_label.hide()

        self.cancel_btn = _ghost(t.t("btn.cancel"))
        self.cancel_btn.clicked.connect(self.reject)
        self.save_btn = _primary(t.t("btn.ok"))
        self.save_btn.clicked.connect(self.save)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(_scroll_form(inner), 1)
        foot = QWidget()
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(14, 8, 14, 12)
        fl.addWidget(self.error_label, 1)
        fl.addWidget(self.cancel_btn)
        fl.addWidget(self.save_btn)
        root.addWidget(foot)

    # ---------------------------------------------------------------- 三页
    def _context_page(self, text: str) -> QWidget:
        """上下文事件：要写进上下文的文本。"""
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(_label(_t("field.context_text"), "sect"))
        self.context_edit = _text_edit(text, _t("label.please_context"), 4)
        lay.addWidget(self.context_edit)
        return page

    def _character_page(self, hook: Hook, cast: list[str]) -> QWidget:
        """角色事件：角色 + 动作（+ 静默轮数）。"""
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(_label(_t("field.hook_character"), "sect"))
        self.character_combo = QComboBox()
        self.character_combo.setEditable(True)          # 不在场/还没建卡的名字也能写
        self.character_combo.addItems(list(cast))
        self.character_combo.setCurrentText(hook.character_name or "")
        lay.addWidget(self.character_combo)
        lay.addWidget(_label(_t("field.hook_action"), "sect"))
        self.action_combo = QComboBox()
        for label, value in HOOK_ACTIONS:
            self.action_combo.addItem(label, value)
        act_idx = next((i for i, (_l, v) in enumerate(HOOK_ACTIONS)
                        if v == hook.action), 0)
        self.action_combo.setCurrentIndex(act_idx)
        lay.addWidget(self.action_combo)
        lay.addWidget(_label(_t("field.mute_turns"), "sect"))
        self.turns_spin = QSpinBox()
        self.turns_spin.setRange(0, 999)
        self.turns_spin.setValue(int(hook.turns or 0))
        lay.addWidget(self.turns_spin)
        self._on_action_changed()
        self.action_combo.currentIndexChanged.connect(
            lambda _i: self._on_action_changed())
        return page

    def _scene_page(self, patch: dict) -> QWidget:
        """场景事件：要改写的场景字段（一行「键：值」）。"""
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(_label(_t("field.scene_patch", fields=scene_patch_hint()), "sect"))
        # 回显用**真键**（用户/引擎读得懂）；用户敲中文展示名也认（见 scene_patch_pairs）。
        self.patch_edit = _text_edit(_join_pairs(patch or {}),
                                     _t("label.please_patch"), 4)
        lay.addWidget(self.patch_edit)
        return page

    def _on_action_changed(self) -> None:
        """只有「静默 N 轮」才轮到轮数框。"""
        self.turns_spin.setEnabled(self.action_combo.currentData() == "mute_turns")

    # ---------------------------------------------------------------- 组装/保存
    def build_hook(self) -> Hook:
        """按部件当前值组装 Hook（id 沿用被编辑那条的）。"""
        return Hook(
            id=self._hook_id,
            condition=self.condition_edit.toPlainText().strip(),
            event_kind=self.kind_combo.currentData(),
            visible=self.visible_check.isChecked(),
            context_text=self.context_edit.toPlainText().strip(),
            character_name=self.character_combo.currentText().strip(),
            action=self.action_combo.currentData(),
            turns=self.turns_spin.value(),
            scene_patch=scene_patch_pairs(self.patch_edit.toPlainText()),
            enabled=self.enabled_check.isChecked(),
            note=self.note_edit.text().strip())

    def _error(self, msg: str) -> None:
        self.error_label.setText(msg)   # 无前缀符号：错误由 #error 的红字承担（§3.6 不用 emoji）
        self.error_label.show()

    def save(self) -> None:
        """过 hooks.validate → accept()；不合法就地报错、不关窗。

        **新钩子没有 id**（`_hook_id` 为空）：id 由场景编辑器在加入列表时分配
        （见 SceneEditorDialog.add_hook → `_next_hook_id`），本对话框里没有 id 输入框，
        故校验时用一枚合法占位 id 顶替，只校验用户真正能改的内容字段；空 id 不该拦住
        「新建一条 hook」这条路。id 的合法性/唯一性在有全体 hooks 上下文的那一层判定。
        """
        hook = self.build_hook()
        probe = (dataclasses.replace(hook, id=_VALID_HOOK_ID_PROBE) if not hook.id
                 else hook)
        errors = hooks_mod.validate(probe)
        if errors:
            self._error("；".join(errors))
            return
        self.accept()


# ====================================================== 场景库（§3.3）
class LibraryDialog(QDialog):
    """**场景库**（§3.3）：只列场景——打开 / 新建 / 编辑 / 复制 / 删除 / 从模板导入。

    角色**不在这里选**：初始阵容只在场景编辑器（SceneEditorDialog 的候选列表）里定，
    之后增删走顶栏「角色」菜单。故本弹窗没有角色列、没有「至少选一名角色」的守门——
    **空阵容的场景照样能选中、能打开**（§3.3 场景可以没有人，只是没人在活动）。

    点「用这套开场」（即「打开」这套场景）把选中的场景写进 `chosen_scene` 并 accept()；
    取消则保持 None。删除只删文件（先弹确认框），库本身即目录。

    程序化填充用：scene_list / new_scene_btn / edit_scene_btn / copy_scene_btn /
    delete_scene_btn / import_btn / use_btn / last_import。
    """

    def __init__(self, characters_dir: Path | None, scenes_dir: Path,
                 parent: QWidget | None = None):
        super().__init__(parent)
        t = translator()
        #: 角色库目录：本弹窗**不列角色**，但「从模板导入…」要它决定角色卡落在哪，
        #: 场景编辑器要它列候选角色卡。旧调用方照传（可为 None：退回到场景目录的兄弟目录）。
        self._characters_dir = Path(characters_dir) if characters_dir else None
        self._scenes_dir = Path(scenes_dir)
        self.chosen_scene: Path | None = None
        #: 兼容旧调用方（main_window 的旧写法会读它）：场景库不再选角色，**恒为空表**。
        self.chosen_characters: list[Path] = []
        #: 最近一次「从模板导入…」的结果（含未填字段/警告；测试与状态栏可读）。
        self.last_import: ImportResult | None = None
        self.setStyleSheet(_dialog_stylesheet())
        self.setWindowTitle(t.t("dlg.library"))
        self.setMinimumSize(560, 460)

        self.scene_list = QListWidget()
        self.scene_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.scene_list.setToolTip(t.t("list.hint_scene"))
        self.scene_list.itemSelectionChanged.connect(self._refresh_use_btn)
        self.scene_list.itemDoubleClicked.connect(lambda _i: self.edit_scene())

        head = _label(t.t("list.scenes"), "sect")
        self._scene_row = self._buttons()
        self.import_btn = _ghost(t.t("btn.import_template"))
        self.import_btn.setToolTip(t.t("tip.import_template"))
        self.import_btn.clicked.connect(self.import_from_template)
        self.use_btn = _primary(t.t("btn.use_opening"))
        self.use_btn.setToolTip(t.t("tip.library"))
        self.use_btn.setEnabled(False)
        self.use_btn.clicked.connect(self.use_this)
        close_btn = _ghost(t.t("btn.close"))
        close_btn.clicked.connect(self.reject)

        foot = QHBoxLayout()
        foot.setSpacing(8)
        foot.addWidget(self.import_btn)
        foot.addWidget(_label(t.t("hint.library_foot")), 1)
        foot.addWidget(close_btn)
        foot.addWidget(self.use_btn)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(8)
        root.addWidget(head)
        root.addWidget(self.scene_list, 1)
        root.addLayout(self._scene_row)
        root.addWidget(_hairline())
        root.addLayout(foot)

        self.refresh()

    # ---------------------------------------------------------------- 按钮行
    def _buttons(self) -> QHBoxLayout:
        """场景操作行：新建 / 编辑 / 复制 / 删除（删除用 danger 描边，动作有破坏性）。"""
        t = translator()
        row = QHBoxLayout()
        row.setSpacing(6)
        self.new_scene_btn = _ghost(t.t("btn.new"))
        self.new_scene_btn.clicked.connect(self.new_scene)
        self.edit_scene_btn = _ghost(t.t("btn.edit"))
        self.edit_scene_btn.clicked.connect(self.edit_scene)
        self.copy_scene_btn = _ghost(t.t("btn.copy"))
        self.copy_scene_btn.clicked.connect(self.copy_scene)
        self.delete_scene_btn = _danger(t.t("btn.delete"))
        self.delete_scene_btn.clicked.connect(self.delete_scene)
        for b in (self.new_scene_btn, self.edit_scene_btn, self.copy_scene_btn,
                  self.delete_scene_btn):
            row.addWidget(b)
        row.addStretch(1)
        return row

    # ---------------------------------------------------------------- 列表
    @staticmethod
    def _fill(list_widget: QListWidget, paths: list[Path], namer) -> None:
        """填表：文本 = 场景名（坏文件 = 文件名），tooltip = 路径，UserRole = 路径。"""
        list_widget.clear()
        for path in paths:
            item = QListWidgetItem(namer(path))
            item.setToolTip(str(path))
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            list_widget.addItem(item)

    def refresh(self) -> None:
        """重扫场景目录（新建/复制/删除/导入后调用），列表回到最新文件。"""
        self._fill(self.scene_list, list_scene_paths(self._scenes_dir), _scene_name)
        self._refresh_use_btn()

    @staticmethod
    def _current_path(list_widget: QListWidget) -> Path | None:
        item = list_widget.currentItem()
        if item is None:
            return None
        data = item.data(Qt.ItemDataRole.UserRole)
        return Path(data) if data else None

    def _select_path(self, list_widget: QListWidget, path: Path | None) -> None:
        """按路径重新选中一行（刷新后保持/定位选择）。"""
        if path is None:
            return
        for i in range(list_widget.count()):
            item = list_widget.item(i)
            if Path(str(item.data(Qt.ItemDataRole.UserRole))) == Path(path):
                list_widget.setCurrentItem(item)
                item.setSelected(True)
                return

    def _refresh_use_btn(self) -> None:
        """选了一个场景就能「打开」——**不看角色**（空阵容的场景照样能开，§3.3）。"""
        self.use_btn.setEnabled(self._current_path(self.scene_list) is not None)

    def _characters_root(self) -> Path:
        """角色库目录（导入角色模板时的落盘处）；没给就退到场景目录的兄弟目录。"""
        return self._characters_dir or (self._scenes_dir.parent / "characters")

    # ---------------------------------------------------------------- 模板导入
    def import_from_template(self) -> None:
        """「从模板导入…」：挑一个按 templates/ 填好的 .md → 解析入库 → 刷新场景列。

        角色卡 / 场景卡由**文件内容**自动判定（template_import.detect_kind），落盘路径与
        覆盖守门也由它负责；失败只弹一句中文说明（带行号与字段名），绝不把 traceback
        丢到界面上。导入的是场景就把新文件选中，用户直接就能「用这套开场」；导入的是
        角色卡则只刷新（角色列表在「角色」菜单那一侧，本弹窗不列角色）。
        """
        t = translator()
        filename, _selected_filter = QFileDialog.getOpenFileName(
            self, t.t("dlg.import_pick"), str(self._characters_root()),
            t.t("dlg.import_filter"))
        if not filename:
            return
        try:
            result = import_template_file(
                Path(filename), characters_dir=self._characters_root(),
                scenes_dir=self._scenes_dir)
        except TemplateError as exc:
            warn(self, t.t("dlg.import_failed"), _template_error_text(exc))
            return
        self.last_import = result
        self.refresh()
        if result.kind == "scene":
            self._select_path(self.scene_list, result.path)
        info(self, t.t("dlg.import_done"), import_result_text(result))

    # ---------------------------------------------------------------- 场景操作
    def new_scene(self) -> None:
        dlg = SceneEditorDialog(None, self._scenes_dir, self._characters_dir, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.refresh()
            self._select_path(self.scene_list, dlg.saved_path)

    def edit_scene(self) -> None:
        path = self._current_path(self.scene_list)
        if path is None:
            return
        try:
            scene = load_scene(path)
        except (OSError, ValueError) as exc:
            warn(self, _t("err.cannot_edit"),
                 _t("err.scene_parse_failed", path=path, exc=exc))
            return
        dlg = SceneEditorDialog(scene, self._scenes_dir, self._characters_dir, self, path)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.refresh()
            self._select_path(self.scene_list, dlg.saved_path)

    def copy_scene(self) -> None:
        """复制一份场景：**文件名**加「副本」（场景名与文件名已解耦，§3.4，故以文件名为准）。

        副本名仍要能安全当文件名（源文件名带分隔符/`..` 时拒复制，不写盘）。
        """
        path = self._current_path(self.scene_list)
        if path is None:
            return
        try:
            scene = load_scene(path)
        except (OSError, ValueError) as exc:
            warn(self, _t("err.cannot_copy"),
                 _t("err.scene_parse_failed", path=path, exc=exc))
            return
        base = f"{path.stem} 副本"
        err = scene_filename_error(f"{base}.json")
        if err:
            warn(self, _t("err.cannot_copy"),
                 _t("err.filename_not_copy_name", err=err, path=path))
            return
        filename = f"{_unique_name(self._scenes_dir, base)}.json"
        scene.name = f"{scene.name or path.stem} 副本"
        new_path = save_scene(scene, scene_path_for(self._scenes_dir, filename))
        self.refresh()
        self._select_path(self.scene_list, new_path)

    def delete_scene(self) -> None:
        path = self._current_path(self.scene_list)
        if path is None:
            return
        if not confirm(self, _t("dlg.title_delete_scene"),
                       _t("dlg.confirm_delete_scene", path=path)):
            return
        try:
            path.unlink()
        except OSError as exc:
            warn(self, _t("dlg.title_delete_failed"), f"{path}\n{exc}")
            return
        self.refresh()

    # ---------------------------------------------------------------- 打开
    def use_this(self) -> None:
        """把选中的场景写进 `chosen_scene` 并 accept（「打开」这套）。没选就什么都不做。

        阵容不在这里定：打开后以**场景文件里的 characters** 为准（§3.1 场景持有阵容），
        空数组是合法的空场。故 `chosen_characters` 恒为空表——它只为兼容旧调用方存在。
        """
        scene = self._current_path(self.scene_list)
        if scene is None:
            return
        self.chosen_scene = scene
        self.accept()


# ====================================================== 角色详情（只读）
class CharacterDetailDialog(QDialog):
    """角色卡「查看详情」：只读展示一张卡的静态资料。

    右栏角色卡只留姓名与实时分量（谁在场、此刻多想说话），设定/能力/关系/性格权重
    （w1…w7）/情绪衰减/语料全部收进这里按需查看。数据来自本场 sig_scene_info 里那张
    卡的原始载荷（dict）——**只含卡本身的公开设定，绝无私有思考或运行期状态**，故构造
    只收 payload，不碰引擎与文件。

    `body_text()` 汇总窗内全部只读文本（测试与可访问性用，不依赖具体控件层级）。
    """

    def __init__(self, payload: dict, parent: QWidget | None = None):
        super().__init__(parent)
        t = translator()
        self.payload = dict(payload or {})
        self.setStyleSheet(_dialog_stylesheet())
        self.setWindowTitle(t.t("dlg.detail_title",
                                name=self.payload.get("name") or "?"))
        self.setMinimumSize(520, 560)

        inner = QWidget()
        form = QVBoxLayout(inner)
        form.setContentsMargins(14, 12, 14, 12)
        form.setSpacing(10)

        # ---- 设定 ----
        prof = QGroupBox(t.t("grp.profile"))
        pv = QVBoxLayout(prof)
        pv.setSpacing(6)
        self._fill_pairs(pv, self.payload.get("personality") or {})
        form.addWidget(prof)

        # ---- 能力 ----
        abox = QGroupBox(t.t("detail.abilities"))
        av = QVBoxLayout(abox)
        av.setSpacing(6)
        abilities = [a for a in (self.payload.get("abilities") or []) if str(a).strip()]
        if abilities:
            for a in abilities:
                av.addWidget(_kv(f"· {a}"))
        else:
            av.addWidget(_kv(t.t("label.unfilled"), muted=True))
        form.addWidget(abox)

        # ---- 关系 ----
        rbox = QGroupBox(t.t("detail.relations"))
        rv = QVBoxLayout(rbox)
        rv.setSpacing(6)
        self._fill_pairs(rv, self.payload.get("relationships") or {})
        form.addWidget(rbox)

        # ---- 性格权重 + 情绪衰减 ----
        wbox = QGroupBox(t.t("detail.weights"))
        wv = QVBoxLayout(wbox)
        wv.setSpacing(6)
        weights = self.payload.get("weights") or {}
        for field, label in WEIGHT_FIELDS:
            if field in weights:
                # 数值等宽右对齐（§3.6）：权重是一列可比的「仪器读数」。
                _num_row(wv, label, _num_text(weights.get(field)))
        decay = self.payload.get("emotion_decay_rate")
        if decay is not None:
            _num_row(wv, t.t("field.decay"), _num_text(decay))
        if not weights and decay is None:
            wv.addWidget(_kv(t.t("label.unfilled"), muted=True))
        form.addWidget(wbox)

        # ---- 语料 ----
        cbox = QGroupBox(t.t("field.corpus"))
        cv = QVBoxLayout(cbox)
        cv.setSpacing(6)
        self._fill_corpus(cv, self.payload.get("corpus") or {})
        form.addWidget(cbox)
        form.addStretch(1)

        self.close_btn = _ghost(t.t("btn.close"))
        self.close_btn.clicked.connect(self.accept)
        foot = QWidget()
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(14, 8, 14, 12)
        fl.addStretch(1)
        fl.addWidget(self.close_btn)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(_scroll_form(inner), 1)
        root.addWidget(foot)

    # ---------------------------------------------------------------- 填充
    @staticmethod
    def _fill_pairs(v: QVBoxLayout, pairs: dict) -> None:
        """「键：值」逐行；空则一行弱化占位（不留白）。"""
        items = [(str(k), str(val)) for k, val in (pairs or {}).items()]
        if not items:
            v.addWidget(_kv(_t("label.unfilled"), muted=True))
            return
        for k, val in items:
            v.addWidget(_kv(f"{k}：{val}"))

    @staticmethod
    def _fill_corpus(v: QVBoxLayout, corpus: dict) -> None:
        """语料五件套：出处 / 语言风格 / 思维方式 / 口头禅 / 样例（空项跳过）。"""
        def _block(title: str, body: str) -> None:
            v.addWidget(_label(title, "sect"))
            v.addWidget(_kv(body))

        source = str(corpus.get("source") or "").strip()
        if source:
            _block(_t("field.origin"), source)
        style = str(corpus.get("style") or "").strip()
        if style:
            _block(_t("field.style"), style)
        thinking = str(corpus.get("thinking") or "").strip()
        if thinking:
            _block(_t("field.thinking"), thinking)
        quirks = [str(q) for q in (corpus.get("quirks") or []) if str(q).strip()]
        if quirks:
            _block(_t("detail.catchphrase"), "\n".join(f"· {q}" for q in quirks))
        samples = [str(s) for s in (corpus.get("samples") or []) if str(s).strip()]
        if samples:
            _block(_t("detail.samples"), "\n".join(f"· {s}" for s in samples))
        if not (source or style or thinking or quirks or samples):
            v.addWidget(_kv(_t("label.unfilled"), muted=True))

    # ---------------------------------------------------------------- 文本
    def body_text(self) -> str:
        """窗内全部只读文本（换行连接）——不依赖控件层级，供测试/可访问性取整体内容。"""
        return "\n".join(lb.text() for lb in self.findChildren(QLabel) if lb.text())
