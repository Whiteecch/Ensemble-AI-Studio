"""成本/限流探针：live 档 think+speak 各一发，打印耗时/是否 429（配合 §16.2 调参）。

运行（在 app/ 目录下，键经 DEEPSEEK_API_KEY 提供）：
    python -m harness.scripts.probe_models
无 DEEPSEEK_API_KEY 时打印跳过提示并以 0 退出，不联网不崩溃。
"""
from __future__ import annotations

import asyncio
import os
import time

from harness.backends.deepseek import DeepSeekBackend
from harness.loaders import load_models


async def _probe() -> None:
    models = load_models("config/models.live.yaml")
    for role in ("think", "speak"):
        cfg = models[role]
        b = DeepSeekBackend(os.environ["DEEPSEEK_API_KEY"], model=cfg.model,
                            params=dict(cfg.params))
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


def main() -> None:
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("跳过：未设 DEEPSEEK_API_KEY（无键环境下探针不联网）。")
        raise SystemExit(0)
    asyncio.run(_probe())


if __name__ == "__main__":
    main()
