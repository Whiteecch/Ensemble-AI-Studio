"""场景作为一等 agent（引擎 e2e）：复读成灾时自动推进，叙述一到两行，可关/可手动/可撤销。

实况抱怨：剧情需要外部事件时角色只能说话，于是四名角色连着好几轮重复同一个 beat
（「他胳膊在流血，先包一下，包完我们跟你们走」）。推进世界不是任何角色的职责——交给
场景。本文件锁定引擎侧契约：
  · 角色集体复读 → 过 cooldown 后自动冒出一条 speaker_type=="narrator" 的叙述；
  · auto_narrate=False 关掉自动（手动 narrate_now 仍可用）；
  · 叙述行 = {speaker:"场景", speaker_type:"narrator", knows=None, in_scene:本场景名}；
  · 撤销后：它不再进任何角色的 think 视图，也不进叙述者自己的视图；
  · 撤销/改写都不污染私有边通道（think_log/impressions）；
  · 产出被裁到 ≤2 行、~120 字（场景只推进一步，不许长篇铺陈）。
"""
import json
from pathlib import Path

from harness import graph as graph_mod
from harness.backends.stub import StubBackend
from harness.engine import SceneEngine
from harness.graph import _live_messages
from harness.tests.helpers import assert_public_only

#: 实况里四人反复念的那一句。
_REPEAT_LINE = "他胳膊在流血，先包一下，包完我们跟你们走。"
#: 叙述产出（stub narrate 档）。
_NARRATION = "隔壁桌有人站起来，椅子在瓷砖上刮出一声。"


def _build(tmp_path: Path, **kw) -> SceneEngine:
    """二人场（硬边界 22:00）+ 三档模型（think/speak/narrate 全 stub 可换）。"""
    (tmp_path / "贝克街221B.json").write_text(json.dumps({
        "name": "贝克街221B", "participants": ["甲", "乙"],
        "hard_boundary": {"type": "time", "value": "22:00", "desc": "打烊"}},
        ensure_ascii=False), encoding="utf-8")
    for name in ("甲", "乙"):
        (tmp_path / f"{name}.json").write_text(json.dumps(
            {"name": name, "personality": {"描述": "测试"}}, ensure_ascii=False),
            encoding="utf-8")
    (tmp_path / "models.yaml").write_text(
        "think:\n  backend: stub\n  model: stub\n  params: {}\n"
        "speak:\n  backend: stub\n  model: stub\n  params: {}\n"
        "narrate:\n  backend: stub\n  model: stub\n  params: {}\n", encoding="utf-8")
    bid = tmp_path / "bid.yaml"
    bid.write_text("interruption_threshold: 0.4\nspeak_threshold: 0.0\n"
                   "silence_k: 100000\n", encoding="utf-8")
    eng = SceneEngine(tmp_path / "贝克街221B.json",
                      [tmp_path / "甲.json", tmp_path / "乙.json"],
                      tmp_path / "models.yaml", run_root=tmp_path / "runs",
                      bid_path=bid, closing_at_block=200, **kw)
    # 所有角色永远念同一句（复读成灾的实况）；叙述档产出固定一行。
    eng.speak_backend = StubBackend(line_script=[_REPEAT_LINE])
    eng.narrate_backend = StubBackend(line_script=[_NARRATION])
    return eng


def _narrators(msgs: list[dict]) -> list[dict]:
    return [m for m in msgs if m.get("speaker_type") == "narrator"]


# ------------------------------------------------------------------ 提示词
def test_build_narrate_messages_is_scene_omniscient():
    """场景提示词：全知系统口吻（不是角色）+ 四段局势 + 收尾「请写一到两行：」。"""
    from harness.prompters import build_narrate_messages

    msgs = build_narrate_messages("[1] 甲: 他胳膊在流血。", "场景：贝克街221B",
                                  "21:50，距打烊还有 10 分钟", "角色在复读；久未推进")
    assert [m["role"] for m in msgs] == ["system", "user"]
    system = msgs[0]["content"]
    assert "你是这场戏的「场景」——不是角色，而是世界本身（全知）" in system
    assert "只写发生的事" in system and "不替角色说话" in system
    assert "一到两行" in system and "不要替后面的剧情做设计" in system
    user = msgs[1]["content"]
    for marker in ("【整场对话】", "【场景】", "【当前时刻】", "【现在需要你推进的原因】"):
        assert marker in user
    assert user.startswith("【整场对话】\n[1] 甲: 他胳膊在流血。")
    assert "【场景】场景：贝克街221B" in user
    assert "【当前时刻】21:50，距打烊还有 10 分钟" in user
    assert "【现在需要你推进的原因】角色在复读；久未推进" in user
    assert user.endswith("\n\n请写一到两行：")


def test_build_narrate_messages_placeholders():
    """空视图/空时刻落占位，不留空标题。"""
    from harness.prompters import build_narrate_messages

    user = build_narrate_messages("", "", "", "")[1]["content"]
    assert "（暂无）" in user and "（未指定）" in user and "手动推进" in user


async def test_narration_fires_once_per_cooldown_window(tmp_path):
    """角色集体复读 → 第 3 块（cooldown）自动叙述一条；再等一个 window 才出第二条。"""
    eng = _build(tmp_path)
    await eng.open_scene()
    for _ in range(2):                     # cooldown_blocks=3：前两块绝不叙述
        await eng.step(1)
    assert _narrators(await eng.messages()) == []

    await eng.step(1)                      # 第 3 块 → 判定过线且不在冷却期
    first = _narrators(await eng.messages())
    assert len(first) == 1
    n = first[0]
    assert n["speaker"] == "场景" and n["speaker_type"] == "narrator"
    assert n.get("knows") is None, "叙述是所有人都能看见的（无 knows 限定）"
    assert n["in_scene"] == "贝克街221B", "叙述落在当前所在场景（单一空间键，已无对话圈）"
    assert n["content"] == _NARRATION
    assert isinstance(n["id"], int) and n["id"] > 0

    rep = eng.narration_state()["last_report"]
    assert rep["repeat"] >= 0.5 and "角色在复读" in rep["reasons"]

    for _ in range(2):                     # 4/5 块：冷却期内不再叙述
        await eng.step(1)
    assert len(_narrators(await eng.messages())) == 1
    await eng.step(1)                      # 第 6 块：下一个 window 到点 → 第二条
    assert len(_narrators(await eng.messages())) == 2


async def test_auto_narrate_off_still_allows_manual(tmp_path):
    """auto_narrate=False：自动档彻底不出手；手动 narrate_now() 照样能推一步。"""
    eng = _build(tmp_path, auto_narrate=False)
    await eng.open_scene()
    for _ in range(6):
        await eng.step(1)
    assert _narrators(await eng.messages()) == []
    assert eng.narration_state()["auto"] is False

    msg = await eng.narrate_now()
    assert msg is not None and msg["content"] == _NARRATION
    assert len(_narrators(await eng.messages())) == 1


async def test_manual_narration_ignores_cooldown(tmp_path):
    """手动推进无视 cooldown：刚叙述完立刻再点，仍会再出一条。"""
    eng = _build(tmp_path)
    await eng.open_scene()
    await eng.narrate_now()
    await eng.step(1)                      # 只隔一块（< cooldown）
    again = await eng.narrate_now()
    assert again is not None
    assert len(_narrators(await eng.messages())) == 2


async def test_retract_removes_narration_from_all_views(tmp_path, monkeypatch):
    """撤销叙述：下一块 think 的视图与叙述者自己的视图都不再含它（后续轮次看不见）。"""
    eng = _build(tmp_path)
    await eng.open_scene()
    for _ in range(3):
        await eng.step(1)
    mid = _narrators(await eng.messages())[0]["id"]

    think_views: dict[str, str] = {}
    narrate_views: list[str] = []
    from harness.prompters import build_narrate_messages as real_narrate
    from harness.prompters import build_think_messages as real_think

    def _think_spy(card, view_text, last_chunk_text, scene_text, **kw):
        think_views[card.name] = view_text
        return real_think(card, view_text, last_chunk_text, scene_text, **kw)

    def _narrate_spy(view_text, scene_text, clock_text, reason_text, **kw):
        # **kw：S4b 起引擎还传场景四字段（背景/描述/剧情/语言指令），打桩只关心视图。
        narrate_views.append(view_text)
        return real_narrate(view_text, scene_text, clock_text, reason_text, **kw)

    monkeypatch.setattr(graph_mod, "build_think_messages", _think_spy)
    monkeypatch.setattr("harness.engine.build_narrate_messages", _narrate_spy)

    await eng.retract(mid)
    await eng.step(1)                                  # 撤销后的下一次感知
    assert think_views, "撤销后仍应有 think 发生"
    for name, view in think_views.items():
        assert _NARRATION not in view, f"{name} 的视图仍能看到被撤销的叙述"
        assert f"[{mid}]" not in view
    st = await eng._snapshot()
    assert_public_only(st)
    assert mid in st["retracted"] and st["retracted"] == [mid]
    assert _NARRATION not in [m["content"] for m in _live_messages(st)]

    # 叙述者自己也看不到被撤销的那条（同一次叙述产出的提示词里不得有它）
    await eng.narrate_now()
    assert narrate_views and all(_NARRATION not in v for v in narrate_views)
    assert all(f"[{mid}]" not in v for v in narrate_views)
    # 手动再推的那条是**新 id**，与撤销的旧行互不相干
    live_ids = [m["id"] for m in _live_messages(await eng._snapshot())]
    assert mid not in live_ids and len(live_ids) == len(set(live_ids))


async def test_narration_never_leaks_into_private_channel(tmp_path):
    """叙述是公开行，绝不混进 think 只读边通道（私有解读）与角色印象文件。"""
    eng = _build(tmp_path)
    await eng.open_scene()
    for _ in range(3):
        await eng.step(1)
    assert len(_narrators(await eng.messages())) == 1

    log = eng.think_log_tail()
    assert log, "think 边通道应有本场条目"
    for entry in log:
        assert entry["heard"] != _NARRATION, "叙述不得充当某人的私有解读输入"
        assert _NARRATION not in json.dumps(entry["result"], ensure_ascii=False)

    for name in ("甲", "乙"):
        imp_dir = eng.run_root / name / "impressions"
        if imp_dir.is_dir():
            for p in imp_dir.glob("*.md"):
                assert _NARRATION not in p.read_text(encoding="utf-8")


async def test_edit_narration_replaces_text_with_new_id(tmp_path):
    """改写 = 撤销原行 + 追加新行（新 id）：旧 id 进 retracted，正文只剩新的。"""
    eng = _build(tmp_path)
    await eng.open_scene()
    for _ in range(3):
        await eng.step(1)
    old = _narrators(await eng.messages())[0]

    new = await eng.edit_narration(old["id"], "服务员把账单压在杯底，又走开了。")
    assert new["id"] != old["id"] and new["speaker_type"] == "narrator"
    assert new["content"] == "服务员把账单压在杯底，又走开了。"

    st = await eng._snapshot()
    assert st["retracted"] == [old["id"]]
    live = _live_messages(st)
    assert new["content"] in [m["content"] for m in live]
    assert _NARRATION not in [m["content"] for m in live]
    assert eng.narration_state()["narration_ids"] == [old["id"], new["id"]]


async def test_narration_trimmed_to_two_lines_and_short(tmp_path):
    """后端多写就是跑题：落盘的叙述恒 ≤2 行、≤120 字（硬约束，不靠提示词自觉）。"""
    eng = _build(tmp_path)
    long_line = "一句话" * 100
    eng.narrate_backend = StubBackend(line_script=[
        f"\n第一行。\n\n第二行。\n第三行。\n{long_line}\n"])
    await eng.open_scene()
    msg = await eng.narrate_now()
    assert msg is not None
    assert msg["content"].splitlines() == ["第一行。", "第二行。"]
    assert len(msg["content"]) <= 120


async def test_narration_backend_failure_is_swallowed(tmp_path):
    """叙述后端炸了也绝不把异常带进场景：本块当作没发生，场景照常继续。"""
    class _Boom:
        async def complete_text(self, messages):
            raise RuntimeError("boom")

    eng = _build(tmp_path)
    eng.narrate_backend = _Boom()
    await eng.open_scene()
    await eng.step(1)
    assert await eng.narrate_now() is None
    assert not _narrators(await eng.messages())
    await eng.step(1)                      # 引擎仍能继续跑（异常没进主循环）
    assert eng._narration_error and "boom" in eng._narration_error


async def test_narrate_role_falls_back_to_speak_backend(tmp_path):
    """旧 models.yaml 没有 narrate 档 → 退用 speak 档（老配置一字不改照跑）。"""
    (tmp_path / "贝克街221B.json").write_text(json.dumps({
        "name": "贝克街221B", "participants": ["甲", "乙"]}, ensure_ascii=False),
        encoding="utf-8")
    for name in ("甲", "乙"):
        (tmp_path / f"{name}.json").write_text(json.dumps(
            {"name": name, "personality": {"描述": "测试"}}, ensure_ascii=False),
            encoding="utf-8")
    (tmp_path / "models.yaml").write_text(
        "think:\n  backend: stub\n  model: stub\n  params: {}\n"
        "speak:\n  backend: stub\n  model: stub\n  params: {}\n", encoding="utf-8")
    eng = SceneEngine(tmp_path / "贝克街221B.json",
                      [tmp_path / "甲.json", tmp_path / "乙.json"],
                      tmp_path / "models.yaml", run_root=tmp_path / "runs")
    assert eng.narrate_backend is eng.speak_backend


def test_nudge_params_accepts_dict_and_dataclass(tmp_path):
    """nudge_params 接受 dict（GUI 传 JSON 配置）或 SceneNudgeParams 实例。"""
    from harness.scenarist import SceneNudgeParams
    eng = _build(tmp_path, nudge_params={"cooldown_blocks": 9, "threshold": 0.5})
    assert eng._nudge_params.cooldown_blocks == 9 and eng._nudge_params.threshold == 0.5
    (tmp_path / "b").mkdir()
    eng2 = _build(tmp_path / "b", nudge_params=SceneNudgeParams(stall_blocks=1))
    assert eng2._nudge_params.stall_blocks == 1
    assert eng2._nudge_params.repeat_window == 4, "未覆盖项保持缺省"
