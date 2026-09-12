"""runner --demo 流式演示（离线 stub + 脚本化 stdin）端到端测试。

把 runner._read 注入成队列消费者，模拟脚本化输入流：第一行=开场指令，随后每行=
回车（空=自动续演）；队列耗尽返回 "" 视作管道 EOF。断言打印输出含双方角色台词、
场景自然收束（打烊），且 --export 转录落盘。
"""
import json
from pathlib import Path

import pytest

from harness import runner


def _write_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    scene = tmp_path / "scenes" / "贝克街221B.json"
    a = tmp_path / "characters" / "甲.json"
    b = tmp_path / "characters" / "乙.json"
    models = tmp_path / "config" / "models.yaml"
    scene.parent.mkdir(parents=True, exist_ok=True)
    a.parent.mkdir(parents=True, exist_ok=True)
    models.parent.mkdir(parents=True, exist_ok=True)
    scene.write_text(json.dumps({
        "name": "贝克街221B", "participants": ["甲", "乙"],
        "hard_boundary": {"type": "time", "value": "22:00", "desc": "打烊"}},
        ensure_ascii=False), encoding="utf-8")
    a.write_text(json.dumps({"name": "甲", "personality": {"描述": "冷静"}},
                            ensure_ascii=False), encoding="utf-8")
    b.write_text(json.dumps({"name": "乙", "personality": {"描述": "锐利"}},
                            ensure_ascii=False), encoding="utf-8")
    models.write_text(
        "think:\n  backend: stub\n  model: stub\n  params: {}\n"
        "speak:\n  backend: stub\n  model: stub\n  params: {}\n", encoding="utf-8")
    return scene, a, b, models


def _make_reader(lines: list[str]):
    """返回可注入 runner._read 的函数：依次吐行，耗尽后返回 ''（模拟管道 EOF）。"""
    queue = list(lines)

    def _read(prompt: str) -> str:
        return queue.pop(0) if queue else ""

    return _read


def test_demo_stream_offline_alternates_and_closes(
        tmp_path: Path, monkeypatch, capsys):
    scene_p, a_p, b_p, models_p = _write_fixture(tmp_path)
    export_p = tmp_path / "transcript.md"
    monkeypatch.setattr(
        runner, "_read",
        _make_reader(["星光与海风，夜色正浓。", "", ""]))   # 开场 + 两个回车

    runner.main([
        "--demo", "--stub",
        "--scene", str(scene_p),
        "--characters", str(a_p), str(b_p),
        "--models", str(models_p),
        "--closing-at-block", "3",
        "--run-root", str(tmp_path / "runs"),
        "--export", str(export_p),
    ])

    out = capsys.readouterr().out
    # 开场指令即时打印 + 双方角色各至少一句
    assert "星光与海风" in out
    assert "甲" in out and "乙" in out
    # 场景经块钟自然收束（打烊）而非提前退出
    assert "已收束" in out
    # --export 转录含双方
    assert export_p.exists()
    text = export_p.read_text(encoding="utf-8")
    assert "甲" in text and "乙" in text


def test_demo_eof_after_opening_auto_advances_to_close(
        tmp_path: Path, monkeypatch, capsys):
    """stdin 非 TTY：开场行之后立即 EOF → 每步回车读返回 ''，按 tiny-sleep 自动
    续演到块钟收束，不卡死、不崩溃。"""
    scene_p, a_p, b_p, models_p = _write_fixture(tmp_path)
    monkeypatch.setattr(
        runner, "_read",
        _make_reader(["说完第一句就 EOF。"]))               # 开场后即 EOF
    runner.main([
        "--demo", "--stub",
        "--scene", str(scene_p),
        "--characters", str(a_p), str(b_p),
        "--models", str(models_p),
        "--closing-at-block", "3",
        "--run-root", str(tmp_path / "runs_eof"),
    ])
    out = capsys.readouterr().out
    assert "说完第一句就 EOF。" in out
    assert "甲" in out or "乙" in out
    assert "已收束" in out
