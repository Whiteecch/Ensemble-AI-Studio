"""引擎→界面事件契约（技术方案 §9.2）。MVP 先定载荷结构，Task 12 起产出。

散场结算两个事件（设计文档 §7.3/§7.4）见文件末尾：它们承载"引擎只准备、界面才决定"
那条分工——引擎跑在自己的线程/事件循环里，**绝不弹窗**，问用户是界面/CLI 的事。
"""
from __future__ import annotations

from typing import Any, Sequence


def block_spoken(msg: dict) -> dict:
    return {"type": "block_spoken", "payload": msg}


def thinks_done(urges: dict[str, float]) -> dict:
    # 只含标量 urge，不含内心理由——守信息边界
    return {"type": "thinks_done", "payload": {"urges": urges}}


def decision_event(kind: str, speaker: str | None = None) -> dict:
    return {"type": "decision", "payload": {"kind": kind, "speaker": speaker}}


def scene_state(clock: int, closed: bool) -> dict:
    return {"type": "scene_state", "payload": {"clock": clock, "closed": closed}}


# ------------------------------------------------------------ 散场结算（§7） --

def _text(value: Any) -> str:
    """载荷里的文本字段：None → 空串，其余照 `str`（见 `settlement_row` 的理由）。"""
    return "" if value is None else str(value)


def settlement_row(*, name: str, scene: str, added: int, revised: int,
                   titles: Sequence[str] = ()) -> dict:
    """一个角色的「待结算」行（§7.2/§7.3）——两个结算事件共用同一形状。

    载荷：角色名、场景名、本场**新增**条数、本场**修订**条数、每条的一行标题。
    标题而不是键：界面/CLI 给用户看的是"他这一场得了什么"，键
    （`场景-茶室-第1场`）是给图和文件用的。

    逐字段转成字符串/整数：这份载荷要跨线程（Qt 信号）乃至跨语言（JSON）派发，
    调用方一时手滑传进来的怪值不该变成事件流里的地雷。缺值（None）落**空串**而不是
    字符串 `"None"`——界面上一行"场景：None"就是一次事故。键顺序固定、值确定，
    故同一个 row 序列化出来逐字节相同（测试与日志都靠它）。
    """
    return {"name": _text(name), "scene": _text(scene), "added": int(added),
            "revised": int(revised), "titles": [_text(t) for t in titles]}


def _row_copy(row: dict) -> dict:
    """一秒快照：连 `titles` 一起拷（浅拷只拷外层，调用方之后往里加一个标题，界面上
    的清单就跟着变了——引擎还在跑，这类共享最难排查）。"""
    copy = dict(row)
    copy["titles"] = list(row.get("titles") or ())
    return copy


def settlement_pending(rows: Sequence[dict]) -> dict:
    """**散场结算待决**（§7.4）：引擎已经准备好了（总结写完、清单算完、副本里的账记好），
    现在等界面/CLI 决定每人「保留 / 丢弃」。

    载荷 `characters` = 每个有待结算所得的角色一行（`settlement_row` 的形状）。
    没有这一行的角色 = 本场什么都没得到，不必出现在清单里（清单是"要看的东西"，
    不是"在场名单"）。

    行是**快照**：调用方拿走事件后引擎还会继续跑，别名调用方手里的那份 list 会让
    界面上的清单随引擎后续动作变化。
    """
    return {"type": "settlement_pending",
            "payload": {"characters": [_row_copy(row) for row in rows]}}


def character_pending_settlement(row: dict) -> dict:
    """**某角色离场、本场所得待结算**（§7.3）：离场那一刻不弹窗（你正在看戏，不该被
    一个模态框拦住），只挂这么一条提示——谁、哪一场、攒了几条；他随时可被单独结算。

    `row` 用 `settlement_row` 产出（两个事件同一形状，改一处不漏另一处）。
    """
    return {"type": "character_pending_settlement", "payload": _row_copy(row)}
