"""每角色动态状态机 + 数值竞价（设计文档 §7，纯 harness 侧）。纯函数、零 IO、无 LangGraph。

§7 意图是把「谁开口」从 LLM 自报冲动（think.urge）换成 **harness 从每角色随情景
演化的动态状态 + 性格权重 + 沉默压力算出的数值 bid**：动态压力（上面是沉默压力——
越久没开口 bid 越高，自然会拿回话筒；其次是 relevance/arousal/adjacency/goal/scene
压力与情绪衰减）随情景演变。LLM think 仍负责每轮把私有状态变化折进 Dynamics
（observe 读 aroused/goal_progress/addressed/obligation_fulfilled），并把自报 urge
存成 self_urge 留给日志/GUI；自报 urge 同时以 **urge_gain·self_urge** 这一项加权计入
数值 bid（见 bid()）——**负数即「这句我不必说」，能把该角色的发言权压低**（可为负、
被封顶 2.0，故它只是一项可调权重，不再是唯一决定权）。

Deterministic：打破等值的噪声用 (round, name) 的稳定 CRC 哈希（非进程随机的内置
hash），同一场同一轮永远同值。

被点名 = 欠一次回答（pending_reply）：点名/被提及除了抬邻接，还立刻记下一笔**回应义务**
（pending_base）；只要该角色**还没开口**，这笔义务每过一轮就 ×pending_growth（2 倍）累进
（封顶 pending_cap），直接进数值 bid（见 bid()）——被点名者于是必然在随后 1~2 块内作答，
而不是被旁人的沉默压力压过去、拖好几轮；一旦他开口（mark_spoke），义务立即清零。
turns_pending 记「已欠了几轮」（被点名当轮起算，每过一轮未答 +1），供 GUI 显示
「（第 N 轮未答）」。

默认参数（一处集中，见 DynamicsParams 字段注释与 __init__ 文档）按设计文档「初值」
选取，测试可整体/按字段注入。字段语义：
  silence_gain        沉默压力增长系数：bid += silence_gain·turns·(0.4+0.6·w5)
  address_boost       点名/被提及时的邻接抬升量（0.8，cap 1；旧名 adjacency_boost=0.5）
  pending_base        被点名的瞬间欠下的回应义务（1.2）
  pending_growth      未答每一轮义务的倍增系数（2.0 → 1.2/2.4/4.8/…）
  pending_cap         义务封顶（64.0，防指数爆掉）
  recency_penalty     刚说过话者的回摆惩罚（-0.6，防同一人连篇）
  inhibition_base     w6 抑制项所乘的基准层位（1.0）
  adjacency_decay     未被点名时邻接逐轮衰减（×0.75 向 0）
  relevance_decay     未被提及相关性逐轮衰减（×0.9）
  relevance_floor     相关性下限（0.15）
  urge_gain           自报冲动（think.urge，-1~2）折进 bid 的权重：bid += urge_gain·self_urge
                      （0.6；设 0.0 即关掉该项，回到「只看状态」的旧行为）
  arousal_blend       新 think 唤醒折入私有态的混合比（0.6）
  obligation_adjacency_factor  兑现欠债（obligation_fulfilled 非空）时邻接折减（×0.4）
  goal_k / goal_scale / goal_baseline  goal_pressure = clamp(clamp((1-progress·k)·scale,0,1)+baseline)
  noise_scale         噪声分母
  relation_adjacency_gain  亲密度折进 adjacency 项的权重（§6.4 第一行；0.0 = 关掉）
  relation_address_gain    亲密度折进 address_boost 项的权重（§6.4 第二行；0.0 = 关掉）
  relation_notice_threshold / relation_notice_low_threshold  进离场通知的正/负阈值
  reason_tell_gain / reason_ask_gain / reason_named_gain
    进离场原因的**倾向**三项（§5.2）：当事人更想说 / 关系近的人更想问 / 被点名的再想
    一点（0.0 = 关掉那一项）

**亲密度（《人际关系与场景推进》§6.4）**：`closeness`（−100~100）只作为**加权项**进
`bid()`，绝不越过 `bidding.arbitrate` 的阈值与在位者优势——两项各乘一个具名增益与 w3，
增益 × w3（≤1）就是它的上界，且这个上界**小于**打断阈值（有测试钉住）。两项的原料来自
构造时/运行期注入的**关系快照** `{我: {对方: 亲密度}}`（`set_relations`，引擎每块刷新）：
`None` = 没有那一行/没有关系表 → 因子 0 → 与引进关系之前**逐位相同**。

负亲密度在因子处**截断为 0**（`closeness_factor` 的 docstring 写了理由）：§6.4 只写了
"关系近 → 更想接话/更想回应"，"关系差 → 更想反驳/更不敢说"是**另一条**行为通道，没写、
没测、也没有人要求；而它一旦生效就会真的改变谁拿话筒（那是"决定结果"）。故方向单一：
亲密度只往上加，加多少由 closeness 与增益决定，顶不穿阈值。

**进离场原因（§5.2）**：一件"还没说出口的事"（`UnsaidReason`，`set_unsaid_reasons`，
引擎每块刷新）让当事人**更想说**（× w5：话痨的人更藏不住）、让**他关系近的在场者更想
问**（× w3，与 §6.4 的接话/回应同一档）、让**被原因点名的人再想一点**。三项都只是
`bid()` 里与 adjacency/silence_gain 同一张表上的加数（§5.2 明写"不另开一套"），各有
具名增益、可单测、可关掉；三项之和与 §6.4 两项之和加起来仍**小于**打断阈值（测试对拍）。
说什么由模型决定——引擎只让这件事在他心里有分量（§5.3、铁律 4）。

进离场通知是**确定性规则**（不是让模型判断，也不新增任何模型调用）：`entry_notice_text`
把一行中文交给调用方去落行，本模块不碰 IO、不碰引擎。
"""
from __future__ import annotations

import zlib
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping

#: 进离场通知的**正**阈值（§6.4）：A 对 B 的亲密度**严格高于**它时，B 进场会通知 A
#: （"他来了"这件事在我心里有分量）。具名常量 + 参数位两处同源，见 `DynamicsParams`。
RELATION_NOTICE_THRESHOLD = 40

#: 进离场通知的**负**阈值（§6.4 的"低于负阈值同理"）：A 对 B 的亲密度**严格低于**它时，
#: 同样通知 A（我心里一沉）。与正阈值互为相反数（对称是刻意的：40 分"有分量"就该有
#: 40 分"一沉"，否则两个方向要各调一次）。
RELATION_NOTICE_LOW_THRESHOLD = -RELATION_NOTICE_THRESHOLD

# 说明：文档 docstring 是动力学的唯一权威注释。此处不再复制字段长注。
@dataclass(frozen=True)
class DynamicsParams:
    """竞价与状态演化的可调参数（缺省即设计初值，一处集中）。"""
    silence_gain: float = 0.5
    address_boost: float = 0.8   # 点名/被提及时的邻接抬升（旧值 0.5 → 0.8，点名更重）
    pending_base: float = 1.2    # 被点名瞬间欠下的回应义务
    pending_growth: float = 2.0  # 未答每过一轮的倍增系数
    pending_cap: float = 64.0    # 义务封顶（防指数爆掉）
    recency_penalty: float = 0.6
    inhibition_base: float = 1.0
    adjacency_decay: float = 0.75
    relevance_decay: float = 0.9
    relevance_floor: float = 0.15
    arousal_blend: float = 0.6
    obligation_adjacency_factor: float = 0.4
    goal_k: float = 2.0          # goal_pressure = clamp((1-progress·k)·scale)+baseline
    goal_scale: float = 1.2
    goal_baseline: float = 0.05
    urge_gain: float = 0.6       # 自报冲动折进 bid 的权重（bid += urge_gain·self_urge）
    noise_scale: float = 1_000_000.0
    # 亲密度折进 bid 的两项权重（§6.4）。它们是**无量纲增益**，乘的是 closeness_factor
    # （0..1）与 w3_adjacency，故两项的上界就是"增益 × 1.0"，与 ±100 的亲密度原始量纲无关。
    # 为什么不直接拿 closeness 当权重：它一次就能把 bid 顶穿几十上百，仲裁的阈值与在位者
    # 优势当场失去意义——§6.4 要的是"影响倾向"，不是"决定结果"。
    # 0.15 这个数是**按上界倒推**的：两项之和 0.3 严格小于 `bidding.BidParams.
    # interruption_threshold`（0.4，抢话筒要赢在位者的幅度），故哪怕两个人都满格亲密度、
    # w3 又拉满，这一项也翻不动在位者优势；而默认 w3=0.5 时单项最多 0.075，够把一个人从
    # 开口阈值（0.1）边上推过去（"更想接话"要的正是这个），又不足以替谁做决定。
    relation_adjacency_gain: float = 0.15  # 说话人是我关系近的人 → 更想接话（0.0 = 关掉）
    relation_address_gain: float = 0.15    # 被关系近的人点名 → 更想回应（0.0 = 关掉）
    #: 进离场通知的正/负阈值（§6.4）。缺省就是两个模块级常量——**只有一处口径**。
    relation_notice_threshold: int = RELATION_NOTICE_THRESHOLD
    relation_notice_low_threshold: int = RELATION_NOTICE_LOW_THRESHOLD
    # 进离场原因的**倾向**三项（§5.2："关系近、话多的人更会想说；关系近的人也会想问"）。
    # 与 §6.4 那两项同一套写法：都是无量纲增益，乘的是 [0,1] 的因子（w5 / w3 / 亲密度
    # 因子），故每项的上界就是它自己。三个数**按上界倒推**：三项之和 0.09，与 §6.4 两项
    # 之和 0.30 加起来是 0.39，**严格小于** `bidding.BidParams.interruption_threshold`
    # （0.4）——两眼一起拉满也推不动在位者优势（测试逐个对拍）。单项 ≤0.05 则够把站在
    # 开口阈值（0.1）边上的人往前推一点，推不动任何既定结果。
    reason_tell_gain: float = 0.05   # 当事人"想说"：× w5 × 我对听众里最近那位的亲密度
    reason_ask_gain: float = 0.03    # 关系近的人"想问"：× w3 × 我对当事人的亲密度
    reason_named_gain: float = 0.01  # 被原因点名的人再想问一点（同上，再加这一项）


@dataclass(frozen=True)
class UnsaidReason:
    """一件「在他心里有分量、还没说出口」的进离场原因（§5.2 那三项倾向的原料）。

    纯数据、由**引擎**每块算好喂进来（`Dynamics.set_unsaid_reasons`）——本模块不读盘、
    不认识"钩子/预约队列"，只知道"这个人手上有一件事、可以说给这些人听"：

      · `audience`：这件事可以说给谁听（**当下在场的其它人**的名字，不含他自己）。
        空 = 屋里没有别人 → "想说"这一项为 0（想说得有听的人）；
      · `named`：原因正文里**点到名**的在场者（"去找甲"里的甲）。点名只在"关系近"
        之上再加一点，不是一条独立通道（`bid()` 里与亲密度因子相乘）。

    缺省全空 = 不产生任何加成（`+ 0.0` 逐位精确）。
    """
    audience: tuple[str, ...] = ()
    named: tuple[str, ...] = ()


@dataclass
class CharDynamics:
    """一名参与者的数值动态状态（设计初值见字段）。除 pending_reply 与 self_urge 外
    全部 clamp 0..1。

    pending_reply 是**义务量**不是 0..1 分量：它从 pending_base 起、未答每轮翻倍，封顶
    pending_cap（64）——量级刻意压过其余分量，好让「被问到的人」必然把话筒拿回来。

    self_urge 是 LLM 自报冲动（think.urge）的留存，取值范围 -1..2（与 ThinkResult schema
    同）：负数=本人明确说「这句我不必说」，在 bid() 里按 urge_gain 加权后可为负贡献。
    """
    turns_since_spoke: int = 0   # 距上次开口气过的块数（沉默压力 ∝ 它）
    arousal: float = 0.0         # 情绪唤醒（由 think.aroused 混合）
    adjacency: float = 0.0       # 邻接触发（被点名/被提及时抬升，否则衰减）
    relevance: float = 0.4       # 话题相关性（提及时置 0.9，否则向 floor 衰减）
    goal_progress: float = 0.0   # 私有目标推进度（think.goal_progress 累积）
    goal_pressure: float = 0.3   # 目标压力（推进低→高；推进满→自然让出话筒）
    scene_pressure: float = 0.0  # 场景压力（harness 从虚拟钟临近硬边界设置）
    self_urge: float = 0.0       # 自报开口冲动（think.urge，-1..2；负数=不想说，压 bid）
    pending_reply: float = 0.0   # 被点名欠下的回应义务（未答每轮 ×pending_growth，作答清零）
    turns_pending: int = 0       # 已欠了几轮（被点名当轮为 0，每过一轮未答 +1）
    #: 本块**点名/提及我**的那个人的名字（空串 = 这一块没人点我）。它只活一块：下一块
    #: 没人点我时被清空——"更想回应"是一次回应，不是一次点名换来永久加成（`observe`）。
    last_addresser: str = ""


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def closeness_factor(closeness: int | None) -> float:
    """亲密度 → 0..1 的「想接话」因子（§6.4）。**没有关系表/没有这一行（None）→ 0.0。**

    线性：100 → 1.0、50 → 0.5、0 → 0.0。**负亲密度一律 0（不惩罚）**，理由三条：

      1. §6.4 的表格只写了一个方向（"关系近的人 → 更想接话 / 更想回应"）。负方向的
         "见到仇人就不敢开口"或"更想怼回去"是**另一条**行为通道，设计文档没写、用户没要、
         也没有任何测试覆盖它——凭空加一条，等于让一个 −100 悄悄改写谁拿话筒；
      2. 两条方向不同的通道要一起去调、一起解释，而它们的和是互相抵消的（−100 与 +100
         在场时净效应为零），调参的人会看不清到底哪一项在起作用；
      3. 方向上"只加不减"让这一项**不可能**把任何人的 bid 压到开口阈值以下——它只能
         把已经想说话的人往前推一点。这是"影响倾向、不决定结果"最省事的实现方式。

    上界因此是 [0, 1]：一个 ±100 的亲密度顶不出这个区间，`bid()` 里再乘增益与 w3 之后
    整项有界（有测试拿它跟仲裁的打断阈值对拍）。
    """
    if closeness is None:
        return 0.0
    return _clamp(float(closeness) / 100.0)


def entry_notice_text(closeness: int | None, entering: str,
                      params: DynamicsParams | None = None) -> str | None:
    """B 进场时给 A 的那一句通知（§6.4 第三行）；**不需要通知 → None**。

    三条边界（"高于/低于"都是严格不等：阈值本身不发）：

      · `closeness > relation_notice_threshold` → 「他来了」这件事在我心里有分量；
      · `closeness < relation_notice_low_threshold` → 我心里一沉；
      · 中间地带（含 0 与两个阈值本身）与 `None`（没有关系表/没有这一行）→ None。

    这是**确定性规则**：调用方（引擎）拿它去落一行只发给 A 的通知，不判、不问、不调模型。
    文本里点出进场者的名字——通知是给 A 看的，不说清是谁来了就等于没说。

    **这一行必须落成"只发给 A、且不带 address"的上下文行**（`observe` 的判据见那里）：
    它说的是"我心里动了一下"，不是"谁点名问我"。一旦它被标成 `address=A`，`observe` 的
    `addressed_to == listener` 当场为真，A 就被记下一笔 `pending_reply = pending_base` 的
    硬义务（未答每块翻倍、进 bid 不乘任何权重）——亲密度于是绕过 `bid()` 的加权项与仲裁
    阈值，从"影响倾向"变成"决定谁开口"。故本函数只产出**文本**，行怎么落（knows）由调用方
    按上面这条纪律决定，本模块不提供"地址"这个参数。
    """
    p = params if params is not None else DynamicsParams()
    name = str(entering or "").strip()
    if closeness is None or not name:
        return None
    if closeness > p.relation_notice_threshold:
        return f"（{name}来了。他在你心里有分量。）"
    if closeness < p.relation_notice_low_threshold:
        return f"（{name}来了。你心里一沉。）"
    return None


def _stable_noise(turn: int, name: str) -> float:
    """(turn, name) 的确定性微小噪声（0..0.001），只用于打破严格等值。"""
    h = zlib.crc32(f"{turn}:{name}".encode("utf-8"))
    return float(h % 1000) / 1_000_000.0


def _field(value, key, default=0.0):
    """兼容 ThinkResult/dict 与 Weights/dict 的属性/键读取。"""
    if isinstance(value, dict):
        return value.get(key, default)
    return float(getattr(value, key, default))


class Dynamics:
    """持有全体参与者的 CharDynamics、最近说话人 last_speaker 与可调参数。

    每块时序（引擎侧驱动）：
      · 块开始 round_start()  —— 递增内部轮号（bid 噪声据此确定性）；
      · 块内 deliberate 经 bid(name, weights) 取各人数值 bid 仲裁；
      · 块结束后把该块 think 结果逐听众 observe() 折进状态（被点名者在此记下欠答义务），
        再按产出说话人 mark_spoke(name)（答者义务清零、其余仍欠答者翻倍）或全静默
        tick_silence()（欠答者同样翻倍），并从虚拟钟 set_scene_pressure()。
    观察/竞价全部确定性、无 IO；情绪衰减用构造时注入的每角色衰减率。
    """

    def __init__(self, participants: Iterable[str],
                 params: DynamicsParams | None = None,
                 emotion_rates: dict[str, float] | None = None,
                 relations: Mapping[str, Mapping[str, int]] | None = None) -> None:
        self.states: dict[str, CharDynamics] = {n: CharDynamics()
                                                for n in participants}
        self.last_speaker: str | None = None
        self.params = params if params is not None else DynamicsParams()
        self.emotion_rates = dict(emotion_rates or {})
        self._round = 0
        #: 关系快照 `{我: {对方: 亲密度}}`（§6.4）。**只读、纯数据**：本模块不读盘、不缓存
        #: 文件，谁来喂（引擎每块刷新、测试直接给）都一样。缺省空 = 没有关系表 → 两项恒 0。
        self._relations: dict[str, dict[str, int]] = {}
        #: 「还没说出口的原因」快照 `{当事人: 一件事}`（§5.2）。同样只读、纯数据，由引擎
        #: 每块刷新（谁的窗口还开着由引擎决定）。缺省空 = 没有这件事 → 三项恒 0。
        self._unsaid: dict[str, UnsaidReason] = {}
        self.set_relations(relations or {})

    # ------------------------------------------------------------------ 关系 --
    def set_relations(self, relations: Mapping[str, Mapping[str, int]] | None) -> None:
        """换掉整份关系快照（引擎每块刷新；测试直接注入）。

        整体替换而不是增量：关系表能被工具改、能被撤回退回，增量维护意味着本类要懂那份
        账本——那不是动力学该知道的事。一张几十行的表重建一次的代价可以忽略，而"每块
        重建"保证了它永远与磁盘上那份一致。
        """
        self._relations = {str(person): {str(other): int(value)
                                         for other, value in row.items()}
                           for person, row in (relations or {}).items()}

    def closeness_of(self, person: str, other: str | None) -> int | None:
        """**person 对 other** 的亲密度（§6.4）；没有关系表/没有那一行/没给 other → None。

        None 与 0 是两件不同的事：None = "表里没这个人"，0 = "有这个人、关系一般"。
        因子（`closeness_factor`）把它们**都**算成 0，故 bid 上不区分；分开保留是为了让
        "他到底在不在那张表上"这件事在读数时仍看得见（调试与将来的其它用途）。
        """
        name = str(other or "")
        if not name:
            return None
        row = self._relations.get(str(person))
        if not row:
            return None
        return row.get(name)

    # ------------------------------------------------------------------ 没说出口的原因 --
    def set_unsaid_reasons(self, reasons: Mapping[str, UnsaidReason] | None) -> None:
        """换掉整份「还没说出口的原因」快照（§5.2；引擎每块刷新、测试直接注入）。

        整体替换而非增量——与 `set_relations` 同一条理由：谁的窗口还开着、说给谁听，是
        引擎那边的事；本模块只按当下这一份算倾向。`None` / 空表 = 没有这件事。
        """
        self._unsaid = {str(person): hint for person, hint in (reasons or {}).items()}

    def unsaid_reasons(self) -> dict[str, UnsaidReason]:
        """当前快照的**副本**（供日志/断言；改它不影响内部状态）。"""
        return dict(self._unsaid)

    # ------------------------------------------------------------------ 轮次推进
    def round_start(self) -> None:
        """块开始：推进内部轮号（bid 的噪声项据此按 (轮,名) 确定性取哈希）。"""
        self._round += 1

    def mark_spoke(self, name: str) -> None:
        """name 本块产出角色台词：其沉默计数归零并记为 last_speaker；**欠答义务清零**
        （他答了）。其它参与者各 +1（他们这轮没开口，沉默压力随之上涨），且其中**仍在
        欠答者**的义务再翻一倍（_escalate_pending）——没人答，压力就一轮比一轮大。"""
        if name not in self.states:
            raise KeyError(f"未知参与者: {name}")
        spoke_st = self.states[name]
        spoke_st.turns_since_spoke = 0
        spoke_st.pending_reply = 0.0          # 开口即兑现：欠答清零
        spoke_st.turns_pending = 0
        self.last_speaker = name
        for other, st in self.states.items():
            if other != name:
                st.turns_since_spoke += 1
                self._escalate_pending(st)

    def _escalate_pending(self, st: CharDynamics) -> None:
        """又过了一轮而 st 还没答：义务 ×pending_growth（封顶 pending_cap）、欠答轮数 +1。

        只在确有欠答（pending_reply > 0）时累进——无义务者始终为 0/0。
        """
        if st.pending_reply <= 0.0:
            return
        st.pending_reply = min(st.pending_reply * self.params.pending_growth,
                               self.params.pending_cap)
        st.turns_pending += 1

    def tick_silence(self) -> None:
        """本块无人产出角色台词：全员 turns_since_spoke +1；
        并依注入的每角色情绪衰减率让 arousal/adjacency 缓慢回落。
        欠答者同属「这轮仍没答」→ 义务照旧翻倍累进（与 mark_spoke 一致）。"""
        for name, st in self.states.items():
            st.turns_since_spoke += 1
            self._escalate_pending(st)
            rate = float(self.emotion_rates.get(name, 0.0) or 0.0)
            if rate > 0.0:
                st.arousal = _clamp(st.arousal * (1.0 - rate))
                st.adjacency = _clamp(st.adjacency * (1.0 - rate))

    # ------------------------------------------------------------------ 折状态
    def observe(self, listener: str, result, *,
                addressed_to: str | None = None,
                content: str | None = None,
                speaker: str | None = None) -> None:
        """把一次 think 结果折进 listener 的状态（本块 heard 消息的地址/正文辅助）。

        · arousal：向 think.aroused 依 arousal_blend 混合；
        · goal：think.goal_progress>0 时累积进 goal_progress，重算 goal_pressure
          = clamp(clamp((1-goal_progress·goal_k)·goal_scale,0,1)+goal_baseline,0,1)；
        · adjacency：本块听到的消息点名/提到 listener → +address_boost（cap 1），
          否则 ×adjacency_decay 向 0 衰减；兑现欠债（obligation_fulfilled 非空）
          ×obligation_adjacency_factor 降邻接；
        · relevance：被提及/点名 → 0.9，否则 max(floor, relevance·relevance_decay)；
        · pending_reply/turns_pending：被点名/被提及 = 欠一次回答 → 义务取
          max(现值, pending_base)（不因重新点名而变轻）、欠答轮数重起为 0（新一轮欠答）；
          之后每过一轮未答由 mark_spoke/tick_silence 翻倍累进，直到本人开口清零。
        · self_urge：自报开口冲动原样留存（clamp **-1..2**，与 ThinkResult schema 一致：
          负数合法且有意义 = 本人不想说），在 bid() 里按 urge_gain 加权计一项。
        result 为 ThinkResult 或等价 dict；除欠答义务（pending_reply，0..pending_cap）
        与自报冲动（self_urge，-1..2）外全部状态 clamp 0..1。

        `speaker` 是**我听到的那一句的说话人**（引擎给本块 heard 消息的 speaker；缺省 None
        = 说不出是谁说的）。它只喂 `last_addresser`——"被关系近的人点名"那一项（§6.4）要
        知道点我的人是谁。没点名时清空：那是一项**一次回应**的加成，不是永久加成。
        """
        st = self.states[listener]
        p = self.params

        # 自报冲动：原样留存（可为负 = 本人明确「这句我不必说」），范围与 schema 一致。
        st.self_urge = _clamp(_field(result, "urge", 0.0), -1.0, 2.0)

        st.arousal = _clamp(st.arousal * (1.0 - p.arousal_blend)
                            + _field(result, "aroused", 0.0) * p.arousal_blend)

        goal = _field(result, "goal_progress", 0.0)
        if goal > 0.0:
            st.goal_progress = _clamp(st.goal_progress + goal)
            pressure = _clamp((1.0 - st.goal_progress * p.goal_k) * p.goal_scale)
            st.goal_pressure = _clamp(pressure + p.goal_baseline)

        mentioned = bool(content and listener in content) or addressed_to == listener
        if mentioned:
            st.adjacency = _clamp(st.adjacency + p.address_boost)
            # 被点名 = 欠一次回答：义务立刻记一笔（取 max：重新点名不把已累积的
            # 压力打回原值），欠答轮数重起——这是一次「新的提问」。
            st.pending_reply = max(st.pending_reply, p.pending_base)
            st.turns_pending = 0
            # 点我的是谁（§6.4）：说出来的人越近，我越想回应（那一项在 bid()）。
            st.last_addresser = str(speaker or "")
        else:
            st.adjacency = st.adjacency * p.adjacency_decay
            st.last_addresser = ""      # 这一块没人点我 → 上一次那笔加成不留下

        if mentioned:
            st.relevance = 0.9
        else:
            st.relevance = max(p.relevance_floor, st.relevance * p.relevance_decay)

        fulfilled = _field(result, "obligation_fulfilled", None) or []
        if fulfilled:                       # 非空 = 兑现了一笔人情/欠债 → 邻接回落
            st.adjacency = st.adjacency * p.obligation_adjacency_factor

        st.arousal = _clamp(st.arousal)
        st.adjacency = _clamp(st.adjacency)
        st.relevance = _clamp(st.relevance)

    # ------------------------------------------------------------------ 竞价
    def bid(self, name: str, weights) -> float:
        """角色 name 本轮数值 bid（weights 为 CharacterCard.weights 或等价 dict）。

        bid = w1·relevance + w2·arousal + w3·adjacency + w4·goal_pressure
            − w6·inhibition_base + w7·scene_pressure
            + urge_gain·self_urge                            （自报冲动，-1~2，可为负=不想说）
            + pending_reply                                  （被点名欠答，未答每轮翻倍）
            + silence_gain·turns_since_spoke·(0.4+0.6·w5)   （w5 爱说性折叠进沉默压力）
            − recency_penalty（name==last_speaker）
            + w3·relation_adjacency_gain·closeness_factor(我对最后说话人的亲密度)  （§6.4）
            + w3·relation_address_gain·closeness_factor(我对点我那个人的亲密度)    （§6.4）
            + w5·reason_tell_gain·closeness_factor(我对听众里最近那位的亲密度)      （§5.2）
            + w3·(reason_ask_gain + reason_named_gain 若原因点了我名)
                 ·closeness_factor(我对当事人的亲密度)                            （§5.2）
            + 确定性噪声（(轮,name) 哈希）
        w5·0.0（talk base）按 §7.5 折叠进上面的沉默压力项，不在直项重复计。
        欠答项不乘任何 w：它是「被问到就必须答」的硬义务，性格权重不该把它抵消掉。
        self_urge 项（urge_gain·self_urge）是**可调**权重的一项：默认 0.6——自报不想说
        （urge 为负）时确实把自己的 bid 压低，但封顶只有 2.0（最大 ±1.2），故不能凭一个
        数压过欠答义务或长期沉默压力；urge_gain=0.0 即关掉本项（回到只看状态的旧行为）。

        **亲密度那两项（《人际关系与场景推进》§6.4）**：它们与既有项同源、同风格——
        乘 w3（§6.4 把两行都记在 `adjacency（w3）` 那一档）、各有一个具名增益
        （`DynamicsParams.relation_adjacency_gain` / `relation_address_gain`，设 0.0 即
        关掉）、原料是 `closeness_factor` 夹出来的 0..1（故**量纲**是"满格亲密度 = 一倍
        增益"，与 ±100 的原始量纲无关；不直接拿 closeness 当权重，是因为它一次能把 bid
        顶穿几十上百，仲裁的阈值与在位者优势当场失去意义）。
        两项的上界 = (两个增益) × w3 ≤ 两个增益之和，而它**小于**
        `bidding.BidParams.interruption_threshold`（有测试对拍）——亲密度因此只能把一个人
        往前推一点，推不动在位者优势，也压不低任何人（负值在因子里就是 0）。
        **没有关系表 / 引擎没喂快照时两项恒为 0**：`+ 0.0` 是逐位精确的，故无关系角色的
        bid 与引进关系之前**同一个浮点值**（铁律 3）。

        **进离场原因的三项（§5.2）**：`UnsaidReason` 快照里写着"谁手上有一件还没说出口的
        事、可以说给谁听、点了谁的名"。当事人拿"想说"（× w5：话越多越藏不住、× 他对听众
        里最近那位的亲密度：屋里得有他想说的人）；他关系近的在场者拿"想问"（× w3，与
        §6.4 的接话/回应同一档）；被原因点名的人再拿一点（同样 × w3 × 亲密度因子——点名
        不是一条独立通道，它只在"关系近"之上再加一点）。多个当事人时**取最大**不叠加
        （叠加会随在场人数无界增长，取最大 = "最压我的那件事"）。
        三项之和 0.09 与 §6.4 两项之和 0.30 加起来是 0.39，**小于**打断阈值（有测试对拍）。
        **没有这件事 / 引擎没喂快照 / 三个增益关掉时三项恒为 0**（`+ 0.0` 逐位精确）。
        """
        st = self.states[name]
        p = self.params

        def _w(key: str) -> float:
            return _field(weights, key, 0.5)

        base = (_w("w1_relevance") * st.relevance
                + _w("w2_arousal") * st.arousal
                + _w("w3_adjacency") * st.adjacency
                + _w("w4_goal_pressure") * st.goal_pressure
                - _w("w6_inhibition") * p.inhibition_base
                + _w("w7_scene_pressure") * st.scene_pressure)
        silence = (p.silence_gain * st.turns_since_spoke
                   * (0.4 + 0.6 * _w("w5_talkativeness")))
        urge = p.urge_gain * st.self_urge        # 自报冲动（可为负 → 压低发言权）
        pending = st.pending_reply               # 欠答义务（未答每轮翻倍，答后清零）
        recency = -p.recency_penalty if self.last_speaker == name else 0.0
        # 亲密度两项（§6.4）：乘 w3（与邻接同档）、各乘一个具名增益、因子夹在 0..1。
        # 没有关系表时因子恒 0 → 这两项就是 + 0.0（逐位精确，无关系角色分毫不变）。
        relation = (_w("w3_adjacency") * (
            p.relation_adjacency_gain
            * closeness_factor(self.closeness_of(name, self.last_speaker))
            + p.relation_address_gain
            * closeness_factor(self.closeness_of(name, st.last_addresser))))
        reason = self._reason_terms(name, _w, p)
        noise = _stable_noise(self._round, name)
        return base + urge + silence + pending + recency + noise + relation + reason

    def _reason_terms(self, name: str, _w: Callable[[str], float],
                      p: DynamicsParams) -> float:
        """§5.2 的三项（当事人想说 / 关系近者想问 / 被点名者再想问一点），见 `bid()`。"""
        if not self._unsaid:
            return 0.0
        return (self._want_to_tell(name, _w, p) + self._want_to_ask(name, _w, p))

    def _want_to_tell(self, name: str, _w: Callable[[str], float],
                      p: DynamicsParams) -> float:
        """当事人「想说」：夹到 0..1 的 w5 × 增益 × 他对**听众里最近那位**的亲密度。

        没有听众 / 听众里没有他关系近的人 → 0。取听众里的最大值 = "屋里有没有一个我说得
        出口的人"，不是逐个听众各计一项（那会随在场人数无界增长）。
        """
        hint = self._unsaid.get(name)
        if hint is None or not hint.audience:
            return 0.0
        closest = max(closeness_factor(self.closeness_of(name, other))
                      for other in hint.audience)
        if closest <= 0.0:
            return 0.0
        return (p.reason_tell_gain * _clamp(_w("w5_talkativeness")) * closest)

    def _want_to_ask(self, name: str, _w: Callable[[str], float],
                     p: DynamicsParams) -> float:
        """旁观者「想问」：夹到 0..1 的 w3 × (增益 + 被点名的那一点) × 我对当事人的亲密度。

        多个当事人时取**最大**：叠加会随在场人数无界增长（那就越过阈值、变成"决定结果"）。
        亲密度因子为 0（不认识/关系差/没有关系表）时整项为 0——§5.2 只说"关系近的人会
        想问"，点名也不是一条独立通道（它乘在同一个因子上）。
        """
        best = 0.0
        for holder, hint in self._unsaid.items():
            if holder == name or name not in hint.audience:
                continue
            factor = closeness_factor(self.closeness_of(name, holder))
            if factor <= 0.0:
                continue
            gain = p.reason_ask_gain + (p.reason_named_gain if name in hint.named else 0.0)
            best = max(best, factor * gain)
        if best <= 0.0:
            return 0.0
        return _clamp(_w("w3_adjacency")) * best

    # ------------------------------------------------------------------ 场景压力
    def set_scene_pressure(self, progress01: float) -> None:
        """harness 依据虚拟钟到硬边界的位置设置全员 scene_pressure = clamp(p,0,1)。"""
        pv = _clamp(progress01)
        for st in self.states.values():
            st.scene_pressure = pv

    # ------------------------------------------------------------------ 展示
    def snapshot(self) -> dict[str, dict]:
        """返回 {name: 该角色全部动态字段} 深拷贝，供 GUI/日志/断言。"""
        return {n: {
            "turns_since_spoke": s.turns_since_spoke,
            "arousal": s.arousal,
            "adjacency": s.adjacency,
            "relevance": s.relevance,
            "goal_progress": s.goal_progress,
            "goal_pressure": s.goal_pressure,
            "scene_pressure": s.scene_pressure,
            "self_urge": s.self_urge,
            "pending_reply": s.pending_reply,
            "turns_pending": s.turns_pending,
        } for n, s in self.states.items()}
