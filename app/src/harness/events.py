"""引擎→界面事件契约（技术方案 §9.2）。MVP 先定载荷结构，Task 12 起产出。"""
from __future__ import annotations

from typing import Any


def block_spoken(msg: dict) -> dict:
    return {"type": "block_spoken", "payload": msg}


def thinks_done(urges: dict[str, float]) -> dict:
    # 只含标量 urge，不含内心理由——守信息边界
    return {"type": "thinks_done", "payload": {"urges": urges}}


def decision_event(kind: str, speaker: str | None = None) -> dict:
    return {"type": "decision", "payload": {"kind": kind, "speaker": speaker}}


def scene_state(clock: int, closed: bool) -> dict:
    return {"type": "scene_state", "payload": {"clock": clock, "closed": closed}}
