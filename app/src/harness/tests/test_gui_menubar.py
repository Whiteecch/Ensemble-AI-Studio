"""顶栏菜单栏（仿 VSCode，设计文档 §2.1）：结构、接线、持久化——离屏、确定性、不联网。

覆盖：
  · 三个顶级菜单（设置/场景/角色）顺序与各自菜单项；设置下的配色/语言/自动保存周期
    三组互斥勾选子菜单（项数、标签、勾选态）；
  · 选配色 → 窗口 setStyleSheet(theme_qss(该主题)) 且落盘（SettingsStore 指到 tmp 路径）；
  · 选语言 → 落盘 + 勾中 + 广播 language_changed + 写 app 属性（本阶段不翻译文案）；
  · 选自动保存周期 → 落盘 + 勾中；
  · 「模型 api 配置…」弹窗：字段预填、显示开关、保存落盘、「拉取模型」走后台线程
    （monkeypatch 掉网络）拿到 id 列表填进下拉，失败只在弹窗里红字提示且不抛；
  · 场景/角色两个菜单的结构与顺序（含「配置场景…」「保存场景」「导入场景…/导入角色…」、
    「管理场景库…」）与禁用规则；
  · 「打开场景」子菜单列场景库（坏文件标「（坏文件）」）→ 点击走 switch_scene 切场；
  · 启动：settings.json 里选了深色 → 构造窗口即铺深色表并勾中该项。

全部离屏（QT_QPA_PLATFORM=offscreen）、不联网、不碰真实用户设置（默认设置路径一律
monkeypatch 到 tmp_path）。若环境未装 PySide6（可选依赖 gui），整模块 skip。
"""
import json
import os
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QObject, QTimer, Signal  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QDialog, QLabel, QLineEdit, QWidget)

from harness.gui import library as lib_mod  # noqa: E402
from harness.gui import main_window as mw_mod  # noqa: E402
from harness.gui import settings as settings_mod  # noqa: E402
from harness.gui.main_window import (  # noqa: E402
    PALETTE, PALETTE_DARK, ApiConfigDialog, AppConfig, CharacterPickerDialog,
    MainWindow, autosave_label, assign_name_colors, color_for, extract_model_ids,
    models_url, speaker_palette)
from harness.gui.settings import AUTOSAVE_EVERY, AppSettings, SettingsStore  # noqa: E402
from harness.gui.theme import (  # noqa: E402
    PALETTES, SPEAKER_COLORS, Palette, THEMES, theme_qss)
from harness.i18n import LANGUAGES  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    """模块级复用单一 QApplication（Qt 只允许每进程一个）。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def tmp_store(tmp_path, monkeypatch):
    """把「默认设置路径」指到临时目录并返回该路径。

    窗口构造时 SettingsStore() 走的就是这个路径，故所有勾选动作只写临时文件——
    绝不碰开发者真实用户目录里的 settings.json。
    """
    path = tmp_path / "settings" / "settings.json"
    monkeypatch.setattr(settings_mod, "default_settings_path", lambda: path)
    return path


# --------------------------------------------------------------------- 素材
def _write_card(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / f"{name}.json"
    p.write_text(json.dumps({"name": name, "personality": {"描述": "示例"}},
                            ensure_ascii=False), encoding="utf-8")
    return p


def _write_scene(directory: Path, name: str, participants: list[str],
                 filename: str | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / (filename or f"{name}.json")
    p.write_text(json.dumps({
        "name": name, "participants": list(participants),
        "circles": [{"id": "主圈", "members": list(participants)}]},
        ensure_ascii=False), encoding="utf-8")
    return p


class _FakeWorker(QObject):
    """替身 worker：只记 start_scene 调用 + 提供 MainWindow 要连的信号（不起线程）。"""

    sig_scene_info = Signal(dict)
    sig_message = Signal(dict)
    sig_metrics = Signal(dict)
    sig_status = Signal(str)
    sig_finished = Signal()
    sig_dynamics = Signal(dict)
    sig_think = Signal(dict)
    sig_narration = Signal(dict)
    sig_retracted = Signal(int)
    sig_cast = Signal(dict)
    sig_auto_paused = Signal(bool)
    sig_scene_diag = Signal(dict)

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict] = []
        self.languages: list[str] = []        # set_language 收到的语言码（按序）
        self.cast_ok = False                  # can_cast() 的替身开关（角色菜单看它）

    def start_scene(self, **kwargs) -> None:
        self.calls.append(kwargs)

    # ---- 演员表（角色菜单接的 worker 面；本模块只用到 can_cast）----
    def can_cast(self) -> bool:
        return bool(self.cast_ok)

    def set_language(self, code: str) -> None:
        """语言也送到引擎侧（§7：提示词注入「你将使用<语言>回答。」）。

        单独记在 languages 里（不进 calls）——calls 是「开场/人事调用」账本，混进语言
        会让既有的「不得重投开场」这类断言失真。
        """
        self.languages.append(code)

    def add_character(self, name, notify=None, notify_text="", visible=True) -> None:
        self.calls.append({"add_character": name})

    def remove_character(self, name, notify=None, notify_text="", visible=True) -> None:
        self.calls.append({"remove_character": name})

    def mute_character(self, name, turns: int = 0) -> None:
        self.calls.append({"mute_character": name})

    def unmute_character(self, name) -> None:
        self.calls.append({"unmute_character": name})

    def schedule_cast_change(self, character_name, action, fire_after_rounds,
                             notify=None, notify_text="", visible=True,
                             turns: int = 0, reason: str = "") -> None:
        self.calls.append({"schedule_cast_change": character_name})

    def restart(self, *a, **k) -> None:
        pass

    def say(self, text: str) -> None:
        pass

    def can_say(self) -> bool:
        return False

    def set_paused(self, paused: bool) -> None:   # noqa: D102
        pass

    def set_auto_narrate(self, on: bool) -> None:  # noqa: D102
        pass

    def narrate_now(self) -> None:               # noqa: D102
        pass

    def stop_now(self) -> None:
        pass

    def set_pace(self, seconds: float) -> None:   # noqa: D102
        pass

    def set_rate(self, rate: float) -> None:      # noqa: D102
        pass

    def retract_message(self, mid: int) -> None:  # noqa: D102
        pass

    def edit_narration(self, mid: int, text: str) -> None:  # noqa: D102
        pass

    def shutdown(self, *a) -> None:
        pass

    def isRunning(self) -> bool:                  # noqa: N802
        return False


def _make_window(tmp_path: Path, *, settings: AppSettings | None = None,
                 scene_name: str = "餐厅",
                 participants: tuple[str, ...] = ("甲", "乙"),
                 cards: tuple[str, ...] = ("甲", "乙")):
    """真实 MainWindow 装配（替身 worker，不 show、不起线程）+ 素材目录。

    返回 (窗口, 替身 worker, (角色库目录, 场景库目录))。
    """
    cdir, sdir = tmp_path / "characters", tmp_path / "scenes"
    card_paths = [_write_card(cdir, n) for n in cards]
    scene = _write_scene(sdir, scene_name, list(participants))
    models = tmp_path / "models.yaml"
    models.write_text("think: {backend: stub, model: stub, params: {}}\n"
                      "speak: {backend: stub, model: stub, params: {}}\n",
                      encoding="utf-8")
    cfg = AppConfig(scene=scene, characters=card_paths, models=models,
                    bid=tmp_path / "bid.yaml", run_root=tmp_path / "runs",
                    live=False, api_key=None, opening="夜里。")
    worker = _FakeWorker()
    win = MainWindow(worker, cfg, settings=settings)
    return win, worker, (cdir, sdir)


def _pump_until(qapp, cond, timeout_ms: int = 5000, step_ms: int = 10) -> bool:
    """轮询主线程事件循环直到 cond() 为真；超时返回 False（不抛）。"""
    loop = QEventLoop()
    ok = {"v": False}
    timer = QTimer()
    timer.setInterval(step_ms)

    def _tick():
        if cond():
            ok["v"] = True
            timer.stop()
            loop.quit()

    timer.timeout.connect(_tick)
    timer.start()
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    timer.stop()
    return ok["v"]


def _unique_colour(theme: str) -> str:
    """该主题独有的一枚颜色（不出现在其它主题的样式表里）——用来断言「真换了表」。"""
    others = "".join(theme_qss(t) for t in THEMES if t != theme)
    for field in Palette.__dataclass_fields__:
        value = getattr(PALETTES[theme], field)
        if value not in others:
            return value
    raise AssertionError(f"{theme} 主题没有任何独有颜色")


# ============================================================ 1. 菜单结构
def test_top_level_menus_exist_in_the_exact_order(qapp, tmp_path, tmp_store):
    """顶栏：设置 / 场景 / 角色 / 信息库… / 关系…（前三段顺序即设计文档 §2.1，
    第四段见 §8.1，第五段见《人际关系与场景推进》§6.1）。"""
    win, _worker, _dirs = _make_window(tmp_path)
    titles = [a.text() for a in win.menuBar().actions()]
    assert titles == ["设置", "场景", "角色", "信息库…", "关系…"]
    assert win._menu_settings.title() == "设置"
    assert win._menu_scene.title() == "场景"
    assert win._menu_character.title() == "角色"


def test_settings_menu_has_api_item_and_four_groups(qapp, tmp_path, tmp_store):
    """设置菜单：模型 api 配置… + 配色/语言/自动保存周期三组子菜单，标签与项数对齐枚举。"""
    win, _worker, _dirs = _make_window(tmp_path)
    texts = [a.text() for a in win._menu_settings.actions()]
    assert texts[0] == "模型 api 配置…"
    subs = [a.text() for a in win._menu_settings.actions() if a.menu() is not None]
    assert subs == ["配色", "语言", "自动保存周期"]

    assert [a.text() for a in win._theme_menu.actions()] == list(THEMES)
    assert [a.text() for a in win._language_menu.actions()] == list(LANGUAGES.values())
    assert ([a.text() for a in win._autosave_menu.actions()]
            == [autosave_label(n) for n in AUTOSAVE_EVERY])
    assert [autosave_label(n) for n in AUTOSAVE_EVERY] == ["每 5 轮", "每 20 轮",
                                                           "每 50 轮", "每 100 轮"]
    # 三组都是互斥可勾选
    for group, actions in ((win._theme_group, win._theme_menu.actions()),
                           (win._language_group, win._language_menu.actions()),
                           (win._autosave_group, win._autosave_menu.actions())):
        assert group.isExclusive()
        assert all(a.isCheckable() for a in actions)
    # 语言标签取母语名（码 → 名来自 i18n.LANGUAGES）
    assert win._language_actions["ja"].text() == "日本語"
    assert win._language_actions["ko"].text() == "한국어"
    assert win._language_actions["fr"].text() == "Français"


def test_character_menu_lists_the_spec_items_in_order(qapp, tmp_path, tmp_store,
                                                      monkeypatch):
    """角色菜单（§2.1③/§3.3）：添加/移出/新建/**导入…**/管理…/高级…——顺序固定。

    没有打开的场次（can_cast 假）时，「添加/移出」两项禁用并给出原因；「新建角色」
    「导入角色…」「管理角色…」「高级移入/移出…」始终可用（都不依赖已开场次）。
    菜单行为（点击 → worker 人事 API）见 test_gui_characters.py。
    """
    win, _worker, (cdir, _sdir) = _make_window(tmp_path)
    acts = win._menu_character.actions()
    assert [a.text() for a in acts] == ["添加角色", "移出角色", "新建角色",
                                       "导入角色…", "管理角色…", "高级移入/移出…",
                                       "结算待决…"]
    assert win._action_add_character.isEnabled() is False, "没开场时不该能加人"
    assert "打开" in win._action_add_character.toolTip(), "禁用要说明原因"
    assert win._action_remove_character.isEnabled() is False, "没开场时不该能移人"
    assert win._action_new_character.isEnabled() is True
    assert win._action_import_character.isEnabled() is True, "导入不依赖场次"
    assert win._action_manage_characters.isEnabled() is True
    assert win._action_advanced_cast.isEnabled() is True

    # 「新建角色」打开现有角色编辑器：目录＝角色库目录，无预置卡（空白）。
    seen: dict = {}

    class _FakeEditor:
        saved_path = None

        def __init__(self, card=None, characters_dir=None, parent=None,
                     **kw):      # libraries_root（§8.2 订阅区）由主窗口一并传下来
            seen["card"] = card
            seen["dir"] = characters_dir
            seen["parent"] = parent

        def exec(self):
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr(mw_mod, "CharacterEditorDialog", _FakeEditor)
    win._action_new_character.trigger()
    assert seen["card"] is None, "新建角色应给空白卡"
    assert Path(seen["dir"]) == cdir
    assert seen["parent"] is win


# ============================================================ 2. 配色
def test_theme_selection_applies_stylesheet_and_persists(qapp, tmp_path, tmp_store):
    """选「深色」→ 窗口样式表立刻变成深色（含该主题独有色）+ 落盘 + 勾中该项。"""
    win, _worker, _dirs = _make_window(tmp_path)
    before = win.styleSheet()
    dark = _unique_colour("深色")
    assert dark not in before, "起手应是默认配色"

    win._theme_actions["深色"].trigger()

    assert dark in win.styleSheet(), "应立刻 setStyleSheet(theme_qss(深色))"
    assert win.styleSheet() == theme_qss("深色")
    assert win.styleSheet() != before
    assert win._theme_actions["深色"].isChecked()
    assert not win._theme_actions["默认"].isChecked(), "互斥：同组只能勾一个"
    assert SettingsStore(tmp_store).load().theme == "深色", "选择应落盘"


def test_theme_switch_repaints_the_content_not_just_the_stylesheet(qapp, tmp_path,
                                                                   tmp_store):
    """切主题要连**内容**一起换色：对白 HTML 与左右栏标签的颜色是渲染时烘死的（G1）。

    旧实现只 setStyleSheet：深色主题下对白正文/时刻戳、左栏键值行、冲动行数值、角色卡
    姓名仍是默认配色的深字——深底上深字，转录几乎不可读。
    """
    win, worker, _dirs = _make_window(tmp_path)
    cards = [{"name": n, "personality": {}, "abilities": [], "relationships": {},
              "weights": {}, "emotion_decay_rate": 0.4, "corpus": {}}
             for n in ("甲", "乙")]
    worker.sig_scene_info.emit({"scene": {"name": "茶室", "participants": ["甲", "乙"]},
                                "characters": cards, "backend": "stub",
                                "start_time": "21:30", "auto_narrate": True})
    worker.sig_message.emit({"id": 1, "speaker": "甲", "speaker_type": "character",
                             "content": "夜里风大。", "time_hhmmss": "21:31:00"})

    light, dark = PALETTES["默认"], PALETTES["深色"]
    assert light.text in win._view.toHtml(), "起手应是默认配色的正文色"
    assert light.text in win._scene_value.styleSheet()
    # 起手：左栏色点/词条就是**各人自己的**说话者色（不能一律烘焙成默认强调色）
    assert PALETTE[0] in win._urge_rows["甲"]["dot"].styleSheet()
    assert PALETTE[1] in win._urge_rows["乙"]["dot"].styleSheet()
    assert PALETTE[0] in win._urge_rows["甲"]["bar"].styleSheet()
    assert PALETTE[1] in win._urge_rows["乙"]["bar"].styleSheet()

    win._theme_actions["深色"].trigger()

    html = win._view.toHtml()
    assert dark.text in html, "对白正文应改用深色主题的正文色"
    assert light.text not in html, "默认主题的深字不该留在深底上"
    assert dark.time_text in html, "时刻戳也应换成深色主题的弱化色"
    assert PALETTE_DARK[0] in html, "说话者名色应换提亮版（深底上读得清）"
    assert PALETTE[0] not in html, "浅底那套说话者色不该留在深色主题下"
    # 左右栏程序化配色的标签（键值行 / 冲动行 / 角色卡名）
    assert dark.text in win._scene_value.styleSheet()
    assert dark.text in win._metric["blocks"].styleSheet(), "左栏键值行的值标签"
    urge_labels = [row["lab"] for row in win._urge_rows.values()]
    assert urge_labels and all(dark.text in lb.styleSheet() for lb in urge_labels)
    assert all(dark.bar_track in row["bar"].styleSheet()
               for row in win._urge_rows.values()), "数值条槽底也应换色"
    assert PALETTE_DARK[0] in win._urge_rows["甲"]["bar"].styleSheet(), \
        "冲动词条的说话者色也要跟着主题提亮（与对白同色）"
    assert PALETTE_DARK[0] in win._urge_rows["甲"]["dot"].styleSheet()
    assert PALETTE_DARK[1] in win._urge_rows["乙"]["dot"].styleSheet(), \
        "每个人各自回绕到自己的提亮色，不是一律同一个"
    card_names = [lb for lb in win.findChildren(QLabel)
                  if lb.styleSheet().startswith("font-size:15px")]
    assert card_names and all(dark.text in lb.styleSheet() for lb in card_names)
    # 默认主题那几枚硬编码色（键值行值色 #3f3b35 / 角色卡名 #2b2824）不该留在深色主题下
    for lb in urge_labels + card_names + [win._metric["blocks"]]:
        assert "#3f3b35" not in lb.styleSheet() and "#2b2824" not in lb.styleSheet()
        assert PALETTES["默认"].text not in lb.styleSheet()

    win._theme_actions["默认"].trigger()
    assert light.text in win._view.toHtml(), "切回默认应逐色还原"


def test_theme_switch_keeps_live_dynamics_values_on_screen(qapp, tmp_path, tmp_store):
    """切主题**重刷样式而不重建部件** → 已经显示的实时分量不被抹回占位。

    （若走「重建冲动行/角色卡」那条路，右栏数值会集体变回「—」，暂停着看界面时尤其明显。）
    """
    win, worker, _dirs = _make_window(tmp_path)
    cards = [{"name": n, "personality": {}, "abilities": [], "relationships": {},
              "weights": {}, "emotion_decay_rate": 0.4, "corpus": {}}
             for n in ("甲", "乙")]
    worker.sig_scene_info.emit({"scene": {"name": "茶室", "participants": ["甲", "乙"]},
                                "characters": cards, "backend": "stub"})
    worker.sig_dynamics.emit({"甲": {"bid": 1.25, "turns_since_spoke": 2,
                                     "relevance": 0.5, "arousal": 0.4,
                                     "adjacency": 0.3, "goal_pressure": 0.2,
                                     "scene_pressure": 0.1, "self_urge": 0.5,
                                     "pending_reply": 0}})
    assert win._urge_rows["甲"]["num"].text() == "1.25"
    assert win._dyn_state["甲"]["bid"]["num"].text() == "1.25"

    win._theme_actions["深色"].trigger()

    assert win._urge_rows["甲"]["num"].text() == "1.25", "切主题不该抹掉实时数值"
    assert win._dyn_state["甲"]["bid"]["num"].text() == "1.25"
    win._theme_actions["默认"].trigger()


def test_speaker_colours_follow_the_theme():
    """说话者配色盘**按主题取**（色值来自 theme.SPEAKER_COLORS，界面不留字面色）。

    四套主题各有一套：首枚即该主题自己的 accent（视觉锚点），同一套里颜色两两不同
    （相邻两人不会撞色）；浅底两套用深饱和色、深底两套用提亮色，两族不共用色值
    （浅底那套印在近黑底上读不清）。
    """
    for theme in THEMES:
        pal = speaker_palette(theme)
        assert pal == list(SPEAKER_COLORS[theme]), theme
        assert pal[0] == PALETTES[theme].accent, f"{theme}：首枚应是该主题强调色"
        assert len(pal) >= 4 and len(set(pal)) == len(pal), f"{theme}：同套内不许重色"
    light = set(speaker_palette("默认")) | set(speaker_palette("白色"))
    dark = set(speaker_palette("深色")) | set(speaker_palette("深蓝"))
    assert not (light & dark), "深浅两族不该共用任何一枚说话者色"
    assert speaker_palette("深色") != speaker_palette("深蓝"), "深色/深蓝各有一套"
    assert speaker_palette("不存在") == speaker_palette("默认"), "未知名回退默认"
    assert speaker_palette(None) == speaker_palette("默认"), "None 同回退默认"

    # 缺省行为一字不改（既有调用方与测试不受影响）
    assert assign_name_colors(["甲", "乙"]) == {"甲": PALETTE[0], "乙": PALETTE[1]}
    assert assign_name_colors(["甲"], PALETTE_DARK) == {"甲": PALETTE_DARK[0]}
    assert color_for(9, PALETTE_DARK) == PALETTE_DARK[1], "超长仍回绕"


def _badge_pill(win):
    """状态区那枚模型药丸（stub/真实模型的 QLabel）——取容器布局里**当前**那一个。

    （旧药丸走 deleteLater，事件循环没转时仍是子部件，故 findChildren 会连旧的一起捞到。）
    """
    item = win._model_badge.layout().itemAt(0)
    assert item is not None and item.widget() is not None, "模型药丸应已建出"
    return item.widget()


def test_model_badge_uses_the_theme_palette(qapp, tmp_path, tmp_store):
    """模型药丸的底/字一律取自当前主题（旧暖纸色 #f1ede4/#8b8577 一个都不许留）。

    stub = chip_bg/chip_text（中性小标签），真实模型 = accent/accent_text（强调）；
    切主题时药丸**当场重画**，不把旧主题的颜色烘死在标签上。
    """
    win, _worker, _dirs = _make_window(tmp_path)
    light, dark = PALETTES["默认"], PALETTES["深色"]

    pill = _badge_pill(win)
    assert pill.text() == win._t.t("badge.stub")
    assert light.chip_bg in pill.styleSheet() and light.chip_text in pill.styleSheet()
    for old in ("#f1ede4", "#8b8577", "#d1fae5", "#065f46"):
        assert old not in pill.styleSheet(), f"药丸不该再写死 {old}"

    win._set_model_badge("deepseek")
    real = _badge_pill(win)
    assert real.text() == win._t.t("badge.real_model")
    assert light.accent in real.styleSheet() and light.accent_text in real.styleSheet()

    win.apply_theme("深色")
    dark_pill = _badge_pill(win)
    assert dark_pill.text() == win._t.t("badge.real_model"), "切主题不换后端，只换色"
    assert dark.accent in dark_pill.styleSheet()
    assert light.accent not in dark_pill.styleSheet(), "不该留着默认主题的强调色"

    win._set_model_badge("stub")
    assert dark.chip_bg in _badge_pill(win).styleSheet()


def test_speaker_colours_stay_on_theme_for_every_theme(qapp, tmp_path, tmp_store):
    """四套主题下**落到部件上**的说话者色都来自该主题自己的那套（不只深色一路）。

    深蓝主题起手建窗（设置里存的就是深蓝）→ 左栏色点/词条/右栏头像圆底一律用深蓝那套；
    切到白色主题再验一次：色值随之换套，且都没漏出别的主题的说话者色。
    """
    from harness.gui.theme import speaker_colors_for

    win, worker, _dirs = _make_window(
        tmp_path, settings=AppSettings(theme="深蓝"))
    cards = [{"name": n, "personality": {}, "abilities": [], "relationships": {},
              "weights": {}, "emotion_decay_rate": 0.4, "corpus": {}}
             for n in ("甲", "乙")]
    worker.sig_scene_info.emit({"scene": {"name": "茶室", "participants": ["甲", "乙"]},
                                "characters": cards, "backend": "stub"})
    navy = list(speaker_colors_for("深蓝"))
    assert navy[0] in win._urge_rows["甲"]["dot"].styleSheet()
    assert navy[1] in win._urge_rows["乙"]["dot"].styleSheet()
    assert navy[0] in win._urge_rows["甲"]["bar"].styleSheet()
    avatars = [lb for lb in win.findChildren(QLabel)
               if lb.styleSheet().startswith("QLabel{background:")
               and "border-radius:17px" in lb.styleSheet()]
    assert avatars and all(any(c in lb.styleSheet() for c in navy) for lb in avatars), \
        "右栏头像圆底也得用本主题那套说话者色"

    win.apply_theme("白色")
    white = list(speaker_colors_for("白色"))
    assert white[0] in win._urge_rows["甲"]["dot"].styleSheet()
    assert white[1] in win._urge_rows["乙"]["dot"].styleSheet()
    assert navy[0] not in win._urge_rows["甲"]["dot"].styleSheet(), \
        "切主题后不该留着上一套的说话者色"


def test_dialogs_built_after_a_theme_switch_use_the_themed_sheet(qapp, tmp_path,
                                                                 tmp_store):
    """弹窗按**打开那一刻**的主题出样式（库里的对话框与主窗口口径一致）。"""
    win, _worker, (cdir, _sdir) = _make_window(tmp_path)
    win.apply_theme("深色")

    picker = CharacterPickerDialog(["甲"])
    assert PALETTES["深色"].card_bg in picker.styleSheet(), "深色弹窗表应含深色卡片底"

    editor = lib_mod.CharacterEditorDialog(None, cdir)
    assert PALETTES["深色"].card_bg in editor.styleSheet()

    win.apply_theme("默认")
    assert PALETTES["默认"].card_bg in CharacterPickerDialog(["甲"]).styleSheet()


def test_startup_applies_saved_theme_and_checkmarks(qapp, tmp_path, tmp_store):
    """启动：settings.json 里选了深色 → 构造窗口即铺深色表，且配色/语言/周期都勾对。"""
    SettingsStore(tmp_store).save(AppSettings(theme="深色", language="ja",
                                              autosave_every=50))
    win, _worker, _dirs = _make_window(tmp_path, settings=None)

    assert _unique_colour("深色") in win.styleSheet(), "保存的配色应在显示前生效"
    assert win._theme_actions["深色"].isChecked()
    assert win._language_actions["ja"].isChecked()
    assert win._autosave_actions[50].isChecked()
    assert win._autosave_every == 50


# ============================================================ 3. 语言 / 自动保存
def test_language_selection_persists_and_broadcasts(qapp, tmp_path, tmp_store):
    """选「日本語」→ 落盘 + 勾中 + 广播 language_changed("ja") + 写 app 属性。"""
    win, _worker, _dirs = _make_window(tmp_path)
    seen: list[str] = []
    win.language_changed.connect(seen.append)

    win._language_actions["ja"].trigger()          # 标签是母语名「日本語」，键是语言码

    assert seen == ["ja"], "应广播 language_changed（后续 i18n 阶段消费）"
    assert win._language == "ja"
    assert win._language_actions["ja"].isChecked()
    assert not win._language_actions["en"].isChecked()
    assert SettingsStore(tmp_store).load().language == "ja"
    assert QApplication.instance().property("language") == "ja"


def test_language_checkmark_matches_loaded_value_on_startup(qapp, tmp_path, tmp_store):
    """语言勾选态与保存值对齐（默认 zh-Hans）；界面文案本阶段不翻译。"""
    win, _worker, _dirs = _make_window(tmp_path)
    assert win._language_actions["zh-Hans"].isChecked()
    assert win.menuBar().actions()[0].text() == "设置", "菜单文案保持中文（翻译在后续阶段）"


def test_autosave_selection_persists_and_checks(qapp, tmp_path, tmp_store):
    """选「每 50 轮」→ 落盘 + 勾中 + 窗口侧读得到该值。"""
    win, _worker, _dirs = _make_window(tmp_path)
    win._autosave_actions[50].trigger()

    assert SettingsStore(tmp_store).load().autosave_every == 50
    assert win._autosave_actions[50].isChecked()
    assert not win._autosave_actions[20].isChecked()
    assert win._autosave_every == 50, "窗口侧应能读到该值（供后续自动保存落盘用）"


# ====================================================== 4. 模型 api 配置弹窗
def test_api_dialog_prefills_and_saves_to_store(qapp, tmp_path, tmp_store):
    """弹窗按当前设置预填；填好保存 → 三项落盘；API Key 默认密码态、「显示」可切明文。"""
    store = SettingsStore(tmp_store)
    store.save(AppSettings(api_base_url="https://old.example", api_key="sk-old",
                           model_name="m-old"))
    dlg = ApiConfigDialog(store.load(), None, store=store)

    assert dlg.url_edit.text() == "https://old.example"
    assert dlg.key_edit.text() == "sk-old"
    assert dlg.model_edit.text() == "m-old"
    assert dlg.key_edit.echoMode() == QLineEdit.EchoMode.Password
    dlg.show_key_check.setChecked(True)
    assert dlg.key_edit.echoMode() == QLineEdit.EchoMode.Normal
    dlg.show_key_check.setChecked(False)
    assert dlg.key_edit.echoMode() == QLineEdit.EchoMode.Password

    dlg.url_edit.setText("https://api.deepseek.com")
    dlg.key_edit.setText("sk-new")
    dlg.model_edit.setText("deepseek-v4-flash")
    dlg.accept()

    s = store.load()
    assert s.api_base_url == "https://api.deepseek.com"
    assert s.api_key == "sk-new"
    assert s.model_name == "deepseek-v4-flash"
    assert dlg.saved_settings == s, "应给出保存后的快照供调用方刷新"


def test_api_dialog_cancel_changes_nothing(qapp, tmp_path, tmp_store):
    """取消：一个字段都不写盘。"""
    store = SettingsStore(tmp_store)
    store.save(AppSettings(api_base_url="https://keep", api_key="sk-keep"))
    dlg = ApiConfigDialog(store.load(), None, store=store)
    dlg.url_edit.setText("https://changed")
    dlg.key_edit.setText("sk-changed")
    dlg.reject()

    s = store.load()
    assert s.api_base_url == "https://keep" and s.api_key == "sk-keep"
    assert dlg.saved_settings is None


def test_api_dialog_fetch_fills_combo_with_ids(qapp, tmp_path, tmp_store, monkeypatch):
    """「拉取模型」走后台线程请求 `{url}/models`，把 data[].id 填进下拉（不联网）。"""
    calls: list[str] = []

    def _fake_fetch(url, timeout=10.0):
        calls.append(url)
        return ["deepseek-v4-flash", "deepseek-v4-pro"]

    monkeypatch.setattr(mw_mod, "fetch_model_ids", _fake_fetch)
    dlg = ApiConfigDialog(None, None, store=SettingsStore(tmp_store))
    dlg.url_edit.setText("https://api.deepseek.com")
    dlg.fetch_btn.click()

    assert _pump_until(qapp, lambda: dlg.model_combo.count() == 2), \
        "拉取结果应填进下拉"
    assert calls == ["https://api.deepseek.com"], "应请求填进弹窗的服务地址"
    assert [dlg.model_combo.itemText(i) for i in range(2)] == \
        ["deepseek-v4-flash", "deepseek-v4-pro"]
    assert dlg.fetch_btn.isEnabled(), "拉取结束应恢复按钮"
    assert dlg.error_label.text() == ""
    assert "2" in dlg.hint_label.text()

    # 从下拉里选一个 → 填进「模型名称」。
    dlg.model_combo.setCurrentIndex(1)
    dlg._on_model_activated(1)
    assert dlg.model_edit.text() == "deepseek-v4-pro"


def test_api_dialog_fetch_failure_shows_error_without_raising(
        qapp, tmp_path, tmp_store, monkeypatch):
    """拉取失败：弹窗内红字提示原因、按钮恢复、不抛异常、不弹模态框。"""

    def _boom(url, timeout=10.0):
        raise RuntimeError("连接超时")

    monkeypatch.setattr(mw_mod, "fetch_model_ids", _boom)
    dlg = ApiConfigDialog(None, None, store=SettingsStore(tmp_store))
    dlg.url_edit.setText("https://api.deepseek.com")
    dlg.fetch_btn.click()

    assert _pump_until(qapp, lambda: bool(dlg.error_label.text())), "失败应给提示"
    assert "拉取失败" in dlg.error_label.text()
    assert "连接超时" in dlg.error_label.text()
    assert dlg.model_combo.count() == 0
    assert dlg.fetch_btn.isEnabled(), "失败后应能重试"


def test_api_dialog_fetch_without_url_complains(qapp, tmp_path, tmp_store, monkeypatch):
    """没填服务地址就点拉取：直接提示，不起线程、不发请求。"""
    called: list[str] = []
    monkeypatch.setattr(mw_mod, "fetch_model_ids",
                        lambda url, timeout=10.0: called.append(url) or [])
    dlg = ApiConfigDialog(None, None, store=SettingsStore(tmp_store))
    dlg.fetch_btn.click()

    assert "服务地址" in dlg.error_label.text()
    assert called == []
    assert dlg.model_combo.count() == 0


def test_fetch_model_ids_hits_models_endpoint_and_parses_ids(monkeypatch):
    """底层实现：GET `{base_url}/models`，取 data[].id（用替身 httpx，绝不联网）。"""
    calls: list[str] = []

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"data": [{"id": "m-1"}, {"id": "m-2"}, {"nope": 1}, "m-3"]}

    class _Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url):
            calls.append(url)
            return _Resp()

    fake = types.ModuleType("httpx")
    fake.Client = _Client
    monkeypatch.setitem(sys.modules, "httpx", fake)

    assert mw_mod.fetch_model_ids("https://api.deepseek.com/v1/") == \
        ["m-1", "m-2", "m-3"]
    assert calls == ["https://api.deepseek.com/v1/models"], "服务地址 + /models"


def test_fetch_model_ids_raises_readable_errors(monkeypatch):
    """HTTP 错误 / 没有 id：抛可直接给用户看的 RuntimeError（不静默返回空表）。"""

    def _client_with(status, payload):
        class _Resp:
            status_code = status

            @staticmethod
            def json():
                return payload

        class _Client:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get(self, url):
                return _Resp()

        return _Client

    fake = types.ModuleType("httpx")
    fake.Client = _client_with(404, {})
    monkeypatch.setitem(sys.modules, "httpx", fake)
    with pytest.raises(RuntimeError, match="404"):
        mw_mod.fetch_model_ids("https://api.deepseek.com")

    fake.Client = _client_with(200, {"data": []})
    with pytest.raises(RuntimeError, match="没有任何模型 id"):
        mw_mod.fetch_model_ids("https://api.deepseek.com")

    # 辅助函数口径
    assert models_url(" https://api.deepseek.com/ ") == "https://api.deepseek.com/models"
    assert extract_model_ids({"data": [{"id": "x"}]}) == ["x"]
    assert extract_model_ids({"nope": 1}) == []
    assert extract_model_ids(None) == []


def test_api_config_menu_action_seeds_dialog_and_refreshes_settings(
        qapp, tmp_path, tmp_store, monkeypatch):
    """设置 →「模型 api 配置…」：弹窗拿到当前设置与存储，保存后窗口快照随之刷新。"""
    SettingsStore(tmp_store).save(AppSettings(api_base_url="https://saved",
                                              api_key="sk-1", model_name="m1"))
    win, _worker, _dirs = _make_window(tmp_path, settings=None)
    seen: dict = {}

    class _FakeDialog:
        def __init__(self, settings, parent, **kwargs):
            seen["settings"] = settings
            seen["parent"] = parent
            seen["theme"] = kwargs.get("theme")
            self.saved_settings = AppSettings(api_base_url="https://new",
                                              api_key="sk-2", model_name="m2")

        def exec(self):
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(mw_mod, "ApiConfigDialog", _FakeDialog)
    win._action_api_config.trigger()

    assert seen["settings"].api_base_url == "https://saved", "弹窗应按已存配置预填"
    assert seen["theme"] == "默认"
    assert win._settings.api_base_url == "https://new", "保存后窗口快照应刷新"


# ============================================================ 5. 场景菜单
def test_scene_menu_has_the_spec_items_in_order(qapp, tmp_path, tmp_store):
    """场景菜单（§3.3）：打开场景（子菜单）/ 配置场景… / 保存场景 / 重置运行上下文… /
    管理场景库… / 新建场景 / 导入场景… / 分隔线——顺序固定，末尾给「以后可扩展」留一条线。

    依赖已开场次的三项（保存/配置/重置）在空状态页上禁用；导入/新建/管理库始终可用。
    """
    win, _worker, (cdir, sdir) = _make_window(tmp_path)
    acts = win._menu_scene.actions()
    assert [a.text() for a in acts if not a.isSeparator()] == \
        ["打开场景", "配置场景…", "保存场景", "重置场景运行上下文…",
         "管理场景库…", "新建场景", "导入场景…"]
    assert acts[0].menu() is win._open_scene_menu, "第一项是「打开场景」子菜单"
    assert acts[-1].isSeparator(), "末尾留一条分隔线（以后可扩展）"

    # 未打开场景：保存/配置/重置禁用（没有可操作的对象），其余照常可用。
    assert not win._action_save_scene.isEnabled()
    assert not win._action_configure_scene.isEnabled()
    assert not win._action_reset_scene.isEnabled()
    assert win._action_manage_scenes.isEnabled()
    assert win._action_new_scene.isEnabled()
    assert win._action_import_scene.isEnabled()

    # 开场成功 → 三项放开（与中栏那两枚按钮同源）。
    win._on_scene_info({"scene": {"name": "餐厅", "participants": ["甲"]},
                        "characters": [], "backend": "stub",
                        "scene_path": str(sdir / "餐厅.json")})
    assert win._action_save_scene.isEnabled()
    assert win._action_configure_scene.isEnabled()
    assert win._action_reset_scene.isEnabled()


def test_scene_menu_lists_library_scenes(qapp, tmp_path, tmp_store):
    """「打开场景」子菜单按场景库列场景名；坏文件用「（坏文件）」+ 文件名；空目录给禁用占位。"""
    win, _worker, (_cdir, sdir) = _make_window(tmp_path)      # 已含 餐厅
    _write_scene(sdir, "天台", ["甲"], filename="a-天台.json")
    (sdir / "z-坏文件.json").write_text("{这不是 JSON", encoding="utf-8")

    win._refresh_scene_menu()
    acts = win._open_scene_menu.actions()
    texts = [a.text() for a in acts]
    assert "餐厅" in texts and "天台" in texts, "应列出场景名（读文件的 name）"
    assert any(t.startswith("（坏文件）") and "z-坏文件" in t for t in texts), \
        "坏文件应显示为文件名 + 说明，而不是凭空消失（界面不留任何符号）"
    bad = next(a for a in acts if a.text().startswith("（坏文件）"))
    assert bad.data().endswith("z-坏文件.json")

    # 空场景库：给一条禁用的占位，菜单依然可用。
    empty_win, _w2, _d2 = _make_window(tmp_path / "empty", scene_name="独场",
                                       participants=("甲",), cards=("甲",))
    (tmp_path / "empty" / "scenes" / "独场.json").unlink()
    empty_win._refresh_scene_menu()
    acts2 = empty_win._open_scene_menu.actions()
    assert len(acts2) == 1 and acts2[0].isEnabled() is False


def test_scene_menu_click_switches_via_existing_path(qapp, tmp_path, tmp_store):
    """点「打开场景」里的另一场 → 走 switch_scene：cfg 更新 + 向 worker 重投开场。

    角色名单由该场景的 participants 在角色库里配卡（甲只有一张卡 → 一人上场）。
    """
    win, worker, (cdir, sdir) = _make_window(tmp_path)
    _write_scene(sdir, "天台", ["甲"], filename="a-天台.json")
    win._refresh_scene_menu()

    act = next(a for a in win._open_scene_menu.actions() if a.text() == "天台")
    act.trigger()

    assert win._cfg.scene == sdir / "a-天台.json"
    assert win._cfg.characters == [cdir / "甲.json"], "按场景 participants 配卡"
    assert worker.calls, "切场应向 worker 重投开场"
    assert worker.calls[-1]["scene"] == win._cfg.scene
    assert worker.calls[-1]["characters"] == win._cfg.characters


def test_scene_menu_click_on_unreadable_scene_keeps_current_session(
        qapp, tmp_path, tmp_store, monkeypatch):
    """坏场景被点了：switch_scene 的校验拦下并提示，当前场次原样保留（不清面板不重投）。"""
    win, worker, (_cdir, sdir) = _make_window(tmp_path)
    (sdir / "z-坏文件.json").write_text("{这不是 JSON", encoding="utf-8")
    win._refresh_scene_menu()
    warned: list = []
    monkeypatch.setattr(mw_mod, "warn", lambda *a, **k: warned.append(a))

    act = next(a for a in win._open_scene_menu.actions()
               if a.text().startswith("（坏文件）"))
    act.trigger()

    assert warned, "应提示无法切换（不静默）"
    assert win._cfg.scene == sdir / "餐厅.json", "当前场次应保持不变"
    assert worker.calls == [], "不得向 worker 重投开场"


def test_menu_dialog_does_not_block_the_worker(qapp, tmp_path, tmp_store, monkeypatch):
    """菜单弹窗非模态安全：exec() 的嵌套事件循环期间 worker 照常推进（演出不冻住）。

    用真实 SceneWorker（stub 后端、离线）跑一场，然后经菜单打开一个「模态」弹窗——
    弹窗 exec() 里跑 0.6s 嵌套事件循环，期间统计到达 GUI 线程的角色台词条数：
    若弹窗把 worker 或主线程事件派发卡住，这个数就不会增长。
    """
    from harness.gui.worker import SceneWorker

    cdir, sdir = tmp_path / "characters", tmp_path / "scenes"
    cards = [_write_card(cdir, n) for n in ("甲", "乙")]
    scene = _write_scene(sdir, "餐厅", ["甲", "乙"])
    models = tmp_path / "models.yaml"
    models.write_text("think: {backend: stub, model: stub, params: {}}\n"
                      "speak: {backend: stub, model: stub, params: {}}\n"
                      "narrate: {backend: stub, model: stub, params: {}}\n",
                      encoding="utf-8")
    bid = tmp_path / "bid.yaml"
    bid.write_text("interruption_threshold: 0.0\nspeak_threshold: 0.0\n"
                   "silence_k: 8\n", encoding="utf-8")
    cfg = AppConfig(scene=scene, characters=cards, models=models, bid=bid,
                    run_root=tmp_path / "runs", live=False, api_key=None,
                    closing_at_block=100000, opening="夜里。")
    worker = SceneWorker()
    win = MainWindow(worker, cfg)
    worker.start()
    msgs: list[dict] = []
    worker.sig_message.connect(msgs.append)
    chars = lambda: [m for m in msgs                       # noqa: E731
                     if m.get("speaker_type") == "character"
                     and (m.get("content") or "").strip()]
    seen: dict = {}

    class _FakeLibrary:
        chosen_scene = None
        chosen_characters: list = []

        def __init__(self, *a, **k):
            pass

        def exec(self):
            loop = QEventLoop()
            QTimer.singleShot(600, loop.quit)
            loop.exec()                    # 模态弹窗的嵌套事件循环
            seen["during"] = len(chars())
            return QDialog.DialogCode.Rejected

    try:
        win.start_session()
        worker.set_pace(0.02)
        assert _pump_until(qapp, lambda: len(chars()) >= 1), "角色应开始自主开口"
        before = len(chars())

        monkeypatch.setattr(mw_mod, "LibraryDialog", _FakeLibrary)
        win._action_manage_scenes.trigger()    # 弹窗在嵌套循环里待 0.6s

        assert seen["during"] > before, \
            "弹窗期间 worker 应继续产出并送达界面（菜单弹窗不阻塞演出）"
        assert worker.isRunning(), "弹窗不该把 worker 线程弄停"
    finally:
        worker.shutdown(4000)
        assert not worker.isRunning()


def test_manage_scenes_opens_library_dialog(qapp, tmp_path, tmp_store, monkeypatch):
    """「管理场景…」复用现有库弹窗（打开/编辑/复制/删除 + 新建都已在其中）。"""
    win, _worker, (cdir, sdir) = _make_window(tmp_path)
    seen: dict = {}

    class _FakeLibrary:
        chosen_scene = None
        chosen_characters: list = []

        def __init__(self, characters_dir, scenes_dir, parent=None):
            seen["dirs"] = (Path(characters_dir), Path(scenes_dir))
            seen["parent"] = parent

        def exec(self):
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr(mw_mod, "LibraryDialog", _FakeLibrary)
    win._action_manage_scenes.trigger()
    assert seen["dirs"] == (cdir, sdir)
    assert seen["parent"] is win


def test_new_scene_opens_editor_and_switches(qapp, tmp_path, tmp_store, monkeypatch):
    """「新建场景」→ 空白编辑器；accept（已落盘）后切到新场并刷新子菜单。"""
    win, worker, (cdir, sdir) = _make_window(tmp_path)
    new_path = sdir / "新场.json"
    seen: dict = {}

    class _FakeEditor:
        saved_path = new_path

        def __init__(self, scene=None, scenes_dir=None, characters_dir=None,
                     parent=None):
            seen["scene"] = scene
            seen["scenes_dir"] = Path(scenes_dir)
            seen["characters_dir"] = Path(characters_dir)

        def exec(self):
            _write_scene(sdir, "新场", ["甲"], filename="新场.json")
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(mw_mod, "SceneEditorDialog", _FakeEditor)
    win._action_new_scene.trigger()

    assert seen["scene"] is None, "新建场景应给空白场景"
    assert seen["scenes_dir"] == sdir and seen["characters_dir"] == cdir
    assert win._cfg.scene == new_path, "保存后应切到新场"
    assert worker.calls and worker.calls[-1]["scene"] == new_path
    assert "新场" in [a.text() for a in win._open_scene_menu.actions()], \
        "新场应立刻出现在「打开场景」里"


# ========================================================= 6. 多语言（§7 i18n 接线）
def _left_labels(win: MainWindow) -> str:
    """左栏滚动区内**用户可见的全部文字**：标签 + 按钮 + 开关（含被隐藏的行）。"""
    body = win._left_scroll.widget()
    chunks = [lb.text() for lb in body.findChildren(QLabel)]
    chunks += [w.text() for w in body.findChildren(QWidget) if hasattr(w, "text")]
    return "\n".join(c for c in chunks if c)


def test_saved_english_settings_translate_menus_buttons_and_labels(
        qapp, tmp_path, tmp_store):
    """settings.json 里选了 en → 构造出来的窗口就是英文（菜单/按钮/静态标签/状态药丸）。"""
    SettingsStore(tmp_store).save(AppSettings(language="en"))
    win, _worker, _dirs = _make_window(tmp_path, settings=None)

    # 顶栏菜单：标题与各项
    assert [a.text() for a in win.menuBar().actions()] == \
        ["Settings", "Scene", "Character", "Knowledge base…", "Relationships…"]
    assert win._menu_settings.actions()[0].text() == "Model API config…"
    assert [a.text() for a in win._menu_settings.actions() if a.menu() is not None] == \
        ["Theme", "Language", "Autosave interval"]
    assert [a.text() for a in win._menu_scene.actions() if not a.isSeparator()] == \
        ["Open scene", "Configure scene…", "Save scene", "Reset scene runtime…",
         "Manage scene library…", "New scene", "Import scene…"]
    assert [a.text() for a in win._menu_character.actions()] == \
        ["Add character", "Remove character", "New character", "Import character…",
         "Manage characters…", "Advanced add/remove…", "Settle pending…"]
    assert win._autosave_actions[5].text() == "Every 5 rounds"
    assert win._language_actions["ja"].text() == "日本語", "语言项用母语名（是数据不是文案）"

    # 按钮 / 开关 / 占位文本
    assert win._pause_btn.text() == "Pause"
    assert win._stop_btn.text() == "Stop"
    assert win._library_btn.text() == "Scenes…"
    assert win._reopen_btn.text() == "New session"
    assert win._save_scene_btn.text() == "Save scene"
    assert win._log_btn.text() == "Log"
    assert win._send_btn.text() == "Send"
    assert win._input_edit.placeholderText().startswith("Say something")
    # 未覆盖的键回落 zh-Hans 原文——可见降级，且**绝不是键名**
    assert win._live_check.text() == "真实模型"
    assert win._auto_scene_check.text() == "自动推进"
    assert "toggle." not in win._live_check.text()
    assert win._status_chip.text() == "Ready"
    assert win.windowTitle() == "Multi-Agent Roleplay"

    # 左栏卡片标题：已覆盖的键是英文，未覆盖的键回落中文（可见降级，不是空白/键名）
    left = _left_labels(win)
    assert "Metrics" in left and "Controls" in left and "Scene advance" in left
    assert "运行指标" not in left
    assert "panel.metrics" not in left, "缺键也绝不把键名当文案显示"

    win.apply_language("zh-Hans")            # 还原，别把 app 属性留给后面的测试/模块
    assert win._menu_settings.title() == "设置"


def test_runtime_language_switch_retranslates_in_place_and_back(qapp, tmp_path, tmp_store):
    """运行期切到 ja → 就地重译菜单与按钮（**不重建窗口/部件**）；切回 zh-Hans 逐字复原。"""
    win, _worker, _dirs = _make_window(tmp_path)
    settings_menu, pause_btn = win._menu_settings, win._pause_btn
    assert win._menu_settings.title() == "设置"

    win.apply_language("ja")

    assert win._menu_settings is settings_menu, "只该就地换字，不该重建菜单"
    assert win._pause_btn is pause_btn, "按钮对象也不该被换掉"
    assert win._menu_settings.title() == "設定"
    assert win._menu_scene.title() == "シーン"
    assert win._menu_character.title() == "キャラクター"
    assert [a.text() for a in win._language_menu.actions()] == \
        list(LANGUAGES.values()), "语言项标签恒为母语名（数据，不随语言变）"
    assert win._pause_btn.text() == "一時停止"
    assert win._stop_btn.text() == "停止"
    assert win._save_scene_btn.text() == "シーンを保存"
    assert win._send_btn.text() == "送信"
    assert win._status_chip.text() == "待機中"
    left = _left_labels(win)
    assert "指標" in left and "運行指標" not in left

    # 切回简体中文：逐字回到今天的中文（不是"近似"）
    win.apply_language("zh-Hans")
    assert win._menu_settings.title() == "设置"
    assert win._menu_scene.title() == "场景"
    assert win._menu_character.title() == "角色"
    assert win._menu_settings.actions()[0].text() == "模型 api 配置…"
    assert win._pause_btn.text() == "暂停"
    assert win._stop_btn.text() == "停止"
    assert win._save_scene_btn.text() == "保存场景"
    assert win._log_btn.text() == "日志"
    assert win._send_btn.text() == "发送"
    assert win._library_btn.text() == "场景库…"
    assert win._reopen_btn.text() == "开新场"
    assert win._input_edit.placeholderText() == "说点什么——随时打断，角色会回应…"
    assert win._status_chip.text() == "就绪"
    assert win.windowTitle() == "多智能体角色扮演"
    left = _left_labels(win)
    for text in ("运行指标", "运行控制", "场景推进", "场　景", "在场人数",
                 "边界", "自动推进", "推进一下", "真实模型"):
        assert text in left, text
    assert win._autosave_actions[20].text() == "每 20 轮"


def test_scene_title_keeps_scene_name_and_retranslates_its_suffix(
        qapp, tmp_path, tmp_store):
    """换场后的标题是「场景名 · <标题尾>」：切语言只换尾巴，场景名（数据）一字不动。"""
    win, _worker, _dirs = _make_window(tmp_path)
    win._on_scene_info({"scene": {"name": "餐厅", "participants": ["甲"]},
                        "characters": [], "backend": "stub"})
    assert win.windowTitle() == "餐厅 · 多智能体角色扮演"

    win.apply_language("en")
    assert win.windowTitle() == "餐厅 · Multi-Agent Roleplay", "场景名不改，只换尾缀"

    win.apply_language("zh-Hans")
    assert win.windowTitle() == "餐厅 · 多智能体角色扮演"


def test_status_chip_maps_known_status_and_passes_unknown_through(
        qapp, tmp_path, tmp_store):
    """状态药丸：已知词表项按语言翻，未知（带数字/原因）原样放过——绝不吞 worker 的信息。"""
    win, _worker, _dirs = _make_window(tmp_path)
    assert win._status_chip.text() == "就绪"

    win._on_status("已暂停")
    assert win._status_chip.text() == "已暂停", "zh-Hans 下逐字不变"
    win._on_status("等待中…可随时插话")
    assert win._status_chip.text() == "等待中…可随时插话"

    unknown = "已达 20 块，点继续以续演（防止 token 消耗）"
    win._on_status(unknown)
    assert win._status_chip.text() == unknown, "未知状态原样显示"
    # worker 侧的错误串（可能还带旧口径的记号前缀）→ 显示前摘掉记号，正文一字不改，
    # 且照样按「出错」刷红（界面里不出现任何符号，§3.6）。
    error = "⚠ 开场失败：boom"
    win._on_status(error)
    assert win._status_chip.text() == "开场失败：boom"
    assert "#991b1b" in win._status_chip.styleSheet(), "出错要红字"
    win._on_status("保存场景失败：没有可写的场景文件或写盘出错")
    assert "#991b1b" in win._status_chip.styleSheet(), "新口径（不带记号）也认得出是出错"

    # 切语言：已知状态翻，未知状态保持 worker 原文
    win._on_status("已暂停")
    win.apply_language("en")
    assert win._status_chip.text() == "Paused"
    win._on_status(unknown)
    win.apply_language("en")
    assert win._status_chip.text() == unknown

    win._on_status("已收束")
    win.apply_language("zh-Hans")
    assert win._status_chip.text() == "已收束"


def test_language_switch_refreshes_placeholder_and_tooltips(qapp, tmp_path, tmp_store):
    """切语言不止换 setText：输入框占位符与窗内 tooltip 也要换（并换得回来）。

    占位符与 tooltip 都是**建窗那一刻**设一次的：不登记重译表的话，切到 en 后菜单换英文、
    输入框与悬停提示仍是中文。
    """
    win, _worker, _dirs = _make_window(tmp_path)
    tips = lambda: [win._input_edit.placeholderText(), win._stop_btn.toolTip(),
                    win._log_btn.toolTip(), win._save_scene_btn.toolTip(),
                    win._library_btn.toolTip(), win._auto_scene_check.toolTip(),
                    win._narrate_btn.toolTip(), win._reopen_btn.toolTip()]
    zh = tips()
    assert zh[0] and all(zh[1:]), f"起手每个都该有中文文案：{zh}"

    win.apply_language("en")
    en = tips()
    assert en[0] != zh[0], "输入框占位符应随语言变"
    assert "interrupt" in en[0].lower()
    changed = [i for i, (a, b) in enumerate(zip(zh, en)) if a != b]
    assert len(changed) >= 3, f"至少两个 tooltip 应换英文，实际只换了 {changed}"

    win.apply_language("zh-Hans")
    assert tips() == zh, "切回 zh-Hans 应逐字还原"


def test_auto_pause_is_driven_by_the_flag_not_by_the_status_text(qapp, tmp_path,
                                                                tmp_store):
    """成本守卫自动暂停走**机器可读旗标**，不再靠状态串里有没有「点继续」。

    worker 的可见文案一字不改，但界面判定不再猜中文：文本里出现「点继续」而旗标没来，
    窗口就不该假装暂停；旗标来了（哪怕不带那句文案）窗口就要同步成暂停态。
    """
    win, worker, _dirs = _make_window(tmp_path)

    win._on_status("已达 20 块，点继续以续演（防止 token 消耗）")
    assert win._paused is False, "没有旗标就不是成本守卫暂停（不再靠子串猜）"
    assert win._pause_btn.text() == "暂停"

    worker.sig_auto_paused.emit(True)
    assert win._paused is True, "旗标说暂停了就该同步"
    assert win._pause_btn.text() == "继续"
    win._on_status("已达 20 块，点继续以续演（防止 token 消耗）")
    assert "#fef3c7" in win._status_chip.styleSheet(), "成本守卫暂停用琥珀色药丸"
    assert win._status_chip.text() == "已达 20 块，点继续以续演（防止 token 消耗）"

    worker.sig_auto_paused.emit(False)
    win._on_status("进行中")
    assert win._paused is False
    assert win._pause_btn.text() == "暂停"
    assert "#fef3c7" not in win._status_chip.styleSheet(), "续演后回到普通药丸"


def test_scene_diag_refreshes_the_scene_card_and_surfaces_diagnostics(qapp, tmp_path,
                                                                      tmp_store):
    """sig_scene_diag：场景自改的字段刷新场景卡；钩子/工具错误在日志区可见（G9）。

    场景被 hook/场景工具改过之后（改名/改日期），场景卡不能一直停在开场那一刻的值；
    而 hook/tool/save 的最近诊断（引擎早就报在 scene_state 里）此前没有任何消费者，
    界面上完全看不见。
    """
    win, worker, _dirs = _make_window(tmp_path)
    worker.sig_scene_info.emit({"scene": {"name": "茶室", "participants": ["甲"]},
                                "characters": [], "backend": "stub"})
    assert win._scene_value.text() == "茶室"

    worker.sig_scene_diag.emit({
        "fields": {"name": "雨夜的茶室", "date": "2031-07-09", "background": "bg",
                   "description": "桌椅都撤了", "plot_direction": ""},
        "hooks": ["h1"], "description_mutable": True, "language": "zh-Hans",
        "hook_error": None, "tool_error": "场景工具指令未改动任何字段（set_name）",
        "save_error": None, "implicit_events": []})

    assert win._scene_value.text() == "雨夜的茶室", "场景卡应随最新字段刷新"
    assert win.windowTitle() == "雨夜的茶室 · 多智能体角色扮演"
    assert win._date_value.text() == "2031-07-09", "日期行也应随字段刷新"
    assert "场景工具指令未改动任何字段" in win._log_view.toPlainText(), \
        "诊断应进日志区（此前完全不可见）"
    # §三 改口径：诊断不再抢占顶部状态药丸（那个位置归"进行中/已暂停"这类运行状态），
    # 改由顶部**诊断区**承载——只在日志面板开着时露头（关着时它整块收起，不占位）。
    assert "场景工具指令未改动任何字段" in win._scene_diag_text, \
        "新诊断要即时进诊断区（文本留住，开面板立刻可见）"
    assert win._scene_diag_area.isHidden(), "日志面板关着 → 诊断区不显示"
    win._log_btn.setChecked(True)
    assert not win._scene_diag_area.isHidden()
    assert "场景工具指令未改动任何字段" in win._scene_diag_label.text()


def test_scene_diag_does_not_repeat_the_same_diagnostic(qapp, tmp_path, tmp_store):
    """同一条诊断只提示一次（每块都刷 sig_scene_diag，不能每块都糊一遍日志）。"""
    win, worker, _dirs = _make_window(tmp_path)
    diag = {"fields": {"name": "茶室"}, "hook_error": "钩子 h1 想改不可改的字段：path",
            "tool_error": None, "save_error": None, "implicit_events": []}
    worker.sig_scene_diag.emit(dict(diag))
    first = win._log_view.toPlainText().count("想改不可改的字段")
    worker.sig_scene_diag.emit(dict(diag))
    assert win._log_view.toPlainText().count("想改不可改的字段") == first == 1


def test_language_reaches_the_worker(qapp, tmp_path, tmp_store):
    """构造与切换都把语言码投给 worker（引擎据此注入 LLM 语言指令）。"""
    win, worker, _dirs = _make_window(tmp_path)
    assert "zh-Hans" in worker.languages, "构造即把已保存的语言告诉引擎"
    assert worker.calls == [], "语言不该混进开场/人事调用账本"

    win._language_actions["fr"].trigger()
    assert worker.languages[-1] == "fr"
    assert win._menu_settings.title() == "Paramètres"

    win.apply_language("zh-Hans")            # 还原，别把 app 属性留给后面的模块
    assert worker.languages[-1] == "zh-Hans"


def test_apply_language_survives_a_worker_without_set_language(
        qapp, tmp_path, tmp_store):
    """worker 侧还没落地 set_language 时，切语言只动界面，绝不因缺方法而抛。"""
    win, _worker, _dirs = _make_window(tmp_path)
    win._worker = types.SimpleNamespace(can_cast=lambda: False)   # 没有 set_language
    win.apply_language("ko")
    assert win._menu_settings.title() == "설정"
    assert win._pause_btn.text() == "일시정지"
    win.apply_language("zh-Hans")
    assert win._menu_settings.title() == "设置"


def test_dialogs_built_after_a_switch_use_the_new_language(qapp, tmp_path, tmp_store):
    """弹窗按**打开那一刻**的界面语言出字（主窗口与库里的对话框同一套口径）。"""
    win, _worker, (cdir, _sdir) = _make_window(tmp_path)
    win.apply_language("en")

    picker = CharacterPickerDialog(["甲", "乙"])
    assert picker.ok_btn.text() == "OK"
    assert picker.cancel_btn.text() == "Cancel"

    editor = lib_mod.CharacterEditorDialog(None, cdir)
    assert editor.save_btn.text() == "Save"
    assert editor.cancel_btn.text() == "Cancel"

    win.apply_language("zh-Hans")            # 还原
    picker_zh = CharacterPickerDialog(["甲"])
    assert picker_zh.ok_btn.text() == "确定"
    assert lib_mod.CharacterEditorDialog(None, cdir).save_btn.text() == "保存"
