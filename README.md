# TikTok Slideshow Scraper

A small, self-hosted `aiohttp` microservice that extracts TikTok
**slideshow / photo posts** (and, optionally, videos) — a drop-in replacement
for a paid third-party scraper API, with no per-request billing and no vendor
lock-in.

It combines `yt-dlp` (fast profile listing + video metadata) with a headless
Playwright browser (search / hashtag discovery and page-data extraction), and
uses several fallback strategies to stay resilient to TikTok's frequent
front-end changes.

## Why

Paid scraping APIs are convenient but expensive and rate-limited. This service
exposes the same JSON shape the downstream pipeline already expected, so it can
be swapped in behind the same integration with zero changes upstream.

## Features

- **Slideshow extraction** — pulls the individual image URLs from TikTok photo
  posts (carousels).
- **Multi-strategy HTML parsing** — tries `__UNIVERSAL_DATA_FOR_REHYDRATION__`,
  then `SIGI_STATE`, then `og:image`, so a single markup change doesn't break it.
- **Search & hashtag discovery** via Playwright, including live interception of
  TikTok's internal search API responses, with page-data and DOM fallbacks.
- **Video mode** — filter by views/duration for video-oriented pipelines.
- **Instagram endpoint** — bonus profile scraping via Instagram's public web
  profile API, with optional proxy-pool rotation.
- **Proxy support** — a single upstream proxy or a JSON proxy pool.

## Endpoints

Runs on port `3201`.

| Method | Path                       | Purpose |
|--------|----------------------------|---------|
| `GET`  | `/health`                  | Health check |
| `POST` | `/scrape`                  | Slideshows from a list of profiles |
| `POST` | `/scrape-post`             | Slideshow images from a single post URL |
| `POST` | `/scrape-search`           | Discovery by search queries (Playwright) |
| `POST` | `/scrape-hashtag`          | Discovery by hashtags (Playwright) |
| `POST` | `/search-videos`           | Videos by search query, filtered by views/duration |
| `POST` | `/scrape-profile-videos`   | Videos from profiles (yt-dlp) |
| `POST` | `/scrape-instagram`        | Instagram posts from profiles (bonus) |

### Examples

```bash
# Health
curl localhost:3201/health

# Slideshows from profiles
curl -X POST localhost:3201/scrape \
  -H 'Content-Type: application/json' \
  -d '{"profiles": ["someuser"], "max_posts": 5, "min_engagement": 0}'

# A single photo post
curl -X POST localhost:3201/scrape-post \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://www.tiktok.com/@someuser/photo/123"}'

# Discover slideshows by hashtag
curl -X POST localhost:3201/scrape-hashtag \
  -H 'Content-Type: application/json' \
  -d '{"hashtags": ["codingtips"], "max_results": 10, "filter": "slideshows"}'
```

A slideshow result looks like:

```json
{
  "carouselId": "123",
  "sourceUrl": "https://www.tiktok.com/@someuser/photo/123",
  "author": "someuser",
  "engagement": {"likes": 0, "comments": 0, "views": 0, "total": 0},
  "description": "...",
  "images": ["https://...jpg", "https://...jpg"],
  "imageCount": 2
}
```

## Setup

```bash
pip install -r requirements.txt
python3 -m playwright install chromium
cp .env.example .env      # optional — all vars have sane defaults
python3 scraper.py
```

Requirements: Python 3.10+, `yt-dlp` and a Playwright Chromium install.

## Configuration

Everything is environment-driven (see `.env.example`) — no secrets are
hardcoded. Optional `PROXY` (single upstream) or `PROXY_POOL_CONFIG` (JSON pool)
enable proxying; `MAX_POSTS` / `MIN_ENGAGEMENT` set request defaults.

## Notes

This is a sanitized portfolio version of a private project. Hardcoded paths and
environment-specific details have been replaced with environment variables.
