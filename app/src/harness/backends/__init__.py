"""按名字取后端。DeepSeek 需 api_key，用 make_deepseek() 构造。"""
from __future__ import annotations

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


def make_deepseek(api_key: str, model_config, base_url: str | None = None) -> DeepSeekBackend:
    """构造 DeepSeek 后端；base_url 非空才覆盖默认端点（None/"" 一字不改走默认）。

    端点来自用户设置（设置 →「模型 api 配置」的 url，§2.1①），由 SceneEngine 透传；
    model 名则取 model_config（引擎侧已按需套上设置里的模型覆盖）。
    """
    kwargs = {"base_url": base_url} if base_url else {}
    return DeepSeekBackend(api_key=api_key, model=model_config.model,
                           params=dict(model_config.params), **kwargs)
