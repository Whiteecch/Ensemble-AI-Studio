"""散场结算弹窗（设计文档 §8.3 / §7.2 / §7.3）。

散场时**每个在场角色一张「本场所得」清单**（角色名、场景名、新增/修订条数、几条标题），
每人一组「保留 / 丢弃」，顶部一组「全部保留 / 全部丢弃」的快捷操作。

三条硬边界，逐条对应设计文档：

1. **看得到才决定得了**（§7.2）。清单**照抄**引擎给的待决数据，本模块一条都不自己算：
   数与标题是引擎 `_collect_gain` 出来的（`events.settlement_row` 的载荷形状，经 worker
   的信号原样送到主线程）。弹窗自己数一遍，就等于给用户看了一份与"实际会并进去的东西"
   可能对不上的清单。
2. **一条都没并进去的警告要显示**（`settlement_warnings()`）。最要紧的是"某人的散场总结
   没写出来"——不显示，用户点了「保留」却什么都没发生，还不知道为什么。
3. **引擎绝不弹窗**（§7.4）。本模块跑在 Qt 主线程，只负责问与收决定；把决定送回引擎是
   worker 的事（`worker.apply_settlement` → `run_coroutine_threadsafe`）。

样式一律取 `theme.dialog_qss` 与既有 object name（`sect` / `hint` / `error` / `card` /
`primary` / `ghost`），**零字面颜色**——四套主题下观感一致（§3.6）。

程序化填充用：`rows` / `character_names` / `header_labels` / `detail_labels` /
`keep_radios` / `discard_radios` / `keep_all_btn` / `discard_all_btn` / `ok_btn` /
`cancel_btn` / `warning_label`，以及只读的 `decisions()`。测试**绝不真 exec**。
"""
from __future__ import annotations

from typing import Any, Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QDialog, QFrame, QHBoxLayout, QLabel, QRadioButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from .library import _ghost, _label, _primary, app_theme, translator
from .theme import dialog_qss

#: 决定的两个取值（与 `knowledgestore` 的 `OUTCOME_KEEP` / `OUTCOME_DISCARD` 同一口径；
#: 引擎 `apply_settlement` 认的就是这两个串，也认中文「保留」/「丢弃」）。
KEEP = "keep"
DISCARD = "discard"


def _text(value: Any) -> str:
    """载荷里的文本字段：None → 空串，其余照 `str`（脏值不该让弹窗打不开）。"""
    return "" if value is None else str(value)


def _count(value: Any) -> int:
    """载荷里的条数：读不出来当 0（一行脏数据不该让整窗报错）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _titles(row: dict) -> list[str]:
    """一行里的标题清单（非字符串项丢掉——它们是给用户看的文本，不是数据）。"""
    raw = row.get("titles")
    if not isinstance(raw, (list, tuple)):
        return []
    return [_text(item) for item in raw if _text(item).strip()]


class SettlementDialog(QDialog):
    """一场散场结算的「保留 / 丢弃」清单窗。

    `rows` 是引擎待决清单的**原样**载荷（`[{"name","scene","added","revised","titles"}]`）；
    `warnings` 是引擎 `settlement_warnings()` 那批要说给用户听的话（可为空）。
    用户按下「确定」时经 `decided(dict)` 回传 `{角色名: "keep"|"discard"}`；「取消」
    什么都不回传——没做决定与决定丢弃是两回事，后者和保留一样不可逆。
    """

    #: 决定回传（主线程 → worker）；载荷 = {角色名: "keep"|"discard"}。
    decided = Signal(dict)

    def __init__(self, rows: Sequence[dict], *, warnings: Sequence[str] = (),
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        t = translator()
        self.rows: list[dict] = [dict(r) for r in rows if isinstance(r, dict)]
        self.setStyleSheet(dialog_qss(app_theme()))
        self.setWindowTitle(t.t("dlg.settlement_title"))
        self.setMinimumSize(560, 460)

        #: 角色名 → 该角色的控件（同名重复的行只留第一条；清单是引擎给的，不该重名）
        self.header_labels: dict[str, QLabel] = {}
        self.detail_labels: dict[str, QLabel] = {}
        self.keep_radios: dict[str, QRadioButton] = {}
        self.discard_radios: dict[str, QRadioButton] = {}
        self._groups: list[QButtonGroup] = []       # 拿着引用，防被 GC 掉
        self.character_names: list[str] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)
        root.addWidget(_label(t.t("dlg.settlement_title"), "sect"))
        root.addWidget(_label(t.t("hint.settlement"), "hint"))

        self.warning_label = QLabel("")
        self.warning_label.setObjectName("error")
        self.warning_label.setWordWrap(True)
        self.warning_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        if warnings:
            self.warning_label.setText(
                t.t("label.settlement_warn_head") + "\n" + "\n".join(
                    _text(line) for line in warnings))
        else:
            self.warning_label.hide()      # 空的红字行只会让人以为出了事
        root.addWidget(self.warning_label)

        root.addLayout(self._build_quick_row())
        root.addWidget(self._build_list(), 1)
        root.addLayout(self._build_footer())

    # ---------------------------------------------------------------- 快捷操作
    def _build_quick_row(self) -> QHBoxLayout:
        """顶部「全部保留 / 全部丢弃」——只改选择，不替用户按下确定。"""
        t = translator()
        row = QHBoxLayout()
        row.setSpacing(8)
        self.keep_all_btn = _ghost(t.t("btn.keep_all"))
        self.keep_all_btn.clicked.connect(lambda: self._set_all(KEEP))
        self.discard_all_btn = _ghost(t.t("btn.discard_all"))
        self.discard_all_btn.clicked.connect(lambda: self._set_all(DISCARD))
        row.addWidget(self.keep_all_btn)
        row.addWidget(self.discard_all_btn)
        row.addStretch(1)
        return row

    def _build_list(self) -> QScrollArea:
        """每个角色一张卡片：清单文本 + 一组「保留 / 丢弃」。"""
        area = QScrollArea()
        area.setWidgetResizable(True)
        body = QWidget()
        col = QVBoxLayout(body)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(8)
        for row in self.rows:
            name = _text(row.get("name")).strip()
            if not name or name in self.keep_radios:
                continue                    # 无名/重名的行照旧不画（引擎不该给）
            self.character_names.append(name)
            col.addWidget(self._build_card(name, row))
        col.addStretch(1)
        area.setWidget(body)
        return area

    def _build_card(self, name: str, row: dict) -> QFrame:
        t = translator()
        card = QFrame()
        card.setObjectName("card")
        v = QVBoxLayout(card)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(4)

        header = QLabel(f"{name}　·　{_text(row.get('scene'))}")
        header.setObjectName("section")
        header.setWordWrap(True)
        self.header_labels[name] = header
        v.addWidget(header)

        lines = [t.t("settle.counts", added=_count(row.get("added")),
                     revised=_count(row.get("revised")))]
        titles = _titles(row)
        if titles:
            lines += [f"· {title}" for title in titles]
        else:
            lines.append(t.t("settle.no_titles"))
        detail = _label("\n".join(lines), "hint")
        self.detail_labels[name] = detail
        v.addWidget(detail)

        choice = QHBoxLayout()
        choice.setSpacing(12)
        group = QButtonGroup(card)
        keep = QRadioButton(t.t("btn.keep"))
        discard = QRadioButton(t.t("btn.discard"))
        keep.setChecked(True)               # 默认保留：费劲记下的东西不该被默认丢弃
        group.addButton(keep)
        group.addButton(discard)
        self._groups.append(group)
        choice.addWidget(keep)
        choice.addWidget(discard)
        choice.addStretch(1)
        self.keep_radios[name] = keep
        self.discard_radios[name] = discard
        v.addLayout(choice)
        return card

    def _build_footer(self) -> QHBoxLayout:
        t = translator()
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch(1)
        self.cancel_btn = _ghost(t.t("btn.cancel"))
        self.cancel_btn.clicked.connect(self.reject)   # 不回传任何决定
        self.ok_btn = _primary(t.t("btn.ok"))
        self.ok_btn.clicked.connect(self._on_ok)
        row.addWidget(self.cancel_btn)
        row.addWidget(self.ok_btn)
        return row

    # ------------------------------------------------------------------ 读决定
    def decisions(self) -> dict[str, str]:
        """当前选择：`{角色名: "keep"|"discard"}`（引擎 `apply_settlement` 收的形状）。"""
        return {name: (DISCARD if self.discard_radios[name].isChecked() else KEEP)
                for name in self.character_names}

    def _set_all(self, choice: str) -> None:
        """把所有人一次设成同一种选择（顶部两个快捷操作的落点）。"""
        for name in self.character_names:
            radio = (self.keep_radios if choice == KEEP else self.discard_radios)[name]
            radio.setChecked(True)

    def _on_ok(self) -> None:
        """「确定」：把决定交给窗外的世界（worker → 引擎），然后关窗。"""
        self.decided.emit(self.decisions())
        self.accept()
