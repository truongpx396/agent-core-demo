"""A process may serve several domains (GRAPH_PATTERNS.md pattern 49's domain tag, then pattern 96): which one a turn runs on is
`bind_domain`, read by `init_graph_async` (the graph) and `upsert_session` (the `chat_sessions.domain` stamp).

Hermetic: no checkpointer is opened (the `Checkpointing` port and `build_graph` are replaced), and `upsert_session` only needs
`app.agent.runtime.sessions` mocked out.
"""
import asyncio

import pytest

from app.agent.runtime import core as runtime
from app.ports.outbound import checkpointing as checkpointing_port
from tests.conftest import TEST_CTX


@pytest.fixture(autouse=True)
def fresh_graphs(monkeypatch):
    monkeypatch.setattr(runtime, "_graphs", {})
    monkeypatch.setattr(runtime, "_saver", None)
    monkeypatch.setattr(runtime, "_graph_lock", asyncio.Lock())


class _Saver:
    pass


class _Checkpointing:
    def __init__(self):
        self.opened = 0

    async def open(self):
        self.opened += 1
        return _Saver()


@pytest.fixture
def opened(monkeypatch):
    """The fake checkpointer, and `build_graph` recording (manifest name, saver) for each graph it builds."""
    fake = _Checkpointing()
    built = []

    def build(manifest, plugin, checkpointer=None):
        graph = (manifest.name, checkpointer)
        built.append(graph)
        return graph

    monkeypatch.setattr(checkpointing_port.checkpointing, "get", lambda: fake)
    monkeypatch.setattr(runtime, "build_graph", build)
    return fake, built


class TestTheBoundDomain:
    def test_is_the_default_when_nothing_bound_one(self):
        assert runtime.current_domain() == "ecorp"

    def test_is_whatever_the_innermost_bind_says_and_is_restored_after(self):
        with runtime.bind_domain("support"):
            assert runtime.current_domain() == "support"
            with runtime.bind_domain("ops"):
                assert runtime.current_domain() == "ops"
            assert runtime.current_domain() == "support"
        assert runtime.current_domain() == "ecorp"

    async def test_a_sibling_task_keeps_its_own_domain(self):
        """The worker runs jobs as concurrent tasks: one task binding a domain must not move another's."""
        seen = {}
        release = asyncio.Event()

        async def job(name):
            with runtime.bind_domain(name):
                await release.wait()
                seen[name] = runtime.current_domain()

        tasks = [asyncio.create_task(job(n)) for n in ("support", "ops", "sales")]
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(*tasks)

        assert seen == {"support": "support", "ops": "ops", "sales": "sales"}
        assert runtime.current_domain() == "ecorp"


class TestOneGraphPerDomainOverOneCheckpointer:
    async def test_each_domain_gets_its_own_graph_built_from_its_own_manifest(self, opened):
        _, built = opened

        ecorp = await runtime.init_graph_async("ecorp")
        support = await runtime.init_graph_async("support")

        assert ecorp[0] == "ecorp" and support[0] == "support"
        assert [name for name, _ in built] == ["ecorp", "support"]

    async def test_the_checkpointer_is_opened_once_and_shared(self, opened):
        """`PgCheckpointing.open()` makes a NEW pool per call: one per domain would orphan all but the last."""
        fake, _ = opened

        graphs = [await runtime.init_graph_async(name) for name in ("ecorp", "support", "ops", "sales")]

        assert fake.opened == 1
        assert len({id(saver) for _, saver in graphs}) == 1

    async def test_asking_twice_returns_the_same_graph_without_rebuilding(self, opened):
        _, built = opened

        first = await runtime.init_graph_async("ops")
        second = await runtime.init_graph_async("ops")

        assert first is second
        assert len(built) == 1

    async def test_with_no_argument_it_follows_the_bound_domain(self, opened):
        assert (await runtime.init_graph_async())[0] == "ecorp"
        with runtime.bind_domain("sales"):
            assert (await runtime.init_graph_async())[0] == "sales"

    async def test_concurrent_first_callers_build_one_graph_and_open_one_checkpointer(self, opened):
        fake, built = opened

        await asyncio.gather(*(runtime.init_graph_async("support") for _ in range(8)))

        assert fake.opened == 1
        assert len(built) == 1

    async def test_an_unknown_domain_fails_loud_instead_of_falling_back_to_another(self, opened):
        with pytest.raises(ValueError, match="no-such-domain"):
            await runtime.init_graph_async("no-such-domain")


class TestUpsertSessionStampsTheBoundDomain:
    @pytest.fixture
    def captured(self, monkeypatch):
        captured = {}

        async def fake_upsert_session(ctx, thread_id, title, domain):
            captured.update(ctx=ctx, thread_id=thread_id, title=title, domain=domain)

        # patching the module object is what `core.sessions.upsert_session` re-reads at call time
        import app.agent.runtime.sessions as sessions_module

        monkeypatch.setattr(sessions_module, "upsert_session", fake_upsert_session)
        return captured

    async def test_stamps_the_domain_the_turn_runs_on(self, captured):
        with runtime.bind_domain("ops"):
            await runtime.upsert_session(TEST_CTX, "t1", "did anything break overnight?")

        assert captured == {
            "ctx": TEST_CTX,
            "thread_id": "t1",
            "title": "did anything break overnight?",
            "domain": "ops",
        }

    async def test_defaults_to_ecorp_when_nothing_bound_a_domain(self, captured):
        await runtime.upsert_session(TEST_CTX, "t1", "hello")

        assert captured["domain"] == "ecorp"
