# Contributing

Thanks for your interest in corpuscrawl.

## Development setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"        # + [all] for the browser/trafilatura/yaml extras
```

## Before you open a PR

```bash
ruff check corpuscrawl tests   # lint (matches CI)
pytest -q                      # tests (no network — all offline)
```

Both must pass; CI runs the same on Python 3.10–3.12.

## Guidelines

- **Keep the engine domain-agnostic.** `core`, `store`, `adapters`, and `search` must know nothing about
  any specific site or domain — sources are pure config (`config.py`).
- **New adapter?** Add a class in `adapters.py` with a `type` and a `crawl(pack, *, max_pages)` method,
  register it in `ADAPTERS`, and add offline unit tests.
- **Tests are forcing functions.** A new behavior lands with a test that would fail without it. Tests
  must not hit the network — seed a `CorpusPack(tmp_path)` or parse fixtures directly.
- **Be a good citizen.** Anything touching fetching keeps the politeness + robots guarantees intact.
- Conventional-Commits style for messages (`feat:`, `fix:`, `docs:`), and update `CHANGELOG.md`
  (`[Unreleased]`) in the same PR as a user-facing change.
