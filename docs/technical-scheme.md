# 多智能体角色扮演系统 — 技术实现方案

> **摘要**：本文是 `docs/design.md`（下称「设计文档」；代码注释中同样使用这个简称）的**落地技术方案**：把设计文档中的三循环、块级竞价、think/speak 两步、硬信息边界等抽象，翻译成**基于 LangGraph 的 Python 实现蓝图**，含模块划分、接口、伪代码骨架、MVP 里程碑、风险清单与**版本锁定**（§16 给出 LangGraph 1.x 与 DeepSeek 模型现役 id、限流与定价的交叉核实结论）。先评审，再据此生成实现计划。
>
> **对应代码**：`app/pyproject.toml`（依赖与版本锁）、`app/config/models.yaml` / `models.live.yaml`（role→档位映射）、`app/config/bid.yaml`（竞价参数）、`app/src/harness/schemas.py`、`visibility.py`、`bidding.py`、`backends/`、`graph.py`、`engine.py`、`events.py`、`runner.py`。
>
> **引用约定**：不带前缀的 `§x` 一律指本文自身章节；凡指向设计文档（`docs/design.md`）的引用，均写作 `设计文档 §x`，以免两文档同号冲突——代码里的 `技术方案 §x` 则一律指本文。
>
> **读者**：要动手实现、评审技术选型或核对版本/模型接入口径的人读本文。设计意图见 `docs/design.md`；开发过程记录见 `docs/implementation-plan.md`；界面与场景自由度见 `docs/ui-and-scene-freedom.md`。

---

## 1. 已收敛的技术决策

| 决策点 | 结论 | 备注 |
|---|---|---|
| 交付 | 技术实现方案文档 → 评审 → 实现计划 | 本文即方案文档 |
| 实现语言 | Python | 与参考生态（Generative Agents / LangGraph）一致 |
| 基础框架 | **LangGraph**（状态图/Store/Checkpointer/并行扇出） | 设计文档决策日志定位它为参考；此处采纳为地基，但见 §4 隔离策略 |
| 呈现形态 | 核心引擎**无头化**：只输出「事件流」，不感知界面 | 产品将有两个呈现面：**TUI 终端界面** 与 **Web 交互界面**（均后续路线，见 §9.1） |
| UI 本轮范围 | **后置**，但首版即预留引擎→界面事件契约与 human 操作集 | 界面只是事件的消费者，替换/新增界面不改引擎 |
| 调度模型 | 自研块级竞价仲裁（`bidding.py`），**不借框架 select_speaker** | 框架无此原语，借会退化成中央选角 |
| 信息边界 | 数据访问控制：`visibility.py` 纯函数可见性投影 + 共享通道/私有状态分层 | 框架外实现，保证硬约束 |
| 模型接入 | 可插拔 backends 层；**think/speak 双档**由 `models.yaml` 映射 | 默认 DeepSeek，可换厂商不改代码 |
| LLM 调用 | think=轻量调用+锁 JSON；speak=强档出正文 | 守住 设计文档 §8.3「想一下不写成小作文」 |
| 记忆 | 角色长期记忆=独立文件系统文件夹；短期转录=LangGraph Store | MVP 先「追加+软上限」 |

---

## 2. 总体架构：三循环 → LangGraph 图

把设计文档的「感知环 / 权衡环 / 行动环 / 世界规则层」实现为一个**逐块推进的循环图**，每次图执行推进「一个块」。

```
                         ┌──────────────────────────────────────────┐
 场景装载/开场(导演) ───▶ │  每轮 = 一个块：                          │
                         │                                          │
                         │  perceive  ···· 对每个在场听众并行 think   │
                         │    （LangGraph Send 扇出，并发上限≤4）     │
                         │          │ 收集 think-JSON（私有态入角色   │
                         │          │  私有命名空间；urge 入共享态）  │
                         │          ▼                               │
                         │  deliberate ·· 竞价仲裁（自研 bidding）    │
                         │          │ 在位者优势/打断阈值/开口阈值/    │
                         │          │ 沉默计数                        │
                         │          ▼                               │
                         │  act  ······ 胜者 speak 节点生成一个块      │
                         │          │                               │
                         │  world ···· 硬边界检查/事件注入/收束判定     │
                         │          │（连续 K 块静默→注入；边界到→收束）│
                         └──────────┼───────────────────────────────┘
                                    ▼
                          场景收束：导出转录 + 记忆固化
```

**与 LangGraph 的对应关系：**

| 设计文档概念 | 实现载体 |
|---|---|
| 感知环（每个听众解释上一句） | 图内一个 `think` 节点，用 **Send API 并行扇出**到每个在场听众；每份输入 = 该听众**独立可见视图** |
| 权衡环（算 urge） | think 节点内完成（输出锁死 JSON，含 `urge`），`urge` 是唯一进入共享态的内心数 |
| 行动环（说出一个块） | `speak` 节点：强档模型生成一个块（一句话/一个言语动作） |
| 世界规则层 | `world` 节点：块后执行（边界逼近→注入/收束）、（静默→注入） |
| 导演（仅开场/注入/收束） | `runner.py` 的图外编排 API：`open_scene / inject_event / close_scene` |
| 会话转录（带 knows/in_scene） | LangGraph **Store/Checkpointer**，供可见性投影查询 |
| 角色长期记忆/印象 | 文件系统记忆文件夹（§7），think 步为印象写入点 |

**节点类型约定**：图内只有三种节点类型 —— `think`（每听众一份实例）、`speak`、`world`；`deliberate` 不是 LLM 节点而是**图内决定下一跳路由的纯代码**（在哪算、在哪做让位判断见 §5.2）。

---

## 3. 一次「块」的数据流

以图视角，一个块推进经过：

```
1. world 判定本块是否还有意义（边界未到、未收束）；是则进入——
2. think 扇出：前置扇出节点为每个在场听众把「上一块（若有）+ 其可见转录/印象」拼成**独立视图**装进各自 Send payload
   → 每听众一次轻量 LLM 调用，输出结构化 think-JSON（worker 只见自己 payload，不见他人）
   → 私有字段写各自 Store 命名空间；urge 经 dict 合并 reducer 收集到本轮的共享暂存
3. deliberate（纯代码，无 LLM）：
   a. 静默判定：max(urge) < 开口阈值  → 场子静默，silence++；
      连续 K 块静默 → 交给 world 注入或收束，本块无输出
   b. 若上一块有在位者：在位者 urge ≥ 次高 + 打断阈值 → 连任
      （他在把话说完）否则发言权交给 urge 最高者（=打断）
4. act：speak 节点用胜者私有视图 + 胜者 state 生成一个块
   （块 = 一句话/言语动作；speaker/address/content 定型）
   → 消息落转录 Store（携带 knows 可见性集）
5. 回到 1；场景收束时 runner 导出转录并固化记忆
```

**注意**：think 的对象是「在场上一条块的**每个听众**」（含刚说话的下一句准备），不是只有发言者调整——这是 设计文档 §8.2 的硬要求，缺失则无「反应」。

---

## 4. 两条信息边界（核心，Framework 薄弱处，独立实现）

LangGraph 图状态是节点共享的。直接放私有态会破坏「内心状态私有」，故强制分层：

### 4.1 共享通道 vs 私有状态

- **图共享状态只放「公共最小集」**：

| 共享字段 | 内容 |
|---|---|
| `messages` | 转录尾部若干条（已按 knows 收窄的**当前块**等公共事实） |
| `urges` | 本轮每听众的 urge（唯一被允许共享的内心数） |
| `current_speaker` / `speaker_streak` | 在位者及其连任块数 |
| `in_scene` / `circles` | 在场轴与对话圈状态 |
| `silence` | 连续静默块计数 |
| `scene_clock` / 边界临近量 | 世界层公开事实 |

- **角色私有状态永不进图共享态**：情绪强度、目标压力、动机、`obligation` 兑现、`impression_of_speaker` 等，只读写各角色在 Store 中**以角色 key 命名的私有命名空间**。think 节点按 key 隔离读写。
- 由此形成第一道硬边界：**算得出自己的 urge，读不到别人的内心**（urge 聚合时只见标量，不见来源理由）。

### 4.2 可见性投影 `visibility.py`（第二道硬边界）

给任意角色组装输入时，只渲染 `character ∈ msg.knows`（认知轴）且 `character ∈ circle/in_scene`（在场轴）的消息；`reply_to` 仅在**可见子集内**可回溯（防止隔桌或保密消息通过引用链泄露）。

- 纯函数、无 IO、无 LangGraph 依赖 → 可独立单测。
- 这是「信息边界 = 数据访问控制」的落点：不该知道的信息，**组装阶段就不出现**，而非靠 prompt 求它「装不知道」。

**两轴对照**：`in_scene`（在场）决定「谁**能**参与本圈竞价」；`knows`（认知）决定「参与者**能看见**什么」。在场的人不一定知道（如压低声音、被保密），知道的人必在场圈内。二者必须分开投影、分开校验（校验口径见 §11 测试策略）。

---

## 5. 核心模块设计

### 5.1 Schema 层 `schemas.py`（Pydantic）

三张 schema 按设计文档逐字段落地：

```python
class Message(BaseModel):
    id: int
    speaker: str
    speaker_type: Literal["character","director","narrator","human","system"]
    content: str
    in_scene: str                     # 在场轴
    knows: list[str] | None = None    # 认知轴；None=全员可见
    reply_to: int | None = None
    address: str | None = None        # 点名/邻接对
    turn: int

class CharacterCard(BaseModel):
    name: str
    personality: dict
    abilities: list[str]
    relationships: dict[str, str]
    knowledge_boundary: list[str]
    weights: Weights                  # w1_relevance..w7_scene_pressure
    emotion_decay_rate: float

class Scene(BaseModel):
    name: str
    participants: list[str]
    circles: list[Circle]
    hard_boundary: HardBoundary       # type(time/physical/social/goal) + value + desc

class ThinkResult(BaseModel):         # think 步锁死格式（设计文档 §8.3）
    aroused: float
    obligation_fulfilled: list[str]
    goal_progress: float
    addressed: str | None
    impression_of_speaker: str | None
    urge: float
```

### 5.2 竞价仲裁 `bidding.py`（伪代码骨架，落实 设计文档 §15·下一步第 1 条）

```
BIDDING_PARAMS = { interruption_threshold, speak_threshold, silence_K,
                   incumbent_decay, noise_range }

def arbitrate(urges: dict[str,float], incumbent: str|None,
              incumbent_streak: int, silence: int) -> Action:
    """纯函数：输入 → 决策。Action ∈ {incumbent_continues, yield_to(name),
    silence(无人开口), close, inject}"""
    max_urge, next_name = max(urges)          # 次高用于阈值比较
    if incumbent is None:
        if max_urge < speak_threshold:  return silence_tick()   # 静默 +1
        return yield_to(next_name)
    if max_urge < speak_threshold:      return silence_tick()   # 在位者也没话 → 静默
    if urges[incumbent] >= second_highest(urges) + interruption_threshold:
        return incumbent_continues(incumbent_streak + 1)        # 在位者优势：把话说完
    return yield_to(next_name)                                  # 打断 = 丢了重新竞价

def resolve_after_thinks(actions, silence) -> Decision:
    """连续 K 块无人开口 → 世界层兜底"""
    if all(a is silence for a in last_K):  return inject_or_close()
```

> 在位者自身也在每块后 think（他说完没、被激怒没）；「说完了吗」由 `goal_progress`/`obligation_fulfilled` 反映，urge 自然回落让位。沉默压力与场景压力作为**附加项在 think 内并入 urge**（见 §5.3），仲裁只做加法比较，不重新建模。

### 5.3 每听众 think 的内部流程（锁格式关键）

think 提示词把三件事并成**一次轻量调用**：解释上一句 → 更新私有状态 → 按固定 JSON 自标注。附加项并入 urge 的公式按 设计文档 §7.2 落在提示词与私有状态内：

```
urge = w1·relevance(话题) + w2·arousal(当前情绪)
     + w3·adjacency(未兑现的点名/挑衅，含随块衰减)
     + w4·goal_pressure(私有目标未达成压力)
     + w5·talkativeness(性格爱说)
     - w6·inhibition(社会层级/在场约束)
     + w7·scene_pressure(公开场景状态 × 私有目标)   # 公开归世界、私有归角色
     + silence_pressure(∫时间×(1−w5), 性格调制)
     + ε
```

- think 输出**只回共享一个数 `urge`**；其余字段全部留在私有命名空间。
- `impression_of_speaker` 是该块往「我对 TA 的印象」写入的那一笔（设计文档 §9.3：听一句→判断一次→顺手更新，不额外调用）。
- 状态随 `effect(chunk)` 更新后按 `emotion_decay_rate` 衰减，防全员顶格抢话（设计文档 §5 角色卡设计理由）。

### 5.4 图编排 `graph.py`（按 2026 LangGraph 1.x 写法，详见 §16）

- **think 扇出 = Send map-reduce**：前置「扇出节点」为每个在场听众**拼好独立可见视图**并放进 `Send("think", {view, chunk})` payload；被 Send 触发的 think worker **只能看到自己这份 payload**（看不到父图共享态）——天然实现「看不见他人内心」，但也意味着投递内容必须在扇出前置拼好，think 不能再从共享态自取。N 个 worker 并行写同一共享键必须带 reducer（`urges: Annotated[dict[str,float], merge_dict]`；每轮由扇出前置清空）；跑批上限在 **`invoke(config={"max_concurrency": 4})`** 控制，不在图内硬编码。
- **deliberate = 纯代码条件边**（非 LLM 节点）：读 `urges`，用 `add_conditional_edges` 的路由函数返回下一跳节点名字符串即可（静默→`world`，继续→`speak`，收束→`END`）。若还需同时写共享态，返回 `Command(update=..., goto=...)`。
- **act 用单一参数化 speak 节点**，不按角色建 N 个节点（角色列表写死/每次重构图都是反模式）：deliberate 把 `current_speaker` 写进共享态，speak 运行时读它、再按胜者 key 从 Store 取私有上下文拼只给胜者的视图。
- `world` 节点：块后统一出口，负责 `scene_clock` 推进、硬边界逼近→注入事件或收束、静默兜底触发。
- **跨块续跑**：不挂 checkpointer 时每次 invoke 是无状态 run。MVP 每块一轮 `await graph.ainvoke(...)`，**compile(checkpointer=..., store=...) 且全程同一 `thread_id`（=scene_id）**，块间状态才保留、导演可在块间注入。节点只返回要改的 key 的新 dict（偏更新是标准语义），**绝不原地改传入 state**。
- 无头事件流（§9.2）优先用 `astream(stream_mode=["updates"])`，或节点内 `stream_writer` 只发自定义事件而不进共享态（更守隐私），1.1+ 可选 `version="v2"` 强类型流。

### 5.5 记忆 `memory.py` 与转录存取

- 见 §7 记忆系统；接口上对图暴露 `load_private(character) / save_private(character, state)`、`append_impression(character, of, line)`、转录追加/可见性查询。
- **MVP 持久化直接用 Sqlite**（官方定位单进程/本地，正合 MVP，免二次迁移）：`langgraph-checkpoint-sqlite` 提供 `SqliteSaver.from_conn_string("runs/<scene>.sqlite")` 作 checkpointer，`SqliteStore(conn)` 同库作 store（async 用 `AsyncSqliteSaver`）；别用 `MemorySaver`/`InMemoryStore`（内存型，重启即失）。完整转录对象（含 knows/in_scene）适合放 store；checkpointer 只回放共享态。**文件系统记忆文件夹**（§7）与 store 双写职责：文件夹=角色私有态/印象（人读、可调参），store=引擎结构化读写（checkpoint/转录）。多进程/Postgres 后话（见 §12）。

### 5.6 模型接入层 `backends/`

- `base.py`：`async complete(role: ThinkRole|SpeakRole, messages, schema=None) -> 文本|JSON`
- 适配器：`deepseek.py`（OpenAI 兼容端点）、`stub.py`（确定性假 LLM，供离线测试竞价与边界逻辑）
- `models.yaml` 把 role → (backend, model, temperature, max_tokens) 映射；换档只改配置。
- think 的结构化输出：Pydantic schema + provider `json_object` 模式 + 失败轻量重试一次，防「小作文」。

---

## 6. 模型接入层默认档位

### 6.1 role → 档位映射（默认 DeepSeek，`config/models.yaml`）

> 档位现状于 2026-09 核实，详见 §16。**旧名 `deepseek-chat` / `deepseek-reasoner` 已于 2026-07 停用**，现役仅 `deepseek-v4-flash` / `deepseek-v4-pro` / `v4-flash-vision-exp`（实验），思考与否用请求体切换而非换 id。

| role | 用途 | 默认档（model id） | 关键参数 |
|---|---|---|---|
| `think` | 听一句→更新态→urge 自标注 | `deepseek-v4-flash` | **`thinking.type=disabled` 显式关思考**（小 JSON 不需要 CoT）；`response_format={'type':'json_object'}` + Pydantic(ThinkResult)；`max_tokens≈200–400`；可低温/seed 求确定性 |
| `speak` | 生成正文一个块 | `deepseek-v4-pro` | 思考开启（原 reasoner 语义并入此 id），`reasoning_effort` 默认 high、嫌慢可 low/medium；`max_tokens≈512–1024`、正常温度 |

> **关于「轻/强落差」**：DeepSeek 单厂商 pro:flash 输入/输出单价均约 **3:1**（flash 并发 2500、pro 500，远超 think ≤4 扇出），且输入前缀（角色卡+印象+可见转录）命中**上下文缓存≈1/30 价**——单厂商已是低成本可行解。若要"数量级落差"，`models.yaml` 允许 think/speak 指向**不同厂商**适配器，代码零改动：think→`glm-4.7-flash`（免费，支持 JSON，200K ctx）或 `qwen-flash`（须禁思考才配 `json_object`），speak→`deepseek-v4-pro`。备选行的模型名**不要硬编码带日期的快照名**，实施当日以控制台为准（见 §16）。

### 6.2 接入契约要点

- 统一走 `complete()`，speak 也可要求 schema（如结构化导演注、动作注释字段后话）。
- 全部调用 **async**；并发受控（§6.2 扇出上限 ≤4、见 §12）；失败按「重试→退避→单角色降级为静默/默认反应」处理，不让单点超时拖死整场。

---

## 7. 记忆系统

### 7.1 布局（文件系统，可读可改可调试）

```
runs/<scene_id>/<character>/           # 每场运行的独立记忆
├── state.jsonl                        # 私有状态流：情绪/目标/义务/每次 think 追加
├── transcript.visible.<me>.jsonl      # 该角色可见的转录（按 knows/in_scene 投影结果）
└── impressions/
    └── <他人>.md                      # 我对 TA 的印象（Theory of Mind 信念文件）
```

- 每个角色**只能看到自己文件夹**；`state.jsonl`/`impressions/` 是图外文件，不经共享态。
- 角色卡/场景 JSON 也是文件（§9.3 目录结构），即「定义文件 = system prompt + 参数」。

### 7.2 印象文件夹的写入（MVP 策略）

- 写入点：think 步的 `impression_of_speaker` 追加为一行（设计文档 §9.3）。
- MVP 不做自动遗忘/压缩：**先追加 + 软上限**（单印象文件超过 N 行后，仅保留最近 N 行 + 一条压缩摘要，压缩提示词预置但功能列入迭代）。完整遗忘/反思策略见 设计文档 §14.2，列入本文 §13 迭代清单。

### 7.3 转录与可见性的关系

- 完整消息对象（含 `knows`）存 Store；给角色看的一律经 `visibility.py` 投影后再写其 `transcript.visible`（或运行时实时投影，二选一：MVP 用实时投影，避免双份不一致）。

---

## 8. 世界规则层与导演职责

| 边界类型 | 例子（来自设计文档） | 实现 |
|---|---|---|
| 时间 | 餐厅 22:00 打烊 | `scene_clock` 推进；临近→喂 w7 场景压力；到点→强制收束 |
| 物理 | 车来了/雨停了 | 事件注入收束 |
| 社会 | 某人接电话/主人先走 | 事件注入收束 |
| 目标完成 | 谈判成/破裂 | 角色目标达成判定 |

- **边界的双作用**（设计文档 §10.2）：平时背景；临近时给公开场景状态以压力值 → think 并入 w7；到点是闸门。
- **导演降级为场景级**（设计文档 §10.3）：只做 开场 / 注入事件 / 收束，通过 `runner.py` API 调用，不逐句调度。
- 静默兜底：仲裁判定连续 K 块无人开口 → `world` 注入（侍者加水/电话响/换话题）或收束。

---

## 9. 目录结构、CLI 与呈现层预留

### 9.1 呈现形态决策（UI 后置，边界先留）

产品最终有两个呈现面：**TUI 终端界面** 与 **Web 交互界面**，但都不在本轮/首版范围。为杜绝返工，核心引擎从第一天起遵循一条硬规则：

> **引擎是无头的：只向外输出「事件流」，不感知任何界面；界面只是事件的消费者。**

因此架构主链上没有任何 UI 依赖；CLI 也只是「事件流的一个消费者」，并非产品形态。

### 9.2 引擎 → 界面的事件契约（预留）

引擎暴露**有序事件流**（transport-agnostic：MVP 为异步迭代器/内存队列；Web 届时走 WS、TUI 直接订阅同一源）：

| 事件 | 载荷（示意） |
|---|---|
| `block_spoken` | 完整 Message（speaker/address/content/knows，已按可见性定型） |
| `thinks_done` | 本轮各听众 urge 表（**只含标量**，不含内心理由 —— 守 §4.1 边界） |
| `decision` | 连任 / 让位(next) / 静默 / 注入(desc) / 收束(reason) |
| `scene_state` | scene_clock、在场圈、边界临近量 |
| `human_gate` | 可选暂停点：等待 human 类型插入 |

**人机交互操作集（预留进引擎 API，先做）**：`step(N)` 步进、`pause` 暂停、`inject_event` 导演级注入、`speak_as_human` 以 human 身份发言。设计文档消息类型本就含 `human`；这三类操作正是未来 TUI/Web 的最小按键映射集，界面届时只做壳。

### 9.3 目录结构（提案）

```
ensemble/
├── docs/
│   ├── design.md                               # 设计/语义基准
│   ├── technical-scheme.md                     # 本文
│   ├── implementation-plan.md                  # 开发过程记录
│   └── ui-and-scene-freedom.md
├── app/
│   ├── pyproject.toml                          # 依赖：langgraph>=1.2、langgraph-checkpoint-sqlite、pydantic v2、httpx/openai…（Python≥3.10）
│   ├── config/
│   │   ├── models.yaml                         # role→档位映射（think/speak）
│   │   └── bid.yaml                            # interruption/speak 阈值、K、噪声幅度
│   ├── characters/
│   │   ├── 福尔摩斯.json                        # 角色卡（含 w1~w7、衰减率）
│   │   └── 华生.json
│   ├── scenes/
│   │   └── 贝克街221B.json                      # 场景 + 硬边界 + hooks（对话圈已删除）
│   ├── runs/                                   # 运行时产物（自动生成，gitignore）
│   └── src/harness/
│       ├── schemas.py
│       ├── visibility.py                       # 纯函数可见性投影
│       ├── bidding.py                          # 纯函数竞价仲裁
│       ├── memory.py
│       ├── graph.py
│       ├── prompters.py                        # think/speak 提示词模板
│       ├── backends/
│       │   ├── base.py  deepseek.py  stub.py
│       ├── runner.py                           # 最小事件消费者(CLI)：open/inject/close + step + 转录导出
│       └── tests/
│           ├── test_visibility.py  test_bidding.py
│           ├── test_schemas.py  test_memory.py
│           └── test_e2e_stub.py                # 用 stub backend 离线跑竞价与边界
```

### 9.4 CLI：开发调试出口（非产品 UI）

MVP 的 CLI 只做一个**最小事件消费者**（打印事件流 + 导出转录），供开发与调参；它不构成产品形态，未来由 TUI/Web 替换时无架构损失。用法：

```
python -m harness.runner --scene scenes/贝克街221B.json \
    --step N                 # 推进 N 个块（或 1 块/次便于观察）
python -m harness.runner --scene ... --export out/transcript.md
```

---

## 10. MVP 里程碑与验收

| 里程碑 | 内容 | 验收标准 |
|---|---|---|
| **M1 基础层** | schemas + visibility + bidding 纯函数 + memory 读写 | `pytest tests/{visibility,bidding,schemas,memory}` 绿；bidding 覆盖在位者优势/打断/静默/让位四个分支 |
| **M2 双角色说话** | backends(stub+deepseek) + think/speak 节点，先**固定轮流不竞价** | 2 角色对白能离线（stub）跑通；think 输出恒为合法 ThinkResult |
| **M3 竞价接入** | deliberate 路由 + 打断 + 沉默计数 + 开口阈值 | 同一 stub 素材下，竞价结果与手推一致；能出现「被打断让位」与「静默」两个现象 |
| **M4 场景+收束** | world 节点 + 硬边界 + 导演 API + runner CLI | 「贝克街221B·福尔摩斯×华生」首跑能到自然收束并导出转录；时间边界到点强制收束生效 |

**首跑演示素材**：设计文档示例「贝克街221B / 壁炉边·福尔摩斯×华生」即可作为角色卡与场景测试素材；自有素材可后续替换。

---

## 11. 测试策略

- **单测**（纯函数层，不联网）：`visibility.py` 覆盖 knows/in_scene 两轴、reply_to 防跨可见集回溯；`bidding.py` 四个分支 + 参数边界；schemas 反序列化非法输入；memory 追加/上限。
- **契约测试**：`backends` 用 stub 定义「输入→输出」样本，DeepSeek 适配器换入后跑同一套契约，隔离厂商行为差异。
- **集成**：M2/M3 全程用 stub 离线可复现；M4 才引入真实 DeepSeek 试跑。
- **可观测**：每块打印 round 号、urge 表、决策（连任/让位/静默）、是否注入；`--verbose` 输出 think-JSON 以便调参 w1~w7。

---

## 12. 风险与对策

| 风险 | 表现 | 对策（MVP 内） |
|---|---|---|
| 国产 API 限流/慢 | think 扇出被限；服务端排队最久可达 ~10 分钟才接收推理 | 扇出并发 ≤4（远低于账号上限）；**请求 timeout 设 60–120s**（30s 会误杀排队请求）；429/5xx 指数退避重试 2–3 次后**单角色降级为静默/默认反应**不拖场；stub 先覆盖测试 |
| **模型名漂移** | `deepseek-chat`/`deepseek-reasoner` 2026-07 已停用；厂商常改快照名 | model id 全部进 `models.yaml` 不做代码常量；实施日以官方 docs/控制台为准；升级前跑一次 §11 契约测试探明行为 |
| think 写「小作文」 | 锁格式失效、变贵 | Pydantic + `json_object`（prompt 须含英文 'json' 与目标示例）+ max_tokens 上限 + 解析失败重试一次；违反则截断 |
| 私有态泄露 | 印象/情绪被读或写入共享 | §4 分层硬约束；Send payload 只装拼好的视图；memory 仅按角色 key；测试断言共享态不含私有字段 |
| 成本 O(N×M) | 每句每听众一次调用 | think 关思考+小输出+`json_object`；**长输入前缀自动命中上下文缓存≈1/30 价**；注意力门控列入迭代 |
| schema 漂移 | 角色卡/场景与代码不同步 | Pydantic 校验文件即启动时报错，尽早暴露 |
| 框架版本陷阱 | 旧教程把 0.x 当 1.x 写法（`langgraph.constants`、`MessageGraph` 等已弃用） | 全仓锁定 `langgraph>=1.2` + Pydantic V2 + Python≥3.10；只看官方 1.x 文档（§16） |

---

## 13. 后续迭代（**不进首版**）

1. 注意力门控：仅「被点名/戳中/挑衅」听众做全量 think，其余默认衰减（设计文档 §11）。
2. 印象遗忘与定期反思（对标 Generative Agents reflection）。
3. 话题管理（导演 or 涌现）。
4. think 档模型选型与输出锁小到什么程度的实证。
5. 沉默开口阈值与打断阈值的调参（与 w1~w7 一起做首批校准实验）。
6. 块单位从「字数近似」到「一次完整 speech act」的检测。
7. 多进程/持久化 Store（Postgres/磁盘），多人协作者与实时流式体验。
8. 呈现壳：**TUI**（Textual）与 **Web**（FastAPI/WS + SPA）——只消费 §9.2 事件流，不触引擎。
9. 事件流持久化与回放（把每场渲染成可重看的"播放"）。

---

## 14. 参考

- 设计文档 §13 所列（Generative Agents / AutoGen·LangGraph / Turn-taking 语言学 / Anthropic API）——其中 LangGraph 与国产模型接入现状见本文 §16 核实结论。

---

## 15. 待办 / 开放项

- [ ] w1~w7、打断阈值、开口阈值的初始值校准实验（M4 后第一批）。
- [ ] 印象文件遗忘策略参数（N 行上限、摘要触发频率）。
- [ ] `models.yaml` 档位实测校准（成本×质量折线）。

---

## 16. 核实结论与版本锁定（2026-09-09，双 agent 网络交叉查证）

### 16.1 LangGraph（1.x）

- **版本**：包名 `langgraph`，当前 1.x（2026-08 至 1.2.11）。要求 **Python≥3.10、Pydantic V2**。锁定 `langgraph>=1.2`；持久化另装 `langgraph-checkpoint-sqlite`。`Send/Interrupt` 改从 `langgraph.types` 导入；`MessageGraph`、`create_react_agent` 已弃用；勿抄 2025 前的 0.x 教程。
- **状态定义/运行**：`TypedDict` + `Annotated` reducer（消息尾部 `add_messages`）；`builder.compile(checkpointer=..., store=...)` 后才能运行；含 async 节点只能 `await ainvoke/astream`。节点只返回要改的 key 的新 dict（偏更新为标准语义，勿原地改传入 state）。
- **Send 扇出**：被 Send 触发的 think worker **只收到 Send arg 作为输入态**（天然隔离，正合 §4.1 隐私）——故每听众独立视图必须由**扇出前置节点**拼好装进 payload，think 不能从共享态自取。并行写同一共享键须带 reducer（`urges` 用 dict 合并 reducer，每轮由扇出前置清空）；并发由 **`invoke(config={"max_concurrency": 4})`** 控制而非图内硬编码；扇出后自动 fan-in 汇聚。
- **私有内存**：LangGraph 无「按 agent 自动隔离」原语 → 用 **`BaseStore`/Store 按 namespace 隔离**，如 `("runs", scene_id, "character", name, "private")`；印象用 `("...", "impressions")` + key=对方名。读写 `store.put/get/search`。`InMemoryStore` 仅内存、重启即失，MVP/生产用 **`SqliteStore`**。
- **Checkpointer/跨块续跑**：checkpointer=thread 内快照，store=长期记忆。MVP 用 `SqliteSaver.from_conn_string("runs/<scene>.sqlite")` + `SqliteStore(conn)`（async 用 `AsyncSqliteSaver`），**每块 `ainvoke` 且全程同一 `thread_id`（=scene_id）** 才跨块保留状态、允许导演块间注入。
- **act 路由**：不要按角色建 N 个 speak 节点。deliberate 作纯代码条件边，路由函数返回节点名，或 `Command(update=..., goto=...)`；单一**参数化 speak 节点**从共享态读 `current_speaker`、再按胜者 key 从 Store 取私有上下文拼视图。
- 若嫌框架重：本方案真正所需仅是「状态机 + 并行扇出 + checkpoint/store」，可手写 asyncio+sqlite 替代；**维持 LangGraph**（时间旅行/回放与生态可复用）。

### 16.2 DeepSeek / 国产模型接入（2026-09-09 现状）

- **现役 model id**：`deepseek-v4-flash`（V4-Flash-0731）、`deepseek-v4-pro`（V4-Pro-0813）、`deepseek-v4-flash-vision-exp`（实验）。1M 上下文、最大输出 384K。**`deepseek-chat`/`deepseek-reasoner` 已于 2026-07-24 停用**，沿用旧名会报错。
- **思考切换在请求体**：OpenAI SDK 用 `extra_body={'thinking':{'type':'enabled/disabled'}}`；`reasoning_effort`（low/high/max）为顶层参数。温度等采样参数仅在非思考模式生效。**think 步显式关思考**（小 JSON 无需 CoT，省 token、享温度/seed 控制）；思考留给 speak。
- **JSON 输出**：三模型均支持 `response_format={'type':'json_object'}`；约束：prompt 须含**英文 'json' 与目标示例**、设合理 max_tokens、偶尔空返回需重试。不支持 OpenAI 式 `json_schema` 类型。→ 正确做法 = json_object 管合法性 + **Pydantic(ThinkResult) 管字段** + 失败重试一次。
- **价格/限流**（2026-08-17 起峰谷计价，元/百万 token，高峰=北京周一五 9–12/14–18，为闲时 2×）：v4-flash 未命中输入 1.5/3.0、输出 4.5/9.0；v4-pro 未命中输入 4.5/9.0、输出 13.5/27.0；**缓存命中输入价≈未命中 1/30**（think 长前缀反复发送可大幅命中）。并发：flash 2500 / pro 500（账号粒度）。**服务端排队可到 ~10 分钟** → 客户端 timeout 设 60–120s（30s 会误杀）、429/5xx 退避重试 2–3 次。
- **档位建议**：think=`deepseek-v4-flash` 关思考 + json_object + max_tokens≈200–400；speak=`deepseek-v4-pro` 思考开启、reasoning_effort 默认 high（嫌慢可 low/medium）。
- **think 更便宜备选**（跨厂商，仅改 models.yaml 即可）：`glm-4.7-flash`（免费、200K、支持 JSON，注意智谱已迭代到 GLM-5.x，实施日复核）；`qwen-flash`（报价约为 v4-flash 的 1/2~1/3，但**须 `enable_thinking=false` 才可配 json_object**）。用**稳定别名**、勿硬编码带日期的快照名，实施日以控制台为准。

### 16.3 主要来源

- langgraph 1.x 迁移与仓库：docs.langchain.com/oss/python/migrate/langgraph-v1；github.com/langchain-ai/langgraph（1.1/1.2 releases）
- Send 并行与状态传播：forum.langchain.com「Best practices for parallel nodes fanouts」；「Propagate parent state … Send API」
- Store/持久化：docs.langchain.com/oss/python/langgraph/stores；langgraph-checkpoint-sqlite README
- DeepSeek 官方：api-docs.deepseek.com（quick_start/pricing、updates、guides/json_mode、guides/thinking_mode、rate_limit）
- 智谱/通义：docs.bigmodel.cn（glm-4.7-flash）；help.aliyun.com/zh/model-studio（qwen-flash 定价与约束）
