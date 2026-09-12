"""模型接入抽象：think/speak 双档统一入口（设计文档 §8.1）。

桌面端用量统计（duck 契约）：任何后端都暴露以下实例计数属性，供 SceneEngine.metrics()
同步读取（引擎 aclose 也通用调 close）：
  .calls            成功请求次数（每次 complete_json/complete_text/complete_turn/
                    complete_text_stream 成功 +1——流式一次调用仍只算一次）
  .prompt_tokens    累计 prompt 词元
  .completion_tokens 累计 completion 词元
  .errors           失败次数（HTTP 错误/超时等；不吞异常，仅登记）
  .last_error       首次错误的文本摘要（str | None）
子类在 __init__ 里调用 super().__init__()（或 self._reset_usage()）归零；
每次调用结束用 _record_usage(prompt, completion, ok) 记账（词元来自厂商 usage
字段或离线近似，均在此累进）。联网后端可覆盖 close() 释放连接；缺省 no-op。
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import AsyncIterator


class ModelBackend(ABC):
    """无头模型后端。complete_json 返回合法 JSON dict；complete_text 返回正文文本。"""

    #: 计量键名（metrics()["think"]["stub"]…）。子类覆写为注册名（stub/deepseek）。
    name = "backend"

    def __init__(self) -> None:
        self._reset_usage()

    # ---- 用量/错误计数：鸭子契约，详见模块 docstring ----
    def _reset_usage(self) -> None:
        """归零所有计数。子类 __init__ 应先调 super().__init__()（或本方法）。"""
        self._calls = 0
        self._prompt = 0
        self._completion = 0
        self._errors = 0
        self._last_error: str | None = None

    def _record_usage(self, prompt: int = 0, completion: int = 0, ok: bool = True) -> None:
        """登记一次调用：ok=True → 成功（calls+1，词元累进）；ok=False → 失败
        （errors+1，不累计词元）。词元来自厂商 usage 或离线估算，负数输入按 0 计。"""
        if ok:
            self._calls += 1
            self._prompt += max(0, int(prompt))
            self._completion += max(0, int(completion))
        else:
            self._errors += 1

    @property
    def calls(self) -> int:
        return self._calls

    @property
    def prompt_tokens(self) -> int:
        return self._prompt

    @property
    def completion_tokens(self) -> int:
        return self._completion

    @property
    def errors(self) -> int:
        return self._errors

    @property
    def last_error(self) -> str | None:
        return self._last_error

    async def close(self) -> None:
        """释放后端持有连接/客户端。缺省 no-op（无状态后端无事可做）；
        联网后端按需覆盖。SceneEngine.aclose 对 think/speak 后端通用调用。"""
        return None

    @abstractmethod
    async def complete_json(self, messages: list[dict]) -> dict: ...

    @abstractmethod
    async def complete_text(self, messages: list[dict]) -> str: ...

    async def complete_turn(self, messages: list[dict],
                            tools: list[dict] | None = None) -> dict:
        """一次带原生工具调用的完整回合，返回**原始 assistant 消息 dict**（设计文档 §6.2/§6.3）。

        键与厂商 API 同名，至少含 content（API 给 null 时归一成空串），可选 tool_calls
        与 reasoning_content。返回值可直接追加进 messages 作为下一轮的 assistant 消息——
        这是**调用方契约**：带 tools 的后续轮次必须原样回传上一轮返回的全部键
        （尤其 reasoning_content/tool_calls），缺键会被服务端判为请求非法。

        **content 的形状随 tools 而变（三个后端口径一致）**：
          · 带 tools（非空）：工具轮。可能只回 tool_calls，content 为空串；终稿 JSON
            由调用方在循环收敛后改调 complete_json 取得（DeepSeek 口径，见 deepseek.py）。
          · 不带 tools（None 或空列表，两者等价）：**不是**工具轮，等价于一次
            complete_json——content 就是终稿 JSON 文本。「信息库关闭」走这条，请求与
            今天的 complete_json 逐字节相同，故关库角色一次调用都不多花（§6.3）。
        消费端（think 节点）拿 content 后紧接 json.loads/model_validate，退回正文文本
        会当场校验失败——所以这里绝不能走 complete_text。

        **刻意非抽象**：只实现两个老方法的后端与测试替身不该因为多了一个抽象方法就
        整片挂掉。缺省实现（不支持原生工具的后端）有 tools 时抛 NotImplementedError
        且不记账——调用方据此**降级**为不传 tools 再问一次；那次降级正落在上面第二条
        （返回终稿 JSON），故照本宣科也能收敛出合法终稿。
        """
        if tools:
            raise NotImplementedError(
                f"{type(self).__name__} 不支持原生工具调用；"
                "请不传 tools 重问一次（无 tools 即一次 complete_json，返回终稿 JSON）")
        draft = await self.complete_json(messages)
        return {"role": "assistant", "content": json.dumps(draft, ensure_ascii=False)}

    async def complete_text_stream(self, messages: list[dict]) -> AsyncIterator[str]:
        """流式正文（设计文档 §二）：逐片吐出 `complete_text` 的产出。**刻意非抽象**。

        缺省实现就是"一次吐完"——`yield await self.complete_text(messages)`。这样
        所有不支持流式的后端（旧后端、离线替身、测试里的 spy/假后端）与既有测试
        一行都不用改就能接进新通道；与 `complete_turn` 同一条纪律（新能力走带缺省
        实现的非抽象方法，而不是给抽象基类加一条抽象方法把整片替身拖下水）。

        **调用方契约**（graph.speak）：把收到的片**按序拼起来**即正文全文，且它必须与
        同提示词下 `complete_text` 的返回值一致——流式只是**呈现通道**，不是新的真相源
        （真相仍是收尾时落下的那条 block_spoken）。故实现者若自己分片，也必须只分不
        改：拼接结果逐字节等于完整产出。空串片（心跳/推理内容）不吐。

        记账归各后端自己的 `complete_text`（缺省实现里就是那一次调用记一次）；真流式的
        后端（deepseek）在自己的实现里按同口径记账。
        """
        yield await self.complete_text(messages)
