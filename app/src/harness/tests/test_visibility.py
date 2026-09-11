"""可见性投影：在场轴（场景名 space）+ 认知轴（knows）两条正交轴（+ 进场轴）。

对话圈已删除（§3.2）：在场轴退化为**场景本身**——view_for(messages, character, space)
只放行 in_scene == space 的消息，再按 knows 过滤。
进场轴（§3.3 可插拔角色）：since_round 给出时再丢掉 turn 早于进场基线的消息——刚进场
的角色看不到进场前的对话；缺省 None = 不过滤（既有调用方行为不变）。
"""
from harness.schemas import Message
from harness.visibility import view_for


def m(id, speaker, content, in_scene="餐厅", knows=None, address=None, reply_to=None,
      turn=0):
    return Message(id=id, speaker=speaker, content=content,
                   in_scene=in_scene, knows=knows, address=address,
                   reply_to=reply_to, turn=turn)


# 1/2/4 是甲与乙的私密对话（knows 限制）；3 是同场景里别人说的公开句。
MSG = [
    m(1, "甲", "这桌就我们俩", in_scene="餐厅", knows=["甲", "乙"]),
    m(2, "乙", "嘘，那边有耳", in_scene="餐厅", knows=["甲", "乙"]),
    m(3, "丙", "（另一桌说）", in_scene="别的场景", knows=["丙", "丁"]),
    m(4, "甲", "接上这句", in_scene="餐厅", knows=["甲", "乙"], reply_to=1),
]


def test_knows_restricts_across_characters():
    view = view_for(MSG, "乙", "餐厅")
    ids = [x.id for x in view]
    assert 2 in ids and 4 in ids


def test_knows_blocks_non_member():
    # 丙在餐厅场景内，但桌A 对话 knows 均不放行 → 一条都看不到
    view = view_for(MSG, "丙", "餐厅")
    assert view == []


def test_scene_axis_filters_by_space():
    # 甲在餐厅，只看得到本场景的句子；in_scene="别的场景" 那句被在场轴挡住
    view = view_for(MSG, "甲", "餐厅")
    ids = [x.id for x in view]
    assert 3 not in ids
    assert ids == [1, 2, 4]


def test_space_not_in_any_message_is_empty():
    view = view_for(MSG, "甲", "不存在的场景")
    assert view == []


def test_view_is_sorted_by_id():
    """乱序入参也按 id 升序出（视图/锚点都依赖「尾条 = 最高 id」）。"""
    view = view_for(list(reversed(MSG)), "甲", "餐厅")
    assert [x.id for x in view] == [1, 2, 4]


def test_reply_chain_stays_within_visible_set():
    # reply_to=1 的 4 号若能看到，说明其可见；这里验证可见集含祖先 1 号
    view = view_for(MSG, "乙", "餐厅")
    by_id = {x.id: x for x in view}
    assert by_id[4].reply_to in by_id  # 可见集内可回溯


# --------------------------------------------------- 进场轴（§3.3 since_round） --
def _turned():
    """同场景公开对话，turn 依次 0/1/2/3（turn 即「第几句台词」的转录钟）。"""
    return [m(1, "甲", "第一句", turn=0),
            m(2, "乙", "第二句", turn=1),
            m(3, "甲", "第三句", turn=2),
            m(4, "乙", "第四句", turn=3)]


def test_since_round_drops_earlier_turns():
    """进场基线 2：turn < 2 的两句（1、2 号）全被丢掉，进场后的两句留下。"""
    view = view_for(_turned(), "新人", "餐厅", since_round=2)
    assert [x.id for x in view] == [3, 4]


def test_since_round_none_keeps_everything():
    """缺省 None = 不按进场过滤（既有行为逐字节不变）。"""
    view = view_for(_turned(), "新人", "餐厅")
    assert [x.id for x in view] == [1, 2, 3, 4]
    assert view == view_for(_turned(), "新人", "餐厅", since_round=None)


def test_since_round_zero_keeps_everything():
    """0 = 有史以来全见（开场即在场的角色，或裸图默认）。"""
    assert [x.id for x in view_for(_turned(), "甲", "餐厅", since_round=0)] == \
        [1, 2, 3, 4]


def test_since_round_stacks_with_knows_and_space():
    """进场轴与另两条轴**叠加**：knows 挡住的、别的场景的，进场轴放不放都进不来。"""
    msgs = [
        m(1, "甲", "密语", knows=["甲", "乙"], turn=0),
        m(2, "甲", "公开句", turn=1),
        m(3, "丙", "别场的话", in_scene="别的场景", turn=2),
    ]
    view = view_for(msgs, "新人", "餐厅", since_round=0)
    assert [x.id for x in view] == [2], "knows 与在场轴照旧生效"
    assert view_for(msgs, "新人", "餐厅", since_round=2) == []


def test_since_round_keeps_id_order():
    """进场过滤后仍按 id 升序（视图/锚点依赖「尾条 = 最高 id」）。"""
    view = view_for(list(reversed(_turned())), "新人", "餐厅", since_round=1)
    assert [x.id for x in view] == [2, 3, 4]
