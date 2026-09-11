from harness.bidding import BidParams, Decision, arbitrate, should_inject


def test_no_incumbent_yields_to_highest():
    d = arbitrate({"甲": 0.9, "乙": 0.2}, incumbent=None, silent_streak=0)
    assert d == Decision("yield_to", speaker="甲", silent_streak=0)


def test_incumbent_continues_when_within_threshold():
    # 在位者 0.9 vs 次高 0.7，差值 0.2 < 0.4 → 连任
    d = arbitrate({"甲": 0.9, "乙": 0.7}, incumbent="甲", silent_streak=0)
    assert d.kind == "incumbent_continues"
    assert d.speaker == "甲"


def test_incumbent_yields_when_overtaken_beyond_threshold():
    # 乙 1.2 超过甲 0.5 达 0.7 > 0.4 → 打断让位
    d = arbitrate({"甲": 0.5, "乙": 1.2}, incumbent="甲", silent_streak=0)
    assert d == Decision("yield_to", speaker="乙", silent_streak=0)


def test_all_silent_increments_streak():
    d = arbitrate({"甲": 0.02, "乙": 0.01}, incumbent=None, silent_streak=1)
    assert d == Decision("silence", speaker=None, silent_streak=2)


def test_should_inject_at_k():
    assert should_inject(2, k=3) is False
    assert should_inject(3, k=3) is True


def test_challenger_overtake_at_exact_threshold_yields():
    # 乙 0.9 - 甲 0.5 = 0.4 == 打断阈值 → 含等号，仍让位给挑战者
    d = arbitrate({"甲": 0.5, "乙": 0.9}, incumbent="甲", silent_streak=0)
    assert d == Decision("yield_to", speaker="乙", silent_streak=0)


def test_incumbent_below_speak_threshold_yields_within_margin():
    # 甲 0.05 < speak_threshold 0.1，虽在 margin 内也不再连任 → 让位
    d = arbitrate({"甲": 0.05, "乙": 0.3}, incumbent="甲", silent_streak=0)
    assert d == Decision("yield_to", speaker="乙", silent_streak=0)
