"""流式正文通道（设计文档 §二）：后端的 `complete_text_stream` 契约。

三件事，一条都不能松：

  · **基类缺省实现 = 一次吐完**（`yield await self.complete_text(messages)`）。仓库里
    所有只实现两个老方法的替身后端（spy/假后端/离线脚本）因此一行都不用改——新能力
    必须走"带缺省实现的非抽象方法"，与 `complete_turn` 同一条纪律。
  · **stub 的分片完全确定**：同输入 → 逐字节相同的分片序列；拼起来 == `complete_text`
    的整段（离线演示与测试的底座，任何随机/时间/环境依赖都会让它飘）。
  · **deepseek 的 SSE 解析**：`stream: true` + 逐块解析 `data:` 行拼 `delta.content`，
    其中半包（一行被切成两块）、多包粘连（一块里好几条 data 行）、`[DONE]`、中途
    `{"error": …}` 这四种是流式最常见的坑；记账与错误契约与 `_call` 逐字一致
    （失败先记 errors/last_error 再原样抛）。
"""
import pytest

from harness.backends.base import ModelBackend
from harness.backends.deepseek import DeepSeekBackend, _SSEBuffer
from harness.backends.stub import StubBackend, _shard_text


# --------------------------------------------------------------------- 基类缺省


class _LegacyBackend(ModelBackend):
    """只实现两个**老**抽象方法的最小替身（仓库里所有同类替身的代表）。

    它存在的唯一理由是钉死「新增 complete_text_stream 必须非抽象且有缺省实现」：
    声明成 @abstractmethod 的话这里实例化就 TypeError，既有替身整片阵亡。
    同时记下收到过什么，供断言缺省实现走的是不是 `complete_text` 那一条路。
    """

    name = "legacy"

    def __init__(self, text: str = "（正文产出）") -> None:
        super().__init__()
        self._text = text
        self.text_seen: list[list[dict]] = []

    async def complete_json(self, messages: list[dict]) -> dict:
        return {}

    async def complete_text(self, messages: list[dict]) -> str:
        self.text_seen.append(messages)
        self._record_usage(prompt=1, completion=1, ok=True)
        return self._text


async def test_base_complete_text_stream_yields_exactly_once():
    """缺省实现把 complete_text 的整段**一次**吐出，且原样转发消息序列。

    「一次吐完」不是权宜：不支持流式的后端与既有测试替身靠它一行不改地接入新通道，
    多吐几片就得靠"拼接"来还原正文，任何拼接口径的分歧都会让离线绿、线上红。
    """
    b = _LegacyBackend(text="一句台词。")
    msgs = [{"role": "user", "content": "hi"}]
    pieces = [p async for p in b.complete_text_stream(msgs)]
    assert pieces == ["一句台词。"]
    assert b.text_seen == [msgs], "缺省实现必须把消息原样转给 complete_text"
    assert b.calls == 1, "记账仍发生在 complete_text 里（一次调用记一次）"


async def test_base_complete_text_stream_returns_an_async_iterator():
    """返回的是**异步迭代器**（不是一次性算好的列表）：调用方 `async for` 逐片收。

    若实现退化成先 `await` 再返回 list，流式就名存实亡——重点不只是形状，而是它
    必须在 await 点之间把控制权交出去（真流式的呈现才有机会插入）。
    """
    b = _LegacyBackend()
    stream = b.complete_text_stream([])
    assert hasattr(stream, "__aiter__")
    assert not hasattr(stream, "__iter__"), "异步生成器不该同时是同步可迭代对象"


# ------------------------------------------------------------------ stub 确定性


def test_shard_text_splits_into_fixed_number_of_pieces():
    """`_shard_text` 把整段均分成固定片数（余数给前几片），拼起来逐字节等于原文。"""
    text = "一二三四五六七八九十"
    pieces = _shard_text(text, 4)
    assert len(pieces) == 4
    assert "".join(pieces) == text
    # 10 字切 4 片：前两片各多一字（商 2 余 2），次序固定、完全确定
    assert pieces == ["一二三", "四五六", "七八", "九十"]


def test_shard_text_never_empties_a_piece_and_handles_edges():
    """短文本片数收敛到长度（不吐空片），空文本直接不吐——空片会让"片数"失去意义。"""
    assert "".join(_shard_text("好", 4)) == "好"
    assert len(_shard_text("好", 4)) == 1
    assert _shard_text("", 4) == []
    assert _shard_text("ab", 0) == ["ab"], "片数下限 1（0 片等于一个字都吐不出来）"


async def test_stub_stream_is_deterministic_across_two_runs():
    """stub 分片**完全确定**：连跑两次得到逐字节相同的分片序列。

    它是离线演示与所有流式测试的底座；一旦沾上随机/时钟/环境，同一份测试会时绿时红。
    """
    outs_a: list[list[str]] = []
    outs_b: list[list[str]] = []
    for sink in (outs_a, outs_b):
        b = StubBackend(line_script=["（stub 占位台词，供离线演示。）"])
        for _ in range(3):
            sink.append([p async for p in b.complete_text_stream(
                [{"role": "user", "content": "hi"}])])
    assert outs_a == outs_b
    assert len(outs_a) == 3 and all(len(o) > 1 for o in outs_a), "必须是多片（真分片）"


async def test_stub_stream_pieces_concatenate_to_complete_text():
    """分片拼起来 == `complete_text` 的整段正文（同一脚本指针、同一记账）。

    这是「流式只是呈现通道」的硬约束：拼接结果与不走流式那一枪必须一致，否则界面
    上逐字长出来的一句和最终落下的那句会是两个人说的。
    """
    line = "（这句由 stub 生成。）"
    plain = StubBackend(line_script=[line])
    streamed = StubBackend(line_script=[line])
    assert await plain.complete_text([]) == line
    pieces = [p async for p in streamed.complete_text_stream([])]
    assert "".join(pieces) == line
    assert len(pieces) > 1
    assert streamed.calls == 1, "一次流式调用仍只算一次调用"
    assert streamed.completion_tokens == plain.completion_tokens


# --------------------------------------------------------------- deepseek SSE


def _fake_stream(chunks: list[bytes]):
    """`_post_stream` 替身：把给定字节块按序吐出（模拟网络分块，不联网）。"""
    async def _gen(body: dict):
        for chunk in chunks:
            yield chunk
    return _gen


def _chunk(content: str | None, usage: dict | None = None) -> bytes:
    """一条真实的 DeepSeek SSE data 行（含 delta.content 或纯 usage）。"""
    import json
    delta = {} if content is None else {"content": content}
    body = {"choices": [{"delta": delta, "index": 0}], "usage": usage}
    return f'data: {json.dumps(body, ensure_ascii=False)}\n\n'.encode("utf-8")


async def test_deepseek_stream_parses_pieces_and_done(monkeypatch):
    """正常流：逐条 data 行解析出 `delta.content`，`[DONE]` 收尾且不再吐。"""
    b = DeepSeekBackend(api_key="k")
    monkeypatch.setattr(b, "_post_stream", _fake_stream([
        _chunk("你好"), _chunk("，"), _chunk("世界"),
        b"data: [DONE]\n\n",
    ]))
    pieces = [p async for p in b.complete_text_stream(
        [{"role": "user", "content": "hi"}])]
    assert pieces == ["你好", "，", "世界"]
    assert b.calls == 1 and b.errors == 0
    assert b.last_error is None


async def test_deepseek_stream_handles_split_packets_and_glued_packets(monkeypatch):
    """半包 + 多包粘连（最常见的两个坑）：一行被切成两块、一块里塞好几条 data 行。

    行缓冲必须**跨块**续上（半包），且一块里要**逐行**切（粘连）——少任何一条，
    界面上就会出现整段消失或 JSON 解析失败。
    """
    b = DeepSeekBackend(api_key="k")
    raw = (_chunk("前半") + _chunk("后半") + _chunk("尾巴"))
    # 切成 1 字节一块：半包到了极致（连 `data:` 都是断的），行缓冲必须扛住
    monkeypatch.setattr(b, "_post_stream", _fake_stream([raw[i:i + 1]
                                                         for i in range(len(raw))]))
    pieces = [p async for p in b.complete_text_stream([])]
    assert pieces == ["前半", "后半", "尾巴"]
    assert b.calls == 1


async def test_deepseek_stream_handles_crlf_and_no_trailing_newline(monkeypatch):
    """CRLF 与"最后一行没有换行"两种服务端实现差异都要认（实测厂商之间有差）。"""
    import json
    body = json.dumps({"choices": [{"delta": {"content": "收尾"}}]},
                      ensure_ascii=False)
    b = DeepSeekBackend(api_key="k")
    monkeypatch.setattr(b, "_post_stream", _fake_stream([
        f"data: {body}\r\n\r\n".encode("utf-8"),   # CRLF
        f"data: {body}".encode("utf-8"),            # 末行无换行（流到此为止）
    ]))
    pieces = [p async for p in b.complete_text_stream([])]
    assert pieces == ["收尾", "收尾"]


async def test_deepseek_stream_records_usage_from_final_chunk(monkeypatch):
    """用量从**最后那条带 usage 的块**读（DeepSeek 的 include_usage 收尾块）。

    漏了这条，桌面端词元/成本会把整场对白的 speak 全部记成 0——最常走的那一条路径。
    """
    b = DeepSeekBackend(api_key="k")
    monkeypatch.setattr(b, "_post_stream", _fake_stream([
        _chunk("说了"),
        _chunk(None, usage={"prompt_tokens": 30, "completion_tokens": 9}),
        b"data: [DONE]\n\n",
    ]))
    assert [p async for p in b.complete_text_stream([])] == ["说了"]
    assert b.calls == 1 and b.prompt_tokens == 30 and b.completion_tokens == 9


async def test_deepseek_stream_midway_error_is_recorded_and_reraised(monkeypatch):
    """中途 `{"error": …}`：**先记账再原样抛**（与 _call 的失败契约一致）。

    吞掉它就等于把"模型说到一半断了"当正常收场：界面留着半句、引擎却以为整块成功。
    """
    import json
    b = DeepSeekBackend(api_key="k")
    err = json.dumps({"error": {"message": "rate limit", "type": "rate_limit"}})
    monkeypatch.setattr(b, "_post_stream", _fake_stream([
        _chunk("说到一半"),
        f"data: {err}\n\n".encode("utf-8"),
    ]))
    seen: list[str] = []
    with pytest.raises(RuntimeError):
        async for piece in b.complete_text_stream([]):
            seen.append(piece)
    assert seen == ["说到一半"], "已吐出的片不该被收回（流式本来就是这样）"
    assert b.errors == 1
    assert b.last_error is not None and "rate limit" in b.last_error
    assert b.calls == 0, "失败不计成功次数"


async def test_deepseek_stream_bad_json_payload_is_recorded_and_reraised(monkeypatch):
    """载荷不是合法 JSON（代理插话/半行乱码）：同样先记账再原样抛 JSONDecodeError。

    这条路径与"中途 error"同一收场：坏块不能当成空片吞掉——吞了就是静默丢一段正文。
    """
    import json as json_mod
    b = DeepSeekBackend(api_key="k")
    monkeypatch.setattr(b, "_post_stream", _fake_stream([b"data: {not json}\n\n"]))
    with pytest.raises(json_mod.JSONDecodeError):
        async for _piece in b.complete_text_stream([]):
            pass
    assert b.errors == 1 and b.calls == 0


async def test_deepseek_stream_http_error_is_recorded_and_reraised(monkeypatch):
    """连接层异常（HTTP/超时）同样先记账再原样抛，且一次都没吐。"""
    b = DeepSeekBackend(api_key="k")

    async def _boom(body: dict):
        raise RuntimeError("connection reset")
        yield b""                       # pragma: no cover - 只为让函数成为异步生成器

    monkeypatch.setattr(b, "_post_stream", _boom)
    with pytest.raises(RuntimeError, match="connection reset"):
        async for _piece in b.complete_text_stream([]):
            pass
    assert b.errors == 1 and b.calls == 0
    assert b.last_error is not None and "connection reset" in b.last_error


def test_deepseek_stream_payload_sets_stream_true_and_keeps_body_shape():
    """请求体：`stream: true` + 收尾用量开关，**其余与 complete_text 逐字节相同**。

    多带一个 response_format 会把正文流逼成 JSON 输出；thinking 仍是顶层键（不是
    extra_body）——与既有 _payload 的纪律一致。
    """
    b = DeepSeekBackend(api_key="k", params={"thinking": "enabled"})
    plain = b._payload([], json_mode=False)
    body = b._payload([], json_mode=False, stream=True)
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert {k: v for k, v in body.items()
            if k not in ("stream", "stream_options")} == plain
    assert "response_format" not in body


async def test_deepseek_stream_real_post_path_is_used(monkeypatch):
    """真路径（未经 my patch 的 `_post_stream`）也要走通：monkeypatch httpx 客户端，
    断言请求打到 /chat/completions 且带 stream=true——只测解析器会把"根本没发对流式
    请求"这种事故放过去。"""
    import httpx

    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        import json
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode("utf-8"))
        payload = json.dumps({"choices": [{"delta": {"content": "真流"}}]},
                             ensure_ascii=False)

        async def _body():
            # 真·异步字节流：AsyncClient 只接受异步流（同步迭代器会当场断言失败）
            yield f"data: {payload}\n\n".encode("utf-8")
            yield b"data: [DONE]\n\n"

        return httpx.Response(200, content=_body())

    real_client = httpx.AsyncClient

    def _client_factory(**kwargs):
        kwargs.pop("timeout", None)
        return real_client(transport=httpx.MockTransport(_handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory)
    b = DeepSeekBackend(api_key="k")
    pieces = [p async for p in b.complete_text_stream([{"role": "user", "content": "hi"}])]
    assert pieces == ["真流"]
    assert captured["url"].endswith("/chat/completions")
    assert captured["body"]["stream"] is True
    assert captured["body"]["messages"] == [{"role": "user", "content": "hi"}]
    assert b.calls == 1


# ------------------------------------------------------------------ SSE 行缓冲


def test_sse_buffer_keeps_partial_lines_across_feeds():
    """行缓冲的核心契约：没等到换行的那半截**留在缓冲里**，等下一块续上再吐。

    这是半包的唯一正确处置——把半截当整行解析就是 JSON 解析失败，那一句正文丢了。
    """
    buf = _SSEBuffer()
    assert buf.feed(b"data: {\"a\"") == []
    assert buf.feed(b": 1}\n\n") == ['{"a": 1}']


def test_sse_buffer_splits_glued_lines_and_skips_noise():
    """一块里多条 data 行逐行吐；空行、注释行（`: ping`）、其它字段行一律跳过。"""
    buf = _SSEBuffer()
    out = buf.feed(b': keep-alive\n\ndata: one\n\ndata: two\r\n\r\nevent: x\n')
    assert out == ["one", "two"]
    assert buf.close() == []


def test_sse_buffer_flushes_trailing_line_without_newline_on_close():
    """流没有以换行收尾时，close() 把最后那半截补吐出来（否则最后一片永远到不了界面）。"""
    buf = _SSEBuffer()
    assert buf.feed(b"data: tail") == []
    assert buf.close() == ["tail"]


def test_sse_buffer_decodes_multibyte_char_split_across_chunks():
    """一个多字节汉字被切成两块（utf-8 断在字符中间）也不能花字或抛异常。"""
    raw = "data: 好\n\n".encode("utf-8")
    buf = _SSEBuffer()
    out = buf.feed(raw[:7]) + buf.feed(raw[7:])
    assert out == ["好"]


def test_sse_buffer_is_pure_and_reusable():
    """纯逻辑（无 IO、无全局态）：同一个实例喂同一序列得到同一结果，可连用。"""
    def _run() -> list[str]:
        buf = _SSEBuffer()
        return buf.feed(b"data: a\n\ndata: b") + buf.close()
    assert _run() == _run() == ["a", "b"]
