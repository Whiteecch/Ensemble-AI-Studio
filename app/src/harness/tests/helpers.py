"""测试助手：取 checkpoint 末态 + 共享态白名单断言（守 §4.1 信息边界）。"""
from __future__ import annotations

from langgraph.graph.state import CompiledStateGraph

# 公共调度字段白名单：messages/urges/current_speaker/turn/silent_streak
# 及公共字段(decided/injected/closed/closing_at_block/blocks)。
# 角色私有(情绪/目标/印象)一旦出现在共享态即判失败。
# 场景虚拟时间不再是共享态键（clock 已移除）——真实流速虚拟钟由 worker/GUI 持有。
# retracted（被用户撤销的 id 列表）是公共调度字段：它只记 id，不含任何角色的私有解读。
PUBLIC_KEYS = {"messages", "urges", "current_speaker", "turn", "silent_streak",
               "decided", "injected", "closed", "closing_at_block", "blocks",
               "retracted"}


async def last_state(graph: CompiledStateGraph, thread_id: str) -> dict:
    snap = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    return snap.values


def assert_public_only(state: dict) -> None:
    extra = set(state) - PUBLIC_KEYS
    assert not extra, f"共享态混入私有字段: {extra}"
