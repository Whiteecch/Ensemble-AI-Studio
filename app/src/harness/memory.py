"""角色私有记忆文件夹（设计文档 §9）：独立目录、跨场景可持久、仅本人可读。

布局：runs/<scene>/<character>/
  state.jsonl                      私有状态流（情绪/目标/义务…；每条带 "turn"）
  impressions/<他人>.jsonl         我对 TA 的印象（Theory of Mind）——**真相源**，
                                   一行一条 {"turn": int, "text": str}
  impressions/<他人>.md            同一批印象的**人读渲染**（jsonl 一变就重写；
                                   老存档只有 .md 时照旧读得到——legacy 回落）
  transcript.visible.<me>.jsonl    我可见的转录（由 visibility 投影而来）

轮次感知（《界面与场景自由度》§6.1 截断式撤回）：状态与印象都记下写入时的**转录轮次**
（turn），撤回/改写某条推进时，引擎按轮次截断（truncate_after_turn）——被丢弃的那段
下文留下的私有痕迹必须一并消失，否则角色下一轮会带着「从没发生过的事」的内心开口。
"""
from __future__ import annotations

import json
from pathlib import Path

#: 单个他人的印象软上限（条数）：丢最旧（MVP 口径，与旧 .md 版一致）。
_IMPRESSION_MAX_LINES = 200


def _read_jsonl(path: Path) -> list[dict]:
    """逐行读 jsonl → dict 列表；**绝不抛**：坏行跳过，文件不存在 → []。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    rows: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:                 # 坏行（半截写入/手改）只跳过这一行
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8")


def _render_md(texts: list[str]) -> str:
    """印象条目 → 人读 md（一行一条，与旧 .md 版逐字节同构）。"""
    return "".join(f"{t}\n" for t in texts)


def _record_turn(row: dict) -> int:
    """一条记录的轮次；缺失/非法（老数据、坏值）按 0 计（= 最早，绝不会被误截掉）。"""
    value = row.get("turn", 0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


class CharacterMemory:
    def __init__(self, root: Path, character: str):
        self.folder = Path(root) / character
        self.character = character
        self._imp_dir = self.folder / "impressions"
        self.folder.mkdir(parents=True, exist_ok=True)
        self._imp_dir.mkdir(exist_ok=True)

    # ---- state ----
    def append_state(self, entry: dict) -> None:
        """追加一条私有状态；调用方（graph.think）在条目里带上当时的转录轮次 "turn"。"""
        with (self.folder / "state.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def load_state(self) -> list[dict]:
        return _read_jsonl(self.folder / "state.jsonl")

    def truncate_after_turn(self, turn: int) -> None:
        """丢弃 **turn > cut** 的私有痕迹：state.jsonl 截断 + 各印象 jsonl 过滤并重写 md。

        「撤回要回溯整段下文」（§6.1）：被撤销节点之后写下的状态与印象，都是建立在
        「那段下文发生过」之上的私有解读，必须一起消失——kind 上它们会经 next-think
        回喂（graph 的私密回喂）重新把丢弃的内容带回提示词。引擎传的 cut 是
        「该条消息的轮次 − 1」，因为块内 think 与该块产出的消息同轮（见 SceneEngine.retract）。

        只删不改：留下的条目一字不动（不做时间倒流，也不重算任何数值）。幂等、绝不抛
        ——撤回不该因 IO 失败中断。legacy（只有 .md、没有 jsonl）无从按轮次截断，原样保留。
        """
        cut = int(turn)
        self._truncate_state(cut)
        if not self._imp_dir.is_dir():
            return
        for path in sorted(self._imp_dir.glob("*.jsonl")):
            rows = [r for r in _read_jsonl(path) if _record_turn(r) <= cut]
            try:
                _write_jsonl(path, rows)          # 空也重写：md 必须跟着一起变空
                self._write_impression_md(path.stem, rows)
            except OSError:
                continue                          # 写不动就当没截（下次撤回再试）

    def _truncate_state(self, cut: int) -> None:
        """state.jsonl 只留 turn ≤ cut 的条目（文件不存在 → 什么都不做）。"""
        path = self.folder / "state.jsonl"
        if not path.exists():
            return
        try:
            rows = [r for r in _read_jsonl(path) if _record_turn(r) <= cut]
            _write_jsonl(path, rows)
        except OSError:
            pass

    def clear(self) -> None:
        """清空本角色的**私有思考上下文**：截断 state.jsonl + 删掉全部印象文件。

        「重置场景」要清的是这一场的对话与思考上下文（§3.4/§5）：think/speak 会把自己
        的 state.jsonl 尾部与 impressions（jsonl 为准、md 是人读渲染）回喂进下一次提示词
        （见 graph 的私密回喂），只清共享态不清这些，重置后角色仍带着上一场的私有状态
        开口。transcript.visible.*.jsonl（我可见的转录）是**历史存档**不是回喂源，保留不动。

        幂等、绝不抛（文件不存在/删不掉都当无事发生——重置不该因 IO 失败而中断）。
        """
        try:
            (self.folder / "state.jsonl").unlink()
        except OSError:
            pass
        if self._imp_dir.is_dir():
            # jsonl（真相源）与 md（渲染）都是回喂源的一部分，一起删干净。
            for pattern in ("*.md", "*.jsonl"):
                for p in self._imp_dir.glob(pattern):
                    try:
                        p.unlink()
                    except OSError:
                        pass

    # ---- impressions ----
    def append_impression(self, other: str, line: str, turn: int = 0,
                          max_lines: int = _IMPRESSION_MAX_LINES) -> None:
        """记一条印象（jsonl 追加 + md 重写）；超过 max_lines 丢最旧。

        turn = 写下这条印象时的转录轮次（撤回要按它截断，见 truncate_after_turn）。
        """
        path = self._imp_dir / f"{other}.jsonl"
        rows = _read_jsonl(path)
        rows.append({"turn": int(turn), "text": str(line)})
        if max_lines > 0 and len(rows) > max_lines:
            rows = rows[-max_lines:]
        _write_jsonl(path, rows)
        self._write_impression_md(other, rows)

    def _write_impression_md(self, other: str, rows: list[dict]) -> None:
        """jsonl 一变就重写人读 md（内容 = 该批印象的逐行文本）。"""
        texts = [str(r.get("text") or "").strip() for r in rows]
        (self._imp_dir / f"{other}.md").write_text(
            _render_md([t for t in texts if t]), encoding="utf-8")

    def read_impressions(self) -> dict[str, str]:
        """{他人: 多行文本}（回喂给本人提示词的那一份）。

        以 jsonl 为准（坏行跳过）；只有 legacy .md 的老存档照旧整份读回（兼容路径）。
        md 与 jsonl 内容同源，故调用方拿到的文本与磁盘上人读的那份一致。
        """
        out: dict[str, str] = {}
        jsonl_stems: set[str] = set()
        if not self._imp_dir.is_dir():
            return out
        for p in sorted(self._imp_dir.glob("*.jsonl")):
            jsonl_stems.add(p.stem)
            texts = [str(r.get("text") or "").strip() for r in _read_jsonl(p)]
            out[p.stem] = _render_md([t for t in texts if t])
        for p in sorted(self._imp_dir.glob("*.md")):
            if p.stem in jsonl_stems:          # 有 jsonl 的以 jsonl 为准（md 只是渲染）
                continue
            try:
                out[p.stem] = p.read_text(encoding="utf-8")
            except OSError:
                continue
        return out

    # ---- visible transcript ----
    def append_visible(self, msg_json: dict) -> None:
        name = f"transcript.visible.{self.character}.jsonl"
        with (self.folder / name).open("a", encoding="utf-8") as f:
            f.write(json.dumps(msg_json, ensure_ascii=False) + "\n")

    def read_visible(self) -> list[dict]:
        name = f"transcript.visible.{self.character}.jsonl"
        return _read_jsonl(self.folder / name)
