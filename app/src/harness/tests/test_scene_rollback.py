"""撤回的**截断**语义（《界面与场景自由度》§6.1）：撤回一条推进 = 回溯到该节点。

旧语义只把被点的那一行从历史里剔除，它之后产出的正文照旧留在上下文里——于是「撤销
这条叙述」之后，角色仍带着由它衍生的一整段行动（以及他们各自的私有解读）继续演。
现在 retract(id) 是**截断式**撤回：撤回该条**及其之后同一场景内的所有消息**，并且
一切喂给模型的东西都要同意「那段下文从没存在过」：

  · 共享态 retracted 记下整段 id（去重、保序/单调）；
  · 角色视图（think/speak）与叙述者自己的视图都不再含被丢弃的任何一行（spy 提示词）；
  · think 只读边通道按轮次裁掉被回溯区间；
  · 私有记忆（state.jsonl + impressions jsonl/md）按轮次截断；
  · sidecar 存的是**未撤销**的转录（撤回要反映到存档里）。

edit_narration = 先截断到该节点，再追加改写后的新叙述（新 id）。

全部离线确定性：stub 后端（think/speak/narrate 三档）+ 零阈值竞价。
"""
import json
from pathlib import Path

import pytest

from harness import graph as graph_mod
from harness import scenestore
from harness.backends.stub import StubBackend
from harness.engine import SceneEngine
from harness.graph import _live_messages

#: 叙述产出（narrate 档）。它之后的正文（角色台词）才是「被丢弃的下文」。
_NARRATION = "隔壁桌有人站起来，椅子在瓷砖上刮出一声。"


def _think(urge: float) -> dict:
    """固定自报冲动 + 每次都留一条印象（印象也要按轮次截断）。"""
    return {"aroused": 0.5, "obligation_fulfilled": [], "goal_progress": 0.0,
            "addressed": None, "impression_of_speaker": "（印象。）", "urge": urge}


def _build(tmp_path: Path, **kw) -> SceneEngine:
    """二人场 + 三档 stub：每块必有一句角色台词（括号占位，不参与复读判定）。"""
    (tmp_path / "餐厅.json").write_text(json.dumps({
        "name": "餐厅", "participants": ["甲", "乙"],
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
    eng = SceneEngine(tmp_path / "餐厅.json",
                      [tmp_path / "甲.json", tmp_path / "乙.json"],
                      tmp_path / "models.yaml", run_root=tmp_path / "runs",
                      bid_path=bid, closing_at_block=200, **kw)
    eng.think_backend = StubBackend(json_script=[_think(2.0)])
    eng.speak_backend = StubBackend(line_script=[f"（第{i}句占位台词。）"
                                                 for i in range(60)])
    eng.narrate_backend = StubBackend(line_script=[_NARRATION])
    return eng


def _narrators(msgs: list[dict]) -> list[dict]:
    return [m for m in msgs if m.get("speaker_type") == "narrator"]


async def _step_until(eng: SceneEngine, count: int, limit: int = 20) -> list[dict]:
    """步进到共享态有 count 条消息为止（首块是聆听块，静默不产出台词）。"""
    for _ in range(limit):
        msgs = await eng.messages()
        if len(msgs) >= count:
            return msgs
        await eng.step(1)
    raise AssertionError(f"前置：步进 {limit} 块仍未达到 {count} 条消息")


async def _run(tmp_path: Path, **kw) -> tuple[SceneEngine, dict]:
    """开场 → 两块角色台词（id1/2）→ **手动**推进出一条叙述（id3）→ 再跑两块（id4/5）。

    叙述走 narrate_now 而不是等判据，是为了让「撤回点」确定：判据的触发时机取决于复读/
    停滞分量，用它会让本文件的 id 断言变得脆。这里要测的是**撤回的截断语义**，不是判据。
    """
    eng = _build(tmp_path, **kw)
    await eng.open_scene()
    await _step_until(eng, 3)
    narr = await eng.narrate_now()
    assert narr is not None and narr["id"] == 3, "前置：叙述恰在 id 3"
    await _step_until(eng, 6)
    msgs = await eng.messages()
    assert [m["id"] for m in msgs] == [0, 1, 2, 3, 4, 5], f"前置：id 顺延，实得 {msgs}"
    assert len(_narrators(msgs)) == 1, "前置：只有那一条叙述"
    return eng, narr


def _impressions(eng: SceneEngine) -> dict[tuple[str, str], list[dict]]:
    """{（本人, 他人）: [记录…]}：直读磁盘上的印象 jsonl（真相源）。"""
    out: dict[tuple[str, str], list[dict]] = {}
    for name in ("甲", "乙"):
        folder = eng.run_root / name / "impressions"
        if not folder.is_dir():
            continue
        for p in sorted(folder.glob("*.jsonl")):
            rows = [json.loads(line) for line in
                    p.read_text(encoding="utf-8").splitlines() if line.strip()]
            out[(name, p.stem)] = rows
    return out


# ===========================================================================
# 截断：id 与共享态
# ===========================================================================
async def test_retract_truncates_that_message_and_every_later_one(tmp_path):
    """撤回中段的叙述 → 它**及其之后的每一条**都进 retracted（同一场景按 id 顺延）。

    改前：只撤回那一条，其后的角色台词仍留在历史里（他们照旧带着「叙述发生过」的
    前提继续演）。"""
    eng, narr = await _run(tmp_path)
    msgs = await eng.messages()
    ids = [m["id"] for m in msgs]
    assert ids == [0, 1, 2, 3, 4, 5], f"前置：id 顺延，实得 {ids}"
    dropped_ids = [i for i in ids if i >= narr["id"]]
    dropped_text = [m["content"] for m in msgs if m["id"] >= narr["id"]]

    retracted = await eng.retract(int(narr["id"]))

    assert sorted(retracted) == dropped_ids, "retract 应报出被截断的全部 id"
    st = await eng._snapshot()
    assert st["retracted"] == dropped_ids, "retracted 记的是整段（去重、单调）"
    assert [m["id"] for m in _live_messages(st)] == [0, 1, 2]
    # 消息本体仍留在共享态（界面要能显示「这段作废了」），只是不再进任何上下文
    assert ids == [m["id"] for m in st["messages"]]
    for content in dropped_text:
        assert content not in [m["content"] for m in _live_messages(st)]


async def test_retract_is_idempotent_and_stays_monotonic(tmp_path):
    """重复撤回 / 更早地再撤一次：整段 id 恒为去重且单调（不重复、不倒序）。"""
    eng, narr = await _run(tmp_path)
    await eng.retract(int(narr["id"]))
    await eng.retract(int(narr["id"]))          # 幂等
    await eng.retract(2)                        # 更早的节点：截断范围更大
    st = await eng._snapshot()
    assert st["retracted"] == [2, 3, 4, 5]
    assert st["retracted"] == sorted(set(st["retracted"]))


async def test_retract_leaves_block_clock_and_narration_cooldown_forward(tmp_path):
    """撤回**不倒拨**世界钟与叙述冷却（决策记录）：块钟/turn 不回退，冷却不重置。

    理由：块钟是世界已经走过的历史（撤回的是"说过的话"，不是"过掉的时间"）；叙述冷却
    若被撤回重置，一次撤回就等于白送一次立即推进。唯一需要清理的是「被丢的那段下文
    留下的痕迹」——见 think 日志/私有记忆的按轮次截断。"""
    eng, narr = await _run(tmp_path)
    before = await eng._snapshot()
    cooldown_before = eng._blocks_since_narration
    blocks_before = int(before["blocks"])

    await eng.retract(int(narr["id"]))

    after = await eng._snapshot()
    assert int(after["blocks"]) == blocks_before, "块钟不回退"
    assert int(after["turn"]) == int(before["turn"]), "turn 不回退"
    assert eng._blocks_since_narration == cooldown_before, "叙述冷却照常往前走"
    await eng.step(1)                            # 撤回后照常继续推进
    assert eng._narration_error is None


# ===========================================================================
# 一切喂给模型的上下文都同意「那段下文没发生过」
# ===========================================================================
async def test_dropped_tail_absent_from_think_and_speak_prompts(tmp_path, monkeypatch):
    """spy 提示词：撤回后，任何听众的 think/speak 提示词都不得再含被丢弃的任何一行。"""
    eng, narr = await _run(tmp_path)
    msgs = await eng.messages()
    dropped = [m for m in msgs if m["id"] >= narr["id"]]
    dropped_contents = [m["content"] for m in dropped]
    assert len(dropped) == 3, "前置：叙述 + 其后两条台词"

    from harness.prompters import build_speak_messages as real_speak
    from harness.prompters import build_think_messages as real_think

    think_view: dict[str, str] = {}
    speak_view: dict[str, str] = {}

    def _think_spy(card, view_text, last_chunk_text, scene_text, **kw):
        think_view[card.name] = view_text
        return real_think(card, view_text, last_chunk_text, scene_text, **kw)

    def _speak_spy(card, view_text, scene_text, **kw):
        speak_view[card.name] = view_text
        return real_speak(card, view_text, scene_text, **kw)

    monkeypatch.setattr(graph_mod, "build_think_messages", _think_spy)
    monkeypatch.setattr(graph_mod, "build_speak_messages", _speak_spy)

    await eng.retract(int(narr["id"]))
    await eng.step(1)                            # 撤回后的下一次感知

    assert think_view, "撤回后仍应有 think 发生"
    for name, view in think_view.items():
        for line in dropped_contents:
            assert line not in view, f"{name} 的 think 视图仍能看到被丢弃的：{line}"
        for m in dropped:
            assert f"[{m['id']}]" not in view, f"{name} 的 think 视图仍有 [{m['id']}] 的编号"
    for name, view in speak_view.items():
        for line in dropped_contents:
            assert line not in view, f"{name} 的 speak 视图仍能看到被丢弃的：{line}"


async def test_narrator_view_omits_dropped_tail(tmp_path, monkeypatch):
    """叙述者自己（全知视图）同样不得再看到被丢弃的那段——否则它会照着旧文重推。"""
    eng, narr = await _run(tmp_path)
    msgs = await eng.messages()
    dropped_contents = [m["content"] for m in msgs if m["id"] >= narr["id"]]

    views: list[str] = []
    from harness.prompters import build_narrate_messages as real_narrate

    def _narrate_spy(view_text, scene_text, clock_text, reason_text, **kw):
        views.append(view_text)
        return real_narrate(view_text, scene_text, clock_text, reason_text, **kw)

    monkeypatch.setattr("harness.engine.build_narrate_messages", _narrate_spy)

    await eng.retract(int(narr["id"]))
    await eng.narrate_now()

    assert views
    for view in views:
        for line in dropped_contents:
            assert line not in view
        assert f"[{narr['id']}]" not in view


# ===========================================================================
# 只读边通道（think 日志）与私有记忆按轮次截断
# ===========================================================================
async def test_think_log_drops_entries_at_and_after_cut_turn(tmp_path):
    """think 只读日志按轮次裁掉被回溯区间：cut 轮及其后的私有解读一并消失。"""
    eng, narr = await _run(tmp_path)
    cut_turn = int(narr["turn"])
    log_before = eng.think_log_tail()
    assert any(int(e["turn"]) >= cut_turn for e in log_before), \
        "前置：cut 轮之后确实还有 think 条目（否则这条断言测不到东西）"

    await eng.retract(int(narr["id"]))

    log_after = eng.think_log_tail()
    assert log_after, "撤回之前的 think 条目必须保留"
    assert all(int(e["turn"]) < cut_turn for e in log_after)
    assert [e["seq"] for e in log_after] == [e["seq"] for e in log_before
                                             if int(e["turn"]) < cut_turn], \
        "只裁被回溯区间，不重编号、不动其余条目"


async def test_private_memory_truncated_by_turn(tmp_path):
    """私有记忆（state.jsonl）按轮次截断：cut 轮及其后写下的状态一并消失。"""
    eng, narr = await _run(tmp_path)
    cut_turn = int(narr["turn"])
    before = {name: memory_turns(eng, name) for name in ("甲", "乙")}
    assert any(t >= cut_turn for ts in before.values() for t in ts), \
        "前置：cut 轮之后确实写过状态"

    await eng.retract(int(narr["id"]))

    for name in ("甲", "乙"):
        turns = memory_turns(eng, name)
        assert turns, f"{name} 撤回前的状态必须保留"
        assert all(t < cut_turn for t in turns), f"{name} 残留了 cut 之后的私有状态"


def memory_turns(eng: SceneEngine, name: str) -> list[int]:
    """某人 state.jsonl 里各条目的轮次（缺 "turn" 的老条目按 0 计）。"""
    path = eng.run_root / name / "state.jsonl"
    if not path.exists():
        return []
    return [int(json.loads(line).get("turn", 0) or 0) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


async def test_impressions_truncated_by_turn_and_md_rerendered(tmp_path):
    """印象按轮次截断：cut 之后的记录从 jsonl 消失，人读 md 同步重写为剩余那批。"""
    eng, narr = await _run(tmp_path)
    cut_turn = int(narr["turn"])
    before = _impressions(eng)
    assert before, "前置：跑过块后应留下印象记录"
    assert any(r["turn"] >= cut_turn for rows in before.values() for r in rows)

    await eng.retract(int(narr["id"]))

    after = _impressions(eng)
    assert set(after) == set(before), "截断只删条目，不删「我对某人」这条关系"
    for key, rows in after.items():
        expected = [r for r in before[key] if r["turn"] < cut_turn]
        assert rows == expected, f"{key} 的印象应恰为 cut 之前的那批"
        assert all(r["turn"] < cut_turn for r in rows), f"{key} 残留了 cut 之后的印象"
        md = eng.run_root / key[0] / "impressions" / f"{key[1]}.md"
        assert md.read_text(encoding="utf-8") == "".join(f"{r['text']}\n" for r in rows), \
            "md 必须与截断后的 jsonl 一致（人读渲染不是第二份真相）"
    assert any(r["turn"] < cut_turn for rows in after.values() for r in rows), \
        "撤回之前的印象必须留着（不是把整份印象清空）"


# ===========================================================================
# 改写 = 截断 + 新行；sidecar 存的是未撤销的转录
# ===========================================================================
async def test_edit_narration_truncates_then_appends_rewrite(tmp_path):
    """改写 = 先回溯到该节点（其后正文全丢），再追加改写后的新叙述（新 id）。"""
    eng, narr = await _run(tmp_path)
    msgs = await eng.messages()
    dropped_ids = [m["id"] for m in msgs if m["id"] >= narr["id"]]

    new = await eng.edit_narration(int(narr["id"]), "服务员把账单压在杯底，又走开了。")

    st = await eng._snapshot()
    assert st["retracted"] == dropped_ids
    live = _live_messages(st)
    assert [m["id"] for m in live] == [0, 1, 2, new["id"]], "改写后的历史 = 截断 + 新行"
    assert new["content"] == "服务员把账单压在杯底，又走开了。"
    assert _NARRATION not in [m["content"] for m in live]
    assert [m["id"] for m in live if m["id"] in (4, 5)] == [], "被丢弃的下文不得复活"
    assert eng.narration_state()["narration_ids"] == [narr["id"], new["id"]]


async def test_sidecar_saves_live_transcript_after_rollback(tmp_path):
    """sidecar 存的是**未撤销**的转录：撤回后存档里不得再出现被丢弃的那段。"""
    eng, narr = await _run(tmp_path)
    msgs = await eng.messages()
    dropped_contents = [m["content"] for m in msgs if m["id"] >= narr["id"]]

    await eng.retract(int(narr["id"]))
    path = await eng.save_scene_state()

    assert path is not None and path == scenestore.runtime_path_for(eng._scene_file())
    bundle = scenestore.load_runtime(eng._scene_file())
    assert [m["id"] for m in bundle.transcript] == [0, 1, 2]
    for content in dropped_contents:
        assert content not in [m["content"] for m in bundle.transcript]


async def test_retracting_opening_line_clears_whole_scene(tmp_path):
    """撤到开场（id 0）→ 整场上下文清空（cut 轮之前的私有痕迹也一并截掉）。"""
    eng, _ = await _run(tmp_path)
    await eng.retract(0)
    st = await eng._snapshot()
    assert st["retracted"] == [0, 1, 2, 3, 4, 5]
    assert _live_messages(st) == []
    assert eng.think_log_tail() == []


@pytest.mark.parametrize("mid", [99])
async def test_retracting_unknown_id_does_not_blow_up(tmp_path, mid):
    """撤回一个不存在的 id：只记它自己（幂等退化），绝不抛、不牵连任何历史/记忆。

    尤其：它没有轮次可依，绝不能因此把谁的私有记忆一并截空。"""
    eng, _ = await _run(tmp_path)
    log_before = eng.think_log_tail()
    turns_before = {n: memory_turns(eng, n) for n in ("甲", "乙")}

    retracted = await eng.retract(mid)

    assert retracted == [mid]
    st = await eng._snapshot()
    assert st["retracted"] == [mid]
    assert [m["id"] for m in _live_messages(st)] == [0, 1, 2, 3, 4, 5]
    assert eng.think_log_tail() == log_before, "凭空一个 id 不该裁 think 日志"
    assert {n: memory_turns(eng, n) for n in turns_before} == turns_before, \
        "凭空一个 id 不该截断任何人的私有记忆"


async def test_dropped_tail_never_seeds_dynamics_or_think_heard(tmp_path):
    """被丢弃的下文不得再给任何人播种动态状态（邻接/欠答），也不得充当 think 的"听到"。

    动态演化（§7.4）与 think 的 heard 都只从**未撤销**的消息里取（_live_messages）；截断
    式撤回把整段下文一并撤掉，故那条点名行既不抬邻接、也不记欠答、更不会进任何人的
    私有解读——否则角色会为一句"没发生过的话"欠下回应义务。
    """
    eng = _build(tmp_path)
    await eng.open_scene()
    await _step_until(eng, 3)                       # id1/2：两条角色台词
    narr = await eng.narrate_now()                  # id3：叙述
    addressed = "（这句是说给甲听的。）"
    await eng._post_cast_line(addressed, address="甲")    # id4：点名行（被截断的那段）

    await eng.retract(int(narr["id"]))              # 撤 id3 → id4 一并作废
    await eng.step(1)                               # 撤回后的下一块

    snap = eng.dynamics_snapshot()["甲"]
    assert snap["adjacency"] == 0.0, "被丢弃的点名不得抬邻接"
    assert snap["pending_reply"] == 0.0, "被丢弃的点名不得记下欠答义务"
    assert all(e.get("heard") != addressed for e in eng.think_log_tail()), \
        "被丢弃的行不得充当任何人的 think 输入"
