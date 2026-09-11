"""模型接入抽象：think/speak 双档统一入口（设计文档 §8.1）。

桌面端用量统计（duck 契约）：任何后端都暴露以下实例计数属性，供 SceneEngine.metrics()
同步读取（引擎 aclose 也通用调 close）：
  .calls            成功请求次数（每次 complete_json/complete_text 成功 +1）
  .prompt_tokens    累计 prompt 词元
  .completion_tokens 累计 completion 词元
  .errors           失败次数（HTTP 错误/超时等；不吞异常，仅登记）
  .last_error       首次错误的文本摘要（str | None）
子类在 __init__ 里调用 super().__init__()（或 self._reset_usage()）归零；
每次调用结束用 _record_usage(prompt, completion, ok) 记账（词元来自厂商 usage
字段或离线近似，均在此累进）。联网后端可覆盖 close() 释放连接；缺省 no-op。
"""
from __future__ import annotations

from abc import ABC, abstractmethod


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
