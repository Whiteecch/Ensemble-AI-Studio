"""散场结算的两个新事件（设计文档 §7.3 离场挂起 / §7.4 触发点：引擎只准备、界面才决定）。

事件是**引擎与界面之间唯一的契约**：引擎跑在自己的线程/事件循环里，绝不弹窗（§7.4），
所以「这一场结束了、要不要保留」这件事只能经事件交给界面/CLI。钉三件事：

  · 两个事件的形状与既有事件同款（`{"type": ..., "payload": {...}}`，见 events.py），
    而且**能被 JSON 序列化**——GUI 走 Qt 信号、CLI 走内存，但载荷最终都要跨线程/跨语言；
  · 载荷里必须带得够界面渲染清单（§7.2："看得到才决定得了"）：角色名、场景名、
    新增几条、修订几条，以及每条的一行标题（不是键——用户看的是"他得了什么"）；
  · 一个角色的行由 `settlement_row` 一处产出，两个事件共用同一形状（改一处不漏另一处）。
"""
from __future__ import annotations

import json

from harness import events


def test_settlement_row_has_the_fields_the_ui_needs_to_show_a_list():
    """一个角色的待结算行（§7.2）：角色名、场景名、新增条数、修订条数、各自的一行标题。

    标题而不是键：界面/CLI 给用户看的是"他这一场得了什么"，`场景-茶室-第1场` 这种键
    是给图和文件用的。
    """
    row = events.settlement_row(name="甲", scene="茶室", added=2, revised=1,
                                titles=["药铺的暗格", "那封信", "旧相识"])
    assert row == {"name": "甲", "scene": "茶室", "added": 2, "revised": 1,
                   "titles": ["药铺的暗格", "那封信", "旧相识"]}


def test_settlement_row_coerces_types_so_the_payload_always_serializes():
    """载荷的每个字段都喂给了别的语言的界面/序列化器，故产出前统一口径：数字是数字、
    标题是字符串（模型/调用方给的怪值绝不能变成事件流里的地雷）。"""
    row = events.settlement_row(name=1, scene=None, added="2", revised=1.0,
                                titles=(None, 3))
    assert row == {"name": "1", "scene": "", "added": 2, "revised": 1,
                   "titles": ["", "3"]}, "缺值落空串，别写出字面上的 'None'"


def test_settlement_pending_event_shape_and_serializable():
    """「散场结算待决」（§7.4）：引擎准备好了（总结写完、清单算完），等界面决定保留/丢弃。

    载荷含**每个角色一行**（本条就是界面遍历渲染的那张清单）；整个事件必须能
    `json.dumps`，否则跨线程派发（GUI 的 sig_* → 主线程）会当场炸。
    """
    rows = [events.settlement_row(name="甲", scene="茶室", added=1, revised=0,
                                  titles=["药铺的暗格"]),
            events.settlement_row(name="乙", scene="茶室", added=0, revised=2,
                                  titles=["旧相识", "账本"])]
    event = events.settlement_pending(rows)

    assert event["type"] == "settlement_pending"
    assert event["payload"]["characters"] == rows
    assert json.loads(json.dumps(event, ensure_ascii=False)) == event


def test_settlement_pending_takes_a_copy_for_the_same_reason_as_other_events():
    """载荷是**快照**：调用方（GUI/CLI）拿走后引擎还会继续跑，共享同一个 list 会让
    界面上的清单随引擎后续动作变化。"""
    rows = [events.settlement_row(name="甲", scene="茶室", added=1, revised=0)]
    event = events.settlement_pending(rows)
    rows.append(events.settlement_row(name="乙", scene="茶室", added=1, revised=0))
    assert event["payload"]["characters"] == [
        {"name": "甲", "scene": "茶室", "added": 1, "revised": 0, "titles": []}]


def test_character_pending_settlement_event_shape_and_serializable():
    """「某角色离场、本场所得待结算」（§7.3）：离场**不弹窗**（不该打断他正在看的戏），
    只挂这么一条提示——谁、哪一场、攒了几条；他随时可被单独结算。"""
    row = events.settlement_row(name="乙", scene="茶室", added=1, revised=1,
                                titles=["药铺的暗格", "旧相识"])
    event = events.character_pending_settlement(row)

    assert event["type"] == "character_pending_settlement"
    assert event["payload"] == row
    assert json.loads(json.dumps(event, ensure_ascii=False)) == event


def test_character_pending_settlement_does_not_alias_the_caller_row():
    """同上：载荷快照，不别名调用方手里的那一份（引擎之后还会往那份 row 上加东西）。"""
    row = events.settlement_row(name="乙", scene="茶室", added=1, revised=0)
    event = events.character_pending_settlement(row)
    row["titles"].append("后来补的")
    assert event["payload"]["titles"] == []
