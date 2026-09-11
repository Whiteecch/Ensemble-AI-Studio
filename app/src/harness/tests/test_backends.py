import os

import pytest

from harness.backends import get_backend
from harness.backends.stub import StubBackend


@pytest.mark.asyncio
async def test_stub_cycles_through_json_script():
    b = StubBackend(json_script=[{"urge": 0.1}, {"urge": 0.2}])
    assert (await b.complete_json([]))["urge"] == 0.1
    assert (await b.complete_json([]))["urge"] == 0.2
    assert (await b.complete_json([]))["urge"] == 0.1  # 循环回起点


@pytest.mark.asyncio
async def test_stub_text_script():
    b = StubBackend(line_script=["第一句", "第二句"])
    assert await b.complete_text([]) == "第一句"
    assert await b.complete_text([]) == "第二句"
    assert await b.complete_text([]) == "第一句"


def test_get_backend_unknown_name_raises():
    with pytest.raises(ValueError):
        get_backend("nope")


from harness.backends.deepseek import DeepSeekBackend


def test_deepseek_payload_shape():
    b = DeepSeekBackend(api_key="k", params={"thinking": "disabled", "max_tokens": 40})
    body = b._payload([{"role": "user", "content": "hi"}], json_mode=True)
    assert body["model"] == "deepseek-v4-flash"
    assert body["response_format"] == {"type": "json_object"}
    assert body["thinking"] == {"type": "disabled"}
    assert body["max_tokens"] == 40
    assert body["messages"] == [{"role": "user", "content": "hi"}]


def test_deepseek_text_payload_no_json_mode():
    b = DeepSeekBackend(api_key="k", params={"thinking": "enabled"})
    body = b._payload([], json_mode=False)
    assert "response_format" not in body
    assert body["thinking"] == {"type": "enabled"}


@pytest.mark.skipif(not os.environ.get("DEEPSEEK_API_KEY"), reason="需 DEEPSEEK_API_KEY")
@pytest.mark.asyncio
async def test_deepseek_live_think_json_roundtrip():
    b = DeepSeekBackend(api_key=os.environ["DEEPSEEK_API_KEY"],
                        params={"thinking": "disabled", "max_tokens": 60})
    out = await b.complete_json([
        {"role": "system", "content": "输出 JSON。目标 schema 键：{\"urge\": 0~1}。"},
        {"role": "user", "content": "json 请求：一句话回应。"},
    ])
    assert "urge" in out


# ---- 桌面端用量计数（offline：stub 确定性 / deepseek monkeypatch _post）----


@pytest.mark.asyncio
async def test_stub_counters_increment_across_calls():
    """StubBackend 离线确定性记账：每次 complete_json/complete_text 成功 +1 calls，
    prompt/completion 词元非负且带消息调用后 prompt > 0。"""
    b = StubBackend(json_script=[{"urge": 0.1}], line_script=["嗯。"])
    assert b.calls == 0 and b.prompt_tokens == 0 and b.completion_tokens == 0
    assert b.errors == 0 and b.last_error is None
    await b.complete_json([{"role": "user", "content": "今晚吃什么？"}])
    assert b.calls == 1
    assert b.prompt_tokens > 0           # 消息串长度估算
    assert b.completion_tokens > 0       # 产出 JSON 长度估算
    p0, c0 = b.prompt_tokens, b.completion_tokens
    await b.complete_json([])            # 空消息 → prompt 增量 0，仍 +1 calls
    assert b.calls == 2
    assert b.prompt_tokens >= p0 and b.completion_tokens > c0
    await b.complete_text([{"role": "user", "content": "再说一句"}])
    assert b.calls == 3
    assert b.prompt_tokens >= p0           # text 也有 prompt（消息串估算）
    assert b.completion_tokens > c0        # text 产出长度估算继续累进


def test_deepseek_zero_counters_before_any_call():
    """DeepSeekBackend 构造即归零：任何调用前 calls/prompt/completion/errors 为 0、
    last_error 为 None。"""
    b = DeepSeekBackend(api_key="k")
    assert b.calls == 0
    assert b.prompt_tokens == 0
    assert b.completion_tokens == 0
    assert b.errors == 0
    assert b.last_error is None


@pytest.mark.asyncio
async def test_deepseek_error_counted_when_post_raises(monkeypatch):
    """_post 抛错（HTTP/超时模拟）：记 error/last_error 后原样重抛（不改既有抛错
    契约），成功计数不动。"""
    b = DeepSeekBackend(api_key="k")

    async def _boom(body):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(b, "_post", _boom)
    with pytest.raises(RuntimeError, match="connection reset"):
        await b.complete_json([{"role": "user", "content": "hi"}])
    assert b.errors == 1
    assert b.last_error is not None and "connection reset" in b.last_error
    assert b.calls == 0 and b.prompt_tokens == 0 and b.completion_tokens == 0


@pytest.mark.asyncio
async def test_deepseek_usage_accumulates_from_fake_payload(monkeypatch):
    """_post 返回含 usage 的假载荷：success 计数与 prompt/completion 词元逐次累进。"""
    b = DeepSeekBackend(api_key="k")

    async def _fake_post(body):
        return {"usage": {"prompt_tokens": 10, "completion_tokens": 7,
                          "total_tokens": 17},
                "choices": [{"message": {"content": '{"urge": 0.4}'}}]}

    monkeypatch.setattr(b, "_post", _fake_post)
    out = await b.complete_json([{"role": "user", "content": "hi"}])
    assert out == {"urge": 0.4}
    assert b.calls == 1 and b.prompt_tokens == 10 and b.completion_tokens == 7
    await b.complete_json([])
    assert b.calls == 2 and b.prompt_tokens == 20 and b.completion_tokens == 14
    assert b.errors == 0
