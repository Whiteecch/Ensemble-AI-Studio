"""人际关系编辑器（《人际关系与场景推进》§6.1/§6.2）与主窗口入口——离屏、确定性、不联网。

钉住的契约（每条都是设计文档里的硬要求，不是随手加的断言）：

  · 一个角色一张表：左栏一行一个关系（姓名 + 亲密度 + 一句话），右栏是选中那一行的
    详情（姓名 / 性别 / 亲密度 / 关系描述 / 相处模式 / 指向的条目）；
  · **保存只走既有存储层**：`relations` 的渲染 + `knowledgestore.save_relations` 的原子写
    （本模块绝不自己拼那个文件）；改完必须能重新 `load_relations` **逐字读回**；
  · **非法值就地报错且一个字节都不落盘**（亲密度越界 / 非数字 / 姓名为空 / 重名）——
    亲密度是硬的 −100~100，夹取会让"模型/用户想要 −200"与"文件里写着 −100"外观完全一样；
  · **指向的键要能当场核对**：右栏那条只读显示走**既有的库读取路径**（`load_library`），
    不自己拼 `entries/<键>.md`；存在与否一眼看得见；
  · **菜单键七语言齐**（硬要求）：主窗口多一个「关系…」入口，`menu.relations` 七种语言
    都有字（缺了哪一种，那一种的菜单就露中文）；
  · 没有关系表 / 没有信息库时，既有界面行为**逐字节不变**。

全部 tmp_path；库根一律走**可注入**参数，绝不碰仓库的 `app/libraries/` 与真实用户目录。
若环境未装 PySide6（可选依赖 gui），整模块 skip。
"""
import json
import os
import re
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Qt, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from harness import knowledgestore as store  # noqa: E402
from harness.gui import main_window as mw_mod  # noqa: E402
from harness.gui import relations_editor as re_mod  # noqa: E402
from harness.gui.library import app_theme  # noqa: E402
from harness.gui.main_window import AppConfig, MainWindow  # noqa: E402
from harness.gui.relations_editor import (  # noqa: E402
    RelationsEditorDialog, closeness_value)
from harness.gui.theme import PALETTES, dialog_qss  # noqa: E402
from harness.i18n import CATALOGS, LANGUAGES  # noqa: E402
from harness.knowledge import Entry  # noqa: E402
from harness.relations import Relation, RelationTable  # noqa: E402

#: emoji 扫描（§3.6 无 AI 味）：几何/杂项符号区 + 变体选择符 + 彩色 emoji。
_EMOJI_RE = re.compile("[⌀-⏿☀-➿⬀-⯿️\U0001F000-\U0001FAFF]")


@pytest.fixture(scope="module")
def qapp():
    """模块级复用单一 QApplication（Qt 只允许每进程一个）。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


# --------------------------------------------------------------------- 素材 --
def _lib_dir(tmp_path: Path, name: str = "甲") -> Path:
    """一座角色库（正式布局 `<根>/characters/<名>`），返回它的库目录。

    走正式的 `save_library`：条目的真相源是条目文件，键的存在与否要能在**真实的库**上验。
    """
    root = tmp_path / "libraries"
    path = store.library_dir("character", name, root=root)
    store.save_library(store.Library(name=name, scope="character", owner=name), path)
    return path


def _lib_root(lib_dir: Path) -> Path:
    """库目录 → 它的库根（`<根>/characters/<名>` 往上两级）。"""
    return lib_dir.parent.parent


def _add_entry(lib_dir: Path, key: str) -> None:
    lib = store.load_library(lib_dir).library
    lib.upsert(Entry(key=key, title=key, summary="一句话"))
    store.save_library(lib, lib_dir)


def _write_table(lib_dir: Path, rows: list[Relation]) -> None:
    store.save_relations(lib_dir, RelationTable(relations=rows))


def _table(lib_dir: Path) -> RelationTable:
    return store.load_relations(lib_dir).table


def _bytes(lib_dir: Path) -> bytes:
    return store.relations_path(lib_dir).read_bytes()


def _texts(dlg) -> list[str]:
    """整棵微件树上所有可见文本（断言"界面上看得见什么"的朴素办法）。"""
    from PySide6.QtWidgets import QLabel, QLineEdit

    out: list[str] = []
    for child in dlg.findChildren(QLabel):
        out.append(child.text())
    for child in dlg.findChildren(QLineEdit):
        out.append(child.text())
        out.append(child.placeholderText())
    for i in range(dlg.relation_list.count()):
        out.append(dlg.relation_list.item(i).text())
    return out


def _select(dlg, name: str) -> bool:
    for i in range(dlg.relation_list.count()):
        if dlg.relation_list.item(i).data(Qt.ItemDataRole.UserRole) == name:
            dlg.relation_list.setCurrentItem(dlg.relation_list.item(i))
            return True
    return False


# ============================================================ 1. 左栏/右栏
def test_editor_lists_one_row_per_relation(qapp, tmp_path):
    """一个角色一张表：左栏一行一个关系，行文本给出姓名 + 亲密度 + 一句话。"""
    lib = _lib_dir(tmp_path)
    _write_table(lib, [Relation(name="乙", gender="女", closeness=60,
                                description="同门师兄妹"),
                       Relation(name="丙", closeness=-30, mode="见面就掐")])
    dlg = RelationsEditorDialog(lib, libraries_root=_lib_root(lib))

    labels = [dlg.relation_list.item(i).text() for i in range(dlg.relation_list.count())]
    assert len(labels) == 2
    assert "乙" in labels[0] and "60" in labels[0] and "同门师兄妹" in labels[0]
    assert "丙" in labels[1] and "-30" in labels[1] and "见面就掐" in labels[1]


def test_editor_shows_the_detail_of_the_selected_row(qapp, tmp_path):
    """选中一行 → 右栏填上它的姓名/性别/亲密度/描述/模式，亲密度控件停在那个值上。"""
    lib = _lib_dir(tmp_path)
    _write_table(lib, [Relation(name="乙", gender="女", closeness=60,
                                description="同门师兄妹", mode="遇事互相照应")])
    dlg = RelationsEditorDialog(lib, libraries_root=_lib_root(lib))

    assert _select(dlg, "乙")
    assert dlg.current_name() == "乙"
    assert dlg.name_edit.text() == "乙"
    assert dlg.gender_edit.text() == "女"
    assert dlg.closeness_spin.value() == 60
    assert dlg.description_edit.text() == "同门师兄妹"
    assert dlg.mode_edit.text() == "遇事互相照应"


def test_editor_with_no_rows_shows_a_placeholder(qapp, tmp_path):
    """空表：列表给一句占位、右栏禁用（空着一个框会让人以为坏了）。"""
    lib = _lib_dir(tmp_path)
    dlg = RelationsEditorDialog(lib, libraries_root=_lib_root(lib))
    assert "还没有" in "\n".join(_texts(dlg))
    assert dlg.current_name() is None
    assert dlg.save_relation() is False, "没选中时保存应被拦下"


def test_editor_reports_an_unreadable_relation_table(qapp, tmp_path):
    """读不出来的表要**摆到用户面前**（手写的表是唯一会坏的那一份）：空列表 + 原因。"""
    lib = _lib_dir(tmp_path)
    store.relations_path(lib).write_text("{ 这不是 JSON", encoding="utf-8")
    dlg = RelationsEditorDialog(lib, libraries_root=_lib_root(lib))
    joined = "\n".join(_texts(dlg))
    assert "关系表" in joined and "读不出来" in joined


def test_new_relation_refuses_to_overwrite_an_unreadable_table(qapp, tmp_path):
    """表读不干净时**拒绝写盘**：读成空表再 upsert 会把用户手写的其余几行整份抹掉。

    存储层的契约是"坏文件退成空表 + 警告，绝不半读"（`knowledgestore.load_relations` 与
    `relations.parse_relations_json` 两处都写死了），本模块的 docstring 第 1 条据此承诺
    "'读不干净的表'永远写不回用户文件"。可 `new_relation` 原来是拿那张空表 upsert 一行
    再整份 `save_relations`——手写时多一个逗号，点一下「新建」就把整张表覆盖成只剩新行，
    没有备份、也没有一句"这会覆盖"。这一条把那个承诺钉成可执行的：坏表 + 点「新建」→
    就地报错、文件**一个字节都不动**。
    """
    lib = _lib_dir(tmp_path)
    broken = ('{\n  "schema": 1,\n  "relations": [\n'
              '    {"name": "乙", "closeness": 80},\n'   # 末尾多一个逗号 = 坏 JSON
              '  ]\n}\n')
    store.relations_path(lib).write_text(broken, encoding="utf-8")
    before = _bytes(lib)
    dlg = RelationsEditorDialog(lib, libraries_root=_lib_root(lib))

    dlg.new_name_edit.setText("丙")
    assert dlg.new_relation() is False, "读不出来的表上不许新建"
    assert _bytes(lib) == before, "坏表原样留着，等用户去修——绝不被覆盖"
    assert "读不出来" in dlg.error_label.text(), \
        "要告诉用户为什么没成（这条错误会盖掉 reload 摆出来的那句警告，原因得一起带上）"


def test_new_relation_refuses_when_a_row_is_invalid(qapp, tmp_path):
    """同上一条，只是坏法换成"某一行字段越界"（合法 JSON、非法关系表）——结论一样。"""
    lib = _lib_dir(tmp_path)
    store.relations_path(lib).write_text(json.dumps(
        {"schema": 1, "relations": [{"name": "乙", "closeness": 80},
                                    {"name": "丙", "closeness": 150}]},
        ensure_ascii=False), encoding="utf-8")
    before = _bytes(lib)
    dlg = RelationsEditorDialog(lib, libraries_root=_lib_root(lib))

    dlg.new_name_edit.setText("丁")
    assert dlg.new_relation() is False
    assert _bytes(lib) == before, "越界那一行是手写笔误，不能连带把整张表抹掉"


def test_editor_states_where_the_change_lands_and_when_it_takes_effect(qapp, tmp_path):
    """编辑器要说清**改的是哪一份、什么时候进戏**（§6.2 的副本口径）。

    关系表与信息库条目一样：开场时把本体那份拷进本场副本，戏里读的是**副本**
    （`knowledgetools._read_dir` 副本优先）。故这个界面保存的是角色本体的关系表，
    正在跑的这场戏读的还是开场时那份副本——改动要到**下次开场**才进提示词。不说清这一点，
    用户最自然的用法（"戏跑着、发现 A 对 B 的态度不对 → 打开「关系…」改掉 → 保存"）会
    看起来像编辑器坏了：保存返回成功、左栏也立刻变成新值，戏里却一个字都不变。
    """
    lib = _lib_dir(tmp_path)
    _write_table(lib, [Relation(name="乙", closeness=10)])
    dlg = RelationsEditorDialog(lib, libraries_root=_lib_root(lib))
    joined = "\n".join(_texts(dlg))

    assert CATALOGS["zh-Hans"]["hint.relations_scope"] in joined, \
        "要在界面上说明改动落在本体表、下次开场才生效"


# ============================================================ 2. 保存往返 --
def test_save_writes_through_the_store_and_reads_back_verbatim(qapp, tmp_path):
    """改亲密度/描述/模式 → 保存 → 重新 `load_relations` **逐字读回**（走存储层的原子写）。"""
    lib = _lib_dir(tmp_path)
    _write_table(lib, [Relation(name="乙", gender="女", closeness=10,
                                description="泛泛之交")])
    dlg = RelationsEditorDialog(lib, libraries_root=_lib_root(lib))
    assert _select(dlg, "乙")

    dlg.closeness_spin.setValue(70)
    dlg.description_edit.setText("一起过过命")
    dlg.mode_edit.setText("说话不客气，出事第一个到")
    assert dlg.save_relation() is True

    row = _table(lib).get("乙")
    assert row is not None
    assert row.closeness == 70
    assert row.description == "一起过过命"
    assert row.mode == "说话不客气，出事第一个到"
    assert row.gender == "女", "没改的字段原样带回"


def test_save_keeps_the_link_and_the_other_fields_untouched(qapp, tmp_path):
    """`link`（§6.5 指向信息库的键）与 `updated_at` 不是这个界面能改的——原样保留。

    `.json` 是手写素材：编辑器抹掉一个自己都显示不了的字段，用户只会发现"我手写的那一行
    怎么少了一段"。
    """
    lib = _lib_dir(tmp_path)
    _write_table(lib, [Relation(name="绫", closeness=1, link="乙",
                                updated_at="2026-09-01T10:00:00+00:00")])
    dlg = RelationsEditorDialog(lib, libraries_root=_lib_root(lib))
    assert _select(dlg, "绫")
    dlg.closeness_spin.setValue(2)
    assert dlg.save_relation() is True

    row = _table(lib).get("绫")
    assert row.link == "乙"
    assert row.updated_at == "2026-09-01T10:00:00+00:00"


def test_rename_moves_the_row(qapp, tmp_path):
    """改姓名 = 改键：旧名那一行消失、新名那一行带上改过的内容（表里一行都不能多）。"""
    lib = _lib_dir(tmp_path)
    _write_table(lib, [Relation(name="乙", closeness=10)])
    dlg = RelationsEditorDialog(lib, libraries_root=_lib_root(lib))
    assert _select(dlg, "乙")

    dlg.name_edit.setText("萧铃")
    dlg.closeness_spin.setValue(80)
    assert dlg.save_relation() is True

    table = _table(lib)
    assert table.names() == ["萧铃"]
    assert table.get("萧铃").closeness == 80


def test_rename_onto_an_existing_name_is_refused_without_writing(qapp, tmp_path):
    """重名就地报错、**一个字节都不落盘**：姓名就是键，两行同名的那一刻表就自毁了。"""
    lib = _lib_dir(tmp_path)
    _write_table(lib, [Relation(name="乙", closeness=10),
                       Relation(name="丙", closeness=-10)])
    dlg = RelationsEditorDialog(lib, libraries_root=_lib_root(lib))
    assert _select(dlg, "乙")
    before = _bytes(lib)

    dlg.name_edit.setText("丙")
    assert dlg.save_relation() is False
    assert _bytes(lib) == before
    assert "已经有" in dlg.error_label.text()


# ============================================== 3. 非法值：报错且不落盘 --
def test_out_of_range_closeness_is_refused_without_writing(qapp, tmp_path):
    """越界亲密度就地报错、不落盘（−100~100 是硬的，绝不静默夹取）。

    控件是 QSpinBox（正常输入被它挡住），但"读到的文本"仍要过 `relations` 那一个校验器：
    夹取会让"想要 −200"与"文件里写着 −100"外观完全一样，用户再也查不出数据被谁动过。
    """
    lib = _lib_dir(tmp_path)
    _write_table(lib, [Relation(name="乙", closeness=10)])
    dlg = RelationsEditorDialog(lib, libraries_root=_lib_root(lib))
    assert _select(dlg, "乙")
    before = _bytes(lib)

    dlg.closeness_spin.lineEdit().setText("200")
    assert dlg.save_relation() is False
    assert "100" in dlg.error_label.text(), "报错要说清边界"
    assert _bytes(lib) == before, "报错就不该落盘"


def test_non_numeric_closeness_is_refused_without_writing(qapp, tmp_path):
    """非数字亲密度同样就地报错、不落盘。"""
    lib = _lib_dir(tmp_path)
    _write_table(lib, [Relation(name="乙", closeness=10)])
    dlg = RelationsEditorDialog(lib, libraries_root=_lib_root(lib))
    assert _select(dlg, "乙")
    before = _bytes(lib)

    dlg.closeness_spin.lineEdit().setText("abc")
    assert dlg.save_relation() is False
    assert "整数" in dlg.error_label.text()
    assert _bytes(lib) == before


def test_closeness_value_accepts_the_whole_legal_range_and_rejects_the_rest():
    """纯校验函数（界面与测试共用一处口径）：−100 与 100 合法，边界外与非数字报错。"""
    assert closeness_value("-100") == -100
    assert closeness_value("100") == 100
    assert closeness_value(" 7 ") == 7
    for bad in ("101", "-101", "abc", "", "1.5"):
        with pytest.raises(ValueError):
            closeness_value(bad)


def test_blank_name_is_refused_without_writing(qapp, tmp_path):
    """姓名为空 → 报错、不落盘（姓名是键，空名让整张表查不动）。"""
    lib = _lib_dir(tmp_path)
    _write_table(lib, [Relation(name="乙", closeness=10)])
    dlg = RelationsEditorDialog(lib, libraries_root=_lib_root(lib))
    assert _select(dlg, "乙")
    before = _bytes(lib)

    dlg.name_edit.setText("   ")
    assert dlg.save_relation() is False
    assert _bytes(lib) == before


# ==================================================== 4. 新建 / 删除 --
def test_new_relation_lands_on_disk(qapp, tmp_path):
    """新建一行 → 落盘（走存储层），并即可继续编它。"""
    lib = _lib_dir(tmp_path)
    _write_table(lib, [Relation(name="乙", closeness=10)])
    dlg = RelationsEditorDialog(lib, libraries_root=_lib_root(lib))

    dlg.new_name_edit.setText("丙")
    assert dlg.new_relation() is True
    assert _table(lib).names() == ["乙", "丙"]
    assert dlg.current_name() == "丙", "新建完就选中它，接着编"


def test_new_relation_refuses_a_blank_or_duplicate_name(qapp, tmp_path):
    """空名 / 重名 → 就地报错且**不落盘**（绝不悄悄改名或建出第二条同名行）。"""
    lib = _lib_dir(tmp_path)
    _write_table(lib, [Relation(name="乙", closeness=10)])
    before = _bytes(lib)
    dlg = RelationsEditorDialog(lib, libraries_root=_lib_root(lib))

    dlg.new_name_edit.setText("  ")
    assert dlg.new_relation() is False
    dlg.new_name_edit.setText("乙")
    assert dlg.new_relation() is False
    assert _bytes(lib) == before


def test_delete_removes_the_row_from_disk(qapp, tmp_path, monkeypatch):
    """删除（确认后）：那一行从表里消失并落盘，其余行一个不动。"""
    lib = _lib_dir(tmp_path)
    _write_table(lib, [Relation(name="乙", closeness=10),
                       Relation(name="丙", closeness=-10)])
    dlg = RelationsEditorDialog(lib, libraries_root=_lib_root(lib))
    assert _select(dlg, "乙")
    monkeypatch.setattr(re_mod, "confirm", lambda *a, **k: True)

    assert dlg.delete_current() is True
    assert _table(lib).names() == ["丙"]


def test_delete_refused_by_the_confirmation_keeps_the_row(qapp, tmp_path, monkeypatch):
    """确认框点「否」→ 什么都不做（删除是破坏性的，默认答案是"别删"）。"""
    lib = _lib_dir(tmp_path)
    _write_table(lib, [Relation(name="乙", closeness=10)])
    before = _bytes(lib)
    dlg = RelationsEditorDialog(lib, libraries_root=_lib_root(lib))
    assert _select(dlg, "乙")
    monkeypatch.setattr(re_mod, "confirm", lambda *a, **k: False)

    assert dlg.delete_current() is False
    assert _bytes(lib) == before


# ================================================ 5. 指向的条目：核对 --
def test_key_display_reports_an_existing_entry(qapp, tmp_path):
    """指向的键**在库里** → 只读显示那个键，并说明它在库里（走 `load_library` 那条路）。"""
    lib = _lib_dir(tmp_path)
    _add_entry(lib, "乙")
    _write_table(lib, [Relation(name="乙", closeness=10)])
    dlg = RelationsEditorDialog(lib, libraries_root=_lib_root(lib))
    assert _select(dlg, "乙")

    assert dlg.entry_key() == "乙"
    assert "乙" in dlg.key_label.text()
    assert dlg.key_status_label.text() == CATALOGS["zh-Hans"]["value.entry_key_found"]


def test_key_display_reports_a_missing_entry(qapp, tmp_path):
    """键不在库里 → 如实说"库里还没有这条"（不自己拼路径去猜，也不假装存在）。"""
    lib = _lib_dir(tmp_path)
    _write_table(lib, [Relation(name="查无此人", closeness=10)])
    dlg = RelationsEditorDialog(lib, libraries_root=_lib_root(lib))
    assert _select(dlg, "查无此人")

    assert dlg.entry_key() == "查无此人"
    assert dlg.key_status_label.text() == CATALOGS["zh-Hans"]["value.entry_key_missing"]


def test_key_display_follows_the_explicit_link(qapp, tmp_path):
    """有 `link` 时以它为准（§6.5 的约定：条目名与称呼对不上就用 link 指过去）。"""
    lib = _lib_dir(tmp_path)
    _add_entry(lib, "乙")
    _write_table(lib, [Relation(name="师妹", closeness=10, link="乙")])
    dlg = RelationsEditorDialog(lib, libraries_root=_lib_root(lib))
    assert _select(dlg, "师妹")

    assert dlg.entry_key() == "乙"
    assert dlg.key_status_label.text() == CATALOGS["zh-Hans"]["value.entry_key_found"]


# ==================================================== 6. 样式（§3.6） --
@pytest.mark.parametrize("theme", list(PALETTES))
def test_stylesheet_comes_from_the_themed_sheet(qapp, tmp_path, theme):
    """四套主题下弹窗样式都取自 `theme.dialog_qss`——不留任何字面颜色。"""
    app = QApplication.instance()
    app.setProperty("theme", theme)
    try:
        lib = _lib_dir(tmp_path / theme)
        _write_table(lib, [Relation(name="乙", closeness=10)])
        dlg = RelationsEditorDialog(lib, libraries_root=_lib_root(lib))
        assert dlg.styleSheet() == dialog_qss(theme)
        assert PALETTES[theme].window_bg in dlg.styleSheet()
    finally:
        app.setProperty("theme", "")


def test_no_literal_colour_and_no_emoji_in_the_source():
    """零字面颜色、零 emoji（§3.6）：源码里不许出现 `#rrggbb`，也不许出现表情符号。"""
    src = Path(re_mod.__file__).read_text(encoding="utf-8")
    assert re.findall(r"#[0-9a-fA-F]{6}\b", src) == []
    assert _EMOJI_RE.search(src) is None


def test_editor_module_reuses_the_library_widget_helpers():
    """微件与配色只有一份：本模块**复用** `gui.library` 的助手，不另造一套。"""
    src = Path(re_mod.__file__).read_text(encoding="utf-8")
    for helper in ("_label", "_primary", "_ghost", "_hairline", "translator",
                   "palette", "dialog_qss"):
        assert helper in src, helper


# ================================================ 7. 主窗口入口 + i18n --
class _FakeWorker(QObject):
    """替身 worker：只给 MainWindow 要连的信号与 can_cast（不起线程）。"""

    sig_scene_info = Signal(dict)
    sig_message = Signal(dict)
    sig_metrics = Signal(dict)
    sig_status = Signal(str)
    sig_finished = Signal()
    sig_dynamics = Signal(dict)
    sig_think = Signal(dict)
    sig_narration = Signal(dict)
    sig_retracted = Signal(int)
    sig_cast = Signal(dict)

    def can_cast(self) -> bool:
        return False

    def set_language(self, code: str) -> None:
        pass


def _write_card(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / f"{name}.json"
    p.write_text(json.dumps({"name": name, "personality": {"描述": "示例"}},
                            ensure_ascii=False), encoding="utf-8")
    return p


def _window(tmp_path: Path, root: Path) -> MainWindow:
    """真实 MainWindow（替身 worker，不 show、不起线程）+ 素材全在 tmp 里。"""
    cdir, sdir = tmp_path / "characters", tmp_path / "scenes"
    cards = [_write_card(cdir, "甲")]
    sdir.mkdir(parents=True, exist_ok=True)
    models = tmp_path / "models.yaml"
    models.write_text("think: {backend: stub, model: stub, params: {}}\n"
                      "speak: {backend: stub, model: stub, params: {}}\n",
                      encoding="utf-8")
    cfg = AppConfig(scene=None, characters=cards, models=models,
                    bid=tmp_path / "bid.yaml", run_root=tmp_path / "runs",
                    live=False, api_key=None)
    return MainWindow(_FakeWorker(), cfg, libraries_root=root)


def test_mainwindow_has_a_relations_menu_item(qapp, tmp_path):
    """顶栏多一个「关系…」入口（既有四段一字不动）。"""
    root = tmp_path / "libraries"
    win = _window(tmp_path, root)
    texts = [a.text() for a in win.menuBar().actions()]
    assert texts[:4] == ["设置", "场景", "角色", "信息库…"], "既有四段不许漂移"
    assert texts[4] == "关系…"
    assert win._action_relations.isEnabled() is True
    assert win._action_relations.toolTip(), "要有悬停说明"


def test_relations_menu_key_is_translated_in_all_seven_languages():
    """菜单键**七语言齐**（硬要求）：缺哪一种，那一种的菜单就露中文。"""
    assert CATALOGS["zh-Hans"]["menu.relations"] == "关系…"
    for code in LANGUAGES:
        catalog = CATALOGS.get(code, {})
        assert catalog.get("menu.relations", "").strip(), f"{code} 缺 menu.relations"


def test_menu_item_is_retranslated_on_language_switch(qapp, tmp_path):
    """切语言即重译（与其余菜单项同一套 `_retranslate_menus`）。"""
    root = tmp_path / "libraries"
    win = _window(tmp_path, root)
    win.apply_language("en", persist=False)
    assert win._action_relations.text() == CATALOGS["en"]["menu.relations"]
    win.apply_language("zh-Hans", persist=False)
    assert win._action_relations.text() == "关系…"


def test_relations_action_opens_the_editor_for_the_picked_library(
        qapp, tmp_path, monkeypatch):
    """点「关系…」→ 选库弹窗 → 用选中那座库开关系编辑器（不真 exec，别阻塞离屏测试）。"""
    root = tmp_path / "libraries"
    lib = _lib_dir(tmp_path)
    win = _window(tmp_path, root)
    seen: dict = {}

    class _Picker:
        def __init__(self, lib_root, parent=None):
            seen["root"] = lib_root

        def exec(self):
            return QDialog.DialogCode.Accepted

        chosen = lib

    class _Editor:
        def __init__(self, lib_dir, *, libraries_root=None, parent=None):
            seen["lib_dir"] = lib_dir
            seen["libraries_root"] = libraries_root

        def exec(self):
            seen["exec"] = True
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr(mw_mod, "RelationsPickerDialog", _Picker)
    monkeypatch.setattr(mw_mod, "RelationsEditorDialog", _Editor)
    win._action_relations.trigger()

    assert seen["root"] == root, "选库弹窗看的是**本窗口**的信息库根"
    assert seen["lib_dir"] == lib
    assert seen["exec"] is True


def test_relations_action_does_nothing_when_the_picker_is_cancelled(
        qapp, tmp_path, monkeypatch):
    """取消选库 → 不开编辑器（取消就是取消，别自作主张开一个）。"""
    win = _window(tmp_path, tmp_path / "libraries")
    opened: list = []

    class _Picker:
        chosen = None

        def __init__(self, *a, **k):
            pass

        def exec(self):
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr(mw_mod, "RelationsPickerDialog", _Picker)
    monkeypatch.setattr(mw_mod, "RelationsEditorDialog",
                        lambda *a, **k: opened.append(a) or None)
    win._action_relations.trigger()
    assert opened == []


def test_relations_action_really_builds_both_dialogs_offscreen(
        qapp, tmp_path, monkeypatch):
    """**真能打开**：只把两个 `exec` 换成不阻塞的替身，选择器与编辑器都是真造出来的。

    上一条把两个类都换掉了（只验接线），这一条不换——真的去扫库根、真的建两栏对话框。
    `exec()` 换成直接返回，绝不真跑模态循环（离屏测试里那会挂住整个进程）。
    """
    root = tmp_path / "libraries"
    lib = _lib_dir(tmp_path)
    _write_table(lib, [Relation(name="乙", closeness=80)])
    win = _window(tmp_path, root)
    made: list = []
    seen: dict = {}

    def _pick_exec(self):
        seen["picked"] = list(self._infos)
        self.chosen = lib
        return QDialog.DialogCode.Accepted

    def _edit_exec(self):
        made.append(self)
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(re_mod.RelationsPickerDialog, "exec", _pick_exec)
    monkeypatch.setattr(re_mod.RelationsEditorDialog, "exec", _edit_exec)
    win._action_relations.trigger()

    assert [info.name for info in seen["picked"]] == ["甲"], "选择器真的扫了库根"
    assert len(made) == 1, "编辑器真的被造出来了"
    assert [made[0].relation_list.item(i).text()
            for i in range(made[0].relation_list.count())][0].startswith("乙")


def test_character_picker_lists_only_character_libraries(qapp, tmp_path):
    """关系表只住在角色的信息库里 → 选择器只列角色库（广域库不在这里出现）。"""
    root = tmp_path / "libraries"
    store.save_library(store.Library(name="庆国世界观", scope="wide"),
                       store.library_dir("wide", "庆国世界观", root=root))
    store.save_library(store.Library(name="乙", scope="character", owner="乙"),
                       store.library_dir("character", "乙", root=root))
    picker = re_mod.RelationsPickerDialog(root)

    labels = [picker.library_list.item(i).text()
              for i in range(picker.library_list.count())]
    assert any("乙" in text for text in labels)
    assert not any("庆国世界观" in text for text in labels)
    assert picker.windowTitle() == CATALOGS["zh-Hans"]["dlg.relations_pick_title"]


def test_open_editor_helper_reports_the_library_name_in_the_title(qapp, tmp_path):
    """对话框标题带上这座库（角色）的名字——同时开两个的时候才分得清。"""
    lib = _lib_dir(tmp_path)
    dlg = RelationsEditorDialog(lib, libraries_root=_lib_root(lib))
    assert "甲" in dlg.windowTitle()
    assert dlg.styleSheet() == dialog_qss(app_theme())
