"""LangGraph 1.x：感知/权衡/行动/世界 节点（技术方案 §2/§5.4/§16.1）。

M3 底版：fan_out → Send 扇出 think（每听众独立视图，worker 只见自己 payload）
→ fan-in 后 deliberate（纯代码竞价，读 urges 仲裁；urges 先与在场者求交防陈旧键）
→ 条件边 yield_to/incumbent_continues → speak（读 decided.speaker 出块），
  silence → silence_gate（连续静默达 K 块注入导演事件，否则静默结束本块）。
收尾（world）：speak/silence_gate 之后都经 world —— world 只做两层块级收束——
  · closing_at_block 块钟兜底安全上限（GUI/CLI/引擎都传大值缺省，如 200）；
  · 用户/worker 在引擎侧 close_scene() 置 closed（外部收束，见引擎 docstring）。
场景虚拟时间不再是图的共享态通道：改为 worker 在 GUI 侧按真实流逝 × 全局流速连续
计算（见 sceneclock），走到 time 硬边界即由 worker 调 close_scene() 自然收束——
图不持钟、角色开口/思考不增减时间、消息不带时刻戳（时刻由 worker 在派发时补
time_hhmmss，供界面逐行渲染 HH:MM:SS）。
节点以闭包捕获 GraphContext（节点签名只有 state，无其它上下文）。
共享态 messages 存普通 dict（可序列化/断言方便），需要按角色投影时在
visibility 边界转成 Message（view_for/render_view 只认 Message 属性）。

可插拔演员表（《场景编排与桌面外壳》§3.3，S4a）：谁在场是**运行期**事实，故扇出与
竞价一律经 ctx.active_names() 取名单（引擎注入 cast_provider；缺省=场景演员表），
刚进场者的视图再经 ctx.entry_round() 丢掉进场前的对话——进场者看不到历史，离场者
历史保留但不再 think、不再竞价、不再出现在左右栏（引擎侧状态见 SceneEngine.cast_state）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Callable, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from . import bidding as bidding_mod
from . import memory as memory_mod
from . import textsim
from .backends.base import ModelBackend
from .prompters import build_speak_messages, build_think_messages
from .schemas import CharacterCard, Message, Scene, ThinkResult
from .visibility import render_view, view_for

#: speak 近重复判定：与说话人自己最近 ≤3 条台词中任一条的相似度 ≥ 此值即视为复读
#: （不落盘、当作静默块），防模型逐字/几乎逐字自我复读烧 token。
_DUP_RATIO = 0.95
_OWN_RECENT_WINDOW = 3

# ---- reducer 区 ----
def _append_msgs(left: list[dict] | None, right: list[dict] | None) -> list[dict]:
    return (left or []) + (right or [])


def _merge_urges(left: dict[str, float] | None, right: dict[str, float] | None) -> dict[str, float]:
    return {**(left or {}), **(right or {})}


#: think 只读边通道封顶（条数）：UI 只读最近日志，旧条无人消费——丢最旧防无限增长。
#: 远大于 GUI 日志窗格上限(400)与每 500 块成本守卫的累积量级，正常绝不触发裁剪。
_THINK_LOG_CAP = 2000


class GraphState(TypedDict, total=False):
    messages: Annotated[list[dict], _append_msgs]
    retracted: list[int]          # 被用户撤销的消息 id（从历史里剔除，见 _live_messages）
    urges: Annotated[dict[str, float], _merge_urges]
    current_speaker: str | None
    turn: int
    blocks: int                # 世界层自己的块钟（I2：每块 +1，见 world）
    silent_streak: int
    decided: dict | None          # 仲裁结果 {kind, speaker, silent_streak}
    injected: list[dict]          # 世界层注入消息（Task12 起并入 messages）
    closed: bool                  # 收束标记（块钟兜底/world、引擎 close_scene 写 True）
    closing_at_block: int | None  # 收束块上限；None=不按块收束（App/引擎默认，唯一收束
                                  # 来源是虚拟钟到打烊或用户停止；仅测试显式设值才生效）


@dataclass
class GraphContext:
    """节点依赖（cards/scene/backends/run_root/bid_params）经闭包注入，不进节点签名。

    think_log：think 结果的**只读边通道**（仅供人类 UI 检视，绝不为角色互见）。
    think 是本听众的私有解读——不路由共享态（PUBLIC_KEYS 白名单外零新键），
    think worker 每完成一次就把 {seq, speaker, at, heard, turn, result} 追加到这里；
    引擎持有同一 list，`think_log_tail()` 供 GUI 读尾部。append 时按 _THINK_LOG_CAP
    丢最旧封顶；seq 为该引擎内单调递增序号（裁剪不影响 GUI 按 seq 增量派发）。

    cast_provider / entry_round_of：**可插拔演员表**（§3.3）的两只读探针，由引擎注入
    （缺省 None = 裸图语义，与引进场/禁言之前逐字节一致）：
      · cast_provider() → 本块可参与的人数名单（引擎返回「在场且未被禁言」者，按演员表
        原序）。扇出与竞价都只认它——被移出/被禁言者既不 think 也不参与竞价；
      · entry_round_of(name) → 该角色的**进场基线**（转录 turn 制；0 = 有史以来全见）。
        视图按它丢掉进场前的对话：刚进场者看不到此前发生过什么。
    两者都不进共享态、不落盘，纯读。

    language_directive：语言指令（§7），think/speak 直接作为 `language_directive=` 传给
    对应 builder（追加在系统消息末尾）。空串（裸图/缺省）= 提示词逐字节不变。
    """
    cards: dict[str, CharacterCard]
    scene: Scene
    think: ModelBackend
    speak: ModelBackend
    run_root: Path
    bid_params: bidding_mod.BidParams
    think_log: list = field(default_factory=list)
    think_seq: int = 0              # think 只读日志单调序号（每次 append +1）
    demo_alternate: bool = False    # CLI 演示交替叠加（demo-only，默认关闭）
    bidder: Callable[[str], float] | None = None   # 数值竞价源（引擎闭包进 Dynamics）
    cast_provider: Callable[[], list[str]] | None = None   # 在场且未禁言者（引擎注入）
    entry_round_of: Callable[[str], int] | None = None     # 进场基线（引擎注入）
    #: 语言指令（《场景编排与桌面外壳》§7：think/speak/narrate 全都要带「你将使用<语言>
    #: 回答。」）。由引擎在建图时注入（i18n.llm_language_directive(engine.language)）；
    #: 缺省 "" = 不追加，裸图/离线测试的提示词逐字节不变。
    language_directive: str = ""
    #: 在场轴（in_scene）的**不可变空间键**（C1 最终评审）：视图过滤只认它，场景改名
    #: （[[TOOL:set_name]] / 钩子 scene_patch={"name": …}）只改**显示名**（scene.name），
    #: 绝不改这个键——消息落盘时的 in_scene 与这里的 space 同源，改一次名就再也不会
    #: 把整场历史判成「不在本空间」。缺省 "" → 取 scene.name（裸图/离线测试逐字节不变；
    #: 引擎路径在建图时注入 __init__ 捕获的那个 id，见 SceneEngine._scene_space）。
    space: str = ""

    def __post_init__(self) -> None:
        if not self.space:
            self.space = self.scene.name

    def active_names(self) -> list[str]:
        """本块可参与的名单：引擎注入 cast_provider 时以它为准（在场且未禁言），
        缺省（裸图/离线测试）退化为场景演员表原序——既有行为一字不变。"""
        if self.cast_provider is not None:
            return list(self.cast_provider())
        return list(self.scene.participants)

    def entry_round(self, name: str) -> int:
        """该角色的进场基线；未注入读数（裸图）或查不到 → 0（= 全程可见）。"""
        if self.entry_round_of is None:
            return 0
        return int(self.entry_round_of(name) or 0)


def _live_messages(state: dict) -> list[dict]:
    """共享态里**未被撤销**的消息（retracted 里的 id 一律剔除）。

    撤销（用户对任意一行——尤其场景叙述——收回/改写）语义是「这行没发生过」：历史里
    不再存在，后续每一轮的任何视图/锚点/复读判定都不得再看到它。凡从共享态构视图或
    取最新一条的地方，都必须先过这一步（消息本体仍留在 messages 里供 UI 显示"已撤销"）。
    """
    gone = set(state.get("retracted") or [])
    return [m for m in (state.get("messages") or []) if m.get("id") not in gone]


def _next_id(messages: list[dict]) -> int:
    """下一条消息 id = 全量最大 id + 1（空态 → 1）。

    必须看**全量**（含被撤销的）而非尾条：撤销尾条后若按「尾条 id+1」取号，新消息会
    捡回一个已被撤销的 id，一落地就落在 retracted 里 → 永远不进任何视图。
    """
    return max((m.get("id") or 0) for m in messages) + 1 if messages else 1


def _is_visible_to(msg: dict, character: str) -> bool:
    return (msg["speaker"] == character
            or msg.get("knows") is None or character in msg["knows"])


def _msg_models(messages: list[dict]) -> list[Message]:
    """共享态 dict 消息 → visibility 需要的 Message 对象（纯投影，不改共享态）。"""
    return [m if isinstance(m, Message) else Message.model_validate(m) for m in messages]


def _now_iso() -> str:
    """think_log 条目的时间戳（UTC ISO，仅供人类 UI 时间线排序/展示）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _render_last_line(last: dict | None) -> str:
    """last_chunk dict → 归属单行文本，与 render_view 的行约定一致（`[id] 说话人: 内容`）。

    修复：think 的「刚听到这一句」必须带上是谁说的——说话人可能是别人、也可能是听众
    自己（续自己的话）。只用内容会丢归属，模型会把匿名句（甚至自己的话）当别人的话接。
    地址列（→点名）一并保留，与视图同一约定。空/无消息 → ''（由 build 落「开场」占位）。
    """
    if not last:
        return ""
    addr = f" →{last['address']}" if last.get("address") else ""
    return f"[{last['id']}] {last['speaker']}{addr}: {last['content']}"


def _render_recent_state(entries: list[dict], max_entries: int = 5) -> str:
    """本听众 state.jsonl 尾部 ≤5 条折成紧凑文本（唤醒/目标推进/点名/兑现义务）。

    只取 load_state() 的尾巴喂回下一次 think，让倾向随私有状态缓慢演变；
    渲染内容是纯私有（仅进本人 prompt），不涉及共享态。
    """
    rows: list[str] = []
    for e in entries[-max_entries:]:
        obligations = e.get("obligation_fulfilled") or []
        rows.append(
            "- 唤醒 {:.2f} ｜ 目标推进 {:.2f} ｜ 被点名 {} ｜ 兑现义务 {}".format(
                float(e.get("aroused", 0.0) or 0.0),
                float(e.get("goal_progress", 0.0) or 0.0),
                e.get("addressed") or "无",
                "、".join(obligations) if obligations else "无",
            )
        )
    return "\n".join(rows)


def _render_impressions(impressions: dict[str, str]) -> str:
    """read_impressions() 的 {他人: 多行文本} → 逐行「对 X 的印象」清单。"""
    rows: list[str] = []
    for other, text in impressions.items():
        for line in text.splitlines():
            if line.strip():
                rows.append(f"- 对 {other} 的印象：{line.strip()}")
    return "\n".join(rows)


def _closing_msg(state: dict, scene_name: str) -> dict:
    """块钟兜底收束导演消息：world 在 blocks >= closing_at_block 时追加（GUI/CLI
    的大上限极少触发）。文案用「自动收束」，区别于 worker 虚拟钟到点的打烊行（「十点
    了…打烊」由 worker 在 GUI 侧补发，不落图），让引擎兜底收束一眼可辨。"""
    live = _live_messages(state)
    prev = live[-1] if live else None
    return {"id": _next_id(state.get("messages") or []),
            "speaker": "导演", "speaker_type": "director",
            "content": "（本场景对话已持续过久，自动收束。）",
            "in_scene": prev["in_scene"] if prev else scene_name,
            "turn": state.get("turn", 0)}


def _is_near_repeat_of_own(messages: list[dict], speaker: str, content: str) -> bool:
    """candidate 台词是否几乎复读 speaker 自己最近 ≤3 条台词（含动作/措辞相近）。

    只跟自己比（杜绝「复读别人刚说的原句」也需要，但那由提示词约束；引擎兜底只管
    自我复读——GUI 里最典型的就是无人接话时同一角色连篇自我复读）。纯引擎兜底：
    相似度达阈值即判近重复，调用方决定不落盘。
    """
    own_recent = [m.get("content") or "" for m in reversed(messages)
                  if m.get("speaker") == speaker
                  and m.get("speaker_type") == "character"
                  and (m.get("content") or "").strip()]
    for prior in own_recent[:_OWN_RECENT_WINDOW]:
        if textsim.ratio(content, prior) >= _DUP_RATIO:
            return True
    return False


def _make_m3_nodes(ctx: GraphContext):
    def fan_out(state: GraphState) -> dict:
        """每块开始尝试清空上一轮 urges；merge-reducer 下 {} 为 no-op（非真清空），
        真正的新鲜值由本块每个在场者的 think 以同名键覆盖，陈旧键由 deliberate 在场过滤。"""
        return {"urges": {}}

    def emit_thinks(state: GraphState) -> list[Send] | str:
        """Send 扇出：把每听众独立视图拼进各自 payload（worker 看不到父态）。

        C1(最终评审)：last_chunk 不得直喂共享尾条（未按该听众 knows 过滤会把私密最新句
        泄给无权者）。改为 per-listener：last_chunk = 该听众自己过滤视图里的最新一条
        （view_for 已按 id 升序，view[-1] 即最高 id），转普通 dict 下发给 think（仅用于
        提示词/印象落盘），永不含该听众看不见的消息。

        空场景（无在场角色，§3.3）：没有任何听众 → 无 Send 扇出，本块直接落 world 收尾
        （块钟照常 +1；场景 agent 的叙述推进在引擎侧另行发生）。

        扇出名单 = ctx.active_names()（§3.3 可插拔演员表）：被移出/被禁言者不在其中，
        既不 think 也不参与本块竞价；刚进场者的视图按 entry_round 丢掉进场前的对话。
        """
        live = _live_messages(state)          # 被撤销的行不进任何视图（见 _live_messages）
        sends = []
        for name in ctx.active_names():
            # 在场轴 = ctx.space（**不可变空间键**，C1）：场景改名只改显示名，历史照旧可见。
            view = view_for(_msg_models(live), name, ctx.space,
                            since_round=ctx.entry_round(name))
            sends.append(Send("think", {
                "listener": name,
                "view_text": render_view(view),
                "last_chunk": view[-1].model_dump() if view else None,
                "turn": state.get("turn", 0),
            }))
        return sends or "world"

    async def think(state: dict) -> dict:
        """每个 think worker 输入 = 自己的 Send payload；解释→更新私有态→自标注。

        私密回喂（I 阶段新链路）：构图前读本听众**自己**的记忆文件——近期状态尾部
        ≤5 条 + 对他人印象——拼进自己的 prompt，让私有倾向随状态演变。这两段只进
        该听众本人的消息，绝不进共享态（§4.1）。结果同时写入：
          · 私有记忆 state.jsonl（追加 urge，供引擎 dynamic_states 展示最近一次）；
          · ctx.think_log 只读边通道（speaker/at/heard/result，供 GUI，非角色互见）。
        """
        listener = state["listener"]
        last = state.get("last_chunk")
        # own_last：最近听到的一行是否正是自己说的（无人插话的自续/沉默机会）。
        # 是 → 告诉模型"那是你自己的话"，不要当别人来句回答，杜绝自问自答再抬冲动。
        own_last = bool(last is not None and last.get("speaker") == listener)
        mem = memory_mod.CharacterMemory(ctx.run_root, listener)
        state_text = _render_recent_state(mem.load_state())
        impressions_text = _render_impressions(mem.read_impressions())
        think_kw = {}                       # 只在有料时才传，兼容旧 4 参签名/spy
        if state_text:
            think_kw["state_text"] = state_text
        if impressions_text:
            think_kw["impressions_text"] = impressions_text
        # 场景公共信息（§3.3/§3.4）：背景/描述/剧情走向 + 语言指令——进场者看不到历史，
        # 但「这是个什么世界、现在什么走向」人人可见（这正是进场语义的另一半）。
        msgs = build_think_messages(
            ctx.cards[listener],
            view_text=state.get("view_text", ""),
            last_chunk_text=_render_last_line(last),
            scene_text=f"场景：{ctx.scene.name}",
            **think_kw,
            own_last=own_last,
            background_text=ctx.scene.background,
            description_text=ctx.scene.description,
            plot_text=ctx.scene.plot_direction,
            language_directive=ctx.language_directive,
        )
        raw = await ctx.think.complete_json(msgs)          # 返回 dict
        tr = ThinkResult.model_validate(raw)               # 锁格式：非法即抛（无条件严格）
        # 轮次标记（§6.1 截断式撤回）：私有记忆按**写入时的转录轮次**落盘，撤回某条推进
        # 时引擎据此截断（memory.truncate_after_turn）——被丢弃的那段下文留下的私有解读
        # 会经 next-think 回喂把丢弃内容带回提示词，必须一起消失。
        turn = int(state.get("turn", 0) or 0)
        mem.append_state({"aroused": tr.aroused, "goal_progress": tr.goal_progress,
                          "addressed": tr.addressed,
                          "obligation_fulfilled": tr.obligation_fulfilled,
                          "urge": tr.urge, "turn": turn})
        if tr.impression_of_speaker and last:
            mem.append_impression(last["speaker"], tr.impression_of_speaker, turn)
        ctx.think_seq += 1                                 # 单调序号（裁剪无关）
        log = ctx.think_log                                # 只读边通道（非共享态）
        if len(log) >= _THINK_LOG_CAP:                     # 封顶：丢最旧，留最近 CAP
            del log[: len(log) - _THINK_LOG_CAP + 1]
        log.append({
            "seq": ctx.think_seq,
            "speaker": listener,
            "at": _now_iso(),
            "heard": last["content"] if last else None,
            "turn": state.get("turn", 0),
            "result": tr.model_dump(),
        })
        return {"urges": {listener: tr.urge}}              # 只共享标量，守 §4.1

    def deliberate(state: dict) -> dict:
        """纯代码节点（无 LLM）：以**数值 bid** 仲裁（§7.4 动态状态→g(state,weights)）。

        bid 来源：ctx.bidder 存在时（引擎路径）对每名在场者算 harness 数值 bid——
        状态随情景演化（沉默压力/邻接/唤醒/目标/场景压力），自报 urge 只作为其中
        一项（urge_gain·self_urge，可为负=不想说），不单独决定发言权；
        bidder 缺失（裸图/离线测试，无引擎 Dynamics）退化为用共享态 urges（仍先与
        在场者求交，排除被移出 scene 的角色留下的陈旧键，merge-reducer 不真清空）。
        结果语义交给 bidding.arbitrate（在位者优势/打断阈值/开口阈值不变）。

        竞价名单 = ctx.active_names()（在场且未禁言）：被禁言者**根本没有 bid**，
        话筒自然落给别人；被移出者同理。在位者若已不在名单里（半途离场）→ 视同无
        在位者，直接让位给当前最高 bid，杜绝「已离场者继续连任」。
        """
        names = ctx.active_names()
        if ctx.bidder is not None:
            bids = {p: float(ctx.bidder(p)) for p in names}
        else:
            bids = {k: float(v) for k, v in state.get("urges", {}).items()
                    if k in names}
        incumbent = state.get("current_speaker")
        if incumbent is not None and incumbent not in bids:
            incumbent = None              # 在位者已离场/被禁言 → 不再享有在位者优势
        d = bidding_mod.arbitrate(
            bids,
            incumbent=incumbent,
            silent_streak=state.get("silent_streak", 0),
            p=ctx.bid_params,
        )
        if ctx.demo_alternate and incumbent is not None and bids:
            # 演示交替（demo-only，不改变常规仲裁路径）：有在位者时，话筒让给
            # 「除在位者外 argmax bid ≥ 开口阈值」的其它参与者——speak 永不紧随同一人。
            # 仅当对方无意开口（低于阈值）才落回常规 arbitrate，避免冷场拖住场景。
            others = {k: v for k, v in bids.items() if k != incumbent}
            if others:
                best_other = max(others, key=others.get)
                if others[best_other] >= ctx.bid_params.speak_threshold:
                    d = bidding_mod.Decision("yield_to", speaker=best_other,
                                             silent_streak=0)
        return {"decided": {"kind": d.kind, "speaker": d.speaker,
                            "silent_streak": d.silent_streak}}

    def route_after_deliberate(state: dict) -> str:
        kind = state["decided"]["kind"]
        return "speak" if kind in ("yield_to", "incumbent_continues") else "silence_gate"

    async def speak(state: dict) -> dict:
        """读 decided.speaker 出块（顶替 M2 的固定轮流）。

        消息不带时刻戳：真实流速虚拟钟在 GUI 侧（worker）持有，时刻由 worker 派发
        时补 time_hhmmss，图/引擎不关心时间。
        """
        name = state["decided"]["speaker"]
        names = ctx.active_names()
        if name not in names or name not in ctx.cards:
            # 防御性兜底（§3.3）：已离场/被禁言者绝不落台词。deliberate 已把他们排除在
            # 竞价之外，但共享态的 decided/current_speaker 可能在块间被改；这里再守一道，
            # 宁可当作静默块（本块无产出），也不让不在场的人开口。
            return {"silent_streak": state.get("silent_streak", 0) + 1}
        all_msgs = state.get("messages") or []
        live = _live_messages(state)
        view = view_for(_msg_models(live), name, ctx.space,
                        since_round=ctx.entry_round(name))
        prev = live[-1] if live else None     # 尾条取**未撤销**的：撤销行不当锚点/落点
        # 因果锚点 = 胜出者**自己可见**的最新一条（view[-1]，id 升序；C1 可见性纪律：
        # 绝不用共享态 messages[-1] 锚定——它可能是该说话人看不见的私密最新句）。若最新
        # 可见句正是本人说的 → own_previous=True（紧接自续）：开口须是自然续说，不得回应/
        # 复读自己的话。归属文本（[id] 说话人[ →点名]: 内容）随 speak 进「此刻·上一句」。
        last = view[-1] if view else None
        prev_line_text = _render_last_line(last.model_dump()) if last else ""
        own_previous = bool(name is not None and last is not None
                            and last.speaker == name)
        # 本人私有回喂（与 think 同渲染）：开口前把胜出者自己最近的内心状态/印象喂进
        # speak，让台词延续自己的记忆与处境，而不是只锚定/模仿紧邻的他人那句。
        mem = memory_mod.CharacterMemory(ctx.run_root, name)
        state_text = _render_recent_state(mem.load_state())
        impressions_text = _render_impressions(mem.read_impressions())
        speak_kw = {}                       # 只在有料时才传，兼容旧 5 参签名/spy
        if state_text:
            speak_kw["state_text"] = state_text
        if impressions_text:
            speak_kw["impressions_text"] = impressions_text
        # 场景公共信息与 think 同源（§3.3/§3.4）：说话人也该带着世界观/场景描述/剧情
        # 走向开口——进场者尤其只有这些（他看不到进场前的对话）。
        msgs = build_speak_messages(ctx.cards[name], render_view(view),
                                    f"场景：{ctx.scene.name}",
                                    own_previous=own_previous,
                                    prev_line_text=prev_line_text, **speak_kw,
                                    background_text=ctx.scene.background,
                                    description_text=ctx.scene.description,
                                    plot_text=ctx.scene.plot_direction,
                                    language_directive=ctx.language_directive)
        content = await ctx.speak.complete_text(msgs)
        # 近重复判定只看未撤销的历史：撤销了旧句不该把同一句永久钉成「复读」。
        if _is_near_repeat_of_own(live, name, content):
            # 近重复自我复读：不落盘（当作静默块返回）——GUI 事件门控随之看到「本块无新
            # 消息」卸下武装转等待，模型不再连篇自我复读烧 token。绝不收束/绝不报错。
            return {"silent_streak": state.get("silent_streak", 0) + 1}
        block = {"id": _next_id(all_msgs),
                 "speaker": name, "speaker_type": "character",
                 "content": content,
                 "in_scene": prev["in_scene"] if prev else ctx.space,
                 "turn": state.get("turn", 0)}
        for p in ctx.active_names():          # 可见者各自落转录（物理隔离）
            # 进场轴同视图：刚进场者不该把进场前的台词写进自己的转录（§3.3）。
            if _is_visible_to(block, p) and block["turn"] >= ctx.entry_round(p):
                memory_mod.CharacterMemory(ctx.run_root, p).append_visible(block)
        return {"messages": [block], "current_speaker": name,
                "turn": state.get("turn", 0) + 1, "silent_streak": 0, "urges": {}}

    async def silence_gate(state: dict) -> dict:
        """静默连续达 K 块时注入导演事件把「开口机会」交回，否则静默结束本块。

        注入行与其它消息一样不带时刻戳（worker 派发时补）。
        """
        streak = state["decided"]["silent_streak"]
        if bidding_mod.should_inject(streak, k=ctx.bid_params.silence_k):
            live = _live_messages(state)
            prev = live[-1] if live else None
            msg = {"id": _next_id(state.get("messages") or []),
                   "speaker": "导演", "speaker_type": "director",
                   "content": "（世界层注入：侍者过来添水。）",
                   "in_scene": prev["in_scene"] if prev else ctx.space,
                   "turn": state.get("turn", 0)}
            return {"messages": [msg], "silent_streak": 0,
                    "injected": (state.get("injected", []) + [msg])}
        return {"silent_streak": streak}     # 尴尬停顿也是真实状态，本块到此为止

    def world(state: dict) -> dict:
        """每块收尾：只保留块钟兜底收束——blocks >= closing_at_block 即追加「自动
        收束」导演消息并置 closed（GUI/引擎/CLI 都传大值安全上限，几乎不触发）。
        块兜底文案与 worker 虚拟钟到点的「打烊」行不同（见 _closing_msg）。

        场景虚拟时间已不是图的职责：不再按台词/静默推进任何钟键，角色开口与模型
        思考不增减时间；真实流速虚拟钟由桌面 worker 在 GUI 侧持有，走到 time 硬
        边界时由 worker 调引擎 close_scene() 自然收束（引擎见 docstring）。
        """
        if state.get("closed"):
            return {"closed": True}
        blocks = state.get("blocks", 0) + 1          # 本块计数（speak 与 silence 均到 world）
        cap = state.get("closing_at_block")          # None/缺省 → 不按块数收束（默认）
        if cap is not None and blocks >= int(cap):
            # 显式设了上限才生效（仅测试用；App/引擎默认 None = 永不按块收束）。
            return {"closed": True, "blocks": blocks,
                    "messages": [_closing_msg(state, ctx.space)]}
        return {"closed": False, "blocks": blocks}

    return (fan_out, emit_thinks, think, deliberate, route_after_deliberate,
            speak, silence_gate, world)


def build_graph(cards: dict[str, CharacterCard], scene: Scene,
                think_backend: ModelBackend, speak_backend: ModelBackend,
                run_root: Path, checkpointer=None, store=None,
                bid_params=None, closing_at_block: int = 1 << 30,
                demo_alternate: bool = False, think_log: list | None = None,
                bidder: Callable[[str], float] | None = None,
                cast_provider: Callable[[], list[str]] | None = None,
                entry_round_of: Callable[[str], int] | None = None,
                language_directive: str = "",
                space: str = "",
                ctx: GraphContext | None = None) -> object:
    """编译 LangGraph。checkpointer 缺省 MemorySaver（测试/单进程内存）；
    生产/SceneEngine 传入 AsyncSqliteSaver（graph 全走 async API）。bid_params 缺省
    BidParams()（M3 仲裁参数）。demo_alternate（demo-only，默认 False）在 deliberate
    内叠加「轮流开口」覆盖，供 CLI 流式演示；False 时行为与既有仲裁逐字节一致。

    bidder：可选数值竞价源 `name -> bid`。引擎路径必传（闭包进引擎持有的 Dynamics，
    发言仲裁因此用 **状态→g(state,weights) 的数值 bid**，自报 urge 只是其中一项加权）；
    缺省（无引擎的裸图/离线测试）deliberate 退化用共享态 urges 作 bid——语义与旧仲裁
    一致，仅供无 Dynamics 的低层路径。

    closing_at_block：块钟兜底安全上限（GUI/CLI 传大值 200；纯图/引擎测试可设小值）。
    场景时间不在此参数化——虚拟钟由桌面 worker 按真实流逝×流速持有，图不持钟。
    think_log：可选外部持有的 list，think 结果只读边通道在此追加（缺省新建内部
    list；引擎传入自有的 self._think_log，供 think_log_tail 读尾部）。

    cast_provider / entry_round_of：可插拔演员表（§3.3）的两只读探针，缺省 None = 裸图
    语义（扇出/竞价 = 场景演员表，进场不过滤）。引擎路径必传：前者返回「在场且未禁言」
    者，后者返回该角色的进场基线（见 GraphContext 文档）。

    language_directive：语言指令（§7），透传进 ctx，think/speak 各带一份。
    ctx：可选**外部自持**的 GraphContext（引擎注入）。引擎要在运行期改语言指令
    （set_language，提示词立即生效，不必重建图），故 ctx 由引擎持有、这里复用；缺省
    内部自建 = 既有行为一字不变（裸图/离线测试）。
    """
    if ctx is None:
        ctx = GraphContext(cards, scene, think_backend, speak_backend, Path(run_root),
                           bid_params=bid_params or bidding_mod.BidParams(),
                           think_log=think_log if think_log is not None else [],
                           demo_alternate=demo_alternate, bidder=bidder,
                           cast_provider=cast_provider, entry_round_of=entry_round_of,
                           language_directive=language_directive, space=space)
    (fan_out, emit_thinks, think, deliberate, route_after_deliberate,
     speak, silence_gate, world) = _make_m3_nodes(ctx)
    g = StateGraph(GraphState)
    g.add_node("fan_out", fan_out)
    g.add_node("think", think)
    g.add_node("deliberate", deliberate)
    g.add_node("speak", speak)
    g.add_node("silence_gate", silence_gate)
    g.add_node("world", world)
    g.add_edge(START, "fan_out")
    # Send 扇出；空场景（无在场角色）无扇出 → 直接落 world 收尾（块钟照常 +1）。
    g.add_conditional_edges("fan_out", emit_thinks, ["think", "world"])
    g.add_edge("think", "deliberate")                           # fan-in 汇聚 → 仲裁
    g.add_conditional_edges("deliberate", route_after_deliberate,
                            {"speak": "speak", "silence_gate": "silence_gate"})
    g.add_edge("speak", "world")                                # 每块收尾（时钟/收束）
    g.add_edge("silence_gate", "world")
    g.add_edge("world", END)
    return g.compile(checkpointer=checkpointer or MemorySaver(), store=store)
