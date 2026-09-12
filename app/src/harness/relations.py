"""人际关系表的**纯内核**（《人际关系与场景推进》§6.1 一条关系 / §6.2 恒在上下文 / §6.5 与信息库的接口）。

一行关系就是"我对这个人知道的几件事"：姓名（键）、性别、亲密度（−100 ~ 100，可负 =
仇视）、关系描述、相处模式、何时因何而变，以及**指向该人物信息库条目的键**（§6.5）。
本模块只做数据与字符串：**零 IO、零 Qt、零网络、零引擎**——落盘、场景副本与散场结算在
knowledgestore.py（关系表是信息库里的一个**特殊分区**，复用那一套副本/结算，不另造一套），
工具在 knowledgetools.py，提示词注入在 prompters.py。

三处硬约束，改动前先读：

1. **亲密度是硬的 −100 ~ 100**（§6.1）。越界 / 非数字在**构造时当场报错**（`Relation` 的
   校验器 + `RelationFormatError`），绝不静默夹取——夹取会让"模型想要 −200"与"文件里写着
   −100"在外观上完全一样（返回文本、日志、界面都在说"改好了"），用户再也查不出数据被谁
   动过。夹取只留一处：`RelationTable.apply` 把一次**合法幅度**的增量算到边界（95 + 20 →
   100），那里方向没有歧义，且改后的行说得清"到边界了"。

2. **往返逐字节一致**（与 `knowledge.render_entry_md` 同一条纪律）。这张表是**手写素材**：
   UTF-8、不转义中文、缩进 2、人可读可 diff。渲染的字段序**写死**、空值**不写**（空值只
   会让 diff 变吵），故 `parse(render(t)) == t` 且 `render(parse(text)) == text` 对任意合法
   的表都成立——否则每次保存都在无意义地改写用户手写的文件。

3. **空表渲染成空串**（§6.2 的恒在上下文）：`render_relations` 在没有任何关系行时必须返回
   `""`——无关系角色的系统提示逐字节不变，不许出现标题行、不许出现空行。这一条有测试钉死
   （对拍 HEAD 的哈希），别顺手加"（暂无）"之类的占位。

键从哪来（§6.5）：**约定 = 关系名就是那个人的信息库条目键**（"甲"这个人，条目也叫甲）；
条目名与称呼对不上时用 `link` 显式指过去（`Relation.entry_key` 是那两句话的唯一实现）。
两种写法都只为一件事：渲染时把那个键附在行里，让角色顺着链读下去（`read_entry`）。
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterator

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .knowledge import EntryOrigin

#: 亲密度的取值范围（§6.1 写死：−100 ~ 100，可负 = 仇视）。
CLOSENESS_MIN = -100
CLOSENESS_MAX = 100

#: `closeness_delta` 单次幅度上限（§6.3 第三重约束，默认 ±20）：防"一次跳满"。
#: 闸门本身在工具层（`knowledgetools._update_relation`）——这里只是**那个数的唯一定义源**。
MAX_CLOSENESS_DELTA = 20

#: 关系表文件的 schema 版本（与 `library.json` 同一套习惯），落进文件便于将来迁移。
SCHEMA_VERSION = 1

#: 恒在上下文小节的标题与尾注（§6.2）。这两个串与 `knowledge.INDEX_HEADER` 一样是**契约**：
#: 渲染的唯一落点在本模块，prompters 只做搬运（同一句话两处定义必然漂移）。
RELATION_HEADER = "【你的人际关系】"
RELATION_HINT = ("（这些人你每次开口前都该记得——先想清对面站的是谁。想细看谁，"
                 "用 read_entry 读「条目：」后面那个名字。）")

#: 一行的样子：`- 姓名（性别，亲密度 N）｜关系：…｜相处：…｜条目：键`。
_RELATION_ROW_PREFIX = "- "
_RELATION_FIELD_SEP = "｜"
_RELATION_META_SEP = "，"
_CLOSENESS_LABEL = "亲密度"
_DESCRIPTION_LABEL = "关系"
_MODE_LABEL = "相处"
_KEY_LABEL = "条目"


class RelationFormatError(Exception):
    """关系表的 JSON 不合法（坏 JSON / 结构不对 / 某一行字段非法）。

    消息是**中文、可直接给用户看**的（与 `knowledge.EntryFormatError` 同风格）：这张表是
    手写的，"报错只说 English 的 pydantic 原文"对手写的人没有帮助。
    """


def _one_line(text: Any) -> str:
    """压成一行：这一节是一行一条，描述/模式里混进的换行会毁掉整节格式。"""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _closeness_error(value: Any) -> str:
    """亲密度读不出来时的一句中文（空串 = 合法）。

    认整数、整数值的浮点（`20.0` 是 JSON 里常见的写法）与十进制整数字符串；**不认 bool**
    （"真/假"显然不是亲密度——`True` 是 `int` 的子类，放行会静默变成 1）。
    """
    if isinstance(value, bool):
        return f"亲密度得是一个整数（收到的是「{value}」）。"
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and float(value).is_integer():
        number = int(value)
    elif isinstance(value, str):
        try:
            number = int(value.strip())
        except ValueError:
            return f"亲密度得是一个整数（收到的是「{value}」）。"
    else:
        return f"亲密度得是一个整数（收到的是「{value}」）。"
    if not CLOSENESS_MIN <= number <= CLOSENESS_MAX:
        return (f"亲密度得在 {CLOSENESS_MIN} ~ {CLOSENESS_MAX} 之间"
                f"（收到的是 {number}）。")
    return ""


def _clamp(value: int) -> int:
    """夹到 ±100（只用于"一次合法幅度算到了头"，见 `RelationTable.apply`）。"""
    return max(CLOSENESS_MIN, min(CLOSENESS_MAX, int(value)))


class Relation(BaseModel):
    """一条关系（§6.1）：`name` 是键，其余字段都可以空着——空着只是"没写"，不是错。

    `extra="forbid"`：不认识的字段名（多半是手写笔误）当场报错。放行等于下次保存时把用户
    手写的那一行抹掉——文件是这份数据的唯一真相源，静默丢字段就是数据损坏（同 `Entry`）。
    """
    model_config = ConfigDict(extra="forbid")

    name: str
    gender: str = ""
    closeness: int = 0
    description: str = ""
    mode: str = ""
    #: 指向该人物信息库条目的键（§6.5）。空 = 用**约定**：关系名本身就是那个键。
    link: str = ""
    updated_at: str = ""
    origin: EntryOrigin = Field(default_factory=EntryOrigin)

    @field_validator("name")
    @classmethod
    def _guard_name(cls, value: str) -> str:
        """姓名是**键**（一个人只能有一行），空名会让整张表查不动、也没法渲染。

        首尾空白同样拒绝（不静默 strip）：键是用来对上的，"甲"与"甲 "看起来一样却是
        两行，用户只会发现"我改的那条没生效"。
        """
        text = value if isinstance(value, str) else ""
        if not text.strip():
            raise ValueError("关系表里每一行都要有个姓名（对方的称呼）。")
        if text != text.strip():
            raise ValueError("姓名首尾不能有空白。")
        return text

    @field_validator("closeness", mode="before")
    @classmethod
    def _guard_closeness(cls, value: Any) -> int:
        """亲密度在构造时就过边界守门——见模块 docstring 第 1 条（绝不静默夹取）。"""
        err = _closeness_error(value)
        if err:
            raise ValueError(err)
        return int(value)

    @property
    def entry_key(self) -> str:
        """这一行指向的信息库条目键（§6.5）：给了 `link` 以它为准，否则**约定 = 姓名**。

        空白 link 不算给了（手滑多打一个空格不该让链断掉）。
        """
        return (self.link or "").strip() or self.name


class RelationTable(BaseModel):
    """一族关系的集合（§6.1）：**按键索引、保序**（表内顺序就是渲染顺序，也是 diff 的顺序）。

    `relations` 是一个**列表**而不是映射：文件是给人看给人改的，列表的 diff 干净（改一行
    就是改一行），而 JSON 对象的键序在手工编辑时最容易被重排。索引（查/增/改）在内存里按
    名字扫——一张表就是几个人到几十个人，代价可忽略。

    `extra="forbid"` 与 `Relation` 同一理由：顶层写错一个键（`relation` 单数之类）当场报错，
    别让整张表静默读成空的。
    """
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    #: 落盘键名是 `schema`（与 `library.json` 同一习惯），字段名取 `schema_version` 以免
    #: 遮蔽 BaseModel 的同名属性；写盘用 `render_relations_json` 自己拼（不依赖别名机制）。
    schema_version: int = Field(default=SCHEMA_VERSION, alias="schema")
    relations: list[Relation] = Field(default_factory=list)

    def __iter__(self) -> Iterator[Relation]:      # type: ignore[override]
        """按表内顺序逐行（`for row in table`）——顺序就是渲染顺序。"""
        return iter(self.relations)

    def __len__(self) -> int:
        return len(self.relations)

    def __contains__(self, name: object) -> bool:
        return self.get(str(name)) is not None

    def get(self, name: str) -> Relation | None:
        """按姓名取一行；没有 → None。"""
        wanted = (name or "").strip()
        for row in self.relations:
            if row.name == wanted:
                return row
        return None

    def names(self) -> list[str]:
        """全部姓名，按表内顺序（渲染、报错文案与"表里有谁"都用它）。"""
        return [row.name for row in self.relations]

    def upsert(self, relation: Relation) -> Relation:
        """放入/覆盖一行：同名**原位替换**（顺序位不挪），新名字追到末尾。

        与 `knowledgestore.Library.upsert` 同一条：同名就是同一个人，覆盖是它的本意
        （不存在"两条同名"——表里名字就是键）。
        """
        for index, row in enumerate(self.relations):
            if row.name == relation.name:
                self.relations[index] = relation
                return relation
        self.relations.append(relation)
        return relation

    def remove(self, name: str) -> bool:
        """摘掉一行（按姓名）；本来就没有 → False。"""
        wanted = (name or "").strip()
        for index, row in enumerate(self.relations):
            if row.name == wanted:
                del self.relations[index]
                return True
        return False

    def apply(self, name: str, *, description: Any = None, mode: Any = None,
              closeness_delta: Any = None, origin: EntryOrigin | None = None,
              updated_at: str = "") -> Relation:
        """改一条**已经在表里**的关系，返回**改后的新行**（不发回原行、不写盘、不加进表）。

        **姓名不在表里 → `KeyError`**：这张表记的是你**已经有的**关系，绝不"顺手新建一行"
        ——谁能进这张表是用户/编辑器的事，模型能改的只是那几行描述与亲密度。调用方
        （工具层）负责把 KeyError 翻成给模型看的一句话。

        `description` / `mode` 给了才改（`None` = 不动这一项，与 `revise` 的口径一致）。
        `closeness_delta` 是**增量**而不是新值：单次幅度上限由调用方把关（§6.3 的引擎侧
        闸门，`MAX_CLOSENESS_DELTA`），这里只管把结果**夹到 ±100**——见模块 docstring 第 1 条。

        `origin` / `updated_at`（"何时、因何而变"，§6.1）由调用方给：本层**不猜时间**
        （同 `knowledgestore.save_library` 的口径），同一份输入因此渲染出同一份数据。
        返回的行**重新过一遍校验**（`model_copy` 不跑校验，越界的亲密度会静默漏过去）。
        """
        current = self.get(name)
        if current is None:
            raise KeyError(name)
        payload = current.model_dump()
        if description is not None:
            payload["description"] = str(description)
        if mode is not None:
            payload["mode"] = str(mode)
        if closeness_delta is not None:
            payload["closeness"] = _clamp(current.closeness + int(closeness_delta))
        if origin is not None:
            payload["origin"] = origin
        if updated_at:
            payload["updated_at"] = str(updated_at)
        return Relation.model_validate(payload)


# ------------------------------------------------------------------ 序列化 --

def _origin_dict(origin: EntryOrigin) -> dict[str, Any]:
    """`origin` 落进 JSON 的形状：`kind` 恒写，其余**空值不写**（与 `_origin_inline` 同款）。

    `{"kind": "manual", "scene": "", "turn": 0, "at": ""}` 挂在每一行上只是噪声——diff 里
    真正变了的那一处会被它淹掉。缺的字段读回来就是默认值，往返不受影响。
    """
    out: dict[str, Any] = {"kind": origin.kind}
    if origin.scene:
        out["scene"] = origin.scene
    if origin.turn:
        out["turn"] = int(origin.turn)
    if origin.at:
        out["at"] = origin.at
    return out


def render_relations_json(table: RelationTable) -> str:
    """关系表 → JSON 文本（UTF-8、不转义中文、缩进 2、末尾恰好一个换行）。

    字段序**写死**（name/gender/closeness/description/mode/link/updated_at/origin）：
    git diff 才读得懂，也让"只改了亲密度"这种改动在 diff 里就是那么一行。
    """
    payload = {
        "schema": int(table.schema_version),
        "relations": [
            {
                "name": row.name,
                "gender": row.gender,
                "closeness": int(row.closeness),
                "description": row.description,
                "mode": row.mode,
                "link": row.link,
                "updated_at": row.updated_at,
                "origin": _origin_dict(row.origin),
            }
            for row in table
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _format_validation(exc: ValidationError, rows: list) -> str:
    """pydantic 的错误 → 一行中文，**点名是第几行的谁**（手写的表最容易错在这里）。

    `rows` 是原始 JSON 里那一栏（拿来把下标翻译成人名）：只说"relations[2].closeness 不合法"
    对手写的人没有帮助，说"「甲」这一行的亲密度越界"他才知道去改哪儿。
    """
    parts: list[str] = []
    for err in exc.errors():
        loc = list(err.get("loc", ()))
        where = ""
        if len(loc) >= 2 and loc[0] == "relations" and isinstance(loc[1], int):
            raw = rows[loc[1]] if 0 <= loc[1] < len(rows) else None
            name = raw.get("name") if isinstance(raw, dict) else None
            where = f"「{name}」这一行：" if name else f"relations[{loc[1]}] 这一行："
            loc = loc[2:]
        field = ".".join(str(part) for part in loc) or "(整行)"
        message = str(err.get("msg", "")).replace("Value error, ", "")
        if err.get("type") == "extra_forbidden":
            parts.append(f"{where}不认识的字段「{field}」")
        else:
            parts.append(f"{where}{field} {message}")
    return "；".join(parts)


def _duplicate_names(rows: list) -> list[str]:
    """原始 JSON 里出现两次以上的姓名（保序、只报一次）；读不动的行不算。

    在 pydantic 校验**之前**扫原始条目：`RelationTable.relations` 是个列表，重复的姓名在
    模型层是合法输入（只有 `upsert` 才去重），所以这个不变量只能在这里守。行不是对象、
    或 name 读不出来时留给 `_format_validation` 去报（那一条比"重名"更贴近用户的笔误）。
    """
    seen: list[str] = []
    out: list[str] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            continue
        if name in seen:
            if name not in out:
                out.append(name)
            continue
        seen.append(name)
    return out


def parse_relations_json(text: str) -> RelationTable:
    """JSON 文本 → `RelationTable`；任何不合规都抛 `RelationFormatError`（**绝不返回半张表**）。

    严格是刻意的：这张表躺在用户的数据目录里（手写、编辑器、git 同步），半读一份再写回去
    等于把没读到的那几行静默吃掉——那比"读不出来"坏得多。容错留在**读盘那一层**
    （`knowledgestore.load_relations`：坏文件退成空表 + 警告、绝不抛），于是"读不干净的表"
    永远不会被写回用户文件（工具会因为"表里没有这个人"直接拒绝）。

    BOM 与 CRLF 先归一：手写、Windows 编辑器、git autocrlf 都会带进来，那不是坏输入
    （与 `knowledge.parse_entry_md` 同款）。

    **重名同样不合规**（姓名就是键，一个人一行）：那一行没有任何合法含义，静默收下只会
    让模型看到同一个人两条相反的评价，还让"改好了"只改到第一行。见 `_duplicate_names`。
    """
    raw = (text or "").lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RelationFormatError(f"关系表不是合法的 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise RelationFormatError(
            "关系表必须是一个 JSON 对象（{\"schema\": …, \"relations\": [ … ]}）。")
    rows = data.get("relations", [])
    if not isinstance(rows, list):
        raise RelationFormatError("关系表里的 relations 必须是一个数组。")
    duplicated = _duplicate_names(rows)
    if duplicated:
        # 姓名就是**键**（§6.1："每个有关系的人一行"），重复的那一行没有任何合法含义：放行
        # 的话，模型会看到同一个人两条自相矛盾的记录（"对面是谁"这一节当场自毁），工具
        # "改好了"只改到第一行，而散场合并还可能把陈旧那行写回本体。与"一行字段越界"同一
        # 口径：整表退成空 + 警告（`load_relations` 那一层），绝不半读。
        raise RelationFormatError(
            f"关系表里有重复的姓名：{'、'.join(duplicated)}——每个人只能有一行。")
    try:
        return RelationTable.model_validate(data)
    except ValidationError as exc:
        raise RelationFormatError(
            f"关系表的字段不合法：{_format_validation(exc, rows)}") from exc


# ---------------------------------------------------------------- 小节渲染 --

def _relation_line(row: Relation) -> str:
    """一行关系：`- 姓名（性别，亲密度 N）｜关系：…｜相处：…｜条目：键`。

    没写的部分**不渲染**（空性别不留空括号、空描述/空模式不留悬空分隔符）：提示词里每一行
    都要重新计费，而且一个悬空的 `｜相处：` 会让模型以为那里本来就该有个说法。
    """
    # 性别也要过 `_one_line`：这一节是**一行一条**，而这一行的头部是模型认人的唯一依据
    # （"我对面站的是谁"）。性别里混进的换行会渲染出**一个不存在的人**——多出来那一行看起来
    # 完全合法，还带着亲密度与关系描述，角色会照着它称呼、判断亲疏。
    meta = [_one_line(row.gender), f"{_CLOSENESS_LABEL} {int(row.closeness)}"]
    head = (f"{_RELATION_ROW_PREFIX}{_one_line(row.name)}"
            f"（{_RELATION_META_SEP.join(part for part in meta if part)}）")
    fields: list[str] = []
    if row.description.strip():
        fields.append(f"{_DESCRIPTION_LABEL}：{_one_line(row.description)}")
    if row.mode.strip():
        fields.append(f"{_MODE_LABEL}：{_one_line(row.mode)}")
    # 键一定附上（§6.5）：这是角色顺着往下读的唯一线索，缺了它这行就只是一句评价。
    fields.append(f"{_KEY_LABEL}：{_one_line(row.entry_key)}")
    return head + "".join(_RELATION_FIELD_SEP + field for field in fields)


def render_relations(table: RelationTable) -> str:
    """关系表 → 提示词里的【你的人际关系】小节；**没有任何关系行 → 空串**（§6.2）。

    三条判断写在这里（改之前先读）：

    1. **全部关系行都进这一节，不设条数上限、不截断**（与信息库索引的 `max_entries` 正好
       相反）。索引是"想知道更多时去翻的书"，丢了最旧的那几条只是少一个入口；关系表是
       每次开口都要用的——漏掉一行等于让角色忘掉一个**正在跟他说话的人**，而那恰恰是本
       节存在的理由。人真多到撑爆提示词时，该在**编辑侧**收敛（那张表被误用成通讯录了），
       而不是在渲染侧悄悄砍掉几行：砍哪几行都是错的，在场的那个人可能就是被砍的。
    2. **整节原样进系统消息**（由 `prompters` 拼在信息库索引**之前**、语言指令之前）：
       它几乎不变，而索引每 remember 一次就变——把变的留在最后，前缀缓存能多吃一截。
    3. **空串那条硬约束**：没有任何关系行时不许出现标题行、空行或占位文本（无关系角色的
       系统提示逐字节不变，有测试对拍 HEAD 的哈希钉死）。
    """
    rows = list(table)
    if not rows:
        return ""
    return "\n".join([RELATION_HEADER,
                      *(_relation_line(row) for row in rows),
                      "",
                      RELATION_HINT])
