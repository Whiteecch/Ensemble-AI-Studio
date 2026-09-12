"""「角色」菜单 + 演员表随 sig_cast 实时反应（《场景编排与桌面外壳》§2.1③ / §3.3）。

覆盖四件事：
  · 菜单结构：「角色」下固定为 添加角色 / 移出角色 / 新建角色 / 导入角色… / 管理角色… /
    高级移入·移出…，未开场（can_cast 假）时「添加/移出」两项禁用并给出原因；开场收到
    sig_cast 后可用；
  · 点击接线：添加角色子菜单（排除已在场者）→ worker.add_character；移出角色子菜单 →
    worker.remove_character；高级移入/移出弹窗确定 → worker.schedule_cast_change
    （人名 / 动作 / 回合 / 原因 / 通知对象全按弹窗里选的传，原因留空即今天的行为）；
  · sig_cast 反应：载荷里少了一人 → 左栏冲动行与右栏角色卡当场只剩在场者（被移出者的
    句柄消失）、场景卡在场名单同步；新进场者补一行一卡；禁言者在两侧缀「禁言中（剩 N
    回合）」徽标；
  · 管理角色弹窗：按 cast 载荷建表（姓名/在场 ✓/禁言状态），添加/移出走 worker，编辑用
    库里那张卡开编辑器，删除**先确认**再删磁盘上的卡文件（临时角色目录，不碰真实素材）。

全部离屏（QT_QPA_PLATFORM=offscreen）、确定性、不联网、不起 worker 线程：替身 worker 只
记录调用并提供 MainWindow 连接的信号。若环境未装 PySide6（可选依赖 gui），整模块 skip。
"""
import json
import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QLineEdit  # noqa: E402

from harness.gui import main_window as mw_mod  # noqa: E402
from harness.gui import settings as settings_mod  # noqa: E402
from harness.gui.main_window import (  # noqa: E402
    SCENE_NOTIFY_NAME, AdvancedCastDialog, AppConfig, CharacterManagerDialog,
    CharacterPickerDialog, MainWindow, mute_badge_text)


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


def _card_payload(name: str) -> dict:
    """右栏卡/详情弹窗要的那种载荷（与 worker.build_scene_payload 同形）。"""
    return {"name": name, "personality": {"描述": "冷静"}, "abilities": [],
            "relationships": {}, "weights": {}, "emotion_decay_rate": 0.4,
            "corpus": {}}


def _scene_info(names: list[str]) -> dict:
    """一次 sig_scene_info 载荷（只给 MainWindow 真正读到的字段）。"""
    return {"backend": "stub", "auto_narrate": True,
            "scene": {"name": "茶室", "participants": list(names),
                      "date": "", "hard_boundary": None},
            "start_time": "21:30", "boundary_time": None,
            "characters": [_card_payload(n) for n in names]}


def _cast(active=(), inactive=(), muted=None) -> dict:
    """engine.cast_state() 形状的 sig_cast 载荷。"""
    return {"active": list(active), "inactive": list(inactive),
            "muted": dict(muted or {})}


class _FakeWorker(QObject):
    """替身 worker：记录人事调用 + 提供 MainWindow 连接的信号（不起线程、不碰引擎）。"""

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

    def __init__(self, *, cast_ok: bool = False) -> None:
        super().__init__()
        self.cast_ok = bool(cast_ok)
        self.added: list[str] = []
        self.removed: list[str] = []
        self.muted: list[tuple] = []
        self.unmuted: list[str] = []
        self.scheduled: list[tuple] = []

    # ---- 演员表 API（与 SceneWorker 同名同签名）----
    def can_cast(self) -> bool:
        return self.cast_ok

    def add_character(self, name, notify=None, notify_text="", visible=True) -> None:
        self.added.append(str(name))

    def remove_character(self, name, notify=None, notify_text="", visible=True) -> None:
        self.removed.append(str(name))

    def mute_character(self, name, turns: int = 0) -> None:
        self.muted.append((str(name), int(turns)))

    def unmute_character(self, name) -> None:
        self.unmuted.append(str(name))

    def schedule_cast_change(self, character_name, action, fire_after_rounds,
                             notify=None, notify_text="", visible=True,
                             turns: int = 0, reason: str = "") -> None:
        self.scheduled.append((str(character_name), str(action),
                               int(fire_after_rounds), list(notify or []),
                               str(notify_text), bool(visible), int(turns),
                               str(reason)))

    # ---- MainWindow 也会连/调的那些（本模块用不到，给空实现）----
    def start_scene(self, **kwargs) -> None:  # noqa: D102
        pass

    def restart(self, *a, **k) -> None:  # noqa: D102
        pass

    def say(self, text: str) -> None:  # noqa: D102
        pass

    def can_say(self) -> bool:
        return False

    def set_paused(self, paused: bool) -> None:  # noqa: D102
        pass

    def stop_now(self) -> None:  # noqa: D102
        pass

    def set_pace(self, seconds: float) -> None:  # noqa: D102
        pass

    def set_rate(self, rate: float) -> None:  # noqa: D102
        pass

    def set_auto_narrate(self, on: bool) -> None:  # noqa: D102
        pass

    def narrate_now(self) -> None:  # noqa: D102
        pass

    def retract_message(self, mid: int) -> None:  # noqa: D102
        pass

    def edit_narration(self, mid: int, text: str) -> None:  # noqa: D102
        pass

    def shutdown(self, *a) -> None:  # noqa: D102
        pass

    def isRunning(self) -> bool:  # noqa: N802
        return False


def _make_window(tmp_path: Path, *, cards=("甲", "乙", "丙"),
                 cast_ok: bool = False):
    """真实 MainWindow（替身 worker，不 show、不起线程）+ 临时角色/场景素材。

    返回 (窗口, 替身 worker, 角色库目录)。
    """
    cdir, sdir = tmp_path / "characters", tmp_path / "scenes"
    card_paths = [_write_card(cdir, n) for n in cards]
    sdir.mkdir(parents=True, exist_ok=True)
    scene = sdir / "茶室.json"
    scene.write_text(json.dumps({"name": "茶室", "participants": list(cards)},
                                ensure_ascii=False), encoding="utf-8")
    models = tmp_path / "models.yaml"
    models.write_text("think: {backend: stub, model: stub, params: {}}\n"
                      "speak: {backend: stub, model: stub, params: {}}\n",
                      encoding="utf-8")
    cfg = AppConfig(scene=scene, characters=card_paths, models=models,
                    bid=tmp_path / "bid.yaml", run_root=tmp_path / "runs",
                    live=False, api_key=None, opening="夜里。")
    worker = _FakeWorker(cast_ok=cast_ok)
    win = MainWindow(worker, cfg)
    return win, worker, cdir


def _open_session(win, worker, names=("甲", "乙")):
    """把窗口推进到「已开场且已收到演员表」：先场景信息，再 cast 状态。"""
    worker.cast_ok = True
    worker.sig_scene_info.emit(_scene_info(list(names)))
    worker.sig_cast.emit(_cast(active=names))
    win._refresh_cast_menus()


def _menu_action(menu, text: str):
    for act in menu.actions():
        if act.text() == text:
            return act
    raise AssertionError(f"菜单里没有「{text}」：{[a.text() for a in menu.actions()]}")


# ============================================================ 1. 菜单结构
def test_character_menu_has_exactly_the_spec_items_in_order(qapp, tmp_path):
    """角色菜单七项，顺序即设计文档 §2.1③/§3.3（新增「导入角色…」与「结算待决…」§7.3）。"""
    win, _worker, _cdir = _make_window(tmp_path)
    texts = [a.text() for a in win._menu_character.actions()]
    assert texts == ["添加角色", "移出角色", "新建角色", "导入角色…",
                     "管理角色…", "高级移入/移出…", "结算待决…"]
    assert win._menu_character.title() == "角色"
    assert win._action_import_character.text() == "导入角色…"


def test_add_remove_are_disabled_until_a_scene_is_open(qapp, tmp_path):
    """没开场：添加/移出禁用（并说明原因）；开场 + 收到 sig_cast 后才可用。"""
    win, worker, _cdir = _make_window(tmp_path)

    assert win._action_add_character.isEnabled() is False
    assert win._action_remove_character.isEnabled() is False
    assert win._action_add_character.toolTip(), "禁用必须说明为什么点不了"
    assert win._action_new_character.isEnabled() is True, "新建角色不依赖场次"

    # can_cast 真但**还没收到演员表**（sig_cast 未到）→ 仍禁用（不知道谁在场）。
    worker.cast_ok = True
    win._sync_cast_menu_state()
    assert win._action_add_character.isEnabled() is False

    worker.sig_scene_info.emit(_scene_info(["甲", "乙"]))
    worker.sig_cast.emit(_cast(active=["甲", "乙"]))
    assert win._action_add_character.isEnabled() is True
    assert win._action_remove_character.isEnabled() is True

    # 人全被移出 → 没得移 → 移出项禁用（添加项仍可用）。
    worker.sig_cast.emit(_cast(active=[], inactive=["甲", "乙"]))
    assert win._action_remove_character.isEnabled() is False
    assert win._action_add_character.isEnabled() is True


# =============================================== 2. 子菜单点击 → worker 人事 API
def test_add_character_submenu_excludes_active_and_calls_worker(qapp, tmp_path):
    """添加角色子菜单只列**不在场**的人；点击 → worker.add_character。"""
    win, worker, _cdir = _make_window(tmp_path, cards=("甲", "乙", "丙"))
    _open_session(win, worker, ("甲", "乙"))

    assert [a.text() for a in win._add_char_menu.actions()] == ["丙"], \
        "已在场的甲乙不该出现在「添加角色」里"
    _menu_action(win._add_char_menu, "丙").trigger()
    assert worker.added == ["丙"]


def test_remove_character_submenu_lists_active_and_calls_worker(qapp, tmp_path):
    """移出角色子菜单只列**在场**的人；点击 → worker.remove_character。"""
    win, worker, _cdir = _make_window(tmp_path, cards=("甲", "乙", "丙"))
    _open_session(win, worker, ("甲", "乙"))

    assert [a.text() for a in win._remove_char_menu.actions()] == ["甲", "乙"]
    _menu_action(win._remove_char_menu, "乙").trigger()
    assert worker.removed == ["乙"]

    # 已在场的人全在「添加」里消失、空库给禁用占位（不假装能点）。
    _empty = _make_window(tmp_path / "empty", cards=("甲",))
    _open_session(_empty[0], _empty[1], ("甲",))
    assert [a.text() for a in _empty[0]._add_char_menu.actions()] == \
        ["（角色库里没有可添加的角色）"]
    assert _empty[0]._add_char_menu.actions()[0].isEnabled() is False


def test_advanced_dialog_accept_schedules_cast_change(qapp, tmp_path):
    """高级移入/移出：选人 + 动作 + 回合 + 通知对象多选 → worker.schedule_cast_change。"""
    win, worker, _cdir = _make_window(tmp_path, cards=("甲", "乙", "丙"))
    _open_session(win, worker, ("甲", "乙"))

    dlg = AdvancedCastDialog(worker, ["甲", "乙", "丙"], ["甲", "乙"])
    assert [dlg.name_combo.itemText(i) for i in range(3)] == ["甲", "乙", "丙"]
    assert dlg.rounds_spin.minimum() == 0 and dlg.rounds_spin.maximum() == 99, \
        "回合数 0..99（0 = 立即）"
    assert dlg.add_radio.isChecked() is True, "缺省是「移入」"

    # 通知对象 = 在场角色 + 该角色自己 + 场景（可多选）。
    dlg.name_combo.setCurrentIndex(2)                     # 丙
    dlg._refresh_recipients()
    assert [dlg.recipients.item(i).text() for i in range(dlg.recipients.count())] == \
        ["甲", "乙", "丙", SCENE_NOTIFY_NAME]
    dlg.set_recipients(["甲", "丙", SCENE_NOTIFY_NAME])
    dlg.remove_radio.setChecked(True)
    dlg.rounds_spin.setValue(5)
    dlg.notify_check.setChecked(True)
    dlg.notify_text_edit.setText("丙待会儿走")
    dlg.ok_btn.click()

    assert worker.scheduled == [("丙", "remove", 5, ["甲", "丙", "场景"],
                                 "丙待会儿走", True, 0, "")]
    assert dlg.result() == QDialog.DialogCode.Accepted


def test_advanced_dialog_without_notify_schedules_plain_change(qapp, tmp_path):
    """不勾「通知」→ notify 空表、文案为空（引擎侧也就不会多落通知行）。"""
    _win, worker, _cdir = _make_window(tmp_path)
    dlg = AdvancedCastDialog(worker, ["甲"], [])
    dlg.rounds_spin.setValue(0)                            # 0 = 立即
    dlg.ok_btn.click()
    assert worker.scheduled == [("甲", "add", 0, [], "", True, 0, "")]


# ============================= 2b. 高级移入/移出：原因框（§5.1）
def test_advanced_dialog_has_a_reason_field_that_defaults_to_empty(qapp, tmp_path):
    """原因框：跟 HookEditorDialog.reason_edit 一样——空 = 今天的行为、占位符教用户填什么。

    它同时是「界面上可达」的证据：设计文档 §5.1 点名这个弹窗要能写进离场原因，而
    `HookEditorDialog` 那条路（场景卡里的 hook）走不到「人在场时临时请他走」这个用法。
    """
    _win, worker, _cdir = _make_window(tmp_path)
    dlg = AdvancedCastDialog(worker, ["甲"], ["甲"])

    assert isinstance(dlg.reason_edit, QLineEdit), "部件名固定：reason_edit"
    assert dlg.reason_edit.text() == "", "缺省留空 = 与今天逐字节相同"
    assert dlg.reason_edit.placeholderText().strip(), "占位符要教用户这里填什么"


def test_advanced_dialog_reason_label_says_it_is_private(qapp, tmp_path):
    """原因只交给当事人自己（§5.2）——标签必须讲清「别人看不到」，否则会被当成全场叙述。"""
    _win, worker, _cdir = _make_window(tmp_path)
    dlg = AdvancedCastDialog(worker, ["甲"], ["甲"])
    texts = [lb.text() for lb in dlg.findChildren(QLabel)]
    hit = [s for s in texts if "原因" in s]
    assert hit, f"弹窗里没有原因说明文案：{texts}"
    assert any("别人看不到" in s for s in hit), hit


def test_advanced_dialog_accept_schedules_cast_change_with_reason(qapp, tmp_path):
    """带原因的预约：原因框内容进 worker.schedule_cast_change 的 reason 实参，首尾空白裁掉。"""
    _win, worker, _cdir = _make_window(tmp_path, cards=("甲", "乙"))
    dlg = AdvancedCastDialog(worker, ["甲", "乙"], ["甲"])
    dlg.name_combo.setCurrentIndex(1)                      # 乙
    dlg.remove_radio.setChecked(True)
    dlg.rounds_spin.setValue(3)
    dlg.reason_edit.setText("  去见师父  ")
    dlg.ok_btn.click()

    assert worker.scheduled == [("乙", "remove", 3, [], "", True, 0, "去见师父")]
    assert dlg.result() == QDialog.DialogCode.Accepted


def test_advanced_dialog_blank_reason_keeps_todays_call_shape(qapp, tmp_path):
    """留空即今天的行为：reason 传空串（不是 None、不是缺参），别的实参一字不动。"""
    _win, worker, _cdir = _make_window(tmp_path)
    dlg = AdvancedCastDialog(worker, ["甲"], [])
    dlg.reason_edit.setText("   ")                         # 全是空白 = 没写
    dlg.rounds_spin.setValue(2)
    dlg.ok_btn.click()
    assert worker.scheduled == [("甲", "add", 2, [], "", True, 0, "")]


def test_advanced_dialog_without_cards_warns_and_does_not_schedule(qapp, tmp_path,
                                                                  monkeypatch):
    """角色库为空：弹窗给提示、不发预约（不假装排上了一个不存在的人）。"""
    _win, worker, _cdir = _make_window(tmp_path)
    warned: list = []
    monkeypatch.setattr(mw_mod, "warn", lambda *a, **k: warned.append(a))
    dlg = AdvancedCastDialog(worker, [], [])
    assert dlg.ok_btn.isEnabled() is False
    dlg._on_ok()                                           # 按钮已禁用，直接走处理函数
    assert warned and worker.scheduled == []


def test_advanced_menu_action_opens_dialog_with_library_and_cast(qapp, tmp_path,
                                                                 monkeypatch):
    """「高级移入/移出…」菜单项：弹窗拿到整库名单 + 当前在场名单。"""
    win, worker, _cdir = _make_window(tmp_path, cards=("甲", "乙", "丙"))
    _open_session(win, worker, ("甲", "乙"))
    seen: dict = {}

    class _FakeAdvanced:
        def __init__(self, w, names, active, parent=None):
            seen.update(worker=w, names=list(names), active=list(active),
                        parent=parent)

        def exec(self):
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr(mw_mod, "AdvancedCastDialog", _FakeAdvanced)
    win._action_advanced_cast.trigger()
    assert seen["worker"] is worker
    assert set(seen["names"]) == {"甲", "乙", "丙"}, "下拉列整库（含不在场的人）"
    assert seen["active"] == ["甲", "乙"]
    assert seen["parent"] is win


# ==================================================== 3. sig_cast → 左右栏重画
def test_sig_cast_rebuilds_urge_rows_and_cards_to_the_active_set(qapp, tmp_path):
    """载荷里少了一人 → 左栏冲动行与右栏角色卡只剩在场者；在场名单同步刷新。"""
    win, worker, _cdir = _make_window(tmp_path, cards=("甲", "乙", "丙"))
    _open_session(win, worker, ("甲", "乙", "丙"))
    assert set(win._urge_rows) == {"甲", "乙", "丙"}
    assert set(win._dyn_state) == {"甲", "乙", "丙"}
    cards_before = win._right_cards_layout.count()

    worker.sig_cast.emit(_cast(active=["甲", "丙"], inactive=["乙"]))

    assert set(win._urge_rows) == {"甲", "丙"}, "被移出者的冲动行应消失"
    assert set(win._dyn_state) == {"甲", "丙"}, "被移出者的角色卡应消失"
    assert win._participants_value.text() == "甲、丙"
    assert win._right_cards_layout.count() == cards_before - 1, "右栏应少一张卡"

    # 再进场：当场补一行一卡（新人不在开场载荷里 → 按角色库里的卡现补一份）。
    worker.sig_cast.emit(_cast(active=["甲", "丙", "乙"]))
    assert set(win._urge_rows) == {"甲", "乙", "丙"}
    assert set(win._dyn_state) == {"甲", "乙", "丙"}
    assert win._open_character_detail("乙") is not None, \
        "运行期新加入者的详情来自角色库里的卡（开场载荷没有他）"


def test_sig_cast_muted_shows_badge_on_both_columns(qapp, tmp_path):
    """禁言者：左栏行下与右栏卡上各一条「禁言中（剩 N 回合）」；永久禁言无剩余数。"""
    win, worker, _cdir = _make_window(tmp_path, cards=("甲", "乙"))
    _open_session(win, worker, ("甲", "乙"))
    assert win._urge_rows["甲"]["mute"].isHidden() is True, "未禁言时不显示徽标"

    worker.sig_cast.emit(_cast(active=["甲", "乙"], muted={"甲": 3}))
    assert win._urge_rows["甲"]["mute"].text() == "禁言中（剩 3 回合）"
    assert win._urge_rows["甲"]["mute"].isHidden() is False
    assert win._dyn_state["甲"]["mute"].text() == "禁言中（剩 3 回合）"
    assert win._urge_rows["乙"]["mute"].isHidden() is True, "只有被禁言的人带徽标"

    worker.sig_cast.emit(_cast(active=["甲", "乙"], muted={"甲": None}))
    assert win._urge_rows["甲"]["mute"].text() == "禁言中", "永久禁言不给剩余回合"
    worker.sig_cast.emit(_cast(active=["甲", "乙"]))
    assert win._urge_rows["甲"]["mute"].isHidden() is True, "解禁即收起徽标"


def test_sig_cast_before_scene_info_still_works(qapp, tmp_path):
    """还没收到场景信息就先来 cast：不崩，按在场名单建行建卡（配色现取）。"""
    win, worker, _cdir = _make_window(tmp_path, cards=("甲", "乙"))
    worker.cast_ok = True
    worker.sig_cast.emit(_cast(active=["乙"]))
    assert set(win._urge_rows) == {"乙"}
    assert set(win._dyn_state) == {"乙"}
    assert win._participants_value.text() == "乙"


def test_mute_badge_text_is_pure_and_total():
    """徽标文案：未禁言 = 空；永久 = 「禁言中」；临时 = 带剩余回合。"""
    assert mute_badge_text({}, "甲") == ""
    assert mute_badge_text({"甲": None}, "甲") == "禁言中"
    assert mute_badge_text({"甲": 1}, "甲") == "禁言中（剩 1 回合）"
    assert mute_badge_text({"甲": 12}, "乙") == ""
    assert mute_badge_text(None, "甲") == ""


# ==================================================== 4. 管理角色弹窗
def _manager(tmp_path, **kwargs):
    """装配管理角色弹窗 + 临时角色目录（默认 甲 在场、乙 已移出、甲被禁言 2 块）。"""
    worker = _FakeWorker()
    cdir = tmp_path / "characters"
    for name in kwargs.pop("cards", ("甲", "乙", "丙")):
        _write_card(cdir, name)
    cast = kwargs.pop("cast", _cast(active=["甲"], inactive=["乙"],
                                    muted={"甲": 2}))
    dlg = CharacterManagerDialog(cast, worker, cdir, **kwargs)
    return dlg, worker, cdir


def test_manager_builds_table_from_cast_payload(qapp, tmp_path):
    """表格三列 = 姓名/在场(✓)/禁言状态；在场者在前、已移出者标空「在场」。"""
    dlg, _worker, _cdir = _manager(tmp_path)
    assert [dlg.table.horizontalHeaderItem(i).text() for i in range(3)] == \
        ["姓名", "在场", "禁言状态"]
    assert dlg.table.rowCount() == 2
    assert [dlg.table.item(r, 0).text() for r in range(2)] == ["甲", "乙"]
    assert dlg.table.item(0, 1).text() == "✓"
    assert dlg.table.item(1, 1).text() == ""
    assert dlg.table.item(0, 2).text() == "禁言中（剩 2 回合）"
    assert dlg.table.item(1, 2).text() == "", "禁言随离场清除（引擎口径）"

    # sig_cast 一到即重画（窗口把它喂给弹窗）。
    dlg.update_cast(_cast(active=["乙", "丙"], inactive=["甲"]))
    assert [dlg.table.item(r, 0).text() for r in range(2)] == ["乙", "丙"]
    assert dlg.table.item(0, 1).text() == "✓"


def test_manager_add_uses_picker_over_non_active_names(qapp, tmp_path, monkeypatch):
    """「添加角色」（底部那枚也走同一动作）：候选 = 不在场的人；选中 → worker.add_character。"""
    dlg, worker, _cdir = _manager(tmp_path)
    seen: dict = {}

    class _FakePicker:
        chosen = "丙"

        def __init__(self, names, parent=None, **kwargs):
            seen["names"] = list(names)
            seen["parent"] = parent

        def exec(self):
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(mw_mod, "CharacterPickerDialog", _FakePicker)
    dlg.bottom_add_btn.click()
    assert set(seen["names"]) == {"乙", "丙"}, \
        "只排除**在场**者（甲），已移出的乙仍可再加回来"
    assert worker.added == ["丙"]

    worker.added.clear()
    dlg.add_btn.click()
    assert worker.added == ["丙"], "顶部那枚「添加角色」与底部同一个动作"


def test_manager_remove_and_edit_go_through_worker_and_library(qapp, tmp_path,
                                                               monkeypatch):
    """「移出角色」→ worker.remove_character；「编辑角色」→ 用库里那张卡开编辑器。"""
    dlg, worker, cdir = _manager(tmp_path)
    seen: dict = {}

    class _FakeEditor:
        saved_path = None

        def __init__(self, card=None, characters_dir=None, parent=None,
                     **kw):      # libraries_root（§8.2 订阅区）由主窗口一并传下来
            seen["card"] = card
            seen["dir"] = Path(characters_dir)

        def exec(self):
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr(mw_mod, "CharacterEditorDialog", _FakeEditor)

    dlg.table.selectRow(0)                                  # 选中「甲」
    assert dlg.selected_name() == "甲"
    dlg.edit_btn.click()
    assert seen["card"] is not None and seen["card"].name == "甲", \
        "编辑的应是表格里选中那张卡"
    assert seen["dir"] == cdir

    dlg.remove_btn.click()
    assert worker.removed == ["甲"]

    # 已移出的「乙」不在场 → 不能对他点「移出角色」（按钮禁用，不发出无意义的调用）。
    dlg.table.selectRow(1)
    assert dlg.selected_name() == "乙"
    assert dlg.remove_btn.isEnabled() is False
    dlg.remove_btn.click()
    assert worker.removed == ["甲"], "不在场的人点「移出」不该产生调用"


def test_manager_delete_asks_first_and_removes_the_card_file(qapp, tmp_path,
                                                             monkeypatch):
    """「删除角色」：先确认（默认否），确认后才删磁盘上的卡文件（临时目录）。"""
    dlg, _worker, cdir = _manager(tmp_path)
    card = cdir / "甲.json"
    assert card.exists()
    asked: list = []

    def _no(parent, title, text):
        asked.append((title, text))
        return False

    monkeypatch.setattr(mw_mod, "confirm", _no)
    dlg.table.selectRow(0)
    dlg.delete_btn.click()
    assert asked, "删除必须先弹确认"
    assert "甲" in asked[0][1] and card.exists(), "用户点了否 → 文件必须还在"

    monkeypatch.setattr(mw_mod, "confirm", lambda *a, **k: True)
    dlg.delete_btn.click()
    assert not card.exists(), "确认后才真删文件"
    assert (cdir / "乙.json").exists(), "只删选中的那一张"


def test_manager_edit_without_card_file_warns(qapp, tmp_path, monkeypatch):
    """选中的人在库里没有卡文件（比如已被删）：给提示，不崩、不假装打开了编辑器。"""
    dlg, _worker, cdir = _manager(tmp_path)
    (cdir / "甲.json").unlink()
    warned: list = []
    monkeypatch.setattr(mw_mod, "warn", lambda *a, **k: warned.append(a))
    dlg.table.selectRow(0)
    dlg.edit_btn.click()
    assert warned, "应给一句提示"
    assert "找不到" in warned[0][2]


def test_manager_runs_off_the_main_window_menu(qapp, tmp_path, monkeypatch):
    """「管理角色…」菜单项：弹窗拿到当前 cast 镜像 + worker + 角色库目录。"""
    win, worker, cdir = _make_window(tmp_path, cards=("甲", "乙"))
    _open_session(win, worker, ("甲",))
    seen: dict = {}

    class _FakeManager:
        def __init__(self, cast, w, characters_dir, parent=None, **kw):
            # **kw：libraries_root（§8.2 订阅区）由主窗口一并传下来，替身照收不误。
            seen.update(cast=dict(cast), worker=w,
                        dir=Path(characters_dir), parent=parent)

        def exec(self):
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr(mw_mod, "CharacterManagerDialog", _FakeManager)
    win._action_manage_characters.trigger()
    assert seen["worker"] is worker
    assert seen["dir"] == cdir
    assert seen["cast"]["active"] == ["甲"]
    assert seen["parent"] is win
    assert win._cast_manager is None, "弹窗关掉后窗口不再持有它"


def test_picker_dialog_selects_a_name_and_disables_when_empty(qapp, tmp_path):
    """角色选择器：有名字 → 选中即返回；空库 → 列表与确定禁用（不假装有人可选）。"""
    dlg = CharacterPickerDialog(["甲", "乙"])
    dlg.list.setCurrentRow(1)
    dlg.ok_btn.click()
    assert dlg.chosen == "乙" and dlg.result() == QDialog.DialogCode.Accepted

    empty = CharacterPickerDialog([])
    assert empty.ok_btn.isEnabled() is False
    assert empty.selected_name() is None
