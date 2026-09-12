"""信息库的**纯内核**（设计文档 §3 数据模型 / §4.2 条目格式 / §6.1 索引 / §6.2 反链）。

这里只做数据与变换：**零 IO、零 Qt、零网络、零引擎**。落盘布局、场景副本、订阅解析
与结算在 knowledgestore.py（M2/M3）；提示词注入点在 prompters.py（另一次改动）。
这样切开是为了让"条目长什么样、键能不能用、正文指向谁、索引渲染成什么"这些最容易
出错的判定可以在没有文件系统与没有模型的情况下被测穿。

三处硬约束，改动前先读：

1. **`.md` 是条目的唯一真相源**（§4.2）。front-matter 全量元信息 + 正文，手写一个文件
   丢进 `entries/` 就能被收录。因此解析器对坏输入**一律当场抛**（`EntryFormatError`），
   绝不返回半个对象——半个条目会带着空正文被当成真相源存回去，把用户手写的内容吃掉。
   渲染与解析必须严格往返一致（含键序稳定），否则每次保存都在无意义地改写用户文件。
   这条要求对**任意**值成立，不只是"干净"的值：标量一律按**流上下文**渲染、且绝不分行
   （见 `_scalar`），收尾线只认行首那一行（见 `parse_entry_md`）——多行摘要、值里的逗号
   或 `---` 都是模型写得出、手也改得出的输入。

2. **`key` 会成为文件名**（§4.3）。故 `Entry` 在**构造时**就过 `entry_key_error`——
   守门不能只在落盘那一刻做，否则非法的键会在内存里飘很久才炸，而且炸在写入路径上。

3. **索引表渲染"无条目则空串"**（§6.1）。无库角色的系统提示必须逐字节不变，所以
   `render_index` 在没有任何可显示条目时必须返回 `""`——不许出现标题行、不许出现换行。
   这一条有测试钉死，别顺手加"（无）"之类的占位。

键守门与 `loaders.scene_filename_error` / `gui/library.py::filename_error` 是同一套规则的
第三份。**字符表不重复声明**：非法字符集与保留设备名直接复用 loaders 里那两个常量，
只有"规则顺序 + 文案"这一层重写——因为键不是场景文件名（没有 `.json` 后缀要求、文案
说的是「键」而不是「文件名」、另外多一条长度上限）。§13.2 要求两份拷贝同步维护，
这里把最容易漂的字符表收敛成一处引用。
"""
from __future__ import annotations

import re
from typing import Literal, Sequence

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from . import textsim
#: 非法字符集与保留设备名是**同一份表**：键与场景文件名守的是同一批 Windows 陷阱。
#: 直接从 loaders 取私有常量，是为了让 §13.2 那条"两份拷贝要同步维护"的负担只剩下
#: 规则顺序，而不是连字符表都得手抄两遍（已存在的两份拷贝不在本次改动范围内）。
from .loaders import _ILLEGAL_NAME_CHARS, _RESERVED_STEMS

#: 键的长度上限（字符数）。键会成为**文件名**，Windows 全路径上限 260，条目目录
#: （`app/libraries/characters/<角色名>/entries/`）已经吃掉一截，故留到 80 就够慷慨。
MAX_KEY_LENGTH = 80

#: 条目文件的 front-matter 分隔线（与设计文档 §4.2 一致）。
_FENCE = "---"

#: 冲突判定的第二级阈值（§3.5）：标题 normalize 后的 textsim 相似度 ≥ 此值即判同一件事。
#:
#: 为什么是 0.85 而不是 speak 那条 0.95：两者问的不是同一个问题。`_DUP_RATIO` 拦的是
#: "模型在逐字复读自己"，必须几乎一字不差；这里问的是"两套说法是不是同一件事"，
#: 得容下「陈掌柜」与「陈掌柜的」这类写法差异（ratio = 2×3/(3+4) ≈ 0.857）。
#: 也不能再低：0.75 那一档会把「地理·西线」与「地理·东线」判成一件事，那是两处地方。
SUBJECT_SIMILARITY_THRESHOLD = 0.85

#: 索引小节的标题与尾注（§6.1 给定，逐字节对齐）。
INDEX_HEADER = "【你的信息索引】"
INDEX_HINT = "（你知道的远不止这些。索引只列入口，正文里常有指向其他条目的线索。）"

#: 索引行的样子：组头 `▾ 组名`、订阅组头 `▾ 借自《库名》`、条目行 `   标题 — 摘要`。
_SUBJECT_HEADER = "▾ {subject}"
_SUBSCRIBED_HEADER = "▾ 借自《{name}》"
_ENTRY_INDENT = "   "
_ENTRY_SEP = " — "

_LINK_OPEN = "[["
_LINK_CLOSE = "]]"


class EntryFormatError(Exception):
    """条目的 Markdown 不合法（缺 front-matter / YAML 语法错 / 缺 key / 字段非法）。

    消息是**中文、可直接给用户看**的（与 `template_import.TemplateError` 同风格）：
    条目文件是手写的，"报错只说 English 的 pydantic 原文"对手写的人没有帮助。
    """


class EntryOrigin(BaseModel):
    """条目从哪来（§3.1）：`kind` 四选一，`turn` 是写入时的转录轮次。

    `turn` 是撤回截断的依据（§5.3），故默认 0 而不是 None——与 `memory.py:54` 对缺轮次
    记 0 的口径一致。但**别拿 0 当"没有轮次"的哨兵**：§5.3 已经点明 0 永远 ≤ cut，
    靠数值兜底等于静默永不截断，所以副本初始化时就得把基线条目与本场条目标开。
    `kind=wide` 是订阅来的（广域库），它在副本里**不参与本场截断**。
    """
    kind: Literal["scene", "manual", "import", "wide"] = "manual"
    scene: str = ""
    turn: int = 0
    at: str = ""


class Entry(BaseModel):
    """条目：库的最小单位（§3.1）。键、标题、一行摘要、正文，正文里可含 `[[键]]`。

    `extra="forbid"`：不认识的字段名（多半是手写笔误）当场报错。放行等于下次保存时
    把用户手写的那一行抹掉——条目文件是元信息的唯一真相源，静默丢字段就是数据损坏。
    """
    model_config = ConfigDict(extra="forbid")

    key: str
    title: str = ""
    summary: str = ""
    body: str = ""
    subject: str = ""
    indexed: bool = True
    status: Literal["active", "outdated"] = "active"
    superseded_by: str | None = None
    supersedes: list[str] = Field(default_factory=list)
    origin: EntryOrigin = Field(default_factory=EntryOrigin)
    updated_at: str = ""

    @field_validator("key")
    @classmethod
    def _guard_key(cls, value: str) -> str:
        """键在构造时就过文件名守门——见模块 docstring 第 2 条。"""
        err = entry_key_error(value)
        if err:
            raise ValueError(err)
        return value

    @field_validator("body")
    @classmethod
    def _normalize_body(cls, value: str) -> str:
        """正文首尾空行归一（连同 CRLF）：让 `parse(render(e)) == e` 对任意条目都成立。

        空行在正文首尾没有信息量，但在文件里会随"谁渲染的"而变——不归一的话，
        "往返一致"就只对恰好没写空行的条目成立，那种测试等于没测。
        """
        return (value or "").replace("\r\n", "\n").replace("\r", "\n").strip("\n")


# ------------------------------------------------------------------ 键的文件名守门 --

def entry_key_error(key: str) -> str:
    """键能否安全用作条目文件名——空串 = 可以，否则中文原因（可直接给用户看）。

    规则与 `loaders.scene_filename_error` 同源（拒绝空名、首尾空白、前导 `.`、`..`、
    路径分隔符、Windows 非法字符、控制字符、尾随 `.`、系统保留设备名），只是键**不要求
    `.json` 后缀**（条目文件名是 `<键>.md`），并**多一条长度上限**。

    绝不静默改名：键是冲突判定的第一依据，偷偷替换字符会让"同一件事"变成两件事。
    """
    name = key or ""
    if not name.strip():
        return "键不能为空。"
    if name != name.strip():
        return "键首尾不能有空白。"
    if name.startswith("."):
        return "键不能以「.」开头（会变成隐藏文件）。"
    if ".." in name:
        return "键里不能包含「..」。"
    if "/" in name or "\\" in name:
        return "键里不能包含路径分隔符（/ 或 \\）。"
    bad = sorted({c for c in name if c in _ILLEGAL_NAME_CHARS})
    if bad:
        return "键里不能包含这些字符：" + " ".join(bad)
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        return "键里不能包含控制字符。"
    if name.endswith("."):
        return "键不能以「.」结尾。"
    stem = name.split(".")[0]
    if stem.upper() in _RESERVED_STEMS:
        return f"键不能是系统保留名（{stem}）。"
    if len(name) > MAX_KEY_LENGTH:
        return f"键太长（最多 {MAX_KEY_LENGTH} 个字符）。"
    return ""


def entry_key_collision_error(key: str, other: str) -> str:
    """`key` 与 `other` 会不会落到**同一个条目文件**上——空串 = 不会，否则中文原因。

    `entry_key_error` 只看一个键自己，看不出这类冲突：`ABC` 与 `abc` 各自都合法，却是
    同一个文件名（NTFS 与默认的 macOS 卷大小写不敏感），后写的一条静默顶掉前一条——
    用户只会发现"我记的东西少了一条"，无从排查。所以要有一个能比较**两个键**的判据，
    让内存索引（`Library.upsert`）在"同一件事"成立的那一刻就拦住。

    这一条在**所有平台**上生效：素材是要走 git 同步的，在 Linux 上放行等于把雷埋到
    Windows 上才炸——与 `_ILLEGAL_NAME_CHARS` 那套 Windows 规则同一口径。

    完全相同的键返回空串：那是"同一条目被覆盖"，是 upsert 的本意，不是碰撞。
    """
    if not key or not other or key == other:
        return ""
    if key.casefold() != other.casefold():
        return ""
    return (f"键「{key}」与已存在的「{other}」只有大小写不同，在 Windows 上会落到同一个"
            f"条目文件，其中一条会被静默覆盖。")


# ------------------------------------------------------------------ 序列化 / 解析 --

def _scalar(value: str) -> str:
    """字符串 → YAML 标量文本（必要时加引号），保证 `safe_load` 读回来仍是同一个 `str`。

    两道都必须过，少一道就有值写得出、读不回：

    1. **按流上下文分析**。本模块把标量拼进 `origin: {..., scene: <值>, ...}` 与
       `supersedes: [<值>, ...]` 这类**流**上下文（§4.2 的样例就是这么写的；改成块映射
       会让样例与用户手写的文件都变样）。而 `safe_dump` 默认按**块**上下文分析一个标量，
       于是 `乙, 丙` 这种在块里完全合法的值不会被加引号，拼进流里就被逗号切成两半——
       `supersedes` 一个键裂成两个，`origin.scene` 被截断，两处都是静默错值。这里一律
       用流映射壳 `{v: ...}` 让 safe_dump 在**流上下文里**做分析，再把壳剥掉。
    2. **绝不跨行**。含换行的值 safe_dump 会摊成多行引号标量、续行缩进两格，而解析侧的
       收尾线扫描是逐行看 `---`/`...`——值里恰好有那样一行就会被误判成 front-matter 的
       结尾（条目从此读不回来）。故含换行时改用双引号风格：YAML 把换行写成 `\\n` 转义，
       渲染结果永远是一行。解析侧另有一道列位保险，见 `parse_entry_md`。

    `2026-09-11T21:30:00` 不加引号会被读成 datetime、`yes` / `42` 会被改型成 bool / int，
    第二次保存时字段校验直接炸——往返一致靠的就是这里。
    """
    text = "" if value is None else str(value)
    if "\n" in text or "\r" in text:
        return yaml.safe_dump(text, allow_unicode=True, default_style='"',
                              width=10 ** 9).rstrip("\n")
    shell = yaml.safe_dump({"v": text}, allow_unicode=True, default_flow_style=True,
                           width=10 ** 9).rstrip("\n")
    if shell.startswith("{v: ") and shell.endswith("}"):
        return shell[len("{v: "):-1]
    return shell                            # 理论上到不了；真到了也别把壳漏进文件


def _origin_inline(origin: EntryOrigin) -> str:
    """origin 写成一行内联映射（§4.2 的样子）；`at` 为空时省略，免得每行都挂个空值。

    值一律过 `_scalar`：这是流上下文，裸值里的 `,` `?` `[` `{` 会被 YAML 当成语法。
    """
    parts = [f"kind: {origin.kind}", f"scene: {_scalar(origin.scene)}",
             f"turn: {int(origin.turn)}"]
    if origin.at:
        parts.append(f"at: {_scalar(origin.at)}")
    return "{" + ", ".join(parts) + "}"


def render_entry_md(entry: Entry) -> str:
    """条目 → Markdown 文本（front-matter + 空行 + 正文），末尾恰好一个换行。

    键序**写死**（key/title/summary/subject/indexed/status/origin，其后才是可选的
    superseded_by/supersedes/updated_at）：git diff 才读得懂，也让"只有正文变了"这种
    改动在 diff 里就是那么几行。可选项为空时**不写**——空值只会让 diff 变吵。
    正文为空时不留那个空行。
    """
    lines = [
        _FENCE,
        f"key: {_scalar(entry.key)}",
        f"title: {_scalar(entry.title)}",
        f"summary: {_scalar(entry.summary)}",
        f"subject: {_scalar(entry.subject)}",
        f"indexed: {'true' if entry.indexed else 'false'}",
        f"status: {entry.status}",
        f"origin: {_origin_inline(entry.origin)}",
    ]
    if entry.superseded_by is not None:
        lines.append(f"superseded_by: {_scalar(entry.superseded_by)}")
    if entry.supersedes:
        lines.append("supersedes: ["
                     + ", ".join(_scalar(k) for k in entry.supersedes) + "]")
    if entry.updated_at:
        lines.append(f"updated_at: {_scalar(entry.updated_at)}")
    lines.append(_FENCE)
    text = "\n".join(lines) + "\n"
    body = (entry.body or "").strip("\n")
    if body:
        text += "\n" + body + "\n"
    return text


def _format_validation(exc: ValidationError) -> str:
    """pydantic 的错误 → 一行中文；不认识的字段单独点名（手写笔误最常见的就是它）。"""
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ())) or "(根)"
        if err.get("type") == "extra_forbidden":
            parts.append(f"不认识的字段「{loc}」")
        else:
            parts.append(f"{loc}: {err.get('msg', '')}")
    return "；".join(parts)


def parse_entry_md(text: str) -> Entry:
    """Markdown 文本 → Entry；任何不合规都抛 `EntryFormatError`（绝不返回半个对象）。

    BOM 与 CRLF 先归一——手写、Windows 编辑器、git autocrlf 都会带进来，那不是坏输入。
    收尾分隔线接受 `---` 与 YAML 的 `...`（都是合法写法），但必须落在**行首**：缩进的
    `---` 是引号标量 / 块标量的续行，不是收尾线。收尾线若按 `strip()` 比对，手写的多行
    摘要里那句 `---` 就会把 front-matter 从半路截断（引号不闭合，整条读不回来）——YAML
    里续行一律有缩进，所以"只看行首"这一条正好把它们排除干净。
    正文取收尾线之后的全部内容，首尾空行由 `Entry._normalize_body` 与渲染侧对称地归一。
    """
    raw = (text or "").lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    lines = raw.split("\n")
    if not lines or lines[0].strip() != _FENCE:
        raise EntryFormatError("条目的 front-matter 必须以单独一行 --- 开始。")
    end = next((i for i in range(1, len(lines))
                if lines[i].rstrip() in (_FENCE, "...")), None)
    if end is None:
        raise EntryFormatError("条目的 front-matter 没有收尾的 ---。")
    try:
        data = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError as exc:
        raise EntryFormatError(f"条目的 front-matter 不是合法 YAML：{exc}") from exc
    if data is None:
        raise EntryFormatError("条目的 front-matter 是空的，至少要有 key。")
    if not isinstance(data, dict):
        raise EntryFormatError("条目的 front-matter 必须是一组「键: 值」（YAML 映射）。")
    if not str(data.get("key") or "").strip():
        raise EntryFormatError("条目的 front-matter 缺少 key。")
    data["body"] = "\n".join(lines[end + 1:])
    try:
        return Entry.model_validate(data)
    except ValidationError as exc:
        raise EntryFormatError(f"条目的字段不合法：{_format_validation(exc)}") from exc


# ------------------------------------------------------------------ 链接与反链 --

def parse_links(body: str) -> list[str]:
    """正文里全部 `[[出链]]`，去重且**保序**（先出现的先返回）。

    逐行扫描，规则写死（这几条都踩过）：
      · 目标两侧空白剥掉；键**中间**的空格是键的一部分，不动；
      · 空链接（`[[]]`、`[[   ]]`）不产出——空串查不到任何东西，只会污染下游；
      · **不跨行**：一行里没等到 `]]` 就作罢。否则一处手滑的 `[[` 会吞掉后半篇正文；
      · 目标里仍残留方括号的（`[[外[[内]]外]]`）不算链接——这套语法不支持嵌套。
    """
    out: list[str] = []
    seen: set[str] = set()
    for line in (body or "").split("\n"):
        pos = 0
        while True:
            start = line.find(_LINK_OPEN, pos)
            if start < 0:
                break
            end = line.find(_LINK_CLOSE, start + len(_LINK_OPEN))
            if end < 0:
                break                      # 未闭合：本行剩下的都不算链接
            pos = end + len(_LINK_CLOSE)
            target = line[start + len(_LINK_OPEN):end].strip()
            if not target or "[" in target or "]" in target or target in seen:
                continue
            seen.add(target)
            out.append(target)
    return out


def backlinks(entries: Sequence[Entry], key: str) -> list[str]:
    """指向 `key` 的条目有哪些（§6.2：read_entry 的返回里要附这一行）。

    按入参顺序返回、同键只出现一次；**自链不算自己的反链**（列出来是纯噪声）。
    只认键的精确匹配：正文里的 `[[X]]` 指向的就是键为 X 的那一条，不做标题模糊
    ——模糊是 read_entry 的参数匹配，不是图的边。空键 → 空表。
    过时条目不在这里过滤：它们仍在库里、仍是图上的边，由调用方决定要不要显示。
    """
    target = (key or "").strip()
    if not target:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if entry.key == target or entry.key in seen:
            continue
        if target in parse_links(entry.body):
            seen.add(entry.key)
            out.append(entry.key)
    return out


def same_subject(a: Entry, b: Entry) -> bool:
    """两条条目算不算"同一件事"（§3.5 两级判定）：同 key，或标题近似。

    两级之外不自作聪明：措辞不同、key 也不同的两条近似记忆**允许共存**——人本来就可能
    对同一件事留着两套说法。标题 normalize 后为空（纯括号/纯标点）时 ratio 返回 0.0，
    于是"空对空"永不判同一件事，与 `textsim` 的口径一致。
    """
    if a.key and a.key == b.key:
        return True
    return textsim.ratio(a.title, b.title) >= SUBJECT_SIMILARITY_THRESHOLD


# ---------------------------------------------------------------- 索引表渲染 --

def _visible(entries: Sequence[Entry], include_outdated: bool) -> list[Entry]:
    """索引只列 `indexed=true` 的条目（§3.3）；过时条目默认不列（见 render_index）。"""
    return [e for e in entries
            if e.indexed and (include_outdated or e.status == "active")]


def _groups(entries: Sequence[Entry]) -> list[tuple[str, list[Entry]]]:
    """按 subject 归拢成 (组头, 条目) 列表。组头为空串 = 不打组头。

    无 subject 的一桶**排在最前且不打组头**：remember 工具本就不传 subject（§6.2），
    这一桶才是常态，硬安一个「其他」组头只会让索引平白多一行噪声。命名组按**首次出现
    顺序**排（不排序：顺序由调用方给的条目顺序决定，与 library.json 的 order 一致）。
    """
    ungrouped: list[Entry] = []
    named: list[tuple[str, list[Entry]]] = []
    at: dict[str, int] = {}
    for entry in entries:
        subject = (entry.subject or "").strip()
        if not subject:
            ungrouped.append(entry)
            continue
        if subject not in at:
            at[subject] = len(named)
            named.append((_SUBJECT_HEADER.format(subject=subject), []))
        named[at[subject]][1].append(entry)
    if ungrouped:
        named.insert(0, ("", ungrouped))
    return named


def _one_line(text: str) -> str:
    """压成一行：索引是一行一条，标题/摘要里混进的换行会毁掉整节格式。"""
    return re.sub(r"\s+", " ", text or "").strip()


def _entry_line(entry: Entry) -> str:
    """条目行 `标题 — 摘要`；标题为空回落键，摘要为空就只留标题（不留悬空的分隔符）。"""
    title = _one_line(entry.title) or _one_line(entry.key)
    summary = _one_line(entry.summary)
    return f"{title}{_ENTRY_SEP}{summary}" if summary else title


def render_index(
    entries: Sequence[Entry],
    *,
    subscribed: Sequence[tuple[str, Sequence[Entry]]] = (),
    max_entries: int | None = None,
    include_outdated: bool = False,
) -> str:
    """把一座库渲染成提示词里的索引小节（§6.1）；**没有任何可显示条目 → 空串**。

    `entries` 是自有条目，调用方负责按 `library.json` 的 order 排好序（本函数保序）。
    `subscribed` 是订阅来的库（库名 + 该库的条目），每个库单独一组并标出来源——
    活引用与固化都发生在存储层，这里只负责渲染拿到的东西。过时条目默认不列：索引是
    "我现在知道什么"的入口，把旧说法一并列上会让角色读到自相矛盾的两条（旧条仍在库里、
    仍能被 read_entry 摸到，只是不再当入口）。

    `max_entries` 是注入量上限的参数位（§10.2）：给了就按显示顺序保留前 N 条、其余丢弃，
    整组被截空时不留空组头。**丢弃的计数由调用方（存储层）记日志**——本函数是纯渲染，
    不写盘、不打日志、也不知道自己丢掉了几条。

    空串那条是硬约束：无库角色的系统提示必须逐字节不变，所以这里不许有标题行、
    不许有占位文本、不许有换行。返回的文本不含结尾换行，拼进系统消息由调用方决定。
    """
    sections: list[tuple[str, list[Entry]]] = _groups(_visible(entries, include_outdated))
    for name, rows in subscribed or ():
        visible = _visible(rows, include_outdated)
        if visible:
            sections.append((_SUBSCRIBED_HEADER.format(name=name), visible))
    if max_entries is not None:
        budget = max(0, int(max_entries))
        kept: list[tuple[str, list[Entry]]] = []
        for label, rows in sections:
            if budget <= 0:
                break
            take = rows[:budget]
            budget -= len(take)
            kept.append((label, take))
        sections = kept
    if not sections:
        return ""
    out = [INDEX_HEADER]
    for label, rows in sections:
        if label:
            out.append(label)
        out.extend(_ENTRY_INDENT + _entry_line(e) for e in rows)
    out.append("")
    out.append(INDEX_HINT)
    return "\n".join(out)
