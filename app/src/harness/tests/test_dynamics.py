"""dynamics 纯单元测试：沉默压力/邻接/唤醒混合/目标压力/场景压力/相关性/快照。

全部离线、确定性、零 IO（不碰 LangGraph/后端）。用 dict 表达 Weights/ThinkResult 等价物。
"""
import math

from harness.dynamics import CharDynamics, Dynamics, DynamicsParams


def _W(**kw) -> dict:
    """缺省 0.5 的权重 dict（对应 Weights schema 字段名）。"""
    base = {"w1_relevance": 0.5, "w2_arousal": 0.5, "w3_adjacency": 0.5,
            "w4_goal_pressure": 0.5, "w5_talkativeness": 0.5,
            "w6_inhibition": 0.5, "w7_scene_pressure": 0.5}
    base.update(kw)
    return base


def _think(**kw) -> dict:
    """ThinkResult 等价 dict，缺省全中性。"""
    base = {"aroused": 0.0, "obligation_fulfilled": [], "goal_progress": 0.0,
            "addressed": None, "impression_of_speaker": None, "urge": 0.0}
    base.update(kw)
    return base


def _approx(a, b, tol=1e-6) -> bool:
    return math.isclose(a, b, abs_tol=tol)


# ------------------------------------------------------------------ 沉默压力（headline）
def test_long_silent_bid_overtakes_recent_talker_after_monologue_blocks():
    """A 连说若干块（每次 mark_spoke 只归零 A 自己、累加 B），B 沉默计数一路涨；
    到仲裁时 B 的数值 bid 必然反超 A —— 这就是「久未开口者自然拿回话筒」。"""
    dyn = Dynamics(["丁", "戊"])
    # A 有较高基础（刚被点名/上头）→ 前几块由 A 赢；B 一直没开口。
    dyn.states["丁"].adjacency = 0.9
    dyn.states["丁"].arousal = 0.8
    dyn.states["丁"].relevance = 0.9
    for _ in range(4):                     # A 独角戏 4 块
        dyn.round_start()
        dyn.mark_spoke("丁")

    a_bid = dyn.bid("丁", _W())
    b_bid = dyn.bid("戊", _W())
    assert dyn.states["丁"].turns_since_spoke == 0
    assert dyn.states["戊"].turns_since_spoke == 4
    # 刚说完的 A（recency 惩罚 + 0 沉默压力）远低于沉默了 4 块的 B。
    assert b_bid > a_bid, f"久未开口者应反超：B={b_bid} A={a_bid}"

    # 关键机制：沉默压力随块数单调上涨（B 每多静默一块 bid 更高）。
    low = Dynamics(["戊"]).bid("戊", _W())
    dyn2 = Dynamics(["戊"])
    dyn2.states["戊"].turns_since_spoke = 5
    high = dyn2.bid("戊", _W())
    assert high > low


def test_silence_lets_quiet_character_cross_speak_threshold():
    """基础 bid 在开口阈值下时，沉默压力把它抬过阈值 → 有人开口而非死寂。"""
    dyn = Dynamics(["戊"], params=DynamicsParams(silence_gain=0.5))
    base_bid = dyn.bid("戊", _W())
    assert base_bid < 0.1                     # 冷启动没人想开口
    dyn.states["戊"].turns_since_spoke = 3  # 静坐 3 块后压力积累
    risen = dyn.bid("戊", _W())
    assert risen >= 0.1                       # 抬过开口阈值


# ------------------------------------------------------------------ 邻接
def test_addressed_or_mentioned_boosts_adjacency_else_decays():
    dyn = Dynamics(["丁", "戊"])
    # 被点名/被正文提到 → adjacency +address_boost 0.8 (cap 1)
    dyn.observe("丁", _think(), addressed_to="丁")
    assert _approx(dyn.states["丁"].adjacency, 0.8)
    dyn.observe("丁", _think(), content="丁，你怎么看？")
    assert _approx(dyn.states["丁"].adjacency, 1.0)     # +0.8 封顶 1
    # 无关消息 → 衰减 ×0.75
    dyn.observe("丁", _think(), content="戊，安静。")
    assert _approx(dyn.states["丁"].adjacency, 0.75)


def test_naming_uses_stronger_address_boost_gain():
    """点名/被提及的邻接抬升量由 address_boost 控（0.5 → 0.8，可注入覆盖）。"""
    dyn = Dynamics(["丁"])
    dyn.observe("丁", _think(), addressed_to="丁")
    assert _approx(dyn.states["丁"].adjacency, 0.8)
    weak = Dynamics(["丁"], params=DynamicsParams(address_boost=0.3))
    weak.observe("丁", _think(), addressed_to="丁")
    assert _approx(weak.states["丁"].adjacency, 0.3)


def test_obligation_fulfilled_lowers_adjacency():
    # 对照：同一中性 observe，带 obligation 的一方邻接额外 ×0.4。
    plain = Dynamics(["丁"])
    paid = Dynamics(["丁"])
    plain.states["丁"].adjacency = 1.0
    paid.states["丁"].adjacency = 1.0
    plain.observe("丁", _think(), content="（旁人闲话）")
    paid.observe("丁", _think(obligation_fulfilled=["还了人情"]),
                 content="（旁人闲话）")
    assert _approx(plain.states["丁"].adjacency, 0.75)      # 仅未点名衰减
    assert _approx(paid.states["丁"].adjacency, 0.30)       # ×0.4
    assert _approx(paid.states["丁"].adjacency
                   / plain.states["丁"].adjacency, 0.4)


# ------------------------------------------------------------------ 唤醒混合/衰减
def test_arousal_blends_toward_think_reported_value():
    dyn = Dynamics(["戊"])
    dyn.observe("戊", _think(aroused=0.8))
    assert _approx(dyn.states["戊"].arousal, 0.48)        # 0.6·0.8
    dyn.observe("戊", _think(aroused=0.0))
    assert _approx(dyn.states["戊"].arousal, 0.48 * 0.4)  # 混合回落向报告值
    # 自定义 blend
    dyn2 = Dynamics(["戊"], params=DynamicsParams(arousal_blend=1.0))
    dyn2.observe("戊", _think(aroused=0.3))
    assert _approx(dyn2.states["戊"].arousal, 0.3)


def test_tick_silence_decays_arousal_and_adjacency_by_emotion_rate():
    dyn = Dynamics(["丁"], emotion_rates={"丁": 0.4})
    dyn.states["丁"].arousal = 1.0
    dyn.states["丁"].adjacency = 1.0
    dyn.tick_silence()
    assert _approx(dyn.states["丁"].arousal, 0.6)
    assert _approx(dyn.states["丁"].adjacency, 0.6)


# ------------------------------------------------------------------ 目标压力
def test_goal_pressure_high_while_progress_low_then_falls_when_done():
    dyn = Dynamics(["丁"])
    assert dyn.states["丁"].goal_pressure == 0.3        # 初始基线
    dyn.observe("丁", _think(goal_progress=0.1))        # 刚起了个头 → 想说
    assert dyn.states["丁"].goal_progress == 0.1
    early = dyn.states["丁"].goal_pressure
    assert early > 0.3, "推进尚低 → 目标压力应抬升"
    dyn.observe("丁", _think(goal_progress=0.4))        # 推进过半 → 压力回落
    later = dyn.states["丁"].goal_pressure
    assert later < early, "说完了（推进满）→ 目标压力自然让出话筒"
    # 累积封顶 1
    dyn2 = Dynamics(["丁"])
    dyn2.observe("丁", _think(goal_progress=0.9))
    dyn2.observe("丁", _think(goal_progress=0.9))
    assert _approx(dyn2.states["丁"].goal_progress, 1.0)


# ------------------------------------------------------------------ 场景压力
def test_scene_pressure_from_clock_proximity_raises_bid():
    dyn = Dynamics(["丁", "戊"])
    dyn.set_scene_pressure(0.9)
    for st in dyn.states.values():
        assert _approx(st.scene_pressure, 0.9)
    low_dyn = Dynamics(["丁"])
    high_dyn = Dynamics(["丁"])
    high_dyn.set_scene_pressure(1.0)
    w = _W(w7_scene_pressure=0.5)
    assert high_dyn.bid("丁", w) > low_dyn.bid("丁", w)
    # 越界 clamp
    dyn.set_scene_pressure(-3.0)
    assert all(_approx(s.scene_pressure, 0.0) for s in dyn.states.values())


# ------------------------------------------------------------------ 相关性
def test_relevance_mention_heuristic():
    dyn = Dynamics(["丁"])
    assert dyn.states["丁"].relevance == 0.4
    dyn.observe("丁", _think(), content="那丁，你说呢？")
    assert _approx(dyn.states["丁"].relevance, 0.9)
    for _ in range(30):                       # 连续无人提 → 向 floor 衰减
        dyn.observe("丁", _think(), content="（旁人闲话）")
    assert _approx(dyn.states["丁"].relevance, 0.15, tol=1e-3)


# ------------------------------------------------------------------ 轮次书
def test_mark_spoke_and_tick_silence_advance_turns():
    dyn = Dynamics(["丁", "戊"])
    dyn.mark_spoke("丁")
    assert dyn.states["丁"].turns_since_spoke == 0
    assert dyn.states["戊"].turns_since_spoke == 1
    assert dyn.last_speaker == "丁"
    dyn.tick_silence()
    assert dyn.states["丁"].turns_since_spoke == 1
    assert dyn.states["戊"].turns_since_spoke == 2


def test_bid_deterministic_and_tiebreak_noise_is_stable():
    d1 = Dynamics(["丁"])
    d2 = Dynamics(["丁"])
    assert d1.bid("丁", _W()) == d2.bid("丁", _W())
    assert d1.bid("丁", _W()) == d1.bid("丁", _W())
    r0 = d1.bid("丁", _W())
    d1.round_start()                          # 下一轮噪声应变化（但仍确定）
    d2.round_start()                          # 两个实例推到同一轮号
    # 噪声只由 (轮号, 名) 决定，与实例身份/构造时刻无关：同轮同名的 bid 必逐值相同。
    assert d1.bid("丁", _W()) == d2.bid("丁", _W())
    assert d1.bid("丁", _W()) == d1.bid("丁", _W())
    # 但换了一轮，噪声确实变了（否则「打破严格等值」的平局噪声形同虚设）
    assert r0 != d1.bid("丁", _W())


def test_weights_drive_adjacency_arousal_contribution():
    dyn = Dynamics(["丁"])
    dyn.states["丁"].adjacency = 1.0
    dyn.states["丁"].arousal = 1.0
    low = dyn.bid("丁", _W(w3_adjacency=0.0, w2_arousal=0.0))
    high = dyn.bid("丁", _W(w3_adjacency=1.0, w2_arousal=1.0))
    assert high - low > 0.9                    # w2/w3 各贡献 1.0 的量级
    # 抑制权重：越高越不敢抢话
    timid = dyn.bid("丁", _W(w6_inhibition=1.0))
    bold = dyn.bid("丁", _W(w6_inhibition=0.0))
    assert timid < bold


def test_snapshot_exposes_all_fields_per_character():
    dyn = Dynamics(["丁", "戊"])
    dyn.states["丁"].turns_since_spoke = 2
    snap = dyn.snapshot()
    assert set(snap) == {"丁", "戊"}
    keys = {"turns_since_spoke", "arousal", "adjacency", "relevance",
            "goal_progress", "goal_pressure", "scene_pressure", "self_urge",
            "pending_reply", "turns_pending"}
    assert set(snap["丁"]) == keys
    assert snap["丁"]["turns_since_spoke"] == 2
    # 快照是拷贝，改快照不污染内部
    snap["丁"]["arousal"] = 99.0
    assert dyn.states["丁"].arousal == 0.0


def test_CharDynamics_defaults_match_design_initial_values():
    cd = CharDynamics()
    assert (cd.turns_since_spoke, cd.arousal, cd.adjacency, cd.relevance,
            cd.goal_progress, cd.goal_pressure, cd.scene_pressure, cd.self_urge,
            cd.pending_reply, cd.turns_pending) == \
        (0, 0.0, 0.0, 0.4, 0.0, 0.3, 0.0, 0.0, 0.0, 0)


# ------------------------------------------------- 自报冲动计权（self_urge，headline）
def test_observe_stores_self_urge_including_negative():
    """observe 把 think.urge 存成 self_urge，范围与 schema 一致 -1..2（负数合法：
    本人明确「这句我不必说」）。越界按 -1/2 截断。"""
    dyn = Dynamics(["丁", "戊"])
    dyn.observe("丁", _think(urge=1.4))
    assert _approx(dyn.states["丁"].self_urge, 1.4)
    dyn.observe("丁", _think(urge=-1.0))          # 明确的负冲动（不想说）
    assert _approx(dyn.states["丁"].self_urge, -1.0)
    dyn.observe("戊", _think(urge=-3.0))            # 低于 -1 → 截到 -1（不是 0）
    assert _approx(dyn.states["戊"].self_urge, -1.0)
    dyn.observe("戊", _think(urge=9.0))             # 高于 2 → 截到 2
    assert _approx(dyn.states["戊"].self_urge, 2.0)
    # 缺省 0.0（旧 ThinkResult 无 urge 时不留脏值）
    dyn.observe("戊", _think())
    assert _approx(dyn.states["戊"].self_urge, 0.0)
    # 快照暴露该字段
    assert _approx(dyn.snapshot()["丁"]["self_urge"], -1.0)


def test_bid_moves_by_urge_gain_times_self_urge():
    """bid 的 self_urge 项 = urge_gain·self_urge（默认 0.6）：同一状态仅自报冲动不同
    → bid 差值恰为 0.6·Δ，且负 urge 是**负贡献**。"""
    dyn = Dynamics(["丁"])
    w = _W()
    dyn.states["丁"].self_urge = 2.0
    high = dyn.bid("丁", w)
    dyn.states["丁"].self_urge = -1.0
    low = dyn.bid("丁", w)
    assert _approx(high - low, DynamicsParams().urge_gain * 3.0)   # 0.6·(2-(-1))
    dyn.states["丁"].self_urge = 0.0
    zero = dyn.bid("丁", w)
    assert _approx(high - zero, 0.6 * 2.0)
    assert _approx(zero - low, 0.6 * 1.0)
    # 可注入权重
    d2 = Dynamics(["丁"], params=DynamicsParams(urge_gain=1.0))
    d2.states["丁"].self_urge = 2.0
    d3 = Dynamics(["丁"], params=DynamicsParams(urge_gain=1.0))
    assert _approx(d2.bid("丁", w) - d3.bid("丁", w), 2.0)


def test_negative_urge_loses_to_equal_peer_with_higher_urge():
    """用户要求的行为：状态其余全同、只有自报冲动不同时，urge=-1 的一方**输给** peer
    （负冲动确实压低发言权）；反过来 urge 高者赢。"""
    w = _W()
    quiet = Dynamics(["丁", "戊"])
    loud = Dynamics(["丁", "戊"])
    for d, ur in ((quiet, {"丁": -1.0, "戊": 0.5}),
                  (loud, {"丁": 2.0, "戊": -1.0})):
        for name, u in ur.items():
            d.states[name].self_urge = u
    assert quiet.bid("丁", w) < quiet.bid("戊", w), \
        "urge=-1 应输给状态相同、urge 更高的 peer"
    assert loud.bid("丁", w) > loud.bid("戊", w)


def test_urge_gain_zero_disables_self_urge_term():
    """urge_gain=0.0：退回旧行为——自报冲动完全不影响 bid（back-compat 路径）。"""
    w = _W()
    a = Dynamics(["丁"], params=DynamicsParams(urge_gain=0.0))
    b = Dynamics(["丁"], params=DynamicsParams(urge_gain=0.0))
    a.states["丁"].self_urge = 2.0
    b.states["丁"].self_urge = -1.0
    assert a.bid("丁", w) == b.bid("丁", w)
    # 状态本身仍被记录（只是不进 bid）——显示/日志不受影响
    assert _approx(a.states["丁"].self_urge, 2.0)


def test_negative_urge_cannot_override_pending_obligation():
    """可调权重而非否决权：自报 -1 压不过「被点名欠答」的硬义务（封顶 ±1.2 < 1.2 起的
    义务量级），被问者即使不想说也得作答。"""
    dyn = Dynamics(["丁", "戊"])
    dyn.observe("丁", _think(urge=-1.0), content="丁，你怎么看？")
    dyn.states["戊"].self_urge = 2.0
    assert dyn.bid("丁", _W()) > dyn.bid("戊", _W()), \
        "欠答义务不该被负自报冲动抵消"


# ------------------------------------------------- 被点名欠答（pending_reply，headline）
def test_naming_sets_pending_and_beats_mere_silence_pressure():
    """被点名 = 欠一次回答：邻接抬升之外立刻记一笔 pending_base 的义务，且**光凭沉默
    压力的旁听者抢不过它**（旧参数下两者接近，点名者常被别人的沉默压力压过）。"""
    dyn = Dynamics(["丁", "戊"])
    dyn.states["戊"].turns_since_spoke = 3      # 对方只有沉默压力（0.5·3·0.7=1.05）
    dyn.observe("丁", _think(), content="丁，你怎么看？")
    st = dyn.states["丁"]
    assert st.pending_reply == DynamicsParams().pending_base == 1.2
    assert st.turns_pending == 0                 # 刚被问，还没欠过整轮
    assert dyn.bid("丁", _W()) > dyn.bid("戊", _W()), \
        "被点名者应凭欠答义务压过只有沉默压力的旁听者"
    # 没被点名的一方不会被凭空记义务。
    assert dyn.states["戊"].pending_reply == 0.0


def test_unanswered_pending_doubles_each_round_then_caps():
    """未答的每一轮义务翻倍（1.2 → 2.4 → 4.8 …）直到 pending_cap 封顶；
    mark_spoke(他人) 与 tick_silence() 两条路径同样累进。"""
    dyn = Dynamics(["乙", "甲"])
    dyn.observe("乙", _think(), content="乙，你先说。")
    assert dyn.states["乙"].pending_reply == 1.2
    seq = []
    for _ in range(8):                           # 甲 连说 8 块，乙 一直没答
        dyn.mark_spoke("甲")
        seq.append(dyn.states["乙"].pending_reply)
    assert seq[:6] == [2.4, 4.8, 9.6, 19.2, 38.4, 64.0]
    assert seq[6:] == [64.0, 64.0], "封顶后不得再涨（不会爆）"
    assert dyn.states["乙"].turns_pending == 8
    # 无人开口的静默块同属「还没答」→ 同样累进。
    dyn.tick_silence()
    assert dyn.states["乙"].pending_reply == 64.0
    assert dyn.states["乙"].turns_pending == 9
    # 不欠答的人不受累进影响。
    assert dyn.states["甲"].pending_reply == 0.0
    assert dyn.states["甲"].turns_pending == 0


def test_pending_cleared_when_named_character_speaks():
    """答了即清零：说话人自己的义务归零、轮数归零；此后再过轮也不复活。"""
    dyn = Dynamics(["乙", "甲"])
    dyn.observe("乙", _think(), content="乙，你先说。")
    dyn.mark_spoke("甲")
    assert dyn.states["乙"].pending_reply == 2.4
    assert dyn.states["乙"].turns_pending == 1
    dyn.mark_spoke("乙")                         # 乙 答了
    st = dyn.states["乙"]
    assert st.pending_reply == 0.0 and st.turns_pending == 0
    dyn.mark_spoke("甲")                         # 之后他人再说，也不该复活
    dyn.tick_silence()
    assert st.pending_reply == 0.0 and st.turns_pending == 0


def test_new_ask_restarts_count_without_lowering_obligation():
    """再次被点名 = 新一轮欠答：轮数从头计（0），但义务取 max——不因重新点名而变轻。"""
    dyn = Dynamics(["乙", "甲"])
    dyn.observe("乙", _think(), content="乙？")
    dyn.mark_spoke("甲")
    assert dyn.states["乙"].pending_reply == 2.4 and dyn.states["乙"].turns_pending == 1
    dyn.observe("乙", _think(), content="乙，我问你呢。")
    st = dyn.states["乙"]
    assert st.turns_pending == 0, "重新点名应把未答轮数重起"
    assert st.pending_reply == 2.4, "义务取 max：重新点名不把已累积的压力打回原值"
    dyn.mark_spoke("甲")
    assert st.turns_pending == 1 and st.pending_reply == 4.8
    # 旁人（从未被点名）始终零义务。
    assert dyn.states["甲"].pending_reply == 0.0


def test_unanswered_third_party_keeps_escalating_while_others_speak():
    """丙被点名后一直没答，甲乙轮流说话：丙 的义务照旧每轮翻倍，最终数值 bid 反超。"""
    dyn = Dynamics(["丙", "甲", "乙"])
    dyn.observe("丙", _think(), content="丙，这事你怎么说？")
    assert dyn.states["丙"].pending_reply == 1.2
    for i in range(3):
        dyn.mark_spoke("甲" if i % 2 == 0 else "乙")
        assert dyn.states["甲"].pending_reply == 0.0
        assert dyn.states["乙"].pending_reply == 0.0
    assert dyn.states["丙"].pending_reply == 9.6
    assert dyn.states["丙"].turns_pending == 3
    assert dyn.bid("丙", _W()) > max(dyn.bid("甲", _W()), dyn.bid("乙", _W())), \
        "欠答压力的数值 bid 应最终压过仍在轮流开口的旁人"
