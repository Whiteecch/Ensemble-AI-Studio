"""桌面应用入口：解析旗标（缺省即内置演示素材）→ 建 worker + 主窗口 → exec。

**不再有启动小窗**（§3.3）：主界面直接起来，且起手是**未打开场景**的空状态页
（中央「打开场景」「新建场景」「新建角色」三个按钮），用户从顶栏「场景」菜单或那三个
按钮自己挑场次。`--scene/--characters` 只是素材缺省（角色/场景库目录、`开新场` 与
`--autostart` 用），不再是「启动即开场」的意思。
`--autostart` 保留旧语义的**后半段**：跳过一切等待，直接按 `--scene/--characters`
（或内置素材）开场——CLI/脚本/离屏冒烟用得上。

启动时读一次用户设置（gui.settings.SettingsStore → 用户数据目录的 settings.json），
把配色/语言/自动保存周期与 api 配置交给 MainWindow（顶栏「设置」菜单即其入口）。
设置**不覆盖**素材类 CLI 旗标：场景/角色/模型/竞价仍以本函数解析的结果为准。
**api 配置例外**：它是设置的主场（§2.1①），优先级为
「显式 --stub > 设置里的 api_key > 环境变量 DEEPSEEK_API_KEY > stub」
（见 resolve_api_key / resolve_live）——用户在设置里填过 key 就该直接生效，
不必再配环境变量；显式 --stub 仍然最优先。

运行（须已 `pip install PySide6`，见 pyproject [optional-dependencies].gui）：
  python -m harness.gui.app              # 检测到 DEEPSEEK_API_KEY → 真实模型，否则 stub
  python -m harness.gui.app --stub       # 强制离线 stub
  python -m harness.gui.app --autostart  # 不等待，直接按 CLI/内置素材开演
  QT_QPA_PLATFORM=offscreen python -m harness.gui.app --stub   # 无显示器冒烟

模块顶层不 import PySide6（可选依赖），全部延迟到 run() 内导入——纯 CLI 环境 import
harness.gui 包不会硬依赖 Qt。worker 线程跑唯一 asyncio 循环，主线程只做 Qt 事件循环。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

#: app/ 根（scenes/characters/config 所在目录），保证任意 cwd 下双击/模块启动都命中素材。
_APP_DIR = Path(__file__).resolve().parents[3]
_DEFAULT_SCENE = _APP_DIR / "scenes" / "餐厅.json"
_DEFAULT_CHARACTERS = [
    _APP_DIR / "characters" / "甲.json",
    _APP_DIR / "characters" / "乙.json",
]
_DEFAULT_MODELS = _APP_DIR / "config" / "models.yaml"
_DEFAULT_BID = _APP_DIR / "config" / "bid.demo.yaml"
_DEFAULT_RUN_ROOT = _APP_DIR / "runs"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="harness-gui",
        description="多智能体角色扮演桌面端：角色自主持续对话，人类随时插话。")
    ap.add_argument("--scene", type=Path, help="场景 JSON；缺省内置 scenes/贝克街221B.json")
    ap.add_argument("--characters", type=Path, nargs="+",
                    help="角色卡 JSON；缺省内置 甲.json 乙.json")
    ap.add_argument("--models", type=Path,
                    help="基础模型 yaml；缺省内置 config/models.yaml（live 自动切 "
                         "models.live.yaml）")
    ap.add_argument("--bid", type=Path, help="竞价参数 yaml；缺省内置 bid.demo.yaml")
    ap.add_argument("--stub", action="store_true",
                    help="强制离线 stub（否则检测到 DEEPSEEK_API_KEY 即用真实模型）")
    ap.add_argument("--closing-at-block", type=int, default=None,
                    help="块数收束上限；缺省 None=禁用（App 永不按块数结束，只由虚拟钟"
                         "走到打烊或用户停止收束）。仅供调试时显式设一个小值复现兜底")
    ap.add_argument("--opening", type=str, default="", help="开场设定初值（可留空）")
    ap.add_argument("--autostart", action="store_true",
                    help="不等待用户操作，直接按 --scene/--characters（或内置素材）开场"
                         "（缺省进空状态页，由用户在界面里挑场景）")
    return ap.parse_args(argv)


def resolve_api_key(stub: bool, settings_key: str | None,
                    env_key: str | None = None) -> str | None:
    """本次运行可用的 API Key（None = 只能跑离线 stub）。**纯函数**，便于直接测。

    优先级（§2.1① 设置 / §6 设置与用户数据）：显式 `--stub` > 设置里的 api_key >
    环境变量 `DEEPSEEK_API_KEY` > 无。设置里的 key 是**主来源**——用户填过一次就该生效，
    哪怕环境里没有这个变量；`--stub` 是显式的离线旗标，最优先（谁也别想把它顶掉）。
    """
    if stub:
        return None
    key = str(settings_key or "").strip()
    if key:
        return key
    return str(env_key or "").strip() or None


def resolve_live(stub: bool, api_key: str | None) -> bool:
    """是否用真实后端：没被 `--stub` 强制离线、且确实有 key（同 runner 语义）。**纯函数**。"""
    return (not stub) and bool(api_key)


def run(argv: list[str] | None = None) -> int:
    """组装 QApplication + SceneWorker + MainWindow 并开场；返回应用退出码。

    单事件循环纪律：SceneWorker(QThread) 在后台线程跑唯一 asyncio loop 驱动引擎，
    GUI 线程只运行 Qt 事件循环，二者经跨线程信号/投递协作，绝不重复 asyncio.run。
    """
    from PySide6.QtWidgets import QApplication

    from .main_window import AppConfig, MainWindow
    from .settings import SettingsStore
    from .worker import SceneWorker

    args = _parse_args(argv)
    # 用户设置先读（配色/语言/自动保存周期/api 配置）：素材类（场景/角色/模型/竞价）不
    # 覆盖任何 CLI 值——CLI 与内置素材仍是本场次的权威来源；**api 配置则以设置为主来源**
    # （§2.1①：用户填过 key 就该生效，不再要求环境变量）。优先级（高 → 低）：
    #   显式 --stub > 设置里的 api_key > 环境变量 DEEPSEEK_API_KEY > stub。
    settings = SettingsStore().load()
    api_key = resolve_api_key(args.stub, settings.api_key,
                              os.environ.get("DEEPSEEK_API_KEY"))
    scene = args.scene or _DEFAULT_SCENE
    characters = args.characters or list(_DEFAULT_CHARACTERS)
    models = args.models or _DEFAULT_MODELS
    bid = args.bid or _DEFAULT_BID
    live = resolve_live(args.stub, api_key)      # 缺 key 自动落 stub（同 runner 语义）

    app = QApplication(sys.argv[:1])             # 只传程序名，免得 Qt 吃我们的旗标
    app.setApplicationName("多智能体角色扮演")
    # 语言与配色写进 app 属性：library 的对话框（无窗口上下文）按这两个属性取文案与
    # 样式表（§7 / §2.1①）——不先写，已存英文/深色的用户会看到中文、默认配色的弹窗。
    app.setProperty("language", settings.language)
    app.setProperty("theme", settings.theme)

    cfg = AppConfig(
        scene=scene, characters=list(characters), models=models, bid=bid,
        run_root=_DEFAULT_RUN_ROOT, live=live, api_key=api_key,
        closing_at_block=args.closing_at_block, opening=args.opening or "")

    worker = SceneWorker()
    # 保存的设置传进窗口：配色在 show() 之前就铺好，菜单勾选态与设置弹窗初值都随之对齐。
    window = MainWindow(worker, cfg, settings=settings)
    worker.start()                               # 起后台 asyncio 循环
    window.show()                                # 起手是空状态页（未打开场景，§3.3）
    if args.autostart:
        # 显式要「直接开演」：走窗口的**同一个**续演询问（§5）——有存档就问「接着上次演吗」，
        # 答 Yes 时等开场成功由窗口消费（_on_scene_info → worker.restore_scene()），故必须
        # 问在开场前。缺省不做这一步：场景由用户在空状态页/场景菜单里自己挑。
        window._maybe_resume(scene)
        window.start_session()                   # 向 worker 投递开场
    code = app.exec()
    # 评审 #5：收尾等待放宽 + join，先停引擎线程再让 worker/window 随栈析构，
    # 杜绝 destroy-while-running。
    worker.shutdown(10000)                       # 撤 autoplay + aclose + join（内部 wait）
    worker.wait(0)                               # 兜底确认线程已让出（幂等）
    return code


def main(argv: list[str] | None = None) -> None:
    raise SystemExit(run(argv))


if __name__ == "__main__":
    main()
