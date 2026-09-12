"""流式开口（《人际关系与场景推进》§二）的界面侧：worker 增量派发 + 匀速吐字。

契约（逐条对应设计文档 §2.1~§2.6 与 §7.N1 的验收行）：

  · worker 在**节拍循环**里增量读引擎的只读接收器（`speak_stream_tail`），把新片转成
    跨线程信号（`sig_speak_delta`）与收尾信号（`sig_speak_end`）——"已读到哪"靠 seq
    单调递增，与 `think_log_tail`/`_last_scene_change` 同一纪律；
  · **新场必须归零**：换场/重开走的 `_launch` 会把已读到的 seq 重置（同类计数器刚栽过
    一次，别再犯）；
  · **接收侧只攒不画**（§2.4）：片进 `_stream_raw`，一次界面都不碰——跟着网络分片重绘
    就是一顿一顿的；
  · **全量收完才开吐**：`settled=True` 的收尾标记一到才起 QTimer 匀速吐字（每
    `STREAM_TICK_MS` 一跳、每跳重绘一次；长台词按 `STREAM_MAX_MS` 加速）；
  · **收尾无缝**：正式消息到达时若吐字未完就**先扣住**（不入留存、不上屏），吐完那一刻
    换成正式行；吐字中的 HTML 与正式气泡**逐字节相同**（复用 `_format_message`）；
  · 该块判为复读（settled=False）→ **不吐、直接撤**，绝不留半条消息；
  · 换场/关窗/撤回 → 定时器停、气泡清（定时器绝不活过它属于的那一块）；
  · 流式关着（缺省）→ 没有定时器、没有缓冲、没有扣留，喂给 `setHtml` 的那串与今天逐字节
    相同。

全程离屏（`QT_QPA_PLATFORM=offscreen`）、不联网、不碰真实用户设置（default_settings_path
一律指向 tmp_path）。**吐字一律手动驱动**（`_stream_tick`），不依赖 40ms 的真实等待。
"""
import asyncio
import json
import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QObject, QTimer, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from harness.gui.main_window import (  # noqa: E402
    STREAM_MAX_MS, STREAM_MAX_TICKS, STREAM_TICK_MS, AppConfig, MainWindow)
from harness.gui.settings import (  # noqa: E402
    DEFAULT_STREAM_SPEAK, AppSettings, SettingsStore)
from harness.gui.worker import SceneWorker  # noqa: E402


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
    """默认设置路径指到临时目录（窗口构造与 apply_* 都只写这里）。"""
    from harness.gui import settings as settings_mod
    path = tmp_path / "settings" / "settings.json"
    monkeypatch.setattr(settings_mod, "default_settings_path", lambda: path)
    return path


# ======================================================= worker：增量读与派发


class _StreamStub:
    """只有 `speak_stream_tail` 的替身引擎（worker 的增量读只认这一个口）。"""

    def __init__(self, entries: list[dict]) -> None:
        self.entries = entries
        self.calls = 0

    def speak_stream_tail(self, n: int = 200) -> list[dict]:
        self.calls += 1
        return [dict(e) for e in self.entries[-n:]]


def _pieces(speaker: str, texts: list[str], settled: bool, *,
            start: int = 1) -> list[dict]:
    """造一串接收器条目：若干片 + 一条收尾标记（seq 从 start 起，与 graph 同形）。

    seq 是**全引擎单调**的（跨块续着走），故第二块要显式给 start，别从头再数。
    """
    out: list[dict] = []
    for i, text in enumerate(texts, start=start):
        out.append({"seq": i, "speaker": speaker, "turn": 1, "kind": "piece",
                    "text": text, "settled": False})
    out.append({"seq": start + len(texts), "speaker": speaker, "turn": 1,
                "kind": "end", "text": "", "settled": settled})
    return out


def test_worker_forwards_stream_deltas_and_end_exactly_once(qapp):
    """片 → sig_speak_delta、收尾 → sig_speak_end；**同一份条目重复读不重复派发**。

    每拍（ticker）与每次 flush 都会读一次接收器，靠"已读到的 seq"去重——不去重的话
    界面会把同一片攒两遍，吐出来的那句就多出一截（实况里最难自查的一类）。
    """
    worker = SceneWorker()
    deltas: list[dict] = []
    ends: list[dict] = []
    worker.sig_speak_delta.connect(deltas.append)
    worker.sig_speak_end.connect(ends.append)
    engine = _StreamStub(_pieces("甲", ["你好", "，世界"], settled=False))

    worker._emit_speak_stream(engine)
    worker._emit_speak_stream(engine)        # 再读一遍同一份 → 一条都不该再派
    assert [d["text"] for d in deltas] == ["你好", "，世界"]
    assert [d["speaker"] for d in deltas] == ["甲", "甲"]
    # 收尾标记还带当前虚拟钟时刻（§2.4：窗口拿它在吐字第一帧就显示时间）。这个 worker
    # 没持钟（未开场）→ 空串；持钟的真实路径由 test_the_timestamp_is_there_… 覆盖。
    assert ends == [{"speaker": "甲", "settled": False, "turn": 1, "seq": 3,
                     "time_hhmmss": ""}]

    engine.entries = engine.entries + _pieces("乙", ["换人"], settled=True, start=4)
    worker._emit_speak_stream(engine)        # 新增的条目照常派发（seq 更大）
    assert [d["text"] for d in deltas][-1] == "换人"
    assert ends[-1]["speaker"] == "乙" and ends[-1]["settled"] is True


def test_worker_tolerates_engines_without_the_stream_reader(qapp):
    """替身引擎（老 worker 的测试与旧引擎）没有这个读口 → 静默跳过，绝不冒泡。"""
    worker = SceneWorker()

    class _NoStream:
        pass

    worker._emit_speak_stream(_NoStream())   # 不抛即通过
    assert worker._last_speak_seq == -1


class _FlushStub:
    """够跑 `worker._flush` 的替身引擎：只有 `messages()` 与 `speak_stream_tail()`。

    其余读口（cast_state/scene_state/think_log_tail…）一概不提供——`_flush` 里每一处
    都是 try/except 兜底，缺面就当没有（这正是既有契约）。
    """

    def __init__(self, msgs: list[dict], entries: list[dict]) -> None:
        self._msgs = msgs
        self._entries = entries

    async def messages(self) -> list[dict]:
        return [dict(m) for m in self._msgs]

    def speak_stream_tail(self, n: int = 200) -> list[dict]:
        return [dict(e) for e in self._entries[-n:]]


def test_flush_dispatches_the_stream_pieces_before_the_block_message(qapp):
    """实况次序：`_flush` 必须先派流式片（含收尾标记）**再**派正式消息。

    这不是审美偏好，而是**引擎自己的时序**：`graph.speak` 先把片与收尾标记追加进接收器，
    之后才把这一块交给引擎落进 `messages()`（`_append_speak_stream(settled=True)` 在构造
    block 之前）。worker 若反着发（先 sig_message 再补派余片），正式行已经落地、余片又来，
    收尾标记一到就会把那截余片开成一条吐字气泡——同一句在屏上出现两遍（一遍正式、一遍
    重影），还会把下一个块同一说话人的片继续拼进去（实测两句话粘成一句）。窗口侧还有
    (speaker, turn) 那道闸兜底（见下面的幽灵用例），但**次序本身**是这里钉住的。
    """
    worker = SceneWorker()
    events: list[tuple] = []
    worker.sig_speak_delta.connect(lambda p: events.append(("delta", p["speaker"])))
    worker.sig_speak_end.connect(
        lambda p: events.append(("end", p["speaker"], p["settled"])))
    worker.sig_message.connect(lambda m: events.append(("msg", m["speaker"])))
    engine = _FlushStub(
        [{"id": 1, "speaker": "甲", "speaker_type": "character",
          "content": "你好，世界。", "in_scene": "贝克街221B", "turn": 0}],
        _pieces("甲", ["你好", "，世界。"], settled=True))

    asyncio.run(worker._flush(engine))

    assert events[-1] == ("msg", "甲"), "正式消息必须是最后到的（窗口就地定稿）"
    assert events[:-1] == [("delta", "甲"), ("delta", "甲"), ("end", "甲", True)], \
        "片与收尾标记必须整体先于正式消息（引擎的时序：先追加接收器，后落 block）"


def test_launch_resets_the_stream_read_cursor(tmp_path, monkeypatch, qapp):
    """**新场必须归零**：`_launch` 把已读到的 seq 重置（否则新场的头几片会被当成"读过了"）。

    真跑那条 `_launch` 路径（起真实引擎太慢，这里换成替身引擎 + 记账桩任务）——与
    `test_two_overlapping_launches…` 同一套驱动方式：它钉的是同一个坑（计数器的生命周期
    挂在**引擎实例**上，而引擎是每场换一个的）。
    """
    worker_mod = __import__("harness.gui.worker", fromlist=["worker"])
    worker = SceneWorker()

    class _Engine:
        def __init__(self) -> None:
            self.closed = False
            self.start_seconds = 77400
            self.start_hhmm = "21:30"
            self.boundary_hhmm = None
            self.boundary_seconds = None

        async def open_scene(self, opening):
            return None

        async def aclose(self):
            self.closed = True

        async def messages(self):
            return []

        def narration_state(self):
            return {"auto": True}

        def cast_state(self):
            return {"active": [], "inactive": [], "muted": {}}

        def dynamics_snapshot(self):
            return {}

        def dynamic_states(self):
            return {}

        def think_log_tail(self, n=0):
            return []

        def speak_stream_tail(self, n=0):
            return []

        def metrics(self):
            return {}

    started: list = []

    async def _autoplay(self, engine):
        started.append(engine)

    async def _ticker(self):
        started.append("ticker")

    monkeypatch.setattr(worker_mod, "SceneEngine", lambda *a, **k: _Engine())
    monkeypatch.setattr(worker_mod, "build_scene_payload",
                        lambda engine: {"backend": "stub"})
    monkeypatch.setattr(SceneWorker, "_autoplay", _autoplay)
    monkeypatch.setattr(SceneWorker, "_v_ticker_loop", _ticker)
    worker._cfg = {"scene": tmp_path / "s.json", "characters": [tmp_path / "c.json"],
                   "models": tmp_path / "models.yaml", "bid": None, "live": False,
                   "api_key": None, "run_root": tmp_path / "runs",
                   "closing_at_block": None, "start_time": None,
                   "cast_from_cards": False, "characters_dir": None}

    async def _drive():
        worker._oplock = asyncio.Lock()
        await worker._launch(None)

    worker._last_speak_seq = 99          # 上一场读到的位置（新场必须作废）
    worker._last_think_seq = 42
    asyncio.run(_drive())

    assert worker._last_speak_seq == -1, "新场的流式读游标必须归零"
    assert worker._last_think_seq == -1, "think 日志游标同理（既有纪律）"


def test_worker_records_the_stream_expectation(qapp, tmp_path, monkeypatch):
    """开关是"期望值"（与 set_language/set_auto_narrate 同纪律）：线程未起时只记值、不抛。

    可随时点（含开场前），建引擎时带上——故重开/切场不丢。
    """
    worker = SceneWorker()               # 线程未起：设置一律走"只记期望值"那条路
    assert worker._stream_speak is DEFAULT_STREAM_SPEAK, "缺省取产品缺省（现为开）"
    worker.set_speak_stream(False)
    assert worker._stream_speak is False
    worker.set_speak_stream(True)
    assert worker._stream_speak is True


# ======================================================= 窗口：临时气泡


class _FakeWorker(QObject):
    """替身 worker：只提供 MainWindow 要连的信号（不起线程、不建引擎）。"""

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
    sig_speak_delta = Signal(dict)
    sig_speak_end = Signal(dict)

    def __init__(self) -> None:
        super().__init__()
        self.stream_flags: list[bool] = []

    def can_cast(self) -> bool:
        return False

    def set_speak_stream(self, on: bool) -> None:
        self.stream_flags.append(bool(on))


def _make_window(tmp_path: Path, *, settings: AppSettings | None = None):
    """真实 MainWindow 装配（替身 worker，不 show、不起线程），返回 (窗口, 替身 worker)。"""
    cards = tmp_path / "characters"
    scenes = tmp_path / "scenes"
    cards.mkdir(parents=True, exist_ok=True)
    scenes.mkdir(parents=True, exist_ok=True)
    card_paths = []
    for name in ("甲", "乙"):
        p = cards / f"{name}.json"
        p.write_text(json.dumps({"name": name, "personality": {"描述": "示例"}},
                                ensure_ascii=False), encoding="utf-8")
        card_paths.append(p)
    scene = scenes / "贝克街221B.json"
    scene.write_text(json.dumps({"name": "贝克街221B", "participants": ["甲", "乙"]},
                                ensure_ascii=False), encoding="utf-8")
    models = tmp_path / "models.yaml"
    models.write_text("think: {backend: stub, model: stub, params: {}}\n"
                      "speak: {backend: stub, model: stub, params: {}}\n",
                      encoding="utf-8")
    cfg = AppConfig(scene=scene, characters=card_paths, models=models,
                    bid=tmp_path / "bid.yaml", run_root=tmp_path / "runs",
                    live=False, api_key=None, opening="夜里。")
    worker = _FakeWorker()
    win = MainWindow(worker, cfg, settings=settings)
    return win, worker


def _open(win, worker, name: str = "贝克街221B") -> None:
    """把窗口推进到「场景已开」（不需要引擎：载荷由测试自己造）。"""
    worker.sig_scene_info.emit({"scene": {"name": name, "participants": ["甲", "乙"]},
                                "characters": [], "backend": "stub"})


#: 手动驱动吐字的辅助：真实 QTimer 只在事件循环里跑，而这些用例从不跑它——于是"吐字在
#: 推进"这件事由测试自己驱动（确定、快，而且与 `STREAM_TICK_MS` 的具体取值无关）。


def _stop_real_timer(win) -> None:
    """停掉吐字表，改手动驱动（顺带证明"起过表"）。"""
    win._stop_stream_timer()
    assert not _timer_active(win)


def _timer_active(win) -> bool:
    """吐字表是不是在跑（还没建过表 = 没在跑）。"""
    return win._stream_timer is not None and win._stream_timer.isActive()


def _start_drip(win, worker, *, speaker="甲", turn=1, text="半句话在这里。", seq=0) -> str:
    """造一个"正在吐字"的现场：片 + settled=True 的收尾标记（返回整句）。"""
    worker.sig_speak_delta.emit({"speaker": speaker, "text": text,
                                 "turn": turn, "seq": seq + 1})
    worker.sig_speak_end.emit({"speaker": speaker, "settled": True,
                               "turn": turn, "seq": seq + 2})
    assert win._stream is not None and _timer_active(win), "收尾标记一到就该开吐"
    return text


def _drain(win, limit: int = 5000) -> int:
    """把吐字推到底（吐完/被换出去即停）：返回推进了多少跳。"""
    ticks = 0
    while win._stream is not None and not win._stream_exhausted() and ticks < limit:
        win._stream_tick()
        ticks += 1
    return ticks


def _count_renders(win) -> list:
    """把整屏重绘记成账（"片只攒不画"的直接证据）。返回逐次累计的列表。"""
    calls: list = []
    orig = win._rerender_conversation
    win._rerender_conversation = lambda: (calls.append(1), orig())[1]
    return calls


def test_deltas_only_buffer_and_never_touch_the_view(qapp, tmp_path, tmp_store):
    """§2.4 接收侧：片只进缓冲，**一次界面都不碰**（不重绘、不改屏）。

    这是"不卡"的直接证据：SSE 的分片是突发到达的，跟着它重绘就是一顿一顿的；把"攒"与
    "画"分开之后，网络读得再抖，屏上都匀速。
    """
    win, worker = _make_window(tmp_path)
    _open(win, worker)
    calls = _count_renders(win)
    before = win._view.toPlainText()

    for i, text in enumerate(("我", "想说", "一句话。"), start=1):
        worker.sig_speak_delta.emit({"speaker": "甲", "text": text, "turn": 1, "seq": i})

    assert calls == [], "片到达时一次重绘都不许有"
    assert win._view.toPlainText() == before, "屏幕逐字节没变"
    assert win._stream_raw == {"speaker": "甲", "turn": 1, "text": "我想说一句话。"}, \
        "片进的是接收侧缓冲（换块时按 (speaker, turn) 分开）"
    assert win._stream is None, "收尾标记没到，屏上不该有气泡"


def test_settled_end_starts_the_drip_and_advances_one_char_per_tick(qapp, tmp_path,
                                                                    tmp_store):
    """§2.4 显示侧：`settled=True` 到达才开吐 → 短句每跳 1 个字 → 每跳重绘一次。

    "全量收完才开吐"：片阶段屏上一片空白（只攒），收尾标记一到气泡才出现，节奏与网络无关。
    """
    win, worker = _make_window(tmp_path)
    _open(win, worker)
    line = "五字台词在这里。"          # 8 字 → 8 跳
    worker.sig_speak_delta.emit({"speaker": "甲", "text": line, "turn": 1, "seq": 1})

    worker.sig_speak_end.emit({"speaker": "甲", "settled": True, "turn": 1, "seq": 2})
    assert _timer_active(win), "收尾标记一到就该起表（这就是「开吐」）"
    assert win._stream is not None and win._stream["full"] == line
    assert win._stream["text"] == line[:1], "立刻吐第一跳（屏上别先空一拍）"
    assert win._stream_raw is None, "缓冲已抬成吐字状态"

    calls = _count_renders(win)
    win._stream_tick()
    assert win._stream["text"] == line[:2], "短句每跳 1 个字"
    assert len(calls) == 1, "每跳重绘一次"
    assert line[:2] in win._view.toPlainText()

    _drain(win)
    assert win._stream_exhausted(), "整句都吐出来了"
    assert not _timer_active(win), "吐完就停表"
    assert win._stream["text"] == line, "一个字都不许吞"


def test_the_timestamp_is_there_from_the_very_first_frame(qapp, tmp_path, tmp_store):
    """§2.4：时间戳从**吐字第一帧**就在（不许"说着说着时间才冒出来"）。

    开吐那一帧比正式消息早几十毫秒（worker 先派收尾标记、再 sweep 消息）。若时刻只等正式
    消息来了才补，用户看到的就是一行没有时间的台词、过一会儿时间才出现。故时刻由 **worker
    在收尾标记里一并给出**（钟归它持有），窗口开吐时就用它；正式消息到达后 `_hold_message`
    采纳它自己的时刻（同一个值）→ 换行那一帧逐字节不变，时间不会跳。
    """
    win, worker = _make_window(tmp_path)
    _open(win, worker)
    line = "五字台词在这里。"
    worker.sig_speak_delta.emit({"speaker": "甲", "text": line, "turn": 1, "seq": 1})
    worker.sig_speak_end.emit({"speaker": "甲", "settled": True, "turn": 1, "seq": 2,
                               "time_hhmmss": "21:30:05"})

    assert win._stream is not None
    assert win._stream["time_hhmmss"] == "21:30:05", "第一帧就得有时间（不等正式消息）"
    assert "21:30:05" in win._view.toPlainText(), "而且它得真的画在屏上"

    before = win._view.toPlainText()
    worker.sig_message.emit({"id": 7, "speaker": "甲", "speaker_type": "character",
                             "content": line, "in_scene": "贝克街221B", "turn": 1,
                             "time_hhmmss": "21:30:05"})
    assert win._view.toPlainText() == before, "正式消息到达那一帧逐字节不变（时间不跳）"

    _drain(win)
    assert "21:30:05" in win._view.toPlainText(), "定稿之后时间仍在"


def test_message_is_held_and_swapped_in_at_the_last_tick(qapp, tmp_path, tmp_store):
    """§2.4 收尾：正式消息到达时吐字未完 → **先扣住**，吐完那一刻无缝换成正式行。

    扣住的理由很直接：屏上正挂着"吐到一半的气泡"，此刻再把完整的正式行也画上去，就是同一
    句话在屏上出现两遍。判据是"屏上正文只出现一次"。
    **扣住 = 不按正式行渲染，不是不入留存**：它到达的当刻就进留存（插在气泡该在的位置上），
    只是渲染时改画气泡——否则扣住窗口里上屏的任何一行都会排到它前面（见
    test_a_line_landing_mid_drip_keeps_the_engine_order）。
    """
    win, worker = _make_window(tmp_path)
    _open(win, worker)
    line = "这一句够长，好让吐字停在半路。"
    worker.sig_speak_delta.emit({"speaker": "甲", "text": line, "turn": 1, "seq": 1})
    worker.sig_speak_end.emit({"speaker": "甲", "settled": True, "turn": 1, "seq": 2})
    assert win._stream["text"] == line[:1]

    worker.sig_message.emit({"id": 7, "speaker": "甲", "speaker_type": "character",
                             "content": line, "in_scene": "贝克街221B", "turn": 1,
                             "time_hhmmss": "21:30:05"})
    assert win._held_msg is not None, "吐字中到达的正式消息先扣住"
    assert [m.get("id") for m in win._conv_msgs] == [7], \
        "扣住的那条当刻入留存（位置也是最终位置），只是还不按正式行渲染"
    assert line not in win._view.toPlainText(), "完整的正式行此刻不许上屏（否则正文两遍）"
    assert win._stream["time_hhmmss"] == "21:30:05", "已知时刻戳补进气泡（换上那一帧同形）"
    assert win._last_msg_id() == 7, "扣住的那条就是屏上最后一条（插话的 after_id 指向它）"

    while win._stream is not None:
        win._stream_tick()
    assert win._held_msg is None, "换出去之后不该还扣着"
    assert win._conv_msgs[-1]["id"] == 7, "吐完那一刻扣住的那条入留存"
    text = win._view.toPlainText()
    assert text.count(line) == 1, "正文只出现一次（既没重复也没消失）"
    assert not _timer_active(win), "换完就停表"


def test_streaming_html_is_byte_identical_to_the_official_bubble(qapp, tmp_path,
                                                                 tmp_store):
    """§2.5：吐字中的那一条与正式角色气泡**逐字节同一套 HTML**（复用 `_format_message`）。

    做法就是"构造伪消息喂给正式渲染函数"，所以这里直接对拍两段 HTML 字符串——某天有人把
    吐字的样式再抄一遍，这条就红。"同形"因此是结构保证的，不靠人工对齐。
    """
    win, worker = _make_window(tmp_path)
    _open(win, worker)
    line = "同一句话，同一套 HTML。"
    official = {"id": 8, "speaker": "甲", "speaker_type": "character",
                "content": line, "in_scene": "贝克街221B", "turn": 1,
                "time_hhmmss": "21:30:05"}
    worker.sig_speak_delta.emit({"speaker": "甲", "text": line, "turn": 1, "seq": 1})
    worker.sig_speak_end.emit({"speaker": "甲", "settled": True, "turn": 1, "seq": 2})
    worker.sig_message.emit(dict(official))          # 扣住（并把时刻戳补进气泡）

    win._stream["text"] = win._stream["full"]        # 取"整句都吐出来"那一帧的形态
    assert win._format_streaming() == win._format_message(dict(official)), \
        "吐字形态必须复用正式气泡那一套（逐字节相同）"

    # 时刻戳未知（正式消息还没到）时两边同样不带——绝不硬造一个。
    unknown = dict(official)
    unknown.pop("time_hhmmss")
    win._stream["time_hhmmss"] = ""
    assert win._format_streaming() == win._format_message(unknown)


def test_settled_false_never_shows_a_bubble_and_leaves_no_half_line(qapp, tmp_path,
                                                                   tmp_store):
    """判为复读（settled=False）→ **不吐、直接撤**，半条消息都不许留（§2.3）。"""
    win, worker = _make_window(tmp_path)
    _open(win, worker)
    worker.sig_speak_delta.emit({"speaker": "乙", "text": "同一句话。", "turn": 1, "seq": 1})
    worker.sig_speak_end.emit({"speaker": "乙", "settled": False, "turn": 1, "seq": 2})
    assert win._stream is None, "被抑制的块：气泡撤掉、字也不吐"
    assert win._stream_raw is None, "缓冲一并丢掉（绝不留着下次接着吐）"
    assert not _timer_active(win)
    assert "同一句话。" not in win._view.toPlainText(), "绝不留半条消息"
    assert win._last_msg_id() is None, "留存里不该多出一条没有 id 的残留"


def test_settled_false_mid_drip_drops_the_bubble_at_once(qapp, tmp_path, tmp_store):
    """吐字中途收到 settled=False（同一块）→ 立刻停表、撤气泡，不留残句（§2.3）。"""
    win, worker = _make_window(tmp_path)
    _open(win, worker)
    line = _start_drip(win, worker)
    worker.sig_speak_end.emit({"speaker": "甲", "settled": False, "turn": 1, "seq": 3})
    assert win._stream is None and win._stream_raw is None
    assert not _timer_active(win), "撤气泡必须连表一起停"
    assert line[:1] not in win._view.toPlainText()


def test_settled_false_of_another_block_does_not_kill_the_running_drip(qapp, tmp_path,
                                                                      tmp_store):
    """作废**只撤这一块**：另一块的 settled=False 不许掐掉正在吐的这句。

    块挨得近时（同一说话人的下一块尤其容易混），连带撤掉就是"话说到一半突然没了"。
    """
    win, worker = _make_window(tmp_path)
    _open(win, worker)
    line = _start_drip(win, worker, speaker="甲", turn=1)
    worker.sig_speak_end.emit({"speaker": "乙", "settled": False, "turn": 2, "seq": 9})
    assert win._stream is not None and win._stream["full"] == line, \
        "乙那一块作废，与甲正在吐的这句无关"
    assert _timer_active(win)


def test_the_real_timer_actually_advances_the_drip(qapp, tmp_path, tmp_store,
                                                   monkeypatch):
    """真表接线那一段（其余用例走手动 tick）：QTimer 到点真的会推吐字、吐完真的会停。

    把节拍压到 1ms（只改这一个常量，上限 `STREAM_MAX_TICKS` 不动）——既验了接线，又不用
    让测试等 40ms × N。
    """
    mw_mod = __import__("harness.gui.main_window", fromlist=["main_window"])
    monkeypatch.setattr(mw_mod, "STREAM_TICK_MS", 1)
    win, worker = _make_window(tmp_path)
    _open(win, worker)
    line = "真表在推这一句。"          # 8 字 → 8 跳 × 1ms
    worker.sig_speak_delta.emit({"speaker": "甲", "text": line, "turn": 1, "seq": 1})
    worker.sig_speak_end.emit({"speaker": "甲", "settled": True, "turn": 1, "seq": 2})
    assert _timer_active(win)

    assert _wait_until(qapp, lambda: win._stream_exhausted(), timeout_ms=4000), \
        "真表到点必须真的把吐字推到底"
    assert win._stream["text"] == line
    assert not _timer_active(win), "吐完必须停表（真表也一样）"
    assert line in win._view.toPlainText()


def test_switching_speaker_mid_drip_leaves_no_residue(qapp, tmp_path, tmp_store):
    """吐字中途换人说话：上一句照常收尾（换出正式行），新的一句从自己的缓冲重新开吐。

    屏上任何时刻**只该有一条**吐字气泡；上一句若因换人被丢掉，就是"话说到一半没了"
    （实况里最常见的观感事故）。两句都必须在屏上各出现一次、一次不多一次不少。
    """
    win, worker = _make_window(tmp_path)
    _open(win, worker)
    first = "甲的第一句，够长，好让吐字停在半路。"
    second = "乙接话，这一句也够长，同样停在半路。"

    worker.sig_speak_delta.emit({"speaker": "甲", "text": first, "turn": 1, "seq": 1})
    worker.sig_speak_end.emit({"speaker": "甲", "settled": True, "turn": 1, "seq": 2})
    assert win._stream["speaker"] == "甲"
    # 甲的话还没吐完、乙的片就到了（换人说话）：乙的片进自己的缓冲，屏上仍是甲那条。
    worker.sig_speak_delta.emit({"speaker": "乙", "text": second, "turn": 2, "seq": 3})
    assert win._stream["speaker"] == "甲", "屏上只该有一条气泡（还没轮到乙）"
    assert win._stream_raw == {"speaker": "乙", "turn": 2, "text": second}

    # 甲的正式消息到达 → 扣住 → 吐完换成正式行。
    worker.sig_message.emit({"id": 21, "speaker": "甲", "speaker_type": "character",
                             "content": first, "in_scene": "贝克街221B", "turn": 1})
    while win._stream is not None:
        win._stream_tick()
    assert [m["id"] for m in win._conv_msgs if m.get("id")] == [21]

    # 乙收尾 → 开吐 → 正式消息到达 → 换成正式行。全程一枚表、最后停着。
    worker.sig_speak_end.emit({"speaker": "乙", "settled": True, "turn": 2, "seq": 4})
    assert win._stream["speaker"] == "乙"
    worker.sig_message.emit({"id": 22, "speaker": "乙", "speaker_type": "character",
                             "content": second, "in_scene": "贝克街221B", "turn": 2})
    while win._stream is not None:
        win._stream_tick()

    text = win._view.toPlainText()
    assert text.count(first) == 1 and text.count(second) == 1, "两句各出现一次"
    assert not _timer_active(win), "收尾之后定时器必须停着（不泄漏）"
    assert win._held_msg is None and win._stream_raw is None


def test_scene_switch_stops_the_drip_and_clears_the_bubble(qapp, tmp_path, tmp_store):
    """换场 → 定时器停、气泡清：定时器绝不活过它属于的那一块（§2.4）。"""
    win, worker = _make_window(tmp_path)
    _open(win, worker)
    line = _start_drip(win, worker)
    worker.sig_scene_info.emit({"scene": {"name": "茶室", "participants": ["甲"]},
                                "characters": [], "backend": "stub"})
    assert win._stream is None and win._stream_raw is None and win._held_msg is None
    assert not _timer_active(win), "换场必须停表"
    assert line[:1] not in win._view.toPlainText(), "上一场的残句绝不带到新场"


def test_close_scene_stops_the_drip(qapp, tmp_path, tmp_store):
    """关窗（close_scene）→ 定时器停、气泡清、屏上不留残句。"""
    win, worker = _make_window(tmp_path)
    _open(win, worker)
    _start_drip(win, worker)
    win.close_scene()
    assert win._stream is None and win._held_msg is None
    assert not _timer_active(win)
    assert win._view.toPlainText() == ""


def test_retraction_of_the_held_message_drops_the_bubble(qapp, tmp_path, tmp_store):
    """扣住的那条被作废（回溯式撤回）→ 气泡一并撤，绝不留残句、更不许换出去。"""
    win, worker = _make_window(tmp_path)
    _open(win, worker)
    line = _start_drip(win, worker)
    worker.sig_message.emit({"id": 11, "speaker": "甲", "speaker_type": "character",
                             "content": line, "in_scene": "贝克街221B", "turn": 1})
    assert win._held_msg is not None
    worker.sig_retracted.emit(11)
    assert win._stream is None and win._held_msg is None
    assert not _timer_active(win)
    assert line not in win._view.toPlainText()
    assert all(m.get("id") != 11 for m in win._conv_msgs), "作废的那条绝不入留存"


def test_scene_finish_keeps_the_held_line_and_stops_the_timer(qapp, tmp_path, tmp_store):
    """收束（sig_finished）落在吐字中间：扣住的那条**照旧换进留存**，气泡与表收干净。

    扣住的那条是权威的整段正文（不是半条话，只是碰巧还在吐）——收束把它吞掉就是"一句已经
    说完的台词因为世界停了而消失"，屏上与留存都会缺一块。
    """
    win, worker = _make_window(tmp_path)
    _open(win, worker)
    line = _start_drip(win, worker)
    worker.sig_message.emit({"id": 31, "speaker": "甲", "speaker_type": "character",
                             "content": line, "in_scene": "贝克街221B", "turn": 1})
    assert win._held_msg is not None

    worker.sig_finished.emit()
    assert [m["id"] for m in win._conv_msgs if m.get("id")] == [31], "已落地的台词不许被吞"
    assert win._stream is None and win._held_msg is None and win._stream_raw is None
    assert not _timer_active(win)
    assert win._view.toPlainText().count(line) == 1


def test_authoritative_retraction_list_also_clears_the_held_bubble(qapp, tmp_path,
                                                                   tmp_store):
    """权威作废集（sig_narration 载荷里的 `retracted` 全量）里的那条若正被扣住 → 一并撤。

    这是 sig_retracted（单条 id）之外的第二条路：别处发起、界面没参与的撤回只走这条，
    少了这道闸，扣住的那条会在吐完那一刻照常换成正式行——把一个已被回溯的句子留在屏上。
    """
    win, worker = _make_window(tmp_path)
    _open(win, worker)
    line = _start_drip(win, worker)
    worker.sig_message.emit({"id": 12, "speaker": "甲", "speaker_type": "character",
                             "content": line, "in_scene": "贝克街221B", "turn": 1})
    assert win._held_msg is not None
    worker.sig_narration.emit({"auto": False, "retracted": [12]})
    assert win._stream is None and win._held_msg is None
    assert not _timer_active(win)
    assert line not in win._view.toPlainText()
    assert all(m.get("id") != 12 for m in win._conv_msgs)


def test_local_truncation_drops_the_drip(qapp, tmp_path, tmp_store):
    """撤回/改写的**本地截断**（_drop_from）：吐字中的那条比该 id 新 → 一并撤。"""
    win, worker = _make_window(tmp_path)
    _open(win, worker)
    worker.sig_message.emit({"id": 5, "speaker": "场景", "speaker_type": "narrator",
                             "content": "夜里。", "in_scene": "贝克街221B", "turn": 0})
    line = _start_drip(win, worker, turn=1, seq=10)
    assert win._drop_from(5) is True
    assert win._stream is None and win._held_msg is None
    assert not _timer_active(win), "截断之后定时器还在动界面 = 残句重影"
    assert line[:1] not in win._view.toPlainText()
    assert win._conv_msgs == [], "截断点之后的留存（含被撤的叙述）一并清掉"


def test_long_line_is_capped_by_the_accelerator(qapp, tmp_path, tmp_store):
    """长台词的**加速上限**（§2.4）：整句最多 `STREAM_MAX_TICKS` 跳 → ≤ `STREAM_MAX_MS`。

    没有这一条，一句 3000 字的台词按 1 字/40ms 要吐两分钟——用户只会以为卡死了。
    """
    win, worker = _make_window(tmp_path)
    _open(win, worker)
    line = "长" * 3000
    worker.sig_speak_delta.emit({"speaker": "甲", "text": line, "turn": 1, "seq": 1})
    worker.sig_speak_end.emit({"speaker": "甲", "settled": True, "turn": 1, "seq": 2})
    ticks = 1                                   # 开吐时已经吐了第一跳
    while win._stream is not None and not win._stream_exhausted():
        win._stream_tick()
        ticks += 1
    assert win._stream["text"] == line, "整句必须吐完（加速不截断）"
    assert ticks <= STREAM_MAX_TICKS, f"{ticks} 跳超过上限 {STREAM_MAX_TICKS}"
    assert ticks * STREAM_TICK_MS <= STREAM_MAX_MS, "整句耗时绝不超过加速上限"


def test_extreme_inputs_do_not_break_the_drip(qapp, tmp_path, tmp_store):
    """极端输入：空文本 / 一个字 / 换行与 HTML 特殊字符。"""
    win, worker = _make_window(tmp_path)
    _open(win, worker)

    # 空文本（只有收尾标记）：没有可吐的东西 → 不建气泡、不起表；
    # 随后那条正式消息照老路立即入留存（没有气泡可扣）。
    worker.sig_speak_end.emit({"speaker": "甲", "settled": True, "turn": 1, "seq": 1})
    assert win._stream is None and win._stream_timer is None
    worker.sig_message.emit({"id": 1, "speaker": "甲", "speaker_type": "character",
                             "content": "空片之后照旧。", "in_scene": "贝克街221B", "turn": 1})
    assert win._conv_msgs[-1]["id"] == 1, "没有气泡可扣 → 立即入留存（老路）"

    # 一个字：开吐那一跳即吐完（不画半成品帧，直接定格）
    worker.sig_speak_delta.emit({"speaker": "乙", "text": "嗯", "turn": 2, "seq": 2})
    worker.sig_speak_end.emit({"speaker": "乙", "settled": True, "turn": 2, "seq": 3})
    assert win._stream is not None and win._stream_exhausted()
    assert win._stream["text"] == "嗯"
    # 吐字早已走完时正式消息才到 → 就地定稿（不必等下一跳，更不会顶出第二条）
    worker.sig_message.emit({"id": 2, "speaker": "乙", "speaker_type": "character",
                             "content": "嗯", "in_scene": "贝克街221B", "turn": 2})
    assert win._conv_msgs[-1]["id"] == 2 and win._stream is None
    assert not _timer_active(win)

    # 换行与 HTML 特殊字符：转义/换行照正式气泡的规矩（渲染时转，不提前动缓冲）
    tricky = "<b>不是标签</b>\n第二行 & 收尾"
    worker.sig_speak_delta.emit({"speaker": "甲", "text": tricky, "turn": 3, "seq": 4})
    worker.sig_speak_end.emit({"speaker": "甲", "settled": True, "turn": 3, "seq": 5})
    _stop_real_timer(win)
    win._stream["text"] = win._stream["full"]         # 取"整句都吐出来"那一帧的形态
    win._rerender_conversation()
    html = win._format_streaming()
    assert "&lt;b&gt;" in html and "<b>" not in html, "特殊字符必须转义（防注入）"
    assert "<br>" in html, "换行照正式气泡的规矩转 <br>"
    assert "第二行 & 收尾" in win._view.toPlainText(), "整句一个字不差地画出来"
    win._stream_tick()                                # 已吐完 → 收尾（无正式消息则停在气泡）


def test_no_ghost_bubble_when_the_message_lands_before_the_last_pieces(qapp, tmp_path,
                                                                      tmp_store):
    """次序一旦反过来（正式消息先到、余片与收尾标记后到）也**绝不留残句/重影**。

    实况里这曾是最常见的一种次序：`worker._flush` 先 sweep 正式消息、再补派余片（引擎
    的接收器里还压着最后几个 token）。旧实现的后果很短命却很难看——正式行已经落地，余片
    又新建一条"正在说"气泡，收尾标记 settled=True 按设计不撤它，于是同一句台词在屏上出现
    两遍，且会常驻到下一块同说话人的片拼上去。

    与 worker 侧的次序修复是**两道独立的闸**：worker 修的是"片必须先于消息"（引擎自己的
    时序），这一条修的是"窗口无论收到什么次序都不留半条"。判据用 (speaker, turn)——
    两者都由 worker 派发时带上，且同块一致（见 `_FlushStub` 那条的说明）。
    """
    win, worker = _make_window(tmp_path)
    _open(win, worker)

    # 正式消息先落地（权威文本），此刻根本不存在气泡 → 无从定稿。
    worker.sig_message.emit({"id": 3, "speaker": "甲", "speaker_type": "character",
                             "content": "整句话。", "in_scene": "贝克街221B", "turn": 2})
    # 余片与收尾标记后到：都是**同一块**（同 speaker + 同 turn）。
    worker.sig_speak_delta.emit({"speaker": "甲", "text": "整句", "turn": 2, "seq": 5})
    worker.sig_speak_delta.emit({"speaker": "甲", "text": "话。", "turn": 2, "seq": 6})
    worker.sig_speak_end.emit({"speaker": "甲", "settled": True, "turn": 2, "seq": 7})

    assert win._stream is None, "已落地那块的余片不许开新气泡（幽灵）"
    assert win._stream_raw is None, "余片连缓冲都不该进（更不会在下一拍被吐出来）"
    assert not _timer_active(win)
    text = win._view.toPlainText()
    assert text.count("整句话。") == 1, "同一句只许出现一次（正式那条）"


def test_late_pieces_of_a_landed_block_do_not_extend_the_next_bubble(qapp, tmp_path,
                                                                    tmp_store):
    """已落地那块的余片也不许拼进**下一块**的气泡（两句话粘成一句的实况）。

    这是上一条的连带面：残句气泡一旦存在，同一说话人下一块的片会被**追加**进它，屏上就
    成了「上一句的尾巴 + 下一句的开头」这种角色根本没说过的话。闸按 (speaker, turn)
    分块，故只拦旧块、不误伤新块。
    """
    win, worker = _make_window(tmp_path)
    _open(win, worker)

    worker.sig_message.emit({"id": 4, "speaker": "甲", "speaker_type": "character",
                             "content": "第一句。", "in_scene": "贝克街221B", "turn": 2})
    worker.sig_speak_delta.emit({"speaker": "甲", "text": "第一句。", "turn": 2, "seq": 5})
    assert win._stream is None and win._stream_raw is None, "旧块的余片被拦下"

    # 下一块（turn 递增）：片照常进它自己的缓冲，绝不被旧块的残片污染。
    worker.sig_speak_delta.emit({"speaker": "甲", "text": "第二句的", "turn": 3, "seq": 8})
    assert win._stream_raw == {"speaker": "甲", "turn": 3, "text": "第二句的"}, \
        "新块的缓冲只该有它自己的正文"
    assert win._stream is None, "收尾标记没到就不开吐（屏上还是那条正式行）"
    worker.sig_speak_end.emit({"speaker": "甲", "settled": True, "turn": 3, "seq": 9})
    _stop_real_timer(win)
    _drain(win)
    assert "第一句。第二句" not in win._view.toPlainText(), "两句话绝不许粘成一个气泡"


def test_a_landed_block_is_remembered_even_with_another_line_in_between(qapp, tmp_path,
                                                                      tmp_store):
    """"这块已落地"必须**按块逐个记**，不能只记最后一条：中间夹一条别人的正式消息也不行。

    单槽记法（只记最近一条落地的消息）一夹就漏：余片与收尾标记照常开出一条吐字气泡，而那块
    的正式行早在屏上、这条气泡再也不会被收掉——同一句话在屏上出现两遍并常驻（上一轮修掉的
    那类事故）。判据仍是次序无关的那条：无论片与消息谁先到，都不留半条。
    """
    win, worker = _make_window(tmp_path)
    _open(win, worker)
    worker.sig_message.emit({"id": 1, "speaker": "甲", "speaker_type": "character",
                             "content": "整句话。", "in_scene": "贝克街221B", "turn": 2})
    worker.sig_message.emit({"id": 2, "speaker": "乙", "speaker_type": "character",
                             "content": "别的话。", "in_scene": "贝克街221B", "turn": 3})
    # 甲那一块的余片与收尾标记后到（同 speaker + 同 turn）
    worker.sig_speak_delta.emit({"speaker": "甲", "text": "整句话。", "turn": 2, "seq": 5})
    worker.sig_speak_end.emit({"speaker": "甲", "settled": True, "turn": 2, "seq": 6})

    assert win._stream is None, "已落地那块的余片不许开气泡（哪怕中间夹了别人的一条）"
    assert win._stream_raw is None and not _timer_active(win)
    # 让别的人接着说：残句若真开了，会一直顶在最新内容下面
    for i, name in enumerate(("乙", "乙"), start=1):
        worker.sig_message.emit({"id": 10 + i, "speaker": name,
                                 "speaker_type": "character",
                                 "content": f"后面的话{i}。", "in_scene": "贝克街221B",
                                 "turn": 3 + i})
    text = win._view.toPlainText()
    assert text.count("整句话。") == 1, "同一句只许出现一次（正式那条）"


def test_a_line_landing_mid_drip_keeps_the_engine_order(qapp, tmp_path, tmp_store):
    """扣住窗口里上屏的行都要排在**被扣住的那条之后**（留存次序 == 引擎 id 次序）。

    引擎里那句话先发生（角色说在前、叙述/用户插话在后），屏上却按"谁先入留存"排——若扣住的
    那条要等吐完才 append 到末尾，它就会被永久排到后面：时间戳倒流（21:30:07 在 21:30:05
    之上）、用户读到的因果颠倒，而且**换场才清**。所以它**当刻就按最终位置入留存**，吐字
    气泡画在它自己的位置上。
    """
    win, worker = _make_window(tmp_path)
    _open(win, worker)
    line = "甲的一句够长的话，好让吐字停在半路中间。"
    worker.sig_speak_delta.emit({"speaker": "甲", "text": line, "turn": 1, "seq": 1})
    worker.sig_speak_end.emit({"speaker": "甲", "settled": True, "turn": 1, "seq": 2})
    worker.sig_message.emit({"id": 6, "speaker": "甲", "speaker_type": "character",
                             "content": line, "in_scene": "贝克街221B", "turn": 1,
                             "time_hhmmss": "21:30:05"})
    assert [m.get("id") for m in win._conv_msgs] == [6], "扣住的那条当刻入留存（位置已定）"

    # 吐字还没完，后发生的两条上屏了：一条场景叙述、一句用户插话（21:30:07）
    worker.sig_message.emit({"id": 7, "speaker": "场景", "speaker_type": "narrator",
                             "content": "灯光暗了一格。", "in_scene": "贝克街221B", "turn": 1})
    win._append_human("我先说一句。", "21:30:07")
    assert [m.get("id") for m in win._conv_msgs] == [6, 7, None], \
        "留存次序就是引擎次序：台词 → 叙述 → 插话"
    win._stream_tick()                       # 再吐一步：屏上此刻挂着半句（吐字形态）
    mid = win._view.toPlainText()
    assert line[:2] in mid and mid.index(line[:2]) < mid.index("灯光暗了一格。"), \
        f"吐字那一帧也画在它自己的位置上（不许被后到的行越过）：{mid!r}"

    while win._stream is not None and not win._stream_exhausted():
        win._stream_tick()
    assert win._stream is None and win._held_msg is None
    text = win._view.toPlainText()
    assert text.count(line) == 1, "换上正式行之后正文仍只出现一次"
    assert text.index("21:30:05") < text.index("灯光暗了一格。") < text.index("21:30:07"), \
        "对白区不许出现时间戳倒流"


def test_undo_of_a_later_line_never_eats_the_held_line(qapp, tmp_path, tmp_store):
    """撤销/改写一条**后发生**的行，不许把引擎仍保留的那句台词一起从屏上抹掉（§6.1）。

    引擎的回溯是按 `id ≥ 目标` 作废（保留更早那句），而 `_drop_from` 是**位置截断**——两者
    只有在"留存次序 == 引擎 id 次序"时才等价（见上一条）。次序一旦倒挂，位置截断会把引擎
    仍喂给模型、角色记忆里也还在的那句台词静默删掉，而且 worker 不会补派（已派发过的行不
    二次 emit），用户只能换场重开。
    """
    win, worker = _make_window(tmp_path)
    _open(win, worker)
    line = "甲的一句够长的话，好让吐字停在半路中间。"
    worker.sig_speak_delta.emit({"speaker": "甲", "text": line, "turn": 1, "seq": 1})
    worker.sig_speak_end.emit({"speaker": "甲", "settled": True, "turn": 1, "seq": 2})
    worker.sig_message.emit({"id": 51, "speaker": "甲", "speaker_type": "character",
                             "content": line, "in_scene": "贝克街221B", "turn": 1,
                             "time_hhmmss": "21:30:05"})
    worker.sig_message.emit({"id": 52, "speaker": "场景", "speaker_type": "narrator",
                             "content": "灯光暗了一格。", "in_scene": "贝克街221B", "turn": 1})
    while win._stream is not None and not win._stream_exhausted():
        win._stream_tick()

    assert win._drop_from(52) is True
    assert [m.get("id") for m in win._conv_msgs] == [51], \
        "截断只该吃掉 52 及其后（51 在引擎里仍保留）"
    assert line in win._view.toPlainText(), "那句台词还在屏上"
    assert "灯光暗了一格。" not in win._view.toPlainText()


def test_truncation_before_the_drip_still_drops_it(qapp, tmp_path, tmp_store):
    """反过来：截断点**早于**吐字那条 → 照旧连气泡带表一并撤（残句绝不留在屏上）。"""
    win, worker = _make_window(tmp_path)
    _open(win, worker)
    worker.sig_message.emit({"id": 5, "speaker": "场景", "speaker_type": "narrator",
                             "content": "夜里。", "in_scene": "贝克街221B", "turn": 0})
    line = "甲的一句够长的话，好让吐字停在半路中间。"
    worker.sig_speak_delta.emit({"speaker": "甲", "text": line, "turn": 1, "seq": 9})
    worker.sig_speak_end.emit({"speaker": "甲", "settled": True, "turn": 1, "seq": 10})
    worker.sig_message.emit({"id": 6, "speaker": "甲", "speaker_type": "character",
                             "content": line, "in_scene": "贝克街221B", "turn": 1})
    assert win._drop_from(5) is True
    assert win._stream is None and win._held_msg is None
    assert not _timer_active(win), "截断之后定时器还在动界面 = 残句重影"
    assert win._conv_msgs == [] and win._view.toPlainText() == ""


def test_streaming_off_leaves_the_window_without_any_temp_bubble(qapp, tmp_path,
                                                                tmp_store):
    """不开启流式（缺省）：没有任何片信号 → 没有定时器、没有缓冲、没有扣留，逐字节不变。

    这是"逐字节不变"的界面侧一半：老路径只发 sig_message。对拍口径取**喂给 setHtml 的那串
    HTML**——必须恰好是"留存里每条各自的正式 HTML 顺序拼起来"，一个字节的吐字痕迹都不许多。
    """
    win, worker = _make_window(tmp_path)
    _open(win, worker)
    rendered: list[str] = []
    orig = win._view.setHtml
    win._view.setHtml = lambda h: (rendered.append(h), orig(h))[1]

    worker.sig_message.emit({"id": 1, "speaker": "甲", "speaker_type": "character",
                             "content": "老路径。", "in_scene": "贝克街221B", "turn": 1,
                             "time_hhmmss": "21:30:00"})
    assert rendered == ["".join(win._format_message(m) for m in win._conv_msgs)], \
        "流式关着：渲染输入 = 留存的正式 HTML 拼接（没有任何吐字形态）"
    assert win._stream is None and win._stream_raw is None and win._held_msg is None
    assert win._stream_timer is None, "流式关着 → 一枚定时器都不该建"
    assert "老路径。" in win._view.toPlainText()


# ======================================================= 开关：设置里的入口


def test_settings_menu_toggle_on_by_default_and_persists(qapp, tmp_path, tmp_store):
    """用户可见的入口：设置菜单里一项可勾选项；**缺省勾上**（产品缺省=开），
    取消勾选即落盘并推给 worker（关掉它 = 退回逐字不变的老路）。"""
    win, worker = _make_window(tmp_path)
    act = win._action_stream_speak
    assert act.isCheckable() and act.isChecked(), "缺省勾上（产品缺省=开）"
    act.setChecked(False)
    assert worker.stream_flags[-1] is False, "关掉要立刻告诉 worker"
    assert SettingsStore(tmp_store).load().stream_speak is False, "选择要落盘"


def test_saved_stream_setting_is_restored_and_pushed_at_startup(qapp, tmp_path,
                                                                tmp_store):
    """设置里存过「开」→ 起手即勾上，并把期望值推给 worker（第一场建引擎时带上）。"""
    SettingsStore(tmp_store).update(stream_speak=True)
    win, worker = _make_window(tmp_path, settings=SettingsStore(tmp_store).load())
    assert win._action_stream_speak.isChecked()
    assert worker.stream_flags and worker.stream_flags[-1] is True


def test_worker_build_engine_passes_the_stream_flag(tmp_path, monkeypatch, qapp):
    """worker 建引擎时把期望值带进 SceneEngine（重开/切场不丢）。"""
    worker_mod = __import__("harness.gui.worker", fromlist=["worker"])
    worker = SceneWorker()
    captured: dict = {}

    class _Engine:
        def __init__(self, *a, **kw):
            captured.update(kw)

    monkeypatch.setattr(worker_mod, "SceneEngine", _Engine)
    monkeypatch.setattr(worker_mod, "_seed_card_libraries", lambda *a, **k: [])
    worker._cfg = {"scene": tmp_path / "s.json", "characters": [],
                   "models": tmp_path / "models.yaml", "bid": None, "live": False,
                   "api_key": None, "run_root": tmp_path / "runs",
                   "closing_at_block": None, "start_time": None,
                   "cast_from_cards": False, "characters_dir": None}
    asyncio.run(worker._build_engine())
    assert captured["stream_speak"] is DEFAULT_STREAM_SPEAK, "缺省取产品缺省（现为开）"
    worker._stream_speak = False         # 用户关掉（期望值）
    asyncio.run(worker._build_engine())
    assert captured["stream_speak"] is False


# ======================================================= 端到端（真 worker + 真引擎）


def _write_fixture(tmp_path: Path):
    scene = tmp_path / "贝克街221B.json"
    a = tmp_path / "丙.json"
    b = tmp_path / "丁.json"
    models = tmp_path / "models.yaml"
    scene.write_text(json.dumps({
        "name": "贝克街221B", "participants": ["丙", "丁"],
        "circles": [{"id": "贝克街221B", "members": ["丙", "丁"]}],
        "hard_boundary": {"type": "time", "value": "22:00", "desc": "打烊"}},
        ensure_ascii=False), encoding="utf-8")
    a.write_text(json.dumps({"name": "丙", "personality": {"描述": "冷静"}},
                            ensure_ascii=False), encoding="utf-8")
    b.write_text(json.dumps({"name": "丁", "personality": {"描述": "锐利"}},
                            ensure_ascii=False), encoding="utf-8")
    models.write_text(
        "think:\n  backend: stub\n  model: stub\n  params: {}\n"
        "speak:\n  backend: stub\n  model: stub\n  params: {}\n", encoding="utf-8")
    return scene, a, b, models


def _wait_until(qapp, cond, timeout_ms: int = 6000, step_ms: int = 15) -> bool:
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


def test_end_to_end_stream_deltas_match_the_landed_message(qapp, tmp_path, tmp_store):
    """真跑一场（stub 引擎）：片经信号逐条到达界面，且某人的片拼起来 == 他落下的那句。

    这条把整条链子串起来：后端分片 → 图的接收器 → worker 的增量读 → 跨线程信号 →
    窗口的临时气泡。少任何一环，"逐字出现"就只是文档上的一句话。
    """
    scene, a, b, models = _write_fixture(tmp_path)
    worker = SceneWorker()
    worker.start()
    deltas: list[dict] = []
    ends: list[dict] = []
    msgs: list[dict] = []
    worker.sig_speak_delta.connect(deltas.append)
    worker.sig_speak_end.connect(ends.append)
    worker.sig_message.connect(msgs.append)
    try:
        worker.set_speak_stream(True)
        worker.start_scene(scene, [a, b], models, live=False, bid=None,
                           opening="入夜。", run_root=tmp_path / "rr_stream",
                           api_key=None, closing_at_block=100000)
        worker.set_pace(0.03)
        assert _wait_until(qapp, lambda: bool(deltas) and bool(msgs)), \
            "应有流式片与至少一条正式消息"
        chars = [m for m in msgs
                 if m.get("speaker_type") == "character"
                 and (m.get("content") or "").strip()]
        assert chars, "应至少有一条角色台词"
        first = chars[0]
        joined = "".join(d["text"] for d in deltas
                         if d["speaker"] == first["speaker"])
        assert first["content"] in joined, "流式片必须拼回那条正式消息的正文"
        assert ends, "收尾标记也要派发（界面据此收场）"
        assert len(deltas) > 1, "真分片（stub 固定 4 片），不是一次性吐完"
    finally:
        worker.shutdown(4000)
        assert not worker.isRunning()


def test_end_to_end_conversation_view_keeps_no_ghost_bubble(qapp, tmp_path, tmp_store):
    """真跑一场（真 worker + 真引擎 + **真窗口**）：对白区里一句台词只出现一次。

    这条补的是既有两条用例合起来仍空着的那道缝：`test_end_to_end_stream_deltas_…`
    只收信号、从不构造窗口，`test_temp_bubble_grows_then_is_finalized_in_place` 是**手工
    投信号**且次序是"片在前、消息在后"——而 worker 实况里的次序曾经正相反（`_flush` 先
    sweep 正式消息、再补派余片），于是"worker 与窗口合起来"这条链没有任何用例覆盖，
    幽灵气泡（正式行下面吊一条擦不掉的虚线残句）就一直绿着出厂。

    判据不去断言某一瞬间的 `_stream`（块间随时可能有一条**合法**的"正在说"气泡），而是
    数屏上的正文：每条已落地的角色台词在对白区出现的次数，必须等于它被落地的次数。
    幽灵每多一条，这个数就多一。
    """
    scene, a, b, models = _write_fixture(tmp_path)
    worker = SceneWorker()
    msgs: list[dict] = []
    worker.sig_message.connect(msgs.append)
    cfg = AppConfig(scene=scene, characters=[a, b], models=models,
                    bid=None, run_root=tmp_path / "runs", live=False,
                    api_key=None, opening="入夜。")
    win = MainWindow(worker, cfg, settings=SettingsStore(tmp_store).load())
    worker.start()
    try:
        worker.set_speak_stream(True)
        worker.start_scene(scene, [a, b], models, live=False, bid=None,
                           opening="入夜。", run_root=tmp_path / "rr_ghost",
                           api_key=None, closing_at_block=100000)
        worker.set_pace(0.03)
        landed = lambda: [m for m in msgs if m.get("speaker_type") == "character"
                          and (m.get("content") or "").strip()]
        assert _wait_until(qapp, lambda: len(landed()) >= 3), "应至少落地三条角色台词"
        # 先停机（不再有新块/新片进来），再把排队的信号排干净、把吐字推到底——吐字本身在
        # 真跑（QTimer），手动推只是让断言确定：否则"这一跳还没吐到的半句"会被当成幽灵。
        worker.shutdown(4000)
        for _ in range(50):
            qapp.processEvents()
            while win._stream is not None and not win._stream_exhausted():
                win._stream_tick()
        assert win._held_msg is None, "排干净之后不该还扣着谁"
        text = win._view.toPlainText()
        for m in landed():
            content = m["content"]
            want = sum(1 for x in landed() if x["content"] == content)
            assert text.count(content) == want, \
                f"「{content}」屏上出现了 {text.count(content)} 次，落地 {want} 次（幽灵重影）"
    finally:
        worker.shutdown(4000)
        assert not worker.isRunning()


def test_end_to_end_conversation_order_matches_the_engine(qapp, tmp_path, tmp_store):
    """真跑一场（真 worker + 真引擎 + 真窗口 + 自动推进）：对白区次序 == 引擎 id 次序。

    "扣住 = 当刻入留存"这条不变量在真实链路上最容易翻车的地方：叙述行（场景推进 agent）、
    导演行、用户插话都在吐字窗口里到达，而扣住的正式行要等吐完才换上去。按 append 记就是
    对白区次序与引擎 id 次序**永久**相反（时间戳倒流、`_drop_from` 的位置截断跟着吃错行）。
    """
    scene, a, b, models = _write_fixture(tmp_path)
    worker = SceneWorker()
    msgs: list[dict] = []
    worker.sig_message.connect(msgs.append)
    cfg = AppConfig(scene=scene, characters=[a, b], models=models,
                    bid=None, run_root=tmp_path / "runs", live=False,
                    api_key=None, opening="入夜。")
    win = MainWindow(worker, cfg, settings=SettingsStore(tmp_store).load())
    worker.start()
    try:
        worker.set_speak_stream(True)
        worker.start_scene(scene, [a, b], models, live=False, bid=None,
                           opening="入夜。", run_root=tmp_path / "rr_order",
                           api_key=None, closing_at_block=100000)
        worker.set_auto_narrate(True)
        worker.set_pace(0.05)
        landed = lambda: [m for m in msgs if (m.get("content") or "").strip()]
        assert _wait_until(qapp, lambda: len(landed()) >= 10), "应至少落地十条"
        worker.shutdown(4000)
        for _ in range(80):
            qapp.processEvents()
            while win._stream is not None and not win._stream_exhausted():
                win._stream_tick()
        engine_ids = [m["id"] for m in landed()]
        view_ids = [m["id"] for m in win._conv_msgs if m.get("id") in set(engine_ids)]
        assert view_ids == engine_ids, \
            f"对白区次序 {view_ids} 与引擎次序 {engine_ids} 不一致（扣住的那条被后到的行越过）"
    finally:
        worker.shutdown(4000)
        assert not worker.isRunning()
