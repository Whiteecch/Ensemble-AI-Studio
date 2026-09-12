"""knowledgetools.py 的测试（设计文档 §6.2 工具集 / §6.3 工具循环 / §6.4 取用通道 / §13.1 失败不回抛）。

钉的都是**会静默出错**的地方——它们出事时不会抛，只会让角色带着错的知识开口，或者
让整块 think 炸掉重试（重试即重复计费）：

  · 工具执行失败**必须变成给模型看的文本**：键不存在、键不合法、IO 出错都绝不回抛——回抛
    会让 worker 整块重试，一个记不下来的条目名就能把整块的钱再花一遍；
  · 新压旧：新说法上位、旧说法标过时，**旧条一字不删**（§3.5，删了就找不回"他原先怎么
    以为的"）；
  · 一切写入落在**本场副本**，本体库一个字都不动（§5.2，那是散场结算能成立的前提）；
  · 广域库条目拒绝修订（§3.4，世界观不该被某个角色在场景里改写）；
  · 取用记录 recall.jsonl 可按轮次截断（§5.3/§6.4，撤回后角色不能还带着"从没发生过的事"
    开口）。

全程 `tmp_path`，不碰真实用户目录、不碰仓库里的素材目录。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from harness import knowledgestore as store
from harness.knowledge import Entry, EntryOrigin
from harness.knowledgetools import (
    MAX_BODY_CHARS,
    MAX_SUMMARY_CHARS,
    MAX_TOOL_CALLS,
    MAX_TOOL_ROUNDS,
    RECALL_FILENAME,
    TOOL_SPECS,
    KnowledgeAccess,
)

SCENE = "贝克街221B"
ME = "乙"
WIDE = "庆国世界观"


# --------------------------------------------------------------------- 夹具 --

def _access(tmp_path: Path, **kw) -> KnowledgeAccess:
    """注入 tmp_path 的场景运行根与信息库根；默认开库。"""
    kw.setdefault("libraries_root", tmp_path / "libraries")
    return KnowledgeAccess(tmp_path / "runs" / SCENE, SCENE, **kw)


def _own_lib(acc: KnowledgeAccess, character: str, entries=()) -> Path:
    """在信息库根下造一座角色本体库（正式布局 `characters/<角色名>`）。"""
    path = store.library_dir("character", character, root=acc.libraries_root)
    lib = store.Library(name=character, scope="character", owner=character)
    for entry in entries:
        lib.upsert(entry)
    store.save_library(lib, path)
    return path


def _join(acc: KnowledgeAccess, character: str) -> Path:
    """模拟引擎在角色进场时建副本（§5.1），返回副本目录。"""
    own = store.library_dir("character", character, root=acc.libraries_root)
    dst = store.scene_library_dir(acc.run_root, character)
    store.ensure_scene_copy(own, dst)
    return dst


def _copy(acc: KnowledgeAccess, character: str, entries=()) -> Path:
    """直接往副本目录里放一座库（不经过本体），用于只关心读/改的用例。"""
    dst = store.scene_library_dir(acc.run_root, character)
    lib = store.Library(name=character, scope="character", owner=character)
    for entry in entries:
        lib.upsert(entry)
    store.save_library(lib, dst)
    return dst


def _entry(key: str, **kw) -> Entry:
    base = dict(title=key, summary=f"{key}的一句话", body=f"{key}的正文")
    base.update(kw)
    return Entry(key=key, **base)


def _scene_entry(key: str, **kw) -> Entry:
    """一条本场产生的条目（origin.kind=scene，带轮次）。"""
    kw.setdefault("origin", EntryOrigin(kind="scene", scene=SCENE, turn=1))
    return _entry(key, **kw)


def _live(acc: KnowledgeAccess, character: str) -> store.Library:
    """读回副本里的库（断言写入结果用）。"""
    return store.load_library(store.scene_library_dir(acc.run_root, character)).library


def _recall_rows(acc: KnowledgeAccess, character: str) -> list[dict]:
    path = Path(acc.run_root) / character / RECALL_FILENAME
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ------------------------------------------------------------------ 预算常量 --

def test_budget_constants_pin_design_numbers():
    """单角色单块 3 轮 / 8 次取用（§6.3、§10.2）——下一个阶段 graph 的工具循环上限。

    这两个数写死在模块级：工具循环的闸门只有一个落点，散落在各处迟早会漂成两套。
    """
    assert MAX_TOOL_ROUNDS == 3
    assert MAX_TOOL_CALLS == 8


# ---------------------------------------------------------------- TOOL_SPECS --

def _schema_problems(spec: dict) -> list[str]:
    """手工校一遍 OpenAI 函数工具的骨架（仓库没装 jsonschema，不引新依赖）。"""
    problems: list[str] = []
    if spec.get("type") != "function":
        problems.append("type 不是 function")
    fn = spec.get("function")
    if not isinstance(fn, dict):
        return problems + ["没有 function 对象"]
    if not str(fn.get("description") or "").strip():
        problems.append("description 为空")
    params = fn.get("parameters")
    if not isinstance(params, dict) or params.get("type") != "object":
        return problems + ["parameters 不是 object"]
    props = params.get("properties")
    if not isinstance(props, dict) or not props:
        problems.append("properties 为空")
    for field, body in (props or {}).items():
        if not isinstance(body, dict) or not body.get("type"):
            problems.append(f"参数 {field} 没有 type")
        if not str(body.get("description") or "").strip():
            problems.append(f"参数 {field} 没有 description")
    required = params.get("required")
    if not isinstance(required, list):
        problems.append("required 不是数组")
    else:
        for field in required:
            if field not in (props or {}):
                problems.append(f"required 里的 {field} 不在 properties 里")
    return problems


def test_tool_specs_shape_is_three_openai_functions():
    """三个工具、OpenAI 格式、每个参数都有类型与说明（§6.2）。

    description 是模型**唯一**的行为指引：空 description 的工具模型不会用；参数没有
    description，模型就会把 key 写成一句话。故这两项都进断言。
    """
    names = [spec["function"]["name"] for spec in TOOL_SPECS]
    assert names == ["read_entry", "remember", "revise"]
    for spec in TOOL_SPECS:
        assert _schema_problems(spec) == []


def test_tool_specs_required_params_match_design():
    """必填项按 §6.2 写死：read/remember 的必填项、revise 只强制 key。

    revise 的 summary/body 必须是**可选**：两个都必填会让模型为了凑参数而重抄一遍没变的
    那半段，那是纯粹的多花 token。
    """

    def required(name: str) -> list[str]:
        spec = next(s for s in TOOL_SPECS if s["function"]["name"] == name)
        return spec["function"]["parameters"]["required"]

    assert required("read_entry") == ["key"]
    assert required("remember") == ["key", "title", "summary", "body"]
    assert required("revise") == ["key"]


def test_tool_descriptions_teach_the_intended_behaviour():
    """description 必须讲清三件最容易做错的事（§6.2/§3.5）。

    · read_entry 要说明正文里的 [[链接]] 可以顺着读（图要能多跳，否则角色漏掉半边）；
    · remember 要说明"不是每句话都值得记"与"同一件事用同一个 key"（否则同一件事记成
      两条，索引越用越吵）；
    · revise 要说明"推翻旧认识时改它而不是新记"与"旧说法不删、只标过时"。
    """
    by_name = {s["function"]["name"]: s["function"]["description"] for s in TOOL_SPECS}
    assert "[[" in by_name["read_entry"]
    assert "值得记" in by_name["remember"] and "key" in by_name["remember"]
    assert "推翻" in by_name["revise"] and "过时" in by_name["revise"]
    # 同键 remember 是**原地顶掉**旧正文（一个键一个文件，没有两条可留）：这句必须说，
    # 否则模型会用 remember 半截重记同一件事，以为旧内容还在。
    assert "顶掉" in by_name["remember"]


def test_revise_description_does_not_promise_the_old_text_is_kept(tmp_path):
    """revise 的说明不能说"旧说法不会被删掉"：它改的那条是**原地替换**（§6.2/§3.5）。

    description 是模型唯一的行为指引。读到"旧说法不会被删掉"，模型就会放心地只写新增的那半
    段、不重抄旧正文，于是那条认识被悄悄换成半截——而"一字不删、只标过时"说的是**同名的
    其它条目**（`_press_others` 那条路）。两件事必须在措辞上分开，并给出避险动作：你写的
    内容会替换掉原来的正文，想留着的老话得自己写上。
    """
    spec = next(s for s in TOOL_SPECS if s["function"]["name"] == "revise")
    desc = spec["function"]["description"]
    assert "旧说法不会被删掉" not in desc
    assert "替换" in desc
    assert "过时" in desc                          # 同名其它条目只标过时（§3.5），这句留着
    assert "替换" in spec["function"]["parameters"]["properties"]["body"]["description"]


def test_tools_returns_specs_even_when_closed(tmp_path):
    """无库、关闭开关时 `tools()` 照常返回三个工具（调用方自己决定要不要传给后端）。

    契约是"函数本身照常返回"：让调用方去判断"有没有库"，比让本方法返回 None/空表更好——
    传不传 tools 是 §6.3 那条成本闸门的事，落点不该在这里。
    """
    acc = _access(tmp_path, enabled=False)
    assert acc.tools() == TOOL_SPECS
    assert len(acc.tools()) == 3


# ---------------------------------------------------------------- index_text --

def test_index_text_empty_library_renders_empty_string(tmp_path):
    """空库渲染成空串（§6.1 硬约束）：无库角色的系统提示必须逐字节不变。

    一个占位文本都会让所有老卡的提示词变样。
    """
    acc = _access(tmp_path)
    empty_copy = _copy(acc, ME)                       # 有副本目录，但一条都没有
    assert empty_copy.is_dir()
    assert acc.index_text(ME) == ""
    assert acc.index_text("从没建过库的人") == ""


def test_index_text_renders_own_and_subscribed_rows(tmp_path):
    """自有条目与订阅来的活引用都要出现在索引里（§3.3/§3.4）。

    订阅是**活引用**：索引里必须有借来的那几行，否则角色读不到世界观。
    """
    wide = tmp_path / "libraries" / "wide" / WIDE
    lib = store.Library(name=WIDE, scope="wide")
    lib.upsert(Entry(key="地理·西线", title="地理·西线", summary="靠山，常年封冻"))
    store.save_library(lib, wide)

    acc = _access(tmp_path, subscriptions={ME: [(WIDE, wide)]})
    _copy(acc, ME, [_entry("药铺的暗格", summary="柜台下第三块砖是空的")])
    text = acc.index_text(ME)
    assert "药铺的暗格" in text
    assert f"借自《{WIDE}》" in text and "地理·西线" in text


def test_index_text_closed_is_empty(tmp_path):
    """关掉信息库 = 不注入（与无库同一条路径，系统提示逐字节不变，§10.2）。"""
    acc = _access(tmp_path, enabled=False)
    _copy(acc, ME, [_entry("药铺的暗格")])
    assert acc.index_text(ME) == ""


# ------------------------------------------------------------------- execute --

def test_execute_unknown_tool_and_bad_args_become_text(tmp_path):
    """认不出的工具名、读不出的参数都变成文本，绝不抛（§13.1）。

    模型手滑给一个不存在的工具名（或在工具循环里拼错 JSON）时，把整块 think 炸掉等于
    让 worker 整块重试——一个拼写错误就该只回一句话。
    """
    acc = _access(tmp_path)
    _copy(acc, ME, [_entry("药铺的暗格")])
    assert "read_entry" in acc.execute(ME, "读库", {}, turn=1)
    assert "参数" in acc.execute(ME, "read_entry", "{不是 json", turn=1)
    assert "参数" in acc.execute(ME, "read_entry", [1, 2], turn=1)
    assert "参数" in acc.execute(ME, "remember", "[]", turn=1)


def test_execute_closed_returns_text(tmp_path):
    """关库时执行工具（理论上调用方不会传 tools）也只是一句话，不抛。"""
    acc = _access(tmp_path, enabled=False)
    assert acc.execute(ME, "remember", {"key": "X"}, turn=1) != ""


def test_execute_never_raises_on_io_error(tmp_path, monkeypatch):
    """磁盘出错（写不动/读不了）也只回一段文本（§13.1）。

    这条是"工具执行失败绝不回抛"的兜底：任何没被预见的异常都不该炸掉整块 think。
    """
    acc = _access(tmp_path)
    _copy(acc, ME)

    def _boom(*a, **kw):
        raise OSError("磁盘满了")

    monkeypatch.setattr(store, "save_library", _boom)
    out = acc.execute(ME, "remember", {"key": "新事", "title": "新事"}, turn=1)
    assert "remember" in out and "磁盘满了" in out


# ---------------------------------------------------------------- read_entry --

def test_read_entry_returns_body_with_backlinks(tmp_path):
    """命中时返回正文**并附反向链接**（§6.2）。

    图只能单向走的话，角色从"药铺的暗格"摸到"陈掌柜"，却永远想不起"那封信里也提到过
    这个暗格"——反链正是让图双向的那一行。
    """
    acc = _access(tmp_path)
    _copy(acc, ME, [
        _entry("药铺的暗格", body="第三块砖是活的，[[陈掌柜]]说他自己也没打开过。"),
        _entry("陈掌柜", body="跛足，左眉有疤。"),
        _entry("那封信", body="信里提到[[药铺的暗格]]，没写收信人。", indexed=False),
    ])
    out = acc.execute(ME, "read_entry", {"key": "药铺的暗格"}, turn=3)
    assert "第三块砖是活的" in out
    # 反链行要让人读得出这是"指向这条的条目"，而不是把它混进正文。
    backlink = next(line for line in out.splitlines() if "指向" in line)
    assert "那封信" in backlink                # 它指向这里
    assert "陈掌柜" not in backlink            # 这是本条正文里的出链，不是入边


def test_read_entry_backlink_line_says_none_when_alone(tmp_path):
    """没有入边时也留一行明话，而不是让模型自己猜"是不是没印出来"。"""
    acc = _access(tmp_path)
    _copy(acc, ME, [_entry("独条", body="没有别人提到它。")])
    out = acc.execute(ME, "read_entry", {"key": "独条"}, turn=1)
    assert "独条" in out and "指向" in out


def test_read_entry_matches_by_title_and_by_rough_name(tmp_path):
    """按标题、以及"记不太准的名字"都能命中（§6.2 的模糊匹配）。

    索引里列的是**标题**，模型不一定知道键；它照着标题回读、或把标题记岔一两个字，
    都必须读得到，否则一次手滑就白跑一轮。
    """
    acc = _access(tmp_path)
    _copy(acc, ME, [_entry("药铺的暗格", title="药铺的暗格", body="夹层在柜台下。")])
    assert "夹层在柜台下。" in acc.execute(ME, "read_entry", {"key": "药铺的暗格"}, turn=1)
    assert "夹层在柜台下。" in acc.execute(ME, "read_entry", {"key": "药铺暗格"}, turn=1)


def test_read_entry_reaches_unindexed_deep_entry(tmp_path):
    """`indexed=false` 的深层条目不在索引上，但顺着链接读得到（§3.3）。

    只看索引的话，"那封信"这类东西永远不会被角色想起来——图的意义就没了。
    """
    acc = _access(tmp_path)
    _copy(acc, ME, [_entry("那封信", body="藏在夹层里。", indexed=False)])
    out = acc.execute(ME, "read_entry", {"key": "那封信"}, turn=1)
    assert "藏在夹层里。" in out


def test_read_entry_miss_lists_nearest_titles(tmp_path):
    """未命中时列出最接近的几条，而不是干巴巴一句"没找到"（让模型下一步有事可做）。"""
    acc = _access(tmp_path)
    _copy(acc, ME, [_entry("药铺的暗格"), _entry("陈掌柜"), _entry("地理·西线")])
    out = acc.execute(ME, "read_entry", {"key": "药铺暗格在哪"}, turn=1)
    assert "没有" in out
    assert "药铺的暗格" in out


def test_read_entry_miss_on_empty_library_says_so(tmp_path):
    """空库时明说"索引是空的"——这与"名字记岔了"是两种处境，给模型的下一步也不同。"""
    acc = _access(tmp_path)
    out = acc.execute(ME, "read_entry", {"key": "随便"}, turn=1)
    assert "空" in out


def test_read_entry_empty_key_is_text_not_throw(tmp_path):
    """空 key / 非法 key 一律回文本（§13.1：这是模型的输入，不是程序员的输入）。"""
    acc = _access(tmp_path)
    _copy(acc, ME, [_entry("有东西")])
    assert acc.execute(ME, "read_entry", {}, turn=1) != ""
    assert "不能" in acc.execute(ME, "read_entry", {"key": "../跑出去"}, turn=1)


def test_read_entry_marks_outdated_entry(tmp_path):
    """过时条目仍能读到，但要**标出来**（§3.5：旧说法不删，只是不再是现行说法）。

    不标的话，模型会把一条已被推翻的旧认识当成现在的事实——这正是"新压旧"要防的事。
    """
    acc = _access(tmp_path)
    stale = _entry("陈掌柜", status="outdated", superseded_by="陈掌柜的底细")
    _copy(acc, ME, [stale, _entry("陈掌柜的底细", title="陈掌柜的底细")])
    out = acc.execute(ME, "read_entry", {"key": "陈掌柜"}, turn=1)
    assert "过时" in out and "陈掌柜的底细" in out


def test_read_entry_outdated_without_live_superseder_points_nowhere(tmp_path):
    """过时条目的"现在以谁为准"只在该条目真的读得到时才说（§6.2）。

    撤回会删掉本场那条替代者，留在库里的旧条就成了"指向一个不存在的键"。模型照着这句去
    read_entry，只会再拿到一句"没有这条"——白烧一轮，还可能以为库里记错了。说不出"以谁
    为准"的时候，就只说这条已经过时。
    """
    acc = _access(tmp_path)
    _copy(acc, ME, [_entry("陈掌柜", status="outdated", superseded_by="已经不在了")])
    out = acc.execute(ME, "read_entry", {"key": "陈掌柜"}, turn=1)
    assert "过时" in out and "旧说法还留着" in out
    assert "已经不在了" not in out


def test_read_entry_follows_live_subscription(tmp_path):
    """订阅是**活引用**（§3.4）：读它时现去源库取，对方一改，我下一次读就是新的。

    固化（拷一份断引用）是存储层 `pin_entry` 的事；工具层的读若自己缓存一份，世界观
    改了之后所有角色仍然拿着过期版本开口——那正是活引用要避免的。
    """
    wide = tmp_path / "libraries" / "wide" / WIDE
    lib = store.Library(name=WIDE, scope="wide")
    lib.upsert(Entry(key="地理·西线", title="地理·西线", body="靠山，常年封冻。"))
    store.save_library(lib, wide)

    acc = _access(tmp_path, subscriptions={ME: [(WIDE, wide)]})
    assert "常年封冻" in acc.execute(ME, "read_entry", {"key": "地理·西线"}, turn=1)
    lib.entries["地理·西线"].body = "靠山，去年起开出一条路。"
    store.save_library(lib, wide)
    assert "开出一条路" in acc.execute(ME, "read_entry", {"key": "地理·西线"}, turn=2)


def test_read_entry_reads_own_library_when_copy_is_missing(tmp_path):
    """副本还没建（引擎的进场钩子没跑到）时，读回落到本体——读是只读的，不会污染本体。

    不回落的话，角色会在这一场里"什么都想不起来"，而且没有任何提示。
    """
    acc = _access(tmp_path)
    _own_lib(acc, ME, [_entry("药铺的暗格", body="夹层在柜台下。")])
    out = acc.execute(ME, "read_entry", {"key": "药铺的暗格"}, turn=1)
    assert "夹层在柜台下。" in out


def test_read_entry_accepts_json_string_args(tmp_path):
    """工具调用传来的参数是 JSON **字符串**（OpenAI 的 tool_calls.arguments 就是这个形状）。

    适配器要能吃下它；否则真跑起来第一枪就报"参数读不出来"。
    """
    acc = _access(tmp_path)
    _copy(acc, ME, [_entry("药铺的暗格", body="夹层在柜台下。")])
    raw = json.dumps({"key": "药铺的暗格"}, ensure_ascii=False)
    assert "夹层在柜台下。" in acc.execute(ME, "read_entry", raw, turn=1)


# ------------------------------------------------------------------- remember --

def test_remember_writes_scene_copy_and_leaves_own_library_untouched(tmp_path):
    """remember 只落**本场副本**：origin 记本场与轮次，本体库一个字不动（§5.2）。

    本体被动过，"散场结算（保留/丢弃）"就不成立了——用户会在没同意的情况下永久得到
    角色在戏里随口记下的东西。
    """
    acc = _access(tmp_path)
    own = _own_lib(acc, ME, [_entry("旧事")])
    copy = _join(acc, ME)
    out = acc.execute(ME, "remember",
                      {"key": "药铺的暗格", "title": "药铺的暗格",
                       "summary": "柜台下第三块砖是空的", "body": "抽出来有个夹层。"},
                      turn=42)
    assert "药铺的暗格" in out
    written = _live(acc, ME).entries["药铺的暗格"]
    assert written.body == "抽出来有个夹层。"
    assert written.origin.kind == "scene"
    assert written.origin.scene == SCENE
    assert written.origin.turn == 42
    assert store.entry_path(copy, "药铺的暗格").is_file()
    own_entries = store.entries_dir(own)
    assert sorted(p.name for p in own_entries.glob("*.md")) == ["旧事.md"]


def test_remember_creates_copy_from_own_library_when_missing(tmp_path):
    """副本缺席时 remember 先按 §5.1 建副本（从本体拷一份），再写进去。

    否则场景中途才想起来要记东西的角色会把本体的基线全丢掉——他这一场就"忘了自己的身世"。
    """
    acc = _access(tmp_path)
    _own_lib(acc, ME, [_entry("旧事", body="本体里的旧事。")])
    acc.execute(ME, "remember", {"key": "新事", "title": "新事"}, turn=1)
    lib = _live(acc, ME)
    assert set(lib.entries) == {"旧事", "新事"}
    assert lib.entries["旧事"].origin.turn == store.BASELINE_TURN


def test_remember_supersedes_near_titled_entry_without_deleting_it(tmp_path):
    """标题近似 = 同一件事 → 新压旧：新条 active，旧条转 outdated 并记 superseded_by。

    旧条**一字不删**（§3.5）：删掉等于把他的原先认识抹掉，而"他原以为是这样"本身就是
    有用的信息（回溯、结算清单都要看它）。
    """
    acc = _access(tmp_path)
    _copy(acc, ME, [
        _entry("陈掌柜", title="陈掌柜", summary="跛足，左眉有疤", body="旧的认识：只是跛足。"),
    ])
    out = acc.execute(ME, "remember",
                      {"key": "陈掌柜的底细", "title": "陈掌柜",
                       "summary": "他其实认得那封信", "body": "新的认识：他认得那封信。"},
                      turn=7)
    lib = _live(acc, ME)
    old, new = lib.entries["陈掌柜"], lib.entries["陈掌柜的底细"]
    assert old.status == "outdated" and old.superseded_by == "陈掌柜的底细"
    assert old.body == "旧的认识：只是跛足。"          # 一字不删
    assert new.status == "active" and new.supersedes == ["陈掌柜"]
    assert store.entry_path(store.scene_library_dir(acc.run_root, ME), "陈掌柜").is_file()
    assert "过时" in out


def test_remember_same_key_updates_in_place(tmp_path):
    """同 key 就是同一条目（一个键就是一个文件）：重复 remember 是覆盖，不是长第二条。

    若在这里另起一条，索引里就会出现同名两行，而模型永远分不清哪行是现行的。
    """
    acc = _access(tmp_path)
    _copy(acc, ME)
    acc.execute(ME, "remember", {"key": "药铺的暗格", "title": "药铺的暗格",
                                 "summary": "第一版", "body": "第一版正文。"}, turn=1)
    acc.execute(ME, "remember", {"key": "药铺的暗格", "title": "药铺的暗格",
                                 "summary": "第二版", "body": "第二版正文。"}, turn=2)
    lib = _live(acc, ME)
    assert list(lib.entries) == ["药铺的暗格"]
    assert lib.entries["药铺的暗格"].body == "第二版正文。"
    assert lib.entries["药铺的暗格"].status == "active"


def test_remember_rejects_unsafe_key_with_text(tmp_path):
    """键会成为文件名（§4.3）：不安全就回一句能懂的话 + 怎么改，绝不静默改名、绝不抛。"""
    acc = _access(tmp_path)
    _copy(acc, ME)
    out = acc.execute(ME, "remember", {"key": "药铺/暗格", "title": "药铺暗格"}, turn=1)
    assert "键" in out and "分隔符" in out
    assert _live(acc, ME).entries == {}


def test_remember_records_pending_for_settlement(tmp_path):
    """每次 remember 都要登记进 pending.json（§7.2 的"本场所得"清单靠它）。

    漏登记 = 用户散场时根本看不到这一条，它是"悄悄写进去的记忆"。
    """
    acc = _access(tmp_path)
    copy = _copy(acc, ME)
    acc.execute(ME, "remember", {"key": "药铺的暗格", "title": "药铺的暗格"}, turn=1)
    pending = store.load_pending(copy)
    assert pending.added == ["药铺的暗格"] and pending.revised == []


def test_remember_same_key_hit_is_recorded_as_revised_not_added(tmp_path):
    """同键 remember 改的是**既有的认识**：结算清单上记"修订"，不是"本场新增"（§7.2）。

    用户在「本场所得」清单上看到"新增"，就看不出某条既有认识已经被替换；而清单的两半正是
    "本场新增 / 本场修订"，在界面上是两件事。反过来，本场自己长大、又被重记一次的那条仍
    该留在"新增"里——它本来就是这一场长出来的。
    """
    acc = _access(tmp_path)
    own = store.library_dir("character", ME, root=acc.libraries_root)
    lib = store.Library(name=ME, scope="character", owner=ME)
    lib.upsert(Entry(key="陈掌柜", title="陈掌柜", body="旧的认识。"))
    store.save_library(lib, own)
    copy = _join(acc, ME)

    acc.execute(ME, "remember", {"key": "药铺的暗格", "title": "药铺的暗格"}, turn=1)
    assert store.load_pending(copy).added == ["药铺的暗格"]

    acc.execute(ME, "remember", {"key": "陈掌柜", "title": "陈掌柜", "body": "新的认识。"}, turn=2)
    pending = store.load_pending(copy)
    assert pending.revised == ["陈掌柜"] and "陈掌柜" not in pending.added

    acc.execute(ME, "remember", {"key": "药铺的暗格", "title": "药铺的暗格",
                                 "body": "第二版。"}, turn=3)
    pending = store.load_pending(copy)
    assert pending.added == ["药铺的暗格"] and pending.revised == ["陈掌柜"]


# --------------------------------------------------------------------- revise --

def test_revise_rewrites_entry_and_records_pending(tmp_path):
    """revise 改副本里的既有条目：给了的字段换掉，没给的字段原样不动。

    只给 summary 却被清空 body 的话，模型每次修订都要重抄一遍正文——多花 token 之外，
    还会把正文抄丢。
    """
    acc = _access(tmp_path)
    copy = _copy(acc, ME, [_scene_entry("药铺的暗格", summary="旧摘要", body="正文不动。")])
    out = acc.execute(ME, "revise", {"key": "药铺的暗格", "summary": "新摘要"}, turn=5)
    entry = _live(acc, ME).entries["药铺的暗格"]
    assert entry.summary == "新摘要" and entry.body == "正文不动。"
    assert entry.status == "active" and entry.superseded_by is None
    assert "药铺的暗格" in out
    assert store.load_pending(copy).revised == ["药铺的暗格"]
    # 本场条目重盖轮次：这条现行说法是本轮确立的，撤回本轮那段就该把它带走（§5.3）。
    assert entry.origin.kind == "scene" and entry.origin.turn == 5


def test_revise_keeps_baseline_origin_turn(tmp_path):
    """修订从本体拷来的基线条目不重盖轮次：它的出处不是这一场（§5.3 的哨兵语义）。

    盖成当前轮次会让"这条不是本场产物"这件事在数据上变模糊——判断"该不该跟着撤回走"
    就只剩 kind 一道保险了。
    """
    acc = _access(tmp_path)
    own = store.library_dir("character", ME, root=acc.libraries_root)
    lib = store.Library(name=ME, scope="character", owner=ME)
    lib.upsert(Entry(key="身世", title="身世", body="本体里的说法。",
                     origin=EntryOrigin(kind="manual", turn=0)))
    store.save_library(lib, own)
    _join(acc, ME)
    acc.execute(ME, "revise", {"key": "身世", "body": "改过的说法。"}, turn=9)
    entry = _live(acc, ME).entries["身世"]
    assert entry.body == "改过的说法。"
    assert entry.origin.kind == "manual" and entry.origin.turn == store.BASELINE_TURN


def test_revise_keeps_previous_scene_entry_off_the_truncation_list(tmp_path):
    """修订**上一场**留下的 kind=scene 条目不重盖轮次——它是散场保留的长期记忆，不是本场产物。

    副本初始化时本体条目一律带哨兵轮次 -1（knowledgestore 模块 docstring 第 2 条），意思是
    "这条不是本场产生的"。若 revise 只看 kind == "scene" 就盖成本场轮次，store 的截断判据
    （kind=="scene" and turn>cut）立刻成立：撤回本场一条推进，删掉的是角色的整条旧记忆。
    条目文件没了、索引渲染成空串、read_entry 回一句"还什么都没记过"——用户看到的是
    "他突然不记得自己的身世了"，而磁盘上没有任何提示。
    """
    acc = _access(tmp_path)
    own = store.library_dir("character", ME, root=acc.libraries_root)
    lib = store.Library(name=ME, scope="character", owner=ME)
    lib.upsert(Entry(key="旧伤", title="旧伤", body="上一场留下的长期记忆。",
                     origin=EntryOrigin(kind="scene", scene="上一场", turn=3)))
    store.save_library(lib, own)
    _join(acc, ME)
    assert _live(acc, ME).entries["旧伤"].origin.turn == store.BASELINE_TURN

    acc.execute(ME, "revise", {"key": "旧伤", "body": "本场的补充说法。"}, turn=5)
    entry = _live(acc, ME).entries["旧伤"]
    assert entry.origin.turn == store.BASELINE_TURN         # 哨兵没被盖掉
    assert entry.origin.scene == "上一场"                    # 出处也不许改

    # 撤回一段更早的推进（cut=5 ≥ 修订轮次 5）：这次修订落在 cut **之内**，不回退（§5.3.1）。
    report = acc.truncate_scene_after_turn(ME, 5)
    assert report.dropped == 0 and report.restored == 0
    assert "旧伤" in _live(acc, ME).entries
    assert "旧伤" in acc.index_text(ME)
    assert "本场的补充说法。" in acc.execute(ME, "read_entry", {"key": "旧伤"}, turn=3)


def test_revise_keeps_same_named_scene_baseline_sentinel(tmp_path):
    """场景名跨 run 复用时同样不盖：判据不是"名字像本场"，而是那枚哨兵轮次（§5.3）。

    同名场景再跑一遍时，上一场结算进本体的条目在副本里也长着 "kind=scene + scene=本场"。
    只看这两个字段会把它误判成本场产物——拷进副本的一律 -1，本场写的永远不可能是 -1。
    """
    acc = _access(tmp_path)
    own = store.library_dir("character", ME, root=acc.libraries_root)
    lib = store.Library(name=ME, scope="character", owner=ME)
    lib.upsert(Entry(key="旧账", title="旧账", body="上一轮跑本场时留下的。",
                     origin=EntryOrigin(kind="scene", scene=SCENE, turn=7)))
    store.save_library(lib, own)
    _join(acc, ME)

    acc.execute(ME, "revise", {"key": "旧账", "body": "这次的补充。"}, turn=9)
    assert _live(acc, ME).entries["旧账"].origin.turn == store.BASELINE_TURN
    # 撤回轮次 9 那段：条目**不删**（哨兵让它躲过了截断），但本场的修订要回退（§5.3.1）。
    report = acc.truncate_scene_after_turn(ME, 8)
    assert report.dropped == 0 and report.restored == 1
    assert "旧账" in _live(acc, ME).entries
    assert _live(acc, ME).entries["旧账"].body == "上一轮跑本场时留下的。"


def test_revise_accepts_rough_name_like_read_entry(tmp_path):
    """修订与读取用同一套名字解析：模型把标题记岔一个字也该改得成。"""
    acc = _access(tmp_path)
    _copy(acc, ME, [_scene_entry("药铺的暗格", body="旧正文。")])
    acc.execute(ME, "revise", {"key": "药铺暗格", "body": "新正文。"}, turn=5)
    assert _live(acc, ME).entries["药铺的暗格"].body == "新正文。"


def test_revise_refuses_wide_subscribed_entry(tmp_path):
    """广域库条目**拒绝**修订（§3.4）：世界观不该被某个角色在场景里改写。

    拒绝也要给一句清楚的解释（"借来的，改不了，要记自己的说法就 remember"），
    干巴巴一句"失败"会让模型反复重试同一个动作。
    """
    wide = tmp_path / "libraries" / "wide" / WIDE
    lib = store.Library(name=WIDE, scope="wide")
    lib.upsert(Entry(key="朝局·三相", title="朝局·三相", summary="互相牵制", body="三相互相牵制。"))
    store.save_library(lib, wide)

    acc = _access(tmp_path, subscriptions={ME: [(WIDE, wide)]})
    _copy(acc, ME)
    out = acc.execute(ME, "revise", {"key": "朝局·三相", "body": "三相里有一家倒了。"}, turn=5)
    assert WIDE in out and "remember" in out
    assert store.load_library(wide).library.entries["朝局·三相"].body == "三相互相牵制。"
    assert _live(acc, ME).entries == {}


def test_revise_ensures_copy_before_looking(tmp_path):
    """副本还没建时 revise 也要改得成本体那条：先按 §5.1 建副本（把本体拷进来）再改。

    若直接在空副本里找，模型会收到"没有这条"——而他要改的明明是自己本体里的那条。
    """
    acc = _access(tmp_path)
    _own_lib(acc, ME, [_entry("身世", body="原来的说法。")])
    out = acc.execute(ME, "revise", {"key": "身世", "body": "改过的说法。"}, turn=3)
    assert "改好了" in out
    assert _live(acc, ME).entries["身世"].body == "改过的说法。"
    own = store.library_dir("character", ME, root=acc.libraries_root)
    assert store.load_library(own).library.entries["身世"].body == "原来的说法。"


def test_revise_missing_entry_is_text_with_hint(tmp_path):
    """副本里没有这条时，回"没有"并提示新事实用 remember——让模型的下一步有方向。"""
    acc = _access(tmp_path)
    _copy(acc, ME, [_entry("别的条目")])
    out = acc.execute(ME, "revise", {"key": "从没记过的事", "body": "X"}, turn=1)
    assert "没有" in out and "remember" in out


def test_revise_without_any_field_asks_for_one(tmp_path):
    """summary 与 body 都没给 = 没有内容的修订：明说"至少给一个"，别静默返回成功。"""
    acc = _access(tmp_path)
    _copy(acc, ME, [_scene_entry("药铺的暗格")])
    out = acc.execute(ME, "revise", {"key": "药铺的暗格"}, turn=1)
    assert "summary" in out and "body" in out


def test_revise_supersedes_other_near_titled_entry(tmp_path):
    """修订同样走"新压旧"：改过的这条成了现行说法，同副标题的其它条目转过时。

    两套说法并存时，模型读到哪条全凭缘分——修订正是"以我这条为准"的表达。
    """
    acc = _access(tmp_path)
    _copy(acc, ME, [
        _scene_entry("陈掌柜", title="陈掌柜", body="旧的。"),
        _scene_entry("陈掌柜的底细", title="陈掌柜", body="另一套说法。"),
    ])
    acc.execute(ME, "revise", {"key": "陈掌柜", "body": "改过的现行说法。"}, turn=5)
    lib = _live(acc, ME)
    assert lib.entries["陈掌柜"].body == "改过的现行说法。"
    assert lib.entries["陈掌柜的底细"].status == "outdated"
    assert lib.entries["陈掌柜的底细"].superseded_by == "陈掌柜"


# ------------------------------------------------------- 撤回：副本按轮次截断 --

def test_truncate_scene_after_turn_undoes_supersede_left_by_dropped_entry(tmp_path):
    """撤回把"这条被谁顶掉了"一并还原：被截掉的那条**从没发生过**（§5.3/§3.5）。

    同一场里先记「陈掌柜」（轮次 1）、再记一条同标题的「底细」（轮次 5），后者把前者压成
    过时。撤回轮次 5 之后，前者明明在本场之内就存在（轮次 1 ≤ cut=2），却永远留在 outdated
    上：索引默认不列过时条，角色再也看不见它；read_entry还会说"现在以「底细」为准"，而那条
    已经被删掉了——模型被引去读一个不存在的条目。撤回的语义是"角色不能带着从没发生过的事
    开口"，反过来**丢掉确实发生过的事**是同一类错。
    """
    acc = _access(tmp_path)
    _copy(acc, ME)
    acc.execute(ME, "remember", {"key": "陈掌柜", "title": "陈掌柜", "body": "旧"}, turn=1)
    acc.execute(ME, "remember", {"key": "底细", "title": "陈掌柜", "body": "新"}, turn=5)
    assert _live(acc, ME).entries["陈掌柜"].status == "outdated"

    report = acc.truncate_scene_after_turn(ME, 2)
    assert report.dropped == 1 and report.restored == 1        # 删了一条，还原了一条
    kept = _live(acc, ME).entries["陈掌柜"]
    assert kept.status == "active" and kept.superseded_by is None
    assert "陈掌柜" in acc.index_text(ME)
    out = acc.execute(ME, "read_entry", {"key": "陈掌柜"}, turn=6)
    assert "过时" not in out and "旧" in out


def test_truncate_scene_after_turn_restores_baseline_overwritten_in_place(tmp_path):
    """同键 remember 顶掉基线条目后撤回：把那条原文从本体放回副本（§5.2 本体整场只读）。

    一个键就是一个文件，同一件事没有"两条"可留（`knowledgestore.Library.upsert`），模型用
    同一个 key 重记时旧正文只能被顶掉——这是数据模型的硬约束，不是本模块能绕开的。但撤回
    之后，那条**先于本场存在**的认识必须回来：否则"撤销一次重记"等于让角色整条忘记自己的
    身世。本体库在整场戏里一个字没动（§5.2），它正是这条基线的原文来源。
    """
    acc = _access(tmp_path)
    own = store.library_dir("character", ME, root=acc.libraries_root)
    lib = store.Library(name=ME, scope="character", owner=ME)
    lib.upsert(Entry(key="陈掌柜", title="陈掌柜", body="他跛足，左眉有疤。",
                     origin=EntryOrigin(kind="manual", turn=store.BASELINE_TURN)))
    store.save_library(lib, own)
    copy = _join(acc, ME)

    acc.execute(ME, "remember", {"key": "陈掌柜", "title": "陈掌柜",
                                 "body": "他今天露了口风：那封信他认得。"}, turn=12)
    assert _live(acc, ME).entries["陈掌柜"].body == "他今天露了口风：那封信他认得。"
    assert store.load_pending(copy).revised == ["陈掌柜"]

    report = acc.truncate_scene_after_turn(ME, 2)
    assert report.dropped == 1 and report.restored == 1
    restored = _live(acc, ME).entries["陈掌柜"]
    assert restored.body == "他跛足，左眉有疤。"
    assert restored.origin.kind == "manual" and restored.origin.turn == store.BASELINE_TURN
    assert "陈掌柜" in acc.index_text(ME)
    assert "他跛足" in acc.execute(ME, "read_entry", {"key": "陈掌柜"}, turn=3)
    pending = store.load_pending(copy)          # 还原之后它不再是本场改动
    assert pending.added == [] and pending.revised == []


def test_truncate_scene_after_turn_restores_pre_scene_outdated_status(tmp_path):
    """还原基线条目时**连它原来的状态一起还**：本体里本来就过时的那条不该被"救活"（§3.5）。

    只把被压的条目翻成 active 是不够的——本体里它可能本来就带着"已被别的条目取代"的标注。
    错翻成现行说法，等于凭空多出一条早就被推翻的旧认识，索引里还多出一行。
    """
    acc = _access(tmp_path)
    own = store.library_dir("character", ME, root=acc.libraries_root)
    lib = store.Library(name=ME, scope="character", owner=ME)
    lib.upsert(Entry(key="陈掌柜", title="陈掌柜", body="旧的。", status="outdated",
                     superseded_by="真名",
                     origin=EntryOrigin(kind="manual", turn=store.BASELINE_TURN)))
    lib.upsert(Entry(key="真名", title="真名", body="他其实姓周。",
                     origin=EntryOrigin(kind="manual", turn=store.BASELINE_TURN)))
    store.save_library(lib, own)
    _join(acc, ME)

    acc.execute(ME, "remember",
                {"key": "陈掌柜的新说法", "title": "陈掌柜", "body": "新。"}, turn=5)
    assert _live(acc, ME).entries["陈掌柜"].superseded_by == "陈掌柜的新说法"

    report = acc.truncate_scene_after_turn(ME, 4)
    assert report.dropped == 1 and report.restored == 1
    back = _live(acc, ME).entries["陈掌柜"]
    assert back.status == "outdated" and back.superseded_by == "真名"
    assert "陈掌柜" not in acc.index_text(ME)


def test_truncate_scene_after_turn_noop_without_copy(tmp_path):
    """没有副本 = 空操作（老存档、还没进过场的角色都是这个状态），且不顺手造一个副本出来。"""
    acc = _access(tmp_path)
    report = acc.truncate_scene_after_turn(ME, 0)
    assert report.dropped == 0 and report.restored == 0 and report.warnings == []
    assert not store.scene_library_dir(acc.run_root, ME).exists()


def test_truncate_scene_after_turn_never_raises_on_io_error(tmp_path, monkeypatch):
    """撤回路径上的写盘失败不回抛（§13.1 同一口径：撤回不该因 IO 失败中断，更不该炸掉整块）。

    删除与还原是两次写盘：条目文件已经被截断了，撤回本身算数（`dropped` 照旧报 1）；
    还原那一次没写下去就不能算数（`restored` 报 0），而且要把这件事**说出来**——
    报告一个"还原了 1 条"而磁盘上什么都没变，用户只会以为这条记忆已经回来了。
    """
    acc = _access(tmp_path)
    own = store.library_dir("character", ME, root=acc.libraries_root)
    lib = store.Library(name=ME, scope="character", owner=ME)
    lib.upsert(Entry(key="旧事", title="旧事", body="本体里的说法。"))
    store.save_library(lib, own)
    _join(acc, ME)
    acc.execute(ME, "remember", {"key": "旧事", "title": "旧事", "body": "本场改的。"}, turn=3)

    def _boom(*a, **kw):
        raise OSError("磁盘满了")

    monkeypatch.setattr(store, "save_library", _boom)
    report = acc.truncate_scene_after_turn(ME, 1)
    assert report.dropped == 1 and report.restored == 0
    assert report.warnings                                   # 没写下去这件事不许静默


# ------------------------------------- 撤回：改过的旧认识要一起回退（§5.3.1） --

def test_truncate_scene_after_turn_rolls_back_revised_baseline_entry(tmp_path):
    """本场 revise 过的**基线**条目，撤回时还原成本体库的原文（§5.3.1）。

    这段改写的依据恰恰是从被撤回的下文里学来的（"原来那封信是伪造的"）。按 §5.3 的判据
    它 kind 不是 scene、turn 是哨兵，于是"不截"，留在副本里继续回喂；散场选「保留」还会
    把它永久并入本体——你再也退不回去。所以要的是**回退**（还原成本体原文），不是删条目：
    它本来就不是本场产物。还原之后它也不再是"本场改动"，否则结算仍会把它并进本体。
    """
    acc = _access(tmp_path)
    own = store.library_dir("character", ME, root=acc.libraries_root)
    lib = store.Library(name=ME, scope="character", owner=ME)
    lib.upsert(Entry(key="陈掌柜", title="陈掌柜", summary="跛足，眉上有疤",
                     body="他跛足，左眉有疤。", origin=EntryOrigin(kind="manual", turn=0)))
    store.save_library(lib, own)
    copy = _join(acc, ME)

    acc.execute(ME, "revise", {"key": "陈掌柜", "body": "他今天承认那封信是他伪造的。"}, turn=5)
    assert store.load_pending(copy).revised_turns == {"陈掌柜": 5}

    report = acc.truncate_scene_after_turn(ME, 2)
    assert report.dropped == 0 and report.restored == 1 and report.warnings == []
    back = _live(acc, ME).entries["陈掌柜"]
    assert back.body == "他跛足，左眉有疤。"                 # 逐字还原本体原文
    assert back.summary == "跛足，眉上有疤"
    assert back.origin.kind == "manual" and back.origin.turn == store.BASELINE_TURN
    pending = store.load_pending(copy)                       # 还原之后它不再是本场改动
    assert pending.revised == [] and pending.revised_turns == {}
    assert "陈掌柜" in acc.index_text(ME)
    assert "他跛足" in acc.execute(ME, "read_entry", {"key": "陈掌柜"}, turn=3)


def test_truncate_scene_after_turn_keeps_baselines_it_did_not_roll_back(tmp_path):
    """没被本场改过的基线一字不动；修订轮次**不晚于** cut 的也不回退（§5.3.1）。

    cut 的语义是"这一段之后发生的事都当作没发生"：轮次 ≤ cut 的修订就落在这一段之内，
    回退它等于删掉一段确实发生过的事——与截断那条判据（保留 turn ≤ cut）同一口径。
    """
    acc = _access(tmp_path)
    own = store.library_dir("character", ME, root=acc.libraries_root)
    lib = store.Library(name=ME, scope="character", owner=ME)
    for key in ("没动过的", "本场内改过"):
        lib.upsert(Entry(key=key, title=key, body="本体原文。",
                         origin=EntryOrigin(kind="manual", turn=0)))
    store.save_library(lib, own)
    _join(acc, ME)
    acc.execute(ME, "revise", {"key": "本场内改过", "body": "本场内改的说法。"}, turn=3)

    report = acc.truncate_scene_after_turn(ME, 3)
    assert report.dropped == 0 and report.restored == 0
    live = _live(acc, ME)
    assert live.entries["没动过的"].body == "本体原文。"
    assert live.entries["本场内改过"].body == "本场内改的说法。"


def test_truncate_scene_after_turn_rolls_back_status_of_revised_baseline(tmp_path):
    """回退基线条目时**连 status / superseded_by / indexed 一起还原**（§5.3.1）。

    revise 会把条目翻成现行说法（active，清掉 superseded_by）——那是对"本场新认识"的
    声明。撤回之后这份声明也消失了：本体里它本来就带着"已被别的条目取代"的标注，只还原
    正文而留着 active，等于凭空把一条早就被推翻的旧认识变回现行说法，索引里还多一行。
    """
    acc = _access(tmp_path)
    own = store.library_dir("character", ME, root=acc.libraries_root)
    lib = store.Library(name=ME, scope="character", owner=ME)
    lib.upsert(Entry(key="陈掌柜", title="陈掌柜", body="旧的。", status="outdated",
                     superseded_by="真名", indexed=False,
                     origin=EntryOrigin(kind="manual", turn=0)))
    lib.upsert(Entry(key="真名", title="真名", body="他其实姓周。",
                     origin=EntryOrigin(kind="manual", turn=0)))
    store.save_library(lib, own)
    _join(acc, ME)

    acc.execute(ME, "revise", {"key": "陈掌柜", "body": "本场改成现行说法。"}, turn=5)
    assert _live(acc, ME).entries["陈掌柜"].status == "active"

    assert acc.truncate_scene_after_turn(ME, 4).restored == 1
    back = _live(acc, ME).entries["陈掌柜"]
    assert back.body == "旧的。"
    assert back.status == "outdated" and back.superseded_by == "真名"
    assert back.indexed is False
    assert "陈掌柜" not in acc.index_text(ME)


def test_truncate_scene_after_turn_drops_new_entries_and_rolls_back_baselines(tmp_path):
    """一次撤回里两件事同时发生：本场新建的照旧删除，改过的基线还原（§5.3 + §5.3.1）。

    返回值要把两件事都报出来。只回一个数的话，M2 引擎没法记出一条能看懂的日志，用户也
    看不出"这次撤回到底动了什么"——而撤回恰恰是最需要留痕的动作。
    """
    acc = _access(tmp_path)
    own = store.library_dir("character", ME, root=acc.libraries_root)
    lib = store.Library(name=ME, scope="character", owner=ME)
    lib.upsert(Entry(key="基线", title="基线", body="本体原文。",
                     origin=EntryOrigin(kind="manual", turn=0)))
    store.save_library(lib, own)
    _join(acc, ME)
    acc.execute(ME, "revise", {"key": "基线", "body": "本场改过的说法。"}, turn=6)
    acc.execute(ME, "remember", {"key": "本场新记的", "title": "本场新记的"}, turn=6)

    report = acc.truncate_scene_after_turn(ME, 2)
    assert report.dropped == 1 and report.restored == 1
    live = _live(acc, ME)
    assert "本场新记的" not in live.entries
    assert live.entries["基线"].body == "本体原文。"


def test_truncate_scene_after_turn_leaves_old_pending_alone(tmp_path):
    """老副本的 pending 没有轮次字段 → **保守不回退**（§5.3.1 的降级口径）。

    不知道那次修订是第几轮，就无从判断它是否沾了被撤回的下文：往回退（猜一个轮次）会把
    一条本场现行说法静默还原成旧文，不退回则与这次改动之前的行为一致（那份副本本来就是
    这么跑过来的）。两害相权取轻者，且这一条只影响轮次缺失的那几条，别的照常处理。
    """
    acc = _access(tmp_path)
    own = store.library_dir("character", ME, root=acc.libraries_root)
    lib = store.Library(name=ME, scope="character", owner=ME)
    lib.upsert(Entry(key="基线", title="基线", body="本体原文。",
                     origin=EntryOrigin(kind="manual", turn=0)))
    store.save_library(lib, own)
    copy = _join(acc, ME)
    acc.execute(ME, "revise", {"key": "基线", "body": "本场改过的说法。"}, turn=6)
    raw = json.loads(store.pending_path(copy).read_text(encoding="utf-8"))
    raw.pop("revised_turns", None)                           # 模拟这次改动之前落盘的 pending
    store.pending_path(copy).write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    report = acc.truncate_scene_after_turn(ME, 2)
    assert report.dropped == 0 and report.restored == 0
    assert _live(acc, ME).entries["基线"].body == "本场改过的说法。"
    assert store.load_pending(copy).revised == ["基线"]


def test_truncate_scene_after_turn_reports_when_own_original_is_gone(tmp_path):
    """本体库里找不到那条键 → **不动它**，并把这件事报出来（不静默，§5.3.1）。

    副本目录会被复用（§5.1），本体那条也可能早已被删掉。这时没有可信的原文可还原：猜一句
    "翻成 active"是编造，删掉它是更大的损失。唯一诚实的做法是保持原样，并让调用方拿到
    "这条没能还原"——引擎记一条日志、界面上看得见，比悄悄无事发生强。它同时**留在本场
    改动清单上**：没回退成功就等于这次改动还在，散场时该让用户看见并自己决定怎么办。
    """
    acc = _access(tmp_path)
    _own_lib(acc, ME, [_entry("别的条目")])                   # 本体里**没有**「基线」这条
    copy = _copy(acc, ME, [_entry("基线", body="副本里的原文。",
                                  origin=EntryOrigin(kind="manual", turn=store.BASELINE_TURN))])
    acc.execute(ME, "revise", {"key": "基线", "body": "本场改过的说法。"}, turn=6)

    report = acc.truncate_scene_after_turn(ME, 2)
    assert report.dropped == 0 and report.restored == 0
    assert any("基线" in warning for warning in report.warnings)
    assert _live(acc, ME).entries["基线"].body == "本场改过的说法。"   # 一个字都没动
    assert store.load_pending(copy).revised == ["基线"]               # 仍是本场改动，看得见


def test_truncate_scene_after_turn_unpresses_baseline_pressed_by_rolled_back_revise(tmp_path):
    """回退的修订把它压下去过的条目也一起放开——"被压"这件事从没发生过（§3.5 + §5.3.1）。

    两条基线同名时，本场 revise 其中一条会把另一条标成过时（新压旧）。那条被压的条目
    明明在本场之前就是现行说法，撤回之后却永久留在 outdated 上：索引默认不列过时条，
    角色再也看不见它。所以回退一个修订时，得把"它压过谁"这件事一并抹掉——
    正是删掉本场条目时那条收尾的同一件事，只是这次的"没了"发生在**被还原**而不是被删的键上。
    """
    acc = _access(tmp_path)
    own = store.library_dir("character", ME, root=acc.libraries_root)
    lib = store.Library(name=ME, scope="character", owner=ME)
    lib.upsert(Entry(key="陈掌柜", title="陈掌柜", body="本体里的说法。",
                     origin=EntryOrigin(kind="manual", turn=0)))
    lib.upsert(Entry(key="陈掌柜的底细", title="陈掌柜", body="另一套说法。",
                     origin=EntryOrigin(kind="manual", turn=0)))
    store.save_library(lib, own)
    _join(acc, ME)

    acc.execute(ME, "revise", {"key": "陈掌柜", "body": "本场改的说法。"}, turn=5)
    assert _live(acc, ME).entries["陈掌柜的底细"].status == "outdated"

    report = acc.truncate_scene_after_turn(ME, 2)
    assert report.restored == 2                              # 改过的 + 被它压下去的
    live = _live(acc, ME)
    assert live.entries["陈掌柜"].body == "本体里的说法。"
    assert live.entries["陈掌柜的底细"].status == "active"
    assert live.entries["陈掌柜的底细"].superseded_by is None
    assert acc.index_text(ME).count("陈掌柜") == 2           # 两条都回到了索引上


def test_truncate_scene_after_turn_rolls_back_same_named_scene_baseline(tmp_path):
    """同名场景跨 run 复用时，"本场改过的基线"靠那枚**哨兵轮次**认，不是靠场景名（§5.3.1）。

    上一轮跑同名场景时保留进本体的条目，这一场拷进副本后长得跟本场产物一模一样
    （kind=scene + scene=本场），只有哨兵能把它认出来。认不出来就是两个错误同时发生：
    撤回会把它**整条删掉**（角色上一场保留的长期记忆没了），而且它在本场的修订也不会回退。
    这里要的是：条目留着，修订还原成本体原文。
    """
    acc = _access(tmp_path)
    own = store.library_dir("character", ME, root=acc.libraries_root)
    lib = store.Library(name=ME, scope="character", owner=ME)
    lib.upsert(Entry(key="旧账", title="旧账", body="上一轮跑本场时留下的。",
                     origin=EntryOrigin(kind="scene", scene=SCENE, turn=7)))
    store.save_library(lib, own)
    _join(acc, ME)

    acc.execute(ME, "revise", {"key": "旧账", "body": "这一场的补充说法。"}, turn=9)
    report = acc.truncate_scene_after_turn(ME, 8)
    assert report.dropped == 0 and report.restored == 1
    live = _live(acc, ME)
    assert live.entries["旧账"].body == "上一轮跑本场时留下的。"
    assert live.entries["旧账"].origin.turn == store.BASELINE_TURN
    assert live.entries["旧账"].origin.scene == SCENE      # 出处仍是本场（这是本体里的事实）


# --------------------- 回退的边界：cut 之内改过的东西一个字都不许动（§5.3/§5.3.1） --

def test_truncate_scene_after_turn_keeps_in_scene_rewrite_of_a_baseline_key(tmp_path):
    """同键重记基线条目的那一版，**落在 cut 之内就不许被"还原"掉**（§5.3 / §5.3.1）。

    开演前库里是「陈掌柜」的老说法；本场第 1 轮他听到新消息，用同一个键重记了一版；第 5 轮
    又记了一条标题近似的新条目，把前者压成过时。用户撤回第 5 轮（cut=3）——第 1 轮那次重记
    **确实发生过**（轮次 ≤ cut），按截断口径必须留下。

    可同键重记是一个键一个文件（`Library.upsert`）：在副本里它长得跟本场新建的条目一模一样。
    "放开被压的旧条目"那一步若按"本体里有原文就整条换回本体"处理，这段第 1 轮学到的东西就被
    静默换成开演前的旧文——副本里再没有那段话，而 `pending` 还写着"本场改过这条（第 1 轮）"，
    账实不符：散场选「保留」并回本体的还是旧文，用户第 1 轮学到的永久消失，报告里一声不吭。
    """
    acc = _access(tmp_path)
    _own_lib(acc, ME, [_entry("陈掌柜", body="本体原文。",
                              origin=EntryOrigin(kind="manual", turn=store.BASELINE_TURN))])
    copy = _join(acc, ME)
    acc.execute(ME, "remember", {"key": "陈掌柜", "title": "陈掌柜",
                                 "body": "本场第一轮重记的说法。"}, turn=1)
    acc.execute(ME, "remember", {"key": "陈掌柜的", "title": "陈掌柜的",
                                 "body": "第五轮的新说法。"}, turn=5)
    assert _live(acc, ME).entries["陈掌柜"].status == "outdated"

    report = acc.truncate_scene_after_turn(ME, 3)
    assert report.dropped == 1
    kept = _live(acc, ME).entries["陈掌柜"]
    assert kept.body == "本场第一轮重记的说法。"            # 第 1 轮那段话还在
    assert kept.status == "active" and kept.superseded_by is None
    assert store.load_pending(copy).revised_turns == {"陈掌柜": 1}   # 账实相符


def test_truncate_scene_after_turn_keeps_a_baseline_revised_inside_the_cut(tmp_path):
    """cut 之内的修订留下的正文同样是"确实发生过的事"，撤回不许把它换回原点（§5.3.1）。

    与上一条同一处边界的另一条路：这里是 revise（不是同键重记）。第 2 轮把基线改了一版，
    第 5 轮又记了一条同标题的新条目把它压下去；撤回第 5 轮时，若还原一律取本体原文，
    第 2 轮那版正文就没了——它落在 cut 之内，本该留下。
    """
    acc = _access(tmp_path)
    _own_lib(acc, ME, [_entry("基线", body="本体原文。",
                              origin=EntryOrigin(kind="manual", turn=store.BASELINE_TURN))])
    _join(acc, ME)
    acc.execute(ME, "revise", {"key": "基线", "body": "本场第 2 轮改的说法。"}, turn=2)
    acc.execute(ME, "remember", {"key": "近似标题的", "title": "基线",
                                 "body": "第 5 轮的新说法。"}, turn=5)
    assert _live(acc, ME).entries["基线"].status == "outdated"

    report = acc.truncate_scene_after_turn(ME, 3)
    assert report.dropped == 1
    kept = _live(acc, ME).entries["基线"]
    assert kept.body == "本场第 2 轮改的说法。"
    assert kept.status == "active" and kept.superseded_by is None


def test_truncate_scene_after_turn_warns_when_a_dropped_rewrite_loses_its_original(tmp_path):
    """同键重记顶掉了基线条目、而本体里那条又找不到了：**不许静默**把这条从副本里抹掉。

    副本目录会被复用（§5.1），本体那条也可能早已被删掉（用户自己删的、或这一场根本没有
    本体库）。撤回把本场那次重记删掉之后，这条**先于本场存在**的记忆就整条没了：没有原文
    可放回来，散场清单上也不会再有它的影子。唯一诚实的做法是把"这次丢了一条旧记忆"写进报告
    ——引擎记一条日志、界面上看得见。静默才是真正不可接受的：用户既看不见、也选不了保留。
    """
    acc = _access(tmp_path)
    own = _own_lib(acc, ME, [_entry("陈掌柜", body="本体原文。",
                                    origin=EntryOrigin(kind="manual", turn=store.BASELINE_TURN))])
    _join(acc, ME)
    acc.execute(ME, "remember", {"key": "陈掌柜", "title": "陈掌柜",
                                 "body": "本场重记的。"}, turn=5)
    shutil.rmtree(own)                        # 本体那条没了（用户删了 / 本场没有本体库）

    report = acc.truncate_scene_after_turn(ME, 2)
    assert report.dropped == 1 and report.restored == 0
    assert any("陈掌柜" in warning for warning in report.warnings)
    assert "陈掌柜" not in _live(acc, ME).entries           # 那条本场条目照旧被撤回


def test_truncate_scene_after_turn_does_not_warn_for_a_pure_scene_entry(tmp_path):
    """本场自己长出来的条目被撤回 → **零警告**：没有旧东西可丢，就不该有噪声（§13.1 的反面）。

    上一条的告警只在"这条键在开演前就有东西"时才发。判据是 `pending` 里的修订记录——本场
    全新记下的条目根本没有可丢的原文，为它记一条"没能还原"是假警报，日志里全是噪声之后，
    真的出了问题反倒看不见。
    """
    acc = _access(tmp_path)
    _copy(acc, ME)
    acc.execute(ME, "remember", {"key": "本场新记的", "title": "本场新记的"}, turn=5)

    report = acc.truncate_scene_after_turn(ME, 2)
    assert report.dropped == 1 and report.restored == 0
    assert report.warnings == []


# --------------------- 回退之后"谁压着谁"必须与 cut 那一刻一致（§3.5 / §5.3.1） --

def test_truncate_scene_after_turn_leaves_a_surviving_presser_in_charge(tmp_path):
    """被截掉的那条压过它、可 cut 之内还有一条更早的压制者：现行说法不许并列两条（§3.5）。

    开演前的「基线」（标题「陈掌柜」）是一条基线条目；本场第 3 轮记的 A 把它标成过时，
    第 5 轮记的 B 又把这两条都标成过时。撤回第 5 轮（cut=4）后，"B 压过它"这件事从没发生过，
    但 A 的压制**确实发生过**——这时把它整个放开成现行，副本里就同时有两条同标题的现行说法
    （索引并列两行、read_entry 把已被推翻的旧说法当现状回给模型），而磁盘与报告上没有任何
    痕迹。回退要还原的是 cut 那一刻的世界：基线仍由 A 压着。
    """
    acc = _access(tmp_path)
    _own_lib(acc, ME, [_entry("基线", title="陈掌柜", body="本体原文。",
                              origin=EntryOrigin(kind="manual", turn=store.BASELINE_TURN))])
    _join(acc, ME)
    acc.execute(ME, "remember", {"key": "A", "title": "陈掌柜", "body": "A 的说法。"}, turn=3)
    acc.execute(ME, "remember", {"key": "B", "title": "陈掌柜", "body": "B 的说法。"}, turn=5)
    live = _live(acc, ME)
    assert live.entries["基线"].superseded_by == "B" and live.entries["A"].superseded_by == "B"

    report = acc.truncate_scene_after_turn(ME, 4)
    assert report.dropped == 1
    live = _live(acc, ME)
    assert live.entries["A"].status == "active" and live.entries["A"].superseded_by is None
    back = live.entries["基线"]
    assert back.status == "outdated" and back.superseded_by == "A"   # 仍由 A 压着
    assert back.body == "本体原文。"
    assert acc.index_text(ME).count("陈掌柜") == 1                   # 索引里只留一条现行


def test_truncate_scene_after_turn_rolls_back_the_whole_press_chain(tmp_path):
    """整条压制链一起回到 cut 那一刻：被回退的那条不该把自己压过的人也放成现行（§3.5）。

    本场第 1 轮记的「陈掌柜X」压掉基线条目，第 2 轮记的 J 又把前两条都压掉；第 5 轮 revise
    基线条目，反过来把 X 和 J 都压住。撤回第 5 轮（cut=3）之后，X 与基线条目都该回到
    "由 J 压着"的状态——只还原正文与 J 的那一下压制，会让三条同标题条目全变成现行说法。
    """
    acc = _access(tmp_path)
    _own_lib(acc, ME, [_entry("陈掌柜", body="本体里的老说法。",
                              origin=EntryOrigin(kind="manual", turn=store.BASELINE_TURN))])
    _join(acc, ME)
    acc.execute(ME, "remember", {"key": "陈掌柜X", "title": "陈掌柜",
                                 "body": "X 的说法。"}, turn=1)
    acc.execute(ME, "remember", {"key": "J", "title": "陈掌柜", "body": "J 的说法。"}, turn=2)
    acc.execute(ME, "revise", {"key": "陈掌柜", "body": "本场改过的说法。"}, turn=5)

    report = acc.truncate_scene_after_turn(ME, 3)
    assert report.dropped == 0
    live = _live(acc, ME)
    assert live.entries["J"].status == "active" and live.entries["J"].superseded_by is None
    for key in ("陈掌柜", "陈掌柜X"):
        assert live.entries[key].status == "outdated"
        assert live.entries[key].superseded_by == "J"
    assert live.entries["陈掌柜"].body == "本体里的老说法。"     # 第 5 轮的修订已回退
    assert acc.index_text(ME).count("陈掌柜") == 1              # 只有 J 是现行


def test_truncate_scene_after_turn_restores_the_press_chain_of_a_reverted_revision(tmp_path):
    """回退一次修订，要把它**改之前**的压制关系一并还回来（§5.3.1 + §3.5）。

    开演前的 X（标题「陈掌柜」）是本场第 5 轮那条同标题新说法 K 的受害者（X 过时←K）；
    第 7 轮他把 X 改成现行，反过来把 K 压住。撤回第 7 轮（cut=6）后，X 应回到"第 5 轮结束时"
    的样子——本体原文 **+ 过时←K**，K 回到现行。只还原正文、不管压制链，就会把 X 也摆成
    现行：两条同标题的现行说法并列在索引里，模型照着被推翻的旧正文开口。
    """
    acc = _access(tmp_path)
    _own_lib(acc, ME, [_entry("X", title="陈掌柜", body="本体里的老说法。",
                              origin=EntryOrigin(kind="manual", turn=store.BASELINE_TURN))])
    _join(acc, ME)
    acc.execute(ME, "remember", {"key": "K", "title": "陈掌柜", "body": "本场新说法。"}, turn=5)
    acc.execute(ME, "revise", {"key": "X", "body": "中途改回老说法。"}, turn=7)
    assert _live(acc, ME).entries["X"].status == "active"

    report = acc.truncate_scene_after_turn(ME, 6)
    assert report.dropped == 0 and report.warnings == []
    live = _live(acc, ME)
    assert live.entries["K"].status == "active" and live.entries["K"].superseded_by is None
    back = live.entries["X"]
    assert back.status == "outdated" and back.superseded_by == "K"
    assert back.body == "本体里的老说法。"
    assert acc.index_text(ME).count("陈掌柜") == 1            # 只有 K 是现行


def test_truncate_scene_after_turn_points_at_the_latest_presser(tmp_path):
    """重新认压制者时挑的是**最晚写下的那条**，不是库里位置最靠后的那条（§3.5）。

    "新压旧"压制者可能不止一个：第 2 轮记的 K2 压过它，第 6 轮记的 K1 又压过它（两条都与它
    同副标题）。回退第 7 轮那次修订之后，它还该由**最后**压它的那条罩着——否则 read_entry
    会说"现在以 K2 为准"，而 K2 自己早就被 K1 压住了：提示把模型引向一条已经被推翻的旧说法。

    挑法不能看库里的位置：一个键一个文件，同键重记是**原位**覆盖（`Library.upsert` 不挪
    顺序位），所以"位置靠后"与"写得晚"根本不是一回事。轮次才是这里真正的时钟。
    """
    acc = _access(tmp_path)
    _own_lib(acc, ME, [
        _entry("V", title="甲", body="V 本体原文。",
               origin=EntryOrigin(kind="manual", turn=store.BASELINE_TURN)),
        _entry("K1", title="甲", body="K1 本体原文。",
               origin=EntryOrigin(kind="manual", turn=store.BASELINE_TURN)),
        _entry("K2", title="甲", body="K2 本体原文。",
               origin=EntryOrigin(kind="manual", turn=store.BASELINE_TURN))])
    _join(acc, ME)
    acc.execute(ME, "remember", {"key": "K2", "title": "甲", "body": "K2 第二轮的说法。"}, turn=2)
    acc.execute(ME, "remember", {"key": "K1", "title": "甲", "body": "K1 第六轮的说法。"}, turn=6)
    acc.execute(ME, "revise", {"key": "V", "body": "本场改过的 V。"}, turn=7)
    assert store.load_library(store.scene_library_dir(acc.run_root, ME)).library.order == \
        ["V", "K1", "K2"]                                     # 同键重记没有挪动顺序位

    report = acc.truncate_scene_after_turn(ME, 6)
    assert report.dropped == 0 and report.warnings == []
    live = _live(acc, ME)
    assert live.entries["K1"].status == "active"
    assert live.entries["K2"].status == "outdated" and live.entries["K2"].superseded_by == "K1"
    back = live.entries["V"]
    assert back.status == "outdated" and back.superseded_by == "K1"   # 最晚压它的那条
    assert back.body == "V 本体原文。"
    assert "现在以「甲」（键：K1）为准" in acc.execute(ME, "read_entry", {"key": "V"}, turn=8)


def test_truncate_scene_after_turn_does_not_release_victims_of_a_failed_rollback(tmp_path):
    """回退**没成功**的条目不算"没了"：它仍以本场改后的正文现行，压着的条目不许被放开。

    本体里那条被删掉时，工具层无从取回原文，于是那条**留在副本里、一个字没动**（并记一条
    warning，见 `reports_when_own_original_is_gone`）。此时它仍是现行的说法、仍压着对手——
    把对手放成现行，副本里就多出一条同标题的现行说法；报告还会说"还原了 1 条"，与磁盘上的
    实际情况正好相反（warning 说没还原、restored 说还原了），照着日志没法定出副本的状态。
    """
    acc = _access(tmp_path)
    own = _own_lib(acc, ME, [
        _entry("K", title="陈掌柜", body="本体 K 原文。",
               origin=EntryOrigin(kind="manual", turn=store.BASELINE_TURN)),
        _entry("X", title="陈掌柜", body="本体 X 原文。",
               origin=EntryOrigin(kind="manual", turn=store.BASELINE_TURN))])
    _join(acc, ME)
    acc.execute(ME, "revise", {"key": "K", "body": "本场改过的 K。"}, turn=7)
    assert _live(acc, ME).entries["X"].superseded_by == "K"
    store.delete_entry(own, "K")                 # 本体那条被删了：原文再也取不回来

    report = acc.truncate_scene_after_turn(ME, 6)
    assert report.restored == 0
    assert any("K" in warning for warning in report.warnings)
    live = _live(acc, ME)
    assert live.entries["K"].status == "active"
    assert live.entries["K"].body == "本场改过的 K。"          # 没能还原 → 一个字没动
    assert live.entries["X"].status == "outdated"              # 压制它的那条还在，不许放开
    assert live.entries["X"].superseded_by == "K"


# ------------------------------------------------------------- 取用通道 recall --

def test_execute_read_entry_notes_recall_for_this_turn(tmp_path):
    """read_entry 命中即登记取用（§6.4）：think 想起来的东西要能延续到本块的 speak。

    不登记的话，角色想完又说不出刚想起来的事——等于白查一轮（那一轮是真花了钱的）。
    """
    acc = _access(tmp_path)
    _copy(acc, ME, [_entry("药铺的暗格", body="夹层在柜台下。")])
    acc.execute(ME, "read_entry", {"key": "药铺的暗格"}, turn=4)
    assert "夹层在柜台下。" in acc.recall_text(ME, 4)
    assert acc.recall_text(ME, 5) == ""               # 别的轮次不带过来


def test_note_recall_row_shape_and_location(tmp_path):
    """recall.jsonl 落在 `runs/<场景>/<角色名>/`（与私有记忆同级），一行四个字段（§6.4）。

    行形状是硬约定：speak 侧按 turn 取，界面上也按它读；少一个 title 就少一层可读性。
    """
    acc = _access(tmp_path)
    acc.note_recall(ME, 2, "药铺的暗格", "夹层在柜台下。", title="药铺的暗格")
    path = Path(acc.run_root) / ME / RECALL_FILENAME
    assert path.is_file()
    rows = _recall_rows(acc, ME)
    assert rows == [{"turn": 2, "key": "药铺的暗格", "title": "药铺的暗格",
                     "text": "夹层在柜台下。"}]


def test_note_recall_dedupes_same_turn_and_key(tmp_path):
    """同一块里重复取用同一条目只留一行（后一次覆盖前一次）。

    重复行会让 speak 的提示词里同一段正文出现两遍——白烧 token，还会让模型以为
    "这事被强调了两次"。
    """
    acc = _access(tmp_path)
    acc.note_recall(ME, 2, "K", "第一版。", title="K")
    acc.note_recall(ME, 2, "K", "第二版。", title="K")
    acc.note_recall(ME, 3, "K", "下一轮再取一次。", title="K")
    rows = _recall_rows(acc, ME)
    assert [r["text"] for r in rows] == ["第二版。", "下一轮再取一次。"]
    assert acc.recall_text(ME, 2) == "第二版。"
    assert acc.recall_text(ME, 3) == "下一轮再取一次。"


def test_recall_text_joins_multiple_reads_and_skips_empty(tmp_path):
    """本块取了多条时按顺序拼成一段；空正文不留多余空行。"""
    acc = _access(tmp_path)
    acc.note_recall(ME, 1, "A", "甲。", title="A")
    acc.note_recall(ME, 1, "B", "", title="B")
    acc.note_recall(ME, 1, "C", "丙。", title="C")
    assert acc.recall_text(ME, 1) == "甲。\n\n丙。"


def test_recall_absent_file_is_empty_and_truncate_is_noop(tmp_path):
    """文件不存在 = 空（老存档、还没查过库的角色都是这个状态）；截断也当无事发生。"""
    acc = _access(tmp_path)
    assert acc.recall_text(ME, 1) == ""
    assert acc.truncate_recall_after_turn(ME, 0) is None
    # 不存在的账本不该被"截断"顺手创建出来（空文件会让下一步误以为本块查过库）。
    assert not (Path(acc.run_root) / ME / RECALL_FILENAME).exists()


def test_truncate_recall_keeps_turns_up_to_cut(tmp_path):
    """按轮次截断：保留 turn ≤ cut（§5.3，口径与 memory.truncate_after_turn 对齐）。

    撤回某条推进后，角色不能还带着"从没发生过的事"开口——那是同一类 bug 的第四次复发。
    文件不存在、截断后为空都不抛。
    """
    acc = _access(tmp_path)
    for turn, key in ((1, "A"), (2, "B"), (3, "C")):
        acc.note_recall(ME, turn, key, f"{key}的正文。", title=key)
    acc.truncate_recall_after_turn(ME, 2)
    rows = _recall_rows(acc, ME)
    assert [r["turn"] for r in rows] == [1, 2]
    assert acc.recall_text(ME, 3) == ""
    acc.truncate_recall_after_turn(ME, 2)             # 幂等：再来一次也不变
    assert [r["turn"] for r in _recall_rows(acc, ME)] == [1, 2]


def test_recall_bad_lines_are_skipped_and_missing_turn_counts_as_zero(tmp_path):
    """坏行跳过、缺轮次按 0 计（= 最早，绝不会被误截掉）——照 memory._read_jsonl 的口径。

    截断把一行读不出来的记录"顺手"当成大于 cut 丢掉，就等于撤回时悄悄删掉了不该删的
    回喂内容；而直接把坏行当异常抛出去，又会炸掉整块 speak。
    """
    acc = _access(tmp_path)
    acc.note_recall(ME, 5, "新", "新取的。", title="新")
    path = Path(acc.run_root) / ME / RECALL_FILENAME
    with path.open("a", encoding="utf-8") as fh:
        fh.write("这不是 json\n")
        fh.write("\"这是字符串不是对象\"\n")
        fh.write(json.dumps({"key": "老", "title": "老", "text": "没有轮次的老记录。"},
                            ensure_ascii=False) + "\n")
    assert acc.recall_text(ME, 0) == "没有轮次的老记录。"
    acc.truncate_recall_after_turn(ME, 1)
    rows = _recall_rows(acc, ME)
    assert [r.get("key") for r in rows] == ["老"]
    assert acc.recall_text(ME, 5) == ""               # turn=5 的那条被截掉了


# --------------------------------------------- 契约硬化：坏轮次 / 深拷贝 / 上限 --

def test_recall_methods_tolerate_unreadable_turn(tmp_path):
    """三个 recall 方法的 docstring 都写着"绝不抛"，坏轮次就必须真的不抛（§13.1）。

    M2 的引擎直接调它们，而轮次来自外部：一个坏值（None、模型给的怪串）炸掉的是整块
    think，worker 还会把整块重试——重试即重复计费。坏轮次按 0 计（= 最早，绝不会被
    误截掉），与 `_row_turn` 对老数据的口径一致：记还是要记得下来。
    """
    acc = _access(tmp_path)
    _copy(acc, ME, [_entry("K", body="K 的正文。")])
    acc.note_recall(ME, None, "K", "坏轮次也记得下来。", title="K")
    acc.note_recall(ME, "不是数字", "L", "另一个坏轮次。", title="L")
    assert acc.recall_text(ME, None) == acc.recall_text(ME, 0)
    assert acc.recall_text(ME, "不是数字") == "坏轮次也记得下来。\n\n另一个坏轮次。"
    assert "K 的正文。" in acc.execute(ME, "read_entry", {"key": "K"}, turn=None)


def test_truncate_recall_with_unreadable_cut_deletes_nothing(tmp_path):
    """撤回位置（cut）读不出来 → 一条都不删，绝不抛。

    与"坏轮次按 0 计"相反：0 对**条目轮次**意味着"最早、绝不误截"，对**撤回位置**却意味着
    "把本轮以内的取用一次删光"（包括刚发生的这一轮）。判据读不出来时唯一安全的动作是什么
    都不做，下次撤回再试。
    """
    acc = _access(tmp_path)
    for turn in (1, 5):
        acc.note_recall(ME, turn, f"K{turn}", f"第{turn}轮取的。", title=f"K{turn}")

    assert acc.truncate_recall_after_turn(ME, None) is None
    assert acc.truncate_recall_after_turn(ME, "五") is None
    assert acc.recall_text(ME, 1) == "第1轮取的。"
    assert acc.recall_text(ME, 5) == "第5轮取的。"
    acc.truncate_recall_after_turn(ME, 3)             # 正常的 cut 照旧生效
    assert acc.recall_text(ME, 1) == "第1轮取的。"
    assert acc.recall_text(ME, 5) == ""


def test_tools_returns_a_deep_copy(tmp_path):
    """`tools()` 返回**深拷贝**：调用方就地改一下，改不到模块级常量（§6.2）。

    浅拷贝只复制了外层 list，里面的 dict / description 还是全局那一份。graph 拿到 tools 后
    顺手补一句 description、或按角色删掉一个参数位，改的就是所有角色所有场景共用的常量，
    而且这种串味只在"另一个角色用了别的工具描述"时才显形——最难排查的那一类。
    """
    acc = _access(tmp_path)
    first = acc.tools()
    assert first == TOOL_SPECS and first is not TOOL_SPECS
    assert first[0] is not TOOL_SPECS[0]

    first[0]["function"]["description"] = "改坏了"
    first[0]["function"]["parameters"]["properties"].clear()
    first.append({"type": "function"})

    assert TOOL_SPECS[0]["function"]["description"] != "改坏了"
    assert TOOL_SPECS[0]["function"]["parameters"]["properties"]
    assert len(TOOL_SPECS) == 3
    assert acc.tools() == TOOL_SPECS


def test_index_text_ex_reports_how_many_rows_the_cap_dropped(tmp_path):
    """注入量上限：超出的**最旧**那些行被丢弃，丢了几行必须报出来（§10.2）。

    上限是为了让索引表不至于随角色的记性无限长下去——它恒在系统提示里，每一块都要重算、
    都要计费。丢弃必须可见：静默截断会让用户以为"他记得的东西变少了"却找不到原因，也让
    调参的人无法确认自己配的上限到底生效没有。口径与 `render_index` 一致：按显示顺序取
    前 N 条，自有在前、订阅在后，跨组一起算。
    """
    wide = tmp_path / "libraries" / "wide" / WIDE
    lib = store.Library(name=WIDE, scope="wide")
    lib.upsert(Entry(key="地理·西线", title="地理·西线", summary="靠山"))
    lib.upsert(Entry(key="朝局·三相", title="朝局·三相", summary="牵制"))
    store.save_library(lib, wide)

    acc = _access(tmp_path, subscriptions={ME: [(WIDE, wide)]})
    _copy(acc, ME, [_entry("一"), _entry("二"), _entry("三")])

    text, dropped = acc.index_text_ex(ME, max_entries=4)
    assert dropped == 1                                      # 5 行可见，只留前 4 行
    assert text.count(" — ") == 4
    assert "朝局·三相" not in text                            # 丢的是最后那一行（最旧）
    assert "一" in text and "地理·西线" in text

    assert acc.index_text_ex(ME, max_entries=9)[1] == 0       # 上限没到，一条不丢
    assert acc.index_text_ex(ME)[1] == 0                      # 不给上限 = 不截断
    # 上限是坏值（配置写错了）当没设上限：为它把整节索引变成空串，等于让角色突然什么都想不起来。
    broken, broken_dropped = acc.index_text_ex(ME, max_entries="不是数字")
    assert broken_dropped == 0 and "朝局·三相" in broken
    assert acc.index_text(ME) == acc.index_text_ex(ME)[0]     # index_text 转调它


def test_index_text_ex_empty_and_closed_report_nothing(tmp_path):
    """空库 / 关库：`("", 0)`——没有行可丢，也不能因为上限就把空串变成占位文本（§6.1）。

    空串那条是硬约束（无库角色的系统提示逐字节不变）。上限只作用于"有哪些行"，
    不改变"一行都没有就是空串"这件事。
    """
    acc = _access(tmp_path)
    _copy(acc, ME)                                           # 有副本目录，一条都没有
    assert acc.index_text_ex(ME, max_entries=3) == ("", 0)

    closed = _access(tmp_path, enabled=False)
    _copy(closed, ME, [_entry("一")])
    assert closed.index_text_ex(ME, max_entries=1) == ("", 0)


def test_read_entry_outdated_hint_gives_the_superseder_key(tmp_path):
    """"新压旧"最常见的样子就是**两条标题一样**：提示必须给出替代者的**键**（§6.2）。

    只给标题的话，读「陈掌柜」拿到的是"现在以「陈掌柜」为准"——那看起来就是它自己那一行，
    模型无从分辨该去读哪条。替代者已经被删掉时更不能说（照着去读只会再拿一句"没有这条"），
    那一条由 `test_read_entry_outdated_without_live_superseder_points_nowhere` 守着。
    """
    acc = _access(tmp_path)
    _copy(acc, ME, [
        _entry("陈掌柜", title="陈掌柜", body="旧的。", status="outdated",
               superseded_by="陈掌柜的底细"),
        _entry("陈掌柜的底细", title="陈掌柜", body="新的。"),
    ])
    out = acc.execute(ME, "read_entry", {"key": "陈掌柜"}, turn=1)
    note = next(line for line in out.splitlines() if "过时" in line)
    assert "陈掌柜的底细" in note          # 给的是**键**，不是又一个一模一样的标题
    assert "键" in note


def test_remember_clips_overlong_summary_and_body(tmp_path):
    """summary / body 有长度上限，超了**截断并在返回文本里说明**（§10.2）。

    索引表恒在系统提示里、每块都要重算并计费，而每条条目在索引里占一行 `标题 — 摘要`：
    一条失控的摘要（模型偶尔会把整段正文塞进 summary）能把整节撑爆，代价立刻体现在每一块
    的输入 token 上；正文同理——取用时它会被整段嵌进 speak 的提示词。截断要**说出来**，
    否则模型下一轮还以为自己写进去了，会一直写这么长。
    """
    acc = _access(tmp_path)
    _copy(acc, ME)
    out = acc.execute(ME, "remember",
                      {"key": "长条目", "title": "长条目",
                       "summary": "摘" * (MAX_SUMMARY_CHARS + 30),
                       "body": "文" * (MAX_BODY_CHARS + 30)}, turn=1)
    stored = _live(acc, ME).entries["长条目"]
    assert len(stored.summary) == MAX_SUMMARY_CHARS
    assert len(stored.body) == MAX_BODY_CHARS
    assert str(MAX_SUMMARY_CHARS) in out and str(MAX_BODY_CHARS) in out

    short = acc.execute(ME, "remember", {"key": "正常条目", "title": "正常条目",
                                         "summary": "一行摘要", "body": "一小段正文。"}, turn=1)
    assert "太长" not in short                                 # 没超限就一个字都不提


def test_revise_clips_overlong_summary_and_body(tmp_path):
    """revise 同样截断并说明：一次修订写多长，与首次记下对提示词的影响一模一样。"""
    acc = _access(tmp_path)
    _copy(acc, ME, [_scene_entry("条目")])
    out = acc.execute(ME, "revise", {"key": "条目", "body": "文" * (MAX_BODY_CHARS + 5)}, turn=2)
    assert len(_live(acc, ME).entries["条目"].body) == MAX_BODY_CHARS
    assert str(MAX_BODY_CHARS) in out


# ------------------------------------------ 订阅接线：本体库旁读到索引里（§3.4/§6.1） --

def _subscribe(own_dir: Path, root: Path, rel: str, name: str) -> None:
    """在角色**本体库目录**旁写一份订阅（§8.2 的落盘形态），模拟用户在编辑器里勾上它。"""
    (own_dir / "subscriptions.json").write_text(json.dumps(
        {"schema": 1, "libraries": [{"name": name, "path": rel}]},
        ensure_ascii=False), encoding="utf-8")


def _wide(tmp_path: Path, entries) -> Path:
    """造一座广域库，返回它的目录。"""
    path = tmp_path / "libraries" / "wide" / WIDE
    lib = store.Library(name=WIDE, scope="wide")
    for entry in entries:
        lib.upsert(entry)
    store.save_library(lib, path)
    return path


def test_index_text_reads_subscriptions_beside_the_own_library(tmp_path):
    """订阅挂在本体库目录旁（§8.2 的落点）：渲染时现读它，借来的那一组就进索引。

    这一步补的是"订阅只活在对话框里"的缺口——角色订阅了《庆国世界观》，他的索引里就
    必须出现「借自《庆国世界观》」那一组，否则订阅等于没订。
    """
    wide = _wide(tmp_path, [Entry(key="地理·西线", title="地理·西线",
                                  summary="靠山，常年封冻")])
    acc = _access(tmp_path)
    own = _own_lib(acc, ME, [_entry("药铺的暗格")])
    _subscribe(own, acc.libraries_root, f"wide/{WIDE}", WIDE)

    text = acc.index_text(ME)
    assert "药铺的暗格" in text
    assert f"借自《{WIDE}》" in text and "地理·西线" in text


def test_index_text_subscription_is_a_live_reference(tmp_path):
    """订阅是**活引用**（§3.4）：源库一改，订阅者的下一次渲染立刻是新内容。

    拷贝快照会让世界观各人手里版本不一致——设定本就该全员同步。
    """
    wide = _wide(tmp_path, [_entry("地理·西线", summary="靠山，常年封冻")])
    acc = _access(tmp_path)
    own = _own_lib(acc, ME, [_entry("药铺的暗格")])
    _subscribe(own, acc.libraries_root, f"wide/{WIDE}", WIDE)
    assert "靠山，常年封冻" in acc.index_text(ME)

    lib = store.load_library(wide).library
    lib.upsert(_entry("地理·西线", summary="官方改了口径"))
    store.save_library(lib, wide)
    assert "官方改了口径" in acc.index_text(ME), "源库改了，订阅者必须立刻看到"
    assert "靠山，常年封冻" not in acc.index_text(ME)


def test_index_text_is_byte_identical_without_subscriptions(tmp_path):
    """没订阅 → 输出与"完全没有这套机制"逐字节相同（§6.1 硬约束的延伸）。

    证据取两处对拍：磁盘上根本没有 subscriptions.json，与显式给一份空订阅表——
    两条路渲染出的索引必须一字不差，绝不能凭空多出一行组头或一个空行。
    """
    entries = [_entry("药铺的暗格", summary="柜台下第三块砖是空的")]
    acc = _access(tmp_path)
    _own_lib(acc, ME, entries)
    disk = acc.index_text(ME)

    # 对拍一：显式给一份空订阅表，结果必须一字不差。
    # 对拍二：拿渲染函数直接渲染同一批自有条目（订阅一个都不给），也必须一字不差。
    assert _access(tmp_path, subscriptions={ME: []}).index_text(ME) == disk
    assert store.render_library_index(
        store.library_dir("character", ME, root=acc.libraries_root)) == disk
    assert "借自" not in disk and disk != ""


def test_bad_subscription_file_does_not_break_the_index(tmp_path):
    """坏订阅文件只丢订阅这一层，自有条目照常渲染（读侧宽容，绝不抛）。"""
    acc = _access(tmp_path)
    own = _own_lib(acc, ME, [_entry("药铺的暗格")])
    (own / "subscriptions.json").write_text("{ 这不是 json", encoding="utf-8")
    text = acc.index_text(ME)
    assert "药铺的暗格" in text
    assert "借自" not in text


def test_pinned_entry_does_not_follow_the_source_library(tmp_path):
    """固化过的条目不随源库变（§3.4）：断开引用之后，源库再改我这份一字不动。

    这是"我对某一节的个人理解与官方版已经分叉"的用法——固化后还跟着源库变，
    这个动作就白做了。

    订阅**不动**（`subscriptions.json` 照旧写着整座库）：固化是单条的，界面上没有
    "只退订这一条"这个动作，所以断开引用不能靠取消订阅来做——那会把同库其余条目
    一起丢掉。用例刻意不删订阅文件：删了就等于替界面做了它做不到的事。
    """
    wide = _wide(tmp_path, [_entry("地理·西线", summary="官方口径")])
    acc = _access(tmp_path)
    own = _own_lib(acc, ME, [_entry("药铺的暗格")])
    _subscribe(own, acc.libraries_root, f"wide/{WIDE}", WIDE)
    store.pin_entry(wide, "地理·西线", own)          # 固化：拷进我的库、断开引用

    lib = store.load_library(wide).library
    lib.upsert(_entry("地理·西线", summary="官方改了口径"))
    store.save_library(lib, wide)

    # 覆盖检查：我的库那份正文就是固化那一刻的原文（不是被顶掉，也不是又借来的）。
    text = acc.index_text(ME)
    assert "官方改了口径" not in text
    assert text.count("地理·西线") == 1, "同一件事只能有一条在索引上"
    assert "官方口径" in text


def test_pinning_one_entry_does_not_unsubscribe_the_rest(tmp_path):
    """固化只断**这一条**：同库其余条目照旧借（§3.4 / §8.2）。

    订阅挂在整座库上（`subscriptions.json` 一个库一项），所以"断开引用"只能按**键**
    判：自有库里有了同键条目，借入的同键那条不再列。若拿"取消整座库"来实现固化，
    用户为了固化一条世界观就会丢掉同一座库里其余所有设定——这正是不许那么做的理由。
    """
    wide = _wide(tmp_path, [_entry("地理·西线", summary="官方口径"),
                            _entry("朝局·三相", summary="互相牵制")])
    acc = _access(tmp_path)
    own = _own_lib(acc, ME, [_entry("药铺的暗格")])
    _subscribe(own, acc.libraries_root, f"wide/{WIDE}", WIDE)
    store.pin_entry(wide, "地理·西线", own)

    text = acc.index_text(ME)
    assert text.count("地理·西线") == 1, "固化过的那一条不再从源库借一遍"
    assert "借自" in text and "朝局·三相" in text, "同库其余条目照旧借（没被连坐退订）"


def test_a_self_written_entry_also_shadows_the_borrowed_one_of_the_same_key(tmp_path):
    """自有条目压住**同键**的借入条目（§3.5 同键即同一件事），不限于固化那一条。

    固化只是这条规矩最常见的来路。若只对"固化过的"特判，就得另记一份"哪几条固化过"
    的账——而那份账与库里已有的条目是同一个事实的两种说法，迟早漂开。判据用键：
    同一件事在索引里列两行（我的口径 + 源库的口径），角色读到的是自相矛盾的两条。
    """
    wide = _wide(tmp_path, [_entry("地理·西线", summary="官方口径")])
    acc = _access(tmp_path)
    own = _own_lib(acc, ME, [_entry("地理·西线", summary="我自己写的口径")])
    _subscribe(own, acc.libraries_root, f"wide/{WIDE}", WIDE)

    text = acc.index_text(ME)
    assert text.count("地理·西线") == 1
    assert "我自己写的口径" in text and "官方口径" not in text


def test_explicit_subscriptions_win_over_the_file(tmp_path):
    """显式传入的订阅表优先于磁盘（调用方注入用；引擎不传时走磁盘那一份）。

    两条来源都给时绝不叠加：同一个源库列两次，索引上就是两组一模一样的「借自」。
    """
    wide = _wide(tmp_path, [_entry("地理·西线", summary="靠山")])
    acc = _access(tmp_path, subscriptions={ME: [(WIDE, wide)]})
    _own_lib(acc, ME, [_entry("药铺的暗格")])
    assert acc.index_text(ME).count(f"借自《{WIDE}》") == 1
