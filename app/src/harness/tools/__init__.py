"""命令行工具包：素材导入等「不依赖界面」的批处理入口。

现在只有一条命令——`python -m harness.tools.import_cards`
（见 `tools/import_cards.py`：把大模型按 templates/*.md 填好的 Markdown 导进素材库）。
本包不 import PySide6，纯 CLI 环境可直接跑。
"""
