"""用户设置持久化（设计文档 §2.1① 设置 + §6 设置与用户数据）。

职责只有两件：把「模型 api 配置 / 配色 / 语言 / 自动保存周期」装进一个纯数据类
AppSettings，以及把它读写到「用户数据目录」下的 settings.json（与仓库解耦，
随系统用户走）。

本模块**不依赖 PySide6**：界面层（main_window/library）按需 import 即可，纯 CLI
环境也能直接调用与测试。所有读盘操作对「缺失 / 损坏 / 越权 / 非字典」一律回落
默认值，绝不抛异常——设置坏了不该让软件起不来。

校验策略（§2.1① 的枚举）：
  - 主题 theme ∈ 默认/深色/白色/深蓝
  - 语言 language ∈ zh-Hans/zh-Hant/en/fr/de/ja/ko
  - 自动保存周期 autosave_every ∈ 5/20/50/100（轮）
  - 场景推进活跃度 narrate_activity ∈ 0.2/0.5/0.8/1.0（§6.2「少/中/多/极多」）
文件里某一字段非法时**只回落该字段**的默认值，其余字段照旧保留。
"""
from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

# 用户数据目录名（Windows: %APPDATA%/<此名>/settings.json；其余: ~/.config/<此名>/…）
APP_DIR_NAME = "Ensemble-AI-Studio"
SETTINGS_FILE_NAME = "settings.json"

THEMES: tuple[str, ...] = ("默认", "深色", "白色", "深蓝")
LANGUAGES: tuple[str, ...] = ("zh-Hans", "zh-Hant", "en", "fr", "de", "ja", "ko")
AUTOSAVE_EVERY: tuple[int, ...] = (5, 20, 50, 100)

#: 场景推进活跃度（§6.2）的四档数值——**数值的唯一出处**。界面的「少/中/多/极多」
#: 四档标签挂在这四个值上（见 gui/main_window.NARRATE_ACTIVITY_LEVELS），校验也用它。
NARRATE_ACTIVITIES: tuple[float, ...] = (0.2, 0.5, 0.8, 1.0)

DEFAULT_THEME = "默认"
DEFAULT_LANGUAGE = "zh-Hans"
DEFAULT_AUTOSAVE_EVERY = 20
DEFAULT_NARRATE_ACTIVITY = 0.5


def _as_text(value: Any) -> str:
    """只认字符串；其余类型（含 None/数字/列表）回落空串。"""
    return value if isinstance(value, str) else ""


def _as_autosave(value: Any) -> int:
    """只认 5/20/50/100；数字串可容错转换，其余回落默认。**永不抛**。

    `str.isdigit()` 对「²」「①」「⁰」这类上标/圆圈数字也为真，但 `int()` 会抛
    ValueError——故转换必须包在 try 里，脏设置文件不能把异常带出 load()。
    """
    if isinstance(value, bool):
        return DEFAULT_AUTOSAVE_EVERY
    if isinstance(value, int):
        n = value
    elif isinstance(value, str):
        text = value.strip().lstrip("+")
        if not text.isdigit():
            return DEFAULT_AUTOSAVE_EVERY
        try:
            n = int(text)
        except (TypeError, ValueError):     # isdigit 为真但 int 不认（²/①/⁰…）
            return DEFAULT_AUTOSAVE_EVERY
    else:
        return DEFAULT_AUTOSAVE_EVERY
    return n if n in AUTOSAVE_EVERY else DEFAULT_AUTOSAVE_EVERY


def snap_narrate_activity(value: Any) -> float:
    """任意输入 → 四档之一（§6.2 的活跃度校验，**唯一口径**）。**永不抛**。

    规则（按需求「只认四档，其余吸附到最近档」）：
      · 数字（int/float）或数字串 → 吸附到 `NARRATE_ACTIVITIES` 里最近的一档；越界夹
        到端点档（-3 → 0.2、7.5 → 1.0），并列（如 0.35）取较小档（确定性）。
      · NaN/±inf、bool（虽是 int 子类但不作数字用）、非数字串、其它类型 → 回落 0.5。
    """
    if isinstance(value, bool):
        return DEFAULT_NARRATE_ACTIVITY
    if isinstance(value, (int, float)):
        num = float(value)
    elif isinstance(value, str):
        try:
            num = float(value.strip())
        except ValueError:                  # 空串 / "经常" / "0.5.5" / "²" …
            return DEFAULT_NARRATE_ACTIVITY
    else:
        return DEFAULT_NARRATE_ACTIVITY
    if not math.isfinite(num):
        return DEFAULT_NARRATE_ACTIVITY
    for allowed in NARRATE_ACTIVITIES:
        if abs(num - allowed) < 1e-9:       # 已在档上：原样返回，免浮点尾巴
            return allowed
    return min(NARRATE_ACTIVITIES, key=lambda a: abs(a - num))


@dataclass
class AppSettings:
    """一份用户设置（纯数据，无 IO）。"""

    api_base_url: str = ""          # 例如 https://api.deepseek.com
    api_key: str = ""
    model_name: str = ""            # 例如 deepseek-v4-flash
    theme: str = DEFAULT_THEME      # 默认/深色/白色/深蓝
    language: str = DEFAULT_LANGUAGE    # zh-Hans/zh-Hant/en/fr/de/ja/ko
    autosave_every: int = DEFAULT_AUTOSAVE_EVERY   # 轮：5/20/50/100
    #: 场景推进活跃度（§6.2）：0.2/0.5/0.8/1.0 = 少/中/多/极多；越高越常主动插叙。
    narrate_activity: float = DEFAULT_NARRATE_ACTIVITY

    def to_dict(self) -> dict[str, Any]:
        """转为可 JSON 序列化的普通字典（快照，改它不影响本对象）。"""
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, d: Any) -> "AppSettings":
        """从任意对象尽力还原；非法/缺失字段各自回落默认，永不抛异常。"""
        if not isinstance(d, dict):
            return cls()
        return cls(
            api_base_url=_as_text(d.get("api_base_url", "")),
            api_key=_as_text(d.get("api_key", "")),
            model_name=_as_text(d.get("model_name", "")),
            theme=d.get("theme", DEFAULT_THEME)
            if d.get("theme") in THEMES else DEFAULT_THEME,
            language=d.get("language", DEFAULT_LANGUAGE)
            if d.get("language") in LANGUAGES else DEFAULT_LANGUAGE,
            autosave_every=_as_autosave(d.get("autosave_every", DEFAULT_AUTOSAVE_EVERY)),
            narrate_activity=snap_narrate_activity(
                d.get("narrate_activity", DEFAULT_NARRATE_ACTIVITY)),
        )

    def as_dict(self) -> dict[str, Any]:  # 兼容别名
        return self.to_dict()


def default_settings_path() -> Path:
    """默认设置文件：用户数据目录 + 固定子目录（跨平台，绝不在仓库内）。"""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or ""
        root = Path(base) if base.strip() else Path.home() / "AppData" / "Roaming"
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or ""
        root = Path(base) if base.strip() else Path.home() / ".config"
    return root / APP_DIR_NAME / SETTINGS_FILE_NAME


class SettingsStore:
    """settings.json 的读写门面；任何读失败都回落默认值。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path: Path = Path(path) if path is not None else default_settings_path()

    # ------------------------------------------------------------- 读
    def load(self) -> AppSettings:
        """读取设置；文件缺失/损坏/非法一律返回默认值，不抛异常也不写盘。

        **from_dict 也在 try 内**：设置文件是用户可手改的文本，任何字段的转换都可能
        在坏输入上抛（如 `{"autosave_every": "²"}`）——而 load() 是在 app.py 里、
        QApplication 之前调用的，抛出去直接把软件挡在启动门外。故这里兜底为
        「绝不抛」：读盘、解析、还原三步的任一异常都回落整份默认值。
        """
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
            return AppSettings.from_dict(data)
        except Exception:  # noqa: BLE001 - 脏设置文件绝不能把软件挡在启动门外
            # 缺失、无权限、是目录、编码错、JSON 语法错、字段转换异常 → 全部回落默认
            return AppSettings()

    # ------------------------------------------------------------- 写
    def save(self, s: AppSettings) -> Path:
        """写盘（自动建父目录，UTF-8 美化 JSON），返回写入路径。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(s.to_dict(), ensure_ascii=False, indent=2) + "\n"
        self.path.write_text(text, encoding="utf-8")
        return self.path

    # ------------------------------------------------------------- 改
    def update(self, **kwargs: Any) -> AppSettings:
        """读 → 覆盖已知字段 → 写 → 返回新设置；未知键忽略，非法枚举回落默认。"""
        current = self.load()
        known = {f.name for f in fields(AppSettings)}
        merged = current.to_dict()
        merged.update({k: v for k, v in kwargs.items() if k in known})
        # 仍走 from_dict：单一入口做枚举校验，避免非法值被写进文件
        updated = AppSettings.from_dict(merged)
        self.save(updated)
        return updated
