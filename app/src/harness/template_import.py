"""模板导入器：把大模型按 `templates/*.md` 填好的 Markdown 解析成角色卡/场景卡/信息库并入库。

**模板即规格**：`templates/角色卡模板.md`、`templates/场景卡模板.md` 与
`templates/信息库模板.md` 是唯一权威格式，
本模块逐字段照它解析——字段名写作 `【字段名】`（可带 markdown 粗体 `**【姓名】**`）后跟
全角或半角冒号，值取冒号之后的**整行**，外加其后**直到下一个 `【…】` 字段名或章节标题**
的非空行（大模型常把长文本换行写在字段下面）。章节标题（`## 二、性格与能力`）、分隔线
（`---`）与下一个字段名都当作值的终点。

**唯一的例外是【条目 N 正文】**（信息库）：那一路的值一路吃到下一个信息库字段行
（`【条目 N 主题】`/下一条的字段/库级字段）为止，`---`、`## 小标题`、`【…】：` 都留在
正文里（`_extended_body`）。理由是正文乃条目的唯一真相源、是自由 Markdown，在那里收尾
等于静默丢掉用户半段记忆。角色卡/场景卡的取值规则一个字未动。

**提示 vs 真值**：模板自带提示（`【一句话定位】：（例：冷静克制…）`）。提示不是值——整行
是一个括号提示（`（…）`）、或以已知提示词开头（`（例：` / `（一行一条` / `（填 ` / …）的行
一律丢弃；`（素材未提及）` / `（无）` / `—` / `N/A` / `无` 这些占位同样算空。于是「原样
未填」的模板解析出来的全是字段默认值（且逐个列进 `empty_fields`，供界面/CLI 提示用户）。

**列表**：一行一条，`-` / `*` / `•` / `数字.` / `数字、` 开头；没有项目符号但用 `、` 分隔的
单行也拆成多条（`能力：过目不忘、一手好字`）。空条目与 `（无）` 丢弃。

**报错说人话**：任何一处不合法（权重不是 0~1、时间不是 HH:MM、边界类型不认识、hook 不合法、
pydantic 校验不过）都抛 `TemplateError`——带中文字段名、中文说明与出错行号，绝不把
traceback 丢给用户；CLI 与界面照着它逐字段提示（见《使用说明》「解析失败怎么办」）。

本模块是**纯解析 + 落盘**：不依赖 PySide6、不做网络、不建引擎。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from .hooks import Hook, validate as validate_hook
from .knowledge import Entry, EntryOrigin, entry_key_collision_error, entry_key_error
from .knowledgestore import (
    BASELINE_TURN, Library, entries_dir, has_library, library_dir, library_json_path,
    load_library, save_library)
from .loaders import (
    save_character_card, save_scene, scene_filename_error, scene_path_for)
from .sceneclock import format_hhmm, parse_hhmm
from .schemas import (
    CharacterCard, Corpus, HardBoundary, Scene, SceneCastMember, Weights)

__all__ = [
    "ImportResult", "LibraryTemplate", "TemplateError", "detect_kind",
    "import_template_file", "parse_character_template", "parse_library_template",
    "parse_scene_template",
]


class TemplateError(Exception):
    """模板解析/落盘失败：`.message` 中文说明、`.field` 中文字段名（无则空串）、
    `.line` 出错行号（1 起，未知则 None）——CLI 与界面照它逐字段提示用户。"""

    def __init__(self, message: str = "", field: str = "",
                 line: int | None = None):
        super().__init__(message)
        self.message = message
        self.field = field
        self.line = line

    def __str__(self) -> str:
        """`第 N 行附近 字段【X】：消息`（缺行号/字段名就省略那一段）——日志里也说人话。"""
        where = ([f"第 {self.line} 行附近"] if self.line else []) + \
                ([f"字段【{self.field}】"] if self.field else [])
        return (" ".join(where) + "：" + self.message) if where else self.message


@dataclass
class ImportResult:
    """一次导入的结果：写出的路径（试运行/未写盘 → None）、类型、名字、解析出的对象，
    以及「模板里没填、已用默认值」的字段清单与解析期警告。"""
    path: Path | None
    kind: str                      # "character" | "scene" | "library"
    name: str
    card_or_scene: object          # CharacterCard / Scene / LibraryTemplate
    empty_fields: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ 文本工具
#: 字段行：可有项目符号、可有 markdown 粗体包裹的【字段名】，冒号全/半角皆可。
_LABEL_RE = re.compile(
    r"^\s*(?:[-*+•]\s*)?(?:\*\*|__)?\s*【(?P<label>[^】]+)】\s*(?:\*\*|__)?"
    r"\s*[：:]\s*(?P<rest>.*)$")
#: 章节标题（`## 一、基本信息`）——值的终点。
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s")
#: 分隔线（`---` / `***` / `___`）——值的终点。
_HR_RE = re.compile(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$")
#: 项目符号（行首）：`-` / `*` / `+` / `•` / `1.` / `1、` / `1)`。
_BULLET_RE = re.compile(r"^(?:[-*+•]\s*|\d{1,3}[.、)）]\s*)")
_BULLET_ONLY_RE = re.compile(r"(?:[-*+•]|\d{1,3}[.、)）])\s*")

#: 占位（算「没填」）：全角/半角括号、破折号、N/A 与「无」。
_PLACEHOLDERS = frozenset({
    "", "—", "–", "―", "-", "－", "N/A", "n/a", "NA", "无", "无。",
    "（无）", "(无)", "（无内容）", "（素材未提及）", "(素材未提及)", "素材未提及",
    "（未提及）", "(未提及)", "未提及", "（略）", "(略)", "略", "TBD", "待填", "（待填）",
})

#: 模板提示的开头词（去掉前导括号后比对）；命中即「这一行是提示、不是值」。
_HINT_CORES = (
    "例：", "例:", "例如", "如：", "如:", "如 ", "一行一条", "一行一个", "3~", "3-",
    "0~1", "0~", "填", "素材", "24 小时制", "24小时制", "仅当", "你希望", "一句话说明",
    "对方姓名", "这个地点", "他知道", "如何", "措辞", "话题", "情绪", "被点名",
    "有非说不可", "天生", "克制", "公开场合", "越大消气",
)
_OPEN_PARENS = "（("
_CLOSE_PARENS = "）)"


def _strip_md(text: str) -> str:
    """去掉 markdown 粗体/下划线与反引号（引号「」"" 一律保留——台词样例要原样）。"""
    return text.replace("**", "").replace("__", "").replace("`", "")


def _is_paren_group(text: str) -> bool:
    """整行就是一个（可嵌套的）括号组 → 是模板提示（如 `（3~5 条，必须是素材原句）`）。"""
    if len(text) < 2 or text[0] not in _OPEN_PARENS:
        return False
    depth = 0
    for i, ch in enumerate(text):
        if ch in _OPEN_PARENS:
            depth += 1
        elif ch in _CLOSE_PARENS:
            depth -= 1
            if depth == 0:
                return i == len(text) - 1
    return False


def _is_hint_paren(text: str) -> bool:
    """这一行是不是模板自带的**括号提示**（`（例：…）` / `（3~5 条…）`）。

    只看形状不看字段：整行一个括号组，或以括号开头且接已知提示词。故 `无` 这种
    「既是占位又是合法取值」的文本不会被误判（边界类型要的就是 `无`）。
    """
    if not text:
        return True
    if _is_paren_group(text):
        return True
    return text[0] in _OPEN_PARENS and text[1:].startswith(_HINT_CORES)


def _is_placeholder(text: str) -> bool:
    return text in _PLACEHOLDERS


def _key(label: str) -> str:
    """字段名归一化（去空白）——`【相关权重 w1】` 与 `【相关权重w1】` 同一个字段。"""
    return re.sub(r"\s+", "", label or "")


def _strip_bullet(text: str) -> str:
    return _BULLET_RE.sub("", text).strip() if _BULLET_RE.match(text) else text.strip()


# ------------------------------------------------------------------ 字段块
@dataclass
class _Block:
    """一个 `【字段名】` 块：原始字段名、行号（1 起）、值行（含冒号后那一段）。"""
    name: str
    line: int
    raw: list[str]


def _blocks(md: str) -> list[_Block]:
    """把 Markdown 切成字段块（按出现顺序；值一直吃到下一个字段名/标题/分隔线）。"""
    lines = (md or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")

    def _stop(line: str) -> bool:
        return bool(_LABEL_RE.match(line) or _HEADING_RE.match(line)
                    or _HR_RE.match(line))

    out: list[_Block] = []
    i = 0
    while i < len(lines):
        m = _LABEL_RE.match(lines[i])
        if m is None:
            i += 1
            continue
        rest = m.group("rest")
        raw = [rest] if rest.strip() else []
        j = i + 1
        while j < len(lines) and not _stop(lines[j]):
            raw.append(lines[j])
            j += 1
        out.append(_Block(name=m.group("label").strip(), line=i + 1, raw=raw))
        i = j
    return out


def _index(blocks: list[_Block]) -> dict[str, _Block]:
    """归一化字段名 → 块（同名重复出现时取**第一次**，与模板顺序一致）。"""
    out: dict[str, _Block] = {}
    for b in blocks:
        out.setdefault(_key(b.name), b)
    return out


def _value_lines(block: _Block | None, *, drop_placeholders: bool = True) -> list[str]:
    """块 → 有效值行（丢空行、提示行、占位行、只有项目符号的空行）。

    `drop_placeholders=False` 时保留 `无` / `—` / `N/A` 这些文本——**边界类型**要拿它们
    当值看（`无` = type "none"，`—` = 没填）；模板自带的括号提示则一律丢掉。
    """
    if block is None:
        return []
    out: list[str] = []
    for raw in block.raw:
        text = _strip_md(raw).strip()
        if not text or _is_hint_paren(text):
            continue
        body = _strip_bullet(text)
        if drop_placeholders and (not body or _is_bullet_only(text)
                                  or _is_placeholder(body)):
            continue
        out.append(text)
    return out


def _is_bullet_only(text: str) -> bool:
    return bool(_BULLET_ONLY_RE.fullmatch(text))


def _text(block: _Block | None) -> str:
    """块 → 多行文本值（每行去掉项目符号；行间以换行相连）。"""
    return "\n".join(_strip_bullet(s) for s in _value_lines(block))


def _first_line(block: _Block | None, *, drop_placeholders: bool = True) -> str:
    """块 → 第一行有效值（标量字段用：时间/是-否/枚举都只有一个值）。

    多出来的行（大模型在字段下面补的一句说明）不影响标量解析——内容字段才整段收。
    """
    lines = _value_lines(block, drop_placeholders=drop_placeholders)
    return _strip_bullet(lines[0]).strip() if lines else ""


def _list(block: _Block | None) -> list[str]:
    """块 → 列表值（一行一条；无项目符号但含 `、` 的单行也拆开）。"""
    out: list[str] = []
    for text in _value_lines(block):
        body = _strip_bullet(text)
        if not body:
            continue
        if not _BULLET_RE.match(text) and "、" in body \
                and "：" not in body and ":" not in body:
            parts = [p.strip() for p in body.split("、")]
            out.extend(p for p in parts if p and not _is_placeholder(p))
        else:
            out.append(body)
    return out


def _line_of(by_key: dict[str, _Block], label: str) -> int | None:
    """字段所在行号（字段不存在 → None）。"""
    block = by_key.get(_key(label))
    return block.line if block is not None else None


def _split_pair(line: str) -> tuple[str, str]:
    """「键：值」→ (键, 值)：全/半角冒号都认，取最先出现的那个；无冒号则值空。"""
    best = -1
    for sep in ("：", ":"):
        i = line.find(sep)
        if i != -1 and (best == -1 or i < best):
            best = i
    if best == -1:
        return line.strip(), ""
    return line[:best].strip(), line[best + 1:].strip()


def _pairs(block: _Block | None, *, field_name: str,
           warnings: list[str]) -> dict[str, str]:
    """块 → 「键：值」字典（认不出的行警告后丢弃，不静默吞）。"""
    out: dict[str, str] = {}
    for item in _list(block):
        key, value = _split_pair(item)
        if not key:
            warnings.append(f"【{field_name}】里的「{item}」不是「键：值」格式，已忽略。")
            continue
        out[key] = value
    return out


# ------------------------------------------------------------------ 类型判定
#: 只在角色卡模板里出现的字段（判定类型用）。
_CHARACTER_ONLY = ("姓名", "性格描述", "台词样例")
#: 只在场景卡模板里出现的字段。
_SCENE_ONLY = ("场景名", "边界类型", "背景信息")
#: 只在信息库模板里出现的**库级**字段（条目族的编号不固定，由 `_ENTRY_LABEL_RE` 另认）。
#:
#: `【类型】` **刻意不算标记**：它太普通，手写的场景卡里出现一个「类型」字段完全可能，
#: 把它当信息库标记会让那种卡从"场景卡"变成"分不清"——既有两选一的判定结果就变了。
#: 而信息库模板必定有【库名】与【条目 N 名称】，这两个信号已经足够。
_LIBRARY_ONLY = ("库名", "归属角色")
#: 条目字段名：`条目` + 编号（可缺/可乱） + 五个部分之一。类型判定与分组共用这一条正则。
_ENTRY_LABEL_RE = re.compile(r"^条目(?P<idx>.*?)(?P<part>名称|摘要|正文|主题|进索引)$")


def _library_fields(blocks: list[_Block]) -> list[str]:
    """信息库的标记字段（按出现顺序、去重；条目族只报一次「条目 N 名称」）。

    条目族的标签带编号（`【条目 1 名称】`），逐条列进报错文案会变成一屏噪声，故只报一次。
    """
    out: list[str] = []
    entry_seen = False
    for block in blocks:
        name = _key(block.name)
        if name in _LIBRARY_ONLY:
            if block.name.strip() not in out:
                out.append(block.name.strip())
        elif not entry_seen and _ENTRY_LABEL_RE.match(name):
            entry_seen = True
            out.append("条目 N 名称")
    return out


def detect_kind(md: str) -> str:
    """认模板类型 → "character" / "scene" / "library"；认不出或多组标记同中 → TemplateError。

    三组标记**各认各的**，命中两组以上就报错——"一份模板只填一个对象"这条语义与只有
    角色卡/场景卡两选一的时候完全一致（那两种输入的判定结果与报错文案一字未变）。
    """
    blocks = _blocks(md)
    keys = {_key(b.name) for b in blocks}
    hits: list[tuple[str, str, str, list[str]]] = [
        ("character", "角色卡", "角色卡模板.md",
         [f for f in _CHARACTER_ONLY if _key(f) in keys]),
        ("scene", "场景卡", "场景卡模板.md",
         [f for f in _SCENE_ONLY if _key(f) in keys]),
        ("library", "信息库", "信息库模板.md", _library_fields(blocks)),
    ]
    present = [(kind, label, tpl, fields) for kind, label, tpl, fields in hits if fields]
    if len(present) > 1:
        detail = "与".join(f"{label}字段（{'、'.join(fields)}）"
                          for _kind, label, _tpl, fields in present)
        if len(present) == 2:
            _a, first, _t, _f = present[0]
            _b, second, _t, _f = present[1]
            what = f"分不清是{first}还是{second}"
        else:
            what = "分不清是" + "、".join(label for _k, label, _t, _f in present) + "中的哪一种"
        guide = "，".join(f"{label}请用 templates/{tpl}"
                         for _k, label, tpl, _f in present)
        raise TemplateError(
            f"这份文件同时出现了{detail}，{what}。{guide}，一份模板只填一个对象。")
    if present:
        return present[0][0]
    raise TemplateError(
        "认不出这是什么模板：文件里既没有角色卡的【姓名】/【性格描述】/【台词样例】，"
        "也没有场景卡的【场景名】/【边界类型】/【背景信息】，"
        "也没有信息库的【库名】/【类型】/【条目 N 名称】。"
        "请用 templates/ 下的模板填写（字段名连同【】与冒号要原样保留）。")


# ------------------------------------------------------------------ 角色卡
#: 权重字段：模板里的建议标签 → Weights 字段名（顺序即 w1…w7）。
_WEIGHT_LABELS: tuple[tuple[str, str], ...] = (
    ("w1_relevance", "相关权重 w1"),
    ("w2_arousal", "唤醒权重 w2"),
    ("w3_adjacency", "邻接权重 w3"),
    ("w4_goal_pressure", "目标权重 w4"),
    ("w5_talkativeness", "话痨权重 w5"),
    ("w6_inhibition", "抑制权重 w6"),
    ("w7_scene_pressure", "场景权重 w7"),
)
_DECAY_LABEL = "情绪衰减"
#: 「相关权重 w1」→ w1_relevance（按 w 编号索引；缺编号时按中文名兜底）。
_WEIGHT_BY_NUM = {str(i): f for i, (f, _l) in enumerate(_WEIGHT_LABELS, start=1)}
_WEIGHT_BY_NAME = {_key(label): f for f, label in _WEIGHT_LABELS}


def _weight_field(label: str) -> str | None:
    """字段名 → Weights 字段（不是权重字段则 None）。"""
    key = _key(label)
    m = re.search(r"w([1-7])", key, re.IGNORECASE)
    if m:
        return _WEIGHT_BY_NUM[m.group(1)]
    return _WEIGHT_BY_NAME.get(key)


_TRAILING_PAREN_RE = re.compile(r"[（(][^（()）]*[)）]\s*$")


def _try_weight(text: str) -> float | None:
    """一行文本 → 数值（**不做 0~1 范围判定**）；根本不是个数字则返回 None。

    认 `0.7` / `0,7`（逗号小数点）/ `70%` / `0.7（说明）`（尾随说明剥掉）；
    大于 1 视为百分数（÷100，大模型常写 `70` 这种整数）。
    """
    raw = text.strip()
    trimmed = _TRAILING_PAREN_RE.sub("", raw).strip()
    while trimmed != raw:                       # 说明可能叠了好几层括号
        raw = trimmed
        trimmed = _TRAILING_PAREN_RE.sub("", raw).strip()
    token = trimmed.rstrip("。.").strip()
    if token.endswith("%"):
        try:
            value = float(token[:-1]) / 100.0
        except ValueError:
            return None
    elif re.fullmatch(r"\d+(?:,\d+)?", token):
        value = float(token.replace(",", "."))   # `,7` / `7,` 都不匹配 → 报错
    else:
        try:
            value = float(token)
        except ValueError:
            return None
    return value / 100.0 if value > 1.0 else value


def _parse_weight(lines: list[str], *, field_name: str, line: int | None) -> float:
    """权重值行 → 0~1 小数：取**第一个是个数字的**行（字段下面多写的说明行不参与）。"""
    for text in lines:
        value = _try_weight(text)
        if value is None:
            continue
        if not 0.0 <= value <= 1.0:
            raise TemplateError(f"必须在 0~1 之间（收到 {text.strip()!r}）。",
                                field=field_name, line=line)
        return value
    raise TemplateError(
        f"要填 0~1 的小数（如 0.7、70%），认不出这个值：{' '.join(lines).strip()!r}。",
        field=field_name, line=line)


def _parse_character(md: str, fallback_name: str = "") -> tuple[
        CharacterCard, list[str], list[str]]:
    """角色卡模板 → (卡, 未填字段, 警告)。"""
    blocks = _blocks(md)
    by_key = _index(blocks)
    warnings: list[str] = []

    name = _text(by_key.get(_key("姓名"))).strip() or (fallback_name or "").strip()
    if not name:
        raise TemplateError("模板里没有填写【姓名】（角色名不能为空）。",
                            field="姓名", line=_line_of(by_key, "姓名"))

    personality: dict[str, str] = {}
    for label in ("一句话定位", "性格描述"):
        value = _text(by_key.get(_key(label)))
        if value:
            personality[label] = value

    source = ""
    for label in ("出处/作品", "出处", "作品"):
        source = _text(by_key.get(_key(label)))
        if source:
            break

    weights: dict[str, float] = Weights().model_dump()
    given_weights: set[str] = set()
    for block in blocks:
        weight_field = _weight_field(block.name)
        if weight_field is None:
            continue
        lines = _value_lines(block)
        if not lines:
            continue                             # 留空 → 用默认值（记进 empty_fields）
        weights[weight_field] = _parse_weight(
            lines, field_name=block.name, line=block.line)
        given_weights.add(weight_field)

    decay = CharacterCard.model_fields["emotion_decay_rate"].default
    decay_given = False
    decay_block = by_key.get(_key(_DECAY_LABEL))
    if decay_block is not None:
        lines = _value_lines(decay_block)
        if lines:
            decay = _parse_weight(lines, field_name=_DECAY_LABEL,
                                  line=decay_block.line)
            decay_given = True

    try:
        card = CharacterCard(
            name=name,
            personality=personality,
            abilities=_list(by_key.get(_key("能力"))),
            relationships=_pairs(by_key.get(_key("关系")), field_name="关系",
                                 warnings=warnings),
            knowledge_boundary=_list(by_key.get(_key("已知边界"))),
            weights=Weights(**weights),
            emotion_decay_rate=decay,
            corpus=Corpus(
                source=source,
                style=_text(by_key.get(_key("语言风格"))),
                thinking=_text(by_key.get(_key("思维方式"))),
                quirks=_list(by_key.get(_key("口头禅"))),
                samples=_list(by_key.get(_key("台词样例")))))
    except ValidationError as exc:
        raise _schema_error(exc, "角色卡") from None
    return card, _character_empty_fields(card, given_weights, decay_given), warnings


def _character_empty_fields(card: CharacterCard, given_weights: set[str],
                            decay_given: bool) -> list[str]:
    """角色卡里「没填、已用默认值」的字段（按模板顺序，中文字段名）。"""
    empty: list[str] = []
    if not card.corpus.source:
        empty.append("出处/作品")
    if not card.personality.get("一句话定位"):
        empty.append("一句话定位")
    if not card.personality.get("性格描述"):
        empty.append("性格描述")
    if not card.abilities:
        empty.append("能力")
    if not card.relationships:
        empty.append("关系")
    if not card.knowledge_boundary:
        empty.append("已知边界")
    if not card.corpus.style:
        empty.append("语言风格")
    if not card.corpus.thinking:
        empty.append("思维方式")
    if not card.corpus.quirks:
        empty.append("口头禅")
    if not card.corpus.samples:
        empty.append("台词样例")
    for weight_field, label in _WEIGHT_LABELS:
        if weight_field not in given_weights:
            empty.append(label)
    if not decay_given:
        empty.append(_DECAY_LABEL)
    return empty


def parse_character_template(md: str, *, fallback_name: str = "") -> CharacterCard:
    """角色卡模板 Markdown → CharacterCard（字段缺失用默认值；不合法抛 TemplateError）。"""
    card, _empty, _warnings = _parse_character(md, fallback_name)
    return card


# ------------------------------------------------------------------ 场景卡
_BOUNDARY_TYPES: dict[str, str] = {
    "时间": "time", "time": "time",
    "物理": "physical", "physical": "physical",
    "社会": "social", "social": "social",
    "目标": "goal", "goal": "goal",
    "无": "none", "none": "none", "无边界": "none", "没有": "none", "没有边界": "none",
    "否": "none", "no": "none",
}
_BOUNDARY_EMPTY = frozenset({"", "—", "–", "―", "-", "－", "N/A", "n/a", "NA",
                             "（无）", "(无)", "略"})
_SCENE_PATCH_KEYS = ("name", "description", "background", "plot_direction", "date")

_MUTABLE_TRUE = frozenset({"是", "true", "yes", "y", "1", "可改变", "可改", "能"})
_MUTABLE_FALSE = frozenset({"否", "false", "no", "n", "0", "不可改变", "不可改", "不能"})


def _parse_bool(value: str, *, field_name: str, line: int | None,
                warnings: list[str]) -> tuple[bool, bool]:
    """是/否 文本 → (布尔值, 是否填了)；认不出 → (False, True) + 警告。"""
    token = value.strip().lower()
    if token in _MUTABLE_TRUE:
        return True, True
    if token in _MUTABLE_FALSE:
        return False, True
    warnings.append(f"【{field_name}】只认「是」或「否」，"
                    f"收到 {value.strip()!r}，已按「否」处理。")
    return False, True


def _parse_hhmm(value: str, *, field_name: str,
                line: int | None) -> str:
    """`HH:MM` 文本 → 规范化 `HH:MM`（走引擎的 sceneclock.parse_hhmm 口径）。"""
    try:
        return format_hhmm(parse_hhmm(value.strip()))
    except ValueError as exc:
        raise TemplateError(
            f"要填 24 小时制的 HH:MM（例 21:30），{exc}。",
            field=field_name, line=line) from None


def _parse_scene(md: str, fallback_name: str = "") -> tuple[
        Scene, list[str], list[str]]:
    """场景卡模板 → (场景, 未填字段, 警告)。"""
    blocks = _blocks(md)
    by_key = _index(blocks)
    warnings: list[str] = []

    name = (_text(by_key.get(_key("场景名"))).strip()
            or (fallback_name or "").strip())
    if not name:
        raise TemplateError("模板里没有填写【场景名】（场景名不能为空）。",
                            field="场景名", line=_line_of(by_key, "场景名"))

    # ---- 硬边界 ----
    type_block = by_key.get(_key("边界类型"))
    type_text = _first_line(type_block, drop_placeholders=False)
    boundary: HardBoundary | None = None
    if type_text not in _BOUNDARY_EMPTY:
        boundary_type = _BOUNDARY_TYPES.get(_normalize_token(type_text))
        if boundary_type is None:
            raise TemplateError(
                "【边界类型】只能填「时间 / 物理 / 社会 / 目标 / 无」之一，"
                f"收到 {type_text!r}。", field="边界类型",
                line=type_block.line if type_block else None)
        value = _first_line(by_key.get(_key("边界值")))
        desc = _text(by_key.get(_key("边界描述")))
        if boundary_type == "none":
            value, desc = "", ""                 # 「无」= 不设边界：值与描述一并清空
        elif boundary_type == "time":
            if not value:
                raise TemplateError(
                    "【边界类型】填了「时间」时，【边界值】必须填 HH:MM（例 22:00）。",
                    field="边界值", line=_line_of(by_key, "边界值"))
            value = _parse_hhmm(value, field_name="边界值",
                                line=_line_of(by_key, "边界值"))
        boundary = HardBoundary(type=boundary_type, value=value, desc=desc)

    # ---- 开始时间（留空 → 场景默认 21:30 开场）----
    start_raw = _first_line(by_key.get(_key("开始时间")))
    start_given = bool(start_raw)
    start_time = (_parse_hhmm(start_raw, field_name="开始时间",
                              line=_line_of(by_key, "开始时间"))
                  if start_given else Scene.model_fields["start_time"].default)

    # ---- 描述可改变 ----
    mutable_raw = _first_line(by_key.get(_key("描述可改变")))
    mutable_given = bool(mutable_raw)
    if mutable_given:
        description_mutable, _given = _parse_bool(
            mutable_raw, field_name="描述可改变",
            line=_line_of(by_key, "描述可改变"), warnings=warnings)
    else:
        description_mutable = Scene.model_fields["description_mutable"].default

    # ---- 出场角色（去重保序）----
    characters: list[SceneCastMember] = []
    seen: set[str] = set()
    for cast_name in _list(by_key.get(_key("出场角色"))):
        if cast_name in seen:
            continue
        seen.add(cast_name)
        characters.append(SceneCastMember(name=cast_name))

    hooks = _parse_hooks(blocks, warnings)

    try:
        scene = Scene(
            name=name,
            date=_text(by_key.get(_key("日期"))),
            background=_text(by_key.get(_key("背景信息"))),
            description=_text(by_key.get(_key("场景描述"))),
            description_mutable=description_mutable,
            plot_direction=_text(by_key.get(_key("剧情设定"))),
            hooks=hooks,
            characters=characters,
            hard_boundary=boundary,
            start_time=start_time)
    except ValidationError as exc:
        raise _schema_error(exc, "场景卡") from None
    empty = _scene_empty_fields(scene, start_given, mutable_given)
    return scene, empty, warnings


def _normalize_token(text: str) -> str:
    """边界类型等枚举值归一：去空白、去 markdown、去包裹的引号/括号、转小写。"""
    token = _strip_md(text).strip().lower()
    token = token.strip("「」『』\"'`（）()")
    return re.sub(r"\s+", "", token)


def _scene_empty_fields(scene: Scene, start_given: bool,
                        mutable_given: bool) -> list[str]:
    """场景卡里「没填、已用默认值」的字段（按模板顺序，中文字段名）。"""
    empty: list[str] = []
    if not scene.date:
        empty.append("日期")
    if not start_given:
        empty.append("开始时间")
    if scene.hard_boundary is None:
        empty.append("边界类型")
    elif scene.hard_boundary.type != "none":
        if not scene.hard_boundary.value:
            empty.append("边界值")
        if not scene.hard_boundary.desc:
            empty.append("边界描述")
    if not scene.background:
        empty.append("背景信息")
    if not scene.description:
        empty.append("场景描述")
    if not mutable_given:
        empty.append("描述可改变")
    if not scene.plot_direction:
        empty.append("剧情设定")
    if not scene.characters:
        empty.append("出场角色")
    if not scene.hooks:
        empty.append("触发器 hooks")
    return empty


def parse_scene_template(md: str, *, fallback_name: str = "") -> Scene:
    """场景卡模板 Markdown → Scene（字段缺失用默认值；不合法抛 TemplateError）。"""
    scene, _empty, _warnings = _parse_scene(md, fallback_name)
    return scene


# ------------------------------------------------------------------ hooks
_HOOK_LABEL_RE = re.compile(r"^hook(?P<idx>.*?)(?P<part>条件|事件类型|事件内容|显隐)$")
_HOOK_PARTS = ("条件", "事件类型", "事件内容", "显隐")
_KIND_MAP = {"上下文": "context", "context": "context",
             "角色": "character", "character": "character",
             "场景": "scene", "scene": "scene"}
_VISIBLE_TRUE = frozenset({"显式", "显性", "可见", "true", "yes", "是", "visible", "显"})
_VISIBLE_FALSE = frozenset({"隐式", "隐性", "不可见", "false", "no", "否", "hidden", "隐"})
_ACTION_RES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^禁言\s*(\d+)\s*(?:回合|轮|次)?$"), "mute_turns"),
    (re.compile(r"^(?:mute_turns|mute)\s*(\d+)$"), "mute_turns"),
    (re.compile(r"^(?:禁言|静默|mute|muted|mute_turns)$"), "mute"),
    (re.compile(r"^(?:解禁|解除禁言|解除静默|unmute|unmuted)$"), "unmute"),
    (re.compile(r"^(?:加入|入场|进入|add|join)$"), "add"),
    (re.compile(r"^(?:离开|离场|退出|remove|leave)$"), "remove"),
)


@dataclass
class _HookGroup:
    """一条 hook 的各字段块：`token` 是 `hook` 后面那段（`1` / 空 / 自定的 id）。"""
    token: str
    line: int
    parts: dict[str, _Block]


def _hook_groups(blocks: list[_Block]) -> list[_HookGroup]:
    """按出现顺序把 `【hook N xxx】` 分组（缺编号/断号都容错：以「条件」开新组）。"""
    groups: list[_HookGroup] = []
    cur: _HookGroup | None = None
    for block in blocks:
        m = _HOOK_LABEL_RE.match(_key(block.name))
        if m is None:
            continue
        part = m.group("part")
        token = m.group("idx").strip()
        if (cur is None or part == "条件" or part in cur.parts
                or (token and cur.token and token != cur.token)):
            cur = _HookGroup(token=token, line=block.line, parts={})
            groups.append(cur)
        cur.parts[part] = block
    return groups


def _hook_id(token: str, position: int) -> str:
    """组编号 → hook id：数字/缺省 → h1、h2…；非数字 → 原样当 id（随后照 hooks.validate 校验）。"""
    return f"h{token}" if token.isdigit() else (token or f"h{position}")


def _parse_hooks(blocks: list[_Block], warnings: list[str]) -> list[Hook]:
    """模板里的 hook 组 → Hook 列表（逐条过 hooks.validate；不合法抛 TemplateError）。"""
    hooks: list[Hook] = []
    lines: list[int] = []           # 第 i 条 hook 的组行号（报错时告诉用户改哪儿）
    for group in _hook_groups(blocks):
        parts = group.parts
        if not any(_value_lines(parts.get(p)) for p in _HOOK_PARTS):
            continue                               # 整组是提示/留空 → 不是一条 hook
        position = len(hooks) + 1
        # 编号是数字/缺省 → h1、h2…；写成别的（`【hook 开门 条件】`）= 用户自定的 id，
        # 原样用，交给下面的 hooks.validate 判合法性（带这一组的行号）。
        hook_id = _hook_id(group.token, position)
        lines.append(group.line)

        condition = _text(parts.get("条件"))
        kind_text = _first_line(parts.get("事件类型"))
        kind = _KIND_MAP.get(_normalize_token(kind_text)) if kind_text else None
        if kind is None:
            detail = (f"必须是「上下文 / 角色 / 场景」之一，收到 {kind_text!r}。"
                      if kind_text else "没有填写（上下文 / 角色 / 场景）。")
            raise TemplateError(f"【hook {position} 事件类型】{detail}",
                                field=f"hook {position} 事件类型",
                                line=parts["事件类型"].line if "事件类型" in parts
                                else group.line)

        visible_block = parts.get("显隐")
        visible_text = _first_line(visible_block)
        if not visible_text:
            visible = True
        elif _normalize_token(visible_text) in _VISIBLE_TRUE:
            visible = True
        elif _normalize_token(visible_text) in _VISIBLE_FALSE:
            visible = False
        else:
            warnings.append(
                f"【hook {position} 显隐】只认「显式」或「隐式」，"
                f"收到 {visible_text!r}，已按「显式」处理。")
            visible = True

        content_block = parts.get("事件内容")
        content = _text(content_block)
        content_line = content_block.line if content_block else group.line
        if kind == "character":
            # 一条 hook 里写了多个「角色名：动作」→ 展开成多条独立 hook（条件/显隐相同，
            # id 依次为 hN、hN-2、hN-3…）。多写几个动作是很自然的写法，不该报错。
            actions = _parse_character_events(content, position=position,
                                              line=content_line)
            for i, (char_name, action, turns) in enumerate(actions):
                hid = hook_id if i == 0 else f"{hook_id}-{i + 1}"
                hooks.append(Hook(id=hid, condition=condition, event_kind=kind,
                                  visible=visible, character_name=char_name,
                                  action=action, turns=turns))
                if i:
                    lines.append(content_line)   # 首条的行号已在本组开头追加过
            continue
        hook = Hook(id=hook_id, condition=condition, event_kind=kind,
                    visible=visible)
        if kind == "context":
            hook.context_text = content
        else:
            hook.scene_patch = _parse_scene_event(
                content, position=position, warnings=warnings)
        hooks.append(hook)

    for position, hook in enumerate(hooks, start=1):
        # id 合法性/唯一性、条件非空、事件载荷齐不齐——全交给 hooks.validate 这一处口径
        errors = validate_hook(hook, siblings=[h for h in hooks if h is not hook])
        if errors:
            raise TemplateError("；".join(errors), field=f"hook {position}",
                                line=lines[position - 1])
    return hooks


def _parse_character_events(content: str, *, position: int,
                            line: int | None) -> list[tuple[str, str, int]]:
    """`角色名：动作`（可写多个）→ [(角色名, 动作, 轮数), …]。

    一条 hook 里写多个动作是自然写法（"甲：离开；乙：离开"），故按分号/换行切段、
    逐段解析，各段由调用方**展开成一条独立 hook**。段内动作也可换行写（`丙：\\n离开`）。
    """
    # 分号与换行都当分隔（两者都是"我又要写下一个动作了"的自然写法）；`丙：\n离开`
    # 这种"动作另起一行"由下面的 pending_name 逻辑接住，不会被误拆。
    segments = [seg for seg in re.split(r"[；;\n]", content or "") if seg.strip()]
    if not segments:
        raise TemplateError(
            "角色事件要写成「角色名：动作」（动作取 加入 / 离开 / 禁言N回合 / "
            "禁言 / 解禁），当前为空。",
            field=f"hook {position} 事件内容", line=line)
    out: list[tuple[str, str, int]] = []
    pending_name = ""            # 上一段只写了名字（`丙：`）→ 下一段是它的动作
    for seg in segments:
        if pending_name:
            name, action_text = pending_name, seg      # 这一段就是上一段的动作
        else:
            if not re.search(r"[：:]", seg):            # 连「角色名：」都没有 → 写法不对
                raise TemplateError(
                    "角色事件要写成「角色名：动作」（动作取 加入 / 离开 / 禁言N回合 / "
                    f"禁言 / 解禁），收到 {seg.strip()!r}。",
                    field=f"hook {position} 事件内容", line=line)
            name, action_text = _split_pair(seg)
        if not name:
            raise TemplateError(
                "角色事件要写成「角色名：动作」（动作取 加入 / 离开 / 禁言N回合 / "
                f"禁言 / 解禁），收到 {seg.strip()!r}。",
                field=f"hook {position} 事件内容", line=line)
        matched: tuple[str, str, int] | None = None
        # 动作可能在下一行（`丙：\n离开`），也可能后面又跟了一句说明——两种都试。
        for candidate in (action_text.split("\n")[0], action_text):
            token = _normalize_token(candidate)
            for pattern, action in _ACTION_RES:
                m = pattern.match(token)
                if m:
                    turns = int(m.group(1)) if (m.groups() and m.group(1)) else 0
                    matched = (name, action, turns)
                    break
            if matched:
                break
        if matched:
            out.append(matched)
            pending_name = ""
        elif not action_text.strip():
            pending_name = name           # 动作留到下一段/下一行
        else:
            raise TemplateError(
                "角色事件的动作只认 加入 / 离开 / 禁言N回合 / 禁言 / 解禁（也接受 "
                f"add/remove/mute_turns/mute/unmute），收到 {name}：{action_text.strip()!r}。",
                field=f"hook {position} 事件内容", line=line)
    if pending_name:
        raise TemplateError(
            f"「{pending_name}」只写了角色名、没写动作（加入 / 离开 / 禁言N回合 / "
            "禁言 / 解禁）。",
            field=f"hook {position} 事件内容", line=line)
    return out


def _clean_lines(text: str) -> list[str]:
    """任意多行文本 → 有效值行（与字段块同一套「丢空行/提示/占位」口径）。"""
    return _value_lines(_Block(name="", line=0, raw=(text or "").split("\n")))


def _parse_scene_event(content: str, *, position: int,
                       warnings: list[str]) -> dict:
    """`键：值` 多行 → 场景补丁（未知键警告后丢弃）。"""
    patch: dict[str, str] = {}
    for item in _clean_lines(content):
        key, value = _split_pair(_strip_bullet(item))
        if not key:
            continue
        if key not in _SCENE_PATCH_KEYS:
            warnings.append(
                f"【hook {position} 事件内容】里的场景字段「{key}」不认识，已忽略"
                f"（可用：{' / '.join(_SCENE_PATCH_KEYS)}）。")
            continue
        patch[key] = value
    return patch


# ------------------------------------------------------------------ 信息库
#: 条目字段的五个部分（顺序即模板里的一组）。
_ENTRY_PARTS = ("名称", "摘要", "正文", "主题", "进索引")
#: 库级字段（正文区域在它们处收尾）。
_LIBRARY_META_KEYS = frozenset(_key(name) for name in ("库名", "类型", "归属角色", "订阅"))
#: 【类型】的取值（归一后比对）：广域库 = 全员共享的设定，角色库 = 归某个角色。
_LIBRARY_TYPES: dict[str, str] = {
    "广域库": "wide", "广域": "wide", "wide": "wide", "世界观库": "wide", "设定库": "wide",
    "角色库": "character", "角色": "character", "character": "character", "个人库": "character",
}
#: 库名/归属角色两边的书名号或引号（`《庆国世界观》` → `庆国世界观`）。
_NAME_WRAPPERS = {"《": "》", "〈": "〉", "「": "」", "『": "』", "\"": "\"", "'": "'"}
#: 【条目 N 进索引】的取值（认不出只警告，并保持"进索引"这一默认）。
_INDEX_TRUE = frozenset({"是", "true", "yes", "y", "1", "进索引", "进", "收录"})
_INDEX_FALSE = frozenset({"否", "false", "no", "n", "0", "不进索引", "不进", "不收录",
                          "隐藏"})


@dataclass
class LibraryTemplate:
    """一份填好的信息库模板 → 中间结构：库元信息 + 条目 + 订阅（**落盘之前**的东西）。

    落盘不在这里做：写盘一律交给 `knowledgestore.save_library`（条目文件的唯一真相源、
    原子写、顺序归一都在那一处，别另写一套写盘）。

    `name` 与 `owner` 是**两件事**：`name` 是显示名（索引里渲染成 `▾ 借自《庆国世界观》`），
    而库目录那一段对角色库而言是**归属角色**——引擎按角色名换算本体库位置
    （`library_dir("character", 角色名, root=…)`），目录段写错等于这个角色的库凭空消失。
    """
    name: str
    scope: str                     # "wide" | "character"
    owner: str = ""
    entries: list[Entry] = field(default_factory=list)
    subscriptions: list[str] = field(default_factory=list)

    def dir_key(self) -> str:
        """库目录的那一段名字：角色库用归属角色，广域库用库名。"""
        return self.owner if self.scope == "character" else self.name

    def to_library(self) -> Library:
        """中间结构 → `knowledgestore.Library`（`save_library` 的入参）。"""
        lib = Library(name=self.name, scope=self.scope, owner=self.owner)
        for entry in self.entries:
            lib.upsert(entry)
        return lib


@dataclass
class _EntryGroup:
    """一条条目的各字段块：`token` 是 `条目` 后面那段编号（`1` / 空 / 乱写的都收）。"""
    token: str
    line: int
    parts: dict[str, _Block]


def _entry_groups(blocks: list[_Block]) -> list[_EntryGroup]:
    """按出现顺序把 `【条目 N xxx】` 分组（缺编号/断号都容错：以「名称」开新组）。

    照抄 `_hook_groups` 那套正则 + token + 组内 parts 字典的写法。**绝不能用 `_index`**：
    它 `setdefault` 去重同名块，而一份信息库模板有 N 个条目、条目内字段重名（复制骨架
    忘了改编号时甚至一字不差），第二个条目起会被**静默吞掉**——不报错、不警告，用户只会
    发现"我记的东西少了一条"。
    """
    groups: list[_EntryGroup] = []
    cur: _EntryGroup | None = None
    for block in blocks:
        m = _ENTRY_LABEL_RE.match(_key(block.name))
        if m is None:
            continue
        part = m.group("part")
        token = m.group("idx").strip()
        if (cur is None or part == "名称" or part in cur.parts
                or (token and cur.token and token != cur.token)):
            cur = _EntryGroup(token=token, line=block.line, parts={})
            groups.append(cur)
        cur.parts[part] = block
    return groups


def _body_text(block: _Block | None) -> str:
    """条目正文 → 多行文本：**原样保留**（不剥项目符号、不剥 markdown、保留段间空行）。

    与其它字段不同：正文是写给角色看的自由 Markdown，`- ` 列表、`**加粗**`、`[[链接]]`
    都是内容本身，动了就是改用户的记忆。只丢两类行——模板自带的括号提示（原样未填的
    骨架不该变成一条奇怪的正文）与整行占位（`（无）` / `—`），并抹掉首尾空行。

    传进来的块已经由 `_extended_body` 把值行补全到下一个信息库字段（见那里的说明）；
    本函数只管过滤，不管边界。
    """
    if block is None:
        return ""
    lines = [line.rstrip() for line in block.raw]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    out: list[str] = []
    for line in lines:
        text = line.strip()
        if not text:
            out.append("")
            continue
        if _is_hint_paren(_strip_md(text)) or _is_placeholder(text):
            continue
        out.append(line)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)


def _md_lines(md: str) -> list[str]:
    """Markdown → 行列表（换行归一化与 `_blocks` 同一口径——两处必须一致，故照抄那一行）。"""
    return (md or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _ends_library_body(line: str) -> bool:
    """这一行是不是**信息库字段行**（正文区域的终点）。

    `条目…` 开头的一律算：`【条目 1 主题】`、下一条的 `【条目 2 名称】`、以及写错字的
    `【条目 1 备注】`——最后这个要留给"不认识的条目字段"那条警告，不能被正文吞掉。
    库级字段（库名/类型/归属角色/订阅）同理。**别的 `【…】：` 形状的行不算终点**：
    正文是自由 Markdown，角色自己在正文里写一行 `【注意】：…` 是内容，不是字段。
    """
    m = _LABEL_RE.match(line)
    if m is None:
        return False
    key = _key(m.group("label"))
    return key in _LIBRARY_META_KEYS or key.startswith("条目")


def _extended_body(md_lines: list[str], block: _Block) -> _Block:
    """正文块 → 值行补全到下一个信息库字段行的正文块（其余部分原样）。

    为什么不像别的字段那样在标题/分隔线处收尾（`_blocks` 的 `_stop`）：那套规则是为
    角色卡/场景卡的**短句**字段定的——`## 二、性格与能力` 是另一节的开头。正文不一样：
    它是条目的**唯一真相源**、是自由 Markdown，`---`、`## 小标题`、`【…】：` 都可能是
    用户真要角色记住的内容。在那里收尾等于**静默丢掉半段记忆**（不报错、不警告，报告
    照样打「√ 信息库」），而这恰恰是本模块处处在规避的事。故正文一路吃到下一个字段行。

    只改信息库这一条取值路径：`_blocks`（角色卡/场景卡共用的那套切块）一个字未动。
    """
    if not 0 <= block.line - 1 < len(md_lines):
        return block
    head = _LABEL_RE.match(md_lines[block.line - 1])
    if head is None:                  # 理论上到不了：块就是这么切出来的
        return block
    rest = head.group("rest")
    raw = [rest] if rest.strip() else []
    i = block.line
    while i < len(md_lines) and not _ends_library_body(md_lines[i]):
        raw.append(md_lines[i])
        i += 1
    return _Block(name=block.name, line=block.line, raw=raw)


def _parse_indexed(block: _Block | None, *, ordinal: str,
                   warnings: list[str]) -> bool:
    """【条目 N 进索引】→ bool（认不出只警告，并按默认的「是」处理）。"""
    text = _first_line(block)
    if not text:
        return True
    token = _normalize_token(text)
    if token in _INDEX_TRUE:
        return True
    if token in _INDEX_FALSE:
        return False
    warnings.append(f"【条目 {ordinal} 进索引】只认「是」或「否」，"
                    f"收到 {text!r}，已按「是」（进索引）处理。")
    return True


def _clean_library_name(text: str) -> str:
    """库名/归属角色 → 去掉首尾空白与包裹的书名号（`《庆国世界观》` → `庆国世界观`）。

    书名号是**渲染体例**不是名字的一部分：索引里那一段渲染成 `▾ 借自《库名》`，名字自己
    再带一层就成了《《…》》。除书名号外，中英文引号同理（模型爱把库名写进引号里）。
    """
    name = (text or "").strip()
    while len(name) >= 2 and _NAME_WRAPPERS.get(name[0]) == name[-1]:
        name = name[1:-1].strip()
    return name


def _required_line(by_key: dict[str, _Block], blocks: list[_Block], label: str,
                   *fallbacks: str) -> int | None:
    """必需字段出错时的定位行：先看它自己的块，再退到同节的其它必需字段，最后退到首个块。

    行号是给用户看的（CLI 打「第 N 行附近」），所以"字段整块没写"时也不能没有——退到的
    位置至少落在同一节里，用户照着找过去就能补上。
    """
    line = _line_of(by_key, label)
    if line is not None:
        return line
    for other in fallbacks:
        line = _line_of(by_key, other)
        if line is not None:
            return line
    return blocks[0].line if blocks else None


def _parse_library(md: str, fallback_name: str = "") -> tuple[
        LibraryTemplate, list[str], list[str]]:
    """信息库模板 → (中间结构, 未填字段, 警告)。

    容错口径与角色卡/场景卡一致（「宽进严值」）：缺编号、断号、不认识的条目字段、认不出的
    「进索引」只警告并继续；**语义值不合法当场抛**（库名为空、类型不认识、角色库缺归属角色、
    条目名为空或过不了键守门、条目名重复、一条条目都没有），带中文字段名与行号。
    """
    blocks = _blocks(md)
    by_key = _index(blocks)
    warnings: list[str] = []

    # ---- 库名（必填；缺就给 fallback_name，与角色卡/场景卡同一口径）----
    name_line = _required_line(by_key, blocks, "库名", "类型", "归属角色")
    name = _clean_library_name(_first_line(by_key.get(_key("库名"))))
    if not name:
        name = _clean_library_name(fallback_name or "")
    if not name:
        raise TemplateError("模板里没有填写【库名】（库名不能为空）。",
                            field="库名", line=name_line)

    # ---- 类型（必填，二选一）----
    type_block = by_key.get(_key("类型"))
    type_text = _first_line(type_block, drop_placeholders=False)
    scope = ""
    if type_text and type_text not in _BOUNDARY_EMPTY:
        scope = _LIBRARY_TYPES.get(_normalize_token(type_text), "")
    if not scope:
        detail = (f"只能填「广域库」或「角色库」，收到 {type_text!r}。"
                  if type_text and type_text not in _BOUNDARY_EMPTY
                  else "没有填写（填「广域库」或「角色库」）。")
        raise TemplateError(f"【类型】{detail}", field="类型",
                            line=_required_line(by_key, blocks, "类型", "库名", "归属角色"))

    # ---- 归属角色：角色库必填（它是库目录那一段，也是引擎找库的键）----
    owner_line = _required_line(by_key, blocks, "归属角色", "类型", "库名")
    owner_text = _clean_library_name(_first_line(by_key.get(_key("归属角色"))))
    owner = ""
    if scope == "character":
        if not owner_text:
            raise TemplateError(
                "【类型】填了「角色库」时，【归属角色】必须填（写角色卡里已有的姓名）。",
                field="归属角色", line=owner_line)
        err = entry_key_error(owner_text)
        if err:
            raise TemplateError(f"归属角色「{owner_text}」不能当库目录名：{err}",
                                field="归属角色", line=owner_line)
        owner = owner_text
    elif owner_text:
        warnings.append(f"广域库没有归属角色，【归属角色】里的「{owner_text}」已忽略。")

    dir_key = owner if scope == "character" else name
    err = entry_key_error(dir_key)
    if err:
        raise TemplateError(f"库名「{name}」不能当库目录名：{err}", field="库名",
                            line=name_line)

    # ---- 订阅（可选，一行一个库名）----
    subscriptions: list[str] = []
    for item in _list(by_key.get(_key("订阅"))):
        sub = _clean_library_name(item)
        if not sub:
            continue
        if sub == name:
            warnings.append(f"【订阅】里写了本库自己的名字「{sub}」，已忽略。")
            continue
        if sub in subscriptions:
            continue
        subscriptions.append(sub)
    if subscriptions:                  # 本期只解析与报告，落盘/生效在订阅那一侧（§3.4）
        warnings.append("【订阅】已解析：" + "、".join(subscriptions)
                        + "；订阅的实际生效由角色编辑器/引擎那一侧配置（本次只解析）。")

    # ---- 条目 ----
    entries: list[Entry] = []
    empty: list[str] = []
    taken: dict[str, str] = {}         # 条目名 → 第几条（重名报错时告诉用户撞在哪）
    md_lines = _md_lines(md)
    for position, group in enumerate(_entry_groups(blocks), start=1):
        parts = dict(group.parts)
        if "正文" in parts:            # 正文：值行补全到下一个信息库字段（见 `_extended_body`）
            parts["正文"] = _extended_body(md_lines, parts["正文"])
        if not any(_value_lines(parts.get(part)) for part in _ENTRY_PARTS):
            continue                   # 整组是提示/留空 → 不是一条条目
        # 编号优先用**用户自己写的那一个**（`【条目 5 名称】` 报错就该报 5），写乱了才退回序号。
        ordinal = group.token if group.token.isdigit() else str(position)
        label = f"条目 {ordinal} 名称"
        entry_line = parts["名称"].line if "名称" in parts else group.line
        entry_name = _first_line(parts.get("名称"))
        if not entry_name:
            raise TemplateError(
                f"【{label}】不能为空（条目名会成为条目的键，也是文件名）。",
                field=label, line=entry_line)
        err = entry_key_error(entry_name)
        if err:
            raise TemplateError(
                f"条目名「{entry_name}」不能当条目的键（它会成为文件名）：{err}"
                "导入器不会替你改名，请换一个安全的名称。",
                field=label, line=entry_line)
        if entry_name in taken:
            raise TemplateError(
                f"条目名「{entry_name}」重复（第 {taken[entry_name]} 条已经用过这个名字）；"
                "同一座库里名称就是条目的键，不能重复。", field=label, line=entry_line)
        for existing in taken:
            clash = entry_key_collision_error(entry_name, existing)
            if clash:
                raise TemplateError(clash, field=label, line=entry_line)
        taken[entry_name] = ordinal

        summary = _text(parts.get("摘要"))
        body = _body_text(parts.get("正文"))
        subject = _first_line(parts.get("主题"))
        indexed = _parse_indexed(parts.get("进索引"), ordinal=ordinal,
                                 warnings=warnings)
        try:
            entry = Entry(key=entry_name, title=entry_name, summary=summary,
                          body=body, subject=subject, indexed=indexed,
                          origin=EntryOrigin(kind="import", turn=BASELINE_TURN))
        except ValueError as exc:      # 兜底：键已单独守过门，这里只防没预料到的字段值
            raise TemplateError(f"【{label}】的字段值不符合要求：{exc}",
                                field=label, line=entry_line) from None
        entries.append(entry)
        if not summary:
            empty.append(f"条目 {ordinal} 摘要")
        if not body:
            empty.append(f"条目 {ordinal} 正文")

    for block in blocks:               # 结构噪声：`【条目 1 备注】` 这类只警告，但绝不静默
        key = _key(block.name)
        if key.startswith("条目") and not _ENTRY_LABEL_RE.match(key):
            warnings.append(f"【{block.name.strip()}】不是信息库模板认识的条目字段，已忽略"
                            f"（可用：{' / '.join(_ENTRY_PARTS)}）。")

    if not entries:
        raise TemplateError(
            "模板里一条条目都没有（至少要填一条【条目 1 名称】）。", field="条目 1 名称",
            line=blocks[0].line if blocks else None)
    if not subscriptions:
        empty.insert(0, "订阅")
    return (LibraryTemplate(name=name, scope=scope, owner=owner, entries=entries,
                            subscriptions=subscriptions), empty, warnings)


def parse_library_template(md: str, *, fallback_name: str = "") -> LibraryTemplate:
    """信息库模板 Markdown → LibraryTemplate（字段缺失用默认值；不合法抛 TemplateError）。"""
    tpl, _empty, _warnings = _parse_library(md, fallback_name)
    return tpl


# ------------------------------------------------------------------ 落盘
#: Windows 保留设备名：CON.json 之类建不出来（与 loaders 同一份名单）。
_RESERVED_STEMS = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)] + [f"LPT{i}" for i in range(1, 10)])


def safe_filename(name: str) -> str:
    """场景名 → 可安全落盘的文件名主干（不带 .json）：非法字符换成 `_`、截断、兜底。"""
    stem = re.sub(r'[\\/:*?"<>|]', "_", (name or "").strip())
    stem = "".join("_" if (ord(c) < 32 or ord(c) == 127) else c for c in stem)
    stem = stem.strip().strip(".").strip()
    stem = stem[:60].strip()
    if not stem:
        return "场景"
    if stem.split(".")[0].upper() in _RESERVED_STEMS:
        stem = f"{stem}_"
    return stem


def _library_path_conflict(root: Path, target: Path) -> str:
    """信息库落盘路径上的**非目录占位** → 中文原因（空串 = 没有冲突）。

    库是**目录**（`library.json` + `entries/`），而 `save_library` 的写盘从
    `entries.mkdir(parents=True, exist_ok=True)` 开始：路径上任何一段被一个同名文件占着
    （手工放错文件、上一次写入中断），它就抛 `FileExistsError`（WinError 183）一路冒到
    用户面前——CLI 只认 `TemplateError`，用户看到的就是一段英文 traceback。故在这里先把
    话说清楚（与别处同一口径：绝不把 traceback 丢给用户），加不加 `--force` 都不放行
    （写不进去就是写不进去，硬试只会再炸一次）。
    """
    for label, path, want_dir in (("信息库根目录", root, True),
                                  ("目标库", target, True),
                                  ("目标库的 entries/", entries_dir(target), True),
                                  ("目标库的 library.json", library_json_path(target), False)):
        if path.exists() and path.is_dir() != want_dir:
            kind = "目录" if path.is_dir() else "文件"   # 占着那一位的东西**是什么**
            return (f"{label}的位置被一个同名{kind}占着：{path}。"
                    "请先把它移开或删掉再导入。")
    return ""


def _existing_entry_clash(existing: Library, incoming: list[Entry]) -> str:
    """模板里的条目名与**库里已有**的条目名只差大小写 → 中文原因（空串 = 没有撞车）。

    模板内部两个条目名撞车在 `_parse_library` 里已经当场报错
    （`entry_key_collision_error`），但那只看得见模板自己。库里**没被模板列出**的手写条目
    在报告里的口径是"已原样保留"，而 `ABC.md` 与 `abc.md` 在 NTFS（与默认的 macOS 卷）上
    就是**同一个文件**：`save_library` 会把模板正文直接写进去，用户手写的那条被静默顶掉，
    报告却恰恰写着"已原样保留"。同一份判定口径必须也管住"模板 vs 磁盘"这一侧。
    """
    for entry in incoming:
        for key in existing.entries:
            clash = entry_key_collision_error(entry.key, key)
            if clash:
                return (f"{clash}请把模板里的「{entry.key}」改个名字，"
                        f"或先在库里处理掉那条「{key}」再覆盖导入。")
    return ""


def import_template_file(path: Path, *, characters_dir: Path, scenes_dir: Path,
                         libraries_dir: Path | None = None,
                         filename: str | None = None, dry_run: bool = False,
                         overwrite: bool = False) -> ImportResult:
    """把一个填好的模板文件解析并写进素材库（角色卡/场景卡/信息库由内容自动判定）。

    · 角色卡落到 `characters_dir/<姓名>.json`（卡名即文件名）；场景卡落到
      `scenes_dir/<filename>.json`，`filename` 缺省由场景名折成安全 slug；
    · 信息库落进 `libraries_dir/<characters|wide>/<目录段>/`——
      角色库的目录段是**归属角色**（引擎按角色名找库），广域库的目录段是库名；写盘一律
      走 `knowledgestore.save_library`（条目文件的唯一真相源 + 原子写都在那一处）。
      `libraries_dir` 缺省取 `characters_dir` 的兄弟目录 `characters_dir.parent/"libraries"`
      ——正式布局下就是 `app/libraries/`，与 `app/characters/` 同级（§13.3）。
    · 文件名一律过 `loaders.scene_filename_error` 守门（绝不把文件写出库目录之外）。
    · 目标已存在且没给 `overwrite=True` → 抛 TemplateError（不静默覆盖用户素材）。
      信息库的"目标已存在"判据是库目录里已经有东西（`library.json` 或 `entries/`）；
      另有两条硬拦（都抛 TemplateError，中文说明）：**落盘路径被同名文件/目录占着**
      （`save_library` 会在 `mkdir` 处抛 WinError 183），以及**模板里的条目名与库里
      没被列出的既有条目只差大小写**（同一个文件，覆盖等于抹掉人家手写的那条，
      而报告还写着"已原样保留"）。
    · `dry_run=True`：只解析与校验，**一个字节都不写**，`path=None`。
    """
    source = Path(path)
    try:
        md = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise TemplateError(f"读不出模板文件：{exc}") from None

    kind = detect_kind(md)
    empty: list[str] = []
    warnings: list[str] = []
    if kind == "character":
        card, empty, warnings = _parse_character(md, source.stem)
        name = card.name
        target_name = f"{name}.json"
        error = scene_filename_error(target_name)
        if error:
            raise TemplateError(f"角色名「{name}」不能直接当文件名：{error}",
                                field="姓名")
        target = Path(characters_dir) / target_name
        payload: object = card
    elif kind == "scene":
        scene, empty, warnings = _parse_scene(md, source.stem)
        name = scene.name
        target_name = (filename or "").strip() or f"{safe_filename(name)}.json"
        error = scene_filename_error(target_name)
        if error:
            raise TemplateError(f"场景文件名不可用：{error}", field="文件名")
        target = scene_path_for(Path(scenes_dir), target_name)
        payload = scene
    else:
        library, empty, warnings = _parse_library(md, source.stem)
        name = library.name
        root = (Path(libraries_dir) if libraries_dir is not None
                else Path(characters_dir).parent / "libraries")
        target = library_dir(library.scope, library.dir_key(), root=root)
        payload = library

    written: Path | None = None
    if not dry_run:
        if kind == "library":                    # 库：目标是目录，不是一个文件
            problem = _library_path_conflict(root, target)
            if problem:                          # 路径被同名文件/目录占着：说人话，不抛 WinError
                raise TemplateError(problem)
            if (has_library(target) or entries_dir(target).is_dir()) and not overwrite:
                raise TemplateError(
                    f"目标库已存在：{target}。要覆盖请加 --force"
                    f"（或 import_template_file(..., overwrite=True)）。")
            existing = load_library(target).library
            clash = _existing_entry_clash(existing, library.entries)
            if clash:        # 大小写撞名：同一个文件，覆盖等于静默抹掉用户手写的那条
                raise TemplateError(clash)
            keep = [k for k in existing.ordered_keys()
                    if k not in {e.key for e in library.entries}]
            if keep:                 # 覆盖只重写模板列出的条目，别的手写条目留着并说明
                warnings.append("库里已有的条目未在模板中列出，已原样保留：" + "、".join(keep))
            written = save_library(library.to_library(), target)
        else:
            if target.exists() and not overwrite:
                raise TemplateError(
                    f"目标文件已存在：{target}。要覆盖请加 --force"
                    f"（或 import_template_file(..., overwrite=True)）。")
            written = (save_character_card(payload, target) if kind == "character"
                       else save_scene(payload, target))
    return ImportResult(path=written, kind=kind, name=name, card_or_scene=payload,
                        empty_fields=empty, warnings=warnings)


def _schema_error(exc: ValidationError, what: str) -> TemplateError:
    """pydantic 校验失败 → 中文 TemplateError（绝不把 traceback 丢给用户）。"""
    parts: list[str] = []
    for err in exc.errors():
        where = ".".join(str(p) for p in err.get("loc", ())) or "?"
        parts.append(f"{where}：{err.get('msg', '')}")
    return TemplateError(f"{what}的字段值不符合要求：{'；'.join(parts)}")
