"""S6 接线（离屏、确定性、不联网）：把引擎侧已有能力真正接进桌面外壳。

覆盖五条接线（设计文档 §2.1①/§2.3/§5）：
  1. 「保存场景」按钮 → worker.save_scene()；sig_saved(path) → 状态区「已保存：<文件名>」；
     本场没有可写的场景文件时只报「当前场景未落盘，无法保存」，绝不投递保存动作；
  2. 自动保存周期：启动时把设置里的值推给 worker，菜单一改即再推一次（5/20/50/100；
     0 在 worker/引擎侧仍是「关闭」）；
  3. 续演：打开场景时若 sidecar 有存档才问「接着上次继续演吗」——Yes → 开场成功后
     worker.restore_scene()；No → 清掉存档且不恢复；没有存档 → 既不问也不动；
     **所有打开路径共用 MainWindow._maybe_resume 一处**（三条打开路径 + app 启动各验一遍）；
  4. 「重置场景运行上下文…」：确认后 worker.reset_scene()；
  5. api 配置：设置里的 url / key / 模型名推到 worker 并进引擎（base_url / model_override
     真正落到后端构造）；空值完全等于今日行为；app.py 的优先级
     「--stub > 设置里的 key > 环境变量 > stub」由纯函数锁定。

全部离屏（QT_QPA_PLATFORM=offscreen）、不联网、不碰真实用户设置（默认设置路径一律
monkeypatch 到 tmp_path）。若环境未装 PySide6（可选依赖 gui），整模块 skip。
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from harness import scenestore as scenestore_mod  # noqa: E402
from harness.engine import SceneEngine  # noqa: E402
from harness.gui import main_window as mw_mod  # noqa: E402
from harness.gui import settings as settings_mod  # noqa: E402
from harness.gui import worker as worker_mod  # noqa: E402
from harness.gui.app import resolve_api_key, resolve_live  # noqa: E402
from harness.gui.main_window import AppConfig, MainWindow  # noqa: E402
from harness.gui.settings import AppSettings, SettingsStore  # noqa: E402
from harness.gui.worker import SceneWorker  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    """模块级复用单一 QApplication（Qt 只允许每进程一个）。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def tmp_store(tmp_path, monkeypatch):
    """把「默认设置路径」指到临时目录（窗口构造/落盘都只碰这个文件）。"""
    path = tmp_path / "settings" / "settings.json"
    monkeypatch.setattr(settings_mod, "default_settings_path", lambda: path)
    return path


# --------------------------------------------------------------------- 素材
def _write_card(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / f"{name}.json"
    p.write_text(json.dumps({"name": name, "personality": {"描述": "示例"}},
                            ensure_ascii=False), encoding="utf-8")
    return p


def _write_scene(directory: Path, name: str, participants: list[str]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / f"{name}.json"
    p.write_text(json.dumps({
        "name": name, "participants": list(participants),
        "circles": [{"id": "主圈", "members": list(participants)}]},
        ensure_ascii=False), encoding="utf-8")
    return p


def _card_payload(name: str) -> dict:
    """右栏卡要的那种载荷（与 worker.build_scene_payload 同形）。"""
    return {"name": name, "personality": {"描述": "冷静"}, "abilities": [],
            "relationships": {}, "weights": {}, "emotion_decay_rate": 0.4,
            "corpus": {}}


def _scene_info(names: list[str], scene_path: Path | None = None) -> dict:
    """一次 sig_scene_info 载荷（只给 MainWindow 真正读到的字段）。"""
    return {"backend": "stub", "auto_narrate": True,
            "scene": {"name": "茶室", "participants": list(names),
                      "date": "", "hard_boundary": None},
            "scene_path": (str(scene_path) if scene_path is not None else None),
            "start_time": "21:30", "boundary_time": None,
            "characters": [_card_payload(n) for n in names]}


class _FakeWorker(QObject):
    """替身 worker：记账 + 提供 MainWindow 连接的信号（不起线程、不碰引擎）。"""

    sig_scene_info = Signal(dict)
    sig_message = Signal(dict)
    sig_metrics = Signal(dict)
    sig_status = Signal(str)
    sig_finished = Signal()
    sig_dynamics = Signal(dict)
    sig_think = Signal(dict)
    sig_narration = Signal(dict)
    sig_retracted = Signal(int)
    sig_cast = Signal(dict)
    sig_saved = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.saved = 0
        self.restored = 0
        self.reset = 0
        self.autosave: list[int] = []          # set_autosave_every 的实参（按序）
        self.api_configs: list[tuple] = []     # set_api_config 的实参（按序）
        self.started: list[dict] = []          # start_scene 的 kwargs

    # ---- S6 接线面（与 SceneWorker 同名同签名）----
    def save_scene(self) -> None:
        self.saved += 1

    def restore_scene(self) -> None:
        self.restored += 1

    def reset_scene(self) -> None:
        self.reset += 1

    def set_autosave_every(self, n: int) -> None:
        self.autosave.append(int(n))

    def set_api_config(self, base_url: str, api_key: str, model: str) -> None:
        self.api_configs.append((str(base_url), str(api_key), str(model)))

    # ---- 窗口构造/切场会用到的其余 worker 面 ----
    def start_scene(self, **kwargs) -> None:
        self.started.append(kwargs)

    def can_cast(self) -> bool:
        return False

    def can_say(self) -> bool:
        return False

    def restart(self, *a, **k) -> None:
        pass

    def shutdown(self, *a) -> None:
        pass

    def isRunning(self) -> bool:                  # noqa: N802
        return False


def _make_window(tmp_path: Path, *, settings: AppSettings | None = None,
                 participants: tuple[str, ...] = ("甲", "乙")):
    """真实 MainWindow 装配（替身 worker，不 show、不起线程）+ 素材目录。

    返回 (窗口, 替身 worker, 场景文件, (角色库目录, 场景库目录))。
    """
    cdir, sdir = tmp_path / "characters", tmp_path / "scenes"
    cards = [_write_card(cdir, n) for n in participants]
    scene = _write_scene(sdir, "茶室", list(participants))
    models = tmp_path / "models.yaml"
    models.write_text("think: {backend: stub, model: stub, params: {}}\n"
                      "speak: {backend: stub, model: stub, params: {}}\n",
                      encoding="utf-8")
    cfg = AppConfig(scene=scene, characters=cards, models=models,
                    bid=tmp_path / "bid.yaml", run_root=tmp_path / "runs",
                    live=False, api_key=None, opening="夜里。")
    worker = _FakeWorker()
    win = MainWindow(worker, cfg, settings=settings)
    return win, worker, scene, (cdir, sdir)


# ====================================================== 1. 保存场景按钮
def test_save_button_hands_off_to_worker_and_reports_saved_name(qapp, tmp_path,
                                                                tmp_store):
    """开场后点「保存场景」→ worker.save_scene；sig_saved → 「已保存：<文件名>」。"""
    win, worker, scene, _dirs = _make_window(tmp_path)
    assert not win._save_scene_btn.isEnabled(), "开场前没有可存的场次"

    worker.sig_scene_info.emit(_scene_info(["甲", "乙"], scene_path=scene))
    assert win._save_scene_btn.isEnabled(), "开场成功即启用"

    win._save_scene_btn.click()
    assert worker.saved == 1, "点按钮应交给 worker（落盘在引擎侧）"

    # sig_saved 报的是 sidecar 路径（<场景>.json.runtime.json）；状态区报**场景文件**名，
    # 免得把那层内部后缀摊给用户看。
    worker.sig_saved.emit(str(scenestore_mod.runtime_path_for(scene)))
    assert win._status_chip.text() == f"已保存：{scene.name}"
    assert win._saved_path == scenestore_mod.runtime_path_for(scene), "应记住落盘路径"


def test_saved_status_falls_back_to_saved_name_without_scene_file(qapp, tmp_path,
                                                                 tmp_store):
    """没有场景文件坐标时，状态区退回报存档自己的文件名（任何时候都有个名字可看）。"""
    win, worker, _scene, _dirs = _make_window(tmp_path)
    worker.sig_scene_info.emit(_scene_info(["甲"], scene_path=None))

    worker.sig_saved.emit("/tmp/x.json")
    assert win._status_chip.text() == "已保存：x.json"


def test_save_without_scene_file_only_reports_never_fakes(qapp, tmp_path, tmp_store):
    """本场没有可写的场景文件：只报「未落盘，无法保存」，不投递任何保存动作。"""
    win, worker, _scene, _dirs = _make_window(tmp_path)
    worker.sig_scene_info.emit(_scene_info(["甲", "乙"], scene_path=None))

    win._on_save_scene()
    assert win._status_chip.text() == "当前场景未落盘，无法保存"
    assert worker.saved == 0, "没得可存就不该发保存动作（不假装保存）"


# ====================================================== 2. 自动保存周期
def test_startup_pushes_saved_autosave_every_to_worker(qapp, tmp_path, tmp_store):
    """启动：settings.json 里的周期在构造窗口时就推给 worker（第一场即带上去）。"""
    win, worker, _scene, _dirs = _make_window(
        tmp_path, settings=AppSettings(autosave_every=50))
    assert win._autosave_every == 50
    assert worker.autosave == [50], "启动应把保存的周期推给 worker"


def test_autosave_menu_change_pushes_new_value(qapp, tmp_path, tmp_store):
    """改「自动保存周期」菜单：落盘 + 立刻把新周期推给 worker（下一块起生效）。"""
    win, worker, _scene, _dirs = _make_window(tmp_path)
    assert worker.autosave == [20], "启动先把设置里的缺省周期推一次"

    win._autosave_actions[5].trigger()
    assert worker.autosave == [20, 5]
    assert SettingsStore(tmp_store).load().autosave_every == 5
    assert win._autosave_every == 5


# ====================================================== 3. 续演（打开场景）
def _with_runtime(scene: Path) -> bytes:
    """给场景写一份 sidecar 存档，返回其字节（供「清没清掉」断言）。"""
    scenestore_mod.save_runtime(scene, scenestore_mod.RuntimeBundle(
        transcript=[{"id": 1, "speaker": "甲", "speaker_type": "character",
                     "content": "上次说到一半。"}],
        clock_seconds=3600, blocks=3))
    path = scenestore_mod.runtime_path_for(scene)
    return path.read_bytes()


def test_resume_yes_asks_then_restores_after_scene_opens(qapp, tmp_path, tmp_store,
                                                        monkeypatch):
    """有存档 + 用户答 Yes → 开场成功那一刻调 worker.restore_scene（不是问完就恢复）。"""
    win, worker, scene, _dirs = _make_window(tmp_path)
    _with_runtime(scene)
    asked: list = []
    monkeypatch.setattr(mw_mod, "ask_resume",
                        lambda *a, **k: asked.append(a) or True)

    win._maybe_resume(scene)
    assert asked, "有存档就该问一次"
    assert worker.restored == 0, "场景还没开，不能先恢复"

    worker.sig_scene_info.emit(_scene_info(["甲", "乙"], scene_path=scene))
    assert worker.restored == 1, "开场成功后应接着上次演"


def test_resume_no_clears_runtime_and_never_restores(qapp, tmp_path, tmp_store,
                                                     monkeypatch):
    """答 No → 清掉存档、从头演；开场后绝不恢复。"""
    win, worker, scene, _dirs = _make_window(tmp_path)
    _with_runtime(scene)
    assert scenestore_mod.has_runtime(scene)
    monkeypatch.setattr(mw_mod, "ask_resume", lambda *a, **k: False)

    win._maybe_resume(scene)
    assert not scenestore_mod.has_runtime(scene), "选「否」应丢弃存档"

    worker.sig_scene_info.emit(_scene_info(["甲", "乙"], scene_path=scene))
    assert worker.restored == 0


def test_no_runtime_never_asks_and_keeps_everything(qapp, tmp_path, tmp_store,
                                                    monkeypatch):
    """没有存档：既不问，也不清，也不恢复（打开场景的常见情形）。"""
    win, worker, scene, _dirs = _make_window(tmp_path)
    monkeypatch.setattr(mw_mod, "ask_resume",
                        lambda *a, **k: pytest.fail("没有存档不该弹询问框"))

    win._maybe_resume(scene)
    worker.sig_scene_info.emit(_scene_info(["甲", "乙"], scene_path=scene))
    assert worker.restored == 0


def test_stale_pending_resume_is_not_consumed_by_another_scene(qapp, tmp_path,
                                                               tmp_store, monkeypatch):
    """待恢复的那场与实际开出来的不是同一场 → 不恢复（开场失败/错场不误恢复）。"""
    win, worker, scene, _dirs = _make_window(tmp_path)
    _with_runtime(scene)
    monkeypatch.setattr(mw_mod, "ask_resume", lambda *a, **k: True)
    win._maybe_resume(scene)

    other = tmp_path / "scenes" / "别处.json"
    other.write_text(json.dumps({"name": "别处", "participants": ["甲"]},
                                ensure_ascii=False), encoding="utf-8")
    worker.sig_scene_info.emit(_scene_info(["甲"], scene_path=other))
    assert worker.restored == 0


def test_every_open_path_goes_through_the_single_resume_helper(qapp, tmp_path,
                                                               tmp_store, monkeypatch):
    """三条「打开」路径（打开场景菜单 / 管理场景弹窗 / 新建场景）都走同一个 _maybe_resume。"""
    win, _worker, scene, (cdir, sdir) = _make_window(tmp_path)
    seen: list = []
    monkeypatch.setattr(win, "_maybe_resume", lambda p: seen.append(Path(p)))

    win._open_scene_from_menu(scene)                 # 场景 →「打开场景」
    assert seen == [scene], "打开场景菜单应经 _maybe_resume"

    class _FakeLibrary:
        def __init__(self, *a, **k):
            self.chosen_scene = scene
            # 场景自己的名单（甲/乙）在库里配到的卡：切场校验按「场景声明的角色都得有卡」
            # 把关（§3.1），只给甲会被拦下并弹提示（离屏下那是模态框，会卡死整个用例）。
            self.chosen_characters = [cdir / "甲.json", cdir / "乙.json"]

        def exec(self):
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(mw_mod, "LibraryDialog", _FakeLibrary)
    win._on_open_library()                           # 场景 →「管理场景…」弹窗里打开
    assert seen == [scene, scene], "管理场景弹窗的打开路径应经 _maybe_resume"

    class _FakeEditor:
        def __init__(self, *a, **k):
            self.saved_path = scene

        def exec(self):
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(mw_mod, "SceneEditorDialog", _FakeEditor)
    win._on_new_scene()                              # 场景 →「新建场景」
    assert seen == [scene, scene, scene], "新建场景路径应经 _maybe_resume"


# ====================================================== 4. 重置场景运行上下文
def test_reset_menu_item_confirms_then_asks_worker(qapp, tmp_path, tmp_store,
                                                   monkeypatch):
    """「重置场景运行上下文…」：确认后 worker.reset_scene()，并给可见状态。"""
    win, worker, scene, _dirs = _make_window(tmp_path)
    assert not win._action_reset_scene.isEnabled(), "没场次时无可重置"
    worker.sig_scene_info.emit(_scene_info(["甲", "乙"], scene_path=scene))
    assert win._action_reset_scene.isEnabled()

    monkeypatch.setattr(mw_mod, "confirm", lambda *a, **k: False)
    win._action_reset_scene.trigger()
    assert worker.reset == 0, "取消确认不得动运行上下文"

    monkeypatch.setattr(mw_mod, "confirm", lambda *a, **k: True)
    win._action_reset_scene.trigger()
    assert worker.reset == 1
    assert win._status_chip.text() == "已重置场景运行上下文"


# ====================================================== 5. api 配置真正生效
def test_window_pushes_saved_api_config_on_startup(qapp, tmp_path, tmp_store):
    """启动：设置里的 url/key/模型名先推给 worker（开场前就记下期望值）。"""
    _win, worker, _scene, _dirs = _make_window(
        tmp_path, settings=AppSettings(api_base_url="https://my.api",
                                       api_key="sk-set", model_name="my-model"))
    assert worker.api_configs == [("https://my.api", "sk-set", "my-model")]


def test_api_dialog_save_pushes_new_config(qapp, tmp_path, tmp_store, monkeypatch):
    """「模型 api 配置」弹窗保存后立即推给 worker（下一次建引擎生效，不必重启）。"""
    win, worker, _scene, _dirs = _make_window(tmp_path)
    assert worker.api_configs == [("", "", "")], "启动先推一次（可能为空）"

    class _FakeDialog:
        def __init__(self, *a, **k):
            self.saved_settings = AppSettings(api_base_url="https://new.api",
                                              api_key="sk-new", model_name="m-new")

        def exec(self):
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(mw_mod, "ApiConfigDialog", _FakeDialog)
    win._on_api_config()
    assert win._settings.api_key == "sk-new"
    assert worker.api_configs[-1] == ("https://new.api", "sk-new", "m-new")


def test_saving_an_api_key_enables_the_real_model_checkbox(qapp, tmp_path, tmp_store,
                                                           monkeypatch):
    """「真实模型」开关不再一直灰着：设置里存过 key（含当场保存）即解锁并清掉原因提示。

    旧实现只在建窗那一刻按启动快照判定一次：用户在 设置→模型 api 配置 里存了 key，
    开关仍禁用、tooltip 仍写着「未检测到 DEEPSEEK_API_KEY」，只能重启软件。
    """
    win, worker, _scene, _dirs = _make_window(tmp_path)
    assert not win._live_check.isEnabled(), "没有 key 时开关应禁用"
    assert win._live_check.toolTip(), "禁用时要说明原因"

    class _FakeDialog:
        def __init__(self, *a, **k):
            self.saved_settings = AppSettings(api_base_url="https://new.api",
                                              api_key="sk-new", model_name="m")

        def exec(self):
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(mw_mod, "ApiConfigDialog", _FakeDialog)
    win._on_api_config()

    assert worker.api_configs[-1] == ("https://new.api", "sk-new", "m")
    assert win._live_check.isEnabled(), "保存 key 后应能打开真实模型"
    assert win._live_check.toolTip() == "", "禁用原因的 tooltip 应被清掉"


def test_startup_api_key_also_unlocks_the_real_model_checkbox(qapp, tmp_path,
                                                              tmp_store):
    """启动时设置里就有 key → 建窗即解锁（同一处判据，不靠另写一遍）。"""
    win, _worker, _scene, _dirs = _make_window(
        tmp_path, settings=AppSettings(api_key="sk-set"))
    assert win._live_check.isEnabled()
    assert win._live_check.toolTip() == ""


def test_secret_key_never_leaks_into_scene_payload_or_status(qapp, tmp_path,
                                                             tmp_store):
    """key 只在内存里流转：不进状态区，也不进任何界面文案。"""
    win, _worker, scene, _dirs = _make_window(
        tmp_path, settings=AppSettings(api_key="sk-secret"))
    _worker.sig_scene_info.emit(_scene_info(["甲", "乙"], scene_path=scene))
    assert "sk-secret" not in win._status_chip.text()
    assert "sk-secret" not in win.windowTitle()


def _deepseek_models_yaml(tmp_path: Path) -> Path:
    """三档全 deepseek 的 yaml（每档参数各不相同，用来验「覆盖模型名但保留参数」）。"""
    p = tmp_path / "models_deepseek.yaml"
    p.write_text(
        "think:\n  backend: deepseek\n  model: yaml-think\n"
        "  params: {thinking: enabled, max_tokens: 111}\n"
        "speak:\n  backend: deepseek\n  model: yaml-speak\n"
        "  params: {temperature: 0.3}\n"
        "narrate:\n  backend: deepseek\n  model: yaml-narrate\n"
        "  params: {max_tokens: 222}\n", encoding="utf-8")
    return p


def _deepseek_engine(tmp_path: Path, monkeypatch, **kwargs):
    """建一个 deepseek 档引擎并捕获 make_deepseek 的实参（不打桩网络）。"""
    calls: list[dict] = []

    def _fake_make_deepseek(api_key, model_config, base_url=None):
        calls.append({"api_key": api_key, "model_config": model_config,
                      "base_url": base_url})
        return worker_mod.SceneEngine  # 只当占位后端：本用例不联网、不跑图

    monkeypatch.setattr("harness.engine.make_deepseek", _fake_make_deepseek)
    scene = _write_scene(tmp_path / "s", "茶室", ["甲"])
    cards = [_write_card(tmp_path / "c", "甲")]
    engine = SceneEngine(scene, cards, _deepseek_models_yaml(tmp_path),
                         run_root=tmp_path / "runs", api_key="sk-t", **kwargs)
    return engine, calls


def test_engine_passes_api_base_url_and_model_override_to_backend(tmp_path,
                                                                  monkeypatch):
    """设置的 url/模型名真正进后端：三档都拿到自定义 base_url 与被覆盖的模型名。"""
    _engine, calls = _deepseek_engine(
        tmp_path, monkeypatch, api_base_url="https://my.api",
        model_override="my-model")

    assert len(calls) == 3, "think/speak/narrate 三档都应构造"
    assert all(c["base_url"] == "https://my.api" for c in calls)
    assert all(c["model_config"].model == "my-model" for c in calls)
    assert all(c["api_key"] == "sk-t" for c in calls)
    # 每档自己的 params 照旧从 yaml 来（只覆盖模型名，不动其它参数）
    assert {tuple(sorted(c["model_config"].params)) for c in calls} == \
        {("max_tokens", "thinking"), ("temperature",), ("max_tokens",)}


def test_engine_without_overrides_uses_yaml_exactly_as_before(tmp_path, monkeypatch):
    """空设置 ⇒ 今日行为：三档仍用 yaml 里的模型名，且不传自定义端点。"""
    _engine, calls = _deepseek_engine(tmp_path, monkeypatch)

    assert [c["model_config"].model for c in calls] == \
        ["yaml-think", "yaml-speak", "yaml-narrate"]
    assert all(c["base_url"] is None for c in calls), "没设 url 就不该改端点"


def test_engine_ignores_blank_overrides(tmp_path, monkeypatch):
    """空串/纯空白与 None 等价（界面上的「没填」不该变成空端点/空模型名）。"""
    _engine, calls = _deepseek_engine(tmp_path, monkeypatch, api_base_url="   ",
                                      model_override="  ")
    assert [c["model_config"].model for c in calls] == \
        ["yaml-think", "yaml-speak", "yaml-narrate"]
    assert all(c["base_url"] is None for c in calls)


def test_worker_build_engine_carries_api_config_and_autosave(tmp_path):
    """worker 记下的 api 配置/自动保存周期在建引擎时真的带上（重开、切场不丢）。"""
    seen: dict = {}

    def _fake_engine(*args, **kwargs):
        seen.update(kwargs)
        seen["model_args"] = args
        return object()

    worker = SceneWorker()
    worker.set_api_config(" https://cfg.api ", " sk-set ", " cfg-model ")
    worker.set_autosave_every(0)          # 0 = 关闭，照样带进引擎（keep 0 可用）
    worker._cfg = {
        "scene": tmp_path / "s.json", "characters": [tmp_path / "c.json"],
        "models": tmp_path / "models.yaml", "bid": None, "live": False,
        "api_key": "sk-cli", "run_root": tmp_path / "runs",
        "closing_at_block": None, "start_time": None, "cast_from_cards": False,
    }

    import harness.gui.worker as wm
    original = wm.SceneEngine
    wm.SceneEngine = _fake_engine
    try:
        asyncio.run(worker._build_engine())
    finally:
        wm.SceneEngine = original

    assert seen["api_base_url"] == "https://cfg.api"
    assert seen["model_override"] == "cfg-model"
    assert seen["api_key"] == "sk-set", "设置推来的 key 优先于 start_scene 那份"
    assert seen["autosave_every"] == 0


# ====================================================== app.py 的 api 优先级
def test_resolve_api_key_precedence_is_stub_then_settings_then_env():
    """优先级（§2.1①）：--stub > 设置里的 key > 环境变量 > 无。"""
    assert resolve_api_key(True, "sk-set", "sk-env") is None, "--stub 最优先"
    assert resolve_live(True, None) is False

    assert resolve_api_key(False, "sk-set", None) == "sk-set", "设置是主来源"
    assert resolve_live(False, "sk-set") is True, "有设置的 key 就该 live（无需环境变量）"

    assert resolve_api_key(False, "", "sk-env") == "sk-env", "没设置才回落环境变量"
    assert resolve_api_key(False, None, "sk-env") == "sk-env"
    assert resolve_api_key(False, "   ", "  sk-env  ") == "sk-env", "空白当没填"

    assert resolve_api_key(False, "", None) is None
    assert resolve_live(False, None) is False, "两处都没有 → stub"


def test_run_reads_key_from_settings_when_env_absent(qapp, tmp_path, monkeypatch):
    """app.run：环境变量缺席时，设置里的 key 也让 live=True（设置是主来源）。"""
    from harness.gui import app as gui_app

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    path = tmp_path / "settings" / "settings.json"
    monkeypatch.setattr(settings_mod, "default_settings_path", lambda: path)
    SettingsStore(path).save(AppSettings(api_key="sk-set", model_name="m"))
    captured = _run_app(gui_app, qapp, tmp_path, monkeypatch, [])
    assert captured["cfg"].live is True, "设置里有 key → live"
    assert captured["cfg"].api_key == "sk-set"

    # --stub 依旧最优先：设置里有 key 也不许顶掉它。
    captured = _run_app(gui_app, qapp, tmp_path, monkeypatch, ["--stub"])
    assert captured["cfg"].live is False
    assert captured["cfg"].api_key is None


def test_run_without_any_key_stays_stub(qapp, tmp_path, monkeypatch):
    """两处都没有 key → stub（内置素材照常可跑）。"""
    from harness.gui import app as gui_app

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    path = tmp_path / "settings" / "settings.json"
    monkeypatch.setattr(settings_mod, "default_settings_path", lambda: path)
    captured = _run_app(gui_app, qapp, tmp_path, monkeypatch, [])
    assert captured["cfg"].live is False


def test_run_without_autostart_opens_no_scene_and_no_startup_dialog(
        qapp, tmp_path, monkeypatch):
    """缺省启动（§3.3 取消启动小窗）：不弹开场设置、不投开场——窗口起来就是空状态页。

    `--scene/--characters` 仍进 AppConfig（角色/场景库目录、`开新场` 与 `--autostart`
    用它），只是不再是「启动即开演」的意思。
    """
    from harness.gui import app as gui_app

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    path = tmp_path / "settings" / "settings.json"
    monkeypatch.setattr(settings_mod, "default_settings_path", lambda: path)

    captured = _run_app(gui_app, qapp, tmp_path, monkeypatch, [], autostart=False)

    assert Path(captured["cfg"].scene).stem == "茶室", "素材仍按 CLI/缺省进 AppConfig"


# ============================================== 端到端（真 worker + 真引擎，stub 档）
def _wait_until(cond, timeout_ms: int = 8000, step_ms: int = 20) -> bool:
    """轮询主线程 Qt 事件循环直到 cond() 为真；超时返回 False（不抛，见 test_gui_scene）。"""
    from PySide6.QtCore import QEventLoop, QTimer
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


def _real_session(tmp_path: Path, sub: str):
    """真 SceneWorker + 真 MainWindow（stub 三档模型，硬边界远在未来）。"""
    cdir = tmp_path / "characters"
    sdir = tmp_path / "scenes"
    cards = [_write_card(cdir, n) for n in ("甲", "乙")]
    scene = _write_scene(sdir, "茶室", ["甲", "乙"])
    models = tmp_path / "models-real.yaml"
    models.write_text("think: {backend: stub, model: stub, params: {}}\n"
                      "speak: {backend: stub, model: stub, params: {}}\n"
                      "narrate: {backend: stub, model: stub, params: {}}\n",
                      encoding="utf-8")
    bid = tmp_path / "bid-real.yaml"
    bid.write_text("interruption_threshold: 0.0\nspeak_threshold: 0.0\n"
                   "silence_k: 8\n", encoding="utf-8")
    cfg = AppConfig(scene=scene, characters=cards, models=models, bid=bid,
                    run_root=tmp_path / sub, live=False, api_key=None,
                    closing_at_block=100000, opening="入夜。")
    worker = SceneWorker()
    win = MainWindow(worker, cfg)
    worker.start()
    return worker, win, scene


def test_end_to_end_save_then_resume_with_real_worker(qapp, tmp_path, monkeypatch):
    """真链路：开场 → 点「保存场景」落 sidecar → 重开一场答 Yes → 历史被接回对白区。

    前面的用例都用替身 worker 锁调用面；这一条锁**真实** worker/引擎那一侧（保存确实
    写盘、续演确实把历史追回界面）。
    """
    worker, win, scene = _real_session(tmp_path, "runs1")
    try:
        win.show()
        win.start_session()
        assert _wait_until(lambda: win._started), "开场超时"
        assert win._scene_path == scene

        win._save_scene_btn.click()
        assert _wait_until(lambda: win._saved_path is not None), "保存超时"
        assert scenestore_mod.has_runtime(scene), "存档应落在场景文件旁的 sidecar"
        assert win._status_chip.text() == f"已保存：{scene.name}"
    finally:
        win.close()
        worker.shutdown(8000)
        worker.wait(0)

    # 第二场：有存档 → 问一次（答 Yes）→ 开场成功后把历史接回来。
    worker2, win2, scene2 = _real_session(tmp_path, "runs2")
    asked: list = []
    monkeypatch.setattr(mw_mod, "ask_resume", lambda *a, **k: asked.append(a) or True)
    try:
        win2.show()
        win2._maybe_resume(scene2)
        assert asked, "有存档就该问"
        win2.start_session()
        assert _wait_until(lambda: win2._started), "第二次开场超时"
        assert _wait_until(lambda: len(win2._conv_msgs) > 1), "续演的历史没上屏"
    finally:
        win2.close()
        worker2.shutdown(8000)
        worker2.wait(0)


def _run_app(gui_app, qapp, tmp_path: Path, monkeypatch, extra: list[str],
             *, autostart: bool = True) -> dict:
    """跑一次 app.run（替身窗口/worker，无 Qt 事件循环），返回捕获到的 AppConfig。

    autostart=True（缺省）下顺带验「启动路径也走窗口的那个续演询问」：_maybe_resume 必须
    被调用一次、且发生在 start_session 之前（此时场景还没开）。
    autostart=False 验反面：**不弹任何开场设置窗、也不自动开场**——窗口直接起来，起手是
    空状态页（§3.3）。
    """
    cdir, sdir = tmp_path / "characters", tmp_path / "scenes"
    chars = [_write_card(cdir, "甲")]
    scene = _write_scene(sdir, "茶室", ["甲"])
    captured: dict = {"order": []}

    class _FakeWindow:
        def __init__(self, worker, cfg, settings=None):
            captured["cfg"] = cfg
            captured["order"].append(("build", None))

        def show(self):
            captured["order"].append(("show", None))

        def _maybe_resume(self, scene_path):
            captured["order"].append(("resume", Path(scene_path)))

        def start_session(self):
            captured["order"].append(("start", None))

    class _FakeWorker:
        def start(self):
            pass

        def shutdown(self, *a):
            pass

        def wait(self, *a):
            pass

        def isRunning(self):        # noqa: N802
            return False

    monkeypatch.setattr("harness.gui.main_window.MainWindow", _FakeWindow)
    monkeypatch.setattr("harness.gui.worker.SceneWorker", _FakeWorker)
    monkeypatch.setattr("PySide6.QtWidgets.QApplication", lambda *a, **k: qapp)
    monkeypatch.setattr(QApplication, "exec", lambda self: 0)

    argv = ["--scene", str(scene), "--characters", str(chars[0])]
    if autostart:
        argv.append("--autostart")
    argv += extra
    assert gui_app.run(argv) == 0
    if autostart:
        # 建窗 → show → 续演询问 → 开场（顺序固定：问在开场前）
        assert captured["order"][:2] == [("build", None), ("show", None)]
        assert captured["order"][2] == ("resume", scene), \
            "启动路径应在开场前走窗口的 _maybe_resume"
        assert captured["order"][3] == ("start", None)
    else:
        assert captured["order"] == [("build", None), ("show", None)], \
            "缺省不自动开场（也没有任何开场设置窗）：窗口直接起来，起手是空状态页"
    return captured
