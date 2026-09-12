"""场景跳时间（《人际关系与场景推进》§四）的判据侧与引擎侧契约。

本节设计的灵魂是**严格限制，不许轻易跳**（§4.2）——实现时宁可跳不成，也不要跳错。
本文件逐条钉死这套限制：

  · **前件闸**（§4.1 条件 1，引擎侧的确定性判据）：「全员无话可说」= **本块没有任何人
    开口** 且 在场每个人的**发言倾向（bid）全部低于阈值**（0.5），且**连续 K 块**如此
    ——复用 `dynamics` 已有的 bid，不新增数值系统。两半缺一不可：只查 bid 会在两人
    一句接一句对话时洞开（见 BID_SILENCE_THRESHOLD 的标定与下面的错跳回归用例）；
  · **幅度闸**（§4.2）：正文必须**自己点明**过了多久，且正文时距不得大于申报值（叙事与
    钟必须一致）；单次跳跃有上限（默认 8 小时 = 480 分钟），且**小幅度优先**：落地的钟
    偏移取「模型申报的分钟数」与「正文自己写出的时距」中**较小**者（能跳 30 分钟就不跳
    3 小时）；
  · **冷却闸**（§4.2）：跳过一次后 M 块内不许再跳；
  · **频次闸**（§4.2）：整场最多跳 N 次（次数按**活着的**跳时间行算：撤回/改写退回额度、
    续演接着旧账）；
  · **节奏闸**：跳时间**经过**既有的叙述节奏体系（§4.2 末条），不绕过它——自动档看
    引擎自己的"这一轮可以叙述"（冷却 + 活跃度缩放后的**触发线**）；
  · 被闸挡下时留下**可诊断的记录**（`time_skip_state()["records"]`），绝不静默丢弃，
    并把最近一条回喂进提示词（免得模型每块重提同一条永远过不了的提案）。

最重要的一条是 `test_model_proposal_is_vetoed_when_precondition_fails`：模型说该跳、
条件不成立就**不跳**——与 hooks 的时间闸门同一套思路（模型判断 + 引擎否决）。

落行仍走既有叙述那条路（speaker=场景 / speaker_type=narrator，§4.3），只在消息上多一个
`clock_jump_minutes` 字段；钟归 worker 持有，引擎不推进任何钟（虚拟钟的前推见
test_gui_time_skip.py）。

全部离线确定性：stub 后端（think/speak/narrate 三档）+ 零阈值竞价 + tmp_path。
"""
import json
from pathlib import Path

import pytest

from harness import sceneclock as sc
from harness import scenarist
from harness import scenestore
from harness.backends.stub import StubBackend
from harness.engine import SceneEngine
from harness.tests.helpers import assert_public_only

#: 一条跳时间的叙述正文（「30 分钟之后……」+ 活动完成的宣告，§4.3）。
_SKIP_BODY = "30 分钟之后，桌上的灯自己灭了，作业终于写完了。"
#: stub 叙述档的固定产出：一条跳时间提案。
_SKIP_RAW = f"[[SKIP:30]] {_SKIP_BODY}"

#: 「全员无话可说」的 think 产出：不想说（urge 0），情绪也平（arousal 0）。
_URGE_LOW = {"aroused": 0.0, "obligation_fulfilled": [], "goal_progress": 0.0,
             "addressed": None, "impression_of_speaker": None, "urge": 0.0}

#: 把沉默压力与自报冲动两项关掉：bid 因此恒定低于阈值——本文件的引擎用例钉的是
#: **前件闸的计数与四道闸**，不是动力学标定（那是 test_dynamics.py 的事）。
_NO_BID_DRIFT = {"silence_gain": 0.0, "urge_gain": 0.0}

#: 一条永不成立的钩子：它的唯一作用是让场景**每块都被咨询**（钩子路径的既有口径），
#: 于是跳时间的提案每块都有机会提交——这正是"引擎否决模型"要用到的场面。
_HOOK = {"id": "h1", "condition": "有人哭了", "event_kind": "context",
         "context_text": "（有人哭了。）"}

#: 判据参数的"快档"：K=1、M=1，让"逐条闸门"的用例几块之内就能走完。
_FAST = {"streak_blocks": 1, "cooldown_blocks": 1, "max_jumps": 3}
#: 把**既有的叙述节奏**闸门归零（冷却与触发线都恒过），好让别的闸单独受检。
_NO_RHYTHM = {"cooldown_blocks": 0, "threshold": 0.0}

#: 生产档竞价（`app/config/bid.yaml` 的原值）：开口阈值 0.1。用它才能跑出"角色真的在
#: 一块接一块地对话"的场面——测试档的 0.0 会让全场永远沉默（那正是错跳用例要的对照面）。
_PROD_BID = "interruption_threshold: 0.4\nspeak_threshold: 0.1\nsilence_k: 3\n"
#: 测试档：开口阈 0.0（bid 恒定低于它 → 谁都不开口）+ 静默兜底不可达。
_TEST_BID = "interruption_threshold: 0.4\nspeak_threshold: 0.0\nsilence_k: 100000\n"


class _RecordingNarrate(StubBackend):
    """叙述档替身：吐出固定脚本，**并把每次的提示词记下来**（证明引擎发了什么）。"""

    def __init__(self, lines: list[str]) -> None:
        super().__init__(line_script=lines)
        self.prompts: list[list[dict]] = []

    async def complete_text(self, messages: list[dict]) -> str:
        self.prompts.append(messages)
        return await super().complete_text(messages)


def _build(root: Path, *, enabled: bool = True, skip_params=None, dynamics=_NO_BID_DRIFT,
           narrate: str = _SKIP_RAW, nudge=_NO_RHYTHM, hooks=(_HOOK,),
           bid_yaml: str = _TEST_BID, think: dict | None = None,
           speak_lines: list[str] | None = None, **kw) -> SceneEngine:
    """两名角色的自习室 + 三档 stub；默认开跳时间、前件闸快档、节奏闸恒过。"""
    root.mkdir(parents=True, exist_ok=True)
    scene = {"name": "自习室", "participants": ["丙", "丁"],
             "description": "一间自习室，四张桌椅围在一起。", "plot_direction": "",
             "hooks": list(hooks or []),
             "hard_boundary": {"type": "time", "value": "23:30", "desc": "熄灯"}}
    (root / "自习室.json").write_text(json.dumps(scene, ensure_ascii=False),
                                     encoding="utf-8")
    for name in ("丙", "丁"):
        (root / f"{name}.json").write_text(
            json.dumps({"name": name, "personality": {"描述": name}},
                       ensure_ascii=False), encoding="utf-8")
    (root / "models.yaml").write_text(
        "think:\n  backend: stub\n  model: stub\n  params: {}\n"
        "speak:\n  backend: stub\n  model: stub\n  params: {}\n"
        "narrate:\n  backend: stub\n  model: stub\n  params: {}\n", encoding="utf-8")
    (root / "bid.yaml").write_text(bid_yaml, encoding="utf-8")
    eng = SceneEngine(root / "自习室.json",
                      [root / "丙.json", root / "丁.json"],
                      root / "models.yaml", run_root=root / "runs",
                      bid_path=root / "bid.yaml", closing_at_block=200,
                      time_skip_enabled=enabled, time_skip_params=skip_params,
                      dynamics_params=dynamics, nudge_params=nudge, **kw)
    # 全员无话可说（低 urge）+ 谁都不开口（台词不含任何人名，绝不误抬邻接/欠答）。
    eng.think_backend = StubBackend(json_script=[think or _URGE_LOW])
    eng.speak_backend = StubBackend(line_script=list(speak_lines or ["（……）"]))
    eng.narrate_backend = _RecordingNarrate([narrate])
    return eng


def _jump_lines(msgs: list[dict]) -> list[dict]:
    """带钟偏移的叙述行（跳时间的落地产物）。"""
    return [m for m in msgs if m.get("clock_jump_minutes")]


def _judge(requested: int = 30, **kw) -> scenarist.TimeSkipVerdict:
    """判据的默认输入：前件已成立、无冷却、没跳过、正文非空且点明了时距。

    `implied_minutes` 缺省时**按正文现算**（与引擎同口径：幅度闸拿正文自己写的时距
    与申报值比对），要单测"正文说不出时距"就显式传 None。
    """
    args = {"body": "30 分钟之后，作业终于写完了。", "streak": 3,
            "blocks_since_last": None, "jumps_done": 0}
    args.update(kw)
    if "implied_minutes" not in args:
        args["implied_minutes"] = scenarist.jump_minutes_from_text(args["body"])
    return scenarist.judge_time_skip(requested, **args)


# ============================================================ A) 前件闸（纯函数）
def test_all_bids_below_is_strict_and_empty_never_qualifies():
    """「全员无话可说」= 每个人**严格低于**阈值；空场不成立（宁可跳不成）。

    恰好等于阈值不算「低于」（§4.1 的用词就是"低于"）；一人想说就不算全员；空场
    （全员离场/全部禁言）根本没有人可沉默——跳它在语义上是空的，故不成立。
    """
    below = scenarist.all_bids_below
    assert below({"甲": 0.49, "乙": 0.0}) is True
    assert below({"甲": 0.4, "乙": 0.9}) is False, "一人想说就不算全员无话可说"
    assert below({"甲": 0.5}) is False, "恰好等于阈值不算「低于」"
    assert below({}) is False, "空场：没人可沉默 → 不成立"


def test_silent_streak_counts_trailing_consecutive_blocks():
    """给定逐块 bid 序列 → 尾部连续「全员无话可说」的块数（被打断即从头数）。"""
    low, high = {"甲": 0.1, "乙": 0.2}, {"甲": 0.9, "乙": 0.1}
    assert scenarist.silent_streak([low, low, high, low, low, low]) == 3
    assert scenarist.silent_streak([high, low]) == 1
    assert scenarist.silent_streak([low, low, low]) == 3
    assert scenarist.silent_streak([]) == 0
    assert scenarist.silent_streak([high]) == 0


def test_silence_gate_needs_k_consecutive_blocks_and_resets_on_interruption():
    """连续 K 块才成立：中间有一块有人想说（bid 过线）就归零重数。"""
    low, high = {"甲": 0.1, "乙": 0.2}, {"甲": 0.9}
    gate = scenarist.SilenceGate()
    assert gate.observe(low) == 1 and not gate.ready(3)
    assert gate.observe(low) == 2 and not gate.ready(3)
    assert gate.observe(high) == 0, "被打断 → 归零"
    assert gate.observe(low) == 1
    assert gate.observe(low) == 2
    assert gate.observe(low) == 3 and gate.ready(3), "连续 K 块才成立"
    gate.reset()
    assert gate.count == 0 and not gate.ready(3)
    assert gate.ready(0) is True, "K=0 = 不需要任何前件（只供测试/实验）"


def test_silence_gate_a_block_with_a_speaker_never_counts():
    """**有人开口的块一律不算「无话可说」**——哪怕所有人的 bid 都低。

    实况错跳的根因就在这里：`bid` 里的 `recency_penalty`（刚开口的人 −0.6）与只涨一格的
    沉默压力让「全员都低」在两人一句接一句时**每块都成立**，streak 于是无限涨、闸门洞开
    （模型照着引擎给的假前提提提案，引擎再放行）。有人刚说了话 = 他显然有话要说——这一块
    必须归零。
    """
    low = {"甲": 0.1, "乙": 0.2}
    gate = scenarist.SilenceGate()
    assert gate.observe(low) == 1
    assert gate.observe(low, spoke=True) == 0, "本块有人开口 → 归零（bid 再低也不算）"
    assert gate.observe(low) == 1, "下一块没人开口 → 重新起算"
    # 「说话人刚被 recency 扣到 −0.6、另一人只沉默 1 块」正是实况里的采样形状：
    assert gate.observe({"甲": -0.6, "乙": 0.32}, spoke=True) == 0
    assert scenarist.silent_streak([(low, False), (low, True), (low, False)]) == 1, \
        "纯函数与增量计数器同一口径（(bids, spoke) 逐块）"
    assert scenarist.silent_streak([low, low, low]) == 3, "只给 bids = spoke 全 False"


def test_silence_gate_is_a_pure_read():
    """判据是**纯读**的：不改任何数值、不推进任何钟（同一输入必得同一计数）。"""
    bids = {"甲": 0.1, "乙": 0.2}
    snapshot = dict(bids)
    gate = scenarist.SilenceGate()
    assert gate.observe(bids) == 1
    assert gate.observe(bids) == 2
    assert bids == snapshot, "判据绝不改动喂进来的数值"
    # 同一个 gate 连读两次同一块：计数不因"多读了一次"而漂
    again = scenarist.SilenceGate()
    assert again.observe(bids) == 1 and again.observe(bids) == 2


def test_silence_gate_handles_single_and_empty_casts():
    """单人场照常计；空场归零（无人可沉默 → 前件永不成立）。"""
    gate = scenarist.SilenceGate()
    assert gate.observe({"独": 0.2}) == 1, "单人场：就他一个，他不想说即成立"
    assert gate.observe({}) == 0, "空场 → 归零，绝不跳"
    assert gate.observe({"独": 0.6}) == 0


def test_bid_silence_threshold_is_the_documented_constant():
    """阈值 0.5 是模块级常量（§4.1 给的数），可按需注入但缺省恒为 0.5。"""
    assert scenarist.BID_SILENCE_THRESHOLD == 0.5
    assert scenarist.all_bids_below({"甲": 0.4}, threshold=0.3) is False
    assert scenarist.SilenceGate().threshold == scenarist.BID_SILENCE_THRESHOLD


# ============================================================ B) 四道闸（判据）
def test_verdict_granted_on_the_happy_path():
    """四道闸全过 → 放行，并给出实际生效的分钟数。"""
    v = _judge()
    assert v.granted is True and v.minutes == 30 and v.gate == "ok"
    assert v.requested == 30 and "30" in v.reason


def test_precondition_gate_vetoes_even_when_the_model_insists():
    """**前件闸**：条件 1 不成立 → 无条件否决（模型说跳也不跳），一分一秒都不推。"""
    v = _judge(streak=2)
    assert v.granted is False and v.gate == "precondition"
    assert v.minutes == 0 and "前件" in v.reason
    assert _judge(streak=0).gate == "precondition"
    assert _judge(streak=4).granted is True, "第 3 块起即成立（≥K）"


def test_amplitude_gate_caps_a_single_jump():
    """**幅度闸**：单次跳跃有上限（默认 480 分钟）；恰好在上限 → 允许（闭区间）。"""
    v = _judge(600, body="10 小时之后，天亮了。", implied_minutes=600)
    assert v.granted is False and v.gate == "amplitude" and "上限" in v.reason
    assert v.minutes == 0
    ok = _judge(480, body="8 小时之后，天亮了。", implied_minutes=480)
    assert ok.granted is True and ok.minutes == 480


def test_amplitude_gate_prefers_the_smaller_magnitude():
    """**小幅度优先**：落地值 = min(申报分钟, 正文自己写出的时距)，取小者。

    两个理由：① §4.2 明写"能跳 30 分钟就不跳 3 小时"；② 落地值与正文必须一致——
    正文说「30 分钟之后」而钟走了 8 小时，是读者一眼能看出的矛盾，取小者即消除它。
    """
    v = _judge(480, body="30 分钟之后，他打了个盹。", implied_minutes=30)
    assert v.granted is True and v.minutes == 30, "申报 8 小时而正文只说 30 分钟 → 落小的"
    # 申报值超上限、正文却只写 30 分钟 → 取小之后在上限内，照跳（不因申报值过大而否决）
    v = _judge(600, body="30 分钟之后，他打了个盹。", implied_minutes=30)
    assert v.granted is True and v.minutes == 30


def test_amplitude_gate_requires_the_body_to_name_the_duration():
    """正文必须**自己点明**过了多久：说不出时距 → 拒绝（钟不能骗人）。

    §4.2 的幅度闸写的是"必须是钟点词能表达的整数"，§4.3 的正文格式也要求"开头用钟点词
    点明过了多久"。正文只说「灯灭了。」而申报 480 分钟时，用户读到的是"灯灭了"、钟却走了
    8 小时——两个事实无从互相校验（137 分钟这种任意数也同理）。宁可跳不成。
    """
    v = _judge(480, body="灯灭了。", implied_minutes=None)
    assert v.granted is False and v.gate == "amplitude" and v.minutes == 0
    assert "正文" in v.reason
    # 正文含钟点词（半小时后 / 天亮了）→ 时距说得出来，闸门放行
    assert _judge(30, body="半小时后，他打了个盹。").granted is True
    assert _judge(420, body="天亮了。", implied_minutes=420).granted is True


def test_amplitude_gate_rejects_a_body_that_claims_more_time_than_the_jump():
    """正文比钟走得**远** → 拒绝（不是取小）：镜像的矛盾同样看得见。

    取小者只消除了"申报大、正文小"这一半；反方向的"正文说「10 小时之后／到了第二天
    早上」而指令只报 30 分钟"取小之后仍是正文说一夜过去了、钟只走半小时——取小留下矛盾，
    故这里拒绝整条提案（模型下一块自然会提一条与正文相称的）。
    """
    v = _judge(30, body="10 小时之后，天亮了。", implied_minutes=600)
    assert v.granted is False and v.gate == "amplitude" and v.minutes == 0
    assert "正文" in v.reason
    v = _judge(30, body="到了第二天早上，天亮了。", implied_minutes=510)
    assert v.granted is False and v.gate == "amplitude"
    assert _judge(600, body="10 小时之后，天亮了。", implied_minutes=600).gate == "amplitude", \
        "正文与指令一致、但超过单次上限 → 仍被上限挡下"


def test_amplitude_gate_rejects_a_nonpositive_amount():
    """非正整数没有意义（0 分钟不是跳跃）：当场否决。"""
    assert _judge(0).gate == "amplitude"
    assert _judge(-5).gate == "amplitude"


def test_cooldown_gate_closes_for_m_blocks():
    """**冷却闸**：跳过一次后 M 块内不许再跳；从未跳过则无冷却。"""
    v = _judge(blocks_since_last=0)
    assert v.granted is False and v.gate == "cooldown" and "冷却" in v.reason
    assert _judge(blocks_since_last=9).gate == "cooldown"
    assert _judge(blocks_since_last=10).granted is True, "满 M 块即开"
    assert _judge(blocks_since_last=None).granted is True, "从未跳过 → 无冷却"


def test_frequency_gate_caps_the_whole_scene():
    """**频次闸**：整场 N 次封顶；第 N+1 次一律否决。"""
    assert _judge(jumps_done=2).granted is True
    v = _judge(jumps_done=3)
    assert v.granted is False and v.gate == "frequency" and "上限" in v.reason


def test_body_gate_requires_the_announcement():
    """没有正文就没有"活动完成"的宣告——这条行存在的意义（§4.3）不成立，故不跳。"""
    v = _judge(body="   ")
    assert v.granted is False and v.gate == "body"
    assert v.minutes == 0


def test_rhythm_gate_respects_the_existing_narration_cooldown():
    """跳时间**经过**既有的叙述冷却/活跃度体系（§4.2 末条），不绕过它。"""
    v = _judge(rhythm_ok=False)
    assert v.granted is False and v.gate == "rhythm"
    assert "冷却" in v.reason


def test_gates_read_their_thresholds_from_the_params():
    """四道闸的数值全部来自参数对象（可注入；默认即设计初值）。"""
    p = scenarist.TimeSkipParams(streak_blocks=1, cooldown_blocks=0,
                                 max_jumps=1, max_jump_minutes=120)
    assert _judge(streak=1, params=p).granted is True
    assert _judge(streak=0, params=p).gate == "precondition"
    assert _judge(200, params=p, implied_minutes=200).gate == "amplitude"
    assert _judge(jumps_done=1, params=p).gate == "frequency"
    assert _judge(blocks_since_last=0, params=p).granted is True, "M=0 = 无冷却"


def test_verdict_is_deterministic_and_pure():
    """同一输入必得同一判定（frozen dataclass 逐字段相等），绝不抛异常。"""
    args = {"body": "30 分钟之后。", "streak": 0, "blocks_since_last": 0,
            "jumps_done": 5, "implied_minutes": 30, "rhythm_ok": False}
    first = scenarist.judge_time_skip(30, **args)
    assert first == scenarist.judge_time_skip(30, **args)
    assert first.gate == "precondition", "多闸同时不过时按固定次序报第一个（前件最前）"


def test_default_params_are_the_designed_ones():
    """默认值逐项 = 设计文档 §4.2 给的数（K=3 块 / 8 小时 / M=10 块 / N=3 次）。"""
    p = scenarist.TimeSkipParams()
    assert (p.streak_blocks, p.max_jump_minutes, p.cooldown_blocks, p.max_jumps) \
        == (3, 480, 10, 3)


def test_effective_skip_params_is_identity_at_default_activity():
    """活跃度缺省 0.5 是恒等点：本旋钮引入前后判据一模一样（既有标定不被扰动）。"""
    assert scenarist.effective_skip_params(None, 0.5) == scenarist.TimeSkipParams()
    assert scenarist.effective_skip_params(scenarist.TimeSkipParams(), 0.5) \
        == scenarist.TimeSkipParams()


def test_effective_skip_params_shortens_streak_and_cooldown_with_activity():
    """活跃度复用既有的那套缩放（scenarist._ACT_*）：越高越早能跳、冷却越短；
    但**幅度上限与整场次数是硬限制**，不随活跃度松（§4.2）。"""
    low = scenarist.effective_skip_params(None, 0.0)
    high = scenarist.effective_skip_params(None, 1.0)
    assert low.streak_blocks > scenarist.TimeSkipParams().streak_blocks \
        > high.streak_blocks
    assert low.cooldown_blocks > scenarist.TimeSkipParams().cooldown_blocks \
        > high.cooldown_blocks
    assert high.streak_blocks >= 1 and high.cooldown_blocks >= 0
    for p in (low, high):
        assert p.max_jump_minutes == 480 and p.max_jumps == 3


# ============================================================ C) 指令解析
def test_parse_skip_inline_body():
    """`[[SKIP:30]] 正文`：标记后面的同一行就是这一跳的正文。"""
    got = scenarist.parse_time_skip(_SKIP_RAW)
    assert got.minutes == 30 and got.text == _SKIP_BODY and got.malformed == []


def test_parse_skip_directive_alone_then_body_on_the_next_line():
    """指令自成一行也行（"另起一行写一条指令"）：正文取它下面的行。"""
    got = scenarist.parse_time_skip(f"[[SKIP:30]]\n{_SKIP_BODY}")
    assert got.minutes == 30 and got.text == _SKIP_BODY


def test_parse_skip_without_a_marker_returns_the_text_verbatim():
    """没有标记 → **逐字节原样返回**（不规范化、不动一个空格）。

    这是「不开启时逐字节不变」在解析层的落点：引擎只在真出现标记时才改正文。
    """
    raw = "  服务员 把账单 压在杯底。\n\n"
    got = scenarist.parse_time_skip(raw)
    assert got.text == raw and got.minutes is None and got.malformed == []


@pytest.mark.parametrize("raw", ["[[SKIP:abc]] 三十分钟之后。",
                                 "[[SKIP:]] 之后。",
                                 "[[SKIP:-30]] 之后。",
                                 "[[SKIP:30 之后。"])
def test_parse_skip_malformed_fragments_are_recorded_and_stripped(raw):
    """畸形指令：记进 malformed 并**从正文里剥掉**（语法泄进叙述是事故），绝不抛。"""
    got = scenarist.parse_time_skip(raw)
    assert got.minutes is None
    assert got.malformed and "[[SKIP" in got.malformed[0]
    assert "[[SKIP" not in got.text


def test_parse_skip_only_honours_the_first_wellformed_directive():
    """一行里两张指令：只认第一张，第二张留痕（可诊断）——绝不静默吞掉。"""
    got = scenarist.parse_time_skip("[[SKIP:30]] 之后 [[SKIP:90]] 又过了很久。")
    assert got.minutes == 30
    assert "[[SKIP" not in got.text
    assert got.malformed, "重复的指令要留痕"


def test_parse_skip_is_case_sensitive_and_never_raises():
    """标记大小写精确（与 `[[TOOL:` 同一纪律）；烂输入一律不抛。"""
    got = scenarist.parse_time_skip("[[skip:30]] 之后。")
    assert got.minutes is None and "[[skip:30]]" in got.text
    for raw in (None, "", "   ", "[[SKIP", "[[SKIP:", 12, ["x"]):
        scenarist.parse_time_skip(raw)          # 不抛即通过


@pytest.mark.parametrize("text,expected", [
    ("30 分钟之后，作业终于写完了。", 30),
    ("三十分钟之后。", 30),
    ("半小时后，他醒了。", 30),
    ("一小时之后。", 60),
    ("两小时后，雨停了。", 120),
    ("12 个小时之后。", 720),
    ("几个小时后，天边泛白。", 120),      # 模糊词取**最小**可表达值（小幅度优先）
    ("数小时过去。", 120),
    ("他睡了一觉，什么也没发生。", None),
])
def test_jump_minutes_from_text_reads_the_clock_phrase(text, expected):
    """正文里的钟点短语 → 分钟数（§4.2「必须是钟点词能表达的整数」的机械读法）。"""
    assert scenarist.jump_minutes_from_text(text) == expected


def test_jump_minutes_from_text_reads_dawn_only_with_a_clock():
    """「到了第二天早上」需要一个当前钟才算得出时距；没有钟 → None（不改用猜测值）。"""
    assert scenarist.jump_minutes_from_text(
        "到了第二天早上。", clock_seconds=sc.parse_hhmm("22:00")) == 480
    assert scenarist.jump_minutes_from_text(
        "天亮了。", clock_seconds=sc.parse_hhmm("23:00")) == 420
    assert scenarist.jump_minutes_from_text("到了第二天早上。") is None


def test_time_skip_prompt_block_states_every_gate_and_the_current_state():
    """提示词块：讲清两个条件、四道闸的数值，并报出当前前件状态（供场景自己把握）。"""
    block = scenarist.time_skip_prompt_block(scenarist.TimeSkipParams(),
                                            streak=2, jumps=1, blocks_since=4)
    assert "【可以跳时间】" in block
    assert scenarist.SKIP_OPEN in block and scenarist.SKIP_CLOSE in block
    assert "480" in block and "8 小时" in block
    assert "连续 2/3 块" in block and "已跳 1/3 次" in block
    assert "宁可不跳" in block
    assert "待会儿" not in block, "提示词不写死任何题材词/口头禅"
    # 从没跳过：状态行照样成立（不落一个空位），空参数也绝不抛
    assert "尚未跳过" in scenarist.time_skip_prompt_block(None, streak=0, jumps=0,
                                                          blocks_since=None)


def test_time_skip_prompt_block_feeds_back_the_last_rejection():
    """上一次被挡下的理由要**回喂**（只说引擎自己的判定结果，不放宽任何闸）。

    模型照示例提「到了第二天早上」而被幅度上限拒时，它无从知道为什么——于是每块重提一遍，
    白烧 token 又推不动（实况：22:00 前开场的"等天亮"必然超上限）。回喂让模型自己换个
    小一点的数，或者干脆等条件成立。
    """
    plain = scenarist.time_skip_prompt_block(None, streak=0, jumps=0, blocks_since=None)
    assert "挡下" not in plain, "没有回喂时不多一个字节（既有提示词逐字节不变）"
    assert plain == scenarist.time_skip_prompt_block(None, streak=0, jumps=0,
                                                     blocks_since=None,
                                                     last_rejection=None)
    fed = scenarist.time_skip_prompt_block(
        None, streak=0, jumps=0, blocks_since=None,
        last_rejection="跳跃 660 分钟超过单次上限 480 分钟")
    assert "上一次提案被挡下了" in fed and "660" in fed and "480" in fed
    assert "宁可不跳" in fed, "回喂不改变「绝不轻易跳」这条纪律"


# ============================================================ D) 引擎集成
async def test_model_proposal_is_vetoed_when_precondition_fails(tmp_path):
    """**最重要的一条**：前件不成立时，模型说跳也不跳（引擎侧确定性否决）。

    证据有三层：① 模型**确实**每块都提了（叙述档被调用、提示词里有指令契约、产出里
    就是那条 `[[SKIP:…]]`）；② 一条跳时间行都没落；③ 每条提案都留下 gate=="precondition"
    的诊断记录。缺了①就证明不了"是引擎否决的模型"，缺了③就说不清为什么没跳。
    """
    eng = _build(tmp_path / "s", skip_params=_FAST, dynamics=None)
    eng.think_backend = StubBackend()          # 演示脚本：urge 1.2/0.3 交替 → 总有人想说
    await eng.open_scene()
    for _ in range(4):
        await eng.step(1)

    st = eng.time_skip_state()
    assert st["streak"] == 0, "有人 bid 过线 → 前件计数归零"
    assert eng.narrate_backend.calls >= 4, "场景每块都被咨询（钩子路径的既有口径）"
    assert all(scenarist.SKIP_OPEN in p[0]["content"]
               for p in eng.narrate_backend.prompts), "提示词里给了跳时间的契约"
    assert _jump_lines(await eng.messages()) == [], "引擎否决了模型的提案：一行都没落"
    assert st["jumps"] == 0
    assert st["records"] and all(r["gate"] == "precondition" for r in st["records"])
    assert all("前件" in r["reason"] for r in st["records"])
    assert_public_only(await eng._snapshot())


async def test_granted_jump_lands_a_narrator_line_with_clock_offset(tmp_path):
    """放行时：走既有叙述那条路落一行，正文 + 钟偏移字段都对，额外键在共享态里保留。"""
    eng = _build(tmp_path / "s", skip_params=_FAST)
    await eng.open_scene()
    await eng.step(1)

    msgs = await eng.messages()
    jumps = _jump_lines(msgs)
    assert len(jumps) == 1
    line = jumps[0]
    assert line["speaker"] == "场景" and line["speaker_type"] == "narrator"
    assert line["content"] == _SKIP_BODY, "指令标记绝不泄进正文"
    assert line["clock_jump_minutes"] == 30
    assert line["in_scene"] == "自习室"
    assert line.get("knows") is None, "叙述是所有人都能看见的（与既有叙述同一条路）"
    assert line["id"] in eng.narration_state()["narration_ids"], "跳时间也是叙述：可撤销/可改写"

    st = eng.time_skip_state()
    assert st["enabled"] is True and st["jumps"] == 1
    assert st["records"][-1]["gate"] == "ok"
    assert st["records"][-1]["granted"] == 30
    # 消息 dict 的额外键是被允许的（scenestore 只搬不解释，存档往返原样保留）
    bundle = scenestore.RuntimeBundle(transcript=[dict(line)])
    assert bundle.to_dict()["transcript"][0]["clock_jump_minutes"] == 30
    assert_public_only(await eng._snapshot())


async def test_jump_advances_nothing_in_the_engine_and_keeps_silence_pressure(tmp_path):
    """§4.3/§4.4：引擎**不推进任何钟**（钟归 worker），也**不重置沉默压力**。

    跳过 8 小时不等于"刚说过话"：跳时间行是叙述行（narrator），没有人因此开口，
    `turns_since_spoke` 只增不减，`last_speaker` 不动——不做这一条，"跳完时间大家
    就该重新热络起来"的错觉就会从数值侧渗进戏里。
    """
    eng = _build(tmp_path / "s", skip_params=_FAST)
    await eng.open_scene()
    await eng.step(1)
    assert _jump_lines(await eng.messages()), "这一块确实跳了"
    after_one = eng.dynamics_snapshot()
    assert all(s["turns_since_spoke"] == 1 for s in after_one.values())

    await eng.step(1)
    after_two = eng.dynamics_snapshot()
    assert all(s["turns_since_spoke"] == 2 for s in after_two.values()), \
        "跳时间之后沉默压力照旧累积（没有被当成「刚说过话」）"
    assert eng._dynamics.last_speaker is None, "没有人因跳时间而成为「刚说过话的人」"
    assert not [m for m in await eng.messages() if m.get("speaker_type") == "character"]
    assert eng._clock_now is None, "引擎不持钟、绝不自行推进（钟归 worker，§4.3）"


async def test_cooldown_gate_blocks_the_second_jump(tmp_path):
    """冷却闸：跳过一次之后 M 块内不许再跳（M=3 时第 2、3 块被拒，第 4 块放行）。"""
    eng = _build(tmp_path / "s",
                 skip_params={"streak_blocks": 1, "cooldown_blocks": 3, "max_jumps": 3})
    await eng.open_scene()
    for _ in range(4):
        await eng.step(1)
    st = eng.time_skip_state()
    assert [r["gate"] for r in st["records"]] == ["ok", "cooldown", "cooldown", "ok"]
    assert st["jumps"] == 2
    assert len(_jump_lines(await eng.messages())) == 2
    assert st["blocks_since"] == 0, "刚跳过 → 冷却重新起算"


async def test_frequency_gate_blocks_the_fourth_jump(tmp_path):
    """频次闸：整场最多 N 次——第 N+1 条提案一律被拒，且留下记录。"""
    eng = _build(tmp_path / "s",
                 skip_params={"streak_blocks": 1, "cooldown_blocks": 1, "max_jumps": 3})
    await eng.open_scene()
    for _ in range(5):
        await eng.step(1)
    st = eng.time_skip_state()
    assert [r["gate"] for r in st["records"]] == ["ok", "ok", "ok", "frequency",
                                                  "frequency"]
    assert st["jumps"] == 3 == len(_jump_lines(await eng.messages()))


async def test_amplitude_gate_rejects_an_over_long_proposal(tmp_path):
    """幅度闸（>上限 → **拒绝**，不截到上限）：截断会让正文与钟偏移对不上。

    正文写着「10 小时之后」而钟只走了 8 小时，是读者一眼看得出的矛盾；拒绝则整条
    提案作废（不落行、不推钟、留诊断），模型下一块自然会提一个更小的。
    """
    eng = _build(tmp_path / "s", skip_params=_FAST,
                 narrate="[[SKIP:600]] 10 小时之后，天亮了。")
    await eng.open_scene()
    for _ in range(2):
        await eng.step(1)
    st = eng.time_skip_state()
    assert [r["gate"] for r in st["records"]] == ["amplitude", "amplitude"]
    assert st["jumps"] == 0 and _jump_lines(await eng.messages()) == []
    assert "上限" in st["records"][0]["reason"]


async def test_the_engine_rejects_a_body_that_says_more_time_than_the_jump(tmp_path):
    """镜像矛盾（正文说一夜过去、指令只报 30 分钟）在引擎这一层也必须被拒。

    幅度闸取小者只消除了"申报大、正文小"那一半；反方向（`[[SKIP:30]] 10 小时之后…`）
    取小之后正文与钟仍然对不上——用户读到「10 小时之后，天亮了」而钟只走了 30 分钟，
    之后所有按钟点算的东西都会与正文打架。故整条提案作废：不落行、不推钟、留诊断。
    """
    eng = _build(tmp_path / "s", skip_params=_FAST,
                 narrate="[[SKIP:30]] 10 小时之后，天亮了。")
    await eng.open_scene()
    await eng.step(1)
    st = eng.time_skip_state()
    assert [r["gate"] for r in st["records"]] == ["amplitude"]
    assert st["jumps"] == 0 and _jump_lines(await eng.messages()) == []
    assert "正文" in st["records"][0]["reason"]


async def test_a_greedy_proposal_lands_the_smaller_magnitude(tmp_path):
    """小幅度优先落到引擎：模型申报 480 分钟、正文只说「30 分钟之后」→ 落 30。"""
    eng = _build(tmp_path / "s", skip_params=_FAST,
                 narrate="[[SKIP:480]] 30 分钟之后，他打了个盹，作业终于写完了。")
    await eng.open_scene()
    await eng.step(1)
    jumps = _jump_lines(await eng.messages())
    assert len(jumps) == 1 and jumps[0]["clock_jump_minutes"] == 30
    rec = eng.time_skip_state()["records"][-1]
    assert rec["requested"] == 480 and rec["granted"] == 30


async def test_rhythm_gate_keeps_the_existing_narration_cooldown(tmp_path):
    """跳时间不绕过既有的叙述冷却：冷却期内的提案被拒（gate=="rhythm"），不落行。"""
    eng = _build(tmp_path / "s", skip_params=_FAST, nudge={"cooldown_blocks": 5})
    await eng.open_scene()
    await eng.step(1)
    st = eng.time_skip_state()
    assert [r["gate"] for r in st["records"]] == ["rhythm"]
    assert st["jumps"] == 0 and _jump_lines(await eng.messages()) == []


async def test_feature_off_is_untouched_and_costs_no_extra_call(tmp_path):
    """**不开启时**：不认识这条指令（不解析、不拦、不跳），提示词里也没有这一块。

    关掉时引擎根本不认 `[[SKIP:…]]`，是为了让「不开启时对**任何**模型输出逐字节不变」
    这条硬约束成立（别在关着的路径上做半套处理）。咨询次数 = 块数，一次都不多。
    """
    eng = _build(tmp_path / "s", enabled=False, nudge={})   # 普通叙述照旧走既有触发线
    await eng.open_scene()
    for _ in range(3):
        await eng.step(1)
    st = eng.time_skip_state()
    assert st["enabled"] is False and st["records"] == [] and st["jumps"] == 0
    assert st["streak"] == 0, "关掉时不采样 bid（一个字节、一次读取都不多）"
    assert len(eng.narrate_backend.prompts) == 3, "每块一次咨询，一次不多"
    assert all("跳时间" not in p[0]["content"] for p in eng.narrate_backend.prompts)
    narr = [m for m in await eng.messages() if m.get("speaker_type") == "narrator"]
    assert narr == [], "关掉时既不跳、也不解析：普通叙述照旧受触发线约束"
    manual = await eng.narrate_now()        # 手动推进：一定落行，正文原样（含标记）
    assert manual is not None and "[[SKIP:30]]" in manual["content"], \
        "关掉时不解析：标记原样留着（对任何模型输出都逐字节不变）"
    assert "clock_jump_minutes" not in manual
    assert _jump_lines(await eng.messages()) == []


async def test_enabled_engine_states_the_contract_in_the_narrate_prompt(tmp_path):
    """开启时才把跳时间的契约写进叙述提示词（系统消息），且带当前前件状态。"""
    eng = _build(tmp_path / "s", skip_params=_FAST)
    await eng.open_scene()
    await eng.step(1)
    system = eng.narrate_backend.prompts[-1][0]["content"]
    assert "【可以跳时间】" in system and scenarist.SKIP_OPEN in system
    assert "连续" in system and "/1 块" in system


async def test_default_profile_never_jumps_while_the_cast_keeps_talking(tmp_path):
    """**错跳回归**（前件闸是那唯一一道"引擎否决"）：角色在一句接一句说话时，一条都不许跳。

    为什么必须单钉这一条：`dynamics.bid()` 里有 `recency_penalty`（刚开口的人 −0.6）与只涨
    一格的沉默压力——两人轮流说话时，说话人恰好被扣到 −0.6、另一人只沉默 1 块（≈0.3），
    于是"所有人的 bid 都低于 0.5"**每块都成立**、连续计数能无限涨。只查 bid 的前件闸在这种
    场面上等于不存在：模型只要照提示词提一条 `[[SKIP:30]]`，引擎就放行——用户看到两个正在
    聊天的人中间凭空插进「30 分钟之后，作业终于写完了」，虚拟钟被前推 30 分钟，之后所有按
    钟点算的东西（打烊边界、时间条件钩子、场景压力）全被挪到错误时刻（§4.2 说的"跳错"）。

    本用例用**生产档**跑：生产开口阈值（0.1）让角色真的轮流开口、默认动力学（沉默压力/
    recency 都开着）、默认跳时间参数（K=3），叙述档每块都提同一条提案。断言三层：① 角色
    确实在一块接一块说话（否则这个用例什么也没钉）；② 一行跳时间都没落；③ 每条提案都留下
    gate=="precondition" 的记录（是**引擎**否决了模型，不是模型没提）。
    """
    eng = _build(tmp_path / "s", skip_params=scenarist.TimeSkipParams(),
                 dynamics=None, nudge=_NO_RHYTHM, bid_yaml=_PROD_BID,
                 speak_lines=["（我们接着看下一题吧。）", "（好，这题我来。）"])
    await eng.open_scene()
    for _ in range(14):
        await eng.step(1)

    msgs = await eng.messages()
    spoken = [m for m in msgs if m.get("speaker_type") == "character"]
    assert len(spoken) >= 8, f"角色确实在一块接一块地对话（本用例的前提），实际 {len(spoken)}"
    st = eng.time_skip_state()
    assert st["jumps"] == 0 and _jump_lines(msgs) == [], "全员都在说话时，一次都不许跳"
    assert st["records"], "模型每块都提了提案（否则证明不了是引擎否决的）"
    assert all(r["gate"] == "precondition" for r in st["records"]), \
        f"被前件闸挡下（没人把话说完了）：{[r['gate'] for r in st['records']]}"
    assert eng.narrate_backend.calls >= 14, "场景每块都被咨询（钩子路径的既有口径）"


async def test_a_real_lull_lets_the_jump_through_after_k_quiet_blocks(tmp_path):
    """前件闸的**正例**：真·全员无话可说（没人开口 + 大家都不想说话）攒够 K 块才放行。

    与上一条是同一把尺的两端。生产档 + 默认动力学下沉默压力每块 +0.35，于是"全员 bid < 0.5"
    的窗口天生只有开头几块（再往后必然有人被顶过开口阈值、故事自然重新开口）——K=3 正好卡在
    这个窗口的最后一格：第 3 块跳成，第 4 块起角色重新开口（这时前件闸自然关上）。
    """
    eng = _build(tmp_path / "s", skip_params=scenarist.TimeSkipParams(),
                 dynamics=None, nudge=_NO_RHYTHM, bid_yaml=_PROD_BID,
                 think={"aroused": 0.0, "obligation_fulfilled": [], "goal_progress": 0.0,
                        "addressed": None, "impression_of_speaker": None, "urge": -1.0},
                 speak_lines=["（……）"])
    await eng.open_scene()
    for _ in range(3):
        await eng.step(1)

    msgs = await eng.messages()
    assert not [m for m in msgs if m.get("speaker_type") == "character"], \
        "整段没人开口（正例的前提：真·无话可说）"
    st = eng.time_skip_state()
    assert [r["gate"] for r in st["records"]] == ["precondition", "precondition", "ok"]
    assert st["jumps"] == 1 and len(_jump_lines(msgs)) == 1
    assert _jump_lines(msgs)[0]["clock_jump_minutes"] == 30

    await eng.step(1)                        # 第 4 块：沉默压力把 bid 顶过开口阈值
    assert eng.time_skip_state()["streak"] == 0, "有人重新开口 → 前件计数归零（闸门自己关上）"


async def test_rhythm_gate_includes_the_activity_trigger_line(tmp_path):
    """节奏闸 = 既有的叙述**冷却 + 触发线**（§4.2 末条的"不绕过它"），不是只看冷却。

    触发线是**推进活跃度**的执行器（少/中/多/极多经 effective_params 缩放它）。若跳时间只
    继承冷却那一半，用户把活跃度调到"少"也照样会在"引擎自己判定此刻不该叙述"的块里被推着
    跳时间——而那条行会自动上屏、推钟、进所有人的上下文，成了唯一能无视触发线硬落叙述的
    通道（与"有 hook 就每块都叙述"是同一个洞，那个洞刚被堵上）。
    """
    eng = _build(tmp_path / "s", skip_params=_FAST,
                 nudge={"cooldown_blocks": 0, "threshold": 99.0})
    await eng.open_scene()
    await eng.step(1)
    st = eng.time_skip_state()
    assert [r["gate"] for r in st["records"]] == ["rhythm"]
    assert "冷却" in st["records"][0]["reason"]
    assert st["jumps"] == 0 and _jump_lines(await eng.messages()) == []
    # 对照：同一块、同一提案，只把触发线放开（其余全同）→ 落行
    ok = _build(tmp_path / "s2", skip_params=_FAST, nudge=_NO_RHYTHM)
    await ok.open_scene()
    await ok.step(1)
    assert ok.time_skip_state()["jumps"] == 1


async def test_a_jump_restarts_the_precondition_count(tmp_path):
    """跳过一次后前件重新攒 K 块（活动已完结）：下一块只有 1 块，前件不成立。"""
    eng = _build(tmp_path / "s",
                 skip_params={"streak_blocks": 3, "cooldown_blocks": 0, "max_jumps": 3})
    await eng.open_scene()
    for _ in range(6):
        await eng.step(1)
    st = eng.time_skip_state()
    # 攒够 3 块才第一次跳；跳完计数清零 → 又得重新攒 3 块
    gates = [r["gate"] for r in st["records"]]
    assert gates == ["precondition", "precondition", "ok",
                     "precondition", "precondition", "ok"]
    assert st["jumps"] == 2 and st["streak"] == 0, "刚落过那一跳之后计数又从头起算"


async def test_a_human_line_breaks_the_silent_run(tmp_path):
    """用户刚说了话 → 这一块不算「无话可说」（哪怕他谁也没点名）。

    跳时间跳过的是"没人说话的那段"。用户插了一句话而角色还没接，跳过时间等于把用户的话
    跳过去——点名的情形由 bid 里的欠答义务兜住，没点名的情形只有"本块有人开口"这一半能
    看见（人类这行不是角色台词，不进 dynamics，故由引擎按新增消息单独判定）。
    """
    quiet = _build(tmp_path / "a", skip_params={"streak_blocks": 1, "cooldown_blocks": 0,
                                                "max_jumps": 3})
    await quiet.open_scene()
    await quiet.step(1)
    assert quiet.time_skip_state()["jumps"] == 1, "没人开口 → K=1 即成立（对照）"

    spoke = _build(tmp_path / "b", skip_params={"streak_blocks": 1, "cooldown_blocks": 0,
                                                "max_jumps": 3})
    await spoke.open_scene()
    await spoke.say("（窗外的雨还在下。）")      # 人类插话：没点名任何人
    await spoke.step(1)
    st = spoke.time_skip_state()
    assert st["streak"] == 0, "用户刚开口 → 这一块不算无话可说"
    assert st["jumps"] == 0 and _jump_lines(await spoke.messages()) == []
    assert [r["gate"] for r in st["records"]] == ["precondition"]
    await spoke.step(1)                          # 下一块没人开口 → 重新攒（K=1 即成）
    assert spoke.time_skip_state()["jumps"] == 1


async def test_malformed_directive_leaves_a_diagnostic(tmp_path):
    """畸形指令绝不静默：正文里不留语法，诊断留在 time_skip_state()["error"] 里。"""
    eng = _build(tmp_path / "s", skip_params=_FAST,
                 narrate="[[SKIP:abc]] 30 分钟之后，作业终于写完了。")
    await eng.open_scene()
    await eng.step(1)
    st = eng.time_skip_state()
    assert st["error"] and "[[SKIP" in st["error"]
    assert st["records"] == [] and st["jumps"] == 0
    assert _jump_lines(await eng.messages()) == []


async def test_switch_can_be_flipped_at_runtime(tmp_path):
    """开关可运行期翻转（与 set_speak_stream 同纪律）：关着时一个字节不多，开了即生效。"""
    eng = _build(tmp_path / "s", enabled=False, nudge={})   # 关着时普通叙述照旧受触发线约束
    await eng.open_scene()
    first = await eng.step(1)
    assert all(scenarist.SKIP_OPEN not in e["payload"].get("content", "")
               for e in first if e["type"] == "block_spoken")
    assert all("跳时间" not in p[0]["content"] for p in eng.narrate_backend.prompts)

    eng.set_time_skip_enabled(True)
    await eng.step(1)
    assert "跳时间" in eng.narrate_backend.prompts[-1][0]["content"], "开启后提示词即带契约"
    assert eng.time_skip_state()["enabled"] is True
    eng.set_time_skip_enabled(False)
    assert eng.time_skip_state()["enabled"] is False


async def test_skip_state_is_a_read_only_snapshot(tmp_path):
    """time_skip_state() 是只读快照：取回后改动它绝不污染引擎内部（供 GUI/日志）。"""
    eng = _build(tmp_path / "s", skip_params=_FAST)
    await eng.open_scene()
    await eng.step(1)
    first = eng.time_skip_state()
    first["jumps"] = 999
    first["records"].clear()
    assert eng.time_skip_state()["jumps"] == 1
    assert eng.time_skip_state()["records"], "记录照旧在（返回的是浅拷贝列表）"


async def test_reset_scene_runtime_zeroes_the_time_skip_bookkeeping(tmp_path):
    """重置场景 = 这一场退回开演前：跳时间的四个计数器跟着归零（§4.2 写的是"整场戏"）。

    重置（§3.4/§5）把对话/动力学/叙述判据全部退回开演前，对用户就是**新的一场**（对白区
    清空、dynamics 重新播种）。漏掉跳时间这几个计数器有两个方向的可见事故：① 跳过 3 次再
    重置 → 频次闸永远说"已跳 3/3"，这场新戏再也跳不成（功能静默死掉，而 time_skip_state()
    还报着上一场的旧账）；② 冷却计数没退回"刚开演"，重置后立刻就能跳。
    """
    eng = _build(tmp_path / "s",
                 skip_params={"streak_blocks": 1, "cooldown_blocks": 5, "max_jumps": 3})
    await eng.open_scene()
    await eng.step(1)
    assert eng.time_skip_state()["jumps"] == 1
    await eng.reset_scene_runtime()
    st = eng.time_skip_state()
    assert (st["jumps"], st["blocks_since"], st["streak"], st["records"]) \
        == (0, None, 0, []), f"新的一场：额度/冷却/前件计数/诊断全部退回开演前，实际 {st}"
    await eng.step(1)
    assert eng.time_skip_state()["jumps"] == 1, "新一场的头一块就跳得成（额度与冷却都是新的）"


async def test_retracting_a_jump_line_gives_the_slot_back(tmp_path):
    """撤回一条跳时间行 = 那一跳没发生过：频次额度退还（与 worker 收回钟同一口径）。

    §4.2 的频次闸记的是"这一场**发生过**几次跳跃"。被撤回的那一行连同它的钟偏移都不存在
    了（worker 侧同步把分钟从虚拟钟里收回），额度继续被它占着就成了"用户撤销之后再也跳不
    成"的静默死掉。**冷却**是节奏计数器（与 blocks/turn 同类），照旧不倒退——绝不因撤回
    白送一次立即推进。
    """
    eng = _build(tmp_path / "s",
                 skip_params={"streak_blocks": 1, "cooldown_blocks": 0, "max_jumps": 1})
    await eng.open_scene()
    await eng.step(1)
    assert eng.time_skip_state()["jumps"] == 1
    await eng.retract(_jump_lines(await eng.messages())[0]["id"])
    assert eng.time_skip_state()["jumps"] == 0, "那一跳作废 → 额度退还"
    await eng.step(1)
    assert eng.time_skip_state()["jumps"] == 1, "额度退还后才跳得成（否则本场永远封顶）"


async def test_restore_rebuilds_the_frequency_budget_from_the_transcript(tmp_path):
    """续演（§5）不是新的一场：频次/冷却接着转录里的旧账，不许反复续演刷额度。

    续演把 sidecar 转录追回共享态，其中可能已有跳时间行（钟前推过、提示词也按那账报过）。
    若计数器退回初值，同一场戏就能靠反复续演把"整场最多 3 次"变成 3×续演次数——而用户
    看到的是一份转录里钟被推了一次又一次（§4.2 的频次闸与冷却闸一起被冲掉）。故续演后：
    已跳次数 = 转录里活着的跳时间行数；只要还有活着的那条，冷却就按"刚刚推过"起算
    （宁可跳不成）。
    """
    root = tmp_path / "s"
    fast = {"streak_blocks": 1, "cooldown_blocks": 5, "max_jumps": 3}
    eng = _build(root, skip_params=fast)
    await eng.open_scene()
    await eng.step(1)
    assert eng.time_skip_state()["jumps"] == 1
    assert await eng.save_scene_state() is not None

    eng2 = _build(root, skip_params=fast)
    await eng2.open_scene()
    assert await eng2.restore_scene_state() is True
    st = eng2.time_skip_state()
    assert st["jumps"] == 1, "转录里那条跳时间行要计入本场额度（不许白送一整套预算）"
    assert st["blocks_since"] == 0, "刚续演 → 冷却从'刚刚推过'起算（宁可跳不成）"
    await eng2.step(1)
    assert [r["gate"] for r in eng2.time_skip_state()["records"]] == ["cooldown"], \
        "续演后头一块就跳 → 被冷却闸挡下"


async def test_a_rejected_proposal_is_fed_back_into_the_prompt(tmp_path):
    """被挡下的理由回喂进提示词（模型否则每块原样重提同一条，白烧 token 又推不动）。"""
    eng = _build(tmp_path / "s", skip_params=_FAST,
                 narrate="[[SKIP:600]] 10 小时之后，天亮了。")
    await eng.open_scene()
    await eng.step(1)                            # 第 1 块：提案被挡下（此刻的提示词还看不到）
    await eng.step(1)                            # 第 2 块：提示词里带上一次的理由
    assert eng.time_skip_state()["records"][-1]["gate"] == "amplitude"
    system = eng.narrate_backend.prompts[-1][0]["content"]
    assert "上一次提案被挡下了" in system and "上限" in system
    # 关掉时一个字节都不加（不开启时逐字节不变）
    off = _build(tmp_path / "s2", enabled=False)
    await off.open_scene()
    await off.step(1)
    assert "上一次提案被挡下了" not in off.narrate_backend.prompts[-1][0]["content"]
