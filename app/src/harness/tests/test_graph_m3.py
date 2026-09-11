import asyncio

from harness.backends.stub import StubBackend
from harness.graph import build_graph
from harness.schemas import CharacterCard, Scene
from harness.tests.helpers import assert_public_only, last_state


def _scene():
    return Scene(name="餐厅", participants=["甲", "乙"])


def _cards():
    return {
        "甲": CharacterCard(name="甲", weights={"w2_arousal": 0.2}),
        "乙": CharacterCard(name="乙", weights={"w2_arousal": 0.9}),
    }


def _think_entry(urge: float) -> dict:
    """ThinkResult 全六键（schema 锁格式，think 步无条件严格校验）。"""
    return {"aroused": 0.5, "obligation_fulfilled": [], "goal_progress": 0.0,
            "addressed": None, "impression_of_speaker": None, "urge": urge}


def test_m3_interruption_yields_to_higher_urge(tmp_path):
    cards, scene = _cards(), _scene()
    # 乙 urge 恒定高 → 抢走甲的话
    think = StubBackend(json_script=[_think_entry(0.2), _think_entry(1.5),
                                     _think_entry(0.2), _think_entry(1.5)])
    speak = StubBackend(line_script=["甲的话。", "乙抢过话头。"])
    graph = build_graph(cards, scene, think, speak, run_root=tmp_path / "runs")

    asyncio.run(graph.ainvoke(
        {"messages": [{"id": 0, "speaker": "导演", "speaker_type": "director",
                       "content": "开局", "in_scene": "餐厅", "turn": 0}],
         "urges": {}, "current_speaker": None, "turn": 0, "silent_streak": 0,
         "decided": None, "injected": []},
        config={"configurable": {"thread_id": "m3a"}, "max_concurrency": 4}))
    for i in range(2):
        asyncio.run(graph.ainvoke(
            {"messages": [], "urges": {}, "current_speaker": None, "turn": i,
             "silent_streak": 0, "decided": None, "injected": []},
            config={"configurable": {"thread_id": "m3a"}, "max_concurrency": 4}))

    state = asyncio.run(last_state(graph, "m3a"))
    assert_public_only(state)
    # 至少出现过一次 yield_to 让位给乙，且甲的话没被说完就打断
    speakers = [m["speaker"] for m in state["messages"]]
    assert speakers[-1] == "乙"


def test_m3_silence_then_inject(tmp_path):
    cards, scene = _cards(), _scene()
    think = StubBackend(json_script=[_think_entry(0.0) for _ in range(6)])  # 连续静默
    speak = StubBackend(line_script=["无人说话"])
    graph = build_graph(cards, scene, think, speak, run_root=tmp_path / "runs")

    asyncio.run(graph.ainvoke(
        {"messages": [{"id": 0, "speaker": "导演", "speaker_type": "director",
                       "content": "开局", "in_scene": "餐厅", "turn": 0}],
         "urges": {}, "current_speaker": None, "turn": 0, "silent_streak": 0,
         "decided": None, "injected": []},
        config={"configurable": {"thread_id": "m3b"}, "max_concurrency": 4}))

    for i in range(3):   # 连续 3 块静默 → 第 3 块应注入导演事件
        asyncio.run(graph.ainvoke(
            {"messages": [], "urges": {}, "current_speaker": None, "turn": i,
             "silent_streak": i, "decided": None, "injected": []},
            config={"configurable": {"thread_id": "m3b"}, "max_concurrency": 4}))

    state = asyncio.run(last_state(graph, "m3b"))
    assert any(m.get("speaker_type") == "director" and "注入" in m["content"]
               for m in state["messages"])


def test_m3_last_chunk_filtered_by_knows_per_listener(tmp_path, monkeypatch):
    """C1(最终评审) 回归：knows 限制的最新条不得作为 last_chunk 喂给无权听众。

    场景三参与者（白/萧/叶），圈内对话 knows 只放行白/萧；丙虽在场，
    其 think payload 的 last_chunk 只能是它自己可见的旧行，绝不可见密语。
    通过给 graph.build_think_messages 打桩记录每名听众收到的 last_chunk 文本。
    """
    from harness import graph as graph_mod
    from harness.prompters import build_think_messages as _real_build
    from harness.schemas import CharacterCard, Scene

    cards = {
        "甲": CharacterCard(name="甲"),
        "乙": CharacterCard(name="乙"),
        "丙": CharacterCard(name="丙"),
    }
    scene = Scene(name="餐厅", participants=["甲", "乙", "丙"])
    seen: dict[str, str] = {}

    def _spy(card, view_text, last_chunk_text, scene_text, **kw):
        seen[card.name] = last_chunk_text
        return _real_build(card, view_text, last_chunk_text, scene_text, **kw)

    monkeypatch.setattr(graph_mod, "build_think_messages", _spy)

    think = StubBackend(json_script=[_think_entry(0.0) for _ in range(9)])  # 全员静默
    speak = StubBackend(line_script=["无人说话"])
    graph = build_graph(cards, scene, think, speak, run_root=tmp_path / "runs")

    asyncio.run(graph.ainvoke(
        {"messages": [
            {"id": 0, "speaker": "导演", "speaker_type": "director",
             "content": "（餐厅开场，人来人往）", "in_scene": "餐厅", "turn": 0},
            {"id": 1, "speaker": "甲", "speaker_type": "character",
             "content": "只告诉乙的密语：桌下递来的纸条", "in_scene": "餐厅",
             "knows": ["甲", "乙"], "turn": 1},
        ],
         "urges": {}, "current_speaker": None, "turn": 1,
         "silent_streak": 0, "decided": None, "injected": []},
        config={"configurable": {"thread_id": "m3c1"}, "max_concurrency": 4}))

    # 乙（knows 内）应把密语最新条作为 last_chunk 收到，且带说话者归属（同 view 形式）
    assert seen.get("乙") == "[1] 甲: 只告诉乙的密语：桌下递来的纸条"
    # 丙（knows 外）绝不能被喂密语——只能看到自己可见的开场旧行（若非 None），同样带归属
    assert seen.get("丙") == "[0] 导演: （餐厅开场，人来人往）"
    assert "密语" not in seen.get("丙", "")


def test_m3_last_chunk_carries_speaker_even_for_own_line(tmp_path, monkeypatch):
    """fix 回归：刚听到的一句必须带「是谁说的」——自问自答的根因修掉。

    末条是乙刚说的话（id=1）；块推进后所有在场者 think：
      · 甲（回应他人）收到的 last_chunk 是归属完整的 `[1] 乙: ...`；
      · 乙（自己刚说过、可能要自续）收到同样归属文本——能据此认出那是自己的话，
        而不是把匿名来句当别人说的话去接（严禁自问自答）。
    归属文本须以 `[id] 说话人: 内容` 形式进入 built 用户消息（同 render_view 约定）。
    """
    from harness import graph as graph_mod
    from harness.prompters import build_think_messages as _real_build

    cards, scene = _cards(), _scene()
    seen: dict[str, str] = {}
    built_user: dict[str, str] = {}

    def _spy(card, view_text, last_chunk_text, scene_text, **kw):
        seen[card.name] = last_chunk_text
        msgs = _real_build(card, view_text, last_chunk_text, scene_text, **kw)
        built_user[card.name] = msgs[1]["content"]
        return msgs

    monkeypatch.setattr(graph_mod, "build_think_messages", _spy)

    think = StubBackend(json_script=[_think_entry(0.0) for _ in range(2)])  # 全员静默
    speak = StubBackend(line_script=["无人说话"])
    graph = build_graph(cards, scene, think, speak, run_root=tmp_path / "runs")

    asyncio.run(graph.ainvoke(
        {"messages": [
            {"id": 0, "speaker": "导演", "speaker_type": "director",
             "content": "（餐厅开场）", "in_scene": "餐厅", "turn": 0},
            {"id": 1, "speaker": "乙", "speaker_type": "character",
             "content": "今晚这桌该我请。", "in_scene": "餐厅", "turn": 1},
        ],
         "urges": {}, "current_speaker": None, "turn": 1,
         "silent_streak": 0, "decided": None, "injected": []},
        config={"configurable": {"thread_id": "m3attr"}, "max_concurrency": 4}))

    expected = "[1] 乙: 今晚这桌该我请。"
    # 甲（他人 B）与乙（自己 A）收到的 last_chunk 都带说话者归属
    assert seen["甲"] == expected
    assert seen["乙"] == expected
    # 归属文本出现在 built 用户消息（模型实际看到的提示词里）
    assert expected in built_user["乙"]
    assert expected in built_user["甲"]


def test_m3_own_last_reaches_think_when_same_speaker_may_continue(tmp_path, monkeypatch):
    """fix：角色刚说完、无人插话——think 必须以 own_last=True 构图（被告知那是自己的话）。

    块1 甲 urge 高、乙 0.0（无意开口）→ 无在位者 yield_to 白，白开口。
    块2 白仍 0.9、乙仍 0.0：上句是白自己说的，白是唯一想开口者 → 可能接着续说。
    此时白（听众）收到自己上句作 last_chunk → build_think_messages 须带 own_last=True，
    让模型知道"刚听到的是自己的话"：不回答自己，只会真续说或沉默；乙听到的始终
    是别人的话 → own_last 恒 False。白在块2续话 → speak 也收到 own_previous=True。
    """
    from harness import graph as graph_mod
    from harness.prompters import build_speak_messages as _real_speak
    from harness.prompters import build_think_messages as _real_think

    cards, scene = _cards(), _scene()
    think_calls: list[dict] = []
    speak_own_previous: list[tuple[str, bool]] = []

    def _think_spy(card, view_text, last_chunk_text, scene_text, **kw):
        msgs = _real_think(card, view_text, last_chunk_text, scene_text, **kw)
        think_calls.append({"listener": card.name,
                            "own_last": kw.get("own_last", False),
                            "user": msgs[1]["content"]})
        return msgs

    def _speak_spy(card, view_text, scene_text, **kw):
        speak_own_previous.append((card.name, kw.get("own_previous", False)))
        return _real_speak(card, view_text, scene_text, **kw)

    monkeypatch.setattr(graph_mod, "build_think_messages", _think_spy)
    monkeypatch.setattr(graph_mod, "build_speak_messages", _speak_spy)

    think = StubBackend(json_script=[_think_entry(1.0), _think_entry(0.0),
                                     _think_entry(0.9), _think_entry(0.0)])
    speak = StubBackend(line_script=["甲的第一句话。", "甲的续话。"])
    graph = build_graph(cards, scene, think, speak, run_root=tmp_path / "runs")

    opener = {"messages": [{"id": 0, "speaker": "导演", "speaker_type": "director",
                            "content": "（餐厅开场）", "in_scene": "餐厅", "turn": 0}],
              "urges": {}, "current_speaker": None, "turn": 0,
              "silent_streak": 0, "decided": None, "injected": []}
    asyncio.run(graph.ainvoke(dict(opener),
                              config={"configurable": {"thread_id": "m3ownlast"},
                                      "max_concurrency": 4}))
    # 块2：刻意不传 current_speaker → 甲作为在位者保留，可续话（incumbent_continues）
    asyncio.run(graph.ainvoke({"messages": [], "urges": {}, "turn": 1,
                               "silent_streak": 0, "decided": None,
                               "injected": []},
                              config={"configurable": {"thread_id": "m3ownlast"},
                                      "max_concurrency": 4}))

    white = [c for c in think_calls if c["listener"] == "甲"]
    xiao = [c for c in think_calls if c["listener"] == "乙"]
    assert len(white) == 2 and len(xiao) == 2
    # 块1 白听到导演开场(own_last False)；块2 上句是它自己的话 → own_last True
    assert [c["own_last"] for c in white] == [False, True]
    assert [c["own_last"] for c in xiao] == [False, False]   # 乙永远听别人的话
    # own_last=True 那次构图确实注入了"刚听到的是你自己说的话"提醒
    assert "刚听到的是你自己说的话" in white[1]["user"]
    assert "冲动应保持低" in white[1]["user"]

    # 仲裁确实让白在块2续话（同一人紧接自己上句再开口），speak 收到 own_previous=True
    assert speak_own_previous == [("甲", False), ("甲", True)]
    state = asyncio.run(last_state(graph, "m3ownlast"))
    assert_public_only(state)
    speakers = [m["speaker"] for m in state["messages"]]
    assert speakers[-1] == "甲" and speakers.count("甲") == 2
