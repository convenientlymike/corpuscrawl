"""core.py — the shared scrape engine: politeness + a tiered fetcher + readable extraction.

TieredFetcher.get(url) escalates automatically:
  1. static  — a polite httpx GET (honest UA, rate-limit + jitter, retry/backoff on 429/503,
     Retry-After honored, robots.txt aware).
  2. browser — when the static response is an anti-bot wall (Cloudflare "just a moment", a JS
     challenge) or an empty JS/SPA shell, escalate to a REAL Chrome-for-Testing render (Playwright
     + the cached CfT binary, keychain-safe flags, ONE fresh reused instance per run). This is the
     legitimate "bypass" tier — a real browser rendering PUBLIC content.

The MediaWiki adapter does NOT use the browser tier (its API is plain HTTPS); the browser tier only
fires for a `web` source behind JS/Cloudflare.
"""
from __future__ import annotations

import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

log = logging.getLogger("corpuscrawl")

# Honest, contactable UA — MediaWiki/Fandom etiquette asks for identification + purpose.
USER_AGENT = os.environ.get(
    "CORPUSCRAWL_UA",
    "corpuscrawl/1.0 (+https://github.com/convenientlymike/corpuscrawl)",
)

# Markers that mean "the static body is a wall / not the real content" → escalate to a browser.
_WALL_MARKERS = (
    "just a moment", "cf-browser-verification", "checking your browser", "cf-challenge",
    "attention required", "enable javascript and cookies", "__cf_chl", "captcha-delivery",
    "please turn javascript on", "ddos protection by",
)


@dataclass
class Throttle:
    """Rate-limit with jitter. Call .wait() before each request to a source."""

    min_interval_s: float = 0.6
    jitter_s: float = 0.4
    _last: float = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        gap = now - self._last
        target = self.min_interval_s + random.uniform(0, self.jitter_s)
        if gap < target:
            time.sleep(target - gap)
        self._last = time.monotonic()


# ── a CORRECT robots.txt matcher (Google spec) — stdlib urllib.robotparser mishandles Allow: / with a
#    wildcard Disallow (it false-negatives an explicitly-allowed path). We implement longest-match-wins
#    with Allow breaking ties and *,$ wildcards; self-contained + portable. ────────────────────────────

class _RobotsGroup:
    __slots__ = ("allows", "disallows")

    def __init__(self) -> None:
        self.allows: list[tuple[int, re.Pattern]] = []
        self.disallows: list[tuple[int, re.Pattern]] = []


def _robots_pattern_to_re(pat: str) -> re.Pattern:
    """A robots path pattern (with * and a trailing $) → a regex matching from the path start."""
    end_anchor = pat.endswith("$")
    if end_anchor:
        pat = pat[:-1]
    rx = "".join(".*" if ch == "*" else re.escape(ch) for ch in pat)
    return re.compile(rx + ("$" if end_anchor else ""))


def parse_robots(text: str) -> dict[str, _RobotsGroup]:
    groups: dict[str, _RobotsGroup] = {}
    pending: list[str] = []
    seen_rule = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field_name, _, val = line.partition(":")
        field_name = field_name.strip().lower()
        val = val.strip()
        if field_name == "user-agent":
            if seen_rule:  # a rule already landed → this UA starts a NEW group
                pending = []
                seen_rule = False
            pending.append(val.lower())
            groups.setdefault(val.lower(), _RobotsGroup())
        elif field_name in ("allow", "disallow"):
            seen_rule = True
            if not pending or val == "":  # empty value = no constraint
                continue
            entry = (len(val), _robots_pattern_to_re(val))
            for ua in pending:
                g = groups.setdefault(ua, _RobotsGroup())
                (g.allows if field_name == "allow" else g.disallows).append(entry)
    return groups


def robots_can_fetch(groups: dict[str, _RobotsGroup], ua: str, path: str) -> bool:
    ua_l = ua.lower()
    best: _RobotsGroup | None = None
    best_len = -1
    for token, g in groups.items():
        if token == "*":
            continue
        if token and token in ua_l and len(token) > best_len:
            best, best_len = g, len(token)
    if best is None:
        best = groups.get("*")
    if best is None:
        return True
    allow_len = max((L for L, rx in best.allows if rx.match(path)), default=-1)
    dis_len = max((L for L, rx in best.disallows if rx.match(path)), default=-1)
    if dis_len < 0:
        return True
    return allow_len >= dis_len  # Allow wins ties (Google spec)


class Robots:
    """robots.txt awareness, cached per host, using the correct Google-spec matcher. Fail-open (allow)
    when robots is unreachable — we are already polite (rate-limited, identified). Set
    CORPUSCRAWL_IGNORE_ROBOTS=1 to skip entirely."""

    def __init__(self) -> None:
        self._cache: dict[str, dict[str, _RobotsGroup] | None] = {}
        self._ignore = os.environ.get("CORPUSCRAWL_IGNORE_ROBOTS", "") == "1"

    def _load(self, host: str) -> dict[str, _RobotsGroup] | None:
        try:
            r = httpx.get(urljoin(host, "/robots.txt"),
                          headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=15.0)
            if r.status_code >= 400 or not r.text:
                return {}  # no/empty robots → allow all
            return parse_robots(r.text)
        except httpx.HTTPError as exc:
            log.debug("robots.txt unreadable for %s (%s) — allowing", host, exc)
            return None

    def can_fetch(self, url: str) -> bool:
        if self._ignore:
            return True
        parsed = urlparse(url)
        host = f"{parsed.scheme}://{parsed.netloc}"
        if host not in self._cache:
            self._cache[host] = self._load(host)
        groups = self._cache[host]
        if groups is None:  # fetch failed → fail-open
            return True
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        return robots_can_fetch(groups, USER_AGENT, path)


def looks_like_wall(status: int, body: str) -> bool:
    """True when a static response is an anti-bot wall or an empty JS shell that needs a browser."""
    low = body[:4000].lower()
    if any(m in low for m in _WALL_MARKERS):
        return True
    if status in (403, 429, 503) and ("cloudflare" in low or "<title>attention" in low):
        return True
    # near-empty SPA shell: a root mount + a script bundle, almost no readable text.
    if status == 200 and len(body) < 20000:
        text_chars = sum(c.isalpha() for c in body)
        has_root = ('id="root"' in body or 'id="app"' in body or 'id="__next"' in body)
        if has_root and "<script" in body and text_chars < 500:
            return True
    return False


@dataclass
class TieredFetcher:
    """Static httpx with automatic Chrome-for-Testing escalation. Reuse across a whole crawl run."""

    throttle: Throttle = field(default_factory=Throttle)
    robots: Robots = field(default_factory=Robots)
    timeout_s: float = 30.0
    allow_browser: bool = True
    _client: httpx.Client | None = None
    _browser: Any = None          # playwright Browser (lazy)
    _pw: Any = None               # playwright context manager (lazy)
    _browser_failed: bool = False

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
                follow_redirects=True,
                timeout=self.timeout_s,
            )
        return self._client

    def get_json(self, url: str, params: dict | None = None, *, max_retries: int = 5) -> Any:
        """Polite JSON GET (the MediaWiki API path — no browser tier). Retries on 429/503/5xx."""
        self.throttle.wait()
        backoff = 1.0
        for attempt in range(max_retries):
            try:
                r = self._http().get(url, params=params)
                if r.status_code in (429, 503):
                    retry_after = float(r.headers.get("Retry-After", backoff))
                    log.warning("%s -> %s; backing off %.1fs", url, r.status_code, retry_after)
                    time.sleep(min(retry_after, 30.0))
                    backoff *= 2
                    continue
                r.raise_for_status()
                return r.json()
            except (httpx.HTTPError, ValueError) as exc:
                if attempt == max_retries - 1:
                    raise
                log.warning("get_json %s failed (%s) — retry %d", url, exc, attempt + 1)
                time.sleep(backoff)
                backoff *= 2
        raise RuntimeError("unreachable")

    def get_html(self, url: str, *, max_retries: int = 4) -> tuple[str, str]:
        """Return (html, tier) where tier in {'static','browser','blocked'}. Escalates on a wall."""
        if not self.robots.can_fetch(url):
            log.info("robots.txt disallows %s — skipping", url)
            return ("", "blocked")
        self.throttle.wait()
        backoff = 1.0
        body, status = "", 0
        for attempt in range(max_retries):
            try:
                r = self._http().get(url)
                status, body = r.status_code, r.text
                if status in (429, 503):
                    ra = float(r.headers.get("Retry-After", backoff))
                    time.sleep(min(ra, 30.0))
                    backoff *= 2
                    continue
                break
            except httpx.HTTPError as exc:
                if attempt == max_retries - 1:
                    log.warning("get_html %s failed after retries (%s)", url, exc)
                    body, status = "", 0
                    break
                time.sleep(backoff)
                backoff *= 2
        if body and not looks_like_wall(status, body):
            return (body, "static")
        # escalate to a real browser
        if self.allow_browser and not self._browser_failed:
            rendered = self._render(url)
            if rendered:
                return (rendered, "browser")
        return (body, "static" if body else "blocked")

    # ── browser escalation (Playwright + Chrome for Testing) ────────────────────────────────────
    def _cft_binary(self) -> str | None:
        cache = Path.home() / "Library/Caches/ms-playwright"
        if not cache.is_dir():
            return None
        cands = sorted(
            (d for d in cache.iterdir() if d.name.startswith("chromium-") and "headless" not in d.name),
            reverse=True,
        )
        for d in cands:
            for leaf in (
                d / "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
                d / "chrome-mac/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
                d / "chrome-linux/chrome",
            ):
                if leaf.exists():
                    return str(leaf)
        return None

    def _ensure_browser(self) -> Any:
        if self._browser is not None:
            return self._browser
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            log.warning("playwright not available — browser escalation disabled")
            self._browser_failed = True
            return None
        binary = os.environ.get("CHROME_FOR_TESTING_BIN") or self._cft_binary()
        if not binary:
            log.warning("no Chrome-for-Testing binary found — browser escalation disabled")
            self._browser_failed = True
            return None
        try:
            self._pw = sync_playwright().start()
            # headless:false per browser doctrine; keychain-safe flags mandatory on macOS; ONE fresh
            # reused instance for the whole run (fresh user-data-dir, navigates many pages sequentially).
            self._browser = self._pw.chromium.launch(
                headless=False,
                executable_path=binary,
                args=[
                    "--use-mock-keychain", "--password-store=basic",
                    "--no-first-run", "--no-default-browser-check",
                ],
            )
            log.info("browser escalation ready (Chrome for Testing: %s)", binary)
            return self._browser
        except Exception as exc:  # noqa: BLE001
            log.warning("browser launch failed (%s) — escalation disabled", exc)
            self._browser_failed = True
            return None

    def _render(self, url: str) -> str:
        browser = self._ensure_browser()
        if browser is None:
            return ""
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            page.goto(url, wait_until="networkidle", timeout=int(self.timeout_s * 1000))
            html = page.content()
            page.close()
            log.info("browser-rendered %s (%d bytes)", url, len(html))
            return html
        except Exception as exc:  # noqa: BLE001
            log.warning("browser render failed for %s (%s)", url, exc)
            return ""

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:  # noqa: BLE001
                pass
            self._browser = None
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            self._pw = None

    def __enter__(self) -> TieredFetcher:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# ── readable content extraction ─────────────────────────────────────────────────────────────────

def extract_readable(html: str, url: str = "") -> tuple[str, str]:
    """Return (title, plaintext) from an HTML page. Uses trafilatura if present (best), else lxml."""
    if not html:
        return ("", "")
    # best-in-class if available (optional dep)
    try:
        import trafilatura  # type: ignore

        text = trafilatura.extract(html, include_comments=False, include_tables=True, url=url or None)
        title = ""
        meta = trafilatura.extract_metadata(html)
        if meta and getattr(meta, "title", None):
            title = meta.title
        if text:
            return (title, text)
    except Exception:  # noqa: BLE001 — fall through to lxml
        pass
    # solid fallback: lxml — strip chrome, take the largest text block.
    try:
        from lxml import html as lhtml

        doc = lhtml.fromstring(html)
        title = ""
        t = doc.find(".//title")
        if t is not None and t.text:
            title = t.text.strip()
        for tag in doc.xpath("//script|//style|//nav|//header|//footer|//aside|//noscript|//form"):
            tag.getparent().remove(tag)
        # prefer a <main>/<article> if present, else body
        node = None
        for xp in ("//main", "//article", "//div[@id='content']", "//div[@role='main']"):
            found = doc.xpath(xp)
            if found:
                node = found[0]
                break
        if node is None:
            node = doc.find(".//body") if doc.find(".//body") is not None else doc
        text = "\n".join(s.strip() for s in node.itertext() if s.strip())
        return (title, text)
    except Exception as exc:  # noqa: BLE001
        log.debug("lxml extract failed for %s (%s)", url, exc)
        return ("", "")
