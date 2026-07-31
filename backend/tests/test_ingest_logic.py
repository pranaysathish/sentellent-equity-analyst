"""Tests for the idempotency and deduplication primitives.

These functions are what make re-running ingestion a no-op and stop the same
story being indexed three times because three outlets published it.
"""

from __future__ import annotations

import datetime as dt

from app.ingest import chunk_text
from app.sources import NewsItem, canonicalise_url, normalise_title


def _item(title: str, url: str, source: str = "Moneycontrol") -> NewsItem:
    return NewsItem(
        title=title,
        url=url,
        source=source,
        published_at=dt.datetime(2026, 7, 30, tzinfo=dt.UTC),
    )


class TestUrlCanonicalisation:
    def test_tracking_parameters_are_stripped(self):
        assert canonicalise_url(
            "https://www.moneycontrol.com/news/x-123.html?utm_source=rss&utm_medium=feed"
        ) == canonicalise_url("https://www.moneycontrol.com/news/x-123.html")

    def test_fragment_and_trailing_slash_are_ignored(self):
        assert canonicalise_url("https://a.com/story/#top") == canonicalise_url(
            "https://a.com/story"
        )

    def test_meaningful_query_parameters_are_kept(self):
        assert "id=42" in canonicalise_url("https://a.com/story?id=42")

    def test_case_differences_in_host_do_not_matter(self):
        assert canonicalise_url("https://WWW.Example.COM/a") == canonicalise_url(
            "https://www.example.com/a"
        )


class TestTitleNormalisation:
    def test_outlet_suffix_is_removed(self):
        assert normalise_title("TCS Q1 profit rises 9% - Moneycontrol") == normalise_title(
            "TCS Q1 profit rises 9%"
        )

    def test_punctuation_and_case_are_ignored(self):
        assert normalise_title("Reliance's Q1: Profit Up!") == normalise_title(
            "reliances q1 profit up"
        )


class TestArticleIdempotency:
    def test_same_story_produces_the_same_hash(self):
        """Re-polling the feed must not create a second row."""
        first = _item("TCS Q1 profit rises 9%", "https://mc.com/tcs-q1?utm_source=rss")
        second = _item("TCS Q1 profit rises 9%", "https://mc.com/tcs-q1")
        assert first.content_hash() == second.content_hash()

    def test_different_stories_produce_different_hashes(self):
        a = _item("TCS Q1 profit rises 9%", "https://mc.com/tcs-q1")
        b = _item("Infosys Q1 profit falls 3%", "https://mc.com/infy-q1")
        assert a.content_hash() != b.content_hash()

    def test_hash_is_stable_across_calls(self):
        item = _item("A headline", "https://mc.com/a")
        assert item.content_hash() == item.content_hash()

    def test_syndicated_copies_are_not_hash_equal(self):
        """Different outlets differ in URL, so they need semantic dedup.

        This documents *why* the embedding-distance check exists: hashing alone
        cannot catch the same wire story republished elsewhere.
        """
        mc = _item("TCS Q1 profit rises 9%", "https://moneycontrol.com/tcs")
        et = _item(
            "TCS Q1 net profit up 9% YoY", "https://economictimes.com/tcs", source="Economic Times"
        )
        assert mc.content_hash() != et.content_hash()


class TestChunking:
    def test_short_text_is_a_single_chunk(self):
        assert chunk_text("A short article body.") == ["A short article body."]

    def test_empty_input_produces_no_chunks(self):
        assert chunk_text("") == []
        assert chunk_text("   \n  ") == []

    def test_long_text_is_split_into_multiple_chunks(self):
        text = " ".join(f"Sentence number {i} about the company." for i in range(300))
        chunks = chunk_text(text)
        assert len(chunks) > 1

    def test_chunks_overlap_so_boundaries_are_not_lost(self):
        text = " ".join(f"Sentence number {i} about the company." for i in range(300))
        chunks = chunk_text(text, size=400, overlap=100)
        joined = sum(len(c) for c in chunks)
        assert joined > len(text)  # overlap means total exceeds the original

    def test_chunking_terminates_on_pathological_input(self):
        """A long run with no separators must not loop forever."""
        chunks = chunk_text("x" * 5000, size=300, overlap=100)
        assert len(chunks) > 1
        assert all(chunks)

    def test_no_content_is_dropped(self):
        text = "Alpha beta gamma. " * 200
        chunks = chunk_text(text)
        assert chunks[0].startswith("Alpha")
        assert "gamma" in chunks[-1]
