"""上下文完整性锁定：全量可见历史逐条进 think/speak + speak 补喂本人近期想法。

实况问题：角色隔很多轮复读近同句，或套用紧邻他人的长模板（乙用甲的惯用句、
自称自问自答）。根因二：① speak 侧没有本人内部记忆连续感，只盯着刚过去的他人那句
容易锚定模仿；② 提示词没锁死"只做自己、不得套他人句式、不得自答"。
本文件用离线确定性图测试锁定：
  1) 共享可见 H 条消息 → think/speak 的 view_text 逐条含全部 [id]，无窗口截断；
  2) knows 限定的私密消息绝不被无权者看到（think 与 speak 视图都投影）；
  3) speak 构图前把胜出者**自己**最近的 state/impressions 喂进去（本人连续性）。
"""
import asyncio
import re

from harness.backends.stub import StubBackend
from harness.graph import build_graph
from harness.schemas import CharacterCard, Scene
from harness.tests.helpers import assert_public_only, last_state


def _cards():
    return {
        "甲": CharacterCard(name="甲"),
        "乙": CharacterCard(name="乙"),
    }


def _scene():
    return Scene(name="餐厅", participants=["甲", "乙"])


def _think(urge: float, impression=None) -> dict:
    return {"aroused": 0.5, "obligation_fulfilled": [],
            "goal_progress": 0.0, "addressed": None,
            "impression_of_speaker": impression, "urge": urge}


def _ids(text: str) -> set[int]:
    return {int(m) for m in re.findall(r"\[(\d+)\]", text)}


_SEED = [
    {"id": 0, "speaker": "导演", "speaker_type": "director",
     "content": "（餐厅开场）", "in_scene": "餐厅", "turn": 0},
    {"id": 1, "speaker": "甲", "speaker_type": "character",
     "content": "这家店我常来。", "in_scene": "餐厅", "turn": 1},
    {"id": 2, "speaker": "乙", "speaker_type": "character",
     "content": "是吗。", "in_scene": "餐厅", "turn": 1},
    {"id": 3, "speaker": "甲", "speaker_type": "character",
     "content": "其实我紧张得手都在抖。", "in_scene": "餐厅",
     "knows": ["甲"], "turn": 2},           # 乙不可见（knows 外密语）
    {"id": 4, "speaker": "甲", "speaker_type": "character",
     "content": "你喝什么？", "in_scene": "餐厅", "turn": 3},
    {"id": 5, "speaker": "乙", "speaker_type": "character",
     "content": "热的。", "in_scene": "餐厅", "turn": 3},
    {"id": 6, "speaker": "甲", "speaker_type": "character",
     "content": "那就热的。", "in_scene": "餐厅", "turn": 4},
    {"id": 7, "speaker": "甲", "speaker_type": "character",
     "content": "再加一碟花生。", "in_scene": "餐厅", "turn": 5},
]


def _initial_state(turn: int = 5) -> dict:
    return {"messages": _SEED, "urges": {}, "current_speaker": None,
            "turn": turn, "silent_streak": 0, "decided": None,
            "injected": []}


def test_full_history_every_visible_line_reaches_think_and_speak(tmp_path, monkeypatch):
    """8 条共享历史（含 1 条只对白可见的密语 id3）：

    · 甲的 think 看到全部 8 条；乙的 think 看到 7 条、绝不见 [3]；
    · 胜出者乙的 speak 视图 = 她可见的 7 条，[3] 绝不出现在 speak 提示词；
    · built 用户消息（模型真看到的）同样逐条带 [id]。
    白 urge 0.0、乙 urge 1.5 → 无在位者 yield_to 乙出块。"""
    from harness import graph as graph_mod
    from harness.prompters import build_speak_messages as _real_speak
    from harness.prompters import build_think_messages as _real_think

    think_view: dict[str, str] = {}
    speak_views: list[tuple[str, str]] = []
    built_user: dict[str, str] = {}

    def _think_spy(card, view_text, last_chunk_text, scene_text, **kw):
        think_view[card.name] = view_text
        msgs = _real_think(card, view_text, last_chunk_text, scene_text, **kw)
        built_user[f"think:{card.name}"] = msgs[1]["content"]
        return msgs

    def _speak_spy(card, view_text, scene_text, **kw):
        speak_views.append((card.name, view_text))
        msgs = _real_speak(card, view_text, scene_text, **kw)
        built_user[f"speak:{card.name}"] = msgs[1]["content"]
        return msgs

    monkeypatch.setattr(graph_mod, "build_think_messages", _think_spy)
    monkeypatch.setattr(graph_mod, "build_speak_messages", _speak_spy)

    cards, scene = _cards(), _scene()
    think = StubBackend(json_script=[_think(0.0), _think(1.5)])   # 白安静、乙抢话
    speak = StubBackend(line_script=["第一次来这，看你挺自在。"])
    graph = build_graph(cards, scene, think, speak, run_root=tmp_path / "runs")

    asyncio.run(graph.ainvoke(
        _initial_state(),
        config={"configurable": {"thread_id": "fullhist"}, "max_concurrency": 4}))

    # think：白见 8 条（含密语 id3），乙见 7 条（knows 投影，[3] 不混入）
    assert _ids(think_view["甲"]) == {0, 1, 2, 3, 4, 5, 6, 7}
    assert _ids(think_view["乙"]) == {0, 1, 2, 4, 5, 6, 7}
    assert "[3]" not in think_view["乙"]

    # speak：胜出者乙，视图逐条含她可见的全部 7 条，[3] 不在其中
    assert len(speak_views) == 1 and speak_views[0][0] == "乙"
    assert _ids(speak_views[0][1]) == {0, 1, 2, 4, 5, 6, 7}
    assert "[3]" not in speak_views[0][1]

    # built 提示词（模型真正看到的 user 消息）确实逐条带 [id]
    assert {0, 1, 2, 3, 4, 5, 6, 7} <= _ids(built_user["think:甲"])
    assert {0, 1, 2, 4, 5, 6, 7} <= _ids(built_user["think:乙"])
    assert {0, 1, 2, 4, 5, 6, 7} <= _ids(built_user["speak:乙"])

    state = asyncio.run(last_state(graph, "fullhist"))
    assert_public_only(state)


def test_speak_feeds_winning_speakers_own_state_and_impressions(tmp_path, monkeypatch):
    """speak 开口前读胜出者**本人**的记忆：state（本块 think 刚折好）与 impressions
    （预存的自己内心）进 built 用户消息的「你的近期状态/想法」小节。

    两人 urge 同高 → 无论谁拿话筒，其 speak 都应带非空 state_text（紧接自己 think，
    本人连续性成立）与非空 impressions_text（预存印象文件）；密语不做多圈测试，白名单仍守。"""
    from harness import graph as graph_mod
    from harness.prompters import build_speak_messages as _real_speak

    seen: dict[str, dict] = {}
    built_user: list[tuple[str, str]] = []

    def _speak_spy(card, view_text, scene_text, **kw):
        seen[card.name] = {"state_text": kw.get("state_text", ""),
                           "impressions_text": kw.get("impressions_text", "")}
        msgs = _real_speak(card, view_text, scene_text,
                           state_text=kw.get("state_text", ""),
                           impressions_text=kw.get("impressions_text", ""))
        built_user.append((card.name, msgs[1]["content"]))
        return msgs

    monkeypatch.setattr(graph_mod, "build_speak_messages", _speak_spy)

    run_root = tmp_path / "runs"
    # 预存两人的印象文件（本人私有内心），模拟已进行一阵的场景
    for name, other, line in [("甲", "乙", "她话少但戳人。"),
                              ("乙", "甲", "他好像很紧张。")]:
        imp_dir = run_root / name / "impressions"
        imp_dir.mkdir(parents=True, exist_ok=True)
        (imp_dir / f"{other}.md").write_text(line, encoding="utf-8")

    cards, scene = _cards(), _scene()
    think = StubBackend(json_script=[_think(1.2), _think(1.2)])   # 同高，任一人开口
    speak = StubBackend(line_script=["行，那就这么说定了。"])
    graph = build_graph(cards, scene, think, speak, run_root=run_root)

    asyncio.run(graph.ainvoke(
        _initial_state(turn=1),
        config={"configurable": {"thread_id": "speakmem"}, "max_concurrency": 4}))

    # 恰一人出块；speak 收到本人 state_text（紧接其 think）与 impressions_text（私有内心）
    assert len(seen) == 1
    winner = next(iter(seen))
    assert winner in {"甲", "乙"}
    assert seen[winner]["state_text"] != ""
    assert "唤醒" in seen[winner]["state_text"]
    assert seen[winner]["impressions_text"] != ""
    assert "印象" in seen[winner]["impressions_text"]

    # built 用户消息：含「你的近期状态/想法」小节，且落在开口指令之前
    _, user = built_user[0]
    assert "【你的近期状态/想法】" in user
    assert "唤醒" in user and "印象" in user
    assert user.index("【你的近期状态/想法】") < user.index("你现在开口：")

    state = asyncio.run(last_state(graph, "speakmem"))
    assert_public_only(state)


def _director_open() -> dict:
    """最小开场：只有导演一行可见消息，turn 0。"""
    return {"messages": [
        {"id": 0, "speaker": "导演", "speaker_type": "director",
         "content": "（餐厅开场）", "in_scene": "餐厅", "turn": 0}],
        "urges": {}, "current_speaker": None, "turn": 0,
        "silent_streak": 0, "decided": None, "injected": []}


def _next_block(turn: int, keep_incumbent: bool) -> dict:
    """下一块增量状态。keep_incumbent=False 清空在位者（force yield_to 给最高 urge）；
    True 则保留在位者（可走 incumbent_continues 让同一人续话）。"""
    st = {"messages": [], "urges": {}, "turn": turn,
          "silent_streak": 0, "decided": None, "injected": []}
    if not keep_incumbent:
        st["current_speaker"] = None
    return st


def test_speak_prev_line_anchor_is_other_speakers_visible_line(tmp_path, monkeypatch):
    """speak 因果锚点：甲刚开口 → 下一块乙拿话筒，prev_line_text 正是白那条归属句。

    归属行须带 [id]/说话人（同 view 行约定）、非空、own_previous=False；built 用户消息
    的「此刻·上一句」小节含归属行原文与自续/他人双分支。"""
    from harness import graph as graph_mod
    from harness.prompters import build_speak_messages as _real_speak

    calls: list[dict] = []

    def _speak_spy(card, view_text, scene_text, **kw):
        msgs = _real_speak(card, view_text, scene_text, **kw)
        calls.append({"name": card.name,
                      "own_previous": kw.get("own_previous", False),
                      "prev_line_text": kw.get("prev_line_text", ""),
                      "user": msgs[1]["content"]})
        return msgs

    monkeypatch.setattr(graph_mod, "build_speak_messages", _speak_spy)

    cards, scene = _cards(), _scene()
    # 块1 甲开口（urge 高）；块2 白回落、乙抢话（无在位者 force yield_to）
    think = StubBackend(json_script=[_think(1.0), _think(0.0),
                                     _think(0.0), _think(1.5)])
    speak = StubBackend(line_script=["这家店我常来。", "是吗，那听你的。"])
    graph = build_graph(cards, scene, think, speak, run_root=tmp_path / "runs")

    asyncio.run(graph.ainvoke(
        _director_open(),
        config={"configurable": {"thread_id": "spkanchor"}, "max_concurrency": 4}))
    asyncio.run(graph.ainvoke(
        _next_block(turn=1, keep_incumbent=False),
        config={"configurable": {"thread_id": "spkanchor"}, "max_concurrency": 4}))

    assert [c["name"] for c in calls] == ["甲", "乙"]
    white, xiao = calls
    # 甲块1可见最新 = 导演开场行（自己说的话要等块2才有）
    assert white["prev_line_text"] == "[0] 导演: （餐厅开场）"
    assert white["own_previous"] is False
    # 乙块2锚点 = 甲刚说的那句归属行（非空、带说话人），不是她自己的话
    assert xiao["prev_line_text"] == "[1] 甲: 这家店我常来。"
    assert xiao["own_previous"] is False
    assert "【此刻·上一句】" in xiao["user"]
    assert "这家店我常来。" in xiao["user"]
    assert "若它的说话人正是你自己" in xiao["user"]
    assert "若是他人说的" in xiao["user"] and "→点名" in xiao["user"]
    assert "自然续说" in xiao["user"]
    assert xiao["user"].index("【此刻·上一句】") < xiao["user"].index("你现在开口：")

    state = asyncio.run(last_state(graph, "spkanchor"))
    assert_public_only(state)
    assert state["messages"][-1]["speaker"] == "乙"


def test_speak_prev_line_own_tail_self_continuation(tmp_path, monkeypatch):
    """同一位说话人合理续说：上句正是自己 → own_previous=True、小节走自续分支。

    块1 白开口、无人插话；块2 白仍最高（保留在位者 → incumbent_continues）。此时白
    speak 的 prev_line_text = 自己刚说那条可见尾句、own_previous=True；built 用户消息
    明确是自续：只能自然续说，不得回应对手句式/复读自己/假装有人接话。"""
    from harness import graph as graph_mod
    from harness.prompters import build_speak_messages as _real_speak

    calls: list[dict] = []

    def _speak_spy(card, view_text, scene_text, **kw):
        msgs = _real_speak(card, view_text, scene_text, **kw)
        calls.append({"name": card.name,
                      "own_previous": kw.get("own_previous", False),
                      "prev_line_text": kw.get("prev_line_text", ""),
                      "user": msgs[1]["content"]})
        return msgs

    monkeypatch.setattr(graph_mod, "build_speak_messages", _speak_spy)

    cards, scene = _cards(), _scene()
    # 块1 白 urge 高、乙 0.0 → 白开口；块2 同态势、保留在位者 → 白续话
    think = StubBackend(json_script=[_think(1.0), _think(0.0),
                                     _think(1.0), _think(0.0)])
    speak = StubBackend(line_script=["这家店我常来。", "所以常点的就那几样。"])
    graph = build_graph(cards, scene, think, speak, run_root=tmp_path / "runs")

    asyncio.run(graph.ainvoke(
        _director_open(),
        config={"configurable": {"thread_id": "spkown"}, "max_concurrency": 4}))
    asyncio.run(graph.ainvoke(
        _next_block(turn=1, keep_incumbent=True),
        config={"configurable": {"thread_id": "spkown"}, "max_concurrency": 4}))

    assert [c["name"] for c in calls] == ["甲", "甲"]
    _, cont = calls
    assert cont["own_previous"] is True
    assert cont["prev_line_text"] == "[1] 甲: 这家店我常来。"
    user = cont["user"]
    assert "【此刻·上一句】" in user
    assert "[1] 甲: 这家店我常来。" in user
    assert "再复读" in user                    # 别把自己刚说的当新内容复读
    assert "若它的说话人正是你自己" in user
    assert "自然续说" in user
    assert "回应对手的句式" in user and "假装别人刚接了话" in user
    assert user.index("【此刻·上一句】") < user.index("你现在开口：")

    state = asyncio.run(last_state(graph, "spkown"))
    assert_public_only(state)
    speakers = [m["speaker"] for m in state["messages"]]
    assert speakers[-1] == "甲" and speakers.count("甲") == 2


def test_speak_prev_line_never_leaks_knows_restricted_newest(tmp_path, monkeypatch):
    """C1(可见性) 进 speak 锚点：knows 限定的最新密语绝不当胜出者的 prev_line_text。

    末条是甲只对自己可见的密语（id2）。白安静、乙以高 urge 拿话筒时，她的
    锚点只能取自她本人可见的尾句（白公开的 id1）——密语内容绝不进 speak 提示词。"""
    from harness import graph as graph_mod
    from harness.prompters import build_speak_messages as _real_speak

    calls: list[dict] = []

    def _speak_spy(card, view_text, scene_text, **kw):
        msgs = _real_speak(card, view_text, scene_text, **kw)
        calls.append({"name": card.name,
                      "own_previous": kw.get("own_previous", False),
                      "prev_line_text": kw.get("prev_line_text", ""),
                      "user": msgs[1]["content"]})
        return msgs

    monkeypatch.setattr(graph_mod, "build_speak_messages", _speak_spy)

    cards, scene = _cards(), _scene()
    seed = [
        {"id": 0, "speaker": "导演", "speaker_type": "director",
         "content": "（餐厅开场）", "in_scene": "餐厅", "turn": 0},
        {"id": 1, "speaker": "甲", "speaker_type": "character",
         "content": "今晚这桌我请。", "in_scene": "餐厅", "turn": 1},
        {"id": 2, "speaker": "甲", "speaker_type": "character",
         "content": "其实我紧张得手都在抖。", "in_scene": "餐厅",
         "knows": ["甲"], "turn": 2},   # 密语：乙不可见，却是共享态最新条
    ]
    think = StubBackend(json_script=[_think(0.0), _think(1.5)])   # 白安静、乙拿话筒
    speak = StubBackend(line_script=["你先喝口热的，别拘谨。"])
    graph = build_graph(cards, scene, think, speak, run_root=tmp_path / "runs")

    asyncio.run(graph.ainvoke(
        {"messages": seed, "urges": {}, "current_speaker": None, "turn": 2,
         "silent_streak": 0, "decided": None, "injected": []},
        config={"configurable": {"thread_id": "spkvis"}, "max_concurrency": 4}))

    assert len(calls) == 1
    xiao = calls[0]
    assert xiao["name"] == "乙"
    # 锚点 = 她可见的尾句（白公开的 id1），绝不是密语 id2
    assert xiao["prev_line_text"] == "[1] 甲: 今晚这桌我请。"
    assert "紧张" not in xiao["prev_line_text"]
    # own_previous：她可见最新是白（他人）的话 → False
    assert xiao["own_previous"] is False
    # built 用户消息：含锚点小节与归属行，但密语内容绝不混入
    assert "【此刻·上一句】" in xiao["user"]
    assert "[1] 甲: 今晚这桌我请。" in xiao["user"]
    assert "紧张" not in xiao["user"] and "手都在抖" not in xiao["user"]

    state = asyncio.run(last_state(graph, "spkvis"))
    assert_public_only(state)
    assert state["messages"][-1]["speaker"] == "乙"
