"""场景钩子（spec §4）：纯数据模型 + 纯函数记账/渲染辅助。

SCENE agent 自己判断条件是否成立（不额外发起 LLM 调用），因此本模块只提供
数据模型与确定性的纯函数：描述（UI 列表用）、校验、延时角色动作倒计时、
场景补丁应用、以及给场景 agent 提示词用的钩子清单块。

本模块不含 LLM、不做任何 IO；所有函数都不改动入参。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Iterable, Literal

# ---------------------------------------------------------------------------
# 类型
# ---------------------------------------------------------------------------
EventKind = Literal["context", "character", "scene"]
CharacterAction = Literal["add", "remove", "mute_turns", "mute", "unmute"]

_EVENT_KINDS: tuple[str, ...] = ("context", "character", "scene")
_CHARACTER_ACTIONS: tuple[str, ...] = ("add", "remove", "mute_turns", "mute", "unmute")

_ACTION_LABELS: dict[str, str] = {
    "add": "加入",
    "remove": "离场",
    "mute_turns": "静默",
    "mute": "长期静默",
    "unmute": "解除静默",
}

#: 合法钩子 id 的字符集：字母/数字/下划线/点/连字符（与引擎解析 `[[HOOK:<id>]]` 时用的
#: `engine._HOOK_ID_RE` **逐字符相同**——两处各写一套就会出现「校验放行但永远打不响」的
#: 钩子；测试 `test_hook_id_rule_is_literally_the_engines_rule` 锁死这一点）。
#: 注意本判据不先 strip：引擎是先 strip 再按原 id 查表，故带前后空白的 id 本就打不响，
#: 在这里直接算非法（详见 valid_hook_id）。
VALID_HOOK_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def valid_hook_id(value: str) -> bool:
    """id 是否能被引擎的 `[[HOOK:<id>]]` 解析器认出来并按原样查表命中。

    空串 / 非字符串 / 含字符集外字符（中文、空格、标点、全角字符等）→ False。
    `hooks_prompt_block` 与 `validate` 共用本判据，保证「提示词里出现的 id 一定打得响」。
    """
    text = value if isinstance(value, str) else ""
    return bool(text) and VALID_HOOK_ID_RE.match(text) is not None


#: 场景自身字段的**白名单**（唯一规则来源）：场景事件（hook.scene_patch）与场景编辑工具
#: 只许改这些键。刻意是白名单而非黑名单——文件名/路径是磁盘身份（归 GUI/装载器），
#: 任何不在表里的键一律丢弃。引擎 `engine._SCENE_HOOK_KEYS` 镜像同一张表
#: （引擎就地改活对象，故不 import 本函数；测试 `test_scene_patch_whitelist_...` 锁死两者相等）。
SCENE_PATCH_ALLOWED_KEYS: tuple[str, ...] = ("name", "description", "background",
                                             "plot_direction", "date")

#: 场景的磁盘身份键：改名会切断既有引用，**永不写入**。白名单里本就没有它们，这里是
#: 第二道防线（万一调用方把 allowed 传错，也不至于把场景写成别的文件）。
SCENE_IDENTITY_KEYS: frozenset[str] = frozenset({"path", "filename"})


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
@dataclass
class Hook:
    """一条场景钩子：条件成立时执行一个事件。

    事件分三类（event_kind），各自只使用对应的载荷字段：
    - context：context_text 被写入 transcript；
    - character：character_name / action / turns 改变在场角色；
    - scene：scene_patch 里的字段被应用到场景（如改名、改描述）。
    """

    id: str
    condition: str
    event_kind: EventKind
    visible: bool = True  # 显式=True / 隐式=False
    # 以下为按 kind 取用的载荷：
    context_text: str = ""                       # context 事件：写入的文本
    character_name: str = ""                     # character 事件：角色名
    action: CharacterAction = "add"              # character 事件：动作
    turns: int = 0                               # character 事件：静默轮数
    scene_patch: dict = field(default_factory=dict)  # scene 事件：要改的字段
    enabled: bool = True                         # 用户可停用而不删除
    note: str = ""                               # 用户可见的备注/标签


@dataclass
class PendingCharacterAction:
    """一条已判定成立、但需延迟 fire_after_rounds 轮才执行的角色动作。"""

    character_name: str
    action: CharacterAction
    turns: int
    fire_after_rounds: int
    notify: list[str] = field(default_factory=list)
    notify_text: str = ""
    visible: bool = True


# ---------------------------------------------------------------------------
# 描述
# ---------------------------------------------------------------------------
def _visibility_label(hook: Hook) -> str:
    return "显式" if hook.visible else "隐式"


def _character_effect(hook: Hook) -> str:
    name = hook.character_name.strip() or "（未填角色）"
    label = _ACTION_LABELS.get(hook.action, hook.action)
    effect = f"让 {name} {label}"
    if hook.action == "mute_turns":
        effect += f" {hook.turns} 轮"
    return effect


def _context_effect(hook: Hook) -> str:
    text = hook.context_text.strip() or "（未填文本）"
    return f"写入背景文本「{text}」"


def _scene_effect(hook: Hook) -> str:
    if not hook.scene_patch:
        return "改写场景：（未填字段）"
    parts = "、".join(f"{k}={v}" for k, v in hook.scene_patch.items())
    return f"改写场景：{parts}"


def describe(hook: Hook) -> str:
    """UI 列表用的一句话中文描述，例如：
    「若『甲得知真相』→ 让 甲 离场（显式）」。
    """
    condition = hook.condition.strip() or "（未填条件）"
    if hook.event_kind == "character":
        effect = _character_effect(hook)
    elif hook.event_kind == "context":
        effect = _context_effect(hook)
    elif hook.event_kind == "scene":
        effect = _scene_effect(hook)
    else:
        effect = f"未知事件（{hook.event_kind}）"

    line = f"若『{condition}』→ {effect}（{_visibility_label(hook)}）"
    if hook.note:
        line = f"〔{hook.note}〕{line}"
    if not hook.enabled:
        line += "（已停用）"
    return line


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def validate(hook: Hook, siblings: Iterable[Hook] = ()) -> list[str]:
    """返回中文错误串列表；空列表表示合法。

    id 先校验：它是引擎 `[[HOOK:<id>]]` 的查表键，id 不合法（空/含字符集外字符/与同场景
    另一条撞名）的钩子**永远打不响**——必须在作者编辑时就拦住，而不是等它被写进提示词、
    模型照着念、最后只留一行不点名的"畸形指令"日志。

    siblings：同一场景里的**其它**钩子（不含本对象本身），仅用于查重复 id；缺省 () 即
    只做单条校验（引擎逐条 validate 的旧调用不受影响）。
    """
    errors: list[str] = []
    hook_id = hook.id if isinstance(hook.id, str) else ""

    if not hook_id.strip():
        errors.append("钩子 id 不能为空（id 是场景报告钩子成立时要写的那个名字）。")
    elif not valid_hook_id(hook_id):
        errors.append(
            f"钩子 id「{hook_id}」含非法字符：只能用字母、数字、下划线、点、连字符，"
            f"且不含空白——这样的 id 引擎永远认不出来。")
    elif any(h is not hook and getattr(h, "id", None) == hook_id for h in siblings):
        errors.append(f"钩子 id 重复：{hook_id}（同一场景里的钩子 id 必须唯一）。")

    if not hook.condition.strip():
        errors.append("触发条件不能为空。")

    if hook.event_kind not in _EVENT_KINDS:
        errors.append(f"未知的事件类型：{hook.event_kind}（应为 context/character/scene）。")
        return errors

    if hook.event_kind == "context":
        if not hook.context_text.strip():
            errors.append("上下文事件必须填写要写入的文本。")
    elif hook.event_kind == "character":
        if not hook.character_name.strip():
            errors.append("角色事件必须填写角色名。")
        if hook.action not in _CHARACTER_ACTIONS:
            errors.append(
                f"未知的角色动作：{hook.action}"
                f"（应为 add/remove/mute_turns/mute/unmute）。"
            )
        elif hook.action == "mute_turns" and hook.turns <= 0:
            errors.append("静默指定轮数时，轮数必须大于 0。")
    else:  # scene
        if not hook.scene_patch:
            errors.append("场景事件必须至少指定一个要修改的字段。")

    return errors


# ---------------------------------------------------------------------------
# 延时角色动作
# ---------------------------------------------------------------------------
def tick(
    pending: list[PendingCharacterAction],
    rounds_elapsed: int = 1,
) -> tuple[list[PendingCharacterAction], list[PendingCharacterAction]]:
    """把所有待触发动作的 fire_after_rounds 扣减 rounds_elapsed。

    返回 (fired, remaining)，两者都保持原有相对顺序；扣减后 <= 0 的进入 fired，
    其余留在 remaining。触发项携带的是扣减后的值（<= 0）。入参不被改动。
    """
    fired: list[PendingCharacterAction] = []
    remaining: list[PendingCharacterAction] = []
    for item in pending:
        updated = replace(item, fire_after_rounds=item.fire_after_rounds - rounds_elapsed)
        if updated.fire_after_rounds <= 0:
            fired.append(updated)
        else:
            remaining.append(updated)
    return fired, remaining


# ---------------------------------------------------------------------------
# 场景补丁
# ---------------------------------------------------------------------------
def apply_scene_patch(scene_dict: dict, patch: dict,
                      allowed: Iterable[str]) -> tuple[dict, list[str]]:
    """白名单式场景补丁：返回 (新 dict, 被拒键列表)，绝不改动 scene_dict 或 patch。

    **只应用 allowed 里的键**（`allowed` 用 SCENE_PATCH_ALLOWED_KEYS——引擎
    `_SCENE_HOOK_KEYS` 镜像同一张表；测试锁死两者相等）。不在 allowed 里的键（含
    path/filename 这类磁盘身份、以及任何未知键）一律**丢弃并回报**，由调用方决定怎么
    记诊断——旧版是黑名单（只拦 path/filename，其余未知键照单全收），与引擎的白名单
    政策相反：那会让调用方以为改成功了，实际引擎全忽略。

    语义边界：
    · scene_dict 是**底本**，非 allowed 的键原样保留（本函数只挑 patch，绝不删场景键）；
    · allowed 里即便误写了 SCENE_IDENTITY_KEYS（path/filename），第二道防线仍拒绝写入；
    · rejected 按 patch 顺序去重（同一个键只报一次）。
    """
    allowed_set = set(allowed or ())
    out = dict(scene_dict)
    rejected: list[str] = []
    for key, value in (patch or {}).items():
        if key not in allowed_set or key in SCENE_IDENTITY_KEYS:
            rejected.append(key)
            continue
        out[key] = value
    return out, list(dict.fromkeys(rejected))


# ---------------------------------------------------------------------------
# 提示词块
# ---------------------------------------------------------------------------
def hooks_prompt_block(
    hooks: list[Hook],
    pending: list[PendingCharacterAction],
) -> str:
    """渲染给场景 agent 提示词用的中文块：逐条列出启用中的钩子条件与效果，
    以及尚未到期的延时角色动作，供场景每轮自行判断条件是否成立。

    只列**启用中且 id 合法**（valid_hook_id）的钩子：id 不合法的钩子引擎永远查不到，
    写进提示词只会让模型照着一个打不响的名字报告——作者侧的报错由 validate 给出。
    全部被剔除（且无待触发动作）时返回空串，与空输入同。
    """
    active = [h for h in hooks if h.enabled and valid_hook_id(h.id)]
    if not active and not pending:
        return ""

    lines: list[str] = []
    if active:
        lines.append("【场景钩子】每轮请判断下列条件是否成立，成立即按其中效果执行：")
        for hook in active:
            lines.append(f"- [{hook.id}] {describe(hook)}")

    if pending:
        if lines:
            lines.append("")
        lines.append("【待触发的延时角色动作】已判定成立，按剩余轮数到期后执行：")
        for item in pending:
            label = _ACTION_LABELS.get(item.action, item.action)
            detail = f"- {item.character_name} {label}"
            if item.action == "mute_turns":
                detail += f" {item.turns} 轮"
            detail += f"：还需 {item.fire_after_rounds} 轮"
            if item.notify:
                detail += f"；需通知：{'、'.join(item.notify)}"
            if item.notify_text:
                detail += f"；说明：{item.notify_text}"
            lines.append(detail)

    return "\n".join(lines)
