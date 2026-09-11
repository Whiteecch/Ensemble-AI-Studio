"""桌面端支持回归：SceneEngine.metrics/say/speak_as_human（offline stub）。"""
import asyncio
import json
from pathlib import Path

from harness.engine import SceneEngine
from harness.tests.helpers import assert_public_only


def _write(tmp_path: Path):
    (tmp_path / "餐厅.json").write_text(json.dumps({
        "name": "餐厅", "participants": ["甲", "乙"],
        "hard_boundary": {"type": "time", "value": "22:00", "desc": "打烊"}},
        ensure_ascii=False), encoding="utf-8")
    (tmp_path / "甲.json").write_text(json.dumps({"name": "甲",
        "personality": {"描述": "冷静"}}, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "乙.json").write_text(json.dumps({"name": "乙",
        "personality": {"描述": "锐利"}}, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "models.yaml").write_text(
        "think:\n  backend: stub\n  model: stub\n  params: {}\n"
        "speak:\n  backend: stub\n  model: stub\n  params: {}\n", encoding="utf-8")
    return (tmp_path / "餐厅.json", tmp_path / "甲.json",
            tmp_path / "乙.json", tmp_path / "models.yaml")


def test_engine_metrics_reports_usage_and_mirrors_after_close(tmp_path: Path):
    """metrics() 同步非阻塞：只读镜像。stub 跑几块收束后 think/speak 计数皆 >0、
    词元 ≥0、uptime_s ≥0，messages/blocks 镜像与到达的 closed 末态一致。"""
    scene_p, a_p, b_p, models_p = _write(tmp_path)
    eng = SceneEngine(scene_p, [a_p, b_p], models_p, run_root=tmp_path / "runs",
                      closing_at_block=4)

    async def _drive():
        await eng.open_scene()
        guard = 0
        while not eng.closed:
            await eng.step(1)
            guard += 1
            assert guard < 80, "should close within closing_at_block=4"
        m = eng.metrics()                       # sync：绝不经 async graph
        assert m["uptime_s"] >= 0.0
        assert m["think"]["stub"]["calls"] > 0
        assert m["speak"]["stub"]["calls"] > 0
        assert m["think"]["stub"]["prompt_tokens"] >= 0
        assert m["think"]["stub"]["completion_tokens"] >= 0
        assert m["speak"]["stub"]["prompt_tokens"] >= 0
        assert m["speak"]["stub"]["completion_tokens"] >= 0
        # 镜像与最后一块到达的 closed 末态一致（测试里可比，metrics 自身不查图）
        st = await eng._snapshot()
        assert m["messages"] == len(st.get("messages", []))
        assert m["blocks"] == st.get("blocks", 0)
        assert st.get("closed") is True

    asyncio.run(_drive())


def test_engine_say_human_inserts_message_and_step_reacts(tmp_path: Path):
    """say()：追加 speaker_type=human、speaker=你 的消息（区别于导演 inject）；
    下一步 step 角色把其当作最新块回应（messages 增长），且共享态仍守公共白名单。"""
    scene_p, a_p, b_p, models_p = _write(tmp_path)
    eng = SceneEngine(scene_p, [a_p, b_p], models_p, run_root=tmp_path / "runs",
                      closing_at_block=99)

    async def _drive():
        await eng.open_scene()
        await eng.say("我在外面喊了一声。")
        msgs = await eng.messages()
        human = [m for m in msgs if m.get("speaker_type") == "human"]
        assert human, "say 后应有 human 消息"
        assert human[-1]["speaker"] == "你"
        assert human[-1]["content"] == "我在外面喊了一声。"
        n0 = len(msgs)
        assert human[-1]["id"] == msgs[-1]["id"]          # 人类消息是当前尾部

        # §7.4 数值化后「回应」要等唤醒/沉默压力演化到位（冷启动首块常是静默的
        # 聆听块，随后自然有人开口）。步进 ≤5 块直到出现角色发言。
        for _ in range(5):
            await eng.step(1)
            msgs2 = await eng.messages()
            reacted = [m for m in msgs2[n0:]
                       if m.get("speaker_type") == "character"]
            if reacted:
                break
        assert reacted, "人类插话后应有角色发言（数值 bid 自然涌现）"
        st = await eng._snapshot()
        assert_public_only(st)

    asyncio.run(_drive())


def test_engine_speak_as_human_backcompat_delegates_to_say(tmp_path: Path):
    """speak_as_human 不再抛 NotImplementedError：后向兼容别名，等价 say(content)。"""
    scene_p, a_p, b_p, models_p = _write(tmp_path)
    eng = SceneEngine(scene_p, [a_p, b_p], models_p, run_root=tmp_path / "runs",
                      closing_at_block=99)

    async def _drive():
        await eng.open_scene()
        await eng.speak_as_human("旧接口还在。")
        msgs = await eng.messages()
        assert any(m.get("speaker") == "你" and m.get("speaker_type") == "human"
                   and m["content"] == "旧接口还在。" for m in msgs)

    asyncio.run(_drive())


def test_engine_aclose_closes_backends_even_with_foreign_close(tmp_path: Path):
    """aclose() 对每个后端调用 close()；stub no-op 不炸，既有 saver 关闭语义保留。"""
    scene_p, a_p, b_p, models_p = _write(tmp_path)
    eng = SceneEngine(scene_p, [a_p, b_p], models_p, run_root=tmp_path / "runs",
                      closing_at_block=99)
    closed = {"think": False, "speak": False}

    class _Closey:
        """鸭子后端：挂 close() 的探针（计数是否被引擎 aclose 到）。"""
        def __init__(self, tag):
            self.tag = tag
            self._calls = self._prompt = self._completion = 0

        @property
        def calls(self):
            return self._calls

        @property
        def prompt_tokens(self):
            return self._prompt

        @property
        def completion_tokens(self):
            return self._completion

        name = "closey"

        async def complete_json(self, messages):
            return {"aroused": 0.5, "obligation_fulfilled": [],
                    "goal_progress": 0.0, "urge": 0.7}

        async def complete_text(self, messages):
            return "。"
        async def close(self):
            closed[self.tag] = True

    eng.think_backend = _Closey("think")
    eng.speak_backend = _Closey("speak")

    async def _drive():
        await eng.open_scene()
        await eng.step(1)
        await eng.aclose()

    asyncio.run(_drive())
    assert closed == {"think": True, "speak": True}
