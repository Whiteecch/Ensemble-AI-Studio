"""knowledgestore.py 的测试（设计文档 §4 落盘布局 / §5 场景副本 / §7.5 幂等 / §9.1 播种）。

钉的都是**会静默出错**的地方——它们出事时不会抛，只会让角色带着错的知识开口：

  · 条目文件是唯一真相源：手写一个 `.md` 丢进 `entries/` 就该被收录；一个坏文件只跳过
    并报告，绝不因它让整座库读不出来，也绝不明不白地消失；
  · 副本基线条目的 `turn` 哨兵：`0` 会被截断逻辑判为"永不截断"（§5.3 点名的坑），
    于是撤回后角色仍记得从没发生过的事；
  · 截断只伤本场条目：从本体拷进来的、订阅来的是上一场/别人的产物，不该被这一场的
    撤回连坐；
  · 结算幂等：重复结算不能产生副本条目，也不能把"已丢弃"翻成"保留"；
  · 播种幂等 + 兜底句丢弃：那行默认文案没有任何信息量，进库只会在索引里占一行噪声。

全程 `tmp_path`，不碰真实用户目录、不碰仓库里的素材目录。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness import memory as memory_mod
from harness.knowledge import Entry, EntryOrigin, entry_key_error, render_entry_md
from harness.knowledgestore import (
    BASELINE_TURN,
    ENTRIES_DIRNAME,
    KNOWLEDGE_FALLBACK,
    PENDING_JSON,
    Library,
    create_scene_copy,
    delete_entry,
    ensure_scene_copy,
    entries_dir,
    entry_path,
    has_library,
    library_dir,
    library_json_path,
    load_library,
    load_pending,
    load_subscriptions,
    mark_settled,
    _inside_root,
    _safe_rel,
    pending_path,
    pin_entry,
    record_pending,
    render_library_index,
    save_library,
    scene_library_dir,
    seed_character_library,
    seed_from_card,
    truncate_copy_after_turn,
)


# --------------------------------------------------------------------- 夹具 --

def _libs(tmp_path: Path) -> Path:
    """信息库根（正式布局是 `app/libraries/`，测试里注入 tmp_path）。"""
    return tmp_path / "libraries"


def _entry(key: str, **kw) -> Entry:
    """造一条条目；只写与断言相关的字段，其余走默认值。"""
    base = dict(title=key, summary=f"{key}的一行摘要", body=f"{key}的正文")
    base.update(kw)
    return Entry(key=key, **base)


def _save(entries: list[Entry], path: Path, **meta) -> Path:
    """把若干条条目装进一座库落盘，返回库目录。"""
    lib = Library(**meta)
    for e in entries:
        lib.upsert(e)
    save_library(lib, path)
    return path


def _write_entry_file(lib_path: Path, entry: Entry) -> Path:
    """绕过 save_library 直接手写一个条目文件（模拟"用户手写的 .md"）。"""
    path = entry_path(lib_path, entry.key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_entry_md(entry), encoding="utf-8", newline="\n")
    return path


class _FakeCard:
    """CharacterCard 的鸭子类型替身：`seed_from_card` 只要求有 `knowledge_seed`。

    schemas.py 现在还没有这个字段（那是后续里程碑的改动），所以替身刻意不带任何
    pydantic 依赖——这正是「接受鸭子类型」这一契约的测试价值。
    """

    def __init__(self, seed: list[str] | None) -> None:
        if seed is not None:
            self.knowledge_seed = seed


# ------------------------------------------------------------- 路径换算 --

def test_library_dir_maps_character_and_wide(tmp_path):
    """两类库落在 §4.1 写死的两个子目录下：characters/<角色名> 与 wide/<库名>。

    路径是唯一碰磁盘的东西，错一级就等于把角色库写到广域库里去（那会让一个角色的
    私人见识变成全员可见）。kind 认不出来时必须当场抛，不许猜一个默认值。
    """
    root = _libs(tmp_path)
    assert library_dir("character", "丙", root=root) == root / "characters" / "丙"
    assert library_dir("wide", "庆国世界观", root=root) == root / "wide" / "庆国世界观"
    with pytest.raises(ValueError):
        library_dir("bogus", "丙", root=root)


def test_scene_library_dir_sits_beside_private_memory(tmp_path):
    """副本目录 = `runs/<场景>/<角色名>/library`，与私有记忆目录同级（§4.1/§5.1）。

    这一条按「同一场的内心状态、印象、所得要在一起结算」设计，所以它不是任意一个
    路径：必须紧挨着 CharacterMemory 的文件夹。跨目录会让散场结算找不到副本。
    """
    run_root = tmp_path / "runs" / "app-abcdef"
    here = scene_library_dir(run_root, "丙")
    assert here == run_root / "丙" / "library"
    assert here.parent == memory_mod.CharacterMemory(run_root, "丙").folder


def test_entry_path_guards_key_and_names_entries_dir(tmp_path):
    """键直接变成文件名（§4.3）：不安全的键当场抛，绝不静默改名。

    `../` 这种键放过一次就等于让写盘跑出库目录；而"偷偷替换字符"会让同一件事
    变成两件事（键是冲突判定的第一依据）。
    """
    lib_path = tmp_path / "lib"
    assert entry_path(lib_path, "药铺的暗格") == lib_path / "entries" / "药铺的暗格.md"
    assert entry_path(lib_path, "药铺的暗格").parent == entries_dir(lib_path)
    with pytest.raises(ValueError):
        entry_path(lib_path, "../逃逸")


def test_pending_path_lives_in_copy_root(tmp_path):
    """`pending.json` 是副本的库级文件（§4.1 / §7.5），与 library.json 同级。"""
    lib_path = tmp_path / "lib"
    assert pending_path(lib_path) == lib_path / PENDING_JSON
    assert library_json_path(lib_path) == lib_path / "library.json"
    assert entries_dir(lib_path) == lib_path / ENTRIES_DIRNAME


# --------------------------------------------------------------- 读写往返 --

def test_roundtrip_preserves_meta_entries_and_order(tmp_path):
    """存了再读：库级元信息、每条条目的全字段、索引顺序**逐项相同**。

    条目文件是唯一真相源（§4.2），所以往返一致是底线——不一致就意味着每次保存都在
    无意义地改写用户的文件，git diff 从此全是噪声。
    """
    lib_path = library_dir("character", "丙", root=_libs(tmp_path))
    _save([_entry("药铺的暗格", body="柜台下第三块砖，[[陈掌柜]]"),
           _entry("陈掌柜", summary="跛足，左眉有疤")],
          lib_path, name="丙", scope="character", owner="丙")

    loaded = load_library(lib_path)
    assert loaded.warnings == []
    lib = loaded.library
    assert (lib.name, lib.scope, lib.owner) == ("丙", "character", "丙")
    assert lib.ordered_keys() == ["药铺的暗格", "陈掌柜"]
    assert lib.entries["药铺的暗格"].body == "柜台下第三块砖，[[陈掌柜]]"
    assert lib.entries["陈掌柜"].summary == "跛足，左眉有疤"
    assert lib.entries["药铺的暗格"].origin.kind == "manual"


def test_library_json_holds_only_meta_and_order(tmp_path):
    """`library.json` 只存库级元信息 + 索引顺序，**不存条目内容**（§4.2）。

    条目一旦被复制进 library.json，就出现了第二个真相源；下次手改 `.md` 会被
    library.json 里的旧副本反压回去——用户改了却没生效，这是最难查的一类 bug。
    """
    lib_path = library_dir("wide", "庆国世界观", root=_libs(tmp_path))
    _save([_entry("地理·西线")], lib_path, name="庆国世界观", scope="wide")
    raw = library_json_path(lib_path).read_text(encoding="utf-8")
    data = json.loads(raw)
    assert set(data) == {"schema", "name", "scope", "owner", "order", "updated_at"}
    assert data["order"] == ["地理·西线"]
    assert data["schema"] == 1
    assert "正文" not in raw and "一行摘要" not in raw      # 条目内容一个字都不许进来


def test_hand_written_entry_is_picked_up_without_library_json(tmp_path):
    """手写一个 `.md` 丢进 `entries/` 就能被收录，没有 library.json 也算正常。

    这是"素材即文件"的命脉（§4.1）：往目录丢个文件就进库。此时顺序只能靠扫描
    定（键序），但库必须读得出来、且不报"缺文件"的假警告。
    """
    lib_path = tmp_path / "手写库"
    _write_entry_file(lib_path, _entry("手写的"))
    loaded = load_library(lib_path)
    assert loaded.warnings == []
    assert loaded.library.ordered_keys() == ["手写的"]


def test_order_from_library_json_is_honoured_and_self_healed_on_save(tmp_path):
    """`order` 决定索引显示顺序（不是键序），幽灵键在保存时被剔除。

    顺序是给角色看的（先读什么后读什么），故必须可控；而 order 里指向已删条目的
    幽灵键留着只会让下次加载多一条空位，保存时顺手自愈。
    """
    lib_path = tmp_path / "手写库"
    _write_entry_file(lib_path, _entry("甲"))
    _write_entry_file(lib_path, _entry("乙"))
    library_json_path(lib_path).write_text(
        json.dumps({"schema": 1, "name": "手写库", "scope": "character",
                    "owner": "丙", "order": ["乙", "甲", "幽灵"]},
                   ensure_ascii=False, indent=2),
        encoding="utf-8", newline="\n")

    loaded = load_library(lib_path)
    assert loaded.library.ordered_keys() == ["乙", "甲"]
    save_library(loaded.library, lib_path)
    data = json.loads(library_json_path(lib_path).read_text(encoding="utf-8"))
    assert data["order"] == ["乙", "甲"]


def test_bad_entry_file_is_skipped_and_reported(tmp_path):
    """一个坏条目只跳过它自己，并**出现在 warnings 里**；整座库照常读出来。

    静默吞掉是最坏的选择：用户手写的条目一夜之间凭空少了一条，而程序不吭声。
    绝不因一个坏文件让整座库读不出来——那会把"一条坏"放大成"全丢"。
    """
    lib_path = tmp_path / "手写库"
    _write_entry_file(lib_path, _entry("好的"))
    (entries_dir(lib_path) / "坏的.md").write_text("没有 front-matter 的一段话",
                                                   encoding="utf-8", newline="\n")

    loaded = load_library(lib_path)
    assert loaded.library.ordered_keys() == ["好的"]
    assert len(loaded.warnings) == 1
    assert "坏的" in loaded.warnings[0]


def test_entry_file_key_mismatch_warns_and_trusts_the_file(tmp_path):
    """文件名与 front-matter 的 `key` 不一致 → 以 **front-matter 为准**并报一条警告。

    条目文件是真相源（§4.2）：用户把 key 改了却没改文件名（或反着来），必须让他看到，
    而不是默默按文件名收录——那会变成"我改的 key 没生效"。
    """
    lib_path = tmp_path / "手写库"
    ents = entries_dir(lib_path)
    ents.mkdir(parents=True)
    (ents / "文件名.md").write_text(render_entry_md(_entry("里面的键")),
                                    encoding="utf-8", newline="\n")

    loaded = load_library(lib_path)
    assert loaded.library.ordered_keys() == ["里面的键"]
    assert len(loaded.warnings) == 1
    assert "文件名" in loaded.warnings[0]


def test_corrupt_library_json_degrades_to_scan(tmp_path):
    """`library.json` 坏了不能把整座库带走：退回"扫描 entries/"并报一条警告。

    条目文件才是真相源，library.json 丢的只是显示顺序。这里如果抛，就等于一个
    手滑的编辑让角色彻底失忆。
    """
    lib_path = tmp_path / "手写库"
    _write_entry_file(lib_path, _entry("还活着"))
    library_json_path(lib_path).write_text("{ 这不是 json", encoding="utf-8", newline="\n")

    loaded = load_library(lib_path)
    assert loaded.library.ordered_keys() == ["还活着"]
    assert any("library.json" in w for w in loaded.warnings)


def test_load_missing_library_is_empty_and_has_library_is_false(tmp_path):
    """库不存在 = 空库、零警告（老卡老存档的正常状态，§9.2 不报错）。

    缺库与坏库必须区分开：前者是常态不该吵，后者要报。`has_library` 是给
    "副本是否已存在"这类判定用的显式入口。
    """
    missing = tmp_path / "从没有过的库"
    loaded = load_library(missing)
    assert loaded.library.entries == {}
    assert loaded.warnings == []
    assert has_library(missing) is False
    assert has_library(tmp_path / "手写库") is False


def test_save_is_atomic_and_leaves_no_temp_files(tmp_path):
    """写盘走临时文件 + os.replace：不留 `.tmp` 垃圾，失败也不毁旧文件。

    裸 `open(..., "a")` 或直接覆盖写在断电/崩溃时会留下半截 JSON——下次加载就是
    一条坏库。临时文件必须清干净，否则每个库目录都会慢慢积垃圾。
    """
    lib_path = library_dir("character", "丙", root=_libs(tmp_path))
    _save([_entry("甲")], lib_path, name="丙", owner="丙")
    loaded = load_library(lib_path).library
    loaded.upsert(_entry("乙"))
    save_library(loaded, lib_path)

    assert sorted(load_library(lib_path).library.entries) == ["乙", "甲"]
    assert list(tmp_path.rglob("*.tmp")) == []


def test_delete_entry_removes_file_and_order_slot(tmp_path):
    """删条目 = 删文件 + 从 order 里摘掉，且对不存在的键是幂等的 False。"""
    lib_path = library_dir("character", "丙", root=_libs(tmp_path))
    _save([_entry("甲"), _entry("乙")], lib_path, name="丙", owner="丙")

    assert delete_entry(lib_path, "甲") is True
    assert not entry_path(lib_path, "甲").exists()
    assert load_library(lib_path).library.ordered_keys() == ["乙"]
    assert delete_entry(lib_path, "甲") is False


# ----------------------------------------- 文件名与 key 不一致（手写 / 改键的常客）--

def _write_raw_entry_file(lib_path: Path, filename: str, entry: Entry) -> Path:
    """按**指定文件名**手写一个条目文件（模拟"文件名与 key 不一致"的手写/改键现场）。"""
    path = entries_dir(lib_path) / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_entry_md(entry), encoding="utf-8", newline="\n")
    return path


def test_save_canonicalizes_a_misnamed_copy(tmp_path):
    """文件名与 key 不一致时，保存要**归一化**成 `<键>.md` 并清掉旧文件名那份。

    否则同一条目在库里有两份文件，而加载是"按文件名排序扫描、后来者覆盖"：旧副本
    （`z.md`，key 是 `b`）排在新写的 `b.md` 之后，于是**每次保存都白改**——界面上看到
    的是旧正文，重载后改动凭空消失。这正是「用户改了却没生效」那类最难查的 bug，也直接
    违反 §4.2"条目文件是唯一真相源"：两份文件同时存在时真相源就不唯一了。

    `prune=False` 不删的是**读不出来的坏文件**（它们不在内存里，删了就是数据损坏），
    不是同一个键的第二份副本——那份的内容刚刚已经被写进 `<键>.md` 了。
    """
    lib_path = tmp_path / "手写库"
    _write_raw_entry_file(lib_path, "z.md", _entry("b", body="原始内容"))
    loaded = load_library(lib_path)
    assert len(loaded.warnings) == 1                      # 文件名与 key 不一致，报过了
    loaded.library.entries["b"].body = "用户改过的新内容"

    save_library(loaded.library, lib_path)

    assert sorted(p.name for p in entries_dir(lib_path).glob("*.md")) == ["b.md"]
    assert load_library(lib_path).library.entries["b"].body == "用户改过的新内容"


def test_save_leaves_only_one_file_when_two_files_claim_a_key(tmp_path):
    """两个文件声明同一个 key：保存后只该剩 `<键>.md` 一份，且内容就是在内存里看到的那份。

    加载时后来者覆盖前者（sorted 扫描），被压住的那份此刻就读不到了；保存若不把多余的那份
    清掉，下一次扫描又会重演一遍——用户手改的那份文件被静默吞掉，且**每次保存都会制造
    出这个重复**（按 key 另写一份），于是这条条目从此被永久压住。
    """
    lib_path = tmp_path / "手写库"
    _write_raw_entry_file(lib_path, "a.md", _entry("a", body="a.md 里的正文"))
    _write_raw_entry_file(lib_path, "b.md", _entry("a", title="b", body="b.md 里的正文"))
    loaded = load_library(lib_path)
    shown = loaded.library.entries["a"].body

    save_library(loaded.library, lib_path)

    assert sorted(p.name for p in entries_dir(lib_path).glob("*.md")) == ["a.md"]
    assert load_library(lib_path).library.entries["a"].body == shown


def test_save_never_deletes_files_it_cannot_read(tmp_path):
    """读不出来的坏文件一律不碰（`prune=False` 的底线），哪怕同一次保存里有副本要清。

    坏文件不在内存里，删掉就等于把用户手写的东西永久抹掉——新逻辑只清理"声明了某个正在
    写的键"的副本，坏文件连 key 都读不出来，自然不在其列。
    """
    lib_path = tmp_path / "手写库"
    _write_raw_entry_file(lib_path, "好的.md", _entry("好的"))
    _write_raw_entry_file(lib_path, "z.md", _entry("好的"))
    bad = entries_dir(lib_path) / "坏的.md"
    bad.write_text("没有 front-matter 的一段话", encoding="utf-8", newline="\n")

    loaded = load_library(lib_path)
    save_library(loaded.library, lib_path)

    assert bad.exists()
    assert sorted(p.name for p in entries_dir(lib_path).glob("*.md")) == sorted(
        ["好的.md", "坏的.md"])


def test_delete_entry_also_removes_a_misnamed_copy(tmp_path):
    """按 key 删条目要**删干净**：文件名与 key 不一致的那份副本也是这条条目。

    只按 `<键>.md` 删就会出现"删了又自己回来"：另一份文件还在，下次加载照样把它读进来，
    而 `delete_entry` 已经返回 True——用户看到条目复活，程序却以为删成功了。
    """
    lib_path = tmp_path / "手写库"
    _write_raw_entry_file(lib_path, "z.md", _entry("b"))

    assert delete_entry(lib_path, "b") is True
    assert list(entries_dir(lib_path).glob("*.md")) == []
    assert load_library(lib_path).library.entries == {}
    assert delete_entry(lib_path, "b") is False            # 幂等


def test_delete_entry_never_removes_a_file_holding_another_key(tmp_path):
    """文件名叫 `<键>.md`、内容却是另一条时，按文件名下手会删掉**别人的正文**。

    改键之后（`a.md` 里写的是 `key: b`），被删的 `b` 其实住在 `a.md` 里；这时若按名字
    去删 `a.md`，丢的是用户正在看的那条条目。判据只能是文件**声明的键**——条目由键识别。
    """
    lib_path = tmp_path / "手写库"
    _write_raw_entry_file(lib_path, "a.md", _entry("b", body="b 的正文"))
    assert load_library(lib_path).library.ordered_keys() == ["b"]

    assert delete_entry(lib_path, "a") is False            # 没有任何文件声明键「a」
    assert (entries_dir(lib_path) / "a.md").exists()

    assert delete_entry(lib_path, "b") is True             # 声明了 b 的那份才是要删的
    assert list(entries_dir(lib_path).glob("*.md")) == []


def test_load_warns_when_two_files_declare_the_same_key(tmp_path):
    """两个文件声明同一个 key → 必须报出**两份文件名**：此刻有一份内容读不到。

    静默取后来者是数据损坏的温床——用户改的那份文件可能正是被压住的那份。警告要能让他
    找到该删哪个文件，所以两份名字都得点名。
    """
    lib_path = tmp_path / "手写库"
    _write_raw_entry_file(lib_path, "a.md", _entry("a"))
    _write_raw_entry_file(lib_path, "b.md", _entry("a", title="b"))

    loaded = load_library(lib_path)
    assert loaded.library.ordered_keys() == ["a"]
    dup = [w for w in loaded.warnings if "a.md" in w and "b.md" in w]
    assert len(dup) == 1


def test_upsert_rejects_keys_that_clash_case_insensitively(tmp_path):
    """`ABC` 与 `abc` 各自合法，却是同一个文件名（Windows / 默认 macOS 卷大小写不敏感）。

    放行等于后一条静默顶掉前一条：写盘时两个键只有一个文件，重载只剩一条，全程没有报错。
    用户只会发现"我记的东西少了一条"。这里当场抛中文原因，把问题摆到调用方面前。
    """
    lib = Library(name="丙", owner="丙")
    lib.upsert(Entry(key="ABC"))
    with pytest.raises(ValueError) as exc:
        lib.upsert(Entry(key="abc"))
    assert "大小写" in str(exc.value)
    assert lib.ordered_keys() == ["ABC"]                   # 已有的那条没被动过

    lib.upsert(Entry(key="ABC", body="改了"))               # 键完全相同 = 覆盖，不是碰撞
    assert lib.entries["ABC"].body == "改了"
    assert lib.ordered_keys() == ["ABC"]


# ------------------------------------------------------------- 场景副本 --

def test_create_scene_copy_marks_baseline_turn_with_sentinel(tmp_path):
    """本体条目拷进副本时 `origin.kind` 原样保留，`turn` 改成**哨兵**（不是 0）。

    §5.3 点名的坑：`turn=0` 会被"保留 turn ≤ cut"判为永不截断，于是撤回后角色仍
    带着从没发生过的事开口。用哨兵把"这不是本场产物"这件事**显式**写进数据里，
    而不是靠数值兜底。本体上原本的 turn 也要抹掉——那是别处的轮次，在这一场没有意义。
    """
    src = library_dir("character", "丙", root=_libs(tmp_path))
    _save([_entry("旧事", origin=EntryOrigin(kind="manual", turn=7)),
           _entry("别处捡的", origin=EntryOrigin(kind="scene", scene="上一场", turn=5)),
           _entry("体系设定", origin=EntryOrigin(kind="import"))],
          src, name="丙", owner="丙")
    dst = scene_library_dir(tmp_path / "runs" / "app-1", "丙")

    assert create_scene_copy(src, dst) == []
    copied = load_library(dst).library
    assert sorted(copied.entries) == ["体系设定", "别处捡的", "旧事"]
    for key in ("旧事", "别处捡的", "体系设定"):
        assert copied.entries[key].origin.turn == BASELINE_TURN
        assert copied.entries[key].origin.turn != 0
    assert copied.entries["旧事"].origin.kind == "manual"
    assert copied.entries["别处捡的"].origin.kind == "scene"
    assert copied.entries["体系设定"].origin.kind == "import"
    assert copied.entries["别处捡的"].origin.scene == "上一场"   # 出处不抹，只标基线


def test_create_scene_copy_from_missing_source_makes_empty_copy(tmp_path):
    """角色从没有过库 → 建空副本，owner/name 从路径推出来，一切照常（§5.1）。

    这里绝不能抛：没有库的角色也要能进场，"没有"是合法状态而不是错误。
    """
    src = library_dir("character", "无名", root=_libs(tmp_path))
    dst = scene_library_dir(tmp_path / "runs" / "app-1", "无名")
    assert create_scene_copy(src, dst) == []
    loaded = load_library(dst)
    assert loaded.library.entries == {}
    assert loaded.library.owner == "无名"
    assert loaded.library.scope == "character"


def test_ensure_scene_copy_creates_once_then_reuses(tmp_path):
    """副本已存在则**复用**，绝不重置：他离场又回来时，这一场的所得必须还在（§5.1）。

    重置等于把他中途学到的东西悄悄抹掉——而界面上没有任何提示，只有"他怎么不记得了"。
    """
    src = library_dir("character", "丙", root=_libs(tmp_path))
    _save([_entry("旧事")], src, name="丙", owner="丙")
    dst = scene_library_dir(tmp_path / "runs" / "app-1", "丙")

    assert ensure_scene_copy(src, dst) is True            # 首次创建
    lib = load_library(dst).library
    lib.upsert(_entry("本场所得", origin=EntryOrigin(kind="scene", scene="贝克街221B", turn=3)))
    save_library(lib, dst)

    assert ensure_scene_copy(src, dst) is False           # 复用，不重建
    after = load_library(dst).library
    assert sorted(after.entries) == ["旧事", "本场所得"]   # 本场所得没被抹掉
    assert after.entries["本场所得"].origin.turn == 3


def test_truncate_drops_only_this_scene_entries(tmp_path):
    """截断只丢 `origin.kind == "scene"` 且 `turn > cut` 的条目（§5.3 口径写死）。

    基线条目（从本体拷来的 manual/import/wide）不是这一场的产物，撤回到再早也不能
    把它们截掉——那等于让角色忘掉自己的身世。
    """
    src = library_dir("character", "丙", root=_libs(tmp_path))
    _save([_entry("旧事", origin=EntryOrigin(kind="manual"))],
          src, name="丙", owner="丙")
    dst = scene_library_dir(tmp_path / "runs" / "app-1", "丙")
    create_scene_copy(src, dst)

    lib = load_library(dst).library
    lib.upsert(_entry("早的", origin=EntryOrigin(kind="scene", scene="贝克街221B", turn=1)))
    lib.upsert(_entry("晚的", origin=EntryOrigin(kind="scene", scene="贝克街221B", turn=9)))
    lib.upsert(_entry("借来的", origin=EntryOrigin(kind="wide")))
    save_library(lib, dst)

    assert truncate_copy_after_turn(dst, 5) == 1
    left = load_library(dst).library
    assert sorted(left.entries) == ["借来的", "旧事", "早的"]
    assert not entry_path(dst, "晚的").exists()


def test_truncate_keeps_previous_scene_entry_copied_as_baseline(tmp_path):
    """本体里 kind=scene 的**旧场**条目拷进副本后仍是 scene，靠哨兵逃过截断。

    这是哨兵存在的直接理由：只按 kind 判会误伤（那条不是这一场的），只按 turn 判
    在 0 上会静默永不截断。两个条件合起来才安全。
    """
    src = library_dir("character", "丙", root=_libs(tmp_path))
    _save([_entry("上一场的记性", origin=EntryOrigin(kind="scene", scene="上一场", turn=40))],
          src, name="丙", owner="丙")
    dst = scene_library_dir(tmp_path / "runs" / "app-1", "丙")
    create_scene_copy(src, dst)
    lib = load_library(dst).library
    lib.upsert(_entry("这一场的", origin=EntryOrigin(kind="scene", scene="贝克街221B", turn=2)))
    save_library(lib, dst)

    assert truncate_copy_after_turn(dst, 0) == 1
    assert load_library(dst).library.ordered_keys() == ["上一场的记性"]


def test_truncate_is_idempotent_and_prunes_pending(tmp_path):
    """重复截断不报错、不重复计数；被截掉的键同时从 pending 清单里摘掉。

    留着"待结算"的空键，散场合并时会去找一条已经不存在的条目——结算要么报错、
    要么并入一条幽灵。截断与 pending 必须一起动。
    """
    dst = scene_library_dir(tmp_path / "runs" / "app-1", "丙")
    lib = Library(name="丙", scope="character", owner="丙")
    lib.upsert(_entry("早的", origin=EntryOrigin(kind="scene", scene="贝克街221B", turn=1)))
    lib.upsert(_entry("晚的", origin=EntryOrigin(kind="scene", scene="贝克街221B", turn=9)))
    save_library(lib, dst)
    record_pending(dst, "早的")
    record_pending(dst, "晚的")

    assert truncate_copy_after_turn(dst, 5) == 1
    assert truncate_copy_after_turn(dst, 5) == 0            # 幂等
    pending = load_pending(dst)
    assert pending.added == ["早的"]


def test_truncate_keeps_state_when_the_file_cannot_be_deleted(tmp_path, monkeypatch):
    """条目文件删不掉时，**内存 / order / pending 一处都不能摘**：否则它下次加载自己回来。

    删不掉（只读文件、或被另一个进程开着）却把三处内存状态都改成"已丢弃"，撤回就等于没做：
    下一次 `load_library`（每次渲染索引都会调）把这条从没被截掉的记忆读回来，而且因为
    order 里已被摘掉，它会静默出现在另一个位置；pending 里也没有它了，散场结算不会把它
    算进"本场所得"。调用方（M2 引擎）还拿到"成功丢弃 1 条"的返回值，无从知道副本并未截断。
    如实返回 0、把这条留在原处，下次撤回再试——这才是"绝不抛"该有的样子。
    """
    dst = scene_library_dir(tmp_path / "runs" / "app-1", "丙")
    lib = Library(name="丙", owner="丙")
    lib.upsert(_entry("晚的", origin=EntryOrigin(kind="scene", scene="贝克街221B", turn=9)))
    save_library(lib, dst)
    record_pending(dst, "晚的")

    target = entry_path(dst, "晚的")
    real_unlink = Path.unlink

    def _deny(self, *args, **kwargs):
        """只让这一个文件删不掉，别处（tmp_path 清理等）照旧。"""
        if self == target:
            raise PermissionError("文件被占用")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _deny)
    assert truncate_copy_after_turn(dst, 5) == 0            # 一条也没截掉，如实返回
    assert target.exists()
    assert list(load_library(dst).library.entries) == ["晚的"]
    assert load_pending(dst).added == ["晚的"]               # 待结算清单不能先摘

    monkeypatch.undo()
    assert truncate_copy_after_turn(dst, 5) == 1            # 下次撤回接着试
    assert not target.exists()
    assert load_library(dst).library.entries == {}
    assert load_pending(dst).added == []


# ------------------------------------------------------------- pending --

def test_pending_missing_file_is_empty_not_error(tmp_path):
    """没有 `pending.json` = 本场还没有任何改动，不是错误（老存档 / 刚建的副本）。"""
    dst = scene_library_dir(tmp_path / "runs" / "app-1", "丙")
    pending = load_pending(dst)
    assert pending.added == [] and pending.revised == []
    assert pending.settled is False and pending.outcome == ""


def test_record_pending_splits_added_from_revised(tmp_path):
    """本场**新增**与**修订**分两份清单（§7.2 的「本场所得」清单靠它生成）。

    混成一份的话，界面没法区分"他学到了新东西"与"他改了旧认识"——而这正是你
    决定保留/丢弃时要看的东西。先新增后修订仍算新增（它就是本场长出来的）。
    """
    dst = scene_library_dir(tmp_path / "runs" / "app-1", "丙")
    record_pending(dst, "新知道的")
    record_pending(dst, "改过的", revised=True)
    record_pending(dst, "新知道的", revised=True)          # 还是新增
    record_pending(dst, "改过的", revised=True)            # 去重
    record_pending(dst, "新来的")

    pending = load_pending(dst)
    assert pending.added == ["新知道的", "新来的"]
    assert pending.revised == ["改过的"]
    assert pending.settled is False
    # 落盘了：重读仍在
    assert load_pending(dst).added == ["新知道的", "新来的"]


def test_settle_is_idempotent_and_first_outcome_wins(tmp_path):
    """结算**先到先得**：重复结算不翻盘、不改写（§7.5 幂等）。

    已被丢弃的副本再次结算仍是丢弃——否则一次手滑的重复点击就会把"丢弃"翻成"保留"，
    角色凭空多出一批你从没同意永久化的记忆。
    """
    dst = scene_library_dir(tmp_path / "runs" / "app-1", "丙")
    record_pending(dst, "新知道的")

    assert mark_settled(dst, "keep") is True
    assert mark_settled(dst, "discard") is False           # 不翻盘
    pending = load_pending(dst)
    assert pending.settled is True and pending.outcome == "keep"
    assert pending.settled_at                              # 记下了时间
    assert pending.added == ["新知道的"]                    # 清单不因结算被抹掉


# --------------------------------------------------------------- 播种 --

def test_seed_from_card_writes_entries_in_order(tmp_path):
    """卡上的 `knowledge_seed` 拆成条目写进**新建**的库（§9.1 第二步）。

    「知道：X」形态取 X 作标题——前缀是给写卡的人看的语法，不是标题的一部分。
    """
    dst = library_dir("character", "丙", root=_libs(tmp_path))
    card = _FakeCard(["知道：药铺的暗格", "陈掌柜是个跛子"])

    res = seed_from_card(card, dst)
    assert res.created is True
    assert res.count == 2
    assert res.warnings == []
    lib = load_library(dst).library
    assert lib.ordered_keys() == ["药铺的暗格", "陈掌柜是个跛子"]
    assert lib.entries["陈掌柜是个跛子"].title == "陈掌柜是个跛子"
    assert lib.owner == "丙"


def test_seed_drops_fallback_sentence(tmp_path):
    """兜底句**丢弃不进库**（§9.1）：它是"没写边界"的占位，没有任何信息量。

    放进索引就是一行纯噪声，还占着注入预算——正是它当初在提示词里的那点作用，
    没必要换个地方继续存在。
    """
    dst = library_dir("character", "丙", root=_libs(tmp_path))
    res = seed_from_card(_FakeCard([KNOWLEDGE_FALLBACK, "知道：药铺的暗格"]), dst)

    assert res.count == 1
    assert load_library(dst).library.ordered_keys() == ["药铺的暗格"]


def test_seed_ignores_blank_lines_and_duplicates(tmp_path):
    """空行忽略；重复的行只落一条——重复的键会在列表里出现两行一模一样的入口。"""
    dst = library_dir("character", "丙", root=_libs(tmp_path))
    res = seed_from_card(_FakeCard(["", "   ", "知道：药铺的暗格", "知道：药铺的暗格",
                                    "知道：药铺的暗格"]), dst)

    assert res.count == 1
    assert load_library(dst).library.ordered_keys() == ["药铺的暗格"]


def test_seed_is_skipped_when_library_already_exists(tmp_path):
    """库已存在 → **整段跳过**（幂等）。第二次播种绝不能覆盖角色这一路长出来的见识。

    这也是"读时迁移不写盘"（§9.1）能成立的前提：卡上留着的 seed 只在首次建库时
    用一次，此后无害。
    """
    dst = library_dir("character", "丙", root=_libs(tmp_path))
    card = _FakeCard(["知道：药铺的暗格"])
    assert seed_from_card(card, dst).created is True
    lib = load_library(dst).library
    lib.upsert(_entry("本场刚学的", origin=EntryOrigin(kind="scene", scene="贝克街221B", turn=2)))
    save_library(lib, dst)

    again = seed_from_card(card, dst)
    assert again.created is False
    assert again.count == 0
    assert "本场刚学的" in load_library(dst).library.entries


def test_seed_from_real_card_carries_every_boundary_line_and_drops_only_the_fallback(
        tmp_path):
    """端到端（§9.1）：老卡 JSON 的 `knowledge_boundary` **每一条**都活着进库，顺序不变。

    这是本次改造的最高约束——卡 →（读时迁移）→ `knowledge_seed` →（首次建库）→ 条目。
    两头都要钉：迁移不丢（`test_schemas` 那边），播种也不丢。丢掉的**只有**那句兜底文案：
    它是"没写边界"的占位，进索引只占一行噪声。

    用的是一张**真卡**（`CharacterCard.model_validate` 读老形状的 JSON），不再是替身——
    `seed_from_card` 收鸭子类型，但真正要证明的是"用户卡上的字一条不少地到了库里"。
    """
    from harness.schemas import CharacterCard

    card = CharacterCard.model_validate({
        "name": "丙",
        "knowledge_boundary": ["知道：药铺的暗格", "陈掌柜是个跛子",
                               "不知道：信是谁写的", KNOWLEDGE_FALLBACK],
    })
    dst = library_dir("character", "丙", root=_libs(tmp_path))

    res = seed_from_card(card, dst)

    assert res.created is True and res.count == 3
    assert load_library(dst).library.ordered_keys() == [
        "药铺的暗格", "陈掌柜是个跛子", "不知道：信是谁写的"]
    # 卡上那份种子原样留着（播种不改卡）：库存在之后就再也不会播第二次
    assert card.knowledge_seed == ["知道：药铺的暗格", "陈掌柜是个跛子",
                                   "不知道：信是谁写的", KNOWLEDGE_FALLBACK]
    assert seed_from_card(card, dst).created is False, "第二次是幂等跳过"


def test_seed_character_library_picks_the_path_from_the_character_name(tmp_path):
    """`seed_character_library` = 按角色名换算库目录 + 播种（正式布局 `characters/<名>`）。

    入口（GUI / CLI）只管"把这张卡播一下"，路径换算留在存储层**一处**——两处各算一次
    `characters/<角色名>` 迟早会有一处算成别的 scope，那等于把角色的私人见识写进别人
    看得见的地方。
    """
    from harness.schemas import CharacterCard

    root = _libs(tmp_path)
    card = CharacterCard.model_validate({"name": "丁",
                                         "knowledge_boundary": ["知道：丙的旧伤"]})

    res = seed_character_library(card, root=root)

    assert res.created is True and res.count == 1
    dst = library_dir("character", "丁", root=root)
    assert load_library(dst).library.ordered_keys() == ["丙的旧伤"]
    assert seed_character_library(card, root=root).created is False


def test_seed_character_library_refuses_a_name_that_is_not_a_safe_dir(tmp_path):
    """角色名要过文件名守门：它会成为 `characters/<名>/` 这一层的**目录名**。

    卡名是用户写的自由文本（`CharacterCard.name` 没有守门），含路径分隔符或 `..` 的名字
    会把库写到信息库根**之外**去——与编辑器保存卡时那条"绝不把文件写到库目录之外"是
    同一条原则。不安全的名字不猜不改，直接跳过并报告：卡上的种子原样留着，改对名字再来。

    播种范围现在覆盖角色库目录里的每一张卡，这条守门因此更要紧（见
    `gui/worker._seed_card_libraries`）。
    """
    root = _libs(tmp_path)
    card = _FakeCard(["知道：甲"])
    card.name = "../跑到外面去"

    res = seed_character_library(card, root=root)

    assert res.created is False and res.count == 0
    assert res.warnings and "角色名" in res.warnings[0]
    assert not (tmp_path / "跑到外面去").exists(), "绝不能在根目录之外建库"
    assert not (root.parent / "跑到外面去").exists()


def test_fallback_sentence_has_exactly_one_definition(tmp_path):
    """兜底句只有一处定义（§9.1）：迁移时丢弃它的判据就是这条常量，**不许另立第二份口径**。

    它原来是提示词里"没写边界"时的默认文案。§9.1 撤掉了那一节，这句的唯一去处就只剩
    "播种时要丢掉的那一行"——判据只此一处，改动它必须同时改这里。
    """
    import harness.prompters as prompters

    assert KNOWLEDGE_FALLBACK == "只知道自己经历和被告知的事"
    assert KNOWLEDGE_FALLBACK not in Path(prompters.__file__).read_text(encoding="utf-8"), \
        "提示词模块里不该再有这句的第二份定义（那一节已撤除）"


def test_seed_keeps_lines_whose_text_cannot_be_a_filename(tmp_path):
    """含文件名非法字符的种子行**照样进库**（§9.1 最高约束：卡上每条边界都要活着）。

    键会成为文件名（§4.3），可**用户写在卡上的是内容，不是键**：真实卡里
    「第一批工程"存在"这一事实」「（东区/西区作战）」这类行遍地都是（半角引号与斜杠
    在中文写作里极常见）。若因为"整行当键过不了守门"就 `continue` 跳过，用户卡上的
    知识会**成片消失**——而提示词里那一节已随 §9.1 撤除，等于两条路一起断。

    故：内容一行不丢——`title` 保留原字；键只是**为了落盘**才在必要时改造（改造要有
    警告，绝不静默）。这里覆盖半角引号、两种斜杠、`:`、`?`、`*`、`|`、`<`、`>` 与
    控制字符；顺序必须与卡上一致。
    """
    lines = ['第一批工程"存在"这一事实（戊透露过部分）',
             "那场事故；他亲身经历了第三次（东区/西区作战）",
             "路径\\里的反斜杠", "带星号*的问号?和竖线|",
             "尖括号<甲>里的内容", "半角冒号: 之后的说明"]
    dst = library_dir("character", "丙", root=_libs(tmp_path))

    res = seed_from_card(_FakeCard(list(lines)), dst)

    lib = load_library(dst).library
    assert [lib.entries[k].title for k in lib.ordered_keys()] == lines, \
        "每一行都要有一条、标题逐字保留原文、顺序与卡上一致（键只是文件名的载体）"
    assert res.count == len(lines)
    assert all(entry_key_error(k) == "" for k in lib.ordered_keys()), "落盘的键必须都安全"
    assert len(res.warnings) == len(lines), "改造过键的行都要有警告（绝不静默改名）"


def test_seed_falls_back_to_a_safe_key_when_nothing_usable_survives(tmp_path):
    """整行取不出合法键（前导点 / 超长 / 保留设备名）时退化成 `种子-<序号>`，**仍不丢行**。

    这是"绝不静默改名"与"一条都不能丢"的平衡点：键必须安全（它真的是文件名），可内容
    必须活下来。改造不出来就给一个稳定的序号键——比丢掉强得多，且警告里写清了。
    """
    lines = ["..隐藏的回忆", "CON", "长" * 200,
             "知道：药铺的暗格"]
    dst = library_dir("character", "丙", root=_libs(tmp_path))

    res = seed_from_card(_FakeCard(list(lines)), dst)

    lib = load_library(dst).library
    assert [lib.entries[k].title for k in lib.ordered_keys()] == \
        ["..隐藏的回忆", "CON", "长" * 200, "药铺的暗格"]
    assert lib.ordered_keys()[-1] == "药铺的暗格", "本身合法的行仍用原键"
    assert res.count == len(lines)


def test_seed_survives_keys_that_only_differ_in_case(tmp_path):
    """只有大小写不同的两个键**不会**炸掉整张卡的播种（Windows 上它们是同一个文件）。

    `Library.upsert` 对这类冲突当场抛（§4.3 的精神：宁可报错不可丢条），可播种是**批量**
    动作：让异常逃出去，代价不是"少一条"而是"这张卡的迁移全废、且每次重试都在同一处再炸"
    ——永久性全丢。故播种自己要把第二个键让开（加后缀），两条都活下来。

    触发条件（`HP`/`hp`、`NPC`/`npc` 这类缩写与同词小写并存）在老卡上并不罕见。
    """
    dst = library_dir("character", "丙", root=_libs(tmp_path))

    res = seed_from_card(_FakeCard(["ABC", "abc"]), dst)

    lib = load_library(dst).library
    assert sorted(lib.entries[k].title for k in lib.ordered_keys()) == ["ABC", "abc"]
    assert res.count == 2 and any("大小写" in w for w in res.warnings)


def test_seed_carries_every_line_of_the_real_cards_in_the_repo(tmp_path):
    """真卡回归（§9.1 最高约束）：`app/characters/` 里每张真卡的每一行都进得来库。

    这是"用户资产一条不丢"的端到端证明——**读**仓库自带的卡（只读，不改），把每个 `知道`
    标题与库里条目标题逐一对照。卡目录不存在就跳过（离线/裁剪的检出里没有素材）。

    写成**属性**而非行数对拍：用户以后往卡上加行、改行都不该让这条红；能让它红的只有
    "有内容没进库"这一件事。
    """
    from harness.loaders import list_character_paths, load_character_card
    from harness.schemas import CharacterCard

    cards_dir = Path(__file__).resolve().parents[3] / "characters"
    paths = list_character_paths(cards_dir) if cards_dir.is_dir() else []
    if not paths:
        pytest.skip("仓库里没有角色卡素材")

    checked = 0
    for path in paths:
        card = load_character_card(path)
        assert isinstance(card, CharacterCard)
        expected = [ln.strip() for ln in card.knowledge_seed
                    if ln.strip() and ln.strip() != KNOWLEDGE_FALLBACK]
        if not expected:
            continue
        root = _libs(tmp_path) / path.stem           # 每张卡一座独立的库

        res = seed_from_card(card, library_dir("character", card.name, root=root))

        titles = [e.title for e in load_library(
            library_dir("character", card.name, root=root)).library.entries.values()]
        for line in expected:
            want = line
            for prefix in ("知道：", "知道:"):
                if want.startswith(prefix):
                    want = want[len(prefix):].strip()
            assert want in titles, f"{path.name} 的这一行没进库：{want}"
        assert res.count >= len(set(expected))
        checked += 1
    assert checked, "真卡里连一行种子都没有？那这条用例什么也没证明"


def test_seed_on_card_without_seed_is_noop(tmp_path):
    """老卡没有 `knowledge_seed` 属性（或种子全被丢弃）→ **不建库**、不落盘。

    鸭子类型：`schemas.py` 现在还没有这个字段。没有种子就不该凭空造出一座空库——
    "没有库"是合法状态（§9.2 提示词那一节渲染成空串），凭空多出一个目录只会让
    `app/libraries/` 里多出打不开的空壳。
    """
    dst = library_dir("character", "丙", root=_libs(tmp_path))
    res = seed_from_card(_FakeCard(None), dst)
    assert res.created is False and res.count == 0
    assert res.warnings == []
    assert not has_library(dst) and not dst.exists()


# -------------------------------------------------------- 订阅 / 固化 / 渲染 --

def test_subscription_is_live_reference(tmp_path):
    """订阅是**活引用**：源库一改，订阅者的索引立刻跟着变（§3.4）。

    拷贝快照会让世界观各人手里版本不一致——设定本就该全员同步。
    """
    wide = library_dir("wide", "庆国世界观", root=_libs(tmp_path))
    _save([_entry("地理·西线", summary="靠山，常年封冻")], wide,
          name="庆国世界观", scope="wide")
    mine = library_dir("character", "丙", root=_libs(tmp_path))
    _save([_entry("药铺的暗格")], mine, name="丙", owner="丙")

    subs = [("庆国世界观", wide)]
    text = render_library_index(mine, subscriptions=subs)
    assert "借自《庆国世界观》" in text
    assert "地理·西线 — 靠山，常年封冻" in text
    assert "药铺的暗格" in text

    lib = load_library(wide).library
    lib.upsert(_entry("地理·西线", summary="官方改了口径"))
    save_library(lib, wide)
    assert "官方改了口径" in render_library_index(mine, subscriptions=subs)


def test_pin_copies_entry_as_own_and_breaks_reference(tmp_path):
    """固化 = 拷进本库自有条目 + 断开引用（§3.4）：此后源库再改，我的这份不动。

    这是"我对某一节的个人理解与官方版已经分叉"的落点——固化后还跟着源库变，
    这个动作就白做了。
    """
    wide = library_dir("wide", "庆国世界观", root=_libs(tmp_path))
    _save([_entry("地理·西线", summary="官方口径", origin=EntryOrigin(kind="wide"))],
          wide, name="庆国世界观", scope="wide")
    mine = library_dir("character", "丙", root=_libs(tmp_path))
    _save([_entry("药铺的暗格")], mine, name="丙", owner="丙")

    pinned = pin_entry(wide, "地理·西线", mine)
    assert pinned.key == "地理·西线"
    assert pinned.origin.kind == "wide"                    # 出处留着（可追溯）
    assert pinned.origin.turn == BASELINE_TURN             # 绝不当成本场产物参与截断
    assert "地理·西线" in load_library(mine).library.entries

    lib = load_library(wide).library
    lib.upsert(_entry("地理·西线", summary="官方改了口径"))
    save_library(lib, wide)
    assert load_library(mine).library.entries["地理·西线"].summary == "官方口径"


def test_render_index_is_empty_string_when_library_is_empty(tmp_path):
    """无库 / 空副本 → **空串**，连换行都没有（§6.1 硬约束）。

    「无库角色系统提示逐字节不变」这条验收就落在这里：多一个标题行就打破了
    DeepSeek 的前缀缓存，也让老卡的行为漂移。
    """
    assert render_library_index(tmp_path / "从没有过的库") == ""
    empty = library_dir("character", "白落", root=_libs(tmp_path))
    save_library(Library(name="白落", owner="白落"), empty)
    assert render_library_index(empty) == ""


def test_render_index_skips_outdated_and_indexed_false(tmp_path):
    """索引只列 `indexed=true` 且未过时的条目（§3.3）：入口清单不是库的全集。"""
    mine = library_dir("character", "丙", root=_libs(tmp_path))
    _save([_entry("入口"), _entry("藏在图里", indexed=False),
           _entry("过了时的", status="outdated")],
          mine, name="丙", owner="丙")

    text = render_library_index(mine)
    assert "入口" in text
    assert "藏在图里" not in text
    assert "过了时的" not in text


# -------------------------------------------- 本场修订的轮次（§5.3.1） --

def test_record_pending_remembers_the_turn_of_each_revision(tmp_path):
    """`revised_turns` 记下"哪条在本场第几轮被改过"（§5.3.1）。

    撤回要判断的是"这次修订沾没沾被撤回的下文"，而键名清单答不了这个问题——它不知道
    那份现行说法是哪一轮确立的。同一个键在本场被反复修订时取**最晚**的那次：只要有一次
    修订依据的是被撤回的下文，这条现行说法就沾了它，宁可多回退一次也别把"从没发生过的事"
    留在副本里继续回喂。反过来，不带轮次的重复登记（老调用点、或调用方此刻拿不到轮次）
    **不覆盖**已知轮次——擦掉它等于主动放弃回退，那比留着一条稍旧的轮次更糟。
    """
    dst = scene_library_dir(tmp_path / "runs" / "app-1", "丙")
    record_pending(dst, "基线的", revised=True, turn=3)
    assert load_pending(dst).revised_turns == {"基线的": 3}

    record_pending(dst, "基线的", revised=True, turn=7)
    assert load_pending(dst).revised_turns == {"基线的": 7}

    record_pending(dst, "基线的", revised=True)             # 没给轮次
    assert load_pending(dst).revised_turns == {"基线的": 7}  # 已知的轮次不许被擦掉

    record_pending(dst, "基线的")                            # 转成"本场新增"
    assert load_pending(dst).revised_turns == {}
    assert load_pending(dst).added == ["基线的"]


def test_pending_without_rounds_field_stays_readable_and_unrollable(tmp_path):
    """老 `pending.json`（没有 `revised_turns` 字段）照常读出来，只是**无从回退**（§5.3.1）。

    降级方向是"保守不回退"：轮次是"这次修订是否落在 cut 之后"的唯一依据，猜一个值
    （0、1、下标……）都是拿用户的数据赌——猜大了会把一条本场现行说法静默还原成旧文，
    猜小了会把"从没发生过的事"永远留在副本里。而"不回退"恰恰与这次改动之前的行为一致
    （那份副本本来就是这么跑过来的），所以它是不确定性最小的一端。
    这一点绝不能做成"读不出来就整份退回空清单"：那会把 `settled` / `outcome` 一起抹掉，
    结算幂等就断了——坏值只丢它自己那一条的轮次。
    """
    dst = scene_library_dir(tmp_path / "runs" / "app-1", "丙")
    path = pending_path(dst)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "added": ["本场新记的"], "revised": ["基线的"],
        "settled": True, "outcome": "keep", "settled_at": "2026-01-01T00:00:00+00:00",
    }, ensure_ascii=False), encoding="utf-8")

    pending = load_pending(dst)
    assert pending.revised == ["基线的"] and pending.revised_turns == {}
    assert pending.settled is True and pending.outcome == "keep"

    path.write_text(json.dumps({"revised": ["基线的"],
                                "revised_turns": {"基线的": "昨天", "另一条": 4}},
                               ensure_ascii=False), encoding="utf-8")
    pending = load_pending(dst)
    assert pending.revised_turns == {"另一条": 4}             # 坏值只丢自己那一条
    assert pending.revised == ["基线的"]                      # 清单本身照旧读得出来


def test_load_pending_keeps_settlement_when_a_list_field_is_bad(tmp_path):
    """清单里的坏项只丢它自己，**绝不牵连 `settled` / `outcome`**：结算幂等靠它们（§7.5）。

    `pending.json` 是运行产物，现实里会被手改、被别版本写坏。`added` / `revised` 里混进一个
    数字时，若整份清单解析失败而退回空值，"已结算 / 丢弃"就一起没了：下一次结算
    `mark_settled` 会返回 True 并把 outcome 翻成 keep——一次重复点击或重开界面，就可能把
    用户明确选了「丢弃」的一整场记忆永久并入本体，先到先得当场断掉。

    降级方向：`added` / `revised` 只收非空字符串（键必须是字符串，收下一个数字就是凭空造出
    一个不存在的键），整栏不是列表则当空清单——**丢的是"本场所得"里的条目名，最坏结果是
    散场清单少列一行**，用户还能在副本里看见条目本身；而整份作废丢的是结算状态，不可逆。
    """
    dst = scene_library_dir(tmp_path / "runs" / "app-1", "丙")
    path = pending_path(dst)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "added": [123, "本场新记的"], "revised": "整栏被写坏了",
        "revised_turns": {"好的": 4},
        "settled": True, "outcome": "discard", "settled_at": "2026-01-01T00:00:00+00:00",
    }, ensure_ascii=False), encoding="utf-8")

    pending = load_pending(dst)
    assert pending.added == ["本场新记的"]                   # 坏项只丢自己那一条
    assert pending.revised == []                             # 整栏不是列表 → 当空清单
    assert pending.revised_turns == {"好的": 4}
    assert pending.settled is True and pending.outcome == "discard"

    assert mark_settled(dst, "keep") is False                # 先到先得：丢弃不会被翻成保留
    assert load_pending(dst).outcome == "discard"


def test_load_pending_treats_an_unreadable_settled_as_settled_by_its_outcome(tmp_path):
    """`settled` 自己读不出真假时**以 outcome 为准**——方向是刻意选的（§7.5 先到先得）。

    `mark_settled` 永远同时写下 `settled` 与 `outcome`，故 outcome 有值就说明结算发生过。
    把它当成"没结算"是最坏的方向：一次重复点击就能把用户明选的「丢弃」翻成「保留」，角色
    凭空多出一批从没同意永久化的记忆。反过来误判成"已结算"只是让重复的那一次不生效——
    用户看得见、也还能重来。两边的代价差着一个数量级，所以往"已结算"这边降。
    """
    dst = scene_library_dir(tmp_path / "runs" / "app-1", "丙")
    path = pending_path(dst)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"settled": "好久以前", "outcome": "discard",
                                "settled_at": 5}, ensure_ascii=False), encoding="utf-8")

    pending = load_pending(dst)
    assert pending.settled is True and pending.outcome == "discard"
    assert pending.settled_at == ""                          # 读不出来的时间戳清空，不猜
    assert mark_settled(dst, "keep") is False

    path.write_text(json.dumps({"settled": "好久以前"}, ensure_ascii=False), encoding="utf-8")
    assert load_pending(dst).settled is False                # 连 outcome 都没有 → 当没结算


def test_truncate_drops_the_rounds_of_the_entries_it_deletes(tmp_path):
    """被截掉的条目连同它的轮次一起从 pending 里摘掉：不留幽灵键。

    留着它，后续撤回会拿着一个已经不存在的键去本体找原文（§5.3.1 的回退路径），找不到
    就报一条"没能还原"的假警报——用户照着去查，发现库里根本没有这条。
    """
    dst = scene_library_dir(tmp_path / "runs" / "app-1", "丙")
    lib = Library(name="丙", owner="丙")
    lib.upsert(_entry("晚的", origin=EntryOrigin(kind="scene", scene="贝克街221B", turn=9)))
    save_library(lib, dst)
    record_pending(dst, "晚的", revised=True, turn=9)

    assert truncate_copy_after_turn(dst, 5) == 1
    pending = load_pending(dst)
    assert pending.revised == [] and pending.revised_turns == {}


def test_truncate_with_unreadable_cut_deletes_nothing(tmp_path):
    """cut 读不出来（None / 坏串）→ 一条都不删，**绝不抛**。

    与"条目的坏轮次按 0 计"相反：0 对**条目**意味着"最早、永不误截"，对**撤回的位置**
    却意味着"把本场所有条目一次删光"（包括本轮刚记下的）。判据读不出来时唯一安全的动作
    是什么都不做——这次调用多半只是调用方传错了参数，下次撤回再试。
    """
    dst = scene_library_dir(tmp_path / "runs" / "app-1", "丙")
    lib = Library(name="丙", owner="丙")
    lib.upsert(_entry("晚的", origin=EntryOrigin(kind="scene", scene="贝克街221B", turn=9)))
    save_library(lib, dst)

    assert truncate_copy_after_turn(dst, None) == 0
    assert truncate_copy_after_turn(dst, "五") == 0
    assert list(load_library(dst).library.entries) == ["晚的"]
    assert truncate_copy_after_turn(dst, 5) == 1              # 正常 cut 照旧生效


# ------------------------------------------------ 读订阅文件（§3.4/§8.2） --

def test_load_subscriptions_reads_name_and_source_dir(tmp_path):
    """`<库目录>/subscriptions.json` → `[(库名, 源库目录), …]`（§8.2）。

    返回形状恰是 `render_library_index(..., subscriptions=…)` 收的那一种：读侧只有这一
    处口径，界面（knowledge_editor 转调本函数）与引擎看到的必须是同一份订阅。
    """
    root = _libs(tmp_path)
    wide = library_dir("wide", "庆国世界观", root=root)
    _save([_entry("地理·西线")], wide, name="庆国世界观", scope="wide")
    mine = library_dir("character", "丙", root=root)
    _save([_entry("暗格")], mine, name="丙", owner="丙")
    (mine / "subscriptions.json").write_text(json.dumps(
        {"schema": 1, "libraries": [
            {"name": "庆国世界观", "kind": "wide", "path": "wide/庆国世界观"}]},
        ensure_ascii=False), encoding="utf-8")

    rows = load_subscriptions(mine, root=root)
    assert rows == [("庆国世界观", wide)]


def test_load_subscriptions_is_lenient_and_never_raises(tmp_path):
    """缺文件 / 坏 JSON / 坏项 / 逃逸路径：只丢它自己，**绝不抛**（与 load_pending 同口径）。

    一个手改坏的项不该让其余订阅一起失效；而读库根目录之外的东西是安全边界——宁可少一条
    订阅，也不能让引擎去读库根外的目录（`_safe_rel` 认写法、`_inside_root` 认结果，两道都要）。
    盘符绝对路径（`C:/Windows`）最阴：pathlib 认它时会把左边的 root 整个丢掉。
    """
    root = _libs(tmp_path)
    mine = library_dir("character", "丙", root=root)
    _save([_entry("暗格")], mine, name="丙", owner="丙")

    assert load_subscriptions(mine, root=root) == []          # 文件缺失是常态

    subs_path = mine / "subscriptions.json"
    subs_path.write_text("{ 这不是 json", encoding="utf-8")
    assert load_subscriptions(mine, root=root) == []

    subs_path.write_text(json.dumps(["不是对象"]), encoding="utf-8")
    assert load_subscriptions(mine, root=root) == []

    subs_path.write_text(json.dumps({"schema": 1, "libraries": [
        "不是对象", 42,
        {"name": "缺路径"},
        {"name": "逃逸", "path": "wide/../../秘密"},
        {"name": "绝对", "path": "C:/Windows"},
        {"name": "好库", "path": "wide/好库"},
        {"name": "好库", "path": "wide/好库"},           # 重复项去重
    ]}, ensure_ascii=False), encoding="utf-8")
    rows = load_subscriptions(mine, root=root)
    assert [name for name, _ in rows] == ["好库"], "只有一个合法项该留下"
    assert rows[0][1] == root / "wide" / "好库"
    # 前导斜杠只是被规范化掉（`/etc` → `etc`），落在库内——它逃不出根，不必丢。
    assert _safe_rel("/etc") == "etc"
    assert _inside_root(root / "etc", root) is True


def test_load_subscriptions_defaults_the_name_to_the_dir_name(tmp_path):
    """项里没写名字（或写了个空的）→ 用库目录名兜底，绝不产出一条无名订阅。"""
    root = _libs(tmp_path)
    mine = library_dir("character", "丙", root=root)
    _save([_entry("暗格")], mine, name="丙", owner="丙")
    (mine / "subscriptions.json").write_text(json.dumps(
        {"schema": 1, "libraries": [{"path": "wide/庆国世界观"},
                                    {"name": "   ", "path": "wide/朝局"}]},
        ensure_ascii=False), encoding="utf-8")

    assert [name for name, _ in load_subscriptions(mine, root=root)] == \
        ["庆国世界观", "朝局"]
