"""think/speak 提示词构建（设计文档 §8）。JSON 输出三件套之一：prompt 含 json 与示例。"""
from __future__ import annotations

import json
from typing import Sequence

from .schemas import CharacterCard, Corpus

_THINK_SYSTEM_TMPL = """你是角色 {name}，正在参与一场会话。只依据你确实看见/经历过的信息行事，绝不假装知道没被告知的事。

【角色设定】{personality_json}{corpus}{scene}

【怎么想】
- 先综观整场对话的局势：话题走到哪了、谁在主导、有没有人问过你话你还没回、有没有事情悬着没解决。
- 再落到最新这一句上：它是不是说给你听的？需不需要你接？
- 然后诚实判断此刻该不该开口：
  · 若是别人刚替你把想说的话说了，或你此刻没有新的、实质的内容 —— 你的冲动 urge 应该是**负值或很低**（明确表达"这句我不必说"）。
  · 若有人点了你的名、问了你、或你有非说不可的事 —— 冲动才高。
  · 别把"插一句"当默认动作；沉默是完全正常、且经常正确的选择。

【本次任务的硬性规则】
- 用一次简短思考完成三件事：按上节读懂局势与最新这一句、更新你的私有状态、自标注。
- 这句刚听到的话在用户消息里带说话人标注（行首 [编号] 说话人: 内容）。先想清你在回应谁的哪句话：
  若行首说话人正是你自己，说明你在自续（延续自己的话），须注明是"自续"，不得把它当成别人说的匿名话来接——严禁自问自答。
- 永远以你自己（第一人称，你的角色名 {name}）的口吻思考；绝不模仿、套用或延续**其他角色**的句式/口头禅，尤其不得借用刚在你前面开口那位的话头。绝不用第三人称称呼自己，绝不把自己当作对方来问话/回答。
- 不要重复自己或他人已经说过的话（含意思相近的表述）；若此刻没有新的、实质的话可说，就不要提高开口冲动(urge)。
- 写印象、写理由时也别说套话（"目光带着几分玩味"这类模板禁止）；用朴素的词，像人心里默念。
- 思考结果只输出一段 JSON，不要任何解释性文字（禁止小作文）。
- JSON 严格包含以下键（字段含义与取值范围）：
  "aroused": 0~1 的当前情绪唤醒度
  "obligation_fulfilled": list[str]，这句话兑现了哪些欠你的人情/挑衅（没有则空数组）
  "goal_progress": 0~1，你的私有目标因此推进了多少
  "addressed": 被这句话点名的人名或 null
  "impression_of_speaker": 你对说话人的一句新印象（可为 null）
  "urge": 你想开口的冲动自评（-1~2，可为负=不想说；会作为一项加权计入发言权）
- 只输出这一份 JSON。请输出英文单词 json 引导的正确 JSON 文本。"""

_THINK_USER_HEAD = """【场景】{scene}
【整场对话（自开场至今，逐条带说话人）】{view}
【此刻最新的一句】（行首标注了说话人——可能是别人，也可能正是你自己）
{last_chunk}"""
_THINK_OUTRO = "\n\n请输出你的 think JSON："

_SPEAK_SYSTEM_TMPL = """你是角色 {name}。{personality_json} 用符合角色的口吻说话。只说你知道的事；不知道就不提。{scene}
说出的内容就是一个「言语单位」：一句话或一个简短动作/表情，不要长篇独白。
永远以你自己的身份开口：保持你的第一人称与角色名 {name} 的自我认同，绝不模仿、套用或延续**其他角色**的句式/口头禅——尤其不得借用或续写刚在你前面开口那位的话头。绝不用第三人称称呼自己，绝不把自己当作对方来问话或回答。
不得复读或几乎复读自己或任何角色已说过的话（含意思、动作与措辞相近）；哪怕隔了很多轮，也绝不把场景里已有人说过的话当作新内容再念一遍；如果此刻没有新内容可说，宁可沉默。
别硬接一句显然是说给别人听的话——除非真需要你回应，否则不必接。

【说人话（去 AI 味）】
- 破折号"——"几乎不要用。只有两种例外：话被抢断或指认（"你看——""那边——"）、强说明（"比如说——"）。其余一律改用逗号、句号或直接断句。
- 不用"不仅…而且…""不是…而是…"这类排比；不用"此外/然而/值得注意的是/总而言之/某种意义上"这类连接词；不要堆四字词。
- 别写金句、别做总结陈词、别给对话升华意义。说完事就停。
- 句子长短要错开。别每句一样长，也别每句都工整。
- 说具体的东西（杯子、灯、菜、钱、几点），少抽象抒情。
- 神态/动作（括号里的）要短、要具体、别用模板：不要通篇"目光带着几分玩味""指尖轻点桌面""唇角微扬""语气平淡"这类套话；同一个神态不要重复用；能不加就不加。
- 允许口语的碎、停顿、跑题、半截话。真人说话就是这样。
- 不用粗体、编号、列表、emoji。{corpus}"""

_SPEAK_USER_HEAD = """【场景】{scene}
【你看到的最近对话】{view}"""
_SPEAK_USER_OUTRO = "\n\n你现在开口："
_SPEAK_PRIVATE_HEADER = ("【你的近期状态/想法】（你此刻的内心：最近的状态走向与对ta的印象。"
                         "只用来帮你顺着自己的处境开口、保持自己是自己，不要把这些心里话"
                         "直接讲出来）")

#: 追加于 speak 系统规则之后（仅 own_previous=True 且 prev_line_text 为空——旧式纯函数
#: 调用，无归属行可渲染）：上一句是自己说的——开口只能是自然续说（接着说新内容），不是
#: 回应他人。不得用对答句式、不得复读、不得假装有人接话。
#: 有归属行（prev_line_text 非空）时这段语义折叠进用户消息的「此刻·上一句」小节——该小节
#: 按行内说话人判定自续 vs 回应他人，系统侧不再重复追加，避免两处同义文本。
_SPEAK_OWN_PREVIOUS_RULE = (
    "\n\n上一句是你自己说的——若你开口，应是自然的延续（接着表达新内容），不要用回应"
    "对手的句式、不要复读自己刚说的内容、不要假装别人刚回答了。"
)

#: 「此刻·上一句」因果锚点小节（仅 prev_line_text 非空时插入，位于本人私有小节之后、
#: 「你现在开口」之前）。与 think 的「刚听到这一句」对称：speak 也必须有"最新一句是谁
#: 说的/是否在叫我回应"的因果锚点，杜绝角色自答自己的话或对幻影接话者说话。
#: 行内自带说话人归属与 →点名；判定规则把"说话人正是你自己（自续≠回应）"与"他人说的
#: （判断是否点名我）"分开，并把旧 _SPEAK_OWN_PREVIOUS_RULE 的语义折进自续分支。
_SPEAK_PREV_LINE_HEAD = "【此刻·上一句】{prev}\n"
_SPEAK_PREV_LINE_BODY = (
    "（这段是对话里最新的一行；它就是上表最后一列的内容，请不要把它当\"新内容\"再复读。）\n"
    "判断与行动：\n"
    "- 若它的说话人正是你自己：你现在只能\"自然续说\"——接着说新的实质内容；不得用回应"
    "对手的句式、不得复读自己刚说的、不得假装别人刚接了话。\n"
    "- 若是他人说的：先判断这句话是否在对你/要求你回应（行内若有 →点名，那就是对你的）。"
    "需要你回应→回应它；不需要、而你此刻也没有新的、能推进场景的话→宁可沉默。"
)


def _prev_line_section(prev_line_text: str) -> str:
    """「此刻·上一句」小节的完整用户侧文本（含归属行）。"""
    return (_SPEAK_PREV_LINE_HEAD + _SPEAK_PREV_LINE_BODY).format(prev=prev_line_text)

#: 追加于 think 用户消息「此刻最新的一句」之后（仅 own_last=True）：上句归属自己，冲动的
#: 唯一合法来源是"真有新的实质内容要接着说"，而不是"想回答那句话"。
_THINK_OWN_LAST_RULE = (
    "\n\n【说话权：你刚听到的是自己的话】刚听到的是你自己说的话——不要把它当作别人要你"
    "回应的话；只有当你有新的、实质的内容想接着说时才提高说话的冲动(urge)；若是想"
    "回答自己刚说的，冲动应保持低（倾向沉默）。"
)


#: 语料小节：样例头部必须带"绝不照抄"告诫（样例是风格锚点，不是内容素材；此前吃过
#: 复读原句的亏）。样例限量限长，防止整段语料把系统提示撑爆、盖过角色设定与硬规则。
_CORPUS_SAMPLE_HEADER = "【代表性台词样例（模仿其口吻与思维，但绝不照抄样例内容或原句）】"
_CORPUS_MAX_SAMPLES = 8
_CORPUS_SAMPLE_MAX_CHARS = 120


def _corpus_section(corpus: Corpus | None) -> str:
    """把人物语料渲染成中文小节；空语料/全空子项 → ''（整体或逐行省略）。

    只描述"怎么说话、怎么想"：出处/语言风格/思维方式/口头禅/样例。样例取前 8 条、
    每条裁到 ~120 字；样例头部固定带不照抄告诫。语料为空时返回 ''，调用方据此保证
    系统提示逐字节不变（不留空标题、不留多余空行）。
    """
    if corpus is None:
        return ""
    lines: list[str] = []
    if corpus.source and corpus.source.strip():
        lines.append(f"【人物语料·出处】{corpus.source.strip()}")
    if corpus.style and corpus.style.strip():
        lines.append(f"【语言风格】{corpus.style.strip()}")
    if corpus.thinking and corpus.thinking.strip():
        lines.append(f"【思维方式】{corpus.thinking.strip()}")
    quirks = [q.strip() for q in corpus.quirks if q and q.strip()]
    if quirks:
        lines.append(f"【口头禅】{'、'.join(quirks)}")
    samples = [s.strip()[:_CORPUS_SAMPLE_MAX_CHARS]
               for s in corpus.samples if s and s.strip()]
    if samples:
        lines.append(_CORPUS_SAMPLE_HEADER)
        lines.extend(f"- {s}" for s in samples[:_CORPUS_MAX_SAMPLES])
    return "\n".join(lines)


#: 场景小节头（系统消息）：世界观/设定 + 场景描述。均为「非空才渲染」——全空时系统提示
#: 逐字节等于加场景特性之前（缺省路径不漂移）。
_SCENE_BACKGROUND_HEADER = "【场景背景】"
_SCENE_DESCRIPTION_HEADER = "【场景描述】"

#: 剧情走向小节头（用户消息侧，narrate 在系统侧）：点明这是**作者意图**——用来指导行动，
#: 不是拿来念的台词素材（防模型把走向复述进对白）。
_PLOT_HEADER = "【剧情走向（作者意图，参考它行动，不要直接复述）】"


def _scene_sections(background_text: str, description_text: str) -> str:
    """把场景的「背景信息（世界观/设定）」与「场景描述」折成系统消息小节；全空 → ''。

    逐小节独立省略（缺哪节就整行不留，不给空标题）；文本两端空白裁掉。空串/纯空白
    都算空——调用方据此保证系统提示逐字节不变。
    """
    parts: list[str] = []
    if background_text and background_text.strip():
        parts.append(f"{_SCENE_BACKGROUND_HEADER}{background_text.strip()}")
    if description_text and description_text.strip():
        parts.append(f"{_SCENE_DESCRIPTION_HEADER}{description_text.strip()}")
    return "\n\n".join(parts)


def _plot_section(plot_text: str) -> str:
    """「剧情走向（作者意图）」小节；空 → ''（不渲染空标题）。"""
    if plot_text and plot_text.strip():
        return f"{_PLOT_HEADER}{plot_text.strip()}"
    return ""


def _language_directive_line(language_directive: str) -> str:
    """语言指令（如「你将使用简体中文回答。」）：非空时作系统消息**最后**一段追加。

    只在非空时追加，且由各 builder 在**所有**其他追加（own_previous 旧规则等）之后调用，
    确保它恒为系统消息的最后一行、且只出现一次；空 → ''，缺省路径逐字节不变。
    """
    if language_directive and language_directive.strip():
        return "\n\n" + language_directive.strip()
    return ""


def _relation_section(relation_text: str) -> str:
    """【你的人际关系】小节（《人际关系与场景推进》§6.2）：`relation_text` **原样**整段返回；
    空/纯空白 → ''。

    与 `_index_section` 同一条纪律：文案的唯一定义源在 `relations.render_relations`
    （标题、行、尾注都在那里渲染），prompters 只做搬运——同一句话两处定义必然漂移；
    "没有任何关系行 → 空串"那条硬约束也落在那里（无关系角色的系统提示逐字节不变）。

    它比索引更"该在"（§6.2：索引是按需取用的入口，关系表是每次开口都要用的），故拼在
    **信息库索引之前**、语言指令之前：关系表几乎不变，而索引每 remember 一次就变——把变的
    留在系统消息末尾，DeepSeek 的前缀缓存能多吃一截（索引那一节的位置理由见
    `build_think_messages`）。
    """
    if relation_text and relation_text.strip():
        return relation_text.strip()
    return ""


def _index_section(index_text: str) -> str:
    """信息库索引小节（§6.1）：`index_text` **原样**整段返回；空/纯空白 → ''。

    两件事说清楚，改之前先读：

    1. **文案的唯一定义源不在这里**。索引小节的标题「【你的信息索引】」与尾注由
       `knowledge.render_index` 渲染（§6.1 逐字节给定），本函数只做搬运——prompters 若
       再拼一次同样的标题，同一句话就有两处定义，早晚漂移；`render_index` 在无可显示
       条目时也返回空串，正好对上「无库角色系统提示逐字节不变」那条硬约束。
    2. **边界空白一律裁掉**（首尾整体 strip，内部一字不动）：小节之间只留一个空行，
       调用方手滑带进来的前导/尾随空行不会改变输出（确定性：同输入逐字节同输出）。
    """
    if index_text and index_text.strip():
        return index_text.strip()
    return ""


#: speak 的「你想起的事」小节头（§6.4）：think 阶段取用到的条目正文，延续到本块开口。
#: 私有内容（本场只有这名角色读过），故进**用户**消息、紧挨本人私有小节——与「近期状态/
#: 印象」走同一条路（都是只喂这名听众的），不新开信息边界。括号里的两句告诫各有来由：
#: 原样念出来就是复读（仓库吃过复读的亏），提"条目/索引"会把系统机制漏进对白。
_SPEAK_RECALL_HEADER = ("【你想起的事】（你此刻翻出来的旧内容；顺着它开口，"
                        "别把这段原样念出来，也别提“条目”“索引”这类系统词。）")


def _recall_section(recall_text: str) -> str:
    """speak 的「你想起的事」小节正文（小节头 + 裁过空白的取用正文）；空 → ''。

    只给**用户**消息（私有）。空/纯空白 → ''，不留空标题、不留多余空行。
    """
    if recall_text and recall_text.strip():
        return f"{_SPEAK_RECALL_HEADER}\n{recall_text.strip()}"
    return ""


def _system_content(tmpl: str, card: CharacterCard, scene_section: str = "") -> str:
    """{corpus}/{scene} 占位统一填充：非空才补前导空行——空语料/无场景时渲染出的系统提示
    与对应特性加入前逐字节相同（缺省路径不因新特性漂移）。

    语言指令不在这里拼：由各 builder 在全部追加结束后调 _language_directive_line，保证
    它恒为系统消息最末一段。
    """
    import json
    corpus = _corpus_section(getattr(card, "corpus", None))
    if corpus:
        corpus = "\n\n" + corpus
    scene = "\n\n" + scene_section if scene_section else ""
    return tmpl.format(
        name=card.name,
        personality_json=json.dumps(card.personality, ensure_ascii=False),
        corpus=corpus,
        scene=scene,
    )


def system_prompt(card: CharacterCard) -> str:
    return _system_content(_SPEAK_SYSTEM_TMPL, card)


def _speak_private_section(state_text: str, impressions_text: str) -> str:
    """speak 用的本人私有小节：与 think 相同的「近期状态/对ta印象」渲染，外包一个
    「你的近期状态/想法」引导头。全空 → ''。speak 不收 JSON，仅供开口前的自我参照，
    让说话人带着自己刚更新的内心连续感开口，而不是只盯紧邻那一声他人的话去锚定模仿。"""
    inner = _private_section(state_text, impressions_text)
    if not inner:
        return ""
    return f"{_SPEAK_PRIVATE_HEADER}\n\n{inner}"


def _private_section(state_text: str, impressions_text: str) -> str:
    """把「近期私有状态/对ta的印象」折成用户消息小节的正文；全空 → ''。

    本节与共享态无关（仅进本听众自己的 think prompt）：think worker 在构图前读自己
    的记忆文件喂进来，让倾向随私有状态演变，同时保持锁死的 ThinkResult JSON 收尾。
    """
    sections: list[str] = []
    if state_text and state_text.strip():
        sections.append(f"【你的近期状态】\n{state_text.strip()}")
    if impressions_text and impressions_text.strip():
        sections.append(f"【你对ta的印象】\n{impressions_text.strip()}")
    return "\n\n".join(sections)


#: 叙述（场景）系统提示：不是角色，是世界本身（全知）。只在对话卡住/需要外界发生点
#: 事时开口，写一到两行「实际发生了什么」——不写内心、不替角色说话、不设计后续剧情。
#: 去 AI 味约定与 speak 一致（破折号/排比/升华/列表一律不要）；输出是所有人都能看见的
#: 客观事实，故只写他们能观察到的东西。
_NARRATE_SYSTEM = """你是这场戏的「场景」——不是角色，而是世界本身（全知）。你只在对话卡住、或需要外界发生点事把故事往前推时开口，写一到两行"实际发生了什么"。
规则：
- 只写发生的事（客观事件、时间地点变化、旁人的动作），不写任何角色的内心，不替角色说话。
- 一到两行，短。像小说里的一句过场，不要铺陈、不要抒情、不要升华、不要总结意义。
- 承接当前局势往前推一步就好，不要替后面的剧情做设计，不要一次安排多步走向。
- 不用破折号"——"（除非抢断或强说明）；不用列表、粗体、emoji。
- 你要写的事是所有人都能看见的，所以只写他们能观察到的事实。
- 场景描述若标注「可变」、且这一场刚刚改动过它，叙述就要体现改动后的新状态（以最新描述为准）。"""

_NARRATE_USER_TMPL = """【整场对话】
{view_text}
【场景】{scene_text}
【当前时刻】{clock_text}
【现在需要你推进的原因】{reason_text}

请写一到两行："""

#: 钩子条件里「时间条件」的判定契约（补强 `[[HOOK:…]]` 的报告契约，**只在场景有启用
#: 钩子时**由引擎传入 `hook_time_rule`）。实况事故：模型看着「虚拟钟走到 21:30」的条件，
#: 在 19:03 就报告了它（两人提前 2.5 小时离场）。提示词这一侧要把话说死——当前时刻就在
#: 【当前时刻】里，钟点没到就不许报；引擎另有一道确定性闸门（engine._time_gate）兜底，
#: 两处口径必须一致（这里说"没到就别报"，那里"报了也不执行"）。
TIME_CONDITION_RULE = (
    "【时间条件】当前钟点在用户消息的【当前时刻】里给出。条件里带钟点的钩子"
    "（如「虚拟钟走到 21:30」）必须**严格对照这个钟点**判断：那个点还没到，就绝"
    "**不要报告**这条钩子——「快了」「差不多了」都不算到点，到点之前它就是不成立。"
    "没有钟点的钩子（如「有人问起甲的过去」）照常按局势判断，不受此限。")


def build_narrate_messages(view_text: str, scene_text: str, clock_text: str,
                           reason_text: str, language_directive: str = "",
                           background_text: str = "", description_text: str = "",
                           plot_text: str = "", activity_hint: str = "",
                           hook_time_rule: str = "") -> list[dict]:
    """场景叙述提示词：整场对话（**不做 knows 过滤**，场景全知）+ 场景 + 当前时刻 +
    需要推进的原因，收尾仍是「请写一到两行：」。

    view_text 由调用方传**未撤销**的全部消息（撤销的叙述视同没发生过，绝不能进这里）；
    空视图落「（暂无）」占位，与 think/speak 同约定。

    场景五参（均可选，缺省 '' → 用户消息逐字节不变）：
    - background_text/description_text → **系统**消息的【场景背景】/【场景描述】小节；
    - plot_text → **系统**消息的【剧情走向（作者意图…）】小节（narrate 侧放系统：作者意图
      是对「世界该怎么动」的约束，不是要说出口的话，放系统与全知口吻一致；think/speak
      侧才放用户消息——那里它是角色自己的行动参考）；
    - activity_hint（§6.2，推进活跃度）→ **系统**消息的一段（"当前活跃度：低/中/高/极高，
      请相应地少/多介入；只判钩子时只写指令行、不要写叙述正文"）。由引擎按活跃度档位给出
      （scenarist.activity_hint），是**给场景自己的频率说明**：引擎只决定"何时叫它"，
      叫来之后要不要真叙述由它自己定；
    - language_directive → 系统消息最末一段；
    - hook_time_rule（缺省 ''）→ 钩子报告契约里的**时间条件**那一段（由引擎传
      TIME_CONDITION_RULE，仅在场景有启用钩子时给）。空/纯空白 → 一字不追加。
    系统消息拼接顺序（确定性）：世界规则（末行含「可变场景描述→体现新状态」提示，恒定存在）
    → 【场景背景】 → 【场景描述】 → 【剧情走向…】 → 活跃度提示 → 时间条件契约 →
    语言指令。用户消息侧不变。

    **叙述是可选的**（§6.2）：调用方（引擎）只在场景真的产出了正文时才落一条叙述消息；
    空产出（只报钩子指令、或什么都不写）是合法的一轮，不算失败。
    """
    content = _NARRATE_USER_TMPL.format(
        view_text=view_text or "（暂无）", scene_text=scene_text,
        clock_text=clock_text or "（未指定）", reason_text=reason_text or "手动推进")
    system = _NARRATE_SYSTEM
    scene = _scene_sections(background_text, description_text)
    if scene:
        system += f"\n\n{scene}"
    plot = _plot_section(plot_text)
    if plot:
        system += f"\n\n{plot}"
    if activity_hint and activity_hint.strip():
        system += f"\n\n{activity_hint.strip()}"
    if hook_time_rule and hook_time_rule.strip():
        system += f"\n\n{hook_time_rule.strip()}"
    system += _language_directive_line(language_directive)
    return [{"role": "system", "content": system},
            {"role": "user", "content": content}]


def build_think_messages(card: CharacterCard, view_text: str, last_chunk_text: str,
                         scene_text: str, state_text: str = "",
                         impressions_text: str = "", own_last: bool = False,
                         language_directive: str = "", background_text: str = "",
                         description_text: str = "", plot_text: str = "",
                         index_text: str = "", relation_text: str = "") -> list[dict]:
    """think 提示词。state_text/impressions_text 缺省 ''（旧调用方不受影响）；
    非空时插到「请输出 ThinkResult JSON」指令之前，JSON 收尾与 schema 字段恒不变。
    view_text 恒为**整场可见转录全文**（不截断）——think 要综观全局形势，不只盯最新一句。
    own_last=True：最新一行归属自己（说话人==听众本人，自续或沉默机会）。
    在「此刻最新的一句」后补一条说话权提醒——不把己句当别人来句回应；除非真有新实质
    内容要接着说才抬 urge，想"回答"自己刚说的话时冲动应低。缺省 False 输出逐字节不变。

    index_text（§6.1，可选，缺省 ''）：信息库索引小节，由 `knowledge.render_index` 渲染好
    （已含【你的信息索引】标题与尾注；空库渲染出空串）。放**系统消息末尾**——`_system_content`
    之后、语言指令之前——是刻意的：前面的角色设定/语料/场景因此保持逐字节稳定，DeepSeek 的
    **前缀缓存**不受索引随场景增长的影响，只有末尾这一小节重算。空/纯空白 → 一字不追加，
    输出与加索引特性前逐字节相同（无库角色是验收硬约束）。索引不进用户消息：它是"我知道
    什么"的入口，挂到用户消息会让每块变化的前缀整体重算。

    relation_text（《人际关系与场景推进》§6.2，可选，缺省 ''）：【你的人际关系】小节，由
    `relations.render_relations` 渲染好（已含标题与尾注；没有任何关系行时是空串）。与索引
    不同，它**全部**进上下文（不截断、不按需取用）——"我对面这个人是谁"每次开口都要用。
    位置在**信息库索引之前**（理由见 `_relation_section`）。空/纯空白 → 一字不追加，输出与
    加关系特性前逐字节相同（无关系角色是验收硬约束）。

    场景四参（均可选，四个都空 → 整条输出与加场景特性前逐字节相同）：
    系统消息拼接顺序（确定性）= 角色卡（【角色设定】+ personality） → 人物语料 → 【场景背景】
    → 【场景描述】 → 身份/硬性规则（怎么想、本次任务的硬性规则、锁死 JSON 字段；§9.1 起
    不再有【你的已知边界】那一节——"他知道什么"由信息库索引表承担）
    → 【你的人际关系】（仅 relation_text 非空） → 【你的信息索引】（仅 index_text 非空）
    → 语言指令（恒为最后一段）。
    用户消息拼接顺序（确定性）= 【场景】+【整场对话】+【此刻最新的一句】 → （own_last 说话权
    提醒，仅 own_last=True） → 私有状态/印象 → 【剧情走向（作者意图…）】（仅 plot_text 非空）
    → 「请输出你的 think JSON：」收尾。
    plot_text 放用户消息：它是给这名角色自己的行动参考（作者意图），不是要说出口的话。
    """
    scene = _scene_sections(background_text, description_text)
    system = _system_content(_THINK_SYSTEM_TMPL, card, scene_section=scene)
    relation = _relation_section(relation_text)
    if relation:
        system += f"\n\n{relation}"
    index = _index_section(index_text)
    if index:
        system += f"\n\n{index}"
    system += _language_directive_line(language_directive)
    head = _THINK_USER_HEAD.format(scene=scene_text, view=view_text or "（暂无）",
                                   last_chunk=last_chunk_text or "（开场，无需回应）")
    private = _private_section(state_text, impressions_text)
    content = head
    if own_last:
        content += _THINK_OWN_LAST_RULE
    if private:
        content += f"\n\n{private}"
    plot = _plot_section(plot_text)
    if plot:
        content += f"\n\n{plot}"
    content += _THINK_OUTRO
    return [{"role": "system", "content": system}, {"role": "user", "content": content}]


def build_speak_messages(card: CharacterCard, view_text: str, scene_text: str,
                         own_previous: bool = False,
                         state_text: str = "", impressions_text: str = "",
                         prev_line_text: str = "",
                         language_directive: str = "", background_text: str = "",
                         description_text: str = "",
                         plot_text: str = "", index_text: str = "",
                         recall_text: str = "", relation_text: str = "") -> list[dict]:
    """speak 提示词。own_previous=True：上一句是说话人自己说的（紧接自续）。

    prev_line_text（归属过的最新一行，`[id] 说话人[ →点名]: 内容`）非空时，在本人私有
    小节之后、「你现在开口」之前插入「此刻·上一句」因果锚点小节：行内自带说话人与点名，
    判断/行动把"正是你自己（只能自续）"与"他人说的（判断是否点名我）"分开——杜绝角色
    自答自己的话或对幻影接话者说话。own_previous 语义折进该小节的自续分支；故 prev_line
    非空时系统侧不再重复追加旧规则。prev_line_text 为空（旧式纯函数调用，无归属行）且
    own_previous=True 时退回旧系统规则追加；缺省输出逐字节不变。
    state_text/impressions_text 缺省 ''（旧调用方不受影响）：非空时在「你现在开口：」
    之前插入「你的近期状态/想法」小节（同 think 的私有渲染，speak 侧此前缺失——没有本人
    内部连续感，角色只能锚定/模仿刚过去的他人那句）。缺省输出逐字节不变。

    index_text（§6.1，可选，缺省 ''）：与 think **同位置**——系统消息末尾、语言指令之前，
    由 `knowledge.render_index` 渲染好后整段搬运（标题与尾注都在里面，空库为空串）。理由同
    think：前缀稳定，索引随场景增长只重算末尾这一小节。

    relation_text（《人际关系与场景推进》§6.2，可选，缺省 ''）：与 think 同位置（索引**之前**）
    ——开口前先知道"对面站的是谁"，与 think 阶段必须看到的那份是同一份（由
    `relations.render_relations` 渲染，没有任何关系行时是空串）。

    recall_text（§6.4，可选，缺省 ''）：**本块取用到的条目正文**（think 的工具循环里
    `read_entry` 翻出来的东西）。它是**私有**内容，故放**用户**消息、紧挨本人私有小节
    （【你的近期状态/想法】）之后、「此刻·上一句」之前，外包一个小节头【你想起的事】。
    理由是这一步不能断：think 与本块的 speak 是同一个角色的连续两步，刚想起的东西必须延续
    到开口——否则回忆完了又失忆，取用的成本白花。最新一句仍紧贴收尾指令（它是"要不要开口"
    的直接原因）。两个参都空/纯空白 → 输出与加本特性前逐字节相同。

    场景四参（均可选，四个都空 → 整条输出与加场景特性前逐字节相同）：
    系统消息拼接顺序（确定性）= 角色卡 → 【场景背景】 → 【场景描述】 → 身份/反复读规则 →
    去 AI 味规则（末行附人物语料） → （own_previous 且无归属行时的旧系统规则） →
    【你的信息索引】（仅 index_text 非空） → 语言指令（恒为最后一段，故排在旧系统规则与
    索引之后，绝不悬在中间）。
    用户消息拼接顺序（确定性）= 【场景】+【你看到的最近对话】 → 私有状态/想法 →
    【你想起的事】（仅 recall_text 非空） → 【此刻·上一句】（仅 prev_line_text 非空） →
    【剧情走向（作者意图…）】（仅 plot_text 非空） → 「你现在开口：」。
    plot_text 放用户消息：给说话人自己的行动参考（作者意图），不是要说出口的词。
    """
    scene = _scene_sections(background_text, description_text)
    system = _system_content(_SPEAK_SYSTEM_TMPL, card, scene_section=scene)
    user = _SPEAK_USER_HEAD.format(scene=scene_text, view=view_text or "（暂无）")
    private = _speak_private_section(state_text, impressions_text)
    if private:
        user += f"\n\n{private}"
    recall = _recall_section(recall_text)
    if recall:
        user += f"\n\n{recall}"
    if prev_line_text:
        # 有归属行：因果锚点小节（含说话人/点名判定）。own_previous 的自续语义已在此小节
        # 的自续分支里，不必再往系统侧追加同义旧规则（避免一处概念两处重复文本）。
        user += f"\n\n{_prev_line_section(prev_line_text)}"
    elif own_previous:
        # 无归属行可渲染的旧式调用 → 维持系统侧旧规则，行为逐字节不变。
        system += _SPEAK_OWN_PREVIOUS_RULE
    plot = _plot_section(plot_text)
    if plot:
        user += f"\n\n{plot}"
    user += _SPEAK_USER_OUTRO
    relation = _relation_section(relation_text)
    if relation:
        system += f"\n\n{relation}"
    index = _index_section(index_text)
    if index:
        system += f"\n\n{index}"
    system += _language_directive_line(language_directive)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# ------------------------------------------------- 散场总结（§7.1，枢纽条目） -----
#
# 散场时为**每个在场角色**各写一条汇总，成为他信息库里的枢纽条目（§7.1）：日后想回忆
# "那一夜"，从这一条进、顺着 [[链接]] 散开。它与 speak/narrate 都不是一路：
#   · 不是演出（speak 是要说出口的话）、也不是全知叙述（narrate 是世界本身），
#     而是"这个角色自己"写下的一份收束；
#   · 不锁 JSON：调用方用 **narrate 档**后端调它（§13.3 已定：复用 narrate，不新增档位），
#     走 complete_text，输出就是一段正文。
# 故模板独立，绝不复用 _THINK/_SPEAK 那几套——三种口径混用哪一套都是错的。

_SUMMARY_SYSTEM_TMPL = """你是角色 {name}。戏散了，你要给自己留下一段话：这一场你看见了什么、知道了什么、拿走了什么，写下来留给你自己日后翻。{personality_json}{corpus}

【怎么写】
- 用你自己的眼睛、你自己的口吻写，第一人称。只写你真在场、真听见、真经历过的；别人心里怎么想、你没在的时候发生了什么，你不知道，就别写。这不是给这场戏写的梗概，是**你**这一场的收束。
- 别用第三人称写你自己，也别替谁总结他是什么样的人。
- 写具体的东西：谁说了什么、你答应了什么、你从谁那里听来了什么。不抒情、不总结意义、不写金句、不给谁下评语。
- 不用破折号"——"（除非抢断或强说明）；不用列表、粗体、编号、emoji。
- 一段话，十行以内，像人写给自己的一条备忘。
- 写完另起一行：把你这一场记下的条目一个不落地写成 [[条目名]] 的样子（名字照抄下面清单里的「条目名」，一个字都别改），日后你从这一条能顺着摸到它们。清单是空的就不用写那一行。
- 只输出这段文字本身：不要 JSON、不要标题、不要任何解释。"""

#: 条目清单小节头（§7.1 的三样：键 + 标题 + 一行摘要）。键必须给——双链要写的就是它。
_SUMMARY_DIGEST_HEADER = "【你这一场记下的条目（条目名 ｜ 标题 ｜ 一行摘要）】"
#: 本场什么都没记下时的占位：不留一段空白让模型自己编几个条目名出来。
_SUMMARY_DIGEST_EMPTY = "（这一场你没记下什么新的东西。）"
#: 条目行里标题与摘要之间的分隔（与索引行同款）。
_SUMMARY_DIGEST_SEP = " — "
_SUMMARY_PRESENT_HEADER = "【在场的人】"
_SUMMARY_OUTRO = "\n\n请写你自己这一场的收束："


def _one_line(text: object) -> str:
    """压成一行：清单是一行一条，标题/摘要里混进的换行会毁掉格式。"""
    return " ".join(str(text or "").split())


def _summary_digest(entry_digest: Sequence[tuple[str, str, str]]) -> str:
    """本场条目清单小节：每行 `- [[键]] 标题 — 一行摘要`。空清单 → 一句明确的占位。

    行是三元组（键, 标题, 摘要）；多给的忽略、少给的按空、键为空的行整行丢掉——键是双链
    要写的东西，没有键的行留着只会让模型照着写一个 `[[]]`。**绝不抛**：调用方（引擎结算）
    不该因为一条坏清单把整场戏的收尾炸掉。
    """
    lines: list[str] = []
    for row in entry_digest or ():
        parts = list(row) if isinstance(row, (list, tuple)) else [row]
        key = _one_line(parts[0] if parts else "")
        if not key:
            continue
        title = _one_line(parts[1]) if len(parts) > 1 else ""
        summary = _one_line(parts[2]) if len(parts) > 2 else ""
        line = f"- [[{key}]] {title or key}"
        if summary:
            line += f"{_SUMMARY_DIGEST_SEP}{summary}"
        lines.append(line)
    body = "\n".join(lines) if lines else _SUMMARY_DIGEST_EMPTY
    return f"{_SUMMARY_DIGEST_HEADER}\n{body}"


def build_summary_messages(card: CharacterCard, view_text: str, scene_text: str,
                           entry_digest: Sequence[tuple[str, str, str]] = (),
                           present: Sequence[str] = (),
                           language_directive: str = "") -> list[dict]:
    """散场总结提示词（§7.1）：让**某个角色**在散场时写一条汇总，成为他信息库里的枢纽条目。

    输入（都是普通数据，不碰信息库的任何类型——引擎拿 `SceneGain.digest()` 直接喂进来）：
    `card` 角色卡（谁在写）、`scene_text` 场景（哪一场）、`view_text` **他看到的整场对话**
    （与 think 同一来源：可见转录，不是上帝视角的全场）；`entry_digest` 他这一场记下的条目
    清单（键 + 标题 + 一行摘要）；`present` 在场者名字（可选，渲染成一行）；`language_directive`
    语言指令（可选，恒为系统消息**最后一段**，空/纯空白一字不追加）。

    要求（都写进提示词本身，改之前先读）：

      · **以这个角色自己的口吻与视角写**（他知道什么、他拿走了什么），不是上帝视角的剧情
        梗概；去 AI 味的约定与 speak/narrate 同一条线（朴素、不抒情、不写金句、不用破折号
        堆砌、无 emoji）；
      · 输出**一段简短正文**（不要 JSON——调用方用 narrate 档后端走 complete_text，§13.3
        已定复用 narrate，不新增档位）；
      · 末尾附上他本场各条目的双链标记 `[[键]]`，让它成为图的**枢纽**（§7.1）。键必须从
        清单里照抄：模型自己改写名字就会链到不存在的键上，枢纽当场断掉。清单为空时不要求
        写那一行（留一句占位说明本场没记下什么）。

    为什么要为**每个在场角色**各写一条：总结是"他"的收束，不是这场戏的旁白。五个角色就有
    五条互相不同的枢纽条目——这正是信息边界（每人只知道自己的那一份）。

    空 `view_text` 落「（暂无）」占位，与 think/speak 同约定；`scene_text` 为空落「（未指定）」。
    本函数**不碰**系统消息里的索引/回忆小节：那是 think/speak 的事，散场这一条不吃它们。
    """
    corpus = _corpus_section(getattr(card, "corpus", None))
    system = _SUMMARY_SYSTEM_TMPL.format(
        name=card.name,
        personality_json=json.dumps(card.personality, ensure_ascii=False),
        corpus="\n\n" + corpus if corpus else "",
    )
    system += _language_directive_line(language_directive)

    lines = [f"【这一场】{_one_line(scene_text) or '（未指定）'}"]
    names = [n for n in (_one_line(name) for name in present or ()) if n]
    if names:
        lines.append(f"{_SUMMARY_PRESENT_HEADER}{'、'.join(names)}")
    lines.append(f"【你看到的整场对话】\n{view_text or '（暂无）'}")
    lines.append(_summary_digest(entry_digest))
    return [{"role": "system", "content": system},
            {"role": "user", "content": "\n".join(lines) + _SUMMARY_OUTRO}]
