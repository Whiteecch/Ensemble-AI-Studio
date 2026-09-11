# Ensemble-AI-Studio

本目录是项目实现（Python 包 `harness`）。完整说明见仓库根目录的 [README.md](../README.md)（English）与 [README.zh-CN.md](../README.zh-CN.md)（简体中文）；
设计与使用文档见 [docs/](../docs/)。

快速开始：

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev,gui]"   # Windows；其它平台为 .venv/bin/python
.venv/Scripts/python -m harness.gui.app                # 桌面应用
.venv/Scripts/python -m pytest -q                      # 测试
```
