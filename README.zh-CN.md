# Ensemble

[English](README.md) | **简体中文**

Ensemble 是一套无头的多智能体角色扮演引擎，外加一个 PySide6 桌面应用。一群各自持有私有状态、私有记忆与性格权重的角色在同一场景里轮流开口，而「场景」本身也是一个独立的 agent——它全知、并且负责把故事往前推。

它和别处不一样的地方：

- **信息边界即数据访问控制。** 角色的提示词由「它有权看到的那部分转录」投影拼成。它根本收不到自己不该知道的文字，而不是被要求「假装不知道」。
- **谁开口是算出来的，不是选出来的。** 每个角色由自身的动态状态与性格权重算出一个数值 bid；被点名欠下的回应义务每沉默一轮就翻一倍。没有导演挑人，也不靠 LLM 自报冲动决定顺序。
- **场景是一等 agent。** 它看得见全部消息，有自己的触发算法（复读 / 停滞 / 临近边界），用一到两行推进剧情，可以调工具改写自己的描述，而且完全可回溯。
- **钩子。** 条件由场景 agent 判定，效果分三类（上下文 / 角色 / 场景），可显式可隐式；时间条件另有一道引擎侧的确定性闸门。
- **可插拔、语料驱动的角色。** Markdown 授权模板交给任意大模型按素材填写，配套解析器与导入器。每名角色的语料（语言风格 / 思维方式 / 口头禅 / 台词样例）决定它的「像不像」。
- **运行期自由选角。** 任何时刻都能加人、移出、禁言；调度与界面随之变化，场景也可以一个人都没有。

## 演示

仓库内置一个场景 `app/scenes/贝克街221B.json` 与它的两张角色卡。取材自柯南·道尔的福尔摩斯与华生，均已进入公有领域。

这个场景做了什么：

- **21:00** 开场，地点是贝克街 221B 的起居室；硬边界 **23:00**（就寝），虚拟钟走到即收束。
- 演员表为福尔摩斯与华生，两人自第 0 轮起在场。
- 钩子 `h1`（上下文，显式）：有人提起一桩未结的案子时，往对话里写一行——福尔摩斯把烟斗从唇边移开，坐直了。
- 钩子 `h2`（角色，显式）：虚拟钟走到 22:30 时，华生离场。
- 场景描述**不可改变**（`description_mutable: false`），所以场景 agent 能推动剧情，但不能改写自己的房间。

跑离线命令行演示：

```bash
cd app
python -m harness.runner --demo --stub
```

`--demo` 逐句实时打印，并强制在场者轮流开口（`demo_alternate`），配合放开的 `config/bid.demo.yaml` 阈值，不接模型也能看到一场对白。每次运行都会新建一个 `runs/demo-<id>/` 目录。回车走下一句，`q` 提前结束。

跑桌面应用：

```bash
cd app
python -m harness.gui.app --stub     # 强制离线；去掉 --stub 即用真实模型
```

应用起手是空状态页。从顶栏「场景」菜单或「打开场景」按钮挑一个场景即可。它在后台线程上驱动同一个引擎，界面里有实时 bid、think 日志、场景推进面板与虚拟钟。

## 快速开始

**前置条件。** Python 3.10 或更新。Windows / macOS / Linux。桌面应用另需 PySide6。

```bash
git clone https://github.com/Whiteecch/Ensemble-AI-Studio.git
cd Ensemble-AI-Studio/app
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -e ".[dev]"     # 引擎 + CLI + 测试
pip install PySide6         # 桌面应用（或：pip install -e ".[dev,gui]"）
```

安装后还会有三个命令进 PATH，各自等价于下文里的 `python -m` 写法：

```bash
ensemble-gui                # 桌面应用      = python -m harness.gui.app
ensemble-demo --stub        # 离线命令行演示 = python -m harness.runner --demo --stub
ensemble-import your.md     # 模板导入      = python -m harness.tools.import_cards your.md
```

**跑桌面应用：**

```bash
python -m harness.gui.app
```

**跑离线命令行演示：**

```bash
python -m harness.runner --demo --stub
```

**跑测试：**

```bash
pytest -q
```

950 个测试，约一分钟。整套测试不需要联网。

**接真实模型。** 引擎默认对接 DeepSeek。Key 有两种给法：

- 环境变量 `DEEPSEEK_API_KEY`；
- 桌面应用里的「设置 → 模型 api 配置…」弹窗（base URL、key、模型名）。填过之后存在用户设置文件里：Windows 是 `%APPDATA%\Ensemble-AI-Studio\settings.json`，其余平台是 `~/.config/Ensemble-AI-Studio/settings.json`。

优先级为 `--stub` > 设置里存过的 key > `DEEPSEEK_API_KEY` > 离线 stub。`app/config/models.live.yaml` 把 think / speak / narrate 三档都指向 `deepseek-v4-flash`。没有 key 时一切回退到离线 stub 后端，绝不崩溃。

另有两个 Windows 双击启动器：`app/start-demo.bat`（命令行演示）与 `app/启动应用.bat`（桌面应用）。环境变量没设时，两者都会去读同目录下的 `.env.deepseek`。

## 工作原理

本项目的发行包名是 `ensemble`，而 import 包名仍是 `harness`（两者刻意分开，见 `app/pyproject.toml`）。

每一个「块」跑三个循环，外加一层世界：

```
                     WORLD（块钟、静默闸门、收束）
                        │
      ┌─────────────────┼──────────────────────────────────┐
      │                 │                                  │
 ┌────▼─────┐     ┌─────▼──────┐     ┌──────────────┐      │
 │  感知    │     │    权衡    │     │     行动     │      │
 │  think   │────▶│    bid     │────▶│    speak     │──────┘  下一块
 │ 逐角色   │     │  纯代码    │     │  只说一句    │
 └──────────┘     └────────────┘     └──────────────┘
   每人一份          数值竞价           胜者开口
   独立视图
      ▲
 ┌────┴─────────────────────────────────────────────┐
 │ 场景 agent（叙述者）：看见全部消息，负责推进、    │
 │ 判定钩子，也可以改写自己的字段                    │
 └──────────────────────────────────────────────────┘
```

**信息边界。** 角色的提示词由 `visibility.view_for(messages, character, space, since_round)` 拼成：只保留 `in_scene` 等于本场景、`knows` 名单放行该角色（或为 `None` = 在场全员可见）、且 `turn` 不早于该角色进场基线的消息。刚进场的角色对之前的对话一无所知。`Message.knows` 是认知轴，`in_scene` 是在场轴，两者都是消息上的字段，而不是提示词里的一句嘱咐。LangGraph 的共享态被锁在一张白名单里（`messages`、`urges`、`current_speaker`、`turn`、`silent_streak`、`blocks`、`decided`、`injected`、`closed`、`closing_at_block`、`retracted`），测试助手会断言共享态里永不出现白名单之外的键。每名角色的 think 结果只走两条路：给人看的只读旁路通道，以及该角色自己的记忆目录 `runs/<场景>/<角色>/`——绝不进共享态。

**谁开口。** 扇出结束后，每名角色的动态状态折进 `Dynamics`，由 `dynamics.Dynamics.bid()` 算出：

```
bid = w1·relevance
    + w2·arousal
    + w3·adjacency
    + w4·goal_pressure
    − w6·inhibition
    + w7·scene_pressure
    + urge_gain · self_urge                             # 本人自报冲动（-1~2）
    + pending_reply                                     # 欠答义务，未答每轮翻倍
    + silence_gain · turns_since_spoke · (0.4 + 0.6·w5) # 爱说性折叠进沉默压力
    − recency_penalty                                   # 上一句正是此人说的
    + stable_noise(round, name)                         # "(轮:名)" 的 CRC32
```

再由 `bidding.arbitrate()` 三选一：在位者连任、挑战者只有超出 `interruption_threshold` 才抢过话筒、或者无人过 `speak_threshold` 则本块静默。其中 `pending_reply` 是承重项：被点名当刻记 1.2，此后每沉默一轮翻一倍（封顶 64），于是被问到的人必然在一两轮内作答，而不会被别人的沉默压力拖过去。它不乘任何性格权重——硬义务不该被性格抵消。全部项都是确定性的：打破等值的噪声是 `(轮, 名)` 的 CRC 哈希而非进程随机的 `hash()`，所以同一场、同一轮永远得到同一个结果。

**场景 agent。** 场景在 `scenarist.py` 里有自己的触发算法，而不是等角色来推动：

```
score = 2.0·repeat + 1.2·stall + 0.5·silence + 1.0·boundary
```

`repeat` 是最近四条角色台词的两两平均文本相似度（`textsim.ratio`），`stall` 是距上次叙述的块数，`silence` 是连续静默块数，`boundary` 随虚拟钟逼近硬边界上升。两次叙述之间还有冷却（缺省 3 块）。推进频率是用户可调项：`effective_params` 统一缩放触发线与冷却，档位同时作为语气提示写进提示词。场景的产出被裁到至多两行 / 120 字——它是推进局势，不写段落。若场景标了 `description_mutable`，它可以吐 `[[TOOL:...]]` 指令改自己的名称、描述或背景；引擎照改，然后把新描述喂回去**只再多思考一轮**。

**可回溯。** `retract(id)` 作废该条及其之后同一场景内的全部消息，并按轮次截断 think 日志与每名角色的私有记忆（`state.jsonl`、`impressions/`）——一切喂给模型的东西都同意「被丢弃的那段从没发生过」。`edit_narration(id, text)` 是「撤回 + 追加」而非原地改字，这样「当时的模型看到过什么」不会失真。世界钟与叙述冷却刻意**不**倒拨。

**钩子**是纯数据（`hooks.Hook`）：一句自然语言条件 + 三类效果之一——`context` 把文字写进转录，`character` 执行 `add` / `remove` / `mute_turns` / `mute` / `unmute`，`scene` 修补场景白名单字段（`name`、`description`、`background`、`plot_direction`、`date`；文件名与路径永远不可写）。钩子分显式（会落一行可见的话）与隐式（只改状态、不落行，仅出现在内部事件流里）。场景 agent 每块逐条判定条件，把成立的那些以 `[[HOOK:<id>]]` 指令行的形式报告出来，引擎剥掉、校验、执行，并在块内去重。由于让 LLM 判钟点被证明不可靠，时间条件另有一道确定性闸门：`scenarist.time_condition_target()` 认出「明确在说虚拟钟」的条件，引擎即便被场景告知成立，也会在钟点未到时拒绝执行。

想深入，见 `docs/`：

- `docs/design.md` —— 设计基准：核心原则、三循环、各模块 schema、竞价模型、信息边界。
- `docs/technical-scheme.md` —— 设计如何落到 LangGraph 实现上。
- `docs/ui-and-scene-freedom.md` —— 运行期选角、推进控制、截断式撤回。
- `docs/implementation-plan.md` —— 逐里程碑的开发过程记录。
- `docs/操作手册.md` —— 面向使用者的操作手册，从安装到跑完一场戏。

这几份文档目前只有中文。

## 授权角色与场景

角色与场景都以 Markdown 授权，交给任意大模型填写，再解析入库。三步：

**第一步：把模板和素材交给大模型。** 复制 `templates/角色卡模板.md`（角色）或 `templates/场景卡模板.md`（场景）的全文，连同你的小说素材一起发给它，并附一句：

> 请严格按这份模板的填写说明，依据下面的小说素材填写。保留所有【】字段名与结构，不要输出 JSON，不要编造素材里没有的内容。

**第二步：检查填写结果。** 字段名是否原样保留、台词样例是否是原句（而非改写）、权重是否是 0~1 的小数、【出场角色】是否都是已有角色卡的姓名。

**第三步：导入。**

```bash
cd app
python -m harness.tools.import_cards ../templates/填好的卡.md    # 自动识别角色卡 / 场景卡
python -m harness.tools.import_cards 某文件.md --dry-run        # 只解析，不写盘
python -m harness.tools.import_cards 某场景.md --name 深夜小馆.json
python -m harness.tools.import_cards 某文件.md --force          # 覆盖同名旧文件
```

导入器逐文件报告：识别出的类型、名字、落盘位置、哪些字段没填而已用默认值，以及解析警告。失败时给的是字段级的说明而不是 traceback——`某文件.md → 第 12 行附近 字段【唤醒权重 w2】：…`。同一套流程在应用里也有：顶栏「场景」/「角色」菜单 →「角色/场景库…」→ 底部「从模板导入…」。

填好的角色卡长这样（下面这段与内置的 `app/characters/华生.json` 一致）：

```markdown
【姓名】：华生
【出处/作品】：柯南·道尔《福尔摩斯探案集》（公有领域）
【一句话定位】：可靠、务实的记录者，替读者问出那个「为什么」
【性格描述】：军旅生涯留下的习惯，东西放回原处，承诺一定兑现。对朋友
  忠诚，但这种忠诚是安静的——不表功，也不追问。被真正冒犯时会立刻硬起来。
【语言风格】：句子完整、语速平稳，先铺垫再落点。克制：即使吃惊，也只用一个
  短句或一次停顿表示。情绪上来时反而更客气。
【思维方式】：先问「这样安全吗」，再问「这样对吗」。以人为先——案子再要紧，
  先看有没有人需要包扎、需要坐下、需要一杯水。
【口头禅】：
- 「我亲爱的朋友——」
- 「如果我没记错的话……」
- 「这一点我得记下来。」
【台词样例】：
- 「我记下了。不过请你说慢一点，我得让它听起来像人话。」
- 「你已经一整天没吃东西了。案子跑不掉，你先坐下。」
【相关权重 w1】：0.6
【邻接权重 w3】：0.7
【抑制权重 w6】：0.75
```

【语言风格】【思维方式】【口头禅】【台词样例】这一组就是角色的 `corpus`。它被渲染进每一条提示词，作为语气与思维方式的 few-shot 锚点。台词样例明确不是可供照抄的内容——提示词会要求模型模仿这个角色**怎么说话**，而不是复述他说过的话。

## 项目结构

```
ensemble/
├── LICENSE
├── README.md
├── README.zh-CN.md
├── .gitignore
├── docs/                              设计、开发与使用文档
│   ├── design.md
│   ├── technical-scheme.md
│   ├── ui-and-scene-freedom.md
│   ├── implementation-plan.md
│   └── 操作手册.md                     面向使用者的操作手册
├── templates/                         Markdown 授权模板
│   ├── 角色卡模板.md
│   ├── 场景卡模板.md
│   ├── 使用说明.md                     三步走授权说明
│   ├── 示例素材.md                     示例素材片段
│   ├── _filled_示例_沈砚.md             填好的角色卡示例
│   └── _filled_示例_小馆.md             填好的场景卡示例
└── app/                               项目根（pyproject.toml 在这里）
    ├── .gitignore
    ├── pyproject.toml                 发行名 "ensemble"，import 包名 "harness"
    ├── start-demo.bat                 Windows 命令行演示启动器
    ├── 启动应用.bat                     Windows 桌面应用启动器
    ├── characters/                    角色卡素材库
    │   ├── 福尔摩斯.json
    │   └── 华生.json
    ├── scenes/                        场景库（运行期另有 *.runtime.json 存档）
    │   └── 贝克街221B.json
    ├── config/
    │   ├── models.yaml                离线 stub 后端
    │   ├── models.live.yaml           DeepSeek 后端
    │   ├── bid.yaml                   竞价阈值
    │   └── bid.demo.yaml              流式演示用的放开阈值
    └── src/harness/                   引擎
        ├── engine.py                  高层引擎：演员表、钩子、场景、存取、撤回
        ├── graph.py                   LangGraph 节点、逐听众扇出、隐私白名单
        ├── dynamics.py                每角色动态状态与数值 bid
        ├── bidding.py                 块级仲裁
        ├── scenarist.py               场景触发算法、工具指令、时间闸门
        ├── hooks.py                   钩子数据模型与纯函数
        ├── visibility.py              信息边界投影
        ├── memory.py                  角色私有记忆
        ├── prompters.py               think / speak / narrate 提示词
        ├── schemas.py                 Scene、CharacterCard、Message、ThinkResult
        ├── loaders.py                 角色卡与场景的读写
        ├── scenestore.py              运行期 sidecar 存档
        ├── sceneclock.py              虚拟钟
        ├── template_import.py         Markdown 模板解析器
        ├── textsim.py                 近重复判定
        ├── i18n.py                    语言目录与 LLM 语言指令
        ├── events.py                  事件契约
        ├── runner.py                  命令行
        ├── backends/
        │   ├── base.py                后端接缝
        │   ├── stub.py                离线后端
        │   └── deepseek.py            DeepSeek 后端
        ├── gui/
        │   ├── app.py                 入口
        │   ├── main_window.py         三栏主窗口
        │   ├── worker.py              承载唯一 asyncio 循环的 QThread
        │   ├── library.py             场景 / 角色编辑器与弹窗
        │   ├── settings.py            用户设置持久化
        │   └── theme.py               配色
        ├── tools/
        │   └── import_cards.py        模板导入器的命令行入口
        ├── scripts/
        │   └── probe_models.py        一次性的模型探测脚本
        └── tests/                     46 个测试模块，950 个测试
```

## 现状

这是一个能用的引擎加一个能用的桌面应用，不是演示骨架。950 个测试约一分钟跑完、不需要联网，覆盖引擎、图、动力学、钩子、可见性、场景 agent、导入器与 Qt 部件。

已知限制（都可在代码里核对）：

- **对话圈已删除。** 在场轴退化为场景本身：一条消息要么属于本场景，要么不属于。带 `circles` 键的旧场景文件由 `schemas.Scene._migrate_legacy` 静默丢弃该键完成迁移。
- **只有中文是完整的。** 界面语言表列了七种（`zh-Hans`、`zh-Hant`、`en`、`fr`、`de`、`ja`、`ko`），但只有 `zh-Hans` 的目录是完整的；其余逐键回落。文档也没有英文版。
- **提示词无上限增长。** 每名角色的视图就是全部未撤销的转录。长会话会越来越慢、越来越贵；没有摘要压缩，也没有滑动窗口。唯一的封顶是给人看的 think 日志（`_THINK_LOG_CAP = 2000`），角色永远看不到它。
- **一个引擎绑定一个事件循环。** 走 SQLite checkpointer 的 `SceneEngine` 绑定它首次构图时所在的事件循环。Qt 应用靠「工作线程内独占一个 asyncio 循环」维持这条纪律；把一个引擎跨两个循环复用是不支持的。
- **有成本护栏，没有预算控制。** 桌面应用在无人值守自动连演 500 块后会自动暂停，等你点「继续」。没有 token 记账，也没有花费上限。
- **只接好了 DeepSeek 一家后端。** `backends/base.py` 是加新后端的接缝，`backends/stub.py` 展示了接口形态，但目前没有随包发布第二个提供方。
- **单进程、单机。** 没有服务端，没有多用户，除模型 API 外没有任何网络。

## 许可

MIT，见 [LICENSE](LICENSE)。

欢迎贡献，issue 与 pull request 皆可。改动行为时请把测试套件当作契约：`pytest -q` 应保持全绿。
