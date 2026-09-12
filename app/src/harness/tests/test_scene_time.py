"""真实流速制虚拟钟（取代旧的按句/按静默折算情节分钟模型）。

场景虚拟钟 = 开场时刻 + 真实流逝 × 全局流速，由桌面 worker（GUI 侧）持有；图/引擎
不再推进任何钟键、不给消息打时刻戳（time_hhmmss 由 worker 派发时补）。本文件覆盖：
  1) sceneclock 纯函数：parse_hhmm(→秒) / format_clock(HH:MM:SS) / format_hhmm；
  2) VirtualClock（可注入时钟源，确定性）：advance ≈ 流逝×rate、暂停冻结、继续从
     冻结值接着走、调速先折现再换率（不丢时间）；
  3) 引擎侧契约：start_seconds/boundary_seconds 解析暴露（含跨午夜 +86400 换算）、
     开场/say/inject 消息不再带 at_min/clock、metrics 不再报 clock 条目、共享态无
     clock 键；
  4) 收束语义保留：块钟兜底（world 写「自动收束」导演行）与 close_scene（停止，
     不补打烊行）；时间打烊「十点了…」行只在 worker 虚拟钟到点路径补发。
worker 的真实流速自然收束（到点 → 已收束 + 打烊导演行 + 停 autoplay）与 GUI 流速/
时刻戳在 test_gui_smoke.py 覆盖（QThread 需 Qt 事件循环）。全部离线 stub、确定性、<5s。
"""
import asyncio
import json
from pathlib import Path

import pytest

from harness.engine import SceneEngine
from harness.sceneclock import (
    VirtualClock,
    format_clock,
    format_hhmm,
    parse_hhmm,
)
from harness.tests.helpers import assert_public_only


class _FakeMonotonic:
    """可手动推进的时钟源：让 VirtualClock 的流逝完全可控（确定性、无真实 sleep）。"""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _scene_files(tmp_path: Path, *, boundary: bool = True,
                 start_time: str | None = None) -> tuple[Path, Path, Path, Path]:
    scene = {"name": "贝克街221B", "participants": ["甲", "乙"]}
    if boundary:
        scene["hard_boundary"] = {"type": "time", "value": "22:00", "desc": "打烊"}
    if start_time is not None:
        scene["start_time"] = start_time
    (tmp_path / "贝克街221B.json").write_text(json.dumps(scene, ensure_ascii=False),
                                        encoding="utf-8")
    (tmp_path / "甲.json").write_text(
        json.dumps({"name": "甲", "personality": {"描述": "冷静"}},
                   ensure_ascii=False), encoding="utf-8")
    (tmp_path / "乙.json").write_text(
        json.dumps({"name": "乙", "personality": {"描述": "锐利"}},
                   ensure_ascii=False), encoding="utf-8")
    (tmp_path / "models.yaml").write_text(
        "think:\n  backend: stub\n  model: stub\n  params: {}\n"
        "speak:\n  backend: stub\n  model: stub\n  params: {}\n", encoding="utf-8")
    return (tmp_path / "贝克街221B.json", tmp_path / "甲.json",
            tmp_path / "乙.json", tmp_path / "models.yaml")


# ------------------------------------------------------------------ 纯函数
def test_parse_hhmm_returns_seconds():
    assert parse_hhmm("21:30") == 77400        # 21h*3600 + 30m*60
    assert parse_hhmm("00:00") == 0
    assert parse_hhmm("00:05") == 300          # 5 分钟 = 300 秒
    assert parse_hhmm("22:00") == 79200
    with pytest.raises(ValueError):
        parse_hhmm("25:99")
    with pytest.raises(ValueError):
        parse_hhmm("abc")
    with pytest.raises(ValueError):
        parse_hhmm("21:30:00")


def test_format_clock_and_hhmm():
    assert format_clock(77400) == "21:30:00"
    assert format_clock(77430) == "21:30:30"
    assert format_clock(79200) == "22:00:00"
    assert format_clock(0) == "00:00:00"
    assert format_clock(86400 + 30) == "00:00:30"     # 跨午夜回卷
    assert format_hhmm(77400) == "21:30"
    assert format_hhmm(79200) == "22:00"
    assert format_hhmm(88200) == "00:30"              # +86400 次日换算回卷


# ---------------------------------------------------------- VirtualClock
def test_virtualclock_advances_equals_elapsed_times_rate():
    fake = _FakeMonotonic()
    c = VirtualClock(77400, rate=1.0, monotonic=fake)
    assert c.current() == 77400
    c.start()
    fake.advance(5.0)
    assert c.current() == 77405, "1×：5 真实秒 = 5 虚拟秒"

    c2 = VirtualClock(77400, rate=60.0, monotonic=fake)
    c2.start()
    fake.advance(0.2)
    assert c2.current() == 77412, "60×：0.2 真实秒 ≈ 12 虚拟秒（公式正确）"


def test_virtualclock_pause_freezes_and_resume_continues():
    fake = _FakeMonotonic()
    c = VirtualClock(77400, rate=30.0, monotonic=fake)
    c.start()
    fake.advance(3.0)                 # 走 90 虚拟秒
    assert c.current() == 77490
    c.freeze()                        # 暂停：冻结
    frozen = c.current()
    fake.advance(5.0)                 # 暂停期间流逝不计
    assert c.current() == frozen, "暂停期间时钟不动"
    c.start()                         # 继续：从冻结值接着走
    fake.advance(1.0)
    assert c.current() == frozen + 30, "继续从冻结值 + 30（rate30×1s）"


def test_virtualclock_rate_change_preserves_current_time():
    fake = _FakeMonotonic()
    c = VirtualClock(77400, rate=1.0, monotonic=fake)
    c.start()
    fake.advance(10.0)                # 到 77410
    c.set_rate(4.0)                   # 折现后换率：不丢已走 10s、不从开场重来
    assert c.current() == 77410
    fake.advance(1.0)
    assert c.current() == 77414, "调速后 1s×4 = 77414（时间被保留且以新率前进）"


def test_virtualclock_advance_jumps_forward_without_losing_elapsed_time():
    """跳时间（§4.3）：整段前推 seconds，**不丢已走时间、不改流速、不改变走/停状态**。

    跳完还得照原节奏接着走——"时间过去了"与"重新起表"是两回事（worker 派发场景跳时间行
    时正是走这一条，见 test_gui_time_skip.py）。
    """
    fake = _FakeMonotonic()
    c = VirtualClock(77400, rate=30.0, monotonic=fake)
    c.start()
    fake.advance(2.0)                  # 走 60 虚拟秒 → 77460
    c.advance(1800)                    # 跳 30 分钟
    assert c.current() == 77460 + 1800
    assert c.rate == 30.0 and c.is_running(), "流速与走表状态都不变"
    fake.advance(1.0)
    assert c.current() == 77460 + 1800 + 30, "跳完照原流速继续走"


def test_virtualclock_advance_keeps_a_paused_clock_paused():
    """暂停（冻结）中的钟被前推后**仍然是暂停的**：跳时间不偷偷把表走起来。"""
    fake = _FakeMonotonic()
    c = VirtualClock(77400, rate=1.0, monotonic=fake)
    assert c.is_running() is False
    c.advance(3600)
    assert c.current() == 77400 + 3600 and c.is_running() is False
    fake.advance(5.0)
    assert c.current() == 77400 + 3600, "冻结中：前推之后依旧不动"


def test_virtualclock_advance_crosses_midnight():
    """前推可以跨午夜（「等天亮」一类）：秒数照加，展示层回卷。"""
    c = VirtualClock(parse_hhmm("23:30"))
    c.advance(8 * 3600)
    assert format_clock(c.current()) == "07:30:00"


# ------------------------------------------------------------------ 引擎契约
async def test_engine_exposes_seconds_and_properties(tmp_path):
    scene_p, a_p, b_p, models_p = _scene_files(tmp_path)
    eng = SceneEngine(scene_p, [a_p, b_p], models_p,
                      run_root=tmp_path / "runs", closing_at_block=200)
    assert eng.start_seconds == 77400
    assert eng.boundary_seconds == 79200
    assert eng.boundary_value == "22:00"
    assert eng.start_hhmm == "21:30"
    assert eng.boundary_hhmm == "22:00"

    await eng.open_scene()
    msgs = await eng.messages()
    assert msgs[0]["speaker"] == "导演" and msgs[0]["content"]
    assert "at_min" not in msgs[0] and "time_hhmmss" not in msgs[0], \
        "开场消息不带时刻戳（worker 派发时补 time_hhmmss）"
    st = await eng._snapshot()
    assert "clock" not in st, "真实流速钟不再是共享态键"
    assert_public_only(st)


async def test_engine_cross_midnight_boundary_next_day_seconds(tmp_path):
    """23:50 开场、00:30 打烊 → 打烊在开场之后须 +86400（次日 00:30）。"""
    scene_p, a_p, b_p, models_p = _scene_files(tmp_path, boundary=False)
    scene_p.write_text(json.dumps({
        "name": "贝克街221B", "participants": ["甲", "乙"],
        "hard_boundary": {"type": "time", "value": "00:30", "desc": "打烊"}},
        ensure_ascii=False), encoding="utf-8")
    eng = SceneEngine(scene_p, [a_p, b_p], models_p,
                      run_root=tmp_path / "runs", closing_at_block=200,
                      start_time="23:50")
    assert eng.start_seconds == 85800
    assert eng.boundary_seconds == 88200, "00:30 ≤ 23:50 → 次日 +86400"
    assert eng.boundary_hhmm == "00:30"
    assert eng.start_hhmm == "23:50"


async def test_engine_no_time_boundary_none(tmp_path):
    scene_p, a_p, b_p, models_p = _scene_files(tmp_path, boundary=False)
    eng = SceneEngine(scene_p, [a_p, b_p], models_p,
                      run_root=tmp_path / "runs", closing_at_block=200)
    assert eng.boundary_seconds is None
    assert eng.boundary_hhmm is None


async def test_engine_say_and_inject_append_without_clock(tmp_path):
    """say/inject 只追加消息：不推进任何钟键、不判打烊（时间全归 worker）。"""
    scene_p, a_p, b_p, models_p = _scene_files(tmp_path)
    eng = SceneEngine(scene_p, [a_p, b_p], models_p,
                      run_root=tmp_path / "runs", closing_at_block=200)
    await eng.open_scene()
    await eng.say("我在窗边敲了敲。")
    await eng.inject("（世界事件：灯忽明忽暗。）")
    st = await eng._snapshot()
    assert "clock" not in st
    assert_public_only(st)
    msgs = await eng.messages()
    human = [m for m in msgs if m.get("speaker_type") == "human"][-1]
    inj = [m for m in msgs if m.get("content") == "（世界事件：灯忽明忽暗。）"][-1]
    assert human["speaker"] == "你"
    assert "at_min" not in human and "at_min" not in inj, "行内无时刻戳（worker 补）"


async def test_engine_block_cap_closes_with_director_punch(tmp_path):
    """块钟兜底收束（world）：blocks ≥ closing_at_block 追加「自动收束」导演行并置
    closed——文案区别于 worker 到点的「打烊」行（graph._closing_msg）。"""
    scene_p, a_p, b_p, models_p = _scene_files(tmp_path)
    eng = SceneEngine(scene_p, [a_p, b_p], models_p,
                      run_root=tmp_path / "runs", closing_at_block=3)
    events = await eng.run_to_close(max_blocks=10)
    assert eng.closed is True
    assert any(e["type"] == "scene_state" and e["payload"]["closed"]
               for e in events)
    st = await eng._snapshot()
    assert "clock" not in st
    msgs = await eng.messages()
    assert any(m["speaker_type"] == "director" and "自动收束" in m["content"]
               for m in msgs)
    assert not any("打烊" in m["content"] for m in msgs), \
        "块钟兜底文案不得用时间打烊行（两者应可区分）"


async def test_engine_close_scene_stops_without_punch_message(tmp_path):
    """停止语义（engine 层）：close_scene() 立即 closed、不补打烊行。"""
    scene_p, a_p, b_p, models_p = _scene_files(tmp_path)
    eng = SceneEngine(scene_p, [a_p, b_p], models_p,
                      run_root=tmp_path / "runs", closing_at_block=200)
    await eng.open_scene()
    await eng.step(2)
    assert not eng.closed
    n_before = len(await eng.messages())
    await eng.close_scene()
    assert eng.closed is True
    msgs = await eng.messages()
    assert len(msgs) == n_before, "停止只收束、不追加打烊行"
    assert not any("打烊" in m["content"] for m in msgs)


async def test_engine_metrics_drops_clock_fields(tmp_path):
    """metrics 不再报时钟条目——真实流速时间由 worker 的 time 指标供应。"""
    scene_p, a_p, b_p, models_p = _scene_files(tmp_path)
    eng = SceneEngine(scene_p, [a_p, b_p], models_p,
                      run_root=tmp_path / "runs", closing_at_block=4)
    await eng.open_scene()
    guard = 0
    while not eng.closed:
        await eng.step(1)
        guard += 1
        assert guard < 80
    m = eng.metrics()
    assert m["messages"] > 0 and m["blocks"] > 0
    assert "clock_minutes" not in m and "clock_hhmm" not in m
    assert "start_hhmm" not in m and "boundary_hhmm" not in m
    assert m["think"]["stub"]["calls"] > 0 and m["speak"]["stub"]["calls"] > 0
