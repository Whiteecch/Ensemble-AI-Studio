import asyncio
import json
from pathlib import Path

import pytest

from harness.backends.stub import StubBackend
from harness.dynamics import DynamicsParams
from harness.engine import SceneEngine


def _write(tmp_path: Path):
    import json
    (tmp_path / "餐厅.json").write_text(json.dumps({
        "name": "餐厅", "participants": ["丁", "戊"],
        "hard_boundary": {"type": "time", "value": "22:00", "desc": "打烊"}}, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "丁.json").write_text(json.dumps({"name": "丁",
        "personality": {"描述": "冷静"}}, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "戊.json").write_text(json.dumps({"name": "戊",
        "personality": {"描述": "锐利"}}, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "models.yaml").write_text(
        "think:\n  backend: stub\n  model: stub\n  params: {}\n"
        "speak:\n  backend: stub\n  model: stub\n  params: {}\n", encoding="utf-8")
    return (tmp_path / "餐厅.json", tmp_path / "丁.json",
            tmp_path / "戊.json", tmp_path / "models.yaml")


def test_engine_boundary_closes_and_exports(tmp_path: Path):
    scene_p, a_p, b_p, models_p = _write(tmp_path)
    eng = SceneEngine(scene_p, [a_p, b_p], models_p,
                      run_root=tmp_path / "runs", bid_path=None,
                      closing_at_block=4)
    events = asyncio.run(eng.run_to_close())
    assert events and any(e["type"] == "scene_state" and e["payload"]["closed"]
                          for e in events)
    assert eng.closed is True

    out = tmp_path / "t.md"
    asyncio.run(eng.export_transcript(out))
    text = out.read_text(encoding="utf-8")
    assert "丁" in text or "戊" in text


def test_engine_inject_adds_director_message(tmp_path):
    scene_p, a_p, b_p, models_p = _write(tmp_path)
    eng = SceneEngine(scene_p, [a_p, b_p], models_p,
                      run_root=tmp_path / "runs", closing_at_block=99)
    asyncio.run(eng.open_scene())
    asyncio.run(eng.inject("（电话响了。）"))
    msgs = asyncio.run(eng.messages())
    assert any(m["content"] == "（电话响了。）" and m["speaker_type"] == "director"
               for m in msgs)


def test_engine_sqlite_persistence_forces_async_saver(tmp_path: Path):
    """评审修复轮 1 回归：run_root 已存在时必须走 AsyncSqliteSaver（async-only），
    open_scene/step/messages/closed/export_transcript 全部工作，不得 NotImplementedError。"""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    scene_p, a_p, b_p, models_p = _write(tmp_path)
    runs = tmp_path / "runs"
    runs.mkdir(parents=True)             # 强制 sqlite 持久化路径

    # 无 running loop 下构造必须安全（惰性构图：不触碰 checkpointer / graph）
    eng = SceneEngine(scene_p, [a_p, b_p], models_p, run_root=runs,
                      bid_path=None, closing_at_block=4)
    assert eng._graph is None and eng._saver is None

    async def _drive():
        events = await eng.open_scene()
        # 构图发生在本 loop 内，且走 AsyncSqliteSaver（非 MemorySaver 掩盖）
        assert isinstance(eng._saver, AsyncSqliteSaver)
        assert (runs / "scene.sqlite").exists()
        assert await eng.messages()       # 导演开场已进 checkpoint
        guard = 0
        while not eng.closed:             # closed 读镜像，绝不经 sync get_state
            events += await eng.step(1)
            guard += 1
            assert guard < 80, "should close within closing_at_block=4"
        assert eng.closed is True
        out = tmp_path / "sqlite.md"
        await eng.export_transcript(out)
        text = out.read_text(encoding="utf-8")
        assert "丁" in text or "戊" in text
        await eng.aclose()
        return events

    events = asyncio.run(_drive())        # 单 loop：AsyncSqliteSaver 绑定本 loop
    assert any(e["type"] == "scene_state" and e["payload"]["closed"] for e in events)
    assert eng.closed is True             # loop 回收后镜像仍可读


def _silent_entry() -> dict:
    """全员 think 全零 → 数值 bid 恒低于开口阈值：全程无 speak、turn 不推进。"""
    return {"aroused": 0.0, "obligation_fulfilled": [], "goal_progress": 0.0,
            "addressed": None, "impression_of_speaker": None, "urge": 0.0}


def test_engine_all_silent_closes_by_block_clock(tmp_path: Path):
    """I2(最终评审) 回归：全静默场景没有 speak、turn 永不推进，收束必须靠 world
    自己的块钟 blocks（silence_gate→world 每块 +1），而非 turn。

    §7.5 数值化后「真静默」= 无唤醒/无目标且**沉默压力关闭**（silence_gain=0）——
    否则沉默压力会把久未开口者抬过阈值（这正是本特性的目的）。测试注入该参数以
    隔离出「无人想开口」的死寂语义，验证只靠块钟收束。"""
    scene_p, a_p, b_p, models_p = _write(tmp_path)
    eng = SceneEngine(scene_p, [a_p, b_p], models_p, run_root=tmp_path / "runs",
                      closing_at_block=4,
                      dynamics_params=DynamicsParams(silence_gain=0.0))
    eng.think_backend = StubBackend(json_script=[_silent_entry()])   # 恒静默

    events = asyncio.run(eng.run_to_close(max_blocks=60))
    assert eng.closed is True
    assert any(e["type"] == "scene_state" and e["payload"]["closed"] for e in events)
    # 收束消息已落地（块钟兜底「自动收束」文案，非时间打烊行）；且全程无人开口
    # （turn 恒 0）——旧 turn 钟在此会永不收束
    msgs = asyncio.run(eng.messages())
    assert any("自动收束" in m["content"] and m["speaker_type"] == "director"
               for m in msgs)
    assert all(m["turn"] == 0 for m in msgs)


def test_engine_init_rejects_participant_without_card(tmp_path: Path):
    """I4(最终评审)：场景参与者缺角色卡 → 构图前 fail fast。"""
    scene_p, a_p, b_p, models_p = _write(tmp_path)
    (tmp_path / "餐厅.json").write_text(json.dumps({
        "name": "餐厅", "participants": ["丁", "戊", "己"]},
        ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="缺少角色卡"):
        SceneEngine(scene_p, [a_p, b_p], models_p, run_root=tmp_path / "runs")


def test_engine_init_rejects_duplicate_card_name(tmp_path: Path):
    """I4(最终评审)：角色卡重名（同名文件重复/两份同名卡）→ 构图前 fail fast。"""
    scene_p, a_p, b_p, models_p = _write(tmp_path)
    with pytest.raises(ValueError, match="重名"):
        SceneEngine(scene_p, [a_p, a_p], models_p, run_root=tmp_path / "runs")


def test_engine_open_scene_accepts_custom_opening(tmp_path: Path):
    """open_scene(opening)：给了开场指令就替代默认导演开场；缺省仍用原文。"""
    scene_p, a_p, b_p, models_p = _write(tmp_path)
    eng = SceneEngine(scene_p, [a_p, b_p], models_p, run_root=tmp_path / "runs")
    asyncio.run(eng.open_scene("黄昏的海边，两人并肩看潮。"))
    msgs = asyncio.run(eng.messages())
    assert msgs and msgs[0]["speaker_type"] == "director"
    assert msgs[0]["content"] == "黄昏的海边，两人并肩看潮。"

    eng2 = SceneEngine(scene_p, [a_p, b_p], models_p, run_root=tmp_path / "runs2")
    asyncio.run(eng2.open_scene())                        # 缺省
    msgs2 = asyncio.run(eng2.messages())
    assert msgs2[0]["content"] == "夜色渐深，两人对坐。"


def _urge_entry(urge: float) -> dict:
    return {"aroused": 0.5, "obligation_fulfilled": [], "goal_progress": 0.0,
            "addressed": None, "impression_of_speaker": None, "urge": urge}


def test_engine_demo_alternate_forces_alternation_over_blocks(tmp_path: Path):
    """SceneEngine(demo_alternate=True) 透传 build_graph：发言由**数值 bid** 决定
    （§7.5 沉默压力/邻接演化），给丁更高的邻接开局 → 块1 白拿话筒；demo 叠加后
    ≥3 块内两人轮流、无连续同人（白 萧 白）。不再依赖自报 urge 的高低脚本。"""
    scene_p, a_p, b_p, models_p = _write(tmp_path)
    eng = SceneEngine(scene_p, [a_p, b_p], models_p, run_root=tmp_path / "runs",
                      closing_at_block=99, demo_alternate=True)
    eng._dynamics.states["丁"].adjacency = 0.8     # 白开局更投入 → 先开口
    eng._dynamics.states["丁"].relevance = 0.9
    eng.think_backend = StubBackend(json_script=[_urge_entry(0.9), _urge_entry(0.5)] * 3)
    eng.speak_backend = StubBackend(line_script=["白句一。", "萧句一。",
                                                 "白句二。", "萧句二。", "白句三。"])

    asyncio.run(eng.open_scene())
    for _ in range(3):
        asyncio.run(eng.step(1))

    msgs = asyncio.run(eng.messages())
    speakers = [m["speaker"] for m in msgs if m.get("speaker_type") == "character"]
    assert speakers[:3] == ["丁", "戊", "丁"]
    for a, b in zip(speakers, speakers[1:]):
        assert a != b, "demo 交替下引擎内不得出现连续同人 speak"


# ------------------------------------------- 以所选角色卡为准的演员表（cast_from_cards） --
def _write_three(tmp_path: Path):
    """场景只声明 2 人（甲/乙）的「旧格式场景文件」（participants，无 characters）
    + 3 张角色卡（甲/乙/丙）的路径。旧键由 Scene 装载时迁移进 characters。"""
    (tmp_path / "scene3.json").write_text(json.dumps({
        "name": "茶室", "participants": ["甲", "乙"]}, ensure_ascii=False),
        encoding="utf-8")
    paths = []
    for name in ("甲", "乙", "丙"):
        p = tmp_path / f"{name}.json"
        p.write_text(json.dumps({"name": name, "personality": {"描述": name}},
                                ensure_ascii=False), encoding="utf-8")
        paths.append(p)
    models = tmp_path / "models3.yaml"
    models.write_text(
        "think:\n  backend: stub\n  model: stub\n  params: {}\n"
        "speak:\n  backend: stub\n  model: stub\n  params: {}\n", encoding="utf-8")
    return tmp_path / "scene3.json", paths, models


def _rotating_bid(path: Path) -> Path:
    """开口阈值 0、无在位者优势 → 仲裁只看数值 bid 的 argmax（配 recency 惩罚自然轮转）。"""
    path.write_text("interruption_threshold: 0.0\nspeak_threshold: 0.0\n"
                    "silence_k: 100000\n", encoding="utf-8")
    return path


def test_engine_stored_cast_wins_over_selected_cards(tmp_path: Path):
    """场景持有阵容（《界面与场景自由度》§3.1）：场景存着甲/乙，选 3 张卡也还是一 2 人在场。

    cast_from_cards 旧口径（用所选卡覆盖场景名单）会让场景永远存不住人——这正是根因 1。
    """
    scene_p, cards, models_p = _write_three(tmp_path)
    eng = SceneEngine(scene_p, cards, models_p, run_root=tmp_path / "runs",
                      bid_path=_rotating_bid(tmp_path / "bid.yaml"),
                      closing_at_block=40, cast_from_cards=True)
    assert eng.scene.participants == ["甲", "乙"], "场景文件里的名单说了算"
    assert "丙" in eng.cards, "选中的卡照常装载（库里有卡），只是没被拉进这场"

    asyncio.run(eng.open_scene())
    for _ in range(20):
        if eng.closed:
            break
        asyncio.run(eng.step(1))
    speakers = {m["speaker"] for m in asyncio.run(eng.messages())
                if m.get("speaker_type") == "character"}
    assert speakers == {"甲", "乙"}, f"未被场景持有的丙不得开口，实际 {speakers}"

    snap = eng.dynamics_snapshot()
    assert set(snap) == {"甲", "乙"}, "数值动态快照只含在场者"
    assert all("bid" in snap[n] for n in snap)


def test_engine_cast_from_cards_seeds_a_scene_without_cast(tmp_path: Path):
    """cast_from_cards=True 的**新口径** = 播种：只在场景没存过阵容时才用所选卡填上。

    空 characters 的场次（新建场景）→ 3 张卡全进场、各开口；一存过名单就不再被覆盖。
    """
    scene_p, cards, models_p = _write_three(tmp_path)
    scene_p.write_text(json.dumps({"name": "新场", "characters": []},
                                  ensure_ascii=False), encoding="utf-8")
    eng = SceneEngine(scene_p, cards, models_p, run_root=tmp_path / "runs",
                      bid_path=_rotating_bid(tmp_path / "bid.yaml"),
                      closing_at_block=40, cast_from_cards=True)
    assert eng.scene.participants == ["甲", "乙", "丙"]
    # 演员表写进 characters（不再是 participants 字段），全部从零起算入场记录。
    assert [m.name for m in eng.scene.characters] == ["甲", "乙", "丙"]
    assert all(m.entered_round == 0 and m.entered_at == "" for m in eng.scene.characters)

    asyncio.run(eng.open_scene())
    for _ in range(20):
        if eng.closed:
            break
        asyncio.run(eng.step(1))
    speakers = {m["speaker"] for m in asyncio.run(eng.messages())
                if m.get("speaker_type") == "character"}
    assert speakers == {"甲", "乙", "丙"}, f"三人应各开口，实际 {speakers}"
    assert set(eng.dynamics_snapshot()) == {"甲", "乙", "丙"}


def test_engine_cast_from_cards_false_keeps_scene_participants(tmp_path: Path):
    """缺省（False）＝场景名单说了算：多选的卡不进场；旧 participants 键迁移进
    characters（顺序保留、入场记录为空）。"""
    scene_p, cards, models_p = _write_three(tmp_path)
    eng = SceneEngine(scene_p, cards, models_p, run_root=tmp_path / "runs",
                      closing_at_block=6)
    assert eng.scene.participants == ["甲", "乙"]
    assert [(m.name, m.entered_round) for m in eng.scene.characters] == [("甲", 0), ("乙", 0)]
    asyncio.run(eng.run_to_close())
    speakers = {m["speaker"] for m in asyncio.run(eng.messages())
                if m.get("speaker_type") == "character"}
    assert "丙" not in speakers, "未进场的卡不得开口"
    assert set(eng.dynamics_snapshot()) == {"甲", "乙"}


def test_engine_keeps_entry_records_from_scene_characters(tmp_path: Path):
    """场景自带 characters 的入场记录（entered_at/entered_round）装载后原样保留——
    本阶段只承载数据、不做进场可见性过滤（S4）。"""
    scene_p, cards, models_p = _write_three(tmp_path)
    scene_p.write_text(json.dumps({
        "name": "茶室",
        "characters": [{"name": "甲", "entered_at": "21:35", "entered_round": 3},
                       {"name": "乙"}]}, ensure_ascii=False), encoding="utf-8")
    eng = SceneEngine(scene_p, cards, models_p, run_root=tmp_path / "runs")
    assert eng.scene.participants == ["甲", "乙"]
    assert [(m.name, m.entered_at, m.entered_round) for m in eng.scene.characters] == [
        ("甲", "21:35", 3), ("乙", "", 0)]


def test_engine_empty_cast_scene_opens_without_error(tmp_path: Path):
    """空场景（一个人都没有，§3.3）合法：构造/开场不报错；step 不产出角色台词，
    块钟照常推进（不崩、不重复计 turn）。"""
    scene_p, cards, models_p = _write_three(tmp_path)
    scene_p.write_text(json.dumps({"name": "空场", "characters": []},
                                  ensure_ascii=False), encoding="utf-8")
    eng = SceneEngine(scene_p, cards, models_p, run_root=tmp_path / "runs",
                      auto_narrate=False)
    assert eng.scene.participants == []
    assert eng.dynamics_snapshot() == {}

    asyncio.run(eng.open_scene())
    events = asyncio.run(eng.step(2))
    assert [e["payload"]["kind"] for e in events if e["type"] == "decision"] == [
        "block_done", "block_done"], "空场仍照常走块钟（每块一条 block_done）"
    msgs = asyncio.run(eng.messages())
    assert [m["speaker_type"] for m in msgs] == ["director"], "空场只有导演开场，无人开口"
    assert all(m["in_scene"] == "空场" for m in msgs), "在场轴 = 场景名"


def test_engine_empty_cast_still_narrates(tmp_path: Path):
    """空场景里场景 agent 照常推进（叙述是场景自己的路径，不依赖任何角色在场）。"""
    scene_p, cards, models_p = _write_three(tmp_path)
    scene_p.write_text(json.dumps({"name": "空场"}, ensure_ascii=False),
                       encoding="utf-8")
    eng = SceneEngine(scene_p, cards, models_p, run_root=tmp_path / "runs",
                      auto_narrate=False)
    eng.narrate_backend = StubBackend(line_script=["门外传来脚步声。"])
    asyncio.run(eng.open_scene())
    line = asyncio.run(eng.narrate_now())
    assert line is not None and line["speaker_type"] == "narrator"
    assert line["in_scene"] == "空场", "叙述落点 = 本场景名"
    assert len(asyncio.run(eng.messages())) == 2      # 导演开场 + 叙述


def test_engine_hard_boundary_none_has_no_clock_boundary(tmp_path: Path):
    """hard_boundary.type="none" 与「没有边界」等价：不设任何钟边界（§3.1）。"""
    scene_p, cards, models_p = _write_three(tmp_path)
    scene_p.write_text(json.dumps({
        "name": "茶室", "characters": [{"name": "甲"}],
        "hard_boundary": {"type": "none", "value": "", "desc": "无"}},
        ensure_ascii=False), encoding="utf-8")
    eng = SceneEngine(scene_p, cards, models_p, run_root=tmp_path / "runs")
    assert eng.boundary_seconds is None and eng.boundary_value is None
    assert eng.boundary_hhmm is None
    assert eng._clock_progress() is None, "无钟边界 → 场景压力不参与判定"


def test_engine_deepseek_without_api_key_raises(tmp_path: Path):
    """I4(最终评审)：deepseek 无 api_key 显式抛 ValueError（不依赖 assert，-O 下仍生效）。"""
    scene_p, a_p, b_p, models_p = _write(tmp_path)
    (tmp_path / "models.yaml").write_text(
        "think:\n  backend: deepseek\n  model: deepseek-chat\n  params: {}\n"
        "speak:\n  backend: deepseek\n  model: deepseek-chat\n  params: {}\n",
        encoding="utf-8")
    with pytest.raises(ValueError, match="api_key"):
        SceneEngine(scene_p, [a_p, b_p], models_p, run_root=tmp_path / "runs",
                    api_key=None)
