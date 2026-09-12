import json
from dataclasses import asdict
from pathlib import Path

import pytest

from harness.loaders import (
    ModelConfig, list_character_paths, list_scene_paths, load_character_card,
    load_models, load_scene, save_character_card, save_scene, scene_filename_error,
    scene_path_for, scene_with_cast,
)
from harness.schemas import CharacterCard, HardBoundary, Scene, SceneCastMember

#: app/ 根：内置场景/角色素材目录所在（tests → harness → src → app）。
APP_DIR = Path(__file__).resolve().parents[3]


def _write(tmp_path: Path, name: str, data) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def test_load_scene_validates(tmp_path):
    p = _write(tmp_path, "贝克街221B.json", {
        "name": "贝克街221B", "participants": ["福尔摩斯", "华生"],
        "hard_boundary": {"type": "time", "value": "22:00", "desc": "打烊"},
    })
    scene = load_scene(p)
    assert scene.name == "贝克街221B"
    assert scene.hard_boundary.value == "22:00"
    assert scene.participants == ["福尔摩斯", "华生"]


def test_load_scene_without_any_cast_is_legal(tmp_path):
    """§3.3：场景可以没有角色——只有名字的场景文件照常装载（空演员表）。"""
    p = _write(tmp_path, "空场.json", {"name": "空场"})
    scene = load_scene(p)
    assert scene.participants == [] and scene.characters == []


def test_load_character_card_defaults_weights(tmp_path):
    p = _write(tmp_path, "福尔摩斯.json", {"name": "福尔摩斯", "personality": {"描述": "冷静"}})
    card = load_character_card(p)
    assert card.weights.w1_relevance == 0.5
    assert card.emotion_decay_rate == 0.4


def test_load_models_yaml(tmp_path):
    p = tmp_path / "models.yaml"
    p.write_text(
        "think:\n  backend: stub\n  model: stub\n  params: {}\n"
        "speak:\n  backend: stub\n  model: stub\n  params: {}\n",
        encoding="utf-8",
    )
    cfg = load_models(p)
    assert cfg["think"] == ModelConfig(backend="stub", model="stub", params={})
    assert asdict(cfg["speak"]) == {"backend": "stub", "model": "stub", "params": {}}


# ------------------------------------------------------- 目录扫描（可插拔） -----

def test_list_character_paths_returns_shipped_files_sorted():
    paths = list_character_paths(APP_DIR / "characters")
    names = [p.name for p in paths]
    assert names == sorted(names)
    assert "福尔摩斯.json" in names and "华生.json" in names
    assert all(p.suffix == ".json" and p.is_file() for p in paths)


def test_list_scene_paths_returns_shipped_files_sorted():
    paths = list_scene_paths(APP_DIR / "scenes")
    names = [p.name for p in paths]
    assert names == sorted(names)
    assert "贝克街221B.json" in names
    assert all(p.suffix == ".json" for p in paths)


def test_list_paths_ignore_non_json_and_survive_malformed(tmp_path):
    """非 .json 一律忽略；坏 json 不能让扫描崩掉（错误留到 load 时暴露）。"""
    (tmp_path / "a.json").write_text('{"name": "a"}', encoding="utf-8")
    (tmp_path / "b.json").write_text("{ 这不是合法 json", encoding="utf-8")
    (tmp_path / "note.txt").write_text("x", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.json").write_text('{"name": "c"}', encoding="utf-8")

    paths = list_character_paths(tmp_path)
    assert [p.name for p in paths] == ["a.json", "b.json", "c.json"]
    assert paths == sorted(paths)
    assert list_scene_paths(tmp_path) == paths


def test_list_paths_missing_or_empty_dir_is_empty(tmp_path):
    assert list_character_paths(tmp_path) == []
    assert list_scene_paths(tmp_path / "不存在") == []


# --------------------------------------------------------- 保存助手（编辑器用） --

def test_save_character_card_roundtrip(tmp_path):
    """save→load 恒等；UTF-8 不转义、缩进 2——把文件当 system prompt 的人眼可读。"""
    card = CharacterCard(
        name="福尔摩斯", personality={"描述": "冷静"},
        corpus={"source": "《示例作品》", "style": "短句", "thinking": "先观察",
                "quirks": ["……"], "samples": ["今晚这桌，我请。"]},
    )
    out = tmp_path / "新建目录" / "福尔摩斯.json"
    save_character_card(card, out)
    assert out.is_file()
    assert load_character_card(out) == card

    text = out.read_text(encoding="utf-8")
    assert "今晚这桌，我请。" in text          # ensure_ascii=False
    assert '"corpus"' in text
    assert text.startswith("{\n  ")            # indent=2
    # 目录里立刻可被发现（可插拔）
    assert out in list_character_paths(out.parent)


def test_save_scene_roundtrip(tmp_path):
    scene = Scene(name="贝克街221B", participants=["福尔摩斯", "华生"])
    out = tmp_path / "贝克街221B.json"
    save_scene(scene, out)
    assert load_scene(out) == scene
    assert out.read_text(encoding="utf-8").startswith("{\n  ")


def test_save_scene_roundtrips_every_new_field(tmp_path):
    """§3.1 全部新字段 save→load 恒等（hooks 落成 dict 列表再解回来）。"""
    scene = Scene(
        name="茶室", date="2026-09-11", background="民国上海，雨季",
        description="茶室里有一些桌椅", description_mutable=True,
        plot_direction="让两人把话说开",
        hard_boundary=HardBoundary(type="none", value="", desc="无"),
        characters=[SceneCastMember(name="甲", entered_at="20:05", entered_round=2)],
        hooks=[{"id": "h1", "condition": "若甲说出真相", "event_kind": "context",
                "context_text": "真相大白", "visible": True},
               {"id": "h2", "condition": "若乙在座", "event_kind": "character",
                "character_name": "丙", "action": "mute_turns", "turns": 3}],
    )
    out = tmp_path / "自定义文件名.json"
    save_scene(scene, out)
    loaded = load_scene(out)
    assert loaded == scene
    assert loaded.hooks[1].action == "mute_turns" and loaded.hooks[1].turns == 3
    assert loaded.characters[0].entered_at == "20:05"
    text = out.read_text(encoding="utf-8")
    assert "民国上海" in text and '"hooks"' in text      # ensure_ascii=False


def test_load_scene_legacy_and_unknown_keys_do_not_crash(tmp_path):
    """旧/未知键（circles、未来字段）既不炸也不进模型；participants 迁移进 characters。"""
    p = _write(tmp_path, "旧场景.json", {
        "name": "贝克街221B", "participants": ["甲", "乙"],
        "circles": [{"id": "桌A", "members": ["甲", "乙"]}],
        "hard_boundary": {"type": "time", "value": "22:00", "desc": "打烊"},
        "未来的字段": {"随便": "什么都行"},
    })
    scene = load_scene(p)
    assert scene.participants == ["甲", "乙"]
    assert scene.hard_boundary.value == "22:00"
    assert "circles" not in scene.model_dump()


# ---------------------------------------------------------------- 文件名校验 --
def _filename_cases() -> list[tuple[str, bool]]:
    """(文件名, 是否合法)——拒绝表与 gui/library.py::filename_error 同一套规则。"""
    return [
        ("晚宴.json", True), ("贝克街221B 2.json", True), ("a.b.json", True),
        ("", False), ("   ", False), (" 晚宴.json", False), ("晚宴.json ", False),
        (".hidden.json", False), ("..json", False), ("a..b.json", False),
        ("dir/晚宴.json", False), ("dir\\晚宴.json", False),
        ("晚:宴.json", False), ("晚*宴.json", False), ('晚"宴.json', False),
        ("CON.json", False), ("com1.json", False), ("LPT9.json", False),
        ("晚宴.", False), ("晚宴", False), ("晚宴.JSON", True),
    ]


def test_scene_filename_error_matches_the_rejection_table():
    for name, ok in _filename_cases():
        err = scene_filename_error(name)
        assert (err == "") is ok, f"{name!r} → {err!r}"
        if not ok:
            assert err.startswith("文件名"), "错误文案须是可读的中文句子"


def test_scene_filename_error_rejects_control_chars():
    assert scene_filename_error("晚\x00宴.json") != ""


def test_scene_path_for_resolves_valid_name_under_dir(tmp_path):
    assert scene_path_for(tmp_path, "晚宴.json") == tmp_path / "晚宴.json"


def test_scene_path_for_rejects_unsafe_names(tmp_path):
    for name, ok in _filename_cases():
        if ok:
            continue
        with pytest.raises(ValueError):
            scene_path_for(tmp_path, name)
    # 越界的相对路径绝不允许写成「库目录之外」的文件
    with pytest.raises(ValueError):
        scene_path_for(tmp_path, "../逃逸.json")


# ------------------------------------- 以所选角色为准的演员表（cast_from_cards 纯变换） --
def test_scene_with_cast_overrides_characters_in_given_order():
    """演员表 = 给定顺序（用户选卡顺序），写进 characters、入场记录从零起算。"""
    out = scene_with_cast(Scene(name="贝克街221B", participants=["甲"]), ["丙", "甲", "乙"])
    assert out.participants == ["丙", "甲", "乙"]
    assert [(m.name, m.entered_round, m.entered_at) for m in out.characters] == [
        ("丙", 0, ""), ("甲", 0, ""), ("乙", 0, "")]


def test_scene_with_cast_fills_cast_into_empty_scene():
    """空场景（§3.3 先建空场景）也能被选中角色填满。"""
    out = scene_with_cast(Scene(name="空地"), ["甲", "乙"])
    assert out.participants == ["甲", "乙"]
    assert len(out.characters) == 2


def test_scene_with_cast_leaves_original_scene_alone():
    """返回的是副本：原 Scene 的演员表/入场记录一字不改。"""
    scene = Scene(name="贝克街221B",
                  characters=[SceneCastMember(name="甲", entered_at="21:35",
                                              entered_round=3)])
    out = scene_with_cast(scene, ["丙", "甲"])
    assert out.participants == ["丙", "甲"]
    assert scene.participants == ["甲"], "原场景不得被就地改写"
    assert scene.characters[0].entered_at == "21:35"
    assert out.hard_boundary == scene.hard_boundary
    assert out.name == scene.name


def test_shipped_cards_carry_usable_corpus():
    """内置演示卡自带语料：落盘合法、可被扫描发现、且真的渲染进 think/speak 提示。"""
    from harness.prompters import build_speak_messages, build_think_messages

    # 只查**内置演示卡**：用户往 characters/ 里自建的卡（语料可空）不该让测试套件变红。
    shipped = [APP_DIR / "characters" / n for n in ("福尔摩斯.json", "华生.json")]
    paths = list_character_paths(APP_DIR / "characters")
    assert paths, "内置角色目录不该为空"
    for path in shipped:
        assert path in paths, f"内置演示卡未被扫描发现: {path.name}"
        card = load_character_card(path)
        assert card.corpus.style and card.corpus.thinking
        assert len(card.corpus.samples) >= 3   # 够当 few-shot 风格锚点
        assert card.corpus.quirks
        for msgs in (build_think_messages(card, view_text="[1] 华生: 你好",
                                          last_chunk_text="你好", scene_text="贝克街221B"),
                     build_speak_messages(card, view_text="[1] 华生: 你好",
                                          scene_text="贝克街221B")):
            system = msgs[0]["content"]
            assert "【语言风格】" in system and "绝不照抄样例内容或原句" in system
            assert f"【人物语料·出处】{card.corpus.source}" in system
