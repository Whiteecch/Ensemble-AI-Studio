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
  - 信息库检索 knowledge_enabled ∈ 真/假（§10.2，缺省**开**）
  - 流式开口 stream_speak ∈ 真/假（§二，缺省**关**，见 DEFAULT_STREAM_SPEAK 的理由）
文件里某一字段非法时**只回落该字段**的默认值，其余字段照旧保留。

**新增字段必须同时进 `from_dict` 与字段白名单**（`update` 的白名单是 dataclass 的字段
集合，`to_dict` 也是）：§10.2 点名的坑就是"没同步白名单 → 用户改的值被静默丢弃"，
界面看起来正常、引擎却照旧按默认跑。
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from .. import paths as paths_mod
from ..paths import APP_DIR_NAME, SETTINGS_FILE_NAME  # noqa: F401 - 单一出处（paths）

# 用户数据目录名与文件名都在 `harness.paths` 里（**唯一出处**）：本模块不再自带一份
# `APP_DIR_NAME` 与平台分支，只复用 `paths.user_dir()`——两处各写一份必然有一天分叉，
# 那时设置会写到一个"软件不再读"的目录里，表现为"改了设置不生效"。上面那两条 import
# 只是把名字继续挂在 `settings` 上（既有引用照旧可用）。

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

#: 「信息库检索」缺省**开**（§10.2）：关 = 与没有信息库同义（不注入索引、不传 tools、
#: 不执行取用），整场行为**退回今天**。缺省开是刻意的——信息库是这套系统的常态，
#: 关掉它是一个降级开关，不是一个可选特性。
DEFAULT_KNOWLEDGE_ENABLED = True

#: 「流式开口」（《人际关系与场景推进》§二）缺省**开**——这是**产品行为**，不是可选特性：
#: 需求原话是"人物的对话都采用流式输出"，逐字出现给思考留出时间、不那么生硬，是这一批
#: 要的默认观感。
#:
#: **层次要说清**：这里是**产品缺省**（设置 / worker / 窗口这一层）；`graph.build_graph`
#: 与 `SceneEngine` 的 `stream_speak` 参数缺省仍是 **False**——"不开启时从后端调用到事件流
#: 到界面逐字节、逐事件、逐调用次数相同"这条不变量留在低层，由它守住：谁显式传 False
#: （测试、CLI、或用户关掉这个开关），跑的就是那条老路。
#:
#: 关掉它的代价（用户自己权衡）：模型说到一半你也看得见——**包括**罕见的近重复作废那一次
#: "话说一半消失了"。逐字出现好看，但值不值得这个代价交给用户。
DEFAULT_STREAM_SPEAK = True


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


def _as_flag(value: Any, default: bool) -> bool:
    """宽容地读一个是非开关（§10.2「信息库检索 开/关」）。**永不抛。**

    与 `knowledgestore._as_bool` 同一套路：认真 `bool`、`0`/`1`（JSON 里手写成数字很常见）、
    以及 `true/false/yes/no/on/off` 这类字符串（手改时大小写随意）。其余（`"久了"`、列表、
    NaN…）→ `default`。读不出来就往"默认"降级，绝不把异常带出 `load()`。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return {0: False, 1: True}.get(value, default)
    if isinstance(value, float):
        return {0.0: False, 1.0: True}.get(value, default) if math.isfinite(value) else default
    if isinstance(value, str):
        return {"true": True, "1": True, "yes": True, "on": True,
                "false": False, "0": False, "no": False, "off": False,
                }.get(value.strip().casefold(), default)
    return default


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
    #: 信息库检索 开/关（§10.2）：关 = 与没有信息库同义——不注入索引、不传 tools、不执行
    #: 取用，整场行为退回今天。**缺省开**（见 DEFAULT_KNOWLEDGE_ENABLED 的理由）。
    knowledge_enabled: bool = DEFAULT_KNOWLEDGE_ENABLED
    #: 流式开口（§二）：开 = 台词逐字出现（走 complete_text_stream 的新通道）。
    #: **缺省关**（见 DEFAULT_STREAM_SPEAK 的理由）——不开启时一切与今天逐字节相同。
    stream_speak: bool = DEFAULT_STREAM_SPEAK

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
            knowledge_enabled=_as_flag(d.get("knowledge_enabled",
                                             DEFAULT_KNOWLEDGE_ENABLED),
                                       DEFAULT_KNOWLEDGE_ENABLED),
            stream_speak=_as_flag(d.get("stream_speak", DEFAULT_STREAM_SPEAK),
                                  DEFAULT_STREAM_SPEAK),
        )

    def as_dict(self) -> dict[str, Any]:  # 兼容别名
        return self.to_dict()


def default_settings_path() -> Path:
    """默认设置文件 = **用户数据目录**下的 `settings.json`（跨平台，绝不在仓库内）。

    与 `harness.paths.user_dir()` **同源**：平台三分支与目录名只有 `paths` 那一份。这里
    每次都现调 `paths_mod.user_dir()`（不是在 import 时算死），故 monkeypatch 它就能连
    设置一起关进沙箱——"绝不写真实用户目录"这条纪律靠的正是这一点。
    """
    return paths_mod.user_dir() / SETTINGS_FILE_NAME


def legacy_settings_paths() -> tuple[Path, ...]:
    """**只读**兼容用的旧设置文件候选（改名前的用户目录，见 `paths.LEGACY_APP_DIR_NAMES`）。

    目录名统一到 `Ensemble-AI-Studio`（§1.1）之后，老用户那份 settings.json 就落在新落点
    之外：只读新落点 = 已填过的 api_key/主题/语言静默回默认（api_key 没了还会悄悄退回离线
    stub），而旧文件一直躺在原地既不报错也不被读。故新文件读不到时逐个回落**读**这里——
    写永远写新落点、旧文件不删不动（用户想清自己清）。
    """
    return tuple(d / SETTINGS_FILE_NAME for d in paths_mod.legacy_user_dirs())


class SettingsStore:
    """settings.json 的读写门面；任何读失败都回落默认值。"""

    def __init__(self, path: Path | None = None) -> None:
        #: 是否由调用方**显式**指定了文件（显式=不启用改名兼容回落，见 `_read_candidates`）。
        self._explicit: bool = path is not None
        self.path: Path = Path(path) if path is not None else default_settings_path()

    # ------------------------------------------------------------- 读
    def _read_candidates(self) -> tuple[Path, ...]:
        """`load()` 的读候选（按优先级）：**显式给的路径只认它自己**，缺省路径才带兼容回落。

        显式路径（`SettingsStore(p)`）是调用方指定的那一份，不该"顺带"去读用户旧目录——那会让
        `p` 指哪儿都读得到别处的设置，也让测试设的隔离目录形同虚设。缺省路径则相反：它就是
        "用户的那份设置"，改名后的新旧两处都要看（新优先）。
        """
        if self._explicit:
            return (self.path,)
        return (self.path, *legacy_settings_paths())

    def load(self) -> AppSettings:
        """读取设置；候选逐个尝试，全都不成则返回默认值，不抛异常也不写盘。

        **from_dict 也在 try 内**：设置文件是用户可手改的文本，任何字段的转换都可能
        在坏输入上抛（如 `{"autosave_every": "²"}`）——而 load() 是在 app.py 里、
        QApplication 之前调用的，抛出去直接把软件挡在启动门外。故这里兜底为
        「绝不抛」：读盘、解析、还原三步的任一异常都回落默认值。

        候选顺序（`_read_candidates`）：新落点 → 旧落点（改名兼容，只读）。**新文件损坏时
        也继续试旧文件**：损坏的新文件不该连带把用户填过的 api_key 一起丢掉。旧文件只在
        "新落点读不出可用设置"时被读到；下一次 `save()/update()` 自然整份写进新落点，旧文件
        原样留着——这就是本次改名唯一的那条迁移路径（不做自动搬迁：load() 不写盘）。
        """
        for candidate in self._read_candidates():
            try:
                raw = candidate.read_text(encoding="utf-8")
                data = json.loads(raw)
                return AppSettings.from_dict(data)
            except Exception:  # noqa: BLE001 - 脏设置文件绝不能把软件挡在启动门外
                # 缺失、无权限、是目录、编码错、JSON 语法错、字段转换异常 → 试下一个候选
                continue
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
