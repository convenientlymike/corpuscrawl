# Security Policy

## Reporting a vulnerability

Please open a private security advisory on GitHub (Security → Advisories) or email the maintainer.
Do not open a public issue for a security report.

## Scope & responsible-use notes

corpuscrawl is a scraper for **public** content, built to be a good citizen:

- **Politeness is on by default** — a rate-limit with jitter, an honest identifiable `User-Agent`, and
  exponential backoff on `429/503`. Tune `--min-interval` up for sensitive sources.
- **robots.txt is respected** by default via a correct Google-spec matcher. `CORPUSCRAWL_IGNORE_ROBOTS=1`
  exists only for sources you own or are explicitly authorized to crawl.
- **The browser-escalation tier renders public pages** to defeat JS/anti-bot gating. It is **not** a
  login, paywall, or authentication bypass, and must not be used as one.
- **No secrets, no telemetry.** corpuscrawl makes no network calls beyond the sources you configure and
  writes only to your local packs directory.

You are responsible for complying with the terms of service and applicable law for any source you crawl.

## Supported versions

The latest released `1.x` line receives security fixes.
