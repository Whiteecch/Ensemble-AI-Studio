import asyncio
from pathlib import Path

from harness.engine import SceneEngine


def _paths(root: Path):
    return (root / "scenes" / "餐厅.json", root / "characters" / "甲.json",
            root / "characters" / "乙.json", root / "config" / "models.yaml")


def test_e2e_demo_offline_closes_and_exports(tmp_path: Path):
    scene_p, a_p, b_p, models_p = _paths(tmp_path)
    for p, content in [
        (scene_p, {"name": "餐厅", "participants": ["甲", "乙"],
                   "hard_boundary": {"type": "time", "value": "22:00", "desc": "打烊"}}),
        (a_p, {"name": "甲", "personality": {"描述": "冷静，观察多于开口"}}),
        (b_p, {"name": "乙", "personality": {"描述": "锐利，话多且快"}}),
    ]:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(__import__("json").dumps(content, ensure_ascii=False), encoding="utf-8")
    models_p.parent.mkdir(parents=True, exist_ok=True)
    models_p.write_text(
        "think:\n  backend: stub\n  model: stub\n  params: {}\n"
        "speak:\n  backend: stub\n  model: stub\n  params: {}\n", encoding="utf-8")

    eng = SceneEngine(scene_p, [a_p, b_p], models_p, run_root=tmp_path / "runs",
                      closing_at_block=3)
    asyncio.run(eng.open_scene())
    for _ in range(5):
        if eng.closed:
            break
        asyncio.run(eng.step(1))
    assert eng.closed is True

    # 无圈场景照常跑块：在场轴 = 场景名，演员表里的角色确实开口
    msgs = asyncio.run(eng.messages())
    assert all(m["in_scene"] == "餐厅" for m in msgs), "in_scene 即场景名（无圈层）"
    assert any(m["speaker_type"] == "character" for m in msgs), "在场者照常开口"

    out = tmp_path / "transcript.md"
    asyncio.run(eng.export_transcript(out))
    assert out.exists() and "乙" in out.read_text(encoding="utf-8")
