"""把填好的模板 Markdown 导进素材库（角色卡 → app/characters/，场景卡 → app/scenes/）。

用法（须在 app/ 下、或 `python -m` 带上 app/src 到 PYTHONPATH）：

    python -m harness.tools.import_cards ../templates/填好的角色卡.md
    python -m harness.tools.import_cards a.md b.md --dry-run
    python -m harness.tools.import_cards 小馆.md --name 深夜小馆.json --force

逐个文件打中文报告：识别出的类型、名字、落盘路径（试运行则写「（试运行，未写盘）」）、
模板里没填而已用默认值的字段、以及解析警告。**解析失败只说人话**：
`文件名 → 第 N 行附近 字段【X】：消息`（照《使用说明》「照着改那一个字段再导入即可」），
并继续处理剩下的文件；退出码 0 当且仅当每个文件都成功（含试运行）。

目录缺省是**包所在的 app/** 下的 characters / scenes（与 gui/app.py 同一口径：按
`__file__` 解析，故在任意 cwd 下、双击或模块启动都命中同一个素材库）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..template_import import ImportResult, TemplateError, import_template_file

#: app/ 根（characters/scenes 所在目录）——与 gui/app.py 同一算法，任意 cwd 都命中。
_APP_DIR = Path(__file__).resolve().parents[3]
DEFAULT_CHARACTERS_DIR = _APP_DIR / "characters"
DEFAULT_SCENES_DIR = _APP_DIR / "scenes"

_KIND_LABELS = {"character": "角色卡", "scene": "场景卡"}


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
    """一次成功导入的中文报告（类型/名字/落盘位置 + 未填字段 + 警告）。"""
    kind = _KIND_LABELS.get(result.kind, result.kind)
    lines = [f"√ {kind}《{result.name}》（{path}）"]
    lines.append("  目标位置：（试运行，未写盘）" if result.path is None
                 else f"  已写入：{result.path}")
    if result.empty_fields:
        lines.append("  以下字段模板里没填，已用默认值："
                     + "、".join(result.empty_fields))
    for warning in result.warnings:
        lines.append(f"  提示：{warning}")
    return "\n".join(lines)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="python -m harness.tools.import_cards",
        description="把按 templates/*.md 填好的 Markdown 导入角色/场景库。")
    ap.add_argument("files", nargs="+", type=Path, help="填好的模板 .md（可多个）")
    ap.add_argument("--characters-dir", type=Path, default=None,
                    help=f"角色卡目录；缺省 {DEFAULT_CHARACTERS_DIR}")
    ap.add_argument("--scenes-dir", type=Path, default=None,
                    help=f"场景目录；缺省 {DEFAULT_SCENES_DIR}")
    ap.add_argument("--name", default=None,
                    help="场景文件名（含 .json）；缺省按场景名生成安全 slug")
    ap.add_argument("--dry-run", action="store_true",
                    help="只解析与校验，不写盘")
    ap.add_argument("--force", action="store_true",
                    help="目标文件已存在时覆盖（缺省会拒绝并报错）")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None, *, out=None) -> int:
    """导一组文件；返回退出码（0 = 每个文件都成功）。`out` 可注入（缺省 stdout）。"""
    args = _parse_args(argv)
    stream = out if out is not None else sys.stdout
    characters_dir = args.characters_dir or DEFAULT_CHARACTERS_DIR
    scenes_dir = args.scenes_dir or DEFAULT_SCENES_DIR

    ok = True
    for path in args.files:
        try:
            result = import_template_file(
                path, characters_dir=characters_dir, scenes_dir=scenes_dir,
                filename=args.name, dry_run=args.dry_run, overwrite=args.force)
        except TemplateError as exc:
            ok = False
            print(format_error(path, exc), file=stream)
            continue
        print(format_report(path, result), file=stream)
    return 0 if ok else 1


if __name__ == "__main__":                      # pragma: no cover - 手动跑
    raise SystemExit(main())
