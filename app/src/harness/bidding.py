"""块级竞价仲裁（设计文档 §7，纯函数）。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BidParams:
    interruption_threshold: float = 0.4   # 在位者优势阈值
    speak_threshold: float = 0.1          # 开口阈值：低于=无人想开口
    silence_k: int = 3                    # 连续静默 K 块后世界层兜底


@dataclass(frozen=True)
class Decision:
    kind: str                             # incumbent_continues | yield_to | silence
    speaker: str | None = None
    silent_streak: int = 0


def arbitrate(urges: dict[str, float],
              incumbent: str | None,
              silent_streak: int,
              p: BidParams = BidParams()) -> Decision:
    """每块末对所有刚 think 完的在场听众的 urge 做一次仲裁。

    裁定决策表（含等号语义）：
      1) urges 为空                          → silence(+1)
      2) best=argmax；best < speak_threshold  → silence(+1)   （全员不想开口）
      3) incumbent 为 None                   → yield_to(best)
      4) best == incumbent                    → incumbent_continues(incumbent)
      5) best >= incumbent + interruption_th  → yield_to(best)  （挑战者≥阈值幅度抢过话筒，含等号）
      6) 否则（margin 内挑战者）：incumbent ≥ speak_threshold → incumbent_continues，
         否则                                → yield_to(best)
    """
    if not urges:                                      # 1) 无任何 urge → 静默
        return Decision("silence", silent_streak=silent_streak + 1)
    best = max(urges, key=urges.get)
    if urges[best] < p.speak_threshold:                # 2) 全员低于开口阈值 → 静默
        return Decision("silence", silent_streak=silent_streak + 1)
    if incumbent is None:                              # 3) 无在位者 → 交给最高 urge
        return Decision("yield_to", speaker=best, silent_streak=0)
    cur = urges.get(incumbent, 0.0)
    if best == incumbent:                              # 4) 在位者仍最高 → 连任
        return Decision("incumbent_continues", speaker=incumbent, silent_streak=0)
    if urges[best] >= cur + p.interruption_threshold:  # 5) 挑战者≥阈值幅度（含等号）→ 让位
        return Decision("yield_to", speaker=best, silent_streak=0)
    if cur >= p.speak_threshold:                       # 6) margin 内：在位者仍想开口 → 连任
        return Decision("incumbent_continues", speaker=incumbent, silent_streak=0)
    return Decision("yield_to", speaker=best, silent_streak=0)


def should_inject(silent_streak: int, k: int = 3) -> bool:
    return silent_streak >= k
