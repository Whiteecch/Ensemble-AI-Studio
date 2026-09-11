"""场景（叙述者）自己的触发算法：像角色有数值 bid 一样，场景有自己的开口判据。

为什么需要它（实况抱怨）：剧情需要外部事件时，角色只能说话——于是四名角色连着好几轮
重复同一个 beat（真实转录里四人反复念「他胳膊在流血，先包一下，包完我们跟你们走」）。
推进世界不是任何角色的职责，交给场景。

判据（全为 0..1 分量，加权求和得 score，过 threshold 才开口）：
  · repeat   最近 repeat_window 条**角色台词**两两平均相似度（textsim.ratio）。集体复读
            是本特性的主信号——权重最高（2.0）。相似度低于 repeat_threshold 视为「正常
             对话」，不计分（避免正常闲聊几轮就被推进）。
  · stall    距上次叙述的块数 / stall_blocks（久未推进）。
  · silence  连续静默块数 / silence_blocks（没人接得上话）。
  · boundary 距打烊剩余比例 ≤ boundary_window → 1.0（该收尾推进了）。
  score = 2.0*repeat + 1.2*stall + 0.5*silence + 1.0*boundary

cooldown：两次叙述之间至少隔 cooldown_blocks 块（blocked_by_cooldown 为真时调用方不该
开口；本模块只报告，不替调用方决定——手动推进要能无视它）。

**推进活跃度**（《界面与场景自由度》§6.2「频率可调」）：调用的频率不该写死，于是把
「触发线 + 冷却」两个**节奏**参数交给 `effective_params(params, activity)` 统一缩放
（活跃度越高 → 触发线越低、冷却越短；a=0.5 恒等，见该函数）；`activity_hint` 则把当前
档位翻成一句中文说明写进场景提示词——**要不要真叙述由场景自己按活跃度把握**，引擎只
决定"什么时候叫它"。

纯函数、确定性、零 IO：同样的输入必得同样的报告（可离线单测）。

本模块还承载**场景编辑指令**（tool directive）的纯文本协议：场景若被标为「可改变」
（description_mutable），它改场景时相当于"调工具"，但模型只会吐纯文本——于是把工具
调用编码成叙述里的一行指令，由本模块解析出来交给外壳执行。见文末「场景工具指令」一节。

另有一处**钩子条件的时间闸门**：`time_condition_target` 从钩子条件里认出「明确在说
虚拟钟」的钟点，交引擎在执行前做一次确定性校验（钟点没到就不执行）——见「时间条件」
一节。
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, replace  # noqa: F401  (asdict 供引擎/GUI 转报告)

from . import sceneclock, textsim


@dataclass(frozen=True)
class SceneNudgeParams:
    """场景推进判据参数（缺省即「四人复读该有人出来推一把」的实况标定值）。"""
    repeat_window: int = 4          # 最近 N 条角色台词参与"集体复读"判定
    repeat_threshold: float = 0.5   # 两两平均相似度 ≥ 此值 视为在复读（textsim.ratio）
    cooldown_blocks: int = 3        # 两次叙述间至少隔这么多块
    stall_blocks: int = 8           # 隔这么久没推进 → 停滞压力拉满（见下：停滞可单独触发）
    silence_blocks: int = 4         # 连续静默块数满格参考
    boundary_window: float = 0.15   # 距打烊剩余比例 ≤ 此值 → 该收尾推进
    threshold: float = 1.0          # 触发线


@dataclass(frozen=True)
class NudgeReport:
    """一次判据的完整结果（供引擎决定是否开口、供 GUI/日志解释为什么）。"""
    score: float
    repeat: float
    stall: float
    silence: float
    boundary: float
    blocked_by_cooldown: bool
    reasons: list[str] = field(default_factory=list)


#: 分量权重（与模块 docstring 的公式一致；改这里即改判据强度）。
#: 权重标定（实况教训 2026-09）：stall 权重必须 > threshold(1.0)，否则「久未推进」这类
#: 僵局单独永远够不到触发线——只有复读会触发叙述，而剧情卡住往往并不复读（一群人僵在
#: 「等管事的来」上，13 块都没人推）。现取 1.2/stall_blocks=8：约 8 块无推进即单独触发，
#: 复读则 2~3 块就触发。
_W_REPEAT = 2.0
_W_STALL = 1.2
_W_SILENCE = 0.5
_W_BOUNDARY = 1.0

#: reasons 的入选门槛：分量达到自身满格的这个比例才值得写进提示词/日志（少而准）。
_REASON_LEVEL = 0.5

# ---------------------------------------------------------------------------
# 活跃度旋钮（《界面与场景自由度》§6.2「推进频率可调」）
# ---------------------------------------------------------------------------
#: 活跃度 → 触发线与冷却的**统一**缩放系数。两式都以 a=0.5 为恒等点：
#:   threshold'       = threshold       × (1.3 − 0.6·a)   a=0.5 → ×1.0；a=1 → ×0.7；a=0 → ×1.3
#:   cooldown_blocks' = round(cooldown × (1.6 − 1.2·a))   a=0.5 → ×1.0；a=1 → ×0.4；a=0 → ×1.6
#: 系数写在这里（**唯一**），别散到调用方去——「高活跃度更容易开口」这条语义只有一处。
_ACT_THRESHOLD_BASE, _ACT_THRESHOLD_SLOPE = 1.3, 0.6
_ACT_COOLDOWN_BASE, _ACT_COOLDOWN_SLOPE = 1.6, 1.2

#: 活跃度四档标签（与 GUI 的 少/中/多/极多 四档值 0.2/0.5/0.8/1.0 一一对应）。
#: 边界取「档内更接近的那一档」：<0.25 低、<0.6 中、<0.85 高、其余极高。
_ACTIVITY_LABELS: tuple[tuple[float, str], ...] = (
    (0.25, "低"), (0.6, "中"), (0.85, "高"), (1.01, "极高"))

#: 各档「该怎么介入」的一句话（写进提示词，让模型自己调整频率）。
_ACTIVITY_HOW = {
    "低": "尽量少介入，把戏留给角色自己演",
    "中": "适度介入，卡住或需要外界变化时再推一步",
    "高": "多介入一些，主动把局势往前带",
    "极高": "尽量主动介入，频繁给出新的外界变化",
}


def effective_params(params: SceneNudgeParams | None = None,
                     activity: float = 0.5) -> SceneNudgeParams:
    """按**活跃度**（0..1）缩放判据的触发线与冷却，返回**新**参数（原参数不动）。

    §6.2「推进频率可调」：活跃度越高 → 越常主动推进（触发线更低、冷却更短）。映射是
    唯一的、单调的、无散落魔数的（见 _ACT_* 常量）：
      · threshold'       = threshold        × (1.3 − 0.6·a)
      · cooldown_blocks' = round(cooldown  × (1.6 − 1.2·a))
    两式都在 **a = 0.5** 处恒等（×1.0）——缺省活跃度下判据与本旋钮引入前逐字节一致，
    既有标定不被扰动。夹取：threshold' ≥ 0（负触发线没有意义）、cooldown' ≥ 0（0 = 无冷却）；
    activity 先夹到 0..1（越界等同端点档）。

    只动**节奏**两个参数：复读窗口/停滞/沉默/打烊窗口描述的是"局势有多僵"，不是"多久
    推一次"，一律原样保留。
    """
    p = params or SceneNudgeParams()
    a = _clamp01(float(activity))
    threshold = max(0.0, p.threshold * (_ACT_THRESHOLD_BASE - _ACT_THRESHOLD_SLOPE * a))
    cooldown = int(round(
        p.cooldown_blocks * (_ACT_COOLDOWN_BASE - _ACT_COOLDOWN_SLOPE * a)))
    return replace(p, threshold=threshold, cooldown_blocks=max(0, cooldown))


def activity_label(activity: float) -> str:
    """活跃度 → 中文档位（低/中/高/极高）；越界先夹到 0..1。"""
    a = _clamp01(float(activity))
    for upper, label in _ACTIVITY_LABELS:
        if a < upper:
            return label
    return "极高"


def activity_hint(activity: float, *, hooks: bool = False) -> str:
    """推进活跃度的**提示词短句**（引擎每轮附在叙述提示词的系统消息末尾）。

    两件事（§6.2）：① 把当前档位与"该少介入还是多介入"讲清楚——频率旋钮的真正执行者
    是场景自己（引擎侧的触发线/冷却只决定**何时叫它**）；② 说清"这轮只是被叫来判钩子"
    时该怎么写：只回指令行、**不要**为凑数写叙述正文（引擎只在真有正文时才落一行，
    所以"每块都咨询"不等于"每块都叙述"）。

    hooks=True 时才提 `[[HOOK:…]]` 语法：没有钩子的场景提它只会诱发模型凭空乱报钩子。
    """
    a = _clamp01(float(activity))
    label = activity_label(a)
    head = f"【推进活跃度】当前活跃度：{label}（{a:.2f}）。{_ACTIVITY_HOW[label]}。"
    if hooks:
        tail = ("这一轮若只是判钩子（没有值得推进的局势），就只写 [[HOOK:...]] 指令行，"
                "不要写叙述正文；真有需要推进的局势时才另写一到两行。")
    else:
        tail = "没有值得推进的局势时可以不写正文——引擎不会因此报错，绝不要求每轮都叙述。"
    return f"{head}\n{tail}"


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


# ===========================================================================
# 时间条件（钩子条件里的钟点：引擎侧确定性校验的**唯一**判据）
# ===========================================================================
# 为什么需要它（实况事故）：场景 阅览室 的钩子 h3/h3-2 条件是「虚拟钟走到 21:30」
# → 乙/甲离场。用户在 ~19:03 加人，两人**立刻走了**（提前 2.5 小时）。根因：钩子
# 条件只由场景 LLM 判——引擎把当前钟点喂进提示词（【当前时刻】），却从不自己校验时间
# 条件，模型误判即照执行。`time_condition_target` 就是那条确定性判据：条件**明确在说
# 虚拟钟**时才给出目标秒数，其余一律 None（引擎不越权替模型判非时间条件）。
#
#: 「明确在说虚拟钟」的钟表词。必须与**钟点**同时出现才认定是时间闸门——只有其一都不算：
#: 「有人提起 21:30 那件事」提到钟点但不是时间条件；「到点了」有钟表词却没有钟点。
_CLOCK_WORDS: tuple[str, ...] = (
    "虚拟钟", "时间", "钟点", "时刻", "到点", "走到", "到了", "已经到",
    "之后", "晚于", "打烊", "开门",
)

#: 条件里的钟点写法（取**第一处**匹配）：`21:30`/`9:05`（HH:MM，也接受 H:MM）与
#: 中文式 `21点30分`/`21点30`/`22 点`（只给小时 → 整点）。
_TIME_RE: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?P<h>\d{1,2}):(?P<m>\d{2})"),
    re.compile(r"(?P<h>\d{1,2})\s*点\s*(?:(?P<m>\d{1,2})\s*分?)?"),
)


def time_condition_target(condition: str | None) -> int | None:
    """条件若是**明确的时间闸门**，返回目标时刻（距当天 00:00 的秒数）；否则 None。

    判定 = 文本里**同时**出现①一处钟点（`HH:MM`/`H:MM` 或 `21点30分`/`21点30`/`22点`）
    与②一个钟表词（见 _CLOCK_WORDS，如「虚拟钟/时间/走到/到点/打烊/开门」）。两条缺一
    不可：只有钟点（`21:30`、「有人提起 21:30 那件事」）不是时间闸门，只有词（「到点了」）
    也没有目标可校——这两类都返回 None，由场景按原逻辑自行判断，引擎绝不越权。

    返回的秒数只建模**当天**（0..86399），跨午夜换算（目标 ≤ 开场时刻 → 次日）归调用方
    （与打烊边界的口径一致，见 engine._effective_time_target）。越界钟点（25:70、24:00、
    99点99分）一律 None——宁可不管，也不猜一个替代值。

    纯函数、绝不抛异常（None/空/非字符串/半截钟点 → None）；解析用
    `sceneclock.parse_hhmm`，本函数只负责"从自由文本里认出钟点"。
    """
    text = condition if isinstance(condition, str) else ""
    if not text or not any(word in text for word in _CLOCK_WORDS):
        return None
    for pattern in _TIME_RE:
        match = pattern.search(text)
        if match is None:
            continue
        hour = int(match.group("h"))
        minute = int(match.group("m") or 0)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None                      # 越界钟点：当没看见（连同后续写法也不再看）
        try:
            return sceneclock.parse_hhmm(f"{hour:02d}:{minute:02d}")
        except ValueError:                   # parse_hhmm 的越界口径兜底
            return None
    return None


def _character_lines(messages: list[dict], window: int) -> list[str]:
    """最近 window 条**有内容**的角色台词（speaker_type 缺省按角色计）。

    叙述/导演/人类的话不参与复读判定——复读的是角色，场景自己插的话不算。
    """
    if window <= 0:
        return []
    lines = [(m.get("content") or "").strip() for m in messages
             if (m.get("speaker_type") or "character") == "character"
             and (m.get("content") or "").strip()]
    return lines[-window:]


def _mean_pairwise_ratio(lines: list[str]) -> float:
    """两两相似度均值；不足两条（无对可比）→ 0.0（不把单句当复读）。"""
    pairs = [textsim.ratio(a, b)
             for i, a in enumerate(lines) for b in lines[i + 1:]]
    return sum(pairs) / len(pairs) if pairs else 0.0


def _ratio_component(value: int, full: int) -> float:
    """计数 → 0..1 分量（full ≤ 0 视为无需累积、直接满格）。"""
    if full <= 0:
        return 1.0
    return _clamp01(value / full)


def evaluate(messages: list[dict], blocks_since_narration: int, silent_streak: int,
             clock_progress: float | None,
             params: SceneNudgeParams | None = None) -> NudgeReport:
    """判据主入口：`messages` 应为**未撤销**的共享消息（撤销的叙述不进视图）。

    clock_progress：0=开场、1=打烊（None=场景无时间硬边界 → 不参与）。返回完整报告，
    score 恒为原始加权分（即便被 cooldown 挡住也照报，便于日志/GUI 解释）。
    """
    p = params or SceneNudgeParams()
    lines = _character_lines(messages or [], p.repeat_window)
    repeat = _mean_pairwise_ratio(lines)
    # 复读权重要用满，但只在「确实在复读」时才计入——正常对话不该被这条推着走。
    repeat = repeat if repeat >= p.repeat_threshold else 0.0
    stall = _ratio_component(max(0, int(blocks_since_narration)), p.stall_blocks)
    silence = _ratio_component(max(0, int(silent_streak)), p.silence_blocks)
    boundary = 0.0
    # 容差 1e-9：剩余比例「恰在窗口上」（如 progress=0.85、window=0.15）时 1-0.85 的
    # 浮点尾数会略大于 0.15 —— 闭区间语义不该由浮点噪声决定。
    if (clock_progress is not None
            and (1.0 - float(clock_progress)) <= p.boundary_window + 1e-9):
        boundary = 1.0
    score = (_W_REPEAT * repeat + _W_STALL * stall
             + _W_SILENCE * silence + _W_BOUNDARY * boundary)
    reasons: list[str] = []
    if repeat >= p.repeat_threshold:
        reasons.append("角色在复读")
    if stall >= _REASON_LEVEL:
        reasons.append("久未推进")
    if silence >= _REASON_LEVEL:
        reasons.append("久无人接话")
    if boundary:
        reasons.append("临近打烊")
    return NudgeReport(score=score, repeat=repeat, stall=stall, silence=silence,
                       boundary=boundary,
                       blocked_by_cooldown=blocks_since_narration < p.cooldown_blocks,
                       reasons=reasons)


# ===========================================================================
# 场景工具指令（tool directive）
# ===========================================================================
# 背景（设计文档 §3.1/§4.1③）：场景可以标 `description_mutable`，变化时它"调工具"——
# 如「餐厅里有一些桌椅」→「餐厅里有一些破碎的桌椅，到处都是战斗后的痕迹」。模型只会输出
# 纯文本，于是把工具调用编码成叙述里的一行：`[[TOOL:set_description]] 新的场景描述`。
# 本模块只负责**认出来 + 剥干净**（解析）与**照着改一个 dict**（应用），执行/落盘/校验
# 归外壳（shell）——这样这一层保持纯函数、确定性、零 IO，可离线单测。
#
# 解析规则（text 与 argument 的边界，调用方按此契约写测试）：
#   1. 一条指令形如 `[[TOOL:<name>]] <argument>`，从标记一直吃到**行尾**；**参数不能跨行**
#      （指令就是一行，叙述从下一行继续）。因此一行只有第一条指令有效，参数取到行尾为止。
#   2. name 必须匹配 ^[a-z_]+$。名字合法但**不认识**（如 set_time）照样解析成 SceneToolCall
#      ——"认不认识"是调用方的政策，这里绝不静默丢弃，否则外壳无从知道模型想干什么。
#   3. 畸形片段（有开头没 `]]`、名字为空、`[[TOOL]]` 少冒号、名字含非法字符）记进 malformed
#      **并从 text 里删掉**：语法泄进用户看到的叙述是事故，宁可少几行也不能露 `[[TOOL:`。
#      畸形只记录、**绝不抛异常**（模型输出再烂也不能炸掉一轮对话）。
#   4. text 是"给人看的那部分"：逐行把空白串压成一个空格、去掉行首尾空白；整行都是指令
#      （或被截断的指令）时该行整行不留（否则每改一次场景就多一个空行），段间原有空行保留，
#      首尾空行去掉。参数只做首尾 strip，**内部空白原样保留**（那是场景文案本身）。
#   5. 标记必须半角且大小写精确（`[[TOOL:`）；写成全角冒号或小写 tool 的，当普通文字留着。

TOOL_OPEN = "[[TOOL:"
TOOL_CLOSE = "]]"

#: 标记本体（去掉冒号）：`[[TOOL]]` 这类"少冒号"的畸形也要认得出来。
_TOOL_MARK = TOOL_OPEN[:-1]

_TOOL_NAME_RE = re.compile(r"^[a-z_]+$")
#: 只压 ASCII 空白：中文叙述常用全角空格（U+3000）排版，压成半角会改字面。
_WS_RE = re.compile(r"[ \t\f\v]+")


@dataclass(frozen=True)
class SceneToolCall:
    """一条场景编辑指令。"""
    name: str        # "set_description" | "set_background" | "set_name" | "append_description"
    argument: str    # 新文本（已 strip 首尾；内部空白原样）


@dataclass(frozen=True)
class ParsedNarration:
    """一次解析的完整结果：给人看的 text + 给外壳执行的 tools + 给日志的 malformed。"""
    text: str
    tools: list[SceneToolCall] = field(default_factory=list)
    malformed: list[str] = field(default_factory=list)


#: 工具 → scene_patch 的目标键。**唯一**写入映射；不在此表里的名字一律忽略（绝不猜）。
#: 注意这里没有任何文件名/路径键——见 _REFUSED_TOOLS/_FILE_KEYS。
_TOOL_TARGET = {
    "set_description": "description",
    "append_description": "description",
    "set_background": "background",
    "set_name": "name",
}

#: 显式拒绝的工具名：名字/路径是外壳的事，模型给的名字绝不能变成文件名（越权改文件）。
#: set_name 只写 name 键，永远不写 path/filename；写 set_path/set_filename 的直接无视。
_REFUSED_TOOLS = frozenset({"set_path", "set_filename", "set_file", "set_filepath",
                            "set_path_name", "path", "filename"})

#: 绝不由工具写入的键（防御性：_TOOL_TARGET 当前不含它们，改表时也别加进来）。
_FILE_KEYS = frozenset({"path", "filename", "file", "filepath", "path_name"})

#: 字段 → 该字段可用的工具（供提示词按「可改变字段」逐条列出）。
_FIELD_TOOLS = {
    "description": ("set_description", "append_description"),
    "background": ("set_background",),
    "name": ("set_name",),
}

#: 工具的一句话说明（写进提示词，让模型知道每个名字什么意思）。
_TOOL_DOC = {
    "set_description": "替换场景描述",
    "append_description": "在场景描述末尾追加一段",
    "set_background": "设置场景背景",
    "set_name": "设置场景名字（只改名字）",
}

#: 提示词里那条示例指令的参数（按字段给，省得示例里出现没被标为可改变的字段）。
_EXAMPLE_ARGUMENT = {
    "description": "餐厅里有一些破碎的桌椅，到处都是战斗后的痕迹",
    "background": "夜里的餐厅，只剩柜台上方一盏灯",
    "name": "废墟餐厅",
}


def _normalise_line(line: str) -> str:
    """单行规范化：ASCII 空白串压成一个空格，再去掉行首尾空白。"""
    return _WS_RE.sub(" ", line).strip()


def _parse_line(line: str, tools: list[SceneToolCall],
                malformed: list[str]) -> tuple[str, bool]:
    """处理一行，返回（剥掉指令后剩下的叙述片段, 本行是否出现过指令标记）。

    一条指令从标记吃到行尾（含参数），所以本行标记之后的内容不再算叙述。
    """
    start = line.find(_TOOL_MARK)
    if start < 0:
        return line, False
    head = line[:start]
    end = line.find(TOOL_CLOSE, start + len(_TOOL_MARK))
    if end < 0:
        # 有开头没结尾（写一半被截断/参数跨了行）：整段收进 malformed，一行都不外泄。
        malformed.append(line[start:].strip())
        return head, True
    header = line[start + len(_TOOL_MARK):end]
    if not header.startswith(":"):
        # `[[TOOL]]`：少冒号。
        malformed.append(line[start:end + len(TOOL_CLOSE)].strip())
        return head, True
    name = header[1:]
    if not _TOOL_NAME_RE.match(name):
        # 空名字 / 大写或空格等非法字符（`[[TOOL:Set Description]]`）。
        malformed.append(line[start:end + len(TOOL_CLOSE)].strip())
        return head, True
    tools.append(SceneToolCall(name=name,
                               argument=line[end + len(TOOL_CLOSE):].strip()))
    return head, True


def parse_narration(raw: str | None) -> ParsedNarration:
    """把一段叙述切成「给人看的文本」与「给外壳执行的工具指令」。**绝不抛异常**。

    raw=None/"" → 空结果（模型偶发空输出不该炸）。text 里保证不含 `[[TOOL`。
    """
    source = "" if raw is None else str(raw)
    lines: list[str] = []
    tools: list[SceneToolCall] = []
    malformed: list[str] = []
    for raw_line in source.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        kept, had_directive = _parse_line(raw_line, tools, malformed)
        line = _normalise_line(kept)
        if not line and had_directive:
            continue      # 整行就是指令（或写坏的指令）→ 不留空行
        lines.append(line)
    while lines and not lines[0]:
        lines.pop(0)      # 首尾空行去掉；段间原有空行保留
    while lines and not lines[-1]:
        lines.pop()
    return ParsedNarration(text="\n".join(lines), tools=tools, malformed=malformed)


def render_tool_call(call: SceneToolCall) -> str:
    """一条指令的**原文**（提示词靠它把语法演给模型看；与 parse_narration 互逆）。"""
    if call.argument:
        return f"{TOOL_OPEN}{call.name}{TOOL_CLOSE} {call.argument}"
    return f"{TOOL_OPEN}{call.name}{TOOL_CLOSE}"


def tools_prompt_block(mutable_fields: list[str]) -> str:
    """按「可改变字段」生成中文提示词块；没有可改变字段 → ""（外壳直接别附这段）。"""
    fields = [f for f in dict.fromkeys(mutable_fields or []) if f in _FIELD_TOOLS]
    if not fields:
        return ""
    example_field = fields[0]
    example = render_tool_call(SceneToolCall(_FIELD_TOOLS[example_field][0],
                                             _EXAMPLE_ARGUMENT.get(example_field, "……")))
    lines = [
        "【场景编辑指令】可改变的字段：" + "、".join(fields),
        "要改变场景时，另起一行写一条指令（行首尾不要有别的字），一行一条，格式：",
        example,
        "可用指令：",
    ]
    for f in fields:
        for tool in _FIELD_TOOLS[f]:
            lines.append(f"  · {tool}（{_TOOL_DOC[tool]}）→ {f}")
    lines += [
        "只有上面列出的字段可以改，其余一律别动。",
        "叙述必须自成一体：把指令行整行删掉后，用户读到的叙述依然完整、连贯；"
        "不要在叙述或对白里写「我要改场景」这类话。",
    ]
    return "\n".join(lines)


def apply_tools(scene_patch: dict, calls: list[SceneToolCall]) -> dict:
    """照着指令改 scene_patch，返回**新 dict**（调用方给的 dict 绝不改动）。

    · set_description      → 替换 description
    · append_description   → 追加到 description 末尾（两边都非空时中间补一个空格）
    · set_background       → 设置 background
    · set_name             → **只**设置 name（名字/路径不由工具决定：path/filename 类键
                             一律拒绝写入，set_path/set_filename 这类越权工具名直接无视）
    · 其他名字             → 忽略（不认识就不猜；执行前该由外壳校验参数，空参数会被如实
                             写入——那是调用方的责任，本函数不做政策）
    """
    result = dict(scene_patch or {})
    for call in calls or []:
        if call.name in _REFUSED_TOOLS:
            continue                                    # 越权：不给工具改文件名/路径的能力
        target = _TOOL_TARGET.get(call.name)
        if target is None or target in _FILE_KEYS:
            continue                                    # 未知工具：忽略
        if call.name == "append_description":
            old = result.get(target) or ""
            result[target] = (f"{old} {call.argument}" if old and call.argument
                              else old or call.argument)
        else:
            result[target] = call.argument
    return result
