#!/usr/bin/env python3
"""Website monitoring, MV-style discovery, IMDb cards, and auto-leech dispatch.

This module deliberately reuses WZML-X's download/upload queue.  It only finds
new links and creates normal qbleech/leech tasks, so torrent selection, split
uploads, limits, cancellation, and status reporting remain in one engine.
"""

import asyncio
import hashlib
import os
import re
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import urljoin, urlparse

import feedparser
from aiohttp import ClientSession, ClientTimeout
from bs4 import BeautifulSoup
from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram.filters import command, regex
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot import (
    DATABASE_URL,
    LOGGER,
    OWNER_ID,
    bot,
    config_dict,
    scheduler,
    user_data,
)
from bot.helper.ext_utils.bot_utils import new_task
from bot.helper.telegram_helper.filters import CustomFilters
from bot.helper.telegram_helper.message_utils import sendMessage
from bot.modules.mirror_leech import leech, qb_leech


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
    )
}
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
AUTO_MAX_ITEMS = max(1, int(os.getenv("AUTO_MAX_ITEMS_PER_SITE", "10")))
AUTO_LEECH_EXISTING = os.getenv("AUTO_LEECH_EXISTING", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
AUTO_FORWARD_CHATS = [
    value.strip()
    for value in re.split(r"[\s,]+", os.getenv("AUTO_FORWARD_CHATS", ""))
    if _configured(value)
]
MV_SITE_URL = os.getenv("MV_SITE_URL", "").strip()
MV_SITE_URL = MV_SITE_URL if _configured(MV_SITE_URL) else ""
OMDB_API_KEY = os.getenv("OMDB_API_KEY", "").strip()
OMDB_API_KEY = OMDB_API_KEY if _configured(OMDB_API_KEY) else ""

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


def _key(value):
    return hashlib.sha256(value.encode("utf-8", "ignore")).hexdigest()


def _clean_title(value):
    value = re.sub(r"\.(torrent|mkv|mp4|avi|mov|webm|zip|rar)$", "", value, flags=re.I)
    value = re.sub(r"[\[\(\{].*?[\]\)\}]", " ", value)
    value = re.sub(r"\b(2160p|1080p|720p|480p|x26[45]|hevc|web-?dl|bluray|hdrip)\b", " ", value, flags=re.I)
    return re.sub(r"[\W_]+", " ", value).strip()


async def _fetch(url):
    timeout = ClientTimeout(total=35)
    async with ClientSession(headers=HEADERS, timeout=timeout) as session:
        async with session.get(url, allow_redirects=True) as response:
            response.raise_for_status()
            return await response.text(errors="replace"), str(response.url)


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
    for title, page in list(dict.fromkeys(detail_pages))[:AUTO_MAX_ITEMS]:
        for link in await _scrape_detail(page):
            results.append({"title": title, "url": link, "page_url": page})
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
    for title, page in list(dict.fromkeys(topics))[:AUTO_MAX_ITEMS]:
        for link in await _scrape_detail(page):
            results.append({"title": title, "url": link, "page_url": page})
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
    use_dumps = await _prepare_forward_dumps()
    is_torrent = item["url"].startswith("magnet:") or ".torrent" in item["url"].lower()
    cmd = "qbleech" if is_torrent else "leech"
    dump_arg = " -ud all" if use_dumps else ""
    text = f"/{cmd} {item['url']}{dump_arg}\nTag: Auto {OWNER_ID}"
    message = await bot.send_message(AUTO_CHAT, text, disable_web_page_preview=True)
    message.from_user = await bot.get_users(OWNER_ID)
    if is_torrent:
        qb_leech(bot, message)
    else:
        leech(bot, message)


async def _ensure_indexes():
    if _db is None:
        return
    await _db.sites.create_index("url", unique=True)
    await _db.items.create_index("key", unique=True)
    if MV_SITE_URL:
        await _db.sites.update_one(
            {"url": MV_SITE_URL},
            {"$setOnInsert": {
                "url": MV_SITE_URL,
                "name": "MV",
                "type": "mv",
                "enabled": True,
                "created_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )


async def _run_check(force=False):
    if _db is None:
        raise RuntimeError("DATABASE_URL is required for auto monitoring")
    if _check_lock.locked() and not force:
        return {"sites": 0, "new": 0, "errors": 0}
    async with _check_lock:
        await _ensure_indexes()
        sites = await _db.sites.find({"enabled": True}).to_list(length=None)
        stats = {"sites": len(sites), "new": 0, "errors": 0}
        for site in sites:
            try:
                items = await _scrape(site)
                if not site.get("initialized", False) and not AUTO_LEECH_EXISTING:
                    for item in items[:AUTO_MAX_ITEMS]:
                        key = _key(item["url"])
                        await _db.items.update_one(
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
                await _db.sites.update_one(
                    {"_id": site["_id"]},
                    {"$set": {
                        "initialized": True,
                        "last_checked": datetime.now(timezone.utc),
                        "last_error": "",
                    }},
                )
            except Exception as error:
                stats["errors"] += 1
                LOGGER.exception("Auto monitor failed for %s", site.get("url"))
                await _db.sites.update_one(
                    {"_id": site["_id"]},
                    {"$set": {
                        "last_checked": datetime.now(timezone.utc),
                        "last_error": str(error)[:500],
                    }},
                )
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
        lines.append(
            f"{index}. <b>{escape(site.get('name', 'Site'))}</b> "
            f"[{state}/{escape(site.get('type', 'auto'))}]\n"
            f"<code>{escape(site['url'])}</code>"
        )
    await sendMessage(message, "\n\n".join(lines))


@new_task
async def add_site(_, message):
    if _db is None:
        return await sendMessage(message, "DATABASE_URL is required.")
    parts = message.text.split(maxsplit=3)
    if len(parts) < 2:
        return await sendMessage(message, "Usage: /addsite URL [auto|rss|html|mv] [name]")
    url = parts[1].strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return await sendMessage(message, "Site URL must start with http:// or https://")
    kind = parts[2].lower() if len(parts) > 2 else "auto"
    if kind not in {"auto", "rss", "html", "mv"}:
        return await sendMessage(message, "Type must be auto, rss, html, or mv.")
    name = parts[3] if len(parts) > 3 else urlparse(url).netloc
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
    result = await _db.sites.delete_one({"url": parts[1].strip()})
    await sendMessage(message, "Site removed." if result.deleted_count else "Site not found.")


@new_task
async def check_sites(_, message):
    status = await sendMessage(message, "Checking monitored sites now…")
    try:
        stats = await _run_check(force=True)
        await status.edit_text(
            f"<b>Check complete</b>\nSites: {stats['sites']}\n"
            f"New tasks: {stats['new']}\nErrors: {stats['errors']}"
        )
    except Exception as error:
        await status.edit_text(f"Check failed: <code>{escape(str(error))}</code>")


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
    scheduler.add_job(
        scheduled_check,
        "interval",
        seconds=AUTO_INTERVAL,
        id="auto_site_monitor",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=20),
    )

bot.add_handler(MessageHandler(auto_sites, filters=command("autosites") & CustomFilters.sudo))
bot.add_handler(MessageHandler(add_site, filters=command("addsite") & CustomFilters.sudo))
bot.add_handler(MessageHandler(delete_site, filters=command("delsite") & CustomFilters.sudo))
bot.add_handler(MessageHandler(check_sites, filters=command("checksites") & CustomFilters.sudo))
bot.add_handler(MessageHandler(mv_view, filters=command("mv") & CustomFilters.authorized))
bot.add_handler(CallbackQueryHandler(mv_callback, filters=regex(r"^mv(show|link|leech):")))
