#!/usr/bin/env python3
"""
TikTok Slideshow Scraper — FREE Edition (yt-dlp + web parsing)
Drop-in replacement for a paid third-party TikTok scraper API.

Endpoints:
  GET  /health
  POST /scrape    {"profiles": ["user1"], "max_posts": 5, "min_engagement": 0}
  POST /scrape-post {"url": "https://www.tiktok.com/@user/photo/123"}

Port: 3201
"""

import asyncio
import json
import logging
import os
import random
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import aiohttp
from aiohttp import web
from playwright.async_api import async_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.environ.get("LOG_FILE", "scraper.log")),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("tiktok-slideshow-free")

MIN_ENGAGEMENT = int(os.environ.get("MIN_ENGAGEMENT", "0"))
MAX_POSTS = int(os.environ.get("MAX_POSTS", "10"))
RATE_DELAY = 2.0

PROXY = os.environ.get("PROXY", "")

# Playwright browser instance (reused across requests)
_browser = None
_browser_lock = asyncio.Lock()


def _yt_dlp_list_posts(username: str, max_posts: int = 10, proxy: str = None) -> list:
    """List recent posts from a TikTok profile using yt-dlp --flat-playlist."""
    cmd = [
        "yt-dlp", "--dump-json", "--flat-playlist",
        f"https://www.tiktok.com/@{username}",
        "--playlist-items", f"1-{max_posts}",
        "--no-warnings",
    ]
    if proxy:
        cmd.extend(["--proxy", proxy])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            log.warning(f"yt-dlp error for @{username}: {result.stderr[:200]}")
            return []

        posts = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                duration = data.get("duration") or 0
                post_url = data.get("url") or data.get("webpage_url", "")
                post_id = str(data.get("id", ""))

                posts.append({
                    "id": post_id,
                    "url": post_url,
                    "author": username,
                    "title": (data.get("title") or data.get("description", ""))[:500],
                    "duration": duration,
                    "views": data.get("view_count", 0),
                    "likes": data.get("like_count", 0),
                    "comments": data.get("comment_count", 0),
                    "is_slideshow": duration == 0 or "/photo/" in post_url,
                })
            except json.JSONDecodeError:
                continue

        return posts
    except subprocess.TimeoutExpired:
        log.warning(f"Timeout listing @{username}")
        return []
    except Exception as e:
        log.warning(f"Error listing @{username}: {e}")
        return []


async def _fetch_slideshow_images(session: aiohttp.ClientSession, post_url: str, proxy: str = None) -> list:
    """Fetch individual slideshow images from a TikTok post page."""
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }

    try:
        async with session.get(post_url, headers=headers, proxy=proxy, timeout=aiohttp.ClientTimeout(total=15), allow_redirects=True) as resp:
            if resp.status != 200:
                log.warning(f"Failed to fetch {post_url}: {resp.status}")
                return []

            html = await resp.text()

            # Method 1: __UNIVERSAL_DATA_FOR_REHYDRATION__
            match = re.search(r'<script[^>]*id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', html, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group(1))
                    default_scope = data.get("__DEFAULT_SCOPE__", {})
                    # Try multiple paths — TikTok changes structure
                    item_struct = {}
                    for key in ("webapp.video-detail", "webapp.reflow.video.detail"):
                        module = default_scope.get(key, {})
                        candidate = module.get("itemInfo", {}).get("itemStruct", {})
                        if candidate:
                            item_struct = candidate
                            break

                    images = []
                    image_post = item_struct.get("imagePost", {})
                    if image_post:
                        img_list = image_post.get("images", [])
                        for img in img_list:
                            url_list = img.get("imageURL", {}).get("urlList", [])
                            if url_list:
                                images.append(url_list[0])

                    if images:
                        return images
                except (json.JSONDecodeError, KeyError, TypeError) as e:
                    log.debug(f"REHYDRATION parse error: {e}")

            # Method 2: SIGI_STATE
            match2 = re.search(r'<script[^>]*id="SIGI_STATE"[^>]*>(.*?)</script>', html, re.DOTALL)
            if match2:
                try:
                    data = json.loads(match2.group(1))
                    items = data.get("ItemModule", {})
                    for item_id, item in items.items():
                        image_post = item.get("imagePost", {})
                        if image_post:
                            img_list = image_post.get("images", [])
                            images = []
                            for img in img_list:
                                url_list = img.get("imageURL", {}).get("urlList", [])
                                if url_list:
                                    images.append(url_list[0])
                            if images:
                                return images
                except (json.JSONDecodeError, KeyError, TypeError):
                    pass

            # Method 3: og:image fallback
            og_images = re.findall(r'<meta[^>]*property="og:image"[^>]*content="([^"]+)"', html)
            if og_images:
                return og_images

            return []
    except Exception as e:
        log.warning(f"Error fetching slideshow {post_url}: {e}")
        return []


async def scrape_profiles(profiles: list, max_posts: int = 10, min_engagement: int = 0) -> list:
    """Scrape multiple TikTok profiles for slideshow carousels."""
    all_carousels = []
    proxy = PROXY or None

    async with aiohttp.ClientSession() as session:
        for username in profiles:
            log.info(f"Scraping @{username}...")
            posts = _yt_dlp_list_posts(username, max_posts, proxy)
            log.info(f"  Found {len(posts)} posts from @{username}")

            # Check ALL posts — yt-dlp can't detect slideshows reliably
            # We check each post by fetching images; if 2+ images found, it's a carousel
            log.info(f"  Checking all {len(posts)} posts for carousels...")

            for post in posts:
                engagement = (post.get("likes", 0) or 0) + (post.get("comments", 0) or 0) + (post.get("views", 0) or 0)
                if engagement < min_engagement:
                    continue

                images = await _fetch_slideshow_images(session, post["url"], proxy)
                if len(images) < 2:
                    log.debug(f"  Skip {post['id']}: only {len(images)} images")
                    continue

                carousel = {
                    "carouselId": post["id"],
                    "sourceUrl": post["url"],
                    "author": post["author"],
                    "engagement": {
                        "likes": post.get("likes", 0),
                        "comments": post.get("comments", 0),
                        "views": post.get("views", 0),
                        "total": engagement,
                    },
                    "description": post.get("title", ""),
                    "images": images,
                    "imageCount": len(images),
                }
                all_carousels.append(carousel)
                log.info(f"  Carousel {post['id']}: {len(images)} images, eng={engagement}")

                await asyncio.sleep(RATE_DELAY + random.random())

            await asyncio.sleep(RATE_DELAY)

    all_carousels.sort(key=lambda c: c["engagement"]["total"], reverse=True)
    log.info(f"Total: {len(all_carousels)} carousels from {len(profiles)} profiles")
    return all_carousels


async def handle_health(request):
    return web.json_response({
        "status": "ok",
        "service": "tiktok-slideshow-scraper",
        "engine": "yt-dlp-free",
    })


async def handle_scrape(request):
    data = await request.json() if request.content_type == "application/json" else {}
    profiles = data.get("profiles", [])
    max_posts = data.get("max_posts", MAX_POSTS)
    min_engagement = data.get("min_engagement", MIN_ENGAGEMENT)

    if not profiles:
        return web.json_response({"error": "profiles required"}, status=400)

    carousels = await scrape_profiles(profiles, max_posts, min_engagement)
    return web.json_response(carousels)


async def handle_scrape_post(request):
    data = await request.json() if request.content_type == "application/json" else {}
    url = data.get("url", "")

    if not url:
        return web.json_response({"error": "url required"}, status=400)

    async with aiohttp.ClientSession() as session:
        images = await _fetch_slideshow_images(session, url)

    if not images:
        return web.json_response({"error": "Not a slideshow or no images found"}, status=404)

    post_id = re.search(r'/(?:photo|video)/(\d+)', url)
    post_id = post_id.group(1) if post_id else "unknown"
    author = re.search(r'@(\w+)', url)
    author = author.group(1) if author else "unknown"

    return web.json_response({
        "carouselId": post_id,
        "sourceUrl": url,
        "author": author,
        "images": images,
        "imageCount": len(images),
    })


# ---------------------------------------------------------------------------
# Playwright-based TikTok search (free, no API limits)
# ---------------------------------------------------------------------------

async def _get_browser():
    """Get or create a shared Playwright browser instance."""
    global _browser
    async with _browser_lock:
        if _browser is None or not _browser.is_connected():
            pw = await async_playwright().start()
            _browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
            )
            log.info("Playwright browser launched")
        return _browser


async def _tiktok_search_playwright(query: str, max_results: int = 10) -> list:
    """Search TikTok via Playwright, extract video data from page JSON."""
    browser = await _get_browser()
    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
    )

    collected = []
    page = await context.new_page()

    try:
        encoded = urllib.parse.quote(query)
        url = f"https://www.tiktok.com/search?q={encoded}"
        log.info("Playwright: navigating to %s", url)

        # Intercept API responses
        api_items = []

        async def handle_response(response):
            try:
                if "/api/search/" in response.url and response.status == 200:
                    rurl = response.url
                    # Skip suggestion/autocomplete endpoints
                    if "sug" in rurl or "autocomplete" in rurl:
                        return
                    data = await response.json()
                    # TikTok search API returns: data: [{type, item, common}, ...]
                    raw_data = data.get("data", [])
                    items = []
                    if isinstance(raw_data, list):
                        for entry in raw_data:
                            if isinstance(entry, dict) and "item" in entry:
                                items.append(entry["item"])
                            elif isinstance(entry, dict) and "id" in entry:
                                items.append(entry)
                    # Also try item_list (older format)
                    if not items:
                        items = data.get("item_list", [])
                    if items:
                        api_items.extend(items)
                        log.info("Intercepted %d video items from search API", len(items))
            except Exception as e:
                log.debug("Response parse error: %s", e)

        page.on("response", handle_response)

        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(5000)

        # Scroll to load more results if needed
        for _ in range(min(3, max(1, max_results // 10))):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(2000)

        # Method 1: Parse intercepted API data
        if api_items:
            for item in api_items[:max_results]:
                try:
                    stats = item.get("stats", {})
                    author_info = item.get("author", {})
                    video_info = item.get("video", {})
                    post_id = str(item.get("id", ""))
                    author = author_info.get("uniqueId", "unknown")
                    is_photo = bool(item.get("imagePost"))
                    duration = video_info.get("duration", 0) if not is_photo else 0

                    # Extract images for photo posts
                    image_urls = []
                    if is_photo:
                        image_post = item.get("imagePost", {})
                        for img in image_post.get("images", []):
                            url_list = img.get("imageURL", {}).get("urlList", [])
                            if url_list:
                                image_urls.append(url_list[0])

                    collected.append({
                        "id": post_id,
                        "sourceUrl": f"https://www.tiktok.com/@{author}/video/{post_id}",
                        "author": author,
                        "description": (item.get("desc") or "")[:500],
                        "likes": stats.get("diggCount", 0),
                        "comments": stats.get("commentCount", 0),
                        "shares": stats.get("shareCount", 0),
                        "views": stats.get("playCount", 0),
                        "saves": stats.get("collectCount", 0),
                        "engagement_total": (stats.get("diggCount", 0) + stats.get("commentCount", 0) +
                                            stats.get("shareCount", 0) + stats.get("collectCount", 0)),
                        "duration": duration,
                        "is_slideshow": is_photo,
                        "images": image_urls,
                        "imageCount": len(image_urls),
                        "hashtags": [c.get("hashtagName", "") for c in item.get("challenges", []) if isinstance(c, dict)],
                    })
                except Exception as e:
                    log.debug("Parse error for item: %s", e)

        # Method 2: Parse SIGI_STATE / __UNIVERSAL_DATA from page HTML
        if not collected:
            log.info("No API intercepts, trying page data extraction")
            try:
                html = await page.content()

                # Try __UNIVERSAL_DATA_FOR_REHYDRATION__
                match = re.search(r'<script[^>]*id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', html, re.DOTALL)
                if match:
                    data = json.loads(match.group(1))
                    default_scope = data.get("__DEFAULT_SCOPE__", {})
                    search_data = default_scope.get("webapp.search-detail", {})
                    items = search_data.get("searchItemList", []) or []
                    log.info("Extracted %d items from page rehydration data", len(items))

                    for item in items[:max_results]:
                        try:
                            stats = item.get("stats", {})
                            author_info = item.get("author", {})
                            video_info = item.get("video", {})
                            post_id = str(item.get("id", ""))
                            author = author_info.get("uniqueId", "unknown")
                            is_photo = bool(item.get("imagePost"))
                            duration = video_info.get("duration", 0) if not is_photo else 0

                            image_urls = []
                            if is_photo:
                                image_post = item.get("imagePost", {})
                                for img in image_post.get("images", []):
                                    url_list = img.get("imageURL", {}).get("urlList", [])
                                    if url_list:
                                        image_urls.append(url_list[0])

                            collected.append({
                                "id": post_id,
                                "sourceUrl": f"https://www.tiktok.com/@{author}/video/{post_id}",
                                "author": author,
                                "description": (item.get("desc") or "")[:500],
                                "likes": stats.get("diggCount", 0),
                                "comments": stats.get("commentCount", 0),
                                "shares": stats.get("shareCount", 0),
                                "views": stats.get("playCount", 0),
                                "saves": stats.get("collectCount", 0),
                                "engagement_total": (stats.get("diggCount", 0) + stats.get("commentCount", 0) +
                                                    stats.get("shareCount", 0) + stats.get("collectCount", 0)),
                                "duration": duration,
                                "is_slideshow": is_photo,
                                "images": image_urls,
                                "imageCount": len(image_urls),
                                "hashtags": [c.get("hashtagName", "") for c in item.get("challenges", []) if isinstance(c, dict)],
                            })
                        except Exception as e:
                            log.debug("Parse error: %s", e)
            except Exception as e:
                log.warning("Page data extraction failed: %s", e)

        # Method 3: Extract video links from DOM as last resort
        if not collected:
            log.info("Trying DOM link extraction")
            try:
                links = await page.evaluate("""
                    () => {
                        const items = [];
                        document.querySelectorAll('a[href*="/@"]').forEach(a => {
                            const href = a.href;
                            const match = href.match(/\\/@([^/]+)\\/(video|photo)\\/(\\d+)/);
                            if (match) {
                                items.push({
                                    author: match[1],
                                    type: match[2],
                                    id: match[3],
                                    url: href
                                });
                            }
                        });
                        return items;
                    }
                """)
                seen = set()
                for link in links[:max_results]:
                    if link["id"] in seen:
                        continue
                    seen.add(link["id"])
                    collected.append({
                        "id": link["id"],
                        "sourceUrl": link["url"],
                        "author": link["author"],
                        "description": "",
                        "likes": 0, "comments": 0, "shares": 0, "views": 0, "saves": 0,
                        "engagement_total": 0,
                        "duration": 0,
                        "is_slideshow": link["type"] == "photo",
                        "images": [],
                        "imageCount": 0,
                        "hashtags": [],
                    })
                log.info("DOM extraction: %d links", len(collected))
            except Exception as e:
                log.warning("DOM extraction failed: %s", e)

    except Exception as e:
        log.error("Playwright search error: %s", e)
    finally:
        await page.close()
        await context.close()

    return collected[:max_results]


async def _tiktok_hashtag_playwright(hashtag: str, max_results: int = 10) -> list:
    """Search TikTok by hashtag via Playwright."""
    clean = hashtag.lstrip("#")
    return await _tiktok_search_playwright(f"#{clean}", max_results)


async def handle_scrape_search(request):
    """
    POST /scrape-search
    Body: {"queries": ["coding tips", "web dev tutorial"], "max_results": 10, "min_engagement": 0, "filter": "slideshows"|"videos"|"all"}
    Returns carousels (slideshows) by default, or all content.
    """
    data = await request.json() if request.content_type == "application/json" else {}
    queries = data.get("queries", [])
    if not queries:
        return web.json_response({"error": "queries required"}, status=400)

    max_results = min(int(data.get("max_results", 10)), 50)
    min_eng = int(data.get("min_engagement", 0))
    content_filter = data.get("filter", "slideshows")  # slideshows, videos, all

    log.info("Search request: queries=%s, max=%d, filter=%s", queries, max_results, content_filter)

    all_results = []
    for q in queries:
        items = await _tiktok_search_playwright(q, max_results)
        all_results.extend(items)

    # Deduplicate by ID
    seen = set()
    parsed = []
    for item in all_results:
        if item["id"] not in seen:
            seen.add(item["id"])
            parsed.append(item)

    # Apply filters
    if content_filter == "slideshows":
        parsed = [p for p in parsed if p["is_slideshow"] and p["imageCount"] >= 2]
    elif content_filter == "videos":
        parsed = [p for p in parsed if not p["is_slideshow"]]

    if min_eng > 0:
        parsed = [p for p in parsed if p["engagement_total"] >= min_eng]

    parsed.sort(key=lambda x: x["engagement_total"], reverse=True)
    log.info("Search: %d results after filter (%s)", len(parsed), content_filter)
    return web.json_response(parsed)


async def handle_scrape_hashtag(request):
    """
    POST /scrape-hashtag
    Body: {"hashtags": ["codingtips", "techtok"], "max_results": 10, "min_engagement": 0, "filter": "slideshows"|"videos"|"all"}
    """
    data = await request.json() if request.content_type == "application/json" else {}
    hashtags = data.get("hashtags", [])
    if not hashtags:
        return web.json_response({"error": "hashtags required"}, status=400)

    max_results = min(int(data.get("max_results", 10)), 50)
    min_eng = int(data.get("min_engagement", 0))
    content_filter = data.get("filter", "slideshows")

    log.info("Hashtag request: hashtags=%s, max=%d, filter=%s", hashtags, max_results, content_filter)

    all_results = []
    for h in hashtags:
        items = await _tiktok_hashtag_playwright(h, max_results)
        all_results.extend(items)

    # Deduplicate
    seen = set()
    parsed = []
    for item in all_results:
        if item["id"] not in seen:
            seen.add(item["id"])
            parsed.append(item)

    if content_filter == "slideshows":
        parsed = [p for p in parsed if p["is_slideshow"] and p["imageCount"] >= 2]
    elif content_filter == "videos":
        parsed = [p for p in parsed if not p["is_slideshow"]]

    if min_eng > 0:
        parsed = [p for p in parsed if p["engagement_total"] >= min_eng]

    parsed.sort(key=lambda x: x["engagement_total"], reverse=True)
    log.info("Hashtag: %d results after filter (%s)", len(parsed), content_filter)
    return web.json_response(parsed)


async def handle_search_videos(request):
    """
    POST /search-videos
    Body: {"queries": ["dance challenge", "aesthetic dance"], "max_results": 10, "min_views": 10000}
    Returns VIDEOS (not slideshows) — for dance/content pipelines.
    """
    data = await request.json() if request.content_type == "application/json" else {}
    queries = data.get("queries", [])
    if not queries:
        return web.json_response({"error": "queries required"}, status=400)

    max_results = min(int(data.get("max_results", 10)), 50)
    min_views = int(data.get("min_views", 10000))
    min_duration = int(data.get("min_duration", 10))
    max_duration = int(data.get("max_duration", 60))

    log.info("Search videos: queries=%s, max=%d, min_views=%d", queries, max_results, min_views)

    all_results = []
    for q in queries:
        items = await _tiktok_search_playwright(q, max_results)
        all_results.extend(items)

    # Deduplicate
    seen = set()
    parsed = []
    for item in all_results:
        if item["id"] not in seen:
            seen.add(item["id"])
            parsed.append(item)

    # Filter: only videos, not slideshows
    videos = []
    for p in parsed:
        if p["is_slideshow"]:
            continue
        if p["views"] < min_views:
            continue
        if p["duration"] < min_duration or p["duration"] > max_duration:
            continue
        videos.append(p)

    videos.sort(key=lambda x: x["views"], reverse=True)
    log.info("Search videos: %d results after filter", len(videos))
    return web.json_response(videos)


async def handle_scrape_profile_videos(request):
    """
    POST /scrape-profile-videos
    Body: {"profiles": ["charlidamelio", "brookemonk_"], "max_posts": 10, "min_views": 10000, "min_duration": 10, "max_duration": 60}
    Returns VIDEOS (not slideshows) from TikTok profiles via yt-dlp.
    """
    data = await request.json() if request.content_type == "application/json" else {}
    profiles = data.get("profiles", [])
    if not profiles:
        return web.json_response({"error": "profiles required"}, status=400)

    max_posts = min(int(data.get("max_posts", 10)), 30)
    min_views = int(data.get("min_views", 10000))
    min_duration = int(data.get("min_duration", 10))
    max_duration = int(data.get("max_duration", 60))

    log.info("Scrape profile videos: %d profiles, max_posts=%d", len(profiles), max_posts)

    all_videos = []
    for username in profiles:
        posts = _yt_dlp_list_posts(username, max_posts)
        for p in posts:
            if p["is_slideshow"]:
                continue
            if p["views"] < min_views:
                continue
            if p["duration"] < min_duration or p["duration"] > max_duration:
                continue
            p["sourceUrl"] = p.get("url", "")
            p["author"] = username
            all_videos.append(p)

    # Deduplicate
    seen = set()
    unique = []
    for v in all_videos:
        if v["id"] not in seen:
            seen.add(v["id"])
            unique.append(v)

    unique.sort(key=lambda x: x["views"], reverse=True)
    log.info("Scrape profile videos: %d videos after filter from %d profiles", len(unique), len(profiles))
    return web.json_response(unique)


async def handle_scrape_instagram(request):
    """
    POST /scrape-instagram
    Body: {"profiles": ["leaelui", "gabimouura"], "max_posts": 5, "min_engagement": 0}
    or:   {"username": "mkbhd", "count": 5}
    Returns Instagram posts from profiles via public API (no auth needed).
    Output format matches the downstream pipeline schema:
      { type, shortCode, id, url, caption, likes, comments, displayUrl, images, author, timestamp }
    """
    data = await request.json() if request.content_type == "application/json" else {}
    profiles = data.get("profiles", [])
    if not profiles:
        username = data.get("username", "")
        if username:
            profiles = [username]
    if not profiles:
        return web.json_response({"error": "profiles required"}, status=400)

    max_posts = min(int(data.get("max_posts", data.get("count", data.get("resultsLimit", 12)))), 30)
    min_eng = int(data.get("min_engagement", 0))

    log.info("Instagram API request: %d profiles, max_posts=%d", len(profiles), max_posts)

    all_posts = []

    # Load proxy pool for fallback
    ig_proxies = []
    try:
        proxy_config_path = Path(os.environ.get("PROXY_POOL_CONFIG", "proxy_pool.json"))
        if proxy_config_path.exists():
            pc = json.loads(proxy_config_path.read_text())
            ws = pc.get("webshare", {})
            ws_user = ws.get("username", "")
            ws_pass = ws.get("password", "")
            for p in pc.get("proxies", []):
                if ws_user:
                    ig_proxies.append(f"http://{ws_user}:{ws_pass}@{p['host']}:{p['port']}")
            swiss = pc.get("swiss", {})
            if swiss.get("host"):
                proto = swiss.get("protocol", "socks5h")
                ig_proxies.append(f"{proto}://{swiss.get('username','')}:{swiss.get('password','')}@{swiss['host']}:{swiss['port']}")
    except Exception as e:
        log.warning("Could not load proxy pool: %s", e)

    # Rotate User-Agents to reduce fingerprinting
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
    ]

    async def _ig_api_call(session, username, proxy_url=None):
        """Make Instagram API call, optionally through proxy. Returns parsed JSON or None."""
        headers = {
            "User-Agent": random.choice(user_agents),
            "X-IG-App-ID": "936619743392459",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
        }
        url = f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}"
        try:
            kwargs = {"headers": headers, "timeout": aiohttp.ClientTimeout(total=20)}
            if proxy_url:
                kwargs["proxy"] = proxy_url
            async with session.get(url, **kwargs) as resp:
                if resp.status in (401, 429):
                    return None  # rate limited
                if resp.status != 200:
                    return None
                return await resp.json()
        except Exception:
            return None

    async with aiohttp.ClientSession() as session:
        for profile in profiles:
            username = profile.strip().replace("https://www.instagram.com/", "").replace("/", "").replace("@", "")
            if not username:
                continue

            log.info("IG API: scraping @%s...", username)

            # Try direct first, then proxies
            ig_data = await _ig_api_call(session, username)
            if not ig_data:
                log.info("IG @%s: direct failed, trying proxies (%d available)...", username, len(ig_proxies))
                random.shuffle(ig_proxies)
                for px in ig_proxies[:3]:
                    ig_data = await _ig_api_call(session, username, proxy_url=px)
                    if ig_data:
                        log.info("IG @%s: success via proxy", username)
                        break
                    await asyncio.sleep(1)

            if not ig_data:
                log.warning("IG @%s: all attempts failed (direct + proxies)", username)
                continue

                user = ig_data.get("data", {}).get("user", {})
                if not user:
                    log.error("IG @%s: no user data", username)
                    continue

                edges = user.get("edge_owner_to_timeline_media", {}).get("edges", [])
                log.info("IG @%s: got %d posts from API", username, len(edges))

                for edge in edges[:max_posts]:
                    node = edge.get("node", {})
                    typename = node.get("__typename", "")
                    shortcode = node.get("shortcode", "")
                    post_id = node.get("id", shortcode)

                    # Determine type (matches the paid scraper output schema)
                    if typename == "GraphVideo":
                        post_type = "Video"
                    elif typename == "GraphSidecar":
                        post_type = "Sidecar"
                    else:
                        post_type = "Image"

                    # Get images for carousels/sidecars
                    images = []
                    sidecar_edges = node.get("edge_sidecar_to_children", {}).get("edges", [])
                    if sidecar_edges:
                        for sc in sidecar_edges:
                            sc_node = sc.get("node", {})
                            img_url = sc_node.get("display_url", "")
                            if img_url:
                                images.append(img_url)

                    caption_edges = node.get("edge_media_to_caption", {}).get("edges", [])
                    caption = caption_edges[0].get("node", {}).get("text", "") if caption_edges else ""

                    likes = node.get("edge_liked_by", {}).get("count", 0)
                    comments = node.get("edge_media_to_comment", {}).get("count", 0)
                    eng = likes + comments
                    if eng < min_eng:
                        continue

                    post = {
                        "type": post_type,
                        "shortCode": shortcode,
                        "id": post_id,
                        "url": f"https://www.instagram.com/p/{shortcode}/",
                        "sourceUrl": f"https://www.instagram.com/p/{shortcode}/",
                        "caption": caption[:500],
                        "likes": likes,
                        "comments": comments,
                        "views": 0,
                        "displayUrl": node.get("display_url", ""),
                        "images": images if images else [node.get("display_url", "")],
                        "is_video": node.get("is_video", False),
                        "videoUrl": node.get("video_url", "") if node.get("is_video") else "",
                        "video_url": node.get("video_url", "") if node.get("is_video") else "",
                        "timestamp": node.get("taken_at_timestamp", 0),
                        "author": username,
                        "ownerUsername": username,
                        "dimensions": node.get("dimensions", {}),
                    }
                    all_posts.append(post)

            # Rate limit between profiles — Instagram is strict
            await asyncio.sleep(3)

    all_posts.sort(key=lambda p: (p.get("likes", 0) or 0), reverse=True)
    log.info("Instagram API total: %d posts from %d profiles", len(all_posts), len(profiles))
    return web.json_response(all_posts)


def _extract_reels_from_xhr(data, reel_details, username):
    """Recursively search XHR JSON data for reel/video info and populate reel_details dict."""
    if isinstance(data, dict):
        # Check if this dict represents a media item
        shortcode = data.get('code') or data.get('shortcode')
        media_type = data.get('media_type') or data.get('__typename', '')
        is_video = media_type in (2, '2', 'GraphVideo', 'XDTGraphVideo') or data.get('is_video') or data.get('video_url')

        if shortcode and is_video and shortcode not in reel_details:
            video_url = data.get('video_url', '')
            if not video_url:
                # Try video_versions
                versions = data.get('video_versions', [])
                if versions and isinstance(versions, list):
                    video_url = versions[0].get('url', '')

            caption_data = data.get('caption', {})
            caption = ''
            if isinstance(caption_data, dict):
                caption = caption_data.get('text', '')
            elif isinstance(caption_data, str):
                caption = caption_data

            likes = data.get('like_count', 0) or data.get('edge_media_preview_like', {}).get('count', 0) if isinstance(data.get('edge_media_preview_like'), dict) else 0
            comments = data.get('comment_count', 0)
            views = data.get('play_count', 0) or data.get('view_count', 0) or data.get('video_play_count', 0)
            duration = data.get('video_duration', 0)

            reel_details[shortcode] = {
                'type': 'video',
                'shortCode': shortcode,
                'id': data.get('pk', shortcode),
                'url': f'https://www.instagram.com/reel/{shortcode}/',
                'sourceUrl': f'https://www.instagram.com/reel/{shortcode}/',
                'caption': (caption or '')[:500],
                'title': '',
                'likes': likes if isinstance(likes, int) else 0,
                'comments': comments if isinstance(comments, int) else 0,
                'views': views if isinstance(views, int) else 0,
                'duration': duration if isinstance(duration, (int, float)) else 0,
                'videoUrl': video_url,
                'video_url': video_url,
                'author': username,
                'timestamp': '',
            }

        # Recurse into dict values
        for v in data.values():
            _extract_reels_from_xhr(v, reel_details, username)
    elif isinstance(data, list):
        for item in data:
            _extract_reels_from_xhr(item, reel_details, username)


async def main():
    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_post("/scrape", handle_scrape)
    app.router.add_post("/scrape-post", handle_scrape_post)
    app.router.add_post("/scrape-search", handle_scrape_search)
    app.router.add_post("/scrape-hashtag", handle_scrape_hashtag)
    app.router.add_post("/search-videos", handle_search_videos)
    app.router.add_post("/scrape-profile-videos", handle_scrape_profile_videos)
    app.router.add_post("/scrape-instagram", handle_scrape_instagram)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 3201)
    await site.start()
    log.info("TikTok Slideshow Scraper (FREE) running on :3201")

    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
