"""T2/T3/T4 界面结构（《界面与场景自由度》§3.3/§3.4/§3.5）——离屏、确定性、不联网。

锁定四件事：
  · **空状态页**（§3.3）：没有打开的场次时中栏是空状态页（一句话 headline + 恰三个居中
    按钮「打开场景」「新建场景」「新建角色」），对白/日志/输入条**存在但收起**；任一条
    打开路径成功开场即换成正常三栏对白界面；`close_scene()` 回到空状态。
  · **场景持有阵容**（§3.1）：打开场景**不再要求先选人**，阵容＝场景文件自己的
    characters/participants 在库里配到的卡；**空阵容照样开得起来**（无人活动而已），
    不再有「请至少选一名角色」那条守门。
  · **配置场景**（§3.4）：开当前场景的编辑器 → 落盘 → 把热更新字段推给
    `worker.apply_scene_config` → 阵容按增删调 worker 的演员表 API → 场景卡当场刷新。
    并行开发期 worker 还没这个方法时，只落盘并**照实说明**（不假装已经热更新）。
  · **导入**（§3.3）：文件选择 → `template_import.import_template_file(角色目录, 场景目录)`
    → 结果框（含「以下字段模板里没填，已用默认值：…」）→ 刷新菜单。
  · **场景变更日志**（§3.5）：`sig_scene_changed(list)` 的每一条在日志窗格里落一行
    「场景变更 · <字段> → <新值>」，**浅红**高亮，且绝不碰对白区。

全部离屏（QT_QPA_PLATFORM=offscreen）、不联网、不碰真实用户设置（默认设置路径一律
monkeypatch 到 tmp_path）。若环境未装 PySide6（可选依赖 gui），整模块 skip。
"""
import json
import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QDialog, QLabel, QPushButton)

from harness.gui import main_window as mw_mod  # noqa: E402
from harness.gui import settings as settings_mod  # noqa: E402
from harness.gui.main_window import (  # noqa: E402
    SCENE_CHANGE_RED_DARK, SCENE_CHANGE_RED_LIGHT, AppConfig, MainWindow,
    scene_change_color, scene_change_text)
from harness.loaders import save_scene  # noqa: E402
from harness.schemas import Scene, SceneCastMember  # noqa: E402
from harness.template_import import ImportResult, TemplateError  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    """模块级复用单一 QApplication（Qt 只允许每进程一个）。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(autouse=True)
def tmp_settings(tmp_path, monkeypatch):
    """默认设置路径指到 tmp：窗口构造不会碰开发者真实用户目录里的 settings.json。"""
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


def _write_scene(directory: Path, name: str, cast: list[str],
                 filename: str | None = None) -> Path:
    """场景文件：cast 写进 `characters`（空表 = 合法的空场，§3.1）。"""
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / (filename or f"{name}.json")
    p.write_text(json.dumps({
        "name": name,
        "characters": [{"name": n} for n in cast],
        "start_time": "21:30"},
        ensure_ascii=False), encoding="utf-8")
    return p


def _card_payload(name: str) -> dict:
    return {"name": name, "personality": {"描述": "冷静"}, "abilities": [],
            "relationships": {}, "weights": {}, "emotion_decay_rate": 0.4,
            "corpus": {}}


def _scene_info(names: list[str], scene_path: Path | None = None) -> dict:
    return {"backend": "stub", "auto_narrate": True,
            "scene": {"name": "茶室", "participants": list(names),
                      "date": "", "hard_boundary": None},
            "scene_path": (str(scene_path) if scene_path is not None else None),
            "start_time": "21:30", "boundary_time": None,
            "characters": [_card_payload(n) for n in names]}


class _FakeWorker(QObject):
    """替身 worker：记账 + 提供 MainWindow 连接的信号（不起线程、不碰引擎）。"""

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
    sig_scene_changed = Signal(list)

    def __init__(self, *, hot_config: bool = True, cast_ok: bool = False) -> None:
        super().__init__()
        self.hot_config = bool(hot_config)     # 有没有 apply_scene_config（并行开发期）
        self.cast_ok = bool(cast_ok)
        self.calls: list[dict] = []
        self.added: list[str] = []
        self.removed: list[str] = []
        self.saved = 0
        self.configs: list[dict] = []

    # ---- 场景配置热更新（§3.4/T3）；hot_config=False 时实例属性被置 None（当作没落地）----
    def apply_scene_config(self, **fields) -> None:
        self.configs.append(dict(fields))

    # ---- 人事 API（§3.1）----
    def add_character(self, name, notify=None, notify_text="", visible=True) -> None:
        self.added.append(str(name))

    def remove_character(self, name, notify=None, notify_text="", visible=True) -> None:
        self.removed.append(str(name))

    def can_cast(self) -> bool:
        return bool(self.cast_ok)

    # ---- MainWindow 会调/会连的其余 worker 面 ----
    def start_scene(self, **kwargs) -> None:
        self.calls.append({"start_scene": kwargs})

    def save_scene(self) -> None:
        self.saved += 1

    def restore_scene(self) -> None:      # noqa: D102
        pass

    def reset_scene(self) -> None:        # noqa: D102
        pass

    def set_language(self, code: str) -> None:    # noqa: D102
        pass

    def set_autosave_every(self, n: int) -> None:  # noqa: D102
        pass

    def set_api_config(self, *a) -> None:          # noqa: D102
        pass

    def restart(self, *a, **k) -> None:            # noqa: D102
        pass

    def say(self, text: str) -> None:              # noqa: D102
        pass

    def can_say(self) -> bool:
        return False

    def set_paused(self, paused: bool) -> None:    # noqa: D102
        pass

    def stop_now(self) -> None:                    # noqa: D102
        pass

    def set_pace(self, seconds: float) -> None:    # noqa: D102
        pass

    def set_rate(self, rate: float) -> None:       # noqa: D102
        pass

    def set_auto_narrate(self, on: bool) -> None:  # noqa: D102
        pass

    def narrate_now(self) -> None:                 # noqa: D102
        pass

    def retract_message(self, mid: int) -> None:   # noqa: D102
        pass

    def edit_narration(self, mid: int, text: str) -> None:  # noqa: D102
        pass

    def shutdown(self, *a) -> None:                # noqa: D102
        pass

    def isRunning(self) -> bool:                   # noqa: N802
        return False


def _make_window(tmp_path: Path, *, cast=("甲", "乙"), cfg_cast: bool = True,
                 hot_config: bool = True):
    """真实 MainWindow（替身 worker，不 show、不起线程）+ 临时素材，返回 (窗口, worker, 目录)。

    cast = 场景文件里声明的阵容；cfg_cast=False 时 AppConfig.characters 留空（模拟
    「空场 + 没有 CLI 素材」这种最极端的情形）。
    """
    cdir, sdir = tmp_path / "characters", tmp_path / "scenes"
    cards = [_write_card(cdir, n) for n in ("甲", "乙", "丙")]
    scene = _write_scene(sdir, "茶室", list(cast))
    cfg = AppConfig(scene=scene, characters=(cards if cfg_cast else []),
                    models=tmp_path / "models.yaml", bid=tmp_path / "bid.yaml",
                    run_root=tmp_path / "runs", live=False, api_key=None,
                    opening="夜里。")
    worker = _FakeWorker(hot_config=hot_config)
    if not hot_config:
        # 实例属性遮蔽类方法 → getattr 取到 None（＝「worker 侧还没落地这个方法」）。
        worker.apply_scene_config = None
    win = MainWindow(worker, cfg)
    return win, worker, (cdir, sdir)


# ============================================================ 1. 空状态页
def test_window_starts_on_the_empty_state_with_exactly_three_buttons(qapp, tmp_path):
    """起手未打开场景：中栏是空状态页，恰三个按钮，会话区（对白/日志/输入条）收起。"""
    win, _worker, _dirs = _make_window(tmp_path)

    assert win._scene_open is False
    assert win._empty_state.isVisibleTo(win), "空状态页应可见"
    assert not win._view.isVisibleTo(win), "对白窗格应收起"
    assert not win._session_area.isVisibleTo(win), "整个会话区（含输入条）应收起"
    assert win._conv_msgs == [] and win._view.toPlainText() == ""

    buttons = win._empty_state.findChildren(QPushButton)
    assert [b.text() for b in buttons] == ["打开场景", "新建场景", "新建角色"], \
        "空状态页恰三个按钮，顺序固定"
    assert all(b.isVisibleTo(win) for b in buttons)
    assert win._empty_open_btn.objectName() == "primary", "只有「打开场景」是主色按钮"
    assert all(b.toolTip() for b in buttons), "三个按钮都要有说明"

    # 居中 = 按钮行两侧各一个弹簧；上方留白（页面开头/结尾都有弹簧）。
    row = win._empty_button_row
    assert row.itemAt(0).spacerItem() is not None
    assert row.itemAt(row.count() - 1).spacerItem() is not None


#: emoji/装饰符号的码位区间（空状态页与日志行都不该出现；中文不在其中）。
_EMOJI_RANGES = ((0x1F300, 0x1FAFF), (0x2600, 0x27BF), (0x2190, 0x21FF),
                 (0x2B00, 0x2BFF), (0xFE0F, 0xFE0F))


def _has_emoji(text: str) -> bool:
    return any(lo <= ord(ch) <= hi for ch in text for lo, hi in _EMOJI_RANGES)


def test_empty_state_speaks_chinese_and_uses_no_emoji(qapp, tmp_path):
    """空状态页文案是短中文 headline + 一句说明，且**不含任何 emoji/符号**（§3.6）。"""
    win, _worker, _dirs = _make_window(tmp_path)
    texts = [lb.text() for lb in win._empty_state.findChildren(QLabel)]
    joined = "\n".join(t for t in texts if t)
    assert "还没有打开场景" in joined, "应有一句短中文 headline"
    assert not _has_emoji(joined), f"空状态页不该有 emoji/符号：{joined}"


def test_opening_a_scene_replaces_the_empty_state_with_the_normal_ui(qapp, tmp_path):
    """开场成功（sig_scene_info）→ 收起空状态页、显出正常三栏对白界面。"""
    win, worker, (_cdir, sdir) = _make_window(tmp_path)
    scene = sdir / "茶室.json"

    worker.sig_scene_info.emit(_scene_info(["甲", "乙"], scene_path=scene))

    assert win._scene_open is True
    assert not win._empty_state.isVisibleTo(win), "开场后空状态页应收起"
    assert win._view.isVisibleTo(win) and win._session_area.isVisibleTo(win)
    assert win._save_scene_btn.isEnabled() and win._configure_scene_btn.isEnabled()


def test_close_scene_brings_the_empty_state_back(qapp, tmp_path):
    """`close_scene()` 回到「未打开场景」：清空对白/日志/演员表镜像，空状态页回来。"""
    win, worker, (_cdir, sdir) = _make_window(tmp_path)
    scene = sdir / "茶室.json"
    worker.sig_scene_info.emit(_scene_info(["甲", "乙"], scene_path=scene))
    worker.sig_message.emit({"id": 1, "speaker": "甲", "speaker_type": "character",
                             "content": "夜里风大。", "time_hhmmss": "21:31:00"})
    assert win._conv_msgs, "开场后应已收到对白"

    win.close_scene()

    assert win._scene_open is False
    assert win._empty_state.isVisibleTo(win) and not win._view.isVisibleTo(win)
    assert win._conv_msgs == [] and win._view.toPlainText() == ""
    assert win._urge_rows == {} and win._dyn_state == {}
    assert not win._save_scene_btn.isEnabled()
    assert not win._configure_scene_btn.isEnabled()
    assert not win._action_save_scene.isEnabled()
    assert win._session_active is False, "没有场次就没有「开新场/切模型重开」"
    assert win.windowTitle() == "Ensemble-AI-Studio · 群像", "标题回到未打开场景那一屏"


def test_empty_state_open_button_goes_through_the_library(qapp, tmp_path, monkeypatch):
    """空状态页「打开场景」→ 场景库弹窗 → 选中即切场（与「管理场景库…」同一条路）。"""
    win, worker, (_cdir, sdir) = _make_window(tmp_path)
    seen: dict = {}

    class _FakeLibrary:
        chosen_scene = sdir / "茶室.json"
        chosen_characters: list = []

        def __init__(self, *a, **k):
            seen["args"] = a

        def exec(self):
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(mw_mod, "LibraryDialog", _FakeLibrary)
    monkeypatch.setattr(mw_mod, "warn",
                        lambda *a, **k: pytest.fail(f"不该有提示框：{a}"))
    win._empty_open_btn.click()

    assert seen["args"], "应真的开了库弹窗"
    assert worker.calls, "选中场景即向 worker 重投开场"
    assert worker.calls[-1]["start_scene"]["scene"] == sdir / "茶室.json"


def test_empty_state_new_scene_and_new_character_buttons_are_wired(
        qapp, tmp_path, monkeypatch):
    """另两个按钮分别接场景编辑器与角色编辑器（走既有路径，不另写一套）。"""
    win, _worker, (cdir, sdir) = _make_window(tmp_path)
    seen: dict = {}

    class _FakeSceneEditor:
        saved_path = None

        def __init__(self, scene=None, scenes_dir=None, characters_dir=None,
                     parent=None):
            seen["scene_editor"] = (scene, Path(scenes_dir), Path(characters_dir))

        def exec(self):
            return QDialog.DialogCode.Rejected

    class _FakeCharEditor:
        saved_path = None

        def __init__(self, card=None, characters_dir=None, parent=None):
            seen["char_editor"] = (card, Path(characters_dir))

        def exec(self):
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr(mw_mod, "SceneEditorDialog", _FakeSceneEditor)
    monkeypatch.setattr(mw_mod, "CharacterEditorDialog", _FakeCharEditor)

    win._empty_new_scene_btn.click()
    assert seen["scene_editor"][0] is None, "新建场景给空白场景"
    assert seen["scene_editor"][1] == sdir and seen["scene_editor"][2] == cdir

    win._empty_new_character_btn.click()
    assert seen["char_editor"][0] is None, "新建角色给空白卡"
    assert seen["char_editor"][1] == cdir


# ==================================================== 2. 打开场景不要求选人
def test_opening_a_scene_with_an_empty_cast_succeeds(qapp, tmp_path, monkeypatch):
    """**空阵容的场景照样开得起来**（§3.3）：不弹「请至少选一名角色」，正常进对白界面。"""
    win, worker, (_cdir, sdir) = _make_window(tmp_path, cast=(), cfg_cast=False)
    scene = sdir / "茶室.json"
    warned: list = []
    monkeypatch.setattr(mw_mod, "warn", lambda *a, **k: warned.append(a))

    assert win.switch_scene(scene) is True
    assert warned == [], "空阵容不该被守门拦下（没有「选择角色」这类提示）"
    assert win._cfg.characters == [], "阵容就是场景自己的（空的）名单，不从库里凑"
    assert worker.calls[-1]["start_scene"]["characters"] == []

    # 开场信息到达（participants 为空）→ 正常三栏界面，冲动卡给「无人在场」占位。
    worker.sig_scene_info.emit(_scene_info([], scene_path=scene))
    assert win._scene_open is True and win._view.isVisibleTo(win)
    assert win._urge_rows == {}
    assert win._participants_value.text() == "—"


def test_opening_a_scene_uses_the_scenes_own_cast_not_the_cfg_cards(
        qapp, tmp_path, monkeypatch):
    """场景文件里的 characters 说了算：cfg 里另有一批卡也不影响本场阵容（§3.1）。"""
    win, worker, (cdir, sdir) = _make_window(tmp_path, cast=("甲",))
    scene = sdir / "茶室.json"
    monkeypatch.setattr(mw_mod, "warn", lambda *a, **k: pytest.fail("不该拦下"))

    assert win.switch_scene(scene) is True
    assert win._cfg.characters == [cdir / "甲.json"], "只按场景声明的「甲」配卡"
    assert worker.calls[-1]["start_scene"]["characters"] == [cdir / "甲.json"]


def test_scene_declaring_a_missing_card_still_warns(qapp, tmp_path, monkeypatch):
    """场景声明的人在库里没有卡 → 照旧拦下并说明是谁（比静默开一个空场清楚）。"""
    win, _worker, (cdir, sdir) = _make_window(tmp_path, cast=("甲", "幽灵"))
    warned: list = []
    monkeypatch.setattr(mw_mod, "warn", lambda *a, **k: warned.append(a))

    assert win.switch_scene(sdir / "茶室.json") is False
    assert warned and "幽灵" in warned[0][2]


# ======================================================== 3. 场景变更日志
def test_scene_change_signal_lands_in_the_log_as_a_light_red_line(qapp, tmp_path):
    """sig_scene_changed → 日志窗格一条浅红「场景变更 · 描述 → …」；对白区一点不动。"""
    win, worker, _dirs = _make_window(tmp_path)
    worker.sig_scene_info.emit(_scene_info(["甲"], scene_path=tmp_path / "scenes/茶室.json"))

    worker.sig_scene_changed.emit([
        {"field": "description", "old": "桌椅都在", "new": "桌椅都撤了", "at": 3},
        {"field": "name", "old": "茶室", "new": "雨夜的茶室", "at": 3}])

    log = win._log_view.toPlainText()
    assert "场景变更 · 描述 → 桌椅都撤了" in log
    assert "场景变更 · 场景名 → 雨夜的茶室" in log
    assert SCENE_CHANGE_RED_LIGHT in win._log_view.toHtml(), "浅红高亮"
    assert win._conv_msgs == [] and win._view.toPlainText() == "", "变更日志不碰对白区"
    # 留存是结构化的（切主题要能按新色重排）——不是渲染后的 HTML 字符串。
    assert [e.get("kind") for e in win._log_entries] == ["change", "change"]


def test_scene_change_line_is_distinct_from_diagnostics_and_blank_values(
        qapp, tmp_path):
    """空值给「（空）」占位；未知字段原样放过；浅红与诊断的琥珀色不同。"""
    win, worker, _dirs = _make_window(tmp_path)
    worker.sig_scene_changed.emit([{"field": "unknown_field", "old": "x", "new": ""}])
    log = win._log_view.toPlainText()
    assert "unknown_field" in log and "（空）" in log, "认不出的字段与空值都要有字可看"
    assert SCENE_CHANGE_RED_LIGHT not in win._pal.warn_text, "浅红不等于诊断色"


def test_implicit_scene_changes_show_up_too(qapp, tmp_path):
    """隐式（visible=False）变更不进对话——日志这几行就是它唯一的可见痕迹。"""
    win, worker, _dirs = _make_window(tmp_path)
    worker.sig_scene_changed.emit([{"field": "description", "old": "", "new": "雨停了",
                                    "visible": False, "at": 7}])
    assert "雨停了" in win._log_view.toPlainText()
    assert win._conv_msgs == [], "隐式变更不该出现在对白区"


def test_scene_change_payload_shapes_are_total(qapp, tmp_path):
    """脏载荷（空表/单条 dict/缺字段/非字典）一律不抛，也不写空行。"""
    win, worker, _dirs = _make_window(tmp_path)
    win._on_scene_changed([])
    win._on_scene_changed(None)
    win._on_scene_changed([{"old": "x"}, "nope", 42])
    win._on_scene_changed({"field": "name", "new": "茶室"})       # 单条 dict 也认
    assert win._log_view.toPlainText().count("场景变更") == 1


def test_scene_change_colour_follows_the_palette(qapp, tmp_path):
    """浅底用 #c2565c、深底用提亮的 #e6929a（按日志底色的明暗选，不看主题名）。"""
    from harness.gui.theme import PALETTES

    assert scene_change_color(PALETTES["默认"]) == SCENE_CHANGE_RED_LIGHT
    assert scene_change_color(PALETTES["白色"]) == SCENE_CHANGE_RED_LIGHT
    assert scene_change_color(PALETTES["深色"]) == SCENE_CHANGE_RED_DARK
    assert scene_change_color(PALETTES["深蓝"]) == SCENE_CHANGE_RED_DARK

    win, _worker, _dirs = _make_window(tmp_path)
    win.apply_theme("深色")
    win._on_scene_changed([{"field": "name", "old": "a", "new": "b"}])
    assert SCENE_CHANGE_RED_DARK in win._log_view.toHtml(), "深色主题下用提亮版浅红"
    assert SCENE_CHANGE_RED_LIGHT not in win._log_view.toHtml()
    win.apply_theme("默认")


def test_scene_change_text_helper_is_pure_and_total():
    """纯函数：布尔/名单/硬边界/空值都给出可读文本，未知字段不吞。"""
    from harness.i18n import Translator

    tr = Translator("zh-Hans")
    assert scene_change_text({"field": "description", "new": "桌椅都撤了"}, tr) == \
        "场景变更 · 描述 → 桌椅都撤了"
    assert scene_change_text({"field": "description_mutable", "new": True}, tr) == \
        "场景变更 · 描述可改变 → 是"
    assert scene_change_text({"field": "characters", "new": ["甲", "乙"]}, tr) == \
        "场景变更 · 出场角色 → 甲、乙"
    assert scene_change_text({"field": "name", "new": ""}, tr) == "场景变更 · 场景名 → （空）"
    assert scene_change_text({"field": "硬边界", "new": "22:00"}, tr) == \
        "场景变更 · 硬边界 → 22:00"


def _worker_class_without(signal_name: str):
    """造一个**没有**某条信号的替身 worker 类（模拟并行开发期尚未落地的信号）。

    信号是类属性：子类删不掉（del 对继承属性会抛），故按 `QObject` + `_FakeWorker` 的
    类属性现造一个同名类（照用它的元类，信号才会正常注册），只是少了那一条。
    """
    meta = type(_FakeWorker)
    namespace = {k: v for k, v in _FakeWorker.__dict__.items()
                 if not k.startswith("__") and k != signal_name}

    def _init(self, *, hot_config: bool = True, cast_ok: bool = False) -> None:
        # 不能直接复用 _FakeWorker.__init__（它的零参 super() 绑在那个类上，换了基类就废）；
        # 这里照抄那几行记账字段，基类初始化直接点 QObject。
        QObject.__init__(self)
        self.hot_config = bool(hot_config)
        self.cast_ok = bool(cast_ok)
        self.calls: list[dict] = []
        self.added: list[str] = []
        self.removed: list[str] = []
        self.configs: list[dict] = []
        self.saved = 0

    namespace["__init__"] = _init
    return meta(f"FakeWorkerWithout_{signal_name}", (QObject,), namespace)


def test_worker_without_the_scene_change_signal_still_builds(qapp, tmp_path):
    """worker 侧还没落地 sig_scene_changed 时，窗口照常构造（防御式连接）。"""
    cls = _worker_class_without("sig_scene_changed")
    assert not hasattr(cls, "sig_scene_changed")
    cfg = AppConfig(scene=tmp_path / "s.json", characters=[], models=tmp_path / "m",
                    bid=tmp_path / "b", run_root=tmp_path / "r", live=False)
    win = MainWindow(cls(), cfg)                      # 不抛即通过
    assert win._scene_open is False


# ============================================================ 4. 导入
def _patch_import(monkeypatch, result=None, error=None):
    """打桩「选文件 + 解析入库 + 结果框」，返回记账用的 seen。"""
    seen: dict = {"imports": [], "infos": [], "warns": []}
    monkeypatch.setattr(mw_mod, "pick_template_file",
                        lambda parent, title, start_dir, filt:
                        seen.update(pick=(title, start_dir, filt)) or "C:/tmp/x.md")

    def _import(path, *, characters_dir, scenes_dir, **kw):
        seen["imports"].append((Path(path), Path(characters_dir), Path(scenes_dir)))
        if error is not None:
            raise error
        return result

    monkeypatch.setattr(mw_mod, "import_template_file", _import)
    monkeypatch.setattr(mw_mod, "info",
                        lambda parent, title, text: seen["infos"].append((title, text)))
    monkeypatch.setattr(mw_mod, "warn",
                        lambda parent, title, text: seen["warns"].append((title, text)))
    return seen


def _character_result(cdir: Path) -> ImportResult:
    return ImportResult(path=cdir / "新角色.json", kind="character", name="新角色",
                        card_or_scene=None, empty_fields=["出场作品", "语料"],
                        warnings=["【性格】只认「是」或「否」，已按「否」处理。"])


def test_import_character_imports_into_the_library_and_shows_the_result(
        qapp, tmp_path, monkeypatch):
    """「导入角色…」→ 文件选择 → import_template_file(角色目录, 场景目录) → 结果框。"""
    win, _worker, (cdir, sdir) = _make_window(tmp_path)
    seen = _patch_import(monkeypatch, result=_character_result(cdir))

    win._action_import_character.trigger()

    assert seen["imports"] == [(Path("C:/tmp/x.md"), cdir, sdir)], \
        "落盘目录必须是**角色库**与场景库两处"
    assert seen["pick"][1] == str(cdir), "文件框从角色库目录起步"
    title, text = seen["infos"][0]
    assert title == "导入完成"
    assert "新角色" in text
    assert "以下字段模板里没填，已用默认值：出场作品、语料" in text, \
        "「模板里没填」的字段必须照实列出来"
    assert "只认「是」或「否」" in text, "解析警告也一并带出"
    assert seen["warns"] == []


def test_import_scene_imports_into_the_library_and_refreshes_the_menu(
        qapp, tmp_path, monkeypatch):
    """「导入场景…」同款流程；成功后场景库子菜单当场刷新，新场景立即可选。"""
    win, _worker, (cdir, sdir) = _make_window(tmp_path)
    new_scene = _write_scene(sdir, "新场", ["甲"], filename="新场.json")
    result = ImportResult(path=new_scene, kind="scene", name="新场",
                          card_or_scene=None, empty_fields=[], warnings=[])
    seen = {"imports": [], "infos": [], "refreshed": 0}

    def _fake_import(path, *, characters_dir, scenes_dir, **kw):
        seen["imports"].append((Path(path), Path(characters_dir), Path(scenes_dir)))
        return result

    monkeypatch.setattr(mw_mod, "import_template_file", _fake_import)
    monkeypatch.setattr(mw_mod, "pick_template_file", lambda *a, **k: "C:/tmp/s.md")
    monkeypatch.setattr(mw_mod, "info",
                        lambda parent, title, text: seen["infos"].append((title, text)))
    real_refresh = win._refresh_scene_menu
    monkeypatch.setattr(win, "_refresh_scene_menu",
                        lambda: (seen.__setitem__("refreshed", seen["refreshed"] + 1),
                                 real_refresh())[1])

    win._action_import_scene.trigger()

    assert seen["imports"] == [(Path("C:/tmp/s.md"), cdir, sdir)]
    assert seen["refreshed"] == 1, "导入后应刷新「打开场景」子菜单"
    assert "新场" in [a.text() for a in win._open_scene_menu.actions()], \
        "刷新后新场景当场可选"
    title, text = seen["infos"][0]
    assert title == "导入完成" and "新场" in text
    assert "以下字段模板里没填" not in text, "模板填全了就不该有那句提示"


def test_import_failure_shows_a_readable_reason_without_raising(qapp, tmp_path,
                                                                monkeypatch):
    """解析失败：只弹一句带行号/字段名的说明，不把 traceback 摊到界面上。"""
    win, _worker, (_cdir, _sdir) = _make_window(tmp_path)
    seen = _patch_import(monkeypatch, error=TemplateError(
        "模板里没有填写【姓名】（角色名不能为空）。", field="姓名", line=7))

    win._action_import_character.trigger()

    assert seen["infos"] == [], "失败时不该说「导入完成」"
    title, text = seen["warns"][0]
    assert title == "导入失败"
    assert "第 7 行附近" in text and "姓名" in text, "行号与字段名都要带上"


def test_import_cancel_does_nothing(qapp, tmp_path, monkeypatch):
    """文件框取消（空路径）→ 不解析、不提示、不刷新。"""
    win, _worker, _dirs = _make_window(tmp_path)
    monkeypatch.setattr(mw_mod, "pick_template_file", lambda *a, **k: "")
    monkeypatch.setattr(mw_mod, "import_template_file",
                        lambda *a, **k: pytest.fail("取消不该开始导入"))
    monkeypatch.setattr(mw_mod, "info", lambda *a, **k: pytest.fail("取消不该弹结果框"))

    win._action_import_scene.trigger()
    win._action_import_character.trigger()


# ============================================================ 5. 配置场景
def _open_scene(win, worker, scene: Path):
    """把窗口推进到「已开场」：先场景信息，再演员表状态。"""
    names = [m["name"] for m in json.loads(scene.read_text(encoding="utf-8"))["characters"]]
    worker.sig_scene_info.emit(_scene_info(names, scene_path=scene))
    worker.sig_cast.emit({"active": names, "inactive": [], "muted": {}})
    worker.cast_ok = True


def _fake_editor_for(scene_path: Path, edited: Scene):
    """替身场景编辑器：exec() 把 edited 落盘（编辑器本来就负责保存）并 accept。"""

    class _FakeEditor:
        def __init__(self, scene=None, scenes_dir=None, characters_dir=None,
                     parent=None, scene_path_arg=None, **kw):
            self.saved_path = None

        def exec(self):
            self.saved_path = save_scene(edited, Path(scene_path))
            return QDialog.DialogCode.Accepted

    return _FakeEditor


def test_configure_scene_pushes_config_and_reconciles_the_cast(qapp, tmp_path,
                                                               monkeypatch):
    """「配置场景」：开当前场景编辑器 → 落盘 → 推热更新字段 + 按增删调人事 API + 刷新场景卡。"""
    win, worker, (cdir, sdir) = _make_window(tmp_path, cast=("甲", "乙"))
    scene = sdir / "茶室.json"
    _open_scene(win, worker, scene)
    assert win._configure_scene_btn.isEnabled()
    assert win._action_configure_scene.isEnabled()

    edited = Scene(name="雨夜的茶室", date="2031-07-09", background="沿海城市",
                   description="桌椅都撤了", description_mutable=True,
                   plot_direction="两人摊牌", start_time="22:10",
                   characters=[SceneCastMember(name="甲"), SceneCastMember(name="丙")])
    monkeypatch.setattr(mw_mod, "SceneEditorDialog", _fake_editor_for(scene, edited))
    win._configure_scene_btn.click()

    # ① 热更新字段整份推给 worker（引擎口径的 8 个字段）
    assert len(worker.configs) == 1
    fields = worker.configs[0]
    assert fields["name"] == "雨夜的茶室" and fields["date"] == "2031-07-09"
    assert fields["description"] == "桌椅都撤了" and fields["description_mutable"] is True
    assert fields["plot_direction"] == "两人摊牌" and fields["start_time"] == "22:10"
    assert set(fields) == {"name", "date", "background", "description",
                           "description_mutable", "plot_direction", "hard_boundary",
                           "start_time"}
    # ② 阵容差集：丙进场、乙离场（同一批人不动）
    assert worker.added == ["丙"] and worker.removed == ["乙"]
    # ③ 场景卡按新配置刷新，且文件确实写回原路径
    assert win._scene_value.text() == "雨夜的茶室"
    assert win._date_value.text() == "2031-07-09" and not win._date_row.isHidden()
    assert win._clock_time["start"].text() == "22:10"
    assert win._status_chip.text() == "已配置场景：雨夜的茶室"
    assert json.loads(scene.read_text(encoding="utf-8"))["name"] == "雨夜的茶室"


def test_configure_scene_reports_when_the_worker_cannot_hot_update(
        qapp, tmp_path, monkeypatch):
    """worker 还没落地 apply_scene_config：文件照旧落盘，但状态**照实说明**没热更新。"""
    win, worker, (_cdir, sdir) = _make_window(tmp_path, cast=("甲",), hot_config=False)
    scene = sdir / "茶室.json"
    _open_scene(win, worker, scene)

    edited = Scene(name="改过的名", characters=[SceneCastMember(name="甲")])
    monkeypatch.setattr(mw_mod, "SceneEditorDialog", _fake_editor_for(scene, edited))
    win._configure_scene_btn.click()

    assert worker.configs == [], "没有这个方法就不该被调用"
    assert json.loads(scene.read_text(encoding="utf-8"))["name"] == "改过的名"
    assert win._status_chip.text() == "已保存场景文件：改过的名（本次运行暂未热更新）"


def test_configure_scene_is_disabled_without_an_open_scene(qapp, tmp_path, monkeypatch):
    """没有打开的场次：按钮与菜单项都禁用；直接调处理函数只给提示，不开编辑器。"""
    win, _worker, _dirs = _make_window(tmp_path)
    assert not win._configure_scene_btn.isEnabled()
    assert not win._action_configure_scene.isEnabled()
    monkeypatch.setattr(mw_mod, "SceneEditorDialog",
                        lambda *a, **k: pytest.fail("没场次不该开编辑器"))

    win._on_configure_scene()
    assert win._status_chip.text() == "还没有打开场景：先在「场景」菜单里打开或新建一个场景。"


def test_configure_scene_without_a_scene_file_says_so(qapp, tmp_path, monkeypatch):
    """场次开着但没有可写的场景文件（sig_scene_info 的 scene_path 为空）→ 照实说明。"""
    win, worker, _dirs = _make_window(tmp_path)
    worker.sig_scene_info.emit(_scene_info(["甲"], scene_path=None))
    assert win._configure_scene_btn.isEnabled(), "场次开着按钮就可用（点了会说明原因）"
    monkeypatch.setattr(mw_mod, "SceneEditorDialog",
                        lambda *a, **k: pytest.fail("没落盘就不该开编辑器"))

    win._configure_scene_btn.click()
    assert win._status_chip.text() == "当前场景未落盘，无法配置"


def test_configure_scene_editor_gets_the_current_scene_file(qapp, tmp_path, monkeypatch):
    """编辑器拿到的是**当前场景**（含该文件坐标），不是空白场景。"""
    win, worker, (_cdir, sdir) = _make_window(tmp_path, cast=("甲",))
    scene = sdir / "茶室.json"
    _open_scene(win, worker, scene)
    seen: dict = {}

    class _FakeEditor:
        saved_path = None

        def __init__(self, scene_obj=None, scenes_dir=None, characters_dir=None,
                     parent=None, scene_path=None):
            seen["scene"] = scene_obj
            seen["scene_path"] = scene_path
            seen["scenes_dir"] = Path(scenes_dir)
            seen["characters_dir"] = Path(characters_dir)

        def exec(self):
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr(mw_mod, "SceneEditorDialog", _FakeEditor)
    win._configure_scene_btn.click()

    assert seen["scene"].name == "茶室", "应是当前打开的那个场景"
    assert Path(seen["scene_path"]) == scene, "要带上当前场景文件坐标（保存写回同一文件）"
    assert seen["scenes_dir"] == sdir and seen["characters_dir"] == sdir.parent / "characters"
    assert worker.configs == [], "取消编辑不该推任何配置"


def test_configure_scene_cancel_pushes_nothing(qapp, tmp_path, monkeypatch):
    """取消编辑：不推配置、不动阵容、不改状态。"""
    win, worker, (_cdir, sdir) = _make_window(tmp_path, cast=("甲", "乙"))
    _open_scene(win, worker, sdir / "茶室.json")
    before = win._status_chip.text()

    class _Cancel:
        saved_path = None

        def __init__(self, *a, **k):
            pass

        def exec(self):
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr(mw_mod, "SceneEditorDialog", _Cancel)
    win._configure_scene_btn.click()

    assert worker.configs == [] and worker.added == [] and worker.removed == []
    assert win._status_chip.text() == before
