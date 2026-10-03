"""Tenant/conversation isolation of the per-conversation sandbox lookup.

Constitution Principle I (tenant isolation, NON-NEGOTIABLE) applies: a
sandbox holds files a model wrote on a user's behalf, so two conversations
must never land in the same one. `sandbox_session.get_or_create_sandbox_id`
used to find a sandbox by the conversation id *rewritten to OpenSandbox's
metadata-value rules* (every character outside `[A-Za-z0-9_.-]` became `-`,
cut to 63 characters) and never looked at the tenant, so `telegram:12345`
and `telegram-12345` resolved to the same sandbox, and so did the same id
under two tenants. Reproduced against a stand-in service (below) before the
fix; spec 009 B24.

The stand-in is the part that has to be honest: it applies the metadata
filter the way `sandbox_list` documents it (every requested key must equal
the sandbox's own tag) and enforces the metadata VALUE rules the module
docstring records (<=63 chars, alphanumeric at both ends, only
alphanumeric/`-`/`_`/`.` between). That proves the lookup logic and the tags
this app builds; it does not prove the real service's behaviour (the real
sandbox service was never driven for this fix — see the 009 spec).
"""
import json
import re

import pytest

from app.domains import sandbox_session
from app.domains.ops import tools as ops_tools
from app.domains.sales import tools as sales_tools
from app.domains.support import tools as support_tools

_VALUE_RULE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9_.-]{0,61}[A-Za-z0-9])?$")


class _StandInSandboxService:
    """`sandbox_list` / `sandbox_create` over an in-memory set of sandboxes."""

    def __init__(self, *, honour_filter: bool = True):
        self.sandboxes: list[dict] = []
        self.honour_filter = honour_filter

    def tools(self) -> dict:
        return {"sandbox_list": _Tool(self._list), "sandbox_create": _Tool(self._create)}

    def _list(self, filter):
        wanted = filter.get("metadata", {})
        infos = [
            {"id": s["id"], "status": {"state": "RUNNING"}, "metadata": s["metadata"]}
            for s in self.sandboxes
            if not self.honour_filter or all(s["metadata"].get(k) == v for k, v in wanted.items())
        ]
        return json.dumps({"sandbox_infos": infos, "pagination": {}})

    def _create(self, **kwargs):
        metadata = kwargs["metadata"]
        for key, value in metadata.items():
            assert _VALUE_RULE.match(value), f"tag {key!r}={value!r} would be rejected by OpenSandbox"
        sandbox_id = f"sbx_{len(self.sandboxes) + 1}"
        self.sandboxes.append({"id": sandbox_id, "metadata": dict(metadata)})
        return json.dumps({"sandbox_id": sandbox_id, "info": {}})


class _Tool:
    def __init__(self, fn):
        self._fn = fn

    async def ainvoke(self, kwargs):
        return self._fn(**kwargs)


async def _sandbox_for(service, *, tenant: str, thread_id: str) -> str:
    return await sandbox_session.get_or_create_sandbox_id(service.tools(), thread_id, tenant=tenant)


# Pairs the old sanitizer collapsed onto one tag value, plus the truncation case.
_COLLIDING_THREAD_IDS = [
    "telegram:12345",
    "telegram-12345",
    "demo:1",
    "demo-1",
    "demo 1",
    "a" * 63 + "x",
    "a" * 63 + "y",
]


async def test_conversation_ids_that_only_differ_in_punctuation_or_the_tail_get_distinct_sandboxes():
    service = _StandInSandboxService()

    ids = [await _sandbox_for(service, tenant="ecorp", thread_id=t) for t in _COLLIDING_THREAD_IDS]

    assert len(set(ids)) == len(_COLLIDING_THREAD_IDS), (
        f"conversations shared a sandbox: {dict(zip(_COLLIDING_THREAD_IDS, ids, strict=True))}"
    )


async def test_the_same_conversation_id_under_two_tenants_gets_two_sandboxes():
    service = _StandInSandboxService()

    acme = await _sandbox_for(service, tenant="acme", thread_id="telegram:12345")
    globex = await _sandbox_for(service, tenant="globex", thread_id="telegram:12345")

    assert acme != globex


async def test_the_same_tenant_and_conversation_still_reuses_one_sandbox():
    service = _StandInSandboxService()

    first = await _sandbox_for(service, tenant="ecorp", thread_id="telegram:12345")
    second = await _sandbox_for(service, tenant="ecorp", thread_id="telegram:12345")

    assert first == second
    assert len(service.sandboxes) == 1


async def test_a_service_that_ignores_the_filter_cannot_make_a_conversation_reuse_someone_elses_sandbox():
    """Defence in depth: the lookup verifies the tags of what it found rather
    than trusting that the server applied the filter."""
    service = _StandInSandboxService(honour_filter=False)

    victim = await _sandbox_for(service, tenant="acme", thread_id="telegram:12345")
    other_tenant = await _sandbox_for(service, tenant="globex", thread_id="telegram:12345")
    other_thread = await _sandbox_for(service, tenant="acme", thread_id="telegram-12345")

    assert len({victim, other_tenant, other_thread}) == 3


async def test_a_sandbox_with_no_readable_tags_is_never_reused():
    """A listing entry whose metadata is missing cannot be verified, so the
    lookup fails closed to creating a fresh sandbox rather than guessing."""

    def untagged_list(filter):
        return json.dumps({"sandbox_infos": [{"id": "sbx_untagged", "status": {"state": "RUNNING"}}], "pagination": {}})

    created = []

    def create(**kwargs):
        created.append(kwargs)
        return json.dumps({"sandbox_id": "sbx_new", "info": {}})

    raw = {"sandbox_list": _Tool(untagged_list), "sandbox_create": _Tool(create)}

    sandbox_id = await sandbox_session.get_or_create_sandbox_id(raw, "t", tenant="ecorp")

    assert sandbox_id == "sbx_new"
    assert len(created) == 1


async def test_the_tags_name_the_tenant_and_the_conversation_by_hash_not_by_raw_value():
    """Neither the tenant nor the conversation id (a Telegram chat id, say) is
    written into the sandbox service in the clear, and both fit its rules."""
    service = _StandInSandboxService()

    await _sandbox_for(service, tenant="acme-corp", thread_id="telegram:12345")

    (metadata,) = [s["metadata"] for s in service.sandboxes]
    assert set(metadata) == {sandbox_session.SANDBOX_TENANT_KEY, sandbox_session.SANDBOX_THREAD_KEY}
    assert "acme-corp" not in metadata.values()
    assert "12345" not in "".join(metadata.values())


@pytest.mark.parametrize(
    ("tenant", "thread_id"),
    [("", "telegram:12345"), ("   ", "telegram:12345"), ("ecorp", ""), ("ecorp", "  ")],
)
async def test_a_blank_tenant_or_conversation_id_is_refused_rather_than_sharing_one_bucket(tenant, thread_id):
    service = _StandInSandboxService()

    with pytest.raises(sandbox_session.SandboxCallFailed):
        await _sandbox_for(service, tenant=tenant, thread_id=thread_id)

    assert service.sandboxes == []


# --- the call sites: each product hands its tenant over from the identity ---------

_CALLS = [
    ("run_command_in_sandbox", ("ls",)),
    ("run_python_in_sandbox", ("print(1)",)),
    ("read_sandbox_file", ("/tmp/a",)),
    ("write_sandbox_file", ("/tmp/a", "x")),
]


@pytest.mark.parametrize("tools_module", [ops_tools, support_tools, sales_tools], ids=["ops", "support", "sales"])
@pytest.mark.parametrize(("name", "args"), _CALLS, ids=[c[0] for c in _CALLS])
async def test_every_sandbox_tool_in_every_product_passes_the_identitys_tenant(
    tools_module, name, args, monkeypatch
):
    seen: dict = {}

    async def fake_impl(*call_args, **kwargs):
        seen.update(kwargs)
        return "ok"

    async def fake_raw():
        return {}

    monkeypatch.setattr(sandbox_session, f"{name}_impl", fake_impl)
    monkeypatch.setattr(tools_module, "_raw_sandbox_tools_or_raise", fake_raw)

    ctx = {"tenant": "globex", "principal": "alice", "clearance": 1}
    await getattr(tools_module, f"_{name}_impl")(*args, "thread-1", ctx)

    assert seen == {"tenant": "globex"}
