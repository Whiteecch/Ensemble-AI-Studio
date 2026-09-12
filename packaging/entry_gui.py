"""PyInstaller 冻结态的 GUI 入口（打包方案 §2.1）。

为什么不直接拿 `harness/gui/app.py` 当入口：PyInstaller 的入口脚本必须是一个**独立文件**，
它会被当作 `__main__` 执行——直接吃包内模块会让 `from .main_window import ...` 这类相对
import 全部落空（`__package__` 变成空串）。本文件就是那层壳：补齐 `sys.path`、做 Windows
上的常规防御，然后把控制权交给 `harness.gui.app.main()`。

三件事：

1. **`sys.path`**：冻结态下源码布局（`app/src/harness`）不存在，`harness` 由 PyInstaller
   放进产物根并已在 `sys.path` 上；这里仍显式补一遍——冻结布局不止一种（包可能被塞进
   `_internal/`、`data/` 或 `src/`），而"import 找不到 harness"是打包最典型的开局失败。
   开发态（不冻结）也能直接跑：把仓库的 `app/src` 插进去。
2. **Windows 常规防御**：`multiprocessing.freeze_support()`（防子进程把整个 app 再启动
   一遍——`--windowed` 下这是无限弹窗）；`sys.stdout/stderr` 为 None 时接到 `/dev/null`
   （无控制台时它们就是 None，任何 `print`——argparse 的报错、库的调试输出——都会
   抛 AttributeError，把"参数写错"变成"程序打不开"）。
3. **打包自检**：`ENSEMBLE_SELFTEST=<输出文件>` 时**不开窗口**，只把路径解析结果、素材
   清单、Qt 可用性写进该文件并退出 0。装完包后既无控制台又常无显示器，这是唯一能验收
   "素材到底被解到哪儿、Qt 剪枝有没有剪坏"的手段（`build.ps1 -Verify` 走的就是这条路）。
"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path


def _bootstrap_sys_path() -> None:
    """让 `import harness` 在这台机器上一定成功（冻结态与开发态各一条路）。

    冻结态：候选根就是 `sys._MEIPASS`（PyInstaller 的产物根；onedir 下是 exe 同级的
    `_internal/`）与 exe 同级；再兜底看几个常见子目录（`src/`、`_internal/`），因为
    "把包塞进子目录"的冻结布局确实存在。只补**存在且还没有**的目录，不做任何清理。
    开发态：`<repo>/app/src`（本文件在 `<repo>/packaging/` 下）。
    """
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        roots: list[Path] = []
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            roots.append(Path(meipass))
        roots.append(Path(sys.executable).resolve().parent)
        for root in roots:
            candidates += [root, root / "src", root / "_internal", root / "data"]
    else:
        candidates.append(Path(__file__).resolve().parents[1] / "app" / "src")
    for path in candidates:
        try:
            if path.is_dir() and str(path) not in sys.path:
                sys.path.insert(0, str(path))
        except OSError:                                   # pragma: no cover - 极端路径
            continue


def _windows_defences() -> None:
    """Windows 专属的两条常规防御（见模块 docstring；两条都只在 win32 上做）。"""
    if sys.platform != "win32":
        return
    try:
        import multiprocessing
        multiprocessing.freeze_support()
    except Exception:                                     # pragma: no cover - 可选依赖
        pass
    # `--windowed` 产物没有控制台：stdout/stderr 是 None。任何一次 print 都会炸，
    # 而 argparse/第三方库的调试输出并不受我们控制，故这里直接兜住。
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except OSError:                               # pragma: no cover
                pass


def _selftest(out_path: str) -> int:
    """无界面自检：把"冻结态到底解析到哪儿"写成一份可核对的文件。返回退出码。

    刻意**不 import Qt 之外的东西时不带 try**（Qt 那一段带 try，因为 offscreen 插件的
    有无本身就是结论之一）：路径解析这一段若抛异常，报告里就要看到那条异常——这正是
    打包失败的第一现场。
    """
    lines: list[str] = []

    def emit(text: str = "") -> None:
        lines.append(text)

    emit("=== Ensemble packaging selftest ===")
    emit(f"frozen              : {getattr(sys, 'frozen', False)}")
    emit(f"executable          : {sys.executable}")
    emit(f"_MEIPASS            : {getattr(sys, '_MEIPASS', '(none)')}")
    emit(f"cwd                 : {os.getcwd()}")
    emit(f"QT_QPA_PLATFORM     : {os.environ.get('QT_QPA_PLATFORM', '(unset)')}")

    try:
        from harness import paths
    except Exception:
        emit("!! `import harness.paths` 失败：")
        emit(traceback.format_exc())
        _write_report(out_path, lines)
        return 1

    emit("")
    emit(f"is_frozen()         : {paths.is_frozen()}")
    emit(f"resource_dir()      : {paths.resource_dir()}")
    emit(f"  exists            : {paths.resource_dir().is_dir()}")
    emit(f"materials_dir()     : {paths.materials_dir()}")
    emit(f"templates_dir()     : {paths.templates_dir()}")
    emit(f"user_dir()          : {paths.user_dir()}")
    emit("")

    # 素材清单：打包真正的验收点——`resource_dir()` 指的地方到底有没有东西。
    bad = 0
    for name in ("characters", "scenes", "config", "templates"):
        sub = paths.resource_dir() / name
        files = sorted(p.name for p in sub.glob("*")) if sub.is_dir() else []
        emit(f"[{name}] dir={sub.is_dir()} n={len(files)} {files}")
        if not sub.is_dir():
            bad += 1
    emit("")

    # 播种：启动流程里真的会做的那一步（首次运行体验 §三）。
    try:
        paths.ensure_user_dirs()
        seeded = sorted(p.name for p in (paths.user_dir() / "characters").glob("*"))
        emit(f"seeded characters   : {seeded}")
        emit(f"seeded scenes       : "
             f"{sorted(p.name for p in (paths.user_dir() / 'scenes').glob('*'))}")
    except Exception:
        emit("!! ensure_user_dirs() 失败：")
        emit(traceback.format_exc())
        bad += 1
    emit("")

    # Qt：剪枝有没有剪坏，只有真建一个 QApplication 才算数。
    try:
        from PySide6 import QtCore
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        emit(f"QtCore.__version__  : {QtCore.__version__}")
        emit(f"QApplication        : {type(app).__name__} ok")
        emit(f"platformName        : {app.platformName()}")
    except Exception:
        emit("!! Qt 起不来（多半是被剪枝剪掉了必需模块）：")
        emit(traceback.format_exc())
        bad += 1
    emit("")

    # 应用自己的窗口模块：**import 一遍**即证明它要的 Qt 类都在（不真开窗）。
    try:
        from harness.gui import main_window, worker  # noqa: F401
        emit("harness.gui.main_window / worker : import ok")
    except Exception:
        emit("!! 导入主窗口/worker 失败：")
        emit(traceback.format_exc())
        bad += 1

    emit("")
    emit(f"RESULT: {'FAIL' if bad else 'OK'} (problems={bad})")
    _write_report(out_path, lines)
    return 1 if bad else 0


def _write_report(out_path: str, lines: list[str]) -> None:
    """把自检报告写到文件（无控制台，只能落盘）并同时尽力打印一份。"""
    text = "\n".join(lines) + "\n"
    try:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(text, encoding="utf-8")
    except OSError:
        pass
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except Exception:                                     # pragma: no cover - stdout=None
        pass


def main() -> None:
    """入口：自检分支 → Qt 主流程。异常兜底写一份崩溃日志再原样抛出。"""
    _bootstrap_sys_path()
    _windows_defences()

    selftest_out = os.environ.get("ENSEMBLE_SELFTEST")
    if selftest_out:
        # 自检报告由 `_selftest()` 自己落盘（含 `RESULT: OK/FAIL` 那一行）；这里**只**定
        # 退出码——再写一次报告会把前面那份覆盖掉，只剩一句 exit code。
        code = _selftest(selftest_out)
        try:
            sys.stdout.write(f"(exit {code})\n")
            sys.stdout.flush()
        except Exception:                                 # pragma: no cover - stdout=None
            pass
        # `os._exit` 跳过 Qt 的收尾：offscreen 下析构阶段会冒出与本结论无关的杂音，
        # 而进程树里还挂着 QApplication 时"正常退出"本身也不可靠。
        os._exit(code)

    try:
        from harness.gui.app import main as gui_main
        gui_main()
    except Exception:
        _write_crash_log(traceback.format_exc())
        raise


def _write_crash_log(tb: str) -> None:
    """窗口版没有控制台，崩溃现场只能落盘（`%APPDATA%\\Ensemble-AI-Studio\\last-crash.log`）。

    定位用户数据目录**不用** `paths`（崩溃可能就发生在导入 paths 时）；这里自己按
    `%APPDATA%` 拼一次是最保守的写法，失败就算了——崩溃日志本身不值得再崩一次。
    """
    try:
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        target = Path(base) / "Ensemble-AI-Studio" / "last-crash.log"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(tb, encoding="utf-8")
    except Exception:
        pass


if __name__ == "__main__":
    main()
