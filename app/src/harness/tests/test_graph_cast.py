"""裸图上的可插拔演员表（§3.3）：cast_provider 决定扇出/竞价名单，entry_round_of
决定进场者能看到哪一段历史。

不涉及引擎（无 Dynamics/无存档）：直接 build_graph 并用两只读探针模拟「谁在场、
谁何时进场」——引擎路径（SceneEngine 注入同一对探针）见 test_engine_cast.py。
"""
import asyncio

from harness.backends.stub import StubBackend
from harness.graph import build_graph
from harness.schemas import CharacterCard, Scene


def _cards(names=("甲", "乙", "丙")):
    return {n: CharacterCard(name=n) for n in names}


def _scene(names=("甲", "乙", "丙")):
    return Scene(name="茶室", participants=list(names))


def _think(urge: float = 0.5) -> dict:
    return {"aroused": 0.5, "obligation_fulfilled": [], "goal_progress": 0.0,
            "addressed": None, "impression_of_speaker": None, "urge": urge}


def _opening() -> dict:
    return {"id": 0, "speaker": "导演", "speaker_type": "director",
            "content": "（开局）", "in_scene": "茶室", "turn": 0}


def _run(graph, thread_id: str, state: dict) -> None:
    asyncio.run(graph.ainvoke(state, config={
        "configurable": {"thread_id": thread_id}, "max_concurrency": 4}))


def _turn_state(turn: int = 0) -> dict:
    """续块输入：**不显式写 silent_streak**——静默计数由上一块留在 checkpoint 里，
    覆写成 0 会把静默注入路径焊死。"""
    return {"messages": [], "urges": {}, "current_speaker": None, "turn": turn,
            "decided": None, "injected": []}


def _spy_views(monkeypatch) -> dict:
    """把 graph.build_think_messages 换成记录器：{听众: (view_text, last_chunk)}。"""
    from harness import graph as graph_mod
    from harness.prompters import build_think_messages as _real

    seen: dict[str, tuple[str, str]] = {}

    def _spy(card, view_text, last_chunk_text, scene_text, **kw):
        seen[card.name] = (view_text, last_chunk_text)
        return _real(card, view_text, last_chunk_text, scene_text, **kw)

    monkeypatch.setattr(graph_mod, "build_think_messages", _spy)
    return seen


def test_cast_provider_limits_think_fanout(tmp_path, monkeypatch):
    """cast_provider 只返回 甲/乙 → 丙 既不 think（扇出名单），也不参与竞价。"""
    seen = _spy_views(monkeypatch)
    think = StubBackend(json_script=[_think(0.5) for _ in range(6)])
    speak = StubBackend(line_script=["甲先说话。"])
    graph = build_graph(_cards(), _scene(), think, speak, run_root=tmp_path / "runs",
                        cast_provider=lambda: ["甲", "乙"])
    _run(graph, "cast-fanout", {**_turn_state(), "messages": [_opening()]})
    assert set(seen) == {"甲", "乙"}, "扇出名单以外的人不得 think"
    assert "丙" not in seen


def test_entry_round_hides_earlier_history(tmp_path, monkeypatch):
    """entry_round_of=2 → 该听众的视图/上一句只剩 turn ≥ 2 的两行（进场前一句没听过）。"""
    seen = _spy_views(monkeypatch)
    msgs = [_opening(),
            {"id": 1, "speaker": "甲", "speaker_type": "character", "content": "第一句",
             "in_scene": "茶室", "turn": 0},
            {"id": 2, "speaker": "乙", "speaker_type": "character", "content": "第二句",
             "in_scene": "茶室", "turn": 1},
            {"id": 3, "speaker": "甲", "speaker_type": "character", "content": "第三句",
             "in_scene": "茶室", "turn": 2},
            {"id": 4, "speaker": "乙", "speaker_type": "character", "content": "第四句",
             "in_scene": "茶室", "turn": 3}]
    think = StubBackend(json_script=[_think(0.5) for _ in range(6)])
    speak = StubBackend(line_script=["甲接着说话。"])
    graph = build_graph(
        _cards(), _scene(), think, speak, run_root=tmp_path / "runs",
        entry_round_of=lambda name: 2 if name == "丙" else 0)
    _run(graph, "cast-entry", {**_turn_state(3), "messages": msgs})

    view, last = seen["丙"]
    assert "[3] 甲: 第三句" in view and "[4] 乙: 第四句" in view
    assert "[1] 甲: 第一句" not in view and "[2] 乙: 第二句" not in view
    assert "[0] 导演: （开局）" not in view, "开场行也早于进场"
    assert last == "[4] 乙: 第四句", "上一句 = 自己可见范围内的最新一条"
    # 老面孔（基线 0）照旧看全量
    assert "[1] 甲: 第一句" in seen["甲"][0] and "[0] 导演: （开局）" in seen["甲"][0]


def test_speak_guards_against_cast_change_mid_block(tmp_path, monkeypatch):
    """speak 的兜底：仲裁把话筒给了丙，但开演前丙已不在名单里（半途离场）→ 不落台词。

    用「第 3 次询问才把丙摘掉」的探针模拟块内变故（emit_thinks→deliberate→speak 依次
    问 3 次）：decided.speaker 仍是丙，speak 必须自己拦下——宁可当静默块，也不让不在场
    的人开口（共享态的 decided/current_speaker 本就可能被块间改动）。
    """
    from harness import graph as graph_mod
    from harness.prompters import build_speak_messages as _real_speak

    calls: list[str] = []
    asked = {"n": 0}

    def _provider():
        asked["n"] += 1
        return ["甲", "丙"] if asked["n"] <= 2 else ["甲"]

    def _spy(card, *a, **kw):
        calls.append(card.name)
        return _real_speak(card, *a, **kw)

    monkeypatch.setattr(graph_mod, "build_speak_messages", _spy)
    think = StubBackend(json_script=[_think(0.1) for _ in range(4)])
    speak = StubBackend(line_script=["丙的台词不该出现。"])
    graph = build_graph(
        _cards(), _scene(), think, speak, run_root=tmp_path / "runs",
        cast_provider=_provider,
        bidder=lambda name: 1.0 if name == "丙" else 0.05)   # 丙必拿话筒
    _run(graph, "cast-speak-guard", {**_turn_state(), "messages": [_opening()]})

    assert "丙" not in calls, "不在名单里的人绝不被叫去说话"
    assert not calls, "名单里只剩甲、而仲裁指名丙 → 本块当作静默块（无任何台词）"


def test_silence_gate_still_works_with_cast_provider(tmp_path):
    """注入探针不改变静默兜底：连续静默达 K 块照旧注入导演行（回归护栏）。"""
    think = StubBackend(json_script=[_think(0.0) for _ in range(9)])
    speak = StubBackend(line_script=["无人说话"])
    graph = build_graph(_cards(), _scene(), think, speak, run_root=tmp_path / "runs",
                        cast_provider=lambda: ["甲"], bidder=lambda name: 0.0)
    _run(graph, "cast-silence", {"messages": [_opening()], "urges": {},
                                 "current_speaker": None, "turn": 0,
                                 "silent_streak": 0, "decided": None, "injected": []})
    for i in range(3):
        _run(graph, "cast-silence", _turn_state(i))

    from harness.tests.helpers import last_state
    state = asyncio.run(last_state(graph, "cast-silence"))
    assert any(m.get("speaker_type") == "director" and "注入" in m["content"]
               for m in state["messages"])
