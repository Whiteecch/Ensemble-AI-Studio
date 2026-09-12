import json
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


# ---- 原生工具调用 complete_turn（设计文档 §6.2/§6.3/§6.5）----
#
# 契约要点（三个后端共用同一形状）：
#   complete_turn(messages, tools=None) -> 原始 assistant 消息 dict，键与 API 一致
#   （role/content/tool_calls/reasoning_content）。它是信息库 agentic 检索的唯一入口：
#   think 从「一次调用」变成「调工具 → 本地执行 → 回喂 → 再调」，终稿仍是 JSON 文本。
# 为什么非抽象：老替身（测试里的 spy/假后端）只实现了两个老方法，若 complete_turn
# 是抽象方法，它们会连同整片既有测试一起挂掉——新能力必须走可选参数/带缺省实现。

from harness.backends.base import ModelBackend  # noqa: E402  （紧随其用途排布）

_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_entry",
        "description": "读取条目正文",
        "parameters": {"type": "object",
                       "properties": {"key": {"type": "string"}},
                       "required": ["key"]},
    },
}


def _tool_call(call_id: str, name: str, arguments: str) -> dict:
    """OpenAI 格式的一次工具调用：arguments 是 **JSON 字符串**（不是 dict），
    回喂时的 role:"tool" 消息靠 tool_call_id 与它关联。"""
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": arguments}}


def _msg_post(message: dict, usage: dict | None = None):
    """造一个 _post 替身：返回含 choices[0].message 的假载荷（不联网）。"""
    async def _fake_post(body: dict) -> dict:
        return {"usage": usage if usage is not None
                else {"prompt_tokens": 10, "completion_tokens": 7},
                "choices": [{"message": message}]}
    return _fake_post


class _NoToolBackend(ModelBackend):
    """最小后端替身：只实现 complete_json/complete_text 两个**老**抽象方法。

    它存在的唯一理由是钉死「新增 complete_turn 必须非抽象」：若基类把它声明成
    @abstractmethod，这里实例化就会 TypeError，仓库里所有同类替身一并阵亡。
    两个老方法各自记录收到的 messages，供断言缺省实现走的是**哪一条**路径——
    走 complete_text（正文）正是集成方照 docstring 降级就踩雷的那个坑。
    """

    name = "no-tool"

    def __init__(self, draft: dict | None = None, text: str = "（正文产出）") -> None:
        super().__init__()
        self._draft = {"urge": 0.5} if draft is None else dict(draft)
        self._text = text
        self.json_seen: list[list[dict]] = []
        self.text_seen: list[list[dict]] = []

    async def complete_json(self, messages: list[dict]) -> dict:
        self.json_seen.append(messages)
        self._record_usage(prompt=1, completion=1, ok=True)
        return dict(self._draft)

    async def complete_text(self, messages: list[dict]) -> str:
        self.text_seen.append(messages)
        self._record_usage(prompt=1, completion=1, ok=True)
        return self._text


async def test_base_complete_turn_default_without_tools_returns_terminal_json():
    """缺省 complete_turn（无 tools）返回的是**终稿 JSON 文本**，不是正文。

    消费端（graph.py think 节点）拿到 content 后紧接 json.loads + ThinkResult
    .model_validate；退回 complete_text 的正文会当场 ValidationError、整块失败。
    故无 tools 分支 = 一次 complete_json 包成 assistant 消息，且**不碰** complete_text。
    """
    b = _NoToolBackend(draft={"urge": 0.5})
    msgs = [{"role": "user", "content": "hi"}]
    out = await b.complete_turn(msgs)
    assert out["role"] == "assistant"
    assert json.loads(out["content"]) == {"urge": 0.5}
    assert "tool_calls" not in out           # 普通调用不可能凭空长出工具调用
    assert b.json_seen == [msgs]             # 确实转发了同一条消息序列
    assert b.text_seen == []                 # 正文路径一次都没走


async def test_base_complete_turn_default_with_tools_raises_not_implemented():
    """缺省 complete_turn（有 tools）：抛 NotImplementedError，**且不记账**——
    调用方据此降级为「不传 tools 再问一次」，那次降级调用才是花钱的那次。"""
    b = _NoToolBackend()
    with pytest.raises(NotImplementedError):
        await b.complete_turn([{"role": "user", "content": "hi"}],
                              tools=[_TOOL_SCHEMA])
    assert b.calls == 0 and b.errors == 0
    assert b.json_seen == [] and b.text_seen == []   # 没有发出任何调用


async def test_base_complete_turn_degradation_after_not_implemented_parses_as_json():
    """按 docstring 的降级链路整条走一遍：有 tools 抛 NotImplementedError → 不传
    tools 再问一次 → 拿到的必须是**可解析的终稿 JSON**。

    这条钉的是「docstring 的指引与实现的产物一致」。无原生工具的后端（仓库全部离线
    替身、离线演示的 stub、旧后端）都只能走这条降级路；若降级产物是正文，集成方照
    本宣科就会在整条离线链路上失败——那不是边角，是主干。
    """
    b = _NoToolBackend(draft={"urge": 0.9, "goal_progress": 0.1})
    msgs = [{"role": "user", "content": "hi"}]
    with pytest.raises(NotImplementedError):
        await b.complete_turn(msgs, tools=[_TOOL_SCHEMA])
    out = await b.complete_turn(msgs)        # docstring 说的「不传 tools 再问一次」
    assert json.loads(out["content"]) == {"urge": 0.9, "goal_progress": 0.1}
    assert b.calls == 1                      # 只花降级这一枪


def test_deepseek_payload_carries_tools_and_never_tool_choice():
    """有 tools 时 body 加 tools；**绝不加 tool_choice**（DeepSeek 思考模式拒绝该
    参数，加了直接 400）。tools 为空/缺省时 body 里不该出现 tools 键。"""
    b = DeepSeekBackend(api_key="k", params={"thinking": "enabled"})
    body = b._payload([], json_mode=False, tools=[_TOOL_SCHEMA])
    assert body["tools"] == [_TOOL_SCHEMA]
    assert "tool_choice" not in body
    # 思考模式的 body 里 thinking 是**顶层键**（不是 extra_body），这条易被改错
    assert body["thinking"] == {"type": "enabled"}
    assert "extra_body" not in body
    # 缺省/空 tools 与老调用逐字节一致
    assert "tools" not in b._payload([], json_mode=False)
    assert "tools" not in b._payload([], json_mode=False, tools=[])


def test_deepseek_payload_response_format_only_in_json_mode_even_with_tools():
    """response_format 只在 json_mode 时加：工具轮的请求不是 JSON 模式（终稿才是），
    多带一个 response_format 会把工具轮也逼成 JSON 输出。"""
    b = DeepSeekBackend(api_key="k", params={"thinking": "disabled"})
    assert "response_format" not in b._payload([], json_mode=False,
                                               tools=[_TOOL_SCHEMA])
    assert b._payload([], json_mode=True,
                      tools=[_TOOL_SCHEMA])["response_format"] == {"type": "json_object"}


async def test_deepseek_complete_turn_without_tools_is_byte_identical_to_complete_json(monkeypatch):
    """无 tools（信息库关闭）时 complete_turn 发出的请求体与今天的 complete_json
    **逐字节相同**：加 response_format、不加 tools/tool_choice、thinking 仍顶层键。

    这是「关掉信息库 = 与今天一模一样的调用」的硬约束，也是无工具后端降级后的同一形状：
    少了 response_format，线上 deepseek 返回的是自由文本而非 JSON，而离线 stub 吐的是
    JSON——同一段集成代码离线绿、线上红，验收用例两头对不上。
    """
    b = DeepSeekBackend(api_key="k", params={"thinking": "enabled"})
    seen: dict = {}

    async def _cap(body: dict) -> dict:
        seen.clear()
        seen.update(body)
        return {"usage": {"prompt_tokens": 5, "completion_tokens": 3},
                "choices": [{"message": {"content": '{"urge": 0.4}'}}]}

    monkeypatch.setattr(b, "_post", _cap)
    msgs = [{"role": "user", "content": "hi"}]
    out = await b.complete_turn(msgs)
    assert seen == b._payload(msgs, json_mode=True)   # 与 complete_json 的请求逐字节相同
    assert "tools" not in seen and "tool_choice" not in seen
    assert json.loads(out["content"]) == {"urge": 0.4}
    # 空 tools 列表与 None 等价：都不加 tools 键，同样是 JSON 终稿
    await b.complete_turn(msgs, tools=[])
    assert seen == b._payload(msgs, json_mode=True)


async def test_deepseek_complete_turn_parses_tool_calls_and_null_content(monkeypatch):
    """工具轮的真实形状：content 为 **null**（归一成空串）、tool_calls 原样带出、
    role 为 assistant。不归一的话下游对 content 做字符串操作会 AttributeError。"""
    b = DeepSeekBackend(api_key="k")
    monkeypatch.setattr(b, "_post", _msg_post({
        "role": "assistant",
        "content": None,
        "tool_calls": [_tool_call("call_1", "read_entry", '{"key": "药铺"}')],
    }))
    out = await b.complete_turn([{"role": "user", "content": "hi"}],
                                tools=[_TOOL_SCHEMA])
    assert out["role"] == "assistant"
    assert out["content"] == ""
    assert out["tool_calls"] == [_tool_call("call_1", "read_entry", '{"key": "药铺"}')]


async def test_deepseek_complete_turn_keeps_parallel_tool_calls_in_order(monkeypatch):
    """并行工具调用（一次返回多个 tool_call）：顺序与 id 必须原样保留——
    引擎要逐个本地执行并按 tool_call_id 回喂，丢了 id 就没法把结果对回请求。"""
    b = DeepSeekBackend(api_key="k")
    calls = [_tool_call("call_a", "read_entry", '{"key": "甲"}'),
             _tool_call("call_b", "remember", '{"key": "乙", "title": "乙"}')]
    monkeypatch.setattr(b, "_post",
                        _msg_post({"role": "assistant", "content": "",
                                   "tool_calls": calls}))
    out = await b.complete_turn([{"role": "user", "content": "hi"}],
                                tools=[_TOOL_SCHEMA])
    assert [c["id"] for c in out["tool_calls"]] == ["call_a", "call_b"]


async def test_deepseek_complete_turn_carries_reasoning_content(monkeypatch):
    """思考模式下带 tools 的**后续轮次必须原样回传上一轮的 reasoning_content**，
    否则 HTTP 400。后端单方面保证不了回喂，所以把它按 API 同名键带进返回值，
    由调用方拼回 messages（这是本方法的调用方契约，见 docstring）。"""
    b = DeepSeekBackend(api_key="k", params={"thinking": "enabled"})
    monkeypatch.setattr(b, "_post", _msg_post({
        "role": "assistant", "content": None, "reasoning_content": "我先想想……",
        "tool_calls": [_tool_call("call_1", "read_entry", "{}")],
    }))
    out = await b.complete_turn([{"role": "user", "content": "hi"}],
                                tools=[_TOOL_SCHEMA])
    assert out["reasoning_content"] == "我先想想……"
    # 终稿轮没有推理内容时不凭空造键（回喂时缺键才是正常形状）
    monkeypatch.setattr(b, "_post", _msg_post({"role": "assistant",
                                               "content": '{"urge": 0.5}'}))
    assert "reasoning_content" not in await b.complete_turn([],
                                                            tools=[_TOOL_SCHEMA])


async def test_deepseek_complete_turn_records_usage(monkeypatch):
    """complete_turn 沿用 _call 的记账口径：成功读 usage 的 prompt/completion 词元，
    工具轮同样计入桌面端用量——agentic 检索是本项目最贵的一档，漏记会低估成本。"""
    b = DeepSeekBackend(api_key="k")
    monkeypatch.setattr(b, "_post", _msg_post(
        {"role": "assistant", "content": '{"urge": 0.5}'},
        usage={"prompt_tokens": 33, "completion_tokens": 12}))
    await b.complete_turn([{"role": "user", "content": "hi"}])
    assert b.calls == 1 and b.prompt_tokens == 33 and b.completion_tokens == 12


async def test_deepseek_complete_turn_error_recorded_and_reraised(monkeypatch):
    """失败路径与 _call 一致：先记 errors/last_error，再**原样 re-raise**——
    worker 靠这个异常做整块重试；吞掉它就等于静默丢块。"""
    b = DeepSeekBackend(api_key="k")

    async def _boom(body: dict):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(b, "_post", _boom)
    with pytest.raises(RuntimeError, match="connection reset"):
        await b.complete_turn([{"role": "user", "content": "hi"}],
                              tools=[_TOOL_SCHEMA])
    assert b.errors == 1
    assert b.last_error is not None and "connection reset" in b.last_error
    assert b.calls == 0 and b.prompt_tokens == 0 and b.completion_tokens == 0


async def test_deepseek_complete_json_still_returns_parsed_dict(monkeypatch):
    """回归钉子：加工具路径**不许动** complete_json/complete_text 的语义。
    complete_json 仍返回解析后的 dict（content 字符串 → json.loads）。"""
    b = DeepSeekBackend(api_key="k")
    monkeypatch.setattr(b, "_post",
                        _msg_post({"role": "assistant", "content": '{"urge": 0.4}'}))
    assert await b.complete_json([]) == {"urge": 0.4}
    monkeypatch.setattr(b, "_post",
                        _msg_post({"role": "assistant", "content": "一句台词"}))
    assert await b.complete_text([]) == "一句台词"


# ---- stub 的确定性工具路径：离线工具循环测试的底座 ----

_STUB_TOOL_SCRIPT = [
    {"reasoning_content": "先查查药铺",
     "tool_calls": [_tool_call("call_1", "read_entry", '{"key": "药铺"}')]},
    {"tool_calls": [_tool_call("call_2", "read_entry", '{"key": "陈掌柜"}')]},
    {"content": '{"urge": 0.6, "goal_progress": 0.1}'},   # 终稿：不带 tool_calls
]


async def test_stub_tool_script_yields_tool_turns_then_final_draft():
    """脚本按调用次序吐出：先若干带 tool_calls 的回合，最后不带 tool_calls 的终稿。
    工具循环靠「最后一次没有 tool_calls」判定收敛，这条不稳循环就停不下来。"""
    b = StubBackend(tool_script=_STUB_TOOL_SCRIPT)
    first = await b.complete_turn([{"role": "user", "content": "hi"}],
                                  tools=[_TOOL_SCHEMA])
    assert first["role"] == "assistant"
    assert first["tool_calls"][0]["id"] == "call_1"
    assert first["reasoning_content"] == "先查查药铺"
    second = await b.complete_turn([], tools=[_TOOL_SCHEMA])
    assert second["tool_calls"][0]["id"] == "call_2"
    final = await b.complete_turn([], tools=[_TOOL_SCHEMA])
    assert final["content"] == '{"urge": 0.6, "goal_progress": 0.1}'
    assert "tool_calls" not in final
    # 脚本用尽后钉在终稿上：循环即使多问一次也不会再拿到工具调用（不会无限转）
    assert await b.complete_turn([], tools=[_TOOL_SCHEMA]) == final


async def test_stub_tool_script_is_gated_on_the_tools_argument():
    """`tools` 才是「这一枪要不要工具能力」的开关；`tool_script` 只描述工具轮长什么样。

    与 deepseek 对齐：tools=None/[] 时请求体里根本没有 tools 键，服务端不可能返回
    tool_calls，stub 也必须一个字都不吐。否则离线路径会在用户把信息库**关掉**时照样
    执行查库回合（角色说出检索来的内容），同一份代码上线跑 deepseek 又不会——任何钉
    「关掉信息库不查库」的验收用例都会离线绿、线上错。
    """
    for disabled in (None, []):
        b = StubBackend(tool_script=_STUB_TOOL_SCRIPT)
        out = await b.complete_turn([{"role": "user", "content": "hi"}], tools=disabled)
        assert "tool_calls" not in out and "reasoning_content" not in out
        assert json.loads(out["content"])       # 无工具 = 一次普通调用，产出终稿 JSON
        # 这次无工具调用**不消费**脚本指针：随后真开工具仍从第一条吐起
        first = await b.complete_turn([{"role": "user", "content": "hi"}],
                                      tools=[_TOOL_SCHEMA])
        assert first["tool_calls"][0]["id"] == "call_1"


async def test_stub_tool_script_same_input_sequence_same_output_sequence():
    """完全确定性：同样长度的输入序列 → 逐字节相同的输出序列。
    这是后续所有离线工具循环测试的底座，任何随机/时间/环境依赖都会让它飘。"""
    outs_a, outs_b = [], []
    for sink in (outs_a, outs_b):
        b = StubBackend(tool_script=_STUB_TOOL_SCRIPT)
        for _ in range(5):
            sink.append(await b.complete_turn([{"role": "user", "content": "hi"}],
                                              tools=[_TOOL_SCHEMA]))
    assert outs_a == outs_b
    assert len(outs_a) == 5


async def test_stub_without_tool_script_keeps_legacy_behavior():
    """缺省 tool_script=None = 老行为：演示脚本不调工具（complete_turn 无 tools 时
    走一次普通终稿；有 tools 时按不支持工具处理），且新增参数**不扰动**
    _j/_t 两个老计数器——老离线演示的调用序列与今日逐字节一致。"""
    b = StubBackend(json_script=[{"urge": 0.1}], line_script=["老台词。"])
    assert await b.complete_json([]) == {"urge": 0.1}
    msg = await b.complete_turn([])                 # 无 tools → 一次普通终稿（JSON 文本）
    assert msg == {"role": "assistant", "content": '{"urge": 0.1}'}
    assert await b.complete_text([]) == "老台词。"
    assert b.calls == 3
    with pytest.raises(NotImplementedError):        # 没脚本就没工具能力
        await b.complete_turn([], tools=[_TOOL_SCHEMA])


async def test_stub_tool_script_counters_stay_deterministic():
    """工具路径同样记入桌面端用量（离线近似），且 counts 单调：stub 恒成功、
    不抛错，故 errors 恒 0。缺了记账，界面上的调用数会少掉整个检索轮。"""
    b = StubBackend(tool_script=_STUB_TOOL_SCRIPT)
    await b.complete_turn([{"role": "user", "content": "四字消息"}],
                          tools=[_TOOL_SCHEMA])
    assert b.calls == 1 and b.errors == 0 and b.last_error is None
    assert b.prompt_tokens > 0 and b.completion_tokens > 0
