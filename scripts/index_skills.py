"""Embed the bundled skill catalog (name + description) and load it into
the `skills` Qdrant collection — the search index `skill_search` queries
(app/agent/tools.py, GRAPH_PATTERNS.md pattern 45).

Run with: `make index-skills`, any time a SKILL.md is added, edited, or
removed under `skills/`.

Mirrors scripts/seed.py's shape and division of labor: `ensure_collection`
is destructive (recreates the collection) so it's called explicitly, once,
here — never implicitly inside the loop. Unlike seed.py, this does NOT go
through app/ingestion/ingestor.py's chunking pipeline: a skill's searchable
text is just its short `name: description` summary, never split into
parent/child chunks, and the collection holds only that summary — a
skill's full instruction body is never written to Qdrant at all (see
app/agent/skills.py's docstring for why: disk stays the one source of
truth for content, Qdrant is only the search index over metadata).
"""
import uuid

from app.agent import skills as skills_module
from app.core.config import SKILLS_COLLECTION
from app.retrieval import qdrant_store
from app.retrieval.embeddings import embed_sparse, embed_text


def main() -> None:
    try:
        skills_module.reload_skills()
        catalog = skills_module.get_skills()
        if not catalog:
            raise SystemExit(
                f"No skills found under {skills_module.SKILLS_DIR!r} — nothing to index."
            )

        # dim comes from a real embed call, same as scripts/seed.py — never hardcoded.
        qdrant_store.ensure_collection(
            dim=len(embed_text("dimension probe")), collection=SKILLS_COLLECTION
        )

        points = []
        for record in catalog.values():
            text = f"{record.name}: {record.description}"
            try:
                sparse = embed_sparse(text)
            except Exception:  # noqa: BLE001 - degrade to dense-only, same as tools.py's write paths
                sparse = None
            points.append(
                qdrant_store.build_point(
                    point_id=str(uuid.uuid4()),
                    dense_vector=embed_text(text),
                    sparse_vector=sparse,
                    payload={"text": text, "name": record.name, "description": record.description},
                )
            )
        qdrant_store.upsert(points, collection=SKILLS_COLLECTION)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            f"Failed to index skills: {exc}\n"
            "Is the stack running? Try `make up` and `make pull-models` first."
        ) from exc

    print(
        f"Indexed {len(catalog)} skill(s) into the {SKILLS_COLLECTION!r} collection: "
        f"{', '.join(sorted(catalog))}."
    )


if __name__ == "__main__":
    main()
