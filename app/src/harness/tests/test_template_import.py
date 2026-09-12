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
import sys
from pathlib import Path

import pytest

from harness import template_import as ti
from harness.knowledge import Entry, parse_links
from harness.knowledgestore import BASELINE_TURN, entries_dir, library_dir, load_library
from harness.loaders import load_character_card, load_scene
from harness.schemas import Corpus, HardBoundary, Weights
from harness.tools import import_cards

#: 仓库根（tests → harness → src → app → 仓库）。
REPO_ROOT = Path(__file__).resolve().parents[4]
TEMPLATES = REPO_ROOT / "templates"
CHAR_TPL = TEMPLATES / "角色卡模板.md"
SCENE_TPL = TEMPLATES / "场景卡模板.md"
LIB_TPL = TEMPLATES / "信息库模板.md"

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
    "姓名": "戊",
    "出处/作品": "（素材未提及）",
    "一句话定位": "冷静克制、习惯先观察再开口的旧友",
    "性格描述": "话少，先观察再开口。\n压力下不急不辩，用沉默拖住。\n在意的是别把人拖下水。",
    "能力": "\n- （无）",
    "关系": "\n- 庚：多年同窗，最近在冷战\n- 辛：旧部，欠他一条命",
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
    md = md.replace("【姓名】：戊", "**【姓名】**：戊")     # 粗体字段名
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
    assert res.name == "戊"

    assert card.name == "戊"
    assert card.corpus.source == ""                    # （素材未提及）
    assert card.personality == {
        "一句话定位": "冷静克制、习惯先观察再开口的旧友",
        "性格描述": "话少，先观察再开口。\n压力下不急不辩，用沉默拖住。\n在意的是别把人拖下水。",
    }
    assert card.abilities == []                        # - （无）
    assert card.relationships == {"庚": "多年同窗，最近在冷战",
                                  "辛": "旧部，欠他一条命"}
    # 模板里那一栏仍叫「已知边界」（模板文件本阶段不动），可它落到卡上的**新字段**是
    # knowledge_seed——读卡的校验器把老键逐条搬过去（§9.1 第一步）。故这里断言新名。
    assert card.knowledge_seed == ["知道：那封信的内容", "不知道：信是谁写的"]
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
    assert card.name == "戊" and card.weights.w5_talkativeness == 0.7


def test_character_template_multiline_value_keeps_paragph_lines():
    """多行字段：整段接在字段下面（大模型的常见写法），行间以换行相连。"""
    md = "## 二、性格与能力\n\n【姓名】：阿黎\n\n【性格描述】：\n第一句。\n\n第二句。\n\n" \
         "【能力】：\n- 记路\n"
    card = ti.parse_character_template(md)
    assert card.personality["性格描述"] == "第一句。\n第二句。"
    assert card.abilities == ["记路"]


def test_character_template_slash_separated_single_line_is_a_list():
    card = ti.parse_character_template(
        "【姓名】：阿黎\n\n【能力】：过目不忘、一手好字\n")
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
    assert card.relationships == {} and card.knowledge_seed == []
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
        extra_hook=False, **{"出场角色": "\n- 沈砚\n- 苏晚\n- 沈砚\n- 苏晚\n- 辛"}))
    assert scene.participants == ["沈砚", "苏晚", "辛"]


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
    assert res.path == tmp_path / "characters" / "戊.json"
    assert res.path.exists()
    assert load_character_card(res.path) == res.card_or_scene
    assert json.loads(res.path.read_text(encoding="utf-8"))["name"] == "戊"


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
    assert res.card_or_scene.name == "戊"


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


def test_cli_default_dirs_are_the_app_material_dirs(monkeypatch):
    """开发态缺省导入目标 = 仓库里的 `app/characters`、`app/scenes`——**与改造前逐字节同址**。

    这是界面的「角色库/场景库」目录（缺省卡/场景所在处）：导入的卡当场就能在库里看到。
    改造前这条就是本用例的原形（钉 `DEFAULT_CHARACTERS_DIR = _APP_DIR/"characters"`），
    打包改造不许把它改掉——开发态的行为是本次的最高约束。
    """
    from harness import paths as paths_mod

    monkeypatch.delattr(sys, "frozen", raising=False)
    app_dir = Path(import_cards.__file__).resolve().parents[3]
    assert import_cards.default_characters_dir() == app_dir / "characters"
    assert import_cards.default_scenes_dir() == app_dir / "scenes"
    assert import_cards.default_characters_dir().is_absolute()
    assert import_cards.default_characters_dir() == \
        paths_mod.materials_dir() / "characters"


def test_cli_default_dirs_move_to_user_dir_when_frozen(monkeypatch, tmp_path):
    """冻结态缺省导入目标 = **用户数据目录**（§1.2），绝不写安装目录（那儿只读、升级还会覆盖）。

    与 conftest 的沙箱夹具配合：`user_dir()` 指向的是一次性 tmp。
    """
    from harness import paths as paths_mod

    app_dir = Path(import_cards.__file__).resolve().parents[3]
    monkeypatch.setattr(paths_mod, "is_frozen", lambda: True)
    assert import_cards.default_characters_dir() == paths_mod.user_dir() / "characters"
    assert import_cards.default_scenes_dir() == paths_mod.user_dir() / "scenes"
    for resolved in (import_cards.default_characters_dir(),
                     import_cards.default_scenes_dir()):
        assert paths_mod.user_dir() in resolved.parents
        assert app_dir not in resolved.parents, "缺省绝不再指回仓库/安装目录"


def test_cli_imports_and_reports(tmp_path, capsys):
    src = tmp_path / "沈砚.md"
    src.write_text(_char_md(), encoding="utf-8")
    code = import_cards.main([str(src), *_cli_dirs(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "戊" in out
    assert "以下字段模板里没填，已用默认值" in out and "出处/作品" in out
    assert (tmp_path / "characters" / "戊.json").exists()


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
    assert "看情况" in out and "戊" in out          # 坏的那份报错、好的那份照导
    assert (tmp_path / "characters" / "戊.json").exists()


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
    assert (characters_dir / "戊.json").exists()
    assert dialog.scene_list.count() == 1, "角色导入不改变场景列表"
    assert "戊" in shown["text"]
    dialog.deleteLater()
    app.processEvents()


# ------------------------------------------------ 一条 hook 写多个角色动作（实况回归）
def _scene_md_hook(hook_body: str) -> str:
    return "\n".join([
        "# 场景卡模板",
        "【场景名】：自习室",
        "【开始时间】：19:00",
        "【边界类型】：时间",
        "【边界值】：22:30",
        hook_body,
    ])


def test_character_hook_with_multiple_actions_expands_into_separate_hooks():
    """一条 hook 里写多个「角色名：动作」必须各自展开成一条独立 hook。

    实况来源：用户在一条 hook 里写 `壬：离开；癸：离开`，旧解析器把
    `离开；癸：离开` 整个当成动作而报错——多写动作是自然写法，不该失败。
    """
    md = _scene_md_hook("\n".join([
        "【hook 1 条件】：钟声响起",
        "【hook 1 事件类型】：角色",
        "【hook 1 事件内容】：壬：离开；癸：离开",
        "【hook 1 显隐】：显式",
    ]))
    scene = ti.parse_scene_template(md)
    assert [(h.character_name, h.action, h.visible) for h in scene.hooks] == [
        ("壬", "remove", True), ("癸", "remove", True)]
    assert [h.id for h in scene.hooks] == ["h1", "h1-2"]
    assert {h.condition for h in scene.hooks} == {"钟声响起"}


def test_character_hook_multiple_actions_split_by_newline_and_mixed_actions():
    """换行分隔同样支持；动作可以各不相同（离开 / 禁言N回合 / 加入）。"""
    md = _scene_md_hook("\n".join([
        "【hook 1 条件】：有人提到监控",
        "【hook 1 事件类型】：角色",
        "【hook 1 事件内容】：壬：离开",
        "癸：禁言 2 回合",
        "辛：加入",
        "【hook 1 显隐】：隐式",
    ]))
    scene = ti.parse_scene_template(md)
    assert [(h.character_name, h.action, h.turns, h.visible) for h in scene.hooks] == [
        ("壬", "remove", 0, False),
        ("癸", "mute_turns", 2, False),
        ("辛", "add", 0, False),
    ]


def test_character_hook_bad_action_still_names_the_offending_pair():
    """真写错动作时，报错要指向出错的那一对（而不是整块内容）。"""
    md = _scene_md_hook("\n".join([
        "【hook 1 条件】：钟声响起",
        "【hook 1 事件类型】：角色",
        "【hook 1 事件内容】：壬：离开；癸：飞走",
        "【hook 1 显隐】：显式",
    ]))
    with pytest.raises(ti.TemplateError) as ei:
        ti.parse_scene_template(md)
    msg = str(ei.value)
    assert "癸" in msg and "飞走" in msg and "壬" not in msg
    assert "动作只认" in msg


# ================================================= 信息库模板（§3 数据模型 / §4 落盘布局）
#: 库信息四件事：库名（必填）、类型（必填）、归属角色（角色库必填）、订阅（可选）。
_LIB_META: dict[str, str] = {
    "库名": "庆国世界观", "类型": "广域库", "归属角色": "", "订阅": ""}
#: 条目字段体例：`【条目 N 名称】/【条目 N 摘要】/【条目 N 正文】/【条目 N 主题】/【条目 N 进索引】`。
_ENTRY_FIELD_LABELS: tuple[tuple[str, str], ...] = (
    ("摘要", "summary"), ("正文", "body"), ("主题", "subject"), ("进索引", "indexed"))


def _entry_lines(idx: str, name: str, **kw: str) -> list[str]:
    """按模板体例拼一条条目的字段行（编号 `idx` 原样写进标签，便于造"编号写错"的输入）。"""
    lines = [f"【条目 {idx} 名称】：{name}"]
    for label, field_name in _ENTRY_FIELD_LABELS:
        if field_name in kw:
            lines.append(f"【条目 {idx} {label}】：{kw[field_name]}")
    return lines


def _lib_md(*groups: list[str], meta: dict[str, str] | None = None) -> str:
    """库信息 + 若干条目组 → 一份填好的信息库模板文本（同组字段之间空行隔开）。"""
    values = dict(_LIB_META if meta is None else meta)
    lines = [f"【{k}】：{v}" for k, v in values.items()]
    for group in groups:
        lines.extend(group)
    return "\n\n".join(lines) + "\n"


def _wide_lib_md(**over: str) -> str:
    """三条条目的广域库：含主题、含不进索引的那条、含正文里的双链。"""
    meta = dict(_LIB_META)
    meta.update(over)
    return _lib_md(
        _entry_lines(1, "地理·西线", summary="靠山，常年封冻", subject="地理",
                     body="西线靠山，常年封冻，商路一年只通三个月。[[朝局·三相]]"),
        _entry_lines(2, "朝局·三相", summary="互相牵制", subject="朝局",
                     body="三相互相牵制，谁也不肯先动。"),
        _entry_lines(3, "那封信", summary="旧信一封", subject="",
                     indexed="否", body="信是伪造的，[[地理·西线]]的商队带来的。"),
        meta=meta)


def _import_lib(md: str, tmp_path: Path, *, dry_run: bool = True,
                source_name: str = "填好的信息库.md") -> ti.ImportResult:
    """把 md 写成文件后走公开 API 导一份信息库（库根固定在 tmp_path/libraries）。"""
    src = tmp_path / source_name
    src.write_text(md, encoding="utf-8")
    return ti.import_template_file(
        src, characters_dir=tmp_path / "characters", scenes_dir=tmp_path / "scenes",
        libraries_dir=tmp_path / "libraries", dry_run=dry_run)


def _line_text(md: str, line: int | None) -> str:
    """报错行号指向的那一行原文（错误必须带行号，CLI 才说得出「第 N 行附近」）。"""
    assert line, "错误必须带行号（CLI 要说『第 N 行附近』）"
    return md.splitlines()[line - 1]


# ------------------------------------------------------------------ 完整解析
def test_library_template_multiple_entries_parsed_one_by_one():
    """三个条目逐条断言——只看条数会漏掉"第二条起被同名吞掉"这类静默丢失。"""
    tpl = ti.parse_library_template(_wide_lib_md())
    assert (tpl.name, tpl.scope, tpl.owner) == ("庆国世界观", "wide", "")
    assert tpl.subscriptions == []
    assert [e.key for e in tpl.entries] == ["地理·西线", "朝局·三相", "那封信"]

    first, second, third = tpl.entries
    assert (first.key, first.title) == ("地理·西线", "地理·西线")
    assert first.summary == "靠山，常年封冻"
    assert first.subject == "地理"
    assert first.indexed is True
    assert first.status == "active"
    assert first.body == "西线靠山，常年封冻，商路一年只通三个月。[[朝局·三相]]"
    assert parse_links(first.body) == ["朝局·三相"]      # 双链原样进正文
    assert first.origin.kind == "import"
    assert first.origin.turn == BASELINE_TURN            # 既有认识，不是本场产物

    assert (second.summary, second.subject) == ("互相牵制", "朝局")
    assert (third.summary, third.subject) == ("旧信一封", "")
    assert third.indexed is False                        # 「否」= 只藏在图里
    assert third.body == "信是伪造的，[[地理·西线]]的商队带来的。"
    assert parse_links(third.body) == ["地理·西线"]


def test_links_to_missing_entries_are_kept_verbatim_and_not_validated():
    """双链只原样保留：目标存不存在是**运行期**的事，导入期去校验等于无端报错。"""
    md = _lib_md(_entry_lines(1, "甲", body="指向[[查无此条]]的链接照样留着。"),
                 meta=_LIB_META)
    entry = ti.parse_library_template(md).entries[0]
    assert entry.body == "指向[[查无此条]]的链接照样留着。"
    assert parse_links(entry.body) == ["查无此条"]


def test_parse_library_template_public_api_returns_entries():
    tpl = ti.parse_library_template(_wide_lib_md())
    assert isinstance(tpl, ti.LibraryTemplate)
    assert tpl.entries[1].key == "朝局·三相"


def test_shipped_library_template_filled_parses():
    """模板即规格：仓库里那份模板按大模型填好的样子填一遍必须解析得出来。"""
    md = _filled(_read(LIB_TPL), {
        "库名": "《庆国世界观》",
        "类型": "广域库",
        "归属角色": "（无）",
        "订阅": "\n- 庙堂志",
        "条目 1 名称": "地理·西线",
        "条目 1 摘要": "靠山，常年封冻",
        "条目 1 正文": "西线靠山。\n商路一年只通三个月，[[朝局·三相]]。",
        "条目 1 主题": "地理",
        "条目 1 进索引": "是",
        "条目 2 名称": "朝局·三相",
        "条目 2 摘要": "互相牵制",
        "条目 2 正文": "三相互相牵制。",
        "条目 2 主题": "朝局",
        "条目 2 进索引": "否",
    })
    md += "\n" + "\n\n".join(_entry_lines(3, "那封信", summary="旧信一封",
                                          body="信是伪造的。")) + "\n"
    tpl = ti.parse_library_template(md)
    assert tpl.name == "庆国世界观"          # 书名号是渲染体例，不是名字的一部分
    assert tpl.scope == "wide" and tpl.owner == ""
    assert tpl.subscriptions == ["庙堂志"]
    assert [e.key for e in tpl.entries] == ["地理·西线", "朝局·三相", "那封信"]
    assert tpl.entries[0].body == "西线靠山。\n商路一年只通三个月，[[朝局·三相]]。"
    assert (tpl.entries[1].subject, tpl.entries[1].indexed) == ("朝局", False)


def test_shipped_library_template_alone_is_detected_as_library():
    assert ti.detect_kind(_read(LIB_TPL)) == "library"


def test_unfilled_library_template_is_rejected_on_the_library_name():
    """原样未填（全是模板提示）→ 首个该报的是「库名不能为空」，且带行号。"""
    md = _read(LIB_TPL)
    with pytest.raises(ti.TemplateError) as ei:
        ti.parse_library_template(md)
    assert ei.value.field == "库名"
    assert "【库名】" in _line_text(md, ei.value.line)


def test_library_fallback_name_used_when_library_name_missing():
    md = "【类型】：广域库\n\n【条目 1 名称】：甲\n"
    assert ti.parse_library_template(md, fallback_name="补库").name == "补库"


# ------------------------------------------------------------------ 库元信息
def test_character_scope_library_keeps_owner_and_subscriptions():
    md = _lib_md(_entry_lines(1, "药铺的暗格", summary="柜台下第三块砖",
                              body="第三块砖是活的。[[陈掌柜]]"),
                 meta={"库名": "戊的认知", "类型": "角色库",
                       "归属角色": "戊", "订阅": "\n- 庆国世界观\n- 庙堂志"})
    tpl = ti.parse_library_template(md)
    assert (tpl.name, tpl.scope, tpl.owner) == ("戊的认知", "character", "戊")
    assert tpl.subscriptions == ["庆国世界观", "庙堂志"]


@pytest.mark.parametrize("raw", ["角色库", "character", "角色"])
def test_character_library_type_spellings(raw):
    md = _lib_md(_entry_lines(1, "甲"), meta={"库名": "本子", "类型": raw,
                                             "归属角色": "戊", "订阅": ""})
    assert ti.parse_library_template(md).scope == "character"


def test_wide_library_ignores_owner_but_says_so(tmp_path):
    res = _import_lib(_lib_md(_entry_lines(1, "甲"),
                              meta={"库名": "本子", "类型": "广域库",
                                    "归属角色": "戊", "订阅": ""}), tmp_path)
    tpl = res.card_or_scene
    assert tpl.owner == ""
    assert any("归属角色" in w for w in res.warnings)     # 不静默丢


def test_unrecognised_library_type_is_rejected():
    md = _lib_md(_entry_lines(1, "甲"), meta={"库名": "本子", "类型": "看情况",
                                             "归属角色": "", "订阅": ""})
    with pytest.raises(ti.TemplateError) as ei:
        ti.parse_library_template(md)
    assert ei.value.field == "类型" and "看情况" in ei.value.message
    assert "【类型】" in _line_text(md, ei.value.line)


# ------------------------------------------------------------------ 分组：不多不少
def test_entries_with_identical_field_labels_are_not_swallowed():
    """两个条目的字段名**完全相同**（复制骨架忘了改编号）时，两条都必须解析出来。

    这正是 `_index`（`setdefault` 去重同名块）会踩的坑：它只留第一次出现的块，第二个
    条目起会被静默吞掉——不报错、不警告，只是"我记的东西少了一条"。
    """
    md = _lib_md(
        ["【条目 名称】：甲", "【条目 摘要】：甲的摘要", "【条目 正文】：甲的正文"],
        ["【条目 名称】：乙", "【条目 摘要】：乙的摘要", "【条目 正文】：乙的正文"],
        meta={"库名": "本子", "类型": "角色库", "归属角色": "戊", "订阅": ""})
    tpl = ti.parse_library_template(md)
    assert [(e.key, e.summary, e.body) for e in tpl.entries] == [
        ("甲", "甲的摘要", "甲的正文"), ("乙", "乙的摘要", "乙的正文")]


def test_reused_entry_numbers_start_a_new_entry():
    """第二组仍写 `【条目 1 …】`（复制粘贴没改编号）——以「名称」另起一组，两条都在。"""
    md = _lib_md(
        ["【条目 1 名称】：甲", "【条目 1 正文】：甲的正文"],
        ["【条目 1 名称】：乙", "【条目 1 正文】：乙的正文"],
        meta=_LIB_META)
    assert [e.key for e in ti.parse_library_template(md).entries] == ["甲", "乙"]


def test_missing_and_out_of_order_entry_numbers_are_tolerated():
    """缺编号 / 断号（1、3）都认：按出现顺序收，编号只管分组。"""
    md = _lib_md(["【条目 1 名称】：甲"],
                 ["【条目 3 名称】：乙"],
                 ["【条目 名称】：丙"],
                 meta=_LIB_META)
    assert [e.key for e in ti.parse_library_template(md).entries] == ["甲", "乙", "丙"]


def test_unknown_entry_field_only_warns(tmp_path):
    """不认识的条目字段（`【条目 1 备注】`）只警告并继续——但绝不静默。"""
    res = _import_lib(_lib_md(
        ["【条目 1 名称】：甲", "【条目 1 备注】：随手写的一行"], meta=_LIB_META), tmp_path)
    assert [e.key for e in res.card_or_scene.entries] == ["甲"]
    assert any("备注" in w for w in res.warnings)


def test_empty_entry_group_is_skipped_silently(tmp_path):
    """整组留空（原样未填的骨架）不是一条条目，也不该报错。"""
    md = _lib_md(_entry_lines(1, "甲"),
                 ["【条目 2 名称】：", "【条目 2 摘要】：", "【条目 2 正文】："],
                 meta=_LIB_META)
    res = _import_lib(md, tmp_path)
    assert [e.key for e in res.card_or_scene.entries] == ["甲"]
    assert res.warnings == []


def test_unparseable_indexed_flag_only_warns_and_keeps_default(tmp_path):
    res = _import_lib(_lib_md(_entry_lines(1, "甲", indexed="大概吧"),
                              meta=_LIB_META), tmp_path)
    assert res.card_or_scene.entries[0].indexed is True
    assert any("进索引" in w for w in res.warnings)


def test_missing_summary_and_body_are_listed_as_empty_fields(tmp_path):
    res = _import_lib(_lib_md(_entry_lines(1, "甲"), meta=_LIB_META), tmp_path)
    assert "条目 1 摘要" in res.empty_fields
    assert "条目 1 正文" in res.empty_fields
    assert "订阅" in res.empty_fields


# ------------------------------------------------------- 正文：原样整段保留（不许截断）
def test_body_keeps_markdown_after_a_rule_heading_or_bracket_line():
    """正文是自由 Markdown：`---`、`## 小标题`、`【…】：` 都是**内容**，不是值的终点。

    别的字段在标题/分隔线处收尾是对的——那是短句，`## 二、性格与能力` 是另一节的开头。
    正文不一样：它是条目的**唯一真相源**（设计 §4.2），用户真的会把一段带小标题、带
    分隔线、带 `【…】：` 的笔记写进去。在那里收尾就是**静默丢掉半段记忆**——不报错、
    不警告、报告照样打「√ 信息库」，用户只会在日后发现"它怎么不记得这段"，无从排查。
    """
    body = ("柜台下方，第三块砖是活的。抽出来，里侧有个夹层。\n\n"
            "---\n\n"
            "## 暗格的来历\n\n"
            "陈掌柜说这是上一任掌柜留下的，[[陈掌柜]]自己也没打开过。\n\n"
            "【注意】：他没说的是，夹层里还塞着一封对折的信。")
    md = _lib_md(_entry_lines(1, "药铺的暗格", summary="柜台下第三块砖", body=body,
                              subject="药铺"), meta=_LIB_META)
    entry = ti.parse_library_template(md).entries[0]
    assert entry.body == body
    assert parse_links(entry.body) == ["陈掌柜"]
    # 正文区域要停在下一个字段（主题）上——多吃了就会把后面的字段也吞掉
    assert entry.subject == "药铺" and entry.indexed is True


def test_body_region_ends_at_the_next_entry_field():
    """正文一路吃到下一个信息库字段行为止：下一条条目的字段不会被吞进正文。"""
    md = _lib_md(
        ["【条目 1 名称】：甲",
         "【条目 1 正文】：甲的正文。\n\n## 甲的小标题\n\n甲的后半段。"],
        ["【条目 2 名称】：乙", "【条目 2 正文】：乙的正文。"],
        meta=_LIB_META)
    tpl = ti.parse_library_template(md)
    assert [(e.key, e.body) for e in tpl.entries] == [
        ("甲", "甲的正文。\n\n## 甲的小标题\n\n甲的后半段。"), ("乙", "乙的正文。")]


def test_unknown_entry_field_after_a_body_is_still_reported(tmp_path):
    """`【条目 1 备注】` 是**字段行的形状**，不能被正文吞掉——否则"不认识的字段"那条警告就没了。"""
    res = _import_lib(_lib_md(
        ["【条目 1 名称】：甲",
         "【条目 1 正文】：正文一段。\n【条目 1 备注】：随手写的一行"], meta=_LIB_META),
        tmp_path)
    assert res.card_or_scene.entries[0].body == "正文一段。"
    assert any("备注" in w for w in res.warnings)


def test_character_and_scene_values_still_stop_at_a_heading_or_rule():
    """铁律：既有两套解析器一字不动——角色卡/场景卡的值仍在标题与分隔线处收尾。

    "一路吃到下一个字段"只挂在信息库的【条目 N 正文】上；`_blocks` 的 `_stop` 与
    `_body_text` 之外的取值路径一个字没改（角色卡的【性格描述】遇到 `## 下一节`
    必须还是收尾）。
    """
    card = ti.parse_character_template(
        "【姓名】：阿黎\n\n【性格描述】：话少。\n## 二、性格与能力\n这句属于下一节。\n")
    assert card.personality["性格描述"] == "话少。"

    scene = ti.parse_scene_template(
        "【场景名】：小馆\n\n【背景信息】：夜里十一点。\n---\n下一段不算背景。\n")
    assert scene.background == "夜里十一点。"


def test_body_truncation_is_not_possible_for_the_shipped_template(tmp_path):
    """仓库自带模板按大模型填好的样子填、正文里写了分隔线与小标题 → 一行都不许少。"""
    body = ("柜台下方，第三块砖是活的。\n\n---\n\n## 暗格的来历\n\n"
            "陈掌柜说这是上一任掌柜留下的，[[陈掌柜]]自己也没打开过。")
    md = _filled(_read(LIB_TPL), {
        "库名": "庆国世界观", "类型": "广域库", "归属角色": "（无）", "订阅": "（无）",
        "条目 1 名称": "药铺的暗格", "条目 1 摘要": "柜台下第三块砖是空的",
        "条目 1 正文": body, "条目 1 主题": "药铺", "条目 1 进索引": "是",
    })
    res = _import_lib(md, tmp_path, dry_run=False)
    written = entries_dir(res.path) / "药铺的暗格.md"
    assert load_library(res.path).library.entries["药铺的暗格"].body == body
    assert "## 暗格的来历" in written.read_text(encoding="utf-8")   # 落到条目文件里也没有


# ------------------------------------------------------------------ 类型判定
#: HEAD 版 detect_kind 的两组标记（冻结副本，只用于对拍）。
_HEAD_CHARACTER_ONLY = ("姓名", "性格描述", "台词样例")
_HEAD_SCENE_ONLY = ("场景名", "边界类型", "背景信息")


def _head_detect_kind(md: str) -> str:
    """改动前（HEAD）detect_kind 的**逐字副本**，用于对拍。

    与其相信记忆，不如把旧算法抄一份在这里对着跑：铁律要求"只有角色卡字段"与"只有
    场景卡字段"两种输入的判定结果（含歧义时的报错文案）**逐字节不变**。
    """
    keys = {ti._key(b.name) for b in ti._blocks(md)}
    char = [f for f in _HEAD_CHARACTER_ONLY if ti._key(f) in keys]
    scene = [f for f in _HEAD_SCENE_ONLY if ti._key(f) in keys]
    if char and scene:
        raise ti.TemplateError(
            "这份文件同时出现了角色卡字段（" + "、".join(char) + "）与场景卡字段（"
            + "、".join(scene) + "），分不清是角色卡还是场景卡。"
            "角色卡请用 templates/角色卡模板.md，场景卡请用 templates/场景卡模板.md，"
            "一份模板只填一个对象。")
    if char:
        return "character"
    if scene:
        return "scene"
    raise ti.TemplateError("认不出这是角色卡还是场景卡。")


def _detect_outcome(func, md: str) -> tuple[str, str]:
    """判定结果或报错文案——对拍要比的就是"要么给出同一个类型，要么给出同一句话"。"""
    try:
        return ("ok", func(md))
    except ti.TemplateError as exc:
        return ("error", str(exc))


def test_detect_kind_matches_head_for_character_and_scene_inputs():
    """对拍 HEAD：既有两种输入的判定结果（含歧义时的报错文案）与改动前逐字相同。"""
    cases = [
        _read(CHAR_TPL), _read(SCENE_TPL), _char_md(), _scene_md(),
        "【姓名】：甲\n", "【性格描述】：话少。\n", "【台词样例】：\n- 「不久。」\n",
        "【场景名】：某地\n", "【边界类型】：无\n", "【背景信息】：一个地方。\n",
        "## 随便写写\n\n【姓名】：甲\n\n【能力】：记路\n",
        # 角色卡模板里把【已知边界】换成场景卡的【边界类型】——两边标记同时命中
        _char_md().replace("【已知边界】", "【边界类型】"),
        # 【类型】这类普通字段名**不算**信息库标记：手写场景卡里的一个「类型」字段
        # 不该把这张卡从"场景卡"变成"分不清"（加了标记就会踩这个回归）
        _scene_md(extra_hook=False) + "\n\n【类型】：室内\n",
        _read(CHAR_TPL) + "\n## 附\n\n【场景名】：某地\n",
    ]
    for case in cases:
        assert _detect_outcome(ti.detect_kind, case) == \
            _detect_outcome(_head_detect_kind, case), case[:40]


@pytest.mark.parametrize("md,kind", [
    (_read(CHAR_TPL), "character"),
    (_read(SCENE_TPL), "scene"),
    (_read(LIB_TPL), "library"),
    ("【姓名】：甲\n", "character"),
    ("【场景名】：某地\n", "scene"),
    ("【库名】：某库\n", "library"),
    ("【条目 1 名称】：甲\n", "library"),
])
def test_detect_kind_three_way(md, kind):
    assert ti.detect_kind(md) == kind


def test_detect_kind_library_with_its_own_fields_is_not_ambiguous():
    assert ti.detect_kind("【库名】：某库\n\n【条目 1 名称】：甲\n") == "library"


@pytest.mark.parametrize("md,names", [
    ("【姓名】：甲\n\n【库名】：某库\n", ("角色卡", "信息库")),
    ("【场景名】：某地\n\n【库名】：某库\n", ("场景卡", "信息库")),
])
def test_detect_kind_rejects_library_mixed_with_another_kind(md, names):
    """多组标记同时命中仍按既有语义报错（信息库是第三组，不是例外）。"""
    with pytest.raises(ti.TemplateError) as ei:
        ti.detect_kind(md)
    for name in names:
        assert name in ei.value.message


def test_detect_kind_rejects_three_groups_at_once():
    md = "【姓名】：甲\n\n【场景名】：某地\n\n【库名】：某库\n"
    with pytest.raises(ti.TemplateError) as ei:
        ti.detect_kind(md)
    msg = str(ei.value)
    for name in ("角色卡", "场景卡", "信息库"):
        assert name in msg


def test_detect_kind_unrecognisable_mentions_all_three_templates():
    with pytest.raises(ti.TemplateError) as ei:
        ti.detect_kind("# 随便写写\n\n没有字段。\n")
    msg = str(ei.value)
    assert "认不出" in msg and "信息库" in msg


# ------------------------------------------------------------------ 落盘
def test_import_library_writes_a_loadable_library(tmp_path):
    libs = tmp_path / "libraries"
    res = _import_lib(_wide_lib_md(), tmp_path, dry_run=False)
    assert (res.kind, res.name) == ("library", "庆国世界观")
    assert res.path == library_dir("wide", "庆国世界观", root=libs)
    assert res.path.is_dir()

    loaded = load_library(res.path)
    assert loaded.warnings == []
    assert (loaded.library.name, loaded.library.scope, loaded.library.owner) == (
        "庆国世界观", "wide", "")
    assert loaded.library.ordered_keys() == ["地理·西线", "朝局·三相", "那封信"]
    written = res.card_or_scene
    assert isinstance(written, ti.LibraryTemplate)
    for entry in written.entries:                 # 读回来逐字一致
        assert loaded.library.entries[entry.key] == entry
    meta = json.loads((res.path / "library.json").read_text(encoding="utf-8"))
    assert (meta["schema"], meta["name"], meta["scope"], meta["owner"]) == (
        1, "庆国世界观", "wide", "")
    assert meta["order"] == ["地理·西线", "朝局·三相", "那封信"]


def test_character_library_lands_under_the_owner_name(tmp_path):
    """角色库的目录段是**归属角色**（引擎按角色名找库），库名只是显示名（§3.2）。"""
    md = _lib_md(_entry_lines(1, "药铺的暗格", summary="柜台下第三块砖",
                              body="第三块砖是活的。"),
                 meta={"库名": "戊的认知", "类型": "角色库",
                       "归属角色": "戊", "订阅": ""})
    res = _import_lib(md, tmp_path, dry_run=False)
    assert res.path == library_dir("character", "戊", root=tmp_path / "libraries")
    loaded = load_library(res.path).library
    assert (loaded.name, loaded.scope, loaded.owner) == ("戊的认知", "character",
                                                         "戊")
    assert loaded.entries["药铺的暗格"].body == "第三块砖是活的。"


def test_import_library_dry_run_writes_nothing(tmp_path):
    res = _import_lib(_wide_lib_md(), tmp_path, dry_run=True)
    assert res.path is None
    assert not (tmp_path / "libraries").exists()
    assert res.card_or_scene.entries[0].key == "地理·西线"


def test_import_library_refuses_to_overwrite_without_force(tmp_path):
    _import_lib(_wide_lib_md(), tmp_path, dry_run=False)
    with pytest.raises(ti.TemplateError) as ei:
        _import_lib(_wide_lib_md(), tmp_path, dry_run=False)
    assert "已存在" in ei.value.message


def test_import_library_overwrites_with_force_and_keeps_unlisted_entries(tmp_path):
    """覆盖只重写模板里列出的条目；库里别的手写条目**保留**并提示（不静默删素材）。"""
    from harness.knowledgestore import save_library

    res = _import_lib(_wide_lib_md(), tmp_path, dry_run=False)
    lib = load_library(res.path).library
    lib.upsert(Entry(key="手写的", title="手写的", body="用户自己加的。"))
    save_library(lib, res.path)

    src = tmp_path / "改过.md"
    src.write_text(_wide_lib_md(), encoding="utf-8")
    again = ti.import_template_file(
        src, characters_dir=tmp_path / "characters", scenes_dir=tmp_path / "scenes",
        libraries_dir=tmp_path / "libraries", overwrite=True)
    loaded = load_library(again.path).library
    assert "手写的" in loaded.entries
    assert any("手写的" in w for w in again.warnings)


def test_import_library_without_libraries_dir_defaults_beside_characters(tmp_path):
    """缺省库根 = `characters_dir` 的兄弟目录 `libraries/`（app/ 下与 characters 同级）。"""
    src = tmp_path / "库.md"
    src.write_text(_wide_lib_md(), encoding="utf-8")
    res = ti.import_template_file(
        src, characters_dir=tmp_path / "characters", scenes_dir=tmp_path / "scenes")
    assert res.path == tmp_path / "libraries" / "wide" / "庆国世界观"
    assert entries_dir(res.path).is_dir()


def _overwrite_lib(md: str, tmp_path: Path, *, source_name: str = "覆盖.md") -> ti.ImportResult:
    """带 `overwrite=True` 再导一份信息库（覆盖场景专用）。"""
    src = tmp_path / source_name
    src.write_text(md, encoding="utf-8")
    return ti.import_template_file(
        src, characters_dir=tmp_path / "characters", scenes_dir=tmp_path / "scenes",
        libraries_dir=tmp_path / "libraries", overwrite=True)


def test_import_library_rejects_a_template_entry_clashing_with_an_existing_one(tmp_path):
    """模板里的条目名与库里**未列出**的既有条目只差大小写 → 当场报错，绝不信"已原样保留"。

    NTFS 与默认的 macOS 卷上 `abc.md` 与 `ABC.md` 是**同一个文件**：覆盖导入会把模板
    正文写进那份文件，用户手写的那条被静默顶掉，而报告恰恰写着"已原样保留：abc"。
    模板内部撞名在解析期就报错（`entry_key_collision_error`），同一份判定口径必须也
    管住"模板 vs 磁盘"这一侧——否则用户不但丢了东西，还被告知没丢。
    """
    first = _import_lib(_lib_md(_entry_lines(1, "abc", summary="手写的",
                                             body="用户手写的正文"), meta=_LIB_META),
                        tmp_path, dry_run=False)
    with pytest.raises(ti.TemplateError) as ei:
        _overwrite_lib(_lib_md(_entry_lines(1, "ABC", summary="模板的",
                                            body="模板正文"), meta=_LIB_META), tmp_path)
    assert "大小写" in ei.value.message
    assert "abc" in ei.value.message and "ABC" in ei.value.message
    # 报错之外更要紧的是：库里那份**一个字节都没被动**
    loaded = load_library(first.path).library
    assert loaded.entries["abc"].body == "用户手写的正文"
    assert sorted(p.name for p in entries_dir(first.path).glob("*.md")) == ["abc.md"]


def test_import_library_rejects_a_target_path_occupied_by_a_file(tmp_path):
    """库是**目录**：路径被同名文件占着时必须说人话，不能把 WinError 183 甩给用户。

    原来的"目标已存在"判据只认 `library.json` 与 `entries/`，于是"目标存在但是个文件"
    被判成"不存在"，直接进 `save_library` → `entries.mkdir()` 抛 FileExistsError 一路
    冒出去（CLI 只捕 TemplateError，用户看到的是 Python traceback + WinError 183）。
    """
    target = library_dir("wide", "庆国世界观", root=tmp_path / "libraries")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("占位", encoding="utf-8")

    with pytest.raises(ti.TemplateError) as ei:
        _import_lib(_wide_lib_md(), tmp_path, dry_run=False)
    assert "占着" in ei.value.message and str(target) in ei.value.message
    with pytest.raises(ti.TemplateError) as forced:     # 加 --force 也一样写不进去
        _overwrite_lib(_wide_lib_md(), tmp_path)
    assert "占着" in forced.value.message
    assert target.read_text(encoding="utf-8") == "占位"   # 那个文件也没被动


@pytest.mark.parametrize("occupied", ["entries", "library.json", "root"])
def test_import_library_reports_every_non_directory_obstacle(tmp_path, occupied):
    """同一处缺口的另外三段：`entries/` 是文件、`library.json` 是目录、库根是文件。"""
    libs = tmp_path / "libraries"
    target = library_dir("wide", "庆国世界观", root=libs)
    if occupied == "entries":
        (target / "entries").parent.mkdir(parents=True, exist_ok=True)
        (target / "entries").write_text("占位", encoding="utf-8")
    elif occupied == "library.json":
        target.mkdir(parents=True, exist_ok=True)
        (target / "library.json").mkdir()
    else:
        libs.parent.mkdir(parents=True, exist_ok=True)
        libs.write_text("占位", encoding="utf-8")

    with pytest.raises(ti.TemplateError) as ei:
        _import_lib(_wide_lib_md(), tmp_path, dry_run=False)
    assert "占着" in ei.value.message


# ------------------------------------------------------------------ 错误表
@pytest.mark.parametrize("md,field,line_label,fragment", [
    (_lib_md(_entry_lines(1, "甲"), meta={"库名": "", "类型": "广域库",
                                          "归属角色": "", "订阅": ""}),
     "库名", "【库名】", "不能为空"),
    (_lib_md(_entry_lines(1, "甲"), meta={"库名": "本子", "类型": "",
                                          "归属角色": "", "订阅": ""}),
     "类型", "【类型】", "广域库"),
    (_lib_md(_entry_lines(1, "甲"), meta={"库名": "本子", "类型": "角色库",
                                          "归属角色": "", "订阅": ""}),
     "归属角色", "【归属角色】", "归属角色"),
    (_lib_md(_entry_lines(1, "../坏"), meta=_LIB_META),
     "条目 1 名称", "【条目 1 名称】", "键"),
    (_lib_md(_entry_lines(1, "甲"), meta={"库名": "../坏", "类型": "广域库",
                                          "归属角色": "", "订阅": ""}),
     "库名", "【库名】", "目录"),
    (_lib_md(_entry_lines(1, "甲"), meta={"库名": "本子", "类型": "角色库",
                                          "归属角色": "a/b", "订阅": ""}),
     "归属角色", "【归属角色】", "目录"),
])
def test_library_error_table_names_field_and_line(md, field, line_label, fragment):
    with pytest.raises(ti.TemplateError) as ei:
        ti.parse_library_template(md)
    assert ei.value.field == field
    assert fragment in ei.value.message
    assert line_label in _line_text(md, ei.value.line)


def test_missing_library_meta_labels_still_report_a_line():
    """整块没写（连字段标签都没有）时也不能没有行号：退到同节能定位的最前一行为止。"""
    md = "【条目 1 名称】：甲\n"
    with pytest.raises(ti.TemplateError) as ei:
        ti.parse_library_template(md)
    assert ei.value.field == "库名"
    assert "【条目 1 名称】" in _line_text(md, ei.value.line)

    md2 = "【库名】：本子\n\n【类型】：角色库\n\n【条目 1 名称】：甲\n"
    with pytest.raises(ti.TemplateError) as ei2:
        ti.parse_library_template(md2)
    assert ei2.value.field == "归属角色"
    assert "【类型】" in _line_text(md2, ei2.value.line)


def test_library_without_any_entry_is_rejected():
    md = _lib_md(meta=_LIB_META)
    with pytest.raises(ti.TemplateError) as ei:
        ti.parse_library_template(md)
    assert "条目" in ei.value.message


def test_entry_without_a_name_is_rejected():
    md = _lib_md(["【条目 1 名称】：", "【条目 1 正文】：没有名字的正文"], meta=_LIB_META)
    with pytest.raises(ti.TemplateError) as ei:
        ti.parse_library_template(md)
    assert ei.value.field == "条目 1 名称"
    assert "【条目 1 名称】" in _line_text(md, ei.value.line)


def test_duplicate_entry_names_are_rejected_with_the_second_line():
    md = _lib_md(["【条目 1 名称】：甲", "【条目 1 正文】：正文一"],
                 ["【条目 2 名称】：甲", "【条目 2 正文】：正文二"],
                 meta=_LIB_META)
    with pytest.raises(ti.TemplateError) as ei:
        ti.parse_library_template(md)
    assert "甲" in ei.value.message and "重复" in ei.value.message
    assert "【条目 2 名称】" in _line_text(md, ei.value.line)


def test_entry_names_differing_only_by_case_are_rejected():
    """`ABC` 与 `abc` 各自合法却是同一个文件名——当场报错，绝不静默丢一条（§4.3）。"""
    md = _lib_md(["【条目 1 名称】：ABC"], ["【条目 2 名称】：abc"], meta=_LIB_META)
    with pytest.raises(ti.TemplateError) as ei:
        ti.parse_library_template(md)
    assert "大小写" in ei.value.message
    assert "【条目 2 名称】" in _line_text(md, ei.value.line)


# ------------------------------------------------------------------ CLI
def _cli_dirs_all(tmp_path: Path) -> list[str]:
    return [*_cli_dirs(tmp_path),
            "--libraries-dir", str(tmp_path / "libraries")]


def test_cli_default_libraries_dir_is_the_materials_libraries_dir():
    """信息库根缺省与 characters/scenes 同一口径（`materials_dir()/libraries`）。

    它是**写**路径（播种、每场副本、散场结算都往那儿写），故必须与素材同址分叉：
    开发态 = 仓库里的 `app/libraries`（既有数据原地不动、与改造前逐字节同址），
    冻结态 = 用户数据目录（安装目录只读，写进去必失败）。
    """
    from harness import paths as paths_mod
    assert import_cards.default_libraries_dir() == paths_mod.materials_dir() / "libraries"


def test_cli_imports_library_and_reports_chinese_kind(tmp_path, capsys):
    src = tmp_path / "库.md"
    src.write_text(_wide_lib_md(订阅="\n- 庙堂志"), encoding="utf-8")
    code = import_cards.main([str(src), *_cli_dirs_all(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "信息库《庆国世界观》" in out
    assert "庙堂志" in out
    assert (tmp_path / "libraries" / "wide" / "庆国世界观" / "library.json").is_file()


def test_cli_imports_all_three_kinds_in_one_run(tmp_path, capsys):
    paths = []
    for name, md in (("卡.md", _char_md()), ("场.md", _scene_md()),
                     ("库.md", _wide_lib_md())):
        path = tmp_path / name
        path.write_text(md, encoding="utf-8")
        paths.append(str(path))
    code = import_cards.main([*paths, *_cli_dirs_all(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "角色卡《戊》" in out
    assert "场景卡《深夜小馆》" in out
    assert "信息库《庆国世界观》" in out


def test_cli_keeps_going_after_a_bad_library_file(tmp_path, capsys):
    bad = tmp_path / "坏库.md"
    bad.write_text(_lib_md(_entry_lines(1, "甲"),
                           meta={"库名": "本子", "类型": "看情况",
                                 "归属角色": "", "订阅": ""}), encoding="utf-8")
    good = tmp_path / "好库.md"
    good.write_text(_wide_lib_md(), encoding="utf-8")
    code = import_cards.main([str(bad), str(good), *_cli_dirs_all(tmp_path)])
    out = capsys.readouterr().out
    assert code == 1
    assert "看情况" in out and "第 " in out and "行附近" in out
    assert "信息库《庆国世界观》" in out
    assert (tmp_path / "libraries" / "wide" / "庆国世界观").is_dir()


def test_cli_reports_an_occupied_library_path_in_chinese(tmp_path, capsys):
    """CLI 契约「失败只说人话」：路径被同名文件占着也是 TemplateError，不是 traceback。"""
    target = library_dir("wide", "庆国世界观", root=tmp_path / "libraries")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("占位", encoding="utf-8")
    src = tmp_path / "库.md"
    src.write_text(_wide_lib_md(), encoding="utf-8")
    code = import_cards.main([str(src), *_cli_dirs_all(tmp_path)])
    out = capsys.readouterr().out
    assert code == 1
    assert "占着" in out and "Traceback" not in out


def test_cli_library_dry_run_writes_nothing(tmp_path, capsys):
    src = tmp_path / "库.md"
    src.write_text(_wide_lib_md(), encoding="utf-8")
    code = import_cards.main([str(src), "--dry-run", *_cli_dirs_all(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "（试运行，未写盘）" in out
    assert not (tmp_path / "libraries").exists()
