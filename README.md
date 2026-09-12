# Ensemble

**English** | [简体中文](README.zh-CN.md)

Ensemble is a headless multi-agent engine for roleplay scenes, plus a PySide6 desktop app. A cast of characters with private state, private memory and personality weights takes turns speaking in a shared scene, while the scene itself is a separate agent that perceives everything and pushes the story forward.

What makes it different:

- **The information boundary is data access control.** A character's prompt is assembled from a projection of the transcript it is allowed to see. It never receives text it shouldn't know, instead of being asked to pretend not to know.
- **Who speaks is computed, not chosen.** Each character gets a numeric bid from its own dynamic state and personality weights; a pending-reply obligation doubles every round you stay silent. No director, no LLM self-report deciding the order.
- **The scene is a first-class agent.** It perceives everything, has its own trigger algorithm (repetition / stall / approaching boundary), advances the story in one or two lines, can rewrite its own description through tool directives, and is fully reversible.
- **Hooks.** Conditions judged by the scene agent, effects in three kinds (context / cast / scene), explicit or implicit, with a deterministic engine-side gate for clock conditions.
- **Pluggable, corpus-driven characters.** Markdown authoring templates that an LLM fills from your source material, plus a parser/importer. Per-character corpus (style, thinking, quirks, sample lines) drives the voice.
- **Free casting at runtime.** Add, remove or mute anyone at any time; the scheduler and the UI follow, and a scene may run with an empty cast.

## Demo

The repository ships one scene, `app/scenes/贝克街221B.json`, and its two character cards. It uses Conan Doyle's Sherlock Holmes and Dr. Watson, which are in the public domain.

What the scene does:

- Starts at **21:00** in the sitting room at 221B Baker Street, with a hard boundary at **23:00** ("就寝", bedtime). When the virtual clock reaches the boundary, the scene closes.
- Casts 福尔摩斯 and 华生, both present from round 0.
- Hook `h1` (context, explicit): when someone brings up an unsolved case, a line is written into the transcript — Holmes sets down his pipe and sits up.
- Hook `h2` (character, explicit): when the virtual clock reaches 22:30, 华生 leaves.
- The scene description is **not** mutable (`description_mutable: false`), so the scene agent can nudge the story but cannot rewrite its own room.

Run the offline CLI demo:

```bash
cd app
python -m harness.runner --demo --stub
```

`--demo` streams line by line and forces the cast to alternate (`demo_alternate`), using the looser `config/bid.demo.yaml` thresholds, so you see a conversation without a model. It creates a fresh `runs/demo-<id>/` directory per run. Press Enter to advance, `q` to stop early.

Run the desktop app:

```bash
cd app
python -m harness.gui.app --stub     # force offline; drop --stub to use a real model
```

The app opens on an empty state page. Pick the scene from the **场景** menu or the **打开场景** button. It drives the same engine on a background thread, with live bids, the think log, the scene-narration panel and a virtual clock.

## Quickstart

**Prerequisites.** Python 3.10 or newer. Windows, macOS or Linux. PySide6 for the desktop app.

```bash
git clone https://github.com/Whiteecch/Ensemble-AI-Studio.git
cd Ensemble-AI-Studio/app
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -e ".[dev]"     # engine + CLI + tests
pip install PySide6         # desktop app (or: pip install -e ".[dev,gui]")
```

Installing also puts three commands on your PATH. Each is a shorthand for the `python -m` form used below:

```bash
ensemble-gui                # desktop app        = python -m harness.gui.app
ensemble-demo --stub        # offline CLI demo   = python -m harness.runner --demo --stub
ensemble-import your.md     # template importer  = python -m harness.tools.import_cards your.md
```

**Run the desktop app:**

```bash
python -m harness.gui.app
```

**Run the offline CLI demo:**

```bash
python -m harness.runner --demo --stub
```

**Run the tests:**

```bash
pytest -q
```

950 tests, about one minute. The suite runs without network access.

**Real models.** The engine talks to DeepSeek by default. Supply the key either way:

- Environment variable `DEEPSEEK_API_KEY`, or
- the desktop app's **设置 → 模型 api 配置…** dialog (base URL, key, model name), which writes to the user settings file at `%APPDATA%\Ensemble-AI-Studio\settings.json` on Windows (`~/.config/Ensemble-AI-Studio/settings.json` elsewhere).

Resolution order is `--stub` > key saved in settings > `DEEPSEEK_API_KEY` > offline stub. `app/config/models.live.yaml` points the think / speak / narrate stages at `deepseek-v4-flash`. Without a key everything falls back to the offline stub backend and never crashes.

Two Windows launchers are included for double-click use: `app/start-demo.bat` (CLI demo) and `app/启动应用.bat` (desktop app). Both read a local `.env.deepseek` if the environment variable is not set.

## How it works

The project is a Python package whose distribution name is `ensemble` and whose import package is `harness` (these are deliberately separate — see `app/pyproject.toml`).

Each block runs three loops plus a world layer:

```
                      WORLD  (block clock, silence gate, closing)
                        │
      ┌─────────────────┼──────────────────────────────────┐
      │                 │                                  │
 ┌────▼─────┐     ┌─────▼──────┐     ┌──────────────┐      │
 │ PERCEIVE │     │ DELIBERATE │     │     ACT      │      │
 │  think   │────▶│    bid     │────▶│    speak     │──────┘  next block
 │ per cast │     │ pure code  │     │  one line    │
 └──────────┘     └────────────┘     └──────────────┘
   one view          numeric           winner
   per agent          bid              speaks
      ▲
 ┌────┴─────────────────────────────────────────────┐
 │ SCENE agent (narrator): sees all messages,       │
 │ nudges, fires hooks, may rewrite its own fields  │
 └──────────────────────────────────────────────────┘
```

**Information boundary.** A character's prompt is built from `visibility.view_for(messages, character, space, since_round)`. That keeps only messages whose `in_scene` equals this scene, whose `knows` list admits the character (or is `None`, meaning everyone present), and whose `turn` is at or after the character's entry round — a character who just walked in has no idea what was said before. `Message.knows` is the cognitive axis and `in_scene` is the presence axis; both are fields on the message, not instructions in a prompt. The LangGraph shared state is locked to a whitelist (`messages`, `urges`, `current_speaker`, `turn`, `silent_streak`, `blocks`, `decided`, `injected`, `closed`, `closing_at_block`, `retracted`) and a test helper asserts no key outside it ever appears. Each character's think result goes to a read-only side channel for the human UI and to that character's own memory folder under `runs/<scene>/<character>/` — never into shared state.

**Who speaks.** After the fan-out, each character's dynamic state is folded into `Dynamics`, and `dynamics.Dynamics.bid()` computes:

```
bid = w1·relevance
    + w2·arousal
    + w3·adjacency
    + w4·goal_pressure
    − w6·inhibition
    + w7·scene_pressure
    + urge_gain · self_urge                             # the character's own -1..2 urge
    + pending_reply                                     # owes an answer; doubles per round
    + silence_gain · turns_since_spoke · (0.4 + 0.6·w5) # talkativeness folded into silence
    − recency_penalty                                   # if this character spoke last
    + stable_noise(round, name)                         # CRC32 of "(round:name)"
```

`bidding.arbitrate()` then picks one of three outcomes: the incumbent keeps the floor, a challenger takes it only if it beats the incumbent by `interruption_threshold`, or the block is silent when nobody clears `speak_threshold`. The `pending_reply` term is the load-bearing one: being named sets it to 1.2 immediately, and it doubles every round that character stays silent (capped at 64), so an addressed character answers within a round or two instead of being outlasted. It is not multiplied by any personality weight — a hard obligation should not be cancelled out by temperament. Every term is deterministic; the tie-breaking noise is a CRC hash of `(round, name)`, not the process-random `hash()`, so the same scene at the same round always produces the same result.

**The scene agent.** The scene has its own trigger algorithm in `scenarist.py` rather than waiting on a character:

```
score = 2.0·repeat + 1.2·stall + 0.5·silence + 1.0·boundary
```

`repeat` is the pairwise mean text similarity of the last four character lines (`textsim.ratio`), `stall` is blocks since the last narration, `silence` is consecutive silent blocks, and `boundary` rises as the virtual clock approaches the hard boundary. A cooldown (3 blocks by default) sits between narrations. Push frequency is a user setting: `effective_params` scales the trigger line and the cooldown together, with the activity level also written into the prompt as a tone hint. The scene's output is trimmed to at most two lines / 120 characters — it advances the situation, it does not write paragraphs. If `description_mutable` is set, the scene may emit `[[TOOL:...]]` directives to change its own name, description or background; the engine applies them, then feeds the new description back for exactly one extra think round.

**Reversibility.** `retract(id)` invalidates that message and every later message in the same scene, and truncates the think log and each character's private memory (`state.jsonl`, `impressions/`) by turn — everything that feeds the model agrees the discarded passage never happened. `edit_narration(id, text)` is retract plus append, rather than editing in place, so "what the model saw at the time" stays truthful. The world clock and the narration cooldown are deliberately not rewound.

**Hooks** are pure data (`hooks.Hook`): a condition in natural language plus one of three effect kinds — `context` writes text into the transcript, `character` performs `add` / `remove` / `mute_turns` / `mute` / `unmute`, and `scene` patches a whitelisted scene field (`name`, `description`, `background`, `plot_direction`, `date`; file names and paths are never writable). A hook is either explicit (it produces a visible line) or implicit (state changes, no line — it only appears in the internal event stream). The scene agent evaluates every condition each block and reports the ones that hold as `[[HOOK:<id>]]` lines, which the engine strips, validates and executes, de-duplicated within the block. Because LLM judgment of clock conditions proved unreliable, clock conditions get a deterministic gate: `scenarist.time_condition_target()` recognizes a condition that explicitly names a time, and the engine refuses to fire it before that time even if the scene reports it fired.

For depth, see `docs/`:

**Knowledge library.** A character's knowledge persists across scenes. A library is a set of entries (key, title, one-line summary, body); bodies link to each other with `[[key]]`, which makes the library a *graph* rather than a list. The index — titles and summaries — always sits in the prompt as the way in; bodies are fetched on demand by the character itself, through native tool calls (`read_entry` / `remember` / `revise`) issued during the think step, and it may follow links for several hops. A scene runs against a **copy** of the library, so nothing a character learns leaks into who it is until you say so: at the end of the scene each character's gains are offered to you, and you keep or discard them. Merging is newest-wins, and the superseded entry is archived rather than deleted. A library may also be **subscribed** to (a live reference — the source changes, every subscriber sees it at once) or **pinned** (copied into your own library with the reference cut). The old `knowledge_boundary` field on the character card is retired; its lines migrate into the library on first read.

**Relationships.** Every character keeps a table of relations, one row per person: name, gender, closeness (−100..100), a description, and a mode of interaction. The whole table is always in context, because "who is standing in front of me" is needed every time a character opens its mouth — unlike the knowledge index, which is consulted on demand. A character may edit a row through an `update_relation` tool, but only rarely: the tool description, a per-pair per-scene cap and a single-step magnitude cap all encode the same rule, that only a change of the relationship's *nature* counts — a confession, a break, a rescue, a betrayal, not a mood. Closeness enters the bid as one more weighted term.

**Streamed speech.** Lines appear character by character. The display rate is deliberately decoupled from the network: pieces are buffered as they arrive, and typing starts only once the whole line is in, so the pacing is the app's and not the connection's. The line under construction is rendered by the same function as a finished one — there is no separate "typing" style, and nothing shifts when it lands.

**Skipping time.** When nobody has had anything to say for several blocks *and* a sustained activity is under way (sleeping, homework, waiting for dawn), the scene may advance the clock and announce that the activity finished. Four gates keep it in check: a deterministic precondition evaluated engine-side (the model may propose, the engine disposes), a cap on how far a single jump may go, a cooldown, and a per-scene quota.

For depth, see `docs/`:

- `docs/design.md` — the design basis: principles, the three loops, schemas, the bidding model, the information boundary.
- `docs/technical-scheme.md` — how the design maps onto a LangGraph implementation.
- `docs/ui-and-scene-freedom.md` — runtime casting, the narration controls, truncating retraction.
- `docs/information-library.md` — the knowledge library: the graph, the tool loop, scene copies, settlement, subscriptions.
- `docs/relations-and-scene-pacing.md` — streamed speech, time skips, cast-change reasons, the relationship system.
- `docs/implementation-plan.md` — the milestone-by-milestone build log.
- `docs/packaging.md` — the packaging and distribution plan (not implemented yet).
- `docs/操作手册.md` — the end-user manual, from install to a finished scene.

These documents are currently Chinese only.

## Authoring characters and scenes

Characters, scenes and knowledge libraries are authored as Markdown, filled in by any LLM, then parsed into the library. Three template types ship in `templates/`: `角色卡模板.md`, `场景卡模板.md` and `信息库模板.md` (a library: its name, whether it is a character's or a shared world library, and any number of entries with a title, a one-line summary and a body that may link to other entries). Three steps:

**1. Hand the template and your material to a model.** Copy the full text of `templates/角色卡模板.md` (character) or `templates/场景卡模板.md` (scene), paste it with your source material, and add one instruction:

> 请严格按这份模板的填写说明，依据下面的小说素材填写。保留所有【】字段名与结构，不要输出 JSON，不要编造素材里没有的内容。

**2. Check the result.** Field names kept verbatim, sample lines copied as original quotes rather than paraphrased, weights as 0–1 decimals, and every name under 【出场角色】 already existing as a character card.

**3. Import it.**

```bash
cd app
python -m harness.tools.import_cards ../templates/your-card.md   # character or scene, auto-detected
python -m harness.tools.import_cards your-card.md --dry-run      # parse only, write nothing
python -m harness.tools.import_cards your-scene.md --name 深夜小馆.json
python -m harness.tools.import_cards your-card.md --force        # overwrite an existing file
```

The importer reports per file: the detected kind, the name, where it was written, which fields were left empty and defaulted, and any warnings. On failure it prints a field-level message instead of a traceback — `file.md → 第 12 行附近 字段【唤醒权重 w2】：...`. The same flow is available inside the app under the **场景** / **角色** menu → library dialog → **从模板导入…**.

A filled character card looks like this (this fragment matches the shipped `app/characters/华生.json`):

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

The 【语言风格】【思维方式】【口头禅】【台词样例】 block is the character's `corpus`. It is rendered into every prompt as a few-shot anchor for voice and reasoning. The sample lines are explicitly not content to reuse — the prompt tells the model to imitate how the character speaks, not to repeat what they said.

## Project layout

```
ensemble/
├── LICENSE
├── README.md
├── README.zh-CN.md
├── .gitignore
├── docs/                              design, build and usage documents
│   ├── design.md
│   ├── technical-scheme.md
│   ├── ui-and-scene-freedom.md
│   ├── implementation-plan.md
│   └── 操作手册.md                      step-by-step user manual
├── templates/                         Markdown authoring templates
│   ├── 角色卡模板.md                    character card template
│   ├── 场景卡模板.md                    scene card template
│   ├── 使用说明.md                      three-step authoring guide
│   ├── 示例素材.md                      sample source material
│   ├── _filled_示例_沈砚.md              filled character card example
│   └── _filled_示例_小馆.md              filled scene card example
└── app/                               project root (pyproject.toml lives here)
    ├── .gitignore
    ├── pyproject.toml                 distribution "ensemble", import package "harness"
    ├── start-demo.bat                 Windows CLI demo launcher
    ├── 启动应用.bat                     Windows desktop app launcher
    ├── characters/                    character card library
    │   ├── 福尔摩斯.json
    │   └── 华生.json
    ├── scenes/                        scene library (plus *.runtime.json sidecars at runtime)
    │   └── 贝克街221B.json
    ├── config/
    │   ├── models.yaml                offline stub backends
    │   ├── models.live.yaml           DeepSeek backends
    │   ├── bid.yaml                   bidding thresholds
    │   └── bid.demo.yaml              looser thresholds for the streaming demo
    └── src/harness/                   the engine
        ├── engine.py                  high-level engine: cast, hooks, scene, save/resume, retract
        ├── graph.py                   LangGraph nodes, per-listener fan-out, privacy whitelist
        ├── dynamics.py                per-character dynamic state and the numeric bid
        ├── bidding.py                 block-level arbitration
        ├── scenarist.py               scene trigger algorithm, tool directives, time gate
        ├── hooks.py                   hook data model and pure helpers
        ├── visibility.py              the information boundary projection
        ├── memory.py                  per-character private memory
        ├── prompters.py               think / speak / narrate prompts
        ├── schemas.py                 Scene, CharacterCard, Message, ThinkResult
        ├── loaders.py                 reading and writing cards and scenes
        ├── scenestore.py              runtime sidecar saves
        ├── sceneclock.py              virtual clock
        ├── template_import.py         Markdown template parser
        ├── textsim.py                 near-duplicate detection
        ├── i18n.py                    language catalogs and LLM language directive
        ├── events.py                  event contract
        ├── runner.py                  CLI
        ├── backends/
        │   ├── base.py                backend seam
        │   ├── stub.py                offline backend
        │   └── deepseek.py            DeepSeek backend
        ├── gui/
        │   ├── app.py                 entry point
        │   ├── main_window.py         three-pane window
        │   ├── worker.py              QThread hosting the single asyncio loop
        │   ├── library.py             scene / character editors and dialogs
        │   ├── settings.py            user settings persistence
        │   └── theme.py               color schemes
        ├── tools/
        │   └── import_cards.py        CLI for the template importer
        ├── scripts/
        │   └── probe_models.py        one-off model probe
        └── tests/                     46 test modules, 950 tests
```

## Status

This is a working engine and a working desktop app, not a demo skeleton. 950 tests pass in about a minute with no network access, covering the engine, the graph, dynamics, hooks, visibility, the scene agent, the importers, and the Qt widgets.

Known limitations, verifiable in the code:

- **Dialogue circles were removed.** The presence axis is now the scene itself: a message either belongs to this scene or it does not. Old scene files with a `circles` key are migrated by silently dropping it (`schemas.Scene._migrate_legacy`).
- **Only Chinese is complete.** Seven UI languages are listed (`zh-Hans`, `zh-Hant`, `en`, `fr`, `de`, `ja`, `ko`) but only `zh-Hans` has a full catalog; the others fall back key by key. The documents under `docs/` have no English translation either.
- **Prompts grow unbounded.** Each character's view is the whole unretracted transcript. Long sessions get slower and more expensive; there is no summarization or sliding window. The only cap is the read-only think log for the UI (`_THINK_LOG_CAP = 2000`), which characters never see.
- **One event loop per engine.** A `SceneEngine` using the SQLite checkpointer is bound to the event loop it first built its graph on. The Qt app keeps that discipline by owning a single asyncio loop inside a worker thread; reusing one engine across two loops is unsupported.
- **Cost guards, not budget control.** The desktop app auto-pauses after 500 blocks of unattended autoplay and asks you to continue. There is no token accounting or spending limit.
- **DeepSeek is the only wired backend.** `backends/base.py` is the seam for adding others, and `backends/stub.py` shows the interface, but no other provider ships today.
- **Single-process, single-machine.** No server, no multi-user, no networking beyond the model API.

## License

MIT. See [LICENSE](LICENSE).

Contributions are welcome — issues and pull requests alike. If you change behavior, the test suite is the contract; `pytest -q` should stay green.
