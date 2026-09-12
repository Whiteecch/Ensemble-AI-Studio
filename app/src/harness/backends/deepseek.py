"""DeepSeek OpenAI 兼容适配器（2026-09：统一 deepseek-v4-flash，思考在请求体切换）。

用量/错误计数字段（继承自 ModelBackend，见 base 模块 docstring）：
  .calls / .prompt_tokens / .completion_tokens / .errors / .last_error
登记点统一在 _call：成功读响应 usage（prompt_tokens/completion_tokens/total_tokens，
缺省按 0）累进并 calls+1；任何异常（HTTP 错误/超时/解析）先记 errors+1 与首次
last_error 文本，随后原样重抛——不吞异常、不改既有抛错契约（调用方 Pydantic 兜底
重试 / I3 重试属调用方职责）。close() 无持久客户端可释放，故为 base no-op。
"""
from __future__ import annotations

import codecs
import json
from typing import AsyncIterator

import httpx

from .base import ModelBackend

#: SSE 载荷里的终止标记（OpenAI 兼容口径：最后一条 data 行就是它）。
_SSE_DONE = "[DONE]"


class _SSEBuffer:
    """SSE **行缓冲**（纯逻辑，无 IO／无全局态）：喂字节块 → 吐完整的 `data:` 载荷串。

    为什么自己攒行而不是直接 `aiter_lines()`：半包（一行被网络切成两块，甚至断在多
    字节字符中间）、多包粘连（一块里好几条 data 行）、CRLF、末行无换行——这四种是
    流式最常见的坑，必须由**可单测的纯逻辑**处置，而不是埋在 httpx 的迭代器里
    （出了事只看得到"某台机器上偶尔少一句"）。

    · 字节 → 文本走**增量解码器**：utf-8 字符被切断时先留在解码器里，下块续上；
    · 文本按 `\\n` 切行，**最后那半截不完整的行留在缓冲**（`feed` 不吐它）；
    · 完整的行：去掉行尾 `\\r`，跳过空行与注释行（`: keep-alive`），只认 `data:` 前缀，
      取冒号后的载荷（去首尾空白）；
    · `close()` 把流末尾那半截（若还有）补吐一次，免得最后一片永远到不了界面。
    """

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._buf = ""

    def feed(self, chunk: bytes) -> list[str]:
        """喂一块原始字节，返回本次能凑出的全部 `data:` 载荷（可能为空）。"""
        self._buf += self._decoder.decode(chunk or b"")
        return self._drain(final=False)

    def close(self) -> list[str]:
        """流结束：冲刷解码器与最后那半截行，返回剩余的载荷。"""
        self._buf += self._decoder.decode(b"", final=True)
        return self._drain(final=True)

    def _drain(self, *, final: bool) -> list[str]:
        out: list[str] = []
        while True:
            nl = self._buf.find("\n")
            if nl < 0:
                break
            line, self._buf = self._buf[:nl], self._buf[nl + 1:]
            payload = _sse_payload(line)
            if payload is not None:
                out.append(payload)
        if final and self._buf:
            payload = _sse_payload(self._buf)
            self._buf = ""
            if payload is not None:
                out.append(payload)
        return out


def _sse_payload(line: str) -> str | None:
    """一条完整的 SSE 行 → 载荷串；不是 `data:` 行（空行/注释/其它字段）→ None。"""
    text = line.rstrip("\r")
    if not text.startswith("data:"):
        return None                        # 空行/`: ping`/`event:` 一律不是载荷
    return text[len("data:"):].strip()


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

    def _payload(self, messages: list[dict], json_mode: bool,
                 tools: list[dict] | None = None, stream: bool = False) -> dict:
        body: dict = {"model": self._model, "messages": messages}
        thinking = self._params.get("thinking", "enabled")  # enabled|disabled
        body["thinking"] = {"type": thinking}
        if (eff := self._params.get("reasoning_effort")) is not None:
            body["reasoning_effort"] = eff
        if (mt := self._params.get("max_tokens")) is not None:
            body["max_tokens"] = mt
        if (temp := self._params.get("temperature")) is not None and thinking == "disabled":
            body["temperature"] = temp   # 温度仅非思考模式生效
        if tools:
            # 原生工具调用（信息库 agentic 检索，§6.2）。**刻意不传 tool_choice**：
            # DeepSeek 思考模式直接拒绝该参数（HTTP 400），而留空即默认自选，
            # 非思考模式也一样；两档统一不传，少一条模式分支。
            body["tools"] = tools
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if stream:
            # 流式正文（设计文档 §二）：除了这两把开关，请求体与 complete_text 逐字节
            # 相同（多带 response_format 会把正文流逼成 JSON 输出）。
            # stream_options.include_usage：DeepSeek 在流末尾补一条只带 usage 的块，
            # 少了它，桌面端的词元/成本会把**最常走的** speak 那一路全记成 0。
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
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

    async def _call_message(self, body: dict) -> dict:
        """带用量/错误记账的一次往返，返回 choices[0].message 的**原始 dict**
        （含 content / tool_calls / reasoning_content）。记账与抛错契约与 _call 逐字
        一致：成功读 usage 累进 calls/prompt/completion；异常记 errors 与首次
        last_error 后原样重抛。取 message 放在记账**之后**，与改前 _call 的顺序一致
        （载荷畸形时同样已计成功、异常照抛）。"""
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
        return data["choices"][0]["message"]

    async def _call(self, body: dict) -> str:
        """带用量/错误记账的一次往返，取 content 文本。签名与语义保持既有：
        content 缺失照抛 KeyError、为 null 照返回 None(不归一——归一发生在 complete_turn)。"""
        return (await self._call_message(body))["content"]

    async def _post_stream(self, body: dict) -> AsyncIterator[bytes]:
        """POST /chat/completions（body 里已带 stream=true）并逐块吐出**原始字节**。

        纯网络层：不做记账（用量/错误登记统一在 `complete_text_stream`，与 `_post`／
        `_call` 的分工一致——monkeypatch 这一层的抛错路径因此同样被登记）。
        """
        headers = {"Authorization": f"Bearer {self._api_key}",
                   "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            async with client.stream("POST", f"{self._base_url}/chat/completions",
                                     headers=headers, json=body) as r:
                r.raise_for_status()
                async for chunk in r.aiter_bytes():
                    yield chunk

    async def _iter_sse_payloads(self, chunks: AsyncIterator[bytes]) -> AsyncIterator[str]:
        """字节块（async）→ 逐个 `data:` 载荷串：行缓冲交给 `_SSEBuffer`（纯逻辑）。"""
        buf = _SSEBuffer()
        async for chunk in chunks:
            for payload in buf.feed(chunk):
                yield payload
        for payload in buf.close():
            yield payload

    async def _stream_pieces(self, body: dict,
                             usage: dict) -> AsyncIterator[str]:
        """SSE 载荷 → 正文片：拼 `delta.content`、登记末尾 usage，中途 error 抛 RuntimeError。

        `usage` 是调用方给的**可变登记处**（流结束后由 `complete_text_stream` 读它记账）——
        异步生成器没法 return，故用量走这个出口。终止（`[DONE]`）即收尾，不再读后续块。
        """
        async for payload in self._iter_sse_payloads(self._post_stream(body)):
            if payload == _SSE_DONE:
                return
            data = json.loads(payload)
            if data.get("usage"):
                usage.update(data["usage"])
            if data.get("error"):
                raise RuntimeError(f"SSE 流中途报错：{data['error']}")
            for choice in data.get("choices") or []:
                piece = (choice.get("delta") or {}).get("content")
                if piece:
                    yield piece

    async def complete_text_stream(self, messages: list[dict]) -> AsyncIterator[str]:
        """真流式正文（设计文档 §二．2）：`stream: true` 的 SSE，逐块拼 `delta.content`。

        **记账与错误契约与 `_call` 逐字一致**：成功读末尾块的 usage 累进 calls/prompt/
        completion；任何异常（HTTP/超时/JSON 坏/中途 error）**先记 errors 与首次
        last_error，再原样重抛**——不吞、不改抛错类型，worker 靠它做整块重试。

        已吐出的片不收回（流式本来就是这样）：调用方（graph.speak）逐片放进只读接收器，
        界面上那句话说到一半断了就是断了，收尾那条 block_spoken 才是真相。
        """
        body = self._payload(messages, json_mode=False, stream=True)
        usage: dict = {}
        try:
            async for piece in self._stream_pieces(body, usage):
                yield piece
        except Exception as exc:
            if self._last_error is None:
                self._last_error = f"{type(exc).__name__}: {exc}"
            self._record_usage(ok=False)
            raise
        self._record_usage(prompt=usage.get("prompt_tokens", 0),
                           completion=usage.get("completion_tokens", 0), ok=True)

    async def complete_json(self, messages: list[dict]) -> dict:
        content = await self._call(self._payload(messages, json_mode=True))
        return json.loads(content)   # 解析失败由调用方(Pydantic)重试一次

    async def complete_text(self, messages: list[dict]) -> str:
        return await self._call(self._payload(messages, json_mode=False))

    async def complete_turn(self, messages: list[dict],
                            tools: list[dict] | None = None) -> dict:
        """原生工具调用的一轮（设计文档 §6.5，两个已核实的坑都在这里兑现）。

        请求体：tools 只在非空时加；**思考模式下绝不传 tool_choice**（DeepSeek 拒绝
        该参数）；thinking 仍是**顶层键** body["thinking"]（不是 extra_body）。

        response_format 只在**无 tools** 时加（json_mode = not tools）：
          · 带 tools = 工具轮，不是 JSON 模式（多带一个 response_format 会把工具轮也
            逼成 JSON 输出），终稿那轮由调用方走 complete_json；
          · 无 tools（None 与 [] 等价）= 信息库关闭，请求与 complete_json **逐字节
            相同**，content 即终稿 JSON。少了这一条，关库路径在这里返回自由文本，
            而离线 stub 吐 JSON——同一段集成代码离线绿、线上红。

        返回的 assistant 消息可直接追加进 messages。

        **调用方契约（硬约束，单靠后端保证不了）**：思考模式下带 tools 的后续轮次，
        必须把上一轮返回的 `reasoning_content` 按**同名键**原样回传，否则 DeepSeek
        返回 HTTP 400；`tool_calls` 同理要原样带上，随后按 tool_call_id 以 role:"tool"
        消息回喂本地执行结果。故本方法对 content 为 null 的情况归一为空串，并保留
        reasoning_content/tool_calls 原名原值。失败时沿用 _call_message：先记账再原样
        抛出，由 worker 做整块重试。
        """
        message = await self._call_message(
            self._payload(messages, json_mode=not tools, tools=tools))
        out: dict = {"role": "assistant", "content": message.get("content") or ""}
        if message.get("tool_calls"):
            out["tool_calls"] = message["tool_calls"]
        if message.get("reasoning_content") is not None:
            out["reasoning_content"] = message["reasoning_content"]
        return out
