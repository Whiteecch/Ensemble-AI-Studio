"""高层引擎：导演三件事(开场/注入/收束) + human 操作 + 逐块 step + 事件流。

消费技术方案 §9.2 事件契约；CLI(Task 13) 只消费 events。

Checkpointer（评审修复轮 1）：graph 全走 async API（ainvoke/aget_state/aupdate_state），
持久化必须用 AsyncSqliteSaver（langgraph-checkpoint-sqlite 3.1.1 的 sync SqliteSaver 对
async 直接 NotImplementedError）。AsyncSqliteSaver 构造需要 running loop（self.loop =
asyncio.get_running_loop()），故构图改为惰性：__init__ 只存配置，不触碰 checkpointer；
首个公共 async 方法经 `await self._ensure_graph()` 在 loop 内建 saver + 编译 graph 并缓存。
判定：run_root 目录已存在 → AsyncSqliteSaver（真实 sqlite 落地），否则 → MemorySaver
（大多数无 run_root 的测试走内存）。AsyncSqliteSaver 绑定其创建时所在的 loop，因此一个
SceneEngine 的全部 async 操作须在同一个 event loop 内执行（典型：CLI 单 loop / 测试单
asyncio.run / pytest-asyncio 单测）。`closed` 是同步属性（brief 契约），不 route 到 graph
的 sync get_state（AsyncSqliteSaver 同 loop 下 raise InvalidStateError、跨已关 loop 会
死锁），改由每个 async 状态读取更新引擎内镜像 `self._closed`。

可插拔演员表（《场景编排与桌面外壳》§3.3 +《界面与场景自由度》§3.1，S4a/T1）：
**场景持有阵容**——scene.characters 是这一场的**唯一权威**，打开场景即用它；运行期的
增删（add/remove/禁言/钩子的角色事件/延时动作）**立即生效**，由本引擎持有权威的在场
名单与禁言状态，并在保存时写回场景文件（`save_scene_file`）与 sidecar（`save_scene_state`）
——重开一场因此回到**上次存下的阵容**，而不是场景文件的原始名单。两种写回的差别：
sidecar 存运行快照（转录/钟/人事含已移出者），场景文件只存**配置 + 当前在場者**。
移出只把人标成 inactive、绝不擦除记录（其历史与私有记忆原样保留）。

**按需装卡**（§3.2）：引擎持有角色库目录（`characters_dir`），构造期与运行期都能按名
从库里装载角色卡——任何库中角色都能在**任何时候**加入任何场景；库里没有才报错。
`cast_from_cards` 因此退化为**播种**语义：只在场景没存过阵容时才用所选卡填上。

每个角色另有**进场基线** entry_round（转录 turn 制，见 `_entry_round_of`）：进场者看不到
进场前的一切对话（公共信息由 S4b 的提示词场景段供给）；离场者历史保留但不再 think、
不再竞价、不再出现在 GUI 左右栏。

S4b（本文档 §3.1/§3.4/§4/§5）把场景的**剩余职责**全部接上：
  · 场景字段进所有提示词：background/description/plot_direction 进 think/speak/narrate
    （公共信息，进场者也有份），语言指令（§7）另由引擎按 self.language 注入三处；
  · 钩子判定归场景自己（§4.1：它本来就每块要综观上下文，不算额外一次 LLM 调用）：
    场景启用钩子时**每块**都过一次叙述提示词（附钩子清单），场景用 `[[HOOK:<id>]]`
    一行一条地报告哪些钩子成立，引擎剥掉这些指令行、逐条执行、块内去重；
  · 场景自改（tools，§4.1③）：`[[TOOL:...]]` 改描述/背景/场景名（**绝不含文件名**），
    因为改完这一轮它还没输出思考，引擎会把改动后的新描述再喂回去**多思考一次**
    （上限一轮，见 `_narrate`）；
  · 保存/续演/自动保存/重置（§5）：sidecar 存档 = 转录 + 虚拟钟 + 块数 + 在场/禁言。

第四批（《界面与场景自由度》§6）：
  · **撤回是截断式的**（§6.1）：retract(id) 撤该条**及其之后同一场景内的所有消息**，
    并把「那段下文留下的痕迹」一并清掉（think 只读日志按轮次裁、私有记忆按轮次截断）；
    世界钟与叙述冷却**不倒拨**（见 retract 的决策记录）。
  · **推进频率可调**（§6.2）：咨询场景（判钩子）与落一条叙述是两件事——有钩子的场景
    照旧每块咨询，但只有场景真给了正文才落行；无钩子场景按判据过线才开口，触发线与
    冷却由活跃度（`set_narrate_activity`，缺省 0.5 = 恒等）统一缩放，并作为语气提示
    写进场景提示词（scenarist.effective_params / activity_hint）。
见各方法 docstring。语言与场景路径等运行期参数都由 worker/GUI 注入。
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import aiosqlite
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import START
from langgraph.store.memory import InMemoryStore

from . import bidding as bidding_mod
from . import graph as graph_mod
from . import events as ev
from . import hooks as hooks_mod
from . import i18n as i18n_mod
from . import memory as memory_mod
from . import sceneclock as sc
from . import scenarist as scenarist_mod
from . import scenestore as scenestore_mod
from .backends import get_backend, make_deepseek
from .dynamics import CharDynamics, Dynamics, DynamicsParams
from .loaders import (list_character_paths, load_bid_params, load_character_card,
                      load_models, load_scene, save_scene, scene_with_cast)
from .prompters import TIME_CONDITION_RULE, build_narrate_messages
from .schemas import CharacterCard, HardBoundary, Message, Scene, SceneCastMember
from .visibility import render_view

logger = logging.getLogger(__name__)


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _clip(text: str, limit: int) -> str:
    """裁到 limit 字（超长补省略号）——播报/日志行专用，绝不用来裁叙述正文。"""
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


#: 叙述行裁剪上限：一到两行、约 120 字（规格硬约束——场景只推进一步，不许长篇铺陈）。
_NARRATION_MAX_LINES = 2
_NARRATION_MAX_CHARS = 120

#: 延时角色动作的合法动作集（§4.1② 角色事件；与 hooks.CharacterAction 一致）。
_CAST_ACTIONS: tuple[str, ...] = ("add", "remove", "mute_turns", "mute", "unmute")

# ---------------------------------------------------------------------------
# 钩子指令协议（§4.1①：条件由场景每轮自行判定）
# ---------------------------------------------------------------------------
# 与 scenarist 的 [[TOOL:...]] 同一约定——模型只会吐纯文本，于是把「哪条钩子成立」
# 编码成叙述里的一行：`[[HOOK:<id>]]`，一行一条、从标记吃到行尾。解析**本地**实现
# （scenarist 只管场景工具指令，钩子是引擎的事），畸形片段照 tools 的纪律处理：
# 剥离、记进诊断、绝不抛异常、绝不把 `[[HOOK` 泄进用户看到的叙述。
_HOOK_OPEN = "[[HOOK:"
_HOOK_MARK = "[[HOOK"          # 去掉冒号的本体：`[[HOOK]]` 这类写坏也要认得出来
_HOOK_CLOSE = "]]"
#: 合法钩子 id：字母/数字/下划线/点/连字符（场景 id 是用户起的名，宽松但拒绝空白与标记字符）。
_HOOK_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

#: 场景事件（§4.1③）允许改的**场景自身字段**。刻意是白名单而非黑名单：文件名/路径是
#: 磁盘身份（归 GUI/装载器），任何情况下都不许被钩子或工具改写——不在表里的键一律忽略。
_SCENE_HOOK_KEYS: tuple[str, ...] = ("name", "description", "background",
                                     "plot_direction", "date")
_SCENE_HOOK_LABELS: dict[str, str] = {
    "name": "场景名", "description": "场景描述", "background": "场景背景",
    "plot_direction": "剧情走向", "date": "日期"}

#: 「配置场景」（§3.4）**可热更新**的场景字段：保存后立刻应用到运行中的活场景。
#: 与钩子白名单同一套字段（外加 description_mutable 这个开关与 start_time/hard_boundary
#: 两个时间字段，见 apply_scene_config）——文件名/路径仍然永远不在其中。
_SCENE_HOT_FIELDS: tuple[str, ...] = ("name", "date", "background", "description",
                                      "description_mutable", "plot_direction")

#: 追加在 hooks_prompt_block 之后的中文指令（钩子判定的**输出契约**）。写在引擎里而
#: 不改 hooks.py：hooks 是纯数据/渲染模块，`[[HOOK:]]` 是引擎这一层的执行协议。
_HOOK_INSTRUCTION = (
    "【怎么报告】逐条对照上面的条件与当前局势，条件成立的钩子按它的效果执行；"
    f"每成立一条，就在叙述里**另起一行**写一条指令（行首尾不要有别的字，一行一条）：\n"
    f"{_HOOK_OPEN}h1{_HOOK_CLOSE}\n"
    "上面的 h1 换成该钩子的 id（方括号里那个）。没有钩子成立就一条都不写。\n"
    "**判钩子不等于要叙述**：引擎每块都会请你看一眼钩子，但只有你写出的正文才会成为"
    "一条叙述——需要推进局势时才写（一到两行，规则同上）；此刻无需推进，就只写指令行、"
    "或什么都不写，引擎不会因此报错，也绝不替你补一条叙述。"
    "指令行整行删掉后，剩下的叙述必须依然完整。")


def _as_hard_boundary(value: Any) -> HardBoundary | None:
    """外部传入的硬边界 → HardBoundary | None。

    容忍三种形态（GUI 的配置弹窗按自己的方便给）：None / HardBoundary / 可校验成
    HardBoundary 的 dict。形态非法 → pydantic 的 ValidationError 照常抛出（界面会把它
    显示成一行中文失败提示，绝不静默改坏场景）。
    """
    if value is None or isinstance(value, HardBoundary):
        return value
    return HardBoundary.model_validate(value)


def _trim_narration(raw: str | None) -> str:
    """后端产出 → 叙述正文：只留前 2 行**非空**行，再裁到 ~120 字。

    模型偶发写多段/空行/前置说明时，兜底比提示词更可靠——场景多写一个字都是跑题。
    """
    if not raw:
        return ""
    lines = [ln.strip() for ln in str(raw).splitlines() if ln.strip()]
    return "\n".join(lines[:_NARRATION_MAX_LINES])[:_NARRATION_MAX_CHARS].strip()


def _pick_backend(cfg, api_key, base_url: str | None = None):
    """按 yaml 的 backend 档取后端；base_url 非空才覆盖默认端点（§2.1① 设置里的 url）。

    stub 档不看 api_key 也不看 base_url（离线占位，绝不因为设置里填过 url 就联网）。
    """
    if cfg.backend == "stub":
        return get_backend("stub")
    if cfg.backend == "deepseek":
        if not api_key:
            # I4(最终评审)：显式抛错替代 assert（python -O 下 assert 会被剥掉）。
            raise ValueError("deepseek 后端需要 api_key（DEEPSEEK_API_KEY）")
        return make_deepseek(api_key, cfg, base_url=base_url)
    raise ValueError(cfg.backend)


class SceneEngine:
    def __init__(self, scene_path: Path, card_paths: list[Path], models_path: Path,
                 run_root: Path, bid_path: Path | None = None, api_key: str | None = None,
                 closing_at_block: int | None = None, thread_id: str = "scene1",
                 demo_alternate: bool = False,
                 start_time: str | None = None,
                 dynamics_params: DynamicsParams | dict | None = None,
                 cast_from_cards: bool = False,
                 auto_narrate: bool = True,
                 nudge_params: scenarist_mod.SceneNudgeParams | dict | None = None,
                 narrate_activity: float = 0.5,
                 language: str = i18n_mod.DEFAULT_LANGUAGE,
                 scene_file: Path | None = None,
                 autosave_every: int = 0,
                 api_base_url: str | None = None,
                 model_override: str | None = None,
                 characters_dir: Path | None = None):
        self.scene: Scene = load_scene(scene_path)
        # 场景文件路径（sidecar 存档的落点，§5）。缺省就用装载它的那个路径——worker/GUI
        # 传的就是「正在跑的这个场景文件」；显式给 scene_file 可按需另指（名字与装载路径
        # 不同名也支持）。**只作存档坐标**：钩子/工具永远改不到它（见 _SCENE_HOOK_KEYS）。
        self._scene_path: Path | None = (Path(scene_file) if scene_file is not None
                                         else Path(scene_path))
        # 语言（§7）：指令注入所有 LLM 提示词（think/speak/narrate 三处，见 graph/引擎）。
        self.language: str = (language if language in i18n_mod.LANGUAGES
                              else i18n_mod.DEFAULT_LANGUAGE)
        self._language_directive: str = i18n_mod.llm_language_directive(self.language)
        self.cards: dict[str, CharacterCard] = {}
        cast_order: list[str] = []       # 装载顺序 = card_paths 顺序（演员表原序）
        for p in card_paths:
            card = load_character_card(p)
            if card.name in self.cards:
                raise ValueError(f"角色卡重名: {card.name}（{p} 与已有卡冲突）")
            self.cards[card.name] = card
            cast_order.append(card.name)
        # 角色库目录（§3.2「按需装卡」）：任何库中角色都能在**任何时候**加入任何场景。
        # 两个消费点：① 构造期给场景阵容里缺卡的人补卡；② 运行期 add_character 现装卡。
        # None = 没有库可用（纯内存/离线引擎）→ 只能调度已装载的卡，报错文案照旧中文。
        self._characters_dir: Path | None = (
            Path(characters_dir) if characters_dir is not None else None)
        # cast_from_cards（口径已改，§2 根因 1）：**场景持有阵容**——scene.characters 是
        # 唯一权威，打开场景即用它。此开关退化为「播种」：**只在场景没存过阵容**（空
        # characters）时才用所选角色卡把它填上（新建场景/老场景文件没写名单）；场景存过
        # 名单时一律以文件为准，绝不覆盖——否则每次打开都会把上次存下的人事抹掉。
        if cast_from_cards and not self.scene.characters:
            self.scene = scene_with_cast(self.scene, cast_order)
        # 场景名单里的人缺卡 → 从角色库按需补载（否则这一场根本打不开；库里也没有 →
        # 下面 _validate_cast 照旧 fail fast）。
        self._load_missing_cast_cards()
        # C1(最终评审)：**不可变的在场轴（空间键）**，构造时一次捕获。scene.name 是
        # 「显示名」，会被 [[TOOL:set_name]] 或钩子 scene_patch 改掉；若把它同时当空间
        # 键，改名当刻全部历史（in_scene=旧名）就被 view_for 判成「不在本空间」→ 人人
        # 视图清空。故落行与视图一律认这个 id，改名只改显示名。旧存档/旧消息的可能处理
        # 见 _adopt_legacy_space（首次续演时接受历史里已有的那个键）。
        self._scene_space: str = self.scene.name
        self._validate_cast()            # I4：任何构图/建库前 fail fast（含 -O 下仍生效）
        # 数值动态（§7.4）：引擎持有一份 per-character Dynamics，随每块 think/产出演化；
        # 发言仲裁经闭包 bidder 取 bid()（见 _ensure_graph）：读的是数值 bid，而自报 urge
        # 经每块 observe() 存进 self_urge、按 urge_gain 加权后成为 bid 里的一项（非唯一项）。
        dp = dynamics_params if isinstance(dynamics_params, DynamicsParams) \
            else DynamicsParams(**(dynamics_params or {}))
        self.dynamics_params = dp
        self._dynamics = Dynamics(
            self.scene.participants,
            params=dp,
            emotion_rates={name: self.cards[name].emotion_decay_rate
                           for name in self.scene.participants})
        self._clock_now: int | None = None   # 虚拟钟当前秒（worker 注入；None=开场时刻）
        models = load_models(models_path)
        # 「模型 api 配置」（§2.1①，S6）：**非空才生效**——空串/None 完全等于今日行为。
        #   · model_override 覆盖 think/speak/narrate **三档的角色模型名**（每档的
        #     params —— thinking/timeout/max_tokens 等 —— 照旧从 yaml 来）；
        #   · api_base_url 只作用于真实后端（stub 不看它，故离线档不会因此多联网）。
        override = str(model_override or "").strip()
        if override:
            models = {role: replace(mcfg, model=override)
                      for role, mcfg in models.items()}
        base_url = str(api_base_url or "").strip() or None
        self.think_backend = _pick_backend(models["think"], api_key, base_url)
        self.speak_backend = _pick_backend(models["speak"], api_key, base_url)
        # 场景/叙述者的后端：models.yaml 的 narrate 档；旧配置没这一档就退用 speak 档
        # （老配置一字不改照跑，narrate 只是可选的第二档）。
        self.narrate_backend = (_pick_backend(models["narrate"], api_key, base_url)
                                if "narrate" in models else self.speak_backend)
        self._bid = load_bid_params(bid_path) if bid_path else None
        self.closing_at_block = closing_at_block
        self.thread_id = thread_id
        self.run_root = Path(run_root)
        self.demo_alternate = demo_alternate    # CLI 流式演示的轮流开口叠加

        # 场景时间的权威数据只作解析/暴露（worker/GUI 读取，图不消费）：起点来自显式
        # start_time 覆盖，否则取 scene.start_time（缺省 21:30）；打烊边界取 scene.
        # hard_boundary(type=time)。真实流速虚拟钟由桌面 worker 按 开场时刻+流逝×流速
        # 连续计算并决定到点收束，引擎不推进任何钟键、不给消息打时刻戳。
        self.start_time = start_time if start_time is not None else self.scene.start_time
        self.start_seconds = sc.parse_hhmm(self.start_time)     # 非法值开场前 fail fast（秒）
        self.boundary_value: str | None = None
        self.boundary_seconds: int | None = None
        self._recompute_boundary()

        # 惰性构图：不在 __init__（可能无 running loop）里建 checkpointer/graph，
        # 首个 async 调用才在 loop 内构造（AsyncSqliteSaver 需要 get_running_loop）。
        self._graph = None
        self._saver = None
        self._store = None
        self._closed = False            # closed 镜像，随每次 async 状态读取刷新
        self._t0 = time.monotonic()     # metrics().uptime_s 计时起点
        self._msg_count = 0             # messages 数镜像（sync metrics 只读镜像）
        self._blocks = 0                # 世界块钟镜像
        # think 只读边通道（非共享态）：think worker 完成一次即追加；GUI 读尾部。
        # 引擎本就每场一个（GUI 重启建新 SceneEngine → 新 list），无需跨场复用。
        self._think_log: list[dict] = []

        # ---- 场景（叙述者）状态 ----
        # 场景是这个引擎里的**一等 agent**：它有自己的一套触发判据（见 scenarist），
        # 全知（看全部未撤销消息，不做 knows 过滤），输出只有一到两行、只把局势往前推
        # 一步。auto_narrate=False 关掉自动推进（手动 narrate_now 仍可用）；nudge_params
        # 缺省即 scenarist 的标定值，可传 dict 覆盖（GUI/实验调节）。
        self._nudge_params = (nudge_params if isinstance(
            nudge_params, scenarist_mod.SceneNudgeParams)
            else scenarist_mod.SceneNudgeParams(**(nudge_params or {})))
        # 推进**活跃度**（§6.2 可调频率，0..1，缺省 0.5）：经 scenarist.effective_params
        # 统一缩放判据的触发线与冷却，并作为语气提示写进场景提示词。缺省 0.5 = 恒等点，
        # 故本旋钮引入前后无钩子场景的自动推进行为一模一样（既有标定不被扰动）。
        self._narrate_activity = _clamp01(float(narrate_activity))
        self._auto_narrate = auto_narrate
        self._blocks_since_narration = 0     # 距上次叙述的块数（cooldown/停滞分量）
        self._narration_ids: list[int] = []  # 本场产出过的叙述 id（含已被撤销的）
        self._last_nudge: scenarist_mod.NudgeReport | None = None
        self._retracted: list[int] = []      # 共享态 retracted 镜像（sync 读取用）
        self._narration_error: str | None = None   # 最近一次叙述失败原因（诊断用）

        # ---- S4b：钩子 / 场景自改 / 存档 ----
        self._ctx: graph_mod.GraphContext | None = None   # 自持 ctx（改语言指令即时生效）
        self._hook_error: str | None = None        # 最近一次钩子判定/执行的失败原因
        self._tool_error: str | None = None        # 最近一次场景工具指令被忽略/失败的原因
        self._save_error: str | None = None        # 最近一次存档写盘失败原因
        # 隐式事件（§4.2）：visible=False 的事件不落任何消息行，只记在这条只读流里
        # （GUI/测试可读，角色永远看不到）。带块号/turn，供界面按时间线展示。
        self._implicit_events: list[dict] = []
        # 本块钩子落下的**可见行**（交给 step 的事件流，与延时角色动作同一条路）。
        self._hook_lines: list[dict] = []
        # 场景自改进的**变更流**（§3.5）：{field, old, new, at}。钩子的场景事件与场景
        # 工具指令（set_description/append_description/set_name/set_background）两处都
        # 往这里追加，worker 增量广播给界面（日志按浅红高亮）。连续重复的同一次改动
        # 只记一条（模型爱把同一句再写一遍）。用户经「配置场景」的改动**不**进这条流
        # ——那是用户动作，界面自己知道改了什么（见 apply_scene_config）。
        self._scene_changes: list[dict] = []
        # 自动保存（§5）：缺省禁用（every<=0），GUI 按设置里的 5/20/50/100 轮开启。
        self._autosave = scenestore_mod.AutosaveTracker(
            scenestore_mod.AutosavePolicy(every=max(0, int(autosave_every))))

        # ---- 可插拔演员表（§3.3）：运行期在场/禁言/延时角色动作 ----
        # _active 按演员表原序；移出者进 _inactive（记录不删）；_mute 只在场者持有。
        self._active: list[str] = [m.name for m in self.scene.characters]
        self._inactive: list[str] = []
        self._mute: dict[str, int | None] = {}          # name -> 剩余块数（None=永久禁言）
        # 进场基线：转录 turn 制（用 _turn 而非块钟——静默块会让块钟跑在 turn 前面，
        # 用块钟当基线会把进场后的台词也一并挡掉）。场景文件带的 entered_round 只当
        # 初值（本阶段尚无续演存档，通常为 0 = 全程可见）。
        self._entry_round: dict[str, int] = {m.name: int(m.entered_round or 0)
                                             for m in self.scene.characters}
        self._pending_cast: list[hooks_mod.PendingCharacterAction] = []
        self._cast_error: str | None = None    # 最近一次延时角色动作失败原因（诊断用）
        self._turn = 0                          # 共享态 turn 镜像（进场基线 + 落行用）

    def _validate_cast(self) -> None:
        """I4(最终评审)：开场前的一次性 cast 校验——**缺卡**直接拒建。

        重名已在装载循环内即时拦截（见 __init__）。场景**可以一个人都没有**（§3.3：
        先建空场景，角色之后再添加），故空演员表合法、不报错。库里补得到的卡已由
        `_load_missing_cast_cards` 先装载（§3.2），到这里还缺 = 库里也没有这张卡。
        """
        missing = [p for p in self.scene.participants if p not in self.cards]
        if missing:
            raise ValueError(f"场景参与者缺少角色卡: {missing}")

    def _load_missing_cast_cards(self) -> None:
        """把场景阵容里**尚未装载**的卡从角色库按需补上（§3.1 场景持有阵容的另一半）。

        场景文件里存着谁，这一场就得有谁——用户上次存下的名单不该因为「这次没勾他」
        而打不开场景。补不到的留给 `_validate_cast` 报错（库里也没有才是真缺卡）。
        """
        for name in self.scene.participants:
            if name not in self.cards:
                self._load_card_from_library(name)

    def _load_card_from_library(self, name: str) -> str | None:
        """从角色库按名装载一张卡并注册进 self.cards；找不到返回 None（§3.2）。

        找法（两步）：① 直接试 `<角色库目录>/<name>.json`；② 退化为**扫库按卡名匹配**
        ——卡名与文件名不一致（手写/导入的卡）时也能按卡名认出来。逐个文件容错：坏
        json / 不是角色卡（如误扫到场景文件或 sidecar）一律跳过，继续找下一个；一张坏
        文件不该让「加人」这件事炸掉。

        注册进 self.cards 后，图（ctx.cards 与引擎共用同一个 dict）与数值动态都能用
        它——同场后续 think/speak 立刻有效。
        """
        directory = self._characters_dir
        clean = str(name or "").strip()
        if directory is None or not clean:
            return None
        direct = directory / f"{clean}.json"
        candidates: list[Path] = [direct] if direct.is_file() else []
        candidates += [p for p in list_character_paths(directory) if p != direct]
        for path in candidates:
            try:
                card = load_character_card(path)
            except Exception:                       # 坏 json / 不是卡：跳过，继续找
                continue
            if card.name == clean:
                self.cards[card.name] = card
                logger.info("按需装载角色卡：%s（%s）", card.name, path)
                return card.name
        return None

    def _recompute_boundary(self) -> None:
        """按当前 scene.hard_boundary / start_seconds 重算打烊边界（构造期与热更新共用）。

        跨午夜守卫：打烊在开场之后（如 23:30 开场 00:30 打烊 → 视为次日）。parse 只建模
        当天（0..86399），此处把 ≤ 开场的打烊换算到次日（+86400），worker 比较不变。
        hard_boundary 非 time / 值非法 → 无边界（None），不报错（开场前 fail fast 只管
        start_time，边界非法只当没设边界）。
        """
        hb = self.scene.hard_boundary
        self.boundary_value = None
        self.boundary_seconds = None
        if hb is not None and hb.type == "time":
            try:
                self.boundary_seconds = sc.parse_hhmm(hb.value)
                self.boundary_value = hb.value
            except ValueError:
                self.boundary_seconds = None
        if (self.boundary_seconds is not None
                and self.boundary_seconds <= self.start_seconds):
            self.boundary_seconds += sc.SECONDS_PER_DAY

    def _cfg(self):
        return {"configurable": {"thread_id": self.thread_id}, "max_concurrency": 4}

    async def _ensure_graph(self):
        """惰性构图：首次调用在 running loop 内建 checkpointer + 编译 graph，缓存复用。

        run_root 目录已存在 → AsyncSqliteSaver 真实落地；否则 → MemorySaver（无 run_root
        场景静默走内存）。建库失败同样回退内存。
        """
        if self._graph is not None:
            return self._graph
        self._store = InMemoryStore()    # MVP 短期转录；Sqlite 落地见 §5.5 双写
        self._saver = MemorySaver()      # 缺省；run_root 存在才切 AsyncSqliteSaver
        try:
            if self.run_root.is_dir():
                conn = await aiosqlite.connect(str(self.run_root / "scene.sqlite"))
                self._saver = AsyncSqliteSaver(conn=conn)
        except Exception:
            self._saver = MemorySaver()
        # ctx 由引擎自持（不只在 build_graph 内部建）：set_language 要在运行期改语言指令
        # 并**立即**生效（提示词带上新语言），不必重建图/重开 sqlite。
        self._ctx = graph_mod.GraphContext(
            self.cards, self.scene, self.think_backend, self.speak_backend,
            Path(self.run_root), bid_params=self._bid or bidding_mod.BidParams(),
            think_log=self._think_log, demo_alternate=self.demo_alternate,
            bidder=lambda name: self._dynamics.bid(name, self.cards[name].weights),
            cast_provider=self.speakable_names,      # 扇出/竞价只认在场且未禁言者
            entry_round_of=self._entry_round_of,     # 进场可见性基线（§3.3）
            language_directive=self._language_directive,
            space=self._scene_space)                 # C1：不可变空间键（改名不动它）
        self._graph = graph_mod.build_graph(
            self.cards, self.scene, self.think_backend, self.speak_backend,
            run_root=self.run_root, bid_params=self._bid, checkpointer=self._saver,
            closing_at_block=self.closing_at_block, store=self._store,
            demo_alternate=self.demo_alternate, think_log=self._think_log,
            bidder=lambda name: self._dynamics.bid(name, self.cards[name].weights),
            cast_provider=self.speakable_names,      # 扇出/竞价只认在场且未禁言者
            entry_round_of=self._entry_round_of,     # 进场可见性基线（§3.3）
            ctx=self._ctx)
        return self._graph

    async def _snapshot(self) -> dict:
        """读取当前共享态并刷新镜像（closed/messages/blocks）。AsyncSqliteSaver 不可
        走 sync get_state；镜像供 sync 的 metrics()/closed 读取，绝不反查 async graph。"""
        graph = await self._ensure_graph()
        snap = await graph.aget_state(self._cfg())
        vals = snap.values
        self._closed = bool(vals.get("closed", False))
        self._msg_count = len(vals.get("messages") or [])
        self._blocks = int(vals.get("blocks", 0) or 0)
        self._turn = int(vals.get("turn", 0) or 0)
        self._retracted = list(vals.get("retracted") or [])
        return vals

    async def messages(self) -> list[dict]:
        return (await self._snapshot()).get("messages", [])

    async def open_scene(self, opening: str | None = None) -> list[dict]:
        """开场：导演开场消息的 content = opening（给了就用），否则沿用默认
        （"夜色渐深，两人对坐。"）。开场消息不带时刻戳——真实流速虚拟钟在
        worker/GUI 侧（开场即此刻），由 worker 派发时补 time_hhmmss。其余与事件流不变。"""
        graph = await self._ensure_graph()
        content = (opening if opening is not None
                   else "夜色渐深，两人对坐。")
        opening_msg = {"id": 0, "speaker": "导演", "speaker_type": "director",
                       "content": content,
                       "in_scene": self._scene_space, "turn": 0}
        events = [ev.block_spoken(opening_msg), ev.scene_state(0, False)]
        # 外部写入一律声明 as_node=START（langgraph 1.2：已有 checkpoint 后再 aupdate_state
        # 不带 as_node 会 Ambiguous update；导演注入视同新输入从 START 进入）。
        await graph.aupdate_state(self._cfg(), {
            "messages": [opening_msg], "current_speaker": None,
            "turn": 0, "blocks": 0, "silent_streak": 0,
            "closed": False, "decided": None, "injected": [],
            "closing_at_block": self.closing_at_block,
        }, as_node=START)
        self._closed = False
        self._turn = 0              # 开场即世界起点：进场基线随新场从 0 起算
        # I2(最终评审)：**真的**把进场基线清零（此前 docstring 这么说、代码没做）。
        # 清空 = 全员基线 0（`_entry_round_of` 对未知名字返回 0），故场景文件里那份
        # entered_round（块钟制，仅作记录）不会把新场的对话误挡掉；续演时基线由
        # `_restore_cast_meta` 从 sidecar 的 meta 重新套回（open_scene 先、restore 后，
        # 正是 GUI 的调用次序）。
        self._entry_round = {name: 0 for name in self._entry_round}
        self._msg_count += 1        # 镜像同步（开场消息已入 state）
        return events

    async def step(self, n: int = 1) -> list[dict]:
        graph = await self._ensure_graph()
        out: list[dict] = []
        for _ in range(n):
            st = await self._snapshot()
            if st.get("closed"):
                break
            msgs = st.get("messages") or []
            n_before = len(msgs)                # 本块前消息条数（diff 判产出说话人）
            think_before = len(self._think_log)  # 本块前 think 日志条数（diff 折 observe）
            self._dynamics.round_start()        # 轮号推进（bid 噪声据此确定性）
            self._apply_scene_pressure()        # 虚拟钟临近硬边界 → 全员 scene_pressure
            await graph.ainvoke({}, self._cfg())
            st = await self._snapshot()
            # 数值动态推进：折本块 think → 判 mark_spoke/tick_silence（见 docstring）
            self._advance_dynamics(n_before, think_before, st)
            out += [ev.decision_event("block_done"), ev.scene_state(
                st.get("turn", 0), st.get("closed", False))]
            # 块末的可插拔演员表推进（§3.3/§4）：先让本块起算的禁言走完一格（N 块禁言
            # 恰好挡 N 个块），再让到期的延时角色动作执行——本块新下的禁言因此不被本块
            # 扣减。两种产出都是正常消息行，随事件流/界面派发（worker 靠 sweep 上屏）。
            self._tick_mutes()
            for line in await self._fire_pending_cast():
                out.append(ev.block_spoken(line))
            narrated = await self._maybe_narrate(st)
            # 钩子落下的可见行（上下文事件/角色事件/场景事件）先进事件流：它们是在本块
            # 叙述之前写下的（叙述要体现改动后的新状态，见 _narrate 的次序）。
            for line in self._drain_hook_lines():
                out.append(ev.block_spoken(line))
            if narrated is not None:
                out.append(ev.block_spoken(narrated))
            # 自动保存（§5）：到点就在块末静默存一次（失败只记诊断，绝不打断本块）。
            await self._maybe_autosave()
        return out

    # ------------------------------------------------ 场景（叙述者）：判据与产出 --
    async def _maybe_narrate(self, st: dict) -> dict | None:
        """每块末的场景推进判定（自动档）：块数 +1 → 算判据 → 决定「咨询」与「落行」两件事。

        与角色的 bid 完全对称：场景有自己的触发算法（scenarist.evaluate），判据读的是
        **未撤销**的全量消息（场景全知，不做 knows 过滤）。被 cooldown 挡住时不开口但
        照样记录报告（GUI/日志能解释"这轮为什么没推"）。

        **咨询 ≠ 叙述**（§6.2 实况抱怨「有 hook 的场景每块都出一条叙述」的修复口径）：
          · 有启用钩子 → **每块都咨询**场景（§4.1①：它本来就每块要综观上下文，钩子判定
            只是多分配一个任务、不额外增加调用）。判据/冷却不再挡在咨询前面，但**落不落
            一条叙述消息，由场景自己的产出决定**——只有真给出正文（parse_narration().text
            非空）才落行；只报 `[[HOOK:…]]`、或什么都不写，就是"这一轮不叙述"，钩子照常
            执行、正文不落一行（见 _narrate 的"空产出不落行"）。
          · 无钩子 → 判据说话：分数过**活跃度缩放后**的触发线且不在冷却期才开口。
            活跃度（§6.2 可调频率）经 scenarist.effective_params 统一缩放触发线与冷却，
            并作为语气提示写进场景提示词——引擎只决定"何时叫它"，叫来之后多不多说由
            场景自己按活跃度把握。

        「自动推进」总开关仍是唯一闸门：关掉后引擎不再自动调场景，钩子也随之不自动判定
        （手动 narrate_now 照常带钩子清单）。
        """
        self._blocks_since_narration += 1
        report = self._evaluate_nudge(st)
        self._last_nudge = report
        if not self._auto_narrate:
            return None
        params = self._effective_nudge_params()
        # 节奏闸门：判据过（活跃度缩放后的）触发线、且不在冷却期 → 这一轮"可以叙述"。
        may_narrate = (report.score >= params.threshold
                       and not report.blocked_by_cooldown)
        if self._enabled_hooks():
            # 有钩子：**咨询照旧每块做**（判 hook、执行事件），但**叙述仍受节奏闸门约束**——
            # 否则模型只要每块都写字，就能绕过冷却与活跃度、变成"每块一条叙述"（实况抱怨）。
            return await self._narrate(report, consult_hooks=True,
                                       keep_narration=may_narrate)
        if may_narrate:
            return await self._narrate(report)
        return None

    def _effective_nudge_params(self) -> scenarist_mod.SceneNudgeParams:
        """当前活跃度缩放后的判据参数（触发线/冷却随活跃度；其余不动）。"""
        return scenarist_mod.effective_params(self._nudge_params, self._narrate_activity)

    def _evaluate_nudge(self, st: dict) -> scenarist_mod.NudgeReport:
        """当前局势 → 推进判据报告（纯计算，不调后端、不改状态）。

        用**活跃度缩放后**的参数算：`blocked_by_cooldown` 于是也跟着活跃度走（高活跃度
        冷却更短），调用方的判据与报告因此同一口径。
        """
        return scenarist_mod.evaluate(
            graph_mod._live_messages(st), self._blocks_since_narration,
            int(st.get("silent_streak", 0) or 0), self._clock_progress(),
            self._effective_nudge_params())

    def _clock_progress(self) -> float | None:
        """距打烊的推进比例 0..1（无 time 硬边界 → None=不参与判定）。

        钟由 worker 经 set_clock_now 注入；未注入则取开场时刻（进度 0）。
        """
        if self.boundary_seconds is None:
            return None
        clock = self._clock_now if self._clock_now is not None else self.start_seconds
        total = self.boundary_seconds - self.start_seconds
        return _clamp01((clock - self.start_seconds) / total) if total else 0.0

    def _clock_text(self) -> str:
        """叙述提示词里的【当前时刻】：当前钟点（+ 距打烊还有多久，若有硬边界）。"""
        clock = self._clock_now if self._clock_now is not None else self.start_seconds
        text = sc.format_hhmm(clock)
        if self.boundary_seconds is not None:
            remain_min = max(0, self.boundary_seconds - clock) // 60
            text += f"，距打烊还有 {remain_min} 分钟"
        return text

    async def _narrate(self, report: scenarist_mod.NudgeReport | None = None,
                       *, consult_hooks: bool = False,
                       keep_narration: bool = True) -> dict | None:
        """场景开口：构提示词 → 调 narrate 档后端 → 剥指令 → 裁剪成 1~2 行 → 落进共享态。

        视图 = **全部未撤销消息**（场景全知，不做 knows 过滤——它要能看见角色看不见的
        私密句才谈得上"知道世界在发生什么"）。落点的在场轴 = 本场景名（尾条的 in_scene
        沿用，无尾条则场景名）、knows=None（人人都看得见发生的事）。叙述失败绝不把异常
        带进场景：记下原因、返回 None，本块当作没发生。

        一次场景开口里发生的事（次序固定）：
          1. 调 narrate 档 → 拿到原始文本（带指令行的，别直接落盘）；
          2. 剥 `[[HOOK:<id>]]`（本地解析，见 _parse_hook_directives）→ 钩子 id 列表；
          3. `parse_narration` 剥 `[[TOOL:...]]` → 工具指令；
          4. 有工具且 description_mutable → 改**活场景**（名/描述/背景，绝不含路径），
             并把改后的新描述/背景**再喂回场景多想一次**（§4.1③：这一轮它还没输出思考），
             用第二次的产出当本轮叙述；第二次里的工具照改，但**绝不再触发第三次思考**
             （上限一轮，防模型自我循环）；
          5. 逐条执行钩子（块内去重、逐条校验，失败只记诊断）；
          6. 裁剪正文落一行（正文永远不含任何指令）。
        content 为空（场景判断此刻不必叙述）就**不落行**——这正是 §6.2 的口径：被叫来
        咨询（判钩子）不等于要出一条叙述，只有 `parse_narration(raw).text` 真有正文才落
        消息。空产出仍可能已触发钩子/改了场景，不算失败。
        """
        try:
            st = await self._snapshot()
            if st.get("closed"):
                return None                      # 已收束：世界不再推进
            live = graph_mod._live_messages(st)
            view_text = render_view([Message.model_validate(m) for m in live])
            reason_text = "；".join(report.reasons) if report and report.reasons else "手动推进"
            hooks_on = bool(consult_hooks and self._enabled_hooks())
            extra = self._scene_prompt_extra(hooks_on)
            raw = await self._call_scene(view_text, reason_text, extra)
            text, hook_ids = self._parse_hook_directives(raw)
            parsed = scenarist_mod.parse_narration(text)
            if parsed.tools:
                if self.scene.description_mutable:
                    self._apply_scene_tools(parsed.tools)
                    # 额外思考一次（仅此一次）：把「我改了什么」纳入考量再产出本轮输出。
                    raw2 = await self._call_scene(view_text, reason_text, extra)
                    text2, hook_ids2 = self._parse_hook_directives(raw2)
                    hook_ids += [i for i in hook_ids2 if i not in hook_ids]
                    parsed = scenarist_mod.parse_narration(text2)
                    if parsed.tools:            # 第二轮的工具照改，但不再触发第三次思考
                        self._apply_scene_tools(parsed.tools)
                else:
                    self._tool_error = (
                        f"场景未标为「可改变」（description_mutable=False）→ 忽略 "
                        f"{len(parsed.tools)} 条场景工具指令")
                    logger.warning("场景自改被忽略：%s", self._tool_error)
            if hook_ids:
                await self._fire_hooks(hook_ids)
            if not keep_narration and not hook_ids:
                # 节奏闸门没过（活跃度/冷却）**且这一轮没有任何钩子真的触发** → 只带回
                # 诊断、不落叙述。这样"有 hook 的场景每块都插一句"不再成立；而**真的发生了
                # 事件**（钩子触发）时，它那句话是该事件的说明，照常落行。
                return None
            content = _trim_narration(parsed.text)
            if not content:
                return None                      # 空产出：不落一行空话（钩子/改动照常生效）
            # 取号/取尾都要**重新快照**：钩子刚落过行（上下文/播报），用开场时那份旧快照
            # 取号会让叙述捡回同一个 id。
            st = await self._snapshot()
            live = graph_mod._live_messages(st)
            tail = live[-1] if live else None
            msg = {"id": graph_mod._next_id(st.get("messages") or []),
                   "speaker": "场景", "speaker_type": "narrator",
                   "content": content,
                   "in_scene": (tail.get("in_scene") if tail else None)
                               or self._scene_space,
                   "turn": st.get("turn", 0)}
            await self._post_line(msg)
            self._blocks_since_narration = 0     # 刚推进过：cooldown 重新起算
            self._narration_ids.append(msg["id"])
            self._narration_error = None
            return msg
        except Exception as exc:                 # 后端/校验任何失败都不进场景
            self._narration_error = f"{type(exc).__name__}: {exc}"
            return None

    # ------------------------------------------- 场景提示词（S4b：字段/工具/钩子） ----
    def _scene_prompt_extra(self, hooks_on: bool) -> str:
        """叙述提示词的追加块：可改变字段的工具指令说明 + 钩子清单与报告契约。

        两段都非空才拼；都没有 → ""（提示词与 S4b 之前逐字节相同，既有路径不漂移）。
        工具段只在 description_mutable 时给——不给语法，模型就无从凭空改场景。
        """
        parts: list[str] = []
        if self.scene.description_mutable:
            tools_block = scenarist_mod.tools_prompt_block(
                ["description", "background", "name"])
            if tools_block:
                parts.append(tools_block)
        if hooks_on:
            hooks_block = hooks_mod.hooks_prompt_block(self._enabled_hooks(),
                                                      self._pending_cast)
            if hooks_block:
                parts.append(f"{hooks_block}\n\n{_HOOK_INSTRUCTION}")
        return "\n\n".join(parts)

    def _build_scene_prompt(self, view_text: str, reason_text: str,
                            extra: str = "") -> list[dict]:
        """叙述提示词 = build_narrate_messages（含场景四字段/活跃度/语言指令）+ 追加块。

        追加块接在**系统**消息末尾（工具语法与钩子契约都是「世界怎么动」的规则，与系统
        消息的世界规则同类；用户消息仍以「请写一到两行：」收尾）。语言指令仍是系统消息
        里的一段（只是不再恒为最后一段——规则块随任务追加）。

        活跃度提示（§6.2）同样走系统消息（在剧情走向之后、语言指令之前）：它是给场景
        自己的频率说明，不是本轮的局势。是否有钩子决定提示里提不提 `[[HOOK:…]]` 语法，
        以及要不要附上时间条件的判定契约（TIME_CONDITION_RULE，见 engine._time_gate）。
        """
        hooks_on = bool(self._enabled_hooks())
        msgs = build_narrate_messages(
            view_text, f"场景：{self.scene.name}", self._clock_text(), reason_text,
            language_directive=self._language_directive,
            background_text=self.scene.background,
            description_text=self.scene.description,
            plot_text=self.scene.plot_direction,
            activity_hint=scenarist_mod.activity_hint(
                self._narrate_activity, hooks=hooks_on),
            hook_time_rule=(TIME_CONDITION_RULE if hooks_on else ""))
        if extra:
            msgs[0]["content"] = f"{msgs[0]['content']}\n\n{extra}"
        return msgs

    async def _call_scene(self, view_text: str, reason_text: str, extra: str = "") -> str:
        """调 narrate 档后端拿原始产出（**未剥指令**，交给调用方解析）。"""
        return await self.narrate_backend.complete_text(
            self._build_scene_prompt(view_text, reason_text, extra))

    # ------------------------------------------------------- 钩子指令的解析与执行 ----
    def _enabled_hooks(self) -> list[hooks_mod.Hook]:
        """启用中的钩子（按场景里的声明序）。停用者既不进提示词、也不许被触发。"""
        return [h for h in (self.scene.hooks or []) if getattr(h, "enabled", True)]

    def _parse_hook_directives(self, raw: str | None) -> tuple[str, list[str]]:
        """把一段场景产出切成（剥掉钩子指令的正文, 命中的钩子 id 列表）。**绝不抛异常**。

        规则与 scenarist 的工具指令同一约定：一条指令从 `[[HOOK:` 一直吃到**行尾**
        （参数不能跨行），一行只有第一条有效；畸形片段（有开头没 `]]`、`[[HOOK]]` 少冒
        号、id 含空白或非法字符、id 为空）记进诊断并**整段剥离**——`[[HOOK` 泄进用户
        看到的叙述是事故，宁可少几行。id 校验只认 [A-Za-z0-9_.-]（宽松但拒空白）。
        去重留给 `_fire_hooks`（同一 id 在块内只执行一次）。
        """
        text = "" if raw is None else str(raw)
        self._hook_error = None          # 每次解析都重开一份诊断（"最近一次"语义）
        lines: list[str] = []
        ids: list[str] = []
        malformed: list[str] = []
        for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            start = raw_line.find(_HOOK_MARK)
            if start < 0:
                lines.append(raw_line)      # 没有标记：整行原样保留
                continue
            head = raw_line[:start]
            end = raw_line.find(_HOOK_CLOSE, start + len(_HOOK_MARK))
            if end < 0:
                malformed.append(raw_line[start:].strip())
                lines.append(head)
                continue
            header = raw_line[start + len(_HOOK_MARK):end]
            if not header.startswith(":"):
                malformed.append(raw_line[start:end + len(_HOOK_CLOSE)].strip())
                lines.append(head)
                continue
            hook_id = header[1:].strip()
            if not hook_id or not _HOOK_ID_RE.match(hook_id):
                malformed.append(raw_line[start:end + len(_HOOK_CLOSE)].strip())
                lines.append(head)
                continue
            ids.append(hook_id)
            lines.append(head)
        if malformed:
            self._note_hook_error(f"畸形钩子指令（已剥离）：{'；'.join(malformed)}")
        return "\n".join(lines), ids

    def _note_hook_error(self, message: str) -> None:
        """钩子诊断：累积进 _hook_error（同一块里多条诊断都留得住）**并落日志**。

        规格要求「非法钩子带原因被跳过」——原因既给界面（scene_state），也进 logging
        （事后排障：模型吐了什么、哪条钩子为什么没执行）。绝不抛异常。
        """
        self._hook_error = (f"{self._hook_error}；{message}" if self._hook_error
                            else message)
        logger.warning("场景钩子诊断：%s", message)

    async def _fire_hooks(self, hook_ids: list[str]) -> list[str]:
        """执行本块命中的钩子：块内按 id 去重、逐条校验、失败只记诊断（绝不炸本块）。

        跨块**无状态**（§4.1①：条件由场景每轮自己判）——场景下一块再报一次，就再执行
        一次；块内重复报告只算一次（老实说模型偶尔会同一 id 写两行）。

        **时间条件闸门**（实况事故 2026-09）：条件是钟点的钩子（如「虚拟钟走到 21:30」
        → 乙/甲离场）不交给模型一句话定生死——执行前用
        `scenarist.time_condition_target` 做一次确定性校验，钟点没到就**跳过**并记中文
        诊断（见 `_time_gate`）。模型误判时间是最常见的一类误报：用户在 19:03 加人，
        两人曾因模型判错而提前 2.5 小时走掉。
        """
        by_id = {h.id: h for h in self._enabled_hooks()}
        fired: list[str] = []
        clock_unknown_noted = False                 # 「钟点未知」同一块只提一次
        for hook_id in dict.fromkeys(hook_ids):     # dict.fromkeys = 去重且保序
            hook = by_id.get(hook_id)
            if hook is None:
                self._note_hook_error(f"钩子 {hook_id} 未启用或不存在（已忽略）")
                continue
            action, target = self._time_gate(hook)
            if action == "unknown":
                # 钟未注入 = 无证据：不凭猜测拦，但要让用户看见"这条没校验"。
                if not clock_unknown_noted:
                    self._note_hook_error(
                        f"钩子 {hook_id} 的条件是 {sc.format_hhmm(target)}，但当前钟点"
                        f"未知（虚拟钟未注入）——无法校验，本次不跳过")
                    clock_unknown_noted = True
            elif action == "not_yet":
                self._note_hook_error(
                    f"钩子 {hook_id} 的条件是 {sc.format_hhmm(target)}，当前 "
                    f"{sc.format_hhmm(self._clock_now)} 未到点，已跳过")
                continue
            try:
                errors = hooks_mod.validate(hook)
                if errors:
                    self._note_hook_error(
                        f"钩子 {hook_id} 不合法，本次跳过：{'；'.join(errors)}")
                    continue
                line = await self._apply_hook(hook)
            except Exception as exc:                # 校验/执行任何异常：只记原因，不炸本块
                self._note_hook_error(f"钩子 {hook_id} 执行失败："
                                      f"{type(exc).__name__}: {exc}")
                continue
            fired.append(hook_id)
            if line is not None:                    # 可见行进本块事件流（step 统一派发）
                self._hook_lines.append(line)
        return fired

    def _effective_time_target(self, target: int) -> int:
        """时间条件的目标秒数 → 本场的**有效目标**（跨午夜与打烊边界同口径）。

        `time_condition_target` 只建模当天（0..86399），而「虚拟钟走到 00:30」在 19:00
        开场的这一场里指的是**次日**——与 `_recompute_boundary` 把 ≤ 开场的打烊 +86400
        换算是同一条规则（引擎里只有这一处判跨午夜，钩子闸门复用它）。
        """
        return target + sc.SECONDS_PER_DAY if target <= self.start_seconds else target

    def _time_gate(self, hook: hooks_mod.Hook) -> tuple[str, int | None]:
        """钩子的时间条件闸门 → (动作, 有效目标秒数)。

        动作三态：
          "pass"    不是时间条件（`time_condition_target` 为 None，交场景自己判——此时
                    目标也是 None），或钟点已到 → 正常执行；
          "not_yet" 明确是时间条件、当前钟点**还没到** → 调用方跳过其事件；
          "unknown" 明确是时间条件、但此刻钟点未知（`_clock_now is None`）→ **不跳**
                    （没有证据就不推翻场景的判断），调用方只记一句诊断。
        "not_yet"/"unknown" 两个动作的诊断都要目标秒数来写「条件是 21:30」，故一并返回。

        比较用**原始秒数**（可跨 86400，与 worker 注入的虚拟钟同域），不取模：
        target 已按本场开场时刻换算到次日，取模会把「次日 00:30」比回「当天 00:30」。
        """
        target = scenarist_mod.time_condition_target(hook.condition)
        if target is None:
            return "pass", None
        target = self._effective_time_target(target)
        if self._clock_now is None:
            return "unknown", target
        return ("pass", target) if self._clock_now >= target else ("not_yet", target)

    async def _apply_hook(self, hook: hooks_mod.Hook) -> dict | None:
        """一条钩子的执行（三类事件，§4.1②③ + §4.2 显式/隐式）：返回落下的可见行。

        · context：把 context_text 写进上下文（speaker=场景/narrator/knows=None）；
        · character：add/remove/mute/mute_turns/unmute（复用引擎既有的演员表方法，
          visible 原样透传——隐式离场不落行、只改状态）；
        · scene：改写场景自身字段（白名单见 _SCENE_HOOK_KEYS，**文件名/路径永不改**），
          改动本身可见时才补一行「场景变了：…」。
        隐式（visible=False）一律不落消息行，只记进 _implicit_events（§4.2：读者看不到，
        内部状态照改）。
        """
        if hook.event_kind == "context":
            text = (hook.context_text or "").strip()
            if not hook.visible:
                self._note_implicit(hook, text)
                return None
            return await self._post_cast_line(text)

        if hook.event_kind == "character":
            line = await self._apply_character_hook(hook)
            if not hook.visible:
                self._note_implicit(
                    hook, f"{hook.character_name.strip()} {hook.action}")
            return line

        if hook.event_kind == "scene":
            changed = self._apply_scene_hook(hook)
            if not changed:
                return None
            text = self._scene_change_text(changed)
            if not hook.visible:
                self._note_implicit(hook, text)
                return None
            return await self._post_cast_line(text)

        raise ValueError(f"未知的事件类型：{hook.event_kind}")   # validate 已挡，纯兜底

    async def _apply_character_hook(self, hook: hooks_mod.Hook) -> dict | None:
        """角色事件（§4.1②）：dispatch 到 add/remove/mute/mute_turns/unmute。

        add/remove 透传 visible（隐式进出不落播报行）；禁言/解禁本来就不落行（状态变化
        由 sig_cast/左栏呈现）。`mute`（无轮数）= 永久禁言（turns=0），`mute_turns` 带上
        场景判定的轮数。
        """
        name = hook.character_name.strip()
        action = hook.action
        if action == "add":
            return await self.add_character(name, visible=hook.visible)
        if action == "remove":
            return await self.remove_character(name, visible=hook.visible)
        if action == "mute_turns":
            await self.mute_character(name, int(hook.turns or 1))
        elif action == "mute":
            await self.mute_character(name, 0)
        elif action == "unmute":
            await self.unmute_character(name)
        else:
            raise ValueError(f"未知的角色动作：{action}")       # validate 已挡，纯兜底
        return None

    def _apply_scene_hook(self, hook: hooks_mod.Hook) -> list[str]:
        """场景事件（§4.1③）：把 hook.scene_patch 应用到**活场景**上，返回真正改掉的键。

        只认白名单 _SCENE_HOOK_KEYS（场景名/描述/背景/剧情走向/日期）——文件名与路径是
        磁盘身份（归 GUI/装载器），patch 里的 path/filename 与任何未知键一律忽略（与
        scenarist.apply_scene_tools 同一条纪律，只是这里就地改活对象而非返回新 dict）。
        删掉场景名无意义：空串照写（用户可见），不做额外政策。
        """
        changed: list[str] = []
        for key, value in (hook.scene_patch or {}).items():
            if key not in _SCENE_HOOK_KEYS:
                self._note_hook_error(
                    f"钩子 {hook.id} 想改不可改的字段：{key}（已忽略）")
                continue
            old = getattr(self.scene, key, "")
            new = "" if value is None else str(value)
            setattr(self.scene, key, new)
            changed.append(key)
            self._note_scene_change(key, old, new)     # §3.5：自改进变更流
        return changed

    def _scene_change_text(self, changed: list[str]) -> str:
        """场景改动的可见播报行（隐式事件也记这一句，供界面/日志解释内部状态）。

        只报「哪个字段改成了什么」——描述/背景这类长文本裁到 40 字（播报行不是复述全文，
        新描述本身会经提示词场景段被角色看到）。
        """
        parts = [f"{_SCENE_HOOK_LABELS.get(key, key)}改为「"
                 f"{_clip(str(getattr(self.scene, key, '')), 40)}」" for key in changed]
        return "（场景变了：" + "；".join(parts) + "。）"

    def _note_implicit(self, hook: hooks_mod.Hook, content: str) -> None:
        """隐式事件（§4.2）：事件不落消息行，只进只读事件流（_implicit_events）。"""
        self._implicit_events.append({
            "hook_id": hook.id, "event_kind": hook.event_kind,
            "content": content, "visible": False,
            "turn": self._turn, "blocks": self._blocks})

    def implicit_events(self) -> list[dict]:
        """隐式事件流快照（浅拷贝，供 GUI/测试）：visible=False 的钩子事件全在这里。"""
        return [dict(e) for e in self._implicit_events]

    def _drain_hook_lines(self) -> list[dict]:
        """取走本块钩子落下的可见行（step 事件流用），取完即清。"""
        lines, self._hook_lines = self._hook_lines, []
        return lines

    def _apply_scene_tools(self, calls: list[scenarist_mod.SceneToolCall]) -> list[str]:
        """把场景工具指令应用到**活场景**（§4.1③）：就地改 name/description/background。

        复用 scenarist.apply_tools 的政策（它负责拒绝 set_path/set_filename、忽略不认识
        的工具名、处理 append_description 的追加语义），只是拿一个投影 dict 进出、再写回
        活场景——**文件名/路径永远不在其中**（apply_tools 本身就不写这些键）。
        返回真正被改动的键，供日志；每条改动同时进 scene_changes()（§3.5：界面日志高亮）。
        """
        current = {"name": self.scene.name, "description": self.scene.description,
                   "background": self.scene.background}
        patch = scenarist_mod.apply_tools(current, calls)
        changed = [k for k in ("name", "description", "background")
                   if patch.get(k) != current[k]]
        for key in changed:
            self._note_scene_change(key, current.get(key, ""), patch[key])
            setattr(self.scene, key, patch[key])
        self._tool_error = None if changed else (
            f"场景工具指令未改动任何字段（{'、'.join(c.name for c in calls)}）")
        return changed

    def scene_state(self) -> dict:
        """场景自身信息快照（sync、只读；供 GUI/测试看钩子与语言等运行期设置）：
          language        当前语言码
          hooks           启用中的钩子 id（按声明序）
          description_mutable
          fields          {name/date/background/description/plot_direction}（**活值**——
                          场景工具/场景事件改过之后这里就是改后的样子）
          hook_error/tool_error/save_error  最近一次诊断（None = 无）
          implicit_events 隐式事件流
        """
        return {
            "language": self.language,
            "hooks": [h.id for h in self._enabled_hooks()],
            "description_mutable": bool(self.scene.description_mutable),
            "fields": {k: getattr(self.scene, k, "")
                       for k in ("name", "date", "background", "description",
                                 "plot_direction")},
            "hook_error": self._hook_error,
            "tool_error": self._tool_error,
            "save_error": self._save_error,
            "implicit_events": self.implicit_events(),
        }

    async def narrate_now(self) -> dict | None:
        """手动「推进一下」：无视 cooldown 与 auto 开关，立刻让场景推进一步。

        手动路径同样带钩子清单（用户点「推进一下」= 请场景综观一次局势，钩子判定是它
        这一眼的天然部分），也**同样受时间条件闸门约束**（钩子的执行只走 `_fire_hooks`
        一条路，手动与自动同口径——否则按一下「推进一下」就能提前引爆 21:30 的钩子）；
        落下的可见行由调用方从 messages 读（GUI/worker 走 flush），
        这里把事件缓冲清掉：**清在最后**（M4 最终评审）——先清再叙述会把本次叙述自己落下
        的钩子行留在缓冲里，下一次 step() 于是把它们当成那一块的产出再派发一遍（同一行
        在流里出现两次）。开头也清一次：清掉上一条残留，双保险。
        """
        self._drain_hook_lines()
        st = await self._snapshot()
        report = self._evaluate_nudge(st)
        self._last_nudge = report
        msg = await self._narrate(report, consult_hooks=bool(self._enabled_hooks()))
        self._drain_hook_lines()      # 本块自己的钩子行留给 messages，不进下一次 step 的事件流
        return msg

    def set_auto_narrate(self, on: bool) -> None:
        """开关自动推进（GUI 左栏「自动推进」复选框）：关掉后只认手动 narrate_now()。

        sync、只改一个旗标（供 narration_state()["auto"] 如实回读）；调用方须在引擎
        所在 loop 内调用（worker 经 _submit 投递，GUI 线程不直接碰引擎）。
        """
        self._auto_narrate = bool(on)

    def set_narrate_activity(self, level: float) -> None:
        """改推进活跃度（§6.2 GUI 左栏「场景推进」的 少/中/多/极多）：**立即生效**。

        sync、只改一个标量（供 narration_state()["activity"] 如实回读）：判据的触发线
        与冷却在**下一次判定**时即用新值（_effective_nudge_params 是现算的，无需重建
        任何东西），场景提示词里的活跃度提示同样是下一轮现拼的。越界夹到 0..1（非数字
        → 抛 ValueError，让调用方看到非法输入，绝不静默改档）。
        调用方须在引擎所在 loop 内调用（worker 经 _post_call 投递）。
        """
        self._narrate_activity = _clamp01(float(level))

    # ---------------------------------- 场景配置热更新 + 自改变更流（§3.4/§3.5） ----
    def apply_scene_config(self, **fields) -> list[str]:
        """把「配置场景」的改动**就地**应用到运行中的活场景（§3.4），返回真正改动的字段名。

        可热更新的字段：name / date / background / description / description_mutable /
        plot_direction（见 _SCENE_HOT_FIELDS）+ hard_boundary + start_time。不认识的键
        一律忽略（文件名/路径是磁盘身份，永远不从这条路径改）。

        · **name 改的是显示名**：不可变空间键（self._scene_space，C1）绝不跟着动，历史
          因此照旧可见（改名不是换场）；
        · start_time / hard_boundary 改完**重算派生值**（start_seconds/boundary_seconds/
          boundary_hhmm 与跨午夜守卫）——场景压力与叙述提示词的当前时刻据此立即变化；
        · 非法 start_time（不是 HH:MM）→ 中文 ValueError，且**一个字段都不改**（先全量
          校验、再统一落值）；
        · 提示词读的就是这个活场景对象（GraphContext.scene 与 self.scene 是同一个），
          故下一块的 think/speak/narrate 就用新值，无需重建图、无需重开一场。

        用户动作**不进** `scene_changes()`（那是「场景 agent 自改」的变更流）；写回场景
        文件由 GUI 的编辑器落盘 + worker.save_scene() 负责。
        """
        pending: dict[str, Any] = {}
        if "start_time" in fields:                     # 先校验（非法 → 什么都不改）
            raw = str(fields["start_time"] or "").strip()
            if raw:
                sc.parse_hhmm(raw)                     # 非法值当场 ValueError
                pending["start_time"] = raw
        if "hard_boundary" in fields:
            pending["hard_boundary"] = _as_hard_boundary(fields["hard_boundary"])
        for key in _SCENE_HOT_FIELDS:
            if key not in fields:
                continue
            value = fields[key]
            pending[key] = (bool(value) if key == "description_mutable"
                            else str(value if value is not None else ""))
        changed: list[str] = []
        for key, new in pending.items():
            if getattr(self.scene, key) == new:
                continue                               # 没实际变化就不算改动
            setattr(self.scene, key, new)
            changed.append(key)
        if "start_time" in pending:
            self.start_time = self.scene.start_time
            self.start_seconds = sc.parse_hhmm(self.start_time)
        if "start_time" in pending or "hard_boundary" in pending:
            self._recompute_boundary()                 # 派生值随新起点/新边界重算
        return changed

    def scene_changes(self) -> list[dict]:
        """场景**自改**的变更流（§3.5）：`[{field, old, new, at}]` 的浅拷贝。

        两条来源都记在这里：钩子的场景事件（scene_patch）与场景工具指令
        （set_description/append_description/set_name/set_background）。`at` = 记录那一刻的
        世界块钟；连续重复的同一次改动只记一条（模型/钩子常把同一句再写一遍）。
        worker 据此增量广播 sig_scene_changed，界面在日志里浅红高亮。
        """
        return [dict(c) for c in self._scene_changes]

    def _note_scene_change(self, field: str, old: Any, new: Any) -> None:
        """追加一条场景自改记录（连续重复的丢弃）：见 scene_changes 的 docstring。"""
        old_s, new_s = str(old if old is not None else ""), str(new if new is not None else "")
        last = self._scene_changes[-1] if self._scene_changes else None
        if (last is not None and last["field"] == field
                and last["old"] == old_s and last["new"] == new_s):
            return
        self._scene_changes.append({"field": field, "old": old_s, "new": new_s,
                                    "at": self._blocks})

    # ------------------------------------------------ 语言（§7，S4b） ----
    @property
    def language_directive(self) -> str:
        """当前语言指令（如「你将使用简体中文回答。」）——think/speak/narrate 三处共用。"""
        return self._language_directive

    def set_language(self, code: str) -> bool:
        """切换 LLM 作答语言（§7）：认识返回 True，不认识的码回落默认语言并返回 False。

        sync（GUI 设置在任意时刻点得到），改完**立即生效**——提示词带的是引擎自持
        GraphContext 里的那一份（think/speak 下一块就换语言），无需重建图。narrate 走
        引擎自己的 `_language_directive`，同样即时。
        """
        known = code in i18n_mod.LANGUAGES
        self.language = code if known else i18n_mod.DEFAULT_LANGUAGE
        self._language_directive = i18n_mod.llm_language_directive(self.language)
        if self._ctx is not None:
            self._ctx.language_directive = self._language_directive
        return known

    # ------------------------------------------------ 保存/续演/自动保存/重置（§5） ----
    def _scene_file(self) -> Path | None:
        """本场场景文件路径（sidecar 存档坐标）；未给（纯内存引擎）→ None。"""
        return self._scene_path

    def set_autosave_every(self, n: int) -> None:
        """自动保存周期（设置里的 5/20/50/100 轮；<=0 = 关闭）。

        sync、只换 tracker（保留「已存到第几块」的记账点——中途改周期不该立刻或永远
        存不上）。
        """
        every = max(0, int(n))
        self._autosave = scenestore_mod.AutosaveTracker(
            scenestore_mod.AutosavePolicy(every=every),
            last_saved_blocks=self._autosave.last_saved_blocks)

    async def save_scene_state(self) -> Path | None:
        """把这一场存进 sidecar（§5）：**未撤销**的转录 + 虚拟钟 + 块数 + 人事状态。

        存的是**运行快照**（scenestore 的 sidecar，`<场景文件>.runtime.json`），绝不改动
        场景配置本体（§5/§9.3：场景文件即存档的配置部分）。没有场景路径（纯内存引擎）
        或写盘出错 → 记诊断、返回 None，**绝不让存档失败带垮正在跑的对话**。
        """
        path = self._scene_file()
        if path is None:
            self._save_error = "本引擎没有场景文件路径，无法存档"
            return None
        try:
            st = await self._snapshot()
            # 人事状态（§5「角色名单与各角色进场时间」的运行期那一半）：在场名单 +
            # 禁言剩余（None = 永久禁言）+ 进场基线（转录 turn 制）。
            meta: dict = {"active": list(self._active),
                          "muted": {n: left for n, left in self._mute.items()}}
            # I2(最终评审)：进场基线也要落盘——否则续演后中途进场者（基线 > 0）又变回
            # 「全程可见」，会读到进场前的历史（他的私有记忆与公共提示词都不该凭空多出
            # 这段）。**只记非零项**且空表不写键：零 = 默认值，存档里塞满 0 只会让旧存档
            # 与既有断言（meta 逐字节相等）平白多出一堆 0。
            entry = {n: int(v) for n, v in self._entry_round.items()
                     if int(v or 0) > 0}
            if entry:
                meta["entry_round"] = entry
            bundle = scenestore_mod.RuntimeBundle(
                transcript=[dict(m) for m in graph_mod._live_messages(st)],
                clock_seconds=self._clock_now,
                blocks=int(st.get("blocks", 0) or 0),
                meta=meta)
            saved = scenestore_mod.save_runtime(path, bundle)
            self._save_error = None
            return saved
        except Exception as exc:                 # IO/权限/序列化任何失败都不进场景
            self._save_error = f"{type(exc).__name__}: {exc}"
            logger.exception("保存场景失败：%s", path)
            return None

    async def save_scene_file(self) -> Path | None:
        """把**当前阵容**写回场景文件（§3.1：运行期增删要落到场景里），返回路径或 None。

        场景文件 = 场景的**配置本体**（§5）：这里只替换它的 `characters`，其余字段一字
        不动。做法是**重新读盘**再只换阵容，而不是把活场景整个 dump 回去——理由：
          · 磁盘上的文件是用户/编辑器写下的权威配置（GUI 的「配置场景」直接落盘），
            引擎只拥有「谁在场」这一半，绝不越权把别处的改动覆盖掉；
          · 场景 agent 的运行期自改（工具/钩子改描述/背景/名）只活在活对象里（见
            scene_state 的「活值」），要长久保存由用户经「配置场景」显式写回。

        写进去的是**当前在场者**（按演员表序，携带各自 entered_round/entered_at）：场景
        文件 schema 没有 active 标志位，故不保留已移出者——他们仍在转录与私有记忆里
        （记录不删），只是不再是这一场的阵容。

        没有场景路径 / 读不出 / 写盘失败 → 记诊断、返回 None（绝不带垮正在跑的对话）。
        """
        path = self._scene_file()
        if path is None:
            self._save_error = "本引擎没有场景文件路径，无法写回场景"
            return None
        try:
            scene = load_scene(path)                 # 以**磁盘上的配置**为准
            scene.characters = self._live_cast_records()
            save_scene(scene, path)
            self._save_error = None
            return path
        except Exception as exc:                     # IO/权限/坏文件都不进场景
            self._save_error = f"{type(exc).__name__}: {exc}"
            logger.exception("写回场景文件失败：%s", path)
            return None

    def _live_cast_records(self) -> list[SceneCastMember]:
        """当前**在场**者的演员表记录（按演员表序），供写回场景文件。

        入场信息取活场景里那一行的现值（add_character 每次进场都会就地更新它）；名字
        在记录里查不到（理论上不会发生）时补一行零记录，绝不丢人。
        """
        by_name = {m.name: m for m in self.scene.characters}
        return [by_name[name].model_copy() if name in by_name else SceneCastMember(name=name)
                for name in self._active]

    async def restore_scene_state(self) -> bool:
        """续演（§5）：把 sidecar 里的转录**追加**回共享态，返回是否真的恢复了什么。

        追加而非覆盖：新引擎开场后可能已经有开场行/人事播报，接着往下演才自然。消息
        保留原 id（`_next_id` 取全量最大 +1，故新行自然续在旧 id 之后）。blocks/turn
        取「现有与存档的较大者」，避免把已推进的世界钟往回拨。人事状态（meta）**尽力**
        恢复：只认本引擎装载了卡的 name（缺卡的名字静默忽略——卡没装载就不是这一场的
        人）。没有存档/存档为空 → False（调用方据此按全新开场处理）。

        I1(最终评审)：**按 id 去重**。生产路径是 GUI 先 open_scene()（写了 id 0 的开场行）
        再续演——不去重就会落出两条 id 0（撤销一条两条一起消失，重开还会与第一步 autoplay
        抢跑）。规则：存档里 id 已在实时态里的一律**不再追加**（实时态那份为准，用户眼前
        的开场行不被旧档覆盖）；存档**内部**的重复 id 重新编号到当前最大 id 之后，保证
        落地后 id 全局唯一，且 `_next_id`（全量最大 +1）的连续性不破。
        """
        path = self._scene_file()
        if path is None or not scenestore_mod.has_runtime(path):
            return False
        try:
            bundle = scenestore_mod.load_runtime(path)
            if not bundle.transcript:
                return False
            graph = await self._ensure_graph()
            st = await self._snapshot()
            existing = list(st.get("messages") or [])
            kept, dropped, renumbered = self._dedupe_restored(existing, bundle.transcript)
            if not kept:
                return False                     # 存档里的行全都在实时态里了 → 没恢复什么
            blocks = max(int(st.get("blocks", 0) or 0), int(bundle.blocks or 0))
            merged = existing + kept
            turn = max([int(m.get("turn", 0) or 0) for m in merged] or [0])
            self._adopt_legacy_space(merged)     # C1：旧档的空间键与历史保持一致
            await graph.aupdate_state(self._cfg(), {"messages": kept,
                                                    "blocks": blocks, "turn": turn},
                                      as_node=START)
            self._msg_count = len(merged)
            self._blocks = blocks
            self._turn = turn
            self._restore_cast_meta(bundle.meta)
            self._save_error = None
            if dropped or renumbered:
                logger.info("续演去重：丢弃 %d 条已在实时态的行，重编号 %d 条",
                            dropped, renumbered)
            return True
        except Exception as exc:                 # 坏存档/半截文件一律当作"没恢复"
            self._save_error = f"{type(exc).__name__}: {exc}"
            logger.exception("读取场景存档失败：%s", path)
            return False

    @staticmethod
    def _dedupe_restored(existing: list[dict],
                         transcript: list[dict]) -> tuple[list[dict], int, int]:
        """（可追加的存档行, 丢弃条数, 重编号条数）：见 restore_scene_state 的 I1 说明。

        与实时态撞 id → 丢弃（实时态那份为准）；存档内部自撞 id（同一份档被追加过两次
        之类）→ 重新编号到「当前见过的最大 id + 1」，逐条递增。**不改原 dict**。
        """
        live_ids = {int(m.get("id") or 0) for m in existing}
        max_id = max(live_ids or {0})
        seen = set(live_ids)                 # 「已占用」= 实时态 + 本次已保留的
        kept: list[dict] = []
        dropped = renumbered = 0
        for raw in transcript:
            msg = dict(raw)
            mid = int(msg.get("id") or 0)
            if mid in live_ids:              # 实时态里已有同 id 的行 → 不重复追加
                dropped += 1
                continue
            if mid in seen:                  # 存档内部自撞 → 续到当前最大 id 之后
                max_id += 1
                mid = max_id
                msg["id"] = mid
                renumbered += 1
            kept.append(msg)
            seen.add(mid)
            max_id = max(max_id, mid)
        return kept, dropped, renumbered

    def _adopt_legacy_space(self, messages: list[dict]) -> None:
        """C1 旧数据兜底：接受历史里已经存在的那个空间键（改名只改显示名）。

        本引擎的空间键在构造时捕获（C1 修复前落盘的行带的就是「当时」的场景名）。**正常
        的旧数据不需要走这条**：场景没改过名时历史里的 in_scene 与本引擎的键一字相同，
        照常过滤。只有「上一场改过名、随后存了档」的历史才会带旧键：此时若合并集里根本
        没有本引擎的键、且历史里的 in_scene **唯一**，就把它接受为空间键（等价于「改名
        只改显示名，空间轴沿用历史」）。多值/新旧混存时不猜——保持本引擎的键，绝不给两条
        不同空间的历史判成同一场（GUI 先 open_scene 写了新键的开场行、再续演这种混合场景
        即落在这一支：旧键的那些行照旧不可见，与修复前行为一致，不会更糟）。
        """
        keys = {m.get("in_scene") for m in messages if m.get("in_scene")}
        if not keys or self._scene_space in keys:
            return
        if len(keys) == 1:
            self._scene_space = next(iter(keys))
            if self._ctx is not None:
                self._ctx.space = self._scene_space
            logger.info("续演：接受历史里的空间键 %s 为在场轴", self._scene_space)

    def _restore_cast_meta(self, meta: dict) -> None:
        """把存档里的人员状态（在场/禁言/进场基线）尽力套回运行期名单（缺卡的名字忽略）。"""
        meta = meta or {}
        active = [str(n) for n in (meta.get("active") or []) if str(n) in self.cards]
        if active:
            self._active = active
            self._inactive = [n for n in self.scene.participants if n not in active]
            for name in active:
                self._ensure_dynamics(name)
        muted: dict[str, int | None] = {}
        for name, left in (meta.get("muted") or {}).items():
            if str(name) not in self.cards:
                continue
            if left is None:
                muted[str(name)] = None
            elif isinstance(left, (int, float)) and not isinstance(left, bool):
                muted[str(name)] = max(1, int(left))
        if muted:
            self._mute = muted
        # I2：进场基线（转录 turn 制）——续演后中途进场者仍旧看不到进场前的对话。
        entry = meta.get("entry_round") or {}
        for name, base in (entry.items() if isinstance(entry, dict) else []):
            if str(name) not in self.cards:
                continue
            if isinstance(base, bool) or not isinstance(base, (int, float)):
                continue
            self._entry_round[str(name)] = max(0, int(base))

    async def _maybe_autosave(self) -> None:
        """块末自动保存（§5）：到周期就静默存一次；没场景路径/没开启则什么都不做。

        记账点在 AutosaveTracker 内（到点即推进），故保存失败不会每块反复重试——节奏
        与「每 N 轮存一次」的设置一致，用户手动「保存场景」随时可补。

        写两份（§3.2「运行期的增删立即生效，并写回场景（保存场景 / 自动保存 / 关闭时）」）：
        先 sidecar（转录 + 虚拟钟 + 人事），再把**阵容**写回场景文件——这样自动保存周期
        内加/减的人不会在进程意外退出时丢掉。
        """
        if self._scene_file() is None:
            return
        if self._autosave.note(self._blocks):
            await self.save_scene_state()
            await self.save_scene_file()

    async def reset_scene_runtime(self) -> None:
        """重置场景（§3.4/§5）：清空**对话与思考上下文**，保留配置与在场角色。

        做法与「取消一切」对齐共享态的既有语义：
          · 把当前每一条消息都记进 retracted（视图层的行为与用户逐条撤销完全一致，
            消息本体与 id 都留着——界面仍能显示"这一场已重置"之前的痕迹）；
          · 清 think 只读边通道**与每名在场者的私有记忆回喂**（state.jsonl 截断 +
            impressions 的 jsonl/md 全删，见 CharacterMemory.clear；I3：next-think 回喂读
            的就是这两处，只清边通道等于没清——GUI 的 think 面板与私有回喂都不该跨过重置）；
          · 数值动态**重新播种**（Dynamics 全新、turns_since_spoke 归零，按当前在场名单）；
          · 叙述判据/钩子事件缓冲归零（叙述状态从"刚开过口"起算的那几个计数器）；
          · **不删 sidecar**（§5：重置是清运行上下文，不是删存档；要不要丢弃存档由用户
            在界面上的「保存场景」/库管理决定）。
        静默事件流（_implicit_events）随重置一并清空：它是这一场的运行痕迹。
        """
        graph = await self._ensure_graph()
        st = await self._snapshot()
        retracted = list(st.get("retracted") or [])
        for msg in (st.get("messages") or []):
            mid = msg.get("id")
            if mid is not None and mid not in retracted:
                retracted.append(int(mid))
        await graph.aupdate_state(self._cfg(), {"retracted": retracted}, as_node=START)
        self._retracted = retracted
        self._think_log.clear()              # 原地清（graph 持有的就是这个 list）
        # I3(最终评审)：**私有**的 next-think 回喂也要清。think/speak 每次都读本人的
        # state.jsonl 尾部与 impressions（jsonl 为准、md 是人读渲染）拼进提示词——
        # 只清 _think_log（只读边通道）不够：那些文件还在，重置后的角色照样带着上一场的
        # 私有状态开口（dynamic_states() 也仍旧返回旧值）。对**每个在场者**清一次。
        for name in self._active:
            if name in self.cards:
                memory_mod.CharacterMemory(self.run_root, name).clear()
        self._dynamics = Dynamics(
            self._active, params=self.dynamics_params,
            emotion_rates={name: self.cards[name].emotion_decay_rate
                           for name in self._active if name in self.cards})
        self._blocks_since_narration = 0
        self._narration_ids = []
        self._last_nudge = None
        self._narration_error = None
        self._hook_error = None
        self._tool_error = None
        self._implicit_events.clear()
        self._hook_lines.clear()
        self._scene_changes.clear()          # 自改变更流也是这一场的运行痕迹

    @staticmethod
    def _rollback_target(messages: list[dict], mid: int) -> tuple[list[int], int | None]:
        """撤回目标 →（该一并撤回的 id 列表, 被撤那条的 turn）。turn=None 表示**没找到该 id**。

        「同一场景」按消息的 in_scene（空间键）比对：历史里可能出现别的空间键（续演旧档，
        见 _adopt_legacy_space），撤回只截断**这一段**，绝不把另一场的正文一并作废。
        id 不存在时退化为只撤它自己（幂等、绝不抛），且 turn 返回 None——调用方据此
        **不做任何按轮次的截断**（凭空一个 id 不该把谁的私有记忆清空）。
        """
        target = next((m for m in messages if int(m.get("id") or 0) == mid), None)
        if target is None:
            return [mid], None
        space = target.get("in_scene")
        ids = sorted({int(m.get("id") or 0) for m in messages
                      if int(m.get("id") or 0) >= mid and m.get("in_scene") == space})
        return (ids or [mid]), int(target.get("turn", 0) or 0)

    async def retract(self, message_id: int) -> list[int]:
        """**截断式**撤回（§6.1）：撤回该条**及其之后同一场景内的所有消息**，返回被撤的 id。

        旧语义只剔除被点的那一行（§6.1 用户抱怨）：撤销一条叙述之后，由它衍生的一整段
        下文照旧留在上下文里，角色仍带着「叙述发生过」的前提继续演。现在撤回 = 回溯到
        该推进节点：``id ≥ 目标`` 且 ``in_scene`` 与目标相同的消息全部作废（消息本体仍
        留在共享态，界面要能显示"这段作废了"）。

        一切喂给模型的东西必须同意「那段下文从没存在过」，故一次撤回要同时对齐四处：
          · 共享态 retracted：整段 id，去重且**单调**（排序后存，重复撤回幂等）；
          · think 只读边通道：丢掉 **turn ≥ 被撤消息的 turn** 的条目——块内 think 与本块
            产出的消息同 turn，故该轮及其后写下的私有解读正是"看过被丢内容"的那批；
          · 私有记忆（state.jsonl / impressions）：按轮次截断（truncate_after_turn 传
            cut−1：该 API 的语义是"保留 ≤ cut"，而 cut 轮本身的条目同样要丢）；
          · 视图/锚点/复读判定/动态 heard 取样：本就只读 `_live_messages`，无需另做。

        **不倒拨世界钟**（决策）：blocks / turn / _blocks_since_narration / 数值动态一律
        不回退——撤回的是"说过的话"，不是"过掉的时间"；冷却若被撤回重置，一次撤回就等于
        白送一次立即推进。被丢弃的下文既已不在任何视图里，也不会因这些计时器而复活。
        """
        graph = await self._ensure_graph()
        st = await self._snapshot()
        mid = int(message_id)
        ids, cut_turn = self._rollback_target(st.get("messages") or [], mid)
        retracted = sorted(set(list(st.get("retracted") or []) + ids))
        await graph.aupdate_state(self._cfg(), {"retracted": retracted}, as_node=START)
        self._retracted = retracted
        if cut_turn is not None:                 # 找不到该 id：只有 id 记账，不截断任何东西
            self._truncate_think_log(cut_turn)
            self._truncate_memory(cut_turn)
        return ids

    def _truncate_think_log(self, cut_turn: int) -> None:
        """think 只读边通道按轮次裁掉被回溯区间（**原地**改：图持有的就是这个 list）。"""
        self._think_log[:] = [e for e in self._think_log
                              if int(e.get("turn", 0) or 0) < cut_turn]

    def _truncate_memory(self, cut_turn: int) -> None:
        """把每名在场/离场者的私有记忆截断到 cut_turn 之前（见 CharacterMemory）。

        对**在册的全部人**截（不只是当前在场者）：被丢弃的那段下文里可能有人已经离场，
        他的私有解读同样建立在"那段发生过"之上，下一场再进场时不该带着它。
        """
        for name in dict.fromkeys([*self._active, *self._inactive]):
            memory_mod.CharacterMemory(self.run_root, name).truncate_after_turn(cut_turn - 1)

    async def edit_narration(self, message_id: int, text: str) -> dict:
        """改写一条叙述 = **截断到该节点** + 落一条新叙述行（新 id）。

        不做原地改字：历史是追加式的，原地改会让"当时的模型看到过什么"失真；截断旧行
        及其后全部下文、再追加新行，等价于"用户改了口径，从这一刻起重新往后演"。
        """
        await self.retract(message_id)
        st = await self._snapshot()
        live = graph_mod._live_messages(st)
        tail = live[-1] if live else None
        msg = {"id": graph_mod._next_id(st.get("messages") or []),
               "speaker": "场景", "speaker_type": "narrator",
               "content": (text or "").strip(),
               "in_scene": (tail.get("in_scene") if tail else None)
                           or self._scene_space,
               "turn": st.get("turn", 0)}
        await self._post_line(msg)
        self._blocks_since_narration = 0
        self._narration_ids.append(msg["id"])
        return msg

    def narration_state(self) -> dict:
        """叙述者状态快照（供 GUI/指标展示，sync、只读镜像）：
          auto            自动推进开关
          blocks_since    距上次叙述的块数
          last_report     最近一次判据（{score/repeat/stall/silence/boundary/
                          blocked_by_cooldown/reasons}，未判过为 None）
          narration_ids   本场产出过的叙述 id（含已被撤销的）
          retracted       已撤销 id（共享态镜像）
          activity        推进活跃度 0..1（§6.2；GUI 据此镜像「场景推进」档位）
        """
        rep = self._last_nudge
        return {
            "auto": self._auto_narrate,
            "blocks_since": self._blocks_since_narration,
            "last_report": (scenarist_mod.asdict(rep) if rep is not None else None),
            "narration_ids": list(self._narration_ids),
            "retracted": list(self._retracted),
            "activity": self._narrate_activity,
        }

    def _apply_scene_pressure(self) -> None:
        """本块开场的场景压力：仅存在 time 硬边界时算 clamp((clock-start)/(boundary-start))，
        否则恒 0；worker 经 set_clock_now 注入当前虚拟钟，未注入则取开场时刻（进度 0）。"""
        self._dynamics.set_scene_pressure(self._clock_progress() or 0.0)

    def _advance_dynamics(self, n_before: int, think_before: int, st: dict) -> None:
        """step 末的数值动态推进（§7.4 state_{t+1}=state_t+effect(chunk_t)）：

        (a) 把本块 think 日志新增条目逐听众 observe() 折进 Dynamics——听的是本块
            开始时已存在的最新消息（heard content 逐听众、address 取本块前尾条）；
            被这句点名/提及的听众在此记下一笔**欠答义务**（pending_reply）；
        (b) 本块有角色产出 → mark_spoke(说话人)（他答了 → 其义务清零），否则（静默块）
            tick_silence()——沉默压力随之在久未开口者身上累积，且**仍欠答者的义务
            每轮翻倍**（未答越久，bid 越高，直到拿回话筒作答）。
        发言仲裁发生在**下一块**的 deliberate（bidder 闭包读 Dynamics）——一块的
        感知/演化延迟，正是"听完→想→开口"的自然节奏。
        """
        msgs = st.get("messages") or []
        spoke = next((m["speaker"] for m in msgs[n_before:]
                      if m.get("speaker_type") == "character"), None)
        # M2(最终评审)：听的那条必须从**未撤销**的消息里取（被撤销 = 这行没发生过，不能
        # 再抬任何人的邻接/欠答）；且**绝不**把共享尾条的内容当作某听众的「听到」兜底——
        # 他自己的 think 条目里没有 heard，说明他这一眼什么都没看到（进场前/全不可见），
        # 拿共享尾条去填等于用他无权看的句子给他播私有状态。
        live = graph_mod._live_messages(st)
        heard = live[-1] if live else None
        heard_addr = heard.get("address") if heard else None
        for entry in self._think_log[think_before:]:
            result = entry.get("result") or {}
            self._dynamics.observe(
                entry["speaker"], result,
                addressed_to=heard_addr,
                content=entry.get("heard"))
        if spoke is not None:
            self._dynamics.mark_spoke(spoke)
        else:
            self._dynamics.tick_silence()

    def set_clock_now(self, seconds: int) -> None:
        """worker 每块步进前注入当前虚拟钟秒数 → 场景压力随到点临近演化。"""
        self._clock_now = int(seconds)

    # ------------------------------------------------ 可插拔演员表（§3.3） ----
    # 运行期增减角色/禁言/解禁。全部 async、幂等、**收束后照常可用**（收束只冻结世界
    # 推进，不冻结人事：收束后仍可把某人移出以便存档/界面收尾），且都只经共享态落行，
    # 不新增任何共享态键、绝不改 scene.characters 已有行的名字（记录只增不删）。
    def active_names(self) -> list[str]:
        """当前**在场**（可插拔）的角色名，按演员表原序。

        sync、只读镜像（服务 GUI/测试/内部遍历 alike）；权威名单由本引擎持有——被移出
        者进 inactive（历史与记忆保留），不再出现在任何需要「当下谁在场」的地方：
        扇出/竞价（经 speakable_names）、左右栏（dynamics_snapshot/dynamic_states）。
        """
        return list(self._active)

    def speakable_names(self) -> list[str]:
        """本块**可参与**的名单 = 在场且未被禁言者（§4.1② 角色事件）。

        作为 graph 的 cast_provider：禁言者连 think 都不发（不烧 token），deliberate
        更不会给他 bid —— 话筒自然落到别人手上；解禁后立刻回到名单。
        """
        return [n for n in self._active if n not in self._mute]

    def cast_state(self) -> dict:
        """演员表运行期状态快照（供 GUI/worker 的 sig_cast）：
          active   当前在场（按演员表原序）
          inactive 已移出但记录/历史保留者（按移出顺序）
          muted    {name: 剩余块数}；None = 永久禁言（只含在场者；解禁/离场即移除）
        """
        return {
            "active": list(self._active),
            "inactive": list(self._inactive),
            "muted": {n: left for n, left in self._mute.items() if n in self._active},
        }

    def pending_cast_changes(self) -> list[dict]:
        """尚未到期的延时角色动作（§4.1② 高级移入移出）→ 可序列化 dict 列表。

        条目 = {character_name, action, turns, fire_after_rounds, notify, notify_text,
        visible}；每 step() 一块扣 1，扣到 0 即执行并从队列移除（hooks.tick 记账）。
        """
        return [{"character_name": p.character_name, "action": p.action, "turns": p.turns,
                 "fire_after_rounds": p.fire_after_rounds, "notify": list(p.notify),
                 "notify_text": p.notify_text, "visible": p.visible}
                for p in self._pending_cast]

    def _require_card(self, name: str) -> str:
        """名字 → 规范名；卡没装载就**当场从角色库按需装载**（§3.2），库里也没有才抛错。

        构造期装载的那批卡只是起步：任何库中角色都能在**任何时候**加入任何场景——先在
        已装载的卡里找，找不到就去角色库按名装载（`_load_card_from_library`）。找不到
        才是真没有：抛中文 ValueError（可直接给用户看）。
        """
        clean = str(name or "").strip()
        if clean not in self.cards:
            loaded = self._load_card_from_library(clean)
            if loaded is None:
                raise ValueError(f"角色库里没有找到《{clean or '（空名）'}》的角色卡。")
            return loaded
        return clean

    def _ensure_dynamics(self, name: str) -> None:
        """给新进场者补一份数值状态（老面孔离场再入场则沿用其原状态，不重置）。

        Dynamics 的状态表在构造期按初始名单建；运行期新增的人必须补进同一张表，
        否则 bid()/observe()/mark_spoke() 会 KeyError。
        """
        if name not in self._dynamics.states:
            self._dynamics.states[name] = CharDynamics()
        self._dynamics.emotion_rates.setdefault(
            name, float(self.cards[name].emotion_decay_rate))

    def _entry_round_of(self, name: str) -> int:
        """进场基线（转录 turn 制）：turn < 它的消息对该角色不可见（graph 经 ctx 读）。

        注意与持久化字段 SceneCastMember.entered_round 的**单位差异**：后者记世界**块钟**
        （给界面/存档看的「第几块进场的」），本条读的是转录 turn 基线（保证「进场前一句
        都看不到」这条硬语义在静默块把两者拉开时依然成立）。未知名字 → 0（全程可见）。
        """
        return int(self._entry_round.get(name, 0))

    def _entry_clock_text(self) -> str:
        """进场时刻的 HH:MM 文本；引擎不知道当前钟（worker 未注入）时返回空串。"""
        return sc.format_hhmm(self._clock_now) if self._clock_now is not None else ""

    async def _post_cast_line(self, content: str, *, knows: list[str] | None = None,
                              address: str | None = None) -> dict:
        """落一条场景叙述行（人事播报用）：speaker="场景"、speaker_type="narrator"。

        与 narrate/inject 同一落法（in_scene 沿用尾条、id 取全量最大 +1、turn 取当前
        turn 镜像——快照随手刷新，取到的就是当下那一格）。knows 给出时该行只对名单内
        的人可见（§4 通知：通知对象可多选），None = 全员可见。
        """
        st = await self._snapshot()
        msgs = st.get("messages") or []
        prev = msgs[-1] if msgs else None
        msg = {"id": graph_mod._next_id(msgs),
               "speaker": "场景", "speaker_type": "narrator", "content": content,
               "in_scene": (prev.get("in_scene") if prev else None) or self._scene_space,
               "turn": self._turn}
        if knows is not None:
            msg["knows"] = list(knows)
        if address:
            msg["address"] = address
        await self._post_line(msg)
        return msg

    async def _notify_cast_line(self, name: str, notify: list[str] | None,
                                notify_text: str, visible: bool,
                                *, leaving: bool = False) -> dict | None:
        """人事通知行（§4.1② advanced 移入/移出的「通知」）：通知对象非空才落一行。

        内容取 notify_text，留空时给一句默认（进场「（X 来了。）」/ 离场「（X 走了。）」）；
        可见性同 §4.2：implicit（visible=False）**连通知行也不落**（隐式事件整体不上屏）。
        通知行的 knows = 通知对象名单，即只有被通知者看得见这条（作者可选自己/在场者/场景）。
        """
        targets = [str(n).strip() for n in (notify or []) if str(n).strip()]
        if not targets or not visible:
            return None
        text = (notify_text or "").strip() or (
            f"（{name}走了。）" if leaving else f"（{name}来了。）")
        return await self._post_cast_line(text, knows=targets, address="、".join(targets))

    async def add_character(self, name: str, *, notify: list[str] | None = None,
                            notify_text: str = "", visible: bool = True) -> dict | None:
        """让一名角色进场（§3.3）：进在场名单 + 落一条可见播报行，返回该行（无行则 None）。

        · name 要有角色卡：已装载的直接用，没有的**从角色库按需装载**（§3.2）——因此
          任何库中角色都能在任何时刻加入任何场景；库里也没有 → ValueError（中文原因）；
        · 已在场 → 幂等空操作（hook 重复触发不重复播报）；
        · 记录：scene.characters 里没有就**追加**一行 SceneCastMember，已有则就地更新
          入场信息（entered_at = 当前钟 HH:MM，引擎不知道钟时留空；entered_round = 当前
          世界块钟）；
        · 进场基线 = 当前转录 turn → 他**看不到进场前的任何对话**（公共信息由 S4b 的提示词
          场景段供给），且其私有记忆里的旧状态保留（那是他自己的东西）；
        · visible：True 落「（X 走进了场景。）」；False（隐式）不落任何行、状态照改；
        · notify 非空时另落一条**只发给这些名字**的通知行（内容 notify_text 或默认）。
        """
        name = self._require_card(name)
        if name in self._active:
            return None
        await self._snapshot()                     # 刷 _blocks/_turn 镜像（下列取值据此）
        member = next((m for m in self.scene.characters if m.name == name), None)
        if member is None:
            member = SceneCastMember(name=name)
            self.scene.characters.append(member)
        member.entered_at = self._entry_clock_text()
        member.entered_round = self._blocks
        self._entry_round[name] = self._turn
        self._inactive = [n for n in self._inactive if n != name]
        self._active.append(name)
        self._ensure_dynamics(name)
        line = await self._post_cast_line(f"（{name}走进了场景。）") if visible else None
        await self._notify_cast_line(name, notify, notify_text, visible)
        return line

    async def remove_character(self, name: str, *, notify: list[str] | None = None,
                               notify_text: str = "", visible: bool = True) -> dict | None:
        """让一名角色离场（§3.3）：出在场名单 + 落一条可见播报行，返回该行（无行则 None）。

        **历史与私有记忆一律保留**（记录不删、转录不删、Dynamics 状态不重置），只是他
        不再 think、不再参与竞价、不再出现在左右栏；上下文里留下「谁何时离开了场景」
        这条事件行（visible=True 时）。禁言状态随离场清除（再入场是干净状态）。
        不在场 → 幂等空操作。name 无卡 → ValueError（同 add_character）。
        """
        name = self._require_card(name)
        if name not in self._active:
            return None
        await self._snapshot()
        self._active = [n for n in self._active if n != name]
        if name not in self._inactive:
            self._inactive.append(name)
        self._mute.pop(name, None)
        line = await self._post_cast_line(f"（{name}离开了场景。）") if visible else None
        await self._notify_cast_line(name, notify, notify_text, visible, leaving=True)
        return line

    async def mute_character(self, name: str, turns: int = 0) -> None:
        """禁言（§4.1②）：turns > 0 → 禁言这么多**块**（每 step 一块自动解除）；
        turns <= 0 → 永久禁言，直到 unmute_character()。

        被禁言者不进扇出、不参与竞价（没有 bid），直到解禁或（临时禁言）到期。可对
        不在场者调用（状态记下，等他进场时生效）；名字无卡 → ValueError。
        """
        name = self._require_card(name)
        n = int(turns)
        self._mute[name] = n if n > 0 else None

    async def unmute_character(self, name: str) -> None:
        """解禁（§4.1②）：撤掉该角色的禁言状态（无论临时还是永久、在场与否）。"""
        name = self._require_card(name)
        self._mute.pop(name, None)

    async def schedule_cast_change(self, character_name: str, action: str,
                                   fire_after_rounds: int,
                                   notify: list[str] | None = None,
                                   notify_text: str = "",
                                   visible: bool = True,
                                   turns: int = 0) -> dict:
        """预约一次**延时**角色动作（§2.1④ 高级移入/移出）：fire_after_rounds 块后执行。

        action ∈ hooks.CharacterAction（add/remove/mute_turns/mute/unmute）；turns 只在
        mute_turns 时用（**相对 S4a 规格的增补**：延时禁用 N 块必须能带上轮数，否则
        mute_turns 无从表达）。fire_after_rounds <= 0 视同 0（下一块末即执行）。
        每次 step() 一块扣 1（hooks.tick），到期即调对应方法执行——因此
        schedule(fire_after_rounds=2) 恰在第 2 次 step 的块末触发、第 1 次不动。
        返回入队的 dict（供日志/回执）；角色名无卡 → ValueError（当场拦下，不进队列）。
        """
        name = self._require_card(character_name)
        if action not in _CAST_ACTIONS:
            raise ValueError(
                f"未知的角色动作：{action}（应为 add/remove/mute_turns/mute/unmute）。")
        item = hooks_mod.PendingCharacterAction(
            character_name=name, action=action, turns=max(0, int(turns)),
            fire_after_rounds=max(0, int(fire_after_rounds)),
            notify=[str(n) for n in (notify or [])], notify_text=str(notify_text or ""),
            visible=bool(visible))
        self._pending_cast.append(item)
        return self.pending_cast_changes()[-1]

    def _tick_mutes(self) -> None:
        """块末把临时禁言各扣 1 块，扣到 0 即自动解禁；永久禁言（None）不动。

        扣减放在「执行本块延时动作之前」：本块新下的禁言因此能完整挡满 N 块。

        M3(最终评审)：**只扣在场者**。禁言可以对不在场的人下（§4.1② 的用法之一就是
        「等他进场再生效」）——若他缺席期间块钟照样扣，等真正进场时禁言早已归零，
        「禁言一个不在场的人」永远不生效。缺席 = 世界没对他推进，他的计时也不该走。
        """
        for name, left in list(self._mute.items()):
            if left is None or name not in self._active:
                continue
            remain = int(left) - 1
            if remain <= 0:
                self._mute.pop(name, None)
            else:
                self._mute[name] = remain

    async def _apply_cast_action(self, item: hooks_mod.PendingCharacterAction) -> dict | None:
        """执行一条到期的延时角色动作（dispatch 到 add/remove/mute/unmute）。"""
        action = item.action
        if action == "add":
            return await self.add_character(item.character_name, notify=item.notify,
                                            notify_text=item.notify_text,
                                            visible=item.visible)
        if action == "remove":
            return await self.remove_character(item.character_name, notify=item.notify,
                                               notify_text=item.notify_text,
                                               visible=item.visible)
        if action == "mute_turns":
            return await self.mute_character(item.character_name, item.turns or 1)
        if action == "mute":
            return await self.mute_character(item.character_name, 0)
        if action == "unmute":
            return await self.unmute_character(item.character_name)
        raise ValueError(f"未知的角色动作：{action}")

    async def _fire_pending_cast(self) -> list[dict]:
        """块末推进延时角色动作一格：到期者执行，返回它们落下的消息行（供事件流）。

        单条失败（典型：卡已不在/名字奇怪）绝不炸掉本块：记进 _cast_error 继续下一条
        ——场景照常演，界面能从诊断里看到哪条没生效。
        """
        fired, self._pending_cast = hooks_mod.tick(self._pending_cast, 1)
        lines: list[dict] = []
        for item in fired:
            try:
                line = await self._apply_cast_action(item)
                if line is not None:
                    lines.append(line)
            except Exception as exc:
                self._cast_error = f"{type(exc).__name__}: {exc}"
        return lines

    def dynamics_snapshot(self) -> dict[str, dict]:
        """数值动态快照 {name: {turns_since_spoke/arousal/.../bid}}（供 GUI/测试展示）。

        每个角色附带其**当前数值 bid**（w1…w7 权重 + 沉默压力 + 欠答义务 + recency 惩罚，
        与仲裁读同一 Dynamics 源）——GUI 左/右栏据此实时展示「谁当下最可能拿话筒」；
        欠答项（pending_reply/turns_pending）另供右栏「待回应压力」行显示「第 N 轮未答」。
        观察/竞价纯确定性、零 IO，可安全在 loop 线程多次读取。

        名单 = **在场者**（§3.3）：被移出者不再出现在左/右栏（其 Dynamics 状态与历史
        仍在，只是不再展示）；被禁言者仍在名单里（要显示「禁言中」），只是不参与竞价
        ——故这里含他们、bid 照算，谁拿话筒由 speakable_names 决定。
        """
        full = self._dynamics.snapshot()
        snap: dict[str, dict] = {}
        for name in self.active_names():
            snap[name] = dict(full.get(name) or {})
            snap[name]["bid"] = self._dynamics.bid(name, self.cards[name].weights)
        return snap

    async def inject(self, content: str) -> None:
        """导演插话（世界事件，区别于人类 say）。closed 时早退；否则只追加一行，
        不增减虚拟时间（真实流速钟由 worker 持有，角色开口/思考/插话都不计时间）。
        """
        graph = await self._ensure_graph()
        vals = await self._snapshot()
        if vals.get("closed"):
            return                              # 已收束：导演插话不再进场
        msgs = vals.get("messages", [])
        prev = msgs[-1] if msgs else None
        nid = graph_mod._next_id(msgs)      # 全量最大 id+1：撤销过的 id 绝不复用
        msg = {"id": nid, "speaker": "导演", "speaker_type": "director",
               "content": content,
               "in_scene": (prev.get("in_scene") if prev else None) or self._scene_space,
               # M1(最终评审)：turn 取**当前 turn 计数器**（快照镜像），不沿用尾条的 turn
               # ——尾条可能是「本块之前」产出的，沿用它会把这条插话打上过期的轮号，刚
               # 进场者（基线 = 当前 turn）于是看不到它，仿佛一句当时无人存在的独白。
               "turn": self._turn}
        await self._post_line(msg)

    async def say(self, content: str, name: str = "你") -> None:
        """人类插话：追加一条 HUMAN 消息（speaker_type="human"，区别于导演 inject）。

        这是桌面端「真人在场说话」的入口——角色下一块 think 会把这条消息当最新块
        感知并回应。消息落在当前 in_scene（尾部消息的 in_scene，否则场景名）、
        id = 尾部 id+1、turn 沿用当前值；经 aupdate_state as_node=START 视同新输入
        从 START 进入，与 inject 相同的构图机制。返回值无事件——UI 层以随后 step 的
        块消息为准。插话不增减虚拟时间（time_hhmmss 由 worker 在派发时按当前钟补）。
        """
        graph = await self._ensure_graph()
        vals = await self._snapshot()
        if vals.get("closed"):
            return                              # 已收束：人类不再开口
        msgs = vals.get("messages", [])
        prev = msgs[-1] if msgs else None
        nid = graph_mod._next_id(msgs)      # 全量最大 id+1：撤销过的 id 绝不复用
        in_scene = (prev.get("in_scene") if prev else None) or self._scene_space
        msg = {"id": nid, "speaker": name, "speaker_type": "human",
               "content": content, "in_scene": in_scene,
               # M1(最终评审)：turn 取当前计数器，不沿用尾条（理由同 inject）。
               "turn": self._turn}
        await self._post_line(msg)

    async def _post_line(self, msg: dict) -> None:
        """人类 say / 导演 inject 的共同收尾：只把消息写进共享态（aupdate_state），
        不推进任何钟键、不判打烊——真实流速钟与到点收束都由 worker/GUI 负责。镜像同步。"""
        graph = await self._ensure_graph()
        await graph.aupdate_state(self._cfg(), {"messages": [msg]}, as_node=START)
        self._msg_count += 1            # 镜像同步（say/inject 前 _snapshot 已刷旧计数）

    async def speak_as_human(self, content: str) -> None:
        # FIX4(最终评审) 撤除占位：speak_as_human 是 say() 的后向兼容别名（缺省名"你"），
        # 保留以防旧调用方；真实入口为 say(content, name=...)。
        return await self.say(content)

    async def close_scene(self) -> None:
        graph = await self._ensure_graph()
        await graph.aupdate_state(self._cfg(), {"closed": True}, as_node=START)
        self._closed = True

    async def run_to_close(self, max_blocks: int = 60) -> list[dict]:
        events = await self.open_scene()
        for _ in range(max_blocks):
            if (await self._snapshot()).get("closed"):
                break
            events += await self.step(1)
            if self._closed:
                break
        return events

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def start_hhmm(self) -> str:
        """开始时刻 "HH:MM"（供 GUI/CLI 显示，由秒制权威换算）。"""
        return sc.format_hhmm(self.start_seconds)

    @property
    def boundary_hhmm(self) -> str | None:
        """打烊时刻 "HH:MM"；场景无 time 硬边界时 None。跨午夜按 +86400 换算后回卷显示。"""
        return sc.format_hhmm(self.boundary_seconds) if self.boundary_seconds is not None \
            else None

    async def export_transcript(self, path: Path) -> None:
        msgs = await self.messages()
        lines = [f"**[{m['id']}] {m['speaker']}**: {m['content']}\n" for m in msgs]
        Path(path).write_text("\n".join(lines), encoding="utf-8")

    def dynamic_states(self) -> dict[str, dict]:
        """每名参与者的**最近一次 think 自报**（只读、直读文件系统，不进共享态）。

        返回 {name: state.jsonl[-1]}；尚无任何 think 记录的参与者不出现在结果里
        （全场都没想过 → 空 dict）。条目 = 该听众最近一次私有状态
        (aroused/goal_progress/addressed/obligation_fulfilled + urge)，供 GUI 逐角色
        动态展示。与图并发写入同目录时只做尾部快照，不锁（MVP 语义）。
        名单 = 在场者（§3.3）：被移出者不再逐角色展示（其记忆文件仍在磁盘上）。
        """
        out: dict[str, dict] = {}
        for name in self.active_names():
            entries = memory_mod.CharacterMemory(self.run_root, name).load_state()
            if entries:
                out[name] = entries[-1]
        return out

    def think_log_tail(self, n: int = 200) -> list[dict]:
        """think 只读边通道尾部（缺省最近 200 条），返回浅拷贝。

        条目 = {seq, speaker, at, heard, turn, result:<ThinkResult 全字段含 urge>}；
        seq 为该引擎内单调序号（think_log 在 append 端按 _THINK_LOG_CAP 封顶裁剪，
        序号不受影响，GUI 据此增量派发）。仅供人类 UI 检视——think 是听众私有解读，
        绝不路由共享态、绝不给其它角色看。
        """
        return list(self._think_log[-n:])

    async def aclose(self) -> None:
        """关闭 sqlite 连接（仅 AsyncSqliteSaver 路径）并逐个关闭后端（若实现了
        close()）。须在引擎所在 loop 内调用；调用后再次调用 async 方法会重建 graph
        （重开同一 sqlite 文件）。既有关闭语义保留：saver 关闭先行，后端关闭兜底
        各自 no-op/异常吞掉。"""
        saver = self._saver
        self._graph = None
        self._saver = None
        self._store = None
        if isinstance(saver, AsyncSqliteSaver) and saver.conn is not None:
            try:
                await saver.conn.close()
            except Exception:
                pass
        # narrate 与 speak 同源（旧配置回退）时只关一次：同一对象重复 close 无益。
        backends = [self.think_backend, self.speak_backend]
        if self.narrate_backend is not None and self.narrate_backend is not self.speak_backend:
            backends.append(self.narrate_backend)
        for backend in backends:
            if backend is not None and hasattr(backend, "close"):
                try:
                    await backend.close()
                except Exception:
                    pass    # 后端关闭失败不阻塞引擎收尾

    def metrics(self) -> dict:
        """同步、非阻塞的运行指标：只读 __init__/每块镜像 + 后端计数，绝不反查
        async graph（AsyncSqliteSaver 不可从 sync 读取）。返回：
          uptime_s   引擎存活秒数（monotonic，1 位小数）
          messages   messages 条数（_snapshot 镜像）
          blocks     世界块钟（_snapshot 镜像）
          think/speak 各后端用量 {name: {calls, prompt_tokens, completion_tokens}}

        时钟条目不再由引擎给出——真实流速虚拟钟在 worker 侧连续计算（GUI 场景时间
        卡由 worker 的 clock_seconds/clock_hhmmss/rate/remaining_s 供应）。
        """
        return {
            "uptime_s": round(time.monotonic() - self._t0, 1),
            "messages": self._msg_count,
            "blocks": self._blocks,
            "think": self._usage_slice(self.think_backend),
            "speak": self._usage_slice(self.speak_backend),
        }

    @staticmethod
    def _usage_slice(backend) -> dict:
        """后端 → {名称: {calls/prompt_tokens/completion_tokens}}。鸭子类型容错：
        缺计数属性的外来后端按 0 计，不炸 sync metrics。"""
        bname = getattr(backend, "name", type(backend).__name__)
        return {bname: {
            "calls": getattr(backend, "calls", 0),
            "prompt_tokens": getattr(backend, "prompt_tokens", 0),
            "completion_tokens": getattr(backend, "completion_tokens", 0),
        }}
