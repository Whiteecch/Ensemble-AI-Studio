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
from typing import Any, Sequence

from . import knowledgestore as store
from . import paths as paths_mod
from .engine import SceneEngine
from .loaders import load_character_card

_QUIT_WORDS = {"q", "quit", "exit", "结束", "退出"}

#: 非交互场合的结算默认策略（§7.4：没有用户的场合必须**显式**选一个，并在输出里讲清楚）。
#: 缺省 `leave` = 什么都不做、留着待结算：这是唯一**不可逆性为零**的选择——"保留"会把
#: 角色这一场记下的东西永久并进本体，"丢弃"会把它标成永不合并（§7.5 先到先得），
#: 两个方向都不该由"这里没有用户"替用户决定。
_SETTLE_DEFAULT = "leave"
_SETTLE_POLICIES = {
    "leave": "不结算、留着待你回来处理（什么都没并进本体库，也没被丢掉）",
    "keep": "保留（本场所得已并进本体库）",
    "discard": "丢弃（什么都没并进本体库，副本与这一场的存档都留着）",
}
_SETTLE_POLICY_CHOICES = tuple(_SETTLE_POLICIES)

# 内置演示素材缺省值：不传 --scene/--characters/--models 也能一条指令跑 demo。
# **不再相对 cwd**（打包方案 §1.2）：相对 cwd 的默认值在"装到 Program Files 后从快捷方式
# 启动"这类场景下必然找不到素材（cwd 可能是任何地方）。缺省一律问 `paths.resource_dir()`
# ——开发态它正是仓库里的 `app/`，故从 `app/` 里跑 CLI 时解析结果与改造前**同一个文件**。
_DEFAULT_SCENE = paths_mod.resource_dir() / "scenes" / "贝克街221B.json"
_DEFAULT_CHARACTERS = [paths_mod.resource_dir() / "characters" / "福尔摩斯.json",
                       paths_mod.resource_dir() / "characters" / "华生.json"]
_DEFAULT_MODELS = paths_mod.resource_dir() / "config" / "models.yaml"
_DEFAULT_BID_DEMO = paths_mod.resource_dir() / "config" / "bid.demo.yaml"

#: `--libraries-root` 的「关」写法（§13.3）：显式写这几个之一等于**不启用信息库**，
#: 与今天逐字节一致（一个字都不多打印）。空串也认——它是命令行上最自然的那句"不要"。
_LIBRARIES_OFF_WORDS = frozenset({"", "none", "off", "no", "false", "-", "关", "无"})


def _default_libraries_root() -> Path:
    """信息库根的缺省落点：`paths.materials_dir()/libraries`。

    与 `scenes/`、`characters/` 同一层、同一个口径（`materials_dir()` 是"读写必须同址"
    的枢纽）：**开发态 = 仓库里的 `app/libraries`**（既有数据原地不动，逐字节同址），
    **冻结态 = 用户数据目录下的 `libraries/`**。

    为什么必须跟过去：信息库根是**写**路径——首次播种、每场的副本、散场结算都会往里写。
    留成"仓库/安装目录"的话，打包后装到 Program Files 下这些写操作会直接失败（onefile
    更糟：写进退出即删的解压目录，重启就没）。
    """
    return paths_mod.materials_dir() / "libraries"


#: 缺省信息库根（隔离夹具会把它指到临时目录，故测试里不要把"缺省到底是哪儿"钉在这条常量上，
#: 要钉就钉 `_default_libraries_root()` 的推导）。
_DEFAULT_LIBRARIES_ROOT = _default_libraries_root()


def _resolve_libraries_root(value: str | Path | None) -> Path | None:
    """`--libraries-root` 的取值 → 路径或 None（None = 这一场不启用信息库）。**纯函数。**

    规则（都写进了 `--help`）：
      · 不传（None）→ **缺省 `app/libraries`**：信息库检索是常态，缺省开（§10.2/§13.3）；
      · 传路径 → 就用它（显式传参覆盖缺省）；
      · 传 `none`/`off`/`-`/空串等「关」的写法 → None，退回今天的行为（逐字节一致）。
    """
    if value is None:
        return _DEFAULT_LIBRARIES_ROOT
    if isinstance(value, str) and value.strip().casefold() in _LIBRARIES_OFF_WORDS:
        return None
    return Path(value)


def _seed_card_libraries(paths: Sequence[Path | str] | None,
                         root: Path | None) -> list[Any]:
    """把这一场装载的卡上的 `knowledge_seed` 播进各自的角色库（§9.1 第二步）。**绝不抛。**

    与 GUI 侧（`gui/worker._seed_card_libraries`）同一件事、同一套容错：只在库**还不存在**
    时写入（幂等由 `seed_from_card` 保证）；没有信息库根就什么都不做；读卡失败只记日志并
    跳过——播种是旁挂动作，绝不该拦住开场（引擎随后还会按需再读一次卡，报错更准确）。

    `SeedResult.warnings`（哪些行被当作重复丢掉、哪些键被改造过）一律**打印到 stderr**：
    这些是"用户写的东西被动了"的实情，静默丢弃正是 §4.3 明令禁止的那件事。走 stderr 而非
    stdout——stdout 是要与"没有信息库"逐字节对拍的输出面（§13.3）。
    """
    if root is None:
        return []
    results: list[Any] = []
    for path in paths or ():
        try:
            card = load_character_card(Path(path))
        except Exception:                   # 坏卡/缺文件不拦开场，也不吭声
            continue                        # （引擎随后会再读一次，报出更准确的错）
        name = getattr(card, "name", "?")
        try:
            res = store.seed_character_library(card, root=Path(root))
        except Exception as exc:            # 磁盘满/权限：旁挂动作只记一行到 stderr
            print(f"[播种] 「{name}」的信息库没能建起来（这一场照常）：{exc}",
                  file=sys.stderr)
            continue
        for line in getattr(res, "warnings", ()) or ():
            print(f"[播种] 「{name}」：{line}", file=sys.stderr)
        results.append(res)
    return results


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="多智能体角色扮演 harness")
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
    ap.add_argument("--libraries-root", type=str, default=None,
                    help="信息库根目录；**缺省 = 仓库/安装目录下的 app/libraries**"
                         "（信息库检索是常态）。显式传 `none` / `off` / `-` / 空串 "
                         "= 这一场不启用信息库：整场行为与没有信息库时逐字节一致，"
                         "也不会打印任何结算相关的东西（§13.3 的老调用方零语感差异）")
    ap.add_argument("--settle-default", choices=list(_SETTLE_POLICY_CHOICES),
                    default=_SETTLE_DEFAULT,
                    help="没有可交互输入的场合（管道 / --stub 自动跑 / 没有 stdin）散场结算按"
                         f"什么策略处理；缺省 {_SETTLE_DEFAULT}={_SETTLE_POLICIES[_SETTLE_DEFAULT]}")
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


# ------------------------------------------------------- 散场结算（§7.2/§7.4）--

def _stdin_interactive() -> bool:
    """有没有可交互的输入（stdin 是 TTY）。

    只管"能不能问"，不管"该不该问"：管道重定向、pytest 的假 stdin、没有 stdin 一律 False
    ——那时绝不 `input()`（一读就卡死或读到 EOF），改走显式默认策略（`--settle-default`）。
    """
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except Exception:                  # 假 stdin（测试替身）连 isatty 都没有
        return False


def _ask_settlement(prompt: str) -> str | None:
    """问一次「保留 / 丢弃」；**EOF / Ctrl-C / 没有可交互输入 → None**（= 不结算、留着）。

    绝不崩：用户中途走了（关掉终端、Ctrl-C）不该把刚看完的一场戏的收尾整段丢掉，
    也不该替他做一个不可逆的选择——没答的都留在待结算（§7.3）。
    """
    if not _stdin_interactive():
        return None
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        return None


def _parse_decision(answer: str) -> str:
    """用户的回答 → `"keep"` / `"discard"` / `"leave"`（认不出来一律 leave）。

    宁可留着也不猜：并进本体与标记丢弃都是不可逆的（§7.5 先到先得），一个手滑的乱输
    不该替用户做掉这个决定。空行（直接回车）正是"留着"这个选项本身。
    """
    text = str(answer or "").strip().lower()
    if text in ("k", "keep", "保留", "1"):
        return "keep"
    if text in ("d", "discard", "丢弃", "2"):
        return "discard"
    return "leave"


def _print_settlement_list(engine: SceneEngine) -> None:
    """把待决清单打出来（§7.2："看得到才决定得了"）：谁、哪一场、新增几条、修订几条、标题。"""
    print("\n[结算] 这一场结束了。这些人的本场所得等你决定：", flush=True)
    for name, gain in engine.settlement_gains().items():
        print(f"  · {name}（{gain.scene or engine.scene.name}）："
              f"新增 {len(gain.added)} 条、修订 {len(gain.revised)} 条", flush=True)
        for entry in [*gain.added, *gain.revised]:
            print(f"      - {entry.title or entry.key}", flush=True)
    for warning in engine.settlement_warnings():
        print(f"  （警告）{warning}", flush=True)


def _collect_decisions(args: argparse.Namespace, names: list[str]) -> dict[str, str]:
    """拿到每个角色的决定（§7.4）：交互则逐个问，非交互则走显式默认策略并**讲清楚**。"""
    if not _stdin_interactive():
        policy = str(getattr(args, "settle_default", None) or _SETTLE_DEFAULT)
        print(f"[结算] 没有可交互的输入（管道 / 自动跑 / 没有 stdin）：按默认策略"
              f"「{policy}」处理——{_SETTLE_POLICIES.get(policy, '不结算、留着')}。",
              flush=True)
        if policy not in ("keep", "discard"):     # 认不出来（缺参/坏值）→ 当"留着"
            return {}
        return {name: policy for name in names}
    decisions: dict[str, str] = {}
    for name in names:
        answer = _ask_settlement(f"{name}：保留 / 丢弃（回车=留着，稍后再结）> ")
        if answer is None:
            print("[结算] 输入结束（EOF / 中断）：没答的都没结算，留着待你回来处理。",
                  flush=True)
            break
        choice = _parse_decision(answer)
        if choice == "leave":
            print(f"  {name}：留着，不结算。", flush=True)
            continue
        decisions[name] = choice
    return decisions


def _print_reports(reports: dict) -> None:
    """把每个角色**实际发生了什么**打出来（§7.4：不静默）；警告逐条上屏。"""
    for name, report in reports.items():
        if report.outcome == "keep":
            merged = report.merged
            print(f"  {name}：保留 —— 并入本体库：新增 {merged.added} 条、"
                  f"修订 {merged.revised} 条、归档 {merged.archived} 条、"
                  f"跳过 {merged.skipped} 条。", flush=True)
        elif report.outcome == "discard":
            print(f"  {name}：丢弃 —— {report.discarded} 条没有并进本体库，"
                  f"副本与这一场的存档都留着。", flush=True)
        else:
            print(f"  {name}：没有结算（留在待结算）。", flush=True)
        if report.repeated:
            print(f"  （{name} 这一场此前已经结算过了，这次没有改动。）", flush=True)
        for warning in report.warnings:
            print(f"  （警告）{name}：{warning}", flush=True)


async def _settle_after_scene(args: argparse.Namespace, engine: SceneEngine) -> None:
    """跑完一场之后的结算回路（§7.4）：引擎只准备，**CLI 才决定**。

    场景还没收束就不结算（这一场还没结束：所得仍待结算，用户随时可以回来处理）。
    没有信息库（`--libraries-root none`/`off`/空串）时 `prepare_settlement` 立刻返回空映射，
    这里**一个字都不打印**——整段行为与今天逐字节相同。
    """
    if not engine.closed:
        return
    pending = await engine.prepare_settlement()
    if not pending:
        return                                   # 没人有所得：没什么可问的
    # 「有没有要问的」与「要问谁」是**两个**来源，这是有意的（§7.2/§7.4）：
    #   · `pending`（= `pending_settlement()`，按 `is_pending` 过滤）回答"还有谁欠着账"
    #     ——它是"这一场有没有东西要问"的闸门；
    #   · `settlement_gains()` 回答"谁有**真收得上来**的东西"——清单与逐个询问只认它。
    # 两者只在一种情形下分开：账本上记着一笔、那份条目文件却读不出来（`collect_gain` 只
    # 给得出空所得 + 一条读盘警告）。那时**不问他**是故意的——他一条都收不上来，回答
    # 「保留」只会把他标成已结算（§7.5 先到先得），修好文件之后本可并进去的东西就永远
    # 没机会了；引擎会把那条警告挂进 `settlement_warnings()`（下面照旧会打出来），故
    # 用户看得见"这一场少了一条、为什么"，不静默。
    _print_settlement_list(engine)
    decisions = _collect_decisions(args, list(engine.settlement_gains()))
    if not decisions:
        print("[结算] 没有替你结算任何一场：所得仍留在待结算，你随时可以回来处理。",
              flush=True)
        return
    _print_reports(engine.apply_settlement(decisions))


def _default_run_root(args: argparse.Namespace, demo: bool) -> Path:
    """run_root 缺省：**用户数据目录**下的 `runs/`（打包方案 §1.2：不再相对 cwd）。

    存档（转录、角色私有记忆、场景 sqlite）是**写**：装在 `Program Files` 里时 cwd 可能是
    只读的安装目录，相对 cwd 落盘会直接失败。故缺省一律 `paths.runs_dir()`——与 GUI 同一
    个落点（CLI 与 GUI 行为一致），卸载安装包也不影响，卸载默认保留（§三）。

    demo 仍每场唯一（`runs/demo-<uuid>`）：AsyncSqliteSaver 按 run_root 落盘，共享同一
    目录会在多次运行间累积转录（含开场与台词串场）。每场自带目录即天然隔离，无需开场清空、
    绝不动显式 --run-root。
    """
    if args.run_root is not None:
        return Path(args.run_root)
    base = paths_mod.runs_dir()
    if demo:
        import uuid
        return base / f"demo-{uuid.uuid4().hex[:8]}"
    return base


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
                      start_time=args.start_time,
                      libraries_root=args.libraries_root)
    await eng.open_scene()
    for _ in range(args.steps):
        if eng.closed:
            break
        await eng.step(1)
    msgs = await eng.messages()
    print(f"[closed={eng.closed}] 共 {len(msgs)} 条消息")
    await _settle_after_scene(args, eng)         # §7.4：跑完一场才问保留/丢弃
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
                      demo_alternate=True, start_time=args.start_time,
                      libraries_root=args.libraries_root)
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
        await _settle_after_scene(args, eng)     # §7.4：收束之后才问保留/丢弃
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
    # 信息库（§9.1 第二步 + §13.3）：先把 `--libraries-root` 解析成路径或 None（缺省 =
    # app/libraries；`none`/`off`/空串 = 关），再给这一场装载的卡播种——老卡的
    # `knowledge_boundary` 在**这一刻**变成库里的条目（库已存在则整段跳过，幂等）。
    # 两条路（--demo 与遗留 --live）共用这一处，不各播一遍。
    args.libraries_root = _resolve_libraries_root(args.libraries_root)
    _seed_card_libraries(args.characters, args.libraries_root)

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
