"""把填好的模板 Markdown 导进素材库（角色卡 → app/characters/，场景卡 → app/scenes/，
信息库 → libraries/）。

用法（须在 app/ 下、或 `python -m` 带上 app/src 到 PYTHONPATH）：

    python -m harness.tools.import_cards ../templates/填好的角色卡.md
    python -m harness.tools.import_cards a.md b.md --dry-run
    python -m harness.tools.import_cards 小馆.md --name 深夜小馆.json --force

逐个文件打中文报告：识别出的类型、名字、落盘路径（试运行则写「（试运行，未写盘）」）、
模板里没填而已用默认值的字段、以及解析警告。**解析失败只说人话**：
`文件名 → 第 N 行附近 字段【X】：消息`（照《使用说明》「照着改那一个字段再导入即可」），
并继续处理剩下的文件；退出码 0 当且仅当每个文件都成功（含试运行）。

目录缺省（打包方案 §1.2，不再相对 cwd）：
  · 角色卡/场景卡 → `paths.materials_dir()` 下的 characters/ scenes/，即**界面正在读的那个
    库**：冻结态是用户数据目录（安装目录只读、升级安装包会覆盖），开发态是仓库里的
    `app/`（缺省卡所在处，与改造前逐字节同址——导入的卡立即出现在应用库里）；
  · 信息库根 → `paths.materials_dir()/libraries`（开发态 = 仓库里的 `app/libraries`，
    冻结态 = 用户数据目录）：它是**写**路径，不能留在安装目录里。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .. import paths as paths_mod
from ..template_import import (
    ImportResult, LibraryTemplate, TemplateError, import_template_file)


def default_libraries_dir() -> Path:
    """信息库根缺省 = `paths.materials_dir()/libraries`（与 GUI、runner 同一口径）。

    开发态 = 仓库里的 `app/libraries`；冻结态 = 用户数据目录。与上面两个缺省同理，
    **做成函数而不是模块常量**：常量在 import 时就把真实路径焊死，测试的沙箱夹具
    拦不住（那正是"测试写进真实用户目录"的成因）。
    """
    return paths_mod.materials_dir() / "libraries"


def default_characters_dir() -> Path:
    """角色卡目录缺省 = `paths.materials_dir()/characters`，即**界面正在读的那个库**。

    导入是**写**，而写在两个态下各有一个正确落点：
      · 冻结态（装到 `Program Files` 后）：安装目录只读、且升级安装包会覆盖，用户导进来的卡
        只能落 `%APPDATA%\\Ensemble-AI-Studio\\characters`——也正是界面「角色库」读的那处；
      · 开发态：界面读的是仓库里的 `app/characters`（缺省卡所在目录），导入就落那儿。
        §1.2 的底盘是"写不进只读安装目录"，开发态不存在这个问题；而把它硬挪到 %APPDATA%
        会让**界面里看不见刚导入的卡**（`templates/使用说明.md` 第 23/35 行承诺的正是"导入后
        出现在应用的库里"，用户日常就这么用），等于把既有工作流打断。故按冻结与否分叉。

    **是函数不是常量**：常量在 import 时就算死，会把真实用户目录焊在模块上——测试再怎么
    隔离也拦不住它（这不是理论风险，而是"测试写进真实 %APPDATA%"的直接成因）。
    """
    return paths_mod.materials_dir() / "characters"


def default_scenes_dir() -> Path:
    """场景目录缺省 = `paths.materials_dir()/scenes`（理由同 `default_characters_dir`）。"""
    return paths_mod.materials_dir() / "scenes"


_KIND_LABELS = {"character": "角色卡", "scene": "场景卡", "library": "信息库"}


def format_error(path: Path, exc: TemplateError) -> str:
    """`文件名 → 第 N 行附近 字段【X】：消息`（行号/字段名未知时省略那一段）。"""
    where: list[str] = []
    if exc.line:
        where.append(f"第 {exc.line} 行附近")
    if exc.field:
        where.append(f"字段【{exc.field}】")
    prefix = " ".join(where)
    if prefix:
        return f"{path} → {prefix}：{exc.message}"
    return f"{path} → {exc.message}"


def format_report(path: Path, result: ImportResult) -> str:
    """一次成功导入的中文报告（类型/名字/落盘位置 + 订阅 + 未填字段 + 警告）。"""
    kind = _KIND_LABELS.get(result.kind, result.kind)
    lines = [f"√ {kind}《{result.name}》（{path}）"]
    lines.append("  目标位置：（试运行，未写盘）" if result.path is None
                 else f"  已写入：{result.path}")
    if isinstance(result.card_or_scene, LibraryTemplate) \
            and result.card_or_scene.subscriptions:
        lines.append("  订阅：" + "、".join(result.card_or_scene.subscriptions))
    if result.empty_fields:
        lines.append("  以下字段模板里没填，已用默认值："
                     + "、".join(result.empty_fields))
    for warning in result.warnings:
        lines.append(f"  提示：{warning}")
    return "\n".join(lines)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="python -m harness.tools.import_cards",
        description="把按 templates/*.md 填好的 Markdown 导入角色/场景/信息库。")
    ap.add_argument("files", nargs="+", type=Path, help="填好的模板 .md（可多个）")
    ap.add_argument("--characters-dir", type=Path, default=None,
                    help=f"角色卡目录；缺省 {default_characters_dir()}")
    ap.add_argument("--scenes-dir", type=Path, default=None,
                    help=f"场景目录；缺省 {default_scenes_dir()}")
    ap.add_argument("--libraries-dir", type=Path, default=None,
                    help=f"信息库根；缺省 {default_libraries_dir()}")
    ap.add_argument("--name", default=None,
                    help="场景文件名（含 .json）；缺省按场景名生成安全 slug")
    ap.add_argument("--dry-run", action="store_true",
                    help="只解析与校验，不写盘")
    ap.add_argument("--force", action="store_true",
                    help="目标文件/库已存在时覆盖（缺省会拒绝并报错）")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None, *, out=None) -> int:
    """导一组文件；返回退出码（0 = 每个文件都成功）。`out` 可注入（缺省 stdout）。"""
    args = _parse_args(argv)
    stream = out if out is not None else sys.stdout
    characters_dir = args.characters_dir or default_characters_dir()
    scenes_dir = args.scenes_dir or default_scenes_dir()
    libraries_dir = args.libraries_dir or default_libraries_dir()

    ok = True
    for path in args.files:
        try:
            result = import_template_file(
                path, characters_dir=characters_dir, scenes_dir=scenes_dir,
                libraries_dir=libraries_dir,
                filename=args.name, dry_run=args.dry_run, overwrite=args.force)
        except TemplateError as exc:
            ok = False
            print(format_error(path, exc), file=stream)
            continue
        print(format_report(path, result), file=stream)
    return 0 if ok else 1


if __name__ == "__main__":                      # pragma: no cover - 手动跑
    raise SystemExit(main())
