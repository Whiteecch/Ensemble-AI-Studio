"""`harness.paths` 的契约测试：内置只读素材 vs 可写用户数据目录（打包方案 §1.1/§1.3）。

这组用例钉的是**打包改造的地基**，四件事缺一不可：

1. **开发态逐字节不变**：不冻结时 `resource_dir()` 必须仍是仓库里的 `app/`——与改造前
   `gui/app.py::_APP_DIR`（`parents[3]`）、`runner.py`/`tools/import_cards.py`
   （`parents[2]`/`parents[3]`）用的是**同一套算法**。打包改造最容易出的错就是"顺手把
   开发态的素材目录也挪了"，那会让 1970 条既有用例与用户本机素材一起崩掉。
2. **伪冻结**：`monkeypatch` `is_frozen()=True` + 伪造 `_MEIPASS`/exe 目录 → 两种产物形态
   （onefile 解压目录 / onefolder 的 `data/`）各自解析正确。没有这一层，"打包后打不开"
   这类问题只能等真打了包才发现。
3. **用户目录三分支**：Windows/macOS/XDG 各走各的，且 XDG/APPDATA 未设时回落。
   `gui/settings.py::default_settings_path()` 必须与它**同源**（不许两处各写一份）。
4. **播种不覆盖**：`ensure_user_dirs()` 只在首次把内置素材复制进用户目录，**已存在的文件
   一个都不动**——用户改过的卡不能因为一次升级/重启被内置版盖掉。

隔离纪律：conftest 的 autouse 夹具把 `paths.user_dir()` 指到了一次性 tmp（**绝不写真实
用户目录**）；本文件里凡是要钉"真实平台分支"的用例，都先经 `real_user_dir` 夹具把
`user_dir()` 还原成真实实现，再自己给环境变量。

「调用点接线」一节钉的是 §1.2 那五个改造点**真的接上了 paths**（spy/断言默认值，不是
读一眼代码就算数）——素材读内置只读目录、写一律落用户数据目录。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

from harness import paths as paths_mod
#: **import 时**抓一份真实实现：conftest 的沙箱夹具会把 `settings.default_settings_path`
#: 换成指向自己沙箱的替身（防写真实用户目录），而"与 user_dir 同源"这条契约要验的正是
#: 那个被换下去的原函数。
from harness.gui.settings import default_settings_path as _real_default_settings_path

#: 与改造前 `gui/app.py::_APP_DIR` 同一算法：本文件在 app/src/harness/tests/ 下，
#: `parents[2]` = app/src，`parents[3]` = app。
APP_DIR = Path(paths_mod.__file__).resolve().parents[2]

#: **import 时**抓一份 `legacy_user_dirs` 的真身（conftest 的沙箱夹具会把它置空，
#: 而"旧目录名/平台分支"这类契约要验的正是真身）。
_REAL_LEGACY_USER_DIRS = paths_mod.legacy_user_dirs


# ============================================================ 夹具
@pytest.fixture
def real_user_dir(monkeypatch):
    """把 `paths.user_dir()` 还原成**真实实现**（不做测试注入）。

    本文件里"三分支 / 与 settings 同源"这类用例要钉的正是平台与环境变量本身，而 conftest
    的沙箱夹具会把 `user_dir()` 整体指到 tmp——两者同时生效就什么都测不出来了。还原之后
    这些用例自己给 env（并只断言路径，不写盘），故仍然不碰真实用户目录。
    """
    monkeypatch.setattr(paths_mod, "user_dir", paths_mod._platform_user_dir)
    return paths_mod._platform_user_dir


@pytest.fixture
def real_legacy_user_dirs(monkeypatch):
    """把 `paths.legacy_user_dirs()` 还原成**真实实现**（理由同 `real_user_dir`）。

    conftest 的沙箱夹具把它指成空表（防读真实老目录）；要验"旧目录名/平台分支"的用例
    必须先把真身还回来——快照取自本模块 import 时（那时还没有任何夹具在 patch）。
    """
    monkeypatch.setattr(paths_mod, "legacy_user_dirs", _REAL_LEGACY_USER_DIRS)
    return _REAL_LEGACY_USER_DIRS


@pytest.fixture
def fake_home(monkeypatch, tmp_path: Path) -> Path:
    """把 `Path.home()` 钉到 tmp。

    平台分支里的"缺失即回落家目录"两处都经 `Path.home()`，而 Windows 上它**不看** `HOME`
    环境变量（认 USERPROFILE），Linux 上又认 HOME——想跨平台测同一件事，只能替身 `Path.home`
    本身，否则用例结果会随跑在哪个系统上而变。回落值本身仍只**断言路径**，不写盘。
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


@pytest.fixture
def fake_bundle(tmp_path: Path, monkeypatch) -> Path:
    """伪造一棵"内置素材"树（characters/scenes/config/templates），并把 `resource_dir()` 指过去。

    播种用例绝不能拿真素材目录当源：那会把用例的期望绑死在仓库当前的内容上（用户加了张卡
    就红），也会真的去读用户本机的创作文件。
    """
    bundle = tmp_path / "bundle"
    for name in ("characters", "scenes", "config", "templates"):
        (bundle / name).mkdir(parents=True)
    (bundle / "characters" / "甲.json").write_text('{"name": "甲"}', encoding="utf-8")
    (bundle / "characters" / "乙.json").write_text('{"name": "乙"}', encoding="utf-8")
    (bundle / "scenes" / "茶室.json").write_text('{"name": "茶室"}', encoding="utf-8")
    (bundle / "config" / "models.yaml").write_text("think: {}\n", encoding="utf-8")
    (bundle / "templates" / "角色卡模板.md").write_text("# 模板\n", encoding="utf-8")
    monkeypatch.setattr(paths_mod, "resource_dir", lambda: bundle)
    return bundle


@pytest.fixture
def fake_frozen(tmp_path: Path, monkeypatch):
    """伪冻结：`is_frozen()` 恒真 + 伪造 exe 位置；返回 (exe_dir, meipass 设置器)。"""
    monkeypatch.setattr(paths_mod, "is_frozen", lambda: True)
    exe_dir = tmp_path / "dist" / "ensemble"
    exe_dir.mkdir(parents=True)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "ensemble.exe"))

    def _set_meipass(value: Path | None) -> None:
        if value is None:
            monkeypatch.delattr(sys, "_MEIPASS", raising=False)
        else:
            monkeypatch.setattr(sys, "_MEIPASS", str(value), raising=False)

    return exe_dir, _set_meipass


# ============================================================ is_frozen
def test_is_frozen_wraps_sys_frozen(monkeypatch):
    """`is_frozen()` 就是 `getattr(sys, "frozen", False)` 的封装——**便于测试注入**。

    单独包一层而不是各处直接读 `sys.frozen`，是因为"伪冻结"用例只能靠 monkeypatch 一个
    函数名来伪造，直接读属性的话每个调用点都得各自伪造一遍。
    """
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert paths_mod.is_frozen() is False
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert paths_mod.is_frozen() is True


# ============================================================ resource_dir：开发态
def test_resource_dir_dev_is_app_root_same_as_before(monkeypatch):
    """开发态 `resource_dir()` == 仓库里的 `app/`，与改造前三条既有算法**口径一致**。

    §3 的最高约束：不冻结时"所有路径解析结果与今天完全相同"。改造前素材目录被三处各自
    算了一遍（`gui/app.py` 的 `parents[3]`、`tools/import_cards.py` 的 `parents[3]`、
    `runner.py` 的 `parents[2]`）——这里把它钉成"三处算出来必须是同一个目录"。
    """
    monkeypatch.delattr(sys, "frozen", raising=False)
    resolved = paths_mod.resource_dir()
    assert resolved == APP_DIR
    assert resolved.is_absolute()
    assert (resolved / "scenes" / "贝克街221B.json").is_file(), "内置演示场景必须在素材目录里"
    assert (resolved / "config" / "models.yaml").is_file()

    from harness.gui import app as gui_app
    from harness import runner as runner_mod
    from harness.tools import import_cards

    assert (Path(gui_app.__file__).resolve().parents[3] == resolved)
    assert (Path(import_cards.__file__).resolve().parents[3] == resolved)
    assert (Path(runner_mod.__file__).resolve().parents[2] == resolved)


def test_resource_dir_dev_ignores_meipass(monkeypatch, tmp_path: Path):
    """不冻结时**不许**被 `_MEIPASS` 带跑：开发态跑测试时环境里可能残留它（打包环境变量
    会泄漏进子进程），判据只能是 `is_frozen()`。"""
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "解压目录"), raising=False)
    assert paths_mod.resource_dir() == APP_DIR


# ============================================================ resource_dir：伪冻结
def test_resource_dir_frozen_onefile_uses_meipass(fake_frozen, tmp_path: Path):
    """onefile：`sys._MEIPASS` 是**解压出来的临时目录**（与 exe 所在目录不同）→ 用它。

    判据：`_MEIPASS` 存在且不等于 exe 目录 = onefile 的解压目录；否则按 onefolder 处理。
    """
    exe_dir, set_meipass = fake_frozen
    extract = tmp_path / "_MEI123456"
    extract.mkdir()
    set_meipass(extract)
    assert paths_mod.resource_dir() == extract
    assert paths_mod.resource_dir() != exe_dir


def test_resource_dir_frozen_onefolder_uses_exe_sibling_data(fake_frozen):
    """onefolder：exe 同级的 `data/`（`--contents-directory data` 的布局，`_MEIPASS` 指向它
    或干脆没有）。"""
    exe_dir, set_meipass = fake_frozen
    (exe_dir / "data" / "characters").mkdir(parents=True)   # 真实产物里 add-data 就落这儿
    set_meipass(exe_dir)
    assert paths_mod.resource_dir() == exe_dir / "data"

    set_meipass(None)                     # 老版本/裁剪过的产物可能根本没有 _MEIPASS
    assert paths_mod.resource_dir() == exe_dir / "data"


def test_resource_dir_frozen_flat_layout_falls_back_to_exe_dir(fake_frozen, tmp_path: Path):
    """**扁平 onedir**（`--contents-directory .`，或 PyInstaller ≤5 的 onedir）→ exe 同级**本身**。

    这条布局里 `_MEIPASS` 就是 exe 目录（或没有），`--add-data` 的 `characters/` 直接躺在 exe
    旁边、**没有 `data/` 那一层**。若兜底依旧写死 `exe/data`，`resource_dir()` 会指向一个不存在
    的目录，缺省场景/角色/模型全落空——打包版打开一片空白，而这条兜底恰恰是专为它写的。
    故：`data/` 只在它确实存在时才用，否则退回 exe 同级。
    """
    exe_dir, set_meipass = fake_frozen
    (exe_dir / "characters").mkdir(parents=True)            # 素材与 exe 同级（没有 data/）
    set_meipass(exe_dir)
    assert paths_mod.resource_dir() == exe_dir
    assert (paths_mod.resource_dir() / "characters").is_dir(), "兜底必须指向素材真正所在处"

    set_meipass(None)                                       # 同一种布局，只是没有 _MEIPASS
    assert paths_mod.resource_dir() == exe_dir


def test_resource_dir_frozen_prefers_existing_data_dir(fake_frozen):
    """`data/` 存在时优先用它——两种形态都摆出来时不能选错（素材在 data/ 里，不在 exe 旁）。"""
    exe_dir, set_meipass = fake_frozen
    (exe_dir / "data" / "scenes").mkdir(parents=True)
    set_meipass(exe_dir)
    assert paths_mod.resource_dir() == exe_dir / "data"


def test_resource_dir_frozen_data_dir_is_relative_to_exe_not_cwd(fake_frozen, monkeypatch,
                                                                tmp_path: Path):
    """onefolder 的判据是 **exe 所在目录**，与当前工作目录无关。

    装到 `Program Files` 后被快捷方式启动时 cwd 可能是任何地方（甚至是只读的安装目录），
    这就是"相对 cwd"这条老路必须废除的原因。
    """
    exe_dir, set_meipass = fake_frozen
    (exe_dir / "data").mkdir()
    set_meipass(None)
    elsewhere = tmp_path / "随便一个 cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert paths_mod.resource_dir() == exe_dir / "data"


# ============================================================ user_dir：三分支
def test_user_dir_windows_uses_appdata(monkeypatch, tmp_path: Path, real_user_dir):
    """Windows：`%APPDATA%\\Ensemble-AI-Studio`。"""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert paths_mod.user_dir() == tmp_path / paths_mod.APP_DIR_NAME


def test_user_dir_windows_falls_back_to_home_when_appdata_missing(monkeypatch, fake_home,
                                                                  real_user_dir):
    """`%APPDATA%` 缺失/为空 → 回落 `~/AppData/Roaming`（服务账户、精简环境下确实会缺）。"""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("APPDATA", raising=False)
    expected = fake_home / "AppData" / "Roaming" / paths_mod.APP_DIR_NAME
    assert paths_mod.user_dir() == expected
    monkeypatch.setenv("APPDATA", "   ")          # 只有空白也算没设
    assert paths_mod.user_dir() == expected


def test_user_dir_macos_uses_application_support(monkeypatch, tmp_path: Path, fake_home,
                                                 real_user_dir):
    """macOS：`~/Library/Application Support/Ensemble-AI-Studio`（**不看** XDG 变量）。"""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert paths_mod.user_dir() == fake_home / "Library" / "Application Support" \
        / paths_mod.APP_DIR_NAME


def test_user_dir_linux_uses_xdg_config_home(monkeypatch, tmp_path: Path, real_user_dir):
    """其它平台：`$XDG_CONFIG_HOME/Ensemble-AI-Studio`。"""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert paths_mod.user_dir() == tmp_path / "xdg" / paths_mod.APP_DIR_NAME


def test_user_dir_linux_falls_back_to_dot_config(monkeypatch, fake_home, real_user_dir):
    """XDG 未设/为空 → 回落 `~/.config/Ensemble-AI-Studio`。"""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    expected = fake_home / ".config" / paths_mod.APP_DIR_NAME
    assert paths_mod.user_dir() == expected
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    assert paths_mod.user_dir() == expected


def test_user_dir_is_injectable_by_tests(monkeypatch, tmp_path: Path):
    """`user_dir()` 必须能被测试指到 tmp（conftest 的沙箱夹具就是这么做的）。

    这是"绝不写真实用户目录"这条铁律的技术前提：只要所有用户目录入口都经它，指到 tmp
    就等于把整条写入路径都关进了沙箱。
    """
    sandbox = tmp_path / "沙箱"
    monkeypatch.setattr(paths_mod, "user_dir", lambda: sandbox)
    assert paths_mod.user_dir() == sandbox
    assert paths_mod.characters_dir() == sandbox / "characters"
    assert paths_mod.runs_dir() == sandbox / "runs"


def test_user_dir_is_same_source_as_settings(monkeypatch, tmp_path: Path, real_user_dir):
    """`gui/settings.py::default_settings_path()` 与 `user_dir()` **同源**（不许两处各写一份）。

    钉法不是"读一眼代码"，而是把 `user_dir()` 指到别处，看 settings 跟不跟着走——各写一份的
    实现这一条必红。
    """
    from harness.gui import settings as settings_mod

    # 目录名与文件名是**同一个对象**（settings 从 paths import 的），不是各写一份同值字符串。
    assert settings_mod.APP_DIR_NAME is paths_mod.APP_DIR_NAME
    assert settings_mod.SETTINGS_FILE_NAME is paths_mod.SETTINGS_FILE_NAME
    assert _real_default_settings_path() == \
        paths_mod.user_dir() / paths_mod.SETTINGS_FILE_NAME

    moved = tmp_path / "换个地方"
    monkeypatch.setattr(paths_mod, "user_dir", lambda: moved)
    assert _real_default_settings_path() == moved / "settings.json"


# ============================================================ 各子目录
def test_subdirs_read_the_right_side(monkeypatch, tmp_path: Path, fake_bundle):
    """子目录口径（§1.1 的表）：写的一侧全在 `user_dir()` 下，**只有 templates 读内置只读素材**。"""
    sandbox = tmp_path / "user-data"
    monkeypatch.setattr(paths_mod, "user_dir", lambda: sandbox)
    assert paths_mod.characters_dir() == sandbox / "characters"
    assert paths_mod.scenes_dir() == sandbox / "scenes"
    assert paths_mod.config_dir() == sandbox / "config"
    assert paths_mod.runs_dir() == sandbox / "runs"
    assert paths_mod.templates_dir() == fake_bundle / "templates"


def test_materials_dir_dev_is_repo_app(monkeypatch):
    """开发态：缺省素材库 = 仓库里的 `app/`（= `resource_dir()`），与今天逐字节同址。

    §1.2 的"素材缺省"这一格：开发态界面读的就是仓库 `app/`（开发者自己的卡一直放那儿），
    把缺省素材挪到用户目录会把既有工作流整条打断。
    """
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert paths_mod.materials_dir() == paths_mod.resource_dir() == APP_DIR


def test_materials_dir_frozen_is_user_dir(fake_frozen, monkeypatch, tmp_path: Path):
    """冻结态：缺省素材库 = **用户数据目录**（启动时播种出的可编辑副本）。

    安装目录只读：缺省场景/卡/模型若指那儿，新建与导入都会撞权限，而播种出来的副本谁也读不到。
    """
    sandbox = tmp_path / "user-data"
    monkeypatch.setattr(paths_mod, "user_dir", lambda: sandbox)
    assert paths_mod.materials_dir() == sandbox
    assert paths_mod.materials_dir() != paths_mod.resource_dir()


# ============================================================ 旧目录兼容（改名）
def test_legacy_user_dirs_are_old_name_on_same_base(monkeypatch, tmp_path: Path,
                                                    real_user_dir, real_legacy_user_dirs):
    """旧用户目录 = **同一个平台根** + 改名前的目录名（只读兼容）。

    改名（§1.1：`文字创作agent` → `Ensemble-AI-Studio`）不能把老用户填过的设置一起丢掉，
    故旧落点要按同一套平台分支算得出来——若在别处另写一份平台逻辑，早晚与 `user_dir()` 分叉。
    """
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert paths_mod.legacy_user_dirs() == [
        tmp_path / name for name in paths_mod.LEGACY_APP_DIR_NAMES]
    assert "文字创作agent" in paths_mod.LEGACY_APP_DIR_NAMES, \
        "旧名必须真的在里面，否则兼容读等于没有"
    assert paths_mod.legacy_user_dirs()[0] != paths_mod.user_dir()


def test_legacy_user_dirs_follows_xdg_on_linux(monkeypatch, tmp_path: Path, real_user_dir,
                                               real_legacy_user_dirs):
    """非 Windows/macOS：旧目录同样落在 `$XDG_CONFIG_HOME` 下（三分支一套算法）。"""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert paths_mod.legacy_user_dirs() == [
        tmp_path / "xdg" / name for name in paths_mod.LEGACY_APP_DIR_NAMES]


def test_legacy_user_dirs_empty_in_tests_by_default():
    """测试默认**没有**旧目录（conftest 沙箱夹具把 `legacy_user_dirs()` 指成空表）。

    这条是"绝不写/读真实用户目录"的一半：本机真实存在 `%APPDATA%\\文字创作agent\\settings.json`，
    不置空的话用例会读到用户真实设置、结果随机器变。
    """
    assert paths_mod.legacy_user_dirs() == []


# ============================================================ ensure_user_dirs：播种
def test_ensure_user_dirs_creates_dirs(monkeypatch, tmp_path: Path, fake_bundle):
    """建目录：用户目录本身 + 它名下的那几个子目录（首次运行时一个都还不存在）。"""
    sandbox = tmp_path / "user-data"
    monkeypatch.setattr(paths_mod, "user_dir", lambda: sandbox)
    assert not sandbox.exists()
    returned = paths_mod.ensure_user_dirs()
    assert returned == sandbox
    assert sandbox.is_dir()
    for name in ("characters", "scenes", "config", "runs"):
        assert (sandbox / name).is_dir(), f"{name}/ 应当建好"


def test_ensure_user_dirs_seeds_builtin_materials(monkeypatch, tmp_path: Path, fake_bundle):
    """首次运行播种：内置 characters/scenes/config/templates **复制**进用户目录。"""
    sandbox = tmp_path / "user-data"
    monkeypatch.setattr(paths_mod, "user_dir", lambda: sandbox)
    paths_mod.ensure_user_dirs()
    assert (sandbox / "characters" / "甲.json").read_text(encoding="utf-8") \
        == '{"name": "甲"}'
    assert (sandbox / "characters" / "乙.json").is_file()
    assert (sandbox / "scenes" / "茶室.json").is_file()
    assert (sandbox / "config" / "models.yaml").is_file()
    assert (sandbox / "templates" / "角色卡模板.md").is_file()
    # 是**副本**不是软链/同一文件：改了用户那份，内置那份纹丝不动。
    (sandbox / "characters" / "甲.json").write_text('{"name": "改了"}', encoding="utf-8")
    assert (fake_bundle / "characters" / "甲.json").read_text(encoding="utf-8") \
        == '{"name": "甲"}'


def test_ensure_user_dirs_never_overwrites_existing(monkeypatch, tmp_path: Path, fake_bundle):
    """**已存在的文件一个都不覆盖**——用户改过的卡不能被内置版盖掉（升级/重启同理）。

    改一个字符再播一次，改过的内容必须还在；同时"新出现的内置文件"照常补上。
    """
    sandbox = tmp_path / "user-data"
    monkeypatch.setattr(paths_mod, "user_dir", lambda: sandbox)
    paths_mod.ensure_user_dirs()
    (sandbox / "characters" / "甲.json").write_text('{"name": "用户改过的甲"}',
                                                   encoding="utf-8")
    (fake_bundle / "characters" / "丙.json").write_text('{"name": "丙"}', encoding="utf-8")

    paths_mod.ensure_user_dirs()          # 再播一次

    assert (sandbox / "characters" / "甲.json").read_text(encoding="utf-8") \
        == '{"name": "用户改过的甲"}'
    assert (sandbox / "characters" / "丙.json").is_file(), "内置新增的文件照常补上"


def test_ensure_user_dirs_keeps_user_files_not_in_bundle(monkeypatch, tmp_path: Path,
                                                         fake_bundle):
    """用户自己建的文件/子目录绝不被清理：播种只做加法（不做镜像同步）。"""
    sandbox = tmp_path / "user-data"
    monkeypatch.setattr(paths_mod, "user_dir", lambda: sandbox)
    paths_mod.ensure_user_dirs()
    (sandbox / "scenes" / "我的场.json").write_text('{"name": "我的场"}', encoding="utf-8")
    (sandbox / "characters" / "我的库").mkdir()
    paths_mod.ensure_user_dirs()
    assert (sandbox / "scenes" / "我的场.json").is_file()
    assert (sandbox / "characters" / "我的库").is_dir()


def test_ensure_user_dirs_is_idempotent(monkeypatch, tmp_path: Path, fake_bundle):
    """播两次与播一次结果相同（幂等的"只做一次"由**不覆盖**保证，不需要标记文件）。"""
    sandbox = tmp_path / "user-data"
    monkeypatch.setattr(paths_mod, "user_dir", lambda: sandbox)
    paths_mod.ensure_user_dirs()
    first = sorted(p.relative_to(sandbox).as_posix()
                   for p in sandbox.rglob("*"))
    paths_mod.ensure_user_dirs()
    second = sorted(p.relative_to(sandbox).as_posix()
                    for p in sandbox.rglob("*"))
    assert first == second


def test_ensure_user_dirs_tolerates_missing_source_dirs(monkeypatch, tmp_path: Path):
    """内置素材缺目录（开发态确实没有 `app/templates/`）不报错——素材缺失是降级不是崩溃。"""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    monkeypatch.setattr(paths_mod, "resource_dir", lambda: bundle)
    sandbox = tmp_path / "user-data"
    monkeypatch.setattr(paths_mod, "user_dir", lambda: sandbox)
    assert paths_mod.ensure_user_dirs() == sandbox
    for name in ("characters", "scenes", "config", "runs"):
        assert (sandbox / name).is_dir()


def test_ensure_user_dirs_seed_false_only_creates(monkeypatch, tmp_path: Path, fake_bundle):
    """`seed=False` 只建目录、不复制（供不想播种的调用方用）。"""
    sandbox = tmp_path / "user-data"
    monkeypatch.setattr(paths_mod, "user_dir", lambda: sandbox)
    paths_mod.ensure_user_dirs(seed=False)
    assert (sandbox / "characters").is_dir()
    assert not (sandbox / "characters" / "甲.json").exists()


def test_ensure_user_dirs_does_not_copy_onto_itself(monkeypatch, tmp_path: Path):
    """用户目录与素材目录是同一个时**什么都不做**（开发态硬把 user_dir 指到 app/ 也不会
    自己覆盖自己、更不会无限复制）。"""
    same = tmp_path / "同一处"
    (same / "characters").mkdir(parents=True)
    (same / "characters" / "甲.json").write_text('{"name": "甲"}', encoding="utf-8")
    monkeypatch.setattr(paths_mod, "resource_dir", lambda: same)
    monkeypatch.setattr(paths_mod, "user_dir", lambda: same)
    paths_mod.ensure_user_dirs()
    assert (same / "characters" / "甲.json").read_text(encoding="utf-8") == '{"name": "甲"}'


# ============================================================ 调用点接线（§1.2）
def _stub_gui_app(monkeypatch) -> dict:
    """把 `gui/app.py::run()` 的 Qt 与窗口/worker 全换成替身，返回捕获字典。

    只留 `run()` 自己的解析逻辑在跑：这样"缺省素材/缺省 run_root 到底取哪儿"才看得清。
    """
    pytest.importorskip("PySide6")
    from harness.gui import app as gui_app
    import harness.gui.main_window as mw_mod
    import harness.gui.worker as worker_mod

    captured: dict = {}

    class _FakeQApp:
        def __init__(self, *a, **k) -> None:
            pass

        def setApplicationName(self, *a) -> None:      # noqa: N802
            pass

        def setProperty(self, *a) -> None:             # noqa: N802
            pass

        def exec(self) -> int:                         # noqa: N802
            return 0

    class _FakeWindow:
        def __init__(self, worker, cfg, settings=None) -> None:
            captured["cfg"] = cfg

        def show(self) -> None:
            pass

    class _FakeWorker:
        def start(self) -> None:
            pass

        def shutdown(self, *a) -> None:
            pass

        def wait(self, *a) -> None:
            pass

    monkeypatch.setattr("PySide6.QtWidgets.QApplication", _FakeQApp)
    monkeypatch.setattr(mw_mod, "MainWindow", _FakeWindow)
    monkeypatch.setattr(worker_mod, "SceneWorker", _FakeWorker)
    return captured


def _fresh_app_module(name: str = "harness.gui._probe_app"):
    """把 `gui/app.py` 的模块体**重新执行一遍**，返回新模块对象（不动已加载的那个）。

    为什么必须另开一路：`_DEFAULT_*` 是 **import 时**求值的常量，而 conftest 的沙箱夹具为了
    对齐沙箱会把 `_DEFAULT_RUN_ROOT` 猴补成 `paths.runs_dir()`——于是"断言模块属性等于
    `paths.runs_dir()`"两边是同一个被 patch 的值，app.py 里写死 `app/runs` 也照样绿（实测：
    把该常量改成 `Path("毫无疑问的垃圾目录")`，31 条 test_paths 一条不红）。同理，"按
    `is_frozen()` 分叉"的常量（materials_dir）也绝无可能被后置 monkeypatch 验到冻结分支。
    重新执行一遍模块体，拿到的才是"照当前环境真算一遍"的结果。

    用独立模块名（不进 `sys.modules`）执行，避免污染已加载的 `harness.gui.app`。
    """
    from harness.gui import app as gui_app

    spec = importlib.util.spec_from_file_location(name, gui_app.__file__)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)      # 相对 import（`from .. import paths`）靠 __package__ 解析
    return module


def test_gui_app_defaults_read_builtin_and_write_user_dir(user_data_sandbox):
    """`gui/app.py`：**读**缺省素材、**写**用户数据目录（runs_dir）。

    这是"打包后素材读得到、写不进安装目录"两件事的分界线：默认演示场景/角色/模型/竞价
    随包走（且与今天逐字节同址），而 runs（含转录与角色私有记忆）必须落用户目录。

    注：断言的是**已加载模块**的常量，而 conftest 的沙箱夹具会猴补 `_DEFAULT_RUN_ROOT`
    （见其 docstring），故本条**钉不住**"app.py 里到底写了什么"——那由下面
    `test_gui_app_defaults_are_computed_from_paths_*`（重新执行模块体）负责。
    """
    from harness.gui import app as gui_app

    resource = paths_mod.resource_dir()
    assert gui_app._DEFAULT_SCENE == resource / "scenes" / "贝克街221B.json"
    assert gui_app._DEFAULT_CHARACTERS == [
        resource / "characters" / "福尔摩斯.json",
        resource / "characters" / "华生.json",
    ]
    assert gui_app._DEFAULT_MODELS == resource / "config" / "models.yaml"
    assert gui_app._DEFAULT_BID == resource / "config" / "bid.demo.yaml"
    assert gui_app._DEFAULT_RUN_ROOT == paths_mod.runs_dir() == user_data_sandbox / "runs"


def test_gui_app_defaults_are_computed_from_paths_dev(monkeypatch):
    """**绕过夹具的 patch**核一遍 `gui/app.py` 的模块级表达式（开发态）。

    没有这一条，"缺省存档根真的搬出了安装目录"就是**假绿**：夹具把常量覆盖成沙箱值再拿它
    断言，等于自说自话。这里重新执行模块体，故 app.py 若退回 `_APP_DIR/"runs"`、
    `resource_dir()/"runs"`、或任何写死的路径，本条即红（前者落仓库、后者落只读安装目录，
    打包后一个是"升级就丢"、一个是"PermissionError"）。
    """
    monkeypatch.delattr(sys, "frozen", raising=False)
    fresh = _fresh_app_module()

    assert fresh._DEFAULT_RUN_ROOT == paths_mod.runs_dir()
    assert APP_DIR not in fresh._DEFAULT_RUN_ROOT.parents, "存档绝不落仓库/安装目录"
    # 开发态素材 = 仓库 `app/`（铁律：开发态逐字节不变）。
    assert fresh._DEFAULT_SCENE == paths_mod.resource_dir() / "scenes" / "贝克街221B.json"
    assert fresh._DEFAULT_CHARACTERS == [
        paths_mod.resource_dir() / "characters" / "福尔摩斯.json",
        paths_mod.resource_dir() / "characters" / "华生.json",
    ]
    assert fresh._DEFAULT_MODELS == paths_mod.resource_dir() / "config" / "models.yaml"
    assert fresh._DEFAULT_BID == paths_mod.resource_dir() / "config" / "bid.demo.yaml"


def test_gui_app_defaults_follow_materials_dir_when_frozen(fake_frozen, monkeypatch,
                                                           tmp_path: Path):
    """冻结态：缺省素材 = **用户数据目录**里的可编辑副本（播种出来的那份）。

    这不是"顺手改改"：GUI 的「角色库/场景库」目录取自当前场景/当前卡所在目录，新建角色、
    导入模板、引擎装卡都往那儿写。缺省若还指安装目录，打包后这些写操作会撞 Program Files
    的只读权限（或 onefile 的临时解压目录，重启即丢），而播种出的副本没有任何界面读得到。
    """
    sandbox = tmp_path / "user-data"
    monkeypatch.setattr(paths_mod, "user_dir", lambda: sandbox)
    fresh = _fresh_app_module()

    assert fresh._DEFAULT_SCENE == sandbox / "scenes" / "贝克街221B.json"
    assert fresh._DEFAULT_CHARACTERS == [
        sandbox / "characters" / "福尔摩斯.json",
        sandbox / "characters" / "华生.json",
    ]
    assert fresh._DEFAULT_MODELS == sandbox / "config" / "models.yaml"
    assert fresh._DEFAULT_BID == sandbox / "config" / "bid.demo.yaml"
    assert fresh._DEFAULT_RUN_ROOT == sandbox / "runs"


def test_gui_app_run_seeds_user_dirs_then_uses_them(monkeypatch, user_data_sandbox):
    """`app.run()`：**先 `ensure_user_dirs()`**（首次运行播种），再做其余解析。

    顺序不是细节：用户第一次双击时 `%APPDATA%` 里就该有那几份可编辑的副本（内置素材是
    随包走的只读底本，用户的那份得先播出来才有得改）；缺省 run_root 亦走 `paths.runs_dir()`。

    这里直接用 conftest 的沙箱用户目录（`user_data_sandbox`）：`gui/app.py` 的
    `_DEFAULT_RUN_ROOT` 是 import 时从用户目录算好的常量，只有跟同一个沙箱对齐才验得到
    "缺省真的落在用户目录里"。
    """
    from harness.gui import app as gui_app

    sandbox = user_data_sandbox

    calls: list[int] = []
    real_ensure = paths_mod.ensure_user_dirs

    def _spy_ensure(seed: bool = True) -> Path:
        calls.append(1)
        return real_ensure(seed=seed)

    monkeypatch.setattr(paths_mod, "ensure_user_dirs", _spy_ensure)
    captured = _stub_gui_app(monkeypatch)

    assert gui_app.run(["--stub"]) == 0
    assert calls, "启动时必须先播种/建用户目录"
    cfg = captured["cfg"]
    assert cfg.run_root == paths_mod.runs_dir() == sandbox / "runs"
    assert cfg.scene == paths_mod.resource_dir() / "scenes" / "贝克街221B.json"
    assert cfg.characters == [
        paths_mod.resource_dir() / "characters" / "福尔摩斯.json",
        paths_mod.resource_dir() / "characters" / "华生.json",
    ]
    assert (sandbox / "config" / "models.yaml").is_file(), "播种真的落进了用户目录"
    assert (sandbox / "scenes" / "贝克街221B.json").is_file()


def test_frozen_startup_seeds_the_files_the_defaults_point_at(fake_frozen, monkeypatch,
                                                             tmp_path: Path):
    """冻结态：缺省场景/卡指向的文件**必须真的被播种出来**（否则打包版一开就说文件不存在）。

    这是"缺省值落在哪"与"播种播了啥"两处之间的接缝：`materials_dir()` 把缺省素材指到用户
    目录，靠的正是 `ensure_user_dirs()` 把 `resource_dir()` 下的同名文件（贝克街221B.json、福尔摩斯.json…）
    复制过去。两处若各改各的，开发态一切正常、打包版点开就报错——而这条接缝没有任何单点
    测试看得见（常量测常量、播种测播种，各绿各的）。

    这里让 `resource_dir()` 指向**真的仓库素材**（只读），用户目录照旧在 tmp。
    """
    sandbox = tmp_path / "user-data"
    monkeypatch.setattr(paths_mod, "user_dir", lambda: sandbox)
    monkeypatch.setattr(paths_mod, "resource_dir", lambda: APP_DIR)

    fresh = _fresh_app_module()            # 冻结态那一份缺省值
    paths_mod.ensure_user_dirs()           # 启动流程里做的播种（§1.2/gui/app.py::run）

    assert fresh._DEFAULT_SCENE.is_file(), f"缺省场景没被播出来：{fresh._DEFAULT_SCENE}"
    for card in fresh._DEFAULT_CHARACTERS:
        assert card.is_file(), f"缺省角色卡没被播出来：{card}"
    assert fresh._DEFAULT_MODELS.is_file() and fresh._DEFAULT_BID.is_file()
    # 缺省素材的所在目录就是界面的「角色库/场景库」目录，也是 CLI 导入的缺省落点。
    from harness.tools import import_cards
    assert fresh._DEFAULT_CHARACTERS[0].parent == import_cards.default_characters_dir()
    assert fresh._DEFAULT_SCENE.parent == import_cards.default_scenes_dir()


def test_runner_defaults_match_pre_change_resolution(monkeypatch, tmp_path: Path):
    """`runner.py` 的缺省素材：**改动前后的解析结果相同**（对拍）。

    改动前是相对 cwd 的 `Path("scenes/贝克街221B.json")`；改动后是 `resource_dir()` 下的绝对路径。
    两者在"从 `app/` 里跑"这个既有场景下必须**指向同一个文件**——这就是"开发态行为逐字节
    不变"这条约束在 runner 上的具体样子。
    """
    from harness import runner as runner_mod

    monkeypatch.chdir(APP_DIR)                    # 既有用法：在 app/ 里跑 CLI
    assert runner_mod._DEFAULT_SCENE == paths_mod.resource_dir() / "scenes" / "贝克街221B.json"
    assert runner_mod._DEFAULT_SCENE.resolve() == Path("scenes/贝克街221B.json").resolve()
    assert runner_mod._DEFAULT_CHARACTERS == [
        paths_mod.resource_dir() / "characters" / "福尔摩斯.json",
        paths_mod.resource_dir() / "characters" / "华生.json",
    ]
    assert [p.resolve() for p in runner_mod._DEFAULT_CHARACTERS] == [
        p.resolve() for p in (Path("characters/福尔摩斯.json"), Path("characters/华生.json"))]
    assert runner_mod._DEFAULT_MODELS == paths_mod.resource_dir() / "config" / "models.yaml"
    assert runner_mod._DEFAULT_BID_DEMO == \
        paths_mod.resource_dir() / "config" / "bid.demo.yaml"


def test_runner_default_run_root_is_user_runs_dir(monkeypatch, tmp_path: Path):
    """`runner._default_run_root`：缺省落**用户数据目录**下的 `runs/`，不再相对 cwd。

    demo 仍每场唯一（`runs/demo-<8位>`，避免多次运行共用同一份转录而串场）；显式
    `--run-root` 照旧最优先。
    """
    from harness import runner as runner_mod

    sandbox = tmp_path / "user-data"
    monkeypatch.setattr(paths_mod, "user_dir", lambda: sandbox)
    monkeypatch.chdir(tmp_path)                   # cwd 与用户目录无关

    ns = argparse.Namespace(run_root=None)
    assert runner_mod._default_run_root(ns, demo=False) == sandbox / "runs"
    demo = runner_mod._default_run_root(ns, demo=True)
    assert demo.parent == sandbox / "runs"
    assert re.fullmatch(r"demo-[0-9a-f]{8}", demo.name)
    assert demo != runner_mod._default_run_root(ns, demo=True), "每场唯一"
    explicit = tmp_path / "显式"
    assert runner_mod._default_run_root(argparse.Namespace(run_root=explicit),
                                        demo=True) == explicit


def test_import_cards_defaults_are_the_library_the_gui_reads(monkeypatch, tmp_path: Path):
    """缺省导入目标 = `materials_dir()` 下的 characters/scenes（= **界面正在读的那个库**）。

    导入是**写**，两端各有正确落点：冻结态必须落用户数据目录（安装目录只读，写那儿必失败，
    且用户导进来的卡不该混进安装包）；开发态界面读的就是仓库 `app/`，导入就落那儿——否则
    会退化成"导入成功、界面里找不到这张卡"（`templates/使用说明.md` 承诺的恰恰是导完即见于库）。
    """
    from harness.tools import import_cards

    sandbox = tmp_path / "user-data"
    monkeypatch.setattr(paths_mod, "user_dir", lambda: sandbox)

    # 开发态：与改造前逐字节同址（`app/characters`、`app/scenes`）。
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert import_cards.default_characters_dir() == APP_DIR / "characters"
    assert import_cards.default_scenes_dir() == APP_DIR / "scenes"

    # 冻结态：改走用户数据目录。
    monkeypatch.setattr(paths_mod, "is_frozen", lambda: True)
    assert import_cards.default_characters_dir() == sandbox / "characters"
    assert import_cards.default_scenes_dir() == sandbox / "scenes"
    assert import_cards.default_characters_dir().is_absolute()


def test_import_defaults_match_gui_library_dir(monkeypatch, fake_frozen, tmp_path: Path):
    """**读写同址**：CLI 导入缺省目录 == GUI 缺省卡/场景所在目录（= 界面的「角色库/场景库」）。

    这条是"导入成功的卡界面里永远看不见"那类病的总护栏，两种态各验一遍。GUI 的库目录 =
    `MainWindow._characters_dir()/._scenes_dir()` = 当前卡/当前场景的所在目录，故只要求
    "缺省卡/场景的父目录 == 导入缺省目录"就够了（不必去碰窗口内部方法）。
    """
    from harness.tools import import_cards

    sandbox = tmp_path / "user-data"
    monkeypatch.setattr(paths_mod, "user_dir", lambda: sandbox)

    frozen = _fresh_app_module()          # fake_frozen 生效 → 冻结态那一份常量
    assert frozen._DEFAULT_CHARACTERS[0].parent == \
        import_cards.default_characters_dir() == sandbox / "characters"
    assert frozen._DEFAULT_SCENE.parent == \
        import_cards.default_scenes_dir() == sandbox / "scenes"


def test_import_cards_cli_writes_into_the_library(monkeypatch, tmp_path: Path, capsys):
    """走一遍 CLI（不给 `--characters-dir`）：解析出来的落点就是上面那个库目录。

    只 spy `import_template_file` 收实参，不真写盘——这里要钉的是"缺省目录有没有接上
    paths"，不是模板解析本身（那是 `test_template_import.py` 的地盘）。
    """
    from harness.tools import import_cards

    sandbox = tmp_path / "user-data"
    monkeypatch.setattr(paths_mod, "user_dir", lambda: sandbox)

    seen: list[dict] = []

    class _Result:
        kind = "character"
        name = "甲"
        path = None
        empty_fields: list = []
        warnings: list = []
        card_or_scene = {}

    def _fake_import(path, **kwargs):
        seen.append(dict(kwargs))
        return _Result()

    monkeypatch.setattr(import_cards, "import_template_file", _fake_import)
    src = tmp_path / "甲.md"
    src.write_text("# 甲\n", encoding="utf-8")

    # 开发态：落仓库素材目录（与改造前一致）。
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert import_cards.main([str(src)]) == 0
    assert seen[-1]["characters_dir"] == APP_DIR / "characters"
    assert seen[-1]["scenes_dir"] == APP_DIR / "scenes"

    # 冻结态：落用户数据目录。
    monkeypatch.setattr(paths_mod, "is_frozen", lambda: True)
    assert import_cards.main([str(src)]) == 0
    assert seen[-1]["characters_dir"] == sandbox / "characters"
    assert seen[-1]["scenes_dir"] == sandbox / "scenes"


def test_worker_start_scene_run_root_defaults_to_user_runs(monkeypatch, tmp_path: Path):
    """`SceneWorker.start_scene` 的 `run_root` 缺省 = `paths.runs_dir()`（每场仍唯一子目录）。"""
    from harness.gui import worker as worker_mod

    sandbox = tmp_path / "user-data"
    monkeypatch.setattr(paths_mod, "user_dir", lambda: sandbox)
    scene_p = tmp_path / "茶室.json"
    scene_p.write_text(json.dumps({"name": "茶室"}, ensure_ascii=False), encoding="utf-8")
    card_p = tmp_path / "甲.json"
    card_p.write_text(json.dumps({"name": "甲"}, ensure_ascii=False), encoding="utf-8")
    models_p = tmp_path / "models.yaml"
    models_p.write_text("think: {}\n", encoding="utf-8")
    bid_p = tmp_path / "bid.yaml"
    bid_p.write_text("interruption_threshold: 0.0\n", encoding="utf-8")

    worker = worker_mod.SceneWorker()               # 只构造不 start
    monkeypatch.setattr(worker_mod.SceneWorker, "_submit",
                        lambda self, coro: coro.close(), raising=True)
    worker.start_scene(scene_p, [card_p], models_p, live=False, bid=bid_p,
                       opening="静场。", run_root=None)
    assert worker._cfg["run_root"] == sandbox / "runs"
    assert worker._cfg["scene"] == scene_p


def _minimal_engine_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    """一副最小可构造的（场景、角色卡、模型）三件套——只为把引擎构造起来。"""
    scene_p = tmp_path / "茶室.json"
    scene_p.write_text(json.dumps({"name": "茶室", "participants": ["甲"]},
                                  ensure_ascii=False), encoding="utf-8")
    card_p = tmp_path / "甲.json"
    card_p.write_text(json.dumps({"name": "甲", "personality": {"描述": "甲"}},
                                 ensure_ascii=False), encoding="utf-8")
    models_p = tmp_path / "models.yaml"
    models_p.write_text(
        "think:\n  backend: stub\n  model: stub\n  params: {}\n"
        "speak:\n  backend: stub\n  model: stub\n  params: {}\n", encoding="utf-8")
    return scene_p, card_p, models_p


def test_engine_run_root_defaults_to_user_runs(monkeypatch, tmp_path: Path):
    """`SceneEngine.run_root` 缺省 = `paths.runs_dir()`（不给就落用户数据目录，不是 cwd）。"""
    from harness import engine as engine_mod

    sandbox = tmp_path / "user-data"
    monkeypatch.setattr(paths_mod, "user_dir", lambda: sandbox)
    scene_p, card_p, models_p = _minimal_engine_inputs(tmp_path)
    eng = engine_mod.SceneEngine(scene_p, [card_p], models_p)
    assert eng.run_root == sandbox / "runs"


def test_engine_explicit_run_root_still_wins(tmp_path: Path):
    """显式给的 run_root 照旧原样用（缺省只补 None，不改任何人传进来的值）。"""
    from harness import engine as engine_mod

    explicit = tmp_path / "本场"
    scene_p, card_p, models_p = _minimal_engine_inputs(tmp_path)
    eng = engine_mod.SceneEngine(scene_p, [card_p], models_p, run_root=explicit)
    assert eng.run_root == explicit
