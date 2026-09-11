import json
from pathlib import Path

from harness.memory import CharacterMemory


def test_state_append_and_read(run_root: Path):
    mem = CharacterMemory(run_root / "scene1", "丁")
    mem.append_state({"aroused": 0.6, "goal_progress": 0.1})
    mem.append_state({"aroused": 0.2, "goal_progress": 0.4})
    assert mem.load_state() == [{"aroused": 0.6, "goal_progress": 0.1},
                                {"aroused": 0.2, "goal_progress": 0.4}]


def test_impressions_capped_to_max_lines(run_root: Path):
    mem = CharacterMemory(run_root / "scene1", "丁")
    for i in range(205):
        mem.append_impression("戊", f"第{i}句印象", max_lines=200)
    text = mem.read_impressions()["戊"]
    assert "第0句印象" not in text   # 最旧被丢
    assert "第204句印象" in text


def test_impressions_isolated_per_character(run_root: Path):
    a = CharacterMemory(run_root / "scene1", "丁")
    b = CharacterMemory(run_root / "scene1", "戊")
    a.append_impression("戊", "甲眼中的戊")
    assert b.read_impressions() == {}   # 乙读不到甲的印象文件夹


# ------------------------------------------------- 轮次感知（§6.1 截断式撤回） -----
def test_truncate_after_turn_drops_later_state_entries(run_root: Path):
    """state.jsonl 按轮次截断：turn > cut 的条目消失，其余原样。"""
    mem = CharacterMemory(run_root / "scene1", "丁")
    mem.append_state({"turn": 0, "urge": 0.1})
    mem.append_state({"turn": 1, "urge": 0.2})
    mem.append_state({"turn": 2, "urge": 0.3})
    mem.append_state({"turn": 2, "urge": 0.4})

    mem.truncate_after_turn(1)

    assert mem.load_state() == [{"turn": 0, "urge": 0.1}, {"turn": 1, "urge": 0.2}]
    mem.truncate_after_turn(1)                       # 幂等
    assert len(mem.load_state()) == 2
    mem.truncate_after_turn(-1)                      # 全清也允许（cut 早于开场）
    assert mem.load_state() == []


def test_impressions_jsonl_is_source_of_truth_with_md_rendering(run_root: Path):
    """印象以 impressions/<他人>.jsonl 为准（逐条带轮次），.md 只是人读渲染。"""
    mem = CharacterMemory(run_root / "scene1", "丁")
    mem.append_impression("戊", "她话少但戳人。", turn=1)
    mem.append_impression("戊", "她好像在等我说。", turn=3)

    recs = [json.loads(line) for line in
            (mem.folder / "impressions" / "戊.jsonl").read_text("utf-8").splitlines()]
    assert recs == [{"turn": 1, "text": "她话少但戳人。"},
                    {"turn": 3, "text": "她好像在等我说。"}]
    md = (mem.folder / "impressions" / "戊.md").read_text(encoding="utf-8")
    assert md == "她话少但戳人。\n她好像在等我说。\n", "md 是同一批印象的人读渲染"
    assert mem.read_impressions()["戊"] == md, "读的仍是同一份文本（给提示词用）"


def test_truncate_after_turn_filters_impressions_and_rewrites_md(run_root: Path):
    """撤回截断印象：jsonl 被过滤，md 同步重写成剩余那批（不残留被丢的那句）。"""
    mem = CharacterMemory(run_root / "scene1", "丁")
    mem.append_impression("戊", "第一轮的印象。", turn=1)
    mem.append_impression("戊", "第三轮的印象。", turn=3)
    mem.append_impression("乙", "第一轮的印象。", turn=1)
    mem.append_impression("丙", "第五轮的印象。", turn=5)

    mem.truncate_after_turn(1)

    assert mem.read_impressions()["戊"] == "第一轮的印象。\n"
    assert "第三轮的印象。" not in (
        mem.folder / "impressions" / "戊.md").read_text(encoding="utf-8")
    assert mem.read_impressions()["乙"] == "第一轮的印象。\n", "cut 之内的照旧保留"
    assert mem.read_impressions()["丙"] == "", "cut 之后的另一个人的印象同样被截掉"


def test_read_impressions_falls_back_to_legacy_md_only_folder(run_root: Path):
    """老存档只有 .md（无 jsonl）：照旧读得到（兼容路径不得因为改格式而失效）。"""
    mem = CharacterMemory(run_root / "scene1", "丁")
    imp = mem.folder / "impressions"
    imp.mkdir(parents=True, exist_ok=True)
    (imp / "戊.md").write_text("老格式的印象。\n", encoding="utf-8")

    assert mem.read_impressions()["戊"] == "老格式的印象。\n"
    mem.truncate_after_turn(0)              # 没有 jsonl 可截：不动 legacy md
    assert mem.read_impressions()["戊"] == "老格式的印象。\n"


def test_truncate_after_turn_survives_missing_and_broken_files(run_root: Path):
    """绝不抛：没有 state.jsonl / jsonl 里有坏行 / 目录不存在，都当无事发生。"""
    mem = CharacterMemory(run_root / "scene1", "丁")
    mem.truncate_after_turn(3)                       # 什么都没有
    (mem.folder / "state.jsonl").write_text('{"turn": 5}\n坏行\n', encoding="utf-8")
    (mem.folder / "impressions" / "戊.jsonl").write_text('{"turn": 5, "text": "x"}\n坏行\n',
                                                           encoding="utf-8")
    mem.truncate_after_turn(1)
    assert mem.load_state() == []
    assert mem.read_impressions()["戊"] == ""
