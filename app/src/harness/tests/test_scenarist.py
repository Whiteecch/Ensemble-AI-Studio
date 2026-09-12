"""场景（叙述者）触发算法：复读/停滞/沉默/临近打烊 四个分量 + cooldown + reasons。

实况抱怨（真实转录）：四名角色连着好几轮重复同一个 beat（「他胳膊在流血，先包一下，
包完我们跟你们走」），没人能推进世界——推进世界不是角色的职责。本文件锁定：这种
集体复读必须触发「场景开口」，而正常的有来有回不该被推着走。
"""
import pytest

from harness import scenarist
from harness.scenarist import (TOOL_CLOSE, TOOL_OPEN, ParsedNarration, SceneNudgeParams,
                               SceneToolCall, apply_tools, evaluate, parse_narration,
                               render_tool_call, tools_prompt_block)


def _line(content: str, speaker: str = "甲", kind: str = "character") -> dict:
    return {"id": 1, "speaker": speaker, "speaker_type": kind, "content": content,
            "in_scene": "贝克街221B", "turn": 0}


#: 真实转录里那句的四个改写版（四人各说一遍同一个 beat）。
_REPEAT_LINES = [
    "他胳膊在流血，先包一下，包完我们跟你们走。",
    "得先给他包上胳膊，包完了我们再跟你们走。",
    "先把他胳膊包一下，血止住了就跟你们走。",
    "他胳膊一直在流血，先包一下吧，包完我们跟你们走。",
]

#: 正常的有来有回（各说各的，没有复读）。
_HEALTHY_LINES = [
    "这家店我常来。",
    "是吗，我第一次来。",
    "你喝什么？",
    "热的，谢谢。",
]


def _msgs(lines: list[str], speakers: list[str] | None = None) -> list[dict]:
    speakers = speakers or [f"角色{i}" for i in range(len(lines))]
    return [_line(text, sp) for text, sp in zip(lines, speakers)]


def test_collective_repeat_of_same_beat_triggers():
    """四人各念一遍「先包一下胳膊」→ repeat 过阈、score 过触发线。"""
    rep = evaluate(_msgs(_REPEAT_LINES), blocks_since_narration=0, silent_streak=0,
                   clock_progress=None)
    assert rep.repeat >= SceneNudgeParams().repeat_threshold
    assert rep.score >= SceneNudgeParams().threshold
    assert "角色在复读" in rep.reasons


def test_healthy_varied_exchange_does_not_trigger():
    """有来有回的正常对话（刚叙述过、无人沉默、无时间边界）→ 不触发。"""
    rep = evaluate(_msgs(_HEALTHY_LINES), blocks_since_narration=0, silent_streak=0,
                   clock_progress=0.0)
    assert rep.repeat == 0.0
    assert rep.score < SceneNudgeParams().threshold
    assert rep.reasons == []


def test_repeat_below_threshold_contributes_nothing():
    """相似度没过阈就不是复读：分量归零（正常对话不因"有点像我"被推着走）。"""
    p = SceneNudgeParams(repeat_threshold=0.99)
    rep = evaluate(_msgs(_REPEAT_LINES), 0, 0, None, p)
    assert rep.repeat == 0.0 and rep.score == 0.0


def test_repeat_window_limits_which_lines_count():
    """repeat_window=2：只比较最近两条——把复读句挤到窗外就不再判复读。"""
    p = SceneNudgeParams(repeat_window=2)
    msgs = _msgs(_REPEAT_LINES)
    assert evaluate(msgs, 0, 0, None, p).repeat >= p.repeat_threshold
    # 窗口只看最后两条（"先把他胳膊包一下…" 与 "他胳膊一直在流血…"）→ 相似度 0.564
    two = msgs[-2:]
    assert evaluate(two, 0, 0, None, p).repeat >= p.repeat_threshold
    # 把复读句换成两句不搭界的，同一窗口就不再复读
    assert evaluate(_msgs(_HEALTHY_LINES[-2:]), 0, 0, None, p).repeat == 0.0


def test_cooldown_blocks_second_narration():
    """刚叙述过（块数不足 cooldown）→ blocked_by_cooldown 真，即便 score 已过线。"""
    p = SceneNudgeParams(cooldown_blocks=3)
    for blocks, blocked in ((0, True), (1, True), (2, True), (3, False), (9, False)):
        rep = evaluate(_msgs(_REPEAT_LINES), blocks, 0, None, p)
        assert rep.blocked_by_cooldown is blocked, blocks
        # cooldown 只拦调用方，不改判据本身：分数照报
        assert rep.score >= p.threshold


def test_boundary_proximity_alone_triggers():
    """临近打烊（剩余比例 ≤ boundary_window）单独就够触发——不需要复读/停滞。"""
    p = SceneNudgeParams()
    far = evaluate(_msgs(_HEALTHY_LINES), 0, 0, 1.0 - p.boundary_window * 2)
    assert far.boundary == 0.0 and far.score < p.threshold
    near = evaluate(_msgs(_HEALTHY_LINES), 0, 0, 1.0 - p.boundary_window / 2)
    assert near.boundary == 1.0
    assert near.score >= p.threshold
    assert "临近打烊" in near.reasons


def test_no_clock_no_boundary_component():
    """无时间硬边界（clock_progress=None）→ boundary 恒 0，绝不凭空调时间压力。"""
    assert evaluate(_msgs(_HEALTHY_LINES), 0, 0, None).boundary == 0.0


def test_stall_and_silence_components_and_reasons():
    """停滞/沉默各自累积到半格才写进 reasons；满格分量封顶 1.0（不会溢出记分）。

    期望值按模块权重实算（scenarist._W_STALL/_W_SILENCE），不硬编码——权重可调，
    但"分量线性、满格封顶、reasons 半格才记"这三条语义必须钉住。
    """
    from harness import scenarist as sc
    p = SceneNudgeParams(stall_blocks=8, silence_blocks=4)
    half = evaluate(_msgs(_HEALTHY_LINES), 4, 2, None, p)      # 4/8, 2/4
    assert half.stall == 0.5 and half.silence == 0.5
    assert "久未推进" in half.reasons and "久无人接话" in half.reasons
    assert half.score == sc._W_STALL * 0.5 + sc._W_SILENCE * 0.5   # 无复读

    tiny = evaluate(_msgs(_HEALTHY_LINES), 2, 1, None, p)     # 2/8, 1/4 —— 都不到半格
    assert tiny.reasons == []

    full = evaluate(_msgs(_HEALTHY_LINES), 50, 50, None, p)   # 远超满格 → 分量封顶
    assert full.stall == 1.0 and full.silence == 1.0
    assert full.score == sc._W_STALL + sc._W_SILENCE


def test_stall_alone_can_cross_the_threshold():
    """实况教训：纯僵局（不复读、不静默）也必须能单独触发——否则"没人推剧情"时场景
    永远不出手（曾实测 13 块零触发）。故 _W_STALL 必须 > threshold。"""
    from harness import scenarist as sc
    p = SceneNudgeParams()
    assert sc._W_STALL > p.threshold, "停滞权重需大于触发线，否则停滞永远无法单独触发"
    rep = evaluate(_msgs(_HEALTHY_LINES), p.stall_blocks, 0, None, p)
    assert rep.repeat == 0.0 and rep.stall == 1.0
    assert rep.score >= p.threshold and not rep.blocked_by_cooldown


def test_progress_toward_boundary_uses_remaining_ratio():
    """剩余比例恰在窗口上（== boundary_window）视为该推进（闭区间）。"""
    p = SceneNudgeParams(boundary_window=0.15)
    rep = evaluate(_msgs(_HEALTHY_LINES), 0, 0, 0.85, p)
    assert rep.boundary == 1.0


def test_only_character_lines_count_toward_repeat():
    """叙述/导演/人类的话不参与复读判定——场景自己插的话不算「角色在复读」。"""
    msgs = [_line(t, "场景", "narrator") for t in _REPEAT_LINES]
    msgs += [_line("（世界层注入：侍者过来添水。）", "导演", "director")]
    assert evaluate(msgs, 0, 0, None).repeat == 0.0
    mixed = msgs + _msgs(_REPEAT_LINES)
    assert evaluate(mixed, 0, 0, None).repeat >= 0.5


def test_deterministic_and_does_not_mutate_input():
    """纯函数：同样输入必得同样报告，且不改调用方给的 messages。"""
    msgs = _msgs(_REPEAT_LINES)
    before = [dict(m) for m in msgs]
    a = evaluate(msgs, 1, 2, 0.9)
    b = evaluate(msgs, 1, 2, 0.9)
    assert a == b
    assert msgs == before


# ---------------------------------------------------------------------------
# 场景工具指令：LLM 只吐纯文本，"改场景"只能编码成叙述里的一行指令，解析出来交给外壳。
# 设计文档 §3.1/§4.1③：`description_mutable` 的场景变化时"调工具"（桌椅 → 破碎的桌椅）。
# ---------------------------------------------------------------------------

_CLEAN = "贝克街221B里有一些桌椅。\n他推开门，风灌了进来。\n「打烊了。」侍者说。"


def test_pure_narration_is_unchanged_and_yields_nothing():
    """没有指令的干净叙述：**逐字不变**（空白规范化的既有约定下），无 tools/malformed。"""
    p = parse_narration(_CLEAN)
    assert p.text == _CLEAN
    assert p.tools == []
    assert p.malformed == []


def test_directive_at_the_end_is_stripped_and_parsed():
    """收在末尾的 set_description：正文不留痕，参数（中文标点/引号/首尾空格）精确取出。"""
    raw = _CLEAN + "\n[[TOOL:set_description]]   贝克街221B里有一些破碎的桌椅，到处都是战斗后的痕迹。   "
    p = parse_narration(raw)
    assert p.text == _CLEAN
    assert p.tools == [SceneToolCall("set_description",
                                     "贝克街221B里有一些破碎的桌椅，到处都是战斗后的痕迹。")]
    assert p.malformed == []
    assert TOOL_OPEN not in p.text


def test_argument_is_verbatim_apart_from_outer_strip():
    """参数只做首尾 strip：内部空白属于场景文案本身，原样保留（含中文引号、破折号）。"""
    raw = "[[TOOL:set_description]]　「破碎  的桌椅」——到处都是战斗后的痕迹。　"
    p = parse_narration(raw)
    assert p.tools == [SceneToolCall("set_description", "「破碎  的桌椅」——到处都是战斗后的痕迹。")]
    assert p.malformed == [] and p.text == ""


def test_directive_in_the_middle_keeps_both_sides_in_order():
    """夹在中间的指令：前后两段叙述都留着、顺序不变，指令行（连同它占的整行）消失。"""
    raw = "他推开门。\n[[TOOL:set_description]] 贝克街221B里有一些破碎的桌椅。\n侍者抬起头。"
    p = parse_narration(raw)
    assert p.text == "他推开门。\n侍者抬起头。"
    assert p.tools == [SceneToolCall("set_description", "贝克街221B里有一些破碎的桌椅。")]
    assert p.malformed == []


def test_inline_directive_keeps_the_text_before_it():
    """行内指令（前有叙述）：标记之前的字留下，标记到行尾都归指令（参数不跨行）。"""
    p = parse_narration("他推开门。[[TOOL:set_name]] 废墟贝克街221B\n侍者抬起头。")
    assert p.text == "他推开门。\n侍者抬起头。"
    assert p.tools == [SceneToolCall("set_name", "废墟贝克街221B")]


def test_two_directives_on_separate_lines_keep_order():
    """两行两条指令：都解析出来，顺序按出现先后（外壳改场景有先后语义）。"""
    raw = ("[[TOOL:set_background]] 夜里的贝克街221B，只剩一盏灯\n叙述一。\n"
           "[[TOOL:set_name]] 废墟贝克街221B\n叙述二。")
    p = parse_narration(raw)
    assert p.tools == [SceneToolCall("set_background", "夜里的贝克街221B，只剩一盏灯"),
                       SceneToolCall("set_name", "废墟贝克街221B")]
    assert p.text == "叙述一。\n叙述二。"
    assert p.malformed == []


def test_malformed_fragments_are_recorded_removed_and_never_raise():
    """畸形表：有开头没结尾 / 空名字 / 少冒号 / 名字非法字符 → 进 malformed 且不出现在 text。"""
    for raw in ("[[TOOL]] 贝克街221B里有一些桌椅。",
                "[[TOOL:]] 贝克街221B里有一些桌椅。",
                "[[TOOL:set_description 贝克街221B里有一些桌椅。",
                "[[TOOL:Set Description]] 贝克街221B里有一些桌椅。",
                "[[TOOL:set Description]]",
                "[[TOOL]]"):
        p = parse_narration(raw)
        assert p.tools == [], raw
        assert len(p.malformed) == 1, raw
        assert p.malformed[0].startswith("[[TOOL"), raw
        assert p.text == "", raw
        assert TOOL_OPEN not in p.text, raw


def test_unterminated_marker_keeps_narration_before_it():
    """写到一半被截断的指令：畸形片段完整留在 malformed（供日志），前面的叙述不陪葬。"""
    p = parse_narration("他推开门。[[TOOL:set_description 破碎的桌椅")
    assert p.text == "他推开门。"
    assert p.malformed == ["[[TOOL:set_description 破碎的桌椅"]


def test_empty_none_and_blank_input_never_raise():
    """空/None/纯空白：返回空结果，绝不抛（模型偶发空输出不该炸掉一轮对话）。"""
    for raw in ("", None, "\n\n   \n"):
        p = parse_narration(raw)
        assert p == ParsedNarration(text="", tools=[], malformed=[]), repr(raw)


def test_unknown_but_wellformed_tool_name_is_parsed_not_dropped():
    """名字合法但不认识（政策归调用方）：照样解析出来，绝不静默丢弃。"""
    p = parse_narration("[[TOOL:set_time]] 打烊后")
    assert p.tools == [SceneToolCall("set_time", "打烊后")]
    assert p.malformed == []


def test_text_never_leaks_raw_syntax():
    """"text 里不含 `[[TOOL`"是硬不变量：语法泄进用户看到的叙述是事故。"""
    raw = "前。[[TOOL 中。\n[[TOOL:x]] 后。\n[[TOOL:set_name\n尾。"
    p = parse_narration(raw)
    assert "[[TOOL" not in p.text
    # 指令只吃到行尾：被截断的那行自己消失，下一行「尾。」仍是无辜的叙述
    assert p.text == "前。\n尾。"
    assert p.tools == [SceneToolCall("x", "后。")]
    assert p.malformed == ["[[TOOL 中。", "[[TOOL:set_name"]


def test_render_tool_call_round_trips_through_parse_narration():
    """render 出的指令必须能被 parse 原样读回（提示词里的语法示例才可信）。"""
    for call in (SceneToolCall("set_description", "贝克街221B里有一些破碎的桌椅，到处都是战斗后的痕迹。"),
                 SceneToolCall("set_name", "废墟贝克街221B"),
                 SceneToolCall("append_description", ""),
                 SceneToolCall("set_background", "夜里的贝克街221B")):
        p = parse_narration(render_tool_call(call))
        assert p.tools == [call], call
        assert p.text == "" and p.malformed == []


def test_render_tool_call_is_the_documented_syntax():
    """render 的字面形态钉住（提示词与 parse 的契约）：`[[TOOL:名字]] 参数`。"""
    assert render_tool_call(SceneToolCall("set_name", "废墟贝克街221B")) == "[[TOOL:set_name]] 废墟贝克街221B"
    assert render_tool_call(SceneToolCall("set_description", "")) == "[[TOOL:set_description]]"


def test_tools_prompt_block_empty_when_nothing_is_mutable():
    """没有可改变字段 → 空块（外壳别附这段提示词），未知字段名不算数。"""
    assert tools_prompt_block([]) == ""
    assert tools_prompt_block(["不存在的字段"]) == ""


def test_tools_prompt_block_states_syntax_and_every_mutable_field():
    """有可改变字段 → 给出语法示例、每个字段名，并强调叙述要自成一体（删掉指令行仍完整）。"""
    block = tools_prompt_block(["description", "background"])
    assert TOOL_OPEN in block and TOOL_CLOSE in block
    assert "可改变" in block
    for field in ("description", "background"):
        assert field in block, field
    assert "set_description" in block and "append_description" in block
    assert "set_background" in block and "set_name" not in block   # 没标可改的不列出来
    assert "自成一体" in block
    # 示例按第一个可改变字段选（省得示例里出现没标可改变的字段）
    assert "[[TOOL:set_description]]" in block
    assert "[[TOOL:set_background]]" in tools_prompt_block(["background"])


def test_apply_tools_each_name_has_its_documented_effect():
    """四种工具各按契约生效，且返回新 dict。"""
    patch = {"name": "贝克街221B", "description": "贝克街221B里有一些桌椅。", "background": "白天"}
    out = apply_tools(patch, [SceneToolCall("set_description", "贝克街221B里有一些破碎的桌椅。"),
                              SceneToolCall("set_background", "夜里"),
                              SceneToolCall("set_name", "废墟贝克街221B")])
    assert out == {"name": "废墟贝克街221B",
                   "description": "贝克街221B里有一些破碎的桌椅。",
                   "background": "夜里"}
    assert out is not patch


def test_append_description_joins_with_one_space():
    """追加：两边都非空时中间补一个空格（别粘成一坨）。"""
    out = apply_tools({"description": "贝克街221B里有一些桌椅。"},
                      [SceneToolCall("append_description", "到处都是战斗后的痕迹。")])
    assert out["description"] == "贝克街221B里有一些桌椅。 到处都是战斗后的痕迹。"


def test_append_description_on_empty_value_adds_no_stray_space():
    """原值为空/缺键/None：不该留下前导空格；追加空串也不该留下尾随空格。"""
    for patch in ({}, {"description": ""}, {"description": None}):
        out = apply_tools(patch, [SceneToolCall("append_description", "只有一张桌子。")])
        assert out["description"] == "只有一张桌子。", patch
    assert apply_tools({"description": "贝克街221B。"},
                       [SceneToolCall("append_description", "")])["description"] == "贝克街221B。"


def test_set_name_never_writes_path_or_filename():
    """名字只进 name：path/filename 是外壳的事，工具改不得（也拒绝 set_path 这类名字）。"""
    patch = {"name": "贝克街221B", "path": "scenes/贝克街221B.md", "filename": "贝克街221B.md"}
    out = apply_tools(patch, [SceneToolCall("set_name", "废墟贝克街221B"),
                              SceneToolCall("set_path", "scenes/废墟贝克街221B.md"),
                              SceneToolCall("set_filename", "废墟贝克街221B.md")])
    assert out["name"] == "废墟贝克街221B"
    assert out["path"] == "scenes/贝克街221B.md"
    assert out["filename"] == "贝克街221B.md"


def test_unknown_tool_is_ignored():
    """不认识的工具名：忽略（不认识就不猜），其余照改。"""
    patch = {"description": "贝克街221B。"}
    assert apply_tools(patch, [SceneToolCall("set_time", "打烊后"),
                               SceneToolCall("destroy_everything", "x")]) == patch


def test_apply_tools_does_not_mutate_input_and_is_deterministic():
    """纯函数：调用方的 dict 不被改动，同样输入必得同样输出。"""
    patch = {"description": "贝克街221B。", "name": "贝克街221B"}
    before = dict(patch)
    calls = [SceneToolCall("set_description", "废墟。")]
    assert apply_tools(patch, calls) == apply_tools(patch, calls)
    assert patch == before


def test_parse_is_deterministic():
    """同样的输入解析两遍必得同样的结果（含 tools/malformed 的顺序）。"""
    raw = ("他推开门。\n[[TOOL:set_description]] 破碎的桌椅\n[[TOOL]] 坏写法\n"
           "[[TOOL:set_name 截断\n侍者抬头。")
    first, second = parse_narration(raw), parse_narration(raw)
    assert first == second
    assert first.tools == [SceneToolCall("set_description", "破碎的桌椅")]
    assert len(first.malformed) == 2


# ===========================================================================
# 活跃度（§6.2「推进频率可调」）
# ===========================================================================
def test_effective_params_is_identity_at_default_activity():
    """a=0.5（缺省档）→ 与原始标定逐字段相同：新旋钮不扰动既有判据。"""
    p = SceneNudgeParams()
    assert scenarist.effective_params(p, 0.5) == p
    assert scenarist.effective_params(p) == p, "缺省活跃度就是 0.5"
    assert scenarist.effective_params() == p, "连参数都可省（缺省标定 + 缺省活跃度）"
    assert scenarist.effective_params(p, 0.5).cooldown_blocks == p.cooldown_blocks


def test_effective_params_monotone_threshold_and_cooldown():
    """活跃度越高越爱推进：触发线单调不增、冷却单调不增（两端确实拉开）。"""
    p = SceneNudgeParams()
    levels = [0.0, 0.2, 0.5, 0.8, 1.0]
    eff = [scenarist.effective_params(p, a) for a in levels]
    thresholds = [e.threshold for e in eff]
    cooldowns = [e.cooldown_blocks for e in eff]
    assert thresholds == sorted(thresholds, reverse=True), thresholds
    assert cooldowns == sorted(cooldowns, reverse=True), cooldowns
    assert thresholds[0] > thresholds[-1] and cooldowns[0] > cooldowns[-1]
    assert thresholds[0] == pytest.approx(p.threshold * 1.3)
    assert thresholds[-1] == pytest.approx(p.threshold * 0.7)
    assert cooldowns[0] == round(p.cooldown_blocks * 1.6)
    assert cooldowns[-1] == round(p.cooldown_blocks * 0.4)
    assert thresholds[-1] > 0 and cooldowns[-1] >= 0, "夹取到 sane 范围"


def test_effective_params_only_moves_rhythm_not_situation():
    """只有节奏（触发线/冷却）随活跃度动；局势类参数（复读/停滞/沉默/打烊）原样。"""
    p = SceneNudgeParams()
    for a in (0.0, 0.2, 0.5, 0.8, 1.0):
        eff = scenarist.effective_params(p, a)
        assert (eff.repeat_window, eff.repeat_threshold, eff.stall_blocks,
                eff.silence_blocks, eff.boundary_window) == (
            p.repeat_window, p.repeat_threshold, p.stall_blocks,
            p.silence_blocks, p.boundary_window)
    # 纯函数：原参数一动不动
    assert p == SceneNudgeParams()


def test_effective_params_clamps_activity_and_never_goes_negative():
    p = SceneNudgeParams(threshold=0.1, cooldown_blocks=0)
    assert scenarist.effective_params(p, 1.5) == scenarist.effective_params(p, 1.0)
    assert scenarist.effective_params(p, -3) == scenarist.effective_params(p, 0.0)
    assert scenarist.effective_params(p, 0.0).cooldown_blocks == 0
    assert scenarist.effective_params(SceneNudgeParams(threshold=-5), 1.0).threshold == 0.0


def test_activity_moves_the_trigger_line_over_the_same_report():
    """同一个报告：a=1.0 过线（该开口），a=0.0 不过线（憋着）——活跃度真的改变结论。"""
    # 久未推进（7 块）：stall = 7/8 → score = 1.05，恰落在两条触发线之间。
    rep = evaluate(_msgs(_HEALTHY_LINES), blocks_since_narration=7, silent_streak=0,
                   clock_progress=None)
    hi = scenarist.effective_params(SceneNudgeParams(), 1.0)
    lo = scenarist.effective_params(SceneNudgeParams(), 0.0)
    assert lo.threshold > rep.score >= hi.threshold, (lo.threshold, rep.score, hi.threshold)


def test_activity_label_follows_the_gui_four_levels():
    """四档标签与 GUI 的四档值（0.2/0.5/0.8/1.0）一一对应。"""
    assert [scenarist.activity_label(a) for a in (0.2, 0.5, 0.8, 1.0)] == \
        ["低", "中", "高", "极高"]
    assert scenarist.activity_label(-1) == "低" and scenarist.activity_label(9) == "极高"


def test_activity_hint_states_level_and_hook_only_contract():
    """提示句：档位 + 少/多介入 + 「只判钩子时只写指令行、不写正文」。"""
    hint = scenarist.activity_hint(0.5, hooks=True)
    assert "【推进活跃度】" in hint and "中" in hint and "0.50" in hint
    assert "[[HOOK:" in hint and "不要写叙述正文" in hint
    assert "适度介入" in hint
    assert scenarist.activity_hint(1.0, hooks=True).startswith("【推进活跃度】当前活跃度：极高")
    assert "尽量主动介入" in scenarist.activity_hint(1.0)
    assert "尽量少介入" in scenarist.activity_hint(0.2)


def test_activity_hint_omits_hook_syntax_when_no_hooks():
    """没有钩子的场景绝不提 `[[HOOK:…]]`（免得模型凭空乱报钩子）。"""
    hint = scenarist.activity_hint(0.8)
    assert "[[HOOK" not in hint
    assert "可以不写正文" in hint


# ===========================================================================
# 时间条件闸门（钩子条件里的钟点：引擎侧确定性校验的唯一判据）
# ===========================================================================
# 实况 bug：场景 阅览室 的钩子 h3/h3-2 条件是「虚拟钟走到 21:30」（两人离场），
# 用户在 19:03 加人，两人**立刻走了**——2.5 小时提前。根因：钩子条件只由场景 LLM 判，
# 引擎把当前钟点喂进提示词（【当前时刻】）却从不自己校验时间条件，模型判错就照执行。
# 本节的纯函数就是那条确定性判据：条件**明确在说虚拟钟**时才给出目标秒数，其余一律
# None（引擎不越权替模型判「有人提起 21:30 那件事」这类非时间条件）。
@pytest.mark.parametrize("condition,expected", [
    # —— 正例：有钟点 + 有钟表词 → 目标秒数 ——
    ("虚拟钟走到 21:30", 21 * 3600 + 30 * 60),
    ("时间到了 22 点", 22 * 3600),                      # 只给小时（点）→ 整点
    ("虚拟钟走到 21点30分", 21 * 3600 + 30 * 60),        # 中文「点/分」式
    ("虚拟钟走到 21点30", 21 * 3600 + 30 * 60),          # 省略「分」
    ("虚拟钟走到 21点", 21 * 3600),
    ("时间 9:05 之后就散会", 9 * 3600 + 5 * 60),
    ("打烊之后 22:30", 22 * 3600 + 30 * 60),
    ("开门时间 8:00", 8 * 3600),
    ("晚于 23:15 就别等了", 23 * 3600 + 15 * 60),
    ("钟点到了 6:00", 6 * 3600),
    ("已经到了 10:20", 10 * 3600 + 20 * 60),
    ("时刻来到 0:30", 30 * 60),
    ("走到 00:00", 0),
])
def test_time_condition_target_positive_table(condition, expected):
    assert scenarist.time_condition_target(condition) == expected


@pytest.mark.parametrize("condition", [
    "21:30",                        # 只有钟点、没有钟表词 —— 不是时间闸门
    "有人提起 21:30 那件事",          # 提到某个钟点 ≠ 时间条件（不得提前闸门）
    "21点30分",                      # 中文钟点但同样没有钟表词
    "到点了",                        # 有钟表词、没有钟点
    "时间不早了",                     # 同上
    "虚拟钟走到 25:70",               # 小时越界
    "虚拟钟走到 21:75",               # 分钟越界
    "虚拟钟走到 99点99分",             # 中文式越界
    "虚拟钟走到 24:00",               # 24:00 越界（parse_hhmm 同口径）
    "",
    "   ",
    "随便一句话",
], ids=lambda s: repr(s))
def test_time_condition_target_negative_table(condition):
    assert scenarist.time_condition_target(condition) is None


@pytest.mark.parametrize("garbage", [None, 123, [], {}, "虚拟钟走到 :", "时间 :30"])
def test_time_condition_target_never_raises(garbage):
    """模型/作者给什么都不能炸：非字符串、半截钟点一律 None，绝不抛异常。"""
    assert scenarist.time_condition_target(garbage) is None


def test_time_condition_target_is_deterministic_and_pure():
    c = "虚拟钟走到 21:30"
    assert scenarist.time_condition_target(c) == scenarist.time_condition_target(c) == 77400
