"""信息库编辑器（设计文档 §8.1）与「订阅信息库」区的**界面 + 数据助手**。

第三个编辑器（前两个是角色卡与场景卡），两栏：左栏按 subject 分组的条目表，右栏是选中
条目的详情。六件事在这里定下来，都是 §8.1 的硬要求：

1. **过时条目必须看得见**。全仓其它地方（`knowledge.render_index`）默认不列过时条——
   索引是"我现在知道什么"的入口，旧说法一并列上会让角色读到自相矛盾的两条。编辑器正
   相反：它是账本，你得像看 diff 一样看见"这条被谁取代了"。故这里 `include_outdated`
   的等价物是"全列"，过时行灰显 + 后缀一句「被 X 取代」。
2. **两个方向都要给**。指向（出链，`knowledge.parse_links`）与被引用（反链，
   `knowledge.backlinks`）同屏并列——§6.2 说图只能单向走的话角色会漏掉半边；编辑器里
   看不见反链，你就改不对别处正文里的 `[[键]]`。
3. **保存只走 `knowledgestore.save_library`**（原子写、条目文件是唯一真相源）。本模块
   **绝不自己拼条目文件、绝不自己动 library.json**——唯一例外是订阅文件，见下。
4. **键名/计数一律等宽排版**（§3.6 视觉语言），配色一律取自 `theme.dialog_qss` 与
   `palette()`——本模块不留任何字面颜色，四套主题下观感一致。
5. **写盘前守门：磁盘上已存在的东西，一个都不许静默覆盖**。"新建条目"与"固化"都要先看
   磁盘（`unreadable_entry_file`）再看内存（`lib.entries`）——`load_library` 对读不出来的
   条目文件是"跳过 + 报一条 warning"，那种条目不在内存里、正文却完好地躺在磁盘上，只看
   内存就会把 `<键>.md` 覆写成空壳。存储层的 `Library.upsert` 认为"键完全相同就是覆盖"，
   那是给合并路径的假设（§3.5 那条"同键冲突要归档、不能就地抹掉"的另一半），界面不能
   照抄。借入行还必须按 `row.source_dir` 取详情：同键共存是常态，"借自《X》"那一行显示
   的必须是 X 那份，否则所见非所得。
6. **没保存的改动不许静默丢**。点别的行 / 新建条目 / 换库都会重填右栏，落笔前先过一句
   确认（与"删除条目"同一口径）——保存是右栏唯一的落盘动作，一次误点不该抹掉刚敲的正文。

**订阅关系落在哪（§8.2 / §3.4）**：`<库目录>/subscriptions.json`，与库同目录。
理由三条，缺一不可：

  · **不能放进 `library.json`**：那是 `Library` 的模型序列化（`extra="forbid"`），任何
    额外键在下次 `save_library` 时都会被整份重写抹掉——放进去等于没放。
  · **与角色库同目录**：订阅是"这个角色订了什么"，库搬到哪它跟到哪（打包方案实施时
    随用户数据一起迁，天然受益，不需要额外设计）。
  · **引擎侧接得上**：读实现下沉在 `knowledgestore.load_subscriptions`（返回
    `[(库名, 源库目录), …]`，正是 `render_library_index(..., subscriptions=…)` 收的形状），
    本模块的 `load_subscriptions` 只是**转调**它再裹上界面要的 `Subscription`。于是
    `knowledgetools.KnowledgeAccess` 渲染索引时读的就是同一份订阅——界面上勾的，
    角色下一块的索引里就有，两处不可能漂开。

文件里存**相对库根**的路径（`wide/庆国世界观`），不存绝对路径：库根在开发态与打包态
不同，绝对路径一搬就全断。读侧一律宽容（缺失/坏 JSON/坏项/逃出库根的路径只丢它自己，
绝不抛），与本仓"运行产物读坏不崩"的一贯口径一致（对照 `knowledgestore.load_pending`）。

两份模块互相引用：本模块 import `library`（复用它那套微件助手与译者，保证 object name
与样式**只有一份**），`library.CharacterEditorDialog` 的订阅区反过来**延迟** import 本
模块（函数内 import，见那里的说明）——循环引用就此断开，两边都不必各自复制一套界面零件。
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QVBoxLayout, QWidget,
)

from .. import knowledgestore as store
from ..knowledge import (
    Entry, EntryFormatError, backlinks, entry_key_error, parse_entry_md, parse_links)
from .library import (
    _danger, _ghost, _hairline, _label, _mono, _primary, _text_edit, app_theme,
    confirm, palette, translator)
from .theme import dialog_qss

#: 订阅文件（§8.2）：住在库目录里，与 `library.json` / `entries/` 并列。
#: 名字与读侧实现都下沉到了存储层（`knowledgestore`）——**只有一处口径**，从那里取。
SUBSCRIPTIONS_JSON = store.SUBSCRIPTIONS_JSON

#: 订阅文件的 schema 版本（便于将来迁移，与 library.json 的 `schema` 同规矩）。
SUBSCRIPTIONS_SCHEMA = 1

#: 读侧的两道守门（认写法 / 认结果）也只有一个实现，存放在 knowledgestore。
#: 这里保留同名别名：界面的其余代码（与测试）照旧按旧名字调用，实现却只有一份。
_safe_rel = store._safe_rel
_inside_root = store._inside_root

#: 库类型 → 列表排序权重：广域库排前面（订阅的主战场是世界观一类的共享设定）。
_KIND_ORDER = {"wide": 0, "character": 1}

#: 订阅列表 / 借入条目列表的定高口径：最多显示 4 行，超出即滚动（不随库数无限拉长）。
_LIST_ROWS = 4
_LIST_ROW_PX = 24
_LIST_PAD_PX = 8

#: 库根下两个 scope 目录（与 §4.1 的落盘布局同一份名字，来自存储层常量而非手抄）。
_SCOPE_DIRS: tuple[tuple[str, str], ...] = (
    ("wide", store.WIDE_SCOPE_DIR),
    ("character", store.CHARACTER_SCOPE_DIR),
)


# ------------------------------------------------------------------ 库根与库表
def default_libraries_root() -> Path:
    """缺省信息库根：**与引擎侧同一处**（`gui/worker.py::_default_libraries_root` 的
    `parents[3]` 惯例）——编辑器与引擎各指一处的话，界面里编出来的库引擎根本找不到。

    优先读 worker 的类属性（那是引擎真正用的那个值，测试也照它做隔离），取不到再按惯例
    自算。延迟 import：worker 会把引擎/后端一并拖进来，纯界面路径不该付这份钱。
    """
    try:
        from .worker import SceneWorker
        configured = SceneWorker.libraries_root
        if configured:
            return Path(configured)
    except Exception:                       # noqa: BLE001 - 缺依赖/循环引用都只是回落
        pass
    return Path(__file__).resolve().parents[3] / "libraries"


def library_kind_label(kind: str, t=None) -> str:
    """库类型 → 界面名（广域库 / 角色库）。"""
    t = t or translator()
    return t.t("kind.wide" if kind == "wide" else "kind.character_lib")


@dataclass(frozen=True)
class LibraryInfo:
    """库根下的一座库：类型 + 名字 + 目录 + **相对库根的路径**（落盘形态）。"""
    kind: str
    name: str
    path: Path
    rel: str

    def label(self, t=None) -> str:
        """列表行文本：`《库名》　类型`（与仓库其它列表一样，键值同屏给出）。"""
        return f"《{self.name}》　{library_kind_label(self.kind, t)}"


@dataclass(frozen=True)
class Subscription:
    """一条订阅：库名 + 类型 + 源库目录 + 相对库根的路径。"""
    name: str
    kind: str
    path: Path
    rel: str


def list_libraries(root: Path | str | None) -> list[LibraryInfo]:
    """扫库根下的广域库与角色库（§4.1 的 `wide/` 与 `characters/`），按类型+名字排序。

    判据与引擎一致：目录里有 `library.json` 或 `entries/` 才算一座库（`has_library`
    与 `entries_dir` 都是**存储层**的口径——两处各写一份「什么算一座库」迟早漂开）。
    空目录、随手建的子目录都不列：列出来点进去是一座空库，用户只会以为坏了。
    """
    base = Path(root) if root is not None else default_libraries_root()
    out: list[LibraryInfo] = []
    for kind, dirname in _SCOPE_DIRS:
        scope = base / dirname
        if not scope.is_dir():
            continue
        for child in sorted(scope.iterdir(), key=lambda p: p.name.casefold()):
            if not child.is_dir():
                continue
            if not (store.has_library(child) or store.entries_dir(child).is_dir()):
                continue
            out.append(LibraryInfo(kind=kind, name=child.name, path=child,
                                   rel=f"{dirname}/{child.name}"))
    out.sort(key=lambda info: (_KIND_ORDER.get(info.kind, 9), info.name.casefold()))
    return out


def character_library_dir(root: Path | str | None, name: str) -> Path | None:
    """角色名 → 它的角色库目录；名字空 / 过不了 §4.3 的守门 → None。

    角色名同时是库目录名，故与键守同一套规则（`knowledgestore.seed_character_library`
    也是这么把关的）：不合格**不猜、不改**，直接说"没有落点"。
    """
    text = (name or "").strip()
    if not text or entry_key_error(text):
        return None
    base = Path(root) if root is not None else default_libraries_root()
    return store.library_dir("character", text, root=base)


def create_library(root: Path | str, kind: str, name: str) -> Path:
    """新建一座库（目录 + `library.json` + `entries/`），返回库目录。

    库名过 `entry_key_error`（它会是目录名）；已有同名同类库 → 报错，**绝不覆盖**
    （覆盖等于把人家这一路长出来的条目抹回出厂状态）。写盘走后端唯一的
    `save_library`，本模块不自己拼落盘结构。
    """
    t = translator()
    text = (name or "").strip()
    if not text:
        raise ValueError(t.t("err.library_name_required"))
    problem = entry_key_error(text)
    if problem:
        raise ValueError(t.t("err.library_name_unsafe", name=text, reason=problem))
    path = store.library_dir(kind, text, root=Path(root))   # 未知类型在这里抛
    if store.has_library(path) or store.entries_dir(path).is_dir():
        raise ValueError(t.t("err.library_exists", path=path))
    store.save_library(store.Library(
        name=text, scope=kind, owner=(text if kind == "character" else "")), path)
    return path


# ------------------------------------------------------------------ 订阅（§8.2）
def subscriptions_path(lib_dir: Path) -> Path:
    """库目录 → 它的订阅文件路径（转调存储层，口径只有一处）。"""
    return store.subscriptions_path(lib_dir)


def _kind_of_rel(rel: str) -> str:
    """相对路径的首段 → 库类型（认不出首段时才回落到 character）。"""
    head = rel.split("/")[0] if rel else ""
    for kind, dirname in _SCOPE_DIRS:
        if head == dirname:
            return kind
    return "character"


def _rel_of(target: Path | str, root: Path) -> str:
    """源库目录 → **相对库根**的 posix 路径（落盘形态；不在根下则抛，不写绝对路径）。"""
    base = Path(root).resolve()
    try:
        return Path(target).resolve().relative_to(base).as_posix()
    except ValueError:
        raise ValueError(translator().t("err.library_outside_root", path=target)) from None


def load_subscriptions(lib_dir: Path, *, root: Path | str) -> list[Subscription]:
    """读订阅 → 界面用的 `Subscription` 表；**转调存储层那一个读函数**（口径只有一处）。

    文件缺失 / 坏 JSON / 坏项一律只丢它自己、绝不抛——这条宽容语义全在
    `knowledgestore.load_subscriptions` 里（那里还兼任安全边界：逃出库根的项丢掉）。
    本函数只是把存储层给的 `[(库名, 源库目录), …]` 裹上界面要的 `rel` / `kind`
    （相对库根的路径由**同一个 root** 现算，故与写侧落盘形态逐字一致）。
    """
    base = Path(root) if root is not None else default_libraries_root()
    out: list[Subscription] = []
    for name, path in store.load_subscriptions(lib_dir, root=base):
        rel = path.relative_to(base).as_posix()
        out.append(Subscription(name=name, kind=_kind_of_rel(rel),
                                path=path, rel=rel))
    return out


def save_subscriptions(lib_dir: Path, subs: Sequence[Subscription], *,
                       root: Path | str) -> Path:
    """原子写订阅文件（临时文件 + fsync + `os.replace`，照抄 `knowledgestore` 那套）。

    这是本模块**唯一**自己写盘的地方——订阅不是条目，存储层没有它的模型（see 模块
    docstring 的三条理由），故而在这里写；条目一律仍旧走 `save_library`。
    """
    data = {
        "schema": SUBSCRIPTIONS_SCHEMA,
        "libraries": [{"name": s.name, "kind": s.kind,
                       "path": s.rel or _rel_of(s.path, Path(root))} for s in subs],
    }
    target = subscriptions_path(lib_dir)
    _atomic_write_text(target, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return target


def set_subscription(lib_dir: Path, target: Path | str, *, root: Path | str,
                     subscribed: bool) -> list[Subscription]:
    """勾上/摘掉一条订阅（幂等），返回写后的订阅表。

    顺序 = 勾选顺序（后来者排后面）：订阅在索引表里是按库分组显示的，顺序只影响观感，
    但"我最后勾的那个排在最后"符合直觉，也免得每次保存都重排一份 diff。
    """
    base = Path(root) if root is not None else default_libraries_root()
    rel = _rel_of(target, base)
    subs = [s for s in load_subscriptions(lib_dir, root=base) if s.rel != rel]
    if subscribed:
        subs.append(Subscription(name=Path(target).name, kind=_kind_of_rel(rel),
                                 path=Path(target), rel=rel))
    save_subscriptions(lib_dir, subs, root=base)
    return subs


def unreadable_entry_file(lib_dir: Path | str, key: str) -> Path | None:
    """磁盘上是不是有个**装着这个键却读不出来**的条目文件（因而不在内存索引里）。

    看磁盘而不是只看 `lib.entries`：`load_library` 对读不出来的条目文件是"跳过 + 报一条
    warning"，那条文件**不在** `lib.entries` 里，正文却完好无损地躺在磁盘上（§4.2：条目
    文件是唯一真相源）。只看内存就会把这种情况误判成"这个键没人占"，随后 `save_library`
    把 `<键>.md` 覆写成一条空壳——用户手写的整条正文就此消失，而界面当时还正显示着那条
    warning。§4.3 说"不安全就报错给用户看，绝不静默改名"，静默改内容更不行。

    读得出来的同名文件不算（它要么就是这一条、要么以别的键在内存里，正文都不会丢）；
    文件名与键不一致、又读不出来的，无从知道它装的是哪个键，只能不管。
    """
    wanted = f"{key}.md".casefold()
    for path_md in sorted(store.entries_dir(Path(lib_dir)).glob("*.md")):
        if path_md.name.casefold() != wanted:
            continue
        try:
            parse_entry_md(path_md.read_text(encoding="utf-8"))
        except (EntryFormatError, OSError, UnicodeDecodeError):
            return path_md
        return None                     # 读得出来 → 它归内存索引管，不归这里
    return None


def pin_target_problem(dst_lib_dir: Path | str, key: str) -> str:
    """固化前的守门：目标库里这个键是不是已经有主了？空串 = 可以固化，否则中文原因。

    两类"有主"都不许静默覆盖（§3.5：「同键冲突要归档，不能就地抹掉」，直接覆盖等于把
    本体里那条旧正文永久抹掉）：

      · 本库已有一条**同键的自有条目**。存储层的 `Library.upsert` 认为"键完全相同就是
        覆盖"——那是给合并路径的假设（同一条目被覆盖正是它的本意），界面不能照抄：固化一条
        借入条目时命中的是**另一条内容**，而固化恰恰是"这一节我想保住我自己的理解"的动作，
        把它抹成官方版正好相反；
      · 磁盘上有个同名条目文件**读不出来**（见 `unreadable_entry_file`，它连内存都没进）。

    要固化请先把自有那条改个键或删掉——绝不替用户做这个决定。
    """
    t = translator()
    if key in store.load_library(Path(dst_lib_dir)).library.entries:
        return t.t("err.pin_target_taken", entry=key)
    stuck = unreadable_entry_file(dst_lib_dir, key)
    if stuck is not None:
        return t.t("err.pin_file_taken", name=stuck.name)
    return ""


def pin_entry(src_lib_dir: Path | str, key: str, dst_lib_dir: Path | str) -> Entry:
    """固化一条借入条目（§3.4）：过守门后转调 `knowledgestore.pin_entry`，返回那条拷贝。

    只做守门与转发，不重写实现：`origin.turn` 换哨兵（绝不让这条参与本场撤回的截断）那一手
    在存储层，界面侧照抄一份迟早漂开。`KeyError`（源库没有这个键）/ `ValueError`（键不
    安全、大小写撞名、目标已被同键占着）/ `OSError` 一律上抛，由调用方就地报错——**绝不
    静默返回 None**，那会让用户以为固化成功了。
    """
    problem = pin_target_problem(dst_lib_dir, key)
    if problem:
        raise ValueError(problem)
    return store.pin_entry(Path(src_lib_dir), key, Path(dst_lib_dir))


def borrowed_entries(
        subs: Sequence[Subscription]) -> list[tuple[str, Path, list[Entry]]]:
    """订阅的**活引用**（§3.4）：现去源库读一次，返回 [(库名, 源库目录, 条目)]。

    读盘走 `knowledgestore.subscribed_rows`（活引用的唯一实现，不缓存、不拷贝——
    对方一改，下一次渲染立刻是新内容），这里只是把**源库目录**一并带出来：编辑器要拿
    它去固化那一条（`pin_entry(src, key, dst)` 的第一个参数）。
    """
    rows = store.subscribed_rows([(s.name, s.path) for s in subs])
    return [(name, sub.path, list(entries))
            for (name, entries), sub in zip(rows, subs)]


# ------------------------------------------------------------------ 左栏行模型
@dataclass(frozen=True)
class EntryRow:
    """左栏的一行：组头（`header=True`）或一条条目。

    `source` / `source_dir` 只对**借入条目**有值（来自订阅）——它们是只读的（要改先
    固化），故详情面板据此停用编辑框，固化按钮据此知道去哪个源库取原文。
    """
    header: bool
    label: str
    key: str = ""
    source: str = ""
    source_dir: Path | None = None
    outdated: bool = False


def _one_line(text: str) -> str:
    """压成一行：标题里混进的换行会把左栏的行高撑坏（与索引渲染同一口径）。"""
    return " ".join((text or "").split())


def build_rows(entries: Sequence[Entry],
               borrowed: Sequence[tuple[str, Path, Sequence[Entry]]],
               t=None) -> list[EntryRow]:
    """自有条目 + 借入条目 → 左栏行表（**过时条一并列出**，见模块 docstring 第 1 条）。

    分组口径与索引渲染一致（`knowledge._groups`）：无 subject 的一桶排最前且不打组头
    ——remember 工具本就不传 subject，这一桶才是常态，硬安一个「其他」组头只会平白多
    一行噪声。借入条目每个源库单独一组，组头标明「借自《库名》」（§8.1）。
    """
    t = t or translator()

    def _row(entry: Entry, source: str = "",
             source_dir: Path | None = None) -> EntryRow:
        base = _one_line(entry.title) or _one_line(entry.key)
        outdated = entry.status == "outdated"
        if outdated:
            # 占位符不能叫 `key`：`Translator.t(self, key, **fmt)` 的第一个形参就是它。
            base += (t.t("label.outdated_superseded", by=entry.superseded_by)
                     if entry.superseded_by else t.t("label.outdated_plain"))
        if source:
            base += " ⇢"                       # 借来的（§8.1 的记号），来源见组头
        return EntryRow(header=False, label=base, key=entry.key, source=source,
                        source_dir=source_dir, outdated=outdated)

    rows: list[EntryRow] = []
    ungrouped = [e for e in entries if not (e.subject or "").strip()]
    order: list[str] = []
    groups: dict[str, list[Entry]] = {}
    for entry in entries:
        subject = (entry.subject or "").strip()
        if not subject:
            continue
        if subject not in groups:
            groups[subject] = []
            order.append(subject)
        groups[subject].append(entry)
    rows.extend(_row(e) for e in ungrouped)
    for subject in order:
        rows.append(EntryRow(header=True, label=subject))
        rows.extend(_row(e) for e in groups[subject])
    for name, source_dir, items in borrowed:
        if not items:
            continue
        rows.append(EntryRow(header=True, label=t.t("grp.borrowed", name=name)))
        rows.extend(_row(e, source=name, source_dir=source_dir) for e in items)
    return rows


# ------------------------------------------------------------------ 原子写
def _atomic_write_text(path: Path, text: str) -> None:
    """文本原子落盘（同目录临时文件 → fsync → `os.replace`）。

    与 `knowledgestore._atomic_write_text` 同一套做法，这里再写一遍是因为本模块只写
    **订阅文件**这一种东西（不是条目），为一行调用去 import 引擎侧的私有函数不划算。
    临时文件必须与目标同目录（同卷）才谈得上原子替换；`newline="\\n"` 让落盘字节可复现。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp",
                                    dir=str(path.parent))
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


# ------------------------------------------------------------------ 小工具
def _mono_font() -> QFont:
    """等宽字体对象（指向/被引用里的**键名**一律等宽排版，§3.6）。

    字族取自当前主题的 `mono_font` 串（那是界面上等宽排版的唯一出处，见 `theme`）：
    取它的第一个族名给 QFont——列表项只认 QFont，不认 CSS 串。
    """
    family = palette().mono_font.split(",")[0].strip().strip('"')
    return QFont(family)


def _root_of(lib_dir: Path, fallback: Path | None = None) -> Path:
    """库目录 → 它的库根：`<根>/<characters|wide>/<名>` 往上两级；认不出才用 fallback。"""
    parent = Path(lib_dir).parent
    if parent.name in (store.CHARACTER_SCOPE_DIR, store.WIDE_SCOPE_DIR):
        return parent.parent
    return Path(fallback) if fallback is not None else default_libraries_root()


def _key_item(text: str, key: str) -> QListWidgetItem:
    """指向/被引用列表里的一行（键名等宽）。"""
    item = QListWidgetItem(text)
    item.setFont(_mono_font())
    item.setData(Qt.ItemDataRole.UserRole, key)
    item.setToolTip(key)
    return item


def _placeholder(list_widget: QListWidget, text: str) -> None:
    """列表占位行：不可选中、灰显——空着一个框会让人以为坏了。"""
    item = QListWidgetItem(text)
    item.setFlags(Qt.ItemFlag.NoItemFlags)
    item.setForeground(QBrush(QColor(palette().muted_text)))
    list_widget.addItem(item)


# ====================================================== 库选择器
class LibraryPickerDialog(QDialog):
    """「信息库」选择器：列出库根下的广域库与角色库，并可就地新建一座。

    §8.1 要求编辑器是"选一座库 → 编辑"，而第一次用的时候一座库都没有——故新建入口也在
    这里（否则用户卡在"没有库可选、又没有地方建库"的死角里）。打开/新建都把结果写进
    `chosen` 并 accept()。

    程序化填充用：library_list / new_kind_combo / new_name_edit / new_lib_btn /
    open_btn / error_label / chosen。

    **子类可换的三处**（关系编辑器只认角色库，见 `gui/relations_editor`）：`_TITLE_KEY` /
    `_HINT_KEY` 两个类属性换文案，`list_for()` 换列表来源——"什么算一座库"的口径仍只有
    `list_libraries` 一份，子类只做过滤。
    """

    #: 标题与提示语取哪两个键（子类覆盖；见 `gui/relations_editor.RelationsPickerDialog`）。
    _TITLE_KEY = "dlg.knowledge_pick_title"
    _HINT_KEY = "hint.knowledge_pick"

    def __init__(self, libraries_root: Path | str, parent: QWidget | None = None):
        super().__init__(parent)
        t = translator()
        self._root = Path(libraries_root)
        self._infos: list[LibraryInfo] = []
        #: 用户选中的库目录（打开或新建出来的）；没选 = None。
        self.chosen: Path | None = None
        self.setStyleSheet(dialog_qss(app_theme()))
        self.setWindowTitle(t.t(self._TITLE_KEY))
        self.setMinimumSize(520, 460)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(8)
        root.addWidget(_label(t.t(self._HINT_KEY), "sect"))

        self.library_list = QListWidget()
        self.library_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.library_list.itemSelectionChanged.connect(self._refresh_open_btn)
        self.library_list.itemDoubleClicked.connect(lambda _i: self.open_selected())
        root.addWidget(self.library_list, 1)

        root.addWidget(_hairline())
        new_row = QHBoxLayout()
        new_row.setSpacing(6)
        self.new_kind_combo = QComboBox()
        for kind, _dirname in _SCOPE_DIRS:       # 广域库在前（订阅的主战场）
            self.new_kind_combo.addItem(library_kind_label(kind, t), kind)
        self.new_name_edit = QLineEdit()
        self.new_name_edit.setPlaceholderText(t.t("label.please_library_name"))
        self.new_lib_btn = _ghost(t.t("btn.new_library"))
        self.new_lib_btn.clicked.connect(self.new_library)
        new_row.addWidget(self.new_kind_combo)
        new_row.addWidget(self.new_name_edit, 1)
        new_row.addWidget(self.new_lib_btn)
        root.addLayout(new_row)

        self.error_label = QLabel("")
        self.error_label.setObjectName("error")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        root.addWidget(self.error_label)

        foot = QHBoxLayout()
        foot.setSpacing(8)
        foot.addStretch(1)
        cancel_btn = _ghost(t.t("btn.cancel"))
        cancel_btn.clicked.connect(self.reject)
        self.open_btn = _primary(t.t("btn.open"))
        self.open_btn.setEnabled(False)
        self.open_btn.clicked.connect(self.open_selected)
        foot.addWidget(cancel_btn)
        foot.addWidget(self.open_btn)
        root.addLayout(foot)

        self.refresh()

    # ---------------------------------------------------------------- 列表
    def list_for(self) -> list[LibraryInfo]:
        """本选择器要列的库（子类收窄：关系编辑器只认角色库）。"""
        return list_libraries(self._root)

    def refresh(self) -> None:
        """重扫库根（新建之后调用），列表回到最新磁盘状态。"""
        t = translator()
        self.library_list.clear()
        self._infos = self.list_for()
        for info in self._infos:
            item = QListWidgetItem(info.label(t))
            item.setData(Qt.ItemDataRole.UserRole, str(info.path))
            item.setToolTip(str(info.path))
            self.library_list.addItem(item)
        if not self._infos:
            _placeholder(self.library_list, t.t("label.no_libraries"))
        self._refresh_open_btn()

    def _current_path(self) -> Path | None:
        item = self.library_list.currentItem()
        data = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return Path(str(data)) if data else None

    def _refresh_open_btn(self) -> None:
        self.open_btn.setEnabled(self._current_path() is not None)

    def _select_path(self, path: Path) -> None:
        """按路径选中一行（新建之后定位到它）。"""
        for i in range(self.library_list.count()):
            item = self.library_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == str(path):
                self.library_list.setCurrentItem(item)
                return

    # ---------------------------------------------------------------- 打开/新建
    def open_selected(self) -> bool:
        """打开选中的库：写进 `chosen` 并 accept；没选就什么都不做。"""
        path = self._current_path()
        if path is None:
            return False
        self.chosen = path
        self.accept()
        return True

    def new_library(self) -> Path | None:
        """按当前的类型下拉 + 名字框建一座库；成功即选中它并 accept。

        非法库名 / 已有同名同类库 → 就地报错并返回 None（**不落盘**，绝不静默改名）。
        """
        t = translator()
        kind = self.new_kind_combo.currentData() or "wide"
        name = self.new_name_edit.text().strip()
        try:
            path = create_library(self._root, kind, name)
        except (ValueError, OSError) as exc:
            self._error(str(exc))
            return None
        self.new_name_edit.clear()
        self.error_label.hide()
        self.chosen = path
        self.refresh()
        self._select_path(path)
        self.accept()
        return path

    def _error(self, msg: str) -> None:
        """就地报错（红字承担语义，不加前缀符号，§3.6 不用 emoji）。"""
        self.error_label.setText(msg)
        self.error_label.show()


# ====================================================== 信息库编辑器（§8.1）
class KnowledgeEditorDialog(QDialog):
    """一座库的两栏编辑器：左栏分组条目表（含过时条与借入条），右栏条目详情。

    开一座库（`lib_dir`）并把它编到磁盘上——保存/新建/删除全部经 `knowledgestore`，
    本模块不自己拼条目文件、不自己动 `library.json`（模块 docstring 第 3 条）。

    **借入条目只读**：它来自订阅（活引用），改它没有落点——§3.4 的路径是"先固化，再按
    自己的理解改"。故详情面板对借入条停用编辑框、保存按钮不认它，固化按钮才是它该走的
    那条路。

    库根是可注入参数（缺省按引擎那套惯例）；测试一律注入 tmp，绝不碰仓库的
    `app/libraries/`。

    程序化填充用：entry_list / new_key_edit / new_entry_btn / new_library_btn /
    delete_entry_btn / pin_btn / title_edit / summary_edit / body_edit /
    outgoing_list / incoming_list / outgoing_count / incoming_count / status_label /
    library_label / error_label / save_btn / close_btn。
    """

    def __init__(self, lib_dir: Path | str, *, libraries_root: Path | str | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        t = translator()
        self._lib_dir = Path(lib_dir)
        self._root = (Path(libraries_root) if libraries_root is not None
                      else _root_of(self._lib_dir))
        self._library = store.Library()
        self._subs: list[Subscription] = []
        self._borrowed: list[tuple[str, Path, list[Entry]]] = []
        self._key: str | None = None
        #: 右栏三个框**填进去时**的内容（判"有没有没保存的改动"的基准；没选中 → None）。
        self._loaded: tuple[str, str, str] | None = None
        self._reloading = False         # 重绘列表期间不响应选中变化（防自转）
        self.setStyleSheet(dialog_qss(app_theme()))
        self.setWindowTitle(t.t("dlg.knowledge_title", name=self._lib_dir.name))
        self.setMinimumSize(880, 620)

        split = QHBoxLayout(self)
        split.setContentsMargins(16, 14, 16, 14)
        split.setSpacing(12)
        split.addWidget(self._build_left(), 1)
        split.addWidget(self._build_right(), 2)
        self.reload()

    # ---------------------------------------------------------------- 左栏
    def _build_left(self) -> QWidget:
        t = translator()
        left = QWidget()
        v = QVBoxLayout(left)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        self.library_label = _label("", "sect")
        self.library_label.setToolTip(str(self._lib_dir))
        v.addWidget(self.library_label)
        v.addWidget(_label(t.t("grp.entries"), "sect"))

        self.entry_list = QListWidget()
        self.entry_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.entry_list.itemSelectionChanged.connect(self._on_selection_changed)
        v.addWidget(self.entry_list, 1)

        self.new_key_edit = QLineEdit()
        self.new_key_edit.setPlaceholderText(t.t("label.please_entry_key"))
        self.new_entry_btn = _ghost(t.t("btn.new_entry"))
        self.new_entry_btn.clicked.connect(self.new_entry)
        new_row = QHBoxLayout()
        new_row.setSpacing(6)
        new_row.addWidget(self.new_key_edit, 1)
        new_row.addWidget(self.new_entry_btn)
        v.addLayout(new_row)

        self.pin_btn = _ghost(t.t("btn.pin"))
        self.pin_btn.setEnabled(False)
        self.pin_btn.clicked.connect(self.pin_current)
        self.delete_entry_btn = _danger(t.t("btn.delete_entry"))
        self.delete_entry_btn.clicked.connect(self.delete_current)
        self.new_library_btn = _ghost(t.t("btn.new_library"))
        self.new_library_btn.clicked.connect(self.new_library)
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        for btn in (self.pin_btn, self.delete_entry_btn, self.new_library_btn):
            btn_row.addWidget(btn)
        btn_row.addStretch(1)
        v.addLayout(btn_row)
        return left

    # ---------------------------------------------------------------- 右栏
    def _build_right(self) -> QWidget:
        t = translator()
        right = QWidget()
        v = QVBoxLayout(right)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        v.addWidget(_label(t.t("grp.entry_detail"), "sect"))

        v.addWidget(_label(t.t("field.entry_title"), "sect"))
        self.title_edit = QLineEdit()
        v.addWidget(self.title_edit)
        v.addWidget(_label(t.t("field.entry_summary"), "sect"))
        self.summary_edit = QLineEdit()
        v.addWidget(self.summary_edit)
        v.addWidget(_label(t.t("field.entry_body"), "sect"))
        self.body_edit = _text_edit("", "", 8)
        self.body_edit.textChanged.connect(self._refresh_links)
        v.addWidget(self.body_edit, 1)

        v.addLayout(self._link_header("outgoing"))
        self.outgoing_list = self._link_list(self.outgoing_clicked)
        v.addLayout(self._link_header("incoming"))
        self.incoming_list = self._link_list(self.incoming_clicked)

        self.status_label = _label("", "hint")
        v.addWidget(self.status_label)

        self.error_label = QLabel("")
        self.error_label.setObjectName("error")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        v.addWidget(self.error_label)

        foot = QHBoxLayout()
        foot.setSpacing(8)
        foot.addStretch(1)
        self.close_btn = _ghost(t.t("btn.close"))
        self.close_btn.clicked.connect(self.reject)
        self.save_btn = _primary(t.t("btn.save"))
        self.save_btn.clicked.connect(self.save_entry)
        foot.addWidget(self.close_btn)
        foot.addWidget(self.save_btn)
        v.addLayout(foot)
        return right

    def _link_header(self, which: str) -> QHBoxLayout:
        """「指向 / 被引用」的小标题 + **等宽计数**（数值一律等宽，§3.6）。"""
        t = translator()
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(_label(t.t(f"field.{which}"), "sect"))
        count = _mono("0")
        row.addWidget(count)
        row.addStretch(1)
        if which == "outgoing":
            self.outgoing_count = count
        else:
            self.incoming_count = count
        return row

    def _link_list(self, handler) -> QListWidget:
        """指向/被引用列表：键名等宽，点一下跳到那条条目（图在编辑器里也要走得通）。"""
        widget = QListWidget()
        widget.setFixedHeight(_LIST_ROWS * _LIST_ROW_PX + _LIST_PAD_PX)
        widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        widget.itemClicked.connect(handler)
        return widget

    def outgoing_clicked(self, item: QListWidgetItem) -> None:
        """点「指向」里的一条 → 跳到那条条目（库里没有则什么都不做）。"""
        self.select_key(str(item.data(Qt.ItemDataRole.UserRole) or ""))

    def incoming_clicked(self, item: QListWidgetItem) -> None:
        """点「被引用」里的一条 → 跳到那条条目。"""
        self.select_key(str(item.data(Qt.ItemDataRole.UserRole) or ""))

    # ---------------------------------------------------------------- 读盘/重绘
    def reload(self) -> None:
        """重新读盘并重绘两栏（保存/删除/固化/换库之后都走它）。

        读盘一律经 `knowledgestore.load_library`：它是"条目文件才是真相源"的唯一实现，
        坏文件跳过后把原因写在 warnings 里——那**必须**摆到用户面前（手写的条目没被收录，
        闷声不响是最坏的选择），故这里进 error_label 一并显示。
        """
        t = translator()
        loaded = store.load_library(self._lib_dir)
        self._library = loaded.library
        self._subs = load_subscriptions(self._lib_dir, root=self._root)
        self._borrowed = borrowed_entries(self._subs)
        rows = build_rows(self._library.ordered_entries(), self._borrowed, t)

        self._key = None
        self._reloading = True
        self.entry_list.blockSignals(True)
        self.entry_list.clear()
        for row in rows:
            item = QListWidgetItem(row.label)
            if row.header:
                item.setFlags(Qt.ItemFlag.NoItemFlags)
                item.setForeground(QBrush(QColor(palette().muted_text)))
            else:
                # 整行挂 UserRole：QListWidgetItem 不可哈希（做不了 dict 键），而它认
                # 任意 Python 对象——行模型直接挂在行上，是这里唯一稳的关联方式。
                item.setData(Qt.ItemDataRole.UserRole, row)
                if row.outdated:            # 过时条灰显（§8.1：必须看得见）
                    item.setForeground(QBrush(QColor(palette().muted_text)))
            self.entry_list.addItem(item)
        if not any(not row.header for row in rows):
            _placeholder(self.entry_list, t.t("label.no_entries"))
        self.entry_list.blockSignals(False)
        self._reloading = False

        self.library_label.setText(
            f"《{self._library.name or self._lib_dir.name}》　"
            f"{library_kind_label(self._library.scope, t)}")
        self._clear_detail()
        if loaded.warnings:
            self._error("；".join(loaded.warnings))
        else:
            self.error_label.hide()

    def rows(self) -> list[EntryRow]:
        """左栏当前的行表（含组头）——测试与"当前在显示什么"的判据。"""
        return [row for _i, row in self._row_items()]

    def _row_items(self) -> list[tuple[int, EntryRow]]:
        """左栏里**条目行**的 (下标, 行模型) 表（组头与占位行不在其中）。"""
        out: list[tuple[int, EntryRow]] = []
        for i in range(self.entry_list.count()):
            row = self.entry_list.item(i).data(Qt.ItemDataRole.UserRole)
            if isinstance(row, EntryRow):
                out.append((i, row))
        return out

    def current_row(self) -> EntryRow | None:
        """当前选中的条目行（选中组头/占位行/没选 → None）。"""
        item = self.entry_list.currentItem()
        row = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return row if isinstance(row, EntryRow) else None

    def current_key(self) -> str | None:
        """当前选中条目的键（没选中 → None）。"""
        return self._key

    def _entry_of(self, row: EntryRow) -> Entry | None:
        """行 → 条目：自有行查本库，**借入行去它自己的源库那份查**。

        借入行必须按 `row.source_dir` 取，不能只按 key 取：同一个键同时存在于我库与订阅
        库里（§3.4 说这是常态——"我对这一节的个人理解与官方版分叉了"）时，按 key 先查自有
        会命中我自己那条，于是选中"借自《X》"看到的是我的正文，而固化拷的是 X 那份——
        所见非所得。`build_rows` 给借入行带上了源库目录，正为此。
        """
        if row.source_dir is not None:
            for _name, src, items in self._borrowed:
                if Path(src) != Path(row.source_dir):
                    continue
                return next((item for item in items if item.key == row.key), None)
            return None
        return self._library.entries.get(row.key)

    def select_key(self, key: str) -> bool:
        """按键选中左栏的一行；键不在表里 → False（不抛）。"""
        for i, row in self._row_items():
            if row.key == key:
                self.entry_list.setCurrentItem(self.entry_list.item(i))
                return True
        return False

    def _on_selection_changed(self) -> None:
        """选中变了 → 重填右栏（真有没保存的改动时**先问一句**）。重绘期间不响应，免得自转。

        点「否」就把选中拨回原来那一行：`_reloading` 闸门让这次拨动不再触发本函数，右栏那些
        还没保存的字一个都不动。
        """
        if self._reloading:
            return
        row = self.current_row()
        if (row is None or row.key != self._key) and not self._confirm_discard():
            self._reselect(self._key)
            return
        self._fill_detail(row)

    def _is_dirty(self) -> bool:
        """右栏三个框相对**填进去时**有没有改动（没选中 / 借入条目 → False）。

        标题与摘要跟保存口径对齐（保存时 `.strip()`）：不然"标题尾部多一个空格"会被当成
        未保存改动，平白弹一次确认。
        """
        if self._key is None or self._loaded is None:
            return False
        return (self.title_edit.text().strip(), self.summary_edit.text().strip(),
                self.body_edit.toPlainText()) != self._loaded

    def _confirm_discard(self) -> bool:
        """有没保存的改动时问一句；点「否」= 留在原地（True = 可以往下走）。

        保存是右栏**唯一**的落盘动作，而"点一下别的行"就把三个框整份覆盖——一次误点就丢掉
        刚敲的整段正文，而界面上既没有脏标记也没有提示。本仓同类破坏性动作（删除条目）都要
        过 `confirm`，这一条破坏性只大不小，故同一口径。
        """
        if not self._is_dirty():
            return True
        t = translator()
        return confirm(self, t.t("title.discard_edits"),
                       t.t("dlg.confirm_discard_edits", title=self._key or ""))

    def _reselect(self, key: str | None) -> None:
        """把选中拨回 `key` 那一行，**不**因此重填右栏（丢弃确认被拒时用）。"""
        if key is None:
            return
        self._reloading = True
        try:
            self.select_key(key)
        finally:
            self._reloading = False

    def _clear_detail(self) -> None:
        """清空右栏（没选中 / 列表刚重绘完）：编辑框停用、给一句「先选一条」。"""
        self._key = None
        self._loaded = None
        for widget in (self.title_edit, self.summary_edit):
            widget.setText("")
        self.body_edit.setPlainText("")
        for widget in (self.title_edit, self.summary_edit, self.body_edit):
            widget.setEnabled(False)
        self.save_btn.setEnabled(False)
        self.pin_btn.setEnabled(False)
        self.status_label.setText(translator().t("label.select_entry"))
        self._refresh_links()

    def _fill_detail(self, row: EntryRow | None) -> None:
        """右栏：标题/摘要/正文 + 指向 + 被引用 + 状态；没选中就清空并给出提示。"""
        t = translator()
        if row is None:
            self._clear_detail()
            return
        borrowed = row.source_dir is not None
        editable = not borrowed
        for widget in (self.title_edit, self.summary_edit, self.body_edit):
            widget.setEnabled(editable)
        self.save_btn.setEnabled(editable)
        self.pin_btn.setEnabled(borrowed)

        entry = self._entry_of(row)
        if entry is None:                   # 行还在、条目没了（别处删的）：刷新一次并说明
            self._clear_detail()
            if not self._reloading:
                self.reload()
                self._error(t.t("err.entry_gone"))
            return
        self._key = row.key
        self.title_edit.setText(entry.title)
        self.summary_edit.setText(entry.summary)
        self.body_edit.setPlainText(entry.body)
        # 记下"填进去的就是这些"——之后三个框与它不一致即为有未保存的改动（见 _is_dirty）。
        self._loaded = (entry.title.strip(), entry.summary.strip(), entry.body)
        if borrowed:
            self.status_label.setText(t.t("err.borrowed_readonly"))
        elif entry.status == "outdated":
            self.status_label.setText(
                t.t("field.entry_status") + "：" +
                (t.t("value.status_outdated", by=entry.superseded_by)
                 if entry.superseded_by else t.t("value.status_outdated_plain")))
        else:
            self.status_label.setText(
                t.t("field.entry_status") + "：" + t.t("value.status_active"))
        self._refresh_links()

    def _refresh_links(self) -> None:
        """按**当前正文框**的内容重算出链，并在下面列出反链（两向同屏，§6.2）。

        出链取编辑中的正文（你刚敲下的 `[[键]]` 立刻出现在"指向"里）；反链取**库里已
        落盘**的正文（别人怎么指向我，与我此刻的改动无关）。
        """
        t = translator()
        own = self._library.ordered_entries()
        everything = list(own) + [e for _n, _s, items in self._borrowed for e in items]
        known = {e.key for e in everything}

        outgoing = parse_links(self.body_edit.toPlainText())
        self.outgoing_list.clear()
        for key in outgoing:
            text = key if key in known else t.t("label.dangling_link", link=key)
            self.outgoing_list.addItem(_key_item(text, key))
        self.outgoing_count.setText(str(len(outgoing)))

        incoming = backlinks(everything, self._key or "")
        self.incoming_list.clear()
        for key in incoming:
            self.incoming_list.addItem(_key_item(key, key))
        self.incoming_count.setText(str(len(incoming)))

    def outgoing_keys(self) -> list[str]:
        """「指向」列表里的键（按显示顺序）——出链的界面口径。"""
        return [str(self.outgoing_list.item(i).data(Qt.ItemDataRole.UserRole))
                for i in range(self.outgoing_list.count())]

    def incoming_keys(self) -> list[str]:
        """「被引用」列表里的键（按显示顺序）——反链的界面口径。"""
        return [str(self.incoming_list.item(i).data(Qt.ItemDataRole.UserRole))
                for i in range(self.incoming_list.count())]

    # ---------------------------------------------------------------- 动作
    def save_entry(self) -> bool:
        """把右栏三个框写回条目并落盘（经 `save_library`）；成功 → True。

        只改这三个字段：`status` / `superseded_by` / `indexed` / `subject` / `origin`
        一律原样带回——过时标记是**结算合并**的产物，不是编辑器能随手涂掉的东西。
        写盘前重新读一次库（`self._library` 可能已经过时）：拿陈旧的内存整份覆盖会把
        别处（比如刚固化进来的条目）悄悄抹掉。
        """
        t = translator()
        row = self.current_row()
        if row is None:
            self._error(t.t("err.select_entry"))
            return False
        if row.source_dir is not None:
            self._error(t.t("err.borrowed_readonly"))
            return False
        lib = store.load_library(self._lib_dir).library
        old = lib.entries.get(row.key)
        if old is None:
            self.reload()                   # reload 会清掉错误框，故先重绘再报错
            self._error(t.t("err.entry_gone"))
            return False
        data = old.model_dump()
        data["title"] = self.title_edit.text().strip()
        data["summary"] = self.summary_edit.text().strip()
        data["body"] = self.body_edit.toPlainText()
        try:
            entry = Entry.model_validate(data)
            lib.upsert(entry)
            store.save_library(lib, self._lib_dir)
        except ValueError as exc:           # 校验失败（字段非法）——就地报错、不落盘
            self._error(t.t("err.save_failed", exc=exc))
            return False
        except OSError as exc:
            self._error(t.t("err.save_failed", exc=exc))
            return False
        self.reload()
        self.select_key(entry.key)
        return True

    def new_entry(self) -> bool:
        """按「新建条目的键」建一条空条目（标题先取键），成功即选中它继续编。

        键过不了 §4.3 的守门 / 库里已有同键（或只有大小写之差，那是同一个文件）→ 就地
        报错、**一个字节都不落盘**，绝不静默改名；磁盘上已有同名条目文件却**读不出来**的
        也一律停手（那条正文还在磁盘上，覆盖它就是抹掉用户手写的内容，见
        `unreadable_entry_file`）。
        """
        t = translator()
        key = self.new_key_edit.text().strip()
        if not key:
            self._error(t.t("err.entry_key_required"))
            return False
        problem = entry_key_error(key)
        if problem:
            self._error(problem)
            return False
        lib = store.load_library(self._lib_dir).library
        if key in lib.entries:
            self._error(t.t("err.entry_exists", entry=key))
            return False
        stuck = unreadable_entry_file(self._lib_dir, key)
        if stuck is not None:
            self._error(t.t("err.entry_file_taken", name=stuck.name))
            return False
        # 拦到这一步都没问题、真的会 reload 右栏了，才问那句"放弃未保存的改动"
        # （先问后拦的话，用户答完"放弃"却因为键非法什么都没发生，白吓一跳）。
        if not self._confirm_discard():
            return False
        try:
            entry = Entry(key=key, title=key)      # 构造即过守门，双保险
            lib.upsert(entry)                      # 大小写撞名的第二道闸在这里
            store.save_library(lib, self._lib_dir)
        except ValueError as exc:
            self._error(str(exc))
            return False
        except OSError as exc:
            self._error(t.t("err.save_failed", exc=exc))
            return False
        self.new_key_edit.clear()
        self.reload()
        self.select_key(entry.key)
        return True

    def delete_current(self) -> bool:
        """删掉选中的条目（先确认）：文件与 library.json 的 order 一并摘掉。

        删除只删这一条、不动别的条目文件（`knowledgestore.delete_entry` 的口径）。
        过时条目照样可以删——那是你的明确动作，与"合并时旧条一字不删"不是一回事。
        """
        t = translator()
        row = self.current_row()
        if row is None or row.source_dir is not None:
            return False
        if not confirm(self, t.t("title.delete_entry"),
                       t.t("dlg.confirm_delete_entry", title=row.key)):
            return False
        if not store.delete_entry(self._lib_dir, row.key):
            self.reload()
            self._error(t.t("err.entry_gone"))
            return False
        self.reload()
        return True

    def pin_current(self) -> bool:
        """把选中的**借入**条目固化成自己的条目（§3.4），成功即选中这份拷贝。

        固化 = 拷贝 + 断开引用：此后源库再改，这一份不动——那正是"我对某一节的个人理解
        与官方版已经分叉"的用法。走本模块的 `pin_entry`（它先过守门：本库已有同键条目时
        报错而不是把人家那条正文盖掉），拷贝本身由 `knowledgestore.pin_entry` 完成（它还
        要给 `origin.turn` 换哨兵，绝不让这条参与本场撤回的截断）。
        """
        t = translator()
        row = self.current_row()
        if row is None or row.source_dir is None:
            return False
        try:
            pinned = pin_entry(row.source_dir, row.key, self._lib_dir)
        except (KeyError, OSError, ValueError) as exc:
            self._error(t.t("err.pin_failed", exc=exc))
            return False
        self.reload()
        self.select_key(pinned.key)
        return True

    def new_library(self) -> None:
        """「新建库…」：开选择器建一座新的，建好就切到它继续编。"""
        picker = LibraryPickerDialog(self._root, self)
        if picker.exec() == QDialog.DialogCode.Accepted and picker.chosen is not None:
            self.switch_library(picker.chosen)

    def switch_library(self, lib_dir: Path | str) -> None:
        """换一座库来编（左右栏整屏重绘；库根若也换了就跟着换）。

        整屏重绘会丢掉右栏没保存的改动 → 与"点别的行"同一套确认（点「否」就留在原库）。
        """
        if Path(lib_dir) != self._lib_dir and not self._confirm_discard():
            return
        self._lib_dir = Path(lib_dir)
        self._root = _root_of(self._lib_dir, fallback=self._root)
        self.setWindowTitle(translator().t("dlg.knowledge_title",
                                           name=self._lib_dir.name))
        self.library_label.setToolTip(str(self._lib_dir))
        self.error_label.hide()
        self.reload()

    def _error(self, msg: str) -> None:
        """就地报错（红字承担语义，不加前缀符号，§3.6 不用 emoji）。"""
        self.error_label.setText(msg)
        self.error_label.show()
