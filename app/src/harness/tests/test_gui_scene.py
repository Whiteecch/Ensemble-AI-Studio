"""GUI 场景叙述层（离屏冒烟）：居中斜体渲染 + 任意条撤销/改写 + 自动开关与手动推进。

全部离线、确定性（QT_QPA_PLATFORM=offscreen + stub 后端），不联网。锁定界面侧契约：
  · speaker_type=="narrator" 的行渲染成**居中斜体**块（弱化色），带 `撤销`/`改写` 两个
    行内链接（href 带该条 id）；角色/导演/人类行不受影响（不带叙述样式）；
  · 点 `scene:undo:<id>` → **先确认**（回溯会丢弃其后的内容，§6.1）→ worker 撤销该条 →
    该行即刻从对白区消失（本地先摘 + 重绘），引擎共享态 retracted 含该 id；
  · 点 `scene:edit:<id>` → 弹多行输入（预填当前文本）→ 再确认回溯 → 落新叙述行；
    空/纯空白一律拒绝（给提示、什么都不动）；
  · 左栏「场景推进」卡：自动开关反映引擎 narration_state()["auto"]，切换即投递 worker；
    自动关掉后「推进一下」照样产出一条叙述。
若环境未装 PySide6（可选依赖 gui），整模块 skip。
"""
import asyncio
import json
import os
import re
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from harness.gui import main_window as mw_mod  # noqa: E402
from harness.gui.main_window import AppConfig, MainWindow  # noqa: E402
from harness.gui.theme import PALETTES  # noqa: E402
from harness.gui.worker import SceneWorker  # noqa: E402

#: 渲染取的是**当前主题色表**里的语义色（§3.6 视觉语言统一后不再硬编码十六进制）。
_FMT = PALETTES["默认"]

#: 叙述产出（stub narrate 档）：本模块断言渲染/撤销/改写都围绕这条。
_NARRATION = "隔壁桌有人站起来，椅子在瓷砖上刮出一声。"
#: 改写后的新叙述文本。
_EDITED = "服务员把账单压在杯底，又走开了。"


@pytest.fixture(scope="module")
def qapp():
    """模块级复用单一 QApplication（Qt 只允许每进程一个）。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _write_fixture(tmp_path: Path) -> tuple[Path, list[Path], Path]:
    """二人场（硬边界 22:00）+ think/speak/narrate 三档 stub 模型。"""
    scene = tmp_path / "餐厅.json"
    cards = [tmp_path / "甲.json", tmp_path / "乙.json"]
    models = tmp_path / "models.yaml"
    scene.write_text(json.dumps({
        "name": "餐厅", "participants": ["甲", "乙"],
        "circles": [{"id": "餐厅", "members": ["甲", "乙"]}],
        "hard_boundary": {"type": "time", "value": "22:00", "desc": "打烊"}},
        ensure_ascii=False), encoding="utf-8")
    for p, desc in zip(cards, ("冷静", "锐利")):
        p.write_text(json.dumps({"name": p.stem, "personality": {"描述": desc}},
                                ensure_ascii=False), encoding="utf-8")
    models.write_text(
        "think:\n  backend: stub\n  model: stub\n  params: {}\n"
        "speak:\n  backend: stub\n  model: stub\n  params: {}\n"
        "narrate:\n  backend: stub\n  model: stub\n  params: {}\n", encoding="utf-8")
    return scene, cards, models


def _spawn(tmp_path: Path, sub: str) -> tuple[SceneWorker, MainWindow, list, list]:
    """真实 MainWindow 装配的离线窗口 + 已连好的信号探针（场景未开）。"""
    scene, cards, models = _write_fixture(tmp_path)
    bid = tmp_path / f"{sub}-bid.yaml"
    bid.write_text("interruption_threshold: 0.0\nspeak_threshold: 0.0\n"
                   "silence_k: 8\n", encoding="utf-8")
    cfg = AppConfig(scene=scene, characters=cards, models=models, bid=bid,
                    run_root=tmp_path / sub, live=False, api_key=None,
                    closing_at_block=100000, opening="入夜。")
    worker = SceneWorker()
    win = MainWindow(worker, cfg)
    worker.start()
    msgs: list[dict] = []
    narrations: list[dict] = []
    worker.sig_message.connect(msgs.append)
    worker.sig_narration.connect(narrations.append)
    return worker, win, msgs, narrations


def _narrators(msgs: list[dict]) -> list[dict]:
    return [m for m in msgs if m.get("speaker_type") == "narrator"]


def _wait_until(qapp, cond, timeout_ms: int = 6000, step_ms: int = 15) -> bool:
    """轮询运行主线程 Qt 事件循环直到 cond() 为真；超时返回 False（不抛）。

    超时用**自己的**一次性计时器并要求返回前停掉：早先的 `QTimer.singleShot(超时, loop.quit)`
    会在队列里留一枚"以后来敲一下 loop.quit"的定时器，等它在**下一次** _wait_until 的循环里
    到点时，会把那次循环提前敲停——条件明明已经/将要满足，却返回 False（本模块实测过的
    间歇性假失败）。自己持有并 stop() 掉，队列里就不留这种跨次干扰。
    """
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
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)
    deadline.start(timeout_ms)
    loop.exec()
    timer.stop()
    deadline.stop()          # 条件已满足/已超时：这枚"闹钟"绝不能留到下一次等待里
    return ok["v"]


def _url(text: str):
    """锚点 URL（QTextBrowser.anchorClicked 传的就是 QUrl）。"""
    from PySide6.QtCore import QUrl
    return QUrl(text)


def _narrator_ids(msgs: list[dict]) -> list[int]:
    return [m["id"] for m in _narrators(msgs)]


def _conv_narrators(win: MainWindow) -> list[dict]:
    """对白区本地留存里的叙述行（界面渲染真源）。"""
    return [m for m in win._conv_msgs if m.get("speaker_type") == "narrator"]


# ------------------------------------------------------------ 渲染（纯函数级）
def _fmt(win: MainWindow, msg: dict) -> str:
    return win._format_message(msg)


def test_narrator_line_renders_centered_italic_with_links(qapp, tmp_path):
    """叙述行：居中斜体弱化块 + `撤销`/`改写` 行内链接（href 带 id）；角色行照旧。"""
    worker, win, msgs, narrations = _spawn(tmp_path, "fmt")
    try:
        narrator = {"id": 9, "speaker": "场景", "speaker_type": "narrator",
                    "content": _NARRATION, "time_hhmmss": "21:31:02"}
        html_n = _fmt(win, narrator)
        assert "text-align:center" in html_n, "叙述应居中"
        assert "font-style:italic" in html_n, "叙述应为斜体"
        assert _FMT.narrator_text in html_n, "叙述应用弱化色（当前主题的 narrator_text）"
        assert f'href="scene:undo:9"' in html_n and f'href="scene:edit:9"' in html_n, \
            "叙述行须带撤销/改写两个带 id 的行内链接"
        assert "场景" not in html_n.replace(_NARRATION, ""), \
            "叙述不得再带加粗的说话人名前缀"
        assert _NARRATION in html_n

        # 角色行：带名/色，绝无叙述样式与叙述链接。
        char = {"id": 10, "speaker": "甲", "speaker_type": "character",
                "content": "他胳膊在流血。", "time_hhmmss": "21:31:03"}
        html_c = _fmt(win, char)
        assert "text-align:center" not in html_c and "font-style:italic" not in html_c, \
            "角色行不得带叙述样式"
        assert "scene:undo" not in html_c and _FMT.narrator_text not in html_c
        assert html_c.count("font-weight:700") == 1, "角色行保持「加粗名 + 正文」渲染"

        # 导演行照旧（灰 + 斜体，但非居中、无链接）。
        direc = {"id": 11, "speaker": "导演", "speaker_type": "director",
                 "content": "（打烊。）"}
        html_d = _fmt(win, direc)
        assert "text-align:center" not in html_d and "scene:edit" not in html_d
    finally:
        worker.shutdown(4000)


def test_conversation_pane_html_carries_narrator_markup_and_anchors(qapp, tmp_path):
    """对白区 HTML（Qt 往返后）仍含居中斜体与两个 anchor href；角色行不受影响。"""
    worker, win, msgs, narrations = _spawn(tmp_path, "pane")
    try:
        win._on_message({"id": 3, "speaker": "甲", "speaker_type": "character",
                         "content": "他胳膊在流血。", "time_hhmmss": "21:31:00"})
        win._on_message({"id": 4, "speaker": "场景", "speaker_type": "narrator",
                         "content": _NARRATION, "time_hhmmss": "21:31:02"})
        win._on_message({"id": 5, "speaker": "乙", "speaker_type": "character",
                         "content": "先包一下。", "time_hhmmss": "21:31:03"})
        doc = win._view.toHtml()
        assert f'href="scene:undo:4"' in doc and f'href="scene:edit:4"' in doc, \
            "对白区 HTML 应含叙述行的撤销/改写锚点"
        assert "italic" in doc and _FMT.narrator_text in doc, \
            "对白区应含居中斜体弱化样式（弱化色取当前主题的 narrator_text）"
        assert doc.count('align="center"') == 1, "只有叙述段居中（角色行不得继承居中）"
        # 角色行照常渲染（不留叙述样式）：左对齐段落，两条各一次。
        assert win._view.toPlainText().count("他胳膊在流血。") == 1
        assert win._view.toPlainText().count("先包一下。") == 1
        assert "先包一下。" in doc
        # 叙述行本地留存（撤销/改写据此取原文与 id）。
        assert [m.get("id") for m in win._conv_msgs] == [3, 4, 5]
    finally:
        worker.shutdown(4000)


# ------------------------------------------------------------ 撤销
def test_undo_anchor_drops_line_and_marks_retracted(qapp, tmp_path, monkeypatch):
    """点 `scene:undo:<id>`：确认后叙述行立刻从对白区消失，引擎 retracted 收下该 id。

    §6.1 起撤销是**回溯式**且不可逆，故先弹确认框——离屏用例必须打桩它，否则真模态
    会把整个 pytest 卡死（确认文案与「取消则零动作」见 test_gui_narrate_activity.py）。
    """
    monkeypatch.setattr(mw_mod, "confirm", lambda *a, **k: True)
    worker, win, msgs, narrations = _spawn(tmp_path, "undo")
    try:
        win.start_session()
        worker.set_pace(0.02)
        assert _wait_until(qapp, lambda: worker.can_say(), 8000), "场景应已开场"
        assert _wait_until(
            qapp, lambda: bool(narrations) and narrations[0].get("auto") is True, 8000), \
            "开场应广播叙述状态（含 auto）"
        # 关掉自动推进：此后只认「推进一下」产出的那一条（确定性）。
        win._auto_scene_check.setChecked(False)
        assert _wait_until(
            qapp, lambda: narrations and narrations[-1].get("auto") is False, 8000), \
            "关掉自动后应回传 auto=False"

        win._narrate_btn.click()
        # 等新叙述落到界面上（本地留存 = 渲染真源）。
        assert _wait_until(
            qapp, lambda: any(m.get("speaker_type") == "narrator"
                              for m in win._conv_msgs), 8000), "叙述应已上屏"
        target = _conv_narrators(win)[-1]
        mid = int(target["id"])
        assert target["content"] in win._view.toPlainText()
        assert 'align="center"' in win._view.toHtml(), "上屏的叙述应为居中段"
        assert mid in [m["id"] for m in _narrators(msgs)], "叙述应经 sig_message 到达"

        # 走真实锚点入口：对白区的 anchorClicked 信号已接到处理函数（连接本身也被验证）。
        win._view.anchorClicked.emit(_url(f"scene:undo:{mid}"))
        assert not _wait_until(
            qapp, lambda: any(m.get("id") == mid for m in win._conv_msgs), 300), \
            "撤销后该行应立刻从对白区消失（本地先摘 + 重绘）"
        # 该条已从渲染真源消失：对白区不再有任何叙述段（居中块）。
        assert _conv_narrators(win) == []
        assert 'align="center"' not in win._view.toHtml(), "撤销后不得再留有叙述段"

        # 引擎共享态收下该 id（worker 投递到 loop，有界轮询等它落地）。
        assert _wait_until(
            qapp,
            lambda: mid in (narrations[-1].get("retracted") or []) if narrations else False,
            8000), "sig_narration 应回传 retracted 含该 id"
        eng = worker._engine
        assert eng is not None and mid in eng.narration_state()["retracted"], \
            "引擎共享态 retracted 应含被撤销的 id"
    finally:
        worker.shutdown(4000)
        assert not worker.isRunning()


def test_retract_signal_drops_already_shown_line(qapp, tmp_path):
    """worker.retract_message 对**已上屏**的叙述同样生效：sig_retracted 触达 → 重绘消失。"""
    worker, win, msgs, narrations = _spawn(tmp_path, "sig")
    try:
        win.start_session()
        worker.set_pace(0.02)
        assert _wait_until(qapp, lambda: worker.can_say(), 8000), "场景应已开场"
        win._auto_scene_check.setChecked(False)
        assert _wait_until(
            qapp, lambda: bool(narrations) and narrations[-1].get("auto") is False, 8000), \
            "关掉自动后应回传 auto=False"
        win._narrate_btn.click()
        assert _wait_until(
            qapp, lambda: any(m.get("speaker_type") == "narrator"
                              for m in win._conv_msgs), 8000), "叙述应已上屏"
        mid = int(_conv_narrators(win)[-1]["id"])
        assert 'align="center"' in win._view.toHtml()

        worker.retract_message(mid)          # 直接投 worker（不经界面锚点）
        assert _wait_until(
            qapp, lambda: not any(m.get("id") == mid for m in win._conv_msgs), 8000), \
            "sig_retracted 应让已上屏的叙述行消失"
        assert _conv_narrators(win) == []
        assert 'align="center"' not in win._view.toHtml()
    finally:
        worker.shutdown(4000)


# ------------------------------------------------------------ 改写
class _FakeInput:
    """打桩的多行输入框：记下预填文本，返回脚本化结果（None=取消）。"""
    result: str | None = _EDITED
    seen: list[tuple[str, str]] = []

    @classmethod
    def getMultiLineText(cls, parent, title, label, text="", **kw):
        cls.seen.append((title, text))
        return (cls.result, True) if cls.result is not None else ("", False)


def test_edit_anchor_replaces_text_and_rejects_blank(qapp, tmp_path, monkeypatch):
    """改写锚点：预填原文 → 返回非空 → 旧文消失、新文上屏；返回空/空白 → 原样不动。"""
    worker, win, msgs, narrations = _spawn(tmp_path, "edit")
    monkeypatch.setattr(mw_mod, "QInputDialog", _FakeInput)
    # 改写现在也要确认（回溯会丢弃其后的内容，§6.1）：打桩掉，别弹真模态。
    monkeypatch.setattr(mw_mod, "confirm", lambda *a, **k: True)
    try:
        win.start_session()
        worker.set_pace(0.02)
        assert _wait_until(qapp, lambda: worker.can_say(), 8000), "场景应已开场"
        win._auto_scene_check.setChecked(False)
        assert _wait_until(
            qapp, lambda: bool(narrations) and narrations[-1].get("auto") is False, 8000), \
            "关掉自动后应回传 auto=False"
        win._narrate_btn.click()
        assert _wait_until(
            qapp, lambda: any(m.get("speaker_type") == "narrator"
                              for m in win._conv_msgs), 8000), "叙述应已上屏"
        old = _conv_narrators(win)[-1]
        mid = int(old["id"])
        assert old["content"] in win._view.toPlainText()

        # (a) 空/纯空白：拒绝——什么都没变（旧行仍在、无新 id 落地、提示可见）。
        _FakeInput.seen = []
        for blank in ("", "   \n\t "):
            _FakeInput.result = blank
            ids_before = _narrator_ids(msgs)
            win._on_anchor_clicked(_url(f"scene:edit:{mid}"))
            assert not _wait_until(
                qapp, lambda: _narrator_ids(msgs) != ids_before, 400), \
                "空白改写不得产生新叙述"
            assert [m["id"] for m in _conv_narrators(win)] == [mid], \
                "空白改写后旧行应原样保留"
            assert old["content"] in win._view.toPlainText()
        assert _FakeInput.seen, "应弹过输入框"
        assert _FakeInput.seen[0][1] == old["content"], "输入框应预填当前叙述原文"
        assert "改写内容为空，已忽略" in win._status_chip.text(), "空白改写应给提示"

        # (b) 非空：旧行作废、新文上屏（新 id 的叙述行，唯一的叙述段）。
        _FakeInput.result = _EDITED
        win._on_anchor_clicked(_url(f"scene:edit:{mid}"))
        assert _wait_until(
            qapp,
            lambda: (not any(m.get("id") == mid for m in win._conv_msgs))
            and _EDITED in win._view.toPlainText(), 8000), \
            "改写后应以新文替换旧文（旧 id 行消失）"
        live = _conv_narrators(win)
        assert len(live) == 1 and live[0]["id"] != mid, "旧行应被新叙述行取代"
        assert live[0]["content"] == _EDITED
        doc = win._view.toHtml()
        assert doc.count('align="center"') == 1, "旧叙述段已消失，只剩新的那一段"
        assert _EDITED in win._view.toPlainText()
    finally:
        _FakeInput.result = _EDITED
        _FakeInput.seen = []
        worker.shutdown(4000)


# ------------------------------------------------------------ 左栏控制
def test_auto_checkbox_reflects_engine_and_toggles_worker(qapp, tmp_path):
    """自动开关：初值反映引擎 narration_state()["auto"]；切换即投递到引擎 loop。"""
    worker, win, msgs, narrations = _spawn(tmp_path, "auto")
    try:
        assert win._auto_scene_check.isChecked(), "默认应为自动推进（引擎缺省 auto=True）"
        win.start_session()
        worker.set_pace(0.02)
        assert _wait_until(qapp, lambda: worker.can_say(), 8000), "场景应已开场"

        # 关 → worker 落地（引擎 narration_state()["auto"] 变 False）。
        win._auto_scene_check.setChecked(False)
        assert _wait_until(
            qapp, lambda: worker._engine is not None
            and worker._engine.narration_state()["auto"] is False, 8000), \
            "关掉开关应投到引擎"
        # 再开 → 同样落地。
        win._auto_scene_check.setChecked(True)
        assert _wait_until(
            qapp, lambda: worker._engine is not None
            and worker._engine.narration_state()["auto"] is True, 8000), \
            "打开开关应投到引擎"

        # 场景信息携带 auto 初值（开场/切场据此对齐复选框，不被 "to_ggled" 反噬）。
        assert _wait_until(
            qapp, lambda: bool(narrations) and "auto" in narrations[0], 8000), \
            "sig_narration 应携带 auto"
    finally:
        worker.shutdown(4000)


def test_manual_push_works_when_auto_off(qapp, tmp_path):
    """自动关掉后「推进一下」照样产出一条叙述，且状态行给出触发理由/块数。"""
    worker, win, msgs, narrations = _spawn(tmp_path, "push")
    try:
        win.start_session()
        worker.set_pace(0.02)
        assert _wait_until(qapp, lambda: worker.can_say(), 8000), "场景应已开场"
        win._auto_scene_check.setChecked(False)
        assert _wait_until(
            qapp, lambda: worker._engine is not None
            and worker._engine.narration_state()["auto"] is False, 8000)
        # 自动已关：等若干块也不该有叙述（触发判定恒被开关挡下）。
        assert not _wait_until(qapp, lambda: bool(_narrators(msgs)), 1200), \
            "自动已关时不得自行叙述"

        win._narrate_btn.click()
        assert _wait_until(qapp, lambda: len(_narrators(msgs)) >= 1, 8000), \
            "自动关掉后「推进一下」应照样产出叙述"
        assert _wait_until(
            qapp, lambda: "块" in win._narration_status.text(), 8000), \
            "状态行应显示距上次推进的块数/理由"
    finally:
        worker.shutdown(4000)
