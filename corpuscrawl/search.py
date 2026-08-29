"""search.py — full-text search over a corpus pack (reusable by the CLI or any consumer).

Uses the pack's FTS5 index with BM25 ranking + a highlighted snippet. Returns plain dicts so a caller
(a CLI, a web API, a notebook) can render them however it likes.
"""
from __future__ import annotations

import sqlite3

from .store import CorpusPack


def _fts_query(user_query: str) -> str:
    """Turn a free-text query into a safe FTS5 MATCH expression (quote each term, AND them). This
    avoids FTS5 syntax errors on punctuation and treats the query as an all-terms search."""
    terms = [t for t in "".join(c if (c.isalnum() or c.isspace()) else " " for c in user_query).split() if t]
    if not terms:
        return '""'
    return " AND ".join(f'"{t}"' for t in terms)


def search_pack(pack: CorpusPack, query: str, *, limit: int = 20, snippet_tokens: int = 12) -> list[dict]:
    """Return ranked hits: {title, url, source_id, snippet, kind}. Empty query → empty list."""
    match = _fts_query(query)
    if match == '""':
        return []
    try:
        rows = pack.db.execute(
            """SELECT p.title, p.url, p.source_id, p.kind,
                      snippet(pages_fts, 1, '[', ']', ' … ', ?) AS snip
                 FROM pages_fts
                 JOIN pages p ON p.id = pages_fts.rowid
                WHERE pages_fts MATCH ?
                ORDER BY bm25(pages_fts)
                LIMIT ?""",
            (snippet_tokens, match, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [
        {"title": r[0], "url": r[1], "source_id": r[2], "kind": r[3], "snippet": (r[4] or "").strip()}
        for r in rows
    ]


def get_page(pack: CorpusPack, title: str) -> dict | None:
    """Return the full stored page for a title (or None)."""
    import json

    r = pack.db.execute(
        "SELECT title, url, categories_json, sections_json, plaintext, wikitext, revid, kind "
        "FROM pages WHERE title = ? LIMIT 1",
        (title,),
    ).fetchone()
    if not r:
        return None
    return {
        "title": r[0], "url": r[1],
        "categories": json.loads(r[2] or "[]"), "sections": json.loads(r[3] or "[]"),
        "plaintext": r[4], "wikitext": r[5], "revid": r[6], "kind": r[7],
    }
