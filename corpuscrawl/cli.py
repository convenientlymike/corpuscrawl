"""cli.py — the corpuscrawl command line.

  corpuscrawl crawl --config sources.json                 # crawl every scrapeable source
  corpuscrawl crawl --url https://www.mediawiki.org --type mediawiki --collection docs  # quickstart, no file
  corpuscrawl search "parser functions" --collection docs # full-text search the pack(s)
  corpuscrawl list [--collection docs]                    # collections / sources + pack stats
  corpuscrawl info --collection docs --source mediawiki-org   # provenance
  corpuscrawl reprocess --config sources.json            # re-derive plaintext from stored wikitext
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from .adapters import adapter_for, reprocess_pack
from .config import ConfigError, load_config, sources_from_url
from .core import Throttle, TieredFetcher
from .search import search_pack
from .store import CorpusPack, default_packs_dir


def _log(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")


def _resolve_sources(args) -> tuple[str, list[dict]]:
    if getattr(args, "url", None):
        coll = args.collection or "default"
        return coll, sources_from_url(args.url, type=args.type, id=args.source)
    if not getattr(args, "config", None):
        raise ConfigError("provide --config <file> or --url <url> --type <type>")
    return load_config(args.config, collection=args.collection)


def _packs_dir(args) -> Path:
    return Path(args.packs_dir) if getattr(args, "packs_dir", None) else default_packs_dir()


# ── crawl ─────────────────────────────────────────────────────────────────────────────────────────

def cmd_crawl(args) -> int:
    collection, sources = _resolve_sources(args)
    if args.source and not args.url:
        sources = [s for s in sources if s["id"] == args.source]
        if not sources:
            print(f"no source '{args.source}' in the config", file=sys.stderr)
            return 2
    scrapeable = [s for s in sources if s.get("scrape") and s["type"] in ("mediawiki", "web")]
    if not scrapeable:
        print("no scrapeable sources (mediawiki/web with scrape=true)")
        return 0
    fetcher = TieredFetcher(throttle=Throttle(min_interval_s=args.min_interval),
                            allow_browser=not args.no_browser)
    rc = 0
    try:
        for s in scrapeable:
            print(f"\n=== crawl {collection}/{s['id']} [{s['type']}] {s['url']} ===")
            adapter = adapter_for(s, fetcher)
            if adapter is None:
                print(f"  no adapter for type '{s['type']}' — skipping")
                continue
            t0 = time.time()
            with CorpusPack(collection, s["id"], _packs_dir(args)) as pack:
                try:
                    r = adapter.crawl(pack, max_pages=args.max_pages)
                    print(f"  +{r['pages_added']} pages this run ({r['pages']} total, {r['images']} images) "
                          f"in {time.time() - t0:.0f}s\n  pack: {pack.path}")
                except KeyboardInterrupt:
                    print("  interrupted — checkpoint saved; re-run to resume")
                    rc = 130
                    break
                except Exception as exc:  # noqa: BLE001
                    logging.getLogger("corpuscrawl").exception("crawl failed")
                    print(f"  FAILED: {exc} (checkpoint saved; re-run to resume)")
                    rc = 1
    finally:
        fetcher.close()
    return rc


# ── search ──────────────────────────────────────────────────────────────────────────────────────

def _collections(packs: Path) -> list[str]:
    return sorted(d.name for d in packs.iterdir() if d.is_dir()) if packs.is_dir() else []


def _source_ids(packs: Path, collection: str) -> list[str]:
    cdir = packs / collection
    return sorted(d.name for d in cdir.iterdir() if (d / "pack.sqlite").exists()) if cdir.is_dir() else []


def cmd_search(args) -> int:
    packs = _packs_dir(args)
    collections = [args.collection] if args.collection else _collections(packs)
    if not collections:
        print("no collections found — crawl something first", file=sys.stderr)
        return 2
    hits: list[dict] = []
    for coll in collections:
        for sid in ([args.source] if args.source else _source_ids(packs, coll)):
            if not (packs / coll / sid / "pack.sqlite").exists():
                continue
            with CorpusPack(coll, sid, packs) as pack:
                for h in search_pack(pack, args.query, limit=args.limit):
                    h["collection"] = coll
                    hits.append(h)
    hits = hits[: args.limit]
    if not hits:
        print(f"no results for '{args.query}'")
        return 0
    for h in hits:
        print(f"\n\033[1m{h['title']}\033[0m  ({h['collection']}/{h['source_id']})")
        print(f"  {h['url']}")
        if h["snippet"]:
            print(f"  {h['snippet']}")
    print(f"\n{len(hits)} result(s)")
    return 0


# ── list / info / reprocess ───────────────────────────────────────────────────────────────────────

def cmd_list(args) -> int:
    packs = _packs_dir(args)
    collections = [args.collection] if args.collection else _collections(packs)
    if not collections:
        print(f"no collections in {packs}")
        return 0
    for coll in collections:
        print(f"\ncollection: {coll}")
        for sid in _source_ids(packs, coll):
            with CorpusPack(coll, sid, packs) as pack:
                c = pack.counts()
                print(f"  • {sid:24s} {c['pages']:>6} pages  {c['images']:>6} images")
    return 0


def cmd_info(args) -> int:
    packs = _packs_dir(args)
    if not args.collection:
        print("--collection required", file=sys.stderr)
        return 2
    for sid in ([args.source] if args.source else _source_ids(packs, args.collection)):
        p = packs / args.collection / sid / "pack.sqlite"
        if not p.exists():
            continue
        with CorpusPack(args.collection, sid, packs) as pack:
            print(f"\n{args.collection}/{sid}  ({pack.path})")
            for key in ("source_type", "source_url", "generator", "sitename", "site_articles", "site_images"):
                v = pack.get_meta(key)
                if v is not None:
                    print(f"  {key:14s} {v}")
            c = pack.counts()
            print(f"  {'pages':14s} {c['pages']}")
            print(f"  {'images':14s} {c['images']}")
    return 0


def cmd_reprocess(args) -> int:
    collection, sources = _resolve_sources(args)
    total = 0
    for s in sources:
        if not s.get("scrape"):
            continue
        with CorpusPack(collection, s["id"], _packs_dir(args)) as pack:
            n = reprocess_pack(pack)
            total += n
            print(f"  reprocessed {collection}/{s['id']}: {n} pages")
    print(f"done: {total} pages reprocessed (no network)")
    return 0


# ── parser ──────────────────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="corpuscrawl", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--packs-dir", help=f"where packs live (default: {default_packs_dir()})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_common(p):
        p.add_argument("--config", help="sources config (JSON/YAML)")
        p.add_argument("--collection", help="collection name (namespace for the packs)")

    c = sub.add_parser("crawl", help="crawl sources into packs")
    add_common(c)
    c.add_argument("--url", help="quickstart: crawl a single URL (with --type)")
    c.add_argument("--type", default="mediawiki", choices=["mediawiki", "web", "tool"])
    c.add_argument("--source", help="only this source id")
    c.add_argument("--max-pages", type=int, help="cap pages this run")
    c.add_argument("--no-browser", action="store_true", help="disable the browser-escalation tier")
    c.add_argument("--min-interval", type=float, default=0.6, help="min seconds between requests")
    c.set_defaults(func=cmd_crawl)

    s = sub.add_parser("search", help="full-text search the pack(s)")
    s.add_argument("query")
    s.add_argument("--collection")
    s.add_argument("--source")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_search)

    li = sub.add_parser("list", help="list collections / sources + stats")
    li.add_argument("--collection")
    li.set_defaults(func=cmd_list)

    inf = sub.add_parser("info", help="show a source's provenance")
    inf.add_argument("--collection", required=True)
    inf.add_argument("--source")
    inf.set_defaults(func=cmd_info)

    rp = sub.add_parser("reprocess", help="re-derive plaintext from stored wikitext (no network)")
    add_common(rp)
    rp.set_defaults(func=cmd_reprocess)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    _log(args.verbose)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
