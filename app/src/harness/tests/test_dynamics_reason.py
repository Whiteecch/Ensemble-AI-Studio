"""`dynamics` 里的「进离场原因的倾向」（《人际关系与场景推进》§5.2）——纯单元测试。

§5.2 的三条口径在这里钉成可执行契约：

  · **倾向 = f(话痨程度 w5, 两人间亲密度, 是否点名)**：
      - 当事人（有原因还没说出口的那个人）**更想说**——话越多（w5 越高）越想说，
        听众里越有他关系近的人越想；
      - 他**关系近的人更想问**（被原因**点名**的人再想一点）；
  · **并入既有 bid**（§5.2 明写"作为一项加权、不另开一套"）：三项都是 `bid()` 里与
    adjacency/silence_gain 同一张表上的加数，乘的是**既有权重**（w5 / w3）与既有的
    亲密度因子（`closeness_factor`），不新增任何数值系统；
  · **有上界、不决定结果**（铁律 4）：三项之和 + §6.4 那两项之和 < 仲裁打断阈值——
    两者一起拉满也顶不穿在位者优势；
  · **没有原因 / 关掉参数 / 没有关系表** → 与今天**逐位相同**（`==`，不是近似）——
    这是铁律 3 在本功能上的可证伪形式。

纯逻辑、零 IO、确定性。
"""
from __future__ import annotations

import math

from harness.bidding import BidParams
from harness.dynamics import (
    CharDynamics,
    Dynamics,
    DynamicsParams,
    UnsaidReason,
)


def _W(**kw) -> dict:
    """缺省 0.5 的权重 dict（对应 Weights schema 字段名）。"""
    base = {"w1_relevance": 0.5, "w2_arousal": 0.5, "w3_adjacency": 0.5,
            "w4_goal_pressure": 0.5, "w5_talkativeness": 0.5,
            "w6_inhibition": 0.5, "w7_scene_pressure": 0.5}
    base.update(kw)
    return base


def _approx(a: float, b: float, tol: float = 1e-9) -> bool:
    return math.isclose(a, b, abs_tol=tol)


def _pair(relations: dict | None = None) -> tuple[Dynamics, Dynamics]:
    """(不带原因的对照, 带原因的) 两台动力学，状态逐位相同——差值就是那几项。"""
    plain = Dynamics(["甲", "乙"], relations=relations)
    wired = Dynamics(["甲", "乙"], relations=relations)
    return plain, wired


# ---------------------------------------------------------------- 具名参数 --
def test_gains_are_named_params_with_documented_defaults():
    """三项增益是 `DynamicsParams` 上的具名参数（可单测、可用参数关掉）。"""
    p = DynamicsParams()
    assert p.reason_tell_gain > 0.0
    assert p.reason_ask_gain > 0.0
    assert p.reason_named_gain > 0.0
    off = DynamicsParams(reason_tell_gain=0.0, reason_ask_gain=0.0,
                         reason_named_gain=0.0)
    assert (off.reason_tell_gain, off.reason_ask_gain, off.reason_named_gain) \
        == (0.0, 0.0, 0.0)


def test_unsaid_reason_defaults_to_an_empty_row():
    """`UnsaidReason` 的字段全有缺省：什么都没有 = 不产生任何加成。"""
    assert UnsaidReason() == UnsaidReason(audience=(), named=())


# ------------------------------------------------------- 逐位不变（铁律 3） --
def test_bid_is_bit_identical_without_any_unsaid_reason():
    """没有原因（没挂 / 挂空表 / 挂 None）/ 增益关掉：bid **同一个浮点值**。

    铁律 3 的最小可证伪形式：任何一条路多出一个非零加数（哪怕 1e-18），这里的 `==`
    立刻变红——不是近似比较，是逐位。
    """
    def _seed(dyn: Dynamics) -> Dynamics:
        dyn.states["甲"].adjacency = 0.7
        dyn.states["甲"].turns_since_spoke = 2
        return dyn

    plain = _seed(Dynamics(["甲", "乙"]))
    empty = _seed(Dynamics(["甲", "乙"]))
    empty.set_unsaid_reasons({})
    none_arg = _seed(Dynamics(["甲", "乙"]))
    none_arg.set_unsaid_reasons(None)
    off = _seed(Dynamics(
        ["甲", "乙"],
        params=DynamicsParams(reason_tell_gain=0.0, reason_ask_gain=0.0,
                              reason_named_gain=0.0),
        relations={"甲": {"乙": 100}, "乙": {"甲": 100}}))
    off.set_unsaid_reasons({"甲": UnsaidReason(audience=("乙",), named=("乙",))})

    wanted = plain.bid("甲", _W())
    for other in (empty, none_arg, off):
        assert other.bid("甲", _W()) == wanted
        assert other.bid("乙", _W()) == plain.bid("乙", _W())


def test_bid_is_bit_identical_without_a_relation_table():
    """挂了原因但没有关系表 → 因子恒 0 → 与今天逐位相同，且**不崩**（缺表是常态）。"""
    plain, wired = _pair(relations=None)
    wired.set_unsaid_reasons({"甲": UnsaidReason(audience=("乙",), named=("乙",))})
    assert wired.bid("甲", _W()) == plain.bid("甲", _W())
    assert wired.bid("乙", _W()) == plain.bid("乙", _W())


# ------------------------------------------------------------ 想说（当事人） --
def test_talkativeness_only_raises_the_holders_bid():
    """w5 高 → 当事人更想说，且**正比**；w5=0（闷葫芦）= 与没有原因逐位相同。"""
    p = DynamicsParams()

    def _delta(w5: float) -> float:
        plain, wired = _pair(relations={"甲": {"乙": 100}})
        wired.set_unsaid_reasons({"甲": UnsaidReason(audience=("乙",))})
        w = _W(w5_talkativeness=w5)
        return wired.bid("甲", w) - plain.bid("甲", w)

    assert _approx(_delta(1.0), p.reason_tell_gain)
    assert _approx(_delta(0.5), p.reason_tell_gain / 2.0)
    assert _delta(0.0) == 0.0


def test_the_holder_wants_to_tell_more_when_someone_close_is_listening():
    """听众里有关系近的人 → 更想说；关系越好加得越多（正比），没有那一行 → 一点不加。"""
    p = DynamicsParams()

    def _delta(closeness: int | None) -> float:
        rows = {"甲": {"乙": closeness}} if closeness is not None else {}
        plain, wired = _pair(relations=rows)
        wired.set_unsaid_reasons({"甲": UnsaidReason(audience=("乙",))})
        w = _W(w5_talkativeness=1.0)
        return wired.bid("甲", w) - plain.bid("甲", w)

    full = p.reason_tell_gain
    assert _approx(_delta(100), full)
    assert _approx(_delta(50), full / 2.0)
    assert _delta(0) == 0.0
    assert _delta(None) == 0.0


def test_the_holder_needs_someone_to_tell():
    """没有听众（他一个人在场）→ 想说这一项恒 0：想说得有听的人。"""
    plain, wired = _pair(relations={"甲": {"乙": 100}})
    wired.set_unsaid_reasons({"甲": UnsaidReason(audience=(), named=())})
    assert wired.bid("甲", _W(w5_talkativeness=1.0)) \
        == plain.bid("甲", _W(w5_talkativeness=1.0))


def test_the_holder_does_not_get_a_boost_from_a_stranger_in_the_room():
    """听众里只有关系一般/不认识的人 → 想说这一项不加（"关系近的人在场"才成立）。"""
    plain, wired = _pair(relations={"甲": {"乙": 5}})
    wired.set_unsaid_reasons({"甲": UnsaidReason(audience=("乙",))})
    w = _W(w5_talkativeness=1.0)
    delta = wired.bid("甲", w) - plain.bid("甲", w)
    assert _approx(delta, DynamicsParams().reason_tell_gain * 0.05)


# ------------------------------------------------------------ 想问（旁观者） --
def test_the_close_listener_wants_to_ask():
    """当事人是我关系近的人 → 我更想问（多出的正是 w3 · reason_ask_gain · 因子）。"""
    p = DynamicsParams()
    plain, wired = _pair(relations={"乙": {"甲": 100}})
    wired.set_unsaid_reasons({"甲": UnsaidReason(audience=("乙",))})
    delta = wired.bid("乙", _W(w3_adjacency=1.0)) - plain.bid("乙", _W(w3_adjacency=1.0))
    assert _approx(delta, 1.0 * p.reason_ask_gain)


def test_ask_scales_with_closeness_and_with_w3():
    """想问的幅度随亲密度与 w3 双双正比（与 §6.4 的接话项同一档、同一口径）。"""
    p = DynamicsParams()

    def _delta(closeness: int, w3: float) -> float:
        plain, wired = _pair(relations={"乙": {"甲": closeness}})
        wired.set_unsaid_reasons({"甲": UnsaidReason(audience=("乙",))})
        w = _W(w3_adjacency=w3)
        return wired.bid("乙", w) - plain.bid("乙", w)

    assert _approx(_delta(100, 1.0), p.reason_ask_gain)
    assert _approx(_delta(50, 1.0), p.reason_ask_gain / 2.0)
    assert _approx(_delta(100, 0.5), p.reason_ask_gain / 2.0)
    assert _delta(0, 1.0) == 0.0


def test_a_stranger_does_not_ask():
    """关系不到（0/负亲密度/表里没有这个人）→ 想问为 0：§5.2 只说"关系近的人会想问"。"""
    for relations in ({"乙": {"甲": 0}}, {"乙": {"甲": -80}}, {}, None):
        plain, wired = _pair(relations=relations)
        wired.set_unsaid_reasons({"甲": UnsaidReason(audience=("乙",))})
        w = _W(w3_adjacency=1.0)
        assert wired.bid("乙", w) == plain.bid("乙", w), relations


# ------------------------------------------------------------- 是否点名 --
def test_being_named_in_the_reason_makes_the_close_listener_ask_a_bit_more():
    """被原因**点名**的人再想问一点（多出的正是 w3 · reason_named_gain · 因子）。"""
    p = DynamicsParams()

    def _delta(named: tuple[str, ...]) -> float:
        plain, wired = _pair(relations={"乙": {"甲": 100}})
        wired.set_unsaid_reasons({"甲": UnsaidReason(audience=("乙",), named=named)})
        w = _W(w3_adjacency=1.0)
        return wired.bid("乙", w) - plain.bid("乙", w)

    assert _approx(_delta(()), p.reason_ask_gain)
    assert _approx(_delta(("乙",)), p.reason_ask_gain + p.reason_named_gain)


def test_being_named_helps_only_those_who_are_already_close():
    """点名不是一条独立通道：关系不到的人被点了名也一点不加（不越过"关系近"这道门槛）。"""
    plain, wired = _pair(relations={"乙": {"甲": 0}})
    wired.set_unsaid_reasons({"甲": UnsaidReason(audience=("乙",), named=("乙",))})
    w = _W(w3_adjacency=1.0)
    assert wired.bid("乙", w) == plain.bid("乙", w)


def test_naming_the_holder_himself_changes_nothing():
    """原因里点了当事人自己的名：对他自己既不加"想说"也不加"想问"（他自己不问他）。"""
    plain, wired = _pair(relations={"甲": {"乙": 100}, "乙": {"甲": 100}})
    wired.set_unsaid_reasons({"甲": UnsaidReason(audience=("乙",), named=("甲",))})
    w = _W(w3_adjacency=1.0, w5_talkativeness=1.0)
    assert _approx(wired.bid("甲", w) - plain.bid("甲", w),
                   DynamicsParams().reason_tell_gain)


# ------------------------------------------------------------- 多项与上界 --
def test_ask_takes_the_max_over_holders_not_the_sum():
    """多个当事人同时有一件没说出口的事：想问取**最大**的那一项，不叠加。

    叠加会让加成随在场人数无界增长——那是"决定结果"，也正是 §6.4 用因子夹住量纲要
    避免的。取最大 = "最压我的那件事"。
    """
    p = DynamicsParams()
    three = ["甲", "乙", "丙"]
    plain = Dynamics(three, relations={"乙": {"甲": 100, "丙": 40}})
    wired = Dynamics(three, relations={"乙": {"甲": 100, "丙": 40}})
    wired.set_unsaid_reasons({"甲": UnsaidReason(audience=("乙", "丙")),
                              "丙": UnsaidReason(audience=("乙", "甲"))})
    w = _W(w3_adjacency=1.0)
    delta = wired.bid("乙", w) - plain.bid("乙", w)
    assert _approx(delta, p.reason_ask_gain)      # 100 的那一项，不是 0.03+0.012


def test_a_holder_can_also_be_a_listener_of_another_holders_reason():
    """两个人各有一件没说出口的事：每人都拿"想说 + 想问"两项，各自有界。"""
    p = DynamicsParams()
    rows = {"甲": {"乙": 100}, "乙": {"甲": 100}}
    plain = Dynamics(["甲", "乙"], relations=rows)
    wired = Dynamics(["甲", "乙"], relations=rows)
    wired.set_unsaid_reasons({"甲": UnsaidReason(audience=("乙",)),
                              "乙": UnsaidReason(audience=("甲",))})
    w = _W(w3_adjacency=1.0, w5_talkativeness=1.0)
    want = p.reason_tell_gain + p.reason_ask_gain
    assert _approx(wired.bid("甲", w) - plain.bid("甲", w), want)
    assert _approx(wired.bid("乙", w) - plain.bid("乙", w), want)


def test_reason_and_relation_terms_together_stay_under_the_interruption_threshold():
    """上界（铁律 4）：三项原因 + §6.4 两项，**一起拉满**也小于仲裁的打断阈值。

    `bidding.arbitrate` 第 5 条：挑战者要 ≥ 在位者 + `interruption_threshold`（0.4）才
    夺话筒。这里的上界取自**参数自身**（每个因子最大 1.0），故这是一条与实现绑定的硬
    边界，不是"大概够小"：任何把增益调到能顶穿阈值的改动都会在这里变红。
    """
    p = DynamicsParams()
    ceiling = (p.reason_tell_gain + p.reason_ask_gain + p.reason_named_gain
               + p.relation_adjacency_gain + p.relation_address_gain)
    threshold = BidParams().interruption_threshold
    assert ceiling < threshold, (
        f"关系两项 + 原因三项的上界 {ceiling} 必须小于打断阈值 {threshold}"
        f"——否则这一整族加权会从「影响倾向」变成「决定结果」")
    # 三项各自也都有上界（0.1 以内），够把边缘上的人推过开口阈值、推不动在位者优势。
    assert max(p.reason_tell_gain, p.reason_ask_gain, p.reason_named_gain) \
        < BidParams().speak_threshold


def test_reason_terms_are_capped_even_at_extreme_closeness():
    """一次真实行情里的经验上界：关系拉满、话最多、还被点名，多出来的正好是三项之和。"""
    p = DynamicsParams()
    rows = {"甲": {"乙": 100}, "乙": {"甲": 100}}
    plain = Dynamics(["甲", "乙"], relations=rows)
    wired = Dynamics(["甲", "乙"], relations=rows)
    wired.set_unsaid_reasons({"甲": UnsaidReason(audience=("乙",), named=("乙",))})
    w = _W(w3_adjacency=1.0, w5_talkativeness=1.0)
    delta = wired.bid("甲", w) - plain.bid("甲", w)
    assert _approx(delta, p.reason_tell_gain)          # 甲自己：只有想说那一项
    delta_b = wired.bid("乙", w) - plain.bid("乙", w)
    assert _approx(delta_b, p.reason_ask_gain + p.reason_named_gain)


# --------------------------------------------------------------- 挂载语义 --
def test_set_unsaid_reasons_replaces_wholesale_and_is_readable():
    """整体替换（与 `set_relations` 同一套口径）：引擎每块重建一份，测试直接注入。"""
    dyn = Dynamics(["甲", "乙"])
    assert dyn.unsaid_reasons() == {}
    dyn.set_unsaid_reasons({"甲": UnsaidReason(audience=("乙",), named=("乙",))})
    assert dyn.unsaid_reasons() == {"甲": UnsaidReason(audience=("乙",), named=("乙",))}
    snapshot = dyn.unsaid_reasons()
    snapshot.clear()                      # 返回的必须是副本：改它不影响内部
    assert dyn.unsaid_reasons() != {}
    dyn.set_unsaid_reasons(None)
    assert dyn.unsaid_reasons() == {}


def test_unsaid_reason_for_an_unknown_character_is_harmless():
    """快照里出现一个不在这台动力学里的人 → 谁也不受影响，不崩（引擎刷新时的竞态兜底）。"""
    plain, wired = _pair(relations={"甲": {"乙": 100}})
    wired.set_unsaid_reasons({"丙": UnsaidReason(audience=("甲", "乙"), named=("甲",))})
    assert wired.bid("甲", _W()) == plain.bid("甲", _W())
    assert wired.bid("乙", _W()) == plain.bid("乙", _W())


def test_unsaid_reasons_do_not_touch_the_other_state_fields():
    """这项倾向只进 bid：一分钱都不改动 CharDynamics 的既有字段（§5.3 的边界）。"""
    plain, wired = _pair(relations={"甲": {"乙": 100}})
    wired.set_unsaid_reasons({"甲": UnsaidReason(audience=("乙",), named=("乙",))})
    for name in ("甲", "乙"):
        assert wired.states[name] == CharDynamics()
        assert plain.states[name] == wired.states[name]
