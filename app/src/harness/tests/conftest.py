import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # src/

@pytest.fixture
def run_root(tmp_path: Path) -> Path:
    """每次测试独立的 runs 根，避免污染真实目录。"""
    root = tmp_path / "runs"
    root.mkdir(parents=True)
    return root

@pytest.fixture(autouse=True)
def _isolate_user_settings(tmp_path_factory, monkeypatch):
    """**整场测试**都把设置读写重定向到临时目录。

    为什么必须：MainWindow 在不显式传 settings 时会读取 `default_settings_path()`——
    那是**用户真实**的 %APPDATA%/Ensemble-AI-Studio/settings.json。于是（a）测试结果会随用户
    本机配置而变（曾出现"测试期望默认主题、实际读到用户改过的深色主题"而红），
    （b）更糟的是 `apply_theme/apply_language` 会**把测试写进用户的真实配置**（确实发生过）。
    这里把 default_settings_path 指向一次性临时路径，测试与用户数据从此互不干扰。
    """
    from harness.gui import settings as settings_mod

    sandbox = tmp_path_factory.mktemp("user-settings") / "settings.json"
    monkeypatch.setattr(settings_mod, "default_settings_path", lambda: sandbox)
    return sandbox
