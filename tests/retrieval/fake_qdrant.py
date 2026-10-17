"""An in-memory stand-in for the parts of `AsyncQdrantClient` that `qdrant_store`, the re-embed script and the identity guard use.

It models the behavior those tests depend on, deliberately including the rules a mock would get wrong:
  * a name is EITHER a collection or an alias, never both (creating an alias with a collection's name fails, as in Qdrant);
  * operations given to `update_collection_aliases` apply ALL or NOTHING (the atomic swap the re-embed relies on);
  * an operation on a missing collection fails the way Qdrant's "collection doesn't exist" does;
  * `scroll` pages with an offset and returns named vectors only when asked.
A mock proves statement shape, not Qdrant's behavior (Constitution VII); the real-Qdrant claims are proved in
tests/integration/test_embedding_identity_real_qdrant.py, and this fake is what keeps the hermetic suite from drifting from them.
"""
from types import SimpleNamespace
from typing import Any


class NotFound(Exception):
    pass


class FakeQdrant:
    def __init__(self) -> None:
        self.collections: dict[str, dict[str, Any]] = {}  # physical name -> {"points": {id: PointStruct}, "config": {...}}
        self.aliases: dict[str, str] = {}
        self.calls: list[tuple] = []
        self.query_result: list = []
        self.fail_next_alias_update: Exception | None = None

    # ---- naming --------------------------------------------------------------------------------------------------------------
    def _physical(self, name: str) -> str:
        physical = self.aliases.get(name, name)
        if physical not in self.collections:
            raise NotFound(f"Collection `{name}` doesn't exist!")
        return physical

    def seed(self, name: str, points: list | None = None) -> None:
        self.collections[name] = {"points": {str(p.id): p for p in (points or [])}, "config": {}}

    # ---- the client surface --------------------------------------------------------------------------------------------------
    async def recreate_collection(self, collection_name, **kwargs):
        self.calls.append(("recreate_collection", collection_name))
        self.collections[collection_name] = {"points": {}, "config": kwargs}

    async def collection_exists(self, collection_name):
        return collection_name in self.collections

    async def get_aliases(self):
        return SimpleNamespace(aliases=[SimpleNamespace(alias_name=a, collection_name=c) for a, c in self.aliases.items()])

    async def update_collection_aliases(self, change_aliases_operations, **_):
        self.calls.append(("update_collection_aliases", len(change_aliases_operations)))
        if self.fail_next_alias_update is not None:
            error, self.fail_next_alias_update = self.fail_next_alias_update, None
            raise error
        staged = dict(self.aliases)  # all-or-nothing: only committed if every operation is valid
        for op in change_aliases_operations:
            if getattr(op, "delete_alias", None) is not None:
                name = op.delete_alias.alias_name
                if name not in staged:
                    raise NotFound(f"Alias `{name}` doesn't exist")
                del staged[name]
            elif getattr(op, "create_alias", None) is not None:
                alias, target = op.create_alias.alias_name, op.create_alias.collection_name
                if target not in self.collections:
                    raise NotFound(f"Collection `{target}` doesn't exist!")
                if alias in staged or alias in self.collections:
                    raise ValueError(f"`{alias}` already exists")
                staged[alias] = target
        self.aliases = staged

    async def upsert(self, collection_name, points):
        self.calls.append(("upsert", collection_name, len(points)))
        store = self.collections[self._physical(collection_name)]["points"]
        for point in points:
            store[str(point.id)] = point

    async def query_points(self, collection_name, **kwargs):
        self.calls.append(("query_points", collection_name))
        physical = self._physical(collection_name)
        query, using = kwargs.get("query"), kwargs.get("using")
        if isinstance(query, (list, tuple)) and using and not self.query_result:
            # A plain vector query against one named vector: nearest by cosine, so "does this point find itself" is a real question.
            def cosine(a, b):
                na, nb = sum(x * x for x in a) ** 0.5, sum(x * x for x in b) ** 0.5
                return sum(x * y for x, y in zip(a, b, strict=True)) / (na * nb) if na and nb else 0.0

            scored = sorted(
                ((cosine(query, p.vector[using]), p) for p in self.collections[physical]["points"].values() if using in (p.vector or {})),
                key=lambda sp: -sp[0],
            )
            limit = kwargs.get("limit", 10)
            return SimpleNamespace(points=[SimpleNamespace(id=p.id, score=s, payload=p.payload) for s, p in scored[:limit]])
        return SimpleNamespace(points=self.query_result)

    async def count(self, collection_name, count_filter=None, **_):
        self.calls.append(("count", collection_name))
        return SimpleNamespace(count=len(self.collections[self._physical(collection_name)]["points"]))

    async def delete(self, collection_name, points_selector=None, **_):
        self.calls.append(("delete", collection_name))
        self._physical(collection_name)

    async def scroll(self, collection_name, limit=10, offset=None, with_payload=True, with_vectors=False, scroll_filter=None, **_):
        self.calls.append(("scroll", collection_name, offset))
        ids = sorted(self.collections[self._physical(collection_name)]["points"])
        start = ids.index(offset) if offset is not None else 0
        page = ids[start : start + limit]
        nxt = ids[start + limit] if start + limit < len(ids) else None
        points = self.collections[self._physical(collection_name)]["points"]
        records = [
            SimpleNamespace(
                id=points[i].id,
                payload=dict(points[i].payload or {}) if with_payload else None,
                vector=dict(points[i].vector) if with_vectors else None,
            )
            for i in page
        ]
        return records, nxt
