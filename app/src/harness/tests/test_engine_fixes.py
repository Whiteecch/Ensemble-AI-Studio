"""最终评审修复轮（引擎核心）的回归测试：C1 / I1 / I2 / I3 / M1 / M2 / M3 / M4。

每一条都对着一个**改前必失败**的具体行为（修复前的失败形态写在各自 docstring 里）：

  C1  场景改名（钩子 scene_patch / `[[TOOL:set_name]]`）不得擦掉任何人的视图；
  I1  `open_scene()` 之后再 `restore_scene_state()`（GUI 的生产次序）不得落出重复 id；
  I2  续演后中途进场者的进场基线（看不到进场前的对话）必须还在；
  I3  `reset_scene_runtime()` 必须清掉私有 next-think 回喂（state.jsonl / impressions）；
  M1  `inject`/`say` 的 turn 取当前计数器，不沿用尾条（否则新人看不到在场时的话）；
  M2  被撤销的行不得给任何人的邻接/欠答播种；
  M3  对不在场者的临时禁言不随块钟流逝（等他进场才生效）；
  M4  `narrate_now()` 自己的钩子行不得缓冲进下一次 step 的事件流。

全部离线确定性：stub 三档后端 + 零阈值竞价（每块必有人开口，turn 与块钟同步演进）。
"""
import asyncio
import json
from pathlib import Path

from harness import scenestore
from harness.backends.stub import StubBackend
from harness.engine import SceneEngine
from harness.memory import CharacterMemory
from harness.schemas import Message
from harness.visibility import view_for


# --------------------------------------------------------------------- 素材 --
def _urge(urge: float = 2.0) -> dict:
    return {"aroused": 0.5, "obligation_fulfilled": [], "goal_progress": 0.0,
            "addressed": None, "impression_of_speaker": None, "urge": urge}


def _write(tmp_path: Path) -> tuple[Path, list[Path], Path, Path]:
    """场景「茶室」只声明 甲/乙；三张卡（甲/乙/丙）都装载——丙是「已装载但不在场」。"""
    (tmp_path / "茶室.json").write_text(json.dumps(
        {"name": "茶室", "participants": ["甲", "乙"],
         "description": "茶室里有一些桌椅。", "background": "民国年间，城南的一间茶室。",
         "plot_direction": "让两人渐渐谈到那封没寄出的信。"}, ensure_ascii=False),
        encoding="utf-8")
    cards = []
    for name in ("甲", "乙", "丙"):
        p = tmp_path / f"{name}.json"
        p.write_text(json.dumps({"name": name, "personality": {"描述": name}},
                                ensure_ascii=False), encoding="utf-8")
        cards.append(p)
    models = tmp_path / "models.yaml"
    models.write_text(
        "think:\n  backend: stub\n  model: stub\n  params: {}\n"
        "speak:\n  backend: stub\n  model: stub\n  params: {}\n"
        "narrate:\n  backend: stub\n  model: stub\n  params: {}\n", encoding="utf-8")
    bid = tmp_path / "bid.yaml"
    bid.write_text("interruption_threshold: 0.0\nspeak_threshold: 0.0\n"
                   "silence_k: 100000\n", encoding="utf-8")
    return tmp_path / "茶室.json", cards, models, bid


def _engine(tmp_path: Path, sub: str = "a", *, auto_narrate: bool = False,
            hooks: list[dict] | None = None, description_mutable: bool = False,
            narrate_lines: list[str] | None = None) -> SceneEngine:
    """（同一 tmp_path 下）新引擎：run_root 用 sub 区分，场景文件与 sidecar 共用。"""
    scene_p, cards, models_p, bid_p = _write(tmp_path)
    if hooks is not None or description_mutable:
        scene = json.loads(scene_p.read_text(encoding="utf-8"))
        scene["hooks"] = list(hooks or [])
        scene["description_mutable"] = bool(description_mutable)
        scene_p.write_text(json.dumps(scene, ensure_ascii=False), encoding="utf-8")
    eng = SceneEngine(scene_p, cards, models_p, run_root=tmp_path / sub,
                      bid_path=bid_p, auto_narrate=auto_narrate)
    # 括号内的占位台词 normalize 后为空串 → 永不被判「自我复读」，块块都有台词。
    eng.think_backend = StubBackend(json_script=[_urge()] * 60)
    eng.speak_backend = StubBackend(line_script=[f"（第{i}句占位台词。）"
                                                 for i in range(60)])
    eng.narrate_backend = _Scripted(narrate_lines) if narrate_lines is not None \
        else StubBackend(line_script=["（场景没有新动静。）"])
    return eng


class _Scripted:
    """按次序吐固定产出的 narrate 替身；lines 用完后重复最后一条。"""

    def __init__(self, lines: list[str]) -> None:
        self.lines = list(lines)
        self.calls: list[list[dict]] = []

    async def complete_text(self, messages: list[dict]) -> str:
        self.calls.append(messages)
        return self.lines[min(len(self.calls) - 1, len(self.lines) - 1)]


def _spy_views(monkeypatch) -> dict:
    """打桩 graph.build_think_messages → {听众: (view_text, last_chunk_text)}。"""
    from harness import graph as graph_mod
    from harness.prompters import build_think_messages as _real

    seen: dict[str, tuple[str, str]] = {}

    def _spy(card, view_text, last_chunk_text, scene_text, **kw):
        seen[card.name] = (view_text, last_chunk_text)
        return _real(card, view_text, last_chunk_text, scene_text, **kw)

    monkeypatch.setattr(graph_mod, "build_think_messages", _spy)
    return seen


def _msg_ids(view_text: str) -> list[int]:
    """视图文本里 `[id] ` 开头的行号（render_view 的行约定）。"""
    out = []
    for line in view_text.splitlines():
        if line.startswith("[") and "] " in line:
            head = line[1:line.index("]")]
            if head.isdigit():
                out.append(int(head))
    return out


def _views(msgs: list[dict], name: str, space: str, since_round: int | None = None):
    return view_for([Message.model_validate(m) for m in msgs], name, space,
                    since_round=since_round)


# ===========================================================================
# C1 —— 改名（钩子 / 工具）不得擦掉任何人的视图
# ===========================================================================
def test_c1_rename_via_hook_keeps_every_view(tmp_path: Path, monkeypatch):
    """钩子改场景名之后，下一块里每个人的视图照旧（改前：改名后视图全空）。"""
    hook = {"id": "h1", "condition": "总是成立", "event_kind": "scene",
            "scene_patch": {"name": "新茶室"}, "visible": False}
    eng = _engine(tmp_path, auto_narrate=True, hooks=[hook],
                  narrate_lines=["叙述。\n[[HOOK:h1]]"])
    seen = _spy_views(monkeypatch)
    asyncio.run(eng.open_scene())
    asyncio.run(eng.step(1))              # 本块末钩子把**显示名**改掉
    assert eng.scene.name == "新茶室", "前置：钩子已经改过名"
    msgs = asyncio.run(eng.messages())
    before = [m["id"] for m in msgs]
    assert before and 0 in before, "前置：改名之前有历史（含开场行）"
    assert all(m["in_scene"] == "茶室" for m in msgs), "历史都落在空间键上"

    seen.clear()
    asyncio.run(eng.step(1))              # 改名后的这一块：视图必须一条不少
    for name in ("甲", "乙"):
        ids = _msg_ids(seen[name][0])
        assert set(before) <= set(ids), \
            f"{name} 改名前的行丢了（{before} → {ids}）"
        assert 0 in ids, "开场行也必须在：空间键没跟着显示名跑"


def test_c1_rename_via_tool_keeps_history_visible(tmp_path: Path, monkeypatch):
    """`[[TOOL:set_name]]` 只改显示名：下一块的视图照旧（历史一条不少）。"""
    eng = _engine(tmp_path, description_mutable=True,
                  narrate_lines=["叙述一。\n[[TOOL:set_name]] 新茶室"])
    seen = _spy_views(monkeypatch)
    asyncio.run(eng.open_scene())
    asyncio.run(eng.step(1))
    asyncio.run(eng.narrate_now())        # 手动推进 → 场景工具改显示名
    msgs = asyncio.run(eng.messages())
    before = [m["id"] for m in msgs]
    assert eng.scene.name == "新茶室", "工具改了显示名"
    assert eng._ctx.space == "茶室", "空间键必须钉在构造时捕获的 id 上"
    assert msgs[0]["in_scene"] == "茶室", "开场行落在空间键上"

    seen.clear()
    asyncio.run(eng.step(1))              # 改名后的这一块
    for name in ("甲", "乙"):
        ids = _msg_ids(seen[name][0])
        assert set(before) <= set(ids), f"{name} 改名前的行丢了（{before} → {ids}）"


def test_c1_legacy_archive_with_old_space_key_is_adopted(tmp_path: Path):
    """旧存档（改名之前落下的 in_scene）不该因为本修复而变瞎：接受历史里那个键。"""
    eng = _engine(tmp_path, sub="a")
    asyncio.run(eng.open_scene())
    asyncio.run(eng.step(1))
    assert asyncio.run(eng.save_scene_state()) is not None
    path = eng._scene_file()
    bundle = scenestore.load_runtime(path)
    assert bundle.transcript
    # 模拟「C1 修复前改过名之后存的档」：历史全带旧名。
    bundle.transcript = [{**m, "in_scene": "旧茶室"} for m in bundle.transcript]
    scenestore.save_runtime(path, bundle)

    fresh = _engine(tmp_path, sub="b")        # 场景文件里的 name 仍是「茶室」
    assert asyncio.run(fresh.restore_scene_state()) is True
    assert fresh._scene_space == "旧茶室", "历史里唯一的那个键被接受为在场轴"
    assert fresh._ctx.space == "旧茶室"
    msgs = asyncio.run(fresh.messages())
    assert [m.id for m in _views(msgs, "甲", fresh._ctx.space)] == [m["id"] for m in msgs], \
        "旧存档的历史照旧全部可见"


# ===========================================================================
# I1 —— open_scene() 之后再 restore_scene_state()：id 必须唯一
# ===========================================================================
def test_i1_open_then_restore_keeps_ids_unique(tmp_path: Path):
    """GUI 的生产次序：先开场（写 id 0）再续演——存档里那份 id 0 不得重复追加。"""
    eng = _engine(tmp_path, sub="a")
    asyncio.run(eng.open_scene("（开场白。）"))
    asyncio.run(eng.step(2))
    assert asyncio.run(eng.save_scene_state()) is not None
    saved = scenestore.load_runtime(eng._scene_file())
    assert saved.transcript[0]["id"] == 0, "前置：存档里也有那条 id 0 的开场"

    fresh = _engine(tmp_path, sub="b")
    asyncio.run(fresh.open_scene("（新引擎的开场白。）"))     # GUI 先开场
    assert asyncio.run(fresh.restore_scene_state()) is True
    msgs = asyncio.run(fresh.messages())
    ids = [m["id"] for m in msgs]
    assert len(ids) == len(set(ids)), f"id 必须全局唯一，实际 {ids}"
    openings = [m for m in msgs if m["id"] == 0]
    assert len(openings) == 1, "开场行只能有一条（id 0 不重复追加）"
    assert openings[0]["content"] == "（新引擎的开场白。）", "实时态的那份为准"
    # 存档的其余行照旧追回（原 id 保留）
    assert all(i in ids for i in [m["id"] for m in saved.transcript if m["id"] != 0])
    # 下一条消息的 id 仍 = 全量最大 + 1（_next_id 连续性不破）
    asyncio.run(fresh.say("喂。"))
    tail = asyncio.run(fresh.messages())[-1]
    assert tail["id"] == max(ids) + 1


# ===========================================================================
# I2 —— 进场基线跨会话保留
# ===========================================================================
def test_i2_entry_baseline_survives_save_and_restore(tmp_path: Path, monkeypatch):
    """中途进场者 → 保存 → 新引擎开场 + 续演：他仍旧看不到进场前的任何一行。"""
    eng = _engine(tmp_path, sub="a")
    seen = _spy_views(monkeypatch)
    asyncio.run(eng.open_scene())
    asyncio.run(eng.step(2))
    pre_entry_ids = {m["id"] for m in asyncio.run(eng.messages())}
    asyncio.run(eng.add_character("丙"))
    baseline = eng._entry_round_of("丙")
    assert baseline > 0, "前置：丙的进场基线在对话推进之后"
    asyncio.run(eng.save_scene_state())
    assert scenestore.load_runtime(eng._scene_file()).meta.get("entry_round") \
        == {"丙": baseline}, "进场基线必须落进 sidecar"

    fresh = _engine(tmp_path, sub="b")
    asyncio.run(fresh.open_scene())
    assert asyncio.run(fresh.restore_scene_state()) is True
    assert fresh._entry_round_of("丙") == baseline, "续演把基线套回来"
    seen.clear()
    asyncio.run(fresh.step(1))
    ids = _msg_ids(seen["丙"][0])
    assert ids, "丙进场后必须能看见进场之后的行"
    assert not (set(ids) & pre_entry_ids), \
        f"进场前的行一律不可见（视图 {ids}，进场前 {sorted(pre_entry_ids)}）"


# ===========================================================================
# I3 —— 重置清掉私有 next-think 回喂
# ===========================================================================
def test_i3_reset_clears_private_think_feedback(tmp_path: Path):
    """重置后 dynamic_states() 必须读空（改前：state.jsonl 还在，旧值原样返回）。"""
    eng = _engine(tmp_path)
    asyncio.run(eng.open_scene())
    asyncio.run(eng.step(2))
    assert eng.dynamic_states(), "前置：跑过块后每个在场者都有私有 think 状态"
    assert eng.think_log_tail(), "前置：think 边通道非空"

    asyncio.run(eng.reset_scene_runtime())

    assert eng.dynamic_states() == {}, "重置必须清掉私有回喂源（state.jsonl 截断）"
    assert eng.think_log_tail() == [], "只读边通道同样清零"
    asyncio.run(eng.step(1))              # 重置后照常继续
    assert eng.dynamic_states(), "新的一块照常写入新的私有状态"


def test_character_memory_clear_is_idempotent(tmp_path: Path):
    """CharacterMemory.clear()：截断状态流 + 删光印象文件；重复调用/空目录都不抛。"""
    mem = CharacterMemory(tmp_path / "runs", "甲")
    mem.append_state({"urge": 1.0})
    mem.append_impression("乙", "我觉得他怪怪的。")
    mem.append_visible({"id": 1, "content": "（历史。）"})
    assert mem.load_state() and mem.read_impressions()

    mem.clear()

    assert mem.load_state() == []
    assert mem.read_impressions() == {}
    assert mem.read_visible(), "可见转录是历史存档，不清"
    mem.clear()                          # 幂等（文件已不在）


# ===========================================================================
# M1 —— inject/say 的 turn 取当前计数器
# ===========================================================================
def test_m1_say_after_invisible_add_uses_current_turn(tmp_path: Path):
    """隐式进场后 say：这句必须落在当前轮，新人看得见（改前：沿用尾条旧 turn → 看不到）。"""
    eng = _engine(tmp_path)
    asyncio.run(eng.open_scene())
    asyncio.run(eng.step(2))
    prev_turn = asyncio.run(eng.messages())[-1]["turn"]
    asyncio.run(eng.add_character("丙", visible=False))
    baseline = eng._entry_round_of("丙")
    assert baseline > prev_turn, "前置：基线要跑在尾条 turn 之前，才检验得出 M1"

    asyncio.run(eng.say("这是我说的。"))
    line = asyncio.run(eng.messages())[-1]
    assert line["turn"] >= baseline, "插话落在当前轮，不沿用尾条的旧 turn"
    ids = [m.id for m in _views(asyncio.run(eng.messages()), "丙", eng._ctx.space,
                                since_round=baseline)]
    assert line["id"] in ids, "在场的新人必须看得见这句插话"


def test_m1_inject_uses_current_turn(tmp_path: Path):
    """导演插话同理：turn 取当前计数器（否则刚进场者看不到世界事件）。"""
    eng = _engine(tmp_path)
    asyncio.run(eng.open_scene())
    asyncio.run(eng.step(2))
    prev_turn = asyncio.run(eng.messages())[-1]["turn"]
    asyncio.run(eng.add_character("丙", visible=False))
    baseline = eng._entry_round_of("丙")
    assert baseline > prev_turn

    asyncio.run(eng.inject("（窗外打了一个响雷。）"))
    line = asyncio.run(eng.messages())[-1]
    assert line["turn"] >= baseline
    ids = [m.id for m in _views(asyncio.run(eng.messages()), "丙", eng._ctx.space,
                                since_round=baseline)]
    assert line["id"] in ids


# ===========================================================================
# M2 —— 被撤销的行不得给人播种动态状态
# ===========================================================================
def test_m2_retracted_line_does_not_seed_dynamics(tmp_path: Path):
    """点名某人的行被撤销后，不得再抬他的邻接/欠答（改前：共享尾条照读）。"""
    eng = _engine(tmp_path)
    asyncio.run(eng.open_scene())
    asyncio.run(eng.step(1))
    asyncio.run(eng.add_character("丙", notify=["甲"], notify_text="（悄悄话。）"))
    tail = asyncio.run(eng.messages())[-1]
    assert tail.get("address") == "甲", "前置：通知行点名甲（撤销前它会抬高甲的邻接）"
    asyncio.run(eng.retract(tail["id"]))
    before = eng.dynamics_snapshot()["甲"]["adjacency"]
    assert before == 0.0, "前置：此前没人被点过名"

    asyncio.run(eng.step(1))

    snap = eng.dynamics_snapshot()["甲"]
    assert snap["adjacency"] == 0.0, "被撤销的点名不得抬邻接"
    assert snap["pending_reply"] == 0.0, "被撤销的点名不得记下欠答义务"


# ===========================================================================
# M3 —— 对不在场者的临时禁言等进场才生效
# ===========================================================================
def test_m3_mute_absent_character_waits_until_entry(tmp_path: Path):
    """禁言不在场者：缺席期间不扣块（改前：几块之后禁言早已归零，永远不生效）。"""
    eng = _engine(tmp_path)
    asyncio.run(eng.open_scene())
    asyncio.run(eng.mute_character("丙", 2))         # 丙 还没进场
    asyncio.run(eng.step(3))
    asyncio.run(eng.add_character("丙"))
    assert eng.cast_state()["muted"] == {"丙": 2}, "缺席期间不扣，进场时满格生效"
    assert "丙" not in eng.speakable_names()

    asyncio.run(eng.step(1))
    assert eng.cast_state()["muted"] == {"丙": 1}, "在场后才开始计时"
    asyncio.run(eng.step(1))
    assert eng.cast_state()["muted"] == {}, "在场满 2 块自动解禁"
    assert "丙" in eng.speakable_names()


# ===========================================================================
# M4 —— 手动推进的钩子行不缓冲进下一次 step
# ===========================================================================
def test_m4_narrate_now_hook_lines_stay_out_of_next_step(tmp_path: Path):
    """narrate_now 的钩子行由 messages 上屏，不得再进下一块的事件流（会显示两遍）。"""
    hook = {"id": "h1", "condition": "有人提到那封信", "event_kind": "context",
            "context_text": "窗外忽然有人喊了一声。", "visible": True}
    eng = _engine(tmp_path, auto_narrate=False, hooks=[hook],
                  narrate_lines=["叙述。\n[[HOOK:h1]]"])
    asyncio.run(eng.open_scene())
    msg = asyncio.run(eng.narrate_now())
    assert msg["content"] == "叙述。"
    assert any(m["content"] == "窗外忽然有人喊了一声。"
               for m in asyncio.run(eng.messages())), "钩子行已落进 messages"
    assert eng._hook_lines == [], "手动路径自己收尾，不留缓冲"

    events = asyncio.run(eng.step(1))
    spoken = [e["payload"]["content"] for e in events if e["type"] == "block_spoken"]
    assert "窗外忽然有人喊了一声。" not in spoken, \
        "手动推进的钩子行不得混进下一块的事件流"
