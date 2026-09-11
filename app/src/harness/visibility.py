"""可见性投影：信息边界 = 数据访问控制（设计文档 原则3）。

认知轴 knows（这条谁能看） × 在场轴 in_scene（在哪一场里）分开判定；对话圈已删除
（§3.2），在场轴退化为**场景本身**：一条消息要么属于本场景、要么不属于。
第三条轴是**进场轴**（§3.3 可插拔角色）：since_round 给出后，早于进场时刻产出的
消息一律不进视图——刚进场的角色看不到此前的一切对话（公共信息由提示词的场景段
另行供给，S4b）。纯函数、无 IO、不依赖 LangGraph，可独立单测。
"""
from __future__ import annotations

from typing import Iterable

from .schemas import Message


def view_for(messages: Iterable[Message], character: str, space: str,
             since_round: int | None = None) -> list[Message]:
    """返回 character 可见、且落在 space（场景名）内、按 id 升序的消息子集。

    space 即场景名：只有属于本场景的消息才进视图（跨场景/旧圈层的消息一律不入），
    再叠加认知轴 is_visible_to（自己是说话者、或 knows 放行）。

    since_round：**进场基线**（转录的 turn 制，见 engine.entry_round_of）。给了就丢掉
    turn < since_round 的消息，即「进场之前的对话他一概没听过」；None（缺省）＝不按
    进场过滤，行为与引进场轴之前逐字节一致（既有调用方/测试不受影响）。
    """
    view = [
        msg for msg in messages
        if msg.in_scene == space and msg.is_visible_to(character)
        and (since_round is None or msg.turn >= since_round)
    ]
    return sorted(view, key=lambda x: x.id)


def render_view(view: list[Message]) -> str:
    """拼成给 LLM 的转录文本；只含可见集，故 reply_to 链必然可回溯。"""
    lines = []
    for msg in view:
        addr = f" →{msg.address}" if msg.address else ""
        lines.append(f"[{msg.id}] {msg.speaker}{addr}: {msg.content}")
    return "\n".join(lines)
