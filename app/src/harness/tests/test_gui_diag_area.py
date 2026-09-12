"""诊断收进日志（《人际关系与场景推进》§三）：场景诊断**只在日志面板打开时**显示。

现状（改前）：场景诊断一到就占住中栏顶部的状态药丸——日志面板关着也照挂，且它和
"进行中/已暂停"争同一个位置。改成：

  · 中栏顶部另有一块**场景诊断区**（一行小字），**只在日志面板打开时**可见；
  · 关闭时**不占位**（不是"隐藏但保留高度"那种把布局顶来顶去的做法），
    开关切换后布局不跳（其余部件原地不动）；
  · 诊断本身照旧进日志窗格（那是它的主去处，日志面板一开就看得见全文）。

全程离屏、不联网、不碰真实用户设置。
"""
import json
import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from harness.gui.main_window import AppConfig, MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    """模块级复用单一 QApplication（Qt 只允许每进程一个）。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def tmp_store(tmp_path, monkeypatch):
    from harness.gui import settings as settings_mod
    path = tmp_path / "settings" / "settings.json"
    monkeypatch.setattr(settings_mod, "default_settings_path", lambda: path)
    return path


class _FakeWorker(QObject):
    """替身 worker：只提供 MainWindow 要连的信号（不起线程、不建引擎）。"""

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
    sig_auto_paused = Signal(bool)
    sig_scene_diag = Signal(dict)

    def can_cast(self) -> bool:
        return False


def _make_window(tmp_path: Path):
    cards = tmp_path / "characters"
    scenes = tmp_path / "scenes"
    cards.mkdir(parents=True, exist_ok=True)
    scenes.mkdir(parents=True, exist_ok=True)
    card_paths = []
    for name in ("甲", "乙"):
        p = cards / f"{name}.json"
        p.write_text(json.dumps({"name": name, "personality": {"描述": "示例"}},
                                ensure_ascii=False), encoding="utf-8")
        card_paths.append(p)
    scene = scenes / "餐厅.json"
    scene.write_text(json.dumps({"name": "餐厅", "participants": ["甲", "乙"]},
                                ensure_ascii=False), encoding="utf-8")
    models = tmp_path / "models.yaml"
    models.write_text("think: {backend: stub, model: stub, params: {}}\n"
                      "speak: {backend: stub, model: stub, params: {}}\n",
                      encoding="utf-8")
    cfg = AppConfig(scene=scene, characters=card_paths, models=models,
                    bid=tmp_path / "bid.yaml", run_root=tmp_path / "runs",
                    live=False, api_key=None, opening="夜里。")
    worker = _FakeWorker()
    return MainWindow(worker, cfg), worker


_DIAG = {"fields": {"name": "餐厅"}, "hooks": ["h1"], "description_mutable": True,
         "language": "zh-Hans", "hook_error": None,
         "tool_error": "场景工具指令未改动任何字段（set_name）",
         "save_error": None, "implicit_events": []}


def _open(win, worker) -> None:
    worker.sig_scene_info.emit({"scene": {"name": "餐厅", "participants": ["甲", "乙"]},
                                "characters": [], "backend": "stub"})


def test_diag_area_is_hidden_while_the_log_pane_is_closed(qapp, tmp_path, tmp_store):
    """日志面板关着 → 诊断区**不可见**（整块不占位），诊断本身照样进日志窗格的留存。

    "不占位"是本节的原话：不能用"隐藏但保留高度"糊弄——那种做法下中栏顶部永远空着
    一条，用户看到的是一块谁都说不清用途的留白。
    """
    win, worker = _make_window(tmp_path)
    _open(win, worker)
    assert win._log_view.isHidden(), "前置：日志面板缺省关着"

    worker.sig_scene_diag.emit(dict(_DIAG))
    assert win._scene_diag_area.isHidden(), "关着的时候诊断区不该露头"
    assert "场景工具指令未改动任何字段" in win._log_view.toPlainText(), \
        "诊断仍要进日志窗格（那是它一开面板就能看全文的地方）"
    assert win._scene_diag_text, "文本要留住（开面板时立刻能显示）"


def test_diag_area_appears_when_the_log_pane_is_opened(qapp, tmp_path, tmp_store):
    """点开日志 → 诊断区出现在中栏上方；再关掉 → 立刻收起（不留高度）。"""
    win, worker = _make_window(tmp_path)
    _open(win, worker)
    worker.sig_scene_diag.emit(dict(_DIAG))

    win._log_btn.setChecked(True)        # 等价于点「日志」（信号已接）
    assert not win._scene_diag_area.isHidden(), "日志面板开着 → 诊断区可见"
    assert "场景工具指令未改动任何字段" in win._scene_diag_label.text()

    win._log_btn.setChecked(False)
    assert win._scene_diag_area.isHidden(), "关掉日志 → 诊断区整块收起"


def test_diag_area_follows_the_pane_without_shifting_the_layout(qapp, tmp_path,
                                                                tmp_store):
    """开关切换后**布局不跳**：关着时诊断区整块收起（`isHidden()`，不是"保留高度"），
    开着时它自己占一行且位于日志/对白**之上**；反复开关几何回到同一处（不累积漂移）。

    "隐藏但保留高度"的做法会让中栏顶部永远空出一条，且每次开关都把对白区顶来顶去
    （几次之后几何会漂），故这里两头都钉：隐藏即 `isHidden()`、反复开关回到同一几何。
    """
    win, worker = _make_window(tmp_path)
    win.resize(1200, 760)
    win.show()
    _open(win, worker)
    worker.sig_scene_diag.emit(dict(_DIAG))

    win._log_btn.setChecked(True)
    assert not win._scene_diag_area.isHidden(), "开着时它自己占一行"
    assert win._scene_diag_area.height() > 0
    assert win._scene_diag_area.y() < win._center_split.y(), "诊断区在日志/对白**上方**"
    with_log = win._view.geometry()

    win._log_btn.setChecked(False)
    assert win._scene_diag_area.isHidden(), "关掉后整块收起（不是保留高度）"
    assert win._view.geometry().height() >= with_log.height()

    win._log_btn.setChecked(True)          # 反复开关：几何回到同一处（不累积漂移）
    assert win._view.geometry() == with_log
    win._log_btn.setChecked(False)


def test_new_diagnostic_updates_the_area_text_while_closed(qapp, tmp_path, tmp_store):
    """关着的时候来的新诊断也要更新文本与去重签名——一开面板立刻看到最新那条。"""
    win, worker = _make_window(tmp_path)
    _open(win, worker)
    worker.sig_scene_diag.emit(dict(_DIAG))
    first = win._scene_diag_text

    worker.sig_scene_diag.emit({**_DIAG, "tool_error": "又一条诊断"})
    assert win._scene_diag_text != first and "又一条诊断" in win._scene_diag_text
    win._log_btn.setChecked(True)
    assert "又一条诊断" in win._scene_diag_label.text()


def test_new_scene_clears_the_diag_area_text(qapp, tmp_path, tmp_store):
    """换场：上一场的诊断不跟着走（诊断区清空、日志窗格也清空）。"""
    win, worker = _make_window(tmp_path)
    _open(win, worker)
    worker.sig_scene_diag.emit(dict(_DIAG))
    win._log_btn.setChecked(True)
    assert not win._scene_diag_area.isHidden()

    worker.sig_scene_info.emit({"scene": {"name": "茶室", "participants": ["甲"]},
                                "characters": [], "backend": "stub"})
    assert win._scene_diag_text == "" and win._scene_diag_area.isHidden()
    assert "场景工具指令未改动任何字段" not in win._log_view.toPlainText()


def test_diag_area_does_not_hijack_the_status_chip(qapp, tmp_path, tmp_store):
    """诊断**不再**占住顶部状态药丸——药丸归"进行中/已暂停"这类运行状态（§三的本意）。

    改前：一条场景诊断会顶掉状态药丸（用户因此看不出这场是在跑还是停了）。改后：
    诊断有自己的位置，药丸保持 worker 报来的状态词。
    """
    win, worker = _make_window(tmp_path)
    _open(win, worker)
    worker.sig_status.emit("进行中")
    before = win._status_chip.text()
    worker.sig_scene_diag.emit(dict(_DIAG))
    assert win._status_chip.text() == before == "进行中"
