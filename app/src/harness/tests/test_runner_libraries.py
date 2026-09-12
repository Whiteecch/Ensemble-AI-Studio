"""CLI 的信息库接线（设计文档 §9.1 第二步 / §10.2 / §13.3）。

三件事在这里钉死：

  · **缺省开**：`--libraries-root` 不传不再是"没有信息库"，而是仓库/安装目录下的
    `app/libraries`（§13.3）。显式传 `none`/`off`/`-`/空串才是关——关掉的那条路必须
    与今天逐字节一致（一个字都不多打印）；
  · **入场播种**：这一场装载的卡上的 `knowledge_seed`（老卡的 `knowledge_boundary` 读时
    迁移来的）在 main() 里真的变成库里的条目；库已存在则整段跳过（幂等）；
  · **零语感差异的硬约束**：缺省开启之后，"什么都没记下的一场戏"输出仍与关掉时逐字节
    相同（§13.3）——这条最容易在"顺手多打印一句状态"时破掉。

全程 tmp_path；缺省信息库根由 conftest 的隔离夹具指到临时目录（绝不在仓库里播种）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness import knowledgestore as store
from harness import runner

SCENE = "茶室"
A = "甲"


def _write_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    """场景 + 一张**带老键**的卡 + models.yaml；返回 (场景, 卡, models)。

    卡上写着 `knowledge_boundary`（老形状的 JSON），其中一句正是那句兜底文案——它迁移时
    留在卡上，**播种时**被丢掉。
    """
    scene_p = tmp_path / "scenes" / f"{SCENE}.json"
    scene_p.parent.mkdir(parents=True, exist_ok=True)
    scene_p.write_text(json.dumps({"name": SCENE, "participants": [A]},
                                  ensure_ascii=False), encoding="utf-8")
    card_p = tmp_path / "characters" / f"{A}.json"
    card_p.parent.mkdir(parents=True, exist_ok=True)
    card_p.write_text(json.dumps({
        "name": A, "personality": {"描述": "话少"},
        "knowledge_boundary": ["知道：药铺的暗格", "陈掌柜是个跛子",
                               "只知道自己经历和被告知的事"],
    }, ensure_ascii=False), encoding="utf-8")
    models = tmp_path / "config" / "models.yaml"
    models.parent.mkdir(parents=True, exist_ok=True)
    models.write_text(
        "think:\n  backend: stub\n  model: stub\n  params: {}\n"
        "speak:\n  backend: stub\n  model: stub\n  params: {}\n", encoding="utf-8")
    return scene_p, card_p, models


def _argv(scene_p: Path, card_p: Path, models: Path, runs: Path,
          *extra: str) -> list[str]:
    return ["--scene", str(scene_p), "--characters", str(card_p),
            "--models", str(models), "--closing-at-block", "2", "--steps", "5",
            "--run-root", str(runs), *extra]


# ------------------------------------------------------------ 缺省落点与「关」--

def test_default_libraries_root_is_the_repo_app_libraries():
    """缺省信息库根 = 仓库内的 `app/libraries`（与 `app/characters`、`app/scenes` 同级，§13.3）。

    隔离夹具把缺省常量指到了临时目录（不许用例往仓库里写），故这里钉的是**推导**：
    `_default_libraries_root()` 按模块位置算出的 app 根，必须真的是那两个素材目录所在处，
    结果必须叫 `libraries`。若哪天有人把 `parents[2]` 写错一层，这条先红。
    """
    app_dir = Path(runner.__file__).resolve().parents[2]
    assert (app_dir / "scenes").is_dir() and (app_dir / "characters").is_dir(), \
        "app 根推断错了（素材目录不在这里）"
    assert runner._default_libraries_root() == app_dir / "libraries"
    # 不传 = 用缺省（开），不是"没有信息库"
    assert runner._resolve_libraries_root(None) == runner._DEFAULT_LIBRARIES_ROOT


@pytest.mark.parametrize("off", ["", "  ", "none", "None", "OFF", "off", "no",
                                 "-", "关", "无"])
def test_off_sentinels_resolve_to_no_library(off):
    """「关」的几种写法都认（大小写随意、留白随意），都解析成 None = 这一场没有信息库。

    这些是写进 `--help` 的公开写法：用户要一条"退回今天"的路，且它必须**显式**。
    """
    assert runner._resolve_libraries_root(off) is None


def test_explicit_path_overrides_the_default(tmp_path):
    """显式传路径仍覆盖缺省（缺省只是缺省）。"""
    libs = tmp_path / "my-libs"
    assert runner._resolve_libraries_root(str(libs)) == libs
    assert runner._resolve_libraries_root(libs) == libs


# ------------------------------------------------------------------ 入场播种 --

def test_cli_seeds_old_cards_into_the_default_libraries_root(
        tmp_path, _isolate_libraries_root):
    """不传 `--libraries-root` = 缺省开：这一场装载的老卡在这一刻真的建了库（§9.1 第二步）。

    `_isolate_libraries_root` 就是被夹具替换掉的那个缺省根（临时目录）。卡上那三条边界里
    前两条进库、兜底句**丢弃**——"逐条搬过去"这条最高约束的 CLI 侧证明。
    """
    scene_p, card_p, models = _write_fixture(tmp_path)

    runner.main(_argv(scene_p, card_p, models, tmp_path / "runs"))

    lib = store.library_dir("character", A, root=_isolate_libraries_root)
    assert store.load_library(lib).library.ordered_keys() == ["药铺的暗格", "陈掌柜是个跛子"]


def test_cli_seeding_is_idempotent_and_skipped_when_the_library_exists(
        tmp_path, _isolate_libraries_root):
    """库已存在 → **整段跳过**：第二次开同一场不得把角色这一路长出来的见识抹回出厂状态。

    播过一次之后，卡上的种子仍留着（迁移只搬数据、绝不改卡）。库里后来长出来的东西绝不能
    被第二次播种顶掉——那正是"库已存在则跳过"（幂等）要保的东西。
    """
    scene_p, card_p, models = _write_fixture(tmp_path)
    lib = store.library_dir("character", A, root=_isolate_libraries_root)
    store.save_library(store.Library(name=A, scope="character", owner=A), lib)
    grown = store.Entry(key="本场刚学的", title="本场刚学的")
    loaded = store.load_library(lib).library
    loaded.upsert(grown)
    store.save_library(loaded, lib)

    runner.main(_argv(scene_p, card_p, models, tmp_path / "runs"))

    keys = store.load_library(lib).library.ordered_keys()
    assert keys == ["本场刚学的"], f"已存在的库被重播了：{keys}"


def test_cli_off_sentinel_disables_the_library_and_seeds_nothing(
        tmp_path, _isolate_libraries_root):
    """显式 `none` = 关：一个字都不建（连播种都不做）——"关"是与没有信息库**同义**。"""
    scene_p, card_p, models = _write_fixture(tmp_path)

    runner.main(_argv(scene_p, card_p, models, tmp_path / "runs",
                      "--libraries-root", "none"))

    assert not (_isolate_libraries_root / "characters" / A).exists()


def test_runner_hands_the_resolved_root_to_the_engine(tmp_path, monkeypatch):
    """真正传进引擎的是**解析后的路径**（spy 钉实参，不看代码）：缺省一路给的是 app 风格的
    信息库根，`none` 一路给 None——引擎侧据此决定要不要注入索引/传 tools（§10.2）。

    与"缺省到底是哪儿"分开钉：那条由 `test_default_libraries_root_is_the_repo_app_libraries`
    管；这条只管"解析结果被原样交给引擎"，免得 `main()` 里哪天漏传一次。
    """
    scene_p, card_p, models = _write_fixture(tmp_path)
    seen: list[object] = []
    real = runner.SceneEngine

    def _spy(*args, **kwargs):
        seen.append(kwargs.get("libraries_root"))
        return real(*args, **kwargs)

    monkeypatch.setattr(runner, "SceneEngine", _spy)

    runner.main(_argv(scene_p, card_p, models, tmp_path / "runs1"))
    runner.main(_argv(scene_p, card_p, models, tmp_path / "runs2",
                      "--libraries-root", "none"))

    assert seen == [runner._DEFAULT_LIBRARIES_ROOT, None]


def test_cli_seeds_awkward_lines_and_reports_what_it_did(tmp_path, capsys,
                                                         _isolate_libraries_root):
    """含文件名非法字符的种子行**照样进库**，且 CLI 把"改造过键"讲给用户听（§4.3/§9.1）。

    中文写作里半角引号与斜杠极常见（真实卡上就有），而键会成为文件名。绝不能因为守门
    过不去就静默丢掉这一行——那正是"用户资产一条不丢"这条最高约束被击穿的最短路径。
    改造过的行必须**有话说**（stderr），否则用户只看到"我写的东西没了"。
    """
    scene_p, card_p, models = _write_fixture(tmp_path)
    awkward = '第一批工程"存在"这一事实（东区/西区作战）'
    card_p.write_text(json.dumps({
        "name": A, "personality": {"描述": "话少"},
        "knowledge_boundary": [awkward, "知道：药铺的暗格"],
    }, ensure_ascii=False), encoding="utf-8")

    runner.main(_argv(scene_p, card_p, models, tmp_path / "runs"))

    lib = store.library_dir("character", A, root=_isolate_libraries_root)
    titles = [e.title for e in store.load_library(lib).library.entries.values()]
    assert titles == [awkward, "药铺的暗格"], "含引号/斜杠的那一行必须活着进库（原字保留）"
    assert awkward in capsys.readouterr().err, "改造过键的行要在 stderr 里说清楚（绝不静默）"


# -------------------------------------- 缺省开启后：空手一场的输出逐字节不变 --

def test_empty_scene_output_is_byte_identical_with_libraries_on_and_off(
        tmp_path, capsys):
    """**§13.3 硬约束**：缺省开启信息库之后，"什么都没记下的一场戏"输出与关掉时逐字节相同。

    这一场里没有任何角色查库/记东西（stub 后端不调工具），故 `prepare_settlement` 收不到
    任何所得、一个字都不打印。这条用例是"有没有信息库，用户看不出区别"的现行版本：谁哪天
    顺手加一句"[结算] 无所得"之类的状态输出，它先红。
    """
    scene_p, card_p, models = _write_fixture(tmp_path)
    card_p.write_text(json.dumps({"name": A, "personality": {"描述": "话少"}},
                                 ensure_ascii=False), encoding="utf-8")

    runner.main(_argv(scene_p, card_p, models, tmp_path / "runs_off",
                      "--libraries-root", "none"))
    off = capsys.readouterr().out

    runner.main(_argv(scene_p, card_p, models, tmp_path / "runs_on"))
    on = capsys.readouterr().out

    assert on == off, "缺省开启信息库后，空手一场的输出必须与今天逐字节一致"
    assert "结算" not in on and "默认策略" not in on
