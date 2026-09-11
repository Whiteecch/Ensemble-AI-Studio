"""speak 近重复判定的纯文本相似度助手（三层刹车第 B 层）。

normalize(s)：剥空白 → 剥全/半角括号的舞台说明片段（（…）、(...)，含嵌套）→
剥标点，只留字母数字（含 CJK 汉字）。ratio(a, b)：normalize 后算
difflib.SequenceMatcher 比例（确定性）。两串都空（如纯舞台说明）按不相似处理，
避免「任意纯括号行都相撞」的误伤。graph.py 的 speak 节点用它拦「自己复读自己」。
"""
from __future__ import annotations

import difflib
import re

#: 匹配一段不含括号的括号片段（全角/半角都算）；循环替换以清掉嵌套括号。
_PAREN_FRAG = re.compile(r"[（(][^（()）]*[）)]")


def _strip_parentheticals(s: str) -> str:
    prev = None
    while prev != s:
        prev = s
        s = _PAREN_FRAG.sub("", s)
    return s


def normalize(s: str | None) -> str:
    """内容 → 只含字母/数字/汉字的紧凑串（供相似度比较）。"""
    if not s:
        return ""
    text = _strip_parentheticals(str(s))
    text = re.sub(r"\s+", "", text)
    return "".join(ch for ch in text if ch.isalnum() or ch == "_")


def ratio(a: str | None, b: str | None) -> float:
    """两串 normalize 后的相似度 0~1；任一侧空串 → 0.0（避免空/空判成相同）。"""
    x, y = normalize(a), normalize(b)
    if not x or not y:
        return 0.0
    return difflib.SequenceMatcher(None, x, y).ratio()
