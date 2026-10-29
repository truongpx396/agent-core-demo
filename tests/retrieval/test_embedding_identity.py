"""The embedding identity: what makes two vector spaces the same, or different (app/retrieval/embedding_identity.py)."""
import pytest

from app.ports import models as model_ports
from app.retrieval import embedding_identity as ident
from app.retrieval.embedding_identity import (
    EmbeddingIdentity,
    EmbeddingIdentityMismatch,
)
from app.retrieval.embedding_identity import (
    current_identity as real_current_identity,  # captured before the autouse fixture replaces it
)


def _identity(**overrides) -> EmbeddingIdentity:
    base = dict(alias="embed", resolved_model="ollama/nomic-embed-text", dim=768, query_prefix="", document_prefix="", revision_label="")
    return EmbeddingIdentity(**{**base, **overrides})


def test_the_hash_is_stable_and_eight_hex_digits():
    h = _identity().hash8()
    assert h == _identity().hash8() and len(h) == 8 and int(h, 16) >= 0


def test_two_models_of_the_same_dimension_are_different_identities():
    """The trap that motivated all of this: nomic-embed-text and gte-modernbert-base are both 768-d, so a size check passes while every
    search returns nonsense."""
    nomic = _identity(resolved_model="ollama/nomic-embed-text", dim=768)
    gte = _identity(resolved_model="openai/gte-modernbert-base", dim=768)
    assert nomic.dim == gte.dim and nomic.hash8() != gte.hash8()


def test_turning_a_prefix_on_changes_the_identity_although_the_model_does_not():
    plain = _identity()
    assert _identity(query_prefix="search_query: ").hash8() != plain.hash8()
    assert _identity(document_prefix="search_document: ").hash8() != plain.hash8()
    assert _identity(query_prefix="a").hash8() != _identity(document_prefix="a").hash8()  # which side the prefix is on matters


def test_the_operator_revision_label_changes_the_identity():
    """For the one change the alias resolver cannot see: the same model name re-pulled to different weights."""
    assert _identity(revision_label="2026-11").hash8() != _identity().hash8()


def test_the_dimension_is_part_of_the_identity():
    assert _identity(dim=384).hash8() != _identity(dim=768).hash8()


def test_renaming_the_alias_to_the_same_model_does_not_force_a_re_embed():
    assert _identity(alias="embed").hash8() == _identity(alias="embeddings-v2").hash8()


def test_when_nothing_resolved_the_alias_name_stands_in_for_the_model_and_the_identity_is_marked_unverified():
    unresolved = _identity(resolved_model=None)
    assert unresolved.verified is False and _identity().verified is True
    assert unresolved.hash8() != _identity().hash8()
    assert unresolved.hash8() != _identity(resolved_model=None, alias="other").hash8()  # the alias is the only handle left


def test_describe_names_the_model_and_hash_for_a_log_line_and_marks_an_unresolved_alias():
    assert _identity().describe()["model"] == "ollama/nomic-embed-text"
    assert "unresolved" in _identity(resolved_model=None).describe()["model"]
    assert _identity().describe()["hash"] == _identity().hash8()


def test_the_mismatch_message_names_the_way_out():
    err = EmbeddingIdentityMismatch("docs", "aaaaaaaa", "bbbbbbbb", write=False)
    text = str(err)
    assert "docs" in text and "aaaaaaaa" in text and "bbbbbbbb" in text and "make reembed COLLECTION_NAME=docs" in text
    assert err.code.value == "embedding_identity_mismatch"


async def test_the_current_identity_is_assembled_from_the_alias_the_probe_and_the_settings(monkeypatch):
    class Catalog:
        async def resolve(self, alias):
            return f"resolved/{alias}"

    async def dim():
        return 384

    monkeypatch.setattr(ident.embeddings, "embedding_dim", dim)
    monkeypatch.setattr(ident, "EMBED_QUERY_PREFIX", "q: ")
    monkeypatch.setattr(ident, "EMBED_DOCUMENT_PREFIX", "d: ")
    monkeypatch.setattr(ident, "EMBED_MODEL_REVISION", "r1")

    with model_ports.model_catalog.overriding(Catalog()):  # type: ignore[arg-type] - only `resolve` is asked of it here
        got = await real_current_identity()

    assert got == EmbeddingIdentity("embed", "resolved/embed", 384, "q: ", "d: ", "r1")


@pytest.mark.parametrize("bad", [None])
def test_an_identity_with_no_resolved_model_still_hashes(bad):
    assert len(_identity(resolved_model=bad).hash8()) == 8
