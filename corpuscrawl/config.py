"""config.py — load a generic sources configuration (the ONLY place the engine learns *what* to crawl).

A config is domain-agnostic — a collection name plus a list of source specs:

    {
      "collection": "pokemon-go",
      "sources": [
        {"id": "fandom-wiki", "name": "Fandom Wiki", "type": "mediawiki",
         "url": "https://pokemongo.fandom.com", "scrape": true},
        {"id": "guides", "name": "Guides", "type": "web",
         "url": "https://example.com/guides", "scrape": true},
        {"id": "tool-x", "name": "Tool X", "type": "tool", "url": "https://tool.example"}
      ]
    }

JSON always works; YAML works when PyYAML is installed. A bare list of sources is also accepted (the
collection then comes from --collection or the file stem). ``sources_from_url`` builds a one-source
config for the ``corpuscrawl crawl --url ... --type ...`` quickstart.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

VALID_TYPES = {"mediawiki", "web", "tool"}
_ID_RE = re.compile(r"^[a-z][a-z0-9-]*$")


class ConfigError(ValueError):
    """A malformed sources config."""


def validate_source(src: dict, *, where: str = "source") -> None:
    for key in ("id", "name", "type", "url"):
        if key not in src:
            raise ConfigError(f"{where}: missing required key '{key}'")
    if not _ID_RE.match(str(src["id"])):
        raise ConfigError(f"{where}: id '{src['id']}' must match ^[a-z][a-z0-9-]*$")
    if src["type"] not in VALID_TYPES:
        raise ConfigError(f"{where}: type '{src['type']}' not in {sorted(VALID_TYPES)}")
    if not str(src["url"]).startswith(("http://", "https://")):
        raise ConfigError(f"{where}: url must be http(s):// (got '{src['url']}')")
    if src.get("scrape") and src["type"] == "tool":
        raise ConfigError(f"{where}: scrape=true is invalid for a link-only 'tool' source")


def _read(path: Path) -> object:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise ConfigError("YAML config needs PyYAML (pip install corpuscrawl[yaml])") from exc
        return yaml.safe_load(text)
    return json.loads(text)


def load_config(path: str | Path, *, collection: str | None = None) -> tuple[str, list[dict]]:
    """Return (collection, sources) from a config file. Raises ConfigError on a malformed config."""
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config not found: {p}")
    data = _read(p)
    if isinstance(data, list):
        sources = data
        coll = collection or p.stem
    elif isinstance(data, dict):
        sources = data.get("sources", [])
        coll = collection or data.get("collection") or p.stem
    else:
        raise ConfigError("config must be a JSON/YAML object with 'sources' or a bare list of sources")
    if not isinstance(sources, list) or not sources:
        raise ConfigError("config has no sources")
    seen: set[str] = set()
    for i, s in enumerate(sources):
        if not isinstance(s, dict):
            raise ConfigError(f"sources[{i}] must be an object")
        validate_source(s, where=f"sources[{i}]")
        if s["id"] in seen:
            raise ConfigError(f"duplicate source id '{s['id']}'")
        seen.add(s["id"])
    return coll, sources


def sources_from_url(url: str, *, type: str = "mediawiki", id: str | None = None,
                     name: str | None = None) -> list[dict]:
    """Build a one-source list for the CLI quickstart (crawl --url ... --type ...)."""
    from urllib.parse import urlparse

    host = urlparse(url).netloc or "source"
    sid = id or re.sub(r"[^a-z0-9-]+", "-", host.lower()).strip("-") or "source"
    src = {"id": sid, "name": name or host, "type": type, "url": url, "scrape": type != "tool"}
    validate_source(src, where="--url")
    return [src]
