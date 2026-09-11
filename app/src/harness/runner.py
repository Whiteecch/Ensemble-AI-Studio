"""CLI：最小事件消费者（技术方案 §9.4）。--live 需 DEEPSEEK_API_KEY。

--demo（流式演示，本迭代新增）：真人只给一条开场指令/导播，两名角色他一句、她一句
逐句实时打印（demo_alternate 叠加强制轮流），直到场景块钟收束。人在窗外——不是
说话角色，非空输入只是导演注入（世界事件），角色下一块会回应。demo 缺省 stub 后端
离线可跑；给 DEEPSEEK_API_KEY 且不 --stub 时切 DeepSeek（models.live.yaml），缺 key
则打印提示并回退 stub，绝不崩。

单事件循环约束（评审轮 1 裁决）：SceneEngine 走 AsyncSqliteSaver 时绑定构图首次
所在 loop，全部 async 调用（含 aclose）必须在同一个 loop 内执行。因此本 CLI 把
open/step/export 全放进一个 asyncio.run()，不反复 asyncio.run；stdin 经
asyncio.to_thread 在子线程阻塞读（input 若在主 loop 直接调用会卡住整个事件循环）。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from .engine import SceneEngine

_QUIT_WORDS = {"q", "quit", "exit", "结束", "退出"}

# 内置演示素材缺省值：不传 --scene/--characters/--models 也能一条指令跑 demo。
_DEFAULT_SCENE = Path("scenes/贝克街221B.json")
_DEFAULT_CHARACTERS = [Path("characters/福尔摩斯.json"),
                       Path("characters/华生.json")]
_DEFAULT_MODELS = Path("config/models.yaml")
_DEFAULT_BID_DEMO = Path("config/bid.demo.yaml")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Ensemble-AI-Studio 命令行 harness")
    ap.add_argument("--scene", type=Path,
                    help="场景 JSON；缺省内置 scenes/贝克街221B.json")
    ap.add_argument("--characters", type=Path, nargs="+",
                    help="角色卡 JSON；缺省内置 福尔摩斯.json 华生.json")
    ap.add_argument("--models", type=Path,
                    help="models.yaml；缺省内置 config/models.yaml")
    ap.add_argument("--bid", type=Path)
    ap.add_argument("--steps", type=int, default=8)
    ap.add_argument("--closing-at-block", type=int, default=200,
                    help="块钟兜底收束上限（缺省大值，让场景虚拟钟打烊）")
    ap.add_argument("--start-time", type=str, default=None,
                    help="场景开始时间 HH:MM（缺省取场景 start_time / 21:30）")
    ap.add_argument("--run-root", type=Path, default=None)
    ap.add_argument("--export", type=Path)
    # 遗留 --live（无 --demo）：语义不变，逐块静默跑完 N 步后导出。
    ap.add_argument("--live", action="store_true")
    # ---- --demo 流式演示参数 ----
    ap.add_argument("--demo", action="store_true", help="流式演示：逐句实时打印")
    ap.add_argument("--prompt", type=str, default=None,
                    help="开场指令；缺省在 stdin 提示输入（直接回车用默认开场）")
    ap.add_argument("--stub", action="store_true",
                    help="demo 强制离线 stub（缺 DEEPSEEK_API_KEY 时自动回退）")
    ap.add_argument("--pace", choices=["auto", "enter"], default="enter",
                    help="auto=每句自动续演；enter=回车才走下一句/q 提前结束")
    ap.add_argument("--max-steps", type=int, default=60,
                    help="demo 兜底步数护栏（防永不收束）")
    return ap.parse_args(argv)


def _read(prompt: str) -> str:
    """阻塞读一行 stdin（供 demo 的开场指令与 pace 两步共用，测试可整体替换注入）。

    stdin 非 TTY（管道重定向）：不给 prompt 以免污染 stdout；读到 EOF（Ctrl-D /
    管道耗尽）返回 '' —— 调用方把它当「空行自动续演」，不崩溃。
    """
    try:
        if sys.stdin.isatty():
            return input(prompt)
        return input()
    except EOFError:
        return ""


async def _to_thread_read(prompt: str) -> str:
    return await asyncio.to_thread(_read, prompt)


def _default_run_root(args: argparse.Namespace, demo: bool) -> Path:
    """run_root 缺省：遗留 runs/；demo 每次唯一 runs/demo-<uuid>（相对 cwd）。

    demo 用唯一目录而非共享 runs/demo：AsyncSqliteSaver 按 run_root 落盘，
    共享同一目录会在多次运行间累积转录（含开场与台词串场）。每场自带目录即
    天然隔离，无需开场清空、绝不动显式 --run-root。
    """
    if args.run_root is not None:
        return Path(args.run_root)
    if demo:
        import uuid
        return Path("runs") / f"demo-{uuid.uuid4().hex[:8]}"
    return Path("runs")


async def _emit_new(engine: SceneEngine, start: int) -> int:
    """打印 start 之后新增的全部消息，逐句即时（flushed）。返回最新消息数。

    跳过空内容消息（DeepSeek 偶发 thinking 模式返回空 content；display 层过滤，
    底层韧性重试属 I3 待办）。"""
    msgs = await engine.messages()
    for m in msgs[start:]:
        if not (m.get("content") or "").strip():
            continue
        print(f"**[{m['id']}] {m['speaker']}**: {m['content']}", flush=True)
    return len(msgs)


async def _export_if_requested(args: argparse.Namespace, engine: SceneEngine) -> None:
    if args.export:
        args.export.parent.mkdir(parents=True, exist_ok=True)
        await engine.export_transcript(args.export)
        print(f"转录导出: {args.export}")


async def _legacy_async_main(args: argparse.Namespace, models: Path,
                             api_key: str | None) -> None:
    # run_root 先建目录：engine 首次构图会走 AsyncSqliteSaver（真实 sqlite 落地，
    # 绑定本 loop）——CLI 正是要演示持久化路径，故在此保证 run_root.is_dir() 成立。
    run_root = _default_run_root(args, demo=False)
    run_root.mkdir(parents=True, exist_ok=True)
    eng = SceneEngine(args.scene, args.characters, models,
                      run_root=run_root, bid_path=args.bid,
                      api_key=api_key, closing_at_block=args.closing_at_block,
                      start_time=args.start_time)
    await eng.open_scene()
    for _ in range(args.steps):
        if eng.closed:
            break
        await eng.step(1)
    msgs = await eng.messages()
    print(f"[closed={eng.closed}] 共 {len(msgs)} 条消息")
    await _export_if_requested(args, eng)
    await eng.aclose()          # 关闭 sqlite 连接（须在本 loop 内；MemorySaver 路径为 no-op）


async def _demo_async_main(args: argparse.Namespace, models: Path,
                           api_key: str | None) -> None:
    # 单 loop 内完成全部 demo 流程。run_root 缺省为每场唯一 runs/demo-<uuid>
    # （见 _default_run_root），天然隔离多次运行的转录；显式 --run-root 仍尊重。
    run_root = _default_run_root(args, demo=True)
    run_root.mkdir(parents=True, exist_ok=True)
    eng = SceneEngine(args.scene, args.characters, models,
                      run_root=run_root, bid_path=args.bid,
                      api_key=api_key, closing_at_block=args.closing_at_block,
                      demo_alternate=True, start_time=args.start_time)
    try:
        # 1) 开场指令：--prompt > stdin 提示行（空/EOF/"" → 默认开场）。
        opening = args.prompt or None
        if opening is None:
            opening = await _to_thread_read("开场指令（直接回车用默认）> ")
            opening = opening or None
        # 2) 开场即打印导演开场。
        await eng.open_scene(opening)
        seen = await _emit_new(eng, 0)

        # 3) 循环：每步引擎产一整块，新消息立即逐句打印，随后按 --pace 续演。
        step_no = 0
        quit_early = False
        while not eng.closed and step_no < args.max_steps:
            step_no += 1
            await eng.step(1)
            seen = await _emit_new(eng, seen)
            if eng.closed:
                break
            if args.pace == "auto":
                await asyncio.sleep(0.6)
                continue
            line = await _to_thread_read("回车=下一句 / q=提前结束 > ")
            low = line.strip().lower()
            if low in _QUIT_WORDS:
                quit_early = True
                await eng.close_scene()
                break
            if line.strip():                # 非空 = 导演导播，下一块角色会回应
                await eng.inject(line)
                continue
            await asyncio.sleep(0.6)        # 空回车 / 管道 EOF → 自动续演（防空转）

        # 5) 收尾：自然收束 / q 提前退出都给出明确结论；转录须在 aclose 前写
        # （aclose 之后再用引擎会重开一条无人回收的 sqlite 连接）。
        print(f"（场景结束：{'已收束' if not quit_early else '提前退出'}）")
        await _export_if_requested(args, eng)
    finally:
        await eng.aclose()


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    api_key = os.environ.get("DEEPSEEK_API_KEY")

    # 缺省即内置演示素材，省去长命令；显式传参仍覆盖。
    args.scene = args.scene or _DEFAULT_SCENE
    args.characters = args.characters or _DEFAULT_CHARACTERS
    args.models = args.models or _DEFAULT_MODELS
    if args.demo and args.bid is None:
        args.bid = _DEFAULT_BID_DEMO   # demo 默认用放开的竞价，避免全员静默

    if args.demo:
        live = not args.stub
        if live and not api_key:
            # 缺 key：清晰提示后回退离线 stub，绝不崩溃（与 --live 的硬报错不同）。
            print("未检测到 DEEPSEEK_API_KEY：--demo 回退离线 stub 演示。", file=sys.stderr)
            live = False
        models = args.models if not live else args.models.with_name("models.live.yaml")
        if live:
            print("[demo] 模型后端：DeepSeek 真实模型（think/speak 均 deepseek-v4-flash，"
                  "speak 关思考）")
        else:
            print("[demo] 模型后端：离线 stub —— 看到占位台词说明没读到 "
                  "DEEPSEEK_API_KEY（见 start-demo.bat 提示）")
        asyncio.run(_demo_async_main(args, models, api_key))
        return

    # ---- 遗留路径（无 --demo）：行为不变 ----
    models = args.models if not args.live else args.models.with_name("models.live.yaml")
    if args.live and not api_key:
        raise SystemExit("--live 需要 DEEPSEEK_API_KEY 环境变量")
    asyncio.run(_legacy_async_main(args, models, api_key))


if __name__ == "__main__":
    main()
