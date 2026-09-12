"""speak 近重复抑制 + 纯相似度助手（三层刹车的第 B 层）。

textsim.normalize/ratio 是纯函数；speak-node 抑制走真实 graph（stub 脚本化：同一说话人
第二块产出逐字相同台词 → 不落盘、不收束、块钟照推进；换成不同内容 → 正常落盘）。
"""
import asyncio

from harness.backends.stub import StubBackend
from harness.graph import build_graph
from harness.schemas import CharacterCard, Scene
from harness.textsim import normalize, ratio
from harness.tests.helpers import assert_public_only, last_state


# ---------------------------------------------------------------- textsim 纯函数
def test_normalize_strips_stage_parens_and_punctuation():
    # 全角/半角括号的舞台说明片段（含嵌套）整体剥除；空白、标点一并剥
    s = "乙（端起茶杯，浅笑）：我们走吧……！(站起来)"
    assert normalize(s) == "乙我们走吧"
    assert normalize("甲（（很用力）低声）说：好。 嗯") == "甲说好嗯"


def test_ratio_near_one_for_verbatim_same_content_after_normalize():
    assert ratio("我们走吧。", "我们走吧") >= 0.95
    assert ratio("（他起身）我们走吧。", "我们走吧") >= 0.95
    assert ratio("", "我们走吧") == 0.0          # 空/纯舞台说明不算近重复


def test_ratio_low_for_clearly_different_content():
    assert ratio("我们走吧。", "今晚这桌我请。") < 0.5


# ---------------------------------------------------------------- speak-node 抑制
def _scene():
    return Scene(name="贝克街221B", participants=["乙", "丙"])


def _cards():
    return {"乙": CharacterCard(name="乙"),
            "丙": CharacterCard(name="丙")}


def _entry(urge: float) -> dict:
    return {"aroused": 0.5, "obligation_fulfilled": [], "goal_progress": 0.0,
            "addressed": None, "impression_of_speaker": None, "urge": urge}


def _opener_state():
    return {"messages": [{"id": 0, "speaker": "导演", "speaker_type": "director",
                          "content": "（开局）", "in_scene": "贝克街221B", "turn": 0}],
            "urges": {}, "current_speaker": None, "turn": 0, "silent_streak": 0,
            "decided": None, "injected": [], "blocks": 0, "closed": False}


def _step_state(turn: int):
    return {"messages": [], "urges": {}, "turn": turn,
            "silent_streak": 0, "decided": None, "injected": []}


def test_speak_node_suppresses_verbatim_self_repeat_not_different(tmp_path):
    """同一说话人（白）连任：块1首句落盘 → 块2逐字复读同一句 → 抑制（无新消息、
    不收束、块钟推进）；块3换成不同内容 → 正常落盘。"""
    # 每块 白=1.2（愿说）、萧=0.0（无意）→ 白连任三块；speak 台本 块1/块2 逐字相同。
    think = StubBackend(json_script=[_entry(1.2), _entry(0.0),
                                     _entry(1.2), _entry(0.0),
                                     _entry(1.2), _entry(0.0)])
    speak = StubBackend(line_script=["我们走吧。", "我们走吧。", "换个新话题吧。"])
    graph = build_graph(_cards(), _scene(), think, speak,
                        run_root=tmp_path / "runs")
    cfg = {"configurable": {"thread_id": "dup"}, "max_concurrency": 4}

    asyncio.run(graph.ainvoke(_opener_state(), cfg))        # 块1：白首句落盘
    state1 = asyncio.run(last_state(graph, "dup"))
    assert_public_only(state1)
    assert [m["content"] for m in state1["messages"]] == ["（开局）", "我们走吧。"]
    assert state1["blocks"] == 1 and state1["closed"] is False

    asyncio.run(graph.ainvoke(_step_state(turn=2), cfg))    # 块2：逐字复读 → 抑制
    state2 = asyncio.run(last_state(graph, "dup"))
    assert_public_only(state2)
    assert len(state2["messages"]) == 2, "复读块不得新增消息"
    assert [m["content"] for m in state2["messages"]] == ["（开局）", "我们走吧。"]
    assert state2["blocks"] == 2, "被抑制的复读块仍推进块钟"
    assert state2["closed"] is False, "抑制不得收束场景"

    asyncio.run(graph.ainvoke(_step_state(turn=3), cfg))    # 块3：全新内容 → 不抑制
    state3 = asyncio.run(last_state(graph, "dup"))
    assert_public_only(state3)
    assert len(state3["messages"]) == 3
    assert state3["messages"][-1]["content"] == "换个新话题吧。"
    assert state3["messages"][-1]["speaker"] == "乙"
