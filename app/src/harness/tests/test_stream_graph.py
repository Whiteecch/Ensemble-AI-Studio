"""流式正文通道（设计文档 §二）：图与引擎侧的契约。

四条硬约束：

  · **不开启时逐字节不变**：`stream_speak` 缺省 False，speak 仍走今天那一句
    `complete_text`，一次调用不多不少。本文件的对拍用例把「关闭流式」的一次确定性
    短跑（调用序列 + 事件流签名 + 消息）与 **HEAD 源码**跑同一场景的输出冻结成字面量
    （用 `git archive HEAD` 取出旧源码、同一个探针脚本两边各跑一遍，逐字节相同），
    并在这里算「对拍组数与差异数」——组数 25（11 次调用 + 10 条事件 + 4 条消息），
    差异数 0。
  · **开启时只是多一条呈现通道**：接收器拿到的片**拼起来逐字节等于**最终落下的那条
    消息正文；事件流与消息本体与关闭时**一模一样**（不因流式改动任何真相）。
  · **近重复抑制优先**：判为复读的那一块，片照样出去了（已经长在屏幕上），但
    **一条正式消息都不落**，收尾标记 `settled=False` 明确告诉界面「撤掉临时气泡」。
  · **引擎不弹窗、不碰 Qt**：它只持一个 list（照 think_log 的范式）并提供只读增量读取口。
"""
import json
from pathlib import Path

import pytest

from harness import bidding as bidding_mod
from harness import graph as graph_mod
from harness.backends.stub import StubBackend
from harness.engine import SceneEngine
from harness.graph import build_graph
from harness.schemas import CharacterCard, Scene
from harness.tests.helpers import assert_public_only, last_state

_REPLY = "（这句由 stub 生成。）"


# ------------------------------------------------------------------ 共用小工具


class _Recorder(StubBackend):
    """在 stub 外面记一笔「这次走的是哪个方法」——**共用一份 sink**，次序即真实调用次序。

    对拍与"关闭时不碰流式通道"两件事都靠它：只实现两个老方法的后端（仓库里的常态）
    经基类缺省实现接进流式时，日志里出现的仍是 `complete_text`，不会凭空多出
    `complete_text_stream` 这一项。
    """

    def __init__(self, sink: list[str], **kw) -> None:
        super().__init__(**kw)
        self._sink = sink
        self._in_stream = False     # stub 的流式实现内部会复用 complete_text，不重复记账

    async def complete_json(self, messages: list[dict]) -> dict:
        self._sink.append("complete_json")
        return await super().complete_json(messages)

    async def complete_text(self, messages: list[dict]) -> str:
        if not self._in_stream:
            self._sink.append("complete_text")
        return await super().complete_text(messages)

    async def complete_text_stream(self, messages: list[dict]):
        self._sink.append("complete_text_stream")
        self._in_stream = True
        try:
            async for piece in super().complete_text_stream(messages):
                yield piece
        finally:
            self._in_stream = False


def _think(urge: float) -> dict:
    """ThinkResult 全六键（schema 锁格式，think 步无条件严格校验）。"""
    return {"aroused": 0.5, "obligation_fulfilled": [], "goal_progress": 0.0,
            "addressed": None, "impression_of_speaker": None, "urge": urge}


def _cards() -> dict[str, CharacterCard]:
    return {"甲": CharacterCard(name="甲"),
            "乙": CharacterCard(name="乙")}


def _scene() -> Scene:
    return Scene(name="贝克街221B", participants=["甲", "乙"])


def _cfg(thread: str) -> dict:
    return {"configurable": {"thread_id": thread}, "max_concurrency": 4}


def _run_one(graph, thread: str, state: dict) -> None:
    import asyncio
    asyncio.run(graph.ainvoke(state, config=_cfg(thread)))


def _opening_state() -> dict:
    return {"messages": [{"id": 0, "speaker": "导演", "speaker_type": "director",
                          "content": "入夜。", "in_scene": "贝克街221B", "turn": 0}],
            "urges": {}, "current_speaker": None, "turn": 0, "silent_streak": 0,
            "decided": None, "injected": []}


# ------------------------------------------------------ 开：接收器与落地消息


def test_streamed_pieces_concatenate_to_the_landed_block(tmp_path):
    """开启流式：接收器的片拼起来**逐字节等于**最终落下的那条消息正文。

    这是「流式只是呈现通道、不是新的真相源」的核心钉子：界面逐字长出来的那句和收尾
    落下的那句必须是同一句，否则用户看到的和存档里的会分成两个人说的。收尾那条
    `kind="end"` 标记带上 `settled=True`（这一块真的落了消息）。
    """
    log: list[dict] = []
    think = StubBackend(json_script=[_think(1.5), _think(0.1)])
    speak = StubBackend(line_script=[_REPLY])
    graph = build_graph(_cards(), _scene(), think, speak, run_root=tmp_path / "runs",
                        speak_stream=log, stream_speak=True)
    state = _run_one_and_state(graph, "st1", _opening_state())

    assert_public_only(state)
    spoken = [m for m in state["messages"] if m.get("speaker_type") == "character"]
    assert len(spoken) == 1, "应恰有一句台词落地"
    pieces = [e["text"] for e in log if e.get("kind") == "piece"]
    assert len(pieces) > 1, "真流式：至少切成两片（stub 固定 4 片）"
    assert "".join(pieces) == spoken[0]["content"]
    end = log[-1]
    assert end["kind"] == "end" and end["settled"] is True
    assert end["speaker"] == spoken[0]["speaker"]
    assert [e["seq"] for e in log] == list(range(1, len(log) + 1)), "序号单调递增"


def _run_one_and_state(graph, thread: str, state: dict) -> dict:
    import asyncio
    asyncio.run(graph.ainvoke(state, config=_cfg(thread)))
    return asyncio.run(last_state(graph, thread))


def empty_state(turn: int) -> dict:
    return {"messages": [], "urges": {}, "current_speaker": None, "turn": turn,
            "silent_streak": 0, "decided": None, "injected": []}


def test_stream_receiver_is_only_a_read_channel_for_the_speaker(tmp_path):
    """接收器的条目带 speaker/kind/seq，且**绝不进共享态**（信息边界不动）。

    与 think_log 同一范式：它是给人看的只读边通道，加进去的新键一旦路由进共享态就
    破了 §4.1 的信息边界（helpers 的白名单会当场红）。
    """
    log: list[dict] = []
    think = StubBackend(json_script=[_think(1.5), _think(0.1), _think(1.5),
                                     _think(0.1)])
    speak = StubBackend(line_script=["第一句。", "第二句。"])
    graph = build_graph(_cards(), _scene(), think, speak, run_root=tmp_path / "runs",
                        speak_stream=log, stream_speak=True)
    _run_one(graph, "st2", _opening_state())
    state = _run_one_and_state(graph, "st2", empty_state(2))
    assert_public_only(state)
    assert {e["speaker"] for e in log} == {"甲"}
    assert all(e["kind"] in ("piece", "end") for e in log)
    assert not any(k in state for k in ("speak_stream", "stream_speak", "speak_seq"))


# ------------------------------------------------------------ 近重复抑制（§2.3）


def test_near_repeat_streams_out_loud_but_lands_nothing(tmp_path):
    """判为复读的说话：片照样流出去（屏幕上已经长出来了），但**一条正式消息都不落**。

    复读抑制（`_is_near_repeat_of_own`）是整块作废的既有语义，流式与它冲突时**抑制优先**
    ——不能因为"已经显示过了"就把这半句变成正式台词。收尾标记 `settled=False` 就是给
    界面的收场指令：临时气泡必须被撤掉，绝不留半条消息（见 gui 侧同名用例）。
    """
    log: list[dict] = []
    think = StubBackend(json_script=[_think(1.5), _think(0.1), _think(1.5),
                                     _think(0.1)])
    speak = StubBackend(line_script=["同一句话。"])     # 循环吐同一句 → 第二块必判复读
    graph = build_graph(_cards(), _scene(), think, speak, run_root=tmp_path / "runs",
                        speak_stream=log, stream_speak=True)
    _run_one(graph, "st3", _opening_state())            # 第一块：正常落一句
    before = _run_one_and_state(graph, "st3", empty_state(1))
    landed = [m for m in before["messages"] if m.get("speaker_type") == "character"]
    assert len(landed) == 1 and landed[0]["content"] == "同一句话。"

    first_end = len(log)
    state = _run_one_and_state(graph, "st3", empty_state(2))   # 第二块：复读 → 作废
    after = [m for m in state["messages"] if m.get("speaker_type") == "character"]
    assert len(after) == 1, "复读块绝不落消息（历史只有第一块那一句）"

    new = log[first_end:]
    pieces = [e["text"] for e in new if e.get("kind") == "piece"]
    assert "".join(pieces) == "同一句话。", "片照样流出去（话说了一半才被判定）"
    assert new[-1]["kind"] == "end" and new[-1]["settled"] is False, \
        "收尾必须明说这一块没有正式消息 → 界面据此撤掉临时气泡"


def test_stream_failure_midway_marks_the_block_void(tmp_path):
    """流到一半后端抛错：异常照抛（worker 据此整块重试），但**先补一条 settled=False**。

    不收场的代价很具体：重试那一遍的片与已吐出的半句**是同一个说话人**，界面上会被当成
    同一个临时气泡接着拼——用户看到的是"半句 + 整句"粘在一起。标记一到，界面撤掉旧气泡。
    """
    import asyncio

    class _Boom(StubBackend):
        async def complete_text_stream(self, messages):
            yield "说到一半"
            raise RuntimeError("connection reset")

    log: list[dict] = []
    think = StubBackend(json_script=[_think(1.5), _think(0.1)])
    graph = build_graph(_cards(), _scene(), think, _Boom(line_script=["不相关"]),
                        run_root=tmp_path / "runs",
                        speak_stream=log, stream_speak=True)
    with pytest.raises(RuntimeError, match="connection reset"):
        asyncio.run(graph.ainvoke(_opening_state(), config=_cfg("st4")))
    assert [e["kind"] for e in log] == ["piece", "end"]
    assert "".join(e["text"] for e in log if e["kind"] == "piece") == "说到一半"
    assert log[-1]["settled"] is False, "中途失败同样要明说「这一块没有正式消息」"


# -------------------------------------------------------- 关：逐字节不变（对拍 HEAD）

#: 对拍基准：**HEAD 源码**（`git archive HEAD`）跑同一条探针脚本的产出，逐字节冻结。
#: 场景 = 二人场、硬边界 22:00、关闭自动推进、think 脚本 urge 1.2/0.3 交替、
#: speak 脚本三句占位台词、连跑开场 + 4 块。任何一处漂移都会让下面的等值断言变红。
_HEAD_CALLS = ["complete_json", "complete_json", "complete_json", "complete_json",
               "complete_text", "complete_json", "complete_json", "complete_text",
               "complete_json", "complete_json", "complete_text"]
_HEAD_EVENTS = [
    ["block_spoken", 0, "导演", "入夜。"],
    ["scene_state", '{"clock": 0, "closed": false}'],
    ["decision", '{"kind": "block_done", "speaker": null}'],
    ["scene_state", '{"clock": 0, "closed": false}'],
    ["decision", '{"kind": "block_done", "speaker": null}'],
    ["scene_state", '{"clock": 1, "closed": false}'],
    ["decision", '{"kind": "block_done", "speaker": null}'],
    ["scene_state", '{"clock": 2, "closed": false}'],
    ["decision", '{"kind": "block_done", "speaker": null}'],
    ["scene_state", '{"clock": 3, "closed": false}'],
]
_HEAD_MESSAGES = [[0, "导演", "入夜。"],
                  [1, "甲", "（这句由 stub 生成。）"],
                  [2, "甲", "（stub 占位台词。）"],
                  [3, "甲", "（没有感情的占位对白。）"]]


def _event_sig(e: dict) -> list:
    """事件 → 可对拍签名：block_spoken 取（id, speaker, content），其余取 JSON 载荷。"""
    payload = e.get("payload") or {}
    if e.get("type") == "block_spoken":
        return ["block_spoken", payload.get("id"), payload.get("speaker"),
                payload.get("content")]
    return [e.get("type"), json.dumps(payload, ensure_ascii=False, sort_keys=True)]


def _stream_blocks(entries: list[dict]) -> list[list[dict]]:
    """接收器条目 → 逐块的「片」列表（遇到 `kind="end"` 就断一句）。"""
    blocks: list[list[dict]] = []
    cur: list[dict] = []
    for e in entries:
        if e.get("kind") == "end":
            blocks.append(cur)
            cur = []
        else:
            cur.append(e)
    if cur:
        blocks.append(cur)
    return blocks


def _diff_count(expected: list, actual: list) -> int:
    """两组签名的差异数（长度不同即按较长的一侧计满）。"""
    return sum(1 for a, b in zip(expected, actual) if a != b) + abs(len(expected) - len(actual))


def _build_two_cast(tmp_path: Path, **kw) -> SceneEngine:
    """二人场 + 三档 stub（think/speak 由调用方换成 Recorder）——对拍与流式共用。"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "贝克街221B.json").write_text(json.dumps({
        "name": "贝克街221B", "participants": ["甲", "乙"],
        "hard_boundary": {"type": "time", "value": "22:00", "desc": "打烊"}},
        ensure_ascii=False), encoding="utf-8")
    for name in ("甲", "乙"):
        (tmp_path / f"{name}.json").write_text(json.dumps(
            {"name": name, "personality": {"描述": "测试"}}, ensure_ascii=False),
            encoding="utf-8")
    (tmp_path / "models.yaml").write_text(
        "think:\n  backend: stub\n  model: stub\n  params: {}\n"
        "speak:\n  backend: stub\n  model: stub\n  params: {}\n", encoding="utf-8")
    bid = tmp_path / "bid.yaml"
    bid.write_text("interruption_threshold: 4.0\nspeak_threshold: 0.0\n"
                   "silence_k: 100000\n", encoding="utf-8")
    return SceneEngine(tmp_path / "贝克街221B.json",
                       [tmp_path / "甲.json", tmp_path / "乙.json"],
                       tmp_path / "models.yaml", run_root=tmp_path / "runs",
                       bid_path=bid, closing_at_block=200, auto_narrate=False, **kw)


async def _short_run(eng: SceneEngine, calls: list[str], blocks: int = 4):
    """接上记账替身跑「开场 + N 块」，返回（事件签名, 消息签名）。"""
    eng.think_backend = _Recorder(calls, json_script=[_think(1.2), _think(0.3)])
    eng.speak_backend = _Recorder(calls, line_script=[
        "（这句由 stub 生成。）", "（stub 占位台词。）", "（没有感情的占位对白。）"])
    events = await eng.open_scene("入夜。")
    for _ in range(blocks):
        events += await eng.step(1)
    msgs = await eng.messages()
    sigs = [_event_sig(e) for e in events]
    met = [[m.get("id"), m.get("speaker"), m.get("content")] for m in msgs]
    return sigs, met


def test_stream_off_is_byte_identical_to_head(tmp_path):
    """**关闭流式（缺省）= 与 HEAD 逐字节相同**（本特性的第一铁律）。

    对拍基准来自 `git archive HEAD` 取出的旧源码跑同一条探针脚本；这里把那份产出冻结
    成字面量，逐组比对并算出**差异数**。同时钉住：一次 `complete_text_stream` 都不该有
    （老后端经基类缺省实现也不会漂——只要没人打开这个开关）。
    """
    import asyncio

    calls: list[str] = []
    eng = _build_two_cast(tmp_path)                 # stream_speak 缺省 = False
    sigs, met = asyncio.run(_short_run(eng, calls))

    groups = len(_HEAD_CALLS) + len(_HEAD_EVENTS) + len(_HEAD_MESSAGES)
    diffs = (_diff_count(_HEAD_CALLS, calls)
             + _diff_count(_HEAD_EVENTS, sigs)
             + _diff_count(_HEAD_MESSAGES, met))
    assert groups == 25, "对拍组数固定：11 次调用 + 10 条事件 + 4 条消息"
    assert diffs == 0, f"（对拍 {groups} 组，差异 {diffs} 处）"
    assert "complete_text_stream" not in calls, "关闭流式时一次都不许走新通道"
    assert calls == _HEAD_CALLS and sigs == _HEAD_EVENTS and met == _HEAD_MESSAGES


def test_stream_on_keeps_truth_identical_and_only_reroutes_the_speak_call(tmp_path):
    """开启流式：**真相一字不改**（事件流与消息与关闭时逐条相同），只是 speak 那一枪
    换成了流式方法，且接收器里的片拼起来等于落下的正文。

    这是「流式是新增通道、不是替换」的可执行定义：两者唯一允许的差异就是调用序列里
    同一位置上的方法名（`complete_text` → `complete_text_stream`），差异条数恰好等于
    speak 次数。
    """
    import asyncio

    off_calls: list[str] = []
    eng_off = _build_two_cast(tmp_path / "off")
    off_sigs, off_met = asyncio.run(_short_run(eng_off, off_calls))

    on_calls: list[str] = []
    eng_on = _build_two_cast(tmp_path / "on", stream_speak=True)
    on_sigs, on_met = asyncio.run(_short_run(eng_on, on_calls))

    assert on_sigs == off_sigs, "事件流必须与关闭时逐条相同"
    assert on_met == off_met, "落下的消息必须与关闭时逐条相同"

    speak_n = on_calls.count("complete_text_stream")
    assert speak_n == off_calls.count("complete_text") == 3
    assert "complete_text" not in on_calls, "开启后不再走老路"
    renamed = [("complete_text" if a == "complete_text_stream" else a)
               for a in on_calls]
    assert renamed == off_calls, "唯一允许的差异就是 speak 那一枪的方法名"
    assert len(on_calls) == len(off_calls), "调用次数一个都不多"

    tail = eng_on.speak_stream_tail()
    assert tail and tail[-1]["kind"] == "end" and tail[-1]["settled"] is True
    blocks = _stream_blocks(tail)
    assert len(blocks) == 3, "三块各一段流式片（按收尾标记断句）"
    assert "".join(e["text"] for e in blocks[-1]) == on_met[-1][2]


def test_engine_stream_reader_reset_and_live_toggle(tmp_path):
    """引擎侧的三个口子：只读增量读（`speak_stream_tail`）、重置清空、运行期开关。

    与 `think_log_tail` 同一范式：引擎持列表、界面增量读；换场/重置必须把这条通道也
    清干净（否则新场的界面上会先冒出一段上一场的话——同类计数器刚栽过一次的坑）。
    """
    import asyncio

    async def _drive():
        calls: list[str] = []
        eng = _build_two_cast(tmp_path, stream_speak=True)
        await _short_run(eng, calls, blocks=2)
        assert eng.speak_stream_tail(), "开启后应有片"
        assert eng.speak_stream_tail(n=2) == eng.speak_stream_tail()[-2:], "只读尾部"

        eng.set_speak_stream(False)                 # 运行期关：下一块起不再走流式
        before = len(eng.speak_stream_tail())
        await eng.step(1)
        assert len(eng.speak_stream_tail()) == before, "关掉后不再追加片"

        eng.set_speak_stream(True)
        await eng.step(1)
        assert len(eng.speak_stream_tail()) > before, "再打开立刻生效"

        await eng.reset_scene_runtime()
        assert eng.speak_stream_tail() == [], "重置必须清空流式通道（与 think_log 同纪律）"
        await eng.aclose()

    asyncio.run(_drive())


def test_default_stream_flag_is_off_everywhere(tmp_path):
    """缺省一律关：裸图的 ctx、`build_graph` 的缺省、引擎的构造缺省。

    这是铁律 3 的"缺省即今天"——任何一处默认变 True，既有 1609 条用例里的调用序列与
    事件流都会跟着漂。
    """
    ctx = graph_mod.GraphContext(_cards(), _scene(), StubBackend(), StubBackend(),
                                 tmp_path / "runs",
                                 bid_params=bidding_mod.BidParams())
    assert ctx.stream_speak is False and ctx.speak_stream == []
    eng = _build_two_cast(tmp_path / "d")
    assert eng.speak_stream_tail() == []
