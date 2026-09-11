"""角色卡/场景/模型配置装载：文件即 system prompt + 参数（设计文档 §6）。

场景文件名是**用户可自定义**的独立设置（不改场景名也改文件名，反之亦然，§3.4）：
落盘入口一律先过 scene_filename_error / scene_path_for 这道守门，拒绝路径分隔符、
`..`、前导点、Windows 非法字符与保留设备名——文件名绝不把文件写出场景库之外。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .schemas import CharacterCard, Scene, SceneCastMember


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


def _list_json_paths(directory: Path) -> list[Path]:
    """目录下全部 *.json（含一层子目录），按路径排序；目录不存在 → []。

    只做列举、不解析：坏 json 不该让"发现"这一步崩掉（错误留到 load_* 时暴露），
    这样往 app/characters/、app/scenes/ 里丢一个文件就能被看见（可插拔素材库）。
    """
    directory = Path(directory)
    if not directory.is_dir():
        return []
    found = [p for pattern in ("*.json", "*/*.json")
             for p in directory.glob(pattern) if p.is_file()]
    return sorted(found)


def list_character_paths(directory: Path) -> list[Path]:
    """角色卡目录扫描：app/characters/ 下所有 .json（排序，非 json 忽略）。"""
    return _list_json_paths(directory)


#: 场景运行的旁挂存档后缀（scenestore 写出 `<场景>.json.runtime.json`）。它不是场景
#: 文件，必须从"场景库"列表里排除，否则每存一次就在界面上多出一个打不开的幽灵条目。
RUNTIME_SUFFIX = ".runtime.json"


def list_scene_paths(directory: Path) -> list[Path]:
    """场景目录扫描：app/scenes/ 下所有 .json（排序，非 json 忽略）。

    排除 `*.runtime.json` 旁挂存档（见 RUNTIME_SUFFIX）——那是运行快照，不是场景。
    """
    return [p for p in _list_json_paths(directory)
            if not p.name.endswith(RUNTIME_SUFFIX)]


def _dump_json(data: Any, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def save_character_card(card: CharacterCard, path: Path) -> Path:
    """写出角色卡（UTF-8、不转义、缩进 2）——供 GUI 编辑器保存，落盘即可被扫描到。"""
    return _dump_json(card.model_dump(), path)


def save_scene(scene: Scene, path: Path) -> Path:
    """写出场景（UTF-8、不转义、缩进 2）。"""
    return _dump_json(scene.model_dump(), path)


def scene_with_cast(scene: Scene, cast: list[str]) -> Scene:
    """把**所选角色卡**播种成场景演员表（返回副本，原 scene 一字不改）。

    《界面与场景自由度》§3.1 起，`Scene.characters` 是这一场的**唯一权威**，故本函数
    只用于「场景**还没存过**阵容」（characters 为空：新建场景 / 老场景文件没写名单）
    的那一次播种——引擎以所选卡把它填上（见 SceneEngine.__init__ 的 cast_from_cards）。
    场景存过名单时**绝不**调用它：否则每次打开都会把上次存下的人事覆盖掉（这正是根因 1）。

    characters 换成 cast（**按给定顺序**＝用户选卡顺序），一律从零起算入场记录
    （entered_round=0／entered_at 空）——选卡即本场开场时的在场名单；谁何时进场是运行期
    行为（§3.3），由 S4 的进场/离场事件写入，并由 save_scene_file 写回场景文件。
    """
    cast = list(cast)
    characters = [SceneCastMember(name=name, entered_round=0) for name in cast]
    return scene.model_copy(update={"characters": characters})


# ------------------------------------------------------------ 场景文件名守门 --
#: Windows 下不允许出现在文件名里的字符（保守起见 Linux/macOS 也一并禁）。
#: 与 gui/library.py::filename_error 同一套规则——loaders 不反向依赖 gui，故此处自带一份。
_ILLEGAL_NAME_CHARS = frozenset(':*?"<>|')
#: Windows 保留设备名：CON.json 之类无法正常读写。
_RESERVED_STEMS = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)] + [f"LPT{i}" for i in range(1, 10)])


def scene_filename_error(filename: str) -> str:
    """场景文件名能否安全落盘——空串 = 可以，否则中文原因（可直接给用户看）。

    场景文件名由用户自定义（与场景名无关，§3.4），因此必须当成**路径输入**来守：
    拒绝空名、首尾空白、前导 `.`（隐藏文件）、`..`、路径分隔符、Windows 非法字符、
    控制字符、尾随 `.`、系统保留设备名，并要求 `.json` 后缀（落盘即可被
    list_scene_paths 扫描到）。
    """
    name = filename or ""
    if not name.strip():
        return "文件名不能为空。"
    if name != name.strip():
        return "文件名首尾不能有空白。"
    if name.startswith("."):
        return "文件名不能以「.」开头（会变成隐藏文件）。"
    if ".." in name:
        return "文件名里不能包含「..」。"
    if "/" in name or "\\" in name:
        return "文件名里不能包含路径分隔符（/ 或 \\）。"
    bad = sorted({c for c in name if c in _ILLEGAL_NAME_CHARS})
    if bad:
        return "文件名里不能包含这些字符：" + " ".join(bad)
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        return "文件名里不能包含控制字符。"
    if name.endswith("."):
        return "文件名不能以「.」结尾。"
    stem = name.split(".")[0]
    if stem.upper() in _RESERVED_STEMS:
        return f"文件名不能是系统保留名（{stem}）。"
    if not name.lower().endswith(".json"):
        return "文件名必须以 .json 结尾。"
    return ""


def scene_path_for(dir: Path, filename: str) -> Path:
    """场景文件名 → 场景文件路径；不安全的名字抛 ValueError（文案可直接给用户看）。

    场景库落盘的唯一换算口：界面可以展示任意场景名，文件名一律经这里变成
    「场景库目录 / 文件名」，绝不拿用户输入直接拼路径。
    """
    err = scene_filename_error(filename)
    if err:
        raise ValueError(err)
    return Path(dir) / filename


def load_bid_params(path: Path) -> Any:
    """惰性导入 bidding，避免本文件在 Task 5 之前依赖它。"""
    from .bidding import BidParams
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return BidParams(**raw)
