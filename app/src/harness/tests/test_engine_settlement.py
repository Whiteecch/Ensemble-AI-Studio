"""引擎侧散场结算（设计文档 §7.1 场景总结 / §7.3 离场挂起 / §7.4 触发点 / §7.5 幂等）。

钉六件事：

  · **没有信息库时逐字节不变**（§13.3 硬约束）：`libraries_root` 缺省 None → 收束不产生
    任何新事件、**不多一次模型调用**、连一次副本读都没有；
  · **收束跳变只触发一次 prepare**（§7.4）：检测点在 `step` 里那一块跑完之后 `closed`
    由假变真的跳变——那是唯一同时覆盖 GUI/CLI/块钟兜底三条路的位置；块钟兜底那条路
    （图自己写 closed，不经过 `close_scene`）照样触发；
  · **总结写进本场副本**（§7.1）：origin.kind=scene、带当前轮次、键与标题照命名口径
    （`场景-<场景名>-第N场` / `《<场景名>》那一夜`）、双链被兜底补齐（已有则不重复补），
    且兜底链接**活过写路径的正文截尾**（模型的总结一超上限，挂在末尾的链接会被整段截掉）；
  · **总结的模型调用失败绝不打断结算**（§7.4）：记一条 warning、跳过总结，所得照常待结算；
    但闸门**不因此关死**：下一次 prepare 只补没写成的那个人（重试正是 §7.5 幂等的用处），
    已经写成的人绝不重写（那会在副本里凭空多出 第2场、第3场…）；
  · **已经结过账的副本不再准备**（§7.3 手动结算 + §7.5 先到先得）：不烧模型调用、不写
    永不并入本体库的孤儿枢纽条目、不把已结清的人塞进待决事件；
  · **收不上来的所得不静默**：副本里那条条目文件读不出来时，`collect_gain` 的读盘警告
    带上角色名进 `settlement_warnings()`（用户唯一的知情渠道），他照旧留在待结算；
  · **apply 只结算 decisions 里的角色**、重复 apply 幂等、单个角色失败不牵连别人（§7.2/§7.5）；
  · **离场挂起**（§7.3）：`remove_character` 不弹窗、不做模型调用，只挂一条待结算提示；
  · **轮换归档**（§5.1「一条戏一份副本」）：角色**第一次进入这一场**时（开场阵容全体，
    以及运行中首次进场的——`add_character`/钩子进场）若发现本场副本**已经结算过**（只认
    `settled`，不认 `outcome`——丢弃过的账也结完了），把整个副本目录**重命名**成
    `library.settled-<N>` 归档（序号递增、绝不覆盖、条目与 pending.json 逐字留痕），并把
    这一场的取用记录 `recall.jsonl`（§6.4 的私人回喂源，按精确轮次回喂、新一场又从轮次 0
    起算）一并收进归档，再建一份干净的；**未**结算的副本照旧复用、一字不动，**这一场自己的**
    账本即使结算过也不轮换（他离场又回来，这一场的工作记忆必须还在）。归档名取自"副本目录名
    + 后缀"，故 `library_dir` / `scene_library_dir` 都换算不出它、也不住在库根那棵树里——
    绝不被读成一座库。轮换失败（OSError）绝不打断开场：记一条 warning（**不会被 `open_scene`
    清掉**）、照旧复用那份已结算的副本，并说清"这一场的所得并不进本体"（静默复用会让用户
    以为结算生效了）。`libraries_root` 缺省 None 时这条路一行都不执行。

全程 `tmp_path`：素材、运行根、信息库根都在临时目录里，不碰真实用户数据。
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from harness import events
from harness import graph as graph_mod
from harness import knowledgesettle as settle_mod
from harness import knowledgestore as store
from harness.backends.stub import StubBackend
from harness.engine import SceneEngine
from harness.knowledge import Entry, EntryOrigin
from harness.knowledgetools import MAX_BODY_CHARS, RECALL_FILENAME

SCENE = "茶室"
A, B = "甲", "乙"

SUMMARY_KEY = f"场景-{SCENE}-第1场"
SUMMARY_TITLE = f"《{SCENE}》那一夜"


# --------------------------------------------------------------------- 素材 --

def _urge(urge: float) -> dict:
    return {"aroused": 0.5, "obligation_fulfilled": [], "goal_progress": 0.0,
            "addressed": None, "impression_of_speaker": None, "urge": urge}


def _write(tmp_path: Path, *, cast=(A, B)) -> tuple[Path, list[Path], Path, Path]:
    """场景（含阵容）+ 两张卡 + models.yaml + 零阈值 bid（每块必有人开口）。"""
    scene_p = tmp_path / f"{SCENE}.json"
    scene_p.write_text(json.dumps({"name": SCENE, "characters": [{"name": n} for n in cast]},
                                  ensure_ascii=False), encoding="utf-8")
    cards: list[Path] = []
    for name in (A, B):
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


def _libroot(tmp_path: Path) -> Path:
    """一座（空的）信息库根；本体库按需由 `_save_own` 建。"""
    libs = tmp_path / "libraries"
    libs.mkdir(parents=True, exist_ok=True)
    return libs


def _save_own(libs: Path, character: str, entries=()) -> Path:
    lib = store.Library(name=character, scope="character", owner=character)
    for entry in entries:
        lib.upsert(entry)
    dst = store.library_dir("character", character, root=libs)
    store.save_library(lib, dst)
    return dst


def _copy_dir(tmp_path: Path, character: str) -> Path:
    return store.scene_library_dir(tmp_path / "runs", character)


def _copy_keys(tmp_path: Path, character: str) -> list[str]:
    return list(store.load_library(_copy_dir(tmp_path, character)).library.ordered_keys())


def _seed_copy(tmp_path: Path, character: str, entries, *, pending=()) -> Path:
    """直接往本场副本里放一座库（模拟"他这一场已经记下了东西"）。

    `pending` 里的键同时记进 `pending.json` 的 added——**账本才是"本场所得"的判据**
    （§5.3.1），只放条目文件不记账，`collect_gain` 会把它当成"开演前就在库里"的基线。
    """
    dst = _copy_dir(tmp_path, character)
    lib = store.Library(name=character, scope="character", owner=character)
    for entry in entries:
        lib.upsert(entry)
    store.save_library(lib, dst)
    for row in pending:
        store.record_pending(dst, row.key if isinstance(row, Entry) else str(row), turn=1)
    return dst


def _engine(tmp_path: Path, *, libraries_root: Path | None = None, cast=(A, B),
            closing_at_block: int = 2,
            narrate_lines: list[str] | None = None,
            run_root: Path | None = None) -> SceneEngine:
    scene_p, cards, models_p, bid_p = _write(tmp_path, cast=cast)
    eng = SceneEngine(scene_p, cards, models_p, run_root=run_root or tmp_path / "runs",
                      bid_path=bid_p, auto_narrate=False,
                      libraries_root=libraries_root, closing_at_block=closing_at_block)
    eng.think_backend = StubBackend(json_script=[_urge(2.0)] * 60)
    eng.speak_backend = StubBackend(line_script=[f"（第{i}句占位台词。）"
                                                 for i in range(60)])
    eng.narrate_backend = StubBackend(
        line_script=list(narrate_lines or ["这一夜就到这里了。"]))
    return eng


def _closed_scene(tmp_path: Path, *, cast=(A,), **kw) -> tuple[SceneEngine, list[dict]]:
    """跑一场到块钟兜底收束，返回（引擎，事件流）。用 `_run_to_close` 包一层。"""
    eng = _engine(tmp_path, cast=cast, **kw)
    return eng, _run_to_close(eng)


def _run_to_close(eng: SceneEngine, max_blocks: int = 5) -> list[dict]:
    """在**同一个** event loop 里开场→跑块→收尾（AsyncSqliteSaver 绑 loop，不能分开跑）。"""
    async def _drive():
        events = await eng.run_to_close(max_blocks=max_blocks)
        await eng.aclose()
        return events
    return asyncio.run(_drive())


def _copy_snapshot(path: Path) -> dict[str, bytes]:
    """库目录的逐文件快照（幂等断言用：文件内容一个字节都不许变）。"""
    return {str(p.relative_to(path)): p.read_bytes()
            for p in sorted(Path(path).rglob("*")) if p.is_file()}


def _play_one_scene(tmp_path: Path, libs: Path, *, gain_key: str, turn: int,
                    run_root: Path | None = None) -> dict[str, settle_mod.SettleReport]:
    """完整跑一场：开场 → 本场记下一条 `gain_key` → 收束 → 「保留」。

    记在副本上而不是靠模型吐（模型不写信息库）：工具层写路径落到磁盘上的就是
    「条目 + 账本」这两样，测试直接照它的样子写即可（同一个 idiom 见
    `test_a_gain_recorded_after_the_copy_was_settled_is_reported_not_silent`）。
    返回结算报告，供调用方断言"这一场的所得到底并进本体没有"。
    """
    eng = _engine(tmp_path, libraries_root=libs, cast=(A,), run_root=run_root)

    async def _drive():
        await eng.open_scene()
        copy = store.scene_library_dir(eng.run_root, A)
        lib = store.load_library(copy).library
        lib.upsert(_scene_entry(gain_key, turn=turn))
        store.save_library(lib, copy)
        store.record_pending(copy, gain_key, turn=turn)
        await eng.run_to_close(max_blocks=5)
        reports = eng.apply_settlement({A: "keep"})
        await eng.aclose()
        return reports

    return asyncio.run(_drive())


# --------------------------------------------------- 缺省：没有信息库（§13.3）--

def test_without_libraries_root_close_adds_no_event_and_no_model_call(tmp_path):
    """`libraries_root` 缺省 None → 收束照旧**一个字节、一次调用都不变**：不发待决事件、
    不调一次模型（没有总结要写）、连副本都不碰。既有 1311 条测试正是靠这条活着的。"""
    eng, events = _closed_scene(tmp_path, cast=(A, B))
    types = [e["type"] for e in events]

    assert any(e["type"] == "scene_state" and e["payload"]["closed"] for e in events), \
        "前置：这一场确实收束了"
    assert "settlement_pending" not in types
    assert "character_pending_settlement" not in types
    assert eng.narrate_backend.calls == 0, "没有信息库却多花了模型调用"
    assert eng.settlement_gains() == {}
    assert eng.pending_settlement() == {}
    assert eng.apply_settlement({A: "keep"}) == {}


def test_without_libraries_root_remove_character_stays_silent(tmp_path):
    """无信息库时离场也不挂任何东西（那条提示要读副本，无库时连读都不该发生）。"""
    eng = _engine(tmp_path, cast=(A, B))

    async def _drive():
        await eng.open_scene()
        await eng.step(1)
        await eng.remove_character(B)
        await eng.aclose()

    asyncio.run(_drive())
    assert eng.implicit_events() == []


# ----------------------------------------------------- 收束跳变触发 prepare --

def test_close_transition_prepares_settlement_once_and_is_idempotent(tmp_path):
    """收束跳变触发**一次** prepare（§7.4）；再调一次是空操作（§7.5）：总结条目只长一条。

    「连调两次不能生成两条总结条目」是幂等的硬要求——键里的 N 是按"已有几条枢纽条目"
    算的，第二次若照算就会写出 `第2场`、`第3场`…，用户的清单里凭空多出几条假总结。
    """
    libs = _libroot(tmp_path)
    _seed_copy(tmp_path, A, [_scene_entry("药铺", turn=1, body="柜台下第三块砖是空的。")],
               pending=["药铺"])
    eng = _engine(tmp_path, libraries_root=libs, cast=(A,))

    async def _drive():
        events = await eng.run_to_close(max_blocks=5)
        first = await eng.prepare_settlement()
        second = await eng.prepare_settlement()
        keys = _copy_keys(tmp_path, A)
        await eng.aclose()
        return events, first, second, keys

    events, first, second, keys = asyncio.run(_drive())
    rows = [e for e in events if e["type"] == "settlement_pending"]
    assert len(rows) == 1, "收束跳变只该发一次待决事件"
    assert keys.count(SUMMARY_KEY) == 1, f"重复 prepare 写出了重复的总结：{keys}"
    assert sorted(keys) == sorted(["药铺", SUMMARY_KEY])
    assert set(first) == {A} and set(second) == {A}
    assert set(first[A].added) == {"药铺", SUMMARY_KEY}
    assert first[A].is_pending is True


def test_an_already_settled_copy_is_not_prepared_again(tmp_path):
    """已经结过账的副本不再准备（§7.3 手动结算 / §7.5 先到先得）。

    用户从菜单先单独结了某个人（§7.3 明写的路），散场时引擎不该为同一个人再走一遍：
    那会白烧一次散场总结的模型调用、往他的副本里写一条**永远不会并进本体**的孤儿枢纽条目
    （settle 见 settled=True 直接 repeated），还在待决事件里把他再列一遍——而同一时刻
    `prepare_settlement()` 的返回值是空映射，两处真相打架（界面照事件问人，用户点「保留」
    只得一次静默空操作）。
    """
    libs = _libroot(tmp_path)
    _seed_copy(tmp_path, A, [_scene_entry("药铺", turn=1)], pending=["药铺"])
    eng = _engine(tmp_path, libraries_root=libs, cast=(A,), closing_at_block=2)

    async def _drive():
        await eng.open_scene()
        await eng.step(1)                       # 还没收束
        report = eng.apply_settlement({A: "keep"})       # 手动结算（§7.3 菜单那条路）
        before = eng.narrate_backend.calls
        events = await eng.step(1)              # 这一块里场景收束 → 收束跳变
        pending = await eng.prepare_settlement()
        own = store.load_library(store.library_dir("character", A, root=libs)).library
        await eng.aclose()
        return report, before, events, pending, own

    report, before, events, pending, own = asyncio.run(_drive())
    assert report[A].outcome == "keep" and report[A].repeated is False
    assert eng.closed is True, "前置：这一块确实收束了"
    assert eng.narrate_backend.calls == before, "已经结过账的人不该再烧一次散场总结"
    assert _copy_keys(tmp_path, A) == ["药铺"], "孤儿枢纽条目不许再写（它永远并不进本体）"
    assert [e for e in events if e["type"] == "settlement_pending"] == [], \
        "已经结清的人不该出现在待决事件里"
    assert pending == {} and eng.pending_settlement() == {}
    assert "药铺" in own.entries, "前置：手动结算那次确实并进本体了"


def test_a_gain_recorded_after_the_copy_was_settled_is_reported_not_silent(tmp_path):
    """结账之后又记下的东西**不许静默消失**（§7.5 先到先得 + §7.4 不静默）。

    副本结过账之后，`settle` 一律 `repeated`：他本场新记下的条目**永远并不进本体库**。
    先到先得是硬的（一次手滑的重复点击绝不能把"丢弃"翻成"保留"），可"账封了、东西卡在
    副本里"这件事必须说出来——不说，用户看到的是一次什么都没发生的收尾，而他这一场的
    记忆从此只活在副本里（CLI 用共享的 `runs/` 跑第二次就是这条路）。

    警告只列**结账之后**才记下的键：结账那一刻账本上就有、并且当场被并进本体的那些，
    再报一遍就是噪声（§7.3 那条正常路会被它天天吵）。
    """
    libs = _libroot(tmp_path)
    _seed_copy(tmp_path, A, [_scene_entry("药铺", turn=1)], pending=["药铺"])
    eng = _engine(tmp_path, libraries_root=libs, cast=(A,), closing_at_block=2)

    async def _drive():
        await eng.open_scene()
        await eng.step(1)                        # 还没收束
        eng.apply_settlement({A: "keep"})        # 先结一次（§7.3 菜单那条路）
        # 结完之后他又记下一条（工具层写路径落下的结果：条目 + 账本）
        copy = _copy_dir(tmp_path, A)
        lib = store.load_library(copy).library
        lib.upsert(_scene_entry("那封信", turn=2))
        store.save_library(lib, copy)
        store.record_pending(copy, "那封信", turn=2)
        await eng.step(1)                        # 这一块里收束 → 收束跳变
        await eng.aclose()
        return eng.settlement_warnings(), eng.settlement_gains()

    warnings, gains = asyncio.run(_drive())
    assert eng.closed is True, "前置：这一块确实收束了"
    assert any("那封信" in w for w in warnings), \
        f"结账之后记下的那条无声无息地没了：{warnings}"
    assert not any("药铺" in w for w in warnings), \
        f"结账时就在账上、当场并进本体的那条不该被报成卡住的：{warnings}"
    assert gains == {}, "结过账的副本不再准备：不写总结、不进清单"


def test_a_pending_character_whose_copy_is_unreadable_is_reported_not_swallowed(tmp_path):
    """待结算、却**收不上来**的人：读盘警告必须说出来，且不许把他塞进待决清单。

    副本里那条条目文件读不出来时（手写的字段名/格式有问题，`load_library` 跳过它并警告），
    `collect_gain` 给的是空所得——"他的库少了一条"的唯一提示就是那条 warning。它被引擎吞了
    用户看到的就是一次静默的收尾；而把他塞进决定清单更坏：他一条都收不上来，「保留」只会把
    他标成已结算（先到先得），那条修好文件之后本可并进去的条目就永远没机会了。
    """
    libs = _libroot(tmp_path)
    _seed_copy(tmp_path, A, [_scene_entry("药铺", turn=1)], pending=["药铺"])
    broken = _seed_copy(tmp_path, B, [], pending=["密室"])
    store.entries_dir(broken).mkdir(parents=True, exist_ok=True)
    (store.entries_dir(broken) / "密室.md").write_text(
        "没有 front-matter 的正文。", encoding="utf-8")

    eng = _engine(tmp_path, libraries_root=libs, cast=(A, B))

    async def _drive():
        events = await eng.run_to_close(max_blocks=5)
        pending = eng.pending_settlement()
        gains = eng.settlement_gains()
        warnings = eng.settlement_warnings()
        settled_b = store.load_pending(broken).settled
        await eng.aclose()
        return events, pending, gains, warnings, settled_b

    events, pending, gains, warnings, settled_b = asyncio.run(_drive())
    assert set(pending) == {A, B}, "前置：乙的账本上记着一笔待结算"
    assert set(gains) == {A}, "乙的条目收不上来，他没有可并进本体的东西"
    assert any("密室" in w and B in w for w in warnings), \
        f"读盘警告（他这一场少记了一条）被吞了：{warnings}"
    assert settled_b is False, "收不上来不等于结清了——留着他修好文件之后再来结"
    rows = [e for e in events if e["type"] == "settlement_pending"][0]["payload"]["characters"]
    assert [r["name"] for r in rows] == [A], "待决事件里只该有真收得上来的人"


def test_block_clock_close_also_triggers_settlement(tmp_path):
    """**块钟兜底那条路**（图自己在 world 里写 closed，根本不经过 `close_scene`）照样触发
    ——这正是把检测点放在 `step` 而不是 `close_scene` 里的理由（§7.4：挂哪一处都必然漏路）。"""
    libs = _libroot(tmp_path)
    _seed_copy(tmp_path, A, [_scene_entry("药铺", turn=1)], pending=["药铺"])
    eng = _engine(tmp_path, libraries_root=libs, cast=(A,), closing_at_block=1)

    async def _drive():
        await eng.open_scene()
        assert not eng.closed
        events = await eng.step(1)                  # 这一块里 world 就把场景收束了
        await eng.aclose()
        return events

    events = asyncio.run(_drive())
    assert eng.closed is True
    assert [e["type"] for e in events if e["type"] == "settlement_pending"] == \
        ["settlement_pending"]
    assert _copy_keys(tmp_path, A) == ["药铺", SUMMARY_KEY]


def test_close_scene_itself_never_prepares(tmp_path):
    """`close_scene()` **自己不做任何结算准备**（§7.4）：它是"停止"，不是"散场流程"——
    把准备挂在它里面就漏掉块钟那条路。停完之后由调用方（CLI/GUI）显式调 prepare 接手，
    而那一调同样是幂等的。"""
    libs = _libroot(tmp_path)
    _seed_copy(tmp_path, A, [_scene_entry("药铺", turn=1)], pending=["药铺"])
    eng = _engine(tmp_path, libraries_root=libs, cast=(A,))

    async def _drive():
        await eng.open_scene()
        await eng.step(1)
        before = eng.narrate_backend.calls
        await eng.close_scene()                      # 停止：不该写总结、不该调模型
        assert eng.narrate_backend.calls == before
        assert SUMMARY_KEY not in _copy_keys(tmp_path, A)
        pending = await eng.prepare_settlement()     # 停完之后显式接手（CLI/GUI 侧）
        keys = _copy_keys(tmp_path, A)
        await eng.aclose()
        return pending, keys

    pending, keys = asyncio.run(_drive())
    assert SUMMARY_KEY in keys and A in pending


def test_prepare_without_any_gain_writes_no_summary_and_calls_no_model(tmp_path):
    """本场什么都没得到 → 不写总结、不调模型、返回空映射（§7.1：总结是"枢纽"，没有可
    指的东西时写它只是白烧一次调用）。"""
    libs = _libroot(tmp_path)
    eng = _engine(tmp_path, libraries_root=libs, cast=(A,))

    async def _drive():
        events = await eng.run_to_close(max_blocks=5)
        pending = await eng.prepare_settlement()
        keys = _copy_keys(tmp_path, A)
        await eng.aclose()
        return events, pending, keys

    events, pending, keys = asyncio.run(_drive())
    assert pending == {}
    assert keys == []
    assert eng.narrate_backend.calls == 0
    assert all(e["type"] != "settlement_pending" for e in events)


def test_an_empty_prepare_does_not_lock_the_gate(tmp_path):
    """**没有所得的一次 prepare 不关上闸门**：调用方在戏还没演起来时先问了一声（空清单），
    收束时该写的总结照写——否则"先问一句"就等于把这一场的枢纽条目永久取消了。"""
    libs = _libroot(tmp_path)
    eng = _engine(tmp_path, libraries_root=libs, cast=(A,))

    async def _drive():
        await eng.open_scene()
        empty = await eng.prepare_settlement()      # 此刻本场还没所得（空清单）
        assert empty == {}
        # 戏演到一半他记下了一条（工具层的写路径落下的结果：条目 + 账本）
        _seed_copy(tmp_path, A, [_scene_entry("药铺", turn=1)], pending=["药铺"])
        await eng.step(1)
        await eng.close_scene()
        later = await eng.prepare_settlement()      # 收束后：该写总结了
        keys = _copy_keys(tmp_path, A)
        await eng.aclose()
        return later, keys

    later, keys = asyncio.run(_drive())
    assert SUMMARY_KEY in keys and A in later


def test_settlement_event_payload_carries_counts_titles_and_scene(tmp_path):
    """待决事件带上界面渲染清单所需的全部东西（§7.2："看得到才决定得了"）：
    谁、哪一场、新增几条、修订几条、每条的一行标题。"""
    libs = _libroot(tmp_path)
    _seed_copy(tmp_path, A, [_scene_entry("药铺", turn=1)], pending=["药铺"])
    eng = _engine(tmp_path, libraries_root=libs, cast=(A,))

    async def _drive():
        events = await eng.run_to_close(max_blocks=5)
        await eng.aclose()
        return events

    events = asyncio.run(_drive())
    rows = [e for e in events if e["type"] == "settlement_pending"][0]["payload"]["characters"]
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == A and row["scene"] == SCENE
    assert (row["added"], row["revised"]) == (2, 0), "枢纽条目也算本场所得（§7.1）"
    assert row["titles"] == ["药铺", SUMMARY_TITLE]


# --------------------------------------------------------- 散场总结（§7.1）--

def test_summary_entry_is_written_into_the_scene_copy_with_scene_origin(tmp_path):
    """散场总结写进**本场副本**、走工具层的写路径（§7.1）：origin.kind=scene、带本场轮次、
    键与标题照命名口径。写进副本它才会被算进"本场所得"（§7.2 的清单与合并都看这份账）。"""
    libs = _libroot(tmp_path)
    _seed_copy(tmp_path, A, [_scene_entry("药铺", turn=1, body="柜台下第三块砖是空的。")],
               pending=["药铺"])
    eng = _engine(tmp_path, libraries_root=libs, cast=(A,))

    async def _drive():
        await eng.run_to_close(max_blocks=5)
        turn = eng._turn
        entry = store.load_library(_copy_dir(tmp_path, A)).library.entries[SUMMARY_KEY]
        await eng.aclose()
        return turn, entry

    turn, entry = asyncio.run(_drive())
    assert entry.title == SUMMARY_TITLE
    assert entry.status == "active" and entry.indexed is True
    assert entry.origin.kind == "scene" and entry.origin.scene == SCENE
    assert entry.origin.turn == turn > 0, "总结带着本场轮次（撤回时按轮次一并截断，§5.3）"
    assert entry.body.startswith("这一夜就到这里了。")
    assert "[[药铺]]" in entry.body, "枢纽的价值全在链接上（§7.1）"
    assert entry.summary, "索引表里显示的就是这一行摘要（§7.1：在场者、我拿到了什么）"


def test_summary_backfills_missing_links_without_duplicating_the_written_one(tmp_path):
    """双链兜底（§7.1）：模型漏写的 `[[条目名]]` 由引擎补齐；已经写了的那条**不重复补**
    ——枢纽条目的价值全在链接上，断一处图就散一片；补重了则是误导（一条链写两遍）。"""
    libs = _libroot(tmp_path)
    _seed_copy(tmp_path, A, [_scene_entry("药铺", turn=1),
                             _scene_entry("那封信", turn=2)], pending=["药铺", "那封信"])
    eng = _engine(tmp_path, libraries_root=libs, cast=(A,),
                  narrate_lines=["这一夜我记下了 [[药铺]]。"])

    async def _drive():
        await eng.run_to_close(max_blocks=5)
        entry = store.load_library(_copy_dir(tmp_path, A)).library.entries[SUMMARY_KEY]
        await eng.aclose()
        return entry

    entry = asyncio.run(_drive())
    assert entry.body.count("[[药铺]]") == 1, "模型已经写了这一条，别再补一遍"
    assert "[[那封信]]" in entry.body, "漏掉的那条要补上"


def test_hub_links_survive_an_over_long_summary(tmp_path):
    """兜底链接必须**活过写路径的正文截尾**：枢纽条目的价值全在链接上（§7.1）。

    `remember` 对正文是截尾（`MAX_BODY_CHARS`）。链接挂在正文末尾，模型一啰嗦（提示词
    要求十行以内，但上限给了约五倍余量，偶发违规即中招）就会把引擎刚补上的那一段整段
    截掉：本体里那条枢纽条目一个链接都没有，角色日后从它进图却摸不到任何东西，而且
    全程无痕（键被收下了、条目也在，只有链接没了）。
    """
    libs = _libroot(tmp_path)
    _seed_copy(tmp_path, A, [_scene_entry("药铺", turn=1)], pending=["药铺"])
    long_body = "散场总结。" * 1100                  # 5500 字，远超写路径的正文上限
    assert len(long_body) > MAX_BODY_CHARS, "前置：这份总结确实超了正文上限"
    eng = _engine(tmp_path, libraries_root=libs, cast=(A,),
                  narrate_lines=[long_body])

    async def _drive():
        await eng.run_to_close(max_blocks=5)
        entry = store.load_library(_copy_dir(tmp_path, A)).library.entries[SUMMARY_KEY]
        await eng.aclose()
        return entry

    entry = asyncio.run(_drive())
    assert "[[药铺]]" in entry.body, "链接被正文截尾吃掉了：这一场在图上就没了门"
    assert len(entry.body) <= MAX_BODY_CHARS, "正文仍受写路径那条上限约束"


def test_summary_key_counts_the_hub_entries_already_in_the_library(tmp_path):
    """N = 该角色库里**已有的同场景枢纽条目数 + 1**（确定性，不引入新状态）：副本里已经
    躺着上一场留下的 `场景-茶室-第1场` → 这一场是 `第2场`（否则两条总结会撞成同名的
    一个文件，后写的把先写的顶掉）。"""
    libs = _libroot(tmp_path)
    _seed_copy(tmp_path, A, [
        _scene_entry("药铺", turn=1),
        Entry(key=SUMMARY_KEY, title="《茶室》那一夜", body="上一场留下的一条。",
              origin=EntryOrigin(kind="scene", scene=SCENE, turn=1)),
    ], pending=["药铺"])
    eng = _engine(tmp_path, libraries_root=libs, cast=(A,))

    async def _drive():
        await eng.run_to_close(max_blocks=5)
        keys = _copy_keys(tmp_path, A)
        await eng.aclose()
        return keys

    keys = asyncio.run(_drive())
    assert f"场景-{SCENE}-第2场" in keys
    assert keys.count(SUMMARY_KEY) == 1, "上一场那条必须原样留着"


def test_summary_model_failure_does_not_break_settlement(tmp_path, caplog):
    """总结的模型调用失败**绝不打断结算**（§7.4）：宁可没有枢纽条目，也不能让用户点不了
    「保留」。失败只记一条 warning（日志 + `settlement_warnings()`），所得照常待结算。"""
    class _Boom(StubBackend):
        async def complete_text(self, messages: list[dict]) -> str:
            raise RuntimeError("后端炸了")

    libs = _libroot(tmp_path)
    _seed_copy(tmp_path, A, [_scene_entry("药铺", turn=1)], pending=["药铺"])
    eng = _engine(tmp_path, libraries_root=libs, cast=(A,))
    eng.narrate_backend = _Boom()

    async def _drive():
        with caplog.at_level("WARNING", logger="harness.engine"):
            pending = await eng.prepare_settlement()
        await eng.aclose()
        return pending

    pending = asyncio.run(_drive())
    assert A in pending and pending[A].added == ["药铺"], "所得照常待结算"
    assert SUMMARY_KEY not in _copy_keys(tmp_path, A), "总结没写出来"
    assert any("散场总结" in w for w in eng.settlement_warnings())
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "散场总结" in logged, f"失败必须进日志（用户唯一的知情渠道）：{logged!r}"


# --------------------------------------------------- 失败可重试（§7.5 幂等）--

class _FlakyNarrate(StubBackend):
    """前 N 次散场总结调用抛（模拟后端超时/限流/网络抖动），之后照常出稿。

    自己数次数：抛在 `super().complete_text` 之前，基类的 `calls` 计不上这一枪。
    """

    def __init__(self, *, fail_times: int = 1, lines: list[str] | None = None):
        super().__init__(line_script=list(lines or ["这一夜就到这里了。"]))
        self.attempts = 0
        self._fail_left = fail_times

    async def complete_text(self, messages: list[dict]) -> str:
        self.attempts += 1
        if self._fail_left > 0:
            self._fail_left -= 1
            raise RuntimeError("后端抽风")
        return await super().complete_text(messages)


def test_a_failed_summary_is_retried_by_the_next_prepare(tmp_path, caplog):
    """闸门只在**真的写出总结**之后关上（§7.5 幂等的意义正是让重试安全）。

    一次后端抽风（超时/限流/网络抖动）绝不能让这一场的枢纽条目**永久**消失：那条条目是
    §7.1 整条设计的落点，也是角色日后"回忆那一夜"的唯一入口。生产的调用次序正好留了这次
    机会——`step` 收束跳变里先 prepare 一次（失败），CLI 随后在 `_settle_after_scene` 里
    再 prepare 一次（runner.py:208）。只断言"所得仍待结算"是不够的：那样闸门照旧关死，
    第二次连一枪模型都不发。
    """
    libs = _libroot(tmp_path)
    _seed_copy(tmp_path, A, [_scene_entry("药铺", turn=1)], pending=["药铺"])
    eng = _engine(tmp_path, libraries_root=libs, cast=(A,))
    flaky = _FlakyNarrate(fail_times=1)
    eng.narrate_backend = flaky

    async def _drive():
        with caplog.at_level("WARNING", logger="harness.engine"):
            first = await eng.prepare_settlement()          # 收束跳变里那一次：模型炸了
            keys_first = _copy_keys(tmp_path, A)
            second = await eng.prepare_settlement()         # CLI 收束后那一次：后端好了
            keys_second = _copy_keys(tmp_path, A)
        await eng.aclose()
        return first, keys_first, second, keys_second

    first, keys_first, second, keys_second = asyncio.run(_drive())
    assert A in first and SUMMARY_KEY not in keys_first, "前置：第一次确实没写成"
    assert flaky.attempts == 2, f"第二次 prepare 必须真的再打一枪模型：{flaky.attempts}"
    assert SUMMARY_KEY in keys_second, "后端恢复后那条枢纽条目必须补上"
    assert set(second[A].added) == {"药铺", SUMMARY_KEY}, "清单里要能看见补上的那条"


def test_a_retry_never_writes_a_second_hub_entry_for_the_character_that_succeeded(
        tmp_path):
    """重试只补没写成的那个人，已经写成的人**绝不重写**。

    键里的 N 是按"已有几条枢纽条目"算的（`_summary_key`）：把写成过的人重写一遍，副本里
    就凭空多出 `第2场`、`第3场`…——一条永远不会并进本体的假总结，用户的清单里也跟着脏。
    """
    libs = _libroot(tmp_path)
    for name in (A, B):
        _seed_copy(tmp_path, name, [_scene_entry("药铺", turn=1)], pending=["药铺"])
    eng = _engine(tmp_path, libraries_root=libs, cast=(A, B))
    flaky = _FlakyNarrate(fail_times=1)
    eng.narrate_backend = flaky

    async def _drive():
        await eng.prepare_settlement()          # 甲（先扇出）失败，乙写成
        after_first = flaky.attempts
        await eng.prepare_settlement()          # 重试：只该补甲
        await eng.aclose()
        return after_first

    after_first = asyncio.run(_drive())
    assert after_first == 2, f"前置：两个人各打了一枪：{after_first}"
    assert flaky.attempts == 3, f"重试该只补一个人（乙不许重写）：{flaky.attempts}"
    assert sorted(_copy_keys(tmp_path, A)) == sorted(["药铺", SUMMARY_KEY])
    assert sorted(_copy_keys(tmp_path, B)) == sorted(["药铺", SUMMARY_KEY]), \
        "乙的副本里不许出现 第2场（重写一次就是一条假总结）"


# ------------------------------------------------ apply：保留 / 丢弃（§7.2）--

def test_apply_settles_only_the_characters_in_decisions(tmp_path):
    """只结算 `decisions` 里出现的角色；没出现的**不结算**（§7.3：留在待结算，稍后再结）
    ——一次误点/一次中途退出都不该顺手替别人做决定。"""
    libs = _libroot(tmp_path)
    for name in (A, B):
        _seed_copy(tmp_path, name, [_scene_entry(f"{name}的所得", turn=1)],
                   pending=[f"{name}的所得"])
    eng = _engine(tmp_path, libraries_root=libs, cast=(A, B))

    async def _drive():
        await eng.run_to_close(max_blocks=5)
        reports = eng.apply_settlement({A: "keep"})
        left = eng.pending_settlement()
        own_a = store.load_library(store.library_dir("character", A, root=libs)).library
        own_b = store.load_library(store.library_dir("character", B, root=libs)).library
        settled_b = store.load_pending(_copy_dir(tmp_path, B)).settled
        await eng.aclose()
        return reports, left, own_a, own_b, settled_b

    reports, left, own_a, own_b, settled_b = asyncio.run(_drive())
    assert set(reports) == {A}
    assert reports[A].outcome == "keep" and reports[A].merged.persisted is True
    assert f"{A}的所得" in own_a.entries and SUMMARY_KEY in own_a.entries
    assert own_b.entries == {}, "没点过乙，他一个字都不该并进本体"
    assert settled_b is False and set(left) == {B}


def test_apply_is_idempotent_and_changes_no_byte_the_second_time(tmp_path):
    """重复 apply 幂等（§7.5）：第二次是空操作（`repeated=True`），磁盘上一个字节都不变——
    重试一次手滑的点击绝不能造出第二份归档条目。"""
    libs = _libroot(tmp_path)
    _seed_copy(tmp_path, A, [_scene_entry("药铺", turn=1)], pending=["药铺"])
    eng = _engine(tmp_path, libraries_root=libs, cast=(A,))

    async def _drive():
        await eng.run_to_close(max_blocks=5)
        own = store.library_dir("character", A, root=libs)
        first = eng.apply_settlement({A: "keep"})
        snapshot = _copy_snapshot(own)
        second = eng.apply_settlement({A: "keep"})
        await eng.aclose()
        return first, second, snapshot, own

    first, second, snapshot, own = asyncio.run(_drive())
    assert first[A].repeated is False and second[A].repeated is True
    assert _copy_snapshot(own) == snapshot


def test_apply_keeps_going_when_one_character_fails(tmp_path, monkeypatch):
    """单个角色失败**不牵连别人**（§7.4：引擎不许因为一次结算崩掉）：失败进那一份报告的
    warnings、结局留空（没结成就别说结了），另一个人照常结算。"""
    real = settle_mod.settle
    libs = _libroot(tmp_path)
    for name in (A, B):
        _seed_copy(tmp_path, name, [_scene_entry(f"{name}的所得", turn=1)],
                   pending=[f"{name}的所得"])
    eng = _engine(tmp_path, libraries_root=libs, cast=(A, B))

    def _boom(copy_dir, gain, own_dir, *, keep):
        if Path(copy_dir).parent.name == A:
            raise OSError("磁盘满了")
        return real(copy_dir, gain, own_dir, keep=keep)

    monkeypatch.setattr(settle_mod, "settle", _boom)

    async def _drive():
        await eng.run_to_close(max_blocks=5)
        reports = eng.apply_settlement({A: "keep", B: "keep"})
        own_b = store.load_library(store.library_dir("character", B, root=libs)).library
        pending_a = settle_mod.pending_gain(_copy_dir(tmp_path, A))
        await eng.aclose()
        return reports, own_b, pending_a

    reports, own_b, pending_a = asyncio.run(_drive())
    assert reports[A].outcome == "" and reports[A].warnings
    assert reports[B].outcome == "keep" and f"{B}的所得" in own_b.entries
    assert pending_a.is_pending is True, "甲没结成就仍是待结算，处理好还能重来"


def test_apply_refuses_an_unreadable_decision_and_leaves_the_scene_pending(tmp_path):
    """认不出来的决定（手滑/版本不匹配）**当作没决定**：不猜成"丢弃"（那也是不可逆的），
    报告里说清楚，这一场仍待结算。"""
    libs = _libroot(tmp_path)
    _seed_copy(tmp_path, A, [_scene_entry("药铺", turn=1)], pending=["药铺"])
    eng = _engine(tmp_path, libraries_root=libs, cast=(A,))

    async def _drive():
        await eng.run_to_close(max_blocks=5)
        reports = eng.apply_settlement({A: "随便"})
        own = store.load_library(store.library_dir("character", A, root=libs)).library
        pending = settle_mod.pending_gain(_copy_dir(tmp_path, A))
        await eng.aclose()
        return reports, own, pending

    reports, own, pending = asyncio.run(_drive())
    assert reports[A].outcome == "" and reports[A].warnings
    assert own.entries == {} and pending.is_pending is True


def test_pending_settlement_query_is_read_only(tmp_path):
    """只读查询（§7.3）：面板每刷新一次就调它一次，写盘等于把提示变成动作——连问三次，
    `pending.json` 一个字节都不许变，返回的也只该是"待结算"的那批。"""
    libs = _libroot(tmp_path)
    _seed_copy(tmp_path, A, [_scene_entry("药铺", turn=1)], pending=["药铺"])
    eng = _engine(tmp_path, libraries_root=libs, cast=(A, B))
    pending_file = store.pending_path(_copy_dir(tmp_path, A))

    async def _drive():
        await eng.open_scene()
        before = pending_file.read_bytes()
        out = [eng.pending_settlement(), eng.pending_settlement(),
               eng.pending_settlement()]
        after = pending_file.read_bytes()
        await eng.aclose()
        return out, before, after

    out, before, after = asyncio.run(_drive())
    assert before == after, "只读查询写了盘"
    assert set(out[0]) == {A} and out[0][A].added == ["药铺"]
    assert out[0] == out[1] == out[2]
    assert out[0][A].scene == SCENE and out[0][A].character == A, \
        "「哪一场」以引擎自己的场景名为准（路径推断只在运行根按场景名建目录时才对）"
    assert B not in out[0], "乙什么都没记下，不在待结算名单里"


# ------------------------------------- 开场轮换归档（§5.1 一条戏一份副本）--

def test_a_settled_copy_is_rotated_into_an_archive_and_recreated_clean(tmp_path):
    """开场时发现本场副本**已经结算过**（§5.1「一条戏一份副本」）：整个副本目录被**重命名**
    成 `library.settled-1` 归档，另建一份干净的。

    为什么必须轮换：副本按（场景，角色）存，而结算状态是一票制、先到先得（§7.5）——上一场
    遗留的 settled 会把新一场的所得**永久挡在门外**（并不进本体、也没有第二次机会）。

    为什么是重命名而不是删除：旧账要留痕、可查（§7.2 明写副本留着）——归档里的条目与
    `pending.json` 必须**逐字**保留，用户事后翻得到他那一场学了什么、结成了什么样。
    新副本则是干净的起点：不含上一场的条目，但基线照旧从本体拷来（§5.1 的原有语义）。
    """
    libs = _libroot(tmp_path)
    _save_own(libs, A, entries=[_entry("身世", body="他记得自己从哪来。")])
    copy = _seed_copy(tmp_path, A, [_scene_entry("药铺", turn=1, body="柜台下第三块砖是空的。")],
                      pending=["药铺"])
    store.mark_settled(copy, store.OUTCOME_KEEP)
    before = _copy_snapshot(copy)              # 上一场那份账本的逐文件快照

    _engine(tmp_path, libraries_root=libs, cast=(A,))

    archive = copy.parent / f"{store.SETTLED_ARCHIVE_PREFIX}1"
    assert archive.is_dir(), "已结算的副本没有被轮换归档"
    assert _copy_snapshot(archive) == before, "归档必须把上一场的条目与 pending.json 逐字留下"
    assert store.load_pending(archive).settled is True, "归档里那份账本仍是「已结算」的样子"
    # 新副本：干净（上一场的条目不在），基线仍在
    assert _copy_keys(tmp_path, A) == ["身世"]
    assert store.load_pending(copy).settled is False and store.load_pending(copy).added == []


def test_three_scenes_in_a_row_make_numbered_archives_that_never_overwrite(tmp_path):
    """连着三场（同一 run_root）→ 归档按序号递增取 `settled-1` / `settled-2`，**互不覆盖**：
    每一份里都各留着它那一场的东西。覆盖掉一份等于把用户查得到的旧账抹掉（§7.2），
    故序号是"已有同类归档数 + 1"顺延，绝不是固定名覆盖写。
    """
    libs = _libroot(tmp_path)
    runs = tmp_path / "runs"
    _play_one_scene(tmp_path, libs, gain_key="药铺", turn=1, run_root=runs)
    _play_one_scene(tmp_path, libs, gain_key="那封信", turn=2, run_root=runs)

    chardir = runs / A
    first = chardir / f"{store.SETTLED_ARCHIVE_PREFIX}1"
    second = chardir / f"{store.SETTLED_ARCHIVE_PREFIX}2"
    # 归档在**下一场开场**那一刻生成：两场之后有第一份（第一场的账本已归档）
    assert first.is_dir() and not second.exists()
    frozen = _copy_snapshot(first)             # 第一份归档此刻的样子

    _play_one_scene(tmp_path, libs, gain_key="钥匙", turn=3, run_root=runs)
    assert second.is_dir(), "第三场开场该把第二场的账本归档成 settled-2"

    archive_names = sorted(p.name for p in chardir.iterdir()
                           if p.name.startswith(store.SETTLED_ARCHIVE_PREFIX))
    assert archive_names == [first.name, second.name], \
        f"归档序号该顺延到 settled-1/2，没有被覆盖也没有多出来：{archive_names}"
    assert _copy_snapshot(first) == frozen, "第三场把第一份归档覆盖掉了"
    assert "药铺" in store.load_library(first).library.entries
    assert "那封信" not in store.load_library(first).library.entries
    assert "那封信" in store.load_library(second).library.entries


def test_an_unsettled_copy_is_reused_and_never_rotated(tmp_path):
    """副本**未**结算而复用 → 一律照旧：不轮换、内容一字不动。

    这正是 §5.1 原本要保的东西——他中途离场又回来，这一场学到的东西必须还在。轮换只认
    `settled`（结完了账），跟"这一场进行到哪儿"无关；把未结算的副本换个名字，就是把**正在
    进行的这一场**的账本轮换走了，等于丢账。
    """
    libs = _libroot(tmp_path)
    copy = _seed_copy(tmp_path, A, [_scene_entry("药铺", turn=1)], pending=["药铺"])
    before = _copy_snapshot(copy)

    _engine(tmp_path, libraries_root=libs, cast=(A,))

    assert _copy_snapshot(copy) == before, "未结算的副本被动了"
    assert not (copy.parent / f"{store.SETTLED_ARCHIVE_PREFIX}1").exists(), "不该有归档"
    assert _copy_keys(tmp_path, A) == ["药铺"]


def test_rotation_takes_the_previous_scene_recall_with_it(tmp_path):
    """轮换要把**上一场的取用记录**一并带走（§5.1「一条戏一份副本」× §6.4）。

    轮换搬的是 `runs/<角色>/library`，可这一场的**私人回喂源有两个**：另一个就住在它旁边
    ——`runs/<角色>/recall.jsonl`（§6.4 的 think→speak 通道，落在与副本同级的角色运行目录
    下，比副本**高一层**）。它按**精确轮次**回喂（`recall_text` 只返回 `turn == 当前轮` 的
    行，见 knowledgetools.py），而新一场的轮次又从 0 起算：不搬它，第二场第 1 块的 speak
    就会拿到第一场**同轮**取用的正文当【你想起的事】（注入点 `graph.py` 那处 speak 提示词），
    角色照着一整场"从没发生过的事"开口——这正是 §5.3/§6.4 反复在防的同一类 bug（撤回用例
    专门为同一形态写了 recall 截断）。

    旧账仍要留痕（§7.2）：取用记录随那一场的副本一起收进归档，事后翻得到他那一场查过什么。
    """
    libs = _libroot(tmp_path)
    _save_own(libs, A)
    copy = _seed_copy(tmp_path, A, [_scene_entry("暗格", turn=1, body="柜台下第三块砖是空的。")],
                      pending=["暗格"])
    store.mark_settled(copy, store.OUTCOME_DISCARD)
    recall = copy.parent / RECALL_FILENAME
    recall.write_text(json.dumps({"turn": 0, "key": "暗格", "title": "暗格",
                                  "text": "柜台下第三块砖是空的，里面藏着一把铜钥匙。"},
                                 ensure_ascii=False) + "\n", encoding="utf-8")

    eng = _engine(tmp_path, libraries_root=libs, cast=(A,))

    archive = copy.parent / f"{store.SETTLED_ARCHIVE_PREFIX}1"
    assert archive.is_dir(), "前置：上一场已结算的副本被轮换归档了"
    assert not recall.exists(), "上一场的取用记录还留在新一场的角色运行目录里"
    # 新一场第 1 块（turn 0）的 speak：拿不到上一场同轮那一句
    assert eng._knowledge.recall_text(A, 0) == ""
    # 但旧账留痕：归档里逐字留着那一场的取用记录
    kept = (archive / RECALL_FILENAME).read_text(encoding="utf-8")
    assert "铜钥匙" in kept, "取用记录该随那一场的副本一起收进归档（§7.2 留痕）"


def test_a_second_scene_never_speaks_with_the_previous_scenes_recall(tmp_path, monkeypatch):
    """**真跑一遍**（不手写 recall 文件）：第一场的工具循环把取用正文写进 recall.jsonl，
    第二场（同一 run_root、本场一次库都没查）的 speak 提示词里不得再出现它。

    第一场：think 走工具循环读到「暗格」（取用即记录，§6.4）→ 落一行 `turn=0`；散场选
    「丢弃」（副本轮换只认 settled，丢弃过的也算）。第二场：新引擎、同一 run_root、没有
    工具脚本（本场一次都没查库）——若不把 recall 随副本一起轮换，第二场第 1 块（`_turn`
    从 0 重新起算）的 speak 就会拿到第一场**同一轮**取用的正文当【你想起的事】。

    注入点用真身：`graph.py` 里 `build_speak_messages(..., recall_text=...)` 的实参——
    那一段就是 §6.4 的【你想起的事】，用户看到的行为全在这里兑现。
    """
    libs = _libroot(tmp_path)
    runs = tmp_path / "runs"
    _save_own(libs, A, entries=[_entry("暗格", body="柜台下第三块砖是空的，里面藏着一把铜钥匙。")])
    first = _engine(tmp_path, libraries_root=libs, cast=(A,), run_root=runs)
    first.think_backend = StubBackend(json_script=[_urge(2.0)], tool_script=[
        {"tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "read_entry",
                                      "arguments": '{"key": "暗格"}'}}]},
        {"content": json.dumps(_urge(2.0), ensure_ascii=False)},
    ])

    async def _drive_first():
        await first.run_to_close(max_blocks=5)
        first.apply_settlement({A: "discard"})          # 用户选「丢弃」
        await first.aclose()

    asyncio.run(_drive_first())
    recall = runs / A / RECALL_FILENAME
    rows = [json.loads(line) for line in recall.read_text(
        encoding="utf-8").splitlines() if line.strip()]
    assert [r["key"] for r in rows] == ["暗格"], "前置：第一场的取用记录真的落了盘"
    assert store.load_pending(runs / A / "library").settled is True, "前置：第一场已结账"

    seen: list[str] = []
    real = graph_mod.build_speak_messages

    def _spy(*args, **kwargs):
        seen.append(kwargs.get("recall_text") or "")
        return real(*args, **kwargs)

    monkeypatch.setattr(graph_mod, "build_speak_messages", _spy)
    second = _engine(tmp_path, libraries_root=libs, cast=(A,), run_root=runs)
    _run_to_close(second)

    assert seen, "前置：第二场有人开口（spy 没被调到）"
    assert all("铜钥匙" not in text for text in seen), \
        f"第二场的 speak 还在拿第一场取用的正文当【你想起的事】：{seen}"


def test_an_unsettled_copy_keeps_its_recall_rows(tmp_path):
    """未结算的副本连同它的取用记录**一个字都不动**（§5.1 的硬约束）。

    轮换只认 `settled`；把正在进行的这一场的 `recall.jsonl` 搬走，等于让角色这一场下一页
    就想不起刚查过的东西——`recall_text` 只服务**本块**的 think→speak（§6.4），它的每一行
    都还活在当下。
    """
    libs = _libroot(tmp_path)
    copy = _seed_copy(tmp_path, A, [_scene_entry("药铺", turn=1)], pending=["药铺"])
    recall = copy.parent / RECALL_FILENAME
    row = json.dumps({"turn": 1, "key": "药铺", "title": "药铺",
                      "text": "柜台下第三块砖是空的。"}, ensure_ascii=False) + "\n"
    recall.write_text(row, encoding="utf-8")

    eng = _engine(tmp_path, libraries_root=libs, cast=(A,))

    assert recall.read_text(encoding="utf-8") == row, "未结算的账本连同取用记录一起被动了"
    assert not (copy.parent / f"{store.SETTLED_ARCHIVE_PREFIX}1").exists(), "不该有归档"
    assert eng._knowledge.recall_text(A, 1) == "柜台下第三块砖是空的。"


def test_a_leftover_settled_copy_is_rotated_when_a_character_joins_late(tmp_path):
    """**第一次进入这一场**时就轮换——运行中进场（`add_character`/钩子进场）不例外（§5.1）。

    "副本已结算"是**上一场遗留物**这个物理状态，与"他是开场就在名单里、还是中途进来的"
    无关：`_rotate_settled_copy` 第一步就只认 `settled`，未结算的副本（= 这一场正在写的
    账本）本来就会原样跳过，所以把它接到进场这条路**不会丢任何账**（归档是重命名，旧账逐字
    留着）。不接的代价是实录里那种：同一运行根 + 上一场把人结掉 + 这一场他不在开场阵容、
    中途进来 → 他这一场新记下的东西永远进不了本体（`settle` 见 settled 直接 repeated），
    而散场清单是空的（`is_pending` 把他排除在外），用户点了一整场却什么都没发生。

    判据取"这一场里**从没出现过**"（不在场、也没离场过）：那才可能是上一场的遗留物。
    """
    libs = _libroot(tmp_path)
    eng = _engine(tmp_path, libraries_root=libs, cast=(A,))
    copy_b = _seed_copy(tmp_path, B, [_scene_entry("乙的旧所得", turn=1)], pending=["乙的旧所得"])
    store.mark_settled(copy_b, store.OUTCOME_KEEP)
    before = _copy_snapshot(copy_b)
    assert not (copy_b.parent / f"{store.SETTLED_ARCHIVE_PREFIX}1").exists(), \
        "前置：他不在开场阵容里，构造期不会碰他的副本"

    async def _drive():
        await eng.add_character(B)                       # 迟到进场
        copy = _copy_dir(tmp_path, B)
        lib = store.load_library(copy).library           # 这一场他又记下一条
        lib.upsert(_scene_entry("这一场新记的", turn=2))
        store.save_library(lib, copy)
        store.record_pending(copy, "这一场新记的", turn=2)
        await eng.run_to_close(max_blocks=5)
        reports = eng.apply_settlement({B: "keep"})
        await eng.aclose()
        return reports

    reports = asyncio.run(_drive())
    archive = copy_b.parent / f"{store.SETTLED_ARCHIVE_PREFIX}1"
    assert archive.is_dir(), "上一场遗留的已结算副本没有被轮换"
    assert _copy_snapshot(archive) == before, "归档必须把上一场的账本逐字留下"
    own_b = store.load_library(store.library_dir("character", B, root=libs)).library
    assert reports[B].repeated is False, "这一场被上一场遗留的 settled 当成重复结算了"
    assert "这一场新记的" in own_b.entries, \
        "迟进场者这一场的所得被上一场的 settled 永久挡在门外了（正是要修的缺口）"
    assert eng.settlement_warnings() == [], \
        "轮换做成了就不该再有「他这一场的东西并不进本体」那条警告"


def test_this_scenes_own_settled_ledger_is_never_rotated_on_re_entry(tmp_path):
    """**这一场自己的**账本即使结算过也不轮换（§5.1 原本要保的东西）。

    他中途离场、用户从菜单当场把他结掉（§7.3），之后他又回来——进场的这一刻正在上演的就是
    这一场，这份账本是**这一场的工作记忆**：把它换个名字，等于让角色这一场走到一半突然
    想不起前面记下的东西（尤其用户选的是「丢弃」：那些条目本来就只活在这一场里，并不并进
    本体不该连"这一场他还记得"一起判掉）。判据是"他这一场里出现过"（在场或离场过），
    与上一条的"上一场遗留物"互斥。

    代价照旧写明：他结账**之后**新记下的那几条仍卡在副本里（先到先得，§7.5），由
    `_note_stuck_gain` 说出来——那是这条路自己的、已经写明的代价（见那条用例）。
    """
    libs = _libroot(tmp_path)
    eng = _engine(tmp_path, libraries_root=libs, cast=(A, B))
    copy_b = _seed_copy(tmp_path, B, [_scene_entry("乙的所得", turn=1)], pending=["乙的所得"])

    async def _drive():
        await eng.open_scene()
        await eng.step(1)                                # 戏演起来
        await eng.remove_character(B)                    # 他离场（§7.3：不弹窗、只挂待结算）
        eng.apply_settlement({B: "discard"})             # 用户当场把他结掉
        assert store.load_pending(copy_b).settled is True
        before = _copy_snapshot(copy_b)
        await eng.add_character(B)                       # 他又回来了
        await eng.aclose()
        return before

    before = asyncio.run(_drive())
    assert _copy_snapshot(copy_b) == before, \
        "这一场自己的账本被轮换掉了（他这一场学到的东西没了）"
    assert not (copy_b.parent / f"{store.SETTLED_ARCHIVE_PREFIX}1").exists(), "不该有归档"


def test_a_discarded_copy_is_rotated_too(tmp_path):
    """轮换只认 `settled`、**不认 `outcome`**：用户选过「丢弃」的副本同样要轮换。

    它的账也结完了（先到先得），不轮换的话新一场的所得照样被那份 settled 挡在门外——
    "丢弃"只该影响"那一场的东西并不并进本体"，不该把**下一场**也一起判了刑。
    """
    libs = _libroot(tmp_path)
    _save_own(libs, A)
    copy = _seed_copy(tmp_path, A, [_scene_entry("药铺", turn=1)], pending=["药铺"])
    store.mark_settled(copy, store.OUTCOME_DISCARD)

    _engine(tmp_path, libraries_root=libs, cast=(A,))

    assert (copy.parent / f"{store.SETTLED_ARCHIVE_PREFIX}1").is_dir(), "丢弃过的副本也要轮换"
    assert _copy_keys(tmp_path, A) == []
    assert "药铺" in store.load_library(
        copy.parent / f"{store.SETTLED_ARCHIVE_PREFIX}1").library.entries


def test_the_archive_number_never_lands_on_an_occupied_name(tmp_path):
    """序号取"已有同类归档数 + 1"，算出来的位置**被占着就继续往后找**：绝不覆盖。

    归档是旧账留痕（§7.2），覆盖一份就等于把用户查得到的证据抹掉——命名这一步就得挡住，
    不能指望"正常情况下序号不会撞"（删过中间一份、或那个名字被别的东西占着，都会撞）。
    """
    copy = _copy_dir(tmp_path, A)
    copy.mkdir(parents=True)
    assert store.settled_archive_dir(copy).name == f"{store.SETTLED_ARCHIVE_PREFIX}1"

    (copy.parent / f"{store.SETTLED_ARCHIVE_PREFIX}1").mkdir()
    assert store.settled_archive_dir(copy).name == f"{store.SETTLED_ARCHIVE_PREFIX}2"

    (copy.parent / f"{store.SETTLED_ARCHIVE_PREFIX}2").write_text("占位的文件", encoding="utf-8")
    assert store.settled_archive_dir(copy).name == f"{store.SETTLED_ARCHIVE_PREFIX}3"


def test_the_rotated_archive_is_never_mistaken_for_a_library(tmp_path):
    """归档目录**绝不能被当成一座库读进来**——这是归档名（`library.settled-<N>`）必须满足的
    硬约束：读成库就等于凭空多出一座装着上一场旧条目的库，下一次索引注入会把它喂给角色。

    名字能满足这条的理由（也正是这么取名的原因）：

      · `scene_library_dir` 的结果**永远以副本目录名 `library` 结尾**——任何角色名都换算不出
        一个多带后缀的名字，故归档与"角色的正常副本目录"结构性撞不上；
      · `library_dir` 的结果**倒数第二段只能是 `characters` / `wide`**，且整条路径落在**本体
        库根**那棵树里；归档住在**运行根**的角色运行目录下（父目录名是角色名）。
    """
    libs = _libroot(tmp_path)
    _save_own(libs, A)
    copy = _seed_copy(tmp_path, A, [_scene_entry("药铺", turn=1)], pending=["药铺"])
    store.mark_settled(copy, store.OUTCOME_KEEP)

    _engine(tmp_path, libraries_root=libs, cast=(A,))

    runs = tmp_path / "runs"
    archive = runs / A / f"{store.SETTLED_ARCHIVE_PREFIX}1"
    assert archive.is_dir()
    # ① 两个换算助手都产不出它——把归档名当角色名/库名去换也换不出来
    for candidate in (A, store.SCENE_LIBRARY_DIRNAME, archive.name, "library.settled-2"):
        assert store.scene_library_dir(runs, candidate) != archive
        assert store.library_dir("character", candidate, root=libs) != archive
        assert store.library_dir("wide", candidate, root=libs) != archive
    # ② 归档不住在本体库根那棵树里（枚举本体库永远扫不到它）
    assert libs not in archive.parents
    # ③ "本场副本"的枚举口径是**精确名 `library`**（`scene_library_dir` 的末段就是它）：
    #    角色运行目录里挨着副本的那个归档不会被算成副本（前缀扫描才会扫到它，正因如此
    #    副本名必须是一个固定整名，而归档名是"整名 + 后缀"）
    names = sorted(p.name for p in (runs / A).iterdir())
    assert store.SCENE_LIBRARY_DIRNAME in names and archive.name in names
    assert [n for n in names if n == store.SCENE_LIBRARY_DIRNAME] == [store.SCENE_LIBRARY_DIRNAME]
    # ④ 拿副本路径读出来的也不是归档里的旧账
    assert _copy_keys(tmp_path, A) == [] and "药铺" in store.load_library(archive).library.entries


def test_a_failed_rotation_does_not_break_open_and_says_so(tmp_path, monkeypatch, caplog):
    """轮换失败（重命名抛 OSError / 磁盘满 / 目录被占用）**绝不打断开场**：记一条 warning，
    然后**照旧复用那份已结算的副本**。

    为什么选择"复用"而不是"删掉重来"：删掉是把用户没归档成功的旧账**直接销毁**（§7.2 要求
    副本留着），而复用至少把上一场的所得原样留在磁盘上、什么都没丢。代价是这一场新记下的
    东西并不进本体（先到先得，§7.5），所以 warning 必须把这件事**说清楚**——静默复用会让
    用户以为结算生效了。
    """
    libs = _libroot(tmp_path)
    own = store.library_dir("character", A, root=libs)
    _save_own(libs, A)
    copy = _seed_copy(tmp_path, A, [_scene_entry("药铺", turn=1)], pending=["药铺"])
    store.mark_settled(copy, store.OUTCOME_KEEP)
    before = _copy_snapshot(copy)

    def _boom(_copy_dir):
        raise OSError("磁盘满了")

    monkeypatch.setattr(store, "settled_archive_dir", _boom)

    eng = _engine(tmp_path, libraries_root=libs, cast=(A,))     # 构造不得抛
    assert _copy_snapshot(copy) == before, "轮换失败时那份副本必须原样留着（复用，不删不重置）"
    assert not (copy.parent / f"{store.SETTLED_ARCHIVE_PREFIX}1").exists()

    async def _drive():
        await eng.open_scene()                                  # 开场照常
        lib = store.load_library(copy).library                  # 这一场又记下一条
        lib.upsert(_scene_entry("那封信", turn=2))
        store.save_library(lib, copy)
        store.record_pending(copy, "那封信", turn=2)
        await eng.run_to_close(max_blocks=5)
        reports = eng.apply_settlement({A: "keep"})             # 用户选「保留」
        await eng.aclose()
        return reports, eng.settlement_warnings()

    with caplog.at_level("WARNING", logger="harness.engine"):
        reports, warnings = asyncio.run(_drive())

    assert store.load_pending(copy).settled is True, "账还是封着的（先到先得）"
    assert reports[A].repeated is True, "前置：这一场只能被当成重复结算（账早封了）"
    assert "那封信" not in store.load_library(own).library.entries, \
        "这正是那条 warning 说的事：这一场的所得并不进本体（复用的代价，必须说出来）"
    assert any(A in w and "无法并入本体" in w for w in warnings), \
        f"轮换失败必须明说「这一场的所得并不进本体」，不能静默复用：{warnings}"
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "磁盘满了" in logged and A in logged, \
        f"也要进日志（用户唯一的知情渠道），实际记录：{logged!r}"


def test_rotation_runs_zero_times_without_libraries_root(tmp_path, monkeypatch):
    """没传 `libraries_root`（`self._knowledge is None`）→ 这条路**一行都不执行**：与今天
    逐字节相同（§13.3 硬约束）。用一个一调就炸的陷阱函数钉住"根本没走到"这件事。
    """
    copy = _seed_copy(tmp_path, A, [_scene_entry("药铺", turn=1)], pending=["药铺"])
    store.mark_settled(copy, store.OUTCOME_KEEP)
    before = _copy_snapshot(copy)

    def _tripwire(*_a, **_kw):
        raise AssertionError("没有 libraries_root 却去算归档路径了")

    monkeypatch.setattr(store, "settled_archive_dir", _tripwire)

    eng = _engine(tmp_path, libraries_root=None, cast=(A,))

    assert _copy_snapshot(copy) == before and eng.settlement_warnings() == []


def test_a_second_scene_on_the_same_run_root_can_still_settle_into_the_body(tmp_path):
    """**同一 run_root 连开两场，第二场的所得照样并得进本体**（本次要修的缺口）。

    副本按（场景，角色）存、结算状态却是一票制先到先得（§7.5）：没有开场轮换时，第一场结完
    账留下的 settled 会把第二场**永久挡在门外**——第二场的条目并不进本体，也没有第二次机会
    （CLI 显式传 `--run-root`、以及续演，都会走到这条路）。开场轮换让第二场从一份干净的副本
    重新开始，于是"副本 = 这一场戏的临时账本"在每种运行方式下都成立。
    """
    libs = _libroot(tmp_path)
    runs = tmp_path / "runs"
    own = store.library_dir("character", A, root=libs)

    first = _play_one_scene(tmp_path, libs, gain_key="药铺", turn=1, run_root=runs)
    assert first[A].merged.persisted is True and first[A].repeated is False
    assert "药铺" in store.load_library(own).library.entries, "前置：第一场的所得并进了本体"

    second = _play_one_scene(tmp_path, libs, gain_key="那封信", turn=2, run_root=runs)

    keys = set(store.load_library(own).library.entries)
    assert second[A].repeated is False, "第二场被上一场遗留的 settled 当成重复结算了"
    assert second[A].merged.persisted is True
    assert "那封信" in keys, "第二场的所得没能并进本体（正是要修的缺口）"
    assert "药铺" in keys, "第一场的旧账必须还在——轮换归档的是副本，不是本体"
    # 第二场是从干净副本起的：归档里留着第一场的账，副本里不重复第一场的东西
    assert "药铺" in store.load_library(
        runs / A / f"{store.SETTLED_ARCHIVE_PREFIX}1").library.entries


# ------------------------------------------------------- 离场挂起（§7.3）--

def test_remove_character_hangs_a_pending_notice_and_calls_no_model(tmp_path):
    """离场挂起（§7.3）：**不弹窗、不做模型调用**（别打断正在进行的戏），只把「这位有一笔
    本场所得待结算」挂进只读事件流；他随时可被单独结算（副本与账本原样留着）。"""
    libs = _libroot(tmp_path)
    _seed_copy(tmp_path, B, [_scene_entry("药铺", turn=1)], pending=["药铺"])
    eng = _engine(tmp_path, libraries_root=libs, cast=(A, B))

    async def _drive():
        await eng.open_scene()
        await eng.step(1)
        before = (eng.think_backend.calls, eng.speak_backend.calls,
                  eng.narrate_backend.calls)
        await eng.remove_character(B)
        after = (eng.think_backend.calls, eng.speak_backend.calls,
                 eng.narrate_backend.calls)
        still = settle_mod.pending_gain(_copy_dir(tmp_path, B)).is_pending
        await eng.aclose()
        return before, after, still

    before, after, still = asyncio.run(_drive())
    assert after == before, "离场不许有任何模型调用"
    notices = [e for e in eng.implicit_events()
               if e.get("event_kind") == "pending_settlement"]
    assert len(notices) == 1
    assert notices[0]["name"] == B and notices[0]["scene"] == SCENE
    assert notices[0]["added"] == 1 and B in notices[0]["content"]
    assert notices[0]["visible"] is False, "它不该落成任何角色看得见的一行"
    assert still is True, "挂起 = 留着待结算，绝不顺手结掉"


def test_pending_notice_row_is_the_events_contract_row(tmp_path):
    """待结算提示那一行由 `events.character_pending_settlement` 产出，引擎不自己拼（§7.3）。

    信封（`hook_id`/`event_kind`/`content`/`visible`/`turn`/`blocks`）是隐式事件流的统一
    外形——离场发生在块中间，块的事件表早就返回了，提示只能走这条随读随取的通道。但**行
    的字段**不能在这里手搓：契约文件里那份 `character_pending_settlement` 与真实事件流
    一旦各长各的，将来按 `events.py` 写的消费方（CLI 面板、诊断工具）读到的字段名会和
    真实事件对不上，而且是静默对不上——它只会"收不到这条事件"，不会报错。
    """
    libs = _libroot(tmp_path)
    _seed_copy(tmp_path, B, [_scene_entry("药铺", turn=1, title="药铺的暗格")], pending=["药铺"])
    eng = _engine(tmp_path, libraries_root=libs, cast=(A, B))

    async def _drive():
        await eng.open_scene()
        await eng.remove_character(B)
        await eng.aclose()

    asyncio.run(_drive())
    notice = [e for e in eng.implicit_events()
              if e.get("event_kind") == "pending_settlement"][0]
    expected = events.character_pending_settlement({
        "name": B, "scene": SCENE, "added": 1, "revised": 0, "titles": ["药铺的暗格"]})
    assert {k: notice[k] for k in expected["payload"]} == expected["payload"], \
        "行必须与契约文件的产出逐字段一致（含标题口径与类型）"


def test_summary_of_a_departed_character_stops_at_his_departure(tmp_path, monkeypatch):
    """离场者的散场总结**只算到他离场那一刻**（§7.3：离场 = 副本冻结快照）。

    他走之后的事他没在场、没听见；把那些写进"他的一夜"就是把别人的戏塞进他的记忆
    ——与 §6.1/§6.4 那条信息边界是同一条线（不可变空间键/进场基线管的是同一件事）。
    """
    import harness.engine as engine_mod

    libs = _libroot(tmp_path)
    for name in (A, B):
        _seed_copy(tmp_path, name, [_scene_entry(f"{name}的所得", turn=1)],
                   pending=[f"{name}的所得"])
    eng = _engine(tmp_path, libraries_root=libs, cast=(A, B), closing_at_block=8)
    seen: dict[str, str] = {}
    real = engine_mod.build_summary_messages

    def _spy(card, view_text, scene_text, *args, **kwargs):
        seen[card.name] = view_text
        return real(card, view_text, scene_text, *args, **kwargs)

    monkeypatch.setattr(engine_mod, "build_summary_messages", _spy)

    async def _drive():
        await eng.open_scene()
        await eng.step(1)
        cutoff = max(m["id"] for m in await eng.messages())   # 乙走之前**已经存在**的最后一条
        await eng.remove_character(B)
        await eng.step(2)                                     # 他走之后还有人说、有事发生
        await eng.close_scene()
        await eng.prepare_settlement()
        await eng.aclose()
        return cutoff

    cutoff = asyncio.run(_drive())

    def _ids(text: str) -> list[int]:
        return [int(n) for n in re.findall(r"\[(\d+)\]", text or "")]

    assert set(seen) == {A, B}, "两个人都各有总结（各自的口吻与视角，§7.1）"
    assert _ids(seen[B]) and max(_ids(seen[B])) <= cutoff, \
        f"乙的总结不该含他离场之后的行：{seen[B]!r}"
    assert max(_ids(seen[A])) > cutoff, "前置：他走之后确实有新内容（对甲可见）"


def test_remove_character_without_gain_hangs_nothing(tmp_path):
    """本场没所得的人离场**不挂提示**：`pending.json` 空着的人不欠账，提示只是噪声
    （面板上全是"待结算"就没人看了）。"""
    libs = _libroot(tmp_path)
    eng = _engine(tmp_path, libraries_root=libs, cast=(A, B))

    async def _drive():
        await eng.open_scene()
        await eng.step(1)
        await eng.remove_character(B)
        await eng.aclose()

    asyncio.run(_drive())
    assert [e for e in eng.implicit_events()
            if e.get("event_kind") == "pending_settlement"] == []


# --------------------------------------------------------------------- 素材 --

def _entry(key: str, **kw) -> Entry:
    base = dict(title=key, summary=f"{key}的一句话", body=f"{key}的正文")
    base.update(kw)
    return Entry(key=key, **base)


def _scene_entry(key: str, *, turn: int, **kw) -> Entry:
    kw.setdefault("origin", EntryOrigin(kind="scene", scene=SCENE, turn=turn))
    return _entry(key, **kw)


def test_pending_notice_lists_readable_titles_not_keys(tmp_path):
    """离场待决清单里的 `titles` 是**给人看**的标题，不是文件用的键（§7.2/§8.3）。

    手动结算那张窗直接照抄这一行；键（`场景-茶室-第1场`）摊给用户看就是一行看不懂的
    内部名——清单是"看得到才决定得了"的落点，看不看得懂是它成立的前提。
    """
    libs = _libroot(tmp_path)
    _seed_copy(tmp_path, B, [_scene_entry("药铺", turn=1, title="药铺的暗格")],
               pending=["药铺"])
    eng = _engine(tmp_path, libraries_root=libs, cast=(A, B))

    async def _drive():
        await eng.open_scene()
        await eng.remove_character(B)
        await eng.aclose()

    asyncio.run(_drive())
    notice = [e for e in eng.implicit_events()
              if e.get("event_kind") == "pending_settlement"][0]
    assert notice["titles"] == ["药铺的暗格"], "给人看的是标题，不是键"
