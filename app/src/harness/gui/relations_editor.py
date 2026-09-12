"""人际关系编辑器（《人际关系与场景推进》§6.1/§6.2）——两栏，照信息库编辑器那套壳。

一个角色一张表：左栏一行一个关系（姓名 + 亲密度 + 一句话），右栏是选中那一行的详情
（姓名 / 性别 / 亲密度 / 关系描述 / 相处模式 / 指向的条目）。五件事在这里定下来：

1. **保存只走既有存储层**：`relations.render_relations_json` 的渲染 + `knowledgestore.
   save_relations` 的原子写（临时文件 → fsync → `os.replace`）。本模块**绝不自己拼
   `relations.json`**——那是"往返逐字节一致"与"空值不写"两条纪律的唯一实现处，抄一份
   等于下次改字段序时漏掉这里。读也是 `load_relations`（坏文件退成空表 + 警告，
   绝不半读），故"读不干净的表"永远写不回用户文件——**这句话由 `_writable_table` 落实**：
   一切写盘动作（新建/保存/删除）都先过那道闸，这次读盘有警告就一个字节都不落。少了它，
   `new_relation` 会拿"坏文件退成的空表"upsert 一行再整份写回——用户手写的其余几行当场
   消失，且没有备份（写盘是 `os.replace`）。
2. **非法值就地报错且一个字节都不落盘**。亲密度是硬的 −100~100（§6.1），非数字/越界在
   **写盘之前**就被挡下：夹取会让"想要 −200"与"文件里写着 −100"在外观上完全一样，用户
   再也查不出数据被谁动过（同 `relations` 模块 docstring 第 1 条）。姓名为空、改名撞上
   已有的人同样拦下——姓名就是键，两行同名的那一刻整张表就自毁了。
3. **指向的键要能当场核对**：右栏只读显示 `Relation.entry_key`（§6.5：有 `link` 以它为准，
   否则约定 = 姓名），并说明它在不在库里。核对走**既有的库读取路径**（`knowledgestore.
   load_library` 的键集合），**不自己拼 `entries/<键>.md`**——条目文件才是真相源，而"什么
   算一条条目"（坏文件跳过、大小写、索引顺序）只有存储层说了算。
4. **样式只有一处来源**：`theme.dialog_qss` + `library.palette()`，零字面颜色、零 emoji
   （§3.6）。微件助手（`_label` / `_primary` / `_ghost` / `_hairline` / `_mono`）一律复用
   `gui.library` 那一套——两份实现迟早漂成两种观感。
5. **不在这个界面里的字段一个都不动**：`link`（那是手写的指路牌）、`updated_at` 与
   `origin`（"何时、因何而变"）原样带回。编辑器抹掉一个自己都显示不了的字段，用户只会
   发现"我手写的那一行怎么少了一段"。

6. **改动落在哪一份、什么时候进戏，要说在界面上**：这个界面编的是角色**本体**的关系表
   （`<库根>/characters/<角色名>/relations.json`），而正在跑的那场戏读的是**开场时建的副本**
   （`knowledgetools._read_dir` 副本优先，§5.1/§6.2）。故改动要到**下次开场**才进提示词，
   戏演到一半改这里是不会当场生效的——不说清这一点，用户最自然的用法（"戏跑着、发现 A 对
   B 的态度不对 → 打开「关系…」改掉 → 保存"）看起来就像编辑器坏了：保存成功、左栏也立刻
   变新值，戏里却一个字不变。那条说明就是左栏那句 `hint.relations_scope`（与信息库编辑器
   同一处境，那边也一样是本体/副本两份）。

选择器 `RelationsPickerDialog` 复用信息库编辑器那一个（`LibraryPickerDialog`），只把可选的
库收窄到**角色库**：关系表只住在角色的信息库里（引擎的 `KnowledgeAccess._own_dir` 认的就是
`<库根>/characters/<角色名>`），列一座广域库出来让用户去编它的关系表，那个文件没人会读。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QSpinBox, QVBoxLayout, QWidget,
)

from .. import knowledgestore as store
from ..knowledge import entry_key_error
from ..relations import (
    CLOSENESS_MAX, CLOSENESS_MIN, Relation, RelationTable, _closeness_error)
from .knowledge_editor import (
    LibraryPickerDialog, _placeholder, _root_of, list_libraries)
from .library import (
    _danger, _ghost, _hairline, _label, _mono, _primary, app_theme, confirm,
    palette, translator)
from .theme import dialog_qss


def closeness_value(text: str) -> int:
    """右栏那个数值框的文本 → 亲密度整数；读不出来 → `ValueError`（中文，直接给用户看）。

    校验**只有一处实现**：`relations._closeness_error`——它就是 `Relation` 字段守门用的
    那一个（越界 / 非数字 / bool 都拒），在这里再写一遍判断迟早与模型层漂开。QSpinBox
    正常输入时不会给出越界值，但"读到的文本"仍要过这一关：控件能被程序化填入（粘贴、
    将来的批量导入、测试），而**落盘的判据必须与模型层同一条**。
    """
    raw = str(text if text is not None else "").strip()
    problem = _closeness_error(raw)
    if problem:
        raise ValueError(problem)
    return int(raw)


class RelationsPickerDialog(LibraryPickerDialog):
    """信息库选择器的**角色库**版：关系表只住在角色库里（§6.2），故不列广域库。

    只改两处（列表来源与两处文案），壳、新建库、打开钮全复用——"什么算一座库"的口径
    仍然只有 `list_libraries` 一份。
    """

    #: 标题与提示语换成本功能自己的（基类按这两个类属性取字，见 `LibraryPickerDialog`）。
    _TITLE_KEY = "dlg.relations_pick_title"
    _HINT_KEY = "hint.relations_pick"

    def list_for(self) -> list:
        """本选择器要列的库：只留角色库（广域库里的关系表没有任何人读）。"""
        return [info for info in list_libraries(self._root)
                if info.kind == "character"]

    def __init__(self, libraries_root: Path | str, parent: QWidget | None = None):
        super().__init__(libraries_root, parent)
        # 新建库的类型默认落在角色库（关系表要住的正是那一种）。
        index = self.new_kind_combo.findData("character")
        if index >= 0:
            self.new_kind_combo.setCurrentIndex(index)


class RelationsEditorDialog(QDialog):
    """一个角色一张关系表的两栏编辑器（§6.1）。

    开一座库（`lib_dir`，正式布局是 `<库根>/characters/<角色名>`）并把它编到磁盘上——
    保存/新建/删除全部经 `knowledgestore.save_relations`，本模块不自己写盘。

    库根是可注入参数（缺省按 `<库目录>` 往上两级推断）；测试一律注入 tmp，绝不碰仓库的
    `app/libraries/`。

    程序化填充用：library_label / relation_list / new_name_edit / new_relation_btn /
    delete_relation_btn / name_edit / gender_edit / closeness_spin / description_edit /
    mode_edit / key_label / key_status_label / save_btn / close_btn / error_label。
    """

    def __init__(self, lib_dir: Path | str, *, libraries_root: Path | str | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        t = translator()
        self._lib_dir = Path(lib_dir)
        self._root = (Path(libraries_root) if libraries_root is not None
                      else _root_of(self._lib_dir))
        self._table = RelationTable()
        #: 当前选中那一行的名字（= 键）；没选中 → None。
        self._name: str | None = None
        #: 当前那一行的 `link`（界面不显示也不改，保存时原样带回，见模块 docstring 第 5 条）。
        self._link = ""
        #: 右栏填进去时的那几个值（判"有没有没保存的改动"的基准；没选中 → None）。
        self._loaded: tuple | None = None
        self._reloading = False        # 重绘列表期间不响应选中变化（防自转）
        self.setStyleSheet(dialog_qss(app_theme()))
        self.setWindowTitle(t.t("dlg.relations_title", name=self._lib_dir.name))
        self.setMinimumSize(820, 560)

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
        # 改动落在本体、下次开场才进戏（见模块 docstring 第 6 条）：这句话是给用户的唯一提示，
        # 少了它，戏演到一半改这里就是"保存成功但戏里一个字不变"。
        self.scope_label = _label(t.t("hint.relations_scope"), "hint")
        v.addWidget(self.scope_label)
        v.addWidget(_label(t.t("grp.relation_rows"), "sect"))

        self.relation_list = QListWidget()
        self.relation_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.relation_list.itemSelectionChanged.connect(self._on_selection_changed)
        v.addWidget(self.relation_list, 1)

        self.new_name_edit = QLineEdit()
        self.new_name_edit.setPlaceholderText(t.t("label.please_relation_name"))
        self.new_relation_btn = _ghost(t.t("btn.new_relation"))
        self.new_relation_btn.clicked.connect(self.new_relation)
        new_row = QHBoxLayout()
        new_row.setSpacing(6)
        new_row.addWidget(self.new_name_edit, 1)
        new_row.addWidget(self.new_relation_btn)
        v.addLayout(new_row)

        self.delete_relation_btn = _danger(t.t("btn.delete_relation"))
        self.delete_relation_btn.clicked.connect(self.delete_current)
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        btn_row.addWidget(self.delete_relation_btn)
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
        v.addWidget(_label(t.t("grp.relation_detail"), "sect"))

        v.addWidget(_label(t.t("field.relation_name"), "sect"))
        self.name_edit = QLineEdit()
        self.name_edit.textChanged.connect(self._refresh_key)
        v.addWidget(self.name_edit)

        v.addWidget(_label(t.t("field.relation_gender"), "sect"))
        self.gender_edit = QLineEdit()
        v.addWidget(self.gender_edit)

        v.addWidget(_label(t.t("field.relation_closeness"), "sect"))
        self.closeness_spin = QSpinBox()
        self.closeness_spin.setRange(CLOSENESS_MIN, CLOSENESS_MAX)
        self.closeness_spin.setAlignment(Qt.AlignmentFlag.AlignRight
                                         | Qt.AlignmentFlag.AlignVCenter)
        v.addWidget(self.closeness_spin)

        v.addWidget(_label(t.t("field.relation_description"), "sect"))
        self.description_edit = QLineEdit()
        v.addWidget(self.description_edit)

        v.addWidget(_label(t.t("field.relation_mode"), "sect"))
        self.mode_edit = QLineEdit()
        v.addWidget(self.mode_edit)

        v.addWidget(_label(t.t("field.relation_entry_key"), "sect"))
        self.key_label = _mono("", color=palette().text)
        v.addWidget(self.key_label)
        self.key_status_label = _label("", "hint")
        v.addWidget(self.key_status_label)

        self.error_label = QLabel("")
        self.error_label.setObjectName("error")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        v.addWidget(self.error_label)
        v.addStretch(1)

        foot = QHBoxLayout()
        foot.setSpacing(8)
        foot.addStretch(1)
        self.close_btn = _ghost(t.t("btn.close"))
        self.close_btn.clicked.connect(self.reject)
        self.save_btn = _primary(t.t("btn.save"))
        self.save_btn.clicked.connect(self.save_relation)
        foot.addWidget(self.close_btn)
        foot.addWidget(self.save_btn)
        v.addLayout(foot)
        return right

    # ---------------------------------------------------------------- 读盘/重绘
    def reload(self) -> None:
        """重新读盘并重绘两栏（保存/新建/删除之后都走它）。

        读盘一律经 `knowledgestore.load_relations`：坏表退成空表 + 警告，而那条警告
        **必须**摆到用户面前（手写的表是唯一会坏的那一份，闷声不响等于"他忽然一个熟人
        都不认得了"却没人说得出为什么）。
        """
        t = translator()
        loaded = store.load_relations(self._lib_dir)
        self._table = loaded.table

        self._name = None
        self._reloading = True
        self.relation_list.blockSignals(True)
        self.relation_list.clear()
        for row in self._table:
            item = QListWidgetItem(self._row_label(row))
            # 整行挂 UserRole（QListWidgetItem 不可哈希，做不了 dict 键；它认任意对象）。
            item.setData(Qt.ItemDataRole.UserRole, row.name)
            item.setToolTip(f"{row.name}　{t.t('field.relation_closeness')} {row.closeness}")
            self.relation_list.addItem(item)
        if not len(self._table):
            _placeholder(self.relation_list, t.t("label.no_relations"))
        self.relation_list.blockSignals(False)
        self._reloading = False

        self.library_label.setText(
            f"《{self._lib_dir.name}》　{t.t('kind.character_lib')}")
        self._clear_detail()
        if loaded.warnings:
            self._error("；".join(loaded.warnings))
        else:
            self.error_label.hide()

    def _writable_table(self) -> RelationTable | None:
        """读一次盘并判"这张表能不能写"：读不干净 → 就地报错、返回 None（模块 docstring 第 1 条）。

        `load_relations` 的契约是"坏文件退成空表 + 警告，绝不半读"。那张空表**绝不能**被
        拿去 upsert 一行再整份写回——那正是把手写的其余几行静默吃掉。故一切写盘动作
        （新建 / 保存 / 删除）都在这里过同一道闸；返回的 `None` 就是"别写"。
        """
        loaded = store.load_relations(self._lib_dir)
        if loaded.warnings:
            # 把原因一起带上：这条错误会覆盖掉 `reload()` 摆出来的那句警告，不带原因的话
            # 用户点一下「新建」就再也看不到"到底哪坏了"。
            self._error("；".join([translator().t("err.relation_table_unreadable"),
                                   *loaded.warnings]))
            return None
        return loaded.table

    def _row_label(self, row: Relation) -> str:
        """左栏一行：`姓名　亲密度　一句话`（描述优先，没有就用相处模式）。"""
        note = " ".join((row.description or "").split()) or \
               " ".join((row.mode or "").split())
        parts = [" ".join((row.name or "").split()), str(int(row.closeness))]
        if note:
            parts.append(note)
        return "　".join(parts)

    def rows(self) -> list[str]:
        """左栏当前的行名（按显示顺序）——测试与"当前在显示什么"的判据。"""
        return [str(self.relation_list.item(i).data(Qt.ItemDataRole.UserRole))
                for i in range(self.relation_list.count())
                if self.relation_list.item(i).data(Qt.ItemDataRole.UserRole)]

    def current_name(self) -> str | None:
        """当前选中那一行的姓名（没选中/占位行 → None）。"""
        return self._name

    def select_name(self, name: str) -> bool:
        """按姓名选中左栏的一行；表里没有 → False（不抛）。"""
        for i in range(self.relation_list.count()):
            if self.relation_list.item(i).data(Qt.ItemDataRole.UserRole) == name:
                self.relation_list.setCurrentItem(self.relation_list.item(i))
                return True
        return False

    def _on_selection_changed(self) -> None:
        """选中变了 → 重填右栏（真有没保存的改动时**先问一句**）。重绘期间不响应，免得自转。"""
        if self._reloading:
            return
        item = self.relation_list.currentItem()
        name = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if (name != self._name) and not self._confirm_discard():
            self._reselect(self._name)
            return
        self._fill_detail(str(name) if name else None)

    # ---------------------------------------------------------------- 右栏内容
    def _clear_detail(self) -> None:
        """清空右栏（没选中 / 列表刚重绘完）：编辑框停用、给一句「先选一行」。"""
        self._name = None
        self._link = ""
        self._loaded = None
        for widget in (self.name_edit, self.gender_edit, self.description_edit,
                       self.mode_edit):
            widget.setText("")
        self.closeness_spin.setValue(0)
        for widget in (self.name_edit, self.gender_edit, self.description_edit,
                       self.mode_edit, self.closeness_spin):
            widget.setEnabled(False)
        self.save_btn.setEnabled(False)
        # 键与状态一起清掉：留着上一行的"这条在信息库里"会让人以为右栏还是那一行。
        self.key_label.setText("")
        self.key_status_label.setText(translator().t("label.select_relation"))

    def _fill_detail(self, name: str | None) -> None:
        """右栏：姓名/性别/亲密度/描述/模式 + 指向的条目；没选中就清空并给出提示。"""
        row = self._table.get(name) if name else None
        if row is None:
            self._clear_detail()
            return
        for widget in (self.name_edit, self.gender_edit, self.description_edit,
                       self.mode_edit, self.closeness_spin):
            widget.setEnabled(True)
        self.save_btn.setEnabled(True)
        self._name = row.name
        self._link = row.link
        self.name_edit.setText(row.name)
        self.gender_edit.setText(row.gender)
        self.closeness_spin.setValue(int(row.closeness))
        self.description_edit.setText(row.description)
        self.mode_edit.setText(row.mode)
        # 记下"填进去的就是这些"——之后与它不一致即为有未保存的改动（见 _is_dirty）。
        self._loaded = (row.name, row.gender, int(row.closeness),
                        row.description, row.mode)
        self._refresh_key()

    # ---------------------------------------------------------------- 键核对 --
    def entry_key(self) -> str:
        """这一行指向的信息库键（§6.5）：给了 `link` 以它为准，否则约定 = 当前姓名框里的名字。

        姓名框可编辑（改名是真实需要），故键要**跟着它动**——显示一个过时的键比不显示更坏。
        """
        name = self.name_edit.text().strip()
        return (self._link or "").strip() or name

    def entry_key_exists(self) -> bool:
        """这个键在库里有没有这一条（走 `load_library` 那条既有读取路径，不拼文件路径）。"""
        key = self.entry_key()
        if not key or entry_key_error(key):
            return False
        return key in store.load_library(self._lib_dir).library.entries

    def _refresh_key(self) -> None:
        """把"指向哪条、在不在库里"重新算一遍（选中变化与姓名框每次改动都调它）。"""
        t = translator()
        key = self.entry_key()
        self.key_label.setText(f"{t.t('label.relation_entry_key')}{key}" if key else "")
        if not key:
            self.key_status_label.setText(translator().t("label.select_relation"))
            return
        self.key_status_label.setText(
            t.t("value.entry_key_found") if self.entry_key_exists()
            else t.t("value.entry_key_missing"))

    # ---------------------------------------------------------------- 脏检查 --
    def _is_dirty(self) -> bool:
        """右栏相对**填进去时**有没有改动（没选中 → False）。"""
        if self._name is None or self._loaded is None:
            return False
        return (self.name_edit.text().strip(), self.gender_edit.text(),
                self.closeness_spin.value(), self.description_edit.text(),
                self.mode_edit.text()) != self._loaded

    def _confirm_discard(self) -> bool:
        """有没保存的改动时问一句；点「否」= 留在原地（True = 可以往下走）。

        保存是右栏唯一的落盘动作，而"点一下别的行"就把五个框整份覆盖——一次误点就丢掉
        刚敲的内容。与信息库编辑器同一口径（那里也是 `confirm`）。
        """
        if not self._is_dirty():
            return True
        t = translator()
        return confirm(self, t.t("title.discard_edits"),
                       t.t("dlg.confirm_discard_edits", title=self._name or ""))

    def _reselect(self, name: str | None) -> None:
        """把选中拨回 `name` 那一行，**不**因此重填右栏（丢弃确认被拒时用）。"""
        if name is None:
            return
        self._reloading = True
        try:
            self.select_name(name)
        finally:
            self._reloading = False

    # ---------------------------------------------------------------- 动作 --
    def save_relation(self) -> bool:
        """把右栏写回那一行并落盘（经 `knowledgestore.save_relations`）；成功 → True。

        顺序是刻意的：**先校验、再读盘、后写盘**。校验不过就地报错、一个字节都不动；
        写盘前重新读一次表（`self._table` 可能已经过时），免得拿陈旧的内存整份覆盖掉
        别处的改动。`link` / `updated_at` / `origin` 原样带回（见模块 docstring 第 5 条）。

        改名 = 改键：先摘掉旧名那一行再放新的（`remove` + `upsert`），中途撞上已有人名
        则整件事作废——绝不悄悄改名，也绝不留下两行同名。
        """
        t = translator()
        if self._name is None:
            self._error(t.t("err.select_relation"))
            return False
        name = self.name_edit.text().strip()
        if not name:
            self._error(t.t("err.relation_name_required"))
            return False
        try:
            closeness = closeness_value(self.closeness_spin.cleanText())
        except ValueError as exc:
            self._error(str(exc))            # 越界/非数字：中文原话，直接就地说清边界
            return False
        table = self._writable_table()
        if table is None:                    # 表读不干净 → 一个字节都不落（模块 docstring 第 1 条）
            return False
        current = table.get(self._name)
        if current is None:
            self.reload()                    # reload 会清掉错误框，故先重绘再报错
            self._error(t.t("err.relation_gone"))
            return False
        if name != self._name and table.get(name) is not None:
            self._error(t.t("err.relation_exists", name=name))
            return False
        payload = current.model_dump()
        payload.update(name=name, gender=self.gender_edit.text().strip(),
                       closeness=closeness,
                       description=self.description_edit.text().strip(),
                       mode=self.mode_edit.text().strip())
        try:
            row = Relation.model_validate(payload)      # 构造即过守门，双保险
            if name != self._name:
                table.remove(self._name)
            table.upsert(row)
            store.save_relations(self._lib_dir, table)
        except (ValueError, OSError) as exc:
            self._error(t.t("err.relation_save_failed", exc=exc))
            return False
        self.reload()
        self.select_name(row.name)
        return True

    def new_relation(self) -> bool:
        """按「新建关系的姓名」建一行空关系，成功即选中它继续编。

        空名 / 表里已有同名 → 就地报错、**一个字节都不落盘**，绝不静默改名（姓名是键）。
        其余字段留空、亲密度 0——"没写"不是错（§6.1 除了姓名之外都可以空着）。
        """
        t = translator()
        name = self.new_name_edit.text().strip()
        if not name:
            self._error(t.t("err.relation_name_required"))
            return False
        table = self._writable_table()
        if table is None:                    # 读成空表再写回会抹掉手写的其余几行
            return False
        if table.get(name) is not None:
            self._error(t.t("err.relation_exists", name=name))
            return False
        # 拦到这一步都没问题、真的会 reload 右栏了，才问那句"放弃未保存的改动"
        # （先问后拦的话，用户答完"放弃"却因为名字非法什么都没发生，白吓一跳）。
        if not self._confirm_discard():
            return False
        try:
            row = Relation(name=name)               # 构造即过守门，双保险
            table.upsert(row)
            store.save_relations(self._lib_dir, table)
        except (ValueError, OSError) as exc:
            self._error(t.t("err.relation_save_failed", exc=str(exc)))
            return False
        self.new_name_edit.clear()
        self.reload()
        self.select_name(row.name)
        return True

    def delete_current(self) -> bool:
        """删掉选中的那一行（先确认）：只动这一行，其余关系与库里的条目一个不碰。"""
        t = translator()
        if self._name is None:
            self._error(t.t("err.select_relation"))
            return False
        if not confirm(self, t.t("title.delete_relation"),
                       t.t("dlg.confirm_delete_relation", name=self._name)):
            return False
        table = self._writable_table()
        if table is None:                    # 表读不干净 → 删除同样会整份覆盖，一并拦下
            return False
        if not table.remove(self._name):
            self.reload()
            self._error(t.t("err.relation_gone"))
            return False
        try:
            store.save_relations(self._lib_dir, table)
        except OSError as exc:
            self._error(t.t("err.relation_save_failed", exc=exc))
            return False
        self.reload()
        return True

    def _error(self, msg: str) -> None:
        """就地报错（红字承担语义，不加前缀符号，§3.6 不用 emoji）。"""
        self.error_label.setText(msg)
        self.error_label.show()
