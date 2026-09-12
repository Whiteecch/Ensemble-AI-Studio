"""knowledge.py 纯内核的测试（设计文档 §3 数据模型 / §4.2 条目格式 / §6.1 索引注入 / §6.2 反链）。

钉住四类行为，每一条都对应一个会真实出错的地方：
  · 条目 Markdown 的往返一致与坏输入的当场报错——`.md` 是条目的**唯一真相源**，解析
    若半途而废（返回半个对象）就会把正文静默丢掉，用户手写的条目一夜之间变空；
    往返一致还要求渲染对**任意**值成立：值里带换行（多行摘要）、带逗号方括号问号
    （流上下文里的语法字符）时写出来的东西必须还能读回来，否则条目是"写得出、读不回"；
  · 键的文件名守门——键直接变成文件名，放过一个 `../` 就等于让写盘跑出库目录；
  · 正文链接解析与反向链接——图只能单向走的话，角色会漏掉半边（§6.2）；
  · 索引表渲染——**无条目必须返回逐字节空串**，这是「无库角色系统提示逐字节不变」
    这条硬验收的落点（§6.1）。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from harness.knowledge import (
    INDEX_HINT, MAX_KEY_LENGTH, SUBJECT_SIMILARITY_THRESHOLD, Entry, EntryFormatError,
    EntryOrigin, backlinks, entry_key_collision_error, entry_key_error, parse_entry_md,
    parse_links, render_entry_md, render_index, same_subject,
)

#: 设计文档 §4.2 给出的条目样例，逐字节照抄——渲染函数要能原样产出它。
DOC_ENTRY_MD = """---
key: 药铺的暗格
title: 药铺的暗格
summary: 柜台下第三块砖是空的，里侧有夹层
subject: 药铺
indexed: true
status: active
origin: {kind: scene, scene: 贝克街221B, turn: 42}
---

柜台下方，第三块砖是活的。抽出来，里侧有个夹层，塞得进一封对折的信。
陈掌柜说这是上一任掌柜留下的，[[陈掌柜]]自己也没打开过。
"""

DOC_ENTRY = Entry(
    key="药铺的暗格",
    title="药铺的暗格",
    summary="柜台下第三块砖是空的，里侧有夹层",
    subject="药铺",
    indexed=True,
    status="active",
    origin=EntryOrigin(kind="scene", scene="贝克街221B", turn=42),
    body="柜台下方，第三块砖是活的。抽出来，里侧有个夹层，塞得进一封对折的信。\n"
         "陈掌柜说这是上一任掌柜留下的，[[陈掌柜]]自己也没打开过。",
)


def _entry(**kw) -> Entry:
    base = dict(key="键", title="标题", summary="摘要", body="正文")
    base.update(kw)
    return Entry(**base)


# ------------------------------------------------------------------ 序列化往返 --

def test_render_matches_design_doc_sample():
    """渲染必须与设计文档 §4.2 的样例逐字节相同——格式是定死的，不是"差不多就行"。

    这条一旦漂了，用户手写的 .md 与程序写出的 .md 就会长出两种风格，diff 全是噪声。
    """
    assert render_entry_md(DOC_ENTRY) == DOC_ENTRY_MD


def test_parse_design_doc_sample():
    """解析设计文档样例要拿到全部字段（含 origin 的 kind/scene/turn）。"""
    entry = parse_entry_md(DOC_ENTRY_MD)
    assert entry == DOC_ENTRY
    assert entry.origin.kind == "scene" and entry.origin.scene == "贝克街221B"
    assert entry.origin.turn == 42
    assert entry.indexed is True and entry.status == "active"
    assert "[[陈掌柜]]" in entry.body


def test_round_trip_is_byte_stable():
    """render → parse → render 必须逐字节相等：否则每次保存都在无意义地改写用户文件。"""
    full = Entry(
        key="那封信", title="那封信", summary="对折的，蜡封已裂", body="信纸只剩半张。\n",
        subject="药铺", indexed=False, status="outdated",
        superseded_by="暗格里的信", supersedes=["旧信", "残页"],
        origin=EntryOrigin(kind="manual", scene="", turn=7, at="2026-09-11T21:30:00"),
        updated_at="2026-09-11T21:31:00",
    )
    text = render_entry_md(full)
    assert parse_entry_md(text) == full
    assert render_entry_md(parse_entry_md(text)) == text
    # 首尾空行以外的正文原样保留（多段正文中的空行是有意义的）
    multi = _entry(body="第一段\n\n第二段")
    assert parse_entry_md(render_entry_md(multi)).body == "第一段\n\n第二段"


def test_render_omits_empty_optional_fields():
    """可选字段为空时不写进 front-matter——样例里就没有它们，空值只会让 diff 变吵。"""
    text = render_entry_md(_entry())
    assert "superseded_by" not in text and "supersedes" not in text
    assert "updated_at" not in text
    # 该写的核心字段一个不少，且键序稳定（git diff 才可读）
    assert [ln.split(":")[0] for ln in text.splitlines()[1:8]] == [
        "key", "title", "summary", "subject", "indexed", "status", "origin"]


def test_render_quotes_values_that_yaml_would_retype():
    """会被 YAML 改型的值必须加引号——ISO 时间戳会被读成 datetime，键 `yes` 会被读成 True。

    不加引号的后果是 round-trip 把 str 变成 datetime/ bool，第二次保存直接炸字段校验。
    """
    entry = _entry(key="yes", updated_at="2026-09-11T21:30:00",
                   origin=EntryOrigin(kind="manual", at="2026-09-11T21:30:00"))
    back = parse_entry_md(render_entry_md(entry))
    assert back.key == "yes" and isinstance(back.key, str)
    assert back.updated_at == "2026-09-11T21:30:00"
    assert back.origin.at == "2026-09-11T21:30:00"


def test_parse_tolerates_bom_and_crlf():
    """手写/Windows 编辑过的文件带 BOM 或 CRLF 也要读得出来——这不是"坏输入"，是常态。"""
    text = DOC_ENTRY_MD.replace("\n", "\r\n")
    assert parse_entry_md("﻿" + text) == DOC_ENTRY


def test_body_leading_and_trailing_blank_lines_are_normalized():
    """正文首尾空行归一（去首尾空行）——这是往返逐字节一致能对任意输入成立的前提。"""
    assert _entry(body="\n\n正文\n\n").body == "正文"
    assert Entry(key="甲").body == ""   # 正文可以为空（只有索引行也合法）


# ------------------------------------------------- 渲染的上下文安全（往返一致的前提）--

#: 会被 front-matter 收尾线扫描误认的两行（`...` 也是合法的 YAML 文档结束标记）。
_FENCE_LIKE = ("---", "...")

#: 模型写多行摘要时最容易带进来的形态：一行摘要里嵌一条 markdown 分隔线。
_NOISY_LINE = "在场者：掌柜、我\n{mark}\n我拿到了那封信"


@pytest.mark.parametrize("mark", _FENCE_LIKE)
@pytest.mark.parametrize("where", ["title", "summary", "subject", "superseded_by",
                                   "updated_at", "origin.scene", "origin.at",
                                   "supersedes.0"])
def test_fence_like_line_in_any_field_round_trips(where, mark):
    """值里有一行 `---`/`...` 时，条目仍必须**写得出、读得回**（否则条目静默消失）。

    渲染多行字符串时 safe_dump 会把它摊成多行引号标量、续行缩进两格；收尾线扫描若按
    `line.strip()` 逐行比对，就会把续行里那句 `---` 当成 front-matter 的结尾——引号从此
    不闭合，整条解析失败。后果不是报错给人看，而是下次加载时这条记忆**不在内存里**：
    索引表里没有它、read_entry 摸不到它、结算的 pending 也对不上，而磁盘上那个坏文件
    会一直躺着。摘要里带上一条分隔线对模型来说毫不难发生，所以这条必须钉死。
    """
    noisy = _NOISY_LINE.format(mark=mark)
    kw: dict = {"title": "标题", "summary": "摘要"}
    if where == "origin.scene":
        kw["origin"] = EntryOrigin(kind="scene", scene=noisy)
    elif where == "origin.at":
        kw["origin"] = EntryOrigin(kind="scene", at=noisy)
    elif where == "supersedes.0":
        kw["supersedes"] = [noisy]
    else:
        kw[where] = noisy
    entry = _entry(**kw)

    text = render_entry_md(entry)
    assert parse_entry_md(text) == entry
    assert render_entry_md(parse_entry_md(text)) == text
    # 行首的收尾线只该有一对（开 + 收）。值里那句若漏到行首，扫描就会在半路截断。
    assert [ln for ln in text.split("\n")
            if ln == ln.lstrip() and ln.strip() in _FENCE_LIKE] == ["---", "---"]


@pytest.mark.parametrize("where", ["summary", "origin.scene", "supersedes.0"])
@pytest.mark.parametrize("value", [
    "乙, 丙",              # 流上下文里的逗号：不引就是两个元素（一个键静默裂成两个）
    "第3场, 雨夜",          # 出处被从逗号处截断，后半截静默丢失
    "?雨夜",               # 行首问号是流上下文里的指示符：不引直接语法错
    "礼堂[东]", "[东厢]", "{正门}", "正门}",
    "a: b", "末尾:", "#井号", "*星号", "&与", "!叹", "%百分", "@at",
    "- 破折号", "yes", "42", "2026-09-11T21:30:00",
])
def test_flow_context_values_round_trip(where, value):
    """`origin: {...}` 与 `supersedes: [...]` 是**流**上下文，值里的语法字符必须被引起来。

    safe_dump 默认按**块**上下文分析一个标量，`乙, 丙` 那样在块里完全合法的值不会被加
    引号；可这些值是被拼进流映射/流序列的，逗号在那里是分隔符——于是 `supersedes` 里
    一个键裂成两个（指向一个不存在的键，旧条永远不会被标成过时），`origin.scene` 被从
    逗号处切断，出处就是错的。两处都是**静默错值**，文件写坏了还稳定地错下去。
    """
    kw: dict = {"title": "标题", "summary": "摘要"}
    if where == "origin.scene":
        kw["origin"] = EntryOrigin(kind="scene", scene=value)
    elif where == "supersedes.0":
        kw["supersedes"] = [value]
    else:
        kw[where] = value
    entry = _entry(**kw)

    text = render_entry_md(entry)
    assert parse_entry_md(text) == entry
    assert render_entry_md(parse_entry_md(text)) == text


def test_parse_handles_legit_multiline_values_and_only_fence_at_column_zero():
    """收尾线只认**行首**的 `---`/`...`：手写的多行标量（含缩进的 `---`）要能整段读出来。

    反面同样要钉住：缩进的 `---` 不算收尾线，缺收尾线时必须照旧报错，不能"将就着截断"
    ——那会把剩下的 front-matter 当成正文吃进去。
    """
    handwritten = ("---\nkey: 甲\nsummary: '先写了这句\n\n  ---\n\n  又写了这句'\n---\n正文\n")
    parsed = parse_entry_md(handwritten)
    assert parsed.summary == "先写了这句\n---\n又写了这句"
    assert parsed.body == "正文"

    with pytest.raises(EntryFormatError) as exc:
        parse_entry_md("---\nkey: 甲\n  ---\n正文\n")     # 缩进的收尾线不算数
    assert "---" in str(exc.value)


# -------------------------------------------------------------------- 键守门 --

def test_entry_key_collision_error_flags_case_only_clash():
    """只有大小写不同的两个键会落到**同一个文件名**上——必须有一个能拦住它们的地方。

    键是冲突判定的第一依据，`ABC` 与 `abc` 是两条不同的条目；但 NTFS 与默认的 macOS 卷
    大小写不敏感，`ABC.md` 与 `abc.md` 是同一个文件，后写的一条静默顶掉前一条。判据放在
    纯函数里，让内存索引（`Library.upsert`）能在"同一件事"成立的那一刻就拦住，而不是等
    到写盘才发现少了一条。这条规则在**所有平台**上生效：素材是要走 git 同步的，在 Linux
    上放行等于把雷埋到 Windows 上才炸（与 `_ILLEGAL_NAME_CHARS` 那套 Windows 规则同口径）。
    """
    assert entry_key_collision_error("ABC", "abc") != ""
    assert entry_key_collision_error("NPC-A", "npc-a") != ""
    assert entry_key_collision_error("abc", "ABC") != ""      # 与先后顺序无关
    assert entry_key_collision_error("甲", "甲") == ""          # 同一个键 = 同一条目，覆盖是调用方的本意
    assert entry_key_collision_error("甲", "乙") == ""
    assert entry_key_collision_error("ab", "abc") == ""        # 只是前缀，不是同一个文件
    assert entry_key_collision_error("", "abc") == ""          # 空键由 entry_key_error 负责拦


# -------------------------------------------------------------------- 坏输入 --

@pytest.mark.parametrize("text, hint", [
    ("没有 front-matter，直接是正文", "---"),
    ("---\nkey: 甲\n\n正文\n", "---"),           # 缺收尾分隔线
    ("---\nkey: [甲\n---\n正文\n", "YAML"),      # YAML 语法错（未闭合的 flow 序列）
    ("---\ntitle: 没有键\n---\n正文\n", "key"),  # 缺 key
    ("---\nkey: '  '\n---\n正文\n", "key"),      # key 全是空白
    ("---\n- 甲\n- 乙\n---\n正文\n", "映射"),     # front-matter 不是映射
    ("---\n\n---\n正文\n", "key"),               # 空 front-matter
    ("---\nkey: a/b\n---\n正文\n", "分隔符"),     # key 没过守门
])
def test_parse_bad_input_raises_with_reason(text, hint):
    """坏 front-matter / 缺 key / YAML 语法错 / 键非法——一律当场抛，且原因带得出关键词。

    「绝不静默返回半个对象」：半个条目比报错危险得多，它会带着空正文被当成真相源保存下去。
    """
    with pytest.raises(EntryFormatError) as exc:
        parse_entry_md(text)
    assert hint in str(exc.value)


def test_parse_rejects_unknown_front_matter_keys():
    """不认识的字段名（多半是手写笔误）必须报错，不能静默丢弃。

    静默丢弃等于下次保存时把用户手写的那一行抹掉——entry 文件是元信息的唯一真相源。
    """
    with pytest.raises(EntryFormatError) as exc:
        parse_entry_md("---\nkey: 甲\ntitel: 打错字的标题\n---\n正文\n")
    assert "titel" in str(exc.value)


def test_entry_model_rejects_illegal_values():
    """模型层自身就要严：键过守门、status/origin.kind 只能取枚举里的值、不认的字段直接拒。"""
    with pytest.raises(ValidationError) as exc:
        Entry(key="a/b")
    assert "分隔符" in str(exc.value)
    with pytest.raises(ValidationError):
        _entry(status="过时")
    with pytest.raises(ValidationError):
        Entry(key="甲", origin={"kind": "梦里"})
    with pytest.raises(ValidationError):
        Entry(key="甲", 未知字段=1)


# -------------------------------------------------------------------- 键守门 --

@pytest.mark.parametrize("key, reason", [
    ("", "不能为空"),
    ("   ", "不能为空"),
    (" 甲", "首尾不能有空白"),
    ("甲 ", "首尾不能有空白"),
    (".甲", "以「.」开头"),
    ("甲..乙", "「..」"),
    ("甲/乙", "路径分隔符"),
    ("甲\\乙", "路径分隔符"),
    ("甲:乙", "这些字符"),
    ("甲?乙", "这些字符"),
    ("甲<乙>丙|丁", "这些字符"),
    ("甲\x07乙", "控制字符"),
    ("甲.", "以「.」结尾"),
    ("CON", "系统保留名"),
    ("con", "系统保留名"),
    ("com3", "系统保留名"),
    ("LPT9", "系统保留名"),
    ("甲" * (MAX_KEY_LENGTH + 1), "太长"),
])
def test_entry_key_error_rejects(key, reason):
    """键会成为文件名：每一条拒绝规则都要有中文原因，且原因里点名是哪一种问题。

    与 loaders.scene_filename_error 同源——放行一个 `../` 就等于让写盘跑出信息库目录。
    长度上限是本模块**额外**加的一条：Windows 全路径有上限，键得留出目录前缀的余量。
    """
    assert reason in entry_key_error(key)


@pytest.mark.parametrize("key", ["药铺的暗格", "场景-贝克街221B-第3场", "那封信", "a.b", "A-B_C",
                                 "甲" * MAX_KEY_LENGTH, "1"])
def test_entry_key_error_accepts(key):
    """合法键返回空串（调用方据此放行）；返回非空串的语义是"给用户看的中文原因"。"""
    assert entry_key_error(key) == ""


# -------------------------------------------------------------------- 链接解析 --

def test_parse_links_basic_dedup_and_order():
    """取出全部 [[出链]]，去重且**保序**——索引顺序由正文顺序决定，反链列表才能稳定。"""
    body = "先见 [[甲]]，再见 [[乙]]，又提 [[甲]]。"
    assert parse_links(body) == ["甲", "乙"]


def test_parse_links_trims_spaces_and_keeps_inner_ones():
    """`[[ 陈掌柜 ]]` 里的空白是笔误，剥掉；但键中间的空格是键的一部分，不能动。"""
    assert parse_links("[[ 陈掌柜 ]]") == ["陈掌柜"]
    assert parse_links("[[陈 掌柜]]") == ["陈 掌柜"]


def test_parse_links_ignores_empty_brackets():
    """空链接（`[[]]`、`[[   ]]`）指向不了任何东西——不产出空串，免得下游拿它去查库。"""
    assert parse_links("[[]] 与 [[   ]] 都不是链接") == []


def test_parse_links_never_spans_lines():
    """链接不跨行：一个孤零零的 `[[` 不许把后面整段正文都吞成链接目标。

    这是"未闭合"这条边界的兜底——跨行匹配会让一处手滑吃掉半篇文章。
    """
    assert parse_links("前[[陈\n掌柜]]后") == []
    assert parse_links("看[[陈掌柜") == []          # 未闭合


def test_parse_links_rejects_nested_brackets():
    """目标里还残留方括号的（`[[外[[内]]外]]`）不算链接——嵌套不是这套语法支持的东西。"""
    assert parse_links("[[外[[内]]外]]") == []


def test_parse_links_tolerates_edge_text_and_empty_body():
    """前后有散字、`]]` 在 `[[` 之前、正文为空——都不该抛，最多是零条链接。"""
    assert parse_links("散字[[甲]]散字") == ["甲"]
    assert parse_links("]] 在前 [[甲]] 在后") == ["甲"]
    assert parse_links("") == [] and parse_links("没有链接") == []


# ------------------------------------------------------------------ 反向链接 --

def test_backlinks_lists_sources_in_input_order():
    """反链 = 哪些条目的正文里指向了这个键（§6.2 的 read_entry 要附这一行）。

    顺序跟输入条目顺序走，且同一键只出现一次——界面上列"被引用 N 处"得是稳定的。
    """
    a = _entry(key="药铺的暗格", body="砖下夹层，[[陈掌柜]]知道")
    b = _entry(key="旧账本", body="[[陈掌柜]]记过一笔，[[陈掌柜]]又划掉了")
    c = _entry(key="无关", body="谁也不指向")
    assert backlinks([a, b, c], "陈掌柜") == ["药铺的暗格", "旧账本"]


def test_backlinks_excludes_self_and_unknown_key():
    """自链不算自己的反链（列出来是纯噪声）；没人指向的键返回空表，不抛。"""
    a = _entry(key="陈掌柜", body="我提到我自己 [[陈掌柜]]")
    assert backlinks([a], "陈掌柜") == []
    assert backlinks([a], "查无此键") == []
    assert backlinks([a], "") == []


# ------------------------------------------------------------------ 冲突判定 --

def test_same_subject_same_key_wins():
    """两级判定的第一级：同 key 即同一件事，标题写得再不一样也判同一件（§3.5）。"""
    assert same_subject(_entry(key="陈掌柜", title="完全不同的写法"),
                        _entry(key="陈掌柜", title="另一套说法"))


def test_same_subject_by_similar_title():
    """第二级：标题在 textsim 意义上近似即同一件事——复用仓库既有的近重复判定。"""
    assert same_subject(_entry(key="甲", title="陈掌柜"),
                        _entry(key="乙", title="陈掌柜（跛足，左眉有疤）"))
    assert same_subject(_entry(key="甲", title="药铺的暗格"),
                        _entry(key="乙", title="药铺暗格"))


def test_same_subject_rejects_different_things():
    """两级之外不自作聪明：同结构但异指、或短标题只差一个字的，允许共存（§3.5 明说）。

    阈值必须低于 speak 的复读线（0.95）——那是拦"几乎逐字重复"，这里是"是不是同一件事"；
    但也不能低到把「地理·西线」与「地理·东线」判成一件事。
    """
    assert same_subject(_entry(key="甲", title="地理·西线"),
                        _entry(key="乙", title="地理·东线")) is False
    assert same_subject(_entry(key="甲", title="朝局"), _entry(key="乙", title="暗格")) is False
    # 标题 normalize 后为空（纯括号/纯标点）：一律不判同一件事，避免空对空相撞
    assert same_subject(_entry(key="甲", title="（无题）"),
                        _entry(key="乙", title="（无题）")) is False
    assert SUBJECT_SIMILARITY_THRESHOLD < 0.95


# ------------------------------------------------------------------ 索引渲染 --

def test_render_index_empty_is_empty_string():
    """**硬约束**：无条目 / 全是不进索引的条目 → 返回空串。

    这是「无库角色的系统提示逐字节不变」的落点（§6.1）：只要这里漏出一个换行或一个标题，
    950 个既有测试与所有老卡的提示词就全变。
    """
    assert render_index([]) == ""
    assert render_index([_entry(indexed=False)]) == ""
    assert render_index([], subscribed=[("庆国世界观", [])]) == ""


def test_render_index_matches_design_doc_sample():
    """按 subject 分组、订阅来的条目单独一组并标出来源库——照 §6.1 的样子逐字节对齐。"""
    entries = [
        Entry(key="药铺的暗格", summary="柜台下第三块砖是空的，里侧有夹层", subject="药铺"),
        Entry(key="陈掌柜", summary="跛足，左眉有疤", subject="人物"),
    ]
    subscribed = [("庆国世界观", [
        Entry(key="地理·西线", summary="靠山，常年封冻"),
        Entry(key="朝局·三相", summary="互相牵制"),
    ])]
    expected = f"""【你的信息索引】
▾ 药铺
   药铺的暗格 — 柜台下第三块砖是空的，里侧有夹层
▾ 人物
   陈掌柜 — 跛足，左眉有疤
▾ 借自《庆国世界观》
   地理·西线 — 靠山，常年封冻
   朝局·三相 — 互相牵制

{INDEX_HINT}"""
    assert render_index(entries, subscribed=subscribed) == expected


def test_render_index_ungrouped_entries_come_first_without_header():
    """没有 subject 的条目**不打组头**、排在最前——remember 工具本就不传 subject，
    这一桶才是常态，给它硬安一个「其他」组头会让索引平白多出一行噪声。
    """
    text = render_index([Entry(key="甲", summary="无组"), Entry(key="乙", subject="药铺")])
    assert text.splitlines() == [
        "【你的信息索引】",
        "   甲 — 无组",
        "▾ 药铺",
        "   乙",
        "",
        INDEX_HINT,
    ]


def test_render_index_falls_back_to_key_and_drops_empty_summary():
    """标题为空时回落 key；摘要为空时不留一个悬空的分隔符——索引行永远读得通。"""
    text = render_index([Entry(key="甲"), Entry(key="乙", title="乙的标题", summary="")])
    assert "   甲" in text.splitlines()
    assert "   乙的标题" in text.splitlines()
    assert "—" not in text


def test_render_index_excludes_outdated_by_default():
    """过时条目默认不进索引：索引是"我现在知道什么"的入口，把旧说法一并列上会让
    角色读到自相矛盾的两条（旧条仍在库里、仍能被 read_entry 摸到，只是不再当入口）。"""
    old = Entry(key="旧", summary="旧说法", status="outdated", superseded_by="新")
    new = Entry(key="新", summary="新说法")
    assert "旧说法" not in render_index([old, new])
    assert "旧说法" in render_index([old, new], include_outdated=True)


def test_render_index_respects_max_entries():
    """条目数上限参数位（§10.2 要能截断并记日志）：按显示顺序保留前 N 条，多余的丢。

    丢弃的计数由调用方（存储层）记日志——本函数是纯渲染，不写盘、不打日志。
    """
    entries = [Entry(key=f"第{i}", summary=f"摘要{i}", subject="一" if i < 3 else "二")
               for i in range(5)]
    text = render_index(entries, max_entries=3)
    assert [ln for ln in text.splitlines() if "摘要" in ln] == [
        "   第0 — 摘要0", "   第1 — 摘要1", "   第2 — 摘要2"]
    assert "▾ 二" not in text                       # 整组被截掉时不留下空组头
    assert render_index(entries, max_entries=0) == ""
    assert render_index(entries, max_entries=None).count("摘要") == 5


def test_render_index_collapses_newlines_in_titles():
    """标题/摘要里混进的换行在索引行里压成空格——索引是一行一条，破行会毁掉整节格式。"""
    text = render_index([Entry(key="甲", title="上\n下", summary="摘\n要")])
    assert "   上 下 — 摘 要" in text.splitlines()
