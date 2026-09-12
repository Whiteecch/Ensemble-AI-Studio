"""`dynamics` 里的「关系」两项与进离场通知（《人际关系与场景推进》§6.4）——纯单元测试。

§6.4 的表格只列了三行：`adjacency`（说话人是我关系近的人 → 更想接话）、`address_boost`
（被关系近的人点名 → 更想回应）、进离场通知。本文件把这三行钉成可执行的契约：

  · **亲密度只影响倾向，不决定结果**（铁律 4）。两项各乘 `DynamicsParams` 的具名增益，
    增益 × w3（≤1）就是上界；本文件直接断言「两项合起来的上界 < 仲裁的打断阈值」——
    亲密度再极端也推不动 `bidding.arbitrate` 的在位者优势；
  · **负亲密度不产生「更想说」**。因子在 0 处截断（理由写在 `dynamics.closeness_factor`
    的 docstring 里：§6.4 只写了"关系近 → 更想接话"，没写"关系差 → 更想反驳"；多给一条
    没有依据的行为通道，等于让一个 −100 悄悄改写发言权，而那正是"影响倾向"与"决定结果"
    之间那条线）；
  · **没有关系表 / 关掉增益时逐字节不变**：与"今天那条构造路"（不传 relations）**同一个
    浮点值**（`==`，不是近似）——这是铁律 3 的最小可证伪形式；
  · **进离场通知是一条确定性规则**：阈值是具名常量、可配置、中间地带一条不发、不看模型。

全部离线、零 IO、确定性（同一场同一轮永远同值）。
"""
from __future__ import annotations

import math
import re

from harness.bidding import BidParams
from harness.dynamics import (
    RELATION_NOTICE_LOW_THRESHOLD,
    RELATION_NOTICE_THRESHOLD,
    CharDynamics,
    Dynamics,
    DynamicsParams,
    closeness_factor,
    entry_notice_text,
)

#: emoji 扫描（§3.6 无 AI 味）：几何/杂项符号区 + 变体选择符 + 彩色 emoji。
#: 与 `test_gui_knowledge_editor` 同一个区间（不含箭头一类的排版符号）。
_EMOJI_RE = re.compile("[⌀-⏿☀-➿⬀-⯿️\U0001F000-\U0001FAFF]")


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


def _approx(a: float, b: float, tol: float = 1e-9) -> bool:
    return math.isclose(a, b, abs_tol=tol)


# ---------------------------------------------------------------- 亲密度因子 --
def test_closeness_factor_is_linear_in_the_positive_range():
    """正亲密度线性映射到 0..1（100 → 1.0，50 → 0.5）——量纲是「亲密度满格 = 一倍增益」。"""
    assert closeness_factor(None) == 0.0
    assert closeness_factor(0) == 0.0
    assert closeness_factor(100) == 1.0
    assert closeness_factor(50) == 0.5


def test_closeness_factor_clamps_negative_and_overflow_to_zero():
    """负亲密度与越界值都夹到 [0, 1]：负亲密度**不加也不减**（理由见模块 docstring）。

    这一条是"上界"的地基：因子有界 → 两项有界 → 一个 ±100 的亲密度顶不穿 bid。
    """
    assert closeness_factor(-1) == 0.0
    assert closeness_factor(-100) == 0.0
    assert closeness_factor(-9999) == 0.0
    assert closeness_factor(9999) == 1.0


# ------------------------------------------------------- 无关系表：逐字节不变 --
def test_bid_is_bit_identical_when_no_relations_are_wired():
    """不传 relations（今天的构造路）与传空表/空增益：bid **同一个浮点值**。

    铁律 3 的最小可证伪形式：只要这三条路里任何一条多出一个非零加数（哪怕 1e-18），
    这里的 `==` 立刻变红——不是近似比较，是逐位。
    """
    plain = Dynamics(["甲", "乙"])
    empty_map = Dynamics(["甲", "乙"], relations={})
    empty_row = Dynamics(["甲", "乙"], relations={"甲": {}})
    off = Dynamics(["甲", "乙"],
                   params=DynamicsParams(relation_adjacency_gain=0.0,
                                         relation_address_gain=0.0),
                   relations={"甲": {"乙": 100}})
    plain.states["甲"].adjacency = 0.7
    plain.states["甲"].turns_since_spoke = 2
    for other in (empty_map, empty_row, off):
        other.states["甲"].adjacency = 0.7
        other.states["甲"].turns_since_spoke = 2

    wanted = plain.bid("甲", _W())
    assert empty_map.bid("甲", _W()) == wanted
    assert empty_row.bid("甲", _W()) == wanted
    assert off.bid("甲", _W()) == wanted


def test_bid_is_bit_identical_when_the_close_one_is_not_the_last_speaker():
    """关系表在，但说话人不是我关系里的人 → bid 与没有关系表**逐位相同**。

    关系表本身不是成本：只有"这一项真的命中"时才该动 bid。
    """
    plain = Dynamics(["甲", "乙", "丙"])
    wired = Dynamics(["甲", "乙", "丙"],
                     relations={"甲": {"乙": 100}})
    for dyn in (plain, wired):
        dyn.mark_spoke("丙")          # 最后一句是无关的人说的
    assert wired.bid("甲", _W()) == plain.bid("甲", _W())


# ------------------------------------------------------ adjacency：想接话 --
def test_close_speaker_raises_bid_by_w3_times_the_gain():
    """说话人是我 100 亲密度的人 → 多出的正是 `w3 · relation_adjacency_gain · 1.0`。

    逐项断言（不是"变大了"）：系数就是 w3，因为 §6.4 把这一项记在 `adjacency（w3）`
    那一行——它与既有邻接同源，只是邻接由"被点到"驱动、这一项由"谁在说"驱动。
    """
    plain = Dynamics(["甲", "乙"])
    wired = Dynamics(["甲", "乙"], relations={"甲": {"乙": 100}})
    for dyn in (plain, wired):
        dyn.last_speaker = "乙"

    delta = wired.bid("甲", _W()) - plain.bid("甲", _W())
    assert _approx(delta, 0.5 * 0.15 * 1.0), f"多出的是 w3·gain·1.0，实际 {delta}"


def test_close_speaker_boost_scales_with_closeness():
    """幅度随亲密度线性：50 只有 100 的一半（"closeness 越高加得越多"）。"""
    def _delta(closeness: int) -> float:
        plain = Dynamics(["甲", "乙"])
        wired = Dynamics(["甲", "乙"],
                         relations={"甲": {"乙": closeness}})
        for dyn in (plain, wired):
            dyn.last_speaker = "乙"
        return wired.bid("甲", _W()) - plain.bid("甲", _W())

    assert _approx(_delta(100) / 2.0, _delta(50))
    assert _approx(_delta(50) / 5.0, _delta(10))


def test_negative_and_zero_closeness_never_raise_bid():
    """0 与负亲密度都不加（负值**也不减**）：与没有关系表逐位相同。

    §6.4 只写了"关系近 → 更想接话"。让 −100 去压低别人的发言权是**另一条**行为
    （"见到仇人不敢开口"），设计文档没写、用户也没要求；而它一压低就会真的改变
    谁拿话筒（那是"决定结果"）。故这里截断在 0，方向单一、可解释。
    """
    plain = Dynamics(["甲", "乙"])
    plain.last_speaker = "乙"
    for closeness in (0, -1, -60, -100):
        wired = Dynamics(["甲", "乙"],
                         relations={"甲": {"乙": closeness}})
        wired.last_speaker = "乙"
        assert wired.bid("甲", _W()) == plain.bid("甲", _W()), closeness


def test_last_speaker_from_mark_spoke_drives_the_term():
    """走真实时序（mark_spoke 记最后说话人）时同样成立——不是只有手置字段才行。"""
    plain = Dynamics(["甲", "乙"])
    wired = Dynamics(["甲", "乙"], relations={"甲": {"乙": 80}})
    for dyn in (plain, wired):
        dyn.round_start()
        dyn.mark_spoke("乙")
    assert _approx(wired.bid("甲", _W()) - plain.bid("甲", _W()),
                   0.5 * 0.15 * 0.8)


# ------------------------------------------------------ address_boost：想回应 --
def test_being_addressed_by_a_close_one_raises_bid():
    """被关系近的人点名 → 更想回应（多出的正是 w3 · relation_address_gain · 因子）。"""
    plain = Dynamics(["甲", "乙"])
    wired = Dynamics(["甲", "乙"], relations={"甲": {"乙": 100}})
    for dyn in (plain, wired):
        dyn.observe("甲", _think(), addressed_to="甲",
                    content="甲，你怎么看？", speaker="乙")
    assert _approx(wired.bid("甲", _W()) - plain.bid("甲", _W()),
                   0.5 * 0.15 * 1.0)


def test_address_boost_is_zero_when_the_mentioner_is_not_close():
    """点名我的人亲密度为负 / 没给说话人 / 表里没有他 → 一项都不加（与无关系表逐位相同）。"""
    for relations in ({"甲": {"乙": -90}}, {"甲": {"丙": 100}}, {}):
        plain = Dynamics(["甲", "乙"])
        wired = Dynamics(["甲", "乙"], relations=relations)
        for dyn in (plain, wired):
            dyn.observe("甲", _think(), addressed_to="甲",
                        content="甲，你怎么看？", speaker="乙")
        assert wired.bid("甲", _W()) == plain.bid("甲", _W()), relations


def test_address_boost_needs_an_actual_mention():
    """这一块没人点我（点的是别人）→ 不加，且上一块的点名**不再留着**。

    留着的后果是"被关系近的人点过一次，此后每一块都更想说"——那不是"更想回应"，
    那是永久加成，且与既有 adjacency 的衰减口径自相矛盾。
    """
    plain = Dynamics(["甲", "乙"])
    wired = Dynamics(["甲", "乙"], relations={"甲": {"乙": 100}})
    for dyn in (plain, wired):
        dyn.observe("甲", _think(), addressed_to="乙", content="乙说话。",
                    speaker="乙")            # 点的是别人，我只是旁听
    assert wired.states["甲"].last_addresser == ""
    assert wired.bid("甲", _W()) == plain.bid("甲", _W())


def test_address_boost_is_cleared_by_the_next_unaddressed_block():
    """点过我一轮之后，下一轮没人点我 → 加成回落为零（逐位对齐无关系表）。"""
    plain = Dynamics(["甲", "乙"])
    wired = Dynamics(["甲", "乙"], relations={"甲": {"乙": 100}})
    for dyn in (plain, wired):
        dyn.observe("甲", _think(), addressed_to="甲", content="你来。",
                    speaker="乙")
        dyn.observe("甲", _think(), addressed_to="乙", content="我说完了。",
                    speaker="乙")
    assert wired.bid("甲", _W()) == plain.bid("甲", _W())


# ------------------------------------------------------------------ 上界 --
def test_relation_terms_cannot_overturn_the_incumbent_advantage():
    """两项合起来的上界 < 仲裁的打断阈值 → 亲密度再极端也抢不过在位者（铁律 4）。

    `bidding.arbitrate` 的第 5 条：挑战者要 ≥ 在位者 + `interruption_threshold` 才夺话筒。
    上界取自**参数自身**（增益 × w3 的最大值 1.0）× 2，故这是一条与实现绑定的硬边界，
    不是"大概够小"：任何把增益调到能顶穿阈值的改动都会在这里变红。
    """
    p = DynamicsParams()
    ceiling = (p.relation_adjacency_gain + p.relation_address_gain) * 1.0
    assert ceiling < BidParams().interruption_threshold, (
        f"关系两项的上界 {ceiling} 必须小于打断阈值 "
        f"{BidParams().interruption_threshold}——否则亲密度会从「影响倾向」变成「决定结果」")


def test_relation_terms_are_capped_even_at_extreme_closeness():
    """一次真实竞价里的经验上界：±100 全开时，两项多出来的正好等于增益 × w3 之和。"""
    plain = Dynamics(["甲", "乙"])
    wired = Dynamics(["甲", "乙"], relations={"甲": {"乙": 100}})
    for dyn in (plain, wired):
        dyn.round_start()
        dyn.mark_spoke("乙")
        dyn.observe("甲", _think(), addressed_to="甲", content="你来。",
                    speaker="乙")
    delta = wired.bid("甲", _W()) - plain.bid("甲", _W())
    assert _approx(delta, (0.15 + 0.15) * 0.5), delta


# ---------------------------------------------------------- 挂载与可配置 --
def test_relations_can_be_wired_after_construction():
    """`set_relations` 后加的挂载与前装同效（引擎每块刷新、重播时构造完再挂）。"""
    dyn = Dynamics(["甲", "乙"])
    dyn.last_speaker = "乙"
    before = dyn.bid("甲", _W())
    dyn.set_relations({"甲": {"乙": 100}})
    assert dyn.bid("甲", _W()) > before
    dyn.set_relations({})                  # 清回去 → 与最初逐位相同
    assert dyn.bid("甲", _W()) == before


def test_gains_are_named_params_with_documented_defaults():
    """两项的增益是 `DynamicsParams` 上的具名参数（可单测、可用参数关掉）。"""
    p = DynamicsParams()
    assert p.relation_adjacency_gain > 0.0
    assert p.relation_address_gain > 0.0
    off = DynamicsParams(relation_adjacency_gain=0.0, relation_address_gain=0.0)
    assert (off.relation_adjacency_gain, off.relation_address_gain) == (0.0, 0.0)


def test_last_addresser_is_a_char_dynamics_field():
    """`last_addresser` 落在 CharDynamics 上（与既有字段同一处），缺省空串。"""
    assert CharDynamics().last_addresser == ""


# ---------------------------------------------------- 进离场通知（§6.4） --
def test_notice_thresholds_are_named_constants_and_param_defaults():
    """阈值是具名常量，且 `DynamicsParams` 的缺省就是那两个常量（唯一的定义源）。"""
    assert RELATION_NOTICE_THRESHOLD > 0
    assert RELATION_NOTICE_LOW_THRESHOLD == -RELATION_NOTICE_THRESHOLD
    p = DynamicsParams()
    assert p.relation_notice_threshold == RELATION_NOTICE_THRESHOLD
    assert p.relation_notice_low_threshold == RELATION_NOTICE_LOW_THRESHOLD


def test_entry_notice_fires_above_the_threshold_and_names_the_person():
    """"他来了"这件事在我心里有分量：高于正阈值 → 给一句通知，且点出是谁来了。"""
    text = entry_notice_text(RELATION_NOTICE_THRESHOLD + 1, "乙")
    assert text and "乙" in text


def test_entry_notice_is_silent_at_and_below_the_threshold():
    """阈值本身与中间地带一条都不发（"高于"是严格大于：边界不含）。"""
    for closeness in (RELATION_NOTICE_THRESHOLD, RELATION_NOTICE_THRESHOLD - 1,
                      0, RELATION_NOTICE_LOW_THRESHOLD + 1,
                      RELATION_NOTICE_LOW_THRESHOLD, None):
        assert entry_notice_text(closeness, "乙") is None, closeness


def test_entry_notice_fires_below_the_negative_threshold_with_a_different_line():
    """低于负阈值同理（"我心里一沉"）：发通知，且**与正那一条措辞不同**。

    措辞不同是硬要求：两句相同的话等于把"有分量"与"一沉"合并成一个信号，而这两种
    处境在角色心里是相反的。
    """
    warm = entry_notice_text(RELATION_NOTICE_THRESHOLD + 1, "乙")
    cold = entry_notice_text(RELATION_NOTICE_LOW_THRESHOLD - 1, "乙")
    assert cold and "乙" in cold
    assert cold != warm


def test_entry_notice_threshold_is_configurable():
    """阈值可配置（具名参数）：调高到 90 之后，80 的亲密度不再触发。"""
    assert entry_notice_text(80, "乙") is not None
    strict = DynamicsParams(relation_notice_threshold=90,
                            relation_notice_low_threshold=-90)
    assert entry_notice_text(80, "乙", strict) is None
    assert entry_notice_text(95, "乙", strict) is not None


def test_entry_notice_is_deterministic_and_free_of_emoji():
    """确定性（同输入同输出、不盖时间戳）+ 无 emoji（§3.6 的文风纪律）。"""
    first = entry_notice_text(100, "乙")
    second = entry_notice_text(100, "乙")
    assert first == second
    for text in (first, entry_notice_text(-100, "乙")):
        assert text is not None
        assert not _EMOJI_RE.search(text), text
