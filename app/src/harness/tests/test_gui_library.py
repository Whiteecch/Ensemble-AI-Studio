"""应用内编辑器 + **场景库**：离屏、确定性、不联网、快速。

覆盖：CharacterEditorDialog 存盘/回读/空名与非法文件名拒绝，SceneEditorDialog
存盘/回读/引擎开场前置条件校验，LibraryDialog **只列场景**（打开/新建/编辑/复制/删除/
导入；没有角色列、没有「至少选一名角色」的守门，空阵容场景照样能选中打开）与复制安全，
各弹窗的样式随主题、文案无 emoji（§3.6），MainWindow 按钮切场（含素材打不开时拒绝切场），
以及 worker 换场 aclose 旧引擎（唯一一处真跑引擎线程的用例）。其余全走替身 worker，
故整模块 <3s。
开场设置小窗已随 §3.3 删除——启动直达主界面，故本模块不再有用例构造它。
若环境未装 PySide6（可选依赖 gui）整模块 skip。
"""
import copy
import json
import os
import re
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QObject, Qt, QTimer, Signal  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractButton, QApplication, QDialog, QLabel, QListWidget)

from harness.gui import library as lib  # noqa: E402
from harness.gui.library import (  # noqa: E402
    CharacterDetailDialog, CharacterEditorDialog, LibraryDialog, SceneEditorDialog)
from harness.gui.main_window import AppConfig, MainWindow  # noqa: E402
from harness.gui.theme import PALETTES, dialog_qss  # noqa: E402
from harness.hooks import Hook  # noqa: E402
from harness.loaders import load_character_card, load_scene, save_scene  # noqa: E402
from harness.schemas import HardBoundary, Scene, SceneCastMember  # noqa: E402

#: emoji 扫描（§3.6 无 AI 味）：几何/杂项符号区 + 变体选择符 + 彩色 emoji。
#: 不含箭头（→）与带圈数字（①②）——排版符号，docstring 里在用。
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
def _write_card(directory: Path, name: str, source: str = "") -> Path:
    """写一张角色卡（可带 corpus.source——场景编辑器的「出场作品」筛选按它归类）。"""
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / f"{name}.json"
    p.write_text(json.dumps({"name": name, "personality": {"描述": "示例"},
                             "corpus": {"source": source}},
                            ensure_ascii=False), encoding="utf-8")
    return p


def _write_chars(directory: Path, *names: str) -> list[Path]:
    return [_write_card(directory, n) for n in names]


def _write_scene(directory: Path, name: str, characters: list[str],
                 start_time: str = "21:30") -> Path:
    """写一个场景（角色写进新模型的 characters 演员表；可以一个人都没有）。"""
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / f"{name}.json"
    p.write_text(json.dumps({
        "name": name, "start_time": start_time,
        "characters": [{"name": n} for n in characters]},
        ensure_ascii=False), encoding="utf-8")
    return p


def _idx(combo, value) -> int:
    """combo 里 itemData == value 的那一项的下标（找不到即断言失败，别静默用 0）。"""
    for i in range(combo.count()):
        if combo.itemData(i) == value:
            return i
    raise AssertionError(f"combo 里没有 {value!r}")


def _check(dlg: SceneEditorDialog, name: str, on: bool = True) -> None:
    """勾/取消勾候选列表里的某个名字（QListWidget 的勾选态）。"""
    dlg.participant_items[name].setCheckState(
        Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)


def _fake_hook_dialog(hook: Hook):
    """替身 HookEditorDialog：exec() 直接「接受」，build_hook() 每次交出一份新副本
    （SceneEditorDialog 会就地改 id，同一对象被复用会让两次新建变成同一条）。"""

    class _Fake:
        def __init__(self, *a, **k):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def build_hook(self):
            return copy.deepcopy(hook)

    return _Fake


def _names(path: Path) -> list[str]:
    """目录下的条目名（排序）——用来断言「没多出文件/子目录」；目录不存在视为空。"""
    return sorted(p.name for p in path.iterdir()) if path.is_dir() else []


def _wait_until(cond, timeout_ms: int = 8000, step_ms: int = 15) -> bool:
    """轮询跑主线程 Qt 事件循环直到 cond() 为真；超时返回 False（不抛）。"""
    loop = QEventLoop()
    ok = {"v": False}
    timer = QTimer()
    timer.setInterval(step_ms)

    def _tick():
        if cond():
            ok["v"] = True
            timer.stop()
            loop.quit()

    timer.timeout.connect(_tick)
    timer.start()
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    timer.stop()
    return ok["v"]


# ------------------------------------------------------- CharacterEditorDialog
def test_character_editor_saves_and_reloads_all_fields(qapp, tmp_path):
    """填满各部件 → 保存 → 落盘 JSON 可回读，语料/性格/权重逐项一致。"""
    cdir = tmp_path / "characters"
    dlg = CharacterEditorDialog(None, cdir)
    dlg.name_edit.setText("阿离")
    dlg.source_edit.setText("某作品")
    dlg.style_edit.setPlainText("短句为主，语速慢。")
    dlg.thinking_edit.setPlainText("先看对方反应再决定开口。")
    dlg.quirks_edit.setPlainText("……\n倒是")
    dlg.samples_edit.setPlainText("（样例）你倒是先说说看。\n（样例）（笑）随你。")
    dlg.personality_edit.setPlainText("描述：冷静\n习惯：晚睡")
    dlg.abilities_edit.setPlainText("记忆力好")
    dlg.relationships_edit.setPlainText("己：暗恋的转学生")
    dlg.boundary_edit.setPlainText("只知道自己经历和被告知的事")
    dlg.weight_sliders["w1_relevance"].setValue(70)
    dlg.decay_slider.setValue(30)

    dlg.save()

    assert dlg.result() == QDialog.DialogCode.Accepted, "保存成功应 accept"
    path = cdir / "阿离.json"
    assert path.exists(), "保存应写出 characters_dir/姓名.json"
    card = load_character_card(path)
    assert card.name == "阿离"
    assert card.corpus.source == "某作品"
    assert card.corpus.style == "短句为主，语速慢。"
    assert card.corpus.thinking == "先看对方反应再决定开口。"
    assert card.corpus.quirks == ["……", "倒是"]
    assert card.corpus.samples == ["（样例）你倒是先说说看。", "（样例）（笑）随你。"]
    assert card.personality == {"描述": "冷静", "习惯": "晚睡"}
    assert card.abilities == ["记忆力好"]
    assert card.relationships == {"己": "暗恋的转学生"}
    assert card.knowledge_boundary == ["只知道自己经历和被告知的事"]
    assert card.weights.w1_relevance == pytest.approx(0.7)
    assert card.weights.w2_arousal == pytest.approx(0.5), "未动的权重保持默认"
    assert card.emotion_decay_rate == pytest.approx(0.3)


def test_character_editor_rejects_empty_name(qapp, tmp_path):
    """空姓名 → 就地报错、不 accept、不落盘（对话框留在原地等用户改）。"""
    cdir = tmp_path / "characters"
    dlg = CharacterEditorDialog(None, cdir)
    dlg.style_edit.setPlainText("短句")
    dlg.save()

    assert dlg.result() != QDialog.DialogCode.Accepted, "空名不得 accept"
    assert dlg.error_label.text().strip(), "应有就地错误提示"
    assert not cdir.exists() or _names(cdir) == [], "不得落盘"


@pytest.mark.parametrize("bad", ["../escape", "a/b", "a\\b", ".hidden", "..", ".",
                                 "坏:名", "坏|名", "坏?名", "CON"])
def test_character_editor_rejects_unsafe_filenames(qapp, tmp_path, bad):
    """名字即文件名：路径分隔符/`..`/前导点/非法字符/保留名一律拒，绝不越出库目录。"""
    cdir = tmp_path / "characters"
    cdir.mkdir(parents=True)
    dlg = CharacterEditorDialog(None, cdir)
    dlg.name_edit.setText(bad)
    dlg.save()

    assert dlg.result() != QDialog.DialogCode.Accepted, "不得 accept"
    assert dlg.error_label.text().strip(), "应有就地错误提示"
    assert _names(cdir) == [], "库目录里不得多出文件或子目录"
    assert _names(tmp_path) == ["characters"], "不得写到库目录之外"


def test_library_module_drops_circle_entirely():
    """回归守门：Circle 已随对话圈（§3.2）从 schema 删除，library 里不得再有任何引用——
    之前的一处 `from ..schemas import Circle` 就足以让整包不可导入、四个 GUI 测试模块集体
    收集失败。顺带确认「重置场景」的纯函数内核可从模块直接取用。"""
    src = Path(lib.__file__).read_text(encoding="utf-8")
    assert "circle" not in src.lower(), "library 里不该再出现对话圈的字眼或引用"
    assert not hasattr(lib, "Circle")
    assert callable(lib.reset_scene_runtime), "重置场景的纯函数应导出可复用"


def test_default_scene_filename_is_a_safe_slug(tmp_path):
    """新建时的缺省文件名：场景名 → 安全 slug（非法字符/路径分隔符/前导点一律消化掉）。"""
    assert lib.default_scene_filename("餐厅") == "餐厅.json"
    for raw in ("../逃逸", "a/b", "a\\b", ".hidden", "坏:名", "坏|名", "坏?名", "CON", ""):
        got = lib.default_scene_filename(raw, tmp_path)
        assert lib.scene_filename_error(got) == "", f"{raw!r} → {got!r} 不是合法文件名"
    assert lib.default_scene_filename("", tmp_path) == "场景.json"
    # 目录里已占用时自动让名（新建即能存下去，不覆盖别人的场景）
    (tmp_path / "餐厅.json").write_text("{}", encoding="utf-8")
    assert lib.default_scene_filename("餐厅", tmp_path) == "餐厅2.json"


def test_library_copy_rejects_unsafe_scene_copy_name(qapp, tmp_path, monkeypatch):
    """源场景文件名当不得副本名时（前导点 → 会变隐藏文件），复制必须拒绝且不落盘。

    角色卡没有「复制」了（角色不在这里管，§3.3），但场景复制的同一套守门还在。
    """
    cdir, sdir = tmp_path / "characters", tmp_path / "scenes"
    cdir.mkdir(parents=True)
    sdir.mkdir(parents=True)
    (sdir / ".hidden.json").write_text(
        json.dumps({"name": "藏起来"}, ensure_ascii=False), encoding="utf-8")
    warned: list = []
    monkeypatch.setattr(lib, "warn", lambda *a, **k: warned.append(a))

    dlg = LibraryDialog(cdir, sdir)
    dlg.scene_list.setCurrentRow(0)
    dlg.copy_scene()

    assert warned, "应弹提示而不是静默失败"
    assert dlg.scene_list.count() == 1, "列表不应多出条目"
    assert _names(sdir) == [".hidden.json"], "不得写出副本"
    assert _names(tmp_path) == ["characters", "scenes"], "不得写到库之外"


def test_character_editor_prefills_existing_card(qapp, tmp_path):
    """编辑既有卡：各部件按卡内容预填，保存后名字/权重不变。"""
    cdir = tmp_path / "characters"
    _write_chars(cdir, "戊")
    card = load_character_card(cdir / "戊.json")
    dlg = CharacterEditorDialog(card, cdir)
    assert dlg.name_edit.text() == "戊"
    assert "示例" in dlg.personality_edit.toPlainText()
    dlg.save()
    assert load_character_card(cdir / "戊.json").name == "戊"


# ----------------------------------------------------------- SceneEditorDialog
def test_scene_editor_rejects_unsafe_filename(qapp, tmp_path):
    """文件名不得越出 scenes/（其余字段合法，只有文件名有问题）——就地报错、不写盘。"""
    cdir, sdir = tmp_path / "characters", tmp_path / "scenes"
    _write_chars(cdir, "甲")
    dlg = SceneEditorDialog(None, sdir, cdir)
    dlg.name_edit.setText("天台")
    dlg.filename_edit.setText("../escape.json")
    _check(dlg, "甲")
    dlg.save()

    assert dlg.result() != QDialog.DialogCode.Accepted
    assert dlg.error_label.text().strip()
    assert _names(sdir) == []
    assert "escape.json" not in _names(tmp_path), "不得写到库目录之外"


def test_scene_editor_roundtrips_every_new_field(qapp, tmp_path):
    """填满新模型的每个字段 → 存到「选定目录 + 选定文件名」→ 回读逐项一致。"""
    cdir, sdir = tmp_path / "characters", tmp_path / "scenes"
    _write_chars(cdir, "甲", "乙")
    dlg = SceneEditorDialog(None, sdir, cdir)
    dlg.name_edit.setText("天台上的告白")
    dlg.filename_edit.setText("rooftop.json")        # 文件名与场景名解耦（§3.4）
    dlg.date_check.setChecked(True)
    dlg.date_year.setValue(2031)
    dlg.date_month.setValue(7)
    dlg.date_day.setValue(9)
    dlg.start_time_edit.setText("19:05")
    dlg.background_edit.setPlainText("近未来的海边城市，梅雨季。")
    dlg.description_edit.setPlainText("天台上有几把旧椅子。")
    dlg.description_mutable.setChecked(True)
    dlg.plot_edit.setPlainText("两人从试探到摊牌。")
    dlg.boundary_type.setCurrentIndex(_idx(dlg.boundary_type, "time"))
    dlg.boundary_value.setText("22:30")
    dlg.boundary_desc.setText("末班车")
    _check(dlg, "甲")
    _check(dlg, "乙")
    dlg.add_hook(Hook(id="", condition="乙说出「我早就知道」", event_kind="character",
                      character_name="乙", action="mute_turns", turns=2, visible=False,
                      note="摊牌"))
    dlg.add_hook(Hook(id="", condition="下雨了", event_kind="context",
                      context_text="雨点砸在铁栏上。"))

    dlg.save()

    assert dlg.result() == QDialog.DialogCode.Accepted, "保存成功应 accept"
    assert dlg.saved_path == sdir / "rooftop.json", "存到选定目录 + 选定文件名"
    assert dlg.saved_path.exists()
    scene = load_scene(dlg.saved_path)
    assert scene.name == "天台上的告白"
    assert scene.date == "2031-07-09"
    assert scene.background == "近未来的海边城市，梅雨季。"
    assert scene.description == "天台上有几把旧椅子。"
    assert scene.description_mutable is True
    assert scene.plot_direction == "两人从试探到摊牌。"
    assert scene.start_time == "19:05"
    # 候选列表按扫描序（CJK 码点序：乙 < 甲），故只比集合；顺序断言在 picker 用例里。
    assert sorted(scene.participants) == sorted(["甲", "乙"])
    assert [m.entered_round for m in scene.characters] == [0, 0]
    assert scene.hard_boundary is not None
    assert (scene.hard_boundary.type, scene.hard_boundary.value,
            scene.hard_boundary.desc) == ("time", "22:30", "末班车")
    assert [h.id for h in scene.hooks] == ["h1", "h2"], "新 hook 自动补 id"
    assert scene.hooks[0].action == "mute_turns" and scene.hooks[0].turns == 2
    assert scene.hooks[0].visible is False
    assert scene.hooks[1].context_text == "雨点砸在铁栏上。"


def test_scene_editor_date_off_saves_empty_and_keeps_other_fields(qapp, tmp_path):
    """日期不勾 = 存空串；勾了再取消也回到空串，且不影响其它字段。"""
    cdir, sdir = tmp_path / "characters", tmp_path / "scenes"
    _write_chars(cdir, "甲")
    dlg = SceneEditorDialog(None, sdir, cdir)
    dlg.name_edit.setText("无日场")
    dlg.date_year.setValue(2031)
    dlg.date_month.setValue(7)
    dlg.date_day.setValue(9)
    assert not dlg.date_check.isChecked(), "缺省不设日期"
    assert not dlg.date_year.isEnabled(), "不设日期时年月日不可编辑"
    dlg.date_check.setChecked(True)
    assert dlg.date_year.isEnabled()
    dlg.save()
    assert load_scene(dlg.saved_path).date == "2031-07-09"

    dlg2 = SceneEditorDialog(load_scene(dlg.saved_path), sdir, cdir)
    assert dlg2.date_check.isChecked() and dlg2.date_year.value() == 2031
    dlg2.date_check.setChecked(False)
    dlg2.filename_edit.setText("无日场2.json")
    dlg2.save()
    assert load_scene(sdir / "无日场2.json").date == ""

    # 旧文件的自由文本日期：不动三个数字框就原样保留，动了才换算成 ISO
    legacy = Scene(name="旧日期", date="某个夏天")
    dlg3 = SceneEditorDialog(legacy, sdir, cdir)
    assert dlg3.date_check.isChecked(), "有日期就勾上"
    assert dlg3.build_scene().date == "某个夏天", "没动过就得原样留着"
    dlg3.date_year.setValue(1999)
    assert dlg3.build_scene().date == "%04d-%02d-%02d" % (
        dlg3.date_year.value(), dlg3.date_month.value(),
        dlg3.date_day.value()), "动过则写 ISO"


def test_scene_editor_boundary_none_disables_value_fields(qapp, tmp_path):
    """硬边界「无」：值/描述两个输入框停用，存下去的是 type="none"。"""
    cdir, sdir = tmp_path / "characters", tmp_path / "scenes"
    _write_chars(cdir, "甲")
    dlg = SceneEditorDialog(None, sdir, cdir)
    dlg.name_edit.setText("空场")
    dlg.boundary_desc.setText("打烊")                 # 先选时间边界填上值…

    dlg.boundary_type.setCurrentIndex(_idx(dlg.boundary_type, "time"))
    assert dlg.boundary_value.isEnabled() and dlg.boundary_desc.isEnabled()

    dlg.boundary_type.setCurrentIndex(_idx(dlg.boundary_type, "none"))
    assert not dlg.boundary_value.isEnabled(), "「无」边界时值不可编辑"
    assert not dlg.boundary_desc.isEnabled(), "「无」边界时描述不可编辑"
    dlg.boundary_value.setText("")
    dlg.boundary_desc.setText("")
    dlg.save()

    assert dlg.result() == QDialog.DialogCode.Accepted
    scene = load_scene(dlg.saved_path)
    assert scene.hard_boundary is not None
    assert scene.hard_boundary.type == "none"
    assert (scene.hard_boundary.value, scene.hard_boundary.desc) == ("", "")


def test_scene_editor_participant_picker(qapp, tmp_path):
    """候选列表：按出场作品筛选收窄、最多 6 行定高滚动、勾选顺序 = characters 顺序。"""
    cdir, sdir = tmp_path / "characters", tmp_path / "scenes"
    for i in range(1, 8):
        _write_card(cdir, f"p{i}", source="作品A" if i <= 4 else "作品B")
    _write_card(cdir, "q1", source="作品B")

    dlg = SceneEditorDialog(None, sdir, cdir)
    dlg.name_edit.setText("多人场")
    assert dlg.filter_combo.itemText(0) == "全部"
    assert {dlg.filter_combo.itemText(i) for i in range(dlg.filter_combo.count())} \
        == {"全部", "作品A", "作品B"}
    # 8 个候选也不无限拉长：高度封顶在 6 行（超出即滚动）
    assert dlg.participant_list.maximumHeight() == 6 * 24 + 8
    assert dlg.participant_list.maximumHeight() < 200

    # 出场作品筛选：选「作品A」只剩 4 行可见，且高度跟着收到 4 行
    dlg.filter_combo.setCurrentIndex(_idx(dlg.filter_combo, "作品A"))
    assert [n for n, it in dlg.participant_items.items() if not it.isHidden()] \
        == ["p1", "p2", "p3", "p4"]
    assert dlg.participant_list.maximumHeight() == 4 * 24 + 8
    # 被筛掉的行只是隐藏，勾选状态仍在（切回「全部」即见）
    _check(dlg, "p1")
    dlg.filter_combo.setCurrentIndex(0)
    assert not dlg.participant_items["p1"].isHidden()
    assert dlg.participant_list.maximumHeight() == 6 * 24 + 8

    # 勾 3 个 → characters 按列表顺序；取消勾选即移出
    _check(dlg, "q1")
    _check(dlg, "p2")
    assert dlg.checked_characters() == ["p1", "p2", "q1"]
    _check(dlg, "p2", False)
    assert dlg.checked_characters() == ["p1", "q1"]
    dlg.save()
    assert load_scene(dlg.saved_path).participants == ["p1", "q1"]

    # 原在场顺序优先：旧场景 [q1, p3] 存回去仍是 [q1, p3]（不按列表排布重排），新勾的追加
    existing = Scene(name="旧场", characters=[
        SceneCastMember(name="q1", entered_at="21:40", entered_round=4),
        SceneCastMember(name="p3")])
    dlg2 = SceneEditorDialog(existing, sdir, cdir)
    _check(dlg2, "p1")
    assert dlg2.checked_characters() == ["q1", "p3", "p1"]
    assert [m.entered_at for m in dlg2.build_scene().characters] == ["21:40", "", ""], \
        "编辑不该抹掉既有入场记录"


def test_scene_editor_allows_empty_cast_but_requires_cards(qapp, tmp_path):
    """场景可以一个角色都没有（§3.3）；但勾上的角色必须有卡——缺卡就地报错、不落盘。"""
    cdir, sdir = tmp_path / "characters", tmp_path / "scenes"
    _write_chars(cdir, "甲")
    dlg = SceneEditorDialog(None, sdir, cdir)
    dlg.name_edit.setText("空场")

    dlg.save()                                   # 没勾任何人 → 允许（§3.3 场景可以没有角色）
    assert dlg.result() == QDialog.DialogCode.Accepted
    assert load_scene(dlg.saved_path).participants == []

    # 库里没卡的旧名字照样列出、勾着；存盘被拦（引擎缺卡开不了场）
    ghost = Scene(name="旧场", characters=[SceneCastMember(name="甲"),
                                           SceneCastMember(name="幽灵")])
    dlg2 = SceneEditorDialog(ghost, sdir, cdir)
    assert "幽灵" in dlg2.participant_items, "库里没卡的旧在场者也要列出来（不悄悄丢人）"
    assert dlg2.checked_characters() == ["甲", "幽灵"]
    dlg2.filename_edit.setText("旧场2.json")
    dlg2.save()
    assert dlg2.result() != QDialog.DialogCode.Accepted, "缺卡的场景不得存盘"
    assert "角色卡" in dlg2.error_label.text()
    assert _names(sdir) == ["空场.json"], "缺卡那次不得落盘"


def test_scene_editor_validates_name_and_filename(qapp, tmp_path):
    """空场景名 / 空文件名 / 不以 .json 结尾 → 各自就地报错且不落盘。"""
    cdir, sdir = tmp_path / "characters", tmp_path / "scenes"
    _write_chars(cdir, "甲")
    dlg = SceneEditorDialog(None, sdir, cdir)

    dlg.save()                                   # 无名
    assert dlg.result() != QDialog.DialogCode.Accepted
    assert "场景名" in dlg.error_label.text()

    dlg.name_edit.setText("天台")
    dlg.filename_edit.setText("")
    dlg.save()                                   # 有名，文件名空
    assert dlg.result() != QDialog.DialogCode.Accepted
    assert "文件名" in dlg.error_label.text()

    dlg.filename_edit.setText("天台")            # 少了 .json，扫不到
    dlg.save()
    assert dlg.result() != QDialog.DialogCode.Accepted
    assert ".json" in dlg.error_label.text()
    assert _names(sdir) == [], "三次都不得落盘"


def test_scene_editor_validates_hhmm_times(qapp, tmp_path):
    """开始时间与时间边界值都必须是 HH:MM（否则引擎静默当无边界/开场报错）；「无」不校验。"""
    cdir, sdir = tmp_path / "characters", tmp_path / "scenes"
    _write_chars(cdir, "甲")
    dlg = SceneEditorDialog(None, sdir, cdir)
    dlg.name_edit.setText("天台")

    dlg.start_time_edit.setText("25:00")
    dlg.save()
    assert dlg.result() != QDialog.DialogCode.Accepted
    assert "开始时间" in dlg.error_label.text()

    dlg.start_time_edit.setText("19:05")
    dlg.boundary_type.setCurrentIndex(_idx(dlg.boundary_type, "none"))
    dlg.save()                                   # 无边界：值/描述不参与校验
    assert dlg.result() == QDialog.DialogCode.Accepted
    assert load_scene(sdir / "天台.json").start_time == "19:05"

    dlg2 = SceneEditorDialog(None, sdir, cdir)
    dlg2.name_edit.setText("有边界")
    dlg2.boundary_type.setCurrentIndex(_idx(dlg2.boundary_type, "time"))
    dlg2.boundary_value.setText("打烊")           # 时间边界的值却不是时间
    dlg2.save()
    assert dlg2.result() != QDialog.DialogCode.Accepted
    assert "时间边界" in dlg2.error_label.text()

    dlg2.boundary_value.setText("22:00")          # 改对 → 可存
    dlg2.save()
    assert dlg2.result() == QDialog.DialogCode.Accepted
    scene = load_scene(sdir / "有边界.json")
    assert scene.hard_boundary is not None and scene.hard_boundary.value == "22:00"


def test_scene_editor_hooks_add_edit_delete(qapp, tmp_path, monkeypatch):
    """hooks 列表：新建/编辑/删除都反映到列表与落盘；不合法的 hook 拦住保存并显示原因。"""
    cdir, sdir = tmp_path / "characters", tmp_path / "scenes"
    _write_chars(cdir, "甲")
    dlg = SceneEditorDialog(None, sdir, cdir)
    dlg.name_edit.setText("钩子场")

    # 不合法（条件为空）→ 保存被拦下，弹的是 hooks.validate 的原文，且不落盘
    dlg.add_hook(Hook(id="", condition="", event_kind="context", context_text="灯灭了。"))
    assert dlg.hook_list.count() == 1
    assert "灯灭了" in dlg.hook_list.item(0).text(), "列表行 = hooks.describe(hook)"
    dlg.save()
    assert dlg.result() != QDialog.DialogCode.Accepted
    assert "触发条件不能为空" in dlg.error_label.text()
    assert _names(sdir) == [], "有非法 hook 时不得落盘"

    # 补上条件 → 可存，hook 逐字段回读
    dlg.hooks[0] = Hook(id=dlg.hooks[0].id, condition="甲说出「再见」",
                        event_kind="context", context_text="灯灭了。",
                        visible=False, note="收场")
    dlg._refresh_hook_list()
    dlg.save()
    assert dlg.result() == QDialog.DialogCode.Accepted
    assert dlg.saved_path.exists()
    scene = load_scene(dlg.saved_path)
    assert len(scene.hooks) == 1
    assert scene.hooks[0].id == "h1"
    assert scene.hooks[0].condition == "甲说出「再见」"
    assert scene.hooks[0].context_text == "灯灭了。"
    assert scene.hooks[0].visible is False and scene.hooks[0].note == "收场"

    # 编辑：子对话框返回改过的一条 → 列表与 build_scene 同步，id 保持不变
    monkeypatch.setattr(lib, "HookEditorDialog", _fake_hook_dialog(
        Hook(id="", condition="甲退场", event_kind="character", character_name="甲",
             action="remove", enabled=False)))
    dlg.hook_list.setCurrentRow(0)
    dlg.edit_hook()
    assert len(dlg.hooks) == 1, "编辑不该多出一条"
    assert dlg.hooks[0].id == "h1", "编辑保留原 id"
    assert dlg.hooks[0].event_kind == "character" and dlg.hooks[0].action == "remove"
    assert dlg.hooks[0].enabled is False
    assert "甲退场" in dlg.hook_list.item(0).text()
    assert "已停用" in dlg.hook_list.item(0).text(), "停用的 hook 在描述里标出来"

    # 新建：子对话框接受 → 追加一条
    dlg.new_hook()
    assert len(dlg.hooks) == 2 and dlg.hook_list.count() == 2
    assert dlg.hooks[1].id == "h2"

    # 删除：选中一行即删
    dlg.hook_list.setCurrentRow(0)
    dlg.delete_hook()
    assert len(dlg.hooks) == 1 and dlg.hook_list.count() == 1
    dlg.save()
    assert [h.id for h in load_scene(dlg.saved_path).hooks] == ["h2"]


def test_hook_editor_dialog_builds_and_validates(qapp):
    """Hook 子编辑器：分类字段随类型走、静默轮数只在「静默 N 轮」时可用、非法不关窗。"""
    dlg = lib.HookEditorDialog(None, cast=["甲", "乙"])
    dlg.condition_edit.setPlainText("甲离场后")
    dlg.kind_combo.setCurrentIndex(_idx(dlg.kind_combo, "character"))
    dlg.character_combo.setCurrentText("甲")
    dlg.action_combo.setCurrentIndex(_idx(dlg.action_combo, "mute_turns"))
    assert dlg.turns_spin.isEnabled()
    dlg.turns_spin.setValue(3)
    dlg.action_combo.setCurrentIndex(_idx(dlg.action_combo, "unmute"))
    assert not dlg.turns_spin.isEnabled(), "非静默轮数时轮数不可编辑"
    dlg.action_combo.setCurrentIndex(_idx(dlg.action_combo, "mute_turns"))
    dlg.save()
    assert dlg.result() == QDialog.DialogCode.Accepted
    hook = dlg.build_hook()
    assert (hook.event_kind, hook.character_name, hook.action, hook.turns) \
        == ("character", "甲", "mute_turns", 3)

    bad = lib.HookEditorDialog(None, cast=[])
    bad.condition_edit.setPlainText("条件")
    bad.kind_combo.setCurrentIndex(_idx(bad.kind_combo, "character"))
    bad.save()
    assert bad.result() != QDialog.DialogCode.Accepted, "缺角色名的角色事件不得关窗"
    assert "角色名" in bad.error_label.text()

    scene_hook = lib.HookEditorDialog(
        Hook(id="h9", condition="灯灭", event_kind="scene",
             scene_patch={"description": "桌椅都撤了"}), cast=[])
    assert scene_hook.build_hook().scene_patch == {"description": "桌椅都撤了"}, \
        "键值对回读"
    assert scene_hook.build_hook().id == "h9"


def test_scene_patch_field_keys_match_engine_whitelist():
    """编辑器教的字段键 = 引擎 `_SCENE_HOOK_KEYS` 白名单（错一个 patch 就被静默忽略）。"""
    from harness.engine import _SCENE_HOOK_KEYS  # 引擎侧的唯一权威（只读比对）

    assert tuple(k for k, _label in lib.SCENE_PATCH_FIELDS) == tuple(_SCENE_HOOK_KEYS)


def test_hook_editor_placeholder_teaches_real_engine_keys(qapp):
    """占位符/字段说明只出现**真键**（description），不再教「描述」这种会被忽略的写法。"""
    dlg = lib.HookEditorDialog(None, cast=[])
    placeholder = dlg.patch_edit.placeholderText()
    assert "description" in placeholder, f"占位符应给真键：{placeholder!r}"
    assert "描述：" not in placeholder, "占位符不得再教中文展示名"

    labels = " ".join(lb.text() for lb in dlg.findChildren(lib.QLabel))
    for key in ("name", "description", "background", "plot_direction", "date"):
        assert key in labels, f"字段说明应列出真键 {key}"


def test_hook_editor_scene_patch_maps_display_labels_to_engine_keys(qapp):
    """用户敲中文展示名（或旧占位符里的「描述」）→ 组装 hook 时一律换成引擎真键。

    引擎白名单只认 name/description/background/plot_direction/date；展示名写进去会被
    静默忽略（hook 看起来生效、实际什么都没改）。故这里把展示名映射回真键，未知键
    原样保留（引擎会以「想改不可改的字段」记一条诊断）。
    """
    dlg = lib.HookEditorDialog(None, cast=[])
    dlg.condition_edit.setPlainText("灯灭")
    dlg.kind_combo.setCurrentIndex(_idx(dlg.kind_combo, "scene"))
    dlg.patch_edit.setPlainText("场景描述：桌椅都撤了\n背景：夜里的海边\n自定义键：原样留")

    hook = dlg.build_hook()

    assert hook.scene_patch == {"description": "桌椅都撤了",
                                "background": "夜里的海边",
                                "自定义键": "原样留"}
    dlg.save()
    assert dlg.result() == QDialog.DialogCode.Accepted, "合法补丁不该被拦下"


def test_hook_editor_accepts_the_short_alias_taught_by_the_old_placeholder(qapp):
    """旧占位符教的「描述：…」仍然可用（映射成 description），不打断老用户的手感。"""
    dlg = lib.HookEditorDialog(None, cast=[])
    dlg.condition_edit.setPlainText("灯灭")
    dlg.kind_combo.setCurrentIndex(_idx(dlg.kind_combo, "scene"))
    dlg.patch_edit.setPlainText("描述：桌椅都撤了")
    assert dlg.build_hook().scene_patch == {"description": "桌椅都撤了"}


def test_reset_scene_runtime_is_pure():
    """重置场景的纯函数内核：配置一字不动、入场记录归零、入参不被改动。"""
    scene = Scene(
        name="餐厅", date="2031-07-09", background="bg", description="桌椅",
        description_mutable=True, plot_direction="摊牌",
        hooks=[Hook(id="h1", condition="到点", event_kind="scene",
                    scene_patch={"描述": "空"})],
        hard_boundary=HardBoundary(type="time", value="22:00", desc="打烊"),
        start_time="21:30",
        characters=[SceneCastMember(name="甲", entered_at="21:40", entered_round=7),
                    SceneCastMember(name="乙", entered_at="22:00", entered_round=9)])

    out = lib.reset_scene_runtime(scene)

    assert out is not scene, "应返回副本"
    assert out.participants == ["甲", "乙"], "在场角色保留（§5：保留配置与在场角色）"
    assert [(m.entered_at, m.entered_round) for m in out.characters] == [("", 0), ("", 0)]
    assert out.name == "餐厅" and out.date == "2031-07-09" and out.description == "桌椅"
    assert out.description_mutable is True and out.plot_direction == "摊牌"
    assert out.hooks == scene.hooks and out.hard_boundary == scene.hard_boundary
    assert out.start_time == "21:30"
    # 入参一字不改
    assert (scene.characters[0].entered_at, scene.characters[0].entered_round) \
        == ("21:40", 7)


def test_scene_editor_reset_button_writes_cleared_scene(qapp, tmp_path, monkeypatch):
    """「重置场景」按钮：确认后把入场记录清零的场景写回原文件；取消则一字不动。"""
    cdir, sdir = tmp_path / "characters", tmp_path / "scenes"
    _write_chars(cdir, "甲")
    path = _write_scene(sdir, "占位", ["甲"])
    save_scene(Scene(name="旧场", description="桌椅",
                     characters=[SceneCastMember(name="甲", entered_at="21:40",
                                                 entered_round=5)]),
               path)
    dlg = SceneEditorDialog(load_scene(path), sdir, cdir, None, path)
    assert dlg.filename_edit.text() == "占位.json", "编辑既有场景沿用它的文件名"

    calls: list = []
    monkeypatch.setattr(lib, "confirm", lambda *a, **k: calls.append(a) or False)
    assert dlg.reset_scene() is False and not dlg.reset_done, "取消确认不得动盘"
    assert load_scene(path).characters[0].entered_round == 5, "取消时盘上原样"

    monkeypatch.setattr(lib, "confirm", lambda *a, **k: True)
    assert dlg.reset_scene() is True
    assert dlg.reset_done
    back = load_scene(path)
    assert (back.characters[0].entered_at, back.characters[0].entered_round) == ("", 0)
    assert back.name == "旧场" and back.description == "桌椅", "配置与在场角色保留"


# ---------------------------------------------------------------- 场景库（§3.3）
def _texts(widget) -> list[str]:
    """窗内全部用户可见文案（标题 + Label + 按钮）——扫 emoji 用，不依赖控件层级。"""
    out = [widget.windowTitle()]
    out += [lb.text() for lb in widget.findChildren(QLabel)]
    out += [b.text() for b in widget.findChildren(QAbstractButton)]
    return [t for t in out if t]


def test_library_is_scenes_only(qapp, tmp_path):
    """场景库只列场景（§3.3）：没有角色列表、没有角色按钮、也没有角色相关的守门。"""
    cdir, sdir = tmp_path / "characters", tmp_path / "scenes"
    _write_chars(cdir, "甲", "乙")
    _write_scene(sdir, "餐厅", ["甲", "乙"])
    _write_scene(sdir, "空场", [])                    # 一个角色都没有的场景

    dlg = LibraryDialog(cdir, sdir)

    assert not hasattr(dlg, "character_list"), "不该再有角色列"
    for gone in ("new_char_btn", "edit_char_btn", "copy_char_btn", "delete_char_btn",
                 "new_character", "edit_character", "copy_character",
                 "delete_character"):
        assert not hasattr(dlg, gone), f"角色侧入口 {gone} 应已删除"
    # 窗里只有一个列表控件（场景列）——不是「两列库」
    assert len(dlg.findChildren(QListWidget)) == 1

    shown = {dlg.scene_list.item(i).text() for i in range(dlg.scene_list.count())}
    assert shown == {"餐厅", "空场"}, "应列出目录里的两个场景"
    row = next(i for i in range(2) if dlg.scene_list.item(i).text() == "餐厅")
    assert Path(dlg.scene_list.item(row).data(Qt.ItemDataRole.UserRole)) \
        == sdir / "餐厅.json"
    assert dlg.scene_list.item(row).toolTip() == str(sdir / "餐厅.json"), "tooltip=路径"


def test_library_shows_corrupt_scene_by_filename(qapp, tmp_path):
    """坏 json 不该让列表崩掉：读不出名字就回退文件名 stem（错误留到打开时暴露）。"""
    cdir, sdir = tmp_path / "characters", tmp_path / "scenes"
    sdir.mkdir(parents=True)
    (sdir / "坏场景.json").write_text("{ 这不是合法 json", encoding="utf-8")

    dlg = LibraryDialog(cdir, sdir)
    assert [dlg.scene_list.item(i).text() for i in range(dlg.scene_list.count())] \
        == ["坏场景"]


def test_library_selection_sets_chosen_scene_without_any_character_gate(qapp, tmp_path):
    """空阵容场景：选中即能「打开」——不看角色（§3.3 场景可以没有人）。"""
    cdir, sdir = tmp_path / "characters", tmp_path / "scenes"
    cdir.mkdir(parents=True)                          # 库目录里一张卡都没有
    _write_scene(sdir, "空场", [])

    dlg = LibraryDialog(cdir, sdir)
    assert not dlg.use_btn.isEnabled(), "没选场景时「打开」不可用"
    dlg.scene_list.setCurrentRow(0)
    assert dlg.use_btn.isEnabled(), "选了场景就该能打开（与角色无关）"
    assert dlg.chosen_scene is None, "还没点「打开」不该带出选择"

    dlg.use_btn.click()
    assert dlg.result() == QDialog.DialogCode.Accepted
    assert dlg.chosen_scene == sdir / "空场.json"
    assert dlg.chosen_characters == [], "场景库不再产出角色表（阵容随场景文件走）"


def test_library_buttons_are_wired_to_the_same_actions(qapp, tmp_path, monkeypatch):
    """五个按钮 + 「打开」各自接到原来的那套函数上（只搬位置，不改行为）。"""
    cdir, sdir = tmp_path / "characters", tmp_path / "scenes"
    _write_scene(sdir, "餐厅", [])
    calls: list[str] = []

    def _spy(n: str):
        # 只收 self：PySide 会把 clicked 的 bool 塞给任何「还能收一个参数」的可调用体，
        # 故这里不能写成带默认值的 lambda（那个默认值会被 checked=False 顶掉）。
        def slot(_self):
            calls.append(n)
        return slot

    for name in ("new_scene", "edit_scene", "copy_scene", "delete_scene",
                 "import_from_template", "use_this"):
        monkeypatch.setattr(LibraryDialog, name, _spy(name))

    dlg = LibraryDialog(cdir, sdir)
    dlg.scene_list.setCurrentRow(0)
    for btn, want in ((dlg.new_scene_btn, "new_scene"),
                      (dlg.edit_scene_btn, "edit_scene"),
                      (dlg.copy_scene_btn, "copy_scene"),
                      (dlg.delete_scene_btn, "delete_scene"),
                      (dlg.import_btn, "import_from_template"),
                      (dlg.use_btn, "use_this")):
        btn.click()
        assert calls[-1] == want, f"{want} 按钮没接到 {want}"

    assert dlg.delete_scene_btn.objectName() == "danger", "删除是破坏性动作，走 danger 样式"
    assert dlg.use_btn.objectName() == "primary", "「打开」是主行动"


def test_library_lists_duplicates_and_deletes_scenes(qapp, tmp_path, monkeypatch):
    """场景库的列举 → 复制出新文件（名带「副本」）→ 确认后删除只删文件。"""
    cdir, sdir = tmp_path / "characters", tmp_path / "scenes"
    _write_scene(sdir, "餐厅", [])

    dlg = LibraryDialog(cdir, sdir)
    dlg.scene_list.setCurrentRow(0)
    dlg.copy_scene()

    assert dlg.scene_list.count() == 2
    idx = next(i for i in range(2) if dlg.scene_list.item(i).text() == "餐厅 副本")
    copy_path = Path(dlg.scene_list.item(idx).data(Qt.ItemDataRole.UserRole))
    assert copy_path.exists() and copy_path != sdir / "餐厅.json"
    assert load_scene(copy_path).name == "餐厅 副本"

    monkeypatch.setattr(lib, "confirm", lambda *a, **k: True)
    dlg.scene_list.setCurrentRow(idx)
    dlg.delete_scene()
    assert not copy_path.exists(), "确认后应删除文件"
    assert dlg.scene_list.count() == 1
    assert (sdir / "餐厅.json").exists(), "其余文件不受影响"


# ---------------------------------------------------------------- 视觉语言（§3.6）
def test_library_dialog_styling_comes_from_the_themed_sheet(qapp, tmp_path, monkeypatch):
    """深色主题下弹窗整份样式表 = `dialog_qss(深色)`：不残留浅色（不会白底黑字）。"""
    cdir, sdir = tmp_path / "characters", tmp_path / "scenes"
    _write_scene(sdir, "餐厅", [])
    qapp.setProperty("theme", "深色")
    try:
        dlg = LibraryDialog(cdir, sdir)
        assert dlg.styleSheet() == dialog_qss("深色")
        dark = PALETTES["深色"]
        for colour in (dark.window_bg, dark.card_bg, dark.text, dark.hairline):
            assert colour in dlg.styleSheet(), f"深色表里应有 {colour}"
        assert PALETTES["默认"].window_bg not in dlg.styleSheet(), "不得残留默认浅底"
        assert "#ffffff" not in dlg.styleSheet().lower(), "深色下不得出现纯白"
    finally:
        qapp.setProperty("theme", "")


def test_dialogs_carry_no_emoji_in_their_texts(qapp, tmp_path):
    """§3.6 无 AI 味：各弹窗的标题/标签/按钮文案里不许有 emoji。"""
    cdir, sdir = tmp_path / "characters", tmp_path / "scenes"
    _write_chars(cdir, "甲")
    _write_scene(sdir, "餐厅", ["甲"])
    dialogs = [
        LibraryDialog(cdir, sdir),
        SceneEditorDialog(None, sdir, cdir),
        CharacterEditorDialog(None, cdir),
        lib.HookEditorDialog(None, cast=["甲"]),
        CharacterDetailDialog({"name": "甲", "weights": {"w1_relevance": 0.5}}),
    ]
    for dlg in dialogs:
        for text in _texts(dlg):
            hit = _EMOJI_RE.search(text)
            assert hit is None, f"{type(dlg).__name__} 文案里有 emoji：{text!r}"


# --------------------------------------- 切场守门的「以所选卡为准」口径（cast_from_cards）
def test_validate_materials_cast_from_cards_uses_selection(tmp_path):
    """cast_from_cards=True：演员表以**所选卡**为准，场景文件声明的人缺卡/不在圈里都不再
    拦（引擎会按选择改写演员表）；缺省 False 仍是「场景闭环」旧口径（回归）。"""
    cdir, sdir = tmp_path / "characters", tmp_path / "scenes"
    chars = _write_chars(cdir, "甲", "乙")
    scene = _write_scene(sdir, "餐厅", ["甲", "幽灵"])          # 幽灵没卡
    no_circle = sdir / "无圈.json"                              # 场景连圈都没写
    no_circle.write_text(json.dumps({"name": "无圈", "participants": ["幽灵"]}),
                         encoding="utf-8")
    ghost_only = _write_scene(sdir, "幽灵场", ["幽灵"])

    assert "幽灵" in lib.validate_materials(scene, chars), "旧口径：声明的人缺卡 → 拦下"
    assert "幽灵" in lib.validate_materials(no_circle, chars)
    assert lib.validate_materials(scene, chars, cast_from_cards=True) == ""
    assert lib.validate_materials(no_circle, chars, cast_from_cards=True) == ""
    assert lib.validate_materials(ghost_only, chars, cast_from_cards=True) == ""

    # 选择本身不自洽仍拦下：没选卡 / 卡读不出来（重名亦然）。
    assert lib.validate_materials(scene, [], cast_from_cards=True) != ""
    broken = _write_chars(cdir, "丙")
    broken[0].write_text("{ 这不是合法 json", encoding="utf-8")
    assert lib.validate_materials(scene, broken, cast_from_cards=True) != ""
    assert lib.validate_materials(scene, [cdir / "甲.json"] * 2,
                                  cast_from_cards=True) != ""
    # 时钟字段照旧拦（引擎开场即解析，与演员表无关）。
    bad_time = _write_scene(sdir, "坏钟", ["甲"], start_time="25:99")
    assert lib.validate_materials(bad_time, chars, cast_from_cards=True) != ""


# -------------------------------------------------- 替身 LibraryDialog（切场用）
def _fake_library(scene: Path, chars: list[Path]):
    """替身 LibraryDialog：exec() 直接「接受」并给出预定选择。"""

    class _Fake:
        def __init__(self, *a, **k):
            self.chosen_scene = scene
            self.chosen_characters = list(chars)

        def exec(self):
            return QDialog.DialogCode.Accepted

    return _Fake


def test_mainwindow_switch_passes_cast_from_cards(qapp, tmp_path, monkeypatch):
    """窗口切场跟随 App 口径：start_scene 带 cast_from_cards=True（选中几人就几人在场）。"""
    cdir, sdir = tmp_path / "characters", tmp_path / "scenes"
    chars = _write_chars(cdir, "甲", "乙", "丙")
    scene = _write_scene(sdir, "餐厅", ["甲", "乙"])            # 场景只声明 2 人
    worker = _FakeWorker()
    cfg = AppConfig(scene=scene, characters=chars, models=tmp_path / "models.yaml",
                    bid=tmp_path / "bid.yaml", run_root=tmp_path / "runs",
                    live=False, api_key=None)
    win = MainWindow(worker, cfg)
    monkeypatch.setattr("harness.gui.main_window.LibraryDialog",
                        _fake_library(scene, chars))

    win._library_btn.click()
    assert worker.calls, "选 3 张卡（场景声明 2 人）应开得起来，而非被守门拦下"
    assert worker.calls[-1]["characters"] == chars
    assert worker.calls[-1]["cast_from_cards"] is True


# --------------------------------------------------------------------- app 入口
class _AppWindowStub:
    """替身 MainWindow：记下 cfg 与「是否被要求开场/续演询问」；不起线程、不画界面。"""

    def __init__(self, captured: dict):
        self._captured = captured

    def __call__(self, worker, cfg, settings=None):
        self._captured["cfg"] = cfg
        return self

    def show(self) -> None:
        pass

    def _maybe_resume(self, scene_path) -> None:
        self._captured["resume_asked"] = scene_path

    def start_session(self) -> None:
        self._captured["started"] = True


class _AppWorkerStub:
    def start(self) -> None:
        pass

    def shutdown(self, *a) -> None:
        pass

    def wait(self, *a) -> None:
        pass

    def isRunning(self) -> bool:            # noqa: N802
        return False


def test_app_builds_appconfig_without_any_startup_dialog(qapp, tmp_path, monkeypatch):
    """app.run：**不再弹开场设置窗**（§3.3）——素材由 CLI/内置缺省决定，直接进主界面。

    启动小窗已随 §3.3 **整个删掉**（类与用例都不在了），不是只停用——app 里也没有任何
    「开场设置」分支可走；场景由用户在空状态页 / 「场景」菜单里自己挑，
    `--autostart` 才是「直接开演」的那个开关。
    """
    from harness.gui import app as gui_app

    cdir, sdir = tmp_path / "characters", tmp_path / "scenes"
    chars = _write_chars(cdir, "甲")
    other = _write_scene(sdir, "天台", ["甲"])
    captured: dict = {}

    monkeypatch.setattr("harness.gui.main_window.MainWindow",
                        _AppWindowStub(captured))
    monkeypatch.setattr("harness.gui.worker.SceneWorker", _AppWorkerStub)
    monkeypatch.setattr("PySide6.QtWidgets.QApplication", lambda *a, **k: qapp)
    monkeypatch.setattr(QApplication, "exec", lambda self: 0)
    monkeypatch.setattr(gui_app, "_DEFAULT_RUN_ROOT", tmp_path / "runs")

    argv = ["--stub", "--scene", str(other), "--characters", str(chars[0])]
    assert gui_app.run(argv) == 0
    cfg = captured["cfg"]
    assert cfg.scene == other and cfg.characters == chars, "CLI 素材是权威来源"
    assert cfg.live is False, "--stub 仍应落离线"
    assert "started" not in captured, "没有 --autostart 就不开场（空状态页等用户开）"

    captured.clear()
    assert gui_app.run([*argv, "--autostart"]) == 0
    assert captured.get("started") is True, "--autostart 直接开演"
    assert captured.get("resume_asked") == other, "开场前仍走续演询问（§5）"


# ------------------------------------------------------------------- MainWindow
class _FakeWorker(QObject):
    """替身 worker：只记 start_scene 调用 + 提供 MainWindow 连接的信号（不起线程）。"""
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

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict] = []
        self.cast_ok = False                  # can_cast() 的替身开关

    def start_scene(self, **kwargs) -> None:
        self.calls.append(kwargs)

    # ---- 演员表（角色菜单接的 worker 面；本模块只用到 can_cast）----
    def can_cast(self) -> bool:
        return bool(self.cast_ok)

    def add_character(self, name, notify=None, notify_text="", visible=True) -> None:
        self.calls.append({"add_character": name})

    def remove_character(self, name, notify=None, notify_text="", visible=True) -> None:
        self.calls.append({"remove_character": name})

    def mute_character(self, name, turns: int = 0) -> None:
        self.calls.append({"mute_character": name})

    def unmute_character(self, name) -> None:
        self.calls.append({"unmute_character": name})

    def schedule_cast_change(self, character_name, action, fire_after_rounds,
                             notify=None, notify_text="", visible=True,
                             turns: int = 0) -> None:
        self.calls.append({"schedule_cast_change": character_name})

    def restart(self, *a, **k) -> None:
        pass

    def say(self, text: str) -> None:
        pass

    def can_say(self) -> bool:
        return False

    def set_paused(self, paused: bool) -> None:   # noqa: D102
        pass

    def stop_now(self) -> None:
        pass

    def set_pace(self, seconds: float) -> None:   # noqa: D102
        pass

    def set_rate(self, rate: float) -> None:      # noqa: D102
        pass

    def shutdown(self, *a) -> None:
        pass

    def isRunning(self) -> bool:                  # noqa: N802
        return False


def test_mainwindow_library_button_switches_scene(qapp, tmp_path, monkeypatch):
    """左栏「场景库…」按钮：选中另一套 → cfg 更新、对白区清空、worker 重投开场。"""
    cdir, sdir = tmp_path / "characters", tmp_path / "scenes"
    old_chars = _write_chars(cdir, "甲", "乙")
    old_scene = _write_scene(sdir, "餐厅", ["甲", "乙"])
    new_scene = _write_scene(sdir, "天台", ["乙"])
    new_chars = [cdir / "乙.json"]
    models = tmp_path / "models.yaml"
    models.write_text("think: {backend: stub, model: stub, params: {}}\n"
                      "speak: {backend: stub, model: stub, params: {}}\n",
                      encoding="utf-8")

    worker = _FakeWorker()
    cfg = AppConfig(scene=old_scene, characters=old_chars, models=models,
                    bid=tmp_path / "bid.yaml", run_root=tmp_path / "runs",
                    live=False, api_key=None, opening="夜里。")
    win = MainWindow(worker, cfg)
    assert win._library_btn.text() == "场景库…", "左栏应有库入口按钮"

    monkeypatch.setattr("harness.gui.main_window.LibraryDialog",
                        _fake_library(new_scene, new_chars))

    worker.sig_message.emit({"speaker": "甲", "content": "旧场的一句。"})
    assert "旧场的一句。" in win._view.toPlainText()

    win._library_btn.click()

    assert win._cfg.scene == new_scene, "cfg.scene 应切成库中选中的场景"
    assert win._cfg.characters == new_chars, "cfg.characters 应换成库中选中的角色"
    assert win._view.toPlainText() == "", "换场应清空对白区"
    assert worker.calls, "换场应向 worker 重新投递开场"
    last = worker.calls[-1]
    assert last["scene"] == new_scene and last["characters"] == new_chars
    assert last["opening"] == "夜里。", "沿用开场输入框的当前值"
    assert last["models_yaml"] == models and last["bid"] == cfg.bid, "沿用模型/竞价"
    assert last["run_root"] == cfg.run_root and last["live"] is False


def test_mainwindow_refuses_switch_to_unlaunchable_materials(qapp, tmp_path,
                                                            monkeypatch):
    """素材打不开（选中角色的卡读不出来）→ 只弹提示：cfg/对白区/worker 一律不动，
    当前场次保留。

    注：演员表以所选卡为准（cast_from_cards）后，「场景声明的某人缺卡」不再是开不了场
    的理由（那人只是不在场）；「素材打不开」改由**选择本身**坏掉来制造——卡文件坏掉。
    """
    cdir, sdir = tmp_path / "characters", tmp_path / "scenes"
    chars = _write_chars(cdir, "甲", "幽灵")
    (cdir / "幽灵.json").write_text("{ 坏掉的卡", encoding="utf-8")   # 卡读不出来
    scene = _write_scene(sdir, "餐厅", ["甲"])
    bad = _write_scene(sdir, "坏的", ["甲", "幽灵"])
    worker = _FakeWorker()
    cfg = AppConfig(scene=scene, characters=chars, models=tmp_path / "models.yaml",
                    bid=tmp_path / "bid.yaml", run_root=tmp_path / "runs",
                    live=False, api_key=None)
    win = MainWindow(worker, cfg)
    warned: list = []
    monkeypatch.setattr("harness.gui.main_window.warn",
                        lambda *a, **k: warned.append(a))
    monkeypatch.setattr("harness.gui.main_window.LibraryDialog",
                        _fake_library(bad, chars))

    worker.sig_message.emit({"speaker": "甲", "content": "正在进行的一句。"})
    win._library_btn.click()

    assert warned, "应弹提示说明为何切不过去"
    assert win._cfg.scene == scene and win._cfg.characters == chars, "cfg 不得变"
    assert "正在进行的一句。" in win._view.toPlainText(), "对白区不得被清空"
    assert worker.calls == [], "不得向 worker 重投开场"


def test_mainwindow_library_cancel_keeps_session(qapp, tmp_path, monkeypatch):
    """库对话框取消（Rejected）→ 什么都不换、不重投开场。"""
    cdir, sdir = tmp_path / "characters", tmp_path / "scenes"
    chars = _write_chars(cdir, "甲")
    scene = _write_scene(sdir, "餐厅", ["甲"])
    worker = _FakeWorker()
    cfg = AppConfig(scene=scene, characters=chars, models=tmp_path / "models.yaml",
                    bid=tmp_path / "bid.yaml", run_root=tmp_path / "runs",
                    live=False, api_key=None)
    win = MainWindow(worker, cfg)

    class _Cancel:
        def __init__(self, *a, **k):
            self.chosen_scene = None
            self.chosen_characters = []

        def exec(self):
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr("harness.gui.main_window.LibraryDialog", _Cancel)
    win._library_btn.click()

    assert win._cfg.scene == scene and win._cfg.characters == chars
    assert worker.calls == [], "取消不得重投开场"


# --------------------------------------------------------------- worker 换场
def test_worker_switch_scene_closes_old_engine(qapp, tmp_path):
    """换场（start_scene 再投一次 → _launch）必须 aclose 旧引擎：否则每切一次场漏一个
    AsyncSqliteSaver 连接（_restart 早有此纪律，但切场走的是 _launch，别再漏）。"""
    from harness.gui.worker import SceneWorker

    cdir, sdir = tmp_path / "characters", tmp_path / "scenes"
    chars = _write_chars(cdir, "甲", "乙")
    scene_a = _write_scene(sdir, "甲场", ["甲", "乙"])
    scene_b = _write_scene(sdir, "乙场", ["甲", "乙"], start_time="08:30")
    models = tmp_path / "models.yaml"
    models.write_text("think: {backend: stub, model: stub, params: {}}\n"
                      "speak: {backend: stub, model: stub, params: {}}\n",
                      encoding="utf-8")
    bid = tmp_path / "bid.yaml"
    bid.write_text("interruption_threshold: 0.0\nspeak_threshold: 0.0\nsilence_k: 8\n",
                   encoding="utf-8")

    worker = SceneWorker()
    infos: list[dict] = []
    worker.sig_scene_info.connect(infos.append)
    worker.start()
    try:
        launch = dict(characters=chars, models_yaml=models, live=False, bid=bid,
                      opening=None, run_root=tmp_path / "runs", api_key=None,
                      closing_at_block=None)
        worker.start_scene(scene=scene_a, **launch)
        assert _wait_until(lambda: len(infos) >= 1), "第一场应开场"
        old = worker._engine
        assert old is not None and old._saver is not None, "第一场应有活引擎"

        closed: list[bool] = []
        real_aclose = old.aclose

        async def spy():
            closed.append(True)
            await real_aclose()

        old.aclose = spy                          # noqa: SLF001  (实例级打点)
        worker.start_scene(scene=scene_b, **launch)

        assert _wait_until(lambda: len(infos) >= 2), "第二场应开场"
        assert closed, "切场必须 aclose 旧引擎"
        assert old._saver is None, "旧引擎的 sqlite saver 应已释放"
        assert worker._engine is not old, "第二场应是新引擎"
        assert infos[-1]["scene"]["name"] == "乙场"
    finally:
        worker.shutdown(4000)
    assert not worker.isRunning(), "worker 线程应收尾退出"
