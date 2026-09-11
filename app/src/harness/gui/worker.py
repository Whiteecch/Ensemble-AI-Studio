"""桌面端引擎工作线程：QThread 内跑唯一 asyncio 事件循环，驱动 SceneEngine 自主对话。

单事件循环纪律（评审轮 1 裁决）：SceneEngine 走 AsyncSqliteSaver 时绑定其首次构图
所在 loop，所有 async 调用（含 open/step/say/aclose）必须在同一个 loop 内执行。因此
SceneWorker 在子线程里建一个独立 asyncio loop 并 run_forever；一切引擎操作都经
asyncio.run_coroutine_threadsafe 投递到该 loop，GUI 线程绝不直接触碰引擎/loop，
也绝不重复 asyncio.run。

跨线程信号：信号从引擎 loop 线程 emit；SceneWorker 对象本身活在 GUI 线程（QThread
affinity），Qt 依 AutoConnection 自动把信号排队投递到 GUI 线程槽。

场景时间 = 真实流速制（取代旧的按句/按静默折算情节分钟模型）：虚拟钟由本 worker
（GUI 侧）持有——开场时置 baseline 于 engine.start_seconds，之后每个 ~0.5s 的
ticker 按 v = 开始 + 流逝 × 流速 连续计算并广播 sig_metrics；角色开口/模型思考不再
增减时间。暂停冻结钟、调速先折现再换流速（不丢时间）、停止/打烊（v ≥ 打烊边界）
自然收束：worker 调 engine.close_scene()、补发一条合成收束导演行（文案取**场景自己**的
硬边界描述 hard_boundary.desc，按当前界面语言出字，见 closing_text——场景是通用模型，
不写死任何题材词）、置 已收束。
每条派发到界面的消息由 worker 补 time_hhmmss（当前虚拟钟 HH:MM:SS），供逐行渲染。

run_root：每次建引擎都取 run_base 下唯一的 app-<uuid> 子目录（先 mkdir 走真实
AsyncSqliteSaver），重开/切换模型即换新目录，绝不复用，杜绝串场累积（同 start-demo）。

事件门控步进（不烧空 token）：autoplay 只在「有理由反应」时 step——开场、上场 step
产出了新消息、human 插话/手动继续 都会触发一步；一步无产出（无人回应/静默）即停步
进入「等待中…可随时插话」，等下一个事件或由独立虚拟钟 ticker 到点自然收束。每轮消息
派发后补发旁路信号 sig_dynamics（各角色数值动态快照 + 数值 bid）与 sig_think（think
只读日志，人类 UI 检视用，绝不进共享态）。发言权归数值 Dynamics 竞价，无独白刹车。

可插拔演员表（§3.3，S4a +《界面与场景自由度》§3.1/§3.2，T1）：加人/移出/禁言/解禁/预约
延时角色动作由本 worker 的 add_character / remove_character / mute_character /
unmute_character / schedule_cast_change 承载——一律经 _submit_cast 投到引擎 loop（GUI
线程绝不碰引擎），每次操作后 flush 新消息上屏并广播 sig_cast（engine.cast_state 全量：
在场/已移出/禁言）。sig_cast 在开场与每块之后也各发一次，界面据此重画左右栏。
建引擎时把**角色库目录**交给引擎（cfg["characters_dir"]，缺省 = 已装载卡所在目录）：
引擎据此按需装卡，故任何库中角色都能在任何时刻加入任何场景，且构造期就能补齐场景名单
里没被装载的那些人。

场景外壳（§5/S4b）：save_scene / reset_scene / set_autosave_every / set_language /
apply_scene_config 同样一律投到引擎 loop，GUI 线程绝不直接碰引擎。
  · save_scene：写两份——先把当前场的运行快照写进 sidecar（引擎的 scene 路径 = 本场场景
    文件，故存档落在场景库目录里的 `<场景文件>.runtime.json`，与场景配置本体互不干扰），
    再把**当前阵容**写回场景文件本体（§3.1：运行期增删要存住），成功后发 sig_saved(path)；
  · apply_scene_config：「配置场景」的热更新字段（名称/日期/背景/描述/可改变/剧情/边界/
    开始时刻）就地应用到活场景 → 下一块提示词即生效；
  · sig_scene_changed：场景 agent 经钩子/场景工具**自改**了场景信息时，把新条目
    [{field, old, new, at}] 按索引增量广播（T4 在日志面板里浅红高亮）；
  · reset_scene：清空对话与思考上下文（消息全量撤销 → 逐 id 发 sig_retracted 让界面
    摘行；配置与在场角色保留），并广播 sig_cast；
  · set_autosave_every：设置里的 5/20/50/100 轮（0 = 关闭），记成期望值并在建引擎时
    带上（重开/切场不丢）；
  · set_language：语言指令注入所有提示词（§7），同样记期望值 + 立即改活引擎；
  · set_narrate_activity：「场景推进」的活跃度档（§6.2 少/中/多/极多）——记期望值 +
    立即改活引擎（判据触发线与冷却随之缩放，场景提示词里也带上活跃度说明），改完广播
    一次 sig_narration（载荷带 activity，界面据此镜像档位）；重开/切场时由 _build_engine
    把期望值带进新引擎，故档位不丢；
  · set_api_config：设置里的 url/apikey/模型名（§2.1①）——记期望值，下次建引擎时透传给
    SceneEngine（api_base_url / model_override；api_key 优先于 start_scene 的那份），
    故「设置里改完 → 下一场（重）开就生效」。三项只在内存里流转，绝不落盘/进日志。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QThread, Signal

from .. import sceneclock as sc
from .. import scenestore as scenestore_mod
from ..engine import SceneEngine
from ..i18n import DEFAULT_LANGUAGE, Translator
#: 活跃度缺省档（§6.2）：与设置里的口径**同一处**（settings 是四档值的唯一定义处，
#: 这里只读它的常量，绝不复制一份——两处各写一个 0.5 迟早会漂）。
from .settings import DEFAULT_NARRATE_ACTIVITY

logger = logging.getLogger(__name__)

#: 等事件循环就绪的兜底超时（秒）——QThread 启动后 run() 很快建好 loop。
_READY_TIMEOUT = 10.0

#: 虚拟钟广播/收束检查的节拍（秒）。
_V_TICK_S = 0.5


def closing_text(desc: str | None, language: str = DEFAULT_LANGUAGE) -> str:
    """时间硬边界到点的合成收束行（**纯函数**，便于单测）。

    场景模型是通用的——「餐厅打烊」只是内置演示素材的边界描述。故收束行取**场景自己**
    的 `hard_boundary.desc`（如「散场了」「午休结束」），按当前界面语言出字；场景没写
    描述时给一句不含题材词的中性兜底（与 world 块钟兜底的「自动收束」文案不同，时间
    边界收束与块数兜底收束一眼可辨）。
    """
    tr = Translator(language)
    text = str(desc or "").strip()
    if text:
        return tr.t("scene.boundary_closing", desc=text)
    return tr.t("scene.boundary_closing_default")

#: App 端不再按块数收束场景：传给引擎 world 的块钟兜底上限架空为不会触发的极大值，
#: 场景正常收束只由虚拟钟走到打烊边界决定（见 _v_ticker_loop）。保留该参数仅为
#: 兼容 CLI/引擎/测试显式小值（图/引擎仍保留块钟兜底安全网，非 App 正常关闭路径）。
#: 块数收束上限缺省 = None（禁用）。App 永不按块数收束：场景只由虚拟钟走到打烊
#: 边界（worker ticker）或用户点「停止」结束；仅测试显式传整数才启用兜底。
_DEFAULT_BLOCK_CEIL: int | None = None

#: 成本守卫阈值：autoplay 距上次手动继续/开新场累计达此块数即自动暂停，请用户点
#: 「继续」续演（冻结钟、不收束），防止挂机空烧 token。公开实例属性 resume_every
#: 可注入小值供测试驱动（生产缺省 500）。
_RESUME_EVERY = 500

#: 事件门控：一段里没有任何新消息（无人回应/静默）就停步等待的事件空闲文案。
_IDLE_TEXT = "等待中…可随时插话"

#: 旧「独白硬刹车」（同人连说 N 句即自动暂停）已移除：发言权交给数值 Dynamics 竞价——
#: 沉默压力随 turns_since_spoke 抬升，久未开口者自然会拿回话筒，无需按句计数的刹车。
#: 防挂机烧 token 仍由成本守卫（每 resume_every 块）与事件门控静默容忍窗口负责。

#: _emit_think 每轮读取 think 只读边通道的尾部上界——正常场景经 500 块成本守卫
#: 反复自动暂停，累计 think 远到不了该量级；只是防极端长跑的读拷贝失控。
_THINK_READ_BOUND = 10**6


def build_scene_payload(engine: SceneEngine) -> dict:
    """把引擎持有的场景 + 角色卡序列化成一次 sig_scene_info 载荷（GUI 只消费 dict）。

    characters 按场景参与者顺序给出（与对白中说话者顺序一致），权重字段名沿用
    Weights 声明序 w1_relevance…w7_scene_pressure；语料 corpus（出处/风格/思维/口头禅/
    样例）一并带上——主窗口的「查看详情」弹窗要完整展示这张卡，而这里只放卡本身的
    公开设定，**绝不放私有思考或运行期数据**。
    """
    scene = engine.scene
    hb = scene.hard_boundary
    cards = engine.cards
    backend = getattr(engine.think_backend, "name", type(engine.think_backend).__name__)
    scene_file = engine._scene_file()           # sidecar 存档坐标（§5，同包内直读私有）
    autosave = getattr(engine, "_autosave", None)   # 替身引擎可能没有 → 回落 0=关闭
    autosave_every = int(getattr(getattr(autosave, "policy", None), "every", 0))
    chars: list[dict[str, Any]] = []
    for name in scene.participants:
        c = cards[name]
        chars.append({
            "name": c.name,
            "personality": dict(c.personality or {}),
            "abilities": list(getattr(c, "abilities", None) or []),
            "relationships": dict(c.relationships or {}),
            "weights": c.weights.model_dump(),
            "emotion_decay_rate": float(c.emotion_decay_rate),
            "corpus": c.corpus.model_dump(),      # 语料五件套（右栏「查看详情」用）
        })
    return {
        "backend": backend,                 # deepseek=真实模型 / stub=离线占位
        # 场景推进（叙述者）自动开关初值：窗口据此对齐左栏「自动推进」复选框（开场/切场
        # 都以此为准，不靠界面自己记——引擎才是权威）。
        "auto_narrate": bool(engine.narration_state()["auto"]),
        "scene": {
            "name": scene.name,
            # 场景自身信息（§3.1，S4b 全量带上）：日期/背景/描述（+可变）/剧情走向/hooks。
            # hooks 是纯数据（hooks.Hook 的 dict 形态），供场景卡与「管理场景」只读展示；
            # 真正的判定与执行在引擎里（每轮由场景自己判，§4.1①）。
            "date": scene.date,
            "background": scene.background,
            "description": scene.description,
            "description_mutable": bool(scene.description_mutable),
            "plot_direction": scene.plot_direction,
            "hooks": [asdict(h) for h in (scene.hooks or [])],
            # 演员表 = 场景文件的**记录**（谁曾进场 + 入场信息）；当下在场/禁言是运行期
            # 状态，走 sig_cast（engine.cast_state），此载荷不重复表达（§3.3）。
            "participants": list(scene.participants),
            "hard_boundary": ({"type": hb.type, "value": hb.value, "desc": hb.desc}
                              if hb else None),
        },
        # 本场场景文件路径（sidecar 存档就落在它旁边，§5）；未指定时 None。
        "scene_path": str(scene_file) if scene_file else None,
        # 是否已有可续演的存档（§5「打开场景」据此提示「接着上次演」）。
        "has_runtime": bool(scene_file and scenestore_mod.has_runtime(scene_file)),
        # 语言与自动保存周期（§7/§5）：界面设置项的镜像初值（权威值在引擎/worker）。
        # 鸭子类型容错：替身引擎（测试）可能没有这两项，缺了就回落缺省。
        "language": getattr(engine, "language", DEFAULT_LANGUAGE),
        "autosave_every": autosave_every,
        # 场景时间卡静态部分：开始/打烊 "HH:MM"（动态 当前/剩余 走 sig_metrics）
        "start_time": getattr(engine, "start_hhmm", None),
        "boundary_time": getattr(engine, "boundary_hhmm", None),
        "characters": chars,
    }


class _SupersededLaunch(Exception):
    """本次 launch 已被后来的一次取代（G2）——内部信号，绝不冒出 worker。"""


class SceneWorker(QThread):
    """拥有并运行唯一事件循环的引擎驱动线程。信号全部从引擎 loop 线程 emit。"""

    sig_message = Signal(dict)        # 一条新台词（空 content 已在 worker 内滤除）
    sig_metrics = Signal(dict)        # 引擎指标 + 虚拟钟时间指标 快照
    sig_status = Signal(str)          # 就绪/进行中/已暂停/等待中…/已收束…
    sig_scene_info = Signal(dict)     # build_scene_payload 结果（开场/重开各发一次）
    sig_finished = Signal()           # 场景收束（自然打烊/块钟兜底/用户停止）
    sig_dynamics = Signal(dict)       # 各角色数值动态快照 + bid {name: {...}}（实时状态）
    sig_think = Signal(dict)          # 新一条 think 只读日志（人类 UI 检视，非角色互见）
    sig_narration = Signal(dict)      # 场景推进（叙述者）状态快照：narration_state() 全量
    sig_retracted = Signal(int)       # 某条消息被撤销（id）→ 窗口重绘对白区
    sig_cast = Signal(dict)           # 演员表运行期状态（§3.3）：cast_state() 全量
    # 场景**自改**的变更流（§3.5，T1）：一批新条目 [{field, old, new, at}]，按索引增量
    # 派发（同 sig_think 的纪律）。界面在日志面板里以浅红高亮渲染（T4）——凡场景 agent
    # 经钩子/工具改了场景信息（描述/背景/名），人类都看得见一条变更记录。
    sig_scene_changed = Signal(list)
    sig_saved = Signal(str)           # 场景已存盘（§5）：sidecar 路径（str）
    # 成本守卫自动暂停的**机器可读旗标**（True=已达块数自动暂停，False=守卫解除）：
    # 界面据此同步暂停态/按钮/药丸色，不再靠状态串里有没有「点继续」猜（G6）。可见
    # 文案一字不改（仍走 sig_status 的「已达 N 块……」，词表与语言无关的那份不动）。
    sig_auto_paused = Signal(bool)
    # 场景自身状态快照（G9）：engine.scene_state() 的诊断（hook/tool/save 最近错误）
    # + **活场景字段**（被 hook/场景工具改过之后就是改后的样子）+ 隐式事件流尾部。
    # 只在内容变化时广播一次——界面据此刷新场景卡并让此前无处可看的诊断可见。
    sig_scene_diag = Signal(dict)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._oplock: asyncio.Lock | None = None
        self._ready = threading.Event()
        self._cfg: dict[str, Any] | None = None     # start_scene 注入的配置
        self._engine: SceneEngine | None = None
        self._autoplay_task: asyncio.Task | None = None
        self._seen = 0                              # 已 emit 的消息条数（loop 线程）
        self._paused = False                        # 仅 loop 线程读写
        self._pace = 0.8                            # 每块间隔秒数（loop 线程读写）
        self._quitting = False
        self._restarting = False            # 重开进行中的互斥旗标（loop 线程读写）
        #: launch 代际令牌（loop 线程读写）：每次 _launch 进场自增；交叠的两次 launch
        #: 各自只认自己那一代，被取代者 aclose 掉自己的引擎后静默退出（G2）。
        self._launch_gen = 0
        self._open = False                  # 引擎已开场且未收束的镜像（loop 写、GUI 只读）
        self._status = "就绪"
        # ---- 真实流速虚拟钟（本 worker 持有；GUI 只经 sig_metrics/消息读取）----
        self._rate = 1.0                    # 全局流速（默认 1×），set_rate 更新
        self._vclock: sc.VirtualClock | None = None
        self._boundary_seconds: int | None = None   # engine 解析后的打烊边界（次日报 +86400）
        self._start_hhmm: str | None = None
        self._boundary_hhmm: str | None = None
        #: 场景自己的硬边界描述（开场时从 engine.scene.hard_boundary 记下）——时间到点的
        #: 收束合成行按它出字（场景是通用模型，界面/文案都不写死任何题材词，见 closing_text）。
        self._boundary_desc: str | None = None
        #: 上一次 sig_scene_diag 的**去重签名**（loop 线程）：每块 flush 后都要看场景有没有
        #: 自改/报错，内容不变就别再推一份给界面。
        self._scene_diag_sig: tuple | None = None
        #: 已派发到的场景变更条目索引（-1 = 还没派过）：引擎的 scene_changes() 是 append-only
        #: 的，按索引增量推新条目即可（与 sig_think 按 seq 派发同一纪律，见 _emit_scene_changes）。
        self._last_scene_change = -1
        self._v_task: asyncio.Task | None = None    # 时钟 ticker（loop 线程）
        self._notified = False              # 本场是否已发过 sig_finished（防双收）
        self._v_closed = False              # 虚拟钟是否已收束停走
        # ---- 成本守卫（每 resume_every 块自动暂停防烧 token）----
        self.resume_every = _RESUME_EVERY   # 阈值（可注入小值供测试；loop 线程读）
        self._autoplay_blocks = 0           # 距上次手动继续/开新场的步数（loop 线程）
        self._auto_pause = False            # 当前暂停是否为成本守卫触发（loop 线程）
        self._auto_paused_emitted = False   # sig_auto_paused 已广播的镜像（去重，loop 线程）
        self._pause_visible = False         # 暂停态文案是否已展示（loop 线程）
        # ---- 独白刹车已移除（§7.5）：同人连说由数值 bid 的 recency 惩罚 + 沉默压力
        # 自然纠偏，不再按「连续 N 句」计数刹车；防烧 token 归成本守卫与静默窗口。
        # ---- 事件门控步进（loop 线程读写）----
        # 只在「有理由反应」时 step：开场后/上场产出新消息/插话/手动继续 武装一次。
        # §7.5 数值化后静默语义软化：数值竞价的破冰常要 1~3 个聆听/静默块（首块常
        # 无人开口、随后沉默压力把久未开口者抬过阈值），故容忍 silence_retry 个连续
        # 静默块仍保持武装；超过窗口才卸下武装转「等待中…」（省 token / 真死寂空闲）。
        self._armed = True                  # 是否有理由走下一步（开场默认先走一轮）
        self._waiting_idle = False          # 是否正停在「等待中…可随时插话」状态
        self._backend_err = False           # 上一步是否失败（模型暂不可用）→ 复健后回 进行中
        self._silence_streak = 0            # 连续「无新消息」块计数（loop 线程）
        self.silence_retry = 3              # 连续静默块容忍窗口（破冰/导演注入留白）
        self.dynamics_params = None         # 数值 Dynamics 参数（可注入；None=引擎缺省）
        # ---- 场景推进（叙述者）----
        # 自动推进开关的**权威期望值**（loop 线程读写：set_auto_narrate 落在 loop 上改，
        # _build_engine 建引擎时读）。GUI 侧的复选框只是它的镜像，切换一律经 _submit
        # 投到 loop 再改引擎，GUI 线程绝不直接碰引擎。
        self._auto_narrate = True
        #: 推进活跃度（§6.2，0..1）：与 _auto_narrate 同纪律——GUI 的四档控件只是镜像，
        #: 权威期望值记在这里（当前引擎不在/线程未起时只记值，建引擎时带上，重开/切场不丢）。
        self._narrate_activity = DEFAULT_NARRATE_ACTIVITY
        # 数值竞价是否叠加「轮流开口」覆盖（demo-only；GUI 缺省叠加保证对白交替好看）。
        # 可注入 False 关闭 → 仲裁走纯 bidding.arbitrate（incumbent 优势/打断阈值），
        # 供测试观测「久未开口者 bid 反超在位者再由沉默压力接管」的纯数值路径。
        self.demo_alternate = True
        # think 只读日志已派发到的最大 seq（引擎封顶裁剪不影响增量派发；每场从 -1 起）。
        self._last_think_seq = -1
        # ---- 场景外壳（S4b §5/§7）：语言与自动保存周期的**期望值**（loop 线程读写）----
        # 与 _auto_narrate 同纪律：GUI 侧设置项只是镜像，权威期望值记在这里，建引擎时
        # 带上（重开/切场不丢）；改设置一律经 _submit/_post_call 落到 loop。
        self._language = DEFAULT_LANGUAGE
        self._autosave_every = 0            # 0 = 不自动保存（GUI 按设置开启 5/20/50/100）
        # ---- 模型 api 配置（§2.1①，S6）：设置里的 url / apikey / 模型名期望值 ----
        # 与 _language 同纪律：GUI 只是镜像，权威期望值记在这里，建引擎时带上（重开/切场
        # 不丢）。**key 只活在内存里**：绝不进日志、不进 sig_scene_info 载荷、不写任何
        # 文件（settings.json 的读写归 SettingsStore，worker 不碰盘）。
        self._api_base_url = ""
        self._api_key = ""
        self._model_override = ""

    # ------------------------------------------------------------------ QThread
    def run(self) -> None:  # noqa: D102  (QThread 入口，运行于本线程)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._oplock = asyncio.Lock()
        self._ready.set()           # 就绪先于 run_forever，投递的任务会排队等 loop 起来
        try:
            loop.run_forever()
        finally:
            # run_forever 返回（_teardown 调 loop.stop 后）→ 兜底清空余留任务再关 loop。
            with contextlib.suppress(Exception):
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    # ------------------------------------------------------------ 投递原语
    def _submit(self, coro: Callable) -> None:
        """Thread-safe 把协程投递到引擎 loop，绝不等待其结果（fire-and-forget）。"""
        if not self._ready.wait(_READY_TIMEOUT):
            raise RuntimeError("SceneWorker 事件循环未就绪")
        if self._quitting:
            raise RuntimeError("SceneWorker 已关闭")
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    def _post_attr(self, name: str, value: Any) -> None:
        """把简单标量属性写到 loop 线程（与 autoplay 读同线程，天然无竞态）。"""
        if self._loop is None or not self._ready.is_set():
            setattr(self, name, value)      # 线程还没起：此刻仍在 GUI 线程单线程阶段
            return
        self._loop.call_soon_threadsafe(setattr, self, name, value)

    def _post_call(self, fn: Callable, *args: Any) -> None:
        """把 loop 线程上的同步回调排队执行（调速/暂停即时性由回调保证）。"""
        if self._loop is None or not self._ready.is_set():
            fn(*args)                       # 线程没起：GUI 线程单线程阶段直接执行
            return
        self._loop.call_soon_threadsafe(fn, *args)

    def _submit_cast(self, make: Callable[[], Any]) -> None:
        """投递一个人事协程（fire-and-forget，见 _submit）；线程未起/已关闭则静默忽略。

        人事操作会被 GUI 菜单在任意时刻点到（不像发话那样有 can_say 门控），故这里
        自带兜底：绝不把 RuntimeError 抛进 GUI 槽（PySide 对槽里未捕获异常是致命的）。
        传的是**协程工厂**而非协程：未投递时不会留下未被 await 的协程对象。
        """
        if self._quitting or self._loop is None or not self._ready.is_set():
            return
        coro = make()
        try:
            self._submit(coro)
        except RuntimeError:                # 与 _quitting 的竞态：放弃投递，别泄漏协程
            coro.close()

    # ------------------------------------------------------------ 公开 GUI API
    def start_scene(self, scene: Path, characters: list[Path], models_yaml: Path,
                    live: bool, bid: Path, opening: str | None, run_root: Path,
                    api_key: str | None = None,
                    closing_at_block: int | None = _DEFAULT_BLOCK_CEIL,
                    start_time: str | None = None,
                    cast_from_cards: bool = False,
                    characters_dir: Path | None = None) -> None:
        """（首次）开一场：记录配置，投递 launch 协程。live 且缺 key 时自动落 stub。

        closing_at_block 是引擎 world 的块钟兜底安全上限；App 不再按块数收束——
        缺省即架空极大值（不会触发），场景正常收束只由真实流速虚拟钟（worker 侧）
        走到打烊边界决定。start_time=None 时引擎取场景 start_time（缺省 21:30）。

        cast_from_cards（口径见《界面与场景自由度》§3.1）：演员表**以场景自己存的名单
        为准**，此开关只在场景**没存过阵容**（characters 为空）时才用 characters 播种
        ——桌面端「新建场景时选的人」由此进场；存过名单的场次绝不覆盖。

        characters_dir = **角色库目录**（§3.2）：引擎据此按需装卡（构造期补场景阵容缺的
        卡、运行期 add_character 现装卡），因此任何库中角色都能在任何时刻加入本场。缺省
        （None）＝ 已装载卡所在的目录——调用方只要给的是素材库里的卡，这个推断就对。
        """
        characters = [Path(p) for p in characters]
        self._cfg = {
            "scene": Path(scene),
            "characters": characters,
            "models": Path(models_yaml),
            "bid": Path(bid) if bid is not None else None,
            "live": bool(live),
            "api_key": api_key,
            "run_root": Path(run_root),
            "closing_at_block": (None if closing_at_block is None
                                 else int(closing_at_block)),
            "start_time": start_time,
            "cast_from_cards": bool(cast_from_cards),
            "characters_dir": (Path(characters_dir) if characters_dir is not None
                               else None),      # None → 建引擎时按装载卡所在目录推断
        }
        self._submit(self._launch(opening))

    def restart(self, opening: str | None, live: bool | None = None) -> None:
        """重开一场：先撤旧引擎（cancel autoplay + ticker + aclose），再按同配置重建开场。

        live 非 None 时先更新（真实模型开关 → 换 stub/live 后端重启）。
        """
        if self._cfg is None:
            return
        if live is not None:
            self._cfg["live"] = bool(live)
        self._submit(self._restart(opening))

    def say(self, text: str) -> None:
        """人类随时插话（非必需回合）。若正暂停则解除暂停，让角色下一块回应。"""
        if not text.strip():
            return
        self._submit(self._say(text))

    def stop_now(self) -> None:
        """立即停止本场（停止按钮）：收束 → 停 autoplay/ticker → 状态「已停止」、
        sig_finished；say 等后续操作受 can_say 门控。开新场仍可用。"""
        self._submit(self._stop_now())

    def set_paused(self, paused: bool) -> None:
        self._post_call(self._apply_paused, bool(paused))

    def set_pace(self, seconds: float) -> None:
        self._post_attr("_pace", max(0.0, float(seconds)))

    def set_rate(self, rate: float) -> None:
        """改全局流速（GUI 流速档）：在 loop 线程先折现当前时刻再换流速——不丢时间。"""
        self._post_call(self._apply_rate, float(rate))

    def set_auto_narrate(self, on: bool) -> None:
        """左栏「自动推进」开关：投到引擎 loop 改引擎旗标（关掉后只认手动推进）。

        开关可被随时点（不像发话那样有 can_say 门控），故这里自带兜底：线程还没起就
        直写期望值（此刻仍在 GUI 线程单线程阶段，与 _post_attr 同纪律），已关闭则忽略
        ——绝不把 RuntimeError 抛进 GUI 槽（PySide 对槽里未捕获异常是致命的）。
        """
        if self._quitting:
            return
        if self._loop is None or not self._ready.is_set():
            self._auto_narrate = bool(on)
            return
        self._submit(self._set_auto_narrate(bool(on)))

    def set_narrate_activity(self, level: float) -> None:
        """推进活跃度（§6.2 GUI 四档 少/中/多/极多）：记期望值 + 立即改活引擎。

        与 set_auto_narrate 同纪律：可被随时点（含开场前/线程未起），线程未起则只记期望值
        （此刻仍在 GUI 线程单线程阶段），绝不把异常抛进 GUI 槽；建引擎时带上期望值，故
        **重开/切场也不丢**。非法值（非数字）由调用方（界面）吸附到四档后再给，这里只做
        0..1 夹取（引擎侧同样夹一次，两处口径一致）。
        """
        try:
            value = max(0.0, min(1.0, float(level)))
        except (TypeError, ValueError):
            return                              # 非数字：忽略（界面已吸附，这里只守不崩）
        if self._quitting:
            return
        if self._loop is None or not self._ready.is_set():
            self._narrate_activity = value
            return
        self._post_call(self._apply_narrate_activity, value)

    def narrate_now(self) -> None:
        """「推进一下」：手动让场景推进一步（无视 cooldown 与自动开关）。"""
        self._submit(self._narrate_now())

    # ---- 场景外壳（S4b §5/§7）：保存 / 重置 / 自动保存周期 / 语言 ----
    def save_scene(self) -> None:
        """「保存场景」按钮（§5）：把当前场存进 sidecar，成功后发 sig_saved(path)。

        收束后照样可用（收尾时正该存一份）；线程未起/已关闭则静默忽略（同人事操作）。
        """
        self._submit_cast(self._save_scene)

    def restore_scene(self) -> None:
        """「打开场景」的续演（§5）：把 sidecar 里的历史接着演（没有存档则什么都不做）。

        开场之后再调（先 open 才有"接着哪一场"的语境）；恢复的历史逐条经 sig_message
        上屏，并广播一次 sig_cast（人事状态随档带回）。
        """
        self._submit_cast(self._restore_scene)

    def reset_scene(self) -> None:
        """「重置场景」（§3.4/§5）：清空对话与思考上下文（配置与在场角色保留）。

        逐 id 发 sig_retracted（界面据此把对白区摘空）+ flush 广播 sig_cast。
        """
        self._submit_cast(self._reset_scene)

    def set_autosave_every(self, n: int) -> None:
        """自动保存周期（设置里的 5/20/50/100 轮；0 = 关闭）：记期望值 + 立即落引擎。

        与 set_auto_narrate 同纪律：可被随时点，线程未起则只记期望值（建引擎时带上），
        绝不把异常抛进 GUI 槽。
        """
        if self._quitting:
            return
        if self._loop is None or not self._ready.is_set():
            self._autosave_every = max(0, int(n))
            return
        self._post_call(self._apply_autosave_every, max(0, int(n)))

    def set_api_config(self, base_url: str, api_key: str, model: str) -> None:
        """设置 →「模型 api 配置」的三项（§2.1①）：记期望值，下一次建引擎即生效。

        与 set_autosave_every 同纪律：可被随时调（含开场前/worker 线程未起时），线程未起
        则只记期望值（此刻仍在 GUI 线程单线程阶段），绝不把异常抛进 GUI 槽。

        api_key 优先于 start_scene 的 api_key（设置是主来源，环境变量只是回落）；三项都
        **只活在内存里**，绝不进日志/载荷/文件。
        """
        if self._quitting:
            return
        base_url = str(base_url or "").strip()
        api_key = str(api_key or "").strip()
        model = str(model or "").strip()
        if self._loop is None or not self._ready.is_set():
            self._apply_api_config(base_url, api_key, model)
            return
        self._post_call(self._apply_api_config, base_url, api_key, model)

    def set_language(self, code: str) -> None:
        """切换 LLM 作答语言（§7）：记期望值 + 立即改活引擎（提示词下一块就换语言）。

        界面文案的翻译由 GUI 侧自理（语言表在 i18n），这里只管提示词侧的语言指令。
        """
        if self._quitting:
            return
        code = str(code or DEFAULT_LANGUAGE)
        if self._loop is None or not self._ready.is_set():
            self._language = code
            return
        self._submit(self._set_language(code))

    def retract_message(self, mid: int) -> None:
        """撤销任意一条（典型是场景叙述）：从历史剔除并让窗口摘掉该行。"""
        self._submit(self._retract_message(int(mid)))

    def edit_narration(self, mid: int, text: str) -> None:
        """改写一条叙述：撤销原行 + 落一条新叙述行（详见引擎 edit_narration）。"""
        self._submit(self._edit_narration(int(mid), str(text)))

    # ---- 可插拔演员表（§3.3/§4.1②）：全部经 _submit_cast 投到引擎 loop，GUI 线程不碰引擎 ----
    def add_character(self, name: str, notify: list[str] | None = None,
                      notify_text: str = "", visible: bool = True) -> None:
        """让角色进场（§3.3）：落可见播报行 + 刷新界面（详见引擎 add_character）。"""
        self._submit_cast(lambda: self._add_character(
            str(name), list(notify or []), str(notify_text), bool(visible)))

    def remove_character(self, name: str, notify: list[str] | None = None,
                         notify_text: str = "", visible: bool = True) -> None:
        """让角色离场（§3.3）：历史保留、退出竞价（详见引擎 remove_character）。"""
        self._submit_cast(lambda: self._remove_character(
            str(name), list(notify or []), str(notify_text), bool(visible)))

    def mute_character(self, name: str, turns: int = 0) -> None:
        """禁言：turns > 0 = 禁言这么多块（自动解除），turns <= 0 = 永久禁言。"""
        self._submit_cast(lambda: self._mute_character(str(name), int(turns)))

    def unmute_character(self, name: str) -> None:
        """解禁（临时/永久皆撤）。"""
        self._submit_cast(lambda: self._unmute_character(str(name)))

    def schedule_cast_change(self, character_name: str, action: str,
                             fire_after_rounds: int,
                             notify: list[str] | None = None, notify_text: str = "",
                             visible: bool = True, turns: int = 0) -> None:
        """预约延时角色动作（§2.1④ 高级移入/移出）：fire_after_rounds 块后到期执行。

        action ∈ add/remove/mute_turns/mute/unmute；turns 仅 mute_turns 用（S4a 增补）。
        """
        self._submit_cast(lambda: self._schedule_cast_change(
            str(character_name), str(action), int(fire_after_rounds),
            list(notify or []), str(notify_text), bool(visible), int(turns)))

    def can_cast(self) -> bool:
        """GUI 可改人事的判据（同步、只读镜像）：引擎在且 worker 未在退出。

        与 can_say 有意不同：收束后仍可改人事（引擎侧人事操作收束后照常可用，存档/收尾
        时常要把人移出），故这里不要求 `_open`。
        """
        return self._engine is not None and not self._quitting

    def can_say(self) -> bool:
        """GUI 可发话判据（同步、只读镜像）：引擎已开场且未收束/未撤换。

        供 _on_send 在清空输入前判断，避免在收束/重建/失败间隙把用户文字白吞。
        """
        return bool(self._open)

    def shutdown(self, wait_ms: int = 8000) -> None:
        """优雅收尾：在 loop 上撤 autoplay + ticker + aclose 引擎 + loop.stop，再 join。"""
        loop = self._loop
        if not self.isRunning():
            return
        if loop is not None and self._ready.is_set() and not self._quitting:
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(self._teardown(), loop)
        self.wait(wait_ms)

    # ------------------------------------------------------------ 引擎协程
    async def _build_engine(self) -> SceneEngine:
        cfg = self._cfg
        assert cfg is not None
        base = Path(cfg["run_root"])
        base.mkdir(parents=True, exist_ok=True)
        run_root = base / f"app-{uuid.uuid4().hex[:8]}"   # 每场唯一，杜绝串场
        run_root.mkdir(parents=True, exist_ok=True)        # 目录存在 → AsyncSqliteSaver
        models = cfg["models"]
        # api key 取值：**设置推来的为主**（§2.1① 设置是 api 配置的主来源），start_scene
        # 的（CLI/环境变量）只作回落。空串一律当没有（engine 侧也照样兜底）。
        api_key = str(self._api_key or cfg["api_key"] or "").strip() or None
        # 角色库目录（§3.2）：显式给了就用；没给 = **已装载卡所在的目录**（调用方给的是
        # 素材库里的卡时，这个推断必然对）。都没有（本场一张卡都没有）→ None，引擎退回
        # 「只能调度已装载的卡」。
        characters = [Path(p) for p in cfg.get("characters") or []]
        characters_dir = cfg.get("characters_dir")
        if characters_dir is None and characters:
            characters_dir = characters[0].parent
        if cfg["live"] and api_key:
            models = models.with_name("models.live.yaml")  # 真实后端（DeepSeek）
        # live 而无 key：保持 models.yaml(stub)，不崩（同 runner --demo 语义）。
        return SceneEngine(
            cfg["scene"], cfg["characters"], models,
            run_root=run_root, bid_path=cfg["bid"], api_key=api_key,
            api_base_url=self._api_base_url or None,
            model_override=self._model_override or None,
            closing_at_block=cfg["closing_at_block"],
            demo_alternate=self.demo_alternate,
            start_time=cfg.get("start_time"), dynamics_params=self.dynamics_params,
            cast_from_cards=bool(cfg.get("cast_from_cards", False)),
            # 角色库目录（§3.2）：引擎据此按需装卡（场景阵容缺的卡 + 运行期 add_character），
            # 故「任何库中角色都能在任何时刻加入任何场景」。
            characters_dir=characters_dir,
            auto_narrate=bool(self._auto_narrate),
            # 场景文件路径即 sidecar 存档坐标（§5）：引擎缺省就用装载它的这个路径，
            # 显式传一次让「存档落在本场场景文件旁边」这件事在调用点可见。
            scene_file=cfg["scene"],
            language=str(self._language),
            autosave_every=int(self._autosave_every),
            # 推进活跃度（§6.2）：期望值在建引擎时带上（重开/切场不丢）；用户改档后由
            # set_narrate_activity 就地改活引擎，这里只管"下一场从哪一档开始"。
            narrate_activity=float(self._narrate_activity))

    async def _launch(self, opening: str | None) -> None:
        """建引擎 + 开场 + 起真实流速虚拟钟 + 广播一次场景信息 + 起 autoplay/ticker。

        失败不静默：任何建库/装载/cast/开场错误都在这里捕获 → 尽力 aclose 半建引擎 →
        emit "⚠ 开场失败：…"（窗口在状态区红字显示），绝不把窗口干停在"就绪"。

        launch 也会被 GUI 的「切场」（start_scene 再投一次）复用，故开头必须像 _restart
        一样先把旧引擎 aclose 掉：否则旧场的 AsyncSqliteSaver 连接会一直吊着（泄漏），
        每切一次场漏一个。

        **重入代际（G2）**：与 _restart 的 `_restarting` 互斥旗标不同，launch 允许交叠
        （慢开场 + 快速切场：两次 launch 会各自 await 在 open_scene 上），故用**代际令牌**
        区分胜出者——进场自增 `_launch_gen` 并记下本次代际，此后每个 await 之后复查：
        代际已经不是自己的（后来者接管了）就 aclose 自己那台引擎并直接返回，绝不碰
        self._engine/self._v_task，也绝不给自己那台起 autoplay/ticker。否则被取代的那台
        既不会被 aclose（sqlite 连接吊着），它的 ticker/autoplay 还会成为孤儿任务。
        """
        if self._quitting:
            return
        self._launch_gen += 1
        gen = self._launch_gen
        # 撤上一次的 ticker/autoplay（幂等，防连点/重开竞态留下双任务）。
        vtask = self._v_task
        self._v_task = None
        if vtask is not None and not vtask.done():
            vtask.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await vtask
        prev = self._autoplay_task
        if prev is not None and not prev.done():
            prev.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await prev
        self._open = False
        self._notified = False
        self._v_closed = False
        self._boundary_desc = None          # 新场的边界描述随后由本场引擎回填
        self._scene_diag_sig = None         # 新场：场景诊断去重签名从零起
        self._last_scene_change = -1        # 新引擎的变更流是新的 list → 索引从零起
        self._autoplay_blocks = 0           # 新场景/重开 → 成本守卫计数清零重计
        self._set_auto_pause(False)         # 新场：旧场若停在守卫暂停态，这里解除并广播
        self._pause_visible = False
        self._armed = True                  # 新场先武装一步：让角色对开场反应
        self._waiting_idle = False
        self._backend_err = False
        self._silence_streak = 0            # 静默容忍窗口清零
        self._last_think_seq = -1           # 新引擎 think 日志从首条起派发
        # 旧引擎（若有）先关：切场走的就是这条路径（start_scene 再投一次 launch），
        # 与 _restart 同纪律——持锁取出再 aclose，绝不把旧场的 sqlite 连接留在身后。
        old = self._engine
        self._engine = None
        if old is not None:
            async with self._oplock:
                with contextlib.suppress(Exception):
                    await old.aclose()
        engine: SceneEngine | None = None
        try:
            engine = await self._build_engine()
            # 空/None → 用引擎默认开场（GUI 的"留空"即传 None，绝不能 .strip() 崩溃）。
            await engine.open_scene(opening.strip() if opening else None)
            if gen != self._launch_gen:
                # 建/开期间被后来的一次 launch 取代：自弃这台，绝不碰 self.*。
                await self._discard(engine)
                return
            async with self._oplock:
                if gen != self._launch_gen:
                    raise _SupersededLaunch()
                self._engine = engine
                self._seen = 0
                self._paused = False
                self._open = True
                # 虚拟钟开场即此刻：rate 沿用最近一次流速（缺省 1.0）。
                clock = sc.VirtualClock(engine.start_seconds, rate=self._rate)
                clock.start()
                self._vclock = clock
                self._boundary_seconds = engine.boundary_seconds
                self._start_hhmm = engine.start_hhmm
                self._boundary_hhmm = engine.boundary_hhmm
                self._boundary_desc = self._read_boundary_desc(engine)
                self.sig_scene_info.emit(build_scene_payload(engine))
                self._emit_narration()       # 开场即广播一次叙述状态（auto 初值）
                await self._flush(engine)
            self._emit_status("进行中")
        except _SupersededLaunch:
            # 已在 _oplock 内被取代：此刻 self._engine 仍是自己的那台（还没赋值），
            # 关掉即走，绝不改 self._engine。
            if engine is not None:
                await self._discard(engine)
            return
        except asyncio.CancelledError:
            self._forget_engine(engine)
            if engine is not None:
                with contextlib.suppress(Exception):
                    await engine.aclose()
            raise
        except Exception as exc:
            # 开场失败：撤下引擎（半建也可能开了 sqlite），给窗口可见的错误状态。
            self._forget_engine(engine)
            if engine is not None:
                with contextlib.suppress(Exception):
                    await engine.aclose()
            self._emit_status(f"⚠ 开场失败：{exc}")
            return
        if gen != self._launch_gen:
            # 起任务前最后一道复查：这一刹那又被取代 → 自弃，不起孤儿任务。
            await self._discard(engine)
            return
        # 起新 autoplay/ticker 前再清一次已存在任务：防连点/重开竞态留下双份。
        prev = self._autoplay_task
        if prev is not None and not prev.done():
            prev.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await prev
        self._autoplay_task = asyncio.ensure_future(self._autoplay(engine))
        self._v_task = asyncio.ensure_future(self._v_ticker_loop())

    async def _discard(self, engine) -> None:
        """自弃一台被取代/半建的引擎（G2）：尽力 aclose，失败只记日志，绝不冒泡。"""
        with contextlib.suppress(Exception):
            await engine.aclose()

    def _forget_engine(self, engine) -> None:
        """失败/取消时撤下引擎镜像——**只撤自己的那台**（后来者已接手就不动它）。"""
        if self._engine is engine:
            self._engine = None
            self._vclock = None

    async def _restart(self, opening: str | None) -> None:
        if self._quitting:
            return
        if self._restarting:                 # 上一次重开还没完成 → 丢弃本次（连点保护）
            return
        self._restarting = True
        self._open = False
        try:
            # 先撤 ticker（旧场虚拟钟），再撤 autoplay，最后关旧引擎。
            vtask = self._v_task
            self._v_task = None
            self._vclock = None
            if vtask is not None and not vtask.done():
                vtask.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await vtask
            task = self._autoplay_task
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            old = self._engine
            self._engine = None
            if old is not None:
                async with self._oplock:
                    with contextlib.suppress(Exception):
                        await old.aclose()
            await self._launch(opening)
        finally:
            self._restarting = False

    async def _say(self, text: str) -> None:
        engine = self._engine
        if engine is None:
            return
        if engine.closed:                        # 已收束：同步镜像，让 GUI can_say 停发
            self._open = False
            return
        async with self._oplock:
            # 拿到锁后再测一次终态：say 可能在 _finalize(自然收束/停止) 持锁期间被调度，
            # 预检当时 closed=False，若不复查会把刚发的 已收束/已停止 覆盖成 进行中、
            # 并 clock.start() 复活已停的钟。终态后直接 return（不启动时钟、不发状态）。
            if engine.closed or self._notified:
                self._open = False
                return
            was_paused = self._paused
            self._paused = False                 # 暂停中插话也解除暂停 → 角色回应
            if was_paused:                       # 暂停中插话 = 一次手动续演 → 守卫清零
                self._set_auto_pause(False)
                self._autoplay_blocks = 0
            clock = self._vclock
            if clock is not None and not clock.is_running():
                clock.start()                    # 插话即恢复走钟
            await engine.say(str(text), name="你")
            await self._flush(engine)
            self._armed = True                   # 插话重新武装：角色下一步会回应
            self._silence_streak = 0             # 新输入 → 静默容忍窗口重新计时
        self._emit_status("进行中")

    async def _set_auto_narrate(self, on: bool) -> None:
        """（loop 线程）改自动推进开关：先记期望值（建引擎时也读它），引擎在则同步落地。

        开新场/切模型会重建引擎并在 _build_engine 里带上这个期望值，故此刻引擎还没起
        （用户在开场前就点了开关）也不会丢设置。
        """
        self._auto_narrate = bool(on)
        engine = self._engine
        if engine is not None:
            engine.set_auto_narrate(bool(on))
        self._emit_narration()

    def _apply_narrate_activity(self, level: float) -> None:
        """（loop 线程）改活跃度：记期望值（建/重建引擎时带上） + 立即改活引擎。

        引擎侧改的是一个标量，判据触发线/冷却与场景提示词在**下一次判定**时即用新值
        （不必重开一场）。改完广播一次 sig_narration，界面据此回读（并把控件对齐引擎值）。
        替身引擎（测试）没有这个方法 → 跳过，不炸 loop。
        """
        self._narrate_activity = max(0.0, min(1.0, float(level)))
        engine = self._engine
        setter = getattr(engine, "set_narrate_activity", None)
        if setter is not None:
            setter(self._narrate_activity)
        self._emit_narration()

    async def _set_language(self, code: str) -> None:
        """（loop 线程）改语言：先记期望值（建/重建引擎时带上），引擎在则同步落地。

        引擎侧 set_language 现场改的就是提示词读的那份指令，故**下一块**的 think/speak/
        narrate 全换成新语言，不需要重开一场。
        """
        self._language = str(code or DEFAULT_LANGUAGE)
        engine = self._engine
        if engine is not None:
            engine.set_language(self._language)

    def _apply_api_config(self, base_url: str, api_key: str, model: str) -> None:
        """（loop 线程）记下 api 配置期望值：建/重建引擎时带上（重开/切场不丢）。

        三项都只是**内存里的字符串**：worker 不写盘、不记日志（key 永不落地）。
        """
        self._api_base_url = str(base_url)
        self._api_key = str(api_key)
        self._model_override = str(model)

    def _apply_autosave_every(self, n: int) -> None:
        """（loop 线程）改自动保存周期：记期望值 + 立即落引擎（块末按新周期自动存）。"""
        self._autosave_every = max(0, int(n))
        engine = self._engine
        if engine is not None:
            engine.set_autosave_every(self._autosave_every)

    async def _save_scene(self) -> None:
        """（loop 线程）保存场景（§5/§3.1）：sidecar + **阵容写回场景文件** → 刷新界面 → sig_saved。

        持锁进行：与 step/人事操作同锁，避免存档读到半块状态。两份都写（T1 的口径）：
          · sidecar（save_scene_state）= 运行快照：转录 + 虚拟钟 + 块数 + 在场/禁言；
          · 场景文件（save_scene_file）= 把**当前阵容**写回 characters（其余配置一字不动），
            故重开这一场仍是这些人。
        两份都没有场景路径 / 写盘出错时引擎返回 None（诊断记在引擎里），这里只在非终态时
        给一行状态提示，绝不发 sig_saved（界面不会报"已保存"却什么都没写）。
        """
        engine = self._engine
        if engine is None or self._quitting:
            return
        path = None
        async with self._oplock:
            if self._engine is None:
                return
            path = await engine.save_scene_state()
            scene_path = await engine.save_scene_file()
            await self._flush(engine)          # 复用 sweep：顺带刷新 sig_cast/动态/变更流
        # 报**存下来的那两个坐标**里先成功的那个：正常两条都成，sidecar 在前（界面显示时
        # 取的是场景文件名，见 MainWindow._on_saved）。
        primary = path if path is not None else scene_path
        if primary is not None:
            self.sig_saved.emit(str(primary))
        elif not self._notified:
            self._emit_status("⚠ 保存场景失败：没有可写的场景文件或写盘出错")

    def apply_scene_config(self, **fields) -> None:
        """「配置场景」（§3.4/T3）：把热更新字段投到活场景，**立刻生效**。

        可改的字段见 SceneEngine.apply_scene_config（name/date/background/description/
        description_mutable/plot_direction/hard_boundary/start_time）；阵容变化不走这里
        （那是添加/移出角色两条通路）。GUI 侧只负责把编辑结果送过来，别的什么都不用做：
        引擎就地改活场景，随后 flush 刷新 sig_cast/场景卡/变更流，下一块提示词即用新值。

        与人事操作同纪律（可被随时点、失败绝不抛进 GUI 槽）：非法输入（如时间不是 HH:MM）
        由引擎抛 ValueError，这里兜成一行可见状态。
        """
        self._submit_cast(lambda: self._apply_scene_config(fields))

    async def _apply_scene_config(self, fields: dict) -> None:
        engine = self._engine
        if engine is None or self._quitting:
            return
        await self._cast_op("应用场景配置",
                            lambda: self._apply_config(engine, fields))

    @staticmethod
    async def _apply_config(engine: SceneEngine, fields: dict) -> None:
        """引擎侧调用是**同步**的（就地改活对象），这里只为套进 _cast_op 的 async 外壳。"""
        engine.apply_scene_config(**fields)

    async def _restore_scene(self) -> None:
        """（loop 线程）续演（§5）：引擎把 sidecar 转录追回共享态 → 逐条上屏 → 广播。

        恢复的历史追在现有消息之后（引擎不做覆盖），`_sweep` 因此照常从 _seen 起把新
        出现的那些行派发到界面。没有存档时引擎返回 False：什么都不发生（界面按全新一场
        继续），不报错也不改状态。
        """
        engine = self._engine
        if engine is None or self._quitting:
            return
        restored = False
        async with self._oplock:
            if self._engine is None:
                return
            restored = await engine.restore_scene_state()
            await self._flush(engine)          # 恢复的行逐条 sig_message + sig_cast
            if restored:
                self._armed = True
                self._silence_streak = 0
        if restored:
            self._emit_narration()

    async def _reset_scene(self) -> None:
        """（loop 线程）重置场景（§3.4/§5）：清空对话与思考上下文，保留配置与角色。

        引擎把每条现存消息记进 retracted（视图层语义与逐条撤销一致），这里**逐 id 发
        sig_retracted**：界面据此把对白区摘空（窗口按 id 摘行，没有"整屏清空"这条通路）。
        随后 flush 广播 sig_cast，并重新武装 autoplay——空场之后世界照常继续。
        """
        engine = self._engine
        if engine is None or self._quitting:
            return
        before: list[dict] = []
        async with self._oplock:
            if self._engine is None:
                return
            before = await engine.messages()
            await engine.reset_scene_runtime()
            await self._flush(engine)
            self._armed = True
            self._silence_streak = 0
        for mid in [m.get("id") for m in before if m.get("id") is not None]:
            self.sig_retracted.emit(int(mid))
        self._emit_narration()

    async def _narrate_now(self) -> None:
        """（loop 线程）手动推进：落一条叙述后立刻 flush 到界面（不等下一块）。"""
        engine = self._engine
        if engine is None or engine.closed or self._notified or self._quitting:
            return
        async with self._oplock:
            if engine.closed or self._notified:
                return
            await engine.narrate_now()
            await self._flush(engine)        # 新叙述立即上屏
            self._armed = True               # 世界推进了一步 → 角色可对之反应
            self._silence_streak = 0
        self._emit_narration()

    async def _retract_message(self, mid: int) -> None:
        """（loop 线程）撤回一条消息：引擎**截断**后逐 id 广播 sig_retracted（窗口摘行）。

        §6.1：撤回是截断式的——该条**及其之后同一场景内的所有消息**一起作废，故这里
        把引擎报回来的整段 id 逐个 emit（界面按 id 摘行，没有"整屏清空"这条通路）。
        替身引擎（测试）返回 None 时退化为只报被点的那一条。
        """
        engine = self._engine
        if engine is None or self._quitting:
            return
        async with self._oplock:
            ids = await engine.retract(mid)
        for i in (ids or [mid]):
            self.sig_retracted.emit(int(i))
        self._emit_narration()

    async def _edit_narration(self, mid: int, text: str) -> None:
        """（loop 线程）改写一条叙述：截断到该节点（含其后全部下文）+ 新行 flush 上屏。"""
        engine = self._engine
        if engine is None or engine.closed or self._notified or self._quitting:
            return
        ids: list[int] = []
        async with self._oplock:
            if engine.closed or self._notified:
                return
            # 改写 = 引擎侧「截断到该节点 + 追加新行」，故新旧 id 的差集就是本次作废的整段
            # （引擎没给差集接口，narration_state().retracted 是只读镜像，取前后差最直接）。
            before = set((engine.narration_state() or {}).get("retracted") or [])
            await engine.edit_narration(mid, text)
            after = set((engine.narration_state() or {}).get("retracted") or [])
            ids = sorted(after - before) or [int(mid)]
            await self._flush(engine)
        for i in ids:                          # 旧行及其后全部下文作废 → 窗口逐行摘掉
            self.sig_retracted.emit(int(i))
        self._emit_narration()

    async def _cast_op(self, label: str, make: Callable[[], Any]) -> bool:
        """人事操作公共外壳：持锁 → 调引擎 → 冲刷消息上屏 → 广播 sig_cast。

        收束后仍可用（§3.3：收束只冻结世界推进，不冻结人事——存档/收尾时还要能移人）；
        引擎不在（未开场/已撤）→ 什么都不做。失败（典型：角色卡未装载）不冒泡成
        未取回的异常：发一行可见状态说明，并把 sig_cast 照常刷新（界面状态不僵死）。
        成功则重新武装 autoplay：新人到场/有人离场都值得让角色反应一句（否则停在
        「等待中…」时人事变更将毫无动静）。
        """
        engine = self._engine
        if engine is None or self._quitting:
            return False
        try:
            async with self._oplock:
                if self._engine is None:
                    return False
                await make()
                await self._flush(engine)      # 复用 sweep：播报行立刻上屏 + sig_cast
                self._armed = True
                self._silence_streak = 0
            return True
        except Exception as exc:
            self._emit_status(f"⚠ {label}失败：{exc}")
            return False

    async def _add_character(self, name: str, notify: list[str], notify_text: str,
                             visible: bool) -> None:
        await self._cast_op(
            "加入角色",
            lambda: self._engine.add_character(name, notify=notify,
                                               notify_text=notify_text, visible=visible))

    async def _remove_character(self, name: str, notify: list[str], notify_text: str,
                                visible: bool) -> None:
        await self._cast_op(
            "移出角色",
            lambda: self._engine.remove_character(name, notify=notify,
                                                  notify_text=notify_text, visible=visible))

    async def _mute_character(self, name: str, turns: int) -> None:
        await self._cast_op("禁言", lambda: self._engine.mute_character(name, turns))

    async def _unmute_character(self, name: str) -> None:
        await self._cast_op("解禁", lambda: self._engine.unmute_character(name))

    async def _schedule_cast_change(self, character_name: str, action: str,
                                    fire_after_rounds: int, notify: list[str],
                                    notify_text: str, visible: bool,
                                    turns: int) -> None:
        await self._cast_op(
            "预约角色动作",
            lambda: self._engine.schedule_cast_change(
                character_name, action, fire_after_rounds, notify=notify,
                notify_text=notify_text, visible=visible, turns=turns))

    async def _stop_now(self) -> None:
        """引擎 loop 上执行停止：持锁收束 + 停 autoplay/ticker，杜绝「已停止」之后
        又被 autoplay 以「已收束」覆盖。幂等：已收束/未开场则直返。"""
        if self._quitting:
            return
        engine = self._engine
        if engine is None:
            return
        if self._notified:
            return
        await self._finalize("已停止", reason="用户停止",
                             close_engine=True, closing_content=None)

    async def _autoplay(self, engine: SceneEngine) -> None:
        """自主对话主循环——**事件门控步进**：只在「有理由反应」时 step 一次。

        开场（武装）、上场 step 产出了新消息（保持武装）、human 插话/手动继续
        （重新武装）都会触发一步；一步没有产出（无人回应/静默）即卸下武装进入
        等待态，不再空转 think/step——省 token。虚拟钟 ticker 独立常走，与步进
        空闲无关；到点收束仍由 ticker 统一 _finalize。引擎一旦收束即静默返回。
        """
        try:
            while not self._quitting:
                if engine.closed or self._notified or self._v_closed:
                    return
                if self._paused:
                    # 暂停（手动或成本守卫）都让 autoplay 空转待命，文案按暂停缘起。
                    self._pause_visible = True
                    self._emit_status(self._pause_status_text())
                    await asyncio.sleep(0.15)
                    continue
                if not self._armed:
                    # 事件门控空闲：上一段无人说话/无新消息 → 不再 step/think。
                    # 只把状态播一次并轻睡，等 human send / 继续 / 开新场重新武装，
                    # 或由独立 ticker 在 22:00 自然收束。
                    if not self._waiting_idle:
                        self._waiting_idle = True
                        self._pause_visible = False
                        self._emit_status(_IDLE_TEXT)
                    await asyncio.sleep(0.15)
                    continue
                if self._pause_visible or self._waiting_idle:
                    self._pause_visible = False
                    self._waiting_idle = False
                    self._emit_status("进行中")
                recovered = False
                try:
                    async with self._oplock:
                        if engine.closed or self._notified:
                            return
                        self._armed = False          # 先卸下武装；有产出再重新武装
                        seen_before = self._seen
                        # 数值场景压力：把当前虚拟钟喂给引擎（到打烊临近全员 bid 抬升）。
                        clock = self._vclock
                        if clock is not None and engine is not None:
                            engine.set_clock_now(clock.current())
                        await engine.step(1)
                        await self._flush(engine)
                        if self._seen > seen_before:
                            # 这一步产出了新消息（角色台词/导演行）→ 有人说话，继续反应。
                            self._armed = True
                            self._silence_streak = 0
                        else:
                            # 静默块：数值化后首块常是聆听块、破冰常要 1~3 块，故容忍
                            # silence_retry 个连续静默块仍保持武装；超过窗口才转等待
                            # （省 token；也留出真死寂场景的空闲态）。
                            self._silence_streak += 1
                            self._armed = self._silence_streak <= self.silence_retry
                        # 成本守卫：每成功推进一块计一次；累计达阈值即自动暂停（冻结
                        # 钟、请点「继续」续演，绝不收束），防止挂机空烧 token。暂停
                        # 期间不计数；手动继续/插话把计数清零（见 _apply_paused/_say）。
                        self._autoplay_blocks += 1
                        if (self.resume_every > 0
                                and self._autoplay_blocks >= self.resume_every):
                            self._autoplay_blocks = 0
                            self._set_auto_pause(True)    # 先广播旗标，再发状态文案
                            self._apply_paused(True)      # 与手动暂停同语义：冻结钟
                            self._emit_status(self._autopause_text())
                        if self._backend_err:
                            self._backend_err = False     # 本块成功 → 模型已恢复
                            recovered = True
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # DeepSeek 偶发 429/排队：不崩、不退场，如实报错后**重新武装**，
                    # 按节拍持续重试（不卸下武装落入 idle 静默——那是「无人说话」语义，
                    # 与后端失败不是一回事）。
                    self._backend_err = True
                    self._armed = True
                    self._emit_status("模型暂不可用，稍候重试…")
                    await asyncio.sleep(self._pace)
                    continue
                if recovered:
                    self._emit_status("进行中")     # 失败态被复健，回 进行中
                self.sig_metrics.emit(self._metrics_snapshot())
                self._emit_narration()             # 每块刷新叙述状态（含自动档产出的）
                await asyncio.sleep(self._pace)
        except asyncio.CancelledError:
            raise

    @staticmethod
    def _read_boundary_desc(engine) -> str | None:
        """从引擎的活场景读硬边界描述（无边界/无描述/替身引擎缺字段 → None）。

        鸭子类型容错：替身引擎（测试）可能没有 scene.hard_boundary，读不到就当没有描述，
        收束行走中性兜底——绝不因读一个描述把开场路径掀翻。
        """
        try:
            hb = getattr(getattr(engine, "scene", None), "hard_boundary", None)
            desc = getattr(hb, "desc", None)
        except Exception:  # noqa: BLE001 - 属性链上的任何异常都当"没有描述"
            return None
        text = str(desc or "").strip()
        return text or None

    def _closing_content(self) -> str:
        """本场时间到点的收束合成行：场景自己的边界描述 + 当前语言（见 closing_text）。"""
        return closing_text(self._boundary_desc, self._language)

    async def _v_ticker_loop(self) -> None:
        """虚拟钟连续计算 + 到点自然收束（loop 线程唯一收束出口之一）。

        每个节拍：暂停→冻结钟（current 恒为折现值）；运行→确保在走。引擎被块钟兜底
        收束（engine.closed）或虚拟钟走到打烊边界（v ≥ boundary）都经 _finalize 统一
        收尾：补发合成收束导演行（仅时间到点路径，块兜底文案已由 world 落图）、置
        已收束、停 autoplay/ticker。收束原因在 _finalize_locked 打 stderr 诊断行。
        """
        while not self._quitting:
            await asyncio.sleep(_V_TICK_S)
            engine = self._engine
            clock = self._vclock
            if engine is None or clock is None:
                continue
            try:
                if self._v_closed or self._notified:
                    return
                if engine.closed:            # 块钟兜底收束（world 已写自动收束导演行）
                    await self._finalize("已收束", reason="引擎块兜底收束",
                                         close_engine=False, closing_content=None)
                    return
                if self._paused:
                    clock.freeze()           # 冻结：暂停期间钟不动
                    self.sig_metrics.emit(self._metrics_snapshot())
                    continue
                if not clock.is_running():
                    clock.start()            # 继续/开场后首次到点起表
                if (self._boundary_seconds is not None
                        and clock.current() >= self._boundary_seconds):
                    # 时间到点打烊：引擎不知道时间，由 worker 收束 + 补发导演行。
                    await self._finalize("已收束", reason="时间到点(打烊)",
                                         close_engine=True,
                                         closing_content=self._closing_content())
                    return
                self.sig_metrics.emit(self._metrics_snapshot())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("虚拟钟 ticker 异常，跳过本拍继续")

    async def _finalize(self, status: str, *, reason: str, close_engine: bool,
                        closing_content: str | None) -> None:
        """本场收尾唯一出口（stop_now / 打烊 ticker / 引擎兜底共用）：拿锁后转
        _finalize_locked。reason 说明收束缘起（用户停止/时间到点/引擎块兜底），
        _finalize_locked 会把它连同虚拟钟/边界/块数打一行 stderr 诊断，便于事后判断
        场景为何停下。status 保持既有文案（已收束/已停止），GUI 语义不受影响。

        flush 余下消息、停 autoplay、补发可选合成收束行、终态指标、置状态 + sig_finished。
        _notified 防重入（已收束/已停止只广播一次）。
        """
        if self._notified or self._quitting:
            return
        async with self._oplock:
            await self._finalize_locked(status, reason=reason,
                                        close_engine=close_engine,
                                        closing_content=closing_content)

    async def _finalize_locked(self, status: str, *, reason: str,
                               close_engine: bool,
                               closing_content: str | None) -> None:
        """收尾本体——假定调用方已持有 oplock。抽出供测试精确复现「say 恰在收束持锁
        期间被调度」的窗口（见 test_gui_smoke）。非持锁调用一律走 _finalize。"""
        if self._notified or self._quitting:
            return
        self._notified = True
        self._open = False
        self._v_closed = True
        clock = self._vclock
        if clock is not None:
            clock.freeze()               # 任一收束路径都冻结虚拟钟（恒停走）
        engine = self._engine
        if engine is not None:
            if close_engine and not engine.closed:
                await engine.close_scene()
            task = self._autoplay_task
            if (task is not None and not task.done()
                    and task is not asyncio.current_task()):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            self._autoplay_task = None
            msgs = await engine.messages()
            self._sweep(msgs)
            self._emit_side_channels(engine)
            if closing_content:
                prev = msgs[-1] if msgs else None
                line = {"id": (prev["id"] + 1) if prev else 1,
                        "speaker": "导演", "speaker_type": "director",
                        "content": closing_content,
                        "in_scene": (prev.get("in_scene") if prev else None)
                        or engine.scene.name,
                        "turn": (prev.get("turn", 0) if prev else 0)}
                self.sig_message.emit(self._stamp(line))
            self.sig_metrics.emit(self._metrics_snapshot())
            self._emit_narration()           # 终态再广播一次（状态行不停在上次块）
        self._log_close_reason(reason)
        self._emit_status(status)
        self.sig_finished.emit()

    def _log_close_reason(self, reason: str) -> None:
        """收束原因诊断：stderr 一行（终端可见），供判断场景为何停下——reason 区分
        三种路径（用户停止 / 时间到点打烊 / 引擎块兜底），附冻结后虚拟钟 HH:MM:SS、
        打烊边界、引擎块钟镜像与 closed。引擎 metrics 同步读廉价镜像；取不到记
        "none"，任何失败只记日志，绝不干扰收尾。
        """
        try:
            clock = self._vclock
            clock_s = sc.format_clock(clock.current()) if clock is not None else "none"
            boundary_s = self._boundary_hhmm or "none"
            engine = self._engine
            if engine is not None:
                blocks = engine.metrics().get("blocks")
                closed = engine.closed
            else:
                blocks = closed = None
            print(f"[close] reason={reason} clock={clock_s} boundary={boundary_s} "
                  f"blocks={blocks} closed={closed}", file=sys.stderr)
        except Exception:
            logger.exception("写 close 原因诊断行失败，忽略")

    async def _teardown(self) -> None:
        """loop 上优雅收尾：停 autoplay + ticker、关引擎（aclose 须在本 loop）、停 run_forever。

        先置 _quitting（autoplay 下一拍自退），再等当前块让出 oplock（≤0.5s）后取消
        autoplay——避免在 engine.step 中途硬取消留下 langgraph 后台写任务；等不到才
        强制取消兜底。
        """
        self._quitting = True
        self._open = False
        vtask = self._v_task
        self._v_task = None
        self._vclock = None
        if vtask is not None and not vtask.done():
            vtask.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await vtask
        locked = False
        try:
            await asyncio.wait_for(self._oplock.acquire(), timeout=0.5)
            locked = True
        except asyncio.TimeoutError:
            locked = False
        task = self._autoplay_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._autoplay_task = None
        engine = self._engine
        self._engine = None
        try:
            if engine is not None:
                with contextlib.suppress(Exception):
                    await engine.aclose()
        finally:
            if locked:
                self._oplock.release()
        if self._loop is not None:
            self._loop.stop()

    # ------------------------------------------------------------ 内部小工具
    def _apply_paused(self, paused: bool) -> None:
        """（loop 线程）暂停/继续：暂停即刻冻结钟（不等到下个 ticker），继续从冻结值起表。

        与 autoplay 的暂停检查同线程写 _paused，插话/继续都不丢已走时间。手动继续
        （按钮「继续」/暂停中插话）视作一次续演，把成本守卫计数归零重计——成本守卫
        只统计「距上次手动续演以来的步数」。
        """
        was_paused = self._paused
        self._paused = bool(paused)
        clock = self._vclock
        engine = self._engine
        if clock is not None:
            if paused:
                clock.freeze()
            else:
                if was_paused:                   # 手动继续 → 守卫计数清零、解除守卫态
                    self._set_auto_pause(False)
                    self._autoplay_blocks = 0
                self._silence_streak = 0         # 续演 → 静默容忍窗口重新计时
                self._armed = True               # 继续 = 一次续演：武装让角色接话
                if not self._notified and not self._v_closed:
                    clock.start()
            if engine is not None and not self._notified:
                self.sig_metrics.emit(self._metrics_snapshot())

    def _set_auto_pause(self, on: bool) -> None:
        """（loop 线程）置成本守卫暂停态并**按变化**广播 sig_auto_paused（G6）。

        只在真正翻转时 emit：成本守卫每块检查一次、暂停期间 autoplay 每 0.15s 空转，
        逐次广播只会让 GUI 白刷。可见状态文案不在这里（仍由 _emit_status 发，一字不改）。
        """
        self._auto_pause = bool(on)
        if self._auto_paused_emitted == self._auto_pause:
            return
        self._auto_paused_emitted = self._auto_pause
        self.sig_auto_paused.emit(self._auto_pause)

    def _autopause_text(self) -> str:
        """成本守卫自动暂停时给用户的状态文案（提示点「继续」续演，不是收束）。"""
        return f"已达 {self.resume_every} 块，点继续以续演（防止 token 消耗）"

    def _pause_status_text(self) -> str:
        """当前暂停态要展示的状态文案：成本守卫给提示语，手动暂停给已暂停。"""
        if self._auto_pause:
            return self._autopause_text()
        return "已暂停"

    def _apply_rate(self, rate: float) -> None:
        """（loop 线程）改流速：先折现当前虚拟时刻再换 rate 重新起表，不丢时间。

        暂停中调速：set_rate 内部会重新起表，随即再冻结（暂停期间钟不得走动）。
        """
        self._rate = float(rate)
        clock = self._vclock
        engine = self._engine
        if clock is not None:
            clock.set_rate(float(rate))
            if self._paused:
                clock.freeze()
            if (engine is not None and not engine.closed
                    and not self._notified and not self._v_closed):
                self.sig_metrics.emit(self._metrics_snapshot())

    def _stamp(self, msg: dict) -> dict:
        """给一条派发消息补上当前虚拟钟 HH:MM:SS（time_hhmmss，取代旧图内 at_min）。"""
        clock = self._vclock
        if clock is None:
            return dict(msg)
        return {**msg, "time_hhmmss": sc.format_clock(clock.current())}

    def _emit_narration(self) -> None:
        """广播叙述者状态快照（narration_state() 全量：auto/blocks_since/last_report/
        narration_ids/retracted/**activity**）。GUI 左栏状态行与复选框/活跃度控件据此
        刷新；读廉价镜像，未开场则什么都不发（保持界面占位）。

        activity（§6.2）由引擎侧给（权威值）；替身引擎（测试）没有这个键时补上 worker
        手里的期望值——界面拿到的快照因此恒有该键，不必自己兜底。
        """
        engine = self._engine
        if engine is None:
            return
        try:
            state = dict(engine.narration_state())
            state.setdefault("activity", self._narrate_activity)
            self.sig_narration.emit(state)
        except Exception:
            logger.exception("读取叙述者状态失败，跳过本轮 sig_narration")

    def _emit_cast(self, engine: SceneEngine) -> None:
        """广播演员表运行期状态（§3.3）：cast_state() 全量 = 在场/已移出/禁言剩余块数。

        与 sig_narration 同纪律：只读镜像、try/except 兜底，任一步失败只记日志，绝不影响
        主对白派发与人事操作本身。GUI 据此重画左右栏（被移出者撤卡、禁言者标出剩余轮数）。
        """
        try:
            self.sig_cast.emit(engine.cast_state())
        except Exception:
            logger.exception("读取演员表状态失败，跳过本轮 sig_cast")

    def _emit_scene_diag(self, engine: SceneEngine) -> None:
        """广播场景自身状态（G9）：活字段 + hook/tool/save 最近诊断 + 隐式事件尾部。

        引擎的 `scene_state()` 早就如实报着「场景被谁改成了什么样」「哪条钩子/工具指令
        被拒了」，但此前没有任何消费者——场景卡一直停在开场那一刻的值，出错也无人可见。
        这里在每次 flush（开场/每块/人事/存档…）后读一次；**内容不变就不重复广播**
        （每块都刷会给界面白推一份同内容的载荷）。

        只读镜像 + try/except 兜底：替身引擎/半建引擎读不到就当没有，绝不影响主对白派发。
        """
        try:
            state = engine.scene_state()
        except Exception:
            logger.exception("读取场景自身状态失败，跳过本轮 sig_scene_diag")
            return
        fields = dict(state.get("fields") or {})
        events = list(state.get("implicit_events") or [])
        payload = {
            "language": state.get("language"),
            "hooks": list(state.get("hooks") or []),
            "description_mutable": bool(state.get("description_mutable")),
            "fields": fields,
            "hook_error": state.get("hook_error") or None,
            "tool_error": state.get("tool_error") or None,
            "save_error": state.get("save_error") or None,
            # 隐式事件（visible=False 的钩子事件）只进只读通道，供人类日志区展示；
            # 长跑时只带尾部若干条（界面也只展示最新一条的变化）。
            "implicit_events": events[-20:],
        }
        sig = (tuple(sorted(fields.items())), payload["hook_error"],
               payload["tool_error"], payload["save_error"],
               len(events), payload["implicit_events"][-1].get("content")
               if payload["implicit_events"] else None)
        if sig == self._scene_diag_sig:
            return
        self._scene_diag_sig = sig
        self.sig_scene_diag.emit(payload)

    def _emit_scene_changes(self, engine: SceneEngine) -> None:
        """广播场景**自改**的新条目（§3.5/T1）：engine.scene_changes() 按索引增量派发。

        引擎的变更流是 append-only 的（钩子的场景事件 + 场景工具指令两处都往它追加，
        连续重复的已被引擎去重），故只需记住上次派到哪一条，把其后的新条目整批发一次
        ——同 sig_think 按 seq 派发的纪律；界面在日志面板里以浅红高亮渲染新条目。

        只读镜像 + try/except 兜底：替身引擎（测试）没有这条通路就读不到，绝不影响主
        对白派发与人事操作本身。
        """
        try:
            changes = engine.scene_changes()
        except Exception:
            logger.exception("读取场景变更流失败，跳过本轮 sig_scene_changed")
            return
        start = self._last_scene_change + 1
        if start >= len(changes):
            return
        new = [dict(c) for c in changes[start:]]
        self._last_scene_change = len(changes) - 1
        self.sig_scene_changed.emit(new)

    def _metrics_snapshot(self) -> dict:
        """引擎指标 + 虚拟钟时间指标合并快照（sig_metrics 唯一来源，供 GUI 渲染）。"""
        engine = self._engine
        m = engine.metrics() if engine is not None else {}
        clock = self._vclock
        if engine is not None and clock is not None:
            v = clock.current()
            m["clock_seconds"] = v
            m["clock_hhmmss"] = sc.format_clock(v)
            m["rate"] = clock.rate
            if self._boundary_seconds is not None:
                m["remaining_s"] = max(0, self._boundary_seconds - v)
            m["start_hhmm"] = self._start_hhmm
            m["boundary_hhmm"] = self._boundary_hhmm
        return m

    async def _flush(self, engine: SceneEngine) -> None:
        """把引擎自上次以来新增的消息逐条 emit（空 content 滤除，防 DeepSeek 空句）。

        人类行（speaker_type == "human"）跳过不派发——GUI 已即时上屏，二次 emit 会
        造成同句双显；_seen 仍按全量推进，保证后续索引不错位。随后补派本轮的数值
        动态快照（sig_dynamics）与新增 think 日志（sig_think，人类 UI 只读边通道）。
        """
        msgs = await engine.messages()
        self._sweep(msgs)
        # 先发演员表再发数值：sig_cast 会**重画**左右栏（角色卡是新建的，实时分量先落
        # 占位），若数值先到就会被重画擦掉——顺序反了的话右栏数字会在每块之间闪空。
        self._emit_cast(engine)             # 演员表状态（§3.3）：开/每块/人事操作后各一次
        self._emit_side_channels(engine)
        self._emit_scene_diag(engine)       # 场景自改/诊断（G9）：内容变了才广播
        self._emit_scene_changes(engine)    # 场景自改进日志（§3.5/T1）：按索引增量

    def _build_dynamics_payload(self, engine: SceneEngine) -> dict:
        """组装每轮 sig_dynamics 载荷：数值动态分量 + 数值 bid，并后向兼容保留旧 think
        字段（urge/aroused/goal_progress/addressed…，引擎 dynamic_states 仍在提供）。

        返回 {name: {turns_since_spoke, arousal, adjacency, relevance, goal_progress,
                     goal_pressure, scene_pressure, bid, [urge/aroused/addressed…]}}。
        数值快照恒含全员（冷启动即给初值），GUI 据此实时展示随情景演化的各分量。
        """
        snap = engine.dynamics_snapshot()
        try:
            old = engine.dynamic_states() or {}
        except Exception:
            old = {}
        for name, st in snap.items():
            for k, v in (old.get(name) or {}).items():
                if k not in st:           # 数值键优先；旧字段只补缺（后向兼容）
                    st[k] = v
        return snap

    def _emit_side_channels(self, engine: SceneEngine) -> None:
        """一轮消息派发后补发两条旁路信号：sig_dynamics（角色数值动态 + bid）与
        sig_think（think 只读日志的新条）。两者都是 GUI 侧人类 UI 的只读展示，
        绝不进共享态/不给角色看。读文件/内存都 cheap，且各自 try/except 兜底：
        任一失败只记日志，不影响主对白派发。
        """
        try:
            payload = self._build_dynamics_payload(engine)
            if payload:                   # 至少一名参与者才有可广播内容
                self.sig_dynamics.emit(payload)
        except Exception:
            logger.exception("读取角色数值动态失败，跳过本轮 sig_dynamics")
        try:
            # 按条目内嵌的单调 seq 增量派发：引擎在 append 时封顶裁剪（丢最旧），
            # 单靠「尾部长度差」会因裁剪失灵，seq 不受裁剪影响 → 不漏新条。
            tail = engine.think_log_tail(n=_THINK_READ_BOUND)
            last = self._last_think_seq
            for entry in tail:
                seq = entry.get("seq", -1)
                if seq > last:
                    self._last_think_seq = seq
                    self.sig_think.emit(dict(entry))
        except Exception:
            logger.exception("派发 think 日志失败，跳过本轮 sig_think")

    def _sweep(self, msgs: list[dict]) -> None:
        """派发自 _seen 起的新消息到 GUI；human 行跳过（GUI 即时上屏处负责渲染）。

        空 content 滤除；_seen 一律推进到全量尾部，人类行照常计数但不 emit。
        不再做「同人连说 N 句」刹车——发言权已交数值竞价（recency 惩罚 + 沉默压力
        自然纠偏），无计数器介入。
        """
        total = len(msgs)
        for m in msgs[self._seen:total]:
            if m.get("speaker_type") == "human":
                continue                # GUI 已画过「你」气泡，这里不二次派发
            if not (m.get("content") or "").strip():
                continue
            self.sig_message.emit(self._stamp(m))
        self._seen = total

    def _emit_status(self, text: str) -> None:
        if self._status == text:
            return
        self._status = text
        self.sig_status.emit(text)
