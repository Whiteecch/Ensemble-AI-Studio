"""graph.py 的信息库接线（设计文档 §6.1 索引注入 / §6.3 工具循环 / §6.4 取用通道）。

钉的都是"改了会静默出错"的地方：

  · **不变量**：`ctx.knowledge is None` 时 think/speak 的调用序列与提示词必须与今天
    **逐字节、逐调用次数**相同——关库/无库角色一次调用都不多花（§6.3 的成本闸门）；
  · 工具循环：模型要工具 → **本地**执行 → 结果以 `role:"tool"` 回喂 → 终稿；预算
    （MAX_TOOL_ROUNDS / MAX_TOOL_CALLS）耗尽时必须停下并仍能拿到终稿；
  · 思考模式的 `reasoning_content` **原样回喂**（不回传就 400，整块直接失败）；
  · 后端不支持工具（NotImplementedError）→ 降级为**带索引、不传 tools** 的一次普通调用；
  · 取用到的正文延续到本块 speak（§6.4 的 recall 通道），且**绝不**进共享态
    （PUBLIC_KEYS 白名单，私人知识进共享态就破了信息边界）。

全程离线：`StubBackend(tool_script=...)` 确定性地吐工具回合，不需要真实模型；
`tmp_path` 独立运行根，不碰真实用户目录。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from harness import knowledgestore as store
from harness.backends.stub import StubBackend
from harness.graph import build_graph
from harness.knowledge import Entry
from harness.knowledgetools import (
    MAX_TOOL_CALLS,
    MAX_TOOL_ROUNDS,
    RECALL_FILENAME,
    KnowledgeAccess,
)
from harness.prompters import build_speak_messages, build_think_messages
from harness.schemas import CharacterCard, Message, Scene
from harness.tests.helpers import assert_public_only, last_state
from harness.visibility import render_view, view_for

SCENE = "贝克街221B"
ME = "甲"
OTHER = "乙"

#: 本场一条真实条目：工具循环读它，speak 也要能"想起"它。
DRUG = Entry(key="药铺", title="药铺", summary="柜台下有个暗格",
             body="柜台下第三块砖是空的，里侧有夹层。")


# --------------------------------------------------------------------- 夹具 --

def _think(urge: float) -> dict:
    """ThinkResult 全六键（schema 锁格式，think 步无条件严格校验）。"""
    return {"aroused": 0.5, "obligation_fulfilled": [], "goal_progress": 0.0,
            "addressed": None, "impression_of_speaker": None, "urge": urge}


def _tool_call(cid: str, name: str, args: str) -> dict:
    """OpenAI 函数调用形状的一条 tool_call（arguments 是 JSON **字符串**）。"""
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": args}}


def _cards(names=(ME,)) -> dict[str, CharacterCard]:
    return {n: CharacterCard(name=n) for n in names}


def _scene(names=(ME,)) -> Scene:
    return Scene(name=SCENE, participants=list(names))


def _seed() -> dict:
    return {"messages": [{"id": 0, "speaker": "导演", "speaker_type": "director",
                          "content": "（贝克街的夜，炉火正旺。）", "in_scene": SCENE,
                          "turn": 0}],
            "urges": {}, "current_speaker": None, "turn": 0, "silent_streak": 0,
            "decided": None, "injected": []}


class _RecordingStub(StubBackend):
    """记录每次调用（方法名 + messages + tools）的 stub，供逐字节日志/调用序列断言。"""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.seen: list[tuple[str, list[dict]]] = []
        self.tools_seen: list[list[dict] | None] = []

    async def complete_json(self, messages: list[dict]) -> dict:
        self.seen.append(("complete_json", [dict(m) for m in messages]))
        return await super().complete_json(messages)

    async def complete_text(self, messages: list[dict]) -> str:
        self.seen.append(("complete_text", [dict(m) for m in messages]))
        return await super().complete_text(messages)

    async def complete_turn(self, messages: list[dict],
                            tools: list[dict] | None = None) -> dict:
        self.seen.append(("complete_turn", [dict(m) for m in messages]))
        self.tools_seen.append(tools)
        return await super().complete_turn(messages, tools)


class _CountingAccess(KnowledgeAccess):
    """记录每一次工具执行的适配器（断言"取用几次"，与 recall 去重无关）。"""

    def __init__(self, *args, **kw) -> None:
        super().__init__(*args, **kw)
        self.executed: list[str] = []

    def execute(self, character: str, name: str, args, *, turn: int) -> str:
        self.executed.append(name)
        return super().execute(character, name, args, turn=turn)


def _access(root: Path, tmp_path: Path, entries=(DRUG,),
            character: str = ME) -> KnowledgeAccess:
    """造一座角色本体库（正式布局 `characters/<角色名>`），返回直连它的适配器。"""
    libs = tmp_path / "libraries"
    lib = store.Library(name=character, scope="character", owner=character)
    for entry in entries:
        lib.upsert(entry)
    store.save_library(lib, store.library_dir("character", character, root=libs))
    return KnowledgeAccess(root, SCENE, libraries_root=libs)


def _recall_rows(root: Path, character: str = ME) -> list[dict]:
    path = root / character / RECALL_FILENAME
    if not path.is_file():
        return []
    return [json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _run(graph, thread: str) -> dict:
    asyncio.run(graph.ainvoke(_seed(), config={
        "configurable": {"thread_id": thread}, "max_concurrency": 4}))
    return asyncio.run(last_state(graph, thread))


# ------------------------------------------------------- 不变量：无库逐字节不变 --

def test_without_knowledge_call_sequence_and_prompts_are_byte_identical(tmp_path):
    """**最重要的一条**：ctx.knowledge is None 时 think/speak 与今天逐字节、逐调用相同。

    同一条场景跑两遍——一遍完全不传 knowledge（今天的老调用方），一遍显式传
    knowledge=None（新参数的老语义）——两次的每一次请求（方法名 + 完整 messages）必须
    逐字节相等；且关库路径**绝不**出现 complete_turn（带 tools 的一枪意味着多一次往返、
    多一次计费）。现有 1239 个测试是这条不变量的证据，这条是把它显式钉住。
    """
    def _run_once(sub: str, **kw) -> tuple[_RecordingStub, _RecordingStub]:
        think = _RecordingStub(json_script=[_think(2.0), _think(0.0)])
        speak = _RecordingStub(line_script=["（占位台词。）"])
        graph = build_graph(_cards((ME, OTHER)), _scene((ME, OTHER)),
                            think, speak, run_root=tmp_path / sub, **kw)
        _run(graph, f"none-{sub}")
        return think, speak

    base_think, base_speak = _run_once("a")
    none_think, none_speak = _run_once("b", knowledge=None)

    assert none_think.seen == base_think.seen      # 含 messages 逐字节
    assert none_speak.seen == base_speak.seen
    assert [name for name, _ in none_think.seen] == ["complete_json", "complete_json"]
    assert [name for name, _ in none_speak.seen] == ["complete_text"]
    assert none_think.tools_seen == []             # 关库：一次带 tools 的调用都没有


def test_without_knowledge_prompts_carry_no_index_or_recall(tmp_path):
    """无库时索引/取用小节**一字不注入**（系统提示与用户消息都不许出现它们的标题）。"""
    think = _RecordingStub(json_script=[_think(2.0)])
    speak = _RecordingStub(line_script=["（占位台词。）"])
    graph = build_graph(_cards(), _scene(), think, speak,
                        run_root=tmp_path / "runs", knowledge=None)
    _run(graph, "none-sections")

    system = think.seen[0][1][0]["content"]
    assert "【你的信息索引】" not in system
    speak_user = speak.seen[0][1][1]["content"]
    assert "【你想起的事】" not in speak_user


#: 老路（无库）这一块的**金标摘要**：think/speak 两次请求的方法名 + 完整 messages 的
#: sha256。它不是"再跑一遍对拍"，而是把提示词的字节冻在文件里——要让它红，必须真的
#: 改动了老路发给模型的每一个字节（措辞、顺序、分隔符、消息条数、role）。
#: 允许（且必须）打断它的场合只有一个：设计文档 §9.1 那类**故意**的提示词变更。
#: （think 那份在 §9.1 撤除【你的已知边界】那一节时更新过一次；speak 那一节本就没有
#: 那个占位，故它的摘要一字未动。）
_OLD_PATH_THINK_DIGEST = ("0c209e15d8df3f737cfbfe767028c16e8f6948ca41f1"
                          "f76e1df610a3cd59ba53")
_OLD_PATH_SPEAK_DIGEST = ("914a6edcd0e57f2f793e64d72141c752dbd7ff66b54e"
                          "7c97ec2461ab4c57ede8")

#: 老路这一块里语言指令非空——让"语言指令的透传"也在守卫范围内（空串会掩盖漏传）。
_OLD_PATH_DIRECTIVE = "你将使用简体中文回答。"

#: 首块的「此刻最新的一句」归属行（开场那条导演消息），与 `_render_last_line` 的约定同形。
_SEED_LAST_LINE = "[0] 导演: （贝克街的夜，炉火正旺。）"

#: think 落盘一条私有状态后，speak 私有回喂（`_render_recent_state` 的渲染）的样子。
#: 写死成字面量是刻意的：它就是"发给模型的那段字节"。
_OLD_PATH_STATE_TEXT = "- 唤醒 0.50 ｜ 目标推进 0.00 ｜ 被点名 无 ｜ 兑现义务 无"


def _digest(seen: list[tuple[str, list[dict]]]) -> str:
    """一次运行的全部请求（方法名 + messages）→ sha256（确定性：不看时钟、不看路径）。"""
    return hashlib.sha256(json.dumps(seen, ensure_ascii=False,
                                     sort_keys=True).encode("utf-8")).hexdigest()


def test_without_knowledge_prompts_are_pinned_against_an_independent_reconstruction(tmp_path):
    """无库老路的提示词必须**逐字节**等于"图之外的独立重建"，并等于一份金标摘要。

    为什么单立一条：上面那条对比的是"不传 knowledge"与"显式传 None"，而两者走的是**同一
    条代码**（参数默认值就是 None）——往老路提示词里加一句、改一处场景措辞，它照样绿，
    证明不了 docstring 里那句"与今天逐字节相同"。这里换两个独立判据：

      · 拿 `prompters` 的 builder（被冻结、只吃参数的纯函数）**亲手重建**这一块该发的
        think/speak 两份消息，与图实际发给后端的逐字节比对——graph 的"参数映射"错一处
        （场景措辞、index_text 不是空串、语言指令漏传、索引挂错消息……）当场红，且能看出
        差在哪；
      · 再钉一份 sha256 金标（见 `_OLD_PATH_*_DIGEST`）：把 role/条数/分隔符/模板字节一并
        冻住，补上"重建与被重建共用一个 builder"这条缝。

    这是把铁律 3 的"提示词那一半"真正架上守卫——它此前是裸的（现有测试只钉住调用次数）。
    """
    think = _RecordingStub(json_script=[_think(2.0)])
    speak = _RecordingStub(line_script=["（占位台词。）"])
    graph = build_graph(_cards(), _scene(), think, speak, run_root=tmp_path / "runs",
                        language_directive=_OLD_PATH_DIRECTIVE)
    _run(graph, "old-path-pinned")

    # ① 独立重建 think：首块还没有任何私有状态/印象（它们由本块的 think 之后才落盘）。
    view = view_for([Message.model_validate(_seed()["messages"][0])], ME, SCENE)
    view_text = render_view(view)
    expected_think = build_think_messages(
        _cards()[ME], view_text=view_text, last_chunk_text=_SEED_LAST_LINE,
        scene_text=f"场景：{SCENE}", own_last=False,
        background_text="", description_text="", plot_text="",
        language_directive=_OLD_PATH_DIRECTIVE, index_text="")
    assert think.seen == [("complete_json", expected_think)]

    # ② 独立重建 speak：状态回喂是本块 think 刚落下的那一条，归属行仍是开场那条（还没人开口）。
    expected_speak = build_speak_messages(
        _cards()[ME], view_text, f"场景：{SCENE}", own_previous=False,
        prev_line_text=_SEED_LAST_LINE, state_text=_OLD_PATH_STATE_TEXT,
        background_text="", description_text="", plot_text="",
        language_directive=_OLD_PATH_DIRECTIVE, index_text="", recall_text="")
    assert speak.seen == [("complete_text", expected_speak)]

    # ③ 金标摘要：字节冻在这里，任何漂移都躲不过（含 builder 自身的改动）。
    assert _digest(think.seen) == _OLD_PATH_THINK_DIGEST
    assert _digest(speak.seen) == _OLD_PATH_SPEAK_DIGEST


# ---------------------------------------------------------------- 工具循环 --

def test_tool_loop_executes_locally_feeds_back_and_parses_final(tmp_path):
    """§6.3 主干：模型要一次工具 → **本地**执行 → 结果以 role:"tool" 回喂 → 终稿。

    断言四件事：
      · 索引进了**系统消息**（§6.1），且只在这一处；
      · 第二轮的 messages 里，上一轮的 assistant 消息**原样**带着 tool_calls 与
        reasoning_content（思考模式的硬要求，见下一条测试）；
      · 工具结果带**对应的 tool_call_id**（OpenAI 的关联键，缺了服务端判请求非法）；
      · 终稿 JSON 被解析成 ThinkResult 并驱动了本块发言。
    """
    root = tmp_path / "runs"
    acc = _access(root, tmp_path)
    think = _RecordingStub(json_script=[_think(2.0)], tool_script=[
        {"reasoning_content": "先查查药铺",
         "tool_calls": [_tool_call("call_1", "read_entry", '{"key": "药铺"}')]},
        {"content": json.dumps(_think(2.0), ensure_ascii=False)},
    ])
    speak = _RecordingStub(line_script=["（那封信就压在第三块砖下面。）"])
    graph = build_graph(_cards(), _scene(), think, speak,
                        run_root=root, knowledge=acc)
    state = _run(graph, "loop-basic")

    assert [name for name, _ in think.seen] == ["complete_turn", "complete_turn"]
    assert think.tools_seen[0] is not None and len(think.tools_seen[0]) == 3
    assert "【你的信息索引】" in think.seen[0][1][0]["content"]

    second = think.seen[1][1]
    echoed = [m for m in second if m.get("tool_calls")]
    assert len(echoed) == 1                       # assistant 回合原样回喂
    assert echoed[0]["tool_calls"][0]["id"] == "call_1"
    assert echoed[0]["reasoning_content"] == "先查查药铺"
    results = [m for m in second if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in results] == ["call_1"]
    assert "柜台下第三块砖是空的" in results[0]["content"]

    # 取用即记录（§6.4）：read_entry 命中时 recall.jsonl 自动落一条
    rows = _recall_rows(root)
    assert [r["key"] for r in rows] == ["药铺"]
    # 终稿驱动了发言：甲确实开口；共享态只留公共字段（§4.1 信息边界）
    assert any(m["speaker"] == ME for m in state["messages"])
    assert_public_only(state)


def test_tool_loop_supports_parallel_calls_in_one_round(tmp_path):
    """一轮里可并行要好几条（§6.5「支持并行工具调用」）：每条各自执行、各自回喂。"""
    root = tmp_path / "runs"
    entries = (DRUG, Entry(key="陈掌柜", title="陈掌柜", summary="跛足",
                           body="陈掌柜跛足，左眉有疤。"))
    acc = _CountingAccess(root, SCENE, libraries_root=_lib_root(tmp_path, entries))
    think = _RecordingStub(json_script=[_think(2.0)], tool_script=[
        {"tool_calls": [_tool_call("c1", "read_entry", '{"key": "药铺"}'),
                        _tool_call("c2", "read_entry", '{"key": "陈掌柜"}')]},
        {"content": json.dumps(_think(2.0), ensure_ascii=False)},
    ])
    speak = _RecordingStub(line_script=["（占位台词。）"])
    graph = build_graph(_cards(), _scene(), think, speak, run_root=root, knowledge=acc)
    _run(graph, "loop-parallel")

    assert acc.executed == ["read_entry", "read_entry"]
    results = [m for m in think.seen[1][1] if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in results] == ["c1", "c2"]
    assert "第三块砖" in results[0]["content"] and "左眉有疤" in results[1]["content"]
    assert [r["key"] for r in _recall_rows(root)] == ["药铺", "陈掌柜"]


def test_tool_round_budget_stops_and_still_yields_a_final_draft(tmp_path):
    """MAX_TOOL_ROUNDS 耗尽即停：最多 3 轮工具往返，之后补一次**不带 tools** 的调用拿终稿。

    脚本永远吐工具调用（stub 用尽后钉在最后一条）——没有闸门的话这就是无限循环。
    预算耗尽后的收尾调用不带 tools，故 stub 走 json_script 出终稿：内容未必是 JSON 的
    工具轮不能直接当终稿解析，这一枪是**必须**的，不是多花。
    """
    root = tmp_path / "runs"
    acc = _CountingAccess(root, SCENE, libraries_root=_lib_root(tmp_path, (DRUG,)))
    tool_round = {"tool_calls": [_tool_call("c", "read_entry", '{"key": "药铺"}')]}
    think = _RecordingStub(json_script=[_think(2.0)],
                           tool_script=[tool_round] * (MAX_TOOL_ROUNDS + 2))
    speak = _RecordingStub(line_script=["（占位台词。）"])
    graph = build_graph(_cards(), _scene(), think, speak, run_root=root, knowledge=acc)
    state = _run(graph, "loop-rounds")

    assert len(acc.executed) == MAX_TOOL_ROUNDS
    turns = [name for name, _ in think.seen if name == "complete_turn"]
    assert len(turns) == MAX_TOOL_ROUNDS + 1        # 3 轮工具往返 + 1 次收尾
    # 最后一枪不带 tools（预算耗尽的收尾；带 tools 的那几轮都没有 response_format）
    assert think.tools_seen[-1] is None
    assert all(t is not None for t in think.tools_seen[:-1])
    assert any(m["speaker"] == ME for m in state["messages"])   # 仍拿到了终稿
    assert_public_only(state)


def test_tool_call_budget_stops_and_keeps_every_call_paired(tmp_path):
    """MAX_TOOL_CALLS 耗尽即停：一轮里要了 10 条也只执行 8 条。

    超额的那几条**也必须回一条 tool 消息**：带了 tool_calls 的 assistant 回合后面缺一条
    对应 tool_call_id 的结果，服务端会判请求非法（400）——省下的那两次取用会换来整块失败。
    """
    root = tmp_path / "runs"
    keys = [f"条目{i}" for i in range(10)]
    entries = tuple(Entry(key=k, title=k, body=f"{k}的正文") for k in keys)
    acc = _CountingAccess(root, SCENE, libraries_root=_lib_root(tmp_path, entries))
    calls = [_tool_call(f"c{i}", "read_entry", json.dumps({"key": k}, ensure_ascii=False))
             for i, k in enumerate(keys)]
    think = _RecordingStub(json_script=[_think(2.0)],
                           tool_script=[{"tool_calls": calls},
                                        {"tool_calls": calls}])
    speak = _RecordingStub(line_script=["（占位台词。）"])
    graph = build_graph(_cards(), _scene(), think, speak, run_root=root, knowledge=acc)
    _run(graph, "loop-calls")

    assert len(acc.executed) == MAX_TOOL_CALLS
    results = [m for m in think.seen[1][1] if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in results] == [f"c{i}" for i in range(10)]
    # 超额的两条拿到的是"这一块查得够多了"，不是执行结果
    assert results[8]["content"] != results[0]["content"]


# ------------------------------------------------------------ 后端兼容与降级 --

@pytest.mark.parametrize("bad_content", [
    "",                                                   # 空串（API 的 null 归一化后）
    "我先看看情况。",                                       # 一句自然语言前言
    "```json\n{\"urge\": 2.0}\n```",                       # 围栏里的 JSON
])
def test_convergence_turn_that_is_not_json_falls_back_to_a_plain_final_call(tmp_path,
                                                                            bad_content):
    """收敛轮（模型没要工具）的 content 不是 JSON → 补一次**不带 tools** 的调用拿终稿。

    带 tools 的请求在落地后端上**没有 response_format**（deepseek._payload：json_mode =
    not tools；base.complete_turn 的调用方契约也写着"终稿 JSON 由调用方在循环收敛后改调
    complete_json 取得"）。旧路 complete_json 有服务端保证"content 必是合法 JSON"，这条路
    没有——思考模式的模型常先给一句自然语言、或把 JSON 包在 ``` 围栏里。直接 json.loads 的
    代价不是这一次失败：graph 不吞异常 → worker 把整块当"模型暂不可用"重跑（**重复计费**），
    而同一提示词的失败是确定性的，场景会在这块上无限空转烧 token。

    补的这一枪**不是多花**：它只在 content 解析不出来时才发生（§6.3 的成本闸门仍成立——
    模型规规矩矩出 JSON 时一次调用都不多）。
    """
    root = tmp_path / "runs"
    acc = _access(root, tmp_path)
    think = _RecordingStub(json_script=[_think(2.0)], tool_script=[
        {"tool_calls": [_tool_call("call_1", "read_entry", '{"key": "药铺"}')]},
        {"content": bad_content},          # 收敛轮：不带 tool_calls，content 却不是 JSON
    ])
    speak = _RecordingStub(line_script=["（占位台词。）"])
    graph = build_graph(_cards(), _scene(), think, speak, run_root=root, knowledge=acc)
    state = _run(graph, "rescue-final")

    assert [name for name, _ in think.seen] == [
        "complete_turn", "complete_turn", "complete_json"]
    assert len(think.tools_seen) == 2                   # 兜底那一枪是 complete_json（无 tools）
    assert think.seen[2][1] == think.seen[0][1]         # 用同一份带索引的提示词重问
    assert any(m["speaker"] == ME for m in state["messages"])   # 终稿照常驱动了本块发言
    assert_public_only(state)


class _BadFinalStub(_RecordingStub):
    """收尾那一枪（**不带 tools**）吐一句自然语言：复现"这一枪的 content 也不是 JSON"。

    真后端上不带 tools 的调用有 response_format，坏 content 罕见；但"从哪一枪来"不该决定
    坏 content 的下场（一个例外就够让整块进入 worker 的无限重试）。故这条假后端故意违反契约。
    """

    async def complete_turn(self, messages: list[dict],
                            tools: list[dict] | None = None) -> dict:
        if tools:
            return await super().complete_turn(messages, tools)
        self.seen.append(("complete_turn", [dict(m) for m in messages]))
        self.tools_seen.append(None)
        return {"role": "assistant", "content": "我想想……"}


def test_tool_round_budget_exhausted_final_is_rescued_the_same_way(tmp_path):
    """预算耗尽后的收尾那一枪同样有兜底：content 还是坏掉时也补一次 complete_json。

    否则同一条失败路径会因为"从哪一枪来"而两种下场：收敛轮能救、收尾轮直抛——而收尾轮
    恰恰是工具循环里最常见的那一枪（预算本来就是要被用掉的）。
    """
    root = tmp_path / "runs"
    acc = _CountingAccess(root, SCENE, libraries_root=_lib_root(tmp_path, (DRUG,)))
    tool_round = {"tool_calls": [_tool_call("c", "read_entry", '{"key": "药铺"}')]}
    think = _BadFinalStub(json_script=[_think(2.0)],
                          tool_script=[tool_round] * MAX_TOOL_ROUNDS)
    speak = _RecordingStub(line_script=["（占位台词。）"])
    graph = build_graph(_cards(), _scene(), think, speak, run_root=root, knowledge=acc)
    state = _run(graph, "rescue-budget")

    assert think.seen[-1][0] == "complete_json"          # 坏终稿被兜底救回
    assert len(acc.executed) == MAX_TOOL_ROUNDS
    assert any(m["speaker"] == ME for m in state["messages"])
    assert_public_only(state)



def test_backend_without_tools_degrades_to_indexed_complete_json(tmp_path):
    """后端不支持原生工具（complete_turn 抛 NotImplementedError）→ 降级为**带索引、
    不传 tools** 的一次 complete_json：索引照给，取用能力没有。

    这是旧后端/离线替身的唯一出路；降级请求必须与老路径逐字节同一形状（就是那份带索引
    的 messages），否则"无库角色提示词不变"那条验收在降级路径上当场失守。
    """
    root = tmp_path / "runs"
    acc = _CountingAccess(root, SCENE, libraries_root=_lib_root(tmp_path, (DRUG,)))
    think = _RecordingStub(json_script=[_think(2.0)])   # 无 tool_script → 不支持工具
    speak = _RecordingStub(line_script=["（占位台词。）"])
    graph = build_graph(_cards(), _scene(), think, speak, run_root=root, knowledge=acc)
    state = _run(graph, "degrade")

    assert [name for name, _ in think.seen] == ["complete_turn", "complete_json"]
    assert acc.executed == []                        # 一次取用都没有
    degraded = think.seen[1][1]
    assert "【你的信息索引】" in degraded[0]["content"]   # 索引照给
    assert degraded == think.seen[0][1]              # 同一份提示词，只是不传 tools
    assert any(m["speaker"] == ME for m in state["messages"])
    assert_public_only(state)


def test_reasoning_content_is_fed_back_verbatim_in_the_next_request(tmp_path):
    """思考模式：带 tools 的后续每一轮都必须原样回传 reasoning_content，否则 400。

    这条单独钉一次是因为它只在**真跑思考模型**时才咬人，离线全绿也看不出来；用假响应里
    的一个魔法字符串断言它确实出现在第二次请求的 messages 里。
    """
    root = tmp_path / "runs"
    acc = _access(root, tmp_path)
    think = _RecordingStub(json_script=[_think(2.0)], tool_script=[
        {"reasoning_content": "REASONING-MARKER-42",
         "tool_calls": [_tool_call("call_1", "read_entry", '{"key": "药铺"}')]},
        {"content": json.dumps(_think(2.0), ensure_ascii=False)},
    ])
    speak = _RecordingStub(line_script=["（占位台词。）"])
    graph = build_graph(_cards(), _scene(), think, speak, run_root=root, knowledge=acc)
    _run(graph, "reasoning")

    echoed = [m for m in think.seen[1][1] if m.get("tool_calls")]
    assert echoed and echoed[0]["reasoning_content"] == "REASONING-MARKER-42"


# ------------------------------------------------------------ 取用通道 → speak --

def test_recall_reaches_the_same_blocks_speak_prompt(tmp_path):
    """§6.4：think 取用到的正文延续到**本块同一角色**的 speak 提示词。

    think 与 speak 是同一块的连续两步；刚想起的东西若不延续，角色回忆完了又失忆，
    那一轮取用的钱白花。通道走文件（recall.jsonl），不进共享态。
    """
    root = tmp_path / "runs"
    acc = _access(root, tmp_path)
    think = _RecordingStub(json_script=[_think(2.0)], tool_script=[
        {"tool_calls": [_tool_call("call_1", "read_entry", '{"key": "药铺"}')]},
        {"content": json.dumps(_think(2.0), ensure_ascii=False)},
    ])
    speak = _RecordingStub(line_script=["（占位台词。）"])
    graph = build_graph(_cards(), _scene(), think, speak, run_root=root, knowledge=acc)
    state = _run(graph, "recall-speak")

    speak_user = speak.seen[0][1][1]["content"]
    assert "【你想起的事】" in speak_user
    assert "柜台下第三块砖是空的" in speak_user
    # 索引同时进 speak 的系统消息（§6.1 同位置）
    assert "【你的信息索引】" in speak.seen[0][1][0]["content"]
    # 私人知识**绝不**进共享态（PUBLIC_KEYS 白名单是信息边界的根基）
    assert_public_only(state)
    assert "第三块砖" not in json.dumps(state["messages"], ensure_ascii=False)


def _lib_root(tmp_path: Path, entries) -> Path:
    """在 tmp_path 下造一座只含给定条目、且**不属于任何角色本体**的库根。

    它只用来给 KnowledgeAccess 一个可读的副本来源；刻意不叫 `_access`，因为
    `_CountingAccess` 要自己持有实例（记录 execute 次数）。
    """
    libs = tmp_path / "libraries"
    lib = store.Library(name=ME, scope="character", owner=ME)
    for entry in entries:
        lib.upsert(entry)
    store.save_library(lib, store.library_dir("character", ME, root=libs))
    return libs
