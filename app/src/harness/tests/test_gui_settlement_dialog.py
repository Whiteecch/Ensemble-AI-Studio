"""散场结算弹窗（设计文档 §8.3 / §7.2 / §7.3）——离屏、确定性、不联网。

钉住的契约：

  · **看得到才决定得了**（§7.2）：清单必须**照抄**引擎给的待决数据（角色名、场景名、
    新增/修订条数、几条标题），弹窗自己一条都不算、一条都不猜；
  · 每人一组「保留 / 丢弃」，默认选「保留」（`collect_gain` 算出来的冲突已经自动判过，
    用户面对的只是"这一场要不要永久记下"这一个总开关）；
  · 顶部「全部保留 / 全部丢弃」是**快捷操作**（改选择，不替用户按下确定）；
  · 引擎的 `settlement_warnings()` 必须显示出来——点了「保留」却什么都没发生而界面
    不吭声，是用户最没法自查的一种收场；
  · 决定经 `decided(dict)` 信号回传，形状 = `{角色名: "keep"|"discard"}`（引擎
    `apply_settlement` 收的那一种）；
  · 样式一律取 `theme.dialog_qss` + 既有 object name，零字面颜色。

全部离屏（QT_QPA_PLATFORM=offscreen）；**绝不真 exec**（弹窗只构造 + 程序化点按钮）。
若环境未装 PySide6（可选依赖 gui），整模块 skip。
"""
import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog, QLabel, QRadioButton  # noqa: E402

from harness.gui.settlement_dialog import SettlementDialog  # noqa: E402
from harness.gui.theme import dialog_qss  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    """模块级复用单一 QApplication（Qt 只允许每进程一个）。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _row(name: str, scene: str = "茶室", added: int = 2, revised: int = 1,
         titles: list[str] | None = None) -> dict:
    """一行待决数据（与 `events.settlement_row` 同形）。"""
    return {"name": name, "scene": scene, "added": int(added), "revised": int(revised),
            "titles": list(titles if titles is not None else ["药铺的暗格", "陈掌柜"])}


ROWS = [_row("甲", added=2, revised=1, titles=["药铺的暗格", "陈掌柜", "那封信"]),
        _row("乙", added=0, revised=3, titles=["旧钥匙"])]


# ------------------------------------------------------------ 清单（§7.2） --

def test_lists_every_character_with_scene_counts_and_titles(qapp):
    """每个角色一行：角色名、场景名、「新增 N 条、修订 M 条」、几条标题。

    清单是**照抄**引擎的待决数据，不是弹窗自己数出来的——数与标题一旦对不上引擎，
    用户按着错的清单做决定，而结果落在引擎那边，两边永远对不齐。
    """
    dlg = SettlementDialog(ROWS)
    assert dlg.character_names == ["甲", "乙"]
    for name, row in zip(("甲", "乙"), ROWS):
        header = dlg.header_labels[name].text()
        assert name in header and row["scene"] in header
        detail = dlg.detail_labels[name].text()
        assert f"新增 {row['added']} 条" in detail
        assert f"修订 {row['revised']} 条" in detail
        for title in row["titles"]:
            assert title in detail


def test_missing_fields_do_not_crash_the_list(qapp):
    """字段缺失/是怪值时照常出一行（不因为一行脏数据整窗打不开）。"""
    dlg = SettlementDialog([{"name": "丙"}])
    assert dlg.character_names == ["丙"]
    detail = dlg.detail_labels["丙"].text()
    assert "新增 0 条" in detail and "修订 0 条" in detail


# ------------------------------------------------------------ 决定（§7.2） --

def test_default_choice_is_keep_for_everyone(qapp):
    """默认「保留」：用户没别的主意时，"费劲记下的东西"不该被默认丢弃。"""
    dlg = SettlementDialog(ROWS)
    assert dlg.decisions() == {"甲": "keep", "乙": "keep"}
    assert dlg.keep_radios["甲"].isChecked() and dlg.keep_radios["乙"].isChecked()


def test_per_character_choice_is_read_back(qapp):
    """逐人勾「丢弃」/「保留」，读回来就是那一份决定（谁也不许被连坐）。"""
    dlg = SettlementDialog(ROWS)
    dlg.discard_radios["甲"].setChecked(True)
    assert dlg.decisions() == {"甲": "discard", "乙": "keep"}
    dlg.keep_radios["甲"].setChecked(True)
    assert dlg.decisions() == {"甲": "keep", "乙": "keep"}


def test_keep_all_and_discard_all_are_shortcuts_over_every_character(qapp):
    """顶部两个快捷操作把**所有人**一次设成同一种选择（只是改选择，不代按确定）。"""
    dlg = SettlementDialog(ROWS)
    dlg.discard_all_btn.click()
    assert dlg.decisions() == {"甲": "discard", "乙": "discard"}
    dlg.keep_all_btn.click()
    assert dlg.decisions() == {"甲": "keep", "乙": "keep"}


def test_ok_emits_the_decisions_and_closes(qapp):
    """「确定」把决定经 `decided` 信号回传（主线程开窗、决定回引擎那条链的第一跳）。"""
    dlg = SettlementDialog(ROWS)
    seen: list[dict] = []
    dlg.decided.connect(seen.append)
    dlg.discard_radios["乙"].setChecked(True)
    dlg.ok_btn.click()
    assert seen == [{"甲": "keep", "乙": "discard"}]
    assert dlg.result() == int(QDialog.DialogCode.Accepted)


def test_cancel_emits_nothing(qapp):
    """「取消」什么都不回传：没做决定 ≠ 决定丢弃（丢弃与保留一样不可逆）。"""
    dlg = SettlementDialog(ROWS)
    seen: list[dict] = []
    dlg.decided.connect(seen.append)
    dlg.cancel_btn.click()
    assert seen == []
    assert dlg.result() == int(QDialog.DialogCode.Rejected)


# ------------------------------------------------------ 警告（§7.4 不静默） --

def test_engine_warnings_are_shown(qapp):
    """引擎的 `settlement_warnings()` 必须显示出来。

    最要紧的一条是"某人的散场总结没写出来"——不显示，用户看到的就是一次什么都正常、
    却少了东西的收场，无从自查。
    """
    warnings = ["「甲」的散场总结没写出来（TimeoutError: 超时）：所得照常待结算。",
                "「乙」：副本里有一条条目文件读不出来，这一条没有并进来。"]
    dlg = SettlementDialog(ROWS, warnings=warnings)
    text = dlg.warning_label.text()
    assert dlg.warning_label.isVisibleTo(dlg)
    for line in warnings:
        assert line in text


def test_no_warnings_hides_the_banner(qapp):
    """没有警告时那一条横幅收起来——空的红字行只会让人以为出了事。"""
    dlg = SettlementDialog(ROWS)
    assert dlg.warning_label.text() == ""
    assert not dlg.warning_label.isVisibleTo(dlg)


# -------------------------------------------------------------- 样式（§3.6） --

def test_dialog_uses_the_shared_dialog_stylesheet(qapp):
    """样式只有一处来源（`theme.dialog_qss` + 当前主题），与其它弹窗同一套观感。"""
    from harness.gui.library import app_theme

    dlg = SettlementDialog(ROWS)
    assert dlg.styleSheet() == dialog_qss(app_theme())


def test_no_literal_colour_in_the_source():
    """零字面颜色（§3.6）：源码里不许出现 `#rrggbb` 这类硬编码色值。"""
    import re
    from pathlib import Path
    import harness.gui.settlement_dialog as mod
    src = Path(mod.__file__).read_text(encoding="utf-8")
    assert re.findall(r"#[0-9a-fA-F]{6}\b", src) == []


def test_no_emoji_in_the_source():
    """无 emoji（§3.6「无 AI 味」）。"""
    import re
    from pathlib import Path
    import harness.gui.settlement_dialog as mod
    src = Path(mod.__file__).read_text(encoding="utf-8")
    assert re.findall("[⌀-⏿☀-➿\U0001F000-\U0001FAFF]", src) == []
