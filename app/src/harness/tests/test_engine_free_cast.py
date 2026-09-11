"""引擎侧「人物与场景完全自由」（《界面与场景自由度_优化文档》§2/§3，T1）。

覆盖七件事：
  1. **场景持有阵容**：scene.characters 是唯一权威（打开即用它）；cast_from_cards 退化为
     「场景没有存过阵容时才用所选卡播种」；场景名单里的人缺卡时按需从角色库补载；
  2. **空场可运行**：一个人都没有也照常开场/走块钟，之后再 add_character 就能开口；
  3. **按需装卡**：add_character 时卡没装载 → 从角色库按名加载（文件名与卡名不同也认），
     库里没有 → 中文 ValueError；
  4. **阵容写回场景文件**：save_scene_file 把**在场者**（按演员表序）写回 characters，
     其余配置一字不动；自动保存同样写回；
  5. **热更新配置**：apply_scene_config 就地改活场景（含 start_time/hard_boundary 重算，
     且**空间键不变**）；
  6. **场景自改进变更流**：scene_changes() 记录钩子路径与工具路径，连续重复的不再记；
  7. **worker 那一层**：角色库目录进引擎、保存写两份（sidecar + 场景文件）、
     sig_scene_changed 按索引增量广播、apply_scene_config 投到活引擎。

全部离线确定性：stub 后端 + 零阈值竞价（每块必有人开口）。
"""
import asyncio
import json
import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6")          # worker 那一半用例要 Qt
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from harness import hooks as hooks_mod  # noqa: E402
from harness import scenarist as scenarist_mod  # noqa: E402
from harness.backends.stub import StubBackend  # noqa: E402
from harness.engine import SceneEngine  # noqa: E402
from harness.gui import worker as worker_mod  # noqa: E402
from harness.gui.worker import SceneWorker  # noqa: E402
from harness.loaders import load_scene  # noqa: E402


# --------------------------------------------------------------------- 素材 --
def _urge(urge: float) -> dict:
    return {"aroused": 0.5, "obligation_fulfilled": [], "goal_progress": 0.0,
            "addressed": None, "impression_of_speaker": None, "urge": urge}


def _card(path: Path, name: str) -> Path:
    path.write_text(json.dumps({"name": name, "personality": {"描述": name}},
                               ensure_ascii=False), encoding="utf-8")
    return path


def _env(tmp_path: Path, scene: dict | None = None, cards=("甲", "乙"), *,
         library=(), characters_dir="auto", **kw) -> SceneEngine:
    """一份场景文件 + 若干角色卡（都在 tmp_path 里，tmp_path 即角色库）+ 零阈值竞价。

    scene 缺省 = 场景自己存着甲/乙（**权威阵容**）；characters_dir 缺省 = tmp_path
    （「角色库就是这个目录」——库里有卡就能按需装载）。
    cards = 本次**装载**（构造期传入）的卡；library = 只躺在库里、本次没装载的卡。
    """
    scene = dict(scene if scene is not None
                 else {"name": "茶室", "characters": [{"name": "甲"}, {"name": "乙"}]})
    scene_p = tmp_path / "茶室.json"
    scene_p.write_text(json.dumps(scene, ensure_ascii=False), encoding="utf-8")
    paths = [_card(tmp_path / f"{n}.json", n) for n in cards]
    for n in library:
        _card(tmp_path / f"{n}.json", n)
    models_p = tmp_path / "models.yaml"
    models_p.write_text(
        "think:\n  backend: stub\n  model: stub\n  params: {}\n"
        "speak:\n  backend: stub\n  model: stub\n  params: {}\n"
        "narrate:\n  backend: stub\n  model: stub\n  params: {}\n", encoding="utf-8")
    bid_p = tmp_path / "bid.yaml"
    bid_p.write_text("interruption_threshold: 0.0\nspeak_threshold: 0.0\n"
                     "silence_k: 100000\n", encoding="utf-8")
    eng = SceneEngine(scene_p, paths, models_p, run_root=tmp_path / "runs",
                      bid_path=bid_p, auto_narrate=False,
                      characters_dir=(tmp_path if characters_dir == "auto"
                                      else characters_dir), **kw)
    # 括号内的占位台词 normalize 后为空串 → 永不被判「自我复读」，块块都有台词。
    eng.think_backend = StubBackend(json_script=[_urge(2.0)] * 60)
    eng.speak_backend = StubBackend(line_script=[f"（第{i}句占位台词。）"
                                                for i in range(60)])
    return eng


def _speakers(msgs: list[dict]) -> list[str]:
    return [m["speaker"] for m in msgs if m.get("speaker_type") == "character"]


def _blocks(events: list[dict]) -> list[str]:
    return [e["payload"]["kind"] for e in events if e["type"] == "decision"]


def _file_characters(eng: SceneEngine) -> list[dict]:
    """场景文件里的 characters（原始 dict 形态，**不经过活对象**）。"""
    return json.loads(Path(eng._scene_file()).read_text(encoding="utf-8"))["characters"]


# ===========================================================================
# 1. 场景持有阵容：scene.characters 是唯一权威（§3.1）
# ===========================================================================
def test_stored_cast_is_authoritative_over_selected_cards(tmp_path: Path):
    """场景存着甲/乙，本次装载了甲/乙/丙（cast_from_cards=True）→ 在场仍是甲/乙。

    这正是根因 1：旧口径用「本次装载的卡」覆盖场景名单，场景因此存不住人。
    """
    eng = _env(tmp_path, cards=("甲", "乙", "丙"), cast_from_cards=True)
    assert eng.scene.participants == ["甲", "乙"], "场景文件里的阵容说了算"
    assert eng.active_names() == ["甲", "乙"]
    assert "丙" in eng.cards, "丙的卡照常装载（库里可用），只是没被拉进这场"

    asyncio.run(eng.open_scene())
    asyncio.run(eng.step(3))
    assert set(_speakers(asyncio.run(eng.messages()))) == {"甲", "乙"}, \
        "未被场景持有的丙不得开口"


def test_cast_from_cards_seeds_only_a_scene_without_cast(tmp_path: Path):
    """场景**没有**存过阵容（characters 为空）→ cast_from_cards 才用所选卡播种。"""
    eng = _env(tmp_path, scene={"name": "空场", "characters": []},
               cards=("甲", "乙", "丙"), cast_from_cards=True)
    assert eng.scene.participants == ["甲", "乙", "丙"], "空场才按所选卡播种"
    assert all(m.entered_round == 0 and m.entered_at == "" for m in eng.scene.characters)

    asyncio.run(eng.open_scene())
    asyncio.run(eng.step(8))
    assert set(_speakers(asyncio.run(eng.messages()))) == {"甲", "乙", "丙"}, \
        "播种进来的三人都有机会开口"
    assert set(eng.dynamics_snapshot()) == {"甲", "乙", "丙"}


def test_cast_from_cards_false_ignores_selection(tmp_path: Path):
    """缺省（False）＝场景名单说了算：空场不会因为「选了卡」就凭空进人。"""
    eng = _env(tmp_path, scene={"name": "空场", "characters": []},
               cards=("甲", "乙"))
    assert eng.scene.participants == []


def test_stored_cast_member_missing_from_cards_is_loaded_from_library(tmp_path: Path):
    """场景存着乙，但本次只装载了甲（乙的卡交给角色库）→ 构造期按需补载，场景照开。"""
    eng = _env(tmp_path, cards=("甲",), library=("乙",))
    assert eng.scene.participants == ["甲", "乙"]
    assert "乙" in eng.cards, "缺的卡从角色库按需装载"
    asyncio.run(eng.open_scene())
    asyncio.run(eng.step(3))
    assert set(_speakers(asyncio.run(eng.messages()))) == {"甲", "乙"}


def test_stored_cast_member_without_any_card_still_fails_fast(tmp_path: Path):
    """库里也没有那张卡 → 仍然开场前 fail fast（不静默丢人）。"""
    with pytest.raises(ValueError, match="缺少角色卡"):
        _env(tmp_path, cards=("甲",), characters_dir=None)


# ===========================================================================
# 2. 空场照常运行（§3.1：没人 = 只是没有人在活动）
# ===========================================================================
def test_empty_scene_runs_blocks_and_late_joiner_speaks(tmp_path: Path):
    """空场：开场合法、连走 3 块（各一条 block_done）、无角色消息；加人后立刻开口。"""
    eng = _env(tmp_path, scene={"name": "空场", "characters": []}, cards=("甲",))
    events = asyncio.run(eng.open_scene())
    assert [e["type"] for e in events[:2]] == ["block_spoken", "scene_state"]
    assert eng.dynamics_snapshot() == {} and eng.speakable_names() == []

    events = asyncio.run(eng.step(3))
    assert _blocks(events) == ["block_done"] * 3, "空场每块照常落一条 block_done"
    msgs = asyncio.run(eng.messages())
    assert _speakers(msgs) == [], "空场没有任何角色消息"
    assert all(m["speaker_type"] == "director" for m in msgs)

    line = asyncio.run(eng.add_character("甲"))
    assert line is not None and eng.active_names() == ["甲"]
    asyncio.run(eng.step(2))
    assert _speakers(asyncio.run(eng.messages())) == ["甲"], "加进来的人下一块就开口"


def test_empty_scene_narration_still_lands(tmp_path: Path):
    """空场里场景叙述照常（叙述不依赖任何角色在场），且在场轴 = 场景名。"""
    eng = _env(tmp_path, scene={"name": "空场", "characters": []}, cards=())
    eng.narrate_backend = StubBackend(line_script=["门外传来脚步声。"])
    asyncio.run(eng.open_scene())
    line = asyncio.run(eng.narrate_now())
    assert line is not None and line["in_scene"] == "空场"
    assert line["speaker_type"] == "narrator"


# ===========================================================================
# 3. 按需装卡（§3.2：任何库中角色，任何时候都能加入任何场景）
# ===========================================================================
def test_add_character_loads_card_by_file_name(tmp_path: Path):
    """丙的卡在库里（丙.json）但本次没装载 → add_character 当场装载并注册。"""
    eng = _env(tmp_path, cards=("甲", "乙"), library=("丙",))
    asyncio.run(eng.open_scene())
    assert "丙" not in eng.cards
    asyncio.run(eng.add_character("丙"))
    assert "丙" in eng.cards and eng.cards["丙"].name == "丙"
    assert eng.cast_state()["active"] == ["甲", "乙", "丙"]
    asyncio.run(eng.step(2))
    assert "丙" in _speakers(asyncio.run(eng.messages())), "新装卡的人照常 think/speak"


def test_add_character_finds_card_whose_file_name_differs(tmp_path: Path):
    """卡名与文件名不一致（库里的 bing.json 里写着 name="丙"）→ 扫库按卡名兜底认出来。"""
    _card(tmp_path / "bing.json", "丙")
    eng = _env(tmp_path, cards=("甲", "乙"))
    asyncio.run(eng.open_scene())
    asyncio.run(eng.add_character("丙"))
    assert "丙" in eng.cards and eng.cast_state()["active"][-1] == "丙"


def test_add_character_missing_in_library_raises_clear_error(tmp_path: Path):
    """库里没有这张卡 → 中文 ValueError（可直接给用户看），且不改动任何状态。"""
    eng = _env(tmp_path, cards=("甲", "乙"))
    with pytest.raises(ValueError, match="角色库里没有找到《幽灵》的角色卡"):
        asyncio.run(eng.add_character("幽灵"))
    with pytest.raises(ValueError, match="角色库里没有找到《幽灵》的角色卡"):
        asyncio.run(eng.schedule_cast_change("幽灵", "add", 1))
    assert eng.cast_state()["active"] == ["甲", "乙"]


def test_add_character_without_library_dir_gives_same_error(tmp_path: Path):
    """没有配角色库目录（纯内存/离线引擎）→ 同一个中文报错，绝不静默失败。"""
    eng = _env(tmp_path, cards=("甲", "乙"))
    eng._characters_dir = None
    with pytest.raises(ValueError, match="角色库里没有找到《丙》的角色卡"):
        asyncio.run(eng.add_character("丙"))


def test_corrupt_card_in_library_is_skipped_when_scanning(tmp_path: Path):
    """扫库兜底遇到坏 json → 跳过继续找（坏文件不该让「加人」炸掉）。"""
    (tmp_path / "坏卡.json").write_text("{ 这不是 json", encoding="utf-8")
    _card(tmp_path / "bing.json", "丙")
    eng = _env(tmp_path, cards=("甲", "乙"))
    asyncio.run(eng.open_scene())
    asyncio.run(eng.add_character("丙"))
    assert "丙" in eng.cards


# ===========================================================================
# 4. 阵容写回场景文件（§3.1：运行期增删写回场景）
# ===========================================================================
def test_save_scene_file_writes_live_cast_and_keeps_config(tmp_path: Path):
    """save_scene_file：characters = **当前在场者**（按演员表序），其余字段一字不动。"""
    scene = {"name": "茶室", "date": "民国二十六年三月", "background": "城南",
             "description": "桌椅", "plot_direction": "谈信",
             "characters": [{"name": "甲", "entered_at": "21:35", "entered_round": 3},
                            {"name": "乙"}],
             "hard_boundary": {"type": "time", "value": "22:00", "desc": "打烊"}}
    eng = _env(tmp_path, scene=scene, cards=("甲", "乙", "丙"))
    asyncio.run(eng.open_scene())
    eng.set_clock_now(21 * 3600 + 40 * 60)
    asyncio.run(eng.remove_character("乙"))
    asyncio.run(eng.add_character("丙"))

    path = asyncio.run(eng.save_scene_file())
    assert path == eng._scene_file() and path.is_file()
    saved = load_scene(path)
    assert [m.name for m in saved.characters] == ["甲", "丙"], "只存在场者，按演员表序"
    assert next(m for m in saved.characters if m.name == "甲").entered_at == "21:35"
    assert next(m for m in saved.characters if m.name == "丙").entered_at == "21:40"
    assert (saved.name, saved.date, saved.background, saved.description,
            saved.plot_direction) == ("茶室", "民国二十六年三月", "城南", "桌椅", "谈信")
    assert saved.hard_boundary.value == "22:00"
    assert eng.scene_state()["save_error"] is None


def test_save_scene_file_records_failure_without_raising(tmp_path: Path):
    """写盘失败只记诊断、返回 None：存档失败绝不带垮正在跑的对话。"""
    eng = _env(tmp_path)
    asyncio.run(eng.open_scene())
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    eng._scene_path = blocker / "茶室.json"
    assert asyncio.run(eng.save_scene_file()) is None
    assert eng.scene_state()["save_error"]
    asyncio.run(eng.step(1))                      # 世界照常继续


def test_autosave_writes_cast_back_into_scene_file(tmp_path: Path):
    """自动保存同样写回阵容（§3.2：保存/自动保存/关闭时各写一次）。"""
    eng = _env(tmp_path, cards=("甲", "乙", "丙"))
    eng.set_autosave_every(2)
    asyncio.run(eng.open_scene())
    asyncio.run(eng.add_character("丙"))
    asyncio.run(eng.step(2))
    assert [m["name"] for m in _file_characters(eng)] == ["甲", "乙", "丙"], \
        "自动保存到点即把在场者写回场景文件"


# ===========================================================================
# 5. 热更新配置（§3.4：保存后立刻生效）
# ===========================================================================
def test_apply_scene_config_text_fields(tmp_path: Path):
    """可热更新字段就地生效，返回**真正改动**的字段；重复应用返回空表。"""
    eng = _env(tmp_path)
    changed = eng.apply_scene_config(
        name="雨夜的茶室", date="民国二十七年", background="新的背景",
        description="新的描述", description_mutable=True, plot_direction="新的走向")
    assert sorted(changed) == ["background", "date", "description",
                              "description_mutable", "name", "plot_direction"]
    assert eng.scene.name == "雨夜的茶室" and eng.scene.description == "新的描述"
    assert eng.scene.description_mutable is True
    assert eng._scene_space == "茶室", "空间键（不可变在场轴）绝不跟着改名走"
    assert eng.scene_state()["fields"]["name"] == "雨夜的茶室", "场景状态快照读的是活值"

    again = eng.apply_scene_config(name="雨夜的茶室", description="新的描述")
    assert again == [], "没有实际变化就不算改动"


def test_apply_scene_config_ignores_unknown_fields(tmp_path: Path):
    """不认识的键一律忽略（绝不往活场景对象上乱写属性）。"""
    eng = _env(tmp_path)
    assert eng.apply_scene_config(file_name="别的.json", path="/tmp/x") == []
    assert not hasattr(eng.scene, "file_name")


def test_apply_scene_config_recomputes_start_time_and_boundary(tmp_path: Path):
    """start_time / hard_boundary 改完要重算派生值（含跨午夜守卫）。"""
    eng = _env(tmp_path)
    assert eng.start_seconds == 21 * 3600 + 30 * 60
    changed = eng.apply_scene_config(
        start_time="23:30", hard_boundary={"type": "time", "value": "00:30",
                                           "desc": "散场"})
    assert sorted(changed) == ["hard_boundary", "start_time"]
    assert eng.start_time == "23:30" and eng.start_seconds == 23 * 3600 + 30 * 60
    assert eng.boundary_value == "00:30" and eng.boundary_hhmm == "00:30"
    assert eng.boundary_seconds == 30 * 60 + 86400, "打烊在开场之后 → 视为次日"
    assert eng._clock_progress() == 0.0

    assert eng.apply_scene_config(hard_boundary=None) == ["hard_boundary"]
    assert eng.boundary_seconds is None and eng.boundary_hhmm is None
    assert eng._clock_progress() is None, "去掉钟边界 → 场景压力不参与判定"


def test_apply_scene_config_start_time_also_updates_boundary(tmp_path: Path):
    """只改 start_time：跨午夜守卫要按新起点重算（否则打烊边界会算错一天）。"""
    eng = _env(tmp_path, scene={"name": "茶室", "characters": [{"name": "甲"}],
                                "start_time": "21:30",
                                "hard_boundary": {"type": "time", "value": "22:00",
                                                  "desc": "打烊"}})
    assert eng.boundary_seconds == 22 * 3600
    eng.apply_scene_config(start_time="22:30")            # 起点越过了打烊点
    assert eng.start_seconds == 22 * 3600 + 30 * 60
    assert eng.boundary_seconds == 22 * 3600 + 86400, "起点一改，边界跨午夜守卫重算"


def test_apply_scene_config_rejects_bad_start_time(tmp_path: Path):
    """非法时刻当场抛中文 ValueError（界面据此提示，不静默吞掉）。"""
    eng = _env(tmp_path)
    with pytest.raises(ValueError):
        eng.apply_scene_config(start_time="25:99")
    assert eng.start_time == "21:30", "非法输入不改动任何东西"


# ===========================================================================
# 6. 场景自改进变更流（§3.5：钩子路径 + 工具路径）
# ===========================================================================
def test_scene_changes_records_tool_path(tmp_path: Path):
    """工具路径：set_description / append_description / set_name / set_background 都记账。"""
    eng = _env(tmp_path)
    eng._apply_scene_tools([
        scenarist_mod.SceneToolCall("set_description", "新的描述"),
        scenarist_mod.SceneToolCall("set_background", "新的背景"),
        scenarist_mod.SceneToolCall("set_name", "新茶室"),
    ])
    changes = eng.scene_changes()
    assert [(c["field"], c["old"], c["new"]) for c in changes] == [
        ("name", "茶室", "新茶室"),
        ("description", "", "新的描述"),
        ("background", "", "新的背景"),
    ]
    assert all(c["at"] == 0 for c in changes), "块号随记录时的世界块钟"

    eng._apply_scene_tools([scenarist_mod.SceneToolCall("append_description", "又一句")])
    assert eng.scene_changes()[-1] == {
        "field": "description", "old": "新的描述", "new": "新的描述 又一句", "at": 0}


def test_scene_changes_records_hook_path_and_dedups(tmp_path: Path):
    """钩子路径（scene 事件）同样记账；**连续重复**的同一次改动不再记第二条。"""
    eng = _env(tmp_path)
    hook = hooks_mod.Hook(id="h1", condition="条件", event_kind="scene",
                          scene_patch={"description": "被钩子改了"})
    assert eng._apply_scene_hook(hook) == ["description"]
    assert eng.scene_changes() == [
        {"field": "description", "old": "", "new": "被钩子改了", "at": 0}]

    eng.scene.description = ""                    # 回到原值再来一次 = 同样的改动
    eng._apply_scene_hook(hook)
    assert len(eng.scene_changes()) == 1, "连续重复的同一次改动只记一条"

    other = hooks_mod.Hook(id="h2", condition="条件", event_kind="scene",
                           scene_patch={"description": "另一句"})
    eng._apply_scene_hook(other)
    assert len(eng.scene_changes()) == 2, "内容变了就是新的一条"


def test_scene_changes_flow_through_narration_tools(tmp_path: Path):
    """生产路径（叙述里的 [[TOOL:set_description]]）也进变更流——日志据此高亮。"""
    eng = _env(tmp_path, scene={"name": "茶室", "characters": [{"name": "甲"}],
                                "description_mutable": True})
    eng.narrate_backend = StubBackend(line_script=[
        "茶壶被碰倒了。\n[[TOOL:set_description]] 一地碎瓷。"])
    asyncio.run(eng.open_scene())
    asyncio.run(eng.narrate_now())
    assert eng.scene.description == "一地碎瓷。"
    assert [(c["field"], c["new"]) for c in eng.scene_changes()] == [
        ("description", "一地碎瓷。")]


def test_scene_changes_are_per_engine_and_reset_with_runtime(tmp_path: Path):
    """变更流是引擎侧只读流：重置运行上下文时一并清零（那是这一场的运行痕迹）。"""
    eng = _env(tmp_path)
    eng._apply_scene_tools([scenarist_mod.SceneToolCall("set_description", "改了")])
    assert eng.scene_changes()
    asyncio.run(eng.open_scene())
    asyncio.run(eng.reset_scene_runtime())
    assert eng.scene_changes() == []


# ===========================================================================
# 7. worker：角色库目录进引擎、保存两处、变更流广播（§3.1/§3.5）
# ===========================================================================
@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _wait_until(qapp, cond, timeout_ms: int = 4000, step_ms: int = 10) -> bool:
    loop = QEventLoop()
    ok = {"v": False}
    timer = QTimer()
    timer.setInterval(step_ms)

    def _tick():
        if cond():
            ok["v"] = True
            timer.stop()
            loop.quit()

    timer.timeout.connect(_tick)
    timer.start()
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    timer.stop()
    return ok["v"]


class _FakeEngine:
    """最小引擎替身：worker 的保存/配置/变更流路径会碰到的那些接口。"""

    def __init__(self, changes: list[dict] | None = None):
        self.saved_state = 0
        self.saved_file = 0
        self.configs: list[dict] = []
        self._changes = list(changes or [])
        self._msgs: list[dict] = []

    async def messages(self):
        return list(self._msgs)

    def cast_state(self):
        return {"active": ["甲"], "inactive": [], "muted": {}}

    def dynamics_snapshot(self):
        return {"甲": {"bid": 0.0}}

    def dynamic_states(self):
        return {}

    def think_log_tail(self, n: int = 200):
        return []

    def scene_state(self):
        return {"language": "zh-Hans", "hooks": [], "description_mutable": False,
                "fields": {}, "hook_error": None, "tool_error": None,
                "save_error": None, "implicit_events": []}

    def scene_changes(self):
        return [dict(c) for c in self._changes]

    async def save_scene_state(self):
        self.saved_state += 1
        return Path("/tmp/茶室.json.runtime.json")

    async def save_scene_file(self):
        self.saved_file += 1
        return Path("/tmp/茶室.json")

    def apply_scene_config(self, **fields):
        self.configs.append(dict(fields))
        return sorted(fields)


def _worker_with(engine) -> SceneWorker:
    worker = SceneWorker()
    worker.start()
    assert worker._ready.wait(5.0), "worker 事件循环未起"
    worker._engine = engine
    return worker


def test_worker_passes_characters_dir_derived_from_loaded_cards(tmp_path, monkeypatch):
    """worker 把角色库目录交给引擎（未显式给时＝已装载卡的所在目录）。"""
    seen: dict = {}

    class _Rec:
        def __init__(self, *a, **kw):
            seen.update(kw)

    monkeypatch.setattr(worker_mod, "SceneEngine", _Rec)
    worker = SceneWorker()
    worker._cfg = {"scene": tmp_path / "s.json",
                   "characters": [tmp_path / "甲.json"], "models": tmp_path / "m.yaml",
                   "bid": None, "live": False, "api_key": None,
                   "run_root": tmp_path / "runs", "closing_at_block": None,
                   "start_time": None, "cast_from_cards": False}
    asyncio.run(worker._build_engine())
    assert seen["characters_dir"] == tmp_path

    worker._cfg["characters_dir"] = tmp_path / "lib"      # 显式给 → 以显式为准
    asyncio.run(worker._build_engine())
    assert seen["characters_dir"] == tmp_path / "lib"


def test_worker_save_scene_writes_sidecar_and_scene_file(qapp):
    """save_scene = sidecar + 场景文件（阵容写回），两者都写才发 sig_saved。"""
    engine = _FakeEngine()
    worker = _worker_with(engine)
    saved: list[str] = []
    worker.sig_saved.connect(saved.append)
    try:
        worker.save_scene()
        assert _wait_until(qapp, lambda: bool(saved)), "应发 sig_saved"
        assert engine.saved_state == 1 and engine.saved_file == 1, \
            "侧车与场景文件都要写"
    finally:
        worker.shutdown(4000)


def test_worker_emits_scene_changes_incrementally(qapp):
    """sig_scene_changed：按索引增量派发新条目（同一批不重复推给界面）。"""
    engine = _FakeEngine(changes=[{"field": "description", "old": "a", "new": "b",
                                   "at": 1}])
    worker = _worker_with(engine)
    seen: list[list[dict]] = []
    worker.sig_scene_changed.connect(seen.append)
    try:
        worker.save_scene()
        assert _wait_until(qapp, lambda: bool(seen)), "变更流应经 sig_scene_changed 上屏"
        assert seen[-1] == [{"field": "description", "old": "a", "new": "b", "at": 1}]

        worker.save_scene()                    # 没有新条目 → 不再广播
        assert _wait_until(qapp, lambda: engine.saved_file == 2)
        assert len(seen) == 1, "没有新条目就不该重复广播"

        engine._changes.append({"field": "name", "old": "茶室", "new": "新茶室",
                                "at": 2})
        worker.save_scene()
        assert _wait_until(qapp, lambda: len(seen) == 2)
        assert [c["field"] for c in seen[-1]] == ["name"], "只推新增的那条"
    finally:
        worker.shutdown(4000)


def test_worker_apply_scene_config_reaches_engine(qapp):
    """「配置场景」热更新：字段投到引擎，随后刷新界面（sig_cast）+ 广播变更流。"""
    engine = _FakeEngine()
    worker = _worker_with(engine)
    casts: list[dict] = []
    worker.sig_cast.connect(casts.append)
    try:
        worker.apply_scene_config(name="新茶室", description="新描述")
        assert _wait_until(qapp, lambda: bool(engine.configs))
        assert engine.configs == [{"name": "新茶室", "description": "新描述"}]
        assert _wait_until(qapp, lambda: bool(casts)), "应用后应刷新演员表/界面"
    finally:
        worker.shutdown(4000)


def test_worker_apply_scene_config_before_engine_is_silent(qapp):
    """没开场/线程没起时调配置应用：静默忽略，绝不把异常抛进 GUI 槽。"""
    worker = SceneWorker()
    worker.apply_scene_config(name="新茶室")          # 只构造不 start
    assert worker.can_cast() is False
