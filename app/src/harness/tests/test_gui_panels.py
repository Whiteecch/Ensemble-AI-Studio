"""桌面外壳 S2a（离屏、确定性）：三栏面板重构的界面契约。

锁定四件事：
  · 左栏**整栏可滚动**（QScrollArea：widgetResizable、无横向条），「开场设定」卡与餐厅
    绑定行（打烊/剩余到打烊）彻底消失，原「场景时间」并入「场景」卡且只留通用行
    （场景名/日期（设了才有）/开始/当前/在场人数/边界）——边界是无时间硬边界时给「—」，
    界面上绝不出现「打烊」这类题材词；运行控制/推进控件在滚动重构后仍在且可达。
  · token 与消息/块数 ≥ 10 万走 k 单位（fmt_count，纯函数级边界全覆盖）。
  · 右栏角色卡瘦身：只留姓名（+色点）与「实时分量（随情景演化）」，「查看详情」灰色
    按钮弹出只读 CharacterDetailDialog 展示设定/能力/关系/性格权重/情绪衰减/语料。
  · 中栏「保存场景」按钮在「日志」左边，没场次时禁用、有场次时只交给 worker（绝不假装
    保存；落盘接线见 tests/test_gui_wiring.py）。
若环境未装 PySide6（可选依赖 gui），整模块 skip。
"""
import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QObject, Qt, Signal  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QLabel, QLineEdit, QPushButton, QScrollArea, QWidget)

from harness.gui import main_window as mw_mod  # noqa: E402
from harness.gui.main_window import (  # noqa: E402
    K_UNIT_THRESHOLD, AppConfig, MainWindow, fmt_count)


@pytest.fixture(scope="module")
def qapp():
    """模块级复用单一 QApplication（Qt 只允许每进程一个）。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _flush_deferred_deletes() -> None:
    """立刻执行 deleteLater 的删除事件（Qt 官方推荐的收尾方式）。

    离屏用例不跑事件循环：重建角色卡/换场（_clear_layout 的 deleteLater）留下的
    DeferredDelete 会一直悬在队列里，等窗口析构时才与 Qt 的子对象销毁相撞——实测会让
    **后续**用例在 worker 线程里访问冲突、崩掉整个 pytest 进程。故窗口的统一收尾是
    「先执行延迟删除 → 关窗/删窗 → 再执行一次」，保证本模块不给后面的 GUI 用例留雷。
    """
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()


@pytest.fixture
def win(qapp):
    """一个装配好的离屏窗口（stub 配置，场景未开；不读任何素材文件）。"""
    w = _window()
    try:
        yield w
    finally:
        _flush_deferred_deletes()
        w.close()
        w.deleteLater()
        _flush_deferred_deletes()


class _FakeWorker(QObject):
    """替身 worker：只记调用 + 提供 MainWindow 连接的信号（不起线程、不开场）。"""
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
        self.calls.append({"start_scene": kwargs})

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
        self.calls.append({"restart": (a, k)})

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

    def set_auto_narrate(self, on: bool) -> None:  # noqa: D102
        pass

    def narrate_now(self) -> None:
        pass

    def shutdown(self, *a) -> None:
        pass

    def isRunning(self) -> bool:                  # noqa: N802
        return False


def _window() -> MainWindow:
    """装配好的离屏窗口（stub 配置，场景未开；不读任何素材文件）。"""
    cfg = AppConfig(scene=Path("scenes/餐厅.json"),
                    characters=[Path("characters/甲.json")],
                    models=Path("config/models.yaml"), bid=Path("config/bid.yaml"),
                    run_root=Path("runs"), live=False, api_key=None,
                    opening="入夜。")
    return MainWindow(_FakeWorker(), cfg)


def _left_text(win: MainWindow) -> str:
    """左栏滚动区内全部标签文本（含被隐藏的行，故能断言「整行不存在」）。"""
    body = win._left_scroll.widget()
    return "\n".join(lb.text() for lb in body.findChildren(QLabel) if lb.text())


def _left_ui_text(win: MainWindow) -> str:
    """左栏**用户可见的全部文字**：标签文本 + 按钮文本 + 各处 tooltip。"""
    body = win._left_scroll.widget()
    chunks = [lb.text() for lb in body.findChildren(QLabel) if lb.text()]
    for wdg in body.findChildren(QWidget):
        if isinstance(wdg, QPushButton):
            chunks.append(wdg.text())
        if wdg.toolTip():
            chunks.append(wdg.toolTip())
    return "\n".join(chunks)


def _scene_info(*, name: str = "餐厅", participants=("甲",), boundary=None,
                boundary_time=None, date=None, characters=None) -> dict:
    """一份 sig_scene_info 等价载荷（worker.build_scene_payload 的形状）。"""
    scene = {"name": name, "participants": list(participants),
             "circles": [], "hard_boundary": boundary}
    if date is not None:
        scene["date"] = date
    return {"scene": scene, "characters": characters or [],
            "backend": "stub", "auto_narrate": True,
            "start_time": "21:30", "boundary_time": boundary_time}


def _card_payload(name: str, **kw) -> dict:
    """一张角色卡的场景信息载荷（build_scene_payload 里 chars[i] 的形状）。"""
    payload = {"name": name,
               "personality": {"描述": "冷静"},
               "abilities": ["枪法"],
               "relationships": {"乙": "旧识"},
               "weights": {"w1_relevance": 0.7, "w6_inhibition": 0.2},
               "emotion_decay_rate": 0.4,
               "corpus": {"source": "某作", "style": "短句",
                          "thinking": "先看什么再决定什么",
                          "quirks": ["唔"], "samples": ["嗯。"]}}
    payload.update(kw)
    return payload


# ============================================================ fmt_count（纯函数）
def test_fmt_count_below_threshold_verbatim():
    """低于 10 万：原样（不去千位、不补零）。"""
    assert fmt_count(0) == "0"
    assert fmt_count(1) == "1"
    assert fmt_count(99999) == "99999"
    assert fmt_count(K_UNIT_THRESHOLD - 1) == "99999"
    assert fmt_count("99999") == "99999"       # 数值串照收
    assert fmt_count("abc") == "abc"           # 脏值原样字符串化，不抛


def test_fmt_count_at_and_above_threshold_uses_k():
    """≥ 10 万：k 单位，且**向下截断到千位**（绝不上夸真实值）。"""
    assert fmt_count(K_UNIT_THRESHOLD) == "100k"
    assert fmt_count(273123) == "273k"
    assert fmt_count(273999) == "273k"         # 截断而非四舍五入
    assert fmt_count(999999) == "999k"         # 不因进位变 "1000k"
    assert fmt_count(1_000_000) == "1000k"
    assert fmt_count(100_000_000) == "100000k"


# ============================================================ 左栏：滚动 + 去餐厅绑定
def test_left_panel_whole_column_lives_in_scroll_area(win):
    """整栏包在滚动区里：widgetResizable、无横向滚动条；各卡的部件都在滚动区内容中。"""
    scroll = win._left_scroll
    assert isinstance(scroll, QScrollArea), "左栏应整栏可滚动"
    assert scroll.widgetResizable() is True, "滚动区应随窗口缩放（内容不被挤压变形）"
    assert scroll.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    body = scroll.widget()
    assert body.minimumWidth() > 0, "滚动内容应有最小宽度（窄栏时换行而非截断）"
    outer = scroll.parentWidget()
    assert outer.minimumWidth() >= body.minimumWidth(), \
        "整栏最小宽度须包住内容宽（横向滚动条关着，拖窄了只会裁字）"
    # 各卡的部件都随整栏进了滚动区（不是只包了其中一张卡）。
    for widget in (win._scene_value, win._metric["think"],
                   win._pause_btn, win._stop_btn, win._narrate_btn,
                   win._library_btn, win._reopen_btn, win._auto_scene_check):
        assert body.isAncestorOf(widget), f"{widget} 应在左栏滚动区内"
    assert "冲动 bid（实时）" in _left_text(win), "冲动 bid 卡也随整栏滚动"


def test_left_panel_dropped_opening_card_and_restaurant_rows(win):
    """「开场设定」卡与 打烊/剩余到打烊 行彻底消失；中栏输入框是唯一 QLineEdit。"""
    left = _left_ui_text(win)
    assert "开场设定" not in left, "开场设定卡应已删除（开场由开始界面/场景库负责）"
    assert "打烊" not in left, "不得再出现餐厅绑定措辞「打烊」（含 tooltip）"
    assert "剩余" not in left, "不得再有「剩余到打烊」行"
    assert not hasattr(win, "_opening_edit"), "开场输入框部件应已删除"
    assert win.findChild(QLineEdit, "opening") is None, "不应再有 objectName=opening 的输入框"
    # 库入口与「开新场」仍在左栏（只是搬进了运行控制卡）。
    assert win._library_btn.text() == "场景库…"
    assert win._reopen_btn.text() == "开新场"
    body = win._left_scroll.widget()
    assert body.isAncestorOf(win._library_btn) and body.isAncestorOf(win._reopen_btn)


def test_left_scene_card_merges_clock_and_shows_neutral_rows(win):
    """合并后的「场景」卡：场景名/开始/当前/在场人数 + 中性「边界」行。"""
    win._on_scene_info(_scene_info(
        participants=("甲", "乙"),
        boundary={"type": "time", "value": "22:00", "desc": "打烊"},
        boundary_time="22:00"))
    left = _left_text(win)
    for key in ("场景名", "开始", "当前", "在场人数", "边界"):
        assert key in left, f"合并后的场景卡应有「{key}」行"
    assert win._scene_value.text() == "餐厅"
    assert win._participants_value.text() == "甲、乙"
    assert win._clock_time["start"].text() == "21:30"
    assert win._boundary_value.text() == "22:00", "有 time 硬边界 → 边界行给 HH:MM"
    assert "打烊" not in left, "硬边界的 desc「打烊」不得被摆上界面"
    assert win._date_row.isHidden(), "场景没设日期 → 日期行整行不出现"


def test_left_scene_card_boundary_is_dash_without_time_boundary(win):
    """payload 的 boundary_time=None（无边界/非 time 类型）→ 边界行读「—」。"""
    win._on_scene_info(_scene_info(boundary=None, boundary_time=None))
    assert win._boundary_value.text() == "—"
    # 非 time 类型同样不给时刻（物理/社会/目标边界没有「到点」这回事）。
    win._on_scene_info(_scene_info(
        boundary={"type": "physical", "value": "打烊", "desc": "打烊"},
        boundary_time=None))
    assert win._boundary_value.text() == "—"
    # 指标刷新（boundary_hhmm 缺省）不得把「—」改成别的。
    win._on_metrics({"messages": 1, "blocks": 1, "clock_hhmmss": "21:31:07"})
    assert win._boundary_value.text() == "—"
    assert win._clock_time["now"].text() == "21:31:07"


def test_left_scene_card_shows_date_row_only_when_scene_has_one(win):
    """场景设了日期 → 日期行出现并显示该日期；换到没日期的场景 → 整行再隐藏。"""
    win._on_scene_info(_scene_info(date="1999-12-31"))
    assert not win._date_row.isHidden(), "设了日期应显示日期行"
    assert win._date_value.text() == "1999-12-31"
    win._on_scene_info(_scene_info())
    assert win._date_row.isHidden()
    assert "1999-12-31" not in _left_text(win), "换场后旧日期不得残留"


def test_left_controls_reachable_after_scroll_refactor(win):
    """运行控制（暂停/停止/流速）与场景推进（自动开关/推进一下）在滚动重构后仍可用。"""
    win._on_scene_info(_scene_info())
    assert win._pause_btn.isEnabled() and win._stop_btn.isEnabled()
    assert win._narrate_btn.isEnabled(), "开场后「推进一下」可点（就在左栏滚动区内）"
    assert win._rate_combo.currentData() == 1.0
    win._reopen_btn.click()                           # 未开过会话 → 无动作、不抛
    assert win._worker.calls == []


# ============================================================ 运行指标：k 单位
def test_metric_card_uses_k_units_for_tokens_and_counts(win):
    """token 与消息/块数 ≥ 10 万走 k 单位；用时照旧 "12.3 s"。"""
    win._on_metrics({
        "uptime_s": 12.34, "messages": 273123, "blocks": 99,
        "think": {"stub": {"calls": 4, "prompt_tokens": 100000,
                           "completion_tokens": 273123}},
        "speak": {"deepseek": {"calls": 1, "prompt_tokens": 5555,
                               "completion_tokens": 99999}},
        "clock_hhmmss": "21:31:07"})
    assert win._metric["uptime"].text() == "12.3 s", "用时保持秒（不缩写成 k）"
    assert win._metric["messages"].text() == "273k"
    assert win._metric["blocks"].text() == "99", "未过门槛原样"
    assert win._metric["think"].text() == "4 次 · token 100k+273k"
    assert win._metric["speak"].text() == "1 次 · token 5555+99999"


# ============================================================ 右栏：瘦身 + 详情弹窗
def test_right_card_keeps_only_name_and_realtime_components(win):
    """角色卡只剩姓名（+色点）与「实时分量（随情景演化）」块；静态资料一行不留。"""
    win._repopulate_right({"characters": [_card_payload("甲")]})
    assert set(win._dyn_state) == {"甲"}, "实时分量句柄仍按角色名归档"
    card = win._right_cards_layout.itemAt(0).widget()
    labels = [lb.text() for lb in card.findChildren(QLabel)]
    assert "甲" in labels
    assert "实时分量（随情景演化）" in labels
    for row in ("冲动 bid", "沉默轮数", "相关度", "唤醒", "邻接", "目标压力",
                "场景压力", "自报 urge", "待回应压力", "被点名/回应对象"):
        assert row in labels, f"实时分量区应保留「{row}」行"
    # 移出卡片的内容：设定/能力/关系/性格权重/情绪衰减/语料。
    joined = "\n".join(labels)
    assert "描述：冷静" not in joined
    assert "枪法" not in joined
    assert "旧识" not in joined
    assert "w1" not in joined and "性格权重" not in joined and "相关权重" not in joined
    assert "情绪衰减" not in joined
    assert "短句" not in joined and "某作" not in joined


def test_right_card_has_ghost_detail_button_that_opens_dialog(win, monkeypatch):
    """每张卡右上都有灰色「查看详情」按钮；点它弹出该角色的只读详情弹窗。"""
    win._repopulate_right({"characters": [_card_payload("甲"),
                                          _card_payload("乙")]})
    buttons = []
    for i in range(win._right_cards_layout.count()):
        item = win._right_cards_layout.itemAt(i)
        card = item.widget() if item is not None else None
        if card is None:
            continue
        found = [b for b in card.findChildren(QPushButton) if b.text() == "查看详情"]
        assert len(found) == 1, "每张角色卡应恰有一个「查看详情」按钮"
        assert found[0].objectName() == "ghost", "「查看详情」用现有灰色 ghost 样式"
        buttons.append(found[0])

    shown: list = []
    monkeypatch.setattr(mw_mod.CharacterDetailDialog, "exec",
                        lambda self: shown.append(self) or 0)
    buttons[0].click()                     # 真实按钮槽（exec 打桩，避免离屏模态阻塞）
    assert len(shown) == 1, "点「查看详情」应弹出详情弹窗"
    assert "冷静" in shown[0].body_text(), "弹窗内容应是**这张卡**（甲）的资料"


def test_detail_dialog_shows_setting_abilities_relations_weights_corpus(win):
    """详情弹窗以只读文本给出该角色的设定/能力/关系/权重/情绪衰减/语料全部取值。"""
    win._repopulate_right({"characters": [_card_payload("甲"),
                                          _card_payload("乙", personality={"描述": "锐利"})]})
    dlg = win._open_character_detail("甲")
    text = dlg.body_text()
    for expected in ("冷静",              # 设定
                     "枪法",              # 能力
                     "乙：旧识",         # 关系
                     "0.70", "0.20",       # 性格权重 w1/w6
                     "0.40",              # 情绪衰减
                     "某作", "短句", "先看什么再决定什么",   # 语料：出处/风格/思维
                     "唔", "嗯。"):        # 语料：口头禅/样例
        assert expected in text, f"详情弹窗应含该角色的「{expected}」"
    # 每位角色一份：乙的详情不含甲的取值；换场后旧卡查不到。
    other = win._open_character_detail("乙").body_text()
    assert "锐利" in other and "冷静" not in other
    win._repopulate_right({"characters": []})
    assert win._open_character_detail("甲") is None
    win._on_show_detail("甲")          # 查无此人：给提示而不抛
    assert "甲" in win._status_chip.text()


def test_detail_dialog_fills_placeholders_for_empty_card(win):
    """空卡（无设定/能力/关系/权重/语料）不得留白：给「（未填写）」占位。"""
    win._repopulate_right({"characters": [{"name": "空卡"}]})
    dlg = win._open_character_detail("空卡")
    assert dlg.body_text().count("（未填写）") >= 4, \
        "设定/能力/关系/权重/语料空项都应给占位"
    assert "空卡" in dlg.windowTitle()


# ============================================================ 中栏：保存场景按钮位
def test_save_scene_button_sits_left_of_log_and_is_not_faked(win):
    """「保存场景」在「日志」左边；开场前禁用，开场后点了也只交给 worker（不假装保存）。

    真实的落盘断言在 tests/test_gui_wiring.py（那里有会记账的替身 worker）；这里只锁
    位置与「没有场次时不可能发出任何保存动作」这一条。
    """
    top = win._save_scene_btn.parentWidget().layout().itemAt(0).layout()
    i_save = top.indexOf(win._save_scene_btn)
    i_log = top.indexOf(win._log_btn)
    assert i_save >= 0 and i_log >= 0, "两个按钮都应在中栏顶行"
    assert i_save < i_log, "「保存场景」应排在「日志」左边"
    assert not win._save_scene_btn.isEnabled(), "还没开场（没有可存的场次）→ 按钮禁用"
    win._save_scene_btn.click()            # 禁用态点击不该有任何副作用（不得抛）
    assert win._worker.calls == [], "没有场次时不得触发任何 worker 动作（不假装保存）"
    win._on_save_scene()                   # 直接调用同样安全（幂等）
    assert win._worker.calls == []
