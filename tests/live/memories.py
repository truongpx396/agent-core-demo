"""Undo the memories a live test wrote, so the next test does not inherit them.

`remember` writes a persistent, cross-session memory into the SAME Qdrant `docs` collection the whole
`real_stack_with_retrieval` session shares (`kind: "memory"`), and every later turn pre-fetches the
principal's memories that look relevant to its prompt (app/agent/graph_retrieval.py). So a memory is
state that outlives the test that wrote it.

That is a real, reproduced failure, not a hypothetical one. `test_a_mutating_tool_call_pauses_for_approval_and_resumes_on_approve`
remembers "I prefer dark roast coffee." and `test_a_skill_is_found_and_followed`, a few tests later, says
"coffee $5 and lunch $12". With the memory present the 3B model called `skill_search` with no arguments
(a `ValidationError`) and then proposed a batch that included the mutating `add_note`, so the turn paused
for an approval nobody could give: a red `test-live` in every CI run since Oct 3 (about 40). With only the memory removed between
the two tests (an A/B on one machine, same code, same model) the skill test passed; with it present it
failed in all three runs, and the same sequence failed identically in CI.

Kept free of fixtures and of anything that needs Docker so it is testable against an in-memory Qdrant.
"""
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue, PointIdsList

_ONLY_MEMORIES = Filter(must=[FieldCondition(key="kind", match=MatchValue(value="memory"))])
_PAGE = 256


def memory_ids(client: QdrantClient, collection: str) -> set[int | str]:
    """The id of every memory point in `collection` (paged: a collection may hold more than one page)."""
    ids: set[int | str] = set()
    offset = None
    while True:
        points, offset = client.scroll(
            collection,
            scroll_filter=_ONLY_MEMORIES,
            limit=_PAGE,
            offset=offset,
            with_payload=False,
            with_vectors=False,
        )
        ids.update(point.id for point in points)
        if offset is None:
            return ids


def forget_memories_not_in(client: QdrantClient, collection: str, keep: set[int | str]) -> int:
    """Delete every memory except the ids in `keep` (the ones that existed before the test), by id.

    Deliberately not "delete everything with kind=memory": a memory that was already there is not this
    test's to remove, and deleting by id can never touch a document or a skill. Returns how many it removed.
    """
    leaked = memory_ids(client, collection) - keep
    if leaked:
        client.delete(collection, points_selector=PointIdsList(points=sorted(leaked, key=str)))
    return len(leaked)
