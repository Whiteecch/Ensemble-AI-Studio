"""CLI 的散场结算回路（设计文档 §7.2 纯总开关 / §7.4 触发点：引擎只准备、界面才决定）。

§7.4 把职责切得很清：**引擎绝不弹窗**（它跑在自己的线程/事件循环里），问用户是 CLI/GUI
的事。CLI 这条路的验收点是：

  · 一场戏跑完后把待决清单**打印出来**（谁、新增几条、修订几条、每条的一行标题）；
  · 有 stdin（交互）时逐个问「保留 / 丢弃」，回车 = 留着（不结算）；
  · **非交互**（管道 / --stub 自动跑 / 没有 stdin）时走一个**显式**的默认策略，并在输出里
    讲清"我用默认策略做了什么"——**不静默**（§7.4 明写）；
  · 用户中途放弃（EOF / Ctrl-C）不崩，按「不结算、留着」处理并说明；
  · 没有信息库（`--libraries-root none`/`off`/空串）时整段行为与今天逐字节相同：
    **一个字都不打印**。

全程 `tmp_path`：素材、运行根、信息库根都在临时目录里，不碰真实用户数据。
"""
from __future__ import annotations

import json
from pathlib import Path

from harness import knowledgestore as store
from harness import runner
from harness.knowledge import Entry, EntryOrigin

SCENE = "茶室"
A = "甲"
GAIN = "药铺"


# --------------------------------------------------------------------- 素材 --

def _write_fixture(tmp_path: Path) -> tuple[Path, list[Path], Path, Path, Path]:
    """场景 + 两张卡 + models.yaml + 信息库根；返回 (场景, 卡表, models, 信息库根, run_root)。

    run_root 里**预置**甲这一场的副本（模拟"他这一场已经记下了一条"）：CLI 侧的引擎
    进场即复用已有副本（§5.1 绝不重置），于是散场时有一笔账可结。
    """
    scene_p = tmp_path / "scenes" / f"{SCENE}.json"
    scene_p.parent.mkdir(parents=True, exist_ok=True)
    scene_p.write_text(json.dumps({"name": SCENE, "participants": [A]},
                                  ensure_ascii=False), encoding="utf-8")
    a_p = tmp_path / "characters" / f"{A}.json"
    a_p.parent.mkdir(parents=True, exist_ok=True)
    a_p.write_text(json.dumps({"name": A, "personality": {"描述": A}},
                              ensure_ascii=False), encoding="utf-8")
    models = tmp_path / "config" / "models.yaml"
    models.parent.mkdir(parents=True, exist_ok=True)
    models.write_text(
        "think:\n  backend: stub\n  model: stub\n  params: {}\n"
        "speak:\n  backend: stub\n  model: stub\n  params: {}\n", encoding="utf-8")
    libs = tmp_path / "libraries"
    libs.mkdir(parents=True, exist_ok=True)
    runs = tmp_path / "runs"

    lib = store.Library(name=A, scope="character", owner=A)
    lib.upsert(Entry(key=GAIN, title=GAIN, summary="夹层里有信", body="柜台下第三块砖是空的。",
                     origin=EntryOrigin(kind="scene", scene=SCENE, turn=1)))
    copy = store.scene_library_dir(runs, A)
    store.save_library(lib, copy)
    store.record_pending(copy, GAIN, turn=1)
    return scene_p, [a_p], models, libs, runs


def _argv(scene_p: Path, a_p: Path, models: Path, libs: Path | None, runs: Path,
          *extra: str) -> list[str]:
    argv = ["--scene", str(scene_p), "--characters", str(a_p), "--models", str(models),
            "--closing-at-block", "2", "--steps", "5", "--run-root", str(runs), *extra]
    if libs is not None:
        argv += ["--libraries-root", str(libs)]
    return argv


def _own_keys(libs: Path) -> list[str]:
    return list(store.load_library(store.library_dir("character", A, root=libs))
                .library.ordered_keys())


def _scripted_inputs(monkeypatch, lines: list[str]) -> None:
    """把 CLI 的每一次询问接到一串脚本化回答上（耗尽后抛 EOFError = 用户走了）。"""
    queue = list(lines)

    def _fake_input(prompt: str = "") -> str:
        if not queue:
            raise EOFError
        return queue.pop(0)

    monkeypatch.setattr("builtins.input", _fake_input)


# ------------------------------------------------------------ 交互式（有 stdin）--

def test_interactive_cli_asks_per_character_and_applies_the_answer(
        tmp_path: Path, monkeypatch, capsys):
    """交互式：逐个问「保留 / 丢弃」，回答「保留」→ 真并进本体库，并把做了什么打在屏幕上。"""
    scene_p, cards, models, libs, runs = _write_fixture(tmp_path)
    monkeypatch.setattr(runner, "_stdin_interactive", lambda: True)
    _scripted_inputs(monkeypatch, ["keep"])

    runner.main(_argv(scene_p, cards[0], models, libs, runs))

    out = capsys.readouterr().out
    assert GAIN in out, "清单里要有他这一场记下的东西（看得到才决定得了，§7.2）"
    assert "保留" in out
    assert GAIN in _own_keys(libs), "点了保留就得真并进本体库"


def test_interactive_cli_discards_when_asked(tmp_path: Path, monkeypatch, capsys):
    """回答「丢弃」→ 什么都不并（§7.2），副本与这一场的存档留着，且说清"什么都没并"。"""
    scene_p, cards, models, libs, runs = _write_fixture(tmp_path)
    monkeypatch.setattr(runner, "_stdin_interactive", lambda: True)
    _scripted_inputs(monkeypatch, ["discard"])

    runner.main(_argv(scene_p, cards[0], models, libs, runs))

    out = capsys.readouterr().out
    assert "丢弃" in out
    assert _own_keys(libs) == [], "丢弃 = 一个字都不并"


def test_interactive_cli_leaves_it_pending_on_a_bare_enter(tmp_path: Path, monkeypatch, capsys):
    """回车 = 留着（不结算，§7.3 允许稍后再结）：既不并进本体，也不标记丢弃——
    两个不可逆的选择都不该由一个空行替用户做。"""
    scene_p, cards, models, libs, runs = _write_fixture(tmp_path)
    monkeypatch.setattr(runner, "_stdin_interactive", lambda: True)
    _scripted_inputs(monkeypatch, [""])

    runner.main(_argv(scene_p, cards[0], models, libs, runs))

    out = capsys.readouterr().out
    assert "留着" in out and "没有替你结算" in out
    assert _own_keys(libs) == []
    assert store.load_pending(store.scene_library_dir(runs, A)).settled is False


# --------------------------------------------------------- 非交互（显式默认策略）--

def test_non_interactive_says_what_the_default_policy_did(tmp_path: Path, capsys):
    """非交互（管道/自动跑/没有 stdin）：走**显式**默认策略，并在输出里讲清做了什么
    （§7.4：不静默）。缺省策略 = 不结算、留着——两个不可逆的选择都不该自动替用户做。"""
    scene_p, cards, models, libs, runs = _write_fixture(tmp_path)

    runner.main(_argv(scene_p, cards[0], models, libs, runs))

    out = capsys.readouterr().out
    assert "默认策略" in out and "没有可交互的输入" in out
    assert GAIN in out, "清单照旧要给你看（看得到才决定得了）"
    assert _own_keys(libs) == []
    assert store.load_pending(store.scene_library_dir(runs, A)).settled is False


def test_non_interactive_keep_policy_merges_and_reports_it(tmp_path: Path, capsys):
    """`--settle-default keep`：自动并进本体库，并明说"按默认策略保留了"。"""
    scene_p, cards, models, libs, runs = _write_fixture(tmp_path)

    runner.main(_argv(scene_p, cards[0], models, libs, runs,
                      "--settle-default", "keep"))

    out = capsys.readouterr().out
    assert "默认策略" in out and "保留" in out
    assert GAIN in _own_keys(libs)


def test_cli_with_libraries_disabled_prints_nothing_about_settlement(tmp_path: Path,
                                                                     capsys):
    """`--libraries-root none` = 这一场没有信息库：整段结算行为与今天**逐字节相同**——输出
    里一个结算相关的字都不许有（§13.3 硬约束：老调用方零语感差异）。

    注意缺省已改：**不传**这个旗标现在等于启用 `app/libraries`（§13.3），"没有信息库"这条
    路要显式写 `none`/`off`/空串。这条用例钉的正是那条显式的路（缺省开着而一场空手的
    输出一致性由 `test_runner_libraries` 里那条逐字节对拍钉住）。
    """
    scene_p, cards, models, libs, runs = _write_fixture(tmp_path)

    runner.main(_argv(scene_p, cards[0], models, "none", runs,
                      "--settle-default", "keep"))

    out = capsys.readouterr().out
    assert "结算" not in out and "默认策略" not in out
    assert _own_keys(libs) == []


def test_cli_prints_why_a_pending_character_could_not_be_collected(tmp_path: Path, capsys):
    """待结算却**收不上来**的那条账，CLI 必须把他为什么不算数讲清楚（§7.4：不静默）。

    他的副本里有一条条目文件读不出来（手写格式有问题，`load_library` 跳过并警告）——那条
    警告是"他这一场少记了一条"的唯一提示。吞掉它，用户看到的就是一次"什么都正常、就是
    没人提那一条"的收尾：副本里的东西既并不进本体，也没人告诉他。
    """
    scene_p, cards, models, libs, runs = _write_fixture(tmp_path)
    broken = store.entries_dir(store.scene_library_dir(runs, A))
    broken.mkdir(parents=True, exist_ok=True)
    (broken / "密室.md").write_text("没有 front-matter 的正文。", encoding="utf-8")
    store.record_pending(store.scene_library_dir(runs, A), "密室", turn=1)

    runner.main(_argv(scene_p, cards[0], models, libs, runs))

    out = capsys.readouterr().out
    assert "密室" in out and A in out, f"读盘警告（他这一场少记了一条）没上屏：{out!r}"
    assert store.load_pending(store.scene_library_dir(runs, A)).settled is False, \
        "没答的人（默认策略=留着）绝不能被标成已结算"


# --------------------------------------------------------------- 中途放弃不崩 --

def test_cli_eof_midway_leaves_the_rest_pending_and_says_so(tmp_path: Path, monkeypatch, capsys):
    """用户中途走了（EOF / Ctrl-C）**不许崩**：没答的那几位按「不结算、留着」处理并说明
    ——崩在这里等于把用户刚看的一场戏的收尾整段丢掉。"""
    scene_p, cards, models, libs, runs = _write_fixture(tmp_path)
    monkeypatch.setattr(runner, "_stdin_interactive", lambda: True)
    _scripted_inputs(monkeypatch, [])                    # 一问就 EOF

    runner.main(_argv(scene_p, cards[0], models, libs, runs))

    out = capsys.readouterr().out
    assert "输入结束" in out
    assert _own_keys(libs) == []
    assert store.load_pending(store.scene_library_dir(runs, A)).settled is False
