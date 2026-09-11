"""界面侧两件事（优化文档 §6.1 / §6.2）：**推进活跃度可调** + **撤销/改写前先确认**。

本模块只锁界面契约（离屏、替身 worker、不起线程、不联网）：

§6.2 推进活跃度
  · 左栏「场景推进」卡多一个四档下拉（少 / 中 / 多 / 极多 ↔ 0.2 / 0.5 / 0.8 / 1.0），
    默认「中」；档位标签的 i18n 键在 zh-Hans 主目录里（其余六种语言见 test_i18n.py）；
  · 改档 → 落盘（SettingsStore.update(narrate_activity=…)）+ 立刻投
    `worker.set_narrate_activity(档位数值)`（防御式 getattr：替身 worker 没有这个方法
    也必须活得好好的）；
  · 启动读设置：存了「极多」→ 控件选中「极多」且开工前就投了 1.0；
  · worker 的叙述状态载荷带了当前活跃度 → 控件按它对齐（不落盘、不回头再投，避免打转）。

§6.1 撤回/改写前的确认
  · `scene:undo:<id>` / `scene:edit:<id>` 都会**回溯整段下文**，故两者动手前都要过
    `library.confirm`（测试一律打桩——真模态在离屏下会挂死整个进程）；
  · 「取消」= 零动作：不摘行、不投递、不留半个改动；确认文案必须点明「它之后的所有内容」；
  · 撤回/改写让对白区**从该条起截断**：sig_retracted 只报一个 id，回溯丢掉的整段尾巴
    也必须当场从屏上消失。
若环境未装 PySide6（可选依赖 gui），整模块 skip。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from harness.gui import main_window as mw_mod  # noqa: E402
from harness.gui.main_window import (  # noqa: E402
    NARRATE_ACTIVITY_LEVELS, AppConfig, MainWindow)
from harness.gui.settings import (  # noqa: E402
    NARRATE_ACTIVITIES, AppSettings, SettingsStore)

#: 四档标签（zh-Hans 主目录）→ 档位数值：界面契约的**期望**（不是实现里的那份映射）。
EXPECTED_LEVELS = (("少", 0.2), ("中", 0.5), ("多", 0.8), ("极多", 1.0))

_NARRATION = "隔壁桌有人站起来，椅子在瓷砖上刮出一声。"
_EDITED = "服务员把账单压在杯底，又走开了。"


@pytest.fixture(scope="module")
def qapp():
    """模块级复用单一 QApplication（Qt 只允许每进程一个）。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _FakeWorker(QObject):
    """替身 worker：只记账 + 提供 MainWindow 连接的信号（不起线程、不碰引擎）。

    **故意不实现 set_narrate_activity**——接口是并行开发的，界面必须 getattr 防御式地接；
    需要它的用例用 `_ActivityWorker`（见下）。
    """

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

    def __init__(self) -> None:
        super().__init__()
        self.narrate_activity: list[float] = []   # set_narrate_activity 的实参（按序）
        self.retracted: list[int] = []            # retract_message 的实参（按序）
        self.edits: list[tuple[int, str]] = []    # edit_narration 的实参（按序）
        self.auto: list[bool] = []

    # ---- MainWindow 构造/运行会用到的 worker 面 ----
    def can_cast(self) -> bool:
        return False

    def can_say(self) -> bool:
        return False

    def start_scene(self, **kwargs) -> None:  # noqa: D102
        pass

    def restart(self, *a, **k) -> None:  # noqa: D102
        pass

    def set_auto_narrate(self, on: bool) -> None:
        self.auto.append(bool(on))

    def narrate_now(self) -> None:  # noqa: D102
        pass

    def retract_message(self, mid: int) -> None:
        self.retracted.append(int(mid))

    def edit_narration(self, mid: int, text: str) -> None:
        self.edits.append((int(mid), str(text)))

    def shutdown(self, *a) -> None:  # noqa: D102
        pass

    def isRunning(self) -> bool:                  # noqa: N802
        return False


class _ActivityWorker(_FakeWorker):
    """会记账 set_narrate_activity 的替身（对应并行实现的那个接口）。"""

    def set_narrate_activity(self, level: float) -> None:
        self.narrate_activity.append(float(level))


def _window(tmp_path: Path, worker=None, *, settings: AppSettings | None = None,
            saved: AppSettings | None = None):
    """装配好的离屏窗口 + 一份**临时**设置文件（绝不动用户真实 settings.json）。

    saved 给了就先落盘一份设置，再让窗口从它启动（模拟「上次调过」）。
    """
    store = SettingsStore(tmp_path / "settings" / "settings.json")
    if saved is not None:
        store.save(saved)
    worker = worker if worker is not None else _FakeWorker()
    cfg = AppConfig(scene=Path("scenes/餐厅.json"),
                    characters=[Path("characters/甲.json")],
                    models=Path("config/models.yaml"), bid=Path("config/bid.yaml"),
                    run_root=tmp_path / "runs", live=False, api_key=None,
                    opening="入夜。")
    win = MainWindow(worker, cfg, settings=settings, store=store)
    return win, worker, store


def _labels(win: MainWindow) -> list[str]:
    """下拉里的四个档位标签（当前语言）。"""
    return [win._activity_combo.itemText(i) for i in range(win._activity_combo.count())]


def _levels(win: MainWindow) -> list[float]:
    """下拉里每个条目挂的档位数值（QComboBox itemData）。"""
    return [win._activity_combo.itemData(i) for i in range(win._activity_combo.count())]


def _narrator(mid: int, text: str = _NARRATION) -> dict:
    return {"id": mid, "speaker": "场景", "speaker_type": "narrator",
            "content": text, "time_hhmmss": "21:31:02"}


# ==================================================== §6.2 活跃度控件
def test_activity_combo_has_four_labelled_levels_defaulting_to_mid(qapp, tmp_path):
    """四档标签齐全、各自挂着 0.2/0.5/0.8/1.0，默认选中「中」。"""
    win, _worker, _store = _window(tmp_path)
    assert _labels(win) == [label for label, _ in EXPECTED_LEVELS], \
        "下拉应是 少/中/多/极多 四档"
    assert _levels(win) == [level for _, level in EXPECTED_LEVELS], \
        "每档必须挂到对应的数值上"
    assert win._activity_combo.currentText() == "中"
    assert win._activity_combo.currentData() == 0.5
    # 映射只有一处：界面的四档与设置里认的四档是**同一组**数值。
    assert tuple(level for _, level in NARRATE_ACTIVITY_LEVELS) == NARRATE_ACTIVITIES


def test_activity_change_persists_and_pushes_to_worker(qapp, tmp_path):
    """改档 → 落盘 + 投 worker.set_narrate_activity（按下标取映射里的数值）。"""
    win, worker, store = _window(tmp_path, _ActivityWorker())
    assert worker.narrate_activity == [0.5], "启动应把设置里的档位推一次给 worker"

    win._activity_combo.setCurrentIndex(3)                # 极多
    assert worker.narrate_activity[-1] == 1.0, "改档应立刻投给 worker"
    assert store.load().narrate_activity == 1.0, "改档应落盘"

    win._activity_combo.setCurrentIndex(0)                # 少
    assert worker.narrate_activity[-1] == 0.2
    assert store.load().narrate_activity == 0.2
    assert worker.auto == [] and worker.retracted == [], "只该动活跃度，不该顺手投别的"


def test_startup_applies_saved_max_level(qapp, tmp_path):
    """设置里存了「极多」→ 开工前就投 1.0，且控件选中「极多」。"""
    win, worker, _store = _window(tmp_path, _ActivityWorker(),
                                 saved=AppSettings(narrate_activity=1.0))
    assert worker.narrate_activity == [1.0], "启动应把保存的档位推给 worker"
    assert win._narrate_activity == 1.0
    assert win._activity_combo.currentText() == "极多"
    assert win._activity_combo.currentData() == 1.0


def test_startup_snaps_saved_out_of_range_level(qapp, tmp_path):
    """设置文件里的脏档位（0.83）在窗口侧同样吸附到最近档（0.8 = 多）后投出去。"""
    win, worker, _store = _window(tmp_path, _ActivityWorker(),
                                  saved=AppSettings(narrate_activity=0.83))
    assert worker.narrate_activity == [0.8]
    assert win._activity_combo.currentText() == "多"


def test_worker_without_activity_api_is_tolerated(qapp, tmp_path):
    """并行实现还没落地（替身 worker 没有 set_narrate_activity）时界面照样能用。"""
    win, _worker, store = _window(tmp_path)          # _FakeWorker 故意没有这个方法
    win._activity_combo.setCurrentIndex(2)           # 多
    assert win._narrate_activity == 0.8
    assert store.load().narrate_activity == 0.8, "接口缺失也不该妨碍落盘"


def test_narration_payload_mirrors_activity_without_echoing_back(qapp, tmp_path):
    """worker 载荷带了当前活跃度 → 控件按它对齐；**不落盘、不回头再投**（不打转）。"""
    win, worker, store = _window(tmp_path, _ActivityWorker())
    worker.narrate_activity.clear()

    win._on_narration({"auto": True, "activity": 0.2})
    assert win._activity_combo.currentText() == "少"
    assert win._narrate_activity == 0.2
    assert worker.narrate_activity == [], "镜像不是新指令，不能反手再投一次"
    assert store.load().narrate_activity == 0.5, "镜像不改设置文件（用户在设置里选的那档）"

    # 旧 worker 的载荷没有这个字段 → 维持原样（不崩、不乱改）。
    win._on_narration({"auto": True})
    assert win._activity_combo.currentText() == "少"
    # 脏值（非数字）同样只忽略，不动控件。
    win._on_narration({"activity": "很多"})
    assert win._activity_combo.currentText() == "少"


def test_activity_combo_labels_retranslate_on_language_switch(qapp, tmp_path):
    """切语言后四档标签就地重译（下标与数值不变，仍指向同一档）。"""
    win, _worker, _store = _window(tmp_path)
    win.apply_language("en")
    assert _labels(win) == ["Low", "Mid", "High", "Max"]
    assert _levels(win) == [0.2, 0.5, 0.8, 1.0], "重译只换字，不换档位数值"
    win.apply_language("zh-Hans")
    assert _labels(win) == ["少", "中", "多", "极多"]


# ==================================================== §6.1 撤回/改写前确认
def _seed_conversation(win: MainWindow) -> list[dict]:
    """屏上造一段：叙述(10) → 角色(11) → 叙述(12) → 角色(13)（顺序即 id 顺序）。"""
    for m in ({"id": 9, "speaker": "甲", "speaker_type": "character",
               "content": "这家店我常来。"},
              _narrator(10),
              {"id": 11, "speaker": "乙", "speaker_type": "character",
               "content": "是吗。"},
              _narrator(12, "侍者走过来，把窗子关上了。"),
              {"id": 13, "speaker": "甲", "speaker_type": "character",
               "content": "起风了。"}):
        win._on_message(m)
    return list(win._conv_msgs)


def test_undo_confirms_and_mentions_the_truncation(qapp, tmp_path, monkeypatch):
    """撤销：确认文案必须点明「它之后的所有内容」；确认后投 worker 撤回该 id。"""
    win, worker, _store = _window(tmp_path)
    _seed_conversation(win)
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(mw_mod, "confirm",
                        lambda parent, title, text: seen.append((title, text)) or True)

    win._on_anchor_clicked(mw_mod.QUrl("scene:undo:10"))
    assert worker.retracted == [10], "确认后应把撤销投给 worker"
    assert seen, "撤销前必须先确认"
    _title, text = seen[0]
    assert "它之后的所有内容" in text, "确认文案必须说清回溯会丢弃什么"


def test_undo_cancelled_changes_nothing(qapp, tmp_path, monkeypatch):
    """取消撤销：不投递、不摘行——对白区一个字符都不动。"""
    win, worker, _store = _window(tmp_path)
    before = _seed_conversation(win)
    text_before = win._view.toPlainText()
    monkeypatch.setattr(mw_mod, "confirm", lambda *a, **k: False)

    win._on_anchor_clicked(mw_mod.QUrl("scene:undo:10"))
    assert worker.retracted == [], "取消后不得投递撤销"
    assert win._conv_msgs == before, "取消后本地留存应原样"
    assert win._view.toPlainText() == text_before, "取消后对白区不得变样"


def test_undo_truncates_the_tail_from_that_point(qapp, tmp_path, monkeypatch):
    """撤销是回溯式：该条**及其之后**的行（含人类气泡）全部消失，之前的原样保留。"""
    win, worker, _store = _window(tmp_path)
    _seed_conversation(win)
    win._append_human("我先走了。", "21:31:10")      # 人类气泡（无 id）也在尾巴里
    monkeypatch.setattr(mw_mod, "confirm", lambda *a, **k: True)

    win._on_anchor_clicked(mw_mod.QUrl("scene:undo:10"))
    assert [m.get("id") for m in win._conv_msgs] == [9], "该条与其后的全部行都该消失"
    assert worker.retracted == [10]
    plain = win._view.toPlainText()
    assert _NARRATION not in plain and "起风了。" not in plain and "我先走了。" not in plain
    assert "这家店我常来。" in plain, "该条**之前**的行不受影响"


def test_retract_signal_drops_that_id_and_keeps_later_reposts(qapp, tmp_path):
    """sig_retracted 只按 id 摘那一条：**不做位置截断**（改写会重新落一行更大的 id）。"""
    win, _worker, _store = _window(tmp_path)
    _seed_conversation(win)
    win._worker.sig_retracted.emit(12)          # 撤回中间那条（12）
    assert [m.get("id") for m in win._conv_msgs] == [9, 10, 11, 13], \
        "只摘被点的那条：13 未收到作废通知就留在屏上（改写重落的新行同理不能误杀）"
    assert "侍者走过来" not in win._view.toPlainText()
    assert "起风了。" in win._view.toPlainText()


def test_reconcile_drops_tail_and_orphan_human_bubbles(qapp, tmp_path):
    """worker 叙述载荷里的**权威作废集**：整段尾巴（含写在其上的人类气泡）一并摘掉。"""
    win, _worker, _store = _window(tmp_path)
    _seed_conversation(win)
    win._append_human("我先走了。", "21:31:10")     # 气泡无 id，after_id = 13（前一条有 id 的行）
    win._append_human("路上小心。", "21:31:11")

    win._on_narration({"auto": True, "retracted": [10, 11, 12, 13]})
    assert [m.get("id") for m in win._conv_msgs] == [9], "作废集里的行与其后的气泡都该消失"
    plain = win._view.toPlainText()
    assert _NARRATION not in plain and "起风了。" not in plain
    assert "我先走了。" not in plain and "路上小心。" not in plain

    # 反向：改写后**重新落**的新行（id 更大、不在作废集里）不受影响。
    win._on_message(_narrator(14, _EDITED))
    win._append_human("那就这样。", "21:31:20")
    win._on_narration({"auto": True, "retracted": [10, 11, 12, 13]})
    assert [m.get("id") for m in win._conv_msgs] == [9, 14, None], \
        "新行与它之后的插话都得留着（末条 None = 人类气泡）"
    assert _EDITED in win._view.toPlainText() and "那就这样。" in win._view.toPlainText()


class _FakeInput:
    """打桩的多行输入框：记下预填文本，返回脚本化结果（None=取消）。"""
    result: str | None = _EDITED
    seen: list[tuple[str, str]] = []

    @classmethod
    def getMultiLineText(cls, parent, title, label, text="", **kw):
        cls.seen.append((title, text))
        return (cls.result, True) if cls.result is not None else ("", False)


def test_edit_flow_confirms_then_applies(qapp, tmp_path, monkeypatch):
    """改写：输入框（预填原文）→ 确认（文案点明回溯）→ 才把改写投给 worker。"""
    win, worker, _store = _window(tmp_path)
    _seed_conversation(win)
    monkeypatch.setattr(mw_mod, "QInputDialog", _FakeInput)
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(mw_mod, "confirm",
                        lambda parent, title, text: seen.append((title, text)) or True)
    _FakeInput.seen = []
    _FakeInput.result = _EDITED

    win._on_anchor_clicked(mw_mod.QUrl("scene:edit:10"))
    assert _FakeInput.seen and _FakeInput.seen[0][1] == _NARRATION, "输入框应预填原文"
    assert worker.edits == [(10, _EDITED)], "确认后应把改写投给 worker"
    assert seen and "它之后的所有内容" in seen[0][1], "改写同样要说清会丢弃下文"
    assert [m.get("id") for m in win._conv_msgs] == [9], \
        "改写也是回溯：该条与其后的行就地消失（新叙述由 worker 另行派发）"


def test_edit_flow_cancelled_changes_nothing(qapp, tmp_path, monkeypatch):
    """改写的确认框选「取消」：不截断、不投递——原文与下文一行不动。"""
    win, worker, _store = _window(tmp_path)
    before = _seed_conversation(win)
    monkeypatch.setattr(mw_mod, "QInputDialog", _FakeInput)
    monkeypatch.setattr(mw_mod, "confirm", lambda *a, **k: False)
    _FakeInput.seen = []
    _FakeInput.result = _EDITED

    win._on_anchor_clicked(mw_mod.QUrl("scene:edit:10"))
    assert worker.edits == [], "取消后不得投递改写"
    assert win._conv_msgs == before, "取消后本地留存应原样（含被回溯过的尾巴）"
    assert _NARRATION in win._view.toPlainText()
    assert "起风了。" in win._view.toPlainText()


def test_edit_confirms_exactly_once(qapp, tmp_path, monkeypatch):
    """改写只确认**一次**：说清代价即可，不该一道接一道地烦人。"""
    win, worker, _store = _window(tmp_path)
    _seed_conversation(win)
    monkeypatch.setattr(mw_mod, "QInputDialog", _FakeInput)
    calls: list[int] = []
    monkeypatch.setattr(mw_mod, "confirm", lambda *a, **k: calls.append(1) or True)
    _FakeInput.seen = []
    _FakeInput.result = _EDITED

    win._on_anchor_clicked(mw_mod.QUrl("scene:edit:10"))
    assert worker.edits == [(10, _EDITED)], "确认后应把改写投给 worker"
    assert len(calls) == 1, "改写只有一次确认（输入框 + 一次回溯警告）"


def test_edit_blank_text_rejected_without_any_confirm(qapp, tmp_path, monkeypatch):
    """空/纯空白：给提示就结束——连确认框都不该弹（没什么可确认的）。"""
    win, worker, _store = _window(tmp_path)
    _seed_conversation(win)
    monkeypatch.setattr(mw_mod, "QInputDialog", _FakeInput)
    calls: list[int] = []
    monkeypatch.setattr(mw_mod, "confirm",
                        lambda *a, **k: calls.append(1) or True)
    _FakeInput.result = "   \n\t "
    win._on_anchor_clicked(mw_mod.QUrl("scene:edit:10"))
    assert worker.edits == [] and calls == [], "空白改写应就地拒绝，不投递也不确认"
    assert [m.get("id") for m in win._conv_msgs] == [9, 10, 11, 12, 13]


def test_edit_of_a_line_already_gone_is_a_noop(qapp, tmp_path, monkeypatch):
    """该行已不在屏上（被撤回/换场）→ 不弹输入框、不投递（无从改写）。"""
    win, worker, _store = _window(tmp_path)
    _seed_conversation(win)
    monkeypatch.setattr(mw_mod, "QInputDialog", _FakeInput)
    monkeypatch.setattr(mw_mod, "confirm", lambda *a, **k: True)
    _FakeInput.seen = []
    _FakeInput.result = _EDITED
    win._on_anchor_clicked(mw_mod.QUrl("scene:edit:404"))
    assert _FakeInput.seen == [] and worker.edits == []


def test_settings_file_round_trip_for_activity_level(qapp, tmp_path):
    """窗口改档写的就是那份设置文件（narrate_activity 落成 JSON 数字，重开仍在）。"""
    win, _worker, store = _window(tmp_path)
    win._activity_combo.setCurrentIndex(2)              # 多
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    assert raw["narrate_activity"] == 0.8
    assert SettingsStore(store.path).load().narrate_activity == 0.8
    # 换一个窗口读同一份设置（= 重启）→ 选中「多」。
    again, worker, _ = _window(tmp_path, _ActivityWorker(), settings=None)
    assert again._activity_combo.currentText() == "多"
    assert worker.narrate_activity == [0.8]
