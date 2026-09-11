"""场景钩子（spec §4）纯数据/纯函数测试。

本模块不含 LLM、不做 IO；所有函数必须确定性且不得改动入参。
"""
from __future__ import annotations

import copy
from dataclasses import asdict

from harness.hooks import (
    SCENE_PATCH_ALLOWED_KEYS,
    VALID_HOOK_ID_RE,
    Hook,
    PendingCharacterAction,
    apply_scene_patch,
    describe,
    hooks_prompt_block,
    tick,
    valid_hook_id,
    validate,
)


# --------------------------------------------------------------------------
# 构造辅助
# --------------------------------------------------------------------------
def ctx_hook(**kw) -> Hook:
    base = dict(id="h-ctx", condition="天亮之后", event_kind="context",
                context_text="清晨的集市刚开张")
    base.update(kw)
    return Hook(**base)


def char_hook(**kw) -> Hook:
    base = dict(id="h-char", condition="甲得知真相", event_kind="character",
                character_name="甲", action="remove")
    base.update(kw)
    return Hook(**base)


def scene_hook(**kw) -> Hook:
    base = dict(id="h-scene", condition="雨停", event_kind="scene",
                scene_patch={"name": "雨后的巷口", "description": "地面积水"})
    base.update(kw)
    return Hook(**base)


def pending(name: str, action: str = "remove", turns: int = 0,
            after: int = 1, **kw) -> PendingCharacterAction:
    return PendingCharacterAction(character_name=name, action=action,
                                  turns=turns, fire_after_rounds=after, **kw)


# --------------------------------------------------------------------------
# describe
# --------------------------------------------------------------------------
def test_describe_character_remove_matches_spec_example():
    h = char_hook(condition="甲得知真相", character_name="甲", action="remove")
    assert describe(h) == "若『甲得知真相』→ 让 甲 离场（显式）"


def test_describe_character_actions():
    assert "加入" in describe(char_hook(action="add", visible=True))
    assert "静默 3 轮" in describe(
        char_hook(action="mute_turns", turns=3, visible=False))
    assert "长期静默" in describe(char_hook(action="mute"))
    assert "解除静默" in describe(char_hook(action="unmute"))


def test_describe_visibility_marker():
    assert "（显式）" in describe(char_hook(visible=True))
    assert "（隐式）" in describe(char_hook(visible=False))


def test_describe_context_and_scene():
    c = describe(ctx_hook())
    assert "天亮之后" in c and "清晨的集市刚开张" in c
    s = describe(scene_hook())
    assert "雨后的巷口" in s and "雨停" in s


def test_describe_scene_empty_patch_does_not_crash():
    assert isinstance(describe(scene_hook(scene_patch={})), str)
    assert isinstance(describe(Hook(id="x", condition="c", event_kind="scene")), str)


def test_describe_disabled_and_note():
    d = describe(char_hook(enabled=False))
    assert "已停用" in d
    n = describe(char_hook(note="第二章伏笔"))
    assert "第二章伏笔" in n


def test_describe_unknown_kind_does_not_crash():
    assert isinstance(describe(Hook(id="x", condition="c", event_kind="weather")), str)


def test_describe_is_pure():
    h = char_hook(note="备注")
    before = asdict(h)
    describe(h)
    describe(h)
    assert asdict(h) == before == asdict(char_hook(note="备注"))


def test_describe_deterministic():
    a = ctx_hook()
    assert describe(a) == describe(a)


# --------------------------------------------------------------------------
# validate
# --------------------------------------------------------------------------
def test_validate_valid_hooks():
    assert validate(ctx_hook()) == []
    assert validate(char_hook()) == []
    assert validate(char_hook(action="mute_turns", turns=2)) == []
    assert validate(scene_hook()) == []


def test_validate_context_needs_text():
    errs = validate(ctx_hook(context_text="   "))
    assert errs and all(isinstance(e, str) for e in errs)
    assert any("文本" in e for e in errs)


def test_validate_character_needs_name():
    errs = validate(char_hook(character_name=" "))
    assert errs
    assert any("角色" in e for e in errs)


def test_validate_mute_turns_requires_positive_turns():
    assert validate(char_hook(action="mute_turns", turns=0))
    assert validate(char_hook(action="mute_turns", turns=-3))
    assert validate(char_hook(action="mute_turns", turns=1)) == []


def test_validate_unknown_action_and_kind():
    bad_action = validate(char_hook(action="explode"))
    assert bad_action and any("动作" in e for e in bad_action)
    bad_kind = validate(Hook(id="x", condition="c", event_kind="weather"))
    assert bad_kind and any("类型" in e for e in bad_kind)


def test_validate_requires_condition():
    assert validate(ctx_hook(condition="  "))


def test_validate_scene_needs_patch():
    assert validate(scene_hook(scene_patch={}))


def test_valid_hook_id_accepts_engine_charset_and_rejects_everything_else():
    """H1：id 合法性有唯一判据——与引擎解析 `[[HOOK:<id>]]` 的字符集同一套。"""
    for good in ("h1", "hook-1", "hook_1", "a.b", "H9", "9"):
        assert valid_hook_id(good), good
    assert VALID_HOOK_ID_RE.match("h1")
    for bad in ("", "   ", "得知真相", "真相", "h 1", " h1", "h1 ", "h[1]", "h/1",
                "h1!", "ｈ１", "a\nb"):
        assert not valid_hook_id(bad), bad


def test_hook_id_rule_is_literally_the_engines_rule():
    """id 判据必须与引擎**逐字节同一条**（引擎侧解析 id 用 _HOOK_ID_RE）——两处若各自
    漂移，就会出现「校验通过但永远打不响」或反过来的钩子。"""
    from harness.engine import _HOOK_ID_RE
    assert VALID_HOOK_ID_RE.pattern == _HOOK_ID_RE.pattern


def test_validate_rejects_empty_hook_id():
    errs = validate(ctx_hook(id=""))
    assert errs and any("id" in e for e in errs)


def test_validate_rejects_id_with_characters_outside_the_allowed_set():
    """中文等非法字符的 id：能通过旧校验、被写进提示词、却永远打不响——必须当场报错。"""
    errs = validate(ctx_hook(id="得知真相"))
    assert errs and any("得知真相" in e for e in errs)
    assert any("h 1" in e or "id" in e for e in validate(ctx_hook(id="h 1")))
    # 合法 id 不受影响
    assert validate(ctx_hook(id="h1")) == []
    assert validate(ctx_hook(id="a.b-c_1")) == []


def test_validate_rejects_duplicate_ids_within_the_list():
    """同一场景内两条钩子同 id：引擎按 id 查表只能命中一条，另一条永远不触发。"""
    a, b = ctx_hook(id="h1"), char_hook(id="h1")
    assert a is not b
    errs = validate(a, siblings=[b])
    assert errs and any("重复" in e and "h1" in e for e in errs)
    # 对方视角同样报（两边都提示，作者才知道是哪两条撞了）
    assert any("重复" in e for e in validate(b, siblings=[a]))
    # 不同 id / 空列表 / 缺省 siblings 都不误报
    assert validate(a, siblings=[char_hook(id="h2")]) == []
    assert validate(a, siblings=[]) == validate(a) == []
    # 自己不在 siblings 里也不算重复（siblings 语义 = 同场景的**其它**钩子）
    assert validate(a, siblings=[a]) == []


def test_validate_duplicate_check_ignores_disabled_and_skips_when_id_invalid():
    """重复判定只看 id，不看启用状态；id 本身非法时不叠报重复（先修 id）。"""
    on, off = ctx_hook(id="h1"), char_hook(id="h1", enabled=False)
    assert any("重复" in e for e in validate(on, siblings=[off]))
    errs = validate(ctx_hook(id="真相"), siblings=[char_hook(id="真相")])
    assert not any("重复" in e for e in errs)


def test_validate_is_pure_and_deterministic():
    h = ctx_hook(context_text="")
    before = asdict(h)
    first = validate(h)
    second = validate(h)
    assert first == second
    assert asdict(h) == before
    assert isinstance(first, list)
    # 返回的是新列表，调用方改动不影响后续调用
    first.append("污染")
    assert validate(h) == second


# --------------------------------------------------------------------------
# tick
# --------------------------------------------------------------------------
def test_tick_fires_when_countdown_reaches_zero():
    fired, remaining = tick([pending("甲", after=1)], rounds_elapsed=1)
    assert len(fired) == 1 and fired[0].character_name == "甲"
    assert remaining == []
    # 触发项携带扣减后的值
    assert fired[0].fire_after_rounds == 0


def test_tick_keeps_waiting_items_in_order():
    a, b, c = pending("A", after=1), pending("B", after=3), pending("C", after=2)
    fired, remaining = tick([a, b, c], rounds_elapsed=1)
    assert [p.character_name for p in fired] == ["A"]
    assert [p.character_name for p in remaining] == ["B", "C"]
    assert [p.fire_after_rounds for p in remaining] == [2, 1]


def test_tick_multi_round_elapsed():
    p = pending("甲", after=3)
    fired, remaining = tick([p], rounds_elapsed=2)
    assert fired == [] and remaining[0].fire_after_rounds == 1
    fired, remaining = tick(remaining, rounds_elapsed=1)
    assert len(fired) == 1 and fired[0].fire_after_rounds == 0
    assert remaining == []


def test_tick_overdue_fires_immediately():
    fired, remaining = tick([pending("甲", after=0), pending("陈默", after=-5)],
                            rounds_elapsed=1)
    assert [p.character_name for p in fired] == ["甲", "陈默"]
    assert remaining == []


def test_tick_preserves_order_across_many():
    items = [pending(f"P{i}", after=i) for i in range(5)]
    fired, remaining = tick(items, rounds_elapsed=2)
    assert [p.character_name for p in fired] == ["P0", "P1", "P2"]
    assert [p.character_name for p in remaining] == ["P3", "P4"]


def test_tick_default_rounds_elapsed_is_one():
    fired, remaining = tick([pending("甲", after=1)])
    assert len(fired) == 1 and remaining == []


def test_tick_empty_input():
    assert tick([]) == ([], [])


def test_tick_does_not_mutate_inputs():
    items = [pending("A", after=1), pending("B", after=4)]
    snapshot = copy.deepcopy(items)
    before_len = len(items)
    fired, remaining = tick(items, rounds_elapsed=1)
    assert items == snapshot
    assert len(items) == before_len
    assert fired[0] is not items[0]
    assert remaining[0] is not items[1]


def test_tick_keeps_payload_fields():
    p = pending("甲", action="mute_turns", turns=3, after=1,
                notify=["陈默"], notify_text="甲暂时不出声", visible=False)
    fired, _ = tick([p], rounds_elapsed=1)
    f = fired[0]
    assert (f.action, f.turns, f.notify, f.notify_text, f.visible) == \
        ("mute_turns", 3, ["陈默"], "甲暂时不出声", False)


def test_tick_is_deterministic():
    items = [pending("A", after=2), pending("B", after=1)]
    assert tick(items, 1) == tick(items, 1)


# --------------------------------------------------------------------------
# apply_scene_patch（白名单：只应用 allowed 里的键，其余丢弃并回报）
# --------------------------------------------------------------------------
def scene() -> dict:
    return {"name": "旧名字", "description": "旧描述",
            "path": "scenes/old.md", "filename": "old.md", "mood": "平静"}


def test_scene_patch_whitelist_is_literally_the_engines_whitelist():
    """H2：本函数与引擎 _apply_scene_hook 用的必须是**同一张白名单**——两处各自维护
    就会出现「按提示词改了、引擎却忽略」或「提示词说不可改、这里却写进去」的错配。"""
    from harness.engine import _SCENE_HOOK_KEYS
    assert SCENE_PATCH_ALLOWED_KEYS == _SCENE_HOOK_KEYS


def test_apply_scene_patch_applies_allowed_keys_into_new_dict():
    s = scene()
    out, rejected = apply_scene_patch(s, {"name": "新名字", "description": "新描述"},
                                      allowed=SCENE_PATCH_ALLOWED_KEYS)
    assert out is not s
    assert out["name"] == "新名字" and out["description"] == "新描述"
    assert rejected == []
    assert s["name"] == "旧名字" and s["description"] == "旧描述"   # 入参不动
    # 场景底本的非白名单键（磁盘身份等）原样保留——本函数只挑 patch，绝不删场景键
    assert out["path"] == "scenes/old.md" and out["filename"] == "old.md"
    assert out["mood"] == "平静"


def test_apply_scene_patch_drops_unknown_keys_and_reports_them():
    """黑名单→白名单：未知键不再照单全收，而是丢弃 + 回报（调用方可记诊断）。"""
    out, rejected = apply_scene_patch(scene(), {"weather": "rain", "props": ["伞"]},
                                      allowed=SCENE_PATCH_ALLOWED_KEYS)
    assert "weather" not in out and "props" not in out
    assert rejected == ["weather", "props"]


def test_apply_scene_patch_never_touches_path_and_filename():
    """文件名/路径是磁盘身份：不在白名单 → 拒绝写入并回报，也不改底本里的原值。"""
    out, rejected = apply_scene_patch(
        scene(), {"path": "hack/evil.md", "filename": "evil.md", "name": "新名字"},
        allowed=SCENE_PATCH_ALLOWED_KEYS)
    assert out["path"] == "scenes/old.md" and out["filename"] == "old.md"
    assert out["name"] == "新名字"
    assert rejected == ["path", "filename"]


def test_apply_scene_patch_allowed_argument_is_the_single_rule():
    """allowed 由调用方给出（与引擎白名单同一常量）；换一份 allowed 即换一套政策。"""
    out, rejected = apply_scene_patch(scene(), {"weather": "rain", "name": "新名字"},
                                      allowed=("weather",))
    assert out["weather"] == "rain"
    assert out["name"] == "旧名字"          # 不在 allowed → 丢弃
    assert rejected == ["name"]


def test_apply_scene_patch_refuses_file_keys_even_without_allowed_list():
    """文件键即使被误写进 allowed 也拒绝——磁盘身份不归钩子/工具改（纵深防御）。"""
    out, rejected = apply_scene_patch(scene(), {"filename": "evil.md"},
                                      allowed=("filename", "name"))
    assert out["filename"] == "old.md"
    assert rejected == ["filename"]


def test_apply_scene_patch_only_rejected_keys_is_copy():
    s = scene()
    out, rejected = apply_scene_patch(s, {"path": "x.md", "filename": "x.md"},
                                      allowed=SCENE_PATCH_ALLOWED_KEYS)
    assert out == s and out is not s and rejected == ["path", "filename"]


def test_apply_scene_patch_empty_patch_is_copy():
    s = scene()
    out, rejected = apply_scene_patch(s, {}, allowed=SCENE_PATCH_ALLOWED_KEYS)
    assert out == s and out is not s and rejected == []


def test_apply_scene_patch_does_not_mutate_inputs():
    s, p = scene(), {"name": "新名字", "path": "hack.md"}
    s_before, p_before = copy.deepcopy(s), copy.deepcopy(p)
    apply_scene_patch(s, p, allowed=SCENE_PATCH_ALLOWED_KEYS)
    assert s == s_before and p == p_before


def test_apply_scene_patch_missing_base_keys_ok():
    out, rejected = apply_scene_patch({}, {"name": "全新场景"},
                                      allowed=SCENE_PATCH_ALLOWED_KEYS)
    assert out == {"name": "全新场景"} and rejected == []


# --------------------------------------------------------------------------
# hooks_prompt_block
# --------------------------------------------------------------------------
def test_prompt_block_empty_inputs():
    assert hooks_prompt_block([], []) == ""


def test_prompt_block_lists_conditions_and_effects():
    block = hooks_prompt_block([char_hook(), ctx_hook()], [])
    assert block
    assert "甲得知真相" in block          # 条件
    assert "甲" in block and "离场" in block   # 效果
    assert "清晨的集市刚开张" in block       # context 效果


def test_prompt_block_skips_disabled_hooks():
    block = hooks_prompt_block([char_hook(enabled=False)], [])
    assert "甲得知真相" not in block
    assert block == ""


def test_prompt_block_lists_pending_delayed_actions():
    block = hooks_prompt_block([], [pending("甲", after=2,
                                            notify_text="甲两轮后离场")])
    assert "甲" in block
    assert "2" in block
    assert "甲两轮后离场" in block


def test_prompt_block_pending_only_without_hooks_is_non_empty():
    assert hooks_prompt_block([], [pending("陈默", after=1)]) != ""


def test_prompt_block_never_advertises_unfireable_ids():
    """H1：id 打不响的钩子绝不写进提示词——模型照着念也永远触发不了，只会污染叙述。

    合法 id 的钩子照常列出；全被剔除且无待触发动作时与空输入同返回 ""。
    """
    bad = ctx_hook(id="得知真相")
    good = scene_hook(id="h2")
    block = hooks_prompt_block([bad, good], [])
    assert "得知真相" not in block                # 条件/效果整条都不露面
    assert "清晨的集市刚开张" not in block
    assert "h2" in block and "雨停" in block
    assert hooks_prompt_block([ctx_hook(id="")], []) == ""
    # 只剔非法 id 的那条，不牵连同批合法钩子
    assert hooks_prompt_block([good], []) == block


def test_prompt_block_deterministic_and_pure():
    hooks = [char_hook(), scene_hook(), ctx_hook()]
    ps = [pending("A", after=1), pending("B", after=3)]
    h_snapshot = copy.deepcopy(hooks)
    p_snapshot = copy.deepcopy(ps)
    first = hooks_prompt_block(hooks, ps)
    second = hooks_prompt_block(hooks, ps)
    assert first == second
    assert hooks == h_snapshot and ps == p_snapshot
