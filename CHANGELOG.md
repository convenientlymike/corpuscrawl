# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-08-28

### Added
- **Tiered fetch engine** (`core.TieredFetcher`): polite static `httpx` (honest User-Agent, rate-limit
  + jitter, exponential backoff on `429/503` honoring `Retry-After`) with automatic escalation to a
  real Chrome-for-Testing render (Playwright) when a response is a JS/SPA shell or an anti-bot wall.
- **Correct `robots.txt` matcher** — a self-contained Google-spec implementation (longest-match wins,
  `Allow` breaks ties, `*`/`$` wildcards) that fixes the stdlib `urllib.robotparser` false-negative on
  an explicitly-allowed path behind a wildcard `Disallow`.
- **Pluggable adapters** by source type: `MediaWikiAdapter` (allpages generator + allimages, wikitext →
  plaintext/sections derived locally) and `WebAdapter` (bounded same-prefix readable crawl,
  `trafilatura` if present else `lxml`).
- **Resumable SQLite + FTS5 pack** (`store.CorpusPack`): BM25 search + highlighted snippets, WAL +
  bounded journal, idempotent upsert-by-title, checkpointed continue-tokens.
- **`config`** — a domain-agnostic sources loader (JSON/YAML, or a `--url` quickstart) with validation.
- **`search`** — BM25 full-text search + page fetch, reusable as a library API.
- **CLI** — `crawl` · `search` · `list` · `info` · `reprocess`, organized by *collection*.
- Forcing-function unit tests (no network): robots matcher, wall detection, wikitext extraction, pack
  upsert/resume/FTS, config validation, search ranking.

[Unreleased]: https://github.com/convenientlymike/corpuscrawl/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/convenientlymike/corpuscrawl/releases/tag/v1.0.0
