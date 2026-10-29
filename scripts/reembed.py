"""Rebuild a vector collection under the CURRENT embedding identity, with no moment in which the live collection holds two (specs/014
FR-020, contracts/reembed.md).

    make reembed COLLECTION_NAME=docs            # or: python -m scripts.reembed docs [--dry-run] [--resume] [--page N] [--allow-skips id,id]

WHEN. After the embedding model, its dimension, its query/document prefixes or EMBED_MODEL_REVISION changed, `qdrant_store` refuses to read
or write a collection made under the old identity (GET /health/ready names it). This is the way out.

HOW. The live collection stays untouched until the last step:
  1. plan   - the live collection (a legacy one is adopted first), the new `<name>-<hash>` and a point count; `--dry-run` stops here;
  2. copy   - every point is read with its payload and its EXISTING sparse vector, its `payload["text"]` (exactly what was embedded) is
              embedded again under the new identity, and the point is written into the new collection with the SAME id, payload
              verbatim and sparse vector kept. Nothing is re-uploaded, nothing is recomputed that did not change;
  3. verify - the new collection holds exactly the ids that were copied, and a sample of its points find themselves;
  4. swap   - the source is read once more, and if it no longer holds exactly the ids that were copied (a write landed during the run)
              the run STOPS without swapping; otherwise ONE alias request repoints `<name>-live` (Qdrant makes that atomic).
The old physical collection is never deleted here: an operator removes it after a bake period.

SAFETY. Payload (tenant and owner fields included) is copied verbatim, so tenant scoping is untouched (Principle I); this is an operator
script that reads every tenant's points by design, like the existing sweep scripts, and nothing under app/ imports it (a test checks).
A failure before step 4 leaves the old collection serving. Re-running is safe: ids are content-derived, and `--resume` reuses a
half-built target. Logs and output carry counts and durations only, never a payload or a text (Principle V).
Not supported, and detected rather than lost: writes to the collection while this runs (step 4). The milliseconds between the final
read and the alias request are a disclosed gap.

EXIT CODES. 0 swapped (or already current) | 1 error, nothing swapped | 2 usage | 3 the embedding service is unavailable |
4 the source changed during the run, or verification failed (nothing swapped).
"""
import argparse
import asyncio
import logging
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from qdrant_client.models import PointStruct

from app.bootstrap import wire
from app.core.config import COLLECTION, SKILLS_COLLECTION
from app.retrieval import embedding_identity, embeddings, qdrant_store

logger = logging.getLogger("reembed")

DEFAULT_PAGE = 256
DEFAULT_SAMPLE = 100
EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_EMBED_DOWN, EXIT_CHANGED = 0, 1, 2, 3, 4


class ReembedError(Exception):
    def __init__(self, exit_code: int, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass
class Report:
    collection: str
    source: str
    target: str
    points: int = 0
    copied: int = 0
    skipped: list[str] = field(default_factory=list)  # ids with no payload["text"]
    swapped: bool = False
    already_current: bool = False
    dry_run: bool = False
    seconds: float = 0.0

    def lines(self) -> list[str]:
        if self.already_current:
            return [f"{self.collection}: already under the configured embedding identity ({self.target}); nothing to do"]
        out = [f"{self.collection}: {self.source} -> {self.target}: {self.points} points"]
        if self.dry_run:
            return out + ["dry run: nothing written"]
        out.append(f"copied {self.copied}, skipped {len(self.skipped)} without text, in {self.seconds:.1f}s")
        out.append(
            f"swapped: {self.collection}-live now points at {self.target}; the old collection {self.source} is NOT deleted "
            "(remove it after a bake period)"
            if self.swapped
            else "NOT swapped"
        )
        return out


async def _scroll_all(client, name: str, *, page: int, with_payload: bool, with_vectors: bool):
    offset = None
    while True:
        records, offset = await client.scroll(
            collection_name=name, limit=page, offset=offset, with_payload=with_payload, with_vectors=with_vectors
        )
        for record in records:
            yield record
        if offset is None:
            return


async def _ids(client, name: str, page: int) -> set[str]:
    return {str(r.id) async for r in _scroll_all(client, name, page=page, with_payload=False, with_vectors=False)}


async def reembed(
    logical: str,
    *,
    page: int = DEFAULT_PAGE,
    sample: int = DEFAULT_SAMPLE,
    dry_run: bool = False,
    resume: bool = False,
    allow_skips: frozenset[str] = frozenset(),
    embed: Callable[[list[str]], Awaitable[list[list[float]]]] = embeddings.embed_documents,
) -> Report:
    started = time.monotonic()
    identity = await embedding_identity.current_identity()
    target = qdrant_store.physical_name(logical, identity.hash8())
    source = await qdrant_store.ensure_live(logical)  # adopts a legacy collection, creating the alias (non-destructive)
    if source is None:
        raise ReembedError(EXIT_ERROR, f"{logical}: there is no collection to re-embed (nothing exists yet; run `make ingest`)")
    report = Report(logical, source, target, dry_run=dry_run)
    if source == target:
        report.already_current = True
        return report

    client = qdrant_store.get_client()
    report.points = (await client.count(collection_name=source, exact=True)).count
    if dry_run:
        return report

    # ---- copy -------------------------------------------------------------------------------------------------------------
    if not (resume and await client.collection_exists(target)):
        await qdrant_store.create_physical(target, identity.dim)
    copied_ids: set[str] = set()
    batch: list = []

    async def flush() -> None:
        if not batch:
            return
        try:
            dense = await embed([str(r.payload["text"]) for r in batch])
        except ReembedError:
            raise
        except Exception as exc:  # noqa: BLE001 - any failure of the embedding service is exit 3, whatever its client library raised
            raise ReembedError(EXIT_EMBED_DOWN, f"{logical}: the embedding service failed ({type(exc).__name__}); nothing was swapped") from exc
        points = []
        for record, vector in zip(batch, dense, strict=True):
            named: dict[str, Any] = {qdrant_store.DENSE_VECTOR_NAME: vector}  # dense list and sparse model share one dict
            sparse = (record.vector or {}).get(qdrant_store.SPARSE_VECTOR_NAME)
            if sparse is not None:
                named[qdrant_store.SPARSE_VECTOR_NAME] = sparse  # kept, not recomputed: the lexical leg did not change
            points.append(PointStruct(id=record.id, vector=named, payload=record.payload))
        await client.upsert(collection_name=target, points=points)
        report.copied += len(points)
        batch.clear()

    async for record in _scroll_all(client, source, page=page, with_payload=True, with_vectors=True):
        copied_ids.add(str(record.id))
        text = (record.payload or {}).get("text")
        if not isinstance(text, str) or not text:
            report.skipped.append(str(record.id))
            continue
        batch.append(record)
        if len(batch) >= page:
            await flush()
    await flush()

    # ---- verify -----------------------------------------------------------------------------------------------------------
    unexplained = [i for i in report.skipped if i not in allow_skips]
    if unexplained:
        raise ReembedError(
            EXIT_CHANGED,
            f"{logical}: {len(unexplained)} points have no payload text and cannot be re-embedded (first: {unexplained[:3]}); "
            "list their ids in --allow-skips to leave them out, or fix them. Nothing was swapped.",
        )
    expected_in_target = copied_ids - set(report.skipped)
    in_target = await _ids(client, target, page)
    if in_target != expected_in_target:
        raise ReembedError(
            EXIT_CHANGED,
            f"{logical}: the new collection holds {len(in_target)} points, expected {len(expected_in_target)}; nothing was swapped. "
            "Re-run with --resume to fill the gap.",
        )
    await _check_sample(client, source, target, sample, page)

    # ---- swap -------------------------------------------------------------------------------------------------------------
    if await _ids(client, source, page) != copied_ids:
        raise ReembedError(
            EXIT_CHANGED,
            f"{logical}: the source collection changed while it was being copied (a write landed); nothing was swapped. "
            "Pause ingestion and run it again.",
        )
    await qdrant_store.repoint_live(logical, target)
    report.swapped = True
    report.seconds = time.monotonic() - started
    logger.info("re-embedded", extra={"collection": logical, "points": report.copied, "seconds": round(report.seconds, 1)})
    return report


async def _check_sample(client, source: str, target: str, sample: int, page: int) -> None:
    """A sample of the new collection's points must find themselves: it catches a vector written against the wrong point or a dimension
    that does not match the data. A different point with identical text is an accepted answer (duplicate passages embed identically)."""
    checked = 0
    async for record in _scroll_all(client, target, page=page, with_payload=True, with_vectors=True):
        dense = (record.vector or {}).get(qdrant_store.DENSE_VECTOR_NAME)
        if dense is None:
            raise ReembedError(EXIT_CHANGED, f"{target}: point {record.id} has no dense vector; nothing was swapped")
        hits = (await client.query_points(collection_name=target, query=dense, using=qdrant_store.DENSE_VECTOR_NAME, limit=1)).points
        top = hits[0] if hits else None
        if top is None or (str(top.id) != str(record.id) and (top.payload or {}).get("text") != (record.payload or {}).get("text")):
            raise ReembedError(EXIT_CHANGED, f"{target}: point {record.id} does not find itself; nothing was swapped")
        checked += 1
        if checked >= sample:
            return


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m scripts.reembed", description=__doc__.split("\n\n")[0])
    parser.add_argument("collection", help=f"the logical collection: {COLLECTION!r} (documents, notes, memories) or {SKILLS_COLLECTION!r}")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and stop; write nothing")
    parser.add_argument("--resume", action="store_true", help="reuse a half-built target collection instead of recreating it")
    parser.add_argument("--page", type=int, default=DEFAULT_PAGE, help="points per read/embed batch")
    parser.add_argument("--allow-skips", default="", help="comma list of point ids to leave out because they have no payload text")
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    if args.collection not in (COLLECTION, SKILLS_COLLECTION):
        print(f"unknown collection {args.collection!r}; expected {COLLECTION!r} or {SKILLS_COLLECTION!r}", file=sys.stderr)
        return EXIT_USAGE
    if args.page < 1:
        print("--page must be at least 1", file=sys.stderr)
        return EXIT_USAGE
    try:
        report = await reembed(
            args.collection,
            page=args.page,
            dry_run=args.dry_run,
            resume=args.resume,
            allow_skips=frozenset(i for i in args.allow_skips.split(",") if i),
        )
    except ReembedError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code
    except Exception as exc:  # noqa: BLE001 - an operator script: report the class, never a payload, and leave the old collection serving
        print(f"{args.collection}: failed ({type(exc).__name__}: {exc}); the old collection is untouched", file=sys.stderr)
        return EXIT_ERROR
    print("\n".join(report.lines()))
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    wire()
    return asyncio.run(_run(_parse(sys.argv[1:] if argv is None else argv)))


if __name__ == "__main__":
    raise SystemExit(main())
