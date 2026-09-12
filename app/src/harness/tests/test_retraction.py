"""撤销（retraction）：被用户撤销的行从历史里消失，后续任何一轮都不得再看见它。

需求：场景的叙述（以及任何一行）用户随时可撤销/改写。撤销 = 从共享态的历史里剔除
（`GraphState.retracted` 记 id），不是标记「已失效仍留在提示词里」——否则模型下一轮
照样照着念。图内每一处「构视图/找锚点/判复读」都必须先过 `_live_messages`。
"""
import asyncio

from harness import graph as graph_mod
from harness.backends.stub import StubBackend
from harness.graph import _live_messages, _next_id, build_graph
from harness.schemas import CharacterCard, Scene
from harness.tests.helpers import assert_public_only, last_state


def _cards():
    return {"甲": CharacterCard(name="甲"),
            "乙": CharacterCard(name="乙")}


def _scene():
    return Scene(name="贝克街221B", participants=["甲", "乙"])


def _think(urge: float) -> dict:
    return {"aroused": 0.5, "obligation_fulfilled": [], "goal_progress": 0.0,
            "addressed": None, "impression_of_speaker": None, "urge": urge}


def _seed(retracted: list[int] | None = None) -> dict:
    """5 条历史：[1][2] 正常对话、[3] 乙那条被撤销、[4] 尾条（乙）。"""
    msgs = [
        {"id": 0, "speaker": "导演", "speaker_type": "director",
         "content": "（贝克街221B开场）", "in_scene": "贝克街221B", "turn": 0},
        {"id": 1, "speaker": "甲", "speaker_type": "character",
         "content": "这家店我常来。", "in_scene": "贝克街221B", "turn": 1},
        {"id": 2, "speaker": "乙", "speaker_type": "character",
         "content": "是吗。", "in_scene": "贝克街221B", "turn": 1},
        {"id": 3, "speaker": "乙", "speaker_type": "character",
         "content": "（这段叙述被用户撤销了。）", "in_scene": "贝克街221B", "turn": 1},
        {"id": 4, "speaker": "甲", "speaker_type": "character",
         "content": "那就这么说定了。", "in_scene": "贝克街221B", "turn": 2},
    ]
    return {"messages": msgs, "urges": {}, "current_speaker": None, "turn": 2,
            "silent_streak": 0, "decided": None, "injected": [],
            "retracted": list(retracted or [])}


def _spy_prompts(monkeypatch) -> tuple[dict, dict, dict]:
    """打桩 build_think_messages/build_speak_messages，记录每名听众的 view_text。"""
    from harness.prompters import build_speak_messages as real_speak
    from harness.prompters import build_think_messages as real_think

    think_view: dict[str, str] = {}
    speak_view: dict[str, str] = {}
    built: dict[str, str] = {}

    def _think_spy(card, view_text, last_chunk_text, scene_text, **kw):
        think_view[card.name] = view_text
        built[f"think:{card.name}"] = last_chunk_text
        return real_think(card, view_text, last_chunk_text, scene_text, **kw)

    def _speak_spy(card, view_text, scene_text, **kw):
        speak_view[card.name] = view_text
        return real_speak(card, view_text, scene_text, **kw)

    monkeypatch.setattr(graph_mod, "build_think_messages", _think_spy)
    monkeypatch.setattr(graph_mod, "build_speak_messages", _speak_spy)
    return think_view, speak_view, built


def test_live_messages_filters_retracted_ids():
    state = _seed(retracted=[3])
    live = _live_messages(state)
    assert [m["id"] for m in live] == [0, 1, 2, 4]
    assert [m["id"] for m in _live_messages({"messages": []})] == []
    # 缺 retracted 键的老状态：原样返回（旧行为逐字节不变）
    assert len(_live_messages({"messages": state["messages"]})) == 5


def test_retracted_line_absent_from_think_and_speak_views(tmp_path, monkeypatch):
    """[3] 被撤销 → 甲/乙的 think 视图与胜出者的 speak 视图都不含 [3]。

    白 urge 0.0、乙 urge 1.5 → 乙拿话筒：她的视图里既有被撤销的 [3] 要剔除，
    也必须仍含其余各条（撤销只剔那一行，不是截断历史）。"""
    think_view, speak_view, built = _spy_prompts(monkeypatch)
    graph = build_graph(_cards(), _scene(),
                        StubBackend(json_script=[_think(0.0), _think(1.5)]),
                        StubBackend(line_script=["行，那就这么说定了。"]),
                        run_root=tmp_path / "runs")

    asyncio.run(graph.ainvoke(_seed(retracted=[3]),
                              config={"configurable": {"thread_id": "retr1"},
                                      "max_concurrency": 4}))

    assert think_view["甲"] == "[0] 导演: （贝克街221B开场）\n[1] 甲: 这家店我常来。\n[2] 乙: 是吗。\n[4] 甲: 那就这么说定了。"
    assert "[3]" not in think_view["乙"] and "[4]" in think_view["乙"]
    assert set(speak_view) == {"乙"}
    assert "[3]" not in speak_view["乙"] and "[4]" in speak_view["乙"]
    # 此刻最新一句（last_chunk 锚点）也不得落在被撤销的行上
    assert "撤销" not in built["think:乙"]

    state = asyncio.run(last_state(graph, "retr1"))
    assert_public_only(state)
    assert state["retracted"] == [3]
    # 撤销的一行仍留在共享态（其余 UI 要能显示"这行被撤了"），只是不再进任何视图
    assert any(m["id"] == 3 for m in state["messages"])


def test_retracted_tail_does_not_recycle_id(tmp_path):
    """撤销尾条后新消息必须取「全量最大 id + 1」，不得复用被撤销的 id。

    复用会让新行一落地就落在 retracted 里 → 永远不可见（历史静默蒸发）。
    """
    seed = _seed(retracted=[4])
    assert _next_id(seed["messages"]) == 5

    graph = build_graph(_cards(), _scene(),
                        StubBackend(json_script=[_think(0.0), _think(1.5)]),
                        StubBackend(line_script=["那就这样。"]),
                        run_root=tmp_path / "runs")
    asyncio.run(graph.ainvoke(seed, config={"configurable": {"thread_id": "retr2"},
                                            "max_concurrency": 4}))
    state = asyncio.run(last_state(graph, "retr2"))
    spoken = [m for m in state["messages"] if m["speaker_type"] == "character"
              and m["id"] > 4]
    assert [m["id"] for m in spoken] == [5]
    assert 4 not in [m["id"] for m in _live_messages(state)]


def test_repeat_guard_ignores_retracted_own_lines(tmp_path):
    """说话人自己那条被撤销后，不算「已经说过」——同一句可以重新说出口。

    否则撤销反而把角色永久钉死：模型想说新话，兜底却拿被撤销的旧句判它复读。
    """
    own = "那我把话说回去：胳膊先包上。"
    seed = _seed(retracted=[3])
    seed["messages"].append({"id": 4, "speaker": "甲", "speaker_type": "character",
                             "content": own, "in_scene": "贝克街221B", "turn": 2})
    # 甲说过的同一句在 [4]（未撤销）→ 判为自我复读，不落盘
    assert graph_mod._is_near_repeat_of_own(seed["messages"], "甲", own) is True
    # 把 [4] 撤销 → 历史里再无此句，同一句不再算复读
    seed["retracted"] = [4]
    assert graph_mod._is_near_repeat_of_own(
        _live_messages(seed), "甲", own) is False
