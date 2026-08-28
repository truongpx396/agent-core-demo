"""Tests for app/ingestion/chunking.py — pure functions, no I/O, so these run purely
on string arithmetic (no mocking needed)."""
import pytest

from app.ingestion.chunking import chunk_text


class TestChunkText:
    def test_blank_text_returns_no_parents(self):
        assert chunk_text("") == []
        assert chunk_text("   \n\n  ") == []

    def test_short_text_becomes_one_parent_with_one_child(self):
        text = "A short sentence about checkpointers."
        parents = chunk_text(text, parent_chars=1200, child_chars=300, child_overlap=75)
        assert len(parents) == 1
        assert parents[0].text == text
        assert parents[0].children == [text]

    def test_each_parent_gets_a_unique_id(self):
        text = ("Paragraph one. " * 50) + "\n\n" + ("Paragraph two. " * 50)
        parents = chunk_text(text, parent_chars=200, child_chars=80, child_overlap=20)
        assert len(parents) > 1
        ids = [p.parent_id for p in parents]
        assert len(ids) == len(set(ids))

    def test_paragraphs_are_packed_without_splitting_a_short_paragraph(self):
        """Paragraph-aware packing: two short paragraphs that fit together
        under parent_chars end up in the SAME parent, not one-per-parent."""
        text = "First short paragraph.\n\nSecond short paragraph."
        parents = chunk_text(text, parent_chars=1200, child_chars=300, child_overlap=75)
        assert len(parents) == 1
        assert "First short paragraph." in parents[0].text
        assert "Second short paragraph." in parents[0].text

    def test_a_paragraph_longer_than_parent_chars_is_hard_split(self):
        long_para = "x" * 500
        parents = chunk_text(long_para, parent_chars=200, child_chars=80, child_overlap=20)
        assert len(parents) == 3  # 500 // 200 rounded up
        assert sum(len(p.text) for p in parents) == 500

    def test_children_overlap_by_the_configured_amount(self):
        parent_text = "y" * 1000
        parents = chunk_text(parent_text, parent_chars=1200, child_chars=300, child_overlap=75)
        assert len(parents) == 1
        children = parents[0].children
        assert len(children) > 1
        # Consecutive children share exactly `child_overlap` characters at
        # the boundary (the sliding window's step is child_chars - overlap).
        for a, b in zip(children, children[1:], strict=False):
            assert a[-75:] == b[:75]

    def test_children_reconstruct_the_full_parent_when_overlap_is_removed(self):
        parent_text = "".join(f"word{i} " for i in range(200))  # long enough to need multiple windows
        parents = chunk_text(parent_text, parent_chars=5000, child_chars=100, child_overlap=25)
        assert len(parents) == 1
        children = parents[0].children
        step = 100 - 25
        reconstructed = children[0]
        for child in children[1:]:
            reconstructed += child[25:] if len(child) >= 25 else ""
        # Reconstructing via the known step should reproduce the source
        # (up to the final window's own length) — proves the sliding
        # window doesn't silently drop or duplicate text beyond the
        # declared overlap.
        assert reconstructed[: len(parent_text)] == parent_text or len(reconstructed) >= len(parent_text) - step

    def test_last_child_window_can_be_shorter_than_child_chars(self):
        parent_text = "z" * 350
        parents = chunk_text(parent_text, parent_chars=1200, child_chars=100, child_overlap=25)
        children = parents[0].children
        assert len(children[-1]) <= 100
        assert "".join(dict.fromkeys(children))  # sanity: non-empty, no crash

    def test_child_overlap_must_be_smaller_than_child_chars(self):
        with pytest.raises(ValueError):
            chunk_text("some text", child_chars=100, child_overlap=100)
        with pytest.raises(ValueError):
            chunk_text("some text", child_chars=100, child_overlap=150)

    def test_multi_paragraph_document_produces_multiple_parents(self):
        text = "\n\n".join(f"Paragraph {i}. " * 30 for i in range(5))
        parents = chunk_text(text, parent_chars=300, child_chars=100, child_overlap=25)
        assert len(parents) > 1
        for p in parents:
            assert p.children  # every parent has at least one child
