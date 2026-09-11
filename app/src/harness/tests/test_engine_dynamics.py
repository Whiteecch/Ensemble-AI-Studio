"""引擎 e2e：数值动态竞价驱动发言权（§7.4/7.5）——offline stub、确定性。

核心回归（替代旧"独角戏上限"依赖）：发言由 harness 数值 bid 决定，而**自报 urge 只是
bid 里的一项可调权重（urge_gain·self_urge，默认 0.6、封顶 ±1.2）**，不是唯一决定权。
状态驱动路径（沉默压力/邻接/唤醒…）仍照旧：A 说完一块即因 recency + 不续被点名而
回落，久未开口的 B 凭沉默压力自然拿回话筒 → turn-taking 涌现，无需任何"同人连说 N
句即停"的刹车；自报 urge 专题另见下方 self_urge 测试。
"""
import asyncio
import json
from pathlib import Path

from harness.backends.stub import StubBackend
from harness.dynamics import Dynamics
from harness.engine import SceneEngine


def _build(tmp_path: Path) -> SceneEngine:
    (tmp_path / "餐厅.json").write_text(json.dumps({
        "name": "餐厅", "participants": ["戊", "己"],
        "hard_boundary": {"type": "time", "value": "22:00", "desc": "打烊"}},
        ensure_ascii=False), encoding="utf-8")
    for name in ("戊", "己"):
        (tmp_path / f"{name}.json").write_text(json.dumps(
            {"name": name, "personality": {"描述": "测试"}}, ensure_ascii=False),
            encoding="utf-8")
    (tmp_path / "models.yaml").write_text(
        "think:\n  backend: stub\n  model: stub\n  params: {}\n"
        "speak:\n  backend: stub\n  model: stub\n  params: {}\n", encoding="utf-8")
    return SceneEngine(tmp_path / "餐厅.json",
                       [tmp_path / "戊.json", tmp_path / "己.json"],
                       tmp_path / "models.yaml", run_root=tmp_path / "runs",
                       closing_at_block=99)


def _rows() -> list[dict]:
    """双方中性 think（aroused 0、urge 0）：发言完全由数值状态（沉默压力/邻接…）决定。"""
    return [
        {"aroused": 0.0, "obligation_fulfilled": [], "goal_progress": 0.0,
         "addressed": None, "impression_of_speaker": None, "urge": 0.0},
        {"aroused": 0.0, "obligation_fulfilled": [], "goal_progress": 0.0,
         "addressed": None, "impression_of_speaker": None, "urge": 0.0},
    ]


def _urge_rows(a_urge: float, b_urge: float) -> list[dict]:
    """两份中性 think、仅自报 urge 不同（按 fan-out 顺序：首名听众多得 a_urge）。"""
    return [
        {"aroused": 0.0, "obligation_fulfilled": [], "goal_progress": 0.0,
         "addressed": None, "impression_of_speaker": None, "urge": a_urge},
        {"aroused": 0.0, "obligation_fulfilled": [], "goal_progress": 0.0,
         "addressed": None, "impression_of_speaker": None, "urge": b_urge},
    ]


def _char_speakers(msgs: list[dict]) -> list[str]:
    return [m["speaker"] for m in msgs if m.get("speaker_type") == "character"]


async def _drive(eng: SceneEngine, blocks: int) -> None:
    await eng.open_scene()
    for _ in range(blocks):
        if eng.closed:
            break
        await eng.step(1)


def test_numeric_bid_turn_taking_without_monologue_cap(tmp_path: Path):
    """A 开局被点名/上头 → 块1 开口；随后不续被点名、A 情绪回落，B 沉默压力抬升
    → 块2 B 数值 bid 反超并开口。自报 urge 两侧同为中性 0，故这里只验状态驱动的轮转
    （自报冲动本身另见下面的 urge 专题测试）。"""
    eng = _build(tmp_path)
    # A 开局投入（数值状态），保证块1 由 A 拿话筒。
    eng._dynamics.states["戊"].adjacency = 1.0
    eng._dynamics.states["戊"].relevance = 0.9
    eng._dynamics.states["戊"].arousal = 1.0
    eng.think_backend = StubBackend(json_script=_rows())
    eng.speak_backend = StubBackend(
        line_script=["白的话。", "萧终于拿到话筒。", "白再续一句。", "萧再一句。"])

    asyncio.run(_drive(eng, 4))

    assert eng.closed is False, "数值 turn-taking 不应触发任何收束"
    msgs = asyncio.run(eng.messages())
    speakers = _char_speakers(msgs)
    assert speakers and speakers[0] == "戊", "块1 应由被点名/上头者戊开口"
    assert "己" in speakers, "久未开口的己应凭沉默压力自然开口（turn-taking）"
    assert speakers[-1] != speakers[0] or len(set(speakers)) > 1, \
        "不应退化成同一人独角戏（数值竞价 + 沉默压力）"
    assert eng.dynamics_snapshot()["戊"]["turns_since_spoke"] >= 0


def test_engine_feeds_clock_scene_pressure_into_dynamics(tmp_path: Path):
    """worker 注入虚拟钟 → 引擎每块把 `clamp((clock-start)/(boundary-start))` 折进
    Dynamics.set_scene_pressure；打烊越近场景压力越高，bid 相应抬升（w7 项）。"""
    eng = _build(tmp_path)                       # 场景硬边界 21:30→22:00
    assert eng.start_hhmm == "21:30" and eng.boundary_hhmm == "22:00"
    eng.set_clock_now(eng.start_seconds + 20 * 60)   # 21:50：走了 20/30
    eng.think_backend = StubBackend(json_script=_rows())
    eng.speak_backend = StubBackend(line_script=["甲", "乙", "丙", "丁"])

    asyncio.run(_drive(eng, 1))
    snap = eng.dynamics_snapshot()
    for st in snap.values():
        assert abs(st["scene_pressure"] - 20.0 / 30.0) < 1e-6, \
            "块开局应把时钟临近度写进每名角色的 scene_pressure"

    # 场景压力确实进入 bid（同一状态仅 scene_pressure 不同 → w7·Δ 抬升）
    far = Dynamics(["戊"])
    near = Dynamics(["戊"])
    near.set_scene_pressure(1.0)
    w = {"w1_relevance": 0.5, "w2_arousal": 0.5, "w3_adjacency": 0.5,
         "w4_goal_pressure": 0.5, "w5_talkativeness": 0.5,
         "w6_inhibition": 0.5, "w7_scene_pressure": 0.5}
    assert near.bid("戊", w) - far.bid("戊", w) > 0.4


def _neutral_rows(n: int = 30) -> list[dict]:
    """恒中性 think 行（无唤醒/无目标推进）：发言只由数值 bid 决定。"""
    row = {"aroused": 0.0, "obligation_fulfilled": [], "goal_progress": 0.0,
           "addressed": None, "impression_of_speaker": None, "urge": 0.0}
    return [dict(row) for _ in range(n)]


def _build_named(tmp_path: Path) -> SceneEngine:
    """甲乙二人场（乙高抑制 w6=1.0）：乙未被点名时抢不过甲 → 「点名→欠答→翻倍→答」
    的时序可被确定性断言（不是靠运气轮流）。"""
    (tmp_path / "茶室.json").write_text(json.dumps({
        "name": "茶室", "participants": ["甲", "乙"]}, ensure_ascii=False),
        encoding="utf-8")
    (tmp_path / "甲.json").write_text(json.dumps(
        {"name": "甲", "personality": {"描述": "测试"}}, ensure_ascii=False),
        encoding="utf-8")
    (tmp_path / "乙.json").write_text(json.dumps(
        {"name": "乙", "personality": {"描述": "克制"},
         "weights": {"w6_inhibition": 1.0}}, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "models.yaml").write_text(
        "think:\n  backend: stub\n  model: stub\n  params: {}\n"
        "speak:\n  backend: stub\n  model: stub\n  params: {}\n", encoding="utf-8")
    bid = tmp_path / "bid.yaml"
    bid.write_text("interruption_threshold: 0.4\nspeak_threshold: 0.0\n"
                   "silence_k: 100000\n", encoding="utf-8")
    return SceneEngine(tmp_path / "茶室.json",
                       [tmp_path / "甲.json", tmp_path / "乙.json"],
                       tmp_path / "models.yaml", run_root=tmp_path / "runs",
                       bid_path=bid, closing_at_block=99)


def test_addressed_character_owes_reply_and_answers_within_two_blocks(tmp_path: Path):
    """被点名 = 欠一次回答（§7.4）：甲的块1 台词点名乙 → 乙 欠答且**未答则每轮翻倍**，
    因此乙在感知到点名后的下一块（点名后第 2 块）就拿回话筒作答——不是拖好几轮；
    作答后义务清零。快照在欠答期间可见 pending_reply/turns_pending。
    """
    eng = _build_named(tmp_path)
    # 甲开局投入（数值状态）→ 块1 由甲拿话筒并说出点名乙的那句。
    eng._dynamics.states["甲"].adjacency = 1.0
    eng._dynamics.states["甲"].relevance = 0.9
    eng._dynamics.states["甲"].arousal = 1.0
    eng.think_backend = StubBackend(json_script=_neutral_rows())
    eng.speak_backend = StubBackend(
        line_script=["乙，你先说。", "（甲自续一句。）", "（乙应声。）", "（甲接一句。）"])

    asyncio.run(eng.open_scene())
    snaps: list[dict] = []
    for _ in range(3):
        asyncio.run(eng.step(1))
        snaps.append(eng.dynamics_snapshot())

    msgs = asyncio.run(eng.messages())
    speakers = _char_speakers(msgs)
    ask_block = 0                                   # 甲的块1 台词即「点名乙」那一句
    assert "乙" in (msgs[1]["content"] or ""), f"块1 台词应点名乙：{msgs[1]}"
    assert speakers[:2] == ["甲", "甲"], \
        f"块1/块2 应由甲开口（乙未被点名、高抑制不抢话）：{speakers}"

    # (a) 被点名后 1~2 块内必须回应（此例恰是感知后的下一块）。
    assert "乙" in speakers[ask_block + 1:ask_block + 3], \
        f"被点名者应在本块或下一块作答，不得拖成数轮：{speakers}"
    assert speakers[2] == "乙"

    # (b) 欠答项在未答期间可见：点名当块折入 pending_base，未答 → 翻倍。
    assert snaps[0]["乙"]["pending_reply"] == 0.0, "点名尚未折入（感知一拍延迟）"
    assert snaps[1]["乙"]["pending_reply"] == 2.4
    assert snaps[1]["乙"]["turns_pending"] == 1
    assert snaps[1]["乙"]["bid"] > snaps[1]["甲"]["bid"], \
        "欠答压力应把乙的数值 bid 抬过在位者（这正是它抢回话筒的原因）"

    # (c) 答后清零。
    assert snaps[2]["乙"]["pending_reply"] == 0.0
    assert snaps[2]["乙"]["turns_pending"] == 0


def test_engine_observe_records_self_urge_from_think(tmp_path: Path):
    """C：引擎无需改逻辑——observe 已收到 think 结果，自报 urge 因此落进 self_urge
    并出现在 dynamics_snapshot（GUI/日志可见），且按 urge_gain 计入 bid。"""
    eng = _build(tmp_path)
    eng.think_backend = StubBackend(json_script=_urge_rows(1.5, -1.0) * 4)
    eng.speak_backend = StubBackend(line_script=["甲句。", "乙句。"])
    asyncio.run(_drive(eng, 1))

    snap = eng.dynamics_snapshot()
    assert snap["戊"]["self_urge"] == 1.5, \
        "首名听众的 think.urge 应原样落进 self_urge"
    assert snap["己"]["self_urge"] == -1.0, "负冲动同样要留存（不是被 clamp 成 0）"
    # 同一状态下 bid 的差 = urge_gain·Δself_urge（0.6·2.5），证明它确实计权入 bid。
    from harness.dynamics import DynamicsParams
    assert abs((snap["戊"]["bid"] - snap["己"]["bid"])
               - DynamicsParams().urge_gain * 2.5) < 0.05


def test_negative_self_urge_needs_silence_pressure_to_be_beaten(tmp_path: Path):
    """C（用户要求的行为）：状态其余全同、只有自报冲动不同 → urge=2.0 的一方先拿话筒，
    且 urge=-1.0 的一方（明确"这句我不必说"）在最初几块内**不开口**；但自报冲动只是
    一项可调权重（±1.2），久未开口的沉默压力最终仍会把它抬回来说话权——不会永久封口。"""
    eng = _build(tmp_path)
    eng.think_backend = StubBackend(json_script=_urge_rows(2.0, -1.0) * 8)
    eng.speak_backend = StubBackend(
        line_script=["A1。", "A2。", "A3。", "B1。", "A4。", "B2。"])

    asyncio.run(eng.open_scene())
    for _ in range(3):
        asyncio.run(eng.step(1))
    early = _char_speakers(asyncio.run(eng.messages()))
    assert early and early[0] == "戊", "urge 高者先开口"
    assert "己" not in early, \
        f"urge=-1 的一方状态相同却应被判为不想说：{early}"

    for _ in range(9):                          # 继续：沉默压力累积
        asyncio.run(eng.step(1))
    speakers = _char_speakers(asyncio.run(eng.messages()))
    assert "己" in speakers, \
        "自报冲动只是一项权重：久未开口者的沉默压力最终应把话筒交回给他"


def test_legacy_urge_script_alone_cannot_hold_the_floor(tmp_path: Path):
    """回归（旧 test_urge_value_is_irrelevant_to_selection 的新语义版）：A 的 think.urge
    恒顶格 2.0 → 数值竞价下 A 也不再是独角戏：B 凭沉默压力最终拿回话筒。"""
    eng = _build(tmp_path)
    eng._dynamics.states["戊"].adjacency = 1.0
    eng._dynamics.states["戊"].relevance = 0.9
    eng._dynamics.states["戊"].arousal = 1.0
    eng.think_backend = StubBackend(json_script=_urge_rows(2.0, 0.0) * 6)
    eng.speak_backend = StubBackend(
        line_script=["A句。", "B句。", "A句2。", "B句2。", "A句3。", "B句3。"])

    asyncio.run(_drive(eng, 12))

    msgs = asyncio.run(eng.messages())
    speakers = _char_speakers(msgs)
    assert speakers[0] == "戊"
    assert "己" in speakers, "A 的自报 urge 恒 2.0 也不能独占话筒"
    assert any(b == "己" for a, b in zip(["戊"] + speakers, speakers)), \
        "戊说过后应出现让位给己的块"
