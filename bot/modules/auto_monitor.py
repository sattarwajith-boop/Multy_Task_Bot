#!/usr/bin/env python3
"""Website monitoring, MV-style discovery, IMDb cards, and auto-leech dispatch.

This module deliberately reuses WZML-X's download/upload queue.  It only finds
new links and creates normal qbleech/leech tasks, so torrent selection, split
uploads, limits, cancellation, and status reporting remain in one engine.
"""

import asyncio
import hashlib
import os
import random
import re
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import urljoin, urlparse, urlunparse

import cloudscraper
import feedparser
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiohttp import (
    ClientConnectionError,
    ClientResponseError,
    ClientSession,
    ClientTimeout,
)
from bs4 import BeautifulSoup
from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram.errors import FloodWait
from pyrogram.filters import command, regex
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from tzlocal import get_localzone

from bot import (
    DATABASE_URL,
    LOGGER,
    OWNER_ID,
    SCRAPER_ONLY,
    bot,
    bot_loop,
    config_dict,
    user_data,
)
from bot.helper.ext_utils.bot_utils import new_task
from bot.helper.telegram_helper.filters import CustomFilters
from bot.helper.telegram_helper.message_utils import sendMessage
if not SCRAPER_ONLY:
    from bot.modules.mirror_leech import leech, qb_leech
else:
    leech = qb_leech = None


USER_AGENTS = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.5 Safari/605.1.15"
    ),
]
DIRECT_RE = re.compile(
    r"\.(torrent|mkv|mp4|avi|mov|webm|m4v|ts|mp3|flac|zip|rar|7z|iso)"
    r"(?:$|[?#])",
    re.I,
)
AUTO_ENABLED = os.getenv("AUTO_MONITOR_ENABLED", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
AUTO_INTERVAL = max(60, int(os.getenv("AUTO_MONITOR_INTERVAL", "900")))


def _configured(value):
    return bool(value and value.strip().upper() not in {"CHANGE_ME", "YOUR_VALUE"})


_auto_chat_value = os.getenv(
    "AUTO_MONITOR_CHAT", str(config_dict.get("RSS_CHAT") or OWNER_ID)
).strip()
if not _configured(_auto_chat_value):
    _auto_chat_value = str(config_dict.get("RSS_CHAT") or OWNER_ID)
AUTO_CHAT = (
    int(_auto_chat_value)
    if _auto_chat_value.lstrip("-").isdigit()
    else _auto_chat_value
)
AUTO_MAX_ITEMS = max(1, int(os.getenv("AUTO_MAX_ITEMS_PER_SITE", "3")))
AUTO_MAX_TASKS_PER_RUN = max(1, int(os.getenv("AUTO_MAX_TASKS_PER_RUN", "2")))
AUTO_DISPATCH_DELAY = max(0, int(os.getenv("AUTO_DISPATCH_DELAY", "8")))
AUTO_LEECH_EXISTING = os.getenv("AUTO_LEECH_EXISTING", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
AUTO_FETCH_RETRIES = max(1, int(os.getenv("AUTO_FETCH_RETRIES", "3")))
AUTO_FETCH_TIMEOUT = max(10, int(os.getenv("AUTO_FETCH_TIMEOUT", "35")))
AUTO_SITE_COOKIE = os.getenv("AUTO_SITE_COOKIE", "").strip()
AUTO_SITE_PROXY = os.getenv("AUTO_SITE_PROXY", "").strip()
AUTO_SITE_PROXY = AUTO_SITE_PROXY if _configured(AUTO_SITE_PROXY) else ""
AUTO_FORWARD_CHATS = [
    value.strip()
    for value in re.split(r"[\s,]+", os.getenv("AUTO_FORWARD_CHATS", ""))
    if _configured(value)
]
MV_SITE_URL = os.getenv("MV_SITE_URL", "").strip()
MV_SITE_URL = MV_SITE_URL if _configured(MV_SITE_URL) else ""
OMDB_API_KEY = os.getenv("OMDB_API_KEY", "").strip()
OMDB_API_KEY = OMDB_API_KEY if _configured(OMDB_API_KEY) else ""
SITE_TYPES = {"auto", "rss", "html", "mv"}

_mongo = (
    AsyncIOMotorClient(
        DATABASE_URL,
        serverSelectionTimeoutMS=10000,
        connectTimeoutMS=10000,
    )
    if DATABASE_URL
    else None
)
_db = _mongo.wzmlx_auto if _mongo is not None else None
_check_lock = asyncio.Lock()
_mv_cache = {}
auto_scheduler = AsyncIOScheduler(timezone=str(get_localzone()), event_loop=bot_loop)
TG_SAFE_TEXT_LIMIT = 3800


class SiteBlockedError(RuntimeError):
    """The remote site rejected all browser-like request attempts."""


def _key(value):
    return hashlib.sha256(value.encode("utf-8", "ignore")).hexdigest()


def _clean_title(value):
    value = re.sub(r"\.(torrent|mkv|mp4|avi|mov|webm|zip|rar)$", "", value, flags=re.I)
    value = re.sub(r"[\[\(\{].*?[\]\)\}]", " ", value)
    value = re.sub(r"\b(2160p|1080p|720p|480p|x26[45]|hevc|web-?dl|bluray|hdrip)\b", " ", value, flags=re.I)
    return re.sub(r"[\W_]+", " ", value).strip()


def _canonical_url(url):
    url = url.strip()
    if url and "://" not in url and re.match(r"^[\w.-]+\.[a-z]{2,}(?:[/:?#]|$)", url, re.I):
        url = f"https://{url}"
    parsed = urlparse(url)
    scheme = parsed.scheme.lower() or "https"
    host = (parsed.hostname or "").lower()
    if not host:
        return url.strip()
    port = f":{parsed.port}" if parsed.port else ""
    path = re.sub(r"/+", "/", parsed.path or "")
    if path == "/":
        path = ""
    else:
        path = path.rstrip("/")
    return urlunparse((scheme, host + port, path, "", parsed.query, ""))


def _request_headers(url):
    parsed = urlparse(url)
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": f"{parsed.scheme}://{parsed.netloc}/",
        "Upgrade-Insecure-Requests": "1",
    }
    if AUTO_SITE_COOKIE:
        headers["Cookie"] = AUTO_SITE_COOKIE
    return headers


def _cloudscraper_fetch(url):
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    response = scraper.get(
        url,
        headers=_request_headers(url),
        timeout=AUTO_FETCH_TIMEOUT,
        allow_redirects=True,
        proxies=(
            {"http": AUTO_SITE_PROXY, "https": AUTO_SITE_PROXY}
            if AUTO_SITE_PROXY
            else None
        ),
    )
    if response.status_code in {403, 429, 503, 999}:
        raise SiteBlockedError(
            f"site blocked Koyeb after browser fallback (HTTP {response.status_code})"
        )
    response.raise_for_status()
    return response.text, response.url


async def _fetch(url):
    url = _canonical_url(url)
    timeout = ClientTimeout(total=AUTO_FETCH_TIMEOUT)
    last_error = None
    for attempt in range(AUTO_FETCH_RETRIES):
        try:
            async with ClientSession(
                headers=_request_headers(url),
                timeout=timeout,
                trust_env=True,
            ) as session:
                async with session.get(
                    url,
                    allow_redirects=True,
                    proxy=AUTO_SITE_PROXY or None,
                ) as response:
                    if response.status in {403, 429, 503, 999}:
                        raise SiteBlockedError(
                            f"site rejected request with HTTP {response.status}"
                        )
                    response.raise_for_status()
                    return await response.text(errors="replace"), str(response.url)
        except (
            ClientConnectionError,
            ClientResponseError,
            asyncio.TimeoutError,
            SiteBlockedError,
        ) as error:
            last_error = error
            if attempt + 1 < AUTO_FETCH_RETRIES:
                await asyncio.sleep(1.5 * (attempt + 1))

    try:
        return await asyncio.to_thread(_cloudscraper_fetch, url)
    except Exception as error:
        last_error = error
    raise SiteBlockedError(
        f"unable to fetch {url}: {last_error}. "
        "The site may block Koyeb IPs; set AUTO_SITE_COOKIE or AUTO_SITE_PROXY."
    ) from last_error


async def _scrape_rss(url):
    html, _ = await _fetch(url)
    feed = feedparser.parse(html)
    results = []
    for entry in feed.entries[:AUTO_MAX_ITEMS]:
        title = entry.get("title", "Untitled")
        links = []
        for item in entry.get("links", []):
            href = item.get("href", "")
            if href and (href.startswith("magnet:") or DIRECT_RE.search(href)):
                links.append(href)
        page = entry.get("link", "")
        if not links and page:
            links = await _scrape_detail(page)
        for link in dict.fromkeys(links):
            results.append({"title": title, "url": link, "page_url": page or url})
    return results


async def _scrape_detail(url):
    try:
        html, final_url = await _fetch(url)
    except SiteBlockedError:
        raise
    except Exception as error:
        LOGGER.warning("Auto monitor detail fetch failed for %s: %s", url, error)
        return []
    soup = BeautifulSoup(html, "lxml")
    links = []
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if href.startswith("magnet:"):
            links.append(href)
        else:
            resolved = urljoin(final_url, href)
            if DIRECT_RE.search(resolved):
                links.append(resolved)
    return list(dict.fromkeys(links))


async def _scrape_html(url):
    html, final_url = await _fetch(url)
    soup = BeautifulSoup(html, "lxml")
    results = []
    detail_pages = []
    origin = urlparse(final_url).netloc
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        resolved = href if href.startswith("magnet:") else urljoin(final_url, href)
        title = tag.get_text(" ", strip=True) or _clean_title(urlparse(resolved).path.rsplit("/", 1)[-1])
        if resolved.startswith("magnet:") or DIRECT_RE.search(resolved):
            results.append({"title": title, "url": resolved, "page_url": final_url})
        elif (
            urlparse(resolved).netloc == origin
            and resolved != final_url
            and not resolved.startswith(("javascript:", "mailto:"))
            and "#" not in resolved
        ):
            detail_pages.append((title, resolved))
    if results:
        return _dedupe(results)[:AUTO_MAX_ITEMS]
    blocked_pages = 0
    for title, page in list(dict.fromkeys(detail_pages))[:AUTO_MAX_ITEMS]:
        try:
            links = await _scrape_detail(page)
        except SiteBlockedError:
            blocked_pages += 1
            continue
        for link in links:
            results.append({"title": title, "url": link, "page_url": page})
    if not results and blocked_pages:
        raise SiteBlockedError(
            f"site listing opened, but {blocked_pages} detail page(s) were blocked"
        )
    return _dedupe(results)


async def _scrape_mv(url):
    html, final_url = await _fetch(url)
    soup = BeautifulSoup(html, "lxml")
    topics = []
    for block in soup.select("div.ipsType_break.ipsContained"):
        anchor = block.find("a", href=True)
        if anchor:
            topics.append((anchor.get_text(" ", strip=True), urljoin(final_url, anchor["href"])))
    if not topics:
        for anchor in soup.select("a[href]"):
            href = urljoin(final_url, anchor["href"])
            text = anchor.get_text(" ", strip=True)
            if text and urlparse(href).netloc == urlparse(final_url).netloc:
                topics.append((text, href))
    results = []
    blocked_pages = 0
    for title, page in list(dict.fromkeys(topics))[:AUTO_MAX_ITEMS]:
        try:
            links = await _scrape_detail(page)
        except SiteBlockedError:
            blocked_pages += 1
            continue
        for link in links:
            results.append({"title": title, "url": link, "page_url": page})
    if not results and blocked_pages:
        raise SiteBlockedError(
            f"MV listing opened, but {blocked_pages} release page(s) were blocked"
        )
    return _dedupe(results)


def _dedupe(items):
    found = set()
    output = []
    for item in items:
        if item["url"] not in found:
            found.add(item["url"])
            output.append(item)
    return output


async def _scrape(site):
    kind = site.get("type", "auto").lower()
    if kind == "mv":
        return await _scrape_mv(site["url"])
    if kind == "rss":
        return await _scrape_rss(site["url"])
    if kind == "html":
        return await _scrape_html(site["url"])
    try:
        rss = await _scrape_rss(site["url"])
        if rss:
            return rss
    except Exception:
        pass
    return await _scrape_html(site["url"])


async def _imdb_card(title):
    if not OMDB_API_KEY:
        return None
    key = OMDB_API_KEY
    match = re.search(r"[?&]apikey=([^&]+)", key)
    if match:
        key = match.group(1)
    params = {"apikey": key, "t": _clean_title(title), "plot": "short"}
    try:
        async with ClientSession(timeout=ClientTimeout(total=15)) as session:
            async with session.get("https://www.omdbapi.com/", params=params) as response:
                data = await response.json(content_type=None)
        if data.get("Response") != "True":
            return None
        text = (
            f"<b>{escape(data.get('Title', title))}</b> "
            f"({escape(data.get('Year', 'N/A'))})\n"
            f"<b>IMDb:</b> {escape(data.get('imdbRating', 'N/A'))}/10\n"
            f"<b>Genre:</b> {escape(data.get('Genre', 'N/A'))}\n"
            f"<b>Runtime:</b> {escape(data.get('Runtime', 'N/A'))}\n"
            f"<b>Plot:</b> {escape(data.get('Plot', 'N/A'))}"
        )
        poster = data.get("Poster")
        return text, poster if poster and poster != "N/A" else None
    except Exception as error:
        LOGGER.warning("OMDb lookup failed: %s", error)
        return None


async def _send_imdb(item):
    card = await _imdb_card(item["title"])
    if not card:
        return
    text, poster = card
    try:
        if poster:
            message = await bot.send_photo(AUTO_CHAT, poster, caption=text)
        else:
            message = await bot.send_message(AUTO_CHAT, text)
        for chat in AUTO_FORWARD_CHATS:
            try:
                await message.copy(int(chat) if chat.lstrip("-").isdigit() else chat)
            except Exception as error:
                LOGGER.warning("IMDb card copy to %s failed: %s", chat, error)
    except Exception as error:
        LOGGER.warning("IMDb card send failed: %s", error)


async def _send_auto_message(text):
    while True:
        try:
            return await bot.send_message(
                AUTO_CHAT,
                text,
                disable_web_page_preview=True,
            )
        except FloodWait as error:
            wait_time = int(getattr(error, "value", 30)) + 2
            LOGGER.warning(
                "Telegram FloodWait while dispatching auto task. Sleeping %ss.",
                wait_time,
            )
            await asyncio.sleep(wait_time)


async def _prepare_forward_dumps():
    if not AUTO_FORWARD_CHATS:
        return False
    dumps = dict(user_data.get(OWNER_ID, {}).get("ldump", {}))
    for index, chat in enumerate(AUTO_FORWARD_CHATS, 1):
        dumps[f"auto_{index}"] = int(chat) if chat.lstrip("-").isdigit() else chat
    user_data.setdefault(OWNER_ID, {})["ldump"] = dumps
    return True


async def _dispatch(item):
    await _send_imdb(item)
    use_dumps = False if SCRAPER_ONLY else await _prepare_forward_dumps()
    is_torrent = item["url"].startswith("magnet:") or ".torrent" in item["url"].lower()
    cmd = "qbleech" if is_torrent else "leech"
    dump_arg = " -ud all" if use_dumps else ""
    text = f"/{cmd} {item['url']}{dump_arg}\nTag: Auto {OWNER_ID}"
    message = await _send_auto_message(text)
    if SCRAPER_ONLY:
        LOGGER.info("SCRAPER_ONLY dispatched %s command to %s", cmd, AUTO_CHAT)
        if AUTO_DISPATCH_DELAY:
            await asyncio.sleep(AUTO_DISPATCH_DELAY)
        return
    # Telegram does not feed a bot's own messages back through command handlers.
    # Fetch the just-sent command and call WZML-X's queue entrypoint directly so
    # auto-monitor creates the same task that a manual /qbleech paste would.
    message = await bot.get_messages(chat_id=message.chat.id, message_ids=message.id)
    message.from_user = await bot.get_users(OWNER_ID)
    if is_torrent:
        await qb_leech(bot, message)
    else:
        await leech(bot, message)
    if AUTO_DISPATCH_DELAY:
        await asyncio.sleep(AUTO_DISPATCH_DELAY)


async def _ensure_indexes():
    if _db is None:
        return
    sites = await _db.sites.find({}).sort("created_at", 1).to_list(length=None)
    canonical_sites = {}
    for site in sites:
        canonical = _canonical_url(site["url"])
        canonical_sites.setdefault(canonical, []).append(site)
    for canonical, duplicates in canonical_sites.items():
        keeper = duplicates[0]
        if len(duplicates) > 1:
            merged_enabled = any(site.get("enabled", True) for site in duplicates)
            merged_initialized = any(
                site.get("initialized", False) for site in duplicates
            )
            merged_type = (
                "mv"
                if any(site.get("type") == "mv" for site in duplicates)
                else keeper.get("type", "auto")
            )
            for duplicate in duplicates[1:]:
                await _db.sites.delete_one({"_id": duplicate["_id"]})
                LOGGER.info(
                    "Removed duplicate monitored site URL: %s", duplicate["url"]
                )
            await _db.sites.update_one(
                {"_id": keeper["_id"]},
                {"$set": {
                    "enabled": merged_enabled,
                    "initialized": merged_initialized,
                    "type": merged_type,
                }},
            )
        if canonical != keeper["url"]:
            await _db.sites.update_one(
                {"_id": keeper["_id"]}, {"$set": {"url": canonical}}
            )
    await _db.sites.create_index("url", unique=True)
    await _db.items.create_index("key", unique=True)


async def _run_check(force=False):
    if _db is None:
        raise RuntimeError("DATABASE_URL is required for auto monitoring")
    if _check_lock.locked() and not force:
        return {
            "sites": 0,
            "found": 0,
            "new": 0,
            "baseline": 0,
            "known": 0,
            "deferred": 0,
            "blocked": 0,
            "errors": 0,
            "details": [],
        }
    async with _check_lock:
        await _ensure_indexes()
        sites = await _db.sites.find({"enabled": True}).to_list(length=None)
        stats = {
            "sites": len(sites),
            "found": 0,
            "new": 0,
            "baseline": 0,
            "known": 0,
            "deferred": 0,
            "blocked": 0,
            "errors": 0,
            "details": [],
        }
        for site in sites:
            site_name = site.get("name", site["url"])
            site_detail = {
                "name": site_name,
                "url": site["url"],
                "found": 0,
                "new": 0,
                "baseline": 0,
                "known": 0,
                "deferred": 0,
                "status": "ok",
                "error": "",
            }
            try:
                items = await _scrape(site)
                site_detail["found"] = len(items)
                stats["found"] += len(items)
                if not site.get("initialized", False) and not AUTO_LEECH_EXISTING:
                    for item in items[:AUTO_MAX_ITEMS]:
                        key = _key(item["url"])
                        result = await _db.items.update_one(
                            {"key": key},
                            {"$setOnInsert": {
                                "key": key,
                                **item,
                                "site_url": site["url"],
                                "status": "baseline",
                                "created_at": datetime.now(timezone.utc),
                            }},
                            upsert=True,
                        )
                        if result.upserted_id is None:
                            site_detail["known"] += 1
                            stats["known"] += 1
                        else:
                            site_detail["baseline"] += 1
                            stats["baseline"] += 1
                    await _db.sites.update_one(
                        {"_id": site["_id"]},
                        {"$set": {
                            "initialized": True,
                            "last_checked": datetime.now(timezone.utc),
                            "last_error": "",
                        }},
                    )
                    LOGGER.info(
                        "Initialized %s with %s existing item(s); future items will auto-leech",
                        site.get("name", site["url"]),
                        len(items[:AUTO_MAX_ITEMS]),
                    )
                    continue
                # Oldest first keeps multi-item releases in a sensible order.
                for item in reversed(items[:AUTO_MAX_ITEMS]):
                    if stats["new"] >= AUTO_MAX_TASKS_PER_RUN:
                        site_detail["deferred"] += 1
                        stats["deferred"] += 1
                        continue
                    key = _key(item["url"])
                    claim = await _db.items.update_one(
                        {"key": key},
                        {"$setOnInsert": {
                            "key": key,
                            **item,
                            "site_url": site["url"],
                            "status": "dispatching",
                            "created_at": datetime.now(timezone.utc),
                        }},
                        upsert=True,
                    )
                    if claim.upserted_id is None:
                        site_detail["known"] += 1
                        stats["known"] += 1
                        continue
                    try:
                        await _dispatch(item)
                        await _db.items.update_one(
                            {"key": key},
                            {"$set": {
                                "status": "queued",
                                "queued_at": datetime.now(timezone.utc),
                            }},
                        )
                    except Exception:
                        await _db.items.delete_one(
                            {"key": key, "status": "dispatching"}
                        )
                        raise
                    stats["new"] += 1
                    site_detail["new"] += 1
                await _db.sites.update_one(
                    {"_id": site["_id"]},
                    {"$set": {
                        "initialized": True,
                        "last_checked": datetime.now(timezone.utc),
                        "last_error": "",
                    }},
                )
            except SiteBlockedError as error:
                stats["blocked"] += 1
                site_detail["status"] = "blocked"
                site_detail["error"] = str(error)[:160]
                LOGGER.warning("Auto monitor blocked for %s: %s", site.get("url"), error)
                await _db.sites.update_one(
                    {"_id": site["_id"]},
                    {
                        "$set": {
                            "last_checked": datetime.now(timezone.utc),
                            "last_error": str(error)[:500],
                        },
                        "$inc": {"blocked_count": 1},
                    },
                )
            except Exception as error:
                stats["errors"] += 1
                site_detail["status"] = "error"
                site_detail["error"] = str(error)[:160]
                LOGGER.exception("Auto monitor failed for %s", site.get("url"))
                await _db.sites.update_one(
                    {"_id": site["_id"]},
                    {"$set": {
                        "last_checked": datetime.now(timezone.utc),
                        "last_error": str(error)[:500],
                    }},
                )
            finally:
                stats["details"].append(site_detail)
        return stats


@new_task
async def auto_sites(_, message):
    if _db is None:
        return await sendMessage(message, "DATABASE_URL is required.")
    sites = await _db.sites.find({}).sort("created_at", 1).to_list(length=None)
    if not sites:
        return await sendMessage(message, "No monitored sites. Use /addsite.")
    lines = ["<b>Monitored sites</b>"]
    for index, site in enumerate(sites, 1):
        state = "ON" if site.get("enabled", True) else "OFF"
        last_error = site.get("last_error")
        last_checked = site.get("last_checked")
        checked_line = f"\nLast checked: <code>{escape(str(last_checked))}</code>" if last_checked else ""
        error_line = f"\nLast error: <code>{escape(last_error[:220])}</code>" if last_error else ""
        lines.append(
            f"{index}. <b>{escape(site.get('name', 'Site'))}</b> "
            f"[{state}/{escape(site.get('type', 'auto'))}]\n"
            f"<code>{escape(site['url'])}</code>"
            f"{checked_line}{error_line}"
        )
    await sendMessage(message, "\n\n".join(lines))


@new_task
async def add_site(_, message):
    if _db is None:
        return await sendMessage(message, "DATABASE_URL is required.")
    parts = message.text.split(maxsplit=3)
    if len(parts) < 2:
        return await sendMessage(message, "Usage: /addsite URL [auto|rss|html|mv] [name]")
    url = _canonical_url(parts[1])
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return await sendMessage(message, "Site URL must start with http:// or https://")
    kind = "auto"
    name = urlparse(url).netloc
    if len(parts) > 2:
        maybe_kind = parts[2].lower()
        if maybe_kind in SITE_TYPES:
            kind = maybe_kind
            if len(parts) > 3:
                name = parts[3]
        else:
            name = " ".join(parts[2:])
    await _db.sites.update_one(
        {"url": url},
        {
            "$set": {
                "name": name,
                "type": kind,
                "enabled": True,
            },
            "$setOnInsert": {
                "url": url,
                "initialized": False,
                "created_at": datetime.now(timezone.utc),
            },
        },
        upsert=True,
    )
    baseline_note = (
        "Existing items will be recorded without downloading; only future items auto-leech."
        if not AUTO_LEECH_EXISTING
        else "Existing discovered items may also be queued."
    )
    await sendMessage(
        message,
        f"Added <b>{escape(name)}</b> as <code>{kind}</code>.\n{baseline_note}",
    )


@new_task
async def delete_site(_, message):
    if _db is None:
        return await sendMessage(message, "DATABASE_URL is required.")
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return await sendMessage(message, "Usage: /delsite URL")
    result = await _db.sites.delete_one({"url": _canonical_url(parts[1])})
    await sendMessage(message, "Site removed." if result.deleted_count else "Site not found.")


@new_task
async def reset_site(_, message):
    if _db is None:
        return await sendMessage(message, "DATABASE_URL is required.")
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return await sendMessage(message, "Usage: /resetsite URL")
    url = _canonical_url(parts[1])
    site = await _db.sites.find_one({"url": url})
    if not site:
        return await sendMessage(message, "Site not found. Check /autosites.")
    result = await _db.items.delete_many({"site_url": site["url"]})
    await _db.sites.update_one(
        {"_id": site["_id"]},
        {"$set": {
            "initialized": True,
            "last_error": "",
            "last_checked": datetime.now(timezone.utc),
        }},
    )
    await sendMessage(
        message,
        f"Reset <b>{escape(site.get('name', site['url']))}</b>.\n"
        f"Forgot {result.deleted_count} known item(s). "
        "Next /checksites can queue currently found links.",
    )


def _format_site_details(details):
    if not details:
        return ""
    lines = ["", "", "<b>Sites</b>"]
    for item in details[:8]:
        line = (
            f"• {escape(item['name'])}: found {item['found']}, "
            f"new {item['new']}, baseline {item['baseline']}, "
            f"known {item['known']}, deferred {item['deferred']}"
        )
        if item["status"] != "ok":
            line += f" [{escape(item['status'])}: {escape(item['error'])}]"
        lines.append(line)
    if len(details) > 8:
        lines.append(f"…and {len(details) - 8} more")
    return "\n".join(lines)[:1800]


def _short_text(value, limit=300):
    value = str(value or "")
    return value if len(value) <= limit else f"{value[:limit - 1]}…"


async def _safe_edit(message, text, **kwargs):
    text = text if len(text) <= TG_SAFE_TEXT_LIMIT else f"{text[:TG_SAFE_TEXT_LIMIT - 40]}\n\n…truncated"
    return await message.edit_text(text, **kwargs)


@new_task
async def check_sites(_, message):
    status = await sendMessage(message, "Checking monitored sites now…")
    try:
        stats = await _run_check(force=True)
        monitor_job = auto_scheduler.get_job("auto_site_monitor")
        if AUTO_ENABLED and monitor_job:
            next_run = monitor_job.next_run_time
            next_run = (
                next_run.strftime("%Y-%m-%d %H:%M:%S %Z")
                if next_run
                else "pending"
            )
            monitor_line = f"\nAuto monitor: ON\nNext run: {next_run}"
        else:
            monitor_line = "\nAuto monitor: OFF"
        await status.edit_text(
            f"<b>Check complete</b>\nSites: {stats['sites']}\n"
            f"Found links: {stats['found']}\n"
            f"New tasks: {stats['new']}\n"
            f"Baselined: {stats['baseline']}\n"
            f"Known/skipped: {stats['known']}\n"
            f"Deferred: {stats['deferred']}\n"
            f"Blocked: {stats['blocked']}\nErrors: {stats['errors']}"
            f"{monitor_line}"
            f"{_format_site_details(stats['details'])}"
        )
    except Exception as error:
        await status.edit_text(f"Check failed: <code>{escape(str(error))}</code>")


async def _diagnose_monitor():
    if _db is None:
        return "DATABASE_URL is required."
    monitor_job = auto_scheduler.get_job("auto_site_monitor")
    if AUTO_ENABLED and monitor_job:
        next_run = monitor_job.next_run_time
        next_run = next_run.strftime("%Y-%m-%d %H:%M:%S %Z") if next_run else "pending"
        scheduler_line = f"ON, next run {next_run}"
    else:
        scheduler_line = "OFF"
    sites = await _db.sites.find({}).sort("created_at", 1).to_list(length=20)
    queued = await _db.items.count_documents({"status": "queued"})
    baseline = await _db.items.count_documents({"status": "baseline"})
    lines = [
        "<b>Auto monitor test</b>",
        f"Enabled env: <code>{AUTO_ENABLED}</code>",
        f"Scheduler: <code>{escape(scheduler_line)}</code>",
        f"Auto chat: <code>{escape(str(AUTO_CHAT))}</code>",
        f"Sites in DB: <code>{len(sites)}</code>",
        f"Queued items in DB: <code>{queued}</code>",
        f"Baseline items in DB: <code>{baseline}</code>",
    ]
    for site in sites[:8]:
        lines.append(
            f"• {escape(site.get('name', 'Site'))} "
            f"[{escape(site.get('type', 'auto'))}] "
            f"<code>{escape(site['url'])}</code>"
        )
        if site.get("last_error"):
            lines.append(f"  Last error: <code>{escape(site['last_error'][:180])}</code>")
    return "\n".join(lines)


@new_task
async def test_monitor(_, message):
    parts = message.text.split(maxsplit=2)
    if len(parts) >= 3 and parts[1].lower() == "leech":
        item = {
            "title": "Auto monitor manual leech test",
            "url": parts[2].strip(),
            "page_url": "manual-test",
        }
        try:
            await _dispatch(item)
            return await sendMessage(
                message,
                "Test leech dispatched. Check the auto monitor chat/status.",
            )
        except Exception as error:
            return await sendMessage(message, f"Test leech failed: <code>{escape(str(error))}</code>")
    await sendMessage(message, await _diagnose_monitor())


@new_task
async def test_site(_, message):
    parts = message.text.split(maxsplit=2)
    if len(parts) < 2:
        return await sendMessage(message, "Usage: /testsite URL [auto|rss|html|mv]")
    url = _canonical_url(parts[1])
    kind = parts[2].strip().lower() if len(parts) > 2 else "auto"
    if kind not in SITE_TYPES:
        return await sendMessage(message, "Type must be auto, rss, html, or mv.")
    waiting = await sendMessage(
        message,
        f"Testing <code>{escape(url)}</code> as <code>{kind}</code>…",
    )
    try:
        items = await _scrape({"url": url, "type": kind})
        lines = [
            "<b>Test site complete</b>",
            f"URL: <code>{escape(url)}</code>",
            f"Type: <code>{kind}</code>",
            f"Found links: <code>{len(items)}</code>",
        ]
        for index, item in enumerate(items[:3], 1):
            lines.append(
                f"\n<b>{index}. {escape(_short_text(item['title'], 90))}</b>\n"
                f"<code>{escape(_short_text(item['url'], 320))}</code>"
            )
        if len(items) > 3:
            lines.append(f"\n…and {len(items) - 3} more. Use /mv for buttons or /checksites to queue.")
        await _safe_edit(waiting, "\n".join(lines), disable_web_page_preview=True)
    except Exception as error:
        await _safe_edit(
            waiting,
            f"Test site failed: <code>{escape(_short_text(error, 900))}</code>",
            disable_web_page_preview=True,
        )


@new_task
async def mv_view(_, message):
    url = message.command[1] if len(message.command) > 1 else MV_SITE_URL
    if not url:
        return await sendMessage(message, "Set MV_SITE_URL or use /mv https://site.example")
    waiting = await sendMessage(message, "Loading latest releases…")
    try:
        now = datetime.now(timezone.utc)
        expired = [
            key for key, value in _mv_cache.items()
            if now - value["created_at"] > timedelta(minutes=30)
        ]
        for key in expired:
            _mv_cache.pop(key, None)
        items = await _scrape_mv(url)
        grouped = {}
        for item in items:
            grouped.setdefault(item["title"], []).append(item)
        rows = []
        for title, links in list(grouped.items())[:15]:
            cache_id = _key(title + links[0]["page_url"])[:16]
            _mv_cache[cache_id] = {
                "owner": message.from_user.id,
                "items": links,
                "created_at": now,
            }
            rows.append([InlineKeyboardButton(title[:55], callback_data=f"mvshow:{cache_id}")])
        if not rows:
            return await waiting.edit_text("No torrent or magnet links were found.")
        await waiting.edit_text(
            "<b>Select a release</b>",
            reply_markup=InlineKeyboardMarkup(rows),
        )
    except Exception as error:
        await waiting.edit_text(f"MV fetch failed: <code>{escape(str(error))}</code>")


@new_task
async def mv_callback(_, query):
    action, cache_id, *rest = query.data.split(":")
    cached = _mv_cache.get(cache_id)
    if not cached:
        return await query.answer("This result expired. Run /mv again.", show_alert=True)
    if cached["owner"] != query.from_user.id and query.from_user.id != OWNER_ID:
        return await query.answer("Run /mv yourself to use these buttons.", show_alert=True)
    items = cached["items"]
    await query.answer()
    if action == "mvshow":
        rows = []
        for index, item in enumerate(items[:20]):
            label = "Magnet" if item["url"].startswith("magnet:") else "Torrent"
            rows.append([
                InlineKeyboardButton(f"View {label} {index + 1}", callback_data=f"mvlink:{cache_id}:{index}"),
                InlineKeyboardButton(f"Leech {index + 1}", callback_data=f"mvleech:{cache_id}:{index}"),
            ])
        return await query.message.edit_text(
            f"<b>{escape(items[0]['title'])}</b>\nChoose a link:",
            reply_markup=InlineKeyboardMarkup(rows),
        )
    if not rest or not rest[0].isdigit():
        return await query.answer("Invalid selection.", show_alert=True)
    index = int(rest[0])
    if index >= len(items):
        return await query.answer("This selection expired.", show_alert=True)
    item = items[index]
    if action == "mvlink":
        return await query.message.reply_text(
            f"<b>{escape(item['title'])}</b>\n<code>{escape(item['url'])}</code>",
            disable_web_page_preview=True,
        )
    await query.message.reply_text("Added to the WZML-X leech queue.")
    await _dispatch(item)


async def scheduled_check():
    if AUTO_ENABLED:
        try:
            await _run_check()
        except Exception:
            LOGGER.exception("Scheduled auto monitor run failed")


if AUTO_ENABLED and DATABASE_URL:
    auto_scheduler.add_job(
        scheduled_check,
        "interval",
        seconds=AUTO_INTERVAL,
        id="auto_site_monitor",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=20),
    )
    if not auto_scheduler.running:
        auto_scheduler.start()
    LOGGER.info(
        "Auto monitor enabled: interval=%ss chat=%s max_items=%s",
        AUTO_INTERVAL,
        AUTO_CHAT,
        f"{AUTO_MAX_ITEMS}, max_tasks_per_run={AUTO_MAX_TASKS_PER_RUN}",
    )

bot.add_handler(MessageHandler(auto_sites, filters=command("autosites") & CustomFilters.sudo))
bot.add_handler(MessageHandler(add_site, filters=command("addsite") & CustomFilters.sudo))
bot.add_handler(MessageHandler(delete_site, filters=command("delsite") & CustomFilters.sudo))
bot.add_handler(MessageHandler(reset_site, filters=command("resetsite") & CustomFilters.sudo))
bot.add_handler(MessageHandler(check_sites, filters=command("checksites") & CustomFilters.sudo))
bot.add_handler(MessageHandler(test_monitor, filters=command("testmonitor") & CustomFilters.sudo))
bot.add_handler(MessageHandler(test_site, filters=command("testsite") & CustomFilters.sudo))
bot.add_handler(MessageHandler(mv_view, filters=command("mv") & CustomFilters.authorized))
bot.add_handler(CallbackQueryHandler(mv_callback, filters=regex(r"^mv(show|link|leech):")))
