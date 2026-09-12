"""散场结算的**接线**（设计文档 §7.2 / §7.3 / §7.4 / §8.3）——离屏、确定性、不起线程。

钉住这条链的每一跳：

  · 引擎待决清单（`settlement_rows` / `settlement_warnings`）→ worker 的
    `sig_settlement_pending` → **主线程**开窗：清单与警告照抄，弹窗这一侧一条都不自己算；
  · 没有待决内容 → **不发信号、不开窗**（绝不打扰）；
  · 用户决定 → `worker.apply_settlement` → 引擎 loop 上的 `engine.apply_settlement`
    （GUI 线程绝不直接碰引擎）；
  · 离场挂起（§7.3）：来的是**非打断**提示（日志 + 状态区），绝不弹窗；「角色」菜单里
    的「结算待决…」随之可用，点了能开同一张结算窗；
  · 无信息库 / 无待决时：既有信号序列与今天**逐字节相同**（新信号一个都不发）。

全部离屏（QT_QPA_PLATFORM=offscreen）、**不真起 worker 线程**（替身钉住每一跳）、
不碰真实用户设置。若环境未装 PySide6（可选依赖 gui），整模块 skip。
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from harness import knowledgesettle as settle_mod  # noqa: E402
from harness import knowledgestore as store  # noqa: E402
from harness.backends.stub import StubBackend  # noqa: E402
from harness.engine import SceneEngine  # noqa: E402
from harness.gui import main_window as mw_mod  # noqa: E402
from harness.gui.main_window import AppConfig, MainWindow  # noqa: E402
from harness.gui.worker import SceneWorker  # noqa: E402
from harness.knowledge import Entry, EntryOrigin  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    """模块级复用单一 QApplication（Qt 只允许每进程一个）。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


# --------------------------------------------------------------------- 素材
def _write_card(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / f"{name}.json"
    p.write_text(json.dumps({"name": name, "personality": {"描述": "示例"}},
                            ensure_ascii=False), encoding="utf-8")
    return p


def _scene_info(names: list[str]) -> dict:
    return {"backend": "stub", "auto_narrate": True,
            "scene": {"name": "茶室", "participants": list(names),
                      "date": "", "hard_boundary": None},
            "scene_path": None, "start_time": "21:30", "boundary_time": None,
            "characters": [{"name": n, "personality": {"描述": "冷静"}, "abilities": [],
                            "relationships": {}, "weights": {}, "emotion_decay_rate": 0.4,
                            "corpus": {}} for n in names]}


def _row(name: str, *, scene: str = "茶室", added: int = 2, revised: int = 1,
         titles: list[str] | None = None) -> dict:
    return {"name": name, "scene": scene, "added": int(added), "revised": int(revised),
            "titles": list(titles if titles is not None else ["药铺的暗格"])}


class _FakeWorker(QObject):
    """替身 worker：提供主窗口要连的信号 + 记账（不起线程、不碰引擎）。"""

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
    sig_settlement_pending = Signal(dict)
    sig_settlement_hint = Signal(dict)
    sig_settlement_applied = Signal(dict)

    def __init__(self) -> None:
        super().__init__()
        self.decisions: list[dict] = []

    def apply_settlement(self, decisions) -> None:
        self.decisions.append(dict(decisions))

    def can_cast(self) -> bool:
        return False

    def can_say(self) -> bool:
        return False

    def start_scene(self, **kwargs) -> None:
        pass

    def set_language(self, code: str) -> None:
        pass

    def set_autosave_every(self, n: int) -> None:
        pass

    def set_api_config(self, base_url: str, api_key: str, model: str) -> None:
        pass

    def restart(self, *a, **k) -> None:
        pass

    def shutdown(self, *a) -> None:
        pass

    def isRunning(self) -> bool:                  # noqa: N802
        return False


def _window(tmp_path: Path, worker) -> MainWindow:
    """真实 MainWindow（替身 worker，不 show、不起线程）+ 素材全在 tmp 里。"""
    cdir = tmp_path / "characters"
    cards = [_write_card(cdir, "甲")]
    models = tmp_path / "models.yaml"
    models.write_text("think: {backend: stub, model: stub, params: {}}\n"
                      "speak: {backend: stub, model: stub, params: {}}\n",
                      encoding="utf-8")
    cfg = AppConfig(scene=None, characters=cards, models=models,
                    bid=tmp_path / "bid.yaml", run_root=tmp_path / "runs",
                    live=False, api_key=None)
    return MainWindow(worker, cfg, libraries_root=tmp_path / "libraries")


class _RecordingDialog(QObject):
    """替身结算弹窗：只记"被谁用什么清单开过"，并提供 `decided` 供测试回传决定。"""

    decided = Signal(dict)
    made: list["_RecordingDialog"] = []

    def __init__(self, rows, *, warnings=(), parent=None) -> None:
        super().__init__(parent)
        self.rows = [dict(r) for r in rows]
        self.warnings = list(warnings)
        self.opened = False
        type(self).made.append(self)

    def open(self) -> None:
        self.opened = True


@pytest.fixture
def dialogs(monkeypatch):
    """把主窗口用的结算弹窗换成替身（**绝不真 exec**），返回替身类。"""
    _RecordingDialog.made = []
    monkeypatch.setattr(mw_mod, "SettlementDialog", _RecordingDialog)
    return _RecordingDialog


# ===================================================== 1. 待决 → 开窗（§8.3）
def test_pending_signal_opens_the_dialog_with_the_engine_rows(qapp, tmp_path, dialogs):
    """引擎的待决清单到主线程 → 开窗，且清单与警告**照抄**引擎那一份。"""
    win = _window(tmp_path, _FakeWorker())
    rows = [_row("甲", added=2, revised=1, titles=["药铺的暗格", "陈掌柜"]),
            _row("乙", added=0, revised=3, titles=["旧钥匙"])]
    warnings = ["「甲」的散场总结没写出来（TimeoutError: 超时）。"]

    win._worker.sig_settlement_pending.emit({"characters": rows, "warnings": warnings})

    assert len(dialogs.made) == 1, "有待决内容就该开一张窗"
    dialog = dialogs.made[0]
    assert dialog.rows == rows, "清单必须与引擎给的一模一样（一条都不许自己算）"
    assert dialog.warnings == warnings
    assert dialog.opened is True
    assert win._action_settle_pending.isEnabled(), "有待决 → 手动结算入口可用"


def test_no_pending_content_means_no_dialog(qapp, tmp_path, dialogs):
    """空清单 → 不开窗（清单是"要看的东西"，空的只是一次骚扰）。"""
    win = _window(tmp_path, _FakeWorker())
    win._worker.sig_settlement_pending.emit({"characters": [], "warnings": []})
    assert dialogs.made == []


def test_decisions_travel_back_to_the_worker(qapp, tmp_path, dialogs):
    """窗里的决定经 `decided` → 主窗口 → `worker.apply_settlement`（第一跳）。"""
    worker = _FakeWorker()
    win = _window(tmp_path, worker)
    win._worker.sig_settlement_pending.emit({"characters": [_row("甲"), _row("乙")]})

    dialogs.made[0].decided.emit({"甲": "keep", "乙": "discard"})
    assert worker.decisions == [{"甲": "keep", "乙": "discard"}]

    dialogs.made[0].decided.emit({})           # 空决定不回传（没做决定 ≠ 决定丢弃）
    assert len(worker.decisions) == 1


# ================================================= 2. 离场挂起（§7.3）
def test_exit_hint_is_non_blocking_and_enables_the_menu(qapp, tmp_path, dialogs):
    """离场挂起来的是一条**非打断**提示：不弹窗，只在日志/状态区各露一行。"""
    win = _window(tmp_path, _FakeWorker())
    win._worker.sig_settlement_hint.emit(_row("甲", added=2, revised=1))

    assert dialogs.made == [], "离场绝不弹窗——用户正在看戏"
    assert "甲" in win._status_chip.text()
    logged = [str(item.get("text") or "") for item in win._log_entries]
    assert any("甲" in text for text in logged), "日志区要留一条（可回溯）"
    assert win._action_settle_pending.isEnabled() is True


def test_manual_settle_opens_the_dialog_for_the_pending_character(qapp, tmp_path, dialogs):
    """「角色 → 结算待决…」把待决的人开成同一张结算窗（随时可单独结）。"""
    worker = _FakeWorker()
    win = _window(tmp_path, worker)
    win._worker.sig_settlement_hint.emit(_row("甲"))

    win._action_settle_pending.trigger()
    assert len(dialogs.made) == 1
    assert [r["name"] for r in dialogs.made[0].rows] == ["甲"]

    dialogs.made[0].decided.emit({"甲": "keep"})
    assert worker.decisions == [{"甲": "keep"}]


def test_applied_receipt_clears_the_pending_mirror(qapp, tmp_path, dialogs):
    """结算回执一到：结清的人从待决名单收掉、入口重新禁用、结果在状态区说一句。"""
    win = _window(tmp_path, _FakeWorker())
    win._worker.sig_settlement_hint.emit(_row("甲"))
    assert win._action_settle_pending.isEnabled() is True

    win._worker.sig_settlement_applied.emit(
        {"decisions": {"甲": "keep"}, "warnings": [],
         "settled": ["甲"], "unsettled": []})
    assert win._action_settle_pending.isEnabled() is False
    assert "1" in win._status_chip.text(), "要说清结了几个人"


def test_an_unsettled_character_keeps_the_retry_entry_open(qapp, tmp_path, dialogs):
    """引擎没并进去的人**留在待决名单**、入口照旧可用，文案不许报"已结算"（§7.2/§7.4）。

    引擎在这种情况下留在待结算，并在报告里叫人"处理好之后再结一次"。界面若照用户的点击
    把名单清干净，那条重试入口（「角色 → 结算待决…」）当场变灰：用户把坏文件修好回来，
    却发现再也点不开结算窗——这一场的所得只能靠 CLI 救回。同理，状态区不能在他一个字都
    没并进去的时候说"已结算"。
    """
    win = _window(tmp_path, _FakeWorker())
    win._worker.sig_settlement_hint.emit(_row("甲"))
    assert win._action_settle_pending.isEnabled() is True

    win._worker.sig_settlement_applied.emit(
        {"decisions": {"甲": "keep"},
         "warnings": ["这次结算没有完全生效：这一场仍是待结算状态。"],
         "settled": [], "unsettled": ["甲"]})

    assert win._action_settle_pending.isEnabled() is True, "重试入口必须还开着"
    assert win._pending_settlement.get("甲"), "他仍在待决名单里"
    chip = win._status_chip.text()
    assert "甲" in chip and "没" in chip, f"要如实说没结清，而不是报成功：{chip!r}"
    logged = [str(item.get("text") or "") for item in win._log_entries]
    assert any("没有完全生效" in text for text in logged), "引擎的警告要落日志"


def test_an_empty_list_with_warnings_still_reaches_the_user(qapp, tmp_path, dialogs):
    """清单空、但引擎有话说（有人账记着却收不上来）→ 不进无声：不开窗，但落日志+状态区。

    §7.4 要求"绝不静默"：副本里那条条目文件坏了的时候，`settlement_rows()` 是空的
    （没有可看的东西），警告却是用户唯一能知道"他的库少了一条"的渠道。两条路都堵死
    （不开窗是对的·不发信号是错的），用户就以为散场一切正常，而角色的知识永远不会并进
    本体库——这本场再没有第二次 prepare 的机会（运行根是 app-<uuid>，用户找不到）。
    """
    win = _window(tmp_path, _FakeWorker())
    warning = "「甲」：本体库：条目文件 entries/密室.md 读不了。"
    win._worker.sig_settlement_pending.emit({"characters": [], "warnings": [warning]})

    assert dialogs.made == [], "没有可看的东西 → 绝不开一张空窗"
    logged = [str(item.get("text") or "") for item in win._log_entries]
    assert any("密室.md" in text for text in logged), "引擎的话必须落到日志区"
    assert "密室.md" in win._status_chip.text(), "当下也要看得见"


def test_the_exit_hint_names_the_scene_as_well_as_the_character(qapp, tmp_path, dialogs):
    """离场提示要说清**谁、哪一场**（§7.3）：名字认得人，场景名认出这笔账属于哪一场。

    用户连开几场、或中途改过场景名之后，光看角色名认不出这是哪一场的账；结算窗里显示的
    是「名字 · 场景」，两处口径该一致。场景名读不出来（空白）时不留下一个空括号。
    """
    win = _window(tmp_path, _FakeWorker())
    win._worker.sig_settlement_hint.emit(_row("甲", scene="茶室"))
    assert "茶室" in win._status_chip.text(), "状态区要说清是哪一场"

    logged = [str(item.get("text") or "") for item in win._log_entries]
    assert any("茶室" in text for text in logged), "日志区同样"

    win._worker.sig_settlement_hint.emit(_row("乙", scene=""))
    assert "（）" not in win._status_chip.text(), "场景名读不出来时不留空括号"


def test_the_exit_notice_is_logged_once_not_twice(qapp, tmp_path, dialogs):
    """离场待结算那条隐式事件不写通用隐式事件日志：同一件事只说一遍（§7.3）。

    通用那条（渲染成"（隐式事件：…）"）与 `sig_settlement_hint` 那条说的是同一次离场，
    而后者更完整（谁、哪一场、几条）。两条都写，日志区就多出一行更含糊的重复话——用户
    得读两遍才知道是同一次。**别的隐式事件照旧写**：那是它们唯一的可见痕迹。
    """
    win = _window(tmp_path, _FakeWorker())
    head = {"hook_id": "", "event_kind": "pending_settlement", "turn": 1}
    win._on_scene_diag({"implicit_events": [
        {**head, "content": "（甲离场：他这一场记下的东西还没结算。）", **_row("甲")}]})
    win._worker.sig_settlement_hint.emit(_row("甲"))

    texts = [str(item.get("text") or "") for item in win._log_entries]
    assert len([text for text in texts if "甲" in text]) == 1, \
        "同一次离场只留一条（提示那条，带场景名）"

    win._on_scene_diag({"implicit_events": [
        {"hook_id": "h1", "event_kind": "hook", "turn": 2, "content": "（灯灭了。）"}]})
    assert any("灯灭了" in str(item.get("text") or "") for item in win._log_entries), \
        "别的隐式事件照旧写日志"


def test_no_pending_means_the_menu_item_is_disabled(qapp, tmp_path, dialogs):
    """没有待决内容时入口禁用并说明原因（绝不点开一个空窗）。"""
    win = _window(tmp_path, _FakeWorker())
    assert win._action_settle_pending.isEnabled() is False
    assert win._action_settle_pending.toolTip()


# ============================================== 3. worker：事件 → 信号
class _FakeEngine:
    """引擎替身：只实现结算接线要读的那几个面。"""

    def __init__(self, *, rows=(), warnings=(), implicit=(), closed=True,
                 reports=None) -> None:
        self._rows = [dict(r) for r in rows]
        self._warnings = list(warnings)
        self._implicit = [dict(e) for e in implicit]
        self._reports = dict(reports or {})
        self.closed = bool(closed)
        self.prepared = 0
        self.settlements: list[dict] = []

    async def prepare_settlement(self) -> dict:
        self.prepared += 1
        return {}

    def settlement_rows(self) -> list[dict]:
        return [dict(r) for r in self._rows]

    def settlement_warnings(self) -> list[str]:
        return list(self._warnings)

    def implicit_events(self) -> list[dict]:
        return [dict(e) for e in self._implicit]

    def apply_settlement(self, decisions: dict) -> dict:
        self.settlements.append(dict(decisions))
        return dict(self._reports)


def test_worker_has_the_three_settlement_signals():
    """三条信号在 `SceneWorker` 上（主窗口按名字防御式连接）。"""
    for name in ("sig_settlement_pending", "sig_settlement_hint",
                 "sig_settlement_applied"):
        assert hasattr(SceneWorker, name), name


def test_worker_turns_prepared_rows_into_the_pending_signal():
    """收尾准备完 → 待决清单转成 `sig_settlement_pending`（清单 + 警告都照抄引擎）。"""
    worker = SceneWorker()
    seen: list[dict] = []
    worker.sig_settlement_pending.connect(seen.append)
    engine = _FakeEngine(rows=[_row("甲")], warnings=["总结没写出来"])

    asyncio.run(worker._prepare_settlement(engine))

    assert engine.prepared == 1
    assert len(seen) == 1
    assert seen[0]["characters"] == [_row("甲")]
    assert seen[0]["warnings"] == ["总结没写出来"]
    assert worker.settlement_rows() == [_row("甲")]


def test_worker_does_not_emit_without_pending_content():
    """没有信息库/没有所得（空表）→ **一个信号都不发**（既有信号序列逐字节不变）。"""
    worker = SceneWorker()
    seen: list[dict] = []
    worker.sig_settlement_pending.connect(seen.append)

    asyncio.run(worker._prepare_settlement(_FakeEngine(rows=[])))
    worker._emit_settlement_pending(_FakeEngine(rows=[]))
    worker._emit_settlement_hint(_FakeEngine(implicit=[]))
    assert seen == []
    assert worker.settlement_rows() == []


def test_worker_emits_warnings_even_when_there_is_nothing_to_settle():
    """清单空、警告非空 → **照发**（清单空着，界面据此不开窗，只把话带给用户）。

    空清单直接 return 会把"他的库少了一条"这唯一一句知情渠道一起吞掉（§7.4 绝不静默）：
    那条警告正是"这次收不上来"的全部解释，而它只走这一个信号。
    """
    worker = SceneWorker()
    seen: list[dict] = []
    worker.sig_settlement_pending.connect(seen.append)
    warning = "「甲」的条目文件 entries/密室.md 读坏了"

    worker._emit_settlement_pending(_FakeEngine(rows=[], warnings=[warning]))

    assert len(seen) == 1
    assert seen[0]["characters"] == [] and seen[0]["warnings"] == [warning]
    assert worker.settlement_rows() == [], "没人可结 → 待决镜像仍是空的"


def test_worker_does_not_prepare_before_close():
    """还没收束就不准备（未收束的一步都不该写散场总结）。"""
    worker = SceneWorker()
    seen: list[dict] = []
    worker.sig_settlement_pending.connect(seen.append)
    engine = _FakeEngine(rows=[_row("甲")], closed=False)

    asyncio.run(worker._prepare_settlement(engine))
    assert engine.prepared == 0 and seen == []


def test_worker_turns_exit_events_into_hints_once_each():
    """离场挂起的隐式事件按**增量**转成提示：同一条只派一次（每块刷会糊满日志）。"""
    worker = SceneWorker()
    seen: list[dict] = []
    worker.sig_settlement_hint.connect(seen.append)
    event = {"hook_id": "", "event_kind": "pending_settlement",
             "content": "（甲离场）", **_row("甲")}
    engine = _FakeEngine(implicit=[event])

    worker._emit_settlement_hint(engine)
    worker._emit_settlement_hint(engine)          # 同一份事件流：不重复派
    assert seen == [_row("甲")]

    engine._implicit.append({"hook_id": "", "event_kind": "pending_settlement",
                             **_row("乙")})
    worker._emit_settlement_hint(engine)
    assert [row["name"] for row in seen] == ["甲", "乙"]


def test_worker_hint_counter_recovers_after_a_reset():
    """事件流被重置（清空）后计数回零：此后新的离场提示照常派得出。"""
    worker = SceneWorker()
    seen: list[dict] = []
    worker.sig_settlement_hint.connect(seen.append)
    engine = _FakeEngine(implicit=[{"event_kind": "pending_settlement", **_row("甲")}])
    worker._emit_settlement_hint(engine)

    engine._implicit = []                          # 重置运行上下文把事件流清了
    worker._emit_settlement_hint(engine)
    engine._implicit = [{"event_kind": "pending_settlement", **_row("乙")}]
    worker._emit_settlement_hint(engine)
    assert [row["name"] for row in seen] == ["甲", "乙"]


def test_worker_apply_settlement_reaches_the_engine(monkeypatch):
    """决定真的走到 `engine.apply_settlement`（在引擎 loop 上执行），并回执。"""
    worker = SceneWorker()
    engine = _FakeEngine()
    worker._engine = engine
    worker._oplock = asyncio.Lock()

    async def _noop_flush(self, engine):            # 冲刷与引擎状态无关，这里不需要
        return None

    monkeypatch.setattr(SceneWorker, "_flush", _noop_flush)
    applied: list[dict] = []
    worker.sig_settlement_applied.connect(applied.append)

    asyncio.run(worker._apply_settlement({"甲": "keep", "乙": "discard"}))

    assert engine.settlements == [{"甲": "keep", "乙": "discard"}]
    assert applied == [{"decisions": {"甲": "keep", "乙": "discard"},
                        "warnings": [], "settled": [], "unsettled": []}]


def test_worker_receipt_tells_apart_what_the_engine_actually_settled(monkeypatch):
    """回执分 `settled` / `unsettled`，判据是**引擎真做成了什么**（§7.2/§7.5）。

    三个角色的三份报告覆盖三条判据：并进去了（keep + persisted）算结清；被挡下
    （persisted=False）算没结清、留在待决名单里，重试入口不能关；重复结算（repeated）
    是空操作——这一场先前就结过了，引擎那时也不列他，故算结清。
    """
    worker = SceneWorker()
    reports = {
        "甲": settle_mod.SettleReport(outcome="keep",
                                      merged=settle_mod.MergeReport(persisted=True)),
        "乙": settle_mod.SettleReport(
            outcome="keep", merged=settle_mod.MergeReport(persisted=False),
            warnings=["这次结算没有完全生效：这一场仍是待结算状态。"]),
        "丙": settle_mod.SettleReport(outcome="keep", repeated=True),
    }
    engine = _FakeEngine(reports=reports)
    worker._engine = engine
    worker._oplock = asyncio.Lock()
    worker._pending_settlement = {"甲": _row("甲"), "乙": _row("乙"), "丙": _row("丙")}

    async def _noop_flush(self, engine):
        return None

    monkeypatch.setattr(SceneWorker, "_flush", _noop_flush)
    applied: list[dict] = []
    worker.sig_settlement_applied.connect(applied.append)

    asyncio.run(worker._apply_settlement({"甲": "keep", "乙": "keep", "丙": "keep"}))

    assert applied[-1]["settled"] == ["甲", "丙"]
    assert applied[-1]["unsettled"] == ["乙"]
    assert [row["name"] for row in worker.settlement_rows()] == ["乙"], \
        "没并进去的人必须留在待决名单里——他还要重试"
    assert any("没有完全生效" in w for w in applied[-1]["warnings"]), "警告照样带上去"


def test_worker_clears_the_hint_counter_when_a_new_engine_starts(tmp_path, monkeypatch):
    """换成新引擎 → 离场提示的计数与待决镜像归零（新事件流，序号从零起）。

    `_settlement_hint_seen` 数的是**当前这一场那条隐式事件流**里的序号。上一场离场 ≥1 人、
    新场头一块里离场的人数又更多时，跨场留着的计数会把新场前几个人当成"已经派过的"整段
    吞掉（`pending[seen:]` 从中间切）：没有提示、没有日志、菜单项也不亮，用户直到散场才
    在结算窗里第一次见到这个人——而这正是 §7.3 想避免的"等到最后才发现"。`_pending_settlement`
    同清：副本是逐场一份的（新引擎用新的 run_root），上一场的账在新场不作数。
    """
    worker_mod = __import__("harness.gui.worker", fromlist=["worker"])
    worker = SceneWorker()
    worker._settlement_hint_seen = 5               # 上一场派到最后一条时的序号
    worker._pending_settlement = {"甲": _row("甲")}  # 上一场留下的账

    class _StubEngine:
        closed = False
        start_seconds = 77400
        start_hhmm = "21:30"
        boundary_seconds = None
        boundary_hhmm = None

        async def open_scene(self, opening):
            return []

        async def aclose(self):
            self.closed = True

    async def _stub_build(self):
        return _StubEngine()

    async def _noop(self, *a, **k):
        return None

    monkeypatch.setattr(SceneWorker, "_build_engine", _stub_build)
    monkeypatch.setattr(SceneWorker, "_flush", _noop)
    monkeypatch.setattr(SceneWorker, "_emit_narration", lambda self: None)
    monkeypatch.setattr(SceneWorker, "_read_boundary_desc", lambda self, e: None)
    monkeypatch.setattr(SceneWorker, "_autoplay", _noop)
    monkeypatch.setattr(SceneWorker, "_v_ticker_loop", _noop)
    monkeypatch.setattr(worker_mod, "build_scene_payload", lambda engine: {"backend": "stub"})
    worker._cfg = {"scene": None, "characters": [], "models": None, "bid": None,
                   "live": False, "api_key": None, "run_root": tmp_path / "runs"}

    async def _drive():
        worker._oplock = asyncio.Lock()            # loop 上建锁（同 run() 的纪律）
        await worker._launch(None)

    asyncio.run(_drive())

    assert worker._settlement_hint_seen == 0, "新引擎是新的 list，序号必须从零起"
    assert worker._pending_settlement == {}, "上一场的账在新场不作数"

    # 新场头一块里两个人离场：两条提示都派得出（没被上一场的序号吞掉）。
    seen: list[dict] = []
    worker.sig_settlement_hint.connect(seen.append)
    worker._emit_settlement_hint(_FakeEngine(implicit=[
        {"event_kind": "pending_settlement", **_row("乙")},
        {"event_kind": "pending_settlement", **_row("丙")}]))
    assert [row["name"] for row in seen] == ["乙", "丙"]


def test_apply_settlement_submits_the_decisions_once(monkeypatch):
    """公开入口把决定投到引擎 loop（fire-and-forget）；空决定表什么都不投。"""
    worker = SceneWorker()
    # _submit_cast 要求"线程已起"（loop 有、_ready 置位）才肯投递；这里只钉投递本身。
    worker._loop = object()                 # type: ignore[assignment]
    worker._ready.set()
    submitted: list = []
    monkeypatch.setattr(SceneWorker, "_submit", lambda self, coro: submitted.append(coro))

    worker.apply_settlement({"甲": "keep"})
    assert len(submitted) == 1
    submitted.pop().close()                          # 没被 await 的协程别留警告

    worker.apply_settlement({})
    assert submitted == []


def test_worker_survives_a_broken_engine_read():
    """引擎那几个面读不出来（替身引擎/半建引擎）时静默跳过，绝不抛进 Qt 槽。"""
    class _Broken:
        closed = True

        async def prepare_settlement(self):
            raise RuntimeError("boom")

    worker = SceneWorker()
    seen: list[dict] = []
    worker.sig_settlement_pending.connect(seen.append)
    asyncio.run(worker._prepare_settlement(_Broken()))
    worker._emit_settlement_hint(object())          # 连 implicit_events 都没有
    worker._emit_settlement_pending(object())
    assert seen == []


# ====================================== 4. 真引擎串一遍（离场 → 手动结算 → 幂等）
def _tiny_engine(tmp_path: Path, libs: Path):
    """一场能开起来的**真引擎**（stub 后端、零阈值 bid、不自动叙述）。"""
    scene_p = tmp_path / "茶室.json"
    scene_p.write_text(json.dumps({"name": "茶室", "characters": [{"name": "甲"}]},
                                  ensure_ascii=False), encoding="utf-8")
    card = _write_card(tmp_path / "cards", "甲")
    models = tmp_path / "models.yaml"
    models.write_text(
        "think:\n  backend: stub\n  model: stub\n  params: {}\n"
        "speak:\n  backend: stub\n  model: stub\n  params: {}\n", encoding="utf-8")
    bid = tmp_path / "bid.yaml"
    bid.write_text("interruption_threshold: 0.0\nspeak_threshold: 0.0\n"
                   "silence_k: 100000\n", encoding="utf-8")
    engine = SceneEngine(scene_p, [card], models, run_root=tmp_path / "runs",
                         bid_path=bid, auto_narrate=False, libraries_root=libs,
                         closing_at_block=2)
    urge = {"aroused": 0.5, "obligation_fulfilled": [], "goal_progress": 0.0,
            "addressed": None, "impression_of_speaker": None, "urge": 2.0}
    engine.think_backend = StubBackend(json_script=[urge] * 20)
    engine.speak_backend = StubBackend(line_script=["（占位台词。）"] * 20)
    engine.narrate_backend = StubBackend(line_script=["这一夜就到这里了。"])
    return engine


def test_a_refused_merge_leaves_the_character_pending_in_the_worker(tmp_path, monkeypatch):
    """真引擎串一遍：**一个字都没并进去**时，worker 不许把这个人从待决名单里抹掉。

    本体库里那个位置躺着一份读不出来的文件（设计明确支持手写 md，写坏是常事）→
    `merge_gain` 挡下、`persisted=False`，引擎把这一场留在待结算并叫用户"处理好之后再结
    一次"。界面若照用户的点击清名单，那条重试入口当场变灰：用户把文件修好回来，结算窗
    再也点不开——这一场的所得只能靠 CLI 救回（§7.2/§7.5）。
    """
    libs = tmp_path / "libraries"
    libs.mkdir(parents=True, exist_ok=True)
    engine = _tiny_engine(tmp_path, libs)
    worker = SceneWorker()
    worker._engine = engine
    worker._oplock = asyncio.Lock()
    applied: list[dict] = []
    worker.sig_settlement_applied.connect(applied.append)

    async def _noop_flush(self, engine):
        return None

    monkeypatch.setattr(SceneWorker, "_flush", _noop_flush)
    key = "场景-茶室-第1场"
    own = store.library_dir("character", "甲", root=libs)

    async def _drive():
        await engine.open_scene()
        copy = store.scene_library_dir(engine.run_root, "甲")
        lib = store.load_library(copy).library
        lib.upsert(Entry(key=key, title=key, summary="记下了一件小事", body="正文。",
                         origin=EntryOrigin(kind="scene", scene="茶室", turn=1)))
        store.save_library(lib, copy)
        store.record_pending(copy, key, turn=1)
        await engine.remove_character("甲")     # 他离场 → 提示 → 进待决镜像

    asyncio.run(_drive())
    worker._emit_settlement_hint(engine)
    assert [row["name"] for row in worker.settlement_rows()] == ["甲"], "前置：他已在待决名单里"
    # 本体库里那个位置先躺一份读不出来的文件：合并必被挡下（不是"没事可做"，是"没做成"）。
    path_md = store.entry_path(own, key)
    path_md.parent.mkdir(parents=True, exist_ok=True)
    path_md.write_text("这不是条目：没有 front-matter", encoding="utf-8")

    asyncio.run(worker._apply_settlement({"甲": "keep"}))
    asyncio.run(engine.aclose())                 # 关掉 sqlite 连接（aiosqlite 线程不关会吊住退出）

    assert applied and applied[-1]["unsettled"] == ["甲"], "引擎没并进去 → 回执说没结清"
    assert [row["name"] for row in worker.settlement_rows()] == ["甲"], \
        "他还在待决名单里——重试入口必须留着"


def test_a_broken_copy_entry_still_reaches_the_window(qapp, tmp_path, monkeypatch, dialogs):
    """真引擎串一遍：**清单空、引擎有话要说**时窗口照样看得见（§7.4 绝不静默）。

    副本里那条条目文件读不出来（半截写盘、手改坏）→ 他这一场收不上来任何所得：清单是空
    的（没有可看的东西），而 `settlement_warnings()` 那句"他的库少了一条"就是用户唯一的
    知情渠道。空清单把警告一起吞掉，用户以为散场一切正常，角色的知识永远不会并进本体库，
    他连为什么都不知道。**照旧不开窗**——没有可看的东西，空窗只会让人以为有得选。
    """
    libs = tmp_path / "libraries"
    libs.mkdir(parents=True, exist_ok=True)
    engine = _tiny_engine(tmp_path, libs)
    worker = SceneWorker()
    worker._engine = engine
    worker._oplock = asyncio.Lock()
    win = _window(tmp_path, worker)

    async def _drive():
        await engine.open_scene()
        copy = store.scene_library_dir(engine.run_root, "甲")
        path_md = store.entry_path(copy, "药铺")
        path_md.parent.mkdir(parents=True, exist_ok=True)
        path_md.write_text("这不是条目：没有 front-matter", encoding="utf-8")
        store.record_pending(copy, "药铺", turn=1)   # 账记着，这次却收不上来
        await engine.close_scene()
        await engine.prepare_settlement()            # 收束后的准备：读盘警告在这里攒下

    asyncio.run(_drive())
    assert engine.closed, "前置：收束了才会准备结算"
    assert engine.settlement_rows() == [], "前置：没有可看的东西（清单是空的）"
    assert engine.settlement_warnings(), "前置：引擎有话要说"

    asyncio.run(worker._prepare_settlement(engine))
    asyncio.run(engine.aclose())                 # 关掉 sqlite 连接（aiosqlite 线程不关会吊住退出）

    assert dialogs.made == [], "空清单绝不开窗"
    texts = [str(item.get("text") or "") for item in win._log_entries]
    assert any("药铺.md" in text for text in texts), "引擎的话必须落到日志区"
    assert "药铺.md" in win._status_chip.text(), "当下也要看得见"


def test_exit_hang_becomes_a_hint_and_manual_settle_is_idempotent(tmp_path, monkeypatch):
    """真引擎串一遍：离场挂起 → 非打断提示 → 手动结算 → **重复结算幂等**（§7.3/§7.5）。

    这一条同时是"引擎事件真的被 worker 消费了"的证据：提示行来自引擎挂下的隐式事件，
    结算走的是 `engine.apply_settlement`（不是界面自己算的合并）。
    """
    libs = tmp_path / "libraries"
    libs.mkdir(parents=True, exist_ok=True)
    engine = _tiny_engine(tmp_path, libs)
    worker = SceneWorker()
    worker._engine = engine
    worker._oplock = asyncio.Lock()
    hints: list[dict] = []
    applied: list[dict] = []
    worker.sig_settlement_hint.connect(hints.append)
    worker.sig_settlement_applied.connect(applied.append)

    async def _noop_flush(self, engine):
        return None

    monkeypatch.setattr(SceneWorker, "_flush", _noop_flush)
    key = "场景-茶室-第1场"
    own = store.library_dir("character", "甲", root=libs)
    body = {}

    async def _drive():
        await engine.open_scene()
        copy = store.scene_library_dir(engine.run_root, "甲")
        lib = store.load_library(copy).library
        lib.upsert(Entry(key=key, title=key, summary="记下了一件小事",
                         body="正文。",
                         origin=EntryOrigin(kind="scene", scene="茶室", turn=1)))
        store.save_library(lib, copy)
        store.record_pending(copy, key, turn=1)
        await engine.remove_character("甲")

    asyncio.run(_drive())

    # 离场：引擎挂了一条待结算隐式事件 → 提示（非打断），待决名单里有了他。
    assert hints == [], "还没派呢（worker 只在 flush 时消费）"
    worker._emit_settlement_hint(engine)
    assert [row["name"] for row in hints] == ["甲"]
    assert [row["name"] for row in worker.settlement_rows()] == ["甲"]

    # 手动结算（走 worker 的真路径）→ 并进本体库。
    asyncio.run(worker._apply_settlement({"甲": "keep"}))
    first = store.load_library(own).library.entries
    assert key in first, "保留就该并进本体库"
    assert worker.settlement_rows() == [], "结完账不再待决"
    assert applied and applied[-1]["decisions"] == {"甲": "keep"}

    snapshot = {p.name: p.read_bytes() for p in sorted(store.entries_dir(own).glob("*"))}
    # 重复结算：**幂等**（§7.5 先到先得）——再结一次一个字节都不变，也绝不翻盘。
    asyncio.run(worker._apply_settlement({"甲": "discard"}))
    again = {p.name: p.read_bytes() for p in sorted(store.entries_dir(own).glob("*"))}
    assert again == snapshot, "重复结算不许改动本体库"
    assert store.load_library(own).library.entries[key].status == "active", \
        "已「保留」的账不会被后来的「丢弃」翻过去"
