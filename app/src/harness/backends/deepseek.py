"""DeepSeek OpenAI 兼容适配器（2026-09：统一 deepseek-v4-flash，思考在请求体切换）。

用量/错误计数字段（继承自 ModelBackend，见 base 模块 docstring）：
  .calls / .prompt_tokens / .completion_tokens / .errors / .last_error
登记点统一在 _call：成功读响应 usage（prompt_tokens/completion_tokens/total_tokens，
缺省按 0）累进并 calls+1；任何异常（HTTP 错误/超时/解析）先记 errors+1 与首次
last_error 文本，随后原样重抛——不吞异常、不改既有抛错契约（调用方 Pydantic 兜底
重试 / I3 重试属调用方职责）。close() 无持久客户端可释放，故为 base no-op。
"""
from __future__ import annotations

import json

import httpx

from .base import ModelBackend


class DeepSeekBackend(ModelBackend):
    name = "deepseek"

    def __init__(self, api_key: str, base_url: str = "https://api.deepseek.com",
                 model: str = "deepseek-v4-flash", params: dict | None = None):
        super().__init__()
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._params = params or {}
        self._timeout = self._params.get("timeout", 90.0)  # 服务端排队可达 ~10min

    def _payload(self, messages: list[dict], json_mode: bool) -> dict:
        body: dict = {"model": self._model, "messages": messages}
        thinking = self._params.get("thinking", "enabled")  # enabled|disabled
        body["thinking"] = {"type": thinking}
        if (eff := self._params.get("reasoning_effort")) is not None:
            body["reasoning_effort"] = eff
        if (mt := self._params.get("max_tokens")) is not None:
            body["max_tokens"] = mt
        if (temp := self._params.get("temperature")) is not None and thinking == "disabled":
            body["temperature"] = temp   # 温度仅非思考模式生效
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        return body

    async def _post(self, body: dict) -> dict:
        """POST /chat/completions 并返回整份 JSON data。纯网络层：不做记账，
        用量/错误登记统一在 _call（保证 monkeypatch _post 的抛错路径也被登记）。"""
        headers = {"Authorization": f"Bearer {self._api_key}",
                   "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.post(f"{self._base_url}/chat/completions",
                                  headers=headers, json=body)
            r.raise_for_status()
        return r.json()

    async def _call(self, body: dict) -> str:
        """带用量/错误记账的一次往返。成功读 usage 累进 calls/prompt/completion；
        异常记 errors 与首次 last_error 后原样重抛（保持既有抛错行为）。"""
        try:
            data = await self._post(body)
        except Exception as exc:
            if self._last_error is None:
                self._last_error = f"{type(exc).__name__}: {exc}"
            self._record_usage(ok=False)
            raise
        usage = data.get("usage") or {}
        self._record_usage(prompt=usage.get("prompt_tokens", 0),
                           completion=usage.get("completion_tokens", 0), ok=True)
        return data["choices"][0]["message"]["content"]

    async def complete_json(self, messages: list[dict]) -> dict:
        content = await self._call(self._payload(messages, json_mode=True))
        return json.loads(content)   # 解析失败由调用方(Pydantic)重试一次

    async def complete_text(self, messages: list[dict]) -> str:
        return await self._call(self._payload(messages, json_mode=False))
