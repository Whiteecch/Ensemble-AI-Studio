# 多智能体角色扮演系统 — M1–M4 实现计划

> **文档性质**：本文是**开发过程记录（已完成）**，保留作为实现路径的参考；**当前行为以 README 与 `docs/design.md` 为准**。正文按当时的实现口径原样保留，凡实施后已有变更的，就地用括号注明，不改动正文叙述：
> - **对话圈（`circles`）已整体删除**：在场轴退化为场景本身，`circles` 键在装载时被静默淘汰（见 `schemas.py`、`visibility.py`）。
> - 场景收束的 `closing_at_block`（把打烊时刻折算成块数）**已被真实流速虚拟钟取代**：边界判定回到真实时刻（`sceneclock.py`），块数上限不再冒充收束。
> - 场景的在场角色改为**带入场记录的演员表**（`Scene.characters` + `entered_round`），且已支持空场运行、随时增删与按需装卡（见 `docs/ui-and-scene-freedom.md`）。
> - 竞价的实际计算已集中到 `dynamics.py`：LLM 每轮只提供状态增量与一次自评冲动，数值 bid 由 harness 算出；`bidding.py` 退为纯仲裁（§Task 5/11 记录的是当时的纯数值口径）。
> - 正文示例中的角色名一律用中性占位 `甲/乙/丙/丁`；随仓库发布的演示素材是公有领域的一组（贝克街221B / 福尔摩斯 / 华生），见 `docs/design.md`。
>
> **摘要**：本文是 M1–M4 的**逐任务实现计划**（14 个任务、复选框式 TDD 步骤）：工程骨架 → `schemas`/`loaders`/`visibility`/`bidding`/`memory` → 模型后端（stub + DeepSeek）与提示词 → `events` 与 LangGraph 图（M2 固定轮流 → M3 竞价仲裁 → M4 world 收束 + `SceneEngine`）→ 示例素材与 CLI，末尾补 live 契约测试与成本/限流探针。每个任务给出文件清单、接口、失败测试、实现与验证步骤。
>
> **对应代码**：逐任务对应 `app/src/harness/` 下的同名模块（`schemas.py`、`loaders.py`、`visibility.py`、`bidding.py`、`memory.py`、`backends/`、`prompters.py`、`events.py`、`graph.py`、`engine.py`、`runner.py`、`tests/`）；依赖与版本锁见 `app/pyproject.toml`，档位见 `app/config/models.yaml`，竞价参数见 `app/config/bid.yaml`。
>
> **读者**：想知道引擎是怎么一步步长出来的、或要按同样路径复刻实现的人读本文。语义基准见 `docs/design.md`；技术选型、模块边界与版本锁定见 `docs/technical-scheme.md`。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 Python + LangGraph 实现设计文档所述的最小可跑多角色会话引擎：两个角色 + 一个场景 + think/speak 两步 + 块级竞价 + 世界规则层，全部先用 stub 后端离线跑通。

**Architecture:** 无头核心引擎按「感知/权衡/行动/世界」映射为 LangGraph 1.x 有向图，逐块推进；角色私有态永不进共享图状态（文件系统记忆文件夹 + LangGraph Store 双写）；图只向事件流吐公共事实。CLI 是最小事件消费者，非产品 UI（UI 后置）。

**Tech Stack:** Python ≥3.10、Pydantic V2、`langgraph>=1.2` + `langgraph-checkpoint-sqlite`、httpx（DeepSeek OpenAI 兼容端点）、PyYAML、pytest、pytest-asyncio。

**Spec:** 两份输入文档随计划同行，执行者必须通读后再动手：
- `docs/design.md`（语义来源，尤其 §7 调度、§8 think/speak、§9 记忆、§10 世界规则）
- `docs/technical-scheme.md`（本计划的直接依据：§3 数据流、§4 信息边界、§5 模块、§9.2 事件契约、§10 里程碑、§16 版本锁定）

## Global Constraints（实现全程适用）

- 目标目录：`d:\开发\ensemble`（含中文名文档，勿动）。新建代码一律在 `app/` 下。
- 版本硬约束：Python≥3.10；Pydantic V2；`langgraph>=1.2`；persistence 另装 `langgraph-checkpoint-sqlite`；依赖全部锁进 `app/pyproject.toml`。
- 模型接入：代码内**不得出现模型 id 常量**，一律走 `config/models.yaml`（role→backend/model/params）。当前 DeepSeek 现役 `deepseek-v4-flash`/`deepseek-v4-pro`；旧名 `deepseek-chat`/`deepseek-reasoner` 已停用。
- think 步结构化输出三件套：`response_format={"type":"json_object"}` + **prompt 必含英文 `json` 与目标示例** + Pydantic 二次校验；失败重试一次。
- DeepSeek HTTP 超时设 60–120s；429/5xx 指数退避 2–3 次。
- **信息边界硬约束**：图共享 state 永不出现私有字段（情绪/目标/动机/印象）；Send think payload 只装拼好的独立可见视图；`urges` 共享标量即可。
- 所有测试默认**离线**（stub backend）；真实 DeepSeek 仅 Demo/手动 live 冒烟，且需 `DEEPSEEK_API_KEY` 环境变量，无 key 则跳过（pytest `skipif`）。
- TDD：每任务先写失败测试→跑红→最小实现→跑绿→提交。提交消息用 `feat:`/`fix:`/`test:` 前缀 + 中文简述。
- 代码标识符用英文；角色卡/场景内容与转录文本用中文。

---

## 文件结构（本计划将产出）

```
app/
├── pyproject.toml                 # 依赖锁定 + pytest/pytest-asyncio 配置
├── .gitignore                     # runs/、.venv/、__pycache__/、.sqlite
├── config/
│   ├── models.yaml                # think/speak 档位映射（可换 stub 供离线）
│   └── bid.yaml                   # interruption/speak 阈值、silence_K
├── characters/
│   ├── 甲.json
│   └── 乙.json
├── scenes/
│   └── 餐厅.json
├── runs/                          # 运行产物（.gitignore）
└── src/harness/
    ├── __init__.py
    ├── schemas.py                 # Message/CharacterCard/Scene/ThinkResult/…
    ├── loaders.py                 # 读 JSON/YAML + Pydantic 校验
    ├── visibility.py              # 纯函数：认知轴×在场轴投影
    ├── bidding.py                 # 纯函数：块级竞价仲裁
    ├── memory.py                  # 角色私有记忆文件夹
    ├── prompters.py               # think/speak 提示词构建
    ├── backends/
    │   ├── __init__.py            # get_backend(name, model_config)
    │   ├── base.py                # ModelBackend 抽象
    │   ├── stub.py                # 确定性脚本 stub（离线测试）
    │   └── deepseek.py            # httpx + DeepSeek OpenAI 兼容端点
    ├── graph.py                   # LangGraph 感知/权衡/行动/世界图
    ├── events.py                  # 事件契约（§9.2 载荷结构）
    ├── runner.py                  # CLI 最小事件消费者：open/inject/step/close/export
    └── tests/
        ├── conftest.py
        ├── test_schemas.py
        ├── test_loaders.py
        ├── test_visibility.py
        ├── test_bidding.py
        ├── test_memory.py
        ├── test_prompters.py
        ├── test_backends.py
        ├── test_graph_m2.py
        ├── test_graph_m3.py
        └── test_e2e_m4.py
```

---

## Task 1: 工程骨架 + git 初始化

**Files:**
- Create: `app/pyproject.toml`
- Create: `app/.gitignore`
- Create: `app/src/harness/__init__.py`、`app/src/harness/backends/__init__.py`、`app/src/harness/tests/__init__.py`
- Create: `app/src/harness/tests/conftest.py`

**Interfaces:**
- Produces: 可 `pip install -e .` 的包 `harness`；pytest 从 `app/` 运行可发现 `src/harness/tests`。

- [ ] **Step 1: git init + 建目录**

在 `d:\开发\ensemble` 下执行：

```bash
cd "d:/开发/ensemble"
git init
mkdir -p app/src/harness/backends app/src/harness/tests app/config app/characters app/scenes app/runs
touch app/src/harness/__init__.py app/src/harness/backends/__init__.py app/src/harness/tests/__init__.py
```

- [ ] **Step 2: 写 pyproject.toml**

`app/pyproject.toml`：

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "harness"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
    "langgraph>=1.2",
    "langgraph-checkpoint-sqlite>=2.0",
    "pydantic>=2.7",
    "httpx>=0.27",
    "PyYAML>=6.0",
]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.23"]

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["src/harness/tests"]
asyncio_mode = "auto"
```

- [ ] **Step 3: 写 .gitignore**

`app/.gitignore`：

```
.venv/
__pycache__/
*.pyc
.pytest_cache/
runs/
*.sqlite
```

- [ ] **Step 4: conftest 提供临时运行根**

`app/src/harness/tests/conftest.py`：

```python
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
```

- [ ] **Step 5: 装包并冒烟**

```bash
cd "d:/开发/ensemble/app"
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"
.venv/Scripts/python -c "import harness, langgraph, pydantic; print('ok', pydantic.VERSION)"
```

Expected: 打印 `ok 2.x`，不报错。

- [ ] **Step 6: Commit**

```bash
cd "d:/开发/ensemble"
git add -A
git commit -m "feat: 初始化 app 工程骨架与依赖锁定"
```

---

## Task 2: schemas.py — 三张 Schema + ThinkResult

**Files:**
- Create: `app/src/harness/schemas.py`
- Test: `app/src/harness/tests/test_schemas.py`

**Interfaces:**
- Produces: `Message`、`Weights`、`CharacterCard`、`Circle`、`HardBoundary`、`Scene`、`ThinkResult`（均为 Pydantic v2 `BaseModel`）；`Message.is_visible_to(character) -> bool`。

- [ ] **Step 1: 写失败测试**

`app/src/harness/tests/test_schemas.py`：

```python
from pydantic import ValidationError
import pytest

from harness.schemas import (
    CharacterCard, HardBoundary, Message, Scene, ThinkResult,
)


def test_message_knows_none_means_visible_to_all():
    m = Message(id=1, speaker="甲", content="你好", in_scene="餐厅")
    assert m.is_visible_to("乙") is True
    assert m.is_visible_to("路人") is True


def test_message_knows_restricts_visibility():
    m = Message(id=2, speaker="甲", content="秘密", in_scene="餐厅",
                knows=["甲", "乙"])
    assert m.is_visible_to("乙") is True
    assert m.is_visible_to("丙") is False


def test_scene_and_character_card_roundtrip():
    card = CharacterCard(
        name="甲", personality={"描述": "冷静"},
        relationships={"乙": "同伴"}, knowledge_boundary=[],
    )
    assert card.weights.w2_arousal == 0.5  # 默认权重
    assert card.emotion_decay_rate == 0.4

    scene = Scene(name="餐厅", participants=["甲", "乙"],
                  circles=[], hard_boundary=HardBoundary(
                      type="time", value="22:00", desc="打烊"))
    assert scene.hard_boundary.value == "22:00"


def test_think_result_rejects_out_of_range_urge():
    with pytest.raises(ValidationError):
        ThinkResult(aroused=0.5, urge=2.1)  # urge 越界（合法域 -1~2 闭区间）
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd "d:/开发/ensemble/app"
.venv/Scripts/python -m pytest src/harness/tests/test_schemas.py -v
```

Expected: FAIL（`ModuleNotFoundError: harness` 或 `harness.schemas` 不存在）。

- [ ] **Step 3: 实现 schemas.py**

`app/src/harness/schemas.py`：

```python
"""三张核心 schema + think 步锁死输出（设计文档 §4/§5/§6/§8.3）。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

SpeakerType = Literal["character", "director", "narrator", "human", "system"]


class Message(BaseModel):
    """消息：说话者 + 在场轴(in_scene) + 认知轴(knows)，两条正交轴。"""
    id: int
    speaker: str
    speaker_type: SpeakerType = "character"
    content: str
    in_scene: str
    knows: Optional[list[str]] = None   # None = 全员可见（在场内）
    reply_to: Optional[int] = None
    address: Optional[str] = None       # 点名/邻接对
    turn: int = 0

    def is_visible_to(self, character: str) -> bool:
        """认知轴判定：自己是说话者当然可见；否则 sees self 或 knows=None。"""
        if self.speaker == character:
            return True
        return self.knows is None or character in self.knows


class Weights(BaseModel):
    """心理权重 w1~w7（设计文档 §5）。0~1，默认 0.5 保守起步。"""
    w1_relevance: float = 0.5
    w2_arousal: float = 0.5
    w3_adjacency: float = 0.5
    w4_goal_pressure: float = 0.5
    w5_talkativeness: float = 0.5
    w6_inhibition: float = 0.5
    w7_scene_pressure: float = 0.5


class CharacterCard(BaseModel):
    name: str
    personality: dict[str, str] = Field(default_factory=dict)
    abilities: list[str] = Field(default_factory=list)
    relationships: dict[str, str] = Field(default_factory=dict)
    knowledge_boundary: list[str] = Field(default_factory=list)
    weights: Weights = Field(default_factory=Weights)
    emotion_decay_rate: float = 0.4


class Circle(BaseModel):
    id: str
    members: list[str]


class HardBoundary(BaseModel):
    type: Literal["time", "physical", "social", "goal"]
    value: str
    desc: str = ""


class Scene(BaseModel):
    name: str
    participants: list[str]
    circles: list[Circle] = Field(default_factory=list)
    hard_boundary: Optional[HardBoundary] = None


class ThinkResult(BaseModel):
    """think 步锁死输出（设计文档 §8.3）：urge 是唯一给调度器看的数。"""
    aroused: float = Field(ge=0.0, le=1.0)
    obligation_fulfilled: list[str] = Field(default_factory=list)
    goal_progress: float = Field(default=0.0, ge=0.0, le=1.0)
    addressed: Optional[str] = None
    impression_of_speaker: Optional[str] = None
    urge: float = Field(ge=-1.0, le=2.0)
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd "d:/开发/ensemble/app"
.venv/Scripts/python -m pytest src/harness/tests/test_schemas.py -v
```

Expected: 4 passed。

- [ ] **Step 5: Commit**

```bash
cd "d:/开发/ensemble"
git add -A && git commit -m "feat: 三张 schema 与 ThinkResult 锁死格式"
```

---

## Task 3: loaders.py — 配置/角色卡/场景装载

**Files:**
- Create: `app/src/harness/loaders.py`
- Test: `app/src/harness/tests/test_loaders.py`

**Interfaces:**
- Produces:
  - `load_json(path: Path) -> dict`
  - `load_scene(path: Path) -> Scene`
  - `load_character_card(path: Path) -> CharacterCard`
  - `load_models(path: Path) -> dict[str, ModelConfig]`，其中 `ModelConfig` 为 dataclass：`backend: str; model: str; params: dict`
  - `load_bid_params(path: Path) -> BidParams`（引 Task 5 类型；此任务先用内部字段名写测试，Task 5 定型后不改此文件——见 Step 1 注释）

> 注：`ModelConfig` 定义在本文件；`BidParams` 在 bidding.py（Task 5）。loaders 的 `load_bid_params` 测试在 Task 5 之后补跑；本任务先只实现、并把它的导入写成惰性（见 Step 3），避免前向依赖。

- [ ] **Step 1: 写失败测试（models/scene/card 部分）**

`app/src/harness/tests/test_loaders.py`：

```python
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from harness.loaders import ModelConfig, load_character_card, load_models, load_scene


def _write(tmp_path: Path, name: str, data) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def test_load_scene_validates(tmp_path):
    p = _write(tmp_path, "餐厅.json", {
        "name": "餐厅", "participants": ["甲", "乙"],
        "circles": [{"id": "桌A", "members": ["甲", "乙"]}],
        "hard_boundary": {"type": "time", "value": "22:00", "desc": "打烊"},
    })
    scene = load_scene(p)
    assert scene.name == "餐厅"
    assert scene.hard_boundary.value == "22:00"


def test_load_scene_rejects_missing_participants(tmp_path):
    p = _write(tmp_path, "bad.json", {"name": "餐厅"})
    with pytest.raises(Exception):
        load_scene(p)


def test_load_character_card_defaults_weights(tmp_path):
    p = _write(tmp_path, "甲.json", {"name": "甲", "personality": {"描述": "冷静"}})
    card = load_character_card(p)
    assert card.weights.w1_relevance == 0.5
    assert card.emotion_decay_rate == 0.4


def test_load_models_yaml(tmp_path):
    p = tmp_path / "models.yaml"
    p.write_text(
        "think:\n  backend: stub\n  model: stub\n  params: {}\n"
        "speak:\n  backend: stub\n  model: stub\n  params: {}\n",
        encoding="utf-8",
    )
    cfg = load_models(p)
    assert cfg["think"] == ModelConfig(backend="stub", model="stub", params={})
    assert asdict(cfg["speak"]) == {"backend": "stub", "model": "stub", "params": {}}
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd "d:/开发/ensemble/app"
.venv/Scripts/python -m pytest src/harness/tests/test_loaders.py -v
```

Expected: FAIL（模块不存在）。

- [ ] **Step 3: 实现 loaders.py**

`app/src/harness/loaders.py`：

```python
"""角色卡/场景/模型配置装载：文件即 system prompt + 参数（设计文档 §6）。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .schemas import CharacterCard, Scene


@dataclass
class ModelConfig:
    backend: str          # 'stub' | 'deepseek'
    model: str
    params: dict = field(default_factory=dict)


def load_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_scene(path: Path) -> Scene:
    return Scene.model_validate(load_json(path))


def load_character_card(path: Path) -> CharacterCard:
    return CharacterCard.model_validate(load_json(path))


def load_models(path: Path) -> dict[str, ModelConfig]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    out: dict[str, ModelConfig] = {}
    for role, cfg in raw.items():
        out[role] = ModelConfig(**cfg)
    return out


def load_bid_params(path: Path) -> Any:
    """惰性导入 bidding，避免本文件在 Task 5 之前依赖它。"""
    from .bidding import BidParams
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return BidParams(**raw)
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd "d:/开发/ensemble/app"
.venv/Scripts/python -m pytest src/harness/tests/test_loaders.py -v
```

Expected: 4 passed。

- [ ] **Step 5: Commit**

```bash
cd "d:/开发/ensemble"
git add -A && git commit -m "feat: 角色卡/场景/模型配置装载与校验"
```

---

## Task 4: visibility.py — 信息边界可见性投影

**Files:**
- Create: `app/src/harness/visibility.py`
- Test: `app/src/harness/tests/test_visibility.py`

**Interfaces:**
- Produces: `view_for(messages: list[Message], character: str, circles: set[str]) -> list[Message]`（同时满足在场轴 `in_scene ∈ circles` 与认知轴 `knows`）；`render_view(view) -> str`（拼接为提示词用文本，含 id/说话者/内容）。

- [ ] **Step 1: 写失败测试**

`app/src/harness/tests/test_visibility.py`：

```python
from harness.schemas import Message
from harness.visibility import view_for


def m(id, speaker, content, in_scene="餐厅", knows=None, address=None):
    return Message(id=id, speaker=speaker, content=content,
                   in_scene=in_scene, knows=knows, address=address)


MSG = [
    m(1, "甲", "这桌就我们俩", in_scene="餐厅"),
    m(2, "乙", "嘘，那边有耳", in_scene="餐厅", knows=["甲", "乙"]),
    m(3, "丙", "（另一桌说）", in_scene="桌B", knows=["丙", "丁"]),
    m(4, "甲", "接上这句", in_scene="餐厅", reply_to=1),
]


def test_knows_restricts_across_characters():
    view = view_for(MSG, "乙", circles={"餐厅"})
    ids = [x.id for x in view]
    assert 2 in ids and 4 in ids


def test_knows_blocks_non_member():
    # 丙不在餐厅圈，且看不到桌A 秘密
    view = view_for(MSG, "丙", circles={"餐厅"})
    assert view == []


def test_scene_axis_filters_circle():
    # 甲在餐厅，只看得到桌A 相关；桌B 那句被在场轴挡住
    view = view_for(MSG, "甲", circles={"餐厅"})
    ids = [x.id for x in view]
    assert 3 not in ids


def test_reply_chain_stays_within_visible_set():
    # reply_to=1 的 4 号若能看到，说明其可见；这里验证可见集含祖先 1 号
    view = view_for(MSG, "乙", circles={"餐厅"})
    by_id = {x.id: x for x in view}
    assert by_id[4].reply_to in by_id  # 可见集内可回溯
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd "d:/开发/ensemble/app"
.venv/Scripts/python -m pytest src/harness/tests/test_visibility.py -v
```

Expected: FAIL。

- [ ] **Step 3: 实现 visibility.py**

`app/src/harness/visibility.py`：

```python
"""可见性投影：信息边界 = 数据访问控制（设计文档 原则3）。

认知轴 knows（谁能看） × 在场轴 in_scene（谁在本圈）分开判定。
纯函数、无 IO、不依赖 LangGraph，可独立单测。
"""
from __future__ import annotations

from typing import Iterable

from .schemas import Message


def view_for(messages: Iterable[Message], character: str, circles: set[str]) -> list[Message]:
    """返回 character 可见且在 circles 内、按 id 升序的消息子集。"""
    view = [
        msg for msg in messages
        if msg.in_scene in circles and msg.is_visible_to(character)
    ]
    return sorted(view, key=lambda x: x.id)


def render_view(view: list[Message]) -> str:
    """拼成给 LLM 的转录文本；只含可见集，故 reply_to 链必然可回溯。"""
    lines = []
    for msg in view:
        addr = f" →{msg.address}" if msg.address else ""
        lines.append(f"[{msg.id}] {msg.speaker}{addr}: {msg.content}")
    return "\n".join(lines)
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd "d:/开发/ensemble/app"
.venv/Scripts/python -m pytest src/harness/tests/test_visibility.py -v
```

Expected: 4 passed。

- [ ] **Step 5: Commit**

```bash
cd "d:/开发/ensemble"
git add -A && git commit -m "feat: 可见性投影——认知轴×在场轴硬边界"
```

---

## Task 5: bidding.py — 块级竞价仲裁

**Files:**
- Create: `app/src/harness/bidding.py`
- Test: `app/src/harness/tests/test_bidding.py`

**Interfaces:**
- Produces:
  - `BidParams` dataclass：`interruption_threshold: float=0.4; speak_threshold: float=0.1; silence_k: int=3`
  - `Decision` dataclass：`kind: str` ∈ {`incumbent_continues`, `yield_to`, `silence`}；`speaker: str|None=None`；`silent_streak: int=0`
  - `arbitrate(urges: dict[str, float], incumbent: str|None, silent_streak: int, p: BidParams = BidParams()) -> Decision`
  - `should_inject(silent_streak: int, k: int = 3) -> bool`

语义（设计文档 §7.3/7.5）：在位者连任需 `urge >= 次高 + interruption_threshold`，否则让位给最高者；全员低于 `speak_threshold` → 静默（streak+1）；静默 streak ≥ silence_k → 世界层注入/收束（由 world 判断）。

- [ ] **Step 1: 写失败测试**

`app/src/harness/tests/test_bidding.py`：

```python
from harness.bidding import BidParams, Decision, arbitrate, should_inject


def test_no_incumbent_yields_to_highest():
    d = arbitrate({"甲": 0.9, "乙": 0.2}, incumbent=None, silent_streak=0)
    assert d == Decision("yield_to", speaker="甲", silent_streak=0)


def test_incumbent_continues_when_within_threshold():
    # 在位者 0.9 vs 次高 0.7，差值 0.2 < 0.4 → 连任
    d = arbitrate({"甲": 0.9, "乙": 0.7}, incumbent="甲", silent_streak=0)
    assert d.kind == "incumbent_continues"
    assert d.speaker == "甲"


def test_incumbent_yields_when_overtaken_beyond_threshold():
    # 乙 1.2 超过甲 0.5 达 0.7 > 0.4 → 打断让位
    d = arbitrate({"甲": 0.5, "乙": 1.2}, incumbent="甲", silent_streak=0)
    assert d == Decision("yield_to", speaker="乙", silent_streak=0)


def test_all_silent_increments_streak():
    d = arbitrate({"甲": 0.02, "乙": 0.01}, incumbent=None, silent_streak=1)
    assert d == Decision("silence", speaker=None, silent_streak=2)


def test_should_inject_at_k():
    assert should_inject(2, k=3) is False
    assert should_inject(3, k=3) is True
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd "d:/开发/ensemble/app"
.venv/Scripts/python -m pytest src/harness/tests/test_bidding.py -v
```

Expected: FAIL。

- [ ] **Step 3: 实现 bidding.py**

`app/src/harness/bidding.py`：

```python
"""块级竞价仲裁（设计文档 §7，纯函数）。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BidParams:
    interruption_threshold: float = 0.4   # 在位者优势阈值
    speak_threshold: float = 0.1          # 开口阈值：低于=无人想开口
    silence_k: int = 3                    # 连续静默 K 块后世界层兜底


@dataclass(frozen=True)
class Decision:
    kind: str                             # incumbent_continues | yield_to | silence
    speaker: str | None = None
    silent_streak: int = 0


def _second_highest(urges: dict[str, float], excluded: str) -> float:
    vals = sorted((v for n, v in urges.items() if n != excluded), reverse=True)
    return vals[0] if vals else 0.0


def arbitrate(urges: dict[str, float],
              incumbent: str | None,
              silent_streak: int,
              p: BidParams = BidParams()) -> Decision:
    """每块末对所有刚 think 完的在场听众的 urge 做一次仲裁。"""
    if not urges:
        return Decision("silence", silent_streak=silent_streak + 1)
    best = max(urges, key=urges.get)
    if urges[best] < p.speak_threshold:            # 全员低于开口阈值 → 静默
        return Decision("silence", silent_streak=silent_streak + 1)
    if incumbent is not None:
        cur = urges.get(incumbent, 0.0)
        if cur >= _second_highest(urges, incumbent) + p.interruption_threshold:
            return Decision("incumbent_continues", speaker=incumbent, silent_streak=0)
    return Decision("yield_to", speaker=best, silent_streak=0)


def should_inject(silent_streak: int, k: int = 3) -> bool:
    return silent_streak >= k
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd "d:/开发/ensemble/app"
.venv/Scripts/python -m pytest src/harness/tests/test_bidding.py -v
```

Expected: 5 passed。

- [ ] **Step 5: 补 load_bid_params 验收**

```bash
cd "d:/开发/ensemble/app"
.venv/Scripts/python -c "from harness.loaders import load_bid_params; from pathlib import Path; print(load_bid_params(Path('config/bid.yaml')))"
```

先建 `app/config/bid.yaml`：

```yaml
interruption_threshold: 0.4
speak_threshold: 0.1
silence_k: 3
```

Expected: 打印 `BidParams(...)` 不报错。

- [ ] **Step 6: Commit**

```bash
cd "d:/开发/ensemble"
git add -A && git commit -m "feat: 块级竞价仲裁（在位者优势/打断/静默兜底）"
```

---

## Task 6: memory.py — 角色私有记忆文件夹

**Files:**
- Create: `app/src/harness/memory.py`
- Test: `app/src/harness/tests/test_memory.py`

**Interfaces:**
- Produces: `CharacterMemory`：
  - `__init__(self, root: Path, character: str)`（root=runs/<scene_id>；目录自动建）
  - `append_state(entry: dict)` / `load_state() -> list[dict]`（`state.jsonl`）
  - `append_impression(other: str, line: str, max_lines: int = 200)` / `read_impressions() -> dict[str, str]`（`impressions/<other>.md`，超过 max_lines 丢最旧行）
  - `append_visible(msg_json: dict)` / `read_visible() -> list[dict]`（`transcript.visible.<me>.jsonl`）

- [ ] **Step 1: 写失败测试**

`app/src/harness/tests/test_memory.py`：

```python
from pathlib import Path

from harness.memory import CharacterMemory


def test_state_append_and_read(run_root: Path):
    mem = CharacterMemory(run_root / "scene1", "甲")
    mem.append_state({"aroused": 0.6, "goal_progress": 0.1})
    mem.append_state({"aroused": 0.2, "goal_progress": 0.4})
    assert mem.load_state() == [{"aroused": 0.6, "goal_progress": 0.1},
                                {"aroused": 0.2, "goal_progress": 0.4}]


def test_impressions_capped_to_max_lines(run_root: Path):
    mem = CharacterMemory(run_root / "scene1", "甲")
    for i in range(205):
        mem.append_impression("乙", f"第{i}句印象", max_lines=200)
    text = mem.read_impressions()["乙"]
    assert "第0句印象" not in text   # 最旧被丢
    assert "第204句印象" in text


def test_impressions_isolated_per_character(run_root: Path):
    a = CharacterMemory(run_root / "scene1", "甲")
    b = CharacterMemory(run_root / "scene1", "乙")
    a.append_impression("乙", "甲眼中的乙")
    assert b.read_impressions() == {}   # 乙读不到甲的印象文件夹
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd "d:/开发/ensemble/app"
.venv/Scripts/python -m pytest src/harness/tests/test_memory.py -v
```

Expected: FAIL。

- [ ] **Step 3: 实现 memory.py**

`app/src/harness/memory.py`：

```python
"""角色私有记忆文件夹（设计文档 §9）：独立目录、跨场景可持久、仅本人可读。

布局：runs/<scene>/<character>/
  state.jsonl                      私有状态流（情绪/目标/义务…）
  impressions/<他人>.md            我对 TA 的印象（Theory of Mind）
  transcript.visible.<me>.jsonl    我可见的转录（由 visibility 投影而来）
"""
from __future__ import annotations

import json
from pathlib import Path


class CharacterMemory:
    def __init__(self, root: Path, character: str):
        self.folder = Path(root) / character
        self.character = character
        self._imp_dir = self.folder / "impressions"
        self.folder.mkdir(parents=True, exist_ok=True)
        self._imp_dir.mkdir(exist_ok=True)

    # ---- state ----
    def append_state(self, entry: dict) -> None:
        with (self.folder / "state.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def load_state(self) -> list[dict]:
        p = self.folder / "state.jsonl"
        if not p.exists():
            return []
        return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line]

    # ---- impressions ----
    def append_impression(self, other: str, line: str, max_lines: int = 200) -> None:
        p = self._imp_dir / f"{other}.md"
        lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
        lines.append(line)
        if len(lines) > max_lines:          # MVP：软上限，丢最旧
            lines = lines[-max_lines:]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def read_impressions(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for p in self._imp_dir.glob("*.md"):
            out[p.stem] = p.read_text(encoding="utf-8")
        return out

    # ---- visible transcript ----
    def append_visible(self, msg_json: dict) -> None:
        name = f"transcript.visible.{self.character}.jsonl"
        with (self.folder / name).open("a", encoding="utf-8") as f:
            f.write(json.dumps(msg_json, ensure_ascii=False) + "\n")

    def read_visible(self) -> list[dict]:
        name = f"transcript.visible.{self.character}.jsonl"
        p = self.folder / name
        if not p.exists():
            return []
        return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line]

- [ ] **Step 4: 跑测试确认通过**

```bash
cd "d:/开发/ensemble/app"
.venv/Scripts/python -m pytest src/harness/tests/test_memory.py -v
```

Expected: 3 passed。

- [ ] **Step 5: Commit**

```bash
cd "d:/开发/ensemble"
git add -A && git commit -m "feat: 角色私有记忆文件夹（state/印象/可见转录）"
```

---

## Task 7: backends — 抽象 + 确定性 stub

**Files:**
- Create: `app/src/harness/backends/base.py`
- Create: `app/src/harness/backends/stub.py`
- Modify: `app/src/harness/backends/__init__.py`（暴露 `get_backend`）
- Test: `app/src/harness/tests/test_backends.py`

**Interfaces:**
- Produces:
  - `base.ModelBackend(ABC)`：`async complete_json(self, messages: list[dict]) -> dict`；`async complete_text(self, messages: list[dict]) -> str`
  - `stub.StubBackend`：构造 `StubBackend(json_script: list[dict] | None = None, line_script: list[str] | None = None)`。调用时按调用次序出队循环：`complete_json` 依次弹 `json_script`，`complete_text` 依次弹 `line_script`；**弹完即循环**（保证长对话不越界）。
  - `get_backend(name: str) -> ModelBackend`：`"stub"` 返回全局 `StubBackend()`（默认脚本），未知名字抛 `ValueError`。

- [ ] **Step 1: 写失败测试**

`app/src/harness/tests/test_backends.py`：

```python
import pytest

from harness.backends import get_backend
from harness.backends.stub import StubBackend


@pytest.mark.asyncio
async def test_stub_cycles_through_json_script():
    b = StubBackend(json_script=[{"urge": 0.1}, {"urge": 0.2}])
    assert (await b.complete_json([]))["urge"] == 0.1
    assert (await b.complete_json([]))["urge"] == 0.2
    assert (await b.complete_json([]))["urge"] == 0.1  # 循环回起点


@pytest.mark.asyncio
async def test_stub_text_script():
    b = StubBackend(line_script=["第一句", "第二句"])
    assert await b.complete_text([]) == "第一句"
    assert await b.complete_text([]) == "第二句"
    assert await b.complete_text([]) == "第一句"


def test_get_backend_unknown_name_raises():
    with pytest.raises(ValueError):
        get_backend("nope")
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd "d:/开发/ensemble/app"
.venv/Scripts/python -m pytest src/harness/tests/test_backends.py -v
```

Expected: FAIL。

- [ ] **Step 3: 实现 base/stub/__init__**

`app/src/harness/backends/base.py`：

```python
"""模型接入抽象：think/speak 双档统一入口（设计文档 §8.1）。"""
from __future__ import annotations

from abc import ABC, abstractmethod


class ModelBackend(ABC):
    """无头模型后端。complete_json 返回合法 JSON dict；complete_text 返回正文文本。"""

    @abstractmethod
    async def complete_json(self, messages: list[dict]) -> dict: ...

    @abstractmethod
    async def complete_text(self, messages: list[dict]) -> str: ...
```

`app/src/harness/backends/stub.py`：

```python
"""确定性脚本 stub：离线测试与演示用，不联网。"""
from __future__ import annotations

from .base import ModelBackend


class StubBackend(ModelBackend):
    def __init__(self, json_script: list[dict] | None = None,
                 line_script: list[str] | None = None):
        self._json_script = json_script or [{"aroused": 0.5, "obligation_fulfilled": [],
                                             "goal_progress": 0.0, "urge": 0.7}]
        self._line_script = line_script or ["（这句由 stub 生成。）"]
        self._j = 0
        self._t = 0

    async def complete_json(self, messages: list[dict]) -> dict:
        item = self._json_script[self._j % len(self._json_script)]
        self._j += 1
        return dict(item)

    async def complete_text(self, messages: list[dict]) -> str:
        line = self._line_script[self._t % len(self._line_script)]
        self._t += 1
        return line
```

`app/src/harness/backends/__init__.py`：

```python
"""按名字取后端。DeepSeek 适配器在 Task 8 加入，此处先注册 stub。"""
from __future__ import annotations

from .base import ModelBackend
from .stub import StubBackend

_REGISTRY: dict[str, type[ModelBackend]] = {"stub": StubBackend}


def get_backend(name: str) -> ModelBackend:
    try:
        cls = _REGISTRY[name]
    except KeyError:
        raise ValueError(f"未知后端: {name}（可选 {sorted(_REGISTRY)}）") from None
    return cls()
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd "d:/开发/ensemble/app"
.venv/Scripts/python -m pytest src/harness/tests/test_backends.py -v
```

Expected: 4 passed。

- [ ] **Step 5: Commit**

```bash
cd "d:/开发/ensemble"
git add -A && git commit -m "feat: 模型接入抽象与确定性 stub 后端"
```

---

## Task 8: backends/deepseek.py — 真实适配器（live 冒烟，离线测试）

**Files:**
- Create: `app/src/harness/backends/deepseek.py`
- Modify: `app/src/harness/backends/__init__.py`（注册 `deepseek`）
- Test: `app/src/harness/tests/test_backends.py`（追加，离线断言 URL/载荷拼装）

**Interfaces:**
- Produces: `DeepSeekBackend`：
  - `__init__(self, api_key: str, base_url: str = "https://api.deepseek.com", model: str = "deepseek-v4-flash", params: dict | None = None)`（params 支持 `thinking`=`enabled|disabled`、`reasoning_effort`、`max_tokens`、`temperature`、`timeout`(默认 90s)）
  - `async complete_json(...)`: 发 chat/completions，带 `response_format={"type":"json_object"}`；取 `choices[0].message.content` JSON 解析。
  - `async complete_text(...)`: 同上但无 response_format，返回 content。
  - 内部 `_payload(messages, json_mode)` 把 `thinking` 写进 `extra_body` 兼容层的正确位置（OpenAI 兼容：thinking 作为顶层未知参数字段随 body 发送）。

> 说明：DeepSeek 的 `thinking` 开关在 OpenAI SDK 里用 `extra_body={'thinking':...}`；我们 httpx 直连就是把该字段直接放 body。离线测试只断言 `_payload` 产出正确 dict，不发网络请求。

- [ ] **Step 1: 追加失败测试**

在 `test_backends.py` 末尾追加：

```python
from harness.backends.deepseek import DeepSeekBackend


def test_deepseek_payload_shape():
    b = DeepSeekBackend(api_key="k", params={"thinking": "disabled", "max_tokens": 40})
    body = b._payload([{"role": "user", "content": "hi"}], json_mode=True)
    assert body["model"] == "deepseek-v4-flash"
    assert body["response_format"] == {"type": "json_object"}
    assert body["thinking"] == {"type": "disabled"}
    assert body["max_tokens"] == 40
    assert body["messages"] == [{"role": "user", "content": "hi"}]


def test_deepseek_text_payload_no_json_mode():
    b = DeepSeekBackend(api_key="k", params={"thinking": "enabled"})
    body = b._payload([], json_mode=False)
    assert "response_format" not in body
    assert body["thinking"] == {"type": "enabled"}
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd "d:/开发/ensemble/app"
.venv/Scripts/python -m pytest src/harness/tests/test_backends.py -v
```

Expected: 新增 2 条 FAIL（deepseek 模块不存在）。

- [ ] **Step 3: 实现 deepseek.py**

`app/src/harness/backends/deepseek.py`：

```python
"""DeepSeek OpenAI 兼容适配器（2026-09：deepseek-v4-flash/pro，思考在请求体切换）。"""
from __future__ import annotations

import asyncio
import json

import httpx

from .base import ModelBackend


class DeepSeekBackend(ModelBackend):
    def __init__(self, api_key: str, base_url: str = "https://api.deepseek.com",
                 model: str = "deepseek-v4-flash", params: dict | None = None):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._params = params or {}
        self._timeout = self._params.pop("timeout", 90.0)   # 服务端排队可达 ~10min

    def _payload(self, messages: list[dict], json_mode: bool) -> dict:
        body: dict = {"model": self._model, "messages": messages}
        thinking = self._params.pop("thinking", "enabled")  # enabled|disabled
        body["thinking"] = {"type": thinking}
        if (eff := self._params.get("reasoning_effort")) is not None:
            body["reasoning_effort"] = eff
        if (mt := self._params.get("max_tokens")) is not None:
            body["max_tokens"] = mt
        if (temp := self._params.get("temperature")) is not None and thinking == "disabled":
            body["temperature"] = temp   # 温度仅非思考模式生效
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        return body

    async def _post(self, body: dict) -> dict:
        headers = {"Authorization": f"Bearer {self._api_key}",
                   "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.post(f"{self._base_url}/chat/completions",
                                  headers=headers, json=body)
            r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    async def complete_json(self, messages: list[dict]) -> dict:
        content = await self._post(self._payload(messages, json_mode=True))
        return json.loads(content)   # 解析失败由调用方(Pydantic)重试一次

    async def complete_text(self, messages: list[dict]) -> str:
        return await self._post(self._payload(messages, json_mode=False))
```

`app/src/harness/backends/__init__.py`（把 DeepSeek 注册进 registry，但**不 import 时联网**）：

```python
from .base import ModelBackend
from .deepseek import DeepSeekBackend
from .stub import StubBackend

_REGISTRY: dict[str, type[ModelBackend]] = {
    "stub": StubBackend,
    "deepseek": DeepSeekBackend,
}


def get_backend(name: str) -> ModelBackend:
    try:
        cls = _REGISTRY[name]
    except KeyError:
        raise ValueError(f"未知后端: {name}（可选 {sorted(_REGISTRY)}）") from None
    if cls is DeepSeekBackend:
        raise ValueError("deepseek 后端需 api_key：用 make_deepseek() 构造")
    return cls()


def make_deepseek(api_key: str, model_config) -> DeepSeekBackend:
    return DeepSeekBackend(api_key=api_key, model=model_config.model,
                           params=dict(model_config.params))
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd "d:/开发/ensemble/app"
.venv/Scripts/python -m pytest src/harness/tests/test_backends.py -v
```

Expected: 原 4 条 + 新 2 条 = 6 passed。

- [ ] **Step 5: Commit**

```bash
cd "d:/开发/ensemble"
git add -A && git commit -m "feat: DeepSeek OpenAI 兼容适配器（思考开关/JSON 模式/超时）"
```

---

## Task 9: prompters.py — think/speak 提示词构建

**Files:**
- Create: `app/src/harness/prompters.py`
- Test: `app/src/harness/tests/test_prompters.py`

**Interfaces:**
- Produces:
  - `system_prompt(card: CharacterCard) -> str`：把角色卡（含 knowledge_boundary 与权重说明）转成 system prompt。
  - `build_think_messages(card, view_text: str, last_chunk_text: str, scene_text: str) -> list[dict]`：system 含角色卡 + 硬性要求输出含英文 `json` 字样与 ThinkResult 字段示例；user 含可见转录/上一块/场景文本。
  - `build_speak_messages(card, view_text: str, scene_text: str) -> list[dict]`
  - 返回 `[{"role": "system", "content": ...}, {"role": "user", "content": ...}]`。

- [ ] **Step 1: 写失败测试**

`app/src/harness/tests/test_prompters.py`：

```python
from harness.prompters import build_speak_messages, build_think_messages
from harness.schemas import CharacterCard


def _card() -> CharacterCard:
    return CharacterCard(name="甲", personality={"描述": "冷静、观察多"})


def test_think_messages_contain_json_keyword_and_fields():
    msgs = build_think_messages(_card(), view_text="[1] 乙: 你好",
                                last_chunk_text="你好", scene_text="餐厅，10 点打烊")
    blob = msgs[0]["content"] + msgs[1]["content"]
    assert "json" in blob.lower()          # DeepSeek json_object 硬性要求
    assert '"urge"' in blob                # 输出字段示例
    assert '"impression_of_speaker"' in blob


def test_think_system_mentions_privacy_rule():
    msgs = build_think_messages(_card(), view_text="", last_chunk_text="",
                                scene_text="")
    assert "只输出" in msgs[0]["content"]  # 强调除 urge 外不外泄内心
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd "d:/开发/ensemble/app"
.venv/Scripts/python -m pytest src/harness/tests/test_prompters.py -v
```

Expected: FAIL。

- [ ] **Step 3: 实现 prompters.py**

`app/src/harness/prompters.py`：

```python
"""think/speak 提示词构建（设计文档 §8）。JSON 输出三件套之一：prompt 含 json 与示例。"""
from __future__ import annotations

from .schemas import CharacterCard

_THINK_SYSTEM_TMPL = """你是角色 {name}，正在参与一场会话。只依据你确实看见/经历过的信息行事，绝不假装知道没被告知的事。

【角色设定】{personality_json}

【你的已知边界】{kb}

【本次任务的硬性规则】
- 你刚听到一句新的话。用一次简短思考完成三件事：解释这句话、更新你的私有状态、自标注。
- 思考结果只输出一段 JSON，不要任何解释性文字（禁止小作文）。
- JSON 严格包含以下键（字段含义与取值范围）：
  "aroused": 0~1 的当前情绪唤醒度
  "obligation_fulfilled": list[str]，这句话兑现了哪些欠你的人情/挑衅（没有则空数组）
  "goal_progress": 0~1，你的私有目标因此推进了多少
  "addressed": 被这句话点名的人名或 null
  "impression_of_speaker": 你对说话人的一句新印象（可为 null）
  "urge": 你想开口说话的多大冲动（-1~2，越大越想抢话；决定权只由调度器看这一个数）
- 只输出这一份 JSON。请输出英文单词 json 引导的正确 JSON 文本。"""

_THINK_USER_TMPL = """【场景】{scene}
【你看到的最近对话】{view}
【刚听到的这一句】{last_chunk}

请输出你的 think JSON："""

_SPEAK_SYSTEM_TMPL = """你是角色 {name}。{personality_json} 用符合角色的口吻说话。只说你知道的事；不知道就不提。
说出的内容就是一个「言语单位」：一句话或一个简短动作/表情，不要长篇独白。中文输出。"""

_SPEAK_USER_TMPL = """【场景】{scene}
【你看到的最近对话】{view}

你现在开口："""


def _system_content(tmpl: str, card: CharacterCard) -> str:
    import json
    return tmpl.format(
        name=card.name,
        personality_json=json.dumps(card.personality, ensure_ascii=False),
        kb="；".join(card.knowledge_boundary) if card.knowledge_boundary else "只知道自己经历和被告知的事",
    )


def system_prompt(card: CharacterCard) -> str:
    return _system_content(_SPEAK_SYSTEM_TMPL, card)


def build_think_messages(card: CharacterCard, view_text: str, last_chunk_text: str,
                         scene_text: str) -> list[dict]:
    system = _system_content(_THINK_SYSTEM_TMPL, card)
    user = _THINK_USER_TMPL.format(scene=scene_text, view=view_text or "（暂无）",
                                   last_chunk=last_chunk_text or "（开场，无需回应）")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def build_speak_messages(card: CharacterCard, view_text: str, scene_text: str) -> list[dict]:
    system = _system_content(_SPEAK_SYSTEM_TMPL, card)
    user = _SPEAK_USER_TMPL.format(scene=scene_text, view=view_text or "（暂无）")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd "d:/开发/ensemble/app"
.venv/Scripts/python -m pytest src/harness/tests/test_prompters.py -v
```

Expected: 2 passed。

- [ ] **Step 5: Commit**

```bash
cd "d:/开发/ensemble"
git add -A && git commit -m "feat: think/speak 提示词模板（含 json 输出与隐私约束）"
```

---

## Task 10: events.py + graph M2 — 双角色固定轮流 think/speak

**Files:**
- Create: `app/src/harness/events.py`
- Create: `app/src/harness/graph.py`
- Test: `app/src/harness/tests/test_graph_m2.py`

**Interfaces:**
- Produces:
  - `events.py`：`make_events() -> list[dict]`（事件契约占位；`block_spoken`/`thinks_done`/`decision` 结构见 Task 12 完善）
  - `graph.py`：
    - `GraphState(TypedDict)`：`messages: Annotated[list[dict], _append_msgs]`、`urges: Annotated[dict[str, float], _merge_urges]`、`current_speaker: str|None`、`turn: int`、`silent_streak: int`
    - `build_graph(cards: dict[str, CharacterCard], scene: Scene, think_backend, speak_backend, run_root: Path) -> CompiledGraph`
    - 图结构与行为（本任务=**M2，无竞价**）：每块轮到固定次序的下一位说话：`act` 节点直接选 `messages[-1]` 的下一位 → `speak`；所有在场听众先 `think` 更新私有记忆。竞价在 Task 11 替换 `act`。
  - 每个 `think` worker：为角色拼**独立可见视图**（visibility），调 think_backend.complete_json，用 `ThinkResult.model_validate` 校验，写回私有记忆（state + impression），返回 `urges={name: urge}`。
  - `speak` 节点：对胜者拼视图，调 speak_backend.complete_text 产出 block，追加消息并写各听众 `append_visible` 投影。

> 实现说明：think 的私有上下文来自 Send payload（不可见共享态），本任务用 Send 扇出 + `Command(goto=[Send(...)], update=...)`（LangGraph 1.x，见技术方案 §5.4/§16.1）。为控复杂度，state 的 reducer 只做最小集：`messages`（追加）、`urges`（dict 合并）。

- [ ] **Step 1: 写失败测试（离线 stub 双角色对白）**

`app/src/harness/tests/test_graph_m2.py`：

```python
import asyncio
from pathlib import Path

from harness.backends.stub import StubBackend
from harness.graph import build_graph
from harness.schemas import CharacterCard, Circle, Scene


def _two_role_fixture() -> tuple[dict, Scene, dict]:
    cards = {
        "甲": CharacterCard(name="甲", personality={"描述": "冷静"}),
        "乙": CharacterCard(name="乙", personality={"描述": "锐利"}),
    }
    scene = Scene(name="餐厅", participants=["甲", "乙"],
                  circles=[Circle(id="餐厅", members=["甲", "乙"])])
    backends = {
        "think": StubBackend(json_script=[
            {"aroused": 0.5, "obligation_fulfilled": [], "goal_progress": 0.0,
             "impression_of_speaker": "开局", "urge": 0.6}]),
        "speak": StubBackend(line_script=[
            "你说，我听着。", "这句话轮到我了吧。"]),
    }
    return cards, scene, backends


def test_m2_two_role_offline_dialogue(tmp_path):
    cards, scene, backends = _two_role_fixture()
    graph = build_graph(cards, scene, backends["think"], backends["speak"],
                        run_root=tmp_path / "runs")
    # 导演开场（一条 director 消息）进入 messages
    asyncio.run(graph.ainvoke({
        "messages": [{"id": 0, "speaker": "导演", "speaker_type": "director",
                      "content": "你二人在餐厅临窗而坐。", "in_scene": "餐厅", "turn": 0}],
        "urges": {}, "current_speaker": None, "turn": 0, "silent_streak": 0,
    }, config={"configurable": {"thread_id": "demo1"}, "max_concurrency": 4}))

    asyncio.run(graph.ainvoke({
        "messages": [], "urges": {}, "current_speaker": None, "turn": 0,
        "silent_streak": 0,
    }, config={"configurable": {"thread_id": "demo1"}, "max_concurrency": 4}))

    state = asyncio.run(_last_state(graph, "demo1"))
    texts = [m["content"] for m in state["messages"]]
    assert any("你说，我听着" in t for t in texts)
    assert state["turn"] >= 1
```

> 测试依赖 `_last_state` 助手（用 `graph.aget_state({"configurable": {"thread_id": ...}})` 取 checkpoint 末态），并断言**共享态不含私有字段**。助手放独立模块 `harness/tests/helpers.py`，Task 11/12 复用。

- [ ] **Step 2: 建测试助手 helpers.py**

Create `app/src/harness/tests/helpers.py`：

```python
"""测试助手：取 checkpoint 末态 + 共享态白名单断言（守 §4.1 信息边界）。"""
from __future__ import annotations

from langgraph.graph.state import CompiledStateGraph

# 公共调度字段白名单：messages/urges/current_speaker/turn/silent_streak
# 及 Task11-12 将加入的公共字段(decided/injected/closed/closing_at_block)。
# 角色私有(情绪/目标/印象)一旦出现在共享态即判失败。
PUBLIC_KEYS = {"messages", "urges", "current_speaker", "turn", "silent_streak",
               "decided", "injected", "closed", "closing_at_block"}


def last_state(graph: CompiledStateGraph, thread_id: str) -> dict:
    snap = graph.aget_state({"configurable": {"thread_id": thread_id}})
    return snap.values


def assert_public_only(state: dict) -> None:
    extra = set(state) - PUBLIC_KEYS
    assert not extra, f"共享态混入私有字段: {extra}"
```

把 `test_graph_m2.py` 顶部改为下面并补 `assert_public_only` 断言（`_last_state` 改名 `last_state`）：

```python
from harness.tests.helpers import assert_public_only, last_state
```

```python
    state = last_state(graph, "demo1")
    assert_public_only(state)
    texts = [m["content"] for m in state["messages"]]
    assert any("你说，我听着" in t for t in texts)
    assert state["turn"] >= 1
```

- [ ] **Step 3: 跑测试确认失败**

```bash
cd "d:/开发/ensemble/app"
.venv/Scripts/python -m pytest src/harness/tests/test_graph_m2.py -v
```

Expected: FAIL（`harness.graph` 不存在）。

- [ ] **Step 4: 实现 events.py 与 graph.py（M2）**

`app/src/harness/events.py`：

```python
"""引擎→界面事件契约（技术方案 §9.2）。MVP 先定载荷结构，Task 12 起产出。"""
from __future__ import annotations

from typing import Any


def block_spoken(msg: dict) -> dict:
    return {"type": "block_spoken", "payload": msg}


def thinks_done(urges: dict[str, float]) -> dict:
    # 只含标量 urge，不含内心理由——守信息边界
    return {"type": "thinks_done", "payload": {"urges": urges}}


def decision_event(kind: str, speaker: str | None = None) -> dict:
    return {"type": "decision", "payload": {"kind": kind, "speaker": speaker}}


def scene_state(clock: int, closed: bool) -> dict:
    return {"type": "scene_state", "payload": {"clock": clock, "closed": closed}}
```

`app/src/harness/graph.py`（本文件在 Task 10/11/12 持续演进；此任务实现 M2 底版 + 消息 reducer）：

```python
"""LangGraph 1.x：感知/权衡/行动/世界 节点（技术方案 §2/§5.4/§16.1）。

M2 底版：fan_out 清 urges → Send 扇出 think（每听众独立视图，worker 只见自己
payload）→ fan-in 后 act（固定轮流）→ END。M3 起 act 前插入 deliberate 竞价。
节点以闭包捕获 GraphContext（节点签名只有 state，无其它上下文）。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from . import memory as memory_mod
from .backends.base import ModelBackend
from .prompters import build_speak_messages, build_think_messages
from .schemas import CharacterCard, Scene, ThinkResult
from .visibility import render_view, view_for

# ---- reducer 区 ----
def _append_msgs(left: list[dict] | None, right: list[dict] | None) -> list[dict]:
    return (left or []) + (right or [])


def _merge_urges(left: dict[str, float] | None, right: dict[str, float] | None) -> dict[str, float]:
    return {**(left or {}), **(right or {})}


class GraphState(TypedDict, total=False):
    messages: Annotated[list[dict], _append_msgs]
    urges: Annotated[dict[str, float], _merge_urges]
    current_speaker: str | None
    turn: int
    silent_streak: int


@dataclass
class GraphContext:
    """节点依赖（cards/scene/backends/run_root）经闭包注入，不进节点签名。"""
    cards: dict[str, CharacterCard]
    scene: Scene
    think: ModelBackend
    speak: ModelBackend
    run_root: Path


def _is_visible_to(msg: dict, character: str) -> bool:
    return (msg["speaker"] == character
            or msg.get("knows") is None or character in msg["knows"])


def _make_m2_nodes(ctx: GraphContext):
    def fan_out(state: GraphState) -> dict:
        """每块开始：清空上一轮 urges（本块 think 结果将经 reducer 重新聚合）。"""
        return {"urges": {}}

    def emit_thinks(state: GraphState) -> list[Send]:
        """Send 扇出：把每听众独立视图拼进各自 payload（worker 看不到父态）。"""
        circles = {c.id for c in ctx.scene.circles}
        last = state["messages"][-1] if state.get("messages") else None
        sends = []
        for name in ctx.scene.participants:
            view = view_for(state["messages"], name, circles)
            sends.append(Send("think", {
                "listener": name,
                "view_text": render_view(view),
                "last_chunk": last,
                "turn": state.get("turn", 0),
            }))
        return sends

    async def think(state: dict) -> dict:
        """每个 think worker 输入 = 自己的 Send payload；解释→更新私有态→自标注。"""
        listener = state["listener"]
        last = state.get("last_chunk")
        msgs = build_think_messages(
            ctx.cards[listener],
            view_text=state.get("view_text", ""),
            last_chunk_text=last["content"] if last else "",
            scene_text=f"场景：{ctx.scene.name}",
        )
        raw = await ctx.think.complete_json(msgs)          # 返回 dict
        tr = ThinkResult.model_validate(raw)               # 锁格式：非法即抛（M2 直抛）
        mem = memory_mod.CharacterMemory(ctx.run_root, listener)
        mem.append_state({"aroused": tr.aroused, "goal_progress": tr.goal_progress,
                          "addressed": tr.addressed,
                          "obligation_fulfilled": tr.obligation_fulfilled})
        if tr.impression_of_speaker and last:
            mem.append_impression(last["speaker"], tr.impression_of_speaker)
        return {"urges": {listener: tr.urge}}              # 只共享标量，守 §4.1

    async def act(state: GraphState) -> dict:
        """M2：固定轮流下一位（无竞价）。Task 11 会以竞价取代这里的选人。"""
        participants = ctx.scene.participants
        turn = state.get("turn", 0)
        name = participants[turn % len(participants)]
        circles = {c.id for c in ctx.scene.circles}
        view = view_for(state["messages"], name, circles)
        msgs = build_speak_messages(ctx.cards[name], render_view(view),
                                    f"场景：{ctx.scene.name}")
        content = await ctx.speak.complete_text(msgs)
        prev = state["messages"][-1] if state.get("messages") else None
        block = {"id": (prev["id"] + 1) if prev else 1,
                 "speaker": name, "speaker_type": "character",
                 "content": content,
                 "in_scene": prev["in_scene"] if prev else ctx.scene.name,
                 "turn": turn}
        for p in ctx.scene.participants:      # 可见者各自落转录（物理隔离）
            if _is_visible_to(block, p):
                memory_mod.CharacterMemory(ctx.run_root, p).append_visible(block)
        return {"messages": [block], "current_speaker": name,
                "turn": turn + 1, "silent_streak": 0, "urges": {}}

    return fan_out, emit_thinks, think, act


def build_graph(cards: dict[str, CharacterCard], scene: Scene,
                think_backend: ModelBackend, speak_backend: ModelBackend,
                run_root: Path, checkpointer=None, store=None,
                bid_params=None, closing_at_block: int = 1 << 30) -> object:
    """编译 LangGraph。checkpointer 缺省 MemorySaver（测试/单进程内存）；
    生产/SceneEngine 传入 SqliteSaver。bid_params/closing_at_block 由 M3/M4 消费。"""
    ctx = GraphContext(cards, scene, think_backend, speak_backend, Path(run_root))
    fan_out, emit_thinks, think, act = _make_m2_nodes(ctx)
    g = StateGraph(GraphState)
    g.add_node("fan_out", fan_out)
    g.add_node("think", think)
    g.add_node("act", act)
    g.add_edge(START, "fan_out")
    g.add_conditional_edges("fan_out", emit_thinks, ["think"])  # Send 扇出
    g.add_edge("think", "act")                                  # fan-in 汇聚
    g.add_edge("act", END)
    return g.compile(checkpointer=checkpointer or MemorySaver(), store=store)
```

> 注意：节点是 async（内部 await 后端），运行时一律 `await graph.ainvoke(...)`（同步 invoke 不可用于 async 节点）。M3 会在 `fan_out→think→act` 之间插入 `deliberate` 条件边，并把 `act` 的「固定轮流」换成读 `decided`。

- [ ] **Step 5: 跑测试确认通过并绿**

```bash
cd "d:/开发/ensemble/app"
.venv/Scripts/python -m pytest src/harness/tests/test_graph_m2.py -v
```

Expected: PASS（stub 离线两轮对白，共享态仅含白名单键）。若因 LangGraph 细节（Send 收尾/Command 形态）报错，以 1.2 官方文档为准微调——但**不改信息边界断言**。

- [ ] **Step 6: Commit**

```bash
cd "d:/开发/ensemble"
git add -A && git commit -m "feat: LangGraph 图 M2——Send 扇出 think + 双角色固定轮流 speak"
```

---

## Task 11: graph M3 — 竞价仲裁接入（打断/静默/注入）

**Files:**
- Modify: `app/src/harness/graph.py`（加入 `deliberate` 节点与 `world` 静默判定；`act` 改由 `Decision` 驱动）
- Test: `app/src/harness/tests/test_graph_m3.py`

**Interfaces:**
- Produces（在 Task 10 基础上追加共享键）：`decided: dict|None`（仲裁结果，`{"kind", "speaker", "silent_streak"}`）；`injected: list[dict]`（世界层注入的消息，join 到 messages）。
- 语义：`deliberate`（纯代码节点）读 `urges` + `current_speaker`，调 `bidding.arbitrate`；决策写入 `decided`，并按 `kind` 条件边路由：`yield_to/incumbent_continues` → `speak`；`silence` → 若 `should_inject(silent_streak, k)` 为真则 `world.inject` 注入导演事件消息并结束本块，否则直接结束本块（无人开口的尴尬停顿也是真实状态）。

- [ ] **Step 1: 写失败测试**

`app/src/harness/tests/test_graph_m3.py`：

```python
import asyncio

from harness.backends.stub import StubBackend
from harness.graph import build_graph
from harness.schemas import CharacterCard, Circle, Scene
from harness.tests.helpers import assert_public_only, last_state


def _scene():
    return Scene(name="餐厅", participants=["甲", "乙"],
                 circles=[Circle(id="餐厅", members=["甲", "乙"])])


def _cards():
    return {
        "甲": CharacterCard(name="甲", weights={"w2_arousal": 0.2}),
        "乙": CharacterCard(name="乙", weights={"w2_arousal": 0.9}),
    }


def test_m3_interruption_yields_to_higher_urge(tmp_path):
    cards, scene = _cards(), _scene()
    # 乙 urge 恒定高 → 抢走甲的话
    think = StubBackend(json_script=[{"urge": 0.2}, {"urge": 1.5},
                                     {"urge": 0.2}, {"urge": 1.5}])
    speak = StubBackend(line_script=["甲的话。", "乙抢过话头。"])
    graph = build_graph(cards, scene, think, speak, run_root=tmp_path / "runs")

    asyncio.run(graph.ainvoke(
        {"messages": [{"id": 0, "speaker": "导演", "speaker_type": "director",
                       "content": "开局", "in_scene": "餐厅", "turn": 0}],
         "urges": {}, "current_speaker": None, "turn": 0, "silent_streak": 0,
         "decided": None, "injected": []},
        config={"configurable": {"thread_id": "m3a"}, "max_concurrency": 4}))
    for i in range(2):
        asyncio.run(graph.ainvoke(
            {"messages": [], "urges": {}, "current_speaker": None, "turn": i,
             "silent_streak": 0, "decided": None, "injected": []},
            config={"configurable": {"thread_id": "m3a"}, "max_concurrency": 4}))

    state = last_state(graph, "m3a")
    assert_public_only(state)
    # 至少出现过一次 yield_to 让位给乙，且甲的话没被说完就打断
    speakers = [m["speaker"] for m in state["messages"]]
    assert speakers[-1] == "乙"


def test_m3_silence_then_inject(tmp_path):
    cards, scene = _cards(), _scene()
    think = StubBackend(json_script=[{"urge": 0.0}, {"urge": 0.0},   # 连续静默
                                     {"urge": 0.0}, {"urge": 0.0},
                                     {"urge": 0.0}, {"urge": 0.0}])
    speak = StubBackend(line_script=["无人说话"])
    graph = build_graph(cards, scene, think, speak, run_root=tmp_path / "runs")

    asyncio.run(graph.ainvoke(
        {"messages": [{"id": 0, "speaker": "导演", "speaker_type": "director",
                       "content": "开局", "in_scene": "餐厅", "turn": 0}],
         "urges": {}, "current_speaker": None, "turn": 0, "silent_streak": 0,
         "decided": None, "injected": []},
        config={"configurable": {"thread_id": "m3b"}, "max_concurrency": 4}))

    for i in range(3):   # 连续 3 块静默 → 第 3 块应注入导演事件
        asyncio.run(graph.ainvoke(
            {"messages": [], "urges": {}, "current_speaker": None, "turn": i,
             "silent_streak": i, "decided": None, "injected": []},
            config={"configurable": {"thread_id": "m3b"}, "max_concurrency": 4}))

    state = last_state(graph, "m3b")
    assert any(m.get("speaker_type") == "director" and "注入" in m["content"]
               for m in state["messages"])
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd "d:/开发/ensemble/app"
.venv/Scripts/python -m pytest src/harness/tests/test_graph_m3.py -v
```

Expected: FAIL（尚无 deliberate 竞价语义/注入）。

- [ ] **Step 3: 实现 M3 竞价与静默注入**

在 `graph.py` 上做以下修改（`_make_m2_nodes` 更名为 `_make_m3_nodes` 并替换 act；两套测试都经 `build_graph` 跑，M2 测试在等 urge(0.6/0.6) 下由竞价自然让同一位连续说，仍绿）：

**a. imports 与 GraphState**：顶部加 `from . import bidding as bidding_mod`；`GraphState` 追加公共键：

```python
class GraphState(TypedDict, total=False):
    messages: Annotated[list[dict], _append_msgs]
    urges: Annotated[dict[str, float], _merge_urges]
    current_speaker: str | None
    turn: int
    silent_streak: int
    decided: dict | None          # 仲裁结果 {kind, speaker, silent_streak}
    injected: list[dict]          # 世界层注入消息（Task12 起并入 messages）
```

**b. `GraphContext` 增 `bid_params` 字段**（`build_graph` 传入，缺省 `bidding_mod.BidParams()`）。

**c. 新节点函数**（仍以闭包捕获 ctx，写进 `_make_m3_nodes`）：

```python
    def deliberate(state: dict) -> dict:
        d = bidding_mod.arbitrate(
            state.get("urges", {}),
            incumbent=state.get("current_speaker"),
            silent_streak=state.get("silent_streak", 0),
            p=ctx.bid_params,
        )
        return {"decided": {"kind": d.kind, "speaker": d.speaker,
                            "silent_streak": d.silent_streak}}

    def route_after_deliberate(state: dict) -> str:
        kind = state["decided"]["kind"]
        return "speak" if kind in ("yield_to", "incumbent_continues") else "silence_gate"

    async def speak(state: dict) -> dict:
        """读 decided.speaker 出块（顶替 M2 的固定轮流）。"""
        name = state["decided"]["speaker"]
        circles = {c.id for c in ctx.scene.circles}
        view = view_for(state["messages"], name, circles)
        msgs = build_speak_messages(ctx.cards[name], render_view(view),
                                    f"场景：{ctx.scene.name}")
        content = await ctx.speak.complete_text(msgs)
        prev = state["messages"][-1] if state.get("messages") else None
        block = {"id": (prev["id"] + 1) if prev else 1,
                 "speaker": name, "speaker_type": "character",
                 "content": content,
                 "in_scene": prev["in_scene"] if prev else ctx.scene.name,
                 "turn": state.get("turn", 0)}
        for p in ctx.scene.participants:
            if _is_visible_to(block, p):
                memory_mod.CharacterMemory(ctx.run_root, p).append_visible(block)
        return {"messages": [block], "current_speaker": name,
                "turn": state.get("turn", 0) + 1, "silent_streak": 0, "urges": {}}

    async def silence_gate(state: dict) -> dict:
        streak = state["decided"]["silent_streak"]
        if bidding_mod.should_inject(streak, k=ctx.bid_params.silence_k):
            prev = state["messages"][-1] if state.get("messages") else None
            msg = {"id": (prev["id"] + 1) if prev else 1,
                   "speaker": "导演", "speaker_type": "director",
                   "content": "（世界层注入：侍者过来添水。）",
                   "in_scene": prev["in_scene"] if prev else ctx.scene.name,
                   "turn": state.get("turn", 0)}
            return {"messages": [msg], "silent_streak": 0,
                    "injected": (state.get("injected", []) + [msg])}
        return {"silent_streak": streak}     # 尴尬停顿也是真实状态，本块到此为止
```

**d. `build_graph` 接线**（扇出链同 M2，`think` fan-in 后进 `deliberate`）：

```python
    g = StateGraph(GraphState)
    g.add_node("fan_out", fan_out)
    g.add_node("think", think)
    g.add_node("deliberate", deliberate)
    g.add_node("speak", speak)
    g.add_node("silence_gate", silence_gate)
    g.add_edge(START, "fan_out")
    g.add_conditional_edges("fan_out", emit_thinks, ["think"])
    g.add_edge("think", "deliberate")
    g.add_conditional_edges("deliberate", route_after_deliberate,
                            {"speak": "speak", "silence_gate": "silence_gate"})
    g.add_edge("speak", END)
    g.add_edge("silence_gate", END)
    return g.compile(checkpointer=checkpointer or MemorySaver(), store=store)
```

> 语义说明：`deliberate` 是纯代码节点（无 LLM）；`silence_gate` 在静默连续达 K 块时注入导演事件把「开口机会」交回，否则静默结束本块。

- [ ] **Step 4: 跑测试确认通过**

```bash
cd "d:/开发/ensemble/app"
.venv/Scripts/python -m pytest src/harness/tests/test_graph_m3.py -v
```

Expected: 2 passed。

- [ ] **Step 5: Commit**

```bash
cd "d:/开发/ensemble"
git add -A && git commit -m "feat: 竞价仲裁接入图——打断让位与连续静默注入"
```

---

## Task 12: world 节点 + 硬边界收束 + 导演 API + 事件产出

**Files:**
- Modify: `app/src/harness/graph.py`（`world` 收束判定：scene_clock 逼近/到点；注入接口）
- Create: `app/src/harness/engine.py`（高层 `SceneEngine`：open/inject/human/step/events/export）
- Test: `app/src/harness/tests/test_graph_m3.py` 追加收束用例 或新建 `test_engine.py`

**Interfaces:**
- Produces:
  - `SceneEngine`：
    - `__init__(self, scene_path, card_paths, models_path, run_root, api_key=None, bid_path=None)`
    - `async open_scene()`：导演开场消息进入 checkpoint（`messages=[导演开场]`）
    - `async step(n: int = 1) -> list[dict]`：推进 n 个块，返回期间产生的事件列表
    - `async inject(content: str)`：导演级注入一条事件消息
    - `async speak_as_human(content: str)`：以 `human` 类型说话（占位：本块结束后插入下一条消息）
    - `async close_scene()`：置 closed
    - `async export_transcript(path: Path)`：把 messages 渲染成可读转录
  - `closed` 进共享键；`scene_clock` 从导演开场 `scene_clock` 起算，`world` 每块 +1，`时间到点`（`scene_clock >= 打烊时间折算的块数`）→ 收束（写一条导演收束消息，置 closed）。

> 时间边界 MVP 简化：场景 schema 的 `hard_boundary.value` 是「22:00」字符串；MVP 用「**导演开场时注入 `closing_at_block`（从 22:00 折算一个块上限，默认 e.g. 8 块）**」进入 state，`world` 每块检查 `turn >= closing_at_block` 即收束。实现放 engine 的开场参数，避免真正时间换算。见 Step 1 测试的约定。

- [ ] **Step 1: 写失败测试（收束 + 事件 + 导出）**

`app/src/harness/tests/test_engine.py`：

```python
import asyncio
from pathlib import Path

from harness.engine import SceneEngine


def _write(tmp_path: Path):
    import json
    (tmp_path / "餐厅.json").write_text(json.dumps({
        "name": "餐厅", "participants": ["甲", "乙"],
        "circles": [{"id": "餐厅", "members": ["甲", "乙"]}],
        "hard_boundary": {"type": "time", "value": "22:00", "desc": "打烊"}}, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "甲.json").write_text(json.dumps({"name": "甲",
        "personality": {"描述": "冷静"}}, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "乙.json").write_text(json.dumps({"name": "乙",
        "personality": {"描述": "锐利"}}, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "models.yaml").write_text(
        "think:\n  backend: stub\n  model: stub\n  params: {}\n"
        "speak:\n  backend: stub\n  model: stub\n  params: {}\n", encoding="utf-8")
    return (tmp_path / "餐厅.json", tmp_path / "甲.json",
            tmp_path / "乙.json", tmp_path / "models.yaml")


def test_engine_boundary_closes_and_exports(tmp_path: Path):
    scene_p, a_p, b_p, models_p = _write(tmp_path)
    eng = SceneEngine(scene_p, [a_p, b_p], models_p,
                      run_root=tmp_path / "runs", bid_path=None,
                      closing_at_block=4)
    events = asyncio.run(eng.run_to_close())
    assert events and any(e["type"] == "scene_state" and e["payload"]["closed"]
                          for e in events)
    assert eng.closed is True

    out = tmp_path / "t.md"
    asyncio.run(eng.export_transcript(out))
    text = out.read_text(encoding="utf-8")
    assert "甲" in text or "乙" in text


def test_engine_inject_adds_director_message(tmp_path):
    scene_p, a_p, b_p, models_p = _write(tmp_path)
    eng = SceneEngine(scene_p, [a_p, b_p], models_p,
                      run_root=tmp_path / "runs", closing_at_block=99)
    asyncio.run(eng.open_scene())
    asyncio.run(eng.inject("（电话响了。）"))
    msgs = asyncio.run(eng.messages())
    assert any(m["content"] == "（电话响了。）" and m["speaker_type"] == "director"
               for m in msgs)
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd "d:/开发/ensemble/app"
.venv/Scripts/python -m pytest src/harness/tests/test_engine.py -v
```

Expected: FAIL（engine 不存在）。

- [ ] **Step 3: 实现 world 收束 + SceneEngine**

`graph.py` 内 `world` 节点（加入 build_graph，`world` 在所有 speak 之后执行，负责时钟与收束）：

```python
def world(state) -> dict:
    """每块收尾：检查是否到时间边界；到点则写收束消息并置 closed。"""
    turn = state.get("turn", 0)
    if state.get("closed"):
        return {"closed": True}
    if turn < state.get("closing_at_block", 1 << 30):
        return {"closed": False}
    prev = state["messages"][-1] if state.get("messages") else None
    msg = {"id": (prev["id"] + 1) if prev else 1,
           "speaker": "导演", "speaker_type": "director",
           "content": "（十点了，餐厅打烊，众人起身。）",
           "in_scene": prev["in_scene"] if prev else ctx.scene.name,
           "turn": turn}
    return {"closed": True, "messages": [msg]}
```

接线（`GraphState` 补公共键 `closed: bool`、`closing_at_block: int`，把 M3 的收尾边改经 `world`）：

```python
    g.add_node("world", world)
    g.add_edge("speak", "world")
    g.add_edge("silence_gate", "world")
    g.add_edge("world", END)
```

> `closing_at_block` 由 `build_graph` 的闭包捕获后经 engine `open_scene` 的 `aupdate_state` 写入共享态（见 engine）。helpers.py 白名单已含 `closed`/`closing_at_block`。

`app/src/harness/engine.py`：

```python
"""高层引擎：导演三件事(开场/注入/收束) + human 操作 + 逐块 step + 事件流。

消费技术方案 §9.2 事件契约；CLI(Task 13) 只消费 events。
"""
from __future__ import annotations

from pathlib import Path

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.store.memory import InMemoryStore

from . import graph as graph_mod
from .backends import get_backend, make_deepseek
from .loaders import load_bid_params, load_character_card, load_models, load_scene
from .schemas import CharacterCard, Scene
from . import events as ev


def _pick_backend(cfg, api_key):
    if cfg.backend == "stub":
        return get_backend("stub")
    if cfg.backend == "deepseek":
        assert api_key, "deepseek 后端需要 api_key（DEEPSEEK_API_KEY）"
        return make_deepseek(api_key, cfg)
    raise ValueError(cfg.backend)


class SceneEngine:
    def __init__(self, scene_path: Path, card_paths: list[Path], models_path: Path,
                 run_root: Path, bid_path: Path | None = None, api_key: str | None = None,
                 closing_at_block: int = 8, thread_id: str = "scene1"):
        self.scene: Scene = load_scene(scene_path)
        self.cards: dict[str, CharacterCard] = {
            load_character_card(p).name: load_character_card(p) for p in card_paths}
        models = load_models(models_path)
        self.think_backend = _pick_backend(models["think"], api_key)
        self.speak_backend = _pick_backend(models["speak"], api_key)
        bid = load_bid_params(bid_path) if bid_path else None
        self.closing_at_block = closing_at_block
        self.thread_id = thread_id
        self.run_root = Path(run_root)

        self._store = InMemoryStore()    # MVP 短期转录；Sqlite 落地见 §5.5 双写
        try:                             # 单机 Sqlite 优先；环境异常回退内存（测试用）
            self._saver = SqliteSaver.from_conn_string(str(run_root / "scene.sqlite"))
        except Exception:
            self._saver = MemorySaver()
        self.graph = graph_mod.build_graph(
            self.cards, self.scene, self.think_backend, self.speak_backend,
            run_root=run_root, bid_params=bid, checkpointer=self._saver,
            closing_at_block=closing_at_block, store=self._store)

    def _cfg(self):
        return {"configurable": {"thread_id": self.thread_id}, "max_concurrency": 4}

    async def messages(self) -> list[dict]:
        snap = self.graph.aget_state(self._cfg())
        return snap.values.get("messages", [])

    async def open_scene(self) -> list[dict]:
        opening = {"id": 0, "speaker": "导演", "speaker_type": "director",
                   "content": "夜晚的餐厅，二人临窗而坐。",
                   "in_scene": self.scene.circles[0].id, "turn": 0}
        events = [ev.block_spoken(opening), ev.scene_state(0, False)]
        await self.graph.aupdate_state(self._cfg(), {
            "messages": [opening], "current_speaker": None,
            "turn": 0, "silent_streak": 0,
            "closed": False, "decided": None, "injected": [],
            "closing_at_block": self.closing_at_block,
        })
        return events

    async def step(self, n: int = 1) -> list[dict]:
        out: list[dict] = []
        for _ in range(n):
            snap = self.graph.aget_state(self._cfg())
            st = snap.values
            if st.get("closed"):
                break
            await self.graph.ainvoke({}, self._cfg())
            st = self.graph.aget_state(self._cfg()).values
            out += [ev.decision_event("block_done"), ev.scene_state(
                st.get("turn", 0), st.get("closed", False))]
        return out

    async def inject(self, content: str) -> None:
        snap = self.graph.aget_state(self._cfg())
        msgs = snap.values.get("messages", [])
        nid = msgs[-1]["id"] + 1 if msgs else 1
        await self.graph.aupdate_state(self._cfg(), {"messages": [{
            "id": nid, "speaker": "导演", "speaker_type": "director",
            "content": content, "in_scene": msgs[-1]["in_scene"], "turn": 0}]})

    async def speak_as_human(self, content: str) -> None:
        await self.inject(f"[你] {content}")   # 类型用 system/human 扩展在 UI 后置期
        raise NotImplementedError("human 步进时序在 UI 后置期接入 §9.2")

    async def close_scene(self) -> None:
        await self.graph.aupdate_state(self._cfg(), {"closed": True})

    async def run_to_close(self, max_blocks: int = 60) -> list[dict]:
        events = await self.open_scene()
        for _ in range(max_blocks):
            if self.graph.aget_state(self._cfg()).values.get("closed"):
                break
            events += await self.step(1)
            if self.graph.aget_state(self._cfg()).values.get("closed"):
                break
        return events

    @property
    def closed(self) -> bool:
        return self.graph.aget_state(self._cfg()).values.get("closed", False)

    async def export_transcript(self, path: Path) -> None:
        msgs = await self.messages()
        lines = [f"**[{m['id']}] {m['speaker']}**: {m['content']}\n" for m in msgs]
        Path(path).write_text("\n".join(lines), encoding="utf-8")
```

> 时间收束由 `world` 依据 `turn >= closing_at_block` 完成（实现时在 graph.py 把 `world` 挂在 `speak`/`silence_gate` 之后、`END` 之前）。

- [ ] **Step 4: 跑测试确认通过**

```bash
cd "d:/开发/ensemble/app"
.venv/Scripts/python -m pytest src/harness/tests/test_engine.py -v
```

Expected: 2 passed。若 `aupdate_state`/`aget_state` 签名有出入，以 langgraph 1.2 文档微调。

- [ ] **Step 5: Commit**

```bash
cd "d:/开发/ensemble"
git add -A && git commit -m "feat: world 收束 + SceneEngine（开场/注入/step/收束/导出）"
```

---

## Task 13: 示例素材 + CLI runner + M4 演示

**Files:**
- Create: `app/config/models.yaml`
- Create: `app/characters/甲.json`、`app/characters/乙.json`
- Create: `app/scenes/餐厅.json`
- Create: `app/src/harness/runner.py`（argparse CLI）
- Test: `app/src/harness/tests/test_e2e_m4.py`

**Interfaces:**
- Produces:
  - `runner.py`：`python -m harness.runner --scene app/scenes/餐厅.json --characters app/characters/甲.json app/characters/乙.json --models app/config/models.yaml --steps 8 --export runs/out.md`（stub 默认离线跑；`--live` 用 `models.live.yaml` 指向 deepseek 需 `DEEPSEEK_API_KEY`）
  - `config/models.yaml`（stub 档，离线演示）
  - `config/models.live.yaml`（deepseek 档：think=`deepseek-v4-flash` 关思考、speak=`deepseek-v4-pro`）
  - e2e 离线测试：stub 双角色在餐厅场景跑到时间边界自动收束并导出转录

- [ ] **Step 1: 写失败测试**

`app/src/harness/tests/test_e2e_m4.py`：

```python
import asyncio
from pathlib import Path

from harness.engine import SceneEngine


def _paths(root: Path):
    return (root / "scenes" / "餐厅.json", root / "characters" / "甲.json",
            root / "characters" / "乙.json", root / "config" / "models.yaml")


def test_e2e_demo_offline_closes_and_exports(tmp_path: Path):
    scene_p, a_p, b_p, models_p = _paths(tmp_path)
    for p, content in [
        (scene_p, {"name": "餐厅", "participants": ["甲", "乙"],
                   "circles": [{"id": "餐厅", "members": ["甲", "乙"]}],
                   "hard_boundary": {"type": "time", "value": "22:00", "desc": "打烊"}}),
        (a_p, {"name": "甲", "personality": {"描述": "冷静，观察多于开口"}}),
        (b_p, {"name": "乙", "personality": {"描述": "锐利，话多且快"}}),
    ]:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(__import__("json").dumps(content, ensure_ascii=False), encoding="utf-8")
    models_p.parent.mkdir(parents=True, exist_ok=True)
    models_p.write_text(
        "think:\n  backend: stub\n  model: stub\n  params: {}\n"
        "speak:\n  backend: stub\n  model: stub\n  params: {}\n", encoding="utf-8")

    eng = SceneEngine(scene_p, [a_p, b_p], models_p, run_root=tmp_path / "runs",
                      closing_at_block=3)
    asyncio.run(eng.open_scene())
    for _ in range(5):
        if eng.closed:
            break
        asyncio.run(eng.step(1))
    assert eng.closed is True

    out = tmp_path / "transcript.md"
    asyncio.run(eng.export_transcript(out))
    assert out.exists() and "乙" in out.read_text(encoding="utf-8")
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd "d:/开发/ensemble/app"
.venv/Scripts/python -m pytest src/harness/tests/test_e2e_m4.py -v
```

Expected: FAIL（engine 依赖真实文件目录布局不同 → 以 tmp_path 复刻）。

> 若实现正常，本测试应与 Task 12 的 `test_engine.py` 一致地绿；两者保留其一亦可（此处保留为回归网）。

- [ ] **Step 3: 写真实示例素材**

`app/scenes/餐厅.json`：

```json
{
  "name": "餐厅",
  "participants": ["甲", "乙"],
  "circles": [{ "id": "餐厅", "members": ["甲", "乙"] }],
  "hard_boundary": { "type": "time", "value": "22:00", "desc": "餐厅打烊" }
}
```

`app/characters/甲.json`：

```json
{
  "name": "甲",
  "personality": { "描述": "冷静，话少但句句带刺；习惯先观察再开口。" },
  "abilities": [],
  "relationships": { "乙": "旧识" },
  "knowledge_boundary": ["只知道自己经历和被告知的事"],
  "weights": { "w1_relevance": 0.5, "w2_arousal": 0.3, "w3_adjacency": 0.6,
               "w4_goal_pressure": 0.4, "w5_talkativeness": 0.3,
               "w6_inhibition": 0.7, "w7_scene_pressure": 0.4 },
  "emotion_decay_rate": 0.5
}
```

`app/characters/乙.json`：

```json
{
  "name": "乙",
  "personality": { "描述": "锐利，话多且快；想到什么说什么，得理不饶人。" },
  "abilities": [],
  "relationships": { "甲": "旧识" },
  "knowledge_boundary": ["只知道自己经历和被告知的事"],
  "weights": { "w1_relevance": 0.4, "w2_arousal": 0.8, "w3_adjacency": 0.8,
               "w4_goal_pressure": 0.5, "w5_talkativeness": 0.9,
               "w6_inhibition": 0.1, "w7_scene_pressure": 0.3 },
  "emotion_decay_rate": 0.3
}
```

`app/config/models.yaml`（stub，离线演示）：

```yaml
think:
  backend: stub
  model: stub
  params: {}
speak:
  backend: stub
  model: stub
  params: {}
```

`app/config/models.live.yaml`（DeepSeek 真档，须 `DEEPSEEK_API_KEY`）：

```yaml
think:
  backend: deepseek
  model: deepseek-v4-flash
  params:
    thinking: disabled
    max_tokens: 400
    temperature: 0.3
speak:
  backend: deepseek
  model: deepseek-v4-pro
  params:
    thinking: enabled
    reasoning_effort: high
    max_tokens: 1024
```

- [ ] **Step 4: 实现 runner.py**

`app/src/harness/runner.py`：

```python
"""CLI：最小事件消费者（技术方案 §9.4）。--live 需 DEEPSEEK_API_KEY。"""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from .engine import SceneEngine


def main() -> None:
    ap = argparse.ArgumentParser(description="多智能体角色扮演 harness")
    ap.add_argument("--scene", required=True, type=Path)
    ap.add_argument("--characters", required=True, type=Path, nargs="+")
    ap.add_argument("--models", required=True, type=Path)
    ap.add_argument("--bid", type=Path)
    ap.add_argument("--steps", type=int, default=8)
    ap.add_argument("--closing-at-block", type=int, default=8)
    ap.add_argument("--run-root", type=Path, default=Path("runs"))
    ap.add_argument("--export", type=Path)
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args()

    models = args.models if not args.live else args.models.with_name("models.live.yaml")
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    eng = SceneEngine(args.scene, args.characters, models,
                      run_root=args.run_root, bid_path=args.bid,
                      api_key=api_key, closing_at_block=args.closing_at_block)
    asyncio.run(eng.open_scene())
    for _ in range(args.steps):
        if eng.closed:
            break
        asyncio.run(eng.step(1))
    print(f"[closed={eng.closed}] 共 {len(asyncio.run(eng.messages()))} 条消息")
    if args.export:
        asyncio.run(eng.export_transcript(args.export))
        print(f"转录导出: {args.export}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: 离线 CLI 冒烟（stub）**

```bash
cd "d:/开发/ensemble/app"
.venv/Scripts/python -m harness.runner \
  --scene scenes/餐厅.json \
  --characters characters/甲.json characters/乙.json \
  --models config/models.yaml --steps 6 --closing-at-block 5 \
  --export runs/out.md
cat runs/out.md
```

Expected: 打印 closed=True，`runs/out.md` 含两角色对白转录（stub 台词 + 收束）。

- [ ] **Step 6: 跑测试全绿**

```bash
cd "d:/开发/ensemble/app"
.venv/Scripts/python -m pytest -v
```

Expected: 全绿（schemas/loaders/visibility/bidding/memory/backends/prompters/m2/m3/engine/e2e）。

- [ ] **Step 7: Commit**

```bash
cd "d:/开发/ensemble"
git add -A && git commit -m "feat: 示例角色卡/场景 + models 档位 + CLI runner + M4 演示"
```

---

## Task 14: 收尾——契约测试探明 live 档 + 成本/限流探针（可选 gate）

**Files:**
- Modify: `app/src/harness/tests/test_backends.py`（追加 live 冒烟，无 key 自动 skip）
- Create: `app/src/harness/scripts/probe_models.py`（成本/限流探针，非测试）

**Interfaces:**
- Produces: `scripts/probe_models.py`：对 live 档发一次 think（关思考 json_object）与一次 speak，打印 token/耗时/是否 429，帮助 §16.2 的成本实测与重试参数微调。

- [ ] **Step 1: live 冒烟测试（skipif 无 key）**

`test_backends.py` 追加：

```python
import os
import pytest

pytestmark = pytest.mark.asyncio


@pytest.mark.skipif(not os.environ.get("DEEPSEEK_API_KEY"), reason="需 DEEPSEEK_API_KEY")
async def test_deepseek_live_think_json_roundtrip():
    from harness.backends.deepseek import DeepSeekBackend
    b = DeepSeekBackend(api_key=os.environ["DEEPSEEK_API_KEY"],
                        params={"thinking": "disabled", "max_tokens": 60})
    out = await b.complete_json([
        {"role": "system", "content": "输出 JSON。目标 schema 键：{\"urge\": 0~1}。"},
        {"role": "user", "content": "json 请求：一句话回应。"},
    ])
    assert "urge" in out
```

- [ ] **Step 2: 无 key 时确认被 skip**

```bash
cd "d:/开发/ensemble/app"
.venv/Scripts/python -m pytest src/harness/tests/test_backends.py -v
```

Expected: 无 key → `1 skipped`；有 key → 真连一次 DeepSeek 返回含 `urge`。

- [ ] **Step 3: 提交探针脚本**

`app/src/harness/scripts/probe_models.py`（不进测试；标 `if __name__ == "__main__"`）：

```python
"""成本/限流探针：live 档 think+speak 各一发，打印耗时/是否 429（配合 §16.2 调参）。"""
from __future__ import annotations

import asyncio
import os
import time

from harness.backends.deepseek import DeepSeekBackend
from harness.loaders import load_models


async def main() -> None:
    models = load_models("config/models.live.yaml")
    key = os.environ["DEEPSEEK_API_KEY"]
    for role in ("think", "speak"):
        cfg = models[role]
        b = DeepSeekBackend(key, model=cfg.model, params=dict(cfg.params))
        t0 = time.time()
        try:
            if role == "think":
                out = await b.complete_json([{"role": "user",
                    "content": "json 输出 {\"urge\": 0.1}"}])
            else:
                out = await b.complete_text([{"role": "user", "content": "说一句中文。"}])
            print(f"{role} ok in {time.time()-t0:.1f}s -> {str(out)[:80]}")
        except Exception as e:  # 429/超时均在此观察
            print(f"{role} failed {time.time()-t0:.1f}s -> {e!r}")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 4: Commit**

```bash
cd "d:/开发/ensemble"
git add -A && git commit -m "feat: live 冒烟契约测试 + 成本/限流探针脚本"
```

---

## 计划外补丁区（实施中按需修正，不改信息边界断言）

- LangGraph 1.2 若 `Command(goto=[Send(...)])` 不合用，改回经典写法：扇出节点函数返回 `list[Send]`，用 `add_conditional_edges("fan_out", route, ["think"])`（技术方案 §16.1 已给两种合法写法）。
- `SceneEngine.step/run_to_close` 里 `aget_state` 在每步后取 `closed`；若 `SqliteSaver.from_conn_string` 在本机报驱动问题，退回 `MemorySaver`（测试用），生产跑 `langgraph-checkpoint-sqlite` 文档步骤。
- Task 10 conftest 的 `PUBLIC_KEYS` 在加入 `decided/injected/closed/closing_at_block` 后需同步扩充（它们都是**公共调度字段**，非私有），实现 Task 11/12 时一并更新该白名单。

## Self-Review 记录

- 范围覆盖：设计文档/技术方案 §10 的 M1(→Task2–6)、M2(→Task7–10)、M3(→Task11)、M4(→Task12–13)；呈现层事件契约 → Task 10 events.py + Task 12 产出；版本锁定 → Global Constraints 与 Task 8/13。
- 占位符：无 "TBD/TODO"；代码步均给可运行内容。graph 的节点接线以闭包/`Command` 形态给出，允许按 1.2 官方文档微调签名（已在补丁区说明），不改变边界语义。
- 类型一致：`Message.is_visible_to`/`view_for`/`arbitrate`/`CharacterMemory`/`ModelBackend`/`SceneEngine` 在各任务间签名一致；`BidParams` 在 Task 5 定型后供 loaders(Task3 惰性)与 graph(Task11) 使用。
