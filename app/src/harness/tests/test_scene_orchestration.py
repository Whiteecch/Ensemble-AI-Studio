"""场景的剩余职责（《场景编排与桌面外壳》§3.1/§3.4/§4/§5，S4b）。

覆盖四块：
  1. **场景字段进所有提示词**：背景/描述/剧情走向进 think/speak/narrate，语言指令
     （§7）在三处都出现（language="en" → 「你将使用English回答。」）；
  2. **钩子由场景自己判、引擎执行**（§4）：`[[HOOK:<id>]]` 指令的解析与剥离、三类事件
     （上下文/角色/场景事件）、显式 vs 隐式（隐式只进 _implicit_events、不上屏）、
     非法钩子跳过不崩、指令行绝不泄进叙述正文；
  3. **场景自改（tools）+ 额外思考一次**（§4.1③）：description_mutable 才生效，改完把
     新描述再喂回去多思考一次（上限一轮，第二次的工具照改但不再触发第三次）；
  4. **保存/续演/自动保存/重置**（§5）：sidecar 转录与实时消息一致、续演接着演且 id 从
     最大值之后续号、自动保存恰在配置的块边界落盘、重置后视图/思考上下文/数值动态清零。

全部离线确定性：stub 后端（think/speak/narrate 三档）+ 零阈值竞价。
"""
import json
import os
from pathlib import Path

import pytest

from harness import graph as graph_mod
from harness import scenestore
from harness.backends.stub import StubBackend
from harness.engine import SceneEngine
from harness.visibility import view_for

#: 场景的公共信息（§3.1）：三个字段都要进提示词。
_BACKGROUND = "民国年间，城南的一间茶室，窗外常有卖报的小孩跑过。"
_DESCRIPTION = "茶室里有一些桌椅，柜台上摆着一把铜壶。"
_PLOT = "让两人渐渐谈到那封没寄出的信。"


def _urge(urge: float) -> dict:
    return {"aroused": 0.5, "obligation_fulfilled": [], "goal_progress": 0.0,
            "addressed": None, "impression_of_speaker": None, "urge": urge}


def _write_scene(tmp_path: Path, *, hooks: list[dict] | None = None,
                 description_mutable: bool = False) -> Path:
    """写一份含全部新字段的场景 + 两张卡 + 三档模型 + 零阈值竞价。"""
    scene = {
        "name": "茶室", "participants": ["甲", "乙"],
        "date": "民国二十六年三月",
        "background": _BACKGROUND, "description": _DESCRIPTION,
        "description_mutable": bool(description_mutable), "plot_direction": _PLOT,
        "hooks": list(hooks or []),
        "hard_boundary": {"type": "time", "value": "22:00", "desc": "打烊"},
    }
    scene_p = tmp_path / "茶室.json"
    scene_p.write_text(json.dumps(scene, ensure_ascii=False), encoding="utf-8")
    for name in ("甲", "乙"):
        (tmp_path / f"{name}.json").write_text(
            json.dumps({"name": name, "personality": {"描述": name}},
                       ensure_ascii=False), encoding="utf-8")
    (tmp_path / "models.yaml").write_text(
        "think:\n  backend: stub\n  model: stub\n  params: {}\n"
        "speak:\n  backend: stub\n  model: stub\n  params: {}\n"
        "narrate:\n  backend: stub\n  model: stub\n  params: {}\n", encoding="utf-8")
    (tmp_path / "bid.yaml").write_text(
        "interruption_threshold: 0.0\nspeak_threshold: 0.0\nsilence_k: 100000\n",
        encoding="utf-8")
    return scene_p


class _Scripted:
    """记录每次提示词、按次序吐固定产出的 narrate 替身（确定性，不联网）。

    lines 用完后重复最后一条（多块步进时不会 IndexError）。
    """

    def __init__(self, lines: list[str]) -> None:
        self.lines = list(lines)
        self.calls: list[list[dict]] = []

    async def complete_text(self, messages: list[dict]) -> str:
        self.calls.append(messages)
        idx = min(len(self.calls) - 1, len(self.lines) - 1)
        return self.lines[idx]


def _engine(tmp_path: Path, sub: str = "a", *, hooks=None, language: str = "zh-Hans",
            description_mutable: bool = False, narrate_lines=None,
            **kw) -> SceneEngine:
    scene_p = _write_scene(tmp_path, hooks=hooks,
                           description_mutable=description_mutable)
    eng = SceneEngine(scene_p, [tmp_path / "甲.json", tmp_path / "乙.json"],
                      tmp_path / "models.yaml", run_root=tmp_path / sub,
                      bid_path=tmp_path / "bid.yaml", language=language, **kw)
    eng.think_backend = StubBackend(json_script=[_urge(2.0)] * 60)
    eng.speak_backend = StubBackend(line_script=[f"（第{i}句占位台词。）"
                                                 for i in range(60)])
    eng.narrate_backend = (_Scripted(narrate_lines) if narrate_lines is not None
                           else StubBackend(line_script=["（场景没有新动静。）"]))
    return eng


def _narrators(msgs: list[dict]) -> list[dict]:
    return [m for m in msgs if m.get("speaker_type") == "narrator"]


def _contains(msgs: list[dict], text: str) -> list[dict]:
    return [m for m in msgs if (m.get("content") or "") == text]


# ===========================================================================
# 1. 场景字段 + 语言指令进所有提示词（§3.1/§7）
# ===========================================================================
async def test_scene_fields_and_language_reach_all_prompts(tmp_path, monkeypatch):
    """think/speak/narrate 三处都要有【场景背景】【场景描述】【剧情走向】与语言指令。"""
    from harness import prompters
    from harness.prompters import build_narrate_messages as real_narrate
    from harness.prompters import build_speak_messages as real_speak
    from harness.prompters import build_think_messages as real_think

    built: dict[str, list[list[dict]]] = {"think": [], "speak": [], "narrate": []}

    def _think(card, view_text, last_chunk_text, scene_text, **kw):
        msgs = real_think(card, view_text, last_chunk_text, scene_text, **kw)
        built["think"].append(msgs)
        return msgs

    def _speak(card, view_text, scene_text, **kw):
        msgs = real_speak(card, view_text, scene_text, **kw)
        built["speak"].append(msgs)
        return msgs

    def _narrate(view_text, scene_text, clock_text, reason_text, **kw):
        msgs = real_narrate(view_text, scene_text, clock_text, reason_text, **kw)
        built["narrate"].append(msgs)
        return msgs

    monkeypatch.setattr(graph_mod, "build_think_messages", _think)
    monkeypatch.setattr(graph_mod, "build_speak_messages", _speak)
    monkeypatch.setattr(prompters, "build_narrate_messages", _narrate)
    monkeypatch.setattr("harness.engine.build_narrate_messages", _narrate)

    eng = _engine(tmp_path, language="en")
    await eng.open_scene()
    # 两块：首块是「聆听块」（think 完数值 bid 才折进 Dynamics，破冰要 1~2 块），
    # 第二块才有角色台词——think/speak 两档提示词因此都能被观测到。
    await eng.step(2)
    await eng.narrate_now()

    assert built["think"] and built["speak"] and built["narrate"], \
        "三档提示词都应被构过（块内 think/speak + 场景推进）"
    for kind, batches in built.items():
        for msgs in batches:
            text = "\n".join(m["content"] for m in msgs)
            assert _BACKGROUND in text, f"{kind} 提示词缺【场景背景】"
            assert _DESCRIPTION in text, f"{kind} 提示词缺【场景描述】"
            assert _PLOT in text, f"{kind} 提示词缺【剧情走向】"
            assert "你将使用English回答。" in text, f"{kind} 提示词缺语言指令"


async def test_language_switch_takes_effect_without_rebuild(tmp_path):
    """set_language 运行期即时生效：下一块的提示词就换语言（无需重建图/重开场景）。"""
    eng = _engine(tmp_path, language="zh-Hans")
    await eng.open_scene()
    assert eng.language_directive == "你将使用简体中文回答。"

    backend = _Scripted(["（无。）"])
    eng.narrate_backend = backend
    assert eng.set_language("en") is True
    await eng.narrate_now()
    prompt = "\n".join(m["content"] for m in backend.calls[-1])
    assert "你将使用English回答。" in prompt

    assert eng.set_language("不存在的语言") is False
    assert eng.language == "zh-Hans", "不认识的码回落默认语言"


# ===========================================================================
# 2. 钩子：场景判定、引擎执行（§4）
# ===========================================================================
def _context_hook(hook_id: str, *, visible: bool = True) -> dict:
    return {"id": hook_id, "condition": "有人提到那封信", "event_kind": "context",
            "context_text": "窗外忽然有人喊了一声。", "visible": visible}


async def test_context_hook_visible_fires_once_and_stays_out_of_narration(tmp_path):
    """上下文钩子：一条指令 → 恰好一条可见行（knows=None），正文不含指令。"""
    eng = _engine(tmp_path, hooks=[_context_hook("h1")],
                  narrate_lines=["茶凉了，伙计过来添了一壶。\n[[HOOK:h1]]"])
    await eng.open_scene()
    await eng.step(1)
    msgs = await eng.messages()

    lines = _contains(msgs, "窗外忽然有人喊了一声。")
    assert len(lines) == 1, "同一块里同一钩子只执行一次"
    assert lines[0]["speaker"] == "场景" and lines[0]["speaker_type"] == "narrator"
    assert lines[0].get("knows") is None, "上下文事件人人可见"

    contents = [m["content"] for m in _narrators(msgs)]
    assert contents == ["窗外忽然有人喊了一声。", "茶凉了，伙计过来添了一壶。"], \
        "钩子行先落（叙述要体现改动后的局势），正文恰为叙述本身"
    assert all("[[HOOK" not in (m.get("content") or "") for m in msgs), \
        "钩子指令绝不能泄进叙述正文"
    ids = [m["id"] for m in msgs]
    assert len(ids) == len(set(ids)), "钩子行与紧随其后的叙述行不得撞 id"
    assert eng.scene_state()["hook_error"] is None
    assert eng.implicit_events() == []


async def test_context_hook_invisible_only_records_implicit_event(tmp_path):
    """隐式上下文事件（§4.2）：不落任何消息行，只进只读事件流。"""
    eng = _engine(tmp_path, hooks=[_context_hook("h1", visible=False)],
                  narrate_lines=["[[HOOK:h1]]"])
    await eng.open_scene()
    await eng.step(1)
    msgs = await eng.messages()

    assert _contains(msgs, "窗外忽然有人喊了一声。") == []
    events = eng.implicit_events()
    assert len(events) == 1
    assert events[0]["hook_id"] == "h1" and events[0]["visible"] is False
    assert events[0]["content"] == "窗外忽然有人喊了一声。"


async def test_character_hook_removes_character_and_posts_exit_line(tmp_path):
    """角色事件：action=remove 真的把角色移出在场名单，并落一条离场播报行。"""
    eng = _engine(tmp_path, hooks=[{
        "id": "h9", "condition": "甲说要走", "event_kind": "character",
        "character_name": "甲", "action": "remove", "visible": True}],
        narrate_lines=["[[HOOK:h9]]"])
    await eng.open_scene()
    await eng.step(1)

    cast = eng.cast_state()
    assert "甲" not in cast["active"] and "甲" in cast["inactive"]
    assert "甲" not in eng.speakable_names(), "离场者不再参与竞价"
    msgs = await eng.messages()
    assert len(_contains(msgs, "（甲离开了场景。）")) == 1


async def test_scene_hook_patches_live_scene_but_never_a_path(tmp_path):
    """场景事件：改活场景的字段；patch 里的 path/filename 与未知键一律不改。"""
    evil = tmp_path / "被改掉.json"
    eng = _engine(tmp_path, hooks=[{
        "id": "h7", "condition": "茶凉了", "event_kind": "scene",
        "scene_patch": {"description": "新描述：桌椅都翻了。",
                        "path": str(evil), "filename": "被改掉.json",
                        "不知道的字段": "x"},
        "visible": True}], narrate_lines=["[[HOOK:h7]]"])
    scene_before = eng._scene_file()
    await eng.open_scene()
    await eng.step(1)

    assert eng.scene.description == "新描述：桌椅都翻了。"
    assert eng.scene.name == "茶室", "没写在 patch 里的字段不动"
    assert eng._scene_file() == scene_before, "场景路径绝不被钩子改写"
    assert not evil.exists(), "绝不能因为钩子而写出别的文件"
    msgs = await eng.messages()
    assert any(m["content"].startswith("（场景变了：") for m in msgs), \
        "显式场景事件应有一行「场景变了」播报"
    assert any("不知道的字段" in e for e in [eng.scene_state()["hook_error"] or ""]), \
        "不可改的字段要被记进诊断"


async def test_invalid_hook_is_skipped_with_log_and_no_crash(tmp_path):
    """非法钩子（条件为空）：运行期跳过并记原因，绝不炸掉本块。"""
    eng = _engine(tmp_path, hooks=[{
        "id": "bad", "condition": "", "event_kind": "context",
        "context_text": "不该出现。"}], narrate_lines=["[[HOOK:bad]]"])
    await eng.open_scene()
    await eng.step(1)                       # 不抛异常
    msgs = await eng.messages()
    assert _contains(msgs, "不该出现。") == []
    assert eng.scene_state()["hook_error"] and "bad" in eng.scene_state()["hook_error"]
    await eng.step(1)                       # 下一块照常跑


async def test_malformed_and_unknown_hook_directives_are_stripped(tmp_path):
    """畸形指令（少冒号/没结尾）与未知 id：剥离 + 记诊断，正文与消息里都不出现。

    注意本轮的**节奏闸门**：只有畸形指令、**没有任何钩子真的触发**，且这一块仍在冷却内
    → 按 §6.2 的口径（有 hook 也要服从活跃度/冷却）本轮**不落叙述**；正文里的"叙述照常。"
    被一并丢弃。钩子行照旧剥离、诊断照旧记录——本测的要点（剥离与诊断）不受影响。
    """
    eng = _engine(tmp_path, hooks=[_context_hook("h1")],
                  narrate_lines=["叙述照常。\n[[HOOK]]\n[[HOOK:不 合法]]\n[[HOOK"])
    await eng.open_scene()
    await eng.step(1)
    msgs = await eng.messages()

    assert _narration_texts(msgs) == [], \
        "没有钩子触发且在冷却内 → 只咨询不叙述（有 hook 不等于每块都插一句）"
    assert all("[[HOOK" not in (m.get("content") or "") for m in msgs)
    assert "畸形钩子指令" in (eng.scene_state()["hook_error"] or "")
    assert eng.implicit_events() == []


async def test_hook_event_narration_is_not_suppressed_by_the_gate(tmp_path):
    """钩子**真的触发**时，它的伴随叙述不受冷却/活跃度压制（事件驱动 vs 闲聊）。"""
    eng = _engine(tmp_path, hooks=[_context_hook("h1")],
                  narrate_lines=["钟响了，有人起身。\n[[HOOK:h1]]"])
    await eng.open_scene()
    await eng.step(1)                      # 第 1 块仍在 cooldown(3) 内
    assert _narration_texts(await eng.messages()) == ["钟响了，有人起身。"], \
        "触发事件的叙述该照常落行"
    assert len(_contains(await eng.messages(), _HOOK_LINE)) == 1


async def test_hooks_are_consulted_every_block_and_dedupe_within_block(tmp_path):
    """启用钩子后每块都咨询场景（判据/冷却不再是前置）；块内重复报告只执行一次。"""
    eng = _engine(tmp_path, hooks=[_context_hook("h1")],
                  narrate_lines=["[[HOOK:h1]]\n[[HOOK:h1]]"])
    await eng.open_scene()
    backend = eng.narrate_backend
    await eng.step(2)
    assert len(backend.calls) == 2, "两块 → 两次咨询（不受 cooldown 3 块限制）"
    msgs = await eng.messages()
    assert len(_contains(msgs, "窗外忽然有人喊了一声。")) == 2, \
        "每块各执行一次（跨块无状态），块内两行只算一次"


async def test_hooks_do_not_fire_when_auto_narrate_off(tmp_path):
    """自动推进关掉 = 引擎不再自己调场景（钩子随之不自动判定）；手动路径仍带清单。"""
    eng = _engine(tmp_path, hooks=[_context_hook("h1")],
                  narrate_lines=["[[HOOK:h1]]"], auto_narrate=False)
    await eng.open_scene()
    await eng.step(2)
    assert _contains(await eng.messages(), "窗外忽然有人喊了一声。") == []
    await eng.narrate_now()
    assert len(_contains(await eng.messages(), "窗外忽然有人喊了一声。")) == 1


async def test_hooks_prompt_lists_enabled_hooks_and_instructions(tmp_path):
    """提示词侧：钩子清单 + 报告语法（[[HOOK:id]]）都写进 narrate 提示词。"""
    eng = _engine(tmp_path, hooks=[_context_hook("h1"),
                                   {"id": "off", "enabled": False, "condition": "x",
                                    "event_kind": "context", "context_text": "y"}],
                  narrate_lines=["（无事。）"])
    await eng.open_scene()
    await eng.narrate_now()
    prompt = "\n".join(m["content"] for m in eng.narrate_backend.calls[-1])
    assert "【场景钩子】" in prompt and "[h1]" in prompt
    assert "[[HOOK:h1]]" in prompt, "必须把报告语法演给模型看"
    assert "有人提到那封信" in prompt and "窗外忽然有人喊了一声。" in prompt
    assert "[off]" not in prompt, "停用的钩子不进提示词"


# ===========================================================================
# 3. 场景自改（tools）+ 额外思考一次（§4.1③）
# ===========================================================================
async def test_tools_patch_scene_and_trigger_one_extra_think(tmp_path):
    """description_mutable：改活场景 + 再思考一次，用第二次产出当叙述（含新描述）。"""
    eng = _engine(tmp_path, description_mutable=True, narrate_lines=[
        "桌椅还是老样子。\n[[TOOL:set_description]] 破碎的桌椅，到处都是战斗后的痕迹",
        "桌椅碎了一地，茶渍顺着桌腿往下淌。"])
    backend = eng.narrate_backend
    await eng.open_scene()
    msg = await eng.narrate_now()

    assert len(backend.calls) == 2, "改过描述 → 必须额外思考一次（恰好一次）"
    assert eng.scene.description == "破碎的桌椅，到处都是战斗后的痕迹"
    assert msg["content"] == "桌椅碎了一地，茶渍顺着桌腿往下淌。", \
        "叙述取第二次产出（它已把新描述纳入考量）"
    msgs = await eng.messages()
    assert all("[[TOOL" not in (m.get("content") or "") for m in msgs)
    # 第二次的提示词里必须是**新**描述（额外思考的意义所在）
    second = "\n".join(m["content"] for m in backend.calls[1])
    assert "破碎的桌椅" in second and _DESCRIPTION not in second


async def test_second_round_tools_apply_but_never_trigger_third_think(tmp_path):
    """上限一轮：第二次产出里的工具照改，但绝不再触发第三次思考。"""
    eng = _engine(tmp_path, description_mutable=True, narrate_lines=[
        "叙述一。\n[[TOOL:set_description]] 第一次改写",
        "叙述二。\n[[TOOL:set_description]] 第二次改写"])
    backend = eng.narrate_backend
    await eng.open_scene()
    msg = await eng.narrate_now()

    assert len(backend.calls) == 2, "绝不出现第三次调用（防模型自我循环）"
    assert eng.scene.description == "第二次改写"
    assert msg["content"] == "叙述二。"


async def test_tools_ignored_when_description_not_mutable(tmp_path):
    """description_mutable=False：工具指令被忽略（不调用第二次），叙述文本照旧。"""
    eng = _engine(tmp_path, description_mutable=False, narrate_lines=[
        "叙述就好。\n[[TOOL:set_description]] 不该生效",
        "第二次（不该被调用）。"])
    backend = eng.narrate_backend
    await eng.open_scene()
    msg = await eng.narrate_now()

    assert len(backend.calls) == 1, "不可改变 → 不额外思考"
    assert eng.scene.description == _DESCRIPTION
    assert msg["content"] == "叙述就好。"
    assert "description_mutable" in (eng.scene_state()["tool_error"] or "")


# ===========================================================================
# 4. 保存 / 续演 / 自动保存 / 重置（§5）
# ===========================================================================
async def test_save_scene_state_writes_sidecar_matching_live_messages(tmp_path):
    """保存：sidecar 落在场景文件旁边，转录 = 未撤销的实时消息（含虚拟钟/块数/人事）。"""
    eng = _engine(tmp_path)
    scene_p = eng._scene_file()
    await eng.open_scene()
    await eng.step(2)
    eng.set_clock_now(21 * 3600 + 40 * 60)
    await eng.mute_character("乙", 0)

    path = await eng.save_scene_state()
    assert path is not None and path == scenestore.runtime_path_for(scene_p)
    assert path.is_file()
    bundle = scenestore.load_runtime(scene_p)
    live = graph_mod._live_messages(await eng._snapshot())
    assert bundle.transcript == live, "存档转录必须与实时（未撤销）消息逐条一致"
    assert bundle.clock_seconds == 21 * 3600 + 40 * 60
    assert bundle.blocks == (await eng._snapshot())["blocks"]
    assert bundle.meta == {"active": ["甲", "乙"], "muted": {"乙": None}}
    assert eng.scene_state()["save_error"] is None
    # 保存绝不改动场景配置本体（sidecar 是旁挂的）
    assert json.loads(scene_p.read_text(encoding="utf-8"))["description"] == _DESCRIPTION


async def test_save_failure_is_swallowed(tmp_path):
    """写盘失败只记诊断、返回 None：存档失败绝不带垮对话。"""
    eng = _engine(tmp_path)
    await eng.open_scene()
    # 把存档坐标指到「父路径是个文件」的位置：建目录必失败（跨平台都编不出这个路径）。
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    eng._scene_path = blocker / "茶室.json"
    assert await eng.save_scene_state() is None
    assert eng.scene_state()["save_error"]
    await eng.step(1)                       # 引擎照常继续


async def test_restore_appends_transcript_and_continues_ids(tmp_path):
    """续演：新引擎恢复历史（原 id 保留），下一条消息 id 从最大值之后续号。"""
    eng = _engine(tmp_path, sub="a")
    await eng.open_scene()
    await eng.step(2)
    assert await eng.save_scene_state() is not None
    live = graph_mod._live_messages(await eng._snapshot())
    assert len(live) >= 2

    fresh = _engine(tmp_path, sub="b")
    fresh.think_backend = StubBackend(json_script=[_urge(2.0)] * 60)
    assert await fresh.restore_scene_state() is True
    msgs = await fresh.messages()
    assert msgs == live, "恢复 = 把存档转录原样追回共享态（原 id 一字不变）"

    max_id = max(m["id"] for m in msgs)
    await fresh.say("喂。")                 # 新消息 id 必须续在最大 id 之后
    tail = (await fresh.messages())[-1]
    assert tail["id"] == max_id + 1

    scenestore.clear_runtime(eng._scene_file())
    assert await _engine(tmp_path, sub="c").restore_scene_state() is False, \
        "没有存档 → 没恢复任何东西"


async def test_autosave_fires_exactly_at_configured_boundary(tmp_path):
    """自动保存周期 3：第 2 块后还没有 sidecar，第 3 块后恰好有（块边界触发）。"""
    eng = _engine(tmp_path)
    scene_p = eng._scene_file()
    eng.set_autosave_every(3)
    await eng.open_scene()
    await eng.step(2)
    assert not scenestore.has_runtime(scene_p), "未到周期不落盘"
    await eng.step(1)
    assert scenestore.has_runtime(scene_p), "到第 3 块（边界）落盘"
    bundle = scenestore.load_runtime(scene_p)
    assert bundle.blocks == 3
    assert bundle.transcript, "存档应含这一场的转录"


async def test_autosave_disabled_by_default(tmp_path):
    """缺省不自动保存（every<=0 关闭）；set_autosave_every(0) 同样关闭。"""
    eng = _engine(tmp_path)
    await eng.open_scene()
    await eng.step(2)
    assert not scenestore.has_runtime(eng._scene_file())
    eng.set_autosave_every(0)
    await eng.step(4)
    assert not scenestore.has_runtime(eng._scene_file())


async def test_reset_clears_view_think_log_dynamics_and_keeps_sidecar(tmp_path):
    """重置：视图空、思考上下文（think 日志）空、数值动态重新播种；sidecar 不删。"""
    eng = _engine(tmp_path)
    scene_p = eng._scene_file()
    await eng.open_scene()
    await eng.step(2)
    await eng.save_scene_state()
    assert eng.think_log_tail(), "重置前应有 think 记录"
    assert scenestore.has_runtime(scene_p)

    await eng.reset_scene_runtime()

    st = await eng._snapshot()
    live = graph_mod._live_messages(st)
    assert live == [], "重滞后没有一条存活消息"
    assert view_for([], "甲", "茶室") == [], "任何角色的视图都空"
    for mid in [m["id"] for m in st["messages"]]:
        assert mid in st["retracted"], "旧消息全部记进 retracted（界面据此摘行）"
    assert eng.think_log_tail() == [], "思考上下文清零"
    assert all(v["turns_since_spoke"] == 0
               for v in eng.dynamics_snapshot().values()), "数值动态重新播种"
    assert eng.narration_state()["blocks_since"] == 0
    assert eng.implicit_events() == []
    assert scenestore.has_runtime(scene_p), "重置不删存档（§5：清的是运行上下文）"
    await eng.step(1)                       # 重置后世界照常继续


# ===========================================================================
# 5. worker 层（保存/重置/语言/自动保存周期）
# ===========================================================================
pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from harness.gui.worker import SceneWorker  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _wait_until(qapp, cond, timeout_ms: int = 4000, step_ms: int = 10) -> bool:
    loop = QEventLoop()
    ok = {"v": False}
    timer = QTimer()
    timer.setInterval(step_ms)

    def _tick():
        if cond():
            ok["v"] = True
            timer.stop()
            loop.quit()

    timer.timeout.connect(_tick)
    timer.start()
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    timer.stop()
    return ok["v"]


class _FakeEngine:
    """最小替身引擎：只实现 worker 的存档/重置/设置路径 + flush 会读的只读接口。"""

    def __init__(self) -> None:
        self.saved = 0
        self.saved_file = 0
        self.reset = 0
        self.restored = 0
        self.languages: list[str] = []
        self.autosave: list[int] = []
        self._msgs = [{"id": 0, "speaker": "导演", "speaker_type": "director",
                       "content": "（开场。）", "in_scene": "茶室", "turn": 0},
                      {"id": 1, "speaker": "甲", "speaker_type": "character",
                       "content": "（喂。）", "in_scene": "茶室", "turn": 0}]

    async def messages(self):
        return list(self._msgs)

    def cast_state(self):
        return {"active": ["甲"], "inactive": [], "muted": {}}

    def dynamics_snapshot(self):
        return {"甲": {"bid": 0.0}}

    def dynamic_states(self):
        return {}

    def think_log_tail(self, n: int = 200):
        return []

    async def save_scene_state(self):
        self.saved += 1
        return Path("/tmp/茶室.json.runtime.json")

    async def save_scene_file(self):
        """「保存场景」= sidecar + 场景文件（阵容写回，T1）；替身引擎只需记账。"""
        self.saved_file += 1
        return Path("/tmp/茶室.json")

    async def reset_scene_runtime(self):
        self.reset += 1
        self._msgs = []

    def set_language(self, code: str) -> None:
        self.languages.append(code)

    def set_autosave_every(self, n: int) -> None:
        self.autosave.append(n)

    async def restore_scene_state(self) -> bool:
        self.restored += 1
        self._msgs.append({"id": 9, "speaker": "甲", "speaker_type": "character",
                           "content": "（续演的旧话。）", "in_scene": "茶室", "turn": 3})
        return True


@pytest.fixture
def worker_with_engine(qapp):
    worker = SceneWorker()
    worker.start()
    assert worker._ready.wait(5.0), "worker 事件循环未起"
    engine = _FakeEngine()
    saved: list[str] = []
    retracted: list[int] = []
    casts: list[dict] = []
    worker.sig_saved.connect(saved.append)
    worker.sig_retracted.connect(retracted.append)
    worker.sig_cast.connect(casts.append)
    worker._engine = engine
    try:
        yield worker, engine, saved, retracted, casts
    finally:
        worker.shutdown(4000)


def test_worker_save_scene_emits_saved_path(worker_with_engine, qapp):
    """save_scene：sidecar + 场景文件都写 → flush 广播 sig_cast → 成功发 sig_saved(path)。"""
    worker, engine, saved, _, casts = worker_with_engine
    worker.save_scene()
    assert _wait_until(qapp, lambda: bool(saved)), "应发 sig_saved"
    assert engine.saved == 1, "sidecar 写一次"
    assert engine.saved_file == 1, "阵容写回场景文件一次（§3.1）"
    assert saved[0].endswith("茶室.json.runtime.json")
    assert casts, "保存后应刷新一次演员表（flush 顺带）"


def test_worker_reset_scene_retracts_every_message(worker_with_engine, qapp):
    """reset_scene：清引擎上下文 + 逐 id 发 sig_retracted（界面据此摘空对白区）。"""
    worker, engine, _, retracted, _ = worker_with_engine
    worker.reset_scene()
    assert _wait_until(qapp, lambda: bool(retracted))
    assert engine.reset == 1
    assert sorted(retracted) == [0, 1]


def test_worker_restore_scene_flushes_history(worker_with_engine, qapp):
    """restore_scene：引擎追回的历史逐条经 sig_message 上屏（续演接着演）。"""
    worker, engine, _, _, _ = worker_with_engine
    msgs: list[dict] = []
    worker.sig_message.connect(msgs.append)
    worker.restore_scene()
    assert _wait_until(qapp, lambda: any("续演的旧话" in (m.get("content") or "")
                                         for m in msgs))
    assert engine.restored == 1


def test_worker_language_and_autosave_pass_through(worker_with_engine, qapp):
    """set_language / set_autosave_every 落到活引擎；建引擎前设的期望值也不丢。"""
    worker, engine, _, _, _ = worker_with_engine
    worker.set_language("ja")
    worker.set_autosave_every(20)
    assert _wait_until(qapp, lambda: engine.languages == ["ja"])
    assert _wait_until(qapp, lambda: engine.autosave == [20])
    assert worker._language == "ja" and worker._autosave_every == 20

    bare = SceneWorker()                    # 未起线程：只记期望值，绝不抛
    bare.set_language("ko")
    bare.set_autosave_every(5)
    bare.save_scene()
    bare.reset_scene()
    assert bare._language == "ko" and bare._autosave_every == 5


# ===========================================================================
# 5. 推进会话的频率（§6.2）：咨询 ≠ 叙述，且活跃度可调
# ===========================================================================
#: 复读成灾的那一句（人人念同一句 → repeat 分量拉满）。
_SAME_LINE = "他胳膊在流血，先包一下，包完我们跟你们走。"
#: 上下文钩子落下的可见行（叙述计数要把它排除：它同样是 speaker_type=narrator）。
_HOOK_LINE = "窗外忽然有人喊了一声。"


def _narration_texts(msgs: list[dict]) -> list[str]:
    """消息里的**叙述正文**（排除钩子落下的通知行——它也是 narrator 类型）。"""
    return [m["content"] for m in _narrators(msgs) if m["content"] != _HOOK_LINE]


async def test_hook_only_output_never_appends_a_narration(tmp_path):
    """有钩子的场景「每块只报钩子」→ 每块照旧咨询，但**一条叙述都不落**。

    改前：启用钩子走的就是 `_narrate(consult_hooks=True)`，而提示词每轮都要求"写一到
    两行"，于是每块都出一条叙述（实况抱怨的"某些场景发言频率格外高"）。现在落不落行
    由场景自己的产出决定：parse_narration().text 为空 = 这一轮不叙述。"""
    eng = _engine(tmp_path, hooks=[_context_hook("h1")],
                  narrate_lines=["[[HOOK:h1]]"])
    await eng.open_scene()
    backend = eng.narrate_backend
    await eng.step(3)

    msgs = await eng.messages()
    assert len(backend.calls) == 3, "咨询照旧每块进行（判钩子是每块的活）"
    assert _narration_texts(msgs) == [], "没有正文 → 一条叙述都不落"
    assert len(_contains(msgs, _HOOK_LINE)) == 3, "钩子照常每块执行"


async def test_hook_output_with_text_appends_exactly_one_narration(tmp_path):
    """场景给出正文（且带钩子指令）→ 恰落一条叙述；下一轮没正文就不再落。"""
    eng = _engine(tmp_path, hooks=[_context_hook("h1")],
                  narrate_lines=["茶凉了，伙计过来添了一壶。\n[[HOOK:h1]]", "", ""])
    await eng.open_scene()
    await eng.step(1)
    msgs = await eng.messages()
    assert _narration_texts(msgs) == ["茶凉了，伙计过来添了一壶。"]
    assert len(_contains(msgs, _HOOK_LINE)) == 1, "钩子与叙述同一次调用里都生效"

    await eng.step(2)                      # 后两块场景什么都没写
    msgs = await eng.messages()
    assert _narration_texts(msgs) == ["茶凉了，伙计过来添了一壶。"], \
        "没有正文的咨询不落行（不会每块都出一条叙述）"


async def test_hook_scene_cooldown_does_not_block_consultation(tmp_path):
    """冷却期照旧咨询（判钩子不受冷却限制），但正文只在场景自己写出来时才落。"""
    eng = _engine(tmp_path, hooks=[_context_hook("h1")],
                  narrate_lines=["叙述一。\n[[HOOK:h1]]", "[[HOOK:h1]]",
                                 "叙述三。\n[[HOOK:h1]]"])
    await eng.open_scene()
    await eng.step(3)                      # cooldown_blocks=3：这三块都在冷却内
    assert _narration_texts(await eng.messages()) == ["叙述一。", "叙述三。"], \
        "冷却不再约束钩子场景的咨询；落不落行只看场景有没有给正文"


async def test_activity_reaches_the_scene_prompt(tmp_path):
    """活跃度作为提示写进场景提示词（档位文案随档位变），无钩子时不提 HOOK 语法。"""
    eng = _engine(tmp_path, narrate_lines=["（无。）"])
    await eng.open_scene()
    eng.set_narrate_activity(1.0)
    await eng.narrate_now()
    prompt = "\n".join(m["content"] for m in eng.narrate_backend.calls[-1])
    assert "【推进活跃度】" in prompt and "极高" in prompt and "1.00" in prompt
    assert "[[HOOK" not in prompt, "没有钩子就不提钩子语法"

    eng.set_narrate_activity(0.2)
    await eng.narrate_now()
    prompt = "\n".join(m["content"] for m in eng.narrate_backend.calls[-1])
    assert "低" in prompt and "0.20" in prompt and "尽量少介入" in prompt


async def test_activity_scales_trigger_line_and_cooldown(tmp_path):
    """同一场复读成灾的戏：活跃度极高 → 更早开口（冷却短）；活跃度低 → 憋更久。"""
    hi_dir = tmp_path / "hi"
    hi_dir.mkdir()
    hi = _engine(hi_dir, narrate_lines=["（推。）"], narrate_activity=1.0)
    hi.speak_backend = StubBackend(line_script=[_SAME_LINE])       # 人人复读同一句
    await hi.open_scene()
    await hi.step(2)                       # 首块聆听、第二块才有一句台词（复读还起不来）
    assert _narration_texts(await hi.messages()) == []
    await hi.step(1)                       # 第 3 块：复读成立 + 冷却（缩到 1）已过 → 开口
    assert _narration_texts(await hi.messages()) == ["（推。）"], \
        "a=1.0：冷却缩到 1 块，复读一成立就开口"

    lo_dir = tmp_path / "lo"
    lo_dir.mkdir()
    lo = _engine(lo_dir, narrate_lines=["（推。）"], narrate_activity=0.0)
    lo.speak_backend = StubBackend(line_script=[_SAME_LINE])
    await lo.open_scene()
    await lo.step(4)
    assert _narration_texts(await lo.messages()) == [], "a=0.0：冷却拉到 5 块，前 4 块都不开口"
    await lo.step(1)
    assert _narration_texts(await lo.messages()) == ["（推。）"], "第 5 块才到点"


def test_activity_default_is_identity_for_nudge_params(tmp_path):
    """缺省活跃度 0.5 → 判据参数与标定值逐字段相同（既有场景行为不漂移）。"""
    from harness.scenarist import SceneNudgeParams, effective_params
    eng = _engine(tmp_path)
    assert eng._narrate_activity == 0.5
    assert eng.narration_state()["activity"] == 0.5
    assert eng._effective_nudge_params() == SceneNudgeParams()
    eng.set_narrate_activity(9)            # 越界夹到 1.0
    assert eng._narrate_activity == 1.0
    assert eng._effective_nudge_params() == effective_params(SceneNudgeParams(), 1.0)


# ===========================================================================
# 5. 时间条件闸门（引擎确定性校验）
# ===========================================================================
# 实况 bug（本节的锁）：场景 阅览室 的钩子 h3/h3-2 条件是「虚拟钟走到 21:30」
# → 戊/丁离场。用户在 ~19:03 加人，两人**立刻走了**——提前 2.5 小时。根因：
# 钩子条件只由场景 LLM 判（引擎给 `_fire_hooks(hook_ids)` 什么就执行什么），引擎把当
# 前钟点喂进提示词（【当前时刻】）却从不自己校验时间条件，模型误判即照执行。
# 修法：执行前用 scenarist.time_condition_target 做一次确定性校验——条件明确在说
# 虚拟钟、而当前钟点还没到，就跳过这条钩子并把原因写进诊断（GUI 可见）。
_CLOCK_1900 = 19 * 3600                 # 19:00 开场
_CLOCK_1903 = 19 * 3600 + 3 * 60        # 19:03：实况里加人的时刻
_CLOCK_2130 = 21 * 3600 + 30 * 60       # 21:30：条件真正到点

_LEAVE_HOOKS = [
    {"id": "h3", "condition": "虚拟钟走到 21:30", "event_kind": "character",
     "character_name": "戊", "action": "remove", "visible": True},
    {"id": "h3-2", "condition": "虚拟钟走到 21:30", "event_kind": "character",
     "character_name": "丁", "action": "remove", "visible": True},
]
_ASK_HOOK = {"id": "h5", "condition": "有人问起丙的过去", "event_kind": "context",
             "context_text": "丙沉默了一下。", "visible": True}


def _write_time_scene(tmp_path: Path, *, hooks: list[dict],
                      start_time: str = "19:00") -> Path:
    """阅览室 的最小等价场景：两名在场角色 + 时间条件钩子 + 19:00 开场。"""
    scene = {
        "name": "阅览室", "participants": ["戊", "丁"],
        "background": "封闭管理的寄宿中学，未经允许不能出校。",
        "description": "一间阅览室，四张桌椅围在一起。",
        "description_mutable": False, "plot_direction": "",
        "hooks": list(hooks), "start_time": start_time,
        "hard_boundary": {"type": "time", "value": "22:30", "desc": "熄灯"},
    }
    scene_p = tmp_path / "阅览室.json"
    scene_p.write_text(json.dumps(scene, ensure_ascii=False), encoding="utf-8")
    for name in ("戊", "丁"):
        (tmp_path / f"{name}.json").write_text(
            json.dumps({"name": name, "personality": {"描述": name}},
                       ensure_ascii=False), encoding="utf-8")
    (tmp_path / "models.yaml").write_text(
        "think:\n  backend: stub\n  model: stub\n  params: {}\n"
        "speak:\n  backend: stub\n  model: stub\n  params: {}\n"
        "narrate:\n  backend: stub\n  model: stub\n  params: {}\n", encoding="utf-8")
    (tmp_path / "bid.yaml").write_text(
        "interruption_threshold: 0.0\nspeak_threshold: 0.0\nsilence_k: 100000\n",
        encoding="utf-8")
    return scene_p


def _time_engine(tmp_path: Path, sub: str = "t", *, hooks=None, narrate_lines=None,
                 **kw) -> SceneEngine:
    scene_p = _write_time_scene(tmp_path, hooks=hooks or [])
    eng = SceneEngine(scene_p, [tmp_path / "戊.json", tmp_path / "丁.json"],
                      tmp_path / "models.yaml", run_root=tmp_path / sub,
                      bid_path=tmp_path / "bid.yaml", **kw)
    eng.think_backend = StubBackend(json_script=[_urge(2.0)] * 60)
    eng.speak_backend = StubBackend(line_script=[f"（第{i}句占位台词。）"
                                                 for i in range(60)])
    eng.narrate_backend = (_Scripted(narrate_lines) if narrate_lines is not None
                           else StubBackend(line_script=["（场景没有新动静。）"]))
    return eng


def _exit_lines(msgs: list[dict]) -> list[str]:
    return [m["content"] for m in msgs if "离开了场景" in (m.get("content") or "")]


async def test_time_hook_does_not_fire_before_its_clock(tmp_path):
    """复现并锁死实况 bug：19:03 时场景误报 [[HOOK:h3]]（条件 21:30）→ 两人必须留下。

    到点（21:30）后闸门放行，两条离场播报照常落下——修复只拦「提前」，不拦「正常」。"""
    eng = _time_engine(tmp_path, hooks=_LEAVE_HOOKS,
                       narrate_lines=["[[HOOK:h3]]\n[[HOOK:h3-2]]"])
    eng.set_clock_now(_CLOCK_1903)
    await eng.open_scene()
    await eng.step(1)

    cast = eng.cast_state()
    assert cast["active"] == ["戊", "丁"], "没到点，谁都不许走"
    assert cast["inactive"] == []
    msgs = await eng.messages()
    assert _exit_lines(msgs) == [], "提前离场的播报行绝不能落"
    diag = eng.scene_state()["hook_error"] or ""
    assert "h3" in diag and "21:30" in diag and "19:03" in diag and "未到点" in diag, \
        f"必须留下可解释的中文诊断（界面在 scene_state 里读它）：{diag}"

    eng.set_clock_now(_CLOCK_2130)          # 钟真的走到 21:30
    await eng.step(1)
    cast = eng.cast_state()
    assert cast["active"] == [] and set(cast["inactive"]) == {"戊", "丁"}, \
        "到点后照常离场（闸门只拦提前）"
    msgs = await eng.messages()
    assert sorted(_exit_lines(msgs)) == sorted(["（戊离开了场景。）", "（丁离开了场景。）"])
    assert eng.scene_state()["hook_error"] is None, "到点执行不该留下诊断"


async def test_time_hook_skips_are_reachable_from_scene_state(tmp_path):
    """跳过原因必须能经 scene_state() 读到（GUI 那条通道），且说明「为什么 21:30 还没动静」。"""
    eng = _time_engine(tmp_path, hooks=_LEAVE_HOOKS, narrate_lines=["[[HOOK:h3]]"])
    eng.set_clock_now(_CLOCK_1903)
    await eng.open_scene()
    await eng.step(1)
    state = eng.scene_state()
    assert state["hook_error"] and "已跳过" in state["hook_error"]
    assert set(state["hooks"]) == {"h3", "h3-2"}, "钩子本身仍在启用清单里（只是这次没到点）"


async def test_non_time_hook_fires_regardless_of_the_clock(tmp_path):
    """非时间条件（没有钟点）不受闸门影响：任何时刻报了就执行。"""
    eng = _time_engine(tmp_path, hooks=[_ASK_HOOK], narrate_lines=["[[HOOK:h5]]"])
    eng.set_clock_now(_CLOCK_1903)
    await eng.open_scene()
    await eng.step(1)
    assert len(_contains(await eng.messages(), "丙沉默了一下。")) == 1
    assert eng.scene_state()["hook_error"] is None


async def test_time_hook_earlier_than_scene_start_is_treated_as_next_day(tmp_path):
    """条件钟点早于开场时刻（19:00 开场、条件 00:30）→ 与打烊边界同口径算次日。"""
    hook = {"id": "h0", "condition": "虚拟钟走到 00:30", "event_kind": "context",
            "context_text": "凌晨了，教学楼锁了门。", "visible": True}
    eng = _time_engine(tmp_path, hooks=[hook], narrate_lines=["[[HOOK:h0]]"])
    eng.set_clock_now(20 * 3600)            # 当晚 20:00：次日的 00:30 还没到
    await eng.open_scene()
    await eng.step(1)
    assert _contains(await eng.messages(), "凌晨了，教学楼锁了门。") == []
    assert "未到点" in (eng.scene_state()["hook_error"] or "")

    eng.set_clock_now(86400 + 30 * 60)      # 次日 00:30
    await eng.step(1)
    assert len(_contains(await eng.messages(), "凌晨了，教学楼锁了门。")) == 1


async def test_unknown_clock_is_not_skipped_and_noted_once_per_block(tmp_path):
    """钟未注入（_clock_now is None）→ 无证据不拦（旧行为），但诊断里留一句且只留一句。"""
    eng = _time_engine(tmp_path, hooks=_LEAVE_HOOKS,
                       narrate_lines=["[[HOOK:h3]]\n[[HOOK:h3-2]]"])
    await eng.open_scene()                  # 从未 set_clock_now
    await eng.step(1)
    assert eng.cast_state()["active"] == [], "无钟可依 → 不凭猜测拦截"
    diag = eng.scene_state()["hook_error"] or ""
    assert diag.count("钟点未知") == 1, f"同一块里只留一句（两条钩子也只提一次）：{diag}"


async def test_manual_narrate_now_uses_the_same_time_gate(tmp_path):
    """手动「推进一下」路径与自动路径同一口径（否则手动一按就能提前引爆）。"""
    eng = _time_engine(tmp_path, hooks=_LEAVE_HOOKS, narrate_lines=["[[HOOK:h3]]"],
                       auto_narrate=False)
    eng.set_clock_now(_CLOCK_1903)
    await eng.open_scene()
    await eng.narrate_now()
    assert "戊" in eng.cast_state()["active"]
    assert "未到点" in (eng.scene_state()["hook_error"] or "")

    eng.set_clock_now(_CLOCK_2130)
    await eng.narrate_now()
    assert "戊" not in eng.cast_state()["active"], "到点后手动推进照常执行"


async def test_scene_prompt_states_the_time_condition_contract(tmp_path):
    """提示词侧：把「按【当前时刻】严格判断、没到点就别报」讲给场景听（补强而非替换）。"""
    eng = _time_engine(tmp_path, hooks=_LEAVE_HOOKS, narrate_lines=["（无。）"])
    await eng.open_scene()
    await eng.narrate_now()
    system = eng.narrate_backend.calls[-1][0]["content"]
    assert "【当前时刻】" in system, "必须指明当前时刻在哪一节里给出"
    assert "严格" in system and "没到" in system and "不要报告" in system, \
        "必须说明没到点就不许报告该钩子"
    assert "[h3]" in system and "[[HOOK:" in system, \
        "补强不替换：既有钩子清单与报告语法照旧"
