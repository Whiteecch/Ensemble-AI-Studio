"""引擎侧的「关系进动力学」与「进离场通知」（《人际关系与场景推进》§6.4）——offline stub。

钉住四件事：

  · **进离场通知是一条确定性规则**：A 对 B 的亲密度高于正阈值 / 低于负阈值时，B 进场给
    A 落一条**只发给 A** 的通知行；中间地带、没有关系表时**一条都不发**；
  · **不新增模型调用**：通知是 harness 的规则，`add_character` 一个后端请求都不多发
    （用后端自己的 `calls` 计数钉住）；一整场跑下来的调用次数也与没有关系表时相同；
  · **亲密度进 bid**：关系快照按块刷新，接话/回应两项真的进了 `bid()`，且没有关系表的
    角色一分不动；
  · **阈值可配置**：`dynamics_params` 里的具名阈值能把它调严。

全程 `tmp_path`：素材、运行根、信息库根都在临时目录里，不碰真实用户数据。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from harness import knowledgestore as store
from harness.backends.stub import StubBackend
from harness.dynamics import RELATION_NOTICE_THRESHOLD
from harness.engine import SceneEngine
from harness.relations import Relation, RelationTable

SCENE = "茶室"
A, B = "甲", "乙"


# ------------------------------------------------------------------ 素材 --
def _think_rows() -> list[dict]:
    return [{"aroused": 0.0, "obligation_fulfilled": [], "goal_progress": 0.0,
             "addressed": None, "impression_of_speaker": None, "urge": 0.0}] * 3


def _scene_json() -> str:
    """场景只带 A —— B 是**运行中**进场的那个人（通知就发生在那一刻）。"""
    return json.dumps({"name": SCENE, "characters": [{"name": A}]},
                      ensure_ascii=False)


def _write(tmp_path: Path) -> tuple[Path, list[Path], Path, Path]:
    scene_p = tmp_path / f"{SCENE}.json"
    scene_p.write_text(_scene_json(), encoding="utf-8")
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
    bid.write_text("interruption_threshold: 0.4\nspeak_threshold: 0.1\n"
                   "silence_k: 100000\n", encoding="utf-8")
    return scene_p, cards, models, bid


def _seed_relations(tmp_path: Path, rows: list[Relation] | None) -> Path:
    """给 A 在**本体库**里放一张关系表（没有 rows = 连文件都不建），返回信息库根。

    §6.5 说关系表是信息库里的一个特殊分区，所以它必须落在**一座库**里（`library.json`
    或 `entries/` 至少有一个，否则 `_read_dir` 根本不认这座库）；引擎开场建副本时
    `ensure_scene_copy` 会把这份基线一起拷过去（`ensure_relations_copy`）。
    """
    libs = tmp_path / "libraries"
    lib_dir = store.library_dir("character", A, root=libs)
    store.save_library(store.Library(name=A, scope="character", owner=A), lib_dir)
    if rows is not None:
        store.save_relations(lib_dir, RelationTable(relations=rows))
    return libs


def _engine(tmp_path: Path, *, libraries_root: Path | None,
            dynamics_params: dict | None = None) -> SceneEngine:
    scene_p, cards, models, bid = _write(tmp_path)
    eng = SceneEngine(scene_p, cards, models, run_root=tmp_path / "runs",
                      bid_path=bid, auto_narrate=False, closing_at_block=99,
                      libraries_root=libraries_root,
                      dynamics_params=dynamics_params)
    eng.think_backend = StubBackend(json_script=_think_rows())
    eng.speak_backend = StubBackend(line_script=[f"（第{i}句台词。）" for i in range(40)])
    return eng


async def _enter_b(eng: SceneEngine) -> list[dict]:
    """开场 → 记下此刻的消息条数 → 让 B 进场 → 返回**新增的那些行**。"""
    await eng.open_scene()
    before = len(await eng.messages())
    await eng.add_character(B)
    return (await eng.messages())[before:]


def _notices(msgs: list[dict]) -> list[dict]:
    """这批新行里**只发给某个人**的那些（`knows` 非空 = 私密通知，§4.2）。"""
    return [m for m in msgs if m.get("knows")]


# --------------------------------------------------------- 进离场通知 --
def test_entry_notifies_the_one_who_cares(tmp_path):
    """A 对 B 的亲密度高于阈值 → B 进场给 A 落一条通知；这条**只有 A 看得见**。"""
    libs = _seed_relations(tmp_path, [Relation(name=B, closeness=80)])
    eng = _engine(tmp_path, libraries_root=libs)
    msgs = asyncio.run(_enter_b(eng))

    notices = _notices(msgs)
    assert len(notices) == 1, f"应当恰好一条通知，实际 {[m['content'] for m in msgs]}"
    assert notices[0]["knows"] == [A]
    assert B in notices[0]["content"], "通知得说清是谁来了"
    assert notices[0]["speaker_type"] == "narrator", "人事通知走场景叙述行（既有机制）"


def test_entry_notifies_with_a_different_line_when_closeness_is_negative(tmp_path):
    """低于负阈值同理（我心里一沉）：也发，但**措辞不同**——两种处境在角色心里是相反的。"""
    warm_libs = _seed_relations(tmp_path, [Relation(name=B, closeness=80)])
    cold_libs = _seed_relations(tmp_path / "cold", [Relation(name=B, closeness=-80)])

    warm = _notices(asyncio.run(_enter_b(_engine(tmp_path, libraries_root=warm_libs))))
    cold = _notices(asyncio.run(
        _enter_b(_engine(tmp_path / "cold", libraries_root=cold_libs))))

    assert len(warm) == 1 and len(cold) == 1
    assert cold[0]["content"] != warm[0]["content"]
    assert cold[0]["knows"] == [A]


def test_entry_notice_is_silent_in_the_middle_band(tmp_path):
    """中间地带（阈值内、0、负一点）一条都不发——"有分量"与"一沉"之间是绝大多数关系。"""
    for closeness in (0, RELATION_NOTICE_THRESHOLD, -RELATION_NOTICE_THRESHOLD,
                      10, -10):
        root = tmp_path / f"band{closeness}"
        libs = _seed_relations(root, [Relation(name=B, closeness=closeness)])
        eng = _engine(root, libraries_root=libs)
        msgs = asyncio.run(_enter_b(eng))
        assert _notices(msgs) == [], (closeness, [m["content"] for m in msgs])


def test_entry_notice_is_silent_without_a_relation_table(tmp_path):
    """没有关系表的角色**一条都不发**，进场行照旧只有「（B 走进了场景。）」那一行。"""
    libs = _seed_relations(tmp_path, None)          # 建了库、没有关系表
    eng = _engine(tmp_path, libraries_root=libs)
    msgs = asyncio.run(_enter_b(eng))

    assert _notices(msgs) == []
    assert [m["content"] for m in msgs] == [f"（{B}走进了场景。）"]


def test_no_libraries_root_means_no_notice_at_all(tmp_path):
    """连信息库都没有（`libraries_root` 缺省 None）→ 关系表无从谈起，一行都不多。"""
    eng = _engine(tmp_path, libraries_root=None)
    msgs = asyncio.run(_enter_b(eng))
    assert [m["content"] for m in msgs] == [f"（{B}走进了场景。）"]


def test_entry_notice_threshold_is_configurable_from_the_engine(tmp_path):
    """阈值可配置：调到 90 之后，80 的亲密度不再触发（具名参数经引擎传进动力学）。"""
    libs = _seed_relations(tmp_path, [Relation(name=B, closeness=80)])
    eng = _engine(tmp_path, libraries_root=libs,
                  dynamics_params={"relation_notice_threshold": 90,
                                   "relation_notice_low_threshold": -90})
    assert _notices(asyncio.run(_enter_b(eng))) == []


def test_entry_notice_carries_no_address(tmp_path):
    """通知行不带 `address`——它是「我心里动了一下」，不是「谁点名问我」（§6.4）。

    引擎把尾条消息的 `address` 当作「这一句点名了谁」喂给 `Dynamics.observe`
    （`engine._advance_dynamics` → `observe(addressed_to=...)`），而 `observe` 判
    `mentioned = ... or addressed_to == listener`：一旦通知行带上被通知者的名字，被通知者
    就被记下一笔 `pending_reply = pending_base`（1.2）的**硬性回应义务**——未答每块翻倍
    （封顶 64），进 `bid()` 时**不乘任何权重**，量级是本次亲密度两项（上界 0.3）的十几倍
    且随块增长。那正是铁律 4 与 §6.4 要排除的"决定结果"：亲密度会从"影响倾向"变成
    "一进场就必然抢话筒"。可见性本来就由 `knows` 保证，`address` 只贡献"被点名"语义。

    这里钉住机制本身：通知行的字段里**没有** `address`。
    """
    libs = _seed_relations(tmp_path, [Relation(name=B, closeness=80)])
    eng = _engine(tmp_path, libraries_root=libs)
    notices = _notices(asyncio.run(_enter_b(eng)))

    assert len(notices) == 1, "这一趟本来就该有一条通知（否则这条断言什么都没测到）"
    assert "address" not in notices[0], \
        "通知是一句私密的内心活动，不该被引擎读成「B 点名了 A」"
    assert notices[0]["knows"] == [A], "私密仍由 knows 保证（拿掉 address 不影响可见性）"


def test_entry_notice_does_not_owe_a_reply(tmp_path):
    """通知**不**让被通知者欠下回应义务：有通知与没有关系表时，他的动态状态逐项相同。

    这是铁律 4 在"通知"这条通道上的可证伪形式：通知只允许作为一行上下文进提示词，
    不允许改动 `pending_reply` / `adjacency` / `relevance`（那些是数值通道，而数值通道
    在本功能里只有 `bid()` 那两个加权项，上界 0.3 < 打断阈值）。曾经通知行带着
    `address=被通知者` 落盘，被通知者当场欠下 1.2、块末翻成 2.4，bid 从 0.18 跳到 3.09
    ——比开口阈值（0.1）高三十倍，方向还对称（−80 的仇人与 +80 的分量同效）。

    正负两个方向都测：§6.4 的通知本来就对称（"有分量"与"一沉"各发一条），若只有正方向
    不带 address，负方向仍会把"见到就烦的人"推去抢话筒——那正是本功能要排除的行为。
    """
    def _state(eng: SceneEngine) -> tuple:
        async def _run() -> tuple:
            await eng.open_scene()
            await eng.add_character(B)
            await eng.step(1)
            st = eng._dynamics.states[A]
            return (st.pending_reply, st.turns_pending, st.adjacency, st.relevance)
        return asyncio.run(_run())

    plain = _state(_engine(tmp_path / "plain",
                           libraries_root=_seed_relations(tmp_path / "plain", None)))
    for closeness in (80, -80):           # 有分量 / 心里一沉：两个方向都不该硬推开口
        root = tmp_path / f"n{closeness}"
        eng = _engine(root, libraries_root=_seed_relations(
            root, [Relation(name=B, closeness=closeness)]))
        wired = _state(eng)
        assert wired == plain, (
            f"亲密度 {closeness} 的通知行不该动被通知者的数值状态："
            f"有通知 {wired} vs 无关系表 {plain}")
        assert wired[0] == 0.0, "没有谁点过 A，他的欠答义务就该是 0"


def test_entry_notice_adds_no_model_call(tmp_path):
    """**不新增模型调用**：整个 `add_character` 期间两个后端各 0 次请求。

    通知是 harness 的确定性规则（§6.4 明写"不是让模型判断"）——多一次 think 就是
    整块的钱，而"他来了"这件事根本不需要问模型。
    """
    libs = _seed_relations(tmp_path, [Relation(name=B, closeness=80)])

    async def _run() -> tuple[int, int, int, list[dict]]:
        eng = _engine(tmp_path, libraries_root=libs)
        await eng.open_scene()
        think_before = eng.think_backend.calls
        await eng.add_character(B)
        return (think_before, eng.think_backend.calls, eng.speak_backend.calls,
                _notices(await eng.messages()))

    think_before, think_after, speak_after, notices = asyncio.run(_run())
    assert notices, "这一趟本来就该有一条通知（否则这条断言什么都没测到）"
    assert think_after == think_before, "进场不该多一次 think"
    assert speak_after == 0, "进场不该多一次 speak"


def test_entry_notice_does_not_change_the_model_call_count_of_a_whole_scene(tmp_path):
    """一整场（开场 → 若干块 → B 进场 → 再若干块）的调用次数与**没有关系表时逐次相同**。

    这是"通知只是多一行上下文，不是多一次往返"的端到端证据：关系在，脚本指针走的格数
    一模一样（stub 是确定性的，任何多出来的调用都会让计数漂开）。
    """
    async def _drive(root: Path) -> tuple[int, int]:
        libs = _seed_relations(root, [Relation(name=B, closeness=80)])
        eng = _engine(root, libraries_root=libs)
        await eng.open_scene()
        for _ in range(2):
            await eng.step(1)
        await eng.add_character(B)
        for _ in range(2):
            await eng.step(1)
        return eng.think_backend.calls, eng.speak_backend.calls

    async def _drive_plain(root: Path) -> tuple[int, int]:
        libs = _seed_relations(root, None)
        eng = _engine(root, libraries_root=libs)
        await eng.open_scene()
        for _ in range(2):
            await eng.step(1)
        await eng.add_character(B)
        for _ in range(2):
            await eng.step(1)
        return eng.think_backend.calls, eng.speak_backend.calls

    async def _both() -> tuple[tuple[int, int], tuple[int, int]]:
        return (await _drive(tmp_path), await _drive_plain(tmp_path / "plain"))

    with_rel, without_rel = asyncio.run(_both())
    assert with_rel == without_rel, f"{with_rel} != {without_rel}"


# ------------------------------------------------- 亲密度进 bid（引擎接线） --
def test_engine_refreshes_the_relation_snapshot_every_block(tmp_path):
    """引擎每块把关系快照喂给动力学：最后说话人是我关系近的人 → 我的 bid 更高。"""
    libs = _seed_relations(tmp_path, [Relation(name=B, closeness=100)])
    eng = _engine(tmp_path, libraries_root=libs)

    async def _run() -> None:
        await eng.open_scene()
        await eng.add_character(B)
        await eng.step(1)

    asyncio.run(_run())
    # 关系快照真的挂上了（没有它，下面那条 delta 恒为 0）。
    assert eng._dynamics.closeness_of(A, B) == 100
    # 端到端：同一个引擎、同一份状态，只是把快照清空 → bid 正好回落一项增益。
    eng._dynamics.last_speaker = B
    weights = {"w3_adjacency": 1.0}
    wired = eng._dynamics.bid(A, weights)
    eng._dynamics.set_relations({})
    plain = eng._dynamics.bid(A, weights)
    assert abs((wired - plain) - 0.15) < 1e-9, (wired, plain)


def test_engine_without_relations_leaves_bid_untouched(tmp_path):
    """没有关系表（连文件都没有）→ 快照是空的，bid 与今天逐位相同。"""
    libs = _seed_relations(tmp_path, None)
    eng = _engine(tmp_path, libraries_root=libs)

    async def _run() -> None:
        await eng.open_scene()
        await eng.step(1)

    asyncio.run(_run())
    assert eng._dynamics._relations == {}
