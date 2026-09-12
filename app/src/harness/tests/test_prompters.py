import inspect
import re

from harness.prompters import (
    TIME_CONDITION_RULE, _NARRATE_SYSTEM, _corpus_section, build_narrate_messages,
    build_speak_messages, build_summary_messages, build_think_messages,
)
from harness.schemas import CharacterCard, Corpus


def _card() -> CharacterCard:
    return CharacterCard(name="乙", personality={"描述": "冷静、观察多"})


def test_think_messages_contain_json_keyword_and_fields():
    msgs = build_think_messages(_card(), view_text="[1] 丙: 你好",
                                last_chunk_text="你好", scene_text="餐厅，10 点打烊")
    blob = msgs[0]["content"] + msgs[1]["content"]
    assert "json" in blob.lower()          # DeepSeek json_object 硬性要求
    assert '"urge"' in blob                # 输出字段示例
    assert '"impression_of_speaker"' in blob


def test_think_system_mentions_privacy_rule():
    msgs = build_think_messages(_card(), view_text="", last_chunk_text="",
                                scene_text="")
    assert "只输出" in msgs[0]["content"]  # 强调除 urge 外不外泄内心


def test_think_messages_insert_private_state_and_impressions_sections():
    """私有态/印象回喂：非空时插「近期状态」/「对ta印象」小节，且 JSON 收尾仍在。"""
    msgs = build_think_messages(
        _card(), view_text="[1] 丙: 你好", last_chunk_text="你好",
        scene_text="餐厅，10 点打烊",
        state_text="唤醒 0.50 ｜ 目标推进 0.20 ｜ 被点名 丙 ｜ 兑现义务 无",
        impressions_text="对 丙 的印象：话里有话")
    user = msgs[1]["content"]
    assert "【你的近期状态】" in user
    assert "唤醒 0.50" in user and "兑现义务 无" in user
    assert "【你对ta的印象】" in user
    assert "对 丙 的印象：话里有话" in user
    # 小节必须位于收尾指令之前，不污染「只输出 JSON」的锁死格式
    assert user.index("【你的近期状态】") < user.index("请输出你的 think JSON")
    blob = msgs[0]["content"] + user
    assert "json" in blob.lower()          # DeepSeek json_object 硬性要求
    assert '"urge"' in blob                # schema 字段仍在
    assert '"impression_of_speaker"' in blob


def test_think_messages_without_private_state_are_backward_compatible():
    """缺省 state/impressions：与旧签名逐字节一致，且只有 state 时不给空印象小节。"""
    a = build_think_messages(_card(), view_text="v", last_chunk_text="c",
                             scene_text="s")
    b = build_think_messages(_card(), view_text="v", last_chunk_text="c",
                             scene_text="s", state_text="", impressions_text="")
    assert a == b
    blob = a[0]["content"] + a[1]["content"]
    assert '"urge"' in blob and '"impression_of_speaker"' in blob
    assert "【你的近期状态】" not in blob and "【你对ta的印象】" not in blob

    # 只给 state_text：不该出现空的印象小节
    c = build_think_messages(_card(), view_text="v", last_chunk_text="c",
                             scene_text="s", state_text="唤醒 0.50")
    assert "【你的近期状态】" in c[1]["content"]
    assert "【你对ta的印象】" not in c[1]["content"]
    assert "请输出你的 think JSON" in c[1]["content"]


def test_think_system_requires_addressing_who_and_marks_self_continuation():
    """fix：think 必须明确在回应谁的哪句话；自己延续要注『自续』，不得当匿名来句接。"""
    msgs = build_think_messages(_card(), view_text="[1] 丙: 你好",
                                last_chunk_text="[1] 丙: 你好", scene_text="s")
    system = msgs[0]["content"]
    assert "回应" in system and "匿名" in system and "自续" in system


def test_think_user_template_places_attributed_last_chunk_into_built_message():
    """fix：归属过的刚听到一句以 `[id] 说话人: 内容` 原样进入 built 用户消息（模型所见）。"""
    view = "[0] 导演: （开场）\n[1] 丙: 今晚这桌我请。"
    last_line = "[1] 丙: 今晚这桌我请。"
    msgs = build_think_messages(_card(), view_text=view,
                                last_chunk_text=last_line, scene_text="s")
    user = msgs[1]["content"]
    # 「此刻最新的一句」小节正文（到首个空行前）应含归属行，且小节措辞点明要看说话人
    section = user.partition("【此刻最新的一句】")[2].split("\n\n")[0]
    assert last_line in section
    assert "说话人" in section


def test_speak_view_lines_keep_speaker_tags_and_no_fabricated_reply():
    """speak 视图行带说话者；系统温言提示不要硬接显然说给别人的话。"""
    card = CharacterCard(name="丙", personality={"描述": "锐利"})
    view = "[0] 导演: （餐厅开场）\n[1] 乙: 这话是说给丁的。"
    msgs = build_speak_messages(card, view_text=view, scene_text="餐厅")
    user = msgs[1]["content"]
    assert "[0] 导演:" in user and "[1] 乙:" in user
    assert "硬接" in msgs[0]["content"]


def test_think_own_last_rule_added_and_json_intact():
    """own_last=True：明确告知刚听到的是自己说的话——别回答自己，冲动应保持低。

    追加的说话权提醒不得破坏锁死 JSON/收尾：仍含 json 关键词与全部 schema 字段。
    """
    msgs = build_think_messages(_card(), view_text="[1] 丙: 你好",
                                last_chunk_text="[1] 乙: 我的话",
                                scene_text="s", own_last=True)
    blob = msgs[0]["content"] + msgs[1]["content"]
    assert "刚听到的是你自己说的话" in blob
    assert "不要把它当作别人要你回应的话" in blob
    assert "冲动应保持低" in blob
    # 锁死格式不受影响
    assert "json" in blob.lower()
    assert '"urge"' in blob and '"impression_of_speaker"' in blob
    assert "请输出你的 think JSON" in msgs[1]["content"]


def test_think_own_last_false_is_byte_identical_to_before():
    """own_last=False 缺省路径：与不传参完全一致，绝不注入自己的说话权提醒。"""
    base = build_think_messages(_card(), view_text="v", last_chunk_text="c",
                                scene_text="s")
    same = build_think_messages(_card(), view_text="v", last_chunk_text="c",
                                scene_text="s", own_last=False)
    assert same == base
    blob = base[0]["content"] + base[1]["content"]
    assert "刚听到的是你自己说的话" not in blob


def test_think_system_forbids_repeating_self_or_others():
    """三层刹车 A：think 硬性规则禁止重复自己/他人说过的话（含意思相近），
    无可说新话时不抬开口冲动。锁死 JSON / 字段仍完整。"""
    msgs = build_think_messages(_card(), view_text="[1] 丙: 你好",
                                last_chunk_text="[1] 丙: 你好", scene_text="s")
    system = msgs[0]["content"]
    assert "不要重复自己或他人已经说过的话" in system
    assert "意思相近" in system
    assert "没有新的、实质的话可说" in system and "开口冲动" in system
    blob = system + msgs[1]["content"]
    assert "json" in blob.lower()
    assert '"urge"' in blob and '"impression_of_speaker"' in blob
    assert "请输出你的 think JSON" in msgs[1]["content"]


def test_speak_system_forbids_echoing_self_or_any_character():
    """三层刹车 A：speak 系统规则禁止复读/几乎复读自己或任何角色说过的话；
    没有新内容宁可沉默。own_previous 规则与缺省路径不受影响。"""
    card = CharacterCard(name="乙", personality={"描述": "冷静"})
    base = build_speak_messages(card, view_text="v", scene_text="s")
    system = base[0]["content"]
    assert "复读" in system and "几乎复读" in system
    assert "任何角色已说过的话" in system
    assert "没有新内容可说" in system and "宁可沉默" in system
    # 缺省与显式 False 仍逐字节一致
    assert build_speak_messages(card, view_text="v", scene_text="s",
                                own_previous=False) == base


def test_speak_own_previous_rule_only_when_flag():
    """own_previous=True：speak 被告知上一句是自己——开口只能是自然续说，不许回应/复读。"""
    card = CharacterCard(name="乙", personality={"描述": "冷静"})
    base = build_speak_messages(card, view_text="v", scene_text="s")
    marked = build_speak_messages(card, view_text="v", scene_text="s",
                                  own_previous=True)
    marked_blob = marked[0]["content"] + marked[1]["content"]
    base_blob = base[0]["content"] + base[1]["content"]
    assert "上一句是你自己说的" in marked_blob
    assert "自然的延续" in marked_blob
    assert "回应对手的句式" in marked_blob
    # 缺省/显式 False 不注入，与旧行为逐字节一致
    assert build_speak_messages(card, view_text="v", scene_text="s",
                                own_previous=False) == base
    assert "上一句是你自己说的" not in base_blob


def test_think_system_no_imitation_and_self_third_person_forbidden():
    """身份锁：think 也必须守"自己的口吻 + 绝不模仿他人句式 + 绝不用第三人称称呼自己"。"""
    msgs = build_think_messages(_card(), view_text="[1] 丙: 你好",
                                last_chunk_text="[1] 丙: 你好", scene_text="s")
    system = msgs[0]["content"]
    assert "绝不模仿" in system and "句式/口头禅" in system
    assert "其他角色" in system
    assert "第三人称" in system and "绝不把自己当作对方" in system
    assert "你的角色名 乙" in system


def test_speak_system_no_imitation_and_self_third_person_forbidden():
    """身份锁（speak 侧）：不得套他人话头自答；不把旧话当新话复读——宁可沉默。"""
    card = CharacterCard(name="乙", personality={"描述": "冷静"})
    msgs = build_speak_messages(card, view_text="[1] 丙: 你好", scene_text="s")
    system = msgs[0]["content"]
    assert "绝不模仿" in system and "句式/口头禅" in system
    assert "其他角色" in system and "绝不把自己当作对方" in system
    assert "第三人称" in system
    assert "角色名 乙" in system


def test_speak_messages_insert_own_state_and_impressions_section():
    """speak 补喂本人近期状态/想法：非空时在开口指令前插小节，身份/反复读规则不变。"""
    card = CharacterCard(name="乙", personality={"描述": "冷静"})
    msgs = build_speak_messages(
        card, view_text="[1] 丙: 你好", scene_text="餐厅",
        state_text="唤醒 0.60 ｜ 目标推进 0.10 ｜ 被点名 无 ｜ 兑现义务 无",
        impressions_text="对 丙 的印象：她话里有话")
    system, user = msgs[0]["content"], msgs[1]["content"]
    assert "【你的近期状态/想法】" in user
    assert "唤醒 0.60" in user and "兑现义务 无" in user
    assert "对 丙 的印象：她话里有话" in user
    # 小节位于开口指令之前，不顶掉"你现在开口"收尾
    assert user.index("【你的近期状态/想法】") < user.index("你现在开口：")
    # speak 系统仍带身份锁 + 反复读硬规则
    assert "绝不模仿" in system and "第三人称" in system
    assert "宁可沉默" in system


def test_speak_messages_without_own_state_are_backward_compatible():
    """缺省 state/impressions：与旧签名逐字节一致，绝不注入空小节。"""
    card = CharacterCard(name="乙", personality={"描述": "冷静"})
    base = build_speak_messages(card, view_text="v", scene_text="s")
    same = build_speak_messages(card, view_text="v", scene_text="s",
                                state_text="", impressions_text="")
    assert same == base
    blob = base[0]["content"] + base[1]["content"]
    assert "【你的近期状态/想法】" not in blob
    assert "【你的近期状态】" not in blob and "【你对ta的印象】" not in blob


def _many_line_view(n: int = 30) -> str:
    """n 条历史转录：奇数乙、偶数丙（speaker 不影响 id 解析）。"""
    return "\n".join(
        f"[{i}] {'乙' if i % 2 else '丙'}: 第{i}句台词。"
        for i in range(n))


def test_speak_prev_line_section_when_present():
    """fix：speak 给「此刻·上一句」因果锚点——归属行 + 自续≠回应他人的判断/行动。

    prev_line_text 非空时，用户消息须含【此刻·上一句】+ 归属行原文 + 双分支指导，
    小节落在本人私有小节之后、开口指令之前。"""
    card = CharacterCard(name="丙", personality={"描述": "锐利"})
    view = "[0] 导演: （开场）\n[1] 乙: 今晚这桌我请。\n[2] 乙 →丙: 你喝什么？"
    prev_line = "[2] 乙 →丙: 你喝什么？"
    msgs = build_speak_messages(card, view_text=view, scene_text="餐厅",
                                prev_line_text=prev_line)
    user = msgs[1]["content"]
    assert "【此刻·上一句】" in user
    assert prev_line in user                       # 归属行原文进提示词
    assert "再复读" in user                        # 告诫别把它当新内容再念
    assert "若它的说话人正是你自己" in user         # 自续分支
    assert "自然续说" in user and "回应对手的句式" in user
    assert "复读自己刚说的" in user and "假装别人刚接了话" in user
    assert "若是他人说的" in user                   # 他人分支：判断是否点名我
    assert "→点名" in user and "宁可沉默" in user
    assert user.index("【此刻·上一句】") < user.index("你现在开口：")
    # 旧系统侧 own-previous 规则不因纯他人行而注入（它属于行内归属判断）
    assert "上一句是你自己说的" not in msgs[0]["content"]


def test_speak_prev_line_own_previous_uses_self_continuation_branch():
    """own_previous=True + 归属行：自续话术在用户小节里生效，系统不再重复塞旧规则。

    上一句正是自己（无他人插话的自续机会）：小节「若它的说话人正是你自己」分支给出
    自续指令；旧 _SPEAK_OWN_PREVIOUS_RULE 语义已折叠进小节，避免两处同义文本并存。"""
    card = CharacterCard(name="乙", personality={"描述": "冷静"})
    prev_line = "[7] 乙: 再加一碟花生。"
    view = "[6] 乙: 那就热的。\n" + prev_line
    msgs = build_speak_messages(card, view_text=view, scene_text="餐厅",
                                own_previous=True, prev_line_text=prev_line)
    system, user = msgs[0]["content"], msgs[1]["content"]
    # 系统侧不再追加旧的"上一句是你自己说的"（已折叠进用户小节）
    assert "上一句是你自己说的" not in system
    assert "【此刻·上一句】" in user and prev_line in user
    assert "若它的说话人正是你自己" in user
    assert "自然续说" in user
    assert "回应对手的句式" in user and "复读自己刚说的" in user
    assert "假装别人刚接了话" in user


def test_speak_prev_line_empty_is_byte_identical_to_before():
    """缺省/空 prev_line_text：不注入任何新小节，与旧签名输出逐字节一致。

    prev_line_text='' 与不传参逐字节相等；用户消息仍严格 = 场景 + 视图 + 开口指令，
    无【此刻·上一句】；own_previous=True（无归属行可渲染的旧式调用）仍走系统旧规则。"""
    card = CharacterCard(name="乙", personality={"描述": "冷静"})
    base = build_speak_messages(card, view_text="v", scene_text="s")
    empty = build_speak_messages(card, view_text="v", scene_text="s",
                                 prev_line_text="")
    assert empty == base
    user = base[1]["content"]
    # 用户消息 = 场景头 + 视图 + 「你现在开口：」收尾，绝无额外段落
    assert user == "【场景】s\n【你看到的最近对话】v\n\n你现在开口："
    assert "【此刻·上一句】" not in (base[0]["content"] + user)
    # own_previous=True 但无归属行 → 维持旧系统规则，逐字节不变（兼容旧式纯函数调用）
    legacy = build_speak_messages(card, view_text="v", scene_text="s",
                                  own_previous=True)
    assert "上一句是你自己说的" in legacy[0]["content"]
    assert "自然的延续" in legacy[0]["content"]


def test_full_history_think_and_speak_prompts_keep_every_line():
    """全量历史保证：H 条可见消息 → think/speak 提示词都逐条含全部 [id] 行（无窗口截断）。"""
    view = _many_line_view()
    last = view.splitlines()[-1]
    for builder, kwargs in (
        (build_think_messages, {"last_chunk_text": last, "scene_text": "s"}),
        (build_speak_messages, {"scene_text": "s"}),
    ):
        msgs = builder(_card(), view_text=view, **kwargs)
        user = msgs[1]["content"]
        present = {int(m) for m in re.findall(r"\[(\d+)\]", user)}
        assert present >= {i for i in range(30)}, "提示词丢失了部分历史行"


# ------------------------------------------------------- 去 AI 味 / 综观全局 -----

def test_speak_system_contains_de_ai_flavor_rules_with_dash_exception():
    """A：speak 系统提示必须带「说人话（去 AI 味）」小节，含破折号例外原文（抢断/指认、
    强说明两例）、排比/连接词/金句/句长/具体/神态模板/口语碎句/不用格式化符号。"""
    card = CharacterCard(name="乙", personality={"描述": "冷静"})
    system = build_speak_messages(card, view_text="v", scene_text="s")[0]["content"]
    assert "【说人话（去 AI 味）】" in system
    # 破折号例外措辞必须原样在（测试锁的是这段注入文案本身）
    assert ('破折号"——"几乎不要用。只有两种例外：话被抢断或指认（"你看——""那边——"）、'
            '强说明（"比如说——"）。其余一律改用逗号、句号或直接断句。') in system
    assert "不用" in system and "不仅…而且…" in system and "不是…而是…" in system
    assert "此外/然而/值得注意的是/总而言之/某种意义上" in system
    assert "不要堆四字词" in system
    assert "别写金句、别做总结陈词、别给对话升华意义。说完事就停。" in system
    assert "句子长短要错开" in system
    assert "说具体的东西（杯子、灯、菜、钱、几点），少抽象抒情。" in system
    assert "目光带着几分玩味" in system and "指尖轻点桌面" in system
    assert "同一个神态不要重复用" in system
    assert "允许口语的碎、停顿、跑题、半截话" in system
    assert "不用粗体、编号、列表、emoji。" in system
    # 旧有的身份锁/反复读/语料占位规则不因新增小节失效
    assert "绝不模仿" in system and "第三人称" in system
    assert "宁可沉默" in system and "硬接" in system


def test_speak_de_ai_flavor_rules_survive_corpus_and_own_previous_paths():
    """去 AI 味小节在带语料、own_previous、prev_line 各路径下都仍在（不只是缺省路径）。"""
    card = _corpus_card()
    cases = [
        build_speak_messages(card, view_text="v", scene_text="s"),
        build_speak_messages(card, view_text="v", scene_text="s", own_previous=True),
        build_speak_messages(card, view_text="v", scene_text="s",
                             prev_line_text="[1] 丙: 你好。"),
        build_speak_messages(card, view_text="v", scene_text="s",
                             state_text="唤醒 0.5", impressions_text="印象"),
    ]
    for msgs in cases:
        system = msgs[0]["content"]
        assert "【说人话（去 AI 味）】" in system
        assert '破折号"——"几乎不要用' in system


def test_think_system_contains_deliberation_block_with_negative_urge_instruction():
    """B：think 系统提示改为【怎么想】——先综观全局再落到最新一句，并明确「别人替你说
    了/没有新内容 → urge 应为负值或很低」，且沉默是正常且经常正确的选择。"""
    msgs = build_think_messages(_card(), view_text="[1] 丙: 你好",
                                last_chunk_text="[1] 丙: 你好", scene_text="s")
    system = msgs[0]["content"]
    assert "【怎么想】" in system
    assert "先综观整场对话的局势：话题走到哪了、谁在主导、有没有人问过你话你还没回、有没有事情悬着没解决。" in system
    assert "再落到最新这一句上：它是不是说给你听的？需不需要你接？" in system
    assert "你的冲动 urge 应该是**负值或很低**" in system
    assert '明确表达"这句我不必说"' in system
    assert "若有人点了你的名、问了你、或你有非说不可的事 —— 冲动才高。" in system
    assert '别把"插一句"当默认动作；沉默是完全正常、且经常正确的选择。' in system
    # 旧的"你刚听到一句新的话…"框架已不在
    assert "你刚听到一句新的话" not in system


def test_think_system_keeps_short_no_cliche_rule():
    """A（think 短版）：写印象/理由也不许套话，模板词点名禁止；用朴素的词。"""
    msgs = build_think_messages(_card(), view_text="v", last_chunk_text="c",
                                scene_text="s")
    system = msgs[0]["content"]
    assert '写印象、写理由时也别说套话（"目光带着几分玩味"这类模板禁止）；用朴素的词，像人心里默念。' in system


def test_think_user_headers_rename_to_whole_conversation_and_latest_line():
    """B：think 用户消息小节改名为「整场对话（自开场至今，逐条带说话人）」与
    「此刻最新的一句」，且旧标题不再出现（speak 侧标题不动）。"""
    msgs = build_think_messages(_card(), view_text="[1] 丙: 你好",
                                last_chunk_text="[1] 丙: 你好", scene_text="餐厅")
    user = msgs[1]["content"]
    assert "【整场对话（自开场至今，逐条带说话人）】" in user
    assert "【此刻最新的一句】" in user
    assert "【你看到的最近对话】" not in user
    assert "【刚听到的这一句】" not in user
    # 归属提醒仍在（最新一句可能是别人、也可能是你自己）
    assert "行首标注了说话人——可能是别人，也可能正是你自己" in user
    assert "[1] 丙: 你好" in user
    # speak 侧标题不改（仅 think 改名）
    speak = build_speak_messages(_card(), view_text="v", scene_text="s")[1]["content"]
    assert "【你看到的最近对话】" in speak


def test_think_prompts_still_lock_json_contract_identity_and_corpus():
    """B 重写后：锁死 JSON/字段/json 关键词、身份锁、反复读规则、语料与私有小节全在。"""
    card = _corpus_card()
    msgs = build_think_messages(card, view_text="v", last_chunk_text="c",
                                scene_text="s", state_text="唤醒 0.50",
                                impressions_text="对 丙 的印象：话里有话",
                                own_last=True)
    blob = msgs[0]["content"] + msgs[1]["content"]
    assert "json" in blob.lower()
    for fld in ('"aroused"', '"obligation_fulfilled"', '"goal_progress"',
                '"addressed"', '"impression_of_speaker"', '"urge"'):
        assert fld in blob, f"schema 字段丢失：{fld}"
    assert "只输出这一份 JSON" in blob and "请输出你的 think JSON" in msgs[1]["content"]
    assert "绝不模仿" in blob and "第三人称" in blob
    assert "不要重复自己或他人已经说过的话" in blob
    assert "刚听到的是你自己说的话" in blob          # own_last 提醒仍注入
    assert "【人物语料·出处】《示例作品》（占位）" in blob
    assert "【你的近期状态】" in blob and "【你对ta的印象】" in blob


def test_think_urge_field_description_is_honest_about_weighted_bid():
    """C：urge 字段描述须诚实——它现在作为一项加权计入发言权（不再是"唯一决定权"）。"""
    msgs = build_think_messages(_card(), view_text="v", last_chunk_text="c",
                                scene_text="s")
    system = msgs[0]["content"]
    assert '"urge": 你想开口的冲动自评（-1~2，可为负=不想说；会作为一项加权计入发言权）' in system
    assert "决定权只由调度器看这一个数" not in system


# ---------------------------------------------------------------- 人物语料 -----

def _corpus_card() -> CharacterCard:
    return CharacterCard(
        name="乙", personality={"描述": "冷静、观察多"},
        corpus=Corpus(
            source="《示例作品》（占位）",
            style="短句为主，语速慢；先抛观察，再补一句轻描淡写的调侃。",
            thinking="先看对方反应再决定是否开口；重要的话只说一半，情绪藏进玩笑里。",
            quirks=["……", "倒是", "也许吧"],
            samples=["（占位样例）今晚这桌，我请。",
                     "（占位样例）你倒是先说说看。",
                     "（占位样例）（笑）随你。"],
        ))


def test_corpus_section_renders_every_part():
    section = _corpus_section(_corpus_card().corpus)
    assert "【人物语料·出处】《示例作品》（占位）" in section
    assert "【语言风格】短句为主" in section
    assert "【思维方式】先看对方反应" in section
    assert "【口头禅】……、倒是、也许吧" in section
    assert "【代表性台词样例（模仿其口吻与思维，但绝不照抄样例内容或原句）】" in section
    assert "- （占位样例）今晚这桌，我请。" in section


def test_corpus_section_empty_when_corpus_empty():
    assert _corpus_section(Corpus()) == ""
    # 只有空白字符也算空
    assert _corpus_section(Corpus(source="  ", style="\n", quirks=["", " "],
                                  samples=["", "   "])) == ""


def test_corpus_section_omits_empty_subparts():
    """缺哪一节就整行省略（不给空标题）；只剩样例时只有样例小节。"""
    section = _corpus_section(Corpus(samples=["只有样例"]))
    assert section.startswith("【代表性台词样例")
    assert "【人物语料·出处】" not in section
    assert "【语言风格】" not in section
    assert "【思维方式】" not in section
    assert "【口头禅】" not in section
    assert "- 只有样例" in section


def test_corpus_section_caps_to_eight_samples_and_trims_to_120():
    """样例最多 8 条、每条裁到 120 字，防止语料把系统提示撑爆。"""
    corpus = Corpus(samples=[f"第{i}句" + "长" * 300 for i in range(12)])
    section = _corpus_section(corpus)
    lines = [ln for ln in section.splitlines() if ln.startswith("- ")]
    assert len(lines) == 8
    assert all(len(ln) - 2 <= 120 for ln in lines)
    assert all(ln[2:].startswith(f"第{i}句") for i, ln in enumerate(lines))


# ------------------------------------------------- 场景（背景/描述/走向/语言指令） -----
#
# 提示词装配顺序（确定性，见各 builder docstring）：
#   think/speak 系统 = 角色卡 → [语料] → 场景背景 → 场景描述 → 身份/去AI味规则
#                     → [你的信息索引] → 语言指令
#   think 用户 = 场景+整场对话+此刻最新一句 → (own_last) → 私有状态 → 剧情走向 → JSON 收尾
#   speak 用户 = 场景+最近对话 → 私有状态 → [你想起的事] → 此刻·上一句 → 剧情走向 → 开口收尾
#   narrate 系统 = 世界规则 → 场景背景 → 场景描述 → 剧情走向 → 语言指令（剧情走向在系统侧）
# 四个新参均可选、缺省 ''；四个都空时输出与加场景特性前逐字节相同。
# 信息库两参（§6.1/§6.4）同样可选、缺省 ''：索引进系统末尾（前缀缓存稳定），取用正文进
# 用户消息（私有内容只喂这名听众）；见下方「信息库注入」一节。

_BG = "世界观：秋末的临江镇，这场雨停了就要散场。"
_DESC = "餐厅靠窗第二桌；桌上一壶冷茶，窗外雨声很大。"
_PLOT = "乙要把那张旧照片拿出来，但先别解释它是谁的。"
_LANG = "你将使用简体中文回答。"
_SCENE_KW = {"language_directive": _LANG, "background_text": _BG,
             "description_text": _DESC, "plot_text": _PLOT}


def test_think_scene_background_and_description_go_to_system_only():
    """场景背景/描述：非空 → 系统消息出【场景背景】/【场景描述】小节（带原文）；
    绝不漏进用户消息（用户侧只有剧情走向）。"""
    msgs = build_think_messages(_card(), view_text="[1] 丙: 你好",
                                last_chunk_text="你好", scene_text="餐厅",
                                background_text=_BG, description_text=_DESC)
    system, user = msgs[0]["content"], msgs[1]["content"]
    assert f"【场景背景】{_BG}" in system
    assert f"【场景描述】{_DESC}" in system
    assert "【场景背景】" not in user and "【场景描述】" not in user


def test_speak_scene_background_and_description_go_to_system_only():
    msgs = build_speak_messages(_card(), view_text="[1] 丙: 你好", scene_text="餐厅",
                                background_text=_BG, description_text=_DESC)
    system, user = msgs[0]["content"], msgs[1]["content"]
    assert f"【场景背景】{_BG}" in system
    assert f"【场景描述】{_DESC}" in system
    assert "【场景背景】" not in user and "【场景描述】" not in user


def test_scene_sections_absent_or_blank_leave_no_trace():
    """缺省 / 显式空串 / 纯空白：都不渲染小节（不留空标题），三处 builder 一致。"""
    for kw in ({}, {"background_text": "", "description_text": ""},
               {"background_text": "  \n ", "description_text": "\t"}):
        think = build_think_messages(_card(), view_text="v", last_chunk_text="c",
                                     scene_text="s", **kw)
        speak = build_speak_messages(_card(), view_text="v", scene_text="s", **kw)
        narr = build_narrate_messages("v", "s", "c", "r", **kw)
        for msgs in (think, speak, narr):
            blob = msgs[0]["content"] + msgs[1]["content"]
            assert "【场景背景】" not in blob
            assert "【场景描述】" not in blob


def test_one_scene_section_alone_omits_the_other():
    """只有背景 / 只有描述：各自单独渲染，不给另一节留空标题。"""
    only_bg = build_think_messages(_card(), view_text="v", last_chunk_text="c",
                                   scene_text="s", background_text=_BG)
    assert f"【场景背景】{_BG}" in only_bg[0]["content"]
    assert "【场景描述】" not in only_bg[0]["content"]
    only_desc = build_think_messages(_card(), view_text="v", last_chunk_text="c",
                                     scene_text="s", description_text=_DESC)
    assert f"【场景描述】{_DESC}" in only_desc[0]["content"]
    assert "【场景背景】" not in only_desc[0]["content"]


def test_plot_text_goes_to_user_message_before_outro():
    """剧情走向（作者意图）：think/speak 落在**用户**消息收尾指令之前，不进系统消息。"""
    think = build_think_messages(_card(), view_text="v", last_chunk_text="c",
                                 scene_text="s", plot_text=_PLOT)
    think_user = think[1]["content"]
    assert f"【剧情走向（作者意图，参考它行动，不要直接复述）】{_PLOT}" in think_user
    assert think_user.index("【剧情走向") < think_user.index("请输出你的 think JSON")
    assert "【剧情走向" not in think[0]["content"]

    speak = build_speak_messages(_card(), view_text="v", scene_text="s",
                                 plot_text=_PLOT)
    speak_user = speak[1]["content"]
    assert f"【剧情走向（作者意图，参考它行动，不要直接复述）】{_PLOT}" in speak_user
    assert speak_user.index("【剧情走向") < speak_user.index("你现在开口：")
    assert "【剧情走向" not in speak[0]["content"]


def test_plot_text_blank_leaves_no_trace():
    for kw in ({}, {"plot_text": ""}, {"plot_text": "   \n"}):
        think = build_think_messages(_card(), view_text="v", last_chunk_text="c",
                                     scene_text="s", **kw)
        speak = build_speak_messages(_card(), view_text="v", scene_text="s", **kw)
        narr = build_narrate_messages("v", "s", "c", "r", **kw)
        for msgs in (think, speak, narr):
            assert "【剧情走向" not in (msgs[0]["content"] + msgs[1]["content"])


def test_language_directive_is_last_and_exactly_once():
    """语言指令：非空时作系统消息最后一段、且只出现一次——即便其他场景小节都在。"""
    for msgs in (
        build_think_messages(_card(), view_text="v", last_chunk_text="c",
                             scene_text="s", **_SCENE_KW),
        build_speak_messages(_card(), view_text="v", scene_text="s", **_SCENE_KW),
        build_speak_messages(_card(), view_text="v", scene_text="s",
                             own_previous=True, **_SCENE_KW),
        build_narrate_messages("v", "s", "c", "r", **_SCENE_KW),
    ):
        system = msgs[0]["content"]
        assert system.endswith("\n\n" + _LANG), "语言指令必须是系统消息最后一段"
        assert system.count(_LANG) == 1, "语言指令被重复注入"
    # 只给语言指令（其余为空）亦然
    bare = build_think_messages(_card(), view_text="v", last_chunk_text="c",
                                scene_text="s", language_directive=_LANG)
    assert bare[0]["content"].endswith("\n\n" + _LANG)
    assert bare[0]["content"].count(_LANG) == 1


def test_no_hardcoded_chinese_phrase_fights_the_language_directive():
    """P1：语言不再硬编码——speak/narrate 的系统提示里不得留「中文输出。」「- 中文。」。

    语言指令是唯一来源：非中文指令下系统消息里不能同时出现「中文」这类死文案
    （此前 speak 系统同时含「中文输出。」与「你将使用English回答。」，自相矛盾）。
    think 侧本就无中文文案，一并锁死。
    """
    en = "你将使用English回答。"
    speak = build_speak_messages(_card(), view_text="v", scene_text="s",
                                 language_directive=en)
    narr = build_narrate_messages("v", "s", "c", "r", language_directive=en)
    think = build_think_messages(_card(), view_text="v", last_chunk_text="c",
                                 scene_text="s", language_directive=en)
    for msgs in (speak, narr, think):
        assert "中文" not in msgs[0]["content"], "系统提示里残留硬编码中文要求"
        assert en in msgs[0]["content"]            # 唯一语言来源仍在
    # 缺省（无语言指令）时也不许自己冒出来
    assert "中文输出" not in build_speak_messages(
        _card(), view_text="v", scene_text="s")[0]["content"]
    assert "中文。" not in build_narrate_messages("v", "s", "c", "r")[0]["content"]


def test_all_four_scene_params_ordering_think():
    """四个参数同时给：小节按文档顺序落在串里（系统 + 用户各自断言索引序）。"""
    msgs = build_think_messages(_card(), view_text="v", last_chunk_text="c",
                                scene_text="s", state_text="唤醒 0.50", **_SCENE_KW)
    system, user = msgs[0]["content"], msgs[1]["content"]
    assert (system.index("【角色设定】") < system.index("【场景背景】")
            < system.index("【场景描述】") < system.index("【怎么想】")
            < system.index(_LANG)), "think 系统顺序：角色卡→背景→描述→规则→语言指令"
    assert (user.index("【整场对话") < user.index("【此刻最新的一句】")
            < user.index("【你的近期状态】") < user.index("【剧情走向")
            < user.index("请输出你的 think JSON")), "think 用户顺序"


def test_all_four_scene_params_ordering_speak():
    msgs = build_speak_messages(_card(), view_text="v", scene_text="s",
                                state_text="唤醒 0.50",
                                prev_line_text="[1] 丙: 你好。", **_SCENE_KW)
    system, user = msgs[0]["content"], msgs[1]["content"]
    assert (system.index("你是角色 乙") < system.index("【场景背景】")
            < system.index("【场景描述】") < system.index("【说人话（去 AI 味）】")
            < system.index(_LANG)), "speak 系统顺序：角色卡→背景→描述→规则→语言指令"
    assert (user.index("【你看到的最近对话】") < user.index("【你的近期状态/想法】")
            < user.index("【此刻·上一句】") < user.index("【剧情走向")
            < user.index("你现在开口：")), "speak 用户顺序"


def test_scene_sections_coexist_with_corpus_identity_and_json_lock():
    """带语料 + 四个场景参：语料/身份锁/锁死 JSON 字段仍完整，场景只做加法。"""
    msgs = build_think_messages(_corpus_card(), view_text="v", last_chunk_text="c",
                                scene_text="s", own_last=True, **_SCENE_KW)
    blob = msgs[0]["content"] + msgs[1]["content"]
    assert "json" in blob.lower()
    for fld in ('"aroused"', '"obligation_fulfilled"', '"goal_progress"',
                '"addressed"', '"impression_of_speaker"', '"urge"'):
        assert fld in blob, f"schema 字段丢失：{fld}"
    assert "只输出这一份 JSON" in blob and "请输出你的 think JSON" in msgs[1]["content"]
    assert "【人物语料·出处】《示例作品》（占位）" in blob
    assert "绝不模仿" in blob and "第三人称" in blob
    assert "刚听到的是你自己说的话" in blob
    assert f"【场景背景】{_BG}" in blob and f"【剧情走向（作者意图，参考它行动，不要直接复述）】{_PLOT}" in blob
    # speak 侧同样（语料在去 AI 味末行，场景小节仍进系统、不去用户消息）
    speak = build_speak_messages(_corpus_card(), view_text="v", scene_text="s",
                                 **_SCENE_KW)
    assert "【人物语料·出处】《示例作品》（占位）" in speak[0]["content"]
    assert f"【场景背景】{_BG}" in speak[0]["content"]
    assert "【场景背景】" not in speak[1]["content"]
    assert "宁可沉默" in speak[0]["content"]


def test_all_four_empty_is_byte_identical_on_representative_paths():
    """四个新参全空（缺省或显式 ''）→ 与加场景特性前逐字节相同。

    覆盖代表性路径：think 缺省 / think 带私有态+own_last / speak 缺省 / speak 带
    prev_line / speak own_previous（旧系统规则路径）——系统与用户消息都逐字节相等。
    """
    empty = {"language_directive": "", "background_text": "",
             "description_text": "", "plot_text": ""}
    cases = [
        (build_think_messages, dict(view_text="v", last_chunk_text="c", scene_text="s")),
        (build_think_messages, dict(view_text="v", last_chunk_text="c", scene_text="s",
                                    state_text="唤醒 0.50", impressions_text="印象",
                                    own_last=True)),
        (build_speak_messages, dict(view_text="v", scene_text="s")),
        (build_speak_messages, dict(view_text="v", scene_text="s",
                                    prev_line_text="[1] 丙: 你好。")),
        (build_speak_messages, dict(view_text="v", scene_text="s", own_previous=True)),
    ]
    for builder, kwargs in cases:
        assert builder(_card(), **kwargs, **empty) == builder(_card(), **kwargs), \
            f"{builder.__name__} 缺省路径因场景参漂移：{kwargs}"
    # 空语料卡的系统消息仍等于特性前的逐字基线
    assert (build_think_messages(_card(), view_text="v", last_chunk_text="c",
                                 scene_text="s", **empty)[0]["content"]
            == _BASELINE_THINK_SYSTEM)
    assert (build_speak_messages(_card(), view_text="v", scene_text="s",
                                 **empty)[0]["content"] == _BASELINE_SPEAK_SYSTEM)
    # own_previous 旧路径的旧规则仍逐字节追加（语言指令为空时不动）
    legacy = build_speak_messages(_card(), view_text="v", scene_text="s",
                                  own_previous=True, **empty)
    assert legacy[0]["content"].endswith("不要假装别人刚回答了。")


def test_narrate_scene_sections_plot_and_mutable_description_line():
    """narrate：背景/描述进系统；剧情走向也进**系统**（作者意图是世界该怎么动的约束）；
    系统模板恒带「可变场景描述→体现新状态」提示；用户消息一字不动。"""
    base = build_narrate_messages("v", "s", "c", "r")
    msgs = build_narrate_messages("v", "s", "c", "r", **_SCENE_KW)
    system, user = msgs[0]["content"], msgs[1]["content"]
    assert f"【场景背景】{_BG}" in system
    assert f"【场景描述】{_DESC}" in system
    assert f"【剧情走向（作者意图，参考它行动，不要直接复述）】{_PLOT}" in system
    assert ("【场景背景】" in system and "【场景描述】" in system
            and system.index("【场景背景】") < system.index("【场景描述】")
            < system.index("【剧情走向") < system.index(_LANG))
    assert system.count(_LANG) == 1 and system.endswith("\n\n" + _LANG)
    # 用户消息不受场景四参影响（逐字节）
    assert user == base[1]["content"]
    assert "【剧情走向" not in user and "【场景背景】" not in user
    # 恒定的可变描述提示 + 全知的既有硬规则
    assert "可变" in system and "新状态" in system
    assert "你是这场戏的「场景」——不是角色，而是世界本身（全知）" in system
    assert "只写发生的事" in system and "不要替后面的剧情做设计" in system


def test_narrate_all_four_empty_keeps_user_identical_and_system_is_template_plus_line():
    """narrate 四个新参全空：用户消息逐字节不变；系统 = 原模板 + 恒定的可变描述行。"""
    base = build_narrate_messages("v", "s", "c", "r")
    empty = build_narrate_messages("v", "s", "c", "r",
                                   language_directive="", background_text="",
                                   description_text="", plot_text="")
    assert empty == base
    assert empty[1]["content"] == ("【整场对话】\nv\n【场景】s\n【当前时刻】c\n"
                                   "【现在需要你推进的原因】r\n\n请写一到两行：")
    # 全空 → 系统 = 模板原文；模板本身已含恒定的「可变描述」行（本特性唯一非条件文案）
    system = empty[0]["content"]
    assert system == _NARRATE_SYSTEM
    assert "可变" in _NARRATE_SYSTEM and "新状态" in _NARRATE_SYSTEM
    assert "你是这场戏的「场景」——不是角色，而是世界本身（全知）" in system
    for marker in ("【场景背景】", "【场景描述】", "【剧情走向"):
        assert marker not in system


def test_think_and_speak_system_prompts_include_corpus():
    """语料必须同时进 think 与 speak 的系统提示：风格/思维/口头禅/样例 + 不照抄告诫。"""
    card = _corpus_card()
    think = build_think_messages(card, view_text="v", last_chunk_text="c",
                                 scene_text="s")
    speak = build_speak_messages(card, view_text="v", scene_text="s")
    for sys_text in (think[0]["content"], speak[0]["content"]):
        assert "【人物语料·出处】《示例作品》（占位）" in sys_text
        assert "【语言风格】短句为主，语速慢；先抛观察，再补一句轻描淡写的调侃。" in sys_text
        assert "【思维方式】先看对方反应再决定是否开口；重要的话只说一半，情绪藏进玩笑里。" in sys_text
        assert "【口头禅】……、倒是、也许吧" in sys_text
        assert "绝不照抄样例内容或原句" in sys_text
        assert "- （占位样例）今晚这桌，我请。" in sys_text
        assert "- （占位样例）（笑）随你。" in sys_text
    # 既有硬规则/锁死 JSON 不被语料挤掉
    assert '"urge"' in think[0]["content"] and "绝不模仿" in think[0]["content"]
    assert "绝不模仿" in speak[0]["content"] and "宁可沉默" in speak[0]["content"]
    # 只进系统消息，用户消息不被污染
    assert "【语言风格】" not in think[1]["content"]
    assert "【语言风格】" not in speak[1]["content"]


def test_empty_corpus_renders_byte_identical_system_prompts():
    """空语料（缺省或显式空 Corpus）时系统提示与改动前逐字节一致：占位不得留下多余空行。"""
    card = _card()
    explicit = CharacterCard(name="乙", personality={"描述": "冷静、观察多"},
                             corpus=Corpus())
    for c in (card, explicit):
        think = build_think_messages(c, view_text="v", last_chunk_text="c",
                                     scene_text="s")
        speak = build_speak_messages(c, view_text="v", scene_text="s")
        assert think[0]["content"] == _BASELINE_THINK_SYSTEM
        assert speak[0]["content"] == _BASELINE_SPEAK_SYSTEM
        assert "【人物语料·出处】" not in think[0]["content"]
        assert "【代表性台词样例" not in speak[0]["content"]
    # 模板里确有 {corpus} 占位（缺失则上面的逐字节断言会静默失守）
    from harness import prompters as _p
    assert "{corpus}" in _p._THINK_SYSTEM_TMPL
    assert "{corpus}" in _p._SPEAK_SYSTEM_TMPL


#: 空语料基线（加语料特性前逐字捕获，§9.1 撤去【你的已知边界】那一节后的现状）：
#: 缺省路径必须与这两串逐字节相同。改动前那一份留在下面（含那一节），供"只删了它"的对比。
_BASELINE_THINK_SYSTEM = '你是角色 乙，正在参与一场会话。只依据你确实看见/经历过的信息行事，绝不假装知道没被告知的事。\n\n【角色设定】{"描述": "冷静、观察多"}\n\n【怎么想】\n- 先综观整场对话的局势：话题走到哪了、谁在主导、有没有人问过你话你还没回、有没有事情悬着没解决。\n- 再落到最新这一句上：它是不是说给你听的？需不需要你接？\n- 然后诚实判断此刻该不该开口：\n  · 若是别人刚替你把想说的话说了，或你此刻没有新的、实质的内容 —— 你的冲动 urge 应该是**负值或很低**（明确表达"这句我不必说"）。\n  · 若有人点了你的名、问了你、或你有非说不可的事 —— 冲动才高。\n  · 别把"插一句"当默认动作；沉默是完全正常、且经常正确的选择。\n\n【本次任务的硬性规则】\n- 用一次简短思考完成三件事：按上节读懂局势与最新这一句、更新你的私有状态、自标注。\n- 这句刚听到的话在用户消息里带说话人标注（行首 [编号] 说话人: 内容）。先想清你在回应谁的哪句话：\n  若行首说话人正是你自己，说明你在自续（延续自己的话），须注明是"自续"，不得把它当成别人说的匿名话来接——严禁自问自答。\n- 永远以你自己（第一人称，你的角色名 乙）的口吻思考；绝不模仿、套用或延续**其他角色**的句式/口头禅，尤其不得借用刚在你前面开口那位的话头。绝不用第三人称称呼自己，绝不把自己当作对方来问话/回答。\n- 不要重复自己或他人已经说过的话（含意思相近的表述）；若此刻没有新的、实质的话可说，就不要提高开口冲动(urge)。\n- 写印象、写理由时也别说套话（"目光带着几分玩味"这类模板禁止）；用朴素的词，像人心里默念。\n- 思考结果只输出一段 JSON，不要任何解释性文字（禁止小作文）。\n- JSON 严格包含以下键（字段含义与取值范围）：\n  "aroused": 0~1 的当前情绪唤醒度\n  "obligation_fulfilled": list[str]，这句话兑现了哪些欠你的人情/挑衅（没有则空数组）\n  "goal_progress": 0~1，你的私有目标因此推进了多少\n  "addressed": 被这句话点名的人名或 null\n  "impression_of_speaker": 你对说话人的一句新印象（可为 null）\n  "urge": 你想开口的冲动自评（-1~2，可为负=不想说；会作为一项加权计入发言权）\n- 只输出这一份 JSON。请输出英文单词 json 引导的正确 JSON 文本。'

#: 改动前逐字捕获的同一份基线（**含**【你的已知边界】那一节）。只服务于下面那条
#: 用例：证明 §9.1 撤除那一节时**只动了这一节**，没有顺手重排空白/换行。
_BASELINE_THINK_SYSTEM_WITH_KB = '你是角色 乙，正在参与一场会话。只依据你确实看见/经历过的信息行事，绝不假装知道没被告知的事。\n\n【角色设定】{"描述": "冷静、观察多"}\n\n【你的已知边界】只知道自己经历和被告知的事\n\n【怎么想】\n- 先综观整场对话的局势：话题走到哪了、谁在主导、有没有人问过你话你还没回、有没有事情悬着没解决。\n- 再落到最新这一句上：它是不是说给你听的？需不需要你接？\n- 然后诚实判断此刻该不该开口：\n  · 若是别人刚替你把想说的话说了，或你此刻没有新的、实质的内容 —— 你的冲动 urge 应该是**负值或很低**（明确表达"这句我不必说"）。\n  · 若有人点了你的名、问了你、或你有非说不可的事 —— 冲动才高。\n  · 别把"插一句"当默认动作；沉默是完全正常、且经常正确的选择。\n\n【本次任务的硬性规则】\n- 用一次简短思考完成三件事：按上节读懂局势与最新这一句、更新你的私有状态、自标注。\n- 这句刚听到的话在用户消息里带说话人标注（行首 [编号] 说话人: 内容）。先想清你在回应谁的哪句话：\n  若行首说话人正是你自己，说明你在自续（延续自己的话），须注明是"自续"，不得把它当成别人说的匿名话来接——严禁自问自答。\n- 永远以你自己（第一人称，你的角色名 乙）的口吻思考；绝不模仿、套用或延续**其他角色**的句式/口头禅，尤其不得借用刚在你前面开口那位的话头。绝不用第三人称称呼自己，绝不把自己当作对方来问话/回答。\n- 不要重复自己或他人已经说过的话（含意思相近的表述）；若此刻没有新的、实质的话可说，就不要提高开口冲动(urge)。\n- 写印象、写理由时也别说套话（"目光带着几分玩味"这类模板禁止）；用朴素的词，像人心里默念。\n- 思考结果只输出一段 JSON，不要任何解释性文字（禁止小作文）。\n- JSON 严格包含以下键（字段含义与取值范围）：\n  "aroused": 0~1 的当前情绪唤醒度\n  "obligation_fulfilled": list[str]，这句话兑现了哪些欠你的人情/挑衅（没有则空数组）\n  "goal_progress": 0~1，你的私有目标因此推进了多少\n  "addressed": 被这句话点名的人名或 null\n  "impression_of_speaker": 你对说话人的一句新印象（可为 null）\n  "urge": 你想开口的冲动自评（-1~2，可为负=不想说；会作为一项加权计入发言权）\n- 只输出这一份 JSON。请输出英文单词 json 引导的正确 JSON 文本。'

_BASELINE_SPEAK_SYSTEM = '你是角色 乙。{"描述": "冷静、观察多"} 用符合角色的口吻说话。只说你知道的事；不知道就不提。\n说出的内容就是一个「言语单位」：一句话或一个简短动作/表情，不要长篇独白。\n永远以你自己的身份开口：保持你的第一人称与角色名 乙 的自我认同，绝不模仿、套用或延续**其他角色**的句式/口头禅——尤其不得借用或续写刚在你前面开口那位的话头。绝不用第三人称称呼自己，绝不把自己当作对方来问话或回答。\n不得复读或几乎复读自己或任何角色已说过的话（含意思、动作与措辞相近）；哪怕隔了很多轮，也绝不把场景里已有人说过的话当作新内容再念一遍；如果此刻没有新内容可说，宁可沉默。\n别硬接一句显然是说给别人听的话——除非真需要你回应，否则不必接。\n\n【说人话（去 AI 味）】\n- 破折号"——"几乎不要用。只有两种例外：话被抢断或指认（"你看——""那边——"）、强说明（"比如说——"）。其余一律改用逗号、句号或直接断句。\n- 不用"不仅…而且…""不是…而是…"这类排比；不用"此外/然而/值得注意的是/总而言之/某种意义上"这类连接词；不要堆四字词。\n- 别写金句、别做总结陈词、别给对话升华意义。说完事就停。\n- 句子长短要错开。别每句一样长，也别每句都工整。\n- 说具体的东西（杯子、灯、菜、钱、几点），少抽象抒情。\n- 神态/动作（括号里的）要短、要具体、别用模板：不要通篇"目光带着几分玩味""指尖轻点桌面""唇角微扬""语气平淡"这类套话；同一个神态不要重复用；能不加就不加。\n- 允许口语的碎、停顿、跑题、半截话。真人说话就是这样。\n- 不用粗体、编号、列表、emoji。'


# --------------------------------------------------------- 推进活跃度（§6.2） -----
def test_narrate_activity_hint_goes_to_system_before_language_and_spares_user():
    """活跃度提示进**系统**消息（剧情走向之后、语言指令之前），用户消息一字不动。"""
    base = build_narrate_messages("v", "s", "c", "r", **_SCENE_KW)
    hint = "【推进活跃度】当前活跃度：低（0.20）。尽量少介入，把戏留给角色自己演。"
    msgs = build_narrate_messages("v", "s", "c", "r", activity_hint=hint, **_SCENE_KW)
    system, user = msgs[0]["content"], msgs[1]["content"]
    assert hint in system
    assert system.index("【剧情走向") < system.index("【推进活跃度】") < system.index(_LANG), \
        "活跃度提示排在剧情走向之后、语言指令之前（语言指令恒为最后一段）"
    assert system.endswith("\n\n" + _LANG)
    assert user == base[1]["content"], "用户消息不受活跃度影响（逐字节）"


def test_narrate_activity_hint_blank_leaves_output_byte_identical():
    """缺省 / 空串 / 纯空白 → 与没有这个参数时逐字节相同（缺省路径不漂移）。"""
    base = build_narrate_messages("v", "s", "c", "r")
    for kw in ({}, {"activity_hint": ""}, {"activity_hint": "   \n "}):
        assert build_narrate_messages("v", "s", "c", "r", **kw) == base
    with_hint = build_narrate_messages("v", "s", "c", "r", activity_hint="【推进活跃度】中。")
    assert with_hint[0]["content"] == base[0]["content"] + "\n\n【推进活跃度】中。"
    assert with_hint[1]["content"] == base[1]["content"]


# ------------------------------------------------- 时间条件契约（钩子） -----
def test_narrate_time_condition_rule_is_additive_and_default_is_byte_identical():
    """`hook_time_rule` 是可选的追加段：缺省/空串/纯空白 → 输出逐字节不变（缺省路径不漂移）。

    实况事故的提示词侧：模型在 19:03 报告了条件为「虚拟钟走到 21:30」的钩子。这段契约
    把「当前钟点就在【当前时刻】、没到点就不许报」讲死；引擎另有确定性闸门兜底。
    """
    base = build_narrate_messages("v", "s", "c", "r")
    for kw in ({}, {"hook_time_rule": ""}, {"hook_time_rule": "   \n "}):
        assert build_narrate_messages("v", "s", "c", "r", **kw) == base

    msgs = build_narrate_messages("v", "s", "c", "r", hook_time_rule=TIME_CONDITION_RULE)
    assert msgs[0]["content"] == base[0]["content"] + "\n\n" + TIME_CONDITION_RULE
    assert msgs[1]["content"] == base[1]["content"], "用户消息不受影响（逐字节）"


def test_narrate_time_condition_rule_states_the_contract():
    """契约本身要说清三件事：钟点在哪一节、严格对照、没到点不许报。"""
    assert "【当前时刻】" in TIME_CONDITION_RULE
    assert "严格对照" in TIME_CONDITION_RULE
    assert "不要报告" in TIME_CONDITION_RULE and "还没到" in TIME_CONDITION_RULE
    assert "虚拟钟走到 21:30" in TIME_CONDITION_RULE, "给出看得懂的例子"


def test_narrate_time_condition_rule_sits_before_the_language_directive():
    """时间条件契约排在活跃度之后、语言指令（恒为最后一段）之前。"""
    msgs = build_narrate_messages("v", "s", "c", "r", language_directive=_LANG,
                                  activity_hint="【推进活跃度】中。",
                                  hook_time_rule=TIME_CONDITION_RULE)
    system = msgs[0]["content"]
    assert system.index("【推进活跃度】") < system.index("【时间条件】")
    assert system.index("【时间条件】") < system.index(_LANG)
    assert system.endswith("\n\n" + _LANG)


# ------------------------------------------------- 信息库注入（§6.1 / §6.4） -----
#
# think/speak 系统 = … → 【你的信息索引】（仅 index_text 非空） → 语言指令（恒为最后一段）
# speak 用户      = 场景+最近对话 → 私有状态/想法 → 【你想起的事】（仅 recall_text 非空）
#                   → 此刻·上一句 → 剧情走向 → 开口收尾
# index_text 是**渲染好的**索引小节（其标题/尾注由 `knowledge.render_index` 产出，§6.1
# 逐字节给定）——prompters 只是整段搬运，不重复声明同一文案（同一句话两处定义必然漂移）。

_INDEX = ("【你的信息索引】\n"
          "▾ 药铺\n"
          "   药铺的暗格 — 柜台下第三块砖是空的，里侧有夹层\n"
          "▾ 借自《庆国世界观》\n"
          "   地理·西线 — 靠山，常年封冻\n"
          "\n（你知道的远不止这些。索引只列入口，正文里常有指向其他条目的线索。）")
_RECALL = "· 药铺的暗格：柜台下第三块砖是空的，里侧有夹层。\n· 陈掌柜：跛足，左眉有疤。"


def test_think_index_text_lands_in_system_message_before_language_directive():
    """索引表注入（§6.1）：index_text 整段落进 think **系统**消息末尾、语言指令之前。

    位置是刻意的：前面所有内容（角色卡/语料/场景/硬性规则）保持逐字节稳定，DeepSeek 的
    前缀缓存因此不受索引随场景增长的影响——只有末尾这一小节重算。索引只进系统消息：它是
    「这个角色知道什么」的全量入口，与每块变化的对话视图无关，挂到用户消息会让前缀每块重算。
    标题由 `knowledge.render_index` 渲染后原样过来，故注入后只出现一次（本处不重复声明）。
    """
    kwargs = dict(view_text="v", last_chunk_text="c", scene_text="s",
                  language_directive=_LANG, background_text=_BG)
    msgs = build_think_messages(_card(), index_text=_INDEX, **kwargs)
    system, user = msgs[0]["content"], msgs[1]["content"]
    assert _INDEX in system                       # 整段原样搬运（含标题与尾注）
    assert system.count("【你的信息索引】") == 1   # 标题不重复声明
    assert system.index("【本次任务的硬性规则】") < system.index("【你的信息索引】"), \
        "索引排在硬性规则之后（系统消息末尾）"
    assert system.index(f"【场景背景】{_BG}") < system.index("【你的信息索引】")
    assert system.index("【你的信息索引】") < system.index(_LANG)
    assert system.endswith("\n\n" + _LANG), "语言指令恒为最后一段"
    # 索引绝不漏进用户消息；用户消息与不传索引时逐字节相同
    assert "【你的信息索引】" not in user
    assert user == build_think_messages(_card(), **kwargs)[1]["content"]


def test_speak_index_in_system_and_recall_in_user():
    """speak：index_text 进**系统**末尾（语言指令之前）；recall_text 进**用户**消息。

    think 与本块的 speak 是同一个角色的连续两步：think 阶段取用过的条目正文必须延续到
    开口，否则回忆完了又失忆（§6.4）。recall 是私有内容（只有这名角色读过），所以走用户
    消息、紧挨本人私有小节；index 则与 think 同位（系统末尾），保住前缀稳定。
    """
    kwargs = dict(view_text="v", scene_text="s", language_directive=_LANG,
                  state_text="唤醒 0.60", impressions_text="对 丙 的印象：她话里有话")
    msgs = build_speak_messages(_card(), index_text=_INDEX, recall_text=_RECALL, **kwargs)
    system, user = msgs[0]["content"], msgs[1]["content"]
    assert _INDEX in system and system.count("【你的信息索引】") == 1
    assert system.index("【说人话（去 AI 味）】") < system.index("【你的信息索引】")
    assert system.index("【你的信息索引】") < system.index(_LANG)
    assert system.endswith("\n\n" + _LANG)
    assert "【你的信息索引】" not in user, "索引只进系统消息"
    assert _RECALL in user, "取用到的条目正文进用户消息"
    assert "【你想起的事】" in user
    assert "【你想起的事】" not in system, "recall 是私有内容，绝不进系统消息"
    # 用户消息顺序：私有小节 → 想起的事 → （此刻·上一句） → 开口收尾
    assert (user.index("【你的近期状态/想法】") < user.index("【你想起的事】")
            < user.index("你现在开口：")), "想起的事紧挨私有小节、在开口指令之前"


def test_recall_section_sits_after_private_and_before_prev_line():
    """想起的事与私有小节、此刻·上一句的相对位置固定：私有 → 想起 → 最新一句 → 开口。

    最新一句（决定要不要开口的直接原因）必须贴着收尾指令，故回忆插在它之前。
    """
    user = build_speak_messages(
        _card(), view_text="v", scene_text="s", state_text="唤醒 0.60",
        impressions_text="对 丙 的印象：她话里有话",
        recall_text=_RECALL, prev_line_text="[1] 丙: 你倒是说句话。",
        plot_text=_PLOT)[1]["content"]
    assert (user.index("【你的近期状态/想法】") < user.index("【你想起的事】")
            < user.index("【此刻·上一句】") < user.index("【剧情走向")
            < user.index("你现在开口：")), "speak 用户小节顺序"


def test_recall_renders_even_without_private_state_and_omits_empty_titles():
    """只有 recall 而无私心态时：仍渲染「你想起的事」，且不给空标题留位（不留空段）。"""
    user = build_speak_messages(_card(), view_text="v", scene_text="s",
                                recall_text=_RECALL)[1]["content"]
    assert "【你想起的事】" in user and _RECALL in user
    assert "【你的近期状态/想法】" not in user
    assert "【你的近期状态】" not in user and "【你对ta的印象】" not in user
    # 想起的事直接跟在对话视图之后，中间不留空标题/空段
    assert "【你看到的最近对话】v\n\n【你想起的事】" in user


def test_recall_section_warns_against_reciting_and_meta_talk():
    """想起的事小节头必须给去复读与防漏底告诫：原样念出来就成复读，提"条目/索引"会把
    系统机制漏进对白（本仓库吃过复读原句的亏）。"""
    user = build_speak_messages(_card(), view_text="v", scene_text="s",
                                recall_text=_RECALL)[1]["content"]
    assert "【你想起的事】" in user
    assert "别把这段原样念出来" in user
    assert "条目" in user and "索引" in user


def test_index_text_default_and_blank_leave_output_byte_identical():
    """index_text 缺省/空串/纯空白 → 与不传该参逐字节相同（缺省路径不漂移）。

    无库角色（§6.1 验收硬约束）走的就是这条：`render_index` 返回空串，系统提示一字不变。
    """
    for kw in ({}, {"index_text": ""}, {"index_text": "   \n "}, {"index_text": "\n\n"}):
        assert build_think_messages(_card(), view_text="v", last_chunk_text="c",
                                    scene_text="s", **kw) == \
            build_think_messages(_card(), view_text="v", last_chunk_text="c", scene_text="s")
        assert build_speak_messages(_card(), view_text="v", scene_text="s", **kw) == \
            build_speak_messages(_card(), view_text="v", scene_text="s")
    # 缺省路径仍等于加本特性前的逐字基线，且绝无索引小节的痕迹（不留空标题/空行）
    think = build_think_messages(_card(), view_text="v", last_chunk_text="c", scene_text="s",
                                 index_text="")
    speak = build_speak_messages(_card(), view_text="v", scene_text="s", index_text="")
    assert think[0]["content"] == _BASELINE_THINK_SYSTEM
    assert speak[0]["content"] == _BASELINE_SPEAK_SYSTEM
    assert "【你的信息索引】" not in (think[0]["content"] + speak[0]["content"])


def test_recall_text_default_and_blank_leave_output_byte_identical():
    """recall_text 缺省/空串/纯空白 → 与不传该参逐字节相同，且不留空小节头。"""
    base = build_speak_messages(_card(), view_text="v", scene_text="s",
                                state_text="唤醒 0.50", prev_line_text="[1] 丙: 你好。")
    for kw in ({}, {"recall_text": ""}, {"recall_text": " \n\t "}):
        assert build_speak_messages(_card(), view_text="v", scene_text="s",
                                    state_text="唤醒 0.50",
                                    prev_line_text="[1] 丙: 你好。", **kw) == base
    assert "【你想起的事】" not in (base[0]["content"] + base[1]["content"])
    # think 侧本就不收 recall_text（取用发生在 think 内部的工具循环里，§6.3），
    # 用户消息里绝不该冒出这一节
    think = build_think_messages(_card(), view_text="v", last_chunk_text="c", scene_text="s")
    assert "【你想起的事】" not in (think[0]["content"] + think[1]["content"])


def test_index_section_normalizes_surrounding_blank_lines_deterministically():
    """同一输入的两种等价写法（尾随/前导空行）渲染逐字节相同：小节之间只留一个空行。

    确定性要求：注入的是渲染结果而非调用方的拼接手艺，边界空白一律由本节裁掉。
    """
    a = build_think_messages(_card(), view_text="v", last_chunk_text="c", scene_text="s",
                             index_text=_INDEX, language_directive=_LANG)
    b = build_think_messages(_card(), view_text="v", last_chunk_text="c", scene_text="s",
                             index_text="\n\n" + _INDEX + "\n\n", language_directive=_LANG)
    assert a == b
    assert a == build_think_messages(_card(), view_text="v", last_chunk_text="c",
                                     scene_text="s", index_text=_INDEX,
                                     language_directive=_LANG)


def test_index_sits_after_legacy_own_previous_rule_in_speak_system():
    """speak 旧式路径（own_previous 且无归属行 → 系统侧旧规则）：索引仍排在**末尾**、
    旧规则与语言指令之间——系统侧任何追加都不得把索引挤到中间（前缀稳定 + 恒为末段）。"""
    msgs = build_speak_messages(_card(), view_text="v", scene_text="s", own_previous=True,
                                index_text=_INDEX, language_directive=_LANG)
    system = msgs[0]["content"]
    assert (system.index("上一句是你自己说的") < system.index("【你的信息索引】")
            < system.index(_LANG))
    assert system.endswith("\n\n" + _LANG)


def test_index_and_recall_do_not_cross_contaminate_each_builder_default():
    """两个新参互不串味：给 think 传 index_text 不改 speak 缺省，给 speak 传两参也不改
    think 缺省；各自缺省路径仍等于改动前的逐字基线。"""
    think_kwargs = dict(view_text="v", last_chunk_text="c", scene_text="s")
    speak_kwargs = dict(view_text="v", scene_text="s")
    think_default = build_think_messages(_card(), **think_kwargs)
    speak_default = build_speak_messages(_card(), **speak_kwargs)
    assert think_default[0]["content"] == _BASELINE_THINK_SYSTEM
    assert speak_default[0]["content"] == _BASELINE_SPEAK_SYSTEM

    think_with_index = build_think_messages(_card(), index_text=_INDEX, **think_kwargs)
    # think 侧只动系统消息；用户消息（含 JSON 收尾）逐字节不变
    assert think_with_index[1]["content"] == think_default[1]["content"]
    assert "请输出你的 think JSON" in think_with_index[1]["content"]
    # think 带索引不影响 speak 的缺省（两处独立装配，不共享状态）
    assert build_speak_messages(_card(), **speak_kwargs) == speak_default

    speak_with_new = build_speak_messages(_card(), index_text=_INDEX, recall_text=_RECALL,
                                          **speak_kwargs)
    assert speak_with_new != speak_default
    assert "【你的信息索引】" not in speak_with_new[1]["content"]   # 索引不进用户消息
    assert _RECALL not in speak_with_new[0]["content"]              # recall 不进系统消息
    # speak 带两参不影响 think 的缺省
    assert build_think_messages(_card(), **think_kwargs) == think_default


def test_index_and_recall_are_additive_on_full_representative_paths():
    """在带语料+全部场景参+私有态+own_last/prev_line 的代表性路径上，索引与 recall 只做加法：
    锁死 JSON 字段、身份锁、去 AI 味、场景小节与语言指令收尾全部仍在。"""
    think = build_think_messages(_corpus_card(), view_text="v", last_chunk_text="c",
                                 scene_text="s", state_text="唤醒 0.50",
                                 impressions_text="对 丙 的印象：话里有话", own_last=True,
                                 index_text=_INDEX, **_SCENE_KW)
    blob = think[0]["content"] + think[1]["content"]
    assert _INDEX in blob and "json" in blob.lower()
    for fld in ('"aroused"', '"obligation_fulfilled"', '"goal_progress"',
                '"addressed"', '"impression_of_speaker"', '"urge"'):
        assert fld in blob, f"schema 字段丢失：{fld}"
    assert "绝不模仿" in blob and "刚听到的是你自己说的话" in blob
    assert think[0]["content"].endswith("\n\n" + _LANG)

    speak = build_speak_messages(_corpus_card(), view_text="v", scene_text="s",
                                 state_text="唤醒 0.60", recall_text=_RECALL,
                                 prev_line_text="[1] 丙: 你倒是说句话。",
                                 index_text=_INDEX, **_SCENE_KW)
    system, user = speak[0]["content"], speak[1]["content"]
    assert _INDEX in system and system.endswith("\n\n" + _LANG)
    assert "【人物语料·出处】《示例作品》（占位）" in system
    assert "【说人话（去 AI 味）】" in system and "绝不模仿" in system
    assert _RECALL in user and user.endswith("你现在开口：")
    assert f"【场景背景】{_BG}" in system and "【场景背景】" not in user


def test_index_text_accepts_render_index_output_verbatim():
    """接缝钉子：把 `knowledge.render_index` 的产物直接喂 index_text，标题只出现一次。

    渲染方拥有标题/尾注文案（§6.1），prompters 只搬运——两模块的接缝正好在这里。哪天
    prompters 自作主张再拼一个【你的信息索引】，这条会立刻红（同一句话两处定义必然漂移）。
    空库渲染成空串 → 系统提示与不传索引逐字节相同（无库角色的验收硬约束）。
    """
    from harness.knowledge import Entry, render_index
    text = render_index([Entry(key="药铺的暗格", subject="药铺",
                               summary="柜台下第三块砖是空的")])
    assert text.startswith("【你的信息索引】")
    kwargs = dict(view_text="v", last_chunk_text="c", scene_text="s",
                  language_directive=_LANG)
    system = build_think_messages(_card(), index_text=text, **kwargs)[0]["content"]
    assert text in system and system.count("【你的信息索引】") == 1
    assert system.endswith("\n\n" + _LANG)
    assert render_index([]) == ""
    assert build_think_messages(_card(), index_text=render_index([]), **kwargs) == \
        build_think_messages(_card(), **kwargs)


def test_narrate_signature_stays_free_of_knowledge_params():
    """narrate 不动（§6.1）：签名里没有信息库参数——场景（世界）是全知的旁白，不该因为
    某个角色的私人索引/回忆改口吻，那会破掉信息边界。"""
    params = set(inspect.signature(build_narrate_messages).parameters)
    assert "index_text" not in params and "recall_text" not in params
    base = build_narrate_messages("v", "s", "c", "r", **_SCENE_KW)
    for marker in ("【你的信息索引】", "【你想起的事】"):
        assert marker not in (base[0]["content"] + base[1]["content"])


# ------------------------------------------------- 散场总结（§7.1 / §7.3） -----
#
# 散场时为**每个在场角色**各写一条枢纽条目（§7.1）：不是给这场戏写梗概，是"他自己"的一份
# 收束，末尾把他这一场记下的条目写成 [[双链]]，让它成为图里天然的枢纽节点。模板独立，
# 绝不复用 _THINK/_SPEAK（口径完全不同：一个要锁死 JSON、一个是要说出口的话，而这一条
# 是写给自己看的备忘）；调用方用 narrate 档后端调它（§13.3：复用 narrate，不新增档位），
# 故它也只输出一段文字、不含 JSON。

_DIGEST = [("药铺的暗格", "药铺的暗格", "柜台下第三块砖是空的"),
           ("那封信", "夹层里的信", "陈掌柜没打开过")]


def _summary(**kw):
    base = dict(view_text="[1] 丙: 今晚这桌我请。", scene_text="餐厅，10 点打烊")
    base.update(kw)
    return build_summary_messages(_card(), **base)


def test_summary_messages_carry_card_scene_view_and_entry_digest():
    """散场总结的输入齐了（§7.1）：角色卡（谁在写）、场景（哪一场）、整场对话（他看见的）、
    条目清单（他这一场记下的：键 + 标题 + 一行摘要）。两个角色都用同一份输入格式，以便
    引擎对每个在场者各调一次。"""
    msgs = _summary(entry_digest=_DIGEST, present=["乙", "丙"])
    assert [m["role"] for m in msgs] == ["system", "user"]
    system, user = msgs[0]["content"], msgs[1]["content"]
    assert "你是角色 乙" in system and '"描述": "冷静、观察多"' in system
    assert "【这一场】餐厅，10 点打烊" in user
    assert "【在场的人】乙、丙" in user
    assert "[1] 丙: 今晚这桌我请。" in user
    assert "[[药铺的暗格]]" in user and "柜台下第三块砖是空的" in user
    assert "[[那封信]]" in user and "夹层里的信" in user   # 键与标题都给，链的是键


def test_summary_prompt_demands_the_characters_own_voice_not_a_recap():
    """以**这个角色自己**的口吻与视角写（§7.1）：他知道什么、他拿走了什么——不是上帝视角的
    剧情梗概。去 AI 味的约定与 speak/narrate 同一条线（朴素、不抒情、不写金句、不用破折号
    堆砌、无 emoji）；输出是**一段正文**，不走 JSON。"""
    system = _summary()[0]["content"]
    assert "第一人称" in system and "你自己的眼睛" in system
    assert "不是给这场戏写的梗概" in system
    assert "别用第三人称写你自己" in system
    assert "不抒情" in system and "不写金句" in system
    assert "破折号" in system and "emoji" in system
    assert "不要 JSON" in system


def test_summary_prompt_asks_for_the_link_tail_so_it_becomes_a_hub():
    """末尾附上他本场各条目的双链标记 [[键]]（§7.1）：这条总结是图里的枢纽节点，日后想回忆
    "那一夜"从它进、顺链散开。所以必须点名"照抄清单里的条目名"——模型自己改写名字就会链到
    不存在的键上，枢纽当场断掉。"""
    blob = "".join(m["content"] for m in _summary(entry_digest=_DIGEST))
    assert "[[条目名]]" in blob
    assert "照抄" in blob and "一个字都别改" in blob
    assert "清单是空的就不用写那一行" in blob


def test_summary_with_an_empty_digest_says_so_and_asks_for_no_links():
    """本场什么都没记下也照样总结（散场对每个在场角色都要跑一次）：清单位置给一句明确的
    占位，别留一段空白让模型自己编几个条目名出来。"""
    user = _summary(entry_digest=())[1]["content"]
    assert "你没记下什么新的东西" in user
    assert "[[" not in user


def test_summary_language_directive_is_the_last_paragraph_and_optional():
    """语言指令恒为系统消息**最后一段**（与其余 builder 同一约定；用户消息不受影响），
    且缺省/空串/纯空白一字不追加（缺省路径不漂移）。"""
    base = _summary()
    with_lang = _summary(language_directive=_LANG)
    assert with_lang[0]["content"] == base[0]["content"] + "\n\n" + _LANG
    assert with_lang[1]["content"] == base[1]["content"]
    for blank in ("", "   ", "\n\n"):
        assert _summary(language_directive=blank) == base


def test_summary_builder_does_not_touch_the_run_of_play_builders():
    """新增的散场总结绝不动演出用的那几套（模板独立）：既有 builder 的缺省输出仍与加本特性
    前的**逐字基线**相同，且三者的专属小节一个都没漏进总结提示词。"""
    think = build_think_messages(_card(), view_text="v", last_chunk_text="c", scene_text="s")
    speak = build_speak_messages(_card(), view_text="v", scene_text="s")
    narrate = build_narrate_messages("v", "s", "c", "r")
    assert think[0]["content"] == _BASELINE_THINK_SYSTEM
    assert speak[0]["content"] == _BASELINE_SPEAK_SYSTEM
    blob = "".join(m["content"] for m in _summary(entry_digest=_DIGEST))
    for marker in ("【你的信息索引】", "【你想起的事】", "【你的已知边界】", "【怎么想】",
                   "【说人话（去 AI 味）】", "世界本身", "请输出你的 think JSON",
                   "【当前时刻】"):
        assert marker not in blob, f"散场总结串进了别的 builder 的东西：{marker}"
    assert "[[条目名]]" not in "".join(
        m["content"] for msgs in (think, speak, narrate) for m in msgs)


# --------------------------------------- knowledge_boundary 撤除（§9.1/§9.2） -----
#: §9.1 撤除的那一整节（撤除前紧跟【角色设定】之后，前导一个空行）。它是本特性**唯一**
#: 一处故意打破"提示词逐字节不变"的地方，故用"拼回去 = 改动前逐字基线"来钉住"只删了它"。
_KB_SECTION = "\n\n【你的已知边界】只知道自己经历和被告知的事"


def test_think_system_drops_the_boundary_section_and_nothing_else():
    """【你的已知边界】整节撤除（§9.1）：责任交给恒在提示词里的信息库索引表。

    判据不是"文本里没有这一节"（太弱），而是：**把这一节拼回它原来那一行位置，得到的
    就是改动前逐字捕获的那份基线**——`_BASELINE_THINK_SYSTEM_WITH_KB`。与此同时今天的
    输出逐字节等于新基线。两条合起来才证明"只动了这一节"，没有顺手重排空白/换行（否则
    以后再也看不出 diff 里真正变了什么）。

    撤除的是**整节**（标题 + 兜底文案 + 前导空行），不是把 {kb} 填成空串——否则提示词里
    会留下一行"【你的已知边界】"的空标题。
    """
    system = build_think_messages(_card(), view_text="v", last_chunk_text="c",
                                  scene_text="s")[0]["content"]
    assert system == _BASELINE_THINK_SYSTEM
    assert "【你的已知边界】" not in system
    assert "只知道自己经历和被告知的事" not in system
    restored = system.replace("\n\n【怎么想】", _KB_SECTION + "\n\n【怎么想】", 1)
    assert restored == _BASELINE_THINK_SYSTEM_WITH_KB, \
        "除了这一节，系统消息必须与改动前逐字节相同"


def test_kb_placeholder_is_gone_from_the_think_template():
    """模板里的 `{kb}` 占位与 `_system_content` 里对应的填充一并撤除（§9.1）。

    留着占位而不填会在渲染时直接抛 KeyError；填成空串则留下一行空标题（见上一条）。
    故这里钉的是"两处一起没了"：模板无占位、`_system_content` 不再接受/传这个键。
    """
    from harness import prompters as _p

    assert "{kb}" not in _p._THINK_SYSTEM_TMPL
    assert "{kb}" not in _p._SPEAK_SYSTEM_TMPL
    assert "knowledge_boundary" not in _p.__dict__ and "kb" not in (
        inspect.signature(_p._system_content).parameters)
    # 系统消息不再受卡上那个退役字段影响：老卡（带 knowledge_boundary）与新卡渲染同一份
    card = CharacterCard.model_validate({"name": "乙", "personality": {"描述": "冷静、观察多"},
                                         "knowledge_boundary": ["甲"]})
    assert build_think_messages(card, view_text="v", last_chunk_text="c",
                                scene_text="s")[0]["content"] == _BASELINE_THINK_SYSTEM


def test_summary_signature_takes_plain_data_not_knowledge_objects():
    """签名只吃**普通数据**（卡 + 文本 + 键/标题/摘要三元组 + 在场者名字），不碰信息库的
    任何类型：引擎拿 `SceneGain.digest()` 直接喂进来，两边不必互相认识（§7.1 的接缝）。
    与信息库注入的两参（index_text / recall_text）无关——那两个是 think/speak 的事。"""
    params = list(inspect.signature(build_summary_messages).parameters)
    assert params[:3] == ["card", "view_text", "scene_text"]
    for name in ("entry_digest", "present", "language_directive"):
        assert name in params
    assert "index_text" not in params and "recall_text" not in params
