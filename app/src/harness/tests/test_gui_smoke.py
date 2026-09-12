"""GUI 离屏冒烟：窗口可建可跑、worker(stub) 短跑自动产出角色台词并上报指标。

全部离线、确定性、快速（<5s）：QT_QPA_PLATFORM=offscreen + stub 后端，绝不联网。
覆盖两点——(1) MainWindow 三栏窗口在 Qt 事件循环里正常 exec；(2) SceneWorker 的
autoplay 经 stub 引擎把消息/指标/收束信号如实送达主线程。若环境未装 PySide6
（可选依赖 gui），整模块 skip，不影响既有 57 测。
"""
import asyncio
import json
import os
import re
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from harness.dynamics import DynamicsParams  # noqa: E402
from harness.gui.main_window import (  # noqa: E402
    PALETTE, AppConfig, MainWindow, assign_name_colors,
)
from harness.gui.worker import SceneWorker, closing_text  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    """模块级复用单一 QApplication（Qt 只允许每进程一个）。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _write_fixture(tmp_path: Path):
    scene = tmp_path / "贝克街221B.json"
    a = tmp_path / "甲.json"
    b = tmp_path / "乙.json"
    models = tmp_path / "models.yaml"
    scene.write_text(json.dumps({
        "name": "贝克街221B", "participants": ["甲", "乙"],
        "circles": [{"id": "贝克街221B", "members": ["甲", "乙"]}],
        "hard_boundary": {"type": "time", "value": "22:00", "desc": "打烊"}},
        ensure_ascii=False), encoding="utf-8")
    a.write_text(json.dumps({"name": "甲", "personality": {"描述": "冷静"}},
                            ensure_ascii=False), encoding="utf-8")
    b.write_text(json.dumps({"name": "乙", "personality": {"描述": "锐利"}},
                            ensure_ascii=False), encoding="utf-8")
    models.write_text(
        "think:\n  backend: stub\n  model: stub\n  params: {}\n"
        "speak:\n  backend: stub\n  model: stub\n  params: {}\n", encoding="utf-8")
    return scene, a, b, models


def _make_stub_bid(path: Path) -> Path:
    path.write_text(
        "interruption_threshold: 0.0\nspeak_threshold: 0.0\nsilence_k: 8\n",
        encoding="utf-8")
    return path


def _spawn_window(tmp_path: Path, sub: str, closing_at_block: int,
                  opening: str = "") -> tuple[SceneWorker, MainWindow, list, list]:
    """建一个走真实 MainWindow 装配的离线窗口（worker 线程已启动，场景未开）。"""
    scene, a, b, models = _write_fixture(tmp_path)
    bid = _make_stub_bid(tmp_path / f"{sub}-bid.yaml")
    cfg = AppConfig(scene=scene, characters=[a, b], models=models, bid=bid,
                    run_root=tmp_path / sub, live=False, api_key=None,
                    closing_at_block=closing_at_block, opening=opening)
    worker = SceneWorker()
    win = MainWindow(worker, cfg)
    worker.start()
    msgs: list[dict] = []
    statuses: list[str] = []
    worker.sig_message.connect(msgs.append)
    worker.sig_status.connect(statuses.append)
    return worker, win, msgs, statuses


def _char_msgs(msgs: list[dict]) -> list[dict]:
    return [m for m in msgs
            if m.get("speaker_type") == "character"
            and (m.get("content") or "").strip()]


def _left_text(win: MainWindow) -> str:
    """左栏滚动区内全部标签文本（换行连接；含被隐藏的行，故能断言「整行不存在」）。"""
    body = win._left_scroll.widget()
    return "\n".join(l.text() for l in body.findChildren(QLabel) if l.text())


def test_mainwindow_builds_and_runs_offscreen(qapp):
    """三栏主窗口（stub 配置）能在 offscreen Qt 事件循环里正常构造并 exec 返回 0。"""
    cfg = AppConfig(
        scene=Path("scenes/贝克街221B.json"),
        characters=[Path("characters/福尔摩斯.json"), Path("characters/华生.json")],
        models=Path("config/models.yaml"), bid=Path("config/bid.yaml"),
        run_root=Path("runs"), live=False, api_key=None, closing_at_block=8)
    worker = SceneWorker()                 # 只构造不 start：本测只验窗口
    win = MainWindow(worker, cfg)
    win.show()
    QTimer.singleShot(700, qapp.quit)
    assert qapp.exec() == 0                # 无异常、正常退出


def test_worker_stub_autoplay_emits_messages_and_metrics(qapp, tmp_path):
    """worker(stub) 短跑：autoplay 自动逐块推进到自然收束，角色台词与指标信号到位。"""
    scene_p, a_p, b_p, models_p = _write_fixture(tmp_path)
    worker = SceneWorker()
    worker.start()
    msgs: list[dict] = []
    metrics: list[dict] = []
    state = {"finished": False, "timeout": False}

    worker.sig_message.connect(msgs.append)
    worker.sig_metrics.connect(metrics.append)

    loop = QEventLoop()
    QTimer.singleShot(6000, lambda: (state.__setitem__("timeout", True), loop.quit()))

    def _finished():
        state["finished"] = True
        loop.quit()

    worker.sig_finished.connect(_finished)

    # stub 短跑：4 块收束、极快节拍，确保 <5s。
    worker.start_scene(scene_p, [a_p, b_p], models_p, live=False, bid=None,
                       opening="夜深了。", run_root=tmp_path / "runroot",
                       api_key=None, closing_at_block=4)
    worker.set_pace(0.02)
    loop.exec()

    worker.shutdown(4000)
    assert not state["timeout"], "SceneWorker autoplay 6 秒未自然收束"
    assert state["finished"], "场景应收束并派发 sig_finished"

    chars = [m for m in msgs
             if m.get("speaker_type") == "character"
             and (m.get("content") or "").strip()]
    assert chars, "应至少有一条角色台词经 sig_message 到达"
    assert {"甲", "乙"} & {m["speaker"] for m in chars}, "双方应各开口"

    assert metrics, "应至少收到一次 sig_metrics"
    last = metrics[-1]
    assert last.get("messages", 0) > 0
    assert last.get("uptime_s") is not None
    assert last.get("think", {}).get("stub", {}).get("calls", 0) > 0
    assert last.get("speak", {}).get("stub", {}).get("calls", 0) > 0


def _wait_until(qapp, cond, timeout_ms: int = 4000, step_ms: int = 15) -> bool:
    """轮询运行主线程 Qt 事件循环直到 cond() 为真；超时返回 False（不抛）。"""
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


def test_worker_say_pause_restart_roundtrip(qapp, tmp_path):
    """交互回路（stub、短跑）：插话→暂停→继续→重开 各阶段信号如实到达主线程。"""
    scene_p, a_p, b_p, models_p = _write_fixture(tmp_path)
    worker = SceneWorker()
    worker.start()
    msgs: list[dict] = []
    statuses: list[str] = []
    infos: list[dict] = []
    worker.sig_message.connect(msgs.append)
    worker.sig_status.connect(statuses.append)
    worker.sig_scene_info.connect(infos.append)

    char_msgs = lambda: [m for m in msgs  # noqa: E731
                         if m.get("speaker_type") == "character"
                         and (m.get("content") or "").strip()]
    worker.start_scene(scene_p, [a_p, b_p], models_p, live=False, bid=None,
                       opening="继续聊。", run_root=tmp_path / "runroot2",
                       api_key=None, closing_at_block=200)
    worker.set_pace(0.03)

    try:
        assert _wait_until(qapp, lambda: len(char_msgs()) >= 1), "角色应开始自主开口"
        before = len(char_msgs())

        # 人类随时插话：say → 引擎把 HUMAN 行写进共享态（角色下一块感知并回应；
        # 语义由引擎侧测试覆盖）；worker 派发扫描跳过 human（GUI 已即时上屏），
        # 故 sig_message 不二次带出 human。先短泵一段确认始终无 human 行派发。
        worker.say("（窗外有人喊我。）")
        assert _wait_until(qapp, lambda: len(char_msgs()) > before), "插话后角色应回应"
        mark = time.monotonic()
        _wait_until(qapp, lambda: time.monotonic() - mark >= 0.3, 600)
        assert not any(m.get("speaker_type") == "human" for m in msgs), \
            "worker 派发扫描应跳过 human 行（防与 GUI 即时气泡重复）"

        # 暂停 → 状态反映；继续 → 恢复开口（进行中须出现在已暂停之后）。
        worker.set_paused(True)
        assert _wait_until(qapp, lambda: "已暂停" in statuses), "暂停应上报已暂停"
        pause_idx = statuses.index("已暂停")
        worker.set_paused(False)
        assert _wait_until(
            qapp,
            lambda: any(s == "进行中" and i > pause_idx
                        for i, s in enumerate(statuses)),
        ), "继续后应重新回到进行中"

        # 重开：换新 run_root 重建引擎 → 第二份场景信息 + 角色继续开口。
        worker.restart("另一场开场。")
        assert _wait_until(qapp, lambda: len(infos) >= 2), "重开应再派发一次场景信息"
        after_restart = len(char_msgs())
        assert _wait_until(qapp, lambda: len(char_msgs()) > after_restart), \
            "重开后角色应继续自主开口"
    finally:
        worker.shutdown(4000)
        assert not worker.isRunning(), "worker 线程应收尾退出"


def test_gui_send_while_paused_resumes_and_characters_answer(qapp, tmp_path):
    """评审 #3/#4 回归（真实窗口 + 真实按钮槽）：暂停中发送 → 输入清空、按钮回「暂停」、
    人类消息落地、状态回到进行中、角色随后回应。"""
    worker, win, msgs, statuses = _spawn_window(tmp_path, "pausesend", closing_at_block=200,
                                                opening="夜色里。")
    chars = lambda: _char_msgs(msgs)  # noqa: E731
    try:
        win.start_session()
        worker.set_pace(0.03)
        assert _wait_until(qapp, lambda: len(chars()) >= 1), "角色应开始自主开口"

        # 暂停（走真实按钮槽）
        win._on_toggle_pause()
        assert win._paused is True and win._pause_btn.text() == "继续"
        assert _wait_until(qapp, lambda: "已暂停" in statuses)
        pause_idx = statuses.index("已暂停")

        # 暂停中发送
        before = len(chars())
        win._input_edit.setText("喂，暂停中还听得见吗？")
        win._on_send()
        assert win._input_edit.text() == "", "成功投递后应清空输入"
        assert win._paused is False, "暂停中发送应解除暂停（窗口镜像同步）"
        assert win._pause_btn.text() == "暂停", "按钮文本应回『暂停』"

        # 人类消息落地 → 状态回到进行中 → 角色回应
        human = "喂，暂停中还听得见吗？"
        assert _wait_until(qapp, lambda: human in win._view.toPlainText())
        assert _wait_until(
            qapp, lambda: any(s == "进行中" and i > pause_idx
                              for i, s in enumerate(statuses))), "应回到进行中"
        assert _wait_until(qapp, lambda: len(chars()) > before), "插话后角色应回应"
    finally:
        worker.shutdown(4000)
        assert not worker.isRunning(), "worker 线程应收尾退出"


def test_gui_send_bubble_immediate_before_replies_not_duplicated(qapp, tmp_path):
    """即时上屏 + 去重：发送瞬间「你」气泡即入会话（此刻角色零新增句、气泡为会话尾部、
    带当前 HH:MM:SS），随后角色才回应且新句都在气泡之后；worker 派发扫描跳过 human 行
    → 同句全文只显示一次，不随角色下一句二次出现。确定性：暂停冻结 autoplay 后发送。"""
    worker, win, msgs, statuses = _spawn_window(tmp_path, "immsend",
                                                closing_at_block=100000,
                                                opening="夜色里。")
    text = "门外有人递来一封匿名信。"
    try:
        win.start_session()
        worker.set_pace(0.03)
        # 场景就绪且左栏当前时刻已到 HH:MM:SS（气泡要带该时刻作前缀）。
        assert _wait_until(
            qapp,
            lambda: worker.can_say()
            and bool(re.fullmatch(r"\d{2}:\d{2}:\d{2}",
                                  win._clock_time["now"].text())), 6000), \
            "场景应就绪且当前时刻到 HH:MM:SS"

        # 暂停冻结 autoplay：让「气泡先于角色回应句」这一时序可被确定性断言。
        win._on_toggle_pause()
        assert win._paused is True and win._pause_btn.text() == "继续"
        assert _wait_until(qapp, lambda: "已暂停" in statuses, 3000)
        frozen = len(_char_msgs(msgs))

        win._input_edit.setText(text)
        win._on_send()                                  # 真实发送槽

        # (1) 发送返回瞬间气泡已上屏、输入已清空、角色句零新增、气泡为会话尾部。
        doc = win._view.toPlainText()
        assert text in doc, "『你』气泡应即时上屏"
        assert doc.endswith(text), "气泡此刻应是会话最后一条"
        assert re.search(r"\d{2}:\d{2}:\d{2}", doc), "气泡应带当前 HH:MM:SS 前缀"
        assert win._input_edit.text() == "", "成功发送后输入应清空"
        assert len(_char_msgs(msgs)) == frozen, "气泡应先于任何角色回应句"
        bubble_idx = doc.find(text)

        # (2) say 解除暂停 → 角色感知 human 行并回应 → 新句只追加在气泡之后。
        assert _wait_until(qapp, lambda: len(_char_msgs(msgs)) > frozen, 6000), \
            "插话后角色应回应"
        doc2 = win._view.toPlainText()
        assert doc2.count(text) == 1, \
            "human 行只显示一次（worker 扫描跳过 human，防二次上屏）"
        assert doc2.find(text) == bubble_idx, "后续角色行不得挤到气泡之前"
        assert not any(m.get("speaker_type") == "human" for m in msgs), \
            "worker 派发扫描不应 emit human 行"
    finally:
        worker.shutdown(4000)
        assert not worker.isRunning(), "worker 线程应收尾退出"


def test_gui_reopen_after_natural_finish_restarts_scene(qapp, tmp_path):
    """评审 #1 回归（真实窗口 _on_reopen 槽）：自然收束后输入只停不锁会话——
    点「开新场」立即重建引擎、重新可用输入并继续出现对话。"""
    worker, win, msgs, statuses = _spawn_window(tmp_path, "reopen", closing_at_block=4)
    chars = lambda: _char_msgs(msgs)  # noqa: E731
    try:
        win.start_session()
        worker.set_pace(0.03)
        assert _wait_until(qapp, lambda: win._finished, 6000), "应自然收束"

        # 收束态：输入停用、发话判据停用，但会话标记保留（不被 _on_finished 归零）。
        assert not win._input_edit.isEnabled()
        assert win._started is True

        # 评审 #4：收束瞬间发送 → 不清空输入，状态区给提示。
        win._input_edit.setText("晚到的话还听吗？")
        win._on_send()
        assert win._input_edit.text() == "晚到的话还听吗？", "收束后不应清空输入"
        assert "开新场" in win._status_chip.text()

        # 重开（真实槽）→ 引擎重建、输入恢复、新对白继续。
        before = len(chars())
        win._on_reopen()
        assert win._input_edit.isEnabled(), "重开应重新启用输入"
        assert win._finished is False
        assert _wait_until(qapp, lambda: len(chars()) > before, 6000), \
            "重开后应继续自主开口"
        assert _wait_until(qapp, lambda: worker.can_say()), "重开后应可发话"
        assert "夜晚的贝克街221B，二人临窗而坐。" in win._view.toPlainText(), \
            "重开后的开场应重新进入对白"
    finally:
        worker.shutdown(4000)
        assert not worker.isRunning(), "worker 线程应收尾退出"


def test_worker_shutdown_right_after_start_still_stops_the_thread(qapp):
    """收尾竞态回归：`shutdown()` 紧接 `start()` 也必须把线程停住。

    为什么钉这条：`start()` 返回后到 `run()` 里把 `_loop` 建好、`_ready` 置位之间有
    一段极短的窗口。旧写法在窗口里读到 `self._loop is None`，就把"在 loop 上投
    teardown"整段跳过，只剩一次 `wait(timeout)`——而 `run_forever` 没有别的出口，
    等不到就永远不出来：于是这条线程（以及它的事件循环/引擎/sqlite 连接）在进程里
    永久留着。实测这类"永不退出"的 worker 线程会一直接着跑 asyncio 轮询，是整场
    测试进程被掀翻的一条来路（也让 `assert not worker.isRunning()` 变成随机红绿）。

    连开三个、每个都"一起表就收"，把竞态窗口撞准；修好后三个都必须停住。
    """
    for _ in range(3):
        worker = SceneWorker()
        worker.start()
        worker.shutdown(4000)
        assert not worker.isRunning(), "start 后紧接着的 shutdown 也必须停掉线程"


def test_worker_open_failure_emits_error_and_can_retry(qapp, tmp_path):
    """评审 #2 回归：开场失败不静默——emit "⚠" 错误状态、can_say 停用；
    补上素材后 restart 可恢复并继续自主开口。"""
    scene, a, b, models = _write_fixture(tmp_path)
    scene.unlink()                       # 让开场失败（场景文件缺失）
    worker = SceneWorker()
    worker.start()
    msgs: list[dict] = []
    statuses: list[str] = []
    worker.sig_message.connect(msgs.append)
    worker.sig_status.connect(statuses.append)
    chars = lambda: _char_msgs(msgs)  # noqa: E731
    try:
        worker.start_scene(scene, [a, b], models, live=False, bid=None,
                           opening="场。", run_root=tmp_path / "rrf",
                           api_key=None, closing_at_block=20)
        worker.set_pace(0.03)
        assert _wait_until(qapp, lambda: any(s.startswith("⚠ ") for s in statuses)), \
            "开场失败应 emit 错误状态而非静默"
        assert not worker.can_say(), "失败后不应可发话"

        # 补上场景素材 → 重开可恢复。
        scene.write_text(json.dumps({
            "name": "贝克街221B", "participants": ["甲", "乙"],
            "circles": [{"id": "贝克街221B", "members": ["甲", "乙"]}],
            "hard_boundary": {"type": "time", "value": "22:00", "desc": "打烊"}},
            ensure_ascii=False), encoding="utf-8")
        worker.restart("再试一场。")
        assert _wait_until(qapp, lambda: len(chars()) >= 1, 6000), "重开应恢复对话"
        assert _wait_until(qapp, lambda: worker.can_say()), "恢复后应可发话"
    finally:
        worker.shutdown(4000)
        assert not worker.isRunning(), "worker 线程应收尾退出"


def test_gui_stop_now_closes_scene_with_red_status(qapp, tmp_path):
    """虚拟钟停止语义：停止按钮 → worker 调 engine.close_scene() → 场景立即收束、
    sig_finished、状态「已停止」(区别于自然收束的「已收束」)、停止钮自禁、发话停用。
    用无 time 边界的场景 + 大块钟上限，确保不会在点停止前自然收束。"""
    scene, a, b, models = _write_fixture(tmp_path)
    # 无 time 硬边界 → 唯一收束路径是大块钟兜底(200)，点停止前不会自然结束。
    scene.write_text(json.dumps({
        "name": "贝克街221B", "participants": ["甲", "乙"],
        "circles": [{"id": "贝克街221B", "members": ["甲", "乙"]}]},
        ensure_ascii=False), encoding="utf-8")
    bid = _make_stub_bid(tmp_path / "stop-bid.yaml")
    cfg = AppConfig(scene=scene, characters=[a, b], models=models, bid=bid,
                    run_root=tmp_path / "stoprun", live=False, api_key=None,
                    closing_at_block=200, opening="还在营业。")
    worker = SceneWorker()
    win = MainWindow(worker, cfg)
    worker.start()
    msgs: list[dict] = []
    statuses: list[str] = []
    worker.sig_message.connect(msgs.append)
    worker.sig_status.connect(statuses.append)
    chars = lambda: _char_msgs(msgs)  # noqa: E731
    try:
        win.start_session()
        worker.set_pace(0.02)
        assert _wait_until(qapp, lambda: worker.can_say() and len(chars()) >= 1, 6000), \
            "场景应已开场并自主开口"
        assert win._stop_btn.isEnabled()

        win._stop_btn.click()                     # 停止（真实按钮槽）
        assert _wait_until(qapp, lambda: win._finished, 6000), "停止后应收束"
        assert not worker.can_say(), "停止后不可再发话"
        assert not win._stop_btn.isEnabled(), "停止按钮应在收束后自禁"
        assert statuses and statuses[-1] == "已停止", "末态应标 已停止"
        assert win._status_chip.text() == "已停止"

        # 停止后不再有新的角色台词（在飞行中的一块已由 stop_now 收尾）
        tail = list(chars())
        assert not _wait_until(qapp, lambda: len(chars()) > len(tail), 800), \
            "停止后不应再有角色开口"

        # 开新场在停止后依旧可用。
        win._on_reopen()
        assert _wait_until(qapp, lambda: worker.can_say(), 6000), "停止后开新场应恢复"
    finally:
        worker.shutdown(4000)
        assert not worker.isRunning(), "worker 线程应收尾退出"


def test_gui_scene_time_clock_and_pause_resume(qapp, tmp_path):
    """场景卡时间行：左栏显示 开始 21:30 / 边界 22:00（中性措辞，无「打烊」字样）；
    随指标刷新「当前」；对白每条带 HH:MM 时刻戳；暂停→继续 恢复自主开口。"""
    worker, win, msgs, statuses = _spawn_window(tmp_path, "clock", closing_at_block=200,
                                                opening="入夜。")
    chars = lambda: _char_msgs(msgs)  # noqa: E731
    try:
        win.start_session()
        worker.set_pace(0.05)      # 30 分钟余量拉长到 ~1.5s，先于自然收束前完成暂停/继续
        # 场景信息落地 → 开始/边界时刻显示（场景硬边界 time 22:00，默认开始 21:30）
        assert _wait_until(qapp, lambda: win._clock_time["start"].text() == "21:30",
                           6000), "左栏应显示开始 21:30"
        assert win._boundary_value.text() == "22:00", "「边界」行应显示 22:00"
        assert "打烊" not in _left_text(win), "左栏不得再出现贝克街221B绑定措辞「打烊」"

        # 对白消息带 HH:MM 时刻戳；左栏当前时刻随指标推进离开开始值
        assert _wait_until(qapp, lambda: len(chars()) >= 1, 6000), "角色应开口"
        assert _wait_until(
            qapp,
            lambda: bool(re.search(r"\d{2}:\d{2}", win._view.toPlainText())),
        ), "每条台词应带 HH:MM 时刻戳"
        assert _wait_until(
            qapp,
            lambda: (win._clock_time["now"].text() or "").strip()
            not in ("", "…", "21:30"),
        ), "当前时刻应随指标刷新离开 21:30"

        # 暂停 → 状态反映；继续 → 恢复开口。
        win._on_toggle_pause()
        assert win._paused is True and win._pause_btn.text() == "继续"
        assert _wait_until(qapp, lambda: "已暂停" in statuses)
        pause_idx = statuses.index("已暂停")
        before = len(chars())
        win._on_toggle_pause()                    # 继续
        assert win._paused is False and win._pause_btn.text() == "暂停"
        assert _wait_until(
            qapp,
            lambda: any(s == "进行中" and i > pause_idx
                        for i, s in enumerate(statuses)), 6000), "继续后回到进行中"
        assert _wait_until(qapp, lambda: len(chars()) > before, 6000), "继续后应恢复开口"
    finally:
        worker.shutdown(4000)
        assert not worker.isRunning(), "worker 线程应收尾退出"


# ============================================================ 真实流速虚拟钟（worker）
def _scene_near_boundary(tmp_path: Path, sub: str, start: str,
                         boundary: str = "22:00", desc: str = "打烊",
                         name: str = "贝克街221B"):
    """写一个 start 距 boundary 很近的场景（如 21:59→22:00），供加速自然收束用。"""
    scene, a, b, models = _write_fixture(tmp_path)
    scene.write_text(json.dumps({
        "name": name, "participants": ["甲", "乙"],
        "circles": [{"id": "贝克街221B", "members": ["甲", "乙"]}],
        "hard_boundary": {"type": "time", "value": boundary, "desc": desc},
        "start_time": start}, ensure_ascii=False), encoding="utf-8")
    return scene, a, b, models


def _connect_probes(worker):
    msgs: list[dict] = []
    metrics: list[dict] = []
    statuses: list[str] = []
    finished: list = []
    worker.sig_message.connect(msgs.append)
    worker.sig_metrics.connect(metrics.append)
    worker.sig_status.connect(statuses.append)
    worker.sig_finished.connect(lambda: finished.append(True))
    return msgs, metrics, statuses, finished


def _clock_running_probe(worker):
    """返回一个读 worker loop 上虚拟钟 is_running 的同步探针（跨线程，供测试断言）。"""
    async def _running() -> bool | None:
        clock = worker._vclock
        return clock.is_running() if clock is not None else None

    def _probe() -> bool | None:
        fut = asyncio.run_coroutine_threadsafe(_running(), worker._loop)
        return fut.result(timeout=2)

    return _probe


def test_worker_clock_advances_approx_elapsed_times_rate(qapp, tmp_path):
    """真实流速：rate=60 下 0.4s 真实流逝 → 虚拟钟应动 ≥ 8s（约 24s），量级与
    流逝×rate 相符（不是按句/按静默走，也不是 1:1 实钟）。"""
    scene_p, a_p, b_p, models_p = _write_fixture(tmp_path)
    worker = SceneWorker()
    worker.start()
    msgs, metrics, statuses, _ = _connect_probes(worker)
    try:
        worker.start_scene(scene_p, [a_p, b_p], models_p, live=False, bid=None,
                           opening="入夜。", run_root=tmp_path / "rr_adv",
                           api_key=None, closing_at_block=100000)
        worker.set_pace(0.03)
        worker.set_rate(60.0)
        assert _wait_until(
            qapp,
            lambda: metrics and metrics[-1].get("clock_seconds", 0) > 0, 4000), \
            "应有时间指标"
        v0 = metrics[-1]["clock_seconds"]
        t0 = time.monotonic()
        assert _wait_until(qapp, lambda: time.monotonic() - t0 >= 0.4, 2500)
        v1 = metrics[-1]["clock_seconds"]
        dt = time.monotonic() - t0
        assert v1 - v0 >= 8, f"0.4s×60 应动 ≥ 约 24 虚拟秒，实际 {v1 - v0}"
        assert (v1 - v0) <= dt * 60 * 2.0 + 30, "涨幅与流逝×rate 量级相符"
    finally:
        worker.shutdown(4000)
        assert not worker.isRunning()


def test_worker_pause_freezes_and_resume_continues(qapp, tmp_path):
    """暂停冻结钟（暂停期间不涨）、继续从冻结值接着走；调速不丢已走时间。"""
    scene_p, a_p, b_p, models_p = _write_fixture(tmp_path)
    worker = SceneWorker()
    worker.start()
    msgs, metrics, statuses, _ = _connect_probes(worker)
    try:
        worker.start_scene(scene_p, [a_p, b_p], models_p, live=False, bid=None,
                           opening="入夜。", run_root=tmp_path / "rr_pause",
                           api_key=None, closing_at_block=100000)
        worker.set_pace(0.03)
        worker.set_rate(30.0)
        # 让钟充分走开开场（30× 下 ~0.5s 即 +15s），暂停/继续的判定窗口足够宽。
        assert _wait_until(
            qapp,
            lambda: metrics and metrics[-1].get("clock_seconds", 0) > 77400 + 10, 6000), \
            "钟应已从 21:30 走起"

        # 暂停 → 冻结（_apply_paused 即刻冻结并广播一次冻结指标，之后 ticker 重复广播）。
        worker.set_paused(True)
        assert _wait_until(qapp, lambda: "已暂停" in statuses, 3000)
        # 让冻结稳定下来：此刻拿到的末条指标已是冻结值。
        mark = time.monotonic()
        assert _wait_until(qapp, lambda: time.monotonic() - mark >= 0.15, 2000)
        frozen = metrics[-1]["clock_seconds"]

        # 用时间窗直接验证暂停期间不涨。
        mark = time.monotonic()
        assert _wait_until(qapp, lambda: time.monotonic() - mark >= 0.6, 2000), \
            "pump 0.6s（暂停）"
        assert metrics[-1]["clock_seconds"] <= frozen + 1, "暂停期间钟不动"

        # 继续 → 从冻结值接着走（不回到开场、不从零重来）。
        worker.set_paused(False)
        assert _wait_until(qapp, lambda: "进行中" in statuses, 3000)
        first_after = metrics[-1]["clock_seconds"]
        assert first_after >= frozen - 1, "继续应从冻结值起（不倒退）"
        assert _wait_until(
            qapp,
            lambda: metrics and metrics[-1]["clock_seconds"] >= frozen + 8, 4000), \
            "继续后钟应从冻结值继续前进"
    finally:
        worker.shutdown(4000)
        assert not worker.isRunning()


def test_worker_rate_change_preserves_current_time(qapp, tmp_path):
    """调速保留当前时间：从快档降到慢档后仍停留在开场之后、不回起点、不清零。

    before 用 worker loop 上的同步探针紧前采样（不经 GUI 信号队列），并对涨幅只设
    宽松下界——真实流逝×60×调度延迟的量级不可控，紧上界易 flake；关键性质是「不倒退、
    不回到开场」。"""
    scene_p, a_p, b_p, models_p = _write_fixture(tmp_path)
    worker = SceneWorker()
    worker.start()
    msgs, metrics, statuses, _ = _connect_probes(worker)

    async def _read_clock() -> int | None:
        clock = worker._vclock
        return clock.current() if clock is not None else None

    def _probe() -> int | None:
        fut = asyncio.run_coroutine_threadsafe(_read_clock(), worker._loop)
        return fut.result(timeout=2)

    try:
        worker.start_scene(scene_p, [a_p, b_p], models_p, live=False, bid=None,
                           opening="入夜。", run_root=tmp_path / "rr_rate",
                           api_key=None, closing_at_block=100000)
        worker.set_pace(0.03)
        worker.set_rate(60.0)
        assert _wait_until(
            qapp,
            lambda: metrics and metrics[-1].get("clock_seconds", 0) > 77400 + 20, 6000), \
            "钟应在 60× 下走到明显晚于开场的时刻"
        before = _probe()
        assert before is not None and before > 77400 + 20

        worker.set_rate(0.25)                  # 骤降 → 折现当前时刻，不丢已走时间
        assert _wait_until(
            qapp, lambda: metrics and metrics[-1].get("rate") == 0.25, 3000)
        after_rate = _probe()
        assert after_rate >= before - 2, "调速不清零、不倒退"
        assert after_rate > 77400 + 5, "调速后仍停在开场之后（未回到起点 21:30）"
    finally:
        worker.shutdown(4000)
        assert not worker.isRunning()


def test_worker_natural_close_when_clock_crosses_boundary(qapp, tmp_path):
    """加速下虚拟钟跨过打烊边界 → worker 自然收束：状态 已收束、补发打烊导演行、
    autoplay 停、收束后不再有任何新消息。"""
    scene_p, a_p, b_p, models_p = _scene_near_boundary(tmp_path, "rr_close", "21:59")
    worker = SceneWorker()
    worker.start()
    msgs, metrics, statuses, finished = _connect_probes(worker)
    try:
        worker.start_scene(scene_p, [a_p, b_p], models_p, live=False, bid=None,
                           opening="快打烊了。", run_root=tmp_path / "rr_nat",
                           api_key=None, closing_at_block=100000)
        worker.set_pace(0.03)
        worker.set_rate(60.0)                     # 60s 预算 1 真实秒跨过
        assert _wait_until(qapp, lambda: finished, 6000), "应自然收束并 sig_finished"
        assert statuses and statuses[-1] == "已收束", f"末态应 已收束：{statuses}"
        assert any(m.get("speaker_type") == "director" and "打烊" in m.get("content", "")
                   for m in msgs), "应收束导演打烊行"
        assert not worker.can_say(), "收束后不可再发话"
        # 收束后不再有消息（打烊行与余量都已在 finished 前派发）。
        n_after = len(msgs)
        pump_end = time.monotonic()
        assert _wait_until(qapp, lambda: time.monotonic() - pump_end >= 0.5, 2000)
        assert len(msgs) == n_after, "收束后不应再有消息"
    finally:
        worker.shutdown(4000)
        assert not worker.isRunning()


def test_closing_line_is_built_from_scene_boundary_desc_and_translatable():
    """收束合成行用**场景自己的**边界描述，且经 i18n 目录出字（不再写死贝克街221B措辞）。

    场景模型是通用的（贝克街221B只是内置演示素材），收束行必须按场景的 hard_boundary.desc
    生成；没有描述时给一句中性兜底（不出现「贝克街221B/打烊」这类题材词）。
    """
    assert closing_text("散场了", "zh-Hans") == "（散场了。）"
    fallback = closing_text(None, "zh-Hans")
    assert fallback == closing_text("   ", "zh-Hans"), "空白描述等同没有描述"
    assert fallback.startswith("（") and fallback.endswith("）")
    assert "贝克街221B" not in fallback and "打烊" not in fallback, "兜底不得带贝克街221B绑定措辞"

    en = closing_text("the doors close", "en")
    assert "the doors close" in en
    assert "（" not in en, "英文条目不该用中文全角括号"


def test_worker_closing_line_comes_from_the_scene_not_restaurant_wording(qapp, tmp_path):
    """非贝克街221B场景（边界描述「散场了」）到点收束时，补发的导演行用场景自己的措辞。"""
    scene_p, a_p, b_p, models_p = _scene_near_boundary(
        tmp_path, "rr_desc", "21:59", desc="散场了", name="教室")
    worker = SceneWorker()
    worker.start()
    msgs, _metrics, statuses, finished = _connect_probes(worker)
    try:
        worker.start_scene(scene_p, [a_p, b_p], models_p, live=False, bid=None,
                           opening="快散场了。", run_root=tmp_path / "rr_desc_run",
                           api_key=None, closing_at_block=100000)
        worker.set_pace(0.03)
        worker.set_rate(60.0)
        assert _wait_until(qapp, lambda: finished, 6000), "应自然收束并 sig_finished"
        closers = [m.get("content", "") for m in msgs
                   if m.get("speaker_type") == "director"]
        assert any("散场了" in c for c in closers), f"收束行应用场景边界描述：{closers}"
        assert not any("打烊" in c or "贝克街221B" in c for c in closers), \
            f"收束行不得写死贝克街221B措辞：{closers}"
    finally:
        worker.shutdown(4000)
        assert not worker.isRunning()


def test_worker_forwards_scene_diagnostics_and_only_on_change():
    """sig_scene_diag：转发引擎 scene_state() 的诊断 + 活场景字段，且**只在变化时**发。

    每块都调一次 _emit_scene_diag（flush 后），若不去重，长场会把同一份诊断反复推给
    界面；实测口径：同样的状态连推两次只出一个信号，诊断变了才再发。
    """
    worker = SceneWorker()
    seen: list[dict] = []
    worker.sig_scene_diag.connect(seen.append)

    state = {
        "language": "zh-Hans", "hooks": ["h1"], "description_mutable": True,
        "fields": {"name": "茶室", "date": "", "background": "bg",
                   "description": "桌椅", "plot_direction": ""},
        "hook_error": None, "tool_error": None, "save_error": None,
        "implicit_events": [{"hook_id": "h1", "content": "暗处的钟停了"}],
    }

    class _Eng:
        def scene_state(self):
            return dict(state, fields=dict(state["fields"]),
                        implicit_events=list(state["implicit_events"]))

    engine = _Eng()
    worker._emit_scene_diag(engine)
    worker._emit_scene_diag(engine)
    assert len(seen) == 1, "同一状态不该重复广播"
    assert seen[0]["fields"]["name"] == "茶室"
    assert seen[0]["implicit_events"][0]["content"] == "暗处的钟停了"

    state["tool_error"] = "场景工具指令未改动任何字段"     # 诊断变化 → 再发一次
    worker._emit_scene_diag(engine)
    assert len(seen) == 2 and seen[1]["tool_error"].startswith("场景工具指令")

    state["fields"]["name"] = "雨夜的茶室"                # 场景自改字段 → 再发一次
    worker._emit_scene_diag(engine)
    assert len(seen) == 3 and seen[2]["fields"]["name"] == "雨夜的茶室"


def test_two_overlapping_launches_leave_exactly_one_live_engine(tmp_path, monkeypatch):
    """连点「开新场」/快速切场：两次 launch 交叠时只留一台活引擎，被取代的那台 aclose。

    旧实现没有代际令牌：先建成的那台被后完成的覆盖出 self._engine——它既没被 aclose
    （AsyncSqliteSaver 连接一直吊着，每切一次场漏一个），它的 autoplay/ticker 还成了
    孤儿任务继续跑。故这里用「open 慢」的替身引擎逼出交叠，断言：
      · 两次 launch 都真的建了引擎；
      · 活的只有最后那次那台；
      · 被取代的那台被 aclose 了，且它**没有**自己的 autoplay/ticker 任务。
    """
    worker_mod = __import__("harness.gui.worker", fromlist=["worker"])
    worker = SceneWorker()
    made: list = []
    started: list = []

    class _SlowEngine:
        """替身引擎：open_scene 故意慢（让两次 launch 真正交叠）。"""

        def __init__(self):
            self.closed = False
            self.aclosed = False
            self.start_seconds = 77400            # 21:30
            self.start_hhmm = "21:30"
            self.boundary_hhmm = None
            self.boundary_seconds = None
            made.append(self)

        async def open_scene(self, opening):
            await asyncio.sleep(0.05)

        async def aclose(self):
            self.aclosed = True
            self.closed = True

        async def messages(self):
            return []

        async def close_scene(self):
            self.closed = True

        def narration_state(self):
            return {"auto": True}

        def cast_state(self):
            return {"active": [], "inactive": [], "muted": {}}

        def dynamics_snapshot(self):
            return {}

        def dynamic_states(self):
            return {}

        def think_log_tail(self, n=0):
            return []

        def metrics(self):
            return {}

    async def _autoplay(self, engine):            # autoplay/ticker 换成记账桩
        started.append(("autoplay", engine))

    async def _ticker(self):
        started.append(("ticker", None))

    monkeypatch.setattr(worker_mod, "SceneEngine", lambda *a, **k: _SlowEngine())
    monkeypatch.setattr(worker_mod, "build_scene_payload", lambda engine: {"backend": "stub"})
    monkeypatch.setattr(SceneWorker, "_autoplay", _autoplay)
    monkeypatch.setattr(SceneWorker, "_v_ticker_loop", _ticker)

    worker._cfg = {"scene": tmp_path / "s.json", "characters": [tmp_path / "c.json"],
                   "models": tmp_path / "models.yaml", "bid": None, "live": False,
                   "api_key": None, "run_root": tmp_path / "runs",
                   "closing_at_block": None, "start_time": None,
                   "cast_from_cards": False}

    async def _drive():
        worker._oplock = asyncio.Lock()           # loop 上建锁（同 run() 的纪律）
        first = asyncio.ensure_future(worker._launch(None))
        second = asyncio.ensure_future(worker._launch(None))
        await asyncio.gather(first, second)

    asyncio.run(_drive())

    assert len(made) == 2, "两次 launch 都应真的建了引擎"
    assert worker._engine is made[-1], "活引擎必须是最后那次 launch 那台"
    assert made[0].aclosed, "被取代的引擎必须 aclose（否则 sqlite 连接一直吊着）"
    assert not made[1].aclosed, "胜出的引擎不该被关"
    assert [e for kind, e in started if kind == "autoplay"] == [made[-1]], \
        "被取代的那次不得留下孤儿 autoplay（只允许胜出者起任务）"
    assert [k for k, _e in started].count("ticker") == 1, \
        "ticker 也只应起一份（被取代者不得留孤儿 ticker）"


def test_worker_say_during_finalize_window_is_refused(qapp, tmp_path):
    """评审：say 恰在 _finalize(自然收束) 持锁期间被调度 → 拿锁后复查终态直接 return。

    精确复现窗口（确定性）：驱动协程持锁、此刻 spawn _say（预检 closed=False 通过并
    阻塞拿锁），随后在锁内跑真实收尾本体 _finalize_locked（close_scene + 已收束 +
    finished），释放后 _say 才拿到锁。若 _say 不在锁内复查，会把已收束覆盖成进行中、
    并 clock.start() 复活已停的钟——本测断言这些都不发生、且被拒收的文本不进对白。
    """
    scene_p, a_p, b_p, models_p = _write_fixture(tmp_path)
    worker = SceneWorker()
    worker.start()
    msgs, metrics, statuses, finished = _connect_probes(worker)
    try:
        worker.start_scene(scene_p, [a_p, b_p], models_p, live=False, bid=None,
                           opening="入夜。", run_root=tmp_path / "rr_saywin",
                           api_key=None, closing_at_block=100000)
        worker.set_pace(0.03)
        assert _wait_until(qapp, lambda: worker.can_say(), 6000), "场景应已开场"
        # 暂停钉住 autoplay（不再抢锁），收束窗口只由驱动协程控制。
        worker.set_paused(True)
        assert _wait_until(qapp, lambda: "已暂停" in statuses, 3000)

        async def _drive():
            engine = worker._engine
            assert engine is not None and worker._vclock is not None
            async with worker._oplock:
                # say 此刻预检 engine.closed=False 通过，随后阻塞在拿锁（正是窗口）。
                say_task = asyncio.ensure_future(
                    worker._say("临收束喊一句，应被拒收。"))
                await asyncio.sleep(0.01)
                # 真实收尾本体（持锁执行）：close_scene + flush + 已收束 + finished。
                await worker._finalize_locked("已收束", reason="时间到点(打烊)",
                                              close_engine=True,
                                              closing_content=None)
            await say_task
            # 拒绝路径：不复活钟、不改状态、不收行。
            assert not worker._vclock.is_running(), "拒绝路径不得 clock.start() 复活钟"
            assert worker._open is False

        fut = asyncio.run_coroutine_threadsafe(_drive(), worker._loop)
        fut.result(timeout=6)

        # 终态仍 已收束（say 未把它覆盖成 进行中）；收束后无任何新消息。
        assert _wait_until(qapp, lambda: bool(finished), 3000)
        assert statuses and statuses[-1] == "已收束", f"终态应仍 已收束：{statuses}"
        assert not any("应被拒收" in (m.get("content") or "") for m in msgs), \
            "收束窗口内的 say 不得写入 post-close 消息"
    finally:
        worker.shutdown(4000)
        assert not worker.isRunning()


# ============================================ 以所选角色卡为准的演员表（cast_from_cards）
def test_palette_wraps_for_casts_larger_than_the_palette():
    """配色盘回绕：选超过 8 人时下标取模 → 人人有色、不越界，且按演员表序分配。"""
    names = [f"角色{i}" for i in range(len(PALETTE) * 2 + 2)]
    colors = assign_name_colors(names)
    assert len(colors) == len(names)
    for i, n in enumerate(names):
        assert colors[n] == PALETTE[i % len(PALETTE)]
    assert colors[names[0]] == colors[names[len(PALETTE)]] == PALETTE[0], "第九人回绕"
    assert colors[names[len(PALETTE) + 1]] == PALETTE[1]
    assert all(c in PALETTE for c in colors.values())


def test_gui_cast_from_selection_renders_card_and_urge_row_per_selected(qapp, tmp_path):
    """**空场**（没存过阵容）选 3 张卡 → 右栏 3 张角色卡、左栏冲动卡 3 行、名单 3 人。

    场景存过阵容时以场景为准（scene.characters 权威，见 test_engine.py 的
    test_engine_stored_cast_wins_over_selected_cards）——故这里用一份空 characters 的
    场景文件代表「新建场景 / 还没存过人的场次」，所选卡由此播种进场（§3.1）。
    """
    scene, a, b, models = _write_fixture(tmp_path)
    scene.write_text(json.dumps({"name": "贝克街221B", "characters": [],
                                 "hard_boundary": {"type": "time", "value": "22:00",
                                                   "desc": "打烊"}},
                                ensure_ascii=False), encoding="utf-8")
    c = tmp_path / "丙.json"
    c.write_text(json.dumps({"name": "丙", "personality": {"描述": "旁观"}},
                            ensure_ascii=False), encoding="utf-8")
    bid = _make_stub_bid(tmp_path / "cast-bid.yaml")
    cfg = AppConfig(scene=scene, characters=[a, b, c], models=models, bid=bid,
                    run_root=tmp_path / "castrun", live=False, api_key=None,
                    closing_at_block=100000, opening="三人围坐。")
    worker = SceneWorker()
    win = MainWindow(worker, cfg)
    worker.start()
    infos: list[dict] = []
    worker.sig_scene_info.connect(infos.append)
    try:
        win.start_session()
        worker.set_pace(0.03)
        assert _wait_until(qapp, lambda: bool(infos), 6000), "应收到场景信息"
        assert infos[-1]["scene"]["participants"] == ["甲", "乙", "丙"]
        # 右栏角色卡：一张卡一组实时分量句柄 → 3 人 3 组。
        assert set(win._dyn_state) == {"甲", "乙", "丙"}, \
            "右栏应按所选 3 人各建一张角色卡"
        # 左栏冲动 bid（实时）卡：一行一人 → 3 行。
        assert set(win._urge_rows) == {"甲", "乙", "丙"}, \
            "左栏冲动卡应按所选 3 人各建一行"
        assert "丙" in win._participants_value.text()
    finally:
        worker.shutdown(4000)
        assert not worker.isRunning()


def test_worker_message_time_stamps_present_and_non_decreasing(qapp, tmp_path):
    """每条派发消息都带 time_hhmmss（HH:MM:SS），且按派发序单调不减。"""
    scene_p, a_p, b_p, models_p = _write_fixture(tmp_path)
    worker = SceneWorker()
    worker.start()
    msgs, metrics, statuses, _ = _connect_probes(worker)
    try:
        worker.start_scene(scene_p, [a_p, b_p], models_p, live=False, bid=None,
                           opening="入夜。", run_root=tmp_path / "rr_stamp",
                           api_key=None, closing_at_block=100000)
        worker.set_pace(0.03)
        worker.set_rate(30.0)
        assert _wait_until(
            qapp,
            lambda: len([m for m in msgs if m.get("time_hhmmss")]) >= 3, 6000), \
            "应有多条带时刻戳的消息"
        stamps = [m["time_hhmmss"] for m in msgs if m.get("time_hhmmss")]
        assert all(len(s) == 8 and re.fullmatch(r"\d{2}:\d{2}:\d{2}", s)
                   for s in stamps), "时刻戳应形如 HH:MM:SS"
        assert all(b >= a for a, b in zip(stamps, stamps[1:])), "时刻戳应单调不减"
    finally:
        worker.shutdown(4000)
        assert not worker.isRunning()


def test_gui_rate_combo_and_time_labels_update(qapp, tmp_path):
    """GUI 离屏：改流速档 → worker rate 生效；场景卡当前=HH:MM:SS、边界=22:00（无「剩余
    到打烊」这类贝克街221B绑定行）；对白每行前缀带 HH:MM:SS。"""
    worker, win, msgs, statuses = _spawn_window(tmp_path, "rateui",
                                                closing_at_block=100000,
                                                opening="入夜。")
    try:
        win.start_session()
        worker.set_pace(0.03)
        worker.set_rate(2.0)                       # 先 2× 快速让时钟离开 21:30
        rates: list = []
        worker.sig_metrics.connect(lambda m: rates.append(m.get("rate")))
        assert _wait_until(qapp, lambda: len(rates) and rates[-1] == 2.0, 4000)

        # 切到 8×（真实槽）：worker 收到新 rate，时间不被重置。
        idx8 = win._rate_combo.findData(8.0)
        assert idx8 >= 0
        win._rate_combo.setCurrentIndex(idx8)
        assert _wait_until(qapp, lambda: rates and rates[-1] == 8.0, 4000), \
            "流速档应更新 worker rate"

        # 场景卡：当前 HH:MM:SS（虚拟钟秒走）、开始/边界为场景自己的时刻。
        assert _wait_until(
            qapp,
            lambda: bool(re.fullmatch(r"\d{2}:\d{2}:\d{2}",
                                      win._clock_time["now"].text())), 4000), \
            "当前应显示 HH:MM:SS"
        assert win._clock_time["start"].text() == "21:30"
        assert win._boundary_value.text() == "22:00"
        left = _left_text(win)
        assert "剩余" not in left and "打烊" not in left, \
            "左栏不得再有「剩余到打烊」/「打烊」行（场景时间已并入场景卡且去贝克街221B绑定）"

        # 对白前缀 HH:MM:SS（开场/角色行已带 time_hhmmss）。
        assert _wait_until(
            qapp,
            lambda: bool(re.search(r"\d{2}:\d{2}:\d{2}", win._view.toPlainText())),
            6000), "对白应带 HH:MM:SS 前缀"
    finally:
        worker.shutdown(4000)
        assert not worker.isRunning()


def test_worker_emits_machine_readable_auto_pause_flag(qapp, tmp_path):
    """成本守卫的暂停/解除都发 sig_auto_paused(bool)（G6）：界面据此同步，不猜中文。"""
    scene_p, a_p, b_p, models_p = _write_fixture(tmp_path)
    worker = SceneWorker()
    worker.resume_every = 1                      # 跑满 1 块即自动暂停
    worker.start()
    msgs, metrics, statuses, finished = _connect_probes(worker)
    flags: list[bool] = []
    worker.sig_auto_paused.connect(flags.append)
    try:
        worker.start_scene(scene_p, [a_p, b_p], models_p, live=False, bid=None,
                           opening="入夜。", run_root=tmp_path / "rr_autop_flag",
                           api_key=None, closing_at_block=100000)
        worker.set_pace(0.03)
        assert _wait_until(qapp, lambda: True in flags, 8000), "守卫触发应发 True"
        assert any("点继续" in s for s in statuses), "可见文案一字不改（仍提示点继续）"

        worker.set_paused(False)                 # 手动续演 → 守卫解除
        assert _wait_until(qapp, lambda: flags and flags[-1] is False, 4000), \
            "手动续演应发 False"
    finally:
        worker.shutdown(4000)
        assert not worker.isRunning()


def test_gui_cost_guard_autopauses_until_manual_resume(qapp, tmp_path):
    """成本守卫（防烧 token）：autoplay 每跑满 resume_every 块自动暂停（冻结钟、停
    台词、绝不收束场景），状态提示点继续；手动「继续」后计数清零重计，再跑满
    resume_every 块又自动暂停。确定性/离线：resume_every 注入小值 3，stub 短跑即可
    两次触发；closing_at_block 架空（100000），验证不再有按块收束路径。"""
    worker, win, msgs, statuses = _spawn_window(tmp_path, "costguard",
                                                closing_at_block=100000,
                                                opening="入夜。")
    worker.resume_every = 3                  # 注入小阈值（生产缺省 500）
    chars = lambda: _char_msgs(msgs)         # noqa: E731
    autopause = lambda: [s for s in statuses if "点继续" in s]  # noqa: E731
    probe_clock = _clock_running_probe(worker)
    try:
        win.start_session()
        worker.set_pace(0.01)

        # 第一次自动暂停：跑满 3 块即暂停，状态提示 + 窗口同步为暂停态（按钮「继续」）。
        assert _wait_until(qapp, lambda: len(autopause()) >= 1, 8000), \
            "达 resume_every 块应自动暂停并提示点继续"
        assert win._paused is True and win._pause_btn.text() == "继续", \
            "窗口应同步为暂停态（按钮变「继续」）"
        assert not win._finished, "自动暂停只是暂停，不是收束场景"
        n1 = len(chars())

        # 暂停期间：不再产出台词、虚拟钟冻结。
        pump = time.monotonic()
        _wait_until(qapp, lambda: time.monotonic() - pump >= 0.3, 1500)
        assert len(chars()) == n1, "自动暂停期间不应再产出角色台词"
        assert probe_clock() is False, "自动暂停应冻结虚拟钟"

        # 手动点「继续」（真实槽）→ 钟恢复走动、角色继续开口（计数从 0 重计）。
        win._on_toggle_pause()
        assert win._paused is False and win._pause_btn.text() == "暂停"
        assert _wait_until(
            qapp, lambda: probe_clock() is True and len(chars()) > n1, 6000), \
            "继续后钟应恢复走动且角色继续开口"

        # 再跑满 3 块 → 第二次自动暂停（证明计数已清零重计，而非立刻再暂停）。
        assert _wait_until(qapp, lambda: len(autopause()) >= 2, 8000), \
            "继续后计数清零，再跑满 resume_every 块应再次自动暂停"
        assert len(chars()) - n1 >= 2, "两次自动暂停之间应持续产出若干块"
        assert not win._finished, "第二次自动暂停仍不收束场景"
        assert win._paused is True and win._pause_btn.text() == "继续"
    finally:
        worker.shutdown(4000)
        assert not worker.isRunning(), "worker 线程应收尾退出"


# ============================================================ 事件门控步进 / 实时动态 / 日志视图
def _make_bid(path: Path, *, speak_threshold: float = 0.0,
              interruption: float = 0.0, silence_k: int = 8) -> Path:
    path.write_text(
        f"interruption_threshold: {interruption}\n"
        f"speak_threshold: {speak_threshold}\n"
        f"silence_k: {silence_k}\n", encoding="utf-8")
    return path


def _think_row(*, urge: float, aroused: float = 0.5,
               goal: float = 0.0) -> dict:
    return {"aroused": aroused, "obligation_fulfilled": [],
            "goal_progress": goal, "urge": urge}


def _spawn_takeover(tmp_path: Path, sub: str, monkeypatch,
                    *, opening: str = "入夜。",
                    closing_at_block: int = 100000) -> tuple:
    """建一个**非对称权重**的离线窗口（走真实 MainWindow 装配）驱动数值竞价接管场景：

    白(敢说 w6_inhibition=0)每块高唤醒→开局连说；萧(高抑制 w6=1)恒 0 唤醒→前期无意开口，
    只靠沉默压力累积，直到其数值 bid 抬过阈值/在位者而自然拿回话筒。确定性 stub 脚本。
    关闭 worker.demo_alternate（走纯 bidding.arbitrate：incumbent 优势/打断阈值）——
    这样才能在载荷里观测到「B bid 反超 A 若干块后 B 才拿话筒」的纯数值接管相。
    返回 (worker, win, msgs, statuses, dynamics, chars)，信号已连好 sig_dynamics。
    """
    rows = ([_think_row(urge=2.0, aroused=1.0), _think_row(urge=0.0, aroused=0.0)]
            * 60)
    monkeypatch.setattr("harness.backends.stub._DEMO_THINK_SCRIPT", rows)
    speak_lines = [f"台本第{i}句。" for i in range(120)]
    monkeypatch.setattr("harness.backends.stub._DEMO_LINE_SCRIPT", speak_lines)

    scene_p, a_p, b_p, models_p = _write_fixture(tmp_path)
    a_p.write_text(json.dumps({"name": "甲", "personality": {"描述": "话痨"},
                               "weights": {"w6_inhibition": 0.0}},
                              ensure_ascii=False), encoding="utf-8")
    b_p.write_text(json.dumps({"name": "乙", "personality": {"描述": "克制"},
                               "weights": {"w6_inhibition": 1.0}},
                              ensure_ascii=False), encoding="utf-8")
    # interruption_threshold=0.4 造出「B 反超 A 但幅度未达打断阈值、话筒仍在 A」的相位，
    # 该相位正是载荷里出现 B_bid>A_bid 快照的来源（与引擎 e2e 同参）。
    bid = _make_bid(tmp_path / f"{sub}-bid.yaml", speak_threshold=0.1,
                    interruption=0.4, silence_k=100000)
    cfg = AppConfig(scene=scene_p, characters=[a_p, b_p], models=models_p,
                    bid=bid, run_root=tmp_path / sub, live=False, api_key=None,
                    closing_at_block=closing_at_block, opening=opening)
    worker = SceneWorker()
    worker.demo_alternate = False          # 纯数值竞价路径（关闭轮流开口叠加）
    win = MainWindow(worker, cfg)
    worker.start()
    msgs: list[dict] = []
    statuses: list[str] = []
    dynamics: list[dict] = []
    worker.sig_message.connect(msgs.append)
    worker.sig_status.connect(statuses.append)
    worker.sig_dynamics.connect(dynamics.append)
    chars = lambda: [m for m in msgs  # noqa: E731
                     if m.get("speaker_type") == "character"
                     and (m.get("content") or "").strip()]
    return worker, win, msgs, statuses, dynamics, chars


# 数值分量字段（sig_dynamics 载荷每位角色都应含这些演化分量 + bid）。
_NUMERIC_COMPONENTS = {"turns_since_spoke", "arousal", "adjacency", "relevance",
                       "goal_pressure", "scene_pressure", "bid",
                       "pending_reply", "turns_pending"}


def test_worker_autoplay_gated_idle_until_send(qapp, tmp_path, monkeypatch):
    """事件门控（§7.5 数值版）：**真死寂**（无唤醒 + 沉默压力关闭 silence_gain=0）
    → 没有任何数值 bid 会抬过开口阈值 → autoplay 容忍 silence_retry 个静默块后进入
    等待中（消息、块数、think 都不再增长，但虚拟钟照走）；随后 human say 重新武装、
    脚本唤醒转高 → 角色再次回应。控制数值状态而非自报 urge。"""
    # 死寂期：全员 aroused=0.0（唤醒折零）；say 之后的 think 换高唤醒(1.0) 供回应。
    rows = ([_think_row(urge=0.0, aroused=0.0)] * 12) + \
           ([_think_row(urge=1.0, aroused=1.0)] * 40)
    monkeypatch.setattr("harness.backends.stub._DEMO_THINK_SCRIPT", rows)

    scene_p, a_p, b_p, models_p = _write_fixture(tmp_path)
    bid = _make_bid(tmp_path / "gate-bid.yaml", speak_threshold=0.0,
                    silence_k=100000)          # 架空导演注入：空闲由 worker 门控产出
    worker = SceneWorker()
    worker.dynamics_params = DynamicsParams(silence_gain=0.0)   # 无沉默压力 → 真死寂
    worker.silence_retry = 3                   # 容忍 3 个静默聆听块再空闲
    # 本测专测「真死寂 → 门控空闲」这一语义：必须关掉自动场景叙述。否则停滞达阈值时
    # 场景会补一个外部事件（新消息）→ 门控重新武装 → 永远不会进入等待中（叙述本身是
    # 对的行为，见 test_scenarist；两者语义正交，故在此显式关闭）。
    worker.set_auto_narrate(False)
    worker.start()
    msgs: list[dict] = []
    statuses: list[str] = []
    thinks: list[dict] = []
    metrics: list[dict] = []
    worker.sig_message.connect(msgs.append)
    worker.sig_status.connect(statuses.append)
    worker.sig_think.connect(thinks.append)
    worker.sig_metrics.connect(metrics.append)
    chars = lambda: [m for m in msgs  # noqa: E731
                     if m.get("speaker_type") == "character"
                     and (m.get("content") or "").strip()]
    try:
        worker.start_scene(scene_p, [a_p, b_p], models_p, live=False, bid=bid,
                           opening="入夜。", run_root=tmp_path / "gate",
                           api_key=None, closing_at_block=100000)
        worker.set_pace(0.02)
        worker.set_rate(30.0)                    # 快钟：便于断言「空闲期钟照走」

        # 死寂数块后（数值 bid 恒低于开口阈值）autoplay 应进入等待。等待态用 worker
        # 的 _waiting_idle 镜像 + 文本双条件：文本可能先于最后一条旁路信号到，镜像保证
        # autoplay 确实已停在空闲分支（不再 step/think）。
        assert _wait_until(
            qapp,
            lambda: worker._waiting_idle
            and any(s.startswith("等待中") for s in statuses),
            8000), "真死寂数块后应进入等待中（不再自主 step）"

        # 空闲期：无台词/块数/think 增长，但虚拟钟继续走。条件驱动——等虚拟钟推进
        # ≥15 秒（rate=30 → 一个 ticker 节拍 ≈0.5s 内必达）后再断言零增长。
        n_chars, n_thinks = len(chars()), len(thinks)
        blocks0 = metrics[-1].get("blocks", 0) if metrics else 0
        ref_clock = metrics[-1].get("clock_seconds", 0) if metrics else 0
        assert _wait_until(
            qapp,
            lambda: bool(metrics)
            and metrics[-1].get("clock_seconds", 0) >= ref_clock + 15,
            3000), "空闲期虚拟钟应照常前进（≥15 虚拟秒）"
        assert len(chars()) == n_chars, "等待中不应再产出角色台词"
        assert len(thinks) == n_thinks, "等待中不应再追加 think（无烧 token）"
        assert metrics and metrics[-1].get("blocks", 0) == blocks0, \
            "等待中不应再推进块数（无 engine.step）"

        # human send 重新武装 → 点名唤回 → 角色回应、think 恢复、块数推进。
        # 注意：silence_gain=0 的真死寂里普通寒暄抬不动 bid（静默块还按情绪率衰减
        # 唤醒），必须**点名**触发邻接抬升，角色才会回应——这本身就是 §7 的语义。
        worker.say("甲，醒着吗？回我一声。")
        assert _wait_until(qapp, lambda: len(chars()) > n_chars, 6000), \
            "human send 应重新武装步进并让角色回应"
        assert _wait_until(qapp, lambda: len(thinks) > n_thinks, 6000), \
            "send 后的响应轮应产生新 think"
        assert _wait_until(
            qapp,
            lambda: metrics and metrics[-1].get("blocks", 0) > blocks0,
        ), "send 后块数应继续推进"
    finally:
        worker.shutdown(4000)
        assert not worker.isRunning(), "worker 线程应收尾退出"


def test_worker_numeric_takeover_no_brake_and_bid_overtakes(qapp, tmp_path,
                                                            monkeypatch):
    """独白硬刹车已移除（§7.5）：一个角色自报 urge 恒高也挡不住**久未开口者凭沉默
    压力自然拿回话筒**——worker 不再按「连续 N 句」计数刹车，发言权交数值 Dynamics
    竞价。

    A(白) 敢说（w6_inhibition=0）+ 每块高唤醒 → 开局连说数句形成独角戏势头；B(萧)
    高抑制（w6=1）且唤醒为 0 → 前期无意开口，只能沉默轮数一路涨——数值 bid 里 B 持续
    抬升、A 因 recency 惩罚回落，后续块 B 的 bid 反超 A 并 natural takeover（B 开口、
    A 的独角戏结束）。sig_dynamics 载荷实时反映这一接管；全程无任何刹车/收束状态。
    """
    worker, win, msgs, statuses, dynamics, chars = _spawn_takeover(
        tmp_path, "overtake", monkeypatch)
    try:
        win.start_session()
        worker.set_pace(0.02)

        # 白（敢说+高唤醒）先开口且连说数句，形成"独角戏"势头；全程无独白刹车状态。
        assert _wait_until(qapp, lambda: len(chars()) >= 3, 10000), "白应先开口数句"
        assert [m["speaker"] for m in chars()[:2]] == ["甲", "甲"], \
            "开局应为甲连说（高抑制的乙尚无意开口）"
        assert not any("已连续说了" in s for s in statuses), \
            "不得出现独白刹车状态（刹车已移除，交由数值竞价）"

        # sig_dynamics 载荷带全员 + 数值分量字段（bid/沉默轮数/…），且随块推进变化。
        assert _wait_until(
            qapp,
            lambda: {"甲", "乙"} <= set(dynamics[-1]) if dynamics else False,
            10000), "sig_dynamics 应含全员数值快照"
        assert _NUMERIC_COMPONENTS <= set(dynamics[-1]["甲"]), \
            "数值分量字段（含 bid）应齐全"

        # 关键断言：沉默压力一路累积的乙最终自然拿回话筒（B 开口）。
        assert _wait_until(
            qapp,
            lambda: any(m.get("speaker") == "乙" for m in chars()), 10000), \
            "久未开口的乙应凭沉默压力自然开口（无需刹车）"

        # 载荷历史里应存在某个快照 B 的数值 bid 反超 A（接管由数值决定，不是自报 urge）。
        def _bid(snap, name):
            st = snap.get(name) or {}
            return st.get("bid")

        overtook = any(_bid(s, "乙") is not None and _bid(s, "甲") is not None
                       and _bid(s, "乙") > _bid(s, "甲")
                       for s in dynamics)
        assert overtook, "沉默压力应把 B 的 bid 抬过 A（数值接管证据）"
        # bid 分量在两角色身上都移动过（talker 回落、silent 抬升）。
        assert len({_bid(s, "乙") for s in dynamics if _bid(s, "乙") is not None}) >= 2
        assert len({_bid(s, "甲") for s in dynamics if _bid(s, "甲") is not None}) >= 2

        # 未收束、可继续发话；白即使自报 urge 恒 2.0 也没能垄断全场。
        assert worker.can_say(), "natural takeover 只是换话筒，不收束场景"
        assert not any("已收束" in s or "已停止" in s for s in statuses)
    finally:
        worker.shutdown(4000)
        assert not worker.isRunning(), "worker 线程应收尾退出"


def test_worker_step_failure_reattempts_and_recovers(qapp, tmp_path):
    """评审修复回归：step 抛异常（注入一次 429/排队）→ autoplay 不得落 idle 静默——
    重新武装按节拍重试；失败以「模型暂不可用」持续提示，复健后恢复产出并回 进行中。
    用会一直开口的脚本（stub 默认 bid 无静默），失败只来自注入，绝无自然空闲。"""
    scene_p, a_p, b_p, models_p = _write_fixture(tmp_path)
    bid = _make_bid(tmp_path / "fail-bid.yaml", speak_threshold=0.0)
    worker = SceneWorker()
    worker.start()
    msgs: list[dict] = []
    statuses: list[str] = []
    worker.sig_message.connect(msgs.append)
    worker.sig_status.connect(statuses.append)
    worker.resume_every = 10**6             # 本测不触发成本守卫
    chars = lambda: [m for m in msgs  # noqa: E731
                     if m.get("speaker_type") == "character"
                     and (m.get("content") or "").strip()]
    try:
        worker.start_scene(scene_p, [a_p, b_p], models_p, live=False, bid=bid,
                           opening="入夜。", run_root=tmp_path / "fail",
                           api_key=None, closing_at_block=100000)
        worker.set_pace(0.02)
        assert _wait_until(qapp, lambda: len(chars()) >= 2, 8000), \
            "场景应先自主开口若干句"

        # 在引擎 loop 上把 engine.step 包一层：第一次调用抛 RuntimeError（模拟 429），
        # 之后放行原实现。
        async def _inject_once_failure():
            eng = worker._engine
            if eng is None:
                return False
            orig = eng.step
            fired = {"v": False}

            async def flaky(n: int = 1):
                if not fired["v"]:
                    fired["v"] = True
                    raise RuntimeError("simulated backend unavailable")
                return await orig(n)
            eng.step = flaky                # 实例属性遮蔽类方法（loop 线程内同步生效）
            return True
        fut = asyncio.run_coroutine_threadsafe(_inject_once_failure(), worker._loop)
        assert fut.result(timeout=2), "注入失败前引擎应已存在"

        n0 = len(chars())
        # 失败被提示（真实报错，而非一闪后被「等待中」盖掉）。
        assert _wait_until(qapp,
                           lambda: any("模型暂不可用" in s for s in statuses),
                           8000), "step 异常应上报 模型暂不可用"
        err_idx = next(i for i, s in enumerate(statuses) if "模型暂不可用" in s)

        # 自动重试 → 复健产出更多台词；绝不永久停在等待中。「进行中」排在产出消息
        # 之后才到达 GUI 线程，须有界轮询泵到其落地再断言（同步读会撞事件队列竞态）。
        assert _wait_until(qapp, lambda: len(chars()) > n0, 8000), \
            "失败后 autoplay 应重新武装重试并恢复产出（未永久停摆）"
        assert _wait_until(
            qapp,
            lambda: any(s == "进行中" and i > err_idx
                        for i, s in enumerate(statuses)),
            8000), "复健后应回 进行中"

        # 再确认复健后持续开口 ≥2 句（证明始终处于武装开口状态，绝非落入等待中静默）；
        # 此刻之前若真出现过「等待中」，随轮询事件队列已排空、必已在 statuses 里。
        assert _wait_until(qapp, lambda: len(chars()) >= n0 + 2, 8000), \
            "复健后应持续开口（≥2 句）"
        idle_after_err = any(s.startswith("等待中") and i > err_idx
                             for i, s in enumerate(statuses))
        assert not idle_after_err, "后端失败重试不应落入等待中静默"
    finally:
        worker.shutdown(4000)
        assert not worker.isRunning(), "worker 线程应收尾退出"


def test_worker_dynamics_emitted_and_right_card_update(qapp, tmp_path,
                                                       monkeypatch):
    """实时分量：sig_dynamics 载荷携带每名角色的数值分量（bid/沉默轮数/相关度/唤醒/
    邻接/目标压力/场景压力，随块推进变化；旧 think 字段 urge 等后向兼容保留），右栏
    「实时分量（随情景演化）」卡随之刷新并在后续块移动。确定性：非对称权重 + 脚本唤醒，
    B 每静默一块其沉默轮数/bid 都会上移。"""
    worker, win, msgs, statuses, dynamics, chars = _spawn_takeover(
        tmp_path, "dyncard", monkeypatch)
    try:
        win.start_session()
        worker.set_pace(0.02)

        def _snap():
            return dynamics[-1] if dynamics else {}

        # 载荷带全员 + 全部数值分量字段；旧 think 字段在后缘快照后向兼容保留。
        assert _wait_until(
            qapp,
            lambda: {"甲", "乙"} <= set(_snap()) and len(dynamics) >= 2,
            8000), "sig_dynamics 应携带全员数值快照（多轮）"
        assert _NUMERIC_COMPONENTS <= set(_snap()["甲"]), \
            "载荷应含全部数值分量字段（含 bid）"
        assert _wait_until(
            qapp,
            lambda: any("urge" in snap.get("甲", {}) for snap in dynamics),
            8000), "旧 think 字段(urge)应后向兼容保留"

        # 右栏实时分量行已建齐，某角色 bid/沉默轮数行由占位刷成数值。
        assert _wait_until(
            qapp, lambda: {"甲", "乙"} <= set(win._dyn_state), 8000), \
            "右栏角色卡实时分量应已建"
        ref_keys = {"bid", "turns", "relevance", "arousal", "adjacency",
                    "goal_pressure", "scene_pressure", "pending", "respond"}
        assert ref_keys <= set(win._dyn_state["甲"]), "右卡实时分量行应齐全"
        name = "乙"     # 沉默累积 → 沉默轮数单调 +1、沉默压力抬 bid（接管前必动）
        assert _wait_until(
            qapp,
            lambda: win._dyn_state[name]["bid"]["num"].text() not in ("", "—")
            and win._dyn_state[name]["turns"]["num"].text() not in ("", "—"),
            8000), "实时分量数值应已上屏"
        assert _wait_until(qapp, lambda: len(chars()) >= 1, 8000), "应有角色台词"

        # 后续块：沉默轮数行应上移（B 每静默一块 +1）；载荷里 B 的 bid 出现过 ≥2 值。
        turns0 = float(win._dyn_state[name]["turns"]["num"].text())
        assert _wait_until(
            qapp,
            lambda: float(win._dyn_state[name]["turns"]["num"].text()) != turns0,
            6000), "沉默轮数行应随块推进（B 每静默一块 +1）"
        b_bids = [snap[name]["bid"] for snap in dynamics if name in snap]
        assert len(set(b_bids)) >= 2, "sig_dynamics 里 B 的 bid 应随沉默压力变化"
        assert any("goal_pressure" in snap.get(name, {}) for snap in dynamics), \
            "目标压力分量应随载荷提供"
    finally:
        worker.shutdown(4000)
        assert not worker.isRunning(), "worker 线程应收尾退出"


def _dyn_payload(**kw) -> dict:
    """一份 engine.dynamics_snapshot()[name] 等价载荷（worker 逐字段转发，故同构）。"""
    st = {"turns_since_spoke": 0, "arousal": 0.0, "adjacency": 0.0,
          "relevance": 0.4, "goal_pressure": 0.3, "scene_pressure": 0.0,
          "pending_reply": 0.0, "turns_pending": 0, "bid": 0.5}
    st.update(kw)
    return st


def test_gui_pending_reply_row_shows_outstanding_ask(qapp, tmp_path):
    """右栏角色卡「实时分量」新增「待回应压力」行：被点名未答 → 该行显数值（条截在
    cap 8）、被点名/回应对象行后缀「（第 N 轮未答）」；作答后归零、后缀消失。"""
    worker = SceneWorker()                     # 只构造不 start：本测只验渲染路径
    cfg = AppConfig(scene=Path("scenes/贝克街221B.json"),
                    characters=[Path("characters/福尔摩斯.json")],
                    models=Path("config/models.yaml"), bid=Path("config/bid.yaml"),
                    run_root=tmp_path / "pend", live=False, api_key=None,
                    closing_at_block=8)
    win = MainWindow(worker, cfg)
    win._repopulate_right({"characters": [{"name": "甲",
                                           "personality": {"描述": "冷静"}}]})
    card = win._right_cards_layout.itemAt(0).widget()
    labels = [w.text() for w in card.findChildren(QLabel)]
    assert "待回应压力" in labels, "实时分量区应新增「待回应压力」行"

    refs = win._dyn_state["甲"]
    assert refs["pending"]["cap"] == 8.0, "压力条满格参考值取 8（义务可涨到 64）"
    assert refs["pending"]["num"].text() == "—", "未收到载荷前应是占位"

    # 被点名未答（义务 1.2 翻倍到 2.4，第 1 轮未答）→ 数值上屏 + 后缀。
    win._on_dynamics({"甲": _dyn_payload(pending_reply=2.4, turns_pending=1)})
    assert refs["pending"]["num"].text() == "2.40"
    assert refs["pending"]["bar"].value() == 30          # 2.4 / 8
    assert "第 1 轮未答" in refs["respond"].text()

    # 作答后：义务清零、后缀消失。
    win._on_dynamics({"甲": _dyn_payload(pending_reply=0.0, turns_pending=0)})
    assert refs["pending"]["num"].text() == "0.00"
    assert "未答" not in refs["respond"].text()


def test_gui_left_bid_card_tracks_realtime(qapp, tmp_path, monkeypatch):
    """左栏冲动(bid)卡实时追踪：按在场者每人一行（占位→数值），每块数值 Dynamics 演化
    → 该行 bid 数值与进度条随之更新（块 1 → 块 2），且与 sig_dynamics 载荷里的 bid
    同源。确定性：非对称权重 + 脚本化唤醒，白每说完一块 bid 即回落、下一块又变化。"""
    worker, win, msgs, statuses, dynamics, chars = _spawn_takeover(
        tmp_path, "bidcard", monkeypatch)
    name = "甲"     # 敢说+高唤醒 → 每块开口，bid 随 recency/沉默/唤醒持续变化
    try:
        win.start_session()
        worker.set_pace(0.02)

        # 场景信息落地 → 冲动卡已按两名在场者各建一行。
        assert _wait_until(qapp, lambda: set(win._urge_rows) == {"甲", "乙"}, 8000), \
            "左栏冲动卡应按两名在场者各建一行"

        # 数值 Dynamics 冷启动即广播初值：行由占位「—」变为 bid 数值。
        def _labeled():
            if not dynamics:
                return False
            row = win._urge_rows.get(name)
            return bool(row) and row["num"].text() not in ("", "—")
        assert _wait_until(qapp, _labeled, 8000), \
            "左栏冲动(bid)行应显示数值（冷启动初值即可）"
        # 数值竞价下首句台词常落到块 1~2——有界等待而非立即断言。
        assert _wait_until(qapp, lambda: len(chars()) >= 1, 8000), "应已有角色台词"

        # 读首见数值与条位 → 后续块 bid 变化 → 同一行数值/进度条随之更新。
        first = float(win._urge_rows[name]["num"].text())
        bar0 = win._urge_rows[name]["bar"].value()
        assert _wait_until(
            qapp,
            lambda: float(win._urge_rows[name]["num"].text()) != first,
            6000), "块 1→块 2 bid 数值应更新（左栏行实时变化）"
        assert win._urge_rows[name]["bar"].value() != bar0, \
            "bid 行的进度条应随数值变化而移动"

        # 载荷序列里同一角色的 bid 也应出现过 ≥2 个不同值（与行同源）。
        bids = [snap[name]["bid"] for snap in dynamics if name in snap]
        assert len(set(bids)) >= 2, "sig_dynamics 载荷应出现变化的 bid"
    finally:
        worker.shutdown(4000)
        assert not worker.isRunning(), "worker 线程应收尾退出"


def test_gui_log_toggle_shows_think_pane(qapp, tmp_path):
    """日志视图：点「日志」开关注入开关 → 上半区 think 日志窗格出现；跑若干块后
    日志文本含各角色名与格式化思考字段（冲动/唤醒/目标），普通对白保持原样。"""
    worker, win, msgs, statuses = _spawn_window(tmp_path, "log", closing_at_block=100000,
                                                opening="入夜。")
    chars = lambda: _char_msgs(msgs)  # noqa: E731
    try:
        win.start_session()
        worker.set_pace(0.02)
        assert _wait_until(qapp, lambda: len(chars()) >= 1, 8000), "角色应开口"

        # 默认关：日志窗格隐藏；点击开关 → 出现。
        assert win._log_view.isHidden(), "日志窗格默认应隐藏"
        conv_before = win._view.toPlainText()
        win._log_btn.click()
        assert not win._log_view.isHidden(), "点「日志」应显示日志窗格"
        assert win._view.toPlainText() == conv_before, \
            "开日志开关不得改动普通对白内容"

        # 跑若干块后日志文本含角色名与格式化思考字段。
        assert _wait_until(
            qapp,
            lambda: "甲" in win._log_view.toPlainText()
            and "乙" in win._log_view.toPlainText()
            and re.search(r"冲动 \d+\.\d{2}", win._log_view.toPlainText())
            # 起首是极简几何记号「·」（§3.6：界面不留任何 emoji）
            and "· 甲（思考）" in win._log_view.toPlainText(),
            8000), "日志窗格应含各角色思考格式化行"

        # 再关闭：日志窗格隐藏，对白不受影响。
        win._log_btn.click()
        assert win._log_view.isHidden(), "再点「日志」应隐藏日志窗格"
        assert win._view.toPlainText() == conv_before or len(chars()) > 0
    finally:
        worker.shutdown(4000)
        assert not worker.isRunning(), "worker 线程应收尾退出"
