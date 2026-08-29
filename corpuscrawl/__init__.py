"""corpuscrawl — build an offline, full-text-searchable corpus from any set of web/wiki sources.

A universal, domain-agnostic reference scraper:
  * a TIERED fetch engine (polite static httpx → real-browser escalation on a JS/Cloudflare wall),
  * pluggable adapters by source type (MediaWiki API, generic web),
  * a resumable SQLite + FTS5 "pack" per source, organized under a *collection* you name.

Library entry points:
    from corpuscrawl import CorpusPack, TieredFetcher, adapter_for, search_pack, load_config
"""
from __future__ import annotations

from .adapters import MediaWikiAdapter, WebAdapter, adapter_for, reprocess_pack
from .config import ConfigError, load_config, sources_from_url, validate_source
from .core import Throttle, TieredFetcher, extract_readable
from .search import get_page, search_pack
from .store import CorpusPack, default_packs_dir, pack_root

__version__ = "1.0.0"

__all__ = [
    "CorpusPack", "TieredFetcher", "Throttle", "extract_readable",
    "MediaWikiAdapter", "WebAdapter", "adapter_for", "reprocess_pack",
    "ConfigError", "load_config", "sources_from_url", "validate_source",
    "search_pack", "get_page", "default_packs_dir", "pack_root", "__version__",
]
