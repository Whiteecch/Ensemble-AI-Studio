"""信息库的**工具层**（设计文档 §6.2 工具集 / §6.3 工具循环 / §6.4 取用通道 / §13.1 失败不回抛）。

它是"模型说要什么"与"磁盘上有什么"之间的适配器：纯内核在 knowledge.py（零 IO），
落盘与副本在 knowledgestore.py，这里只做**翻译与收口**——把三个工具的定义交给后端，
把工具调用翻成对副本的读写，再把结果翻回一段给模型看的中文文本。

四条硬约束，改动前先读：

1. **工具执行失败绝不回抛**（§13.1）。键不存在、键不合法、条目读不出来、revise 打到
   广域库、磁盘 IO 出错——一律变成**给模型看的工具结果文本**，让模型自己纠正下一步。
   回抛的代价不是这一次调用失败：worker 会把整块 think 重试，重试即重复计费，一个
   记不下来的条目名就能把整块的钱再花一遍。故 `execute` 外面还套着一层兜底 catch，
   任何没被预见的异常也只变成一句话。返回文本一律**确定性**（同输入同输出、不盖时间戳），
   便于测试，也让"这一枪到底回了什么"可复现。

2. **写只写本场副本**（§5.2）。`remember` / `revise` 的目标目录一律由
   `knowledgestore.scene_library_dir` 换算，本体库（`app/libraries/`）在整场戏里一个字
   都不动——那是"散场结算（保留/丢弃）"能成立的前提。读在副本缺席时**回落**到本体：
   读是只读的，回落不会污染本体；不回落的话，进场钩子没跑到就等于让角色这一场什么都
   想不起来，而且没有任何提示。

3. **新压旧，旧条一字不删**（§3.5）。新说法上位（active），同副标题的旧说法标 outdated
   并记 `superseded_by`，键不同则两条并存——"保留旧有的认识，只是将其标记成过时信息"。
   同 key 是例外中的例外：一个键就是一个文件（`knowledgestore.Library.upsert`），同键
   不存在"两条"，此时是覆盖式更新（模型重记同一件事，用途本来就是补全/订正）。两条路的
   差别要如实告诉模型（见 `TOOL_SPECS` 的措辞）：近似标题的**其它**条目一字不删，同键那条
   的原文是被顶掉的。

4. **取用记录可按轮次截断**（§5.3/§6.4）。`recall.jsonl` 是 think→speak 的私人通道
   （与 state.jsonl / impressions 同一条边界，不新开），故它也是**回喂源**：撤回某条
   推进后，角色不能还带着"从没发生过的事"开口。口径与 `memory.CharacterMemory.
   truncate_after_turn` 严格对齐：保留 turn ≤ cut，缺轮次按 0 计（= 最早，绝不会被误截）。

5. **撤回要连"派生出来的事实"一起带走**（§5.3 / §5.3.1）。副本按轮次截断（`truncate_scene_
   after_turn`）不只是删条目文件：被截掉的那条留下的一切痕迹都得跟着走，否则撤回只做了
   一半——
     · 它压下去过的旧条目要**放开**（否则那条明明发生在 cut 之前、却被永久挤出索引，
       提示语还指向一个已被删掉的键）。但"放开"不等于一律清成现行：要回到 **cut 那一刻的
       样子**——副本里还活着的条目若也压着它（更早的第 3 轮记过一条同标题的），它仍该是
       过时的那一条（`_settle_press`）。副本里同时出现两条同标题的现行说法，比原来那个
       毛病更坏：索引并列两行，模型照着被推翻的旧正文开口。
     · 它顶掉的基线条目要**从本体放回来**（一个键一个文件，原地覆盖后原文只剩在本体里，
       §5.2 本体整场只读因此是可靠的原文来源）；
     · **本场修订过的基线条目要还原成本体原文**（§5.3.1）。那段改写的依据常常正是从被撤回
       的下文里学来的（"原来那封信是伪造的"），留着它角色下一块就带着"从没发生过的事"
       开口；散场选「保留」还会把它永久并入本体，你再也退不回去。判据是 `pending.json`
       里记下的**修订轮次**（`revised_turns`），轮次缺失时保守不回退。
     · **还原只回退到 cut，一步都不多**。凡是本场在 cut 之内写下的正文（同键重记、revise
       ——两者都是原地替换）都不换回旧文：那是"确实发生过的事"，换掉它撤回就回退过头了
       （`_keeps_own_body`）。回退**没做成**的条目（本体里找不到原文，见 `warnings`）也不算
       "没了"：它仍以本场改后的正文现行、仍压着它的对手，不许被当成被删掉的那类去放开。
   `origin.turn` 则只在条目**确实是本场产物**时才重盖（见 `_is_scene_product`）：从本体
   拷进来的条目带着哨兵轮次，场景名跨 run 复用时也是它——只看 kind 会误判，撤回一次就
   把角色上一场保留的长期记忆整条删掉。

撤回的报告（删了几条、还原了几条、有什么没做成）走返回值 `TruncateReport`：撤回是最需要
留痕的动作，只回一个数调用方就没法记出一条能看懂的日志。

6. **改关系要过三道闸，且这个工具只给有关系的人**（《人际关系与场景推进》§6.3）。
   `update_relation` 是第四个工具，与 read_entry/remember/revise **并列**（同一套执行器、
   同一套预算、同一套"失败回文本不回抛"的契约），但它有三重约束，且**按角色给**：

     · 工具描述里写死了"只有在发生了**改变关系本质**的大事时才用（表白、决裂、救命、
       背叛）。日常寒暄、心情起伏、一次不愉快的对话——都不算"（`RELATION_TOOL_SPEC`）；
     · **频率闸门**：同一对关系每场最多改 `MAX_RELATION_EDITS_PER_SCENE` 次（默认 1），
       账本落在副本的 `pending.json`（§6.5 复用信息库那一套），跨块、跨 think 都算同一场；
     · **幅度闸门**：`closeness_delta` 单次 ≤ `MAX_CLOSENESS_DELTA`（±20），超出即**拒绝**
       （不是夹取：夹取会让模型以为它一次推到位了，而闸门要防的恰恰是"一次跳满"这个动作）。
       结果算到 ±100 之外时才是夹取（方向没有歧义），并在回执里说出来。
     · `tools(character)` 只给**有关系表**的角色多这一件——给所有人塞一个用不上的工具，
       会让无关系角色的请求体多一段描述、还会诱导模型去调它（然后拿到一句拒绝）。
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import knowledgestore as store
from . import relations as relations_mod
from . import textsim
from .knowledge import (
    Entry,
    EntryOrigin,
    SUBJECT_SIMILARITY_THRESHOLD,
    backlinks,
    entry_key_error,
    same_subject,
)
from .relations import MAX_CLOSENESS_DELTA, RelationTable

#: 单角色单块的工具往返上限（§6.3、§10.2 写死）。下一个阶段 graph 的工具循环用它收口：
#: 超限即停止取用、用已有信息出终稿。3 轮的由来是"多跳链接"：读入口 → 顺链读第二跳 →
#: 再一跳就该够了，再多是模型在库里闲逛。
MAX_TOOL_ROUNDS = 3

#: 单角色单块的取用次数上限（§6.3、§10.2 写死）。比轮数宽（一轮可以并行要好几条），
#: 但必须封顶——五个角色同时陷入"回忆漩涡"时，全局的账就是它乘以人数。
MAX_TOOL_CALLS = 8

#: 取用记录的文件名，落在 `runs/<场景>/<角色名>/recall.jsonl`（§6.4，与私有记忆同级）。
RECALL_FILENAME = "recall.jsonl"

#: 未命中时列出几条"最像的"名字（§6.2 的模糊匹配）。3 条足够指路，再多就是噪声。
NEAR_NAME_LIMIT = 3

#: 摘要的长度上限（字符数）。摘要就是索引行 `标题 — 摘要` 的那后半截，而**索引表恒在系统
#: 提示里**：它每一块都要重新计费，还得跟着模型跑完整块。200 是一行的量级（远超"一句话"），
#: 超出的部分几乎只可能是模型顺手把正文塞了进来——那种一行的代价立刻放大到每一次 think。
MAX_SUMMARY_CHARS = 200

#: 正文的长度上限（字符数）。4000 是一则记忆的量级（一屏文字），远超"记一件事"所需；而
#: 取用时这段正文会被整段嵌进 speak 的提示词，一条上万字的正文等于让角色每说一句话都
#: 重读一遍它——那是真正烧钱的地方。
MAX_BODY_CHARS = 4000

#: 同一对关系**每场最多改几次**（《人际关系与场景推进》§6.3 三重约束的第二重，默认 1 次）。
#: 与 `MAX_CLOSENESS_DELTA`（±20，第三重）一起把改关系压成"极少数大事"：一天里关系变一次
#: 已经很多了，若每块都能改，关系就会随对话漂移，那正是这一节最容易做坏的地方。
MAX_RELATION_EDITS_PER_SCENE = 1

#: 关系行里 `description` / `mode` 的长度上限（字符数）。关系行**恒在上下文**（§6.2）：
#: 它无条件进每一次 think 与每一次 speak 的提示词，比索引行更该封顶——这里的失控会被永久
#: 带进此后每一块（散场「保留」后并进本体，下一场每一块还要重付），而用户既看不见那一节
#: （恒在上下文小节不进日志）也没有入口收缩（v1 没有编辑器）。200 是一行短语的量级，与
#: `MAX_SUMMARY_CHARS` 同源（那里的判据正是"这一行每一块都要重新计费"）。
MAX_RELATION_TEXT_CHARS = 200


@dataclass
class TruncateReport:
    """一次 `truncate_scene_after_turn` 做了什么（§5.3 / §5.3.1 / §6.2）。

    `dropped` = 删掉了几条本场条目；`restored` = 有几条条目被还原成了本体原文（或从"被压
    下去"回到现行）；`relations_restored` = 有几行**关系**退回开演时的样子（§6.2：关系小节
    是恒在上下文的回喂源，撤回/重置必须一并回退）；`warnings` = 有什么**没做成**（本体库里
    找不到原文、写盘失败……）。撤回是最需要留痕的动作：调用方（M2 引擎）拿它记一条日志，
    用户才看得出"这次撤回到底动了什么"——只回一个数就没有可记的东西，出了偏差也无从解释。
    """

    dropped: int = 0
    restored: int = 0
    relations_restored: int = 0
    warnings: list[str] = field(default_factory=list)


#: 三个工具的定义（§6.2），OpenAI 函数调用格式。
#:
#: description 是模型**唯一**的行为指引——写得好不好直接决定功能好不好用，所以每条都
#: 讲清"什么时候用、别什么时候用、用错了会怎样"：read_entry 说明正文里的 `[[链接]]`
#: 可以顺着读（图要能多跳，否则角色漏掉半边）；remember 说明不是每句话都值得记、以及
#: 同一件事要用同一个 key（否则同一件事记成两条，索引越用越吵）；revise 说明推翻旧认识
#: 时改它而不是新记一条。语气与角色提示词一致：朴素、直接、不喊口号。
#:
#: 关于"旧内容去哪了"，两条说明都必须**与实现一致**（否则模型会照着谎话做决定）：
#:   · 一个键就是一个文件（`knowledgestore.Library.upsert`），所以**同键**重记是原地
#:     顶掉旧正文——没有第二条可以留。remember 那边明写了这一点，免得模型用半截正文重记
#:     同一件事还以为旧内容还在。
#:   · revise 也是原地替换（`entry.model_copy(update=...)` → `save_library` 整文件重写），
#:     只是**没给的字段不动**。所以它不能承诺"旧说法不会被删掉"：那句描述是 `_press_others`
#:     那条路（同名的**其它**条目只标过时、一字不删），两件事分开写。
TOOL_SPECS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "read_entry",
            "description": (
                "读你信息库里的一条。参数给条目名（索引里看到的那个标题就行，记不太准、"
                "写个大概也能找到最接近的一条）。正文里常常写着 [[别的条目名]]，顺着读"
                "能想起更多——真需要的时候再往下读，别没事一条条翻。返回里还会告诉你"
                "有哪些条目提到了这条。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string",
                            "description": "条目名或标题（可以只写个大概）"},
                },
                "required": ["key"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": (
                "把这一刻新知道的、以后还用得上的事记下来：人名、去处、约定、变故、谁欠"
                "你什么。不是每句话都值得记，只记你日后真的会想起来的。key 是这件事的"
                "名字，同一件事永远用同一个 key（写成差不多的名字也行，认得出是同一件），"
                "不然会记成两条。已经记过的事别再记一遍；新消息推翻了你原先的认识时，"
                "用 revise 改它——同一个 key 再 remember 一次，是拿你现在写的这段把旧正文"
                "整个顶掉，旧的原话不会给你留着。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string",
                            "description": "这件事的名字；同一件事始终用同一个 key"},
                    "title": {"type": "string",
                              "description": "索引里显示的一行标题"},
                    "summary": {"type": "string",
                                "description": "一行摘要，扫一眼就知道要不要细读"},
                    "body": {"type": "string",
                             "description": "正文，可以长；提到别的条目时写 [[条目名]]。"
                                            "你写的这段就是这条的全文（要留着的老话得自己写上）"},
                },
                "required": ["key", "title", "summary", "body"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "revise",
            "description": (
                "改你原来记下的条目：新知道的事推翻了旧认识时用它，不要另起一条新的。"
                "只给你要改的那几项（summary 一行摘要 / body 正文），没给的部分原样不动。"
                "你给了的那一项会被你写的替换掉——正文尤其如此，想留着的老话得自己写上，"
                "别指望它替你留着。同名的其它条目不会被删，只会标成过时。借来的库"
                "（世界观一类）改不了——那不属于你，要记自己的说法就另记一条。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string",
                            "description": "要改的那条的条目名或标题"},
                    "summary": {"type": "string",
                                "description": "改成什么（不给就不动这一项）"},
                    "body": {"type": "string",
                             "description": "改成什么；你写的这段会替换掉原来的正文"
                                            "（不给就不动这一项）"},
                },
                "required": ["key"],
                "additionalProperties": False,
            },
        },
    },
]

#: 第四个工具（《人际关系与场景推进》§6.3）：改**已经在关系表里**的某个人。**不放进
#: `TOOL_SPECS`**：它只给有关系表的角色（`tools(character)` 按角色加），基础三件套
#: 的常量因此一字不动（无关系角色的工具列表与请求体逐字节不变）。
#:
#: description 的第一段就是 §6.3 写死的三重约束里的第一重——那段文案是**契约**，不是
#: 普通说明文字。少了"日常寒暄、心情起伏、一次不愉快的对话都不算"这一句，这个工具会被
#: 每一块都想调一次，而"关系随对话漂移"正是这一节最怕的结果。后两句把另外两重（每场一次、
#: 单次 ±20）也告诉模型，免得它反复撞墙（撞墙的每一次都是一轮往返的钱）。
RELATION_TOOL_SPEC: dict = {
    "type": "function",
    "function": {
        "name": "update_relation",
        "description": (
            "改你对某一个人的关系：关系描述 description、相处模式 mode、亲密度增减 "
            "closeness_delta（给哪项改哪项，没给的原样不动）。只能改你**已经认识**的人"
            "（关系表里有的名字），表里没有的人改不了。"
            "只有在发生了**改变关系本质**的大事时才用（表白、决裂、救命、背叛）。"
            "日常寒暄、心情起伏、一次不愉快的对话——**都不算**。"
            "一场戏里对同一个人最多改一次；亲密度一次最多动 20 点。"),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "那个人的姓名（关系表里写着的名字）"},
                "description": {"type": "string",
                                "description": "关系描述改成什么（不给就不动这一项）"},
                "mode": {"type": "string",
                         "description": "相处模式改成什么（不给就不动这一项）"},
                "closeness_delta": {"type": "integer",
                                    "description": "亲密度增减多少（-20 ~ 20 的整数，"
                                                   "不给就不动这一项）"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
}


def _now_iso() -> str:
    """改关系时盖下的时刻（UTC ISO，秒精度）——`origin.at` 与 `updated_at` 都用它。

    只落进**数据**（§6.1 要求关系行记下"何时、因何而变"），绝不出现在工具**返回文本**里：
    返回文本必须确定性、可复现（模块 docstring 第 1 条），同输入同输出才测得动。
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ jsonl 小工具 --

def _atomic_write_text(path: Path, text: str) -> None:
    """文本原子落盘：同目录临时文件 → fsync → `os.replace`（照抄 scenestore.py 的做法）。

    取用记录要整文件重写（追加一行 = 读回来 + 加一行 + 写回去，因为截断也在改它），
    裸 `open(..., "a")` 在崩溃时留下半截行，下次读就是一条坏记录。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _read_rows(path: Path) -> list[dict]:
    """逐行读 jsonl → dict 列表；**绝不抛**：坏行跳过、文件不存在 → []。

    与 `memory._read_jsonl` 同一口径：取用记录是旁挂的回喂源，里面一行半截的写入不能
    炸掉整块 speak。
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    rows: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _write_rows(path: Path, rows: Sequence[dict]) -> None:
    """若干行 → 原子写整个 jsonl（一行一条、末尾换行）。"""
    _atomic_write_text(Path(path),
                       "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def _row_turn(row: dict) -> int:
    """一条记录的轮次；缺失/非法（老数据、坏值）按 0 计（= 最早，绝不会被误截掉）。

    与 `memory._record_turn` 逐字同口径：截断只问"是不是晚于 cut"，而坏值当成"很晚"
    就等于撤回时悄悄删掉不该删的回喂内容。
    """
    value = row.get("turn", 0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _row_key(row: dict) -> str:
    """一条记录指向的条目键；缺失按空串（同键去重时用它比较）。"""
    return str(row.get("key") or "")


# --------------------------------------------------------------- 名字解析 --

def _one_line(text: Any) -> str:
    """压成一行：索引与工具结果都是一行一条，标题/摘要里混进的换行会毁掉格式。"""
    return " ".join(str(text or "").split())


def _turn_or_zero(turn: Any) -> int:
    """轮次 → int，读不出来按 **0** 计（= 最早，绝不会被误截掉）。**绝不抛。**

    `execute` / `note_recall` / `recall_text` 的入参来自引擎与模型，`None`、怪串都真的
    出现过；`int()` 直接抛出去炸掉的是整块 think，而 worker 会把整块重试——重试即重复
    计费（§13.1）。口径与 `_row_turn` 一致：读不出来就是"最早的那一轮"。
    """
    value = store._as_int(turn)
    return 0 if value is None else value


def _clip(text: str, limit: int) -> tuple[str, bool]:
    """超长截断 → `(截断后的文本, 是否截过)`。零 IO、确定性（同输入同输出）。"""
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _clip_note(clipped: Sequence[tuple[str, int]]) -> str:
    """截断说明的措辞（拼进工具返回文本）。空表 → 空串。

    **必须说出来**：模型下一轮才知道该写短一点；不说的话它会以为自己写进去了，于是一直
    写这么长，而每一块都要为这一行重新计费。
    """
    if not clipped:
        return ""
    parts = "、".join(f"{name}太长，只收了前 {limit} 字" for name, limit in clipped)
    return f"（{parts}。条目要进索引表、取用时还要整段带进提示词，太长会让每一块都多花钱。）"


def _relation_clip_note(clipped: Sequence[tuple[str, int]]) -> str:
    """关系字段被截断时的说明（措辞与 `_clip_note` 同款，落点是"恒在上下文"）。空表 → 空串。

    关系行**无条件**进每一次 think 与 speak 的提示词（§6.2），所以这句"太长"比索引那一版
    更要紧：不说的话模型会以为自己写进去了，于是此后每一块都带着这一段。
    """
    if not clipped:
        return ""
    parts = "、".join(f"{name}太长，只收了前 {limit} 字" for name, limit in clipped)
    return f"（{parts}。关系行每一块都要带进提示词，太长会让每一块都多花钱。）"


def _clip_entry_fields(args: Mapping[str, Any]) -> tuple[str, str, list[tuple[str, int]]]:
    """`remember` 的入参 → `(summary, body, 被截断的字段清单)`。

    只截**摘要与正文**这两项：它们是唯二会长到失控的字段（键与标题都短，且键还要过文件名
    守门那条长度上限）。摘要先压成一行再截（索引是一行一条），正文保留换行（它本来就是
    正文）。截断本身零 IO、确定性，所以工具返回文本仍然可复现。
    """
    summary, cut_summary = _clip(_one_line(args.get("summary")), MAX_SUMMARY_CHARS)
    body, cut_body = _clip(str(args.get("body") or ""), MAX_BODY_CHARS)
    clipped = [(name, limit) for clipped, name, limit in
               ((cut_summary, "摘要", MAX_SUMMARY_CHARS), (cut_body, "正文", MAX_BODY_CHARS))
               if clipped]
    return summary, body, clipped


def _best(entries: Sequence[Entry]) -> Entry:
    """一批等价命中里挑一条：**现行说法优先**，其次保持池内顺序。

    过时条目与它的替代者往往标题一样（新压旧不改标题），此时必须给现行的——把一条
    已被推翻的旧认识当答案回给模型，比回"没找到"更坏。
    """
    for entry in entries:
        if entry.status == "active":
            return entry
    return entries[0]


def _resolve(query: str, pool: Sequence[Entry]) -> Entry | None:
    """把一个名字（键或标题，可能记岔）解析成池子里的一条条目；解析不出 → None。

    三级，先精确后模糊，全部**确定性**：
      1. 键完全相同——模型手里的键来自上一次 read_entry 的回声，最可靠；
      2. 键或标题（忽略大小写）完全相同；
      3. `textsim.ratio` 超过"同一件事"的阈值（`SUBJECT_SIMILARITY_THRESHOLD`）——
         索引里列的是标题，模型照着念也可能念错一两个字。

    阈值复用 §3.5 那一档而不是另调一个：两处问的是同一个问题（"这是不是同一个东西"）。
    """
    name = (query or "").strip()
    if not name:
        return None
    for entry in pool:
        if entry.key == name:
            return entry
    fold = name.casefold()
    exact = [e for e in pool
             if e.title.strip().casefold() == fold or e.key.casefold() == fold]
    if exact:
        return _best(exact)
    scored = [max(textsim.ratio(name, e.title), textsim.ratio(name, e.key)) for e in pool]
    best = max(scored, default=0.0)
    if best < SUBJECT_SIMILARITY_THRESHOLD:
        return None
    return _best([e for e, score in zip(pool, scored) if score == best])


def _near_names(query: str, pool: Sequence[Entry]) -> list[str]:
    """池子里名字最像的几条（按相似度降序，同分保持池内顺序），最多 `NEAR_NAME_LIMIT` 条。

    未命中时给模型一个"下一步读哪条"的抓手。只列相似度 > 0 的：全不相干的标题列出来
    没有指路作用，只会让模型以为库里有这么一号东西。
    """
    scored = [(max(textsim.ratio(query, e.title), textsim.ratio(query, e.key)),
               _one_line(e.title) or e.key)
              for e in pool]
    hits = [name for score, name in sorted(
        (pair for pair in scored if pair[0] > 0), key=lambda pair: -pair[0])]
    return hits[:NEAR_NAME_LIMIT]


def _is_scene_product(origin: EntryOrigin, scene: str) -> bool:
    """这条条目是不是**本场产生的**（§5.3 的截断判据 + 哨兵保险）。

    两个条件缺一不可：

      · `kind == "scene"` 且 `scene` 就是本场——从本体拷进来的条目（manual/import/wide）
        与订阅来的活引用一律不算；
      · `turn != BASELINE_TURN`——场景名跨 run 复用（同名场景再跑一遍，上一场结算进本体的
        条目在本体里也是 `kind=scene` + `scene=本场`）时，光看名字会把基线当成本场产物。
        拷进副本的一律带哨兵（见 knowledgestore 模块 docstring 第 2 条），本场写的不可能是
        那个值——它才是"这不是本场产物"的可靠标记。

    判据与 `knowledgestore.truncate_copy_after_turn` 对齐：那里只管 `kind`/`turn`（本模块
    改不动它），两处的口径必须一起看——本函数为真 ⟺ 那条会被截断删掉。
    """
    return (origin.kind == "scene" and origin.scene == scene
            and int(origin.turn) != store.BASELINE_TURN)


def _miss_text(query: str, pool: Sequence[Entry]) -> str:
    """未命中的回音：说清"没有这条" + 最接近的几条 / 索引是空的 / 这个名字根本不能用。

    三种处境给三种下一步：名字记岔了（去读最像的那条）、还没记过东西（去 remember）、
    这个名字压根做不了条目名（换个叫法）——一句"没找到"会让模型原地重试同一个名字。
    """
    name = _one_line(query) or "（空名字）"
    if not pool:
        return f"你的信息索引现在还是空的，没有「{name}」这条——还什么都没记过。"
    near = _near_names(query, pool)
    if near:
        return (f"信息库里没有「{name}」这条。名字最接近的有："
                + "、".join(near) + "。要读哪条就照着它的名字再读一次。")
    err = entry_key_error(name)
    if err:
        return f"「{name}」不是一个能用的条目名：{err}信息库里也没有名字接近的。"
    return f"信息库里没有「{name}」这条，也没有名字接近的。"


# ------------------------------------------------------------------ 工具结果渲染 --

def _header(entry: Entry) -> str:
    """工具结果的首行：`【标题】摘要`（摘要为空就只留标题）。"""
    out = f"【{_one_line(entry.title) or entry.key}】"
    summary = _one_line(entry.summary)
    return out + summary if summary else out


def _superseder_name(title: str, key: str) -> str:
    """过时提示里"现在以谁为准"的那一个名字：**给得准的键**，标题只作陪衬。

    "新压旧"最常见的形态恰恰是两条**标题一样**（新说法通常沿用旧标题），只报标题的话，
    读「陈掌柜」拿到的是"现在以「陈掌柜」为准"——看上去就是它自己那一行，模型无从分辨该
    去读哪条。键是唯一的（一个键一个文件），所以键一定要给；标题与键不同时才附上标题，
    免得模型对着一个陌生的键发懵。键是**读得到**的那条才走到这里（见 `_read_entry`）。
    """
    if title and title != key:
        return f"「{title}」（键：{key}）"
    return f"「{key}」"


def _read_text(entry: Entry, backlink_names: Sequence[str],
               superseded: tuple[str, str] | None) -> str:
    """read_entry 命中时的返回：首行 + （过时提示）+ 正文 + 反链行。

    反链行是硬要求（§6.2）——图只能单向走的话，角色从"药铺的暗格"摸到"陈掌柜"，却永远
    想不起"那封信也提到过这个暗格"。过时提示同理：不标出来的话，模型会把一条已被推翻的
    旧认识当成现在的事实；而它给的替代者必须是**读得到的那条的键**（见 `_superseder_name`），
    否则模型照着提示去读，只会再拿一句"没有这条"——白烧一轮。

    `superseded` 是 (标题, 键)；`None` = 替代者不在池子里（或本来就没有），此时只说"这条
    已经过时"，绝不把模型引去读一个空条目。
    """
    parts = [_header(entry)]
    if entry.status == "outdated":
        note = "（这条已经标成过时"
        if superseded is not None:
            note += f"，现在以{_superseder_name(*superseded)}为准"
        parts.append(note + "。旧说法还留着，别把它当成现在的事实。）")
    if entry.body.strip():
        parts.append(entry.body.strip())
    parts.append("（指向这条的条目：" + "、".join(backlink_names) + "）"
                 if backlink_names else "（没有别的条目指向这条。）")
    return "\n\n".join(parts)


class KnowledgeAccess:
    """一个角色在**一场戏里**访问自己信息库的适配器（§6.2/§6.3/§6.4）。

    构造参数：
      · `run_root`：本场的运行根（与 `memory.CharacterMemory` 的 root 同参），
        副本与取用记录都按 `runs/<场景>/<角色名>/` 换算，**不自己拼路径**；
      · `scene`：场景名（写进条目的 `origin.scene`，撤回时按它区分"本场产生的"）；
      · `libraries_root`：本体库根（正式布局是 `app/libraries/`）。副本缺席时由它换算
        出角色的本体库来建副本、也用来兜底读；不给 = 这个角色没有本体库（只认副本）；
      · `subscriptions`：角色名 → [(库名, 源库目录)]，显式注入的订阅（§3.4 活引用）；
        没给某个角色时，改读**他的本体库目录**旁的 `subscriptions.json`（§8.2 的落点，
        也是界面上勾选订阅时的写入处）——见 `_subscriptions`。
      · `enabled`：信息库检索总开关（§10.2）。关掉 = 索引不注入、不传 tools、execute
        只回一句话——三条路径都退回今天的行为。

    本类不含任何模型调用：工具循环（要调几次、什么时候收口）是 graph 的事，这里只负责
    单次调用怎么落盘、失败怎么变成文本。
    """

    def __init__(
        self,
        run_root: Path,
        scene: str,
        *,
        libraries_root: Path | None = None,
        subscriptions: Mapping[str, Sequence[tuple[str, Path]]] | None = None,
        enabled: bool = True,
    ) -> None:
        self.run_root = Path(run_root)
        self.scene = scene
        self.libraries_root = Path(libraries_root) if libraries_root is not None else None
        self.subscriptions: dict[str, list[tuple[str, Path]]] = {
            str(name): [(str(lib_name), Path(path)) for lib_name, path in rows]
            for name, rows in (subscriptions or {}).items()
        }
        self.enabled = bool(enabled)

    # ------------------------------------------------------------ 路径换算 --

    def _copy_dir(self, character: str) -> Path:
        """本场副本目录（写着落点的唯一换算，见 knowledgestore.scene_library_dir）。"""
        return store.scene_library_dir(self.run_root, character)

    def _own_dir(self, character: str) -> Path | None:
        """角色本体库目录；没给 `libraries_root` → None（这个角色没有本体库）。"""
        if self.libraries_root is None:
            return None
        return store.library_dir("character", character, root=self.libraries_root)

    def _own_library(self, character: str) -> store.Library | None:
        """角色本体库（**只读**，只用于给副本里被顶掉的基线条目找回原文）；没有 → None。

        本体在整场戏里一个字都不动（§5.2），所以它是一份稳定的原文来源：副本里的基线条目
        本来就是从它拷进来的，用同一个键去它那儿取，拿到的就是"这条在本场之前是什么样"。
        读了不写——回写本体是 M3 结算的事，本模块永远不碰它。
        """
        own = self._own_dir(character)
        if not self._exists(own):
            return None
        return store.load_library(own).library

    def _exists(self, path: Path | None) -> bool:
        """这个库目录里有没有东西（`library.json` 或 `entries/`，与 store 同一判据）。"""
        return bool(path is not None
                    and (store.has_library(path) or store.entries_dir(path).is_dir()))

    def _read_dir(self, character: str) -> Path:
        """**读**用哪座库：副本优先，副本不在则回落本体；都没有 → 副本路径（读出来是空库）。

        回落只对读成立：副本还没建（引擎的进场钩子没跑到、或角色是 CLI 路径加进来的）
        时，不回落等于让这个角色这一场什么都想不起来，而且没有任何提示。写绝不回落，
        见 `_ensure_copy`。
        """
        copy_dir = self._copy_dir(character)
        if self._exists(copy_dir):
            return copy_dir
        own = self._own_dir(character)
        return own if self._exists(own) else copy_dir

    def _ensure_copy(self, character: str) -> Path:
        """确保副本存在并返回它：已存在则**原样复用**，没有才从本体拷一份/建空副本（§5.1）。

        本体不可知时把副本路径本身当源传进去——那时它必定还不存在（上面刚判过），走的正是
        `ensure_scene_copy` 里"源库没有 → 建空副本"那条分支。`ensure_scene_copy` 绝不重置
        已存在的副本，所以他中途离场又回来，这一场学到的东西还在。
        """
        copy_dir = self._copy_dir(character)
        own = self._own_dir(character)
        store.ensure_scene_copy(own if own is not None else copy_dir, copy_dir)
        return copy_dir

    def _subscriptions(self, character: str) -> list[tuple[str, Path]]:
        """这个角色订阅的库（库名 + 源库目录），没订阅就是空表。

        两个来源，**显式传入的优先**：

          · 构造参数 `subscriptions`（调用方注入/测试钉死用，见类 docstring）；
          · 没有显式给这个角色时，读**本体库目录**旁的 `subscriptions.json`（§8.2 的
            落点）。订阅挂在**本体**那侧而不是这一场的副本：他订了什么随人走，副本是
            逐场一份的运行产物。路径换算一律走存储层（`library_dir`），本模块绝不自己拼。

        读侧宽容由 `store.load_subscriptions` 保证（缺文件/坏 JSON/坏项/逃逸路径只丢自己，
        绝不抛）：一个手改坏的订阅文件不该让角色这一场什么都想不起来。
        """
        explicit = self.subscriptions.get(character)
        if explicit:
            return list(explicit)
        own = self._own_dir(character)
        if own is None:
            return []
        try:
            return store.load_subscriptions(own, root=self.libraries_root)
        except Exception:                     # 最后一张网：索引渲染绝不该在这里炸
            return []

    def _subscribed_entries(self, character: str) -> list[tuple[str, list[Entry]]]:
        """订阅的**活引用**：现去每个源库读一次（§3.4，对方一改我立刻看到）。"""
        return store.subscribed_rows(self._subscriptions(character))

    def _pool(self, character: str) -> list[Entry]:
        """读得到的全部条目：自有（副本，副本缺席则本体）在前，订阅来的在后。

        自有在前是有意的：同名时以我自己的那一条为准（它才是我改得动的那条）。固化成
        自有条目的那一键，借入的那条**整条不进池**（`store.visible_borrowed`）——只靠
        先后排序不够：按标题找的那条路会绕过同键的自有条目，摸回源库那一版，而它已经
        不在索引上了（模型没法知道自己读到的是一条看不见来源的说法）。
        """
        entries = store.load_library(self._read_dir(character)).library.ordered_entries()
        own = list(entries)          # 快照：下面 extend 的是同一个 list，别名会被一起撑大
        for _, rows in self._subscribed_entries(character):
            entries.extend(store.visible_borrowed(own, rows))
        return entries

    def _titles(self, entries: Sequence[Entry]) -> dict[str, str]:
        """键 → 标题（反链与"以谁为准"的提示都显示模型看得懂的标题，而不是键）。"""
        return {e.key: (_one_line(e.title) or e.key) for e in entries}

    # -------------------------------------------------------------- 索引表 --

    def index_text(self, character: str) -> str:
        """本角色的索引小节（§6.1）：`index_text_ex` 的**不管上限**那一档。

        空串那条硬约束（无库角色的系统提示逐字节不变）落在被转调的那一个里，这里只取文本。
        拆成两个入口是为了让"要上限"的调用方（将来按 §10.2 的配置传 `max_entries`）能同时
        拿到丢弃了几行：静默截断是这一节最不该有的行为（用户会以为"他记得的东西变少了"
        却找不到原因），而丢弃计数只有渲染方知道——`render_index` 是纯渲染，不写盘不记日志。

        **上限今天没有接线**：`settings.py` 里还没有对应的配置项，引擎两处取索引
        （`graph.py` 的 think 与 speak）都走的 `index_text_ex(character)` 这一档，即不设
        上限。要真按 §10.2 截断，得先有配置项、再由调用方把 `max_entries` 传进来——
        在那之前这条闸门是备好的参数位，不是正在生效的行为。
        """
        return self.index_text_ex(character)[0]

    def index_text_ex(self, character: str, *,
                      max_entries: int | None = None) -> tuple[str, int]:
        """索引小节 + **被上限丢掉的行数**（§6.1 / §10.2）。**绝不抛。**

        `max_entries` 是注入量的上限（§10.2）：按显示顺序保留前 N 条、其余丢弃，自有在前、
        订阅在后、跨组一起算（口径与 `knowledge.render_index` 一致，本方法不自己实现截断）。
        上限存在的理由：索引表恒在系统提示里，它随角色的记性无限长下去，代价落在**每一块**
        的输入上；丢掉最旧的几行是"记得多"与"每块都贵"之间那个必须有人做的取舍。

        返回的第二个值是**这次丢了几行**，调用方据此记一条日志（§10.2 要求"截断最旧并记日志，
        不静默"）。`None` = 不设上限，恒返回 0；空库/关库 → `("", 0)`：没有行可丢，也绝不把
        空串变成占位文本（那是 §6.1 的硬约束）。

        上限读不出来（配置是坏值）时**当没设上限**：为一个坏配置把整节索引变成空串，等于
        让角色突然什么都想不起来——降级到"照常注入"是这里代价最小的方向。
        渲染失败同样退回空串（这一节拼在系统消息末尾，为它让整块 think 炸掉就是一次整块重试）。
        """
        if not self.enabled:
            return "", 0
        limit = store._as_int(max_entries)
        try:
            read_dir = self._read_dir(character)
            subscriptions = self._subscriptions(character)
            text = store.render_library_index(read_dir, subscriptions=subscriptions,
                                             max_entries=limit)
            if limit is None:
                return text, 0
            own = store.load_library(read_dir).library.ordered_entries()
            visible = self._visible_count(own)
            for _, rows in store.subscribed_rows(subscriptions):
                # 计数必须与渲染同一套判据：固化掉的那几条不在索引上，也不算这一节的行数
                # （`render_library_index` 用的就是 `visible_borrowed`），否则报出来的
                # "丢了几行"是编的。
                visible += self._visible_count(store.visible_borrowed(own, rows))
            return text, max(0, visible - max(0, limit))
        except Exception:
            return "", 0

    @staticmethod
    def _visible_count(entries: Sequence[Entry]) -> int:
        """这批条目里索引**真的会列出来**的有几条（与 `knowledge._visible` 同一判据）。

        计数必须与渲染用同一个判据，否则报出来的"丢了几行"是编的：过时条目与
        `indexed=false` 的条目本来就不在索引里（§3.3），把它们算进去会让每次注入都报一条
        不存在的截断，日志里全是噪声，真出问题时反倒看不见。
        """
        return sum(1 for e in entries if e.indexed and e.status == "active")

    # -------------------------------------------------------------- 关系表 --
    #
    # 关系表是信息库里的一处**特殊分区**（《人际关系与场景推进》§6.2/§6.5）：读写走同一套
    # 副本规则（读回落本体、写只写副本），结算交给存储层（`settle_relations`），渲染交给
    # `relations.render_relations`。本类只负责"什么时候读、读到什么给谁"。

    def _relations(self, character: str) -> store.LoadedRelations:
        """这个角色现在的关系表：副本优先、副本缺席回落本体（读侧宽容由存储层保证）。

        **绝不抛**（最后一张网与 `index_text_ex` 同款）：关系表是旁挂数据，为一个读不动的
        文件让整块 think 炸掉、再整块重试一遍，代价与收益完全不成比例。
        """
        try:
            return store.load_relations(self._read_dir(character))
        except Exception:
            return store.LoadedRelations(table=RelationTable())

    def relation_text(self, character: str) -> str:
        """这个角色的【你的人际关系】小节（§6.2）；**没有任何关系行 → 空串**。

        与索引的差别是本节的全部要点：索引是"想知道更多时去翻的书"（可以按上限截断、按需
        取用），关系表是**每次开口都要用的几行**——"我对面这个人是谁"不能等到想起来才用。
        故这里把**全部**关系行原样渲染出来，不设上限、不截断（判断写在
        `relations.render_relations` 的 docstring 里）。空串那条硬约束与索引同款：无关系
        角色的系统提示逐字节不变。**绝不抛**（这一节拼在系统消息里，为它让整块 think 炸掉
        就是一次整块重试）。
        """
        if not self.enabled:
            return ""
        try:
            return relations_mod.render_relations(self._relations(character).table)
        except Exception:
            return ""

    def relations_warnings(self, character: str) -> list[str]:
        """这个角色**这一场实际读到**的关系表有什么读盘问题（§6.2）。**绝不抛。**

        字面可以直接给用户看（存储层那一层就写成了中文），调用方（引擎）负责把它记进
        `settlement_warnings()`。加这一条是因为**工具层说不出这件事**：关系表读不出来时
        表退成空 → `relation_text` 是空串（提示词里没有小节）、`tools` 只给三件套（拿不到
        `update_relation`），于是 `_relation_miss` 里那段专门写来报读盘问题的话成了够不到
        的死代码——而用户手写的那份**正是唯一会坏的那一份**（副本是程序写的）。不说的话，
        用户看到的是"这个角色忽然一个熟人都不认得了"，而程序一声不吭。

        读哪一份与 `_read_dir` 同一判据（副本优先；副本里那份缺失时回落到本体那一份）：
        副本有 `relations.json` 就报副本那份的问题（它才是生效的那份），否则报本体那份——
        那正是"副本里那份没能拷过来"的原因。
        """
        copy_dir = self._copy_dir(character)
        if store.has_relations(copy_dir):
            return list(store.load_relations(copy_dir).warnings)
        own = self._own_dir(character)
        if own is None:
            return []
        return list(store.load_relations(own).warnings)

    def tools(self, character: str = "") -> list[dict]:
        """工具的定义（§6.2）；调用方拿到什么就原样交给后端。

        必须是**深拷贝**：`TOOL_SPECS` 是模块级常量（所有角色、所有场景共用一份）。浅拷贝
        只复制了外层 list，里面的 dict / description 还是全局那一份——调用方（graph）顺手
        补一句 description、或按角色删掉一个参数位，改的就是所有人共用的定义，而且这种串味
        只在"另一个角色用了别的工具描述"时才显形，最难排查的那一类。

        **按角色给**（《人际关系与场景推进》§6.3）：只有**关系表非空**的角色才多拿到
        `update_relation`。给没有关系表的人塞一个用不上的工具，等于让他的每一块都多背一段
        描述（`TOOL_SPECS` 那三个是所有人的固定成本，第四个不是），还会诱导模型去调它，
        然后拿到一句拒绝——那一轮往返的钱白花。故 `character` 缺省 ""（说不出是谁）与
        关库时都返回基础三件套，与今天逐字节相同。

        无库/关闭时照常返回：传不传 tools 是 §6.3 那条成本闸门的事，落点在调用方，
        不该由这里返回空表来替它决定。
        """
        specs = copy.deepcopy(TOOL_SPECS)
        if character and self.enabled and len(self._relations(character).table):
            specs.append(copy.deepcopy(RELATION_TOOL_SPEC))
        return specs

    # -------------------------------------------------------------- 工具执行 --

    def execute(self, character: str, name: str, args: Any, *, turn: int) -> str:
        """本地执行一次工具调用，返回**给模型看的文本**。**绝不抛**（§13.1）。

        `args` 吃两种形状：后端解析好的 dict，以及 OpenAI `tool_calls.arguments` 那个
        JSON 字符串（真跑起来给的就是后者，适配器得自己认）。

        副作用：`read_entry` 命中时顺手把这条正文记进 recall.jsonl（§6.4）——取用即记录，
        落点就在这一次取用发生的这一刻，调用方不必另行接头。
        """
        try:
            return self._dispatch(character, name, args, _turn_or_zero(turn))
        except Exception as exc:                      # 兜底：任何没预见的错都只变成一句话
            return (f"「{name}」没跑成：{exc}。换个办法再试一次，或者先不管这条。")

    def _dispatch(self, character: str, name: str, args: Any, turn: int) -> str:
        """按工具名分发；认不出来/参数读不出来都给文本。"""
        if not self.enabled:
            return "（信息库检索关着，这次不查库。）"
        payload = _coerce_args(args)
        if payload is None:
            return (f"「{name}」的参数读不出来，得给一个 JSON 对象"
                    f"（收到的是：{_one_line(args) or type(args).__name__}）。")
        if name == "read_entry":
            return self._read_entry(character, payload, turn)
        if name == "remember":
            return self._remember(character, payload, turn)
        if name == "revise":
            return self._revise(character, payload, turn)
        if name == "update_relation":
            return self._update_relation(character, payload, turn)
        # 名字念错/自己发明一个时，把**这个角色手上真有**的那几件列出来：只报基础三件套会
        # 让有关系表的角色以为 update_relation 不存在，白白多试几次。
        available = "、".join(str(spec["function"]["name"]) for spec in self.tools(character))
        return f"没有「{name}」这个工具。能用的只有 {available}。"

    def _read_entry(self, character: str, args: dict, turn: int) -> str:
        """read_entry：按名字取正文 + 反链行；未命中给出最接近的几条。"""
        query = str(args.get("key") or "").strip()
        if not query:
            return "要读哪一条？给我个条目名。"
        pool = self._pool(character)
        entry = _resolve(query, pool)
        if entry is None:
            return _miss_text(query, pool)
        names = self._titles(pool)
        incoming = [names.get(key, key) for key in backlinks(pool, entry.key)]
        # "现在以谁为准"只在那条**真的读得到**时才说：撤回会删掉本场的替代者，留下一条指向
        # 不存在键的过时条目——照着它去 read_entry 只会再拿一句"没有这条"，白烧一轮。
        superseded_by = str(entry.superseded_by or "")
        superseded = ((names[superseded_by], superseded_by)
                      if superseded_by in names else None)
        # 取用即记录（§6.4）：think 想起来的东西要能延续到本块的 speak。写不进去只当
        # 没记——正文已经取到了，不该因为一本旁挂的账没记上而作废。
        self.note_recall(character, turn, entry.key, entry.body,
                         title=_one_line(entry.title) or entry.key)
        return _read_text(entry, incoming, superseded)

    def _remember(self, character: str, args: dict, turn: int) -> str:
        """remember：写进本场副本，同 key/同副标题命中既有条目则走"新压旧"（§3.5）。

        摘要与正文过 `_clip_entry_fields` 截断（上限见 `MAX_SUMMARY_CHARS` /
        `MAX_BODY_CHARS`）：索引行与取用提示词都在提示词里，一条失控的摘要是**每一块**
        的成本。
        """
        key = str(args.get("key") or "").strip()
        err = entry_key_error(key)
        if err:
            return f"这条记不下来：{err}换个名字（别带 / \\ 这类字符）再记一次。"
        copy_dir = self._ensure_copy(character)
        title = _one_line(args.get("title")) or key
        summary, body, clipped = _clip_entry_fields(args)
        entry = Entry(
            key=key, title=title,
            summary=summary, body=body,
            status="active",
            origin=EntryOrigin(kind="scene", scene=self.scene, turn=turn),
        )
        lib = store.load_library(copy_dir).library
        previous = lib.entries.get(key)
        if previous is not None:
            # 同键 = 同一条目（一个键就是一个文件）：模型重记同一件事，用途是补全/订正，
            # 故原地顶上；把它此前压下去过的条目接着记在新的这条上，免得链条断掉。
            entry.supersedes = list(previous.supersedes)
        pressed = _press_others(lib, entry)
        entry.supersedes = _dedupe(entry.supersedes + pressed)
        lib.upsert(entry)
        store.save_library(lib, copy_dir)
        # 命中了既有条目 = 改了一条**原有的认识**，记进"本场修订"那一半（§7.2）。记成"新增"
        # 的话，用户在本场所得清单上看不出某条既有认识已被替换。若这条本来就是本场长出来的，
        # record_pending 会把它留在 added 里——那才是它该在的地方。
        # 轮次一并记下：同键 remember 顶掉基线条目时，撤回正是靠它判断"这次覆盖沾没沾
        # 被撤回的下文"（§5.3.1）。
        store.record_pending(copy_dir, key, revised=previous is not None, turn=turn)

        out = f"记下了【{title}】（键：{key}）。"
        if previous is not None:
            out += ("同名的那条旧内容已被这份新的顶上（要留着旧的原话，用 revise "
                    "只改要改的那几句，别整个重记）。")
        if pressed:
            out += ("旧的「" + "、".join(lib.entries[k].title or k for k in pressed)
                    + "」标成了过时，内容没删。")
        return out + _clip_note(clipped)

    def _revise(self, character: str, args: dict, turn: int) -> str:
        """revise：改副本里的既有条目；广域库（订阅）条目**拒绝**（§3.4）。

        改过来的摘要与正文同样过上限（与 `_remember` 共用 `_clip_entry_fields`）：修订一次
        写多长，对提示词的影响与首次记下的一模一样。
        """
        query = str(args.get("key") or "").strip()
        if not query:
            return "要改哪一条？给我个条目名。"
        clipped: list[tuple[str, int]] = []
        updates: dict[str, str] = {}
        if args.get("summary") is not None:
            updates["summary"], too_long = _clip(_one_line(args["summary"]), MAX_SUMMARY_CHARS)
            clipped += [("摘要", MAX_SUMMARY_CHARS)] if too_long else []
        if args.get("body") is not None:
            updates["body"], too_long = _clip(str(args["body"]), MAX_BODY_CHARS)
            clipped += [("正文", MAX_BODY_CHARS)] if too_long else []
        if not updates:
            return ("你什么都没说要改什么：至少给 summary（一行摘要）或 body（正文）"
                    "其中一项，没给的那项保持原样。")
        # 改之前先确保副本在（§5.1）：副本缺席时本体条目还没来得及拷进来，此时说"没有这条"
        # 是假的——他要改的正是自己本体里的那条。
        copy_dir = self._ensure_copy(character)
        lib = store.load_library(copy_dir).library
        pool = list(lib.ordered_entries())
        for _, rows in self._subscribed_entries(character):
            pool.extend(rows)
        entry = _resolve(query, pool)
        if entry is None or entry.key not in lib.entries:
            return self._refuse(character, query, pool)
        entry = entry.model_copy(update=updates)
        entry.status = "active"
        entry.superseded_by = None
        if _is_scene_product(entry.origin, self.scene):
            # 本场条目重盖一次轮次：这条现行说法是**本轮**确立的，撤回本轮那段就该跟着走
            # （§5.3）。基线条目不盖——它的出处不是这一场，盖了反而让"这条不是本场产物"
            # 这件事变模糊；更糟的是哨兵一被盖掉，store 的截断判据立刻成立，撤回会把这条
            # **先于本场存在**的记忆整条删掉（上一场保留的长期记忆就是这样丢的）。
            entry.origin = entry.origin.model_copy(update={"turn": turn})
        entry.supersedes = list(lib.entries[entry.key].supersedes)
        entry.supersedes = _dedupe(entry.supersedes + _press_others(lib, entry))
        lib.upsert(entry)
        store.save_library(lib, copy_dir)
        # 记下这次修订的轮次（§5.3.1）：基线条目被本场改动之后，撤回要靠它判断"这次改动
        # 沾没沾被撤回的下文"——改过基线的现行说法，撤回时必须还原成本体原文。
        store.record_pending(copy_dir, entry.key, revised=True, turn=turn)
        return (f"改好了【{_one_line(entry.title) or entry.key}】（键：{entry.key}）。"
                + _clip_note(clipped))

    def _refuse(self, character: str, query: str, pool: Sequence[Entry]) -> str:
        """revise 找不到可改的条目：要么是借来的（广域库，拒绝），要么真的没有。

        借来的那条要**说清为什么**（§3.4：世界观不该被某个角色在场景里改写），并给出
        替代动作（用 remember 记自己的说法）——干巴巴一句"失败"会让模型反复重试同一个
        动作，那正是工具循环里最贵的失败姿势。
        """
        for lib_name, rows in self._subscribed_entries(character):
            if _resolve(query, rows) is not None:
                return (f"「{_one_line(query)}」是借自《{lib_name}》的条目，你不能改它"
                        f"——那不属于你，也不是你能定的。要记你自己的说法，用 remember "
                        f"另记一条。")
        return _miss_text(query, pool) + "新的事实请用 remember 记一条。"

    # ------------------------------------------------------------ 改关系（§6.3） --

    def _update_relation(self, character: str, args: dict, turn: int) -> str:
        """`update_relation`（《人际关系与场景推进》§6.3）：改**已经在关系表里**的某个人。

        五步的顺序是刻意的——每一道先说清"为什么不行"，模型才知道下一步怎么办：

          1. **参数**：姓名要给；`description` / `mode` / `closeness_delta` 至少给一项
             （与 `revise` 同一条口径：什么都没说要改什么，等于白跑一趟）；
          2. **幅度闸**（三重约束的第三重）：`closeness_delta` 单次 ≤ `MAX_CLOSENESS_DELTA`，
             超出**拒绝**（不是夹取，见那里的理由），且**不消耗本场的额度**——一次写错的量
             不该把这一场唯一的那次机会用掉；
          3. **存在性**：表里没有这个人 → 拒绝 + 给出替代动作（用 remember 记自己的说法）。
             **绝不顺手新建一行**：谁能进这张表是用户/编辑器的事；
          4. **频率闸**（第二重）：同一对每场最多 `MAX_RELATION_EDITS_PER_SCENE` 次，
             账本在副本的 `pending.json` 里（跨块、跨 think 都算同一场）；
          5. 写**本场副本**（§5.2），再记账本。顺序是"先写盘、后记账"：写盘失败（IO）时
             额度不该被花掉，模型还能重试；反过来先记账再写，一次失败的写会把这唯一的一次
             机会吃掉，而用户什么都没得到。

        两处入参收口（与 `remember` / `revise` 同款）：`description` / `mode` 过
        `MAX_RELATION_TEXT_CHARS`（关系行恒在上下文，失控的长度会被永久带进此后每一块，
        见 `_relation_clip_note`），账本里顺手记下**第几轮**改的与**改之前那一行的底稿**
        （`store.note_relation_edit`）——撤回、重置与散场按字段合并都靠这两样，见
        `knowledgestore.truncate_relations_after_turn` 与 `_patch_row`。

        失败一律返回给模型看的文本、绝不回抛（§13.1 的既有契约）。返回文本**确定性**
        （不盖时间戳）：它只说改成了什么，说得出下一步。
        """
        name = str(args.get("name") or "").strip()
        if not name:
            return "要改谁的关系？给我那个人的姓名。"
        has_description = args.get("description") is not None
        has_mode = args.get("mode") is not None
        raw_delta = args.get("closeness_delta")
        if not (has_description or has_mode or raw_delta is not None):
            return ("你什么都没说要改什么：至少给 description（关系描述）、mode（相处模式）"
                    "或 closeness_delta（亲密度增减）里的一项，没给的项保持原样。")
        clipped: list[tuple[str, int]] = []
        description = mode = None
        if has_description:
            description, cut_desc = _clip(_one_line(args.get("description")),
                                          MAX_RELATION_TEXT_CHARS)
            if cut_desc:
                clipped.append(("关系描述", MAX_RELATION_TEXT_CHARS))
        if has_mode:
            mode, cut_mode = _clip(_one_line(args.get("mode")), MAX_RELATION_TEXT_CHARS)
            if cut_mode:
                clipped.append(("相处模式", MAX_RELATION_TEXT_CHARS))
        delta: int | None = None
        if raw_delta is not None:
            delta = store._as_int(raw_delta)
            if delta is None:
                return f"亲密度的增减得是一个整数（收到的是「{_one_line(raw_delta)}」）。"
            if abs(delta) > MAX_CLOSENESS_DELTA:
                return (f"一次最多只能动 {MAX_CLOSENESS_DELTA} 点亲密度（你给的是 {delta}）"
                        f"——关系不是一天变的。要么少改一点，要么这次先算了。")

        # 写关系与写条目同一套落点换算（§5.2）：副本缺席就先建（他本体那份是基线），
        # 副本里还没有关系表就从本体拷一份过来——否则角色一开场就对所有熟人失忆。
        copy_dir = self._ensure_copy(character)
        store.ensure_relations_copy(self._own_dir(character) or copy_dir, copy_dir)
        loaded = store.load_relations(copy_dir)
        table = loaded.table
        row = table.get(name)
        if row is None:
            return self._relation_miss(name, table, loaded.warnings)
        count = store.relation_edits(copy_dir).get(name, 0)
        if count >= MAX_RELATION_EDITS_PER_SCENE:
            return (f"「{name}」这一场已经改过一次了——关系这一场只能动一次。这次没有改；"
                    f"真有大变故，留到下一场再说。")

        before = row.closeness
        # 底稿 = **改它之前**那一行的样子（撤回时退回它、结算时用它算出本场动过哪几个字段）。
        # 在 apply 之前抓：`apply` 返回新行、`upsert` 也只是换掉表里那一格，拿在手里的这一行
        # 不会被就地改掉。
        base_row = row.model_dump(mode="json")
        stamp = _now_iso()
        updated = table.apply(
            name,
            description=description,
            mode=mode,
            closeness_delta=delta,
            origin=EntryOrigin(kind="scene", scene=self.scene, turn=turn, at=stamp),
            updated_at=stamp)
        table.upsert(updated)
        store.save_relations(copy_dir, table)
        store.note_relation_edit(copy_dir, name, turn=turn, base=base_row)

        parts: list[str] = []
        if has_description:
            parts.append(f"关系：{_one_line(updated.description)}")
        if has_mode:
            parts.append(f"相处：{_one_line(updated.mode)}")
        if delta is not None:
            landed = before + delta
            edge = "（已经到边界了）" if updated.closeness != landed else ""
            parts.append(f"亲密度 {before} → {updated.closeness}{edge}")
        return f"改好了「{name}」：{'；'.join(parts)}。{_relation_clip_note(clipped)}"

    def _relation_miss(self, name: str, table: RelationTable,
                       warnings: Sequence[str]) -> str:
        """关系表里没有这个人：说清"改不了谁" + 下一步该做什么（用 remember）。**绝不静默。**

        顺带把表里现有的名字列出来（模型照着念一遍就能改对，比它自己猜一个名字便宜得多），
        以及**读盘时看见的问题**——关系表坏掉时唯一的知情渠道就是这里（用户看不到日志），
        不说出来，角色会莫名其妙地"一个熟人都不认得了"。
        """
        out = (f"你的关系表里没有「{name}」这个人，这次没有改。这张表只记你**已经有的**"
               f"关系——新认识一个人的事，用 remember 记一条。")
        names = table.names()
        if names:
            out += "关系表里现在有：" + "、".join(names) + "。"
        if warnings:
            out += "（你的关系表这次没读全：" + "；".join(warnings) + "）"
        return out

    # -------------------------------------------------------------- 取用通道 --

    def _recall_path(self, character: str) -> Path:
        """取用记录落点：`runs/<场景>/<角色名>/recall.jsonl`（§6.4，与私有记忆同级）。"""
        return self.run_root / character / RECALL_FILENAME

    def note_recall(self, character: str, turn: int, key: str, text: str,
                    *, title: str = "") -> None:
        """记一条取用（§6.4）：think 取了哪条、正文是什么、当时是第几轮。**绝不抛。**

        同一轮同一键只留一条（后一次覆盖前一次）：工具循环里重复读同一条目、或多个入口
        汇到同一条时，重复行会让 speak 的提示词里同一段正文出现两遍——白烧 token，还会
        让模型以为"这事被强调了两次"。

        写不进去只当没记：取用记录是旁挂的账本，丢一本不该让已经取到的正文作废，更不该
        炸掉整块 think。轮次读不出来（None、怪串）按 0 计而不是抛（`_turn_or_zero`）：
        这条方法的 docstring 写着"绝不抛"，一个坏轮次炸掉整块 think 就是一次重复计费。
        """
        row = {"turn": _turn_or_zero(turn), "key": str(key), "title": str(title or key),
               "text": str(text)}
        path = self._recall_path(character)
        rows = [r for r in _read_rows(path)
                if not (_row_turn(r) == row["turn"] and _row_key(r) == row["key"])]
        rows.append(row)
        try:
            _write_rows(path, rows)
        except OSError:
            pass

    def recall_text(self, character: str, turn: int) -> str:
        """本块该轮取用到的正文合集（供 speak 注入，§6.4）；没有 → 空串。

        **只看本轮的**：think 与 speak 是同一块里同一个角色的连续两步（同轮），跨轮的
        取用是上一块的事——那一段该由撤回机制裁剪，不该被这一块的 speak 顺带带上。

        轮次读不出来按 0 计（`_turn_or_zero`）：**绝不抛**。契约写的是"没有 → 空串"，
        而不是"坏轮次 → 整块 speak 炸掉"。
        """
        wanted = _turn_or_zero(turn)
        texts = [str(row.get("text") or "").strip() for row in _read_rows(
            self._recall_path(character)) if _row_turn(row) == wanted]
        return "\n\n".join(text for text in texts if text)

    def truncate_recall_after_turn(self, character: str, cut: int) -> None:
        """保留 turn ≤ cut 的取用，其余丢弃（§5.3，口径与 `memory.truncate_after_turn` 对齐）。

        它是回喂源，故撤回必须回溯到它：撤回某条推进后，角色不能还带着"从没发生过的事"
        的知识开口——那是 `memory.truncate_after_turn` 当初解决的同一类问题。缺轮次的
        记录按 0 计（永不误截），文件不在、写不动都当无事发生：撤回不该因 IO 失败中断。

        `cut` 读不出来 → **空操作**（`_as_int` 给 None）：这里不能照搬"坏值按 0 计"——
        0 对撤回位置意味着"把本轮以内的取用一次删光"，包括刚发生的这一轮。撤回的位置猜
        小一点就是误删，判据读不出来时唯一安全的动作是不做。
        """
        limit = store._as_int(cut)
        if limit is None:
            return
        try:
            if not self._recall_path(character).is_file():
                return
            keep = [row for row in _read_rows(self._recall_path(character))
                    if _row_turn(row) <= limit]
            _write_rows(self._recall_path(character), keep)
        except OSError:
            pass

    # -------------------------------------------------------- 撤回：副本按轮次截断 --

    def truncate_scene_after_turn(self, character: str, cut: int) -> TruncateReport:
        """撤回：本场副本按轮次截断，并把被改动过的一切痕迹一起带走（§5.3 / §5.3.1）。

        **调用方必须走这里、不要直接调 store 那个**：光删条目只做了一半的撤回，下面三处
        收尾都做在本方法里。返回 `TruncateReport`（删了几条、还原了几条、有什么没做成）——
        撤回是最需要留痕的动作，只回一个数调用方就没法记出一条能看懂的日志。

        1. **本场新建的条目（`kind == "scene"` 且非哨兵）按轮次删除**（`turn > cut`）。
        2. **它压下去的旧条目要放开**。本场先记「陈掌柜」（轮次 1）、再记一条同标题的
           「陈掌柜的底细」（轮次 5）把它压成过时；撤回轮次 5 之后，被截掉的那条**从没发生
           过**，可「陈掌柜」还挂着 outdated——索引默认不列过时条，角色从此看不见它，而
           read_entry 会说"现在以「陈掌柜的底细」为准"，那条已经不在库里了。撤回的语义是
           "角色不能带着从没发生过的事开口"，反过来丢掉确实发生过的事是同一类错。
           放开**不等于一律清成现行**：副本里还活着的条目若也压着它（cut 之内更早的那条），
           它仍该是过时的那一条——见 `_settle_press`。两条同标题的现行说法并列在索引里，
           模型会把已被推翻的旧正文当现状开口，比原来那个毛病更坏。
        3. **它顶掉 / 改过的基线条目要还原成本体原文**（§5.3.1）。同键 remember 是原地覆盖
           （一个键一个文件），revise 也是原地替换——一个键一条，旧原文只剩在本体里。§5.2
           保证本体整场只读，所以它是可靠的原文来源（连那条原来的 status / superseded_by /
           indexed 一起还：本体里本来就过时的条目不该被"救活"）。
           "本场改过哪条基线、第几轮改的"记在 `pending.revised_turns` 里——**轮次缺失
           （老 `pending.json` 没有这个字段）的那几条保守不回退**：无从判断它是否沾了被撤回
           的下文，而猜一个轮次是拿用户的数据赌。这条降级口径写在
           `knowledgestore.PendingChanges` 的 docstring 里。

        还原一律**按本体原文整条覆盖**，而不是"把状态翻成 active"：后者会把一条早就被推翻
        的旧认识变成现行说法。但"换成本体原文"有个前提——那条正文**不是本场写下的**：
        同键重记与 revise 都是原地替换，本场在 cut 之内写下的正文是"确实发生过的事"，
        换回旧文就是撤回回退过头（判据见 `_keeps_own_body`），此时只放开它的状态、正文
        一个字不动。

        本体里没有这个键（本场才长出来的条目、这个角色根本没有本体库、或那条已经被用户
        删掉）→ 没有原文可取，**绝不静默**，但两半的收尾不同：
          · 还原那一半（本场改过基线）——该条**一个字都不动**，并把"没能还原"记进
            `warnings`；猜一句"翻成 active"是编造，删掉它是更大的损失。它**留在 `pending`
            里**：确实还是本场的一处改动，散场清单上要看得见（用户自己决定保留还是丢弃），
            而且它仍在压着它的对手（不许被当成"没了"去放开对手）。
          · 删除那一半（本场同键重记顶掉了开演前那条，见 `pending.revised`）——那条本场
            条目照旧删除，而先于本场存在的那条原文再也取不回来：这件事**写进 `warnings`**，
            否则用户既看不见也选不了（键本身从 `pending` 摘掉——它已经不在了，留一个幽灵
            键会让散场去合并一条不存在的条目）。

        4. **本场改过的关系行退回开演时的样子**（§6.2 × §5.3）。关系小节是**第四条恒在
           上下文的回喂源**（每一块 think 与 speak 的提示词里都有它），比索引更该跟着撤回
           一起退：不退的话角色会带着"刚表白成功"的记忆接着开口，而用户已经明确把那一段
           抹掉了；散场选「保留」还会把这段被撤掉的关系永久并进本体。判据、原文来源与
           账本（含 §6.3 的**本场额度**）一并退回的口径见 `store.truncate_relations_after_turn`。
           计数落在 `relations_restored`（与条目那一半的 `restored` 分开报：一个是条目、
           一个是关系行，混在一个数里调用方就写不出能看懂的日志）。

        绝不抛：撤回不该因为一次 IO 失败中断，更不该炸掉整块 think。删除那半边的计数照旧
        返回（条目文件确实已经被截断了，撤回本身算数）；还原那半边**写不下去就不算数**
        （`restored` 报 0 并记一条警告）——报告"还原了 1 条"而磁盘上什么都没变，用户只会
        以为这条记忆已经回来了。

        已知边界：同一条在本场被连改多次时，还原只能回到**本体原文**——中间那几个版本的正文
        早已被原地覆盖（一个键一个文件），文件里不再有它们的痕迹可还原。跟随正文一起丢掉的
        还有**那个版本压过谁**：压制记录写在压制者自己身上（`supersedes`），它被顶掉时一并
        消失，于是它压过的那几条可能被放开成现行。这条边界只在"同一个键在本场被写了两次
        以上、且后面那次落在 cut 之后"时才咬人：每个键最多写一次时，撤回的结果与"按轮次
        重放一遍"逐字段相同（差分对拍 600 组随机操作，零不一致）。
        """
        copy_dir = self._copy_dir(character)
        cut_value = store._as_int(cut)
        if cut_value is None:
            return TruncateReport(
                warnings=[f"撤回的轮次「{_one_line(cut)}」读不出来，这次撤回什么都没做。"])
        cut = cut_value
        # 关系表那一半（§6.2）：本场晚于 cut 改过的关系行退回开演时的样子，§6.3 的本场额度
        # 一并退回。**先做它**：它不依赖下面条目那一套差分（撤回可能只动过关系、一条条目
        # 都没记），三个 return 点因此都要带上它的报告。绝不抛（撤回不该因 IO 失败中断）。
        try:
            relation_report = store.truncate_relations_after_turn(
                copy_dir, self._own_dir(character), cut)
        except Exception as exc:
            relation_report = store.RelationRollbackReport(
                warnings=[f"退回本场改过的关系时出了意外（{exc}）：那几行没有退回去。"])
        rel_restored = relation_report.restored
        rel_warnings = list(relation_report.warnings)
        # 判据与 store 的截断口径逐字对齐（kind + turn）。它那边改了，这里必须跟着改：
        # 两边不一致就会出现"以为自己还原了、其实没有"。
        before = store.load_library(copy_dir).library
        pending = store.load_pending(copy_dir)
        doomed = {key for key, entry in before.entries.items()
                  if entry.origin.kind == "scene" and int(entry.origin.turn) > cut}
        # §5.3.1：本场修订过、且轮次晚于 cut 的**基线**条目要回退。是不是基线由
        # `_is_scene_product` 判（它认得哨兵，同名场景跨 run 复用也认得出），不是只看 kind。
        edited = [key for key, turn in pending.revised_turns.items()
                  if turn > cut and key in before.entries
                  and not _is_scene_product(before.entries[key].origin, self.scene)]
        dropped = store.truncate_copy_after_turn(copy_dir, cut)
        if not doomed and not edited:
            return TruncateReport(dropped=dropped, relations_restored=rel_restored,
                                  warnings=rel_warnings)
        # 只认**真的没了**的键：删不掉的文件（只读、被占用）store 不算截掉，那种情况
        # 条目还在、标记也还成立，不该被"还原"。
        surviving = store.load_library(copy_dir).library
        gone = {key for key in doomed if key not in surviving.entries}
        rolled = [key for key in edited if key in surviving.entries]
        own = self._own_library(character)
        warnings: list[str] = list(rel_warnings)
        planned: dict[str, Entry] = {}       # 键 → 要写回副本的样子（同一个键只留最后一次）
        from_own: set[str] = set()           # 哪些键写的是"本体原文整条"（status 一起还原）
        for key in sorted(gone):
            # 被截掉的本场条目顶掉的基线条目，从本体放回来。
            original = own.entries.get(key) if own is not None else None
            if original is not None:
                planned[key] = store._as_baseline(original)
                from_own.add(key)
                continue
            # 没有原文可取，而这条键**开演前就有东西**（`pending` 记着本场修订过它）：本场
            # 那次重记被撤回之后，那条先于本场的记忆就整条没了。静默才是真正不可接受的——
            # 用户既看不见也选不了；报告里说一句，引擎才记得下这条日志。
            if key in pending.revised:
                warnings.append(
                    f"「{key}」本场同名同键重记过（把原来那条整个顶掉了），撤回后它的旧原文"
                    f"取不回来——本体库里找不到这一条，它已经不在副本里了。")
        rolled_done: list[str] = []
        for key in rolled:
            # 本场改过的基线条目，还原成本体原文（§5.3.1）。
            original = own.entries.get(key) if own is not None else None
            if original is None:
                warnings.append(f"「{key}」本场改过，但本体库里已经找不到这一条，没能还原；"
                                f"它仍留在散场清单上。")
                continue
            planned[key] = store._as_baseline(original)
            from_own.add(key)
            rolled_done.append(key)
        # 哪些键的"在场"被这次撤回撤掉了：删掉的 + **真的**还原成功的。回退失败的（本体里
        # 找不到原文）不算——它仍以本场改后的正文现行，仍压着它的对手（见 `_settle_press`）。
        void = set(gone) | set(rolled_done)
        for entry in list(surviving.entries.values()):
            # 被"没了 / 被回退"的那条压成过时的旧条目，回到它原来的样子。
            if entry.superseded_by not in void:
                continue
            if self._keeps_own_body(entry, pending, cut):
                planned[entry.key] = entry
                continue
            original = own.entries.get(entry.key) if own is not None else None
            if original is not None:
                planned[entry.key] = store._as_baseline(original)
                from_own.add(entry.key)
            else:
                planned[entry.key] = entry
        if not planned:
            store._drop_pending_keys(copy_dir, gone)
            return TruncateReport(dropped=dropped, relations_restored=rel_restored,
                                  warnings=warnings)
        # 压制关系重算：先按上面的还原结果摆出"副本写回之后的样子"，再逐条问"现在还有谁
        # 压着它"。光把 superseded_by 清成 None 是不够的——cut 之内可能**还有**一条更早的
        # 压制者，或者被回退的那条修订本该由别的键压着（见 `_settle_press`）。
        final = dict(surviving.entries)
        final.update(planned)
        order = list(surviving.ordered_keys())
        order += [key for key in planned if key not in surviving.entries]
        # "这条最后一次写入在第几轮"。写回本体原文的那些例外：本场对它的写入全被撤掉了，
        # 它在本场的最后写入就回落到开演前（哨兵轮次）——否则它本体里那份旧账会被当成
        # "比本体自己还早的陈年压制"而被筛掉（见 `_last_presser` 的第一道筛）。
        written_at = {key: (store.BASELINE_TURN if key in from_own
                            else _write_turn(key, entry, pending))
                      for key, entry in final.items()}
        for key in list(planned):
            planned[key] = _settle_press(planned[key], key, final, order, written_at, void,
                                         from_own=key in from_own)
        for entry in planned.values():
            surviving.upsert(entry)
        # 索引顺序也还原到截断前：还原的条目不该因为"被删过一次"就掉到队尾。
        surviving.order = [key for key in surviving.order if key in surviving.entries]
        surviving.order += [key for key in surviving.entries if key not in surviving.order]
        try:
            store.save_library(surviving, copy_dir)
        except Exception as exc:
            warnings.append(f"还原没能写回磁盘（{exc}），这次撤回只做了删除那一半。")
            return TruncateReport(dropped=dropped, relations_restored=rel_restored,
                                  warnings=warnings)
        # 真的还原回去的键不再是"本场改动"：留着它，散场结算仍会把它并进本体（§7.2）。
        # 没能还原的（本体里找不到原文）反过来要留着——它就是本场的一处改动，散场清单上
        # 得看得见，用户自己决定保留还是丢弃（静默把它藏起来才是最坏的选择）。
        store._drop_pending_keys(copy_dir, [*gone, *rolled_done])
        return TruncateReport(dropped=dropped, restored=len(planned),
                              relations_restored=rel_restored, warnings=warnings)

    def _keeps_own_body(self, entry: Entry, pending: store.PendingChanges, cut: int) -> bool:
        """这条条目的正文是不是"本场写下的、且落在 cut 之内"的那一份（§5.3 的保留口径）。

        它决定被压下去的条目能不能按本体原文整条换回来：**本场改过的东西不能换**。同键重记
        （一个键一个文件）会把基线条目的副本原地顶掉，revise 也是原地替换——这两条路写下的
        正文在本场之内、在 cut 之内，是"确实发生过的事"；把它们换回开演前的旧文，等于撤回
        回退过头，用户当场学到的说法静默消失（副本里再没有那段话，而 `pending` 还写着"本场
        改过这条"）。

        判据分两路，与 `_is_scene_product` / `pending.revised_turns` 的现有口径一致：

          · 本场产物（`kind == scene`、非哨兵）→ 它的 `origin.turn` 就是写下它的那一轮；
          · 基线条目 → `pending.revised_turns` 记着本场最后一次改它的轮次（没有记录 = 本场
            一字未动 = 不是本场写下的）。

        轮次读不出来（老 `pending.json` 没有这份记录）→ 不算"本场写下的"，与 §5.3.1 那条
        "轮次缺失时保守不回退"是同一个方向。
        """
        if _is_scene_product(entry.origin, self.scene):
            written: int | None = int(entry.origin.turn)
        else:
            written = pending.revised_turns.get(entry.key)
        return written is not None and written <= cut


# ------------------------------------------------------------------ 内部助手 --

def _coerce_args(args: Any) -> dict | None:
    """工具参数 → dict；读不出来 → None（调用方据此回一句"参数不对"）。

    OpenAI 的 `tool_calls.arguments` 是一个 JSON **字符串**，而后端/测试替身也可能直接
    给 dict；两种都认。给 None 当空参数（随后由各工具报"缺哪一项"），比在这里猜一个
    默认值更诚实。
    """
    if args is None:
        return {}
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            data = json.loads(args)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None
    return None


def _dedupe(keys: Sequence[str]) -> list[str]:
    """去重且保序（`supersedes` 是列表，链条里重复的键只会让文件变吵）。"""
    out: list[str] = []
    for key in keys:
        if key not in out:
            out.append(key)
    return out


def _write_turn(key: str, entry: Entry, pending: store.PendingChanges) -> int:
    """这条条目最后一次**写入**发生在第几轮（`_last_presser` 挑最晚压制者用的时钟）。

    两路，与库里"谁在本场写过"的既有判据一致：本场产物看 `origin.turn`；基线看
    `pending.revised_turns`——`_revise` 刻意不给基线条目重盖轮次（盖了就把"这条不是本场
    产物"这件事弄模糊了，哨兵一丢，store 的截断判据立刻成立），所以它的修订轮次只记在
    `pending` 里。两条都读不出来 → 哨兵轮次（= 最早）：从本体拷进来的那条在本场一个字
    都没动过，它 `supersedes` 里那些压制全是开演前的事，本来就该排在最前。
    """
    turn = store._as_int(entry.origin.turn)
    turn = store.BASELINE_TURN if turn is None else turn
    revised = pending.revised_turns.get(key)
    return turn if revised is None else max(turn, revised)


def _last_presser(key: str, final: Mapping[str, Entry], order: Sequence[str],
                  written_at: Mapping[str, int]) -> str:
    """副本里现在**还有谁压着**这条：返回最后压它的那个键，没有 → 空串。

    凭据只有压制者自己那份 `supersedes` 清单——`_remember` / `_revise` 总是把 `supersedes`
    与对方的 `superseded_by` 一起写下（`_press_others` 返回的键当场并进 `supersedes`），所以
    两处不会各说各话。用 `supersedes` 是因为它**按条目存**：撤回把某条删掉之后，它压过谁
    这件事随着它一起消失；而 `superseded_by` 留在受害者身上，只说明"前一次压制"是谁，
    答不了"除它以外还有谁压着"。

    两道筛，缺一不可：

      · **比这条自己的最后一次写入还早的压制，是陈旧记录**。同键重记与 revise 都把它翻回
        现行（`status="active"`、清 `superseded_by`），可别人 `supersedes` 里那条旧账没人
        擦——它记的是"我压过你"，不是"我现在还压着你"。写在同一轮的按活账算（`>=`）。
      · 胜出的是**写入轮次最晚**的那条（`written_at`，并列时取 `order` 里靠后的）。它不能
        换成"库顺序靠后的那条"：一个键一个文件，同键重记是**原位**覆盖（`Library.upsert`
        不挪顺序位），于是"位置靠后"与"写得晚"根本不是一回事，挑错就会把模型引向一条早已
        被推翻的旧说法。
    """
    mine = written_at.get(key, store.BASELINE_TURN)
    best_key = ""
    best_rank: tuple[int, int] | None = None
    for position, other in enumerate(order):
        if other == key:
            continue
        candidate = final.get(other)
        if candidate is None or key not in candidate.supersedes:
            continue
        turn = written_at.get(other, store.BASELINE_TURN)
        if turn < mine:                       # 早于它自己的最后一次写入 → 已被那次写入顶掉
            continue
        rank = (turn, position)
        if best_rank is None or rank > best_rank:
            best_rank, best_key = rank, other
    return best_key


def _settle_press(base: Entry, key: str, final: Mapping[str, Entry], order: Sequence[str],
                  written_at: Mapping[str, int], void: set[str], *, from_own: bool) -> Entry:
    """还原之后这条该由谁压着：把"已经没了的"压制算清，再认副本里还活着的压制者。

    `written_at` 是"每个键最后一次写入在第几轮"（`_write_turn`，挑最晚压制者用）。

    三种处境、三种答案（返回写回副本用的副本，不改入参）：

      1. **副本里还有别的条目压着它**（`_last_presser`）→ 标回过时并指向那一条。这是"清成
         active"不够用的那一半：cut 之内可能还有一条更早的压制者（本场第 3 轮记的 A 压过它、
         第 5 轮记的 B 也压过它，撤回第 5 轮后它仍该由 A 压着），或者被回退的那条修订本来
         就压着别人（第 5 轮的新说法压过它、第 7 轮又把它改回现行，撤回第 7 轮后它该回到
         "过时←第 5 轮那条"）。放成现行就是两条同标题的现行说法并列在索引里——模型照着
         被推翻的旧正文开口。
      2. **本体原文里本来就带着的过时标注**（`from_own`，且它指向的键不是这次被撤掉的）
         → 原样保留：那是本场之前的事实，一条早就被推翻的旧认识不该被"救活"（§3.5）。
      3. 其余 → 清成现行说法。它建立时本来就是现行的，本场那次压制才是"从没发生过的事"。
    """
    presser = _last_presser(key, final, order, written_at)
    if presser:
        return base.model_copy(update={"status": "outdated", "superseded_by": presser})
    if base.superseded_by and base.superseded_by in void:
        return base.model_copy(update={"status": "active", "superseded_by": None})
    if from_own:
        return base
    return base.model_copy(update={"status": "active", "superseded_by": None})


def _press_others(lib: store.Library, entry: Entry) -> list[str]:
    """把副本里与 `entry` 同副标题的**其它**条目标成过时，返回被压下去的键（保序）。

    判据用 `knowledge.same_subject`（§3.5 两级判定：同 key 或标题近似）。同 key 的跳过
    ——那是同一个文件、同一条目，不存在"两条"；它由调用方原地顶上（`upsert`）。
    只改状态与指向，**正文一字不动**：旧说法永远留着，只是不再是现行说法。
    """
    out: list[str] = []
    for key in lib.ordered_keys():
        if key == entry.key:
            continue
        other = lib.entries[key]
        if same_subject(entry, other):
            other.status = "outdated"
            other.superseded_by = entry.key
            out.append(key)
    return out
