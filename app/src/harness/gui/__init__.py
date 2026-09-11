"""桌面端（PySide6）：自主对话三栏界面 + SceneWorker 单事件循环驱动。

包内模块：
  worker.py        SceneWorker(QThread)——GUI 线程外的唯一 asyncio 循环内跑引擎
  main_window.py   MainWindow——左栏（场景/指标/冲动/运行控制，整栏可滚动）+ 中央对白
                   （含「保存场景」按钮位）+ 右栏角色卡（实时分量 + 「查看详情」弹窗）
  library.py       角色卡/场景编辑器 + 场景库 + 角色详情（只读）
  app.py           run()/main() 入口与命令行旗标（缺省即内置演示素材）

PySide6 属可选依赖（[project.optional-dependencies].gui），故本包仅在
pip install 了 PySide6 时才可导入；无 Qt 的纯 CLI 环境不要 import gui。
"""
