"""信息库的**散场结算内核**（设计文档 §7.2 纯总开关 / §7.3 离场挂起 / §3.5 冲突与归档 / §7.5 幂等）。

它做的是"本场副本 → 角色本体库"的**一次合并**：纯逻辑 + 文件读写。不含模型调用、不弹窗、
不认识 Qt——散场总结那一次模型调用与"问用户保留还是丢弃"都是引擎的事（§7.1 / §7.4：
引擎只准备，界面才决定），这里只接"用户的决定"这个结果。

推荐的调用顺序（§7.1 + §7.2，写给接线的引擎）：

1. 散场 → `collect_gain(copy_dir, own_dir)` 先收一次所得，拿 `digest()` 去写那条枢纽总结；
2. 把总结条目**写进副本**（走 `knowledgetools` 的 `remember`，它自然带上 `origin.kind="scene"`）；
3. **再收一次**所得（这一次把总结条目也算进 added）→ 问用户保留还是丢弃 → `settle(...)`。

第 3 步再收一次是必须的：总结条目也是"本场所得"的一部分（§7.1 说它要"存入索引表"），
而它是在第 1 步之后才长出来的。

五条硬约束，改动前先读：

1. **旧条一字不删**（§3.5）。任何被顶掉、被压下的条目都必须还在库文件里。近似的两条天然是
   两个文件，标个过时就行；**同键**却只有一个文件，直接覆盖等于把本体里那条旧正文永久抹掉
   ——"旧条一字不删"就成了空话。所以同键冲突一律先**归档**成一条派生键的过时条目
   （`<键>（旧）`，重名则 `（旧2）`…），内容逐字保留。派生键放不下（键太长）时**宁可不并**
   也不覆盖：少并一条看得见（报告里有警告），旧正文没了看不见。

2. **"本场动过哪条基线"以本场自己的账本为准**（§5.3.1）。`pending.json` 的 `revised` /
   `revised_turns` 记着"哪条基线条目在本场第几轮被改过"——工具层每写一条都记一笔。本场对
   基线条目的 revise 不改它的 `origin`（哨兵轮次要留着，见 `knowledgestore` 模块 docstring
   第 2 条），所以**内容比对单独当判据会咬人**：本体那条在副本建好之后变过（用户在外面
   改、或另一场先结算了——§7.3 允许随时手动结算），副本里开演前那份旧正文看起来就像
   "本场改过它"，并回去就把本体里更新的那条顶掉了。故分工是：账本判"是不是本场所得"，
   差分只用来报警（"本体与副本不一致，以本体为准，没有并过去"）。
   账本查不到、副本里又**带着基线哨兵**（`BASELINE_TURN`，§5.3：从本体拷来的条目一律盖
   这个标记）的条目一律不是本场所得——包括本体那条已经被删/改名/改坏的情形（见第 5 条）。

3. **幂等**（§7.5）。同一份所得合并两次，第二次不产生重复归档、也不改磁盘上一个字：
   归档键的生成认得"这份旧正文已经归档过了"（`_archive_key` 的复用分支），并下去等于什么
   都没变的条目跳过（`skipped` 计数，"没变"含 `status`/`superseded_by`——见 `_identical`）。
   **重复结算是空操作**，不是报错——报错会把界面上的一次重试变成弹窗事故；但空操作必须
   说话（`repeated=True` + 一句警告），否则用户以为自己又结了一遍。

4. **绝不抛未捕获异常**（§7.4：引擎跑在自己的线程/事件循环里，界面不许因为一次结算崩掉）。
   写不动、跳过、被挡下、异常全部进报告；**有该并的东西没并进去**（一条都没并成，或者
   只有一部分并成了）时 `persisted=False`——那是调用方判断"这次结算生效了吗"的唯一依据
   （`settle` 据此**不**标记已结算，让用户处理完还能重试）。被挡下的条数在 `refused` 里
   单列：跳过（内容一样）不是问题，挡下（并不动）是。

5. **本体库读不完整的部位绝不动**。键不在主体的内存索引里有两种可能，而它们必须分开对待：
   本体真的没有这条（本场新建，写进去就是新建），或者**那儿有一份文件却没读进来**
   （手写的字段名/格式有问题，`load_library` 跳过它并给警告）。后者的内容从没进过内存——
   写下去就是拿一份连归档都取不到原文的东西盖掉用户手写的正文。所以：本体库的读警告一律
   带进报告；某个键的位置上躺着一份读不出来的文件时，这一条**不并**（`refused` + 警告），
   宁可不并也不覆盖，与第 1 条同一个方向。

还有一件不可逆的事绝不能做：本体库里若已经住着一对**只差大小写**的键（手写 `z.md` 里声明
键 `k` 这类合法输入，`load_library` 读得出来），保存会把 `K.md` / `k.md` 当成同一个文件、
把其中一条的正文静默覆盖掉。那是存储层留下的坏状态，本模块修不了它，但**绝不做那个覆盖
动作**：这一整场都不并，把两个键报出来让用户先清理（`_case_clash_in`）。本场所得里单独一条
的键撞上（`K` 已在库里、本场记了 `k`）只挡那一条，其余照常并。

`pending.json` 的 `settled` / `outcome` 是结算状态的唯一落点（`knowledgestore.mark_settled`
先到先得）：一次手滑的重复点击绝不能把"丢弃"翻成"保留"，那会让角色凭空多出一批你从没同意
永久化的记忆，而且不可逆。`pending_gain` 是这条状态的**只读**查询，供引擎/界面在角色离场后
提示"待结算"（§7.3）——它不改任何文件（面板每刷新一次就调它一次，写盘等于把提示变成动作）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from . import knowledgestore as store
from .knowledge import Entry, entry_key_collision_error, entry_key_error, same_subject

#: 同键冲突时旧条归档用的派生键后缀（§3.5 明写：`<键>（旧）`，重名则 `（旧2）`…）。
ARCHIVE_SUFFIX = "（旧）"
_ARCHIVE_SUFFIX_FMT = "（旧{n}）"


# ------------------------------------------------------------------ 返回结构 --

@dataclass
class SceneGain:
    """一次「本场所得」（§7.2）：要并入本体的两半 + 只供查看的那一半。

    · `added`：本场新建的（账本记着是本场记下的，或副本里那条根本没带基线哨兵）；
    · `revised`：本场改动过的既有条目（账本记着本场改过它）；
    · `untouched`：本场没动过的——**不进所得**，它本来就是本体的东西。留着是为了让界面能把
      "他这一场得了什么"与"他本来就有什么"摆在一起看。本体与副本不一致时以本体为准，那些
      条目也落在这里（"没并过去"的原因在 `warnings` 里，绝不静默）。
    · `order`：本场所得的键，按**最后一次写入的轮次**排出的先后（`merge_gain` 就照它并，
      §3.5 的"新压旧"必须重现副本里当场定的胜负，见 `_rows_of`）。手搓的 SceneGain 可以
      不给，那时退回"先新增、后修订"。
    · `present` / `scene` / `character`：本场元信息（调用方给，缺省从副本路径推断），散场
      总结与"待结算"提示都要用。

    本场什么都没得到时是一个**空 SceneGain，不是 None**：调用方不必到处判 None，
    "没有所得"与"还没收过"是两件事。
    """
    scene: str = ""
    character: str = ""
    present: list[str] = field(default_factory=list)
    added: list[Entry] = field(default_factory=list)
    revised: list[Entry] = field(default_factory=list)
    untouched: list[Entry] = field(default_factory=list)
    order: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """这一场有没有"要并进本体"的东西（只看 added/revised）。"""
        return not (self.added or self.revised)

    def keys(self) -> list[str]:
        """本场所得的键（先新增后修订）——供"待结算"提示与总结条目的双链收尾用。"""
        return [e.key for e in self.added] + [e.key for e in self.revised]

    def digest(self) -> list[tuple[str, str, str]]:
        """「键 + 标题 + 一行摘要」（§7.1）：散场总结提示词的条目清单。"""
        return [(e.key, e.title or e.key, e.summary)
                for e in [*self.added, *self.revised]]


@dataclass
class MergeReport:
    """一次 `merge_gain` 到底做了什么（**绝不静默**：跳过、挡下、写不动、异常都在这里）。

    · `added` / `revised`：真正写进本体库的新增 / 覆盖条数（按所得里的分类计）；
    · `archived`：为同键冲突归档出来的派生条目数；
    · `pressed`：被标成过时的近似标题旧条数（§3.5 第二级，只标不删）；
    · `skipped`：并下去等于什么都没变、于是没动的条数。**跳过不是问题**（它就是"并过了"），
      所以进计数不进 `warnings`；但也不能无声无息——它是"这次几乎什么都没干"的唯一解释。
    · `refused`：**被挡下**、没能并进去的条数（键不合法 / 只差大小写的碰撞 / 本体里那个位置
      躺着一份读不出来的文件 / 归档键放不下）。与"跳过"不是一回事：跳过是没事可做，挡下是
      有事没做成——所以它让 `persisted=False`，并在 `warnings` 里逐条说清；
    · `persisted`：这次结算**完全生效了吗**。False = 有该并的东西没并进去（或者一个字都没
      落盘），调用方（`settle`）别当作结清了——留着待结算让用户处理完能重试；
    · `warnings`：读库时看见的问题 + 挡下的原因 + 写盘失败……按发生顺序。
    """
    added: int = 0
    revised: int = 0
    archived: int = 0
    pressed: int = 0
    skipped: int = 0
    refused: int = 0
    persisted: bool = True
    warnings: list[str] = field(default_factory=list)


@dataclass
class DiscardReport:
    """一次 `discard_gain` 的记录（§7.2：丢弃 = 什么都不并，副本连同这一场的 `runs/` 存档
    一起留着，**不删**）。

    `dropped` 是"有多少条本来可以并、但没并"——用户看到的清单数字与它对齐。
    """
    scene: str = ""
    character: str = ""
    dropped: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class SettleReport:
    """一次 `settle` 的结果：用户的选择 + 这次执行了什么 + 有没有生效。

    `outcome` 是这一场**实际生效**的结果（"keep" / "discard"）；`merged` 是保留那一路的合并
    报告（丢弃那一路是空报告，条数在 `discarded`）；`repeated=True` 表示这一场之前已经结算过，
    本次是**空操作**（`outcome` 是当初那次的选择，不是这次请求的）——先到先得（§7.5）。
    `warnings` 是这一路攒下的全部问题（含"没落盘所以这次不算数"）。
    """
    outcome: str = ""
    repeated: bool = False
    merged: MergeReport = field(default_factory=MergeReport)
    discarded: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class PendingGain:
    """「这个角色有未结算的本场所得吗」（§7.3 离场挂起的数据侧）。

    `added` / `revised` 是 `pending.json` 里记下的键；`is_pending` 才是那个问题的答案
    （已结算的不算待结算）。也带上 `scene` / `character`——界面要显示"谁、哪一场"。
    **只读**：它会被面板反复调用，写盘等于把提示变成动作。
    """
    scene: str = ""
    character: str = ""
    added: list[str] = field(default_factory=list)
    revised: list[str] = field(default_factory=list)
    settled: bool = False
    outcome: str = ""

    @property
    def is_pending(self) -> bool:
        """还有没结算的本场所得吗（界面据此给"待结算"提示）。"""
        return not self.settled and bool(self.added or self.revised)

    def keys(self) -> list[str]:
        """待结算的键（先新增后修订）。"""
        return [*self.added, *self.revised]


# ------------------------------------------------------------------ 路径推断 --

def _character_of(copy_dir: Path) -> str:
    """副本目录 → 角色名（`runs/<场景>/<角色名>/library`，§4.1）。"""
    try:
        return Path(copy_dir).parent.name
    except Exception:
        return ""


def _scene_of(copy_dir: Path) -> str:
    """副本目录 → 场景名（副本的运行根就是"这一场"，它才是 `runs/` 下的那一层）。"""
    try:
        return Path(copy_dir).parent.parent.name
    except Exception:
        return ""


def _is_baseline(entry: Entry) -> bool:
    """这条条目是不是**开演前就在库里**的那一份（§5.3 的轮次哨兵是它唯一的标记）。

    从本体拷进副本的条目一律带着 `BASELINE_TURN`（`knowledgestore._as_baseline`：只改
    轮次这一处），本场写下的条目轮次 ≥ 0（`remember` 盖当轮、`revise` 给本场条目重盖）。
    于是"这条不是本场产物"在副本里可判——它决定了本体那条被删/改名/改坏之后，副本里这份
    开演前的旧版本**绝不能**被当成"本场新建"并回去（那会覆盖用户手写的正文）。
    """
    return int(entry.origin.turn) == store.BASELINE_TURN


def _last_write_turn(entry: Entry, pending: store.PendingChanges) -> int:
    """这条条目在本场**最后一次被写**是第几轮（合并顺序用它，见 `_rows_of`）。

    本场产物：`origin.turn` 就是写下它的那一轮（`revise` 会给本场条目重盖轮次）。被本场
    改过的基线条目哨兵照旧，它的轮次记在 `pending.revised_turns` 里（§5.3.1 为撤回记的
    那份）。两条都取不到（老 `pending.json` 没这个字段）→ 回落哨兵值（-1），排在最前：
    它先并进本体，后来的说法照旧压得住它，方向与"记不清了"一致。
    """
    turn = pending.revised_turns.get(entry.key)
    if turn is not None:
        return int(turn)
    return int(entry.origin.turn)


def _authored(entry: Entry) -> tuple:
    """这条条目"写了什么"的判据（比对用）。

    刻意**不含** `origin.turn`：副本里的基线条目带着哨兵轮次（§5.3），拿它比会把每一条
    从本体拷来的条目都判成"本场改过"。也不含 `status` / `superseded_by` / `supersedes`：
    那三样是**合并过程自己会动的**（新压旧、归档），拿它们比会让第二次合并以为"内容变了"
    而再动一遍，幂等当场失效。
    """
    return (entry.title, entry.summary, entry.body, entry.subject, entry.indexed,
            entry.origin.kind, entry.origin.scene)


def _identical(current: Entry, entry: Entry) -> bool:
    """这两条是不是**同一份**：写了什么一样（`_authored`），现行状态也一样。

    "跳过"要的是"并下去等于什么都没变"。`_authored` 刻意不看 `status` / `superseded_by`
    （见那里的理由：那两样是合并过程自己会动的），可这一条判据要的就是它俩——本场把一条
    过时的旧认识**翻回现行**（正文与摘要一字未改）时，两份"写了什么"相同而 `status` 不同：
    按"写了什么"跳过，用户的「保留」就被吞了，本体里那条仍是过时（索引默认不列过时条目，
    角色下一场根本看不见它），而且一个数都不报。反过来，第二次合并时这两样都与副本一致，
    跳过照旧成立——幂等不受影响。
    """
    return (_authored(current) == _authored(entry)
            and current.status == entry.status
            and current.superseded_by == entry.superseded_by)


def _read_warning(where: str, line: str) -> str:
    """读库警告前面挂上"是哪个库"（本体与副本各有一批，混在一起谁也判不出该修哪儿）。"""
    return f"{where}：{line}"


# -------------------------------------------------------------------- 收集 --

def collect_gain(copy_dir: Path, own_dir: Path, *, scene: str = "", character: str = "",
                 present: Sequence[str] = ()) -> SceneGain:
    """收一次本场所得（§7.2）：把副本里的条目分成 added / revised / untouched。**绝不抛。**

    判据是**本场自己的账本**（`pending.json` 的 `added` / `revised`，工具层每写一条记一笔），
    不是内容比对：本场对基线条目的 revise 不改 origin（§5.3.1 刻意如此），而内容比对分不清
    "本场改了它"与"本体在副本建好之后变过"（用户在外面改、另一场先结算）——后者照"本场改过"
    并回去，就把本体里更新的正文顶掉了。差分只用来报警：不一致而账本没记 → 以本体为准、
    不并，并在 `warnings` 里说一句（账本丢了、或是本体在外面变了，两种可能都说出来）。

    副本里**带着基线哨兵**（`BASELINE_TURN`）的条目一律不是本场产物（§5.3：从本体拷来的
    一律盖这个标记）：账本没记它、本体里又已经没有这个键时，那是本体那条被删了、改了名、
    或者**那份文件读不出来**——不是"本场新建"，绝不并回去。

    `own_dir` 不存在（角色从没有过库）→ 本体按空库处理，于是账本记下的与本场新长出来的
    都是"本场新建"。本体库读盘看见的问题（坏条目文件、文件名与键不一致……）**带进报告**
    （前缀「本体库」）：那是"她的库少了一条"的唯一提示。

    `present` / `scene` / `character` 是调用方给的元信息；`scene` / `character` 缺省时从
    副本路径推断（`runs/<场景>/<角色名>/library`）。读副本时看见的问题同样进 `warnings`
    ——"他这一场少记了一条"必须看得见。
    """
    copy_path = Path(copy_dir)
    gain = SceneGain(scene=scene or _scene_of(copy_path),
                     character=character or _character_of(copy_path),
                     present=[str(name) for name in present])
    try:
        loaded = store.load_library(copy_path)
        own_loaded = store.load_library(Path(own_dir))
        pending = store.load_pending(copy_path)
    except Exception as exc:                      # 三个 load 都声明"绝不抛"，这里是最后一张网
        gain.warnings.append(f"本场副本或本体库读不出来（{exc}），这次当作没有所得。")
        return gain
    own = own_loaded.library
    gain.warnings.extend(loaded.warnings)
    gain.warnings.extend(_read_warning("本体库", line) for line in own_loaded.warnings)
    recorded_new = set(pending.added)
    recorded_edit = set(pending.revised) | set(pending.revised_turns)
    written: list[tuple[int, str]] = []
    for key in loaded.library.ordered_keys():
        entry = loaded.library.entries[key]
        original = own.entries.get(key)
        if key in recorded_new:
            gain.added.append(entry)
        elif key in recorded_edit:
            gain.revised.append(entry)
        elif original is None and not _is_baseline(entry):
            gain.added.append(entry)
        else:
            gain.untouched.append(entry)
            if original is None:
                # 开演前就在库里、本场一个字没动过，而本体里已经没有这个键了。三种来路
                # （用户删了、改了名、那份文件读不出来）正确的处置是同一种：不并，并且在
                # 报告里说一句——用户才知道自己删掉的那条没有被悄悄搬回来。
                gain.warnings.append(
                    f"「{key}」开演前就在本体库里、本场没动过（副本那份带着基线标记），"
                    f"而本体库现在没有这一条了（你删掉了它、改了名，或者那个文件读不出来）；"
                    f"以本体为准，没有并回去。")
            elif _authored(entry) != _authored(original):
                # 本场没动过它，可本体那条与副本里开演前那份不一样了：本体在外面变过（用户
                # 改、另一场先结算），或者本场的改动记录丢了。两种来路分不开，处置却是同一种
                # ——以本体为准（不拿旧版本顶新的那条），但必须说出来，别让本场的修订无声落空。
                gain.warnings.append(
                    f"「{key}」本场没有记录改过它，而本体库里那份与副本开进来时不一样"
                    f"（可能你在外面改过它、或另一场先结算了，也可能本场的改动记录丢了）；"
                    f"以本体为准，没有并过去。")
            continue                       # 没动过的条目不在所得里，也就不参与合并顺序
        written.append((_last_write_turn(entry, pending), key))
    # 合并顺序 = 最后一次写入的先后（稳定排序：同轮次的按副本里的原顺序）。§3.5 的"新压旧"
    # 在副本里当场发生过一次，合并必须重现那个胜负——见 `_rows_of`。
    written.sort(key=lambda row: row[0])
    gain.order = [key for _, key in written]
    return gain


# -------------------------------------------------------------------- 合并 --

def merge_gain(gain: SceneGain, own_dir: Path) -> MergeReport:
    """把一份所得并进本体库（§7.2「保留」）。**绝不抛未捕获异常**，结果全在报告里。

    新增 → 写进本体库；同键的改写 → 覆盖本体那一条（**覆盖前先归档它**，见模块 docstring
    第 1 条）；标题近似的**其它**条目 → 只标过时、一字不删（§3.5 两级判定，复用
    `knowledge.same_subject`）。空所得是空操作，连空库壳都不建。

    并的顺序按 `SceneGain.order`（本场最后一次写入的先后）——副本里当场定过的胜负，这里
    必须重现（见 `_rows_of`）。本体的读盘警告带进报告（前缀「本体库」）：她的库读不完整时，
    用户得知道这次合并是在什么状态上做的。

    写盘只有一次（全部改完再 `save_library`）：中途出错时本体库一个字都没变，报告里
    `persisted=False` 说得明白。计数报的是**磁盘上真的发生了什么**——写失败时全为零。
    有条目被挡下（`refused`）时同样 `persisted=False`：有该并的东西没并进去，这一场就
    不算结清，用户处理完还能重来（幂等：已经并过的不会重复）。
    """
    report = MergeReport(warnings=list(gain.warnings))
    if gain.is_empty:
        return report                      # 本场什么都没得到：一个字都不写（连库都不建）
    own_path = Path(own_dir)
    try:
        loaded = store.load_library(own_path)
        own = loaded.library
    except Exception as exc:
        report.warnings.append(f"本体库读不出来（{exc}），这次没有合并。")
        report.persisted = False
        return report
    for line in loaded.warnings:           # 本体库读不完整这件事必须看得见（采集时可能还没坏）
        note = _read_warning("本体库", line)
        if note not in report.warnings:
            report.warnings.append(note)

    clash = _case_clash_in(own)
    if clash:
        # 本体库里已经有一对"只差大小写"的键（手写的 `z.md` 里声明了键 `k` 这类合法但危险的
        # 输入）：`entries/K.md` 与 `entries/k.md` 在 Windows 上是同一个文件，**任何一次保存**
        # 都会把其中一条的正文覆盖掉。那是用户手上已有的坏状态，本模块修不了它（修它在存储层），
        # 但绝不做那个覆盖动作——这次一整场都不并，把两个键报出来让用户先清理。
        report.warnings.append(
            f"本体库里有只差大小写的重名条目，这次没有合并（保存会把其中一条覆盖掉，"
            f"那是不可逆的）：{clash}清理后再结算这一场。")
        report.persisted = False
        return report

    counted = {"added": 0, "revised": 0, "archived": 0, "pressed": 0, "skipped": 0,
               "refused": 0}
    added_keys = {e.key for e in gain.added}
    gain_keys = added_keys | {e.key for e in gain.revised}
    try:
        for entry, is_new in _rows_of(gain):
            try:
                _merge_one(own, entry, counted, report.warnings, added=is_new,
                           own_path=own_path, gain_keys=gain_keys)
            except Exception as exc:
                # 一条坏的不连坐全场：本体库里同时存在 `K` 与 `k`（手写的两个文件）时，
                # `Library.upsert` 的小写碰撞守门会当场抛——那是那一条并不进去，不是这一场
                # 所得全废。报出来，其余照常并；它算"被挡下"，这一场因此不算结清。
                counted["refused"] += 1
                report.warnings.append(
                    f"「{entry.key}」没能并进本体库（{exc}），其余条目照常处理。")
    except Exception as exc:                   # 最后一张网：连表都摆不出来（理论上到不了）
        report.warnings.append(f"合并时出了意外（{exc}），本体库没有被改动。")
        report.persisted = False
        return report

    report.refused = counted["refused"]
    if not (counted["added"] or counted["revised"]):
        report.skipped = counted["skipped"]            # 空所得 / 全跳过：一个字都不写
        if counted["refused"]:
            # 一条都没并进去，而且不是因为"内容一样没什么可并的"：用户的「保留」没有兑现。
            # 这里不许装作并完了——`settle` 拿 `persisted` 判要不要标记已结算，标了就等于
            # 把这次选择兑成空话（本体里什么都没多，重试又被先到先得挡住）。
            report.persisted = False
            report.warnings.append(
                f"这一场没有任何一条并进本体库（{counted['refused']} 条被挡下，"
                f"上面逐条说了原因）：这一场仍是待结算状态，处理好之后可以再结一次。")
        return report
    try:
        store.save_library(own, own_path)
    except Exception as exc:
        return MergeReport(
            persisted=False, refused=counted["refused"],
            warnings=[*report.warnings,
                     f"合并结果没能写进本体库（{exc}）：一个字都没落盘，"
                     f"这一场仍是待结算状态，可以再试一次。"])
    report.added = counted["added"]
    report.revised = counted["revised"]
    report.archived = counted["archived"]
    report.pressed = counted["pressed"]
    report.skipped = counted["skipped"]
    report.persisted = not counted["refused"]
    if not report.persisted:
        report.warnings.append(
            f"有 {counted['refused']} 条没能并进去（上面逐条说了原因）：这一场还没结清，"
            f"处理好之后再结一次（已经并过的不会重复，也不会重复归档）。")
    return report


def _rows_of(gain: SceneGain) -> list[tuple[Entry, bool]]:
    """这份所得按什么顺序并，返回 `(条目, 是不是本场新增)`。**绝不抛。**

    顺序主用 `SceneGain.order`（`collect_gain` 按"最后一次写入的轮次"排好的）。为什么非要有
    顺序：§3.5 的"新压旧"在副本里当场发生过一次，而它是**后写的胜**——合并若一律"先新增、
    后修订"，就会把胜负做反：本场先改写了旧认识的正文、之后又补了一条更晚的新说法，合并时
    新说法先并进去、接着被那条被它自己推翻的旧认识反压成过时（用户看到的清单说新说法是
    新增，本体里它却是过时的；角色下一块读到的索引只剩那条旧的）。照副本的真实先后并，
    合并结果就与副本、与用户点头时看到的那份清单一致。

    调用方手搓的 SceneGain 可以不给 `order`：退回"先新增、后修订"，与本模块第一版一致
    （老调用点的行为不变，代价只是上段说的那种顺序反了）。
    """
    added = {e.key: e for e in gain.added}
    revised = {e.key: e for e in gain.revised}
    if not gain.order:
        return [(e, True) for e in gain.added] + [(e, False) for e in gain.revised]
    rows: list[tuple[Entry, bool]] = []
    seen: set[str] = set()
    for key in gain.order:
        if key in seen:
            continue
        seen.add(key)
        if key in added:
            rows.append((added[key], True))
        elif key in revised:
            rows.append((revised[key], False))
    rows += [(e, True) for e in gain.added if e.key not in seen]
    rows += [(e, False) for e in gain.revised if e.key not in seen]
    return rows


def _case_clash_in(own: store.Library) -> str:
    """本体库里有没有"只差大小写"的两个键；有 → 返回那句中文原因，没有 → 空串。

    这类坏状态只可能来自手写（`z.md` 里声明键 `k`，同时库里又有一条 `K`）：`load_library`
    只按文件名扫描、不做碰撞判定，于是它读得出来，却在保存时把两条写成同一个文件
    （`K.md` 与 `k.md` 在 Windows 上是同一个文件）。判据复用 `knowledge.entry_key_collision_error`
    ——同一套规则只有一处定义（§4.3）。
    """
    keys = list(own.entries)
    for position, first in enumerate(keys):
        for second in keys[position + 1:]:
            clash = entry_key_collision_error(first, second)
            if clash:
                return clash
    return ""


def _merge_one(own: store.Library, entry: Entry, counted: dict[str, int],
               warnings: list[str], *, added: bool, own_path: Path,
               gain_keys: set[str]) -> None:
    """并入**一条**所得。抛出的异常由 `merge_gain` 接住（那时本体库还没落盘，等于没动过）。

    凡是"并不进去"的出口都要 `counted["refused"] += 1` 并留一句警告：跳过（内容一样）与
    挡下（有东西没并成）在报告里必须是两件事——前者是没事可做，后者让 `persisted=False`。
    """
    key = entry.key
    err = entry_key_error(key)
    if err:
        counted["refused"] += 1
        warnings.append(f"「{key}」不能作条目名（{err}），这一条没有并进本体库。")
        return
    for other in own.entries:
        # 只有大小写不同的两个键在 Windows 上是同一个文件（§4.3）：这一条并不进去，但绝不
        # 静默改名、更不许顶掉住在那儿的另一条。**先查**再动手——否则归档那一步已经落进内存，
        # 最后卡在 upsert 上会白白多出一条归档副本。
        clash = entry_key_collision_error(key, other)
        if clash:
            counted["refused"] += 1
            warnings.append(f"「{key}」没能并进本体库：{clash}其余条目照常处理。")
            return
    unreadable = _unreadable_at(own_path, own, key)
    if unreadable:
        counted["refused"] += 1
        warnings.append(unreadable)
        return
    current = own.entries.get(key)
    if current is not None and _identical(current, entry):
        # 已经并过了（同一份所得并第二次就是这条路），或并下去等于什么都没变。这里连
        # "翻成 active"都不做：本体里那条后来被别的说法压下去的，重新并一次不该把它救活
        # ——那会让一条已经被推翻的旧认识重新出现在索引里。
        counted["skipped"] += 1
        return

    archive_key = ""
    if current is not None and _needs_archive(current, entry):
        archive_key = _archive_key(own, key, current.body)
        if not archive_key:
            counted["refused"] += 1
            warnings.append(
                f"「{key}」的旧正文归档不出来（派生键「{key}{ARCHIVE_SUFFIX}」不能作条目名，"
                f"多半是这个键本身就快到头了）；为了不把旧正文覆盖掉，这一条没有并进本体库。")
            return
        own.upsert(_archived(current, archive_key, key))
        counted["archived"] += 1

    pressed = _press_others(own, entry)
    counted["pressed"] += len(pressed)
    for pressed_key in pressed:
        if pressed_key in gain_keys:
            # 被压的是**本场所得自己**的一条：结果与副本一致（副本里当场就是这么定的），
            # 但报告里只多一个 pressed 数，用户看不出被压的正是自己刚拿到的东西、从此它
            # 不进索引（§5.3.1 把"永久被挤出索引"列为要防的坏事）。点名说一句，正文没删。
            warnings.append(
                f"「{pressed_key}」是本场记下的，被本场更晚的「{key}」标成了过时"
                f"（副本里当场就是这么定的，正文没删；它仍算本场所得）。")
    # supersedes 一并继承：旧条压过谁，这份新说法接过来（与 knowledgetools 的"新压旧"同一口径），
    # 链条否则会断——日后撤回或再并时没人知道它取代过什么。
    merged = entry.model_copy(update={
        "status": "active",
        "superseded_by": None,
        "supersedes": _dedupe([*(current.supersedes if current is not None else []),
                               *entry.supersedes, *pressed,
                               *([archive_key] if archive_key else [])]),
    })
    own.upsert(merged)
    counted["added" if added else "revised"] += 1


def _unreadable_at(own_path: Path, own: store.Library, key: str) -> str:
    """本体库里这个键的位置上是不是躺着一份**读不出来**的文件（是 → 中文原因，否 → 空串）。

    键不在内存索引里有两种可能：本体真的没有这条（本场新建，写进去就是新建），或者**那儿
    有一份文件却没读进来**（手写的字段名/格式有问题，`load_library` 跳过它并给一条警告）。
    后者绝不能当成"这个键是空的"：`entries/<键>.md` 就在那儿，写下去就是拿一份连归档都取
    不到原文的东西（那份内容从没进过内存）盖掉用户手写的正文。探测不出结果时（IO 出错）
    按"有"处理——宁可这一条不并（报告里看得见），也不冒覆盖的风险。
    """
    if key in own.entries:
        return ""
    try:
        path = store.entry_path(own_path, key)
    except ValueError:
        return ""                  # 键本身不合法：上面那道文件名守门已经挡下了
    try:
        occupied = path.is_file()
    except OSError:
        occupied = True
    if not occupied:
        return ""
    return (f"「{key}」没有并进本体库：本体库里 {store.ENTRIES_DIRNAME}/{path.name} 那一份"
            f"现在读不出来（多半是手写的字段名/格式有问题），写下去会把它的正文整份盖掉，"
            f"而那份内容连读都没读进来过——覆盖了连归档都取不到原文。"
            f"先修好或删掉那个文件，再结算这一场。")


def _needs_archive(current: Entry, entry: Entry) -> bool:
    """同键冲突时旧条要不要归档（§3.5）：旧正文为空、或与新条**逐字相同** → 不归档。

    不制造噪声：正文一样的两条归档出来只是库里多一份没有信息量的过时条目。标题/摘要有变
    而正文没变时同样不归档——"旧条一字不删"要保的是**那段话**，它已经在库里了。
    """
    return bool(current.body.strip()) and current.body != entry.body


def _archived(current: Entry, archive_key: str, new_key: str) -> Entry:
    """本体里被顶掉的那条 → 归档条目（§3.5）：正文（连同标题、摘要、出处、压制记录）逐字保留，
    只改四处——换成派生键、`status=outdated`、`superseded_by` 指向新条、**`indexed=false`**
    （它不该占索引表的位置，但仍读得到、仍在图上）。"""
    return current.model_copy(update={
        "key": archive_key,
        "status": "outdated",
        "superseded_by": new_key,
        "indexed": False,
    })


def _archive_key(own: store.Library, key: str, old_body: str) -> str:
    """给旧正文挑一个派生键：`<键>（旧）`，重名则 `（旧2）`、`（旧3）`…；挑不出来 → 空串。

    两件事：

      · **认得"已经归档过了"**：某一格里装的正是同一段旧正文、且它已经指向这个键，那就
        复用它——幂等（§7.5）靠的就是这一条，不然第二次合并会一路造出 `（旧2）`、`（旧3）`。
      · **挑不出来就返回空串**（键太长，派生键过不了文件名守门）：调用方据此**放弃合并这一条**
        并记警告。绝不静默改名，也绝不硬写：把 `<键>（旧）` 换成别的写法会让"归档键可预测"
        这件事失效，而覆盖旧正文是真正的数据损失。
    """
    candidate = f"{key}{ARCHIVE_SUFFIX}"
    index = 1
    while True:
        if entry_key_error(candidate):
            return ""
        found = own.entries.get(candidate)
        if found is None:
            return candidate
        if found.superseded_by == key and found.body == old_body:
            return candidate
        index += 1
        candidate = f"{key}{_ARCHIVE_SUFFIX_FMT.format(n=index)}"


def _press_others(own: store.Library, entry: Entry) -> list[str]:
    """把本体库里与这份新说法**同一件事**的其它条目标成过时，返回被压下的键（保序）。

    判据复用 `knowledge.same_subject`（§3.5 两级：同 key 或标题近似）；同 key 的跳过——那是
    同一个文件、同一条目，由调用方原地顶上（带归档）。只动 `status` 与 `superseded_by`，
    **正文一字不动**：旧说法永远留着，只是不再是现行说法。

    已经是过时状态的不再动它：它身上的 `superseded_by` 指向的是当初压它的那一条（新的那条
    未必是它的替代者），改写它等于篡改历史，也会让本函数在第二次合并时又"动一次"——幂等
    就没了。
    """
    out: list[str] = []
    for other_key in own.ordered_keys():
        if other_key == entry.key:
            continue
        other = own.entries[other_key]
        if other.status != "active":
            continue
        if same_subject(entry, other):
            other.status = "outdated"
            other.superseded_by = entry.key
            out.append(other_key)
    return out


def _dedupe(keys: Sequence[str]) -> list[str]:
    """去重且保序（`supersedes` 是列表，重复的键只会让文件变吵）。"""
    out: list[str] = []
    for key in keys:
        if key and key not in out:
            out.append(key)
    return out


# -------------------------------------------------------------------- 丢弃 --

def discard_gain(gain: SceneGain) -> DiscardReport:
    """丢弃（§7.2）：什么都不并，**一个字都不写**——副本连同这一场的 `runs/` 存档一起留着，
    你事后翻得到他那一场学了什么，只是没进本体。返回一份记录，让调用方说得出丢了多少。
    """
    return DiscardReport(scene=gain.scene, character=gain.character,
                         dropped=len(gain.added) + len(gain.revised),
                         warnings=list(gain.warnings))


# -------------------------------------------------------------------- 结算 --

def settle(copy_dir: Path, gain: SceneGain, own_dir: Path, *, keep: bool) -> SettleReport:
    """按用户的选择结算这一场：保留 → 合并，丢弃 → 什么都不并（§7.2）。**绝不抛。**

    幂等（§7.5）：`pending.json` 里的 `settled` / `outcome` 说了算（`mark_settled` 先到先得）。
    **已结算过的这一场再来一次是空操作**（`repeated=True` 并附一句说明），不报错：报错会让
    界面上的一次重试变成弹窗事故，而"再并一遍"会给出"又并了 1 条"这种骗人的数。先到先得
    是硬的——一次手滑的重复点击绝不能把"丢弃"翻成"保留"。

    **合并没完全生效时不标记已结算**（`merged.persisted` 为 False：有该并的东西没并进去，
    或者一个字都没落盘）：标了就等于把用户的"保留"兑成一句空话，而且先到先得会把他的重试
    一起挡掉。这一场仍是"待结算"，处理好之后还能再来（重试幂等）。丢弃那条路没有可失败的
    东西，永远标得上。
    """
    copy_path = Path(copy_dir)
    pending = store.load_pending(copy_path)
    if pending.settled:
        return SettleReport(
            outcome=pending.outcome, repeated=True,
            warnings=[f"这一场已经结算过了（{_outcome_text(pending.outcome)}），"
                      f"这次没有再动它。"])
    outcome = store.OUTCOME_KEEP if keep else store.OUTCOME_DISCARD
    merged = MergeReport()
    discarded = 0
    if keep:
        merged = merge_gain(gain, own_dir)
        if not merged.persisted:
            return SettleReport(
                outcome=outcome, merged=merged,
                warnings=[*merged.warnings,
                          "这次结算没有完全生效（有没能并进去的条目，或者一个字都没落盘）："
                          "这一场仍是待结算状态，处理好之后再结一次即可（已经并过的不会重复）。"])
    else:
        dropped = discard_gain(gain)
        discarded = dropped.dropped
        merged = MergeReport(warnings=list(dropped.warnings))
    warnings = list(merged.warnings)
    state_saved = True
    try:
        marked = store.mark_settled(copy_path, outcome)
    except Exception as exc:              # 状态写不下去也不许抛：合并那半已经做完了
        marked, state_saved = False, False
        warnings.append(
            f"合并做完了，但结算状态没能记下来（{exc}）：这一场下次仍会被当成待结算，"
            f"再点一次「保留」是幂等的（不会重复归档，也不会把丢弃翻成保留）。")
    if not marked and state_saved:        # 竞态：别人先结了这一场（先到先得，绝不翻盘）
        warnings.append("这一场在结算的同时已被结算过，这次的选择没有生效。")
    return SettleReport(outcome=outcome, repeated=not marked, merged=merged,
                        discarded=discarded, warnings=warnings)


def _outcome_text(outcome: str) -> str:
    """结算结果 → 能读的中文（提示语用）；认不出来的把原值带上，绝不编一个。"""
    if outcome == store.OUTCOME_KEEP:
        return "保留"
    if outcome == store.OUTCOME_DISCARD:
        return "丢弃"
    return f"未知结果「{outcome}」" if outcome else "结果没记下来"


# ---------------------------------------------------------------- 离场挂起 --

def pending_gain(copy_dir: Path) -> PendingGain:
    """这个角色有未结算的本场所得吗（§7.3，供引擎/界面提示"待结算"）。**只读，绝不抛。**

    读的是 `pending.json`（`knowledgestore.record_pending` 记的那两份清单 + 结算状态）。
    副本不存在、`pending.json` 坏了都当作"没有待结算的东西"——界面在角色还没进场、或刚
    结完账时照样会问一句"他欠着吗"，这里抛一次就是面板刷不动。
    """
    path = Path(copy_dir)
    pending = store.load_pending(path)
    return PendingGain(scene=_scene_of(path), character=_character_of(path),
                       added=list(pending.added), revised=list(pending.revised),
                       settled=pending.settled, outcome=pending.outcome)
