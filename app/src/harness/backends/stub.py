"""确定性脚本 stub：离线测试与演示用，不联网。

M4(Task13) 演示修正：默认 think 脚本不再恒为 urge=0.7 —— 恒定 urge 下竞价仲裁
（bidding.arbitrate）会让在位者因「无人以 ≥0.4 幅度抢话」永远连任，离线演示退化成
独角戏。改为按调用次序低/高冲动循环（两角色各占一轮 think 调用时自然形成轮流对白），
同时保证每块至少一人开口、turn 照常推进，既有 engine 测试语义不变。

桌面端用量/错误计数字段（继承自 ModelBackend，见 base 模块 docstring）：
  .calls / .prompt_tokens / .completion_tokens / .errors / .last_error
stub 离线确定性近似（无真实 usage）：每次成功调用 prompt_tokens ≈ messages 各 dict
字符串值长度之和 // 2，completion_tokens ≈ 产出 JSON/文本的字符长度。恒成功、不抛
错，故 errors 恒 0、last_error 恒 None。close() 为 no-op。
"""
from __future__ import annotations

import json

from .base import ModelBackend

# 每块 think 扇出 = 每名在场者各一次调用（两人→每块 2 次）。按调用次序交替给
# 1.2 / 0.3，使「上一位说话者在下一块冲动回落、对方以 ≥0.4 幅度抢过话筒」成立。
_BASE_THINK = {"aroused": 0.5, "obligation_fulfilled": [],
               "goal_progress": 0.0, "urge": 0.7}
_DEMO_THINK_SCRIPT = [
    {**_BASE_THINK, "urge": 1.2},   # 调用 0：甲(若先扇出)
    {**_BASE_THINK, "urge": 0.3},   # 调用 1：乙
    {**_BASE_THINK, "urge": 0.3},   # 调用 2：甲 —— 冲动回落，让位
    {**_BASE_THINK, "urge": 1.2},   # 调用 3：乙 —— 抢过话头
]
_DEMO_LINE_SCRIPT = [
    "（这句由 stub 生成。）",
    "（stub 占位台词。）",
    "（没有感情的占位对白。）",
    "（同上，别误会。）",
]


def _approx_prompt_tokens(messages: list[dict]) -> int:
    """离线 prompt 估算：各消息 dict 里字符串值长度之和 // 2（≈中文 1~2 字/token，
    确定性、便宜，仅供桌面端计量展示，非厂商真实计费）。"""
    total = sum(len(v) for m in messages
                for v in m.values() if isinstance(v, str))
    return total // 2


class StubBackend(ModelBackend):
    name = "stub"

    def __init__(self, json_script: list[dict] | None = None,
                 line_script: list[str] | None = None):
        super().__init__()
        self._json_script = json_script or list(_DEMO_THINK_SCRIPT)
        self._line_script = line_script or list(_DEMO_LINE_SCRIPT)
        self._j = 0
        self._t = 0

    async def complete_json(self, messages: list[dict]) -> dict:
        item = self._json_script[self._j % len(self._json_script)]
        self._j += 1
        text = json.dumps(item, ensure_ascii=False)
        self._record_usage(prompt=_approx_prompt_tokens(messages),
                           completion=len(text), ok=True)
        return dict(item)

    async def complete_text(self, messages: list[dict]) -> str:
        line = self._line_script[self._t % len(self._line_script)]
        self._t += 1
        self._record_usage(prompt=_approx_prompt_tokens(messages),
                           completion=len(line), ok=True)
        return line
