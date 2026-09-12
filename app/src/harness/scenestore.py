"""场景运行时存档（sidecar）：跑起来的状态另存一份，绝不改动场景文件本体。

《场景编排与桌面外壳_设计文档》§5「保存场景 / 续演」要求保存内容 = 场景配置 + 角色
名单与进场时间 + 虚拟钟 + 对话历史。其中**配置部分**留在 `Scene`（`scenes/*.json`）
里原样不动——文件名、schema、字段一个都不加；**运行起来的那部分状态**（转录、虚拟
秒、已跑块数）另存为同目录 + 同名前缀的 sidecar：

    贝克街221B.json  →  贝克街221B.json.runtime.json

为什么是 sidecar 而不是塞回场景文件：
  · 旧版程序 / 手写场景 / 其它工具读场景文件完全不受影响（sidecar 缺席 = 全新开场）；
  · 写坏也坏不到场景本体：落盘走「同目录临时文件 + os.replace」原子替换，断电/崩溃
    时读者要么看到旧的完整存档、要么看到新的完整存档，绝不看到半截 JSON；
  · 存档是可丢的**缓存**而非资产：`load_runtime` 一律**永不抛**——缺失 / 空文件 /
    坏 JSON / 类型不对 → 空 `RuntimeBundle()`（语义等同「没有存档」，界面照常按场景
    配置开场）。一个坏掉的自动保存不该让程序起不来。

本模块只依赖 stdlib（json/os/tempfile/dataclasses），无 Qt、无 harness 内部依赖；
GUI worker 每块调一次 `AutosaveTracker.note()`，测试直接调用纯函数。
"""
from __future__ import annotations

import copy
import json
import math
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: sidecar 后缀：加在**完整场景文件名**之后（含 .json），故 `贝克街221B.json` →
#: `贝克街221B.json.runtime.json`。用整名 + 后缀而非 `with_suffix`——后者会把 `.json`
#: 换成 `.runtime.json`，丢掉原名里的点（`a.b.json` 这类文件名会算错）。
RUNTIME_SUFFIX = ".runtime.json"


def _now_iso() -> str:
    """当前 UTC 时刻的 ISO-8601 文本（秒精度；仅供人类看时间线）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------- 字段级清洗 --
# load_runtime 的契约是「永不抛」，所以每个字段各洗各的：类型不对就回退到默认值，
# 一个烂字段不带垮其它好字段（部分有效的存档尽量救回来）。

def _as_int(value: Any, *, default: int | None, minimum: int | None = None) -> int | None:
    """int / 有限 float → int；bool（int 子类，别被 True==1 蒙混）与其它类型 → default。

    minimum 用于「非负才有意义」的字段（blocks），越界同样回退。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if not math.isfinite(value):
        return default
    out = int(value)
    if minimum is not None and out < minimum:
        return default
    return out


def _as_str(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _as_dict(value: Any) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def _as_transcript(value: Any) -> list[dict]:
    """转录必须是「dict 的列表」；非列表 → []，列表里的非 dict 元素逐条剔除。

    消息 dict 原样保留（含 knows/address/time_hhmmss 与任何额外键）——转录是不透明
    载荷，本模块不解释它的字段语义，只保证存回来一模一样。
    """
    if not isinstance(value, list):
        return []
    return [dict(m) for m in value if isinstance(m, dict)]


@dataclass
class RuntimeBundle:
    """一份运行时存档 = 续演所需的全部「非配置」状态。

    transcript 是共享态消息 dict 的列表（`{id, speaker, speaker_type, content,
    in_scene, turn, ...}`），本模块只搬运不解释；clock_seconds 是虚拟钟当前秒
    （距开场当天 00:00，None = 未知/未开始）；blocks 是已进行的块数（自动保存的
    周期计数基准）；meta 预留放禁言状态之类的杂项，未知键原样往返。
    """

    transcript: list[dict] = field(default_factory=list)
    clock_seconds: int | None = None       # 虚拟钟当前秒（None=未知/未开始）
    saved_at: str = ""                     # ISO-8601 UTC，由调用方传入或由 save 时生成
    blocks: int = 0                        # 已进行块数
    meta: dict = field(default_factory=dict)  # 预留（如禁言状态）

    def to_dict(self) -> dict:
        """纯 helper：转成可 JSON 化的 dict（深拷贝，改返回值不影响本 bundle）。"""
        return {
            "transcript": copy.deepcopy(self.transcript),
            "clock_seconds": self.clock_seconds,
            "saved_at": self.saved_at,
            "blocks": self.blocks,
            "meta": copy.deepcopy(self.meta),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "RuntimeBundle":
        """纯 helper：任意（可能很烂的）dict → bundle；逐字段回退，绝不抛。

        非 dict 输入（列表/标量/None）→ 空 bundle；未知键忽略（前向兼容）。
        """
        if not isinstance(data, dict):
            return cls()
        return cls(
            transcript=_as_transcript(data.get("transcript")),
            clock_seconds=_as_int(data.get("clock_seconds"), default=None),
            saved_at=_as_str(data.get("saved_at")),
            blocks=_as_int(data.get("blocks"), default=0, minimum=0) or 0,
            meta=_as_dict(data.get("meta")),
        )


# ------------------------------------------------------------------ 路径换算 --
def runtime_path_for(scene_file: Path) -> Path:
    """场景文件路径 → 其 sidecar 路径（同目录、整名 + `.runtime.json`）。"""
    p = Path(scene_file)
    return p.with_name(p.name + RUNTIME_SUFFIX)


# ---------------------------------------------------------------------- 读写 --
def save_runtime(scene_file: Path, bundle: RuntimeBundle) -> Path:
    """原子落盘 sidecar，返回写入路径；自动建父目录。

    saved_at 为空时由本函数补当前 UTC 时刻（调用方给了就尊重调用方，便于测试与
    跨端同步）。写盘 = 同目录临时文件 → fsync → `os.replace` 覆盖：读者永远看不到
    半截文件，写失败也不会毁掉旧存档（临时文件会被清掉）。不改 bundle 本体。
    """
    target = runtime_path_for(scene_file)
    payload = bundle.to_dict()
    if not payload["saved_at"]:
        payload["saved_at"] = _now_iso()
    text = json.dumps(payload, ensure_ascii=False, indent=2)

    target.parent.mkdir(parents=True, exist_ok=True)
    # 临时文件必须与目标同目录同卷，os.replace 才是原子替换（跨卷会退化成拷贝）。
    fd, tmp_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent))
    try:
        # newline="\n"：不随平台把 \n 翻成 \r\n，落盘字节可复现（json.dumps 已带 \n）。
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target


def load_runtime(scene_file: Path) -> RuntimeBundle:
    """读 sidecar；缺失 / 空 / 坏 JSON / 类型不对 → 空 bundle。**绝不抛。**"""
    path = runtime_path_for(scene_file)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # 文件不存在、是目录、没权限、非 UTF-8——一律当作「没有存档」。
        return RuntimeBundle()
    try:
        data = json.loads(raw)
    except ValueError:          # json.JSONDecodeError ⊂ ValueError；空串也走这里
        return RuntimeBundle()
    return RuntimeBundle.from_dict(data)


def has_runtime(scene_file: Path) -> bool:
    """sidecar 是否存在（且是文件而非目录）；探测本身出错也算「没有」。"""
    try:
        return runtime_path_for(scene_file).is_file()
    except OSError:
        return False


def clear_runtime(scene_file: Path) -> bool:
    """删除 sidecar；真删掉了→True，本来就不存在（或删不掉）→False。"""
    try:
        runtime_path_for(scene_file).unlink()
    except OSError:             # FileNotFoundError / PermissionError / IsADirectoryError
        return False
    return True


# ------------------------------------------------------------------ 自动保存 --
@dataclass(frozen=True)
class AutosavePolicy:
    """自动保存周期（设置里的 5/20/50/100 轮 → every）；every<=0 视为禁用。"""

    every: int = 20

    def should_save(self, blocks_done: int, last_saved_blocks: int | None = None) -> bool:
        """距上次保存够一个周期就 True。

        blocks_done<=0 不保存（还没跑过任何一块，存了也是空档）；last_saved_blocks
        为 None/0 都当作「从 0 起算」。判定只看**累计差**而非块号取模，故周期中途
        改设置（20→50）也不会意外触发或错过。
        """
        if self.every <= 0:
            return False
        if blocks_done <= 0:
            return False
        return blocks_done - (last_saved_blocks or 0) >= self.every


@dataclass
class AutosaveTracker:
    """自动保存节流器：GUI worker 每块跑完调一次 `note(blocks_done)`。

    到点（返回 True）即**当场推进** last_saved_blocks，调用方不必自己记——避免保存
    失败/重复调用时反复触发。

    契约：**打开/重置场景时必须由调用方调 reset()**。本类拿不到场景上下文，自己察觉
    不了「换了场景」——引擎/GUI 的接线不在这里。漏调的后果只是入账点沿用上一场的块号
    （新场景要再跑满一个周期才第一次自动保存，存档内容不会错、只是节奏偏晚）。
    """

    policy: AutosavePolicy
    last_saved_blocks: int | None = None

    def note(self, blocks_done: int) -> bool:
        """到点→推进入账点并返回 True；否则返回 False（不改状态）。"""
        if not self.policy.should_save(blocks_done, self.last_saved_blocks):
            return False
        self.last_saved_blocks = blocks_done
        return True

    def reset(self) -> None:
        """**打开/重置场景后必须调用**：清掉上次保存点（新场景从 0 起算；不动 policy）。"""
        self.last_saved_blocks = None
