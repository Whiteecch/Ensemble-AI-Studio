import asyncio
from pathlib import Path

from harness.backends.stub import StubBackend
from harness.graph import build_graph
from harness.schemas import CharacterCard, Scene
from harness.tests.helpers import assert_public_only, last_state


def _two_role_fixture() -> tuple[dict, Scene, dict]:
    cards = {
        "甲": CharacterCard(name="甲", personality={"描述": "冷静"}),
        "乙": CharacterCard(name="乙", personality={"描述": "锐利"}),
    }
    scene = Scene(name="贝克街221B", participants=["甲", "乙"])
    backends = {
        "think": StubBackend(json_script=[
            {"aroused": 0.5, "obligation_fulfilled": [], "goal_progress": 0.0,
             "impression_of_speaker": "开局", "urge": 0.6}]),
        "speak": StubBackend(line_script=[
            "你说，我听着。", "这句话轮到我了吧。"]),
    }
    return cards, scene, backends


def test_m2_two_role_offline_dialogue(tmp_path):
    cards, scene, backends = _two_role_fixture()
    graph = build_graph(cards, scene, backends["think"], backends["speak"],
                        run_root=tmp_path / "runs")
    # 导演开场（一条 director 消息）进入 messages
    asyncio.run(graph.ainvoke({
        "messages": [{"id": 0, "speaker": "导演", "speaker_type": "director",
                      "content": "你二人在贝克街221B临窗而坐。", "in_scene": "贝克街221B", "turn": 0}],
        "urges": {}, "current_speaker": None, "turn": 0, "silent_streak": 0,
    }, config={"configurable": {"thread_id": "demo1"}, "max_concurrency": 4}))

    asyncio.run(graph.ainvoke({
        "messages": [], "urges": {}, "current_speaker": None, "turn": 0,
        "silent_streak": 0,
    }, config={"configurable": {"thread_id": "demo1"}, "max_concurrency": 4}))

    state = asyncio.run(last_state(graph, "demo1"))
    assert_public_only(state)
    texts = [m["content"] for m in state["messages"]]
    assert any("你说，我听着" in t for t in texts)
    assert state["turn"] >= 1
