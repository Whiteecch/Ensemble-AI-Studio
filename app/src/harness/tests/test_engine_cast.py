"""引擎侧可插拔演员表（《场景编排与桌面外壳》§3.3 / §4.1②，S4a）。

覆盖四件事：
  1. 在场/离场切换与播报行（显式落行、隐式只改状态、幂等、缺卡报错、通知行）；
  2. 离场/禁言者退出 think 扇出与竞价（用 build_think_messages 打桩记录每人收到的视图）；
  3. 临时禁言 N 块到期自动解禁、永久禁言直到解禁；
  4. 延时角色动作恰在第 N 次 step 到期；进场者看不到进场前的对话。

全部离线确定性：stub 后端 + 零阈值竞价（每块必有人开口，turn 与块钟同步演进）。
"""
import asyncio
import json
from pathlib import Path

import pytest

from harness.backends.stub import StubBackend
from harness.engine import SceneEngine


# --------------------------------------------------------------------- 素材 --
def _urge(urge: float) -> dict:
    return {"aroused": 0.5, "obligation_fulfilled": [], "goal_progress": 0.0,
            "addressed": None, "impression_of_speaker": None, "urge": urge}


def _write(tmp_path: Path, *, scene_cast: list[str] | None = None) -> tuple[Path, list[Path], Path, Path]:
    """场景只声明 甲/乙；三张卡（甲/乙/丙）都在引擎里装载——丙是「已装载但不在场」。

    丙卡在场外可用，正是「运行期加人」（§3.3）要覆盖的形状：库里/装载里有卡，场景
    名单里没有。scene_cast=[] 写一份**空场**（§3.1：场景没存过阵容），此时
    cast_from_cards=True 才会用所选卡播种（新口径见《界面与场景自由度》§3.1）。
    """
    cast = ["甲", "乙"] if scene_cast is None else list(scene_cast)
    (tmp_path / "茶室.json").write_text(json.dumps(
        {"name": "茶室", "characters": [{"name": n} for n in cast]},
        ensure_ascii=False),
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
        "speak:\n  backend: stub\n  model: stub\n  params: {}\n", encoding="utf-8")
    # 阈值全零 + 高 urge：每块必有人开口（turn 与块钟同步，进场基线可断言）
    bid = tmp_path / "bid.yaml"
    bid.write_text("interruption_threshold: 0.0\nspeak_threshold: 0.0\n"
                   "silence_k: 100000\n", encoding="utf-8")
    return tmp_path / "茶室.json", cards, models, bid


def _engine(tmp_path: Path, *, cast_from_cards: bool = False,
            scene_cast: list[str] | None = None) -> SceneEngine:
    scene_p, cards, models_p, bid_p = _write(tmp_path, scene_cast=scene_cast)
    eng = SceneEngine(scene_p, cards, models_p, run_root=tmp_path / "runs",
                      bid_path=bid_p, auto_narrate=False,
                      cast_from_cards=cast_from_cards)
    # 括号内的占位台词 normalize 后为空串 → 永不被判「自我复读」，块块都有台词。
    eng.think_backend = StubBackend(json_script=[_urge(2.0)] * 60)
    eng.speak_backend = StubBackend(line_script=[f"（第{i}句占位台词。）" for i in range(60)])
    return eng


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
    """视图文本里出现过 `[id] ` 开头的行号（render_view 的行约定）。"""
    out = []
    for line in view_text.splitlines():
        if line.startswith("[") and "] " in line:
            head = line[1:line.index("]")]
            if head.isdigit():
                out.append(int(head))
    return out


def _speakers(msgs: list[dict], after_id: int = -1) -> list[str]:
    return [m["speaker"] for m in msgs
            if m.get("speaker_type") == "character" and m["id"] > after_id]


# -------------------------------------------------- 1. 在场/离场与播报行 ------
def test_cast_clearing_everyone_is_safe(tmp_path: Path):
    """把在场者全移出：空场景照常走块钟（§3.3），左右栏清空，不崩不报错。"""
    eng = _engine(tmp_path)
    asyncio.run(eng.open_scene())
    asyncio.run(eng.step(1))
    asyncio.run(eng.remove_character("甲"))
    asyncio.run(eng.remove_character("乙"))
    assert eng.cast_state()["active"] == []
    assert eng.speakable_names() == []
    assert eng.dynamics_snapshot() == {}, "空场无人在左右栏"
    events = asyncio.run(eng.step(2))
    assert [e["payload"]["kind"] for e in events if e["type"] == "decision"] == [
        "block_done", "block_done"], "空场块钟照常推进"
    assert not _speakers(asyncio.run(eng.messages()))


def test_cast_add_and_remove_toggle_state_and_lines(tmp_path: Path):
    eng = _engine(tmp_path)
    asyncio.run(eng.open_scene())
    assert eng.cast_state() == {"active": ["甲", "乙"], "inactive": [], "muted": {}}

    line = asyncio.run(eng.add_character("丙"))
    st = eng.cast_state()
    assert st["active"] == ["甲", "乙", "丙"] and st["inactive"] == []
    assert line["content"] == "（丙走进了场景。）"
    assert line["speaker"] == "场景" and line["speaker_type"] == "narrator"
    assert asyncio.run(eng.messages())[-1] == {**line}

    # 记录（持久化那一份）：追加一行 SceneCastMember，入场信息 = 当前块钟/当前钟点
    rec = next(m for m in eng.scene.characters if m.name == "丙")
    assert rec.entered_round == 0 and rec.entered_at == ""   # 引擎不知道钟 → 留空
    eng.set_clock_now(9 * 3600 + 30 * 60)                    # worker 注入 09:30
    asyncio.run(eng.remove_character("丙"))
    assert rec.entered_at == ""                              # 离场不改入场信息

    # 幂等：已在场再 add / 已离场再 remove 都不再播报
    assert asyncio.run(eng.add_character("丙")) is not None
    n = len(asyncio.run(eng.messages()))
    assert asyncio.run(eng.add_character("丙")) is None
    out = asyncio.run(eng.remove_character("丙"))
    assert out["content"] == "（丙离开了场景。）"
    assert asyncio.run(eng.remove_character("丙")) is None
    st = eng.cast_state()
    assert st["active"] == ["甲", "乙"] and st["inactive"] == ["丙"]
    assert [m.name for m in eng.scene.characters] == ["甲", "乙", "丙"], "记录绝不删"
    assert len(asyncio.run(eng.messages())) == n + 1         # 只有那条离场行

    # 再入场：仍用同一行记录，入场信息就地更新（时刻/块钟取当下）
    again = asyncio.run(eng.add_character("丙"))
    assert again["content"] == "（丙走进了场景。）"
    assert [m.name for m in eng.scene.characters] == ["甲", "乙", "丙"]
    assert next(m for m in eng.scene.characters if m.name == "丙").entered_at == "09:30"


def test_cast_implicit_change_appends_no_line(tmp_path: Path):
    """隐式（visible=False）：状态照改、上下文一行都不落（§4.2）。"""
    eng = _engine(tmp_path)
    asyncio.run(eng.open_scene())
    before = len(asyncio.run(eng.messages()))
    assert asyncio.run(eng.add_character("丙", visible=False)) is None
    assert eng.cast_state()["active"] == ["甲", "乙", "丙"]
    assert len(asyncio.run(eng.messages())) == before
    assert asyncio.run(eng.remove_character("丙", visible=False)) is None
    assert eng.cast_state()["active"] == ["甲", "乙"]
    assert len(asyncio.run(eng.messages())) == before


def test_cast_requires_card_in_library(tmp_path: Path):
    """卡既没装载、角色库里也没有 → ValueError（中文原因）；非法动作也当场拦下。

    （本 fixture 没给 characters_dir，故「库里也没有」= 无从装载；按需装卡的路径见
    tests/test_engine_free_cast.py。）
    """
    eng = _engine(tmp_path)
    with pytest.raises(ValueError, match="角色库里没有找到《幽灵》的角色卡"):
        asyncio.run(eng.add_character("幽灵"))
    with pytest.raises(ValueError, match="角色库里没有找到《幽灵》的角色卡"):
        asyncio.run(eng.remove_character("幽灵"))
    with pytest.raises(ValueError, match="角色库里没有找到《幽灵》的角色卡"):
        asyncio.run(eng.mute_character("幽灵"))
    with pytest.raises(ValueError, match="角色库里没有找到《幽灵》的角色卡"):
        asyncio.run(eng.schedule_cast_change("幽灵", "add", 1))
    with pytest.raises(ValueError, match="未知的角色动作"):
        asyncio.run(eng.schedule_cast_change("丙", "teleport", 1))


def test_cast_notification_line_only_for_listed_names(tmp_path: Path):
    """通知（§4.1②）：通知对象非空才落一条**只发给这些名字**的叙述行。"""
    eng = _engine(tmp_path)
    asyncio.run(eng.open_scene())
    asyncio.run(eng.add_character("丙", notify=["甲", "乙"], notify_text="丙来了，招呼一声"))
    msgs = asyncio.run(eng.messages())
    assert msgs[-1]["content"] == "丙来了，招呼一声"
    assert msgs[-1]["speaker_type"] == "narrator" and msgs[-1]["knows"] == ["甲", "乙"]
    assert msgs[-1]["address"] == "甲、乙"
    assert msgs[-2]["content"] == "（丙走进了场景。）"     # 播报行仍在，通知行在其后

    # notify_text 缺省 → 「（X 走了。）」；只有一个通知对象也照发
    asyncio.run(eng.remove_character("丙", notify=["甲"]))
    assert asyncio.run(eng.messages())[-1]["content"] == "（丙走了。）"

    # 隐式：连通知行也不落（隐式事件整体不上屏）
    before = len(asyncio.run(eng.messages()))
    asyncio.run(eng.add_character("丙", notify=["甲"], notify_text="悄悄来了", visible=False))
    assert len(asyncio.run(eng.messages())) == before


# ------------------------------------- 2. 离场/禁言者退出扇出与竞价 -------------
def test_cast_removed_character_leaves_fanout_and_floor(tmp_path: Path, monkeypatch):
    """在场三人 → 移出丙 → 丙不再 think、不再出现在任何后续台词里（历史保留）。"""
    # 空场 + cast_from_cards=True：所用卡播种进场景（新口径）→ 甲乙丙开场即全在场。
    eng = _engine(tmp_path, cast_from_cards=True, scene_cast=[])
    seen = _spy_views(monkeypatch)
    assert eng.active_names() == ["甲", "乙", "丙"]
    asyncio.run(eng.open_scene())
    asyncio.run(eng.step(2))
    assert "丙" in seen, "在场时丙照常 think"

    cut = max(m["id"] for m in asyncio.run(eng.messages()))
    asyncio.run(eng.remove_character("丙"))
    seen.clear()
    asyncio.run(eng.step(3))
    assert set(seen) == {"甲", "乙"}, "被移出者不得再收到 think payload"
    assert "丙" not in _speakers(asyncio.run(eng.messages()), cut), "被移出者不得再开口"
    # 记录/私有记忆仍在（历史不删）：scene.characters 保留丙，状态表也保留（再入场复用）
    assert "丙" in [m.name for m in eng.scene.characters]
    assert eng.cast_state()["inactive"] == ["丙"]
    assert "丙" in eng._dynamics.states


def test_cast_mute_temporary_blocks_fanout_then_auto_unmutes(tmp_path: Path, monkeypatch):
    """禁言 N 块：N 块内无 think、无 bid（话筒必落他人），第 N 块末自动解禁。"""
    eng = _engine(tmp_path)
    seen = _spy_views(monkeypatch)
    asyncio.run(eng.open_scene())
    asyncio.run(eng.mute_character("甲", 2))
    assert eng.cast_state()["muted"] == {"甲": 2}
    assert eng.speakable_names() == ["乙"]
    assert "甲" in eng.dynamics_snapshot(), "禁言者仍在场（左右栏要显示禁言中），bid 照算"

    cut = max(m["id"] for m in asyncio.run(eng.messages()))
    seen.clear()
    asyncio.run(eng.step(1))
    assert "甲" not in seen and "乙" in seen
    assert eng.cast_state()["muted"] == {"甲": 1}, "块末扣一格"
    seen.clear()
    asyncio.run(eng.step(1))
    assert "甲" not in seen
    assert eng.cast_state()["muted"] == {}, "满 2 块 → 自动解禁"
    # 禁言期间没有 bid → 话筒必落他人：禁言那两块里 甲 一句都没说过
    assert "甲" not in _speakers(asyncio.run(eng.messages()), cut)
    seen.clear()
    asyncio.run(eng.step(1))
    assert "甲" in seen, "解禁后回到扇出名单"


def test_cast_mute_indefinite_until_unmute(tmp_path: Path, monkeypatch):
    """turns=0 = 永久禁言：不随时间自动解除，只有 unmute_character 放行。"""
    eng = _engine(tmp_path)
    seen = _spy_views(monkeypatch)
    asyncio.run(eng.open_scene())
    asyncio.run(eng.mute_character("甲"))
    assert eng.cast_state()["muted"] == {"甲": None}
    for _ in range(3):
        seen.clear()
        asyncio.run(eng.step(1))
        assert "甲" not in seen
    assert eng.cast_state()["muted"] == {"甲": None}, "永久禁言不会被块钟扣掉"
    asyncio.run(eng.unmute_character("甲"))
    assert eng.cast_state()["muted"] == {}
    seen.clear()
    asyncio.run(eng.step(1))
    assert "甲" in seen


# ------------------------------------------- 3. 延时角色动作（§2.1④） -----------
def test_cast_schedule_fires_exactly_on_nth_step(tmp_path: Path):
    """fire_after_rounds=2：第 1 次 step 不动，恰好第 2 次 step 到期执行。"""
    eng = _engine(tmp_path)
    asyncio.run(eng.open_scene())
    rec = asyncio.run(eng.schedule_cast_change("丙", "add", 2,
                                               notify=["甲"], notify_text="丙稍后到"))
    assert rec["character_name"] == "丙" and rec["action"] == "add"
    assert [p["fire_after_rounds"] for p in eng.pending_cast_changes()] == [2]

    asyncio.run(eng.step(1))
    assert "丙" not in eng.cast_state()["active"], "第 1 块不该到期"
    assert [p["fire_after_rounds"] for p in eng.pending_cast_changes()] == [1]

    events = asyncio.run(eng.step(1))
    assert "丙" in eng.cast_state()["active"], "恰好第 2 次 step 到期"
    assert eng.pending_cast_changes() == []
    contents = [e["payload"]["content"] for e in events if e["type"] == "block_spoken"]
    assert "（丙走进了场景。）" in contents, "到期动作的播报行随事件流产出"
    # 通知行与播报行一样落进转录（事件流只带货真价实的那条播报行；界面靠 sweep 上屏）
    tail = [m["content"] for m in asyncio.run(eng.messages())[-2:]]
    assert tail == ["（丙走进了场景。）", "丙稍后到"]


def test_cast_schedule_mute_turns_and_remove(tmp_path: Path):
    """延时动作复用同一套语义：mute_turns 带轮数（S4a 增补参数）、remove 到点离场。"""
    eng = _engine(tmp_path)
    asyncio.run(eng.open_scene())
    asyncio.run(eng.schedule_cast_change("甲", "mute_turns", 1, turns=2))
    asyncio.run(eng.step(1))
    assert eng.cast_state()["muted"] == {"甲": 2}
    asyncio.run(eng.remove_character("乙"))
    asyncio.run(eng.schedule_cast_change("丙", "remove", 1))     # 不在场 → 到期空操作
    asyncio.run(eng.step(1))
    assert eng.cast_state()["active"] == ["甲"] and eng.pending_cast_changes() == []


# ------------------------------------------------ 4. 进场可见性（§3.3） ---------
def test_cast_entry_visibility_newcomer_sees_no_history(tmp_path: Path, monkeypatch):
    """跑 4 块后让丙进场：丙的视图/上一句都不含进场前任何一行，老面孔照旧看全量。

    （第 1 块恒为聆听块——竞价读的是上一块末折进来的数值状态，故 4 块 = 3 句台词，
    进场后丙的可见集正是 [4] 之后的那些行。）"""
    eng = _engine(tmp_path)
    seen = _spy_views(monkeypatch)
    asyncio.run(eng.open_scene())
    asyncio.run(eng.step(4))
    msgs = asyncio.run(eng.messages())
    top = max(m["id"] for m in msgs)
    assert top >= 3, "前置：开场后必须已经产出若干行（否则下面断言无意义）"
    assert _speakers(msgs), "前置：这几块里有人开过口（turn 才走得动）"

    asyncio.run(eng.add_character("丙"))
    assert next(m for m in eng.scene.characters if m.name == "丙").entered_round == 4

    seen.clear()
    asyncio.run(eng.step(2))
    view, last = seen["丙"]
    ids = _msg_ids(view)
    assert ids, "丙进场后必须能看见进场之后的行"
    assert all(i > top for i in ids), f"进场前的行一律不可见，实际 {ids}（进场前最大 id = {top}）"
    assert max(ids) > top, "进场后的新台词必须看得见"
    assert _msg_ids(last) and _msg_ids(last)[0] > top, "「此刻·上一句」同样不得早于进场"

    # 老面孔（基线 0）不受影响：开场行与进场前的每一行都还在
    for name in ("甲", "乙"):
        old_ids = _msg_ids(seen[name][0])
        assert 0 in old_ids and top in old_ids


def test_cast_reentry_hides_history_again(tmp_path: Path, monkeypatch):
    """离场再入场：基线刷新到「再入场那一刻」，中间发生过的对话同样看不到。"""
    eng = _engine(tmp_path, cast_from_cards=True, scene_cast=[])
    seen = _spy_views(monkeypatch)
    asyncio.run(eng.open_scene())
    asyncio.run(eng.step(1))
    asyncio.run(eng.remove_character("丙"))
    asyncio.run(eng.step(2))
    gap_top = max(m["id"] for m in asyncio.run(eng.messages()))
    asyncio.run(eng.add_character("丙"))

    seen.clear()
    asyncio.run(eng.step(1))
    ids = _msg_ids(seen["丙"][0])
    assert all(i > gap_top for i in ids), "再入场的丙看不到离场期间与更早的任何行"
