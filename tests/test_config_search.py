"""Config + search tests. No network."""
from __future__ import annotations

import json
import pathlib

import pytest

from corpuscrawl.config import ConfigError, load_config, sources_from_url, validate_source
from corpuscrawl.search import get_page, search_pack
from corpuscrawl.store import CorpusPack

# ── config ──────────────────────────────────────────────────────────────────────────────────────

def test_load_config_object(tmp_path: pathlib.Path):
    p = tmp_path / "sources.json"
    p.write_text(json.dumps({
        "collection": "kb",
        "sources": [
            {"id": "wiki", "name": "Wiki", "type": "mediawiki", "url": "https://wiki.example.org", "scrape": True},
            {"id": "tool", "name": "Tool", "type": "tool", "url": "https://tool.example"},
        ],
    }))
    coll, sources = load_config(p)
    assert coll == "kb"
    assert [s["id"] for s in sources] == ["wiki", "tool"]


def test_load_config_bare_list_uses_stem(tmp_path: pathlib.Path):
    p = tmp_path / "myproject.json"
    p.write_text(json.dumps([{"id": "a", "name": "A", "type": "web", "url": "https://a.com", "scrape": True}]))
    coll, sources = load_config(p)
    assert coll == "myproject" and sources[0]["id"] == "a"


def test_load_config_rejects_bad_entries(tmp_path: pathlib.Path):
    bad = {
        "bad-type":   {"id": "a", "name": "A", "type": "forum", "url": "https://a.com"},
        "bad-id":     {"id": "Bad_ID", "name": "A", "type": "web", "url": "https://a.com"},
        "no-url":     {"id": "a", "name": "A", "type": "web"},
        "scrape-tool": {"id": "a", "name": "A", "type": "tool", "url": "https://a.com", "scrape": True},
    }
    for label, entry in bad.items():
        p = tmp_path / f"{label}.json"
        p.write_text(json.dumps([entry]))
        with pytest.raises(ConfigError):
            load_config(p)


def test_load_config_rejects_duplicate_id(tmp_path: pathlib.Path):
    p = tmp_path / "dup.json"
    p.write_text(json.dumps([
        {"id": "a", "name": "A", "type": "web", "url": "https://a.com", "scrape": True},
        {"id": "a", "name": "B", "type": "web", "url": "https://b.com", "scrape": True},
    ]))
    with pytest.raises(ConfigError):
        load_config(p)


def test_load_config_missing_file():
    with pytest.raises(ConfigError):
        load_config("/no/such/config.json")


def test_sources_from_url_derives_id():
    srcs = sources_from_url("https://wiki.example.org", type="mediawiki")
    assert len(srcs) == 1
    assert srcs[0]["type"] == "mediawiki" and srcs[0]["scrape"] is True
    assert srcs[0]["id"] == "wiki-example-org"


def test_validate_source_ok():
    validate_source({"id": "x", "name": "X", "type": "web", "url": "https://x.com"})


# ── search ──────────────────────────────────────────────────────────────────────────────────────

def _seed(tmp_path):
    pack = CorpusPack("c", "s", packs_dir=tmp_path)
    pack.upsert_page(title="Parser Functions", url="https://x/wiki/ParserFunctions",
                     plaintext="A parser function adds logic to a template on a wiki. Evaluate it at render time.",
                     categories=["Category:Help"], sections=[{"level": 2, "title": "Overview"}])
    pack.upsert_page(title="Magic Words", url="https://x/wiki/MagicWords",
                     plaintext="Magic words are variables the wiki software substitutes when a page renders.")
    pack.commit()
    return pack


def test_search_ranks_and_snippets(tmp_path: pathlib.Path):
    pack = _seed(tmp_path)
    hits = search_pack(pack, "parser function")
    assert hits and hits[0]["title"] == "Parser Functions"
    assert "[parser]".lower() in hits[0]["snippet"].lower() or "parser" in hits[0]["snippet"].lower()
    assert hits[0]["url"].endswith("/ParserFunctions")
    pack.close()


def test_search_handles_punctuation_and_empty(tmp_path: pathlib.Path):
    pack = _seed(tmp_path)
    assert search_pack(pack, "  ??? ") == []          # punctuation-only → no crash, no hits
    assert search_pack(pack, "nonexistentword") == []
    pack.close()


def test_get_page(tmp_path: pathlib.Path):
    pack = _seed(tmp_path)
    page = get_page(pack, "Parser Functions")
    assert page and page["categories"] == ["Category:Help"]
    assert page["sections"][0]["title"] == "Overview"
    assert get_page(pack, "Missing") is None
    pack.close()
