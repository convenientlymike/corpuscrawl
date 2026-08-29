"""store.py — the SQLite + FTS5 corpus pack (one file per source, offline, full-text searchable).

Layout: ``<packs-dir>/<collection>/<source-id>/pack.sqlite`` where packs-dir defaults to
``~/.corpuscrawl/packs`` (override with $CORPUSCRAWL_PACKS or the ``packs_dir`` arg). A *collection*
is any namespace you choose (a project, a topic, a game) — the engine is domain-agnostic.

Tables:
  meta(key, value)                        -- provenance (scraped_at, source_url, generator, counts, ...)
  pages(id, source_id, title, url, categories_json, sections_json, wikitext, plaintext, revid,
        length, kind, fetched_at)         -- one row per article/page; UPSERT by (source_id, title)
  pages_fts (fts5, external-content)      -- title+plaintext full-text index for the reader's search
  images(name, url, width, height, mime, descriptionurl, pages_json)  -- URLs+metadata only (no binaries)
  checkpoints(key, value)                 -- resumable continue-tokens / phase progress

Resumable + idempotent: re-running upserts by (source_id, title); enumeration phases store their
MediaWiki ``continue`` token in ``checkpoints`` so a crash/sleep/SIGINT resumes mid-crawl.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any


def default_packs_dir() -> Path:
    """Where packs live by default: $CORPUSCRAWL_PACKS or ~/.corpuscrawl/packs."""
    env = os.environ.get("CORPUSCRAWL_PACKS")
    return Path(env) if env else (Path.home() / ".corpuscrawl" / "packs")

_DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS pages (
    id            INTEGER PRIMARY KEY,
    source_id     TEXT NOT NULL,
    title         TEXT NOT NULL,
    url           TEXT,
    categories_json TEXT,
    sections_json TEXT,
    wikitext      TEXT,
    plaintext     TEXT,
    revid         INTEGER,
    length        INTEGER,
    kind          TEXT,
    fetched_at    REAL,
    UNIQUE(source_id, title)
);
CREATE INDEX IF NOT EXISTS idx_pages_source ON pages(source_id);
CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
    title, plaintext, content='pages', content_rowid='id', tokenize='porter unicode61'
);
CREATE TABLE IF NOT EXISTS images (
    name          TEXT PRIMARY KEY,
    url           TEXT,
    width         INTEGER,
    height        INTEGER,
    mime          TEXT,
    descriptionurl TEXT,
    pages_json    TEXT
);
CREATE TABLE IF NOT EXISTS checkpoints (key TEXT PRIMARY KEY, value TEXT);
"""


def pack_root(collection: str, source_id: str, packs_dir: Path | None = None) -> Path:
    base = packs_dir or default_packs_dir()
    return base / collection / source_id


class CorpusPack:
    """A resumable SQLite pack for one (collection, source). Open, upsert pages/images, checkpoint, close."""

    def __init__(self, collection: str, source_id: str, packs_dir: Path | None = None):
        self.collection = collection
        self.source_id = source_id
        self.dir = pack_root(collection, source_id, packs_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "pack.sqlite"
        self.db = sqlite3.connect(str(self.path))
        self.db.executescript(_DDL)
        # WAL + a bounded journal so a read-mostly pack never grows an unbounded WAL file.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA journal_size_limit=8388608")
        self.db.commit()

    # ── provenance ────────────────────────────────────────────────────────────────────────────
    def set_meta(self, **kv: Any) -> None:
        for k, v in kv.items():
            self.db.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, json.dumps(v) if not isinstance(v, str) else v),
            )
        self.db.commit()

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return row[0]

    # ── checkpoints (resumable continue-tokens) ─────────────────────────────────────────────────
    def set_checkpoint(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT INTO checkpoints(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
        self.db.commit()

    def get_checkpoint(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM checkpoints WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    # ── pages ───────────────────────────────────────────────────────────────────────────────────
    def has_page(self, title: str) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM pages WHERE source_id=? AND title=?", (self.source_id, title)
            ).fetchone()
            is not None
        )

    def upsert_page(
        self,
        *,
        title: str,
        url: str | None = None,
        categories: list[str] | None = None,
        sections: list[dict] | None = None,
        wikitext: str | None = None,
        plaintext: str | None = None,
        revid: int | None = None,
        length: int | None = None,
        kind: str = "article",
    ) -> None:
        cur = self.db.execute(
            """INSERT INTO pages(source_id,title,url,categories_json,sections_json,wikitext,plaintext,revid,length,kind,fetched_at)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source_id,title) DO UPDATE SET
                 url=excluded.url, categories_json=excluded.categories_json, sections_json=excluded.sections_json,
                 wikitext=excluded.wikitext, plaintext=excluded.plaintext, revid=excluded.revid,
                 length=excluded.length, kind=excluded.kind, fetched_at=excluded.fetched_at""",
            (
                self.source_id, title, url,
                json.dumps(categories or []), json.dumps(sections or []),
                wikitext, plaintext, revid, length, kind, time.time(),
            ),
        )
        rowid = cur.lastrowid
        # keep the external-content FTS index in sync (delete+insert is the robust upsert for fts5 external-content)
        if rowid:
            self.db.execute(
                "INSERT INTO pages_fts(rowid, title, plaintext) VALUES(?,?,?)",
                (rowid, title, plaintext or ""),
            )
        else:  # updated an existing row — rebuild that row's fts entry
            r = self.db.execute(
                "SELECT id FROM pages WHERE source_id=? AND title=?", (self.source_id, title)
            ).fetchone()
            if r:
                self.db.execute("INSERT INTO pages_fts(pages_fts, rowid, title, plaintext) VALUES('delete',?,?,?)",
                                (r[0], title, plaintext or ""))
                self.db.execute("INSERT INTO pages_fts(rowid, title, plaintext) VALUES(?,?,?)",
                                (r[0], title, plaintext or ""))

    # ── images (URLs + metadata only — no binaries) ─────────────────────────────────────────────
    def upsert_image(self, *, name: str, url: str, width: int | None = None, height: int | None = None,
                     mime: str | None = None, descriptionurl: str | None = None) -> None:
        self.db.execute(
            """INSERT INTO images(name,url,width,height,mime,descriptionurl,pages_json) VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET url=excluded.url, width=excluded.width, height=excluded.height,
                 mime=excluded.mime, descriptionurl=excluded.descriptionurl""",
            (name, url, width, height, mime, descriptionurl, json.dumps([])),
        )

    # ── counts + lifecycle ──────────────────────────────────────────────────────────────────────
    def counts(self) -> dict[str, int]:
        p = self.db.execute("SELECT COUNT(*) FROM pages WHERE source_id=?", (self.source_id,)).fetchone()[0]
        i = self.db.execute("SELECT COUNT(*) FROM images").fetchone()[0]
        return {"pages": p, "images": i}

    def commit(self) -> None:
        self.db.commit()

    def close(self) -> None:
        try:
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        self.db.commit()
        self.db.close()

    def __enter__(self) -> CorpusPack:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
