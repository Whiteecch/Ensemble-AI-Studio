# packaging — 构建、打包与分发

这里只有打包相关的东西，没有一行运行时逻辑。设计依据是 `docs/packaging.md`（§2 打包实现、§2.2 Inno Setup 要点、§2.3 一条命令构建、§三 首次运行体验）。

| 文件 | 作用 |
|---|---|
| `entry_gui.py` | PyInstaller 的入口脚本（冻结态下把 `sys.path` 补齐、做 Windows 防御、`harness.gui.app.main()`）。也带一个无界面自检分支（`ENSEMBLE_SELFTEST`），装完包后没有控制台时靠它验收。 |
| `ensemble.spec` | 唯一的 PyInstaller 构建脚本。**一份 spec 出两种产物**（one-folder / one-file），形态与 Qt 裁剪都走环境变量。 |
| `installer.iss` | Inno Setup 6 脚本：包 one-folder 产物，装到 `{autopf}\Ensemble-AI-Studio`。 |
| `build.ps1` | 一条命令出全部产物（Setup.exe / portable.zip / onefile.exe / wheel），汇总到 `dist/release/`。 |

## 一条命令

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

常用开关：`-Baseline`（额外出一份**不裁剪 Qt** 的 one-folder，做体积对照；只落 `dist/`，不进 `release/`）、`-SkipTests`、`-SkipVerify`、`-SkipInstaller`、`-SkipWheel`、`-Python <解释器>`、`-SourceRoot <别的源码树>`。

单独跑某一步也行（等价于 `build.ps1` 里那几行）：

```powershell
# one-folder（安装包用）
$env:ENSEMBLE_FORM="onedir"; $env:ENSEMBLE_EXCLUDE_QT="1"
python -m PyInstaller --noconfirm --clean --distpath dist --workpath build\work-onedir packaging\ensemble.spec

# one-file（免安装单文件）
$env:ENSEMBLE_FORM="onefile"
python -m PyInstaller --noconfirm --clean --distpath dist --workpath build\work-onefile packaging\ensemble.spec

# 安装包（版本号必须传，唯一来源是 app/pyproject.toml）
iscc /DMyAppVersion=0.1.0 packaging\installer.iss
```

## 为什么是"读环境变量的一份 spec"

`pyinstaller packaging/ensemble.spec --onedir` 里的 `--onedir/--onefile` **对 .spec 不生效**——PyInstaller 一旦拿到 spec 文件就只认里面的 `EXE()/COLLECT()`，命令行旗标静默忽略。"我写了 `--onefile` 却出来一个文件夹"就是这么来的。两条干净的做法：拆两份 spec（共享片段），或让 spec 读环境变量。这里选后者——两种产物的差异只有"收不收集目录"一处，拆两个文件会让裁剪/数据/版本三处逻辑各写两遍，迟早分叉。

| 变量 | 默认 | 含义 |
|---|---|---|
| `ENSEMBLE_FORM` | `onedir` | `onedir` \| `onefile` |
| `ENSEMBLE_EXCLUDE_QT` | `1` | `0` = 不裁剪（体积对照用） |
| `ENSEMBLE_SRC` | `<repo>/app/src` | 源码根 |
| `ENSEMBLE_MATERIALS` | `<repo>/app` | 素材根（`characters/ scenes/ config/`） |
| `ENSEMBLE_TEMPLATES` | `<repo>/templates` | 模板根（在**仓库根**，不在 `app/` 下） |

## 数据：进去的是什么

只有**内置只读素材**：`characters/ scenes/ config/`（来自 `ENSEMBLE_MATERIALS`）和 `templates/`（来自 `ENSEMBLE_TEMPLATES`）。两者都落在产物的**素材根**里——`harness.paths.resource_dir()` 冻结态指向的那一处（onefile = 解压目录；onedir = exe 同级的 `_internal/`）。

`templates/` 本期**打包**（不是"以后再说"）：它的 add-data 目标名写成 `templates`，而 `paths.templates_dir()` = `resource_dir()/"templates"`，两边自然对上，**不需要改 `paths.py`**。模板是"规格"、随包走、不给用户改，开发态它在仓库根（`app/templates/` 本来就不存在，`ensure_user_dirs()` 播种时找不到就跳过，这是既有的容错行为）。

**用户数据一个字节都不进产物**：`libraries/ runs/`、用户自己的卡、`*.runtime.json`、`*.sqlite` 都不在素材目录里（本项目的素材目录只放随包演示素材），spec 也只从上面那两个变量取数据。所以"用什么源构建"决定了产物里是什么——**构建必须在干净的公开素材上进行**。

这句话原来是**口头的**，现在由 `ensemble.spec::_check_materials()` 兜底：收数据之前先扫一遍 `ENSEMBLE_MATERIALS` / `ENSEMBLE_TEMPLATES`，命中下面任一条就**拒绝构建**并列出命中的文件：

- `*.runtime.json` / `*.sqlite` / `*.db`（运行期边车与数据库）；
- `libraries/` / `runs/` 目录（用户数据）；
- 文本素材（`.md/.json/.yaml/.txt`）正文里出现本机绝对路径（`C:\...` / `d:/...`）——作者机器上的路径会跟着产物发给每一个用户，`templates/` 里的说明文档最容易带进来。

确需放行（比如示例素材里本来就有一个 `.db`）时加 `ENSEMBLE_ALLOW_DIRTY_MATERIALS=1`，spec 会打印醒目警告后继续。默认拒绝 = 默认安全：泄露一次收不回来。

### wheel 里只有代码（`Ensemble-AI-Studio-<ver>` 之外的第四个产物）

`dist/release/ensemble-<ver>-py3-none-any.whl` 是**发布物里唯一"只有代码"的那一个**：只有 `harness/**.py` 与 dist-info，没有 `characters/ scenes/ config/ templates/`。原因是 setuptools 只能把**包内**（`src/harness/`）的东西打进 wheel，而素材在 `app/` 与仓库根、不在包内；装完之后 `harness` 落在 `site-packages`，`paths.resource_dir()`（非冻结态 = `__file__` 的上两级）解析到 `<venv>/Lib`——那里没有素材。所以：

- `pip install ensemble` 得到的是**引擎 + 三个命令 + 桌面应用的代码**（Qt 也要自己装）：用来 `import`，或者对着你自己的场景跑；
- 想要自带演示素材（福尔摩斯/华生/贝克街221B 与模板）就用 Setup 或 portable.zip，或者克隆仓库 `pip install -e`（开发态 `resource_dir()` 就是 `app/`，素材齐全）。

把素材塞进 wheel 需要三处联动——包内 `_materials/`（+ setuptools package-data + 构建期 staging）、`resource_dir()` 认安装态布局、`materials_dir()` 按安装态改判落点（否则导入器会往 `<venv>` 里写用户的卡）。这超出本期"只让内置素材在冻结态可寻"的范围（§1.1 的两侧分家是按冻结与否分的），故 README 的安装表按**事实**写明这一行是"仅代码"。要改这条结论，先改 `paths.py` 的语义，再改 README。

## 体积

实测（Windows 11，Python 3.13.2，PySide6 6.11.2，PyInstaller 6.22.2；`dist/` 下整棵树的字节数）：

| 形态 | 裁剪 | 体积 | 文件数 |
|---|---|---|---|
| one-folder | 无（`-Baseline`） | 137.7 MiB / 144.4 MB | 273 |
| one-folder | 有 | **89.1 MiB / 93.4 MB** | 161 |
| one-file | 有 | **41.6 MiB / 43.6 MB** | — |
| Setup.exe（one-folder 压 LZMA2） | 有 | **33.1 MiB / 34.7 MB** | — |
| portable.zip（one-folder 压 Deflate） | 有 | **41.6 MiB / 43.6 MB** | — |

裁剪一步砍掉 51 MB（-35%）、112 个文件。剩下的体积基本是"要跑就得有"的：`PySide6/Qt6Core+Qt6Gui+Qt6Widgets` 三个 DLL 约 25 MiB，Python 解释器与 `.pyd` 约 20 MiB，langgraph/langchain/pydantic/httpx 与 OpenSSL 约 30 MiB，还有 10 MiB 的 exe 本体（内含全部纯 Python 模块的 PYZ）。`docs/packaging.md` §2.1 估的"60–80 MB"是按 PySide6 **全量 200 MB+** 估的；实际 `Analysis` 只会收真正被 import 到的 Qt 模块，未裁剪的基线就已经是 144 MB，所以 89 MB 是这条依赖链上的合理落点。

裁剪分两层，缺一不可：

1. `excludes`（Python 层）：把没用到的 Qt 子模块从依赖图里摘掉，PyInstaller 就不会去收集它们的 Python 绑定。
2. `_is_dropped()`（产物层，`Analysis` 之后过滤已收集的 binaries/datas）：PySide6 的 hook 会按"Qt 模块被导入了"把**整套插件与 DLL** 收进来，只靠 excludes 拦不住。留下的插件目录只有 `platforms`（`qwindows`/`qoffscreen`，必需）、`styles`、`imageformats`+`iconengines`（图标）。

一并裁掉的还有 `opengl32sw.dll`（Mesa 软件 OpenGL，20 MB，只在"没有显卡驱动"时才被 Qt 选用；本应用是纯 Widgets 光栅绘制，不走 OpenGL）与多媒体编解码器（`avcodec-*`/`avformat-*`/`avutil-*`/`swscale-*`，跟着 QtMultimedia 来的）。要全部拿回来就 `ENSEMBLE_EXCLUDE_QT=0`。

## 验收：产物真的能起来吗

装完之后既没有控制台、又常常没有显示器，所以入口脚本带一个自检分支：

```powershell
$env:QT_QPA_PLATFORM="offscreen"
$env:ENSEMBLE_SELFTEST="$PWD\selftest.txt"
& "dist\Ensemble-AI-Studio-0.1.0-onedir\Ensemble-AI-Studio.exe"
Get-Content .\selftest.txt
```

它不开窗口，只把 `is_frozen()/resource_dir()/materials_dir()/templates_dir()/user_dir()`、素材清单（每个目录里到底有几个文件）、播种结果、`QVApplication` 能否建成、`harness.gui.main_window` 能否导入写进报告，最后一行是 `RESULT: OK` / `FAIL`。`build.ps1` 的第 6 步就是拿它当闸门（`-SkipVerify` 可跳过）。

`_is_dropped()` 剪错了东西的话，`QApplication` 那一段或 `main_window` 的导入会当场炸——这就是"体积优化"和"还能不能用"之间唯一的验证点。

## 三个坑（都踩过）

1. **构建环境里的 editable 安装会偷走源码树。** 用 `pip install -e` 装过本项目的 venv，其 `__editable__.*.pth` 会把那个仓库的 `app/src` 加进 `sys.path`；`Analysis` 用的是 `sys.path.extend(pathex)`（**追加**），压不住它——于是在 A 仓构建、打进去的却是 B 仓的代码，而且不会有任何报错。`ensemble.spec` 因此显式把 `ENSEMBLE_SRC` 插到 `sys.path[0]`、同步写进 `PYTHONPATH`（`collect_submodules` 跑在隔离子进程里，只认环境变量），并在 `Analysis` 前断言解析到的 `harness` 确实在 `ENSEMBLE_SRC` 下——宁可构建失败，也不出"装错源"的包。
2. **不许"随便找个 python 就开工"。** 仓库里没有 `.venv` 时，`python` 很可能是系统解释器（这个开发机上它带着一份 user-site 的 PyInstaller，外加 torch/numpy/scipy）。那样构建不会报"环境不对"，而是先收进一堆无关的包、再在 `base_library.zip` 上抛一个看不懂的 `FileNotFoundError`。`build.ps1` 因此：有 `-Python` 用 `-Python`；否则只认 `<repo>/app/.venv`；都没有就按 §2.3 第 1 步**建 venv 并 `pip install -e ".[dev,gui]" pyinstaller`**（唯一需要联网的一步）；最后还要 `Assert-Provisioned` 探一次 `import PyInstaller, PySide6, langgraph, pydantic, httpx, yaml, aiosqlite`，缺哪个就直说缺哪个。
3. **`build.ps1` 故意全用 ASCII。** Windows PowerShell 5.1 按 ANSI 读 `.ps1`（除非文件带 UTF-8 BOM），UTF-8 中文注释会被读成乱码，乱码又可能把引号拆坏。中文说明一律留在本文件里。

## Inno Setup 要点（`installer.iss`）

- 装到 `{autopf}\Ensemble-AI-Studio`（默认 `Program Files`，允许用户改）；`PrivilegesRequiredOverridesAllowed=dialog`，用户可在向导里选"只为我安装"从而完全不碰 UAC。
- **64 位载荷必须显式声明 64 位安装模式**：`ArchitecturesAllowed=x64compatible` + `ArchitecturesInstallIn64BitMode=x64compatible`。少了它们 Inno 会把整场安装按 32 位跑——`{autopf}` 解析成 `C:\Program Files (x86)`（README 公示的默认路径 `C:\Program Files\Ensemble-AI-Studio` 就成了错的），64 位二进制落进 32 位目录，卸载项只写进 32 位注册表视图（`WOW6432Node`）：PowerShell 默认视图、多数盘点与静默卸载脚本按原生视图都**看不到**它（"装了但清单里没有、卸不掉"）。载荷是 64 位（Python 3.13 x64 + Qt6 x64），32 位 Windows 上根本跑不起来，所以用 `x64compatible` 直接拒绝在那里安装，而不是装一个起不来的东西。
- 从 **one-folder 产物**打包（`..\dist\Ensemble-AI-Studio-<ver>-onedir\*`）；开始菜单 + 可选桌面快捷方式；卸载器带 `UninstallDisplayIcon`。
- 附 `LICENSE`（MIT）并在向导里展示，必须接受才能继续。
- **不写任何文件关联**：脚本里没有 `[Registry]` 段（注册表里只会有卸载项本身，那是 Inno 的机制，不是关联）。
- **卸载保留用户数据**：`{app}` 由卸载器自己删，`%APPDATA%\Ensemble-AI-Studio` 一个字节都不动；完成页有"打开用户数据目录"的勾选，卸载后的弹窗也会指出该目录在哪、想清干净就手动删它。
- 版本号由 `build.ps1` 用 `/DMyAppVersion=<ver>` 传进来（唯一来源仍是 `app/pyproject.toml`）。手动跑 `iscc` 忘了传时，版本会落成 `0.0.0-UNSET` 并打印警告——宁可名字刺眼，也不要静默出一个版本号错的安装包。
