from pydantic import ValidationError
import pytest

from harness.schemas import (
    CharacterCard, Corpus, HardBoundary, Message, Scene, SceneCastMember, ThinkResult,
)


def test_message_knows_none_means_visible_to_all():
    m = Message(id=1, speaker="丁", content="你好", in_scene="餐厅")
    assert m.is_visible_to("戊") is True
    assert m.is_visible_to("路人") is True


def test_message_knows_restricts_visibility():
    m = Message(id=2, speaker="丁", content="秘密", in_scene="餐厅",
                knows=["丁", "戊"])
    assert m.is_visible_to("戊") is True
    assert m.is_visible_to("庚") is False


def test_scene_and_character_card_roundtrip():
    card = CharacterCard(
        name="丁", personality={"描述": "冷静"},
        relationships={"戊": "同伴"}, knowledge_seed=[],
    )
    assert card.weights.w2_arousal == 0.5  # 默认权重
    assert card.emotion_decay_rate == 0.4

    scene = Scene(name="餐厅", participants=["丁", "戊"],
                  hard_boundary=HardBoundary(type="time", value="22:00", desc="打烊"))
    assert scene.hard_boundary.value == "22:00"
    assert scene.participants == ["丁", "戊"]     # 派生属性


def test_think_result_rejects_out_of_range_urge():
    with pytest.raises(ValidationError):
        ThinkResult(aroused=0.5, urge=2.1)  # urge 越界（范围 -1~2，含 2.0）


def test_character_card_without_corpus_defaults_to_empty():
    """向后兼容：旧卡（无 corpus 键）照常装载，corpus 取空默认值。"""
    card = CharacterCard.model_validate({"name": "丁",
                                         "personality": {"描述": "冷静"}})
    assert card.corpus == Corpus()
    assert card.corpus.source == "" and card.corpus.style == ""
    assert card.corpus.thinking == ""
    assert card.corpus.quirks == [] and card.corpus.samples == []


def test_character_card_with_corpus_parses_and_dumps():
    """带语料的卡：字段逐个解析；model_dump 仅新增 corpus，旧字段形状不变。"""
    card = CharacterCard.model_validate({
        "name": "丁", "personality": {"描述": "冷静"},
        "corpus": {"source": "《示例作品》", "style": "短句、克制",
                   "thinking": "先观察再开口",
                   "quirks": ["……", "倒是"],
                   "samples": ["今晚这桌，我请。", "你倒是先说说看。"]},
    })
    assert card.corpus.source == "《示例作品》"
    assert card.corpus.quirks == ["……", "倒是"]
    assert card.corpus.samples[-1] == "你倒是先说说看。"

    dumped = card.model_dump()
    assert dumped["corpus"]["style"] == "短句、克制"
    assert dumped["corpus"]["samples"] == ["今晚这桌，我请。", "你倒是先说说看。"]
    # 旧字段一字未改（`knowledge_boundary` 已退役 → 换成 knowledge_seed，见下面那一组）
    assert set(dumped) == {"name", "personality", "abilities", "relationships",
                           "knowledge_seed", "weights", "emotion_decay_rate",
                           "corpus"}
    assert dumped["weights"]["w1_relevance"] == 0.5
    assert dumped["emotion_decay_rate"] == 0.4


# ------------------------------------- knowledge_boundary 退役 + 读时迁移（§9.1）--

def test_character_card_migrates_legacy_boundary_into_seed_in_order():
    """老卡读时迁移（§9.1 第一步）：`knowledge_boundary` 逐条搬进 `knowledge_seed`，顺序不变。

    这是本次改造的最高约束：用户卡上写的每一条边界都必须活着迁进信息库。迁移是**纯映射**
    （只搬数据不碰磁盘），播种是 `knowledgestore.seed_from_card` 在库首次创建时的动作。

    兜底句（"只知道自己经历和被告知的事"）**照搬不误**——它不是迁移该丢的，丢弃发生在
    播种那一步（那份判据只有 `knowledgestore.KNOWLEDGE_FALLBACK` 一处定义）。迁移动它，
    等于在"读一张卡"这个副作用为零的纯函数里替用户做了一次内容取舍。
    """
    card = CharacterCard.model_validate({
        "name": "丁",
        "knowledge_boundary": ["知道：那封信的内容", "不知道：信是谁写的",
                               "只知道自己经历和被告知的事"],
    })
    assert card.knowledge_seed == ["知道：那封信的内容", "不知道：信是谁写的",
                                   "只知道自己经历和被告知的事"]


def test_character_card_seed_wins_when_both_keys_present():
    """两个键都在（半迁移的卡）：以新键为准，老键淘汰——与 Scene 的 participants 同口径。"""
    card = CharacterCard.model_validate({"name": "丁",
                                         "knowledge_boundary": ["旧的"],
                                         "knowledge_seed": ["新的"]})
    assert card.knowledge_seed == ["新的"]


def test_character_card_without_either_key_gets_empty_seed():
    """两个键都没有（今天的新卡）→ 空种子：卡照常装载，"没有库"是合法状态（§9.2）。"""
    assert CharacterCard.model_validate({"name": "丁"}).knowledge_seed == []
    assert CharacterCard(name="丁").knowledge_seed == []


def test_character_card_legacy_key_is_gone_from_the_schema():
    """`knowledge_boundary` 不再是一个字段：不进 model_dump、写它也改不到卡上。

    只留一个**只读派生**属性（`Scene.participants` 那套退役写法）给仍在读它的下游用，
    免得退役一个字段就把模板导入的"未填字段"统计整段打挂。
    """
    card = CharacterCard.model_validate({"name": "丁", "knowledge_boundary": ["甲"]})
    assert "knowledge_boundary" not in card.model_dump()
    assert "knowledge_seed" in card.model_dump()
    assert card.knowledge_boundary == ["甲"], "退役字段仍可读（派生自 knowledge_seed）"


def test_character_card_migration_performs_no_io(monkeypatch):
    """校验器里**绝不做 IO**（§9.1）：读一张带老键的卡不得产生任何磁盘写入/建库。

    用陷阱证明，而不是靠"看代码没写 IO"：把一切写盘与建库入口换成"一碰就炸"，再读卡。
    pydantic 的 `model_validator` 是纯函数——一旦有人在里面建了库，"列目录 / 编辑器预览 /
    测试夹具"这些顺手的读卡动作全都会静默写出文件，排查起来是噩梦。
    """
    from pathlib import Path
    import harness.knowledgestore as store

    def _trap(*args, **kwargs):
        raise AssertionError("读一张卡不该产生任何 IO（§9.1：校验器里绝不做 IO）")

    monkeypatch.setattr(Path, "write_text", _trap)
    monkeypatch.setattr(Path, "write_bytes", _trap)
    monkeypatch.setattr(Path, "mkdir", _trap)
    monkeypatch.setattr(Path, "touch", _trap)
    monkeypatch.setattr(Path, "rename", _trap)
    monkeypatch.setattr(Path, "replace", _trap)
    monkeypatch.setattr(store, "save_library", _trap)
    monkeypatch.setattr(store, "seed_from_card", _trap)

    card = CharacterCard.model_validate({
        "name": "丁",
        "knowledge_boundary": ["知道：那封信的内容"],
    })
    assert card.knowledge_seed == ["知道：那封信的内容"]
    assert card.model_dump()["knowledge_seed"] == ["知道：那封信的内容"]


def test_scene_new_fields_default_to_empty():
    """§3.1 新字段：缺省全空、可写、不影响旧写法。"""
    scene = Scene(name="餐厅")
    assert scene.date == "" and scene.background == ""
    assert scene.description == "" and scene.description_mutable is False
    assert scene.plot_direction == "" and scene.hooks == []
    assert scene.characters == [] and scene.participants == []   # 空场景合法
    assert scene.hard_boundary is None and scene.start_time == "21:30"


def test_scene_new_fields_roundtrip_through_dump():
    """新字段（含 hooks 的 Hook dataclass）model_dump → model_validate 恒等。"""
    scene = Scene(
        name="茶室", date="2026-09-11", background="民国上海，雨季",
        description="茶室里有一些桌椅", description_mutable=True,
        plot_direction="让两人把话说开", start_time="20:00",
        hard_boundary=HardBoundary(type="none"),
        characters=[SceneCastMember(name="甲", entered_at="20:05", entered_round=2),
                    SceneCastMember(name="乙")],
        hooks=[{"id": "h1", "condition": "若甲说出真相", "event_kind": "context",
                "context_text": "真相大白", "visible": False},
               {"id": "h2", "condition": "若乙在座", "event_kind": "character",
                "character_name": "丙", "action": "remove", "turns": 0}],
    )
    assert scene.hooks[0].event_kind == "context" and scene.hooks[0].visible is False
    assert scene.hooks[1].action == "remove"
    assert scene.hard_boundary.type == "none"

    again = Scene.model_validate(scene.model_dump())
    assert again == scene
    assert again.characters[0].entered_at == "20:05"
    assert again.hooks[1].character_name == "丙"


def test_scene_legacy_participants_migrate_into_characters():
    """旧场景文件只有 participants（无 characters）→ 迁移进 characters（保序）。"""
    scene = Scene.model_validate({"name": "餐厅", "participants": ["甲", "乙", "丙"]})
    assert scene.participants == ["甲", "乙", "丙"]
    assert [(m.name, m.entered_round, m.entered_at) for m in scene.characters] == [
        ("甲", 0, ""), ("乙", 0, ""), ("丙", 0, "")]
    assert "participants" not in scene.model_dump(), "participants 不再是字段"
    # 两个键都在时以 characters 为准（新字段优先）
    both = Scene.model_validate({"name": "餐厅", "participants": ["旧"],
                                 "characters": [{"name": "新"}]})
    assert both.participants == ["新"]


def test_scene_legacy_circles_are_dropped():
    """旧文件的 circles（对话圈已删除）不参与任何语义，也不出现在 dump 里。"""
    scene = Scene.model_validate({
        "name": "餐厅", "participants": ["甲"],
        "circles": [{"id": "桌A", "members": ["甲", "乙"]}]})
    assert scene.participants == ["甲"]
    assert "circles" not in scene.model_dump()


def test_scene_characters_accept_bare_names_and_keep_order():
    """characters 写成裸字符串（手写场景 JSON）也认，顺序即演员表顺序。"""
    scene = Scene.model_validate({"name": "餐厅", "characters": ["乙", "甲"]})
    assert scene.participants == ["乙", "甲"]
    assert scene.characters[0] == SceneCastMember(name="乙")


def test_scene_hard_boundary_accepts_none_type():
    """§3.1：hard_boundary.type 新增 "none"；未知类型仍拒绝。"""
    assert Scene(name="餐厅", hard_boundary={"type": "none"}).hard_boundary.type == "none"
    with pytest.raises(ValidationError):
        Scene(name="餐厅", hard_boundary={"type": "打烊"})


def test_scene_rejects_bad_hook_dict_loudly():
    """hooks 是用户内容：结构不合法当场报 ValidationError，绝不静默丢弃。"""
    with pytest.raises(ValidationError):
        Scene.model_validate({"name": "餐厅",
                              "hooks": [{"id": "h", "condition": "若…",
                                         "event_kind": "不存在的事件"}]})
    with pytest.raises(ValidationError):
        Scene.model_validate({"name": "餐厅", "hooks": [{"id": "h"}]})  # 缺 condition


def test_corpus_defaults_are_not_shared_between_instances():
    """可变默认值必须各自独立（避免两张卡共用一个列表）。"""
    a, b = Corpus(), Corpus()
    a.quirks.append("x")
    assert b.quirks == []
