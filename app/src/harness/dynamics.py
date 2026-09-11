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
"""
from __future__ import annotations

import zlib
from dataclasses import dataclass
from typing import Iterable

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


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


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
                 emotion_rates: dict[str, float] | None = None) -> None:
        self.states: dict[str, CharDynamics] = {n: CharDynamics()
                                                for n in participants}
        self.last_speaker: str | None = None
        self.params = params if params is not None else DynamicsParams()
        self.emotion_rates = dict(emotion_rates or {})
        self._round = 0

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
                content: str | None = None) -> None:
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
        else:
            st.adjacency = st.adjacency * p.adjacency_decay

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
            + 确定性噪声（(轮,name) 哈希）
        w5·0.0（talk base）按 §7.5 折叠进上面的沉默压力项，不在直项重复计。
        欠答项不乘任何 w：它是「被问到就必须答」的硬义务，性格权重不该把它抵消掉。
        self_urge 项（urge_gain·self_urge）是**可调**权重的一项：默认 0.6——自报不想说
        （urge 为负）时确实把自己的 bid 压低，但封顶只有 2.0（最大 ±1.2），故不能凭一个
        数压过欠答义务或长期沉默压力；urge_gain=0.0 即关掉本项（回到只看状态的旧行为）。
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
        noise = _stable_noise(self._round, name)
        return base + urge + silence + pending + recency + noise

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
