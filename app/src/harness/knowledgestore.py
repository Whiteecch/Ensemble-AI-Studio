"""信息库的**落盘与场景副本**层（设计文档 §4 落盘布局 / §5 运行时副本 / §7.5 幂等 / §9.1 播种）。

本模块是信息库子系统里**唯一碰磁盘的地方**。纯数据与变换在 knowledge.py（零 IO），
提示词注入在 prompters.py，工具循环与结算决策在引擎（M2/M3）——这里只负责"东西存在
磁盘上的什么形状、怎么读回来、副本怎么建与怎么截"。

四条硬约束，改动前先读：

1. **条目文件是条目的唯一真相源**（§4.2）。`entries/<键>.md` 才是内容，`library.json`
   只存库级元信息与**索引顺序**。于是"扫到坏条目文件"这件事必须有个交代：坏文件只
   跳过它自己、把原因写进返回结构的 `warnings` 里，**绝不静默吞**，也绝不因为一个坏
   文件让整座库读不出来（那会把"一条坏"放大成"全丢"）。同理，`library.json` 坏了也
   不崩——退回扫描 `entries/`，顺序丢了是可恢复的损失，库没了不是。

   "唯一"还有后半句：**同一个键只能有一份文件**。文件名与 front-matter 的 `key` 不一致是
   合法输入（手写丢进来、改了 key 没改文件名），但那时库里就有两份文件装同一条目，而加载
   是"按文件名排序扫描、后来者覆盖"——旧副本会盖掉刚写的新内容，用户改了却不生效。
   故 `load_library` 为重复键报警告、`save_library` 把副本清掉、`delete_entry` 按文件
   **声明的键**删而不按文件名删。

2. **副本里的基线条目必须带轮次哨兵**（§5.3）。从本体拷进副本的条目，`origin.kind`
   原样保留（`manual`/`import`/`wide`），但 `origin.turn` 一律改成 `BASELINE_TURN`
   （-1），**绝不用 0**。理由：截断口径是"保留 turn ≤ cut"，而 0 永远 ≤ cut，等于
   静默永不截断——那正是 §5.3 点名的坑。哨兵把这层意思写进数据，让"只截本场条目"
   这条判定有第二道数值保险（本体里上一场留下的 `kind=scene` 条目，拷进来之后也不
   会被这一场的撤回误伤）。

3. **副本已存在则复用，不重置**（§5.1）。他中途离场又回来时，这一场的所得必须还在
   ——重置等于把他学到的东西悄悄抹掉，而界面上没有任何提示。

4. **写盘一律原子写**（mkstemp + fsync + os.replace，照抄 scenestore.py 的做法）。
   裸 `open(..., "a")` 在断电/崩溃时留下半截文件，下次加载就是一条坏条目。

5. **关系表是库里的一处"特殊分区"**（《人际关系与场景推进》§6.2/§6.5）：`relations.json`
   与 `library.json` / `entries/` / `pending.json` **同级**，跟着同一份副本走——建副本时
   拷一份基线、散场按保留/丢弃一起结算（`settle_relations`）、轮换归档时随目录整体搬走。
   绝不另造一套存储或结算：关系表只是"永远在眼前的几行"，它住的地方与条目完全一样。
   没有关系表的角色**磁盘上也与今天逐字节相同**（一个文件都不建）。

   **撤回与重置同样回溯关系改动**（§5.3 × §6.2）：关系小节是恒在上下文的回喂源，按 §5.3
   的判据它必须跟着退——见 `truncate_relations_after_turn`。判据是账本里的**轮次**与**底稿**
   （`pending.relation_edit_turns` / `relation_base`，工具每改一次记一笔），与条目那一路的
   `revised_turns` 同一口径。它**不在** `truncate_copy_after_turn` 里做：那一层只碰条目、
   也看不到本体库，而"退回开演时的样子"要读本体那一行与那一份底稿。

订阅与固化（§3.4）只做**数据侧**：订阅是活引用（读时现去源库取，天然"对方改我就改"），
固化把一条借来的条目拷成本库自有条目、从此断开引用。渲染一律交给
`knowledge.render_index`，本模块不自己拼索引文本——空库渲染成空串那条硬约束
（§6.1，无库角色系统提示逐字节不变）只有一个落点。
"""
from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from . import relations as relations_mod
from .knowledge import (
    MAX_KEY_LENGTH,
    Entry,
    EntryFormatError,
    EntryOrigin,
    entry_key_collision_error,
    entry_key_error,
    parse_entry_md,
    render_entry_md,
    render_index,
)
#: 非法字符表只有一处定义（`knowledge.py` 也是这么引用 loaders 的）：播种时要给过不了
#: 守门的种子行改造键，若在这里手抄一份字符表，迟早与守门那张漂开。
from .loaders import _ILLEGAL_NAME_CHARS

#: 库的 schema 版本（§3.2），落进 `library.json` 的 `schema` 键，便于将来迁移。
SCHEMA_VERSION = 1

#: 库目录下的固定文件名（§4.1）。
LIBRARY_JSON = "library.json"
ENTRIES_DIRNAME = "entries"
PENDING_JSON = "pending.json"
#: 订阅文件（§8.2）：住在库目录里，与 `library.json` / `entries/` 并列。**唯一一处口径**
#: ——界面（`gui/knowledge_editor`）与引擎（`knowledgetools`）都经 `load_subscriptions` 读它。
SUBSCRIPTIONS_JSON = "subscriptions.json"
#: 关系表文件（《人际关系与场景推进》§6.2）：库/副本里的一处**特殊分区**，与 `library.json`
#: 同级。复用同一份副本、同一份 `pending.json` 账本与同一套散场结算（§6.5），不另造一套。
RELATIONS_JSON = "relations.json"

#: 本体库根下的两个 scope 子目录。
CHARACTER_SCOPE_DIR = "characters"
WIDE_SCOPE_DIR = "wide"

#: 场景副本目录名：`runs/<场景>/<角色名>/library/`，与 state.jsonl / impressions/ 同级。
SCENE_LIBRARY_DIRNAME = "library"

#: **基线条目**的轮次哨兵（§5.3）。不是 0——0 会被"保留 turn ≤ cut"判为永不截断。
#: 任何合法 cut（≥0）都 ≥ 它，故数值上永不参与截断；它同时是一句自解释的声明：
#: "这条不是本场产生的"。
BASELINE_TURN = -1

#: `knowledge_boundary` 的兜底句——**这条常量的唯一出处**（§9.1：别另立第二份口径）。
#: 它原来是提示词里"没写边界"时渲染出的那句默认文案（【你的已知边界】那一节已随 §9.1
#: 撤除，这句话不再在任何提示词里出现）。它没有任何信息量，正是"没有边界声明时的占位"，
#: 播种时直接丢弃，不产出一条莫名其妙的条目。
KNOWLEDGE_FALLBACK = "只知道自己经历和被告知的事"

#: `knowledge_seed` 里"知道：X"形态的前缀（全角/半角冒号都认，写卡的人不该被这个卡住）。
_KNOW_PREFIXES = ("知道：", "知道:")

#: 结算结果的两种取值（§7.2）。
OUTCOME_KEEP = "keep"
OUTCOME_DISCARD = "discard"


def _now_iso() -> str:
    """当前 UTC 时刻的 ISO-8601 文本（秒精度）。只用于 pending 的结算时间戳。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _as_int(value: Any) -> int | None:
    """宽容地读一个整数（轮次 / 撤回位置）：读得出来就 `int`，读不出来 → `None`。**绝不抛。**

    放在存储层是因为它有两个用户，而那两处的口径必须一致：存储层的按轮次截断，以及工具层
    那三个写着"绝不抛"的取用方法（`note_recall` / `recall_text` /
    `truncate_recall_after_turn`）。各写一份迟早会在"什么算读得出来"上漂开——而轮次正是
    撤回的唯一判据（§5.3），两处不一致就会出现"以为截断了其实没有"。

    不认 bool（`True` 是 `int` 的子类，但"真/假"显然不是轮次）；认 int、有限浮点与十进制
    数字串；NaN / inf / 其它类型 → `None`。拿到 `None` 之后**往哪边降级由调用方定**，因为
    "条目的轮次"与"撤回的位置"要的降级方向正好相反：

      · 条目的轮次读不出来 → 按 0 计（= 最早，绝不会被误截掉，与 `memory.py:54` 同一口径）；
      · 撤回的位置读不出来 → 当空操作（= 什么都不删，绝不会把本场的条目一次误删光）。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _as_bool(value: Any) -> bool | None:
    """宽容地读一个是非（结算状态）：读得出来就 `bool`，读不出来 → `None`。**绝不抛。**

    与 `_as_int` 同一套路，只是真假只有两种写法被认：真 `bool`、`0`/`1`（JSON 里手写成
    数字是常见的），以及 `true`/`false`/`yes`/`no` 这类字符串（手改时大小写随意）。
    `42`、`"久了"`、列表一律算读不出来——**拿到 `None` 之后往哪边降级由调用方定**
    （`PendingChanges` 那边是"以 `outcome` 为准"，见那里的说明）。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return {0: False, 1: True}.get(value)
    if isinstance(value, float):
        return {0.0: False, 1.0: True}.get(value) if math.isfinite(value) else None
    if isinstance(value, str):
        return {"true": True, "1": True, "yes": True,
                "false": False, "0": False, "no": False}.get(value.strip().casefold())
    return None


def _int_map(value: Any) -> dict[str, int]:
    """宽容地读一格"名字 → 次数"（关系改动的本场账本）：整栏不是对象 → 空映射，坏值只丢自己。

    与 `_str_list` 同一套路。次数读不出来的那一条按**没改过**丢（而不是保守记 1）：账本
    读不出来时宁可让 §6.3 的频率闸门松一档（模型还能再改一次，用户看得见结果），也不要
    让它变严——那会让这一场的关系再也改不动，而用户无从知道为什么。非正数（0、-1）同样
    丢掉：次数只有"改过几次"，没有"改过零次"这种记法。
    """
    if not isinstance(value, dict):
        return {}
    out: dict[str, int] = {}
    for key, raw in value.items():
        number = _as_int(raw)
        if number is not None and number > 0:
            out[str(key)] = number
    return out


def _row_map(value: Any) -> dict[str, dict[str, Any]]:
    """宽容地读一格"名字 → 一行关系的底稿"（`relation_base`）：坏项只丢自己。

    与 `_int_map` 同一套路。丢掉那一条的代价是"这一对关系退回本体那一行 / 结算整行以副本
    为准"——与这次改动之前的行为一致；而整栏作废会把**同场其它**关系的底稿一起丢掉。
    键空着的那一条同样丢掉（对不上任何一行关系）。
    """
    if not isinstance(value, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, raw in value.items():
        name = str(key)
        if not name.strip() or not isinstance(raw, dict):
            continue
        out[name] = dict(raw)
    return out


def _str_list(value: Any) -> list[str]:
    """宽容地读一格"键名清单"：整栏不是列表 → 空清单，坏项（非字符串 / 空串）只丢它自己。

    键必须是字符串（`entry_key_error` 那一套的下游），收下一个数字等于凭空造出一个不存在
    的键——散场合并时会拿它去库里找条目。丢一项的代价只是"本场所得清单少列一行"，用户还
    能在副本里看见条目本身；整份清单作废的代价见 `PendingChanges` 的说明。
    """
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, str) and item]


# ------------------------------------------------------------------ 路径换算 --

def library_dir(kind: str, name: str, *, root: Path) -> Path:
    """信息库根 + 类型 + 名字 → 库目录（§4.1）。

    `kind` 只认 `"character"` / `"wide"`；别的值当场抛——猜一个默认值就等于把角色库
    写进广域库（让一个角色的私人见识变成全员可见），这种错不能靠运气。
    """
    base = Path(root)
    if kind == "character":
        return base / CHARACTER_SCOPE_DIR / name
    if kind == "wide":
        return base / WIDE_SCOPE_DIR / name
    raise ValueError(f"未知的库类型「{kind}」（只认 character / wide）。")


def entries_dir(lib_dir: Path) -> Path:
    """库目录 → 其 `entries/` 子目录（条目文件的唯一真相源所在）。"""
    return Path(lib_dir) / ENTRIES_DIRNAME


def library_json_path(lib_dir: Path) -> Path:
    """库目录 → `library.json`（库级元信息 + 索引顺序）。"""
    return Path(lib_dir) / LIBRARY_JSON


def entry_path(lib_dir: Path, key: str) -> Path:
    """库目录 + 键 → 条目文件路径；不安全的键抛 `ValueError`（§4.3 的文件名守门）。

    键会成为文件名，所以守门必须在**算路径这一刻**做，而不是等写盘时才炸——那时
    调用方已经拿着一个越界的路径跑了一段了。绝不静默改名：键是冲突判定的第一依据。
    """
    err = entry_key_error(key)
    if err:
        raise ValueError(err)
    return entries_dir(lib_dir) / f"{key}.md"


def pending_path(lib_dir: Path) -> Path:
    """库/副本目录 → `pending.json`（本场改动清单与结算状态，§7.5）。"""
    return Path(lib_dir) / PENDING_JSON


def relations_path(lib_dir: Path) -> Path:
    """库/副本目录 → `relations.json`（关系表这一处特殊分区，§6.2/§6.5）。"""
    return Path(lib_dir) / RELATIONS_JSON


def has_relations(lib_dir: Path) -> bool:
    """这个库目录里有没有关系表；探测本身出错也算"没有"。**绝不抛。**"""
    try:
        return relations_path(lib_dir).is_file()
    except OSError:
        return False


def scene_library_dir(run_root: Path, character: str) -> Path:
    """场景运行根 + 角色名 → 本场副本目录（§4.1）。

    `run_root` 是**这一场**的运行根（与 `memory.CharacterMemory` 的入参同一个东西），
    故副本正好落在该角色本场私有记忆文件夹的旁边——他这一场学到的东西与他的内心
    状态、对别人的印象在一起，散场时一起结算。
    """
    return Path(run_root) / character / SCENE_LIBRARY_DIRNAME


#: 已结算副本的归档名前缀（§5.1「一条戏一份副本」）：`library.settled-1`、`library.settled-2`…
#: 用**整名 + 后缀**（不是 `with_suffix`），与 `scenestore.RUNTIME_SUFFIX` 同一套写法：副本
#: 目录名若将来带上点（如 `library.v2`），换后缀会把原名算错、甚至把归档挪到别处。
SETTLED_ARCHIVE_PREFIX = SCENE_LIBRARY_DIRNAME + ".settled-"


def settled_archive_dir(copy_dir: Path) -> Path:
    """本场副本目录 → 本次轮换归档的目标目录（`<副本所在目录>/library.settled-<N>`，§5.1）。

    名字为什么是它（两条硬约束，都是"读错一次就很坏"的那种）：

      · **绝不与角色的正常副本目录撞名**：`scene_library_dir` 的结果**永远以副本目录名
        `library` 结尾**——归档名多一段后缀，任何角色名都换算不出它（副本目录名的最后一段
        只能是 `library`）。故"开场发现副本已结算 → 轮换 → 再建一份"这条路上，归档绝不会
        被当成"新的那一份副本"，也不会把某个角色真正的副本目录顶掉。
      · **绝不被当成一座库读进来**：库路径只有两个来源，`library_dir(kind, name, root)`
        （`<root>/characters/<名>` 或 `<root>/wide/<名>`，倒数第二段固定是那两个 scope 目录，
        整条路径落在**本体库根**那棵树里）与 `scene_library_dir`（末段固定 `library`）。
        归档两样都不满足：它叫 `library.settled-<N>`，住在**运行根**的角色运行目录下
        （父目录名是角色名，不是 `characters` / `wide`），且离库根那棵树很远。把它读成库，
        等于凭空多出一座装着上一场旧条目的库，下一次索引注入会把这些旧账喂给角色。

    序号 `N` = 该目录下**已有的同类归档数 + 1**（依次递增，不跳号），并且**绝不覆盖**：
    算出来的位置若已被占用（删过中间一份、或那个名字被别的东西占着），继续往后找一个空位。
    这是硬要求——归档是"旧账留痕"（§7.2 明写副本连着 `runs/` 存档一起留着），覆盖掉一份
    就等于把用户事后翻得到的证据抹掉。纯函数：只看目录里有什么，不写盘。
    """
    base = Path(copy_dir)
    parent = base.parent
    try:
        taken = {p.name for p in parent.iterdir()} if parent.is_dir() else set()
    except OSError:
        taken = set()            # 读不出来当作空；下面的"绝不覆盖"循环仍兜得住
    count = sum(1 for name in taken
                if name.startswith(SETTLED_ARCHIVE_PREFIX)
                and name[len(SETTLED_ARCHIVE_PREFIX):].isdigit())
    index = count + 1
    target = parent / f"{SETTLED_ARCHIVE_PREFIX}{index}"
    while target.name in taken or target.exists():
        index += 1
        target = parent / f"{SETTLED_ARCHIVE_PREFIX}{index}"
    return target


def has_library(lib_dir: Path) -> bool:
    """`library.json` 是否存在且是文件；探测本身出错也算"没有"。**绝不抛。**"""
    try:
        return library_json_path(lib_dir).is_file()
    except OSError:
        return False


def _infer_meta(lib_dir: Path) -> tuple[str, str, str]:
    """(name, scope, owner)：库目录路径 → 库级元信息的兜底推断。

    用在两个地方：手写/老库缺 `library.json` 时、以及给"本体库不存在"的角色建空副本时。
    正式布局下 `.../characters/<角色名>` 一眼看穿 scope；副本
    `runs/<场景>/<角色名>/library` 的上一层就是角色名，按角色库处理。推断出来的东西
    只是**兜底**，永远不覆盖 `library.json` 里已有的值。
    """
    path = Path(lib_dir)
    container = path.parent.name
    if container == WIDE_SCOPE_DIR:
        return path.name, "wide", ""
    if container == CHARACTER_SCOPE_DIR:
        return path.name, "character", path.name
    return path.name, "character", path.parent.name


# -------------------------------------------------------------------- 原子写 --

def _atomic_write_text(path: Path, text: str) -> None:
    """文本原子落盘：同目录临时文件 → fsync → `os.replace`；失败清掉临时文件。

    临时文件必须与目标**同目录**（同卷），`os.replace` 才是原子替换；跨卷会退化成
    拷贝，中间态就露出来了。`newline="\\n"` 让落盘字节可复现，不随平台翻成 CRLF。
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


# ------------------------------------------------------------------ 数据模型 --

class Library(BaseModel):
    """一座库：库级元信息（§3.2）+ 内存中的条目表。

    `entries` 是 `键 → Entry` 的**内存索引**，它不落进 `library.json`（那是"唯一真相源"
    的物理形态，见模块 docstring 第 1 条）；读盘时它由扫描 `entries/` 重建。

    `extra="forbid"`：`library.json` 里出现不认识的字段（多半是手写笔误）当场在
    解析时炸——但 `load_library` 把它接住并降级，不让它带走整座库。
    """
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    #: 落盘键名是 `schema`（§3.2 写死的），但 `schema` 会遮蔽 BaseModel 的同名属性，
    #: 故字段名取 `schema_version`、对外用别名。用 `model_dump(by_alias=True)` 写盘。
    schema_version: int = Field(default=SCHEMA_VERSION, alias="schema")
    name: str = ""
    scope: str = "character"
    owner: str = ""
    order: list[str] = Field(default_factory=list)
    updated_at: str = ""
    entries: dict[str, Entry] = Field(default_factory=dict)

    def ordered_keys(self) -> list[str]:
        """索引显示顺序：先按 `order`，再补上 order 里没有的键（按键序，保证可复现）。

        `order` 是 `library.json` 给的人为顺序，但条目文件才是真相源——手写丢进来的
        新条目不在 order 里，不能因此就被丢掉，只能排在后面。
        """
        out: list[str] = []
        seen: set[str] = set()
        for key in self.order:
            if key in self.entries and key not in seen:
                seen.add(key)
                out.append(key)
        out.extend(sorted(k for k in self.entries if k not in seen))
        return out

    def ordered_entries(self) -> list[Entry]:
        """按索引顺序取条目列表（渲染与"活引用"都吃这一份）。"""
        return [self.entries[k] for k in self.ordered_keys()]

    def upsert(self, entry: Entry) -> None:
        """放入/覆盖一条条目，并把它补进索引顺序的末尾（已在其中则原位不动）。

        **拒绝只有大小写不同的键**（`entry_key_collision_error`）：`ABC` 与 `abc` 各自
        都是合法的键、是两条不同的条目，却是同一个文件名——放行等于后一条静默顶掉前一条，
        用户只会发现"我记的东西少了一条"，无从排查。这里当场抛中文原因，把问题摆到调用方
        面前（§4.3"不安全就报错给用户看，绝不静默改名"的同一条原则：宁可报错，不可丢条）。

        键**完全相同**不算碰撞：那是"同一条目被覆盖"，正是 upsert 的本意。
        """
        for existing in self.entries:
            clash = entry_key_collision_error(entry.key, existing)
            if clash:
                raise ValueError(clash)
        self.entries[entry.key] = entry
        if entry.key not in self.order:
            self.order.append(entry.key)

    def remove(self, key: str) -> bool:
        """摘掉一条条目（连同它的顺序位）；本来就没有 → False。"""
        if key not in self.entries:
            return False
        del self.entries[key]
        self.order = [k for k in self.order if k != key]
        return True


@dataclass
class LoadedLibrary:
    """`load_library` 的返回：库 + 读盘时**看见的问题**。

    为什么要把 warnings 放在返回结构里而不是写日志：坏条目意味着"用户手写的条目
    没被收录"，调用方（编辑器、引擎）必须能把它摆到用户面前。静默吞掉是最坏的选择
    ——用户会发现条目一夜之间少了一条，而程序不吭声。
    """

    library: Library
    warnings: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ 库的读写 --

def _declared_keys(ents: Path, skip_stems: Iterable[str] = ()) -> dict[Path, str]:
    """扫一遍 `entries/`：**读得出来**的条目文件 → 它 front-matter 里的 `key`。

    规范形态下文件名就是 `<键>.md`，看 `path_md.stem` 就够了；但"文件名与 key 不一致"是
    合法输入（手写丢进来、或改了 key 却没改文件名，`load_library` 会为此报一条警告），
    这时只有读出 front-matter 才知道这份文件装的是哪个键。

    `skip_stems` 里的文件名**不读**（调用方已经知道它们装的是哪个键）——规范命名的文件
    占绝大多数，跳过它们能让"健康库"这条路上的扫描一次文件解析都不做。
    读不出来的坏文件**不进结果**，于是调用方能据此把它们整类排除在外，绝不误删。
    """
    skip = set(skip_stems)
    out: dict[Path, str] = {}
    for path_md in sorted(Path(ents).glob("*.md")):
        if path_md.stem in skip:
            continue
        try:
            out[path_md] = parse_entry_md(path_md.read_text(encoding="utf-8")).key
        except (EntryFormatError, OSError, UnicodeDecodeError):
            continue
    return out


def _duplicate_copies(ents: Path, keys: set[str]) -> list[Path]:
    """扫出"文件名不是 `<键>.md`、而 front-matter 里的 key 正是这批要写的键"的文件。"""
    return [path_md for path_md, key in _declared_keys(ents, skip_stems=keys).items()
            if key in keys]


def load_library(path: Path) -> LoadedLibrary:
    """读一座库：`library.json` 取元信息与顺序 + **扫描 `entries/` 取条目**。

    容错口径（都记进 warnings，绝不静默）：
      · 条目文件解析失败（`EntryFormatError` / 读不动）→ 跳过该条并报出文件名与原因；
      · 文件名与 front-matter 的 `key` 不一致 → 以 **front-matter 为准**（它才是真相源）
        并报一条警告，否则"用户改了 key 却没生效"；
      · **两个文件声明同一个键** → 按文件名排序扫描，后来者胜，但必须把两份文件名都报出来：
        被压住的那份此刻读不到，用户得知道该删哪个（`save_library` 会把这类副本清掉，
        所以下一次保存后自然收敛成一份）；
      · `library.json` 坏了 → 退回扫描，库级元信息用路径推断兜底；
      · 目录根本不存在 → 空库、零警告（老卡老存档的正常状态，§9.2）。
    """
    lib_dir = Path(path)
    name, scope, owner = _infer_meta(lib_dir)
    warnings: list[str] = []
    library = Library(name=name, scope=scope, owner=owner)

    json_path = library_json_path(lib_dir)
    try:
        raw = json_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raw = None                      # 手写库/首次创建：没有 library.json 是常态
    except (OSError, UnicodeDecodeError) as exc:
        raw = None
        warnings.append(f"library.json 读不出来（{exc}），已退回只读条目文件。")
    if raw is not None:
        try:
            library = Library.model_validate(json.loads(raw))
        except (ValueError, ValidationError) as exc:
            library = Library(name=name, scope=scope, owner=owner)
            warnings.append(f"library.json 不合法（{exc}），已退回只读条目文件。")

    source_of: dict[str, str] = {}
    for path_md in sorted(entries_dir(lib_dir).glob("*.md")):
        try:
            entry = parse_entry_md(path_md.read_text(encoding="utf-8"))
        except (EntryFormatError, OSError, UnicodeDecodeError) as exc:
            warnings.append(f"条目文件 {ENTRIES_DIRNAME}/{path_md.name} 读不了：{exc}")
            continue
        if entry.key != path_md.stem:
            warnings.append(
                f"条目文件 {ENTRIES_DIRNAME}/{path_md.name} 里的 key 是「{entry.key}」，"
                f"与文件名不一致；以文件内容为准。")
        if entry.key in source_of:
            warnings.append(
                f"两条条目文件（{ENTRIES_DIRNAME}/{source_of[entry.key]} 与 "
                f"{ENTRIES_DIRNAME}/{path_md.name}）声明了同一个键「{entry.key}」；"
                f"只读到了后一份的内容，另一份现在读不到，请删掉多余的那份。")
        library.entries[entry.key] = entry
        source_of[entry.key] = path_md.name
    return LoadedLibrary(library=library, warnings=warnings)


def save_library(lib: Library, path: Path, *, prune: bool = False) -> Path:
    """把一座库写回 `path`（目录）：逐条原子写 `entries/*.md` + 写 `library.json`。

    `library.json` 只写库级元信息与**归一化后的**顺序（幽灵键、重复键顺手自愈），
    条目内容一个字都不进去。`updated_at` 由调用方给——本层**不猜时间**：`app/libraries/`
    是要入 git 的素材，每次保存都自动盖个时间戳只会让 diff 全是噪声。

    `prune=False`（默认）时**不删**磁盘上多余的条目文件：刚加载完就保存是常态，而
    加载时跳过的坏文件不在内存里，prune 会把它们永久抹掉——那是数据损坏。要删条目
    请用 `delete_entry` 显式删。

    但**同一个键的第二份文件**必须清掉，这不属于 prune：文件名与 key 不一致的手写条目
    （`z.md` 里装的是键 `b`）内容刚刚已经写进 `<键>.md` 了，留着它就会在下次加载时按
    文件名排序**盖掉刚写的新内容**——界面上改了、重载后改动凭空消失。清不掉的（只读 /
    被占用）也不致命：下一次加载会重新报出"两条文件同一个键"的警告，用户看得见。
    """
    lib_dir = Path(path)
    ents = entries_dir(lib_dir)
    ents.mkdir(parents=True, exist_ok=True)

    keys = lib.ordered_keys()
    for key in keys:
        _atomic_write_text(entry_path(lib_dir, key), render_entry_md(lib.entries[key]))
    for path_md in _duplicate_copies(ents, set(keys)):
        try:
            path_md.unlink()
        except OSError:
            pass                      # 删不掉就等下一次加载把它报给用户
    if prune:
        for path_md in sorted(ents.glob("*.md")):
            if path_md.stem in lib.entries:
                continue
            try:
                path_md.unlink()
            except OSError:
                pass                      # 删不掉不当致命：下次保存再试

    meta = lib.model_dump(by_alias=True, exclude={"entries"})
    meta["order"] = keys
    _atomic_write_text(library_json_path(lib_dir),
                       json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    return lib_dir


def delete_entry(lib_dir: Path, key: str) -> bool:
    """删一条条目：文件删掉 + 从 `library.json` 的顺序里摘掉；本来就没有 → False。

    条目由**键**识别（§3.1"同一 key 即同一件事"），文件名叫什么不算：手写或改键留下的
    `z.md` 里装的可能正是 `key` 这条条目，只按 `<键>.md` 删就会出现"删了又自己回来"
    ——另一份文件还在，下次加载照样把它读进来，而这个函数已经返回 True。
    只动这一条，不重写其它条目文件——删一条不该让整座库的 mtime 全变一遍。

    判据是**文件声明的键**（`_declared_keys`），只有内容读不出来（连 key 都不知道）时才
    退回按规范名删那个壳——否则会把"文件名叫 `<键>.md`、内容其实是另一条"的文件误删，
    那是真正的内容损失。
    """
    lib_dir = Path(lib_dir)
    try:
        canonical = entry_path(lib_dir, key)
    except ValueError:
        return False
    declared = _declared_keys(entries_dir(lib_dir))
    targets = [p for p, declared_key in declared.items() if declared_key == key]
    if canonical.is_file() and canonical not in declared:
        targets.append(canonical)
    if not targets:
        return False
    removed = False
    for path_md in targets:
        try:
            path_md.unlink()
        except OSError:
            continue
        removed = True
    if not removed:
        return False
    _drop_from_order(lib_dir, key)
    return True


def _drop_from_order(lib_dir: Path, key: str) -> None:
    """从 `library.json` 的 order 里摘掉一个键（文件不在/读不动就什么都不做）。"""
    path = library_json_path(lib_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return
    if not isinstance(data, dict) or not isinstance(data.get("order"), list):
        return
    data["order"] = [k for k in data["order"] if k != key]
    try:
        _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    except OSError:
        pass                              # 顺序是次要信息，写不动不值得让删除失败


# ------------------------------------------------------------- 场景副本 --

def _as_baseline(entry: Entry) -> Entry:
    """本体条目 → 副本里的**基线**条目：`kind` 原样，`turn` 换成哨兵。

    只改这两处。`scene`/`at` 等出处信息一字不动——"我从哪知道这件事的"是有价值的
    线索，抹掉它只换来更干净的数据，不值。
    """
    return entry.model_copy(
        update={"origin": entry.origin.model_copy(update={"turn": BASELINE_TURN})})


def create_scene_copy(src_dir: Path, dst_dir: Path) -> list[str]:
    """把本体库复制一份到 `dst_dir`，返回读源库时看见的问题（warnings）。

    本体库不存在（角色从没有过库）→ 建**空副本**，元信息按目标路径推断（§5.1）。
    本体条目拷进来时一律走 `_as_baseline`（`turn` 换哨兵，见模块 docstring 第 2 条）。

    这是**覆盖式**写入：先清掉目标 `entries/` 里已有的条目文件再写。它只该用于首次
    建副本——"他已经在场里待过"的情况走 `ensure_scene_copy`，那条路绝不重置。

    关系表（§6.2 的特殊分区）一并拷基线（`ensure_relations_copy`）：关系改动写只写副本
    （§5.2），副本没有那一份的话，角色一开场就对所有熟人都失忆。本体没有关系表的人
    **一个文件都不多**（无关系 = 磁盘上也与今天相同）。
    """
    src = Path(src_dir)
    dst = Path(dst_dir)
    if has_library(src) or entries_dir(src).is_dir():
        loaded = load_library(src)
        warnings = list(loaded.warnings)
        base = loaded.library
    else:
        name, scope, owner = _infer_meta(dst)
        warnings = []
        base = Library(name=name, scope=scope, owner=owner)

    out = Library(name=base.name, scope=base.scope, owner=base.owner)
    out.order = list(base.order)
    for key in base.ordered_keys():
        out.entries[key] = _as_baseline(base.entries[key])

    ents = entries_dir(dst)
    ents.mkdir(parents=True, exist_ok=True)
    for path_md in sorted(ents.glob("*.md")):
        try:
            path_md.unlink()
        except OSError:
            pass
    save_library(out, dst)
    save_pending(dst, PendingChanges())        # 新副本：本场改动清单从零起算
    ensure_relations_copy(src, dst)            # 关系表的基线（源没有 → 什么都不建）
    return warnings


def ensure_scene_copy(src_dir: Path, dst_dir: Path) -> bool:
    """确保副本存在：已存在则**原样复用**，返回 False；本次新建则返回 True。

    复用这一条是硬的（§5.1）：他中途离场又回来时，这一场学到的东西必须还在。重置等于
    把他这一场的所得悄悄抹掉，而界面上没有任何提示——只有"他怎么不记得了"。
    """
    dst = Path(dst_dir)
    if has_library(dst) or entries_dir(dst).is_dir():
        return False
    create_scene_copy(src_dir, dst)
    return True


def truncate_copy_after_turn(lib_dir: Path, cut: int) -> int:
    """按轮次截断本场副本，返回被丢弃的条目数（§5.3，口径与 `memory.truncate_after_turn` 对齐）。

    只丢**本场产生的**条目：`origin.kind == "scene"` 且 `origin.turn > cut`。从本体
    拷来的基线条目（manual/import/wide）与订阅来的活引用一律不动——它们不是这一场的
    产物，撤回这一场的推进不该让角色忘掉自己的身世。**保留 turn ≤ cut**，与
    `memory.py:79-111` 完全一致。

    被截掉的键同时从 `pending.json` 摘掉：留着"待结算"的空键，散场合并时会去找一条
    已经不在的条目。幂等、绝不抛（撤回不该因 IO 失败中断）。

    **删不掉的文件 = 没截掉这条**（只读文件、或被另一个进程开着）：这时内存、`order` 与
    `pending` 一处都不能摘，返回的计数也不能算它。三处都改成"已丢弃"而文件还在磁盘上，
    撤回就等于没做——下次 `load_library`（每次渲染索引都会调）又把这条读回来，调用方却
    拿到"成功丢弃 1 条"，无从知道副本并未截断。留着它，下次撤回再试。

    **`cut` 读不出来 → 一条都不删、返回 0**（见 `_as_int`）：撤回的位置猜小一点就是误删，
    而条目的坏轮次按 0 计（= 永不误截）那条口径在这里不能照搬——0 对撤回位置意味着"把本场
    所有条目一次删光"，包括本轮刚记下的。判据读不出来时唯一安全的动作是不做。
    """
    lib_dir = Path(lib_dir)
    cut_value = _as_int(cut)
    if cut_value is None:
        return 0
    cut = cut_value
    if not has_library(lib_dir) and not entries_dir(lib_dir).is_dir():
        return 0
    loaded = load_library(lib_dir)
    lib = loaded.library
    candidates = [k for k, e in lib.entries.items()
                  if e.origin.kind == "scene" and int(e.origin.turn) > cut]
    dropped: list[str] = []
    for key in candidates:
        try:
            entry_path(lib_dir, key).unlink()
        except FileNotFoundError:
            pass                              # 文件本来就不在（内存里这条也该跟着走）
        except OSError:
            continue                          # 文件还在磁盘上：这条不算截掉
        lib.remove(key)
        dropped.append(key)
    if dropped:
        try:
            save_library(lib, lib_dir)        # library.json 的顺序里也要摘掉
        except OSError:
            pass                              # 顺序是次要信息，ordered_keys 会忽略幽灵键
        _drop_pending_keys(lib_dir, dropped)
    return len(dropped)


# --------------------------------------------------------------- pending --

class PendingChanges(BaseModel):
    """副本目录下的本场改动清单与结算状态（§7.5）。

    `added` / `revised` 是"本场所得"清单的两半（新增 vs 改过的旧认识），给你决定
    保留/丢弃时看；`settled` + `outcome` + `settled_at` 是结算状态的落点——**幂等靠它**：
    已结算的副本再次结算时，先到的结果说了算，绝不翻盘。

    `revised_turns` 是"改过的旧认识"那一半的**轮次**（键 → 那次修订的轮次，§5.3.1）。
    它服务于撤回：本场对**基线条目**的修订也要跟着回退，而回退的判据只能是"那次修订
    落在 cut 之后吗"。`revised` 那份键名清单答不了这个问题，也不能拿它的下标凑一个——
    那会凭空造出一个轮次（判断错的方向是静默的：要么把一条现行说法还原成旧文，要么让
    "从没发生过的事"永远留在副本里）。

    **降级口径（向后兼容）**：老 `pending.json` 里没有这个字段 → 读到空映射 → 那几条
    **保守不回退**。这不是偷懒而是唯一安全的选择：轮次缺失时，"不回退"与这次改动之前的
    行为一致（那份副本本来就是这么跑过来的），而"猜一个轮次"是拿用户的数据赌。轮次缺失
    只影响那一条，其余条目照常处理。

    `relation_edits` 是**关系表**那一半的本场账本（《人际关系与场景推进》§6.3/§6.5）：
    "本场改过哪一对关系、各几次"。它有两个用处，都必须**跨块**活着（故落在盘上，不能只在
    内存里数）：

      · §6.3 的频率闸门（同一对每场最多改 N 次）——闸门在文件里，跨 think、跨块都算同一场；
      · 散场合并的判据——`merge_relations` 只并账本里记过的名字（与条目那一路同口径：
        账本判"是不是本场所得"，差分只用来报警）。没有关系表的角色这一栏恒空，
        `pending.json` 的形状因此与今天只多一个空对象。

    `relation_edit_turns` / `relation_base` 是同一条账本的另外两栏，服务的都是**退回**：

      · `relation_edit_turns`：那一对关系**最后一次**改在第几轮——撤回与重置按轮次回退的
        唯一判据（与 `revised_turns` 同一口径：缺轮次 = 老账本 = **保守不回退**，
        "不回退"与这次改动之前的行为一致，而猜一个轮次是拿用户的数据赌）；
      · `relation_base`：改它**之前**那一行的底稿（`Relation.model_dump(mode="json")` 的
        形状）。它有两个用处，缺一个都会咬人：回退时它是"本场开演时这一行长什么样"的
        唯一可靠来源（本体那一行可能已被用户在外面改过），结算时它是"本场到底动过哪几个
        字段"的判据——本场没碰的字段一律以本体为准，不能因为本场顺手改了一次亲密度就把
        用户手写的描述整行盖回旧文。**取第一次**（`note_relation_edit` 只补空），因为那
        一份才是开演时的样子。底稿缺失（老账本）→ 回退时回落到本体那一行、结算时整行以
        副本为准（= 这次改动之前的行为）。

    `extra="ignore"`：这是运行产物不是素材，别的版本写进去的陌生键不该让它读不出来
    （与 `library.json` 的严格相反，那里的严格是为了别把用户的笔误抹掉）。
    """
    model_config = ConfigDict(extra="ignore")

    added: list[str] = Field(default_factory=list)
    revised: list[str] = Field(default_factory=list)
    revised_turns: dict[str, int] = Field(default_factory=dict)
    relation_edits: dict[str, int] = Field(default_factory=dict)
    relation_edit_turns: dict[str, int] = Field(default_factory=dict)
    relation_base: dict[str, dict[str, Any]] = Field(default_factory=dict)
    settled: bool = False
    outcome: str = ""                  # "keep" / "discard"（未结算 = 空串）
    settled_at: str = ""

    @model_validator(mode="before")
    @classmethod
    def _keep_only_readable_fields(cls, data: Any) -> Any:
        """逐字段降级：**坏值只丢它自己那一条，绝不因为一个字段让整份清单作废**。

        `pending.json` 是运行产物、现实里会被手改、被别版本写坏（任务书把"人手改成坏 JSON"
        列为必试项）。而 `PendingChanges` 里住着结算状态（`settled` / `outcome`）：整份解析
        失败退回空清单的话，`added` 里混进的一个数字就能把"已结算 / 丢弃"抹掉，
        `mark_settled` 下一次会返回 True 并把 outcome 翻成 keep——一次重复点击或重开界面，
        用户明确选了「丢弃」的一整场记忆就永久并进了本体，§7.5 的先到先得当场断掉。所以
        校验放在**模型级**：逐字段清洗完再交给 pydantic，校验器自己不抛。

        每一栏的方向都是"丢一项"而不是"丢全部"，且丢的那一项必定还有别处兜底：

          · `added` / `revised`（`_str_list`）：只收非空字符串，整栏不是列表当空清单。最坏
            结果是"本场所得"清单少列一行——条目还在副本里，用户看得见；而整份作废丢的是
            不可逆的结算状态。
          · `revised_turns`（`_as_int`）：读不出轮次的那几条**保守不回退**（见类 docstring），
            与这次改动之前的行为一致。
          · `settled`（`_as_bool`）：读不出真假时**以 `outcome` 为准**——`mark_settled` 永远
            同时写下这两项，故 outcome 写了值就说明结算发生过。方向刻意选在"当成已结算"：
            误判成未结算会让一次重复点击把「丢弃」翻成「保留」（不可逆，角色凭空多出一批
            没同意永久化的记忆），误判成已结算只是让这次重复结算不生效（用户看得见，能重来）。
          · `outcome` / `settled_at`：不是字符串就清空——它们只是记录，不参与幂等判据；
            一个手改坏的时间戳不该连累任何别的字段。
          · `relation_edits` / `relation_edit_turns`（`_int_map`）：坏的一格只丢它自己那一条
            （见 `_int_map` 的方向说明：账本读不出来时宁可让频率闸门松一档，也不要让它变严）。
            轮次丢掉的后果是"那一条不回退/不按字段并"，与这次改动之前的行为一致。
          · `relation_base`（`_row_map`）：坏的一格只丢它自己那一条底稿——那一对关系退回
            本体那一行、结算整行以副本为准，都是"这次改动之前的行为"。
        """
        if not isinstance(data, dict):
            return {}
        turns: dict[str, int] = {}
        raw_turns = data.get("revised_turns")
        if isinstance(raw_turns, dict):
            for key, raw in raw_turns.items():
                turn = _as_int(raw)
                if turn is not None:
                    turns[str(key)] = turn
        outcome = data.get("outcome")
        outcome = outcome if isinstance(outcome, str) else ""
        settled = _as_bool(data.get("settled"))
        if settled is None:                       # 读不出真假 → outcome 说了算
            settled = outcome in (OUTCOME_KEEP, OUTCOME_DISCARD)
        settled_at = data.get("settled_at")
        return {
            "added": _str_list(data.get("added")),
            "revised": _str_list(data.get("revised")),
            "revised_turns": turns,
            "relation_edits": _int_map(data.get("relation_edits")),
            "relation_edit_turns": _int_map(data.get("relation_edit_turns")),
            "relation_base": _row_map(data.get("relation_base")),
            "settled": settled,
            "outcome": outcome,
            "settled_at": settled_at if isinstance(settled_at, str) else "",
        }


def load_pending(lib_dir: Path) -> PendingChanges:
    """读 `pending.json`；缺失 / 空 / 坏 JSON / 类型不对 → 空清单。**绝不抛。**

    缺失不是错误：刚建的副本、老存档、还没记过任何改动的场次都是"空清单"。

    注意"坏 JSON"指**文件级**坏掉（读不出来、不是对象）。字段级的坏值不走这条退回空清单的
    路：`PendingChanges` 自己在模型层逐字段降级（坏值只丢它自己，见那边的说明），好让
    `settled` / `outcome` 不被一个手改坏的列表连坐。下面的 except 是最后一张网，兜的是
    模型层之外的意外。
    """
    try:
        raw = pending_path(lib_dir).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return PendingChanges()
    try:
        data = json.loads(raw)
    except ValueError:
        return PendingChanges()
    if not isinstance(data, dict):
        return PendingChanges()
    try:
        return PendingChanges.model_validate(data)
    except ValidationError:
        return PendingChanges()


def save_pending(lib_dir: Path, pending: PendingChanges) -> Path:
    """原子写 `pending.json`，返回写入路径（自动建父目录）。"""
    target = pending_path(lib_dir)
    _atomic_write_text(target, json.dumps(pending.model_dump(), ensure_ascii=False,
                                          indent=2) + "\n")
    return target


def record_pending(lib_dir: Path, key: str, *, revised: bool = False,
                   turn: int | None = None) -> PendingChanges:
    """记一笔本场改动：`revised=False` → 进 `added`，否则进 `revised`。返回写后的清单。

    先新增后修订仍算**新增**（它就是本场长出来的新东西），故不会同时出现在两份清单里。
    重复记同一笔是幂等的（不去重的话"本场所得"清单里会出现两行同名条目）。

    `turn` 是这次修订发生在本场第几轮（§5.3.1），进 `revised_turns`。缺省 `None` = 调用方
    给不出轮次（老调用点、或根本拿不到）——**不覆盖**已经记下的轮次（擦掉它等于主动放弃
    回退），只是这一条在撤回时保守地不回退。同一个键被反复修订时取**最晚**那一次：只要
    有一次修订依据的是被撤回的下文，这条现行说法就沾了它，宁可多回退一次也别把"从没
    发生过的事"留在副本里继续回喂。

    转成"新增"（`revised=False`）时把轮次一并摘掉：这条已经不是"改过的旧认识"了，
    留着它的轮次只会在下次撤回时被当成一条要回退的基线。
    """
    pending = load_pending(lib_dir)
    if revised:
        if key not in pending.added and key not in pending.revised:
            pending.revised.append(key)
        round_ = _as_int(turn)
        if round_ is not None:
            pending.revised_turns[key] = max(pending.revised_turns.get(key, round_), round_)
    else:
        if key not in pending.added:
            pending.added.append(key)
        pending.revised = [k for k in pending.revised if k != key]
        pending.revised_turns.pop(key, None)
    save_pending(lib_dir, pending)
    return pending


def relation_edits(lib_dir: Path) -> dict[str, int]:
    """本场对每一对关系各改了几次（`pending.json` 的 `relation_edits`）。**只读、绝不抛。**

    闸门（§6.3 第二重）与散场合并（§6.5）都读这一份：前者问"这一对还能改吗"，后者问
    "这一对是不是本场改过的"。同一个账本回答两个问题，就不可能两处各说各话。
    """
    return dict(load_pending(lib_dir).relation_edits)


def note_relation_edit(lib_dir: Path, name: str, *, turn: int | None = None,
                       base: dict[str, Any] | None = None) -> int:
    """记一笔"本场改过这个人的关系"，返回**改完之后**的次数。写不动就抛（调用方接住）。

    名字空着不记（那不是一次对某一对关系的改动，闸门不该被一个空名字占掉一格）。
    与 `record_pending` 同一口径：**先写盘再返回**——返回的次数就是磁盘上那个数，
    否则"闸门说还能改、盘上已经满了"这种偏差会在崩溃后变成一次超发。

    同一笔顺手记下**第几轮**改的（`turn`）与**改之前那一行的底稿**（`base`）：

      · 轮次取**最后一次**（同一对关系在本场被改多次时，只要有一次依据的是被撤回的下文，
        这一对就该跟着退，与 `record_pending` 对修订的处置同一个方向）；
      · 底稿取**第一次**（`key not in ...` 才写）：那是"本场开演时它长什么样"，后来的那几
        次改的都不是它。`base` 缺省 `None` = 调用方给不出底稿（老调用点）——那一条回退时
        回落到本体那一行、结算时整行以副本为准（见 `PendingChanges` 的说明）。

    两样都不是必须的：给不出就不记，那一对关系退化成"这次改动之前的行为"，绝不猜。
    """
    key = (name or "").strip()
    pending = load_pending(lib_dir)
    if not key:
        return 0
    count = int(pending.relation_edits.get(key, 0)) + 1
    pending.relation_edits[key] = count
    round_ = _as_int(turn)
    if round_ is not None:
        pending.relation_edit_turns[key] = round_
    if isinstance(base, dict) and key not in pending.relation_base:
        pending.relation_base[key] = dict(base)
    save_pending(lib_dir, pending)
    return count


def mark_settled(lib_dir: Path, outcome: str) -> bool:
    """标记本副本已结算（`outcome` = `"keep"` / `"discard"`）；本次真的改了状态 → True。

    **先到先得**（§7.5）：已结算过的副本再调一次不改结果、返回 False。否则一次手滑的
    重复点击就会把"丢弃"翻成"保留"，角色凭空多出一批你从没同意永久化的记忆。
    """
    pending = load_pending(lib_dir)
    if pending.settled:
        return False
    pending.settled = True
    pending.outcome = outcome
    pending.settled_at = _now_iso()
    save_pending(lib_dir, pending)
    return True


def _drop_pending_keys(lib_dir: Path, keys: Iterable[str]) -> None:
    """把若干键从两份清单**以及它们的轮次记录**里摘掉（截断/还原后调用）；没变化就不动文件。

    轮次也要一起摘：留着它的下一次撤回会拿着一个已经不存在的键去本体找原文（§5.3.1 的
    回退路径），找不到就报一条"没能还原"的假警报——用户照着去查，发现库里根本没有这条。
    """
    pending = load_pending(lib_dir)
    drop = set(keys)
    before = (list(pending.added), list(pending.revised), dict(pending.revised_turns))
    pending.added = [k for k in pending.added if k not in drop]
    pending.revised = [k for k in pending.revised if k not in drop]
    for key in drop:
        pending.revised_turns.pop(key, None)
    if (pending.added, pending.revised, pending.revised_turns) != before:
        save_pending(lib_dir, pending)


def _forget_relation_edits(lib_dir: Path, names: Iterable[str]) -> None:
    """把这几个名字从关系账本的**三栏**里一起摘掉（回退成功之后调用）；没变化就不动文件。

    三栏同进退：`relation_edits` 说"改过几次"（§6.3 的额度与散场判据）、`relation_edit_turns`
    说"第几轮改的"、`relation_base` 说"改之前长什么样"。那一次改动既然已经退回、"从没发生
    过"，留着任何一栏都会在下一处咬人：留着次数 → 额度白花（模型再也改不动这一场的关系）；
    留着轮次 → 下一次撤回拿它去找一条已经不存在的改动；留着底稿 → 模型再改一次时它顶着
    "第一次"的位子，让新底稿进不来。
    """
    pending = load_pending(lib_dir)
    before = (dict(pending.relation_edits), dict(pending.relation_edit_turns),
              dict(pending.relation_base))
    for name in names:
        pending.relation_edits.pop(name, None)
        pending.relation_edit_turns.pop(name, None)
        pending.relation_base.pop(name, None)
    if (pending.relation_edits, pending.relation_edit_turns,
            pending.relation_base) != before:
        save_pending(lib_dir, pending)


# ------------------------------------------------------- 关系表（特殊分区，§6.2/§6.5） --

@dataclass
class LoadedRelations:
    """`load_relations` 的返回：关系表 + 读盘时**看见的问题**（口径同 `LoadedLibrary`）。

    坏文件意味着"他这一场对所有人都没有关系了"——调用方（工具层）必须能把它说出来，否则
    用户看到的是"角色忽然一个熟人都不认得了"，而程序不吭声。
    """
    table: relations_mod.RelationTable
    warnings: list[str] = field(default_factory=list)


def load_relations(lib_dir: Path) -> LoadedRelations:
    """读一座库/副本的关系表；**绝不抛**（与 `load_library` 同一条容错口径）。

    文件不在（老库、没有关系表的角色）→ 空表、零警告：那是常态，不是错误。
    坏文件（读不出来 / 坏 JSON / 某一行字段非法）→ **空表 + 警告**，绝不半读——半张表被
    当成全部、下一次编辑再写回去，就等于把用户手写的另外几行静默吃掉。`relations` 那边
    严进严出，容错只留在这一层；于是"读不干净的表"永远写不回用户文件（工具会因为
    "表里没有这个人"直接拒绝，见 `knowledgetools._update_relation`）。
    """
    empty = relations_mod.RelationTable()
    try:
        raw = relations_path(lib_dir).read_text(encoding="utf-8")
    except FileNotFoundError:
        return LoadedRelations(table=empty)
    except (OSError, UnicodeDecodeError) as exc:
        return LoadedRelations(table=empty, warnings=[
            f"{RELATIONS_JSON} 读不出来（{exc}），这一场按没有关系表处理。"])
    try:
        return LoadedRelations(table=relations_mod.parse_relations_json(raw))
    except relations_mod.RelationFormatError as exc:
        return LoadedRelations(table=empty, warnings=[
            f"{RELATIONS_JSON} 读不出来：{exc}这一场按没有关系表处理。"])


def save_relations(lib_dir: Path, table: relations_mod.RelationTable) -> Path:
    """原子写 `relations.json`（UTF-8、不转义中文、缩进 2、末尾一个换行），返回写入路径。

    与 `library.json` / `pending.json` 同一条原子写纪律：这张表是**素材**（手写、编辑器、
    git 同步），裸 `open(..., "w")` 在断电时留下半截 JSON，下次加载就是一张读不出来的表。
    """
    target = relations_path(lib_dir)
    _atomic_write_text(target, relations_mod.render_relations_json(table))
    return target


def ensure_relations_copy(src_dir: Path, dst_dir: Path) -> bool:
    """副本里还没有关系表时，把本体那份**拷一份当基线**（§5.1 × §6.2 的特殊分区）。

    三条分支，缺一条都会咬人：

      · 副本已经有一份 → **原样不动**并返回 False（复用、绝不重置：他中途离场又回来，
        这一场改过的关系必须还在）；
      · 源没有 / 源读不干净（有警告或读成空表）→ **一个文件都不建**并返回 False：
        没有关系表的角色磁盘上也与今天逐字节相同；源读不干净时更不能把半张表写进副本
        （工具随后会因为"表里没有这个人"拒绝改动，用户不会丢掉任何东西）；
      · 其余 → 拷过去，返回 True。

    关系表与条目共用同一套副本/结算（§6.5）：它跟着副本一起建、一起被截断、一起被归档。
    """
    src, dst = Path(src_dir), Path(dst_dir)
    if has_relations(dst) or not has_relations(src):
        return False
    loaded = load_relations(src)
    if loaded.warnings or not len(loaded.table):
        return False
    try:
        save_relations(dst, loaded.table)
    except OSError:
        return False                   # 旁挂失败：跟没建一样，工具那边会拒绝改动
    return True


@dataclass
class RelationMergeReport:
    """一次关系合并到底做了什么（**绝不静默**：跳过、挡下、写不动都在这里）。

    · `merged`：真的写进本体表的关系条数（只写**本场真的动过的字段**，见 `_patch_row`）；
    · `skipped`：并下去等于什么都没变、于是没动的条数（"没变"不是问题，但也不能无声无息）；
      两边都动过同一个字段、而本体现在这一行恰好就是结果时也在这里（撞车那句在 warnings 里）；
    · `refused`：**被挡下**的条数（本体里那份与副本开演时不一样 / 本体里已经没有这个名字）
      ——跳过是没事可做，挡下是有事没做成，故它让 `persisted=False`；
    · `persisted`：这次合并完全生效了吗（False = 有该并的没并进去，或者一个字都没落盘）；
    · `warnings`：读盘看见的问题 + 挡下的原因 + 写盘失败……按发生顺序。
    """
    merged: int = 0
    skipped: int = 0
    refused: int = 0
    persisted: bool = True
    warnings: list[str] = field(default_factory=list)


#: 合并的字段粒度：这几个字段各自独立地"本场动过没有"（姓名是键，不参与；`updated_at` /
#: `origin` 是"何时因何而变"，跟着本场那次改动一起走）。中文名只给报告用。
_RELATION_FIELDS: tuple[tuple[str, str], ...] = (
    ("gender", "性别"), ("closeness", "亲密度"), ("description", "关系描述"),
    ("mode", "相处模式"), ("link", "条目链接"))


def _patch_row(current: relations_mod.Relation, base: dict[str, Any] | None,
               row: relations_mod.Relation,
               ) -> tuple[relations_mod.Relation, list[str]]:
    """把本场那一行**真的动过的字段**贴到本体现在这一行上 → `(要写回的行, 撞车的字段名)`。

    三个输入分别是"本体现在这一行"（`current`）、"这一行开演时的底稿"（`base`，可能没有）
    与"本场副本里现在这一行"（`row`）。判据只有一条：**本场动过的才贴**——

      · 本场没动、本体动过（用户在外面订正的描述）→ 一字不改（这是这一层存在的理由）；
      · 本场动过、本体没动 → 贴本场那一版；
      · 两边都动过且不一样 → **以本体为准**并把字段名报出来（与"名字不在账本里"那半条
        同一口径：绝不拿旧账去顶新的那条，但必须说一句，别让本场的改动无声落空）；
      · 两边都动过而且动成了同一个值 → 不算撞车（没有分歧要报）。

    `base` 缺失或读不动（老账本，或那一格被手改坏了）→ **整行以副本为准**（`row`）：与这次
    改动之前的行为一致，而且方向是安全的——那种账本本来就只记"改过"，无从判断动过哪些字段。
    """
    if not isinstance(base, dict):
        return row, []
    try:
        original = relations_mod.Relation.model_validate(base)
    except Exception:
        return row, []
    clashes: list[str] = []
    applied: list[str] = []
    for field, label in _RELATION_FIELDS:
        fresh = getattr(row, field)
        if fresh == getattr(original, field):
            continue                      # 本场没动这一项 → 以本体为准
        if getattr(current, field) != getattr(original, field) and fresh != getattr(current, field):
            clashes.append(label)         # 两边都动了、还动得不一样 → 本体说了算
            continue
        applied.append(field)
    if not applied:
        return current, clashes           # 该并的都与本体重合（或全被撞掉）：没什么可写
    payload = current.model_dump()
    for field in applied:
        payload[field] = getattr(row, field)
    # 「何时、因何而变」（§6.1）跟着本场这一次改动走：写回的那几个字段就是本场写的。
    payload["origin"] = row.origin
    payload["updated_at"] = row.updated_at
    return relations_mod.Relation.model_validate(payload), clashes


def merge_relations(copy_dir: Path, own_dir: Path) -> RelationMergeReport:
    """把副本里**本场改过**的关系并进本体库（§7.2「保留」）。**绝不抛未捕获异常。**

    判据是**本场自己的账本**（`pending.json` 的 `relation_edits`，工具每改一次记一笔），
    与条目那一路同一个口径（`knowledgesettle.collect_gain`）：账本说"改过"才并。差分只用来
    报警——副本里那份开演时的旧样子与本体现在不一样（用户在外面改过、另一场先结算了），
    以本体为准并说一句，绝不拿旧账去顶新的那条。

    关系行**不参与**"新压旧"那一套（§3.5 是条目的语义，标题近似才算同一件事）：一张表里
    姓名就是键，同名即同一行，`RelationTable.upsert` 原位替换。

    并的是**本场真的动过的字段**（`pending.relation_base` 是这一行开演时的底稿）：本场没
    碰的字段一律以本体为准。这一条是手写素材的保命绳——拿副本整行去盖，会把你戏演到一半
    在本体里订正的描述静默退回开演时的旧文（不可逆，而报告还说"合并成功"）。两边都动过
    同一个字段时同样以本体为准，但**说一句**（差分只用来报警）。

    副本没有关系表 / 表是空的 → 空操作，**连一个文件都不建**（无关系角色一切照旧）。
    本体库目录不存在时也只写这一份 `relations.json`：关系表是库里的一处分区，不替本体库
    凭空造出 `library.json` / `entries/`（"没有库"是合法状态，§9.2）。

    **本体那份读不干净时一个字都不写**（`persisted=False`）：`load_relations` 会把读不动的
    表退成空表，拿它去 upsert 再整份写回，等于把用户手写的其余每一行静默吃掉。这与
    `relations.parse_relations_json` 的承诺（"读不干净的表永远不会被写回用户文件"）以及
    `knowledgesettle.merge_gain` 遇到"本体库读不出来"时的处置是同一条。
    """
    report = RelationMergeReport()
    copy_path, own_path = Path(copy_dir), Path(own_dir)
    if not has_relations(copy_path):
        return report
    loaded = load_relations(copy_path)
    report.warnings.extend(loaded.warnings)
    if not len(loaded.table):
        return report
    pending = load_pending(copy_path)
    ledger = set(pending.relation_edits)
    own_loaded = load_relations(own_path)
    report.warnings.extend(f"本体库：{line}" for line in own_loaded.warnings)
    if own_loaded.warnings:
        report.warnings.append(
            "本体库的关系表读不出来，这一场的关系一条都没有写回：先把那份文件修好"
            "（或者从 git 里找回上一版）再结这一场，本场改动仍留在本场副本里。")
        report.persisted = False
        return report
    own = own_loaded.table
    try:
        for row in loaded.table:
            current = own.get(row.name)
            if current is None:
                if row.name not in ledger:
                    # 开演前就在副本里、本场没动过，而本体里已经没有这个名字了（用户删了、
                     # 改了名、或者那份文件读不出来）：不如实说出来，用户只会发现"我删掉的
                    # 那个人怎么又回来了"。
                    report.refused += 1
                    report.warnings.append(
                        f"「{row.name}」副本里这份是开演前就在的、本场没动过，而本体库里现在"
                        f"没有这一条了（你删掉了它、改了名，或者那个文件读不出来）；"
                        f"以本体为准，没有并回去。")
                    continue
                own.upsert(row)
                report.merged += 1
                continue
            if current == row:
                report.skipped += 1
                continue
            if row.name not in ledger:
                report.refused += 1
                report.warnings.append(
                    f"「{row.name}」本场没有记录改过它，而本体库里那份与副本开进来时不一样"
                    f"（可能你在外面改过它、或另一场先结算了，也可能本场的改动记录丢了）；"
                    f"以本体为准，没有并过去。")
                continue
            patched, clashes = _patch_row(current, pending.relation_base.get(row.name), row)
            if clashes:
                report.warnings.append(
                    f"「{row.name}」本场改过的 {'、'.join(clashes)} 与你在外面改的撞上了："
                    f"以本体为准（本场那一处改动没有并进去，仍留在本场副本里）。")
            if patched == current:
                report.skipped += 1          # 该并的那几项与外面对上了（或全被撞掉）：没什么可写
                continue
            own.upsert(patched)
            report.merged += 1
    except Exception as exc:                   # 最后一张网：一条坏的不连坐整场
        report.warnings.append(f"合并关系表时出了意外（{exc}），本体库没有被改动。")
        report.persisted = False
        return report
    if report.merged:
        try:
            save_relations(own_path, own)
        except Exception as exc:
            return RelationMergeReport(
                refused=report.refused,
                persisted=False,
                warnings=[*report.warnings,
                          f"关系表没能写进本体库（{exc}）：一个字都没落盘，"
                          f"这一场改过的关系仍留在本场副本里。"])
    report.persisted = not report.refused
    if report.refused:
        report.warnings.append(
            f"有 {report.refused} 条关系没能并进本体库（上面逐条说了原因）："
            f"它们仍留在本场副本里。")
    return report


def settle_relations(copy_dir: Path, own_dir: Path, *, keep: bool) -> RelationMergeReport:
    """按用户的选择结算这一场的**关系改动**（§6.5 × §7.2）：保留 → 并进本体；丢弃 → 什么都不并。

    与 `knowledgesettle.settle` 同一个决定、同一个时刻（引擎的 `apply_settlement` 一次调
    两者）：关系表复用信息库的散场结算，不另开一套开关。丢弃那一路**一个字节都不写**——
    副本连同这一场的 `runs/` 存档一起留着（§7.2），你事后翻得到他当时把关系改成了什么，
    只是没进本体。

    已知边界（明写在这里，免得被当成"忘了"）：这一场**只**改了关系、没记下任何条目时，
    待决清单里不会出现这个人（清单由条目的所得算出来，§7.2），于是他这一场的关系改动要等到
    下一次真正结账时才跟着并进去。改动**不会丢**：未结算的副本在角色再进场时照旧复用
    （§5.1 不轮换），那一笔一直躺在副本的账本里。
    """
    if not keep:
        return RelationMergeReport()          # 丢弃：什么都不并，一个字节都不写
    return merge_relations(copy_dir, own_dir)


@dataclass
class RelationRollbackReport:
    """一次"关系改动按轮次回退"做了什么（§5.3 × §6.2）。**绝不静默**。

    · `restored`：退回开演时样子的关系行数；
    · `warnings`：退回**没做成**的那些（原文找不回来、写盘失败……）——用户必须看得见的
      那一类：副本里继续现行着一条依据已被撤回的改动，而界面上什么都没发生。
    """
    restored: int = 0
    warnings: list[str] = field(default_factory=list)


def truncate_relations_after_turn(copy_dir: Path, own_dir: Path | None,
                                  cut: int) -> RelationRollbackReport:
    """把本场**晚于 cut** 改过的关系行退回开演时的样子（§5.3 × §6.2）。**绝不抛。**

    关系小节是**恒在上下文**的回喂源（§6.2：每一块 think 与 speak 的提示词里都有它），
    比信息库索引更该跟着撤回一起退：不退的话，角色会带着"刚表白成功"的记忆接着开口，
    而用户已经明确把那一段抹掉了；散场选「保留」还会把这段被撤掉的关系永久并进本体。

    判据是**轮次**（`pending.relation_edit_turns`，工具每改一次记一笔），与条目那一路的
    `revised_turns` 同一口径：轮次缺失（老账本）→ **保守不回退**，与这次改动之前的行为
    一致。原文取**底稿**（`pending.relation_base`：工具改它之前那一行）；底稿缺失时回落到
    本体那一行（§5.2 保证本体整场只读，它是一份可靠原文来源）；两者都没有 → **一个字都
    不动**、把"没能退回"写进 `warnings`、账本留着（它仍是一处本场改动，散场清单上要看得见）。

    退回成功的那几条同时从账本三栏里摘掉（`_forget_relation_edits`）：那一次改动从没发生
    过，§6.3 的**本场额度**必须一并退回——否则用户撤回之后，模型再也纠正不了这一场的关系
    （额度已经被一次作废的改动花掉了）。

    副本没有关系表、`cut` 读不出来、账本里没有晚于 cut 的改动 → 空操作（一个字节都不写）。
    """
    report = RelationRollbackReport()
    copy_dir = Path(copy_dir)
    value = _as_int(cut)
    if value is None or not has_relations(copy_dir):
        return report
    pending = load_pending(copy_dir)
    doomed = [name for name, turn in pending.relation_edit_turns.items() if turn > value]
    if not doomed:
        return report
    table = load_relations(copy_dir).table
    own = load_relations(own_dir) if own_dir is not None else None
    rolled: list[str] = []
    for name in doomed:
        original = _relation_baseline(name, pending.relation_base.get(name), own)
        if original is None:
            report.warnings.append(
                f"「{name}」本场改过关系，而开演时那一行找不回来了（本体那份读不出来、或"
                f"那一行已经不在了）：它没有退回去，仍留在本场副本里、也仍在散场清单上。")
            continue
        current = table.get(name)
        if current is None or current != original:
            table.upsert(original)
        rolled.append(name)
    if not rolled:
        return report
    try:
        save_relations(copy_dir, table)
    except OSError as exc:
        report.warnings.append(
            f"退回关系改动没能写回本场副本（{exc}）：这几行仍是本场改后的样子。")
        return report
    try:
        _forget_relation_edits(copy_dir, rolled)
    except OSError as exc:
        # 账本销不掉 → 额度没退回来、散场还会把这几行当"本场改动"（值已经退回开演时的样子，
        # 合并是个空操作）。这不是撤回失败，但必须说出来：用户以为额度回来了，模型却撞墙。
        report.warnings.append(
            f"退回关系改动之后没能销掉本场账本（{exc}）：这一场的关系额度没有退回。")
    report.restored = len(rolled)
    return report


def _relation_baseline(name: str, base: dict[str, Any] | None,
                       own: LoadedRelations | None) -> relations_mod.Relation | None:
    """这一行开演时的样子（`name` 那一行）：先用底稿，其次本体那一行，都没有 → None。

    底稿优先是硬的：本体那一行可能在戏演到一半时被用户改过，拿它当原文抄回副本，等于这次
    撤回顺手改掉了用户刚做的订正。本体那份**读不出来**（有警告）时同样当作没有原文——
    一张半读的表当成全部，比"没能退回"坏得多（那正是 `load_relations` 的容错口径要防的）。
    """
    if isinstance(base, dict):
        try:
            return relations_mod.Relation.model_validate(base)
        except Exception:
            pass                          # 坏底稿 → 回落到本体那一行
    if own is not None and not own.warnings:
        return own.table.get(name)
    return None


# ------------------------------------------------------------ knowledge_seed 播种 --

class SeedResult(BaseModel):
    """`seed_from_card` 的结果：是否真的建了库、写了几条、跳过了什么。"""
    model_config = ConfigDict(extra="forbid")

    created: bool = False
    count: int = 0
    warnings: list[str] = Field(default_factory=list)


def _seed_title(line: str) -> str:
    """一行 seed 文本 → 条目标题（"知道：X" 取 X，否则整行）；空行 → 空串。

    前缀是给写卡的人看的语法，不是标题的一部分；两种冒号都认，免得半角写完发现
    标题里带上了"知道:"。
    """
    text = (line or "").strip()
    for prefix in _KNOW_PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix):].strip()
    return text


#: 种子行的文本取不出合法键时，键里的非法字符换成的占位符（**只改 key，title 逐字保留**）。
_SEED_KEY_PLACEHOLDER = "_"

#: 改造后仍取不出合法键时的兜底键前缀（`种子-3` 的 `3` 是该卡内的行序）。
_SEED_KEY_FALLBACK_PREFIX = "种子-"


def _sanitize_seed_key(title: str) -> str:
    """一行种子文本 → 尽力改造成能当文件名的键（**只动键，标题一字不动**）。

    键会成为文件名（§4.3），可用户写在卡上的是**内容**：半角引号、斜杠在中文写作里极
    常见（真实卡上就有），不能让"守门过不去"变成"这行知识丢掉"。故把非法字符换成
    占位符，再收拾掉另外几个踩守门的形态（前导/尾随点、`..`、超长）。

    返回空串 = 什么都不剩（整行都是非法字符）；调用方退到 `种子-<序号>`，绝不静默丢行。
    """
    chars = [_SEED_KEY_PLACEHOLDER
             if (ch in _ILLEGAL_NAME_CHARS or ch in "/\\"
                 or ord(ch) < 32 or ord(ch) == 127)
             else ch
             for ch in title]
    key = "".join(chars).strip()
    key = key.strip(".")                        # 前导点会变隐藏文件、尾随点 Windows 不认
    key = key.replace("..", ".")                # `..` 是路径回溯，守门也拦
    if len(key) > MAX_KEY_LENGTH:
        key = key[:MAX_KEY_LENGTH].strip().strip(".")
    return key


def _seed_key(title: str, seq: int) -> str:
    """一行种子的**标题** → 能当文件名的**键**（§9.1：内容一条都不能丢，键必须安全）。

    三档，够用即止：
      1. 标题本身过得了 `entry_key_error` → 就用它（绝大多数情况；与用户手写条目同口径）；
      2. 过不了 → 只把非法字符换掉（`_sanitize_seed_key`）；
      3. 换成什么都不合法（空 / 保留设备名 / 太长）→ 退化成 `种子-<行序>`。

    返回空串表示这一行实在取不出键——调用方按"跳过并警告"处理。这条分支**不该发生**
    （兜底键总能给出来），留着只是为了不静默造出一个非法键。
    """
    if not entry_key_error(title):
        return title
    cleaned = _sanitize_seed_key(title)
    if cleaned and not entry_key_error(cleaned):
        return cleaned
    fallback = f"{_SEED_KEY_FALLBACK_PREFIX}{seq}"
    return "" if entry_key_error(fallback) else fallback


def _unique_seed_key(base: str, taken: Sequence[str]) -> str:
    """`base` 与已用键落到**同一个条目文件**时让位加后缀（`-2`、`-3`…），两条都活下来。

    播种是**批量**动作：`Library.upsert` 对"只有大小写不同"的键当场抛（§4.3 的精神），
    可异常从这里逃出去，代价不是"少一条"而是"这张卡的迁移全废、且每次重试都在同一处
    再炸"——永久性全丢。故播种自己让开：`ABC` 与 `abc` 在 Windows 上是同一个文件名，
    第二条改叫 `abc-2`。触发条件（缩写与同词小写并存）在老卡上并不罕见。
    """
    def _clash(key: str) -> bool:
        return key in taken or any(entry_key_collision_error(key, k) for k in taken)

    if not _clash(base):
        return base
    n = 2
    while True:
        suffix = f"-{n}"
        head = base[:max(1, MAX_KEY_LENGTH - len(suffix))]
        cand = f"{head}{suffix}"
        if not entry_key_error(cand) and not _clash(cand):
            return cand
        n += 1


def seed_from_card(card: Any, dst_dir: Path) -> SeedResult:
    """角色库**首次创建**时，把卡上的 `knowledge_seed` 拆成条目写进新库（§9.1 第二步）。

    **库已存在则整段跳过**（幂等）：卡上的 seed 一直留着也无害，第二次不再播种——
    覆盖等于把角色这一路长出来的见识抹回出厂状态。这也是"读时迁移不写盘"能成立的
    前提（迁移只在卡上搬数据，真正播种是这一次显式、可测、有日志的动作）。

    拆行规则（§9.1）：空行忽略；"知道：X"取 X 作标题；兜底句（`KNOWLEDGE_FALLBACK`，
    那句没有任何信息量的占位）**丢弃不进库**——进库只会在索引里占一行噪声。

    **内容一条都不能丢**：标题成了文件名（键）时可能过不了守门（半角引号 / 斜杠 / `..` /
    超长 / 保留设备名），那也不能丢行——标题逐字保留进 `title`，键由 `_seed_key` 改造
    （或退到 `种子-<序号>`），并写一条 `warnings` 说明，绝不静默改名。跳过的只有
    "内容上没有信息量"的行（空行、兜底句）与完全重复的行。

    `card` 是**鸭子类型**：只要求有 `knowledge_seed` 属性（`CharacterCard` 上有，老卡的
    `knowledge_boundary` 由 `schemas.py` 的读时迁移搬进它）。卡上没有这个属性、或种子行
    全被丢弃 → **不建库**："没有库"是合法状态（§9.2 提示词那一节渲染成空串），凭空造一座
    空库只会让 `app/libraries/` 里多出打不开的空壳。
    """
    dst = Path(dst_dir)
    if has_library(dst) or entries_dir(dst).is_dir():
        return SeedResult(created=False, count=0)

    name, scope, owner = _infer_meta(dst)
    lib = Library(name=name, scope=scope, owner=owner)
    warnings: list[str] = []
    seed = getattr(card, "knowledge_seed", None) or []
    seen_titles: set[str] = set()      # 完全相同的**行**只收一条（内容已被那条保住）
    taken: list[str] = []              # 已用掉的键（含大小写比较，见 _unique_seed_key）
    seq = 0
    for raw in seed:
        # 一个元素可能是多行文本（老字段是 list[str]，但每行拆成一条是这里的口径）。
        for line in str(raw).splitlines():
            seq += 1
            title = _seed_title(line)
            if not title or title == KNOWLEDGE_FALLBACK:
                continue
            if title in seen_titles:
                warnings.append(f"种子行「{title}」重复，只保留第一次。")
                continue
            seen_titles.add(title)
            base = _seed_key(title, seq)
            if not base:
                warnings.append(f"种子行「{title}」取不出能当文件名的键，已跳过。")
                continue
            key = _unique_seed_key(base, taken)
            if base != title:
                warnings.append(f"种子行「{title}」含文件名非法字符，改以键「{key}」收录"
                                f"（内容原样保留）。")
            elif key != base:
                warnings.append(f"种子行「{title}」的键与已收录条目只有大小写之差（同一个"
                                f"文件），改以键「{key}」收录（内容原样保留）。")
            taken.append(key)
            lib.upsert(Entry(key=key, title=title,
                             origin=EntryOrigin(kind="manual", turn=BASELINE_TURN)))
    if not lib.entries:
        return SeedResult(created=False, count=0, warnings=warnings)
    save_library(lib, dst)
    return SeedResult(created=True, count=len(lib.entries), warnings=warnings)


def seed_character_library(card: Any, *, root: Path) -> SeedResult:
    """把一张卡上的 `knowledge_seed` 播进**它在 `root` 下的角色库**（§9.1 第二步）。

    入口（GUI 的 `worker`、CLI 的 `runner`）只管"把这张卡播一下"，**路径换算是这里的事**
    ——`characters/<角色名>` 若由各入口自己拼，迟早有一处拼成别的 scope，那等于把一个角色
    的私人见识写进别人看得见的地方。角色名取自卡自己（`card.name`），与引擎算本体库位置
    走的是同一个 `library_dir("character", 名, root=…)`。

    幂等（库已存在即整段跳过）与"没种子就不建库"都由 `seed_from_card` 保证，见那里。

    角色名要过 `entry_key_error`（同一套文件名守门）：它会是 `characters/<名>/` 这一层的
    **目录名**，含路径分隔符或 `..` 的名字会把库写到根之外去（"绝不让名字把文件写到库目录
    之外"——与编辑器保存卡时那条守门同一个理由）。不安全的**不猜、不改**，直接跳过并
    报告：卡上的种子一字不动地留着，名字改对了下次开场照常播。
    """
    name = str(getattr(card, "name", "") or "")
    err = entry_key_error(name)
    if err:
        return SeedResult(created=False, count=0, warnings=[
            f"角色名「{name}」不能当库目录名，已跳过播种（把卡上的 name 改安全再开一场）：{err}"])
    return seed_from_card(card, library_dir("character", name, root=Path(root)))


# -------------------------------------------------------- 订阅 / 固化 / 索引 --

def subscriptions_path(lib_dir: Path) -> Path:
    """库目录 → 它的订阅文件路径（§8.2：与 `library.json` / `entries/` 同目录）。

    订阅是"这个角色订了什么"，属于库不属某张卡——库搬到哪它跟到哪（打包方案实施时随用户
    数据一起迁，天然受益）。文件里存**相对库根**的路径（`wide/庆国世界观`），不存绝对
    路径：库根在开发态与打包态不同，绝对路径一搬就全断。
    """
    return Path(lib_dir) / SUBSCRIPTIONS_JSON


def _safe_rel(raw: Any) -> str:
    """读盘来的路径串 → 干净的相对路径；读不出来 / 不合规 → 空串（当作坏项丢掉）。

    分成两类处理，两类缺一类这道守门就形同虚设：

      · **规范化掉前导与结尾的 `/`**（`/etc` → `etc`）：盘上写来的是"相对库根"的路径，
        多一个前导斜杠只是写法毛糙，落进 `Path(root) / rel` 仍解在库根里面，不必丢——
        丢了等于让用户手改一行格式就整条订阅失效；
      · **丢掉不合规的**，两类：
          - 带 `..` 回溯段的（`wide/../../秘密`）——`Path(root) / rel` 会解到库根外；
          - 带 `:` 的（`C:/Windows` 这类盘符绝对路径，以及 NTFS 的备用数据流写法）。
            这一类最阴：那时 `Path(root) / rel` 会把左边的 root 整个丢掉（pathlib 认
            右操作数带盘符时它就是"绝对"的），于是"相对库根"这条底线被绕过——守门函数
            存在的全部理由就是不让人去读库根外的目录。库名/键都过 Windows 非法字符表
            （`:` 在其列），故正常数据里绝不会冒号。

    这两道只认**写法**，认不出"写法像相对、解出来却在外面"的形态（软链接、Windows 的
    8.3 短名之类）；那是 `_inside_root` 的活——它问结果。两道合起来才是完整的闸。
    """
    if not isinstance(raw, str):
        return ""
    rel = raw.strip().replace("\\", "/").strip("/")
    if not rel or ":" in rel:
        return ""
    if any(part in ("", "..") for part in rel.split("/")):
        return ""
    return rel


def _inside_root(target: Path, base: Path) -> bool:
    """`target` 解出来是不是真的落在库根 `base` 里面——读订阅项的最后一道闸。

    `_safe_rel` 只认路径**写法**，认不出"写法像相对、解出来却在外面"的形态；这道闸直接
    问结果（能解成相对路径吗），与 `_rel_of`（界面侧的写侧换算）判据同一套口径、方向相反。
    解不动的（路径非法 / 盘符不存在）一律当"在外面"——宁可少一条订阅，也不能让引擎去读
    库根外的目录（或把它固化进用户库里）。
    """
    try:
        Path(target).resolve().relative_to(base)
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def load_subscriptions(lib_dir: Path, *, root: Path | str) -> list[tuple[str, Path]]:
    """读 `<库目录>/subscriptions.json` → `[(库名, 源库目录), …]`（§8.2 / §3.4）。

    返回形状恰是 `render_library_index(..., subscriptions=…)` 收的那一种：**读侧只有这一处
    口径**——`gui/knowledge_editor.load_subscriptions` 转调本函数（界面上勾的订阅，引擎下一
    块就看得见），两处不可能漂开。

    宽容读侧是硬要求（与 `load_pending` 同口径，**绝不抛**）：文件缺失（老库、从没订阅过的
    角色）是常态 → 空表；坏 JSON / 顶层不是对象 / `libraries` 不是数组 → 空表；坏项（不是
    对象、path 不是字符串、带 `..`、绝对路径、盘符路径、解出来逃出库根、重复的同一座库）
    **只丢它自己**——一个手改坏的项不该让其余订阅一起失效；而读库根目录之外的东西是安全
    边界，宁可少一条订阅也不能去读它（`_safe_rel` 认写法，`_inside_root` 认结果，两道都要）。
    名字缺失/空白时用库目录名兜底（一条无名订阅在界面上没法勾掉）。
    """
    try:
        raw = subscriptions_path(lib_dir).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    items = data.get("libraries")
    if not isinstance(items, list):
        return []
    base = Path(root)
    try:
        base_real = base.resolve()
    except OSError:                     # 根路径本身解不动：退回原样（下面那闸照样会拦）
        base_real = base
    out: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        rel = _safe_rel(item.get("path"))
        if not rel or rel in seen or not _inside_root(base / rel, base_real):
            continue
        seen.add(rel)
        name = item.get("name")
        name = name.strip() if isinstance(name, str) and name.strip() else \
            rel.split("/")[-1]
        out.append((name, base / rel))
    return out


def subscribed_rows(
    subscriptions: Sequence[tuple[str, Path]],
) -> list[tuple[str, list[Entry]]]:
    """订阅的**活引用**：每个 (库名, 源库目录) 现去读一次源库（§3.4）。

    "现去取"就是活引用的全部实现——不缓存、不拷贝，源库一改，下一次渲染立刻是新内容。
    世界观设定本就该全员同步，不该有人拿着过期版本。坏条目文件照旧只跳过并报告，
    但这里是渲染路径，warnings 不往上带（要诊断请直接用 `load_library`）。
    """
    return [(name, load_library(path).library.ordered_entries())
            for name, path in subscriptions]


def visible_borrowed(own: Sequence[Entry], rows: Sequence[Entry]) -> list[Entry]:
    """借入条目里**还要列出来**的那些：键没被自有条目占住的（§3.4 固化的落点）。

    "固化"是**单条**的：把借来的那一条拷成自己的一条、断开引用。而订阅挂在**整座库**
    上（`subscriptions.json` 一个库一项），界面上没有"只退订这一条"的动作——所以断引用
    只能靠这里：自有库里已经有同键的一条，借入的同键条目就不再列出。

    判据用**键**而不是内容：§3.5 说同键即同一件事（"新压旧"用的也是它）。同一件事在
    索引里列两行——一行是我固化的旧口径、一行是源库的新口径——角色读到的就是两条
    自相矛盾的说法，而索引是"我现在知道什么"的入口。以自有那条为准，与 `_pool` 的
    "同名时以我自己的那一条为准"是同一个规矩。

    空 `own`（没建库/读不出来）或空 `rows`（没订阅）时原样返回：不订阅的角色与
    今天**逐字节相同**——这条只在"自有与借入撞了键"时才动一行。
    """
    keys = {e.key for e in own}
    return [e for e in rows if e.key not in keys] if keys else list(rows)


def render_library_index(
    lib_dir: Path,
    *,
    subscriptions: Sequence[tuple[str, Path]] = (),
    max_entries: int | None = None,
    include_outdated: bool = False,
) -> str:
    """读出"自有 + 借入"的索引数据并渲染成提示词小节（§6.1）。

    渲染本身一律交给 `knowledge.render_index`——**空库渲染成空串**那条硬约束
    （无库角色系统提示逐字节不变）只有一个落点，本模块绝不自己拼索引文本。

    借入的那几组先过 `visible_borrowed`：固化成自有条目的那一条不再从源库借一遍
    （§3.4 的"断开引用"就落在这里）。整组都被自有压住时 `render_index` 不留空组头。
    """
    entries = load_library(lib_dir).library.ordered_entries()
    subscribed = [(name, visible_borrowed(entries, rows))
                  for name, rows in subscribed_rows(subscriptions)]
    return render_index(entries, subscribed=subscribed,
                        max_entries=max_entries, include_outdated=include_outdated)


def pin_entry(src_lib_dir: Path, key: str, dst_lib_dir: Path) -> Entry:
    """固化（§3.4）：把源库里的一条条目拷成**本库自有条目**并断开引用，返回这条副本。

    此后源库再改，我这份不动——这正是"我对某一节的个人理解与官方版已经分叉"的用法。
    拷贝时把 `origin.turn` 换成哨兵：它无论如何都不是本场产物，绝不能参与撤回截断。

    `origin.kind` 与 `scene`/`at` **原样保留**：断引用是结构上的事实（它现在住在我的
    `entries/` 里，不再来自订阅），不需要靠改 kind 来表达；留着出处才有"这条哪来的"
    可追。源库里没有这个键 → `KeyError`（静默返回 None 会让调用方以为固化成功了）。

    "断开引用"的另一半（索引里不再列借来的那一条）不在这里、也不在订阅文件里：它由
    `visible_borrowed` 按**键**判定——自有库里有了同键条目，借入的那条自然不再列。
    故固化**不需要**动订阅（整座库还订着，同库其余条目照旧借）。
    """
    entry = load_library(src_lib_dir).library.entries.get(key)
    if entry is None:
        raise KeyError(f"源库里没有条目「{key}」。")
    pinned = entry.model_copy(
        update={"origin": entry.origin.model_copy(update={"turn": BASELINE_TURN})})
    lib = load_library(dst_lib_dir).library
    lib.upsert(pinned)
    save_library(lib, dst_lib_dir)
    return pinned
