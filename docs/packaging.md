# 打包与分发方案

> 目标：让普通用户**下载一个文件就能用**——`
> Ensemble-AI-Studio-Setup-x.y.z.exe` 双击安装，或免安装 zip 解压即用。
>
> 现状：本方案**尚未实施**。本文是实施清单与决策记录；动手前请先读「一、前置工程」，那是能否打包成功的关键。

---

## 一、前置工程（必须做，否则打包后打不开）

当前程序从**源码目录**读素材：`gui/app.py` 用 `Path(__file__).parents[3]` 定位 `scenes/`、`characters/`、`config/`；`runner.py` 与 `tools/import_cards.py` 的默认路径是**相对当前工作目录**。打包成 exe 后源码树不存在；装到 `Program Files` 后**写入还会被系统拒绝**。因此：

### 1.1 新增 `app/src/harness/paths.py`（单一出处）

| 函数 | 语义 |
|---|---|
| `resource_dir() -> Path` | **只读**内置素材所在目录。开发态：仓库里的 `app/`；PyInstaller 冻结态：`sys._MEIPASS`（onefile）或 exe 同级的 `data/`（onefolder） |
| `user_dir() -> Path` | **可写**用户数据目录：Windows `%APPDATA%\Ensemble-AI-Studio`，macOS `~/Library/Application Support/Ensemble-AI-Studio`，其它 `$XDG_CONFIG_HOME/Ensemble-AI-Studio`（未设则 `~/.config/...`）。与 `gui/settings.py::default_settings_path()` **同源**，避免两处各写一份 |
| `characters_dir() / scenes_dir() / config_dir() / runs_dir() / templates_dir()` | `user_dir()` 下的对应子目录（除 `templates_dir()` 读内置只读素材） |
| `ensure_user_dirs(seed: bool = True)` | 建目录；`seed=True` 时把内置 `characters/ scenes/ config/ templates/` **复制进用户目录**，已存在的文件**不覆盖**（首次运行的播种） |
| `is_frozen() -> bool` | `getattr(sys, "frozen", False)` 的封装，便于测试注入 |

### 1.2 改造调用点（清单）

| 文件 | 现在 | 改成 |
|---|---|---|
| `gui/app.py` | `_APP_DIR = Path(__file__).parents[3]`，默认场景/角色/模型 | `paths.resource_dir()` / `paths.user_dir()`；启动时先 `paths.ensure_user_dirs()` |
| `runner.py` | 默认路径 cwd 相对 | 同上（CLI 与 GUI 行为一致） |
| `tools/import_cards.py` | 默认 `characters/`、`scenes/` cwd 相对 | `paths.characters_dir()` / `paths.scenes_dir()` |
| `gui/worker.py` / `engine.py` | `run_root` 默认 `app/runs` | `paths.runs_dir()` |
| `gui/settings.py` | 已有 `APP_DIR_NAME` | 改为复用 `paths.user_dir()`，消除重复 |

### 1.3 验收

- 开发态跑：现有 950 测试全绿，且测试里**不得写真实用户目录**（`conftest` 已有隔离夹具，新代码要沿用）。
- 「伪冻结」测试：`monkeypatch` `is_frozen()=True` + 临时 `_MEIPASS`/exe 目录 → `resource_dir()`/`user_dir()` 各自解析正确、播种只做一次且不覆盖已有文件。

---

## 二、打包实现

### 2.1 PyInstaller（两种产物共用一套 spec）

`packaging/ensemble.spec`：

- 入口：`packaging/entry_gui.py` → `from harness.gui.app import main; main()`（GUI 用 `--windowed`，无控制台窗口）。
- 数据：`--add-data` 带上 `characters/ scenes/ config/ templates/`（内置素材，供首次播种）。
- **精简体积**：PySide6 全量约 200MB+。排除未用模块可降到 ~60–80MB：
  `QtWebEngineCore/ QtWebEngineWidgets/ QtQml/ QtQuick/ Qt3D*/ QtCharts/ QtMultimedia/ QtNetworkAuth/ QtDesigner/ QtPdf*` 等；仅保留 `QtCore QtGui QtWidgets`。
- 两种形态：
  - **one-folder**（`--onedir`）：启动快，供安装包使用；
  - **one-file**（`--onefile`）：单 exe，首次启动解压到临时目录，稍慢，供免安装分发。
- 版本号**单一来源**：从 `app/pyproject.toml` 的 `version` 读，产物名统一 `Ensemble-AI-Studio-<version>-<form>.zip/exe`。

### 2.2 Inno Setup（Windows 一键安装包）

`packaging/installer.iss` 要点：

- 安装目录 `{autopf}\Ensemble-AI-Studio`（默认 `Program Files`，允许用户改）。
- 从 **one-folder 产物**打包；创建开始菜单与桌面快捷方式（可选）；写卸载器（`UninstallDisplayIcon`）。
- 附带 `LICENSE`（MIT）并在安装页展示。
- **不写注册表关联**（本项目不需要文件关联）；不安装服务；不需要管理员权限时才可选 `PrivilegesRequired=lowest`（装到用户目录，避免 UAC）。
- 卸载时**保留用户数据**（`%APPDATA%\Ensemble-AI-Studio`），在卸载完成页说明如何手动清除。

### 2.3 一条命令构建

`packaging/build.ps1`（Windows）：

```
1) 建/激活 venv，pip install -e ".[dev,gui]" pyinstaller
2) 读 pyproject 版本号
3) pyinstaller packaging/ensemble.spec --onedir   → dist/ensemble/
4) Compress-Archive dist/ensemble → Ensemble-AI-Studio-<ver>-portable.zip
5) pyinstaller packaging/ensemble.spec --onefile  → Ensemble-AI-Studio-<ver>.exe
6) iscc packaging/installer.iss                   → Ensemble-AI-Studio-Setup-<ver>.exe
7) 产物汇总到 dist/release/
```

### 2.4 跨平台

- **代码**按 §1.1 写成分平台的（`user_dir()` 三分支、无 Windows 专有调用）。
- **先只出 Windows 产物**。macOS：后续用 PyInstaller 出 `.app` + `create-dmg`（需在 mac 上构建）；Linux：`--onefile` + 可选 AppImage。
- Windows 上无法交叉构建 mac/Linux 包，这一步必须各自平台或 CI 矩阵。

---

## 三、首次运行体验

1. 无 `%APPDATA%` 数据 → 播种内置素材 → 主界面空状态页（三按钮）。
2. 没有 API key → 自动离线 `stub`（占位台词），界面顶部显示"离线 stub"徽标；设置里填入 key 与模型名后重启场景即走真实模型。
3. 用户自己的角色/场景/存档都在 `%APPDATA%\Ensemble-AI-Studio\`，升级安装包不影响，卸载默认保留。
4. 出问题时看哪里：状态 chip / 日志面板 / `%APPDATA%\Ensemble-AI-Studio\runs\`（每场一个目录，含转录与角色私有记忆）。

---

## 四、发版流程

1. 本地构建 §2.3 产物 → 自测（装一遍、开一场戏、导入一次模板）。
2. `git tag v<version> && git push origin v<version>`。
3. GitHub Release：写说明（相对上一版的改动）+ 附件传 `Setup.exe` / `portable.zip` / `ensemble-<ver>-py3-none-any.whl`。
4. （可选，推荐）GitHub Actions 矩阵构建：`windows-latest` 出 Setup/zip，`macos-latest` 出 dmg，`ubuntu-latest` 出 AppImage/zip；tag 触发后自动挂到 Release。

---

## 五、风险与注意事项

| 风险 | 说明与对策 |
|---|---|
| **体积** | PySide6 是主要体积来源。按 §2.1 排除未用 Qt 模块；one-folder 通常 60–100MB，压缩后 ~30–50MB |
| **杀软误报** | PyInstaller one-file 常被启发式误报。对策：优先分发 one-folder + Setup；有条件做代码签名 |
| **SmartScreen 警告** | 未签名的 exe 首次运行会提示"未知发布者"。文档里写清"更多信息 → 仍要运行"，长期考虑签名证书 |
| **首次启动慢** | one-file 需解压，10–30 秒属正常；装包版（one-folder）无此问题 |
| **写入权限** | 安装目录一律只读；所有写操作走 `%APPDATA%`（§1.1 已覆盖） |
| **自动更新** | 本期**不做**。升级方式=下载新安装包覆盖安装（用户数据保留） |
| **依赖版本漂移** | 打包前用**锁定版本**的 venv 构建；产物与 `pyproject.toml` 的 `dependencies` 一致 |

---

## 六、实施清单（可逐项打勾）

- [ ] 新增 `paths.py`（含 `is_frozen` / `resource_dir` / `user_dir` / 各子目录 / `ensure_user_dirs`）
- [ ] 改造 §1.2 的五个调用点，`settings.py` 复用 `user_dir()`
- [ ] `paths` 单测（开发态 / 伪冻结 / 播种不覆盖 / 三分支 user_dir）
- [ ] 全量测试仍绿；`conftest` 隔离夹具覆盖新路径
- [ ] `packaging/entry_gui.py`、`ensemble.spec`、`installer.iss`、`build.ps1`
- [ ] 本地构建一次：装包 → 开一场戏 → 导入模板 → 卸载（确认用户数据保留）
- [ ] README 增补「安装」章节（下载哪个文件、怎么装、首次运行看到什么）
- [ ] 发 `v0.1.0` Release，附件含 Setup / portable / wheel
- [ ] （可选）GitHub Actions 矩阵构建
