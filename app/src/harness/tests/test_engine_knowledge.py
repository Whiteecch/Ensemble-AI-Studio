"""引擎侧信息库接线（设计文档 §5 运行时副本 / §5.3 撤回一并截断 / §6 引擎接线）。

钉四件事：

  · **缺省无语感差异**：`libraries_root` 缺省 None → `self._knowledge is None`，
    ctx 上也必须是 None（图因此走今天那条老路，逐字节不变）；
  · **进场即建副本**（§5.1）：开场的初始阵容与运行中进场都建；副本已存在则**复用**
    （§5.1 的硬约束——他中途离场又回来，这一场学到的东西必须还在）；建副本失败
    （IO 错）绝不能打断开场/进场，记一笔即可；
  · **撤回一起截断**（§5.3 / §5.3.1）：对在册的全部人（在场 + 离场）调用工具层的
    `truncate_scene_after_turn` / `truncate_recall_after_turn`，cut 与既有
    `memory.truncate_after_turn` **同一口径**；**绝不**直接调存储层的
    `truncate_copy_after_turn`（那条只删条目、不做状态还原，会留下"被推翻的旧说法仍是
    现行"的坏形态）。

全程 `tmp_path`：素材、运行根、信息库根都在临时目录里，不碰真实用户数据。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from harness import knowledgestore as store
from harness.backends.stub import StubBackend
from harness.engine import SceneEngine
from harness.knowledge import Entry, EntryOrigin
from harness.knowledgetools import RECALL_FILENAME, KnowledgeAccess, TruncateReport
from harness.memory import CharacterMemory

SCENE = "茶室"
A, B, C = "甲", "乙", "丙"


# --------------------------------------------------------------------- 素材 --

def _urge(urge: float) -> dict:
    return {"aroused": 0.5, "obligation_fulfilled": [], "goal_progress": 0.0,
            "addressed": None, "impression_of_speaker": None, "urge": urge}


def _scene_json(*, cast=(A, B)) -> str:
    return json.dumps({"name": SCENE, "characters": [{"name": n} for n in cast]},
                      ensure_ascii=False)


def _write(tmp_path: Path, *, cast=(A, B)) -> tuple[Path, list[Path], Path, Path]:
    """场景（含阵容）+ 三张卡 + models.yaml + 零阈值 bid（每块必有人开口）。"""
    scene_p = tmp_path / f"{SCENE}.json"
    scene_p.write_text(_scene_json(cast=cast), encoding="utf-8")
    cards: list[Path] = []
    for name in (A, B, C):
        p = tmp_path / f"{name}.json"
        p.write_text(json.dumps({"name": name, "personality": {"描述": name}},
                                ensure_ascii=False), encoding="utf-8")
        cards.append(p)
    models = tmp_path / "models.yaml"
    models.write_text(
        "think:\n  backend: stub\n  model: stub\n  params: {}\n"
        "speak:\n  backend: stub\n  model: stub\n  params: {}\n", encoding="utf-8")
    bid = tmp_path / "bid.yaml"
    bid.write_text("interruption_threshold: 0.0\nspeak_threshold: 0.0\n"
                   "silence_k: 100000\n", encoding="utf-8")
    return scene_p, cards, models, bid


def _libroot(tmp_path: Path, character: str, entries=()) -> Path:
    """造一座角色本体库（正式布局 `characters/<角色名>`），返回信息库根。"""
    libs = tmp_path / "libraries"
    lib = store.Library(name=character, scope="character", owner=character)
    for entry in entries:
        lib.upsert(entry)
    store.save_library(lib, store.library_dir("character", character, root=libs))
    return libs


def _copy_dir(tmp_path: Path, character: str) -> Path:
    return store.scene_library_dir(tmp_path / "runs", character)


def _copy_keys(tmp_path: Path, character: str) -> list[str]:
    return list(store.load_library(_copy_dir(tmp_path, character)).library.ordered_keys())


def _seed_copy(tmp_path: Path, character: str, entries) -> Path:
    """直接往本场副本里放一座库（模拟"他之前在这个场景里待过"）。"""
    dst = _copy_dir(tmp_path, character)
    lib = store.Library(name=character, scope="character", owner=character)
    for entry in entries:
        lib.upsert(entry)
    store.save_library(lib, dst)
    return dst


class _RecordingThink(StubBackend):
    """记下每次 think 请求 messages 的 stub（引擎侧唯一能看到"真提示词"的地方）。"""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.seen: list[list[dict]] = []

    async def complete_json(self, messages: list[dict]) -> dict:
        self.seen.append([dict(m) for m in messages])
        return await super().complete_json(messages)


def _engine(tmp_path: Path, *, libraries_root: Path | None = None,
            cast=(A, B)) -> SceneEngine:
    scene_p, cards, models_p, bid_p = _write(tmp_path, cast=cast)
    eng = SceneEngine(scene_p, cards, models_p, run_root=tmp_path / "runs",
                      bid_path=bid_p, auto_narrate=False,
                      libraries_root=libraries_root)
    eng.think_backend = StubBackend(json_script=[_urge(2.0)] * 60)
    eng.speak_backend = StubBackend(line_script=[f"（第{i}句占位台词。）"
                                                 for i in range(60)])
    return eng


# ------------------------------------------------------- 缺省：没有信息库 --

def test_without_libraries_root_engine_has_no_knowledge(tmp_path):
    """`libraries_root` 缺省 None = 没有信息库：self._knowledge 与 ctx.knowledge 都是 None。

    这是"老调用方零语感差异"的落点——图看到 None 才走今天那条老路（逐字节、逐调用相同）。
    """
    eng = _engine(tmp_path)

    async def _drive():
        await eng._ensure_graph()
        return eng._ctx

    ctx = asyncio.run(_drive())
    assert eng._knowledge is None
    assert ctx.knowledge is None


def test_knowledge_disabled_switch_equals_no_library(tmp_path):
    """§10.2 的「信息库检索 开/关」：关 = 与没给库同义（不建副本、不注入索引）。

    开关必须**缺省开**，老调用方（不传任何新参数）零语感差异；关掉时连副本都不建——
    "关 = 退回今天"要落到磁盘上，而不只是不注入提示词。
    """
    libs = _libroot(tmp_path, A, entries=[_entry("药铺", body="柜台下有暗格。")])
    eng = _engine(tmp_path, libraries_root=libs)
    assert isinstance(eng._knowledge, KnowledgeAccess)      # 缺省开

    (tmp_path / "off").mkdir()
    scene_p, cards, models_p, bid_p = _write(tmp_path / "off", cast=(A,))
    off = SceneEngine(scene_p, cards, models_p, run_root=tmp_path / "off" / "runs",
                      bid_path=bid_p, auto_narrate=False, libraries_root=libs,
                      knowledge_enabled=False)
    assert off._knowledge is None
    assert not (tmp_path / "off" / "runs" / A / "library").exists(), "关掉时副本都不建"


def test_engine_wires_knowledge_into_graph_context(tmp_path):
    """有 libraries_root 时同一个实例同时进 self._knowledge 与 ctx（两处都要传）。"""
    libs = _libroot(tmp_path, A, entries=[_entry("药铺", body="柜台下有暗格。")])
    eng = _engine(tmp_path, libraries_root=libs)

    async def _drive():
        await eng._ensure_graph()
        await eng.aclose()          # run_root 已存在 → AsyncSqliteSaver：连着不关会拖住解释器退出
        return eng._ctx

    ctx = asyncio.run(_drive())
    assert isinstance(eng._knowledge, KnowledgeAccess)
    assert ctx.knowledge is eng._knowledge


# ------------------------------------------------------- 进场即建副本（§5.1）--

def test_open_creates_scene_copy_for_every_cast_member(tmp_path):
    """构造末期（演员表确定之后）为**本场在场角色**逐个建副本；无本体的角色建空副本。"""
    libs = _libroot(tmp_path, A, entries=[_entry("药铺", body="柜台下有暗格。")])
    _engine(tmp_path, libraries_root=libs)

    for name in (A, B):
        assert store.ensure_scene_copy(_copy_dir(tmp_path, name),
                                       _copy_dir(tmp_path, name)) is False
        assert _copy_dir(tmp_path, name).is_dir()
    # 本体里那条确实被拷进了副本（副本是快照，不是活引用）
    assert _copy_keys(tmp_path, A) == ["药铺"]


def test_existing_scene_copy_is_reused_not_reset(tmp_path):
    """副本已存在 → **原样复用**：他之前在这个场景里待过，这一场学到的东西必须还在。"""
    libs = _libroot(tmp_path, A)
    _seed_copy(tmp_path, A, [_scene_entry("本场所得", turn=3)])
    _engine(tmp_path, libraries_root=libs)

    assert _copy_keys(tmp_path, A) == ["本场所得"]
    entry = store.load_library(_copy_dir(tmp_path, A)).library.entries["本场所得"]
    assert entry.origin.turn == 3 and entry.origin.scene == SCENE


def test_add_character_creates_copy_and_reuses_existing(tmp_path):
    """运行中进场同样建副本（含延时/钩子路径，它们最终都走 add_character）；
    已有副本（他离场又回来）绝不重置。"""
    libs = _libroot(tmp_path, A)
    eng = _engine(tmp_path, libraries_root=libs, cast=(A,))
    # C 之前在这场待过（副本里留着本场所得），乙从没待过（没有副本）
    _seed_copy(tmp_path, C, [_scene_entry("丙的旧所得", turn=2)])

    async def _drive():
        await eng.add_character(B)
        await eng.add_character(C)
        await eng.add_character(C)          # 幂等：已在场不该再动副本

    asyncio.run(_drive())
    assert _copy_keys(tmp_path, B) == []                     # 新副本（本体也没有库）
    assert _copy_keys(tmp_path, C) == ["丙的旧所得"]           # 复用，绝不重置


def test_copy_failure_does_not_break_open_or_entry(tmp_path, monkeypatch):
    """建副本失败（IO 错）绝不能让开场/进场失败：记一笔即可（旁挂功能失败不打断主流程）。"""
    libs = _libroot(tmp_path, A)

    def _boom(*_a, **_kw):
        raise OSError("磁盘满了")

    monkeypatch.setattr(store, "ensure_scene_copy", _boom)
    eng = _engine(tmp_path, libraries_root=libs)             # 构造不得抛

    async def _drive():
        await eng.open_scene()                               # 开场照常
        line = await eng.add_character(C)                    # 进场照常
        return line

    line = asyncio.run(_drive())
    assert line is not None and line["speaker_type"] == "narrator"


# ------------------------------------------------- 撤回：副本与 recall 一起截断 --

def test_retract_truncates_copy_and_recall_via_tool_layer(tmp_path, monkeypatch):
    """撤回按轮次截断副本与 recall，**走工具层那两个方法**，cut 与私有记忆同口径。

    直接调存储层的 `truncate_copy_after_turn` 只删条目、不做 §5.3.1 的状态还原，会留下
    "被推翻的旧说法仍是现行"的坏形态——故这里同时钉住：工具层被调用、存储层没被直接调。
    """
    libs = _libroot(tmp_path, A)
    eng = _engine(tmp_path, libraries_root=libs)

    scene_calls: list[tuple[str, int]] = []
    recall_calls: list[tuple[str, int]] = []
    store_calls: list[tuple] = []
    mem_cuts: list[int] = []

    monkeypatch.setattr(KnowledgeAccess, "truncate_scene_after_turn",
                        lambda self, name, cut: (scene_calls.append((name, cut)),
                                                 TruncateReport())[1])
    monkeypatch.setattr(KnowledgeAccess, "truncate_recall_after_turn",
                        lambda self, name, cut: recall_calls.append((name, cut)))
    monkeypatch.setattr(store, "truncate_copy_after_turn",
                        lambda *a, **kw: (store_calls.append(a), 0)[1])
    monkeypatch.setattr(CharacterMemory, "truncate_after_turn",
                        lambda self, cut: mem_cuts.append(cut))

    async def _drive():
        await eng.open_scene()
        await eng.step(3)                       # 头几块才有 turn > 0 的行（开场是 turn 0）
        msgs = await eng.messages()
        target = msgs[-1]
        assert int(target["turn"]) > 0, "前置：已经产出 turn > 0 的行"
        await eng.retract(target["id"])
        await eng.aclose()
        return int(target["turn"])

    cut_turn = asyncio.run(_drive())
    assert mem_cuts and set(mem_cuts) == {cut_turn - 1}       # 既有口径：cut − 1
    names = [n for n, _ in scene_calls]
    assert sorted(names) == sorted([A, B])                    # 在册的全部人（在场 + 离场）
    assert {cut for _, cut in scene_calls} == {cut_turn - 1}
    assert recall_calls == scene_calls                        # 两处同口径、同名单
    assert store_calls == []                                  # 绝不直接调存储层那个


def test_retract_actually_drops_copy_entries_and_recall_rows(tmp_path):
    """真跑一遍（无打桩）：cut 之后的本场条目被删、recall 被裁；基线条目原样留着。"""
    libs = _libroot(tmp_path, A)
    eng = _engine(tmp_path, libraries_root=libs)
    _seed_copy(tmp_path, A, [
        Entry(key="基线", title="基线", body="开演前就知道的事。",
              origin=EntryOrigin(kind="manual", turn=store.BASELINE_TURN)),
        _scene_entry("后来才记的", turn=5),
    ])
    recall = _copy_dir(tmp_path, A).parent / RECALL_FILENAME
    recall.write_text(json.dumps({"turn": 5, "key": "后来才记的", "title": "后来才记的",
                                  "text": "正文"}, ensure_ascii=False) + "\n",
                      encoding="utf-8")

    async def _drive():
        await eng.open_scene()
        msgs = await eng.messages()
        await eng.retract(msgs[0]["id"])          # 开场那条（turn 0）→ cut = −1
        await eng.aclose()
        return msgs

    asyncio.run(_drive())
    assert _copy_keys(tmp_path, A) == ["基线"]     # turn 5 的本场条目被截掉
    assert recall.read_text(encoding="utf-8").strip() == ""


# ------------------------------------------------- 重置：信息库一并退回开演前 --

def test_reset_scene_runtime_truncates_copy_and_recall_via_tool_layer(tmp_path, monkeypatch):
    """重置 = **整场撤回**：副本与 recall 走工具层的两个方法，cut 取哨兵轮次（比本场任何写入早）。

    只清 state.jsonl / impressions 是不够的——信息库索引（§6.1）与取用记录（§6.4）是**新增
    的两条私人回喂源**：不跟着回退，重置后的角色照样把重置前记下的约定当【你的信息索引】、
    把重置前刚取到的正文当【你想起的事】带进下一块，pending.json 还宣称它们是"本场所得"，
    散场选「保留」会永久并进本体。撤回（retract）早就按同一口径接了这条通道（见
    `_truncate_memory`），重置漏接属于同一类 bug。

    cut 用**基线条目哨兵**（−1）：截断口径是"保留 turn ≤ cut"，取它恰好清掉所有本场产物
    （turn ≥ 0），而从本体拷进来的基线条目与订阅的活引用一条不动（§5.3 的硬要求——重置
    不是"忘掉身世"）。仍然**绝不**直接调存储层的 `truncate_copy_after_turn`（它不做 §5.3.1
    的状态还原）。
    """
    libs = _libroot(tmp_path, A)
    eng = _engine(tmp_path, libraries_root=libs)

    scene_calls: list[tuple[str, int]] = []
    recall_calls: list[tuple[str, int]] = []
    store_calls: list[tuple] = []
    monkeypatch.setattr(KnowledgeAccess, "truncate_scene_after_turn",
                        lambda self, name, cut: (scene_calls.append((name, cut)),
                                                 TruncateReport())[1])
    monkeypatch.setattr(KnowledgeAccess, "truncate_recall_after_turn",
                        lambda self, name, cut: recall_calls.append((name, cut)))
    monkeypatch.setattr(store, "truncate_copy_after_turn",
                        lambda *a, **kw: (store_calls.append(a), 0)[1])

    async def _drive():
        await eng.open_scene()
        await eng.reset_scene_runtime()
        await eng.aclose()

    asyncio.run(_drive())
    assert sorted(n for n, _ in scene_calls) == sorted([A, B])   # 在册的全部人
    assert {cut for _, cut in scene_calls} == {store.BASELINE_TURN}
    assert recall_calls == scene_calls                           # 两处同口径、同名单
    assert store_calls == []                                     # 绝不直接调存储层那个


def test_reset_scene_runtime_actually_rolls_the_copy_back_to_before_the_scene(tmp_path):
    """真跑一遍（无打桩）：重置后本场条目没了、基线条目还在、recall 空、pending 空。

    这正是用户按「重置」时以为会发生的事：这一场从头开始，角色不再记得这一场记下的东西，
    但开演前就知道的事一条不少（§5.3 的截断口径）。
    """
    libs = _libroot(tmp_path, A)
    eng = _engine(tmp_path, libraries_root=libs)
    _seed_copy(tmp_path, A, [
        Entry(key="基线", title="基线", body="开演前就知道的事。",
              origin=EntryOrigin(kind="manual", turn=store.BASELINE_TURN)),
        _scene_entry("本场所得", turn=3),
    ])
    copy_dir = _copy_dir(tmp_path, A)
    store.record_pending(copy_dir, "本场所得", turn=3)
    recall = copy_dir.parent / RECALL_FILENAME
    recall.write_text(json.dumps({"turn": 3, "key": "本场所得", "title": "本场所得",
                                  "text": "重置前取用到的正文"}, ensure_ascii=False) + "\n",
                      encoding="utf-8")

    async def _drive():
        await eng.open_scene()
        await eng.reset_scene_runtime()
        await eng.aclose()

    asyncio.run(_drive())
    assert _copy_keys(tmp_path, A) == ["基线"], "本场条目退回开演前，基线条目一条不动"
    assert store.load_pending(copy_dir).added == [], "本场所得清单也要清（否则散场会并进本体）"
    assert recall.read_text(encoding="utf-8").strip() == ""


def test_reset_scene_runtime_takes_the_index_out_of_the_next_think_prompt(tmp_path):
    """重置后的下一块，提示词里不得再出现重置前记下的条目名（§6.1 的回喂源真被清掉了）。

    磁盘断言只能证明文件变了；这条走真提示词——用户看得见的那一半（"他还记得吗"）。
    """
    libs = _libroot(tmp_path, A)
    eng = _engine(tmp_path, libraries_root=libs)
    _seed_copy(tmp_path, A, [_scene_entry("秘密约定", turn=1, body="重置前记下的约定")])

    rec = _RecordingThink(json_script=[_urge(2.0)] * 60)
    eng.think_backend = rec

    async def _drive():
        await eng.open_scene()
        await eng.step(2)
        before = rec.seen
        await eng.reset_scene_runtime()
        rec.seen = []
        await eng.step(1)
        await eng.aclose()
        return before

    before = asyncio.run(_drive())
    assert any("秘密约定" in "".join(m["content"] for m in one) for one in before), \
        "前置：重置前索引里确实列着本场记下的条目"
    assert rec.seen, "前置：重置后照常继续跑块"
    assert all("秘密约定" not in "".join(m["content"] for m in one)
               for one in rec.seen), "重置后索引里不该再有本场条目"


# ------------------------------------------------- 撤回：报告不许丢在地上 --

def test_retract_logs_the_truncate_report(tmp_path, monkeypatch, caplog):
    """撤回把 `TruncateReport` 记进日志：删了几条、还原了几条、**有什么没做成**。

    工具层的 docstring 把这条日志责任明确派给调用方（"报告就是给调用方记日志用的……静默
    才是真正不可接受的——用户既看不见也选不了"）。引擎把返回值整个丢掉，于是"本场改过的
    基线在本体里找不回原文、还原没做成"这类半截回退永远无人知道——副本里继续现行着一条
    依据已被撤回的改写（§5.3.1 要防的形态）。
    """
    libs = _libroot(tmp_path, A)
    eng = _engine(tmp_path, libraries_root=libs)
    monkeypatch.setattr(KnowledgeAccess, "truncate_scene_after_turn",
                        lambda self, name, cut: TruncateReport(
                            dropped=1, restored=2,
                            warnings=[f"「{name}」本场改过，但本体库里找不到这一条，没能还原。"]))
    monkeypatch.setattr(KnowledgeAccess, "truncate_recall_after_turn",
                        lambda self, name, cut: None)

    async def _drive():
        await eng.open_scene()
        await eng.step(3)
        msgs = await eng.messages()
        target = msgs[-1]
        assert int(target["turn"]) > 0, "前置：已经产出 turn > 0 的行"
        with caplog.at_level("WARNING", logger="harness.engine"):
            await eng.retract(target["id"])
        await eng.aclose()

    asyncio.run(_drive())
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "没能还原" in logged and A in logged, \
        f"撤回警告必须进日志（用户唯一的知情渠道），实际记录：{logged!r}"


# ---------------------------------------------------------------- 工具循环接通 --

def test_engine_tool_loop_records_recall_into_the_scene_copy(tmp_path):
    """引擎路径整条接通：think 走工具循环 → 副本里读到的正文记进 recall.jsonl。"""
    libs = _libroot(tmp_path, A, entries=[_entry("药铺", body="柜台下第三块砖是空的。")])
    eng = _engine(tmp_path, libraries_root=libs, cast=(A,))
    eng.think_backend = StubBackend(json_script=[_urge(2.0)], tool_script=[
        {"tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "read_entry",
                                      "arguments": '{"key": "药铺"}'}}]},
        {"content": json.dumps(_urge(2.0), ensure_ascii=False)},
    ])

    async def _drive():
        await eng.open_scene()
        await eng.step(2)
        await eng.aclose()

    asyncio.run(_drive())
    rows = [json.loads(line) for line in
            (tmp_path / "runs" / A / RECALL_FILENAME).read_text(
                encoding="utf-8").splitlines() if line.strip()]
    assert [r["key"] for r in rows] == ["药铺"]
    assert "第三块砖" in rows[0]["text"]


# --------------------------------------------------------------------- 素材 --

def _entry(key: str, **kw) -> Entry:
    base = dict(title=key, summary=f"{key}的一句话", body=f"{key}的正文")
    base.update(kw)
    return Entry(key=key, **base)


def _scene_entry(key: str, *, turn: int, **kw) -> Entry:
    kw.setdefault("origin", EntryOrigin(kind="scene", scene=SCENE, turn=turn))
    return _entry(key, **kw)


# ------------------------------------ 订阅进真提示词（§6.1 / §3.4 / §8.2） --

def _subscribe_wide(libs: Path, character: str, wide_dir: Path, name: str) -> None:
    """在角色**本体库目录**旁写一份订阅（界面上勾选订阅时的落盘形态，§8.2）。"""
    own = store.library_dir("character", character, root=libs)
    (own / store.SUBSCRIPTIONS_JSON).write_text(
        json.dumps({"schema": 1, "libraries": [{"name": name, "path": f"wide/{name}"}]},
                   ensure_ascii=False), encoding="utf-8")


def test_subscribed_wide_library_reaches_the_think_prompt(tmp_path):
    """订阅真的进提示词（§6.1/§3.4）：本体库旁一勾，他的索引里就有「借自《…》」那一组。

    上一阶段订阅只落在磁盘上、引擎侧没有人读它——这条走**真提示词**，是"缺口补上了"的
    证据：用户看得见的那一半（他知不知道世界观）只能在这里验。
    """
    libs = _libroot(tmp_path, A)
    wide = store.library_dir("wide", "庆国世界观", root=libs)
    lib = store.Library(name="庆国世界观", scope="wide")
    lib.upsert(Entry(key="地理·西线", title="地理·西线", summary="靠山，常年封冻"))
    store.save_library(lib, wide)
    _subscribe_wide(libs, A, wide, "庆国世界观")

    eng = _engine(tmp_path, libraries_root=libs)
    rec = _RecordingThink(json_script=[_urge(2.0)] * 60)
    eng.think_backend = rec

    asyncio.run(_drive_blocks(eng, 1))
    joined = "".join(m["content"] for one in rec.seen for m in one)
    assert "借自《庆国世界观》" in joined and "地理·西线" in joined


def test_subscription_stays_a_live_reference_in_the_prompt(tmp_path):
    """源库一改，订阅者**下一块看到的**就是新内容（§3.4 活引用，不是拷贝快照）。"""
    libs = _libroot(tmp_path, A)
    wide = store.library_dir("wide", "庆国世界观", root=libs)
    lib = store.Library(name="庆国世界观", scope="wide")
    lib.upsert(Entry(key="地理·西线", title="地理·西线", summary="靠山，常年封冻"))
    store.save_library(lib, wide)
    _subscribe_wide(libs, A, wide, "庆国世界观")

    eng = _engine(tmp_path, libraries_root=libs)
    rec = _RecordingThink(json_script=[_urge(2.0)] * 60)
    eng.think_backend = rec

    async def _drive():
        await eng.open_scene()
        await eng.step(1)
        lib = store.load_library(wide).library
        lib.upsert(Entry(key="地理·西线", title="地理·西线", summary="官方改了口径"))
        store.save_library(lib, wide)
        rec.seen = []
        await eng.step(1)
        await eng.aclose()

    asyncio.run(_drive())
    joined = "".join(m["content"] for one in rec.seen for m in one)
    assert "官方改了口径" in joined and "靠山，常年封冻" not in joined


async def _drive_blocks(eng: SceneEngine, blocks: int) -> None:
    """开场 → 跑 N 块 → 收尾（都在同一个 event loop 里，AsyncSqliteSaver 绑 loop）。"""
    await eng.open_scene()
    await eng.step(blocks)
    await eng.aclose()
