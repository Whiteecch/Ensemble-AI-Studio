"""knowledgesettle.py 的测试（设计文档 §7.2 纯总开关 / §7.3 离场挂起 / §3.5 冲突与归档 / §7.5 幂等）。

钉的都是**会静默出错**的地方——它们出事时不会抛，只会让角色的库悄悄变样：

  · 三类划分：本场新建、本场改过的**同键旧认识**、从本体拷来没动过的。第三类进不了
    所得（它本来就是本体的东西），可第二类**必须进**——`_revise` 刻意不给基线条目重盖
    轮次（哨兵 §5.3），所以"本场动没动过一条基线"的判据是**本场自己的账本**
    （`pending.json` 的 `revised` / `revised_turns`，§5.3.1 就是为撤回记下的这份），
    逐字比对内容只用来报警、绝不单独当判据：本体那条在副本建好之后变过时（用户在外面
    改、或另一场先结算），差分看起来就像"本场改过它"，照差分并回去等于把旧正文顶上新的；
  · **本场没动过的条目一个字都不往回写**：本体里那条被删了、改了名、或那份文件读不出来
    时，副本里开演前那份旧版本绝不能被当成"本场新建"并回本体（那会覆盖用户手写的正文，
    而且没有归档、没有警告，不可逆）；
  · 同键冲突必须**归档**（§3.5 那一段）：一个键一个文件，直接覆盖等于把本体里那条旧正文
    永久抹掉——"旧条一字不删"就成了空话。近似标题（键不同）天然是两个文件，只标过时；
  · 旧正文为空或与新条逐字相同 → 不归档（不制造噪声）；
  · 幂等（§7.5）：同一份所得合并两次，第二次不产生重复归档、磁盘一字不变；
  · 写盘失败**绝不抛**（结算失败不该让界面崩），报告里说清楚，且 `persisted=False`；
  · 离场挂起的查询**不改任何文件**——它会被界面反复调用（每次刷新面板）。

全部 `tmp_path`，且断言一律落在**磁盘上的文本**（`parse_entry_md` 读回来的字段 + 与
`render_entry_md` 的逐字比对），不看内存对象：本模块的产出就是文件，内存对了文件不对
等于没对。
"""
from __future__ import annotations

import json
from pathlib import Path

from harness import knowledgestore as store
from harness.knowledge import Entry, EntryOrigin, parse_entry_md, render_entry_md
from harness.knowledgesettle import (
    ARCHIVE_SUFFIX,
    SceneGain,
    collect_gain,
    discard_gain,
    merge_gain,
    pending_gain,
    settle,
)
from harness.prompters import build_summary_messages
from harness.schemas import CharacterCard

A = "甲"
SCENE = "贝克街221B"


# --------------------------------------------------------------------- 夹具 --

def _own_dir(tmp_path: Path, character: str = A) -> Path:
    """角色本体库目录（正式布局是 `app/libraries/characters/<角色名>`）。"""
    return store.library_dir("character", character, root=tmp_path / "libraries")


def _copy_dir(tmp_path: Path, scene: str = SCENE, character: str = A) -> Path:
    """本场副本目录 `runs/<场景>/<角色名>/library`（§4.1，与私有记忆同级）。"""
    return tmp_path / "runs" / scene / character / "library"


def _entry(key: str, *, title: str | None = None, summary: str = "", body: str = "",
           kind: str = "scene", scene: str = "", turn: int = 0,
           status: str = "active", superseded_by: str | None = None,
           indexed: bool = True) -> Entry:
    return Entry(key=key, title=title or key, summary=summary, body=body,
                 status=status, superseded_by=superseded_by, indexed=indexed,
                 origin=EntryOrigin(kind=kind, scene=scene, turn=turn))


def _save(entries: list[Entry], path: Path) -> Path:
    """把若干条条目装进一座库落盘（帮测试摆出"本体库里本来有什么"）。"""
    lib = store.Library()
    for entry in entries:
        lib.upsert(entry)
    store.save_library(lib, path)
    return path


def _files(root: Path) -> dict[str, str]:
    """整棵子树里每个文件的文本（逐字比对整个磁盘状态用）。"""
    return {str(p.relative_to(root)): p.read_text(encoding="utf-8")
            for p in sorted(Path(root).rglob("*")) if p.is_file()}


def _read(path: Path) -> Entry:
    """从磁盘读回一条条目（断言全部走这里，不碰内存对象）。"""
    return parse_entry_md(path.read_text(encoding="utf-8"))


def _archive_path(own: Path, key: str, index: int = 1) -> Path:
    """派生归档键 `（旧）` / `（旧2）`…（§3.5）对应的条目文件路径。"""
    suffix = ARCHIVE_SUFFIX if index == 1 else f"（旧{index}）"
    return own / "entries" / f"{key}{suffix}.md"


def _copy_with_gain(tmp_path: Path) -> tuple[Path, Path, SceneGain]:
    """摆一出最常见的戏：本体有一条旧认识，本场新建一条、改写一条。

    返回 (本体库目录, 副本目录, 本场所得)。
    """
    own = _own_dir(tmp_path)
    _save([_entry("药铺的暗格", summary="旧摘要", body="柜台下第三块砖是空的",
                  kind="manual")], own)
    copy = _copy_dir(tmp_path)
    store.create_scene_copy(own, copy)
    lib = store.load_library(copy).library
    lib.upsert(_entry("那封信", summary="信在夹层里", body="一封对折的信",
                      scene=SCENE, turn=2))
    lib.entries["药铺的暗格"] = lib.entries["药铺的暗格"].model_copy(
        update={"body": "柜台下第三块砖里有夹层", "summary": "夹层里有信",
                "origin": EntryOrigin(kind="manual", turn=store.BASELINE_TURN)})
    store.save_library(lib, copy)
    # 本场改动清单：工具层每写一条就记一笔（`remember` / `revise` 里调的 `record_pending`）。
    # 夹具照它记，"本场改过哪条基线"这件事才算真的发生过——判据是账本，不是内容比对。
    store.record_pending(copy, "那封信", turn=2)
    store.record_pending(copy, "药铺的暗格", revised=True, turn=2)
    return own, copy, collect_gain(copy, own)


# ------------------------------------------------------------------ 收集所得 --

def test_collect_gain_splits_added_revised_and_untouched(tmp_path):
    """三类划分（§7.2）：本场新建 → added；本体已有同键、本场改过 → revised；
    从本体拷来没动过 → untouched（**不进所得**，它本来就是本体的东西）。

    "本场动过基线"的判据是**本场自己的账本**：`pending.json` 的 `revised`（§5.3.1 记的就是
    "哪条基线条目在本场第几轮被改过"），本场对基线条目的 revise 不改它的 `origin`（哨兵轮次
    照旧），所以照 `origin.kind == "scene"` 去判会把它漏成 untouched——于是用户在本场所得
    清单上看不见"他把自己的旧认识改了"，散场选保留也并不过去。
    """
    own = _own_dir(tmp_path)
    _save([_entry("旧识", body="开演前的正文", kind="manual"),
           _entry("没动过", body="从本体拷来的", kind="manual")], own)
    copy = _copy_dir(tmp_path)
    store.create_scene_copy(own, copy)
    lib = store.load_library(copy).library
    lib.upsert(_entry("新见识", summary="刚记的", body="本场学到的", scene=SCENE, turn=3))
    lib.entries["旧识"] = lib.entries["旧识"].model_copy(update={"body": "本场改成的话"})
    store.save_library(lib, copy)
    store.record_pending(copy, "新见识", turn=3)
    store.record_pending(copy, "旧识", revised=True, turn=3)

    gain = collect_gain(copy, own, present=[A, "乙"])
    assert [e.key for e in gain.added] == ["新见识"]
    assert [e.key for e in gain.revised] == ["旧识"]
    assert [e.key for e in gain.untouched] == ["没动过"]
    assert gain.keys() == ["新见识", "旧识"]
    # digest 是喂给散场总结提示词的"键 + 标题 + 一行摘要"（§7.1）
    assert gain.digest() == [("新见识", "新见识", "刚记的"), ("旧识", "旧识", "")]
    assert gain.present == [A, "乙"] and gain.character == A and gain.scene == SCENE
    assert gain.is_empty is False


def test_collect_gain_infers_scene_and_character_from_the_path(tmp_path):
    """场景名与角色名能从副本路径推断（`runs/<场景>/<角色名>/library`，§4.1）：
    调用方少传一个参数就少一处对不上号的地方（界面上的"谁、哪一场"全靠它俩）。"""
    own, copy, gain = _copy_with_gain(tmp_path)
    assert gain.scene == SCENE and gain.character == A


def test_collect_gain_with_nothing_new_is_an_empty_gain_not_none(tmp_path):
    """本场什么都没得到 → **空 SceneGain，不是 None**：调用方不必到处判 None，
    而且"没有所得"与"还没收过"是两件事（§7.2 的清单要给你看，空清单也是一张清单）。"""
    own = _own_dir(tmp_path)
    _save([_entry("没动过", body="从本体拷来的", kind="manual")], own)
    copy = _copy_dir(tmp_path)
    store.create_scene_copy(own, copy)

    gain = collect_gain(copy, own)
    assert isinstance(gain, SceneGain)
    assert gain.is_empty is True
    assert gain.added == [] and gain.revised == [] and gain.keys() == []
    assert [e.key for e in gain.untouched] == ["没动过"]


def test_collect_gain_on_a_missing_copy_is_an_empty_gain(tmp_path):
    """副本不存在（老存档、没跑过结算的场景）→ 空所得、零警告，**绝不抛**（§9.2）。"""
    gain = collect_gain(_copy_dir(tmp_path), _own_dir(tmp_path))
    assert gain.is_empty and not gain.warnings


def test_collect_gain_carries_read_warnings(tmp_path):
    """副本里读不出来的条目文件只跳过它自己，但**必须报出来**（绝不静默）：
    用户手写坏了一行，看见"少了一条"却没有任何提示，是最坏的那种失败。"""
    own, copy, _ = _copy_with_gain(tmp_path)
    bad = store.entries_dir(copy) / "坏条目.md"
    bad.write_text("这不是条目", encoding="utf-8")
    gain = collect_gain(copy, own)
    assert any("坏条目.md" in w for w in gain.warnings)


def test_gain_digest_is_exactly_what_the_summary_prompt_eats(tmp_path):
    """接缝：`SceneGain.digest()` 的产物直接就是散场总结提示词的条目清单（§7.1）——引擎照
    "先收所得 → 拿 digest 写总结 → 再收一次 → 结算"这个顺序接线，所以这两处必须真的对得上；
    两边谁也不用认识谁的类型（一边给三元组，一边吃三元组）。"""
    _, _, gain = _copy_with_gain(tmp_path)
    msgs = build_summary_messages(CharacterCard(name=A), "甲: 那封信我看过了。", SCENE,
                                  entry_digest=gain.digest(), present=[A, "乙"])
    user = msgs[1]["content"]
    assert "[[那封信]]" in user and "信在夹层里" in user      # 键进双链、标题与摘要随行
    assert "[[药铺的暗格]]" in user and "夹层里有信" in user
    assert f"【在场的人】{A}、乙" in user


# ------------------------------------------------------------------ 合并：新增 --

def test_merge_added_writes_entry_into_own_library(tmp_path):
    """added → 写进本体库（§7.2「保留」）。断言落在磁盘文本上，不看内存对象。"""
    own, _, gain = _copy_with_gain(tmp_path)
    report = merge_gain(gain, own)

    assert (report.added, report.revised, report.archived, report.persisted) == (1, 1, 1, True)
    entry = gain.added[0]
    path = store.entry_path(own, entry.key)
    assert path.read_text(encoding="utf-8") == render_entry_md(entry)
    back = _read(path)
    assert back.body == "一封对折的信" and back.summary == "信在夹层里"
    assert back.status == "active" and back.origin.turn == 2 and back.origin.scene == SCENE
    order = json.loads(store.library_json_path(own).read_text(encoding="utf-8"))["order"]
    assert entry.key in order
    assert not report.warnings


def test_merge_creates_the_own_library_when_the_character_never_had_one(tmp_path):
    """角色从来没有过库 → 这一场就是他库里头的第一笔：库要建出来（scope/owner 按路径推断），
    不能因为没有库文件就把所得丢掉。"""
    own = _own_dir(tmp_path)
    report = merge_gain(SceneGain(added=[_entry("第一件事", body="正文", turn=1)]), own)

    assert report.added == 1 and report.warnings == []
    meta = json.loads(store.library_json_path(own).read_text(encoding="utf-8"))
    assert meta["name"] == A and meta["scope"] == "character" and meta["owner"] == A
    assert _read(store.entry_path(own, "第一件事")).body == "正文"


def test_merge_empty_gain_writes_nothing_at_all(tmp_path):
    """空所得 → 空操作：不建库、不写任何文件（凭空造一座空库只会在 `app/libraries/` 里
    多出一个打不开的空壳）。"""
    own = _own_dir(tmp_path)
    report = merge_gain(SceneGain(), own)
    assert (report.added, report.revised, report.archived) == (0, 0, 0)
    assert report.persisted is True and not report.warnings
    assert not own.exists()


# ------------------------------------------------------------------ 合并：冲突 --

def test_merge_revised_overwrites_and_archives_the_old_same_key_entry(tmp_path):
    """同键冲突必须**归档**（§3.5 那一段，M3 拍板）：一个键一个文件，直接覆盖等于把本体里
    那条旧正文永久抹掉，"旧条一字不删"就成了空话。

    归档条目 = 派生键 `<键>（旧）` 的一条过时条目：正文（连同标题/摘要）逐字保留、
    `status=outdated`、`superseded_by` 指向新条、`indexed=false`——它不该占索引表的位置，
    但仍读得到、仍在图上。全部断言落在磁盘文本上。
    """
    own, _, gain = _copy_with_gain(tmp_path)
    merge_gain(gain, own)

    live = _read(store.entry_path(own, "药铺的暗格"))
    assert live.body == "柜台下第三块砖里有夹层" and live.summary == "夹层里有信"
    assert live.status == "active" and live.superseded_by is None

    archive_path = _archive_path(own, "药铺的暗格")
    assert archive_path.is_file()
    archived = _read(archive_path)
    assert archived.body == "柜台下第三块砖是空的"          # 逐字
    assert archived.title == "药铺的暗格" and archived.summary == "旧摘要"
    assert archived.status == "outdated"
    assert archived.superseded_by == "药铺的暗格"
    assert archived.indexed is False


def test_merge_marks_a_near_title_entry_outdated_without_deleting_it(tmp_path):
    """近似标题冲突（§3.5 第二级：`knowledge.same_subject`）走"新压旧"：键不同 = 两个文件，
    旧条**原样留着**、只标过时（正文一字不动），既不删也不归档。"""
    own = _own_dir(tmp_path)
    _save([_entry("陈掌柜的", body="跛足，左眉有疤")], own)
    new = _entry("陈掌柜", body="其实不跛", scene=SCENE, turn=4)

    report = merge_gain(SceneGain(added=[new]), own)
    assert (report.added, report.archived, report.pressed) == (1, 0, 1)
    assert report.warnings == []

    old_path = store.entry_path(own, "陈掌柜的")
    assert old_path.is_file()
    old = _read(old_path)
    assert old.body == "跛足，左眉有疤" and old.status == "outdated"
    assert old.superseded_by == "陈掌柜"
    assert not _archive_path(own, "陈掌柜的").exists()
    assert "陈掌柜的" in _read(store.entry_path(own, "陈掌柜")).supersedes


def test_merge_does_not_archive_an_empty_or_identical_old_body(tmp_path):
    """旧正文为空、或与新条逐字相同 → **不归档**（§3.5：不制造噪声）。正文该覆盖照覆盖。"""
    own = _own_dir(tmp_path)
    _save([_entry("空条", body=""), _entry("同条", body="一模一样的正文", summary="旧摘要")], own)

    report = merge_gain(SceneGain(revised=[
        _entry("空条", body="后来写的", scene=SCENE, turn=1),
        _entry("同条", summary="只有摘要变了", body="一模一样的正文", scene=SCENE, turn=1),
    ]), own)

    assert report.archived == 0 and report.revised == 2
    assert not _archive_path(own, "空条").exists()
    assert not _archive_path(own, "同条").exists()
    assert _read(store.entry_path(own, "空条")).body == "后来写的"
    assert _read(store.entry_path(own, "同条")).summary == "只有摘要变了"


def test_merge_archive_key_collision_goes_to_old2(tmp_path):
    """派生键重名 → `（旧2）`、`（旧3）`…（§3.5）。撞名时绝不能覆盖那条已经归档过的旧正文
    ——它同样是"旧条一字不删"要保的东西。"""
    own = _own_dir(tmp_path)
    _save([_entry("那封信", body="第三版"),
           _entry(f"那封信{ARCHIVE_SUFFIX}", body="第一版", status="outdated",
                  superseded_by="那封信", indexed=False)], own)

    report = merge_gain(SceneGain(revised=[_entry("那封信", body="第四版", scene=SCENE)]), own)

    assert report.archived == 1
    assert _read(_archive_path(own, "那封信")).body == "第一版"      # 已归档的那份没被动过
    assert _read(_archive_path(own, "那封信", 2)).body == "第三版"
    assert _read(store.entry_path(own, "那封信")).body == "第四版"


def test_merge_reuses_an_archive_that_already_holds_the_same_old_body(tmp_path):
    """归档键的生成要认得"这份旧正文已经归档过了"：同一段旧文再被顶一次时复用它，
    不再多产一条 `（旧2）`——幂等靠的就是这一条（§7.5）。"""
    own = _own_dir(tmp_path)
    _save([_entry("那封信", body="第一版"),
           _entry(f"那封信{ARCHIVE_SUFFIX}", body="第一版", status="outdated",
                  superseded_by="那封信", indexed=False)], own)

    report = merge_gain(SceneGain(revised=[_entry("那封信", body="第二版", scene=SCENE)]), own)

    assert report.archived == 1
    assert _read(_archive_path(own, "那封信")).body == "第一版"
    assert not _archive_path(own, "那封信", 2).exists()
    assert _read(store.entry_path(own, "那封信")).body == "第二版"


def test_merging_the_same_gain_twice_changes_nothing_on_disk(tmp_path):
    """幂等（§7.5）：同一份所得合并两次，第二次**不产生重复归档、磁盘一字不变**。

    内容一字不差的条目按"没有可并的东西"跳过，跳过数进 `skipped`（报告里看得见——
    "什么都没做"不许是静默的）。第二次不按"再来一遍"处理是对的：重复归档会凭空造出
    `（旧2）`，而把那一条再"覆盖"一次还会把它翻成现行、把本体里后来压它的说法挤掉。
    """
    own, _, gain = _copy_with_gain(tmp_path)
    first = merge_gain(gain, own)
    snapshot = _files(own)

    second = merge_gain(gain, own)

    assert first.archived == 1
    assert second.archived == 0 and second.added == 0 and second.revised == 0
    assert second.skipped == len(gain.keys())
    assert _files(own) == snapshot
    assert not _archive_path(own, "药铺的暗格", 2).exists()


def _save_case_clashing(own: Path) -> None:
    """摆一座"键只差大小写"的本体库：`K.md` 一条 + 手写的 `z.md`（front-matter 里声明键 `k`）。

    这是合法输入（文件名与键不一致，`load_library` 只报一条警告），但它违反了"一个键一个
    文件"的物理约束——`K.md` 与 `k.md` 在 Windows 上是同一个文件。
    """
    lib = store.Library()
    lib.entries["K"] = _entry("K", body="大写的正文")
    lib.order = ["K"]
    store.save_library(lib, own)
    (own / "entries" / "z.md").write_text(
        render_entry_md(_entry("k", body="小写的正文")), encoding="utf-8", newline="\n")


def test_merge_refuses_when_the_own_library_already_has_case_clashing_keys(tmp_path):
    """本体库里已经有一对"只差大小写"的键 → **一整场都不并**，宁可这次什么都不做。

    理由：保存会把 `K.md` / `k.md` 当成同一个文件写，其中一条的正文被静默覆盖——那是不可逆
    的数据损失，而它既不是本场所得造成的，也不是本模块修得了的（修它在存储层）。报告里点名
    两个键（用户才动得了手），并让这一场留在"待结算"上等他清理完再来。
    """
    own = _own_dir(tmp_path)
    _save_case_clashing(own)
    before = _files(own)

    report = merge_gain(SceneGain(added=[_entry("好条目", body="照常并")]), own)

    assert report.persisted is False and (report.added, report.revised) == (0, 0)
    assert any("只差大小写" in w for w in report.warnings)
    assert _files(own) == before          # 一个字都没动：两条都还在


def test_merge_skips_an_entry_whose_key_collides_by_case_and_keeps_the_rest(tmp_path):
    """本场所得里有一条的键与本体库里某条**只差大小写** → 挡下的是**它自己**（§4.3：绝不
    静默改名、绝不顶掉住在那儿的另一条），其余条目照常并，报告里点名说清楚。

    `persisted=False` + `refused=1`：有该并的东西没并进去，这一场就**不算结清**（结算留在
    待结算上，用户清理完能重来）。挡下它而不是整场拒绝，是因为这条并不进去不影响别的条目
    ——可"并不进去"这件事必须让调用方分辨得出来：`settle` 正是拿 `persisted` 判"这次结算
    生效了吗"，少了这一位，那条记忆就永远留在副本里进不了本体，而报告看起来一切正常。"""
    own = _own_dir(tmp_path)
    _save([_entry("K", body="大写的正文")], own)

    report = merge_gain(SceneGain(added=[_entry("k", body="本场记下的"),
                                         _entry("好条目", body="照常并")]), own)

    assert report.persisted is False and report.added == 1 and report.refused == 1
    assert any("k」没能并进本体库" in w for w in report.warnings)
    assert _read(store.entry_path(own, "K")).body == "大写的正文"     # 大写那条没被动过
    assert not _archive_path(own, "k").exists()                      # 没白造一条归档
    assert _read(store.entry_path(own, "好条目")).body == "照常并"    # 其余照常并


def test_merge_reports_a_write_failure_instead_of_raising(tmp_path, monkeypatch):
    """写盘失败**绝不抛**（§7.4：引擎不弹窗、界面不许因为一次结算崩掉）：报告里说清楚，
    并且 `persisted=False`——调用方据此知道"一个字都没落盘"，别把它当成"并完了"。

    计数也一并归零：报告的是**磁盘上发生了什么**，不是"我打算做什么"。
    """
    own, _, gain = _copy_with_gain(tmp_path)
    before = _files(own)

    def boom(*args, **kwargs):
        raise OSError("磁盘满了")

    monkeypatch.setattr(store, "save_library", boom)
    report = merge_gain(gain, own)

    assert report.persisted is False
    assert report.warnings and "磁盘满了" in report.warnings[-1]
    assert (report.added, report.revised, report.archived) == (0, 0, 0)
    monkeypatch.undo()
    assert _files(own) == before


# --------------------------------------------------------------------- 丢弃 --

def test_discard_gain_merges_nothing_and_keeps_every_file(tmp_path):
    """丢弃（§7.2）：什么都不并，**副本连同这一场的 runs/ 存档一起留着（不删）**——
    你事后翻得到他那一场学了什么，只是没进本体。所以这里一个字都不许写。"""
    own, copy, gain = _copy_with_gain(tmp_path)
    before = _files(tmp_path)

    report = discard_gain(gain)

    assert _files(tmp_path) == before
    assert report.dropped == len(gain.added) + len(gain.revised) == 2
    assert report.scene == SCENE and report.character == A
    assert not report.warnings


# ----------------------------------------------------------------- 结算幂等 --

def test_settle_keep_merges_then_repeats_as_a_noop(tmp_path):
    """结算状态落在 `pending.json` 的 settled/outcome 上（§7.5，`mark_settled` 先到先得）。

    重复结算的行为**定为空操作**（`repeated=True` + 一句警告说明），而不是报错：报错会让
    界面上的一次重试变成事故现场的弹窗；而"再并一遍"在幂等的前提下虽无害，却会在报告里
    给出"又并了 1 条"这种骗人的数。空操作是把"已经结过了"如实说出来。
    """
    own, copy, gain = _copy_with_gain(tmp_path)
    first = settle(copy, gain, own, keep=True)

    assert first.repeated is False and first.outcome == store.OUTCOME_KEEP
    assert first.merged.added == 1 and first.merged.revised == 1
    pending = store.load_pending(copy)
    assert pending.settled is True and pending.outcome == store.OUTCOME_KEEP
    snapshot = _files(tmp_path)

    second = settle(copy, gain, own, keep=True)
    assert second.repeated is True and second.outcome == store.OUTCOME_KEEP
    assert second.merged.added == 0 and second.merged.revised == 0
    assert second.warnings                              # 说了"这一场已经结过了"，不静默
    assert _files(tmp_path) == snapshot


def test_settle_discard_wins_over_a_later_keep(tmp_path):
    """先到先得（§7.5）：一次手滑的重复点击**绝不能把"丢弃"翻成"保留"**——那会让角色
    凭空多出一批你从没同意永久化的记忆，而且不可逆。"""
    own, copy, gain = _copy_with_gain(tmp_path)
    before = _files(own)

    first = settle(copy, gain, own, keep=False)
    assert first.outcome == store.OUTCOME_DISCARD and first.merged.added == 0
    assert _files(own) == before                        # 丢弃 = 一个字都不并

    again = settle(copy, gain, own, keep=True)
    assert again.repeated is True and again.outcome == store.OUTCOME_DISCARD
    assert _files(own) == before
    assert store.load_pending(copy).outcome == store.OUTCOME_DISCARD


def test_settle_keep_that_failed_to_write_stays_pending(tmp_path, monkeypatch):
    """合并**没落盘**时不许标已结算：标了就等于把用户的"保留"兑成了一句空话，而且先到
    先得会把他的重试也一起挡掉。这场仍是"待结算"，下一次还能再来。"""
    own, copy, gain = _copy_with_gain(tmp_path)

    def boom(*args, **kwargs):
        raise OSError("磁盘满了")

    monkeypatch.setattr(store, "save_library", boom)
    report = settle(copy, gain, own, keep=True)

    assert report.merged.persisted is False and report.warnings
    assert store.load_pending(copy).settled is False

    monkeypatch.undo()
    retry = settle(copy, gain, own, keep=True)
    assert retry.repeated is False and retry.merged.added == 1
    assert store.load_pending(copy).settled is True


def test_settle_survives_a_failed_settlement_state_write(tmp_path, monkeypatch):
    """结算状态的写盘失败同样不许抛（§7.4）：合并那半已经做完了，报告如实说"状态没记下来、
    这一场下次还会被当成待结算"，重试是幂等的——不会重复归档，也不会把丢弃翻成保留。"""
    own, copy, gain = _copy_with_gain(tmp_path)

    def boom(*args, **kwargs):
        raise OSError("写不动")

    monkeypatch.setattr(store, "save_pending", boom)
    report = settle(copy, gain, own, keep=True)

    assert report.merged.added == 1 and report.merged.persisted is True
    assert any("结算状态没能记下来" in w for w in report.warnings)

    monkeypatch.undo()
    snapshot = _files(own)
    retry = settle(copy, gain, own, keep=True)
    assert retry.repeated is False and retry.merged.skipped == len(gain.keys())
    assert _files(own) == snapshot
    assert store.load_pending(copy).settled is True


# --------------------------------------------------------------- 离场挂起 --

def test_pending_gain_reports_an_unsettled_gain_and_writes_nothing(tmp_path):
    """离场挂起的数据侧（§7.3）：调用方能知道"谁、哪一场、还欠着几条待结算"，据此在界面上
    给一个"待结算"提示。**不改任何文件**——面板每刷新一次就调它一次，写盘等于把提示变成动作。"""
    own, copy, _ = _copy_with_gain(tmp_path)
    store.record_pending(copy, "那封信", turn=2)
    store.record_pending(copy, "药铺的暗格", revised=True, turn=2)
    before = _files(tmp_path)

    info = pending_gain(copy)

    assert _files(tmp_path) == before
    assert info.scene == SCENE and info.character == A
    assert info.added == ["那封信"] and info.revised == ["药铺的暗格"]
    assert info.keys() == ["那封信", "药铺的暗格"]
    assert info.is_pending is True and info.settled is False


def test_pending_gain_is_quiet_after_settlement_and_without_a_copy(tmp_path):
    """已结算 / 根本没有副本 → 都算"没有待结算的东西"（§7.3）。两个都不能抛：
    界面在角色还没进场、或刚结完账时照样会问一句"他欠着吗"。"""
    copy = _copy_dir(tmp_path)
    store.create_scene_copy(_own_dir(tmp_path), copy)    # 空的本体库：这里只要一个副本目录
    store.record_pending(copy, "那封信", turn=2)
    assert pending_gain(copy).is_pending is True

    store.mark_settled(copy, store.OUTCOME_KEEP)
    settled = pending_gain(copy)
    assert settled.settled is True and settled.is_pending is False
    assert settled.keys() == ["那封信"]                 # 记录留着（看得见这一场学了什么）

    empty = pending_gain(_copy_dir(tmp_path, scene="没跑过的场"))
    assert empty.is_pending is False and empty.keys() == [] and empty.settled is False


# ------------------------------------- 本体库与副本不一致：以本体为准 / 以账本为准 --
#
# 这一组钉的都是**同一件事的另一面**：结算要动的是用户的本体库（他的资产），而"副本里
# 有、本体里没有"绝不等于"本场新建"。本体在副本建好之后被改过、被删过、被改坏过（手写
# 这份文件本来就是设计里的正常用法，§4.2），三条路都必须以本体为准，并且**说出来**。

def _corrupt_own_file(own: Path, key: str, body: str) -> Path:
    """把本体库里某条条目的文件手改坏：正文换成 `body`，同时把 `indexed` 打成 `indexd`。

    这是"文件在、读不出来"的最小复现（手写笔误是最常见的来源）：文件**还在磁盘上**，
    只是 `load_library` 跳过它并给一条警告。本模块必须把"读不出来"与"根本没有这条"分开
    对待——后者是"本场新建"，前者是"用户手写的东西，一个字都不许盖"。
    """
    path = own / "entries" / f"{key}.md"
    path.write_text(
        render_entry_md(_entry(key, body=body)).replace("indexed:", "indexd:", 1),
        encoding="utf-8", newline="\n")
    return path


def test_collect_gain_does_not_bring_back_a_baseline_the_user_deleted(tmp_path):
    """本体库里那条在副本建好之后被用户删掉 → 副本里那份**基线**不是本场所得，绝不并回去。

    本场一个字都没动过它：用户点「保留」是把**本场学到的**并进去（§7.2"本场新增与本场
    修订的条目"），不是把本体按开演时的样子复原一遍。"用户删过"读成"本场新建"的后果是
    那条记忆自己长回来——删了又回来，用户只会以为程序在跟他作对。判据是副本那条带着
    基线哨兵（`BASELINE_TURN`，§5.3：从本体拷进来的一律盖这个标记），不是"本体里有没有
    这个键"。报告里要有一句，用户才知道自己的删除被尊重了。
    """
    own = _own_dir(tmp_path)
    _save([_entry("删掉的那条", body="用户删掉的正文", kind="manual")], own)
    copy = _copy_dir(tmp_path)
    store.create_scene_copy(own, copy)
    assert store.delete_entry(own, "删掉的那条") is True

    gain = collect_gain(copy, own)

    assert [e.key for e in gain.added] == [] and gain.keys() == []
    assert any("删掉的那条" in w for w in gain.warnings)      # 不静默：说一句没并回来

    report = merge_gain(gain, own)
    assert (report.added, report.revised) == (0, 0)
    assert not store.entry_path(own, "删掉的那条").exists()


def test_collect_gain_keeps_the_own_version_when_the_library_moved_on(tmp_path):
    """副本建好之后本体那条变过（用户在编辑器里改 / 另一场先结算了）→ 以**本体**为准。

    这是"拿内容比对当唯一判据"真正咬人的地方：副本里那条还是开演前的旧正文，本体里已经是
    更新过的正文，差分看起来就像"本场改过它"，于是旧正文被并回去顶掉新的那条、本体里新的
    那条降级成「（旧）」——用户点「保留」的初衷（把这一场学到的拿走）变成了"把自己后来改的
    东西倒回去"。判据是**本场自己的账本**：`pending.json` 的 `revised` / `revised_turns`
    记着本场改过哪条基线（§5.3.1），没记 = 本场没动过它。
    """
    own = _own_dir(tmp_path)
    _save([_entry("那封信", body="开演前的正文", kind="manual")], own)
    copy = _copy_dir(tmp_path)
    store.create_scene_copy(own, copy)
    # 副本建好之后，本体那条被改过（模拟另一场先结算，或用户在外面改）
    own_lib = store.load_library(own).library
    own_lib.entries["那封信"].body = "本体里更新过的正文"
    store.save_library(own_lib, own)
    # 本场自己只记了一条新的
    lib = store.load_library(copy).library
    lib.upsert(_entry("本场新记的", body="本场学到的", scene=SCENE, turn=2))
    store.save_library(lib, copy)
    store.record_pending(copy, "本场新记的", turn=2)

    gain = collect_gain(copy, own)

    assert [e.key for e in gain.added] == ["本场新记的"]
    assert [e.key for e in gain.revised] == []
    assert [e.key for e in gain.untouched] == ["那封信"]
    assert any("那封信" in w for w in gain.warnings)          # 以本体为准这件事看得见

    report = merge_gain(gain, own)
    assert report.added == 1 and report.refused == 0
    assert _read(store.entry_path(own, "那封信")).body == "本体里更新过的正文"   # 一个字没动
    assert _read(store.entry_path(own, "本场新记的")).body == "本场学到的"


def test_collect_gain_warns_instead_of_merging_a_baseline_without_a_record(tmp_path):
    """基线条目与本体不一致、账本里却查不到本场改过它 → **不并**，并把这件事说出来。

    两种来路在这里长得一模一样，而正确的处置是同一种（以本体为准，不并）：本体在外面被
    改过（上一条测试），或者本场的改动记录丢了（`pending.json` 被删/写坏，`load_pending`
    退回空清单）。第二种是本模块唯一会让"本场的修订"落空的情形，所以**绝不许静默**——
    报告里那句把两种可能都说出来，用户才判得出该不该手动处理。
    """
    own = _own_dir(tmp_path)
    _save([_entry("那封信", body="开演前的正文", kind="manual")], own)
    copy = _copy_dir(tmp_path)
    store.create_scene_copy(own, copy)
    lib = store.load_library(copy).library
    lib.entries["那封信"].body = "本场改成的话"          # 副本里改了……
    store.save_library(lib, copy)
    store.save_pending(copy, store.PendingChanges())     #……可记录没了（账本一片空白）

    gain = collect_gain(copy, own)

    assert [e.key for e in gain.revised] == [] and gain.keys() == []
    assert any("那封信" in w for w in gain.warnings)
    merge_gain(gain, own)
    assert _read(store.entry_path(own, "那封信")).body == "开演前的正文"


def test_collect_gain_reports_an_unreadable_own_entry_instead_of_overwriting_it(tmp_path):
    """本体库里那份文件读不出来（手写打错字段名）→ 它的正文一个字都不许被覆盖。

    `load_library` 跳过读不出来的条目并给一条警告，可"这个键不在内存索引里"不等于"这个键
    是本场新建的"：把副本里开演前那份旧正文并回去，就是拿旧版本顶掉用户手写的新正文，而且
    既没有归档（内存里从来没有过那份原文，归档不知道要保什么）、也没有任何警告——不可逆、
    事后无从察觉。所以：本体库的读警告必须带进报告，这条键不进所得（少并一条看得见，
    旧正文没了看不见）。
    """
    own = _own_dir(tmp_path)
    _save([_entry("药铺的暗格", summary="旧摘要", body="柜台下第三块砖是空的",
                  kind="manual")], own)
    copy = _copy_dir(tmp_path)
    store.create_scene_copy(own, copy)
    bad = _corrupt_own_file(own, "药铺的暗格", "柜台下第三块砖里有夹层（用户刚改的）")
    before = bad.read_text(encoding="utf-8")

    gain = collect_gain(copy, own)

    assert [e.key for e in gain.added] == [] and gain.keys() == []
    assert any("读不了" in w for w in gain.warnings)          # 本体库的读警告带进来了
    assert any("药铺的暗格" in w for w in gain.warnings)

    report = settle(copy, gain, own, keep=True)
    assert bad.read_text(encoding="utf-8") == before           # 用户手写的那份一个字没动
    assert not _archive_path(own, "药铺的暗格").exists()
    assert any("读不了" in w for w in report.warnings)         # 结算报告里也看得见


def test_merge_refuses_to_write_over_an_own_entry_file_it_cannot_read(tmp_path):
    """本场新建的键，在本体库那个位置上正好躺着一份**读不出来**的文件 → 这一条不并。

    这条路连副本都不需要出问题就能踩到：本体里那份坏文件根本没被拷进副本（读不出来），
    于是本场记下同名条目时"本体里没有这个键"，看起来完全像一次新建——写下去就是把用户
    手写的正文整份盖掉，连归档都拿不到原文（那份内容从没进过内存）。宁可这一条不并。
    """
    own = _own_dir(tmp_path)
    _save([_entry("别的条目", body="别的正文")], own)
    bad = own / "entries" / "新说法.md"
    bad.write_text(render_entry_md(_entry("新说法", body="用户手写的正文")).replace(
        "indexed:", "indexd:", 1), encoding="utf-8", newline="\n")
    before = bad.read_text(encoding="utf-8")

    report = merge_gain(
        SceneGain(added=[_entry("新说法", body="本场记下的", scene=SCENE, turn=1)]), own)

    assert report.added == 0 and report.refused == 1 and report.persisted is False
    assert bad.read_text(encoding="utf-8") == before
    assert any("读不出来" in w for w in report.warnings)
    assert not _archive_path(own, "新说法").exists()


def test_settle_keep_with_nothing_mergeable_stays_pending(tmp_path):
    """一条都没并进去、而且不是因为"内容一样没什么可并的" → **不许标记已结算**。

    标了就等于把用户的「保留」兑成一句空话：本体里什么都没多，那条记忆永远留在副本里，
    而先到先得又把他的重试一起挡掉（§7.5）。同一件"并不动"，整场拒绝那条路（大小写碰撞）
    留在待结算上，这条路却悄悄结清——两条路必须同命运，调用方也只有 `persisted` 能分辨。
    """
    own = _own_dir(tmp_path)
    _save([_entry("K", body="大写的正文")], own)
    copy = _copy_dir(tmp_path)
    store.create_scene_copy(own, copy)
    gain = SceneGain(scene=SCENE, character=A,
                     added=[_entry("k", body="本场记下的", scene=SCENE, turn=1)])

    report = settle(copy, gain, own, keep=True)

    assert report.repeated is False
    assert report.merged.persisted is False and report.merged.refused == 1
    assert any("只有大小写不同" in w for w in report.warnings)
    assert store.load_pending(copy).settled is False           # 仍是待结算，能重来
    # 本体里那条一个字没动（`K.md` 与 `k.md` 在 Windows 上就是同一个文件，只能读内容来判）
    assert _read(store.entry_path(own, "K")).body == "大写的正文"


def test_settle_keep_with_a_partially_mergeable_gain_stays_pending(tmp_path):
    """一部分并进去了、有一条被挡下 → 这一场同样**不算结清**，但已经并过的不受影响。

    "并进去一半"比"一条都没并"更接近"看起来一切正常"：报告里 added=1、警告只提那条被挡的，
    用户很容易以为是小事。可那条记忆还在副本里、进不了本体，而先到先得会把重试挡掉——所以
    留待结算，让他处理完（删掉那条大小写重名的、或修好那份读不出来的文件）再结一次；
    重试是幂等的：已经并过的不重复并，被挡下的那次才并进去。
    """
    own = _own_dir(tmp_path)
    _save([_entry("K", body="大写的正文")], own)
    copy = _copy_dir(tmp_path)
    store.create_scene_copy(own, copy)
    gain = SceneGain(scene=SCENE, character=A,
                     added=[_entry("k", body="本场记下的", scene=SCENE, turn=1),
                            _entry("好条目", body="照常并", scene=SCENE, turn=1)])

    first = settle(copy, gain, own, keep=True)
    assert (first.merged.added, first.merged.refused) == (1, 1)
    assert first.merged.persisted is False
    assert store.load_pending(copy).settled is False
    assert _read(store.entry_path(own, "好条目")).body == "照常并"

    # 用户清理干净（删掉那条大写重名的）之后重来：好条目不再重复并，被挡下的那条并进去
    assert store.delete_entry(own, "K") is True
    second = settle(copy, gain, own, keep=True)
    assert second.repeated is False
    assert (second.merged.added, second.merged.skipped, second.merged.persisted) == (1, 1, True)
    assert _read(store.entry_path(own, "k")).body == "本场记下的"
    assert _read(store.entry_path(own, "好条目")).body == "照常并"
    assert store.load_pending(copy).settled is True


def test_settle_keep_that_could_not_archive_the_old_body_stays_pending(tmp_path):
    """归档键放不下（键太长）→ 宁可不并那条、也不覆盖旧正文，而且这一场**不算结清**。

    §3.5 的同键归档是"旧条一字不删"的唯一落点：归档不出来就不许动那个文件。可"没并"
    与"没结清"是两件事——报告说 `persisted=False`，`settle` 才会把这一场留在待结算上，
    用户改短一点那个键（或改名）之后还能重来。
    """
    own = _own_dir(tmp_path)
    key = "长" * 79                       # 79 + 「（旧）」= 82 > 键长上限 80
    _save([_entry(key, body="旧正文")], own)
    copy = _copy_dir(tmp_path)
    store.create_scene_copy(own, copy)
    gain = SceneGain(scene=SCENE, character=A,
                     revised=[_entry(key, body="本场改成的旧认识", scene=SCENE, turn=1)])

    report = settle(copy, gain, own, keep=True)

    assert report.merged.persisted is False and report.merged.refused == 1
    assert report.merged.revised == 0
    assert _read(store.entry_path(own, key)).body == "旧正文"    # 旧正文一字没动
    assert store.load_pending(copy).settled is False


def test_merge_reproduces_the_copy_when_a_later_entry_supersedes_an_earlier_revision(tmp_path):
    """本场先改写了基线、之后又记了一条近似标题的新说法 → 合并必须重现**副本里的胜负**。

    副本里是按**真实先后**定的胜负（晚写的那条压住早写的那条，`knowledgetools` 的"新压旧"
    与本模块同一个 `same_subject` 判据）；合并顺序原先写死"先新增、后修订"，把这个先后丢了，
    结果整个反过来：后记的新说法反倒被标成过时、被自己推翻的旧认识回到现行，角色下一块
    读到的索引只剩下那条旧的。用户看到的清单说"新增：陈掌柜的"，本体里它却是过时的。
    """
    own = _own_dir(tmp_path)
    _save([_entry("陈掌柜", body="跛足，左眉有疤", kind="manual")], own)
    copy = _copy_dir(tmp_path)
    store.create_scene_copy(own, copy)
    # 轮次 3：改写了基线那条的正文（哨兵轮次照旧，改动只记在账本里）
    lib = store.load_library(copy).library
    lib.entries["陈掌柜"].body = "其实不跛，我亲眼见他站起来过"
    store.save_library(lib, copy)
    store.record_pending(copy, "陈掌柜", revised=True, turn=3)
    # 轮次 5：又记了一条新说法，当场把上一条压成过时（副本里的结局）
    lib = store.load_library(copy).library
    lib.upsert(_entry("陈掌柜的", title="陈掌柜", body="新说法：那跛是老伤",
                      scene=SCENE, turn=5))
    lib.entries["陈掌柜"].status = "outdated"
    lib.entries["陈掌柜"].superseded_by = "陈掌柜的"
    store.save_library(lib, copy)
    store.record_pending(copy, "陈掌柜的", turn=5)

    gain = collect_gain(copy, own)
    assert gain.order == ["陈掌柜", "陈掌柜的"]        # 按最后一次写入的轮次：3 → 5

    report = merge_gain(gain, own)

    live = _read(store.entry_path(own, "陈掌柜"))
    assert live.status == "outdated" and live.superseded_by == "陈掌柜的"
    assert live.body == "其实不跛，我亲眼见他站起来过"    # 本场改的那份，正文一字不丢
    newest = _read(store.entry_path(own, "陈掌柜的"))
    assert newest.status == "active" and newest.superseded_by is None
    # 开演前那条旧正文照样归档留着（§3.5 同键归档）
    assert _read(_archive_path(own, "陈掌柜")).body == "跛足，左眉有疤"
    assert (report.added, report.revised, report.refused) == (1, 1, 0)
    assert report.persisted is True


def test_merge_order_follows_the_write_turns_not_the_copy_slots(tmp_path):
    """合并顺序按**最后一次写入的轮次**，不是副本里的排列顺序（基线总是排在前面）。

    本场先记了一条新说法（压住了旧认识），之后又回头把旧认识改对了（反过来压住新说法）
    ——胜负在副本里已经定过两次，合并若按副本的物理顺序（基线在前）动手，就会把最后那次
    修订放在它该压的东西之前，本体里两条的现行/过时又反了。
    """
    own = _own_dir(tmp_path)
    _save([_entry("陈掌柜", body="开演前的说法", kind="manual")], own)
    copy = _copy_dir(tmp_path)
    store.create_scene_copy(own, copy)
    lib = store.load_library(copy).library
    lib.upsert(_entry("陈掌柜的", title="陈掌柜", body="新说法A", scene=SCENE, turn=2))
    lib.entries["陈掌柜"].status = "outdated"          # 轮次 2：被新说法压住
    lib.entries["陈掌柜"].superseded_by = "陈掌柜的"
    store.save_library(lib, copy)
    store.record_pending(copy, "陈掌柜的", turn=2)
    lib = store.load_library(copy).library
    lib.entries["陈掌柜"].body = "改过的说法"           # 轮次 5：改对了，反过来压住新说法
    lib.entries["陈掌柜"].status = "active"
    lib.entries["陈掌柜"].superseded_by = None
    lib.entries["陈掌柜的"].status = "outdated"
    lib.entries["陈掌柜的"].superseded_by = "陈掌柜"
    store.save_library(lib, copy)
    store.record_pending(copy, "陈掌柜", revised=True, turn=5)

    gain = collect_gain(copy, own)
    assert gain.order == ["陈掌柜的", "陈掌柜"]        # 按写入轮次：2 → 5（不是副本的排列）

    merge_gain(gain, own)

    assert _read(store.entry_path(own, "陈掌柜")).status == "active"
    assert _read(store.entry_path(own, "陈掌柜")).body == "改过的说法"
    assert _read(store.entry_path(own, "陈掌柜的")).status == "outdated"
    assert _read(store.entry_path(own, "陈掌柜的")).superseded_by == "陈掌柜"
    # 开演前那份旧正文照样归档留着（改写过的那条也要归档，§3.5）
    assert _read(_archive_path(own, "陈掌柜")).body == "开演前的说法"


def test_merge_warns_when_a_later_entry_of_the_same_gain_supersedes_an_earlier_one(tmp_path):
    """同一份所得里，后记的那条把先记的那条压成过时 —— 结果与副本一致，但**要说一句**。

    这不是坏事（副本里当场就是这么定的：新说法压旧说法、正文一字没删），可它是"用户点了
    保留、其中一条却当场成了过时、从此不进索引"的那一类意外。报告里的 `pressed` 只报一个
    数，用户看不出被压的是不是自己这一场刚拿到的东西；点名那句才让他有机会知道。
    """
    own = _own_dir(tmp_path)                    # 本体库是空的：两条都是本场新建
    copy = _copy_dir(tmp_path)
    store.create_scene_copy(own, copy)
    lib = store.load_library(copy).library
    lib.upsert(_entry("陈掌柜", title="陈掌柜", body="其实是陈掌柜本人", scene=SCENE, turn=1))
    lib.upsert(_entry("陈掌柜的", title="陈掌柜", body="跛足，左眉有疤", scene=SCENE, turn=2))
    lib.entries["陈掌柜"].status = "outdated"     # 副本里当场被后写的那条压住
    lib.entries["陈掌柜"].superseded_by = "陈掌柜的"
    store.save_library(lib, copy)
    store.record_pending(copy, "陈掌柜", turn=1)
    store.record_pending(copy, "陈掌柜的", turn=2)

    gain = collect_gain(copy, own)
    report = merge_gain(gain, own)

    assert (report.added, report.refused) == (2, 0)
    assert _read(store.entry_path(own, "陈掌柜")).status == "outdated"   # 与副本一致
    assert _read(store.entry_path(own, "陈掌柜的")).status == "active"
    assert any("陈掌柜" in w and "标成了过时" in w for w in report.warnings)


def test_merge_revives_an_outdated_entry_the_scene_flipped_back(tmp_path):
    """本场把一条过时的旧认识翻回现行（正文与摘要一字未改）→ 必须真的翻回来。

    "跳过"的判据是"并下去等于什么都没变"。正文相同、**现行状态不同**时并下去是有变化的
    ——本体里那条还是过时（索引默认不列过时条目，角色下一场根本看不见它），而副本里它已经
    是现行说法、`pending.json` 里也明明记着本场改过它（§5.3.1）。按"写了什么一样"跳过，
    用户的「保留」就被吞了：报告里一个数都没有、一句警告都没有。
    """
    own = _own_dir(tmp_path)
    _save([_entry("那封信", body="是真的", status="outdated",
                  superseded_by="别的说法")], own)
    copy = _copy_dir(tmp_path)
    store.create_scene_copy(own, copy)
    lib = store.load_library(copy).library
    lib.entries["那封信"].status = "active"
    lib.entries["那封信"].superseded_by = None
    store.save_library(lib, copy)
    store.record_pending(copy, "那封信", revised=True, turn=3)

    gain = collect_gain(copy, own)
    assert [e.key for e in gain.revised] == ["那封信"]

    first = merge_gain(gain, own)
    live = _read(store.entry_path(own, "那封信"))
    assert (first.revised, first.refused, first.persisted) == (1, 0, True)
    assert live.status == "active" and live.superseded_by is None
    assert live.body == "是真的"                     # 正文一字不差，也没有什么可归档的
    assert not _archive_path(own, "那封信").exists()

    second = merge_gain(gain, own)                   # 幂等：第二次是空操作
    assert (second.revised, second.skipped) == (0, 1)
    assert _read(store.entry_path(own, "那封信")).status == "active"
