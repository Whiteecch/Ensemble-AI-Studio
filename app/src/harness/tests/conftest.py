import contextlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # src/

@pytest.fixture
def run_root(tmp_path: Path) -> Path:
    """每次测试独立的 runs 根，避免污染真实目录。"""
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    return root


@pytest.fixture(autouse=True)
def _qt_top_level_widgets_dont_leak_between_tests():
    """（GUI 用例）用例收尾后销毁它留下的顶层部件——整场测试共用一个 QApplication。

    为什么必须：Qt 只允许每进程一个 QApplication，于是整场测试（1918+ 条）共用同一个
    实例；而**建出来的 MainWindow 再也回收不掉**——窗口自己的菜单/控制件把 lambda（闭包
    抓着 self）接到了信号上，PySide 对这种"非 QObject 槽"持强引用，引用链要过一层 C++
    对象，Python 的环回收看不见这条环（实测 `del win; gc.collect()` 后窗口与整棵部件树
    仍在 `QApplication.allWidgets()` 里）。一场跑下来几百个窗口、几万个部件堆在同一个
    进程里——这些窗口对后一条用例毫无意义，只是残留。

    症状（本夹具针对的就是它）：单进程跑全套时，`test_gui_smoke.py` 里的用例会**偶发**
    以 `Windows fatal exception: access violation` 掀翻整个 pytest 进程（139 退出，
    后面的用例根本没跑）。崩溃点落在 `MainWindow.__init__`/`_build_left` 构造部件那一带，
    栈是"执行一个堆地址"（调用了一个已失效的函数指针），约 1/3~2/3 概率。把上一批窗口
    在用例之间收掉之后，同一套命令连跑多次不再出现该崩溃。

    只对"已经建过 QApplication"的用例生效（纯 CLI 用例根本不 import Qt，这里直接返回；
    PySide6 是可选依赖，缺它也一样直接返回）。销毁前先 `close()`：窗口的 closeEvent 会
    顺手停掉它名下的 worker 线程，免得留下"窗口没了、线程还在跑"的另一种残留。
    """
    yield
    try:                                  # PySide6 是可选的 gui 依赖：没装就无事可做
        from PySide6.QtCore import QCoreApplication, QEvent
    except ImportError:                   # pragma: no cover - 取决于环境是否装了 gui 依赖
        return
    # 取法**绕开 `PySide6.QtWidgets.QApplication` 这个名字**：有用例把它 monkeypatch
    # 成假类（test_gui_wiring 的 `_run_app` 就是这么跑 app.run 的），本夹具只想知道
    # "这一场有没有 Qt 应用"，不该被那种替身影响。
    app = QCoreApplication.instance()
    if app is None or not hasattr(app, "topLevelWidgets"):
        return                            # 本场还没建过 QApplication（纯 CLI 用例）
    # 关最后一个可见窗口会触发 QApplication::quitOnLastWindowClosed → quit()，
    # 那是"关掉别人正在等的循环"的另一条路：这几次 close() 只是在清理，绝不该
    # 影响任何还在跑的事件循环。故临时关掉，清理完原样还回去。
    quit_on_close = (app.quitOnLastWindowClosed()
                     if hasattr(app, "quitOnLastWindowClosed") else None)
    if quit_on_close is not None:
        app.setQuitOnLastWindowClosed(False)
    try:
        for widget in list(app.topLevelWidgets()):
            with contextlib.suppress(Exception):
                widget.close()            # 先走 closeEvent（顺手停 worker），再排队销毁
            widget.deleteLater()
        # deleteLater 只排队：这里当场把 DeferredDelete 事件处理掉，部件树立刻真的销毁。
        # **只处理这一类事件，不调 processEvents()**——后者会去跑别的待处理事件（连带
        # 其它用例留在队列里的定时器/信号），实测会把下一条用例的嵌套事件循环提前敲停
        # （`_wait_until` 直接返回 False，32 条 GUI 用例连锁红）。
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    finally:
        if quit_on_close is not None:
            app.setQuitOnLastWindowClosed(quit_on_close)


@pytest.fixture(autouse=True)
def _isolate_user_settings(tmp_path_factory, monkeypatch):
    """**整场测试**都把设置读写重定向到临时目录。

    为什么必须：MainWindow 在不显式传 settings 时会读取 `default_settings_path()`——
    那是**用户真实**的 %APPDATA%/文字创作agent/settings.json。于是（a）测试结果会随用户
    本机配置而变（曾出现"测试期望默认主题、实际读到用户改过的深色主题"而红），
    （b）更糟的是 `apply_theme/apply_language` 会**把测试写进用户的真实配置**（确实发生过）。
    这里把 default_settings_path 指向一次性临时路径，测试与用户数据从此互不干扰。
    """
    from harness.gui import settings as settings_mod

    sandbox = tmp_path_factory.mktemp("user-settings") / "settings.json"
    monkeypatch.setattr(settings_mod, "default_settings_path", lambda: sandbox)
    return sandbox


@pytest.fixture(autouse=True)
def _isolate_user_data_dir(tmp_path_factory, monkeypatch):
    """**整场测试**把 `paths.user_dir()`（用户数据目录的单一闸门）指到一次性 tmp。

    为什么必须（打包改造 §1.1/§1.3 的新增风险）：`paths.ensure_user_dirs()` 会**往用户目录
    复制文件**（首次运行播种），而 `gui/app.py::run()`、`tools/import_cards.py`、
    `runner._default_run_root`、`SceneWorker`/`SceneEngine` 的 run_root 缺省都改走用户目录。
    不隔离的话，跑一次测试就会在**真实** `%APPDATA%\\Ensemble-AI-Studio\\` 里播一批素材、
    写一批存档（更别说读：本机一旦真有数据，用例结果就随这台机器变）。这跟上面
    `default_settings_path` 夹具是同一条纪律——用户目录**只经一个函数**，指到 tmp 就把整条
    写入路径关进了沙箱。

    另外把 `gui/app.py` 的 `_DEFAULT_RUN_ROOT` 也指过去：那是**import 时**算好的常量（保留
    惰性会破坏既有用例对 `_DEFAULT_*` 的 monkeypatch 点，见 `test_gui_library`），故这里显式
    对齐。用例自己再 monkeypatch 这个名字仍然生效（函数级 monkeypatch 在本夹具之后应用）。

    还有 `paths.legacy_user_dirs()`（改名前的旧目录，设置读取的兼容回落）：它按 `_platform_base()`
    直接拼路径、**不经过 `user_dir()`**，故上面那个 patch 拦不住它——真实机器上就存在
    `%APPDATA%\\文字创作agent\\settings.json`（开发机就有），不置空的话用例会读到用户真实设置
    且结果随机器变。这里指成空表：测试里"老目录"根本不存在。要验兼容回落行为的用例自己
    monkeypatch 它（见 `test_gui_settings`）。
    """
    from harness import paths as paths_mod

    root = tmp_path_factory.mktemp("user-data") / paths_mod.APP_DIR_NAME
    monkeypatch.setattr(paths_mod, "user_dir", lambda: root)
    monkeypatch.setattr(paths_mod, "legacy_user_dirs", lambda: [])
    try:
        from harness.gui import app as gui_app
    except ImportError:                   # pragma: no cover - 缺依赖时只隔离能隔离的那侧
        return root
    monkeypatch.setattr(gui_app, "_DEFAULT_RUN_ROOT", paths_mod.runs_dir())
    return root


@pytest.fixture
def user_data_sandbox(_isolate_user_data_dir) -> Path:
    """上面那个沙箱用户目录的**公开别名**（用例想往里断言/放东西时按需请求）。

    自动隔离是 autouse 的，用例通常不必关心；但"某条路径必须落在用户目录里"这类断言需要
    知道沙箱到底是哪儿——请求本夹具即可，不必自己再 monkeypatch 一遍（两处各指一个目录
    会让断言跟实现错位）。
    """
    return _isolate_user_data_dir


@pytest.fixture(autouse=True)
def _isolate_libraries_root(tmp_path_factory, monkeypatch):
    """**整场测试**都把「缺省信息库根」重定向到一次性临时目录（同上面那条的纪律）。

    为什么必须：§13.3 把信息库根的缺省落点定在**仓库内的 `app/libraries/`**，而 GUI
    （`SceneWorker.libraries_root`）与 CLI（`runner._DEFAULT_LIBRARIES_ROOT`）都把它当缺省
    ——于是任何一个用**真实素材卡**（仓库 app/characters 下那些）起场的用例，都会在
    `app/libraries/` 里给角色**播种建库**（§9.1 第二步），把仓库当作用户数据目录写脏；
    更隐蔽的是"读"：本机一旦真有一座库，用例的提示词/输出就随它变，绿红全看这台机器。

    只改**缺省落点**，不改显式传参：用例想测信息库就自己传/自己 patch 到 tmp_path。
    GUI 侧改的是类属性（`SceneWorker.libraries_root`，模块常量与 `_default_libraries_root()`
    原样不动，故"缺省到底是哪儿"仍可被用例直接钉住）。
    """
    sandbox = tmp_path_factory.mktemp("libraries") / "libraries"
    try:
        from harness import runner as runner_mod
    except ImportError:                   # pragma: no cover - 缺依赖时只隔离能隔离的那侧
        runner_mod = None
    if runner_mod is not None:
        monkeypatch.setattr(runner_mod, "_DEFAULT_LIBRARIES_ROOT", sandbox)
    try:                                  # GUI 是可选依赖：没装 PySide6 就只隔离 CLI 那侧
        from harness.gui import worker as worker_mod
    except ImportError:                   # pragma: no cover - 取决于环境是否装了 gui 依赖
        return sandbox
    monkeypatch.setattr(worker_mod.SceneWorker, "libraries_root", sandbox)
    return sandbox
