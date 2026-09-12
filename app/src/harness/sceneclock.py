"""真实流速虚拟钟：秒制纯函数 + 可注入时钟源的 VirtualClock（worker/GUI 侧持有）。

旧版按每句/每静默折算「情节分钟」推进共享态 clock 键；本模块取代之——场景虚拟钟
改为 = 开场时刻 + 真实流逝 × 全局流速（默认 1×），由桌面 worker 在 GUI 侧连续计算，
不再进入图/引擎共享态，也不在角色开口/思考时增减时间。

约定：虚拟时刻一律用「距开场当天 00:00 的秒数」（可超 86400 表示跨次日，格式化回卷）。
本模块零依赖、无 IO；worker/界面/测试共用。
"""
from __future__ import annotations

import time
from typing import Callable

SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 86400


def parse_hhmm(value: str | int) -> int:
    """把 "HH:MM" 解析成距 00:00 的秒数（0..86399）。

    "21:30" → 77400；int 输入取模 86400 直接视为秒（宽容宽松调用方）。非法字符串
    （含小时/分钟越界、格式不符、多余冒号）抛 ValueError，供引擎开场前 fail fast。
    """
    if isinstance(value, int):
        return value % SECONDS_PER_DAY
    if not isinstance(value, str):
        raise ValueError(f"无法解析场景时间: {value!r}")
    text = value.strip()
    try:
        hh_s, mm_s = text.split(":")
        seconds = int(hh_s) * SECONDS_PER_HOUR + int(mm_s) * SECONDS_PER_MINUTE
    except (ValueError, AttributeError):
        raise ValueError(f"无法解析场景时间(需 HH:MM): {value!r}") from None
    if not (0 <= seconds < SECONDS_PER_DAY) or text.count(":") != 1:
        raise ValueError(f"场景时间越界(需 00:00~23:59): {value!r}")
    return seconds


def format_clock(seconds: int | float) -> str:
    """秒 → "HH:MM:SS"（虚拟钟界面时刻，跨午夜自动回卷 86400）。"""
    sec = int(seconds) % SECONDS_PER_DAY
    hh, rem = divmod(sec, SECONDS_PER_HOUR)
    mm, ss = divmod(rem, SECONDS_PER_MINUTE)
    return f"{hh:02d}:{mm:02d}:{ss:02d}"


def format_hhmm(seconds: int | float) -> str:
    """秒 → "HH:MM"（开始/打烊等分钟精度展示，跨午夜回卷）。"""
    sec = int(seconds) % SECONDS_PER_DAY
    hh, rem = divmod(sec, SECONDS_PER_HOUR)
    return f"{hh:02d}:{rem // SECONDS_PER_MINUTE:02d}"


class VirtualClock:
    """可注入时钟源的连续虚拟钟：current() = 冻结值 + 流逝 × 流速。

    freeze()/set_rate() 先把流逝折算进冻结值再重置基线，保证暂停与调速都不丢已走
    时间（调速不清零、暂停后继续从冻结时刻接着走）。monotonic 可注入假时钟供测试
    精确驱动；生产用 time.monotonic。
    """

    def __init__(self, start_seconds: int, rate: float = 1.0,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.start_seconds = int(start_seconds)
        self.rate = float(rate)
        self._monotonic = monotonic
        self._now = int(start_seconds)     # 冻结时刻（暂停/调速后的当前值）
        self._baseline: float | None = None  # 非 None=正在走（距 _now 的真实起点）

    def is_running(self) -> bool:
        return self._baseline is not None

    def current(self) -> int:
        """当前虚拟秒数（走时 = _now + 流逝×流速；冻结时返回 _now）。"""
        if self._baseline is None:
            return self._now
        return int(self._now + (self._monotonic() - self._baseline) * self.rate)

    def start(self) -> None:
        """开走：从当前值起计时（开场/继续共用；_now 保持既有冻结值）。"""
        self._baseline = self._monotonic()

    def freeze(self) -> None:
        """暂停/收束：把流逝折进 _now 并停表，此后 current() 恒定。"""
        if self._baseline is not None:
            self._now = self.current()
            self._baseline = None

    def set_rate(self, rate: float) -> None:
        """改流速：先冻结当前时刻（不丢已走时间），再以新流速重新起表。"""
        self.freeze()
        self.rate = float(rate)
        self._baseline = self._monotonic()

    def advance(self, seconds: int) -> None:
        """把钟**整段前推** seconds（跳时间，§4.3）：场景跳过一段时间，钟跟着跳过去。

        冻结值直接加；走时中的表先把已流逝折进冻结值再整体前推，**不丢已走的时间**、
        **不改流速**、**不改变走/停状态**（跳完照原节奏接着走）——跳时间只是"时间过去了"，
        不是重新起表。负值原样接受（调用方负责不传）。
        """
        running = self._baseline is not None
        if running:
            self.freeze()                  # 先折现（冻结值 = 此刻）
        self._now += int(seconds)
        if running:
            self._baseline = self._monotonic()
