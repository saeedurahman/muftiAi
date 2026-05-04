"""Tests for URL canonicalization and text normalization."""

from mufti_scraper.cleaning import canonical_url, content_hash, normalize_text


def test_canonical_url_strips_fragment():
    u = canonical_url("https://Example.COM/path/?x=1#frag")
    assert "#" not in u
    assert "example.com" in u


def test_normalize_text_collapses_whitespace():
    s = normalize_text("  hello  \n\n  world  \u200b ")
    assert s == "hello\nworld"


def test_content_hash_stable():
    a = content_hash("q", "a")
    b = content_hash("q", "a")
    c = content_hash("q", "b")
    assert a == b
    assert a != c
