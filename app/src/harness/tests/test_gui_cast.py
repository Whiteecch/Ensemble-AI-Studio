"""worker 层人事 API（《场景编排与桌面外壳》§3.3 / §4.1②，S4a）。

用一个**替身引擎**（只实现 worker 人事路径会碰到的读接口 + 五个写方法）驱动
SceneWorker：人事操作必须 (a) 经引擎 loop 投到引擎、(b) 把新落的消息 flush 上屏
（复用 sweep → sig_message）、(c) 广播 sig_cast（cast_state 全量，禁言状态如实反映）。

不跑 autoplay、不联网、不建真引擎：只测「worker 这一层把 GUI 与引擎接对了」。
真引擎的行为见 test_engine_cast.py。
"""
import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from harness.gui.worker import SceneWorker  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    """模块级复用单一 QApplication（Qt 只允许每进程一个）。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _wait_until(qapp, cond, timeout_ms: int = 4000, step_ms: int = 10) -> bool:
    """轮询运行主线程 Qt 事件循环直到 cond() 为真；超时返回 False（不抛）。"""
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


class StubEngine:
    """最小引擎替身：人事路径 + 每次 flush 都会读的那几样（messages/cast/动态/think）。"""

    def __init__(self, cards=("甲", "乙", "丙")):
        self.cards = set(cards)
        self.calls: list[tuple] = []
        self._msgs = [{"id": 0, "speaker": "导演", "speaker_type": "director",
                       "content": "（开场。）", "in_scene": "茶室", "turn": 0}]
        self._active = ["甲", "乙"]
        self._inactive: list[str] = []
        self._muted: dict = {}
        self._activity = 0.5          # 叙述活跃度（§6.2）：setter 改它，narration_state 报它

    # ---- worker 每次 flush 都会读的只读接口 ----
    async def messages(self):
        return list(self._msgs)

    def cast_state(self):
        return {"active": list(self._active), "inactive": list(self._inactive),
                "muted": dict(self._muted)}

    def dynamics_snapshot(self):
        return {n: {"bid": 0.0} for n in self._active}

    def dynamic_states(self):
        return {}

    def think_log_tail(self, n: int = 200):
        return []

    def narration_state(self):
        return {"auto": True, "retracted": [], "activity": self._activity}

    def set_narrate_activity(self, level: float):
        self.calls.append(("narrate_activity", float(level)))
        self._activity = float(level)

    # ---- 人事写接口（与 SceneEngine 同名同签名）----
    def _line(self, content):
        self._msgs.append({"id": len(self._msgs), "speaker": "场景",
                           "speaker_type": "narrator", "content": content,
                           "in_scene": "茶室", "turn": 0})

    async def add_character(self, name, *, notify=None, notify_text="", visible=True):
        self.calls.append(("add", name, list(notify or []), notify_text, visible))
        if name not in self.cards:
            raise ValueError(f"角色卡未装载：{name}")
        if visible:
            self._line(f"（{name}走进了场景。）")
        self._active.append(name)
        return {"content": f"（{name}走进了场景。）"}

    async def remove_character(self, name, *, notify=None, notify_text="", visible=True):
        self.calls.append(("remove", name, list(notify or []), notify_text, visible))
        if visible:
            self._line(f"（{name}离开了场景。）")
        self._active = [n for n in self._active if n != name]
        self._inactive.append(name)

    async def mute_character(self, name, turns: int = 0):
        self.calls.append(("mute", name, turns))
        self._muted[name] = turns if turns > 0 else None

    async def unmute_character(self, name):
        self.calls.append(("unmute", name))
        self._muted.pop(name, None)

    async def schedule_cast_change(self, character_name, action, fire_after_rounds,
                                   notify=None, notify_text="", visible=True, turns=0):
        self.calls.append(("schedule", character_name, action, fire_after_rounds,
                           list(notify or []), notify_text, visible, turns))
        return {"character_name": character_name, "action": action,
                "fire_after_rounds": fire_after_rounds}


@pytest.fixture
def worker_with_engine(qapp):
    """起线程的 worker + 注入替身引擎（不开场、不 autoplay，只走人事路径）。"""
    worker = SceneWorker()
    worker.start()
    assert worker._ready.wait(5.0), "worker 事件循环未起"     # 线程就绪再投递人事操作
    engine = StubEngine()
    msgs: list[dict] = []
    casts: list[dict] = []
    statuses: list[str] = []
    worker.sig_message.connect(msgs.append)
    worker.sig_cast.connect(casts.append)
    worker.sig_status.connect(statuses.append)
    worker._engine = engine          # 线程空转中直写：本测不并发驱动引擎
    try:
        yield worker, engine, msgs, casts, statuses
    finally:
        worker.shutdown(4000)


def test_worker_add_character_emits_cast_and_context_line(worker_with_engine, qapp):
    """add_character → 引擎被调用、播报行走 sig_message 上屏、sig_cast 报出在场名单。"""
    worker, engine, msgs, casts, _ = worker_with_engine
    worker.add_character("丙", notify=["甲"], notify_text="丙来了")

    assert _wait_until(qapp, lambda: any("丙" in c["active"] for c in casts)), \
        "应广播一次含丙的 sig_cast"
    assert engine.calls == [("add", "丙", ["甲"], "丙来了", True)]
    assert any(m["content"] == "（丙走进了场景。）" and m["speaker"] == "场景"
               for m in msgs), "播报行必须经 sig_message 到达界面"
    assert casts[-1]["active"] == ["甲", "乙", "丙"]
    assert casts[-1]["inactive"] == []


def test_worker_remove_mute_unmute_reflected_in_sig_cast(worker_with_engine, qapp):
    """移出 → inactive 列表；禁言 → muted 带剩余块数；解禁 → 该项消失。"""
    worker, engine, _, casts, _ = worker_with_engine
    worker.remove_character("乙")
    assert _wait_until(qapp, lambda: bool(casts) and casts[-1]["active"] == ["甲"])
    assert casts[-1]["inactive"] == ["乙"]

    worker.mute_character("甲", 3)
    assert _wait_until(qapp, lambda: casts[-1]["muted"] == {"甲": 3})
    worker.unmute_character("甲")
    assert _wait_until(qapp, lambda: casts[-1]["muted"] == {})

    worker.mute_character("甲")                  # 永久禁言 → None
    assert _wait_until(qapp, lambda: casts[-1]["muted"] == {"甲": None})
    assert ("mute", "甲", 0) in engine.calls


def test_worker_schedule_cast_change_passes_through(worker_with_engine, qapp):
    """延时角色动作原样投给引擎（含 S4a 增补的 turns）。"""
    worker, engine, _, _, _ = worker_with_engine
    worker.schedule_cast_change("丙", "mute_turns", 2, notify=["乙"],
                                notify_text="稍后静默", visible=False, turns=4)
    assert _wait_until(qapp, lambda: any(c[0] == "schedule" for c in engine.calls))
    assert engine.calls[0] == ("schedule", "丙", "mute_turns", 2, ["乙"],
                               "稍后静默", False, 4)


def test_worker_cast_failure_surfaces_status(worker_with_engine, qapp):
    """引擎报错（卡未装载）不冒泡成未取回的异常：界面看到一行可见状态，worker 不死。"""
    worker, engine, _, _, statuses = worker_with_engine
    worker.add_character("幽灵")
    assert _wait_until(qapp, lambda: any("失败" in s for s in statuses)), \
        "失败必须有可见状态说明"
    assert any("未装载" in s for s in statuses)
    worker.add_character("丙")                   # 失败之后照常可用
    assert _wait_until(qapp, lambda: any("丙" in (c.get("active") or [])
                                         for c in [engine.cast_state()]))


def test_worker_add_character_loads_card_from_library(qapp, tmp_path):
    """**真引擎**端到端（T1，§3.2）：worker 把角色库目录交给引擎 → 本场没装载的卡照加。

    场景文件里一个人都没有（空场，§3.1），起场时只装载了甲的卡；丙的卡只躺在库里
    （同一目录）。add_character("丙") 必须当场从库里装卡并让它进场——这正是「任何库中
    角色都能在任何时刻加入任何场景」的验收口径。
    """
    import json as _json

    scene_p = tmp_path / "茶室.json"
    scene_p.write_text(_json.dumps({"name": "茶室", "characters": []},
                                   ensure_ascii=False), encoding="utf-8")
    a_p = tmp_path / "甲.json"
    a_p.write_text(_json.dumps({"name": "甲", "personality": {"描述": "甲"}},
                               ensure_ascii=False), encoding="utf-8")
    c_p = tmp_path / "丙.json"                     # 只在库里，**不在**本场装载的卡里
    c_p.write_text(_json.dumps({"name": "丙", "personality": {"描述": "丙"}},
                               ensure_ascii=False), encoding="utf-8")
    models_p = tmp_path / "models.yaml"
    models_p.write_text("think:\n  backend: stub\n  model: stub\n  params: {}\n"
                        "speak:\n  backend: stub\n  model: stub\n  params: {}\n",
                        encoding="utf-8")
    bid_p = tmp_path / "bid.yaml"
    bid_p.write_text("interruption_threshold: 0.0\nspeak_threshold: 0.0\n"
                     "silence_k: 100000\n", encoding="utf-8")

    worker = SceneWorker()
    worker.start()
    assert worker._ready.wait(5.0)
    casts: list[dict] = []
    msgs: list[dict] = []
    worker.sig_cast.connect(casts.append)
    worker.sig_message.connect(msgs.append)
    try:
        worker.start_scene(scene_p, [a_p], models_p, live=False, bid=bid_p,
                           opening="静场。", run_root=tmp_path / "rr",
                           api_key=None, closing_at_block=100000)
        assert _wait_until(qapp, lambda: bool(casts), 6000), "应收到演员表广播"
        assert "丙" not in worker._engine.cards, "前置：丙的卡本场没装载"
        worker.add_character("丙")
        assert _wait_until(qapp, lambda: any("丙" in (c.get("active") or [])
                                             for c in casts), 6000), \
            "库里的卡应按需装载并进场"
        assert any("丙" in (m.get("content") or "") for m in msgs), \
            "进场播报行照常上屏"
    finally:
        worker.shutdown(4000)


def test_worker_cast_ops_are_silent_before_thread_start(qapp):
    """线程未起（或已退出）时人事操作静默忽略——绝不把异常抛进 GUI 槽。"""
    worker = SceneWorker()                       # 只构造不 start
    worker.add_character("丙")
    worker.remove_character("丙")
    worker.mute_character("丙")
    worker.unmute_character("丙")
    worker.schedule_cast_change("丙", "add", 1)
    assert worker.can_cast() is False


# ============================================== 推进活跃度（《界面与场景自动化》§6.2）
def test_worker_set_narrate_activity_reaches_engine_and_narration_payload(
        worker_with_engine, qapp):
    """活跃度经 worker 落到引擎（活改），并由 sig_narration 的载荷带回界面镜像。"""
    worker, engine, _, _, _ = worker_with_engine
    narrations: list[dict] = []
    worker.sig_narration.connect(narrations.append)

    worker.set_narrate_activity(0.8)

    assert _wait_until(qapp, lambda: ("narrate_activity", 0.8) in engine.calls),         "活跃度必须投到引擎 loop 并落到引擎上"
    assert worker._narrate_activity == 0.8, "worker 手里的期望值与引擎一致"
    assert _wait_until(
        qapp, lambda: bool(narrations) and narrations[-1].get("activity") == 0.8),         "sig_narration 载荷应带当前活跃度（界面据此刻度盘）"


def test_worker_set_narrate_activity_clamps_and_ignores_garbage(worker_with_engine, qapp):
    """越界夹到 0..1；非数字忽略（界面已吸附四档，worker 只守不崩）。"""
    worker, engine, _, _, _ = worker_with_engine
    worker.set_narrate_activity(9)
    assert _wait_until(qapp, lambda: ("narrate_activity", 1.0) in engine.calls)
    worker.set_narrate_activity(-3)
    assert _wait_until(qapp, lambda: ("narrate_activity", 0.0) in engine.calls)
    before = len(engine.calls)
    worker.set_narrate_activity("不是数字")
    assert not _wait_until(qapp, lambda: len(engine.calls) > before, 200), \
        "非数字不产生任何新调用（界面已吸附四档，这里只守不崩）"
    assert worker._narrate_activity == 0.0, "非法值不覆盖上一次的有效档位"


def test_worker_narrate_activity_is_an_expectation_that_survives_rebuild(tmp_path):
    """活跃度与语言/自动保存同纪律：worker 记期望值，**重建引擎时带上**（重开/切场不丢）。"""
    import asyncio

    import harness.gui.worker as wm

    seen: dict = {}

    def _fake_engine(*args, **kwargs):
        seen.update(kwargs)
        return object()

    worker = SceneWorker()
    worker.set_narrate_activity(0.8)          # 线程未起 → 只记期望值（此刻仍在 GUI 线程）
    assert worker._narrate_activity == 0.8
    worker._cfg = {
        "scene": tmp_path / "s.json", "characters": [tmp_path / "c.json"],
        "models": tmp_path / "models.yaml", "bid": None, "live": False,
        "api_key": None, "run_root": tmp_path / "runs",
        "closing_at_block": None, "start_time": None, "cast_from_cards": False,
    }
    original = wm.SceneEngine
    wm.SceneEngine = _fake_engine
    try:
        asyncio.run(worker._build_engine())
    finally:
        wm.SceneEngine = original
    assert seen["narrate_activity"] == 0.8, "建引擎时应带上上次的活跃度档"

    fresh = SceneWorker()                     # 从没设过 → 缺省档 0.5（与设置里的默认同一口径）
    assert fresh._narrate_activity == 0.5


def test_worker_set_narrate_activity_silent_before_thread_start(qapp):
    """线程未起/已关闭时设置活跃度不抛异常（同人事操作的纪律）。"""
    worker = SceneWorker()
    worker.set_narrate_activity(0.2)
    assert worker._narrate_activity == 0.2
