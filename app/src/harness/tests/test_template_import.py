"""模板导入器（harness.template_import + harness.tools.import_cards）：离屏、确定性、不联网。

测试自己**把 templates/ 下真实模板按「大模型填好的样子」填一遍**（多行值、粗体字段名、
半角冒号、`（素材未提及）`、`70%`、`0,7`、`- （无）` 都覆盖），再断言解析出的每个字段；
另外锁住「原样未填 → 全默认 + empty_fields 列全」「原样未填 → 依然解析成功」，
以及一张错误表（权重乱填 / 时间不是 HH:MM / 缺姓名 / 两种模板字段混在一起 / hook id 非法 /
未知场景补丁键只警告 / 出场角色去重）。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from harness import template_import as ti
from harness.loaders import load_character_card, load_scene
from harness.schemas import Corpus, HardBoundary, Weights
from harness.tools import import_cards

#: 仓库根（tests → harness → src → app → 仓库）。
REPO_ROOT = Path(__file__).resolve().parents[4]
TEMPLATES = REPO_ROOT / "templates"
CHAR_TPL = TEMPLATES / "角色卡模板.md"
SCENE_TPL = TEMPLATES / "场景卡模板.md"

#: 测试自己的「字段名 + 冒号」行识别（**故意不复用模块里的正则**：白盒复用会让
#: 「解析器认不出但测试照样过」这种事故溜过去）。
_LABEL_RE = re.compile(r"^(?P<head>.*?【(?P<label>[^】]+)】\s*[：:]\s*)(?P<rest>.*)$")


# ------------------------------------------------------------------ 工具
def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _filled(template: str, values: dict[str, str]) -> str:
    """按「大模型填好的样子」把模板里的提示换成真值。

    值里的换行 = 多行字段：第一行接在冒号后面，其余行紧随其后（与人工填写一致）。
    """
    out: list[str] = []
    for line in template.splitlines():
        m = _LABEL_RE.match(line)
        if m and m.group("label") in values:
            head, *rest = values[m.group("label")].split("\n")
            out.append(m.group("head") + head)
            out.extend(rest)
        else:
            out.append(line)
    return "\n".join(out)


_CHAR_VALUES: dict[str, str] = {
    "姓名": "甲",
    "出处/作品": "（素材未提及）",
    "一句话定位": "冷静克制、习惯先观察再开口的旧友",
    "性格描述": "话少，先观察再开口。\n压力下不急不辩，用沉默拖住。\n在意的是别把人拖下水。",
    "能力": "\n- （无）",
    "关系": "\n- 乙：多年同窗，最近在冷战\n- 丙：旧部，欠他一条命",
    "已知边界": "\n- 知道：那封信的内容\n- 不知道：信是谁写的",
    "语言风格": "句子短，爱用反问。",
    "思维方式": "先看人，再看事；决定前先算代价。",
    "口头禅": "\n- 你说了算。\n- 再说吧。",
    "台词样例": "\n- 「不久。」\n- 「看了。」\n- 「你觉得呢。」",
    "相关权重 w1": "0.6",
    "唤醒权重 w2": "70%",
    "邻接权重 w3": "0.8",
    "目标权重 w4": "0.4",
    "话痨权重 w5": "0,7",
    "抑制权重 w6": "0.9",
    "场景权重 w7": "0.5（公开场合压得住）",
    "情绪衰减": "0.2",
}


def _char_md(**over: str) -> str:
    """真实角色卡模板 + 真值（外加粗体字段名与半角冒号各一处）。"""
    values = dict(_CHAR_VALUES)
    values.update(over or {})
    md = _filled(_read(CHAR_TPL), values)
    md = md.replace("【姓名】：甲", "**【姓名】**：甲")     # 粗体字段名
    md = md.replace("【出处/作品】：", "【出处/作品】: ")            # 半角冒号
    return md


_SCENE_VALUES: dict[str, str] = {
    "场景名": "深夜小馆",
    "日期": "2024-09-11",
    "开始时间": "23:00",
    "边界类型": "时间",
    "边界值": "23:40",
    "边界描述": "小馆打烊",
    "背景信息": "夜里十一点，小馆只剩他们这一桌。\n外面还在下雨，路灯把水痕照得发白。",
    "场景描述": "小馆里只剩他们这一桌，灯关了一半。",
    "描述可改变": "是",
    "剧情设定": "围绕那封信推进。",
    "出场角色": "\n- 沈砚\n- 苏晚\n- 沈砚",
    "hook 1 条件": "有人提起那封信",
    "hook 1 事件类型": "上下文",
    "hook 1 事件内容": "那封信的事被重新摆到桌面上。",
    "hook 1 显隐": "显式",
    "hook 2 条件": "沈砚和苏晚开始互相追问",
    "hook 2 事件类型": "角色",
    "hook 2 事件内容": "沈砚：离开",
    "hook 2 显隐": "隐式",
}

#: 模板末尾那句「需要更多就继续复制」——测试照它再加一条**场景事件** hook。
_HOOK3 = ("\n\n【hook 3 条件】：虚拟钟走到 23:35 之后\n\n"
          "【hook 3 事件类型】：场景\n\n"
          "【hook 3 事件内容】：description：桌椅全被打翻\n\n"
          "【hook 3 显隐】：显式\n")


def _scene_md(extra_hook: bool = True, **over: str) -> str:
    values = dict(_SCENE_VALUES)
    values.update(over or {})
    md = _filled(_read(SCENE_TPL), values)
    if extra_hook:
        md += _HOOK3
    return md


def _import(md: str, tmp_path: Path, *, filename: str | None = None,
            dry_run: bool = True, source_name: str = "填好的.md") -> ti.ImportResult:
    """把 md 写成文件后走**公开 API**导入（dry_run 缺省：只要解析结果，不写盘）。"""
    src = tmp_path / source_name
    src.write_text(md, encoding="utf-8")
    return ti.import_template_file(
        src, characters_dir=tmp_path / "characters", scenes_dir=tmp_path / "scenes",
        filename=filename, dry_run=dry_run)


# ================================================================== 角色卡：填满
def test_character_template_filled_like_an_llm(tmp_path):
    res = _import(_char_md(), tmp_path)
    card = res.card_or_scene
    assert res.kind == "character"
    assert res.name == "甲"

    assert card.name == "甲"
    assert card.corpus.source == ""                    # （素材未提及）
    assert card.personality == {
        "一句话定位": "冷静克制、习惯先观察再开口的旧友",
        "性格描述": "话少，先观察再开口。\n压力下不急不辩，用沉默拖住。\n在意的是别把人拖下水。",
    }
    assert card.abilities == []                        # - （无）
    assert card.relationships == {"乙": "多年同窗，最近在冷战",
                                  "丙": "旧部，欠他一条命"}
    assert card.knowledge_boundary == ["知道：那封信的内容", "不知道：信是谁写的"]
    assert card.corpus.style == "句子短，爱用反问。"
    assert card.corpus.thinking == "先看人，再看事；决定前先算代价。"
    assert card.corpus.quirks == ["你说了算。", "再说吧。"]
    assert card.corpus.samples == ["「不久。」", "「看了。」", "「你觉得呢。」"]

    assert card.weights == Weights(
        w1_relevance=0.6, w2_arousal=0.7, w3_adjacency=0.8, w4_goal_pressure=0.4,
        w5_talkativeness=0.7, w6_inhibition=0.9, w7_scene_pressure=0.5)
    assert card.emotion_decay_rate == 0.2

    assert "出处/作品" in res.empty_fields
    assert "能力" in res.empty_fields
    assert "口头禅" not in res.empty_fields
    assert "唤醒权重 w2" not in res.empty_fields


def test_parse_character_template_public_api_returns_card():
    card = ti.parse_character_template(_char_md())
    assert card.name == "甲" and card.weights.w5_talkativeness == 0.7


def test_character_template_multiline_value_keeps_paragph_lines():
    """多行字段：整段接在字段下面（大模型的常见写法），行间以换行相连。"""
    md = "## 二、性格与能力\n\n【姓名】：庚\n\n【性格描述】：\n第一句。\n\n第二句。\n\n" \
         "【能力】：\n- 记路\n"
    card = ti.parse_character_template(md)
    assert card.personality["性格描述"] == "第一句。\n第二句。"
    assert card.abilities == ["记路"]


def test_character_template_slash_separated_single_line_is_a_list():
    card = ti.parse_character_template(
        "【姓名】：庚\n\n【能力】：过目不忘、一手好字\n")
    assert card.abilities == ["过目不忘", "一手好字"]


# ================================================================== 角色卡：原样未填
def test_unfilled_character_template_defaults_every_field(tmp_path):
    """只填了姓名、其余原样（全是提示）→ 全默认，且 empty_fields 逐个列出。"""
    md = _filled(_read(CHAR_TPL), {"姓名": "无名氏"})
    res = _import(md, tmp_path)
    card = res.card_or_scene
    assert card.name == "无名氏"
    assert card.personality == {}
    assert card.abilities == []
    assert card.relationships == {} and card.knowledge_boundary == []
    assert card.corpus == Corpus()
    assert card.weights == Weights()
    assert card.emotion_decay_rate == 0.4
    assert res.empty_fields == [
        "出处/作品", "一句话定位", "性格描述", "能力", "关系", "已知边界",
        "语言风格", "思维方式", "口头禅", "台词样例",
        "相关权重 w1", "唤醒权重 w2", "邻接权重 w3", "目标权重 w4",
        "话痨权重 w5", "抑制权重 w6", "场景权重 w7", "情绪衰减"]
    assert res.warnings == []


# ================================================================== 场景卡：填满
def test_scene_template_filled_like_an_llm(tmp_path):
    res = _import(_scene_md(), tmp_path)
    scene = res.card_or_scene
    assert res.kind == "scene" and res.name == "深夜小馆"

    assert scene.name == "深夜小馆"
    assert scene.date == "2024-09-11"
    assert scene.start_time == "23:00"
    assert scene.hard_boundary == HardBoundary(type="time", value="23:40",
                                               desc="小馆打烊")
    assert scene.background == "夜里十一点，小馆只剩他们这一桌。\n外面还在下雨，路灯把水痕照得发白。"
    assert scene.description == "小馆里只剩他们这一桌，灯关了一半。"
    assert scene.description_mutable is True
    assert scene.plot_direction == "围绕那封信推进。"
    assert [(m.name, m.entered_at, m.entered_round) for m in scene.characters] == [
        ("沈砚", "", 0), ("苏晚", "", 0)]                 # 重名去重、保序
    assert scene.participants == ["沈砚", "苏晚"]

    assert [h.id for h in scene.hooks] == ["h1", "h2", "h3"]
    h1, h2, h3 = scene.hooks
    assert (h1.condition, h1.event_kind, h1.visible, h1.context_text) == (
        "有人提起那封信", "context", True, "那封信的事被重新摆到桌面上。")
    assert (h2.event_kind, h2.character_name, h2.action, h2.turns, h2.visible) == (
        "character", "沈砚", "remove", 0, False)
    assert (h3.event_kind, h3.scene_patch, h3.visible) == (
        "scene", {"description": "桌椅全被打翻"}, True)
    assert res.empty_fields == []


def test_unfilled_scene_template_defaults_every_field(tmp_path):
    md = _filled(_read(SCENE_TPL), {"场景名": "空场"})
    res = _import(md, tmp_path)
    scene = res.card_or_scene
    assert scene.name == "空场"
    assert scene.date == "" and scene.background == "" and scene.description == ""
    assert scene.plot_direction == "" and scene.description_mutable is False
    assert scene.characters == [] and scene.hooks == []
    assert scene.hard_boundary is None
    assert scene.start_time == "21:30"
    assert res.empty_fields == [
        "日期", "开始时间", "边界类型", "背景信息", "场景描述", "描述可改变",
        "剧情设定", "出场角色", "触发器 hooks"]
    assert res.warnings == []


@pytest.mark.parametrize("raw", ["无", "none", "否"])
def test_scene_boundary_without_boundary_clears_value_and_desc(raw):
    """「无」这一族（含英文 none 与口语「否」）→ type="none"，值与描述一并清空。"""
    scene = ti.parse_scene_template(_scene_md(
        extra_hook=False, **{"边界类型": raw, "边界值": "23:40",
                             "边界描述": "小馆打烊"}))
    assert scene.hard_boundary == HardBoundary(type="none", value="", desc="")


def test_scene_boundary_non_time_keeps_free_text_value():
    md = _scene_md(extra_hook=False, **{
        "边界类型": "物理", "边界值": "雨停", "边界描述": "雨停了"})
    scene = ti.parse_scene_template(md)
    assert scene.hard_boundary == HardBoundary(type="physical", value="雨停",
                                               desc="雨停了")


@pytest.mark.parametrize("raw,normalized", [("21:30", "21:30"), ("9:5", "09:05")])
def test_scene_start_time_is_normalized_to_hhmm(raw, normalized):
    scene = ti.parse_scene_template(
        _scene_md(extra_hook=False, **{"开始时间": raw}))
    assert scene.start_time == normalized


def test_scene_mutable_false_and_english_true():
    assert ti.parse_scene_template(_scene_md(
        extra_hook=False, **{"描述可改变": "不可改变"})).description_mutable is False
    assert ti.parse_scene_template(_scene_md(
        extra_hook=False, **{"描述可改变": "true"})).description_mutable is True


def test_hooks_tolerate_missing_numbers_and_gaps():
    """没编号 / 断号（1、3）都认：id 一律按出现顺序 h1、h2…"""
    md = _scene_md(extra_hook=False, **{
        "hook 1 条件": "他推门进来", "hook 1 事件类型": "上下文",
        "hook 1 事件内容": "门被推开了。", "hook 1 显隐": "显式",
        "hook 2 条件": "他坐下", "hook 2 事件类型": "上下文",
        "hook 2 事件内容": "他坐下了。", "hook 2 显隐": "显式"})
    md += ("\n\n【hook 条件】：灯灭了\n\n【hook 事件类型】：上下文\n\n"
           "【hook 事件内容】：灯灭了。\n\n【hook 显隐】：隐式\n")
    scene = ti.parse_scene_template(md)
    assert [(h.id, h.visible) for h in scene.hooks] == [
        ("h1", True), ("h2", True), ("h3", False)]


def test_character_event_accepts_chinese_and_english_actions():
    md = _scene_md(extra_hook=False, **{
        "hook 1 条件": "吵起来", "hook 1 事件类型": "角色",
        "hook 1 事件内容": "苏晚：禁言2回合", "hook 1 显隐": "隐式",
        "hook 2 条件": "气消了", "hook 2 事件类型": "character",
        "hook 2 事件内容": "苏晚：unmute", "hook 2 显隐": "显式"})
    h1, h2 = ti.parse_scene_template(md).hooks
    assert (h1.character_name, h1.action, h1.turns) == ("苏晚", "mute_turns", 2)
    assert (h2.character_name, h2.action, h2.turns) == ("苏晚", "unmute", 0)


def test_trailing_commentary_line_does_not_break_scalars(tmp_path):
    """标量字段下面多写一句无括号的说明（大模型爱这么收尾）不该毁掉权重/是-否/时间。"""
    card = ti.parse_character_template(_char_md(
        **{"情绪衰减": "0.5\n说明：按模板建议中值填的"}))
    assert card.emotion_decay_rate == 0.5
    scene = ti.parse_scene_template(_scene_md(
        extra_hook=False, **{"开始时间": "21:30\n说明：素材没写明，按情境给一个合理值",
                             "描述可改变": "是\n说明：剧情推进时可以改写"}))
    assert scene.start_time == "21:30"
    assert scene.description_mutable is True


# ================================================================== 错误表
def test_weight_garbage_names_field_and_offending_text():
    with pytest.raises(ti.TemplateError) as ei:
        ti.parse_character_template(_char_md(**{"唤醒权重 w2": "很冲动"}))
    assert "唤醒" in ei.value.field
    assert "很冲动" in ei.value.message


def test_weight_out_of_range_is_rejected():
    with pytest.raises(ti.TemplateError) as ei:
        ti.parse_character_template(_char_md(**{"情绪衰减": "-0.3"}))
    assert "情绪衰减" in ei.value.field and "-0.3" in ei.value.message


def test_comma_without_integer_part_is_rejected():
    """`,7` 不是合法的逗号小数（少了整数部分）——按错误处理，不悄悄当成 0.7。"""
    with pytest.raises(ti.TemplateError) as ei:
        ti.parse_character_template(_char_md(**{"相关权重 w1": ",7"}))
    assert "相关权重 w1" in ei.value.field


def test_time_boundary_must_be_hhmm():
    with pytest.raises(ti.TemplateError) as ei:
        ti.parse_scene_template(_scene_md(extra_hook=False, **{
            "边界类型": "时间", "边界值": "晚上"}))
    assert ei.value.field == "边界值" and "晚上" in ei.value.message
    assert ei.value.line, "错误应带行号，CLI 才能说『第 N 行附近』"


def test_unknown_boundary_type_is_rejected():
    with pytest.raises(ti.TemplateError) as ei:
        ti.parse_scene_template(_scene_md(extra_hook=False, **{"边界类型": "看情况"}))
    assert ei.value.field == "边界类型" and "看情况" in ei.value.message


def test_bad_start_time_is_rejected():
    with pytest.raises(ti.TemplateError) as ei:
        ti.parse_scene_template(_scene_md(extra_hook=False, **{"开始时间": "晚上"}))
    assert ei.value.field == "开始时间"


def test_missing_name_is_rejected():
    with pytest.raises(ti.TemplateError) as ei:
        ti.parse_character_template("## 二\n\n【性格描述】：话少。\n")
    assert ei.value.field == "姓名"


def test_missing_scene_name_is_rejected():
    with pytest.raises(ti.TemplateError) as ei:
        ti.parse_scene_template("## 一\n\n【边界类型】：无\n")
    assert ei.value.field == "场景名"


def test_fallback_name_used_when_name_missing():
    md = "## 二\n\n【性格描述】：话少。\n"
    assert ti.parse_character_template(md, fallback_name="补名").name == "补名"
    assert ti.parse_scene_template("## 一\n\n【边界类型】：无\n",
                                   fallback_name="补场").name == "补场"


def test_ambiguous_template_is_rejected():
    md = _read(CHAR_TPL) + "\n## 附\n\n【场景名】：某地\n"
    with pytest.raises(ti.TemplateError) as ei:
        ti.detect_kind(md)
    assert "角色卡" in ei.value.message and "场景卡" in ei.value.message


def test_unrecognisable_template_is_rejected():
    with pytest.raises(ti.TemplateError) as ei:
        ti.detect_kind("# 随便写写\n\n没有字段。\n")
    assert "认不出" in ei.value.message


@pytest.mark.parametrize("tpl,kind", [(CHAR_TPL, "character"), (SCENE_TPL, "scene")])
def test_detect_kind_on_shipped_templates(tpl, kind):
    assert ti.detect_kind(_read(tpl)) == kind


def test_hook_with_invalid_id_is_rejected():
    md = _scene_md(**{
        "hook 1 条件": "他站起来", "hook 1 事件类型": "上下文",
        "hook 1 事件内容": "他站起来走了。", "hook 1 显隐": "显式"}).replace(
        "【hook 1 条件】", "【hook 开门 条件】").replace(
        "【hook 1 事件类型】", "【hook 开门 事件类型】").replace(
        "【hook 1 事件内容】", "【hook 开门 事件内容】").replace(
        "【hook 1 显隐】", "【hook 开门 显隐】")
    with pytest.raises(ti.TemplateError) as ei:
        ti.parse_scene_template(md)
    assert "开门" in ei.value.message and "非法字符" in ei.value.message
    assert ei.value.field.startswith("hook")


def test_hook_with_unparseable_character_action_is_rejected():
    md = _scene_md(extra_hook=False, **{
        "hook 1 条件": "他烦了", "hook 1 事件类型": "角色",
        "hook 1 事件内容": "沈砚 转身就走", "hook 1 显隐": "显式"})
    with pytest.raises(ti.TemplateError) as ei:
        ti.parse_scene_template(md)
    assert ei.value.field.endswith("事件内容") and "角色名：动作" in ei.value.message


def test_hook_missing_event_kind_is_rejected():
    md = _scene_md(extra_hook=False, **{
        "hook 1 条件": "他来了", "hook 1 事件类型": "", "hook 1 事件内容": "他来了。"})
    with pytest.raises(ti.TemplateError) as ei:
        ti.parse_scene_template(md)
    assert "事件类型" in ei.value.field


def test_unknown_scene_patch_key_only_warns(tmp_path):
    md = _scene_md(extra_hook=False, **{
        "hook 1 条件": "灯灭了", "hook 1 事件类型": "场景",
        "hook 1 事件内容": "description：桌椅全被打翻\ncolor：红", "hook 1 显隐": "显式"})
    res = _import(md, tmp_path)
    hook = res.card_or_scene.hooks[0]
    assert hook.scene_patch == {"description": "桌椅全被打翻"}
    assert any("color" in w for w in res.warnings)
    assert any("description" in w for w in res.warnings)     # 提示可用键


def test_unparseable_mutable_only_warns_with_false(tmp_path):
    res = _import(_scene_md(extra_hook=False, **{"描述可改变": "大概吧"}), tmp_path)
    assert res.card_or_scene.description_mutable is False
    assert any("描述可改变" in w for w in res.warnings)


def test_duplicate_cast_names_are_deduped_in_order():
    scene = ti.parse_scene_template(_scene_md(
        extra_hook=False, **{"出场角色": "\n- 沈砚\n- 苏晚\n- 沈砚\n- 苏晚\n- 丙"}))
    assert scene.participants == ["沈砚", "苏晚", "丙"]


def test_cast_placeholder_is_skipped():
    scene = ti.parse_scene_template(_scene_md(
        extra_hook=False, **{"出场角色": "\n- （无）"}))
    assert scene.characters == []


@pytest.mark.parametrize("name,kind,obj_name", [
    ("_filled_示例_沈砚.md", "character", "沈砚"),
    ("_filled_示例_小馆.md", "scene", "深夜小馆")])
def test_shipped_llm_filled_examples_import(tmp_path, name, kind, obj_name):
    """仓库里那两份「真模型填写样例」必须一路导入成功（模板不改就不该解析失败）。"""
    src = TEMPLATES / name
    if not src.exists():
        pytest.skip(f"样例文件不在了：{src}")
    res = ti.import_template_file(src, characters_dir=tmp_path / "characters",
                                  scenes_dir=tmp_path / "scenes")
    assert (res.kind, res.name) == (kind, obj_name)
    assert res.path is not None and res.path.exists()
    assert res.warnings == []


# ================================================================== 落盘
def test_import_writes_character_card_readable_by_loaders(tmp_path):
    res = _import(_char_md(), tmp_path, dry_run=False, source_name="沈砚.md")
    assert res.path == tmp_path / "characters" / "甲.json"
    assert res.path.exists()
    assert load_character_card(res.path) == res.card_or_scene
    assert json.loads(res.path.read_text(encoding="utf-8"))["name"] == "甲"


def test_import_writes_scene_readable_by_loaders(tmp_path):
    res = _import(_scene_md(), tmp_path, dry_run=False)
    assert res.path == tmp_path / "scenes" / "深夜小馆.json"
    assert load_scene(res.path) == res.card_or_scene


def test_import_honours_explicit_filename(tmp_path):
    res = _import(_scene_md(), tmp_path, filename="小馆-夜.json", dry_run=False)
    assert res.path == tmp_path / "scenes" / "小馆-夜.json"
    assert load_scene(res.path).name == "深夜小馆"


def test_import_refuses_to_overwrite_without_flag(tmp_path):
    res = _import(_scene_md(), tmp_path, dry_run=False)
    res.path.write_text("{}", encoding="utf-8")
    with pytest.raises(ti.TemplateError) as ei:
        _import(_scene_md(), tmp_path, dry_run=False)
    assert "已存在" in ei.value.message
    assert res.path.read_text(encoding="utf-8") == "{}"       # 原文件没被动过


def test_import_overwrites_with_flag(tmp_path):
    _import(_scene_md(), tmp_path, dry_run=False)
    md2 = _scene_md(**{"剧情设定": "换个走向。"})
    src = tmp_path / "改过.md"
    src.write_text(md2, encoding="utf-8")
    res = ti.import_template_file(src, characters_dir=tmp_path / "characters",
                                  scenes_dir=tmp_path / "scenes", overwrite=True)
    assert load_scene(res.path).plot_direction == "换个走向。"


def test_dry_run_writes_nothing(tmp_path):
    res = _import(_char_md(), tmp_path, dry_run=True)
    assert res.path is None
    assert not (tmp_path / "characters").exists()
    assert res.card_or_scene.name == "甲"


@pytest.mark.parametrize("bad", ["../坏.json", "坏.txt", "a/b.json", "..\\坏.json"])
def test_import_rejects_unsafe_scene_filename(tmp_path, bad):
    with pytest.raises(ti.TemplateError) as ei:
        _import(_scene_md(), tmp_path, filename=bad, dry_run=False)
    assert ei.value.field == "文件名"
    assert not (tmp_path / "scenes").exists()


def test_import_rejects_unsafe_character_name(tmp_path):
    with pytest.raises(ti.TemplateError) as ei:
        _import(_char_md(**{"姓名": "../坏"}), tmp_path, dry_run=False)
    assert ei.value.field == "姓名"


def test_import_missing_file_is_a_template_error(tmp_path):
    with pytest.raises(ti.TemplateError) as ei:
        ti.import_template_file(tmp_path / "没有这个.md",
                                characters_dir=tmp_path / "characters",
                                scenes_dir=tmp_path / "scenes")
    assert "读不出" in ei.value.message


def test_scene_filename_comes_from_safe_slug():
    assert ti.safe_filename("深夜/小馆: 二").find("/") == -1
    assert ti.safe_filename("") == "场景"
    assert ti.safe_filename("CON") == "CON_"


# ================================================================== CLI
def _cli_dirs(tmp_path: Path) -> list[str]:
    return ["--characters-dir", str(tmp_path / "characters"),
            "--scenes-dir", str(tmp_path / "scenes")]


def test_cli_default_dirs_are_the_app_material_dirs():
    app_dir = Path(import_cards.__file__).resolve().parents[3]
    assert import_cards.DEFAULT_CHARACTERS_DIR == app_dir / "characters"
    assert import_cards.DEFAULT_SCENES_DIR == app_dir / "scenes"


def test_cli_imports_and_reports(tmp_path, capsys):
    src = tmp_path / "沈砚.md"
    src.write_text(_char_md(), encoding="utf-8")
    code = import_cards.main([str(src), *_cli_dirs(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "甲" in out
    assert "以下字段模板里没填，已用默认值" in out and "出处/作品" in out
    assert (tmp_path / "characters" / "甲.json").exists()


def test_cli_dry_run_reports_and_writes_nothing(tmp_path, capsys):
    src = tmp_path / "沈砚.md"
    src.write_text(_char_md(), encoding="utf-8")
    code = import_cards.main([str(src), "--dry-run", *_cli_dirs(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "（试运行，未写盘）" in out
    assert not (tmp_path / "characters").exists()


def test_cli_reports_field_and_line_then_exits_nonzero(tmp_path, capsys):
    src = tmp_path / "坏卡.md"
    src.write_text(_char_md(**{"唤醒权重 w2": "很冲动"}), encoding="utf-8")
    code = import_cards.main([str(src), *_cli_dirs(tmp_path)])
    out = capsys.readouterr().out
    assert code == 1
    assert str(src) in out
    assert "第 " in out and "行附近" in out
    assert "字段【唤醒权重 w2】" in out and "很冲动" in out
    assert not (tmp_path / "characters").exists()


def test_cli_keeps_going_after_a_bad_file(tmp_path, capsys):
    good = tmp_path / "好卡.md"
    good.write_text(_char_md(), encoding="utf-8")
    bad = tmp_path / "坏卡.md"
    bad.write_text("## 一\n\n【场景名】：某地\n\n【边界类型】：看情况\n",
                   encoding="utf-8")
    code = import_cards.main([str(bad), str(good), *_cli_dirs(tmp_path)])
    out = capsys.readouterr().out
    assert code == 1
    assert "看情况" in out and "甲" in out          # 坏的那份报错、好的那份照导
    assert (tmp_path / "characters" / "甲.json").exists()


def test_cli_scene_with_name_and_force(tmp_path, capsys):
    src = tmp_path / "小馆.md"
    src.write_text(_scene_md(), encoding="utf-8")
    args = [str(src), "--name", "小馆.json", *_cli_dirs(tmp_path)]
    assert import_cards.main(args) == 0
    assert (tmp_path / "scenes" / "小馆.json").exists()
    assert import_cards.main(args) == 1                 # 已存在 → 拒绝
    assert "已存在" in capsys.readouterr().out
    assert import_cards.main([*args, "--force"]) == 0   # --force 覆盖
    assert load_scene(tmp_path / "scenes" / "小馆.json").name == "深夜小馆"


# ================================================================== 界面按钮（可选依赖）
def test_library_dialog_imports_from_template(tmp_path, monkeypatch):
    """「从模板导入…」按钮：选 .md → 落盘 → 刷新场景列 + 弹结果（含未填字段）。

    场景库只列**场景**（§3.3），故这里导场景模板；角色模板仍能导入（落到角色目录），
    只是不在这张列表上露面——角色由「角色」菜单那一侧管。
    """
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from harness.gui import library as lib

    app = QApplication.instance() or QApplication([])
    characters_dir = tmp_path / "characters"
    scenes_dir = tmp_path / "scenes"
    characters_dir.mkdir()
    scenes_dir.mkdir()
    src = tmp_path / "小馆.md"
    src.write_text(_scene_md(), encoding="utf-8")

    monkeypatch.setattr(lib.QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (str(src), "")))
    shown: dict[str, str] = {}
    monkeypatch.setattr(lib, "info",
                        lambda parent, title, text: shown.update(
                            title=title, text=text))

    dialog = lib.LibraryDialog(characters_dir, scenes_dir)
    assert dialog.scene_list.count() == 0
    dialog.import_btn.click()
    assert (scenes_dir / "深夜小馆.json").exists(), "场景卡按场景名折成安全文件名落盘"
    assert dialog.scene_list.count() == 1
    assert dialog.scene_list.item(0).text() == "深夜小馆", "列表给的是场景名"
    assert "深夜小馆" in shown["text"]
    assert dialog.last_import is not None and dialog.last_import.kind == "scene"

    # 角色模板照旧导入（落到角色目录），只是场景列不该因此多出条目。
    src_card = tmp_path / "沈砚.md"
    src_card.write_text(_char_md(), encoding="utf-8")
    monkeypatch.setattr(lib.QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (str(src_card), "")))
    dialog.import_btn.click()
    assert (characters_dir / "甲.json").exists()
    assert dialog.scene_list.count() == 1, "角色导入不改变场景列表"
    assert "甲" in shown["text"]
    dialog.deleteLater()
    app.processEvents()


# ------------------------------------------------ 一条 hook 写多个角色动作（实况回归）
def _scene_md_hook(hook_body: str) -> str:
    return "\n".join([
        "# 场景卡模板",
        "【场景名】：阅览室",
        "【开始时间】：19:00",
        "【边界类型】：时间",
        "【边界值】：22:30",
        hook_body,
    ])


def test_character_hook_with_multiple_actions_expands_into_separate_hooks():
    """一条 hook 里写多个「角色名：动作」必须各自展开成一条独立 hook。

    实况来源：用户在一条 hook 里写 `己：离开；戊：离开`，旧解析器把
    `离开；戊：离开` 整个当成动作而报错——多写动作是自然写法，不该失败。
    """
    md = _scene_md_hook("\n".join([
        "【hook 1 条件】：钟声响起",
        "【hook 1 事件类型】：角色",
        "【hook 1 事件内容】：己：离开；戊：离开",
        "【hook 1 显隐】：显式",
    ]))
    scene = ti.parse_scene_template(md)
    assert [(h.character_name, h.action, h.visible) for h in scene.hooks] == [
        ("己", "remove", True), ("戊", "remove", True)]
    assert [h.id for h in scene.hooks] == ["h1", "h1-2"]
    assert {h.condition for h in scene.hooks} == {"钟声响起"}


def test_character_hook_multiple_actions_split_by_newline_and_mixed_actions():
    """换行分隔同样支持；动作可以各不相同（离开 / 禁言N回合 / 加入）。"""
    md = _scene_md_hook("\n".join([
        "【hook 1 条件】：有人提到监控",
        "【hook 1 事件类型】：角色",
        "【hook 1 事件内容】：己：离开",
        "戊：禁言 2 回合",
        "丙：加入",
        "【hook 1 显隐】：隐式",
    ]))
    scene = ti.parse_scene_template(md)
    assert [(h.character_name, h.action, h.turns, h.visible) for h in scene.hooks] == [
        ("己", "remove", 0, False),
        ("戊", "mute_turns", 2, False),
        ("丙", "add", 0, False),
    ]


def test_character_hook_bad_action_still_names_the_offending_pair():
    """真写错动作时，报错要指向出错的那一对（而不是整块内容）。"""
    md = _scene_md_hook("\n".join([
        "【hook 1 条件】：钟声响起",
        "【hook 1 事件类型】：角色",
        "【hook 1 事件内容】：己：离开；戊：飞走",
        "【hook 1 显隐】：显式",
    ]))
    with pytest.raises(ti.TemplateError) as ei:
        ti.parse_scene_template(md)
    msg = str(ei.value)
    assert "戊" in msg and "飞走" in msg and "己" not in msg
    assert "动作只认" in msg
