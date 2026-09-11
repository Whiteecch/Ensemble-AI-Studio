"""档位统一 flash（2026-09）：live.yaml think/speak 全 deepseek-v4-flash 且关思考；
app/config 与 app/src/harness 源码中不得残留任何 v4-pro 型号引用（用户可见文本/注释）。"""
from pathlib import Path

from harness.loaders import load_models

_APP = Path(__file__).resolve().parents[3]      # <repo>/app
_CONFIG = _APP / "config"
_HARNESS = _APP / "src" / "harness"
_SOURCE_SUFFIXES = {".py", ".yaml", ".yml", ".md", ".json"}
# tests/ 子树是测试自身（会为断言提及被禁型号），不计入「产品/用户可见文本」扫描。
_SKIP_DIRS = {"__pycache__", ".venv", ".pytest_cache", "tests"}


def test_live_speak_and_think_are_v4_flash_and_non_thinking():
    cfg = load_models(_CONFIG / "models.live.yaml")
    assert cfg["think"].model == "deepseek-v4-flash"
    assert cfg["speak"].model == "deepseek-v4-flash"
    # speak 便宜且非思考：thinking=disabled（temperature 只在非思考模式下随请求体下发）
    assert cfg["speak"].params.get("thinking") == "disabled"
    assert cfg["think"].params.get("thinking") == "disabled"


def test_no_v4_pro_string_anywhere_in_config_and_harness_source():
    hits: list[str] = []
    for root in (_CONFIG, _HARNESS):
        for p in root.rglob("*"):
            if not p.is_file() or p.suffix not in _SOURCE_SUFFIXES:
                continue
            if any(part in _SKIP_DIRS for part in p.parts):
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            if "v4-pro" in text or "deepseek-v4-pro" in text:
                hits.append(str(p.relative_to(_APP)))
    assert not hits, f"app/config + app/src/harness 仍残留 v4-pro 引用: {hits}"
