"""场景运行时存档 sidecar（scenestore，设计文档 §5 保存/续演）。

覆盖四件事：
  1) 路径换算：`餐厅.json` → `餐厅.json.runtime.json`，与场景文件名本身不撞名；
  2) 往返：带混合可选键（knows/address/time_hhmmss/额外键）的长转录 + 虚拟钟 +
     块数 + meta，存了再读**逐字节深等**；同时场景文件本体一个字节都不许动；
  3) 健壮性：缺失 / 空 / 坏 JSON / 顶层类型错 / 部分字段烂 → 空 bundle 或逐字段
     回退，**绝不抛**（存档坏了不能拖垮启动）；落盘走临时文件 + os.replace，且不留
     垃圾临时文件；
  4) 自动保存节流：AutosavePolicy 的边界（every<=0 禁用、差一个周期才触发）与
     AutosaveTracker 的入账点推进/复位。

全程 `tmp_path`，无网络、无 Qt、无真实时钟依赖（saved_at 只在「自动补」那条断言
里被解析，不断言具体值）。<1s。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from harness.loaders import load_scene
from harness.scenestore import (
    RUNTIME_SUFFIX,
    AutosavePolicy,
    AutosaveTracker,
    RuntimeBundle,
    clear_runtime,
    has_runtime,
    load_runtime,
    runtime_path_for,
    save_runtime,
)

#: 场景文件本体（放 tmp_path，当「真实场景文件」用；sidecar 必须另起一个文件）。
SCENE_NAME = "餐厅.json"


def _scene_file(tmp_path: Path) -> Path:
    """写一个语法上完全合法的场景文件，返回其路径（内容见 _scene_bytes）。"""
    return _write_scene(tmp_path / SCENE_NAME)


def _scene_bytes(name: str = "餐厅") -> bytes:
    """场景文件字节：固定内容，用于「保存 sidecar 后场景文件分毫未动」的比对。"""
    return json.dumps(
        {"name": name, "start_time": "21:30",
         "characters": [{"name": "甲", "entered_at": "21:30", "entered_round": 0}]},
        ensure_ascii=False, indent=2).encode("utf-8")


def _write_scene(path: Path) -> Path:
    path.write_bytes(_scene_bytes())
    return path


def _long_transcript(n: int = 200) -> list[dict]:
    """混合可选键的确定性长转录：每类键按不同周期出现，另加一个自定义额外键。"""
    speakers = ["甲", "乙", "导演", "旁白", "用户", "系统"]
    types = ["character", "character", "director", "narrator", "human", "system"]
    out: list[dict] = []
    for i in range(n):
        msg: dict = {
            "id": i + 1,
            "speaker": speakers[i % len(speakers)],
            "speaker_type": types[i % len(types)],
            "content": f"第 {i + 1} 句：文本含中文与 \"引号\" 和换行\n第二行",
            "in_scene": "餐厅",
            "turn": i // 2,
        }
        if i % 3 == 0:
            msg["knows"] = ["甲"]
        elif i % 3 == 1:
            msg["knows"] = None            # 显式 None 也得原样往返
        if i % 4 == 0:
            msg["address"] = "乙"
        if i % 5 == 0:
            msg["time_hhmmss"] = "21:30:00"
        if i % 7 == 0:
            msg["note"] = {"extra": [1, 2, {"深层": "也要留住"}]}   # 未知键不丢
        out.append(msg)
    return out


# ------------------------------------------------------------------ 路径换算 --
def test_runtime_path_for_appends_suffix(tmp_path):
    """`餐厅.json` → `餐厅.json.runtime.json`；同目录、不改场景文件名。"""
    scene = tmp_path / SCENE_NAME
    side = runtime_path_for(scene)
    assert side == tmp_path / "餐厅.json.runtime.json"
    assert side.name == "餐厅.json" + RUNTIME_SUFFIX
    assert side.parent == scene.parent
    assert str(side).endswith(RUNTIME_SUFFIX)


def test_runtime_path_does_not_collide_with_scene_file(tmp_path):
    """sidecar 不是场景文件本身：两个不同场景各得其所，也不会互相覆盖。"""
    a = tmp_path / "餐厅.json"
    b = tmp_path / "咖啡厅.json"
    assert runtime_path_for(a) != a
    assert runtime_path_for(a) != runtime_path_for(b)
    # 带点的场景名不会被 with_suffix 那类换算吃掉原名（`a.b.json` 仍是整名 + 后缀）。
    assert runtime_path_for(tmp_path / "a.b.json").name == "a.b.json" + RUNTIME_SUFFIX


# ---------------------------------------------------------------------- 往返 --
def test_round_trip_exact_deep_equality(tmp_path):
    """200 条混合键消息 + 钟 + 块数 + meta：存→读 深等，可选键与额外键一个不少。"""
    scene = _scene_file(tmp_path)
    msgs = _long_transcript(200)
    bundle = RuntimeBundle(transcript=msgs, clock_seconds=77412, saved_at="2026-09-10T12:00:00+00:00",
                           blocks=37, meta={"muted": ["乙"], "tags": {"a": [1, 2]}})
    path = save_runtime(scene, bundle)

    loaded = load_runtime(scene)
    assert path == runtime_path_for(scene)
    assert loaded == bundle                      # dataclass 深等（含嵌套 dict/list）
    assert loaded.transcript == msgs
    assert loaded.transcript[0]["knows"] == ["甲"]
    assert loaded.transcript[1]["knows"] is None
    assert loaded.transcript[4]["address"] == "乙"      # i%4==0
    assert loaded.transcript[7]["note"] == {"extra": [1, 2, {"深层": "也要留住"}]}  # i%7==0
    assert "time_hhmmss" in loaded.transcript[0]
    assert loaded.meta == {"muted": ["乙"], "tags": {"a": [1, 2]}}
    # 落盘是人类可读的 UTF-8 中文 + 缩进（不当 \uXXXX 藏起来，方便人看/手改）。
    text = path.read_text(encoding="utf-8")
    assert "甲" in text and "\\u767d" not in text
    assert text.startswith("{\n  \"transcript\"")


def test_save_leaves_scene_file_byte_identical(tmp_path):
    """保存运行时不许碰场景文件：字节、mtime 都不变，且仍旧可被 load_scene 装载。"""
    scene = _write_scene(tmp_path / SCENE_NAME)
    before = scene.read_bytes()
    before_mtime = os.stat(scene).st_mtime_ns

    save_runtime(scene, RuntimeBundle(transcript=[{"id": 1}], clock_seconds=10))
    save_runtime(scene, RuntimeBundle(transcript=[{"id": 2}], blocks=3))

    assert scene.read_bytes() == before
    assert os.stat(scene).st_mtime_ns == before_mtime
    assert load_scene(scene).name == "餐厅"       # schema/文件名都没被改动
    assert runtime_path_for(scene).exists()       # 状态进的是 sidecar，不是场景文件


def test_saved_at_autofilled_only_when_empty(tmp_path):
    """调用方给了 saved_at 就尊重；没给则由 save 补一个 ISO-8601 UTC 时刻。"""
    scene = _scene_file(tmp_path)

    save_runtime(scene, RuntimeBundle(saved_at="2020-01-02T03:04:05+00:00"))
    assert load_runtime(scene).saved_at == "2020-01-02T03:04:05+00:00"

    save_runtime(scene, RuntimeBundle())
    stamped = load_runtime(scene).saved_at
    assert stamped and datetime.fromisoformat(stamped).utcoffset().total_seconds() == 0


def test_save_creates_missing_parent_dirs(tmp_path):
    """场景文件在尚未创建的子目录里也能存（自动建父目录）。"""
    scene = tmp_path / "新库" / "子目录" / SCENE_NAME
    path = save_runtime(scene, RuntimeBundle(blocks=1))
    assert path.exists() and load_runtime(scene).blocks == 1


# -------------------------------------------------------------------- 原子性 --
def test_resave_keeps_sidecar_loadable_and_leaves_no_temp_files(tmp_path):
    """已有好存档时再存一次：读到的必须是新的完整内容，且不留 .tmp 垃圾。"""
    scene = _scene_file(tmp_path)
    # saved_at 显式给，才能整包 deep-eq（空 saved_at 会被 save 补上当前时刻）。
    first = RuntimeBundle(transcript=[{"id": 1, "content": "旧"}], blocks=1,
                          saved_at="2026-09-10T00:00:01+00:00")
    second = RuntimeBundle(transcript=[{"id": 2, "content": "新"}], blocks=2,
                           saved_at="2026-09-10T00:00:02+00:00")
    save_runtime(scene, first)
    assert load_runtime(scene) == first

    save_runtime(scene, second)
    assert load_runtime(scene) == second
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        [SCENE_NAME, SCENE_NAME + RUNTIME_SUFFIX])
    # 覆盖是 os.replace 原子替换：第二次保存后不存在半截文件（能完整解析）。
    json.loads(runtime_path_for(scene).read_text(encoding="utf-8"))


# -------------------------------------------------------------------- 健壮性 --
@pytest.mark.parametrize("raw", [
    "",                        # 空文件
    "{not json",               # 坏 JSON
    "[]",                      # 顶层是列表
    '"nope"',                  # 顶层是字符串
    "null",                    # 顶层是 null
    '{"transcript": "nope"}',  # 字段类型错
    '{"transcript": 42}',
    '{"meta": [1, 2]}',
    '{"clock_seconds": "3600", "blocks": "x", "saved_at": 7}',
])
def test_load_runtime_never_raises_and_falls_back(tmp_path, raw):
    """缺失 / 空 / 坏 JSON / 类型错 → 空 bundle；一律不抛。"""
    scene = _scene_file(tmp_path)
    assert load_runtime(scene) == RuntimeBundle()          # 先测「文件都没有」
    runtime_path_for(scene).write_text(raw, encoding="utf-8")
    assert load_runtime(scene) == RuntimeBundle()          # 再测「有但烂」
    assert has_runtime(scene) is True                      # 文件在，只是内容不可用


def test_load_runtime_survives_directory_in_place_of_file(tmp_path):
    """sidecar 位置上是个目录（或任何读不了的东西）也不能崩。"""
    scene = _scene_file(tmp_path)
    runtime_path_for(scene).mkdir()
    assert load_runtime(scene) == RuntimeBundle()
    assert has_runtime(scene) is False
    assert clear_runtime(scene) is False


def test_partially_valid_bundle_keeps_good_fields(tmp_path):
    """一个字段烂不带垮其它字段：能救的字段照常回来，烂的各自回默认。"""
    scene = _scene_file(tmp_path)
    runtime_path_for(scene).write_text(json.dumps({
        "transcript": [{"id": 1, "content": "留"}, "垃圾", 3],   # 非 dict 元素剔除
        "clock_seconds": 3600,                                    # 好字段
        "saved_at": 5,                                            # 类型错 → ""
        "blocks": -1,                                             # 无意义 → 0
        "meta": [1, 2],                                           # 类型错 → {}
        "未来字段": "忽略",
    }, ensure_ascii=False), encoding="utf-8")

    bundle = load_runtime(scene)
    assert bundle.transcript == [{"id": 1, "content": "留"}]
    assert bundle.clock_seconds == 3600
    assert bundle.saved_at == ""
    assert bundle.blocks == 0
    assert bundle.meta == {}


def test_only_blocks_present_survives(tmp_path):
    """只写了 blocks 的最小存档：该字段留存，其余默认。"""
    scene = _scene_file(tmp_path)
    runtime_path_for(scene).write_text('{"blocks": 7}', encoding="utf-8")
    loaded = load_runtime(scene)
    assert loaded.blocks == 7
    assert loaded.transcript == [] and loaded.clock_seconds is None
    assert loaded.saved_at == "" and loaded.meta == {}


def test_from_dict_and_to_dict_are_pure(tmp_path):
    """纯 helper：非 dict 输入 → 空 bundle；to_dict 深拷贝，改返回值不污染本体。"""
    assert RuntimeBundle.from_dict(None) == RuntimeBundle()
    assert RuntimeBundle.from_dict(["x"]) == RuntimeBundle()
    assert RuntimeBundle.from_dict("nope") == RuntimeBundle()

    bundle = RuntimeBundle(transcript=[{"id": 1}], meta={"m": [1]}, clock_seconds=5, blocks=2)
    data = bundle.to_dict()
    assert data == {"transcript": [{"id": 1}], "clock_seconds": 5,
                    "saved_at": "", "blocks": 2, "meta": {"m": [1]}}
    data["transcript"].append({"id": 2})
    data["meta"]["m"].append(2)
    assert bundle.transcript == [{"id": 1}] and bundle.meta == {"m": [1]}
    assert RuntimeBundle.from_dict(bundle.to_dict()) == bundle


def test_bool_is_not_an_int(tmp_path):
    """True/False 是 int 子类，但不该被当成钟秒/块数收下。"""
    scene = _scene_file(tmp_path)
    runtime_path_for(scene).write_text('{"clock_seconds": true, "blocks": true}',
                                      encoding="utf-8")
    loaded = load_runtime(scene)
    assert loaded.clock_seconds is None and loaded.blocks == 0


# ------------------------------------------------------------- 清除 / 存在性 --
def test_clear_and_has_runtime(tmp_path):
    """clear 真删→True，重复删→False；has 与文件存在与否同步。"""
    scene = _scene_file(tmp_path)
    assert has_runtime(scene) is False
    assert clear_runtime(scene) is False          # 本来就没有

    save_runtime(scene, RuntimeBundle(blocks=1))
    assert has_runtime(scene) is True
    assert clear_runtime(scene) is True
    assert has_runtime(scene) is False
    assert clear_runtime(scene) is False
    assert load_runtime(scene) == RuntimeBundle()  # 清完就是「没有存档」
    assert scene.exists()                          # 清存档绝不动场景文件


# ---------------------------------------------------------------- 自动保存 --
def test_policy_disabled_when_every_is_not_positive():
    """every<=0 = 禁用：任何时候都不保存，包括刚跑完一大段。"""
    for every in (0, -1, -20):
        policy = AutosavePolicy(every=every)
        for blocks in (0, 1, 5, 20, 100):
            assert policy.should_save(blocks) is False
            assert policy.should_save(blocks, last_saved_blocks=0) is False


def test_policy_boundaries():
    """every=5：差一个周期才触发；刚好等于上次入账点不重复触发。"""
    policy = AutosavePolicy(every=5)
    assert policy.should_save(4) is False           # 差一块
    assert policy.should_save(5) is True            # 到点
    assert policy.should_save(10, last_saved_blocks=5) is True
    assert policy.should_save(9, last_saved_blocks=5) is False
    assert policy.should_save(5, last_saved_blocks=5) is False
    assert policy.should_save(0) is False           # 还没跑过块
    assert policy.should_save(-3) is False
    assert policy.should_save(5, last_saved_blocks=None) is True
    assert policy.should_save(6, last_saved_blocks=0) is True
    # 周期中途改大（5→50）：以「距上次入账点的累计差」判定，不因取模错乱。
    assert AutosavePolicy(every=50).should_save(20, last_saved_blocks=0) is False


def test_tracker_advances_marker_only_on_boundary():
    """note 到点才推进 last_saved_blocks，且推进入账点本身（不是块号取整）。"""
    tracker = AutosaveTracker(policy=AutosavePolicy(every=5))
    assert tracker.last_saved_blocks is None

    assert tracker.note(4) is False
    assert tracker.last_saved_blocks is None        # 没到点不记

    assert tracker.note(5) is True
    assert tracker.last_saved_blocks == 5

    assert tracker.note(9) is False
    assert tracker.note(10) is True
    assert tracker.last_saved_blocks == 10
    assert tracker.note(10) is False                # 同一块重复调用不重复保存
    assert tracker.note(14) is False
    assert tracker.note(15) is True
    assert tracker.last_saved_blocks == 15


def test_tracker_reset_restarts_counting():
    """reset 清掉入账点（打开/重置场景后从 0 起算），但不动 policy。"""
    tracker = AutosaveTracker(policy=AutosavePolicy(every=20))
    assert tracker.note(20) is True
    tracker.reset()
    assert tracker.last_saved_blocks is None
    assert tracker.policy.every == 20
    assert tracker.note(19) is False
    assert tracker.note(20) is True


def test_tracker_reset_is_the_callers_duty_on_open_or_reset():
    """D1：reset() 只是**给调用方的挂点**——本类不认识场景，不会自己察觉「换了场景」。

    约定用法：打开/重置场景 → reset() → 新场景块号从 0 起算。漏调的后果（沿用上一场
    入账点、第一次自动保存偏晚）在这里钉死，免得以后有人以为引擎侧会自动 reset。
    """
    tracker = AutosaveTracker(policy=AutosavePolicy(every=20))
    assert tracker.note(20) is True

    # 漏调：新场景第 1..19 块全被上一场入账点挡着（要跑到第 40 块才存第一次）
    for blocks in range(1, 20):
        assert tracker.note(blocks) is False
    assert tracker.last_saved_blocks == 20

    # 约定用法：打开/重置后由调用方 reset()，新场景第 20 块即存
    tracker.reset()
    assert tracker.last_saved_blocks is None
    assert tracker.note(20) is True


def test_tracker_reset_docstring_states_the_caller_contract():
    """D1：文档必须写明「必须由调用方在打开/重置时调」——旧文案「用于…后重新起算」
    读起来像已接好的线，而全仓库无人调用它（引擎接线在别处）。"""
    assert "必须" in AutosaveTracker.reset.__doc__
    assert "必须" in AutosaveTracker.__doc__


def test_tracker_with_disabled_policy_never_saves():
    """禁用周期下 tracker 纯空转（每块都调也不写盘）。"""
    tracker = AutosaveTracker(policy=AutosavePolicy(every=0))
    for blocks in range(1, 30):
        assert tracker.note(blocks) is False
    assert tracker.last_saved_blocks is None


def test_scene_listing_hides_runtime_sidecar(tmp_path):
    """旁挂存档不得出现在场景库列表里（否则每存一次界面上就多一个打不开的幽灵条目）。"""
    from harness.loaders import list_scene_paths
    from harness.scenestore import RuntimeBundle, runtime_path_for, save_runtime

    scene = tmp_path / "餐厅.json"
    scene.write_text('{"name": "餐厅"}', encoding="utf-8")
    assert list_scene_paths(tmp_path) == [scene]

    save_runtime(scene, RuntimeBundle(transcript=[{"id": 1}]))
    assert runtime_path_for(scene).exists()
    assert list_scene_paths(tmp_path) == [scene], "旁挂存档混进了场景列表"
