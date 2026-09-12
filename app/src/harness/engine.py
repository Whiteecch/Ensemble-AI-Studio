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

信息库（《信息库系统_设计文档》§5/§6/§7）：
  · 引擎持有 `KnowledgeAccess`（`libraries_root` 缺省 None = **没有信息库**，那条路上
    一切与引进信息库之前**逐字节、逐调用次数相同**——老调用方零语感差异，硬约束）；
  · **轮换归档**（§5.1「一条戏一份副本」）：角色**第一次进入这一场**时（开场阵容全体，
    以及运行中首次进场的 `add_character`/钩子进场）若发现本场副本**已经结算过**（只认
    `settled`，丢弃过的也算），把整个副本目录重命名成 `library.settled-<N>` 归档、把这一场
    的取用记录 `recall.jsonl` 一并收进归档（§6.4 的私人回喂源按精确轮次回喂，新一场又从
    轮次 0 起算），再另建一份干净的——副本按（场景，角色）存，而结算状态是一票制先到先得，
    留着上一场的 settled 会把新一场的所得永久挡在门外。**未**结算的副本照旧复用不重置
    （§5.1 原本要保的东西）；**这一场自己的**账本即使结算过也不轮换（他离场又回来，这一场
    学到的东西必须还在）。轮换失败只记一条 warning 并照旧复用（理由见
    `_rotate_settled_copy`）；
  · **散场结算**（§7.4 引擎只准备、界面才决定）：`step` 里那一块跑完检测到 `closed`
    由假变真的跳变（唯一同时覆盖 GUI、CLI、块钟兜底三条路的位置）→ `prepare_settlement()`
    写散场总结、收本场所得，并把「待决」事件挂进本块事件流；界面/CLI 问完用户后把
    「保留 / 丢弃」回传 `apply_settlement()`（§7.2 纯总开关、§7.5 幂等）。引擎**绝不弹窗**。
  · **离场挂起**（§7.3）：`remove_character` 不弹窗、不做模型调用，只挂一条待结算提示。
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Sequence

import aiosqlite
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import START
from langgraph.store.memory import InMemoryStore

from . import bidding as bidding_mod
from . import dynamics as dynamics_mod
from . import graph as graph_mod
from . import events as ev
from . import hooks as hooks_mod
from . import i18n as i18n_mod
from . import knowledgesettle as settle_mod
from . import knowledgestore as knowledge_store_mod
from . import memory as memory_mod
from . import sceneclock as sc
from . import scenarist as scenarist_mod
from . import scenestore as scenestore_mod
from .backends import get_backend, make_deepseek
from .dynamics import CharDynamics, Dynamics, DynamicsParams
from .knowledgetools import (MAX_BODY_CHARS, RECALL_FILENAME, KnowledgeAccess,
                             TruncateReport)
from .loaders import (list_character_paths, load_bid_params, load_character_card,
                      load_models, load_scene, save_scene, scene_with_cast)
from .prompters import (TIME_CONDITION_RULE, build_narrate_messages,
                        build_summary_messages)
from .schemas import CharacterCard, HardBoundary, Message, Scene, SceneCastMember
from .visibility import render_view, view_for

logger = logging.getLogger(__name__)


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _clip(text: str, limit: int) -> str:
    """裁到 limit 字（超长补省略号）——播报/日志行专用，绝不用来裁叙述正文。"""
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


#: 用户的结算决定（§7.2 两个按钮）认得的说法：英文枚举 + 中文界面词 + 单键。
_KEEP_WORDS = frozenset({"keep", "保留", "k", "1"})
_DISCARD_WORDS = frozenset({"discard", "丢弃", "d", "2"})


def _settle_choice(raw: Any) -> str | None:
    """用户的决定 → `"keep"` / `"discard"`；认不出来 → None（调用方当作"没决定"）。

    **绝不猜**：认不出来时既不并进本体、也不标丢弃——两个方向都不可逆（§7.5 先到先得，
    标错了就翻不回来）。宁可让用户重来一次。
    """
    text = str(raw or "").strip().lower()
    if text in _KEEP_WORDS:
        return "keep"
    if text in _DISCARD_WORDS:
        return "discard"
    return None


def _max_message_id(state: dict) -> int:
    """共享态里当前最大的消息 id（空态 → 0）。

    供"离场边界"用（§7.3）：id 与 `_next_id` 同一口径（全量最大 + 1），故"此刻已经存在的
    最末一条"就是这个数——他走之后落的行一律更大，一条也漏不进来。
    """
    return max((int(m.get("id") or 0) for m in (state.get("messages") or [])), default=0)


def _with_hub_links(raw: str, keys: Sequence[str], *, limit: int = MAX_BODY_CHARS) -> str:
    """散场总结的正文 + **双链兜底**（§7.1）。

    提示词要求模型在末尾写上本场各条目的 `[[条目名]]`，但模型会漏——而枢纽条目的价值
    全在链接上：断了图就散一片（角色日后想回忆"那一夜"，从这一条进，顺着链接散开）。
    故漏掉的由引擎补；**已经写了的那条不重复补**（一条链写两遍是误导，也白占正文）。

    正文为空白时只留链接行：一条只剩链接的枢纽条目仍比没有强（图没断）。

    **链接必须活过写路径的正文截尾**：`remember` 对正文是截尾（`MAX_BODY_CHARS`），
    而链接挂在正文末尾——模型一啰嗦（提示词要求十行以内，上限却给了约五倍余量，偶发
    违规即中招）就会把刚补上的那一段整段截掉，本体里那条枢纽条目一个链接都没有，图
    散一片还全程无痕。故这里**先**把模型的正文压到"留得下链接"的长度（反正写路径也
    要截，晚截不如早截），再补链接。链接自己就超上限（极端：本场条目多到一行放不下）
    时按同一个上限先截，剩下的由写路径截——那种情形会有一条警告兜着（见
    `_write_scene_summary` 写完后的链接校验）。
    """
    text = (raw or "").strip()
    missing = [str(k) for k in keys if str(k) and f"[[{k}]]" not in text]
    if not missing:
        return text[:limit]
    links = " ".join(f"[[{key}]]" for key in missing)[:limit]
    budget = max(0, limit - len(links) - (1 if text else 0))    # 1 = 正文与链接之间的换行
    text = text[:budget].rstrip()
    return f"{text}\n{links}" if text else links


def _hub_links_missing(entry: Any, keys: Sequence[str]) -> list[str]:
    """写进去的那条枢纽条目**正文里少了的双链**（写完之后的验收，§7.1）。

    `_with_hub_links` 已经先把正文压到留得下链接，故正常情况下这里是空的；非空只有一种
    来路——链接自己就超了正文上限（本场条目多到一行放不下），写路径把它截掉了。这种残缺
    必须说出来：枢纽条目的价值全在链接上，缺一条就是图上少一条边，而无声的残缺最贵。
    """
    body = str(getattr(entry, "body", "") or "")
    return [str(k) for k in keys if str(k) and f"[[{k}]]" not in body]


def _summary_gist(gain: settle_mod.SceneGain, present: Sequence[str]) -> str:
    """枢纽条目的一行摘要（§7.1：在场者、我拿到了什么）——索引表里显示的就是这一行。

    正文是模型写的，摘要却必须由引擎给（它是**索引行**：格式固定、一行、可数），
    故这里只拼事实：在场的人 + 这一场记下了几条（带标题）。
    """
    parts: list[str] = []
    names = [str(n) for n in present if str(n)]
    if names:
        parts.append("在场：" + "、".join(names))
    titles = [e.title or e.key for e in [*gain.added, *gain.revised]]
    if titles:
        parts.append(f"这一场记下了 {len(titles)} 条：" + "、".join(titles))
    else:
        parts.append("这一场没记下什么。")
    return "；".join(parts)


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


#: 原因正文里的空白（含换行）折叠成单个空格：一条消息在视图里**只占一行**
#: （`[id] 说话人: 内容`），带换行的正文能凭空多出一条归属别人的台词行——用户可自由
#: 填写的文本一旦能断行，就等于改写别人的发言（"gender 那一课"的重演）。
_REASON_WHITESPACE_RE = re.compile(r"\s+")


def _sanitize_reason(reason: str, name: str) -> str:
    """把用户填的原因正文消毒成**一行、且不含当事人自己名字**的文本（§5.2）。

    两件事都是硬性的，不是文风：

      · **折平空白**：见 `_REASON_WHITESPACE_RE`——换行能伪造转录行，一条私密上下文
        绝不能变成两条；
      · **当事人的名字换成「你」**：这一行本来就是写给他看的第二人称（"你想离开这里"），
        而 `Dynamics.observe` 判"被提及"是朴素子串 `listener in content`——他自己名字
        出现在里面，就等于"他点名了自己"，当场欠下一笔 `pending_reply`（1.2 起、未答
        每块翻倍、进 bid 不乘任何权重、量级是本次倾向三项上界的十几倍）。用户按第三人称
        写「甲接到电话，要回城」是最自然的用法（同页的钩子条件就写「甲说要走」），引擎
        的措辞纪律只能保证自己拼的那半句，挡不住用户正文。换成「你」既保住意思，又让
        这件事只作为"在他心里有分量"（那一项在 `bid()` 里、有上界）起作用。

    残留边界：只替换**正文里**的名字；万一角色名恰好是那句固定前缀（"你想离开这里，
    因为："）的子串，`listener in content` 仍会命中。那种名字（"想""因为"）现实里不存在，
    不为它增加复杂度。
    """
    text = _REASON_WHITESPACE_RE.sub(" ", str(reason or "")).strip()
    owner = str(name or "").strip()
    if owner and owner in text:
        text = text.replace(owner, "你")
    return text


def _reason_note_text(reason: str, *, name: str, leaving: bool) -> str:
    """进离场原因那一行中文（§5.2）：交给**当事人自己**看的一句上下文。

    只陈述"他心里有这么一件事"，不写任何"你可以说出来"式的指令——说什么、说不说由模型
    决定（§5.3、铁律 4）；引擎的活儿只是让这件事在他心里有分量（那一项在 `bid()` 里）。

    两条措辞纪律（都不是文风问题）：

      · **第二人称、不写他的名字**：这一行是给他的，写了他的名字就等于他"被点名"，而
        `Dynamics.observe` 把"这一条听得的内容里提到我"记成一笔 `pending_reply`（1.2 起、
        未答每块翻倍、进 bid 不乘任何权重）——那会让"说不说"从倾向变成必然（铁律 4）。
        他自己写下的正文同样受这条约束，故先过 `_sanitize_reason`（换行折平 + 名字换
        "你"），不能只靠这句固定前缀；
      · **"想"而不是"就要"**：离场者的交付发生在**入队那一刻**（他还得等若干块才走，
        见 `schedule_cast_change`），万一这场戏在那之前收束、他其实没走成，"你想离开"
        仍然是真的（他确实动过这个念头），而"你就要走"就会在他记忆里留下一句假话。
    """
    verb = "你想离开这里" if leaving else "你来到了这里"
    return f"（{verb}，因为：{_sanitize_reason(reason, name)}。）"


@dataclass(frozen=True)
class _ReasonNote:
    """一条已经交到当事人手上的进离场原因（§5.2）——"他手上有件没说出口的事"的账目。

      · `delivered`：交付时的世界块号，窗口从这里起算（见 `_unsaid_reasons`）；
      · `text`：用户填的**原文**（"点谁的名"那一项按原文判，与消毒无关）；
      · `message_id`：那一行在转录里的 id——撤回它 = 这件事从没发生过，账要跟着销
        （见 `retract`）；叙述提示词也按这个 id 把私密行剔出去（见 `_narrator_live`）。
    """
    delivered: int
    text: str
    message_id: int


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
                 time_skip_enabled: bool = False,
                 time_skip_params: scenarist_mod.TimeSkipParams | dict | None = None,
                 language: str = i18n_mod.DEFAULT_LANGUAGE,
                 scene_file: Path | None = None,
                 autosave_every: int = 0,
                 api_base_url: str | None = None,
                 model_override: str | None = None,
                 characters_dir: Path | None = None,
                 libraries_root: Path | None = None,
                 knowledge_enabled: bool = True,
                 stream_speak: bool = False,
                 reason_grace_blocks: int = 2):
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
        # 进离场原因"想说"窗口的块数（§5.3）：交付那一刻算起，过了这么多块这项加权就没了
        # （那件事还在他记忆里，只是不再压着他）。0 = 关掉这一项（原因照旧只进上下文）。
        self.reason_grace_blocks = max(0, int(reason_grace_blocks))
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

        # ---- 信息库（设计文档 §5/§6）----
        # libraries_root 缺省 None = **没有信息库**：self._knowledge 必须是 None，图因此
        # 走今天那条老路（think 一次 complete_json、speak 不带取用小节），与引进信息库之前
        # **逐字节、逐调用次数相同**（老调用方零语感差异，硬约束）。
        # 正式布局由 GUI/CLI 显式传 `app/libraries`（§13.3：本期就落在仓库内的素材目录）。
        # knowledge_enabled 是 §10.2 那个用户可见的「信息库检索 开/关」：关 = 与没给库同义
        # （不注入索引、不传 tools、不执行取用），也退回今天的行为。
        self._libraries_root: Path | None = (
            Path(libraries_root) if libraries_root is not None else None)
        self._knowledge: KnowledgeAccess | None = (
            KnowledgeAccess(self.run_root, self._scene_space,
                            libraries_root=self._libraries_root)
            if (self._libraries_root is not None and knowledge_enabled) else None)
        # 散场结算（§7）：引擎只准备（写总结、收所得、算清单），界面/CLI 才决定保留还是丢弃。
        # `_settlement_prepared` 是本场的**幂等闸门**（连调两次不能生成两条总结条目——键里的
        # N 是按"已有几条枢纽条目"算的，第二次照算就会写出 第2场、第3场…）；
        # `_summary_done` 是"这一场的散场总结**不必再写**"的人（真的写进去了，或者没有角色卡
        # 这种重试也不会变的情形）：闸门只在总结真的写出来之后关上，重试时只补没写成的人，
        # 写成过的人绝不重写（§7.5 幂等的用处正是让重试安全）；
        # `_settlement_gains` 是这一轮收出来的所得（供调用方展示清单：谁、几条、各自标题）；
        # `_settled_here` 记"引擎自己**在这一次运行里**给谁结过账、当时账本上有哪些键"——
        # 供 `_note_stuck_gain` 分辨"结账之后又记下的"（那些永远并不进本体库，要说出来）；
        # `_settlement_warnings` 是"总结没写出来"这类只该让用户知道、绝不该打断结算的事。
        # `_copy_rotation_warnings` 是"开场轮换归档没做成"（§5.1）：它发生在**构造**期，而
        # `open_scene` 会把上一行的警告清空重开——故单独存一份，免得那条"这一场的所得并不进
        # 本体"在散场之前被清掉（用户唯一知情渠道见 `_note_copy_rotation_warning`）。
        self._settlement_prepared = False
        self._summary_done: set[str] = set()
        self._settled_here: dict[str, set[str]] = {}
        self._settlement_gains: dict[str, settle_mod.SceneGain] = {}
        self._settlement_warnings: list[str] = []
        self._copy_rotation_warnings: list[str] = []
        # 「关系表读不出来」的警告（《人际关系与场景推进》§6.2）：与轮换警告同一条命——它
        # 发生在开场建副本那一刻，而它要说的事（这一场他不认任何人）整场都成立，所以开场
        # 清结算警告时不清它（见 `_note_relations_warning`）。
        self._relations_warnings: list[str] = []
        # 离场边界（角色名 → 那一刻**最后一条消息的 id**）：他走了之后的事他没在场，散场
        # 总结只算到这一刻（§7.3 离场挂起：副本冻结快照，不能把"他没听见的内容"写进他的一夜）。
        # 用消息 id 而不是 turn：消息的 turn 是"哪一块产出的"，而块末落的行（叙述/播报）
        # 与下一块的消息**共用同一个 turn**（`_post_cast_line` 取的是下一块的号），拿 turn
        # 划界会把下一块的开场白放进他的总结里——差一块，测试能看出来。
        self._left_id: dict[str, int] = {}
        # 「还没说出口的原因」（《人际关系与场景推进》§5.2）：{当事人: _ReasonNote}。
        # 它只有一个来源——`_post_reason_note` 真正把那一行交到他手上的那一刻；窗口长度
        # 由 `reason_grace_blocks` 决定（见 `_unsaid_reasons`）。空表 = 没有这件事
        # → 动力学那三项恒 0 → 与今天逐位相同。
        self._reason_notes: dict[str, _ReasonNote] = {}
        # 所有已落下的原因行 id：叙述提示词据此把它们剔出去（§5.2 的私密性——叙述者不做
        # knows 过滤，而它的产出是公共行，见 `_narrator_live`）。它与 `_reason_notes`
        # 分开记：同一人可能先后有两条原因行，只有后者在窗口账上，但两条都不该给叙述者
        # 看。id 由 `graph._next_id`（全量最大 +1）发放，**单调不重用**，故撤回后留在
        # 这里的旧 id 不会误伤新消息。
        self._reason_note_ids: set[int] = set()

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
        # speak 流式接收器（§二）：与 _think_log 同一范式——speak 逐片追加到这里，
        # worker 经 speak_stream_tail() 增量读、界面据此建临时气泡。**缺省关闭**
        # （stream_speak=False）：不开启时这个 list 恒空，speak 仍走今天那一句
        # complete_text，一切逐字节、逐事件、逐调用次数相同。
        self._speak_stream: list[dict] = []
        self._speak_stream_on = bool(stream_speak)

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
        # 跳时间（§四）：**缺省关**（与 stream_speak 同一纪律——不开启时从模型调用到
        # 事件流到虚拟钟逐字节不变）。开启后由四道闸（前件/幅度/冷却/频次）从严把关，
        # 且**经过**既有的叙述冷却与活跃度体系（§4.2），不绕过它。
        self._time_skip_enabled = bool(time_skip_enabled)
        # 四道闸的参数：缺省即 §4.2 的标定值，可传 dict 覆盖（GUI/实验调节）。活跃度
        # 另行统一缩放其中的节奏两项（见 _effective_skip_params）。
        self._time_skip_params = (time_skip_params if isinstance(
            time_skip_params, scenarist_mod.TimeSkipParams)
            else scenarist_mod.TimeSkipParams(**(time_skip_params or {})))
        #: 前件闸的连续计数（§4.1 条件 1）：在场每个人的 bid 全部低于阈值 +1，否则归零。
        #: **纯读**计数——每块末采样一次数值 bid（与仲裁读同一个 bid()），不改任何数值、
        #: 不推进任何钟。
        self._silence_gate = scenarist_mod.SilenceGate()
        self._blocks_since_time_skip: int | None = None   # None = 本场还没跳过（无冷却）
        self._time_skip_count = 0                          # 本场已跳次数（频次闸）
        self._time_skip_records: list[dict] = []           # 每次提案的判定记录（可诊断）
        self._time_skip_error: str | None = None           # 最近一次畸形指令诊断
        self._time_skip_watermark = 0                      # 上次采样时已看过的消息条数
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

        # ---- 进场即建副本（§5.1）----
        # 放在构造**末期**：演员表到这里才最终确定（含按需补卡），给本场在场角色逐个建
        # 副本（有本体就拷一份快照，没有就建空副本）。运行中进场的走 add_character。
        # `first_entry=True`：开场阵容的每一个人都是**第一次进入这一场**，故都要做
        # "已结算副本的轮换归档"（§5.1 一条戏一份副本）——运行中**首次**进场的同样如此
        # （见 add_character）；只有"他这一场里出现过又回来"才不轮换，那时这份副本是
        # **这一场自己的工作记忆**。
        for name in list(self._active):
            self._open_knowledge_copy(name, first_entry=True)
        # §6.4：建副本**之后**挂一次关系快照，好让"第一块之前的竞价"（旁观者先开口的
        # 那些调用点：dynamics_snapshot / 前件闸 / 第 0 块的 bid）也看得见关系。
        # 没有信息库 → 空表、零 IO。
        self._refresh_relations()

    def _open_knowledge_copy(self, name: str, *, first_entry: bool = False) -> None:
        """给一名**进场**的角色确保本场副本存在（§5.1）。绝不打断主流程。

        副本已存在**原样复用、绝不重置**（`knowledgestore.ensure_scene_copy` 保证）——他
        中途离场又回来时，这一场学到的东西必须还在。
        写只写副本（§5.2）：本体库整场只读，故源目录取自 `libraries_root` 下的本体库；
        没有本体库时源目录就是副本目录本身，`ensure_scene_copy` 会走"源库没有 → 建空副本"
        那条分支。

        `first_entry=True` = **他第一次进入这一场**（开场阵容全体、以及运行中首次进场的），
        先做**轮换归档**：副本若**已经结算过**，说明它是**上一场**遗留的账本，而结算状态是
        一票制、先到先得（§7.5）——留着它，新一场的所得会被永久挡在门外（并不进本体、也没有
        第二次机会），不管是开场就在名单里还是中途才进来都一样。故把它整体重命名成
        `library.settled-<N>` 归档、把这一场的取用记录也收进去（旧账留痕、可查，§7.2 要求
        副本留着），再照上面那条路建一份干净的。

        `first_entry=False` = 他**这一场里出现过又回来**（离场后再度进场）：这份副本是
        **这一场自己的工作记忆**，即使它已经结算过也绝不轮换——他这一场走到一半不该突然
        想不起前面学到的东西（§5.1 原本要保的东西）。他结账之后新记下的那几条仍卡在副本里
        （先到先得，§7.5），由 `_note_stuck_gain` 说出来。

        判据里的"未结算"是硬的：`_rotate_settled_copy` 第一步就只认 `settled`，故**正在
        进行的这一场**（未结算的账本）在任何一条路上都不会被换掉。

        建副本失败（IO 错、路径不可写）**只记一笔日志**：这是旁挂功能，不该让开场/进场
        失败——与仓库里其它"旁挂失败不打断主流程"的处理一致（如 _note_hook_error）。
        """
        if self._knowledge is None:
            return
        try:
            copy_dir = knowledge_store_mod.scene_library_dir(self.run_root, name)
            if first_entry:
                self._rotate_settled_copy(name, copy_dir)
            own = (knowledge_store_mod.library_dir("character", name,
                                                   root=self._libraries_root)
                   if self._libraries_root is not None else None)
            knowledge_store_mod.ensure_scene_copy(
                own if own is not None else copy_dir, copy_dir)
        except Exception as exc:
            logger.warning("为 %s 建信息库副本失败（开场/进场照常）：%s", name, exc)
        # 关系表读不出来（§6.2）：这是**工具层够不到**的那一类——表退成空，于是提示词里
        # 没有那一节、工具列表里也没有 update_relation，连那句专门报读盘问题的话都够不到。
        # 不说的话，用户看到的是"这个角色忽然一个熟人都不认得了"，而程序一声不吭。
        try:
            for line in self._knowledge.relations_warnings(name):
                self._note_relations_warning(f"「{name}」{line}")
        except Exception as exc:                  # 旁挂失败绝不打断开场/进场
            logger.warning("读 %s 的关系表警告失败：%s", name, exc)

    def _rotate_settled_copy(self, name: str, copy_dir: Path) -> None:
        """第一次进场时把**已结算**的本场副本整体轮换归档（§5.1「一条戏一份副本」）。绝不打断开场。

        判据只有 `settled`（`pending.json`），**不看 `outcome`**：丢弃过的副本同样要轮换
        ——它的账也结完了，留着它一样会把新一场挡在门外（"丢弃"只该决定那一场的东西并不并进
        本体，不该连下一场一起判）。归档名与"绝不覆盖"的理由见
        `knowledgestore.settled_archive_dir` 的 docstring。

        轮换搬的不只是那份库：`recall.jsonl`（§6.4 的私人回喂源）住在**高一层**的角色运行
        目录里，也属于"这一场的账"，一并收进归档（见 `_archive_scene_recall`）——它按**精确
        轮次**回喂，而新一场的轮次又从 0 起算，留着它新一场第 1 块的 speak 就会拿到上一场
        同轮取用的正文。

        **轮换失败时选择"照旧复用那份已结算的副本"**（记一条 warning，`_open_knowledge_copy`
        接着走 ensure → 复用），理由有两条：

          · 删掉重来是把用户**没归档成功**的旧账直接销毁——§7.2 明写副本要留着，归档的
            意义正是"旧账留痕"；轮换没做成，至少让那份账原样躺在磁盘上，什么都没丢；
          · 复用的代价是这一场新记下的东西并不进本体（先到先得，§7.5），所以 warning 必须
            把这件事**说清楚**——静默复用会让用户以为结算生效了，而那正是他最需要知道的那
            一件事（`settlement_warnings()` 是界面/CLI 唯一的知情渠道，见 `_note_settlement_warning`）。
        """
        try:
            if not knowledge_store_mod.load_pending(copy_dir).settled:
                return                      # 未结算 = 这一场的账本，一律照旧复用（§5.1）
            target = knowledge_store_mod.settled_archive_dir(copy_dir)
            Path(copy_dir).rename(target)
        except Exception as exc:            # 重命名失败 / 磁盘满 / 目录被占用 …
            self._note_copy_rotation_warning(
                f"「{name}」上一场已结算的副本没能归档（{exc}）：这一场仍复用那份已结算的"
                f"副本，他这一场新记下的东西**无法并入本体库**（先到先得，§7.5）。"
                f"旧账原样留在 {copy_dir} 里，可手工归档后重开。")
            return
        self._archive_scene_recall(copy_dir, target)
        logger.info("「%s」上一场已结算的副本归档为 %s（§5.1 一条戏一份副本）。",
                    name, target.name)

    def _archive_scene_recall(self, copy_dir: Path, archive: Path) -> None:
        """把上一场的取用记录一并收进归档（§5.1 × §6.4）。**绝不抛**，失败只记日志。

        为什么轮换必须带上它：`recall.jsonl` 是这一场**第二个**私人回喂源（think→speak，
        §6.4），住在 `runs/<角色名>/` 下——比副本目录**高一层**，所以"重命名副本"这一步
        天然搬不到它。它按**精确轮次**回喂（`recall_text` 只返回 `turn == 当前轮` 的行），
        而新一场的 `_turn` 又从 0 起算：留着它，新一场第 1 块 speak 注入的【你想起的事】
        就是上一场同轮取用的正文——用户明明把那一场丢弃了/结了账，角色却照着它开口，正是
        §5.3/§6.4 反复在防的那类 bug。

        为什么是**搬进归档**而不是删掉：取用记录与副本一样是"这一场的账"（§7.2 要求留着，
        事后翻得到他那一场查过什么）。归档目录是**刚刚**由 `library` 重命名来的，里面不可能
        已经有同名文件，故用 `rename`（撞名即失败、绝不覆盖半字）。

        失败只记日志、不进 `settlement_warnings()`：后果限于提示词里多一段上一场的正文，
        用户无从处置；而结算警告是"他这一场的东西并不进本体"那类**必须让用户知道**的事，
        掺进这里只会把真正要紧的那条淹没。
        """
        try:
            src = Path(copy_dir).parent / RECALL_FILENAME
            if src.is_file():
                src.rename(Path(archive) / RECALL_FILENAME)
        except Exception as exc:            # 路径怪串 / 权限 / 占用 …：照旧"绝不抛"
            logger.warning("上一场的取用记录没能收进归档（%s）：它仍留在 %s，"
                           "新一场同轮的 speak 可能读到上一场取用的正文。",
                           exc, Path(copy_dir).parent)


    def _note_copy_rotation_warning(self, text: str) -> None:
        """「开场轮换没做成」的警告：进日志 + `settlement_warnings()`，且**开场清不掉它**。

        为什么单独一条列表：轮换发生在**构造**期（副本就是在那里建的），而 `open_scene` 会
        把本场的结算警告清空重开。轮换失败的后果（这一场的所得并不进本体）恰恰要到散场那一步
        才兑现——消息在那之前被清掉，用户就永远看不到"他为什么这一场什么都没记住"。
        """
        if text in self._copy_rotation_warnings:
            return
        self._copy_rotation_warnings.append(text)
        logger.warning("%s", text)

    def _note_relations_warning(self, text: str) -> None:
        """「关系表读不出来」的警告：进日志 + `settlement_warnings()`，开场清不掉它（同轮换警告）。

        为什么单独一条列表：它产生在**开场建副本**那一刻，而 `open_scene` 会把本场的结算
        警告清空重开——可它要说的事（"这一场他不认任何人"）整场都成立，而且用户**唯一**能
        看到它的地方就是散场那份警告清单（界面/CLI 都读 `settlement_warnings()`）。

        为什么要说出来：关系表读不出来时工具层的三条出口全部够不到（表退成空 → 提示词里
        没有小节、工具列表里没有 `update_relation`、`_relation_miss` 那句报读盘问题的话
        无人触发），而会坏的只有**用户手写的那一份**——不报，用户看到的是"角色忽然一个
        熟人都不认得了"，程序一声不吭。
        """
        if text in self._relations_warnings:
            return
        self._relations_warnings.append(text)
        logger.warning("%s", text)

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
            space=self._scene_space,                 # C1：不可变空间键（改名不动它）
            knowledge=self._knowledge,               # §6：None = 无信息库（逐字节不变）
            # §二 流式接收器：引擎自持的只读边通道（worker 增量读尾部）+ 用户开关。
            # 关闭时（缺省）speak 走今天那一句 complete_text，接收器恒空。
            speak_stream=self._speak_stream, stream_speak=self._speak_stream_on)
        self._graph = graph_mod.build_graph(
            self.cards, self.scene, self.think_backend, self.speak_backend,
            run_root=self.run_root, bid_params=self._bid, checkpointer=self._saver,
            closing_at_block=self.closing_at_block, store=self._store,
            demo_alternate=self.demo_alternate, think_log=self._think_log,
            bidder=lambda name: self._dynamics.bid(name, self.cards[name].weights),
            cast_provider=self.speakable_names,      # 扇出/竞价只认在场且未禁言者
            entry_round_of=self._entry_round_of,     # 进场可见性基线（§3.3）
            knowledge=self._knowledge,               # §6：两处都要传（显式 ctx 时以 ctx 为准）
            speak_stream=self._speak_stream,         # §二：同上（两处都传，口径一致）
            stream_speak=self._speak_stream_on,
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
        （"夜晚的餐厅，二人临窗而坐。"）。开场消息不带时刻戳——真实流速虚拟钟在
        worker/GUI 侧（开场即此刻），由 worker 派发时补 time_hhmmss。其余与事件流不变。"""
        graph = await self._ensure_graph()
        content = (opening if opening is not None
                   else "夜晚的餐厅，二人临窗而坐。")
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
        # 新场 → 结算一侧全部重开（§7.5 幂等只在**同一场**内成立）：上一场的准备闸门、
        # 清单与警告都不该跟着新场走（他回来时"这一场是第几场"要重新数）。
        self._settlement_prepared = False
        self._summary_done = set()
        self._settled_here = {}
        self._settlement_gains = {}
        self._settlement_warnings = []
        self._left_id = {}
        self._reason_notes = {}          # 新场从零起算（§5.2 的原因不属于下一场）
        self._reason_note_ids = set()
        self._refresh_reasons()          # 动力学那边同步清空（同一台动力学会被复用）
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
            self._refresh_relations()           # §6.4：本块竞价用的关系快照（无库=空表）
            self._refresh_reasons()             # §5.2：本块竞价用的「没说出口的原因」
            await graph.ainvoke({}, self._cfg())
            st = await self._snapshot()
            # 数值动态推进：折本块 think → 判 mark_spoke/tick_silence（见 docstring）
            spoke = self._advance_dynamics(n_before, think_before, st)
            out += [ev.decision_event("block_done"), ev.scene_state(
                st.get("turn", 0), st.get("closed", False))]
            # 块末的可插拔演员表推进（§3.3/§4）：先让本块起算的禁言走完一格（N 块禁言
            # 恰好挡 N 个块），再让到期的延时角色动作执行——本块新下的禁言因此不被本块
            # 扣减。两种产出都是正常消息行，随事件流/界面派发（worker 靠 sweep 上屏）。
            self._tick_mutes()
            for line in await self._fire_pending_cast():
                out.append(ev.block_spoken(line))
            narrated = await self._maybe_narrate(st, spoke=spoke)
            # 钩子落下的可见行（上下文事件/角色事件/场景事件）先进事件流：它们是在本块
            # 叙述之前写下的（叙述要体现改动后的新状态，见 _narrate 的次序）。
            for line in self._drain_hook_lines():
                out.append(ev.block_spoken(line))
            if narrated is not None:
                out.append(ev.block_spoken(narrated))
            # 自动保存（§5）：到点就在块末静默存一次（失败只记诊断，绝不打断本块）。
            await self._maybe_autosave()
            # 收束跳变（§7.4）：本块跑完 closed 由假变真 = 这一场结束了。检测点必须在这里
            # ——它是**唯一同时覆盖 GUI、CLI、块钟兜底三条路**的位置。`close_scene()` 里做不到：
            # 块钟兜底那条路是图自己在 world 里写 closed，根本不经过引擎那个方法（挂那一处
            # 必然漏路）。检测到就准备结算（写散场总结、收本场所得）并发「待决」事件；
            # 无信息库时整段是空操作（一个字节、一次调用都不多）。
            if self._knowledge is not None and self._closed:
                out += await self._prepare_on_close()
        return out

    # ------------------------------------------------ 场景（叙述者）：判据与产出 --
    async def _maybe_narrate(self, st: dict, *, spoke: bool = False) -> dict | None:
        """每块末的场景推进判定（自动档）：块数 +1 → 算判据 → 决定「咨询」与「落行」两件事。

        `spoke`：本块有没有人开口（角色台词落地）——前件闸（§4.1 条件 1）的一半，
        由 step 从 `_advance_dynamics` 拿来（那里已经在判 mark_spoke/tick_silence）。

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
        self._recount_time_skips(st)         # 频次闸记账 = 活着的跳时间行数（撤回/续演自愈）
        self._observe_time_skip(st, spoke)   # 前件闸：本块末全员 bid 快照（关掉时不做）
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
            # 跳时间同受这道闸约束（rhythm_ok=may_narrate）：见 _narrate。
            return await self._narrate(report, consult_hooks=True,
                                       keep_narration=may_narrate,
                                       rhythm_ok=may_narrate)
        if may_narrate:
            return await self._narrate(report, rhythm_ok=may_narrate)
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

    # ------------------------------------------ 场景跳时间（§四）：四道闸与诊断 ----
    # 分工是硬的（§4.1）：**前件闸在引擎侧**（条件 1：本块没人开口 且 全员 bid 低于阈值，
    # 连续 K 块如此——确定性判据，两半缺一不可，见 _observe_time_skip），**条件 2 由场景
    # agent 判**（它提 `[[SKIP:<分钟>]] <正文>` 提案）。模型说该跳、前件不成立就**不跳**
    # ——与 hooks 的时间闸门（_time_gate）同一套思路：模型判断 + 引擎否决。四道闸的判定
    # 本身是 scenarist.judge_time_skip（纯函数、可离线单测）。
    def _effective_skip_params(self) -> scenarist_mod.TimeSkipParams:
        """当前活跃度缩放后的跳时间参数（前件 K 与冷却 M 随活跃度；幅度/频次是硬限制）。"""
        return scenarist_mod.effective_skip_params(self._time_skip_params,
                                                   self._narrate_activity)

    def _observe_time_skip(self, st: dict, spoke: bool = False) -> None:
        """每块末（叙述判定前）读一次前件闸与冷却计数。**纯读**，关掉时一个读取都不多。

        采样的是 `dynamics.bid()` 的**同一个**数值（与仲裁读同一函数，不新增数值系统）；
        「在场」取 `speakable_names()`——**禁言者不参与**：他被场景明确按住了，他的发言
        倾向不该成为"大家还有话说"的证据。空场（全员离场/全被禁言）恒不成立（见
        scenarist.all_bids_below），故跳不成——宁可跳不成，也不要跳错。

        `spoke`：本块有没有人开口（角色台词落地）。**「无话可说」的另一半**
        （scenarist.SilenceGate.observe）：有人刚开口 = 他显然有话要说，这一块不算数——
        只看 bid 会在两人一句接一句对话时洞开（bid 里 recency 惩罚 −0.6 与只涨一格的
        沉默压力恰好让"全员都低"每块都成立，实况错跳就出在这里）。**人类这一行也算**
        （按上次采样以来的新增消息判）：用户刚说了话而角色还没接，这时跳过时间等于把用户
        的话跳过去——与他点名与否无关（点名那一半由 bid 里的欠答义务兜底，没点名的这一半
        只有这里能看见）。
        """
        if not self._time_skip_enabled:
            return
        msgs = st.get("messages") or []
        start = min(self._time_skip_watermark, len(msgs))
        spoke = bool(spoke) or any((m.get("speaker_type") or "") == "human"
                                   for m in msgs[start:])
        self._time_skip_watermark = len(msgs)
        if self._blocks_since_time_skip is not None:
            self._blocks_since_time_skip += 1
        bids = {name: self._dynamics.bid(name, self.cards[name].weights)
                for name in self.speakable_names()}
        self._silence_gate.observe(bids, spoke=spoke)

    @staticmethod
    def _live_jumps(live: list[dict]) -> int:
        """活着（未被撤销）的跳时间行数：**频次闸的唯一记账源**（§4.2「整场最多跳 N 次」）。

        计数而不是计数器：撤回/改写让一条跳时间行作废时，它那一跳（连同 worker 侧被收回
        的钟偏移）就该从额度里退回来；续演把 sidecar 转录追回来时，旧账要跟着一起回来。
        派生自转录 = 两条路径自愈，不必在四处各写一遍加减。
        """
        return sum(1 for m in (live or []) if m.get("clock_jump_minutes"))

    def _recount_time_skips(self, st: dict, *, retracted=None) -> None:
        """按当前**活着**的消息重记本场跳跃次数（撤回/续演/改写后自动对齐）。

        `retracted`：还要一并算作"已作废"的 id（撤回刚写完共享态、手上那份 st 还是旧的
        时用）。关掉跳时间时什么都不做。
        """
        if not self._time_skip_enabled:
            return
        live = graph_mod._live_messages(st)
        if retracted is not None:
            dead = {int(i) for i in retracted}
            live = [m for m in live if int(m.get("id") or 0) not in dead]
        self._time_skip_count = self._live_jumps(live)

    def _judge_time_skip_proposal(self, requested: int, body: str, *,
                                  rhythm_ok: bool) -> scenarist_mod.TimeSkipVerdict:
        """把一条提案过四道闸，并把判定记进 records（被挡下必须可诊断，绝不静默丢弃）。"""
        verdict = scenarist_mod.judge_time_skip(
            requested, body=body, streak=self._silence_gate.count,
            blocks_since_last=self._blocks_since_time_skip,
            jumps_done=self._time_skip_count,
            # 小幅度优先：正文自己写出的时距也算一份证据（钟偏移不得大于正文承诺）
            implied_minutes=scenarist_mod.jump_minutes_from_text(
                body, clock_seconds=self._clock_now),
            rhythm_ok=rhythm_ok, params=self._effective_skip_params())
        record = {"block": self._blocks, "turn": self._turn,
                  "requested": int(requested),
                  "granted": verdict.minutes if verdict.granted else 0,
                  "gate": verdict.gate, "reason": verdict.reason,
                  "message_id": None}
        self._time_skip_records.append(record)
        if not verdict.granted:
            logger.warning("跳时间被拒（%s）：%s", verdict.gate, verdict.reason)
        return verdict

    def _note_time_skip_error(self, message: str) -> None:
        """跳时间的诊断（畸形指令等）：记进只读字段并落日志。绝不抛异常。"""
        self._time_skip_error = message
        logger.warning("场景跳时间诊断：%s", message)

    def _last_skip_rejection(self) -> str | None:
        """上一块的提案被挡下的中文理由（提示词回喂用；没有/不是上一块 → None）。

        只回喂**引擎自己的判定结果**（`records[-1]["reason"]`，如幅度上限/前件不成立），
        不放宽任何一道闸；目的只是让模型别每块原样重提一条永远过不了的提案。
        **只报上一块的**：更早的理由可能已经不成立（前件/冷却都随时间变），拿着旧账
        教训模型反而会把它带偏。
        """
        if not self._time_skip_records:
            return None
        last = self._time_skip_records[-1]
        if last.get("gate") == "ok" or int(last.get("block", -99)) < self._blocks - 1:
            return None
        return str(last.get("reason") or "") or None

    def time_skip_state(self) -> dict:
        """跳时间状态快照（sync、只读；供 GUI/日志/测试）：

          enabled         开关（关掉时其余字段恒为初值：没采样、没记录、没跳）
          streak          前件闸当前连续「全员无话可说」块数
          streak_blocks   前件闸的 K（活跃度缩放后）
          blocks_since    距上次跳跃的块数（None = 本场还没跳过 → 无冷却）
          jumps/max_jumps 本场已跳次数 / 上限（次数按**活着的**跳时间行重算：撤回/改写
                          让那一跳作废时额度跟着退还，续演时旧账跟着转录一起续）
          max_jump_minutes/cooldown_blocks  幅度闸上限 / 冷却块数（活跃度缩放后）
          records         每次提案的判定记录：{block/turn/requested/granted/gate/reason/
                          message_id}（含被闸挡下的——这是"为什么没跳"的唯一答案）
          error           最近一次畸形指令诊断（None = 无）
        """
        params = self._effective_skip_params()
        return {
            "enabled": self._time_skip_enabled,
            "streak": self._silence_gate.count,
            "streak_blocks": params.streak_blocks,
            "blocks_since": self._blocks_since_time_skip,
            "jumps": self._time_skip_count,
            "max_jumps": params.max_jumps,
            "max_jump_minutes": params.max_jump_minutes,
            "cooldown_blocks": params.cooldown_blocks,
            "records": [dict(r) for r in self._time_skip_records],
            "error": self._time_skip_error,
        }

    def set_time_skip_enabled(self, on: bool) -> None:
        """开关「场景跳时间」（§四）：**立即生效**（下一块起）。

        sync、只改一个旗标（供 time_skip_state()["enabled"] 如实回读）。关掉之后引擎
        不再采样 bid、不再认 `[[SKIP:…]]` 指令，提示词里也不再附那一块——行为逐字节回到
        加这个特性之前。调用方须在引擎所在 loop 内调用（worker 经 _post_call 投递）。
        """
        self._time_skip_enabled = bool(on)

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
                       keep_narration: bool = True,
                       rhythm_ok: bool = True) -> dict | None:
        """场景开口：构提示词 → 调 narrate 档后端 → 剥指令 → 裁剪成 1~2 行 → 落进共享态。

        视图 = **全部未撤销消息**，减去「进离场原因」那些只发给当事人的私密行（场景
        全知、不做 knows 过滤——它要能看见角色看不见的私密句才谈得上"知道世界在发生
        什么"；唯一的例外是当事人亲手填下的私事，理由见 `_narrator_live`：它写出来的
        每个字都会落成公共行）。落点的在场轴 = 本场景名（尾条的 in_scene 沿用，无尾条则
        场景名）、knows=None（人人都看得见发生的事）。叙述失败绝不把异常带进场景：记下
        原因、返回 None，本块当作没发生。

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

        `keep_narration`：引擎自己判定的"这一轮可以叙述"（节奏闸门）。为假时只落**真的
        发生了事件**（钩子触发）的那条说明行，见下面的收尾判断。
        `rhythm_ok`：跳时间提案要过的节奏闸（§4.2 末条"不绕过它"）——自动档传引擎自己
        的判定，手动推进（narrate_now）只保留冷却那一半。
        """
        try:
            st = await self._snapshot()
            if st.get("closed"):
                return None                      # 已收束：世界不再推进
            live = graph_mod._live_messages(st)
            # 私密行（进离场原因）不进叙述者视图（见 _narrator_live）：它写出来的都是
            # 公共行。留空时这一步不留任何痕迹（原样返回同一个 list）。
            view_text = render_view([Message.model_validate(m)
                                     for m in self._narrator_live(live)])
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
            # **跳时间**（§四）：提案由场景自己提（`[[SKIP:<分钟>]] <正文>`，§4.1 条件 2），
            # 但**能不能跳由引擎说了算**——四道闸逐条过（§4.2），与 hooks 的时间闸门同一
            # 套思路（模型判断 + 引擎否决）。被挡下一律留诊断并**不落行**：正文在说时间，
            # 钟不能骗人。放行的行照走既有叙述那条路，只多一个 clock_jump_minutes 字段。
            #
            # 节奏闸（rhythm_ok）由调用方给：自动档 = 引擎自己的"这一轮可以叙述"（冷却 +
            # 活跃度缩放后的触发线，即 _maybe_narrate 的 may_narrate）——跳时间不绕过既有
            # 的推进节奏；**手动**推进（narrate_now）是用户明确要求，只保留冷却那一半
            # （与手动叙述无视 cooldown 的既有口径一致）。
            skip_verdict = None
            final_text = parsed.text
            if self._time_skip_enabled:
                skip = scenarist_mod.parse_time_skip(final_text)
                final_text = skip.text          # 无标记时逐字节原样返回（见 ParsedTimeSkip）
                if skip.malformed:
                    self._note_time_skip_error(
                        f"畸形跳时间指令（已剥离）：{'；'.join(skip.malformed)}")
                if skip.minutes is not None:
                    skip_verdict = self._judge_time_skip_proposal(
                        skip.minutes, skip.text, rhythm_ok=rhythm_ok)
                    if not skip_verdict.granted:
                        return None
            if skip_verdict is None and not keep_narration and not hook_ids:
                # 节奏闸门没过（活跃度/冷却）**且这一轮没有任何钩子真的触发** → 只带回
                # 诊断、不落叙述。这样"有 hook 的场景每块都插一句"不再成立；而**真的发生了
                # 事件**（钩子触发）时，它那句话是该事件的说明，照常落行。
                return None
            content = _trim_narration(final_text)
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
            if skip_verdict is not None and skip_verdict.granted:
                # §4.3：这条行的落地值 = 钟偏移。**引擎自己不碰钟**（钟归 worker 持有，
                # 图与引擎仍不持钟，既有分工不变）；worker 派发这条行时据此前推虚拟钟。
                msg["clock_jump_minutes"] = skip_verdict.minutes
            await self._post_line(msg)
            self._blocks_since_narration = 0     # 刚推进过：cooldown 重新起算
            self._narration_ids.append(msg["id"])
            self._narration_error = None
            if skip_verdict is not None and skip_verdict.granted:
                self._time_skip_count += 1
                self._blocks_since_time_skip = 0     # 冷却闸重新起算（§4.2）
                self._silence_gate.reset()           # 活动已完结：前件重新攒 K 块
                if self._time_skip_records:
                    self._time_skip_records[-1]["message_id"] = msg["id"]
            return msg
        except Exception as exc:                 # 后端/校验任何失败都不进场景
            self._narration_error = f"{type(exc).__name__}: {exc}"
            return None

    # ------------------------------------------- 场景提示词（S4b：字段/工具/钩子） ----
    def _scene_prompt_extra(self, hooks_on: bool) -> str:
        """叙述提示词的追加块：场景工具指令 + 钩子清单与报告契约 + 跳时间的契约。

        各段都非空才拼；都没有 → ""（提示词与这些特性之前逐字节相同，既有路径不漂移）。
        工具段只在 description_mutable 时给——不给语法，模型就无从凭空改场景；跳时间段
        只在开启跳时间时给（关掉时连这一块的字节都没有，见 set_time_skip_enabled）。
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
        if self._time_skip_enabled:
            # 跳时间契约（§4.1 条件 2）：把"什么时候可以跳、怎么提、四道闸的数值、当前
            # 前件状态"一次说清。引擎另有一道硬判据兜底，所以即使模型不看状态乱提，
            # 也不会真的跳错。**上一次被挡下的理由也回喂**（如"跳跃 660 分钟超过单次上限
            # 480 分钟"）：模型否则会每块原样重提同一条，白烧 token 又推不动。
            skip_block = scenarist_mod.time_skip_prompt_block(
                self._effective_skip_params(), streak=self._silence_gate.count,
                jumps=self._time_skip_count,
                blocks_since=self._blocks_since_time_skip,
                last_rejection=self._last_skip_rejection())
            if skip_block:
                parts.append(skip_block)
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
        → 乙/丙离场）不交给模型一句话定生死——执行前用
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

        add/remove 透传 visible（隐式进出不落播报行）与 `hook.reason`（§5.2 进离场原因：
        编辑器里写的那一条，只进当事人自己的上下文；隐式时连它也不落）；禁言/解禁本来就不
        落行（状态变化由 sig_cast/左栏呈现）。`mute`（无轮数）= 永久禁言（turns=0），
        `mute_turns` 带上场景判定的轮数。
        """
        name = hook.character_name.strip()
        action = hook.action
        if action == "add":
            return await self.add_character(name, visible=hook.visible,
                                            reason=hook.reason)
        if action == "remove":
            return await self.remove_character(name, visible=hook.visible,
                                               reason=hook.reason)
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
        self._recount_time_skips(st)          # 频次闸记账与转录对齐（撤回/续演后）
        msg = await self._narrate(report, consult_hooks=bool(self._enabled_hooks()),
                                  rhythm_ok=not report.blocked_by_cooldown)
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

        **跳时间的账跟着转录一起续**（§4.2 的频次/冷却都写"整场戏"）：续演的不是新的一场，
        而是同一场戏的下半段——转录里可能已有跳时间行（钟前推过、提示词也报过账）。故
        续演后**已跳次数 = 转录里活着的跳时间行数**（`_live_jumps`），且只要还有活着的那条，
        冷却就按"刚刚推过"起算（`blocks_since=0`）——宁可跳不成，也不让反复续演刷出额度。
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
            # 跳时间的账跟着转录续（见 docstring）：次数按活着的那几条重算，冷却按
            # 「刚刚推过」起算（续演不是新的一场，不许靠反复续演刷额度）。
            if self._time_skip_enabled:
                self._recount_time_skips(await self._snapshot())
                self._blocks_since_time_skip = 0 if self._time_skip_count else None
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
          · **信息库一侧同样退回开演前**（§5.3/§6.1/§6.4，见 _reset_knowledge）：本场副本的
            条目与 recall.jsonl 是**第三条私人回喂源**（索引每块注入、取用正文延续到本块
            speak），漏掉它角色照样带着"从没发生过的事"开口，散场还会把它们并进本体；
          · 数值动态**重新播种**（Dynamics 全新、turns_since_spoke 归零，按当前在场名单）；
          · 叙述判据/钩子事件缓冲归零（叙述状态从"刚开过口"起算的那几个计数器），
            **跳时间的四个计数器一并归零**（前件连续计数 / 冷却 / 本场已跳次数 / 诊断
            记录）——§4.2 的频次与冷却都写"整场戏"，重置对用户就是新的一场；
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
        self._speak_stream.clear()           # 流式接收器同纪律（否则新场会先冒出上一场的尾片）
        # I3(最终评审)：**私有**的 next-think 回喂也要清。think/speak 每次都读本人的
        # state.jsonl 尾部与 impressions（jsonl 为准、md 是人读渲染）拼进提示词——
        # 只清 _think_log（只读边通道）不够：那些文件还在，重置后的角色照样带着上一场的
        # 私有状态开口（dynamic_states() 也仍旧返回旧值）。对**每个在场者**清一次。
        for name in self._active:
            if name in self.cards:
                memory_mod.CharacterMemory(self.run_root, name).clear()
        self._reset_knowledge()
        self._dynamics = Dynamics(
            self._active, params=self.dynamics_params,
            emotion_rates={name: self.cards[name].emotion_decay_rate
                           for name in self._active if name in self.cards})
        self._refresh_relations()      # 重新播种的一份动力学也要挂上关系快照（§6.4）
        self._blocks_since_narration = 0
        self._narration_ids = []
        self._last_nudge = None
        self._narration_error = None
        self._hook_error = None
        self._tool_error = None
        # 跳时间的四个计数器同属"本场"（§4.2 的频次/冷却都写"整场戏"）：重置对用户就是
        # 新的一场（对白区清空、dynamics 重新播种），旧的额度与冷却必须跟着退回开演前——
        # 漏掉它们会有两个方向的事故：跳过 3 次再重置 → 频次闸永远说"已跳 3/3"，这场新戏
        # 再也跳不成（time_skip_state 还报着上一场的旧账）；冷却没退回 → 重置后立刻能跳。
        self._silence_gate = scenarist_mod.SilenceGate()
        self._blocks_since_time_skip = None
        self._time_skip_count = 0
        self._time_skip_records = []
        self._time_skip_error = None
        self._implicit_events.clear()
        self._hook_lines.clear()
        self._scene_changes.clear()          # 自改变更流也是这一场的运行痕迹
        # 结算一侧同样退回开演前（§5.3/§7）：副本已经被 `_reset_knowledge` 清成本场之前的
        # 样子，上一场那笔"待结算"的清单与准备闸门必须跟着走——留着它，重置后的散场会拿
        # 一份已经不存在于副本里的清单去问用户。
        self._settlement_prepared = False
        self._summary_done = set()
        self._settled_here = {}
        self._settlement_gains = {}
        self._settlement_warnings = []
        self._left_id = {}
        self._reason_notes = {}          # 重置 = 这一场从头开始（原因也一起清）
        self._reason_note_ids = set()
        self._refresh_reasons()          # 动力学那边同步清空（否则带着上一场的加权）

    #: 重置场景的"撤回位置"：**基线条目哨兵**（§5.3，`knowledgestore.BASELINE_TURN`）。
    #: 截断口径是"保留 turn ≤ cut"，取它恰好清掉所有本场产物（turn ≥ 0：本场 remember /
    #: revise 写下的条目与修订），而从本体拷进来的基线条目与订阅的活引用一条不动——重置是
    #: "这一场从头开始"，不是"忘掉身世"。
    _RESET_CUT = knowledge_store_mod.BASELINE_TURN

    def _reset_knowledge(self) -> None:
        """重置场景时把信息库一侧也退回开演前（§5.3 / §6.1 / §6.4）。绝不打断重置。

        重置的语义是"这一场的对话与思考上下文归零"（见 reset_scene_runtime），而信息库的
        **索引（§6.1）与取用记录（§6.4）正是两条新增的私人回喂源**：只清 state.jsonl /
        impressions 不够——副本里的本场条目还在索引里（下一块 think 照样把它当【你的信息
        索引】），recall.jsonl 还在本块 speak 的提示词里（【你想起的事】），pending.json 还
        宣称它们是"本场所得"（散场选「保留」会永久并进本体）。撤回（retract）早就按同一
        口径接了这条通道（见 `_truncate_memory`）；重置 = **整场撤回**，漏接它是同一类 bug。

        口径与撤回逐字一致：走工具层的 `truncate_scene_after_turn` /
        `truncate_recall_after_turn`（**绝不**直接调存储层的 `truncate_copy_after_turn`——
        它只删条目，不做 §5.3.1 的"被压下去的旧条目放开 / 本场改过的基线还原成本体原文"），
        cut 取哨兵轮次（见 `_RESET_CUT`）。名单与 `_truncate_memory` 一致：在册的**全部人**
        （在场 + 已离场）——离场者的副本同样吃着这一场的上下文。旁挂失败只记一笔，绝不
        让一次 IO 错误打断重置。
        """
        if self._knowledge is None:
            return
        cut = self._RESET_CUT
        for name in dict.fromkeys([*self._active, *self._inactive]):
            try:
                report = self._knowledge.truncate_scene_after_turn(name, cut)
                self._knowledge.truncate_recall_after_turn(name, cut)
            except Exception as exc:
                logger.warning("重置时清 %s 的信息库副本失败：%s", name, exc)
                continue
            self._log_truncate_report("重置", name, cut, report)

    @staticmethod
    def _log_truncate_report(action: str, name: str, cut: int,
                             report: TruncateReport) -> None:
        """把一次信息库截断的报告记进日志（§5.3.1）：删了什么、还原了什么、**有什么没做成**。

        工具层的 `TruncateReport` 是为调用方记日志而存在的——"撤回是最需要留痕的动作，只回
        一个数调用方就没法记出一条能看懂的日志"，而 `warnings` 里装的恰恰是**用户必须知道**
        的那一类："本场改过的基线在本体里找不回原文，还原没做成"。把它丢在地上，副本里就
        继续现行着一条依据已被撤回的改写（§5.3.1 要防的形态），用户既看不见也选不了。
        """
        if report.warnings:
            logger.warning("%s截断 %s 的信息库（cut=%s）有没做成的事：%s",
                           action, name, cut, "；".join(report.warnings))
        if report.dropped or report.restored or report.relations_restored:
            logger.info("%s截断 %s 的信息库（cut=%s）：删了 %d 条本场条目、还原了 %d 条、"
                        "关系退回 %d 行。",
                        action, name, cut, report.dropped, report.restored,
                        report.relations_restored)

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
          · 信息库（§5.3/§6.4）：本场副本与 recall.jsonl 按**同一口径**截断——走工具层的
            truncate_scene_after_turn / truncate_recall_after_turn（见 _truncate_memory）；
          · 视图/锚点/复读判定/动态 heard 取样：本就只读 `_live_messages`，无需另做。

        **不倒拨世界钟**（决策）：blocks / turn / _blocks_since_narration / 数值动态一律
        不回退——撤回的是"说过的话"，不是"过掉的时间"；冷却若被撤回重置，一次撤回就等于
        白送一次立即推进。被丢弃的下文既已不在任何视图里，也不会因这些计时器而复活。

        **跳时间的频次额度是个例外**：它记的是"这一场**发生过**几次跳跃"，而作废的跳时间
        行 = 那一跳没发生过（它携带的钟偏移也由 worker 从虚拟钟里收回，见 worker.
        _rewind_retracted_jumps）。故这里按活着的跳时间行重算次数（`_live_jumps`），
        额度跟着转录走；**冷却计数照旧不回退**（与 blocks/turn 同类：跳过一次之后的 M 块
        节奏不因撤回而消失，绝不因撤回白送一次立即推进）。
        """
        graph = await self._ensure_graph()
        st = await self._snapshot()
        mid = int(message_id)
        ids, cut_turn = self._rollback_target(st.get("messages") or [], mid)
        retracted = sorted(set(list(st.get("retracted") or []) + ids))
        await graph.aupdate_state(self._cfg(), {"retracted": retracted}, as_node=START)
        self._retracted = retracted
        self._recount_time_skips(st, retracted=retracted)
        if cut_turn is not None:                 # 找不到该 id：只有 id 记账，不截断任何东西
            self._truncate_think_log(cut_turn)
            self._truncate_memory(cut_turn)
        # 「进离场原因」的那笔账跟着转录走（§5.2）：撤掉那一行 = 这件事从没发生过，
        # 动力学不许再按它算倾向（见 _forget_retracted_reasons）。
        self._forget_retracted_reasons(ids)
        return ids

    def _truncate_think_log(self, cut_turn: int) -> None:
        """think 只读边通道按轮次裁掉被回溯区间（**原地**改：图持有的就是这个 list）。"""
        self._think_log[:] = [e for e in self._think_log
                              if int(e.get("turn", 0) or 0) < cut_turn]

    def _truncate_memory(self, cut_turn: int) -> None:
        """把每名在场/离场者的私有记忆截断到 cut_turn 之前（见 CharacterMemory）。

        对**在册的全部人**截（不只是当前在场者）：被丢弃的那段下文里可能有人已经离场，
        他的私有解读同样建立在"那段发生过"之上，下一场再进场时不该带着它。

        信息库一并按**同一口径**截（§5.3/§5.3.1，cut 与上面 memory.truncate_after_turn
        逐字相同 = cut_turn − 1）：
          · 本场副本——必须走工具层的 `truncate_scene_after_turn`，**绝不**直接调存储层的
            `truncate_copy_after_turn`：后者只删条目，不做"被压下去的旧条目放开 / 本场改过的
            基线条目还原成本体原文"这两处收尾，会在撤回后留下"被推翻的旧说法仍是现行"的
            坏形态（§5.3.1）；
          · recall.jsonl——取用记录是回喂源，撤回某条推进后角色不能还带着"从没发生过的事"
            的知识开口（§6.4）。
        两处都是旁挂的回喂源，绝不让一次 IO 失败打断撤回本身（记一笔即可）。截断的报告
        一并记进日志（见 `_log_truncate_report`）——"还原没做成"这类事只有报告里写着。
        """
        cut = cut_turn - 1
        for name in dict.fromkeys([*self._active, *self._inactive]):
            memory_mod.CharacterMemory(self.run_root, name).truncate_after_turn(cut)
            if self._knowledge is None:
                continue
            try:
                report = self._knowledge.truncate_scene_after_turn(name, cut)
                self._knowledge.truncate_recall_after_turn(name, cut)
            except Exception as exc:
                logger.warning("撤回时截断 %s 的信息库失败：%s", name, exc)
                continue
            self._log_truncate_report("撤回", name, cut, report)

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

    # ------------------------------------------------- 关系（§6.4）：进 bid --
    def _relation_closeness(self) -> dict[str, dict[str, int]]:
        """本刻的**关系快照** `{角色名: {对方名: 亲密度}}`（§6.4），供动力学那两项用。

        三条口径，缺一不可：

          · **只收关系表非空的人**（空表/没有库的人整条不进）——于是"没有关系表"的
            角色在 `bid()` 里拿到的全是 None → 那两项恒 0 → 与今天逐位相同；
          · 读盘走**工具层那一份取值实现**（`KnowledgeAccess._relations`，与
            `relation_text` / `tools` 完全同一条：副本优先、副本缺席回落本体、坏文件退成
            空表 + 警告，且**绝不抛**）。本阶段那不是公开口子，故这里直接调它；路径换算
            仍**一个字都不自己拼**——自己拼一份就多出一个与工具层会漂开的口径；
          · 每块重建一整份（`set_relations` 整体替换）：关系能被工具改、能被撤回退回，
            增量维护意味着动力学要懂那本账。几十行的表重建一次可以忽略，而"每块重建"
            保证它永远与磁盘上那份一致。

        `self._knowledge is None`（没有信息库/关库）→ 空表，一次 IO 都不做。
        """
        if self._knowledge is None:
            return {}
        out: dict[str, dict[str, int]] = {}
        for name in list(self._dynamics.states):
            try:
                table = self._knowledge._relations(name).table
            except Exception:                 # 兜底：读关系表绝不该打断这一块
                continue
            if not len(table):
                continue
            out[name] = {row.name: int(row.closeness) for row in table}
        return out

    def _refresh_relations(self) -> None:
        """把当前关系快照喂给动力学（每块一次，见 `_relation_closeness`）。"""
        self._dynamics.set_relations(self._relation_closeness())

    # ------------------------------------------- 进离场原因（§5.2）：一句话的去向 --
    async def _post_reason_note(self, name: str, reason: str,
                                *, leaving: bool, visible: bool = True) -> dict | None:
        """把一条进离场原因交给**当事人自己**（§5.2）：knows 限定的一行上下文，返回该行。

        三条边界：

          · **只发给当事人**：`knows=[name]`——别人一个字都看不到（§5.2 明写"不是所有离开
            都该被所有人知道"），公共播报行（`（X 离开了场景。）`）一字不改；
          · **不新增模型调用**：一行叙述、零 think/零 speak（确定性规则，与 §6.4 的进离场
            通知同一套做法）；
          · **留空 / 隐式一个字节都不落**：`reason` 空（含纯空白）直接返回 None；`visible=False`
            （隐式进出）同样不落——与 `_notify_cast_line` / `_post_entry_relation_notices`
            同一条 §4.2 口径：隐式事件整体不上屏（这一行虽然是私密的，它同样是一条会出现在
            对话框里的消息行）。

        **绝不带 `address`**（理由同 `_post_entry_relation_notices`）：带了就等于"这句话点名
        了谁"，被点的人当场欠下一笔 1.2 的硬义务（未答每块翻倍、进 `bid()` 不乘权重），
        倾向于是变成"决定结果"；文本也刻意用第二人称、不写当事人的名字（见 `_reason_note_text`）。

        "只发当事人"管的是**角色之间**：别的角色一个字都看不到（`knows=[name]`）。但
        **叙述者也不行**——它不做 knows 过滤（§4.1① 要它综观上下文），而它的产出是
        `knows=None` 的**公共**行：这件私事进了它的提示词，等于把 §5.2 交给一次模型发挥
        （它顺着写一句「甲说是要去见师父」，全体在场者立刻都有了）。故那一行的 id 记进
        `_reason_note_ids`，构叙述提示词时被剔除（见 `_narrator_live`；公开的播报行照旧
        在，不受影响）。§4 的通知行与 §6.4 的进离场通知是**世界发生了什么**，叙述者照旧
        看得到——本方法只管这一条当事人的私事。

        最后把 `(当前块号, 原因原文, 那一行的 id)` 记进 `_reason_notes`——这是"他手上有件
        没说出口的事"的唯一记账口，窗口从这里开始计时（`_unsaid_reasons`）。
        """
        text = str(reason or "").strip()
        if not text or not visible:
            return None
        line = await self._post_cast_line(
            _reason_note_text(text, name=name, leaving=leaving), knows=[name])
        message_id = int(line["id"])
        self._reason_notes[name] = _ReasonNote(delivered=int(self._blocks), text=text,
                                               message_id=message_id)
        self._reason_note_ids.add(message_id)
        return line

    def _narrator_live(self, live: list[dict]) -> list[dict]:
        """叙述者的视图 = 全部未撤销消息 **减去**「进离场原因」那些私密行（§5.2）。

        只动这一处（构叙述提示词）。叙述者照旧不做 `knows` 过滤（§4.1① 的既有口径：它要
        综观上下文才能判钩子、才谈得上"知道世界在发生什么"），唯一的例外是当事人亲手填下
        的私事——那一行的去向写在 §5.2 里："不是所有离开都该被所有人知道"，而叙述者写出
        来的每一个字都会落成公共行。

        没有原因时 `_reason_note_ids` 为空 → **原样返回同一个 list**（不留任何痕迹）：
        留空与今天逐字节相同。
        """
        if not self._reason_note_ids:
            return live
        return [m for m in live if int(m.get("id") or 0) not in self._reason_note_ids]

    def _unsaid_reasons(self) -> dict[str, dynamics_mod.UnsaidReason]:
        """本刻「还没说出口的原因」快照 `{当事人: 一件事}`（§5.2 那三项倾向的原料）。

        两道口径（§5.3 的时序约束就落在这里）：

          · **他只在场才算数**：人一走，竞价里根本没有他（不在 `_active`、不会 think），
            再留着这一行只会让还在场的人继续"想问"一件已经过去的事；
          · **窗口只有 `reason_grace_blocks` 块**：从交付那一刻算起（默认 2，0 = 关掉）。
            那件事还在他的记忆里，但不再压着他——这正是"他'想说'只能在走之前的一两块里
            发生"：一条十块以后才到期的离场预约，不会让他从现在起每块都在念叨这件事。

        `audience` = 当下在场的其它人；`named` = 原因**原文**里点到名的在场者（`observe` 判
        "被提到"用的也是这条朴素口径：名字出现在文本里）。判点名用原文而不是消毒后的交付
        文本：消毒只解决"当事人自己被点名"，与"他打算把这件事说给谁听"无关。

        账本里没有这一条（撤回销了账、或还没有过原因）→ 空表；**没有原因时整个方法一步
        不迈**（`_reason_notes` 为空即返回），与今天逐位相同。
        """
        if not self._reason_notes:
            return {}
        grace = self.reason_grace_blocks
        present = list(self._active)
        out: dict[str, dynamics_mod.UnsaidReason] = {}
        for name, note in self._reason_notes.items():
            if name not in present:
                continue
            if grace <= 0 or self._blocks - note.delivered >= grace:
                continue
            audience = tuple(n for n in present if n != name)
            if not audience:
                continue
            named = tuple(n for n in audience if n and n in note.text)
            out[name] = dynamics_mod.UnsaidReason(audience=audience, named=named)
        return out

    def _refresh_reasons(self) -> None:
        """把「还没说出口的原因」快照喂给动力学（每块/每次人事变动后一次）。"""
        self._dynamics.set_unsaid_reasons(self._unsaid_reasons())

    def _forget_retracted_reasons(self, retracted: Sequence[int]) -> None:
        """撤回之后把"从没发生过"的那笔原因账销掉（§5.2 的账要跟着转录走）。

        撤回的既有契约是"一切喂给模型的东西必须同意那段下文从没存在过"。原因行被撤了，
        而 `_unsaid_reasons` 只看窗口与在场，不看那一行还活着没有——模型于是会继续按一件
        已被撤回的事决定谁开口，且这是**确定性**的（界面上看不出任何痕迹）。故凡 note 的
        那一行在撤回名单里，就把这笔账删掉并立刻刷新动力学；一行都没撤到就一步不迈。
        """
        dead = {int(i) for i in retracted}
        kept = {name: note for name, note in self._reason_notes.items()
                if note.message_id not in dead}
        self._reason_note_ids -= dead
        if len(kept) != len(self._reason_notes):
            self._reason_notes = kept
            self._refresh_reasons()

    def _advance_dynamics(self, n_before: int, think_before: int, st: dict) -> str | None:
        """step 末的数值动态推进（§7.4 state_{t+1}=state_t+effect(chunk_t)），返回本块说话人。

        (a) 把本块 think 日志新增条目逐听众 observe() 折进 Dynamics——听的是本块
            开始时已存在的最新消息（heard content 逐听众、address 取本块前尾条）；
            被这句点名/提及的听众在此记下一笔**欠答义务**（pending_reply）；
        (b) 本块有角色产出 → mark_spoke(说话人)（他答了 → 其义务清零），否则（静默块）
            tick_silence()——沉默压力随之在久未开口者身上累积，且**仍欠答者的义务
            每轮翻倍**（未答越久，bid 越高，直到拿回话筒作答）。
        发言仲裁发生在**下一块**的 deliberate（bidder 闭包读 Dynamics）——一块的
        感知/演化延迟，正是"听完→想→开口"的自然节奏。

        返回值 = 本块开口的角色名（无则 None）：**唯一**的"本块有没有人说话"判据，
        前件闸（§4.1 条件 1 的另一半）与这里共用同一个判断（见 _observe_time_skip）。
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
        # 本块听到的那一句是**谁**说的（§6.4 的 address_boost 要知道点我的人是谁；
        # 说不出是谁（没有 heard）时给 None → last_addresser 为空 → 那一项恒 0）。
        heard_speaker = heard.get("speaker") if heard else None
        for entry in self._think_log[think_before:]:
            result = entry.get("result") or {}
            self._dynamics.observe(
                entry["speaker"], result,
                addressed_to=heard_addr,
                content=entry.get("heard"),
                speaker=heard_speaker)
        if spoke is not None:
            self._dynamics.mark_spoke(spoke)
        else:
            self._dynamics.tick_silence()
        return spoke

    def set_clock_now(self, seconds: int) -> None:
        """worker 每块步进前注入当前虚拟钟秒数 → 场景压力随到点临近演化。"""
        self._clock_now = int(seconds)

    # ------------------------------------------------ 信息库：散场结算（§7） ----
    # 分工（§7.4）是硬的：**引擎只准备、界面才决定**。引擎跑在自己的线程/事件循环里，
    # 绝不弹窗、不问卷——它写散场总结、收本场所得、把清单算好（prepare_settlement），
    # 并把「待决」事件挂进事件流；界面/CLI 拿到事件后问用户，再把「保留/丢弃」回传
    # （apply_settlement）。触发点是 `step` 里那块跑完的收束跳变（见那里），因为只有它
    # 同时覆盖 GUI、CLI、块钟兜底三条路。
    def _settlement_cast(self) -> list[str]:
        """本场**在册**的角色（在场 + 已离场），按演员表原序去重（与 `_truncate_memory` 同口径）。

        离场者也在其中：他的副本从没被删（§7.3 冻结快照），账本里还记着他的本场所得，
        散场时要在清单里，也随时可被单独结算。
        """
        return list(dict.fromkeys([*self._active, *self._inactive]))

    def _copy_dir(self, name: str) -> Path:
        """本场副本目录（`run_root/<角色名>/library`；换算只走存储层那一处）。"""
        return knowledge_store_mod.scene_library_dir(self.run_root, name)

    def _own_dir(self, name: str) -> Path:
        """角色本体库目录；没有信息库根时退回副本路径（`collect_gain` 会当作空库）。

        `libraries_root` 缺省 None = 这个角色没有本体库——那时 `self._knowledge` 也是 None，
        本节的公开方法都在第一步就返回了，这里只为把"没有库"表达成一个合法路径。
        """
        if self._libraries_root is None:
            return self._copy_dir(name)
        return knowledge_store_mod.library_dir("character", name, root=self._libraries_root)

    def _collect_gain(self, name: str) -> settle_mod.SceneGain:
        """收一次这个角色的本场所得（§7.2）。`collect_gain` 自己声明"绝不抛"。"""
        return settle_mod.collect_gain(
            self._copy_dir(name), self._own_dir(name),
            scene=self._scene_space, character=name, present=self._active)

    async def prepare_settlement(self) -> dict[str, settle_mod.PendingGain]:
        """准备散场结算（§7.1/§7.4）：写散场总结 → 收本场所得 → 返回待决清单。**幂等、绝不抛。**

        对每个「本场有所得」的角色做三件事（顺序是硬的，见 `knowledgesettle` 模块 docstring）：

          1. **先收一次**所得（拿清单去写总结——总结要链接的就是这批条目）；
          2. 用 **narrate 档后端**（§13.3 拍板：复用 narrate 档，不新增第四档）调
             `prompters.build_summary_messages`，把产出写成一条**枢纽条目**写进**本场副本**
             （走 `KnowledgeAccess` 的写路径，于是自然带上 `origin.kind="scene"` 与当前轮次，
             也自然被算进"本场所得"）；
          3. **再收一次**所得——总结条目也是本场长出来的东西（§7.1"存入索引表"），
             不收它枢纽条目就永远不会并进本体。

        两个前提判据，都在第一件事之前问过：

          · **已经结过账的副本不再准备**（`pending.json` 的 `settled`，§7.3/§7.5）。用户从
            菜单先单独结了某个人、或者这个副本本身就是上一轮结过的（CLI 缺省运行根
            `runs/` 是跨次共享的），先到先得已经把这一场封住了：再写一条总结只会留在副本里
            当孤儿（`settle` 见 `settled=True` 直接 `repeated`），白烧一次模型调用，还会把
            已经结清的人在待决事件里再列一遍——而同一时刻本方法的返回值（`pending_settlement`
            按 `is_pending` 过滤）里没有他，两处真相打架。
          · **收不上来的所得不静默**：`collect_gain` 的读盘警告（副本里那条条目文件读不出来）
            是"他的库少了一条"的唯一提示，带上角色名进 `settlement_warnings()`。他的所得
            既然是空的，就不进清单、也不写总结（没东西可链），但那一笔账**仍旧待结算**——
            不替他标掉：修好文件之后还能结。

        **幂等**（§7.5）：`_settlement_prepared` 是本场的闸门，连调两次第二次是只读回报
        （绝不再调模型、绝不再写一条总结）——键里的 N 是按"已有几条枢纽条目"算的，
        第二次照算就会写出 第2场、第3场…，用户的清单里凭空多出几条假总结。闸门只在
        **这一场该写的总结都处理完**之后关上：一次什么都没有的调用（比如调用方在戏还没
        演起来时先问了一声）不该把这一场的准备机会锁死，一次**没写成**（后端超时/限流）
        也不该——生产的调用次序（收束跳变里一次 + CLI 收束后一次，runner.py:208）正好
        留着这次免费的重试。重试只补没写成的人（`_summary_done` 记着谁已经写完），
        绝不重写已经写成的那条。

        **模型调用失败绝不打断结算**（§7.4：宁可没有枢纽条目，也不能让用户点不了「保留」）：
        记一条 warning（日志 + `settlement_warnings()`，界面/CLI 该看得见）、跳过这条总结，
        所得照常待结算。一个人失败也不影响别人（逐个兜住，绝不外抛）。

        没有信息库、没有所得时返回**空映射**（连一次模型调用都不发生）。
        """
        if self._knowledge is None:
            return {}
        if self._settlement_prepared:
            return self.pending_settlement()
        self._settlement_gains = {}
        handled = False        # 这一轮有没有"该写的总结"被处理掉（真的写了，或写也白写）
        retryable = False      # 有人的总结这次没写成、值得再打一枪吗
        for name in self._settlement_cast():
            try:
                state = self._pending_gain_of(name)
                gain = self._collect_gain(name)
                self._note_gain_warnings(name, gain)
                if gain.is_empty:
                    continue                  # 本场什么都没得到：不写总结、不烧一次调用
                if state.settled:
                    # 这一场已经结过账（先到先得）：不写总结（写了也永远并不进去）。
                    # 但结账**之后**又记下的那些必须说出来——它们卡在副本里了，见那里。
                    self._note_stuck_gain(name, gain, state)
                    continue
                if name not in self._summary_done:
                    if await self._write_scene_summary(name, gain):
                        self._summary_done.add(name)
                        handled = True
                    else:
                        retryable = True
                self._settlement_gains[name] = self._collect_gain(name)
            except Exception as exc:          # 最后一张网：一个人的意外不牵连其余人
                retryable = True
                self._note_settlement_warning(
                    f"「{name}」的散场结算准备出了意外（{type(exc).__name__}: {exc}）："
                    f"他这一场的所得仍是待结算，其余人照常。")
        if handled and not retryable:
            self._settlement_prepared = True
        return self.pending_settlement()

    def pending_settlement(self) -> dict[str, settle_mod.PendingGain]:
        """只读查询：现在有谁待结算、各自几条（§7.3）。**不写盘、不调模型、绝不抛。**

        转调 `knowledgesettle.pending_gain`（读 `pending.json` 那两份清单 + 结算状态）。
        面板每刷新一次就会调它一次，故它**绝不写任何文件**——写盘等于把提示变成动作。
        已结算过的人不在结果里（`is_pending` 是那个问题的答案）。
        """
        if self._knowledge is None:
            return {}
        out: dict[str, settle_mod.PendingGain] = {}
        for name in self._settlement_cast():
            pending = self._pending_gain_of(name)
            if pending.is_pending:
                out[name] = pending
        return out

    def _pending_gain_of(self, name: str) -> settle_mod.PendingGain:
        """读一个角色的待结算状态（§7.3，**只读**：不改任何文件）。

        清单两半与结算状态照 `pending_gain` 来，场景名/角色名改用**引擎自己**的那份：
        那一处是从路径推断的（`runs/<场景名>/<角色名>/library`），只有"运行根按场景名
        建目录"时才猜得对——GUI 的运行根是 `app-<uuid>`（每场唯一），猜出来的是随机串，
        晒给用户就是"这场戏叫 app-3f9a1c2d"。
        """
        pending = settle_mod.pending_gain(self._copy_dir(name))
        return settle_mod.PendingGain(
            scene=self._scene_space or pending.scene, character=name,
            added=list(pending.added), revised=list(pending.revised),
            settled=pending.settled, outcome=pending.outcome)

    def settlement_gains(self) -> dict[str, settle_mod.SceneGain]:
        """上一轮 `prepare_settlement()` 收出来的本场所得（**只读**，供界面/CLI 展示清单）。

        清单要给人看的是标题（"他这一场得了什么"），而 `prepare_settlement` 的返回是
        待结算的键清单——标题在这份所得里。返回浅拷贝：调用方拿走之后引擎还会继续跑。
        """
        return dict(self._settlement_gains)

    def settlement_warnings(self) -> list[str]:
        """结算准备阶段攒下的警告（**只读**）：最要紧的一条是"某人的散场总结没写出来"。

        它不会打断结算（§7.4），但用户该知道——少了那条枢纽条目，他日后少一个进图的口。
        开场轮换归档没做成的那些也在这里（§5.1）：那句"这一场新记下的东西并不进本体"
        同样是用户必须知道的事，而它记在构造期、**开场清不掉**（见 `_note_copy_rotation_warning`）。
        关系表读不出来的那些同样在（§6.2，见 `_note_relations_warning`）：那是工具层够不到
        的一类问题，界面/CLI 的这份清单就是它唯一的出口。
        """
        return (list(self._settlement_warnings) + list(self._copy_rotation_warnings)
                + list(self._relations_warnings))

    def apply_settlement(self, decisions: dict[str, str]) -> dict[str, settle_mod.SettleReport]:
        """按用户的决定结算这一场（§7.2/§7.4/§7.5）：保留 → 并入本体，丢弃 → 什么都不并。

        `decisions` 是「角色名 → "keep" | "discard"」（也认中文「保留」/「丢弃」）。
        未出现的角色**不结算**——留在待结算（§7.3 允许稍后再结）；认不出来的决定同样
        不结算并说清楚（**绝不猜成"丢弃"**：那和"保留"一样不可逆）。

        幂等（§7.5）：重复调用不改结果，先到先得由 `knowledgesettle.settle` 保证。
        **绝不抛**：单个角色失败（IO 错、坏文件）只进那一份报告的 warnings，别人照常结。

        所得在结算这一刻**现收一次**（而不是用 `prepare_settlement` 存下的那份）：这样
        §7.3 的"随时从菜单单独结算某个已离场的人"不必先跑一次准备，也不怕两次之间副本
        被别处改过。
        """
        if self._knowledge is None:
            return {}
        out: dict[str, settle_mod.SettleReport] = {}
        known = set(self._settlement_cast())
        for name, raw in decisions.items():
            choice = _settle_choice(raw)
            if choice is None:
                out[name] = settle_mod.SettleReport(warnings=[
                    f"「{name}」的决定「{raw}」认不出来（只认 keep/保留 与 discard/丢弃）："
                    f"这一场没有结算，仍留在待结算。"])
                continue
            if name not in known:
                out[name] = settle_mod.SettleReport(warnings=[
                    f"「{name}」不在这一场的演员表里，没有结算。"])
                continue
            try:
                gain = self._collect_gain(name)
                report = settle_mod.settle(
                    self._copy_dir(name), gain, self._own_dir(name),
                    keep=choice == "keep")
                # 关系表复用同一套散场结算（《人际关系与场景推进》§6.5）：保留 → 本场改过的
                # 那几条并进本体；丢弃 → 一个字节都不写。**先到先得也管它**（§7.5）：这一场
                # 已经结过、或条目那边没能落盘（`persisted=False`，用户还要重试）时不动它，
                # 跟着下一次一起结——否则会出现"关系并进去了、条目没并"的半场账。
                if not report.repeated and report.merged.persisted:
                    relation = knowledge_store_mod.settle_relations(
                        self._copy_dir(name), self._own_dir(name), keep=choice == "keep")
                    report.warnings.extend(relation.warnings)
                    if not relation.persisted:
                        self._note_settlement_warning(
                            f"「{name}」这一场改过的关系没能并进他的关系表（见上）："
                            f"它仍留在本场副本里，没有丢。")
                out[name] = report
                # 记下"结账那一刻账本上有哪些键"：他之后再记的条目就永远并不进来了
                # （§7.5 先到先得），那些要说出来（`_note_stuck_gain`）。
                self._settled_here[name] = set(gain.keys())
            except Exception as exc:               # 最后一张网：一个人失败不牵连别人
                out[name] = settle_mod.SettleReport(warnings=[
                    f"「{name}」的结算没能完成（{type(exc).__name__}: {exc}）："
                    f"这一场仍是待结算状态，处理好之后可以再来一次。"])
        return out

    async def _prepare_on_close(self) -> list[dict]:
        """收束跳变时的引擎侧动作（§7.4）：准备结算 + 把「待决」事件挂进本块事件流。

        没有可结算的所得 → 空表（清单是"要看的东西"，空清单只是一条噪声事件）。
        """
        await self.prepare_settlement()
        rows = self.settlement_rows()
        return [ev.settlement_pending(rows)] if rows else []

    def settlement_rows(self) -> list[dict]:
        """本轮 `prepare_settlement()` 收出来的待决清单，摊成**事件同形的行**（§7.2/§8.3）。

        形状由 `events.settlement_row` 一处定死（`settlement_pending` 事件用的就是它）：
        「待决事件」与「界面直接读一份清单」两条路因此不可能给出不同的数——界面照抄即可，
        不必自己从 `settlement_gains()` 里数一遍。

        只认 `_settlement_gains`（**真收得上来**的所得）：`pending_settlement()` 里那些
        "账记着但这次读不出来"的人不进清单——没有可看的东西，列出来只会让人以为有得选。
        没有信息库、还没准备过 → 空表（界面据此**不弹窗**，绝不打扰）。
        """
        return [ev.settlement_row(name=name, scene=gain.scene or self._scene_space,
                                  added=len(gain.added), revised=len(gain.revised),
                                  titles=[e.title or e.key
                                          for e in [*gain.added, *gain.revised]])
                for name, gain in self._settlement_gains.items()]

    async def _write_scene_summary(self, name: str, gain: settle_mod.SceneGain) -> bool:
        """写一名角色的散场总结（§7.1）——本场的**枢纽条目**。失败只记警告，绝不打断结算。

        返回值回答的是"这个人**下次还要不要再写**"（`prepare_settlement` 的闸门与重试判据）：

          · `True` = 不必再写：写进去了，或者**重试也不会变**（没有角色卡）；
          · `False` = 这次没写成、**值得再打一枪**（模型调用失败、写进去了却没被收下）——
            闸门因此不关死，下一次 prepare 只补这个人（§7.5 幂等的用处正是让重试安全；
            生产的调用次序里 CLI 收束后那次 prepare 就是那次免费的重试）。

        键与标题由引擎定（模型的自由度只在正文里），因为它们是**确定性**的：键
        `场景-<场景名>-第N场`（N = 该角色库里已有的同场景枢纽条目数 + 1，不引入新状态）、
        标题 `《<场景名>》那一夜`。同一场结算两次要能写出同一个键，否则就是两条重复的总结。

        正文末尾的 `[[条目名]]` 双链由提示词要求模型写（`build_summary_messages`），
        **模型漏了引擎补**：枢纽条目的价值全在链接上，断了图就散一片。已经写了的那条不重复补。
        补上的链接还会在写完之后**验一遍**（`_hub_links_missing`）：写路径对正文是截尾
        （`MAX_BODY_CHARS`），`_with_hub_links` 已经先把正文压到留得下链接，但链接自己就
        超上限（本场条目多到一行放不下）这类极端仍会把某条截掉——那时候必须说出来。
        """
        card = self.cards.get(name)
        if card is None:
            self._note_settlement_warning(f"「{name}」没有角色卡，这一场的散场总结没有写。")
            return True                   # 重试也不会凭空长出一张卡来
        key = self._summary_key(name)
        messages = build_summary_messages(
            card, await self._summary_view_text(name), self.scene.name,
            entry_digest=gain.digest(), present=self._active,
            language_directive=self._language_directive)
        try:
            raw = await self.narrate_backend.complete_text(messages)
        except Exception as exc:
            self._note_settlement_warning(
                f"「{name}」的散场总结没写出来（{type(exc).__name__}: {exc}）："
                f"这一场的所得照常待结算，只是少了一条枢纽条目。")
            return False                  # 后端抽风：下次 prepare 值得再打一枪
        knowledge = self._knowledge
        if knowledge is None:                     # prepare 已保证非 None；这里是类型收窄
            return True
        try:
            links = gain.keys()
            knowledge.execute(
                name, "remember",
                {"key": key, "title": f"《{self.scene.name}》那一夜",
                 "summary": _summary_gist(gain, self._active),
                 "body": _with_hub_links(raw, links)},
                turn=self._turn)
            after = self._collect_gain(name)
            entries = [*after.added, *after.revised]
            written = {e.key for e in entries}
            if key not in written:
                # 写进去了却没被收下（键不合法、副本读不出来……）：说出来，别让用户以为
                # 枢纽条目已经在了。写路径本身绝不抛，故这里只可能是"没写成"。副本这一时
                # 读不出来是**可以再试**的，故交回闸门（下一次 prepare 只补这个人）。
                self._note_settlement_warning(
                    f"「{name}」的散场总结没能写进本场副本（条目名「{key}」没被收下）："
                    f"这一场的所得照常待结算，只是少了一条枢纽条目。")
                return False
            missing = _hub_links_missing(next(e for e in entries if e.key == key), links)
            if missing:
                # 条目在、链接被截没了：枢纽的价值全在链接上，而这种残缺无痕——不说出来
                # 用户看到的就是一条"正常的"总结，角色日后从它进图却摸不到任何东西。
                self._note_settlement_warning(
                    f"「{name}」的散场总结正文太长，双链被写路径截掉了一段"
                    f"（{'、'.join(missing)}）：枢纽条目还在，但图上少了这几条边。")
        except Exception as exc:
            self._note_settlement_warning(
                f"「{name}」的散场总结没能写进本场副本（{type(exc).__name__}: {exc}）："
                f"这一场的所得照常待结算，只是少了一条枢纽条目。")
            return False
        return True

    def _summary_key(self, name: str) -> str:
        """枢纽条目的键：`场景-<场景名>-第N场`，N = 该角色库里已有的同场景枢纽条目数 + 1。

        **确定性、不引入新状态**（不记"这是第几场"）：键前缀就是同一场景的口径，数一数
        副本里有几条（上一场留下的也算——否则两条总结会撞成同一个文件、后写的顶掉先写的）。
        """
        prefix = f"场景-{self._scene_space}-第"
        try:
            keys = knowledge_store_mod.load_library(self._copy_dir(name)).library.ordered_keys()
        except Exception:                      # load 声明绝不抛，这里是最后一张网
            keys = []
        return f"场景-{self._scene_space}-第{sum(1 for k in keys if k.startswith(prefix)) + 1}场"

    async def _summary_view_text(self, name: str) -> str:
        """这名角色**看到的整场对话**（与 think 同一来源：可见转录，不是上帝视角）。

        与 think 的口径逐条对齐：空间键用不可变空间（改名不动它）、按他的进场基线丢掉
        进场前的对话（§3.3）、被撤销的行不进视图。多一条：**已离场者只算到他离场那一刻**
        ——他走之后的事他没听见，写进他的一夜就是把别人的戏塞进他的记忆。
        """
        st = await self._snapshot()
        live = [Message.model_validate(m) for m in graph_mod._live_messages(st)]
        view = view_for(live, name, self._scene_space,
                        since_round=self._entry_round_of(name))
        left = self._left_id.get(name)
        if left is not None:
            view = [m for m in view if int(m.id) <= left]
        return render_view(view)

    def _note_stuck_gain(self, name: str, gain: settle_mod.SceneGain,
                         state: settle_mod.PendingGain) -> None:
        """报告"**结账之后**又记下、因而永远并不进本体库"的那些条目（§7.4 不静默）。

        副本结过账之后 `settle` 一律 `repeated`（§7.5 先到先得是硬的：一次手滑的重复点击
        绝不能把"丢弃"翻成"保留"），于是这之后写的条目卡在副本里出不去。两条来路都会走到
        这里：CLI 用共享的缺省运行根 `runs/` 再跑一次同一场（副本是逐场一份的），以及用户
        从菜单先单独结了某个人之后他接着又记。两者都不是引擎能替用户决定的事，但**必须
        说出来**——不说，用户看到的是一次什么都没发生的收尾。

        只列**这次结账之后**才出现的键：结账那一刻账本就有的那些，当场已经并进本体
        （或按当时的选择丢弃）了，再报一遍只是噪声——§7.3 那条正常路会天天被它吵。
        """
        settled_keys = self._settled_here.get(name)
        fresh = [k for k in gain.keys() if settled_keys is None or k not in settled_keys]
        if not fresh:
            return
        outcome = {"keep": "保留", "discard": "丢弃"}.get(state.outcome, "结果没记下来")
        self._note_settlement_warning(
            f"「{name}」这一场的副本已经结过账了（{outcome}），他之后又记下的 "
            f"{len(fresh)} 条（{'、'.join(fresh)}）不会再并进本体库——先到先得（§7.5）。"
            f"要让他把这些东西永久记下，得另起一场：副本是逐场一份的。")

    def _note_gain_warnings(self, name: str, gain: settle_mod.SceneGain) -> None:
        """把一次 `collect_gain` 的读盘警告带上角色名记进结算警告（**绝不吞**）。

        `SceneGain.warnings` 是"他的库少了一条"的**唯一提示**（`knowledgesettle` 模块
        docstring 第 5 条）：副本里那份条目文件读不出来时，那条所得既收不进清单、也并不进
        本体，而 `pending.json` 仍记着他有待结算。不说出来，用户看到的就是一次"什么都正常、
        就是没人问我"的收尾——东西卡在副本里，没有人告诉他为什么。
        """
        for line in gain.warnings:
            self._note_settlement_warning(f"「{name}」：{line}")

    def _note_settlement_warning(self, text: str) -> None:
        """结算准备的警告：进日志（用户唯一的知情渠道）+ `settlement_warnings()`（界面/CLI 可读）。

        同一条**不重复记**：闸门为"总结没写成"保持开着时（下一次 prepare 只补那个人），
        同一句读盘警告会被逐轮再看见一遍，攒成一屏重复的话就没人读了。
        """
        if text in self._settlement_warnings:
            return
        self._settlement_warnings.append(text)
        logger.warning("%s", text)

    def _note_pending_settlement(self, name: str) -> None:
        """离场挂起（§7.3）：只把「这位有一笔本场所得待结算」挂进只读事件流。

        **不弹窗、不做模型调用、不写任何文件**（`pending_gain` 是只读查询）——他正在看的
        戏不该被一个模态框拦住，正在进行的块也不该因此多花一次调用。他随时可被单独结算
        （`apply_settlement({name: "keep"})`），散场时也会连同其余人一起结。

        行的字段由 `events.character_pending_settlement` 一处产出（`**event["payload"]`），
        本方法**不自己拼那份 dict**：手搓一份就等于让"给界面看的行"在仓库里长成两个各自
        演化的形状，将来按 `events.py` 写的消费方（CLI 面板、诊断工具）读到的字段名会和
        真实事件流对不上——而且是静默对不上。

        信封（`hook_id` / `event_kind` / `content` / `visible` / `turn` / `blocks`）是
        **隐式事件流**的统一外形：离场发生在块的中间（`remove_character`），而块的事件表
        在它之前就返回了，所以要挂到只读事件流这条随时可读的通道上；消费方（worker）按
        `event_kind` 认它。这正是 `events.character_pending_settlement` 那个
        `{"type", "payload"}` 外形进不来的原因——两条通道的信封本来就不一样，故这里取
        它的 `payload` 铺进隐式事件流，而不是把整个事件对象塞进去。
        """
        if self._knowledge is None:
            return
        pending = self._pending_gain_of(name)
        if not pending.is_pending:
            return
        event = ev.character_pending_settlement(ev.settlement_row(
            name=name, scene=pending.scene,
            added=len(pending.added), revised=len(pending.revised),
            titles=self._pending_titles(name, [*pending.added, *pending.revised])))
        self._implicit_events.append({
            "hook_id": "", "event_kind": "pending_settlement",
            "content": (f"（{name}离场：他这一场记下的东西还没结算——"
                        f"新增 {len(pending.added)} 条、修订 {len(pending.revised)} 条；"
                        f"散场时或随时可以单独结。）"),
            "visible": False, "turn": self._turn, "blocks": self._blocks,
            **event["payload"]})

    def _pending_titles(self, name: str, keys: Sequence[str]) -> list[str]:
        """待结算的键 → 事件行里**给人看**的标题（读不出来就退回键本身）。

        `PendingGain.added/revised` 是**键**清单（`pending.json` 里就只记了键）——而清单
        行是给用户看的（"他这一场得了什么"），键（`场景-茶室-第1场`）是给图和文件用的。
        与 `settlement_rows()` 同一口径：那一处拿的是真条目，这里从本场副本的条目里现查
        一次标题。查不到（副本读不出来/条目不在）不报错，退回键——**有东西可看**比
        "整行空着"强，而这一条提示本来也不该因为一次读盘失败而不出现。
        """
        try:
            entries = knowledge_store_mod.load_library(self._copy_dir(name)).library.entries
        except Exception:                      # load 声明绝不抛，这里是最后一张网
            entries = {}
        out: list[str] = []
        for key in keys:
            entry = entries.get(key)
            title = str(getattr(entry, "title", "") or "").strip()
            out.append(title or str(key))
        return out

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
        visible}（§5.1 的进离场原因带 `reason` 时才多这一个键）；每 step() 一块扣 1，扣到
        0 即执行并从队列移除（hooks.tick 记账）。`reason` 是给日志/回执用的——它只说明
        "这次人事变动带着一个原因"，原因本身早已交给当事人自己（见 `schedule_cast_change`）。
        **空原因不出现这个键**：回执是队列的序列化，留空时它与今天**逐字节相同**（铁律 3），
        按固定键集解析回执的下游不会看到一个恒为空串的新字段。
        """
        rows: list[dict] = []
        for p in self._pending_cast:
            row = {"character_name": p.character_name, "action": p.action,
                   "turns": p.turns, "fire_after_rounds": p.fire_after_rounds,
                   "notify": list(p.notify), "notify_text": p.notify_text,
                   "visible": p.visible}
            if p.reason:
                row["reason"] = p.reason
            rows.append(row)
        return rows

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

    async def _post_entry_relation_notices(self, entering: str, *,
                                           visible: bool = True) -> list[dict]:
        """B 进场时，把「他来了」这件事告诉心里有他的人（§6.4 第三行）。返回落下的行。

        规则是**确定性的**（§6.4 明写"不是让模型判断"）：逐个在场者读**他自己的**关系表，
        取出他对进场者的那一行，交给 `dynamics.entry_notice_text` 判要不要说——
        高于正阈值 = 他在我心里有分量；低于负阈值 = 我心里一沉；中间地带与没有那一行
        一个字都不说。**一个后端请求都不发**（没有 think、没有 speak）：多一次 think 就是
        整块的钱，而"他来了"这件事根本不需要问模型。

        落行复用既有的人事通知机制（`_post_cast_line(knows=[...])`）：speaker 是场景、
        `knows` 只含那一个人——这是**私密**的，别人看不到我心里动了。

        **绝不带 `address`**（铁律 4 的落点，别顺手加回来）：`_advance_dynamics` 把尾条的
        `address` 当作"这一句点名了谁"喂给 `Dynamics.observe`，而 `observe` 判
        `mentioned = ... or addressed_to == listener`。一条"他来了"的通知若带上被通知者的
        名字，就等于**他点名问我**——被通知者当场欠下一笔 `pending_reply = 1.2` 的硬义务
        （未答每块翻倍、封顶 64、进 `bid()` 不乘任何权重），bid 从 0.18 跳到 3.1（开口阈值
        0.1 的三十倍、打断阈值 0.4 的八倍），且正负对称（−80 的仇人与 +80 的分量同效）。
        那正是 §6.4 与铁律 4 排除的"决定结果"：通知只该是**一行上下文**，亲密度只能走
        `bid()` 里那两个加权项（上界 0.3 < 打断阈值）。可见性由 `knows` 保证，`address`
        在这里只贡献"被点名"这个错误语义（还会被 graph 渲染成 `→被通知者` 喂给模型）。

        三条收口：
          · `visible=False`（隐式进出）**连通知行也不落**——与 `_notify_cast_line` 同一条
            §4.2 口径：隐式事件整体不上屏，"心里一沉"也不能把它捅出来；
          · `self._knowledge is None`（没有信息库/关库）→ 空表，一条不发，一次 IO 不做；
          · 进场者自己跳过（表里写着自己一行时不该自己通知自己）。
        """
        if not visible or self._knowledge is None:
            return []
        rows: list[dict] = []
        for name in list(self._active):
            if name == entering:
                continue
            try:
                table = self._knowledge._relations(name).table
                row = table.get(entering)
            except Exception:                 # 兜底：读关系表绝不该打断进场
                continue
            if row is None:
                continue
            text = dynamics_mod.entry_notice_text(int(row.closeness), entering,
                                                  self.dynamics_params)
            if not text:
                continue
            rows.append(await self._post_cast_line(text, knows=[name]))
        return rows

    async def add_character(self, name: str, *, notify: list[str] | None = None,
                            notify_text: str = "", visible: bool = True,
                            reason: str = "") -> dict | None:
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
        · notify 非空时另落一条**只发给这些名字**的通知行（内容 notify_text 或默认）；
        · reason 非空时另落一条**只发给他自己**的原因行走上下文（§5.2）——留空即今天。
        """
        name = self._require_card(name)
        if name in self._active:
            return None
        # 他**第一次进入这一场**（从没在场、也没离过场）→ 遗留的已结算副本要轮换归档
        # （§5.1 一条戏一份副本）：那时他手上那份只可能是上一场的账本，留着它这一场的所得
        # 永远并不进本体。已经在这一场里出现过又回来的（`_inactive` 里有他）不算——那份副本
        # 是**这一场自己的工作记忆**，即使结算过也不该被换掉（见 `_open_knowledge_copy`）。
        first_entry = name not in self._inactive
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
        self._left_id.pop(name, None)            # 他回来了：散场总结照旧算到散场那一刻
        self._open_knowledge_copy(name, first_entry=first_entry)   # 进场即建副本（§5.1）
        self._ensure_dynamics(name)
        self._refresh_relations()          # §6.4：新面孔的关系表也要进快照
        line = await self._post_cast_line(f"（{name}走进了场景。）") if visible else None
        await self._notify_cast_line(name, notify, notify_text, visible)
        # §6.4 的进离场通知：心里有他的人各自多一条**只发给自己的**那一行（确定性规则、
        # 零模型调用；无关系表的人一条都不落，见 _post_entry_relation_notices）。
        await self._post_entry_relation_notices(name, visible=visible)
        # §5.2 的进场原因：他为什么来，只交给他自己（放在最后 = 他视图里最新的一行；他
        # 已经进来、基线已定，这一行落得进去；留空/隐式一行不落）。
        await self._post_reason_note(name, reason, leaving=False, visible=visible)
        self._refresh_reasons()             # 新到手的原因当块就参与竞价
        return line

    async def remove_character(self, name: str, *, notify: list[str] | None = None,
                               notify_text: str = "", visible: bool = True,
                               reason: str = "") -> dict | None:
        """让一名角色离场（§3.3）：出在场名单 + 落一条可见播报行，返回该行（无行则 None）。

        **历史与私有记忆一律保留**（记录不删、转录不删、Dynamics 状态不重置），只是他
        不再 think、不再参与竞价、不再出现在左右栏；上下文里留下「谁何时离开了场景」
        这条事件行（visible=True 时）。禁言状态随离场清除（再入场是干净状态）。
        不在场 → 幂等空操作。name 无卡 → ValueError（同 add_character）。

        **离场挂起**（§7.3）：他有本场所得时**不弹窗、不做模型调用**（别打断正在进行的
        戏），只把「这位有一笔本场所得待结算」挂进只读事件流（见 `_note_pending_settlement`）；
        他随时可被单独结算，散场时也会连同其余人一起结。散场总结只算到他离场这一刻
        （他走之后的事他没听见，见 `_summary_view_text`）。

        reason（§5.2）：非空时先落一条**只发给他自己**的原因行（留空即今天）。直接调本方法
        的路径（钩子角色事件、界面上的即时移出）只有"现在"这一刻——他马上就要走了，这行
        是他走之前知道的最后一件事，故它落在他的散场边界**之内**（散场总结里有它）。
        **延时**离场不走这里交原因：那种情况在入队那一刻就已经交过了（那时他还在场、还有
        块可以开口），到点再交一次只是把一行他永远看不见的死行写进转录
        （见 `schedule_cast_change` 与 `_apply_cast_action`）。
        """
        name = self._require_card(name)
        if name not in self._active:
            return None
        st = await self._snapshot()
        self._active = [n for n in self._active if n != name]
        if name not in self._inactive:
            self._inactive.append(name)
        self._mute.pop(name, None)
        note = await self._post_reason_note(name, reason, leaving=True, visible=visible)
        if note is not None:
            # 他刚知道的这件事在他写下的记忆之内：边界重取一次（留空时一行不落、也不重取
            # ——今天的边界逐字节不变）。
            st = await self._snapshot()
        # 他这一场的戏到此为止：此刻**已经存在**的最后一条消息就是边界（离场播报行还没落）。
        self._left_id[name] = _max_message_id(st)
        line = await self._post_cast_line(f"（{name}离开了场景。）") if visible else None
        self._note_pending_settlement(name)     # §7.3：不弹窗、不调模型，只挂一条待结算提示
        await self._notify_cast_line(name, notify, notify_text, visible, leaving=True)
        self._refresh_reasons()                 # 他走了：他的那一项当场退场（§5.3）
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
                                   turns: int = 0,
                                   reason: str = "") -> dict:
        """预约一次**延时**角色动作（§2.1④ 高级移入/移出）：fire_after_rounds 块后执行。

        action ∈ hooks.CharacterAction（add/remove/mute_turns/mute/unmute）；turns 只在
        mute_turns 时用（**相对 S4a 规格的增补**：延时禁用 N 块必须能带上轮数，否则
        mute_turns 无从表达）。fire_after_rounds <= 0 视同 0（下一块末即执行）。
        每次 step() 一块扣 1（hooks.tick），到期即调对应方法执行——因此
        schedule(fire_after_rounds=2) 恰在第 2 次 step 的块末触发、第 1 次不动。
        返回入队的 dict（供日志/回执）；角色名无卡 → ValueError（当场拦下，不进队列）。

        reason（§5.1/§5.2）：进离场原因，随动作一起入队、一起到期。**交付时刻按方向分**：

          · **离场**：此刻就交给他（他还在场，原因这才有可能在"走之前的一两块"里起作用，
            §5.3）——到点执行时他已经不在场，视图/提示词里都不会再有他，那时落一行他永远
            看不见。他若此刻不在场（预约的是一次空操作），一行都不落；
          · **进场**：此刻不落（他还没进来，落了他也看不见——进场基线会把早于它的一切挡在
            外面），到期真正进场时由 `add_character` 交给他。
        """
        name = self._require_card(character_name)
        if action not in _CAST_ACTIONS:
            raise ValueError(
                f"未知的角色动作：{action}（应为 add/remove/mute_turns/mute/unmute）。")
        item = hooks_mod.PendingCharacterAction(
            character_name=name, action=action, turns=max(0, int(turns)),
            fire_after_rounds=max(0, int(fire_after_rounds)),
            notify=[str(n) for n in (notify or [])], notify_text=str(notify_text or ""),
            visible=bool(visible), reason=str(reason or ""))
        self._pending_cast.append(item)
        if action == "remove" and name in self._active:
            await self._post_reason_note(name, item.reason, leaving=True,
                                        visible=item.visible)
            self._refresh_reasons()         # 窗口从这一刻开始（§5.3）
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
        """执行一条到期的延时角色动作（dispatch 到 add/remove/mute/unmute）。

        原因（§5.3）：**进场**在这里交给本人（他刚进来、基线已定，这一行落得进去）；
        **离场**不在这里交——入队那一刻已经交过了（那时他还在场、还有块可以开口），到点
        再交一次就是一行他永远看不见的内容（`remove_character` 收不到这条 reason 是刻意
        的，别顺手补上）。
        """
        action = item.action
        if action == "add":
            return await self.add_character(item.character_name, notify=item.notify,
                                            notify_text=item.notify_text,
                                            visible=item.visible, reason=item.reason)
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

    def speak_stream_tail(self, n: int = 200) -> list[dict]:
        """speak 流式接收器尾部（缺省最近 200 条），返回浅拷贝。

        条目 = {seq, speaker, turn, kind, text, settled}；`kind` 两态：
          · "piece" 正在说的一个增量（text 是这一片正文）；
          · "end"   这一块说完了；`settled=True` = 有正式消息落地（GUI 等 block_spoken
            就地定稿），`settled=False` = 判为近重复整块作废（GUI 撤掉临时气泡）。
        seq 单调递增（接收器在 append 端按上限封顶裁剪，序号不受影响，GUI 据此增量派发）。

        与 `think_log_tail` 同一分工：引擎只持这个 list、只提供这个读口，**不弹窗、不碰
        Qt**（呈现归界面）；关闭流式时它恒为空（speak 走老路，一个条目都不追加）。
        """
        return list(self._speak_stream[-n:])

    def set_speak_stream(self, on: bool) -> None:
        """开关 speak 流式（§二．8）：**立即生效**（下一块起）。

        sync、只改一个旗标：接受者（图的 ctx）与引擎共用同一个 GraphContext 实例，故这里
        把旗标同时写到两份上——引擎那份供 `_ensure_graph` 重建图时带上，ctx 那份供**当前
        这张图**立刻生效（与 `set_language` 改活 ctx 同一套路，不必重建图/重开 sqlite）。
        ctx 还没建（未开场）时只记引擎那份，构图时自然带上。
        """
        self._speak_stream_on = bool(on)
        if self._ctx is not None:
            self._ctx.stream_speak = self._speak_stream_on

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
