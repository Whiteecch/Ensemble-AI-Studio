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

工具调用（complete_turn）：由构造参数 `tool_script` 注入的**确定性**脚本驱动，
按调用次序吐出"先若干工具轮、最后一条终稿"；脚本**只在调用方传了非空 tools 时生效**
（None 与 [] 等价 = 信息库关闭，一律走普通终稿，与 deepseek 的判据对齐）。缺省
None 时无工具能力（走基类缺省实现），离线演示脚本 _DEMO_THINK_SCRIPT /
_DEMO_LINE_SCRIPT 行为原样不变。
"""
from __future__ import annotations

import json
from typing import AsyncIterator

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


#: 流式演示的分片数（设计文档 §二：stub 给一个**确定性**的分片实现）。
#: 固定片数而不是"按标点切/按随机长度切"：离线测试与演示要的是"同输入同输出序列"，
#: 片数一飘，任何按片断言的用例都会变成偶发红。
_STREAM_SHARDS = 4


def _shard_text(text: str, shards: int = _STREAM_SHARDS) -> list[str]:
    """把整段均分成至多 `shards` 片（余数分给前几片）：**只分不改**，拼起来逐字节等于原文。

    短文本按长度收敛片数（不吐空片），空文本 → 空列表。纯函数、完全确定，供流式实现
    与测试共用（界面上"逐字长出来"的观感由此而来，而不是靠假延时——设计文档 §2.4
    明确不做打字机式的假节奏）。
    """
    text = str(text or "")
    if not text:
        return []
    n = max(1, min(int(shards), len(text)))
    base, extra = divmod(len(text), n)
    pieces: list[str] = []
    start = 0
    for k in range(n):
        size = base + (1 if k < extra else 0)
        pieces.append(text[start:start + size])
        start += size
    return pieces


def _approx_prompt_tokens(messages: list[dict]) -> int:
    """离线 prompt 估算：各消息 dict 里字符串值长度之和 // 2（≈中文 1~2 字/token，
    确定性、便宜，仅供桌面端计量展示，非厂商真实计费）。"""
    total = sum(len(v) for m in messages
                for v in m.values() if isinstance(v, str))
    return total // 2


class StubBackend(ModelBackend):
    name = "stub"

    def __init__(self, json_script: list[dict] | None = None,
                 line_script: list[str] | None = None,
                 tool_script: list[dict] | None = None):
        super().__init__()
        self._json_script = json_script or list(_DEMO_THINK_SCRIPT)
        self._line_script = line_script or list(_DEMO_LINE_SCRIPT)
        self._tool_script = list(tool_script) if tool_script else []
        self._j = 0
        self._t = 0
        self._u = 0

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

    async def complete_text_stream(self, messages: list[dict]) -> AsyncIterator[str]:
        """确定性分片（设计文档 §二）：整段切固定片数，逐片吐出——离线演示与测试的底座。

        **复用 `complete_text` 那一次调用**：脚本指针（`_t`）与记账（calls/prompt/
        completion）都只在那一条路上走一遍，故"走流式"与"不走流式"对同一个 stub 而言
        消耗完全一样（各一次调用、同一句输出）；流式只是**把同一句切成几块吐出来**。
        少了这一层复用，离线演示的调用序列会与今天不同，既有按脚本指针断言的用例
        就会开始漂。

        完全确定：同输入 → 逐字节相同的分片序列（不随机、不看时钟、不看环境）；拼接
        结果逐字节等于 `complete_text` 的整段。
        """
        line = await self.complete_text(messages)
        for piece in _shard_text(line):
            yield piece

    async def complete_turn(self, messages: list[dict],
                            tools: list[dict] | None = None) -> dict:
        """确定性的工具调用路径：`tool_script` 按**调用次序**逐条吐出（设计文档 §6.3）。

        脚本每条是 assistant 消息形状的 dict：前若干条带 `tool_calls`（可带
        `reasoning_content`）表示"模型要查库"，**最后一条不带 tool_calls**（`content`
        是终稿 JSON 文本）表示收敛。脚本用尽后**钉在最后一条**上重复吐——循环即使多问
        一次也不会再拿到工具调用，故离线测试永远不会转进无限工具轮。

        **`tools` 是这次要不要工具能力的开关，脚本只描述工具轮长什么样**：tools 为空
        （None 与 [] 等价，与 deepseek `_payload` 的判据一致）时不吐任何 tool_calls，
        退到基类缺省实现走一次普通调用（返回终稿 JSON）。少了这道闸门，离线路径会在
        用户把信息库**关掉**时照样执行查库回合，而同一份代码上线跑 deepseek 又不会——
        「关掉信息库不查库」的验收用例就会离线绿、线上错。无脚本（缺省 None）时同样
        退到基类：没有工具能力，有 tools 抛 NotImplementedError 由调用方降级。

        完全确定：同一条输入序列 → 逐字节相同的输出序列（不随机、不看时钟、不看环境）。
        独立计数器 `_u` 与老的 `_j`/`_t` 互不干扰，故老演示脚本的调用序列逐字节不变；
        无工具的那一枪不消费 `_u`，脚本从第一条起原样待命。
        """
        if not self._tool_script or not tools:
            return await super().complete_turn(messages, tools)
        item = self._tool_script[min(self._u, len(self._tool_script) - 1)]
        self._u += 1
        out: dict = {"role": "assistant", "content": item.get("content") or ""}
        if item.get("tool_calls"):
            out["tool_calls"] = item["tool_calls"]
        if item.get("reasoning_content") is not None:
            out["reasoning_content"] = item["reasoning_content"]
        self._record_usage(
            prompt=_approx_prompt_tokens(messages),
            completion=len(json.dumps(out, ensure_ascii=False)), ok=True)
        return out
