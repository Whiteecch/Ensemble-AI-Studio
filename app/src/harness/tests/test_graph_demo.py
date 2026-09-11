"""demo_alternate 图级叠加测试：强制二人轮流（demo-only，默认关闭）。

不加 demo_alternate（False，缺省）时行为与普通仲裁逐字节一致——控制组用同一脚本
验证即便在位者冲动恒更高，也按常规裁定连任（独角戏）；开了 demo_alternate 才把话筒
让给「除在位者外 argmax urge ≥ 开口阈值」的其它参与者，speak 永不紧随同一人。
"""
import asyncio

from harness.backends.stub import StubBackend
from harness.graph import build_graph
from harness.schemas import CharacterCard, Scene
from harness.tests.helpers import assert_public_only, last_state


def _scene():
    return Scene(name="餐厅", participants=["甲", "乙"])


def _cards():
    return {
        "甲": CharacterCard(name="甲"),
        "乙": CharacterCard(name="乙"),
    }


def _think_entry(urge: float) -> dict:
    """ThinkResult 全六键（schema 锁格式，think 步无条件严格校验）。"""
    return {"aroused": 0.5, "obligation_fulfilled": [], "goal_progress": 0.0,
            "addressed": None, "impression_of_speaker": None, "urge": urge}


def _drive(graph, thread_id: str, blocks: int) -> dict:
    """导演开场进消息后，连续 blocks 个空输入块（空输入由 checkpoint 持有
    current_speaker，speak 节点更新过的在位者信息得以跨块延续）。"""
    asyncio.run(graph.ainvoke(
        {"messages": [{"id": 0, "speaker": "导演", "speaker_type": "director",
                       "content": "开局", "in_scene": "餐厅", "turn": 0}],
         "urges": {}, "current_speaker": None, "turn": 0, "silent_streak": 0,
         "decided": None, "injected": []},
        config={"configurable": {"thread_id": thread_id}, "max_concurrency": 4}))
    for _ in range(blocks - 1):
        asyncio.run(graph.ainvoke(
            {}, config={"configurable": {"thread_id": thread_id}, "max_concurrency": 4}))
    return asyncio.run(last_state(graph, thread_id))


# 冲动脚本：甲恒 0.9、乙恒 0.5——两者都 ≥ speak_threshold(0.1)。
# 普通仲裁下在位者（白）永不被打断（challenger margin 0.4 < interruption 阈值幅度
# 且 cur ≥ 阈值 → incumbent_continues），故无 demo 是白/白/白独角戏；
# 开 demo 则话筒强制轮流：白/萧/白，绝无连续重样。
_URGE_SCRIPT = [_think_entry(0.9), _think_entry(0.5)] * 3
_LINE_SCRIPT = ["白句一。", "萧句一。", "白句二。", "萧句二。", "白句三。", "萧句三。"]


def test_demo_alternate_switches_speaker_each_block(tmp_path):
    think = StubBackend(json_script=[dict(x) for x in _URGE_SCRIPT])
    speak = StubBackend(line_script=list(_LINE_SCRIPT))
    graph = build_graph(_cards(), _scene(), think, speak,
                        run_root=tmp_path / "runs", demo_alternate=True)

    state = _drive(graph, "demo_alt_a", blocks=3)
    assert_public_only(state)
    speakers = [m["speaker"] for m in state["messages"]
                if m.get("speaker_type") == "character"]
    assert speakers == ["甲", "乙", "甲"]   # 强制轮流 A,B,A
    for a, b in zip(speakers, speakers[1:]):
        assert a != b, "demo 交替下任何说话者不得连续两句同人"


def test_default_path_still_lets_incumbent_continue(tmp_path):
    """demo_alternate=False（缺省）行为不变：在位者冲动更高 → 按常规连任独角戏。"""
    think = StubBackend(json_script=[dict(x) for x in _URGE_SCRIPT])
    speak = StubBackend(line_script=list(_LINE_SCRIPT))
    graph = build_graph(_cards(), _scene(), think, speak,
                        run_root=tmp_path / "runs")   # demo_alternate 缺省 False

    state = _drive(graph, "demo_off", blocks=3)
    speakers = [m["speaker"] for m in state["messages"]
                if m.get("speaker_type") == "character"]
    assert speakers == ["甲", "甲", "甲"]  # 常规仲裁：白连任三块
