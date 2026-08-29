"""adapters.py — pluggable scrape adapters by source ``type``.

  MediaWikiAdapter — structured crawl over the MediaWiki API (allpages generator + allimages), deriving
                     plaintext + sections from wikitext locally. No browser (the API is plain HTTPS).
  WebAdapter       — a bounded, same-prefix readable-content crawl over the TieredFetcher (which
                     escalates to a real browser on a JS/Cloudflare wall).

Both are resumable: they skip pages already in the pack and checkpoint their continue-token / visited
frontier, so a crash/sleep/SIGINT resumes mid-crawl. Source definitions come from config.py — the
engine has no knowledge of where a source list originates (a JSON/YAML file, a CLI flag, a caller).
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import quote, urljoin, urlparse

from .core import TieredFetcher, extract_readable
from .store import CorpusPack

log = logging.getLogger("corpuscrawl")


# ── wikitext → plaintext / sections (local, no extra API calls) ────────────────────────────────────

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_REF_RE = re.compile(r"<ref[^>]*>.*?</ref>|<ref[^>]*/>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_FILE_LINK_RE = re.compile(r"\[\[(?:File|Image):[^\]]*\]\]", re.IGNORECASE)
_HEADING_RE = re.compile(r"^(={2,6})\s*(.+?)\s*\1\s*$", re.MULTILINE)


_TABLE_RE = re.compile(r"\{\|.*?\|\}", re.DOTALL)  # non-greedy so nested tables clear inner-first


def _strip_templates(text: str) -> str:
    """Remove balanced {{...}} spans iteratively (handles nesting). Template delimiters are {} so a
    negated {} char-class spans param-separator | safely."""
    for _ in range(8):
        new = re.sub(r"\{\{[^{}]*?\}\}", "", text, flags=re.DOTALL)
        if new == text:
            break
        text = new
    return text


def _strip_tables(text: str) -> str:
    """Remove {|...|} tables iteratively — a dedicated non-greedy regex (table content is full of |,
    so a negated char-class approach would stop at the first row separator)."""
    for _ in range(8):
        new = _TABLE_RE.sub("", text)
        if new == text:
            break
        text = new
    return text


def wikitext_to_plaintext(wt: str) -> str:
    if not wt:
        return ""
    wt = _COMMENT_RE.sub("", wt)
    wt = _REF_RE.sub("", wt)
    wt = _FILE_LINK_RE.sub("", wt)
    wt = _strip_templates(wt)   # templates {{...}}
    wt = _strip_tables(wt)      # tables {|...|}
    # [[link|display]] -> display ; [[link]] -> link
    wt = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]|]+)\]\]", r"\1", wt)
    # [url display] -> display ; bare external links dropped
    wt = re.sub(r"\[https?://\S+\s+([^\]]+)\]", r"\1", wt)
    wt = re.sub(r"\[https?://\S+\]", "", wt)
    wt = re.sub(r"''+", "", wt)             # bold/italic markers
    wt = _HEADING_RE.sub(r"\2", wt)         # == Heading == -> Heading
    wt = _TAG_RE.sub("", wt)                # stray html
    wt = re.sub(r"^[*#:;]+\s*", "", wt, flags=re.MULTILINE)  # list bullets
    wt = re.sub(r"\n{3,}", "\n\n", wt)
    return wt.strip()


def wikitext_sections(wt: str) -> list[dict]:
    out: list[dict] = []
    for m in _HEADING_RE.finditer(wt or ""):
        out.append({"level": len(m.group(1)), "title": m.group(2).strip()})
    return out


# ── MediaWiki adapter ──────────────────────────────────────────────────────────────────────────

class MediaWikiAdapter:
    type = "mediawiki"

    def __init__(self, source: dict, fetcher: TieredFetcher):
        self.source = source
        self.fetcher = fetcher
        self.base = source["url"].rstrip("/")
        self.api = source.get("api") or f"{self.base}/api.php"
        self.namespaces = source.get("namespaces", [0, 14])  # main + Category by default

    def _api(self, **params: Any) -> Any:
        params.setdefault("format", "json")
        params.setdefault("formatversion", "2")
        return self.fetcher.get_json(self.api, params=params)

    def _page_url(self, title: str) -> str:
        return f"{self.base}/wiki/{quote(title.replace(' ', '_'))}"

    def crawl(self, pack: CorpusPack, *, max_pages: int | None = None) -> dict:
        # provenance
        try:
            info = self._api(action="query", meta="siteinfo", siprop="general|statistics")
            gen = info.get("query", {}).get("general", {})
            stats = info.get("query", {}).get("statistics", {})
            pack.set_meta(source_type="mediawiki", source_url=self.base, api=self.api,
                          generator=gen.get("generator", ""), sitename=gen.get("sitename", ""),
                          site_articles=stats.get("articles", 0), site_images=stats.get("images", 0))
        except Exception as exc:  # noqa: BLE001
            log.warning("siteinfo failed (%s) — continuing", exc)

        self._crawl_images(pack)
        added = self._crawl_pages(pack, max_pages=max_pages)
        pack.commit()
        return {"pages_added": added, **pack.counts()}

    def _crawl_images(self, pack: CorpusPack) -> None:
        cont = pack.get_checkpoint("allimages_continue", {"aifrom": None})
        if cont == "DONE":
            log.info("images already enumerated — skipping")
            return
        n = 0
        params: dict[str, Any] = dict(action="query", list="allimages", ailimit=500, aiprop="url|size|mime")
        if cont and cont.get("aifrom"):
            params["aifrom"] = cont["aifrom"]
        while True:
            data = self._api(**params)
            for img in data.get("query", {}).get("allimages", []):
                pack.upsert_image(name=img.get("name", img.get("title", "")), url=img.get("url", ""),
                                  width=img.get("width"), height=img.get("height"),
                                  mime=img.get("mime"), descriptionurl=img.get("descriptionurl"))
                n += 1
            cont = data.get("continue")
            if not cont:
                pack.set_checkpoint("allimages_continue", "DONE")
                pack.commit()
                break
            params.update(cont)
            pack.set_checkpoint("allimages_continue", cont)
            pack.commit()
            if n % 2000 == 0:
                log.info("images: %d enumerated", n)
        log.info("images: %d total", pack.counts()["images"])

    def _crawl_pages(self, pack: CorpusPack, *, max_pages: int | None) -> int:
        added = 0
        for ns in self.namespaces:
            ckpt_key = f"allpages_continue_ns{ns}"
            cont = pack.get_checkpoint(ckpt_key, {})
            if cont == "DONE":
                log.info("ns %s already crawled — skipping", ns)
                continue
            params: dict[str, Any] = dict(
                action="query", generator="allpages", gapnamespace=ns, gaplimit=50,
                gapfilterredir="nonredirects",
                prop="revisions|categories|info",
                rvprop="content|ids|timestamp", rvslots="main",
                cllimit="max", inprop="url",
            )
            if isinstance(cont, dict) and cont:
                params.update(cont)
            while True:
                data = self._api(**params)
                pages = data.get("query", {}).get("pages", [])
                for pg in pages:
                    title = pg.get("title", "")
                    if not title or pack.has_page(title):
                        continue
                    revs = pg.get("revisions", [])
                    wt = ""
                    revid = None
                    if revs:
                        slot = revs[0].get("slots", {}).get("main", {})
                        wt = slot.get("content", "") or revs[0].get("content", "")
                        revid = revs[0].get("revid")
                    cats = [c.get("title", "") for c in pg.get("categories", [])]
                    plaintext = wikitext_to_plaintext(wt)
                    pack.upsert_page(
                        title=title,
                        url=pg.get("fullurl") or self._page_url(title),
                        categories=cats, sections=wikitext_sections(wt),
                        wikitext=wt, plaintext=plaintext, revid=revid,
                        length=len(wt), kind="category" if ns == 14 else "article",
                    )
                    added += 1
                    if max_pages and added >= max_pages:
                        pack.commit()
                        log.info("reached --max-pages %d", max_pages)
                        return added
                cont = data.get("continue")
                pack.commit()
                if not cont:
                    pack.set_checkpoint(ckpt_key, "DONE")
                    pack.commit()
                    break
                params.update(cont)
                pack.set_checkpoint(ckpt_key, cont)
                pack.commit()
                if added % 200 == 0 and added:
                    log.info("pages(ns %s): %d added (%d total)", ns, added, pack.counts()["pages"])
        return added


# ── Web adapter (bounded same-prefix readable crawl) ──────────────────────────────────────────────

class WebAdapter:
    type = "web"

    def __init__(self, source: dict, fetcher: TieredFetcher):
        self.source = source
        self.fetcher = fetcher
        self.root = (source.get("guide_root") or source["url"]).rstrip("/")
        p = urlparse(self.root)
        self.host = f"{p.scheme}://{p.netloc}"
        self.prefix = self.root  # only crawl URLs under the guide root

    def _same_scope(self, url: str) -> bool:
        u = url.split("#")[0].rstrip("/")
        return u.startswith(self.prefix)

    def _links(self, html: str, base_url: str) -> Iterable[str]:
        try:
            from lxml import html as lhtml
            doc = lhtml.fromstring(html)
            for a in doc.xpath("//a/@href"):
                url = urljoin(base_url, a).split("#")[0]
                if url.startswith("http"):
                    yield url
        except Exception:  # noqa: BLE001
            return

    def crawl(self, pack: CorpusPack, *, max_pages: int | None = None) -> dict:
        pack.set_meta(source_type="web", source_url=self.source["url"], guide_root=self.root)
        limit = max_pages or 400
        visited: set[str] = set(pack.get_checkpoint("web_visited", []))
        frontier: list[str] = pack.get_checkpoint("web_frontier", [self.root])
        if pack.counts()["pages"] == 0:
            # nothing scraped yet → treat as a fresh start, ignoring a stale empty checkpoint from a
            # prior aborted/blocked run (else an empty frontier permanently no-ops the crawl).
            visited, frontier = set(), [self.root]
        added = 0
        while frontier and added < limit:
            url = frontier.pop(0)
            key = url.rstrip("/")
            if key in visited or not self._same_scope(url):
                continue
            visited.add(key)
            html, tier = self.fetcher.get_html(url)
            if not html:
                continue
            title, text = extract_readable(html, url)
            if text and len(text) > 200:  # skip empty/nav-only pages
                if not pack.has_page(title or url):
                    pack.upsert_page(title=title or url, url=url, plaintext=text,
                                     wikitext=None, kind=f"web:{tier}")
                    added += 1
            # enqueue same-scope links
            for link in self._links(html, url):
                lk = link.rstrip("/")
                if lk not in visited and self._same_scope(link) and link not in frontier:
                    frontier.append(link)
            if added % 25 == 0 and added:
                pack.set_checkpoint("web_visited", sorted(visited))
                pack.set_checkpoint("web_frontier", frontier[:2000])
                pack.commit()
                log.info("web: %d pages added (%d visited)", added, len(visited))
        pack.set_checkpoint("web_visited", sorted(visited))
        pack.set_checkpoint("web_frontier", frontier[:2000])
        pack.commit()
        return {"pages_added": added, **pack.counts()}


def reprocess_pack(pack: CorpusPack) -> int:
    """Re-derive plaintext + sections from STORED wikitext using the current extractor (no network).
    Run after improving wikitext_to_plaintext so an existing pack gets the better extraction without
    a re-crawl. Returns the number of pages updated."""
    rows = pack.db.execute(
        "SELECT title, url, categories_json, wikitext, revid, length, kind FROM pages "
        "WHERE wikitext IS NOT NULL AND wikitext != ''"
    ).fetchall()
    n = 0
    for title, url, cats_json, wt, revid, length, kind in rows:
        cats = json.loads(cats_json or "[]")
        pack.upsert_page(title=title, url=url, categories=cats, sections=wikitext_sections(wt),
                         wikitext=wt, plaintext=wikitext_to_plaintext(wt), revid=revid,
                         length=length, kind=kind or "article")
        n += 1
        if n % 500 == 0:
            pack.commit()
    pack.commit()
    return n


ADAPTERS = {"mediawiki": MediaWikiAdapter, "web": WebAdapter}


def adapter_for(source: dict, fetcher: TieredFetcher):
    kind = source.get("type")
    cls = ADAPTERS.get(kind)
    if cls is None:
        return None  # 'tool' or unknown → link-only, not scraped
    return cls(source, fetcher)
