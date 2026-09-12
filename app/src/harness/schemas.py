"""三张核心 schema + think 步锁死输出（设计文档 §4/§5/§6/§8.3）。

场景模型见《场景编排与桌面外壳_设计文档》§3：场景带日期/背景/描述/剧情/hooks，
在场角色是**带入场记录的演员表**（characters），且**可以一个人都没有**（§3.3）。
对话圈（circles）已整体删除（§3.2）：在场轴 in_scene 即场景本身，一条消息要么在
本场景内、要么不在；认知轴 knows 仍逐条限制「这条谁能看」。
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from .hooks import Hook

SpeakerType = Literal["character", "director", "narrator", "human", "system"]


class Message(BaseModel):
    """消息：说话者 + 在场轴(in_scene) + 认知轴(knows)，两条正交轴。"""
    id: int
    speaker: str
    speaker_type: SpeakerType = "character"
    content: str
    in_scene: str
    knows: Optional[list[str]] = None   # None = 全员可见（在场内）
    reply_to: Optional[int] = None
    address: Optional[str] = None       # 点名/邻接对
    turn: int = 0

    def is_visible_to(self, character: str) -> bool:
        """认知轴判定：自己是说话者当然可见；否则 sees self 或 knows=None。"""
        if self.speaker == character:
            return True
        return self.knows is None or character in self.knows


class Weights(BaseModel):
    """心理权重 w1~w7（设计文档 §5）。0~1，默认 0.5 保守起步。"""
    w1_relevance: float = 0.5
    w2_arousal: float = 0.5
    w3_adjacency: float = 0.5
    w4_goal_pressure: float = 0.5
    w5_talkativeness: float = 0.5
    w6_inhibition: float = 0.5
    w7_scene_pressure: float = 0.5


class Corpus(BaseModel):
    """人物语料：把原作的口吻/思维方式喂给模型（few-shot 风格锚点，不是内容素材）。

    仅用于模仿"怎么说话、怎么想"，样例本身绝不可照抄——渲染时带硬性告诫。
    全字段可选：缺省即空语料，渲染出空串（系统提示逐字节不变）。
    """
    source: str = ""          # 出处/作品名（可选）
    style: str = ""           # 语言风格描述
    thinking: str = ""        # 思维方式描述
    quirks: list[str] = Field(default_factory=list)    # 口头禅/习惯用语
    samples: list[str] = Field(default_factory=list)   # 代表性台词样例（few-shot）


class CharacterCard(BaseModel):
    """角色卡：设定/语料/权重，外加**信息库种子**（§9.1）。

    这里没有边界声明那一栏了——"他知道什么"改由信息库承担（索引表恒在提示词里）。
    老卡上的 `knowledge_boundary` 在**读卡时**逐条搬进 `knowledge_seed`（见
    `_migrate_legacy`），真正的入库由 `knowledgestore.seed_from_card` 在角色库**首次
    创建**时做（§9.1 两步走：校验器只搬数据，绝不做 IO）。
    """
    name: str
    personality: dict[str, str] = Field(default_factory=dict)
    abilities: list[str] = Field(default_factory=list)
    relationships: dict[str, str] = Field(default_factory=dict)
    #: 信息库种子：老卡的 `knowledge_boundary` 迁进来的每一行（含那句兜底文案——它在
    #: **播种**时才被丢掉，判据是 `knowledgestore.KNOWLEDGE_FALLBACK`）。库建好之后它
    #: 一直留在卡上也无害：`seed_from_card` 见库已存在即整段跳过（幂等）。
    knowledge_seed: list[str] = Field(default_factory=list)
    weights: Weights = Field(default_factory=Weights)
    emotion_decay_rate: float = 0.4
    #: 原作物语料（风格/思维/口头禅/样例）；旧卡无此键 → 空 Corpus，装载行为不变。
    corpus: Corpus = Field(default_factory=Corpus)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy(cls, data: Any) -> Any:
        """旧卡读时迁移（§9.1 第一步）：`knowledge_boundary` → `knowledge_seed`。**只搬数据。**

        规矩（与 `Scene._migrate_legacy` 同一套）：

          · 逐条保持顺序——用户写的每一条边界都必须活着进信息库，这是本次改造的最高约束；
          · 两个键都在（半迁移的卡）→ 以新键为准，老键淘汰；
          · 老键缺席或为空 → 空列表（"没有库"是合法状态，§9.2）。

        这里**绝不做 IO**：pydantic 的 `model_validator` 是纯函数，在里面建库文件会让
        "读一张卡"变成有副作用的操作（列表扫描、编辑器预览、测试夹具全都会意外写出文件）。
        真正播种是 `knowledgestore.seed_from_card` 那次显式、可测、有日志的动作。
        """
        if not isinstance(data, dict):
            return data
        data = dict(data)
        legacy = data.pop("knowledge_boundary", None)
        if "knowledge_seed" not in data:
            if isinstance(legacy, (list, tuple)):
                data["knowledge_seed"] = [str(item) for item in legacy]
            elif isinstance(legacy, str) and legacy.strip():
                data["knowledge_seed"] = [legacy]     # 手写成一行文本也认（当一条）
            else:
                data["knowledge_seed"] = []
        return data

    @property
    def knowledge_boundary(self) -> list[str]:
        """退役字段 `knowledge_boundary` 的**只读派生**别名（与 `Scene.participants` 同一套
        退役写法）：数据只有一个真相源（`knowledge_seed`），仍读老名字的下游（模板导入的
        "未填字段"统计）照常工作，但**写**不进来——退役的字段不该还有第二条入口。
        """
        return self.knowledge_seed


class HardBoundary(BaseModel):
    """场景硬边界；type="none" 与「没有边界」等价（引擎不设任何钟边界，§3.1）。"""
    type: Literal["time", "physical", "social", "goal", "none"]
    value: str = ""
    desc: str = ""


class SceneCastMember(BaseModel):
    """场景演员表的一行：角色名 + 入场记录（§3.3 可插拔角色）。

    entered_at / entered_round 记「他是何时（场景内时刻 / 世界块号）进场的」，供
    S4 的进场可见性（进场者看不到此前对话）与左栏「谁在何时进出」使用；本阶段只
    承载数据，不做任何过滤。
    """
    name: str
    entered_at: str = ""
    entered_round: int = 0


class Scene(BaseModel):
    """场景：导演系统的一等实体（设计文档 §3）。

    字段分两类——
      · 场景自身的信息：name/date/background/description/description_mutable/
        plot_direction/hooks/hard_boundary/start_time；
      · 演员表 characters：**带入场记录的在场角色**。participants 是它的只读派生
        便捷属性（名单顺序即 characters 顺序），下游读 scene.participants 的代码不受影响。
    场景**可以没有角色**（§3.3）：characters 为空是合法状态，构造/开场都不报错。
    """
    name: str
    date: str = ""               # 可选的年月日（自由文本或 ISO，空=界面不显示）
    background: str = ""         # 背景信息（世界观/设定）
    description: str = ""        # 场景描述（如"餐厅里有一些桌椅"）
    description_mutable: bool = False   # 「可改变的」：勾选后场景可申请改写描述
    plot_direction: str = ""     # 剧情设定：用户期望的走向
    #: 触发器列表（§4）：hooks.Hook 是纯 stdlib dataclass，逐条严格校验（用户内容，
    #: 不合法就当场报错，绝不静默丢弃）。
    hooks: list[Hook] = Field(default_factory=list)
    characters: list[SceneCastMember] = Field(default_factory=list)
    hard_boundary: Optional[HardBoundary] = None
    start_time: str = "21:30"   # 场景虚拟时钟起点 "HH:MM"（无则 21:30 开场）

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy(cls, data: Any) -> Any:
        """旧场景文件兼容（§3.2/§3.3 迁移）：

        · 旧文件里的 `participants`（纯名字表）→ 迁移进 characters（保持原顺序，
          entered_round=0）；两者都有时 characters 说了算（新字段优先）。
        · `circles`（已删除的对话圈）→ 直接丢弃，不再参与任何语义。
        · characters 里写成裸字符串的名字 → 视同 {"name": ...}（手写场景 JSON 友好）。
        """
        if not isinstance(data, dict):
            return data
        data = dict(data)
        data.pop("circles", None)                 # 对话圈已删除：旧键静默淘汰
        raw = data.get("characters")
        if not raw:
            legacy = data.get("participants") or []
            if legacy:
                data["characters"] = list(legacy)
        data.pop("participants", None)            # participants 现在是派生属性，非字段
        chars = data.get("characters")
        if isinstance(chars, list):
            data["characters"] = [{"name": c} if isinstance(c, str) else c
                                  for c in chars]
        return data

    @property
    def participants(self) -> list[str]:
        """在场角色名（只读派生；顺序 = characters 顺序）。"""
        return [m.name for m in self.characters]


class ThinkResult(BaseModel):
    """think 步锁死输出（设计文档 §8.3）：urge 本人自评的开口冲动（-1~2，可为负=不想说），
    由 Dynamics 存为 self_urge 并按 urge_gain 加权计入数值 bid（不再是唯一决定权）。"""
    aroused: float = Field(ge=0.0, le=1.0)
    obligation_fulfilled: list[str] = Field(default_factory=list)
    goal_progress: float = Field(default=0.0, ge=0.0, le=1.0)
    addressed: Optional[str] = None
    impression_of_speaker: Optional[str] = None
    urge: float = Field(ge=-1.0, le=2.0)
