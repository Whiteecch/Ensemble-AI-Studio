"""路径解析的**单一出处**：内置只读素材 vs 可写用户数据目录（打包方案 §1.1）。

打包成 exe 之后源码树不存在（素材从 `__file__` 定位会指到 zip/临时目录之外），装到
`Program Files` 之后安装目录**只读**（往那儿写会被系统拒绝）。所以全项目的路径只有两类，
本模块把它们各收成一处：

  · **只读素材**（`resource_dir()`）：随包走的 characters/scenes/config/templates。
    开发态 = 仓库里的 `app/`；冻结态 = PyInstaller 解出来的素材根（见 `resource_dir`）。
  · **可写用户数据**（`user_dir()`）：角色/场景/信息库/存档/设置。永远在用户目录下——
    升级安装包不影响，卸载默认保留（打包方案 §三）。

判据与写法的三条纪律：

1. **开发态逐字节不变**：`is_frozen()` 为假时，所有解析结果必须与改造前逐字节相同
   （`resource_dir()` 就是老 `gui/app.py::_APP_DIR` 那个 `app/`）。打包改造不许改变
   开发态的既有行为——那是最容易踩的坑。
2. **测试可注入**：`is_frozen()` / `resource_dir()` / `user_dir()` 都是模块级函数，测试
   monkeypatch 它们就能伪造冻结、把用户目录指到 tmp。`conftest` 正靠这一点保证**任何
   用例都不会写真实用户目录**（`ensure_user_dirs()` 会往用户目录复制文件，真播到
   `%APPDATA%` 就是事故）。
3. **单一出处**：目录名常量与三分支平台逻辑只在这里写一份，`gui/settings.py` 复用
   `user_dir()`（不再自带一份 `APP_DIR_NAME` 与平台分支）。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

#: 用户数据目录名。Windows `%APPDATA%\<此名>`、macOS `~/Library/Application Support\<此名>`、
#: 其它 `$XDG_CONFIG_HOME\<此名>`。**唯一出处**（`gui/settings.py` 从这里 import）。
APP_DIR_NAME = "Ensemble-AI-Studio"

#: **改名前的**用户数据目录名（历史名，仅供**只读**兼容）。
#:
#: 目录名从 `文字创作agent` 统一到 `Ensemble-AI-Studio` 是 §1.1 的要求；但改名若只改常量，
#: 老用户（含开发机）那份已填过 api_key/主题/语言的 `settings.json` 会**静默失效**——读不到、
#: 也不报错，表现是"我的设置怎么回默认了"。故新落点里没有的东西，回落到这里**读一遍**：
#: 只读、不写、不删（见 `legacy_user_dirs()` 与 `gui/settings.py::SettingsStore.load`）。
LEGACY_APP_DIR_NAMES: tuple[str, ...] = ("文字创作agent",)

#: 用户设置文件名（`user_dir()` 下）。
SETTINGS_FILE_NAME = "settings.json"

#: 要建/要播种的子目录名。templates 也播一份到用户目录（随包只读底本 + 用户可改副本），
#: 但 `templates_dir()` 读的仍是**内置只读**那份（§1.1 的表）。
_SEED_DIR_NAMES: tuple[str, ...] = ("characters", "scenes", "config", "templates")

#: 用户目录下始终建好的子目录（runs 不播种，但必须能写）。
_USER_DIR_NAMES: tuple[str, ...] = ("characters", "scenes", "config", "runs")


def is_frozen() -> bool:
    """是否跑在 PyInstaller 产物里（`getattr(sys, "frozen", False)` 的封装）。

    单独包一层是**为了测试能注入**：伪冻结用例 monkeypatch 本函数就够，不必去伪造
    `sys.frozen` 再逐处清理。
    """
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> Path:
    """**只读**内置素材所在目录（characters/scenes/config/templates 的上一级）。

    开发态（`is_frozen()` 为假）= 仓库里的 `app/`——与改造前 `gui/app.py::_APP_DIR`
    （`Path(__file__).resolve().parents[3]`）、`runner.py` 的 `parents[2]` 同一算法，
    故开发态解析结果逐字节不变。**不冻结时绝不看 `_MEIPASS`**：打包环境的变量会泄漏进
    子进程，判据只能是 `is_frozen()`。

    冻结态两种产物形态（判据：`_MEIPASS` 是不是**exe 目录之外的**解压目录）：

      · **onefile**：`sys._MEIPASS` 指向进程启动时解压出来的临时目录（与 exe 不同）→ 用它；
      · **onefolder**：`_MEIPASS` 就是 exe 所在目录（老版 PyInstaller 布局）或干脆没有
        → 取 exe 同级的 `data/`；`data/` 不在则退回 **exe 同级本身**（`--contents-directory .`
        的扁平布局素材就摆在 exe 旁边，见下）。**相对 exe 而非 cwd**：装到 `Program Files`
        后从快捷方式启动时 cwd 可能是任何地方（甚至只读），相对 cwd 必然找不到素材。

    注：新版 PyInstaller（≥6）的 onedir 把 `--add-data` 放进 `exe 同级/_internal`，此时
    `_MEIPASS` 正是那个目录、且不等于 exe 目录，于是走上面第一条分支——素材照旧找得到。
    `--contents-directory data` 同理（`_MEIPASS` = `exe/data`）。真正落到最后那条兜底的是
    **没有独立 bundle 目录**的扁平布局：`--contents-directory .` 或 PyInstaller ≤5 的 onedir
    （那时 `_MEIPASS` == exe 目录，`--add-data` 的素材就落在 exe 同级），故 `data/` 只在
    它确实存在时才用——否则会指到一个不存在的 `data/`，"打包版打开就是空的"。
    """
    if not is_frozen():
        return Path(__file__).resolve().parents[2]        # app/（本文件在 app/src/harness/）
    exe_dir = Path(sys.executable).resolve().parent
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bundle = Path(meipass).resolve()
        if bundle != exe_dir:
            return bundle                                 # onefile：解压目录
    data = exe_dir / "data"                               # onefolder：exe 同级 data/
    return data if data.is_dir() else exe_dir


def materials_dir() -> Path:
    """缺省**素材库**目录：演示场景/角色/模型/竞价这些"用户也会改"的素材的落点。

    · 开发态（不冻结）= `resource_dir()`——就是今天的 `app/`，与改造前逐字节同址；
    · 冻结态 = `user_dir()`——安装目录只读，而启动时 `ensure_user_dirs()` 已把内置素材播种
      成**可编辑副本**（升级安装包不冲掉用户改过的那份）。

    这一处是**读写必须同址**的枢纽：GUI 的「角色库/场景库」目录取自"当前场景/当前卡所在
    目录"，CLI 的导入缺省、引擎的装卡目录也都由它派生。两侧若各指一处，用户导入的卡在界面里
    永远看不见（`templates/使用说明.md` 明写"导入成功后出现在应用的库里"），新建的卡还会写进
    只读的安装目录。故这里按冻结与否二选一，绝不让读侧与写侧分家。
    """
    return user_dir() if is_frozen() else resource_dir()


def _platform_base() -> Path:
    """平台用户数据**根**（`user_dir()` 的上一级）。三分支与缺失回落只写在这一处。

    单独拎出来是因为旧目录名（`LEGACY_APP_DIR_NAMES`）要用**同一个根**拼出兼容路径——
    若在别处再写一遍平台分支，某天就会与 `user_dir()` 分叉（那正是"两处各写一份"的老病）。
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or ""
        return Path(base) if base.strip() else Path.home() / "AppData" / "Roaming"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    base = os.environ.get("XDG_CONFIG_HOME") or ""
    return Path(base) if base.strip() else Path.home() / ".config"


def legacy_user_dirs() -> list[Path]:
    """**只读**兼容用的旧用户数据目录（改名前的落点，顺序=新→旧）。

    当前只有设置文件会用它（`gui/settings.py::SettingsStore.load` 在新落点读不到时逐个回落）。
    约定死三条：**只读**（写永远写新落点）、**不删**（用户那份老文件原样留着，手动清理由
    用户决定）、**不迁移复制**（老文件里改了字段，读的还是它——直到用户下一次保存，那时
    整份设置自然写进新落点）。

    conftest 的沙箱夹具把它指成**空表**：测试里连"老目录"都不存在，绝无读真实用户文件之虞。
    """
    base = _platform_base()
    return [base / name for name in LEGACY_APP_DIR_NAMES]


def user_dir() -> Path:
    """**可写**用户数据目录（角色/场景/信息库/存档/设置都在这儿）。

    Windows `%APPDATA%\\Ensemble-AI-Studio`（缺失则 `~/AppData/Roaming/...`）、
    macOS `~/Library/Application Support/Ensemble-AI-Studio`、
    其它 `$XDG_CONFIG_HOME/Ensemble-AI-Studio`（未设则 `~/.config/...`）。

    `gui/settings.py::default_settings_path()` 与它**同源**（调它，不另写一份）。测试里
    monkeypatch 本函数即可把整个用户目录关进沙箱——**所有**用户目录入口都必须经它。
    """
    return _platform_user_dir()


def _platform_user_dir() -> Path:
    """`user_dir()` 的**真实实现**（三分支 + 环境变量缺失时的回落），不做任何测试注入。

    单独拎出来是为了让"要验平台/环境变量分支"的用例能把 `user_dir()` 还原成它
    （conftest 的沙箱夹具默认会把 `user_dir()` 整体指到 tmp）。
    """
    return _platform_base() / APP_DIR_NAME


def characters_dir() -> Path:
    """**用户**角色目录（可写：永远在 `user_dir()` 下）。

    与 `materials_dir()/"characters"` 的区别只在开发态：那时素材库就是仓库里的 `app/`
    （开发者的卡一直放那儿，界面读的也是那儿），**写**进仓库正是既有行为；冻结态两者同一个
    目录。要"缺省导入落哪儿"用 `materials_dir()`，要"用户目录在哪"用本函数。
    """
    return user_dir() / "characters"


def scenes_dir() -> Path:
    """**用户**场景目录（可写：永远在 `user_dir()` 下）。开发态的取舍见 `characters_dir()`。"""
    return user_dir() / "scenes"


def config_dir() -> Path:
    """用户配置目录（可写：模型/竞价 yaml 的用户副本落这儿）。"""
    return user_dir() / "config"


def runs_dir() -> Path:
    """用户存档根（可写：每场一个子目录，含转录与角色私有记忆）。"""
    return user_dir() / "runs"


def templates_dir() -> Path:
    """模板目录 = **内置只读**素材里的 `templates/`（模板即规格，随包走、不给用户改）。"""
    return resource_dir() / "templates"


def _copy_tree_missing(src: Path, dst: Path) -> None:
    """把 `src` 下的文件树复制进 `dst`，**已存在的文件一律跳过**（首次运行播种）。

    只做加法：不删、不覆盖（用户改过的卡不能因为一次重启/升级被内置版盖掉）。源目录
    缺失、源就是目标、单个文件复制失败（权限/占用）都静默跳过——素材缺失是**降级**，
    不该把软件挡在启动门外。
    """
    if not src.is_dir():
        return
    try:
        if src.resolve() == dst.resolve():          # 开发态硬把用户目录指到素材目录：无事可做
            return
    except OSError:                                 # pragma: no cover - 极端路径不可解析
        return
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        target = dst / path.relative_to(src)
        if target.exists():
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        except OSError:                             # pragma: no cover - 权限/占用
            continue


def ensure_user_dirs(seed: bool = True) -> Path:
    """建好用户目录（并按其缺省播种），返回 `user_dir()`。**启动时调用**。

    `seed=True`（缺省）时把内置的 `characters/ scenes/ config/ templates/` **复制**进用户
    目录——首次运行时用户目录还是空的，用户得有一份**可编辑**的底本（内置那份是随包走的
    只读原件）。已存在的文件**不覆盖**：播种因此天然幂等，"只做一次"由这条不覆盖规则
    保证，不需要额外的标记文件。

    幂等 + 只加不减，故随时可调（每次启动都调一次也无副作用）。
    """
    root = user_dir()
    root.mkdir(parents=True, exist_ok=True)
    for name in _USER_DIR_NAMES:
        (root / name).mkdir(parents=True, exist_ok=True)
    if seed:
        source = resource_dir()
        for name in _SEED_DIR_NAMES:
            _copy_tree_missing(source / name, root / name)
    return root
