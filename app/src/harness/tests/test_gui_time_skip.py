"""场景跳时间的**界面侧**（《人际关系与场景推进》§4.3/§4.4）：虚拟钟归 worker 持有。

契约（逐条对应设计文档）：

  · 引擎只在四道闸全过的跳时间行上写一个**钟偏移字段**（`clock_jump_minutes`），
    它自己**不碰钟**（图与引擎仍不持钟，§4.3 的分工不变）；
  · worker 在派发这条行时把虚拟钟**前推**：先推钟再派发，于是这条行上的 `time_hhmmss`
    就是"之后"的时刻（与正文「30 分钟之后」对得上）；
  · **撤回/改写要把那段时间收回来**（§6.1："该行及其后整段下文从没发生过"）：跳时间行是
    "时间过去了"的唯一凭据，作废它就得把已经推掉的分钟从钟里收回——否则用户读到的转录说
    "这段没发生过"、钟却说已经 22:00 了（钟跳了、别的没跟上）；
  · **跳时间不重置沉默压力**（§4.4）：跳过 8 小时不等于"刚说过话"——推钟这一步只动钟，
    不动任何"谁刚开过口"的状态；
  · 同一批消息只推一次钟（`_seen` 游标保证），没有偏移字段的行一概不动钟；
  · **不开启时**：worker 不把开关交给引擎（缺省关），一切逐字节不变。

全程离屏（QT_QPA_PLATFORM=offscreen）、不联网、不碰真实用户设置。
"""
import asyncio
import json
import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from harness import sceneclock as sc  # noqa: E402
from harness.gui.worker import SceneWorker  # noqa: E402

_BODY = "30 分钟之后，桌上的灯自己灭了，作业终于写完了。"


@pytest.fixture(scope="module")
def qapp():
    """模块级复用单一 QApplication（Qt 只允许每进程一个）。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _FakeMonotonic:
    """可手动推进的时钟源（同 test_scene_time 的替身）：让"走到哪一刻"完全确定。"""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _jump_msg(minutes: int = 30, **extra) -> dict:
    """一条跳时间叙述行（引擎落地的形状：叙述行 + 钟偏移字段，§4.3）。"""
    return {"id": 5, "speaker": "场景", "speaker_type": "narrator", "content": _BODY,
            "in_scene": "自习室", "turn": 3, "clock_jump_minutes": minutes, **extra}


def _worker_with_clock(start: str = "21:30") -> tuple[SceneWorker, sc.VirtualClock]:
    """离屏 worker + 一只走时中的虚拟钟（21:30 起，1× 流速，假时钟源）。"""
    worker = SceneWorker()
    clock = sc.VirtualClock(sc.parse_hhmm(start), rate=1.0,
                            monotonic=_FakeMonotonic())
    clock.start()
    worker._vclock = clock
    return worker, clock


# ==================================================== 派发时前推虚拟钟
def test_sweep_advances_the_virtual_clock_for_a_jump_line(qapp):
    """跳时间行派发时把钟前推：行上的 time_hhmmss 是**跳后**的时刻（21:30 + 30min）。"""
    worker, clock = _worker_with_clock()
    seen: list[dict] = []
    worker.sig_message.connect(seen.append)

    worker._sweep([_jump_msg(30)])

    assert clock.current() == sc.parse_hhmm("21:30") + 30 * 60
    assert len(seen) == 1 and seen[0]["content"] == _BODY
    assert seen[0]["time_hhmmss"] == "22:00:00", "先推钟再派发：行上的时刻是跳后"
    assert seen[0]["clock_jump_minutes"] == 30, "偏移字段原样带给界面"


def test_sweep_advances_the_clock_only_once_per_line(qapp):
    """同一批消息只推一次钟（`_seen` 游标去重）：重复 sweep 不重复跳时间。"""
    worker, clock = _worker_with_clock()
    seen: list[dict] = []
    worker.sig_message.connect(seen.append)
    msgs = [_jump_msg(30)]

    worker._sweep(msgs)
    worker._sweep(msgs)                     # 再 sweep 一次同一批 → 不为它再跳一次
    assert clock.current() == sc.parse_hhmm("21:30") + 30 * 60
    assert len(seen) == 1


def test_plain_lines_never_move_the_clock(qapp):
    """没有偏移字段的行（普通叙述/角色台词/导演行）一概不动钟。"""
    worker, clock = _worker_with_clock()
    seen: list[dict] = []
    worker.sig_message.connect(seen.append)

    worker._sweep([
        {"id": 1, "speaker": "甲", "speaker_type": "character", "content": "（……）"},
        {"id": 2, "speaker": "场景", "speaker_type": "narrator", "content": "灯灭了。"},
        {"id": 3, "speaker": "导演", "speaker_type": "director", "content": "开场。"},
    ])

    assert clock.current() == sc.parse_hhmm("21:30")
    assert [m["time_hhmmss"] for m in seen] == ["21:30:00"] * 3


def test_jump_without_a_clock_or_offset_is_a_no_op(qapp):
    """钟还没起（线程未开）/ 偏移非法或为 0 → 静默不推，绝不冒泡异常。"""
    worker = SceneWorker()                  # 没有 _vclock
    seen: list[dict] = []
    worker.sig_message.connect(seen.append)
    worker._sweep([_jump_msg(30)])
    assert len(seen) == 1, "没有钟也要照常派发行"

    worker2, clock = _worker_with_clock()
    worker2._sweep([_jump_msg(0), _jump_msg(-5),
                    {"id": 9, "speaker": "场景", "speaker_type": "narrator",
                     "content": "灯灭了。", "clock_jump_minutes": "坏了"}])
    assert clock.current() == sc.parse_hhmm("21:30"), "0/负数/坏载荷都不推钟"


def test_jump_does_not_reset_the_workers_silence_bookkeeping(qapp):
    """§4.4：跳时间不重置沉默压力——推钟这一步不碰任何"谁刚开过口"的状态。

    角色侧的数值沉默压力由引擎的 dynamics 持有（叙述行不是角色台词，本就抬不动它，
    见 test_time_skip.py 的引擎侧用例）；worker 侧同理：跳时间行与普通叙述行在派发上
    走同一条路，只是多推一次钟。
    """
    worker, clock = _worker_with_clock()
    worker._silence_streak = 4              # 上一段静默攒下的计数
    worker._armed = False                   # 空闲态
    worker._sweep([_jump_msg(480)])         # 跳 8 小时
    assert clock.current() == sc.parse_hhmm("21:30") + 8 * 3600
    assert worker._silence_streak == 4, "跳时间不把沉默计数清零"
    assert worker._armed is False, "也不把空闲态当成「有话要说」"


class _FakeEngine:
    """替身引擎：只实现 worker 的撤回/改写路径用到的那几个接口（同步记账，无 IO）。"""

    def __init__(self, retract_ids: list[int]) -> None:
        self.closed = False
        self._ids = list(retract_ids)
        self.retracted: set[int] = set()
        self.edited: tuple[int, str] | None = None

    async def retract(self, mid: int) -> list[int]:
        if not self._ids:
            return [int(mid)]                    # 替身引擎的退化口径：只报被点的那条
        self.retracted |= {int(i) for i in self._ids}
        return list(self._ids)

    async def edit_narration(self, mid: int, text: str):  # noqa: D102
        self.edited = (int(mid), text)
        self.retracted |= {int(i) for i in self._ids}
        return {"id": 99}

    async def messages(self) -> list[dict]:  # noqa: D102
        return []

    async def reset_scene_runtime(self) -> None:  # noqa: D102
        return None

    def narration_state(self) -> dict:  # noqa: D102
        return {"retracted": sorted(self.retracted)}


async def _noop_flush(engine) -> None:
    """旁挂广播（演员表/数值/诊断）不参与本用例：这里只钉"钟有没有被收回来"。"""
    return None


def _set_fake_engine(worker: SceneWorker, ids: list[int]) -> None:
    """给替身 worker 挂上替身引擎与空 flush（这三样是 loop 里的既有前置条件）。"""
    worker._oplock = asyncio.Lock()
    worker._engine = _FakeEngine(ids)
    worker._flush = _noop_flush


# ==================================================== 撤回/改写：把时间收回来
def test_rewinding_a_retracted_jump_takes_the_time_back(qapp):
    """撤回跳时间行 = 那一跳从没发生过（§6.1）：虚拟钟跟着**收回来**。

    跳时间行是"时间过去了"的唯一凭据（§4.3）。不收回来，界面钟点、下一块提示词里的
    【当前时刻】、时间条件钩子的判定、以及"到次日天亮"的换算就全都还背着一次不存在的
    跳跃——用户读到的转录说"这段没发生过"，钟却说已经 22:00 了。
    """
    worker, clock = _worker_with_clock()
    worker._sweep([_jump_msg(30)])
    assert clock.current() == sc.parse_hhmm("21:30") + 30 * 60

    assert worker._rewind_retracted_jumps([5]) == 30
    assert clock.current() == sc.parse_hhmm("21:30"), "那一跳的 30 分钟被收回"
    assert worker._rewind_retracted_jumps([5]) == 0, "同一条不会收两次（幂等）"
    assert clock.current() == sc.parse_hhmm("21:30")

    worker._sweep([_jump_msg(0, id=6), _jump_msg(-5, id=7),
                   {"id": 8, "speaker": "场景", "speaker_type": "narrator",
                    "content": "灯灭了。"}])
    assert worker._rewind_retracted_jumps([6, 7, 8]) == 0, "没推过钟的行收回 0"
    assert clock.current() == sc.parse_hhmm("21:30")


def test_retracting_a_jump_line_through_the_worker_rewinds_the_clock(qapp):
    """撤回一条（截断式）走的就是 worker 的撤回路径：作废整段里的跳时间行一律收回。"""
    worker, clock = _worker_with_clock()
    worker._sweep([_jump_msg(30, id=5), _jump_msg(60, id=6)])
    assert clock.current() == sc.parse_hhmm("21:30") + 90 * 60

    async def run() -> None:
        _set_fake_engine(worker, [5, 6])             # 撤回 id=5 → 整段（含 6）作废
        await worker._retract_message(5)

    asyncio.run(run())
    assert clock.current() == sc.parse_hhmm("21:30"), "被撤的两条跳时间偏移都收回"


def test_editing_a_jump_line_frees_its_time(qapp):
    """改写 = 截断到该节点 + 新行：被改写掉的跳时间行同样把钟收回来。

    改写是用户的手笔，引擎不替用户把新正文里的钟点解释成一次跳跃（要跳就再让场景提一次
    提案）——钟因此停在"那一跳之前"，不会再叠着一次已经不存在的偏移往后走。
    """
    worker, clock = _worker_with_clock()
    worker._sweep([_jump_msg(30, id=5)])
    assert clock.current() == sc.parse_hhmm("21:30") + 30 * 60

    async def run() -> None:
        _set_fake_engine(worker, [5])
        await worker._edit_narration(5, "5 分钟之后，灯灭了。")

    asyncio.run(run())
    assert clock.current() == sc.parse_hhmm("21:30"), "旧的那一跳作废 → 时间收回来"


def test_a_reset_scene_forgets_the_jump_ledger_without_rewinding(qapp):
    """重置场景**不倒退虚拟钟**（与既有口径一致：钟只按真实流逝 × 流速走），
    但旧账要划掉——否则那些已作废的行会被重复收回。"""
    worker, clock = _worker_with_clock()
    worker._sweep([_jump_msg(30, id=5)])

    async def run() -> None:
        _set_fake_engine(worker, [5])
        await worker._reset_scene()

    asyncio.run(run())
    assert clock.current() == sc.parse_hhmm("21:30") + 30 * 60, "重置不倒拨钟"
    assert worker._jump_applied == {}, "旧账划掉"
    assert worker._rewind_retracted_jumps([5]) == 0, "已作废的行不会被重复收回"


# ==================================================== 开关（缺省关）
def test_time_skip_is_off_by_default(qapp):
    """缺省关：不开启时 worker 不把开关交给引擎（铁律：逐字节不变）。"""
    worker = SceneWorker()
    assert worker._time_skip is False


def test_set_time_skip_records_the_expectation_before_the_loop_starts(qapp):
    """线程未起时只记期望值（此刻仍是 GUI 线程单线程阶段），绝不抛进 GUI 槽。"""
    worker = SceneWorker()
    worker.set_time_skip(True)
    assert worker._time_skip is True
    worker.set_time_skip(False)
    assert worker._time_skip is False


def test_build_engine_hands_the_flag_to_the_engine(tmp_path, monkeypatch, qapp):
    """建引擎时带上期望值（重开/切场不丢）——替身 SceneEngine 只记 kwargs。"""
    worker_mod = __import__("harness.gui.worker", fromlist=["worker"])
    captured: dict = {}
    worker = SceneWorker()
    worker._time_skip = True
    worker._cfg = {"scene": tmp_path / "s.json", "characters": [],
                   "models": tmp_path / "models.yaml", "bid": None, "live": False,
                   "api_key": None, "run_root": tmp_path / "runs",
                   "closing_at_block": None, "start_time": None,
                   "cast_from_cards": False, "characters_dir": None}
    (tmp_path / "models.yaml").write_text("think:\n  backend: stub\n  model: stub\n"
                                          "  params: {}\n", encoding="utf-8")
    monkeypatch.setattr(worker_mod, "SceneEngine",
                        lambda *a, **kw: captured.update(kw) or object())
    monkeypatch.setattr(worker_mod, "_seed_card_libraries", lambda *a, **kw: None)

    asyncio.run(worker._build_engine())

    assert captured["time_skip_enabled"] is True
