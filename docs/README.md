# 文档索引

本目录收录这套多角色会话引擎（多智能体角色扮演 harness）的设计与实现文档。代码在 `app/`：引擎 `app/src/harness/`、桌面界面 `app/src/harness/gui/`、示例素材 `app/characters/` 与 `app/scenes/`；配置在 `app/config/`（`models.yaml` 档位映射、`bid.yaml` 竞价参数）。

**阅读顺序**：`design.md` → `technical-scheme.md` → `ui-and-scene-freedom.md` → `information-library.md` → `relations-and-scene-pacing.md`；要给最终用户做安装包，另读 `packaging.md`；`implementation-plan.md` 是开发过程记录，可最后读或按需查阅。前五篇描述当前口径。

**命名约定**：文档与代码注释中的「设计文档」指 `docs/design.md`，「技术方案」指 `docs/technical-scheme.md`。

---

## 1. `design.md` — Design Document: semantics and rationale

- **是什么**：设计基准（语义来源）。三循环（感知/权衡/行动）+ 世界规则层、块级竞价调度、think/speak 两步、信息边界=数据访问控制、消息与角色卡与场景 schema、私有记忆与印象、决策日志、开放问题。
- **对应代码**：`schemas.py`、`dynamics.py`、`bidding.py`、`visibility.py`、`memory.py`、`prompters.py`、`graph.py`、`engine.py`。
- **谁读**：想理解「为什么这样设计」的人；改动调度语义之前必读。

## 2. `technical-scheme.md` — Technical Scheme: modules, interfaces, version locks

- **是什么**：落地技术方案。三循环到 LangGraph 图的映射、模块划分与接口、伪代码骨架、两道信息边界的实现方式、引擎到界面的事件契约、MVP 里程碑与验收标准、测试策略、风险与对策，以及 **§16 版本锁定**（LangGraph 1.x、DeepSeek 现役 model id、思考开关、限流与定价的交叉核实结论）。
- **对应代码**：`app/pyproject.toml`、`app/config/models.yaml` 与 `models.live.yaml`、`app/config/bid.yaml`、`backends/`、`schemas.py`、`bidding.py`、`graph.py`、`events.py`、`runner.py`。
- **谁读**：要动手实现、评审技术选型，或核对模型接入与版本口径的人。

## 3. `ui-and-scene-freedom.md` — UI and Scene Freedom: requirements and landing plan

- **是什么**：第三、四批优化需求与落地方案。场景持有阵容与按需装卡、空场可运行、界面结构（去启动小窗、只保留场景库、配置场景、导入）、场景变更进日志、视觉语言（科技感、简约、无 AI 味），以及撤回回溯整段下文与自动推进频率可调（含根因定位与判据缩放口径）。
- **对应代码**：`engine.py`、`scenarist.py`、`hooks.py`、`scenestore.py`、`template_import.py`、`gui/`（`main_window.py`、`library.py`、`settings.py`、`theme.py`、`worker.py`）。
- **谁读**：要改界面、场景自由度、场景推进节奏或运行态存档行为的人。

## 4. `implementation-plan.md` — Implementation Plan (M1–M4): task-by-task record

- **是什么**：开发过程记录（已完成）。14 个任务、复选框式 TDD 步骤：工程骨架 → `schemas`/`loaders`/`visibility`/`bidding`/`memory` → 模型后端（stub + DeepSeek）与提示词 → `events` 与 LangGraph 图（M2 固定轮流 / M3 竞价仲裁 / M4 world 收束 + `SceneEngine`）→ 示例素材与 CLI → live 契约测试与成本探针。
- **对应代码**：`app/src/harness/` 下的同名模块与 `tests/`、`app/pyproject.toml`。
- **谁读**：想按同样路径复刻实现、或想知道某个模块是怎么一步步长出来的人。**它保留的是当时的实现口径；凡与当前行为不符处，以本索引与 `design.md` 为准**（文首已列出主要变更）。

---

文档中的角色示例为公有领域的演示素材（贝克街221B / 福尔摩斯 / 华生），与任何具体作品的设定无关。实现记录中出现的角色名一律使用中性占位 `甲/乙/丙/丁`。

## 5. `packaging.md` — Packaging & Distribution Plan: one-file installers

- **是什么**：打包与分发方案（**尚未实施**）。前置改造（冻结态资源路径 `paths.py`、用户数据目录 `%APPDATA%\Ensemble-AI-Studio`、首次运行播种）、PyInstaller 两种产物、Inno Setup 一键安装包、一条命令构建、发版流程与风险对策，末尾附可打勾的实施清单。
- **对应代码**：拟新增 `app/src/harness/paths.py`、`packaging/`（`ensemble.spec`、`entry_gui.py`、`installer.iss`、`build.ps1`）。
- **谁读**：想给最终用户一个「下载即装」的安装包，或要给项目接 CI 自动出包的人。

## 6. `information-library.md` — Information Library: a character's knowledge, as a graph

- **是什么**：角色**跨场景持久**的知识库。条目（键 / 标题 / 一行摘要 / 正文）正文之间以双链互链成**图**；**索引表**恒在提示词里当入口，正文由角色在 think 阶段**用工具调用**按需取用、可顺链多跳。含：场景副本、散场结算（保留 / 丢弃）、冲突自动新压旧且旧条归档、**订阅**（活引用，可单条固化）、`knowledge_boundary` 退役迁移、Markdown 模板导入、信息库编辑器。
- **对应代码**：`knowledge.py`、`knowledgestore.py`、`knowledgetools.py`、`knowledgesettle.py`、`backends/`（工具调用）、`graph.py`（think 工具循环）、`gui/knowledge_editor.py`、`gui/settlement_dialog.py`。
- **谁读**：要理解"角色知道什么"这一子系统、或要改工具循环 / 结算语义的人。**§13 接线地图**是实现者必读。

## 7. `relations-and-scene-pacing.md` — Relationships & Scene Pacing

- **是什么**：第五批需求。**流式开口**（接收侧全量缓冲、显示侧匀速吐字）、**场景诊断收进日志**、**场景跳时间**（四道闸：前件 / 幅度 / 冷却 / 频次，前件闸是引擎侧确定性判据）、**进离场讲原因**（原因只发当事人，倾向并入既有 bid）、**人际关系系统**（一人一行的关系表，恒在上下文；`update_relation` 三重频率约束；亲密度进 dynamics；与信息库键链接）。
- **对应代码**：`relations.py`、`dynamics.py`、`scenarist.py`、`engine.py`、`gui/relations_editor.py`、`gui/main_window.py`（吐字）。
- **谁读**：要改界面呈现节奏、场景推进、或人物关系的人。**§2.4 的节奏设计**（为什么是"全量收完再匀速吐"而不是跟着网络分片走）值得先读。
