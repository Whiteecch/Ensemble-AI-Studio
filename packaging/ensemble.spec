# -*- mode: python ; coding: utf-8 -*-
"""Ensemble 的 PyInstaller 构建脚本（打包方案 §2.1）：**一份 spec 出两种产物**。

## 为什么不是"两份 spec"（以及为什么命令行旗标没用）

`pyinstaller packaging/ensemble.spec --onedir` 里的 `--onedir/--onefile` 是**给
`pyinstaller foo.py` 那套用的**；一旦把 `.spec` 交给它，PyInstaller 就只认 spec 里的
`EXE()/COLLECT()`，命令行那些旗标**静默失效**——"我明明写了 --onefile，出来的却是文件夹"
就是这么来的。两条干净的做法：拆两份 spec（共享片段）或**让 spec 读环境变量**。这里选后者
——两种产物的差异只有"收不收集目录"一处，拆两个文件会让"裁剪/数据/版本"三处逻辑各写两遍，
迟早分叉。变量：

  · `ENSEMBLE_FORM`        = `onedir`（默认，供安装包）| `onefile`（单 exe，免安装分发）
  · `ENSEMBLE_EXCLUDE_QT`  = `1`（默认，裁剪未用 Qt）| `0`（不裁剪，供体积对照）
  · `ENSEMBLE_SRC`         = 源码根，默认 `<repo>/app/src`
  · `ENSEMBLE_MATERIALS`   = 素材根，默认 `<repo>/app`
  · `ENSEMBLE_TEMPLATES`   = 模板根，默认 `<repo>/templates`

输出目录走命令行（`--distpath/--workpath/--clean --noconfirm`），不在这里改 `DISTPATH`。

## 数据与素材（§1.1 的两侧分家）

打进去的只有**内置只读素材**：`characters/ scenes/ config/`（源 `<repo>/app/`）与
`templates/`（源在**仓库根**，不在 `app/` 下）。两者都落在产物的**素材根**里——
`harness.paths.resource_dir()` 在冻结态指向的正是那一处（onefile = 解压目录，onedir =
exe 同级的 `_internal/`），所以 `templates_dir()`（= `resource_dir()/"templates"`）不需要
任何改动就能找到模板：只要把模板的 add-data **目标名写成 `templates`** 即可。

用户数据（角色卡/场景/信息库/存档/设置）**一律不进产物**：它们在 `%APPDATA%`，由
`paths.ensure_user_dirs()` 在首次运行时播种（打包方案 §三）。本 spec 只从
`ENSEMBLE_MATERIALS` / `ENSEMBLE_TEMPLATES` 取素材，故"构建源"是什么，产物里就是什么——
所以才要有下面那道 `_check_materials()`：源是干净的就一定干净，源不干净（开发机的私有
`app/`、边车、本机绝对路径）**直接拒绝构建**，而不是"但愿没人这么干"。

## 体积（§2.1）

PySide6 全量 641MB，其中 `Qt6WebEngineCore.dll` 一个就 203MB。应用只用
`QtCore/QtGui/QtWidgets`，故按 `_EXCLUDE_MODULES` + `_is_dropped()` 两层裁剪：前者砍
Python 模块（让 PyInstaller 不去收集），后者在 `Analysis` 之后**过滤已经收进来的
binaries/datas**（PySide6 的 hook 会把整套 Qt DLL 与插件都收进来，只靠 excludes 拦不住）。
裁剪可关（`ENSEMBLE_EXCLUDE_QT=0`），体积对照与回归排查都靠它。
"""
import os
import re
import sys
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

# ----------------------------------------------------------------- 路径与版本
SPEC_DIR = Path(SPECPATH).resolve()          # <repo>/packaging
REPO = SPEC_DIR.parent                       # <repo>


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(raw).resolve() if raw else default


SRC = _env_path("ENSEMBLE_SRC", REPO / "app" / "src")
MATERIALS = _env_path("ENSEMBLE_MATERIALS", REPO / "app")
TEMPLATES = _env_path("ENSEMBLE_TEMPLATES", REPO / "templates")

# ---- 源码树防呆：必须**真的**从 ENSEMBLE_SRC 收 `harness` ----------------------------
# 典型的坑：构建用的 venv 里装着**另一个**仓库的 `harness`（`pip install -e` 留下的
# `__editable__.*.pth`），它在 sys.path 上排在 `pathex` 之前，于是"在 A 仓构建，打进去的
# 却是 B 仓的代码"——产物看起来正常，内容全是别人的。`Analysis` 用 `sys.path.extend(pathex)`
# （追加），**压不住** `.pth` 的条目，故这里显式把 `ENSEMBLE_SRC` 插到最前，并同步写进
# `PYTHONPATH`（`collect_submodules` 等钩子工具跑在隔离子进程里，只认环境变量）。最后
# 断言一次解析结果，宁可构建失败也不出"装错源"的包。
sys.path.insert(0, str(SRC))
os.environ["PYTHONPATH"] = str(SRC) + os.pathsep + os.environ.get("PYTHONPATH", "")
import importlib.util  # noqa: E402

importlib.invalidate_caches()
_spec = importlib.util.find_spec("harness")
_origin = Path(_spec.origin).resolve() if (_spec and _spec.origin) else None
if _origin is None or SRC not in _origin.parents:
    raise SystemExit(
        f"分析到的 harness 不是 ENSEMBLE_SRC 下的那份：\nsource = {SRC}\n"
        f"resolved = {_origin}\n请清掉构建环境里别的 editable 安装（或换一个干净的 venv）。")
print(f"[ensemble.spec] 源码：{_origin}")

FORM = (os.environ.get("ENSEMBLE_FORM", "onedir").strip().lower() or "onedir")
if FORM not in ("onedir", "onefile"):
    raise SystemExit(f"ENSEMBLE_FORM 只能是 onedir/onefile，收到：{FORM!r}")
SLIM = os.environ.get("ENSEMBLE_EXCLUDE_QT", "1").strip() != "0"

#: 版本号**单一来源**：`app/pyproject.toml` 的 `project.version`。spec 里不再写第二份常量。
_PYPROJECT = REPO / "app" / "pyproject.toml"
if not _PYPROJECT.is_file():
    raise SystemExit(f"找不到版本号来源：{_PYPROJECT}")
VERSION = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]

#: 产物名统一 `Ensemble-AI-Studio-<version>-<form>`（§2.1）：onedir 是**目录名**，
#: onefile 是 exe 名；安装包/压缩包的名字由 build.ps1 从这里派生。
PRODUCT = f"Ensemble-AI-Studio-{VERSION}-{FORM}"
APP_NAME = "Ensemble-AI-Studio"

# ----------------------------------------------------------------- 数据
#: 要打进去的素材：源目录 → 产物内目录名（两者同名 → 落在素材根下）。模板在**仓库根**
#: （不在 app/ 下），但目标名仍必须是 `templates`——冻结态 `paths.templates_dir()` =
#: `resource_dir()/templates`，只有这样才对得上。源目录缺失就跳过（降级，不报错）。
_MATERIAL_ROOTS = [(MATERIALS / name, name) for name in ("characters", "scenes", "config")]
_MATERIAL_ROOTS.append((TEMPLATES, "templates"))

# ---- 素材防呆：产物里**绝不能**有用户数据 ------------------------------------------
# datas 是**整棵目录照搬**，所以"用什么源构建"就等于"产物里是什么"。把默认的
# `<repo>/app` 换成开发机的私有 `app/`（用户自己的卡、`*.runtime.json` 边车、
# `libraries/ runs/`、`*.sqlite`）时，spec 一行都不会报错——用户数据就这么静默地进了
# 公开发行物，而泄露一次是收不回来的。故**收数据之前**先扫一遍源目录，命中就拒绝构建：
#   (a) 运行期/用户数据标记（边车、数据库、信息库与存档目录）；
#   (b) 本机绝对路径（`C:\...` / `d:/...`）出现在将要随包发出的文本素材里——那是作者
#       机器上的路径，会跟着产物发给每一个用户（`templates/` 里的说明文档尤其容易带进来）。
# 两条都可用 `ENSEMBLE_ALLOW_DIRTY_MATERIALS=1` 显式放行（会打印醒目警告），免得一份
# 合法素材永远卡住构建；默认拒绝 = 默认安全。
_FORBIDDEN_SUFFIXES = (".runtime.json", ".sqlite", ".sqlite3", ".db", ".db-wal", ".db-shm")
_FORBIDDEN_DIR_NAMES = {"libraries", "runs", "__pycache__", ".git"}
_TEXT_SUFFIXES = (".md", ".txt", ".json", ".yaml", ".yml")
_LOCAL_PATH_RE = re.compile(r"[A-Za-z]:[\\/]")


def _check_materials() -> None:
    problems: list[str] = []
    for root, label in _MATERIAL_ROOTS:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            rel = f"{label}/{path.relative_to(root).as_posix()}"
            if path.is_dir():
                if path.name.lower() in _FORBIDDEN_DIR_NAMES:
                    problems.append(f"  {rel}/  （用户数据/运行期目录）")
                continue
            low = path.name.lower()
            if low.endswith(_FORBIDDEN_SUFFIXES):
                problems.append(f"  {rel}  （运行期边车或数据库）")
            elif low.endswith(_TEXT_SUFFIXES):
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:                       # pragma: no cover - 权限/占用
                    continue
                if _LOCAL_PATH_RE.search(text):
                    problems.append(f"  {rel}  （正文里带本机绝对路径）")
    if not problems:
        return
    detail = "\n".join(problems)
    if os.environ.get("ENSEMBLE_ALLOW_DIRTY_MATERIALS", "").strip():
        print(f"[ensemble.spec] !! 警告：素材命中下列检查，因 ENSEMBLE_ALLOW_DIRTY_MATERIALS=1 "
              f"仍继续构建（产物里会有这些内容）：\n{detail}")
        return
    raise SystemExit(
        "拒绝构建：素材目录里有**用户数据**或**本机绝对路径**——打进去就是公开发行物里的泄露，"
        f"或把作者机器的路径发给每个用户：\n{detail}\n"
        "请把 ENSEMBLE_MATERIALS / ENSEMBLE_TEMPLATES 指向一份干净的公开素材"
        "（默认 = 本仓的 app/ 与 templates/）；确需放行再加 ENSEMBLE_ALLOW_DIRTY_MATERIALS=1。")

_check_materials()

datas = [(str(root), label) for root, label in _MATERIAL_ROOTS if root.is_dir()]
print(f"[ensemble.spec] 素材：{[(d[1], d[0]) for d in datas]}")

# ----------------------------------------------------------------- 裁剪（§2.1）
#: 未用的 Qt Python 模块：先让 PyInstaller 别去收集它们。
_EXCLUDE_MODULES = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
    "PySide6.QtQuickControls2",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput", "PySide6.Qt3DLogic",
    "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtSpatialAudio",
    "PySide6.QtNetworkAuth", "PySide6.QtDesigner", "PySide6.QtUiTools",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtHelp",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning", "PySide6.QtLocation",
    "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtRemoteObjects",
    "PySide6.QtScxml", "PySide6.QtStateMachine", "PySide6.QtTextToSpeech",
    "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtSvg", "PySide6.QtSvgWidgets",
    # 与 GUI 无关的 Python 世界（装机环境里可能顺手装了，打包不该顺手带上）
    "tkinter", "unittest", "pydoc_data", "setuptools", "pip", "wheel", "pytest",
    "_pytest", "IPython", "numpy", "PIL", "matplotlib",
]

#: Qt 的**库名主干**（`Qt6Xxx.dll` 与 `QtXxx.pyd` 两条都要命中）。Python 模块被 excludes
#: 拦下后，hook 仍可能按"依赖扫描"把对应的 Qt DLL 收进来——所以还得在 Analysis 之后过滤。
_DROP_QT_STEMS = (
    "WebEngineCore", "WebEngineWidgets", "WebEngineQuick", "WebChannel", "WebSockets",
    "Qml", "QmlModels", "QmlCompiler", "QmlWorkerScript", "QmlLocalStorage",
    "Quick", "QuickWidgets", "QuickTemplates2", "QuickControls2", "QuickDialogs2",
    "QuickLayouts", "QuickShapes", "QuickTest", "Quick3D",
    "3DCore", "3DRender", "3DInput", "3DLogic", "3DAnimation", "3DExtras", "3DQuick",
    "Charts", "DataVisualization", "Graphs",
    "Multimedia", "MultimediaWidgets", "SpatialAudio",
    "NetworkAuth", "Designer", "DesignerComponents", "UiTools",
    "Pdf", "PdfWidgets", "Sql", "Test", "Help", "Bluetooth", "Nfc",
    "Positioning", "Location", "Sensors", "SerialPort", "RemoteObjects",
    "Scxml", "StateMachine", "TextToSpeech", "LabsStyleKit",
    "ShaderTools", "OpenGL", "OpenGLWidgets", "Svg", "SvgWidgets",
)

#: 只留这几个插件目录：`platforms`（qwindows/qoffscreen，**必需**）、`styles`
#: （Windows 原生控件样式）、`imageformats`+`iconengines`（PNG/ICO 图标）。其余
#: （sqldrivers/assetimporters/qmltooling/multimedia/webview/designer/…）都是上面那些
#: 被裁模块的附属，跟着一起走。
_KEEP_PLUGIN_DIRS = {"platforms", "styles", "imageformats", "iconengines"}

#: 多媒体/软件渲染的大块头（跟着 Multimedia 来的编解码器 + Mesa 软件 OpenGL）。
#: `opengl32sw.dll` 只在"没有显卡驱动"时被 Qt 选用；本应用是纯 Widgets 光栅绘制，
#: 不走 OpenGL，故一并裁掉（要留就把 `ENSEMBLE_EXCLUDE_QT=0` 关掉裁剪）。
_DROP_FILE_PREFIXES = ("avcodec-", "avformat-", "avutil-", "swscale-", "swresample-",
                       "opengl32sw.")


def _is_dropped(bundle_name: str) -> bool:
    """要不要把这条 binary/data 从产物里剔掉（`bundle_name` 是产物内相对路径，'/' 分隔）。"""
    norm = bundle_name.replace("\\", "/")
    leaf = norm.rsplit("/", 1)[-1]
    if norm.startswith("PySide6/"):
        rest = norm[len("PySide6/"):]
        if rest.startswith("plugins/"):
            return rest.split("/")[1] not in _KEEP_PLUGIN_DIRS
        if rest.startswith(("qml/", "translations/", "typesystems/", "support/")):
            return True
        for stem in _DROP_QT_STEMS:
            # `Qt6Qml.dll` / `QtQml.pyd`：库名主干前可有 `6`，也可没有。
            if rest.startswith((f"Qt6{stem}", f"Qt{stem}", f"{stem}.")):
                return True
    return leaf.startswith(_DROP_FILE_PREFIXES)


# ----------------------------------------------------------------- 分析
#: `harness` 全量收进来（除了 tests）：GUI 里大量**延迟 import**（Qt 与部分引擎模块都是
#: 函数内 import），静态分析看不到，漏一个就是"打包后点某个按钮才崩"。包本身很小，
#: 全收的代价远低于漏收的代价。
_hidden = [m for m in collect_submodules("harness") if ".tests" not in m]
_hidden += ["PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets",
            "aiosqlite", "sqlite3", "langgraph.checkpoint.sqlite", "jsonpointer", "jsonpatch"]

a = Analysis(
    [str(SPEC_DIR / "entry_gui.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=_EXCLUDE_MODULES if SLIM else [],
    noarchive=False,
    optimize=0,
)

if SLIM:
    _before = (len(a.binaries), len(a.datas))
    # PyInstaller 6 起 TOC 已弃用，普通三元组列表即可（下游统一走 normalize_toc）。
    a.binaries = [b for b in a.binaries if not _is_dropped(b[0])]
    a.datas = [d for d in a.datas if not _is_dropped(d[0])]
    print(f"[ensemble.spec] Qt 裁剪：binaries {_before[0]} → {len(a.binaries)}，"
          f"datas {_before[1]} → {len(a.datas)}")

pyz = PYZ(a.pure)

_onedir = FORM == "onedir"
exe = EXE(
    pyz,
    a.scripts,
    [] if _onedir else a.binaries,      # onedir：二进制交给 COLLECT，别进 exe 内嵌
    [] if _onedir else a.datas,
    [],
    exclude_binaries=_onedir,
    name=APP_NAME if _onedir else PRODUCT,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                          # UPX 是杀软误报的头号来源（§五），不用
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,                      # GUI 产物不带控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

if _onedir:
    # onefolder：exe + 一个同级 bundle 目录（PyInstaller 6 默认 `_internal/`）。
    # `paths.resource_dir()` 冻结态认 `_MEIPASS != exe 目录` 那条分支，素材就在那里面。
    COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name=PRODUCT,
    )
