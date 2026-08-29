"""Engine tests — robots matcher, wall detection, wikitext extraction, the SQLite/FTS pack. No network."""
from __future__ import annotations

import pathlib

from corpuscrawl.adapters import wikitext_sections, wikitext_to_plaintext
from corpuscrawl.core import extract_readable, looks_like_wall, parse_robots, robots_can_fetch
from corpuscrawl.store import CorpusPack

# ── robots matcher (the stdlib-bug fix: Allow: / + wildcard Disallow) ───────────────────────────

def test_robots_allow_with_wildcard_disallow():
    g = parse_robots("User-Agent: *\nAllow: /\nDisallow: /*?z=\n")
    assert robots_can_fetch(g, "any-bot/1.0", "/guides") is True
    assert robots_can_fetch(g, "any-bot/1.0", "/guides/how-to/") is True
    assert robots_can_fetch(g, "any-bot/1.0", "/foo?z=1") is False


def test_robots_specific_group_and_longest_match():
    g = parse_robots("User-Agent: *\nAllow: /\n\nUser-Agent: BadBot\nDisallow: /\n")
    assert robots_can_fetch(g, "Mozilla BadBot/2", "/x") is False
    assert robots_can_fetch(g, "GoodBot/1", "/x") is True
    g2 = parse_robots("User-Agent: *\nDisallow: /admin\nAllow: /admin/public\n")
    assert robots_can_fetch(g2, "x", "/admin/public/page") is True
    assert robots_can_fetch(g2, "x", "/admin/secret") is False


def test_robots_empty_or_absent_allows():
    assert robots_can_fetch(parse_robots(""), "x", "/anything") is True
    assert robots_can_fetch(parse_robots("User-Agent: *\nDisallow:\n"), "x", "/anything") is True


# ── wall detection ────────────────────────────────────────────────────────────────────────────────

def test_wall_detection():
    assert looks_like_wall(503, "<title>Just a moment...</title><div>cf-browser-verification</div>")
    assert looks_like_wall(403, "<html><title>Attention Required! | Cloudflare</title></html>")
    assert looks_like_wall(200, '<html><body><div id="root"></div><script src="/app.js"></script></body></html>')
    real = "<html><body><article>" + ("Real readable content about the subject. " * 60) + "</article></body></html>"
    assert not looks_like_wall(200, real)


# ── wikitext → plaintext / sections ─────────────────────────────────────────────────────────────

def test_wikitext_strips_templates_links_tables():
    wt = ("{{Infobox|a=1}}\nA '''Raid''' is a [[Gym|gym]] battle.<ref>cite</ref>\n"
          "{| class=wikitable\n|foo\n|}\n== Mechanics ==\n[[File:Pic.png|thumb]]Body.")
    out = wikitext_to_plaintext(wt)
    assert "Infobox" not in out and "{{" not in out
    assert "wikitable" not in out and "foo" not in out
    assert "cite" not in out and "Pic.png" not in out
    assert "gym battle" in out and "Body." in out and "Mechanics" in out
    assert "'''" not in out and "==" not in out


def test_wikitext_sections():
    secs = [(s["level"], s["title"]) for s in wikitext_sections("Intro\n== One ==\nx\n=== Sub ===\n")]
    assert (2, "One") in secs and (3, "Sub") in secs


def test_extract_readable_lxml_fallback():
    html = "<html><head><title>My Page</title></head><body><nav>menu</nav>" \
           "<article>" + ("The real content lives here in prose. " * 20) + "</article></body></html>"
    title, text = extract_readable(html, "https://x/p")
    assert title == "My Page"
    assert "real content" in text and "menu" not in text


# ── pack: upsert / resume / FTS / images ─────────────────────────────────────────────────────────

def test_pack_roundtrip_and_fts(tmp_path: pathlib.Path):
    with CorpusPack("c", "s", packs_dir=tmp_path) as pack:
        pack.upsert_page(title="Raid Battles", url="https://x/wiki/Raid",
                         plaintext="A raid is a cooperative gym battle.", revid=42)
        pack.upsert_page(title="Eggs", url="https://x/wiki/Eggs", plaintext="Eggs hatch after walking.")
        pack.commit()
        assert pack.counts()["pages"] == 2
        assert pack.has_page("Raid Battles") and not pack.has_page("Missing")
        hits = [r[0] for r in pack.db.execute("SELECT title FROM pages_fts WHERE pages_fts MATCH 'gym'")]
        assert "Raid Battles" in hits and "Eggs" not in hits
        # idempotent update keeps FTS coherent
        pack.upsert_page(title="Eggs", plaintext="Eggs now mention gyms too.")
        pack.commit()
        hits2 = [r[0] for r in pack.db.execute("SELECT title FROM pages_fts WHERE pages_fts MATCH 'gym'")]
        assert "Eggs" in hits2


def test_pack_images(tmp_path: pathlib.Path):
    with CorpusPack("c", "s2", packs_dir=tmp_path) as pack:
        pack.upsert_image(name="P.png", url="https://img/P.png", width=256, mime="image/png")
        pack.upsert_image(name="P.png", url="https://img/P_v2.png")
        pack.commit()
        assert pack.counts()["images"] == 1
        assert pack.db.execute("SELECT url FROM images WHERE name='P.png'").fetchone()[0].endswith("P_v2.png")
