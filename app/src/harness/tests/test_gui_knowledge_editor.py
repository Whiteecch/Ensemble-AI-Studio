"""信息库编辑器（§8.1）与角色编辑器的「订阅信息库」区（§8.2）——离屏、确定性、不联网。

钉住的契约（每条都是设计文档里的硬要求，不是随手加的断言）：

  · 左栏按 subject 分组，且**过时条目必须看得见**（灰显 + 一句「被 X 取代」）——全仓
    其它地方（索引渲染）默认不列过时条，唯独编辑器要列：看不见旧说法，你就不敢删也
    不知道该不该改；
  · 右栏同时给**指向**（出链，`knowledge.parse_links`）与**被引用**（反链，
    `knowledge.backlinks`）。§6.2 说图只能单向走的话角色会漏掉半边——编辑器是同一个
    道理的正面：看不见反链，就改不对别处正文里的 `[[键]]`；
  · 保存一律走 `knowledgestore.save_library`（原子写、条目文件是唯一真相源），改完正文
    必须能重新 `load_library` **逐字读回**；
  · 键过不了守门 / 库名非法 / 重名 → 就地报错，**一个字节都不落盘**；写盘前还要看磁盘
    （读不出来的条目文件同样不许覆盖）与内存（同键不许覆盖），"新建条目"与"固化"两条路
    都守——用户手写的正文是唯一真相源，一次未加确认的覆盖就是不可逆的内容销毁；
  · 借入行按 `source_dir` 取详情：同键同时存在于我库与订阅库里时，"借自《X》"那一行显示
    的必须是 X 那份（固化拷的也是它），否则所见非所得；
  · 右栏没保存的改动不许被静默丢掉：点别的行 / 新建条目 / 换库都先问一句（点「否」留在
    原地，字一个不丢）；
  · 订阅关系落在 `<库目录>/subscriptions.json`（不是只活在对话框内存里的东西），固化走
    `knowledgestore.pin_entry`——固化之后源库再改，我这一份不动；订阅文件里那些**逃出库根**
    的路径（`..` / `C:/Windows` 这类盘符绝对路径）一律丢掉，界面绝不去读库根外的目录；
  · 没有信息库 / 没有订阅时，既有角色编辑器的行为**逐字节不变**（对拍）。

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
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractButton, QApplication, QComboBox, QDialog, QLabel, QLineEdit,
    QListWidget, QPlainTextEdit)

from harness import knowledgestore as store  # noqa: E402
from harness.gui import knowledge_editor as ke  # noqa: E402
from harness.gui import library as lib_mod  # noqa: E402
from harness.gui import main_window as mw_mod  # noqa: E402
from harness.gui.knowledge_editor import (  # noqa: E402
    EntryRow, KnowledgeEditorDialog, LibraryPickerDialog)
from harness.gui.library import (  # noqa: E402
    CharacterEditorDialog, LibraryDialog, import_result_text)
from harness.gui.main_window import AppConfig, MainWindow  # noqa: E402
from harness.gui.theme import PALETTES, dialog_qss  # noqa: E402
from harness.i18n import CATALOGS, LANGUAGES  # noqa: E402
from harness.knowledge import Entry  # noqa: E402
from harness.template_import import ImportResult  # noqa: E402

#: emoji 扫描（§3.6 无 AI 味）：几何/杂项符号区 + 变体选择符 + 彩色 emoji。
#: 不含箭头（→ ⇢）与带圈数字——排版符号，不是表情。
_EMOJI_RE = re.compile(
    "[⌀-⏿☀-➿⬀-⯿️\U0001F000-\U0001FAFF]")


@pytest.fixture(scope="module")
def qapp():
    """模块级复用单一 QApplication（Qt 只允许每进程一个）。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


# --------------------------------------------------------------------- 素材
def _entry(key: str, **kw) -> Entry:
    """一条条目（缺 title 就用键当标题——与索引渲染的回落口径一致）。"""
    kw.setdefault("title", key)
    return Entry(key=key, **kw)


def _write_library(root: Path, kind: str, name: str,
                   entries: list[Entry]) -> Path:
    """在库根下写一座库（走正式的 save_library，条目文件即真相源）。"""
    path = store.library_dir(kind, name, root=root)
    lib = store.Library(name=name, scope=kind,
                        owner=(name if kind == "character" else ""))
    for entry in entries:
        lib.upsert(entry)
    store.save_library(lib, path)
    return path


def _write_card(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / f"{name}.json"
    p.write_text(json.dumps({"name": name, "personality": {"描述": "示例"}},
                            ensure_ascii=False), encoding="utf-8")
    return p


def _kinds(root: Path) -> list[str]:
    """库根下所有条目文件（相对路径、排序）——用来断言「没落盘」/「落了盘」。"""
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


#: 手写条目样例（§4.2 的正文写法 + §3.1 字段表的字段）。`updated_at` 照字段表写 ISO
#: 时间戳而**不加引号**——YAML 把它读成 datetime，而 `Entry.updated_at` 是 str，于是
#: 整条读不出来（`load_library` 跳过它 + 报一条 warning）。这不是臆造：字段表就说它是
#: ISO 时间戳，照抄的人不会想到要加引号，而"读不出来"的条目文件**正文完好无损**。
_HANDWRITTEN_ISO = """---
key: 占位键
title: 占位键
summary: 柜台下第三块砖是空的，里侧有夹层
subject: 药铺
indexed: true
status: active
origin: {kind: manual, scene: '', turn: 0}
updated_at: 2026-09-12T10:00:00
---

柜台下方，第三块砖是活的。抽出来，里侧有个夹层，塞得进一封对折的信。
[[陈掌柜]]说这是上一任掌柜留下的。
"""


def _write_handwritten_entry(lib_dir: Path, key: str) -> Path:
    """往 `entries/` 里丢一条**手写**条目（模拟用户在文件系统里自己写的）。"""
    path = store.entries_dir(lib_dir) / f"{key}.md"
    path.write_text(_HANDWRITTEN_ISO.replace("占位键", key), encoding="utf-8")
    return path


def _select_row(dlg, *, key: str, source: str) -> bool:
    """按（键, 来源）选中左栏那一行：同键可以有两行（自有 + 借入），故来源也要对上。"""
    for i in range(dlg.entry_list.count()):
        item = dlg.entry_list.item(i)
        row = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(row, EntryRow) and row.key == key and row.source == source:
            dlg.entry_list.setCurrentItem(item)
            return True
    return False


# =================================== A) 信息库编辑器（§8.1）
@pytest.fixture
def lib_root(tmp_path: Path) -> Path:
    root = tmp_path / "libraries"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _grouped_library(root: Path) -> Path:
    """一座有分组、有出链、有反链、还有一条过时条的库（编辑器用例的公共素材）。"""
    return _write_library(root, "character", "丙", [
        _entry("药铺的暗格", summary="柜台下第三块砖是空的", subject="药铺",
               body="柜台下方，第三块砖是活的。[[陈掌柜]]说这是上一任留下的。"),
        _entry("陈掌柜", summary="跛足，左眉有疤", subject="人物",
               body="药铺的掌柜。[[那封信]]"),
        _entry("那封信", summary="", subject="", indexed=False, body="对折的信。"),
        _entry("旧钥匙", summary="已经不用了", subject="药铺",
               status="outdated", superseded_by="药铺的暗格", body="铜钥匙。"),
    ])


def _texts(dlg) -> list[str]:
    """窗内全部可见文字（标题/标签/输入框/列表项/下拉项）——emoji 与占位文案断言用。"""
    out = [dlg.windowTitle()]
    out += [lb.text() for lb in dlg.findChildren(QLabel) if lb.text()]
    out += [w.text() for w in dlg.findChildren(QLineEdit) if w.text()]
    out += [w.toPlainText() for w in dlg.findChildren(QPlainTextEdit)
            if w.toPlainText()]
    out += [b.text() for b in dlg.findChildren(QAbstractButton) if b.text()]
    for lst in dlg.findChildren(QListWidget):
        out += [lst.item(i).text() for i in range(lst.count())]
    for combo in dlg.findChildren(QComboBox):
        out += [combo.itemText(i) for i in range(combo.count())]
    return [t for t in out if t]


def _combo_index(combo, value) -> int:
    """combo 里 itemData == value 的那一项下标（找不到即断言失败，别静默用 0）。"""
    for i in range(combo.count()):
        if combo.itemData(i) == value:
            return i
    raise AssertionError(f"combo 里没有 {value!r}")


def test_editor_groups_entries_by_subject_and_lists_outdated_visible(
        qapp, lib_root, tmp_path):
    """左栏按 subject 分组，**过时条目就列在原组里**（不另开组、不隐藏）。

    过时条是这幅图的另一半：编辑器要让你看见"这条已经被取代了"，而索引渲染默认不列它
    （`render_index(include_outdated=False)`）。两处口径不同是**故意的**——索引是入口，
    编辑器是账本。
    """
    library = _grouped_library(lib_root)
    dlg = KnowledgeEditorDialog(library, libraries_root=lib_root)

    labels = [dlg.entry_list.item(i).text() for i in range(dlg.entry_list.count())]
    # 无 subject 的「那封信」排最前且不打组头（与 knowledge._groups 同一口径）
    assert labels == ["那封信", "药铺", "药铺的暗格", "旧钥匙（被 药铺的暗格 取代）",
                      "人物", "陈掌柜"], labels
    outdated = next(it for it in dlg.rows() if it.key == "旧钥匙")
    assert outdated.outdated is True and outdated.header is False


def test_editor_greys_out_outdated_entries(qapp, lib_root):
    """过时条目**灰显**（用当前主题的 muted_text，不留任何字面颜色）。"""
    dlg = KnowledgeEditorDialog(_grouped_library(lib_root), libraries_root=lib_root)
    item = next(dlg.entry_list.item(i) for i in range(dlg.entry_list.count())
                if dlg.entry_list.item(i).data(Qt.ItemDataRole.UserRole) is not None
                and dlg.entry_list.item(i).data(Qt.ItemDataRole.UserRole).key == "旧钥匙")
    assert item.foreground().color().name().lower() == \
        lib_mod.palette().muted_text.lower(), "过时条必须灰显（§8.1）"


def test_editor_detail_shows_both_link_directions(qapp, lib_root):
    """右栏：标题/摘要/正文 + 指向（出链）+ 被引用（反链）——图要走得通两边。"""
    dlg = KnowledgeEditorDialog(_grouped_library(lib_root), libraries_root=lib_root)
    assert dlg.select_key("药铺的暗格") is True

    assert dlg.title_edit.text() == "药铺的暗格"
    assert dlg.summary_edit.text() == "柜台下第三块砖是空的"
    assert "[[陈掌柜]]" in dlg.body_edit.toPlainText(), "正文里的 [[键]] 原样保留、不解析"
    assert dlg.outgoing_keys() == ["陈掌柜"], "指向 = 正文里的出链"
    assert dlg.incoming_keys() == [], "没人指向它"

    assert dlg.select_key("陈掌柜") is True
    assert dlg.incoming_keys() == ["药铺的暗格"], "反链：谁指向我（§6.2）"
    assert dlg.outgoing_keys() == ["那封信"]


def test_editor_marks_dangling_links(qapp, lib_root):
    """指向库里没有的键（悬空链）也列出来并标注——沉默地藏起来等于骗人。"""
    root = lib_root
    _write_library(root, "character", "甲", [
        _entry("甲条", body="指向[[还不存在的键]]。")])
    dlg = KnowledgeEditorDialog(store.library_dir("character", "甲", root=root),
                                libraries_root=root)
    dlg.select_key("甲条")
    texts = [dlg.outgoing_list.item(i).text() for i in range(dlg.outgoing_list.count())]
    assert texts and "还不存在的键" in texts[0]
    assert "没有" in texts[0], "悬空链要写明库里还没有这一条"


def test_editor_saving_an_edited_field_lands_on_disk_verbatim(qapp, lib_root):
    """改正文 → 保存 → **重新 load_library 逐字读回**（保存不得只改内存）。"""
    library = _grouped_library(lib_root)
    dlg = KnowledgeEditorDialog(library, libraries_root=lib_root)
    dlg.select_key("那封信")
    dlg.title_edit.setText("那封信（已拆）")
    dlg.summary_edit.setText("拆开看了")
    dlg.body_edit.setPlainText("信里只有半张地图。\n[[药铺的暗格]]")

    assert dlg.save_entry() is True

    back = store.load_library(library).library.entries["那封信"]
    assert back.title == "那封信（已拆）"
    assert back.summary == "拆开看了"
    assert back.body == "信里只有半张地图。\n[[药铺的暗格]]", "正文逐字落盘"
    assert back.indexed is False and back.subject == "", "没动的字段原样保留"


def test_editor_new_entry_writes_file_and_index_order(qapp, lib_root):
    """新建条目：落一条 `entries/<键>.md` 并进 library.json 的 order。"""
    library = _grouped_library(lib_root)
    dlg = KnowledgeEditorDialog(library, libraries_root=lib_root)
    dlg.new_key_edit.setText("新的钥匙")

    assert dlg.new_entry() is True

    assert (store.entries_dir(library) / "新的钥匙.md").is_file(), "条目文件是真相源"
    meta = json.loads(store.library_json_path(library).read_text(encoding="utf-8"))
    assert "新的钥匙" in meta["order"]
    assert store.load_library(library).library.entries["新的钥匙"].title == "新的钥匙"
    assert dlg.current_key() == "新的钥匙", "新建后当场选中，可以直接接着写正文"


def test_editor_rejects_unsafe_key_without_writing(qapp, lib_root):
    """键过不了守门（`entry_key_error`）→ 就地报错，**一个字节都不落盘**。"""
    library = _grouped_library(lib_root)
    before = _kinds(library)
    dlg = KnowledgeEditorDialog(library, libraries_root=lib_root)

    for bad in ("../逃逸", "带斜杠/的键", "", "CON"):
        dlg.new_key_edit.setText(bad)
        assert dlg.new_entry() is False, bad
        assert dlg.error_label.text().strip(), "必须就地报错"
    assert _kinds(library) == before, "报错不得写盘"


def test_editor_rejects_a_key_that_already_exists(qapp, lib_root):
    """库里已有同键 → 报错、不覆盖（键是冲突判定的第一依据，不是「顺手改个名」）。"""
    library = _grouped_library(lib_root)
    dlg = KnowledgeEditorDialog(library, libraries_root=lib_root)
    dlg.new_key_edit.setText("陈掌柜")
    assert dlg.new_entry() is False
    assert "陈掌柜" in dlg.error_label.text()
    assert store.load_library(library).library.entries["陈掌柜"].body == "药铺的掌柜。[[那封信]]", \
        "原有正文一字不动"


def test_editor_delete_removes_file_and_order(qapp, lib_root, monkeypatch):
    """删除条目：文件删掉、order 里摘掉（走 knowledgestore.delete_entry）。"""
    library = _grouped_library(lib_root)
    monkeypatch.setattr(ke, "confirm", lambda *a, **k: True)
    dlg = KnowledgeEditorDialog(library, libraries_root=lib_root)
    dlg.select_key("那封信")

    assert dlg.delete_current() is True

    assert not (store.entries_dir(library) / "那封信.md").exists()
    meta = json.loads(store.library_json_path(library).read_text(encoding="utf-8"))
    assert "那封信" not in meta["order"]
    assert "那封信" not in store.load_library(library).library.entries


def test_editor_delete_cancel_keeps_the_entry(qapp, lib_root, monkeypatch):
    """确认框上点「否」→ 什么都不删（破坏性动作必须问过）。"""
    library = _grouped_library(lib_root)
    monkeypatch.setattr(ke, "confirm", lambda *a, **k: False)
    dlg = KnowledgeEditorDialog(library, libraries_root=lib_root)
    dlg.select_key("那封信")
    assert dlg.delete_current() is False
    assert (store.entries_dir(library) / "那封信.md").is_file()


def test_editor_shows_borrowed_entries_with_their_source_library(qapp, lib_root):
    """订阅来的条目单独一组、组头标出来源库（`借自《…》`）。"""
    wide = _write_library(lib_root, "wide", "庆国世界观", [
        _entry("地理·西线", summary="靠山，常年封冻")])
    mine = _write_library(lib_root, "character", "丙", [_entry("暗格")])
    ke.set_subscription(mine, wide, root=lib_root, subscribed=True)

    dlg = KnowledgeEditorDialog(mine, libraries_root=lib_root)
    labels = [dlg.entry_list.item(i).text() for i in range(dlg.entry_list.count())]
    assert "暗格" in labels
    assert "借自《庆国世界观》" in labels, "借入条目单独一组并标出来源库"
    borrowed = [r for r in dlg.rows() if r.source]
    assert [r.key for r in borrowed] == ["地理·西线"]
    assert borrowed[0].label.endswith("⇢"), "借入条目标 ⇢（§8.1）"


def test_editor_pinning_a_borrowed_entry_detaches_it_from_the_source(qapp, lib_root):
    """固化（§3.4）：把借来的条目拷成我自己的；此后**源库再改，我这份不动**。"""
    wide = _write_library(lib_root, "wide", "庆国世界观", [
        _entry("地理·西线", summary="靠山，常年封冻", body="官方版：西线靠山。")])
    mine = _write_library(lib_root, "character", "丙", [])
    ke.set_subscription(mine, wide, root=lib_root, subscribed=True)

    dlg = KnowledgeEditorDialog(mine, libraries_root=lib_root)
    assert dlg.select_key("地理·西线") is True, "借入条目也能选中看详情"
    assert dlg.pin_current() is True
    assert (store.entries_dir(mine) / "地理·西线.md").is_file(), "固化 = 拷成我自己的条目"

    # 源库改一遍：我这份必须一个字都不动（这就是"断开引用"的全部意义）
    store.save_library(
        store.Library(name="庆国世界观", scope="wide", owner="",
                      entries={"地理·西线": _entry("地理·西线", body="改过的官方版。")}),
        wide)
    assert store.load_library(mine).library.entries["地理·西线"].body == "官方版：西线靠山。"


def test_editor_borrowed_entries_are_read_only_until_pinned(qapp, lib_root):
    """借入条目只能看与固化，改不了——它来自订阅（活引用），改它没有落点（§3.4）。"""
    wide = _write_library(lib_root, "wide", "庆国世界观",
                          [_entry("地理·西线", body="官方版。")])
    mine = _write_library(lib_root, "character", "丙", [])
    ke.set_subscription(mine, wide, root=lib_root, subscribed=True)
    dlg = KnowledgeEditorDialog(mine, libraries_root=lib_root)
    dlg.select_key("地理·西线")

    assert dlg.title_edit.isEnabled() is False, "借入条目：编辑框停用"
    assert dlg.save_btn.isEnabled() is False
    assert dlg.pin_btn.isEnabled() is True, "它该走的路是固化"
    assert dlg.save_entry() is False
    assert dlg.error_label.text().strip(), "要说明为什么改不了"
    assert not (store.entries_dir(mine) / "地理·西线.md").exists(), "不落盘"


def test_editor_switch_library_reloads_the_other_one(qapp, lib_root):
    """换库：左右栏整屏换成另一座库的内容（新建库之后就走这条路）。"""
    first = _write_library(lib_root, "wide", "甲库", [_entry("甲条")])
    second = _write_library(lib_root, "wide", "乙库", [_entry("乙条")])
    dlg = KnowledgeEditorDialog(first, libraries_root=lib_root)
    assert dlg.select_key("甲条") is True

    dlg.switch_library(second)

    assert dlg.select_key("甲条") is False, "旧库的条目不该还在表里"
    assert dlg.select_key("乙条") is True
    assert "乙库" in dlg.library_label.text()


def test_editor_shows_load_warnings_instead_of_swallowing_them(qapp, lib_root):
    """坏条目文件读不了 → 摆到用户面前（绝不静默；§4.1 的 warnings 口径）。"""
    library = _write_library(lib_root, "wide", "甲库", [_entry("甲条")])
    (store.entries_dir(library) / "坏的.md").write_text("没有 front-matter",
                                                        encoding="utf-8")
    dlg = KnowledgeEditorDialog(library, libraries_root=lib_root)
    assert "坏的.md" in dlg.error_label.text(), "哪一条读不了要指名道姓"


def test_editor_broken_file_does_not_hide_the_healthy_entries(qapp, lib_root):
    """一条坏文件不该让整座库读不出来（同上的第二半）。"""
    library = _write_library(lib_root, "wide", "甲库", [_entry("甲条")])
    (store.entries_dir(library) / "坏的.md").write_text("没有 front-matter",
                                                        encoding="utf-8")
    dlg = KnowledgeEditorDialog(library, libraries_root=lib_root)
    assert dlg.select_key("甲条") is True
    assert dlg.body_edit.toPlainText() == ""


def test_editor_empty_library_says_so_instead_of_showing_nothing(qapp, lib_root):
    """空库：左栏给一句「还没有条目」的占位，右栏提示先选一条（不留白）。"""
    empty = _write_library(lib_root, "wide", "空库", [])
    dlg = KnowledgeEditorDialog(empty, libraries_root=lib_root)
    assert "还没有条目" in "\n".join(_texts(dlg))
    assert dlg.current_key() is None
    assert dlg.save_entry() is False, "没选中条目时保存应被拦下"


@pytest.mark.parametrize("theme", list(PALETTES))
def test_editor_stylesheet_comes_from_the_themed_sheet(qapp, lib_root, theme):
    """四套主题下弹窗样式都取自 `dialog_qss`——不留任何字面颜色（深底不会白底黑字）。"""
    app = QApplication.instance()
    app.setProperty("theme", theme)
    try:
        dlg = KnowledgeEditorDialog(_grouped_library(lib_root), libraries_root=lib_root)
        assert dlg.styleSheet() == dialog_qss(theme)
        assert PALETTES[theme].window_bg in dlg.styleSheet()
    finally:
        app.setProperty("theme", "")


def test_editor_texts_carry_no_emoji(qapp, lib_root):
    """§3.6 无 AI 味：新弹窗的文案不许有 emoji（⇢ 是排版箭头，不算）。"""
    _write_library(lib_root, "wide", "庆国世界观", [_entry("地理·西线")])
    dialogs = [KnowledgeEditorDialog(_grouped_library(lib_root),
                                     libraries_root=lib_root),
               LibraryPickerDialog(lib_root)]
    for dlg in dialogs:
        for text in _texts(dlg):
            assert _EMOJI_RE.search(text) is None, \
                f"{type(dlg).__name__} 文案里有 emoji：{text!r}"


# --------------------------------------------------------------- 数据不丢（写盘前的守门）
def test_editor_refuses_to_create_an_entry_over_an_unreadable_file(qapp, lib_root):
    """磁盘上已有同名条目文件却读不出来 → 「新建条目」必须停手，那个文件一字不动。

    `load_library` 对读不出来的条目文件是"跳过 + 报一条 warning"：那种条目**不在**
    `lib.entries` 里，正文却完好无损地躺在磁盘上（条目文件是唯一真相源，§4.2）。守门
    只看内存就会放行，`save_library` 随即把 `<键>.md` 覆写成一条空壳——用户手写的一整条
    正文就此消失，而当时界面还正显示着"这个文件读不了"（§4.3：不安全就报错给用户看，
    绝不静默改名；静默改内容更不行）。
    """
    library = _write_library(lib_root, "wide", "甲库", [_entry("别的")])
    handwritten = _write_handwritten_entry(library, "药铺的暗格")
    before = handwritten.read_text(encoding="utf-8")
    dlg = KnowledgeEditorDialog(library, libraries_root=lib_root)
    assert "药铺的暗格.md" in dlg.error_label.text(), "先决条件：这条文件确实读不出来"

    dlg.new_key_edit.setText("药铺的暗格")
    assert dlg.new_entry() is False
    assert dlg.error_label.text().strip(), "必须就地报错"
    assert handwritten.read_text(encoding="utf-8") == before, "手写正文一字不删"


def test_editor_pin_refuses_to_overwrite_my_own_entry(qapp, lib_root):
    """固化不许把本库已有的同键自有条目覆盖掉（§3.5 的「旧条一字不删」）。

    借入行就摆在自有条目下面一行（只差一个 ⇢），点中它再点「固化」是很自然的操作；而
    固化 = 拷成我的一条、覆盖同键文件——一次点击就把我自己写的正文换成官方版，界面还报
    「已固化」。固化的本意恰恰是"这一节我想保住我自己的理解"，把它抹掉正好相反。
    """
    wide = _write_library(lib_root, "wide", "庆国世界观", [
        _entry("地理·西线", body="官方版：西线靠山。")])
    mine = _write_library(lib_root, "character", "丙", [
        _entry("地理·西线", title="我的地理", body="我自己的记法。")])
    ke.set_subscription(mine, wide, root=lib_root, subscribed=True)
    dlg = KnowledgeEditorDialog(mine, libraries_root=lib_root)
    assert _select_row(dlg, key="地理·西线", source="庆国世界观") is True

    assert dlg.pin_current() is False
    assert "地理·西线" in dlg.error_label.text(), "要说清为什么没固化"
    assert store.load_library(mine).library.entries["地理·西线"].body == "我自己的记法。", \
        "我自己的正文一字不动"


def test_editor_pin_refuses_to_overwrite_an_unreadable_file(qapp, lib_root):
    """目标库里那个键被一条**读不出来**的文件占着 → 固化同样停手（空洞不能拿正文去填）。"""
    wide = _write_library(lib_root, "wide", "庆国世界观",
                          [_entry("地理·西线", body="官方版。")])
    mine = _write_library(lib_root, "character", "丙", [])
    handwritten = _write_handwritten_entry(mine, "地理·西线")
    before = handwritten.read_text(encoding="utf-8")
    ke.set_subscription(mine, wide, root=lib_root, subscribed=True)
    dlg = KnowledgeEditorDialog(mine, libraries_root=lib_root)
    assert _select_row(dlg, key="地理·西线", source="庆国世界观") is True

    assert dlg.pin_current() is False
    assert "地理·西线.md" in dlg.error_label.text()
    assert handwritten.read_text(encoding="utf-8") == before, "手写正文一字不删"


def test_editor_shows_the_borrowed_copy_when_both_sides_share_a_key(qapp, lib_root):
    """同键同时在我库里与订阅库里 → 「借自《…》」那一行显示的必须是**源库那份**。

    详情原来只按 key 取（先查自有、再借入），于是选中借入行看到的是我自己那条——组头与
    状态行都写着「借自《庆国世界观》」，正文却是我的：所见非所得。固化取的是源库那份，
    用户会基于错误的正文做决定（§3.4：这个动作本就是给"我与官方版分叉了"用的，同键共存
    恰恰是常态）。
    """
    wide = _write_library(lib_root, "wide", "庆国世界观", [
        _entry("地理·西线", title="地理·西线", body="官方版：西线靠山。")])
    mine = _write_library(lib_root, "character", "丙", [
        _entry("地理·西线", title="我的地理", body="我自己的记法。")])
    ke.set_subscription(mine, wide, root=lib_root, subscribed=True)
    dlg = KnowledgeEditorDialog(mine, libraries_root=lib_root)

    assert _select_row(dlg, key="地理·西线", source="庆国世界观") is True
    assert dlg.pin_btn.isEnabled() is True, "借入行仍可固化（只是不许覆盖同键自有条）"
    assert dlg.title_edit.text() == "地理·西线", "标题取源库那份"
    assert dlg.body_edit.toPlainText() == "官方版：西线靠山。", "正文取源库那份"

    assert _select_row(dlg, key="地理·西线", source="") is True
    assert dlg.title_edit.text() == "我的地理", "自有那一行显示的仍是我这条"
    assert dlg.body_edit.toPlainText() == "我自己的记法。"


def test_editor_detail_follows_the_row_when_two_subscribed_libraries_share_a_key(
        qapp, lib_root):
    """两个订阅库都有同一个键 → 选谁那一行就显示谁的正文（固化也拷那一份）。"""
    a = _write_library(lib_root, "wide", "甲世界观",
                       [_entry("西线", body="甲库的版本：靠山封冻。")])
    b = _write_library(lib_root, "wide", "乙世界观",
                       [_entry("西线", body="乙库的版本：是片盐碱地。")])
    mine = _write_library(lib_root, "character", "丙", [])
    ke.set_subscription(mine, a, root=lib_root, subscribed=True)
    ke.set_subscription(mine, b, root=lib_root, subscribed=True)

    dlg = KnowledgeEditorDialog(mine, libraries_root=lib_root)
    assert _select_row(dlg, key="西线", source="乙世界观") is True
    assert dlg.body_edit.toPlainText() == "乙库的版本：是片盐碱地。"

    assert dlg.pin_current() is True
    assert store.load_library(mine).library.entries["西线"].body == "乙库的版本：是片盐碱地。", \
        "固化拷的是刚才那一行那一份"


def test_editor_asks_before_dropping_unsaved_edits(qapp, lib_root, monkeypatch):
    """敲了没保存就点别的行 → 先问一句；点「否」留在原地，改动一个字都不丢。

    保存是右栏唯一的落盘动作，"点一下别的行"却把三个框整份覆盖——一次误点就丢掉刚敲的
    整段正文，界面上既没有脏标记也没有提示。本仓同类破坏性动作（删除条目）都要过
    `confirm`，这一条只大不小，故同一口径。（`select_key` 的返回值仍是"表里有这一行"，
    拒绝丢弃时选中会被拨回原来那一行。）
    """
    library = _grouped_library(lib_root)
    asked: list = []
    monkeypatch.setattr(ke, "confirm", lambda *a, **k: asked.append(a) or False)
    dlg = KnowledgeEditorDialog(library, libraries_root=lib_root)
    assert dlg.select_key("那封信") is True
    dlg.body_edit.setPlainText("我敲了很久的新正文")

    dlg.select_key("陈掌柜")
    assert asked, "有没保存的改动时必须问一句"
    assert dlg.current_key() == "那封信", "点「否」就留在原来那一行"
    assert dlg.body_edit.toPlainText() == "我敲了很久的新正文", "改动一个字都不许丢"
    assert dlg.title_edit.isEnabled() is True, "右栏仍在可编辑状态"

    monkeypatch.setattr(ke, "confirm", lambda *a, **k: True)
    dlg.select_key("陈掌柜")
    assert dlg.current_key() == "陈掌柜", "点「是」才真的切走"
    assert dlg.body_edit.toPlainText() == "药铺的掌柜。[[那封信]]"


def test_editor_does_not_ask_when_there_is_nothing_to_lose(qapp, lib_root, monkeypatch):
    """没有未保存的改动就不许弹确认（否则每点一行都问一次，纯噪声）。"""
    asked: list = []
    monkeypatch.setattr(ke, "confirm", lambda *a, **k: asked.append(a) or True)
    dlg = KnowledgeEditorDialog(_grouped_library(lib_root), libraries_root=lib_root)
    assert dlg.select_key("那封信") is True
    assert dlg.select_key("陈掌柜") is True
    assert dlg.current_key() == "陈掌柜"
    assert asked == []


def test_editor_new_entry_and_switch_library_ask_before_dropping_edits(
        qapp, lib_root, monkeypatch):
    """「新建条目」与「换库」同样会丢掉没保存的改动 → 同样先问；点「否」什么都不做。"""
    first = _write_library(lib_root, "wide", "甲库", [_entry("甲条", body="甲原文")])
    second = _write_library(lib_root, "wide", "乙库", [_entry("乙条")])
    monkeypatch.setattr(ke, "confirm", lambda *a, **k: False)
    dlg = KnowledgeEditorDialog(first, libraries_root=lib_root)
    assert dlg.select_key("甲条") is True
    dlg.body_edit.setPlainText("改了一半")
    dlg.new_key_edit.setText("新条")

    assert dlg.new_entry() is False, "点「否」：不新建"
    assert not (store.entries_dir(first) / "新条.md").exists(), "确认被拒就不许落盘"

    dlg.switch_library(second)
    assert dlg.select_key("甲条") is True, "点「否」：还在原来那座库"
    assert dlg.body_edit.toPlainText() == "改了一半", "改动还在"


# --------------------------------------------------------------- 库选择/新建
def test_picker_lists_wide_and_character_libraries(qapp, lib_root):
    """选择器列出库根下的广域库与角色库，各带类型标签。"""
    _write_library(lib_root, "wide", "庆国世界观", [])
    _write_library(lib_root, "character", "丙", [])
    dlg = LibraryPickerDialog(lib_root)
    texts = [dlg.library_list.item(i).text() for i in range(dlg.library_list.count())]
    assert len(texts) == 2
    assert any("庆国世界观" in t and "广域库" in t for t in texts)
    assert any("丙" in t and "角色库" in t for t in texts)


def test_picker_open_sets_the_chosen_library(qapp, lib_root):
    """选中一行点「打开」→ chosen 记下路径并 accept。"""
    path = _write_library(lib_root, "wide", "庆国世界观", [])
    dlg = LibraryPickerDialog(lib_root)
    dlg.library_list.setCurrentRow(0)
    dlg.open_selected()
    assert dlg.result() == QDialog.DialogCode.Accepted
    assert dlg.chosen == path


@pytest.mark.parametrize("kind", ["wide", "character"])
def test_picker_creates_a_library_on_disk(qapp, lib_root, kind):
    """新建库：按类型落到 `<根>/<characters|wide>/<名>`，并写出 library.json。"""
    dlg = LibraryPickerDialog(lib_root)
    name = "新库" if kind == "wide" else "新人"
    dlg.new_name_edit.setText(name)
    dlg.new_kind_combo.setCurrentIndex(_combo_index(dlg.new_kind_combo, kind))

    made = dlg.new_library()

    assert made == store.library_dir(kind, name, root=lib_root)
    assert store.has_library(made), "库目录里必须有 library.json（否则根下认不出它）"
    assert dlg.chosen == made, "新建后当场选中它"


def test_picker_rejects_unsafe_or_duplicate_name_without_writing(qapp, lib_root):
    """库名非法 / 已存在 → 就地报错、不落盘。"""
    _write_library(lib_root, "wide", "庆国世界观", [])
    dlg = LibraryPickerDialog(lib_root)
    before = _kinds(lib_root)

    for bad in ("..", "带斜杠/的库"):
        dlg.new_name_edit.setText(bad)
        assert dlg.new_library() is None, bad
        assert dlg.error_label.text(), "必须就地报错"
    dlg.new_name_edit.setText("庆国世界观")
    assert dlg.new_library() is None, "同类型的同名库已经有了"
    assert _kinds(lib_root) == before, "报错不得写盘"


# =================================== B) 订阅存哪里（§8.2 / §3.4）
def test_subscription_store_roundtrips_relative_paths(lib_root):
    """订阅落 `<库目录>/subscriptions.json`，存**相对库根**的路径（可搬迁、不是内存状态）。"""
    wide = _write_library(lib_root, "wide", "庆国世界观", [])
    mine = _write_library(lib_root, "character", "丙", [])

    subs = ke.set_subscription(mine, wide, root=lib_root, subscribed=True)

    assert [s.name for s in subs] == ["庆国世界观"]
    raw = json.loads(ke.subscriptions_path(mine).read_text(encoding="utf-8"))
    assert raw["libraries"][0]["path"] == "wide/庆国世界观", "相对库根，绝无绝对路径"
    assert raw["libraries"][0]["kind"] == "wide"
    back = ke.load_subscriptions(mine, root=lib_root)
    assert [(s.name, s.kind, s.path) for s in back] == [("庆国世界观", "wide", wide)]

    assert ke.set_subscription(mine, wide, root=lib_root, subscribed=False) == []
    assert ke.load_subscriptions(mine, root=lib_root) == []
    assert json.loads(ke.subscriptions_path(mine).read_text(encoding="utf-8")) == \
        {"libraries": [], "schema": 1}


def test_subscription_store_tolerates_missing_or_broken_files(lib_root, tmp_path):
    """没有订阅文件 / 文件坏了 → 空表，**绝不抛**（老库、手改坏的文件都是常态）。"""
    mine = _write_library(lib_root, "character", "丙", [])
    assert ke.load_subscriptions(mine, root=lib_root) == []
    ke.subscriptions_path(mine).write_text("{ 这不是 json", encoding="utf-8")
    assert ke.load_subscriptions(mine, root=lib_root) == []
    ke.subscriptions_path(mine).write_text(
        json.dumps({"libraries": [{"path": 3}, "乱写的"]}), encoding="utf-8")
    assert ke.load_subscriptions(mine, root=lib_root) == []


def test_subscriptions_drop_paths_that_escape_the_library_root(lib_root):
    """订阅文件里的盘符绝对路径 / 回溯路径 → 丢它自己，绝不让界面去读库根外的目录。

    `_safe_rel` 拦得住 `..`，却拦不住 `C:/Windows` 这类**盘符绝对路径**：pathlib 在右
    操作数带盘符时会丢掉左边的 base（`Path(root) / 'C:/Windows' == Path('C:/Windows')`），
    于是"相对路径"这条底线整个被绕过。守门函数存在的全部理由就是不让人读库根外的目录，
    漏一种形态等于没有守门——故读侧再加一道"解出来必须在库根里"的闸。
    """
    mine = _write_library(lib_root, "character", "丙", [])
    good = _write_library(lib_root, "wide", "好库", [])
    ke.subscriptions_path(mine).write_text(json.dumps({"schema": 1, "libraries": [
        {"name": "坏", "path": "C:/Windows"},
        {"name": "回溯", "path": "wide/../.."},
        {"name": "好库", "path": "wide/好库"},
    ]}, ensure_ascii=False), encoding="utf-8")

    subs = ke.load_subscriptions(mine, root=lib_root)

    assert [s.name for s in subs] == ["好库"], "坏项只丢它自己，不连坐"
    assert subs[0].path == good
    root = Path(lib_root).resolve()
    for sub in subs:
        sub.path.resolve().relative_to(root)      # 抛 = 逃出库根，这条用例就红
    assert ke._safe_rel("C:/Windows") == "", "盘符绝对路径不算相对库根的路径"
    assert ke._safe_rel("wide/../..") == ""


# =================================== C) 角色编辑器的「订阅信息库」区
def _char_editor(tmp_path: Path, root: Path, name: str = "丙"):
    cdir = tmp_path / "characters"
    _write_card(cdir, "别的人")
    dlg = CharacterEditorDialog(None, cdir, libraries_root=root)
    dlg.name_edit.setText(name)
    return dlg


def test_character_editor_lists_libraries_and_remembers_subscription(
        qapp, tmp_path, lib_root):
    """勾选一个库 → 当场写进该角色的 `subscriptions.json`；取消勾选 → 摘掉。"""
    wide = _write_library(lib_root, "wide", "庆国世界观", [])
    dlg = _char_editor(tmp_path, lib_root)

    item = dlg.subs_items[str(wide)]
    item.setCheckState(Qt.CheckState.Checked)
    mine = store.library_dir("character", "丙", root=lib_root)
    assert [s.name for s in ke.load_subscriptions(mine, root=lib_root)] == ["庆国世界观"]

    item.setCheckState(Qt.CheckState.Unchecked)
    assert ke.load_subscriptions(mine, root=lib_root) == []


def test_character_editor_borrowed_entries_are_listed_and_pinnable(
        qapp, tmp_path, lib_root):
    """借入条目列出来，可单独固化；固化之后源库再改，我这份不动（§3.4）。"""
    wide = _write_library(lib_root, "wide", "庆国世界观", [
        _entry("地理·西线", summary="靠山，常年封冻", body="官方版：西线靠山。")])
    dlg = _char_editor(tmp_path, lib_root)
    dlg.subs_items[str(wide)].setCheckState(Qt.CheckState.Checked)

    labels = [dlg.borrowed_list.item(i).text()
              for i in range(dlg.borrowed_list.count())]
    assert any("地理·西线" in t for t in labels), "借入条目要列出来"

    dlg.borrowed_list.setCurrentRow(0)
    assert dlg.pin_selected() is True
    mine = store.library_dir("character", "丙", root=lib_root)
    assert store.load_library(mine).library.entries["地理·西线"].body == "官方版：西线靠山。"

    store.save_library(
        store.Library(name="庆国世界观", scope="wide", owner="",
                      entries={"地理·西线": _entry("地理·西线", body="改过的官方版。")}),
        wide)
    assert store.load_library(mine).library.entries["地理·西线"].body == "官方版：西线靠山。"


def test_character_editor_pin_refuses_to_overwrite_my_own_entry(qapp, tmp_path, lib_root):
    """角色编辑器里的「固化」是同一个缺口的第二个入口 → 同一条守门，且要说清原因。

    借入条目列表就摆在同屏，选中一条点「固化」同样会覆盖我自有库里的同键正文；原先
    失败时连一句话都没有（状态行是空的），用户只会以为固化成功了。
    """
    wide = _write_library(lib_root, "wide", "庆国世界观", [
        _entry("地理·西线", body="官方版：西线靠山。")])
    mine = _write_library(lib_root, "character", "丙", [
        _entry("地理·西线", title="我的地理", body="我自己写的私人理解。")])
    dlg = _char_editor(tmp_path, lib_root)
    dlg.subs_items[str(wide)].setCheckState(Qt.CheckState.Checked)
    dlg.borrowed_list.setCurrentRow(0)

    assert dlg.pin_selected() is False
    assert "地理·西线" in dlg.subs_status.text(), "要说清为什么没固化"
    assert store.load_library(mine).library.entries["地理·西线"].body == "我自己写的私人理解。"


def test_character_editor_does_not_offer_its_own_library(qapp, tmp_path, lib_root):
    """不列这个角色自己的库——订阅自己只会让索引里多出一份重复。"""
    _write_library(lib_root, "character", "丙", [_entry("暗格")])
    _write_library(lib_root, "wide", "庆国世界观", [])
    dlg = _char_editor(tmp_path, lib_root)
    mine = str(store.library_dir("character", "丙", root=lib_root))
    assert mine not in dlg.subs_items
    assert len(dlg.subs_items) == 1


def test_character_editor_says_so_when_there_is_no_library_root_at_all(
        qapp, tmp_path):
    """库里一座信息库都没有 → 给一句说明，而不是一个空框。"""
    dlg = CharacterEditorDialog(None, tmp_path / "characters",
                                libraries_root=tmp_path / "空的")
    assert "还没有库" in "\n".join(_texts(dlg))


def test_character_editor_requires_a_name_before_subscribing(qapp, tmp_path, lib_root):
    """姓名还没填/不可用时勾订阅 → 就地报错、不写盘（订阅挂在角色库里，没有名字就没有落点）。"""
    wide = _write_library(lib_root, "wide", "庆国世界观", [])
    dlg = CharacterEditorDialog(None, tmp_path / "characters", libraries_root=lib_root)
    dlg.name_edit.setText("")
    before = _kinds(lib_root)
    item = dlg.subs_items[str(wide)]
    item.setCheckState(Qt.CheckState.Checked)
    assert item.checkState() == Qt.CheckState.Unchecked, "勾不上：没有落点就别假装订上了"
    assert "姓名" in dlg.subs_status.text()
    assert _kinds(lib_root) == before, "没有落点就不该写盘"


def test_character_editor_card_bytes_are_unchanged_without_any_library(
        qapp, tmp_path, lib_root):
    """**没有信息库 / 没有订阅时，既有对话框行为逐字节不变**（对拍）。

    两个编辑器（一个有库根、一个没有）填同样的字段存出同一张卡，落盘字节必须一模一样：
    订阅区只读库根、不在打开时写任何东西，也绝不往卡上加字段。
    """
    def _save(into: Path, **kw) -> str:
        dlg = CharacterEditorDialog(None, into, **kw)
        dlg.name_edit.setText("丙")
        dlg.personality_edit.setPlainText("描述：话少")
        dlg.abilities_edit.setPlainText("记性好")
        dlg.weight_sliders["w1_relevance"].setValue(70)
        dlg.save()
        return (into / "丙.json").read_text(encoding="utf-8")

    with_root = _save(tmp_path / "a", libraries_root=lib_root)
    without_root = _save(tmp_path / "b")
    assert with_root == without_root


# =================================== D) 主窗口入口（§8.1 / §8.4）
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


def test_mainwindow_has_a_knowledge_menu_item(qapp, tmp_path, lib_root):
    """顶栏多一个「信息库…」入口（顺序排在原有三段之后，原有的三段一字不动）。"""
    win = _window(tmp_path, lib_root)
    texts = [a.text() for a in win.menuBar().actions()]
    assert texts[:3] == ["设置", "场景", "角色"], "既有三段不许漂移"
    assert texts[3] == "信息库…"
    assert win._action_knowledge.isEnabled() is True
    assert win._action_knowledge.toolTip(), "要有悬停说明"


def test_mainwindow_knowledge_action_opens_the_editor_for_the_picked_library(
        qapp, tmp_path, lib_root, monkeypatch):
    """点「信息库…」→ 选库弹窗 → 用选中那座库开编辑器（不真 exec，别阻塞离屏测试）。"""
    library = _write_library(lib_root, "wide", "庆国世界观", [_entry("地理·西线")])
    win = _window(tmp_path, lib_root)
    seen: dict = {}

    class _Picker:
        def __init__(self, root, parent=None):
            seen["root"] = root

        def exec(self):
            return QDialog.DialogCode.Accepted

        chosen = library

    class _Editor:
        def __init__(self, lib_dir, *, libraries_root=None, parent=None):
            seen["lib_dir"] = lib_dir

        def exec(self):
            seen["exec"] = True
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr(mw_mod, "LibraryPickerDialog", _Picker)
    monkeypatch.setattr(mw_mod, "KnowledgeEditorDialog", _Editor)
    win._action_knowledge.trigger()

    assert seen["root"] == lib_root, "选库弹窗看的是**本窗口**的信息库根"
    assert seen["lib_dir"] == library
    assert seen["exec"] is True


def test_mainwindow_knowledge_action_does_nothing_when_the_picker_is_cancelled(
        qapp, tmp_path, lib_root, monkeypatch):
    """取消选库 → 不开编辑器（取消就是取消，别自作主张开一个）。"""
    win = _window(tmp_path, lib_root)
    opened: list = []

    class _Picker:
        chosen = None

        def __init__(self, *a, **k):
            pass

        def exec(self):
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr(mw_mod, "LibraryPickerDialog", _Picker)
    monkeypatch.setattr(mw_mod, "KnowledgeEditorDialog",
                        lambda *a, **k: opened.append(a) or None)
    win._action_knowledge.trigger()
    assert opened == []


# --------------------------------------------- 导入：类型文案与库根（M4a-2 遗留）
def test_import_result_text_names_the_library_kind():
    """信息库导入不许再弹「已导入角色卡」（M4a-2 遗留）：三类各有各的中文名。"""
    result = ImportResult(path=Path("C:/tmp/wide/庆国世界观"), kind="library",
                          name="庆国世界观", card_or_scene=None,
                          empty_fields=[], warnings=[])
    text = import_result_text(result)
    assert "已导入信息库《庆国世界观》" in text
    assert "角色卡" not in text, "那是卡，不是库"

    # 回归：既有两类一字不动
    sc = ImportResult(path=Path("C:/tmp/a.json"), kind="scene", name="餐厅",
                      card_or_scene=None, empty_fields=[], warnings=[])
    ch = ImportResult(path=Path("C:/tmp/乙.json"), kind="character", name="乙",
                      card_or_scene=None, empty_fields=[], warnings=[])
    assert "已导入场景卡《餐厅》" in import_result_text(sc)
    assert "已导入角色卡《乙》" in import_result_text(ch)


def test_mainwindow_import_passes_the_libraries_root(qapp, tmp_path, lib_root,
                                                     monkeypatch):
    """「导入场景…」两个调用点都要把**本窗口的**库根传下去（信息库模板知道往哪落）。"""
    win = _window(tmp_path, lib_root)
    seen: dict = {}
    monkeypatch.setattr(mw_mod, "pick_template_file", lambda *a, **k: "C:/tmp/s.md")

    def _import(path, **kw):
        seen.update(kw)
        seen["path"] = Path(path)
        return ImportResult(path=Path("C:/tmp/a.json"), kind="scene", name="餐厅",
                            card_or_scene=None, empty_fields=[], warnings=[])

    monkeypatch.setattr(mw_mod, "import_template_file", _import)
    monkeypatch.setattr(mw_mod, "info", lambda *a, **k: None)
    win._action_import_scene.trigger()

    assert seen["libraries_dir"] == lib_root


def test_library_dialog_import_passes_the_libraries_root(tmp_path, monkeypatch,
                                                         qapp):
    """场景库弹窗里的「从模板导入…」同样要传库根（缺省按素材布局推导）。"""
    cdir, sdir = tmp_path / "characters", tmp_path / "scenes"
    _write_card(cdir, "甲")
    sdir.mkdir(parents=True, exist_ok=True)
    seen: dict = {}

    def _import(path, **kw):
        seen.update(kw)
        return ImportResult(path=Path("C:/tmp/a.json"), kind="scene", name="餐厅",
                            card_or_scene=None, empty_fields=[], warnings=[])

    monkeypatch.setattr(lib_mod, "import_template_file", _import)
    monkeypatch.setattr(lib_mod.QFileDialog, "getOpenFileName",
                        lambda *a, **k: ("C:/tmp/s.md", ""))
    monkeypatch.setattr(lib_mod, "info", lambda *a, **k: None)
    dlg = LibraryDialog(cdir, sdir)
    dlg.import_from_template()

    assert seen["libraries_dir"] == tmp_path / "libraries", "缺省的库根 = 角色目录的兄弟"


def test_library_dialog_import_uses_an_injected_libraries_root(tmp_path, monkeypatch,
                                                              qapp):
    """注入的库根优先（打包后用户目录与素材目录不必是兄弟）。"""
    cdir, sdir, root = tmp_path / "characters", tmp_path / "scenes", tmp_path / "libs"
    _write_card(cdir, "甲")
    sdir.mkdir(parents=True, exist_ok=True)
    seen: dict = {}
    monkeypatch.setattr(lib_mod, "import_template_file",
                        lambda path, **kw: seen.update(kw) or ImportResult(
                            path=None, kind="library", name="库", card_or_scene=None))
    monkeypatch.setattr(lib_mod.QFileDialog, "getOpenFileName",
                        lambda *a, **k: ("C:/tmp/l.md", ""))
    monkeypatch.setattr(lib_mod, "info", lambda *a, **k: None)
    LibraryDialog(cdir, sdir, libraries_root=root).import_from_template()
    assert seen["libraries_dir"] == root


# =================================== E) 多语言（§8.4：菜单键七语言齐）
def test_knowledge_menu_key_exists_in_all_seven_languages():
    """菜单键**七种语言齐**（§8.4 的硬要求；test_i18n 的 MENU_KEYS 也钉着它）。"""
    for code in LANGUAGES:
        value = CATALOGS[code].get("menu.knowledge")
        assert value, f"{code} 缺 menu.knowledge"
        assert value.strip() and value != "menu.knowledge"
    assert CATALOGS["zh-Hans"]["menu.knowledge"] == "信息库…"


def test_new_main_catalog_keys_are_present_and_non_empty():
    """其余新键只补 zh-Hans 主目录（缺键回落中文是既有机制，不是妥协）。"""
    keys = ["tip.knowledge", "dlg.knowledge_title", "grp.entries",
            "field.entry_body", "field.outgoing", "field.incoming",
            "btn.new_entry", "btn.new_library", "btn.pin", "btn.delete_entry",
            "grp.subscriptions", "kind.library", "kind.wide", "kind.character_lib",
            "grp.borrowed", "err.entry_exists", "err.library_exists"]
    for key in keys:
        assert CATALOGS["zh-Hans"].get(key), f"主目录缺 {key}"


def test_mainwindow_knowledge_item_translates_to_the_current_language(qapp, tmp_path,
                                                                     lib_root):
    """切语言后菜单项按新语言出字（登记进 _retranslate_menus 才做得到）。"""
    win = _window(tmp_path, lib_root)
    win.apply_language("en", persist=False)
    assert win._action_knowledge.text() == CATALOGS["en"]["menu.knowledge"]


# =================================== F) 读订阅的口径只有一处（§8.2/§3.4）
def test_gui_reader_delegates_to_the_store_reader(tmp_path, lib_root, monkeypatch):
    """界面的 `load_subscriptions` **转调**存储层的同名函数——改一处另一处跟着变。

    上一阶段订阅的读实现在界面侧，引擎侧接不上（没有第二处会去读它）。下沉到存储层之后
    界面不再自己解析文件；这个用例把"只有一处口径"钉死：把存储层的函数换掉，界面读到的
    必须就是那一份。
    """
    mine = _write_library(lib_root, "character", "丙", [_entry("暗格")])
    sentinel = [("哨兵库", lib_root / "wide" / "哨兵库")]
    monkeypatch.setattr(store, "load_subscriptions", lambda *a, **k: list(sentinel))

    subs = ke.load_subscriptions(mine, root=lib_root)
    assert [s.name for s in subs] == ["哨兵库"]
    assert subs[0].path == lib_root / "wide" / "哨兵库"
    assert subs[0].rel == "wide/哨兵库" and subs[0].kind == "wide"


def test_gui_and_store_readers_agree_on_the_same_file(tmp_path, lib_root):
    """同一份 subscriptions.json，两处读出来的库名与源目录必须一致。"""
    mine = _write_library(lib_root, "character", "丙", [_entry("暗格")])
    wide = _write_library(lib_root, "wide", "庆国世界观", [_entry("地理·西线")])
    ke.set_subscription(mine, wide, root=lib_root, subscribed=True)

    gui_rows = [(s.name, s.path) for s in ke.load_subscriptions(mine, root=lib_root)]
    assert gui_rows == store.load_subscriptions(mine, root=lib_root)


def test_path_guard_is_a_single_definition(tmp_path):
    """两道读侧守门（写法 / 结果）下沉到存储层，界面只留同一个对象。"""
    assert ke._safe_rel is store._safe_rel
    assert ke._inside_root is store._inside_root
    assert ke._safe_rel("C:/Windows") == "" and ke._safe_rel("wide/../..") == ""
