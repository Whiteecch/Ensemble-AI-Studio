"""私有态/印象回喂下一次 think + 引擎只读边通道（dynamic_states / think_log_tail）。

信息边界守则：think 是听众私有解读，新数据全部走**非共享**边通道——本听众记忆文件
(state.jsonl/impressions) 与 ctx.think_log 列表；共享态 PUBLIC_KEYS 白名单不得新增键。
"""
import asyncio
import json
from pathlib import Path

from harness import graph as graph_mod
from harness.backends.stub import StubBackend
from harness.dynamics import DynamicsParams
from harness.engine import SceneEngine
from harness.prompters import build_think_messages as real_build_think
from harness.tests.helpers import assert_public_only, last_state

PARTICIPANTS = {"甲", "乙"}


def _entry(urge: float, aroused: float = 0.5, addressed=None, obligation=None,
           impression=None, goal: float = 0.1) -> dict:
    return {"aroused": aroused, "obligation_fulfilled": obligation or [],
            "goal_progress": goal, "addressed": addressed,
            "impression_of_speaker": impression, "urge": urge}


def _build_engine(tmp_path: Path, closing_at_block: int = 99,
                  dynamics_params=None) -> SceneEngine:
    """tmp 素材 + 引擎。故意不预建 run_root（首个 async 调用走 MemorySaver，
    单 asyncio.run 驱动；角色记忆文件仍真实落盘到 run_root/<角色>/）。"""
    (tmp_path / "贝克街221B.json").write_text(json.dumps({
        "name": "贝克街221B", "participants": ["甲", "乙"],
        "hard_boundary": {"type": "time", "value": "22:00", "desc": "打烊"}},
        ensure_ascii=False), encoding="utf-8")
    (tmp_path / "甲.json").write_text(json.dumps(
        {"name": "甲", "personality": {"描述": "冷静"}}, ensure_ascii=False),
        encoding="utf-8")
    (tmp_path / "乙.json").write_text(json.dumps(
        {"name": "乙", "personality": {"描述": "锐利"}}, ensure_ascii=False),
        encoding="utf-8")
    (tmp_path / "models.yaml").write_text(
        "think:\n  backend: stub\n  model: stub\n  params: {}\n"
        "speak:\n  backend: stub\n  model: stub\n  params: {}\n", encoding="utf-8")
    return SceneEngine(tmp_path / "贝克街221B.json",
                       [tmp_path / "甲.json", tmp_path / "乙.json"],
                       tmp_path / "models.yaml", run_root=tmp_path / "runs",
                       closing_at_block=closing_at_block,
                       dynamics_params=dynamics_params)


async def _run_blocks(eng: SceneEngine, n: int) -> None:
    await eng.open_scene()
    for _ in range(n):
        if eng.closed:
            break
        await eng.step(1)


def test_dynamic_states_empty_before_any_think(tmp_path: Path):
    eng = _build_engine(tmp_path)
    assert eng.dynamic_states() == {}      # 无人 think → 无条目
    assert eng.think_log_tail() == []


def test_dynamic_states_and_think_log_match_stub_scripts(tmp_path: Path):
    """脚本 urge 按块交替消费：白高(1.0/0.9/1.0)、萧低(0.2/0.3/0.1)。
    3 块 → think_log 6 条；dynamic_states 给每人最近一次自报（含 urge）。
    白每块都开口 → messages 正常推进；共享态仍守 PUBLIC_KEYS 白名单。"""
    eng = _build_engine(tmp_path)
    eng.think_backend = StubBackend(json_script=[
        _entry(1.0, aroused=0.6), _entry(0.2),     # 块1：白、萧
        _entry(0.9, goal=0.5), _entry(0.3),        # 块2
        _entry(1.0), _entry(0.1),                  # 块3
    ])
    eng.speak_backend = StubBackend(
        line_script=["白的话一。", "白的话二。", "白的话三。"])

    asyncio.run(_run_blocks(eng, 3))

    # —— dynamic_states：每名参与者最近一次自报，键齐且 urge 对应脚本 ——
    states = eng.dynamic_states()
    assert set(states) == PARTICIPANTS
    for name, s in states.items():
        assert {"aroused", "goal_progress", "addressed",
                "obligation_fulfilled", "urge"} <= set(s)
        assert isinstance(s["urge"], (int, float))
    assert states["甲"]["urge"] == 1.0        # 白块3 urge=1.0
    assert states["乙"]["urge"] == 0.1          # 萧块3 urge=0.1

    # —— think_log 只读边通道：条数与块数、speaker 与脚本 urge 逐条对应 ——
    log = eng.think_log_tail()
    assert len(log) >= eng.metrics()["blocks"]
    assert [e["speaker"] for e in log] == ["甲", "乙"] * 3
    assert [e["result"]["urge"] for e in log] == [1.0, 0.2, 0.9, 0.3, 1.0, 0.1]
    assert [e["result"]["aroused"] for e in log][0] == 0.6
    for e in log:
        assert e["speaker"] in PARTICIPANTS
        assert "heard" in e and "at" in e and "result" in e

    # tail 切片
    assert [e["result"]["urge"] for e in eng.think_log_tail(2)] == [1.0, 0.1]

    # —— 共享态零私有键泄漏 ——
    state = asyncio.run(last_state(eng._graph, "scene1"))
    assert_public_only(state)


def test_private_state_and_impressions_feed_next_think(tmp_path: Path, monkeypatch):
    """块2 起 build_think_messages 应带非空近期状态；印象只在「确实形成过」时出现。
    白块1 对开场给出 impression_of_speaker → 块2 impressions 非空；萧无印象 → 空。"""
    eng = _build_engine(tmp_path)
    eng.think_backend = StubBackend(json_script=[
        _entry(0.6, impression="慢热但认真"), _entry(0.4),   # 块1：白、萧
        _entry(0.8), _entry(0.3),                            # 块2
    ])
    eng.speak_backend = StubBackend(line_script=["白的话。", "萧的话。"])

    seen: list[dict] = []

    def _spy(card, view_text, last_chunk_text, scene_text, **kw):
        seen.append({"listener": card.name,
                     "state_text": kw.get("state_text", ""),
                     "impressions_text": kw.get("impressions_text", "")})
        return real_build_think(card, view_text, last_chunk_text, scene_text,
                                state_text=kw.get("state_text", ""),
                                impressions_text=kw.get("impressions_text", ""))

    monkeypatch.setattr(graph_mod, "build_think_messages", _spy)

    asyncio.run(_run_blocks(eng, 2))

    white = [s for s in seen if s["listener"] == "甲"]
    xiao = [s for s in seen if s["listener"] == "乙"]
    assert len(white) == 2 and len(xiao) == 2
    # 块1 无从回喂 → 无私有态小节；块2 已各写过一次 think → 状态小节非空
    assert white[0]["state_text"] == ""
    assert white[1]["state_text"] != ""
    assert xiao[0]["state_text"] == ""
    assert xiao[1]["state_text"] != ""
    # 印象：白块1 对说话人形成过印象（记入 导演.md）→ 块2 回喂；萧没有 → 空
    assert white[1]["impressions_text"] != ""
    assert xiao[1]["impressions_text"] == ""

    # think_log 边通道在 monkeypatch 下照常累计
    log = eng.think_log_tail()
    assert len(log) == 4
    assert {e["speaker"] for e in log} == PARTICIPANTS


def test_think_log_entries_carry_full_thinkresult_and_heard(tmp_path: Path):
    """heard = 该听众当块看到的最新句；result 含 ThinkResult 全部 schema 字段。

    数值竞价下开局常是聆听块（无人开口）——注入低抑制让块1 即有角色产出，块2
    think 的 heard 才能落到真实台词上。"""
    eng = _build_engine(tmp_path,
                        dynamics_params=DynamicsParams(inhibition_base=0.0))
    eng.think_backend = StubBackend(json_script=[
        _entry(1.0, obligation=["还了人情"], impression="终于说了重点"), _entry(0.2),
        _entry(0.9), _entry(0.1),
    ])
    eng.speak_backend = StubBackend(line_script=["第一句台词。", "第二句台词。"])
    asyncio.run(_run_blocks(eng, 2))

    log = eng.think_log_tail()
    first_white = log[0]
    # 块1 全员只看到开场导演行
    assert first_white["speaker"] == "甲"
    assert first_white["heard"] == "夜晚的贝克街221B，二人临窗而坐。"
    fields = first_white["result"]
    assert {"aroused", "obligation_fulfilled", "goal_progress", "addressed",
            "impression_of_speaker", "urge"} == set(fields)
    assert fields["obligation_fulfilled"] == ["还了人情"]
    assert fields["impression_of_speaker"] == "终于说了重点"
    # 后几块 heard 落到真实台词
    assert any(e["heard"] and "台词" in e["heard"] for e in log[1:])
